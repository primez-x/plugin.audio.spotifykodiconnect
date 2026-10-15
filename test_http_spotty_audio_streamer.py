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
    def getLocalizedString(self, number):
        return f"str-{number}"

    def getAddonInfo(self, key):
        return "Spotify"

    def getSetting(self, key):
        if key == "spotify_bitrate":
            return "320"
        if key == "spotify_normalization":
            return "auto"
        return ""


class FakeStreamSpec:
    def __init__(self, track_id, duration, wav_header=None, track_length=None,
                 bitrate="320", normalization="auto", initial_volume=35):
        self.track_id = track_id
        self.track_duration = float(max(1, duration or 1))
        self.wav_header = wav_header or b"0" * 44
        self.track_length = track_length or 44 + int(self.track_duration * 176400)
        self.bitrate = bitrate
        self.normalization_gain_type = normalization
        self.initial_volume = initial_volume


class FakeSpottyAudioStreamer:
    def __init__(self, spotty):
        self.initial_volume = 35
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

    def get_stream_generation(self):
        return self.terminate_generation

    def _is_terminated(self, generation):
        return self.terminate_generation != generation

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
            bitrate=self.bitrate,
            normalization=self.normalization_gain_type,
        )

    def restore_stream_spec(self, stream_spec):
        self.track_id = stream_spec.track_id
        self.duration = stream_spec.track_duration
        self.bitrate = stream_spec.bitrate
        self.normalization_gain_type = stream_spec.normalization_gain_type

    def send_part_audio_stream(self, range_len, range_begin, stream_spec=None, downloader=None, stream_generation=None):
        captured_spec = stream_spec or self.get_stream_spec()
        captured_generation = self.terminate_generation

        def generate():
            if captured_generation != self.terminate_generation:
                return
            self.send_spec_calls.append(captured_spec.track_id)
            yield b"x" * min(range_len, 16)

        return generate()

    def _get_or_start_downloader(self, stream_spec, range_begin, stream_generation=None):
        return ActiveDownloader()

    def prepare_part_audio_stream(self, range_begin, stream_spec=None, downloader=None, stream_generation=None):
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
    xbmc.LOGWARNING = 2
    xbmc.getInfoLabel = lambda label: ""
    xbmc.getCondVisibility = lambda condition: False
    sys.modules["xbmc"] = xbmc

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda id=None: FakeAddon()
    sys.modules["xbmcaddon"] = xbmcaddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = FakeWindow
    xbmcgui.Dialog = lambda: types.SimpleNamespace(notification=lambda *a, **kw: None)
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
    utils.get_valid_cached_auth_token = lambda: None
    utils.log_msg = lambda *args, **kwargs: None
    sys.modules["utils"] = utils

    like_actions = types.ModuleType("like_actions")
    like_actions.notify_not_connected = lambda: None
    like_actions.set_current_track_liked = lambda track_id, liked: True
    sys.modules["like_actions"] = like_actions

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

    def identity_valid(self):
        return True

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
    def find_best_downloader(cls, track_id, request_byte, stream_spec=None, spotty=None):
        if track_id == "current-track":
            return cls.active_downloader
        return None


class MissingSpottyCacheManager:
    @classmethod
    def find_best_downloader(cls, track_id, request_byte, stream_spec=None, spotty=None):
        return None


def install_downloader_cache(downloader, track_id="current-track", start_byte=0):
    class FixedSpottyCacheManager:
        @classmethod
        def find_best_downloader(cls, requested_track_id, request_byte, stream_spec=None, spotty=None):
            if requested_track_id == track_id and start_byte <= request_byte:
                return downloader
            return None

    spotty_cache = types.ModuleType("spotty_cache")
    spotty_cache.SpottyCacheManager = FixedSpottyCacheManager
    sys.modules["spotty_cache"] = spotty_cache


