import os
import sys
import threading
import time
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


class FakeWindow:
    properties = {}

    def __init__(self, window_id=None):
        self.window_id = window_id

    def getProperty(self, key):
        return self.properties.get(key, "")

    def setProperty(self, key, value):
        self.properties[key] = str(value)

    def clearProperty(self, key):
        self.properties.pop(key, None)


class FakeAddon:
    def getSetting(self, key):
        if key == "spotify_bitrate":
            return "320"
        if key == "spotify_normalization":
            return "auto"
        return ""


class FakeStreamSpec:
    def __init__(self, track_id, duration, bitrate="320", normalization="auto"):
        self.track_id = track_id
        self.track_duration = int(max(1, duration or 1))
        self.wav_header = b"0" * 44
        self.track_length = 44 + int(self.track_duration * 176400)
        self.bitrate = bitrate
        self.normalization_gain_type = normalization
        self.initial_volume = 35


class FakeSpottyAudioStreamer:
    def __init__(self, spotty):
        self.normalization_gain_type = "auto"
        self.use_autoplay = False
        self.bitrate = "320"
        self.track_id = ""
        self.duration = 0
        self.terminate_calls = 0
        self.set_track_calls = []
        self.prepare_result = True
        self.prepare_exception = None
        self.prepare_calls = []
        self.prepare_entered = threading.Event()
        self.prepare_release = None
        self.send_spec_calls = []
        self.terminate_generation = 0

    def set_notify_track_finished(self, func):
        self.notify_track_finished = func

    def terminate_stream(self):
        self.terminate_calls += 1
        self.terminate_generation += 1
        return True

    def set_track(self, track_id, duration):
        self.track_id = track_id
        self.duration = duration
        self.set_track_calls.append((track_id, duration))

    def get_track_length(self):
        return 44 + int(max(1, self.duration or 1) * 176400)

    def get_stream_spec(self):
        return FakeStreamSpec(
            self.track_id,
            self.duration,
            self.bitrate,
            self.normalization_gain_type,
        )

    def restore_stream_spec(self, stream_spec):
        self.track_id = stream_spec.track_id
        self.duration = stream_spec.track_duration
        self.bitrate = stream_spec.bitrate
        self.normalization_gain_type = stream_spec.normalization_gain_type

    def send_part_audio_stream(self, range_len, range_begin, stream_spec=None):
        captured_spec = stream_spec or self.get_stream_spec()
        captured_generation = self.terminate_generation

        def generate():
            if captured_generation != self.terminate_generation:
                return
            self.send_spec_calls.append(captured_spec.track_id)
            yield b"x" * min(range_len, 16)

        return generate()

    def prepare_part_audio_stream(self, range_begin, stream_spec=None):
        self.prepare_calls.append(range_begin)
        self.prepare_entered.set()
        if self.prepare_release is not None:
            self.prepare_release.wait(timeout=5.0)
        if self.prepare_exception is not None:
            raise self.prepare_exception
        return self.prepare_result


def install_stubs():
    FakeWindow.properties = {}

    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    xbmc.getInfoLabel = lambda label: ""
    xbmc.getCondVisibility = lambda condition: False
    sys.modules["xbmc"] = xbmc

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda id=None: FakeAddon()
    sys.modules["xbmcaddon"] = xbmcaddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = FakeWindow
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
    spotty_audio_streamer.STARTUP_REAL_PCM_WAIT_SECONDS = 15.0
    spotty_audio_streamer.SpottyAudioStreamer = FakeSpottyAudioStreamer
    spotty_audio_streamer.SpottyStreamSpec = FakeStreamSpec
    spotty_audio_streamer.create_wav_header_for_duration = lambda duration: (
        b"0" * 44,
        44 + int(float(duration) * 176400),
    )
    sys.modules["spotty_audio_streamer"] = spotty_audio_streamer


class ActiveDownloader:
    def __init__(self, has_real_pcm=True, has_recent_progress=True, is_finished=False):
        self.is_finished = is_finished
        self.error = False
        self.aborted = False
        self.cond = threading.Condition()
        self._has_real_pcm = has_real_pcm
        self._has_recent_progress = has_recent_progress
        self.cleanup_calls = 0

    def has_real_pcm(self):
        return self._has_real_pcm

    def has_recent_progress(self, max_idle_seconds):
        return self._has_recent_progress

    def cleanup(self):
        self.cleanup_calls += 1
        self.aborted = True


