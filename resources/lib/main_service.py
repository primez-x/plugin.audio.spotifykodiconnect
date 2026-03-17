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
from nexttrack_broadcast import broadcast_to_nexttrack
from playlist_next import get_next_playlist_item, parse_track_url
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

# Artist fanart for Music OSD (single largest image URL; no rotation – Spotify only provides same image in multiple sizes)
_artist_fanart_urls = []  # type: list
_artist_fanart_index = 0
# Track ID for which the liked state was last fetched. Prevents __on_track_started
# from resetting Spotify.CurrentTrackLiked on every Kodi buffering re-request.
_liked_state_track_id: str = ""


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
            threading.Thread(target=self._handle_toggle_like, daemon=True).start()

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


def abort_app(timeout_in_secs: int) -> bool:
    return _monitor.waitForAbort(timeout_in_secs)


class _SpotifyOSDPlayerMonitor(xbmc.Player):
    """Clears Spotify OSD window properties when Kodi playback actually stops.

    We deliberately do NOT clear them from the HTTP stream callbacks because those
    fire whenever Kodi's internal buffer fills (mid-song), not only at true end-of-track.
    """

    def _clear(self) -> None:
        global _liked_state_track_id
        _liked_state_track_id = ""
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
        log_msg(
            f"Spotify plugin version: {xbmcaddon.Addon(id=ADDON_ID).getAddonInfo('version')}."
        )

        self.__spotty_helper: SpottyHelper = SpottyHelper()
        self.__spotty = spotty.get_spotty(self.__spotty_helper)

        self.__spotty_auth: SpottyAuth = SpottyAuth(self.__spotty)
        self.__auth_token_expires_at = ""
        self.__welcome_msg = True

        normalization_setting = (
            (SPOTIFY_ADDON.getSetting("spotify_normalization") or "auto")
            .strip()
            .lower()
        )
        if normalization_setting not in ("off", "auto", "track", "album"):
            normalization_setting = "auto"
        use_autoplay = SPOTIFY_ADDON.getSetting("spotify_autoplay").lower() == "true"
        bitrate = self._get_bitrate_setting()
        self.__prebuffer_enabled = (
            SPOTIFY_ADDON.getSetting("prebuffer_enabled").lower() == "true"
        )
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
        self.__osd_player_monitor = _SpotifyOSDPlayerMonitor()

        # Cancellation token for _deferred_prebuffer threads.  Incremented each
        # time __on_track_started fires so only the latest thread proceeds to call
        # get_or_start.  Prevents cascade-mode threads from all firing at once and
        # evicting each other's buffers.
        self._prebuffer_token = 0
        self._prebuffer_token_lock = threading.Lock()

        bottle_manager.route_all(self.__http_spotty_streamer)

    def __on_track_started(self, track_id: str, duration_sec: float) -> None:
        """Set OSD properties for Spotify track; pre-buffer next; broadcast to service.nexttrack."""
        global _artist_fanart_urls, _artist_fanart_index, _liked_state_track_id
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        win.setProperty("Spotify.CurrentTrackId", track_id or "")
        # Only reset and re-query the liked state when the track actually changes.
        # Kodi issues fresh Range: bytes=0- requests for the same track during buffering,
        # which would otherwise wipe a user-toggled liked state mid-play.
        track_changed = track_id != _liked_state_track_id
        if track_changed:
            _liked_state_track_id = track_id
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
                log_msg(
                    f"Spotify.CurrentTrackLiked = {liked!r} for {track_id}.", LOGDEBUG
                )
            except Exception as e:
                log_msg(f"Error setting liked state for {track_id}: {e}", LOGWARNING)
                pass

        # Only run the liked state check when the track actually changes.
        if track_changed:
            threading.Thread(target=_set_liked_state, daemon=True).start()

        try:
            current_item, next_item = get_next_playlist_item()
            if not next_item:
                if SPOTIFY_ADDON.getSetting("spotify_autoplay").lower() == "true":
                    threading.Thread(
                        target=self.__queue_autoplay_tracks,
                        args=(track_id,),
                        daemon=True,
                    ).start()
                return

            next_track_id, next_duration = parse_track_url(next_item.get("file") or "")
            if not next_track_id or next_duration is None:
                return

            prebuffer_enabled = (
                SPOTIFY_ADDON.getSetting("prebuffer_enabled").lower() == "true"
            )
            # Prebuffer collects PCM bytes for the next track. Pass current
            # settings so prebuffer uses them without addon restart.
            # IMPORTANT: Delay prebuffer start so the main track's spotty process
            # has time to connect to Spotify first. Spotty uses a single Spotify
            # connection per account — starting the prebuffer's spotty immediately
            # causes it to compete with the main spotty, making both fail.
            if prebuffer_enabled:
                with self._prebuffer_token_lock:
                    self._prebuffer_token += 1
                    my_token = self._prebuffer_token

                def _deferred_prebuffer():
                    # Wait for the main stream to finish downloading to the disk cache.
                    # librespot only supports one stream per account; starting prebuffer
                    # while the main track is still downloading kicks the main stream.
                    from spotty_cache import SpottyCacheManager

                    # Wait for the main downloader to register and start.
                    time.sleep(2.0)

                    # During a cascade, many _deferred_prebuffer threads are spawned
                    # in quick succession (one per skipped track).  Only the most
                    # recent one should proceed — older threads would call get_or_start
                    # with stale track IDs, rapidly filling _recent_tracks past its
                    # 3-entry limit and evicting the freshly prebuffered track's buffer.
                    with self._prebuffer_token_lock:
                        if self._prebuffer_token != my_token:
                            log_msg(
                                f"_deferred_prebuffer: cancelled (stale, track={track_id})",
                                LOGDEBUG,
                            )
                            return

                    # Wait on the condition variable instead of polling every 1s;
                    # wakes up immediately when the download finishes.
                    dl = SpottyCacheManager.find_best_downloader(track_id, 0)
                    if dl is not None and not dl.is_finished:
                        with dl.cond:
                            while (
                                not dl.is_finished and not dl.error and not dl.aborted
                            ):
                                dl.cond.wait(timeout=30.0)

                    log_msg(
                        f"Main track {track_id} finished downloading. Safe to start prebuffer for next track.",
                        LOGDEBUG,
                    )

                    # Brief pause so Spotify's backend releases the previous
                    # session before the prebuffer's spotty process connects.
                    # Without this, the new spotty may get kicked immediately
                    # and exit with 0 PCM bytes (returncode=0, 0 bytes).
                    # 1s was insufficient (~1.3s total gap still caused 0-byte
                    # prebuffers); 2s gives enough margin for session release.
                    # 15s provides ample room for error on slow connections.
                    time.sleep(15.0)

                    # Final stale-check after the session-release sleep.
                    with self._prebuffer_token_lock:
                        if self._prebuffer_token != my_token:
                            log_msg(
                                f"_deferred_prebuffer: cancelled after sleep (track={track_id})",
                                LOGDEBUG,
                            )
                            return

                    # Re-query the playlist here instead of using the value
                    # captured at __on_track_started time.  When the HTTP
                    # stream starts, Kodi's player position has often not
                    # advanced yet, so the captured "next" is wrong (it is
                    # the current track or even the previous one).
                    try:
                        _, next_item_now = get_next_playlist_item()
                    except Exception:
                        return
                    if not next_item_now:
                        return
                    next_id_now, next_dur_now = parse_track_url(
                        next_item_now.get("file") or ""
                    )
                    if not next_id_now or next_dur_now is None:
                        return
                    # Guard: never prebuffer the track that triggered this
                    # deferred prebuffer — that means the playlist position
                    # is still stale and we would download the current track.
                    if next_id_now == track_id:
                        log_msg(
                            f"_deferred_prebuffer: next track same as triggering track"
                            f" ({track_id}), skipping.",
                            LOGDEBUG,
                        )
                        return

                    bitrate = self._get_bitrate_setting()
                    norm = (
                        (SPOTIFY_ADDON.getSetting("spotify_normalization") or "auto")
                        .strip()
                        .lower()
                    )
                    if norm not in ("off", "auto", "track", "album"):
                        norm = "auto"
                    self.__prebuffer_manager.start_prebuffer(
                        next_id_now,
                        next_dur_now,
                        bitrate=bitrate,
                        normalization_gain_type=norm,
                    )

                threading.Thread(target=_deferred_prebuffer, daemon=True).start()

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
                        broadcast_to_nexttrack(
                            current_item,
                            next_item,
                            int(duration_sec),
                        )
                    except Exception:
                        pass

                t = threading.Thread(target=_do_broadcast, daemon=True)
                t.start()
        except Exception:
            pass

    def __queue_autoplay_tracks(self, seed_track_id: str) -> None:
        """Fetch recommended tracks and append them to Kodi's music playlist."""
        try:
            token = get_cached_auth_token()
            if not token:
                log_msg("Autoplay: no auth token available.", LOGWARNING)
                return

            sp = spotipy.Spotify(auth=token)

            # Fetch a larger set of recommendations to fill the autoplay playlist.
            RECOMMEND_LIMIT = 49
            result = sp.recommendations(
                seed_tracks=[seed_track_id], limit=RECOMMEND_LIMIT
            )
            rec_tracks = (result or {}).get("tracks") or []
            if not rec_tracks:
                log_msg("Autoplay: no recommendations returned.", LOGDEBUG)
                return

            # Build a new playlist: put the current (seed) track first, then the recommendations.
            playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
            try:
                playlist.clear()
            except Exception:
                # Some Kodi versions may not support clear(); fall back to creating and replacing.
                pass

            added = 0

            # Add the current/seed track as the first item (fetch its metadata if possible).
            try:
                seed_info = sp.track(seed_track_id)
                seed_name = (seed_info or {}).get("name") or ""
                seed_duration_ms = (seed_info or {}).get("duration_ms") or 0
                seed_artists = (seed_info or {}).get("artists") or []
                seed_artist_name = (
                    seed_artists[0].get("name") or "" if seed_artists else ""
                )
                seed_album = (seed_info or {}).get("album") or {}
                seed_album_name = seed_album.get("name") or ""
                seed_images = seed_album.get("images") or []
                seed_art_url = seed_images[0].get("url") if seed_images else ""
                seed_duration_sec = (
                    math.ceil(seed_duration_ms / 1000) if seed_duration_ms else 1
                )
                seed_url = f"http://{PROXY_HOST}:{PROXY_PORT}/track/{seed_track_id}/{seed_duration_sec}.wav"
                li = xbmcgui.ListItem(label=seed_name or seed_track_id)
                li.setProperty("IsPlayable", "true")
                li.setProperty("spotifytrackid", seed_track_id)
                # Set rich music info and artwork so Kodi shows titles, artist and cover art.
                li.setInfo(
                    "music",
                    {
                        "title": seed_name,
                        "artist": seed_artist_name,
                        "album": seed_album_name,
                        "duration": seed_duration_sec,
                    },
                )
                if seed_art_url:
                    try:
                        li.setArt(
                            {
                                "thumb": seed_art_url,
                                "icon": seed_art_url,
                                "fanart": seed_art_url,
                            }
                        )
                    except Exception:
                        pass
                playlist.add(seed_url, li)
                added += 1
            except Exception:
                # If fetching metadata fails, still add a minimal entry for the seed track.
                try:
                    seed_url = f"http://{PROXY_HOST}:{PROXY_PORT}/track/{seed_track_id}/1.wav"
                    li = xbmcgui.ListItem(label=seed_track_id)
                    li.setProperty("IsPlayable", "true")
                    li.setProperty("spotifytrackid", seed_track_id)
                    playlist.add(seed_url, li)
                    added += 1
                except Exception:
                    pass

            # Helper to avoid duplicates (seed may appear in recommendations).
            seen_ids = {seed_track_id}

            # Fetch recommended tracks in batches to reduce API calls and follow the
            # same batched-fetch pattern used elsewhere in the addon.
            rec_ids = [t.get("id") for t in rec_tracks if t.get("id")]
            # Keep original recommendation order but remove duplicates and already seen IDs.
            rec_ids = [rid for rid in rec_ids if rid and rid not in seen_ids]

            from utils import get_chunks

            for chunk in get_chunks(rec_ids, 20):
                try:
                    batch = sp.tracks(chunk, market=None).get("tracks") or []
                except Exception:
                    # On error, fall back to per-track calls for this chunk
                    batch = []
                    for tid in chunk:
                        try:
                            t = sp.track(tid)
                            batch.append(t)
                        except Exception:
                            continue

                for full in batch:
                    if added >= (RECOMMEND_LIMIT + 1):
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
                        duration_sec = (
                            math.ceil(duration_ms / 1000) if duration_ms else 1
                        )
                        url = (
                            f"http://{PROXY_HOST}:{PROXY_PORT}/track/{tid}/{duration_sec}.wav"
                        )
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
                f"Autoplay: built new playlist with {added} items (seed={seed_track_id}).",
                LOGDEBUG,
            )

        except Exception as exc:
            log_msg(f"Autoplay: failed to build autoplay playlist: {exc}", LOGWARNING)

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
            xbmcgui.Dialog().notification(
                addon_name, msg, icon=icon, time=2000, sound=False
            )
        except Exception as exc:
            log_exception(exc, "Could not show welcome notification")
