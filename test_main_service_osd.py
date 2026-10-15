import os
import sys
import threading

# import_main_service patches main_service.threading.Thread, which is the
# global threading module; restore it so later test modules get real threads.
_REAL_THREAD = threading.Thread
import types
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")


FAKE_SETTINGS = {}
_TEST_PROFILE = tempfile.TemporaryDirectory(prefix="spotify-service-")


class FakeAddon:
    def getAddonInfo(self, key):
        if key == "version":
            return "test"
        return "SpotifyKodiConnect"

    def getSetting(self, key):
        return FAKE_SETTINGS.get(key, "")

    def getLocalizedString(self, string_id):
        return f"string-{string_id}"


NOTIFICATIONS = []


class FakeDialog:
    def notification(self, heading, message, icon="", time=0, sound=True):
        NOTIFICATIONS.append((heading, message))


class FakeWindow:
    windows = {}

    def __new__(cls, window_id=None):
        if window_id not in cls.windows:
            instance = super().__new__(cls)
            instance.properties = {}
            cls.windows[window_id] = instance
        return cls.windows[window_id]

    def getProperty(self, key):
        return self.properties.get(key, "")

    def setProperty(self, key, value):
        self.properties[key] = value

    def clearProperty(self, key):
        self.properties.pop(key, None)


class FakeMonitor:
    def waitForAbort(self, timeout_in_secs):
        return False


class FakePlayer:
    pass


