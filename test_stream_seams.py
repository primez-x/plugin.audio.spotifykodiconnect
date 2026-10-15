"""End-to-end seam checks: real HTTP/streamer/cache code against a fake Spotty.

The fake emits a PCM ramp where every 4-byte frame encodes its own index, and
honours --start-position the way the real binary does (measured on CoreELEC,
spotty 1.3.1 / librespot 0.4.2): it lands a few tens of milliseconds *before*
the requested second.  Every served byte must equal the ideal WAV, so any
repeated or missing frame at a seam fails the comparison.
"""

import array
import ast
from contextlib import contextmanager
import hashlib
import io
import os
import subprocess
import sys
import threading
import tempfile
from unittest.mock import patch
import time
import types
import unittest
import wave

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

    def get_spotty_credentials_identity(self):
        return ("fixture-account", getattr(self, "account", 1))

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

    def get_spotty_credentials_identity(self):
        return ("fixture-account", getattr(self, "account", 1))

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


class _RetryPipeSpotty(RampSpotty):
    """Real helper pipes with optional first-attempt exit control."""

    def __init__(self, cuts, gate_first=False, frames=DURATION * FRAMES_PER_SEC):
        super().__init__(cuts)
        self.gate_first = gate_first
        self.frames = frames
        self.corrupt_retries = False
        self.processes = []
        self.process = None
        self.status_thread = None

    def run_spotty(self, args):
        self.calls.append(list(args))
        start = 0
        if "--start-position" in args:
            start = _seek_frame(int(args[args.index("--start-position") + 1])) * 4
        cut = self.cuts.pop(0) if self.cuts else None
        producer = """
import array, sys
start, cut, gate, frames, corrupt = map(int, sys.argv[1:])
pcm = array.array('I', range(frames)).tobytes()[start:]
if corrupt:
    pcm = bytes(len(pcm))
sys.stdout.buffer.write(pcm if cut < 0 else pcm[:cut])
sys.stdout.buffer.flush()
if gate:
    sys.stdin.buffer.read(1)
sys.exit(0 if cut < 0 else 1)
"""
        self.process = subprocess.Popen(
            [sys.executable, "-c", producer, str(start), str(-1 if cut is None else cut),
             str(int(self.gate_first and len(self.calls) == 1)), str(self.frames),
             str(int(self.corrupt_retries and len(self.calls) > 1))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=1048576,
        )
        self.processes.append(self.process)
        return self.process

    def release(self, bulk=False):
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.stdin.write(b"x")
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass

    def close(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            process.stdout.close()
            print(f"retry-pipe reaped pid={process.pid} rc={process.returncode}")


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

        self.profile = tempfile.TemporaryDirectory(prefix="spotify-pcm-test-")
        sys.modules["utils"].ADDON_DATA_PATH = self.profile.name
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
        self.profile.cleanup()
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
    def _pipe_http(self, prefix_bytes=4, spotty=None):
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
        spotty = spotty or _PipeSpotty(prefix_bytes)
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

    def _stop_service(self, server):
        from test_main_service_osd import import_main_service

        modules = dict(sys.modules)
        thread_class = threading.Thread
        try:
            main = import_main_service({})
            main.xbmc.Player = lambda: types.SimpleNamespace(isPlaying=lambda: False)
            main.bottle_manager.stop_thread = lambda: None
            service = object.__new__(main.MainService)
            service._MainService__http_spotty_streamer = server
            service._MainService__prebuffer_manager = self.prebuffer.PrebufferManager(object())
            service._MainService__spotty_helper = types.SimpleNamespace(kill_all_spotties=lambda: None)
            service._prebuffer_token = 0
            service._prebuffer_token_lock = threading.Lock()
        finally:
            threading.Thread = thread_class
            for name in tuple(sys.modules):
                if name in modules:
                    sys.modules[name] = modules[name]
                else:
                    sys.modules.pop(name, None)
        return service

    def test_pcm_prefix_survives_old_trim_and_real_stop_replays_same_helper(self):
        import http.client

        frames = 180 * FRAMES_PER_SEC
        spotty = _RetryPipeSpotty([], frames=frames)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            service = self._stop_service(server)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                path = f"/track/{TRACK}/180000ms"
                connection.request("GET", path, headers={"Range": "bytes=0-12582911"})
                with connection.getresponse() as response:
                    self.assertEqual(206, response.status)
                    original = response.read()
                downloader = self._downloader()
                self.assertIs(downloader, self.cache.SpottyCacheManager.find_best_downloader(TRACK, 0),
                              "byte zero disappeared after more than 60 seconds of reader progress")
                old_body = server._HTTPSpottyAudioStreamer__spotty_streamer.send_part_audio_stream(48, 0)
                service._release_after_stop()
                service._release_after_stop()
                self.assertIsNone(server._HTTPSpottyAudioStreamer__current_track_id)
                connection.request("GET", path, headers={"Range": "bytes=0-12582911"})
                with connection.getresponse() as response:
                    self.assertEqual(206, response.status)
                    self.assertEqual(original, response.read())
                self.assertIs(downloader, self._downloader())
                self.assertEqual(1, len(spotty.calls))
                self.assertEqual(b"", b"".join(old_body))
                connection.request("GET", path, headers={"Range": "bytes=12582912-"})
                with connection.getresponse() as response:
                    tail = response.read()
                expected = _ramp(frames)
                self.assertEqual(expected, original[44:] + tail)
                self.assertEqual(1, len(spotty.calls))
                service._release_after_stop()
                connection.request("GET", path, headers={"Range": "bytes=41-113"})
                with connection.getresponse() as response:
                    self.assertEqual((downloader.wav_header + expected)[41:114], response.read())
                self.assertEqual(1, len(spotty.calls))
                service._release_after_stop()
                service._teardown_for_external_playback()
                self.assertEqual({}, self.cache.SpottyCacheManager._instances)
                print("pcm-replay progress_bytes=12582912 full_stop=True helper_calls=1 "
                      "frontier_exact=True complete_replay=True external_cleanup=True")
            finally:
                connection.close()

    def test_pcm_full_stop_replays_same_producer(self):
        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                for cycle in range(2):
                    connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
                    with connection.getresponse() as response:
                        self.assertEqual((server._HTTPSpottyAudioStreamer__spotty_streamer.get_stream_spec().wav_header + RAMP[:20]), response.read())
                    if cycle == 0:
                        original = self._downloader()
                        self._stop_service(server)._release_after_stop()
                self.assertIs(original, self._downloader(), "Stop launched a replacement helper")
                self.assertEqual(1, len(spotty.calls))
            finally:
                connection.close()

    def test_pcm_stop_wakes_unavailable_byte_reader_without_aborting_store(self):
        spotty = _PipeSpotty()
        with self._pipe_http(spotty=spotty) as (server, _, _):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", _, timeout=3)
            connection.request("GET", f"/track/{TRACK}/30.wav", headers={"Range": "bytes=0-47"})
            with connection.getresponse() as response:
                self.assertEqual(self.ideal[:48], response.read())
            connection.close()
            downloader = self._downloader()
            service = self._stop_service(server)
            low = server._HTTPSpottyAudioStreamer__spotty_streamer
            body = low.send_part_audio_stream(8, 44)
            self.assertEqual(self.ideal[44:48], next(body))
            waiting = threading.Event()
            original = downloader.wait_for_bytes

            def wait(*args, **kwargs):
                waiting.set()
                return original(*args, **kwargs)

            downloader.wait_for_bytes = wait
            result = []
            worker = threading.Thread(target=lambda: result.append(b"".join(body)))
            worker.start()
            self.assertTrue(waiting.wait(1))
            started = time.monotonic()
            service._release_after_stop()
            worker.join(0.2)
            try:
                self.assertFalse(worker.is_alive(), "retired reader remained in the one-second wait")
                self.assertFalse(downloader.aborted, "Stop destroyed reusable PCM")
                self.assertEqual([b""], result)
                self.assertLess(time.monotonic() - started, 0.2)
            finally:
                self.cache.SpottyCacheManager.cleanup_all()
                worker.join(2)

    def test_pcm_identity_changes_reject_retained_bytes(self):
        spotty = RampSpotty()
        server = self._server(spotty)
        self._read(self._get(server, "bytes=0-63"))
        original = self._downloader()
        spotty.account = 2
        self._read(self._get(server, "bytes=0-63"))
        self.assertIsNot(original, self._downloader(), "old-account PCM entered a new generation")
        self.assertTrue(original.aborted)
        self.assertEqual(2, len(spotty.calls))

    def test_pcm_file_quota_retired_open_store_counts_until_close(self):
        spotty = RampSpotty()
        server = self._server(spotty)
        self._read(self._get(server, "bytes=0-63"))
        downloader = self._downloader()
        manager = self.cache.SpottyCacheManager
        self.assertEqual(self.track_length, getattr(manager, "_file_bytes", 0),
                         "zero-origin store has no declared-length reservation")
        downloader.thread.join(2)
        self.assertTrue(downloader.is_finished)
        entered, release, closing = threading.Event(), threading.Event(), threading.Event()
        store = downloader._file_store
        original = store._file.read
        original_close = store.close

        def close():
            closing.set()
            return original_close()

        def read(size):
            entered.set()
            release.wait(2)
            return original(size)

        with patch.object(store._file, "read", side_effect=read), patch.object(store, "close", side_effect=close):
            reader = threading.Thread(target=lambda: downloader.read_range(0, 64))
            reader.start()
            self.assertTrue(entered.wait(1))
            cleanup = threading.Thread(target=manager.cleanup_all)
            cleanup.start()
            try:
                self.assertTrue(closing.wait(1))
                self.assertEqual(self.track_length, manager._file_bytes)
                self.assertFalse(manager._lock.locked(), "file close held the manager lock")
                with patch.object(manager, "_MAX_FILE_BYTES", self.track_length):
                    fresh = self._server(RampSpotty())
                    self._read(self._get(fresh, "bytes=0-63"))
                    self.assertIsNone(self._downloader()._file_store)
                    self.assertEqual(self.track_length, manager._file_bytes)
            finally:
                release.set()
                reader.join(2)
                cleanup.join(2)
        self.assertEqual(0, manager._file_bytes)
        self.assertTrue(store._file.closed)

    def test_pcm_storage_error_fails_without_padding_or_retry(self):
        import http.client

        for failure in ("ENOSPC", "zero-write", "partial-error"):
            with self.subTest(failure=failure), self._pipe_http(spotty=_PipeSpotty()) as (server, spotty, port):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", f"/track/{TRACK}/30.wav", headers={"Range": "bytes=0-47"})
                with connection.getresponse() as response:
                    self.assertEqual(self.ideal[:48], response.read())
                connection.close()
                downloader = self._downloader()
                self.assertIsNotNone(getattr(downloader, "_file_store", None), "PCM stayed in rolling RAM")
                process = spotty.process
                original_write = downloader._file_store._file.write
                writes = []

                def fail_write(data):
                    writes.append(len(data))
                    if failure == "zero-write":
                        return 0
                    if failure == "partial-error" and len(writes) == 1:
                        return original_write(data[:1024])
                    raise OSError(28, "full")

                with patch.object(downloader._file_store._file, "write", side_effect=fail_write):
                    spotty.release(bulk=True)
                    downloader.thread.join(2)
                self.assertTrue(downloader.error)
                self.assertTrue(downloader.is_finished)
                self.assertEqual(48, downloader.written_bytes)
                self.assertIs(process, spotty.process, "storage failure restarted authentication")
                self.assertEqual(b"", b"".join(server._HTTPSpottyAudioStreamer__spotty_streamer.send_part_audio_stream(4, 48, downloader=downloader)))

    def test_pcm_short_writes_and_committed_read_failures_keep_http_honest(self):
        import http.client

        with self._pipe_http(spotty=_PipeSpotty()) as (server, spotty, port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", f"/track/{TRACK}/30.wav", headers={"Range": "bytes=0-47"})
            connection.getresponse().read()
            downloader = self._downloader()
            original_write = downloader._file_store._file.write
            with patch.object(downloader._file_store._file, "write", side_effect=lambda data: original_write(data[:32767])):
                spotty.release(bulk=True)
                connection.request("GET", f"/track/{TRACK}/30.wav", headers={"Range": "bytes=48-"})
                with connection.getresponse() as response:
                    self.assertEqual(self.ideal[48:], response.read())
            connection.close()
            downloader.thread.join(2)
            self.assertFalse(downloader.error)
            self.assertEqual(self.track_length, downloader.written_bytes)
            for failure in ("short-read", "read-error"):
                self.cache.SpottyCacheManager.cleanup_all()
                fake = RampSpotty()
                fresh_server = self._server(fake)
                self.http.bottle = types.SimpleNamespace(request=_Request(), response=_Response())
                body = self._get(fresh_server, "bytes=0-63")
                cached = self._downloader()
                cached.thread.join(2)
                if failure == "short-read":
                    effect = lambda size: b""
                else:
                    def effect(size):
                        raise OSError(5, "read failed")
                with patch.object(cached._file_store._file, "read", side_effect=effect):
                    self.assertEqual(b"", self._read(body))
                self.assertTrue(cached.error)
                self.assertEqual(1, len(fake.calls))

    def test_pcm_unwritable_store_fails_before_http_headers_and_releases_quota(self):
        spotty = RampSpotty()
        server = self._server(spotty)
        with patch.object(self.cache.tempfile, "TemporaryFile", side_effect=PermissionError("denied")):
            self.http.bottle.request.headers = {"Range": "bytes=0-63"}
            result = server.spotty_stream_audio_track(TRACK, "30")
        self.assertEqual(503, self.http.bottle.response.status)
        self.assertEqual("", result)
        self.assertEqual([], spotty.calls)
        self.assertEqual(0, self.cache.SpottyCacheManager._file_bytes)

    def test_pcm_audio_identity_changes_before_headers(self):
        for field, changed in (("duration", 31), ("bitrate", "160"), ("normalization", "off"), ("volume", 61)):
            with self.subTest(field=field):
                self.cache.SpottyCacheManager.cleanup_all()
                spotty = RampSpotty()
                server = self._server(spotty)
                self._read(self._get(server, "bytes=0-63"))
                original = self._downloader()
                duration, bitrate, normalization = 30, "320", "auto"
                if field == "duration":
                    duration = changed
                elif field == "bitrate":
                    bitrate = changed
                elif field == "normalization":
                    normalization = changed
                else:
                    server._HTTPSpottyAudioStreamer__spotty_streamer.set_initial_volume(changed)
                with patch.object(self.http, "_get_current_stream_settings", return_value=(bitrate, normalization)):
                    body = self._get(server, "bytes=0-63", duration=str(duration))
                    self._read(body)
                self.assertIsNot(original, self._downloader(), field)
                self.assertTrue(original.aborted)
                self.assertEqual(2, len(spotty.calls))

    def test_pcm_incomplete_idle_lease_reaps_and_complete_store_survives(self):
        with self._pipe_http(spotty=_RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)) as (server, spotty, port):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
                connection.getresponse().read()
                downloader = self._downloader()
                self.assertIsNotNone(getattr(downloader, "_file_store", None))
                with patch.object(self.cache, "_RETAIN_IDLE_SECONDS", 0.1, create=True):
                    self._stop_service(server)._release_after_stop()
                    downloader.thread.join(2)
                self.assertFalse(downloader.thread.is_alive())
                self.assertTrue(downloader.aborted)
                self.assertIsNotNone(spotty.process.poll())
                self.assertEqual(0, self.cache.SpottyCacheManager._file_bytes)
                self.assertEqual(1, len(spotty.calls), "idle lease launched a retry")
            finally:
                connection.close()

    def test_pcm_quota_defers_prebuffer_and_preserves_rolling_fallback(self):
        manager = self.cache.SpottyCacheManager
        self.assertTrue(hasattr(manager, "_MAX_FILE_BYTES"), "aggregate file quota missing")
        with patch.object(manager, "_MAX_FILE_BYTES", self.track_length):
            server = self._server(RampSpotty())
            self._read(self._get(server, "bytes=0-63"))
            first = self._downloader()
            first.thread.join(2)
            self.assertFalse(first.thread.is_alive())
            prebuffer = self.prebuffer.PrebufferManager(RampSpotty())
            self.assertIsNone(prebuffer.start_prebuffer("next", DURATION))
            self.assertEqual(self.track_length, manager._file_bytes)
            service = self._stop_service(server)
            service._release_after_stop()
            with patch.object(manager, "_MAX_FILE_BYTES", self.track_length - 1):
                other = self._server(RampSpotty())
                body = self._get(other, "bytes=0-63", duration="31")
                self._read(body)
                fallback = self._downloader()
                self.assertIsNone(fallback._file_store)
                self.assertLessEqual(len(fallback._buffer), fallback._MAX_UNREAD_TAIL_BYTES)

    def test_pcm_dead_retained_helper_rejects_before_replay_headers(self):
        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                path = f"/track/{TRACK}/180.wav"
                connection.request("GET", path, headers={"Range": "bytes=0-63"})
                connection.getresponse().read()
                first = self._downloader()
                self._stop_service(server)._release_after_stop()
                spotty.process.kill()
                spotty.process.wait(timeout=2)
                connection.request("GET", path, headers={"Range": "bytes=0-63"})
                with connection.getresponse() as response:
                    self.assertEqual(206, response.status)
                    self.assertEqual(64, len(response.read()))
                self.assertIsNot(first, self._downloader())
                self.assertEqual(2, len(spotty.calls))
            finally:
                connection.close()

    def test_pcm_prebuffer_and_active_health_reject_account_replacement(self):
        spotty = RampSpotty()
        manager = self.prebuffer.PrebufferManager(spotty)
        downloader = manager.start_prebuffer(TRACK, DURATION)
        self.assertTrue(downloader.wait_for_real_pcm(4, timeout=2))
        spotty.account = 2
        self.assertIsNone(self.cache.SpottyCacheManager.find_active_downloader(TRACK),
                          "health lookup accepted changed account metadata")
        result, hit = manager.get_and_clear_prebuffer(TRACK)
        self.assertFalse(hit, "prebuffer accepted changed account metadata")
        self.assertIsNone(result)
        self.assertTrue(downloader.aborted)

    def test_pcm_retired_body_cannot_read_during_stop_disk_overlap(self):
        spotty = RampSpotty()
        server = self._server(spotty)
        self._read(self._get(server, "bytes=0-63"))
        downloader = self._downloader()
        self.assertIsNotNone(downloader._file_store)
        downloader.thread.join(2)
        self.assertTrue(downloader.is_finished)
        low = server._HTTPSpottyAudioStreamer__spotty_streamer
        body = low.send_part_audio_stream(64, 0)
        entered, release = threading.Event(), threading.Event()
        original = downloader._file_store._file.read
        result = []

        def read(size):
            entered.set()
            release.wait(2)
            return original(size)

        with patch.object(downloader._file_store._file, "read", side_effect=read):
            worker = threading.Thread(target=lambda: result.append(b"".join(body)))
            worker.start()
            self.assertTrue(entered.wait(1))
            started = time.monotonic()
            self._stop_service(server)._release_after_stop()
            self.assertLess(time.monotonic() - started, 0.2)
            release.set()
            worker.join(2)
        self.assertEqual([b""], result)
        self.assertFalse(downloader.aborted)

    def test_pcm_first_body_rechecks_account_after_disk_read(self):
        spotty = RampSpotty()
        server = self._server(spotty)
        body = self._get(server, "bytes=0-63")
        downloader = self._downloader()
        original = downloader._file_store.read

        def changed_read(offset, size):
            data = original(offset, size)
            spotty.account = 2
            return data

        with patch.object(downloader._file_store, "read", side_effect=changed_read):
            self.assertEqual(b"", self._read(body))
        self.assertTrue(downloader.aborted)

    def test_pcm_active_replay_cancels_idle_lease_before_cached_frontier(self):
        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
                connection.getresponse().read()
                downloader = self._downloader()
                with patch.object(self.cache, "_RETAIN_IDLE_SECONDS", 0.1):
                    self._stop_service(server)._release_after_stop()
                    body = server._HTTPSpottyAudioStreamer__spotty_streamer.send_part_audio_stream(64, 0)
                    self.assertEqual(64, len(next(body)))
                    try:
                        downloader.thread.join(0.3)
                        self.assertFalse(downloader.aborted, "idle lease expired during an active cached replay")
                        self.assertEqual(1, len(spotty.calls))
                    finally:
                        body.close()
                    downloader.thread.join(2)
                    self.assertTrue(downloader.aborted)
                    self.assertFalse(downloader.thread.is_alive())
            finally:
                connection.close()

    def test_pcm_watchdog_rechecks_expired_snapshot_after_replay_attaches(self):
        source = self.cache.__file__
        with open(source, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        watchdog = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_watch_attempt"
        )
        checkpoint = next(
            node.lineno for node in ast.walk(watchdog)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name)
            and node.test.id == "retained_idle"
        )
        loop_line = next(node.lineno for node in ast.walk(watchdog) if isinstance(node, ast.While))
        downloader = self.cache.SpottyDownloader(
            RampSpotty(), TRACK, DURATION, 0, "320", "off", 35,
            self.header, self.track_length,
        )
        killed = threading.Event()
        process = types.SimpleNamespace(kill=killed.set, poll=lambda: None)
        downloader.process = process
        downloader._retained_idle = True
        downloader._retained_until = time.monotonic() - 1
        snapshot, release, cycle_done = threading.Event(), threading.Event(), threading.Event()
        attempt_done = threading.Event()
        errors = []
        namespace = dict(
            vars(self.cache), self=downloader, attempt_process=process,
            attempt_done=attempt_done, attempt_state_lock=threading.Lock(),
            attempt_state={"started_at": None, "last_output_at": None,
                           "has_real_pcm": False, "timeout_reason": "", "bp_paused": True},
        )
        exec(compile(ast.Module(body=[watchdog], type_ignores=[]), source, "exec"), namespace)

        def trace(frame, event, arg):
            if event == "line" and frame.f_code.co_name == "_watch_attempt":
                if frame.f_lineno == checkpoint and not snapshot.is_set():
                    snapshot.set()
                    if not release.wait(2):
                        raise AssertionError("watchdog snapshot barrier timed out")
                elif frame.f_lineno == loop_line and snapshot.is_set():
                    cycle_done.set()
            return trace

        def watch():
            sys.settrace(trace)
            try:
                namespace["_watch_attempt"]()
            except BaseException as exc:
                errors.append(exc)
            finally:
                sys.settrace(None)
                cycle_done.set()

        worker = threading.Thread(target=watch)
        worker.start()
        try:
            self.assertTrue(snapshot.wait(1))
            with downloader.cond:
                downloader.attach_reader_locked(0)
                self.assertIsNone(downloader._retained_until)
            release.set()
            self.assertTrue(cycle_done.wait(1))
        finally:
            release.set()
            attempt_done.set()
            worker.join(2)
        self.assertFalse(worker.is_alive(), "watchdog deadlocked during lease expiry")
        self.assertEqual([], errors)
        self.assertFalse(downloader.aborted, "stale lease snapshot aborted an active replay")
        self.assertFalse(killed.is_set(), "stale lease snapshot killed the active helper")

    def test_pcm_current_cache_hit_retires_next_helper(self):
        main_spotty = RampSpotty()
        server = self._server(main_spotty)
        self._read(self._get(server, "bytes=0-63"))
        current = self._downloader()
        current.thread.join(2)
        next_spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        try:
            prebuffer = self.prebuffer.PrebufferManager(next_spotty)
            next_download = prebuffer.start_prebuffer("next", 180)
            self.assertTrue(next_download.wait_for_real_pcm(4, timeout=2))
            self._read(self._get(server, "bytes=0-63"))
            self.assertIs(current, self._downloader())
            self.assertTrue(next_download.aborted, "cache-hit route left a competing helper alive")
            self.assertIsNotNone(next_spotty.process.poll())
        finally:
            self.cache.SpottyCacheManager.cleanup_all()
            next_spotty.close()

    def test_pcm_cancelled_prebuffer_cannot_publish_after_stop(self):
        prebuffer = self.prebuffer.PrebufferManager(RampSpotty())
        entered, release = threading.Event(), threading.Event()
        manager = self.cache.SpottyCacheManager
        original = manager.get_or_start
        results = []

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(2)
            return original(*args, **kwargs)

        with patch.object(manager, "get_or_start", side_effect=delayed):
            worker = threading.Thread(target=lambda: results.append(prebuffer.start_prebuffer(TRACK, DURATION)))
            worker.start()
            self.assertTrue(entered.wait(1))
            prebuffer.cancel_prebuffer()
            manager.cleanup_all()
            release.set()
            worker.join(2)
        self.assertEqual([None], results)
        self.assertEqual({}, manager._instances, "cancelled prebuffer resurrected audio after cleanup")

    def test_pcm_failed_optional_start_cannot_abort_playback_takeover(self):
        import http.client

        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            manager = self.cache.SpottyCacheManager
            prebuffer = self.prebuffer.PrebufferManager(spotty)
            failed, release = threading.Event(), threading.Event()
            original = self.cache.SpottyDownloader.start
            results, candidates = [], []

            def held_start(candidate):
                started = original(candidate)
                if started is False:
                    candidates.append(candidate)
                    failed.set()
                    if not release.wait(3):
                        raise AssertionError("optional acquisition barrier timed out")
                return started

            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            with patch.object(manager, "_MAX_FILE_BYTES", 0), patch.object(
                self.cache.SpottyDownloader, "start", held_start
            ):
                worker = threading.Thread(
                    target=lambda: results.append(prebuffer.start_prebuffer(TRACK, 180))
                )
                worker.start()
                try:
                    self.assertTrue(failed.wait(2))
                    connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
                    with connection.getresponse() as response:
                        self.assertEqual(206, response.status)
                        self.assertEqual(RAMP[:20], response.read()[44:])
                    playback = self._downloader()
                    self.assertIs(candidates[0], playback)
                    self.assertIsNone(playback._file_store)
                    release.set()
                    worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual([None], results)
                    self.assertIs(playback, self._downloader())
                    self.assertFalse(playback.aborted)
                    self.assertIsNone(spotty.process.poll())
                    connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=64-127"})
                    with connection.getresponse() as response:
                        self.assertEqual(206, response.status)
                        self.assertEqual(RAMP[20:84], response.read())
                    self.assertEqual(1, len(spotty.calls))
                finally:
                    release.set()
                    worker.join(3)
                    connection.close()

    def test_pcm_late_old_acquisition_cannot_abort_new_track(self):
        spotty = RampSpotty()
        server = self._server(spotty)
        self._read(self._get(server, "bytes=0-63"))
        low = server._HTTPSpottyAudioStreamer__spotty_streamer
        old_spec = low.get_stream_spec()
        entered, release = threading.Event(), threading.Event()
        manager = self.cache.SpottyCacheManager
        original = manager.get_or_start
        results = []
        old_thread = None

        def delayed(*args, **kwargs):
            if threading.current_thread() is old_thread:
                entered.set()
                release.wait(2)
            return original(*args, **kwargs)

        with patch.object(manager, "get_or_start", side_effect=delayed):
            old_thread = threading.Thread(target=lambda: results.append(low.prepare_part_audio_stream(0, stream_spec=old_spec)))
            old_thread.start()
            self.assertTrue(entered.wait(1))
            self._stop_service(server)._release_after_stop()
            self.http.bottle.request.headers = {"Range": "bytes=0-63"}
            body = server.spotty_stream_audio_track("replacement", "30")
            self._read(body)
            replacement = manager.find_active_downloader("replacement")
            self.assertIsNotNone(replacement)
            release.set()
            old_thread.join(2)
        self.assertEqual([False], results)
        self.assertFalse(replacement.aborted, "late old acquisition destroyed the new generation")
        self.assertIs(replacement, manager.find_active_downloader("replacement"))

    def test_pcm_cancelled_wait_cannot_resume_idle_producer(self):
        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
                connection.getresponse().read()
                downloader = self._downloader()
                low = server._HTTPSpottyAudioStreamer__spotty_streamer
                generation = low.get_stream_generation()
                entered, release = threading.Event(), threading.Event()
                results = []

                def retired_wait():
                    entered.set()
                    release.wait(2)
                    results.append(downloader.wait_for_bytes(
                        downloader.track_length, timeout=1,
                        cancelled=lambda: low._is_terminated(generation)))

                worker = threading.Thread(target=retired_wait)
                worker.start()
                self.assertTrue(entered.wait(1))
                with patch.object(self.cache, "_RETAIN_IDLE_SECONDS", 0.1):
                    self._stop_service(server)._release_after_stop()
                    release.set()
                    worker.join(1)
                    self.assertEqual([False], results)
                    downloader.thread.join(2)
                    self.assertFalse(downloader.thread.is_alive(), "cancelled reader disabled the idle lease")
                    self.assertTrue(downloader.aborted)
            finally:
                connection.close()

    def test_pcm_replay_waits_for_release_reservation(self):
        import http.client

        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            path = f"/track/{TRACK}/180.wav"
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", path, headers={"Range": "bytes=0-63"})
            connection.getresponse().read()
            connection.close()
            original = self._downloader()
            service = self._stop_service(server)
            entered, release, replay_started = threading.Event(), threading.Event(), threading.Event()
            received, errors = [], []
            retain = self.cache.SpottyCacheManager.retain_after_stop

            def held(spec):
                entered.set()
                release.wait(2)
                return retain(spec)

            def replay():
                reader = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    replay_started.set()
                    reader.request("GET", path, headers={"Range": "bytes=0-63"})
                    with reader.getresponse() as response:
                        received.append((response.status, response.read()))
                except Exception as exc:
                    errors.append(type(exc).__name__)
                finally:
                    reader.close()

            with patch.object(self.cache.SpottyCacheManager, "retain_after_stop", side_effect=held):
                stopper = threading.Thread(target=service._release_after_stop)
                stopper.start()
                self.assertTrue(entered.wait(1))
                requester = threading.Thread(target=replay)
                requester.start()
                self.assertTrue(replay_started.wait(1))
                requester.join(0.1)
                self.assertTrue(requester.is_alive(), "replay passed the held release reservation")
                release.set()
                stopper.join(2)
                requester.join(2)
            self.assertEqual([], errors)
            self.assertEqual([(206, original.wav_header + RAMP[:20])], received)
            self.assertIs(original, self._downloader())
            self.assertEqual(1, len(spotty.calls))

    def test_pcm_unmatched_helper_restart_truncates_without_nominal_splice(self):
        import http.client

        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        spotty.corrupt_retries = True
        with self._pipe_http(spotty=spotty) as (server, _, port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                path = f"/track/{TRACK}/180.wav"
                connection.request("GET", path, headers={"Range": "bytes=0-63"})
                connection.getresponse().read()
                downloader = self._downloader()
                self._stop_service(server)._release_after_stop()
                connection.request("GET", path)
                response = connection.getresponse()
                prefix = response.read(64)
                self.assertEqual(downloader.wav_header + RAMP[:20], prefix)
                spotty.process.kill()
                spotty.process.wait(timeout=2)
                with self.assertRaises(http.client.IncompleteRead) as caught:
                    response.read()
                partial = prefix + caught.exception.partial
                expected = downloader.wav_header + _ramp(180 * FRAMES_PER_SEC)
                self.assertEqual(expected[:len(partial)], partial)
                self.assertLess(len(partial), len(expected))
                downloader.thread.join(2)
                self.assertTrue(downloader.error)
                self.assertEqual("resume", downloader.failure_cause)
                self.assertEqual(2, len(spotty.calls))
            finally:
                connection.close()

    def test_pcm_shutdown_after_stop_closes_retained_file_and_helper(self):
        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
            connection.getresponse().read()
            connection.close()
            downloader = self._downloader()
            service = self._stop_service(server)
            service._release_after_stop()
            self.assertFalse(downloader.aborted)
            service._MainService__close()
            self.assertTrue(downloader.aborted)
            self.assertTrue(downloader._file_store._file.closed)
            self.assertFalse(downloader.thread.is_alive())
            self.assertIsNotNone(spotty.process.poll())
            self.assertEqual(0, self.cache.SpottyCacheManager._file_bytes)
            self.assertEqual({}, self.cache.SpottyCacheManager._instances)

    def test_pcm_shutdown_rejects_late_prebuffer_admission(self):
        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
            connection.getresponse().read()
            connection.close()
            service = self._stop_service(server)
            service._release_after_stop()
            prebuffer = self.prebuffer.PrebufferManager(spotty)
            service._MainService__prebuffer_manager = prebuffer
            manager = self.cache.SpottyCacheManager
            cleanup = manager.cleanup_all
            delayed = []

            def late_prebuffer(*args, **kwargs):
                result = cleanup(*args, **kwargs)
                delayed.append(prebuffer.start_prebuffer(TRACK + "-late", 180))
                return result

            with patch.object(manager, "cleanup_all", side_effect=late_prebuffer):
                service._MainService__close()
            self.assertEqual([None], delayed)
            self.assertEqual({}, manager._instances)
            self.assertEqual(0, manager._file_bytes)

    def test_pcm_shutdown_rejects_http_admission_after_generation_retirement(self):
        import http.client

        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
                connection.getresponse().read()
                low = server._HTTPSpottyAudioStreamer__spotty_streamer
                generation = low.get_stream_generation()
                self._stop_service(server)._MainService__close()
                self.assertNotEqual(generation, low.get_stream_generation())
                connection.request("GET", "/track/late-http/180.wav", headers={"Range": "bytes=0-63"})
                with connection.getresponse() as response:
                    self.assertEqual(503, response.status)
                    self.assertEqual(b"", response.read())
                self.assertEqual(1, len(spotty.calls))
                self.assertEqual({}, self.cache.SpottyCacheManager._instances)
                self.assertEqual(0, self.cache.SpottyCacheManager._file_bytes)
            finally:
                connection.close()

    def test_pcm_shutdown_rejects_http_waiting_before_cache_publication(self):
        import http.client

        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            manager = self.cache.SpottyCacheManager
            entered, release = threading.Event(), threading.Event()
            original = manager.get_or_start
            results, errors = [], []

            def held(*args, **kwargs):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("HTTP cache admission barrier timed out")
                return original(*args, **kwargs)

            def request():
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
                    with connection.getresponse() as response:
                        results.append((response.status, response.read()))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    connection.close()

            with patch.object(manager, "get_or_start", side_effect=held):
                worker = threading.Thread(target=request)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    self._stop_service(server)._MainService__close()
                finally:
                    release.set()
                    worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([(503, b"")], results)
            self.assertEqual([], spotty.calls)
            self.assertEqual({}, manager._instances)
            self.assertEqual(0, manager._file_bytes)

    def test_pcm_shutdown_reaps_published_start_before_late_acquisition_return(self):
        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, _):
            manager = self.cache.SpottyCacheManager
            header, length = sys.modules["spotty_audio_streamer"].create_wav_header_for_duration(180)
            entered, release = threading.Event(), threading.Event()
            original = self.cache.SpottyDownloader.start
            results, candidates, errors = [], [], []

            def held_start(candidate):
                started = original(candidate)
                candidates.append(candidate)
                entered.set()
                if not release.wait(3):
                    raise AssertionError("published startup barrier timed out")
                return started

            def acquire():
                try:
                    results.append(manager.get_or_start(
                        spotty, TRACK, 180, 0, "320", "auto", 35, header, length,
                    ))
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(self.cache.SpottyDownloader, "start", held_start):
                worker = threading.Thread(target=acquire)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    candidate = candidates[0]
                    self.assertTrue(candidate.wait_for_real_pcm(4, timeout=2))
                    self._stop_service(server)._MainService__close()
                    self.assertTrue(candidate.aborted)
                    self.assertTrue(candidate._file_store._file.closed)
                    self.assertFalse(candidate.thread.is_alive())
                    self.assertIsNotNone(spotty.process.poll())
                finally:
                    release.set()
                    worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual([], errors)
            self.assertEqual([None], results)
            self.assertEqual({}, manager._instances)
            self.assertEqual(0, manager._file_bytes)

    def test_pcm_ordinary_cleanup_preserves_future_http_admission(self):
        import http.client

        spotty = _RetryPipeSpotty([], frames=180 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", f"/track/{TRACK}/180.wav", headers={"Range": "bytes=0-63"})
                connection.getresponse().read()
                original = self._downloader()
                self._stop_service(server)._teardown_for_external_playback()
                self.assertTrue(original.aborted)
                connection.request("GET", "/track/after-external/180.wav", headers={"Range": "bytes=0-63"})
                with connection.getresponse() as response:
                    self.assertEqual(206, response.status)
                    self.assertEqual(RAMP[:20], response.read()[44:])
                self.assertEqual(2, len(spotty.calls))
            finally:
                connection.close()

    def test_pcm_missing_or_pairing_identity_refuses_before_helper_launch(self):
        spotty = RampSpotty()
        spotty.get_spotty_credentials_identity = lambda: None
        server = self._server(spotty)
        self.http.bottle.request.headers = {"Range": "bytes=0-63"}
        result = server.spotty_stream_audio_track(TRACK, "30")
        self.assertEqual(503, self.http.bottle.response.status)
        self.assertEqual("", result)
        self.assertEqual([], spotty.calls, "unverified account started a competing helper")
        self.assertEqual({}, self.cache.SpottyCacheManager._instances)

    def test_pcm_declared_length_stops_overlong_helper_without_false_failure(self):
        import http.client

        spotty = _RetryPipeSpotty([], frames=30 * FRAMES_PER_SEC)
        with self._pipe_http(spotty=spotty) as (server, _, port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", f"/track/{TRACK}/1000ms")
                with connection.getresponse() as response:
                    served = response.read()
                    self.assertEqual(176444, len(served))
                downloader = self._downloader()
                downloader.thread.join(0.3)
                self.assertFalse(downloader.thread.is_alive(), "full declared PCM waited for excess helper output")
                self.assertFalse(downloader.error, "bounded file completion became a false helper failure")
                self.assertEqual(176444, downloader.written_bytes)
                self.assertEqual(downloader.wav_header + _ramp(FRAMES_PER_SEC), served)
                self._stop_service(server)._release_after_stop()
                connection.request("GET", f"/track/{TRACK}/1000ms")
                with connection.getresponse() as response:
                    self.assertEqual(served, response.read())
                self.assertIs(downloader, self._downloader())
                self.assertEqual(1, len(spotty.calls))
            finally:
                connection.close()

    def test_canonical_http_duration_matches_pcm_and_head(self):
        import http.client

        frames = 9_417_996
        spotty = _RetryPipeSpotty([], frames=frames)
        with self._pipe_http(spotty=spotty) as (streamer, _, port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                path = f"/track/{TRACK}/213560ms"
                connection.request("HEAD", path)
                with connection.getresponse() as response:
                    self.assertEqual(200, response.status)
                    head_length = int(response.getheader("Content-Length"))
                    self.assertEqual("audio/x-wav", response.getheader("Content-Type"))
                    self.assertEqual("bytes", response.getheader("Accept-Ranges"))
                    self.assertEqual(b"", response.read())
                self.assertEqual([], spotty.calls)
                connection.request("GET", path)
                with connection.getresponse() as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual(head_length, int(response.getheader("Content-Length")))
                    self.assertEqual("audio/x-wav", response.getheader("Content-Type"))
                    served = response.read()
                self.assertEqual(head_length, len(served))
                with wave.open(io.BytesIO(served), "rb") as wav:
                    self.assertEqual((2, 2, 44100, frames, "NONE", "not compressed"), wav.getparams())
                    pcm = wav.readframes(frames)
                    self.assertEqual(_ramp(frames), pcm)
                    self.assertEqual(213.560, wav.getnframes() / wav.getframerate())
                self.assertEqual(213.560, streamer._HTTPSpottyAudioStreamer__spotty_streamer.get_stream_spec().track_duration)
                print(f"canonical-http token=213560ms seconds=213.560 frames={frames} "
                      f"head_bytes={head_length} get_bytes={len(served)} "
                      f"pcm_sha256={hashlib.sha256(pcm).hexdigest()} exact_pcm=True")
            finally:
                connection.close()

    def test_http_duration_fractions_legacy_and_minimum(self):
        import http.client

        for token, frames in (
            ("1250ms", 55125),
            ("1.250.wav", 55125),
            ("1.250", 55125),
            ("250ms", 44100),
            ("0ms", 44100),
            ("0.wav", 44100),
        ):
            with self.subTest(token=token), self._pipe_http(spotty=_RetryPipeSpotty([])) as (_, spotty, port):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    path = f"/track/{TRACK}/{token}"
                    connection.request("HEAD", path)
                    with connection.getresponse() as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual(44 + frames * 4, int(response.getheader("Content-Length")))
                        response.read()
                    self.assertEqual([], spotty.calls)
                    connection.request("GET", path)
                    with connection.getresponse() as response:
                        self.assertEqual(200, response.status)
                        self.assertEqual(44 + frames * 4, int(response.getheader("Content-Length")))
                        served = response.read()
                    self.assertEqual(44 + frames * 4, len(served))
                    self.assertEqual(_ramp(frames), served[44:])
                finally:
                    connection.close()

    def test_http_rejects_invalid_milliseconds_before_stream_changes(self):
        import http.client

        spotty = _RetryPipeSpotty([])
        with self._pipe_http(spotty=spotty) as (streamer, _, port):
            for token in ("1.5ms", "-1ms", "+1000ms", "nanms", "1e3ms", "ms", "213560ms.wav", "1" + "0" * 400 + "ms"):
                for method in ("HEAD", "GET"):
                    with self.subTest(token=token, method=method):
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                        try:
                            connection.request(method, f"/track/{TRACK}/{token}")
                            with connection.getresponse() as response:
                                self.assertEqual(400, response.status)
                                response.read()
                        finally:
                            connection.close()
            self.assertEqual([], spotty.calls)
            self.assertFalse(streamer._HTTPSpottyAudioStreamer__is_streaming)

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

    @unittest.skipUnless(os.name == "posix", "producer status requires inherited descriptor")
    def test_real_pipe_stop_then_reuse_survives_old_seek_cleanup(self):
        import http.client

        # server.stop() models shutdown. User Stop calls release_stream(cleanup_all).
        with self._pipe_http() as (server, spotty, port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            waiting, resume = threading.Event(), threading.Event()
            results = []
            worker = None
            try:
                connection.request("GET", f"/track/{TRACK}/30.wav", headers={"Range": "bytes=0-47"})
                with connection.getresponse() as response:
                    self.assertEqual(self.ideal[:48], response.read())
                downloader = self._downloader()
                low = server._HTTPSpottyAudioStreamer__spotty_streamer
                original_wait = downloader.wait_for_real_pcm

                def wait_for_real_pcm(*args, **kwargs):
                    waiting.set()
                    resume.wait(2)

                downloader.wait_for_real_pcm = wait_for_real_pcm
                worker = threading.Thread(target=lambda: results.append(low.prepare_part_audio_stream(48)))
                worker.start()
                self.assertTrue(waiting.wait(1))
                server.stop()
                connection.request("GET", f"/track/{TRACK}/30.wav", headers={"Range": "bytes=44-47"})
                with connection.getresponse() as response:
                    self.assertEqual(self.ideal[44:48], response.read())
                self.assertIs(downloader, self._downloader())
                resume.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual([False], results)
                self.assertFalse(downloader.aborted)
                downloader.wait_for_real_pcm = original_wait
                spotty.release(bulk=True)
                connection.request("GET", f"/track/{TRACK}/30.wav", headers={"Range": "bytes=48-"})
                with connection.getresponse() as response:
                    self.assertEqual(self.ideal[48:], response.read())
                self.assertIs(downloader, self._downloader())
                print("stop-reuse old_prepare=False cache_preserved=True remainder_exact=True")
            finally:
                resume.set()
                if worker is not None:
                    worker.join(2)
                connection.close()

    def test_real_pipe_post_commit_failure_truncates_once_then_replay_recovers(self):
        import http.client

        spotty = _RetryPipeSpotty([4, 0, 0, 0, None], gate_first=True)
        notices, finished = [], []
        with self._pipe_http(spotty=spotty) as (server, _, port):
            sys.modules["xbmcgui"].Dialog = lambda: types.SimpleNamespace(
                notification=lambda *args, **kwargs: notices.append(args))
            sys.modules["xbmcaddon"].Addon = lambda **kwargs: types.SimpleNamespace(
                getSetting=lambda key: "", getAddonInfo=lambda key: key,
                getLocalizedString=lambda key: str(key))
            server.set_notify_track_finished(finished.append)
            low = server._HTTPSpottyAudioStreamer__spotty_streamer
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", f"/track/{TRACK}/30.wav")
                response = connection.getresponse()
                self.assertEqual(200, response.status)
                self.assertEqual(self.track_length, int(response.getheader("Content-Length")))
                self.assertEqual(self.ideal[:48], response.read(48))
                failed = self._downloader()
                old_generation = low._SpottyAudioStreamer__terminate_generation
                stale_body = low.send_part_audio_stream(4, 48)
                spotty.release()
                with self.assertRaises(http.client.IncompleteRead) as caught:
                    response.read()
                self.assertEqual(b"", caught.exception.partial)
                failed.thread.join(2)
                self.assertFalse(failed.thread.is_alive())
                self.assertTrue(failed.error)
                self.assertEqual(4, len(spotty.calls))
                self.assertEqual([], finished)
                self.assertEqual(1, len(notices))
                server._notify_playback_failure(TRACK)
                self.assertEqual(1, len(notices))
                connection.close()
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", f"/track/{TRACK}/30.wav")
                with connection.getresponse() as replay:
                    self.assertEqual(200, replay.status)
                    self.assertEqual(self.ideal, replay.read())
                fresh = self._downloader()
                self.assertIsNot(failed, fresh)
                fresh.thread.join(2)
                self.assertFalse(fresh.thread.is_alive())
                self.assertTrue(fresh.is_finished)
                self.assertFalse(fresh.error)
                self.assertGreater(low._SpottyAudioStreamer__terminate_generation, old_generation)
                self.assertEqual(b"", b"".join(stale_body))
                self.assertFalse(fresh.aborted)
                self.assertEqual([TRACK], finished)
                self.assertEqual(1, len(notices))
                self.assertEqual(5, len(spotty.calls))
                self.assertEqual([], spotty.seeks())
                print("post-commit status=200 declared=5292044 received=48 "
                      "client=IncompleteRead failures=1 false_finished=0 replay_exact=True")
            finally:
                spotty.release()
                connection.close()

    def test_real_pipe_partial_first_frame_retry_preserves_exact_bytes(self):
        import http.client

        spotty = _RetryPipeSpotty([2, 3, None])
        with self._pipe_http(spotty=spotty) as (_, _, port):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                connection.request("GET", f"/track/{TRACK}/30.wav")
                with connection.getresponse() as response:
                    self.assertEqual(200, response.status)
                    self.assertEqual(self.ideal, response.read())
                downloader = self._downloader()
                downloader.thread.join(2)
                self.assertFalse(downloader.thread.is_alive())
                self.assertTrue(downloader.is_finished)
                self.assertFalse(downloader.error)
                self.assertEqual(3, len(spotty.calls))
                self.assertEqual([], spotty.seeks())
                print("partial-first-frame cuts=2,3 attempts=3 exact_bytes=True")
            finally:
                connection.close()

    def test_clean_short_tail_range_preserves_finished_cache(self):
        spotty = RampSpotty()
        server = self._server(spotty)
        head = self._get(server, "bytes=0-", duration="30.5.wav")
        downloader = self._downloader()
        downloader.thread.join(2)
        self.assertFalse(downloader.thread.is_alive())
        self.assertFalse(downloader.error)
        padded_header, _ = sys.modules["spotty_audio_streamer"].create_wav_header_for_duration(30.5)
        # Keep an early reader active to distinguish normal head trim from cache loss.
        self.assertEqual(padded_header + RAMP[:4], next(head)[:48])
        before = bytes(downloader._buffer)
        begin = 44 + PCM_LEN + 100
        tail = self._get(server, f"bytes={begin}-{begin + 7}", duration="30.5.wav")
        self.assertEqual(206, int(str(self.http.bottle.response.status).split()[0]))
        self.assertEqual(8, self.http.bottle.response.content_length)
        self.assertEqual(bytes(8), self._read(tail))
        self.assertFalse(downloader.aborted)
        self.assertEqual(before, bytes(downloader._buffer))
        low = server._HTTPSpottyAudioStreamer__spotty_streamer
        self.assertTrue(low.prepare_part_audio_stream(begin))
        self.assertEqual(self.ideal[44:48], self._read(
            self._get(server, "bytes=44-47", duration="30.5.wav")))
        self.assertIs(downloader, self._downloader())
        self.assertEqual(1, len(spotty.calls))
        self.assertEqual([], spotty.seeks())

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
