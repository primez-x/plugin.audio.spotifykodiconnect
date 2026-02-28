import json
import os
import struct
import subprocess
from io import BytesIO
from typing import Callable, Tuple

from xbmc import LOGDEBUG, LOGWARNING, LOGERROR

from spotty import Spotty
from utils import bytes_to_megabytes, kill_process_by_pid, log_msg, log_exception

SPOTIFY_TRACK_PREFIX = "spotify:track:"

SPOTIFY_BITRATE = "320"
SPOTTY_GAIN_TYPE = "track"
SPOTTY_STREAMING_BASE_ARGS = [
    "--disable-audio-cache",
    "--disable-discovery",
    "--bitrate",
    SPOTIFY_BITRATE,
]
SPOTTY_STREAMING_NORMALIZATION_ARGS = [
    "--enable-volume-normalisation",
    "--normalisation-gain-type",
    SPOTTY_GAIN_TYPE,
]


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
        self.__last_spotty_pid = -1
        self.__terminated = False

        self.use_normalization = True

    def set_initial_volume(self, value: int) -> None:
        self.initial_volume = _clamp_volume(value)

    def get_track_length(self) -> int:
        return self.__track_length

    def get_track_duration(self) -> int:
        return self.__track_duration

    def set_track(self, track_id: str, track_duration: float) -> None:
        self.__track_id = track_id
        self.__track_duration = int(track_duration)
        self.__wav_header, self.__track_length = self.__create_wav_header()

    def set_notify_track_finished(self, func: Callable[[str], None]) -> None:
        self.__notify_track_finished = func

    def terminate_stream(self) -> bool:
        self.__terminated = True
        if self.__last_spotty_pid == -1:
            return False
        self.__kill_last_spotty()
        return True

    def send_part_audio_stream(self, range_len: int, range_begin: int) -> str:
        """Chunked transfer of audio data from spotty binary"""

        self.__terminated = False
        spotty_process = None
        bytes_sent = 0
        try:
            self.__kill_last_spotty()

            self.__log_start_transfer(range_begin)

            # File layout is [WAV header][PCM from spotty]. range_begin/range_len
            # are file offsets. Spotty only outputs PCM, so we must skip
            # (range_begin - header_len) bytes of PCM when seeking, not range_begin.
            track_id_uri = SPOTIFY_TRACK_PREFIX + self.__track_id
            self.__log_start_reading_audio(track_id_uri)

            # File layout is [WAV header][PCM from spotty]. range_begin/range_len
            # are file offsets.
            header_len = len(self.__wav_header)
            
            # Calculate where we need to start in the PCM stream
            pcm_target_offset = max(0, range_begin - header_len)
            
            # Use spotty's --start-position (in seconds) to avoid decoding everything from the beginning
            # 44100 Hz * 2 channels * 2 bytes = 176400 bytes/sec
            pcm_bytes_per_sec = 176400
            
            # Fast path logic: only do division and remainder if we are actually skipping into the PCM stream
            if pcm_target_offset > 0:
                start_sec = pcm_target_offset // pcm_bytes_per_sec
                pcm_skip = pcm_target_offset % pcm_bytes_per_sec
            else:
                start_sec = 0
                pcm_skip = 0

            if range_begin == 0:
                bytes_sent = header_len
                self.__log_send_wav_header()
                yield self.__wav_header
            elif range_begin < header_len:
                # Range starts inside the header (rare).
                tail = self.__wav_header[range_begin:]
                to_send = min(len(tail), range_len)
                yield tail[:to_send]
                bytes_sent = to_send

            # Execute the spotty process, then collect stdout.
            args = SPOTTY_STREAMING_BASE_ARGS.copy()
            args += ["--initial-volume", str(self.initial_volume)]
            if self.use_normalization:
                args += SPOTTY_STREAMING_NORMALIZATION_ARGS
            args += ["--single-track", track_id_uri]
            
            # Add start-position if we are seeking into the track
            if start_sec > 0:
                args += ["--start-position", str(start_sec)]
                
            # Check if terminated more frequently to be responsive
            if self.__terminated:
                return

            spotty_process = self.__spotty.run_spotty(args)
            self.__log_spotty_return_code(spotty_process)
            self.__last_spotty_pid = spotty_process.pid

            # Process reference and chunk size for inner loops
            proc_stdout = spotty_process.stdout
            c_size = self.chunk_size

            # Skip the exact remaining PCM bytes so that we start perfectly on target
            if pcm_skip > 0:
                # We need to read and discard `pcm_skip` bytes
                discarded = 0
                while discarded < pcm_skip:
                    if self.__terminated:
                        return
                    to_read = min(c_size, pcm_skip - discarded)
                    chunk = proc_stdout.read(to_read)
                    if not chunk:
                        break
                    discarded += len(chunk)

            # Pre-fetch the first chunk before the loop to reduce latency
            frame = proc_stdout.read(c_size)
            
            # Loop as long as there's something to output.
            while frame and bytes_sent < range_len:
                if self.__terminated:
                    return

                bytes_sent += len(frame)
                
                # Only log every ~10MB to reduce IO overhead in tight loop
                if bytes_sent % 10485760 < c_size:
                    self.__log_continue_sending(bytes_sent)
                
                yield frame
                
                # Fetch next frame while yielding current to allow overlap
                frame = proc_stdout.read(c_size)

            # All done.
            self.__notify_track_finished(self.__track_id)
            self.__log_finished_sending(range_begin, bytes_sent)

        except Exception as ex:
            self.__log_exception_sending(ex, range_begin, bytes_sent)
        finally:
            # Make sure spotty always gets terminated.
            if spotty_process:
                self.__last_spotty_pid = -1
                spotty_process.terminate()
                spotty_process.communicate()
                # Make really sure!
                kill_process_by_pid(spotty_process.pid)

    def __kill_last_spotty(self) -> None:
        if self.__last_spotty_pid == -1:
            return
        kill_process_by_pid(self.__last_spotty_pid)
        self.__last_spotty_pid = -1

    def __log_start_transfer(self, range_begin: int) -> None:
        log_msg(
            f"Start transfer for track '{self.__track_id}' - range begin: {range_begin}",
            LOGDEBUG,
        )
        log_msg(
            f"Use Spotify normalization: {self.use_normalization}, initial volume: {self.initial_volume}.",
            LOGDEBUG,
        )

    def __log_send_wav_header(self) -> None:
        log_msg(
            f"Sending wav header for track '{self.__track_id}'.",
            LOGDEBUG,
        )

    def __log_start_reading_audio(self, track_id_uri: str) -> None:
        log_msg(
            f"Start reading audio data for track: '{track_id_uri}',"
            f" length = {self.__track_length} ({self.__get_mb_str(self.__track_length)}).",
            LOGDEBUG,
        )

    def __log_continue_sending(self, bytes_sent: int) -> None:
        log_msg(
            f"Continue sending track '{self.__track_id}'"
            f" - {self.__get_data_sent_str(bytes_sent, self.__track_length)}.",
            LOGDEBUG,
        )

    def __log_finished_sending(self, range_begin: int, bytes_sent: int) -> None:
        log_msg(
            f"Finished sending track '{self.__track_id}'"
            f" - range begin {range_begin}"
            f" - range end {bytes_sent} - {self.__get_mb_str(bytes_sent)}.",
            LOGDEBUG,
        )

    def __log_exception_sending(self, ex: Exception, range_begin: int, bytes_sent: int) -> None:
        log_msg(
            f"EXCEPTION sending track '{self.__track_id}'"
            f" - range begin {range_begin}"
            f" - range end {bytes_sent} - {self.__get_mb_str(bytes_sent)}.",
            LOGERROR,
        )
        log_msg(f"Exception: {ex}")

    @staticmethod
    def __log_spotty_return_code(spotty_process: subprocess.Popen) -> None:
        if spotty_process.returncode:
            log_msg(
                f"Spotty process return code: {spotty_process.returncode}",
                LOGWARNING,
            )

    @staticmethod
    def __get_mb_str(data_bytes: int) -> str:
        data_mb = bytes_to_megabytes(data_bytes)
        return f"{data_mb:.1f}MB"

    @staticmethod
    def __get_data_sent_str(data_bytes: int, track_length: int) -> str:
        data_mb = bytes_to_megabytes(data_bytes)
        percent = int(100.0 * float(data_bytes) / float(track_length))
        return f"sent so far: {data_mb:>5.1f}MB ({percent:>3}%)"

    def __create_wav_header(self) -> Tuple[bytes, int]:
        """generate a wav header for the stream"""
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
                "fmt ".encode(encoding="UTF-8"),  # Chunk id
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
            data_size = num_samples * channels * (bits_per_sample / 8)
            data_chunk = struct.pack(
                data_chunk_spec,
                "data".encode(encoding="UTF-8"),  # Chunk id
                int(data_size),  # Chunk size (excluding chunk id and this field)
            )
            sum_items = [
                # "WAVE" string following size field
                4,
                # "fmt " + chunk size field + chunk size
                struct.calcsize(format_chunk_spec),
                # Size of data chunk spec + data size
                struct.calcsize(data_chunk_spec) + data_size,
            ]

            # Generate main header.
            all_chunks_size = int(sum(sum_items))
            main_header_spec = "<4sL4s"
            main_header = struct.pack(
                main_header_spec,
                "RIFF".encode(encoding="UTF-8"),
                all_chunks_size,
                "WAVE".encode(encoding="UTF-8"),
            )

            # Write all the contents in.
            file.write(main_header)
            file.write(format_chunk)
            file.write(data_chunk)

            return file.getvalue(), all_chunks_size + 8

        except Exception as exc:
            log_exception(exc, "Failed to create wave header.")