class ImmediateThread:
    def __init__(self, target, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        self.target(*self.args)


class FailingThread:
    def __init__(self, target, args=(), daemon=False):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        raise RuntimeError("can't start new thread")


class FakeSpotify:
    instance = None

    def __init__(self, on_delete=None):
        self.on_delete = on_delete
        self.deleted = []

    def current_user_saved_tracks_delete(self, track_ids):
        self.deleted.extend(track_ids)
        if self.on_delete:
            self.on_delete()

    def current_user_saved_tracks_add(self, track_ids):
        pass


class FakeDownloader:
    def __init__(self, error=False, aborted=False):
        self.cond = threading.Condition()
        self.is_finished = True
        self.error = error
        self.aborted = aborted


def install_stubs(info_labels, settings=None):
    FAKE_SETTINGS.clear()
    FAKE_SETTINGS.update(settings or {})
    NOTIFICATIONS.clear()

    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.Monitor = FakeMonitor
    xbmc.Player = FakePlayer
    xbmc.sleep = lambda millis: None
    xbmc.getInfoLabel = lambda label: info_labels.get(label, "")
    sys.modules["xbmc"] = xbmc

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda id=None: FakeAddon()
    sys.modules["xbmcaddon"] = xbmcaddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = FakeWindow
    xbmcgui.Dialog = FakeDialog
    sys.modules["xbmcgui"] = xbmcgui

    bottle_manager = types.ModuleType("bottle_manager")
    bottle_manager.route_all = lambda streamer: None
    sys.modules["bottle_manager"] = bottle_manager

    spotipy = types.ModuleType("spotipy")
    spotipy.Spotify = lambda auth=None: FakeSpotify.instance
    sys.modules["spotipy"] = spotipy

    spotty = types.ModuleType("spotty")
    spotty.get_spotty = lambda helper: None
    sys.modules["spotty"] = spotty

    http_streamer = types.ModuleType("http_spotty_audio_streamer")
    http_streamer.HTTPSpottyAudioStreamer = object
    sys.modules["http_spotty_audio_streamer"] = http_streamer

    prebuffer = types.ModuleType("prebuffer")
    prebuffer.PrebufferManager = object
    sys.modules["prebuffer"] = prebuffer

    spotty_auth = types.ModuleType("spotty_auth")
    spotty_auth.SpottyAuth = object
    sys.modules["spotty_auth"] = spotty_auth

    spotty_helper = types.ModuleType("spotty_helper")
    spotty_helper.SpottyHelper = object
    sys.modules["spotty_helper"] = spotty_helper

    utils = types.ModuleType("utils")
    utils.ADDON_ID = "plugin.audio.spotifykodiconnect"
    utils.ADDON_WINDOW_ID = 10000
    utils.ADDON_DATA_PATH = _TEST_PROFILE.name
    utils.PROXY_HOST = "127.0.0.1"
    utils.PROXY_PORT = 52309
    utils.get_cached_auth_token = lambda: None
    utils.get_valid_cached_auth_token = lambda: "token"
    utils.clear_expired_cached_auth_token = lambda now=None: False
    utils.set_auth_renew_failing = lambda failures: None
    utils.install_spotipy_rate_limit_hook = lambda module: None
    utils.is_rate_limited = lambda now=None: False
    utils.log_exception = lambda *args, **kwargs: None
    utils.log_msg = lambda *args, **kwargs: None
    sys.modules["utils"] = utils

    string_ids = types.ModuleType("string_ids")
    string_ids.WELCOME_AUTHENTICATED_STR_ID = 0
    string_ids.SPOTIFY_CONNECTING_STR_ID = 11150
    sys.modules["string_ids"] = string_ids


def import_main_service(info_labels, settings=None):
    install_stubs(info_labels, settings=settings)
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    for module_name in ("main_service", "playlist_next", "like_actions", "play_queue"):
        sys.modules.pop(module_name, None)
    import main_service

    main_service.threading.Thread = ImmediateThread
    return main_service


class SpotifyOSDPlayerMonitorTests(unittest.TestCase):
    def test_late_stamped_metadata_publishes_without_adopting_queue_owner(self):
        labels = {}
        main_service = import_main_service(labels)
        import play_queue

        owner = play_queue.start_session(100)
        pending, started = [], []
        main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)

        def settle(_millis):
            labels["MusicPlayer.Property(spotifytrackid)"] = "track-b"
            labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner}:track-b"
            labels["Player.FileNameAndPath"] = "http://127.0.0.1:52309/track/track-b/180.wav"

        main_service.xbmc.sleep = settle
        monitor = main_service._SpotifyOSDPlayerMonitor(started.append)
        monitor.onPlayBackStarted()
        pending[0]()
        self.assertEqual(["track-b"], started)
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        self.assertEqual("track-b", win.getProperty("Spotify.CurrentTrackId"))
        monitor.onPlayBackStopped()
        self.assertEqual(owner, play_queue.session_id())

    def test_old_metadata_owner_rejection_cannot_change_new_callback_owner(self):
        labels = {"MusicPlayer.Property(spotifytrackid)": "track-a"}
        main_service = import_main_service(labels)
        import play_queue

        owner_a = play_queue.start_session(75)
        labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_a}:track-a"
        pending = []
        main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)
        monitor = main_service._SpotifyOSDPlayerMonitor()
        monitor.onPlayBackStarted()
        owner_b = play_queue.start_session(100)
        original_read = main_service._read_player_queue_owner

        class ReplacedOwner(str):
            def __ne__(self, other):
                labels["MusicPlayer.Property(spotifytrackid)"] = "track-b"
                labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_b}:track-b"
                main_service._read_player_queue_owner = original_read
                monitor.onPlayBackStarted()
                return True

        main_service._read_player_queue_owner = lambda track: ReplacedOwner("replaced")
        pending[0]()
        monitor.onPlayBackStopped()
        self.assertEqual("", play_queue.session_id())

    def tearDown(self):
        threading.Thread = _REAL_THREAD
        FakeWindow.windows.clear()
        for module_name in (
            "main_service",
            "playlist_next",
            "like_actions",
            "play_queue",
            "xbmc",
            "xbmcaddon",
            "xbmcgui",
            "bottle_manager",
            "spotipy",
            "spotty",
            "http_spotty_audio_streamer",
            "prebuffer",
            "spotty_auth",
            "spotty_helper",
            "utils",
            "string_ids",
        ):
            sys.modules.pop(module_name, None)

    def test_missing_mismatched_and_no_session_item_owners_preserve_active_queue(self):
        for marker in ("", "{owner}:other-track", ":track-a", "{owner}"):
            with self.subTest(marker=marker):
                labels = {"MusicPlayer.Property(spotifytrackid)": "track-a"}
                main_service = import_main_service(labels)
                import play_queue

                play_queue.clear()
                owner = play_queue.start_session(100)
                labels["MusicPlayer.Property(_spotify_queue_owner)"] = marker.format(owner=owner)
                monitor = main_service._SpotifyOSDPlayerMonitor()
                monitor.onPlayBackStarted()
                monitor.onPlayBackStopped()
                self.assertEqual(owner, play_queue.session_id())
                self.assertFalse(play_queue.should_fire_autoplay())

    def test_owner_read_with_conflicting_player_file_cannot_clear_queue(self):
        labels = {
            "MusicPlayer.Property(spotifytrackid)": "track-b",
            "Player.FileNameAndPath": "http://127.0.0.1:52309/track/track-a/180.wav",
        }
        main_service = import_main_service(labels)
        import play_queue

        owner = play_queue.start_session(100)
        labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner}:track-b"
        monitor = main_service._SpotifyOSDPlayerMonitor()
        monitor.onPlayBackStarted()
        monitor.onPlayBackStopped()
        self.assertEqual(owner, play_queue.session_id())

    def test_late_owner_property_cannot_adopt_replacement_without_new_callback(self):
        for initial_owner in (False, True):
            with self.subTest(initial_owner=initial_owner):
                labels = {"MusicPlayer.Property(spotifytrackid)": "track-a"}
                main_service = import_main_service(labels)
                import play_queue

                play_queue.clear()
                owner_a = play_queue.start_session(75)
                if initial_owner:
                    labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_a}:track-a"
                pending, started = [], []
                main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)
                monitor = main_service._SpotifyOSDPlayerMonitor(started.append)
                monitor.onPlayBackStarted()
                owner_b = play_queue.start_session(100)
                labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_b}:track-b"
                labels["MusicPlayer.Property(spotifytrackid)"] = "track-b"
                pending[0]()
                self.assertEqual([], started)
                monitor.onPlayBackStopped()
                self.assertEqual(owner_b, play_queue.session_id())
                self.assertFalse(play_queue.should_fire_autoplay())

    def test_delayed_metadata_without_new_callback_rejects_same_owner_track_change(self):
        labels = {"MusicPlayer.Property(spotifytrackid)": "track-a"}
        main_service = import_main_service(labels)
        import play_queue

        owner = play_queue.start_session(100)
        labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner}:track-a"
        pending, started = [], []
        main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)
        monitor = main_service._SpotifyOSDPlayerMonitor(started.append)
        monitor.onPlayBackStarted()
        labels["MusicPlayer.Property(spotifytrackid)"] = "track-b"
        labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner}:track-b"
        pending[0]()
        self.assertEqual([], started)
        monitor.onPlayBackStopped()
        self.assertEqual(owner, play_queue.session_id())

    def test_metadata_read_that_changes_item_cannot_publish_or_clear_replacement(self):
        labels = {"MusicPlayer.Property(spotifytrackid)": "track-a"}
        main_service = import_main_service(labels)
        import play_queue

        owner_a = play_queue.start_session(75)
        labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_a}:track-a"
        pending, started = [], []
        main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)
        monitor = main_service._SpotifyOSDPlayerMonitor(started.append)
        monitor.onPlayBackStarted()
        owner_b = play_queue.start_session(100)
        read = main_service.xbmc.getInfoLabel

        def change_during_read(label):
            value = read(label)
            if label == "MusicPlayer.Property(spotifytrackid)":
                labels["MusicPlayer.Property(spotifytrackid)"] = "track-b"
                labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_b}:track-b"
            return value

        main_service.xbmc.getInfoLabel = change_during_read
        pending[0]()
        self.assertEqual([], started)
        monitor.onPlayBackStopped()
        self.assertEqual(owner_b, play_queue.session_id())

    def test_replacement_session_survives_old_terminal_callbacks(self):
        for prior_session in (False, True):
            for callback in ("onPlayBackStopped", "onPlayBackEnded", "onPlayBackError"):
                with self.subTest(prior_session=prior_session, callback=callback):
                    labels = {"MusicPlayer.Property(spotifytrackid)": "track-a"}
                    main_service = import_main_service(labels)
                    import play_queue

                    play_queue.clear()
                    if prior_session:
                        owner_a = play_queue.start_session(75)
                        labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_a}:track-a"
                    monitor = main_service._SpotifyOSDPlayerMonitor()
                    monitor.onPlayBackStarted()
                    owner_b = play_queue.start_session(100)
                    play_queue.report_loaded(50, owner_b)

                    getattr(monitor, callback)()

                    self.assertEqual(owner_b, play_queue.session_id())
                    self.assertFalse(play_queue.should_fire_autoplay())
                    win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
                    self.assertEqual("50", win.getProperty(play_queue.PROP_ORIGINAL_LOADED))
                    labels["MusicPlayer.Property(spotifytrackid)"] = "track-b"
                    labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_b}:track-b"
                    monitor.onPlayBackStarted()
                    monitor.onPlayBackStopped()
                    self.assertEqual("", play_queue.session_id())
                    self.assertEqual("", win.getProperty("Spotify.CurrentTrackId"))

    def test_delayed_metadata_checks_cannot_act_on_newer_playback(self):
        for newer_file in ("http://127.0.0.1:52309/track/track-b/180.wav", "smb://media/movie.mkv"):
            with self.subTest(newer_file=newer_file):
                labels = {}
                main_service = import_main_service(labels)
                import play_queue

                play_queue.clear()
                pending = []
                main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)
                started = []
                external = []
                monitor = main_service._SpotifyOSDPlayerMonitor(started.append, lambda: external.append(True))
                play_queue.start_session(75)
                monitor.onPlayBackStarted()
                owner_b = play_queue.start_session(100)
                labels["Player.FileNameAndPath"] = newer_file
                if newer_file.startswith("http"):
                    labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner_b}:track-b"
                monitor.onPlayBackStarted()

                pending[0]()

                self.assertEqual([], started)
                self.assertEqual([], external)
                self.assertEqual(owner_b, play_queue.session_id())
                pending[1]()
                if newer_file.startswith("http"):
                    self.assertEqual(["track-b"], started)
                    self.assertEqual(owner_b, play_queue.session_id())
                else:
                    self.assertEqual([True], external)
                    self.assertEqual(owner_b, play_queue.session_id())

    def test_metadata_timeout_cannot_clear_replacement_before_new_start(self):
        main_service = import_main_service({})
        import play_queue

        play_queue.start_session(75)
        monitor = main_service._SpotifyOSDPlayerMonitor()
        replacement = []

        def replace_during_wait(_millis):
            if not replacement:
                replacement.append(play_queue.start_session(100))

        main_service.xbmc.sleep = replace_during_wait
        monitor.onPlayBackStarted()

        self.assertEqual(replacement[0], play_queue.session_id())
        self.assertFalse(play_queue.should_fire_autoplay())

    def test_stop_cancels_pending_metadata_check_for_bound_owner(self):
        labels = {"MusicPlayer.Property(spotifytrackid)": "track-b"}
        main_service = import_main_service(labels)
        import play_queue

        owner = play_queue.start_session(100)
        labels["MusicPlayer.Property(_spotify_queue_owner)"] = f"{owner}:track-b"
        pending = []
        main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)
        monitor = main_service._SpotifyOSDPlayerMonitor()
        monitor.onPlayBackStarted()
        monitor.onPlayBackStopped()
        self.assertEqual("", play_queue.session_id())
        replacement = play_queue.start_session(100)
        pending[0]()
        self.assertNotEqual(owner, replacement)
        self.assertEqual(replacement, play_queue.session_id())

    def test_old_metadata_check_cannot_publish_with_same_queue_owner(self):
        labels = {"MusicPlayer.Property(spotifytrackid)": "track-b"}
        main_service = import_main_service(labels)
        import play_queue

        play_queue.start_session(100)
        pending = []
        started = []
        main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)
        monitor = main_service._SpotifyOSDPlayerMonitor(started.append)
        monitor.onPlayBackStarted()
        monitor.onPlayBackStarted()
        pending[1]()
        pending[0]()
        self.assertEqual(["track-b"], started)

    def test_external_teardown_cleanup_preserves_session_created_during_callback(self):
        main_service = import_main_service({"Player.FileNameAndPath": "smb://media/movie.mkv"})
        import play_queue

        play_queue.start_session(75)
        replacement = []
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)

        def replace_during_teardown():
            replacement.append(play_queue.start_session(100))
            win.setProperty("Spotify.CurrentTrackId", "track-b")

        monitor = main_service._SpotifyOSDPlayerMonitor(
            on_external_playback=replace_during_teardown
        )
        monitor.onPlayBackStarted()

        self.assertEqual(replacement[0], play_queue.session_id())
        self.assertFalse(play_queue.should_fire_autoplay())
        self.assertEqual("track-b", win.getProperty("Spotify.CurrentTrackId"))

    def test_playback_started_preserves_published_spotify_track_while_metadata_settles(self):
        main_service = import_main_service({})
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", "track-1")

        main_service._SpotifyOSDPlayerMonitor().onPlayBackStarted()

        self.assertEqual("track-1", win.getProperty("Spotify.CurrentTrackId"))

    def test_playback_started_recovers_track_id_from_active_spotify_stream_url(self):
        main_service = import_main_service(
            {"Player.FileNameAndPath": "http://127.0.0.1:52309/track/stream-track/180.wav"}
        )
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)

        main_service._SpotifyOSDPlayerMonitor().onPlayBackStarted()

        self.assertEqual("stream-track", win.getProperty("Spotify.CurrentTrackId"))

    def test_playback_started_recovers_track_id_from_music_player_property(self):
        main_service = import_main_service(
            {"MusicPlayer.Property(spotifytrackid)": "music-property-track"}
        )
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)

        main_service._SpotifyOSDPlayerMonitor().onPlayBackStarted()

        self.assertEqual("music-property-track", win.getProperty("Spotify.CurrentTrackId"))

    def test_playback_started_invokes_callback_after_spotify_metadata_settles(self):
        main_service = import_main_service(
            {"Player.FileNameAndPath": "http://127.0.0.1:52309/track/stream-track/180.wav"}
        )
        started_tracks = []

        main_service._SpotifyOSDPlayerMonitor(
            on_spotify_started=lambda track_id: started_tracks.append(track_id)
        ).onPlayBackStarted()

        self.assertEqual(["stream-track"], started_tracks)

    def test_playback_started_clears_stale_spotify_state_for_non_spotify_audio(self):
        main_service = import_main_service(
            {"Player.FileNameAndPath": "smb://media/music/example.flac"}
        )
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", "old-track")

        main_service._SpotifyOSDPlayerMonitor().onPlayBackStarted()

        self.assertEqual("", win.getProperty("Spotify.CurrentTrackId"))

    def test_playback_started_invokes_external_playback_callback_for_non_spotify(self):
        main_service = import_main_service(
            {"Player.FileNameAndPath": "smb://media/movies/example.mkv"}
        )
        teardown_called = []

        main_service._SpotifyOSDPlayerMonitor(
            on_external_playback=lambda: teardown_called.append(True)
        ).onPlayBackStarted()

        self.assertEqual(1, len(teardown_called))

    def test_prebuffer_delay_backs_off_after_error(self):
        main_service = import_main_service({})
        service = object.__new__(main_service.MainService)

        service._watch_prebuffer_result(FakeDownloader(error=True), "track-1", 5.0)

        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        self.assertEqual("7.0", win.getProperty(main_service.PREBUFFER_RELEASE_DELAY_PROP))

    def test_prebuffer_delay_trims_after_success(self):
        main_service = import_main_service({})
        service = object.__new__(main_service.MainService)

        service._watch_prebuffer_result(FakeDownloader(error=False), "track-1", 5.0)

        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        self.assertEqual("4.5", win.getProperty(main_service.PREBUFFER_RELEASE_DELAY_PROP))

    def test_proxy_failure_retries_with_one_notification(self):
        main_service = import_main_service({})
        starts = []
        main_service.bottle_manager.is_healthy = lambda: False
        main_service.bottle_manager.start_thread = lambda port: starts.append(port) or False
        main_service.bottle_manager.get_startup_error = lambda: OSError("port busy")
        service = object.__new__(main_service.MainService)

        self.assertFalse(service._MainService__ensure_proxy())
        self.assertFalse(service._MainService__ensure_proxy())

        self.assertEqual([main_service.PROXY_PORT, main_service.PROXY_PORT], starts)
        self.assertEqual(1, len(NOTIFICATIONS))

    def test_playback_time_is_read_from_active_audio_player(self):
        main_service = import_main_service({})
        calls = []

        def execute_jsonrpc(payload):
            request = __import__("json").loads(payload)
            calls.append(request["method"])
            if request["method"] == "Player.GetActivePlayers":
                return '{"result": [{"playerid": 7, "type": "audio"}]}'
            return '{"result": {"time": {"hours": 0, "minutes": 1, "seconds": 23, "milliseconds": 500}}}'

        main_service.xbmc.executeJSONRPC = execute_jsonrpc

        self.assertEqual(83.5, main_service.get_current_playback_time_seconds())
        self.assertEqual(["Player.GetActivePlayers", "Player.GetProperties"], calls)

    def test_prebuffer_waits_for_remaining_time_and_download_completion(self):
        main_service = import_main_service({})

        self.assertFalse(main_service._prebuffer_start_ready(90, True))
        self.assertFalse(main_service._prebuffer_start_ready(30, False))
        self.assertTrue(main_service._prebuffer_start_ready(30, True))
        self.assertTrue(main_service._prebuffer_start_ready(None, True))

    def test_track_started_keeps_core_state_when_worker_thread_cannot_start(self):
        main_service = import_main_service({})
        main_service.threading.Thread = FailingThread
        service = object.__new__(main_service.MainService)

        service._MainService__on_track_started("track-1", 180)

        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        self.assertEqual("track-1", win.getProperty("Spotify.CurrentTrackId"))

    def test_track_started_does_not_prebuffer_before_kodi_playback_confirms(self):
        main_service = import_main_service({}, {"prebuffer_enabled": "true"})
        main_service.threading.Thread = FailingThread
        main_service.get_next_playlist_item = lambda: (
            {"file": "http://127.0.0.1:52309/track/track-1/180.wav"},
            {"file": "http://127.0.0.1:52309/track/track-2/180.wav"},
        )
        service = object.__new__(main_service.MainService)
        service._prebuffer_token = 0
        service._prebuffer_token_lock = threading.Lock()

        service._MainService__on_track_started("track-1", 180)

        self.assertEqual(0, service._prebuffer_token)

    def test_confirmed_spotify_playback_publishes_current_track_hook(self):
        for token in ("213.560.wav", "213560ms.xwav"):
            with self.subTest(token=token):
                url = f"http://127.0.0.1:52309/track/track-1/{token}"
                main_service = import_main_service(
                    {"Player.FileNameAndPath": url},
                    {"prebuffer_enabled": "false", "spotify_autoplay": "false"},
                )
                main_service.get_next_playlist_item = lambda: ({"file": url}, None)
                service = object.__new__(main_service.MainService)
                published = []
                service._MainService__on_track_started = lambda track_id, duration: published.append(
                    (track_id, duration)
                )

                service._MainService__on_spotify_playback_started("track-1")

                self.assertEqual([("track-1", 213.56)], published)

    def test_canonical_next_track_prebuffer_receives_seconds(self):
        main_service = import_main_service({}, {"prebuffer_enabled": "true"})
        main_service.get_next_playlist_item = lambda: (
            {"file": "http://127.0.0.1:52309/track/current/180000ms.xwav"},
            {"file": "http://127.0.0.1:52309/track/next/213560ms.xwav"},
        )
        calls = []
        service = object.__new__(main_service.MainService)
        service._prebuffer_token = 0
        service._prebuffer_token_lock = threading.Lock()
        service._MainService__wait_for_prebuffer_window = lambda *args: True
        service._get_prebuffer_release_delay = lambda: 0
        service._get_bitrate_setting = lambda: "320"
        service._MainService__prebuffer_manager = types.SimpleNamespace(
            start_prebuffer=lambda track_id, duration, **kwargs: calls.append((track_id, duration))
        )
        service._watch_prebuffer_result = lambda *args: None
        cache = types.ModuleType("spotty_cache")
        cache.SpottyCacheManager = types.SimpleNamespace(find_active_downloader=lambda track_id: None)
        with patch.dict(sys.modules, {"spotty_cache": cache}), patch.object(main_service.time, "sleep"):
            service._MainService__on_spotify_playback_started("current")
        self.assertEqual([("next", 213.560)], calls)


