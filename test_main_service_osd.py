import os
import sys
import threading
import types
import unittest

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")


FAKE_SETTINGS = {}


class FakeAddon:
    def getAddonInfo(self, key):
        if key == "version":
            return "test"
        return "SpotifyKodiConnect"

    def getSetting(self, key):
        return FAKE_SETTINGS.get(key, "")


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


class FakeDownloader:
    def __init__(self, error=False, aborted=False):
        self.cond = threading.Condition()
        self.is_finished = True
        self.error = error
        self.aborted = aborted


def install_stubs(info_labels, settings=None):
    FAKE_SETTINGS.clear()
    FAKE_SETTINGS.update(settings or {})

    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
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
    sys.modules["xbmcgui"] = xbmcgui

    bottle_manager = types.ModuleType("bottle_manager")
    bottle_manager.route_all = lambda streamer: None
    sys.modules["bottle_manager"] = bottle_manager

    spotipy = types.ModuleType("spotipy")
    spotipy.Spotify = lambda auth=None: None
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
    utils.PROXY_HOST = "127.0.0.1"
    utils.PROXY_PORT = 52309
    utils.get_cached_auth_token = lambda: None
    utils.log_exception = lambda *args, **kwargs: None
    utils.log_msg = lambda *args, **kwargs: None
    sys.modules["utils"] = utils

    string_ids = types.ModuleType("string_ids")
    string_ids.WELCOME_AUTHENTICATED_STR_ID = 0
    sys.modules["string_ids"] = string_ids


def import_main_service(info_labels, settings=None):
    install_stubs(info_labels, settings=settings)
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    for module_name in ("main_service", "playlist_next"):
        sys.modules.pop(module_name, None)
    import main_service

    main_service.threading.Thread = ImmediateThread
    return main_service


class SpotifyOSDPlayerMonitorTests(unittest.TestCase):
    def tearDown(self):
        FakeWindow.windows.clear()
        for module_name in (
            "main_service",
            "playlist_next",
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


if __name__ == "__main__":
    unittest.main()
