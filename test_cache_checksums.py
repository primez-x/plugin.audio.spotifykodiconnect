"""Cache checksum restructure: content caches do not depend on library totals,
the library-totals checksum is memoised across plugin processes, and
like/save/follow actions are reflected in cached listings via overrides
instead of invalidating every cache."""

import unittest

import test_playlist_fastpath as fp


class SettingsAddon(fp.FakeAddon):
    def __init__(self):
        self.settings = {}

    def getSetting(self, key):
        return self.settings.get(key, "")

    def setSetting(self, key, value):
        self.settings[key] = value


class ContentSpotify(fp.ChecksumSpotify):
    def __init__(self, events):
        super().__init__(events)
        self.saved_track_adds = []

    def album(self, album_id, market=None):
        album = fp.spotify_album(1)
        album["id"] = album_id
        album["tracks"] = {"total": 2}
        return album

    def album_tracks(self, album_id, market=None, limit=50, offset=0):
        return {"items": [{"id": "track-1"}, {"id": "track-2"}]}

    def artist_top_tracks(self, artist_id, country=None):
        return {"tracks": [fp.spotify_track(1), fp.spotify_track(2)]}

    def artist_related_artists(self, artist_id):
        return {"artists": [fp.spotify_artist(1)]}

    def artist_albums(self, artist_id, album_type=None, country=None, limit=50, offset=0):
        return {"total": 1, "items": [{"id": "album-1"}]}

    def current_user_saved_tracks_add(self, track_ids):
        self.saved_track_adds.append(tuple(track_ids))


class ContentTestCase(unittest.TestCase):
    def setUp(self):
        self.pc = fp.import_plugin_content()
        fp.FakeWindow.windows.clear()
        fp.DeferredThread.started_targets.clear()
        fp.RecordingPlayer.events.clear()
        for name in (
            "SORT_METHOD_TRACKNUM",
            "SORT_METHOD_TITLE",
            "SORT_METHOD_VIDEO_YEAR",
            "SORT_METHOD_SONG_RATING",
            "SORT_METHOD_ARTIST",
            "SORT_METHOD_ALBUM_IGNORE_THE",
        ):
            setattr(self.pc.xbmcplugin, name, 0)
        self.commands = []
        self.pc.xbmc.executebuiltin = lambda command: self.commands.append(command)
        self.addon = SettingsAddon()
        self.cache = fp.FakeCache()

    def build(self, spotify):
        content = object.__new__(self.pc.PluginContent)
        content.cache = self.cache
        content._PluginContent__spotipy = spotify
        content._PluginContent__user_country = "US"
        content._PluginContent__userid = "user"
        content._PluginContent__playlist_id = "playlist-1"
        content._PluginContent__album_id = "album-1"
        content._PluginContent__artist_id = "artist-1"
        content._PluginContent__track_id = "track-1"
        content._PluginContent__addon = self.addon
        content._PluginContent__addon_handle = 1
        content._PluginContent__base_url = "plugin://plugin.audio.spotifykodiconnect"
        content._PluginContent__params = {}
        content._PluginContent__action = ""
        content._PluginContent__cached_checksum = ""
        content._PluginContent__addon_icon_path = "icons"
        return content

    def assert_no_library_totals(self, spotify):
        self.assertEqual(0, spotify.saved_track_calls, "saved tracks total fetched")
        self.assertEqual(0, spotify.saved_album_calls, "saved albums total fetched")
        self.assertEqual(0, spotify.followed_artist_calls, "followed artists total fetched")

    def _saved_album_spotify(self, total, head_added_at="2026-01-01T00:00:00Z"):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        spotify.album_batch_requests = []

        def current_user_saved_albums(limit=50, offset=0, market=None):
            spotify.saved_album_calls += 1
            spotify.saved_album_requests.append((limit, offset, market))
            items = []
            for index in range(offset, min(offset + limit, total)):
                album = fp.spotify_album(index)
                album["tracks"] = {"total": 1, "items": [{"id": f"track-{index}"}]}
                added_at = head_added_at if index == 0 else "2025-01-01T00:00:00Z"
                items.append({"added_at": added_at, "album": album})
            return {"total": total, "items": items}

        def albums(album_ids, market=None):
            spotify.album_batch_requests.append(tuple(album_ids))
            return {"albums": [fp.spotify_album(1) for _ in album_ids]}

        spotify.current_user_saved_albums = current_user_saved_albums
        spotify.albums = albums
        return spotify


