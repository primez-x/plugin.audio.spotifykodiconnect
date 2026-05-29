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
    def __init__(self, wav_header, initial_pcm=b""):
        self.start_byte = 0
        self._buffer = bytearray(wav_header)
        self._buffer.extend(initial_pcm)
        self.written_bytes = len(self._buffer)
        self.is_finished = False
        self.error = False
        self.aborted = False
        self.cond = threading.Condition()
        self.wait_targets = []

    def wait_for_bytes(self, target_bytes, timeout=None):
        self.wait_targets.append(target_bytes)
        if self.written_bytes < target_bytes:
            self._buffer.extend(bytes(target_bytes - self.written_bytes))
            self.written_bytes = target_bytes
        return True


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

    def test_from_start_request_has_immediate_silent_preroll(self):
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
        self.assertEqual([1], downloader.wait_targets)

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


if __name__ == "__main__":
    unittest.main()
