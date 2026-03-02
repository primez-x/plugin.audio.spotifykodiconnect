"""
HTTP server for Spotify audio streams. Policy: let Kodi drive.

WAV (PCM) path: standard HTTP range semantics (Content-Length, Accept-Ranges,
Content-Range, 206 Partial Content) so Kodi's own cache/buffer settings take
full effect.
"""

import threading
import time
import uuid
from typing import Callable, Optional

import bottle
import spotipy
import xbmcaddon
import xbmcgui
from spotty import Spotty
from spotty_audio_streamer import SpottyAudioStreamer
from utils import ADDON_ID, LOGDEBUG, get_cached_auth_token, log_msg


# Settings cache to avoid expensive xbmcaddon.Addon() creation on every request
_settings_cache = {
    "use_passthrough": False,
    "bitrate": "320",
    "normalization": "auto",
    "last_update_time": 0.0,
}
_settings_cache_lock = threading.Lock()
_SETTINGS_CACHE_TTL = 1.0  # Cache for 1 second


def _get_current_stream_settings():
    """Read addon settings with caching to avoid expensive xbmcaddon.Addon() creation.

    Settings are cached for 1 second to avoid repeated file I/O on each request,
    while still allowing runtime toggling without restart.
    """
    global _settings_cache, _settings_cache_lock

    current_time = time.time()
    with _settings_cache_lock:
        # Check if cache is still valid
        if current_time - _settings_cache["last_update_time"] < _SETTINGS_CACHE_TTL:
            return (
                _settings_cache["use_passthrough"],
                _settings_cache["bitrate"],
                _settings_cache["normalization"],
            )

        # Cache expired, read from addon (expensive)
        try:
            addon = xbmcaddon.Addon(id=ADDON_ID)
            use_passthrough = addon.getSetting("spotify_passthrough").lower() == "true"
            bitrate_raw = (addon.getSetting("spotify_bitrate") or "320").strip()
            bitrate = bitrate_raw if bitrate_raw in ("96", "160", "320") else "320"
            norm = (addon.getSetting("spotify_normalization") or "auto").strip().lower()
            if norm not in ("off", "auto", "track", "album"):
                norm = "auto"

            # Update cache
            _settings_cache["use_passthrough"] = use_passthrough
            _settings_cache["bitrate"] = bitrate
            _settings_cache["normalization"] = norm
            _settings_cache["last_update_time"] = current_time

            return use_passthrough, bitrate, norm
        except Exception:
            # Fallback to cached values if read fails
            return (
                _settings_cache["use_passthrough"],
                _settings_cache["bitrate"],
                _settings_cache["normalization"],
            )


# No debounce: serve every range request immediately so Kodi's seek bar and
# Player.Progress update right away. Let Kodi drive; we just fulfill each request.

# Minimum seconds between seek restarts (terminate+new spotty). Stops held FF from
# flooding us with seeks and freezing the UI.
SEEK_THROTTLE_SEC = 0.4

# OGG Vorbis byte rates by bitrate (kbps → bytes/sec = kbps * 1000 / 8)
OGG_BPS_BY_BITRATE = {"96": 12000, "160": 20000, "320": 40000}
OGG_BYTES_PER_SEC = OGG_BPS_BY_BITRATE["320"]  # Default (320 kbps)


