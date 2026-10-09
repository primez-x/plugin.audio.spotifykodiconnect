"""Plugin-side auth gating: never start a destructive zeroconf re-pair (or show
dialogs) just because the service has not published a token yet."""

import time
import unittest

import test_playlist_fastpath as fp


class RecordingDialog:
    notifications = []
    yesno_answer = False
    yesno_calls = []

    def yesno(self, *args, **kwargs):
        RecordingDialog.yesno_calls.append(args)
        return RecordingDialog.yesno_answer

    def notification(self, *args, **kwargs):
        RecordingDialog.notifications.append(args)

    def ok(self, *args, **kwargs):
        raise AssertionError("modal dialogs must not be shown")


class PluginAuthGatingTests(unittest.TestCase):
    def setUp(self):
        self.pc = fp.import_plugin_content()
        fp.FakeWindow.windows.clear()
        RecordingDialog.notifications = []
        RecordingDialog.yesno_answer = False
        RecordingDialog.yesno_calls = []
        self.pc.xbmcgui.Dialog = RecordingDialog
        self.end_calls = []
        self.pc.xbmcplugin.endOfDirectory = lambda *a, **kw: self.end_calls.append(kw)
        original_get_token = self.pc.utils.get_valid_cached_auth_token
        self.addCleanup(setattr, self.pc.utils, "get_valid_cached_auth_token", original_get_token)
        self.pc.utils.get_valid_cached_auth_token = lambda: ""
        self.renew_failing = False
        original_failing = self.pc.utils.auth_renew_failing
        self.addCleanup(setattr, self.pc.utils, "auth_renew_failing", original_failing)
        self.pc.utils.auth_renew_failing = lambda: self.renew_failing
        self.auth_calls = []

    def build(self, *, action="", handle=1, folder="", is_media=True, creds=True):
        content = object.__new__(self.pc.PluginContent)
        content._PluginContent__addon = fp.FakeAddon()
        content._PluginContent__addon_handle = handle
        content._PluginContent__action = action
        content._PluginContent__spotipy = None
        content.authenticate_plugin_after_login_failure = lambda: self.auth_calls.append(1)
        self.pc.PluginContent._PluginContent__has_stored_credentials = staticmethod(lambda: creds)
        self.pc.xbmc.getInfoLabel = lambda label: folder
        self.pc.xbmc.getCondVisibility = lambda cond: is_media
        return content

    def test_credentials_exist_notifies_connecting_without_pairing(self):
        content = self.build(action="browse_main_library", creds=True)
        self.assertFalse(content.check_auth_and_refresh_spotipy())
        self.assertEqual([], self.auth_calls)
        self.assertEqual(1, len(RecordingDialog.notifications))
        self.assertIn(f"str-{self.pc.SPOTIFY_CONNECTING_STR_ID}", RecordingDialog.notifications[0])
        self.assertEqual([{"handle": 1, "succeeded": False}], self.end_calls)

    def test_root_menu_while_service_connecting_notifies_without_asking(self):
        content = self.build(creds=True)
        self.assertFalse(content.check_auth_and_refresh_spotipy())
        self.assertEqual([], RecordingDialog.yesno_calls)
        self.assertEqual([], self.auth_calls)
        self.assertEqual(1, len(RecordingDialog.notifications))
        self.assertIn(f"str-{self.pc.SPOTIFY_CONNECTING_STR_ID}", RecordingDialog.notifications[0])
        self.assertEqual([{"handle": 1, "succeeded": False}], self.end_calls)

    def test_root_menu_with_credentials_asks_before_pairing(self):
        self.renew_failing = True
        content = self.build(creds=True)
        self.assertFalse(content.check_auth_and_refresh_spotipy())
        self.assertEqual(1, len(RecordingDialog.yesno_calls))
        self.assertEqual([], self.auth_calls)
        self.assertEqual([{"handle": 1, "succeeded": False}], self.end_calls)

    def test_root_menu_with_credentials_pairs_when_confirmed(self):
        RecordingDialog.yesno_answer = True
        self.renew_failing = True
        content = self.build(creds=True)
        content.check_auth_and_refresh_spotipy()
        self.assertEqual([1], self.auth_calls)

    def test_widget_with_credentials_never_asks(self):
        self.renew_failing = True
        content = self.build(creds=True, folder="", is_media=False)
        content.check_auth_and_refresh_spotipy()
        self.assertEqual([], RecordingDialog.yesno_calls)

    def test_widget_without_credentials_is_silent(self):
        content = self.build(creds=False, folder="", is_media=False)
        self.assertFalse(content.check_auth_and_refresh_spotipy())
        self.assertEqual([], self.auth_calls)
        self.assertEqual([], RecordingDialog.notifications)
        self.assertEqual([{"handle": 1, "succeeded": False}], self.end_calls)

    def test_widget_with_credentials_is_silent(self):
        content = self.build(creds=True, folder="", is_media=False)
        self.assertFalse(content.check_auth_and_refresh_spotipy())
        self.assertEqual([], RecordingDialog.notifications)

    def test_no_handle_request_is_non_interactive(self):
        content = self.build(creds=False, handle=-1)
        self.assertFalse(content.check_auth_and_refresh_spotipy())
        self.assertEqual([], self.auth_calls)
        self.assertEqual([], self.end_calls)

    def test_foreground_without_any_credentials_starts_pairing(self):
        content = self.build(creds=False, folder="addons://sources/audio/", is_media=True)
        content.check_auth_and_refresh_spotipy()
        self.assertEqual([1], self.auth_calls)

    def test_inside_addon_folder_counts_as_interactive(self):
        content = self.build(
            creds=False, folder="plugin://plugin.audio.spotifykodiconnect/", is_media=False
        )
        content.check_auth_and_refresh_spotipy()
        self.assertEqual([1], self.auth_calls)

    def test_explicit_authenticate_action_skips_gate(self):
        polled = []
        self.pc.utils.get_valid_cached_auth_token = lambda: polled.append(1)
        content = self.build(action="authenticate_plugin_request", creds=True)
        self.assertTrue(content.check_auth_and_refresh_spotipy())
        self.assertEqual([], polled)
        self.assertEqual([], self.auth_calls)

    def test_token_present_initialises_client(self):
        self.pc.utils.get_valid_cached_auth_token = lambda: "tok"
        content = self.build()
        inits = []
        content.init_spotipy = lambda token: inits.append(token)
        self.assertTrue(content.check_auth_and_refresh_spotipy())
        self.assertEqual(["tok"], inits)


