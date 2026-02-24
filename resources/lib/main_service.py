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
from save_recently_played import SaveRecentlyPlayed
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from string_ids import HTTP_VIDEO_RULE_ADDED_STR_ID
from utils import ADDON_ID, PROXY_PORT, log_msg, log_exception

try:
    import spotipy
    import playlist_sync
    HAS_PLAYLIST_SYNC = True
except Exception:
    HAS_PLAYLIST_SYNC = False

try:
    from connect import runner as connect_runner
    HAS_CONNECT_RECEIVER = True
except Exception:
    HAS_CONNECT_RECEIVER = False

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
        self.__last_playlist_sync = 0.0
        self.__playlist_sync_interval_sec = 30 * 60  # 30 minutes
        self.__connect_stop = threading.Event()
        self.__connect_thread = None

        # Workaround to make Kodi use it's VideoPlayer to play http audio streams.
        # If we don't do this, then Kodi uses PAPlayer which does not stream.
        add_http_video_rule()

        gap_between_tracks = int(SPOTIFY_ADDON.getSetting("gap_between_playlist_tracks"))
        use_spotify_normalization = (
            SPOTIFY_ADDON.getSetting("use_spotify_normalization").lower() == "true"
        )
        problem_with_terminate_streaming = (
            SPOTIFY_ADDON.getSetting("problem_with_terminate_streaming").lower() == "true"
        )
        self.__http_spotty_streamer: HTTPSpottyAudioStreamer = HTTPSpottyAudioStreamer(
            self.__spotty,
            gap_between_tracks,
            use_spotify_normalization,
            problem_with_terminate_streaming,
        )
        self.__save_recently_played: SaveRecentlyPlayed = SaveRecentlyPlayed()
        self.__http_spotty_streamer.set_notify_track_finished(self.__save_track_to_recently_played)

        bottle_manager.route_all(self.__http_spotty_streamer)

    def __save_track_to_recently_played(self, track_id: str) -> None:
        if SAVE_TO_RECENTLY_PLAYED_FILE:
            self.__save_recently_played.save_track(track_id)

    def run(self) -> None:
        log_msg("Starting main service loop.")

        bottle_manager.start_thread(PROXY_PORT)
        log_msg(f"Started bottle with port {PROXY_PORT}.")

        # Start Spotify Connect receiver (LibreSpot) if enabled
        if HAS_CONNECT_RECEIVER and SPOTIFY_ADDON.getSetting("connect_receiver") == "true":
            self.__connect_stop.clear()
            self.__connect_thread = threading.Thread(
                target=connect_runner.run,
                kwargs={"stop_event": self.__connect_stop},
                daemon=True,
            )
            self.__connect_thread.start()
            log_msg("Connect receiver thread started.")

        self.__renew_token()

        loop_counter = 0
        loop_wait_in_secs = 6
        while True:
            loop_counter += 1
            if (loop_counter % 10) == 0:
                log_msg(f"Main loop continuing. Loop counter: {loop_counter}.")

            self.__http_spotty_streamer.use_normalization(
                SPOTIFY_ADDON.getSetting("use_spotify_normalization").lower() == "true"
            )

            # Monitor authorization.
            if self.__auth_token_expires_at == "":
                log_msg("Spotify not yet authorized.")
                log_msg("Refreshing auth token now.")
                self.__renew_token()
                self._run_playlist_sync_if_authenticated()
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
                self._run_playlist_sync_if_authenticated()

            # Periodic sync: Spotify playlists → Kodi .m3u (Music → Playlists)
            if HAS_PLAYLIST_SYNC and self.__auth_token_expires_at:
                now = time.time()
                if now - self.__last_playlist_sync >= self.__playlist_sync_interval_sec:
                    self._run_playlist_sync_if_authenticated()

            if abort_app(loop_wait_in_secs):
                log_msg("Aborting the main service.")
                break

        self.__close()

    def __close(self) -> None:
        log_msg("Shutdown requested.")
        self.__connect_stop.set()
        if self.__connect_thread and self.__connect_thread.is_alive():
            self.__connect_thread.join(timeout=5)
        self.__http_spotty_streamer.stop()
        self.__spotty_helper.kill_all_spotties()
        bottle_manager.stop_thread()
        log_msg("Main service stopped.")

    def _run_playlist_sync_if_authenticated(self) -> None:
        """Run playlist sync in a background thread if we have a token."""
        if not HAS_PLAYLIST_SYNC:
            return
        auth_token = utils.get_cached_auth_token()
        if not auth_token:
            return
        self.__last_playlist_sync = time.time()

        def _sync():
            try:
                sp = spotipy.Spotify(auth=auth_token)
                playlist_sync.sync_playlists_to_kodi(sp)
            except Exception as e:
                log_exception(e, "playlist_sync from service")

        t = threading.Thread(target=_sync, daemon=True)
        t.start()

    def __renew_token(self) -> None:
        try:
            self.__spotty_auth.renew_token()
            self.__auth_token_expires_at = utils.get_cached_auth_token_expires_at()
        except Exception as exc:
            log_exception(exc, "Could not renew Spotify auth token")
            self.__auth_token_expires_at = ""
