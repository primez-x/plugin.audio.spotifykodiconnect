"""Plugin-side auth gating: never start a destructive zeroconf re-pair (or show
dialogs) just because the service has not published a token yet."""

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
        original_get_token = self.pc.utils.get_cached_auth_token
        self.addCleanup(setattr, self.pc.utils, "get_cached_auth_token", original_get_token)
        self.pc.utils.get_cached_auth_token = lambda: None
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

    def test_root_menu_with_credentials_asks_before_pairing(self):
        content = self.build(creds=True)
        self.assertFalse(content.check_auth_and_refresh_spotipy())
        self.assertEqual(1, len(RecordingDialog.yesno_calls))
        self.assertEqual([], self.auth_calls)
        self.assertEqual([{"handle": 1, "succeeded": False}], self.end_calls)

    def test_root_menu_with_credentials_pairs_when_confirmed(self):
        RecordingDialog.yesno_answer = True
        content = self.build(creds=True)
        content.check_auth_and_refresh_spotipy()
        self.assertEqual([1], self.auth_calls)

    def test_widget_with_credentials_never_asks(self):
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
        self.pc.utils.get_cached_auth_token = lambda: polled.append(1)
        content = self.build(action="authenticate_plugin_request", creds=True)
        self.assertTrue(content.check_auth_and_refresh_spotipy())
        self.assertEqual([], polled)
        self.assertEqual([], self.auth_calls)

    def test_token_present_initialises_client(self):
        self.pc.utils.get_cached_auth_token = lambda: "tok"
        content = self.build()
        inits = []
        content.init_spotipy = lambda token: inits.append(token)
        self.assertTrue(content.check_auth_and_refresh_spotipy())
        self.assertEqual(["tok"], inits)


if __name__ == "__main__":
    unittest.main()
