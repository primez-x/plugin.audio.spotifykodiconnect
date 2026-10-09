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


class CacheChecksumTests(unittest.TestCase):
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
    def _liked_actions(track):
        actions = []
        for _label, command in track["contextitems"]:
            for action in ("save_track", "remove_track"):
                if f"?action={action}&" in command:
                    actions.append(action)
        return actions

    def test_cached_tracks_reflect_later_like_action(self):
        spotify = ContentSpotify(fp.RecordingPlayer.events)
        content = self.build(spotify)
        tracks = self._prepared_tracks(content)
        self.assertEqual(["save_track"], self._liked_actions(tracks[0]))

        real_time = self.pc.time.time
        self.pc.time.time = lambda: real_time() + 5
        try:
            content.save_track()
        finally:
            self.pc.time.time = real_time
        content._PluginContent__apply_relation_overrides(tracks)

        self.assertEqual(["remove_track"], self._liked_actions(tracks[0]))
        self.assertEqual(["save_track"], self._liked_actions(tracks[1]))
        labels = [label for label, cmd in tracks[0]["contextitems"] if "remove_track" in cmd]
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
        content._PluginContent__apply_relation_overrides(tracks)
        self.assertEqual(["save_track"], self._liked_actions(tracks[0]))

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
        content._PluginContent__apply_relation_overrides(artists)
        commands = [cmd for _label, cmd in artists[0]["contextitems"]]
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
        commands = [cmd for _label, cmd in albums[0]["contextitems"]]
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

if __name__ == "__main__":
    unittest.main()