class FakeSpottyCacheManager:
    active_downloader = ActiveDownloader()

    @classmethod
    def find_best_downloader(cls, track_id, request_byte):
        if track_id == "current-track":
            return cls.active_downloader
        return None


class MissingSpottyCacheManager:
    @classmethod
    def find_best_downloader(cls, track_id, request_byte):
        return None


def install_downloader_cache(downloader, track_id="current-track", start_byte=0):
    class FixedSpottyCacheManager:
        @classmethod
        def find_best_downloader(cls, requested_track_id, request_byte):
            if requested_track_id == track_id and start_byte <= request_byte:
                return downloader
            return None

    spotty_cache = types.ModuleType("spotty_cache")
    spotty_cache.SpottyCacheManager = FixedSpottyCacheManager
    sys.modules["spotty_cache"] = spotty_cache


def install_trimmed_active_cache(downloader, track_id="current-track"):
    class TrimAwareSpottyCacheManager:
        @classmethod
        def find_best_downloader(cls, requested_track_id, request_byte):
            # Byte zero has already been trimmed and is no longer readable.
            return None

        @classmethod
        def find_active_downloader(cls, requested_track_id):
            return downloader if requested_track_id == track_id else None

    spotty_cache = types.ModuleType("spotty_cache")
    spotty_cache.SpottyCacheManager = TrimAwareSpottyCacheManager
    sys.modules["spotty_cache"] = spotty_cache


