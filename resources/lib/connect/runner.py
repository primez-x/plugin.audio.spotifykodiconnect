# -*- coding: utf-8 -*-
"""
Starts LibreSpot (Connect receiver) and the onevent player loop.
Uses bundled librespot binary from addon bin/ if present (bin/librespot or bin/librespot.exe on Windows).
"""
import os

import xbmcaddon
import xbmcvfs

import utils
from utils import ADDON_ID, log_msg, log_exception


def run(stop_event=None):
    """
    Run Connect receiver (LibreSpot) until stop_event is set.
    stop_event: threading.Event; when set, we stop librespot and return.
    """
    addon = xbmcaddon.Addon(ADDON_ID)
    if addon.getSetting('connect_receiver') != 'true':
        return
    addon_path = xbmcvfs.translatePath(addon.getAddonInfo('path'))
    onevent_path = os.path.join(addon_path, 'bin', 'onevent.py')
    if not os.path.isfile(onevent_path):
        log_msg('connect: onevent.py not found at %s' % onevent_path)
        return
    device_name = addon.getSetting('connect_device_name') or 'Spotify Kodi Connect@{}'
    options = addon.getSetting('connect_options') or ''
    dnd_kodi = addon.getSetting('connect_dnd_kodi') == 'true'
    alsa_device = addon.getSetting('connect_alsa_device') or 'hw:0,0'
    try:
        from . import librespot_alsa
        librespot_class = librespot_alsa.Librespot
        kwargs = dict(onevent_path=onevent_path, name=device_name, options=options, alsa_device=alsa_device)
        with librespot_class(**kwargs) as librespot:
            with librespot.get_player(librespot=librespot, dnd_kodi=dnd_kodi) as player:
                log_msg('connect: receiver started (backend=alsa)')
                if stop_event:
                    while not stop_event.wait(timeout=2.0):
                        pass
                else:
                    import time
                    while True:
                        time.sleep(60)
    except FileNotFoundError as e:
        log_msg('connect: librespot binary not found. On CoreELEC/Linux the addon tries opkg install librespot automatically; if that failed, run "opkg install librespot" over SSH. Otherwise add addon/bin/librespot (or librespot.exe on Windows). %s' % e, utils.LOGINFO)
    except Exception as e:
        log_exception(e, 'connect: runner')
