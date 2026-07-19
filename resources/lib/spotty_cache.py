import re
import threading
import time
from collections import deque

from spotty import Spotty
from utils import log_exception, log_msg
from xbmc import LOGDEBUG, LOGERROR, LOGWARNING

PCM_BYTES_PER_SEC = 176400
_SHORT_FINISH_PADDING_MAX_BYTES = PCM_BYTES_PER_SEC * 10
_FIRST_REAL_PCM_TIMEOUT_SECONDS = 5.0
_NO_PROGRESS_TIMEOUT_SECONDS = 10.0
_WATCHDOG_POLL_SECONDS = 0.1
_STDERR_TAIL_LINES = 32
_STDERR_LINE_MAX_CHARS = 512
_STDERR_PENDING_MAX_CHARS = 8192

_SENSITIVE_DIAGNOSTIC_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:authorization|proxy[_-]?authorization|cookie|set[_-]?cookie)\b\s*:|"
    r"\b(?:access[_-]?token|refresh[_-]?token|token|password|passwd|secret|"
    r"client[_-]?secret|api[_-]?key|credentials?|username|auth[_-]?data|"
    r"auth[_-]?blob|reusable[_-]?auth[_-]?credentials|sp_dc|sp_key)\b"
    r"[\"']?\s*[:=]"
    r")"
)
_AUTH_SCHEME_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_URL_USERINFO_RE = re.compile(r"(://)[^/@\s]+@")


def _redact_spotty_diagnostic(value: str) -> str:
    """Return a bounded, single-line diagnostic with likely secrets removed."""
    clean = "".join(ch if ch.isprintable() else " " for ch in (value or ""))
    if _SENSITIVE_DIAGNOSTIC_RE.search(clean):
        return "<redacted>"
    clean = _URL_USERINFO_RE.sub(r"\1<redacted>@", clean)
    clean = _AUTH_SCHEME_RE.sub("Auth <redacted>", clean)
    return " ".join(clean.split())[:_STDERR_LINE_MAX_CHARS]


class _SpottyStderrTail:
    """Continuously drain stderr without allowing diagnostics to grow unbounded."""

    def __init__(self, process):
        self._process = process
        self._lines = deque(maxlen=_STDERR_TAIL_LINES)
        self._lock = threading.Lock()
        self._thread = None

    def start(self) -> None:
        stream = getattr(self._process, "stderr", None)
        if stream is None:
            return
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        stream = getattr(self._process, "stderr", None)
        if stream is None:
            return
        pending = ""
        discard_until_newline = False
        try:
            read = getattr(stream, "read1", None) or stream.read
            while True:
                chunk = read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace").replace("\r", "\n")
                if discard_until_newline:
                    separator = text.find("\n")
                    if separator < 0:
                        continue
                    text = text[separator + 1 :]
                    discard_until_newline = False
                pending += text
                entries = pending.split("\n")
                pending = entries.pop()
                with self._lock:
                    for entry in entries:
                        if len(entry) > _STDERR_PENDING_MAX_CHARS:
                            self._lines.append("<oversized Spotty diagnostic redacted>")
                            continue
                        entry = _redact_spotty_diagnostic(entry)
                        if entry:
                            self._lines.append(entry)
                if len(pending) > _STDERR_PENDING_MAX_CHARS:
                    # Never retain or log fragments from an unbounded line: a
                    # credential split across pipe reads must not leak through
                    # diagnostics just because it did not contain a newline.
                    with self._lock:
                        self._lines.append("<oversized Spotty diagnostic redacted>")
                    pending = ""
                    discard_until_newline = True
            if pending and not discard_until_newline:
                entry = _redact_spotty_diagnostic(pending)
                if entry:
                    with self._lock:
                        self._lines.append(entry)
        except Exception:
            # Diagnostics must never be able to break audio streaming.
            return

    def join(self, timeout: float = 0.5) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def snapshot(self) -> str:
        with self._lock:
            return " | ".join(self._lines)


def _clamp_volume(value: int) -> int:
    try:
        v = int(value)
        return max(1, min(100, v))
    except (TypeError, ValueError):
        return 35


