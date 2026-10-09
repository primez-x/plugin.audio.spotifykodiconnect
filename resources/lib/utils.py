import os
import platform
import signal
import sys
import time
import unicodedata
import traceback
from typing import Any, Dict, List, Tuple, Union

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
from xbmc import LOGDEBUG, LOGINFO, LOGERROR

DEBUG = False
DEBUG_SETTING_ID = "debug_logging"
_DEBUG_SETTING_VALUE = None
_DEBUG_SETTING_CHECKED_AT = 0.0
_DEBUG_SETTING_TTL = 30.0

ADDON_ID = "plugin.audio.spotifykodiconnect"
ADDON_DATA_PATH = xbmcvfs.translatePath(f"special://profile/addon_data/{ADDON_ID}")
ADDON_WINDOW_ID = 10000
# Different port so this addon can run alongside plugin.audio.spotify if needed
PROXY_PORT = 52309
# Use 127.0.0.1 instead of "localhost" to avoid IPv6/IPv4 dual-stack resolution
# delays.  WSGIServer defaults to AF_INET (IPv4), so the server always binds to
# 127.0.0.1.  If URLs say "localhost", Kodi's libcurl may try ::1 (IPv6) first,
# wait ~14 s for the timeout, then fall back to IPv4.  On some Linux devices
# (CoreELEC / LibreELEC) localhost resolution can fail entirely.
PROXY_HOST = "127.0.0.1"

KODI_PROPERTY_SPOTIFY_AUTH_TOKEN = "spotifykodiconnect-auth-token"
KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT = "spotifykodiconnect-auth-token-expires-at"
# Epoch seconds at which an in-progress zeroconf pairing was started by the
# plugin. While set (and not stale) the service must not restore
# credentials.json from the .bak the pairing flow just moved aside.
KODI_PROPERTY_ZEROCONF_PAIRING_SINCE = "Spotify.ZeroconfPairingSince"
ZEROCONF_PAIRING_STALE_SECS = 600
# Epoch seconds until which Spotify told us (429 Retry-After) to back off.
KODI_PROPERTY_RATE_LIMITED_UNTIL = "Spotify.RateLimitedUntil"


def _debug_logging_enabled() -> bool:
    global _DEBUG_SETTING_VALUE, _DEBUG_SETTING_CHECKED_AT
    if DEBUG:
        return True

    now = time.monotonic()
    if _DEBUG_SETTING_VALUE is not None and now - _DEBUG_SETTING_CHECKED_AT < _DEBUG_SETTING_TTL:
        return _DEBUG_SETTING_VALUE

    try:
        addon = xbmcaddon.Addon(id=ADDON_ID)
        _DEBUG_SETTING_VALUE = (addon.getSetting(DEBUG_SETTING_ID) or "").strip().lower() == "true"
    except Exception:
        _DEBUG_SETTING_VALUE = False
    _DEBUG_SETTING_CHECKED_AT = now
    return _DEBUG_SETTING_VALUE


def _get_caller_name(depth: int = 2) -> str:
    try:
        frame = sys._getframe(depth)
        return get_formatted_caller_name(frame.f_code.co_filename, frame.f_code.co_name)
    except Exception:
        return ""


def log_msg(msg: str, loglevel: int = LOGDEBUG, caller_name: str = "") -> None:
    if loglevel == LOGDEBUG and not _debug_logging_enabled():
        return
    if not caller_name:
        caller_name = _get_caller_name()

    xbmc.log(f"{ADDON_ID}:{caller_name}: {msg}", level=loglevel)


def log_exception(exc: Exception, exception_details: str) -> None:
    the_caller_name = _get_caller_name()
    log_msg(
        " ".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        loglevel=LOGERROR,
        caller_name=the_caller_name,
    )
    log_msg(f"Exception --> {exception_details}.", loglevel=LOGERROR, caller_name=the_caller_name)


def get_formatted_caller_name(filename: str, function_name: str) -> str:
    return f"{os.path.splitext(os.path.basename(filename))[0]}:{function_name}"


def get_time_str(raw_time: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(raw_time)))


def get_username() -> str:
    """
    Best-effort Spotify username for display/logging.

    Older versions stored a dedicated "username" setting; newer flows may not.
    Treat a missing value as empty instead of raising so callers can still
    show generic messages without failing.
    """
    addon = xbmcaddon.Addon(id=ADDON_ID)
    return addon.getSetting("username") or ""


def kill_this_plugin() -> None:
    sys.exit(1)


