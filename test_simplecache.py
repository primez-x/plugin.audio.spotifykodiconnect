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

    def test_execute_sql_closes_connection(self):
        closed = []
        real_get_database = self.cache._get_database

        class _Tracking:
            def __init__(self, conn):
                self.conn = conn

            def execute(self, *args):
                return self.conn.execute(*args)

            def executemany(self, *args):
                return self.conn.executemany(*args)

            def close(self):
                closed.append(True)
                self.conn.close()

        self.cache._get_database = lambda: _Tracking(real_get_database())
        self.cache.set("k", [1, 2], checksum="c", mem_cache=False)
        self.assertEqual([1, 2], self.cache.get("k", checksum="c", mem_cache=False))
        self.assertGreaterEqual(len(closed), 2, "each query must close its connection")

    def test_execute_sql_handles_unavailable_database(self):
        self.cache._get_database = lambda: None
        self.assertIsNone(self.cache._execute_sql("SELECT 1"))
        self.cache.set("k", [1], mem_cache=False)
        self.assertIsNone(self.cache.get("k", mem_cache=False))

    def test_cleanup_survives_unavailable_database(self):
        self.cache._get_database = lambda: None
        self.cache._do_cleanup()

    def _count_connections(self):
        opened = []
        real_get_database = self.cache._get_database

        def _tracking():
            opened.append(True)
            return real_get_database()

        self.cache._get_database = _tracking
        return opened

    def test_set_many_then_get_many_round_trip(self):
        self.cache.set_many({"a": "1", "b": {"x": [1]}}, checksum="c", mem_cache=False)
        self.assertEqual(
            {"a": "1", "b": {"x": [1]}},
            self.cache.get_many(["a", "b", "missing"], checksum="c", mem_cache=False),
        )
        # rows are ordinary entries, readable one at a time as well
        self.assertEqual("1", self.cache.get("a", checksum="c", mem_cache=False))
        self.assertEqual({}, self.cache.get_many(["a", "b"], checksum="other", mem_cache=False))

    def test_set_many_accepts_pairs_and_mirrors_into_window_property(self):
        self.cache.set_many([("a", "1"), ("b", "0")], checksum="c")
        self.assertIn("a", _Window.props)
        self.assertIn("b", _Window.props)
        self.assertEqual({"a": "1", "b": "0"}, self.cache.get_many(["a", "b"], checksum="c"))

    def test_set_many_and_get_many_use_one_connection_each(self):
        opened = self._count_connections()
        self.cache.set_many({f"k{i}": str(i) for i in range(600)}, checksum="c", mem_cache=False)
        self.assertEqual(1, len(opened), "set_many must write all rows over one connection")
        del opened[:]
        result = self.cache.get_many([f"k{i}" for i in range(600)], checksum="c", mem_cache=False)
        self.assertEqual(600, len(result))
        self.assertEqual("599", result["k599"])
        self.assertEqual(1, len(opened), "get_many must read all rows over one connection")

    def test_get_many_serves_memory_hits_without_database(self):
        self.cache.set_many({"a": "1", "b": "2"}, checksum="c")
        opened = self._count_connections()
        self.assertEqual({"a": "1", "b": "2"}, self.cache.get_many(["a", "b"], checksum="c"))
        self.assertEqual([], opened)

    def test_get_many_db_hit_repopulates_memory_mirror(self):
        self.cache.set_many({"a": "1"}, checksum="c")
        _Window.props.clear()
        self.assertEqual({"a": "1"}, self.cache.get_many(["a"], checksum="c"))
        self.assertIn("a", _Window.props)

    def test_get_many_honours_expiration(self):
        import datetime

        self.cache.set_many(
            {"old": "1"}, checksum="c", expiration=datetime.timedelta(seconds=-5)
        )
        self.cache.set_many({"new": "1"}, checksum="c", expiration=datetime.timedelta(minutes=5))
        self.assertEqual({"new": "1"}, self.cache.get_many(["old", "new"], checksum="c"))
        self.assertIsNone(self.cache.get("old", checksum="c"))

    def test_get_many_drops_corrupt_rows(self):
        self.cache.set("bad", "x", checksum="c", mem_cache=False)
        self.cache._execute_sql(
            "UPDATE simplecache SET data = ? WHERE id = ?", ("{not json", "bad")
        )
        self.assertEqual({}, self.cache.get_many(["bad"], checksum="c", mem_cache=False))
        rows = self.cache._execute_sql("SELECT id FROM simplecache WHERE id = ?", ("bad",))
        self.assertEqual([], rows.fetchall())

    def test_many_methods_handle_unavailable_database(self):
        self.cache._get_database = lambda: None
        self.cache.set_many({"k": [1]}, mem_cache=False)
        self.assertEqual({}, self.cache.get_many(["k"], mem_cache=False))
        self.cache.set_many({})


if __name__ == "__main__":
    unittest.main()
