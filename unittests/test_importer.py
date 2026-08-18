#  This file is part of Lazylibrarian.
#
# Purpose:
#   Testing functionality in importer.py
from unittest import mock
from unittest.mock import MagicMock

from lazylibrarian import database, importer
from lazylibrarian.importer import move_book_to_author
from unittests.unittesthelpers import LLTestCaseWithStartup


class ImporterTest(LLTestCaseWithStartup):
    bookapi = ''

    # Initialisation code that needs to run only once
    @classmethod
    def setUpClass(cls) -> None:
        rc = super().setUpClass()
        cls.bookapi = cls.cfg()['BOOK_API']
        return rc

    @classmethod
    def tearDownClass(cls) -> None:
        cls.cfg().set_str('BOOK_API', cls.bookapi)
        return super().tearDownClass()

    def test_is_valid_authorid_InvalidIDs(self):
        # Test blank/empty/non-string IDs
        self.assertEqual(importer.is_valid_authorid(None), False)
        self.assertEqual(importer.is_valid_authorid(0), False)
        self.assertEqual(importer.is_valid_authorid(''), False)
        self.assertEqual(importer.is_valid_authorid(10), False)

    def test_is_valid_authorid_GoogleBooks(self):
        # Test potentially valid Google Books IDs
        self.cfg().set_str('BOOK_API', 'GoogleBooks')
        self.assertEqual(importer.is_valid_authorid('123'), True)
        self.assertEqual(importer.is_valid_authorid('OLrandomA'), True)

    def test_is_valid_authorid_Goodreads(self):
        # Test potentially valid Goodreads Books IDs
        self.cfg().set_str('BOOK_API', 'GoodReads')
        self.assertEqual(importer.is_valid_authorid('123'), True)
        self.assertEqual(importer.is_valid_authorid('OLrandomA'), False)

    def test_is_valid_authorid_OpenLibrary(self):
        # Test potentially valid Goodreads Books IDs
        self.cfg().set_str('BOOK_API', 'OpenLibrary')
        self.assertEqual(importer.is_valid_authorid('123'), False)
        self.assertEqual(importer.is_valid_authorid('OLrandomA'), True)

    def test_get_preferred_author_name_NotInDB(self):
        testname = 'Allan Mertner'
        name, found = importer.get_preferred_author(testname)
        self.assertEqual(name, testname)
        self.assertEqual(found, '')

        longertestname = testname + ' & Someone Else'
        name, found = importer.get_preferred_author(longertestname)
        self.assertEqual(name, testname)
        self.assertEqual(found, '')

    def test_add_author_name_to_db_UnknownPerson(self):
        testname = 'Mr Allan Mertner The Tester'
        authorname, authorid, new = importer.add_author_name_to_db(
            author=testname, refresh=False, addbooks=False, reason='Testing', title=False)
        self.assertEqual(new, False)
        self.assertEqual(authorname, '')

    @mock.patch('lazylibrarian.gr.GoodReads.find_author_id')
    @mock.patch('lazylibrarian.ol.OpenLibrary.find_author_id')
    @mock.patch.object(importer, 'get_author_image')  # Patches images.get_author_image in import only
    def test_add_author_name_to_db_KnownAuthor_OL(self, images_get_author_image: MagicMock,
                                                  ol_find_author_id: MagicMock, gr_find_author_id: MagicMock):
        self.cfg().set_str('BOOK_API', 'OpenLibrary')
        testname = 'Douglas Adams'
        images_get_author_image.return_value = 'douglas.png'
        gr_find_author_id.return_value = {'authorid': 'OL272947A',
                                          'authorlink': 'https://www.openlibrary.org/authors/OL272947A',
                                          'authorimg': 'http://covers.openlibrary.org/a/id/6387387-M.jpg',
                                          'authorborn': '11 March 1952', 'authordeath': '11 May 2001',
                                          'about': "Douglas Adams was born in Cambridge in March 1952.",
                                          'totalbooks': '0', 'authorname': 'Douglas Adams'}
        ol_find_author_id.return_value = gr_find_author_id.return_value
        authorname, authorid, new = importer.add_author_name_to_db(
            author=testname, refresh=False, addbooks=False, reason='Testing', title=False)
        self.assertEqual(new, True)
        self.assertEqual(authorname, testname)
        self.assertEqual(authorid, 'OL272947A')

        # Try re-adding, and see that it's no longer new
        authorname, authorid, new = importer.add_author_name_to_db(
            author=testname, refresh=False, addbooks=False, reason='Testing', title=False)
        self.assertEqual(new, False)
        self.assertEqual(authorname, testname)
        self.assertEqual(authorid, 'OL272947A')

    @mock.patch('lazylibrarian.ol.OpenLibrary.get_author_info')
    @mock.patch.object(importer, 'get_author_image')  # Patches images.get_author_image in import only
    def test_add_author_to_db_JustByID(self, images_get_author_image: MagicMock, ol_get_author_info: MagicMock):
        testid = 'OL2219179A'  # Maud D. Davies
        self.cfg().set_str('BOOK_API', 'OpenLibrary')
        ol_get_author_info.return_value = {'authorid': 'OL2219179A',
                                           'authorlink': 'https://www.openlibrary.org/authors/OL2219179A',
                                           'authorimg': 'images/nophoto.png', 'authorborn': '', 'authordeath': '',
                                           'about': '', 'totalbooks': '0', 'authorname': 'Maud D. Davies'}
        images_get_author_image.return_value = 'fakeimage.png'
        authorid = importer.add_author_to_db(
            authorname=None, refresh=False, addbooks=False, reason='Testing', authorid=testid)
        self.assertEqual(authorid, testid)


