import json
import importlib.util
import os
import subprocess
import sys
import threading
import time
import types
import unittest

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")


def install_stubs():
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmc.executeJSONRPC = lambda payload: '{"result": {"value": 65536}}'
    xbmc.executebuiltin = lambda *args, **kwargs: None
    sys.modules["xbmc"] = xbmc

    spotty = types.ModuleType("spotty")
    spotty.Spotty = object
    sys.modules["spotty"] = spotty

    utils = types.ModuleType("utils")
    utils.bytes_to_megabytes = lambda value: value / 1024 / 1024
    utils.kill_process_by_pid = lambda pid: None
    utils.log_msg = lambda *args, **kwargs: None
    utils.log_exception = lambda *args, **kwargs: None
    utils.ADDON_DATA_PATH = REPO_ROOT
    sys.modules["utils"] = utils


def import_streamer():
    install_stubs()
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    sys.modules.pop("spotty_audio_streamer", None)
    import spotty_audio_streamer

    return spotty_audio_streamer


class FakeDownloader:
    def __init__(self, wav_header, initial_pcm=b"", auto_fill=True):
        self.start_byte = 0
        self.wav_header = wav_header
        self._buffer = bytearray(wav_header)
        self._buffer.extend(initial_pcm)
        self.written_bytes = len(self._buffer)
        self.auto_fill = auto_fill
        self._consumed_pos = 0
        self._trim_offset = 0
        self._consumer_positions = {}
        self._next_consumer_id = 0
        self.is_finished = False
        self.error = False
        self.aborted = False
        self.cond = threading.Condition()
        self.wait_targets = []

    def wait_for_bytes(self, target_bytes, timeout=None):
        self.wait_targets.append(target_bytes)
        if self.auto_fill and self.written_bytes < target_bytes:
            self._buffer.extend(bytes(target_bytes - self.written_bytes))
            self.written_bytes = target_bytes
        return True

    def _trim_head_locked(self):
        return None

    def cleanup(self):
        self.aborted = True


class FakeStdout:
    def __init__(self, payload):
        self._payload = bytearray(payload)

    def read(self, size):
        if not self._payload:
            return b""
        chunk = bytes(self._payload[:size])
        del self._payload[:size]
        return chunk


class FakeProcess:
    def __init__(self, payload, returncode=0):
        self.stdout = FakeStdout(payload)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        return None


class BlockingStdout:
    def __init__(self):
        self.released = threading.Event()

    def read(self, size):
        self.released.wait(timeout=2.0)
        return b""


class ChunkThenBlockingStdout(BlockingStdout):
    def __init__(self, payload):
        super().__init__()
        self.payload = bytearray(payload)

    def read(self, size):
        if self.payload:
            chunk = bytes(self.payload[:size])
            del self.payload[:size]
            return chunk
        return super().read(size)


class DiagnosticStderr:
    def __init__(self, payload, max_chunk=None):
        self.payload = payload
        self.max_chunk = max_chunk

    def read1(self, size):
        if not self.payload:
            return b""
        if self.max_chunk is not None:
            size = min(size, self.max_chunk)
        chunk = self.payload[:size]
        self.payload = self.payload[size:]
        return chunk


class BlockingProcess:
    def __init__(self, diagnostic=b"", diagnostic_chunk_size=None):
        self.stdout = BlockingStdout()
        self.stderr = DiagnosticStderr(diagnostic, diagnostic_chunk_size)
        self.returncode = None
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.stdout.released.wait(timeout=timeout)
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self.stdout.released.set()


class ChunkThenBlockingProcess(BlockingProcess):
    def __init__(self, payload, diagnostic=b""):
        super().__init__(diagnostic)
        self.stdout = ChunkThenBlockingStdout(payload)


class CoordinatedKillProcess(BlockingProcess):
    def __init__(self):
        super().__init__()
        self.kill_started = threading.Event()
        self.allow_kill_return = threading.Event()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self.stdout.released.set()
        self.kill_started.set()
        self.allow_kill_return.wait(timeout=2.0)


class SequenceSpotty:
    def __init__(self, processes):
        self.processes = list(processes)
        self.calls = []

    def run_spotty(self, args):
        self.calls.append(list(args))
        return self.processes.pop(0)


class FakeSpotty:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def run_spotty(self, args):
        self.calls.append(list(args))
        payload = self.payloads.pop(0) if self.payloads else b""
        return FakeProcess(payload)


class FakeSpottyCacheManager:
    downloader = None
    calls = []

    @classmethod
    def find_best_downloader(cls, track_id, request_byte):
        return None

    @classmethod
    def get_or_start(
        cls,
        spotty,
        track_id,
        duration_sec,
        start_byte,
        bitrate,
        norm,
        volume,
        wav_header,
        track_length,
        **kwargs,
    ):
        cls.calls.append(
            {
                "track_id": track_id,
                "kwargs": dict(kwargs),
            }
        )
        return cls.downloader


