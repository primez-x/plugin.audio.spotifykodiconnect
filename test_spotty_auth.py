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


if __name__ == "__main__":
    unittest.main()
