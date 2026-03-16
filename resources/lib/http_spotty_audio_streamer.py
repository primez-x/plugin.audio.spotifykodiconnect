"""
HTTP server for Spotify audio streams. WAV (PCM) path only.
We serve standard HTTP range semantics so Kodi's cache/buffer settings take effect.
"""

import threading
import time
import uuid
from typing import Callable, Optional

import bottle
import spotipy
import xbmc
import xbmcaddon
import xbmcgui
from spotty import Spotty
from spotty_audio_streamer import SpottyAudioStreamer
from utils import ADDON_ID, ADDON_WINDOW_ID, LOGDEBUG, get_cached_auth_token, log_msg


_settings_cache = {
    "bitrate": "320",
    "normalization": "auto",
    "last_update_time": 0.0,
}
_settings_cache_lock = threading.Lock()
_SETTINGS_CACHE_TTL = 1.0  # Cache for 1 second


def _get_current_stream_settings():
    """Read addon settings with caching to avoid expensive xbmcaddon.Addon() creation.

    Returns (bitrate, normalization)
    """
    global _settings_cache, _settings_cache_lock

    current_time = time.time()
    with _settings_cache_lock:
        if current_time - _settings_cache["last_update_time"] < _SETTINGS_CACHE_TTL:
            return _settings_cache["bitrate"], _settings_cache["normalization"]

        try:
            addon = xbmcaddon.Addon(id=ADDON_ID)
            bitrate_raw = (addon.getSetting("spotify_bitrate") or "320").strip()
            bitrate = bitrate_raw if bitrate_raw in ("96", "160", "320") else "320"
            norm = (addon.getSetting("spotify_normalization") or "auto").strip().lower()
            if norm not in ("off", "auto", "track", "album"):
                norm = "auto"

            _settings_cache["bitrate"] = bitrate
            _settings_cache["normalization"] = norm
            _settings_cache["last_update_time"] = current_time
            return bitrate, norm
        except Exception:
            return _settings_cache["bitrate"], _settings_cache["normalization"]


# No debounce: serve every range request immediately so Kodi's seek bar and
# Player.Progress update right away. Let Kodi drive; we just fulfill each request.


