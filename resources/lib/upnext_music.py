# -*- coding: utf-8 -*-
"""
Up Next Music: built-in "next track" overlay for Spotify Kodi Connect.
Runs when enabled in settings; uses music-appropriate defaults (e.g. always
continue playback when user does nothing). Does not depend on service.upnext.
"""
from __future__ import absolute_import, unicode_literals

import json
import threading
import time

import xbmc
import xbmcaddon

from utils import ADDON_ID, log_msg
from xbmc import LOGDEBUG

from upnext_music_dialog import UpNextMusicDialog, addon_path

ADDON = xbmcaddon.Addon(id=ADDON_ID)

# Notification thread and cancel event; new track start cancels previous wait
_cancel_event = None
_notification_thread = None


def _jsonrpc(**kwargs):
    if kwargs.get("id") is None:
        kwargs["id"] = 1
    if kwargs.get("jsonrpc") is None:
        kwargs["jsonrpc"] = "2.0"
    try:
        raw = xbmc.executeJSONRPC(json.dumps(kwargs))
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _get_audio_player_time():
    """Return (time_sec, total_sec) for active audio player, or (None, None)."""
    r = _jsonrpc(method="Player.GetActivePlayers")
    if not r or "result" not in r:
        return None, None
    playerid = None
    for p in r.get("result", []):
        if p.get("type") == "audio":
            playerid = p.get("playerid")
            break
    if playerid is None:
        return None, None
    r = _jsonrpc(
        method="Player.GetProperties",
        params={"playerid": playerid, "properties": ["time", "totaltime"]},
    )
    if not r or "result" not in r:
        return None, None
    res = r["result"]
    t = res.get("time", {})
    total = res.get("totaltime", {})
    try:
        time_sec = t.get("hours", 0) * 3600 + t.get("minutes", 0) * 60 + t.get("seconds", 0)
        total_sec = (
            total.get("hours", 0) * 3600
            + total.get("minutes", 0) * 60
            + total.get("seconds", 0)
        )
        return time_sec, total_sec
    except Exception:
        return None, None


def _next_item_info_for_dialog(next_item, next_duration_sec):
    """Build dict for UpNextMusicDialog from playlist next_item."""
    if not next_item:
        return {"title": "", "artist": "", "art": {}, "runtime": 0}
    art = next_item.get("art") or {}
    artist = next_item.get("artist")
    if isinstance(artist, list):
        artist = artist[0] if artist else ""
    elif artist is None:
        artist = ""
    else:
        artist = str(artist)
    return {
        "title": (next_item.get("title") or next_item.get("label") or "").strip() or "Unknown",
        "artist": artist,
        "art": {
            "thumb": art.get("thumb", ""),
            "tvshow.landscape": art.get("thumb", ""),
            "tvshow.fanart": art.get("fanart", ""),
            "fanart": art.get("fanart", ""),
        },
        "runtime": next_duration_sec or 0,
    }


def _calculate_progress_steps(remaining_sec):
    """Step size per 100ms so progress bar empties over remaining_sec."""
    if remaining_sec <= 0:
        return 0
    steps = max(1, int(remaining_sec * 10))
    return 100.0 / steps


def is_enabled():
    return ADDON.getSetting("upnext_enabled").lower() == "true"


def get_notification_seconds():
    try:
        v = int(ADDON.getSetting("upnext_preview_seconds") or "15")
        return max(5, min(45, v))
    except (TypeError, ValueError):
        return 15


def cancel_notification():
    """Cancel any pending Up Next Music wait/dialog (e.g. when feature is disabled)."""
    global _cancel_event, _notification_thread
    if _cancel_event:
        _cancel_event.set()
    if _notification_thread and _notification_thread.is_alive():
        _notification_thread.join(timeout=2)


def _wait_and_show_dialog(
    duration_sec,
    notification_seconds,
    next_item_info,
    cancel_event,
):
    """Run in thread: wait until (duration - time) <= notification_seconds, then show dialog."""
    time_sec = total_sec = None
    while not cancel_event.is_set():
        time.sleep(1)
        if cancel_event.is_set():
            return
        time_sec, total_sec = _get_audio_player_time()
        if time_sec is None or total_sec is None or total_sec <= 0:
            continue
        remaining = total_sec - time_sec
        if remaining <= notification_seconds:
            break
    if cancel_event.is_set():
        return
    # Show dialog (music default = continue; we don't stop playback)
    path = addon_path()
    dialog = UpNextMusicDialog(
        "script-upnextmusic-upnext.xml", path, "default", "1080i"
    )
    dialog.set_item(next_item_info)
    remaining = max(0, (total_sec or duration_sec) - (time_sec or 0))
    if remaining <= 0:
        remaining = notification_seconds
    step = _calculate_progress_steps(remaining)
    dialog.set_progress_step_size(step)
    dialog.show()
    # Update progress until playback near end or user closes
    player = xbmc.Player()
    while not cancel_event.is_set():
        if not player.isPlaying():
            break
        try:
            t = player.getTime()
            total = player.getTotalTime()
        except RuntimeError:
            break
        if total - t <= 1:
            break
        if dialog.is_cancel() or dialog.is_play_next():
            break
        remaining = int(total - t)
        dialog.update_progress_control(remaining=remaining, runtime=remaining)
        time.sleep(0.1)
    try:
        dialog.close()
    except Exception:
        pass


def start_notification_thread(duration_sec, next_item, next_duration_sec):
    """
    Cancel any previous notification thread and start a new one that will
    show the Up Next Music dialog notification_seconds before track end.
    """
    global _cancel_event, _notification_thread
    if _cancel_event:
        _cancel_event.set()
    if _notification_thread and _notification_thread.is_alive():
        _notification_thread.join(timeout=2)
    _cancel_event = threading.Event()
    next_item_info = _next_item_info_for_dialog(next_item, next_duration_sec)
    notification_seconds = get_notification_seconds()
    _notification_thread = threading.Thread(
        target=_wait_and_show_dialog,
        args=(
            duration_sec,
            notification_seconds,
            next_item_info,
            _cancel_event,
        ),
        daemon=True,
    )
    _notification_thread.start()
    log_msg(
        "Up Next Music thread started (show in %s s)" % notification_seconds,
        LOGDEBUG,
    )