class ServiceCacheAndFanartTests(unittest.TestCase):
    def tearDown(self):
        threading.Thread = _REAL_THREAD
        FakeWindow.windows.clear()
        for module_name in (
            "main_service",
            "playlist_next",
            "simplecache",
            "utils",
            "xbmc",
            "xbmcgui",
        ):
            sys.modules.pop(module_name, None)

    def _install_fake_simplecache(self):
        events = []

        class FakeSimpleCache:
            def __init__(self, addon_id):
                events.append("open")

            def vacuum_if_due(self):
                events.append("vacuum")
                return True

            def close(self):
                events.append("close")

        module = types.ModuleType("simplecache")
        module.SimpleCache = FakeSimpleCache
        sys.modules["simplecache"] = module
        return events

    def test_cache_vacuum_skipped_while_playing(self):
        main_service = import_main_service({})
        events = self._install_fake_simplecache()
        main_service.xbmc.Player = type("Playing", (), {"isPlaying": lambda self: True})

        self.assertFalse(main_service._vacuum_plugin_cache_if_due())
        self.assertEqual([], events)

    def test_cache_vacuum_runs_when_idle_and_closes_cache(self):
        main_service = import_main_service({})
        events = self._install_fake_simplecache()
        main_service.xbmc.Player = type("Idle", (), {"isPlaying": lambda self: False})

        self.assertTrue(main_service._vacuum_plugin_cache_if_due())
        self.assertEqual(["open", "vacuum", "close"], events)

    def test_cache_keepalive_created_refreshed_and_reused(self):
        main_service = import_main_service({})
        events = self._install_fake_simplecache()

        class FakeKeepAlive:
            def __init__(self, addon_id):
                events.append(("keepalive", addon_id))

            def refresh(self):
                events.append("refresh")
                return True

        sys.modules["simplecache"].WalKeepAlive = FakeKeepAlive
        keepalive = main_service._refresh_cache_keepalive(None)
        self.assertIs(keepalive, main_service._refresh_cache_keepalive(keepalive))
        self.assertEqual([("keepalive", main_service.ADDON_ID), "refresh", "refresh"], events)

    def _track_started_with(self, info_labels, current_item=None):
        main_service = import_main_service(info_labels)
        api_calls = []

        class Client:
            def __init__(self, auth=None):
                pass

            def track(self, track_id):
                api_calls.append("track")
                return {"artists": [{"id": "a1"}]}

            def artist(self, artist_id):
                api_calls.append("artist")
                return {"images": [{"url": "https://api.example/a1.jpg"}]}

            def current_user_saved_tracks_contains(self, ids):
                return [False]

        main_service.spotipy.Spotify = Client
        main_service.get_cached_auth_token = lambda: "token"
        main_service.get_next_playlist_item = lambda: (current_item, None)
        service = object.__new__(main_service.MainService)
        service._MainService__on_track_started("track-1", 180)
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        return api_calls, win.getProperty("Spotify.ArtistFanartCurrent")

    def test_artist_fanart_read_from_player_art_without_api_calls(self):
        api_calls, fanart = self._track_started_with(
            {
                "MusicPlayer.Property(spotifytrackid)": "track-1",
                "Player.Art(artist.fanart)": "https://i.example/artist.jpg",
            }
        )
        self.assertEqual([], api_calls)
        self.assertEqual("https://i.example/artist.jpg", fanart)

    def test_artist_fanart_read_from_queued_item_art(self):
        item = {
            "file": "http://127.0.0.1:52309/track/track-1/180.wav",
            "art": {"artist.fanart": "image://https%3a%2f%2fi.example%2fq.jpg/"},
        }
        api_calls, fanart = self._track_started_with({}, current_item=item)
        self.assertEqual([], api_calls)
        self.assertEqual("https://i.example/q.jpg", fanart)

    def test_artist_fanart_falls_back_to_api(self):
        api_calls, fanart = self._track_started_with({})
        self.assertEqual(["track", "artist"], api_calls)
        self.assertEqual("https://api.example/a1.jpg", fanart)


