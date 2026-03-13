"""
Spotify authentication — OAuth2 PKCE primary, spotty-based fallback.

Token retrieval priority:
  1. OAuth refresh_token (fast HTTP POST, no subprocess)
  2. spotty --save-token   (requires cached credentials.json from a prior
     OAuth or zeroconf login)

After a fresh OAuth login the access_token is also passed to spotty via
``--access-token <token> --get-token`` so spotty caches credentials
locally for audio streaming.
"""

import json
import os
import subprocess
import time
from typing import Dict, Optional, Union

import xbmcaddon
from xbmc import LOGDEBUG, LOGERROR, LOGWARNING

import utils
from spotify_oauth import SpotifyOAuth, oauth, CLIENT_ID as OAUTH_CLIENT_ID, SCOPE
from spotty import Spotty, SPOTTY_CACHE_DIR_NAME, SPOTTY_CREDENTIALS_FILENAME
from string_ids import AUTHENTICATE_FAILED_STR_ID, AUTHENTICATION_PROGRAM_FAILED_STR_ID
from utils import log_msg, log_exception, ADDON_ID

ZEROCONF_PORT = 10001

SPOTTY_SCOPE = SCOPE.split()


class SpottyAuth:
    def __init__(self, spotty: Spotty):
        self.__spotty = spotty

    # ------------------------------------------------------------------
    #  OAuth credential caching — pass access_token to spotty so it
    #  stores a credentials.json for future audio streaming sessions.
    # ------------------------------------------------------------------

    def cache_spotty_credentials(self, access_token: str) -> bool:
        """Run spotty with --access-token to let it cache streaming credentials.

        Returns True on success.
        """
        try:
            args = [
                "--client-id",
                OAUTH_CLIENT_ID,
                "--disable-discovery",
                "--get-token",
                "--scope",
                " ".join(SPOTTY_SCOPE),
                f"--access-token={access_token}",
            ]
            proc = self.__spotty.run_spotty(extra_args=args)
            proc.communicate(timeout=30)

            if os.path.exists(self.__spotty.get_spotty_credentials_file()):
                log_msg("Spotty credentials cached successfully after OAuth login.")
                return True

            log_msg(
                "Spotty --get-token completed but credentials.json was not created.",
                LOGWARNING,
            )
            return False
        except Exception as exc:
            log_exception(exc, "Failed to cache spotty credentials via --access-token")
            return False

    # ------------------------------------------------------------------
    #  Legacy zeroconf auth (kept for fallback but no longer primary)
    # ------------------------------------------------------------------

    def start_zeroconf_authenticate(self) -> Union[None, subprocess.Popen]:
        try:
            if os.path.exists(self.__spotty.get_spotty_credentials_file()):
                os.replace(
                    self.__spotty.get_spotty_credentials_file(),
                    self.__spotty.get_spotty_credentials_backup_file(),
                )
                log_msg(
                    f"Moved credentials file to"
                    f' "{self.__spotty.get_spotty_credentials_backup_file()}"'
                )

            args = [
                "--authenticate",
                "--zeroconf-port",
                str(ZEROCONF_PORT),
            ]
            return self.__spotty.run_spotty(extra_args=args)
        except Exception as exc:
            log_exception(exc, "Zeroconf authentication error")
            return None

    def zeroconf_authenticated_ok(self) -> bool:
        if os.path.exists(self.__spotty.get_spotty_credentials_file()):
            log_msg(
                f"Successfully authenticated. Credentials file created:"
                f' "{self.__spotty.get_spotty_credentials_file()}"'
            )
            return True
        log_msg(
            self.get_zeroconf_authentication_failed_msg(),
            loglevel=LOGERROR,
        )
        return False

    @staticmethod
    def get_zeroconf_program_failed_msg() -> str:
        return xbmcaddon.Addon(id=ADDON_ID).getLocalizedString(
            AUTHENTICATION_PROGRAM_FAILED_STR_ID
        )

    @staticmethod
    def get_zeroconf_authentication_failed_msg() -> str:
        msg = xbmcaddon.Addon(id=ADDON_ID).getLocalizedString(
            AUTHENTICATE_FAILED_STR_ID
        )
        cred_file = (
            f"<ADDON_DATA_DIR>/{SPOTTY_CACHE_DIR_NAME}/{SPOTTY_CREDENTIALS_FILENAME}"
        )
        return f'{msg}\n\n"{cred_file}".'

    # ------------------------------------------------------------------
    #  Token retrieval — OAuth first, spotty fallback
    # ------------------------------------------------------------------

    def renew_token(self) -> None:
        """Obtain a valid Spotify access_token and cache it in Kodi properties.

        Priority:
          1. OAuth refresh_token (simple HTTP POST)
          2. spotty --save-token (subprocess, needs credentials.json)

        Raises on total failure so the caller can decide how to handle it.
        """
        log_msg("Retrieving auth token...", LOGDEBUG)

        # --- Attempt 1: OAuth refresh ---
        auth_token = self._try_oauth_refresh()

        # --- Attempt 2: spotty --save-token (if credentials.json exists) ---
        if not auth_token:
            auth_token = self._try_spotty_token()

        if not auth_token:
            utils.cache_auth_token("")
            utils.cache_auth_token_expires_at("")
            raise AuthTokenUnavailable(
                "No Spotify auth token available."
                "  Open the addon and select 'Authenticate with Spotify'."
            )

        log_msg(
            f"Retrieved Spotify auth token."
            f" Expires at {utils.get_time_str(int(auth_token['expires_at']))}."
        )
        utils.cache_auth_token(str(auth_token["access_token"]))
        utils.cache_auth_token_expires_at(str(auth_token["expires_at"]))

    # --- internal helpers ---

    @staticmethod
    def _try_oauth_refresh() -> Optional[Dict]:
        """Try to get a token via OAuth refresh.  Returns None if not available."""
        if not oauth.is_authenticated():
            return None
        try:
            token_info = oauth.refresh_access_token(force=True)
            if token_info and "access_token" in token_info:
                log_msg("Auth token obtained via OAuth refresh.", LOGDEBUG)
                return token_info
        except Exception as exc:
            log_exception(exc, "OAuth refresh failed")
        return None

    def _try_spotty_token(self) -> Optional[Dict]:
        """Try to get a token by running spotty --save-token (needs credentials.json).

        Only attempts once — no 20-retry loop.
        """
        if not os.path.exists(self.__spotty.get_spotty_credentials_file()):
            log_msg(
                "No spotty credentials.json — skipping spotty token retrieval.",
                LOGDEBUG,
            )
            return None

        try:
            args = [
                "--client-id",
                OAUTH_CLIENT_ID,
                "--scope",
                ",".join(SPOTTY_SCOPE),
                "--save-token",
                self.__spotty.get_spotty_token_file(),
            ]
            proc = self.__spotty.run_spotty(extra_args=args)
            proc.communicate(timeout=30)

            with open(self.__spotty.get_spotty_token_file()) as f:
                json_token = json.load(f)

            expires_in_raw = json_token["expiresIn"]
            expires_in_secs = (
                expires_in_raw["secs"]
                if isinstance(expires_in_raw, dict)
                else int(expires_in_raw)
            )
            token_info = {
                "access_token": json_token["accessToken"],
                "expires_in": expires_in_secs,
                "expires_at": int(time.time()) + expires_in_secs,
                "refresh_token": json_token["accessToken"],
            }
            log_msg("Auth token obtained via spotty --save-token.", LOGDEBUG)
            return token_info

        except Exception as exc:
            log_exception(exc, "spotty --save-token failed")
            return None


class AuthTokenUnavailable(Exception):
    """Raised when no authentication method can produce a token."""
