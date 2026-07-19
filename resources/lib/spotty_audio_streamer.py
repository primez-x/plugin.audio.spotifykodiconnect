import json
import os
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from typing import Callable, Optional, Tuple

from xbmc import LOGDEBUG, LOGWARNING, LOGERROR

from spotty import Spotty
from utils import bytes_to_megabytes, kill_process_by_pid, log_msg, log_exception

SPOTIFY_TRACK_PREFIX = "spotify:track:"

SPOTIFY_BITRATE = "320"
_VALID_BITRATES = ("96", "160", "320")
_VALID_GAIN_TYPES = ("auto", "track", "album")
_DEFAULT_GAIN_TYPE = "track"

_PCM_BYTES_PER_SEC = 176400  # 44.1 kHz * 2 ch * 2 bytes/sample
_WAV_HEADER_SIZE = 44
STARTUP_SILENCE_BYTES = _PCM_BYTES_PER_SEC * 2
_STARTUP_REAL_PCM_PREROLL_BYTES = 2097152
_THROTTLE_LEAD_BYTES = 2097152
STARTUP_REAL_PCM_WAIT_SECONDS = 15.0

# Maximum bytes of PCM silence to pad at the end of a stream when spotty exits
# cleanly but short of the WAV-declared length. 10 seconds @ 176400 B/s = 1,764,000.
# Duration mismatches between the declared track length and spotty's actual output
# are typically < 10 s; larger gaps indicate a real error and should not be masked.
_SILENCE_PADDING_MAX_BYTES = _PCM_BYTES_PER_SEC * 10


@dataclass(frozen=True)
class SpottyStreamSpec:
    """Immutable track/settings snapshot used by one HTTP response."""

    track_id: str
    track_duration: int
    wav_header: bytes
    track_length: int
    bitrate: str
    normalization_gain_type: str
    initial_volume: int


def _clamp_volume(value: int) -> int:
    """Clamp volume to 1-100 for spotty --initial-volume."""
    try:
        v = int(value)
        return max(1, min(100, v))
    except (TypeError, ValueError):
        return 35


