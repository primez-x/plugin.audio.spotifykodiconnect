import json
import os
import struct
import subprocess
import threading
import time
from io import BytesIO
from typing import Callable, Tuple

from xbmc import LOGDEBUG, LOGWARNING, LOGERROR

from spotty import Spotty
from utils import bytes_to_megabytes, kill_process_by_pid, log_msg, log_exception

SPOTIFY_TRACK_PREFIX = "spotify:track:"

SPOTIFY_BITRATE = "320"
_VALID_BITRATES = ("96", "160", "320")
_VALID_GAIN_TYPES = ("auto", "track", "album")
_DEFAULT_GAIN_TYPE = "track"

# Maximum bytes of PCM silence to pad at the end of a stream when spotty exits
# cleanly but short of the WAV-declared length. 10 seconds @ 176400 B/s = 1,764,000.
# Duration mismatches between the declared track length and spotty's actual output
# are typically < 10 s; larger gaps indicate a real error and should not be masked.
_SILENCE_PADDING_MAX_BYTES = 176400 * 10


def _clamp_volume(value: int) -> int:
    """Clamp volume to 1-100 for spotty --initial-volume."""
    try:
        v = int(value)
        return max(1, min(100, v))
    except (TypeError, ValueError):
        return 35



def _get_kodi_chunk_size() -> int:
    """Dynamically get the user's chunk size setting from Kodi (cache.chunksize).
    Defaults to 1MB if not found.
    """
    try:
        import xbmc
        raw = xbmc.executeJSONRPC(json.dumps({
            "jsonrpc": "2.0",
            "method": "Settings.GetSettingValue",
            "params": {"setting": "cache.chunksize"},
            "id": 1
        }))
        res = json.loads(raw)
        val = int(res.get("result", {}).get("value", 0))
        if val > 0:
            return val
    except Exception:
        pass
    return 1048576  # Default fallback

