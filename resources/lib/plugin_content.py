import json
import math
import os
import sys
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Tuple, Union

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

import main_service
import simplecache
import spotipy
import spotty
import utils
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from string_ids import *
from utils import ADDON_ID, LOGINFO, PROXY_PORT, log_exception, log_msg, get_chunks

try:
    from metadata_fetcher import (
        fetch_artist_bio_lastfm,
        fetch_artist_bio_musicbrainz,
        fetch_album_description,
        PROVIDER_OFF,
        PROVIDER_LASTFM,
        PROVIDER_MUSICBRAINZ,
    )
except ImportError:
    fetch_artist_bio_lastfm = None
    fetch_artist_bio_musicbrainz = None
    fetch_album_description = None
    PROVIDER_OFF = "0"
    PROVIDER_LASTFM = "lastfm"
    PROVIDER_MUSICBRAINZ = "musicbrainz"

# Window property keys for streaming enrichment (show list fast, then enrich in background)
_HOME_WINDOW_ID = 10000
_PROP_ENRICHED_ALBUMS = "Spotify.EnrichedAlbums"
_PROP_ENRICHED_ARTISTS = "Spotify.EnrichedArtists"
_PROP_ENRICHED_ARTIST_BIOS = "Spotify.EnrichedArtistBios"
_PROP_ENRICHED_ALBUM_DESCRIPTIONS = "Spotify.EnrichedAlbumDescriptions"
_PROP_PENDING_ALBUMS = "Spotify.PendingEnrichmentAlbums"
_PROP_PENDING_ARTISTS = "Spotify.PendingEnrichmentArtists"

MUSIC_ARTISTS_ICON = "icon_music_artists.png"
MUSIC_TOP_ARTISTS_ICON = "icon_music_top_artists.png"
MUSIC_SONGS_ICON = "icon_music_songs.png"
MUSIC_TOP_TRACKS_ICON = "icon_music_top_tracks.png"
MUSIC_ALBUMS_ICON = "icon_music_albums.png"
MUSIC_PLAYLISTS_ICON = "icon_music_playlists.png"
MUSIC_LIBRARY_ICON = "icon_music_library.png"
MUSIC_SEARCH_ICON = "icon_music_search.png"
MUSIC_EXPLORE_ICON = "icon_music_explore.png"
CLEAR_CACHE_ICON = "icon_clear_cache.png"

Playlist = Dict[str, Union[str, Dict[str, List[Any]]]]

DO_CACHE_LOGGING = False


def cache_log(msg) -> None:
    if DO_CACHE_LOGGING:
        log_msg(msg)


def _get_len(items) -> int:
    if not items:
        return 0
    return len(items)


def _art_for_item(thumb_url: str, fallback_icon_path: str = None) -> Dict[str, str]:
    """Build full Kodi art dict (thumb, poster, fanart, icon) so every view shows art."""
    url = thumb_url or ""
    if not url and fallback_icon_path:
        url = fallback_icon_path
    if not url:
        return {}
    return {
        "thumb": url,
        "poster": url,
        "fanart": url,
        "icon": url,
    }


