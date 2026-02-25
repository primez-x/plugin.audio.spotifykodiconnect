# -*- coding: utf-8 -*-
"""
Next Track widget for Spotify Kodi Connect.
Sets Home window (10000) properties that the skin reads to render a
non-blocking "Next Track" overlay. No modal dialog is opened.
"""
from __future__ import absolute_import, unicode_literals

from datetime import datetime, timedelta

import xbmcgui

PROP_PREFIX = "NextTrack."
_HOME = xbmcgui.Window(10000)


def _set_prop(key, value):
    _HOME.setProperty(key, str(value) if value is not None else "")


def _clear_prop(key):
    _HOME.clearProperty(key)


class NextTrackDialog:
    """Property-based next-track widget (non-blocking, no dialog window)."""

    def __init__(self, *_args, **_kwargs):
        self._item = None
        self._dismissed = False
        self._progress_step_size = 0
        self._current_progress_percent = 100
        self._initial_remaining_sec = None

    def set_item(self, item):
        self._item = item or {}

    def set_progress_step_size(self, step):
        self._progress_step_size = step

    def set_initial_remaining(self, remaining_sec):
        self._initial_remaining_sec = max(0, int(remaining_sec))

    def show(self):
        self._set_info()
        _set_prop(PROP_PREFIX + "progress", "100")
        _set_prop("service.nexttrack.dialog", "true")

    def close(self):
        _clear_prop("service.nexttrack.dialog")
        for key in ("title", "artist", "album", "thumb", "fanart", "landscape",
                     "clearart", "clearlogo", "poster", "year", "rating",
                     "playcount", "runtime", "remaining", "endtime", "progress"):
            _clear_prop(PROP_PREFIX + key)

    def _set_info(self):
        item = self._item or {}
        art = item.get("art") or {}
        _set_prop(PROP_PREFIX + "thumb", art.get("thumb", ""))
        _set_prop(PROP_PREFIX + "landscape", art.get("landscape", "") or art.get("thumb", ""))
        _set_prop(PROP_PREFIX + "fanart", art.get("fanart", ""))
        _set_prop(PROP_PREFIX + "title", item.get("title", ""))
        artist = item.get("artist", "")
        if isinstance(artist, list):
            artist = artist[0] if artist else ""
        _set_prop(PROP_PREFIX + "artist", artist)
        _set_prop(PROP_PREFIX + "runtime", item.get("runtime", 0))
        if self._initial_remaining_sec is not None:
            _set_prop(PROP_PREFIX + "remaining", "%02d" % self._initial_remaining_sec)

    def update_progress_control(self, remaining=None, runtime=None):
        self._current_progress_percent = max(
            0,
            self._current_progress_percent - self._progress_step_size,
        )
        _set_prop(PROP_PREFIX + "progress", str(int(self._current_progress_percent)))
        if remaining is not None:
            _set_prop(PROP_PREFIX + "remaining", "%02d" % remaining)
        if runtime is not None:
            end_time = datetime.now() + timedelta(seconds=runtime)
            _set_prop(PROP_PREFIX + "endtime", end_time.strftime("%H:%M"))

    def is_cancel(self):
        return self._dismissed

    def is_play_next(self):
        return False
