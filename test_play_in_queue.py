import json
import os
import sys
import types
import unittest
from contextlib import contextmanager
from urllib.parse import parse_qs, urlsplit

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")
LIST = "action=browse_saved_tracks"


def song(track_id):
    return f"http://127.0.0.1:52309/track/{track_id}/1000ms.xwav"


class FakeItem:
    def __init__(self, path):
        self.path = path

    def getPath(self):
        return self.path


class FakeKodi:
    """Music queue, home window, player, and JSON-RPC calls that play_in_queue uses."""

    def __init__(self, queue=(), listing=(), focused=True, session="", starts=True):
        self.queue = [FakeItem(path) for path in queue]
        self.listing = list(listing)
        self.properties = {"Spotify.PlayQueue.SessionId": session}
        self.focused = focused
        self.starts = starts
        self.calls = []

    def rows(self, directory):
        params = {k: v[0] for k, v in parse_qs(urlsplit(directory).query).items()}
        last = int(params["last"]) if "last" in params else None
        return [FakeItem(path) for path in self.listing[int(params["first"]) : last]]

    def rpc(self, request):
        request = json.loads(request)
        method, params = request["method"], request["params"]
        self.calls.append((method, params))
        if method == "Playlist.Insert":
            self.queue[params["position"] : params["position"]] = self.rows(params["item"]["directory"])
        elif method == "Playlist.Add":
            self.queue += self.rows(params["item"]["directory"])
        return json.dumps({"result": "OK"})


