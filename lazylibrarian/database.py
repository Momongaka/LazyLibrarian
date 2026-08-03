#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.


import logging
import os
import sqlite3
import threading
import time
import traceback

# DO NOT import from common in this module, circular import
from lazylibrarian.filesystem import DIRS
from lazylibrarian.processcontrol import get_info_on_caller

db_lock = threading.Lock()


class DBConnection:
    def __init__(self):
        try:
            self.connection = sqlite3.connect(DIRS.get_dbfile(), timeout=20)
            # Use write-ahead logging to do fewer disk writes
            self.connection.execute("PRAGMA journal_mode = WAL")
            # sync less often as using WAL mode
            self.connection.execute("PRAGMA synchronous = NORMAL")
            # 32,384 pages of cache
            self.connection.execute(f"PRAGMA cache_size=-{32 * 1024}")
            # for cascade deletes
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA temp_store = 2")  # memory
            self.connection.row_factory = sqlite3.Row
            try:
                self.dblog = DIRS.get_logfile('database.log')
            except Exception:
                self.dblog = DIRS.ensure_data_subdir('Logs')
                self.dblog = os.path.join(self.dblog, 'database.log')
            self.logger = logging.getLogger(__name__)
            self.dbcommslogger = logging.getLogger('special.dbcomms')
            self.dbcommslogger.debug('open')
            self.threadname = threading.current_thread().name
            self.threadid = threading.get_ident()  # native_id is in Python 3.8+
            self.opened = 1
        except Exception as e:
            logger = logging.getLogger(__name__)
            logger.warning(f"Database connection failed: {e}")
            logger.warning(f"Database file: {DIRS.get_dbfile()}")
            if getattr(self, 'connection', None):
                self.connection.close()
            raise

    def __del__(self):
        if hasattr(self, 'opened') and self.opened > 0:
            # If not, the DB object was partially initialised and isn't valud
            self.close()

    def close(self):
        self.dbcommslogger.debug('close')
        with db_lock:
            self.opened -= 1
            try:
                if self.threadid != threading.get_ident():
                    # Don't attempt to close; an error will be thrown
                    self.logger.error(f'The wrong thread is closing the db connection: {self.threadname}, '
                                      f'opened by {threading.current_thread().name}')
                else:
                    self.connection.close()
            except sqlite3.ProgrammingError as e:
                self.logger.error(f'Error closing database, {str(e)}')

    def commit(self):
        self.dbcommslogger.debug('commit')
        with db_lock:
            self.connection.commit()

    # wrapper function with lock
    def action(self, query: str, args=None, suppress=None):
        if not query:
            return None
        with db_lock:
            return self._action(query, args, suppress)

    def progress(self, status, remaining, total):
        self.dbcommslogger.debug(f'Copied {total-remaining} of {total} pages...')

    def backup(self):
        filename = f"lazylibrarian_{time.asctime().replace(' ', '_').replace(':', '_')}.db"
        filename = os.path.join(DIRS.DATADIR, filename)
        _ = self.connection.execute('vacuum')
        bck = sqlite3.connect(filename)
        msg = ''
        with db_lock:
            try:
                self.connection.backup(bck, pages=1, progress=self.progress)
            except Exception as err:
                msg = str(err)
        bck.close()
        if msg:
            return '', msg
        return filename, msg

    # do not use directly, use through action() or upsert() which add lock
    def _action(self, query: str, args=None, suppress=None):
        sql_result = None
        attempt = 0
        start = time.time()

        if '"' in query:
            program, method, lineno = get_info_on_caller(depth=2)
            self.logger.warning(f'Malformed Query: {query}: {program}, {method}, {lineno}')
            query = query.replace('"', "'")

        while attempt < 5:
            try:
                if not args:
                    # context manager adds commit() on success or rollback() on exception
                    with self.connection:
                        sql_result = self.connection.execute(query)
                else:
                    with self.connection:
                        sql_result = self.connection.execute(query, args)

                elapsed = time.time() - start
                self.dbcommslogger.debug(f'#{attempt} {elapsed:.4f} {query} [{args}]')
                break

            except sqlite3.OperationalError as e:
                if "unable to open database file" in str(e) or "database is locked" in str(e):
                    elapsed = time.time() - start
                    self.dbcommslogger.debug(f'#{attempt} {elapsed:.4f} {query} [{args}]')
                    self.dbcommslogger.debug(f'Database Error {str(e)}')

                    self.logger.warning(f'Database Error: {e}')
                    self.logger.error(f"Failed db query: [{query}]")
                    time.sleep(1)
                else:
                    elapsed = time.time() - start
                    self.dbcommslogger.debug(f'#{attempt} {elapsed:.4f} {query} [{args}]')
                    self.dbcommslogger.debug(f'Database OperationalError {str(e)}')

                    self.logger.error(f'Database OperationalError: {e}')
                    self.logger.error(f"Failed query: [{query}]")
                    raise

            except sqlite3.IntegrityError as e:
                # we could ignore unique errors in sqlite by using "insert or ignore into" statements
                # but this would also ignore null values as we can't specify which errors to ignore :-(
                # Also the python interface to sqlite only returns english text messages, not error codes
                elapsed = time.time() - start
                msg = str(e).lower()
                if suppress and 'UNIQUE' in suppress and ('not unique' in msg or 'unique constraint failed' in msg):
                    self.dbcommslogger.debug(f'#{attempt} {elapsed:.4f} {query} [{args}]')
                    self.dbcommslogger.debug(f'Suppressed {msg}')
                    self.connection.commit()
                    break
                self.dbcommslogger.debug(f'#{attempt} {elapsed:.4f} {query} [{args}]')
                self.dbcommslogger.debug(f'IntegrityError: {msg}')

                self.logger.error(f'Database IntegrityError: {e}')
                self.logger.error(f"Failed query: [{query}]")
                self.logger.error(f"Failed args: [{str(args)}]")
                raise

            except sqlite3.DatabaseError as e:
                elapsed = time.time() - start
                self.dbcommslogger.debug(f'#{attempt} {elapsed:.4f} {query} [{args}]')
                self.dbcommslogger.debug(f'DatabaseError: {str(e)}')

                self.logger.error(f'Fatal error executing {query} :{args}: {e}')
                self.logger.error(f"{traceback.format_exc()}")
                raise

            except Exception as e:
                elapsed = time.time() - start
                self.dbcommslogger.debug(f'#{attempt} {elapsed:.4f} {query} [{args}]')
                self.dbcommslogger.debug(f'CatchallError: {str(e)}')

                self.logger.error(f'Exception executing {query} :: {e}')
                raise

            finally:
                attempt += 1

        return sql_result

    def match(self, query, args=None):
        try:
            # if there are no results, action() returns None and .fetchone() fails
            sql_results = self.action(query, args).fetchone()
        except sqlite3.Error:
            return []
        if not sql_results:
            return []

        return sql_results

    def select(self, query, args=None):
        try:
            # if there are no results, action() returns None and .fetchall() fails
            sql_results = self.action(query, args).fetchall()
        except sqlite3.Error:
            return []
        if not sql_results:
            return []

        return sql_results

    @staticmethod
    def gen_params(my_dict):
        return [f"{x} = ?" for x in list(my_dict.keys())]

    def upsert(self, table_name, value_dict, key_dict):
        with db_lock:
            changes_before = self.connection.total_changes

            query = (f"UPDATE {table_name} SET {', '.join(self.gen_params(value_dict))} "
                     f"WHERE {' AND '.join(self.gen_params(key_dict))}")

            self._action(query, list(value_dict.values()) + list(key_dict.values()))

            # This version of upsert is not thread safe, each action() is thread safe,
            # but it's possible for another thread to jump in between the
            # UPDATE and INSERT statements, so we use suppress=unique to log any conflicts
            # -- update -- should be thread safe now, threading lock moved

            if self.connection.total_changes == changes_before:
                query = f"INSERT INTO {table_name} ("
                query += f"{', '.join(list(value_dict.keys()) + list(key_dict.keys()))}) VALUES ("
                query += f"{', '.join(['?'] * len(list(value_dict.keys()) + list(key_dict.keys())))})"
                self._action(query, list(value_dict.values()) + list(key_dict.values()), suppress="UNIQUE")
