import os
import sys
import threading
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
    sys.modules["xbmc"] = xbmc

    spotty = types.ModuleType("spotty")
    spotty.Spotty = object
    sys.modules["spotty"] = spotty

    utils = types.ModuleType("utils")
    utils.bytes_to_megabytes = lambda value: value / 1024 / 1024
    utils.kill_process_by_pid = lambda pid: None
    utils.log_msg = lambda *args, **kwargs: None
    utils.log_exception = lambda *args, **kwargs: None
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
        startup_silence_bytes=0,
    ):
        return cls.downloader


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

    def test_wav_header_declares_startup_silence(self):
        wav_header, total_length = self.module.create_wav_header_for_duration(180)

        self.assertEqual(44, len(wav_header))
        self.assertEqual(
            44 + 180 * self.module._PCM_BYTES_PER_SEC + self.module.STARTUP_SILENCE_BYTES,
            total_length,
        )

    def test_startup_silence_covers_pa_player_cold_decode_window(self):
        self.assertGreaterEqual(
            self.module.STARTUP_SILENCE_BYTES,
            self.module._PCM_BYTES_PER_SEC * 2,
        )

    def test_from_start_request_primes_real_pcm_before_first_chunk(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(wav_header, bytes(self.module.STARTUP_SILENCE_BYTES))
        FakeSpottyCacheManager.downloader = downloader

        generator = streamer.send_part_audio_stream(65536, 0)
        try:
            first_chunk = next(generator)
        finally:
            generator.close()

        self.assertGreaterEqual(len(first_chunk), len(wav_header))
        self.assertGreaterEqual(
            max(downloader.wait_targets),
            len(wav_header) + self.module.STARTUP_SILENCE_BYTES + 2097152,
        )

    def test_from_start_request_does_not_release_silence_only_after_downloader_error(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, _ = self.module.create_wav_header_for_duration(180)
        downloader = FakeDownloader(wav_header, bytes(self.module.STARTUP_SILENCE_BYTES))
        downloader.error = True
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader

        generator = streamer.send_part_audio_stream(
            len(wav_header) + self.module.STARTUP_SILENCE_BYTES,
            0,
        )
        try:
            with self.assertRaises(StopIteration):
                next(generator)
        finally:
            generator.close()

    def test_finished_short_downloader_does_not_pad_large_silent_tail(self):
        streamer = self.module.SpottyAudioStreamer(object())
        streamer.set_track("track-1", 180)
        wav_header, track_length = self.module.create_wav_header_for_duration(180)
        real_pcm = bytes(128 * 1024)
        downloader = FakeDownloader(
            wav_header,
            bytes(self.module.STARTUP_SILENCE_BYTES) + real_pcm,
            auto_fill=False,
        )
        downloader.is_finished = True
        FakeSpottyCacheManager.downloader = downloader

        range_begin = len(wav_header) + self.module.STARTUP_SILENCE_BYTES
        payload = b"".join(streamer.send_part_audio_stream(track_length - range_begin, range_begin))

        self.assertEqual(real_pcm, payload)

    def test_downloader_seeds_silence_and_maps_seek_offset_to_real_pcm(self):
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
            startup_silence_bytes=self.module.STARTUP_SILENCE_BYTES,
        )
        downloader._download_loop = lambda: None

        downloader.start()

        self.assertEqual(
            len(wav_header) + self.module.STARTUP_SILENCE_BYTES,
            downloader.written_bytes,
        )
        self.assertEqual(wav_header, bytes(downloader._buffer[: len(wav_header)]))
        self.assertEqual(
            bytes(self.module.STARTUP_SILENCE_BYTES),
            bytes(downloader._buffer[len(wav_header) :]),
        )

        seek_downloader = spotty_cache.SpottyDownloader(
            spotty=object(),
            track_id="track-1",
            duration_sec=180,
            start_byte=len(wav_header)
            + self.module.STARTUP_SILENCE_BYTES
            + self.module._PCM_BYTES_PER_SEC
            + 123,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=wav_header,
            track_length=track_length,
            startup_silence_bytes=self.module.STARTUP_SILENCE_BYTES,
        )

        args, pcm_skip = seek_downloader._build_args()

        self.assertIn("--start-position", args)
        self.assertEqual("1", args[args.index("--start-position") + 1])
        self.assertEqual(123, pcm_skip)

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
            startup_silence_bytes=0,
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
            startup_silence_bytes=0,
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
            startup_silence_bytes=0,
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
            startup_silence_bytes=0,
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
            startup_silence_bytes=0,
        )
        dl._download_loop = lambda: None
        dl.start()

        with dl.cond:
            dl._buffer.extend(bytes(2 * 1024 * 1024))
            dl.written_bytes += 2 * 1024 * 1024
            dl._consumed_pos = dl.written_bytes
            dl._trim_head_locked()

        self.assertGreater(dl._trim_offset, 0)

        mgr = spotty_cache.SpottyCacheManager
        mgr._instances[("t1", 0)] = dl
        mgr._recent_tracks = ["t1"]
        try:
            self.assertIsNone(mgr.find_best_downloader("t1", 0))
            self.assertIsNone(mgr.find_best_downloader("t1", 100))
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
                startup_silence_bytes=self.module.STARTUP_SILENCE_BYTES,
            )
            downloader.start()
            downloader.thread.join(timeout=2.0)

            self.assertTrue(downloader.error)
            self.assertLess(downloader.written_bytes, track_length)
        finally:
            spotty_cache.SpottyDownloader._MAX_SESSION_RETRIES = original_retries


if __name__ == "__main__":
    unittest.main()