def kill_process_by_pid(pid: int) -> None:
    try:
        if platform.system() == "Windows":
            os.kill(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def bytes_to_megabytes(byts: int) -> float:
    return (byts / 1024.0) / 1024.0


def get_chunks(data, chunk_size: int):
    return [data[x : x + chunk_size] for x in range(0, len(data), chunk_size)]


def try_encode(text, encoding="utf-8"):
    try:
        return text.encode(encoding, "ignore")
    except UnicodeEncodeError:
        return text


def try_decode(text, encoding="utf-8"):
    try:
        return text.decode(encoding, "ignore")
    except UnicodeDecodeError:
        return text


def normalize_string(text):
    text = text.replace(":", "")
    text = text.replace("/", "-")
    text = text.replace("\\", "-")
    text = text.replace("<", "")
    text = text.replace(">", "")
    text = text.replace("*", "")
    text = text.replace("?", "")
    text = text.replace("|", "")
    text = text.replace("(", "")
    text = text.replace(")", "")
    text = text.replace('"', "")
    text = text.strip()
    text = text.rstrip(".")
    text = unicodedata.normalize("NFKD", try_decode(text))

    return text


def cache_auth_token(auth_token: str) -> None:
    cache_value_in_kodi(KODI_PROPERTY_SPOTIFY_AUTH_TOKEN, auth_token)


def get_cached_auth_token() -> str:
    return get_cached_value_from_kodi(KODI_PROPERTY_SPOTIFY_AUTH_TOKEN)


def cache_auth_token_expires_at(auth_token: str) -> None:
    cache_value_in_kodi(KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT, auth_token)


def get_cached_auth_token_expires_at() -> str:
    return get_cached_value_from_kodi(KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT)


def peek_cached_auth_token_expires_at() -> str:
    """Non-blocking read of the cached token expiry ("" when unset)."""
    return xbmcgui.Window(ADDON_WINDOW_ID).getProperty(KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT) or ""


def cached_auth_token_is_unexpired(now: float = None) -> bool:
    """True when a cached token exists and its expiry is still in the future."""
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    if not win.getProperty(KODI_PROPERTY_SPOTIFY_AUTH_TOKEN):
        return False
    try:
        expires_at = int(float(win.getProperty(KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT) or 0))
    except (TypeError, ValueError):
        return False
    return expires_at > (time.time() if now is None else now)


def mark_zeroconf_pairing(active: bool) -> None:
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    if active:
        win.setProperty(KODI_PROPERTY_ZEROCONF_PAIRING_SINCE, str(int(time.time())))
    else:
        win.clearProperty(KODI_PROPERTY_ZEROCONF_PAIRING_SINCE)


def zeroconf_pairing_in_progress(now: float = None) -> bool:
    value = xbmcgui.Window(ADDON_WINDOW_ID).getProperty(KODI_PROPERTY_ZEROCONF_PAIRING_SINCE)
    if not value:
        return False
    try:
        started = float(value)
    except (TypeError, ValueError):
        return False
    return ((time.time() if now is None else now) - started) < ZEROCONF_PAIRING_STALE_SECS


def set_rate_limited_until(until_epoch: float) -> None:
    """Record a long Spotify 429 back-off so background work can skip itself."""
    try:
        win = xbmcgui.Window(ADDON_WINDOW_ID)
        current = float(win.getProperty(KODI_PROPERTY_RATE_LIMITED_UNTIL) or 0)
        if until_epoch > current:
            win.setProperty(KODI_PROPERTY_RATE_LIMITED_UNTIL, str(int(until_epoch)))
    except Exception:
        pass


def is_rate_limited(now: float = None) -> bool:
    """True while a recorded Spotify 429 back-off window is still in the future."""
    try:
        value = xbmcgui.Window(ADDON_WINDOW_ID).getProperty(KODI_PROPERTY_RATE_LIMITED_UNTIL)
        if not value:
            return False
        return float(value) > (time.time() if now is None else now)
    except Exception:
        return False


def install_spotipy_rate_limit_hook(spotipy_module) -> None:
    """Route long-429 notifications from the vendored spotipy Retry into Kodi."""
    try:
        util_module = getattr(spotipy_module, "util", None)
        if util_module is not None and hasattr(util_module, "on_long_rate_limit"):
            util_module.on_long_rate_limit = set_rate_limited_until
    except Exception:
        pass


def cache_value_in_kodi(kodi_property_id: str, value: Any):
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    win.setProperty(kodi_property_id, value)


def get_cached_value_from_kodi(kodi_property_id: str, wait_ms: int = 100) -> Any:
    win = xbmcgui.Window(ADDON_WINDOW_ID)
    for _ in range(50):
        value = win.getProperty(kodi_property_id)
        if value:
            return value
        xbmc.sleep(wait_ms)
    return None


def get_user_playlists(
    spotipy, limit: int = 50, offset: int = 0
) -> Tuple[List[Dict[str, Any]], List[str]]:
    userid = spotipy.me()["id"]
    playlists = spotipy.user_playlists(userid, limit=limit, offset=offset)

    own_playlists = []
    own_playlist_names = []
    for playlist in playlists["items"]:
        if playlist["owner"]["id"] == userid:
            own_playlists.append(playlist)
            own_playlist_names.append(playlist["name"])

    return own_playlists, own_playlist_names


def get_user_playlist_id(spotipy, playlist_name: str) -> Union[str, None]:
    offset = 0
    while True:
        own_playlists, own_playlist_names = get_user_playlists(spotipy, limit=50, offset=offset)
        if len(own_playlists) == 0:
            break
        for playlist in own_playlists:
            if playlist_name == playlist["name"]:
                return playlist["id"]
        offset += 50

    return None
