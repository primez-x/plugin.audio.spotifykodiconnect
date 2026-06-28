import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")


class FakeWindow:
    """Per-test window property store. Reset between tests via setUp."""

    def __init__(self):
        self.properties = {}

    def getProperty(self, key):
        return self.properties.get(key, "")

    def setProperty(self, key, value):
        self.properties[key] = value

    def clearProperty(self, key):
        self.properties.pop(key, None)


def install_stubs(window):
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    sys.modules["xbmc"] = xbmc

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = lambda window_id=None: window
    sys.modules["xbmcgui"] = xbmcgui

    utils = types.ModuleType("utils")
    utils.ADDON_WINDOW_ID = 10000
    utils.log_msg = lambda *args, **kwargs: None
    sys.modules["utils"] = utils


class PlayQueueTests(unittest.TestCase):
    def setUp(self):
        self.window = FakeWindow()
        install_stubs(self.window)
        if LIB_DIR not in sys.path:
            sys.path.insert(0, LIB_DIR)
        sys.modules.pop("play_queue", None)
        import play_queue

        self.play_queue = play_queue

    def tearDown(self):
        for module_name in ("play_queue", "xbmc", "xbmcgui", "utils"):
            sys.modules.pop(module_name, None)

    # --- start_session / mark_original_complete ---------------------------------

    def test_start_session_marks_loading_when_playlist_has_multiple_pages(self):
        """The daylist bug: a multi-page playlist must start "loading" so the
        service knows to suppress autoplay until paging finishes."""
        self.play_queue.start_session(75)
        self.assertTrue(self.window.properties["Spotify.PlayQueue.SessionId"])
        self.assertEqual("75", self.window.getProperty("Spotify.PlayQueue.OriginalTotal"))
        self.assertEqual("0", self.window.getProperty("Spotify.PlayQueue.OriginalLoaded"))
        self.assertEqual("false", self.window.getProperty("Spotify.PlayQueue.OriginalComplete"))
        self.assertEqual("false", self.window.getProperty("Spotify.PlayQueue.AutoplayFetched"))

    def test_start_session_marks_complete_immediately_for_empty_playlist(self):
        self.play_queue.start_session(0)
        self.assertEqual("true", self.window.getProperty("Spotify.PlayQueue.OriginalComplete"))

    def test_mark_original_complete_flips_loading_flag(self):
        self.play_queue.start_session(75)
        self.assertFalse(self.play_queue.is_original_complete())
        self.play_queue.mark_original_complete()
        self.assertTrue(self.play_queue.is_original_complete())

    def test_report_loaded_updates_count_for_diagnostics(self):
        self.play_queue.start_session(75)
        self.play_queue.report_loaded(50)
        self.assertEqual("50", self.window.getProperty("Spotify.PlayQueue.OriginalLoaded"))

    # --- is_loading ------------------------------------------------------------

    def test_is_loading_true_when_session_active_and_paging_incomplete(self):
        self.play_queue.start_session(75)
        self.assertTrue(self.play_queue.is_loading())

    def test_is_loading_false_after_mark_original_complete(self):
        self.play_queue.start_session(75)
        self.play_queue.mark_original_complete()
        self.assertFalse(self.play_queue.is_loading())

    def test_is_loading_false_when_no_session_active(self):
        """Single-track playback never calls start_session; the service must
        not suppress autoplay for those playbacks."""
        self.assertFalse(self.play_queue.is_loading())

    # --- should_fire_autoplay (the core of the daylist fix) --------------------

    def test_should_fire_autoplay_returns_false_while_original_is_loading(self):
        """Critical: this is the exact scenario that destroyed the daylist queue.
        Kodi reports no next item mid-page; autoplay must NOT fire."""
        self.play_queue.start_session(75)
        self.assertFalse(self.play_queue.should_fire_autoplay())

    def test_should_fire_autoplay_returns_true_when_original_complete(self):
        self.play_queue.start_session(75)
        self.play_queue.mark_original_complete()
        self.assertTrue(self.play_queue.should_fire_autoplay())

    def test_should_fire_autoplay_claims_slot_atomically_on_first_true(self):
        """A True return must mark AutoplayFetched so subsequent callbacks
        don't double-fire. Kodi dispatches serially, but the protocol
        defends against re-entry."""
        self.play_queue.start_session(75)
        self.play_queue.mark_original_complete()
        self.assertTrue(self.play_queue.should_fire_autoplay())
        # Second call must see the claimed slot.
        self.assertEqual("true", self.window.getProperty("Spotify.PlayQueue.AutoplayFetched"))
        self.assertFalse(self.play_queue.should_fire_autoplay())

    def test_should_fire_autoplay_fires_only_once_per_session(self):
        self.play_queue.start_session(75)
        self.play_queue.mark_original_complete()
        results = [self.play_queue.should_fire_autoplay() for _ in range(5)]
        self.assertEqual([True, False, False, False, False], results)

    def test_should_fire_autoplay_fires_when_no_session_active(self):
        """Single-track play (no play_playlist call) falls back to legacy
        autoplay: always fire when Kodi reports no next item."""
        self.assertTrue(self.play_queue.should_fire_autoplay())

    def test_should_fire_autoplay_does_not_write_state_without_session(self):
        """No session means no slot to claim — leave state clean for the next
        real session."""
        self.play_queue.should_fire_autoplay()
        self.assertEqual("", self.window.getProperty("Spotify.PlayQueue.AutoplayFetched"))

    # --- clear -----------------------------------------------------------------

    def test_clear_wipes_all_play_queue_props(self):
        self.play_queue.start_session(75)
        self.play_queue.report_loaded(50)
        self.play_queue.mark_original_complete()
        self.play_queue.clear()
        for key in (
            "Spotify.PlayQueue.SessionId",
            "Spotify.PlayQueue.OriginalTotal",
            "Spotify.PlayQueue.OriginalLoaded",
            "Spotify.PlayQueue.OriginalComplete",
            "Spotify.PlayQueue.AutoplayFetched",
        ):
            self.assertEqual("", self.window.getProperty(key), f"{key} not cleared")

    def test_clear_is_idempotent_when_no_session(self):
        self.play_queue.clear()  # must not raise
        self.play_queue.clear()

    # --- session lifecycle: stop mid-load then start new session --------------

    def test_new_session_overwrites_stale_state_from_prior_session(self):
        """First session dies mid-load; user starts a new playlist. The new
        start_session must clear the stale 'loading' state — otherwise the
        new session would inherit the dead session's flags."""
        self.play_queue.start_session(75)
        self.play_queue.report_loaded(50)
        # Session dies without mark_original_complete.
        # User stops playback (service calls clear) and starts a new one:
        self.play_queue.clear()
        self.play_queue.start_session(10)
        self.assertEqual("10", self.window.getProperty("Spotify.PlayQueue.OriginalTotal"))
        self.assertEqual("false", self.window.getProperty("Spotify.PlayQueue.AutoplayFetched"))
        self.assertEqual("false", self.window.getProperty("Spotify.PlayQueue.OriginalComplete"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
