"""
plugin.audio.spotifykodiconnect
SpotifyKodiConnect - service: spotty + HTTP audio streaming to Kodi.
"""

import math
import os
import threading
import time

import bottle_manager
import spotipy
import spotty
import utils
import xbmc
import xbmcaddon
import xbmcgui
from http_spotty_audio_streamer import HTTPSpottyAudioStreamer
from play_queue import clear as play_queue_clear
from play_queue import should_fire_autoplay as play_queue_should_fire_autoplay
from playlist_next import get_next_playlist_item, get_playlist_track_ids, parse_track_url
from prebuffer import PrebufferManager
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from string_ids import WELCOME_AUTHENTICATED_STR_ID
from utils import (
    ADDON_ID,
    ADDON_WINDOW_ID,
    PROXY_HOST,
    PROXY_PORT,
    get_cached_auth_token,
    log_exception,
    log_msg,
)
from xbmc import LOGDEBUG, LOGWARNING

SPOTIFY_ADDON = xbmcaddon.Addon(id=ADDON_ID)
SPOTIFY_TRACK_HOOK_KEYS = (
    "Id",
    "Title",
    "Artist",
    "Album",
    "Thumb",
    "Fanart",
    "Duration",
    "File",
    "Available",
)
SPOTIFY_PLAYER_METADATA_POLL_MS = 100
SPOTIFY_PLAYER_METADATA_ATTEMPTS = 30
SPOTIFY_TRACK_PROPERTY_LABELS = (
    "MusicPlayer.Property(spotifytrackid)",
    "MusicPlayer.(1).Property(spotifytrackid)",
)
PLAYER_FILE_LABELS = (
    "Player.FileNameAndPath",
    "MusicPlayer.FileNameAndPath",
)
PREBUFFER_RELEASE_DELAY_PROP = "Spotify.PrebufferReleaseDelay"
PREBUFFER_RELEASE_DELAY_DEFAULT = 5.0
PREBUFFER_RELEASE_DELAY_MIN = 2.0
PREBUFFER_RELEASE_DELAY_MAX = 15.0
PREBUFFER_RELEASE_DELAY_STEP_UP = 2.0
PREBUFFER_RELEASE_DELAY_STEP_DOWN = 0.5

# Artist fanart for Music OSD (single largest image URL; no rotation – Spotify only provides same image in multiple sizes)
_artist_fanart_urls = []  # type: list
_artist_fanart_index = 0
# Track ID for which the liked state was last fetched. Prevents __on_track_started
# from resetting Spotify.CurrentTrackLiked on every Kodi buffering re-request.
_liked_state_track_id: str = ""


def _start_daemon_thread(target, args=(), task_name: str = "background task") -> bool:
    try:
        threading.Thread(target=target, args=args, daemon=True).start()
        return True
    except RuntimeError as exc:
        log_msg(f"Could not start {task_name}: {exc}", LOGWARNING)
        return False