class BareNowPlayingTagTests(unittest.TestCase):
    """A skin's PlayMedia(<stream url>) plays the track without the plugin's
    ListItem; the OSD then showed the URL's file name (the duration)."""

    URL = "http://127.0.0.1:52309/track/track-1/238.wav"

    def tearDown(self):
        threading.Thread = _REAL_THREAD
        FakeWindow.windows.clear()
        for module_name in ("main_service", "playlist_next", "utils", "xbmc", "xbmcgui", "spotipy"):
            sys.modules.pop(module_name, None)

    def _start(self, info_labels, api_track=None):
        main_service = import_main_service(info_labels)
        updates, api_calls = [], []
        probes = self.probes = []

        class Tag:
            def __init__(self):
                self.values = {}

            def __getattr__(self, name):
                if name.startswith("set"):
                    return lambda value: self.values.__setitem__(name[3:].lower(), value)
                raise AttributeError(name)

        class ListItem:
            def __init__(self, label="", path="", offscreen=False):
                self.label, self.path = label, path
                self.tag, self.art, self.props = Tag(), {}, {}
                self.mime_type, self.content_lookup = "", True

            def getMusicInfoTag(self):
                return self.tag

            def setArt(self, art):
                self.art = art

            def setProperty(self, key, value):
                self.props[key] = value

            def setMimeType(self, mime_type):
                self.mime_type = mime_type

            def setContentLookup(self, enabled):
                self.content_lookup = enabled

        class Player:
            def updateInfoTag(self, item):
                # Kodi's CFileItem::UpdateInfo ends with
                # UpdateMimeType(m_doContentLookup): an http item without a
                # mime type is probed with a blocking HTTP request on the
                # calling thread. The proxy answering that probe runs in this
                # Python runtime, so the probe stalls until curl times out.
                if item.content_lookup and not item.mime_type and item.path.startswith("http"):
                    probes.append(item.path)
                updates.append(item)

        class Client:
            def __init__(self, auth=None):
                pass

            def track(self, track_id):
                api_calls.append("track")
                return api_track

            def artist(self, artist_id):
                api_calls.append("artist")
                return {"images": []}

            def current_user_saved_tracks_contains(self, ids):
                return [False]

        main_service.xbmcgui.ListItem = ListItem
        main_service.xbmc.Player = Player
        main_service.spotipy.Spotify = Client
        main_service.get_cached_auth_token = lambda: "token"
        main_service.get_next_playlist_item = lambda: (None, None)
        service = object.__new__(main_service.MainService)
        service._MainService__on_track_started("track-1", 238)
        return updates, api_calls

    TRACK = {
        "name": "Rich Baby Daddy",
        "artists": [{"id": "a1", "name": "Drake"}, {"id": "a2", "name": "Sexyy Red"}],
        "album": {
            "name": "For All The Dogs",
            "release_date": "2023-10-06",
            "images": [{"url": "https://i.example/640.jpg"}],
        },
        "duration_ms": 319191,
        "track_number": 23,
        "disc_number": 1,
    }

    def test_bare_stream_gets_title_artist_album_and_art(self):
        updates, api_calls = self._start({"Player.FilenameAndPath": self.URL}, self.TRACK)
        self.assertEqual(1, len(updates))
        item = updates[0]
        self.assertEqual(self.URL, item.path)
        values = item.tag.values
        self.assertEqual("Rich Baby Daddy", values["title"])
        self.assertEqual("Drake, Sexyy Red", values["artist"])
        self.assertEqual("For All The Dogs", values["album"])
        self.assertEqual(320, values["duration"])
        self.assertEqual(2023, values["year"])
        self.assertEqual("song", values["mediatype"])
        self.assertEqual("https://i.example/640.jpg", item.art["thumb"])
        self.assertEqual("track-1", item.props["spotifytrackid"])
        self.assertEqual(":track-1", item.props.get("_spotify_queue_owner"))
        self.assertEqual(1, api_calls.count("track"))

    def test_restored_tag_never_makes_kodi_probe_the_local_stream(self):
        """Device run 2026-10-14: the probe froze every Spotify thread for 40 s."""
        updates, _api_calls = self._start({"Player.FilenameAndPath": self.URL}, self.TRACK)
        self.assertEqual(1, len(updates))
        self.assertEqual([], self.probes)
        self.assertEqual("audio/x-wav", updates[0].mime_type)
        self.assertFalse(updates[0].content_lookup)

    def test_plugin_listitem_playback_is_left_alone(self):
        updates, api_calls = self._start(
            {
                "Player.FilenameAndPath": self.URL,
                "MusicPlayer.Property(spotifytrackid)": "track-1",
                "Player.Art(artist.fanart)": "https://i.example/artist.jpg",
            },
            self.TRACK,
        )
        self.assertEqual([], updates)
        self.assertEqual([], api_calls)

    def test_track_changed_before_spotify_answered_is_not_tagged(self):
        updates, _ = self._start(
            {"Player.FilenameAndPath": "http://127.0.0.1:52309/track/track-2/200.wav"},
            self.TRACK,
        )
        self.assertEqual([], updates)


