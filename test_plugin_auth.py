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

    def test_unexpected_listing_error_has_generic_folder_message(self):
        content = self.build()
        content._PluginContent__handle_listing_error(ValueError("unexpected"))
        self.assertIn(f"str-{self.pc.SPOTIFY_LISTING_FAILED_STR_ID}", RecordingDialog.notifications[-1])
        self.assertFalse(self.end_calls[-1]["succeeded"])

    def test_dispatch_failure_ends_unsuccessfully_and_redacts_notification(self):
        from unittest.mock import patch
        self.pc.xbmc.getInfoLabel = lambda label: "plugin://plugin.audio.spotifykodiconnect/"
        def parse(content):
            content._PluginContent__action = "browse_main_library"
        def handler(content, action):
            def failed():
                raise TimeoutError("private diagnostic")
            return failed
        with patch.object(self.pc.PluginContent, "parse_params", parse), \
             patch.object(self.pc.PluginContent, "check_auth_and_refresh_spotipy", return_value=True), \
             patch.object(self.pc.PluginContent, "_get_action_handler", handler):
            self.pc.PluginContent()
        self.assertFalse(self.end_calls[-1]["succeeded"])
        self.assertEqual(1, len(RecordingDialog.notifications))
        self.assertNotIn("private diagnostic", str(RecordingDialog.notifications))

    def test_new_error_strings_exist_once_in_base_catalog(self):
        import ast
        import pathlib
        import re
        root = pathlib.Path(fp.REPO_ROOT)
        tree = ast.parse((root / "resources/lib/string_ids.py").read_text())
        numbers = [node.value.value for node in tree.body if isinstance(node, ast.Assign)
                   and isinstance(node.value, ast.Constant) and isinstance(node.value.value, int)
                   and 11155 <= node.value.value <= 11163]
        self.assertEqual(list(range(11155, 11164)), numbers)
        catalog = (root / "resources/language/resource.language.en_gb/strings.po").read_text()
        for number in numbers:
            self.assertEqual(1, catalog.count(f'msgctxt "#{number}"'))
            self.assertRegex(catalog, rf'msgctxt "#{number}"\nmsgid "[^"\n]+"')

    def test_listing_error_statuses_and_widget_silence(self):
        for status, string_name in ((429, "SPOTIFY_RATE_LIMITED_STR_ID"),
                                    (403, "SPOTIFY_PERMISSION_STR_ID"),
                                    (0, "SPOTIFY_UNREACHABLE_STR_ID")):
            content = self.build()
            error = OSError("network")
            error.http_status = status
            content._PluginContent__handle_listing_error(error)
            self.assertIn(f"str-{getattr(self.pc, string_name)}", RecordingDialog.notifications[-1])
            self.assertFalse(self.end_calls[-1]["succeeded"])
        RecordingDialog.notifications.clear()
        content = self.build(is_media=False)
        content._PluginContent__handle_listing_error(OSError("network"))
        self.assertEqual([], RecordingDialog.notifications)

    def test_listing_401_uses_existing_pairing_confirmation(self):
        content = self.build()
        error = OSError("expired")
        error.http_status = 401
        RecordingDialog.yesno_answer = True
        content._PluginContent__handle_listing_error(error)
        self.assertEqual([1], self.auth_calls)
        self.assertFalse(self.end_calls[-1]["succeeded"])

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


class PairingProgressTests(unittest.TestCase):
    def setUp(self):
        self.pc = fp.import_plugin_content()
        self.dialog_messages = []
        self.progress_updates = []
        self.pairing_flags = []
        self.process = object()

        class Dialog:
            def ok(_self, heading, message):
                self.dialog_messages.append((heading, message))

        class Progress:
            def create(_self, heading, message):
                _self.created = (heading, message)

            def update(_self, percent):
                self.progress_updates.append(percent)

            def iscanceled(_self):
                return False

            def close(_self):
                _self.closed = True

        self.progress = Progress()

        class Auth:
            def __init__(_self, _spotty):
                pass

            def start_zeroconf_authenticate(_self):
                return self.process

            def wait_for_zeroconf_credentials(_self, process, progress_callback=None):
                self.assertIs(self.process, process)
                progress_callback(50)
                return True

            def stop_zeroconf_authenticate(_self, process):
                self.assertIs(self.process, process)
                self.stopped = True

            def zeroconf_authenticated_ok(_self):
                return True

            def renew_token(_self):
                self.renewed = True

        self.pc.SpottyAuth = Auth
        self.pc.xbmcgui.Dialog = Dialog
        self.pc.xbmcgui.DialogProgress = lambda: self.progress
        self.pc.utils.mark_zeroconf_pairing = lambda active: self.pairing_flags.append(active)
        content = object.__new__(self.pc.PluginContent)
        content._PluginContent__addon = fp.FakeAddon()
        content._PluginContent__spotty = object()
        content.refresh_spotipy = lambda: None
        content.get_authenticated_success_msg = lambda: "paired"
        self.content = content

    def test_pairing_waits_for_credentials_and_closes_progress(self):
        self.content.authenticate_plugin("Pair using Spotify on your phone")

        self.assertEqual([50], self.progress_updates)
        self.assertEqual([True, False], self.pairing_flags)
        self.assertEqual(
            ("SpotifyKodiConnect", "Pair using Spotify on your phone"), self.progress.created
        )
        self.assertTrue(self.progress.closed)
        self.assertTrue(self.stopped)
        self.assertTrue(self.renewed)
        self.assertEqual("paired", self.dialog_messages[-1][1])


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
