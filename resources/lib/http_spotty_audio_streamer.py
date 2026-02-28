"""
HTTP server for Spotify audio streams. Policy: let Kodi drive.
We only provide correct HTTP range semantics (Content-Length, Accept-Ranges,
Content-Range) so Kodi's own cache/buffer settings (e.g. 512MB, 10x read)
take full effect. No addon-side caching, throttling, or Cache-Control.
"""
import threading
import time
from typing import Callable, Optional

import bottle
from spotty import Spotty
from spotty_audio_streamer import SpottyAudioStreamer
from utils import log_msg, LOGDEBUG

# No debounce: serve every range request immediately so Kodi's seek bar and
# Player.Progress update right away. Let Kodi drive; we just fulfill each request.


def _clamp_stream_volume(value) -> int:
    """Clamp stream volume setting to 1-100."""
    try:
        v = int(value)
        return max(1, min(100, v))
    except (TypeError, ValueError):
        return 35


class HTTPSpottyAudioStreamer:
    def __init__(
        self,
        spotty: Spotty,
        gap_between_tracks: int = 0,
        use_normalization: bool = True,
        stream_volume: int = 35,
        prebuffer_manager=None,
        on_track_started_callback: Optional[Callable[[str, float], None]] = None,
    ):
        self.__spotty: Spotty = spotty
        self.__gap_between_tracks: int = gap_between_tracks
        self.__prebuffer_manager = prebuffer_manager
        self.__on_track_started = on_track_started_callback or (lambda _id, _dur: None)

        self.__spotty_streamer: SpottyAudioStreamer = SpottyAudioStreamer(
            self.__spotty, initial_volume=_clamp_stream_volume(stream_volume)
        )
        self.__spotty_streamer.use_normalization = use_normalization

        self.__is_streaming = False
        self.__stream_lock = threading.Lock()
        self.__current_track_id: Optional[str] = None

    def use_normalization(self, value):
        self.__spotty_streamer.use_normalization = value

    def set_stream_volume(self, value: int) -> None:
        self.__spotty_streamer.set_initial_volume(value)

    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        self.__spotty_streamer.set_notify_track_finished(func)

    def set_stream_ended(self) -> None:
        """Mark that the current stream has finished so the next request starts fresh."""
        with self.__stream_lock:
            self.__is_streaming = False
            self.__current_track_id = None

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

    def spotty_stream_audio_track(self, track_id: str, duration: str) -> bottle.Response:
        log_msg(f"GET request: {bottle.request}", LOGDEBUG)

        is_new_track = (
            not self.__is_streaming
            or self.__current_track_id != track_id
        )

        prebuf_data, has_prebuf_data = None, False
        if is_new_track and self.__prebuffer_manager:
            prebuf_data, has_prebuf_data = self.__prebuffer_manager.get_and_clear_prebuffer(track_id)
            if has_prebuf_data:
                log_msg(
                    f"Prebuffer hit for track {track_id} ({len(prebuf_data)} bytes).",
                    LOGDEBUG,
                )

        if is_new_track:
            with self.__stream_lock:
                if self.__is_streaming:
                    self.__terminate_streaming()
                self.__is_streaming = True
                self.__current_track_id = track_id

            if self.__gap_between_tracks:
                log_msg(f"Delay {self.__gap_between_tracks}s before starting track.")
                time.sleep(self.__gap_between_tracks)

            self.__spotty_streamer.set_track(track_id, float(duration))

            log_msg(
                f"Start streaming spotify track '{track_id}',"
                f" track length {self.__spotty_streamer.get_track_length()}."
            )

            def _notify_started():
                try:
                    self.__on_track_started(track_id, float(duration))
                except Exception:
                    pass

            threading.Thread(target=_notify_started, daemon=True).start()

        file_size = self.__spotty_streamer.get_track_length()
        range_begin = 0
        range_end = file_size

        request_range = bottle.request.headers.get("Range", "")
        log_msg(f"Request header range: '{request_range}'.", LOGDEBUG)

        if not request_range or (request_range == "bytes=0-"):
            status = 200
            content_range = ""
            log_msg(f"Full request, content length = {range_end - range_begin}.", LOGDEBUG)
        else:
            status = "206 Partial Content"
            try:
                parts = bottle.request.headers["Range"].strip().split("bytes=", 1)[1].split("-", 1)
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
            log_msg(
                f"Partial request, range = {content_range},"
                f" length = {range_end - range_begin}",
                LOGDEBUG,
            )

        is_seek = not is_new_track and range_begin > 0
        streamer = self.__spotty_streamer

        def generate():
            if is_seek:
                self.__terminate_streaming()
                log_msg(f"Seek to byte {range_begin}, streaming immediately.", LOGDEBUG)

            r_begin = range_begin
            r_end = range_end
            r_len = r_end - r_begin
            if has_prebuf_data and prebuf_data:
                prebuffer_len = len(prebuf_data)
                if r_begin < prebuffer_len:
                    end_from_buf = min(r_end, prebuffer_len)
                    yield prebuf_data[r_begin:end_from_buf]
                if r_end > prebuffer_len:
                    rest_begin = max(r_begin, prebuffer_len)
                    rest_len = r_end - rest_begin
                    for chunk in streamer.send_part_audio_stream(
                        rest_len, rest_begin
                    ):
                        yield chunk
            else:
                for chunk in streamer.send_part_audio_stream(
                    r_len, r_begin
                ):
                    yield chunk

        # Only what Kodi needs: status, size, range support. No Cache-Control so
        # Kodi's cache settings (buffer size, read factor) take full precedence.
        bottle.response.status = status
        bottle.response.headers["Accept-Ranges"] = "bytes"
        bottle.response.content_type = "audio/x-wav"
        bottle.response.content_length = range_end - range_begin
        if content_range:
            bottle.response.headers["Content-Range"] = content_range

        if bottle.request.method.upper() == "GET":
            return generate()

        return ""

    spotty_stream_audio_track.route = SPOTTY_AUDIO_TRACK_ROUTE