class ToggleLikeTests(unittest.TestCase):
    def tearDown(self):
        threading.Thread = _REAL_THREAD
        FakeWindow.windows.clear()
        FakeSpotify.instance = None
        for module_name in (
            "main_service",
            "playlist_next",
            "like_actions",
            "xbmc",
            "xbmcaddon",
            "xbmcgui",
            "bottle_manager",
            "spotipy",
            "spotty",
            "http_spotty_audio_streamer",
            "prebuffer",
            "spotty_auth",
            "spotty_helper",
            "utils",
            "string_ids",
        ):
            sys.modules.pop(module_name, None)

    @staticmethod
    def _toggle(main_service):
        main_service._SpotifyOSDServiceMonitor._handle_toggle_like()

    def test_failed_unlike_after_track_change_leaves_next_track_unliked(self):
        main_service = import_main_service({})
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", "track-a")
        win.setProperty("Spotify.CurrentTrackLiked", "true")

        def advance_then_fail():
            win.setProperty("Spotify.CurrentTrackId", "track-b")
            win.clearProperty("Spotify.CurrentTrackLiked")
            raise RuntimeError("Spotify unavailable")

        FakeSpotify.instance = FakeSpotify(on_delete=advance_then_fail)
        self._toggle(main_service)

        self.assertEqual(["track-a"], FakeSpotify.instance.deleted)
        self.assertEqual("", win.getProperty("Spotify.CurrentTrackLiked"))

    def test_failed_unlike_on_same_track_restores_liked_icon(self):
        main_service = import_main_service({})
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", "track-a")
        win.setProperty("Spotify.CurrentTrackLiked", "true")

        def fail():
            raise RuntimeError("Spotify unavailable")

        FakeSpotify.instance = FakeSpotify(on_delete=fail)
        self._toggle(main_service)

        self.assertEqual("true", win.getProperty("Spotify.CurrentTrackLiked"))

    def test_set_liked_notification_only_updates_the_playing_track(self):
        main_service = import_main_service({})
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", "track-b")
        monitor = object.__new__(main_service._SpotifyOSDServiceMonitor)
        sender = "plugin.audio.spotifykodiconnect"

        monitor.onNotification(sender, "Other.SetLiked", '{"track_id": "track-a", "liked": true}')
        self.assertEqual("", win.getProperty("Spotify.CurrentTrackLiked"))

        monitor.onNotification(sender, "Other.SetLiked", '{"track_id": "track-b", "liked": true}')
        self.assertEqual("true", win.getProperty("Spotify.CurrentTrackLiked"))

        monitor.onNotification(sender, "Other.SetLiked", "not json")
        self.assertEqual("true", win.getProperty("Spotify.CurrentTrackLiked"))

    def test_liked_lookup_finishing_after_track_change_leaves_next_track_alone(self):
        main_service = import_main_service({})
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)

        class Client:
            def __init__(self, auth=None):
                pass

            def track(self, track_id):
                return {}

            def current_user_saved_tracks_contains(self, ids):
                win.setProperty("Spotify.CurrentTrackId", "track-b")
                return [True]

        main_service.spotipy.Spotify = Client
        main_service.get_cached_auth_token = lambda: "token"
        main_service.get_next_playlist_item = lambda: (None, None)
        service = object.__new__(main_service.MainService)
        service._MainService__on_track_started("track-a", 180)

        self.assertEqual("track-b", win.getProperty("Spotify.CurrentTrackId"))
        self.assertEqual("", win.getProperty("Spotify.CurrentTrackLiked"))

    def test_delayed_hook_refresh_of_skipped_track_keeps_new_track_id(self):
        main_service = import_main_service({})
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        main_service.get_next_playlist_item = lambda: (None, None)
        main_service._liked_state_track_id = "track-b"
        win.setProperty("Spotify.CurrentTrackId", "track-b")

        main_service._refresh_playback_hooks("track-a", delay_ms=1000)

        self.assertEqual("track-b", win.getProperty("Spotify.CurrentTrackId"))

    def test_toggle_without_valid_token_notifies_immediately(self):
        main_service = import_main_service({})
        main_service.get_valid_cached_auth_token = lambda: ""
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", "track-a")
        main_service.xbmc.sleep = lambda millis: self.fail("must not wait for a token")

        self._toggle(main_service)

        self.assertEqual([("SpotifyKodiConnect", "string-11150")], NOTIFICATIONS)
        self.assertEqual("", win.getProperty("Spotify.CurrentTrackLiked"))


