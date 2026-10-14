"""Vendored spotipy Retry: fail fast on long 429 Retry-After instead of sleeping."""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEPS_DIR = os.path.join(REPO_ROOT, "resources", "lib", "deps")
if DEPS_DIR not in sys.path:
    sys.path.insert(0, DEPS_DIR)


def _drop_stub_spotipy():
    """Other test modules register a bare stub named 'spotipy'; load the real one."""
    module = sys.modules.get("spotipy")
    if module is not None and not hasattr(module, "__path__"):
        sys.modules.pop("spotipy", None)


try:
    import urllib3

    _drop_stub_spotipy()
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

            utils.install_spotipy_rate_limit_hook(types.SimpleNamespace(util=spotipy_util))
            self.assertIs(spotipy_util.on_long_rate_limit, utils.set_rate_limited_until)
            self.assertFalse(utils.is_rate_limited())
            utils.set_rate_limited_until(10_000_000_000)
            self.assertTrue(utils.is_rate_limited())
            self.assertFalse(utils.is_rate_limited(now=20_000_000_000))
        finally:
            sys.modules.pop("utils", None)
            if saved_window is not None:
                sys.modules["xbmcgui"].Window = saved_window
            sys.path.remove(lib)


@unittest.skipUnless(HAVE_DEPS, "requests/urllib3 not installed")
class TransportTests(unittest.TestCase):
    def test_real_http_status_and_single_shot_writes(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from spotipy import Spotify, SpotifyException
        counts = {}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def respond(self):
                counts[self.command] = counts.get(self.command, 0) + 1
                self.send_response(int(self.path.strip("/")))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"unavailable","reason":"server failure"}}')
            do_GET = do_POST = do_PUT = do_DELETE = respond
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = Spotify(retries=1, status_retries=1, backoff_factor=0)
        try:
            self.assertEqual((3.05, 10), client.requests_timeout)
            for method in ("GET", "POST", "PUT", "DELETE"):
                for status in (500, 503, 429):
                    with self.subTest(method=method, status=status):
                        counts.clear()
                        with self.assertRaises(SpotifyException) as caught:
                            client._internal_call(method, f"http://127.0.0.1:{server.server_port}/{status}", None, {})
                        self.assertEqual(status, caught.exception.http_status)
                        self.assertEqual(2 if method == "GET" else 1, counts[method])
        finally:
            client._session.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_retry_without_http_response_preserves_reason(self):
        import requests
        from spotipy import Spotify, SpotifyException
        from unittest.mock import Mock
        reason = urllib3.exceptions.ConnectTimeoutError("connection timed out")
        failure = urllib3.exceptions.MaxRetryError(None, "/v1/me", reason)
        session = Mock()
        session.request.side_effect = requests.exceptions.RetryError(failure)
        client = Spotify(requests_session=False)
        client._session = session
        with self.assertRaises(SpotifyException) as caught:
            client._internal_call("GET", "me", None, {})
        self.assertEqual(0, caught.exception.http_status)
        self.assertIs(reason, caught.exception.reason)

    def test_long_429_preserves_status_and_retry_after(self):
        import requests
        from spotipy import Spotify, SpotifyException
        from unittest.mock import Mock
        retry = spotipy_util.Retry(total=1, allowed_methods={"GET"})
        try:
            retry.increment("GET", "/v1/me", response=_Resp(429, "3600"))
        except urllib3.exceptions.MaxRetryError as failure:
            session = Mock()
            session.request.side_effect = requests.exceptions.RetryError(failure)
        client = Spotify(requests_session=False)
        client._session = session
        with self.assertRaises(SpotifyException) as caught:
            client._internal_call("GET", "me", None, {})
        self.assertEqual(429, caught.exception.http_status)
        self.assertEqual("3600", caught.exception.headers["Retry-After"])


if __name__ == "__main__":
    unittest.main()
