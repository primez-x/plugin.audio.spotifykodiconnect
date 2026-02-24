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
    backend = addon.getSetting('connect_backend') or 'pulseaudio_rtp'
    device_name = addon.getSetting('connect_device_name') or 'Spotify Kodi Connect@{}'
    options = addon.getSetting('connect_options') or ''
    dnd_kodi = addon.getSetting('connect_dnd_kodi') == 'true'
    alsa_device = addon.getSetting('connect_alsa_device') or 'hw:2,0'
    try:
        if backend == 'alsa':
            from . import librespot_alsa
            librespot_class = librespot_alsa.Librespot
            kwargs = dict(onevent_path=onevent_path, name=device_name, options=options, alsa_device=alsa_device)
        else:
            from . import librespot_pulseaudio_rtp
            librespot_class = librespot_pulseaudio_rtp.Librespot
            kwargs = dict(onevent_path=onevent_path, name=device_name, options=options,
                         pa_rtp_device='spotify_kodi_connect', pa_rtp_port='24643')
        with librespot_class(**kwargs) as librespot:
            with librespot.get_player(librespot=librespot, dnd_kodi=dnd_kodi) as player:
                log_msg('connect: receiver started (backend=%s)' % backend)
                if stop_event:
                    while not stop_event.wait(timeout=2.0):
                        pass
                else:
                    import time
                    while True:
                        time.sleep(60)
    except FileNotFoundError as e:
        log_msg('connect: librespot binary not found. Add addon/bin/librespot (or librespot.exe on Windows), or install librespot on the system (e.g. CoreELEC: opkg install librespot). %s' % e, utils.LOGINFO)
    except Exception as e:
        log_exception(e, 'connect: runner')
