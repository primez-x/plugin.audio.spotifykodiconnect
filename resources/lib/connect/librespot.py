# -*- coding: utf-8 -*-
import os
import shlex
import socket
import subprocess
import sys
import threading

import xbmcaddon
import xbmcvfs

import utils
from utils import ADDON_ID, log_msg, log_exception


def _get_librespot_binary_path(addon_path):
    """Resolve path to librespot binary: bundled addon/bin/ (platform-specific name) or same path for error message."""
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
    return candidates[0]


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
                if self._librespot.returncode <= 0:
                    self._retries = 0
                else:
                    self._retries += 1
                    if self._retries < self._max_retries:
                        log_msg('connect: librespot failed %s/%s' % (self._retries, self._max_retries))
                    else:
                        log_msg('connect: librespot failed too many times', utils.LOGINFO)
                        break
            self._librespot = None
        log_msg('connect: librespot thread stopped')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()
