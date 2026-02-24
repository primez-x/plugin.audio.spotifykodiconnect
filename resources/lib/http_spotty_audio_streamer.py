import threading
import time
from typing import Callable, Optional

import bottle
from spotty import Spotty
from spotty_audio_streamer import SpottyAudioStreamer
from utils import log_msg, LOGDEBUG


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
        # Always avoid terminating the current streamer on overlapping requests.
        # This reduces the risk of early stream termination when Kodi issues
        # multiple HTTP range requests for the same track.
        self.__problem_with_terminate_streaming = True
        self.__prebuffer_manager = prebuffer_manager
        self.__on_track_started = on_track_started_callback or (lambda _id, _dur: None)

        self.__spotty_streamer: SpottyAudioStreamer = SpottyAudioStreamer(
            self.__spotty, initial_volume=_clamp_stream_volume(stream_volume)
        )
        self.__spotty_streamer.use_normalization = use_normalization

        self.__is_streaming = False
        self.__stream_lock = threading.Lock()

    def use_normalization(self, value):
        self.__spotty_streamer.use_normalization = value

    def set_stream_volume(self, value: int) -> None:
        self.__spotty_streamer.set_initial_volume(value)

    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        self.__spotty_streamer.set_notify_track_finished(func)

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
            log_msg(f"Terminated running streamer.", LOGDEBUG)
        else:
            log_msg(f"No running streamer. Nothing to terminate.", LOGDEBUG)

    SPOTTY_AUDIO_TRACK_ROUTE = "/track/<track_id>/<duration>"
    # e.g., track_id = "2eHtBGvfD7PD7SiTl52Vxr", duration = 178.795

    # IMPORTANT: If Kodi is running in non-buffered file mode (e.g., cache/buffermode=3 in
    #   'advancedsettings.xml'), then 'CurlFile::Open' will do multiple HTTP GETs for a stream
    #   and eventually request a partial range. That's why there's the added complication
    #   of the '__is_streaming' flag and 'request ranges' code below. (Not to mention
    #   requiring a multithreaded web server to handle the streaming.)
    def spotty_stream_audio_track(self, track_id: str, duration: str) -> bottle.Response:
        log_msg(f"GET request: {bottle.request}", LOGDEBUG)

        if self.__is_streaming:
            if self.__problem_with_terminate_streaming:
                log_msg("Already streaming. But flag 'problem_with_terminate_streaming' = True,"
                        " so NOT terminating current streamer.")
            else:
                with self.__stream_lock:
                    log_msg("Already streaming. Terminating current streamer.")
                    self.__terminate_streaming()

        self.__is_streaming = True

        if self.__gap_between_tracks:
            # TODO - Can we improve on this? Sometimes, when playing a playlist
            #        with no gap between tracks, Kodi does not shutdown the visualizer
            #        before starting the next track and visualizer. So one visualizer
            #        instance is stopping at the same time as another is starting.
            # Give some time for visualizations to finish.
            log_msg(f"Delay {self.__gap_between_tracks}s before starting track.")
            time.sleep(self.__gap_between_tracks)

        self.__spotty_streamer.set_track(track_id, float(duration))

        log_msg(
            f"Start streaming spotify track '{track_id}',"
            f" track length {self.__spotty_streamer.get_track_length()}."
        )

        try:
            self.__on_track_started(track_id, float(duration))
        except Exception:
            pass

        file_size = self.__spotty_streamer.get_track_length()
        range_begin = 0
        range_end = file_size

        def generate():
            r_begin = range_begin
            r_end = range_end
            r_len = r_end - r_begin
            prebuf, has_prebuf = (
                (self.__prebuffer_manager.get_and_clear_prebuffer(track_id))
                if self.__prebuffer_manager
                else (None, False)
            )
            if has_prebuf and prebuf:
                prebuffer_len = len(prebuf)
                if r_begin < prebuffer_len:
                    end_from_buf = min(r_end, prebuffer_len)
                    yield prebuf[r_begin:end_from_buf]
                if r_end > prebuffer_len:
                    rest_begin = max(r_begin, prebuffer_len)
                    rest_len = r_end - rest_begin
                    for chunk in self.__spotty_streamer.send_part_audio_stream(
                        rest_len, rest_begin
                    ):
                        yield chunk
            else:
                for chunk in self.__spotty_streamer.send_part_audio_stream(
                    r_len, r_begin
                ):
                    yield chunk

        request_range = bottle.request.headers.get("Range", "")
        log_msg(f"Request header range: '{request_range}'.", LOGDEBUG)

        if not request_range or (request_range == "bytes=0-"):
            status = 200
            content_range = ""
            log_msg(f"Full request, content length = {range_end- range_begin}.", LOGDEBUG)
        else:
            status = "206 Partial Content"
            try:
                parts = bottle.request.headers["Range"].strip().split("bytes=", 1)[1].split("-", 1)
                start_s = parts[0].strip() if parts else ""
                end_s = parts[1].strip() if len(parts) > 1 else ""
                if not start_s and end_s.isdigit():
                    # Suffix range: last N bytes (e.g. "bytes=-500")
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
                f"Partial request, range = {content_range}," f" length = {range_end - range_begin}",
                LOGDEBUG,
            )

        bottle.response.status = status
        bottle.response.headers["Accept-Ranges"] = "bytes"
        bottle.response.content_type = "audio/x-wav"
        bottle.response.content_length = range_end - range_begin
        if content_range:
            bottle.response.headers["Content-Range"] = content_range

        if bottle.request.method.upper() == "GET":
            return bottle.Response(generate())

        return bottle.Response()

    spotty_stream_audio_track.route = SPOTTY_AUDIO_TRACK_ROUTE
