# -*- coding: utf-8 -*-
"""
Broadcast "Next Track" data to service.nexttrack so the Next Track
service addon can show the next-track overlay and handle play-next.
Uses the same JSONRPC.NotifyAll + base64 payload format that service.nexttrack
expects in its onNotification(sender, method, data) where method ends with
'nexttrack_data'.
"""
from __future__ import absolute_import, unicode_literals

import base64
import json
import sys

import xbmc
import xbmcaddon

from playlist_next import parse_track_url
from utils import ADDON_ID, log_msg
from xbmc import LOGDEBUG

_SENDER = "plugin.audio.spotifykodiconnect.SIGNAL"
_MESSAGE = "nexttrack_data"
_ENCODING = "base64"


def _unwrap_item(item):
    """Kodi Playlist.GetItems may return { item: {...}, duration: N }; use inner item."""
    if not item:
        return item, 0
    duration = 0
    if "duration" in item and isinstance(item.get("duration"), (int, float)):
        duration = int(item["duration"])
    inner = item.get("item")
    if inner is not None:
        return inner, duration
    return item, duration


def _trackid_for_file(file_url):
    """Stable numeric id for service.nexttrack (expects trackid as int)."""
    if not file_url:
        return 0
    h = hash(file_url) & 0x7FFFFFFF
    return h if h else 1


def _build_track_from_playlist_item(item, duration_sec=None):
    """Build track dict for service.nexttrack (next_track/current_track)."""
    if not item:
        return None
    item, item_duration = _unwrap_item(item)
    if not item:
        return None
    file_url = item.get("file") or ""
    title = (item.get("title") or item.get("label") or "").strip() or "Unknown"
    artist = item.get("artist")
    if isinstance(artist, list):
        artist = " / ".join(a for a in artist if a) if artist else ""
    elif artist is None:
        artist = ""
    else:
        artist = str(artist)
    art = item.get("art") or {}
    runtime = duration_sec if duration_sec is not None else item_duration
    return {
        "trackid": _trackid_for_file(file_url),
        "title": title,
        "artist": artist,
        "album": item.get("album") or "",
        "art": art,
        "duration": runtime,
        "runtime": runtime,
        "file": file_url,
    }


def _encode_payload(data):
    """Encode payload for service.nexttrack (base64 JSON)."""
    json_bytes = json.dumps(data).encode("utf-8")
    encoded = base64.b64encode(json_bytes)
    if sys.version_info[0] >= 3:
        encoded = encoded.decode("ascii")
    return encoded


def broadcast_to_nexttrack(
    current_playlist_item,
    next_playlist_item,
    current_duration_sec,
    notification_seconds=15,
):
    """
    Send nexttrack_data to service.nexttrack so it can show the next-track
    overlay and offer to play the next track.

    Call this when a track starts (e.g. from MainService.__on_track_started).
    """
    if not next_playlist_item:
        return
    next_file = next_playlist_item.get("file")
    inner_next = next_playlist_item.get("item") or next_playlist_item
    if not next_file and inner_next:
        next_file = inner_next.get("file")
    if not next_file:
        return

    _, next_duration = parse_track_url(next_file)
    next_duration = next_duration or 0

    next_track = _build_track_from_playlist_item(next_playlist_item, next_duration)
    current_track = _build_track_from_playlist_item(
        current_playlist_item, current_duration_sec
    )
    if not next_track:
        return

    payload = {
        "next_track": next_track,
        "current_track": current_track,
        "play_url": next_file,
        "notification_time": max(5, min(60, int(notification_seconds))),
    }

    encoded = _encode_payload(payload)
    params = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "JSONRPC.NotifyAll",
        "params": {
            "sender": _SENDER,
            "message": _MESSAGE,
            "data": [encoded],
        },
    }
    try:
        raw = xbmc.executeJSONRPC(json.dumps(params))
        log_msg(
            "Broadcast nexttrack_data to service.nexttrack (next: %s)"
            % next_track.get("title", ""),
            LOGDEBUG,
        )
    except Exception as e:
        log_msg("Failed to broadcast nexttrack_data: %s" % e, LOGDEBUG)
