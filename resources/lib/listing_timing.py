"""Opt-in timing log for plugin listings (widgets and browsing).

With Settings > Debug > "Log listing times" on, every plugin invocation writes
one LOGINFO line, e.g.

    plugin.audio.spotifykodiconnect:...: Listing action=browse_playlist
    playlistid=37i9...: 640 ms, 50 items (import 120, auth 2, listing 500,
    other 18)

"import" counts from the start of plugin.py until PluginContent starts, not
Kodi's Python interpreter start-up before it. Off by default: a listing then
only reads the setting from the add-on object it already holds.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

SETTING_ID = "listing_timing_log"
# Plugin parameters that identify a listing in the log line
LABEL_PARAMS = (
    "action",
    "playlistid",
    "ownerid",
    "albumid",
    "artistid",
    "trackid",
    "offset",
    "applyfilter",
)


class ListingTimer:
    def __init__(self, started_at: Optional[float] = None, clock=time.perf_counter):
        self._clock = clock
        self.started_at = clock() if started_at is None else started_at
        self._last = self.started_at
        self.phases: List[Tuple[str, float]] = []
        self.items: Optional[int] = None

    def mark(self, phase: str) -> None:
        """Close the phase that ran since the previous mark (or the start)."""
        now = self._clock()
        self.phases.append((phase, now - self._last))
        self._last = now

    def add_items(self, count: int) -> None:
        self.items = (self.items or 0) + count

    def line(self, listing: str) -> str:
        total = self._clock() - self.started_at
        summed: Dict[str, float] = {}
        for phase, duration in self.phases:
            summed[phase] = summed.get(phase, 0.0) + duration
        other = total - sum(summed.values())
        if other >= 0.0005:
            summed["other"] = other
        text = f"Listing {listing or '(main menu)'}: {round(total * 1000)} ms"
        if self.items is not None:
            text += f", {self.items} items"
        if summed:
            parts = ", ".join(f"{phase} {round(duration * 1000)}" for phase, duration in summed.items())
            text += f" ({parts})"
        return text


def label(params: Dict[str, Any]) -> str:
    """Listing label from urllib.parse.parse_qs() style params."""
    parts = []
    for key in LABEL_PARAMS:
        value = params.get(key)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def count_directory_items(xbmcplugin_module, timer: ListingTimer) -> None:
    """Count the items this process adds (only installed while timing is on)."""
    add_items = xbmcplugin_module.addDirectoryItems
    add_item = xbmcplugin_module.addDirectoryItem

    def counted_add_items(*args, **kwargs):
        items = kwargs.get("items", args[1] if len(args) > 1 else ())
        timer.add_items(len(items))
        return add_items(*args, **kwargs)

    def counted_add_item(*args, **kwargs):
        timer.add_items(1)
        return add_item(*args, **kwargs)

    xbmcplugin_module.addDirectoryItems = counted_add_items
    xbmcplugin_module.addDirectoryItem = counted_add_item
