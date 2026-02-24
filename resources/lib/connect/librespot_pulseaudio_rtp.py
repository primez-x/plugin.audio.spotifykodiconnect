# -*- coding: utf-8 -*-
"""PulseAudio RTP backend: librespot outputs to a sink, Kodi plays rtp:// stream."""
import socket
import subprocess

from . import librespot
import utils
from utils import log_msg


class Librespot(librespot.Librespot):
    def __init__(self,
                 codec='pcm_sb16be',
                 pa_rtp_address='127.0.0.1',
                 pa_rtp_device='spotify_kodi_connect',
                 pa_rtp_port='24643',
                 **kwargs):
        log_msg('connect: pulseaudio backend started')
        self._sap_server = None
        self._modules = []
        self._sink_name = str(pa_rtp_device or 'spotify_kodi_connect')
        try:
            sap_cmd = ['nc', '-l', '-u', '-s', pa_rtp_address, '-p', '9876']
            self._sap_server = subprocess.Popen(
                sap_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            log_msg('connect: sap server started')
        except Exception as e:
            utils.log_exception(e, 'connect: sap server')
        pa_rtp_port = str(pa_rtp_port or '24643')
        try:
            modules = [
                ['module-null-sink', 'sink_name=%s' % self._sink_name],
                [
                    'module-rtp-send',
                    'destination_ip=%s' % pa_rtp_address,
                    'inhibit_auto_suspend=always',
                    'port=%s' % pa_rtp_port,
                    'source=%s.monitor' % self._sink_name,
                ],
            ]
            self._modules = []
            for m in modules:
                out = self._pactl('load-module', *m)
                if out:
                    self._modules.append(out)
            self.stop_sink()
            log_msg('connect: pulseaudio modules loaded: %s' % self._modules)
        except Exception as e:
            utils.log_exception(e, 'connect: pulseaudio modules')
        super().__init__(**kwargs)
        self.command += [
            '--backend', 'pulseaudio',
            '--device', self._sink_name,
        ]
        self.file = 'rtp://%s:%s' % (pa_rtp_address, pa_rtp_port)

    def start_sink(self):
        try:
            self._pactl('suspend-sink', self._sink_name, '0')
        except Exception:
            pass

    def stop_sink(self):
        try:
            self._pactl('suspend-sink', self._sink_name, '1')
        except Exception:
            pass

    def _pactl(self, command, *args):
        out = subprocess.run(
            ['pactl', command, *args],
            stdout=subprocess.PIPE,
            text=True,
        ).stdout.rstrip()
        log_msg('connect: pactl %s %s -> %s' % (command, args, out))
        return out

    def __exit__(self, *args):
        super().__exit__(*args)
        for module in reversed(self._modules):
            if module:
                try:
                    self._pactl('unload-module', module)
                except Exception:
                    pass
        log_msg('connect: pulseaudio backend stopped')
        if self._sap_server and self._sap_server.poll() is None:
            try:
                self._sap_server.terminate()
                self._sap_server.wait(timeout=2)
            except Exception:
                pass
        log_msg('connect: sap server stopped')
