"""The proxy port must be free again once the bottle server is stopped.

A Kodi service restart (e.g. an add-on update) during playback left the old
listening socket open, so the new service failed to bind with EADDRINUSE and
every track request timed out until Kodi was restarted.
"""

import os
import socket
import sys
import threading
import types
import unittest
import urllib.request

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")


def _install_stubs():
    utils = types.ModuleType("utils")
    utils.LOGDEBUG = 0
    utils.PROXY_HOST = "127.0.0.1"
    utils.log_msg = lambda *a, **k: None
    utils.log_exception = lambda *a, **k: None
    sys.modules["utils"] = utils


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class StopThreadReleasesPortTest(unittest.TestCase):
    def setUp(self):
        self._saved = {name: sys.modules.get(name) for name in ("utils", "bottle", "bottle_manager")}
        _install_stubs()
        sys.path[:0] = [LIB_DIR, os.path.join(LIB_DIR, "deps")]
        sys.modules.pop("bottle_manager", None)
        import bottle_manager

        self.bottle_manager = bottle_manager

    def tearDown(self):
        del sys.path[:2]
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_port_can_be_bound_after_stop_with_stream_in_flight(self):
        streaming = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)

        def track():
            def generate():
                yield b"x" * 1024
                streaming.set()
                release.wait(10)
                yield b"x"

            return generate()

        track.route = "/track"
        app = types.SimpleNamespace(track=track)
        self.bottle_manager.route_all(app)

        port = _free_port()
        self.bottle_manager.start_thread(port)

        def stream():
            for _ in range(50):
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/track", timeout=10) as resp:
                        resp.read()
                    return
                except OSError:
                    streaming.wait(0.1)

        client = threading.Thread(target=stream, daemon=True)
        client.start()
        self.assertTrue(streaming.wait(5), "request never reached the server")

        stopper = threading.Thread(target=self.bottle_manager.stop_thread, daemon=True)
        stopper.start()
        stopper.join(5)
        self.assertFalse(stopper.is_alive(), "stop_thread blocked on the in-flight stream")

        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))


if __name__ == "__main__":
    unittest.main()
