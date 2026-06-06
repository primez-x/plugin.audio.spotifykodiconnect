import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")


class FakeAddon:
    def getLocalizedString(self, string_id):
        return f"str-{string_id}"

    def getAddonInfo(self, key):
        return "SpotifyKodiConnect"

    def getSetting(self, key):
        return ""

    def setSetting(self, key, value):
        pass


class FakeWindow:
    def __init__(self, window_id=None):
        self.properties = {}

    def getProperty(self, key):
        return self.properties.get(key, "")

    def setProperty(self, key, value):
        self.properties[key] = value

    def clearProperty(self, key):
        self.properties.pop(key, None)


class FakeMusicInfoTag:
    def __getattr__(self, name):
        if name.startswith("set"):
            return lambda *args, **kwargs: None
        raise AttributeError(name)


class FakeListItem:
    def __init__(self, label="", path=None, offscreen=False):
        self.label = label
        self.path = path
        self.context_items = []
        self.properties = {}

    def getMusicInfoTag(self):
        return FakeMusicInfoTag()

    def setProperty(self, key, value):
        self.properties[key] = value

    def setLabel2(self, label):
        self.label2 = label

    def setArt(self, art):
        self.art = art

    def setContentLookup(self, value):
        self.content_lookup = value

    def addContextMenuItems(self, items, replaceItems=False):
        self.context_items.extend(items or [])

    def setMimeType(self, mime_type):
        self.mime_type = mime_type


class FakeMonitor:
    def abortRequested(self):
        return False


class RecordingPlaylist:
    instances = []

    def __init__(self, playlist_id):
        self.playlist_id = playlist_id
        self.items = []
        RecordingPlaylist.instances.append(self)

    def clear(self):
        self.items.clear()

    def add(self, url, listitem):
        self.items.append((url, listitem))


class RecordingPlayer:
    events = []

    def play(self, playlist):
        RecordingPlayer.events.append("play")


class DeferredThread:
    started_targets = []

    def __init__(self, target, daemon=False):
        self.target = target
        self.daemon = daemon

    def start(self):
        if getattr(self.target, "__name__", "") in ("add_remaining", "_run"):
            DeferredThread.started_targets.append(self.target)
            return
        self.target()

    def join(self):
        pass


class FakeCache:
    def __init__(self):
        self.values = {}

    def get(self, key, checksum=None):
        item = self.values.get(key)
        if not item:
            return None
        value, item_checksum = item
        if checksum is not None and item_checksum != checksum:
            return None
        return value

    def set(self, key, value, checksum=None, **kwargs):
        self.values[key] = (value, checksum)


def install_kodi_stubs():
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmc.Monitor = FakeMonitor
    xbmc.PlayList = RecordingPlaylist
    xbmc.Player = RecordingPlayer
    xbmc.sleep = lambda millis: None
    xbmc.getLocalizedString = lambda string_id: f"kodi-{string_id}"
    xbmc.executebuiltin = lambda command: None
    xbmc.log = lambda message, level=0: None
    sys.modules["xbmc"] = xbmc

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda id=None: FakeAddon()
    sys.modules["xbmcaddon"] = xbmcaddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = FakeWindow
    xbmcgui.ListItem = FakeListItem
    xbmcgui.Dialog = lambda: None
    sys.modules["xbmcgui"] = xbmcgui

    xbmcplugin = types.ModuleType("xbmcplugin")
    xbmcplugin.SORT_METHOD_UNSORTED = 0
    xbmcplugin.addDirectoryItems = lambda *args, **kwargs: None
    xbmcplugin.addDirectoryItem = lambda *args, **kwargs: None
    xbmcplugin.addSortMethod = lambda *args, **kwargs: None
    xbmcplugin.endOfDirectory = lambda *args, **kwargs: None
    xbmcplugin.setContent = lambda *args, **kwargs: None
    xbmcplugin.setPluginCategory = lambda *args, **kwargs: None
    xbmcplugin.setProperty = lambda *args, **kwargs: None
    sys.modules["xbmcplugin"] = xbmcplugin

    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = lambda path: path
    sys.modules["xbmcvfs"] = xbmcvfs

    simplecache = types.ModuleType("simplecache")
    simplecache.SimpleCache = lambda addon_id=None: FakeCache()
    sys.modules["simplecache"] = simplecache

    spotipy = types.ModuleType("spotipy")
    spotipy.Spotify = lambda auth=None: None
    sys.modules["spotipy"] = spotipy

    spotty = types.ModuleType("spotty")
    spotty.Spotty = object
    spotty.get_spotty = lambda helper: None
    sys.modules["spotty"] = spotty

    spotty_auth = types.ModuleType("spotty_auth")
    spotty_auth.SpottyAuth = object
    sys.modules["spotty_auth"] = spotty_auth

    spotty_helper = types.ModuleType("spotty_helper")
    spotty_helper.SpottyHelper = object
    sys.modules["spotty_helper"] = spotty_helper


