# -*- coding: utf-8 -*-
"""
Broadcast "Up Next" data to service.upnextmusic so the Up Next - Music
service addon can show the next-track overlay and handle play-next.
Uses the same JSONRPC.NotifyAll + base64 payload format that service.upnextmusic
expects in its onNotification(sender, method, data) where method ends with
'upnext_data'.
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
_MESSAGE = "upnext_data"
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


def _episodeid_for_file(file_url):
    """Stable numeric id for service.upnextmusic (expects episodeid as int)."""
    if not file_url:
        return 0
    h = hash(file_url) & 0x7FFFFFFF
    return h if h else 1


def _build_episode_from_playlist_item(item, duration_sec=None):
    """Build episode-like dict for service.upnextmusic (next_episode/current_episode)."""
    if not item:
        return None
    item, item_duration = _unwrap_item(item)
    if not item:
        return None
    file_url = item.get("file") or ""
    title = (item.get("title") or item.get("label") or "").strip() or "Unknown"
    artist = item.get("artist")
    if isinstance(artist, list):
        artist = artist[0] if artist else ""
    elif artist is None:
        artist = ""
    else:
        artist = str(artist)
    art = item.get("art") or {}
    runtime = duration_sec if duration_sec is not None else item_duration
    return {
        "episodeid": _episodeid_for_file(file_url),
        "title": title,
        "artist": artist,
        "album": item.get("album") or "",
        "art": art,
        "duration": runtime,
        "runtime": runtime,
        "file": file_url,
    }


def _encode_payload(data):
    """Encode payload for service.upnextmusic (base64 JSON, same as Up Next utils)."""
    json_bytes = json.dumps(data).encode("utf-8")
    encoded = base64.b64encode(json_bytes)
    if sys.version_info[0] >= 3:
        encoded = encoded.decode("ascii")
    return encoded


def broadcast_to_service_upnextmusic(
    current_playlist_item,
    next_playlist_item,
    current_duration_sec,
    notification_seconds=15,
):
    """
    Send upnext_data to service.upnextmusic so it can show the next-track
    overlay and offer to play the next track.

    Call this when a track starts (e.g. from MainService.__on_track_started).
    Pass the current and next playlist items as returned by get_next_playlist_item(),
    and the current track duration in seconds. notification_seconds is when
    before end of track to show the popup (default 15).
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

    next_episode = _build_episode_from_playlist_item(next_playlist_item, next_duration)
    current_episode = _build_episode_from_playlist_item(
        current_playlist_item, current_duration_sec
    )
    if not next_episode:
        return

    payload = {
        "next_episode": next_episode,
        "current_episode": current_episode,
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
            "Broadcast upnext_data to service.upnextmusic (next: %s)"
            % next_episode.get("title", ""),
            LOGDEBUG,
        )
    except Exception as e:
        log_msg("Failed to broadcast upnext_data: %s" % e, LOGDEBUG)
