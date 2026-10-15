"""End-to-end seam checks: real HTTP/streamer/cache code against a fake Spotty.

The fake emits a PCM ramp where every 4-byte frame encodes its own index, and
honours --start-position the way the real binary does (measured on CoreELEC,
spotty 1.3.1 / librespot 0.4.2): it lands a few tens of milliseconds *before*
the requested second.  Every served byte must equal the ideal WAV, so any
repeated or missing frame at a seam fails the comparison.
"""

import array
from contextlib import contextmanager
import os
import subprocess
import sys
import threading
import time
import types
import unittest

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")
FRAMES_PER_SEC = 44100
DURATION = 30
PCM_LEN = DURATION * 176400
TRACK = "seam-track"


def _ramp(frames):
    return array.array("I", range(frames)).tobytes()


RAMP = _ramp(PCM_LEN // 4)


def _seek_frame(seconds):
    # Real Spotty landed 1644..2536 frames early at 10/11 s; vary it per call.
    return max(0, seconds * FRAMES_PER_SEC - (1644 + (seconds * 977) % 1500))


class _Stdout:
    def __init__(self, payload):
        self._payload = memoryview(payload)
        self._pos = 0

    def read(self, size):
        chunk = bytes(self._payload[self._pos : self._pos + size])
        self._pos += len(chunk)
        return chunk


class _Process:
    stderr = None

    def __init__(self, payload, returncode):
        self.stdout = _Stdout(payload)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


class RampSpotty:
    """Each run emits the ramp from its (early-landing) seek point.

    *cuts* lists, per run, how many bytes that run delivers before exiting
    with rc=1 (None = run to the end with rc=0).
    """

    def __init__(self, cuts=()):
        self.cuts = list(cuts)
        self.calls = []

    def run_spotty(self, args):
        self.calls.append(list(args))
        start = 0
        if "--start-position" in args:
            start = _seek_frame(int(args[args.index("--start-position") + 1])) * 4
        cut = self.cuts.pop(0) if self.cuts else None
        payload = RAMP[start:] if cut is None else RAMP[start : start + cut]
        return _Process(payload, 0 if cut is None else 1)

    def seeks(self):
        return [c for c in self.calls if "--start-position" in c]


class _PipeSpotty:
    """Real buffered stdout with separate first-frame and bulk gates."""

    def __init__(self, prefix_bytes=4):
        self.prefix_bytes = prefix_bytes
        self.prefix_written = threading.Event()
        self.frame_written = threading.Event()
        self.frame_written_at = None
        self.bulk_released = False
        self.bulk_released_at = None
        self.process = None
        self.status_thread = None
        self.status = None

    def run_spotty(self, args):
        status_read, status_write = os.pipe()
        producer = """
import os, sys
status, prefix = map(int, sys.argv[1:])
frame = bytes(4)
os.write(1, frame[:prefix])
os.write(status, b'p')
if prefix < 4:
    if not sys.stdin.buffer.read(1):
        sys.exit(0)
    os.write(1, frame[prefix:])
os.write(status, b'f')
if sys.stdin.buffer.read(1):
    import array
    pcm = array.array('I', range(30 * 44100)).tobytes()
    sys.stdout.buffer.write(pcm[4:])
    sys.stdout.buffer.flush()
"""
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-c", producer, str(status_write), str(self.prefix_bytes)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=1048576,
                pass_fds=(status_write,),
            )
        except BaseException:
            os.close(status_read)
            raise
        finally:
            os.close(status_write)
        self.status = os.fdopen(status_read, "rb", buffering=0)

        def acknowledge():
            while True:
                marker = self.status.read(1)
                if not marker:
                    return
                if marker == b'p':
                    self.prefix_written.set()
                elif marker == b'f':
                    self.frame_written_at = time.monotonic()
                    self.frame_written.set()

        self.status_thread = threading.Thread(target=acknowledge)
        self.status_thread.start()
        return self.process

    def release(self, bulk=False):
        if bulk:
            self.bulk_released = True
            if self.bulk_released_at is None:
                self.bulk_released_at = time.monotonic()
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.stdin.write(b'x')
                self.process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def close(self):
        self.release()
        self.release(bulk=True)
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2)
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
            self.process.stdout.close()
        if self.status_thread is not None:
            self.status_thread.join(2)
        if self.status is not None:
            self.status.close()


