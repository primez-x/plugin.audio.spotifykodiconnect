import os
import threading
import time

import xbmcvfs
from xbmc import LOGDEBUG, LOGWARNING, LOGERROR

from spotty import Spotty
from utils import log_msg, log_exception


def _clamp_volume(value: int) -> int:
    try:
        v = int(value)
        return max(1, min(100, v))
    except (TypeError, ValueError):
        return 35


class SpottyDownloader:
    """Downloads a single track from spotty to a temp file in the background."""

    def __init__(
        self,
        spotty: Spotty,
        track_id: str,
        duration_sec: float,
        start_byte: int,
        bitrate: str,
        normalization: str,
        volume: int,
        wav_header: bytes,
        track_length: int,
    ):
        self.spotty = spotty
        self.track_id = track_id
        self.duration_sec = duration_sec
        self.start_byte = start_byte
        self.bitrate = bitrate
        self.normalization = normalization
        self.volume = _clamp_volume(volume)
        self.wav_header = wav_header
        self.track_length = track_length

        temp_dir = xbmcvfs.translatePath("special://temp")
        self.file_path = os.path.join(temp_dir, f"spotify_{track_id}_{start_byte}.wav")

        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.written_bytes = 0
        self.is_finished = False
        self.error = False
        self.aborted = False
        self.process = None
        self.thread = None

    def start(self):
        with self.cond:
            if self.thread is not None:
                return

            try:
                # Initialize file
                with open(self.file_path, "wb") as f:
                    header_len = len(self.wav_header)
                    if self.start_byte == 0:
                        f.write(self.wav_header)
                        self.written_bytes = header_len
                    elif self.start_byte < header_len:
                        f.write(self.wav_header[self.start_byte :])
                        self.written_bytes = header_len - self.start_byte
            except Exception as e:
                log_exception(e, f"Failed to open cache file {self.file_path}")
                self.error = True
                return

            self.thread = threading.Thread(target=self._download_loop, daemon=True)
            self.thread.start()

    def _build_args(self):
        # Calculate start position in seconds. 176400 bytes per second (44.1kHz, 16-bit, stereo)
        header_len = len(self.wav_header)
        pcm_target_offset = max(0, self.start_byte - header_len)
        start_sec_wav = (pcm_target_offset // 176400) if pcm_target_offset > 0 else 0

        args = [
            "--disable-audio-cache",
            "--disable-discovery",
            "--bitrate",
            self.bitrate,
            "--initial-volume",
            str(self.volume),
        ]
        if self.normalization != "off":
            args += [
                "--enable-volume-normalisation",
                "--normalisation-gain-type",
                self.normalization,
            ]
        args += ["--single-track", f"spotify:track:{self.track_id}"]
        if start_sec_wav > 0:
            args += ["--start-position", str(start_sec_wav)]
        return args, (pcm_target_offset % 176400)

    def _download_loop(self):
        log_msg(f"Starting background download for {self.track_id} at {self.start_byte}")
        process = None
        try:
            args, pcm_skip = self._build_args()
            process = self.spotty.run_spotty(args)

            with self.cond:
                self.process = process
                if self.aborted:
                    return

            if pcm_skip > 0:
                discarded = 0
                while discarded < pcm_skip and not self.aborted:
                    chunk = process.stdout.read(min(8192, pcm_skip - discarded))
                    if not chunk:
                        break
                    discarded += len(chunk)

            with open(self.file_path, "ab") as f:
                while not self.aborted:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    f.flush()
                    with self.cond:
                        self.written_bytes += len(chunk)
                        self.cond.notify_all()

            if process.poll() is None and not self.aborted:
                process.wait(timeout=2.0)

            with self.cond:
                if not self.aborted and process.returncode == 0:
                    remaining = (self.track_length - self.start_byte) - self.written_bytes
                    if 0 < remaining <= 176400 * 10:  # 10 secs max padding
                        log_msg(f"Padding {remaining} bytes to end of {self.track_id}")
                        with open(self.file_path, "ab") as f:
                            f.write(bytes(remaining))
                            f.flush()
                        self.written_bytes += remaining

                self.is_finished = True
                self.cond.notify_all()
                log_msg(f"Finished background download for {self.track_id}")

        except Exception as e:
            log_exception(e, "Error in download loop")
            with self.cond:
                self.error = True
                self.cond.notify_all()
        finally:
            if process:
                try:
                    process.kill()
                except:
                    pass

    def abort(self):
        with self.cond:
            self.aborted = True
            self.cond.notify_all()
        if self.process:
            try:
                self.process.kill()
            except:
                pass

    def cleanup(self):
        self.abort()
        try:
            if os.path.exists(self.file_path):
                os.remove(self.file_path)
        except:
            pass

    def wait_for_bytes(self, target_bytes: int, timeout: float = None) -> bool:
        with self.cond:
            start_time = time.time()
            while (
                self.written_bytes < target_bytes
                and not self.is_finished
                and not self.error
                and not self.aborted
            ):
                if timeout:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        break
                    self.cond.wait(timeout - elapsed)
                else:
                    self.cond.wait(1.0)
            return self.written_bytes >= target_bytes or self.is_finished


class SpottyCacheManager:
    _instances = {}
    _lock = threading.Lock()
    _recent_tracks = []

    @classmethod
    def get_or_start(
        cls,
        spotty: Spotty,
        track_id: str,
        duration_sec: float,
        start_byte: int,
        bitrate: str,
        norm: str,
        volume: int,
        wav_header: bytes,
        track_length: int,
    ) -> SpottyDownloader:
        with cls._lock:
            if track_id in cls._recent_tracks:
                cls._recent_tracks.remove(track_id)
            cls._recent_tracks.append(track_id)

            key = (track_id, start_byte)
            if key in cls._instances:
                inst = cls._instances[key]
                if not inst.aborted and not inst.error:
                    return inst
                else:
                    inst.abort()
                    del cls._instances[key]

            inst = SpottyDownloader(
                spotty,
                track_id,
                duration_sec,
                start_byte,
                bitrate,
                norm,
                volume,
                wav_header,
                track_length,
            )
            cls._instances[key] = inst
            inst.start()

            # Keep only the 3 most recent tracks to save disk space
            tracks_to_keep = set(cls._recent_tracks[-3:])
            for k in list(cls._instances.keys()):
                if k[0] not in tracks_to_keep:
                    cls._instances[k].cleanup()
                    del cls._instances[k]

            return inst

    @classmethod
    def find_best_downloader(cls, track_id: str, request_byte: int):
        with cls._lock:
            best = None
            for k, inst in cls._instances.items():
                if k[0] == track_id and not inst.aborted and not inst.error:
                    if inst.start_byte <= request_byte:
                        if best is None or inst.start_byte > best.start_byte:
                            best = inst
            return best

    @classmethod
    def cleanup_all(cls):
        with cls._lock:
            for inst in cls._instances.values():
                inst.cleanup()
            cls._instances.clear()
