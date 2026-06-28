# -*- coding: utf-8 -*-
"""
Plugin-owned play queue coordination between the plugin and service processes.

SpotifyKodiConnect runs in two Python processes that cannot share in-memory
state:

- **Plugin process** — ``PluginContent.play_playlist`` populates Kodi's music
  playlist from a Spotify playlist, fetching additional pages in the
  background after playback has already started.
- **Service process** — ``MainService`` observes Kodi playback events and
  decides when to fire autoplay recommendations.

They communicate through window properties on Kodi's home window
(``xbmcgui.Window(ADDON_WINDOW_ID)``). This module is the protocol surface.

Protocol
========

1. Plugin calls :func:`start_session` at the start of ``play_playlist`` with
   the playlist's total track count. This generates a fresh session id,
   marks the original playlist as "loading" (unless it fit in one page),
   and clears any stale autoplay claim.
2. Plugin's paging thread calls :func:`report_loaded` after each page so the
   service has visibility into progress, and
   :func:`mark_original_complete` once every original track has been pushed
   into Kodi's playlist.
3. Service calls :func:`should_fire_autoplay` from its playback-started
   callback. Returns ``True`` only when the original playlist is fully
   loaded AND no prior caller has claimed autoplay. Returning ``True``
   atomically marks autoplay as fetched.
4. Either process may call :func:`clear` to tear down session state — the
   service does this on playback stop/end/error, the plugin does it at the
   start of a new session.

Why window properties and not just JSON-RPC playlist inspection? Kodi's
``Playlist.GetItems`` tells the service what is *currently* in the Kodi
playlist but not whether more is on the way. Without this protocol, the
service cannot distinguish "playlist genuinely exhausted" from "plugin
still paging in the rest" — which is exactly the daylist bug where autoplay
fired mid-load and destroyed the queue.
"""

from __future__ import absolute_import, unicode_literals

import time

import xbmcgui

from utils import ADDON_WINDOW_ID, log_msg
from xbmc import LOGDEBUG

# All keys are namespaced under "Spotify.PlayQueue." so a future migration
# or cleanup pass can enumerate them with a single prefix scan.
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


def _window():
    return xbmcgui.Window(ADDON_WINDOW_ID)


def clear() -> None:
    """Tear down all play-queue session state.

    Safe to call when no session is active. Idempotent. The service calls
    this on playback stop/end/error so a stale "loading" session from a
    previous play doesn't suppress autoplay for the next one.
    """
    win = _window()
    for key in _PLAY_QUEUE_PROPS:
        win.clearProperty(key)


def start_session(original_total: int) -> str:
    """Begin a new play-queue session from the plugin process.

    Generates a fresh session id, marks the original playlist as "loading"
    (unless empty), and clears any stale autoplay claim from a prior
    session. Returns the new session id for log correlation.
    """
    win = _window()
    session_id = f"{int(time.time() * 1000)}-{original_total}"
    win.setProperty(PROP_SESSION_ID, session_id)
    win.setProperty(PROP_ORIGINAL_TOTAL, str(original_total))
    win.setProperty(PROP_ORIGINAL_LOADED, "0")
    # An empty playlist is trivially complete; non-empty starts "loading"
    # and is flipped by mark_original_complete() when paging finishes.
    complete = "true" if original_total <= 0 else "false"
    win.setProperty(PROP_ORIGINAL_COMPLETE, complete)
    win.setProperty(PROP_AUTOPLAY_FETCHED, "false")
    log_msg(
        f"PlayQueue: session {session_id} started"
        f" (total={original_total}, complete={complete}).",
        LOGDEBUG,
    )
    return session_id


def report_loaded(count: int) -> None:
    """Plugin updates how many original tracks have been pushed to Kodi's playlist.

    Lets the service (and logs) see paging progress, useful for diagnosing
    stuck sessions where the paging thread died before completion.
    """
    _window().setProperty(PROP_ORIGINAL_LOADED, str(count))


def mark_original_complete() -> None:
    """Plugin paging thread finished loading every original track."""
    win = _window()
    loaded = win.getProperty(PROP_ORIGINAL_LOADED)
    total = win.getProperty(PROP_ORIGINAL_TOTAL)
    log_msg(
        f"PlayQueue: original playlist complete (loaded={loaded}, total={total}).",
        LOGDEBUG,
    )
    win.setProperty(PROP_ORIGINAL_COMPLETE, "true")


def is_original_complete() -> bool:
    """Service-side check: has the plugin finished loading the original?"""
    return _window().getProperty(PROP_ORIGINAL_COMPLETE) == "true"


def is_loading() -> bool:
    """True iff an active session is still paging its original playlist in.

    This is the critical signal the service uses to suppress premature
    autoplay. Returns ``False`` when no session is active so single-track
    playback (which never calls :func:`start_session`) still fires autoplay
    under legacy semantics.
    """
    win = _window()
    if not win.getProperty(PROP_SESSION_ID):
        return False
    return win.getProperty(PROP_ORIGINAL_COMPLETE) != "true"


def should_fire_autoplay() -> bool:
    """Service asks: "Kodi reports no next item — may I fire autoplay?"

    Returns ``True`` only when firing is safe. Returning ``True`` atomically
    claims the autoplay slot for the active session, so the caller must act
    on a ``True`` return.

    Rules:

    - If an active session is still loading its original playlist, the
      "no next item" signal is almost certainly a paging gap, not true
      exhaustion. Returns ``False``.
    - If session-scoped autoplay has already fired, returns ``False``.
    - If no session is active (e.g. single-track play from search), returns
      ``True`` without writing state — no session means nothing to gate on.
    """
    win = _window()
    session_active = bool(win.getProperty(PROP_SESSION_ID))
    if session_active and win.getProperty(PROP_ORIGINAL_COMPLETE) != "true":
        # Active session, original still paging in. Hold off.
        return False
    if session_active and win.getProperty(PROP_AUTOPLAY_FETCHED) == "true":
        # Session already claimed its autoplay slot.
        return False
    if session_active:
        # Claim the slot so subsequent onPlayBackStarted callbacks don't
        # double-fire. The service dispatches these serially, but defending
        # against re-entry keeps the invariant local to this module.
        win.setProperty(PROP_AUTOPLAY_FETCHED, "true")
    return True


def session_id() -> str:
    """Return the current session id, or empty string if no session is active."""
    return _window().getProperty(PROP_SESSION_ID)
