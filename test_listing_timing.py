"""Opt-in listing timing log: formatting, item counting and the plugin hook."""

import types
import unittest

import test_playlist_fastpath as fp


class FakeClock:
    def __init__(self, *times):
        self.times = list(times)

    def __call__(self):
        return self.times.pop(0)


class ListingTimerTests(unittest.TestCase):
    def setUp(self):
        fp.import_plugin_content()  # puts resources/lib on sys.path
        import listing_timing

        self.timing = listing_timing

    def test_line_sums_phases_and_reports_the_rest_as_other(self):
        timer = self.timing.ListingTimer(10.0, clock=FakeClock(10.1, 10.102, 10.6, 10.62))
        timer.mark("import")
        timer.mark("auth")
        timer.mark("listing")
        timer.add_items(50)

        self.assertEqual(
            "Listing action=browse_playlist: 620 ms, 50 items "
            "(import 100, auth 2, listing 498, other 20)",
            timer.line("action=browse_playlist"),
        )

    def test_label_uses_the_identifying_params(self):
        params = {"action": ["browse_playlist"], "playlistid": ["abc"], "offset": ["0"], "x": ["y"]}
        self.assertEqual("action=browse_playlist playlistid=abc offset=0", self.timing.label(params))
        self.assertEqual("", self.timing.label({}))

    def test_item_counter_counts_both_add_calls_and_passes_them_on(self):
        calls = []
        module = types.SimpleNamespace(
            addDirectoryItems=lambda *a, **kw: calls.append(("items", a, kw)) or True,
            addDirectoryItem=lambda *a, **kw: calls.append(("item", a, kw)) or True,
        )
        timer = self.timing.ListingTimer(0.0, clock=lambda: 0.0)
        self.timing.count_directory_items(module, timer)

        self.assertTrue(module.addDirectoryItems(1, [1, 2, 3], 3))
        self.assertTrue(module.addDirectoryItems(handle=1, items=[4], totalItems=1))
        self.assertTrue(module.addDirectoryItem(handle=1, url="u", listitem=None))

        self.assertEqual(5, timer.items)
        self.assertEqual(["items", "items", "item"], [call[0] for call in calls])


class PluginTimingHookTests(unittest.TestCase):
    def setUp(self):
        self.pc = fp.import_plugin_content()
        self.settings = {}
        test = self

        class Addon(fp.FakeAddon):
            def getSetting(self, key):
                return test.settings.get(key, "")

        self._patch(self.pc.PluginContent, "_PluginContent__addon", Addon())
        self._patch(self.pc.simplecache, "SimpleCache", lambda *args, **kwargs: object())
        self.logged = []
        self._patch(self.pc, "log_msg", lambda msg, level=0, *args: self.logged.append((msg, level)))
        self.added = []
        self._patch(
            self.pc.xbmcplugin, "addDirectoryItems", lambda *args, **kwargs: self.added.append(args)
        )

    def _patch(self, target, name, value):
        original = getattr(target, name)
        setattr(target, name, value)
        self.addCleanup(setattr, target, name, original)

    def run_listing(self):
        pc = self.pc

        def parse_params(content):
            content._PluginContent__params = {"action": ["browse_top_tracks"]}
            content._PluginContent__action = "browse_top_tracks"

        def listing():
            pc.xbmcplugin.addDirectoryItems(1, [("a", None, False), ("b", None, False)], 2)

        self._patch(pc.PluginContent, "parse_params", parse_params)
        self._patch(pc.PluginContent, "check_auth_and_refresh_spotipy", lambda content: True)
        self._patch(pc.PluginContent, "_get_action_handler", lambda content, action: listing)
        pc.PluginContent(started_at=None)

    def test_no_timing_line_by_default(self):
        self.run_listing()

        self.assertEqual(1, len(self.added))
        self.assertFalse([msg for msg, _ in self.logged if msg.startswith("Listing ")])

    def test_enabled_setting_logs_one_info_line_with_the_item_count(self):
        self.settings["listing_timing_log"] = "true"

        self.run_listing()

        lines = [(msg, level) for msg, level in self.logged if msg.startswith("Listing ")]
        self.assertEqual(1, len(lines))
        msg, level = lines[0]
        self.assertEqual(self.pc.LOGINFO, level)
        self.assertTrue(msg.startswith("Listing action=browse_top_tracks: "), msg)
        self.assertIn(", 2 items (", msg)
        self.assertIn("listing ", msg)
        self.assertEqual(1, len(self.added))


if __name__ == "__main__":
    unittest.main()
