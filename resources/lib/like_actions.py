"""Shared helpers for the Like/Unlike actions (service OSD handler, proxy, plugin)."""

import threading

import xbmcaddon
import xbmcgui
from string_ids import SPOTIFY_CONNECTING_STR_ID
from utils import ADDON_ID, ADDON_WINDOW_ID, log_exception, log_msg
from xbmc import LOGWARNING

_lock = threading.Lock()
_latest_request = 0


def begin_like_request() -> int:
    """Register a like/unlike request; only the latest one may update the window."""
    global _latest_request
    with _lock:
        _latest_request += 1
        return _latest_request


def set_current_track_liked(track_id: str, liked: bool, request: int) -> bool:
    """Write Spotify.CurrentTrackLiked for track_id unless the request is stale.

    A request is stale when a newer one began or playback moved to another
    track while the Spotify call was in flight; its result must not touch the
    icon of whatever is playing now. Returns True when the property was written.
    """
    with _lock:
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        if request != _latest_request or win.getProperty("Spotify.CurrentTrackId") != track_id:
            log_msg(f"Ignoring stale liked-state update for {track_id}.")
            return False
        if liked:
            win.setProperty("Spotify.CurrentTrackLiked", "true")
        else:
            win.clearProperty("Spotify.CurrentTrackLiked")
        return True


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
