"""Shared helpers for the Like/Unlike actions (service OSD handler, proxy, plugin)."""

import json
import threading

import xbmc
import xbmcaddon
import xbmcgui
from string_ids import SPOTIFY_CONNECTING_STR_ID
from utils import ADDON_ID, ADDON_WINDOW_ID, log_exception, log_msg
from xbmc import LOGWARNING

# Held by every service write that changes Spotify.CurrentTrackId or its liked
# state, so a liked result cannot land between a track change and its reset.
LIKED_STATE_LOCK = threading.RLock()


def set_current_track_liked(track_id: str, liked: bool) -> bool:
    """Write Spotify.CurrentTrackLiked for track_id unless playback moved on.

    A Spotify call that finishes after playback advanced to another track must
    not touch the icon of whatever is playing now. Returns True when written.
    """
    with LIKED_STATE_LOCK:
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        if win.getProperty("Spotify.CurrentTrackId") != track_id:
            log_msg(f"Ignoring stale liked-state update for {track_id}.")
            return False
        if liked:
            win.setProperty("Spotify.CurrentTrackLiked", "true")
        else:
            win.clearProperty("Spotify.CurrentTrackLiked")
        return True


def send_liked_to_service(track_id: str, liked: bool) -> None:
    """Hand a liked result from the plugin process to the service, which owns the heart icon."""
    xbmc.executeJSONRPC(json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "JSONRPC.NotifyAll",
        "params": {"sender": ADDON_ID, "message": "SetLiked", "data": {"track_id": track_id, "liked": liked}},
    }))


def notify_not_connected() -> None:
    """Tell the user a Like action was ignored because Spotify has no valid token."""
    log_msg("Like ignored: no valid Spotify auth token; service is still connecting.", LOGWARNING)
    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
        xbmcgui.Dialog().notification(
            addon.getAddonInfo("name"),
            addon.getLocalizedString(SPOTIFY_CONNECTING_STR_ID),
            icon=addon.getAddonInfo("icon"),
            time=3000,
            sound=False,
        )
    except Exception as exc:
        log_exception(exc, "not-connected notification")
