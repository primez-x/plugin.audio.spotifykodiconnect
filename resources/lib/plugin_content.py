import json
import math
import os
import sys
import threading
import time
import urllib.parse
import datetime
import hashlib
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import simplecache
import spotty
import utils
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from string_ids import *
from play_queue import (
    mark_original_complete as play_queue_mark_original_complete,
    report_loaded as play_queue_report_loaded,
    start_session as play_queue_start_session,
)
from utils import (
    ADDON_ID,
    ADDON_WINDOW_ID,
    LOGINFO,
    PROXY_HOST,
    PROXY_PORT,
    get_chunks,
    log_exception,
    log_msg,
)


def _import_spotipy():
    """Import spotipy (and requests/urllib3 with it) on first use only.

    It is about 80% of the plugin's import time, and routes served entirely
    from the cache never touch the Spotify API.
    """
    import spotipy

    utils.install_spotipy_rate_limit_hook(spotipy)
    return spotipy


class _LazySpotify:
    """spotipy.Spotify stand-in that imports and builds the client on first use."""

    def __init__(self, auth: str):
        self._auth = auth
        self._client = None

    def _get_client(self):
        if self._client is None:
            self._client = _import_spotipy().Spotify(auth=self._auth)
        return self._client

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._get_client(), name)


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
ARTIST_FANART_CACHE_MAX_ITEMS = 500
_ARTIST_FANART_MEMO_LOCK = threading.Lock()
_PAGE_FETCH_EXECUTOR = ThreadPoolExecutor
DYNAMIC_PAGE_LIMIT = 50
DYNAMIC_PAGING_COMPLETE_KEY = "_dynamic_paging_complete"
DYNAMIC_PAGING_LOADED_KEY = "_dynamic_paging_loaded"
DYNAMIC_PAGING_BUSY_PREFIX = "Spotify.DynamicPaging."
PLAYLIST_COLLECTION_CACHE_BUCKET_SECONDS = 900
PLAYLIST_COLLECTION_CACHE_EXPIRATION = datetime.timedelta(
    seconds=PLAYLIST_COLLECTION_CACHE_BUCKET_SECONDS
)
USER_PLAYLIST_CACHE_BUCKET_SECONDS = 300
USER_PLAYLIST_CACHE_EXPIRATION = datetime.timedelta(seconds=USER_PLAYLIST_CACHE_BUCKET_SECONDS)
RELATION_CACHE_EXPIRATION = datetime.timedelta(minutes=5)
# Background paging persists the growing collection every N pages (and at
# the end) instead of after every page; these collections are kept out of
# simplecache's window-property mirror (database only).
PAGED_CACHE_WRITE_EVERY_PAGES = 5
# Background paging keeps this many page requests (fetch + prepare) in
# flight; pages are still consumed and stored in order.
PAGED_FETCH_WORKERS = 4
# Content listings (album, artist pages, playlist details) are cached without
# depending on library totals; liked/followed state is overlaid at render time
# from user-action overrides (see __apply_relation_overrides).
ARTIST_CONTENT_CACHE_EXPIRATION = datetime.timedelta(days=1)
TOP_ITEMS_CACHE_EXPIRATION = datetime.timedelta(days=1)
RELATION_OVERRIDES_EXPIRATION = datetime.timedelta(days=30)
RELATION_OVERRIDES_MAX_ITEMS = 500
RELATION_SNAPSHOT_KEY = "_relts"
# Library-totals checksum (3 Spotify calls) is memoised across plugin
# processes in the home window for this long, and cleared by like/save/follow.
LIBRARY_CHECKSUM_PROP = "Spotify.LibraryChecksum"
LIBRARY_CHECKSUM_TTL_SECS = 60
# Paged track collections (playlist details, saved tracks) are stored as a
# small head row plus fixed-size chunk rows, so background paging only
# rewrites the chunk that is still filling instead of the whole list.
PAGED_CACHE_CHUNK_ITEMS = 500
PAGED_CACHE_CHUNKS_KEY = "_chunks"
# Saved tracks: items[:N] of a cached collection match Spotify, the rest is
# the previous listing kept on screen until background paging replaces it.
SAVED_TRACKS_EXACT_KEY = "_exact_items"
SAVED_TRACKS_SOURCE_KEY = "_source"
# Search: one /search call (all four types) per normalised query + market,
# shared by the search menu and its four sub-listings (skin widgets).
SEARCH_RESULT_LIMIT = 50
SEARCH_CACHE_EXPIRATION = datetime.timedelta(minutes=10)
SEARCH_INFLIGHT_PROP_PREFIX = "Spotify.SearchInFlight."
SEARCH_INFLIGHT_WAIT_SECS = 3.0
SEARCH_INFLIGHT_POLL_SECS = 0.1
EXPLORE_CATEGORIES_CACHE_EXPIRATION = datetime.timedelta(days=1)
# Artist id -> largest image URL, persisted (database only) across processes.
ARTIST_FANART_PERSIST_EXPIRATION = datetime.timedelta(days=30)
ARTIST_FANART_PERSIST_CHECKSUM = "fanart-1"
PRECACHE_NAVIGATION_TOKEN_PROP = "Spotify.PreCacheNavigationToken"
PRECACHE_MAX_PLAYLISTS = 10
PRECACHE_MAX_PLAYLIST_TRACKS = 250
PRECACHE_MAX_LIBRARY_ITEMS = 250
DAYLIST_LABEL = "daylist"
DAYLIST_TITLE_BUCKET_SECONDS = 300
DAYLIST_TITLE_BUCKET_KEY = "_daylist_title_bucket"
DAYLIST_TITLE_RETRY_DELAYS_MS = (1000, 2500, 5000)
LEGACY_CATEGORY_ALIASES = {"made-for-you": "Made For You"}
# While the plugin is still building a directory Kodi's Container.FolderPath
# usually still points at the parent, so dynamic continuations wait briefly
# for the listing to resolve before deciding whether it is the active one.
ACTIVE_LISTING_WAIT_SECS = 3.0
ACTIVE_LISTING_POLL_SECS = 0.25

# Spotify DJ playlist ID - not supported by third-party clients (librespot issue #1604)
DJ_PLAYLIST_ID = "37i9dQZF1EYkqdzj48dyYq"

# Bump this when the cached data structure changes (e.g. new fields pulled
# from the Spotify API, different track/album/artist dict shapes, serialisation
# format changes).  Any value different from what is already stored will
# automatically invalidate every cached entry.
CACHE_SCHEMA_VERSION = "6"

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


def _art_for_track(
    track: Dict[str, Any], fallback_icon_path: str = None, artist_fanart: str = None
) -> Dict[str, str]:
    """Build Kodi art from Spotify album.images; use largest (640) for all art so every location stays sharp.
    If artist_fanart is set, add artist.fanart for Artist slideshow / Music OSD background."""
    album = track.get("album") or {}
    images = (album.get("images") or []) if isinstance(album, dict) else []
    if images:
        # Spotify: images sorted by width descending; [0]=largest (typically 640x640)
        largest = images[0].get("url") or ""
        if largest:
            art = {
                "fanart": largest,
                "poster": largest,
                "thumb": largest,
                "icon": largest,
            }
            if artist_fanart:
                art["artist.fanart"] = artist_fanart
            return art
    base = _art_for_item(track.get("thumb") or "", fallback_icon_path)
    if artist_fanart and base:
        base["artist.fanart"] = artist_fanart
    return base


_LEAN_ALBUM_KEYS = ("name", "release_date", "album_type", "label")
_LEAN_TRACK_OPTIONAL_KEYS = (
    "artistid",
    "genre",
    "artist_fanart",
    "artist_genres",
    "artist_followers",
    "saved",
    "artist_followed",
    RELATION_SNAPSHOT_KEY,
)