def _get_kodi_chunk_size() -> int:
    """Read Kodi's file-cache chunk size, with the legacy setting as fallback."""
    try:
        import xbmc
    except Exception:
        return 1048576

    for setting_id in ("filecache.chunksize", "cache.chunksize"):
        try:
            raw = xbmc.executeJSONRPC(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "Settings.GetSettingValue",
                        "params": {"setting": setting_id},
                        "id": 1,
                    }
                )
            )
            res = json.loads(raw)
            val = int(res.get("result", {}).get("value", 0))
            if val > 0:
                return val
        except Exception:
            continue
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
        self.__terminate_generation = 0

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

    def get_stream_spec(self) -> SpottyStreamSpec:
        """Capture the current track and downloader settings immutably."""
        return SpottyStreamSpec(
            track_id=self.__track_id,
            track_duration=self.__track_duration,
            wav_header=bytes(self.__wav_header),
            track_length=self.__track_length,
            bitrate=self.bitrate,
            normalization_gain_type=self.normalization_gain_type,
            initial_volume=self.initial_volume,
        )

    def restore_stream_spec(self, stream_spec: SpottyStreamSpec) -> None:
        """Restore a previously captured track/settings snapshot."""
        self.__track_id = stream_spec.track_id
        self.__track_duration = stream_spec.track_duration
        self.__wav_header = bytes(stream_spec.wav_header)
        self.__track_length = stream_spec.track_length
        self.bitrate = stream_spec.bitrate
        self.normalization_gain_type = stream_spec.normalization_gain_type
        self.initial_volume = stream_spec.initial_volume

    def set_track(self, track_id: str, track_duration: float) -> None:
        """Set the track to stream; builds WAV header for PCM/WAV streaming."""
        self.__track_id = track_id
        try:
            if track_duration <= 0:
                log_msg(
                    f"Warning: Invalid track duration {track_duration} for track {track_id}. Using 1s fallback.",
                    LOGWARNING,
                )
                self.__track_duration = 1
            else:
                self.__track_duration = int(track_duration)
        except (TypeError, ValueError):
            log_msg(
                f"Warning: Could not parse track duration {track_duration} for track {track_id}. Using 1s fallback.",
                LOGWARNING,
            )
            self.__track_duration = 1

        # Include a short silent PCM preroll in the declared length so PAPlayer
        # can initialize before Spotty has produced real PCM on cold starts.
        self.__wav_header, self.__track_length = create_wav_header_for_duration(
            self.__track_duration
        )

    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        """Set callback invoked when the full track has been sent (not on every range chunk)."""
        self.__notify_track_finished = func

    def _log_transfer(self, state: str, stream_track_id: Optional[str] = None, **kwargs) -> None:
        parts = [f"track={stream_track_id or self.__track_id}", f"state={state}"]
        for k, v in kwargs.items():
            parts.append(f"{k}={v}")
        log_msg(" | ".join(parts), LOGDEBUG)

    def _prime_startup_real_pcm(
        self,
        downloader,
        range_begin: int,
        wav_header: bytes,
        track_length: int,
        stream_generation: int,
        track_id: str,
        allow_clean_short: bool = False,
    ) -> bool:
        prefix_end = len(wav_header) + STARTUP_SILENCE_BYTES
        downloader_real_start = max(prefix_end, int(downloader.start_byte))
        requested_real_start = max(prefix_end, int(range_begin))
        requested_real_end = min(
            track_length,
            requested_real_start + _STARTUP_REAL_PCM_PREROLL_BYTES,
        )
        # real_pcm_bytes is relative to the downloader's own real-PCM start.
        # Include any bytes before the requested range so readiness proves that
        # the requested position (plus a bounded preroll) is actually buffered.
        target_real_pcm_bytes = max(0, requested_real_end - downloader_real_start)
        if target_real_pcm_bytes <= 0:
            return True

        synthetic_bytes_in_buffer = max(0, prefix_end - downloader.start_byte)
        target_bytes_in_buf = synthetic_bytes_in_buffer + target_real_pcm_bytes

        def _real_pcm_bytes() -> int:
            explicit = getattr(downloader, "real_pcm_bytes", None)
            if explicit is not None:
                return max(0, int(explicit or 0))
            # Compatibility for tests/older downloader doubles.
            return max(0, int(downloader.written_bytes) - synthetic_bytes_in_buffer)

        with downloader.cond:
            if (
                _real_pcm_bytes() >= target_real_pcm_bytes
                and not downloader.error
                and not downloader.aborted
            ):
                return True
            written = downloader.written_bytes

        self._log_transfer(
            "startup-prime-wait",
            stream_track_id=track_id,
            target=target_bytes_in_buf,
            written=written,
        )
        deadline = time.monotonic() + STARTUP_REAL_PCM_WAIT_SECONDS
        while not self._is_terminated(stream_generation):
            with downloader.cond:
                ready = _real_pcm_bytes() >= target_real_pcm_bytes
                terminal = downloader.is_finished or downloader.error or downloader.aborted
                if ready or terminal:
                    break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_for_real_pcm = getattr(downloader, "wait_for_real_pcm", None)
            if callable(wait_for_real_pcm):
                wait_for_real_pcm(target_real_pcm_bytes, timeout=min(0.25, remaining))
            else:
                downloader.wait_for_bytes(target_bytes_in_buf, timeout=min(0.25, remaining))

        with downloader.cond:
            written = downloader.written_bytes
            real_pcm_bytes = _real_pcm_bytes()
            finished = downloader.is_finished
            error = downloader.error
            aborted = downloader.aborted
        ready = (
            real_pcm_bytes >= target_real_pcm_bytes
            and not error
            and not aborted
            and not self._is_terminated(stream_generation)
        )
        if (
            allow_clean_short
            and finished
            and real_pcm_bytes > 0
            and not error
            and not aborted
            and not self._is_terminated(stream_generation)
        ):
            ready = True
        self._log_transfer(
            "startup-prime-ready",
            stream_track_id=track_id,
            target=target_bytes_in_buf,
            written=written,
            real_pcm_target=target_real_pcm_bytes,
            real_pcm_written=real_pcm_bytes,
            ready=ready,
            finished=finished,
            error=error,
            aborted=aborted,
        )
        return ready

    def terminate_stream(self) -> bool:
        """Signal the current generator to stop."""
        self.__terminate_generation += 1
        return True

    def _is_terminated(self, stream_generation: int) -> bool:
        return stream_generation != self.__terminate_generation

    def _get_or_start_downloader(
        self,
        stream_spec: SpottyStreamSpec,
        range_begin: int,
    ):
        from spotty_cache import SpottyCacheManager

        track_id = stream_spec.track_id
        downloader = SpottyCacheManager.find_best_downloader(track_id, range_begin)

        if not downloader:
            return SpottyCacheManager.get_or_start(
                self.__spotty,
                track_id,
                stream_spec.track_duration,
                range_begin,
                stream_spec.bitrate,
                stream_spec.normalization_gain_type,
                stream_spec.initial_volume,
                stream_spec.wav_header,
                stream_spec.track_length,
                STARTUP_SILENCE_BYTES,
            )

        if (
            range_begin > downloader.start_byte + downloader.written_bytes + 2097152
            and not downloader.is_finished
        ):
            return SpottyCacheManager.get_or_start(
                self.__spotty,
                track_id,
                stream_spec.track_duration,
                range_begin,
                stream_spec.bitrate,
                stream_spec.normalization_gain_type,
                stream_spec.initial_volume,
                stream_spec.wav_header,
                stream_spec.track_length,
                STARTUP_SILENCE_BYTES,
            )

        return downloader

    def _startup_failed_before_real_pcm(
        self, downloader, range_begin: int, wav_header: bytes
    ) -> bool:
        prefix_end = len(wav_header) + STARTUP_SILENCE_BYTES
        explicit_has_real_pcm = None
        if not hasattr(downloader, "real_pcm_bytes"):
            has_real_pcm = getattr(downloader, "has_real_pcm", None)
            explicit_has_real_pcm = has_real_pcm() if callable(has_real_pcm) else None
        with downloader.cond:
            real_pcm_bytes = getattr(downloader, "real_pcm_bytes", None)
            if real_pcm_bytes is not None:
                no_real_pcm = int(real_pcm_bytes or 0) <= 0
            elif explicit_has_real_pcm is not None:
                no_real_pcm = not explicit_has_real_pcm
            else:
                written = int(getattr(downloader, "written_bytes", 0) or 0)
                start_byte = int(getattr(downloader, "start_byte", 0) or 0)
                no_real_pcm = written <= max(0, prefix_end - start_byte)
            failed = (
                bool(getattr(downloader, "error", False))
                or bool(getattr(downloader, "aborted", False))
                or bool(getattr(downloader, "is_finished", False))
            )

        return failed and no_real_pcm

    def _cleanup_failed_startup_downloader(self, downloader) -> None:
        try:
            downloader.cleanup()
        except Exception as ex:
            log_exception(ex, "cleanup_failed_startup_downloader")

    def prepare_part_audio_stream(
        self,
        range_begin: int,
        stream_spec: Optional[SpottyStreamSpec] = None,
    ) -> bool:
        """Start downloader and verify startup does not fail before real PCM."""
        stream_spec = stream_spec or self.get_stream_spec()
        track_id = stream_spec.track_id
        track_length = stream_spec.track_length
        wav_header = stream_spec.wav_header
        stream_generation = self.__terminate_generation
        downloader = self._get_or_start_downloader(stream_spec, range_begin)
        if downloader is None:
            return False

        ready = self._prime_startup_real_pcm(
            downloader,
            range_begin,
            wav_header,
            track_length,
            stream_generation,
            track_id,
        )
        if not ready or self._startup_failed_before_real_pcm(downloader, range_begin, wav_header):
            self._log_transfer(
                "error",
                stream_track_id=track_id,
                msg="Background downloader did not reach real PCM startup target",
            )
            self._cleanup_failed_startup_downloader(downloader)
            return False
        return True

    def send_part_audio_stream(
        self,
        range_len: int,
        range_begin: int,
        defer_kill_previous: bool = False,
        start_sec: int = 0,
        stream_spec: Optional[SpottyStreamSpec] = None,
    ):
        """Return a generator permanently bound to one track specification."""
        # This wrapper intentionally contains no yield. Capturing here, rather
        # than on first iteration, keeps later set_track() calls from retargeting
        # an older WSGI response whose body has not been iterated yet.
        captured_spec = stream_spec or self.get_stream_spec()
        captured_generation = self.__terminate_generation
        return self._send_part_audio_stream(
            range_len,
            range_begin,
            defer_kill_previous,
            start_sec,
            captured_spec,
            captured_generation,
        )

    def _send_part_audio_stream(
        self,
        range_len: int,
        range_begin: int,
        defer_kill_previous: bool = False,
        start_sec: int = 0,
        stream_spec: Optional[SpottyStreamSpec] = None,
        stream_generation: Optional[int] = None,
    ):
        """Generator: yield WAV (PCM) bytes from the background downloader's in-memory buffer."""
        # Capture track-specific state at entry — set_track() may be called concurrently
        # when Kodi pre-loads the next track via QueueNextFileEx while this generator is
        # still draining the current one.  Local copies insulate this generator from those
        # updates so we always stream the correct track from the cache.
        stream_spec = stream_spec or self.get_stream_spec()
        track_id = stream_spec.track_id
        track_length = stream_spec.track_length
        wav_header = stream_spec.wav_header

        if stream_generation is None:
            stream_generation = self.__terminate_generation
        if self._is_terminated(stream_generation):
            return
        bytes_sent = 0

        downloader = self._get_or_start_downloader(stream_spec, range_begin)
        if downloader is None:
            return

        # Rate-throttle: keep the HTTP connection alive for the full track duration so
        # Kodi's QueueNextFileEx fires while our connection is still active.
        # Without throttling, tracks opened via QueueNextFileEx are pre-buffered by Kodi
        # at full speed (40+ MB in ~6 seconds), closing the connection minutes before the
        # *next* QueueNextFileEx fires — causing "Unhandled exception" and cascading skips.
        # Allow a 2 MB initial burst so Kodi's decode buffer fills instantly, then pace
        # at 176400 B/s (44.1 kHz × 2 ch × 2 bytes).  From-start and early
        # follow-up range readers are throttled; true mid-song seeks respond
        # immediately.
        _PCM_BYTES_PER_SEC = 176400  # 44.1 kHz × 2 ch × 2 bytes/sample
        # Throttle for from-start requests (range_begin == 0) AND for WAV-header
        # restarts (range_begin == 44, i.e. "prev" skips the 44-byte header).
        # Kodi may also close the header GET and continue with an early range
        # reader. Treat those as the same active stream, not as seeks with a new
        # burst budget. Mid-song seeks beyond the lead window still respond
        # immediately.
        _WAV_HEADER_SIZE = 44
        stream_start_time = time.monotonic() if range_begin <= _THROTTLE_LEAD_BYTES else None

        self._log_transfer("start", stream_track_id=track_id, range_begin=range_begin)

        buf_offset = range_begin - downloader.start_byte
        consumer_id = None

        try:
            with downloader.cond:
                consumer_id = downloader._next_consumer_id
                downloader._next_consumer_id += 1
                downloader._consumer_positions[consumer_id] = max(0, buf_offset)

            ready = self._prime_startup_real_pcm(
                downloader,
                range_begin,
                wav_header,
                track_length,
                stream_generation,
                track_id,
                True,
            )
            if not ready or self._startup_failed_before_real_pcm(
                downloader, range_begin, wav_header
            ):
                self._log_transfer(
                    "error",
                    stream_track_id=track_id,
                    msg="Background downloader did not reach real PCM startup target",
                )
                self._cleanup_failed_startup_downloader(downloader)
                return

            while bytes_sent < range_len and not self._is_terminated(stream_generation):
                target_bytes_in_buf = buf_offset + bytes_sent + 1
                downloader.wait_for_bytes(target_bytes_in_buf, timeout=1.0)

                if self._is_terminated(stream_generation):
                    break

                chunk = None
                with downloader.cond:
                    available = downloader.written_bytes - buf_offset - bytes_sent
                    if downloader.error and available <= 0:
                        self._log_transfer(
                            "error",
                            stream_track_id=track_id,
                            msg="Background downloader hit an error",
                        )
                        break
                    is_finished = downloader.is_finished
                    if available > 0:
                        to_read = min(self.chunk_size, available, range_len - bytes_sent)
                        read_start = buf_offset + bytes_sent
                        buf_idx = read_start - downloader._trim_offset
                        if buf_idx < 0:
                            self._log_transfer(
                                "trimmed",
                                stream_track_id=track_id,
                                msg="Requested byte trimmed from buffer head",
                                read_start=read_start,
                                trim_offset=downloader._trim_offset,
                            )
                            break
                        chunk = bytes(downloader._buffer[buf_idx : buf_idx + to_read])
                        downloader._consumed_pos = max(
                            downloader._consumed_pos, read_start + to_read
                        )
                        if consumer_id in downloader._consumer_positions:
                            downloader._consumer_positions[consumer_id] = max(
                                downloader._consumer_positions[consumer_id],
                                read_start + to_read,
                            )
                        downloader._trim_head_locked()

                if chunk:
                    yield chunk
                    bytes_sent += len(chunk)
                    if bytes_sent % 10485760 < self.chunk_size:
                        self._log_transfer(
                            "progress",
                            stream_track_id=track_id,
                            bytes_sent=bytes_sent,
                        )
                    # Throttle to real-time rate after the initial burst window.
                    # Sleep in 100 ms increments so terminate signals are honoured quickly.
                    if stream_start_time is not None:
                        elapsed = time.monotonic() - stream_start_time
                        budget = elapsed * _PCM_BYTES_PER_SEC + _THROTTLE_LEAD_BYTES
                        stream_position = range_begin + bytes_sent
                        if stream_position > budget:
                            sleep_needed = (stream_position - budget) / _PCM_BYTES_PER_SEC
                            sleep_end = time.monotonic() + sleep_needed
                            while time.monotonic() < sleep_end and not self._is_terminated(
                                stream_generation
                            ):
                                time.sleep(0.1)
                elif is_finished:
                    break

            # Pad only small tail mismatches. Large gaps mean spotty delivered an
            # incomplete track; padding those turns a failed stream into minutes
            # of fake silence while Kodi's OSD keeps advancing.
            remaining = range_len - bytes_sent
            if remaining > 0 and not self._is_terminated(stream_generation):
                with downloader.cond:
                    dl_finished = downloader.is_finished
                    dl_error = downloader.error
                    dl_aborted = downloader.aborted
                if dl_finished and not dl_error and not dl_aborted:
                    if remaining <= _SILENCE_PADDING_MAX_BYTES:
                        log_msg(
                            f"Padding {remaining} bytes of silence for {track_id}"
                            f" (downloader finished short of declared length,"
                            f" sent={bytes_sent}, expected={range_len})",
                            LOGWARNING,
                        )
                        silence_chunk = bytes(min(self.chunk_size, 1048576))
                        while remaining > 0 and not self._is_terminated(stream_generation):
                            to_yield = min(len(silence_chunk), remaining)
                            yield silence_chunk[:to_yield]
                            bytes_sent += to_yield
                            remaining -= to_yield
                    else:
                        log_msg(
                            f"Not padding {remaining} bytes of silence for {track_id}"
                            f" (downloader finished far short of declared length,"
                            f" sent={bytes_sent}, expected={range_len})",
                            LOGWARNING,
                        )

            end_of_range = range_begin + bytes_sent
            if (
                track_length > 0
                and range_begin <= _WAV_HEADER_SIZE
                and end_of_range >= track_length
            ):
                self.__notify_track_finished(track_id)
            self._log_transfer(
                "finished",
                stream_track_id=track_id,
                range_begin=range_begin,
                bytes_sent=bytes_sent,
            )

        except Exception as ex:
            self._log_transfer(
                "exception",
                stream_track_id=track_id,
                range_begin=range_begin,
                bytes_sent=bytes_sent,
                ex=ex,
            )
            log_exception(ex, "send_part_audio_stream")
        finally:
            if consumer_id is not None:
                with downloader.cond:
                    downloader._consumer_positions.pop(consumer_id, None)
                    downloader._trim_head_locked()


def create_wav_header_for_duration(
    duration_sec: float, startup_silence_bytes: int = STARTUP_SILENCE_BYTES
) -> Tuple[bytes, int]:
    """Create a WAV header and total stream length for a given duration (no side effects)."""
    try:
        file = BytesIO()
        num_samples = int(44100 * max(1.0, float(duration_sec)))
        channels = 2
        sample_rate = 44100
        bits_per_sample = 16
        block_align = channels * (bits_per_sample // 8)
        preroll_size = max(0, int(startup_silence_bytes))
        preroll_size -= preroll_size % block_align

        # Generate format chunk.
        format_chunk_spec = "<4sLHHLLHH"
        format_chunk = struct.pack(
            format_chunk_spec,
            b"fmt ",
            16,
            1,
            channels,
            sample_rate,
            sample_rate * block_align,
            block_align,
            bits_per_sample,
        )

        # Generate data chunk.
        data_chunk_spec = "<4sL"
        data_size = int(num_samples * block_align) + preroll_size
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