class _OggStreamBuffer:
    """Buffers OGG data from a spotty process in a background thread.

    Survives HTTP request lifecycle so Kodi retries read from the same buffer
    without respawning spotty (which takes ~1.5s to connect to Spotify).
    The background fill thread owns the spotty process; HTTP generators just
    read from the accumulated buffer.
    """

    def __init__(self, spotty: Spotty):
        self._lock = threading.Lock()
        self._data = bytearray()
        self._data_event = threading.Event()  # signalled when new data arrives or finished
        self._finished = False
        self._track_id: str = ""
        self._start_sec: int = 0
        self._streamer: Optional[SpottyAudioStreamer] = None
        self._spotty = spotty
        self._thread: Optional[threading.Thread] = None

    def start(
        self,
        track_id: str,
        duration_sec: float,
        fake_file_size: int,
        start_sec: int,
        bitrate: str,
        normalization: str,
        is_passthrough: bool,
        defer_kill: bool,
    ) -> None:
        """Spawn spotty in a background thread and buffer its OGG output."""
        self._track_id = track_id
        self._start_sec = start_sec

        # Each buffer gets its own SpottyAudioStreamer to avoid shared-state
        # races with the main streamer or other buffers.
        streamer = SpottyAudioStreamer(self._spotty)
        streamer.bitrate = bitrate
        streamer.normalization_gain_type = normalization
        streamer.use_passthrough = True
        streamer.set_track(track_id, duration_sec, is_passthrough=True)
        self._streamer = streamer

        def _fill():
            try:
                for chunk in streamer.send_part_audio_stream(
                    range_len=fake_file_size,
                    range_begin=0,
                    start_sec=start_sec,
                    is_passthrough=True,
                    defer_kill_previous=defer_kill,
                ):
                    if chunk:
                        with self._lock:
                            if isinstance(chunk, bytes):
                                self._data.extend(chunk)
                            else:
                                self._data.extend(chunk.encode("latin-1"))
                        self._data_event.set()
            except (GeneratorExit, BrokenPipeError, ConnectionResetError, OSError):
                pass
            except Exception as exc:
                log_msg(f"OGG buffer fill error: {exc}", LOGDEBUG)
            finally:
                with self._lock:
                    self._finished = True
                self._data_event.set()

        self._thread = threading.Thread(target=_fill, daemon=True)
        self._thread.start()
        log_msg(
            f"OGG buffer started for track {track_id} "
            f"(start_sec={start_sec}, fake_size={fake_file_size}).",
            LOGDEBUG,
        )

    def read_from(self, offset: int = 0, timeout: float = 10.0):
        """Generator: yield data from *offset* onwards, blocking for new data.

        The timeout resets every time progress is made (new data yielded).
        """
        pos = offset
        deadline = time.time() + timeout
        while True:
            with self._lock:
                available = len(self._data)
                if pos < available:
                    chunk = bytes(self._data[pos:available])
                    pos = available
                    yield chunk
                    deadline = time.time() + timeout
                    continue
                if self._finished:
                    return

            remaining = deadline - time.time()
            if remaining <= 0:
                log_msg(
                    f"OGG buffer read timed out at offset {pos} "
                    f"(available={available}).",
                    LOGDEBUG,
                )
                return
            self._data_event.wait(timeout=min(0.5, remaining))
            self._data_event.clear()

    def stop(self) -> None:
        """Terminate the background fill thread and its spotty process."""
        if self._streamer:
            self._streamer.terminate_stream()
            self._streamer = None
        log_msg(f"OGG buffer stopped for track {self._track_id}.", LOGDEBUG)

    @property
    def track_id(self) -> str:
        return self._track_id

    @property
    def start_sec(self) -> int:
        return self._start_sec


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
        self.__last_seek_terminate_time = 0.0
        self.__seek_throttle_lock = threading.Lock()
        self.__ogg_main_buffer: Optional[_OggStreamBuffer] = None  # Primary playback buffer (bytes=0-)
        self.__ogg_seek_buffer: Optional[_OggStreamBuffer] = None  # Seek/probe buffer (non-zero ranges)

    def set_normalization_gain_type(self, value: str) -> None:
        self.__spotty_streamer.normalization_gain_type = (
            value or "auto"
        ).strip().lower() or "auto"

    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        self.__notify_track_finished = func or (lambda _id: None)
        self.__spotty_streamer.set_notify_track_finished(self.__notify_track_finished)

    def set_stream_ended(self) -> None:
        """Mark that the current stream has finished so the next request starts fresh."""
        if self.__ogg_main_buffer:
            self.__ogg_main_buffer.stop()
            self.__ogg_main_buffer = None
        if self.__ogg_seek_buffer:
            self.__ogg_seek_buffer.stop()
            self.__ogg_seek_buffer = None
        with self.__stream_lock:
            self.__is_streaming = False
            self.__current_track_id = None

    def set_on_track_started(self, func: Callable[[str, float], None]) -> None:
        self.__on_track_started = func or (lambda _id, _dur: None)

    def set_prebuffer_manager(self, manager) -> None:
        self.__prebuffer_manager = manager

    def stop(self) -> None:
        log_msg("Stopping spotty audio streaming.", LOGDEBUG)
        if self.__ogg_main_buffer:
            self.__ogg_main_buffer.stop()
            self.__ogg_main_buffer = None
        if self.__ogg_seek_buffer:
            self.__ogg_seek_buffer.stop()
            self.__ogg_seek_buffer = None
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
        log_msg(f"{bottle.request.method} request: {bottle.request}", LOGDEBUG)

        # HEAD requests: return headers only. NEVER mutate state, call set_track(),
        # overwrite __current_request_id, or fire on_track_started. HEAD probes must
        # be invisible to the streaming pipeline.
        if bottle.request.method.upper() != "GET":
            return self._handle_head_only(track_id, duration)

        # Generate unique request ID to prevent stale generators from executing
        request_id = str(uuid.uuid4())

        # Read settings FIRST — needed to decide from_start behavior per mode.
        use_passthrough, bitrate, norm = _get_current_stream_settings()

        request_range = bottle.request.headers.get("Range", "")
        is_new_track = not self.__is_streaming or self.__current_track_id != track_id

        _r = (request_range or "").strip()
        from_start = (
            not _r
            or _r == "bytes=0-"
            or (_r.startswith("bytes=0-") and len(_r) > 8)  # bytes=0-1048575 etc.
        )

        if from_start:
            if use_passthrough:
                # OGG passthrough: only re-init if track actually changed.
                # Spotty takes ~1.5s to connect to Spotify. Kodi's demuxer probe
                # times out in ~0.5s, causing retries. If we restart spotty on
                # every retry (bytes=0-), we loop forever — spotty never produces
                # data fast enough.  Let the existing stream continue for same-track.
                if self.__current_track_id != track_id:
                    is_new_track = True
            else:
                # WAV mode: always re-init from start (WAV header sent immediately,
                # so no probe timeout issue). Needed for state recovery after FF storms.
                is_new_track = True

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
            if self.__prebuffer_manager and use_passthrough:
                self.__prebuffer_manager.cancel_prebuffer()

            # Set up new track with proper locking to prevent concurrent overwrites
            with self.__stream_lock:
                self.__spotty_streamer.bitrate = bitrate
                self.__spotty_streamer.normalization_gain_type = norm
                self.__spotty_streamer.use_passthrough = use_passthrough
                self.__spotty_streamer.set_track(track_id, float(duration), is_passthrough=use_passthrough)
                self.__is_streaming = True
                self.__current_track_id = track_id
                self.__current_request_id = request_id

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

        if use_passthrough:
            return self._handle_ogg_request(
                is_new_track,
                request_range,
                track_id=track_id,
                duration_str=duration,
                request_id=request_id,
            )
        else:
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
        use_passthrough, bitrate, _ = _get_current_stream_settings()
        try:
            dur = max(1.0, float(duration))
        except (ValueError, TypeError):
            dur = 1.0

        if use_passthrough:
            ogg_bps = OGG_BPS_BY_BITRATE.get(bitrate, OGG_BYTES_PER_SEC)
            fake_size = int(dur * ogg_bps)
            bottle.response.status = 200
            bottle.response.content_type = "application/ogg"
            bottle.response.content_length = fake_size
            bottle.response.headers["Accept-Ranges"] = "bytes"
        else:
            # WAV: use streamer's track length if available, else estimate
            file_size = self.__spotty_streamer.get_track_length()
            if file_size <= 0:
                pcm_bps = 44100 * 2 * 2  # 176400 bytes/sec
                file_size = int(dur * pcm_bps) + 44  # + WAV header
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
    #  OGG (Passthrough) path — synthetic range mapping
    # ------------------------------------------------------------------

    def _handle_ogg_request(
        self,
        is_new_track,
        request_range,
        track_id=None,
        duration_str=None,
        request_id=None,
    ):
        """Handle OGG passthrough with synthetic range mapping for seeking.

        Uses an _OggStreamBuffer to decouple spotty's lifecycle from the HTTP
        request.  Spotty takes ~1.5s to connect; Kodi's demuxer probe times out
        in ~2s.  Without the buffer, every retry kills spotty and restarts it,
        creating an infinite timeout loop.  The buffer keeps spotty alive in a
        background thread so retries read from already-buffered data instantly.
        """
        # Parse duration from URL parameter
        _duration_sec = 1.0
        if track_id and duration_str:
            try:
                _duration_sec = max(1.0, float(duration_str))
                log_msg(
                    f"OGG: parsed duration_str={duration_str} -> {_duration_sec}s",
                    LOGDEBUG,
                )
            except (ValueError, TypeError):
                log_msg(
                    f"OGG: failed to parse duration_str={duration_str}, defaulting to 1.0s",
                    LOGDEBUG,
                )
        else:
            log_msg(
                f"OGG: no duration_str provided (track_id={track_id}, duration_str={duration_str}), defaulting to 1.0s",
                LOGDEBUG,
            )

        # Synthetic file size for range mapping: duration * actual OGG bitrate
        bitrate_str = self.__spotty_streamer.bitrate if hasattr(self.__spotty_streamer, 'bitrate') else "320"
        ogg_bps = OGG_BPS_BY_BITRATE.get(bitrate_str, OGG_BYTES_PER_SEC)
        fake_file_size = int(_duration_sec * ogg_bps)
        range_begin = 0
        start_sec = 0
        is_seek = False

        # Parse range request and convert to seek position
        if request_range and "bytes=" in request_range:
            try:
                range_part = request_range.split("bytes=", 1)[1].split("-")[0]
                if range_part:
                    range_begin = int(range_part)
                    start_sec = int(range_begin / ogg_bps)
                    is_seek = not is_new_track and range_begin > 0
            except (ValueError, IndexError):
                pass

        # Build HTTP response headers for range requests
        status = 200
        content_range = ""
        if request_range and "bytes=" in request_range:
            status = "206 Partial Content"
            range_end = fake_file_size
            content_range = f"bytes {range_begin}-{range_end - 1}/{fake_file_size}"

        log_msg(
            f"OGG Passthrough request: track={track_id}, range={request_range}, "
            f"range_begin={range_begin}, start_sec={start_sec}, is_seek={is_seek}, "
            f"fake_file_size={fake_file_size}",
            LOGDEBUG,
        )

        # Read current settings for the buffer's streamer
        _use_pt, _bitrate, _norm = _get_current_stream_settings()

        # --- Dual buffer management ---
        # Main buffer: serves bytes=0- and small-offset reconnections (start_sec==0).
        # Seek buffer: serves cache probes and seeks (start_sec>0) WITHOUT
        # destroying the main buffer that is actively streaming playback.
        is_main_stream = (start_sec == 0)

        # Update request_id for main stream requests only; seek/probe requests
        # must not invalidate the main stream's generator.
        if is_main_stream and not is_new_track:
            with self.__stream_lock:
                self.__current_request_id = request_id

        # Stale pre-check (main stream only — seek requests bypass this)
        if is_main_stream and request_id and request_id != self.__current_request_id:
            log_msg(f"OGG request {request_id} is stale (current: {self.__current_request_id}), returning empty.", LOGDEBUG)
            bottle.response.status = 204
            return ""

        if is_new_track:
            # New track: stop both buffers, create fresh main buffer
            if self.__ogg_main_buffer:
                self.__ogg_main_buffer.stop()
                self.__ogg_main_buffer = None
            if self.__ogg_seek_buffer:
                self.__ogg_seek_buffer.stop()
                self.__ogg_seek_buffer = None
            log_msg("OGG new track: starting buffer.", LOGDEBUG)

            buf = _OggStreamBuffer(self.__spotty)
            buf.start(
                track_id=track_id,
                duration_sec=_duration_sec,
                fake_file_size=fake_file_size - range_begin,
                start_sec=start_sec,
                bitrate=_bitrate,
                normalization=_norm,
                is_passthrough=True,
                defer_kill=(range_begin == 0),
            )
            self.__ogg_main_buffer = buf
            buffer_to_use = buf
            read_offset = 0

        elif is_main_stream:
            # Main stream continuation/retry (start_sec==0).
            # Reuse main buffer only if it exists and was started at position 0.
            if (self.__ogg_main_buffer
                    and self.__ogg_main_buffer.track_id == track_id
                    and self.__ogg_main_buffer.start_sec == 0):
                log_msg(
                    f"OGG retry: reusing main buffer for track {track_id} "
                    f"(read from offset {range_begin}).",
                    LOGDEBUG,
                )
                buffer_to_use = self.__ogg_main_buffer
                read_offset = range_begin
            else:
                if self.__ogg_main_buffer:
                    self.__ogg_main_buffer.stop()
                log_msg("OGG: main buffer missing or wrong position, creating new one.", LOGDEBUG)
                buf = _OggStreamBuffer(self.__spotty)
                buf.start(
                    track_id=track_id,
                    duration_sec=_duration_sec,
                    fake_file_size=fake_file_size,
                    start_sec=0,
                    bitrate=_bitrate,
                    normalization=_norm,
                    is_passthrough=True,
                    defer_kill=False,
                )
                self.__ogg_main_buffer = buf
                buffer_to_use = buf
                read_offset = 0

        else:
            # Seek / cache probe (start_sec > 0).
            # Use a separate seek buffer — do NOT touch the main playback buffer.
            if self.__ogg_seek_buffer:
                self.__ogg_seek_buffer.stop()
                self.__ogg_seek_buffer = None
                log_msg("OGG: stopped previous seek buffer.", LOGDEBUG)

            with self.__seek_throttle_lock:
                now = time.time()
                elapsed = now - self.__last_seek_terminate_time
                if elapsed < SEEK_THROTTLE_SEC and self.__last_seek_terminate_time > 0:
                    time.sleep(SEEK_THROTTLE_SEC - elapsed)
                self.__last_seek_terminate_time = time.time()
            log_msg(f"OGG seek/probe to {start_sec}s via seek buffer.", LOGDEBUG)

            buf = _OggStreamBuffer(self.__spotty)
            buf.start(
                track_id=track_id,
                duration_sec=_duration_sec,
                fake_file_size=fake_file_size - range_begin,
                start_sec=start_sec,
                bitrate=_bitrate,
                normalization=_norm,
                is_passthrough=True,
                defer_kill=False,
            )
            self.__ogg_seek_buffer = buf
            buffer_to_use = buf
            read_offset = 0

        def generate():
            if is_main_stream:
                with self.__stream_lock:
                    if request_id and request_id != self.__current_request_id:
                        log_msg(f"OGG generator for request {request_id} is stale, aborting.", LOGDEBUG)
                        return

            try:
                yield from buffer_to_use.read_from(offset=read_offset, timeout=10.0)
            except GeneratorExit:
                raise
            except (BrokenPipeError, ConnectionResetError, OSError):
                log_msg("OGG stream read/write error, not clearing state.", LOGDEBUG)
                raise

        bottle.response.status = status
        bottle.response.headers["Accept-Ranges"] = "bytes"
        bottle.response.content_type = "application/ogg"
        bottle.response.content_length = fake_file_size - range_begin
        if content_range:
            bottle.response.headers["Content-Range"] = content_range

        if bottle.request.method.upper() == "GET":
            return generate()
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
            streamer.set_track(track_id, _duration_sec)
            file_size = streamer.get_track_length()
            range_end = file_size
            log_msg(
                f"Recovered track length from URL (duration={_duration_sec}s), file_size={file_size}.",
                LOGDEBUG,
            )

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
            content_range = f"bytes {range_begin}-{range_end}/{file_size}"
            if not is_new_track and range_begin > 0:
                is_seek = True
            # User selected a different track (e.g. from playlist while one was playing).
            # Kodi may send a stale Range—serve from start. Use a tiny first chunk (256 bytes)
            # so the first response returns immediately (UI snappy); Kodi then requests the
            # rest using its cache.chunksize for everything after.
            if is_new_track and range_begin > 0:
                range_begin = 0
                range_end = min(file_size, 256)
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
        # defer_kill_previous: when starting a new track from byte 0, defer killing the old
        # spotty until after the first chunk is sent (avoids seek-to-start triggering
        # "next track" on the old connection and causing 0-sec skip storms).
        is_seek_to_start = is_new_track and range_begin == 0

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
                    with self.__seek_throttle_lock:
                        now = time.time()
                        elapsed = now - self.__last_seek_terminate_time
                        if (
                            elapsed < SEEK_THROTTLE_SEC
                            and self.__last_seek_terminate_time > 0
                        ):
                            time.sleep(SEEK_THROTTLE_SEC - elapsed)
                        self.__last_seek_terminate_time = time.time()
                    self.__terminate_streaming()
                    log_msg(
                        f"Seek to byte {range_begin}, streaming immediately.", LOGDEBUG
                    )
                elif is_new_track:
                    self.__terminate_streaming()
                    log_msg(
                        "New track: terminated previous stream, streaming new track.",
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
                            rest_len, rest_begin, defer_kill_previous=is_seek_to_start
                        )
                else:
                    yield from streamer.send_part_audio_stream(
                        r_len, r_begin, defer_kill_previous=is_seek_to_start
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
                    "Stream read/write error (likely seek killed stream), not clearing state.",
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
