"""
OAuth2 PKCE authentication for Spotify.

Handles initial login (browser-based), persistent token storage, and
silent token refresh — all without needing the spotty binary.

Flow:
  1. start_auth()        → returns Spotify authorize URL (opens in browser)
  2. handle_callback()   → exchanges auth code for access + refresh tokens
  3. get_valid_token()    → returns a valid access_token, refreshing if needed
  4. (spotty_auth.py)     → passes access_token to spotty --access-token --get-token
                            so spotty caches credentials for audio streaming
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Dict, Optional

import requests
from xbmc import LOGDEBUG, LOGERROR, LOGWARNING

from utils import ADDON_DATA_PATH, log_msg

# Spotify app client_id for OAuth2 PKCE authentication.
# PKCE does NOT require a client_secret.
CLIENT_ID = "375e225fdcb34ad4b0aaf94ecfb7f41b"

# The redirect_uri MUST be registered in the Spotify Developer Dashboard
# for the above client_id.  We use the addon's existing HTTP server port.
# Use 127.0.0.1 (not localhost) — Spotify's dashboard rejects http://localhost
# as insecure, and 127.0.0.1 avoids IPv6 dual-stack resolution issues.
# For remote/headless auth, the /auth/start route overrides this with the
# device's LAN IP so phones on the same network can receive the callback.
REDIRECT_URI = "http://127.0.0.1:52309/auth/callback"

# Scopes — superset of what the addon needs for the Web API + streaming.
SCOPE = " ".join([
    "streaming",
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-modify-playback-state",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-follow-modify",
    "user-follow-read",
    "user-library-read",
    "user-library-modify",
    "user-read-private",
    "user-read-email",
    "user-top-read",
])

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/authorize"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"

# GitHub Pages relay for remote auth (headless devices)
# Register this in Spotify Dashboard as a redirect URI
RELAY_REDIRECT_URI = "https://primez-x.github.io/plugin.audio.spotifykodiconnect/auth/callback"

# Persistent token file — survives Kodi restarts.
_TOKEN_FILE = os.path.join(ADDON_DATA_PATH, "spotify_oauth_token.json")
# Temporary PKCE state file — written by the plugin process, read by the service
# process when the HTTP callback arrives.  Deleted after successful exchange.
_PKCE_STATE_FILE = os.path.join(ADDON_DATA_PATH, "spotify_oauth_pkce.json")


class SpotifyOAuth:
    """Manages Spotify OAuth2 PKCE tokens."""

    def __init__(self):
        self._code_verifier: Optional[str] = None
        self._state: Optional[str] = None
        # Serialises concurrent refresh attempts so only one hits the API.
        self._refresh_lock = threading.Lock()

    # ------------------------------------------------------------------
    #  Persistent token storage
    # ------------------------------------------------------------------

    @staticmethod
    def get_stored_token() -> Optional[Dict]:
        """Load stored token from disk.  Returns None if missing/corrupt."""
        try:
            if os.path.exists(_TOKEN_FILE):
                with open(_TOKEN_FILE, "r") as fh:
                    data = json.load(fh)
                if "access_token" in data and "refresh_token" in data:
                    return data
        except Exception as exc:
            log_msg(f"Failed to read OAuth token file: {exc}", LOGWARNING)
        return None

    @staticmethod
    def _save_token(token_info: Dict) -> None:
        os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
        with open(_TOKEN_FILE, "w") as fh:
            json.dump(token_info, fh, indent=2)

    @staticmethod
    def delete_stored_token() -> None:
        """Remove the persistent token file (e.g. for logout / re-auth)."""
        try:
            if os.path.exists(_TOKEN_FILE):
                os.remove(_TOKEN_FILE)
        except OSError:
            pass

    # ------------------------------------------------------------------
    #  Step 1 — build authorization URL
    # ------------------------------------------------------------------

    def start_auth(self, redirect_uri_override: str = None, kodi_host: str = None) -> str:
        """Generate PKCE parameters and return the Spotify authorization URL.

        The caller should open this URL in a web browser.

        Args:
            redirect_uri_override: If set, use this redirect_uri instead of the
                default 127.0.0.1 one.  Deprecated; use kodi_host instead.
            kodi_host: If set, use GitHub Pages relay as redirect_uri and embed
                this Kodi host in the state parameter so the relay page knows
                where to redirect the code back to.

        The state, code_verifier, and redirect_uri are written to disk so that
        the service process (which runs the HTTP callback server in a different
        Python interpreter) can read them when Spotify redirects back.
        """
        # code_verifier: 43-128 chars, base64-url-safe
        self._code_verifier = secrets.token_urlsafe(96)[:128]
        self._state = secrets.token_urlsafe(16)

        # Determine redirect_uri: relay if kodi_host provided, else local or override
        if kodi_host:
            actual_redirect = RELAY_REDIRECT_URI
            # Embed Kodi host in state so relay page can redirect back
            state_for_spotify = f"{self._state}|{kodi_host}"
        else:
            actual_redirect = redirect_uri_override or REDIRECT_URI
            state_for_spotify = self._state

        # Persist to disk so the service process's handle_callback() can use them.
        os.makedirs(os.path.dirname(_PKCE_STATE_FILE), exist_ok=True)
        with open(_PKCE_STATE_FILE, "w") as fh:
            json.dump({
                "state": self._state,  # Always save just the CSRF token
                "code_verifier": self._code_verifier,
                "redirect_uri": actual_redirect,
            }, fh)

        digest = hashlib.sha256(self._code_verifier.encode("ascii")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

        params = {
            "client_id": CLIENT_ID,
            "response_type": "code",
            "redirect_uri": actual_redirect,
            "code_challenge_method": "S256",
            "code_challenge": code_challenge,
            "scope": SCOPE,
            "state": state_for_spotify,
        }
        from urllib.parse import urlencode
        url = f"{SPOTIFY_AUTH_URL}?{urlencode(params)}"
        log_msg(f"OAuth PKCE: authorization URL generated (redirect={actual_redirect}).", LOGDEBUG)
        return url

    # ------------------------------------------------------------------
    #  Step 2 — exchange authorization code for tokens
    # ------------------------------------------------------------------

    def handle_callback(self, code: str, state: str) -> Optional[Dict]:
        """Exchange the authorization code for access + refresh tokens.

        Called by the /auth/callback HTTP route (runs in the service process).
        Reads the PKCE state+verifier from disk, written by the plugin process
        that called start_auth() — they run in separate Python interpreters.

        Returns token_info dict on success, None on error.
        """
        # Load PKCE state from disk (written by the plugin process's start_auth
        # or the /auth/start HTTP route).
        try:
            with open(_PKCE_STATE_FILE, "r") as fh:
                pkce = json.load(fh)
            saved_state = pkce["state"]
            code_verifier = pkce["code_verifier"]
            # redirect_uri must match exactly what was sent in the auth request.
            redirect_uri = pkce.get("redirect_uri", REDIRECT_URI)
        except Exception as exc:
            log_msg(f"OAuth callback: failed to load PKCE state file: {exc}", LOGERROR)
            return None

        # State might contain pipe-separated kodi_host from relay; extract just CSRF
        received_csrf = state.split('|')[0] if '|' in state else state
        if received_csrf != saved_state:
            log_msg(
                f"OAuth callback: state mismatch (got {received_csrf!r}, expected {saved_state!r}).",
                LOGERROR,
            )
            return None

        data = {
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }

        try:
            resp = requests.post(SPOTIFY_TOKEN_URL, data=data, timeout=15)
        except Exception as exc:
            log_msg(f"OAuth token exchange request failed: {exc}", LOGERROR)
            return None

        if resp.status_code != 200:
            log_msg(
                f"OAuth token exchange failed: HTTP {resp.status_code} — {resp.text}",
                LOGERROR,
            )
            return None

        token_info = resp.json()
        token_info["expires_at"] = int(time.time()) + token_info.get("expires_in", 3600)
        self._save_token(token_info)

        # Clean up the one-time PKCE state file.
        try:
            os.remove(_PKCE_STATE_FILE)
        except OSError:
            pass

        log_msg("OAuth PKCE: tokens obtained and stored successfully.")
        return token_info

    # ------------------------------------------------------------------
    #  Token refresh
    # ------------------------------------------------------------------

    def refresh_access_token(self, force: bool = False) -> Optional[Dict]:
        """Refresh the access token using the stored refresh_token.

        Thread-safe: concurrent callers share one refresh request.
        Returns updated token_info, or None on failure.
        """
        with self._refresh_lock:
            stored = self.get_stored_token()
            if not stored or "refresh_token" not in stored:
                return None

            # Unless forced, skip if the token is still valid (>60 s remaining).
            if not force:
                expires_at = stored.get("expires_at", 0)
                if expires_at - 60 > int(time.time()):
                    return stored  # still valid

            data = {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": stored["refresh_token"],
            }

            try:
                resp = requests.post(SPOTIFY_TOKEN_URL, data=data, timeout=15)
            except Exception as exc:
                log_msg(f"OAuth token refresh request failed: {exc}", LOGERROR)
                return None

            if resp.status_code != 200:
                log_msg(
                    f"OAuth token refresh failed: HTTP {resp.status_code} — {resp.text}",
                    LOGERROR,
                )
                return None

            token_info = resp.json()
            token_info["expires_at"] = int(time.time()) + token_info.get("expires_in", 3600)
            # Spotify may or may not return a new refresh_token.
            if "refresh_token" not in token_info:
                token_info["refresh_token"] = stored["refresh_token"]
            self._save_token(token_info)

            log_msg(
                f"OAuth token refreshed.  Expires at"
                f" {time.strftime('%H:%M:%S', time.localtime(token_info['expires_at']))}.",
                LOGDEBUG,
            )
            return token_info

    # ------------------------------------------------------------------
    #  Convenience
    # ------------------------------------------------------------------

    def get_valid_token(self) -> Optional[str]:
        """Return a valid access_token string, refreshing silently if needed.

        Returns None when no stored token exists (user hasn't authenticated).
        """
        stored = self.get_stored_token()
        if not stored:
            return None

        if stored.get("expires_at", 0) - 60 <= int(time.time()):
            stored = self.refresh_access_token(force=True)
            if not stored:
                return None

        return stored.get("access_token")

    @staticmethod
    def is_authenticated() -> bool:
        """Check whether a persistent OAuth token file exists."""
        return os.path.exists(_TOKEN_FILE)


# Module-level singleton so all imports share the same PKCE state.
oauth = SpotifyOAuth()
