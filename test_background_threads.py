"""plugin.py must not return while this invocation's background work runs.

Kodi tears down the plugin's Python interpreter once the script returns;
threads still running then race that teardown.
"""

import threading
import time
import unittest

import test_playlist_fastpath as fp


class JoinBackgroundThreadsTest(unittest.TestCase):
    def setUp(self):
        self.pc = fp.import_plugin_content()
        # import_plugin_content() swaps in a stub Thread; use real threads
        threading.Thread = fp.DeferredThread.real_thread
        self.addCleanup(setattr, threading, "Thread", fp.DeferredThread)
        self.pc._BACKGROUND_THREADS.clear()
        self.addCleanup(self.pc._BACKGROUND_THREADS.clear)
        self.abort = False
        test = self

        class Monitor:
            def abortRequested(self):
                return test.abort

        self.pc.xbmc.Monitor = Monitor

    def test_waits_until_background_work_finished(self):
        done = []
        self.pc._start_background_thread(lambda: (time.sleep(0.5), done.append(1)))
        self.pc.join_background_threads()
        self.assertEqual(done, [1])
        self.assertFalse(self.pc._BACKGROUND_THREADS[0].is_alive())

    def test_gives_up_shortly_after_kodi_aborts(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.pc.BACKGROUND_ABORT_GRACE_SECS = 0.3
        self.pc._start_background_thread(lambda: release.wait(10))
        self.abort = True
        started = time.monotonic()
        self.pc.join_background_threads()
        self.assertLess(time.monotonic() - started, 2)


if __name__ == "__main__":
    unittest.main()
