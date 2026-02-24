# -*- coding: utf-8 -*-
import threading

import xbmc

import utils
from utils import log_msg

# Onevent module: same package uses bin/onevent.py logic in Python
try:
    from . import onevent_client as onevent
except Exception:
    onevent = None


class Player(xbmc.Player):
    def __init__(self, dnd_kodi='false', librespot=None, **kwargs):
        super().__init__()
        self._dnd_kodi = (dnd_kodi == 'true')
        self._thread = None
        self.last_file = None
        self.librespot = librespot
        if librespot and onevent:
            if not (self._dnd_kodi and self.isPlaying()):
                self.librespot.start()
            self._thread = threading.Thread(daemon=True, target=self._run)
            self._thread.start()

    def onAVStarted(self):
        if not self.librespot:
            return
        file = self.getPlayingFile()
        if file != self.librespot.file:
            if self._dnd_kodi:
                self.librespot.stop()
            elif self.last_file == self.librespot.file:
                self.librespot.restart()
        self.last_file = file

    def onLibrespotStopped(self):
        pass

    def onLibrespotTrackChanged(self, album='', art='', artist='', title=''):
        pass

    def onPlayBackEnded(self):
        if not self.librespot:
            return
        if self.last_file == self.librespot.file:
            self.librespot.restart()
        else:
            self.librespot.start()
        self.last_file = None

    def onPlayBackError(self):
        self.onPlayBackEnded()

    def onPlayBackStopped(self):
        self.onPlayBackEnded()

    def stop(self):
        xbmc.executebuiltin('PlayerControl(Stop)')

    def _run(self):
        if not onevent:
            return
        log_msg('connect: onevent dispatcher started')
        try:
            for event in onevent.receive_event():
                try:
                    player_event = event.pop(onevent.KEY_PLAYER_EVENT, None)
                    if player_event == onevent.PLAYER_EVENT_STOPPED:
                        self.onLibrespotStopped()
                    elif player_event == onevent.PLAYER_EVENT_TRACK_CHANGED:
                        self.onLibrespotTrackChanged(**event)
                except Exception as e:
                    utils.log_exception(e, 'connect: onevent dispatch')
        except Exception as e:
            utils.log_exception(e, 'connect: onevent receive')
        log_msg('connect: onevent dispatcher stopped')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if onevent:
            try:
                onevent.send_event({})
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self.onLibrespotStopped()
