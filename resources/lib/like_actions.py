"""Shared helpers for the Like/Unlike actions (service OSD handler, proxy, plugin)."""

import xbmcaddon
import xbmcgui
from string_ids import SPOTIFY_CONNECTING_STR_ID
from utils import ADDON_ID, ADDON_WINDOW_ID, log_exception, log_msg
from xbmc import LOGWARNING


def set_current_track_liked(track_id: str, liked: bool) -> bool:
    """Write Spotify.CurrentTrackLiked for track_id unless playback moved on.

    A Spotify call that finishes after playback advanced to another track must
    not touch the icon of whatever is playing now. Returns True when written.
    """
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    if win.getProperty("Spotify.CurrentTrackId") != track_id:
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