class _SpotifyOSDServiceMonitor(xbmc.Monitor):
    """Receives inter-addon notifications so the service can act on skin-triggered events.

    Currently handles:
      Other.ToggleLike – sent by the Music OSD Like button via NotifyAll.
                         Performs the Spotify liked-state toggle entirely inside
                         the service process, avoiding RunPlugin reentry problems
                         with the audio plugin while a track is streaming.
    """

    def onNotification(self, sender: str, method: str, data: str) -> None:
        if sender == "plugin.audio.spotifykodiconnect" and method == "Other.ToggleLike":
            log_msg("ToggleLike notification received, spawning handler.", LOGDEBUG)
            _start_daemon_thread(self._handle_toggle_like, task_name="toggle-like handler")

    @staticmethod
    def _handle_toggle_like() -> None:
        global _liked_state_track_id
        log_msg("ToggleLike: handler running.", LOGDEBUG)
        try:
            win = xbmcgui.Window(ADDON_WINDOW_ID)
            track_id = win.getProperty("Spotify.CurrentTrackId")
            if not track_id:
                log_msg("ToggleLike: no current track id.", LOGWARNING)
                return
            token = get_cached_auth_token()
            if not token:
                log_msg("ToggleLike: no auth token.", LOGWARNING)
                return
            # Use cached property for toggle direction — avoids an extra API round-trip.
            currently_liked = bool(win.getProperty("Spotify.CurrentTrackLiked"))

            # Optimistic update: flip the icon immediately so the UI feels instant.
            # The API call below confirms the change; reverted on failure.
            if currently_liked:
                win.clearProperty("Spotify.CurrentTrackLiked")
            else:
                win.setProperty("Spotify.CurrentTrackLiked", "true")

            sp = spotipy.Spotify(auth=token)
            try:
                if currently_liked:
                    sp.current_user_saved_tracks_delete([track_id])
                    log_msg(f"ToggleLike: unliked {track_id}.", LOGDEBUG)
                else:
                    sp.current_user_saved_tracks_add([track_id])
                    log_msg(f"ToggleLike: liked {track_id}.", LOGDEBUG)
            except Exception as api_exc:
                # Revert the optimistic update so the icon matches actual Spotify state.
                if currently_liked:
                    win.setProperty("Spotify.CurrentTrackLiked", "true")
                else:
                    win.clearProperty("Spotify.CurrentTrackLiked")
                log_exception(api_exc, "ToggleLike API call failed, reverted icon")
                return

            # Keep _liked_state_track_id in sync so the next buffering
            # re-request for the same track doesn't overwrite the new state.
            _liked_state_track_id = track_id
        except Exception as exc:
            log_exception(exc, "ToggleLike notification handler failed")


_monitor = _SpotifyOSDServiceMonitor()


def _clear_artist_fanart_rotation() -> None:
    global _artist_fanart_urls, _artist_fanart_index
    _artist_fanart_urls = []
    _artist_fanart_index = 0
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    win.clearProperty("Spotify.ArtistFanartCurrent")


def _set_or_clear(win, key: str, value) -> None:
    if value:
        win.setProperty(key, str(value))
    else:
        win.clearProperty(key)


def _join_artist(value) -> str:
    if isinstance(value, list):
        return ", ".join([str(item) for item in value if item])
    return str(value or "")


def _item_art(item, *keys) -> str:
    art = (item or {}).get("art") or {}
    if not isinstance(art, dict):
        return ""
    for key in keys:
        value = art.get(key)
        if value:
            return value
    return ""


def _publish_track_hook(win, prefix: str, item, track_id: str = "", duration=None) -> None:
    item = item or {}
    title = item.get("title") or item.get("label") or ""
    artist = _join_artist(item.get("artist"))
    album = item.get("album") or ""
    file_url = item.get("file") or ""
    thumb = _item_art(item, "thumb", "poster", "icon", "fanart", "landscape")
    fanart = _item_art(item, "artist.fanart", "fanart", "landscape", "thumb", "poster")
    duration_value = duration or item.get("duration") or item.get("runtime") or ""

    _set_or_clear(win, f"Spotify.{prefix}Id", track_id)
    _set_or_clear(win, f"Spotify.{prefix}Title", title)
    _set_or_clear(win, f"Spotify.{prefix}Artist", artist)
    _set_or_clear(win, f"Spotify.{prefix}Album", album)
    _set_or_clear(win, f"Spotify.{prefix}Thumb", thumb)
    _set_or_clear(win, f"Spotify.{prefix}Fanart", fanart)
    _set_or_clear(win, f"Spotify.{prefix}Duration", duration_value)
    _set_or_clear(win, f"Spotify.{prefix}File", file_url)
    _set_or_clear(win, f"Spotify.{prefix}Available", track_id or title or file_url)


def _clear_track_hook(win, prefix: str) -> None:
    for key in SPOTIFY_TRACK_HOOK_KEYS:
        win.clearProperty(f"Spotify.{prefix}{key}")


def _clear_playback_hooks() -> None:
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    _clear_track_hook(win, "CurrentTrack")
    _clear_track_hook(win, "NextTrack")


