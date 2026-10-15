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

# Canonical integer milliseconds have no file extension. Saved URLs can use seconds.
_TRACK_URL_PATTERN = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?/track/([^/]+)/([0-9]+ms|[0-9]+(?:\.[0-9]+)?(?:\.wav)?)/?$",
    re.I,
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


def _time_value_to_seconds(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, dict):
        return None
    try:
        return (
            int(value.get("hours", 0)) * 3600
            + int(value.get("minutes", 0)) * 60
            + int(value.get("seconds", 0))
            + int(value.get("milliseconds", 0)) / 1000.0
        )
    except (TypeError, ValueError):
        return None


def get_current_playback_time_seconds():
    """Return active audio playback time in seconds, or None when unavailable."""
    playerid = _get_active_audio_player_id()
    if playerid is None:
        return None
    result = _jsonrpc(
        method="Player.GetProperties",
        params={"playerid": playerid, "properties": ["time"]},
    )
    if not result or "result" not in result:
        return None
    return _time_value_to_seconds(result.get("result", {}).get("time"))


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
        duration_str = duration_str.lower()
        if duration_str.endswith("ms"):
            duration_sec = int(duration_str[:-2]) / 1000.0
        else:
            duration_sec = float(duration_str[:-4] if duration_str.endswith(".wav") else duration_str)
        return track_id, duration_sec
    except (ValueError, OverflowError):
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


def get_playlist_track_ids():
    """
    Return the set of Spotify track IDs currently in Kodi's music playlist.

    Used by autoplay to avoid appending recommendations that are already in
    the playlist (now that autoplay appends instead of clearing). Returns an
    empty set on any error so callers fail open.
    """
    result = _jsonrpc(
        method="Playlist.GetItems",
        params={
            "playlistid": PLAYLIST_MUSIC,
            "properties": ["file"],
        },
    )
    if not result or "result" not in result:
        return set()
    items = result.get("result", {}).get("items") or []
    ids = set()
    for item in items:
        file_url = item.get("file") or ""
        track_id, _ = parse_track_url(file_url)
        if track_id:
            ids.add(track_id)
    return ids
