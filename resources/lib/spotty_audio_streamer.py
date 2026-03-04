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
    Used by the HTTP layer to serve range requests; supports seek via --start-position.
    """

    def __init__(self, spotty: Spotty, initial_volume: int = 35):
        self.__spotty = spotty
        self.initial_volume = _clamp_volume(initial_volume)
        self.chunk_size = _get_kodi_chunk_size()

        # Cache process properties to avoid dynamic lookups during tight loops
        self._is_windows = (os.name == "nt")
        if self._is_windows:
            self._startupinfo = subprocess.STARTUPINFO()
            self._startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        else:
            self._startupinfo = None

        self.__track_id: str = ""
        self.__track_duration: int = 0
        self.__wav_header: bytes = bytes()
        self.__track_length: int = 0

        self.__notify_track_finished: Callable[[str], None] = lambda x: None
        self.__current_spotty_pid = -1  # Currently active process
        self.__processes_to_cleanup = []  # List of (pid, process) tuples to clean up
        self.__cleanup_lock = threading.Lock()
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
        self.__wav_header, self.__track_length = self.__create_wav_header()


    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        """Set callback invoked when the full track has been sent (not on every range chunk)."""
        self.__notify_track_finished = func

    def terminate_stream(self) -> bool:
        """Signal the current stream to stop and kill the spotty process. Returns True if a process was killed."""
        self.__terminated = True
        if self.__current_spotty_pid == -1:
            return False
        self.__kill_current_spotty()
        return True

    def send_part_audio_stream(
        self,
        range_len: int,
        range_begin: int,
        defer_kill_previous: bool = False,
        start_sec: int = 0,
    ):
        """Generator: stream WAV (PCM) bytes for the given range from the spotty binary."""
        self.__terminated = False
        spotty_process = None
        bytes_sent = 0
        old_pid_to_kill = -1
        try:
            # WAV: calculate start position from range_begin
            seek_start_sec = 0
            actual_range_begin = range_begin

            # Properly handle old process cleanup
            old_pid_to_kill = self.__prepare_stream(defer_kill_previous, actual_range_begin)
            self._log_transfer("start", range_begin=actual_range_begin, extra_msg=f"start_sec={seek_start_sec}")

            # WAV: handle header offset and PCM skipping
            header_len = len(self.__wav_header)
            pcm_target_offset = max(0, actual_range_begin - header_len)
            pcm_bytes_per_sec = 176400  # 44100 Hz * 2 channels * 2 bytes
            start_sec_wav = (pcm_target_offset // pcm_bytes_per_sec) if pcm_target_offset > 0 else 0
            pcm_skip = (pcm_target_offset % pcm_bytes_per_sec) if pcm_target_offset > 0 else 0

            # Initial chunk: full header, or partial header if range starts inside it.
            chunk, bytes_sent = self._yield_initial_chunk(actual_range_begin, range_len)
            if chunk:
                yield chunk
            if self.__terminated or bytes_sent >= range_len:
                return

            if not self.__track_id:
                self._log_transfer("error", msg="No track ID provided. Aborting stream.")
                return

            track_id_uri = SPOTIFY_TRACK_PREFIX + self.__track_id
            # Build spotty args: use calculated WAV start second
            args = self._build_spotty_args(track_id_uri, start_sec_wav)
            if self.__terminated:
                return

            spotty_process = self.__spotty.run_spotty(args)
            self._log_spotty_process_start(spotty_process)
            self.__current_spotty_pid = spotty_process.pid
            proc_stdout = spotty_process.stdout
            c_size = self.chunk_size

            # Handle the case where spotty process immediately exits
            if spotty_process.poll() is not None:
                # Attempt to capture stderr for diagnostics
                try:
                    out, err = spotty_process.communicate(timeout=0.1)
                    extra = f" stdout_len={len(out) if out else 0} stderr_len={len(err) if err else 0}"
                except Exception:
                    extra = ""
                self._log_transfer("error", msg=f"Spotty process exited immediately with code {spotty_process.returncode}.{extra}")
                return

            # Skip initial PCM bytes if needed (for seek)
            if pcm_skip > 0:
                self._discard_pcm_bytes(proc_stdout, pcm_skip, c_size)
            if self.__terminated:
                return

            frame = proc_stdout.read(c_size)

            # Handle case where no data is read: give spotty a moment to start producing data
            if not frame:
                for _ in range(20):  # a bit more tolerant on slow starts
                    if self.__terminated:
                        return
                    if spotty_process.poll() is not None:
                        remaining = range_len - bytes_sent
                        if spotty_process.returncode == 0 and 0 < remaining <= _SILENCE_PADDING_MAX_BYTES:
                            # Spotty exited cleanly but short of the WAV-declared length
                            # (common when seeking near the end of a track). Pad with silence
                            # so Kodi sees a complete stream and doesn't retry-loop forever.
                            log_msg(
                                f"Track '{self.__track_id}': padding {remaining} bytes of silence "
                                f"(spotty exited during startup at byte {range_begin}, {remaining} bytes short).",
                                LOGDEBUG,
                            )
                            yield bytes(remaining)
                            bytes_sent += remaining
                        else:
                            self._log_transfer("error", msg="Spotty process exited during startup")
                        return
                    time.sleep(0.1)
                    frame = proc_stdout.read(c_size)
                    if frame:
                        break
                else:
                    # Still no frame after retries
                    self._log_transfer("error", msg="Spotty produced no data after startup attempts")
                    return

            # Seek-to-start handling: send first chunk, then queue old process for cleanup
            if old_pid_to_kill != -1 and frame and bytes_sent < range_len:
                # Send an immediate PCM slice to help Kodi demuxer probe quickly.
                # Use 64KB to avoid being too small for probing.
                first_pcm = min(65536, len(frame), range_len - bytes_sent)
                if first_pcm > 0:
                    yield frame[:first_pcm]
                    bytes_sent += first_pcm
                # Queue old process for cleanup after first chunk is sent
                if old_pid_to_kill != -1:
                    self.__add_to_cleanup_queue(old_pid_to_kill)
                old_pid_to_kill = -1
                frame = frame[first_pcm:] if first_pcm < len(frame) else proc_stdout.read(c_size)

            while frame and bytes_sent < range_len:
                if self.__terminated:
                    return
                if spotty_process.poll() is not None:
                    rc = spotty_process.returncode
                    self._log_transfer("error", msg=f"Spotty process exited unexpectedly with code {rc}")
                    break
                bytes_sent += len(frame)
                if bytes_sent % 10485760 < c_size:
                    self._log_transfer("progress", bytes_sent=bytes_sent)
                yield frame
                frame = proc_stdout.read(c_size)

            # Pad with PCM silence if spotty exited cleanly but slightly short of the
            # WAV-declared length. This prevents Kodi from retrying the range request
            # in an infinite loop when the track duration was rounded up by a few seconds.
            if not self.__terminated and bytes_sent < range_len and spotty_process:
                remaining = range_len - bytes_sent
                rc = spotty_process.returncode
                if rc == 0 and 0 < remaining <= _SILENCE_PADDING_MAX_BYTES:
                    log_msg(
                        f"Track '{self.__track_id}': padding {remaining} bytes of silence "
                        f"(spotty exited {remaining} bytes short of WAV declared length).",
                        LOGDEBUG,
                    )
                    yield bytes(remaining)
                    bytes_sent += remaining

            end_of_range = range_begin + bytes_sent
            # WAV mode: track_length is known, fire when all bytes sent.
            if self.__track_length > 0 and end_of_range >= self.__track_length and range_begin == 0:
                self.__notify_track_finished(self.__track_id)
            self._log_transfer("finished", range_begin=range_begin, bytes_sent=bytes_sent)

        except Exception as ex:
            self._log_transfer("exception", range_begin=range_begin, bytes_sent=bytes_sent, ex=ex)
            log_exception(ex, "send_part_audio_stream")
        finally:
            # Ensure proper cleanup
            if spotty_process:
                try:
                    # Only terminate if process is still alive
                    if spotty_process.poll() is None:
                        spotty_process.terminate()
                        spotty_process.communicate(timeout=2)  # Reduced timeout
                except subprocess.TimeoutExpired:
                    # Force kill if it doesn't terminate gracefully
                    try:
                        spotty_process.kill()
                    except:
                        pass
                except Exception:
                    pass
                # Only clear if this was the active process
                if self.__current_spotty_pid == spotty_process.pid:
                    self.__current_spotty_pid = -1
            self.__terminated = False


    def __kill_current_spotty(self) -> None:
        """Kill the currently active spotty process and clear pid."""
        if self.__current_spotty_pid == -1:
            return
        kill_process_by_pid(self.__current_spotty_pid)
        self.__current_spotty_pid = -1

    def __add_to_cleanup_queue(self, pid: int) -> None:
        """Add a process PID to the cleanup queue to be killed asynchronously."""
        if pid == -1:
            return
        with self.__cleanup_lock:
            self.__processes_to_cleanup.append(pid)

    def __cleanup_old_processes(self) -> None:
        """Queue cleanup of old processes asynchronously so it doesn't block stream startup."""
        with self.__cleanup_lock:
            if not self.__processes_to_cleanup:
                return
            # Copy the list and clear it immediately
            pids_to_kill = self.__processes_to_cleanup.copy()
            self.__processes_to_cleanup.clear()

        # Kill processes in background thread to avoid blocking new playback
        def _kill_async():
            for pid in pids_to_kill:
                if pid != -1:
                    try:
                        kill_process_by_pid(pid)
                    except Exception:
                        pass

        threading.Thread(target=_kill_async, daemon=True).start()

    def __prepare_stream(self, defer_kill_previous: bool, range_begin: int) -> int:
        """
        Prepare for streaming: handle old process cleanup.
        If defer_kill_previous is True and seeking to start, return the old PID to kill later.
        Otherwise, kill the old process immediately.
        """
        old_pid_to_defer = -1

        if range_begin == 0 and self.__current_spotty_pid != -1:
            # At start of a new track
            if defer_kill_previous:
                # Return old PID to be killed after first chunk is sent
                old_pid_to_defer = self.__current_spotty_pid
            else:
                # Kill immediately and clean any queued processes
                self.__cleanup_old_processes()
                self.__kill_current_spotty()
        else:
            # Mid-range request (seek), clean up any queued old processes
            self.__cleanup_old_processes()

        self.__current_spotty_pid = -1
        return old_pid_to_defer


    def _yield_initial_chunk(self, range_begin: int, range_len: int) -> Tuple[bytes, int]:
        """Return (bytes to yield for the initial part of the range, bytes_sent)."""
        header_len = len(self.__wav_header)
        if range_begin == 0:
            return self.__wav_header, header_len
        if range_begin < header_len:
            tail = self.__wav_header[range_begin:]
            to_send = min(len(tail), range_len)
            return tail[:to_send], to_send
        return b"", 0

    def _build_spotty_args(self, track_id_uri: str, start_sec: int) -> list:
        """Build the argument list for the spotty subprocess."""
        bitrate = self.bitrate if self.bitrate in _VALID_BITRATES else SPOTIFY_BITRATE
        args = [
            "--disable-audio-cache",
            "--disable-discovery",
            "--bitrate", bitrate,
            "--initial-volume", str(self.initial_volume),
        ]
        gain_type = self.normalization_gain_type
        if gain_type != "off":
            effective_gain = gain_type if gain_type in _VALID_GAIN_TYPES else _DEFAULT_GAIN_TYPE
            args += ["--enable-volume-normalisation", "--normalisation-gain-type", effective_gain]
        args += ["--single-track", track_id_uri]
        if self.use_autoplay:
            args += ["--autoplay"]
        if start_sec > 0:
            args += ["--start-position", str(start_sec)]
        return args

    def _discard_pcm_bytes(self, proc_stdout, num_bytes: int, chunk_size: int) -> None:
        """Read and discard num_bytes from proc_stdout (for seek). Stops on __terminated or EOF."""
        discarded = 0
        while discarded < num_bytes:
            if self.__terminated:
                return
            to_read = min(chunk_size, num_bytes - discarded)
            chunk = proc_stdout.read(to_read)
            if not chunk:
                return
            discarded += len(chunk)

    def _log_transfer(
        self,
        phase: str,
        range_begin: int = 0,
        bytes_sent: int = 0,
        msg: str = "",
        ex: Exception = None,
        extra_msg: str = "",
    ) -> None:
        """Single logging helper for transfer start, progress, finished, error, and exception."""
        tid = self.__track_id
        if phase == "start":
            log_msg(
                f"Start transfer for track '{tid}' range_begin={range_begin}, "
                f"norm={self.normalization_gain_type}, vol={self.initial_volume}, "
                f"length={self.__track_length} ({bytes_to_megabytes(self.__track_length):.1f}MB). {extra_msg}",
                LOGDEBUG,
            )
        elif phase == "progress":
            pct = int(100.0 * bytes_sent / self.__track_length) if self.__track_length else 0
            log_msg(
                f"Continue sending track '{tid}' - {bytes_to_megabytes(bytes_sent):.1f}MB ({pct}%).",
                LOGDEBUG,
            )
        elif phase == "finished":
            log_msg(
                f"Finished sending track '{tid}' range_begin={range_begin} bytes_sent={bytes_sent} ({bytes_to_megabytes(bytes_sent):.1f}MB).",
                LOGDEBUG,
            )
        elif phase == "error":
            log_msg(f"Track '{tid}': {msg}", LOGWARNING)
        elif phase == "exception":
            log_msg(
                f"Exception sending track '{tid}' range_begin={range_begin} bytes_sent={bytes_sent}: {ex}",
                LOGERROR,
            )

    def _log_spotty_process_start(self, spotty_process: subprocess.Popen) -> None:
        """Log spotty process start; warn if it already exited."""
        if spotty_process.returncode is not None:
            log_msg(f"Spotty process already exited with code {spotty_process.returncode}", LOGWARNING)
        else:
            log_msg(f"Spotty process started successfully with PID {spotty_process.pid}", LOGDEBUG)

    def __create_wav_header(self) -> Tuple[bytes, int]:
        """Build WAV header (RIFF/fmt/data) for the current track duration. Returns (header_bytes, total_stream_length)."""
        try:
            log_msg(f"Start getting wav header. Duration = {self.__track_duration}", LOGDEBUG)
            file = BytesIO()
            num_samples = 44100 * self.__track_duration
            channels = 2
            sample_rate = 44100
            bits_per_sample = 16

            # Generate format chunk.
            format_chunk_spec = "<4sLHHLLHH"
            format_chunk = struct.pack(
                format_chunk_spec,
                b"fmt ",  # Chunk id
                16,  # Size of this chunk (excluding chunk id and this field)
                1,  # Audio format, 1 for PCM
                channels,  # Number of channels
                sample_rate,  # Samplerate, 44100, 48000, etc.
                sample_rate * channels * (bits_per_sample // 8),  # Byterate
                channels * (bits_per_sample // 8),  # Blockalign
                bits_per_sample,  # 16 bits for two byte samples, etc.
            )

            # Generate data chunk.
            data_chunk_spec = "<4sL"
            data_size = int(num_samples * channels * (bits_per_sample // 8))
            data_chunk = struct.pack(
                data_chunk_spec,
                b"data",  # Chunk id
                data_size,  # Chunk size (excluding chunk id and this field)
            )
            # Standard WAV: RIFF size = 36 + data_size (4 + (8+16) + (8+data_size))
            riff_size = 36 + data_size
            main_header_spec = "<4sL4s"
            main_header = struct.pack(
                main_header_spec,
                b"RIFF",  # Chunk id
                riff_size,  # Size of the rest of the file (excluding RIFF and size fields)
                b"WAVE",  # Format
            )

            # Write all the header contents.
            file.write(main_header)
            file.write(format_chunk)
            file.write(data_chunk)

            header_bytes = file.getvalue()
            header_len = len(header_bytes)
            total_length = header_len + data_size
            return header_bytes, total_length

        except Exception as exc:
            log_exception(exc, "Failed to create wave header.")
            raise  # Re-raise to fail fast


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

