import json
import os
import shutil
import subprocess
import time
from typing import Dict, Union

import xbmcaddon
from xbmc import LOGDEBUG, LOGERROR, LOGWARNING

import utils
from spotty import Spotty, SPOTTY_CACHE_DIR_NAME, SPOTTY_CREDENTIALS_FILENAME
from string_ids import AUTHENTICATE_FAILED_STR_ID, AUTHENTICATION_PROGRAM_FAILED_STR_ID
from utils import log_msg, log_exception, ADDON_ID

ZEROCONF_PORT = 10001

CLIENT_ID = "2eb96f9b37494be1824999d58028a305"
SPOTTY_SCOPE = [
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
]


class SpottyAuth:
    def __init__(self, spotty: Spotty):
        self.__spotty = spotty

    def restore_credentials_from_backup_if_needed(self) -> bool:
        """Restore credentials.json from .bak if the live file is missing.

        start_zeroconf_authenticate() moves credentials.json -> .bak before
        re-pairing. If that re-pair is interrupted (power loss, Kodi kill,
        crash), the live file is missing but the previous valid credential
        blob sits unused in the backup. Without this recovery the addon
        cannot authenticate and forces the user through a full zeroconf
        re-pair even though a valid blob is one file copy away.

        Copy (not move) so the backup survives another interruption before
        spotty reads the restored file.

        Returns True if a restore was performed.
        """
        cred_file = self.__spotty.get_spotty_credentials_file()
        backup_file = self.__spotty.get_spotty_credentials_backup_file()

        if os.path.exists(cred_file):
            return False

        if not os.path.exists(backup_file):
            log_msg(
                "Spotify credentials file missing and no backup available; "
                "zeroconf re-authentication will be required.",
                loglevel=LOGWARNING,
            )
            return False

        try:
            shutil.copyfile(backup_file, cred_file)
            log_msg(
                "Restored Spotify credentials file from backup after detecting "
                f'the live file was missing: "{cred_file}".'
            )
            return True
        except OSError as exc:
            log_exception(exc, "Failed to restore credentials file from backup")
            return False

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
        msg = xbmcaddon.Addon(id=ADDON_ID).getLocalizedString(AUTHENTICATE_FAILED_STR_ID)
        cred_file = f"<ADDON_DATA_DIR>/{SPOTTY_CACHE_DIR_NAME}/{SPOTTY_CREDENTIALS_FILENAME}"
        return f'{msg}\n\n"{cred_file}".'

    def renew_token(self) -> None:
        log_msg("Retrieving auth token....", LOGDEBUG)

        auth_token = self.__get_retry_auth_token()
        if not auth_token:
            utils.cache_auth_token("")
            utils.cache_auth_token_expires_at("")
            raise Exception(
                f"Could not get Spotify auth token for" f" user '{utils.get_username()}'."
            )

        log_msg(
            f"Retrieved Spotify auth token."
            f" Expires at {utils.get_time_str(int(auth_token['expires_at']))}."
        )

        # Cache auth token for easy access by the plugin.
        utils.cache_auth_token(str(auth_token["access_token"]))
        utils.cache_auth_token_expires_at(str(auth_token["expires_at"]))

    def __get_retry_auth_token(self) -> Dict[str, str]:
        auth_token = None
        max_retries = 20
        count = 0
        while count < max_retries:
            auth_token = self.__get_token()
            if auth_token:
                break
            time.sleep(1)
            count += 1

        if count > 0:
            log_msg(f"Took {count} retries to get authorization token.", LOGWARNING)

        return auth_token

    def __get_token(self) -> Union[Dict[str, str], None]:
        token_info = None

        try:
            # Belt-and-suspenders against a poisoned token file: if a previous
            # run wrote an error-shaped payload, remove it before invoking
            # spotty again. See _remove_token_file_if_poisoned for the full
            # rationale.
            self._remove_token_file_if_poisoned()

            args = [
                "--client-id",
                CLIENT_ID,
                "--scope",
                ",".join(SPOTTY_SCOPE),
                "--save-token",
                self.__spotty.get_spotty_token_file(),
            ]
            spotty = self.__spotty.run_spotty(extra_args=args)

            stdout, stderr = spotty.communicate(timeout=30)
            # done.set()

            with open(self.__spotty.get_spotty_token_file()) as f:
                json_token = json.load(f)

            # Spotty writes {"error": "..."} into the --save-token path when
            # it cannot create a session (missing credentials, network not
            # ready at boot, Spotify outage, rate limit). Detect this shape
            # explicitly, clean up the poisoned file, and return None so the
            # caller's retry spawns spotty fresh on the next pass instead of
            # throwing KeyError and leaving the bad file in place.
            if not self._token_response_is_valid(json_token):
                keys = (
                    list(json_token.keys())
                    if isinstance(json_token, dict)
                    else type(json_token).__name__
                )
                err_detail = ""
                if isinstance(json_token, dict) and json_token.get("error"):
                    err_detail = f" — error: {json_token['error']}"
                log_msg(
                    f"Spotty returned a malformed token response (keys: {keys}){err_detail}."
                    " Removing token file; will retry.",
                    loglevel=LOGWARNING,
                )
                self._remove_token_file_if_poisoned()
                return None

            # Transform token info to spotipy compatible format.
            token_info = {
                "access_token": json_token["accessToken"],
                "expires_in": json_token["expiresIn"],
                "expires_at": int(time.time()) + json_token["expiresIn"],
                "refresh_token": json_token["accessToken"],
            }
            log_msg(
                f"Loaded Spotify auth token metadata. Expires in {json_token['expiresIn']} seconds.",
                LOGDEBUG,
            )

        except Exception as exc:
            log_exception(exc, "Get Spotify token error")

        return token_info

    @staticmethod
    def _token_response_is_valid(json_token) -> bool:
        """A valid spotty token response has accessToken + expiresIn and no error key."""
        return (
            isinstance(json_token, dict)
            and "error" not in json_token
            and "accessToken" in json_token
            and "expiresIn" in json_token
        )

    def _remove_token_file_if_poisoned(self) -> None:
        """Delete the token file if it exists but holds an error-shaped or malformed payload.

        spotty writes {"error": "Failed to create session or connect to servers."}
        into the --save-token path when it cannot reach Spotify or authenticate.
        Without this guard, every retry sees the same poisoned file, throws
        KeyError on json_token["accessToken"], and burns through the retry budget
        without recovery — leaving the addon permanently broken until the user
        manually deletes the file.
        """
        token_file = self.__spotty.get_spotty_token_file()
        if not os.path.exists(token_file):
            return

        try:
            with open(token_file) as f:
                existing = json.load(f)
        except (OSError, ValueError):
            # Unreadable / corrupt JSON — treat as poisoned.
            existing = None

        if existing is None or not self._token_response_is_valid(existing):
            try:
                os.remove(token_file)
                log_msg(
                    "Removed stale malformed spotty token file before re-invoking spotty.",
                    LOGDEBUG,
                )
            except OSError as exc:
                log_exception(exc, "Failed to remove malformed spotty token file")