def import_plugin_content():
    install_kodi_stubs()
    if LIB_DIR not in sys.path:
        sys.path.insert(0, LIB_DIR)
    sys.argv = ["plugin://plugin.audio.spotifykodiconnect", "1", "?"]
    sys.modules.pop("plugin_content", None)
    import plugin_content

    plugin_content.threading.Thread = DeferredThread
    return plugin_content


def spotify_track(index):
    return {
        "id": f"track-{index}",
        "uri": f"spotify:track:track-{index}",
        "name": f"Track {index}",
        "duration_ms": 180000,
        "popularity": 50,
        "artists": [{"id": f"artist-{index}", "name": f"Artist {index}"}],
        "album": {
            "name": "Album",
            "images": [],
            "album_type": "album",
            "release_date": "2024-01-01",
            "genres": [],
        },
    }


class FakeSpotify:
    def __init__(self, events, total=75):
        self.events = events
        self.total = total
        self.saved_track_calls = 0
        self.saved_album_calls = 0
        self.followed_artist_calls = 0
        self.artist_calls = 0
        self.saved_track_contains_requests = []
        self.saved_album_contains_requests = []
        self.following_artist_requests = []
        self.playlist_follow_requests = []
        self.track_detail_requests = []
        self.playlist_detail_requests = []

    def playlist(self, playlist_id, fields="", market=None):
        self.playlist_detail_requests.append((playlist_id, fields, market))
        return {
            "id": playlist_id,
            "name": "Fast Playlist",
            "owner": {"id": "owner"},
            "snapshot_id": "snapshot-1",
            "tracks": {"total": self.total},
        }

    def playlist_items(self, playlist_id, market=None, fields="", limit=50, offset=0):
        self.events.append(f"fetch:{offset}")
        end = min(offset + limit, self.total)
        return {"items": [{"track": spotify_track(i)} for i in range(offset, end)]}

    def tracks(self, track_ids, market=None):
        self.track_detail_requests.append((tuple(track_ids), market))
        tracks = []
        for track_id in track_ids:
            index = int(str(track_id).split("-")[-1])
            tracks.append(spotify_track(index))
        return {"tracks": tracks}

    def current_user_saved_tracks(self, limit=50, offset=0, market=None):
        self.saved_track_calls += 1
        return {"total": 0, "items": []}

    def current_user_saved_albums(self, limit=50, offset=0):
        self.saved_album_calls += 1
        return {"total": 0, "items": []}

    def current_user_followed_artists(self, limit=50, after=None):
        self.followed_artist_calls += 1
        return {"artists": {"total": 0, "items": [], "cursors": {"after": None}}}

    def artists(self, artist_ids):
        self.artist_calls += 1
        return {"artists": [{"id": artist_id, "images": []} for artist_id in artist_ids]}

    def current_user_saved_tracks_contains(self, track_ids):
        self.saved_track_contains_requests.append(tuple(track_ids))
        return [False for _ in track_ids]

    def current_user_saved_albums_contains(self, album_ids):
        self.saved_album_contains_requests.append(tuple(album_ids))
        return [False for _ in album_ids]

    def current_user_following_artists(self, artist_ids):
        self.following_artist_requests.append(tuple(artist_ids))
        return [False for _ in artist_ids]

    def playlist_is_following(self, playlist_id, user_ids):
        self.playlist_follow_requests.append((playlist_id, tuple(user_ids)))
        return [False]


class FanartSpotify(FakeSpotify):
    def artists(self, artist_ids):
        self.artist_calls += 1
        return {
            "artists": [
                {
                    "id": artist_id,
                    "images": [{"url": f"https://images.example/{artist_id}.jpg"}],
                }
                for artist_id in artist_ids
            ]
        }


class FailingFanartSpotify(FakeSpotify):
    def artists(self, artist_ids):
        self.artist_calls += 1
        raise RuntimeError("artist api failed")


