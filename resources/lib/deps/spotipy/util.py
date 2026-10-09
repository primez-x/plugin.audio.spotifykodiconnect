from __future__ import annotations

""" Shows a user's playlists. This needs to be authenticated via OAuth. """

__all__ = ["CLIENT_CREDS_ENV_VARS", "prompt_for_user_token"]

import logging
import os
import warnings
from types import TracebackType

import spotipy

import urllib3

LOGGER = logging.getLogger(__name__)

CLIENT_CREDS_ENV_VARS = {
    "client_id": "SPOTIPY_CLIENT_ID",
    "client_secret": "SPOTIPY_CLIENT_SECRET",
    "client_username": "SPOTIPY_CLIENT_USERNAME",
    "redirect_uri": "SPOTIPY_REDIRECT_URI",
}


def prompt_for_user_token(
    username=None,
    scope=None,
    client_id=None,
    client_secret=None,
    redirect_uri=None,
    cache_path=None,
    oauth_manager=None,
    show_dialog=False
):
    warnings.warn(
        "'prompt_for_user_token' is deprecated."
        "Use the following instead: "
        "    auth_manager=SpotifyOAuth(scope=scope)"
        "    spotipy.Spotify(auth_manager=auth_manager)",
        DeprecationWarning
    )
    """Prompt the user to login if necessary and returns a user token
       suitable for use with the spotipy.Spotify constructor.

        Parameters:
            - username - the Spotify username. (optional)
            - scope - the desired scope of the request. (optional)
            - client_id - the client ID of your app. (required)
            - client_secret - the client secret of your app. (required)
            - redirect_uri - the redirect URI of your app. (required)
            - cache_path - path to location to save tokens. (required)
            - oauth_manager - OAuth manager object. (optional)
            - show_dialog - If True, a login prompt always shows or defaults to False. (optional)
    """
    if not oauth_manager:
        if not client_id:
            client_id = os.getenv("SPOTIPY_CLIENT_ID")

        if not client_secret:
            client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")

        if not redirect_uri:
            redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI")

        if not client_id:
            LOGGER.warning(
                """
                You need to set your Spotify API credentials.
                You can do this by setting environment variables like so:

                export SPOTIPY_CLIENT_ID='your-spotify-client-id'
                export SPOTIPY_CLIENT_SECRET='your-spotify-client-secret'
                export SPOTIPY_REDIRECT_URI='your-app-redirect-url'

                Get your credentials at
                    https://developer.spotify.com/my-applications
            """
            )
            raise spotipy.SpotifyException(550, -1, "no credentials set")

    sp_oauth = oauth_manager or spotipy.SpotifyOAuth(
        client_id,
        client_secret,
        redirect_uri,
        scope=scope,
        cache_path=cache_path,
        username=username,
        show_dialog=show_dialog
    )

    # try to get a valid token for this user, from the cache,
    # if not in the cache, then create a new (this will send
    # the user to a web page where they can authorize this app)

    token_info = sp_oauth.validate_token(sp_oauth.cache_handler.get_cached_token())

    if not token_info:
        code = sp_oauth.get_auth_response()
        token = sp_oauth.get_access_token(code, as_dict=False)
    else:
        return token_info["access_token"]

    # Auth'ed API request
    if token:
        return token
    else:
        return None


def get_host_port(netloc):
    """ Split the network location string into host and port and returns a tuple
        where the host is a string and the the port is an integer.

        Parameters:
            - netloc - a string representing the network location.
    """
    if ":" in netloc:
        host, port = netloc.split(":", 1)
        port = int(port)
    else:
        host = netloc
        port = None

    return host, port


def normalize_scope(scope):
    """Normalize the scope to verify that it is a list or tuple. A string
    input will split the string by commas to create a list of scopes.
    A list or tuple input is used directly.

    Parameters:
        - scope - a string representing scopes separated by commas,
                  or a list/tuple of scopes.
    """
    if scope:
        if isinstance(scope, str):
            scopes = scope.split(',')
        elif isinstance(scope, list) or isinstance(scope, tuple):
            scopes = scope
        else:
            raise Exception(
                "Unsupported scope value, please either provide a list of scopes, "
                "or a string of scopes separated by commas."
            )
        return " ".join(sorted(scopes))
    else:
        return None


# SpotifyKodiConnect: urllib3 honours Retry-After without an upper bound, so a
# 429 with "Retry-After: 3600" would block the calling Kodi thread for an hour.
# Retry-After values above this cap fail fast instead of sleeping.
MAX_RETRY_AFTER_SECS = 10

# Optional hook, set by the add-on, called with the absolute epoch time until
# which Spotify asked us to back off when a long Retry-After is refused.
on_long_rate_limit = None


class Retry(urllib3.Retry):
    """
    Custom class for printing a warning when a rate/request limit is reached.

    Fails fast (MaxRetryError -> requests RetryError -> SpotifyException 429)
    instead of sleeping when Spotify's Retry-After exceeds MAX_RETRY_AFTER_SECS.
    """

    def _long_retry_after(self, response) -> float | None:
        if response is None or getattr(response, "status", None) != 429:
            return None
        header = response.headers.get("Retry-After") if response.headers else None
        if not header:
            return None
        try:
            retry_after = float(self.parse_retry_after(header))
        except Exception:
            return None
        return retry_after if retry_after > MAX_RETRY_AFTER_SECS else None
    def increment(
            self,
            method: str | None = None,
            url: str | None = None,
            response: urllib3.BaseHTTPResponse | None = None,
            error: Exception | None = None,
            _pool: urllib3.connectionpool.ConnectionPool | None = None,
            _stacktrace: TracebackType | None = None,
    ) -> urllib3.Retry:
        long_retry_after = self._long_retry_after(response)
        if long_retry_after is not None:
            logging.warning(
                "Spotify rate limit with Retry-After %.0fs exceeds %ss; not retrying.",
                long_retry_after,
                MAX_RETRY_AFTER_SECS,
            )
            hook = on_long_rate_limit
            if callable(hook):
                try:
                    import time

                    hook(time.time() + long_retry_after)
                except Exception:
                    pass
            raise urllib3.exceptions.MaxRetryError(
                _pool,
                url,
                urllib3.exceptions.ResponseError(
                    f"too many 429 error responses (Retry-After {long_retry_after:.0f}s)"
                ),
            )
        if response:
            retry_header = response.headers.get("Retry-After")
            if self.is_retry(method, response.status, bool(retry_header)):
                logging.warning("Your application has reached a rate/request limit. "
                                f"Retry will occur after: {retry_header}")
        return super().increment(method,
                                 url,
                                 response=response,
                                 error=error,
                                 _pool=_pool,
                                 _stacktrace=_stacktrace)
