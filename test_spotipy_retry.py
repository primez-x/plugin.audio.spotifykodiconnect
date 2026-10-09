"""Vendored spotipy Retry: fail fast on long 429 Retry-After instead of sleeping."""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEPS_DIR = os.path.join(REPO_ROOT, "resources", "lib", "deps")
if DEPS_DIR not in sys.path:
    sys.path.insert(0, DEPS_DIR)

try:
    import urllib3
    from spotipy import util as spotipy_util

    HAVE_DEPS = True
except ImportError:  # pragma: no cover - requests/urllib3 not installed
    HAVE_DEPS = False


class _Resp:
    def __init__(self, status, retry_after=None):
        self.status = status
        self.headers = {"Retry-After": retry_after} if retry_after is not None else {}

    def get_redirect_location(self):
        return False


@unittest.skipUnless(HAVE_DEPS, "requests/urllib3 not installed")
class LongRetryAfterTests(unittest.TestCase):
    def setUp(self):
        self.recorded = []
        spotipy_util.on_long_rate_limit = self.recorded.append

    def tearDown(self):
        spotipy_util.on_long_rate_limit = None

    def make_retry(self):
        return spotipy_util.Retry(
            total=3,
            connect=None,
            read=False,
            allowed_methods=frozenset(["GET"]),
            status=3,
            backoff_factor=0.3,
            status_forcelist=(429, 500),
        )

    def test_long_retry_after_raises_and_records_window(self):
        retry = self.make_retry()
        with self.assertRaises(urllib3.exceptions.MaxRetryError):
            retry.increment("GET", "/v1/me", response=_Resp(429, "3600"))
        self.assertEqual(1, len(self.recorded))
        import time

        self.assertGreater(self.recorded[0], time.time() + 3000)

    def test_short_retry_after_still_retries(self):
        retry = self.make_retry()
        new_retry = retry.increment("GET", "/v1/me", response=_Resp(429, "2"))
        self.assertIsInstance(new_retry, urllib3.Retry)
        self.assertEqual([], self.recorded)

    def test_non_429_status_unaffected(self):
        retry = self.make_retry()
        new_retry = retry.increment("GET", "/v1/me", response=_Resp(500, "3600"))
        self.assertIsInstance(new_retry, urllib3.Retry)
        self.assertEqual([], self.recorded)

    def test_hook_installer_routes_to_kodi_property(self):
        lib = os.path.join(REPO_ROOT, "resources", "lib")
        sys.path.insert(0, lib)
        import types

        win_props = {}

        class _Win:
            def __init__(self, _id=None):
                pass

            def getProperty(self, k):
                return win_props.get(k, "")

            def setProperty(self, k, v):
                win_props[k] = v

            def clearProperty(self, k):
                win_props.pop(k, None)

        for name in ("xbmc", "xbmcaddon", "xbmcgui", "xbmcvfs"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["xbmc"].LOGDEBUG = 0
        sys.modules["xbmc"].LOGINFO = 1
        sys.modules["xbmc"].LOGERROR = 3
        sys.modules["xbmcaddon"].Addon = lambda id=None: types.SimpleNamespace(
            getAddonInfo=lambda k: "", getSetting=lambda k: ""
        )
        sys.modules["xbmcvfs"].translatePath = lambda p: p
        saved_window = getattr(sys.modules["xbmcgui"], "Window", None)
        sys.modules["xbmcgui"].Window = _Win
        sys.modules.pop("utils", None)
        try:
            import utils
            import spotipy

            utils.install_spotipy_rate_limit_hook(spotipy)
            self.assertIs(spotipy.util.on_long_rate_limit, utils.set_rate_limited_until)
            self.assertFalse(utils.is_rate_limited())
            utils.set_rate_limited_until(10_000_000_000)
            self.assertTrue(utils.is_rate_limited())
            self.assertFalse(utils.is_rate_limited(now=20_000_000_000))
        finally:
            sys.modules.pop("utils", None)
            if saved_window is not None:
                sys.modules["xbmcgui"].Window = saved_window
            sys.path.remove(lib)


if __name__ == "__main__":
    unittest.main()
