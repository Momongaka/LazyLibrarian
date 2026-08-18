#  This file is part of Lazylibrarian.
#
# Purpose:
#   Testing functionality in librarysync.py

import os

from lazylibrarian import librarysync
from lazylibrarian.filesystem import get_directory
from unittests.unittesthelpers import LLTestCaseWithConfigandDIRS


class LibrarySyncTest(LLTestCaseWithConfigandDIRS):

    # Initialisation code that needs to run only once
    @classmethod
    def setUpClass(cls) -> None:
        return super().setUpClass()

    def testGetBookInfo_NoExtension(self):
        # Test reading metadata from book files
        self.assertEqual(librarysync.get_book_info("BookWithNoExtension"), {})

    @staticmethod
    def getTestBook(name):
        return get_directory("Testdata") + os.path.sep + name

    def testGetBookInfo_Epub(self):
        expected_meta = {'type': 'epub', 'title': 'Test Title', 'publisher': 'Testing, Inc', 'language': 'en',
                         'isbn': '9782123456803', 'gb_id': '9876542', 'creator': 'Bob Builder', 'authors': ['Bob Builder']}
        bookfile = self.getTestBook('Test Title - Bob Builder.epub')
        self.assertEqual(librarysync.get_book_info(bookfile), expected_meta)

    def testGetBookInfo_Mobi(self):
        bookfile = self.getTestBook('Test Title - Bob Builder.mobi')
        expected_meta = {'type': 'mobi', 'title': 'Test Title', 'creator': 'Bob Builder',
                         'language': 'en', 'isbn': '9782123456803'}
        self.assertEqual(librarysync.get_book_info(bookfile), expected_meta)

    def testGetBookInfo_Azw3(self):
        bookfile = self.getTestBook('Test Title - Bob Builder.azw3')
        expected_meta = {'type': 'azw3', 'title': 'Test Title', 'creator': 'Bob Builder',
                         'language': 'en', 'isbn': '9782123456803'}
        self.assertEqual(librarysync.get_book_info(bookfile), expected_meta)

    def testGetBookInfo_Opf(self):
        bookfile = self.getTestBook('metadata.opf')
        expected_meta = {'type': 'opf', 'title': 'Test Title', 'publisher': 'Testing, Inc', 'language': 'eng',
                         'isbn': '9782123456803', 'gb_id': '9876542', 'creator': 'Bob Builder', 'authors': ['Bob Builder']}
        self.assertEqual(librarysync.get_book_info(bookfile), expected_meta)

    def testLibraryScan(self):
        # Test library_scan, first with an invalid dir:
        self.assertEqual(librarysync.library_scan("Invalid Start Dir"), 0)