class ManagedFakeDownloader:
    created = []

    def __init__(
        self,
        spotty,
        track_id,
        duration_sec,
        start_byte,
        bitrate,
        normalization,
        volume,
        wav_header,
        track_length,
    ):
        self.track_id = track_id
        self.start_byte = start_byte
        self.is_finished = False
        self.error = False
        self.aborted = False
        self.started = False
        self.cond = threading.Condition()
        self._consumer_positions = {}
        ManagedFakeDownloader.created.append(self)

    def start(self):
        self.started = True

    def abort(self):
        self.aborted = True

    def cleanup(self):
        self.abort()


class SpottyAudioStreamerTests(unittest.TestCase):
    def setUp(self):
        self.module = import_streamer()
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = FakeSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache

    def tearDown(self):
        for module_name in (
            "spotty_audio_streamer",
            "spotty_cache",
            "utils",
            "spotty",
            "xbmc",
        ):
            sys.modules.pop(module_name, None)

    def test_wav_header_declares_only_track_pcm(self):
        wav_header, total_length = self.module.create_wav_header_for_duration(180)

        self.assertEqual(44, len(wav_header))
        self.assertEqual(44 + 180 * self.module._PCM_BYTES_PER_SEC, total_length)
        self.assertEqual(
            180 * self.module._PCM_BYTES_PER_SEC,
            int.from_bytes(wav_header[40:44], "little"),
        )

    def test_streamer_does_not_expose_synthetic_startup_silence(self):
        self.assertFalse(hasattr(self.module, "STARTUP_SILENCE_BYTES"))

    def test_kodi_chunk_size_uses_filecache_setting_before_legacy_name(self):
        calls = []

        def execute_jsonrpc(payload):
            setting = json.loads(payload)["params"]["setting"]
            calls.append(setting)
            if setting == "filecache.chunksize":
                return '{"result": {"value": 262144}}'
            return '{"result": {"value": 65536}}'

        sys.modules["xbmc"].executeJSONRPC = execute_jsonrpc

        self.assertEqual(262144, self.module._get_kodi_chunk_size())
        self.assertEqual(["filecache.chunksize"], calls)

    def test_kodi_chunk_size_falls_back_to_legacy_setting_name(self):
        calls = []

        def execute_jsonrpc(payload):
            setting = json.loads(payload)["params"]["setting"]
            calls.append(setting)
            if setting == "filecache.chunksize":
                raise RuntimeError("setting unavailable")
            return '{"result": {"value": 131072}}'

        sys.modules["xbmc"].executeJSONRPC = execute_jsonrpc

        self.assertEqual(131072, self.module._get_kodi_chunk_size())
        self.assertEqual(["filecache.chunksize", "cache.chunksize"], calls)

    def test_spotty_pipes_stderr_only_for_single_track_downloads(self):
        spec = importlib.util.spec_from_file_location(
            "spotty_runtime_test", os.path.join(LIB_DIR, "spotty.py")
        )
        spotty_runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(spotty_runtime)
        calls = []
        original_popen = spotty_runtime.subprocess.Popen
        spotty_runtime.subprocess.Popen = lambda args, **kwargs: calls.append(kwargs) or object()
        try:
            spotty = spotty_runtime.Spotty()
            spotty.set_spotty_path("spotty")
            spotty.run_spotty(["--zeroconf-port", "10001"])
            spotty.run_spotty(["--single-track", "spotify:track:test"])
        finally:
            spotty_runtime.subprocess.Popen = original_popen

        self.assertIs(subprocess.DEVNULL, calls[0]["stderr"])
        self.assertIs(subprocess.PIPE, calls[1]["stderr"])

    def test_from_start_request_primes_real_pcm_before_first_chunk(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(wav_header)
        FakeSpottyCacheManager.downloader = downloader

        generator = streamer.send_part_audio_stream(65536, 0)
        try:
            first_chunk = next(generator)
        finally:
            generator.close()

        self.assertGreaterEqual(len(first_chunk), len(wav_header))
        self.assertGreaterEqual(
            max(downloader.wait_targets),
            len(wav_header) + 2097152,
        )

    def test_delayed_generator_keeps_original_track_spec_during_natural_handoff(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("first-track", 180)
        first_header, _ = self.module.create_wav_header_for_duration(180)
        FakeSpottyCacheManager.downloader = FakeDownloader(first_header)
        FakeSpottyCacheManager.calls = []

        generator = streamer.send_part_audio_stream(65536, 0)
        streamer.set_track("second-track", 200)
        try:
            self.assertTrue(next(generator))
        finally:
            generator.close()

        self.assertEqual("first-track", FakeSpottyCacheManager.calls[-1]["track_id"])

    def test_delayed_generator_stops_before_cache_after_termination(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("first-track", 180)
        first_header, _ = self.module.create_wav_header_for_duration(180)
        FakeSpottyCacheManager.downloader = FakeDownloader(first_header)
        FakeSpottyCacheManager.calls = []

        generator = streamer.send_part_audio_stream(65536, 0)
        streamer.terminate_stream()
        streamer.set_track("second-track", 200)
        try:
            with self.assertRaises(StopIteration):
                next(generator)
        finally:
            generator.close()

        self.assertEqual([], FakeSpottyCacheManager.calls)

    def test_from_start_request_does_not_release_header_only_after_downloader_error(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(wav_header)
        downloader.error = True
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader

        generator = streamer.send_part_audio_stream(
            len(wav_header),
            0,
        )
        try:
            with self.assertRaises(StopIteration):
                next(generator)
        finally:
            generator.close()

    def test_prepare_from_start_rejects_downloader_error_before_real_pcm(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(wav_header)
        downloader.error = True
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader

        self.assertFalse(streamer.prepare_part_audio_stream(0))
        self.assertTrue(downloader.aborted)

    def test_prepare_from_start_rejects_finished_downloader_before_real_pcm(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(
            wav_header,
            auto_fill=False,
        )
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader

        self.assertFalse(streamer.prepare_part_audio_stream(0))
        self.assertTrue(downloader.aborted)

    def test_prepare_from_start_waits_after_prior_termination(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        streamer.terminate_stream()
        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(wav_header, auto_fill=True)
        FakeSpottyCacheManager.downloader = downloader

        self.assertTrue(streamer.prepare_part_audio_stream(0))
        self.assertTrue(downloader.wait_targets)

    def test_prepare_timeout_rejects_live_header_only_downloader(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(
            wav_header,
            auto_fill=False,
        )
        FakeSpottyCacheManager.downloader = downloader
        original_timeout = self.module.STARTUP_REAL_PCM_WAIT_SECONDS
        self.module.STARTUP_REAL_PCM_WAIT_SECONDS = 0.01
        try:
            self.assertFalse(streamer.prepare_part_audio_stream(0))
        finally:
            self.module.STARTUP_REAL_PCM_WAIT_SECONDS = original_timeout

        self.assertTrue(downloader.aborted)

    def test_early_pcm_range_rejects_live_header_only_downloader(self):
        original_timeout = self.module.STARTUP_REAL_PCM_WAIT_SECONDS
        self.module.STARTUP_REAL_PCM_WAIT_SECONDS = 0.01
        try:
            for range_begin in (45, 256):
                with self.subTest(range_begin=range_begin):
                    streamer = self.module.SpottyAudioStreamer(object())
                    streamer.set_track("track-1", 180)
                    wav_header, _ = self.module.create_wav_header_for_duration(180)
                    downloader = FakeDownloader(
                        wav_header,
                        auto_fill=False,
                    )
                    FakeSpottyCacheManager.downloader = downloader

                    self.assertFalse(streamer.prepare_part_audio_stream(range_begin))
                    self.assertTrue(downloader.aborted)
        finally:
            self.module.STARTUP_REAL_PCM_WAIT_SECONDS = original_timeout

    def test_pcm_boundary_requires_real_pcm(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("water-flow", 224)
        wav_header, _ = self.module.create_wav_header_for_duration(224)
        downloader = FakeDownloader(
            wav_header,
            auto_fill=False,
        )
        downloader.error = True
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader
        live_boundary = len(wav_header)

        self.assertEqual(44, live_boundary)
        self.assertFalse(streamer.prepare_part_audio_stream(live_boundary))
        self.assertTrue(downloader.aborted)

    def test_finished_short_downloader_does_not_pad_large_silent_tail(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        real_pcm = bytes(128 * 1024)
        downloader = FakeDownloader(
            wav_header,
            real_pcm,
            auto_fill=False,
        )
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader

        range_begin = len(wav_header)
        payload = b"".join(streamer.send_part_audio_stream(track_length - range_begin, range_begin))

        self.assertEqual(real_pcm, payload)

    def test_seek_range_to_end_does_not_mark_track_finished(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        finished = []
        streamer.set_notify_track_finished(lambda track_id: finished.append(track_id))
        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        range_begin = track_length - 65536
        downloader = FakeDownloader(b"", bytes(65536), auto_fill=False)
        downloader.start_byte = range_begin
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader

        payload = b"".join(streamer.send_part_audio_stream(65536, range_begin))

        self.assertEqual(65536, len(payload))
        self.assertEqual([], finished)

    def test_downloader_seeds_only_wav_header(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        downloader = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="track-1",
            duration_sec=180,
            start_byte=0,
            bitrate="320",
            normalization="auto",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        downloader._download_loop = lambda: None

        downloader.start()

        self.assertEqual(len(wav_header), downloader.written_bytes)
        self.assertEqual(wav_header, bytes(downloader._buffer))

        pcm_downloader = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="track-1",
            duration_sec=180,
            start_byte=len(wav_header),
            bitrate="320",
            normalization="auto",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        pcm_downloader._download_loop = lambda: None
        pcm_downloader.start()

        self.assertEqual(0, pcm_downloader.written_bytes)
        self.assertEqual(b"", bytes(pcm_downloader._buffer))

    def test_downloader_maps_http_ranges_directly_to_pcm_offsets(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        cases = (
            (0, None, 0),
            (len(wav_header), None, 0),
            (len(wav_header) + self.module._PCM_BYTES_PER_SEC + 123, "1", 123),
            # This was the end of the removed two-second prefix. It is now an
            # ordinary byte range pointing exactly two seconds into real PCM.
            (352844, "2", 0),
        )

        for range_begin, expected_start_position, expected_skip in cases:
            with self.subTest(range_begin=range_begin):
                downloader = spotty_cache.SpottyDownloader(
                    spotty=object(),
                    track_id="track-1",
                    duration_sec=180,
                    start_byte=range_begin,
                    bitrate="320",
                    normalization="off",
                    volume=35,
                    wav_header=wav_header,
                    track_length=track_length,
                )

                args, pcm_skip = downloader._build_args()

                if expected_start_position is None:
                    self.assertNotIn("--start-position", args)
                else:
                    self.assertEqual(
                        expected_start_position,
                        args[args.index("--start-position") + 1],
                    )
                self.assertEqual(expected_skip, pcm_skip)

    def test_trim_head_reclaims_consumed_bytes_without_blocking(self):
        """_trim_head_locked releases consumed bytes; writer never stalls."""
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        dl = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="t1",
            duration_sec=180,
            start_byte=0,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        dl._download_loop = lambda: None
        dl.start()

        pcm = bytes(2 * 1024 * 1024)
        with dl.cond:
            dl._buffer.extend(pcm)
            dl.written_bytes += len(pcm)

        full_len = len(dl._buffer)
        self.assertGreater(full_len, 2 * 1024 * 1024)

        with dl.cond:
            dl._consumed_pos = dl.written_bytes
            dl._trim_head_locked()

        self.assertEqual(dl._trim_offset, dl.written_bytes)
        self.assertLess(len(dl._buffer), dl._TRIM_BATCH_BYTES)

    def test_trim_head_preserves_unconsumed_bytes(self):
        """_trim_head_locked must not drop bytes the consumer hasn't read."""
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        dl = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="t1",
            duration_sec=180,
            start_byte=0,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        dl._download_loop = lambda: None
        dl.start()

        pcm = bytes(3 * 1024 * 1024)
        with dl.cond:
            dl._buffer.extend(pcm)
            dl.written_bytes += len(pcm)
            dl._trim_head_locked()

        self.assertEqual(dl._trim_offset, 0)
        self.assertEqual(len(dl._buffer), len(wav_header) + 3 * 1024 * 1024)

    def test_trim_head_uses_slowest_active_consumer(self):
        """One fast range reader must not trim bytes a slower reader still needs."""
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        dl = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="t1",
            duration_sec=180,
            start_byte=0,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        dl._download_loop = lambda: None
        dl.start()

        slow_reader_pos = 1536 * 1024
        fast_reader_pos = 3 * 1024 * 1024
        with dl.cond:
            dl._buffer.extend(bytes(4 * 1024 * 1024))
            dl.written_bytes += 4 * 1024 * 1024
            dl._consumer_positions = {1: slow_reader_pos, 2: fast_reader_pos}
            dl._consumed_pos = fast_reader_pos
            dl._trim_head_locked()

        self.assertEqual(slow_reader_pos, dl._trim_offset)

    def test_trim_head_does_not_overflow_drop_active_reader_bytes(self):
        """The unread-tail cap must not discard bytes from an active reader."""
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        dl = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="t1",
            duration_sec=180,
            start_byte=0,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        dl._download_loop = lambda: None
        dl.start()

        with dl.cond:
            dl._buffer.extend(bytes(dl._MAX_UNREAD_TAIL_BYTES + 1024 * 1024))
            dl.written_bytes += dl._MAX_UNREAD_TAIL_BYTES + 1024 * 1024
            before = len(dl._buffer)
            dl._consumer_positions = {1: 0}
            dl._trim_head_locked()

        self.assertEqual(0, dl._trim_offset)
        self.assertEqual(before, len(dl._buffer))

    def test_find_best_downloader_skips_trimmed_positions(self):
        """A downloader that has trimmed past request_byte is not selected."""
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        dl = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="t1",
            duration_sec=180,
            start_byte=0,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        dl._download_loop = lambda: None
        dl.start()

        with dl.cond:
            dl._buffer.extend(bytes(2 * 1024 * 1024))
            dl.written_bytes += 2 * 1024 * 1024
            dl.real_pcm_bytes = 2 * 1024 * 1024
            dl.last_progress_monotonic = time.monotonic()
            dl._consumed_pos = dl.written_bytes
            dl._trim_head_locked()

        self.assertGreater(dl._trim_offset, 0)

        mgr = spotty_cache.SpottyCacheManager
        mgr._instances[("t1", 0)] = dl
        mgr._recent_tracks = ["t1"]
        try:
            self.assertIsNone(mgr.find_best_downloader("t1", 0))
            self.assertIsNone(mgr.find_best_downloader("t1", 100))
            self.assertIs(dl, mgr.find_active_downloader("t1"))
        finally:
            mgr._instances.clear()
            mgr._recent_tracks.clear()

    def test_downloader_marks_large_clean_short_finish_as_error(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        original_retries = spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES
        spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES = 0
        try:
            downloader = spotty_cache.SpottyDownloader(
                spotty=FakeSpotty([bytes(128 * 1024)]),
                track_id="t1",
                duration_sec=180,
                start_byte=0,
                bitrate="320",
                normalization="off",
                volume=35,
                wav_header=wav_header,
                track_length=track_length,
            )
            downloader.start()
            downloader.thread.join(timeout=2.0)

            self.assertTrue(downloader.error)
            self.assertLess(downloader.written_bytes, track_length)
        finally:
            spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES = original_retries

    def test_watchdog_kills_blocking_stdout_and_retry_recovers_with_real_pcm(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(1)
        blocking = BlockingProcess(
            b'Authorization: Bearer super-secret\n{"access_token":"json-secret"}\n'
            b"Authorization: Basic YmFzaWMtc2VjcmV0\n"
            b"auth_data: [97, 117, 116, 104, 45, 115, 101, 99, 114, 101, 116]\n"
            b"Cookie: sp_dc=cookie-secret\n"
            b"cdn stalled before audio\n",
            diagnostic_chunk_size=5,
        )
        recovered = FakeProcess(bytes(spotty_cache.PCM_BYTES_PER_SEC))
        spotty = SequenceSpotty([blocking, recovered])
        logs = []
        original_log = spotty_cache.log_msg
        original_first_timeout = spotty_cache._FIRST_REAL_PCM_TIMEOUT_SECONDS
        original_poll = spotty_cache._WATCHDOG_POLL_SECONDS
        original_delays = spotty_cache.SpottyDownloader._RETRY_DELAYS
        spotty_cache.log_msg = lambda message, *args, **kwargs: logs.append(str(message))
        spotty_cache._FIRST_REAL_PCM_TIMEOUT_SECONDS = 0.03
        spotty_cache._WATCHDOG_POLL_SECONDS = 0.005
        spotty_cache.SpottyDownloader._RETRY_DELAYS = [0.0, 0.0, 0.0]
        try:
            downloader = spotty_cache.SpottyDownloader(
                spotty=spotty,
                track_id="watchdog-track",
                duration_sec=1,
                start_byte=0,
                bitrate="320",
                normalization="off",
                volume=35,
                wav_header=wav_header,
                track_length=track_length,
            )
            downloader.start()
            downloader.thread.join(timeout=1.0)

            self.assertFalse(downloader.thread.is_alive())
            self.assertTrue(downloader.is_finished)
            self.assertFalse(downloader.error)
            self.assertTrue(downloader.has_real_pcm())
            self.assertTrue(downloader.has_recent_progress(1.0))
            self.assertEqual(spotty_cache.PCM_BYTES_PER_SEC, downloader.real_pcm_bytes)
            self.assertIsNotNone(downloader.first_real_pcm_monotonic)
            self.assertIsNotNone(downloader.last_progress_monotonic)
            self.assertGreaterEqual(blocking.kill_calls, 1)
            self.assertEqual(2, len(spotty.calls))
            joined_logs = "\n".join(logs)
            self.assertIn("cdn stalled before audio", joined_logs)
            self.assertIn("<redacted>", joined_logs)
            self.assertNotIn("super-secret", joined_logs)
            self.assertNotIn("json-secret", joined_logs)
            self.assertNotIn("YmFzaWMtc2VjcmV0", joined_logs)
            self.assertNotIn("97, 117, 116", joined_logs)
            self.assertNotIn("cookie-secret", joined_logs)
        finally:
            spotty_cache.log_msg = original_log
            spotty_cache._FIRST_REAL_PCM_TIMEOUT_SECONDS = original_first_timeout
            spotty_cache._WATCHDOG_POLL_SECONDS = original_poll
            spotty_cache.SpottyDownloader._RETRY_DELAYS = original_delays

    def test_abort_interrupts_blocking_stdout_promptly(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        blocking = BlockingProcess()
        downloader = spotty_cache.SpottyDownloader(
            spotty=SequenceSpotty([blocking]),
            track_id="abort-track",
            duration_sec=180,
            start_byte=0,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        downloader.start()
        deadline = time.monotonic() + 0.5
        while downloader.process is None and time.monotonic() < deadline:
            time.sleep(0.005)

        started = time.monotonic()
        downloader.abort()
        downloader.thread.join(timeout=0.5)

        self.assertFalse(downloader.thread.is_alive())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertGreaterEqual(blocking.kill_calls, 1)

    def test_watchdog_kills_attempt_that_stalls_after_real_pcm(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, _ = self.module.create_wav_header_for_duration(1)
        real_pcm_length = 200
        track_length = len(wav_header) + real_pcm_length
        original_retries = spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES
        original_no_progress_timeout = spotty_cache._NO_PROGRESS_TIMEOUT_SECONDS
        original_poll = spotty_cache._WATCHDOG_POLL_SECONDS
        original_delays = spotty_cache.SpottyDownloader._RETRY_DELAYS
        spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES = 1
        spotty_cache.SpottyDownloader._RETRY_DELAYS = [0.0]
        spotty_cache._NO_PROGRESS_TIMEOUT_SECONDS = 0.03
        spotty_cache._WATCHDOG_POLL_SECONDS = 0.005
        try:
            for range_begin in (0, len(wav_header)):
                with self.subTest(range_begin=range_begin):
                    first_chunk = b"a" * 100
                    blocking = ChunkThenBlockingProcess(first_chunk)
                    # The retry starts Spotty at the beginning of the requested
                    # PCM and discards exactly the 100 bytes already buffered.
                    recovered = FakeProcess(first_chunk + b"b" * 100)
                    spotty = SequenceSpotty([blocking, recovered])
                    downloader = spotty_cache.SpottyDownloader(
                        spotty=spotty,
                        track_id="midstream-stall-track",
                        duration_sec=1,
                        start_byte=range_begin,
                        bitrate="320",
                        normalization="off",
                        volume=35,
                        wav_header=wav_header,
                        track_length=track_length,
                    )
                    downloader.start()
                    downloader.thread.join(timeout=1.0)

                    self.assertFalse(downloader.thread.is_alive())
                    self.assertTrue(downloader.is_finished)
                    self.assertFalse(downloader.error)
                    self.assertEqual(2, len(spotty.calls))
                    self.assertEqual(real_pcm_length, downloader.real_pcm_bytes)
                    self.assertEqual(track_length - range_begin, downloader.written_bytes)
                    expected_prefix = wav_header if range_begin == 0 else b""
                    self.assertEqual(
                        expected_prefix + first_chunk + b"b" * 100,
                        bytes(downloader._buffer),
                    )
                    self.assertGreaterEqual(blocking.kill_calls, 1)
        finally:
            spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES = original_retries
            spotty_cache.SpottyDownloader._RETRY_DELAYS = original_delays
            spotty_cache._NO_PROGRESS_TIMEOUT_SECONDS = original_no_progress_timeout
            spotty_cache._WATCHDOG_POLL_SECONDS = original_poll

    def test_retry_waits_for_old_watchdog_before_starting_new_process(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(1)
        blocking = CoordinatedKillProcess()
        recovered = FakeProcess(bytes(spotty_cache.PCM_BYTES_PER_SEC))
        spotty = SequenceSpotty([blocking, recovered])
        original_retries = spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES
        original_first_timeout = spotty_cache._FIRST_REAL_PCM_TIMEOUT_SECONDS
        original_poll = spotty_cache._WATCHDOG_POLL_SECONDS
        original_delays = spotty_cache.SpottyDownloader._RETRY_DELAYS
        spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES = 1
        spotty_cache._FIRST_REAL_PCM_TIMEOUT_SECONDS = 0.03
        spotty_cache._WATCHDOG_POLL_SECONDS = 0.005
        spotty_cache.SpottyDownloader._RETRY_DELAYS = [0.0]
        downloader = None
        try:
            downloader = spotty_cache.SpottyDownloader(
                spotty=spotty,
                track_id="watchdog-ownership-track",
                duration_sec=1,
                start_byte=0,
                bitrate="320",
                normalization="off",
                volume=35,
                wav_header=wav_header,
                track_length=track_length,
            )
            downloader.start()
            self.assertTrue(blocking.kill_started.wait(timeout=0.5))

            # The former timed join allowed this old watchdog to survive and a
            # retry to start while it still had kill work pending.
            time.sleep(0.55)
            self.assertEqual(1, len(spotty.calls))
        finally:
            blocking.allow_kill_return.set()
            if downloader is not None:
                downloader.thread.join(timeout=1.0)
            spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES = original_retries
            spotty_cache._FIRST_REAL_PCM_TIMEOUT_SECONDS = original_first_timeout
            spotty_cache._WATCHDOG_POLL_SECONDS = original_poll
            spotty_cache.SpottyDownloader._RETRY_DELAYS = original_delays

        self.assertFalse(downloader.thread.is_alive())
        self.assertEqual(2, len(spotty.calls))
        self.assertFalse(downloader.error)

    def test_retry_budget_leaves_recovery_window_inside_preflight_deadline(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        elapsed_before_third_attempt = (
            spotty_cache._FIRST_REAL_PCM_TIMEOUT_SECONDS
            + spotty_cache.SpottyDownloader._RETRY_DELAYS[0]
            + spotty_cache._FIRST_REAL_PCM_TIMEOUT_SECONDS
            + spotty_cache.SpottyDownloader._RETRY_DELAYS[1]
        )
        remaining = self.module.STARTUP_REAL_PCM_WAIT_SECONDS - elapsed_before_third_attempt

        self.assertGreaterEqual(remaining, 3.0)

    def test_cache_start_without_abort_refuses_to_interrupt_active_downloader(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        original_downloader_class = spotty_cache.SpottyDownloader
        spotty_cache.SpottyDownloader = ManagedFakeDownloader
        ManagedFakeDownloader.created = []
        manager = spotty_cache.SpottyCacheManager
        manager._instances.clear()
        manager._recent_tracks.clear()
        try:
            active = ManagedFakeDownloader(
                spotty=object(),
                track_id="current-track",
                duration_sec=180,
                start_byte=0,
                bitrate="320",
                normalization="off",
                volume=35,
                wav_header=b"0" * 44,
                track_length=1000,
            )
            manager._instances[("current-track", 0)] = active
            manager._recent_tracks = ["current-track"]

            result = manager.get_or_start(
                object(),
                "next-track",
                180,
                0,
                "320",
                "off",
                35,
                b"0" * 44,
                1000,
                allow_abort_others=False,
            )

            self.assertIsNone(result)
            self.assertFalse(active.aborted)
            self.assertEqual(1, len(ManagedFakeDownloader.created))
            self.assertNotIn(("next-track", 0), manager._instances)
        finally:
            manager._instances.clear()
            manager._recent_tracks.clear()
            spotty_cache.SpottyDownloader = original_downloader_class

    def test_cache_restarts_downloader_when_requested_start_was_trimmed(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        original_downloader_class = spotty_cache.SpottyDownloader
        spotty_cache.SpottyDownloader = ManagedFakeDownloader
        ManagedFakeDownloader.created = []
        manager = spotty_cache.SpottyCacheManager
        manager._instances.clear()
        manager._recent_tracks.clear()
        try:
            old = ManagedFakeDownloader(
                spotty=object(),
                track_id="trimmed-track",
                duration_sec=180,
                start_byte=0,
                bitrate="320",
                normalization="off",
                volume=35,
                wav_header=b"0" * 44,
                track_length=1000,
            )
            old._trim_offset = 512
            manager._instances[("trimmed-track", 0)] = old
            manager._recent_tracks = ["trimmed-track"]

            result = manager.get_or_start(
                object(),
                "trimmed-track",
                180,
                0,
                "320",
                "off",
                35,
                b"0" * 44,
                1000,
            )

            self.assertIsNot(result, old)
            self.assertTrue(old.aborted)
            self.assertIs(result, manager._instances[("trimmed-track", 0)])
            self.assertTrue(result.started)
        finally:
            manager._instances.clear()
            manager._recent_tracks.clear()
            spotty_cache.SpottyDownloader = original_downloader_class

    def test_cache_eviction_keeps_downloader_with_active_consumers(self):
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        original_downloader_class = spotty_cache.SpottyDownloader
        spotty_cache.SpottyDownloader = ManagedFakeDownloader
        ManagedFakeDownloader.created = []
        manager = spotty_cache.SpottyCacheManager
        manager._instances.clear()
        manager._recent_tracks.clear()
        try:

            def make_finished(track_id):
                downloader = ManagedFakeDownloader(
                    spotty=object(),
                    track_id=track_id,
                    duration_sec=180,
                    start_byte=0,
                    bitrate="320",
                    normalization="off",
                    volume=35,
                    wav_header=b"0" * 44,
                    track_length=1000,
                )
                downloader.is_finished = True
                return downloader

            active_reader = make_finished("active-reader")
            with active_reader.cond:
                active_reader._consumer_positions[1] = 0

            manager._instances[("active-reader", 0)] = active_reader
            manager._instances[("stale-1", 0)] = make_finished("stale-1")
            manager._instances[("stale-2", 0)] = make_finished("stale-2")
            manager._instances[("stale-3", 0)] = make_finished("stale-3")
            manager._recent_tracks = ["active-reader", "stale-1", "stale-2", "stale-3"]

            manager.get_or_start(
                object(),
                "new-track",
                180,
                0,
                "320",
                "off",
                35,
                b"0" * 44,
                1000,
            )

            self.assertIn(("active-reader", 0), manager._instances)
            self.assertIn(("new-track", 0), manager._instances)
            self.assertNotIn(("stale-1", 0), manager._instances)
            self.assertFalse(active_reader.aborted)
        finally:
            manager._instances.clear()
            manager._recent_tracks.clear()
            spotty_cache.SpottyDownloader = original_downloader_class

    def test_prebuffer_starts_cache_without_permission_to_abort_active_downloads(self):
        FakeSpottyCacheManager.calls = []
        FakeSpottyCacheManager.downloader = object()
        sys.modules.pop("prebuffer", None)
        import prebuffer

        prebuffer.SpottyCacheManager = FakeSpottyCacheManager
        manager = prebuffer.PrebufferManager(object())

        result = manager.start_prebuffer("next-track", 180)

        self.assertIs(result, FakeSpottyCacheManager.downloader)
        self.assertEqual(1, len(FakeSpottyCacheManager.calls))
        self.assertIs(False, FakeSpottyCacheManager.calls[0]["kwargs"]["allow_abort_others"])

    def test_refused_prebuffer_does_not_report_later_cache_hit(self):
        FakeSpottyCacheManager.calls = []
        FakeSpottyCacheManager.downloader = None
        sys.modules.pop("prebuffer", None)
        import prebuffer

        prebuffer.SpottyCacheManager = FakeSpottyCacheManager
        manager = prebuffer.PrebufferManager(object())

        result = manager.start_prebuffer("next-track", 180)
        prebuffer_result, has_prebuffer = manager.get_and_clear_prebuffer("next-track")

        self.assertIsNone(result)
        self.assertIsNone(prebuffer_result)
        self.assertIs(False, has_prebuffer)

    def test_empty_prebuffer_does_not_report_cache_hit(self):
        sys.modules.pop("prebuffer", None)
        import prebuffer

        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(wav_header)
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader
        prebuffer.SpottyCacheManager = FakeSpottyCacheManager
        manager = prebuffer.PrebufferManager(object())

        manager.start_prebuffer("next-track", 180)
        prebuffer_result, has_prebuffer = manager.get_and_clear_prebuffer("next-track")

        self.assertIsNone(prebuffer_result)
        self.assertIs(False, has_prebuffer)
        self.assertTrue(downloader.aborted)

    def test_prebuffer_with_real_pcm_reports_cache_hit(self):
        sys.modules.pop("prebuffer", None)
        import prebuffer

        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(wav_header, b"real-pcm")
        downloader.real_pcm_bytes = len(b"real-pcm")
        FakeSpottyCacheManager.downloader = downloader
        prebuffer.SpottyCacheManager = FakeSpottyCacheManager
        manager = prebuffer.PrebufferManager(object())

        manager.start_prebuffer("next-track", 180)
        prebuffer_result, has_prebuffer = manager.get_and_clear_prebuffer("next-track")

        self.assertIsNotNone(prebuffer_result)
        self.assertIs(True, has_prebuffer)
        self.assertFalse(downloader.aborted)

    def test_prebuffer_checks_real_downloader_without_relocking_condition(self):
        sys.modules.pop("prebuffer", None)
        sys.modules.pop("spotty_cache", None)
        import prebuffer
        import spotty_cache

        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        downloader = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="next-track",
            duration_sec=180,
            start_byte=0,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
        )
        with downloader.cond:
            downloader.real_pcm_bytes = 1

        self.assertTrue(prebuffer.PrebufferManager._has_real_pcm(downloader))


if __name__ == "__main__":
    unittest.main()
