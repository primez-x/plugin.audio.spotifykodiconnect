# -*- coding: utf-8 -*-
"""
Pre-buffer the next track in the background so when Kodi requests it,
the first chunk is already available and playback starts without delay.

Collects raw PCM bytes into a buffer (headerless samples are concatenation-safe).
"""
from __future__ import absolute_import, unicode_literals

import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from spotty import Spotty
from spotty_audio_streamer import SpottyAudioStreamer
from utils import log_msg
from xbmc import LOGDEBUG


@dataclass
class PrebufferResult:
    """Result from get_and_clear_prebuffer(). Contains WAV PCM bytes."""
    data: Optional[bytes] = None

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
        normalization_gain_type: str = "auto",
        prebuffer_seconds: int = PREBUFFER_SECONDS_DEFAULT,
        bitrate: str = "320",
        use_passthrough: bool = False,
    ):
        self.__spotty = spotty
        self.__normalization_gain_type = (normalization_gain_type or "auto").strip().lower()
        self.__prebuffer_seconds = _clamp_prebuffer_seconds(prebuffer_seconds)
        self.__bitrate: str = bitrate
        self.__use_passthrough: bool = use_passthrough
        self.__lock = threading.Lock()
        self.__buffer: Optional[bytes] = None
        self.__prebuffer_track_id: Optional[str] = None
        self.__thread: Optional[threading.Thread] = None
        self.__cancel_requested = False
        self.__streamer_ref: Optional[SpottyAudioStreamer] = None
        # Generation counter: incremented each time start_prebuffer() is called for a
        # new track. Each _fill() closure captures its own generation at launch. Before
        # storing its result, _fill checks that the current generation still matches —
        # if start_prebuffer() was called again in the meantime the old _fill discards
        # its (stale/partial) data so the new _fill can store its own result.
        self.__generation: int = 0

    def _get_prebuffer_bytes(self) -> int:
        """Bytes to pre-buffer from current seconds setting."""
        return self.__prebuffer_seconds * WAV_BYTES_PER_SECOND

    def start_prebuffer(
        self,
        track_id: str,
        duration_sec: float,
        bitrate: Optional[str] = None,
        normalization_gain_type: Optional[str] = None,
        use_passthrough: Optional[bool] = None,
    ) -> None:
        """Start filling the pre-buffer for the given track in a background thread.

        Optional bitrate, normalization_gain_type override instance defaults so the
        prebuffer uses current addon settings (no restart required).
        use_passthrough overrides the instance default for this prebuffer operation.
        """
        br = bitrate if bitrate is not None else self.__bitrate
        norm = (normalization_gain_type or self.__normalization_gain_type).strip().lower() or "auto"
        if norm not in ("off", "auto", "track", "album"):
            norm = "auto"
        use_pt = use_passthrough if use_passthrough is not None else self.__use_passthrough

        prebuffer_bytes = self._get_prebuffer_bytes()
        with self.__lock:
            # Skip if we're already prebuffering or have a buffer for this track.
            # This prevents restarting the prebuffer when Kodi issues multiple
            # buffering requests for the current track during playback.
            if self.__prebuffer_track_id == track_id:
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
            self.__generation += 1
            my_gen = self.__generation

        def _fill():
            streamer = SpottyAudioStreamer(self.__spotty)
            streamer.normalization_gain_type = norm
            streamer.bitrate = br
            streamer.set_track(track_id, duration_sec, is_passthrough=use_pt)
            with self.__lock:
                self.__streamer_ref = streamer

            collected = bytearray()
            try:
                for chunk in streamer.send_part_audio_stream(prebuffer_bytes, 0, is_passthrough=use_pt):
                    with self.__lock:
                        if self.__cancel_requested or self.__generation != my_gen:
                            return

                    if type(chunk) is bytes:
                        collected.extend(chunk)
                    elif chunk:
                        collected.extend(chunk.encode("latin-1"))

                    if len(collected) >= prebuffer_bytes:
                        break

                with self.__lock:
                    if not self.__cancel_requested and self.__generation == my_gen and self.__prebuffer_track_id is None:
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

    def get_and_clear_prebuffer(self, track_id: str) -> Tuple[Optional[PrebufferResult], bool]:
        """Return prebuffer result for *track_id* and clear internal state.

        Returns ``(PrebufferResult, True)`` on hit, ``(None, False)`` on miss.
        The result contains WAV PCM bytes.
        """
        with self.__lock:
            if self.__prebuffer_track_id != track_id:
                return None, False

            if self.__buffer is not None:
                buf = self.__buffer
                self.__buffer = None
                self.__prebuffer_track_id = None
                return PrebufferResult(data=buf), True

            return None, False

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

    def set_normalization_gain_type(self, value: str) -> None:
        self.__normalization_gain_type = (value or "auto").strip().lower()

    def set_prebuffer_seconds(self, seconds: int) -> None:
        """Set pre-buffer duration in seconds (clamped to 5–30)."""
        self.__prebuffer_seconds = _clamp_prebuffer_seconds(seconds)
