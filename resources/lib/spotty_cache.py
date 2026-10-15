import os
import re
import tempfile
import threading
import time
from array import array
from collections import deque
from operator import sub
from typing import Optional

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
_RETAIN_IDLE_SECONDS = 60.0


def _account_identity(spotty):
    reader = getattr(spotty, "get_spotty_credentials_identity", None)
    return reader() if callable(reader) else None


class _PCMFile:
    """One anonymous store with serialized portable offset I/O."""

    def __init__(self, capacity, release):
        from utils import ADDON_DATA_PATH

        self.capacity = capacity
        self._release = release
        self._lock = threading.Lock()
        directory = os.path.join(ADDON_DATA_PATH, "spotty-cache")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self._file = tempfile.TemporaryFile(mode="w+b", buffering=0, dir=directory)

    def append(self, offset, data):
        if offset < 0 or offset + len(data) > self.capacity:
            raise OSError("PCM exceeds declared WAV length")
        with self._lock:
            self._file.seek(offset)
            view = memoryview(data)
            while view:
                count = self._file.write(view)
                if count is None or count <= 0 or count > len(view):
                    raise OSError("Incomplete PCM write")
                view = view[count:]

    def read(self, offset, size):
        with self._lock:
            self._file.seek(offset)
            data = self._file.read(size)
            if len(data) != size:
                raise OSError("Incomplete committed PCM read")
            return data

    def close(self):
        with self._lock:
            if self._file.closed:
                return
            self._file.close()
            self._release(self.capacity)


# Writer-side backpressure.  The writer stops pulling Spotty stdout once it is
# _BP_HIGH_BYTES ahead of the leading reader (or of the furthest consumed byte
# when no reader is attached, e.g. a prebuffer) and resumes at _BP_LOW_BYTES.
# A blocked Spotty that exits/drops its session while paused is recovered by
# the offset retry (--start-position + byte skip from start_byte+written_bytes).
_BP_HIGH_BYTES = 16 * 1024 * 1024
_BP_LOW_BYTES = 12 * 1024 * 1024
_STDOUT_READ_BYTES = 65536
# Resuming a track after Spotty exits: --start-position lands on a decoder
# packet boundary tens of milliseconds *before* the requested second (measured
# 37-58 ms on CoreELEC), so a seconds offset plus a byte skip repeats audio.
# Instead restart _RESUME_LEAD_SECONDS earlier and find the exact resume point
# by matching the last _RESUME_REF_BYTES already written against the new
# output.  The restarted decode equals the earlier one only up to dither, hence
# the per-sample tolerance.
_RESUME_LEAD_SECONDS = 1
_RESUME_REF_BYTES = 8192
_RESUME_SEARCH_BEFORE_BYTES = PCM_BYTES_PER_SEC // 4
_RESUME_SEARCH_AFTER_BYTES = PCM_BYTES_PER_SEC
_RESUME_SAMPLE_TOLERANCE = 8
_RESUME_MIN_PEAK = 64
# An attempt that delivered at least this much real PCM made real progress;
# its failure resets the consecutive-failure retry budget.
_RETRY_BUDGET_RESET_BYTES = 1024 * 1024

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


def _classify_spotty_failure(diagnostic_tail: str) -> Optional[str]:
    """Return auth/premium only for verified terminal diagnostic samples.

    No local sample establishes a terminal pattern. Unknown diagnostics retain
    bounded retries until a verified sample supplies a conservative rule.
    """
    return None


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


def _find_resume_offset(ref: bytes, data: bytes, lo: int, hi: int):
    """Return the byte offset in *data* just past its best match of *ref*.

    Candidates are frame-aligned end offsets in [lo, hi].  A candidate must be
    within _RESUME_SAMPLE_TOLERANCE on every sample; the lowest total
    difference wins.  Returns None for near-silent references (ambiguous) or
    when nothing matches.
    """
    ref_samples = array("h", ref)
    peak = max(ref_samples, key=abs, default=0)
    if abs(peak) < _RESUME_MIN_PEAK:
        return None
    anchor = ref_samples.index(peak)
    count = len(ref_samples)
    data_samples = array("h", data[: len(data) - len(data) % 4])
    tolerance = _RESUME_SAMPLE_TOLERANCE
    best = None
    best_cost = None
    end = max(lo, len(ref)) // 4 * 4
    last = min(hi, len(data_samples) * 2)
    while end <= last:
        first = (end - len(ref)) // 2
        if abs(data_samples[first + anchor] - peak) <= tolerance:
            diffs = list(map(abs, map(sub, data_samples[first : first + count], ref_samples)))
            if max(diffs) <= tolerance:
                cost = sum(diffs)
                if best_cost is None or cost < best_cost:
                    best, best_cost = end, cost
        end += 4
    return best


def _clamp_volume(value: int) -> int:
    try:
        v = int(value)
        return max(1, min(100, v))
    except (TypeError, ValueError):
        return 35