class CachedTokenReadTests(unittest.TestCase):
    """The plugin reads the service's token once, without waiting, and treats
    an expired token as missing."""

    def setUp(self):
        self.pc = fp.import_plugin_content()
        self.utils = self.pc.utils
        fp.FakeWindow.windows.clear()
        self.sleeps = []
        original_sleep = self.utils.xbmc.sleep
        self.addCleanup(setattr, self.utils.xbmc, "sleep", original_sleep)
        self.utils.xbmc.sleep = lambda ms: self.sleeps.append(ms)

    def window(self):
        return self.utils.xbmcgui.Window(self.utils.ADDON_WINDOW_ID)

    def set_token(self, token, expires_at):
        self.window().setProperty(self.utils.KODI_PROPERTY_SPOTIFY_AUTH_TOKEN, token)
        self.window().setProperty(self.utils.KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT, expires_at)

    def test_empty_token_returns_immediately_without_sleeping(self):
        started = time.monotonic()
        self.assertEqual("", self.utils.get_valid_cached_auth_token())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([], self.sleeps)

    def test_valid_token_is_returned(self):
        self.set_token("tok", "2000")
        self.assertEqual("tok", self.utils.get_valid_cached_auth_token(now=1000))

    def test_expired_token_is_treated_as_missing(self):
        self.set_token("tok", "1000")
        self.assertEqual("", self.utils.get_valid_cached_auth_token(now=1000))
        self.assertEqual("", self.utils.get_valid_cached_auth_token(now=1500))
        self.assertEqual([], self.sleeps)

    def test_token_without_valid_expiry_is_treated_as_missing(self):
        self.set_token("tok", "")
        self.assertEqual("", self.utils.get_valid_cached_auth_token(now=1000))
        self.set_token("tok", "garbage")
        self.assertEqual("", self.utils.get_valid_cached_auth_token(now=1000))

    def test_widget_with_expired_token_ends_silently_without_waiting(self):
        self.set_token("stale", "1")
        self.pc.xbmcgui.Dialog = RecordingDialog
        RecordingDialog.notifications = []
        RecordingDialog.yesno_calls = []
        end_calls = []
        self.pc.xbmcplugin.endOfDirectory = lambda *a, **kw: end_calls.append(kw)
        content = object.__new__(self.pc.PluginContent)
        content._PluginContent__addon = fp.FakeAddon()
        content._PluginContent__addon_handle = 1
        content._PluginContent__action = "browse_main_library"
        content._PluginContent__spotipy = None
        content.init_spotipy = lambda token: self.fail("expired token must not be used")
        self.pc.PluginContent._PluginContent__has_stored_credentials = staticmethod(lambda: True)
        self.pc.xbmc.getInfoLabel = lambda label: ""
        self.pc.xbmc.getCondVisibility = lambda cond: False
        started = time.monotonic()
        self.assertFalse(content.check_auth_and_refresh_spotipy())
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([], self.sleeps)
        self.assertEqual([], RecordingDialog.notifications)
        self.assertEqual([], RecordingDialog.yesno_calls)
        self.assertEqual([{"handle": 1, "succeeded": False}], end_calls)

    def test_clear_expired_token_drops_only_expired_tokens(self):
        self.set_token("tok", "2000")
        self.assertFalse(self.utils.clear_expired_cached_auth_token(now=1000))
        self.assertEqual(
            "tok", self.window().getProperty(self.utils.KODI_PROPERTY_SPOTIFY_AUTH_TOKEN)
        )
        self.assertTrue(self.utils.clear_expired_cached_auth_token(now=2000))
        self.assertEqual("", self.window().getProperty(self.utils.KODI_PROPERTY_SPOTIFY_AUTH_TOKEN))
        self.assertEqual(
            "", self.window().getProperty(self.utils.KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT)
        )
        # Nothing cached: nothing to clear.
        self.assertFalse(self.utils.clear_expired_cached_auth_token(now=3000))

    def test_renew_failing_flag_round_trip(self):
        self.assertFalse(self.utils.auth_renew_failing())
        self.utils.set_auth_renew_failing(2)
        self.assertTrue(self.utils.auth_renew_failing())
        self.utils.set_auth_renew_failing(0)
        self.assertFalse(self.utils.auth_renew_failing())


if __name__ == "__main__":
    unittest.main()