class PluginContent:
    __addon: xbmcaddon.Addon = xbmcaddon.Addon(id=ADDON_ID)
    __win: xbmcgui.Window = xbmcgui.Window(utils.ADDON_WINDOW_ID)
    __addon_icon_path = os.path.join(
        xbmcvfs.translatePath(__addon.getAddonInfo("path")), "resources"
    )
    __action = ""
    __spotty: spotty.Spotty = None
    __spotipy: spotipy.Spotify = None
    __userid = ""
    __username = ""
    __user_country = ""
    __offset = 0
    __playlist_id = ""
    __album_id = ""
    __track_id = ""
    __artist_id = ""
    __artist_name = ""
    __owner_id = ""
    __filter = ""
    __token = ""
    __limit = 50
    __params = {}
    __base_url = sys.argv[0]
    __addon_handle = int(sys.argv[1])
    __cached_checksum = ""
    __last_playlist_position = 0

    def __init__(self):
        try:
            # logging.basicConfig(level=logging.DEBUG)

            self.cache: simplecache.SimpleCache = simplecache.SimpleCache(ADDON_ID)

            self.append_artist_to_title: bool = (
                self.__addon.getSetting("appendArtistToTitle") == "true"
            )

            self.__spotty: spotty.Spotty = spotty.get_spotty(SpottyHelper())

            self.check_auth_and_refresh_spotipy()

            self.parse_params()

            if self.__action:
                log_msg(f"Evaluating action '{self.__action}'.")
                handler = self._get_action_handler(self.__action)
                if handler:
                    handler()
                else:
                    log_msg(f"Unknown action '{self.__action}'.", LOGINFO)
                    xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            else:
                log_msg("Browsing main and starting background precache.")
                self.__browse_main()
                precache_thread = threading.Thread(target=self.__precache_library, daemon=True)
                precache_thread.start()

        except Exception as exc:
            log_exception(exc, "PluginContent init error")
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def check_auth_and_refresh_spotipy(self):
        auth_token: str = utils.get_cached_auth_token()
        if auth_token:
            self.init_spotipy(auth_token)
            return

        self.authenticate_plugin_after_login_failure()

    def refresh_spotipy(self):
        auth_token: str = utils.get_cached_auth_token()
        if not auth_token:
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            return

        log_msg(f"Got auth_token '{auth_token}'.")

        self.init_spotipy(auth_token)

    def init_spotipy(self, auth_token: str) -> None:
        self.__spotipy: spotipy.Spotify = spotipy.Spotify(auth=auth_token)
        me = self.__spotipy.me()
        self.__userid = me["id"]
        self.__username = me.get("email") or me.get("id") or ""
        self.__user_country = me.get("country") or ""

    def authenticate_plugin_after_login_failure(self) -> None:
        self.authenticate_plugin(
            self.__addon.getLocalizedString(AUTHENTICATE_INSTRUCTIONS_AFTER_LOGIN_FAIL_STR_ID)
        )

    def authenticate_plugin_request(self) -> None:
        self.authenticate_plugin(self.__addon.getLocalizedString(AUTHENTICATE_INSTRUCTIONS_STR_ID))

    def authenticate_plugin(self, instructions: str) -> None:
        dialog = xbmcgui.Dialog()
        dialog_title = self.__addon.getAddonInfo("name")

        spotty_auth = SpottyAuth(self.__spotty)

        zeroconf_auth = spotty_auth.start_zeroconf_authenticate()
        if zeroconf_auth is None:
            dialog.ok(dialog_title, self.get_zeroconf_program_failed_msg(spotty_auth))
            main_service.abort_main_service = True
            utils.kill_this_plugin()
            return

        dialog.ok(dialog_title, instructions)

        zeroconf_auth.terminate()

        if not spotty_auth.zeroconf_authenticated_ok():
            dialog.ok(dialog_title, self.get_zeroconf_authentication_failed_msg(spotty_auth))
            main_service.abort_main_service = True
            utils.kill_this_plugin()
            return

        spotty_auth.renew_token()
        self.refresh_spotipy()

        dialog.ok(dialog_title, self.get_authenticated_success_msg())

    def get_authenticated_success_msg(self) -> str:
        msg = self.__addon.getLocalizedString(AUTHENTICATE_SUCCESS_STR_ID)

        max_str_len = len(max(msg.split("\n"), key=len))
        blanks = " " * (int(max_str_len / 2) - 1)
        msg += f"\n\n{blanks}'{self.__username}'."

        return msg

    def get_zeroconf_program_failed_msg(self, spotty_auth: SpottyAuth) -> str:
        return (
            f"{spotty_auth.get_zeroconf_program_failed_msg()}\n\n"
            f"{self.__addon.getLocalizedString(TERMINATING_SPOTIFY_PLUGIN_STR_ID)}"
        )

    def get_zeroconf_authentication_failed_msg(self, spotty_auth: SpottyAuth) -> str:
        return (
            f"{spotty_auth.get_zeroconf_authentication_failed_msg()}\n\n"
            f"{self.__addon.getLocalizedString(TERMINATING_SPOTIFY_PLUGIN_STR_ID)}"
        )

    def parse_params(self):
        """parse parameters from the plugin entry path"""
        log_msg(f"sys.argv = {str(sys.argv)}")
        self.__params: Dict[str, Any] = urllib.parse.parse_qs(sys.argv[2][1:])

        action = self.__params.get("action", None)
        if action:
            self.__action = action[0].lower()
            log_msg(f"Set action to '{self.__action}'.")

        playlist_id = self.__params.get("playlistid", None)
        if playlist_id:
            self.__playlist_id = playlist_id[0]
        owner_id = self.__params.get("ownerid", None)
        if owner_id:
            self.__owner_id = owner_id[0]
        track_id = self.__params.get("trackid", None)
        if track_id:
            self.__track_id = track_id[0]
        album_id = self.__params.get("albumid", None)
        if album_id:
            self.__album_id = album_id[0]
        artist_id = self.__params.get("artistid", None)
        if artist_id:
            self.__artist_id = artist_id[0]
        artist_name = self.__params.get("artistname", None)
        if artist_name:
            self.__artist_name = artist_name[0]
        offset = self.__params.get("offset", None)
        if offset:
            self.__offset = int(offset[0])
        filt = self.__params.get("applyfilter", None)
        if filt:
            self.__filter = filt[0]

    def _get_action_handler(self, action: str):
        """Return bound method for action name; avoids eval and restricts to callable attributes."""
        if not action or not isinstance(action, str) or action.startswith("_"):
            return None
        meth = getattr(self, action, None)
        return meth if callable(meth) else None

    def __cache_checksum(self, opt_value: Any = None) -> str:
        """simple cache checksum based on a few most important values"""
        result = self.__cached_checksum
        if not result:
            # log_msg("__cached_checksum not found. Getting a new one.")
            saved_tracks = self.__get_saved_track_ids()
            saved_albums = self.__get_saved_album_ids()
            followed_artists = self.__get_followed_artists()
            generic_checksum = self.__addon.getSetting("cache_checksum")
            result = (
                f"{len(saved_tracks)}-{len(saved_albums)}-{len(followed_artists)}"
                f"-{generic_checksum}"
            )
            self.__cached_checksum = result
            # log_msg(f"New __cached_checksum = '{self.__cached_checksum}'.")

        if opt_value:
            result += f"-{opt_value}"

        return result

    def __build_url(self, query: Dict[str, str]) -> str:
        return self.__base_url + "?" + urllib.parse.urlencode(
            [(k, str(v)) for k, v in query.items() if v is not None]
        )

    def delete_cache_db(self) -> None:
        log_msg("Deleting plugin cache...")
        simple_db_cache_addon = xbmcaddon.Addon(ADDON_ID)
        db_path = simple_db_cache_addon.getAddonInfo("profile")
        db_file = xbmcvfs.translatePath(f"{db_path}/simplecache.db")
        try:
            os.remove(db_file)
        except OSError:
            pass
        log_msg(f"Deleted simplecache database file {db_file}.")

        dialog = xbmcgui.Dialog()
        header = self.__addon.getAddonInfo("name")
        msg = self.__addon.getLocalizedString(CACHED_CLEARED_STR_ID)
        dialog.ok(header, msg)

    def refresh_listing(self) -> None:
        self.__addon.setSetting("cache_checksum", time.strftime("%Y%m%d%H%M%S", time.gmtime()))
        log_msg(f"New cache_checksum = '{self.__addon.getSetting('cache_checksum')}'")
        xbmc.executebuiltin("Container.Refresh")

    def refresh_kodi_playlists(self) -> None:
        """
        Legacy hook for native playlist sync.
        No-op now that .m3u playlist generation has been removed.
        """
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __add_track_listitems(self, tracks, append_artist_to_label: bool = False) -> None:
        list_items = self.__get_track_list(tracks, append_artist_to_label)
        xbmcplugin.addDirectoryItems(self.__addon_handle, list_items, totalItems=len(list_items))

    @staticmethod
    def __get_track_name(track, append_artist_to_label: bool) -> str:
        if not append_artist_to_label:
            return track["name"]
        return f"{track['artist']} - {track['name']}"

    @staticmethod
    def __get_track_rating(popularity: int) -> int:
        if not popularity:
            return 0

        return int(math.ceil(popularity * 6 / 100.0)) - 1

    def __get_track_list(
        self, tracks, append_artist_to_label: bool = False
    ) -> List[Tuple[str, xbmcgui.ListItem, bool]]:
        list_items = []
        for count, track in enumerate(tracks):
            list_items.append(self.__get_track_item(track, append_artist_to_label) + (False,))

        return list_items

    def _track_album_description(self, track: Dict[str, Any], album: Dict[str, Any]) -> str:
        """Build album description from Spotify data (release date, label, copyright, genre)."""
        parts = []
        release_date = (album or {}).get("release_date") or ""
        if release_date:
            parts.append("Released %s." % release_date)
        label = (album or {}).get("label")
        if label:
            parts.append("Label: %s." % label)
        copyrights = (album or {}).get("copyrights")
        if copyrights and isinstance(copyrights, list):
            texts = [c.get("text") for c in copyrights if c.get("text")]
            if texts:
                parts.append(" ".join(texts))
        genre = track.get("genre")
        if genre:
            g = genre if isinstance(genre, str) else " / ".join(genre) if genre else ""
            if g:
                parts.append("Genre: %s." % g)
        return " ".join(parts).strip() if parts else ""

    def _track_artist_description(self, track: Dict[str, Any]) -> str:
        """Build artist description from Spotify data (genres, followers). No biography in API."""
        parts = []
        if track.get("artist_genres"):
            genres = track["artist_genres"]
            g = genres if isinstance(genres, str) else ", ".join(genres) if genres else ""
            if g:
                parts.append("Genres: %s." % g)
        elif track.get("genre"):
            g = track["genre"] if isinstance(track["genre"], str) else " / ".join(track["genre"])
            if g:
                parts.append("Genre: %s." % g)
        followers = track.get("artist_followers")
        if followers is not None and followers >= 0:
            if followers >= 1_000_000:
                parts.append("%.1fM followers." % (followers / 1_000_000))
            elif followers >= 1_000:
                parts.append("%.1fK followers." % (followers / 1_000))
            else:
                parts.append("%d followers." % followers)
        return " ".join(parts).strip() if parts else ""

    def _merge_enrichment_into_tracks(self, tracks: List[Dict[str, Any]]) -> None:
        """Merge Window-stored enriched album/artist data into track dicts (mutates in place)."""
        try:
            win = xbmcgui.Window(_HOME_WINDOW_ID)
            alb_json = win.getProperty(_PROP_ENRICHED_ALBUMS)
            art_json = win.getProperty(_PROP_ENRICHED_ARTISTS)
            artist_bios_json = win.getProperty(_PROP_ENRICHED_ARTIST_BIOS)
            album_descs_json = win.getProperty(_PROP_ENRICHED_ALBUM_DESCRIPTIONS)
            if not alb_json and not art_json and not artist_bios_json and not album_descs_json:
                return
            album_data = json.loads(alb_json) if alb_json else {}
            artist_data = json.loads(art_json) if art_json else {}
            artist_bios = json.loads(artist_bios_json) if artist_bios_json else {}
            album_descriptions = json.loads(album_descs_json) if album_descs_json else {}
            for t in tracks:
                aid = (t.get("album") or {}).get("id")
                if aid and aid in album_data:
                    alb = album_data[aid]
                    if not t.get("album"):
                        t["album"] = {}
                    t["album"]["label"] = alb.get("label") or ""
                    t["album"]["copyrights"] = alb.get("copyrights") or []
                if aid and aid in album_descriptions:
                    t["album_description"] = album_descriptions[aid]
                artist_id = t.get("artistid")
                if artist_id and artist_id in artist_data:
                    art = artist_data[artist_id]
                    followers = art.get("followers")
                    genres = art.get("genres") or []
                    t["artist_genres"] = " / ".join(genres) if genres else ""
                    t["artist_followers"] = (
                        followers.get("total", -1) if followers else -1
                    )
                if artist_id and artist_id in artist_bios:
                    t["artist_bio"] = artist_bios[artist_id]
            win.clearProperty(_PROP_ENRICHED_ALBUMS)
            win.clearProperty(_PROP_ENRICHED_ARTISTS)
            win.clearProperty(_PROP_ENRICHED_ARTIST_BIOS)
            win.clearProperty(_PROP_ENRICHED_ALBUM_DESCRIPTIONS)
        except (TypeError, ValueError, RuntimeError):
            pass

    def _start_streaming_enrichment_thread(self) -> None:
        """Start background thread to fetch all pending album/artist data, then refresh container."""
        try:
            win = xbmcgui.Window(_HOME_WINDOW_ID)
            pending_albums = win.getProperty(_PROP_PENDING_ALBUMS)
            pending_artists = win.getProperty(_PROP_PENDING_ARTISTS)
            if not pending_albums and not pending_artists:
                return
            win.clearProperty(_PROP_PENDING_ALBUMS)
            win.clearProperty(_PROP_PENDING_ARTISTS)
            album_ids = [x.strip() for x in pending_albums.split(",") if x.strip()]
            artist_ids = [x.strip() for x in pending_artists.split(",") if x.strip()]
            spotipy_client = self.__spotipy
            market = self.__user_country

            def _run():
                album_data = {}
                artist_data = {}

                def _fetch_albums():
                    for chunk in get_chunks(album_ids, 20):
                        try:
                            for alb in spotipy_client.albums(chunk, market=market).get(
                                "albums", []
                            ):
                                if alb and alb.get("id"):
                                    album_data[alb["id"]] = {
                                        "label": alb.get("label") or "",
                                        "copyrights": alb.get("copyrights") or [],
                                        "name": alb.get("name") or "",
                                        "artists": alb.get("artists") or [],
                                    }
                        except Exception:
                            pass

                def _fetch_artists():
                    for chunk in get_chunks(artist_ids, 50):
                        try:
                            for art in spotipy_client.artists(chunk).get("artists", []):
                                if art and art.get("id"):
                                    artist_data[art["id"]] = {
                                        "followers": art.get("followers"),
                                        "genres": art.get("genres") or [],
                                        "name": art.get("name") or "",
                                    }
                        except Exception:
                            pass

                t_alb = threading.Thread(target=_fetch_albums, daemon=True)
                t_art = threading.Thread(target=_fetch_artists, daemon=True)
                t_alb.start()
                t_art.start()
                t_alb.join()
                t_art.join()
                # Optional: fetch biography/album description from chosen provider
                artist_bios = {}
                album_descriptions = {}
                enable_lookup = self.__addon.getSetting("enable_content_lookup").lower() == "true"
                provider = (self.__addon.getSetting("content_provider") or "").strip().lower()
                if enable_lookup and provider:
                    if provider == PROVIDER_LASTFM:
                        api_key = self.__addon.getSetting("lastfm_api_key") or ""
                        if api_key and fetch_artist_bio_lastfm and fetch_album_description:
                            for aid in album_ids:
                                alb = album_data.get(aid)
                                if alb:
                                    aname = (alb.get("artists") or [{}])[0].get("name") or ""
                                    alname = alb.get("name") or ""
                                    if aname and alname:
                                        desc = fetch_album_description(aname, alname, api_key)
                                        if desc:
                                            album_descriptions[aid] = desc
                                        xbmc.sleep(250)
                            for art_id in artist_ids:
                                art = artist_data.get(art_id)
                                if art:
                                    aname = art.get("name") or ""
                                    if aname:
                                        bio = fetch_artist_bio_lastfm(aname, api_key)
                                        if bio:
                                            artist_bios[art_id] = bio
                                        xbmc.sleep(250)
                    elif provider == PROVIDER_MUSICBRAINZ and fetch_artist_bio_musicbrainz:
                        for art_id in artist_ids:
                            art = artist_data.get(art_id)
                            if art:
                                aname = art.get("name") or ""
                                if aname:
                                    bio = fetch_artist_bio_musicbrainz(aname)
                                    if bio:
                                        artist_bios[art_id] = bio
                                    xbmc.sleep(350)
                try:
                    w = xbmcgui.Window(_HOME_WINDOW_ID)
                    w.setProperty(_PROP_ENRICHED_ALBUMS, json.dumps(album_data))
                    w.setProperty(_PROP_ENRICHED_ARTISTS, json.dumps(artist_data))
                    if artist_bios:
                        w.setProperty(_PROP_ENRICHED_ARTIST_BIOS, json.dumps(artist_bios))
                    if album_descriptions:
                        w.setProperty(_PROP_ENRICHED_ALBUM_DESCRIPTIONS, json.dumps(album_descriptions))
                    xbmc.executebuiltin("Container.Refresh")
                except Exception:
                    pass

            t = threading.Thread(target=_run, daemon=True)
            t.start()
        except (RuntimeError, Exception):
            pass

    def __get_track_item(
        self, track: Dict[str, Any], append_artist_to_label: bool = False
    ) -> Tuple[str, xbmcgui.ListItem]:
        duration_sec = int((track.get("duration_ms") or 0) / 1000)
        label = self.__get_track_name(track, append_artist_to_label)
        title = label if self.append_artist_to_title else track["name"]
        album = track.get("album") or {}
        album_name = album.get("name") or ""

        # Local playback by using proxy on this machine.
        url = f"http://localhost:{PROXY_PORT}/track/{track['id']}/{duration_sec}"

        li = xbmcgui.ListItem(label, offscreen=True)
        li.setProperty("isPlayable", "true")
        info = {
            "title": title,
            "album": album_name,
            "artist": track.get("artist") or "",
            "duration": duration_sec,
            "year": int(track.get("year") or 0),
            "tracknumber": int(track.get("track_number") or 0),
            "discnumber": int(track.get("disc_number") or 1),
            "rating": int(track.get("rating") or 0),
        }
        genre = track.get("genre")
        if genre is not None:
            info["genre"] = genre if isinstance(genre, str) else " / ".join(genre) if genre else ""
        if album.get("album_type") == "compilation":
            info["albumartist"] = ["Various Artists"]
        li.setInfo("music", info)
        # Fill "Additional song info" / context boxes (Album description / Artist biography)
        # Prefer external metadata (e.g. Last.fm) when present; else Spotify-only summary
        album_desc = track.get("album_description") or self._track_album_description(track, album)
        artist_desc = track.get("artist_bio") or self._track_artist_description(track)
        if album_desc:
            li.setProperty("Album_Description", album_desc)
        if artist_desc:
            li.setProperty("Artist_Description", artist_desc)
        li.setArt(_art_for_item(track.get("thumb") or "", "DefaultMusicSongs.png"))
        li.setProperty("spotifytrackid", track["id"])
        li.setContentLookup(False)
        li.addContextMenuItems(track.get("contextitems") or [], True)
        li.setProperty("do_not_analyze", "true")
        li.setMimeType("audio/wave")

        return url, li

    def __browse_main(self) -> None:
        # Main listing.
        xbmcplugin.setContent(self.__addon_handle, "files")

        items = [
            (
                self.__addon.getLocalizedString(MY_MUSIC_FOLDER_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_main_library.__name__}",
                MUSIC_LIBRARY_ICON,
                True,
            ),
            (
                self.__addon.getLocalizedString(EXPLORE_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_main_explore.__name__}",
                MUSIC_EXPLORE_ICON,
                True,
            ),
            (
                xbmc.getLocalizedString(KODI_SEARCH_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.search.__name__}",
                MUSIC_SEARCH_ICON,
                True,
            ),
            (
                self.__addon.getLocalizedString(AUTHENTICATE_PLUGIN_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.authenticate_plugin_request.__name__}",
                CLEAR_CACHE_ICON,
                False,
            ),
            (
                self.__addon.getLocalizedString(CLEAR_CACHE_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.delete_cache_db.__name__}",
                CLEAR_CACHE_ICON,
                False,
            ),
            (
                self.__addon.getLocalizedString(REFRESH_KODI_PLAYLISTS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.refresh_kodi_playlists.__name__}",
                MUSIC_PLAYLISTS_ICON,
                False,
            ),
        ]

        for item in items:
            li = xbmcgui.ListItem(item[0], path=item[1])
            li.setProperty("IsPlayable", "false")
            li.setArt({"icon": os.path.join(self.__addon_icon_path, item[2])})
            li.addContextMenuItems([], True)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=item[1], listitem=li, isFolder=item[3]
            )

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

        log_msg("Finished setting up main menu.")

    def browse_main_library(self) -> None:
        # Library nodes.
        xbmcplugin.setContent(self.__addon_handle, "files")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(MY_MUSIC_FOLDER_STR_ID),
        )

        items = [
            (
                xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID),
                f"plugin://{ADDON_ID}/"
                f"?action={self.browse_playlists.__name__}&ownerid={self.__userid}",
                MUSIC_PLAYLISTS_ICON,
            ),
            (
                xbmc.getLocalizedString(KODI_ALBUMS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_saved_albums.__name__}",
                MUSIC_ALBUMS_ICON,
            ),
            (
                xbmc.getLocalizedString(KODI_SONGS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_saved_tracks.__name__}",
                MUSIC_SONGS_ICON,
            ),
            (
                xbmc.getLocalizedString(KODI_ARTISTS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_saved_artists.__name__}",
                MUSIC_ARTISTS_ICON,
            ),
            (
                self.__addon.getLocalizedString(FOLLOWED_ARTISTS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_followed_artists.__name__}",
                MUSIC_ARTISTS_ICON,
            ),
            (
                self.__addon.getLocalizedString(MOST_PLAYED_ARTISTS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_top_artists.__name__}",
                MUSIC_TOP_ARTISTS_ICON,
            ),
            (
                self.__addon.getLocalizedString(MOST_PLAYED_TRACKS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_top_tracks.__name__}",
                MUSIC_TOP_TRACKS_ICON,
            ),
        ]

        for item in items:
            li = xbmcgui.ListItem(item[0], path=item[1])
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.setArt({"icon": os.path.join(self.__addon_icon_path, item[2])})
            li.addContextMenuItems([], True)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=item[1], listitem=li, isFolder=True
            )

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def browse_top_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        result = self.__spotipy.current_user_top_artists(limit=20, offset=0)

        cache_str = f"spotify.topartists.{self.__userid}"
        checksum = self.__cache_checksum(result["total"])
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached top artists for user "{self.__userid}".')
        else:
            count = len(result["items"])
            while result["total"] > count:
                result["items"] += self.__spotipy.current_user_top_artists(limit=20, offset=count)[
                    "items"
                ]
                count += 20
            artists = self.__prepare_artist_listitems(result["items"])
            self.cache.set(cache_str, artists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED top artists for user "{self.__userid}".'
            )
        self.__add_artist_listitems(artists)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def browse_top_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        results = self.__spotipy.current_user_top_tracks(limit=20, offset=0)

        cache_str = f"spotify.toptracks.{self.__userid}"
        checksum = self.__cache_checksum(results["total"])
        tracks = self.cache.get(cache_str, checksum=checksum)
        if tracks:
            cache_log(f'Retrieved {len(tracks)} cached top tracks for user "{self.__userid}".')
        else:
            tracks = results["items"]
            while results["next"]:
                results = self.__spotipy.next(results)
                tracks.extend(results["items"])
            tracks = self.__prepare_track_listitems(tracks=tracks)
            self.cache.set(cache_str, tracks, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(tracks)} UNCACHED top tracks for user "{self.__userid}".'
            )
        self._merge_enrichment_into_tracks(tracks)
        self.__add_track_listitems(tracks, True)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self._start_streaming_enrichment_thread()

    def __get_explore_categories(self) -> List[Tuple[Any, str, Union[str, Any]]]:
        items = []

        categories = self.__spotipy.categories(
            country=self.__user_country, limit=50, locale=self.__user_country
        )
        count = len(categories["categories"]["items"])
        while categories["categories"]["total"] > count:
            categories["categories"]["items"] += self.__spotipy.categories(
                country=self.__user_country, limit=50, offset=count, locale=self.__user_country
            )["categories"]["items"]
            count += 50

        for item in categories["categories"]["items"]:
            thumb = "DefaultMusicGenre.png"
            for icon in item["icons"]:
                thumb = icon["url"]
                break
            items.append(
                (
                    item["name"],
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.browse_category.__name__}&applyfilter={item['id']}",
                    thumb,
                )
            )

        return items

    def browse_main_explore(self) -> None:
        # Explore nodes.
        xbmcplugin.setContent(self.__addon_handle, "files")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", self.__addon.getLocalizedString(EXPLORE_STR_ID)
        )
        items = [
            (
                self.__addon.getLocalizedString(FEATURED_PLAYLISTS_STR_ID),
                f"plugin://{ADDON_ID}/"
                f"?action={self.browse_playlists.__name__}&applyfilter=featured",
                MUSIC_PLAYLISTS_ICON,
            ),
            (
                self.__addon.getLocalizedString(ALL_NEW_RELEASES_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_new_releases.__name__}",
                MUSIC_ALBUMS_ICON,
            ),
        ]

        # Add categories.
        items += self.__get_explore_categories()
        for item in items:
            li = xbmcgui.ListItem(item[0], path=item[1])
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.setArt({"icon": os.path.join(self.__addon_icon_path, item[2])})
            li.addContextMenuItems([], True)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=item[1], listitem=li, isFolder=True
            )

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_album_tracks(self, album: Dict[str, Any]) -> List[Dict[str, Any]]:
        cache_str = f"spotify.albumtracks{album['id']}"
        checksum = self.__cache_checksum()

        album_tracks = self.cache.get(cache_str, checksum=checksum)
        if album_tracks:
            cache_log(
                f'Retrieved {album["tracks"]["total"]} cached tracks for album "{album["name"]}".'
            )
        else:
            track_ids = []
            count = 0
            while album["tracks"]["total"] > count:
                tracks = self.__spotipy.album_tracks(
                    album["id"], market=self.__user_country, limit=50, offset=count
                )["items"]
                for track in tracks:
                    track_ids.append(track["id"])
                count += 50
            album_tracks = self.__prepare_track_listitems(track_ids, album_details=album)
            self.cache.set(cache_str, album_tracks, checksum=checksum)
            cache_log(
                f'Retrieved {album["tracks"]["total"]} UNCACHED tracks for album "{album["name"]}".'
            )

        return album_tracks

    def browse_album(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        album = self.__spotipy.album(self.__album_id, market=self.__user_country)
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", album["name"])
        tracks = self.__get_album_tracks(album)
        self._merge_enrichment_into_tracks(tracks)
        if album.get("album_type") == "compilation":
            self.__add_track_listitems(tracks, True)
        else:
            self.__add_track_listitems(tracks)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TRACKNUM)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ARTIST)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self._start_streaming_enrichment_thread()

    def artist_top_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
        )
        tracks = self.__spotipy.artist_top_tracks(self.__artist_id, country=self.__user_country)
        tracks = self.__prepare_track_listitems(tracks=tracks["tracks"])
        self._merge_enrichment_into_tracks(tracks)
        self.__add_track_listitems(tracks)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TRACKNUM)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self._start_streaming_enrichment_thread()

    def related_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
        )
        cache_str = f"spotify.relatedartists.{self.__artist_id}"
        checksum = self.__cache_checksum()
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached related artists for "{self.__artist_id}".')
        else:
            artists = self.__spotipy.artist_related_artists(self.__artist_id)
            artists = self.__prepare_artist_listitems(artists["artists"])
            self.cache.set(cache_str, artists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED related artists for "{self.__artist_id}".'
            )
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_playlist_details(self, playlist_id: str) -> Playlist:
        playlist = self.__spotipy.playlist(
            playlist_id, fields="tracks(total),name,owner(id),id", market=self.__user_country
        )
        # Get from cache first.
        cache_str = f"spotify.playlistdetails.{playlist['id']}"
        checksum = self.__cache_checksum(playlist["tracks"]["total"])
        # log_msg(f"Playlist cache_str = '{cache_str}', checksum = '{checksum}'.")
        playlist_details = self.cache.get(cache_str, checksum=checksum)
        if playlist_details:
            cache_log(
                f'Retrieved {playlist["tracks"]["total"]} cached playlist details'
                f' for "{playlist["name"]}".'
            )
        else:
            # Get listing from api.
            count = 0
            playlist_details = playlist
            playlist_details["tracks"]["items"] = []
            while playlist["tracks"]["total"] > count:
                playlist_details["tracks"]["items"] += self.__spotipy.playlist_items(
                    playlist["id"],
                    market=self.__user_country,
                    fields="",
                    limit=50,
                    offset=count,
                )["items"]
                count += 50
            playlist_details["tracks"]["items"] = self.__prepare_track_listitems(
                tracks=playlist_details["tracks"]["items"], playlist_details=playlist
            )
            # log_msg(f"playlist_details = {playlist_details}")
            checksum = self.__cache_checksum(playlist["tracks"]["total"])
            self.cache.set(cache_str, playlist_details, checksum=checksum)
            # log_msg(f"Got new playlist - checksum = '{checksum}'")
            cache_log(
                f'Retrieved {playlist["tracks"]["total"]} UNCACHED playlist details'
                f' for "{playlist["name"]}".'
            )

        return playlist_details

    def browse_playlist(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        playlist_details = self.__get_playlist_details(self.__playlist_id)
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", playlist_details["name"])
        items = playlist_details["tracks"]["items"]
        self._merge_enrichment_into_tracks(items)
        self.__add_track_listitems(items, True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self._start_streaming_enrichment_thread()

    def play_playlist(self) -> None:
        """Play entire playlist: start first track immediately, queue rest in background."""
        playlist_details = self.__get_playlist_details(self.__playlist_id)
        items = playlist_details["tracks"]["items"]
        if not items:
            return
        log_msg(f"Start playing playlist '{playlist_details['name']}'.")

        kodi_playlist = xbmc.PlayList(0)
        kodi_playlist.clear()

        url, li = self.__get_track_item(items[0], True)
        kodi_playlist.add(url, li)
        xbmc.Player().play(kodi_playlist)

        def add_remaining():
            for track in items[1:]:
                if xbmc.Monitor().abortRequested():
                    return
                try:
                    u, listitem = self.__get_track_item(track, True)
                    kodi_playlist.add(u, listitem)
                except Exception:
                    pass
                xbmc.sleep(10)

        t = threading.Thread(target=add_remaining, daemon=True)
        t.start()

    def __get_category(self, categoryid: str) -> Playlist:
        category = self.__spotipy.category(
            categoryid, country=self.__user_country, locale=self.__user_country
        )
        playlists = self.__spotipy.category_playlists(
            categoryid, country=self.__user_country, limit=50, offset=0
        )
        playlists["category"] = category["name"]
        count = len(playlists["playlists"]["items"])
        while playlists["playlists"]["total"] > count:
            playlists["playlists"]["items"] += self.__spotipy.category_playlists(
                categoryid, country=self.__user_country, limit=50, offset=count
            )["playlists"]["items"]
            count += 50
        playlists["playlists"]["items"] = self.__prepare_playlist_listitems(
            playlists["playlists"]["items"]
        )

        return playlists

    def browse_category(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        playlists = self.__get_category(self.__filter)
        self.__add_playlist_listitems(playlists["playlists"]["items"])
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", playlists["category"])
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def follow_playlist(self) -> None:
        self.__spotipy.current_user_follow_playlist(self.__playlist_id)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.refresh_listing()

    def add_track_to_playlist(self) -> None:
        xbmc.executebuiltin("ActivateWindow(busydialog)")

        if not self.__track_id and xbmc.getInfoLabel("MusicPlayer.(1).Property(spotifytrackid)"):
            self.__track_id = xbmc.getInfoLabel("MusicPlayer.(1).Property(spotifytrackid)")

        own_playlists, own_playlist_names = utils.get_user_playlists(self.__spotipy, 50)
        own_playlist_names.append(xbmc.getLocalizedString(KODI_NEW_PLAYLIST_STR_ID))

        xbmc.executebuiltin("Dialog.Close(busydialog)")
        select = xbmcgui.Dialog().select(
            xbmc.getLocalizedString(KODI_SELECT_PLAYLIST_STR_ID), own_playlist_names
        )
        if select != -1 and own_playlist_names[select] == xbmc.getLocalizedString(
            KODI_NEW_PLAYLIST_STR_ID
        ):
            # create new playlist...
            kb = xbmc.Keyboard("", xbmc.getLocalizedString(KODI_ENTER_NEW_PLAYLIST_STR_ID))
            kb.setHiddenInput(False)
            kb.doModal()
            if kb.isConfirmed():
                name = kb.getText()
                playlist = self.__spotipy.user_playlist_create(self.__userid, name, False)
                self.__spotipy.playlist_add_items(playlist["id"], [self.__track_id])
        elif select != -1:
            playlist = own_playlists[select]
            self.__spotipy.playlist_add_items(playlist["id"], [self.__track_id])

    def remove_track_from_playlist(self) -> None:
        self.__spotipy.playlist_remove_all_occurrences_of_items(
            self.__playlist_id, [self.__track_id]
        )
        self.refresh_listing()

    def unfollow_playlist(self) -> None:
        self.__spotipy.current_user_unfollow_playlist(self.__playlist_id)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.refresh_listing()

    def follow_artist(self) -> None:
        self.__spotipy.user_follow_artists([self.__artist_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.refresh_listing()

    def unfollow_artist(self) -> None:
        self.__spotipy.user_unfollow_artists([self.__artist_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.refresh_listing()

    def save_album(self) -> None:
        self.__spotipy.current_user_saved_albums_add([self.__album_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.refresh_listing()

    def remove_album(self) -> None:
        self.__spotipy.current_user_saved_albums_delete([self.__album_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.refresh_listing()

    def save_track(self) -> None:
        self.__spotipy.current_user_saved_tracks_add([self.__track_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.refresh_listing()

    def remove_track(self) -> None:
        self.__spotipy.current_user_saved_tracks_delete([self.__track_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.refresh_listing()

    def __get_featured_playlists(self) -> Playlist:
        playlists = self.__spotipy.featured_playlists(
            country=self.__user_country, limit=50, offset=0
        )
        count = len(playlists["playlists"]["items"])
        total = playlists["playlists"]["total"]
        while total > count:
            playlists["playlists"]["items"] += self.__spotipy.featured_playlists(
                country=self.__user_country, limit=50, offset=count
            )["playlists"]["items"]
            count += 50
        playlists["playlists"]["items"] = self.__prepare_playlist_listitems(
            playlists["playlists"]["items"]
        )

        return playlists

    def __get_user_playlists(self, userid):
        playlists = self.__spotipy.user_playlists(userid, limit=1, offset=0)
        count = len(playlists["items"])
        total = playlists["total"]
        cache_str = f"spotify.userplaylists.{userid}"
        checksum = self.__cache_checksum(total)

        cached_playlists = self.cache.get(cache_str, checksum=checksum)
        if cached_playlists:
            playlists = cached_playlists
            cache_log(f'Retrieved {len(playlists)} cached playlists for user "{self.__userid}".')
        else:
            while total > count:
                playlists["items"] += self.__spotipy.user_playlists(userid, limit=50, offset=count)[
                    "items"
                ]
                count += 50
            playlists = self.__prepare_playlist_listitems(playlists["items"])
            self.cache.set(cache_str, playlists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(playlists)} UNCACHED playlists for user "{self.__userid}".'
            )

        return playlists

    def __get_curuser_playlistids(self) -> List[str]:
        playlists = self.__spotipy.current_user_playlists(limit=1, offset=0)
        count = len(playlists["items"])
        total = playlists["total"]
        cache_str = f"spotify.userplaylistids.{self.__userid}"
        playlist_ids = self.cache.get(cache_str, checksum=total)
        if playlist_ids:
            log_msg(
                f'Retrieved {len(playlist_ids)} cached playlist ids for user "{self.__userid}".'
            )
        else:
            playlist_ids = []
            while total > count:
                playlists["items"] += self.__spotipy.current_user_playlists(limit=50, offset=count)[
                    "items"
                ]
                count += 50
            for playlist in playlists["items"]:
                playlist_ids.append(playlist["id"])
            self.cache.set(cache_str, playlist_ids, checksum=total)
            cache_log(
                f'Retrieved {_get_len(playlist_ids)} UNCACHED playlist ids for user "{self.__userid}".'
            )
        return playlist_ids

    def browse_playlists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        if self.__filter == "featured":
            playlists = self.__get_featured_playlists()
            xbmcplugin.setProperty(self.__addon_handle, "FolderName", playlists["message"])
            playlists = playlists["playlists"]["items"]
        else:
            xbmcplugin.setProperty(
                self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID)
            )
            playlists = self.__get_user_playlists(self.__owner_id)

        self.__add_playlist_listitems(playlists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_new_releases(self):
        albums = self.__spotipy.new_releases(country=self.__user_country, limit=50, offset=0)
        count = len(albums["albums"]["items"])
        while albums["albums"]["total"] > count:
            albums["albums"]["items"] += self.__spotipy.new_releases(
                country=self.__user_country, limit=50, offset=count
            )["albums"]["items"]
            count += 50

        album_ids = []
        for album in albums["albums"]["items"]:
            album_ids.append(album["id"])
        albums = self.__prepare_album_listitems(album_ids)

        return albums

    def browse_new_releases(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(ALL_NEW_RELEASES_STR_ID),
        )
        albums = self.__get_new_releases()
        self.__add_album_listitems(albums)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __prepare_track_listitems(
        self, track_ids=None, tracks=None, playlist_details=None, album_details=None
    ) -> List[Dict[str, Any]]:
        if tracks is None:
            tracks = []
        if track_ids is None:
            track_ids = []

        new_tracks: List[Dict[str, Any]] = []

        # Fetch saved_track_ids and followed_artists in parallel (with track fetch when needed)
        saved_result = [None]
        followed_result = [None]

        def _get_saved():
            saved_result[0] = self.__get_saved_track_ids()

        def _get_followed():
            followed_result[0] = self.__get_followed_artists()

        t_saved = threading.Thread(target=_get_saved, daemon=True)
        t_followed = threading.Thread(target=_get_followed, daemon=True)
        t_saved.start()
        t_followed.start()

        # For tracks, we always get the full details unless full tracks already supplied.
        if track_ids and not tracks:
            for chunk in get_chunks(track_ids, 20):
                tracks += self.__spotipy.tracks(chunk, market=self.__user_country)["tracks"]

        t_saved.join()
        t_followed.join()
        saved_track_ids = saved_result[0] or []
        followed_artists = [a["id"] for a in (followed_result[0] or [])]

        album_ids_to_fetch = set()
        artist_ids_to_fetch = set()
        for track in tracks:
            if track.get("track"):
                track = track["track"]
            if album_details:
                track["album"] = album_details
            if not track.get("album"):
                track["album"] = {"name": "", "images": [], "album_type": ""}
            if track.get("images"):
                thumb = track["images"][0]["url"]
            elif track.get("album", {}).get("images"):
                thumb = track["album"]["images"][0]["url"]
            else:
                thumb = "DefaultMusicSongs.png"
            track["thumb"] = thumb
            track["track_number"] = track.get("track_number") or 0
            track["disc_number"] = track.get("disc_number") or 1

            # Skip local tracks in playlists.
            if not track.get("id"):
                continue

            if "artists" in track:
                artists = []
                for artist in track["artists"]:
                    if artist["name"]:
                        artists.append(artist["name"])
                if artists:
                    track["artist"] = " / ".join(artists)
                    track["artistid"] = track["artists"][0]["id"]

            if "album" not in track:
                track["genre"] = []
                track["year"] = 1900
            else:
                track["genre"] = " / ".join(track["album"].get("genres", []))

                # Allow for 'release_date' being empty.
                release_date = (
                    "0" if "album" not in track else track["album"].get("release_date", "0")
                )
                track["year"] = (
                    1900
                    if not release_date
                    else int(track["album"].get("release_date", "0").split("-")[0])
                )

            track["rating"] = int(self.__get_track_rating(int(track.get("popularity", "0"))))

            if playlist_details:
                track["playlistid"] = playlist_details["id"]

            track["contextitems"] = self.__get_playlist_track_context_menu_items(
                track, saved_track_ids, playlist_details, followed_artists
            )

            album_id = (track.get("album") or {}).get("id")
            if album_id and not (track.get("album") or {}).get("label"):
                album_ids_to_fetch.add(album_id)
            artist_id = track.get("artistid")
            if artist_id and "artist_genres" not in track:
                artist_ids_to_fetch.add(artist_id)

            new_tracks.append(track)

        # Streaming enrichment: if we already have enriched data (from a background refresh), merge and return.
        try:
            win = xbmcgui.Window(_HOME_WINDOW_ID)
            if win.getProperty(_PROP_ENRICHED_ALBUMS) or win.getProperty(_PROP_ENRICHED_ARTISTS):
                self._merge_enrichment_into_tracks(new_tracks)
                return new_tracks
        except RuntimeError:
            pass

        # Otherwise: optional (setting). If fetch_extra, set Pending so caller starts background thread (no cap).
        fetch_extra = self.__addon.getSetting("fetch_extra_song_info").lower() == "true"
        if fetch_extra and (album_ids_to_fetch or artist_ids_to_fetch):
            try:
                win = xbmcgui.Window(_HOME_WINDOW_ID)
                win.setProperty(_PROP_PENDING_ALBUMS, ",".join(album_ids_to_fetch))
                win.setProperty(_PROP_PENDING_ARTISTS, ",".join(artist_ids_to_fetch))
            except RuntimeError:
                pass

        return new_tracks

    def __get_playlist_track_context_menu_items(
        self, track, saved_track_ids, playlist_details, followed_artists: List[str]
    ) -> List[Tuple[str, str]]:
        # Use original track id for actions when the track was relinked.
        if track.get("linked_from"):
            real_track_id = track["linked_from"]["id"]
            real_track_uri = track["linked_from"]["uri"]
        else:
            real_track_id = track["id"]
            real_track_uri = track["uri"]

        context_items = [
            (
                self.__addon.getLocalizedString(REFRESH_LISTING_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/" f"?action={self.refresh_listing.__name__})",
            )
        ]

        if track["id"] in saved_track_ids:
            context_items.append(
                (
                    self.__addon.getLocalizedString(REMOVE_TRACKS_FROM_MY_MUSIC_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.remove_track.__name__}&trackid={real_track_id})",
                )
            )
        else:
            context_items.append(
                (
                    self.__addon.getLocalizedString(SAVE_TRACKS_TO_MY_MUSIC_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.save_track.__name__}&trackid={real_track_id})",
                )
            )

        if playlist_details and playlist_details["owner"]["id"] == self.__userid:
            context_items.append(
                (
                    f"{self.__addon.getLocalizedString(REMOVE_FROM_PLAYLIST_STR_ID)}"
                    f" {playlist_details['name']}",
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.remove_track_from_playlist.__name__}&trackid="
                    f"{real_track_uri}&playlistid={playlist_details['id']})",
                )
            )

        context_items.append(
            (
                xbmc.getLocalizedString(KODI_ADD_TO_PLAYLIST_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/"
                f"?action={self.add_track_to_playlist.__name__}&trackid={real_track_uri})",
            )
        )

        if "artistid" in track:
            context_items.append(
                (
                    self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.artist_top_tracks.__name__}&artistid={track['artistid']})",
                )
            )
            context_items.append(
                (
                    self.__addon.getLocalizedString(ALL_ALBUMS_FOR_ARTIST_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.browse_artist_just_albums.__name__}"
                    f"&artistid={track['artistid']})",
                )
            )
            context_items.append(
                (
                    self.__addon.getLocalizedString(ALL_SINGLES_FOR_ARTIST_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.browse_artist_just_singles.__name__}"
                    f"&artistid={track['artistid']})",
                )
            )
            context_items.append(
                (
                    self.__addon.getLocalizedString(ALL_APPEARS_ON_FOR_ARTIST_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.browse_artist_just_appears_on.__name__}"
                    f"&artistid={track['artistid']})",
                )
            )
            context_items.append(
                (
                    self.__addon.getLocalizedString(EVERYTHING_FOR_ARTIST_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.browse_artist_everything.__name__}"
                    f"&artistid={track['artistid']})",
                )
            )

            if track["artistid"] in followed_artists:
                context_items.append(
                    (
                        self.__addon.getLocalizedString(UNFOLLOW_ARTIST_STR_ID),
                        f"RunPlugin(plugin://{ADDON_ID}/"
                        f"?action={self.unfollow_artist.__name__}"
                        f"&artistid={track['artistid']})",
                    )
                )
            else:
                context_items.append(
                    (
                        self.__addon.getLocalizedString(FOLLOW_ARTIST_STR_ID),
                        f"RunPlugin(plugin://{ADDON_ID}/"
                        f"?action={self.follow_artist.__name__}&artistid={track['artistid']})",
                    )
                )

            context_items.append(
                (
                    self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.related_artists.__name__}&artistid={track['artistid']})",
                )
            )

        return context_items

    def __prepare_album_listitems(
        self, album_ids: List[str] = None, albums: List[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        if albums is None:
            albums: List[Dict[str, Any]] = []
        if album_ids is None:
            album_ids = []
        if not albums and album_ids:
            # Get full info in chunks of 20.
            for chunk in get_chunks(album_ids, 20):
                albums += self.__spotipy.albums(chunk, market=self.__user_country)["albums"]

        saved_albums = self.__get_saved_album_ids()

        # process listing
        for track in albums:
            if track.get("images"):
                track["thumb"] = track["images"][0]["url"]
            else:
                track["thumb"] = "DefaultMusicAlbums.png"

            track["url"] = self.__build_url(
                {"action": self.browse_album.__name__, "albumid": track["id"]}
            )

            artists = []
            for artist in track["artists"]:
                artists.append(artist["name"])
            track["artist"] = " / ".join(artists)
            track["genre"] = " / ".join(track["genres"])
            track["year"] = int(track["release_date"].split("-")[0])
            track["rating"] = str(self.__get_track_rating(track["popularity"]))
            track["artistid"] = track["artists"][0]["id"]

            track["contextitems"] = self.__get_album_track_context_menu_items(track, saved_albums)

        return albums

    def __get_album_track_context_menu_items(
        self, track, saved_albums: List[str]
    ) -> List[Tuple[str, str]]:
        context_items = [
            (
                self.__addon.getLocalizedString(REFRESH_LISTING_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/" f"?action={self.refresh_listing.__name__})",
            ),
            (
                xbmc.getLocalizedString(KODI_BROWSE_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_album.__name__}&albumid={track['id']})",
            ),
            (
                self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.artist_top_tracks.__name__}&artistid={track['artistid']})",
            ),
            (
                self.__addon.getLocalizedString(EVERYTHING_FOR_ARTIST_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_artist_everything.__name__}&artistid={track['artistid']})",
            ),
            (
                self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.related_artists.__name__}&artistid={track['artistid']})",
            ),
        ]

        if track["id"] in saved_albums:
            context_items.append(
                (
                    self.__addon.getLocalizedString(REMOVE_TRACKS_FROM_MY_MUSIC_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.remove_album.__name__}&albumid={track['id']})",
                )
            )
        else:
            context_items.append(
                (
                    self.__addon.getLocalizedString(SAVE_TRACKS_TO_MY_MUSIC_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.save_album.__name__}&albumid={track['id']})",
                )
            )

        return context_items

    def __add_album_listitems(
        self, albums: List[Dict[str, Any]], append_artist_to_label: bool = False
    ) -> None:
        default_album_icon = os.path.join(self.__addon_icon_path, MUSIC_ALBUMS_ICON)
        for track in albums:
            label = self.__get_track_name(track, append_artist_to_label)
            li = xbmcgui.ListItem(label, path=track["url"], offscreen=True)
            info_labels = {
                "title": track["name"],
                "album": track["name"],
                "artist": track.get("artist") or "",
                "genre": track.get("genre") or "",
                "year": int(track.get("year") or 0),
                "rating": int(track.get("rating") or 0),
            }
            li.setInfo("music", info_labels)
            li.setArt(_art_for_item(track.get("thumb") or "", default_album_icon))
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.addContextMenuItems(track.get("contextitems") or [], True)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=track["url"], listitem=li, isFolder=True
            )

    def __prepare_artist_listitems(
        self, artists: List[Dict[str, Any]], is_followed: bool = False
    ) -> List[Dict[str, Any]]:
        followed_artists = []
        if not is_followed:
            for artist in self.__get_followed_artists():
                followed_artists.append(artist["id"])

        artists = [a for a in artists if a]
        for artist in artists:
            if artist.get("artist"):
                artist = artist["artist"]
            if artist.get("images"):
                artist["thumb"] = artist["images"][0]["url"]
            else:
                artist["thumb"] = "DefaultMusicArtists.png"

            artist["url"] = self.__build_url(
                {
                    "action": self.browse_artist_just_albums_and_singles.__name__,
                    "artistid": artist["id"],
                }
            )

            artist["genre"] = " / ".join(artist["genres"])
            artist["rating"] = str(self.__get_track_rating(artist["popularity"]))
            artist["followerslabel"] = f"{artist['followers']['total']} followers"

            artist["contextitems"] = self.__get_artist_context_menu_items(
                artist, is_followed, followed_artists
            )

        return artists

    def __get_artist_context_menu_items(
        self, artist, is_followed: bool, followed_artists: List[str]
    ) -> List[Tuple[str, str]]:
        context_items = [
            (
                xbmc.getLocalizedString(ALL_ALBUMS_AND_SINGLES_FOR_ARTIST_STR_ID),
                f"Container.Update({artist['url']})",
            ),
            (
                self.__addon.getLocalizedString(ALL_ALBUMS_FOR_ARTIST_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_artist_just_albums.__name__}&artistid={artist['id']})",
            ),
            (
                self.__addon.getLocalizedString(ALL_SINGLES_FOR_ARTIST_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_artist_just_singles.__name__}&artistid={artist['id']})",
            ),
            (
                self.__addon.getLocalizedString(ALL_APPEARS_ON_FOR_ARTIST_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_artist_just_appears_on.__name__}&artistid={artist['id']})",
            ),
            (
                self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.artist_top_tracks.__name__}&artistid={artist['id']})",
            ),
        ]

        if is_followed or artist["id"] in followed_artists:
            context_items.append(
                (
                    self.__addon.getLocalizedString(UNFOLLOW_ARTIST_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.unfollow_artist.__name__}&artistid={artist['id']})",
                )
            )
        else:
            context_items.append(
                (
                    self.__addon.getLocalizedString(FOLLOW_ARTIST_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.follow_artist.__name__}&artistid={artist['id']})",
                )
            )

        context_items.append(
            (
                self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.related_artists.__name__}&artistid={artist['id']})",
            )
        )

        return context_items

    def __add_artist_listitems(self, artists: List[Dict[str, Any]]) -> None:
        default_artist_icon = os.path.join(self.__addon_icon_path, MUSIC_ARTISTS_ICON)
        for item in artists:
            li = xbmcgui.ListItem(item["name"], path=item["url"], offscreen=True)
            info_labels = {
                "title": item["name"],
                "artist": item["name"],
                "genre": item.get("genre") or "",
                "rating": int(item.get("rating") or 0),
            }
            li.setInfo("music", info_labels)
            li.setArt(_art_for_item(item.get("thumb") or "", default_artist_icon))
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.setLabel2(item.get("followerslabel") or "")
            li.addContextMenuItems(item.get("contextitems") or [], True)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle,
                url=item["url"],
                listitem=li,
                isFolder=True,
                totalItems=len(artists),
            )

    def __prepare_playlist_listitems(self, playlists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        playlists2 = []
        followed_playlists = self.__get_curuser_playlistids()

        for playlist in playlists:
            if not playlist:
                continue

            if playlist.get("images"):
                playlist["thumb"] = playlist["images"][0]["url"]
            else:
                playlist["thumb"] = "DefaultMusicAlbums.png"

            playlist["url"] = self.__build_url(
                {
                    "action": self.browse_playlist.__name__,
                    "playlistid": playlist["id"],
                    "ownerid": playlist["owner"]["id"],
                }
            )

            playlist["contextitems"] = self.__get_playlist_context_menu_items(
                playlist, followed_playlists
            )

            playlists2.append(playlist)

        return playlists2

    def __get_playlist_context_menu_items(
        self, playlist, followed_playlists: List[str]
    ) -> List[Tuple[str, str]]:
        contextitems = [
            (
                xbmc.getLocalizedString(KODI_PLAY_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/"
                f"?action={self.play_playlist.__name__}&playlistid={playlist['id']}"
                f"&ownerid={playlist['owner']['id']})",
            ),
            (
                self.__addon.getLocalizedString(REFRESH_LISTING_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/" f"?action={self.refresh_listing.__name__})",
            ),
        ]

        if playlist["owner"]["id"] != self.__userid and playlist["id"] in followed_playlists:
            contextitems.append(
                (
                    self.__addon.getLocalizedString(UNFOLLOW_PLAYLIST_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.unfollow_playlist.__name__}&playlistid={playlist['id']}"
                    f"&ownerid={playlist['owner']['id']})",
                )
            )
        elif playlist["owner"]["id"] != self.__userid:
            contextitems.append(
                (
                    self.__addon.getLocalizedString(FOLLOW_PLAYLIST_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.follow_playlist.__name__}&playlistid={playlist['id']}"
                    f"&ownerid={playlist['owner']['id']})",
                )
            )

        return contextitems

    def __add_playlist_listitems(self, playlists: List[Dict[str, Any]]) -> None:
        default_playlist_icon = os.path.join(self.__addon_icon_path, MUSIC_PLAYLISTS_ICON)
        addon_fanart = os.path.join(self.__addon_icon_path, "fanart.jpg")
        for item in playlists:
            li = xbmcgui.ListItem(item["name"], path=item["url"], offscreen=True)
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.addContextMenuItems(item.get("contextitems") or [], True)
            art = _art_for_item(item.get("thumb") or "", default_playlist_icon)
            art["fanart"] = art.get("fanart") or addon_fanart
            li.setArt(art)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=item["url"], listitem=li, isFolder=True
            )

    def browse_artist_everything(self) -> None:
        self.browse_artist_albums(album_type="album,single,appears_on,compilation")

    def browse_artist_just_albums(self) -> None:
        self.browse_artist_albums(album_type="album,compilation")

    def browse_artist_just_singles(self) -> None:
        self.browse_artist_albums(album_type="single")

    def browse_artist_just_albums_and_singles(self) -> None:
        self.browse_artist_albums(album_type="album,single")

    def browse_artist_just_compilations(self) -> None:
        self.browse_artist_albums(album_type="compilation")

    def browse_artist_just_appears_on(self) -> None:
        self.browse_artist_albums(album_type="appears_on")

    def browse_artist_albums(self, album_type: str) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)
        )
        artist_albums = self.__spotipy.artist_albums(
            self.__artist_id,
            album_type=album_type,
            country=self.__user_country,
            limit=50,
            offset=0,
        )
        count = len(artist_albums["items"])
        albumids = []
        while artist_albums["total"] > count:
            artist_albums["items"] += self.__spotipy.artist_albums(
                self.__artist_id,
                album_type=album_type,
                country=self.__user_country,
                limit=50,
                offset=count,
            )["items"]
            count += 50
        for album in artist_albums["items"]:
            albumids.append(album["id"])
        albums = self.__prepare_album_listitems(albumids)
        self.__add_album_listitems(albums)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ALBUM_IGNORE_THE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_saved_album_ids(self) -> List[str]:
        albums = self.__spotipy.current_user_saved_albums(limit=1, offset=0)
        cache_str = f"spotify-savedalbumids.{self.__userid}"
        checksum = albums["total"]
        album_ids = self.cache.get(cache_str, checksum=checksum)
        if album_ids:
            cache_log(f'Retrieved {len(album_ids)} cached album ids for user "{self.__userid}".')
            return album_ids

        album_ids = []
        if albums and albums.get("items"):
            count = len(albums["items"])
            album_ids = []
            while albums["total"] > count:
                albums["items"] += self.__spotipy.current_user_saved_albums(limit=50, offset=count)[
                    "items"
                ]
                count += 50
            for album in albums["items"]:
                album_ids.append(album["album"]["id"])
            self.cache.set(cache_str, album_ids, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(album_ids)} UNCACHED album ids for user "{self.__userid}".'
            )

        return album_ids

    def __get_saved_albums(self) -> List[Dict[str, Any]]:
        album_ids = self.__get_saved_album_ids()
        cache_str = f"spotify.savedalbums.{self.__userid}"
        checksum = self.__cache_checksum(len(album_ids))
        albums = self.cache.get(cache_str, checksum=checksum)
        if albums:
            cache_log(f'Retrieved {len(albums)} cached albums for user "{self.__userid}".')
        else:
            albums = self.__prepare_album_listitems(album_ids)
            self.cache.set(cache_str, albums, checksum=checksum)
            cache_log(f'Retrieved {_get_len(albums)} UNCACHED albums for user "{self.__userid}".')
        return albums

    def browse_saved_albums(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)
        )
        albums = self.__get_saved_albums()
        self.__add_album_listitems(albums, True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ALBUM_IGNORE_THE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmcplugin.setContent(self.__addon_handle, "albums")

    def __get_saved_track_ids(self) -> List[str]:
        saved_tracks = self.__spotipy.current_user_saved_tracks(
            limit=1, offset=self.__offset, market=self.__user_country
        )
        total = saved_tracks["total"]
        cache_str = f"spotify.savedtracksids.{self.__userid}"
        track_ids = self.cache.get(cache_str, checksum=total)
        if track_ids:
            cache_log(
                f'Retrieved {len(track_ids)} cached saved track ids for user "{self.__userid}".'
            )
            return track_ids

        # Get from api.
        track_ids = []
        count = len(saved_tracks["items"])
        while total > count:
            saved_tracks["items"] += self.__spotipy.current_user_saved_tracks(
                limit=50, offset=count, market=self.__user_country
            )["items"]
            count += 50
        for track in saved_tracks["items"]:
            track_ids.append(track["track"]["id"])
        self.cache.set(cache_str, track_ids, checksum=total)
        cache_log(
            f'Retrieved {_get_len(track_ids)} UNCACHED saved track ids for user "{self.__userid}".'
        )

        return track_ids

    def __get_saved_tracks(self):
        # Get from cache first.
        track_ids = self.__get_saved_track_ids()
        cache_str = f"spotify.savedtracks.{self.__userid}"

        tracks = self.cache.get(cache_str, checksum=len(track_ids))
        if tracks:
            cache_log(f'Retrieved {len(tracks)} cached saved tracks for user "{self.__userid}".')
        else:
            # Get from api.
            tracks = self.__prepare_track_listitems(track_ids)
            self.cache.set(cache_str, tracks, checksum=len(track_ids))
            cache_log(
                f'Retrieved {_get_len(tracks)} UNCACHED saved tracks for user "{self.__userid}".'
            )

        return tracks

    def browse_saved_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_SONGS_STR_ID)
        )
        tracks = self.__get_saved_tracks()
        self._merge_enrichment_into_tracks(tracks)
        self.__add_track_listitems(tracks, True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self._start_streaming_enrichment_thread()

    def __get_saved_artists(self) -> List[Dict[str, Any]]:
        saved_albums = self.__get_saved_albums()
        followed_artists = self.__get_followed_artists()
        cache_str = f"spotify.savedartists.{self.__userid}"
        checksum = len(saved_albums) + len(followed_artists)
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached saved artists for user "{self.__userid}".')
        else:
            all_artist_ids = []
            artists = []
            # extract the artists from all saved albums
            for item in saved_albums:
                for artist in item["artists"]:
                    if artist["id"] not in all_artist_ids:
                        all_artist_ids.append(artist["id"])
            for chunk in get_chunks(all_artist_ids, 50):
                artists += self.__prepare_artist_listitems(self.__spotipy.artists(chunk)["artists"])
            # append artists that are followed
            for artist in followed_artists:
                if not artist["id"] in all_artist_ids:
                    artists.append(artist)
            self.cache.set(cache_str, artists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED saved artists for user "{self.__userid}".'
            )

        return artists

    def browse_saved_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)
        )
        artists = self.__get_saved_artists()
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_followed_artists(self) -> List[Dict[str, Any]]:
        artists = self.__spotipy.current_user_followed_artists(limit=50)
        cache_str = f"spotify.followedartists.{self.__userid}"
        checksum = artists["artists"]["total"]

        cached_artists = self.cache.get(cache_str, checksum=checksum)
        if cached_artists:
            artists = cached_artists
            cache_log(
                f'Retrieved {len(artists)} cached followed artists for user "{self.__userid}".'
            )
        else:
            count = len(artists["artists"]["items"])
            after = artists["artists"]["cursors"]["after"]
            while artists["artists"]["total"] > count:
                result = self.__spotipy.current_user_followed_artists(limit=50, after=after)
                artists["artists"]["items"] += result["artists"]["items"]
                after = result["artists"]["cursors"]["after"]
                count += 50
            artists = self.__prepare_artist_listitems(artists["artists"]["items"], is_followed=True)
            self.cache.set(cache_str, artists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED followed artists for user "{self.__userid}".'
            )

        return artists

    def browse_followed_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)
        )
        artists = self.__get_followed_artists()
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def search_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)
        )

        result = self.__spotipy.search(
            q=f"artist:{self.__artist_id}",
            type="artist",
            limit=self.__limit,
            offset=self.__offset,
            market=self.__user_country,
        )

        artists = self.__prepare_artist_listitems(result["artists"]["items"])
        self.__add_artist_listitems(artists)
        self.__add_next_button(result["artists"]["total"])

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)


    def search_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_SONGS_STR_ID)
        )

        result = self.__spotipy.search(
            q=f"track:{self.__track_id}",
            type="track",
            limit=self.__limit,
            offset=self.__offset,
            market=self.__user_country,
        )

        tracks = self.__prepare_track_listitems(tracks=result["tracks"]["items"])
        self._merge_enrichment_into_tracks(tracks)
        self.__add_track_listitems(tracks, True)
        self.__add_next_button(result["tracks"]["total"])

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self._start_streaming_enrichment_thread()


    def search_albums(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)
        )

        result = self.__spotipy.search(
            q=f"album:{self.__album_id}",
            type="album",
            limit=self.__limit,
            offset=self.__offset,
            market=self.__user_country,
        )

        album_ids = []
        for album in result["albums"]["items"]:
            album_ids.append(album["id"])
        albums = self.__prepare_album_listitems(album_ids)
        self.__add_album_listitems(albums, True)
        self.__add_next_button(result["albums"]["total"])

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)


    def search_playlists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")

        result = self.__spotipy.search(
            q=self.__playlist_id,
            type="playlist",
            limit=self.__limit,
            offset=self.__offset,
            market=self.__user_country,
        )

        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID)
        )
        playlists = self.__prepare_playlist_listitems(result["playlists"]["items"])
        self.__add_playlist_listitems(playlists)
        self.__add_next_button(result["playlists"]["total"])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)


    def search(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        xbmcplugin.setPluginCategory(
            self.__addon_handle, xbmc.getLocalizedString(KODI_SEARCH_RESULTS_STR_ID)
        )

        kb = xbmc.Keyboard("", xbmc.getLocalizedString(KODI_ENTER_SEARCH_STRING_STR_ID))
        kb.doModal()
        if kb.isConfirmed():
            value = kb.getText()
            items = []
            result = self.__spotipy.search(
                q=f"{value}",
                type="artist,album,track,playlist",
                limit=1,
                market=self.__user_country,
            )
            items.append(
                (
                    f"{xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)}"
                    f" ({result['artists']['total']})",
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.search_artists.__name__}&artistid={value}",
                )
            )
            items.append(
                (
                    f"{xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID)}"
                    f" ({result['playlists']['total']})",
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.search_playlists.__name__}&playlistid={value}",
                )
            )
            items.append(
                (
                    f"{xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)} ({result['albums']['total']})",
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.search_albums.__name__}&albumid={value}",
                )
            )
            items.append(
                (
                    f"{xbmc.getLocalizedString(KODI_SONGS_STR_ID)} ({result['tracks']['total']})",
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.search_tracks.__name__}&trackid={value}",
                )
            )
            for item in items:
                li = xbmcgui.ListItem(item[0], path=item[1])
                li.setProperty("do_not_analyze", "true")
                li.setProperty("IsPlayable", "false")
                li.addContextMenuItems([], True)
                xbmcplugin.addDirectoryItem(
                    handle=self.__addon_handle, url=item[1], listitem=li, isFolder=True
                )

        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __add_next_button(self, list_total: int) -> None:
        # Adds a next button if needed.
        params = self.__params
        if list_total > self.__offset + self.__limit:
            params["offset"] = [str(self.__offset + self.__limit)]
            url = f"plugin://{ADDON_ID}/"

            for key, value in params.items():
                v = value[0] if isinstance(value, (list, tuple)) and value else value
                if key == "action":
                    url += f"?{key}={v}"
                else:
                    url += f"&{key}={v}"

            li = xbmcgui.ListItem(xbmc.getLocalizedString(KODI_NEXT_PAGE_STR_ID), path=url)
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")

            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=url, listitem=li, isFolder=True
            )

    def __precache_library(self) -> None:
        if not self.__win.getProperty("Spotify.PreCachedItems"):
            monitor = xbmc.Monitor()
            self.__win.setProperty("Spotify.PreCachedItems", "busy")
            user_playlists = self.__get_user_playlists(self.__userid)
            for playlist in user_playlists:
                self.__get_playlist_details(playlist["id"])
                if monitor.abortRequested():
                    return
            self.__get_saved_albums()
            if monitor.abortRequested():
                return
            self.__get_saved_artists()
            if monitor.abortRequested():
                return
            self.__get_saved_tracks()
            del monitor
            self.__win.setProperty("Spotify.PreCachedItems", "done")