class TokenRenewBackoffTests(unittest.TestCase):
    def tearDown(self):
        threading.Thread = _REAL_THREAD
        for module_name in ("main_service", "playlist_next", "utils", "xbmc", "xbmcgui"):
            sys.modules.pop(module_name, None)

    def test_backoff_doubles_from_five_seconds_and_caps_at_300(self):
        main_service = import_main_service({})
        delays = [main_service.MainService.compute_renew_backoff(n) for n in range(10)]
        self.assertEqual([5, 10, 20, 40, 80, 160, 300, 300, 300, 300], delays)

    def test_failed_renew_schedules_next_attempt_and_success_resets(self):
        main_service = import_main_service({})
        service = object.__new__(main_service.MainService)
        service._MainService__renew_failures = 0
        service._MainService__next_renew_attempt_at = 0.0
        service._MainService__welcome_msg = False

        class _FailingAuth:
            def renew_token(self):
                raise RuntimeError("offline")

        service._MainService__spotty_auth = _FailingAuth()
        main_service.time.time = lambda: 1000.0
        try:
            service._MainService__renew_token()
            self.assertEqual(1005.0, service._MainService__next_renew_attempt_at)
            service._MainService__renew_token()
            self.assertEqual(1010.0, service._MainService__next_renew_attempt_at)
            self.assertEqual(2, service._MainService__renew_failures)

            class _OkAuth:
                def renew_token(self):
                    pass

            service._MainService__spotty_auth = _OkAuth()
            main_service.utils.get_cached_auth_token_expires_at = lambda: "4600"
            service._MainService__renew_token()
            self.assertEqual(0, service._MainService__renew_failures)
            self.assertEqual(0.0, service._MainService__next_renew_attempt_at)
            self.assertEqual("4600", service._MainService__auth_token_expires_at)
        finally:
            import time as _time

            main_service.time.time = _time.time

    def test_failed_renewals_are_published_and_cleared_on_success(self):
        main_service = import_main_service({})
        published = []
        main_service.utils.set_auth_renew_failing = published.append
        service = object.__new__(main_service.MainService)
        service._MainService__renew_failures = 0
        service._MainService__next_renew_attempt_at = 0.0
        service._MainService__welcome_msg = False

        class _FailingAuth:
            def renew_token(self):
                raise RuntimeError("revoked")

        class _OkAuth:
            def renew_token(self):
                pass

        service._MainService__spotty_auth = _FailingAuth()
        service._MainService__renew_token()
        service._MainService__renew_token()
        service._MainService__spotty_auth = _OkAuth()
        main_service.utils.get_cached_auth_token_expires_at = lambda: "4600"
        service._MainService__renew_token()
        self.assertEqual([1, 2, 0], published)

    def test_expired_token_is_cleared_without_touching_renewal_backoff(self):
        main_service = import_main_service({})
        cleared = []
        main_service.utils.clear_expired_cached_auth_token = lambda now=None: (
            cleared.append(1) or True
        )
        service = object.__new__(main_service.MainService)
        service._MainService__auth_token_expires_at = "1000"
        service._MainService__renew_failures = 3
        service._MainService__next_renew_attempt_at = 5000.0

        service._MainService__drop_expired_token()

        self.assertEqual([1], cleared)
        # The renewal loop still sees the old expiry (so it renews once the
        # back-off window ends) and the back-off schedule is unchanged.
        self.assertEqual("1000", service._MainService__auth_token_expires_at)
        self.assertEqual(3, service._MainService__renew_failures)
        self.assertEqual(5000.0, service._MainService__next_renew_attempt_at)

    def test_drop_expired_token_survives_helper_errors(self):
        main_service = import_main_service({})

        def boom(now=None):
            raise RuntimeError("window gone")

        main_service.utils.clear_expired_cached_auth_token = boom
        service = object.__new__(main_service.MainService)
        service._MainService__drop_expired_token()


