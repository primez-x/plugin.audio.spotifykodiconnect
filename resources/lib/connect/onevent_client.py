# -*- coding: utf-8 -*-
"""In-process onevent: same protocol as bin/onevent.py (UDP port 36964)."""
import json
import socket

ADDRESS = ('127.0.0.1', 36964)
BUFFER_SIZE = 1024


def send_event(event):
    data = json.dumps(event).encode()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(data, ADDRESS)


def receive_event():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(None)
        sock.bind(ADDRESS)
        while True:
            data, addr = sock.recvfrom(BUFFER_SIZE)
            event = json.loads(data.decode())
            if not event:
                break
            yield event


ARG_ALBUM = 'album'
ARG_ARTIST = 'artist'
ARG_ART = 'art'
ARG_TITLE = 'title'

KEY_ALBUM = 'ALBUM'
KEY_ARTISTS = 'ARTISTS'
KEY_COVERS = 'COVERS'
KEY_ITEM_TYPE = 'ITEM_TYPE'
KEY_NAME = 'NAME'
KEY_PLAYER_EVENT = 'PLAYER_EVENT'
KEY_SHOW_NAME = 'SHOW_NAME'

PLAYER_EVENT_STOPPED = 'stopped'
PLAYER_EVENT_TRACK_CHANGED = 'track_changed'

ITEM_TYPE_EPISODE = 'Episode'
ITEM_TYPE_TRACK = 'Track'
