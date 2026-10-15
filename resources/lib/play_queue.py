# -*- coding: utf-8 -*-
"""Coordinate plugin loaders and service callbacks across Python processes.

Kodi home-window properties remain the public queue state. A separate SQLite
transaction lock protects ownership checks and queue mutations across processes.
HTTP requests and cache operations run outside the queue transaction.
"""

from __future__ import absolute_import, unicode_literals

import os
import sqlite3
import uuid
from contextlib import contextmanager

import xbmcgui

from utils import ADDON_DATA_PATH, ADDON_WINDOW_ID, PROXY_HOST, PROXY_PORT, log_msg
from xbmc import LOGDEBUG

ITEM_OWNER_PROPERTY = "_spotify_queue_owner"

PROP_SESSION_ID = "Spotify.PlayQueue.SessionId"
PROP_ORIGINAL_TOTAL = "Spotify.PlayQueue.OriginalTotal"
PROP_ORIGINAL_LOADED = "Spotify.PlayQueue.OriginalLoaded"
PROP_ORIGINAL_COMPLETE = "Spotify.PlayQueue.OriginalComplete"
PROP_AUTOPLAY_FETCHED = "Spotify.PlayQueue.AutoplayFetched"

_PLAY_QUEUE_PROPS = (
    PROP_SESSION_ID,
    PROP_ORIGINAL_TOTAL,
    PROP_ORIGINAL_LOADED,
    PROP_ORIGINAL_COMPLETE,
    PROP_AUTOPLAY_FETCHED,
)


def build_stream_url(track_id: str, duration_ms: int) -> str:
    """Preserve millisecond WAV length without a file extension that triggers TagLib."""
    duration_ms = max(1000, int(duration_ms or 0))
    return f"http://{PROXY_HOST}:{PROXY_PORT}/track/{track_id}/{duration_ms}ms"


def bind_stream_item(listitem, track_id: str, owner: str = "") -> None:
    """Bind a fresh playback item to the item's track, a queue owner and the stream type.

    Kodi probes an http item without a mime type with a blocking request on the
    thread that hands the item over. The local proxy that answers the probe
    runs in Kodi's Python runtime, so a probe from a Python thread stalls until
    curl times out (20 s per probe).
    """
    listitem.setProperty("spotifytrackid", track_id)
    listitem.setProperty(ITEM_OWNER_PROPERTY, f"{owner}:{track_id}")
    listitem.setMimeType("audio/x-wav")
    listitem.setContentLookup(False)


def _window():
    return xbmcgui.Window(ADDON_WINDOW_ID)


@contextmanager
def session_guard(owner=None):
    """Hold the cross-process queue lock; reject a superseded owner.

    The dedicated database stores no state and shares no cache locks.
    Closing the connection releases the transaction, including after an error.
    Callers must prepare tracks and fetch pages before entering the guard.
    """
    os.makedirs(ADDON_DATA_PATH, exist_ok=True)
    connection = sqlite3.connect(
        os.path.join(ADDON_DATA_PATH, "play_queue.db"),
        timeout=30,
        isolation_level=None,
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        win = _window()
        yield (
            win
            if owner is None or win.getProperty(PROP_SESSION_ID) == owner
            else None
        )
    finally:
        connection.close()


def clear(owner=None) -> bool:
    """Clear a matching owner's queue under the lock; report whether cleanup ran."""
    with session_guard(owner) as win:
        if win is None:
            return False
        for key in _PLAY_QUEUE_PROPS:
            win.clearProperty(key)
    return True


def start_session(original_total: int) -> str:
    """Create a unique loader owner before the plugin changes the Kodi queue."""
    token = uuid.uuid4().hex
    complete = "true" if original_total <= 0 else "false"
    with session_guard() as win:
        win.setProperty(PROP_SESSION_ID, token)
        win.setProperty(PROP_ORIGINAL_TOTAL, str(original_total))
        win.setProperty(PROP_ORIGINAL_LOADED, "0")
        win.setProperty(PROP_ORIGINAL_COMPLETE, complete)
        win.setProperty(PROP_AUTOPLAY_FETCHED, "false")
    log_msg(
        f"PlayQueue: session {token} started"
        f" (total={original_total}, complete={complete}).",
        LOGDEBUG,
    )
    return token


def report_loaded(count: int, owner: str) -> None:
    """Report appended original tracks only for the active loader owner."""
    with session_guard(owner) as win:
        if win is not None:
            win.setProperty(PROP_ORIGINAL_LOADED, str(count))


def mark_original_complete(owner: str) -> None:
    """Finish the active original loader without changing a replacement session."""
    with session_guard(owner) as win:
        if win is None:
            return
        loaded = win.getProperty(PROP_ORIGINAL_LOADED)
        total = win.getProperty(PROP_ORIGINAL_TOTAL)
        win.setProperty(PROP_ORIGINAL_COMPLETE, "true")
    log_msg(
        f"PlayQueue: original playlist complete (loaded={loaded}, total={total}).",
        LOGDEBUG,
    )


def is_original_complete() -> bool:
    """Return whether the active original playlist loader finished."""
    with session_guard() as win:
        return win.getProperty(PROP_ORIGINAL_COMPLETE) == "true"


def is_loading() -> bool:
    """Return whether an active original playlist loader remains incomplete."""
    with session_guard() as win:
        return bool(win.getProperty(PROP_SESSION_ID)) and (
            win.getProperty(PROP_ORIGINAL_COMPLETE) != "true"
        )


def should_fire_autoplay() -> bool:
    """Claim autoplay once per complete session; allow single-track playback."""
    with session_guard() as win:
        session_active = bool(win.getProperty(PROP_SESSION_ID))
        if session_active and win.getProperty(PROP_ORIGINAL_COMPLETE) != "true":
            return False
        if session_active and win.getProperty(PROP_AUTOPLAY_FETCHED) == "true":
            return False
        if session_active:
            win.setProperty(PROP_AUTOPLAY_FETCHED, "true")
        return True


def session_id() -> str:
    """Return the active loader owner, or an empty string without a session."""
    with session_guard() as win:
        return win.getProperty(PROP_SESSION_ID)