class _Request:
    method = "GET"
    headers = {}


class _Response:
    def __init__(self):
        self.status = 200
        self.content_type = ""
        self.content_length = 0
        self.headers = {}


def _install_stubs():
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 1, 2, 3
    xbmc.executeJSONRPC = lambda payload: '{"result": {"value": 131072}}'
    xbmc.executebuiltin = lambda *a, **k: None
    xbmc.getInfoLabel = lambda label: ""
    xbmc.getCondVisibility = lambda condition: False
    sys.modules["xbmc"] = xbmc

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda id=None: types.SimpleNamespace(getSetting=lambda key: "")
    sys.modules["xbmcaddon"] = xbmcaddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = lambda window_id=None: types.SimpleNamespace(
        getProperty=lambda key: "", setProperty=lambda *a: None, clearProperty=lambda *a: None
    )
    sys.modules["xbmcgui"] = xbmcgui

    spotipy = types.ModuleType("spotipy")
    spotipy.Spotify = lambda auth=None: None
    sys.modules["spotipy"] = spotipy

    spotty = types.ModuleType("spotty")
    spotty.Spotty = object
    sys.modules["spotty"] = spotty

    utils = types.ModuleType("utils")
    utils.ADDON_ID = "plugin.audio.spotifykodiconnect"
    utils.ADDON_WINDOW_ID = 10000
    utils.ADDON_DATA_PATH = REPO_ROOT
    utils.LOGDEBUG = 0
    utils.get_valid_cached_auth_token = lambda: None
    utils.bytes_to_megabytes = lambda value: value / 1024 / 1024
    utils.kill_process_by_pid = lambda pid: None
    utils.log_msg = lambda *a, **k: None
    utils.log_exception = lambda *a, **k: None
    sys.modules["utils"] = utils

    like_actions = types.ModuleType("like_actions")
    like_actions.notify_not_connected = lambda: None
    like_actions.set_current_track_liked = lambda track_id, liked: True
    sys.modules["like_actions"] = like_actions

    bottle = types.ModuleType("bottle")
    bottle.request = _Request()
    bottle.response = _Response()
    bottle.Response = object
    sys.modules["bottle"] = bottle


_MODULES = (
    "http_spotty_audio_streamer",
    "spotty_audio_streamer",
    "spotty_cache",
    "prebuffer",
    "utils",
    "like_actions",
    "spotty",
    "spotipy",
    "xbmc",
    "xbmcaddon",
    "xbmcgui",
    "bottle",
)


