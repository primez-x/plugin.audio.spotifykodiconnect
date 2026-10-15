#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
plugin.audio.spotify
Unofficial Spotify client for Kodi
"""

import sys
from time import perf_counter

STARTED_AT = perf_counter()

# A song click (handle -1) skips the plugin's heavy imports. PlayMedia resolves through PluginContent.
if __name__ == "__main__" and sys.argv[1] == "-1" and "action=play_in_queue" in sys.argv[2]:
    from resources.lib.play_in_queue import run

    run(sys.argv[2])
else:
    from resources.lib.plugin_content import PluginContent, join_background_threads  # noqa: E402

    if __name__ == "__main__":
        PluginContent(started_at=STARTED_AT)
        join_background_threads()
