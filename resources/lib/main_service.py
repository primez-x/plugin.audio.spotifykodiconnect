"""
    plugin.audio.spotifykodiconnect
    SpotifyKodiConnect - service: spotty + HTTP audio streaming to Kodi.
"""

import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

import bottle_manager
import spotipy
import spotty
import utils
from http_spotty_audio_streamer import HTTPSpottyAudioStreamer
from prebuffer import PrebufferManager, _clamp_prebuffer_seconds
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from string_ids import WELCOME_AUTHENTICATED_STR_ID
from playlist_next import get_next_playlist_item, parse_track_url
from nexttrack_broadcast import broadcast_to_nexttrack
from utils import ADDON_ID, ADDON_WINDOW_ID, PROXY_PORT, get_cached_auth_token, log_msg, log_exception

SPOTIFY_ADDON = xbmcaddon.Addon(id=ADDON_ID)

# Artist fanart for Music OSD (single largest image URL; no rotation – Spotify only provides same image in multiple sizes)
_artist_fanart_urls = []  # type: list
_artist_fanart_index = 0

_monitor = xbmc.Monitor()


def _clear_artist_fanart_rotation() -> None:
    global _artist_fanart_urls, _artist_fanart_index
    _artist_fanart_urls = []
    _artist_fanart_index = 0
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    win.clearProperty("Spotify.ArtistFanartCurrent")


def abort_app(timeout_in_secs: int) -> bool:
    return _monitor.waitForAbort(timeout_in_secs)


class _SpotifyOSDPlayerMonitor(xbmc.Player):
    """Clears Spotify OSD window properties when Kodi playback actually stops.

    We deliberately do NOT clear them from the HTTP stream callbacks because those
    fire whenever Kodi's internal buffer fills (mid-song), not only at true end-of-track.
    """

    def _clear(self) -> None:
        _clear_artist_fanart_rotation()
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        win.clearProperty("Spotify.CurrentTrackId")
        win.clearProperty("Spotify.CurrentTrackLiked")

    def onPlayBackStopped(self) -> None:
        self._clear()

    def onPlayBackEnded(self) -> None:
        self._clear()

    def onPlayBackError(self) -> None:
        self._clear()

    def onPlayBackStarted(self) -> None:
        # If a non-Spotify item starts playing, clear the Spotify OSD state.
        # Give Kodi a moment to populate MusicPlayer properties.
        def _check():
            xbmc.sleep(500)
            track_id = xbmc.getInfoLabel("MusicPlayer.Property(spotifytrackid)")
            if not track_id:
                self._clear()
        threading.Thread(target=_check, daemon=True).start()


