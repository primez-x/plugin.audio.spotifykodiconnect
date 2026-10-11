#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
plugin.audio.spotify
Unofficial Spotify client for Kodi
"""

from time import perf_counter

STARTED_AT = perf_counter()

from resources.lib.plugin_content import PluginContent  # noqa: E402

if __name__ == "__main__":
    PluginContent(started_at=STARTED_AT)
