#!/usr/bin/python
# -*- coding: utf-8 -*-

'''provides a simple stateless caching system for Kodi addons and plugins'''

import sys
import xbmcvfs
import xbmcgui
import xbmc
import xbmcaddon
import datetime
import time
import sqlite3
import json
from functools import reduce

class _SqlResult(object):
    '''rows fetched before the connection is closed (cursor-like API)'''

    def __init__(self, rows):
        self._rows = list(rows or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class SimpleCache(object):
    '''simple stateless caching system for Kodi'''
    enable_mem_cache = True
    data_is_json = False
    global_checksum = None
    _exit = False
    _auto_clean_interval = datetime.timedelta(hours=4)
    _win = None
    _busy_tasks = []
    _database = None

    def __init__(self, addon_id):
        '''Initialize our caching class'''
        self.addon_id = addon_id
        self._win = xbmcgui.Window(10000)
        self._monitor = xbmc.Monitor()
        self.check_cleanup()
        self._log_msg("Initialized")

    def close(self):
        '''tell any tasks to stop immediately (as we can be called multithreaded) and cleanup objects'''
        self._exit = True
        # wait for all tasks to complete
        while self._busy_tasks and not self._monitor.abortRequested():
            xbmc.sleep(25)
        del self._win
        del self._monitor
        self._log_msg("Closed")

    def __del__(self):
        '''make sure close is called'''
        if not self._exit:
            self.close()

    def get(self, endpoint, checksum="", json_data=False, mem_cache=True):
        '''
            get object from cache and return the results
            endpoint: the (unique) name of the cache object as reference
            checkum: optional argument to check if the checksum in the cacheobject matches the checkum provided
            mem_cache: False keeps this entry database-only (no window property copy),
                       for large collections that would otherwise be serialised twice
        '''
        checksum = self._get_checksum(checksum)
        cur_time = self._get_timestamp(datetime.datetime.now())
        result = None
        use_mem = self.enable_mem_cache and mem_cache
        # 1: try memory cache first
        if use_mem:
            result = self._get_mem_cache(endpoint, checksum, cur_time, json_data)

        # 2: fallback to _database cache
        if result is None:
            result = self._get_db_cache(endpoint, checksum, cur_time, json_data, use_mem)

        return result

    def set(self, endpoint, data, checksum="", expiration=datetime.timedelta(days=30), json_data=False,
            mem_cache=True):
        '''
            set data in cache
        '''
        task_name = "set.%s" % endpoint
        self._busy_tasks.append(task_name)
        checksum = self._get_checksum(checksum)
        expires = self._get_timestamp(datetime.datetime.now() + expiration)

        # memory cache: write to window property
        if self.enable_mem_cache and mem_cache and not self._exit:
            self._set_mem_cache(endpoint, checksum, expires, data, json_data)
        elif not mem_cache and not self._exit:
            # database-only entry: drop any stale window-property copy
            self._win.clearProperty(endpoint)

        # db cache
        if not self._exit:
            self._set_db_cache(endpoint, checksum, expires, data, json_data)

        # remove this task from list
        self._busy_tasks.remove(task_name)

    def check_cleanup(self):
        '''check if cleanup is needed - public method, may be called by calling addon'''
        cur_time = datetime.datetime.now()
        lastexecuted = self._win.getProperty("simplecache.clean.lastexecuted")
        if not lastexecuted:
            self._win.setProperty("simplecache.clean.lastexecuted", cur_time.isoformat())
        else:
            try:
                last_dt = datetime.datetime.fromisoformat(lastexecuted)
            except (ValueError, TypeError):
                last_dt = cur_time
                self._win.setProperty("simplecache.clean.lastexecuted", cur_time.isoformat())
            if (last_dt + self._auto_clean_interval) < cur_time:
                self._do_cleanup()

    def _get_mem_cache(self, endpoint, checksum, cur_time, json_data):
        '''
            get cache data from memory cache
            we use window properties because we need to be stateless
        '''
        result = None
        cachedata = self._win.getProperty(endpoint)

        if cachedata:
            try:
                cachedata = json.loads(cachedata)
            except (json.JSONDecodeError, TypeError, ValueError):
                self._win.clearProperty(endpoint)
                return None
            if not isinstance(cachedata, (list, tuple)) or len(cachedata) < 3:
                self._win.clearProperty(endpoint)
                return None
            if cachedata[0] > cur_time:
                if not checksum or checksum == cachedata[2]:
                    result = cachedata[1]
        return result

    def _set_mem_cache(self, endpoint, checksum, expires, data, json_data):
        '''
            window property cache as alternative for memory cache
            usefull for (stateless) plugins
        '''
        cachedata = (expires, data, checksum)
        cachedata_str = json.dumps(cachedata)
        self._win.setProperty(endpoint, cachedata_str)


    def _get_db_cache(self, endpoint, checksum, cur_time, json_data, use_mem=True):
        '''get cache data from sqllite _database'''
        result = None
        query = "SELECT expires, data, checksum FROM simplecache WHERE id = ?"
        cache_data = self._execute_sql(query, (endpoint,))
        if cache_data:
            cache_data = cache_data.fetchone()
            if cache_data and cache_data[0] > cur_time:
                if not checksum or cache_data[2] == checksum:
                    try:
                        result = json.loads(cache_data[1])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        self._execute_sql(
                            "DELETE FROM simplecache WHERE id = ?", (endpoint,)
                        )
                        return None
                    if self.enable_mem_cache and use_mem:
                        self._set_mem_cache(endpoint, checksum, cache_data[0], result, json_data)
        return result

    def _set_db_cache(self, endpoint, checksum, expires, data, json_data):
        ''' store cache data in _database '''
        query = "INSERT OR REPLACE INTO simplecache( id, expires, data, checksum) VALUES (?, ?, ?, ?)"
        data = json.dumps(data)
        self._execute_sql(query, (endpoint, expires, data, checksum))

    def _do_cleanup(self):
        '''perform cleanup task'''
        if self._exit or self._monitor.abortRequested():
            return
        self._busy_tasks.append(__name__)
        cur_time = datetime.datetime.now()
        cur_timestamp = self._get_timestamp(cur_time)
        self._log_msg("Running cleanup...")
        if self._win.getProperty("simplecachecleanbusy"):
            return
        self._win.setProperty("simplecachecleanbusy", "busy")

        query = "SELECT id, expires FROM simplecache"
        rows = self._execute_sql(query)
        for cache_data in (rows.fetchall() if rows else []):
            cache_id = cache_data[0]
            cache_expires = cache_data[1]

            if self._exit or self._monitor.abortRequested():
                return

            # always cleanup all memory objects on each interval
            self._win.clearProperty(cache_id)

            # clean up db cache object only if expired
            if cache_expires < cur_timestamp:
                query = 'DELETE FROM simplecache WHERE id = ?'
                self._execute_sql(query, (cache_id,))
                self._log_msg("delete from db %s" % cache_id)

        # compact db
        self._execute_sql("VACUUM")

        # remove task from list
        self._busy_tasks.remove(__name__)
        self._win.setProperty("simplecache.clean.lastexecuted", cur_time.isoformat())
        self._win.clearProperty("simplecachecleanbusy")
        self._log_msg("Auto cleanup done")

    def _get_database(self):
        '''get reference to our sqllite _database - performs basic integrity check'''
        addon = xbmcaddon.Addon(self.addon_id)
        dbpath = addon.getAddonInfo('profile')
        dbfile = xbmcvfs.translatePath("%s/simplecache.db" % dbpath)

        if not xbmcvfs.exists(dbpath):
            xbmcvfs.mkdirs(dbpath)
        del addon
        connection = None
        try:
            connection = sqlite3.connect(dbfile, timeout=30, isolation_level=None)
            connection.execute('SELECT * FROM simplecache LIMIT 1')
            return connection
        except Exception as error:
            # our _database is corrupt or doesn't exist yet, we simply try to recreate it
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            if xbmcvfs.exists(dbfile):
                xbmcvfs.delete(dbfile)
            try:
                connection = sqlite3.connect(dbfile, timeout=30, isolation_level=None)
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS simplecache(
                    id TEXT UNIQUE, expires INTEGER, data TEXT, checksum INTEGER)""")
                return connection
            except Exception as error:
                self._log_msg("Exception while initializing _database: %s" % str(error), xbmc.LOGWARNING)
                self.close()
                return None

    def _execute_sql(self, query, data=None):
        '''little wrapper around execute and executemany to just retry a db command if db is locked

        Returns a cursor-like object whose rows were fetched before the
        connection was closed, or None on failure.
        '''
        retries = 0
        error = None
        # always use new db object because we need to be sure that data is available for other simplecache instances
        _database = self._get_database()
        if _database is None:
            self._log_msg("_database unavailable; skipping query", xbmc.LOGWARNING)
            return None
        try:
            while not retries == 10 and not self._monitor.abortRequested():
                if self._exit:
                    return None
                try:
                    if isinstance(data, list):
                        cursor = _database.executemany(query, data)
                    elif data:
                        cursor = _database.execute(query, data)
                    else:
                        cursor = _database.execute(query)
                    return _SqlResult(cursor.fetchall())
                except sqlite3.OperationalError as exc:
                    error = exc
                    if "locked" in str(exc):
                        self._log_msg("retrying DB commit...")
                        retries += 1
                        self._monitor.waitForAbort(0.5)
                    else:
                        break
                except Exception as exc:
                    error = exc
                    break
            self._log_msg("_database ERROR ! -- %s" % str(error), xbmc.LOGWARNING)
        finally:
            try:
                _database.close()
            except Exception:
                pass
        return None

    @staticmethod
    def _log_msg(msg, loglevel=xbmc.LOGDEBUG):
        '''helper to send a message to the kodi log'''
        xbmc.log("Skin Helper Simplecache --> %s" % msg, level=loglevel)

    @staticmethod
    def _get_timestamp(date_time):
        '''Converts a datetime object to unix timestamp'''
        return int(time.mktime(date_time.timetuple()))

    def _get_checksum(self, stringinput):
        '''get int checksum from string'''
        if not stringinput and not self.global_checksum:
            return 0
        if self.global_checksum:
            stringinput = "%s-%s" %(self.global_checksum, stringinput)
        else:
            stringinput = str(stringinput)
        return reduce(lambda x, y: x + y, map(ord, stringinput))


def use_cache(cache_days=14):
    '''
        wrapper around our simple cache to use as decorator
        Usage: define an instance of SimpleCache with name "cache" (self.cache) in your class
        Any method that needs caching just add @use_cache as decorator
        NOTE: use unnamed arguments for calling the method and named arguments for optional settings
    '''
    def decorator(func):
        '''our decorator'''
        def decorated(*args, **kwargs):
            '''process the original method and apply caching of the results'''
            method_class = args[0]
            method_class_name = method_class.__class__.__name__
            cache_str = "%s.%s" % (method_class_name, func.__name__)
            # cache identifier is based on positional args only
            # named args are considered optional and ignored
            for item in args[1:]:
                cache_str += u".%s" % item
            cache_str = cache_str.lower()
            cachedata = method_class.cache.get(cache_str)
            global_cache_ignore = False
            try:
                global_cache_ignore = method_class.ignore_cache
            except Exception:
                pass
            if cachedata is not None and not kwargs.get("ignore_cache", False) and not global_cache_ignore:
                return cachedata
            else:
                result = func(*args, **kwargs)
                method_class.cache.set(cache_str, result, expiration=datetime.timedelta(days=cache_days))
                return result
        return decorated
    return decorator
