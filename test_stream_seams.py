"""End-to-end seam checks: real HTTP/streamer/cache code against a fake Spotty.

The fake emits a PCM ramp where every 4-byte frame encodes its own index, and
honours --start-position the way the real binary does (measured on CoreELEC,
spotty 1.3.1 / librespot 0.4.2): it lands a few tens of milliseconds *before*
the requested second.  Every served byte must equal the ideal WAV, so any
repeated or missing frame at a seam fails the comparison.
"""

import array
import os
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
    like_actions.set_current_track_liked = lambda track_id, liked, request: True
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
