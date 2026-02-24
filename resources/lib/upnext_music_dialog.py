# -*- coding: utf-8 -*-
"""
Up Next Music dialog for Spotify Kodi Connect.
Shows a non-blocking "Next track" overlay with music-specific layout (title,
artist, art). The overlay is informational only: there are no on-screen
Play/Close buttons; users can dismiss it via back/close, and playback always
continues normally.
"""
from __future__ import absolute_import, unicode_literals

from datetime import datetime, timedelta

import xbmc
import xbmcaddon
import xbmcvfs
from xbmcgui import WindowXMLDialog

from utils import ADDON_ID

ACTION_NAV_BACK = 92


def addon_path():
    """Return the addon root path (for WindowXMLDialog)."""
    raw = xbmcaddon.Addon(id=ADDON_ID).getAddonInfo("path")
    path = xbmcvfs.translatePath(raw)
    if isinstance(path, bytes):
        path = path.decode("utf-8")
    return path


def _localize(string_id):
    return xbmcaddon.Addon(id=ADDON_ID).getLocalizedString(string_id)


class UpNextMusicDialog(WindowXMLDialog):
    """Dialog showing next track (title, artist, art) as a non-blocking overlay."""

    def __init__(self, xml_filename, path, skin="default", res="1080i"):
        self._item = None
        self._dismissed = False
        self._progress_step_size = 0
        self._current_progress_percent = 100
        self._progress_control = None
        self.action_exitkeys_id = [10, 13]
        WindowXMLDialog.__init__(self, xml_filename, path, skin, res)

    def set_item(self, item):
        """Set the next track item (dict with title, artist, art.thumb, etc.)."""
        self._item = item or {}

    def set_progress_step_size(self, step):
        self._progress_step_size = step

    def onInit(self):
        self._set_info()
        self._prepare_progress_control()

    def _set_info(self):
        item = self._item or {}
        art = item.get("art") or {}
        self.setProperty("thumb", art.get("thumb", ""))
        self.setProperty("landscape", art.get("tvshow.landscape", "") or art.get("thumb", ""))
        self.setProperty("fanart", art.get("tvshow.fanart", "") or art.get("fanart", ""))
        self.setProperty("title", item.get("title", ""))
        self.setProperty("artist", item.get("artist", ""))
        self.setProperty("runtime", str(item.get("runtime", 0)))

    def _prepare_progress_control(self):
        try:
            self._progress_control = self.getControl(3014)
            self._progress_control.setPercent(self._current_progress_percent)
        except RuntimeError:
            self._progress_control = None

    def update_progress_control(self, remaining=None, runtime=None):
        self._current_progress_percent = max(
            0,
            self._current_progress_percent - self._progress_step_size,
        )
        if self._progress_control:
            try:
                self._progress_control.setPercent(self._current_progress_percent)
            except RuntimeError:
                pass
        if remaining is not None:
            self.setProperty("remaining", "%02d" % remaining)
        if runtime is not None:
            end_time = datetime.now() + timedelta(seconds=runtime)
            self.setProperty("endtime", end_time.strftime("%H:%M"))

    def is_cancel(self):
        return self._dismissed

    def is_play_next(self):
        # Overlay is informational only; never drives playback decisions.
        return False

    def onClick(self, controlId):
        # No clickable controls are defined in the skin; keep for safety.
        self._dismissed = True
        self.close()

    def onAction(self, action):
        if action == ACTION_NAV_BACK:
            self._dismissed = True
            self.close()
