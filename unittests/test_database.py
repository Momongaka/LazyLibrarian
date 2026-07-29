#  This file is part of Lazylibrarian.
#
# Purpose:
#   Testing the database module

import logging
import mock
import sqlite3
import threading
import time

from lazylibrarian.database import DBConnection
from lazylibrarian.dbupgrade import db_upgrade, upgrade_needed
from lazylibrarian.filesystem import DIRS, remove_file
from unittests.unittesthelpers import LLTestCaseWithConfigandDIRS


class DatabaseTest(LLTestCaseWithConfigandDIRS):

    def setUp(self):
        """ Create a new, empty database for testing """
        #dbcommslogger = logging.getLogger('special.dbcomms')
        #dbcommslogger.setLevel(logging.DEBUG)
        DIRS.DBFILENAME = "test-db.db"
        try:  # Make sure the file doesn't exist
            remove_file(DIRS.get_dbfile())
        except FileNotFoundError:
            pass  # Ignore
        curr_ver = upgrade_needed()
        db_upgrade(curr_ver, restartjobs=False)

    def tearDown(self) -> None:
        """ Delete the test database after each test """
        remove_file(DIRS.get_dbfile())

    def test_init(self):
        """ Test initializing the database, which we do all the time """
        db = DBConnection()
        self.assertIsNotNone(db, 'db must not be None')
        db.close()

    def test_connect_100_times(self):
        # Validate that it's a quick operation
        for i in range(100):
            db = DBConnection()
            db.close()
        self.assertTrue(True)

    def test_version_and_integrity(self):
        db = DBConnection()
        result = db.match('PRAGMA user_version')
        self.assertEqual(result[0], 90, 'Unit tests developed for v90; please upgrade')
        check = db.match('PRAGMA integrity_check')
        self.assertEqual('ok', check[0], 'Database integrity check failed')
        db.close()

    @staticmethod
    def get_table_list(db):
        res = db.select("select type, name from sqlite_master where type is 'table'")
        return [table['name'] for table in res]

    @mock.patch.object(DIRS, 'get_dbfile')
    def test_database_does_not_exist(self, mock_getdbfile):
        mock_getdbfile.return_value = 'InvalidDatabaseFile.db'
        db = DBConnection()
        self.assertIsNotNone(db, 'db must not be None')
        tables = self.get_table_list(db)
        self.assertListEqual([], tables, 'Expect new DB to have no tables')
        db.close()

    def test_tables_list(self):
        """ Test that all the expected tables exist """
        expect = ['authors', 'wanted', 'magazines', 'languages', 'stats', 'downloads', 'isbn',
                  'genres', 'sqlite_sequence', 'comics', 'jobs', 'books', 'issues',
                  'sync', 'failedsearch', 'genrebooks', 'comicissues', 'sent_file', 'pastissues',
                  'subscribers', 'unauthorised', 'users', 'readinglists', 'series', 'member',
                  'seriesauthors', 'bookauthors']
        db = DBConnection()
        tables = self.get_table_list(db)
        self.assertListEqual(expect, tables, 'Unexpected table mismatch')
        db.close()

    def test_job_schedule_upsert(self):
        """ Test that the way db.upsert() is used by scheduling is ok """
        db = DBConnection()
        try:
            res = db.match('SELECT Name,Start,Finish from jobs')
            self.assertListEqual([], res, 'Expect no job info in empty DB')
            db.upsert("jobs", {"Start": time.time()}, {"Name": 'Test'})
            time.sleep(0.1)
            db.upsert("jobs", {"Finish": time.time()}, {"Name": 'Test'})

            res = db.match('SELECT Start,Finish from jobs WHERE Name="Test"')
            self.assertIsNotNone(res)
            self.assertGreater(res['Finish'], res['Start'], 'Expect finish time to be later than start!')
        finally:
            db.close()

    def test_db_persistence(self):
        """ Test opening, writing, closing and reopening a database """
        db = DBConnection()
        try:
            res = db.match('SELECT Name,Start,Finish from jobs')
            self.assertListEqual([], res, 'Expect no job info in empty DB')
            db.upsert("jobs", {"Start": time.time()}, {"Name": 'Test'})
            time.sleep(0.01)
            db.upsert("jobs", {"Finish": time.time()}, {"Name": 'Test'})

            res = db.match('SELECT Start,Finish from jobs WHERE Name="Test"')
            self.assertIsNotNone(res)
            self.assertGreater(res['Finish'], res['Start'], 'Expect finish time to be later than start!')

            db.close()
            db = DBConnection()
            res = db.match('SELECT Start,Finish from jobs WHERE Name="Test"')
            self.assertIsNotNone(res)
            self.assertGreater(res['Finish'], res['Start'], 'Expect finish time to be later than start!')
        finally:
            db.close()

    def test_invalid_open_db_file(self):
        """ Test opening a locked database file """
        fname = DIRS.get_dbfile()
        with open(fname, 'w') as f:
            try:
                f.write("I'm not a database file")
                db = DBConnection()
                with self.assertRaises(sqlite3.OperationalError):
                    db.upsert("jobs", {"Start": time.time()}, {"Name": 'Test'})
                db.close()
            finally:
                f.close()

    def test_invalid_disk_db_file(self):
        """ Test opening a locked database file """
        fname = DIRS.get_dbfile()
        with open(fname, 'w') as f:
            f.write("I'm not a database file")
            f.close()
        db = None
        with self.assertRaises(sqlite3.DatabaseError):
            db = DBConnection()
        remove_file(fname)
        db = DBConnection()
        self.assertIsNotNone(db, 'Expect being able to open the DB now')
        db.close()

    def test_uniqueness_error(self):
        """ Test violating a uniqueness constraint """
        db = DBConnection()
        genre = 'test'
        db.action('INSERT into genres (GenreName) VALUES (?)', (genre,))
        match = db.match('SELECT GenreID from genres where GenreName=?', (genre,))
        self.assertIsNotNone(match, 'Expected to get a genreID back')
        id1 = match['GenreID']
        with self.assertRaises(sqlite3.IntegrityError):
            db.action('INSERT into genres (GenreName) VALUES (?)', (genre,))
        match = db.match('SELECT GenreID from genres where GenreName=?', (genre,))
        self.assertEqual(id1, match['GenreID'], 'GenreID should not change')
        # Do the same, but suppress the uniqueness constraint
        db.action('INSERT into genres (GenreName) VALUES (?)', (genre,), suppress='UNIQUE')
        self.assertEqual(id1, match['GenreID'], 'GenreID should not change')
        db.close()

    def test_select_match_error(self):
        """ Test selecting/matching a table that does not exist """
        db = DBConnection()
        res = db.select('select * from unknowntable')
        self.assertEqual([], res, 'SELECT from unknown table should not work')
        res = db.match('select * from unknowntable')
        self.assertEqual([], res, 'match from unknown table should not work')
        db.close()

    def write_to_db(self, index: int, iterations: int):
        """ Write some stuff to the DB """
        db = DBConnection()
        try:
            for i in range(iterations):
                genre = f'Genre #{index}'
                db.action('INSERT into genres (GenreName) VALUES (?)', (genre,), suppress='UNIQUE')
                db.upsert("jobs", {"Start": time.time()}, {"Name": f'Test{index}'})
                time.sleep(0.05)
                db.upsert("jobs", {"Finish": time.time()}, {"Name": f'Test{index}'})
                # Also do some reading
                _ = db.match('SELECT Name,Start,Finish from jobs')
        finally:
            db.close()
        self.assertTrue(True)

    def do_thread_test(self, number_threads: int, iterations: int):
        threads = []
        for i in range(number_threads):
            threads.append(threading.Thread(target=self.write_to_db, args=(i,iterations)))
        for thread in threads:
            thread.start()
            thread.join()

        db = DBConnection()
        try:
            res = db.select('select GenreName from genres')
            self.assertIsNotNone(res, 'Expected genrenames after writing')
            self.assertEqual(number_threads, len(res), f'Expected {number_threads} names after writing')
        finally:
            db.close()

    def test_singlethreaded_writing1x25(self):
        """ Test single-threaded writing to the DB """
        self.do_thread_test(1, 25)

    def test_threaded_writing25x1(self):
        """ Test multi-threaded writing to the DB: 25 threads with one iteration each """
        self.do_thread_test(25, 1)

    def test_threaded_writing5x5(self):
        """ Test multi-threaded writing to the DB: 5 threads, each doing 5 iterations """
        self.do_thread_test(10, 5)

    def test_empty_query(self):
        db = DBConnection()
        res = db.action('')
        self.assertIsNone(res, 'Empty query should return None')
        db.close()





