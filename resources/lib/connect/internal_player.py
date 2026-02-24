# -*- coding: utf-8 -*-
"""Kodi player for LibreSpot: plays RTP stream and updates now-playing info."""
import xbmc
import xbmcgui

from . import player
import utils


class Player(player.Player):
    def __init__(self, codec='pcm_sb16be', **kwargs):
        super().__init__(**kwargs)
        self._list_item = xbmcgui.ListItem(path=self.librespot.file)
        self._list_item.getVideoInfoTag().addAudioStream(xbmc.AudioStreamDetail(2, codec))
        self._music_info_tag = self._list_item.getMusicInfoTag()

    def onLibrespotTrackChanged(self, album='', art='', artist='', title=''):
        self._list_item.setArt({'fanart': art or '', 'thumb': art or ''})
        self._music_info_tag.setAlbum(album)
        self._music_info_tag.setArtist(artist)
        self._music_info_tag.setTitle(title)
        if self.isPlaying() and self.getPlayingFile() == self.librespot.file:
            self.updateInfoTag(self._list_item)
        else:
            self.stop()
            self.librespot.start_sink()
            self.play(self.librespot.file, listitem=self._list_item)

    def onLibrespotStopped(self):
        self.librespot.stop_sink()
        if self.isPlaying() and self.getPlayingFile() == self.librespot.file:
            self.last_file = None
            self.stop()
