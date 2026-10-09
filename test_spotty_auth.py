"""Tests for SpottyAuth credential-rotation recovery and poisoned-token handling.

Covers two hardening fixes added after a post-power-outage incident where:
  1. An interrupted credential rotation left only credentials.json.bak on
     disk; the addon never restored from the backup, forcing a full zeroconf
     re-pair even though a valid blob was one file copy away.
  2. spotty wrote {"error": "..."} into the --save-token path because it
     could not authenticate; __get_token threw KeyError on every retry,
     leaving the addon permanently broken until the file was manually
     deleted.
"""

import json
import os
import sys
import types
import unittest

REPO_ROOT = os.path.dirname(__file__)
LIB_DIR = os.path.join(REPO_ROOT, "resources", "lib")
if LIB_DIR not in sys.path:
    sys.path.insert(0, LIB_DIR)


# ---------------------------------------------------------------------------
# Kodi module stubs (mirrors the pattern in test_main_service_osd.py)
# ---------------------------------------------------------------------------


class _FakeAddon:
    def getAddonInfo(self, key):
        return "test"

    def getSetting(self, key):
        return ""

    def getLocalizedString(self, _id):
        return f"localized_{_id}"


def _make_kodi_stub():
    mod = types.ModuleType("xbmc")
    mod.LOGDEBUG = 1
    mod.LOGINFO = 2
    mod.LOGWARNING = 3
    mod.LOGERROR = 4
    mod.log = lambda msg, level=1: None
    mod.sleep = lambda ms: None

    class _FakeMonitor:
        def waitForAbort(self, timeout=None):
            return False

        def abortRequested(self):
            return False

    mod.Monitor = _FakeMonitor

    addon_mod = types.ModuleType("xbmcaddon")
    addon_mod.Addon = lambda id=None: _FakeAddon()

    vfs_mod = types.ModuleType("xbmcvfs")
    vfs_mod.translatePath = lambda p: p.replace("special://profile/", "/tmp/fake_profile/")

    gui_mod = types.ModuleType("xbmcgui")

    class _FakeWindow:
        def __init__(self, window_id):
            self.properties = {}

        def getProperty(self, key):
            return self.properties.get(key, "")

        def setProperty(self, key, value):
            self.properties[key] = value

        def clearProperty(self, key):
            self.properties.pop(key, None)

    gui_mod.Window = _FakeWindow

    return mod, addon_mod, vfs_mod, gui_mod


_xbmc, _xbmcaddon, _xbmcvfs, _xbmcgui = _make_kodi_stub()
sys.modules.setdefault("xbmc", _xbmc)
sys.modules.setdefault("xbmcaddon", _xbmcaddon)
sys.modules.setdefault("xbmcvfs", _xbmcvfs)
sys.modules.setdefault("xbmcgui", _xbmcgui)

# Re-import target modules fresh per test run (they read Kodi stubs at import).
for _name in ("utils", "spotty", "string_ids", "spotty_auth"):
    sys.modules.pop(_name, None)

import spotty_auth  # noqa: E402
from spotty_auth import SpottyAuth  # noqa: E402

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeSpotty:
    """Minimal Spotty stand-in exposing the path helpers SpottyAuth uses."""

    def __init__(self, cache_dir):
        self._cache_dir = cache_dir
        self.token_file = os.path.join(cache_dir, "spotty-token")
        self.cred_file = os.path.join(cache_dir, "credentials.json")
        self.cred_backup = os.path.join(cache_dir, "credentials.json.bak")

    def get_spotty_token_file(self):
        return self.token_file

    def get_spotty_credentials_file(self):
        return self.cred_file

    def get_spotty_credentials_backup_file(self):
        return self.cred_backup


VALID_TOKEN_PAYLOAD = {
    "accessToken": "test-access-token",
    "expiresIn": 3600,
}

ERROR_PAYLOAD = {"error": "Failed to create session or connect to servers."}


# ---------------------------------------------------------------------------
# restore_credentials_from_backup_if_needed
# ---------------------------------------------------------------------------


class RestoreCredentialsFromBackupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = "/tmp/test_spotty_auth_restore_%d" % os.getpid()
        os.makedirs(self._tmp, exist_ok=True)
        # Clean slate
        for fname in ("credentials.json", "credentials.json.bak", "spotty-token"):
            p = os.path.join(self._tmp, fname)
            if os.path.exists(p):
                os.remove(p)
        self.spotty = FakeSpotty(self._tmp)
        self.auth = SpottyAuth(self.spotty)

    def tearDown(self):
        for root, _dirs, files in os.walk(self._tmp, topdown=False):
            for fname in files:
                os.remove(os.path.join(root, fname))
        os.rmdir(self._tmp)

    def _write(self, path, payload):
        with open(path, "w") as f:
            f.write(payload)

    def test_restores_when_live_missing_and_backup_exists(self):
        """The core regression: live creds gone, .bak present -> restored."""
        self._write(self.spotty.cred_backup, '{"username":"u","auth_type":1,"auth_data":"abc"}')
        result = self.auth.restore_credentials_from_backup_if_needed()
        self.assertTrue(result, "restore should return True when it performed a copy")
        self.assertTrue(
            os.path.exists(self.spotty.cred_file), "credentials.json should exist after restore"
        )
        # Backup must be preserved (copy, not move) so it survives another interruption.
        self.assertTrue(os.path.exists(self.spotty.cred_backup), ".bak must survive the restore")
        with open(self.spotty.cred_file) as f:
            self.assertEqual(f.read(), '{"username":"u","auth_type":1,"auth_data":"abc"}')

    def test_noop_when_live_file_present(self):
        """Happy path: nothing to do when credentials.json already exists."""
        self._write(self.spotty.cred_file, '{"username":"u"}')
        self._write(self.spotty.cred_backup, '{"username":"old"}')
        result = self.auth.restore_credentials_from_backup_if_needed()
        self.assertFalse(result, "restore should return False when live file exists")
        # Live file must be untouched.
        with open(self.spotty.cred_file) as f:
            self.assertEqual(f.read(), '{"username":"u"}')

    def test_noop_when_neither_file_exists(self):
        """Fresh install with no prior auth: warn but do not raise."""
        result = self.auth.restore_credentials_from_backup_if_needed()
        self.assertFalse(result)
        self.assertFalse(os.path.exists(self.spotty.cred_file))

    def test_noop_when_only_live_exists(self):
        """Normal operating state: live creds, no backup yet."""
        self._write(self.spotty.cred_file, '{"username":"u"}')
        result = self.auth.restore_credentials_from_backup_if_needed()
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# __get_token poisoned-file handling
# ---------------------------------------------------------------------------


class PoisonedTokenFileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = "/tmp/test_spotty_auth_poison_%d" % os.getpid()
        os.makedirs(self._tmp, exist_ok=True)
        for fname in ("credentials.json", "credentials.json.bak", "spotty-token"):
            p = os.path.join(self._tmp, fname)
            if os.path.exists(p):
                os.remove(p)
        self.spotty = FakeSpotty(self._tmp)
        self.auth = SpottyAuth(self.spotty)

    def tearDown(self):
        for root, _dirs, files in os.walk(self._tmp, topdown=False):
            for fname in files:
                os.remove(os.path.join(root, fname))
        os.rmdir(self._tmp)

    def _write_token(self, payload):
        with open(self.spotty.token_file, "w") as f:
            json.dump(payload, f)

    def test_token_response_is_valid_accepts_well_formed_payload(self):
        self.assertTrue(SpottyAuth._token_response_is_valid(VALID_TOKEN_PAYLOAD))

    def test_token_response_is_valid_rejects_error_payload(self):
        self.assertFalse(SpottyAuth._token_response_is_valid(ERROR_PAYLOAD))

    def test_token_response_is_valid_rejects_missing_accesstoken(self):
        self.assertFalse(SpottyAuth._token_response_is_valid({"expiresIn": 3600}))

    def test_token_response_is_valid_rejects_non_dict(self):
        self.assertFalse(SpottyAuth._token_response_is_valid("not a dict"))
        self.assertFalse(SpottyAuth._token_response_is_valid(None))

    def test_remove_poisoned_file_deletes_error_shaped_payload(self):
        """The exact payload that caused the incident: error JSON in token file."""
        self._write_token(ERROR_PAYLOAD)
        self.assertTrue(os.path.exists(self.spotty.token_file))
        self.auth._remove_token_file_if_poisoned()
        self.assertFalse(
            os.path.exists(self.spotty.token_file),
            "poisoned token file must be deleted so the next spotty run starts clean",
        )

    def test_remove_poisoned_file_deletes_corrupt_json(self):
        """Garbage bytes in the token file must also be cleared."""
        with open(self.spotty.token_file, "w") as f:
            f.write("{not even valid json")
        self.auth._remove_token_file_if_poisoned()
        self.assertFalse(os.path.exists(self.spotty.token_file))

    def test_remove_poisoned_file_leaves_valid_token_untouched(self):
        """A healthy token file must survive the pre-flight check."""
        self._write_token(VALID_TOKEN_PAYLOAD)
        self.auth._remove_token_file_if_poisoned()
        self.assertTrue(os.path.exists(self.spotty.token_file))

    def test_remove_poisoned_file_noop_when_file_absent(self):
        """No file -> no work, no error."""
        self.auth._remove_token_file_if_poisoned()  # must not raise

    def test_get_token_returns_none_and_cleans_up_when_spotty_writes_error(self):
        """End-to-end: spotty writes {error:...}; __get_token must return None
        and delete the poisoned file so the retry loop can recover.
        """
        self._write_token(ERROR_PAYLOAD)

        # Stub run_spotty to simulate spotty overwriting the file with another
        # error payload (as it does when it cannot authenticate).
        class _FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                # spotty would write the new error payload here.
                with open(self_file[0], "w") as f:
                    json.dump(ERROR_PAYLOAD, f)
                return (b"", b"")

        self_file = [self.spotty.token_file]

        def fake_run_spotty(extra_args=None):
            return _FakeProc()

        self.auth._SpottyAuth__spotty.run_spotty = fake_run_spotty  # type: ignore[attr-defined]

        result = self.auth._SpottyAuth__get_token()  # type: ignore[attr-defined]

        self.assertIsNone(
            result, "__get_token must return None when spotty returns an error payload"
        )
        self.assertFalse(
            os.path.exists(self.spotty.token_file),
            "poisoned token file must be cleaned up so the retry can recover",
        )

    def test_get_token_succeeds_when_spotty_writes_valid_payload(self):
        """Happy path still works end-to-end after hardening."""
        # Pre-existing poisoned file in place; the pre-flight cleanup deletes it
        # before spotty runs, then spotty writes a valid token.
        self._write_token(ERROR_PAYLOAD)

        class _FakeProc:
            returncode = 0

            def communicate(self, timeout=None):
                with open(self_file[0], "w") as f:
                    json.dump(VALID_TOKEN_PAYLOAD, f)
                return (b"", b"")

        self_file = [self.spotty.token_file]

        def fake_run_spotty(extra_args=None):
            return _FakeProc()

        self.auth._SpottyAuth__spotty.run_spotty = fake_run_spotty  # type: ignore[attr-defined]

        result = self.auth._SpottyAuth__get_token()  # type: ignore[attr-defined]

        self.assertIsNotNone(result, "valid spotty response must produce a token_info dict")
        self.assertEqual(result["access_token"], "test-access-token")
        self.assertIn("expires_at", result)

    def _run_fake_spotty(self, returncode=0, payload=None):
        """Stub run_spotty; record whether the old token file was gone at spawn."""
        seen = []
        token_file = self.spotty.token_file

        class _FakeProc:
            def __init__(self):
                self.returncode = None

            def communicate(self, timeout=None):
                seen.append(os.path.exists(token_file))
                if payload is not None:
                    with open(token_file, "w") as f:
                        json.dump(payload, f)
                self.returncode = returncode
                return (b"", b"")

        self.auth._SpottyAuth__spotty.run_spotty = lambda extra_args=None: _FakeProc()
        result = self.auth._SpottyAuth__get_token()  # type: ignore[attr-defined]
        return result, seen

    def test_get_token_removes_previous_valid_token_file_before_spawning(self):
        self._write_token(VALID_TOKEN_PAYLOAD)
        result, seen = self._run_fake_spotty(payload=VALID_TOKEN_PAYLOAD)
        self.assertEqual([False], seen, "old token file must be gone before spotty runs")
        self.assertIsNotNone(result)

    def test_get_token_rejects_stale_file_when_spotty_writes_nothing(self):
        """A previous run's valid token must not be accepted as this run's result."""
        self._write_token(VALID_TOKEN_PAYLOAD)
        result, _seen = self._run_fake_spotty(payload=None)
        self.assertIsNone(result)
        self.assertFalse(os.path.exists(self.spotty.token_file))

    def test_get_token_accepts_fresh_file_despite_nonzero_exit(self):
        """The stale file is removed first, so a file present afterwards is fresh."""
        self._write_token(VALID_TOKEN_PAYLOAD)
        result, _seen = self._run_fake_spotty(returncode=1, payload=VALID_TOKEN_PAYLOAD)
        self.assertIsNotNone(result)

    def test_get_token_rejects_stale_file_on_nonzero_exit(self):
        self._write_token(VALID_TOKEN_PAYLOAD)
        result, _seen = self._run_fake_spotty(returncode=1, payload=None)
        self.assertIsNone(result)

    def test_get_token_rejects_unrewritten_file_when_removal_fails(self):
        """If the old file cannot be deleted, an unchanged mtime means it is stale."""
        self._write_token(VALID_TOKEN_PAYLOAD)
        real_remove = spotty_auth.os.remove

        def failing_remove(path):
            if path == self.spotty.token_file:
                raise PermissionError("read-only")
            return real_remove(path)

        spotty_auth.os.remove = failing_remove
        try:
            result, seen = self._run_fake_spotty(payload=None)
        finally:
            spotty_auth.os.remove = real_remove
        self.assertEqual([True], seen)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Renewal robustness: timeouts, bounded retries, abort, token preservation
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.calls = []


class RenewalRobustnessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = "/tmp/test_spotty_auth_renew_%d" % os.getpid()
        os.makedirs(self._tmp, exist_ok=True)
        for fname in ("credentials.json", "credentials.json.bak", "spotty-token"):
            p = os.path.join(self._tmp, fname)
            if os.path.exists(p):
                os.remove(p)
        self.spotty = FakeSpotty(self._tmp)
        self.auth = SpottyAuth(self.spotty)
        self._saved = {
            name: getattr(spotty_auth.utils, name)
            for name in (
                "cache_auth_token",
                "cache_auth_token_expires_at",
                "cached_auth_token_is_unexpired",
                "zeroconf_pairing_in_progress",
                "get_username",
            )
        }
        self._saved_monitor = spotty_auth.xbmc.Monitor
        self.cached = {"token": "old-token", "expires": "999"}
        spotty_auth.utils.cache_auth_token = lambda v: self.cached.__setitem__("token", v)
        spotty_auth.utils.cache_auth_token_expires_at = lambda v: self.cached.__setitem__(
            "expires", v
        )
        spotty_auth.utils.zeroconf_pairing_in_progress = lambda now=None: False
        spotty_auth.utils.get_username = lambda: "user"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(spotty_auth.utils, name, value)
        spotty_auth.xbmc.Monitor = self._saved_monitor
        for root, _dirs, files in os.walk(self._tmp, topdown=False):
            for fname in files:
                os.remove(os.path.join(root, fname))
        os.rmdir(self._tmp)

    def test_get_token_kills_and_reaps_hung_spotty(self):
        import subprocess

        events = []

        class _HungProc:
            def __init__(self):
                self.killed = False

            def communicate(self, timeout=None):
                events.append(("communicate", timeout, self.killed))
                if not self.killed:
                    raise subprocess.TimeoutExpired("spotty", timeout)
                return (b"", b"")

            def kill(self):
                self.killed = True
                events.append(("kill",))

        self.auth._SpottyAuth__spotty.run_spotty = lambda extra_args=None: _HungProc()
        result = self.auth._SpottyAuth__get_token()  # type: ignore[attr-defined]

        self.assertIsNone(result)
        self.assertEqual(("communicate", spotty_auth.TOKEN_FETCH_TIMEOUT_SECS, False), events[0])
        self.assertEqual(("kill",), events[1])
        self.assertEqual("communicate", events[2][0])
        self.assertTrue(events[2][2], "hung child must be reaped after kill")
        self.assertLessEqual(spotty_auth.TOKEN_FETCH_TIMEOUT_SECS, 15)

    def test_retry_is_bounded_and_uses_monitor_wait(self):
        waits = []

        class _Monitor:
            def waitForAbort(self, timeout=None):
                waits.append(timeout)
                return False

        spotty_auth.xbmc.Monitor = _Monitor
        attempts = []
        self.auth._SpottyAuth__get_token = lambda: attempts.append(1)  # type: ignore
        result = self.auth._SpottyAuth__get_retry_auth_token()  # type: ignore[attr-defined]

        self.assertIsNone(result)
        self.assertEqual(spotty_auth.TOKEN_FETCH_MAX_RETRIES, len(attempts))
        self.assertEqual(3, spotty_auth.TOKEN_FETCH_MAX_RETRIES)
        self.assertEqual(spotty_auth.TOKEN_FETCH_MAX_RETRIES - 1, len(waits))

    def test_retry_stops_on_abort(self):
        class _AbortingMonitor:
            def waitForAbort(self, timeout=None):
                return True

        spotty_auth.xbmc.Monitor = _AbortingMonitor
        attempts = []
        self.auth._SpottyAuth__get_token = lambda: attempts.append(1)  # type: ignore
        self.auth._SpottyAuth__get_retry_auth_token()  # type: ignore[attr-defined]
        self.assertEqual(1, len(attempts))

    def test_renew_failure_keeps_unexpired_token(self):
        spotty_auth.utils.cached_auth_token_is_unexpired = lambda now=None: True
        self.auth._SpottyAuth__get_retry_auth_token = lambda: None  # type: ignore
        with self.assertRaises(Exception):
            self.auth.renew_token()
        self.assertEqual("old-token", self.cached["token"])
        self.assertEqual("999", self.cached["expires"])

    def test_renew_failure_clears_expired_token(self):
        spotty_auth.utils.cached_auth_token_is_unexpired = lambda now=None: False
        self.auth._SpottyAuth__get_retry_auth_token = lambda: None  # type: ignore
        with self.assertRaises(Exception):
            self.auth.renew_token()
        self.assertEqual("", self.cached["token"])
        self.assertEqual("", self.cached["expires"])

    def test_renew_restores_backup_before_attempt(self):
        with open(self.spotty.cred_backup, "w") as f:
            f.write("{}")
        seen = []

        def fake_retry():
            seen.append(os.path.exists(self.spotty.cred_file))
            return {"access_token": "new", "expires_at": 12345}

        self.auth._SpottyAuth__get_retry_auth_token = fake_retry  # type: ignore
        self.auth.renew_token()
        self.assertEqual([True], seen)
        self.assertEqual("new", self.cached["token"])

    def test_renew_publishes_expiry_before_token(self):
        order = []
        spotty_auth.utils.cache_auth_token = lambda v: order.append(("token", v))
        spotty_auth.utils.cache_auth_token_expires_at = lambda v: order.append(("expires", v))
        self.auth._SpottyAuth__get_retry_auth_token = lambda: {  # type: ignore
            "access_token": "new",
            "expires_at": 12345,
        }
        self.auth.renew_token()
        # A reader (or the service's expired-token sweep) must never see the
        # new token paired with the previous, already-expired expiry.
        self.assertEqual([("expires", "12345"), ("token", "new")], order)

    def test_restore_skipped_while_zeroconf_pairing(self):
        with open(self.spotty.cred_backup, "w") as f:
            f.write("{}")
        spotty_auth.utils.zeroconf_pairing_in_progress = lambda now=None: True
        self.assertFalse(self.auth.restore_credentials_from_backup_if_needed())
        self.assertFalse(os.path.exists(self.spotty.cred_file))

    def test_has_stored_credentials(self):
        self.assertFalse(self.auth.has_stored_credentials())
        with open(self.spotty.cred_backup, "w") as f:
            f.write("{}")
        self.assertTrue(self.auth.has_stored_credentials())


if __name__ == "__main__":
    unittest.main()
