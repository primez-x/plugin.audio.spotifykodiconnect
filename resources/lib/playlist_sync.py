# -*- coding: utf-8 -*-
"""
Sync Spotify playlists to Kodi as real .m3u playlists (like PlexKodiConnect).
Playlists appear in Music → Playlists as native Kodi playlists, not addon folders.
"""
import os
import re
from typing import Any, Dict, List

import xbmcvfs

import utils
from utils import ADDON_ID, PROXY_PORT, log_msg, log_exception

# Kodi music playlists folder (same as PlexKodiConnect)
KODI_MUSIC_PLAYLISTS_PATH = xbmcvfs.translatePath("special://profile/playlists/music/")
# Prefix so our playlists are identifiable and don't clash with user's .m3u files
SPOTIFY_PLAYLIST_PREFIX = "Spotify - "
# Invalid filename chars (Windows + Unix)
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_FILENAME_LEN = 200


def _safe_playlist_filename(name: str) -> str:
    """Sanitize playlist name for use as filename (no path)."""
    s = INVALID_FILENAME_CHARS.sub("_", name).strip() or "Playlist"
    if len(s) > MAX_FILENAME_LEN - len(SPOTIFY_PLAYLIST_PREFIX) - 4:
        s = s[: MAX_FILENAME_LEN - len(SPOTIFY_PLAYLIST_PREFIX) - 4]
    return SPOTIFY_PLAYLIST_PREFIX + s + ".m3u"


def _track_line(track: Dict[str, Any], proxy_port: int) -> str:
    """Build one #EXTINF line + URL for a track. Returns '' if track not playable."""
    if not track or not track.get("id"):
        return ""
    duration_ms = track.get("duration_ms") or 0
    duration_sec = int(duration_ms / 1000)
    artists = track.get("artists") or []
    artist_str = ", ".join(a.get("name") or "" for a in artists).strip() or "Unknown"
    title = (track.get("name") or "").strip() or "Unknown"
    label = f"{artist_str} - {title}"
    # Escape commas in EXTINF (they separate duration and display string)
    label_esc = label.replace("\\", "\\\\").replace(",", "\\,")
    url = f"http://localhost:{proxy_port}/track/{track['id']}/{duration_sec}"
    return f"#EXTINF:{duration_sec},{label_esc}\n{url}\n"


def _build_m3u(playlist_name: str, tracks: List[Dict[str, Any]], proxy_port: int) -> str:
    """Build full M3U content for a playlist."""
    lines = ["#EXTM3U", f"#EXTINF:0,{playlist_name}", ""]
    for item in tracks:
        track = item.get("track") if isinstance(item.get("track"), dict) else item
        line = _track_line(track, proxy_port)
        if line:
            lines.append(line.rstrip())
    return "\n".join(lines) + "\n"


def _ensure_playlist_dir() -> bool:
    """Ensure Kodi music playlists folder exists. Returns True on success."""
    if xbmcvfs.exists(KODI_MUSIC_PLAYLISTS_PATH):
        return True
    try:
        return xbmcvfs.mkdirs(KODI_MUSIC_PLAYLISTS_PATH)
    except Exception as e:
        log_exception(e, "playlist_sync: could not create playlists folder")
        return False


def _list_our_playlist_files() -> List[str]:
    """List full paths of .m3u files we created (Spotify - *.m3u)."""
    result = []
    try:
        _dirs, files = xbmcvfs.listdir(KODI_MUSIC_PLAYLISTS_PATH)
        for f in files:
            if f.startswith(SPOTIFY_PLAYLIST_PREFIX) and f.endswith(".m3u"):
                result.append(os.path.join(KODI_MUSIC_PLAYLISTS_PATH, f))
    except Exception as e:
        log_exception(e, "playlist_sync: listdir")
    return result


def sync_playlists_to_kodi(spotipy_client, proxy_port: int = None) -> None:
    """
    Fetch user's Spotify playlists and write each as a .m3u file in Kodi's
    Music playlists folder. Playlists then appear in Music → Playlists as
    real Kodi playlists (not addon folders).
    """
    proxy_port = proxy_port or PROXY_PORT
    if not _ensure_playlist_dir():
        return
    try:
        try:
            market = (spotipy_client.me() or {}).get("country") or "US"
        except Exception:
            market = "US"
        playlists = spotipy_client.current_user_playlists(limit=50, offset=0)
        current_names = set()
        while playlists:
            for pl in playlists.get("items") or []:
                name = (pl.get("name") or "").strip() or "Playlist"
                pl_id = pl.get("id")
                if not pl_id:
                    continue
                try:
                    count = 0
                    tracks = []
                    while True:
                        resp = spotipy_client.playlist_items(
                            pl_id,
                            market=market,
                            fields="items(track(id,name,duration_ms,artists))",
                            limit=100,
                            offset=count,
                        )
                        items = (resp.get("items") or []) if isinstance(resp, dict) else []
                        if not items:
                            break
                        tracks.extend(items)
                        if len(items) < 100:
                            break
                        count += 100
                    content = _build_m3u(name, tracks, proxy_port)
                    filename = _safe_playlist_filename(name)
                    path = os.path.join(KODI_MUSIC_PLAYLISTS_PATH, filename)
                    f = xbmcvfs.File(path, "w")
                    try:
                        f.write(content)
                    finally:
                        f.close()
                    current_names.add(filename)
                    log_msg(f"Synced playlist to Kodi: {filename}")
                except Exception as e:
                    log_exception(e, f"playlist_sync: playlist '{name}'")
            next_page = playlists.get("next")
            if not next_page:
                break
            playlists = spotipy_client.next(playlists) or {}
        # Remove .m3u files we previously wrote that are no longer in Spotify
        for existing in _list_our_playlist_files():
            basename = os.path.basename(existing)
            if basename not in current_names:
                try:
                    xbmcvfs.delete(existing)
                    log_msg(f"Removed orphan Kodi playlist: {basename}")
                except Exception as e:
                    log_exception(e, f"playlist_sync: delete {basename}")
    except Exception as e:
        log_exception(e, "playlist_sync: sync_playlists_to_kodi")