def install_trimmed_active_cache(downloader, track_id="current-track"):
    class TrimAwareSpottyCacheManager:
        @classmethod
        def find_best_downloader(cls, requested_track_id, request_byte, stream_spec=None, spotty=None):
            # Byte zero has already been trimmed and is no longer readable.
            return None

        @classmethod
        def find_active_downloader(cls, requested_track_id, stream_spec=None, spotty=None):
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
            "like_actions",
            "spotty",
            "spotipy",
            "xbmc",
            "xbmcaddon",
            "xbmcgui",
            "bottle",
        ):
            sys.modules.pop(module_name, None)

    def test_real_http_releases_exact_ranges_before_bulk_pcm(self):
        import http.client
        from socketserver import ThreadingMixIn
        from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server
        import test_stream_seams as seams

        fixture = seams.StreamSeamTests()
        fixture.setUp()
        release_bulk = threading.Event()
        first_frame = threading.Event()

        class GatedProcess(seams._Process):
            def __init__(self):
                super().__init__(seams.RAMP, None)
                self.stdout = self
                self.position = 0

            def read(self, size):
                if self.position < 8:
                    end = min(8, self.position + size)
                    chunk = seams.RAMP[self.position:end]
                    self.position = end
                    if end == 8:
                        first_frame.set()
                    return chunk
                release_bulk.wait(2)
                chunk = seams.RAMP[self.position:self.position + size]
                self.position += len(chunk)
                if not chunk:
                    self.returncode = 0
                return chunk

            def kill(self):
                release_bulk.set()
                self.returncode = -9

        class Server(ThreadingMixIn, WSGIServer):
            daemon_threads = True

        class Handler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        server = None
        try:
            sys.modules.pop("bottle", None)
            sys.path.insert(0, os.path.join(LIB_DIR, "deps"))
            import bottle
            fixture.http.bottle = bottle
            spotty = seams.RampSpotty()
            spotty.run_spotty = lambda args: GatedProcess()
            streamer = fixture._server(spotty)
            app = bottle.Bottle()
            app.route(
                "/track/<track_id>/<duration>",
                callback=streamer.spotty_stream_audio_track,
            )
            server = make_server("127.0.0.1", 0, app, Server, Handler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            for begin, end in ((0, 3), (0, 51), (20, 51), (44, 51), (47, 51)):
                with self.subTest(begin=begin, end=end):
                    streamer.release_stream(lambda spec: fixture.cache.SpottyCacheManager.cleanup_all())
                    release_bulk.clear()
                    first_frame.clear()
                    received = []
                    errors = []

                    def request():
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", server.server_port, timeout=2
                        )
                        try:
                            connection.request(
                                "GET", "/track/synthetic/30.wav",
                                headers={"Range": f"bytes={begin}-{end}"},
                            )
                            response = connection.getresponse()
                            received.append((response.status, response.read()))
                        except Exception as exc:
                            errors.append(exc)
                        finally:
                            connection.close()

                    requester = threading.Thread(target=request, daemon=True)
                    requester.start()
                    try:
                        self.assertTrue(first_frame.wait(1))
                        requester.join(0.3)
                        self.assertFalse(
                            requester.is_alive(),
                            "HTTP withheld complete requested PCM until bulk release",
                        )
                        self.assertEqual([], errors)
                        self.assertEqual([(206, fixture.ideal[begin:end + 1])], received)
                        self.assertFalse(release_bulk.is_set())
                    finally:
                        release_bulk.set()
                        requester.join(2)
        finally:
            release_bulk.set()
            if server is not None:
                streamer.release_stream(lambda spec: fixture.cache.SpottyCacheManager.cleanup_all())
                server.shutdown()
                server.server_close()
                server_thread.join(2)
            fixture.tearDown()

    def test_header_only_http_rejects_clean_exit_without_complete_pcm(self):
        import test_stream_seams as seams

        fixture = seams.StreamSeamTests()
        fixture.setUp()
        try:
            fixture.cache.SpottyDownloader._MAX_SESSION_RETRIES = 0
            for pcm in (b"", b"x", b"xy", b"xyz"):
                with self.subTest(pcm_bytes=len(pcm)):
                    spotty = seams.RampSpotty()
                    spotty.run_spotty = lambda args: seams._Process(pcm, 0)
                    streamer = fixture._server(spotty)
                    fixture.http.bottle.request.headers = {"Range": "bytes=0-3"}
                    result = streamer.spotty_stream_audio_track("partial", "1.wav")
                    self.assertEqual(503, fixture.http.bottle.response.status)
                    self.assertIsInstance(result, str)
                    streamer.release_stream(lambda spec: fixture.cache.SpottyCacheManager.cleanup_all())
        finally:
            fixture.tearDown()

    def test_playback_failure_notice_is_once_per_queue_session(self):
        module = import_http_streamer()
        notices = []
        module.xbmcgui.Dialog = lambda: types.SimpleNamespace(notification=lambda *a, **kw: notices.append((a, kw)))
        module.xbmcaddon.Addon = lambda **kw: types.SimpleNamespace(
            getLocalizedString=lambda number: f"str-{number}", getAddonInfo=lambda key: "Spotify")
        failures = types.ModuleType("spotty_cache")
        failures.SpottyCacheManager = types.SimpleNamespace(get_failure_cause=lambda track: "auth")
        sys.modules["spotty_cache"] = failures
        FakeWindow.properties.clear()
        FakeWindow.properties["Spotify.PlayQueue.SessionId"] = "session-1"
        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._notify_playback_failure("track-1")
        streamer._notify_playback_failure("track-2")
        self.assertEqual(1, len(notices))
        self.assertIn("str-11160", notices[0][0])
        FakeWindow.properties["Spotify.PlayQueue.SessionId"] = "session-2"
        failures.SpottyCacheManager.get_failure_cause = lambda track: "premium"
        streamer._notify_playback_failure("track-3")
        self.assertEqual(2, len(notices))
        self.assertIn("str-11161", notices[1][0])

    def test_midstream_failure_notifies_and_replaced_stream_stays_quiet(self):
        module = import_http_streamer()
        notices = []
        module.xbmcgui.Dialog = lambda: types.SimpleNamespace(notification=lambda *a, **kw: notices.append(a))
        FakeWindow.properties.clear()
        FakeWindow.properties["Spotify.PlayQueue.SessionId"] = "midstream-session"
        failures = types.ModuleType("spotty_cache")
        failures.SpottyCacheManager = types.SimpleNamespace(get_failure_cause=lambda track: "connection")
        sys.modules["spotty_cache"] = failures
        streamer = module.HTTPSpottyAudioStreamer(object())
        response = streamer.spotty_stream_audio_track("track", "180.wav")
        list(response)
        self.assertEqual(1, len(notices))
        notices.clear()
        FakeWindow.properties["Spotify.PlayQueue.SessionId"] = "replacement-session"
        response = streamer.spotty_stream_audio_track("next", "180.wav")
        streamer.release_stream(lambda spec: None)
        list(response)
        self.assertEqual([], notices)

    def test_preflight_failure_notifies_but_busy_refusal_stays_quiet(self):
        module = import_http_streamer()
        notices = []
        module.xbmcgui.Dialog = lambda: types.SimpleNamespace(notification=lambda *a, **kw: notices.append(a))
        module.xbmcaddon.Addon = lambda **kw: types.SimpleNamespace(
            getLocalizedString=lambda number: f"str-{number}", getAddonInfo=lambda key: "Spotify")
        FakeWindow.properties.clear()
        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._initialization_busy_response("track", "busy")
        self.assertEqual([], notices)
        streamer._HTTPSpottyAudioStreamer__spotty_streamer.prepare_result = False
        streamer.spotty_stream_audio_track("track", "180.wav")
        self.assertEqual(503, FakeBottleState.response.status)
        self.assertEqual(1, len(notices))
        self.assertIn("str-11159", notices[0])
        streamer.spotty_stream_audio_track("next-track", "180.wav")
        self.assertEqual(1, len(notices))

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

    def test_backpressure_paused_downloader_is_healthy_and_handoff_safe(self):
        module = import_http_streamer()
        # The real downloader needs a few extra stub attributes.
        sys.modules["xbmc"].LOGWARNING = 2
        sys.modules["xbmc"].LOGERROR = 3
        sys.modules["utils"].log_exception = lambda *args, **kwargs: None
        sys.modules.pop("spotty_cache", None)
        import spotty_cache

        downloader = spotty_cache.SpottyDownloader(
            spotty=types.SimpleNamespace(get_spotty_credentials_identity=lambda: ("fixture", 1)),
            track_id="current-track",
            duration_sec=600,
            start_byte=0,
            bitrate="320",
            normalization="off",
            volume=35,
            wav_header=b"0" * 44,
            track_length=44 + 600 * 176400,
        )
        downloader._download_loop = lambda: None
        downloader.start()
        with downloader.cond:
            downloader.written_bytes = 44 + 16 * 1024 * 1024
            downloader.real_pcm_bytes = 16 * 1024 * 1024
            # Last write long ago: only the reader-limited pause explains it.
            downloader.last_progress_monotonic = time.monotonic() - 600.0
            downloader._bp_paused = True
            downloader._consumer_positions[0] = 0

        manager = spotty_cache.SpottyCacheManager
        manager._instances[("current-track", 0)] = downloader
        try:
            streamer = module.HTTPSpottyAudioStreamer(object())
            self.assertTrue(
                module.HTTPSpottyAudioStreamer._downloader_has_healthy_progress(downloader)
            )
            self.assertTrue(
                streamer._previous_downloader_finished_for_handoff(
                    "current-track", "queued-track"
                )
            )

            # Without an attached reader the same idle downloader is stale.
            with downloader.cond:
                downloader._consumer_positions.clear()
            self.assertFalse(
                module.HTTPSpottyAudioStreamer._downloader_has_healthy_progress(downloader)
            )
            self.assertFalse(
                streamer._previous_downloader_finished_for_handoff(
                    "current-track", "queued-track"
                )
            )
        finally:
            manager._instances.clear()
            manager._current_key = None

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

    def test_release_after_stop_makes_same_song_replay_start_fresh(self):
        """Stop -> replay of the same song must not inherit the stopped stream."""
        module = import_http_streamer()
        stale = ActiveDownloader()
        install_downloader_cache(stale, track_id="replay-track")
        streamer = module.HTTPSpottyAudioStreamer(object())
        fake_streamer = streamer._HTTPSpottyAudioStreamer__spotty_streamer
        streamer.spotty_stream_audio_track("replay-track", "180.wav").close()
        replays = []
        replay_threads = []

        def request_replay():
            replays.append(streamer.spotty_stream_audio_track("replay-track", "180.wav"))

        def release_resources(spec):
            # A replay that arrives during the release waits for the release.
            thread = threading.Thread(target=request_replay, daemon=True)
            thread.start()
            replay_threads.append(thread)
            time.sleep(0.2)
            self.assertEqual([], replays)
            stale.cleanup()

        streamer.release_stream(release_resources)
        replay_threads[0].join(timeout=2.0)

        self.assertEqual(1, len(replays))
        try:
            self.assertEqual(b"x" * 16, next(replays[0]))
            # The replay ran a new startup instead of reusing the stopped stream.
            self.assertEqual(
                [("replay-track", 180.0), ("replay-track", 180.0)],
                fake_streamer.set_track_calls,
            )
            self.assertEqual([0, 0], fake_streamer.prepare_calls)
            self.assertEqual(1, stale.cleanup_calls)
        finally:
            replays[0].close()

    def test_overlapping_release_preserves_the_first_cleanup_reservation(self):
        module = import_http_streamer()
        streamer = module.HTTPSpottyAudioStreamer(object())
        entered = threading.Event()
        finish = threading.Event()
        second_cleanup = []

        def first_cleanup(spec):
            entered.set()
            finish.wait(timeout=2)

        thread = threading.Thread(target=lambda: streamer.release_stream(first_cleanup))
        thread.start()
        try:
            self.assertTrue(entered.wait(timeout=1))
            streamer.release_stream(lambda spec: second_cleanup.append(True))
            self.assertEqual([], second_cleanup)
            self.assertTrue(streamer._HTTPSpottyAudioStreamer__init_in_progress)
            self.assertFalse(streamer._HTTPSpottyAudioStreamer__init_event.is_set())
        finally:
            finish.set()
            thread.join(timeout=2)

    def _assert_late_stop_release_keeps_new_startup(self, new_track_id):
        """A Stop release that arrives after a new selection started must yield.

        Kodi delivers the Python Stop callback only after PAPlayer::CloseFile has
        waited for the stopped player's open jobs, so a startup still pending at
        that point belongs to the next player.  Kodi sets the next player's
        isPlaying flag after the open job (and the GET) can start, so the stop
        check alone cannot tell the two apart.
        """
        module = import_http_streamer()
        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = MissingSpottyCacheManager
        sys.modules["spotty_cache"] = spotty_cache
        streamer = module.HTTPSpottyAudioStreamer(object())
        fake_streamer = streamer._HTTPSpottyAudioStreamer__spotty_streamer
        # The stopped song: Kodi closed the connection on Stop.
        streamer.spotty_stream_audio_track("stopped-track", "180.wav").close()
        fake_streamer.prepare_entered.clear()
        fake_streamer.prepare_release = threading.Event()
        results = []
        thread = threading.Thread(
            target=lambda: results.append(
                streamer.spotty_stream_audio_track(new_track_id, "200.wav")
            ),
            daemon=True,
        )
        thread.start()
        self.assertTrue(fake_streamer.prepare_entered.wait(timeout=1.0))
        released = []

        streamer.release_stream(lambda spec: released.append(True))
        fake_streamer.prepare_release.set()
        thread.join(timeout=1.0)

        self.assertEqual([], released)
        self.assertEqual(1, len(results))
        try:
            self.assertEqual(b"x" * 16, next(results[0]))
            self.assertTrue(streamer.is_current_track_streaming(new_track_id))
        finally:
            results[0].close()

    def test_late_stop_release_keeps_a_new_track_startup(self):
        self._assert_late_stop_release_keeps_new_startup("next-track")

    def test_late_stop_release_keeps_a_same_song_replay_startup(self):
        self._assert_late_stop_release_keeps_new_startup("stopped-track")

    def test_stream_refusal_is_logged_without_debug_logging(self):
        """A skip chain at default log level must show why each track was refused."""
        module = import_http_streamer()
        logs = []
        module.log_msg = lambda msg, level=0, *args, **kwargs: logs.append((level, msg))
        streamer = module.HTTPSpottyAudioStreamer(object())
        streamer._HTTPSpottyAudioStreamer__spotty_streamer.prepare_result = False

        streamer.spotty_stream_audio_track("failed-track", "180.wav")

        warnings = [msg for level, msg in logs if level == module.LOGWARNING]
        self.assertEqual(1, len(warnings), logs)
        self.assertIn("failed-track", warnings[0])
        self.assertIn("real PCM", warnings[0])