class StreamSeamTests(unittest.TestCase):
    def setUp(self):
        for name in _MODULES:
            sys.modules.pop(name, None)
        _install_stubs()
        if LIB_DIR not in sys.path:
            sys.path.insert(0, LIB_DIR)
        import http_spotty_audio_streamer
        import prebuffer
        import spotty_audio_streamer
        import spotty_cache

        self.http = http_spotty_audio_streamer
        self.cache = spotty_cache
        self.prebuffer = prebuffer
        spotty_audio_streamer._THROTTLE_LEAD_BYTES = 1 << 40  # no real-time pacing
        spotty_cache.SpottyDownloader._RETRY_DELAYS = [0.0, 0.0, 0.0]
        self.header, self.track_length = spotty_audio_streamer.create_wav_header_for_duration(
            DURATION
        )
        self.ideal = self.header + RAMP
        self.assertEqual(self.track_length, len(self.ideal))
        self.open = []

    def tearDown(self):
        for body in self.open:
            body.close()
        self.cache.SpottyCacheManager.cleanup_all()
        for name in _MODULES:
            sys.modules.pop(name, None)

    def _server(self, spotty, prebuffer_manager=None):
        return self.http.HTTPSpottyAudioStreamer(spotty, prebuffer_manager=prebuffer_manager)

    def _get(self, server, range_header=None, duration=f"{DURATION}.wav"):
        self.http.bottle.request.headers = {"Range": range_header} if range_header else {}
        body = server.spotty_stream_audio_track(TRACK, duration)
        self.assertTrue(hasattr(body, "__iter__"), self.http.bottle.response.status)
        self.open.append(body)
        return body

    @staticmethod
    def _read(body, limit=None, between_chunks=None):
        out = bytearray()
        for chunk in body:
            out += chunk
            if between_chunks is not None:
                between_chunks(len(out))
            if limit is not None and len(out) >= limit:
                break
        body.close()
        return bytes(out if limit is None else out[:limit])

    def _assert_served(self, served, begin, whole=True):
        expected = self.ideal[begin : begin + len(served)]
        if served != expected:
            first = next(i for i, (a, b) in enumerate(zip(served, expected)) if a != b)
            pcm = begin + first - len(self.header)
            got = int.from_bytes(served[first - first % 4 : first - first % 4 + 4], "little")
            self.fail(
                f"served byte {begin + first} (PCM frame {pcm // 4}) carries frame {got}: "
                f"{pcm // 4 - got} frames repeated"
            )
        if whole:
            self.assertEqual(self.track_length - begin, len(served), "served length != WAV length")

    def _downloader(self):
        return self.cache.SpottyCacheManager.find_active_downloader(TRACK)

    @contextmanager
    def _pipe_http(self, prefix_bytes=4):
        from socketserver import ThreadingMixIn
        from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

        class Server(ThreadingMixIn, WSGIServer):
            daemon_threads = True

        class Handler(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        sys.modules.pop("bottle", None)
        deps = os.path.join(LIB_DIR, "deps")
        sys.path.insert(0, deps)
        import bottle

        self.http.bottle = bottle
        spotty = _PipeSpotty(prefix_bytes)
        streamer = self._server(spotty)
        app = bottle.Bottle()
        app.route("/track/<track_id>/<duration>", callback=streamer.spotty_stream_audio_track)
        server = make_server("127.0.0.1", 0, app, Server, Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        try:
            yield streamer, spotty, server.server_port
        finally:
            downloader = self._downloader()
            try:
                self.cache.SpottyCacheManager.cleanup_all()
                spotty.close()
                if downloader is not None and downloader.thread is not None:
                    downloader.thread.join(2)
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(2)
                sys.path.remove(deps)
            if downloader is not None and downloader.thread is not None:
                self.assertFalse(downloader.thread.is_alive(), "downloader survived cleanup")
            self.assertFalse(server_thread.is_alive(), "HTTP server survived cleanup")
            if spotty.status_thread is not None:
                self.assertFalse(spotty.status_thread.is_alive(), "status reader survived cleanup")
                self.assertIsNotNone(spotty.process.returncode, "producer was not reaped")
                print(f"real-pipe reaped pid={spotty.process.pid} rc={spotty.process.returncode} "
                      "downloader_joined=True status_joined=True server_joined=True")

    def _assert_pipe_http(self, begin=None, end=None, prefix_bytes=4):
        import http.client

        with self._pipe_http(prefix_bytes) as (streamer, spotty, port):
            first_response = threading.Event()
            finish_response = threading.Event()
            received, errors, headers, read_times = [], [], [], []
            start = time.monotonic()
            first_count = 48 if begin is None else end - begin + 1

            def request():
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    request_headers = {} if begin is None else {"Range": f"bytes={begin}-{end}"}
                    connection.request("GET", f"/track/{TRACK}/30.wav", headers=request_headers)
                    with connection.getresponse() as response:
                        headers.append((response.status, time.monotonic() - start))
                        received.append(response.read(first_count))
                        read_times.append(time.monotonic() - start)
                        first_response.set()
                        if begin is None:
                            finish_response.wait(3)
                            received.append(response.read())
                except Exception as exc:
                    errors.append(exc)
                    first_response.set()
                finally:
                    connection.close()

            requester = threading.Thread(target=request)
            requester.start()
            try:
                self.assertTrue(spotty.prefix_written.wait(2), "producer never wrote prefix")
                if prefix_bytes < 4:
                    downloader = self._downloader()
                    with downloader.cond:
                        self.assertTrue(downloader.cond.wait_for(
                            lambda: downloader.real_pcm_bytes == prefix_bytes, timeout=2
                        ), "partial PCM never left the real pipe")
                    self.assertFalse(first_response.is_set(), "partial frame admitted HTTP response")
                    self.assertEqual([], headers, "partial frame admitted HTTP headers")
                    released = []
                    self.assertFalse(streamer.release_stream(lambda: released.append(True)))
                    self.assertEqual([], released, "stale Stop released startup resources")
                    spotty.release()
                self.assertTrue(spotty.frame_written.wait(2), "producer never wrote complete frame")
                self.assertTrue(first_response.wait(1), "HTTP waited for bulk despite complete real frame")
                self.assertEqual([], errors)
                self.assertFalse(spotty.bulk_released)
                expected = self.ideal[:48] if begin is None else self.ideal[begin:end + 1]
                self.assertEqual([expected], received)
                self.assertEqual(200 if begin is None else 206, headers[0][0])
                print(f"real-pipe prefix={prefix_bytes} range={begin}:{end} "
                      f"headers_ms={headers[0][1] * 1000:.3f} "
                      f"first_read_ms={read_times[0] * 1000:.3f} "
                      f"frame_ack_ms={(spotty.frame_written_at - start) * 1000:.3f} bulk_released=False")
                spotty.release(bulk=True)
                finish_response.set()
                requester.join(3)
                self.assertFalse(requester.is_alive(), "HTTP request survived bulk release")
                self.assertEqual([], errors)
                if begin is None:
                    self.assertEqual(self.ideal, b"".join(received))
            finally:
                if prefix_bytes < 4 and not spotty.frame_written.is_set():
                    spotty.release()
                spotty.release(bulk=True)
                finish_response.set()
                requester.join(4)
                self.assertFalse(requester.is_alive(), "HTTP request survived cleanup")
                print(f"real-pipe cleanup producer_pid={spotty.process.pid if spotty.process else None} "
                      f"request_done={first_response.is_set()} first_read_bytes="
                      f"{len(received[0]) if received else 0} errors={len(errors)} "
                      f"headers_ms={headers[0][1] * 1000 if headers else None} "
                      f"bulk_release_ms={(spotty.bulk_released_at - start) * 1000:.3f}")

    @unittest.skipUnless(os.name == "posix", "producer status requires inherited descriptor")
    def test_real_pipe_serves_header_and_frame_before_bulk_release(self):
        self._assert_pipe_http()

    @unittest.skipUnless(os.name == "posix", "producer status requires inherited descriptor")
    def test_real_pipe_exact_ranges_require_complete_frame(self):
        for begin, end in ((0, 3), (0, 47), (20, 47), (43, 47), (44, 47)):
            with self.subTest(begin=begin, end=end):
                self._assert_pipe_http(begin, end)

    @unittest.skipUnless(os.name == "posix", "producer status requires inherited descriptor")
    def test_real_pipe_fragmented_frame_survives_stale_stop(self):
        for prefix_bytes in (1, 2, 3):
            with self.subTest(prefix_bytes=prefix_bytes):
                self._assert_pipe_http(0, 3, prefix_bytes)

    @unittest.skipUnless(os.name == "posix", "producer status requires inherited descriptor")
    def test_real_pipe_cancel_unblocks_partial_frame_without_bulk(self):
        with self._pipe_http(3) as (_, spotty, port):
            import http.client

            done = threading.Event()
            errors, responses = [], []

            def request():
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    connection.request("GET", f"/track/{TRACK}/30.wav")
                    with connection.getresponse() as response:
                        responses.append((response.status, response.read()))
                except Exception as exc:
                    errors.append(exc)
                finally:
                    connection.close()
                    done.set()

            requester = threading.Thread(target=request)
            requester.start()
            try:
                self.assertTrue(spotty.prefix_written.wait(2))
                downloader = self._downloader()
                downloader.abort()
                self.assertTrue(done.wait(1), "canceled HTTP startup did not return")
                downloader.thread.join(1)
                self.assertFalse(downloader.thread.is_alive(), "cancel left stdout read blocked")
                self.assertFalse(spotty.bulk_released)
                self.assertEqual([], errors)
                self.assertEqual(1, len(responses))
                self.assertGreaterEqual(responses[0][0], 400)
            finally:
                spotty.close()
                requester.join(4)
                self.assertFalse(requester.is_alive())

    def test_full_stream_matches_declared_wav(self):
        spotty = RampSpotty()
        served = self._read(self._get(self._server(spotty)))
        self._assert_served(served, 0)
        self.assertEqual([], spotty.seeks())

    def test_millisecond_duration_ends_with_the_audio(self):
        # A whole-second length (rounded up) padded silence after the music.
        spotty = RampSpotty(cuts=[None])
        served = self._read(self._get(self._server(spotty), duration="29.987.wav"))
        frames = round(29.987 * FRAMES_PER_SEC)
        self.assertEqual(len(self.header) + frames * 4, len(served))
        self.assertEqual(RAMP[: frames * 4], served[len(self.header) :])

    def test_offset_retry_is_sample_exact(self):
        # Spotty dies mid-track; the retry resumes at a whole second.
        spotty = RampSpotty(cuts=[3_000_002])
        served = self._read(self._get(self._server(spotty)))
        self.assertEqual(1, len(spotty.seeks()))
        self._assert_served(served, 0)

    def test_backpressure_pause_resume_and_exit_while_paused_is_sample_exact(self):
        self.cache._BP_HIGH_BYTES = 2_400_000
        self.cache._BP_LOW_BYTES = 2_200_000
        # Spotty exits just after the writer pauses; the retry happens on resume.
        spotty = RampSpotty(cuts=[2_490_368])
        server = self._server(spotty)
        body = self._get(server)

        def park_once(sent, state={"parked": False}):
            if state["parked"] or sent < 1_000_000:
                return
            state["parked"] = True
            deadline = time.monotonic() + 2.0
            downloader = self._downloader()
            while time.monotonic() < deadline:
                with downloader.cond:
                    if downloader._bp_paused:
                        return
                time.sleep(0.005)
            self.fail("writer never paused")

        served = self._read(body, between_chunks=park_once)
        self.assertEqual(1, len(spotty.seeks()))
        self._assert_served(served, 0)

    def test_kodi_reconnect_behind_served_position_reuses_cache(self):
        # Kodi drops its connection and resumes from what it actually
        # received, which trails what we pushed into the socket.
        spotty = RampSpotty()
        server = self._server(spotty)
        first = self._read(self._get(server), limit=3_200_000)
        self._assert_served(first, 0, whole=False)
        resume = 3_200_000 - 300_001
        served = self._read(self._get(server, f"bytes={resume}-"))
        self.assertEqual([], spotty.seeks())
        self._assert_served(served, resume)

    def test_parallel_header_and_data_readers_share_one_stream(self):
        spotty = RampSpotty()
        server = self._server(spotty)
        head = self._get(server, "bytes=0-")
        first = next(iter(head))
        data = self._read(self._get(server, "bytes=44-"))
        rest = self._read(head)
        self._assert_served(first + rest, 0)
        self._assert_served(data, 44)
        self.assertEqual(1, len(spotty.calls))

    def test_paused_prebuffer_handoff_and_reconnect_is_sample_exact(self):
        self.cache._BP_HIGH_BYTES = 2_400_000
        self.cache._BP_LOW_BYTES = 2_200_000
        spotty = RampSpotty()
        manager = self.prebuffer.PrebufferManager(spotty)
        downloader = manager.start_prebuffer(TRACK, DURATION)
        deadline = time.monotonic() + 2.0
        while not downloader._bp_paused and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(downloader._bp_paused)
        with downloader.cond:
            # The prebuffer has been parked for a whole previous track.
            downloader.last_progress_monotonic -= 120.0

        server = self._server(spotty, prebuffer_manager=manager)
        first = self._read(self._get(server), limit=150_000)
        self._assert_served(first, 0, whole=False)
        served = self._read(self._get(server, "bytes=150000-"))
        self.assertEqual([], spotty.seeks())
        self.assertEqual(1, len(spotty.calls))
        self._assert_served(served, 150_000)

    def test_resume_matcher_tolerates_dither_and_rejects_silence(self):
        import random

        rng = random.Random(7)
        signal, level = [], 0
        for _ in range(40000):  # smooth, non-periodic: neighbours differ little
            level = max(-30000, min(30000, level + rng.randint(-40, 40)))
            signal.append(level)
        clean = array.array("h", signal).tobytes()
        dithered = array.array("h", [v + rng.choice((-2, -1, 0, 1, 2)) for v in signal])
        ref = clean[60000:68192]
        # Offsets run the full window; the true end is 68192 + 400.
        data = bytes(400) + dithered.tobytes()
        found = self.cache._find_resume_offset(ref, data, 60000, 80000)
        self.assertEqual(68192 + 400, found)
        self.assertIsNone(self.cache._find_resume_offset(bytes(8192), bytes(80000), 0, 80000))


if __name__ == "__main__":
    unittest.main()
