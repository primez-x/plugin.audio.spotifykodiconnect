# -*- coding: utf-8 -*-
"""
Up Next integration for Spotify Kodi Connect.
Sends track information to the Up Next service so it can show
"next track" notifications during playback.
"""
from __future__ import absolute_import, unicode_literals

import json
import re
from base64 import b64encode

import xbmc

from utils import ADDON_ID, PROXY_PORT, log_msg
from xbmc import LOGDEBUG, LOGINFO

# Kodi music playlist id
PLAYLIST_MUSIC = 0

# URL pattern for our track endpoint: http://localhost:PORT/track/TRACK_ID/DURATION
_TRACK_URL_PATTERN = re.compile(
    r"^https?://localhost(?::\d+)?/track/([^/]+)/(\d+)(?:/)?$", re.I
)


def _jsonrpc(**kwargs):
    """Execute Kodi JSON-RPC. Returns parsed result or None on error."""
    if kwargs.get("id") is None:
        kwargs["id"] = 1
    if kwargs.get("jsonrpc") is None:
        kwargs["jsonrpc"] = "2.0"
    try:
        raw = xbmc.executeJSONRPC(json.dumps(kwargs))
        return json.loads(raw) if raw else None
    except (TypeError, ValueError) as e:
        log_msg("JSON-RPC error: %s" % e, LOGDEBUG)
        return None


def _get_active_player_id():
    """Return playerid of the active (music) player, or None."""
    result = _jsonrpc(method="Player.GetActivePlayers")
    if not result or "result" not in result:
        return None
    for p in result.get("result", []):
        if p.get("type") == "audio":
            return p.get("playerid")
    return None


def _get_current_playlist_position():
    """Return (playerid, position) for current item in music playlist, or (None, None)."""
    playerid = _get_active_player_id()
    if playerid is None:
        return None, None
    result = _jsonrpc(
        method="Player.GetProperties",
        params={"playerid": playerid, "properties": ["position"]},
    )
    if not result or "result" not in result:
        return playerid, None
    pos = result.get("result", {}).get("position")
    return playerid, pos if pos is not None else None


def _get_playlist_items(start_index, end_index):
    """
    Return list of playlist items for music playlist between start_index and end_index.
    Each item has 'file', 'label', 'title', 'art', etc.
    """
    result = _jsonrpc(
        method="Playlist.GetItems",
        params={
            "playlistid": PLAYLIST_MUSIC,
            "limits": {"start": start_index, "end": end_index},
            "properties": ["art", "file", "title", "duration"],
        },
    )
    if not result or "result" not in result:
        return []
    items = result.get("result", {}).get("items")
    return items if items else []


def parse_track_url(file_url):
    """
    Parse our track URL into (track_id, duration_sec).
    Returns (None, None) if not our URL or parse fails.
    """
    if not file_url:
        return None, None
    m = _TRACK_URL_PATTERN.match(file_url.strip())
    if not m:
        return None, None
    track_id, duration_str = m.group(1), m.group(2)
    try:
        duration_sec = int(duration_str)
        return track_id, duration_sec
    except ValueError:
        return None, None


def _episode_dict_from_playlist_item(item, track_id=None, duration_sec=None):
    """
    Build Up Next 'episode' dict from a music playlist item.
    Up Next expects keys like episodeid, title, art, runtime, showtitle, etc.
    We map track to 'episode' for compatibility.
    """
    if not item:
        return {}
    if track_id is None or duration_sec is None:
        track_id, duration_sec = parse_track_url(item.get("file") or "")
    title = (item.get("title") or item.get("label") or "").strip()
    art = item.get("art") or {}
    return {
        "episodeid": track_id or "",
        "tvshowid": "",
        "title": title or "Unknown",
        "art": {
            "thumb": art.get("thumb", ""),
            "tvshow.poster": art.get("thumb", ""),
            "tvshow.fanart": art.get("fanart", ""),
        },
        "season": 0,
        "episode": 0,
        "showtitle": "",
        "plot": "",
        "playcount": 0,
        "rating": 0,
        "firstaired": "",
        "runtime": duration_sec or 0,
    }


def _upnext_signal(data):
    """Send Up Next data via JSON-RPC NotifyAll (AddonSignals)."""
    sender = "%s.SIGNAL" % ADDON_ID
    encoded = b64encode(json.dumps(data).encode("utf-8")).decode("ascii")
    params = {
        "sender": sender,
        "message": "upnext_data",
        "data": [encoded],
    }
    _jsonrpc(method="JSONRPC.NotifyAll", params=params)


def get_next_playlist_item():
    """
    Get the next item in the music playlist (current position + 1).
    Returns (current_item, next_item) as playlist item dicts, or (None, None) if not available.
    """
    playerid, position = _get_current_playlist_position()
    if position is None:
        return None, None
    # current = position, next = position + 1
    items = _get_playlist_items(position, position + 2)
    current_item = items[0] if len(items) > 0 else None
    next_item = items[1] if len(items) > 1 else None
    return current_item, next_item


def send_upnext_signal_and_return_next_track(current_track_id, current_duration_sec):
    """
    If there is a next track in the playlist, send Up Next signal and return (next_track_id, next_duration_sec).
    Otherwise return (None, None).
    """
    current_item, next_item = get_next_playlist_item()
    if not next_item:
        return None, None

    next_track_id, next_duration_sec = parse_track_url(next_item.get("file") or "")
    if not next_track_id:
        return None, None

    # Build current "episode" from current track (we may not have current_item if position was wrong)
    current_episode = _episode_dict_from_playlist_item(
        current_item, current_track_id, current_duration_sec
    )
    if not current_episode.get("title") and current_item:
        current_episode["title"] = (
            current_item.get("title") or current_item.get("label") or "Unknown"
        )
    if not current_episode.get("runtime"):
        current_episode["runtime"] = current_duration_sec or 0

    next_episode = _episode_dict_from_playlist_item(
        next_item, next_track_id, next_duration_sec
    )

    play_url = "http://localhost:%s/track/%s/%s" % (
        PROXY_PORT,
        next_track_id,
        next_duration_sec,
    )

    # Show popup e.g. 30 seconds before end of track
    notification_time = 30
    if current_duration_sec and current_duration_sec > notification_time:
        notification_time = min(notification_time, current_duration_sec - 5)

    upnext_data = {
        "current_episode": current_episode,
        "next_episode": next_episode,
        "play_url": play_url,
        "notification_time": notification_time,
    }
    _upnext_signal(upnext_data)
    log_msg(
        "Up Next signal sent: current=%s, next=%s"
        % (current_track_id, next_track_id),
        LOGDEBUG,
    )
    return next_track_id, next_duration_sec