class SpotifyAutoplayGatingTests(unittest.TestCase):
    """Regression coverage for the daylist autoplay-destroys-queue bug.

    The plugin's play_playlist opens a play-queue session and pages the rest
    of the original playlist in the background. The service's
    __on_spotify_playback_started must NOT fire autoplay while that paging
    is in progress, even when Kodi reports "no next item" mid-load.
    """

    def tearDown(self):
        threading.Thread = _REAL_THREAD
        FakeWindow.windows.clear()
        for module_name in (
            "main_service",
            "playlist_next",
            "play_queue",
            "xbmc",
            "xbmcaddon",
            "xbmcgui",
            "bottle_manager",
            "spotipy",
            "spotty",
            "http_spotty_audio_streamer",
            "prebuffer",
            "spotty_auth",
            "spotty_helper",
            "utils",
            "string_ids",
        ):
            sys.modules.pop(module_name, None)

    def _build_service(self, main_service, autoplay_enabled=True):
        """Create a minimal MainService shell with autoplay-relevant state.

        Returns (service, fired_list) where fired_list is appended to each
        time _start_daemon_thread would have fired the autoplay task.
        """
        # Simulate "no next item" — this is the branch where autoplay is
        # considered. The plugin process is still paging, Kodi's playlist
        # is briefly empty at the playback-order tail, etc.
        main_service.get_next_playlist_item = lambda: ({"file": ""}, None)

        fired = []

        def _capture_start(target, args=(), task_name=""):
            # Only the autoplay task interests us; other deferred workers
            # (prebuffer, liked-state refresh) aren't fired from this branch.
            if "autoplay" in task_name:
                fired.append((task_name, args))

        main_service._start_daemon_thread = _capture_start

        service = object.__new__(main_service.MainService)
        # __on_spotify_playback_started passes self.__queue_autoplay_tracks
        # to _start_daemon_thread; provide a no-op so the captured callable
        # can be invoked without exploding if a test actually runs it.
        service._MainService__queue_autoplay_tracks = lambda seed: None
        return service, fired

    def test_autoplay_does_not_fire_while_original_playlist_is_loading(self):
        main_service = import_main_service(
            {"Player.FileNameAndPath": "http://127.0.0.1:52309/track/seed/180.wav"},
            {"spotify_autoplay": "true"},
        )
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        # Plugin has opened a session but paging isn't finished.
        win.setProperty("Spotify.PlayQueue.SessionId", "test-session")
        win.setProperty("Spotify.PlayQueue.OriginalComplete", "false")

        service, fired = self._build_service(main_service)
        service._MainService__on_spotify_playback_started("seed")

        self.assertEqual([], fired, "Autoplay must not fire while original is still loading")

    def test_autoplay_fires_when_original_playlist_marked_complete(self):
        main_service = import_main_service(
            {"Player.FileNameAndPath": "http://127.0.0.1:52309/track/seed/180.wav"},
            {"spotify_autoplay": "true"},
        )
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.PlayQueue.SessionId", "test-session")
        win.setProperty("Spotify.PlayQueue.OriginalComplete", "true")
        win.setProperty("Spotify.PlayQueue.AutoplayFetched", "false")

        service, fired = self._build_service(main_service)
        service._MainService__on_spotify_playback_started("seed")

        self.assertEqual(1, len(fired), "Autoplay should fire once original is complete")
        self.assertEqual(("seed",), fired[0][1])

    def test_autoplay_does_not_fire_twice_for_same_session(self):
        """Multiple onPlayBackStarted callbacks (e.g. Kodi re-buffering) must
        not trigger multiple autoplay fetches for the same session."""
        main_service = import_main_service(
            {"Player.FileNameAndPath": "http://127.0.0.1:52309/track/seed/180.wav"},
            {"spotify_autoplay": "true"},
        )
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.PlayQueue.SessionId", "test-session")
        win.setProperty("Spotify.PlayQueue.OriginalComplete", "true")

        service, fired = self._build_service(main_service)
        for _ in range(4):
            service._MainService__on_spotify_playback_started("seed")

        self.assertEqual(1, len(fired), "Subsequent callbacks must see the claimed slot")

    def test_autoplay_fires_when_no_session_active(self):
        """Single-track playback (no play_playlist call) opens no session.
        The legacy autoplay path must still work — fall back to firing."""
        main_service = import_main_service(
            {"Player.FileNameAndPath": "http://127.0.0.1:52309/track/seed/180.wav"},
            {"spotify_autoplay": "true"},
        )

        service, fired = self._build_service(main_service)
        service._MainService__on_spotify_playback_started("seed")

        self.assertEqual(1, len(fired), "No session → legacy autoplay fires")

    def test_autoplay_respects_disabled_setting_even_when_complete(self):
        main_service = import_main_service(
            {"Player.FileNameAndPath": "http://127.0.0.1:52309/track/seed/180.wav"},
            {"spotify_autoplay": "false"},
        )
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.PlayQueue.SessionId", "test-session")
        win.setProperty("Spotify.PlayQueue.OriginalComplete", "true")

        service, fired = self._build_service(main_service)
        service._MainService__on_spotify_playback_started("seed")

        self.assertEqual([], fired, "User-disabled autoplay must never fire")

    def test_autoplay_builds_items_from_recommendation_objects(self):
        """/recommendations returns full track objects: no /tracks refetch."""
        main_service = import_main_service({})
        calls = []

        def _track(index):
            return {
                "id": f"rec-{index}",
                "name": f"Rec {index}",
                "duration_ms": 213560,
                "artists": [{"id": "a", "name": f"Artist {index}"}],
                "album": {"name": "Album", "images": [{"url": f"img-{index}"}]},
            }

        class _Spotify:
            def recommendations(self, seed_tracks=None, limit=20):
                calls.append(("recommendations", tuple(seed_tracks), limit))
                return {"tracks": [_track(1), _track(2), _track(1), _track(3), None]}

            def tracks(self, *args, **kwargs):
                calls.append(("tracks",))
                raise AssertionError("must not refetch recommended tracks")

            def track(self, *args, **kwargs):
                calls.append(("track",))
                raise AssertionError("must not refetch recommended tracks")

        added = []

        class _PlayList:
            def __init__(self, _kind):
                pass

            def add(self, url, li):
                added.append((url, li))

        class _ListItem:
            def __init__(self, label=""):
                self.label = label
                self.props = {}
                self.info = {}
                self.art = {}

            def setProperty(self, key, value):
                self.props[key] = value

            def setInfo(self, _kind, info):
                self.info = info

            def setArt(self, art):
                self.art = art

            def setMimeType(self, mime_type):
                self.mime_type = mime_type

            def setContentLookup(self, enabled):
                self.content_lookup = enabled

        main_service.get_cached_auth_token = lambda: "token"
        main_service.spotipy.Spotify = lambda auth=None: _Spotify()
        main_service.get_playlist_track_ids = lambda: {"rec-2"}
        main_service.xbmc.PLAYLIST_MUSIC = 0
        main_service.xbmc.PlayList = _PlayList
        main_service.xbmcgui.ListItem = _ListItem

        service = object.__new__(main_service.MainService)
        service._MainService__queue_autoplay_tracks("seed")

        self.assertEqual([("recommendations", ("seed",), 49)], calls)
        self.assertEqual(
            [
                "http://127.0.0.1:52309/track/rec-1/213560ms.xwav",
                "http://127.0.0.1:52309/track/rec-3/213560ms.xwav",
            ],
            [url for url, _li in added],
        )
        first = added[0][1]
        self.assertEqual("rec-1", first.props["spotifytrackid"])
        self.assertEqual(":rec-1", first.props.get("_spotify_queue_owner"))
        self.assertEqual("Artist 1", first.info["artist"])
        self.assertEqual("Album", first.info["album"])
        self.assertEqual("img-1", first.art["thumb"])
        # Kodi must open queued items without probing the local stream first.
        self.assertEqual("audio/x-wav", first.mime_type)
        self.assertFalse(first.content_lookup)

        from test_playlist_fastpath import import_plugin_content

        with patch.dict(sys.modules), patch.object(sys, "argv", sys.argv[:]), patch.object(threading, "Thread", _REAL_THREAD):
            sys.modules.pop("string_ids", None)
            plugin_content = import_plugin_content()
            content = object.__new__(plugin_content.PluginContent)
            for url, _li in added:
                track = _track(1 if "/rec-1/" in url else 3)
                browse_url, browse_item = content._PluginContent__get_track_item(track)
                self.assertEqual(url, browse_url)
                self.assertEqual("audio/x-wav", browse_item.mime_type)
                self.assertFalse(browse_item.content_lookup)

    def test_osd_clear_wipes_play_queue_session_state(self):
        """A current manual stop clears the queue and allows ordinary autoplay."""
        main_service = import_main_service(
            {
                "MusicPlayer.Property(spotifytrackid)": "track-a",
                "MusicPlayer.Property(_spotify_queue_owner)": "stale-session:track-a",
            }
        )
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.PlayQueue.SessionId", "stale-session")
        win.setProperty("Spotify.PlayQueue.OriginalComplete", "false")
        win.setProperty("Spotify.PlayQueue.AutoplayFetched", "true")

        monitor = main_service._SpotifyOSDPlayerMonitor()
        monitor.onPlayBackStarted()
        monitor.onPlayBackStopped()

        self.assertEqual("", win.getProperty("Spotify.PlayQueue.SessionId"))
        self.assertEqual("", win.getProperty("Spotify.PlayQueue.OriginalComplete"))
        self.assertEqual("", win.getProperty("Spotify.PlayQueue.AutoplayFetched"))


