# -*- coding: utf-8 -*-
"""
Pre-buffer the next track in the background so when Kodi requests it,
the first chunk is already available and playback starts without delay.

With the local disk cache implementation, prebuffering simply means triggering
the background Spotty downloader for the next track ahead of time.
"""

from __future__ import absolute_import, unicode_literals

import threading
from dataclasses import dataclass
from typing import Optional, Tuple

from spotty import Spotty
from spotty_cache import SpottyCacheManager
from spotty_audio_streamer import STARTUP_SILENCE_BYTES, create_wav_header_for_duration
from utils import log_msg
from xbmc import LOGDEBUG


@dataclass
class PrebufferResult:
    """Result from get_and_clear_prebuffer(). Contains WAV PCM bytes."""

    data: Optional[bytes] = None


class PrebufferManager:
    """Triggers a background download for the next track."""

    def __init__(
        self,
        spotty: Spotty,
        normalization_gain_type: str = "auto",
        bitrate: str = "320",
    ):
        self.__spotty = spotty
        self.__normalization_gain_type = (normalization_gain_type or "auto").strip().lower()
        self.__bitrate: str = bitrate
        self.__lock = threading.Lock()
        self.__prebuffer_track_id: Optional[str] = None
        self.__prebuffer_downloader = None

    def start_prebuffer(
        self,
        track_id: str,
        duration_sec: float,
        bitrate: Optional[str] = None,
        normalization_gain_type: Optional[str] = None,
    ):
        """Start filling the pre-buffer for the given track in a background thread."""
        br = bitrate if bitrate is not None else self.__bitrate
        norm = (normalization_gain_type or self.__normalization_gain_type).strip().lower() or "auto"
        if norm not in ("off", "auto", "track", "album"):
            norm = "auto"

        with self.__lock:
            if self.__prebuffer_track_id == track_id:
                return None
            self.__prebuffer_track_id = track_id
            self.__prebuffer_downloader = None

        log_msg(f"Triggering prebuffer background download for {track_id}", LOGDEBUG)
        wav_header, track_length = create_wav_header_for_duration(duration_sec)

        downloader = SpottyCacheManager.get_or_start(
            self.__spotty,
            track_id,
            duration_sec,
            0,
            br,
            norm,
            35,
            wav_header,
            track_length,
            STARTUP_SILENCE_BYTES,
            allow_abort_others=False,
        )
        if downloader is None:
            with self.__lock:
                if self.__prebuffer_track_id == track_id:
                    self.__prebuffer_track_id = None
                    self.__prebuffer_downloader = None
            log_msg(f"Prebuffer for {track_id} deferred; active stream still owns cache", LOGDEBUG)
        else:
            with self.__lock:
                if self.__prebuffer_track_id == track_id:
                    self.__prebuffer_downloader = downloader
        return downloader

    @staticmethod
    def _has_real_pcm(downloader) -> bool:
        if downloader is None:
            return False
        cond = getattr(downloader, "cond", None)
        if cond is None:
            return False
        with cond:
            if getattr(downloader, "error", False) or getattr(downloader, "aborted", False):
                return False
            wav_header = getattr(downloader, "wav_header", b"") or b""
            startup_silence = max(0, int(getattr(downloader, "startup_silence_bytes", 0) or 0))
            real_pcm_start = len(wav_header) + startup_silence
            return int(getattr(downloader, "written_bytes", 0) or 0) > real_pcm_start

    def get_and_clear_prebuffer(self, track_id: str) -> Tuple[Optional[PrebufferResult], bool]:
        """Return prebuffer result for *track_id* and clear internal state.

        With the disk cache, we just return empty bytes to signify a 'hit'
        since the Streamer will read directly from the disk cache anyway.
        """
        with self.__lock:
            if self.__prebuffer_track_id != track_id:
                return None, False

            downloader = self.__prebuffer_downloader
            self.__prebuffer_track_id = None
            self.__prebuffer_downloader = None

        if not self._has_real_pcm(downloader):
            try:
                if downloader is not None:
                    downloader.cleanup()
            except Exception:
                pass
            log_msg(f"Discarding empty or failed prebuffer for {track_id}", LOGDEBUG)
            return None, False

        return PrebufferResult(data=b""), True

    def cancel_prebuffer(self) -> None:
        """Stop any running pre-buffer and clear state. The SpottyCacheManager handles its own cleanup."""
        with self.__lock:
            self.__prebuffer_track_id = None
            self.__prebuffer_downloader = None

    def set_normalization_gain_type(self, value: str) -> None:
        self.__normalization_gain_type = (value or "auto").strip().lower()
