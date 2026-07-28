#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test the content checks a download has to pass before it is accepted,
#   in particular which files in a torrent get a say in what format it is.

import unittest
from unittest import mock

from lazylibrarian import download_client
from lazylibrarian.download_client import check_contents, is_ancillary_file
from unittests.unittesthelpers import LLTestCase

EBOOK_CONFIG = {
    'BANNED_EXT': 'exe, bat, iso',
    'EBOOK_TYPE': 'epub, mobi, pdf',
    'AUDIOBOOK_TYPE': 'mp3, m4b',
    'REJECT_WORDS': 'audiobook, mp3',
    'REJECT_AUDIO': 'epub, mobi',
    'REJECT_MAXSIZE': 0,
    'REJECT_MINSIZE': 0,
    'REJECT_MAXAUDIO': 0,
}


class FakeConfig:
    def __init__(self, **overrides):
        self.values = dict(EBOOK_CONFIG)
        self.values.update(overrides)

    def __getitem__(self, key):
        return self.values.get(key, '')

    def get_int(self, key):
        return int(self.values.get(key, 0))


def _files(*names):
    return [{'name': name, 'size': 2000000} for name in names]


class IsAncillaryFileTest(LLTestCase):

    def test_release_notes_and_adverts(self):
        for name in ['free audiobook version.txt', 'release/readme.nfo', 'Downloaded from x.url',
                     'cover.jpg', 'files.sfv', 'README']:
            self.assertTrue(is_ancillary_file(name, 'epub, mobi, pdf'), name)

    def test_content_files(self):
        for name in ['book.epub', 'release/book.pdf', 'chapter 1.mp3', 'audiobook.m4b']:
            self.assertFalse(is_ancillary_file(name, 'epub, mobi, pdf'), name)

    def test_a_wanted_type_is_never_ancillary(self):
        self.assertFalse(is_ancillary_file('book.txt', 'epub, txt'))
        self.assertTrue(is_ancillary_file('book.txt', 'epub, mobi'))


class CheckContentsTest(LLTestCase):

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        self.files = mock.patch.object(download_client, 'get_download_files').start()
        mock.patch.object(download_client, 'CONFIG', FakeConfig()).start()
        self.addCleanup(mock.patch.stopall)

    def check(self, files, booktype='ebook'):
        self.files.return_value = files
        return check_contents('QBITTORRENT', 'deadbeef', booktype, 'a release')

    def test_ebook_with_an_advert_for_the_audiobook_is_accepted(self):
        # the file that broke this: an epub release carrying a text file that
        # mentions an audiobook is still an epub release
        rejected = self.check(_files('The Book/The Book.epub',
                                      'The Book/free audiobook version.txt'))
        self.assertEqual(rejected, '')

    def test_ebook_with_ancillary_junk_is_accepted(self):
        rejected = self.check(_files('The Book/The Book.epub',
                                      'The Book/mp3 audiobook also available.nfo',
                                      'The Book/Get the audiobook.url',
                                      'The Book/cover.jpg'))
        self.assertEqual(rejected, '')

    def test_audiobook_only_release_is_still_rejected_for_an_ebook(self):
        rejected = self.check(_files('The Book/01 chapter.mp3', 'The Book/02 chapter.mp3'))
        self.assertTrue(rejected)

    def test_ebook_named_as_an_audiobook_is_still_rejected(self):
        # the banned word is in the name of a file we would actually process
        rejected = self.check(_files('The Book/The Book audiobook.epub'))
        self.assertTrue(rejected)

    def test_release_folder_named_as_an_audiobook_is_still_rejected(self):
        rejected = self.check(_files('The Book audiobook/The Book.epub',
                                      'The Book audiobook/notes.txt'))
        self.assertTrue(rejected)

    def test_ebook_in_an_audiobook_release_is_still_rejected(self):
        rejected = self.check(_files('The Book/The Book.epub', 'The Book/notes.txt'),
                              booktype='audiobook')
        self.assertTrue(rejected)

    def test_audiobook_with_an_ebook_advert_is_accepted(self):
        rejected = self.check(_files('The Book/01 chapter.mp3',
                                      'The Book/epub version inside.txt'),
                              booktype='audiobook')
        self.assertEqual(rejected, '')

    def test_banned_extension_is_still_rejected_on_an_ancillary_file(self):
        rejected = self.check(_files('The Book/The Book.epub', 'The Book/setup.exe'))
        self.assertIn('exe', rejected)

    def test_configured_text_book_type_is_still_word_checked(self):
        with mock.patch.object(download_client, 'CONFIG', FakeConfig(EBOOK_TYPE='epub, txt')):
            rejected = self.check(_files('The Book/free audiobook version.txt'))
        self.assertTrue(rejected)

    def test_oversized_file_is_still_rejected(self):
        with mock.patch.object(download_client, 'CONFIG', FakeConfig(REJECT_MAXSIZE=1)):
            rejected = self.check(_files('The Book/The Book.epub'))
        self.assertIn('too large', rejected)


if __name__ == '__main__':
    unittest.main()