class MoveBookToAuthorTest(LLTestCaseWithStartup):

    def _setup_data(self):
        db = database.DBConnection()
        db.action("INSERT OR REPLACE INTO authors (AuthorID, AuthorName, Status) VALUES (?, ?, ?)",
                  ('OLD_AUTH', 'Old Author', 'Active'))
        db.action("INSERT OR REPLACE INTO authors (AuthorID, AuthorName, Status) VALUES (?, ?, ?)",
                  ('NEW_AUTH', 'New Author', 'Active'))
        db.action("INSERT OR REPLACE INTO books (BookID, AuthorID, BookName, Status, AudioStatus) "
                  "VALUES (?, ?, ?, ?, ?)", ('BOOK1', 'OLD_AUTH', 'Test Book', 'Have', 'Wanted'))
        db.action("INSERT INTO bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)",
                  ('OLD_AUTH', 'BOOK1', 1), suppress='UNIQUE')
        return db

    def test_books_table_updated(self):
        db = self._setup_data()
        try:
            move_book_to_author('BOOK1', 'OLD_AUTH', 'NEW_AUTH')
            row = db.match('SELECT AuthorID FROM books WHERE BookID=?', ('BOOK1',))
            self.assertEqual(row['AuthorID'], 'NEW_AUTH')
        finally:
            db.close()

    def test_bookauthors_updated(self):
        db = self._setup_data()
        try:
            move_book_to_author('BOOK1', 'OLD_AUTH', 'NEW_AUTH')
            new_link = db.match('SELECT Role FROM bookauthors WHERE BookID=? AND AuthorID=?',
                                ('BOOK1', 'NEW_AUTH'))
            self.assertIsNotNone(new_link, "New author should have a bookauthors entry")
            old_link = db.match('SELECT Role FROM bookauthors WHERE BookID=? AND AuthorID=?',
                                ('BOOK1', 'OLD_AUTH'))
            self.assertFalse(old_link, "Old author should lose bookauthors entry when they have no other books")
        finally:
            db.close()

    def test_old_author_keeps_bookauthors_for_other_books(self):
        db = self._setup_data()
        db.action("INSERT OR REPLACE INTO books (BookID, AuthorID, BookName, Status, AudioStatus) "
                  "VALUES (?, ?, ?, ?, ?)", ('BOOK2', 'OLD_AUTH', 'Other Book', 'Have', 'Have'))
        db.action("INSERT INTO bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)",
                  ('OLD_AUTH', 'BOOK2', 1), suppress='UNIQUE')
        try:
            move_book_to_author('BOOK1', 'OLD_AUTH', 'NEW_AUTH')
            old_link = db.match('SELECT Role FROM bookauthors WHERE BookID=? AND AuthorID=?',
                                ('BOOK1', 'OLD_AUTH'))
            self.assertFalse(old_link, "Old author loses link for the moved book")
            other_link = db.match('SELECT Role FROM bookauthors WHERE BookID=? AND AuthorID=?',
                                  ('BOOK2', 'OLD_AUTH'))
            self.assertIsNotNone(other_link, "Old author keeps link for their other book")
        finally:
            db.close()

    def test_seriesauthors_updated(self):
        db = self._setup_data()
        db.action("INSERT OR REPLACE INTO series (SeriesID, SeriesName, Status) VALUES (?, ?, ?)",
                  ('SER1', 'Test Series', 'Active'))
        db.action("INSERT OR REPLACE INTO member (SeriesID, BookID) VALUES (?, ?)",
                  ('SER1', 'BOOK1'))
        db.action("INSERT INTO seriesauthors (SeriesID, AuthorID) VALUES (?, ?)",
                  ('SER1', 'OLD_AUTH'), suppress='UNIQUE')
        try:
            move_book_to_author('BOOK1', 'OLD_AUTH', 'NEW_AUTH')
            new_sa = db.match('SELECT * FROM seriesauthors WHERE SeriesID=? AND AuthorID=?',
                              ('SER1', 'NEW_AUTH'))
            self.assertIsNotNone(new_sa, "New author should be linked to the series")
            old_sa = db.match('SELECT * FROM seriesauthors WHERE SeriesID=? AND AuthorID=?',
                              ('SER1', 'OLD_AUTH'))
            self.assertFalse(old_sa, "Old author should lose series link when they have no other books in it")
        finally:
            db.close()

    def test_old_author_keeps_series_with_other_books(self):
        db = self._setup_data()
        db.action("INSERT OR REPLACE INTO books (BookID, AuthorID, BookName, Status, AudioStatus) "
                  "VALUES (?, ?, ?, ?, ?)", ('BOOK2', 'OLD_AUTH', 'Other Book', 'Have', 'Have'))
        db.action("INSERT INTO bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)",
                  ('OLD_AUTH', 'BOOK2', 1), suppress='UNIQUE')
        db.action("INSERT OR REPLACE INTO series (SeriesID, SeriesName, Status) VALUES (?, ?, ?)",
                  ('SER1', 'Test Series', 'Active'))
        db.action("INSERT OR REPLACE INTO member (SeriesID, BookID) VALUES (?, ?)",
                  ('SER1', 'BOOK1'))
        db.action("INSERT OR REPLACE INTO member (SeriesID, BookID) VALUES (?, ?)",
                  ('SER1', 'BOOK2'))
        db.action("INSERT INTO seriesauthors (SeriesID, AuthorID) VALUES (?, ?)",
                  ('SER1', 'OLD_AUTH'), suppress='UNIQUE')
        try:
            move_book_to_author('BOOK1', 'OLD_AUTH', 'NEW_AUTH')
            old_sa = db.match('SELECT * FROM seriesauthors WHERE SeriesID=? AND AuthorID=?',
                              ('SER1', 'OLD_AUTH'))
            self.assertIsNotNone(old_sa, "Old author keeps series link when they have another book in it")
        finally:
            db.close()

    def test_totals_recomputed(self):
        db = self._setup_data()
        try:
            move_book_to_author('BOOK1', 'OLD_AUTH', 'NEW_AUTH')
            old_auth = db.match('SELECT TotalBooks, HaveBooks FROM authors WHERE AuthorID=?', ('OLD_AUTH',))
            new_auth = db.match('SELECT TotalBooks, HaveBooks FROM authors WHERE AuthorID=?', ('NEW_AUTH',))
            self.assertEqual(old_auth['TotalBooks'], 0, "Old author should have 0 books after move")
            self.assertEqual(new_auth['TotalBooks'], 1, "New author should have 1 book after move")
        finally:
            db.close()
