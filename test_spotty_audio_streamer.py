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
    def __init__(self, wav_header):
        self.start_byte = 0
        self._buffer = bytearray(wav_header)
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

    def test_from_start_request_waits_only_for_small_preroll(self):
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
        self.assertLessEqual(
            max(downloader.wait_targets),
            len(wav_header) + self.module._INITIAL_PCM_PREROLL_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