def import_http_streamer():
    install_stubs()
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    sys.modules.pop("http_spotty_audio_streamer", None)
    import http_spotty_audio_streamer

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

    def test_delayed_replaced_response_cannot_start_after_new_track_takes_over(self):
        module = import_http_streamer()
        streamer = module.HTTPSpottyAudioStreamer(object())

        first_result = streamer.spotty_stream_audio_track("first-track", "180.wav")
        # WSGI has not iterated first_result yet. A newer request replaces it,
        # so the stale body must stop before it can open either track's cache.
        second_result = streamer.spotty_stream_audio_track("second-track", "200.wav")

        try:
            with self.assertRaises(StopIteration):
                next(first_result)
            self.assertEqual([], streamer._HTTPSpottyAudioStreamer__spotty_streamer.send_spec_calls)
        finally:
            first_result.close()
            second_result.close()

    def test_same_track_range_reader_reuses_initialized_stream(self):
        module = import_http_streamer()
        install_downloader_cache(ActiveDownloader(), track_id="same-track")
        streamer = module.HTTPSpottyAudioStreamer(object())

        first = streamer.spotty_stream_audio_track("same-track", "180.wav")
        first.close()
        module.bottle.request.headers = {"Range": "bytes=44-1024"}

        second = streamer.spotty_stream_audio_track("same-track", "180.wav")

        self.assertEqual(
            [("same-track", 180.0)],
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls,
        )
        self.assertEqual("206 Partial Content", FakeBottleState.response.status)
        try:
            self.assertEqual(b"x" * 16, next(second))
        finally:
            second.close()

    def test_unhealthy_same_track_retry_performs_fresh_preflight(self):
        module = import_http_streamer()
        stalled_downloader = ActiveDownloader(
            has_real_pcm=True,
            has_recent_progress=False,
        )
        install_downloader_cache(stalled_downloader, track_id="same-track")
        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "same-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "stalled-request"

        result = streamer.spotty_stream_audio_track("same-track", "180.wav")

        fake_streamer = streamer._HTTPSpottyAudioStreamer__spotty_streamer
        self.assertEqual([("same-track", 180.0)], fake_streamer.set_track_calls)
        self.assertEqual([0], fake_streamer.prepare_calls)
        self.assertEqual(1, fake_streamer.terminate_calls)
        self.assertEqual(1, stalled_downloader.cleanup_calls)
        self.assertNotEqual(
            "stalled-request",
            streamer._HTTPSpottyAudioStreamer__current_request_id,
        )
        self.assertNotEqual(503, FakeBottleState.response.status)
        result.close()

    def test_queue_preload_does_not_terminate_unfinished_current_stream(self):
        module = import_http_streamer()
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "current-track",
        )
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = FakeSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "current-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "current-request"

        started = time.monotonic()
        result = streamer.spotty_stream_audio_track("queued-track", "180.wav")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.25)
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
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "current-track",
        )
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

    def test_active_at_zero_without_real_pcm_is_replaced_immediately(self):
        module = import_http_streamer()
        downloader = ActiveDownloader(has_real_pcm=False, has_recent_progress=False)
        install_downloader_cache(downloader)
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "current-track",
        )
        module.xbmc.getInfoLabel = lambda label: {
            "Player.Filenameandpath": ("http://127.0.0.1:52309/track/current-track/180.wav"),
            "MusicPlayer.Time": "00:00",
            "MusicPlayer.Duration": "03:00",
        }.get(label, "")

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "current-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "current-request"

        started = time.monotonic()
        result = streamer.spotty_stream_audio_track("selected-track", "180.wav")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.25)
        self.assertNotEqual(503, FakeBottleState.response.status)
        self.assertEqual(
            1,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        self.assertEqual(
            [("selected-track", 180.0)],
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls,
        )
        result.close()

    def test_stale_real_pcm_progress_does_not_confirm_previous_playback(self):
        module = import_http_streamer()
        downloader = ActiveDownloader(has_real_pcm=True, has_recent_progress=False)
        install_downloader_cache(downloader)
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "current-track",
        )

        streamer = module.HTTPSpottyAudioStreamer(object())

        self.assertFalse(streamer._previous_stream_has_confirmed_playback("current-track"))

    def test_trimmed_byte_zero_does_not_hide_healthy_active_downloader(self):
        module = import_http_streamer()
        downloader = ActiveDownloader(has_real_pcm=True, has_recent_progress=True)
        install_trimmed_active_cache(downloader)
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "current-track",
        )

        streamer = module.HTTPSpottyAudioStreamer(object())

        self.assertTrue(streamer._previous_stream_has_confirmed_playback("current-track"))

    def test_finished_healthy_downloader_allows_queued_handoff(self):
        module = import_http_streamer()
        downloader = ActiveDownloader(is_finished=True)
        install_downloader_cache(downloader)
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "current-track",
        )

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "current-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "current-request"

        result = streamer.spotty_stream_audio_track("queued-track", "180.wav")

        self.assertNotEqual(503, FakeBottleState.response.status)
        self.assertEqual(
            0,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        self.assertEqual(
            [("queued-track", 180.0)],
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls,
        )
        result.close()

    def test_kodi_confirmed_manual_selection_replaces_healthy_current_track(self):
        module = import_http_streamer()
        downloader = ActiveDownloader()
        install_downloader_cache(downloader)
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "selected-track",
        )
        module.xbmc.getInfoLabel = lambda label: {
            "Player.Filenameandpath": ("http://127.0.0.1:52309/track/selected-track/180.wav"),
            "MusicPlayer.Time": "00:01",
            "MusicPlayer.Duration": "03:00",
        }.get(label, "")

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "current-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "current-request"

        result = streamer.spotty_stream_audio_track("selected-track", "180.wav")

        self.assertNotEqual(503, FakeBottleState.response.status)
        self.assertEqual(
            1,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        self.assertEqual(
            [("selected-track", 180.0)],
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls,
        )
        result.close()

    def test_stale_internal_stream_state_does_not_block_user_selected_track(self):
        module = import_http_streamer()
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = MissingSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache
        module.xbmc.getInfoLabel = lambda label: {
            "Player.Filenameandpath": ("http://127.0.0.1:52309/track/selected-track/180.wav"),
        }.get(label, "")

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "stale-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "stale-request"

        result = streamer.spotty_stream_audio_track("selected-track", "180.wav")

        self.assertNotEqual(503, FakeBottleState.response.status)
        self.assertEqual(
            1,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        self.assertEqual(
            [("selected-track", 180.0)],
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls,
        )
        result.close()

    def test_stale_player_filename_without_active_audio_does_not_confirm_playback(self):
        module = import_http_streamer()
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = MissingSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache
        module.xbmc.getInfoLabel = lambda label: {
            "Player.Filenameandpath": "http://127.0.0.1:52309/track/stale-track/180.wav",
            "MusicPlayer.Time": "02:55",
            "MusicPlayer.Duration": "03:00",
        }.get(label, "")
        module.xbmc.getCondVisibility = lambda condition: False

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "stale-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "stale-request"

        result = streamer.spotty_stream_audio_track("selected-track", "180.wav")

        self.assertNotEqual(503, FakeBottleState.response.status)
        self.assertEqual(
            1,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        self.assertEqual(
            [("selected-track", 180.0)],
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls,
        )
        result.close()

    def test_stale_current_property_without_active_audio_does_not_confirm_playback(self):
        module = import_http_streamer()
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = MissingSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "stale-track",
        )
        module.xbmc.getCondVisibility = lambda condition: False

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "stale-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "stale-request"

        result = streamer.spotty_stream_audio_track("selected-track", "180.wav")

        self.assertNotEqual(503, FakeBottleState.response.status)
        self.assertEqual(
            1,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        self.assertEqual(
            [("selected-track", 180.0)],
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls,
        )
        result.close()

    def test_natural_end_handoff_allows_next_track_when_downloader_was_evicted(self):
        module = import_http_streamer()
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmcgui.Window(module.ADDON_WINDOW_ID).setProperty(
            "Spotify.CurrentTrackId",
            "current-track",
        )
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = MissingSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache
        module.xbmc.getInfoLabel = lambda label: {
            "Player.Filenameandpath": ("http://127.0.0.1:52309/track/current-track/180.wav"),
            "MusicPlayer.Time": "02:55",
            "MusicPlayer.Duration": "03:00",
        }.get(label, "")

        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "current-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "current-request"

        result = streamer.spotty_stream_audio_track("queued-track", "180.wav")

        self.assertNotEqual(503, FakeBottleState.response.status)
        self.assertEqual(
            [("queued-track", 180.0)],
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls,
        )
        self.assertEqual(
            0,
            streamer._HTTPSpottyAudioStreamer__spotty_streamer.terminate_calls,
        )
        result.close()

    def test_failed_preserved_handoff_restores_full_previous_stream_spec(self):
        module = import_http_streamer()
        install_downloader_cache(ActiveDownloader(), track_id="current-track")
        module.xbmc.getCondVisibility = lambda condition: condition == "Player.HasAudio"
        module.xbmc.getInfoLabel = lambda label: {
            "Player.Filenameandpath": ("http://127.0.0.1:52309/track/current-track/180.wav"),
            "MusicPlayer.Time": "02:55",
            "MusicPlayer.Duration": "03:00",
        }.get(label, "")

        streamer = module.HTTPSpottyAudioStreamer(object())
        fake_streamer = streamer._HTTPSpottyAudioStreamer__spotty_streamer
        fake_streamer.set_track("current-track", 180.0)
        current_spec = fake_streamer.get_stream_spec()
        streamer._HTTPSpottyAudioStreamer__is_streaming = True
        streamer._HTTPSpottyAudioStreamer__current_track_id = "current-track"
        streamer._HTTPSpottyAudioStreamer__current_request_id = "current-request"
        streamer._HTTPSpottyAudioStreamer__current_stream_spec = current_spec
        fake_streamer.prepare_result = False

        failed = streamer.spotty_stream_audio_track("next-track", "200.wav")

        self.assertEqual("", failed)
        self.assertEqual("current-track", streamer._HTTPSpottyAudioStreamer__current_track_id)
        self.assertTrue(streamer._HTTPSpottyAudioStreamer__is_streaming)
        self.assertEqual("current-track", fake_streamer.get_stream_spec().track_id)
        self.assertEqual(180, fake_streamer.get_stream_spec().track_duration)

        fake_streamer.prepare_result = True
        module.bottle.request.headers = {"Range": "bytes=44-1024"}
        current_result = streamer.spotty_stream_audio_track("current-track", "180.wav")
        try:
            self.assertEqual(b"x" * 16, next(current_result))
            self.assertEqual("current-track", fake_streamer.send_spec_calls[-1])
        finally:
            current_result.close()

    def test_concurrent_range_waits_past_two_seconds_for_owner_preflight(self):
        module = import_http_streamer()
        self.assertGreater(
            module.INITIALIZATION_WAIT_SECONDS,
            module.STARTUP_REAL_PCM_WAIT_SECONDS,
        )
        install_downloader_cache(
            ActiveDownloader(),
            track_id="same-track",
            start_byte=256,
        )
        module.bottle.request.headers = {"Range": "bytes=256-1024"}
        streamer = module.HTTPSpottyAudioStreamer(object())
        fake_streamer = streamer._HTTPSpottyAudioStreamer__spotty_streamer
        fake_streamer.prepare_release = threading.Event()
        results = []
        errors = []

        def request_track(name):
            try:
                result = streamer.spotty_stream_audio_track("same-track", "180.wav")
                results.append(
                    (
                        name,
                        len(streamer._HTTPSpottyAudioStreamer__spotty_streamer.set_track_calls),
                        result,
                    )
                )
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=request_track, args=("first",), daemon=True)
        first.start()
        self.assertTrue(fake_streamer.prepare_entered.wait(timeout=1.0))

        second = threading.Thread(target=request_track, args=("second",), daemon=True)
        second.start()
        # The former 2-second waiter deadline returned a 503 while the owner
        # still had a valid 15-second Spotty recovery budget.
        time.sleep(2.1)
        self.assertEqual([], results)
        self.assertTrue(second.is_alive())
        self.assertTrue(streamer._HTTPSpottyAudioStreamer__init_in_progress)
        self.assertFalse(streamer._HTTPSpottyAudioStreamer__is_streaming)

        fake_streamer.prepare_release.set()
        first.join(timeout=1.0)
        second.join(timeout=1.0)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual({1}, {set_track_count for _name, set_track_count, _result in results})
        self.assertEqual(
            [("same-track", 180.0)],
            fake_streamer.set_track_calls,
        )
        self.assertEqual([256], fake_streamer.prepare_calls)
        for _name, _set_track_count, result in results:
            result.close()

    def test_new_track_preflight_failure_returns_503_before_generator(self):
        module = import_http_streamer()
        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__spotty_streamer.prepare_result = False

        result = streamer.spotty_stream_audio_track("failed-track", "180.wav")

        self.assertEqual(503, FakeBottleState.response.status)
        self.assertEqual("", result)
        self.assertFalse(streamer._HTTPSpottyAudioStreamer__is_streaming)
        self.assertEqual("", streamer._HTTPSpottyAudioStreamer__current_request_id)

        # A Water Flow-style zero-PCM failure must not poison the coordinator
        # and turn the following selection into a skip chain.
        streamer._HTTPSpottyAudioStreamer__spotty_streamer.prepare_result = True
        recovered = streamer.spotty_stream_audio_track("recovered-track", "180.wav")
        try:
            self.assertEqual(b"x" * 16, next(recovered))
            self.assertEqual(
                "recovered-track",
                streamer._HTTPSpottyAudioStreamer__current_track_id,
            )
        finally:
            recovered.close()

    def test_unexpected_preflight_exception_releases_initialization_for_next_track(self):
        module = import_http_streamer()
        streamer = module.HTTPSpottyAudioStreamer(object())
        fake_streamer = streamer._HTTPSpottyAudioStreamer__spotty_streamer
        fake_streamer.prepare_exception = RuntimeError("unexpected cache failure")

        with self.assertRaisesRegex(RuntimeError, "unexpected cache failure"):
            streamer.spotty_stream_audio_track("failed-track", "180.wav")

        self.assertFalse(streamer._HTTPSpottyAudioStreamer__init_in_progress)
        self.assertTrue(streamer._HTTPSpottyAudioStreamer__init_event.is_set())
        self.assertFalse(streamer._HTTPSpottyAudioStreamer__is_streaming)

        fake_streamer.prepare_exception = None
        started_at = time.monotonic()
        recovered = streamer.spotty_stream_audio_track("recovered-track", "180.wav")
        elapsed = time.monotonic() - started_at
        try:
            self.assertLess(elapsed, 0.5)
            self.assertNotEqual(503, FakeBottleState.response.status)
            self.assertEqual(b"x" * 16, next(recovered))
            self.assertEqual(
                "recovered-track",
                streamer._HTTPSpottyAudioStreamer__current_track_id,
            )
        finally:
            recovered.close()


if __name__ == "__main__":
    unittest.main()
