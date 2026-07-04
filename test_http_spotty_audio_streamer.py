import os
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")


class FakeBottleState:
    request = None
    response = None


class FakeRequest:
    def __init__(self, method="GET", headers=None):
        self.method = method
        self.headers = headers or {}


class FakeResponse:
    def __init__(self):
        self.status = 200
        self.content_type = ""
        self.content_length = 0
        self.headers = {}


class FakeAddon:
    def getSetting(self, key):
        if key == "spotify_bitrate":
            return "320"
        if key == "spotify_normalization":
            return "auto"
        return ""


class FakeSpottyAudioStreamer:
    def __init__(self, spotty):
        self.normalization_gain_type = "auto"
        self.use_autoplay = False
        self.bitrate = "320"
        self.track_id = ""
        self.duration = 0
        self.terminate_calls = 0
        self.set_track_calls = []

    def set_notify_track_finished(self, func):
        self.notify_track_finished = func

    def terminate_stream(self):
        self.terminate_calls += 1
        return True

    def set_track(self, track_id, duration):
        self.track_id = track_id
        self.duration = duration
        self.set_track_calls.append((track_id, duration))

    def get_track_length(self):
        return 44 + int(max(1, self.duration or 1) * 176400)

    def send_part_audio_stream(self, range_len, range_begin):
        yield b"x" * min(range_len, 16)


def install_stubs():
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    xbmc.getInfoLabel = lambda label: ""
    sys.modules["xbmc"] = xbmc

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda id=None: FakeAddon()
    sys.modules["xbmcaddon"] = xbmcaddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = lambda window_id=None: None
    sys.modules["xbmcgui"] = xbmcgui

    spotipy = types.ModuleType("spotipy")
    spotipy.Spotify = lambda auth=None: None
    sys.modules["spotipy"] = spotipy

    spotty = types.ModuleType("spotty")
    spotty.Spotty = object
    sys.modules["spotty"] = spotty

    utils = types.ModuleType("utils")
    utils.ADDON_ID = "plugin.audio.spotifykodiconnect"
    utils.ADDON_WINDOW_ID = 10000
    utils.LOGDEBUG = 0
    utils.get_cached_auth_token = lambda: None
    utils.log_msg = lambda *args, **kwargs: None
    sys.modules["utils"] = utils

    bottle = types.ModuleType("bottle")
    FakeBottleState.request = FakeRequest()
    FakeBottleState.response = FakeResponse()
    bottle.request = FakeBottleState.request
    bottle.response = FakeBottleState.response
    bottle.Response = object
    sys.modules["bottle"] = bottle

    spotty_audio_streamer = types.ModuleType("spotty_audio_streamer")
    spotty_audio_streamer.SpottyAudioStreamer = FakeSpottyAudioStreamer
    spotty_audio_streamer.create_wav_header_for_duration = lambda duration: (
        b"0" * 44,
        44 + int(float(duration) * 176400),
    )
    sys.modules["spotty_audio_streamer"] = spotty_audio_streamer


class ImmediateThread:
    def __init__(self, target, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


class ActiveDownloader:
    def __init__(self):
        self.is_finished = False
        self.error = False
        self.aborted = False
        self.cond = threading.Condition()


class FakeSpottyCacheManager:
    active_downloader = ActiveDownloader()

    @classmethod
    def find_best_downloader(cls, track_id, request_byte):
        if track_id == "current-track":
            return cls.active_downloader
        return None


def import_http_streamer():
    install_stubs()
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    sys.modules.pop("http_spotty_audio_streamer", None)
    import http_spotty_audio_streamer

    http_spotty_audio_streamer.threading.Thread = ImmediateThread
    return http_spotty_audio_streamer


class HTTPSpottyAudioStreamerTests(unittest.TestCase):
    def tearDown(self):
        for module_name in (
            "http_spotty_audio_streamer",
            "spotty_audio_streamer",
            "spotty_cache",
            "utils",
            "spotty",
            "spotipy",
            "xbmc",
            "xbmcaddon",
            "xbmcgui",
            "bottle",
        ):
            sys.modules.pop(module_name, None)

    def test_new_track_get_does_not_publish_confirmed_current_track(self):
        module = import_http_streamer()
        started = []
        streamer = module.HTTPSpottyAudioStreamer(
            object(),
            on_track_started_callback=lambda track_id, duration: started.append(
                (track_id, duration)
            ),
        )

        result = streamer.spotty_stream_audio_track("queued-track", "180.wav")

        self.assertEqual([], started)
        self.assertTrue(hasattr(result, "__iter__"))

    def test_queue_preload_does_not_terminate_unfinished_current_stream(self):
        module = import_http_streamer()
        module.PRELOAD_HANDOFF_WAIT_SECONDS = 0.0
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = FakeSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "current-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "current-request"

        result = streamer.spotty_stream_audio_track("queued-track", "180.wav")

        self.assertEqual(
            0,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        self.assertEqual(
            "current-track",
            streamer._HTTPSpottyAudioStreamer__current_track_id,
        )
        self.assertEqual(503, FakeBottleState.response.status)
        self.assertEqual("", result)

    def test_early_player_metadata_does_not_override_unfinished_handoff(self):
        module = import_http_streamer()
        module.PRELOAD_HANDOFF_WAIT_SECONDS = 0.0
        module.xbmc.getInfoLabel = lambda label: (
            "queued-track"
            if label
            in (
                "MusicPlayer.Property(spotifytrackid)",
                "MusicPlayer.(1).Property(spotifytrackid)",
            )
            else "http://127.0.0.1:52309/track/queued-track/180.wav"
        )
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = FakeSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "current-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "current-request"

        result = streamer.spotty_stream_audio_track("queued-track", "180.wav")

        self.assertEqual(
            0,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        self.assertEqual(
            "current-track",
            streamer._HTTPSpottyAudioStreamer__current_track_id,
        )
        self.assertEqual(503, FakeBottleState.response.status)
        self.assertEqual("", result)


if __name__ == "__main__":
    unittest.main()
