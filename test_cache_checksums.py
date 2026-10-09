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


if __name__ == "__main__":
    unittest.main()