class SpottyDownloader:
    """Download PCM into one admitted file or the bounded rolling RAM buffer."""

    # The rolling RAM fallback is bounded on two sides:
    #
    # * Writer backpressure (_wait_for_room): the writer pauses reading Spotty
    #   stdout once it is _BP_HIGH_BYTES ahead of the leading reader and
    #   resumes at _BP_LOW_BYTES.  The v1.0.17 cap was reverted because a
    #   blocked Spotty that exited early truncated the track; since attempts
    #   now resume from start_byte + written_bytes (--start-position + byte
    #   skip), a Spotty that dies while paused is simply restarted.
    # * Head trimming: bytes every active reader has already consumed are
    #   reclaimed in batched, frame-aligned slices (never past the slowest
    #   reader).
    #
    # _TRIM_BATCH_BYTES: only reclaim >=1 MB at a time to amortize the O(n)
    # bytearray memmove.
    # _MAX_UNREAD_TAIL_BYTES: insurance only -- with no reader attached the
    # oldest bytes beyond this cap are dropped.  Backpressure keeps the buffer
    # below it (see the import-time assertion after this class).  Late range
    # requests for dropped bytes fall through to get_or_start (the seek path).
    # _TRIM_RETAIN_BYTES: history kept behind the slowest reader.  Bytes we
    # yielded may still sit in socket buffers when Kodi drops a connection;
    # it resumes from what it actually received, which must stay readable or
    # the request restarts Spotty at a seconds offset (an audible seam).
    _TRIM_BATCH_BYTES = 1024 * 1024
    _TRIM_RETAIN_BYTES = 4 * 1024 * 1024
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
        self.account_identity = _account_identity(spotty)
        self.audio_identity = (
            track_id, duration_sec, bytes(wav_header), track_length,
            bitrate, normalization, self.volume,
        )
        self._file_store = None
        self._file_admission = False
        self._optional = False
        self._start_lock = threading.Lock()
        self._retained_until = None
        self._retained_idle = False
        self._ever_retained = False

        self._buffer = bytearray()
        self._consumed_pos = 0  # furthest byte any consumer has read (abs, rel start_byte)
        self._trim_offset = 0  # absolute pos of buffer head (rel start_byte)
        self._consumer_positions = {}
        self._next_consumer_id = 0

        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.written_bytes = 0
        # Explicit readiness/progress state. The seeded WAV header never counts
        # as real PCM.
        self.real_pcm_bytes = 0
        self.first_real_pcm_monotonic = None
        self.last_progress_monotonic = None
        self.is_finished = False
        self.error = False
        self.failure_cause = None
        self.created_monotonic = time.monotonic()
        self.aborted = False
        self.process = None
        self.thread = None
        # Backpressure diagnostics/state (guarded by self.cond).
        self._bp_paused = False
        self._bp_paused_total = 0.0
        self._peak_buffer_bytes = 0

    def start(self):
        with self._start_lock:
            if self.thread is not None:
                return True
            if self.aborted:
                return False
            if self._file_admission and self.start_byte == 0:
                try:
                    self._file_store = SpottyCacheManager._open_store(self.track_length)
                except OSError:
                    self._storage_failed()
                    self.is_finished = True
                    return False
                if self._file_store is None and self._optional:
                    return False
            header_len = len(self.wav_header)
            try:
                if self.start_byte < header_len:
                    self._append(self.wav_header[self.start_byte :])
            except OSError:
                self._storage_failed()
                self.is_finished = True
                return False
            with self.cond:
                if self.aborted:
                    return False
                self.thread = threading.Thread(target=self._download_loop, daemon=True)
                self.thread.start()
            return True

    def matches(self, stream_spec, account_identity) -> bool:
        expected = (
            stream_spec.track_id, stream_spec.track_duration, bytes(stream_spec.wav_header),
            stream_spec.track_length, stream_spec.bitrate,
            stream_spec.normalization_gain_type, _clamp_volume(stream_spec.initial_volume),
        )
        return (
            self.audio_identity == expected
            and account_identity is not None
            and self.account_identity == account_identity
        )

    def identity_valid(self) -> bool:
        current = _account_identity(self.spotty)
        return current is not None and current == self.account_identity

    def _storage_failed(self):
        with self.cond:
            self.error = True
            self.failure_cause = "storage"
            self.cond.notify_all()
        log_msg("PCM store failed; refusing incomplete audio.", LOGWARNING)
        process = self.process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass

    def _append(self, data, real_pcm=False):
        with self.cond:
            if self.aborted or self.error:
                return
            offset = self.written_bytes
        if offset + len(data) > self.track_length - self.start_byte:
            self._storage_failed()
            raise OSError("PCM exceeds declared WAV length")
        if self._file_store is not None:
            try:
                self._file_store.append(offset, data)
            except (OSError, ValueError):
                self._storage_failed()
                raise OSError("PCM store write failed") from None
        with self.cond:
            if self.aborted or self.error:
                return
            if self._file_store is None:
                self._buffer.extend(data)
            self.written_bytes += len(data)
            if real_pcm:
                self._record_real_pcm_locked(len(data))
            self._peak_buffer_bytes = max(self._peak_buffer_bytes, len(self._buffer))
            self._trim_head_locked()
            self.cond.notify_all()

    def read_range(self, offset, size):
        """Read only committed bytes, relative to the downloader origin."""
        with self.cond:
            if self.aborted or self.error or offset < self._trim_offset:
                return b""
            size = max(0, min(int(size), 1048576, self.written_bytes - offset))
            if self._file_store is None:
                index = offset - self._trim_offset
                return bytes(self._buffer[index : index + size])
        try:
            data = self._file_store.read(offset, size)
        except (OSError, ValueError):
            self._storage_failed()
            return b""
        with self.cond:
            return b"" if self.aborted or self.error else data

    def retain_after_stop(self):
        if not self.identity_valid():
            return False
        process = self.process
        with self.cond:
            eligible = (
                self.start_byte == 0 and self._file_store is not None
                and self.real_pcm_bytes >= 4 and not self.error and not self.aborted
                and (
                    self.is_finished or self.written_bytes >= self.track_length
                    or (process is not None and process.poll() is None)
                )
            )
            if eligible:
                if self._retained_until is None:
                    self._retained_until = time.monotonic() + _RETAIN_IDLE_SECONDS
                self._ever_retained = True
                self._retained_idle = not self.is_finished
                self.cond.notify_all()
            return eligible

    def reusable(self):
        if not self.identity_valid():
            return False
        with self.cond:
            if self.error or self.aborted:
                return False
            retained = self._retained_until
            finished = self.is_finished or self.written_bytes >= self.track_length - self.start_byte
            process = self.process
        if retained is not None and not finished:
            return time.monotonic() < retained and process is not None and process.poll() is None
        return True

    def notify_readers(self):
        with self.cond:
            self.cond.notify_all()

    def _resume_for_target_locked(self, target):
        if self._retained_idle and target > self.written_bytes:
            self._retained_idle = False
            self._retained_until = None
            self.cond.notify_all()

    def attach_reader_locked(self, position):
        consumer_id = self._next_consumer_id
        self._next_consumer_id += 1
        self._consumer_positions[consumer_id] = max(0, position)
        self._retained_until = None
        self.cond.notify_all()
        return consumer_id

    def release_reader_locked(self, consumer_id):
        self._consumer_positions.pop(consumer_id, None)
        if self._retained_idle and not self._consumer_positions and self._retained_until is None:
            self._retained_until = time.monotonic() + _RETAIN_IDLE_SECONDS
        self._trim_head_locked()
        self.cond.notify_all()

    def has_real_pcm(self) -> bool:
        with self.cond:
            return self.real_pcm_bytes > 0 and not self.error and not self.aborted

    def has_recent_progress(self, max_idle_seconds: float) -> bool:
        with self.cond:
            if self.error or self.aborted or self.real_pcm_bytes <= 0:
                return False
            if self.is_finished:
                return True
            if self._bp_paused and self._consumer_positions:
                # Reader-limited, not dead: the writer is deliberately idle
                # until an attached reader consumes more of the buffer.
                return True
            last_progress = self.last_progress_monotonic
        if last_progress is None:
            return False
        return (time.monotonic() - last_progress) <= max(0.0, float(max_idle_seconds))

    def is_reader_paused(self) -> bool:
        """True while the writer is backpressure-paused by an attached reader."""
        with self.cond:
            return bool(self._consumer_positions) and self._is_paused_locked()

    def is_paused(self) -> bool:
        """True while the writer is backpressure-paused, reader attached or not.

        A prebuffer, or a stream between two of Kodi's range requests, parks
        here for as long as nobody reads; that is idle, not stalled.
        """
        with self.cond:
            return self._is_paused_locked()

    def _is_paused_locked(self) -> bool:
        return self._bp_paused and self.real_pcm_bytes > 0 and not self.error and not self.aborted

    def _bp_reference_locked(self) -> int:
        """Position the writer's lead is measured from.  Caller holds self.cond.

        The LEADING reader drives the writer so one parked range reader cannot
        stall playback; trimming still never passes the slowest reader.  With
        no reader attached (prebuffer, or between Kodi range requests) the
        furthest consumed byte is used (0 for an unread prebuffer).
        """
        if self._consumer_positions:
            return max(self._consumer_positions.values())
        return self._consumed_pos

    def _bytes_ahead_locked(self) -> int:
        return self.written_bytes - self._bp_reference_locked()

    def advance_consumer_locked(self, consumer_id, position: int) -> None:
        """Record that a reader consumed up to *position*.  Caller holds self.cond.

        Updates the furthest-consumed mark and the reader's own position,
        reclaims the buffer head and wakes a backpressure-paused writer once
        it has dropped to the low-water mark.
        """
        if position > self._consumed_pos:
            self._consumed_pos = position
        if consumer_id in self._consumer_positions:
            if position > self._consumer_positions[consumer_id]:
                self._consumer_positions[consumer_id] = position
        self._trim_head_locked()
        if self._bp_paused and self._bytes_ahead_locked() <= _BP_LOW_BYTES:
            self.cond.notify_all()

    def _wait_for_room(self, attempt_state, attempt_state_lock) -> None:
        """Block the writer while it is too far ahead of the readers.

        Called before each stdout read (never inside the pcm_skip discard
        loop).  While paused, the attempt watchdog ignores the lack of PCM
        output; Spotty simply blocks on its full stdout pipe.  If Spotty exits
        meanwhile, the next read returns EOF and the offset retry restarts it.
        """
        with self.cond:
            if self.aborted:
                return
            ahead = self._bytes_ahead_locked()
            if ahead < _BP_HIGH_BYTES and not self._retained_idle:
                return
            readers = len(self._consumer_positions)
            self._bp_paused = True
        with attempt_state_lock:
            attempt_state["bp_paused"] = True
        paused_at = time.monotonic()
        log_msg(
            f"Spotty backpressure pause track={self.track_id} ahead={ahead} readers={readers}",
            LOGDEBUG,
        )
        with self.cond:
            try:
                while not self.aborted and not self.error and (
                    self._retained_idle
                    or self._bytes_ahead_locked() > _BP_LOW_BYTES
                ):
                    if self._retained_until is not None:
                        remaining = self._retained_until - time.monotonic()
                        if remaining <= 0:
                            self.aborted = True
                            self.cond.notify_all()
                            break
                        self.cond.wait(min(1.0, remaining))
                    else:
                        self.cond.wait(1.0)
            finally:
                self._bp_paused = False
                paused_for = time.monotonic() - paused_at
                self._bp_paused_total += paused_for
        with attempt_state_lock:
            # Restart both watchdog clocks: a retry attempt can pause before
            # its first read, so its first-PCM window must not include the
            # time spent waiting for readers.
            now = time.monotonic()
            attempt_state["bp_paused"] = False
            attempt_state["last_output_at"] = now
            attempt_state["started_at"] = now
        log_msg(
            f"Spotty backpressure resume track={self.track_id} after {paused_for:.1f}s",
            LOGDEBUG,
        )

    def wait_for_real_pcm(self, target_real_pcm_bytes: int, timeout: float = None, cancelled=None) -> bool:
        """Wait for actual Spotty PCM, excluding the generated WAV header."""
        target = max(1, int(target_real_pcm_bytes))
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self.cond:
            if not (cancelled and cancelled()):
                self._resume_for_target_locked(max(0, len(self.wav_header) - self.start_byte) + target)
            while (
                self.real_pcm_bytes < target
                and not self.is_finished
                and not self.error
                and not self.aborted
                and not (cancelled and cancelled())
            ):
                if deadline is None:
                    self.cond.wait(1.0)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.cond.wait(remaining)
            return (
                self.real_pcm_bytes >= target and not self.error and not self.aborted
                and not (cancelled and cancelled())
            )

    def _record_real_pcm_locked(self, byte_count: int) -> None:
        if byte_count <= 0:
            return
        now = time.monotonic()
        self.real_pcm_bytes += byte_count
        if self.first_real_pcm_monotonic is None:
            self.first_real_pcm_monotonic = now
        self.last_progress_monotonic = now

    def _build_args(self, start_byte=None, lead_sec=0):
        """Spotty args and the byte skip from its nominal start to *start_byte*."""
        # Calculate start position in seconds. 176400 bytes per second (44.1kHz, 16-bit, stereo)
        target_start_byte = self.start_byte if start_byte is None else int(start_byte)
        header_len = len(self.wav_header)
        pcm_target_offset = max(0, target_start_byte - header_len)
        start_sec_wav = max(0, pcm_target_offset // PCM_BYTES_PER_SEC - lead_sec)

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
        return args, pcm_target_offset - start_sec_wav * PCM_BYTES_PER_SEC

    def _resume_reference(self, resume_byte: int):
        """Already-written real PCM ending at the frame boundary at/before
        *resume_byte*, or None when insufficient committed PCM remains."""
        header_len = len(self.wav_header)
        frame_end = resume_byte - (resume_byte - header_len) % 4
        ref_start = frame_end - _RESUME_REF_BYTES
        buffer_start = self.start_byte + self._trim_offset
        if ref_start < max(buffer_start, header_len, self.start_byte):
            return None
        return self.read_range(ref_start - self.start_byte, _RESUME_REF_BYTES)

    def _read_prefix(self, process, size, attempt_state, attempt_state_lock) -> bytes:
        prefix = bytearray()
        while len(prefix) < size and not self.aborted:
            chunk = process.stdout.read(min(_STDOUT_READ_BYTES, size - len(prefix)))
            if not chunk:
                break
            prefix += chunk
            with attempt_state_lock:
                attempt_state["last_output_at"] = time.monotonic()
        return bytes(prefix)

    # Session/transient retry: when spotty exits before enough real PCM arrives,
    # keep the HTTP generator open and retry from the byte offset already buffered.
    # Small tail mismatches can be padded, but large gaps are stream failures.
    _MAX_SESSION_RETRIES = 3
    _RETRY_DELAYS = [0.5, 1.0, 1.5]

    def _download_loop(self):
        log_msg(f"Starting background download for {self.track_id} at {self.start_byte}")

        # Consecutive-failure budget: an attempt that delivered real progress
        # (>= _RETRY_BUDGET_RESET_BYTES of PCM) resets it, so a long track
        # whose paused Spotty is restarted several times is not limited to
        # _MAX_SESSION_RETRIES restarts in total.
        attempt = -1
        failures = 0
        while True:
            attempt += 1
            self._wait_for_room({"bp_paused": False}, threading.Lock())
            if self.aborted:
                self.cleanup()
                return

            process = None
            pcm_bytes_read = 0
            attempt_exception = None
            stopped_at_declared_end = False
            attempt_done = threading.Event()
            attempt_state_lock = threading.Lock()
            attempt_state = {
                "started_at": None,
                "last_output_at": None,
                "has_real_pcm": False,
                "timeout_reason": "",
                "bp_paused": False,
            }
            watchdog_thread = None
            stderr_tail = None
            try:
                with self.cond:
                    attempt_start_byte = self.start_byte + self.written_bytes
                resume_ref = self._resume_reference(attempt_start_byte)
                if self.error:
                    raise OSError("PCM resume reference read failed")
                if attempt > 0 and self._ever_retained and not resume_ref:
                    raise ValueError("Retained PCM has no exact resume reference")
                lead_sec = _RESUME_LEAD_SECONDS if resume_ref is not None else 0
                args, pcm_skip = self._build_args(attempt_start_byte, lead_sec)
                log_msg(
                    f"Spotty attempt {attempt + 1} for {self.track_id} at byte "
                    f"{attempt_start_byte}: skip {pcm_skip}"
                    f"{', content-aligned' if resume_ref is not None else ''}",
                    LOGDEBUG,
                )
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

                # The stdout read blocks until PCM arrives.
                # This watchdog kills only this attempt's process, unblocking the
                # read without allowing a stale attempt to kill a newer retry.
                def _watch_attempt(attempt_process=attempt_process):
                    exit_logged = False
                    while not attempt_done.wait(_WATCHDOG_POLL_SECONDS):
                        if self.aborted:
                            return
                        now = time.monotonic()
                        with self.cond:
                            retained = self._retained_until
                            retained_idle = self._retained_idle
                        if retained_idle:
                            if retained is not None and now >= retained:
                                with self.cond:
                                    expired = (
                                        self._retained_idle and self._retained_until is not None
                                        and now >= self._retained_until
                                        and self.process is attempt_process
                                        and not attempt_done.is_set()
                                    )
                                    if expired:
                                        self.aborted = True
                                        self.cond.notify_all()
                                if expired:
                                    try:
                                        attempt_process.kill()
                                    except Exception:
                                        pass
                                    return
                            with attempt_state_lock:
                                attempt_state["started_at"] = now
                                attempt_state["last_output_at"] = now
                            continue
                        with attempt_state_lock:
                            if attempt_state["timeout_reason"]:
                                return
                            bp_paused = attempt_state["bp_paused"]
                        if bp_paused:
                            # Reader-limited pause: Spotty is blocked on a full
                            # stdout pipe by design. The writer discovers an
                            # exit (EOF) on resume and the offset retry
                            # restarts Spotty, so never kill here.
                            if not exit_logged:
                                try:
                                    exited = attempt_process.poll() is not None
                                except Exception:
                                    exited = False
                                if exited:
                                    exit_logged = True
                                    log_msg(
                                        f"Spotty exited while backpressure-paused for "
                                        f"{self.track_id}; will restart on resume.",
                                        LOGDEBUG,
                                    )
                            continue
                        with attempt_state_lock:
                            if attempt_state["timeout_reason"]:
                                return
                            if attempt_state["bp_paused"]:
                                continue
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

                # Return available PCM without waiting for a full buffered read.
                read = getattr(process.stdout, "read1", None) or process.stdout.read
                pending = b""
                if resume_ref is not None:
                    partial = (attempt_start_byte - len(self.wav_header)) % 4
                    nominal = pcm_skip - partial
                    prefix = self._read_prefix(
                        process,
                        pcm_skip + _RESUME_SEARCH_AFTER_BYTES,
                        attempt_state,
                        attempt_state_lock,
                    )
                    match = _find_resume_offset(
                        resume_ref,
                        prefix,
                        nominal - _RESUME_SEARCH_BEFORE_BYTES,
                        nominal + _RESUME_SEARCH_AFTER_BYTES,
                    )
                    if match is None:
                        if self._ever_retained:
                            raise ValueError("Retained PCM resume has no content match")
                        log_msg(
                            f"Spotty resume for {self.track_id} at byte {attempt_start_byte}: "
                            f"no content match, using nominal skip {pcm_skip}.",
                            LOGWARNING,
                        )
                        cut = pcm_skip
                    else:
                        cut = match + partial
                        log_msg(
                            f"Spotty resume for {self.track_id} at byte {attempt_start_byte}: "
                            f"aligned skip {cut} (seek landed {cut - pcm_skip} bytes early).",
                            LOGDEBUG,
                        )
                    pending = prefix[cut:]
                elif pcm_skip > 0:
                    self._read_prefix(process, pcm_skip, attempt_state, attempt_state_lock)

                while not self.aborted and not self.error:
                    self._wait_for_room(attempt_state, attempt_state_lock)
                    if self.aborted or self.error:
                        break
                    if pending:
                        chunk, pending = pending, b""
                    else:
                        chunk = read(_STDOUT_READ_BYTES)
                    if not chunk:
                        break
                    if self.aborted:
                        break
                    with self.cond:
                        remaining = self.track_length - self.start_byte - self.written_bytes
                    chunk = chunk[:remaining]
                    if not chunk:
                        break
                    pcm_bytes_read += len(chunk)
                    with attempt_state_lock:
                        attempt_state["has_real_pcm"] = True
                        attempt_state["last_output_at"] = time.monotonic()
                    self._append(chunk, real_pcm=True)
                    if self.written_bytes >= self.track_length - self.start_byte:
                        break

                if process.poll() is None and not self.aborted:
                    if self.written_bytes >= self.track_length - self.start_byte:
                        process.kill()
                        stopped_at_declared_end = True
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
                    try:
                        process.wait(timeout=0.5)
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
                self.cleanup()
                return
            if self.error:
                with self.cond:
                    self.is_finished = True
                    self.cond.notify_all()
                return

            rc = getattr(process, "returncode", None) if process else -1
            if rc is None:
                try:
                    rc = process.wait(timeout=0.5)
                except Exception:
                    rc = -1
            if stopped_at_declared_end and attempt_exception is None:
                rc = 0
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
            terminal_cause = (
                _classify_spotty_failure(diagnostics) if retryable_finish or rc != 0 else None
            )
            if terminal_cause not in ("auth", "premium"):
                terminal_cause = None
            if self._ever_retained and isinstance(attempt_exception, ValueError):
                with self.cond:
                    self.error = True
                    self.failure_cause = "resume"
                    self.is_finished = True
                    self.cond.notify_all()
                return
            if diagnostics and (retryable_finish or rc != 0):
                log_msg(
                    f"Spotty diagnostic tail for {self.track_id}, attempt {attempt + 1}: "
                    f"{diagnostics}",
                    LOGWARNING,
                )
            if retryable_finish and failures and pcm_bytes_read >= _RETRY_BUDGET_RESET_BYTES:
                log_msg(
                    f"Spotty attempt {attempt + 1} for {self.track_id} delivered "
                    f"{pcm_bytes_read} PCM bytes; resetting retry budget.",
                    LOGDEBUG,
                )
                failures = 0
            if (
                retryable_finish
                and terminal_cause is None
                and failures < self._MAX_SESSION_RETRIES
            ):
                delay = self._RETRY_DELAYS[min(failures, len(self._RETRY_DELAYS) - 1)]
                failures += 1
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
                    f"Retry {failures}/{self._MAX_SESSION_RETRIES} after {delay}s.",
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

            if (
                terminal_cause is None and not timeout_reason and attempt_exception is None
                and pcm_bytes_read > 0 and rc == 0
                and 0 < remaining <= _SHORT_FINISH_PADDING_MAX_BYTES
            ):
                try:
                    while remaining > 0 and not self.aborted:
                        padding = bytes(min(remaining, _STDOUT_READ_BYTES))
                        self._append(padding)
                        remaining -= len(padding)
                except OSError:
                    pass

            # Normal finish or final retry failure.
            with self.cond:
                if not self.aborted:
                    remaining = max(0, (self.track_length - self.start_byte) - self.written_bytes)
                    if terminal_cause is not None:
                        self.failure_cause = terminal_cause
                        self.error = True
                        log_msg(
                            f"Spotty terminal {terminal_cause} failure; stopping retries.",
                            LOGWARNING,
                        )
                    elif (timeout_reason or attempt_exception is not None) and remaining > 0:
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
                if self.error and self.failure_cause is None:
                    self.failure_cause = "unknown"
                self.is_finished = True
                self.cond.notify_all()
                peak_mib = self._peak_buffer_bytes / (1024 * 1024)
                paused_total = self._bp_paused_total
                if not self.error:
                    log_msg(
                        f"Finished background download for {self.track_id} "
                        f"(peak buffer MiB={peak_mib:.1f}, paused_total={paused_total:.1f}s)"
                    )
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

        Never blocks (writer pacing is _wait_for_room's job).  Reclaims bytes
        every active consumer has already read, less _TRIM_RETAIN_BYTES of
        history, in batched frame-aligned slices to amortize the bytearray
        memmove.  The unread-tail cap is insurance
        for the no-reader case; backpressure keeps the buffer below it.
        """
        if self._file_store is not None:
            return
        safe_consumed_pos = (
            min(self._consumer_positions.values())
            if self._consumer_positions
            else self._consumed_pos
        ) - self._TRIM_RETAIN_BYTES
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
        with self._start_lock:
            if self._file_store is not None:
                self._file_store.close()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self.cond:
            self._buffer.clear()
            self._consumed_pos = 0
            self._trim_offset = 0
            self._consumer_positions.clear()
            self.real_pcm_bytes = 0
            self.first_real_pcm_monotonic = None
            self.last_progress_monotonic = None

    def wait_for_bytes(self, target_bytes: int, timeout: float = None, cancelled=None) -> bool:
        with self.cond:
            if not (cancelled and cancelled()):
                self._resume_for_target_locked(target_bytes)
            start_time = time.monotonic()
            while (
                self.written_bytes < target_bytes
                and not self.is_finished
                and not self.error
                and not self.aborted
                and not (cancelled and cancelled())
            ):
                if timeout:
                    elapsed = time.monotonic() - start_time
                    if elapsed >= timeout:
                        break
                    self.cond.wait(timeout - elapsed)
                else:
                    self.cond.wait(1.0)
            return (
                (self.written_bytes >= target_bytes or self.is_finished)
                and not (cancelled and cancelled())
            )


# With no reader attached the buffer holds at most HIGH (+ one stdout read)
# unread bytes plus the retained history and < one trim batch of consumed
# bytes, so the insurance cap
# must never be what bounds a paused prebuffer (it would drop its head and
# force a discard/restart when playback begins).
assert (
    _BP_LOW_BYTES < _BP_HIGH_BYTES
    and _BP_HIGH_BYTES
    + SpottyDownloader._TRIM_RETAIN_BYTES
    + SpottyDownloader._TRIM_BATCH_BYTES
    + _STDOUT_READ_BYTES
    < SpottyDownloader._MAX_UNREAD_TAIL_BYTES
), "backpressure high-water mark must stay below the unread-tail cap"


class SpottyCacheManager:
    _instances = {}
    _lock = threading.Lock()
    _closed = False
    _current_key = None
    _prebuffer_key = None
    _MAX_CACHE_BYTES = 64 * 1024 * 1024
    _MAX_FILE_BYTES = 256 * 1024 * 1024
    _file_bytes = 0
    _quota_lock = threading.Lock()

    @classmethod
    def _release_file(cls, capacity):
        with cls._quota_lock:
            cls._file_bytes -= capacity

    @classmethod
    def _open_store(cls, capacity):
        with cls._quota_lock:
            if capacity <= 0 or cls._file_bytes + capacity > cls._MAX_FILE_BYTES:
                return None
            cls._file_bytes += capacity
        try:
            return _PCMFile(capacity, cls._release_file)
        except BaseException:
            cls._release_file(capacity)
            raise

    @classmethod
    def enforce_memory_budget(cls):
        retired = []
        with cls._lock:
            total = sum(len(value._buffer) for value in cls._instances.values())
            candidates = sorted(cls._instances.items(), key=lambda pair: pair[1].created_monotonic)
            for key, candidate in candidates:
                with candidate.cond:
                    readers = bool(candidate._consumer_positions)
                    terminal = candidate.is_finished or candidate.error or candidate.aborted
                    size = len(candidate._buffer)
                owned = key in (cls._current_key, cls._prebuffer_key)
                if readers or owned:
                    continue
                if terminal or total > cls._MAX_CACHE_BYTES:
                    retired.append(candidate)
                    del cls._instances[key]
                    total -= size
        for candidate in retired:
            candidate.cleanup()

    @classmethod
    def get_or_start(
        cls, spotty: Spotty, track_id: str, duration_sec: float, start_byte: int,
        bitrate: str, norm: str, volume: int, wav_header: bytes, track_length: int,
        allow_abort_others: bool = True, request_byte=None, cancelled=None,
    ) -> SpottyDownloader:
        from spotty_audio_streamer import SpottyStreamSpec

        spec = SpottyStreamSpec(track_id, duration_sec, wav_header, track_length,
                                bitrate, norm, _clamp_volume(volume))
        identity = _account_identity(spotty)
        if identity is None:
            return None
        key = (track_id, start_byte)
        request_byte = start_byte if request_byte is None else request_byte
        acquisition_owner = object()
        while True:
            if cancelled and cancelled():
                return None
            with cls._lock:
                if cls._closed:
                    return None
                existing = cls._instances.get(key)
            compatible = (
                existing is not None and existing.matches(spec, identity)
                and existing.reusable()
                and existing.start_byte + existing._trim_offset <= request_byte
            )
            candidate = existing if compatible else SpottyDownloader(
                spotty, track_id, duration_sec, start_byte, bitrate, norm, volume,
                wav_header, track_length,
            )
            retired = []
            with cls._lock:
                if cls._closed:
                    return None
                if cls._instances.get(key) is not existing:
                    continue
                if cancelled and cancelled():
                    return None
                if not allow_abort_others:
                    if identity is None or key == cls._current_key or any(
                        other is not existing and not other.is_finished
                        and not other.aborted and not other.error
                        for other in cls._instances.values()
                    ):
                        return None
                if existing is not None and not compatible:
                    retired.append(existing)
                for other_key, other in list(cls._instances.items()):
                    if other_key == key:
                        continue
                    with other.cond:
                        readers = bool(other._consumer_positions)
                        active = (
                            not other.is_finished and not other.aborted
                            and other.written_bytes < other.track_length - other.start_byte
                        )
                    if allow_abort_others and (active or not readers):
                        retired.append(other)
                        del cls._instances[other_key]
                    elif not allow_abort_others and other_key != cls._current_key and not readers:
                        retired.append(other)
                        del cls._instances[other_key]
                cls._instances[key] = candidate
                if allow_abort_others:
                    cls._current_key = key
                    cls._prebuffer_key = None
                else:
                    cls._prebuffer_key = key
                candidate._acquisition_owner = acquisition_owner
                candidate._file_admission = identity is not None
                candidate._optional = not allow_abort_others
            for other in retired:
                other.cleanup()
            started = candidate.start()
            with cls._lock:
                closed = cls._closed
            cancelled_now = closed or bool(cancelled and cancelled())
            if started is False or cancelled_now:
                dispose = False
                with cls._lock:
                    if cls._instances.get(key) is candidate and (
                        candidate._acquisition_owner is acquisition_owner
                    ):
                        del cls._instances[key]
                        if cls._prebuffer_key == key:
                            cls._prebuffer_key = None
                        dispose = True
                if dispose:
                    candidate.cleanup()
                return None
            cls.enforce_memory_budget()
            return candidate

    @classmethod
    def find_active_downloader(cls, track_id: str, stream_spec=None, spotty=None):
        """Find healthy playback independently of the readable range."""
        identity = _account_identity(spotty) if spotty is not None else None
        with cls._lock:
            candidates = [value for key, value in cls._instances.items() if key[0] == track_id]
        best = None
        best_score = None
        for candidate in candidates:
            if stream_spec is not None and not candidate.matches(stream_spec, identity):
                continue
            if not candidate.reusable():
                continue
            with candidate.cond:
                score = (
                    bool(candidate._consumer_positions), candidate.real_pcm_bytes > 0,
                    not candidate.is_finished, candidate.start_byte,
                )
            if best_score is None or score > best_score:
                best, best_score = candidate, score
        return best

    @classmethod
    def find_best_downloader(cls, track_id: str, request_byte: int, stream_spec=None, spotty=None):
        identity = _account_identity(spotty) if spotty is not None else None
        with cls._lock:
            candidates = [value for key, value in cls._instances.items() if key[0] == track_id]
        best = None
        for candidate in candidates:
            if stream_spec is not None and not candidate.matches(stream_spec, identity):
                continue
            if not candidate.reusable():
                continue
            with candidate.cond:
                readable = candidate.start_byte + candidate._trim_offset <= request_byte
            if readable and (best is None or candidate.start_byte > best.start_byte):
                best = candidate
        return best

    @classmethod
    def retain_after_stop(cls, stream_spec):
        with cls._lock:
            candidates = list(cls._instances.items())
        retained = None
        for key, candidate in candidates:
            matches = (
                candidate.matches(stream_spec, _account_identity(candidate.spotty))
                if stream_spec is not None else candidate._ever_retained
            )
            if retained is None and matches and candidate.retain_after_stop():
                retained = candidate
        with cls._lock:
            retired = []
            for key, candidate in list(cls._instances.items()):
                if candidate is not retained:
                    retired.append(candidate)
                    del cls._instances[key]
            cls._current_key = (retained.track_id, retained.start_byte) if retained is not None else None
            cls._prebuffer_key = None
        for candidate in retired:
            candidate.cleanup()
        return retained is not None

    @classmethod
    def notify_readers(cls):
        with cls._lock:
            candidates = list(cls._instances.values())
        for candidate in candidates:
            candidate.notify_readers()

    @classmethod
    def get_failure_cause(cls, track_id: str) -> Optional[str]:
        """Read the latest track downloader failure without changing ownership."""
        with cls._lock:
            downloader = max(
                (value for key, value in cls._instances.items() if key[0] == track_id),
                key=lambda value: value.created_monotonic,
                default=None,
            )
            if downloader is not None:
                with downloader.cond:
                    if downloader.error:
                        return downloader.failure_cause or "unknown"
        return None

    @classmethod
    def cleanup_all(cls, shutdown=False):
        with cls._lock:
            if shutdown:
                cls._closed = True
            candidates = list(cls._instances.values())
            cls._instances.clear()
            cls._current_key = None
            cls._prebuffer_key = None
        for candidate in candidates:
            candidate.cleanup()
        return bool(candidates)