class CacheChecksumTests(ContentTestCase):
    # -- content listings ---------------------------------------------------

    def test_playlist_details_keyed_on_snapshot_without_library_totals(self):
        spotify = fp.FakeSpotify(fp.RecordingPlayer.events, total=10)
        content = self.build(spotify)
        content.browse_playlist()

        self.assert_no_library_totals(spotify)
        _value, checksum = self.cache.values["spotify.playlistdetails.playlist-1"]
        self.assertIn("snapshot-1", checksum)
        self.assertIn("-content-playlist", checksum)

    def test_album_and_artist_listings_ignore_library_totals(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        content = self.build(spotify)

        content.browse_album()
        content.artist_top_tracks()
        content.related_artists()
        content.browse_artist_albums(album_type="album")

        self.assert_no_library_totals(spotify)
        for key in (
            "spotify.album.album-1",
            "spotify.albumtracksalbum-1",
            "spotify.artisttoptracks.artist-1",
            "spotify.relatedartists.artist-1",
            "spotify.artistalbums.album.artist-1",
        ):
            self.assertIn(key, self.cache.values)
            self.assertIn("-content-", self.cache.values[key][1])

    def test_content_checksum_changes_only_on_manual_refresh(self):
        content = self.build(ContentSpotify(fp.RecordingPlayer.events))
        before = content._PluginContent__content_checksum("album")
        self.assertEqual(before, content._PluginContent__content_checksum("album"))
        content.refresh_listing()
        self.assertNotEqual(before, content._PluginContent__content_checksum("album"))

    # -- library checksum memo ---------------------------------------------

    def test_library_checksum_memoised_across_processes(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        first = self.build(spotify)._PluginContent__cache_checksum()
        second = self.build(spotify)._PluginContent__cache_checksum()

        self.assertEqual(first, second)
        self.assertEqual(1, spotify.saved_track_calls)
        self.assertEqual(1, spotify.saved_album_calls)
        self.assertEqual(1, spotify.followed_artist_calls)

    def test_library_checksum_memo_expires(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        self.build(spotify)._PluginContent__cache_checksum()
        real_time = self.pc.time.time
        self.pc.time.time = lambda: real_time() + self.pc.LIBRARY_CHECKSUM_TTL_SECS + 1
        try:
            self.build(spotify)._PluginContent__cache_checksum()
        finally:
            self.pc.time.time = real_time
        self.assertEqual(2, spotify.saved_track_calls)

    def test_relation_action_invalidates_library_memo_but_not_content(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        content = self.build(spotify)
        content._PluginContent__cache_checksum()
        content_checksum = content._PluginContent__content_checksum("album")
        content.refresh_listing = lambda: self.fail("save_track must not bump cache_checksum")

        content.save_track()

        self.assertEqual([("track-1",)], spotify.saved_track_adds)
        self.assertEqual(["Container.Refresh"], self.commands)
        self.assertEqual(content_checksum, content._PluginContent__content_checksum("album"))
        self.build(spotify)._PluginContent__cache_checksum()
        self.assertEqual(2, spotify.saved_track_calls, "memo cleared by like action")

    # -- render-time overrides ---------------------------------------------

    def _prepared_tracks(self, content):
        return content._PluginContent__prepare_track_listitems(
            tracks=[fp.spotify_track(1), fp.spotify_track(2)], include_artist_fanart=False
        )

    @staticmethod
    def _context(content, track):
        """Context menu as rendered (menus are built at render time)."""
        return content._PluginContent__get_track_list([track])[0][1].context_items

    def _liked_actions(self, content, track):
        actions = []
        for _label, command in self._context(content, track):
            for action in ("save_track", "remove_track"):
                if f"?action={action}&" in command:
                    actions.append(action)
        return actions

    def test_cached_tracks_reflect_later_like_action(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        content = self.build(spotify)
        tracks = self._prepared_tracks(content)
        self.assertEqual(["save_track"], self._liked_actions(content, tracks[0]))

        real_time = self.pc.time.time
        self.pc.time.time = lambda: real_time() + 5
        try:
            content.save_track()
        finally:
            self.pc.time.time = real_time
        content._PluginContent__apply_relation_overrides(tracks, content.TRACK_RELATION_FIELDS)

        self.assertEqual(["remove_track"], self._liked_actions(content, tracks[0]))
        self.assertEqual(["save_track"], self._liked_actions(content, tracks[1]))
        labels = [
            label for label, cmd in self._context(content, tracks[0]) if "remove_track" in cmd
        ]
        self.assertEqual([f"str-{self.pc.REMOVE_FROM_LIKED_SONGS_STR_ID}"], labels)

    def test_override_older_than_item_snapshot_is_ignored(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        content = self.build(spotify)
        real_time = self.pc.time.time
        self.pc.time.time = lambda: real_time() - 60
        try:
            content.save_track()
        finally:
            self.pc.time.time = real_time
        # Fresh lookup after the action says "not liked" (e.g. unliked on phone).
        content._PluginContent__set_relation_cache("savedtrack", "track-1", False)
        tracks = self._prepared_tracks(content)
        content._PluginContent__apply_relation_overrides(tracks, content.TRACK_RELATION_FIELDS)
        self.assertEqual(["save_track"], self._liked_actions(content, tracks[0]))

    def test_follow_override_flips_artist_entry(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        content = self.build(spotify)
        artists = content._PluginContent__prepare_artist_listitems([fp.spotify_artist(1)])
        real_time = self.pc.time.time
        self.pc.time.time = lambda: real_time() + 5
        spotify.user_follow_artists = lambda ids: None
        try:
            content.follow_artist()
        finally:
            self.pc.time.time = real_time
        content._PluginContent__apply_relation_overrides(artists, content.ARTIST_RELATION_FIELDS)
        menu = content._PluginContent__get_artist_context_menu_items(
            artists[0], artists[0]["followed"]
        )
        commands = [cmd for _label, cmd in menu]
        self.assertTrue(any("?action=unfollow_artist&artistid=artist-1" in c for c in commands))
        self.assertFalse(any("?action=follow_artist&" in c for c in commands))

    def test_overrides_are_bounded(self):
        content = self.build(ContentSpotify(fp.RecordingPlayer.events))
        for index in range(self.pc.RELATION_OVERRIDES_MAX_ITEMS + 25):
            content._PluginContent__record_relation_override("savedtrack", f"t{index}", True)
        overrides = content._PluginContent__load_relation_overrides()
        self.assertEqual(self.pc.RELATION_OVERRIDES_MAX_ITEMS, len(overrides))

    # -- request efficiency -------------------------------------------------

    def test_search_sub_links_url_encode_query(self):
        import urllib.parse

        spotify = ContentSpotify(fp.RecordingPlayer.events)
        totals = {key: {"total": 1} for key in ("artists", "albums", "tracks", "playlists")}
        spotify.search = lambda **kwargs: totals
        content = self.build(spotify)
        content._PluginContent__filter = "Simon & Garfunkel #1+2=3"
        urls = []
        real_add = self.pc.xbmcplugin.addDirectoryItem
        self.pc.xbmcplugin.addDirectoryItem = lambda handle, url, listitem, isFolder: urls.append(
            url
        )
        try:
            content.search()
        finally:
            self.pc.xbmcplugin.addDirectoryItem = real_add

        self.assertEqual(4, len(urls))
        expected = {
            "search_artists": "artistid",
            "search_playlists": "playlistid",
            "search_albums": "albumid",
            "search_tracks": "trackid",
        }
        for url in urls:
            params = urllib.parse.parse_qs(url.split("?", 1)[1])
            key = expected.pop(params["action"][0])
            self.assertEqual({"action", key}, set(params))
            self.assertEqual(["Simon & Garfunkel #1+2=3"], params[key])
        self.assertEqual({}, expected)

    def test_saved_albums_built_from_me_albums_objects(self):
        spotify = self._saved_album_spotify(total=60)
        content = self.build(spotify)

        albums = content._PluginContent__get_saved_albums()

        self.assertEqual(60, len(albums))
        self.assertEqual([], spotify.album_batch_requests, "no /albums?ids= refetch")
        self.assertEqual([(50, 0, "US"), (50, 50, "US")], spotify.saved_album_requests)
        self.assertEqual([], spotify.saved_album_contains_requests, "all known saved")
        self.assertEqual(0, spotify.saved_track_calls, "saved-track total not in checksum")
        self.assertEqual("Album 0", albums[0]["name"])
        self.assertEqual(2024, albums[0]["year"])
        self.assertEqual("Artist 0", albums[0]["artist"])
        self.assertNotIn("tracks", albums[0])
        self.assertTrue(albums[0]["saved"])
        self.assertNotIn("contextitems", albums[0], "menus are built at render time")
        menu = content._PluginContent__get_album_track_context_menu_items(albums[0], True)
        commands = [cmd for _label, cmd in menu]
        self.assertTrue(any("?action=remove_album&albumid=album-0" in c for c in commands))
        _value, checksum = self.cache.values["spotify.savedalbums.user"]
        self.assertIn("-savedalbums-60-2026-01-01T00:00:00Z-album-0-", checksum)

    def test_saved_albums_cache_keyed_on_total_and_newest_added_at(self):
        spotify = self._saved_album_spotify(total=60)
        self.build(spotify)._PluginContent__get_saved_albums()
        spotify.saved_album_requests.clear()

        self.build(spotify)._PluginContent__get_saved_albums()
        self.assertEqual([(50, 0, "US")], spotify.saved_album_requests, "cache hit")

        changed = self._saved_album_spotify(total=60, head_added_at="2026-02-02T00:00:00Z")
        self.build(changed)._PluginContent__get_saved_albums()
        self.assertEqual([(50, 0, "US"), (50, 50, "US")], changed.saved_album_requests)

    def test_saved_artists_still_built_from_saved_albums(self):
        spotify = self._saved_album_spotify(total=2)
        spotify.followed_total = 0
        spotify.artists = lambda ids: {"artists": [fp.spotify_artist(int(i[-1])) for i in ids]}
        content = self.build(spotify)

        artists = content._PluginContent__get_saved_artists()

        self.assertEqual(["artist-0", "artist-1"], [artist["id"] for artist in artists])

    def test_album_tracks_reuse_embedded_first_page(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        page_requests = []
        album = fp.spotify_album(1)
        album["tracks"] = {"total": 60, "items": [{"id": f"track-{i}"} for i in range(50)]}
        spotify.album = lambda album_id, market=None: album

        def album_tracks(album_id, market=None, limit=50, offset=0):
            page_requests.append((limit, offset))
            return {"items": [{"id": f"track-{i}"} for i in range(offset, 60)]}

        spotify.album_tracks = album_tracks
        content = self.build(spotify)

        content.browse_album()

        self.assertEqual([(50, 50)], page_requests, "only page beyond the embedded tracks")
        self.assertEqual([50, 10], [len(ids) for ids, _m in spotify.track_detail_requests])
        tracks, _checksum = self.cache.values["spotify.albumtracksalbum-1"]
        self.assertEqual(60, len(tracks))
        self.assertNotIn("tracks", tracks[0]["album"])
        self.assertEqual("Album 1", tracks[0]["album"]["name"])

    def test_relation_states_read_and_written_in_one_cache_call_per_namespace(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        content = self.build(spotify)
        content._PluginContent__prepare_track_listitems(
            tracks=[fp.spotify_track(i) for i in range(5)], include_artist_fanart=False
        )
        # one get_many each for saved tracks and followed artists
        self.assertEqual(2, self.cache.get_many_calls)
        self.assertEqual(2, self.cache.set_many_calls)
        self.assertEqual(1, len(spotify.saved_track_contains_requests))

        content._PluginContent__prepare_track_listitems(
            tracks=[fp.spotify_track(i) for i in range(5)], include_artist_fanart=False
        )
        self.assertEqual(1, len(spotify.saved_track_contains_requests), "served from cache")
        self.assertEqual(1, len(spotify.following_artist_requests), "served from cache")


class SearchSpotify(ContentSpotify):
    def __init__(self, events):
        super().__init__(events)
        self.search_requests = []

    def search(self, q, type, limit=10, offset=0, market=None):
        self.search_requests.append((q, type, limit, offset, market))
        out = {}
        for section, make in (
            ("tracks", fp.spotify_track),
            ("artists", fp.spotify_artist),
            ("albums", fp.spotify_album),
            ("playlists", fp.spotify_playlist),
        ):
            items = [make(i) for i in range(3)]
            if section in ("tracks", "albums"):
                for item in items:
                    item["available_markets"] = ["US", "GB"]
            out[section] = {"total": 99, "items": items + [None]}
        return out


class PerformanceTests(ContentTestCase):
    """Request/caching budget of browsing routes (see the 2026 performance audit)."""

    def setUp(self):
        super().setUp()
        fp.FakeMonitor.wait_calls = 0
        fp.FakeMonitor.on_wait = None
        self.addCleanup(setattr, fp.FakeMonitor, "on_wait", None)

    def render(self, kind, items, **kwargs):
        return fp.rendered_context_items(self.pc, self.content, kind, items, **kwargs)

    # -- search -------------------------------------------------------------

    def _run_search_route(self, spotify, route, query):
        content = self.build(spotify)
        param = {
            "search_artists": "artist_id",
            "search_albums": "album_id",
            "search_tracks": "track_id",
            "search_playlists": "playlist_id",
        }[route]
        setattr(content, f"_PluginContent__{param}", query)
        rendered = []
        module = self.pc.xbmcplugin
        saved = (module.addDirectoryItem, module.addDirectoryItems)
        module.addDirectoryItem = lambda handle=None, url=None, listitem=None, isFolder=None, totalItems=None: rendered.append(
            listitem
        )
        module.addDirectoryItems = lambda handle, items, totalItems=None: rendered.extend(
            li for _u, li, _f in items
        )
        try:
            getattr(content, route)()
        finally:
            module.addDirectoryItem, module.addDirectoryItems = saved
        return rendered

    def test_four_search_widgets_share_one_search_call(self):
        spotify = SearchSpotify(fp.RecordingPlayer.events)
        counts = {}
        for route, query in (
            ("search_artists", "Daft Punk"),
            ("search_albums", "  daft   PUNK "),
            ("search_tracks", "daft punk"),
            ("search_playlists", "Daft Punk"),
        ):
            counts[route] = len(self._run_search_route(spotify, route, query))

        self.assertEqual(1, len(spotify.search_requests), "one /search call per term")
        q, types, limit, offset, market = spotify.search_requests[0]
        self.assertEqual("Daft Punk", q, "no artist:/track:/album: field prefixes")
        self.assertEqual({"artist", "album", "track", "playlist"}, set(types.split(",")))
        self.assertEqual((50, 0, "US"), (limit, offset, market))
        self.assertEqual(
            {"search_artists": 3, "search_albums": 3, "search_tracks": 3, "search_playlists": 3},
            counts,
        )
        self.assertEqual([], spotify.album_detail_requests, "no /albums re-fetch")
        self.assertEqual(1, len(spotify.saved_album_contains_requests), "batched saved check")
        (key,) = [k for k in self.cache.values if k.startswith("spotify.search.")]
        cached = self.cache.values[key][0]
        self.assertNotIn("available_markets", cached["tracks"]["items"][0])
        self.assertNotIn("available_markets", cached["albums"]["items"][0])

    def test_search_menu_totals_come_from_the_shared_call(self):
        spotify = SearchSpotify(fp.RecordingPlayer.events)
        content = self.build(spotify)
        content._PluginContent__filter = "daft punk"
        labels = []
        real_add = self.pc.xbmcplugin.addDirectoryItem
        self.pc.xbmcplugin.addDirectoryItem = lambda handle, url, listitem, isFolder: labels.append(
            listitem.label
        )
        try:
            content.search()
        finally:
            self.pc.xbmcplugin.addDirectoryItem = real_add
        self.assertTrue(all(label.endswith("(99)") for label in labels))
        self._run_search_route(spotify, "search_tracks", "Daft Punk")
        self.assertEqual(1, len(spotify.search_requests))

    def test_search_waits_for_an_in_flight_process_instead_of_calling(self):
        first = SearchSpotify(fp.RecordingPlayer.events)
        second = SearchSpotify(fp.RecordingPlayer.events)
        probe = self.build(second)
        key = probe._PluginContent__search_cache_key("abba")
        marker = f"{self.pc.SEARCH_INFLIGHT_PROP_PREFIX}{key}"
        win = probe._PluginContent__win  # the plugin's home-window handle
        win.setProperty(marker, f"{self.pc.time.time():.3f}-1-1")

        def other_process_finishes():
            if fp.FakeMonitor.wait_calls == 3:
                win.clearProperty(marker)
                self.build(first)._PluginContent__get_search_results("abba")

        fp.FakeMonitor.on_wait = other_process_finishes
        rendered = self._run_search_route(second, "search_tracks", "abba")

        self.assertEqual(3, len(rendered))
        self.assertEqual(1, len(first.search_requests))
        self.assertEqual([], second.search_requests, "served from the first process' result")

    def test_search_in_flight_wait_is_bounded(self):
        spotify = SearchSpotify(fp.RecordingPlayer.events)
        probe = self.build(spotify)
        marker = self.pc.SEARCH_INFLIGHT_PROP_PREFIX + probe._PluginContent__search_cache_key("x")
        win = probe._PluginContent__win  # the plugin's home-window handle
        win.setProperty(marker, f"{self.pc.time.time():.3f}-1-1")

        self._run_search_route(spotify, "search_artists", "x")

        expected = int(self.pc.SEARCH_INFLIGHT_WAIT_SECS / self.pc.SEARCH_INFLIGHT_POLL_SECS)
        self.assertLessEqual(fp.FakeMonitor.wait_calls, expected)
        self.assertGreater(fp.FakeMonitor.wait_calls, 0)
        self.assertEqual(1, len(spotify.search_requests), "gives up waiting and calls itself")

    def test_stale_in_flight_marker_is_ignored(self):
        spotify = SearchSpotify(fp.RecordingPlayer.events)
        probe = self.build(spotify)
        marker = self.pc.SEARCH_INFLIGHT_PROP_PREFIX + probe._PluginContent__search_cache_key("x")
        win = probe._PluginContent__win  # the plugin's home-window handle
        win.setProperty(marker, f"{self.pc.time.time() - 60:.3f}-1-1")

        self._run_search_route(spotify, "search_artists", "x")

        self.assertEqual(0, fp.FakeMonitor.wait_calls)
        self.assertEqual(1, len(spotify.search_requests))
        self.assertEqual("", win.getProperty(marker), "own marker cleared afterwards")

    # -- lean rows, render-time menus ---------------------------------------

    def test_cached_track_rows_are_lean_and_menus_built_at_render(self):
        import json

        spotify = ContentSpotify(fp.RecordingPlayer.events)
        self.content = content = self.build(spotify)
        rich = fp.spotify_track(1)
        rich["album"]["images"] = [{"url": f"https://i.example/{s}.jpg"} for s in (640, 300, 64)]
        rich["album"]["artists"] = [{"id": "artist-1", "name": "Artist 1", "uri": "x" * 40}]
        rich["available_markets"] = ["US"] * 180
        rich["external_ids"] = {"isrc": "USX"}
        relinked = fp.spotify_track(2)
        relinked["linked_from"] = {"id": "orig-2", "uri": "spotify:track:orig-2"}
        spotify.current_user_saved_tracks_contains = lambda ids: [i == "track-1" for i in ids]
        spotify.current_user_following_artists = lambda ids: [i == "artist-2" for i in ids]

        tracks = content._PluginContent__prepare_track_listitems(
            tracks=[rich, relinked], include_artist_fanart=False
        )
        rows = json.loads(json.dumps(tracks))  # what simplecache stores and returns

        self.assertLess(len(json.dumps(rows[0])), 700)
        self.assertNotIn("contextitems", rows[0])
        self.assertEqual("https://i.example/640.jpg", rows[0]["thumb"])
        self.assertEqual((True, False), (rows[0]["saved"], rows[0]["artist_followed"]))

        own_playlist = {"id": "pl-1", "name": "Mine", "owner": {"id": "user"}}
        menus = self.render(
            "track", rows, append_artist_to_label=True, playlist_details=own_playlist
        )
        commands = [[cmd for _label, cmd in menu] for menu in menus]
        self.assertTrue(any("?action=remove_track&trackid=track-1)" in c for c in commands[0]))
        self.assertTrue(any("?action=save_track&trackid=orig-2)" in c for c in commands[1]))
        self.assertTrue(
            any(
                "remove_track_from_playlist&trackid=spotify:track:orig-2&playlistid=pl-1" in c
                for c in commands[1]
            )
        )
        self.assertTrue(any("?action=follow_artist&artistid=artist-1)" in c for c in commands[0]))
        self.assertTrue(any("?action=unfollow_artist&artistid=artist-2)" in c for c in commands[1]))
        self.assertEqual(12, len(menus[0]))
        foreign = self.render("track", rows, playlist_details={"id": "p", "owner": {"id": "x"}})
        self.assertEqual(11, len(foreign[0]), "no remove-from-playlist on foreign playlists")

    def test_render_applies_newer_override_to_lean_rows(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        self.content = content = self.build(spotify)
        rows = content._PluginContent__prepare_track_listitems(
            tracks=[fp.spotify_track(1)], include_artist_fanart=False
        )
        real_time = self.pc.time.time
        self.pc.time.time = lambda: real_time() + 5
        try:
            content.save_track()
        finally:
            self.pc.time.time = real_time
        (menu,) = self.render("track", rows)
        self.assertTrue(any("?action=remove_track&trackid=track-1)" in c for _l, c in menu))

    def test_album_artist_playlist_rows_store_states_not_menus(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        self.content = content = self.build(spotify)
        albums = content._PluginContent__prepare_album_listitems(albums=[fp.spotify_album(1)])
        artists = content._PluginContent__prepare_artist_listitems([fp.spotify_artist(1)])
        playlists = content._PluginContent__prepare_playlist_listitems([fp.spotify_playlist(1)])
        for row in albums + artists + playlists:
            self.assertNotIn("contextitems", row)
        self.assertEqual(False, albums[0]["saved"])
        self.assertEqual(False, artists[0]["followed"])
        self.assertIsNone(playlists[0]["followed"])
        album_menu = self.render("album", albums)[0]
        self.assertTrue(any("?action=save_album&albumid=album-1)" in c for _l, c in album_menu))
        artist_menu = self.render("artist", artists)[0]
        self.assertTrue(
            any("?action=follow_artist&artistid=artist-1)" in c for _l, c in artist_menu)
        )
        playlist_menu = self.render("playlist", playlists)[0]
        self.assertTrue(any("?action=follow_playlist&" in c for _l, c in playlist_menu))

    # -- chunked paged collections ------------------------------------------

    def test_playlist_chunks_round_trip_without_refetch(self):
        spotify = fp.FakeSpotify(fp.RecordingPlayer.events, total=1200)
        content = self.build(spotify)
        content._PluginContent__params = {
            "action": ["browse_playlist"],
            "playlistid": ["playlist-1"],
        }
        content._PluginContent__action = "browse_playlist"
        target = content._PluginContent__current_request_url()
        self.pc.xbmc.getInfoLabel = lambda label: target
        content.browse_playlist()
        fp.DeferredThread.started_targets[0]()
        head, items = fp.read_chunked(self.cache, "spotify.playlistdetails.playlist-1")
        self.assertEqual(3, head["_chunks"])
        self.assertEqual(
            [500, 500, 200],
            [
                len(self.cache.values[f"spotify.playlistdetails.playlist-1.chunk{i}"][0])
                for i in range(3)
            ],
        )

        fp.RecordingPlayer.events.clear()
        fp.DeferredThread.started_targets.clear()
        self.content = again = self.build(spotify)
        details = again._PluginContent__get_playlist_details("playlist-1")
        self.assertEqual([], fp.RecordingPlayer.events, "served from chunk rows")
        self.assertEqual(
            [f"track-{i}" for i in range(1200)], [t["id"] for t in details["tracks"]["items"]]
        )
        self.assertEqual([], fp.DeferredThread.started_targets, "complete: no continuation")

    def test_missing_chunk_is_a_cache_miss(self):
        spotify = fp.FakeSpotify(fp.RecordingPlayer.events, total=20)
        self.build(spotify)._PluginContent__get_playlist_details("playlist-1")
        del self.cache.values["spotify.playlistdetails.playlist-1.chunk0"]
        fp.RecordingPlayer.events.clear()
        self.build(spotify)._PluginContent__get_playlist_details("playlist-1")
        self.assertEqual(["fetch:0"], fp.RecordingPlayer.events)

    def test_saved_tracks_bytes_written_for_large_load_stay_linear(self):
        import json

        spotify = fp.SavedTracksSpotify(fp.RecordingPlayer.events, saved_track_total=2000)
        content = self.build(spotify)
        content._PluginContent__params = {"action": ["browse_saved_tracks"]}
        content._PluginContent__action = "browse_saved_tracks"
        target = content._PluginContent__current_request_url()
        self.pc.xbmc.getInfoLabel = lambda label: target
        written = []
        real_set_many = self.cache.set_many

        def counting_set_many(items, **kwargs):
            written.append(sum(len(json.dumps(v)) for v in dict(items).values()))
            return real_set_many(items, **kwargs)

        self.cache.set_many = counting_set_many
        content.browse_saved_tracks()
        fp.DeferredThread.started_targets[0]()
        _head, items = fp.read_chunked(self.cache, "spotify.savedtracks.user")
        self.assertEqual(2000, len(items))
        total_items_size = len(json.dumps(items))
        # whole-list rewrites every 5 pages would write ~9x the final size
        self.assertLess(sum(written), 2.5 * total_items_size)

    # -- saved artists ------------------------------------------------------

    def test_saved_artists_cache_hit_costs_two_calls(self):
        spotify = self._saved_album_spotify(total=2)
        spotify.followed_total = 3
        artist_lookups = []

        def artists(ids):
            artist_lookups.append(tuple(ids))
            return {"artists": [fp.spotify_artist(int(i.split("-")[-1])) for i in ids]}

        spotify.artists = artists
        first = self.build(spotify)._PluginContent__get_saved_artists()
        fp.FakeWindow.windows.clear()  # no library-totals memo in a later process
        spotify.saved_album_calls = spotify.followed_artist_calls = spotify.saved_track_calls = 0
        del artist_lookups[:]

        second = self.build(spotify)._PluginContent__get_saved_artists()

        self.assertEqual([a["id"] for a in first], [a["id"] for a in second])
        self.assertEqual(1, spotify.saved_album_calls)
        self.assertEqual(1, spotify.followed_artist_calls)
        self.assertEqual(0, spotify.saved_track_calls, "no library-totals checksum")
        self.assertEqual([], artist_lookups)

    def test_saved_artists_rebuild_when_followed_head_changes(self):
        spotify = self._saved_album_spotify(total=1)
        spotify.followed_total = 1
        spotify.artists = lambda ids: {"artists": [fp.spotify_artist(0) for _ in ids]}
        self.build(spotify)._PluginContent__get_saved_artists()
        original = spotify.current_user_followed_artists

        def swapped(limit=50, after=None):
            page = original(limit=limit, after=after)
            page["artists"]["items"] = [fp.spotify_artist(42)]
            return page

        spotify.current_user_followed_artists = swapped
        artists = self.build(spotify)._PluginContent__get_saved_artists()
        self.assertIn("artist-42", [a["id"] for a in artists])

    # -- explore categories -------------------------------------------------

    def test_explore_categories_cached_per_country(self):
        spotify = fp.LegacyMadeForYouCategorySpotify(fp.RecordingPlayer.events, total=1)
        calls = []
        real_categories = spotify.categories

        def categories(**kwargs):
            calls.append(kwargs.get("country"))
            return real_categories(**kwargs)

        spotify.categories = categories
        labels = []
        real_add = self.pc.xbmcplugin.addDirectoryItem
        self.pc.xbmcplugin.addDirectoryItem = lambda handle, url, listitem, isFolder: labels.append(
            listitem.label
        )
        try:
            self.build(spotify).browse_main_explore()
            self.build(spotify).browse_main_explore()
            other = self.build(spotify)
            other._PluginContent__user_country = "SE"
            other.browse_main_explore()
        finally:
            self.pc.xbmcplugin.addDirectoryItem = real_add
        self.assertEqual(["US", "SE"], calls)
        self.assertEqual(3, labels.count("Made For You"))
        (key,) = [k for k in self.cache.values if k.startswith("spotify.categories.US")]
        self.assertIn("-content-categories-", self.cache.values[key][1])

    def test_legacy_category_alias_reuses_cached_categories(self):
        spotify = fp.LegacyMadeForYouCategorySpotify(fp.RecordingPlayer.events, total=1)
        calls = []
        real_categories = spotify.categories
        spotify.categories = lambda **kw: calls.append(1) or real_categories(**kw)
        self.build(spotify)._PluginContent__get_category_list()
        resolved = self.build(spotify)._PluginContent__resolve_category_id("made-for-you")
        self.assertEqual(spotify.current_made_for_you_id, resolved)
        self.assertEqual(1, len(calls))

    # -- artist fanart ------------------------------------------------------

    def test_artist_fanart_persisted_across_processes(self):
        spotify = fp.FanartSpotify(fp.RecordingPlayer.events, total=1)
        first = self.build(spotify)._PluginContent__prepare_track_listitems(
            tracks=[fp.spotify_track(1)], include_context_items=False
        )
        second = self.build(spotify)._PluginContent__prepare_track_listitems(
            tracks=[fp.spotify_track(1)], include_context_items=False
        )
        self.assertEqual("https://images.example/artist-1.jpg", first[0]["artist_fanart"])
        self.assertEqual(first[0]["artist_fanart"], second[0]["artist_fanart"])
        self.assertEqual(1, spotify.artist_calls, "second process reads simplecache")
        (write,) = [
            kw for keys, kw in self.cache.set_many_log if "spotify.artistfanart.artist-1" in keys
        ]
        self.assertIs(False, write["mem_cache"])
        self.assertEqual(self.pc.ARTIST_FANART_PERSIST_EXPIRATION, write["expiration"])

    def test_artist_without_image_is_remembered(self):
        spotify = fp.FakeSpotify(fp.RecordingPlayer.events, total=1)  # artists() has no images
        for _ in range(2):
            self.build(spotify)._PluginContent__prepare_track_listitems(
                tracks=[fp.spotify_track(1)], include_context_items=False
            )
        self.assertEqual(1, spotify.artist_calls)

    # -- daylist ------------------------------------------------------------

    def test_daylist_title_looked_up_once_for_prepare_and_render(self):
        spotify = fp.GenericDaylistSpotify(fp.RecordingPlayer.events, total=1)
        self.content = content = self.build(spotify)
        content._PluginContent__params = {"action": ["browse_category"]}
        playlists = content._PluginContent__prepare_playlist_listitems([fp.spotify_daylist()])
        self.render("playlist", playlists)
        self.assertEqual(1, len(spotify.playlist_detail_requests))

    # -- clear cache --------------------------------------------------------

    def test_clear_cache_empties_the_table_instead_of_unlinking_the_file(self):
        content = self.build(ContentSpotify(fp.RecordingPlayer.events))
        cleared, removed = [], []
        content.cache.clear_all = lambda: cleared.append(True) or True
        real_remove, real_dialog = self.pc.os.remove, self.pc.xbmcgui.Dialog
        self.pc.os.remove = removed.append
        self.pc.xbmcgui.Dialog = lambda: type("D", (), {"ok": lambda *a: None})()
        try:
            content.delete_cache_db()
            self.assertEqual([True], cleared)
            self.assertEqual([], removed, "a WAL database must not be unlinked while open")
            content.cache.clear_all = lambda: False  # unreadable database
            content.delete_cache_db()
            self.assertEqual(3, len(removed), "db, -wal and -shm removed together")
        finally:
            self.pc.os.remove, self.pc.xbmcgui.Dialog = real_remove, real_dialog

    # -- imports ------------------------------------------------------------

    def test_spotify_client_is_built_on_first_api_use_only(self):
        built = []

        class Client:
            def __init__(self, auth=None):
                built.append(auth)

            def me(self):
                return {"id": "user"}

        self.pc.sys.modules["spotipy"].Spotify = Client
        win = self.pc.xbmcgui.Window(self.pc.ADDON_WINDOW_ID)
        win.setProperty("Spotify.UserId", "user")
        content = self.build(ContentSpotify(fp.RecordingPlayer.events))
        content.init_spotipy("token")
        self.assertEqual([], built, "no spotipy client (or import) until an API call")
        self.assertEqual({"id": "user"}, content._PluginContent__spotipy.me())
        self.assertEqual(["token"], built)


if __name__ == "__main__":
    unittest.main()
