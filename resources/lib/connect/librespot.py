# -*- coding: utf-8 -*-
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading

import xbmcaddon
import xbmcvfs

import utils
from utils import ADDON_ID, log_msg, log_exception


def _ensure_librespot_via_opkg():
    """On Linux (e.g. CoreELEC), try to install librespot via opkg so the system binary is available."""
    if sys.platform != 'linux':
        return False
    opkg = shutil.which('opkg') or ('/usr/bin/opkg' if os.path.isfile('/usr/bin/opkg') else None)
    if not opkg:
        log_msg('connect: opkg not found, cannot install librespot')
        return False
    try:
        log_msg('connect: running opkg update then opkg install librespot')
        subprocess.run([opkg, 'update'], check=False, timeout=60,
                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        r = subprocess.run([opkg, 'install', 'librespot', '--force-overwrite'],
                          capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            log_msg('connect: opkg install librespot succeeded', utils.LOGINFO)
            return True
        log_msg('connect: opkg install librespot failed: %s' % (r.stderr or r.stdout or r.returncode))
    except subprocess.TimeoutExpired:
        log_msg('connect: opkg install librespot timed out')
    except Exception as e:
        log_exception(e, 'connect: opkg install librespot')
    return False


def _get_librespot_binary_path(addon_path):
    """
    Resolve path to librespot binary, in order:
    1) Bundled addon bin/ (librespot or librespot.exe on Windows)
    2) System binary (PATH, or after opkg install on Linux/CoreELEC)
    """
    addon_path = xbmcvfs.translatePath(addon_path)
    bin_dir = os.path.join(addon_path, 'bin')
    if sys.platform == 'win32':
        candidates = [
            os.path.join(bin_dir, 'librespot.exe'),
            os.path.join(bin_dir, 'librespot'),
        ]
    else:
        candidates = [os.path.join(bin_dir, 'librespot')]
    for path in candidates:
        if os.path.isfile(path):
            if sys.platform != 'win32':
                try:
                    os.chmod(path, 0o755)
                except Exception:
                    pass
            return path
    # No bundled binary: try system librespot (e.g. from opkg on CoreELEC)
    system_path = shutil.which('librespot')
    if system_path:
        log_msg('connect: using system librespot: %s' % system_path)
        return system_path
    if sys.platform.startswith('linux'):
        _ensure_librespot_via_opkg()
        system_path = shutil.which('librespot')
        if system_path:
            log_msg('connect: using system librespot after opkg: %s' % system_path)
            return system_path
    fallback = candidates[0]
    if not os.path.isfile(fallback):
        raise FileNotFoundError(
            'LibreSpot binary not found. Add addon/bin/librespot (or librespot.exe on Windows), '
            'or on Linux/CoreELEC install via: opkg install librespot'
        )
    return fallback


class Librespot:
    def __init__(self,
                 bitrate='320',
                 device_type='tv',
                 max_retries='5',
                 name='Spotify Kodi Connect@{}',
                 options='',
                 onevent_path='',
                 **kwargs):
        name = name.format(socket.gethostname())
        addon = xbmcaddon.Addon(ADDON_ID)
        addon_path = addon.getAddonInfo('path')
        librespot_path = _get_librespot_binary_path(addon_path)
        self.command = [
            librespot_path,
            '--bitrate', f'{bitrate}',
            '--device-type', f'{device_type}',
            '--disable-audio-cache',
            '--disable-credential-cache',
            '--initial-volume', '100',
            '--name', f'{name}',
            '--quiet',
        ]
        if onevent_path and os.path.isfile(onevent_path):
            self.command += ['--onevent', onevent_path]
        self.command += shlex.split(options or '')
        log_msg('connect: librespot command %s' % self.command)
        self.file = ''
        self._is_started = threading.Event()
        self._is_stopped = threading.Event()
        self._librespot = None
        self._max_retries = int(max_retries or 5)
        self._retries = 0
        self._thread = threading.Thread()

    def get_player(self, **kwargs):
        from . import internal_player
        return internal_player.Player(**kwargs)

    def restart(self):
        if self._thread.is_alive() and self._librespot:
            try:
                self._librespot.terminate()
            except Exception:
                pass
        else:
            self.start()

    def start(self):
        if not self._thread.is_alive() and self._retries < self._max_retries:
            self._thread = threading.Thread(daemon=True, target=self._run)
            self._thread.start()
            self._is_started.wait(2)

    def stop(self):
        if self._thread.is_alive():
            self._is_stopped.set()
            if self._librespot:
                try:
                    self._librespot.terminate()
                except Exception:
                    pass
            self._thread.join(timeout=5)

    def start_sink(self):
        pass

    def stop_sink(self):
        pass

    def _run(self):
        log_msg('connect: librespot thread started')
        self._is_started.clear()
        self._is_stopped.clear()
        while not self._is_stopped.is_set():
            try:
                with subprocess.Popen(
                    self.command,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=os.environ.copy(),
                ) as self._librespot:
                    self._is_started.set()
                    for line in self._librespot.stderr or []:
                        log_msg('librespot: %s' % line.rstrip())
            except Exception as e:
                log_exception(e, 'connect: librespot Popen')
            self.stop_sink()
            if self._librespot and self._librespot.returncode is not None:
                ret = self._librespot.returncode
                if ret <= 0:
                    self._retries = 0
                else:
                    self._retries += 1
                    log_msg('connect: librespot exited with code %s (see "librespot:" lines above for reason)' % ret)
                    if self._retries < self._max_retries:
                        log_msg('connect: librespot failed %s/%s, will retry' % (self._retries, self._max_retries))
                    else:
                        log_msg('connect: librespot failed too many times', utils.LOGINFO)
                        break
            self._librespot = None
        log_msg('connect: librespot thread stopped')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()
