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
        self.simplecache.SimpleCache._busy_tasks = []
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

        self.cache.set_many({"old": "1"}, checksum="c", expiration=datetime.timedelta(seconds=-5))
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

    # -- cleanup ------------------------------------------------------------

    def _insert_rows(self, rows):
        import sqlite3

        connection = sqlite3.connect(os.path.join(self.tmp, "simplecache.db"))
        connection.executemany(
            "INSERT OR REPLACE INTO simplecache(id, expires, data, checksum) VALUES (?,?,?,?)",
            rows,
        )
        connection.commit()
        connection.close()

    def _count_rows(self):
        return self.cache._execute_sql("SELECT COUNT(*) FROM simplecache").fetchone()[0]

    def test_cleanup_deletes_expired_rows_in_one_statement_without_vacuum(self):
        import time

        self.cache.set("live", "1", checksum="c")  # creates the table, mirrors "live"
        now = int(time.time())
        self._insert_rows(
            [("spotify.relation.x.%05d" % i, now - 10, '"1"', 1) for i in range(4000)]
            + [("still-live", now + 3600, '"1"', 1)]
        )
        _Window.props["spotify.relation.x.00007"] = "expired mirror"
        statements = []
        opened = []
        real_get_database = self.cache._get_database

        def _tracing():
            connection = real_get_database()
            opened.append(True)
            connection.set_trace_callback(statements.append)
            return connection

        self.cache._get_database = _tracing
        self.cache._do_cleanup()

        self.assertEqual(1, len(opened), "cleanup must use a single connection")
        self.assertFalse(any("VACUUM" in sql.upper() for sql in statements))
        self.assertEqual(1, sum("DELETE FROM simplecache" in sql for sql in statements))
        self.cache._get_database = real_get_database
        self.assertEqual(2, self._count_rows())
        self.assertNotIn("spotify.relation.x.00007", _Window.props)
        self.assertIn("live", _Window.props, "live mirrors survive the cleanup")
        self.assertEqual({}, self.cache.get_many(["spotify.relation.x.00001"]))
        self.assertTrue(_Window.props.get("simplecache.clean.lastexecuted"))
        self.assertNotIn("simplecachecleanbusy", _Window.props)
        self.assertEqual([], self.simplecache.SimpleCache._busy_tasks)

    def test_cleanup_busy_elsewhere_does_not_leave_a_busy_task(self):
        """A process crossing the 4 h mark while another cleans must not hang in close()."""
        import datetime

        _Window.props["simplecache.clean.lastexecuted"] = (
            datetime.datetime.now() - datetime.timedelta(hours=5)
        ).isoformat()
        _Window.props["simplecachecleanbusy"] = "busy"
        cache = self.simplecache.SimpleCache("plugin.test")
        self.assertEqual([], self.simplecache.SimpleCache._busy_tasks)
        sleeps = []

        def _sleep(ms):
            sleeps.append(ms)
            if len(sleeps) > 5:
                raise AssertionError("close() is spinning on a leaked busy task")

        self.simplecache.xbmc.sleep = _sleep
        cache.close()
        self.assertEqual([], sleeps)

    def test_cleanup_failure_releases_busy_task_and_flag(self):
        def _boom(func):
            raise RuntimeError("db exploded")

        self.cache._run_on_database = _boom
        with self.assertRaises(RuntimeError):
            self.cache._do_cleanup()
        self.assertEqual([], self.simplecache.SimpleCache._busy_tasks)
        self.assertNotIn("simplecachecleanbusy", _Window.props)

    def test_set_failure_releases_busy_task(self):
        def _boom(*args):
            raise RuntimeError("write failed")

        self.cache._set_db_cache = _boom
        with self.assertRaises(RuntimeError):
            self.cache.set("k", "v", mem_cache=False)
        self.assertEqual([], self.simplecache.SimpleCache._busy_tasks)

    # -- vacuum -------------------------------------------------------------

    def test_vacuum_runs_only_when_due_and_worthwhile(self):
        import datetime

        self.cache.set_many({f"k{i}": "x" * 2000 for i in range(400)}, mem_cache=False)
        self.cache._execute_sql("DELETE FROM simplecache WHERE id LIKE 'k%'")
        self.assertTrue(self.cache.vacuum_if_due(interval=datetime.timedelta(days=7)))
        self.assertFalse(self.cache.vacuum_if_due(interval=datetime.timedelta(days=7)), "not due")
        # due again, but nothing to reclaim
        self.assertFalse(self.cache.vacuum_if_due(interval=datetime.timedelta(seconds=-1)))

    # -- checksum -----------------------------------------------------------

    def test_checksum_is_crc32_and_does_not_collide_on_reordered_digits(self):
        import zlib

        checksum = self.cache._get_checksum
        # the former sum of character codes made these equal
        self.assertNotEqual(checksum("v6-playlist-19"), checksum("v6-playlist-28"))
        self.assertNotEqual(checksum("bucket-5895212"), checksum("bucket-5895221"))
        self.assertEqual(zlib.crc32(b"abc") & 0xFFFFFFFF, checksum("abc"))
        self.assertEqual(checksum(17), checksum("17"))
        self.assertEqual(0, checksum(""))
        self.cache.set("k", "v", checksum="v6-playlist-19", mem_cache=False)
        self.assertIsNone(self.cache.get("k", checksum="v6-playlist-28", mem_cache=False))

    # -- journal ------------------------------------------------------------

    def test_database_uses_wal_with_normal_sync(self):
        import sqlite3

        self.cache.set("k", "v", mem_cache=False)
        connection = self.cache._get_database()
        try:
            self.assertEqual("wal", connection.execute("PRAGMA journal_mode").fetchone()[0])
            self.assertEqual(1, connection.execute("PRAGMA synchronous").fetchone()[0])
        finally:
            connection.close()
        # a second "process" (independent connection) reads and writes meanwhile
        other = sqlite3.connect(os.path.join(self.tmp, "simplecache.db"), timeout=5)
        try:
            other.execute("BEGIN")
            self.assertEqual(1, other.execute("SELECT COUNT(*) FROM simplecache").fetchone()[0])
            self.cache.set("k2", "v2", mem_cache=False)  # writer not blocked by the reader
            other.execute("COMMIT")
        finally:
            other.close()
        self.assertEqual("v2", self.cache.get("k2", mem_cache=False))

    def _dbfile(self):
        return os.path.join(self.tmp, "simplecache.db")

    def test_plugin_close_skips_wal_checkpoint_while_service_keeps_a_connection(self):
        self.cache.set("k0", "v", mem_cache=False)
        self.assertFalse(os.path.exists(self._dbfile() + "-wal"), "last close checkpoints")
        keepalive = self.simplecache.WalKeepAlive("plugin.test")
        self.assertTrue(keepalive.refresh())
        try:
            self.cache.set("k1", "v", mem_cache=False)
            self.assertTrue(
                os.path.exists(self._dbfile() + "-wal"),
                "with a held connection a plugin close must not checkpoint/delete the WAL",
            )
            # the idle holder must not pin the WAL: automatic checkpoints still run
            blob = "x" * 4000
            for start in range(0, 3000, 100):
                self.cache.set_many(
                    {f"big{i}": blob for i in range(start, start + 100)}, mem_cache=False
                )
            self.assertLess(os.path.getsize(self._dbfile() + "-wal"), 8 * 1024 * 1024)
            self.assertEqual(blob, self.cache.get("big2999", mem_cache=False))
        finally:
            keepalive.close()
        self.assertFalse(os.path.exists(self._dbfile() + "-wal"))

    def test_keepalive_never_closes_a_connection_to_a_replaced_file(self):
        self.cache.set("k", "v", mem_cache=False)
        keepalive = self.simplecache.WalKeepAlive("plugin.test")
        keepalive.refresh()
        old = keepalive._connection
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(self._dbfile() + suffix):
                os.remove(self._dbfile() + suffix)
        self.cache.set("k", "new", mem_cache=False)  # recreated by a plugin
        keepalive.refresh()
        try:
            self.assertIsNot(old, keepalive._connection)
            self.assertIn(old, keepalive._orphaned, "orphaned, not closed")
            self.cache.set("k2", "v2", mem_cache=False)
            self.assertTrue(os.path.exists(self._dbfile() + "-wal"))
            self.assertEqual("new", self.cache.get("k", mem_cache=False))
        finally:
            keepalive.close()

    def test_keepalive_waits_for_the_database_to_exist(self):
        keepalive = self.simplecache.WalKeepAlive("plugin.test")
        if os.path.exists(self._dbfile()):
            os.remove(self._dbfile())
        self.assertFalse(keepalive.refresh())
        self.assertFalse(os.path.exists(self._dbfile()))

    def test_clear_all_empties_table_and_mirrors_but_keeps_file(self):
        self.cache.set("mirrored", "1", checksum="c")
        self.cache.set("dbonly", "1", mem_cache=False)
        self.assertTrue(self.cache.clear_all())
        self.assertTrue(os.path.exists(self._dbfile()))
        self.assertNotIn("mirrored", _Window.props)
        self.assertEqual(0, self._count_rows())

    def test_locked_database_is_not_deleted(self):
        import sqlite3

        self.cache.set("k", "v", mem_cache=False)
        real_connect = self.simplecache.sqlite3.connect

        class _Locked:
            def execute(self, *args):
                raise sqlite3.OperationalError("database is locked")

            def close(self):
                pass

        self.simplecache.sqlite3.connect = lambda *a, **k: _Locked()
        try:
            self.assertIsNone(self.cache._get_database())
        finally:
            self.simplecache.sqlite3.connect = real_connect
        self.assertEqual("v", self.cache.get("k", mem_cache=False))

    def test_corrupt_database_is_recreated_without_stale_wal(self):
        dbfile = os.path.join(self.tmp, "simplecache.db")
        self.cache.set("k", "v", mem_cache=False)
        with open(dbfile, "wb") as handle:
            handle.write(b"not a database" * 100)
        with open(dbfile + "-wal", "wb") as handle:
            handle.write(b"stale wal")
        self.cache.set("k", "v2", mem_cache=False)
        self.assertEqual("v2", self.cache.get("k", mem_cache=False))
        if os.path.exists(dbfile + "-wal"):
            with open(dbfile + "-wal", "rb") as handle:
                self.assertFalse(handle.read().startswith(b"stale wal"))

    def test_many_methods_handle_unavailable_database(self):
        self.cache._get_database = lambda: None
        self.cache.set_many({"k": [1]}, mem_cache=False)
        self.assertEqual({}, self.cache.get_many(["k"], mem_cache=False))
        self.cache.set_many({})


if __name__ == "__main__":
    unittest.main()