class HTTPRangeTests(unittest.TestCase):
    """RFC 7233 byte ranges: the last-byte-pos is inclusive."""

    TRACK_SIZE = 44 + 180 * 176400  # FakeStreamSpec length for "180.wav"

    def tearDown(self):
        for module_name in (
            "http_spotty_audio_streamer",
            "spotty_audio_streamer",
            "spotty_cache",
            "utils",
            "like_actions",
            "spotty",
            "spotipy",
            "xbmc",
            "xbmcaddon",
            "xbmcgui",
            "bottle",
        ):
            sys.modules.pop(module_name, None)

    def _request(self, range_header, track_id="range-track"):
        module = import_http_streamer()
        streamer = module.HTTPSpottyAudioStreamer(object())
        fake_streamer = streamer._HTTPSpottyAudioStreamer__spotty_streamer
        sent = []
        original_send = fake_streamer.send_part_audio_stream

        def recording_send(range_len, range_begin, stream_spec=None, downloader=None, stream_generation=None):
            sent.append((range_begin, range_len))
            return original_send(range_len, range_begin, stream_spec=stream_spec)

        fake_streamer.send_part_audio_stream = recording_send
        if range_header is not None:
            module.bottle.request.headers = {"Range": range_header}
        result = streamer.spotty_stream_audio_track(track_id, "180.wav")
        if hasattr(result, "close"):
            self.addCleanup(result.close)
        return module, streamer, result, sent

    def test_parse_byte_range(self):
        module = import_http_streamer()
        parse = module.HTTPSpottyAudioStreamer._parse_byte_range
        self.assertEqual((0, 100), parse("bytes=0-99", 1000))
        self.assertEqual((0, 1), parse("bytes=0-0", 1000))
        self.assertEqual((500, 1000), parse("bytes=500-", 1000))
        self.assertEqual((44, 1000), parse("bytes=44-", 1000))
        self.assertEqual((900, 1000), parse("bytes=900-5000", 1000))
        self.assertEqual((999, 1000), parse("bytes=999-999", 1000))
        self.assertEqual((900, 1000), parse("bytes=-100", 1000))
        self.assertEqual((0, 1000), parse("bytes=-5000", 1000))
        self.assertIsNone(parse("bytes=1000-", 1000))
        self.assertIsNone(parse("bytes=1500-1600", 1000))
        self.assertIsNone(parse("bytes=-0", 1000))
        # Malformed ranges are ignored (whole file), as before.
        self.assertEqual((0, 1000), parse("bytes=abc-", 1000))
        self.assertEqual((0, 1000), parse("bytes=50-10", 1000))
        self.assertEqual((0, 1000), parse("items=0-10", 1000))

    def test_closed_range_returns_inclusive_length(self):
        _module, _streamer, result, sent = self._request("bytes=0-99")
        response = FakeBottleState.response
        self.assertEqual("206 Partial Content", response.status)
        self.assertEqual(100, response.content_length)
        self.assertEqual(f"bytes 0-99/{self.TRACK_SIZE}", response.headers["Content-Range"])
        self.assertEqual(b"x" * 16, next(result))
        self.assertEqual([(0, 100)], sent)

    def test_closed_range_is_clamped_to_file_size(self):
        start = self.TRACK_SIZE - 10
        _module, _streamer, _result, sent = self._request(f"bytes={start}-{self.TRACK_SIZE + 500}")
        response = FakeBottleState.response
        self.assertEqual(10, response.content_length)
        self.assertEqual(
            f"bytes {start}-{self.TRACK_SIZE - 1}/{self.TRACK_SIZE}",
            response.headers["Content-Range"],
        )
        self.assertEqual([(start, 10)], sent)

    def test_open_ended_range_unchanged(self):
        _module, _streamer, _result, sent = self._request("bytes=1000-")
        response = FakeBottleState.response
        self.assertEqual("206 Partial Content", response.status)
        self.assertEqual(self.TRACK_SIZE - 1000, response.content_length)
        self.assertEqual(
            f"bytes 1000-{self.TRACK_SIZE - 1}/{self.TRACK_SIZE}",
            response.headers["Content-Range"],
        )
        self.assertEqual([(1000, self.TRACK_SIZE - 1000)], sent)

    def test_full_request_without_range_is_200(self):
        _module, _streamer, _result, _sent = self._request(None)
        response = FakeBottleState.response
        self.assertEqual(200, response.status)
        self.assertEqual(self.TRACK_SIZE, response.content_length)
        self.assertNotIn("Content-Range", response.headers)

    def test_start_past_end_returns_416_and_releases_initialization(self):
        _module, streamer, result, sent = self._request(f"bytes={self.TRACK_SIZE}-")
        response = FakeBottleState.response
        self.assertEqual(416, response.status)
        self.assertEqual(f"bytes */{self.TRACK_SIZE}", response.headers["Content-Range"])
        self.assertEqual(0, response.content_length)
        self.assertEqual("", result)
        self.assertEqual([], sent)
        self.assertEqual([], streamer._HTTPSpottyAudioStreamer__spotty_streamer.prepare_calls)
        self.assertFalse(streamer._HTTPSpottyAudioStreamer__init_in_progress)
        self.assertTrue(streamer._HTTPSpottyAudioStreamer__init_event.is_set())


if __name__ == "__main__":
    unittest.main()