class SpottyAudioStreamer:
    """
    Streams PCM audio from the spotty binary (librespot) as WAV, for a single track.
    Uses a background file cache via spotty_cache to handle seeks instantly.
    """

    def __init__(self, spotty: Spotty, initial_volume: int = 35):
        self.__spotty = spotty
        self.initial_volume = _clamp_volume(initial_volume)
        self.chunk_size = _get_kodi_chunk_size()

        self.__track_id: str = ""
        self.__track_duration: int = 0
        self.__wav_header: bytes = bytes()
        self.__track_length: int = 0

        self.__notify_track_finished: Callable[[str], None] = lambda x: None
        self.__terminated = False

        # Streaming settings — updated by the HTTP layer before each track.
        self.normalization_gain_type: str = _DEFAULT_GAIN_TYPE
        self.bitrate: str = SPOTIFY_BITRATE
        self.use_autoplay: bool = False

    def set_initial_volume(self, value: int) -> None:
        """Set volume (1–100) for the next spotty run."""
        self.initial_volume = _clamp_volume(value)

    def get_track_length(self) -> int:
        """Total byte length of the WAV stream (header + PCM) for the current track."""
        return self.__track_length

    def get_track_duration(self) -> int:
        """Track duration in seconds used for the WAV header."""
        return self.__track_duration

    def set_track(self, track_id: str, track_duration: float) -> None:
        """Set the track to stream; builds WAV header for PCM/WAV streaming."""
        self.__track_id = track_id
        try:
            if track_duration <= 0:
                log_msg(f"Warning: Invalid track duration {track_duration} for track {track_id}. Using 1s fallback.", LOGWARNING)
                self.__track_duration = 1
            else:
                self.__track_duration = int(track_duration)
        except (TypeError, ValueError):
            log_msg(f"Warning: Could not parse track duration {track_duration} for track {track_id}. Using 1s fallback.", LOGWARNING)
            self.__track_duration = 1

        # Always create WAV header for PCM streaming.
        self.__wav_header, self.__track_length = create_wav_header_for_duration(self.__track_duration)


    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        """Set callback invoked when the full track has been sent (not on every range chunk)."""
        self.__notify_track_finished = func

    def _log_transfer(self, state: str, **kwargs) -> None:
        parts = [f"track={self.__track_id}", f"state={state}"]
        for k, v in kwargs.items():
            parts.append(f"{k}={v}")
        log_msg(" | ".join(parts), LOGDEBUG)

    def terminate_stream(self) -> bool:
        """Signal the current generator to stop."""
        self.__terminated = True
        return True

    def send_part_audio_stream(
        self,
        range_len: int,
        range_begin: int,
        defer_kill_previous: bool = False,
        start_sec: int = 0,
    ):
        """Generator: yield WAV (PCM) bytes from the background downloader cache file."""
        from spotty_cache import SpottyCacheManager

        self.__terminated = False
        bytes_sent = 0
        
        # Check if we have an active background downloader for this track that covers our request
        downloader = SpottyCacheManager.find_best_downloader(self.__track_id, range_begin)
        
        # If no suitable downloader, or the downloader is too far behind (e.g. > 2MB behind),
        # start a new downloader at the requested position.
        # Since librespot downloads fast, we only jump if the user seeks far ahead of current progress.
        if not downloader:
            downloader = SpottyCacheManager.get_or_start(
                self.__spotty, self.__track_id, self.__track_duration, range_begin,
                self.bitrate, self.normalization_gain_type, self.initial_volume,
                self.__wav_header, self.__track_length
            )
        elif range_begin > downloader.start_byte + downloader.written_bytes + 2097152 and not downloader.is_finished:
            downloader = SpottyCacheManager.get_or_start(
                self.__spotty, self.__track_id, self.__track_duration, range_begin,
                self.bitrate, self.normalization_gain_type, self.initial_volume,
                self.__wav_header, self.__track_length
            )

        self._log_transfer("start", range_begin=range_begin)

        try:
            with open(downloader.file_path, "rb") as f:
                f.seek(range_begin - downloader.start_byte)

                while bytes_sent < range_len and not self.__terminated:
                    target_bytes_in_file = (range_begin - downloader.start_byte) + bytes_sent + 1
                    downloader.wait_for_bytes(target_bytes_in_file, timeout=1.0)

                    if self.__terminated:
                        break

                    with downloader.cond:
                        available = downloader.written_bytes - ((range_begin - downloader.start_byte) + bytes_sent)
                        if downloader.error and available <= 0:
                            self._log_transfer("error", msg="Background downloader hit an error")
                            break
                        is_finished = downloader.is_finished

                    if available > 0:
                        to_read = min(self.chunk_size, available, range_len - bytes_sent)
                        chunk = f.read(to_read)
                        if chunk:
                            yield chunk
                            bytes_sent += len(chunk)
                            if bytes_sent % 10485760 < self.chunk_size:
                                self._log_transfer("progress", bytes_sent=bytes_sent)
                    elif is_finished:
                        break

            end_of_range = range_begin + bytes_sent
            if self.__track_length > 0 and end_of_range >= self.__track_length:
                self.__notify_track_finished(self.__track_id)
            self._log_transfer("finished", range_begin=range_begin, bytes_sent=bytes_sent)

        except Exception as ex:
            self._log_transfer("exception", range_begin=range_begin, bytes_sent=bytes_sent, ex=ex)
            log_exception(ex, "send_part_audio_stream")
        finally:
            self.__terminated = False


def create_wav_header_for_duration(duration_sec: float) -> Tuple[bytes, int]:
    """Create a WAV header and total stream length for a given duration (no side effects)."""
    try:
        file = BytesIO()
        num_samples = 44100 * int(max(1, duration_sec))
        channels = 2
        sample_rate = 44100
        bits_per_sample = 16

        # Generate format chunk.
        format_chunk_spec = "<4sLHHLLHH"
        format_chunk = struct.pack(
            format_chunk_spec,
            b"fmt ",
            16,
            1,
            channels,
            sample_rate,
            sample_rate * channels * (bits_per_sample // 8),
            channels * (bits_per_sample // 8),
            bits_per_sample,
        )

        # Generate data chunk.
        data_chunk_spec = "<4sL"
        data_size = int(num_samples * channels * (bits_per_sample // 8))
        data_chunk = struct.pack(data_chunk_spec, b"data", data_size)

        # Standard WAV: RIFF size = 36 + data_size
        riff_size = 36 + data_size
        main_header_spec = "<4sL4s"
        main_header = struct.pack(main_header_spec, b"RIFF", riff_size, b"WAVE")

        file.write(main_header)
        file.write(format_chunk)
        file.write(data_chunk)

        header_bytes = file.getvalue()
        header_len = len(header_bytes)
        total_length = header_len + data_size
        return header_bytes, total_length
    except Exception as exc:
        log_exception(exc, "Failed to create wave header (static).")
        raise

