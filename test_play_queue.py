import os
import sys
import types
import tempfile
import multiprocessing
from unittest.mock import patch
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


def install_stubs(window, profile):
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    sys.modules["xbmc"] = xbmc

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = lambda window_id=None: window
    sys.modules["xbmcgui"] = xbmcgui

    utils = types.ModuleType("utils")
    utils.ADDON_WINDOW_ID = 10000
    utils.ADDON_DATA_PATH = profile
    utils.PROXY_HOST = "127.0.0.1"
    utils.PROXY_PORT = 52309
    utils.log_msg = lambda *args, **kwargs: None
    sys.modules["utils"] = utils


class PlayQueueTests(unittest.TestCase):
    def setUp(self):
        self.window = FakeWindow()
        self.profile = tempfile.TemporaryDirectory(prefix="spotify-queue-")
        self.addCleanup(self.profile.cleanup)
        install_stubs(self.window, self.profile.name)
        if LIB_DIR not in sys.path:
            sys.path.insert(0, LIB_DIR)
        sys.modules.pop("play_queue", None)
        import play_queue

        self.play_queue = play_queue

    def tearDown(self):
        for module_name in ("play_queue", "playlist_next", "xbmc", "xbmcgui", "utils"):
            sys.modules.pop(module_name, None)

    def test_stream_url_preserves_milliseconds_and_minimum_duration(self):
        import playlist_next

        for duration_ms, token, seconds in (
            (213560, "213560ms", 213.560),
            (1250, "1250ms", 1.250),
            (999, "1000ms", 1.0),
            (0, "1000ms", 1.0),
            (None, "1000ms", 1.0),
        ):
            with self.subTest(duration_ms=duration_ms):
                url = self.play_queue.build_stream_url("track-1", duration_ms)
                self.assertEqual(f"http://127.0.0.1:52309/track/track-1/{token}", url)
                self.assertNotIn(".", url.rsplit("/", 1)[-1])
                self.assertEqual(("track-1", seconds), playlist_next.parse_track_url(url))

    def test_bound_stream_item_starts_in_music_playlist(self):
        class FakeListItem:
            def __init__(self):
                self.properties = {}

            def setProperty(self, key, value):
                self.properties[key] = value

            def setMimeType(self, mime_type):
                self.mime_type = mime_type

            def setContentLookup(self, enabled):
                self.content_lookup = enabled

        listitem = FakeListItem()
        self.play_queue.bind_stream_item(listitem, "track-1", "owner")
        # Kodi PLAYLIST::TYPE_MUSIC; extensionless URLs give Kodi no other media type.
        self.assertEqual("0", listitem.properties["playlist_type_hint"])
        self.assertEqual("audio/x-wav", listitem.mime_type)
        self.assertFalse(listitem.content_lookup)

    def test_saved_track_urls_keep_second_duration_interface(self):
        import playlist_next

        for host in ("localhost", "127.0.0.1"):
            for token in ("213.560", "213.560.wav", "213.560.wav/", "213560ms", "213560ms/"):
                with self.subTest(host=host, token=token):
                    url = f"http://{host}:52309/track/track-1/{token}"
                    self.assertEqual(("track-1", 213.560), playlist_next.parse_track_url(url))
        for token in ("1.5ms", "-1ms", "nanms", "1e3ms", "213560ms.wav", "ms"):
            with self.subTest(invalid=token):
                self.assertEqual((None, None), playlist_next.parse_track_url(
                    f"http://127.0.0.1:52309/track/track-1/{token}"))

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
        self.play_queue.mark_original_complete(self.play_queue.session_id())
        self.assertTrue(self.play_queue.is_original_complete())

    def test_report_loaded_updates_count_for_diagnostics(self):
        self.play_queue.start_session(75)
        self.play_queue.report_loaded(50, self.play_queue.session_id())
        self.assertEqual("50", self.window.getProperty("Spotify.PlayQueue.OriginalLoaded"))

    def test_session_tokens_are_unique_at_the_same_timestamp(self):
        with patch("time.time", return_value=1234):
            first = self.play_queue.start_session(75)
            second = self.play_queue.start_session(75)
        self.assertNotEqual(first, second)

    def test_stale_progress_and_completion_leave_new_session_unchanged(self):
        first = self.play_queue.start_session(75)
        second = self.play_queue.start_session(100)
        self.play_queue.report_loaded(75, first)
        self.play_queue.mark_original_complete(first)
        self.assertEqual(second, self.play_queue.session_id())
        self.assertEqual("0", self.window.getProperty(self.play_queue.PROP_ORIGINAL_LOADED))
        self.assertFalse(self.play_queue.is_original_complete())

    def test_guard_serializes_cross_process_replacement_and_clear(self):
        context = multiprocessing.get_context("fork")
        with context.Manager() as manager:
            self.window.properties = manager.dict()
            for action in ("replace", "clear"):
                with self.subTest(action=action):
                    old = self.play_queue.start_session(75)
                    entered = context.Event()
                    released = context.Event()
                    attempted = context.Event()
                    finished = context.Event()
                    mutations = manager.list()
                    def append():
                        with self.play_queue.session_guard(old) as owned:
                            if not owned:
                                raise RuntimeError("active owner rejected")
                            entered.set()
                            if not released.wait(5):
                                raise RuntimeError("guard release timeout")
                            mutations.append("old")
                    def transition():
                        attempted.set()
                        if action == "replace":
                            self.play_queue.start_session(100)
                        else:
                            self.play_queue.clear()
                        mutations[:] = [action]
                        finished.set()
                    loader = context.Process(target=append)
                    replacement = context.Process(target=transition)
                    loader.start()
                    try:
                        self.assertTrue(entered.wait(5), "loader did not acquire guard")
                        replacement.start()
                        self.assertTrue(attempted.wait(5))
                        self.assertFalse(finished.wait(0.2), "transition crossed an active mutation")
                    finally:
                        released.set()
                        loader.join(5)
                        if replacement.pid is not None:
                            replacement.join(5)
                        for process in (loader, replacement):
                            if process.pid is not None and process.is_alive():
                                process.terminate()
                                process.join()
                    self.assertEqual(0, loader.exitcode)
                    self.assertEqual(0, replacement.exitcode)
                    self.assertEqual([action], list(mutations))
                    with self.play_queue.session_guard(old) as owned:
                        self.assertFalse(owned)

    def test_guard_releases_after_exception(self):
        old = self.play_queue.start_session(75)
        with self.assertRaises(RuntimeError):
            with self.play_queue.session_guard(old) as owned:
                self.assertTrue(owned)
                raise RuntimeError("append failed")
        new = self.play_queue.start_session(100)
        self.assertEqual(new, self.play_queue.session_id())

    # --- is_loading ------------------------------------------------------------

    def test_is_loading_true_when_session_active_and_paging_incomplete(self):
        self.play_queue.start_session(75)
        self.assertTrue(self.play_queue.is_loading())

    def test_is_loading_false_after_mark_original_complete(self):
        self.play_queue.start_session(75)
        self.play_queue.mark_original_complete(self.play_queue.session_id())
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
        self.play_queue.mark_original_complete(self.play_queue.session_id())
        self.assertTrue(self.play_queue.should_fire_autoplay())

    def test_should_fire_autoplay_claims_slot_atomically_on_first_true(self):
        """A True return must mark AutoplayFetched so subsequent callbacks
        don't double-fire. Kodi dispatches serially, but the protocol
        defends against re-entry."""
        self.play_queue.start_session(75)
        self.play_queue.mark_original_complete(self.play_queue.session_id())
        self.assertTrue(self.play_queue.should_fire_autoplay())
        # Second call must see the claimed slot.
        self.assertEqual("true", self.window.getProperty("Spotify.PlayQueue.AutoplayFetched"))
        self.assertFalse(self.play_queue.should_fire_autoplay())

    def test_should_fire_autoplay_fires_only_once_per_session(self):
        self.play_queue.start_session(75)
        self.play_queue.mark_original_complete(self.play_queue.session_id())
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
        self.play_queue.report_loaded(50, self.play_queue.session_id())
        self.play_queue.mark_original_complete(self.play_queue.session_id())
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

    def test_owner_clear_rejects_stale_and_empty_owners_atomically(self):
        old = self.play_queue.start_session(75)
        new = self.play_queue.start_session(100)
        for owner in (old, ""):
            with self.subTest(owner=owner):
                self.play_queue.clear(owner)
                self.assertEqual(new, self.play_queue.session_id())
                self.assertFalse(self.play_queue.should_fire_autoplay())
        self.play_queue.clear(new)
        self.assertEqual("", self.play_queue.session_id())
        self.window.setProperty(self.play_queue.PROP_ORIGINAL_COMPLETE, "false")
        self.play_queue.clear("")
        self.assertEqual("", self.window.getProperty(self.play_queue.PROP_ORIGINAL_COMPLETE))

    def test_owner_clear_propagates_storage_failure_without_mutation(self):
        owner = self.play_queue.start_session(100)
        with patch.object(self.play_queue.sqlite3, "connect", side_effect=OSError("storage unavailable")):
            with self.assertRaises(OSError):
                self.play_queue.clear(owner)
        self.assertEqual(owner, self.play_queue.session_id())
        self.assertFalse(self.play_queue.should_fire_autoplay())

    # --- session lifecycle: stop mid-load then start new session --------------

    def test_new_session_overwrites_stale_state_from_prior_session(self):
        """First session dies mid-load; user starts a new playlist. The new
        start_session must clear the stale 'loading' state — otherwise the
        new session would inherit the dead session's flags."""
        self.play_queue.start_session(75)
        self.play_queue.report_loaded(50, self.play_queue.session_id())
        # Session dies without mark_original_complete.
        # User stops playback (service calls clear) and starts a new one:
        self.play_queue.clear()
        self.play_queue.start_session(10)
        self.assertEqual("10", self.window.getProperty("Spotify.PlayQueue.OriginalTotal"))
        self.assertEqual("false", self.window.getProperty("Spotify.PlayQueue.AutoplayFetched"))
        self.assertEqual("false", self.window.getProperty("Spotify.PlayQueue.OriginalComplete"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
