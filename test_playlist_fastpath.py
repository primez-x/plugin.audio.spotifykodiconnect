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
    """Singleton-per-window-id, matching real Kodi semantics.

    Real Kodi returns the same Window object for the same id, so plugin
    writes (PluginContent.__win) and play_queue reads (_window()) land on
    the same property store. Per-call instances would silently drop state.
    """

    windows = {}

    def __new__(cls, window_id=None):
        if window_id not in cls.windows:
            instance = super().__new__(cls)
            instance.properties = {}
            cls.windows[window_id] = instance
        return cls.windows[window_id]

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
    xbmc.getInfoLabel = lambda label: ""
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
        self.category_requests = []
        self.category_playlist_requests = []
        self.featured_playlist_requests = []
        self.user_playlist_requests = []
        self.new_release_requests = []
        self.album_detail_requests = []

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

    def albums(self, album_ids, market=None):
        self.album_detail_requests.append((tuple(album_ids), market))
        return {"albums": [spotify_album(str(album_id).split("-")[-1]) for album_id in album_ids]}


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


class GenericDaylistSpotify(FakeSpotify):
    def playlist(self, playlist_id, fields="", market=None):
        self.playlist_detail_requests.append((playlist_id, fields, market))
        return {
            "id": playlist_id,
            "name": "daylist",
            "description": "Your day in a playlist.",
            "images": [
                {
                    "url": "https://daylist.spotifycdn.com/playlist-covers-mix/en/afternoon_default.jpg"
                }
            ],
            "owner": {"id": "spotify"},
            "snapshot_id": "snapshot-daylist",
            "tracks": {"total": 50},
        }


class EventuallyDynamicDaylistSpotify(FakeSpotify):
    def __init__(self, events, total=1):
        super().__init__(events, total=total)
        self.playlist_names = ["daylist", "daylist - jazz rap funky hip hop sunday afternoon"]

    def playlist(self, playlist_id, fields="", market=None):
        self.playlist_detail_requests.append((playlist_id, fields, market))
        name = (
            self.playlist_names.pop(0)
            if self.playlist_names
            else "daylist - jazz rap funky hip hop sunday afternoon"
        )
        return {
            "id": playlist_id,
            "name": name,
            "description": "Your day in a playlist.",
            "images": [
                {
                    "url": "https://daylist.spotifycdn.com/playlist-covers-mix/en/afternoon_default.jpg"
                }
            ],
            "owner": {"id": "spotify"},
            "snapshot_id": "snapshot-daylist",
            "tracks": {"total": 50},
        }


class CategoryPlaylistsSpotify(DynamicDaylistSpotify):
    def category(self, category_id, country=None, locale=None):
        self.category_requests.append((category_id, country, locale))
        return {"id": category_id, "name": "Made For You"}

    def category_playlists(self, category_id, country=None, limit=50, offset=0):
        self.category_playlist_requests.append((category_id, country, limit, offset))
        return {
            "playlists": {
                "total": 2,
                "items": [spotify_daylist(), spotify_playlist(2)],
            }
        }


class LegacyMadeForYouCategorySpotify(CategoryPlaylistsSpotify):
    current_made_for_you_id = "0JQ5DAt0tbjZptfcdMSKl3"

    def categories(self, country=None, locale=None, limit=50, offset=0):
        return {
            "categories": {
                "total": 1,
                "items": [
                    {
                        "id": self.current_made_for_you_id,
                        "name": "Made For You",
                        "icons": [],
                    }
                ],
            }
        }

    def category(self, category_id, country=None, locale=None):
        if category_id == "made-for-you":
            raise RuntimeError("legacy category slug unavailable")
        return super().category(category_id, country=country, locale=locale)


class FeaturedPlaylistsSpotify(FakeSpotify):
    def featured_playlists(self, country=None, limit=50, offset=0):
        self.featured_playlist_requests.append((country, limit, offset))
        return {
            "message": "Featured playlists",
            "playlists": {"total": 1, "items": [spotify_playlist(1)]},
        }


class FailingFeaturedPlaylistsSpotify(FakeSpotify):
    def featured_playlists(self, country=None, limit=50, offset=0):
        raise RuntimeError("featured playlists unavailable")


class FailingCategoryPlaylistsSpotify(DynamicDaylistSpotify):
    def category(self, category_id, country=None, locale=None):
        raise RuntimeError("category unavailable")

    def category_playlists(self, category_id, country=None, limit=50, offset=0):
        raise RuntimeError("category playlists unavailable")