def _read_active_spotify_player_state(win) -> tuple[bool, str, bool]:
    for label in SPOTIFY_TRACK_PROPERTY_LABELS:
        track_id = (xbmc.getInfoLabel(label) or "").strip()
        if track_id:
            return True, track_id, True

    has_player_file = False
    for label in PLAYER_FILE_LABELS:
        file_path = (xbmc.getInfoLabel(label) or "").strip()
        if not file_path:
            continue
        has_player_file = True
        track_id, _duration = parse_track_url(file_path)
        if track_id:
            return True, track_id, True
        if ADDON_ID in file_path:
            return True, win.getProperty("Spotify.CurrentTrackId"), True

    return False, "", has_player_file


def _read_confirmed_spotify_track_duration(track_id: str) -> float:
    try:
        current_item, _next_item = get_next_playlist_item()
        current_id, current_duration = parse_track_url((current_item or {}).get("file") or "")
        if current_id == track_id and current_duration is not None:
            return current_duration
    except Exception:
        pass

    for label in PLAYER_FILE_LABELS:
        file_path = (xbmc.getInfoLabel(label) or "").strip()
        current_id, current_duration = parse_track_url(file_path)
        if current_id == track_id and current_duration is not None:
            return current_duration

    return 0


def _refresh_playback_hooks(current_track_id: str, delay_ms: int = 0) -> None:
    def _run():
        if delay_ms:
            xbmc.sleep(delay_ms)
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        try:
            current_item, next_item = get_next_playlist_item()
            current_id, current_duration = parse_track_url((current_item or {}).get("file") or "")
            if current_id == current_track_id:
                _publish_track_hook(win, "CurrentTrack", current_item, current_id, current_duration)
            else:
                _clear_track_hook(win, "CurrentTrack")
                _set_or_clear(win, "Spotify.CurrentTrackId", current_track_id)

            next_id, next_duration = parse_track_url((next_item or {}).get("file") or "")
            if next_id and next_id != current_track_id:
                _publish_track_hook(win, "NextTrack", next_item, next_id, next_duration)
            else:
                _clear_track_hook(win, "NextTrack")
        except Exception as exc:
            log_exception(exc, "refreshing Spotify skin playback hooks failed")

    _start_daemon_thread(_run, task_name="playback hook refresh")


def abort_app(timeout_in_secs: int) -> bool:
    return _monitor.waitForAbort(timeout_in_secs)


class _SpotifyOSDPlayerMonitor(xbmc.Player):
    """Clears Spotify OSD window properties when Kodi playback actually stops.

    We deliberately do NOT clear them from the HTTP stream callbacks because those
    fire whenever Kodi's internal buffer fills (mid-song), not only at true end-of-track.
    """

    def __init__(self, on_spotify_started=None, on_external_playback=None):
        xbmc.Player.__init__(self)
        self._on_spotify_started = on_spotify_started or (lambda _track_id: None)
        self._on_external_playback = on_external_playback or (lambda: None)

    def _clear(self) -> None:
        global _liked_state_track_id
        _liked_state_track_id = ""
        _clear_artist_fanart_rotation()
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        win.clearProperty("Spotify.CurrentTrackId")
        win.clearProperty("Spotify.CurrentTrackLiked")
        _clear_playback_hooks()
        # Drop any active play-queue session so a stale "loading" state from
        # a previous play doesn't suppress autoplay for the next one.
        play_queue_clear()

    def onPlayBackStopped(self) -> None:
        self._clear()

    def onPlayBackEnded(self) -> None:
        self._clear()

    def onPlayBackError(self) -> None:
        self._clear()

    def onPlayBackStarted(self) -> None:
        # If a non-Spotify item starts playing, clear the Spotify OSD state.
        # PAPlayer can fire before MusicPlayer properties settle, so wait until
        # Kodi exposes either a Spotify marker or a concrete non-Spotify file.
        def _check():
            win = xbmcgui.Window(ADDON_WINDOW_ID)
            for _attempt in range(SPOTIFY_PLAYER_METADATA_ATTEMPTS):
                is_spotify, track_id, has_player_file = _read_active_spotify_player_state(win)
                if is_spotify:
                    if track_id:
                        win.setProperty("Spotify.CurrentTrackId", track_id)
                        try:
                            self._on_spotify_started(track_id)
                        except Exception as exc:
                            log_exception(exc, "Spotify playback-start callback failed")
                    return
                if has_player_file:
                    # A non-Spotify player (e.g. PlexKodiConnect movie) is starting.
                    # Tear down ALL Spotify audio resources immediately to prevent
                    # Amlogic audio driver deadlocks when the AML SPDIF/HDMI codec
                    # reconfigures from stereo PCM (Spotify) to multi-channel AC3
                    # pass-through (movie) while spotty/HTTP streams are still active.
                    try:
                        self._on_external_playback()
                    except Exception as exc:
                        log_exception(exc, "External playback teardown callback failed")
                    self._clear()
                    return
                xbmc.sleep(SPOTIFY_PLAYER_METADATA_POLL_MS)

            if win.getProperty("Spotify.CurrentTrackId"):
                log_msg(
                    "Keeping Spotify OSD state; player metadata did not settle.",
                    LOGDEBUG,
                )
            else:
                self._clear()

        _start_daemon_thread(_check, task_name="Spotify playback metadata check")