class HTTPSpottyAudioStreamer:
    def __init__(
        self,
        spotty: Spotty,
        normalization_gain_type: str = "auto",
        prebuffer_manager=None,
        on_track_started_callback: Optional[Callable[[str, float], None]] = None,
        use_autoplay: bool = False,
        bitrate: str = "320",
    ):
        self.__spotty: Spotty = spotty
        self.__prebuffer_manager = prebuffer_manager
        self.__on_track_started = on_track_started_callback or (lambda _id, _dur: None)
        self.__notify_track_finished: Callable[[str], None] = lambda _id: None

        self.__spotty_streamer: SpottyAudioStreamer = SpottyAudioStreamer(self.__spotty)
        self.__spotty_streamer.normalization_gain_type = (
            normalization_gain_type or "auto"
        ).strip().lower() or "auto"
        self.__spotty_streamer.use_autoplay = use_autoplay
        self.__spotty_streamer.bitrate = bitrate
        self.__is_streaming = False
        self.__stream_lock = threading.Lock()
        self.__current_track_id: Optional[str] = None
        self.__current_request_id: str = ""  # Track current request to ignore stale generators
        # Init coordination: when a new-track GET is being initialized, set this
        # so other concurrent GETs can wait and then reuse the same request id.
        self.__init_in_progress = False
        self.__init_event = threading.Event()
        self.__init_event.set()

    def set_normalization_gain_type(self, value: str) -> None:
        self.__spotty_streamer.normalization_gain_type = (
            value or "auto"
        ).strip().lower() or "auto"

    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        self.__notify_track_finished = func or (lambda _id: None)
        self.__spotty_streamer.set_notify_track_finished(self.__notify_track_finished)

    def set_stream_ended(self) -> None:
        """Mark that the current stream has finished so the next request starts fresh."""
        with self.__stream_lock:
            self.__is_streaming = False
            self.__current_track_id = None

    def is_current_track_streaming(self, track_id: str) -> bool:
        """Check if the given track is still being streamed to Kodi."""
        with self.__stream_lock:
            return self.__is_streaming and self.__current_track_id == track_id

    def set_on_track_started(self, func: Callable[[str, float], None]) -> None:
        self.__on_track_started = func or (lambda _id, _dur: None)

    def set_prebuffer_manager(self, manager) -> None:
        self.__prebuffer_manager = manager

    def stop(self) -> None:
        log_msg("Stopping spotty audio streaming.", LOGDEBUG)
        if self.__is_streaming:
            self.__terminate_streaming()
        else:
            log_msg("No running audio streamer. Nothing to stop.", LOGDEBUG)

    def __terminate_streaming(self) -> None:
        if self.__spotty_streamer.terminate_stream():
            log_msg("Terminated running streamer.", LOGDEBUG)
        else:
            log_msg("No running streamer. Nothing to terminate.", LOGDEBUG)

    SPOTTY_AUDIO_TRACK_ROUTE = "/track/<track_id>/<duration>"

    def spotty_stream_audio_track(
        self, track_id: str, duration: str
    ) -> bottle.Response:
        # Strip optional .wav suffix — URLs include it so Kodi selects PAPlayer
        # directly instead of trying VideoPlayer first (demuxer error + retry).
        if duration.endswith(".wav"):
            duration = duration[:-4]
        log_msg(f"{bottle.request.method} request: {bottle.request}", LOGDEBUG)

        # HEAD requests: return headers only. NEVER mutate state, call set_track(),
        # overwrite __current_request_id, or fire on_track_started. HEAD probes must
        # be invisible to the streaming pipeline.
        if bottle.request.method.upper() != "GET":
            return self._handle_head_only(track_id, duration)

        # Generate unique request ID to prevent stale generators from executing
        request_id = str(uuid.uuid4())

        # Read settings FIRST.
        bitrate, norm = _get_current_stream_settings()

        request_range = bottle.request.headers.get("Range", "")
        is_new_track = not self.__is_streaming or self.__current_track_id != track_id

        _r = (request_range or "").strip()
        from_start = (
            not _r
            or _r == "bytes=0-"
            or (_r.startswith("bytes=0-") and len(_r) > 8)  # bytes=0-1048575 etc.
        )

        if from_start:
            # Always re-init from start for WAV mode (header sent immediately).
            is_new_track = True

        # If this is a new track, coordinate initialization so multiple concurrent
        # GET handlers don't all start spotty processes in parallel.
        if is_new_track:
            with self.__stream_lock:
                # If another init is already in progress for same track, wait briefly.
                if self.__init_in_progress and self.__current_track_id == track_id:
                    log_msg(f"Init already in progress for {track_id}, waiting.", LOGDEBUG)
                    # Wait up to 1s for init to complete
                    self.__init_event.wait(1.0)
                    # After wait, if streamer was initialized, treat as non-new
                    if self.__is_streaming and self.__current_track_id == track_id:
                        is_new_track = False
                        request_id = self.__current_request_id
                elif self.__is_streaming and self.__current_track_id == track_id:
                    # Already streaming same track — reuse request id.
                    is_new_track = False
                    request_id = self.__current_request_id
                else:
                    # We are the initializer for this new track. Reserve slot.
                    self.__init_in_progress = True
                    self.__init_event.clear()
                    self.__is_streaming = True
                    self.__current_track_id = track_id
                    self.__current_request_id = request_id
                    # release lock and continue initialization below
                    # (will clear init_in_progress after set_track)
                    pass
        else:
            # Not a new track — reuse request id if already streaming this track.
            with self.__stream_lock:
                if self.__is_streaming and self.__current_track_id == track_id:
                    request_id = self.__current_request_id

        # Fetch prebuffer result (WAV bytes if prebuffer was used).
        prebuf_result = None
        has_prebuf = False
        if is_new_track and self.__prebuffer_manager:
            prebuf_result, has_prebuf = (
                self.__prebuffer_manager.get_and_clear_prebuffer(track_id)
            )
            if has_prebuf:
                kind = f"{len(prebuf_result.data)} bytes"
                log_msg(f"Prebuffer hit for track {track_id} ({kind}).", LOGDEBUG)

        if is_new_track:
            # Cancel any running prebuffer immediately so its spotty process doesn't
            # compete with the main stream for the single Spotify connection.  A
            # prebuffer from the *previous* track selection may still be running
            # (the 5-second deferred start in main_service only delays the *next*
            # prebuffer, it doesn't cancel an already-running one).
            if self.__prebuffer_manager:
                self.__prebuffer_manager.cancel_prebuffer()

            # Terminate the previous track's stream — unless this is Kodi's QueueNextFileEx
            # pre-load (new track ID while the previous track's download is already complete
            # in the cache).  In that case, killing the HTTP generator cuts the current track
            # ~5 seconds short; let it drain naturally instead.  send_part_audio_stream()
            # captures its track-specific state at entry, so two generators running briefly
            # in parallel read from independent cache entries without interfering.
            # If the previous download is still in progress we must terminate: a competing
            # spotty process would cause a mutual-kick session conflict.
            _skip_terminate = False
            if self.__current_track_id and self.__current_track_id != track_id:
                from spotty_cache import SpottyCacheManager
                _cur_dl = SpottyCacheManager.find_best_downloader(self.__current_track_id, 0)
                if _cur_dl and _cur_dl.is_finished and not _cur_dl.error:
                    _skip_terminate = True
                    log_msg(
                        f"QueueNextFileEx detected: {self.__current_track_id} download complete, "
                        f"not terminating stream — letting generator drain for seamless transition "
                        f"to {track_id}.",
                        LOGDEBUG,
                    )
            if not _skip_terminate:
                self.__terminate_streaming()

            # Set up new track with proper locking to prevent concurrent overwrites
            with self.__stream_lock:
                self.__spotty_streamer.bitrate = bitrate
                self.__spotty_streamer.normalization_gain_type = norm
                self.__spotty_streamer.set_track(track_id, float(duration))
                self.__is_streaming = True
                self.__current_track_id = track_id
                self.__current_request_id = request_id
            # Initialization complete — clear init flag and notify waiters.
            with self.__stream_lock:
                if self.__init_in_progress:
                    self.__init_in_progress = False
                    try:
                        self.__init_event.set()
                    except Exception:
                        pass
            log_msg(
                f"Start streaming spotify track '{track_id}',"
                f" track length {self.__spotty_streamer.get_track_length()}."
            )

            # Fire and forget notification
            threading.Thread(
                target=self.__on_track_started,
                args=(track_id, float(duration)),
                daemon=True,
            ).start()

        log_msg(f"Request header range: '{request_range}'.", LOGDEBUG)

        return self._handle_wav_request(
            is_new_track,
            request_range,
            prebuf_result,
            has_prebuf,
            track_id=track_id,
            duration_str=duration,
            request_id=request_id,
        )

    spotty_stream_audio_track.route = SPOTTY_AUDIO_TRACK_ROUTE

    def _handle_head_only(self, track_id: str, duration: str):
        """Return headers for HEAD requests without touching any streaming state."""
        try:
            dur = max(1.0, float(duration))
        except (ValueError, TypeError):
            dur = 1.0

        # Always derive size from the URL's duration — the current streamer may have a
        # different track loaded, which would return the wrong Content-Length for queued
        # (non-current) tracks and confuse Kodi's prefetch queue.
        pcm_bps = 44100 * 2 * 2  # 176400 bytes/sec at 44.1 kHz 16-bit stereo
        file_size = int(dur * pcm_bps) + 44  # +44 for WAV header

        bottle.response.status = 200
        bottle.response.content_type = "audio/x-wav"
        bottle.response.content_length = file_size
        bottle.response.headers["Accept-Ranges"] = "bytes"

        log_msg(
            f"HEAD response: track={track_id}, content_length={bottle.response.content_length}",
            LOGDEBUG,
        )
        return ""

    # ------------------------------------------------------------------
    #  WAV (PCM) path — standard HTTP range semantics
    # ------------------------------------------------------------------

    def _handle_wav_request(
        self,
        is_new_track,
        request_range,
        prebuf_result,
        has_prebuf,
        track_id=None,
        duration_str=None,
        request_id=None,
    ):
        streamer = self.__spotty_streamer
        # Parse duration from request URL
        _duration_sec = 1.0
        if track_id and duration_str:
            try:
                _duration_sec = max(1.0, float(duration_str))
            except (ValueError, TypeError):
                pass

        file_size = streamer.get_track_length()
        range_begin = 0
        range_end = file_size
        is_seek = False

        # Only call set_track if this is a new track or file_size is invalid.
        # For new tracks, set_track() was already called in spotty_stream_audio_track() inside the lock.
        # This is just a safety net for recovery if file_size is invalid.
        if (file_size <= 0 or file_size < 50000) and track_id:
            # Compute WAV header length without mutating shared streamer state to
            # ensure HEAD/early requests can return accurate Content-Length.
            try:
                from spotty_audio_streamer import create_wav_header_for_duration

                _, total_length = create_wav_header_for_duration(_duration_sec)
                file_size = total_length
                range_end = file_size
                log_msg(
                    f"Computed WAV header length from duration={_duration_sec}s -> file_size={file_size}.",
                    LOGDEBUG,
                )
            except Exception:
                # Fallback to previous behavior (mutating streamer) if static header generation fails.
                try:
                    streamer.set_track(track_id, _duration_sec)
                    file_size = streamer.get_track_length()
                    range_end = file_size
                    log_msg(
                        f"Recovered track length from URL (duration={_duration_sec}s), file_size={file_size}.",
                        LOGDEBUG,
                    )
                except Exception:
                    pass

        prebuf_data = prebuf_result.data if (has_prebuf and prebuf_result) else None

        if not request_range or (request_range == "bytes=0-"):
            status = 200
            content_range = ""
            log_msg(
                f"Full request, content length = {range_end - range_begin}.", LOGDEBUG
            )
        else:
            status = "206 Partial Content"
            try:
                parts = (
                    bottle.request.headers["Range"]
                    .strip()
                    .split("bytes=", 1)[1]
                    .split("-", 1)
                )
                start_s = parts[0].strip() if parts else ""
                end_s = parts[1].strip() if len(parts) > 1 else ""
                if not start_s and end_s.isdigit():
                    suffix = int(end_s)
                    range_begin = max(0, file_size - suffix)
                    range_end = file_size
                else:
                    range_begin = int(start_s) if start_s else 0
                    range_end = int(end_s) if end_s.isdigit() else file_size
                range_begin = max(0, min(range_begin, file_size))
                range_end = max(range_begin, min(range_end, file_size))
            except (ValueError, IndexError, KeyError):
                range_begin = 0
                range_end = file_size
            # Content-Range end is inclusive — use range_end - 1
            content_range = f"bytes {range_begin}-{range_end - 1}/{file_size}"
            if not is_new_track and range_begin > 0:
                is_seek = True
            # User selected a different track (e.g. from playlist while one was playing).
            # Kodi may send a stale Range—serve from start. Use a tiny first chunk (256 bytes)
            # so the first response returns immediately (UI snappy); Kodi then requests the
            # rest using its cache.chunksize for everything after.
            if is_new_track and range_begin > 0:
                range_begin = 0
                range_end = min(file_size, 65536)
                content_range = f"bytes 0-{range_end - 1}/{file_size}"
                status = "206 Partial Content"
                log_msg(
                    f"New track request had range_begin>0 (stale?), serving first chunk from start (size={range_end}).",
                    LOGDEBUG,
                )
            log_msg(
                f"Partial request, range = {content_range},"
                f" length = {range_end - range_begin}",
                LOGDEBUG,
            )

        # Re-apply this request's track at stream time so we stream the correct track even if
        # a concurrent request overwrote the shared streamer (reduces wrong-track/0-length).
        # We NO LONGER defer killing the previous spotty. The background cache handles it.
        is_seek_to_start = False

        # Check if this request is stale BEFORE returning generator (before HTTP headers commit)
        if request_id and request_id != self.__current_request_id:
            log_msg(f"WAV request {request_id} is stale (current: {self.__current_request_id}), returning empty.", LOGDEBUG)
            bottle.response.status = 204  # No Content
            return ""

        def generate():
            # Double-check inside generator as safety net
            with self.__stream_lock:
                # Only proceed if this is still the active request
                if request_id and request_id != self.__current_request_id:
                    log_msg(f"Generator for request {request_id} is stale (current: {self.__current_request_id}), aborting.", LOGDEBUG)
                    return

            try:
                if is_seek:
                    # Do NOT terminate the active stream on seek. Kodi opens multiple connections
                    # (e.g. GET bytes=0- then GET bytes=20- after reading WAV header). Terminating
                    # here closed the first connection prematurely, causing "Transferred a partial
                    # file" and "Open - Unhandled exception". Both connections can read from cache.
                    log_msg(
                        f"Seek to byte {range_begin}, streaming from cache (parallel read, no terminate).",
                        LOGDEBUG,
                    )
                elif is_new_track:
                    # Previous track already terminated in spotty_stream_audio_track();
                    # do not terminate here or a second GET for this track would kill the first.
                    log_msg(
                        "New track: streaming from cache (previous stream already terminated).",
                        LOGDEBUG,
                    )

                r_begin = range_begin
                r_end = range_end
                r_len = r_end - r_begin
                if prebuf_data:
                    prebuffer_len = len(prebuf_data)
                    if r_begin < prebuffer_len:
                        end_from_buf = min(r_end, prebuffer_len)
                        yield prebuf_data[r_begin:end_from_buf]
                    if r_end > prebuffer_len:
                        rest_begin = max(r_begin, prebuffer_len)
                        rest_len = r_end - rest_begin
                        yield from streamer.send_part_audio_stream(
                            rest_len, rest_begin
                        )
                else:
                    yield from streamer.send_part_audio_stream(
                        r_len, r_begin
                    )
            except GeneratorExit:
                # Back can mean "close OSD" (playback continues) or "cancel". Do NOT clear
                # state here—we only clear when the track truly ends (__notify_track_finished).
                # Next range request will still see same track; "from start" requests always re-init.
                raise
            except (BrokenPipeError, ConnectionResetError, OSError):
                # Pipe/connection error: do NOT clear state here. This often happens when
                # we kill the stream for a seek (another request called __terminate_streaming);
                # clearing would make the next request look like a new track and cause
                # desync, no audio, and skip storms. Only GeneratorExit means client left.
                log_msg(
                    "Stream read/write error, not clearing state.",
                    LOGDEBUG,
                )
                raise

        bottle.response.status = status
        bottle.response.headers["Accept-Ranges"] = "bytes"
        bottle.response.content_type = "audio/x-wav"
        bottle.response.content_length = range_end - range_begin
        if content_range:
            bottle.response.headers["Content-Range"] = content_range

        if bottle.request.method.upper() == "GET":
            return generate()
        return ""

    def toggle_track_like(self, track_id: str) -> bottle.Response:
        """Toggle the liked status of a track in Spotify"""
        try:
            # Get the authentication token
            token = get_cached_auth_token()
            if not token:
                bottle.response.status = 401
                return "Unauthorized"

            # Create Spotify client
            sp = spotipy.Spotify(auth=token)

            # Check if track is currently liked
            result = sp.current_user_saved_tracks_contains([track_id])
            is_liked = result[0] if result else False

            # Toggle the like status
            if is_liked:
                # Unlike the track
                sp.current_user_saved_tracks_delete([track_id])
                liked_status = "false"
            else:
                # Like the track
                sp.current_user_saved_tracks_add([track_id])
                liked_status = "true"

            # Update the window property for the current track
            win = xbmcgui.Window(ADDON_WINDOW_ID)
            win.setProperty("Spotify.CurrentTrackLiked", liked_status)

            # Also update the window property to indicate the change occurred
            win.setProperty("Spotify.TrackLikeChanged", "true")

            # Clear the change flag after a short delay to allow UI to react
            def clear_flag():
                xbmc.sleep(100)
                win.clearProperty("Spotify.TrackLikeChanged")

            threading.Thread(target=clear_flag, daemon=True).start()

            # Return success response
            bottle.response.content_type = "application/json"
            return {"success": True, "liked": liked_status == "true"}

        except Exception as e:
            log_msg(f"Error toggling track like status: {e}", LOGDEBUG)
            bottle.response.status = 500
            return {"success": False, "error": str(e)}

    toggle_track_like.route = "/toggle_like/<track_id>"