class PlaybackStopReleaseTests(unittest.TestCase):
    """Device run 2026-10-14: after Stop, the stopped track's Spotty process and
    stream state stayed alive until the next selection, so a replay of the same
    song inherited them."""

    def tearDown(self):
        threading.Thread = _REAL_THREAD
        FakeWindow.windows.clear()
        for module_name in ("main_service", "playlist_next", "play_queue", "spotty_cache"):
            sys.modules.pop(module_name, None)

    def _service(self, playing=False, stream_held=True):
        main_service = import_main_service({})
        events = []

        class Player:
            def isPlaying(self):
                return playing

        class Streamer:
            def stop(self):
                events.append("stop_stream")

            def release_stream(self, release_resources):
                events.append("release_stream")
                release_resources(None)
                return stream_held

        class Prebuffer:
            def cancel_prebuffer(self):
                events.append("cancel_prebuffer")

        class Helper:
            def kill_all_spotties(self):
                events.append("kill_all_spotties")

        class Manager:
            @staticmethod
            def retain_after_stop(spec):
                events.append("retain_after_stop")

            @staticmethod
            def cleanup_all(shutdown=False):
                events.append("shutdown_cache" if shutdown else "cleanup_all")

        spotty_cache = types.ModuleType("spotty_cache")
        spotty_cache.SpottyCacheManager = Manager
        sys.modules["spotty_cache"] = spotty_cache
        main_service.xbmc.Player = Player
        service = object.__new__(main_service.MainService)
        service._MainService__http_spotty_streamer = Streamer()
        service._MainService__prebuffer_manager = Prebuffer()
        service._MainService__spotty_helper = Helper()
        service._prebuffer_token = 7
        service._prebuffer_token_lock = threading.Lock()
        return main_service, service, events

    def test_stop_releases_stream_downloaders_and_pending_prebuffer(self):
        _main_service, service, events = self._service()

        service._release_after_stop()

        # Spotify login and token processes are not stream processes: keep them.
        self.assertEqual(["release_stream", "cancel_prebuffer", "retain_after_stop"], events)
        # A deferred prebuffer for the stopped track must not start afterwards.
        self.assertEqual(8, service._prebuffer_token)

    def test_shutdown_cancels_deferred_prebuffer_before_cache_cleanup(self):
        main_service, service, events = self._service()
        main_service.bottle_manager.stop_thread = lambda: events.append("stop_http")
        main_service.SPOTIFY_ADDON.getSetting = lambda key: "true" if key == "prebuffer_enabled" else ""
        main_service.get_next_playlist_item = lambda: (
            {"file": "http://127.0.0.1:52309/track/current/180000ms.xwav"},
            {"file": "http://127.0.0.1:52309/track/next/180000ms.xwav"},
        )
        pending = []
        main_service._start_daemon_thread = lambda target, **kwargs: pending.append(target)
        service._MainService__on_track_started = lambda *args: None
        service._MainService__wait_for_prebuffer_window = lambda *args: True
        service._get_prebuffer_release_delay = lambda: 0
        service._watch_prebuffer_result = lambda *args: None
        service._MainService__prebuffer_manager.start_prebuffer = (
            lambda *args, **kwargs: events.append("start_prebuffer")
        )
        sys.modules["spotty_cache"].SpottyCacheManager.find_active_downloader = lambda track_id: None
        service._MainService__on_spotify_playback_started("current")
        self.assertEqual(1, len(pending))

        service._MainService__close()
        with patch.object(main_service.time, "sleep"):
            pending[0]()

        self.assertEqual(9, service._prebuffer_token)
        self.assertEqual(
            ["cancel_prebuffer", "stop_stream", "shutdown_cache", "kill_all_spotties", "stop_http"],
            events,
        )

    def test_late_stop_callback_leaves_replacement_playback_alone(self):
        _main_service, service, events = self._service(playing=True)

        service._release_after_stop()

        self.assertEqual([], events)
        self.assertEqual(7, service._prebuffer_token)

    def test_external_playback_teardown_also_kills_untracked_spotty_processes(self):
        _main_service, service, events = self._service()

        service._teardown_for_external_playback()

        self.assertEqual(
            ["release_stream", "cancel_prebuffer", "cleanup_all", "kill_all_spotties"],
            events,
        )

    def test_switch_to_a_movie_tears_down_spotify_after_the_stop_callback(self):
        """Spotify -> Plex: Kodi closes PAPlayer first, so the Stop callback clears
        Spotify.CurrentTrackId before the movie's start check runs, and VideoPlayer
        already plays when the Stop callback arrives."""
        main_service, service, events = self._service(playing=True)
        win = main_service.xbmcgui.Window(main_service.ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", "track-a")
        monitor = main_service._SpotifyOSDPlayerMonitor(
            on_external_playback=service._teardown_for_external_playback,
            on_stopped=service._release_after_stop,
        )

        monitor.onPlayBackStopped()
        service._teardown_for_external_playback()

        self.assertEqual(
            ["release_stream", "cancel_prebuffer", "cleanup_all", "kill_all_spotties"],
            events,
        )

    def test_movie_start_without_spotify_stream_keeps_spotty_processes(self):
        _main_service, service, events = self._service(stream_held=False)

        service._teardown_for_external_playback()

        self.assertNotIn("kill_all_spotties", events)

    def test_only_a_user_stop_releases_spotify_audio(self):
        main_service = import_main_service({})
        stopped = []
        monitor = main_service._SpotifyOSDPlayerMonitor(on_stopped=lambda: stopped.append(True))

        monitor.onPlayBackEnded()
        monitor.onPlayBackError()
        self.assertEqual([], stopped)
        monitor.onPlayBackStopped()
        self.assertEqual([True], stopped)


if __name__ == "__main__":
    unittest.main()
