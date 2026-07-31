#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test the content checks a download has to pass before it is accepted,
#   in particular which files in a torrent get a say in what format it is.
#   What we are allowed to delete afterwards is in test_download_ownership.

import unittest
from unittest import mock

from lazylibrarian import download_client
from lazylibrarian.download_client import check_contents, is_archive_file
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


class IsArchiveFileTest(LLTestCase):

    def test_archives(self):
        for name in ['The Book.rar', 'The Book.part1.rar', 'The Book.r01',
                     'The Book.zip', 'The Book.7z', 'The Book.7z.001', 'The Book.tar.gz']:
            self.assertTrue(is_archive_file(name), name)

    def test_not_archives(self):
        for name in ['The Book.epub', 'The Book.m4b', 'notes.txt', 'The Book']:
            self.assertFalse(is_archive_file(name), name)


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

    def test_audiobook_only_release_is_rejected_for_an_ebook(self):
        # the reported failure: an m4b release matched an ebook search, was
        # accepted because no reject word happened to be in any filename, and
        # only fell over at postprocessing time
        rejected = self.check(_files('The Book/The Book.m4b'))
        self.assertIn('no ebook files', rejected)
        self.assertIn('m4b', rejected)

    def test_ebook_only_release_is_rejected_for_an_audiobook(self):
        # azw3 is not in the audiobook reject words either, so the file list is
        # the only thing that can catch this
        rejected = self.check(_files('The Book/The Book.azw3'), booktype='audiobook')
        self.assertIn('no audiobook files', rejected)

    def test_ebook_named_as_an_audiobook_is_still_rejected(self):
        # the banned word is in the name of a file we would actually process
        rejected = self.check(_files('The Book/The Book audiobook.epub'))
        self.assertIn('contains audiobook', rejected)

    def test_release_folder_named_as_an_audiobook_is_still_rejected(self):
        rejected = self.check(_files('The Book audiobook/The Book.epub',
                                     'The Book audiobook/notes.txt'))
        self.assertIn('contains audiobook', rejected)

    def test_ebook_in_an_audiobook_release_is_still_rejected(self):
        rejected = self.check(_files('The Book/The Book.epub', 'The Book/notes.txt'),
                              booktype='audiobook')
        self.assertTrue(rejected)

    def test_audiobook_with_an_ebook_advert_is_accepted(self):
        rejected = self.check(_files('The Book/01 chapter.mp3',
                                     'The Book/epub version inside.txt'),
                              booktype='audiobook')
        self.assertEqual(rejected, '')

    def test_a_release_holding_both_formats_suits_either_library(self):
        both = _files('The Book/The Book.epub', 'The Book/The Book.m4b')
        self.assertEqual(self.check(both), '')
        self.assertEqual(self.check(both, booktype='audiobook'), '')

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

    def test_an_empty_file_list_is_not_a_wrong_format(self):
        # a magnet whose metadata hasn't arrived yet tells us nothing, and the
        # downloaders that never give us a file list tell us nothing either
        self.assertEqual(self.check([]), '')
        self.assertEqual(self.check(''), '')

    def test_an_archive_is_not_judged_before_it_is_unpacked(self):
        self.assertEqual(self.check(_files('The Book/The Book.rar',
                                           'The Book/The Book.r01')), '')

    def test_an_archive_named_as_an_audiobook_is_rejected(self):
        rejected = self.check(_files('The Book/The Book audiobook.rar'))
        self.assertIn('contains audiobook', rejected)

    def test_sizes_reported_in_kilobytes_are_converted(self):
        # the multiplier was inside the float() call, so the digits were
        # repeated 1024 times instead and the size check was skipped or blew up
        with mock.patch.object(download_client, 'CONFIG', FakeConfig(REJECT_MAXSIZE=1)):
            self.files.return_value = [{'name': 'The Book/The Book.epub', 'size': '4096K'}]
            rejected = check_contents('QBITTORRENT', 'deadbeef', 'ebook', 'a release')
        self.assertIn('too large', rejected)

    def test_m4a_is_accepted_when_it_is_a_configured_audiobook_type(self):
        with mock.patch.object(download_client, 'CONFIG',
                               FakeConfig(AUDIOBOOK_TYPE='mp3, m4b, m4a', REJECT_AUDIO='')):
            rejected = self.check(_files('The Book/The Book.m4a'), booktype='audiobook')
        self.assertEqual(rejected, '')

    def test_m4a_is_rejected_when_it_is_not_a_configured_audiobook_type(self):
        # the shipped default is 'mp3, m4b', so an m4a release is a
        # configuration question rather than anything wrong with the matching
        rejected = self.check(_files('The Book/The Book.m4a'), booktype='audiobook')
        self.assertIn('no audiobook files', rejected)
        self.assertIn('m4a', rejected)

    def test_extensions_match_whatever_case_they_arrive_in(self):
        with mock.patch.object(download_client, 'CONFIG',
                               FakeConfig(AUDIOBOOK_TYPE='mp3, m4b, m4a', REJECT_AUDIO='')):
            rejected = self.check(_files('The Book/The Book.M4A'), booktype='audiobook')
        self.assertEqual(rejected, '')

    def test_files_without_sizes_are_still_matched(self):
        # rtorrent and some deluge versions report a file list with no sizes
        self.files.return_value = [{'name': 'The Book/The Book.epub'}]
        self.assertEqual(check_contents('RTORRENT', 'deadbeef', 'ebook', 'a release'), '')


if __name__ == '__main__':
    unittest.main()
