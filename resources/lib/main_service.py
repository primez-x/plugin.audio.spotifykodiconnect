"""
    plugin.audio.spotifykodiconnect
    Spotify Kodi Connect - service: spotty + playlist sync to Kodi (real playlists).
"""

import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

import bottle_manager
import spotty
import utils
from http_spotty_audio_streamer import HTTPSpottyAudioStreamer
from http_video_player_setter import HttpVideoPlayerSetter
from prebuffer import PrebufferManager, _clamp_prebuffer_seconds
from save_recently_played import SaveRecentlyPlayed
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from string_ids import HTTP_VIDEO_RULE_ADDED_STR_ID, WELCOME_AUTHENTICATED_STR_ID
from playlist_next import get_next_playlist_item, parse_track_url
from nexttrack_broadcast import broadcast_to_nexttrack
from utils import ADDON_ID, PROXY_PORT, log_msg, log_exception

SAVE_TO_RECENTLY_PLAYED_FILE = True
SPOTIFY_ADDON = xbmcaddon.Addon(id=ADDON_ID)


def abort_app(timeout_in_secs: int) -> bool:
    return xbmc.Monitor().waitForAbort(timeout_in_secs)


def add_http_video_rule() -> None:
    video_player_setter = HttpVideoPlayerSetter()

    if not video_player_setter.set_http_rule():
        return

    msg = SPOTIFY_ADDON.getLocalizedString(HTTP_VIDEO_RULE_ADDED_STR_ID)
    dialog = xbmcgui.Dialog()
    header = SPOTIFY_ADDON.getAddonInfo("name")
    dialog.ok(header, msg)


class MainService:
    def __init__(self):
        log_msg(f"Spotify plugin version: {xbmcaddon.Addon(id=ADDON_ID).getAddonInfo('version')}.")

        self.__spotty_helper: SpottyHelper = SpottyHelper()
        self.__spotty = spotty.get_spotty(self.__spotty_helper)

        self.__spotty_auth: SpottyAuth = SpottyAuth(self.__spotty)
        self.__auth_token_expires_at = ""
        self.__welcome_msg = True

        # Workaround to make Kodi use it's VideoPlayer to play http audio streams.
        # If we don't do this, then Kodi uses PAPlayer which does not stream.
        add_http_video_rule()

        gap_between_tracks = int(SPOTIFY_ADDON.getSetting("gap_between_playlist_tracks"))
        # Use fixed defaults: normalization and stream volume no longer user settings
        # (Kodi controls playback/audio; these had no effect on CoreELEC Kodi).
        use_spotify_normalization = True
        stream_volume = 50
        prebuffer_seconds = self._get_prebuffer_seconds_setting()
        self.__prebuffer_enabled = (
            SPOTIFY_ADDON.getSetting("upnext_prebuffer_enabled").lower() == "true"
        )
        self.__prebuffer_manager: PrebufferManager = PrebufferManager(
            self.__spotty,
            initial_volume=stream_volume,
            use_normalization=use_spotify_normalization,
            prebuffer_seconds=prebuffer_seconds,
        )
        self.__http_spotty_streamer: HTTPSpottyAudioStreamer = HTTPSpottyAudioStreamer(
            self.__spotty,
            gap_between_tracks,
            use_spotify_normalization,
            stream_volume,
            prebuffer_manager=self.__prebuffer_manager,
            on_track_started_callback=self.__on_track_started,
        )
        self.__save_recently_played: SaveRecentlyPlayed = SaveRecentlyPlayed()
        self.__http_spotty_streamer.set_notify_track_finished(self.__save_track_to_recently_played)

        bottle_manager.route_all(self.__http_spotty_streamer)

    def __on_track_started(self, track_id: str, duration_sec: float) -> None:
        """Pre-buffer the next track if available; broadcast to service.nexttrack."""
        try:
            current_item, next_item = get_next_playlist_item()
            if not next_item:
                return

            next_track_id, next_duration = parse_track_url(next_item.get("file") or "")
            if not next_track_id or next_duration is None:
                return

            prebuffer_enabled = (
                SPOTIFY_ADDON.getSetting("upnext_prebuffer_enabled").lower() == "true"
            )
            if prebuffer_enabled:
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

    def __save_track_to_recently_played(self, track_id: str) -> None:
        if SAVE_TO_RECENTLY_PLAYED_FILE:
            self.__save_recently_played.save_track(track_id)

    def run(self) -> None:
        log_msg("Starting main service loop.")

        bottle_manager.start_thread(PROXY_PORT)
        log_msg(f"Started bottle with port {PROXY_PORT}.")

        self.__renew_token()

        loop_counter = 0
        loop_wait_in_secs = 6
        use_normalization = True
        stream_volume = 50
        while True:
            loop_counter += 1
            if (loop_counter % 10) == 0:
                log_msg(f"Main loop continuing. Loop counter: {loop_counter}.")
            self.__http_spotty_streamer.use_normalization(use_normalization)
            self.__http_spotty_streamer.set_stream_volume(stream_volume)
            self.__prebuffer_manager.set_volume(stream_volume)
            self.__prebuffer_manager.set_use_normalization(use_normalization)
            self.__prebuffer_manager.set_prebuffer_seconds(self._get_prebuffer_seconds_setting())
            prebuffer_enabled_now = (
                SPOTIFY_ADDON.getSetting("upnext_prebuffer_enabled").lower() == "true"
            )
            if self.__prebuffer_enabled and not prebuffer_enabled_now:
                # User disabled prebuffer; stop any in-flight work.
                self.__prebuffer_manager.cancel_prebuffer()
            self.__prebuffer_enabled = prebuffer_enabled_now

            # Monitor authorization.
            if self.__auth_token_expires_at == "":
                log_msg("Spotify not yet authorized.")
                log_msg("Refreshing auth token now.")
                self.__renew_token()
            elif (int(self.__auth_token_expires_at) - 60) <= int(time.time()):
                expire_time = int(self.__auth_token_expires_at)
                time_now = int(time.time())
                log_msg(
                    f"Spotify token expired."
                    f" Expire time: {utils.get_time_str(expire_time)} ({expire_time});"
                    f" time now: {utils.get_time_str(time_now)} ({time_now})."
                )
                log_msg("Refreshing auth token now.")
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
        Prebuffer duration (seconds) derived from the user-configured
        preview duration setting. Clamped to 5–30 for memory safety.
        """
        try:
            v = int(SPOTIFY_ADDON.getSetting("upnext_preview_seconds") or 15)
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