def spotify_artist(index):
    return {
        "id": f"artist-{index}",
        "name": f"Artist {index}",
        "images": [],
        "genres": [],
        "popularity": 0,
        "followers": {"total": 0},
    }


def spotify_album(index):
    return {
        "id": f"album-{index}",
        "name": f"Album {index}",
        "images": [],
        "artists": [{"id": f"artist-{index}", "name": f"Artist {index}"}],
        "genres": [],
        "popularity": 50,
        "release_date": "2024-01-01",
    }


def spotify_playlist(index, owner_id="other-user"):
    return {
        "id": f"playlist-{index}",
        "name": f"Playlist {index}",
        "images": [],
        "owner": {"id": owner_id},
    }


def spotify_daylist():
    return {
        "id": "37i9dQZF1EP6YuccBxUcC1",
        "name": "daylist",
        "images": [],
        "owner": {"id": "spotify"},
    }


class DynamicDaylistSpotify(FakeSpotify):
    def playlist(self, playlist_id, fields="", market=None):
        self.playlist_detail_requests.append((playlist_id, fields, market))
        return {
            "id": playlist_id,
            "name": "daylist - synthpop saturday morning",
            "owner": {"id": "spotify"},
            "snapshot_id": "snapshot-daylist",
            "tracks": {"total": 50},
        }


class ChecksumSpotify(FakeSpotify):
    def __init__(self, events, saved_track_total=3, saved_album_total=5, followed_total=7):
        super().__init__(events, total=1)
        self.saved_track_total = saved_track_total
        self.saved_album_total = saved_album_total
        self.followed_total = followed_total
        self.saved_track_requests = []
        self.saved_album_requests = []
        self.followed_artist_requests = []

    def current_user_saved_tracks(self, limit=50, offset=0, market=None):
        self.saved_track_calls += 1
        self.saved_track_requests.append((limit, offset, market))
        return {
            "total": self.saved_track_total,
            "items": [
                {"track": spotify_track(i)}
                for i in range(offset, min(offset + limit, self.saved_track_total))
            ],
        }

    def current_user_saved_albums(self, limit=50, offset=0):
        self.saved_album_calls += 1
        self.saved_album_requests.append((limit, offset))
        return {
            "total": self.saved_album_total,
            "items": [
                {"album": {"id": f"album-{i}"}}
                for i in range(offset, min(offset + limit, self.saved_album_total))
            ],
        }

    def current_user_followed_artists(self, limit=50, after=None):
        self.followed_artist_calls += 1
        self.followed_artist_requests.append((limit, after))
        start = int(after or 0)
        end = min(start + limit, self.followed_total)
        next_after = str(end) if end < self.followed_total else None
        return {
            "artists": {
                "total": self.followed_total,
                "items": [spotify_artist(i) for i in range(start, end)],
                "cursors": {"after": next_after},
            }
        }


class SavedTracksSpotify(FakeSpotify):
    def __init__(self, events, saved_track_total=75):
        super().__init__(events, total=1)
        self.saved_track_total = saved_track_total
        self.saved_track_requests = []

    def current_user_saved_tracks(self, limit=50, offset=0, market=None):
        self.saved_track_calls += 1
        self.saved_track_requests.append((limit, offset, market))
        return {
            "total": self.saved_track_total,
            "items": [
                {"track": spotify_track(i)}
                for i in range(offset, min(offset + limit, self.saved_track_total))
            ],
        }