class SpottyDownloader:
    """Downloads a single track from spotty into an in-memory buffer in the background."""

    # Reclaim consumed bytes from the buffer head to bound memory WITHOUT ever
    # stalling the spotty stdout drain.  The v1.0.17 backpressure cap blocked
    # the writer via cond.wait, which stalled librespot's 64 KB stdout pipe and
    # truncated streams (spotty session killed mid-track → partial-file skip).
    # Head-trimming instead releases bytes the consumer has already read; the
    # writer always pulls from spotty immediately.
    #
    # _TRIM_BATCH_BYTES: only reclaim ≥1 MB at a time to amortize the O(n)
    # bytearray memmove.
    # _MAX_UNREAD_TAIL_BYTES: safety valve — if the writer drifts >~2 min of
    # PCM ahead of every consumer (stalled consumer / pathological burst), the
    # oldest unread bytes are dropped.  Late range requests for dropped bytes
    # fall through to get_or_start (the existing seek path).
    _TRIM_BATCH_BYTES = 1024 * 1024
    _MAX_UNREAD_TAIL_BYTES = 24 * 1024 * 1024

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
        startup_silence_bytes: int = 0,
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
        self.startup_silence_bytes = max(0, int(startup_silence_bytes))
        self.startup_silence_bytes -= self.startup_silence_bytes % 4

        self._buffer = bytearray()
        self._consumed_pos = 0  # furthest byte any consumer has read (abs, rel start_byte)
        self._trim_offset = 0  # absolute pos of buffer head (rel start_byte)
        self._consumer_positions = {}
        self._next_consumer_id = 0

        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.written_bytes = 0
        # Explicit readiness/progress state. Seeded WAV header and startup
        # silence never count as real PCM.
        self.real_pcm_bytes = 0
        self.first_real_pcm_monotonic = None
        self.last_progress_monotonic = None
        self.is_finished = False
        self.error = False
        self.aborted = False
        self.process = None
        self.thread = None

    def start(self):
        with self.cond:
            if self.thread is not None:
                return

            header_len = len(self.wav_header)
            prefix_end = header_len + self.startup_silence_bytes
            if self.start_byte == 0:
                self._buffer.extend(self.wav_header)
                if self.startup_silence_bytes:
                    self._buffer.extend(bytes(self.startup_silence_bytes))
                self.written_bytes = prefix_end
            elif self.start_byte < header_len:
                self._buffer.extend(self.wav_header[self.start_byte :])
                if self.startup_silence_bytes:
                    self._buffer.extend(bytes(self.startup_silence_bytes))
                self.written_bytes = prefix_end - self.start_byte
            elif self.start_byte < prefix_end:
                self._buffer.extend(bytes(prefix_end - self.start_byte))
                self.written_bytes = prefix_end - self.start_byte

            self.thread = threading.Thread(target=self._download_loop, daemon=True)
            self.thread.start()

    def has_real_pcm(self) -> bool:
        with self.cond:
            return self.real_pcm_bytes > 0 and not self.error and not self.aborted

    def has_recent_progress(self, max_idle_seconds: float) -> bool:
        with self.cond:
            if self.error or self.aborted or self.real_pcm_bytes <= 0:
                return False
            if self.is_finished:
                return True
            last_progress = self.last_progress_monotonic
        if last_progress is None:
            return False
        return (time.monotonic() - last_progress) <= max(0.0, float(max_idle_seconds))

    def wait_for_real_pcm(self, target_real_pcm_bytes: int, timeout: float = None) -> bool:
        """Wait for actual Spotty PCM, excluding the synthetic WAV prefix."""
        target = max(1, int(target_real_pcm_bytes))
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self.cond:
            while (
                self.real_pcm_bytes < target
                and not self.is_finished
                and not self.error
                and not self.aborted
            ):
                if deadline is None:
                    self.cond.wait(1.0)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(remaining)
            return self.real_pcm_bytes >= target and not self.error and not self.aborted

    def _record_real_pcm_locked(self, byte_count: int) -> None:
        if byte_count <= 0:
            return
        now = time.monotonic()
        self.real_pcm_bytes += byte_count
        if self.first_real_pcm_monotonic is None:
            self.first_real_pcm_monotonic = now
        self.last_progress_monotonic = now

    def _build_args(self, start_byte=None):
        # Calculate start position in seconds. 176400 bytes per second (44.1kHz, 16-bit, stereo)
        target_start_byte = self.start_byte if start_byte is None else int(start_byte)
        header_len = len(self.wav_header)
        pcm_target_offset = max(0, target_start_byte - header_len - self.startup_silence_bytes)
        start_sec_wav = pcm_target_offset // PCM_BYTES_PER_SEC if pcm_target_offset > 0 else 0

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
        return args, (pcm_target_offset % PCM_BYTES_PER_SEC)

    # Session/transient retry: when spotty exits before enough real PCM arrives,
    # keep the HTTP generator open and retry from the byte offset already buffered.
    # Small tail mismatches can be padded, but large gaps are stream failures.
    _MAX_SESSION_RETRIES = 3
    _RETRY_DELAYS = [0.5, 1.0, 1.5]

    def _download_loop(self):
        log_msg(f"Starting background download for {self.track_id} at {self.start_byte}")

        for attempt in range(self._MAX_SESSION_RETRIES + 1):
            if self.aborted:
                return

            process = None
            pcm_bytes_read = 0
            attempt_exception = None
            attempt_done = threading.Event()
            attempt_state_lock = threading.Lock()
            attempt_state = {
                "started_at": None,
                "last_output_at": None,
                "has_real_pcm": False,
                "timeout_reason": "",
            }
            watchdog_thread = None
            stderr_tail = None
            try:
                attempt_start_byte = self.start_byte + self.written_bytes
                args, pcm_skip = self._build_args(attempt_start_byte)
                process = self.spotty.run_spotty(args)
                attempt_process = process
                with attempt_state_lock:
                    attempt_state["started_at"] = time.monotonic()
                stderr_tail = _SpottyStderrTail(process)
                stderr_tail.start()

                with self.cond:
                    self.process = process
                    if self.aborted:
                        return

                # stdout.read() stays blocking so PCM is drained efficiently.
                # This watchdog kills only this attempt's process, unblocking the
                # read without allowing a stale attempt to kill a newer retry.
                def _watch_attempt(attempt_process=attempt_process):
                    while not attempt_done.wait(_WATCHDOG_POLL_SECONDS):
                        if self.aborted:
                            return
                        now = time.monotonic()
                        with attempt_state_lock:
                            if attempt_state["timeout_reason"]:
                                return
                            if attempt_state["has_real_pcm"]:
                                last_output = attempt_state["last_output_at"] or now
                                elapsed = now - last_output
                                if elapsed < _NO_PROGRESS_TIMEOUT_SECONDS:
                                    continue
                                attempt_state["timeout_reason"] = (
                                    f"no PCM progress for {elapsed:.1f}s"
                                )
                            else:
                                started_at = attempt_state["started_at"] or now
                                elapsed = now - started_at
                                if elapsed < _FIRST_REAL_PCM_TIMEOUT_SECONDS:
                                    continue
                                attempt_state["timeout_reason"] = (
                                    f"no real PCM within {elapsed:.1f}s"
                                )
                        try:
                            attempt_process.kill()
                        except Exception:
                            pass
                        return

                watchdog_thread = threading.Thread(target=_watch_attempt, daemon=True)
                watchdog_thread.start()

                if pcm_skip > 0:
                    discarded = 0
                    while discarded < pcm_skip and not self.aborted:
                        chunk = process.stdout.read(min(8192, pcm_skip - discarded))
                        if not chunk:
                            break
                        discarded += len(chunk)
                        with attempt_state_lock:
                            attempt_state["last_output_at"] = time.monotonic()

                while not self.aborted:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    if self.aborted:
                        break
                    pcm_bytes_read += len(chunk)
                    with attempt_state_lock:
                        attempt_state["has_real_pcm"] = True
                        attempt_state["last_output_at"] = time.monotonic()
                    with self.cond:
                        self._buffer.extend(chunk)
                        self.written_bytes += len(chunk)
                        self._record_real_pcm_locked(len(chunk))
                        self._trim_head_locked()
                        self.cond.notify_all()

                if process.poll() is None and not self.aborted:
                    process.wait(timeout=2.0)

            except Exception as exc:
                log_exception(exc, "Error in download loop attempt")
                attempt_exception = exc
            finally:
                attempt_done.set()
                if process:
                    try:
                        if process.poll() is None:
                            process.kill()
                    except Exception:
                        pass
                if watchdog_thread is not None:
                    # A retry must not start until the prior attempt's watchdog
                    # has exited; combined with the value-captured process this
                    # makes it impossible for stale watchdog work to kill a
                    # newer Spotty process.
                    watchdog_thread.join()
                if stderr_tail is not None:
                    stderr_tail.join()

            if self.aborted:
                return

            rc = getattr(process, "returncode", None) if process else -1
            if rc is None:
                try:
                    rc = process.wait(timeout=0.5)
                except Exception:
                    rc = -1
            with attempt_state_lock:
                timeout_reason = attempt_state["timeout_reason"]
            diagnostics = stderr_tail.snapshot() if stderr_tail is not None else ""
            with self.cond:
                remaining = max(0, (self.track_length - self.start_byte) - self.written_bytes)

            large_short_finish = remaining > _SHORT_FINISH_PADDING_MAX_BYTES
            retryable_finish = remaining > 0 and (
                bool(timeout_reason)
                or attempt_exception is not None
                or pcm_bytes_read == 0
                or large_short_finish
                or rc != 0
            )
            if diagnostics and (retryable_finish or rc != 0):
                log_msg(
                    f"Spotty diagnostic tail for {self.track_id}, attempt {attempt + 1}: "
                    f"{diagnostics}",
                    LOGWARNING,
                )
            if retryable_finish and attempt < self._MAX_SESSION_RETRIES:
                delay = self._RETRY_DELAYS[min(attempt, len(self._RETRY_DELAYS) - 1)]
                if timeout_reason:
                    reason = timeout_reason
                elif attempt_exception is not None:
                    reason = f"raised {type(attempt_exception).__name__}"
                elif pcm_bytes_read == 0:
                    reason = "produced 0 PCM bytes"
                elif large_short_finish:
                    reason = f"finished {remaining} bytes short"
                else:
                    reason = f"exited with code {rc}"
                log_msg(
                    f"Spotty {reason} for {self.track_id} "
                    f"(rc={rc}, transient stream failure). "
                    f"Retry {attempt + 1}/{self._MAX_SESSION_RETRIES} after {delay}s.",
                    LOGWARNING,
                )
                # Use condition wait so abort() can interrupt the delay.
                with self.cond:
                    if self.process is process:
                        self.process = None
                    if self.aborted:
                        return
                    self.cond.wait(timeout=delay)
                    if self.aborted:
                        return
                continue

            # Normal finish or final retry failure.
            with self.cond:
                if not self.aborted:
                    remaining = max(0, (self.track_length - self.start_byte) - self.written_bytes)
                    if (timeout_reason or attempt_exception is not None) and remaining > 0:
                        reason = timeout_reason or type(attempt_exception).__name__
                        log_msg(
                            f"Spotty {reason} after {self._MAX_SESSION_RETRIES} retries for "
                            f"{self.track_id} (rc={rc}). Marking as error.",
                            LOGWARNING,
                        )
                        self.error = True
                    elif pcm_bytes_read == 0 and remaining > 0:
                        log_msg(
                            f"Spotty produced 0 PCM bytes after "
                            f"{self._MAX_SESSION_RETRIES} retries for "
                            f"{self.track_id} (rc={rc}). Marking as error.",
                            LOGWARNING,
                        )
                        self.error = True
                    elif remaining > 0:
                        if rc == 0 and remaining <= _SHORT_FINISH_PADDING_MAX_BYTES:
                            log_msg(
                                f"Padding {remaining} bytes to end of {self.track_id}"
                                f" (spotty exited cleanly but short)",
                            )
                            self._buffer.extend(bytes(remaining))
                            self.written_bytes += remaining
                        else:
                            log_msg(
                                f"Spotty ended {remaining} bytes short for {self.track_id}"
                                f" after {self._MAX_SESSION_RETRIES} retries (rc={rc})."
                                f" Marking downloader as errored.",
                                LOGWARNING,
                            )
                            self.error = True
                    elif rc != 0:
                        log_msg(
                            f"Spotty exited with code {rc} for {self.track_id},"
                            f" marking downloader as errored.",
                            LOGWARNING,
                        )
                        self.error = True

                if self.process is process:
                    self.process = None
                self.is_finished = True
                self.cond.notify_all()
                if not self.error:
                    log_msg(f"Finished background download for {self.track_id}")
            return

    def abort(self):
        with self.cond:
            self.aborted = True
            self.cond.notify_all()
        if self.process:
            try:
                self.process.kill()
            except:
                pass

    def _trim_head_locked(self):
        """Drop consumed/surplus bytes from the buffer head.  Caller holds self.cond.

        Never blocks — the writer always drains spotty stdout immediately.
        Reclaims bytes every consumer has already read, in batched frame-aligned
        slices to amortize the bytearray memmove.  The unread-tail cap is a
        safety valve for stalled-consumer scenarios.
        """
        safe_consumed_pos = (
            min(self._consumer_positions.values())
            if self._consumer_positions
            else self._consumed_pos
        )
        reclaimable = safe_consumed_pos - self._trim_offset
        if reclaimable >= self._TRIM_BATCH_BYTES:
            reclaim = reclaimable - (reclaimable % 4)
            if reclaim > 0:
                del self._buffer[:reclaim]
                self._trim_offset += reclaim
        overflow = len(self._buffer) - self._MAX_UNREAD_TAIL_BYTES
        if overflow > 0 and not self._consumer_positions:
            drop = overflow - (overflow % 4)
            if drop > 0:
                del self._buffer[:drop]
                self._trim_offset += drop

    def cleanup(self):
        self.abort()
        with self.cond:
            self._buffer.clear()
            self._consumed_pos = 0
            self._trim_offset = 0
            self._consumer_positions.clear()
            self.real_pcm_bytes = 0
            self.first_real_pcm_monotonic = None
            self.last_progress_monotonic = None

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
        startup_silence_bytes: int = 0,
        allow_abort_others: bool = True,
    ) -> SpottyDownloader:
        with cls._lock:
            if track_id in cls._recent_tracks:
                cls._recent_tracks.remove(track_id)
            cls._recent_tracks.append(track_id)

            key = (track_id, start_byte)
            if key in cls._instances:
                inst = cls._instances[key]
                readable_start = inst.start_byte + getattr(inst, "_trim_offset", 0)
                if not inst.aborted and not inst.error and readable_start <= start_byte:
                    return inst
                else:
                    inst.cleanup()
                    del cls._instances[key]

            if not allow_abort_others:
                for k, other in list(cls._instances.items()):
                    if k != key and not other.is_finished and not other.aborted and not other.error:
                        log_msg(
                            f"Deferring download for {track_id}: active downloader "
                            f"{k[0]} is still running.",
                            LOGDEBUG,
                        )
                        cls._recent_tracks = [t for t in cls._recent_tracks if t != track_id]
                        return None

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
                startup_silence_bytes,
            )
            cls._instances[key] = inst

            if allow_abort_others:
                # Abort all other still-running downloads before starting this one.
                # librespot only allows one active Spotify stream per account; a second
                # spotty process connecting kicks the first (and vice-versa), causing a
                # mutual-kick loop that leaves every track with only the 44-byte WAV header.
                for k, other in list(cls._instances.items()):
                    if k != key and not other.is_finished and not other.aborted:
                        other.abort()

            inst.start()

            # Keep only the 3 most recent tracks in memory
            tracks_to_keep = set(cls._recent_tracks[-3:])
            for k in list(cls._instances.keys()):
                inst = cls._instances[k]
                has_active_consumers = False
                cond = getattr(inst, "cond", None)
                if cond is not None and hasattr(inst, "_consumer_positions"):
                    with cond:
                        has_active_consumers = bool(inst._consumer_positions)
                if has_active_consumers:
                    continue
                if k[0] not in tracks_to_keep:
                    inst.cleanup()
                    del cls._instances[k]

            # Trim _recent_tracks to only entries with active instances; prevents
            # unbounded list growth and ensures stale track IDs can't skew eviction.
            active_ids = {k[0] for k in cls._instances}
            cls._recent_tracks = [t for t in cls._recent_tracks if t in active_ids]

            return inst

    @classmethod
    def find_active_downloader(cls, track_id: str):
        """Return the downloader that best represents live playback for a track.

        Unlike find_best_downloader(), this lookup is intentionally independent
        of whether byte zero (or any other requested range) has been trimmed.
        Active consumers take priority, followed by real-PCM/running state and
        the most recent seek origin.
        """
        with cls._lock:
            best = None
            best_score = None
            for key, inst in cls._instances.items():
                if key[0] != track_id:
                    continue
                with inst.cond:
                    if inst.aborted or inst.error:
                        continue
                    score = (
                        bool(inst._consumer_positions),
                        int(getattr(inst, "real_pcm_bytes", 0) or 0) > 0,
                        not inst.is_finished,
                        int(inst.start_byte),
                    )
                if best_score is None or score > best_score:
                    best = inst
                    best_score = score
            return best

    @classmethod
    def find_best_downloader(cls, track_id: str, request_byte: int):
        with cls._lock:
            best = None
            for k, inst in cls._instances.items():
                if k[0] == track_id and not inst.aborted and not inst.error:
                    if inst.start_byte <= request_byte:
                        # Skip downloaders that have trimmed past the requested byte.
                        if inst.start_byte + inst._trim_offset <= request_byte:
                            if best is None or inst.start_byte > best.start_byte:
                                best = inst
            return best

    @classmethod
    def cleanup_all(cls):
        with cls._lock:
            for inst in cls._instances.values():
                inst.cleanup()
            cls._instances.clear()
            cls._recent_tracks.clear()
