# -*- coding: utf-8 -*-
"""
Pre-buffer the next track in the background so when Kodi requests it,
the first chunk is already available and playback starts without delay.
"""
from __future__ import absolute_import, unicode_literals

import threading
from typing import Optional, Tuple

from spotty import Spotty
from spotty_audio_streamer import SpottyAudioStreamer
from utils import log_msg
from xbmc import LOGDEBUG

# WAV 44.1 kHz, 16-bit, stereo = 176400 bytes/sec (matches spotty_audio_streamer).
WAV_BYTES_PER_SECOND = 44100 * 2 * 2

PREBUFFER_SECONDS_MIN = 5
PREBUFFER_SECONDS_MAX = 30
PREBUFFER_SECONDS_DEFAULT = 15


def _clamp_prebuffer_seconds(seconds: int) -> int:
    """Clamp pre-buffer seconds to allowed range 5–30."""
    try:
        v = int(seconds)
        return max(PREBUFFER_SECONDS_MIN, min(PREBUFFER_SECONDS_MAX, v))
    except (TypeError, ValueError):
        return PREBUFFER_SECONDS_DEFAULT


class PrebufferManager:
    """Fills a buffer for the next track in a background thread."""

    def __init__(
        self,
        spotty: Spotty,
        initial_volume: int = 35,
        use_normalization: bool = True,
        prebuffer_seconds: int = PREBUFFER_SECONDS_DEFAULT,
    ):
        self.__spotty = spotty
        self.__initial_volume = max(1, min(100, initial_volume))
        self.__use_normalization = use_normalization
        self.__prebuffer_seconds = _clamp_prebuffer_seconds(prebuffer_seconds)
        self.__lock = threading.Lock()
        self.__buffer: Optional[bytes] = None
        self.__prebuffer_track_id: Optional[str] = None
        self.__thread: Optional[threading.Thread] = None
        self.__cancel_requested = False
        self.__streamer_ref: Optional[SpottyAudioStreamer] = None

    def _get_prebuffer_bytes(self) -> int:
        """Bytes to pre-buffer from current seconds setting (WAV stream)."""
        return self.__prebuffer_seconds * WAV_BYTES_PER_SECOND

    def start_prebuffer(self, track_id: str, duration_sec: float) -> None:
        """Start filling the pre-buffer for the given track in a background thread."""
        prebuffer_bytes = self._get_prebuffer_bytes()
        with self.__lock:
            if self.__prebuffer_track_id == track_id and self.__buffer is not None:
                return
            self.__cancel_requested = True
            if self.__streamer_ref:
                try:
                    self.__streamer_ref.terminate_stream()
                except Exception:
                    pass
                self.__streamer_ref = None
            self.__buffer = None
            self.__prebuffer_track_id = None
            self.__cancel_requested = False

        def _fill():
            streamer = SpottyAudioStreamer(
                self.__spotty, initial_volume=self.__initial_volume
            )
            streamer.use_normalization = self.__use_normalization
            streamer.set_track(track_id, duration_sec)
            with self.__lock:
                self.__streamer_ref = streamer
            collected = bytearray()
            try:
                for chunk in streamer.send_part_audio_stream(prebuffer_bytes, 0):
                    with self.__lock:
                        if self.__cancel_requested:
                            return
                    if isinstance(chunk, bytes):
                        collected.extend(chunk)
                    elif chunk:
                        collected.extend(chunk.encode("latin-1"))
                    if len(collected) >= prebuffer_bytes:
                        break
                with self.__lock:
                    if not self.__cancel_requested and self.__prebuffer_track_id is None:
                        self.__buffer = bytes(collected[:prebuffer_bytes])
                        self.__prebuffer_track_id = track_id
                        log_msg(
                            "Prebuffer ready for track %s (%d bytes, %d sec)"
                            % (track_id, len(self.__buffer), self.__prebuffer_seconds),
                            LOGDEBUG,
                        )
            except Exception as e:
                log_msg("Prebuffer failed for %s: %s" % (track_id, e), LOGDEBUG)
            finally:
                with self.__lock:
                    self.__streamer_ref = None

        self.__thread = threading.Thread(target=_fill, daemon=True)
        self.__thread.start()

    def get_and_clear_prebuffer(self, track_id: str) -> Tuple[Optional[bytes], bool]:
        """
        If we have a pre-buffer for this track_id, return (buffer_bytes, True) and clear it.
        Otherwise return (None, False).
        """
        with self.__lock:
            if self.__prebuffer_track_id != track_id or self.__buffer is None:
                return None, False
            buf = self.__buffer
            self.__buffer = None
            self.__prebuffer_track_id = None
            return buf, True

    def cancel_prebuffer(self) -> None:
        """Stop any running pre-buffer and clear state."""
        with self.__lock:
            self.__cancel_requested = True
            if self.__streamer_ref:
                try:
                    self.__streamer_ref.terminate_stream()
                except Exception:
                    pass
                self.__streamer_ref = None
            self.__buffer = None
            self.__prebuffer_track_id = None

    def set_volume(self, value: int) -> None:
        self.__initial_volume = max(1, min(100, value))

    def set_use_normalization(self, value: bool) -> None:
        self.__use_normalization = value

    def set_prebuffer_seconds(self, seconds: int) -> None:
        """Set pre-buffer duration in seconds (clamped to 5–30)."""
        self.__prebuffer_seconds = _clamp_prebuffer_seconds(seconds)