def _lean_track(track: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce a prepared Spotify track to the fields rendering needs.

    Prepared tracks used to be cached as the full API object plus a stored
    context menu (~3.7 KB each); this row is ~0.6 KB. Album art is folded
    into "thumb", relation states replace the context menu (built at render).
    """
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    lean_album = {key: album[key] for key in _LEAN_ALBUM_KEYS if album.get(key)}
    copyrights = [
        {"text": entry["text"]}
        for entry in (album.get("copyrights") or [])
        if isinstance(entry, dict) and entry.get("text")
    ]
    if copyrights:
        lean_album["copyrights"] = copyrights
    lean = {
        "id": track["id"],
        "uri": track.get("uri") or f"spotify:track:{track['id']}",
        "name": track.get("name") or "",
        "artist": track.get("artist") or "",
        "duration_ms": int(track.get("duration_ms") or 0),
        "album": lean_album,
        "thumb": track.get("thumb") or "DefaultMusicSongs.png",
    }
    if track.get("track_number"):
        lean["track_number"] = int(track["track_number"])
    if track.get("disc_number") and int(track["disc_number"]) != 1:
        lean["disc_number"] = int(track["disc_number"])
    if track.get("year"):
        lean["year"] = int(track["year"])
    if track.get("rating"):
        lean["rating"] = int(track["rating"])
    for key in _LEAN_TRACK_OPTIONAL_KEYS:
        value = track.get(key)
        if value is not None and value != "" and value != []:
            lean[key] = value
    linked_from = track.get("linked_from")
    if isinstance(linked_from, dict) and linked_from.get("id"):
        lean["linked_from"] = {"id": linked_from["id"], "uri": linked_from.get("uri") or ""}
    return lean


def _is_spotify_daylist_playlist(playlist: Dict[str, Any]) -> bool:
    owner_id = (playlist.get("owner") or {}).get("id")
    if owner_id != "spotify":
        return False

    name = (playlist.get("name") or "").strip().lower()
    description = (playlist.get("description") or "").strip().lower()
    images = playlist.get("images") or []
    image_url = ""
    if images and isinstance(images[0], dict):
        image_url = (images[0].get("url") or "").lower()

    return (
        name == DAYLIST_LABEL
        or name.startswith(f"{DAYLIST_LABEL} - ")
        or description == "your day in a playlist."
        or "daylist.spotifycdn.com" in image_url
    )


def _daylist_display_name(playlist_name: str) -> str:
    name = (playlist_name or "").strip()
    lower = name.lower()
    prefix = f"{DAYLIST_LABEL} - "
    if lower.startswith(prefix):
        return name[len(prefix) :].strip() or name
    return name


def _has_dynamic_daylist_name(playlist_name: str) -> bool:
    display_name = _daylist_display_name(playlist_name)
    return bool(display_name) and display_name.lower() != DAYLIST_LABEL


def _daylist_title_bucket() -> str:
    return str(int(time.time() // DAYLIST_TITLE_BUCKET_SECONDS))


def _normalized_lookup_label(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


class PluginContent:
    __addon: xbmcaddon.Addon = xbmcaddon.Addon(id=ADDON_ID)
    __win: xbmcgui.Window = xbmcgui.Window(utils.ADDON_WINDOW_ID)
    __addon_icon_path = os.path.join(
        xbmcvfs.translatePath(__addon.getAddonInfo("path")), "resources"
    )
    __action = ""
    __spotty: spotty.Spotty = None
    __spotipy: Any = None
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

            # Spotty binary is ONLY needed for the zeroconf authentication flow.
            # Defer creation so normal browse/play actions skip the expensive
            # SpottyHelper self-test (runs spotty subprocess on every invocation
            # on ARM Linux, with no timeout — can hang on slow devices).
            self.__spotty: Optional[spotty.Spotty] = None

            self.parse_params()

            if not self.check_auth_and_refresh_spotipy():
                return
            self.__navigation_token = str(time.time())
            self.__win.setProperty(PRECACHE_NAVIGATION_TOKEN_PROP, self.__navigation_token)

            if self.__action:
                log_msg(f"Evaluating action '{self.__action}'.")
                handler = self._get_action_handler(self.__action)
                if handler:
                    handler()
                else:
                    log_msg(f"Unknown action '{self.__action}'.", LOGINFO)
                    xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            else:
                log_msg("Browsing main.")
                self.__browse_main()
                if self.__addon.getSetting("library_precache_enabled").lower() == "true":
                    precache_thread = threading.Thread(target=self.__precache_library, daemon=True)
                    precache_thread.start()

        except Exception as exc:
            log_exception(exc, "PluginContent init error")
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def check_auth_and_refresh_spotipy(self) -> bool:
        """Ensure a Spotify client exists. Returns False when the request was ended.

        Zeroconf pairing (which moves credentials.json aside) is only started
        automatically when no stored credentials exist at all and the request
        comes from an interactive foreground listing. If credentials exist the
        service is merely (re)connecting, so we notify and end the listing
        instead of forcing a re-pair. Widgets never get dialogs.

        The token is read once without waiting: a missing or expired token
        ends widget requests immediately instead of blocking every home
        widget (and silently returning 401-empty listings after expiry).
        """
        if self.__action == "authenticate_plugin_request":
            # Explicit user request: the handler runs the pairing flow itself.
            return True

        auth_token: str = utils.get_valid_cached_auth_token()
        if auth_token:
            self.init_spotipy(auth_token)
            return True

        interactive = not self.__is_non_interactive_request()
        if interactive and not self.__has_stored_credentials():
            self.authenticate_plugin_after_login_failure()
            if self.__spotipy is not None:
                return True
        elif interactive and not self.__action and utils.auth_renew_failing():
            # Root menu: it hosts the only "Authenticate" entry, so a revoked
            # login must not lock the user out. Ask instead of forcing a re-pair,
            # and only once the service has actually failed a renewal (not
            # while it is still connecting, e.g. right after boot).
            log_msg("No Spotify auth token yet at root menu; offering re-auth.", LOGINFO)
            if self.__confirm_reauthenticate():
                self.authenticate_plugin_after_login_failure()
                if self.__spotipy is not None:
                    return True
        elif interactive:
            log_msg("No Spotify auth token yet; service is still connecting.", LOGINFO)
            self.__notify(self.__addon.getLocalizedString(SPOTIFY_CONNECTING_STR_ID))
        else:
            log_msg("No Spotify auth token yet; ending non-interactive request quietly.")

        self.__end_directory(succeeded=False)
        return False

    @staticmethod
    def __has_stored_credentials() -> bool:
        try:
            # Path helpers only; no SpottyHelper binary self-test needed.
            return SpottyAuth(spotty.Spotty()).has_stored_credentials()
        except Exception as exc:
            log_exception(exc, "stored credentials check")
            # Unknown: assume they exist so we never trigger a destructive re-pair.
            return True

    def __is_non_interactive_request(self) -> bool:
        """Best-effort widget / background invocation detection.

        Kodi gives no explicit widget flag. A request is treated as
        interactive only when it has a directory handle and either Kodi is
        already browsing this add-on or a media window (Music/Videos nav) is
        active. Skin widgets are typically resolved from Home or custom
        non-media windows while Container.FolderPath points elsewhere.
        Any detection error is treated as non-interactive (no dialogs).
        """
        if self.__addon_handle < 0:
            return True
        try:
            folder = xbmc.getInfoLabel("Container.FolderPath") or ""
            if folder.startswith(f"plugin://{ADDON_ID}"):
                return False
            get_cond = getattr(xbmc, "getCondVisibility", None)
            if callable(get_cond) and get_cond("Window.IsMedia"):
                return False
        except Exception as exc:
            log_exception(exc, "interactive request detection")
        return True

    def __confirm_reauthenticate(self) -> bool:
        try:
            return bool(
                xbmcgui.Dialog().yesno(
                    self.__addon.getAddonInfo("name"),
                    self.__addon.getLocalizedString(SPOTIFY_REAUTH_PROMPT_STR_ID),
                )
            )
        except Exception as exc:
            log_exception(exc, "re-authenticate prompt")
            return False

    def __notify(self, message: str) -> None:
        try:
            xbmcgui.Dialog().notification(
                self.__addon.getAddonInfo("name"),
                message,
                icon=self.__addon.getAddonInfo("icon"),
                time=3000,
                sound=False,
            )
        except Exception as exc:
            log_exception(exc, "notification")

    def __end_directory(self, succeeded: bool = True) -> None:
        if self.__addon_handle < 0:
            return
        try:
            xbmcplugin.endOfDirectory(handle=self.__addon_handle, succeeded=succeeded)
        except Exception as exc:
            log_exception(exc, "endOfDirectory")

    def refresh_spotipy(self):
        auth_token: str = utils.get_valid_cached_auth_token()
        if not auth_token:
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            return

        log_msg("Got auth_token (refreshed).")

        self.init_spotipy(auth_token)

    def init_spotipy(self, auth_token: str) -> None:
        self.__spotipy = _LazySpotify(auth_token)
        # Use cached user profile from a previous invocation to avoid an extra
        # Spotify API round-trip (sp.me()) on every browse / play action.
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        cached_id = win.getProperty("Spotify.UserId")
        if cached_id:
            self.__userid = cached_id
            self.__username = win.getProperty("Spotify.Username") or cached_id
            self.__user_country = win.getProperty("Spotify.UserCountry") or ""
            return
        me = self.__spotipy.me()
        self.__userid = me["id"]
        self.__username = me.get("email") or me.get("id") or ""
        self.__user_country = me.get("country") or ""
        win.setProperty("Spotify.UserId", self.__userid)
        win.setProperty("Spotify.Username", self.__username)
        win.setProperty("Spotify.UserCountry", self.__user_country)

    def authenticate_plugin_after_login_failure(self) -> None:
        self.authenticate_plugin(
            self.__addon.getLocalizedString(AUTHENTICATE_INSTRUCTIONS_AFTER_LOGIN_FAIL_STR_ID)
        )

    def authenticate_plugin_request(self) -> None:
        self.authenticate_plugin(self.__addon.getLocalizedString(AUTHENTICATE_INSTRUCTIONS_STR_ID))

    def authenticate_plugin(self, instructions: str) -> None:
        dialog = xbmcgui.Dialog()
        dialog_title = self.__addon.getAddonInfo("name")

        # Lazy-init Spotty only when authentication is actually needed.
        if self.__spotty is None:
            self.__spotty = spotty.get_spotty(SpottyHelper())
        spotty_auth = SpottyAuth(self.__spotty)

        # Tell the service not to restore credentials.json from the .bak we
        # are about to create while the user is pairing.
        utils.mark_zeroconf_pairing(True)
        try:
            zeroconf_auth = spotty_auth.start_zeroconf_authenticate()
            if zeroconf_auth is None:
                dialog.ok(dialog_title, self.get_zeroconf_program_failed_msg(spotty_auth))
                utils.kill_this_plugin()
                return

            dialog.ok(dialog_title, instructions)

            zeroconf_auth.terminate()
            # Check before clearing the pairing flag so a concurrent service
            # backup restore cannot masquerade as a successful pairing.
            paired_ok = spotty_auth.zeroconf_authenticated_ok()
        finally:
            utils.mark_zeroconf_pairing(False)

        if not paired_ok:
            # Put the previous credentials back so playback keeps working.
            spotty_auth.restore_credentials_from_backup_if_needed()
            dialog.ok(dialog_title, self.get_zeroconf_authentication_failed_msg(spotty_auth))
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

    _ALLOWED_ACTIONS = frozenset(
        {
            "browse_main_library",
            "browse_main_explore",
            "browse_album",
            "browse_playlist",
            "play_playlist",
            "browse_category",
            "browse_playlists",
            "browse_new_releases",
            "browse_saved_albums",
            "browse_saved_tracks",
            "browse_saved_artists",
            "browse_followed_artists",
            "browse_top_artists",
            "browse_top_tracks",
            "browse_artist_everything",
            "browse_artist_just_albums",
            "browse_artist_just_singles",
            "browse_artist_just_albums_and_singles",
            "browse_artist_just_compilations",
            "browse_artist_just_appears_on",
            "artist_top_tracks",
            "related_artists",
            "browse_radio",
            "search",
            "search_artists",
            "search_tracks",
            "search_albums",
            "search_playlists",
            "follow_playlist",
            "unfollow_playlist",
            "follow_artist",
            "unfollow_artist",
            "save_album",
            "remove_album",
            "save_track",
            "remove_track",
            "add_track_to_playlist",
            "remove_track_from_playlist",
            "delete_cache_db",
            "refresh_listing",
            "toggle_liked",
            "authenticate_plugin_request",
        }
    )

    def _get_action_handler(self, action: str):
        """Return bound method for action name from explicit allowlist."""
        if not action or action not in self._ALLOWED_ACTIONS:
            return None
        meth = getattr(self, action, None)
        return meth if callable(meth) else None

    def __get_saved_track_total(self) -> int:
        saved_tracks = self.__spotipy.current_user_saved_tracks(
            limit=1, offset=0, market=self.__user_country
        )
        return int(saved_tracks.get("total") or 0)

    def __get_saved_album_total(self) -> int:
        saved_albums = self.__spotipy.current_user_saved_albums(limit=1, offset=0)
        return int(saved_albums.get("total") or 0)

    def __get_followed_artist_total(self) -> int:
        followed_artists = self.__spotipy.current_user_followed_artists(limit=1)
        return int((followed_artists.get("artists") or {}).get("total") or 0)

    def __cache_checksum(self, opt_value: Any = None) -> str:
        """Library-membership checksum based on library counts.

        Only for listings whose *membership* depends on the library (saved
        albums, saved artists). Computing it costs three Spotify calls, so it
        is memoised per process and across plugin processes in a home-window
        property for LIBRARY_CHECKSUM_TTL_SECS; like/save/follow actions clear
        that memo (see __invalidate_library_checksum).

        Includes CACHE_SCHEMA_VERSION so that any change to the data shape
        (new API fields, serialisation format, etc.) automatically invalidates
        every previously-cached entry without requiring a manual cache clear.
        """
        result = self.__cached_checksum
        if not result:
            generic_checksum = self.__addon.getSetting("cache_checksum")
            result = self.__read_library_checksum_memo(generic_checksum)
        if not result:
            saved_track_total = self.__get_saved_track_total()
            saved_album_total = self.__get_saved_album_total()
            followed_artist_total = self.__get_followed_artist_total()
            result = (
                f"v{CACHE_SCHEMA_VERSION}"
                f"-{saved_track_total}-{saved_album_total}-{followed_artist_total}"
                f"-{generic_checksum}"
            )
            self.__write_library_checksum_memo(result, generic_checksum)
        self.__cached_checksum = result

        if opt_value:
            result += f"-{opt_value}"

        return result

    def __read_library_checksum_memo(self, generic_checksum: str) -> str:
        try:
            raw = xbmcgui.Window(ADDON_WINDOW_ID).getProperty(LIBRARY_CHECKSUM_PROP)
            if not raw:
                return ""
            memo = json.loads(raw)
            if (
                memo.get("user") != self.__userid
                or memo.get("generic") != generic_checksum
                or time.time() - float(memo.get("ts") or 0) >= LIBRARY_CHECKSUM_TTL_SECS
            ):
                return ""
            return str(memo.get("value") or "")
        except Exception:
            return ""

    def __write_library_checksum_memo(self, value: str, generic_checksum: str) -> None:
        try:
            xbmcgui.Window(ADDON_WINDOW_ID).setProperty(
                LIBRARY_CHECKSUM_PROP,
                json.dumps(
                    {
                        "user": self.__userid,
                        "generic": generic_checksum,
                        "ts": time.time(),
                        "value": value,
                    }
                ),
            )
        except Exception as exc:
            log_exception(exc, "library checksum memo")

    def __invalidate_library_checksum(self) -> None:
        self.__cached_checksum = ""
        try:
            xbmcgui.Window(ADDON_WINDOW_ID).clearProperty(LIBRARY_CHECKSUM_PROP)
        except Exception:
            pass

    def __content_checksum(self, namespace: str, *parts: Any) -> str:
        """Checksum for content whose items do not depend on library membership.

        Schema version + the manual "Refresh listing" checksum only, so liking
        a track or following an artist no longer invalidates album, artist or
        playlist caches. Liked/followed state is overlaid when rendering.
        """
        suffix = "-".join(str(part) for part in parts if part is not None and part != "")
        if suffix:
            suffix = f"-{suffix}"
        generic_checksum = self.__addon.getSetting("cache_checksum")
        return f"v{CACHE_SCHEMA_VERSION}-content-{namespace}{suffix}-{generic_checksum}"

    def __paged_cache_get(self, cache_str: str, checksum: Any = None) -> Any:
        """Read a large, dynamically paged collection (database only)."""
        if checksum is None:
            return self.cache.get(cache_str, mem_cache=False)
        return self.cache.get(cache_str, checksum=checksum, mem_cache=False)

    def __paged_cache_set(self, cache_str: str, value: Any, checksum: Any = None, **kwargs) -> None:
        """Write a large, dynamically paged collection (database only).

        simplecache otherwise json-encodes the whole collection twice per write
        (window property + sqlite) and keeps a multi-MB copy in Kodi's home
        window properties.
        """
        self.cache.set(cache_str, value, checksum=checksum, mem_cache=False, **kwargs)

    @staticmethod
    def __chunk_key(cache_str: str, index: int) -> str:
        return f"{cache_str}.chunk{index}"

    def __chunked_cache_get(
        self, cache_str: str, checksum: Any
    ) -> Optional[Tuple[Dict[str, Any], List[Any]]]:
        """Read a chunked collection: (head, items), or None on any miss.

        The head row names how many chunk rows belong to it; all chunks share
        the head's checksum and are read over one connection.
        """
        head = self.__paged_cache_get(cache_str, checksum=checksum)
        if not isinstance(head, dict):
            return None
        try:
            count = int(head.get(PAGED_CACHE_CHUNKS_KEY) or 0)
        except (TypeError, ValueError):
            return None
        if count <= 0:
            return head, []
        keys = [self.__chunk_key(cache_str, index) for index in range(count)]
        rows = self.cache.get_many(keys, checksum=checksum, mem_cache=False) or {}
        items: List[Any] = []
        for key in keys:
            chunk = rows.get(key)
            if not isinstance(chunk, list):
                return None
            items.extend(chunk)
        return head, items

    def __chunked_cache_set(
        self,
        cache_str: str,
        head: Dict[str, Any],
        items: List[Any],
        checksum: Any,
        first_dirty_item: int = 0,
        **kwargs,
    ) -> None:
        """Write the head plus only the chunks holding items[first_dirty_item:].

        Chunks before the first dirty item are already stored and complete;
        head and chunks go out in one set_many (one sqlite transaction).
        """
        size = PAGED_CACHE_CHUNK_ITEMS
        count = (len(items) + size - 1) // size
        head = dict(head)
        head[PAGED_CACHE_CHUNKS_KEY] = count
        values: Dict[str, Any] = {}
        for index in range(max(0, int(first_dirty_item)) // size, count):
            values[self.__chunk_key(cache_str, index)] = items[index * size : (index + 1) * size]
        values[cache_str] = head
        self.cache.set_many(values, checksum=checksum, mem_cache=False, **kwargs)

    def __relation_overrides_key(self) -> str:
        return f"spotify.relationoverrides.{self.__userid}"

    def __load_relation_overrides(self) -> Dict[str, Any]:
        try:
            overrides = self.cache.get(
                self.__relation_overrides_key(), checksum=CACHE_SCHEMA_VERSION
            )
        except Exception as exc:
            log_exception(exc, "relation overrides load")
            return {}
        return overrides if isinstance(overrides, dict) else {}

    def __record_relation_override(self, namespace: str, item_id: str, value: bool) -> None:
        """Remember a user-made relation change so cached listings can reflect it."""
        if not item_id:
            return
        overrides = self.__load_relation_overrides()
        overrides[f"{namespace}:{item_id}"] = [bool(value), time.time()]
        if len(overrides) > RELATION_OVERRIDES_MAX_ITEMS:
            newest = sorted(overrides.items(), key=lambda kv: kv[1][1], reverse=True)
            overrides = dict(newest[:RELATION_OVERRIDES_MAX_ITEMS])
        self.cache.set(
            self.__relation_overrides_key(),
            overrides,
            checksum=CACHE_SCHEMA_VERSION,
            expiration=RELATION_OVERRIDES_EXPIRATION,
        )

    def __apply_relation_overrides(
        self,
        items: List[Dict[str, Any]],
        fields: Tuple[Tuple[str, str, Callable[[Dict[str, Any]], str]], ...],
    ) -> None:
        """Overlay user-made save/follow changes onto cached relation states.

        Cached rows store relation states (e.g. "saved") instead of finished
        context menus; menus are built from those states at render time. One
        cache read per render. An override only applies when it is newer than
        the moment the item's relation state was computed (_relts), so a later
        fresh lookup always wins over an older Kodi-side action.

        fields: (relation namespace, state key in the row, item -> id).
        """
        if not items:
            return
        overrides = self.__load_relation_overrides()
        if not overrides:
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                item_ts = float(item.get(RELATION_SNAPSHOT_KEY) or 0)
            except (TypeError, ValueError):
                item_ts = 0.0
            for namespace, state_key, get_id in fields:
                item_id = get_id(item)
                if not item_id:
                    continue
                override = overrides.get(f"{namespace}:{item_id}")
                if not override:
                    continue
                try:
                    if float(override[1]) <= item_ts:
                        continue
                except (IndexError, TypeError, ValueError):
                    continue
                item[state_key] = bool(override[0])

    @staticmethod
    def _real_track_id(track: Dict[str, Any]) -> str:
        """Id used by track actions (the original id when Spotify relinked it)."""
        return (track.get("linked_from") or {}).get("id") or track.get("id") or ""

    TRACK_RELATION_FIELDS = (
        ("savedtrack", "saved", _real_track_id.__func__),
        ("followedartist", "artist_followed", lambda item: item.get("artistid")),
    )
    ALBUM_RELATION_FIELDS = (("savedalbum", "saved", lambda item: item.get("id")),)
    ARTIST_RELATION_FIELDS = (("followedartist", "followed", lambda item: item.get("id")),)
    PLAYLIST_RELATION_FIELDS = (("followedplaylist", "followed", lambda item: item.get("id")),)

    def __localized(self, string_id: int) -> str:
        """Add-on string, memoised per process (menus are built per row at render)."""
        memo = self.__dict__.setdefault("_localized_memo", {})
        key = ("addon", string_id)
        if key not in memo:
            memo[key] = self.__addon.getLocalizedString(string_id)
        return memo[key]

    def __kodi_localized(self, string_id: int) -> str:
        memo = self.__dict__.setdefault("_localized_memo", {})
        key = ("kodi", string_id)
        if key not in memo:
            memo[key] = xbmc.getLocalizedString(string_id)
        return memo[key]

    def __after_relation_change(self, namespace: str, item_id: str, value: bool) -> None:
        """Common bookkeeping after a like/save/follow action.

        Updates the short-lived relation cache, records a durable override for
        cached listings and invalidates the library-totals memo (membership
        listings such as saved albums must recompute). Callers then refresh
        the container. Unlike refresh_listing() it does not bump the global
        cache checksum, so album/artist/playlist caches survive.
        """
        self.__set_relation_cache(namespace, item_id, value)
        self.__record_relation_override(namespace, item_id, value)
        self.__invalidate_library_checksum()

    def __paged_collection_checksum(
        self,
        namespace: str,
        *parts: Any,
        bucket_seconds: int = PLAYLIST_COLLECTION_CACHE_BUCKET_SECONDS,
    ) -> str:
        bucket = int(time.time() // bucket_seconds)
        suffix = "-".join(str(part) for part in parts if part is not None and part != "")
        if suffix:
            suffix = f"-{suffix}"
        return f"v{CACHE_SCHEMA_VERSION}-{namespace}{suffix}-{bucket}"

    def __playlist_collection_checksum(self, *parts: Any) -> str:
        return self.__paged_collection_checksum("playlistcollection", *parts)

    def __user_playlists_checksum(self, userid: str) -> str:
        return self.__paged_collection_checksum(
            "userplaylists",
            userid,
            bucket_seconds=USER_PLAYLIST_CACHE_BUCKET_SECONDS,
        )

    def __build_url(self, query: Dict[str, str]) -> str:
        return (
            self.__base_url
            + "?"
            + urllib.parse.urlencode([(k, str(v)) for k, v in query.items() if v is not None])
        )

    def __current_request_url(self) -> str:
        flat = {}
        for key, value in self.__params.items():
            flat[key] = value[0] if isinstance(value, (list, tuple)) and value else value
        return self.__build_url(flat)

    def __is_active_listing(self, target_url: str) -> bool:
        if not target_url:
            return False
        try:
            get_info_label = getattr(xbmc, "getInfoLabel", None)
            current_url = ""
            if callable(get_info_label):
                current_url = get_info_label("Container.FolderPath") or ""
            if not current_url:
                cache_log(
                    f"Skipping dynamic listing work for {target_url}; active folder is unknown."
                )
                return False
            if current_url != target_url:
                cache_log(
                    f"Skipping dynamic listing work for {target_url}; active folder is {current_url}."
                )
                return False
            return True
        except Exception as exc:
            log_exception(exc, "active dynamic listing check")
            return False

    def __refresh_active_listing(self, target_url: str) -> None:
        if not self.__is_active_listing(target_url):
            return
        try:
            xbmc.executebuiltin("Container.Refresh")
        except Exception as exc:
            log_exception(exc, "dynamic listing refresh")

    def __wait_for_active_listing(self, target_url: str) -> bool:
        """Poll (abort-aware) until target_url is Kodi's active folder or time runs out."""
        if not target_url:
            return False
        monitor = xbmc.Monitor()
        attempts = max(1, int(ACTIVE_LISTING_WAIT_SECS / ACTIVE_LISTING_POLL_SECS))
        for attempt in range(attempts + 1):
            if self.__is_active_listing(target_url):
                return True
            if attempt >= attempts:
                break
            if monitor.waitForAbort(ACTIVE_LISTING_POLL_SECS):
                return False
        return False

    def __start_dynamic_page_continuation(
        self,
        busy_key: str,
        target_url: str,
        worker: Callable[[], None],
        require_active_listing: bool = True,
    ) -> None:
        # Do not check Container.FolderPath here: during first navigation the
        # directory has not resolved yet and FolderPath is still the parent.
        # The worker waits for the listing to become active instead, and
        # bails out (e.g. hidden widgets) if it never does, unless the
        # collection is worth completing anyway (require_active_listing=False).
        prop_key = f"{DYNAMIC_PAGING_BUSY_PREFIX}{busy_key}"
        if self.__win.getProperty(prop_key):
            return
        self.__win.setProperty(prop_key, "busy")

        def _run():
            try:
                if utils.is_rate_limited():
                    cache_log(f"Dynamic continuation {busy_key} skipped; Spotify rate limited.")
                    return
                if require_active_listing and not self.__wait_for_active_listing(target_url):
                    cache_log(f"Dynamic continuation {busy_key} skipped; listing not active.")
                    return
                worker()
            except Exception as exc:
                log_exception(exc, f"dynamic page continuation {busy_key}")
            finally:
                self.__win.clearProperty(prop_key)

        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def __mark_dynamic_collection_state(
        collection: Dict[str, Any], loaded: int, total: int, complete: bool
    ) -> None:
        collection[DYNAMIC_PAGING_LOADED_KEY] = int(loaded)
        collection[DYNAMIC_PAGING_COMPLETE_KEY] = bool(complete)
        collection["total"] = int(total)

    @staticmethod
    def __iter_pages_in_order(
        start: int,
        total: int,
        fetch_page: Callable[[int], Any],
        monitor: xbmc.Monitor,
    ):
        """Yield (offset, fetch_page(offset)) for start, start + page, ... < total.

        Up to PAGED_FETCH_WORKERS pages are requested at once; results come
        back in offset order, and a failed page raises when its turn comes.
        Stops early on Kodi abort or when the consumer stops iterating.
        """
        offsets = iter(range(int(start), int(total), DYNAMIC_PAGE_LIMIT))
        pool = _PAGE_FETCH_EXECUTOR(max_workers=PAGED_FETCH_WORKERS)
        pending = deque()
        try:
            for offset in offsets:
                pending.append((offset, pool.submit(fetch_page, offset)))
                if len(pending) >= PAGED_FETCH_WORKERS:
                    break
            while pending:
                if monitor.abortRequested():
                    return
                offset, future = pending.popleft()
                result = future.result()
                next_offset = next(offsets, None)
                if next_offset is not None:
                    pending.append((next_offset, pool.submit(fetch_page, next_offset)))
                yield offset, result
        finally:
            for _offset, future in pending:
                future.cancel()
            pool.shutdown(wait=True)

    def __relation_cache_key(self, namespace: str, item_id: str) -> str:
        return f"spotify.relation.{namespace}.{self.__userid}.{item_id}"

    def __set_relation_cache(self, namespace: str, item_id: str, value: bool) -> None:
        # Relation rows are database-only: thousands of 5-minute entries must
        # not pile up in Kodi's home-window properties.
        if not item_id:
            return
        self.cache.set(
            self.__relation_cache_key(namespace, item_id),
            "1" if value else "0",
            checksum=CACHE_SCHEMA_VERSION,
            expiration=RELATION_CACHE_EXPIRATION,
            mem_cache=False,
        )

    def __set_relation_cache_many(self, namespace: str, states: Dict[str, bool]) -> None:
        """Store several relation states in one cache write (one sqlite transaction)."""
        values = {
            self.__relation_cache_key(namespace, item_id): "1" if value else "0"
            for item_id, value in states.items()
            if item_id
        }
        if not values:
            return
        self.cache.set_many(
            values,
            checksum=CACHE_SCHEMA_VERSION,
            expiration=RELATION_CACHE_EXPIRATION,
            mem_cache=False,
        )

    def __get_relation_cache_many(self, namespace: str, item_ids: List[str]) -> Dict[str, str]:
        """Read several relation states in one cache read; misses are absent."""
        keys = {
            self.__relation_cache_key(namespace, item_id): item_id
            for item_id in item_ids
            if item_id
        }
        if not keys:
            return {}
        cached = (
            self.cache.get_many(list(keys), checksum=CACHE_SCHEMA_VERSION, mem_cache=False) or {}
        )
        return {keys[key]: value for key, value in cached.items() if key in keys}

    def __get_relation_set_for_page(
        self,
        namespace: str,
        item_ids: List[str],
        lookup: Callable[[List[str]], List[bool]],
    ) -> Set[str]:
        result: Set[str] = set()
        missing: List[str] = []
        unique_ids = list(OrderedDict.fromkeys(item_id for item_id in item_ids if item_id))
        cached_states = self.__get_relation_cache_many(namespace, unique_ids)
        for item_id in unique_ids:
            cached = cached_states.get(item_id)
            if cached == "1":
                result.add(item_id)
            elif cached == "0":
                continue
            else:
                missing.append(item_id)

        fetched: Dict[str, bool] = {}
        for chunk in get_chunks(missing, 50):
            try:
                states = lookup(chunk) or []
            except Exception as exc:
                log_exception(exc, f"{namespace} relation lookup")
                states = []
            for item_id, is_related in zip(chunk, states):
                related = bool(is_related)
                if related:
                    result.add(item_id)
                fetched[item_id] = related
        self.__set_relation_cache_many(namespace, fetched)

        return result

    def __get_saved_track_ids_for_page(self, track_ids: List[str]) -> Set[str]:
        return self.__get_relation_set_for_page(
            "savedtrack", track_ids, self.__spotipy.current_user_saved_tracks_contains
        )

    def __get_saved_album_ids_for_page(self, album_ids: List[str]) -> Set[str]:
        return self.__get_relation_set_for_page(
            "savedalbum", album_ids, self.__spotipy.current_user_saved_albums_contains
        )

    def __get_followed_artist_ids_for_page(self, artist_ids: List[str]) -> Set[str]:
        return self.__get_relation_set_for_page(
            "followedartist", artist_ids, self.__spotipy.current_user_following_artists
        )

    def __get_followed_playlist_states_for_page(
        self, playlists: List[Dict[str, Any]], relation_mode: str = "cache"
    ) -> Dict[str, Optional[bool]]:
        states: Dict[str, Optional[bool]] = {}
        candidates = [
            playlist
            for playlist in playlists
            if playlist
            and playlist.get("id")
            and (playlist.get("owner") or {}).get("id") != self.__userid
        ]
        cached_states = self.__get_relation_cache_many(
            "followedplaylist", [playlist["id"] for playlist in candidates]
        )
        fetched: Dict[str, bool] = {}
        for playlist in candidates:
            playlist_id = playlist["id"]
            if playlist_id in states:
                continue
            cached = cached_states.get(playlist_id)
            if cached == "1":
                states[playlist_id] = True
                continue
            if cached == "0":
                states[playlist_id] = False
                continue
            if relation_mode == "user_collection":
                states[playlist_id] = True
                fetched[playlist_id] = True
                continue
            if relation_mode != "lookup":
                states[playlist_id] = None
                continue
            try:
                state = self.__spotipy.playlist_is_following(playlist_id, [self.__userid])
                is_followed = bool(state and state[0])
            except Exception as exc:
                log_exception(exc, "playlist follow relation lookup")
                is_followed = False
            states[playlist_id] = is_followed
            fetched[playlist_id] = is_followed
        self.__set_relation_cache_many("followedplaylist", fetched)
        return states

    def __get_followed_playlist_ids_for_page(self, playlists: List[Dict[str, Any]]) -> Set[str]:
        states = self.__get_followed_playlist_states_for_page(playlists, relation_mode="lookup")
        return {playlist_id for playlist_id, is_followed in states.items() if is_followed}

    def delete_cache_db(self) -> None:
        log_msg("Deleting plugin cache...")
        # Empty the table instead of unlinking the file: the database runs in
        # WAL mode and the service keeps a connection open, so deleting the
        # file under it could corrupt the next one. The service's weekly
        # VACUUM gives the space back.
        cleared = False
        try:
            cleared = bool(self.cache.clear_all())
        except Exception as exc:
            log_exception(exc, "clearing simplecache")
        if cleared:
            log_msg("Cleared all simplecache entries.")
        else:
            # Unreadable (corrupt) database: remove it with its side files.
            simple_db_cache_addon = xbmcaddon.Addon(ADDON_ID)
            db_path = simple_db_cache_addon.getAddonInfo("profile")
            db_file = xbmcvfs.translatePath(f"{db_path}/simplecache.db")
            for path in (db_file, f"{db_file}-wal", f"{db_file}-shm"):
                try:
                    os.remove(path)
                except OSError:
                    pass
            log_msg(f"Deleted simplecache database file {db_file}.")

        dialog = xbmcgui.Dialog()
        header = self.__addon.getAddonInfo("name")
        msg = self.__addon.getLocalizedString(CACHED_CLEARED_STR_ID)
        dialog.ok(header, msg)

    def refresh_listing(self) -> None:
        self.__invalidate_library_checksum()
        self.__addon.setSetting("cache_checksum", time.strftime("%Y%m%d%H%M%S", time.gmtime()))
        log_msg(f"New cache_checksum = '{self.__addon.getSetting('cache_checksum')}'")
        xbmc.executebuiltin("Container.Refresh")

    def toggle_liked(self) -> None:
        """Add or remove current track from liked songs (for OSD button). Uses trackid param or Window property."""
        track_id = self.__track_id
        if not track_id:
            track_id = xbmcgui.Window(ADDON_WINDOW_ID).getProperty("Spotify.CurrentTrackId") or ""
        if not track_id:
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            return
        self.__track_id = track_id
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        try:
            # Query Spotify directly for the authoritative liked state.
            # The window property may be stale or empty (e.g. during a buffering
            # reset), so relying on it would always toggle in the wrong direction.
            result = self.__spotipy.current_user_saved_tracks_contains([track_id])
            liked = bool(result and result[0])
            if liked:
                self.__spotipy.current_user_saved_tracks_delete([track_id])
                win.clearProperty("Spotify.CurrentTrackLiked")
                self.__set_relation_cache("savedtrack", track_id, False)
            else:
                self.__spotipy.current_user_saved_tracks_add([track_id])
                win.setProperty("Spotify.CurrentTrackLiked", "true")
                self.__set_relation_cache("savedtrack", track_id, True)
        except Exception as exc:
            log_exception(exc, "toggle_liked failed")
        else:
            self.__record_relation_override("savedtrack", track_id, not liked)
            self.__invalidate_library_checksum()
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __add_track_listitems(
        self,
        tracks,
        append_artist_to_label: bool = False,
        playlist_details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.__apply_relation_overrides(tracks, self.TRACK_RELATION_FIELDS)
        list_items = self.__get_track_list(
            tracks, append_artist_to_label, playlist_details=playlist_details
        )
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
        self,
        tracks,
        append_artist_to_label: bool = False,
        playlist_details: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[str, xbmcgui.ListItem, bool]]:
        result = []
        for track in tracks:
            if not isinstance(track, dict) or not track.get("id"):
                continue
            context_items = self.__get_playlist_track_context_menu_items(
                track,
                bool(track.get("saved")),
                playlist_details,
                bool(track.get("artist_followed")),
            )
            item = self.__get_track_item(track, append_artist_to_label, context_items)
            if item is not None:
                result.append(item + (False,))
        return result

    def _track_album_description(self, track: Dict[str, Any], album: Dict[str, Any]) -> str:
        """Build album description from Spotify data (release date, genre). Label/copyright only in full album API."""
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

    def __get_track_item(
        self,
        track: Dict[str, Any],
        append_artist_to_label: bool = False,
        context_items: Optional[List[Tuple[str, str]]] = None,
    ) -> Optional[Tuple[str, xbmcgui.ListItem]]:
        # Unwrap Spotify playlist item format: { "track": { "id", "duration_ms", ... } }
        # Only unwrap when "track" is a dict (nested track object); avoid setting track to None or non-dict
        inner = track.get("track")
        if isinstance(inner, dict):
            track = inner
        # Skip items that are not valid track dicts (e.g. playlist item with track=null)
        if not isinstance(track, dict) or not track.get("id"):
            return None
        # Raw API track has "artists" list; ensure "artist" string exists for label/tag
        if not track.get("artist") and track.get("artists"):
            track = dict(track)
            track["artist"] = " / ".join(
                a.get("name", "") for a in track["artists"] if a.get("name")
            )
        duration_sec = max(1, math.ceil((track.get("duration_ms") or 0) / 1000))
        label = self.__get_track_name(track, append_artist_to_label)
        title = track["name"]
        album = track.get("album") or {}
        album_name = (album.get("name") or "") if isinstance(album, dict) else ""
        release_date = (album.get("release_date") or "") if isinstance(album, dict) else ""
        year = int(track.get("year") or 0)
        genre = track.get("genre")
        genres_list = []
        if genre is not None:
            if isinstance(genre, str) and genre:
                genres_list = [genre]
            elif isinstance(genre, (list, tuple)) and genre:
                genres_list = [str(g) for g in genre if g]

        # Local playback by using proxy on this machine.
        url = f"http://{PROXY_HOST}:{PROXY_PORT}/track/{track['id']}/{duration_sec}.wav"

        li = xbmcgui.ListItem(label, offscreen=True)
        li.setProperty("isPlayable", "true")

        # Kodi native music format via InfoTagMusic (avoids setInfo deprecation)
        tag = li.getMusicInfoTag()
        tag.setTitle(title)
        tag.setAlbum(album_name)
        tag.setArtist(track.get("artist") or "")
        tag.setDuration(duration_sec)
        tag.setYear(year)
        tag.setTrack(int(track.get("track_number") or 0))
        tag.setDisc(int(track.get("disc_number") or 1))
        tag.setRating(int(track.get("rating") or 0))
        tag.setMediaType("song")
        tag.setURL(url)
        # So skin list views (Label_VideoInfo_DetailsItem) show artist when ListItem.DBType=song
        li.setProperty("DBType", "song")
        if release_date:
            tag.setReleaseDate(release_date)
        if genres_list:
            tag.setGenres(genres_list)
        if isinstance(album, dict) and album.get("album_type") == "compilation":
            tag.setAlbumArtist("Various Artists")

        # Additional song info from Spotify only (OSD/skin)
        album_desc = self._track_album_description(track, album)
        artist_desc = self._track_artist_description(track)
        if album_desc:
            li.setProperty("Album_Description", album_desc)
        if artist_desc:
            li.setProperty("Artist_Description", artist_desc)

        li.setArt(_art_for_track(track, "DefaultMusicSongs.png", track.get("artist_fanart") or ""))
        li.setProperty("spotifytrackid", track["id"])
        li.setContentLookup(False)
        li.addContextMenuItems(context_items or [], True)
        li.setProperty("do_not_analyze", "true")
        li.setMimeType("audio/x-wav")

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
        cache_str = f"spotify.topartists.{self.__userid}"
        checksum = self.__content_checksum("topartists")
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached top artists for user "{self.__userid}".')
        else:
            result = self.__spotipy.current_user_top_artists(limit=50, offset=0)
            count = len(result["items"])
            while result["total"] > count:
                result["items"] += self.__spotipy.current_user_top_artists(limit=50, offset=count)[
                    "items"
                ]
                count += 50
            artists = self.__prepare_artist_listitems(result["items"])
            self.cache.set(
                cache_str, artists, checksum=checksum, expiration=TOP_ITEMS_CACHE_EXPIRATION
            )
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED top artists for user "{self.__userid}".'
            )
        self.__add_artist_listitems(artists)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def browse_top_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        cache_str = f"spotify.toptracks.{self.__userid}"
        checksum = self.__content_checksum("toptracks")
        tracks = self.cache.get(cache_str, checksum=checksum)
        if tracks:
            cache_log(f'Retrieved {len(tracks)} cached top tracks for user "{self.__userid}".')
        else:
            results = self.__spotipy.current_user_top_tracks(limit=50, offset=0)
            tracks = results["items"]
            while results["next"]:
                results = self.__spotipy.next(results)
                tracks.extend(results["items"])
            tracks = self.__prepare_track_listitems(tracks=tracks)
            self.cache.set(
                cache_str, tracks, checksum=checksum, expiration=TOP_ITEMS_CACHE_EXPIRATION
            )
            cache_log(
                f'Retrieved {_get_len(tracks)} UNCACHED top tracks for user "{self.__userid}".'
            )
        self.__add_track_listitems(tracks, True)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_category_list(self) -> List[Dict[str, str]]:
        """All browse categories as [{"id", "name", "thumb"}], cached for a day
        per country + locale (plus the manual-refresh checksum)."""
        locale = self.__user_country
        cache_str = f"spotify.categories.{self.__user_country or '-'}.{locale or '-'}"
        checksum = self.__content_checksum("categories", self.__user_country, locale)
        cached = self.cache.get(cache_str, checksum=checksum)
        if isinstance(cached, list) and cached:
            return cached

        categories = self.__spotipy.categories(country=self.__user_country, limit=50, locale=locale)
        count = len(categories["categories"]["items"])
        while categories["categories"]["total"] > count:
            page = self.__spotipy.categories(
                country=self.__user_country,
                limit=50,
                offset=count,
                locale=locale,
            )["categories"]["items"]
            if not page:
                break
            categories["categories"]["items"] += page
            count += len(page)

        result = []
        for item in categories["categories"]["items"]:
            if not item or not item.get("id"):
                continue
            thumb = "DefaultMusicGenre.png"
            for icon in item.get("icons") or []:
                thumb = icon.get("url") or thumb
                break
            result.append({"id": item["id"], "name": item.get("name") or "", "thumb": thumb})
        if result:
            self.cache.set(
                cache_str,
                result,
                checksum=checksum,
                expiration=EXPLORE_CATEGORIES_CACHE_EXPIRATION,
            )
        return result

    def __get_explore_categories(self) -> List[Tuple[Any, str, Union[str, Any]]]:
        return [
            (
                item["name"],
                f"plugin://{ADDON_ID}/"
                f"?action={self.browse_category.__name__}&applyfilter={item['id']}",
                item["thumb"],
            )
            for item in self.__get_category_list()
        ]

    def browse_main_explore(self) -> None:
        # Explore nodes.
        xbmcplugin.setContent(self.__addon_handle, "files")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(EXPLORE_STR_ID),
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
        checksum = self.__content_checksum("albumtracks")

        album_tracks = self.cache.get(cache_str, checksum=checksum)
        if album_tracks:
            cache_log(
                f'Retrieved {album["tracks"]["total"]} cached tracks for album "{album["name"]}".'
            )
        else:
            # GET /albums/{id} already embeds the first page of tracks; only page
            # album_tracks for whatever lies beyond it.
            album_track_page = album.get("tracks") or {}
            total = int(album_track_page.get("total") or 0)
            items = list(album_track_page.get("items") or [])
            count = len(items)
            while total > count:
                page = self.__spotipy.album_tracks(
                    album["id"], market=self.__user_country, limit=50, offset=count
                )["items"]
                if not page:
                    break
                items += page
                count += len(page)
            track_ids = [track["id"] for track in items if track and track.get("id")]
            # Every track references album_details; leave out the embedded
            # track page so it is not serialised once per track in the cache.
            album_details = {key: value for key, value in album.items() if key != "tracks"}
            album_tracks = self.__prepare_track_listitems(track_ids, album_details=album_details)
            self.cache.set(cache_str, album_tracks, checksum=checksum)
            cache_log(
                f'Retrieved {album["tracks"]["total"]} UNCACHED tracks for album "{album["name"]}".'
            )

        return album_tracks

    def browse_album(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")

        # Performance optimization: check cache first to avoid API call
        cache_str = f"spotify.album.{self.__album_id}"
        checksum = self.__content_checksum("album")
        album = self.cache.get(cache_str, checksum=checksum)

        if not album:
            album = self.__spotipy.album(self.__album_id, market=self.__user_country)
            self.cache.set(cache_str, album, checksum=checksum)

        xbmcplugin.setProperty(self.__addon_handle, "FolderName", album["name"])
        tracks = self.__get_album_tracks(album)
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

    def artist_top_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
        )

        # Performance optimization: check cache first to avoid API call
        cache_str = f"spotify.artisttoptracks.{self.__artist_id}"
        checksum = self.__content_checksum("artisttoptracks", self.__user_country)
        tracks_data = self.cache.get(cache_str, checksum=checksum)

        if tracks_data:
            cache_log(f'Retrieved cached top tracks for artist "{self.__artist_id}".')
            tracks = tracks_data
        else:
            tracks_result = self.__spotipy.artist_top_tracks(
                self.__artist_id, country=self.__user_country
            )
            tracks = self.__prepare_track_listitems(tracks=tracks_result["tracks"])
            self.cache.set(
                cache_str, tracks, checksum=checksum, expiration=ARTIST_CONTENT_CACHE_EXPIRATION
            )

        self.__add_track_listitems(tracks)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TRACKNUM)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def related_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
        )
        cache_str = f"spotify.relatedartists.{self.__artist_id}"
        checksum = self.__content_checksum("relatedartists")
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached related artists for "{self.__artist_id}".')
        else:
            artists = self.__spotipy.artist_related_artists(self.__artist_id)
            artists = self.__prepare_artist_listitems(artists["artists"])
            self.cache.set(
                cache_str, artists, checksum=checksum, expiration=ARTIST_CONTENT_CACHE_EXPIRATION
            )
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED related artists for "{self.__artist_id}".'
            )
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def browse_radio(self) -> None:
        """Show recommended tracks (radio station) from artist and/or track seed."""
        seed_artists = []
        seed_tracks = []
        if self.__artist_id:
            seed_artists = [self.__artist_id]
        if self.__track_id:
            seed_tracks = [self.__track_id]
        if not seed_artists and not seed_tracks:
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            return
        try:
            result = self.__spotipy.recommendations(
                seed_artists=seed_artists if seed_artists else None,
                seed_tracks=seed_tracks if seed_tracks else None,
                limit=50,
                country=self.__user_country,
            )
        except Exception as exc:
            log_exception(exc, "browse_radio recommendations failed")
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            return
        tracks = result.get("tracks") or []
        if not tracks:
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            return
        if self.__artist_name:
            folder_name = f"{self.__artist_name} {self.__addon.getLocalizedString(RADIO_STR_ID)}"
        else:
            folder_name = self.__addon.getLocalizedString(RADIO_STR_ID)
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", folder_name)
        prepared = self.__prepare_track_listitems(tracks=tracks)
        self.__add_track_listitems(prepared, True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ARTIST)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_playlist_summary(self, playlist_id: str) -> Playlist:
        return self.__spotipy.playlist(
            playlist_id,
            fields="tracks(total),name,description,images,owner(id),id,snapshot_id",
            market=self.__user_country,
        )

    def __get_playlist_items_page(
        self, playlist_id: str, offset: int = 0, limit: int = 50
    ) -> List[Dict[str, Any]]:
        result = self.__spotipy.playlist_items(
            playlist_id,
            market=self.__user_country,
            fields="",
            limit=limit,
            offset=offset,
        )
        return result.get("items") or []

    def __prepare_playlist_items_page(
        self,
        playlist: Playlist,
        raw_items: List[Dict[str, Any]],
        include_context_items: bool = True,
        include_artist_fanart: bool = True,
    ) -> List[Dict[str, Any]]:
        return self.__prepare_track_listitems(
            tracks=raw_items,
            playlist_details=playlist,
            include_context_items=include_context_items,
            include_artist_fanart=include_artist_fanart,
        )

    def __start_playlist_details_continuation(
        self,
        playlist: Playlist,
        cache_str: str,
        checksum: str,
        target_url: str,
        prepared_items: List[Dict[str, Any]],
        loaded: int,
        total: int,
    ) -> None:
        if total <= loaded:
            return

        def _continue_playlist_details():
            monitor = xbmc.Monitor()
            all_items = list(prepared_items)
            offset = loaded
            unsaved_pages = 0
            persisted = [len(all_items)]  # items already stored in the chunk rows

            def _persist():
                self.__store_playlist_details(
                    cache_str, playlist, all_items, checksum, first_dirty_item=persisted[0]
                )
                persisted[0] = len(all_items)

            def _fetch(page_offset: int):
                raw = self.__get_playlist_items_page(
                    playlist["id"], offset=page_offset, limit=DYNAMIC_PAGE_LIMIT
                )
                return raw, (self.__prepare_playlist_items_page(playlist, raw) if raw else [])

            try:
                for _page_offset, (raw_items, prepared) in self.__iter_pages_in_order(
                    loaded, total, _fetch, monitor
                ):
                    if not raw_items:
                        break
                    all_items += prepared
                    offset += len(raw_items)
                    playlist["tracks"]["items"] = all_items
                    self.__mark_dynamic_collection_state(
                        playlist["tracks"], offset, total, total <= offset
                    )
                    unsaved_pages += 1
                    if unsaved_pages >= PAGED_CACHE_WRITE_EVERY_PAGES:
                        _persist()
                        unsaved_pages = 0

                if monitor.abortRequested():
                    return
                self.__mark_dynamic_collection_state(playlist["tracks"], offset, total, True)
                _persist()
                unsaved_pages = 0
                self.__refresh_active_listing(target_url)
            finally:
                if unsaved_pages:
                    _persist()

        self.__start_dynamic_page_continuation(cache_str, target_url, _continue_playlist_details)

    def __start_playlist_collection_continuation(
        self,
        cache_str: str,
        checksum: str,
        container: Dict[str, Any],
        fetch_page: Callable[[int], List[Dict[str, Any]]],
        target_url: str,
        group_label: str = "",
        relation_mode: str = "cache",
    ) -> None:
        collection = container["playlists"]
        total = int(collection.get("total") or 0)
        loaded = int(
            collection.get(DYNAMIC_PAGING_LOADED_KEY) or len(collection.get("items") or [])
        )
        if total <= loaded:
            return

        def _continue_playlist_collection():
            monitor = xbmc.Monitor()
            all_items = list(collection.get("items") or [])
            offset = loaded
            unsaved_pages = 0

            def _persist():
                self.__paged_cache_set(
                    cache_str,
                    container,
                    checksum=checksum,
                    expiration=PLAYLIST_COLLECTION_CACHE_EXPIRATION,
                )

            try:
                while total > offset:
                    if monitor.abortRequested():
                        return
                    raw_items = fetch_page(offset)
                    if not raw_items:
                        break
                    all_items += self.__prepare_playlist_listitems(
                        raw_items, group_label=group_label, relation_mode=relation_mode
                    )
                    offset += len(raw_items)
                    collection["items"] = all_items
                    self.__mark_dynamic_collection_state(collection, offset, total, total <= offset)
                    unsaved_pages += 1
                    if unsaved_pages >= PAGED_CACHE_WRITE_EVERY_PAGES:
                        _persist()
                        unsaved_pages = 0

                self.__mark_dynamic_collection_state(collection, offset, total, True)
                _persist()
                unsaved_pages = 0
                self.__refresh_active_listing(target_url)
            finally:
                if unsaved_pages:
                    _persist()

        self.__start_dynamic_page_continuation(cache_str, target_url, _continue_playlist_collection)

    def __start_album_collection_continuation(
        self,
        cache_str: str,
        checksum: str,
        container: Dict[str, Any],
        fetch_page: Callable[[int], List[Dict[str, Any]]],
        target_url: str,
    ) -> None:
        collection = container["albums"]
        total = int(collection.get("total") or 0)
        loaded = int(
            collection.get(DYNAMIC_PAGING_LOADED_KEY) or len(collection.get("items") or [])
        )
        if total <= loaded:
            return

        def _continue_album_collection():
            monitor = xbmc.Monitor()
            all_items = list(collection.get("items") or [])
            offset = loaded
            unsaved_pages = 0

            def _persist():
                self.__paged_cache_set(
                    cache_str,
                    container,
                    checksum=checksum,
                    expiration=PLAYLIST_COLLECTION_CACHE_EXPIRATION,
                )

            try:
                while total > offset:
                    if monitor.abortRequested():
                        return
                    raw_items = fetch_page(offset)
                    if not raw_items:
                        break
                    all_items += self.__prepare_album_listitems(albums=raw_items)
                    offset += len(raw_items)
                    collection["items"] = all_items
                    self.__mark_dynamic_collection_state(collection, offset, total, total <= offset)
                    unsaved_pages += 1
                    if unsaved_pages >= PAGED_CACHE_WRITE_EVERY_PAGES:
                        _persist()
                        unsaved_pages = 0

                self.__mark_dynamic_collection_state(collection, offset, total, True)
                _persist()
                unsaved_pages = 0
                self.__refresh_active_listing(target_url)
            finally:
                if unsaved_pages:
                    _persist()

        self.__start_dynamic_page_continuation(cache_str, target_url, _continue_album_collection)

    def __get_playlist_details(self, playlist_id: str) -> Playlist:
        playlist = self.__get_playlist_summary(playlist_id)
        cache_str = f"spotify.playlistdetails.{playlist['id']}"
        is_spotify_curated = playlist.get("owner", {}).get("id") == "spotify"
        content_version = playlist.get("snapshot_id") or playlist["tracks"]["total"]
        # Spotify-curated playlists can lag their snapshot_id, so use a short
        # time bucket. That keeps dynamic paging useful without pinning Daylist
        # or Daily Mixes behind a 30-day cache.
        curated_bucket = f"-curated-{int(time.time() // 300)}" if is_spotify_curated else ""
        playlist_checksum = f"{content_version}-{playlist.get('snapshot_id', '')}{curated_bucket}"
        # Keyed on the playlist's own version only (snapshot_id), not library
        # totals: liking a track must not invalidate every playlist cache.
        checksum = self.__content_checksum("playlist", playlist_checksum)
        cached = self.__chunked_cache_get(cache_str, checksum)
        playlist_details, cached_items = cached if cached else (None, None)
        if isinstance(playlist_details, dict) and isinstance(playlist_details.get("tracks"), dict):
            playlist_details["tracks"]["items"] = cached_items
        else:
            playlist_details = None
        expected_total = playlist["tracks"]["total"] or 0
        target_url = (
            self.__current_request_url() if self.__action == self.browse_playlist.__name__ else ""
        )
        if (
            playlist_details
            and isinstance(cached_items, list)
            and (expected_total == 0 or len(cached_items) > 0)
        ):
            cache_log(
                f"Retrieved {len(cached_items)} cached playlist details"
                f' for "{playlist["name"]}".'
            )
            if not playlist_details["tracks"].get(DYNAMIC_PAGING_COMPLETE_KEY):
                self.__start_playlist_details_continuation(
                    playlist_details,
                    cache_str,
                    checksum,
                    target_url,
                    cached_items,
                    int(
                        playlist_details["tracks"].get(DYNAMIC_PAGING_LOADED_KEY)
                        or len(cached_items)
                    ),
                    expected_total,
                )
        else:
            raw_playlist_items = self.__get_playlist_items_page(
                playlist["id"], offset=0, limit=DYNAMIC_PAGE_LIMIT
            )
            loaded = len(raw_playlist_items)
            playlist_details = playlist
            playlist_details["tracks"]["items"] = self.__prepare_playlist_items_page(
                playlist, raw_playlist_items
            )
            self.__mark_dynamic_collection_state(
                playlist_details["tracks"],
                loaded,
                expected_total,
                expected_total <= loaded,
            )
            self.__store_playlist_details(
                cache_str, playlist_details, playlist_details["tracks"]["items"], checksum
            )
            cache_log(
                f"Retrieved first {loaded}/{expected_total} playlist details"
                f' for "{playlist["name"]}".'
            )
            self.__start_playlist_details_continuation(
                playlist_details,
                cache_str,
                checksum,
                target_url,
                playlist_details["tracks"]["items"],
                loaded,
                expected_total,
            )

        return playlist_details

    def __store_playlist_details(
        self,
        cache_str: str,
        playlist: Dict[str, Any],
        items: List[Dict[str, Any]],
        checksum: str,
        first_dirty_item: int = 0,
    ) -> None:
        head = dict(playlist)
        head["tracks"] = {
            key: value for key, value in (playlist.get("tracks") or {}).items() if key != "items"
        }
        self.__chunked_cache_set(
            cache_str, head, items, checksum, first_dirty_item=first_dirty_item
        )

    def browse_playlist(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        playlist_details = self.__get_playlist_details(self.__playlist_id)
        xbmcplugin.setPluginCategory(self.__addon_handle, playlist_details.get("name", ""))
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", playlist_details["name"])
        items = playlist_details["tracks"]["items"]
        self.__add_track_listitems(items, True, playlist_details=playlist_details)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def play_playlist(self) -> None:
        """Play entire playlist: start first page immediately, queue rest in background.

        Opens a play-queue session (see ``play_queue``) so the service knows
        when the original playlist is fully loaded and only then considers
        firing autoplay. Without this coordination the service used to see
        a transient "no next item" gap during background paging and fire
        autoplay destructively mid-load.
        """
        playlist_details = self.__get_playlist_summary(self.__playlist_id)
        total = int(playlist_details.get("tracks", {}).get("total") or 0)
        # Start session BEFORE we touch Kodi's playlist so the service sees
        # original_complete=false the moment it receives the first
        # onPlayBackStarted callback. If the playlist fits in one page we
        # mark complete at session start (handled inside start_session when
        # total <= 0, otherwise mark_original_complete below after paging).
        play_queue_start_session(total)
        page_limit = 50
        try:
            raw_items = self.__get_playlist_items_page(
                playlist_details["id"], offset=0, limit=page_limit
            )
            items = self.__prepare_playlist_items_page(
                playlist_details,
                raw_items,
                include_context_items=False,
                include_artist_fanart=False,
            )
        except Exception:
            # Never leave the service believing the original is still loading.
            play_queue_mark_original_complete()
            raise
        if not items:
            log_msg(f"Playlist '{playlist_details.get('name', '')}' has no playable tracks.")
            play_queue_mark_original_complete()
            self.__end_directory(succeeded=False)
            return
        log_msg(f"Start playing playlist '{playlist_details['name']}'.")

        kodi_playlist = xbmc.PlayList(0)
        kodi_playlist.clear()

        loaded_count = 0
        for track in items:
            item = self.__get_track_item(track, True)
            if item is not None:
                url, li = item
                kodi_playlist.add(url, li)
                loaded_count += 1
        play_queue_report_loaded(loaded_count)

        xbmc.Player().play(kodi_playlist)

        next_offset = len(raw_items)
        # Fit-in-one-page path: nothing more to page, original is complete.
        if total <= next_offset:
            play_queue_mark_original_complete()
            return

        def add_remaining():
            monitor = xbmc.Monitor()
            offset = next_offset
            nonlocal_loaded = loaded_count
            try:
                while total > offset:
                    if monitor.abortRequested():
                        return
                    raw_page = self.__get_playlist_items_page(
                        playlist_details["id"], offset=offset, limit=page_limit
                    )
                    if not raw_page:
                        return
                    tracks = self.__prepare_playlist_items_page(
                        playlist_details,
                        raw_page,
                        include_context_items=False,
                        include_artist_fanart=False,
                    )
                    page_added = 0
                    for track in tracks:
                        if monitor.abortRequested():
                            return
                        try:
                            item = self.__get_track_item(track, True)
                            if item is not None:
                                u, listitem = item
                                kodi_playlist.add(u, listitem)
                                page_added += 1
                        except Exception:
                            pass
                        xbmc.sleep(2)
                    offset += len(raw_page)
                    nonlocal_loaded += page_added
                    play_queue_report_loaded(nonlocal_loaded)
            finally:
                # Always mark complete when the paging loop exits, whether it
                # finished naturally, hit abort, or got an empty page. Without
                # this, an early exit leaves the service stuck thinking the
                # original is still loading and autoplay never fires.
                play_queue_mark_original_complete()

        t = threading.Thread(target=add_remaining, daemon=True)
        t.start()

    def __get_category(self, categoryid: str) -> Playlist:
        categoryid = self.__resolve_category_id(categoryid)
        cache_str = f"spotify.categoryplaylists.{categoryid}"
        checksum = self.__playlist_collection_checksum("category", categoryid)
        cached = self.__paged_cache_get(cache_str, checksum=checksum)
        if cached and (cached.get("playlists") or {}).get("items"):
            self.__start_playlist_collection_continuation(
                cache_str,
                checksum,
                cached,
                lambda offset: self.__spotipy.category_playlists(
                    categoryid,
                    country=self.__user_country,
                    limit=DYNAMIC_PAGE_LIMIT,
                    offset=offset,
                )["playlists"]["items"],
                self.__current_request_url(),
                group_label=cached.get("category") or "",
            )
            return cached

        try:
            category = self.__spotipy.category(
                categoryid, country=self.__user_country, locale=self.__user_country
            )
            playlists = self.__spotipy.category_playlists(
                categoryid,
                country=self.__user_country,
                limit=DYNAMIC_PAGE_LIMIT,
                offset=0,
            )
        except Exception as exc:
            cached = self.__paged_cache_get(cache_str)
            if cached and (cached.get("playlists") or {}).get("items"):
                log_exception(exc, f"category playlists lookup {categoryid}")
                return cached
            raise

        playlists["category"] = category["name"]
        total = playlists["playlists"]["total"]
        loaded = len(playlists["playlists"]["items"])
        playlists["playlists"]["items"] = self.__prepare_playlist_listitems(
            playlists["playlists"]["items"], group_label=playlists["category"]
        )
        self.__mark_dynamic_collection_state(playlists["playlists"], loaded, total, total <= loaded)
        self.__paged_cache_set(
            cache_str,
            playlists,
            checksum=checksum,
            expiration=PLAYLIST_COLLECTION_CACHE_EXPIRATION,
        )
        self.__start_playlist_collection_continuation(
            cache_str,
            checksum,
            playlists,
            lambda offset: self.__spotipy.category_playlists(
                categoryid,
                country=self.__user_country,
                limit=DYNAMIC_PAGE_LIMIT,
                offset=offset,
            )["playlists"]["items"],
            self.__current_request_url(),
            group_label=playlists["category"],
        )

        return playlists

    def __resolve_category_id(self, categoryid: str) -> str:
        alias_name = LEGACY_CATEGORY_ALIASES.get(categoryid)
        if not alias_name:
            return categoryid

        target_label = _normalized_lookup_label(alias_name)
        try:
            for item in self.__get_category_list():
                if _normalized_lookup_label(item.get("name") or "") == target_label:
                    return item["id"]
        except Exception as exc:
            cache_log(f"Could not resolve legacy category alias {categoryid}: {exc}")

        return categoryid

    def browse_category(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        playlists = self.__get_category(self.__filter)
        self.__add_playlist_listitems(
            playlists["playlists"]["items"], group_label=playlists["category"]
        )
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", playlists["category"])
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def follow_playlist(self) -> None:
        self.__spotipy.current_user_follow_playlist(self.__playlist_id)
        self.__after_relation_change("followedplaylist", self.__playlist_id, True)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmc.executebuiltin("Container.Refresh")

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
        # The playlist snapshot_id changes, which invalidates its cached details.
        xbmc.executebuiltin("Container.Refresh")

    def unfollow_playlist(self) -> None:
        self.__spotipy.current_user_unfollow_playlist(self.__playlist_id)
        self.__after_relation_change("followedplaylist", self.__playlist_id, False)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmc.executebuiltin("Container.Refresh")

    def follow_artist(self) -> None:
        self.__spotipy.user_follow_artists([self.__artist_id])
        self.__after_relation_change("followedartist", self.__artist_id, True)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmc.executebuiltin("Container.Refresh")

    def unfollow_artist(self) -> None:
        self.__spotipy.user_unfollow_artists([self.__artist_id])
        self.__after_relation_change("followedartist", self.__artist_id, False)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmc.executebuiltin("Container.Refresh")

    def save_album(self) -> None:
        self.__spotipy.current_user_saved_albums_add([self.__album_id])
        self.__after_relation_change("savedalbum", self.__album_id, True)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmc.executebuiltin("Container.Refresh")

    def remove_album(self) -> None:
        self.__spotipy.current_user_saved_albums_delete([self.__album_id])
        self.__after_relation_change("savedalbum", self.__album_id, False)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmc.executebuiltin("Container.Refresh")

    def save_track(self) -> None:
        self.__spotipy.current_user_saved_tracks_add([self.__track_id])
        self.__after_relation_change("savedtrack", self.__track_id, True)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmc.executebuiltin("Container.Refresh")

    def remove_track(self) -> None:
        self.__spotipy.current_user_saved_tracks_delete([self.__track_id])
        self.__after_relation_change("savedtrack", self.__track_id, False)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmc.executebuiltin("Container.Refresh")

    def __get_featured_playlists(self) -> Playlist:
        cache_str = "spotify.featuredplaylists"
        checksum = self.__playlist_collection_checksum("featured", self.__user_country)
        cached = self.__paged_cache_get(cache_str, checksum=checksum)
        if cached and (cached.get("playlists") or {}).get("items"):
            self.__start_playlist_collection_continuation(
                cache_str,
                checksum,
                cached,
                lambda offset: self.__spotipy.featured_playlists(
                    country=self.__user_country,
                    limit=DYNAMIC_PAGE_LIMIT,
                    offset=offset,
                )["playlists"]["items"],
                self.__current_request_url(),
                group_label=cached.get("message") or "",
            )
            return cached

        try:
            playlists = self.__spotipy.featured_playlists(
                country=self.__user_country, limit=DYNAMIC_PAGE_LIMIT, offset=0
            )
        except Exception as exc:
            cached = self.__paged_cache_get(cache_str)
            if cached and (cached.get("playlists") or {}).get("items"):
                log_exception(exc, "featured playlists lookup")
                return cached
            raise

        total = playlists["playlists"]["total"]
        loaded = len(playlists["playlists"]["items"])
        playlists["playlists"]["items"] = self.__prepare_playlist_listitems(
            playlists["playlists"]["items"], group_label=playlists["message"]
        )
        self.__mark_dynamic_collection_state(playlists["playlists"], loaded, total, total <= loaded)
        self.__paged_cache_set(
            cache_str,
            playlists,
            checksum=checksum,
            expiration=PLAYLIST_COLLECTION_CACHE_EXPIRATION,
        )
        self.__start_playlist_collection_continuation(
            cache_str,
            checksum,
            playlists,
            lambda offset: self.__spotipy.featured_playlists(
                country=self.__user_country,
                limit=DYNAMIC_PAGE_LIMIT,
                offset=offset,
            )["playlists"]["items"],
            self.__current_request_url(),
            group_label=playlists["message"],
        )

        return playlists

    def __get_user_playlists(self, userid):
        cache_str = f"spotify.userplaylists.{userid}"
        checksum = self.__user_playlists_checksum(userid)

        cached_playlists = self.__paged_cache_get(cache_str, checksum=checksum)
        if isinstance(cached_playlists, dict):
            items = cached_playlists.get("items") or []
            cache_log(f'Retrieved {len(items)} cached playlists for user "{self.__userid}".')
            self.__start_user_playlist_continuation(userid, cache_str, checksum, cached_playlists)
            return items
        if cached_playlists:
            cache_log(
                f'Retrieved {len(cached_playlists)} legacy cached playlists for user "{self.__userid}".'
            )
            return cached_playlists

        playlists = self.__spotipy.user_playlists(userid, limit=DYNAMIC_PAGE_LIMIT, offset=0)
        total = playlists["total"]
        loaded = len(playlists["items"])
        result = self.__prepare_playlist_listitems(
            playlists["items"],
            group_label=xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID),
            relation_mode="user_collection",
        )
        payload = {
            "items": result,
            "total": total,
            DYNAMIC_PAGING_LOADED_KEY: loaded,
            DYNAMIC_PAGING_COMPLETE_KEY: total <= loaded,
        }
        self.__paged_cache_set(
            cache_str, payload, checksum=checksum, expiration=USER_PLAYLIST_CACHE_EXPIRATION
        )
        cache_log(
            f'Retrieved first {_get_len(result)}/{total} playlists for user "{self.__userid}".'
        )
        self.__start_user_playlist_continuation(userid, cache_str, checksum, payload)

        return result

    def __start_user_playlist_continuation(
        self, userid: str, cache_str: str, checksum: str, payload: Dict[str, Any]
    ) -> None:
        total = int(payload.get("total") or 0)
        loaded = int(payload.get(DYNAMIC_PAGING_LOADED_KEY) or len(payload.get("items") or []))
        if total <= loaded or payload.get(DYNAMIC_PAGING_COMPLETE_KEY):
            return

        def _continue_user_playlists():
            monitor = xbmc.Monitor()
            all_items = list(payload.get("items") or [])
            offset = loaded
            unsaved_pages = 0

            def _persist():
                self.__paged_cache_set(
                    cache_str,
                    payload,
                    checksum=checksum,
                    expiration=USER_PLAYLIST_CACHE_EXPIRATION,
                )

            try:
                while total > offset:
                    if monitor.abortRequested():
                        return
                    page = self.__spotipy.user_playlists(
                        userid, limit=DYNAMIC_PAGE_LIMIT, offset=offset
                    )["items"]
                    if not page:
                        break
                    all_items += self.__prepare_playlist_listitems(
                        page,
                        group_label=xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID),
                        relation_mode="user_collection",
                    )
                    offset += len(page)
                    payload["items"] = all_items
                    payload[DYNAMIC_PAGING_LOADED_KEY] = offset
                    payload[DYNAMIC_PAGING_COMPLETE_KEY] = total <= offset
                    unsaved_pages += 1
                    if unsaved_pages >= PAGED_CACHE_WRITE_EVERY_PAGES:
                        _persist()
                        unsaved_pages = 0

                payload[DYNAMIC_PAGING_LOADED_KEY] = offset
                payload[DYNAMIC_PAGING_COMPLETE_KEY] = True
                _persist()
                unsaved_pages = 0
                target_url = (
                    self.__current_request_url()
                    if self.__action == self.browse_playlists.__name__
                    else ""
                )
                self.__refresh_active_listing(target_url)
            finally:
                if unsaved_pages:
                    _persist()

        self.__start_dynamic_page_continuation(
            cache_str, self.__current_request_url(), _continue_user_playlists
        )

    def browse_playlists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        if self.__filter == "featured":
            playlist_container = self.__get_featured_playlists()
            group_label = playlist_container["message"]
            xbmcplugin.setProperty(self.__addon_handle, "FolderName", group_label)
            playlists = playlist_container["playlists"]["items"]
        else:
            group_label = xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID)
            xbmcplugin.setProperty(
                self.__addon_handle,
                "FolderName",
                group_label,
            )
            playlists = self.__get_user_playlists(self.__owner_id)

        self.__add_playlist_listitems(playlists, group_label=group_label)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_new_releases(self):
        cache_str = "spotify.newreleases"
        checksum = self.__paged_collection_checksum("newreleases", self.__user_country)
        cached = self.__paged_cache_get(cache_str, checksum=checksum)
        if cached and (cached.get("albums") or {}).get("items"):
            self.__start_album_collection_continuation(
                cache_str,
                checksum,
                cached,
                lambda offset: self.__spotipy.new_releases(
                    country=self.__user_country,
                    limit=DYNAMIC_PAGE_LIMIT,
                    offset=offset,
                )["albums"]["items"],
                self.__current_request_url(),
            )
            return cached["albums"]["items"]

        albums = self.__spotipy.new_releases(
            country=self.__user_country, limit=DYNAMIC_PAGE_LIMIT, offset=0
        )
        total = albums["albums"]["total"]
        loaded = len(albums["albums"]["items"])
        albums["albums"]["items"] = self.__prepare_album_listitems(albums=albums["albums"]["items"])
        self.__mark_dynamic_collection_state(albums["albums"], loaded, total, total <= loaded)
        self.__paged_cache_set(
            cache_str,
            albums,
            checksum=checksum,
            expiration=PLAYLIST_COLLECTION_CACHE_EXPIRATION,
        )
        self.__start_album_collection_continuation(
            cache_str,
            checksum,
            albums,
            lambda offset: self.__spotipy.new_releases(
                country=self.__user_country,
                limit=DYNAMIC_PAGE_LIMIT,
                offset=offset,
            )["albums"]["items"],
            self.__current_request_url(),
        )

        return albums["albums"]["items"]

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
        self,
        track_ids=None,
        tracks=None,
        playlist_details=None,
        album_details=None,
        include_context_items: bool = True,
        include_artist_fanart: bool = True,
        known_saved_track_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        if tracks is None:
            tracks = []
        if track_ids is None:
            track_ids = []

        new_tracks: List[Dict[str, Any]] = []

        # For tracks, we always get the full details unless full tracks already supplied.
        if track_ids and not tracks:
            # Add early exit condition
            for chunk in get_chunks(track_ids, 50):
                tracks += self.__spotipy.tracks(chunk, market=self.__user_country)["tracks"]

        for track in tracks:
            if not track:
                continue
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
                track["year"] = 0
            else:
                track["genre"] = " / ".join(track["album"].get("genres", []))
                release_date = track["album"].get("release_date") or ""
                year_str = release_date.split("-")[0] if release_date else ""
                track["year"] = int(year_str) if year_str.isdigit() else 0

            track["rating"] = int(self.__get_track_rating(int(track.get("popularity", "0"))))

            new_tracks.append(track)

        if include_context_items:
            track_ids_for_context = [t.get("id") for t in new_tracks if t.get("id")]
            if known_saved_track_ids is None:
                saved_track_ids = self.__get_saved_track_ids_for_page(track_ids_for_context)
            else:
                saved_track_ids = {
                    track_id
                    for track_id in track_ids_for_context
                    if track_id in known_saved_track_ids
                }
                self.__set_relation_cache_many(
                    "savedtrack", {track_id: True for track_id in saved_track_ids}
                )
            followed_artists = self.__get_followed_artist_ids_for_page(
                [t.get("artistid") for t in new_tracks if t.get("artistid")]
            )
            # Relation states only; context menus are built at render time.
            relation_ts = time.time()
            for track in new_tracks:
                track["saved"] = track["id"] in saved_track_ids
                if track.get("artistid"):
                    track["artist_followed"] = track["artistid"] in followed_artists
                track[RELATION_SNAPSHOT_KEY] = relation_ts

        if include_artist_fanart:
            artist_fanart_map = self.__get_artist_fanart_for_ids(
                list(
                    OrderedDict.fromkeys(t.get("artistid") for t in new_tracks if t.get("artistid"))
                )
            )
            for t in new_tracks:
                t["artist_fanart"] = artist_fanart_map.get(t.get("artistid") or "", "")

        return [_lean_track(track) for track in new_tracks]

    def __artist_fanart_key(self, artist_id: str) -> str:
        return f"spotify.artistfanart.{artist_id}"

    def __get_artist_fanart_for_ids(self, artist_ids: List[str]) -> Dict[str, str]:
        """artist id -> largest image URL: process memo, then simplecache, then the API.

        The persisted rows are database-only (no home-window mirror) and live
        ARTIST_FANART_PERSIST_EXPIRATION, so other plugin processes reuse them.
        """
        artist_fanart_map: Dict[str, str] = {}
        if not artist_ids:
            return artist_fanart_map
        # Background paging prepares pages on several threads at once.
        with _ARTIST_FANART_MEMO_LOCK:
            if not hasattr(self, "_artist_fanart_cache"):
                self._artist_fanart_cache = OrderedDict()
            elif not isinstance(self._artist_fanart_cache, OrderedDict):
                self._artist_fanart_cache = OrderedDict(self._artist_fanart_cache)
            memo = self._artist_fanart_cache

            missing_artist_ids = []
            for artist_id in artist_ids:
                if artist_id in memo:
                    artist_fanart_map[artist_id] = memo[artist_id]
                    memo.move_to_end(artist_id)
                else:
                    missing_artist_ids.append(artist_id)

        if missing_artist_ids:
            keys = {
                self.__artist_fanart_key(artist_id): artist_id for artist_id in missing_artist_ids
            }
            try:
                persisted = (
                    self.cache.get_many(
                        list(keys), checksum=ARTIST_FANART_PERSIST_CHECKSUM, mem_cache=False
                    )
                    or {}
                )
            except Exception as exc:
                log_exception(exc, "artist fanart cache read")
                persisted = {}
            found = {keys[key]: value or "" for key, value in persisted.items() if key in keys}
            to_fetch = [artist_id for artist_id in missing_artist_ids if artist_id not in found]
            if to_fetch:
                fetched = self.__get_artist_fanart_map(to_fetch)
                if fetched:
                    try:
                        self.cache.set_many(
                            {
                                self.__artist_fanart_key(artist_id): fanart
                                for artist_id, fanart in fetched.items()
                            },
                            checksum=ARTIST_FANART_PERSIST_CHECKSUM,
                            expiration=ARTIST_FANART_PERSIST_EXPIRATION,
                            mem_cache=False,
                        )
                    except Exception as exc:
                        log_exception(exc, "artist fanart cache write")
                found.update(fetched)
            with _ARTIST_FANART_MEMO_LOCK:
                for artist_id, fanart in found.items():
                    artist_fanart_map[artist_id] = fanart
                    memo[artist_id] = fanart
                    memo.move_to_end(artist_id)
                while len(memo) > ARTIST_FANART_CACHE_MAX_ITEMS:
                    memo.popitem(last=False)

        return artist_fanart_map

    def __get_artist_fanart_map(self, artist_ids: List[str]) -> Dict[str, str]:
        """Fetch full artist objects (GET /artists/) and return artist_id -> largest image URL
        ("" for artists without images). Used for Artist slideshow / Music OSD background."""
        result: Dict[str, str] = {}
        if not artist_ids:
            return result
        try:
            for chunk in get_chunks(artist_ids, 50):
                artists = self.__spotipy.artists(chunk).get("artists") or []
                for artist in artists:
                    if not artist or not artist.get("id"):
                        continue
                    images = artist.get("images") or []
                    # Spotify: images sorted by width descending; [0]=largest
                    result[artist["id"]] = (images[0].get("url") or "") if images else ""
        except Exception as e:
            log_exception(e, "artist fanart fetch")
        return result

    def __get_playlist_track_context_menu_items(
        self,
        track: Dict[str, Any],
        is_saved: bool,
        playlist_details: Optional[Dict[str, Any]],
        artist_followed: bool,
    ) -> List[Tuple[str, str]]:
        """Track context menu, built at render time from the row's relation states."""
        # Use original track id for actions when the track was relinked.
        linked_from = track.get("linked_from") or {}
        if linked_from.get("id"):
            real_track_id = linked_from["id"]
            real_track_uri = linked_from.get("uri") or f"spotify:track:{real_track_id}"
        else:
            real_track_id = track["id"]
            real_track_uri = track.get("uri") or f"spotify:track:{real_track_id}"
        loc = self.__localized
        plugin = f"plugin://{ADDON_ID}/"

        context_items = []

        if is_saved:
            context_items.append(
                (
                    loc(REMOVE_FROM_LIKED_SONGS_STR_ID),
                    f"RunPlugin({plugin}?action=remove_track&trackid={real_track_id})",
                )
            )
        else:
            context_items.append(
                (
                    loc(ADD_TO_LIKED_SONGS_STR_ID),
                    f"RunPlugin({plugin}?action=save_track&trackid={real_track_id})",
                )
            )

        if playlist_details and (playlist_details.get("owner") or {}).get("id") == self.__userid:
            context_items.append(
                (
                    f"{loc(REMOVE_FROM_PLAYLIST_STR_ID)} {playlist_details.get('name', '')}",
                    f"RunPlugin({plugin}?action=remove_track_from_playlist&trackid="
                    f"{real_track_uri}&playlistid={playlist_details['id']})",
                )
            )

        context_items.append(
            (
                self.__kodi_localized(KODI_ADD_TO_PLAYLIST_STR_ID),
                f"RunPlugin({plugin}?action=add_track_to_playlist&trackid={real_track_uri})",
            )
        )

        artist_id = track.get("artistid")
        if artist_id:
            context_items += [
                (
                    loc(ARTIST_TOP_TRACKS_STR_ID),
                    f"Container.Update({plugin}?action=artist_top_tracks&artistid={artist_id})",
                ),
                (
                    loc(ALL_ALBUMS_FOR_ARTIST_STR_ID),
                    f"Container.Update({plugin}"
                    f"?action=browse_artist_just_albums&artistid={artist_id})",
                ),
                (
                    loc(ALL_SINGLES_FOR_ARTIST_STR_ID),
                    f"Container.Update({plugin}"
                    f"?action=browse_artist_just_singles&artistid={artist_id})",
                ),
                (
                    loc(ALL_APPEARS_ON_FOR_ARTIST_STR_ID),
                    f"Container.Update({plugin}"
                    f"?action=browse_artist_just_appears_on&artistid={artist_id})",
                ),
                (
                    loc(EVERYTHING_FOR_ARTIST_STR_ID),
                    f"Container.Update({plugin}"
                    f"?action=browse_artist_everything&artistid={artist_id})",
                ),
            ]

            if artist_followed:
                context_items.append(
                    (
                        loc(UNFOLLOW_ARTIST_STR_ID),
                        f"RunPlugin({plugin}?action=unfollow_artist&artistid={artist_id})",
                    )
                )
            else:
                context_items.append(
                    (
                        loc(FOLLOW_ARTIST_STR_ID),
                        f"RunPlugin({plugin}?action=follow_artist&artistid={artist_id})",
                    )
                )

            context_items.append(
                (
                    loc(RELATED_ARTISTS_STR_ID),
                    f"Container.Update({plugin}?action=related_artists&artistid={artist_id})",
                )
            )
            context_items.append(
                (
                    loc(GO_TO_RADIO_STR_ID),
                    f"Container.Update({plugin}"
                    f"?action=browse_radio&trackid={real_track_id}"
                    f"&artistid={artist_id}"
                    f"&artistname={urllib.parse.quote(track.get('artist', ''))})",
                )
            )

        context_items.append(
            (
                loc(REFRESH_LISTING_STR_ID),
                f"RunPlugin({plugin}?action=refresh_listing)",
            )
        )
        return context_items

    def __prepare_album_listitems(
        self,
        album_ids: List[str] = None,
        albums: List[Dict[str, Any]] = None,
        known_saved_album_ids: Optional[Set[str]] = None,
    ) -> List[Dict[str, Any]]:
        if albums is None:
            albums: List[Dict[str, Any]] = []
        if album_ids is None:
            album_ids = []
        if not albums and album_ids:
            # Get full info in chunks of 20 (the /albums endpoint maximum).
            for chunk in get_chunks(album_ids, 20):
                albums += self.__spotipy.albums(chunk, market=self.__user_country)["albums"]
        albums = [album for album in albums if album]

        page_album_ids = [album.get("id") for album in albums if album.get("id")]
        if known_saved_album_ids is None:
            saved_albums = self.__get_saved_album_ids_for_page(page_album_ids)
        else:
            saved_albums = {
                album_id for album_id in page_album_ids if album_id in known_saved_album_ids
            }
            self.__set_relation_cache_many(
                "savedalbum", {album_id: True for album_id in saved_albums}
            )
        relation_ts = time.time()

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
            for artist in track.get("artists") or []:
                artists.append(artist.get("name", ""))
            track["artist"] = " / ".join(artists) or ""
            track["genre"] = " / ".join(track.get("genres") or [])
            release_date = (track.get("release_date") or "")[:4]
            track["year"] = int(release_date) if release_date.isdigit() else 0
            track["rating"] = str(self.__get_track_rating(int(track.get("popularity", 0))))
            track["artistid"] = (track.get("artists") or [{}])[0].get("id", "")

            track["saved"] = track["id"] in saved_albums
            track[RELATION_SNAPSHOT_KEY] = relation_ts

        return albums

    def __get_album_track_context_menu_items(self, track, is_saved: bool) -> List[Tuple[str, str]]:
        loc = self.__localized
        plugin = f"plugin://{ADDON_ID}/"
        album_id = track["id"]
        artist_id = track.get("artistid", "")
        context_items = [
            (
                self.__kodi_localized(KODI_BROWSE_STR_ID),
                f"Container.Update({plugin}?action=browse_album&albumid={album_id})",
            ),
            (
                loc(ARTIST_TOP_TRACKS_STR_ID),
                f"Container.Update({plugin}?action=artist_top_tracks&artistid={artist_id})",
            ),
            (
                loc(EVERYTHING_FOR_ARTIST_STR_ID),
                f"Container.Update({plugin}?action=browse_artist_everything&artistid={artist_id})",
            ),
            (
                loc(RELATED_ARTISTS_STR_ID),
                f"Container.Update({plugin}?action=related_artists&artistid={artist_id})",
            ),
            (
                loc(GO_TO_RADIO_STR_ID),
                f"Container.Update({plugin}?action=browse_radio&trackid={album_id}"
                f"&artistid={artist_id}&artistname={urllib.parse.quote(track.get('artist', ''))})",
            ),
        ]

        if is_saved:
            context_items.append(
                (
                    loc(REMOVE_TRACKS_FROM_MY_MUSIC_STR_ID),
                    f"RunPlugin({plugin}?action=remove_album&albumid={album_id})",
                )
            )
        else:
            context_items.append(
                (
                    loc(SAVE_TRACKS_TO_MY_MUSIC_STR_ID),
                    f"RunPlugin({plugin}?action=save_album&albumid={album_id})",
                )
            )

        context_items.append(
            (loc(REFRESH_LISTING_STR_ID), f"RunPlugin({plugin}?action=refresh_listing)")
        )
        return context_items

    def __add_album_listitems(
        self, albums: List[Dict[str, Any]], append_artist_to_label: bool = False
    ) -> None:
        default_album_icon = os.path.join(self.__addon_icon_path, MUSIC_ALBUMS_ICON)
        self.__apply_relation_overrides(albums, self.ALBUM_RELATION_FIELDS)
        for track in albums:
            label = self.__get_track_name(track, append_artist_to_label)
            li = xbmcgui.ListItem(label, path=track["url"], offscreen=True)
            tag = li.getMusicInfoTag()
            tag.setTitle(track["name"])
            tag.setAlbum(track["name"])
            tag.setArtist(track.get("artist") or "")
            tag.setYear(int(track.get("year") or 0))
            tag.setRating(int(track.get("rating") or 0))
            tag.setMediaType("album")
            genre = track.get("genre") or ""
            if genre:
                tag.setGenres([genre] if isinstance(genre, str) else genre)
            li.setArt(_art_for_item(track.get("thumb") or "", default_album_icon))
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.addContextMenuItems(
                self.__get_album_track_context_menu_items(track, bool(track.get("saved"))), True
            )
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=track["url"], listitem=li, isFolder=True
            )

    def __prepare_artist_listitems(
        self, artists: List[Dict[str, Any]], is_followed: bool = False
    ) -> List[Dict[str, Any]]:
        artists = [a for a in artists if a]
        followed_artists: Set[str] = set()
        if not is_followed:
            followed_artists = self.__get_followed_artist_ids_for_page(
                [
                    (a.get("artist") or a).get("id")
                    for a in artists
                    if isinstance(a.get("artist") or a, dict)
                ]
            )
        relation_ts = time.time()
        for artist in artists:
            if artist.get("artist"):
                artist = artist["artist"]
            # Use largest (first) image only; API returns same image in various sizes, widest first
            if artist.get("images"):
                artist["thumb"] = artist["images"][0].get("url") or "DefaultMusicArtists.png"
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

            artist["followed"] = bool(is_followed or artist["id"] in followed_artists)
            artist[RELATION_SNAPSHOT_KEY] = relation_ts

        return artists

    def __get_artist_context_menu_items(self, artist, is_followed: bool) -> List[Tuple[str, str]]:
        loc = self.__localized
        plugin = f"plugin://{ADDON_ID}/"
        artist_id = artist["id"]
        context_items = [
            (
                self.__kodi_localized(ALL_ALBUMS_AND_SINGLES_FOR_ARTIST_STR_ID),
                f"Container.Update({artist['url']})",
            ),
            (
                loc(ALL_ALBUMS_FOR_ARTIST_STR_ID),
                f"Container.Update({plugin}?action=browse_artist_just_albums&artistid={artist_id})",
            ),
            (
                loc(ALL_SINGLES_FOR_ARTIST_STR_ID),
                f"Container.Update({plugin}?action=browse_artist_just_singles&artistid={artist_id})",
            ),
            (
                loc(ALL_APPEARS_ON_FOR_ARTIST_STR_ID),
                f"Container.Update({plugin}"
                f"?action=browse_artist_just_appears_on&artistid={artist_id})",
            ),
            (
                loc(ARTIST_TOP_TRACKS_STR_ID),
                f"Container.Update({plugin}?action=artist_top_tracks&artistid={artist_id})",
            ),
            (
                loc(GO_TO_RADIO_STR_ID),
                f"Container.Update({plugin}?action=browse_radio&artistid={artist_id}"
                f"&artistname={urllib.parse.quote(artist.get('name', ''))})",
            ),
        ]

        if is_followed:
            context_items.append(
                (
                    loc(UNFOLLOW_ARTIST_STR_ID),
                    f"RunPlugin({plugin}?action=unfollow_artist&artistid={artist_id})",
                )
            )
        else:
            context_items.append(
                (
                    loc(FOLLOW_ARTIST_STR_ID),
                    f"RunPlugin({plugin}?action=follow_artist&artistid={artist_id})",
                )
            )

        context_items.append(
            (
                loc(RELATED_ARTISTS_STR_ID),
                f"Container.Update({plugin}?action=related_artists&artistid={artist_id})",
            )
        )

        return context_items

    def __add_artist_listitems(self, artists: List[Dict[str, Any]]) -> None:
        default_artist_icon = os.path.join(self.__addon_icon_path, MUSIC_ARTISTS_ICON)
        self.__apply_relation_overrides(artists, self.ARTIST_RELATION_FIELDS)
        for item in artists:
            li = xbmcgui.ListItem(item["name"], path=item["url"], offscreen=True)
            tag = li.getMusicInfoTag()
            tag.setTitle(item["name"])
            tag.setArtist(item["name"])
            tag.setRating(int(item.get("rating") or 0))
            tag.setMediaType("artist")
            genre = item.get("genre") or ""
            if genre:
                tag.setGenres([genre] if isinstance(genre, str) else genre)
            li.setArt(_art_for_item(item.get("thumb") or "", default_artist_icon))
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.setLabel2(item.get("followerslabel") or "")
            li.addContextMenuItems(
                self.__get_artist_context_menu_items(item, bool(item.get("followed"))), True
            )
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle,
                url=item["url"],
                listitem=li,
                isFolder=True,
                totalItems=len(artists),
            )

    def __prepare_playlist_listitems(
        self,
        playlists: List[Dict[str, Any]],
        group_label: str = "",
        relation_mode: str = "cache",
    ) -> List[Dict[str, Any]]:
        playlists2 = []
        followed_playlist_states = self.__get_followed_playlist_states_for_page(
            playlists, relation_mode=relation_mode
        )

        for playlist in playlists:
            if not playlist:
                continue

            # Filter out Spotify DJ playlist - unsupported in third-party clients
            if playlist.get("id") == DJ_PLAYLIST_ID:
                continue

            if playlist.get("images"):
                playlist["thumb"] = playlist["images"][0]["url"]
            else:
                playlist["thumb"] = "DefaultMusicAlbums.png"

            if group_label:
                playlist["label2"] = group_label
            self.__apply_daylist_metadata(playlist)

            playlist["url"] = self.__build_url(
                {
                    "action": self.browse_playlist.__name__,
                    "playlistid": playlist["id"],
                    "ownerid": playlist["owner"]["id"],
                }
            )

            playlist["followed"] = followed_playlist_states.get(playlist["id"])
            playlist[RELATION_SNAPSHOT_KEY] = time.time()

            playlists2.append(playlist)

        return playlists2

    def __apply_daylist_metadata(self, playlist: Dict[str, Any]) -> None:
        if not _is_spotify_daylist_playlist(playlist):
            return

        if not playlist.get("label2"):
            playlist["label2"] = DAYLIST_LABEL
        title_bucket = _daylist_title_bucket()
        current_name = playlist.get("name") or ""
        current_display_name = _daylist_display_name(current_name)
        current_name_is_dynamic = _has_dynamic_daylist_name(current_name)
        if playlist.get(DAYLIST_TITLE_BUCKET_KEY) == title_bucket and current_name_is_dynamic:
            return

        playlist_id = playlist.get("id")
        if not playlist_id:
            return

        # Preparing and rendering a listing both pass through here; look the
        # title up once per process (the retry thread polls for later changes).
        lookups = self.__dict__.setdefault("_daylist_title_lookups", {})
        if playlist_id in lookups:
            summary_name = lookups[playlist_id]
            retry_scheduled = True
        else:
            try:
                summary_name = self.__get_playlist_summary(playlist_id).get("name") or ""
            except Exception as exc:
                log_exception(exc, "daylist playlist title lookup")
                return
            lookups[playlist_id] = summary_name
            retry_scheduled = False

        display_name = _daylist_display_name(summary_name)
        if _has_dynamic_daylist_name(display_name):
            playlist["name"] = display_name
            playlist[DAYLIST_TITLE_BUCKET_KEY] = title_bucket
            return

        if current_name_is_dynamic:
            playlist["name"] = current_display_name
        elif display_name:
            playlist["name"] = display_name
        else:
            playlist["name"] = DAYLIST_LABEL

        if not retry_scheduled:
            self.__schedule_daylist_title_retry(playlist_id)

    def __schedule_daylist_title_retry(self, playlist_id: str) -> None:
        target_url = self.__current_request_url()
        if not playlist_id or not target_url:
            return

        def _retry_daylist_title():
            monitor = xbmc.Monitor()
            for delay_ms in DAYLIST_TITLE_RETRY_DELAYS_MS:
                if monitor.abortRequested():
                    return
                xbmc.sleep(delay_ms)
                try:
                    playlist_summary = self.__get_playlist_summary(playlist_id)
                except Exception as exc:
                    log_exception(exc, "daylist playlist title retry")
                    continue
                display_name = _daylist_display_name(playlist_summary.get("name") or "")
                if _has_dynamic_daylist_name(display_name):
                    self.__refresh_active_listing(target_url)
                    return

        busy_key = f"daylisttitle.{playlist_id}.{abs(hash(target_url))}"
        self.__start_dynamic_page_continuation(busy_key, target_url, _retry_daylist_title)

    def __get_playlist_context_menu_items(
        self, playlist, is_followed: Optional[bool] = None
    ) -> List[Tuple[str, str]]:
        loc = self.__localized
        plugin = f"plugin://{ADDON_ID}/"
        owner_id = (playlist.get("owner") or {}).get("id", "")
        ids = f"playlistid={playlist['id']}&ownerid={owner_id}"
        contextitems = [
            (
                self.__kodi_localized(KODI_PLAY_STR_ID),
                f"RunPlugin({plugin}?action=play_playlist&{ids})",
            ),
        ]

        if owner_id != self.__userid and is_followed is True:
            contextitems.append(
                (
                    loc(UNFOLLOW_PLAYLIST_STR_ID),
                    f"RunPlugin({plugin}?action=unfollow_playlist&{ids})",
                )
            )
        elif owner_id != self.__userid:
            contextitems.append(
                (loc(FOLLOW_PLAYLIST_STR_ID), f"RunPlugin({plugin}?action=follow_playlist&{ids})")
            )

        contextitems.append(
            (loc(REFRESH_LISTING_STR_ID), f"RunPlugin({plugin}?action=refresh_listing)")
        )
        return contextitems

    def __add_playlist_listitems(
        self, playlists: List[Dict[str, Any]], group_label: str = ""
    ) -> None:
        default_playlist_icon = os.path.join(self.__addon_icon_path, MUSIC_PLAYLISTS_ICON)
        addon_fanart = os.path.join(self.__addon_icon_path, "fanart.jpg")
        self.__apply_relation_overrides(playlists, self.PLAYLIST_RELATION_FIELDS)
        for item in playlists:
            if group_label:
                item["label2"] = group_label
            self.__apply_daylist_metadata(item)
            li = xbmcgui.ListItem(item["name"], path=item["url"], offscreen=True)
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.setLabel2(item.get("label2") or "")
            li.addContextMenuItems(
                self.__get_playlist_context_menu_items(item, item.get("followed")), True
            )
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
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_ALBUMS_STR_ID),
        )
        cache_str = f"spotify.artistalbums.{album_type}.{self.__artist_id}"
        checksum = self.__content_checksum("artistalbums", self.__user_country)
        albums = self.cache.get(cache_str, checksum=checksum)

        if albums:
            cache_log(
                f'Retrieved {len(albums)} cached albums of type "{album_type}" for artist "{self.__artist_id}".'
            )
        else:
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
            self.cache.set(
                cache_str, albums, checksum=checksum, expiration=ARTIST_CONTENT_CACHE_EXPIRATION
            )

        self.__add_album_listitems(albums)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ALBUM_IGNORE_THE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __saved_albums_cache_checksum(
        self, total: int, first_items: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Saved albums are newest-first, so the head item catches save+remove
        pairs that leave the total unchanged. The first page is fetched anyway."""
        generic_checksum = self.__addon.getSetting("cache_checksum")
        head = ""
        first = (first_items or [None])[0]
        if isinstance(first, dict):
            album = first.get("album") or {}
            head = f"-{first.get('added_at') or ''}-{album.get('id') or ''}"
        return f"v{CACHE_SCHEMA_VERSION}-savedalbums-{int(total)}{head}-{generic_checksum}"

    def __get_saved_albums_first_page(self) -> Dict[str, Any]:
        return (
            self.__spotipy.current_user_saved_albums(limit=50, offset=0, market=self.__user_country)
            or {}
        )

    def __get_saved_albums(
        self, first_page: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        if first_page is None:
            first_page = self.__get_saved_albums_first_page()
        total = int(first_page.get("total") or 0)
        raw_items = list(first_page.get("items") or [])
        cache_str = f"spotify.savedalbums.{self.__userid}"
        checksum = self.__saved_albums_cache_checksum(total, raw_items)
        albums = self.cache.get(cache_str, checksum=checksum)
        if isinstance(albums, list) and (len(albums) > 0 or total == 0):
            cache_log(f'Retrieved {len(albums)} cached albums for user "{self.__userid}".')
            return albums

        offset = len(raw_items)
        while raw_items and total > offset:
            page = (
                self.__spotipy.current_user_saved_albums(
                    limit=50, offset=offset, market=self.__user_country
                )
                or {}
            )
            items = page.get("items") or []
            if not items:
                break
            raw_items += items
            offset += len(items)

        # /me/albums already returns full album objects; no /albums?ids= refetch.
        # The embedded first page of tracks is not used by album listings.
        album_objects = []
        for item in raw_items:
            album = item.get("album") if isinstance(item, dict) else None
            if not album or not album.get("id"):
                continue
            album = dict(album)
            album.pop("tracks", None)
            album_objects.append(album)
        albums = self.__prepare_album_listitems(
            albums=album_objects,
            known_saved_album_ids={album["id"] for album in album_objects},
        )
        self.cache.set(cache_str, albums, checksum=checksum)
        cache_log(f'Retrieved {_get_len(albums)} UNCACHED albums for user "{self.__userid}".')
        return albums

    def browse_saved_albums(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_ALBUMS_STR_ID),
        )
        albums = self.__get_saved_albums()
        self.__add_album_listitems(albums, True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ALBUM_IGNORE_THE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __saved_tracks_cache_checksum(
        self, total: int, first_items: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """Saved tracks are newest-first, so the head item catches like+unlike
        pairs that leave the total unchanged. The first page is fetched anyway."""
        generic_checksum = self.__addon.getSetting("cache_checksum")
        head = ""
        first = (first_items or [None])[0]
        if isinstance(first, dict):
            track = first.get("track") or {}
            head = f"-{first.get('added_at') or ''}-{track.get('id') or ''}"
        return f"v{CACHE_SCHEMA_VERSION}-savedtracks-{int(total)}{head}-{generic_checksum}"

    def __get_saved_tracks_page(
        self, offset: int = 0, limit: int = DYNAMIC_PAGE_LIMIT
    ) -> Dict[str, Any]:
        return self.__spotipy.current_user_saved_tracks(
            limit=limit, offset=offset, market=self.__user_country
        )

    def __prepare_saved_track_items_page(
        self,
        raw_items: List[Dict[str, Any]],
        include_context_items: bool = True,
        include_artist_fanart: bool = True,
    ) -> List[Dict[str, Any]]:
        valid_items = []
        saved_track_ids: Set[str] = set()
        for item in raw_items:
            track = item.get("track") if isinstance(item, dict) else None
            if not track or not track.get("id"):
                continue
            valid_items.append(item)
            saved_track_ids.add(track["id"])

        return self.__prepare_track_listitems(
            tracks=valid_items,
            include_context_items=include_context_items,
            include_artist_fanart=include_artist_fanart,
            known_saved_track_ids=saved_track_ids,
        )

    def __saved_tracks_source(self) -> str:
        """What a saved-tracks listing was built from. A listing cached under an
        older checksum is only reused as a base when this still matches."""
        return f"v{CACHE_SCHEMA_VERSION}-{self.__addon.getSetting('cache_checksum')}"

    @staticmethod
    def __merge_saved_tracks(
        fresh: List[Dict[str, Any]], stale: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Lay the fresh first page over the previously cached listing.

        Returns (items, reconciled). Reconciled means the old listing's head
        sits inside the fresh page and the overlap matches, so only songs
        liked (or re-liked, which moves them up) since then sit above it.
        Otherwise the old items not on the fresh page follow it.
        """
        fresh_ids = [track.get("id") for track in fresh]
        if stale:
            head_id = stale[0].get("id")
            if head_id in fresh_ids:
                split = fresh_ids.index(head_id)
                new_ids = set(fresh_ids[:split])
                rest = [track for track in stale if track.get("id") not in new_ids]
                overlap = fresh_ids[split:]
                if [track.get("id") for track in rest[: len(overlap)]] == overlap:
                    return list(fresh[:split]) + rest, True
        seen = set(fresh_ids)
        return list(fresh) + [track for track in stale if track.get("id") not in seen], False

    def __start_saved_tracks_continuation(
        self,
        cache_str: str,
        checksum: str,
        collection: Dict[str, Any],
        target_url: str,
    ) -> None:
        total = int(collection.get("total") or 0)
        shown = list(collection.get("items") or [])
        loaded = int(collection.get(DYNAMIC_PAGING_LOADED_KEY) or len(shown))
        if total <= loaded:
            return
        exact_count = min(len(shown), int(collection.get(SAVED_TRACKS_EXACT_KEY, len(shown))))

        def _continue_saved_tracks():
            monitor = xbmc.Monitor()
            exact = shown[:exact_count]
            exact_ids = {track.get("id") for track in exact}
            # Previous listing kept below the fresh part until it is replaced.
            stale_tail = shown[exact_count:]
            offset = loaded
            current_total = total
            unsaved_pages = 0
            persisted = [len(exact)]  # leading items already stored in the chunk rows

            def _persist():
                listing = exact + [t for t in stale_tail if t.get("id") not in exact_ids]
                collection["items"] = listing
                collection[SAVED_TRACKS_EXACT_KEY] = len(exact)
                self.__store_saved_tracks(
                    cache_str, collection, listing, checksum, first_dirty_item=persisted[0]
                )
                persisted[0] = len(exact)

            def _fetch(page_offset: int):
                page = self.__get_saved_tracks_page(offset=page_offset, limit=DYNAMIC_PAGE_LIMIT)
                return page, self.__prepare_saved_track_items_page(page.get("items") or [])

            try:
                for _page_offset, (page, prepared) in self.__iter_pages_in_order(
                    loaded, total, _fetch, monitor
                ):
                    raw_items = page.get("items") or []
                    current_total = int(page.get("total") or current_total)
                    if not raw_items:
                        break
                    for track in prepared:
                        # A like between two page requests shifts items by one.
                        if track.get("id") not in exact_ids:
                            exact_ids.add(track.get("id"))
                            exact.append(track)
                    offset += len(raw_items)
                    self.__mark_dynamic_collection_state(
                        collection, offset, current_total, current_total <= offset
                    )
                    unsaved_pages += 1
                    if unsaved_pages >= PAGED_CACHE_WRITE_EVERY_PAGES:
                        _persist()
                        unsaved_pages = 0
                if monitor.abortRequested():
                    return

                self.__mark_dynamic_collection_state(collection, offset, current_total, True)
                del stale_tail[:]
                _persist()
                unsaved_pages = 0
                cache_log(f'Saved tracks for user "{self.__userid}" complete: {len(exact)}.')
                if [t.get("id") for t in exact] != [t.get("id") for t in shown]:
                    if self.__wait_for_active_listing(target_url):
                        self.__refresh_active_listing(target_url)
            finally:
                if unsaved_pages:
                    _persist()

        # Completed even when the listing is not on screen (widgets, precache,
        # a slow first render): the next visit then shows the whole list.
        self.__start_dynamic_page_continuation(
            cache_str, target_url, _continue_saved_tracks, require_active_listing=False
        )

    def __get_saved_tracks(self):
        first_page = self.__get_saved_tracks_page(offset=0, limit=DYNAMIC_PAGE_LIMIT)
        total = int(first_page.get("total") or 0)
        raw_items = first_page.get("items") or []
        cache_str = f"spotify.savedtracks.{self.__userid}"
        checksum = self.__saved_tracks_cache_checksum(total, raw_items)
        target_url = (
            self.__current_request_url()
            if self.__action == self.browse_saved_tracks.__name__
            else ""
        )

        cached = self.__chunked_cache_get(cache_str, checksum)
        if cached:
            collection, tracks = cached
            collection["items"] = tracks
            if total == 0 or tracks:
                cache_log(
                    f'Retrieved {len(tracks)} cached saved tracks for user "{self.__userid}".'
                )
                if not collection.get(DYNAMIC_PAGING_COMPLETE_KEY):
                    self.__start_saved_tracks_continuation(
                        cache_str, checksum, collection, target_url
                    )
                return tracks

        fresh = self.__prepare_saved_track_items_page(raw_items)
        tracks = fresh
        loaded = len(raw_items)
        complete = total <= loaded
        source = self.__saved_tracks_source()
        if not complete:
            # A like or unlike changed the checksum: start from the previous
            # listing instead of from the first page alone.
            previous = self.__chunked_cache_get(cache_str, "")
            if previous and previous[0].get(SAVED_TRACKS_SOURCE_KEY) == source:
                previous_head, previous_items = previous
                previous_exact = previous_head.get(DYNAMIC_PAGING_COMPLETE_KEY) and int(
                    previous_head.get(SAVED_TRACKS_EXACT_KEY) or 0
                ) >= len(previous_items)
                tracks, reconciled = self.__merge_saved_tracks(fresh, previous_items)
                if reconciled and previous_exact and len(tracks) == total:
                    loaded = total
                    complete = True
                    fresh = tracks
        collection = {
            "items": tracks,
            SAVED_TRACKS_EXACT_KEY: len(fresh),
            SAVED_TRACKS_SOURCE_KEY: source,
        }
        self.__mark_dynamic_collection_state(collection, loaded, total, complete)
        self.__store_saved_tracks(cache_str, collection, tracks, checksum)
        cache_log(
            f"Retrieved {len(tracks)} saved tracks ({len(fresh)} fresh) of {total} "
            f'for user "{self.__userid}".'
        )
        if not complete:
            self.__start_saved_tracks_continuation(cache_str, checksum, collection, target_url)
        return tracks

    def __store_saved_tracks(
        self,
        cache_str: str,
        collection: Dict[str, Any],
        items: List[Dict[str, Any]],
        checksum: str,
        first_dirty_item: int = 0,
    ) -> None:
        head = {key: value for key, value in collection.items() if key != "items"}
        self.__chunked_cache_set(
            cache_str, head, items, checksum, first_dirty_item=first_dirty_item
        )

    def browse_saved_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_SONGS_STR_ID),
        )
        tracks = self.__get_saved_tracks()
        self.__add_track_listitems(tracks, True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __saved_artists_cache_checksum(
        self, saved_albums_page: Dict[str, Any], followed_page: Dict[str, Any]
    ) -> str:
        """Built from the first pages both source listings fetch anyway (totals +
        newest item), so a cache hit costs two Spotify calls instead of five."""
        album_items = list(saved_albums_page.get("items") or [])
        albums_part = self.__saved_albums_cache_checksum(
            int(saved_albums_page.get("total") or 0), album_items
        )
        followed = followed_page.get("artists") or {}
        followed_items = followed.get("items") or []
        head = (followed_items[0] or {}).get("id") if followed_items else ""
        return (
            f"v{CACHE_SCHEMA_VERSION}-savedartists-{albums_part}"
            f"-followed-{int(followed.get('total') or 0)}-{head or ''}"
        )

    def __get_saved_artists(self) -> List[Dict[str, Any]]:
        saved_albums_page = self.__get_saved_albums_first_page()
        followed_page = self.__spotipy.current_user_followed_artists(limit=50) or {}
        cache_str = f"spotify.savedartists.{self.__userid}"
        checksum = self.__saved_artists_cache_checksum(saved_albums_page, followed_page)
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached saved artists for user "{self.__userid}".')
        else:
            saved_albums = self.__get_saved_albums(first_page=saved_albums_page)
            followed_artists = self.__get_followed_artists(first_page=followed_page)
            all_artist_ids = []
            artists = []
            for item in saved_albums:
                for artist in item["artists"]:
                    if artist["id"] not in all_artist_ids:
                        all_artist_ids.append(artist["id"])
            for chunk in get_chunks(all_artist_ids, 50):
                artists += self.__prepare_artist_listitems(self.__spotipy.artists(chunk)["artists"])
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
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_ARTISTS_STR_ID),
        )
        artists = self.__get_saved_artists()
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_followed_artists(
        self, first_page: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        artists = first_page or self.__spotipy.current_user_followed_artists(limit=50)
        cache_str = f"spotify.followedartists.v{CACHE_SCHEMA_VERSION}.{self.__userid}"
        first_items = artists["artists"].get("items") or []
        head = (first_items[0] or {}).get("id") if first_items else ""
        checksum = f"{artists['artists']['total']}-{head or ''}"

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
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_ARTISTS_STR_ID),
        )
        artists = self.__get_followed_artists()
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    @staticmethod
    def _normalize_search_query(query: str) -> str:
        return " ".join((query or "").split()).casefold()

    def __search_cache_key(self, query: str) -> str:
        digest = hashlib.sha1(self._normalize_search_query(query).encode("utf-8")).hexdigest()
        return f"spotify.search.{self.__user_country or '-'}.{self.__offset}.{digest[:20]}"

    def __get_search_results(self, query: str) -> Dict[str, Any]:
        """One /search call for all four types, shared by every search route.

        The skin's search widgets request artists, albums, tracks and playlists
        for the same term in parallel plugin processes. The response is cached
        (database only) for SEARCH_CACHE_EXPIRATION under the normalised query
        + market, and a short-lived window-property marker lets concurrent
        processes wait (bounded) for the first one's result instead of all
        calling Spotify.
        """
        cache_str = self.__search_cache_key(query)
        checksum = self.__content_checksum("search")
        result = self.cache.get(cache_str, checksum=checksum, mem_cache=False)
        if isinstance(result, dict):
            return result

        marker = f"{SEARCH_INFLIGHT_PROP_PREFIX}{cache_str}"
        if self.__wait_for_inflight_search(marker):
            result = self.cache.get(cache_str, checksum=checksum, mem_cache=False)
            if isinstance(result, dict):
                return result

        token = f"{time.time():.3f}-{os.getpid()}-{threading.get_ident()}"
        self.__win.setProperty(marker, token)
        try:
            result = self.__spotipy.search(
                q=query,
                type="artist,album,track,playlist",
                limit=self.__limit or SEARCH_RESULT_LIMIT,
                offset=self.__offset,
                market=self.__user_country,
            )
            result = self._strip_available_markets(result or {})
            self.cache.set(
                cache_str,
                result,
                checksum=checksum,
                expiration=SEARCH_CACHE_EXPIRATION,
                mem_cache=False,
            )
            return result
        finally:
            if self.__win.getProperty(marker) == token:
                self.__win.clearProperty(marker)

    def __wait_for_inflight_search(self, marker: str) -> bool:
        """Wait (at most SEARCH_INFLIGHT_WAIT_SECS) while another process runs the
        same search. Returns True when one was in flight (re-check the cache)."""
        raw = self.__win.getProperty(marker)
        if not raw:
            return False
        try:
            started = float(raw.split("-", 1)[0])
        except (TypeError, ValueError):
            started = 0.0
        remaining = SEARCH_INFLIGHT_WAIT_SECS - (time.time() - started)
        if remaining <= 0:
            return False  # stale marker (crashed or very slow process)
        monitor = xbmc.Monitor()
        attempts = max(
            1, int(min(remaining, SEARCH_INFLIGHT_WAIT_SECS) / SEARCH_INFLIGHT_POLL_SECS)
        )
        for _ in range(attempts):
            if monitor.waitForAbort(SEARCH_INFLIGHT_POLL_SECS):
                return False
            if self.__win.getProperty(marker) != raw:
                break
        return True

    @staticmethod
    def _strip_available_markets(result: Dict[str, Any]) -> Dict[str, Any]:
        """Drop per-item market lists (large, unused) before caching."""
        for section in ("tracks", "albums"):
            for item in (result.get(section) or {}).get("items") or []:
                if not isinstance(item, dict):
                    continue
                item.pop("available_markets", None)
                album = item.get("album")
                if isinstance(album, dict):
                    album.pop("available_markets", None)
        return result

    def __search_items(self, query: str, section: str) -> List[Dict[str, Any]]:
        result = self.__get_search_results(query)
        items = (result.get(section) or {}).get("items") or []
        return [item for item in items if item]

    def search_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_ARTISTS_STR_ID),
        )

        artists = self.__prepare_artist_listitems(self.__search_items(self.__artist_id, "artists"))
        self.__add_artist_listitems(artists)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def search_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_SONGS_STR_ID),
        )

        tracks = self.__prepare_track_listitems(
            tracks=self.__search_items(self.__track_id, "tracks")
        )
        self.__add_track_listitems(tracks, True)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def search_albums(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_ALBUMS_STR_ID),
        )

        # Simplified album objects from /search are enough for the listing
        # (only popularity is missing); no /albums?ids= re-fetch.
        albums = self.__prepare_album_listitems(
            albums=self.__search_items(self.__album_id, "albums")
        )
        self.__add_album_listitems(albums, True)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def search_playlists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")

        items = self.__search_items(self.__playlist_id, "playlists")

        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID),
        )
        playlists = self.__prepare_playlist_listitems(items)
        self.__add_playlist_listitems(playlists)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def search(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        xbmcplugin.setPluginCategory(
            self.__addon_handle, xbmc.getLocalizedString(KODI_SEARCH_RESULTS_STR_ID)
        )

        # Performance optimization: if we already have a search query, skip the keyboard
        if self.__filter:
            value = self.__filter
        else:
            kb = xbmc.Keyboard("", xbmc.getLocalizedString(KODI_ENTER_SEARCH_STRING_STR_ID))
            kb.doModal()
            if kb.isConfirmed():
                value = kb.getText()
            else:
                xbmcplugin.endOfDirectory(handle=self.__addon_handle)
                return

        items = []
        # Same shared call (and cache entry) the four sub-listings read from.
        result = self.__get_search_results(value)

        def total(section: str) -> int:
            return int((result.get(section) or {}).get("total") or 0)

        items.append(
            (
                f"{xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)} ({total('artists')})",
                self.__build_url({"action": self.search_artists.__name__, "artistid": value}),
            )
        )
        items.append(
            (
                f"{xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID)} ({total('playlists')})",
                self.__build_url({"action": self.search_playlists.__name__, "playlistid": value}),
            )
        )
        items.append(
            (
                f"{xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)} ({total('albums')})",
                self.__build_url({"action": self.search_albums.__name__, "albumid": value}),
            )
        )
        items.append(
            (
                f"{xbmc.getLocalizedString(KODI_SONGS_STR_ID)} ({total('tracks')})",
                self.__build_url({"action": self.search_tracks.__name__, "trackid": value}),
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

    def __should_stop_precache(self, monitor: xbmc.Monitor, token: str) -> bool:
        if monitor.abortRequested():
            return True
        if utils.is_rate_limited():
            return True
        if self.__win.getProperty(PRECACHE_NAVIGATION_TOKEN_PROP) != token:
            return True
        try:
            player = xbmc.Player()
            is_playing_audio = getattr(player, "isPlayingAudio", None)
            if callable(is_playing_audio) and is_playing_audio():
                return True
        except Exception:
            pass
        return False

    def __precache_library(self) -> None:
        if not self.__win.getProperty("Spotify.PreCachedItems"):
            monitor = xbmc.Monitor()
            token = getattr(self, "_PluginContent__navigation_token", "")
            self.__win.setProperty("Spotify.PreCachedItems", "busy")
            completed = False
            try:
                if self.__should_stop_precache(monitor, token):
                    return
                user_playlists = self.__get_user_playlists(self.__userid)[:PRECACHE_MAX_PLAYLISTS]
                for playlist in user_playlists:
                    if self.__should_stop_precache(monitor, token):
                        return
                    if (playlist.get("owner") or {}).get("id") == "spotify":
                        continue
                    track_total = int(((playlist.get("tracks") or {}).get("total") or 0))
                    if track_total > PRECACHE_MAX_PLAYLIST_TRACKS:
                        continue
                    self.__get_playlist_details(playlist["id"])

                if self.__should_stop_precache(monitor, token):
                    return
                saved_album_total = self.__get_saved_album_total()
                if saved_album_total <= PRECACHE_MAX_LIBRARY_ITEMS:
                    self.__get_saved_albums()

                if self.__should_stop_precache(monitor, token):
                    return
                followed_artist_total = self.__get_followed_artist_total()
                if followed_artist_total <= PRECACHE_MAX_LIBRARY_ITEMS:
                    self.__get_followed_artists()

                if saved_album_total + followed_artist_total <= PRECACHE_MAX_LIBRARY_ITEMS:
                    self.__get_saved_artists()

                if self.__should_stop_precache(monitor, token):
                    return
                saved_track_total = self.__get_saved_track_total()
                if saved_track_total <= PRECACHE_MAX_LIBRARY_ITEMS:
                    self.__get_saved_tracks()
                completed = True
            finally:
                if completed:
                    self.__win.setProperty("Spotify.PreCachedItems", "done")
                else:
                    self.__win.clearProperty("Spotify.PreCachedItems")
                del monitor