class MainService:
    def __init__(self):
        log_msg(f"Spotify plugin version: {xbmcaddon.Addon(id=ADDON_ID).getAddonInfo('version')}.")

        self.__spotty_helper: SpottyHelper = SpottyHelper()
        self.__spotty = spotty.get_spotty(self.__spotty_helper)

        self.__spotty_auth: SpottyAuth = SpottyAuth(self.__spotty)
        self.__auth_token_expires_at = ""
        self.__welcome_msg = True
        
        use_spotify_normalization = SPOTIFY_ADDON.getSetting("use_spotify_normalization").lower() != "false"
        use_autoplay = SPOTIFY_ADDON.getSetting("spotify_autoplay").lower() == "true"
        use_passthrough = SPOTIFY_ADDON.getSetting("spotify_passthrough").lower() == "true"
        try:
            stream_volume = int(SPOTIFY_ADDON.getSetting("spotify_stream_volume") or 50)
        except (TypeError, ValueError):
            stream_volume = 50
        prebuffer_seconds = self._get_prebuffer_seconds_setting()
        self.__prebuffer_enabled = (
            SPOTIFY_ADDON.getSetting("prebuffer_enabled").lower() == "true"
        )
        self.__prebuffer_manager: PrebufferManager = PrebufferManager(
            self.__spotty,
            initial_volume=stream_volume,
            use_normalization=use_spotify_normalization,
            prebuffer_seconds=prebuffer_seconds,
        )
        self.__http_spotty_streamer: HTTPSpottyAudioStreamer = HTTPSpottyAudioStreamer(
            self.__spotty,
            use_spotify_normalization,
            stream_volume,
            prebuffer_manager=self.__prebuffer_manager,
            on_track_started_callback=self.__on_track_started,
            use_autoplay=use_autoplay,
            use_passthrough=use_passthrough,
        )
        self.__http_spotty_streamer.set_notify_track_finished(self.__on_track_finished)

        # Keep a strong reference so Kodi doesn't GC the player monitor.
        self.__osd_player_monitor = _SpotifyOSDPlayerMonitor()

        bottle_manager.route_all(self.__http_spotty_streamer)

    def __on_track_started(self, track_id: str, duration_sec: float) -> None:
        """Set OSD properties for Spotify track; pre-buffer next; broadcast to service.nexttrack."""
        global _artist_fanart_urls, _artist_fanart_index
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", track_id or "")
        win.setProperty("Spotify.CurrentTrackLiked", "")

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

        threading.Thread(target=_fetch_artist_fanart_urls, daemon=True).start()

        def _set_liked_state():
            try:
                token = get_cached_auth_token()
                if not token:
                    return
                sp = spotipy.Spotify(auth=token)
                result = sp.current_user_saved_tracks_contains([track_id])
                liked = "true" if (result and result[0]) else "false"
                win.setProperty("Spotify.CurrentTrackLiked", liked)
            except Exception:
                pass

        threading.Thread(target=_set_liked_state, daemon=True).start()

        try:
            current_item, next_item = get_next_playlist_item()
            if not next_item:
                return

            next_track_id, next_duration = parse_track_url(next_item.get("file") or "")
            if not next_track_id or next_duration is None:
                return

            prebuffer_enabled = (
                SPOTIFY_ADDON.getSetting("prebuffer_enabled").lower() == "true"
            )
            use_passthrough = (
                SPOTIFY_ADDON.getSetting("spotify_passthrough").lower() == "true"
            )
            if prebuffer_enabled and not use_passthrough:
                self.__prebuffer_manager.start_prebuffer(next_track_id, next_duration)

            broadcast_enabled = (
                SPOTIFY_ADDON.getSetting("broadcast_to_service_nexttrack").lower()
                != "false"
            )
            if broadcast_enabled:
                def _do_broadcast():
                    time.sleep(2)
                    try:
                        current_item, next_item = get_next_playlist_item()
                        if not next_item:
                            return
                        notification_sec = 15
                        v = SPOTIFY_ADDON.getSetting("upnext_preview_seconds") or "15"
                        try:
                            notification_sec = max(5, min(60, int(v)))
                        except (TypeError, ValueError):
                            pass
                        broadcast_to_nexttrack(
                            current_item,
                            next_item,
                            int(duration_sec),
                            notification_seconds=notification_sec,
                        )
                    except Exception:
                        pass
                t = threading.Thread(target=_do_broadcast, daemon=True)
                t.start()
        except Exception:
            pass

    def __on_track_finished(self, track_id: str) -> None:
        """Mark HTTP streamer as ended.

        OSD properties are NOT cleared here because this callback fires whenever
        Kodi's internal buffer fills (mid-song), not only at true end-of-track.
        The _SpotifyOSDPlayerMonitor clears them on real playback stop/end events.
        """
        self.__http_spotty_streamer.set_stream_ended()

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

            self.__prebuffer_manager.set_prebuffer_seconds(self._get_prebuffer_seconds_setting())
            prebuffer_enabled_now = (
                SPOTIFY_ADDON.getSetting("prebuffer_enabled").lower() == "true"
            )
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
        self.__prebuffer_manager.cancel_prebuffer()
        self.__http_spotty_streamer.stop()
        self.__spotty_helper.kill_all_spotties()
        bottle_manager.stop_thread()
        log_msg("Main service stopped.")

    def _get_prebuffer_seconds_setting(self) -> int:
        """
        Prebuffer duration (seconds). Clamped to 5–30 for memory safety.
        """
        try:
            v = int(SPOTIFY_ADDON.getSetting("prebuffer_seconds") or 15)
            return _clamp_prebuffer_seconds(v)
        except (TypeError, ValueError):
            return _clamp_prebuffer_seconds(15)

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
