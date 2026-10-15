# -*- coding: utf-8 -*-
"""Start a clicked song from a Spotify song list inside the music queue.

plugin.py runs this module before the plugin's heavy imports, so a click costs
one interpreter start. Kodi's native click would rebuild the whole queue, which
costs seconds for a large list.

If the song list built the current queue, the song plays from that queue. Any
other click plays the song at once and then loads the list's other songs around
the song, so the queue matches the list.
"""

import json
from urllib.parse import parse_qs, urlencode

import xbmc
import xbmcgui

ACTION = "play_in_queue"
# A listing with rows=stream returns the playable stream rows from FIRST_PARAM up to LAST_PARAM.
STREAM_ROWS_PARAM = "rows"
STREAM_ROWS_VALUE = "stream"
FIRST_PARAM = "first"
LAST_PARAM = "last"
PROP_LISTING = "Spotify.PlayQueue.Listing"

_HOME_WINDOW_ID = 10000
_PLUGIN_URL = "plugin://plugin.audio.spotifykodiconnect/"
_START_TIMEOUT_MS = 15000


def _rpc(method, params):
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    if "error" in response:
        raise RuntimeError(f"{method} failed: {response['error']}")
    return response["result"]


def _queue_index(playlist, track_id, position):
    marker = f"/track/{track_id}/"
    size = playlist.size()
    if 0 <= position < size and marker in playlist[position].getPath():
        return position
    # Shuffle and queue edits move songs away from the listing position.
    return next((index for index in range(size) if marker in playlist[index].getPath()), -1)


def _stream_rows(listing, first, last=None):
    rows = {STREAM_ROWS_PARAM: STREAM_ROWS_VALUE, FIRST_PARAM: first}
    if last is not None:
        rows[LAST_PARAM] = last
    return {"directory": f"{_PLUGIN_URL}?{listing}&{urlencode(rows)}"}


def _clicked_song(play_queue, query, track_id, duration_ms, owner):
    """Build the clicked song with the tags of the focused list row."""
    url = play_queue.build_stream_url(track_id, duration_ms)
    focused = xbmc.getInfoLabel("ListItem.FileNameAndPath") == f"{_PLUGIN_URL}{query}"

    def label(name):
        return xbmc.getInfoLabel(f"ListItem.{name}") if focused else ""

    li = xbmcgui.ListItem(label("Label"), path=url, offscreen=True)
    tag = li.getMusicInfoTag()
    tag.setTitle(label("Title"))
    tag.setArtist(label("Artist"))
    tag.setAlbum(label("Album"))
    tag.setDuration(max(1, -(-duration_ms // 1000)))
    tag.setMediaType("song")
    tag.setURL(url)
    li.setArt({"thumb": label("Art(thumb)"), "fanart": label("Art(fanart)")})
    play_queue.bind_stream_item(li, track_id, owner)
    return url, li


def _play_and_load_list(query, listing, track_id, duration_ms, position, playlist):
    import play_queue

    # The session holds autoplay until the list is in the queue.
    owner = play_queue.start_session(1)
    url, li = _clicked_song(play_queue, query, track_id, duration_ms, owner)
    with play_queue.session_guard(owner) as window:
        if window is None:
            return
        playlist.clear()
        playlist.add(url, li)
    player = xbmc.Player()
    player.play(playlist, startpos=0)

    # Load the other songs after the audio starts, so the load does not delay the song.
    monitor = xbmc.Monitor()
    waited = 0
    while not (player.isPlayingAudio() and player.getTime() > 0):
        if waited >= _START_TIMEOUT_MS or monitor.waitForAbort(0.1):
            play_queue.mark_original_complete(owner)
            return
        waited += 100
    with play_queue.session_guard(owner) as window:
        if window is None:
            return
        if position:
            # The player keeps the playing song current when rows go in before the song.
            _rpc(
                "Playlist.Insert",
                {
                    "playlistid": xbmc.PLAYLIST_MUSIC,
                    "position": 0,
                    "item": _stream_rows(listing, 0, position),
                },
            )
        _rpc(
            "Playlist.Add",
            {"playlistid": xbmc.PLAYLIST_MUSIC, "item": _stream_rows(listing, position + 1)},
        )
        window.setProperty(PROP_LISTING, listing)
    play_queue.mark_original_complete(owner)


def run(query):
    params = {key: values[0] for key, values in parse_qs(query.lstrip("?")).items()}
    listing = params.get("list", "")
    track_id = params.get("track", "")
    try:
        position = int(params.get("position", ""))
        duration_ms = int(params.get("ms", ""))
    except ValueError:
        position = duration_ms = -1
    if not listing or not track_id or position < 0 or duration_ms < 0:
        xbmc.log(f"plugin.audio.spotifykodiconnect: invalid song click '{query}'.", xbmc.LOGWARNING)
        return

    playlist = xbmc.PlayList(xbmc.PLAYLIST_MUSIC)
    if xbmcgui.Window(_HOME_WINDOW_ID).getProperty(PROP_LISTING) == listing:
        index = _queue_index(playlist, track_id, position)
        if index >= 0:
            xbmc.Player().play(playlist, startpos=index)
            return
    _play_and_load_list(query, listing, track_id, duration_ms, position, playlist)
