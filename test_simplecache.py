"""Vendored simplecache: database-only entries and connection handling."""

import os
import shutil
import sys
import tempfile
import types
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DEPS_DIR = os.path.join(REPO_ROOT, "resources", "lib", "deps")


class _Window:
    props = {}

    def __init__(self, _id=None):
        pass

    def getProperty(self, key):
        return _Window.props.get(key, "")

    def setProperty(self, key, value):
        _Window.props[key] = value

    def clearProperty(self, key):
        _Window.props.pop(key, None)


class _Monitor:
    def abortRequested(self):
        return False

    def waitForAbort(self, timeout=None):
        return False


def import_simplecache(profile_dir):
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = 0
    xbmc.LOGWARNING = 2
    xbmc.Monitor = _Monitor
    xbmc.sleep = lambda ms: None
    xbmc.log = lambda msg, level=0: None
    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Window = _Window
    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda id=None: types.SimpleNamespace(getAddonInfo=lambda key: profile_dir)
    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = lambda path: path
    xbmcvfs.exists = os.path.exists
    xbmcvfs.mkdirs = lambda path: os.makedirs(path, exist_ok=True)
    xbmcvfs.delete = os.remove
    saved = {name: sys.modules.get(name) for name in ("xbmc", "xbmcgui", "xbmcaddon", "xbmcvfs")}
    sys.modules.update(xbmc=xbmc, xbmcgui=xbmcgui, xbmcaddon=xbmcaddon, xbmcvfs=xbmcvfs)
    sys.path.insert(0, DEPS_DIR)
    sys.modules.pop("simplecache", None)
    try:
        import simplecache
    finally:
        sys.path.remove(DEPS_DIR)
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return simplecache


class SimpleCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="simplecache_test_")
        _Window.props = {}
        self.simplecache = import_simplecache(self.tmp)
        self.cache = self.simplecache.SimpleCache("plugin.test")

    def tearDown(self):
        self.cache.close()
        sys.modules.pop("simplecache", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_set_mirrors_into_window_property(self):
        self.cache.set("small", {"a": 1}, checksum="c")
        self.assertIn("small", _Window.props)
        self.assertEqual({"a": 1}, self.cache.get("small", checksum="c"))

    def test_db_only_entry_skips_window_property(self):
        _Window.props["big"] = "stale"
        self.cache.set("big", {"items": list(range(5))}, checksum="c", mem_cache=False)
        self.assertNotIn("big", _Window.props, "stale mirror must be dropped")
        self.assertEqual(
            {"items": list(range(5))}, self.cache.get("big", checksum="c", mem_cache=False)
        )
        self.assertNotIn("big", _Window.props, "DB read must not repopulate the mirror")

    def test_db_only_checksum_mismatch_misses(self):
        self.cache.set("big", [1], checksum="c1", mem_cache=False)
        self.assertIsNone(self.cache.get("big", checksum="c2", mem_cache=False))


if __name__ == "__main__":
    unittest.main()