class UserPlaylistsSpotify(FakeSpotify):
    def user_playlists(self, userid, limit=50, offset=0):
        self.user_playlist_requests.append((userid, limit, offset))
        return {"total": 1, "items": [spotify_playlist(1, owner_id=userid)]}


class NewReleasesSpotify(FakeSpotify):
    def new_releases(self, country=None, limit=50, offset=0):
        self.new_release_requests.append((country, limit, offset))
        return {"albums": {"total": 1, "items": [spotify_album(1)]}}


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
        FakeWindow.windows.clear()

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

    def set_active_listing(self, content):
        target_url = content._PluginContent__current_request_url()
        self.plugin_content.xbmc.getInfoLabel = lambda label, target_url=target_url: target_url
        return target_url

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

    # --- play-queue session coordination --------------------------------------

    def _play_queue_window(self):
        """Grab the singleton FakeWindow play_queue writes to."""
        return self.plugin_content.xbmcgui.Window(self.plugin_content.utils.ADDON_WINDOW_ID)

    def test_play_playlist_opens_loading_session_for_multi_page_playlist(self):
        """Multi-page playlist must start a play-queue session in "loading"
        state so the service suppresses autoplay until paging completes."""
        spotify = FakeSpotify(RecordingPlayer.events, total=75)
        content = self.build_content(spotify)

        content.play_playlist()

        win = self._play_queue_window()
        self.assertTrue(win.getProperty("Spotify.PlayQueue.SessionId"))
        self.assertEqual("75", win.getProperty("Spotify.PlayQueue.OriginalTotal"))
        self.assertEqual("50", win.getProperty("Spotify.PlayQueue.OriginalLoaded"))
        self.assertEqual("false", win.getProperty("Spotify.PlayQueue.OriginalComplete"))
        self.assertEqual("false", win.getProperty("Spotify.PlayQueue.AutoplayFetched"))

    def test_play_playlist_marks_complete_immediately_for_single_page_playlist(self):
        """Single-page playlist fits in one fetch — no paging thread needed,
        original_complete must be true right away so the service can fire
        autoplay when this playlist genuinely runs out."""
        spotify = FakeSpotify(RecordingPlayer.events, total=1)
        content = self.build_content(spotify)

        content.play_playlist()

        win = self._play_queue_window()
        self.assertEqual("true", win.getProperty("Spotify.PlayQueue.OriginalComplete"))
        # No paging thread should have been scheduled.
        self.assertEqual(0, len(DeferredThread.started_targets))

    def test_play_playlist_paging_thread_marks_complete_when_done(self):
        """The deferred add_remaining thread must mark the original as
        complete when it finishes paging, so the service can proceed."""
        spotify = FakeSpotify(RecordingPlayer.events, total=75)
        content = self.build_content(spotify)

        content.play_playlist()
        # Simulate the background thread actually running to completion.
        DeferredThread.started_targets[0]()

        win = self._play_queue_window()
        self.assertEqual("true", win.getProperty("Spotify.PlayQueue.OriginalComplete"))
        self.assertEqual("75", win.getProperty("Spotify.PlayQueue.OriginalLoaded"))

    def test_play_playlist_paging_thread_marks_complete_even_on_empty_page(self):
        """If paging exits early (e.g. Spotify returns an empty page mid-way),
        the finally block must still mark original_complete. Without this,
        a transient API hiccup would leave the session stuck in 'loading'
        and autoplay would never fire."""
        spotify = FakeSpotify(RecordingPlayer.events, total=75)
        spotify._empty_after_offset = 50  # type: ignore[attr-defined]

        def maybe_empty(playlist_id, market=None, fields="", limit=50, offset=0):
            if offset >= 50:
                return {"items": []}
            return spotify.__class__.playlist_items(
                spotify, playlist_id, market=market, fields=fields, limit=limit, offset=offset
            )

        spotify.playlist_items = maybe_empty
        content = self.build_content(spotify)

        content.play_playlist()
        DeferredThread.started_targets[0]()

        win = self._play_queue_window()
        self.assertEqual(
            "true",
            win.getProperty("Spotify.PlayQueue.OriginalComplete"),
            "finally block must mark complete even when paging exits early",
        )

    def test_browse_playlist_uses_first_page_and_hidden_continuation(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=75)
        content = self.build_content(spotify)
        content._PluginContent__params = {
            "action": ["browse_playlist"],
            "playlistid": ["playlist-1"],
        }
        content._PluginContent__action = "browse_playlist"
        self.set_active_listing(content)

        content.browse_playlist()

        self.assertEqual(["fetch:0"], events)
        self.assertEqual(1, len(DeferredThread.started_targets))

    def test_browse_playlist_skips_hidden_continuation_when_folder_is_not_active(self):
        for active_folder in ("", "plugin://plugin.audio.spotifykodiconnect/?action=other"):
            with self.subTest(active_folder=active_folder or "unknown"):
                events = RecordingPlayer.events
                events.clear()
                DeferredThread.started_targets.clear()
                spotify = FakeSpotify(events, total=75)
                content = self.build_content(spotify)
                content._PluginContent__params = {
                    "action": ["browse_playlist"],
                    "playlistid": ["playlist-1"],
                }
                content._PluginContent__action = "browse_playlist"
                self.plugin_content.xbmc.getInfoLabel = (
                    lambda label, active_folder=active_folder: active_folder
                )

                content.browse_playlist()

                self.assertEqual(["fetch:0"], events)
                self.assertEqual([], DeferredThread.started_targets)

    def test_browse_saved_tracks_uses_first_page_and_hidden_continuation(self):
        events = RecordingPlayer.events
        spotify = SavedTracksSpotify(events, saved_track_total=75)
        content = self.build_content(spotify)
        content._PluginContent__params = {"action": ["browse_saved_tracks"]}
        content._PluginContent__action = "browse_saved_tracks"
        self.set_active_listing(content)

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
        self.set_active_listing(content)

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

    def test_prepare_playlists_does_not_block_on_follow_lookups(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=1)
        content = self.build_content(spotify)

        playlists = content._PluginContent__prepare_playlist_listitems(
            [spotify_playlist(1), spotify_playlist(2, owner_id="user")]
        )

        self.assertEqual([], spotify.playlist_follow_requests)
        self.assertTrue(
            any("follow_playlist" in command for _label, command in playlists[0]["contextitems"])
        )

    def test_prepare_user_playlists_infers_external_items_are_followed_without_lookup(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=1)
        content = self.build_content(spotify)

        playlists = content._PluginContent__prepare_playlist_listitems(
            [spotify_playlist(1), spotify_playlist(2, owner_id="user")],
            relation_mode="user_collection",
        )

        self.assertEqual([], spotify.playlist_follow_requests)
        self.assertTrue(
            any("unfollow_playlist" in command for _label, command in playlists[0]["contextitems"])
        )

    def test_prepare_daylist_uses_dynamic_title_and_daylist_subtitle(self):
        events = RecordingPlayer.events
        spotify = DynamicDaylistSpotify(events, total=1)
        content = self.build_content(spotify)

        playlists = content._PluginContent__prepare_playlist_listitems([spotify_daylist()])

        self.assertEqual("synthpop saturday morning", playlists[0]["name"])
        self.assertEqual("daylist", playlists[0]["label2"])
        self.assertEqual(
            [
                (
                    "37i9dQZF1EP6YuccBxUcC1",
                    "tracks(total),name,description,images,owner(id),id,snapshot_id",
                    "US",
                )
            ],
            spotify.playlist_detail_requests,
        )

    def test_prepare_daylist_keeps_generic_title_retryable_when_spotify_title_is_generic(self):
        events = RecordingPlayer.events
        spotify = GenericDaylistSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__params = {"action": ["browse_playlists"], "ownerid": ["user"]}
        self.set_active_listing(content)
        playlist = spotify_daylist()
        playlist["description"] = "Your day in a playlist."
        playlist["images"] = [
            {"url": "https://daylist.spotifycdn.com/playlist-covers-mix/en/afternoon_default.jpg"}
        ]

        playlists = content._PluginContent__prepare_playlist_listitems([playlist])

        self.assertEqual("daylist", playlists[0]["name"])
        self.assertEqual("daylist", playlists[0]["label2"])
        self.assertNotIn(self.plugin_content.DAYLIST_TITLE_BUCKET_KEY, playlists[0])
        self.assertEqual(1, len(DeferredThread.started_targets))
        self.assertEqual(
            [
                (
                    "37i9dQZF1EP6YuccBxUcC1",
                    "tracks(total),name,description,images,owner(id),id,snapshot_id",
                    "US",
                )
            ],
            spotify.playlist_detail_requests,
        )

    def test_daylist_generic_title_retry_refreshes_active_listing_when_dynamic_title_appears(self):
        events = RecordingPlayer.events
        spotify = EventuallyDynamicDaylistSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__params = {"action": ["browse_playlists"], "ownerid": ["user"]}
        target_url = content._PluginContent__current_request_url()
        commands = []
        self.plugin_content.xbmc.getInfoLabel = lambda label: target_url
        self.plugin_content.xbmc.executebuiltin = lambda command: commands.append(command)

        playlists = content._PluginContent__prepare_playlist_listitems([spotify_daylist()])
        DeferredThread.started_targets[0]()

        self.assertEqual("daylist", playlists[0]["name"])
        self.assertEqual(["Container.Refresh"], commands)
        self.assertEqual(2, len(spotify.playlist_detail_requests))

    def test_prepare_daylist_does_not_downgrade_dynamic_source_title_when_summary_is_generic(self):
        events = RecordingPlayer.events
        spotify = GenericDaylistSpotify(events, total=1)
        content = self.build_content(spotify)
        playlist = spotify_daylist()
        playlist["name"] = "jazz rap funky hip hop sunday afternoon"
        playlist["description"] = "Here's some jazz rap inspired by your listening."
        playlist["images"] = [
            {"url": "https://daylist.spotifycdn.com/playlist-covers-mix/en/afternoon_default.jpg"}
        ]

        playlists = content._PluginContent__prepare_playlist_listitems([playlist])

        self.assertEqual("jazz rap funky hip hop sunday afternoon", playlists[0]["name"])
        self.assertNotIn(self.plugin_content.DAYLIST_TITLE_BUCKET_KEY, playlists[0])

    def test_browse_category_uses_category_sublabel_for_all_playlists(self):
        events = RecordingPlayer.events
        spotify = CategoryPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__filter = "made-for-you"
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_category()

        self.assertEqual(
            ["synthpop saturday morning", "Playlist 2"], [item[1].label for item in rendered]
        )
        self.assertEqual(["Made For You", "Made For You"], [item[1].label2 for item in rendered])

    def test_browse_category_resolves_legacy_made_for_you_slug(self):
        events = RecordingPlayer.events
        spotify = LegacyMadeForYouCategorySpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__filter = "made-for-you"
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_category()

        self.assertEqual(
            [
                (
                    LegacyMadeForYouCategorySpotify.current_made_for_you_id,
                    "US",
                    self.plugin_content.DYNAMIC_PAGE_LIMIT,
                    0,
                )
            ],
            spotify.category_playlist_requests,
        )
        self.assertEqual(
            [
                (
                    LegacyMadeForYouCategorySpotify.current_made_for_you_id,
                    "US",
                    "US",
                )
            ],
            spotify.category_requests,
        )
        self.assertEqual(
            ["synthpop saturday morning", "Playlist 2"], [item[1].label for item in rendered]
        )

    def test_browse_featured_playlists_uses_featured_message_sublabel(self):
        events = RecordingPlayer.events
        spotify = FeaturedPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__filter = "featured"
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_playlists()

        self.assertEqual(["Featured playlists"], [item[1].label2 for item in rendered])

    def test_browse_featured_playlists_falls_back_to_cached_rows_when_spotify_fails(self):
        events = RecordingPlayer.events
        spotify = FailingFeaturedPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__filter = "featured"
        cached_playlist = spotify_playlist(1)
        cached_playlist.update(
            {
                "label2": "Featured playlists",
                "thumb": "DefaultMusicAlbums.png",
                "url": "plugin://plugin.audio.spotifykodiconnect/?action=browse_playlist",
                "contextitems": [],
            }
        )
        content.cache.set(
            "spotify.featuredplaylists",
            {
                "message": "Featured playlists",
                "playlists": {
                    "total": 1,
                    "items": [cached_playlist],
                    self.plugin_content.DYNAMIC_PAGING_LOADED_KEY: 1,
                    self.plugin_content.DYNAMIC_PAGING_COMPLETE_KEY: True,
                },
            },
            checksum="older-featured-checksum",
        )
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_playlists()

        self.assertEqual(["Playlist 1"], [item[1].label for item in rendered])
        self.assertEqual(["Featured playlists"], [item[1].label2 for item in rendered])

    def test_browse_category_falls_back_to_cached_rows_when_spotify_fails(self):
        events = RecordingPlayer.events
        spotify = FailingCategoryPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__filter = "made-for-you"
        cached_playlist = spotify_playlist(1)
        cached_playlist.update(
            {
                "label2": "Made For You",
                "thumb": "DefaultMusicAlbums.png",
                "url": "plugin://plugin.audio.spotifykodiconnect/?action=browse_playlist",
                "contextitems": [],
            }
        )
        content.cache.set(
            "spotify.categoryplaylists.made-for-you",
            {
                "category": "Made For You",
                "playlists": {
                    "total": 1,
                    "items": [cached_playlist],
                    self.plugin_content.DYNAMIC_PAGING_LOADED_KEY: 1,
                    self.plugin_content.DYNAMIC_PAGING_COMPLETE_KEY: True,
                },
            },
            checksum="older-category-checksum",
        )
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_category()

        self.assertEqual(["Playlist 1"], [item[1].label for item in rendered])
        self.assertEqual(["Made For You"], [item[1].label2 for item in rendered])

    def test_browse_user_playlists_uses_playlists_sublabel(self):
        events = RecordingPlayer.events
        spotify = UserPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__owner_id = "user"
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_playlists()

        self.assertEqual(["kodi-136"], [item[1].label2 for item in rendered])

    def test_browse_user_playlists_uses_route_cache_before_spotify_lookup(self):
        events = RecordingPlayer.events
        spotify = UserPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__owner_id = "user"
        cached_playlist = spotify_playlist(1, owner_id="user")
        cached_playlist.update(
            {
                "label2": "kodi-136",
                "thumb": "DefaultMusicAlbums.png",
                "url": "plugin://plugin.audio.spotifykodiconnect/?action=browse_playlist",
                "contextitems": [],
            }
        )
        content.cache.set(
            "spotify.userplaylists.user",
            {
                "items": [cached_playlist],
                "total": 1,
                self.plugin_content.DYNAMIC_PAGING_LOADED_KEY: 1,
                self.plugin_content.DYNAMIC_PAGING_COMPLETE_KEY: True,
            },
            checksum=content._PluginContent__user_playlists_checksum("user"),
        )
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_playlists()

        self.assertEqual([], spotify.user_playlist_requests)
        self.assertEqual(["Playlist 1"], [item[1].label for item in rendered])

    def test_browse_user_playlists_does_not_use_library_checksum_totals(self):
        events = RecordingPlayer.events
        spotify = UserPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__owner_id = "user"

        content.browse_playlists()

        self.assertEqual(
            [("user", self.plugin_content.DYNAMIC_PAGE_LIMIT, 0)], spotify.user_playlist_requests
        )
        self.assertEqual(0, spotify.saved_track_calls)
        self.assertEqual(0, spotify.saved_album_calls)
        self.assertEqual(0, spotify.followed_artist_calls)

    def test_browse_category_uses_current_cache_before_spotify_lookup(self):
        events = RecordingPlayer.events
        spotify = CategoryPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__filter = "made-for-you"
        cached_playlist = spotify_playlist(1)
        cached_playlist.update(
            {
                "label2": "Made For You",
                "thumb": "DefaultMusicAlbums.png",
                "url": "plugin://plugin.audio.spotifykodiconnect/?action=browse_playlist",
                "contextitems": [],
            }
        )
        content.cache.set(
            "spotify.categoryplaylists.made-for-you",
            {
                "category": "Made For You",
                "playlists": {
                    "total": 1,
                    "items": [cached_playlist],
                    self.plugin_content.DYNAMIC_PAGING_LOADED_KEY: 1,
                    self.plugin_content.DYNAMIC_PAGING_COMPLETE_KEY: True,
                },
            },
            checksum=content._PluginContent__playlist_collection_checksum(
                "category", "made-for-you"
            ),
        )
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_category()

        self.assertEqual([], spotify.category_requests)
        self.assertEqual([], spotify.category_playlist_requests)
        self.assertEqual(["Playlist 1"], [item[1].label for item in rendered])

    def test_browse_featured_uses_current_cache_before_spotify_lookup(self):
        events = RecordingPlayer.events
        spotify = FeaturedPlaylistsSpotify(events, total=1)
        content = self.build_content(spotify)
        content._PluginContent__filter = "featured"
        cached_playlist = spotify_playlist(1)
        cached_playlist.update(
            {
                "label2": "Featured playlists",
                "thumb": "DefaultMusicAlbums.png",
                "url": "plugin://plugin.audio.spotifykodiconnect/?action=browse_playlist",
                "contextitems": [],
            }
        )
        content.cache.set(
            "spotify.featuredplaylists",
            {
                "message": "Featured playlists",
                "playlists": {
                    "total": 1,
                    "items": [cached_playlist],
                    self.plugin_content.DYNAMIC_PAGING_LOADED_KEY: 1,
                    self.plugin_content.DYNAMIC_PAGING_COMPLETE_KEY: True,
                },
            },
            checksum=content._PluginContent__playlist_collection_checksum("featured", "US"),
        )
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_playlists()

        self.assertEqual([], spotify.featured_playlist_requests)
        self.assertEqual(["Playlist 1"], [item[1].label for item in rendered])

    def test_browse_new_releases_uses_release_page_without_album_detail_hydration(self):
        events = RecordingPlayer.events
        spotify = NewReleasesSpotify(events, total=1)
        content = self.build_content(spotify)
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )

        content.browse_new_releases()

        self.assertEqual(
            [("US", self.plugin_content.DYNAMIC_PAGE_LIMIT, 0)], spotify.new_release_requests
        )
        self.assertEqual([], spotify.album_detail_requests)
        self.assertEqual(["Album 1"], [item[1].label for item in rendered])

    def test_dynamic_refresh_skips_when_active_folder_is_unknown(self):
        events = RecordingPlayer.events
        spotify = FakeSpotify(events, total=1)
        content = self.build_content(spotify)
        commands = []

        self.plugin_content.xbmc.getInfoLabel = lambda label: ""
        self.plugin_content.xbmc.executebuiltin = lambda command: commands.append(command)

        content._PluginContent__refresh_active_listing(
            "plugin://plugin.audio.spotifykodiconnect/?action=browse_playlists"
        )

        self.assertEqual([], commands)

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

    def test_add_playlists_refreshes_bucketed_canonical_daylist_before_rendering(self):
        events = RecordingPlayer.events
        spotify = DynamicDaylistSpotify(events, total=1)
        content = self.build_content(spotify)
        rendered = []
        self.plugin_content.xbmcplugin.addDirectoryItem = (
            lambda handle, url, listitem, isFolder: rendered.append((url, listitem, isFolder))
        )
        cached_daylist = spotify_daylist()
        cached_daylist["description"] = "Your day in a playlist."
        cached_daylist["url"] = "plugin://plugin.audio.spotifykodiconnect/?action=browse_playlist"
        cached_daylist[self.plugin_content.DAYLIST_TITLE_BUCKET_KEY] = (
            self.plugin_content._daylist_title_bucket()
        )

        content._PluginContent__add_playlist_listitems([cached_daylist], group_label="Made For You")

        self.assertEqual(1, len(rendered))
        self.assertEqual("synthpop saturday morning", rendered[0][1].label)
        self.assertEqual("Made For You", rendered[0][1].label2)
        self.assertEqual(
            [
                (
                    "37i9dQZF1EP6YuccBxUcC1",
                    "tracks(total),name,description,images,owner(id),id,snapshot_id",
                    "US",
                )
            ],
            spotify.playlist_detail_requests,
        )

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

        self.assertEqual(f"v{self.plugin_content.CACHE_SCHEMA_VERSION}-3-5-7-", checksum)
        self.assertEqual([(1, 0, "US")], spotify.saved_track_requests)
        self.assertEqual([(1, 0)], spotify.saved_album_requests)
        self.assertEqual([(1, None)], spotify.followed_artist_requests)

    def test_cache_checksum_reuses_base_for_optional_values(self):
        events = RecordingPlayer.events
        spotify = ChecksumSpotify(events)
        content = self.build_content(spotify)

        first = content._PluginContent__cache_checksum("playlist-snapshot")
        second = content._PluginContent__cache_checksum("artist-albums")

        self.assertEqual(
            f"v{self.plugin_content.CACHE_SCHEMA_VERSION}-3-5-7--playlist-snapshot", first
        )
        self.assertEqual(
            f"v{self.plugin_content.CACHE_SCHEMA_VERSION}-3-5-7--artist-albums", second
        )
        self.assertEqual(1, spotify.saved_track_calls)
        self.assertEqual(1, spotify.saved_album_calls)
        self.assertEqual(1, spotify.followed_artist_calls)


if __name__ == "__main__":
    unittest.main()
