# -*- coding: utf-8 -*-
"""
Optional metadata fetcher for artist biography and album description.
Supports Last.fm (API key required) and MusicBrainz + Wikipedia (no key).
"""
from typing import Any, Dict, Optional
import re
from urllib.parse import unquote

try:
    import requests
except ImportError:
    requests = None

LASTFM_API_BASE = "https://ws.audioscrobbler.com/2.0/"
MUSICBRAINZ_API_BASE = "https://musicbrainz.org/ws/2/"
WIKIPEDIA_API_BASE = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "SpotifyKodiConnect/1.0 (https://github.com/; music metadata)"

# Provider identifiers (must match addon setting values)
PROVIDER_OFF = "0"
PROVIDER_LASTFM = "lastfm"
PROVIDER_MUSICBRAINZ = "musicbrainz"


def _mb_request(path: str, params: Optional[Dict[str, str]] = None) -> Optional[Dict]:
    """MusicBrainz GET; no API key, user-agent required."""
    if not requests:
        return None
    url = MUSICBRAINZ_API_BASE.rstrip("/") + "/" + path.lstrip("/")
    params = params or {}
    params.setdefault("fmt", "json")
    try:
        r = requests.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _wikipedia_extract(page_title: str) -> str:
    """
    Fetch intro extract from Wikipedia by page title (URL-encoded).
    Uses the same MediaWiki API as script.wikipedia's WikipediaAPI.get_extract()
    (action=query, prop=extracts, exintro, explaintext). We do not depend on
    script.wikipedia to keep this addon lightweight and allow headless use.
    """
    if not requests or not page_title:
        return ""
    try:
        r = requests.get(
            WIKIPEDIA_API_BASE,
            params={
                "action": "query",
                "prop": "extracts",
                "exintro": "1",
                "explaintext": "1",
                "titles": page_title,
                "format": "json",
                "redirects": "1",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        pages = (data or {}).get("query", {}).get("pages", {})
        for pid, page in pages.items():
            if pid != "-1" and page.get("extract"):
                return (page.get("extract") or "").strip()
    except Exception:
        pass
    return ""


def fetch_artist_bio_musicbrainz(artist_name: str) -> str:
    """
    Fetch artist biography via MusicBrainz artist search + Wikipedia relation.
    No API key. Returns empty string on failure.
    """
    if not artist_name or not requests:
        return ""
    # 1) Search artist by name
    data = _mb_request("artist/", {"query": artist_name, "limit": "1"})
    artists = (data or {}).get("artists")
    if not artists:
        return ""
    mbid = artists[0].get("id")
    if not mbid:
        return ""
    # 2) Get artist with url-relations to find Wikipedia
    artist = _mb_request(f"artist/{mbid}", {"inc": "url-relations"})
    if not artist:
        return ""
    relations = artist.get("relations") or []
    wiki_url = None
    for rel in relations:
        if (rel.get("type") or "").lower() == "wikipedia":
            url_obj = rel.get("url")
            if isinstance(url_obj, dict):
                wiki_url = url_obj.get("resource")
            elif isinstance(url_obj, str):
                wiki_url = url_obj
            if wiki_url:
                break
    if not wiki_url:
        return ""
    # 3) Parse Wikipedia URL for page title (e.g. .../wiki/Page_Title)
    match = re.search(r"wiki/(?:.*/)?([^?#]+)$", wiki_url)
    if not match:
        return ""
    page_title = unquote(match.group(1).replace("_", " "))
    return _wikipedia_extract(page_title)


def fetch_artist_bio_lastfm(artist_name: str, api_key: str) -> str:
    """Fetch artist biography from Last.fm artist.getInfo. Returns empty on failure."""
    bio, _ = fetch_artist_info_lastfm(artist_name, api_key)
    return bio


def fetch_artist_info_lastfm(artist_name: str, api_key: str) -> tuple:
    """
    Fetch artist biography and image URL from Last.fm artist.getInfo (one request).
    Returns (bio_str, image_url_str). Either may be empty.
    """
    if not artist_name or not api_key or not requests:
        return ("", "")
    try:
        r = requests.get(
            LASTFM_API_BASE,
            params={
                "method": "artist.getInfo",
                "artist": artist_name,
                "api_key": api_key,
                "format": "json",
                "autocorrect": "1",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        artist = (data or {}).get("artist") or {}
        bio = (artist.get("bio") or {}).get("content") or ""
        bio = bio.strip()
        if bio:
            read_more = "Read more on Last.fm"
            if bio.endswith(read_more):
                bio = bio[: -len(read_more)].strip()
        image_url = ""
        for img in (artist.get("image") or []):
            if isinstance(img, dict) and img.get("size") in ("extralarge", "large", "medium"):
                url = (img.get("#text") or "").strip()
                if url:
                    image_url = url
                    if img.get("size") == "extralarge":
                        break
        return (bio, image_url)
    except Exception:
        pass
    return ("", "")


def fetch_album_description(artist_name: str, album_name: str, api_key: str) -> str:
    """
    Fetch album description/wiki from Last.fm album.getInfo.
    Returns empty string on failure or if no wiki.
    """
    if not artist_name or not album_name or not api_key or not requests:
        return ""
    try:
        r = requests.get(
            LASTFM_API_BASE,
            params={
                "method": "album.getInfo",
                "artist": artist_name,
                "album": album_name,
                "api_key": api_key,
                "format": "json",
                "autocorrect": "1",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        album = (data or {}).get("album") or {}
        wiki = album.get("wiki") or {}
        content = (wiki.get("content") or "").strip()
        if content:
            read_more = "Read more on Last.fm"
            if content.endswith(read_more):
                content = content[: -len(read_more)].strip()
            return content
    except Exception:
        pass
    return ""