class PlayInQueueTests(unittest.TestCase):
    def install(self, kodi):
        xbmc = types.ModuleType("xbmc")
        xbmc.PLAYLIST_MUSIC = 0
        xbmc.LOGWARNING = 2
        xbmc.log = lambda *args: None
        xbmc.executeJSONRPC = kodi.rpc
        row = "plugin://plugin.audio.spotifykodiconnect/?action=play_in_queue&position=2&track=c&ms=1000&list=" + LIST
        labels = {"ListItem.FileNameAndPath": row if kodi.focused else "other", "ListItem.Title": "Song C"}
        xbmc.getInfoLabel = lambda name: labels.get(name, "")

        class PlayList:
            def __init__(self, playlist_id):
                pass

            def size(self):
                return len(kodi.queue)

            def __getitem__(self, index):
                return kodi.queue[index]

            def clear(self):
                kodi.calls.append(("clear", None))
                kodi.queue = []

            def add(self, url, listitem):
                kodi.calls.append(("add", listitem.tags.get("title")))
                kodi.queue.append(FakeItem(url))

        class Player:
            def play(self, item, windowed, startpos):
                kodi.calls.append(("play", {"windowed": windowed, "startpos": startpos}))

            def isPlayingAudio(self):
                return kodi.starts

            def getTime(self):
                return 0.5 if kodi.starts else 0

        class Monitor:
            def waitForAbort(self, seconds):
                return False

        xbmc.PlayList, xbmc.Player, xbmc.Monitor = PlayList, Player, Monitor
        xbmcgui = types.ModuleType("xbmcgui")

        class Window:
            def __init__(self, window_id):
                pass

            def getProperty(self, key):
                return kodi.properties.get(key, "")

            def setProperty(self, key, value):
                kodi.properties[key] = value

        class ListItem:
            def __init__(self, label, path, offscreen):
                self.tags = {}

            def getMusicInfoTag(self):
                tags = self.tags

                class Tag:
                    def __getattr__(self, name):
                        return lambda value: tags.__setitem__(name[3:].lower(), value)

                return Tag()

            def setArt(self, art):
                pass

        xbmcgui.Window, xbmcgui.ListItem = Window, ListItem
        play_queue = types.ModuleType("play_queue")

        def start_session(total):
            kodi.properties["Spotify.PlayQueue.SessionId"] = "owner"
            kodi.properties.pop("Spotify.PlayQueue.Listing", None)
            return "owner"

        @contextmanager
        def session_guard(owner=None):
            current = kodi.properties.get("Spotify.PlayQueue.SessionId")
            yield Window(10000) if owner is None or current == owner else None

        play_queue.start_session = start_session
        play_queue.session_guard = session_guard
        play_queue.build_stream_url = lambda track_id, ms: f"http://127.0.0.1:52309/track/{track_id}/{ms}ms.xwav"
        play_queue.bind_stream_item = lambda li, track_id, owner: None
        play_queue.mark_original_complete = lambda owner: kodi.calls.append(("complete", owner))
        for name, module in (("xbmc", xbmc), ("xbmcgui", xbmcgui), ("play_queue", play_queue)):
            sys.modules[name] = module
            self.addCleanup(sys.modules.pop, name, None)
        if LIB_DIR not in sys.path:
            sys.path.insert(0, LIB_DIR)
        sys.modules.pop("play_in_queue", None)
        import play_in_queue

        self.addCleanup(sys.modules.pop, "play_in_queue", None)
        return play_in_queue

    def click(self, kodi, position, track_id):
        self.install(kodi).run(f"?action=play_in_queue&position={position}&track={track_id}&ms=1000&list={LIST}")

    def test_click_in_the_list_queue_plays_without_a_rebuild(self):
        kodi = FakeKodi([song("a"), song("b"), song("c")])
        kodi.properties["Spotify.PlayQueue.Listing"] = LIST
        self.click(kodi, 1, "b")
        self.assertEqual([("play", {"windowed": True, "startpos": 1})], kodi.calls)

    def test_moved_song_plays_at_the_queue_index(self):
        # Shuffle and queue edits move a song away from the listing position.
        kodi = FakeKodi([song("c"), song("a"), song("b")])
        kodi.properties["Spotify.PlayQueue.Listing"] = LIST
        self.click(kodi, 1, "b")
        self.assertEqual([("play", {"windowed": True, "startpos": 2})], kodi.calls)

    def test_click_in_another_list_plays_first_then_loads_the_list_around_the_song(self):
        listing = [song(t) for t in "abcde"]
        kodi = FakeKodi([song("x")], listing=listing, session="old")
        kodi.properties["Spotify.PlayQueue.Listing"] = "action=browse_playlist"
        self.click(kodi, 2, "c")
        self.assertEqual(
            ["clear", "add", "play", "Playlist.Insert", "Playlist.Add", "complete"],
            [call[0] for call in kodi.calls],
        )
        self.assertEqual("Song C", kodi.calls[1][1], "the clicked song keeps the row tags")
        self.assertEqual({"windowed": True, "startpos": 0}, kodi.calls[2][1])
        self.assertEqual(listing, [item.getPath() for item in kodi.queue])
        self.assertEqual(LIST, kodi.properties["Spotify.PlayQueue.Listing"])

    def test_first_song_loads_only_the_songs_after_the_click(self):
        listing = [song(t) for t in "ab"]
        kodi = FakeKodi(listing=listing)
        self.click(kodi, 0, "a")
        self.assertNotIn("Playlist.Insert", [call[0] for call in kodi.calls])
        self.assertEqual(listing, [item.getPath() for item in kodi.queue])

    def test_unfocused_click_plays_without_row_tags(self):
        kodi = FakeKodi(listing=[song("c")], focused=False)
        self.click(kodi, 0, "c")
        self.assertEqual(("add", ""), kodi.calls[1])

    def test_newer_click_stops_the_list_load(self):
        kodi = FakeKodi(listing=[song(t) for t in "abc"])
        module = self.install(kodi)
        player_play = module.xbmc.Player.play

        def play_then_supersede(player, item, windowed, startpos):
            player_play(player, item, windowed, startpos)
            kodi.properties["Spotify.PlayQueue.SessionId"] = "newer"

        module.xbmc.Player.play = play_then_supersede
        module.run(f"?action=play_in_queue&position=1&track=b&ms=1000&list={LIST}")
        self.assertNotIn("Playlist.Add", [call[0] for call in kodi.calls])
        self.assertNotIn("Spotify.PlayQueue.Listing", kodi.properties)

    def test_song_that_never_starts_ends_the_load(self):
        kodi = FakeKodi(listing=[song(t) for t in "abc"], starts=False)
        self.click(kodi, 1, "b")
        self.assertEqual(["clear", "add", "play", "complete"], [call[0] for call in kodi.calls])

    def test_invalid_click_changes_nothing(self):
        kodi = FakeKodi([song("a")])
        module = self.install(kodi)
        for query in (
            f"?action=play_in_queue&track=a&ms=1&list={LIST}",
            f"?action=play_in_queue&position=z&track=a&ms=1&list={LIST}",
            f"?action=play_in_queue&position=0&track=a&list={LIST}",
            "?action=play_in_queue&position=0&track=a&ms=1",
        ):
            module.run(query)
        self.assertEqual([], kodi.calls)


if __name__ == "__main__":
    unittest.main()