class MainService:
    def __init__(self):
        log_msg(f"Spotify plugin version: {xbmcaddon.Addon(id=ADDON_ID).getAddonInfo('version')}.")

        self.__spotty_helper: SpottyHelper = SpottyHelper()
        self.__spotty = spotty.get_spotty(self.__spotty_helper)

        self.__spotty_auth: SpottyAuth = SpottyAuth(self.__spotty)
        # Recover from an interrupted credential rotation (e.g. power loss
        # during zeroconf re-auth): if credentials.json is missing but the
        # .bak exists, restore it before any token fetch is attempted.
        self.__spotty_auth.restore_credentials_from_backup_if_needed()
        self.__auth_token_expires_at = ""
        self.__welcome_msg = True

        normalization_setting = (
            (SPOTIFY_ADDON.getSetting("spotify_normalization") or "auto").strip().lower()
        )
        if normalization_setting not in ("off", "auto", "track", "album"):
            normalization_setting = "auto"
        use_autoplay = SPOTIFY_ADDON.getSetting("spotify_autoplay").lower() == "true"
        bitrate = self._get_bitrate_setting()
        self.__prebuffer_enabled = SPOTIFY_ADDON.getSetting("prebuffer_enabled").lower() == "true"
        self.__prebuffer_manager: PrebufferManager = PrebufferManager(
            self.__spotty,
            normalization_gain_type=normalization_setting,
            bitrate=bitrate,
        )
        self.__http_spotty_streamer: HTTPSpottyAudioStreamer = HTTPSpottyAudioStreamer(
            self.__spotty,
            normalization_gain_type=normalization_setting,
            prebuffer_manager=self.__prebuffer_manager,
            on_track_started_callback=self.__on_track_started,
            use_autoplay=use_autoplay,
            bitrate=bitrate,
        )
        self.__http_spotty_streamer.set_notify_track_finished(self.__on_track_finished)

        # Keep a strong reference so Kodi doesn't GC the player monitor.
        self.__osd_player_monitor = _SpotifyOSDPlayerMonitor(
            on_spotify_started=self.__on_spotify_playback_started,
            on_external_playback=self._teardown_for_external_playback,
        )

        # Cancellation token for _deferred_prebuffer threads.  Incremented after
        # Kodi confirms playback so only the latest thread proceeds to call
        # get_or_start.  Prevents cascade-mode threads from all firing at once and
        # evicting each other's buffers.
        self._prebuffer_token = 0
        self._prebuffer_token_lock = threading.Lock()

        bottle_manager.route_all(self.__http_spotty_streamer)

    @staticmethod
    def _get_prebuffer_release_delay() -> float:
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        try:
            value = float(win.getProperty(PREBUFFER_RELEASE_DELAY_PROP) or "")
        except (TypeError, ValueError):
            value = PREBUFFER_RELEASE_DELAY_DEFAULT
        return max(PREBUFFER_RELEASE_DELAY_MIN, min(PREBUFFER_RELEASE_DELAY_MAX, value))

    @staticmethod
    def _set_prebuffer_release_delay(value: float) -> None:
        delay = max(PREBUFFER_RELEASE_DELAY_MIN, min(PREBUFFER_RELEASE_DELAY_MAX, value))
        xbmcgui.Window(ADDON_WINDOW_ID).setProperty(PREBUFFER_RELEASE_DELAY_PROP, f"{delay:.1f}")
        log_msg(f"Prebuffer release delay now {delay:.1f}s.", LOGDEBUG)

    def _watch_prebuffer_result(self, downloader, track_id: str, delay_used: float) -> None:
        if downloader is None:
            return
        try:
            with downloader.cond:
                while (
                    not downloader.is_finished and not downloader.error and not downloader.aborted
                ):
                    downloader.cond.wait(timeout=30.0)
                error = downloader.error
                aborted = downloader.aborted
            if aborted:
                return
            if error:
                self._set_prebuffer_release_delay(delay_used + PREBUFFER_RELEASE_DELAY_STEP_UP)
                log_msg(
                    f"Prebuffer for {track_id} errored after {delay_used:.1f}s delay.",
                    LOGDEBUG,
                )
            else:
                self._set_prebuffer_release_delay(delay_used - PREBUFFER_RELEASE_DELAY_STEP_DOWN)
                log_msg(
                    f"Prebuffer for {track_id} succeeded after {delay_used:.1f}s delay.",
                    LOGDEBUG,
                )
        except Exception as exc:
            log_exception(exc, "watching prebuffer result failed")

    def _teardown_for_external_playback(self) -> None:
        """Tear down all Spotify audio resources when a non-Spotify player starts.

        Called from _SpotifyOSDPlayerMonitor.onPlayBackStarted the instant a
        non-Spotify file is detected (e.g. a PlexKodiConnect movie).  Prevents
        Amlogic audio driver deadlocks by killing spotty subprocesses, aborting
        cache downloads, cancelling prebuffer, and terminating the active HTTP
        stream — before VideoPlayer reconfigures the AML audio codec.
        """
        from spotty_cache import SpottyCacheManager

        # Guard: only tear down if Spotify audio resources are actually active.
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        if not win.getProperty("Spotify.CurrentTrackId"):
            return

        log_msg("External playback detected — tearing down Spotify audio resources.")

        # Cancel any deferred prebuffer threads.
        with self._prebuffer_token_lock:
            self._prebuffer_token += 1

        # Cancel prebuffer state.
        self.__prebuffer_manager.cancel_prebuffer()

        # Abort all background downloaders and kill their spotty subprocesses.
        SpottyCacheManager.cleanup_all()

        # Terminate the active HTTP stream generator and clear streaming state.
        self.__http_spotty_streamer.teardown_for_external_playback()

        # Kill any remaining spotty processes not tracked by the cache manager.
        self.__spotty_helper.kill_all_spotties()

        log_msg("Spotify audio resources torn down for external playback.")

    def __on_track_started(self, track_id: str, _duration_sec: float) -> None:
        """Set OSD properties for Kodi-confirmed Spotify playback."""
        global _artist_fanart_urls, _artist_fanart_index, _liked_state_track_id
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", track_id or "")
        # Only reset and re-query the liked state when the track actually changes.
        # Kodi issues fresh Range: bytes=0- requests for the same track during buffering,
        # which would otherwise wipe a user-toggled liked state mid-play.
        track_changed = track_id != _liked_state_track_id
        if track_changed:
            _liked_state_track_id = track_id
            win.clearProperty("Spotify.CurrentTrackLiked")

        _refresh_playback_hooks(track_id, delay_ms=1000)

        def _fetch_artist_fanart_urls():
            global _artist_fanart_urls, _artist_fanart_index
            try:
                token = get_cached_auth_token()
                if not token:
                    return
                sp = spotipy.Spotify(auth=token)
                track = sp.track(track_id)
                artists = (track or {}).get("artists") or []
                if not artists:
                    return
                artist_id = artists[0].get("id")
                if not artist_id:
                    return
                artist = sp.artist(artist_id)
                images = (artist or {}).get("images") or []
                # Spotify returns same image in multiple sizes (640, 300, 64); use only largest
                if not images:
                    return
                largest_url = images[0].get("url") or ""
                if not largest_url:
                    return
                _artist_fanart_urls.clear()
                _artist_fanart_urls.append(largest_url)
                _artist_fanart_index = 0
                w = xbmcgui.Window(ADDON_WINDOW_ID)
                w.setProperty("Spotify.ArtistFanartCurrent", largest_url)
            except Exception:
                _artist_fanart_urls.clear()
                _artist_fanart_index = 0

        _start_daemon_thread(_fetch_artist_fanart_urls, task_name="artist fanart refresh")

        def _set_liked_state():
            try:
                token = get_cached_auth_token()
                if not token:
                    log_msg(
                        f"No auth token when checking liked state for {track_id}.",
                        LOGWARNING,
                    )
                    return
                sp = spotipy.Spotify(auth=token)
                result = sp.current_user_saved_tracks_contains([track_id])
                liked = "true" if (result and result[0]) else ""
                if liked:
                    win.setProperty("Spotify.CurrentTrackLiked", liked)
                else:
                    win.clearProperty("Spotify.CurrentTrackLiked")
                log_msg(f"Spotify.CurrentTrackLiked = {liked!r} for {track_id}.", LOGDEBUG)
            except Exception as e:
                log_msg(f"Error setting liked state for {track_id}: {e}", LOGWARNING)
                pass

        # Only run the liked state check when the track actually changes.
        if track_changed:
            _start_daemon_thread(_set_liked_state, task_name="liked-state refresh")

    def __on_spotify_playback_started(self, track_id: str) -> None:
        try:
            self.__on_track_started(track_id, _read_confirmed_spotify_track_duration(track_id))

            _current_item, next_item = get_next_playlist_item()
            if not next_item:
                if SPOTIFY_ADDON.getSetting("spotify_autoplay").lower() == "true":
                    # Consult the play-queue session before firing. The plugin
                    # opens a session at the start of play_playlist and marks
                    # original_complete when its paging thread finishes; until
                    # then a "no next item" signal is a paging gap, not true
                    # exhaustion, and firing autoplay here would destroy the
                    # in-flight queue (the daylist bug).
                    if play_queue_should_fire_autoplay():
                        _start_daemon_thread(
                            self.__queue_autoplay_tracks,
                            args=(track_id,),
                            task_name="autoplay queue fill",
                        )
                return

            next_track_id, next_duration = parse_track_url(next_item.get("file") or "")
            if not next_track_id or next_duration is None:
                return

            prebuffer_enabled = SPOTIFY_ADDON.getSetting("prebuffer_enabled").lower() == "true"
            if not prebuffer_enabled:
                return

            with self._prebuffer_token_lock:
                self._prebuffer_token += 1
                my_token = self._prebuffer_token

            def _deferred_prebuffer():
                from spotty_cache import SpottyCacheManager

                time.sleep(2.0)

                with self._prebuffer_token_lock:
                    if self._prebuffer_token != my_token:
                        log_msg(
                            f"_deferred_prebuffer: cancelled (stale, track={track_id})",
                            LOGDEBUG,
                        )
                        return

                dl = SpottyCacheManager.find_best_downloader(track_id, 0)
                if dl is not None and not dl.is_finished:
                    with dl.cond:
                        while not dl.is_finished and not dl.error and not dl.aborted:
                            dl.cond.wait(timeout=30.0)

                log_msg(
                    f"Main track {track_id} finished downloading. Safe to start prebuffer for next track.",
                    LOGDEBUG,
                )

                release_delay = self._get_prebuffer_release_delay()
                log_msg(
                    f"_deferred_prebuffer: waiting {release_delay:.1f}s for session release.",
                    LOGDEBUG,
                )
                time.sleep(release_delay)

                with self._prebuffer_token_lock:
                    if self._prebuffer_token != my_token:
                        log_msg(
                            f"_deferred_prebuffer: cancelled after sleep (track={track_id})",
                            LOGDEBUG,
                        )
                        return

                try:
                    _, next_item_now = get_next_playlist_item()
                except Exception:
                    return
                if not next_item_now:
                    return
                next_id_now, next_dur_now = parse_track_url(next_item_now.get("file") or "")
                if not next_id_now or next_dur_now is None:
                    return
                if next_id_now == track_id:
                    log_msg(
                        f"_deferred_prebuffer: next track same as triggering track"
                        f" ({track_id}), skipping.",
                        LOGDEBUG,
                    )
                    return

                bitrate = self._get_bitrate_setting()
                norm = (SPOTIFY_ADDON.getSetting("spotify_normalization") or "auto").strip().lower()
                if norm not in ("off", "auto", "track", "album"):
                    norm = "auto"
                downloader = self.__prebuffer_manager.start_prebuffer(
                    next_id_now,
                    next_dur_now,
                    bitrate=bitrate,
                    normalization_gain_type=norm,
                )
                _start_daemon_thread(
                    self._watch_prebuffer_result,
                    args=(downloader, next_id_now, release_delay),
                    task_name="prebuffer result watcher",
                )

            _start_daemon_thread(_deferred_prebuffer, task_name="deferred prebuffer")

        except Exception:
            pass

    def __queue_autoplay_tracks(self, seed_track_id: str) -> None:
        """Fetch recommended tracks and APPEND them to Kodi's music playlist.

        Does NOT clear the existing playlist — the seed track is already
        there and currently playing. Recommendations are deduped against
        both the seed and any track already in the playlist so we don't
        replay something the user just heard.
        """
        try:
            token = get_cached_auth_token()
            if not token:
                log_msg("Autoplay: no auth token available.", LOGWARNING)
                return

            sp = spotipy.Spotify(auth=token)

            RECOMMEND_LIMIT = 49
            result = sp.recommendations(seed_tracks=[seed_track_id], limit=RECOMMEND_LIMIT)
            rec_tracks = (result or {}).get("tracks") or []
            if not rec_tracks:
                log_msg("Autoplay: no recommendations returned.", LOGDEBUG)
                return

            playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)

            # Dedup against the seed AND the existing playlist contents.
            # Before the play-queue migration this used playlist.clear()
            # which made dedup moot; now that we append, we need it.
            seen_ids = get_playlist_track_ids()
            seen_ids.add(seed_track_id)

            rec_ids = [t.get("id") for t in rec_tracks if t.get("id")]
            rec_ids = [rid for rid in rec_ids if rid and rid not in seen_ids]

            from utils import get_chunks

            added = 0
            for chunk in get_chunks(rec_ids, 20):
                if added >= RECOMMEND_LIMIT:
                    break
                try:
                    batch = sp.tracks(chunk, market=None).get("tracks") or []
                except Exception:
                    # Fall back to per-track calls for this chunk.
                    batch = []
                    for tid in chunk:
                        try:
                            batch.append(sp.track(tid))
                        except Exception:
                            continue

                for full in batch:
                    if added >= RECOMMEND_LIMIT:
                        break
                    try:
                        tid = full.get("id") or ""
                        if not tid or tid in seen_ids:
                            continue
                        name = full.get("name") or ""
                        duration_ms = full.get("duration_ms") or 0
                        artists = full.get("artists") or []
                        artist_name = artists[0].get("name") or "" if artists else ""
                        album = full.get("album") or {}
                        album_name = album.get("name") or ""
                        images = album.get("images") or []
                        art_url = images[0].get("url") if images else ""
                        duration_sec = math.ceil(duration_ms / 1000) if duration_ms else 1
                        url = f"http://{PROXY_HOST}:{PROXY_PORT}/track/{tid}/{duration_sec}.wav"
                        li = xbmcgui.ListItem(label=name)
                        li.setProperty("IsPlayable", "true")
                        li.setProperty("spotifytrackid", tid)
                        li.setInfo(
                            "music",
                            {
                                "title": name,
                                "artist": artist_name,
                                "album": album_name,
                                "duration": duration_sec,
                            },
                        )
                        if art_url:
                            try:
                                li.setArt(
                                    {
                                        "thumb": art_url,
                                        "icon": art_url,
                                        "fanart": art_url,
                                    }
                                )
                            except Exception:
                                pass
                        playlist.add(url, li)
                        seen_ids.add(tid)
                        added += 1
                    except Exception:
                        pass

            log_msg(
                f"Autoplay: appended {added} recommendations (seed={seed_track_id}).",
                LOGDEBUG,
            )

        except Exception as exc:
            log_msg(f"Autoplay: failed to append autoplay tracks: {exc}", LOGWARNING)

    def __on_track_finished(self, track_id: str) -> None:
        """Mark HTTP streamer as ended so the next request is treated as a new track.

        Only called when the streamer has sent the final byte of the track (not on
        every range chunk). OSD properties are cleared by _SpotifyOSDPlayerMonitor
        on real playback stop/end events.
        """
        self.__http_spotty_streamer.set_stream_ended(track_id)

    def run(self) -> None:
        log_msg("Starting main service loop.")

        bottle_manager.start_thread(PROXY_PORT)
        log_msg(f"Started bottle with port {PROXY_PORT}.")

        self.__renew_token()

        loop_counter = 0
        loop_wait_in_secs = 6
        while True:
            loop_counter += 1
            if (loop_counter % 10) == 0:
                log_msg(f"Main loop continuing. Loop counter: {loop_counter}.")

            prebuffer_enabled_now = SPOTIFY_ADDON.getSetting("prebuffer_enabled").lower() == "true"
            if self.__prebuffer_enabled and not prebuffer_enabled_now:
                self.__prebuffer_manager.cancel_prebuffer()
            self.__prebuffer_enabled = prebuffer_enabled_now

            if self.__auth_token_expires_at == "":
                log_msg("Spotify not yet authorized. Refreshing auth token now.")
                self.__renew_token()
            elif (int(self.__auth_token_expires_at) - 60) <= int(time.time()):
                expire_time = int(self.__auth_token_expires_at)
                time_now = int(time.time())
                log_msg(
                    f"Spotify token expired."
                    f" Expire time: {utils.get_time_str(expire_time)} ({expire_time});"
                    f" time now: {utils.get_time_str(time_now)} ({time_now})."
                    f" Refreshing auth token now."
                )
                self.__renew_token()

            if abort_app(loop_wait_in_secs):
                log_msg("Aborting the main service.")
                break

        self.__close()

    def __close(self) -> None:
        log_msg("Shutdown requested.")
        from spotty_cache import SpottyCacheManager

        SpottyCacheManager.cleanup_all()
        self.__prebuffer_manager.cancel_prebuffer()
        self.__http_spotty_streamer.stop()
        self.__spotty_helper.kill_all_spotties()
        bottle_manager.stop_thread()
        log_msg("Main service stopped.")

    @staticmethod
    def _get_bitrate_setting() -> str:
        """Return the bitrate setting string, validated to one of '96', '160', '320'."""
        v = (SPOTIFY_ADDON.getSetting("spotify_bitrate") or "320").strip()
        return v if v in ("96", "160", "320") else "320"

    def __renew_token(self) -> None:
        try:
            self.__spotty_auth.renew_token()
            self.__auth_token_expires_at = utils.get_cached_auth_token_expires_at()
            if self.__welcome_msg:
                self.__welcome_msg = False
                self.__show_welcome_notification()
        except Exception as exc:
            log_exception(exc, "Could not renew Spotify auth token")
            self.__auth_token_expires_at = ""

    def __show_welcome_notification(self) -> None:
        try:
            addon = xbmcaddon.Addon(id=ADDON_ID)
            addon_name = addon.getAddonInfo("name")
            username = utils.get_username()
            welcome = addon.getLocalizedString(WELCOME_AUTHENTICATED_STR_ID)
            msg = f"{welcome} {username}" if username else welcome
            icon = addon.getAddonInfo("icon")
            xbmcgui.Dialog().notification(addon_name, msg, icon=icon, time=2000, sound=False)
        except Exception as exc:
            log_exception(exc, "Could not show welcome notification")
