# -*- coding: utf-8 -*-
"""
Next-track helpers for SpotifyKodiConnect.

This module intentionally contains **no** integration with the external
`service.nexttrack` addon (no JSONRPC.NotifyAll, no nexttrack_data signals).
It only provides utilities to inspect Kodi's music playlist to determine
the current and next queued track and to parse our stream URLs.
"""
from __future__ import absolute_import, unicode_literals

import json
import re

import xbmc

from utils import log_msg
from xbmc import LOGDEBUG

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


def _get_active_audio_player_id():
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
    playerid = _get_active_audio_player_id()
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
            "properties": ["art", "file", "title", "duration", "artist", "album"],
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


def get_next_playlist_item():
    """
    Get the next item in the music playlist (current position + 1).
    Returns (current_item, next_item) as playlist item dicts, or (None, None) if not available.
    """
    _playerid, position = _get_current_playlist_position()
    if position is None:
        return None, None
    # current = position, next = position + 1
    items = _get_playlist_items(position, position + 2)
    current_item = items[0] if len(items) > 0 else None
    next_item = items[1] if len(items) > 1 else None
    return current_item, next_item