class PlaylistFastPathTests(unittest.TestCase):
    def setUp(self):
        self.plugin_content = import_plugin_content()
        RecordingPlaylist.instances.clear()
        RecordingPlayer.events.clear()
        DeferredThread.started_targets.clear()

    def build_content(self, spotify):
        content = object.__new__(self.plugin_content.PluginContent)
        content.cache = FakeCache()
        content._PluginContent__spotipy = spotify
        content._PluginContent__user_country = "US"
        content._PluginContent__userid = "user"
        content._PluginContent__playlist_id = "playlist-1"
        content._PluginContent__addon = FakeAddon()
        content._PluginContent__addon_handle = 1
        content._PluginContent__base_url = "plugin://plugin.audio.spotifykodiconnect"
        content._PluginContent__params = {}
        content._PluginContent__cached_checksum = ""
        content._PluginContent__addon_icon_path = "icons"
        return content

    def test_play_playlist_starts_after_first_page_before_fetching_remaining_pages(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=75)
        content = self.build_content(spotify)

        content.play_playlist()

        self.assertEqual(["fetch:0", "play"], events[:2])
        self.assertEqual(1, len(DeferredThread.started_targets))
        self.assertEqual(50, len(RecordingPlaylist.instances[0].items))

    def test_play_playlist_skips_browse_only_metadata_calls(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=1)
        content = self.build_content(spotify)

        content.play_playlist()

        self.assertEqual(0, spotify.saved_track_calls)
        self.assertEqual(0, spotify.saved_album_calls)
        self.assertEqual(0, spotify.followed_artist_calls)
        self.assertEqual(0, spotify.artist_calls)

    def test_browse_playlist_uses_first_page_and_hidden_continuation(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=75)
        content = self.build_content(spotify)
        content._PluginContent__params = {
            "action": ["browse_playlist"],
            "playlistid": ["playlist-1"],
        }

        content.browse_playlist()

        self.assertEqual(["fetch:0"], events)
        self.assertEqual(1, len(DeferredThread.started_targets))

    def test_browse_saved_tracks_uses_first_page_and_hidden_continuation(self):
        events = RecordingPlayer.events
        spotify = SavedTracksSpotify(events, saved_track_total=75)
        content = self.build_content(spotify)
        content._PluginContent__params = {"action": ["browse_saved_tracks"]}
        content._PluginContent__action = "browse_saved_tracks"

        content.browse_saved_tracks()

        self.assertEqual([(50, 0, "US")], spotify.saved_track_requests)
        self.assertEqual([], spotify.track_detail_requests)
        self.assertEqual([], spotify.saved_track_contains_requests)
        self.assertEqual(1, len(DeferredThread.started_targets))
        cached = content.cache.values["spotify.savedtracks.user"][0]
        self.assertEqual(50, len(cached["items"]))
        self.assertFalse(cached["_dynamic_paging_complete"])

    def test_saved_tracks_continuation_hydrates_remaining_pages(self):
        events = RecordingPlayer.events
        spotify = SavedTracksSpotify(events, saved_track_total=75)
        content = self.build_content(spotify)
        content._PluginContent__params = {"action": ["browse_saved_tracks"]}
        content._PluginContent__action = "browse_saved_tracks"

        content.browse_saved_tracks()
        DeferredThread.started_targets[0]()

        self.assertEqual([(50, 0, "US"), (50, 50, "US")], spotify.saved_track_requests)
        cached = content.cache.values["spotify.savedtracks.user"][0]
        self.assertEqual(75, len(cached["items"]))
        self.assertTrue(cached["_dynamic_paging_complete"])

    def test_prepare_tracks_uses_page_local_relation_checks(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=1)
        content = self.build_content(spotify)

        content._PluginContent__prepare_track_listitems(
            tracks=[spotify_track(1), spotify_track(2)],
            include_artist_fanart=False,
        )

        self.assertEqual(0, spotify.saved_track_calls)
        self.assertEqual(0, spotify.followed_artist_calls)
        self.assertEqual([("track-1", "track-2")], spotify.saved_track_contains_requests)
        self.assertEqual([("artist-1", "artist-2")], spotify.following_artist_requests)

    def test_prepare_albums_uses_page_local_saved_checks(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=1)
        content = self.build_content(spotify)

        content._PluginContent__prepare_album_listitems(albums=[spotify_album(1), spotify_album(2)])

        self.assertEqual(0, spotify.saved_album_calls)
        self.assertEqual([("album-1", "album-2")], spotify.saved_album_contains_requests)

    def test_prepare_playlists_uses_page_local_follow_checks(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=1)
        content = self.build_content(spotify)

        content._PluginContent__prepare_playlist_listitems(
            [spotify_playlist(1), spotify_playlist(2, owner_id="user")]
        )

        self.assertEqual([("playlist-1", ("user",))], spotify.playlist_follow_requests)

    def test_prepare_daylist_uses_dynamic_title_and_daylist_subtitle(self):
        events = RecordingPlayer.events
        spotify = DynamicDaylistSpotify(events, total=1)
        content = self.build_content(spotify)

        playlists = content._PluginContent__prepare_playlist_listitems([spotify_daylist()])

        self.assertEqual("synthpop saturday morning", playlists[0]["name"])
        self.assertEqual("daylist", playlists[0]["label2"])
        self.assertEqual(
            [("37i9dQZF1EP6YuccBxUcC1", "tracks(total),name,owner(id),id,snapshot_id", "US")],
            spotify.playlist_detail_requests,
        )

    def test_add_playlists_refreshes_cached_daylist_metadata_before_rendering(self):
        events = RecordingPlayer.events
        spotify = DynamicDaylistSpotify(events, total=1)
        content = self.build_content(spotify)
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )
        cached_daylist = spotify_daylist()
        cached_daylist["name"] = "yesterday afternoon"
        cached_daylist["description"] = "Your day in a playlist."
        cached_daylist["url"] = "plugin://plugin.audio.spotifykodiconnect/?action=browse_playlist"

        content._PluginContent__add_playlist_listitems([cached_daylist])

        self.assertEqual(1, len(rendered))
        self.assertEqual("synthpop saturday morning", rendered[0][1].label)
        self.assertEqual("daylist", rendered[0][1].label2)

    def test_artist_fanart_fetch_logs_exception_with_exception_first(self):
        events = RecordingPlayer.events
        spotify = FailingFanartSpotify(events, total=1)
        content = self.build_content(spotify)
        logged = []

        self.plugin_content.log_exception = lambda exc, details: logged.append((exc, details))

        result = content._PluginContent__get_artist_fanart_map(["artist-1"])

        self.assertEqual({}, result)
        self.assertEqual(1, len(logged))
        self.assertIsInstance(logged[0][0], RuntimeError)
        self.assertEqual("artist fanart fetch", logged[0][1])

    def test_artist_fanart_cache_hit_avoids_duplicate_artist_lookup(self):
        events = RecordingPlayer.events
        spotify = FanartSpotify(events, total=1)
        content = self.build_content(spotify)
        track = spotify_track(1)

        first = content._PluginContent__prepare_track_listitems(
            tracks=[track],
            include_context_items=False,
        )
        second = content._PluginContent__prepare_track_listitems(
            tracks=[spotify_track(1)],
            include_context_items=False,
        )

        self.assertEqual("https://images.example/artist-1.jpg", first[0]["artist_fanart"])
        self.assertEqual("https://images.example/artist-1.jpg", second[0]["artist_fanart"])
        self.assertEqual(1, spotify.artist_calls)

    def test_artist_fanart_cache_evicts_least_recently_used_entries(self):
        events = RecordingPlayer.events
        spotify = FanartSpotify(events, total=1)
        content = self.build_content(spotify)
        content._artist_fanart_cache = {
            f"artist-{i}": f"https://images.example/artist-{i}.jpg" for i in range(500)
        }

        content._PluginContent__prepare_track_listitems(
            tracks=[spotify_track(0)],
            include_context_items=False,
        )
        content._PluginContent__prepare_track_listitems(
            tracks=[spotify_track(500)],
            include_context_items=False,
        )

        self.assertLessEqual(len(content._artist_fanart_cache), 500)
        self.assertIn("artist-0", content._artist_fanart_cache)
        self.assertIn("artist-500", content._artist_fanart_cache)
        self.assertNotIn("artist-1", content._artist_fanart_cache)

    def test_cache_checksum_uses_lightweight_total_requests(self):
        events = RecordingPlayer.events
        spotify = ChecksumSpotify(events)
        content = self.build_content(spotify)

        checksum = content._PluginContent__cache_checksum()

        self.assertEqual("v3-3-5-7-", checksum)
        self.assertEqual([(1, 0, "US")], spotify.saved_track_requests)
        self.assertEqual([(1, 0)], spotify.saved_album_requests)
        self.assertEqual([(1, None)], spotify.followed_artist_requests)

    def test_cache_checksum_reuses_base_for_optional_values(self):
        events = RecordingPlayer.events
        spotify = ChecksumSpotify(events)
        content = self.build_content(spotify)

        first = content._PluginContent__cache_checksum("playlist-snapshot")
        second = content._PluginContent__cache_checksum("artist-albums")

        self.assertEqual("v3-3-5-7--playlist-snapshot", first)
        self.assertEqual("v3-3-5-7--artist-albums", second)
        self.assertEqual(1, spotify.saved_track_calls)
        self.assertEqual(1, spotify.saved_album_calls)
        self.assertEqual(1, spotify.followed_artist_calls)


if __name__ == "__main__":
    unittest.main()
