#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test how the Transmission client handles the answers it gets back, in
#   particular an id Transmission does not hold.

import unittest
from unittest import mock

from lazylibrarian import transmission
from unittests.unittesthelpers import LLTestCase

HASHID = 'ad01877a13a4c886d4682dad54cef29db1a74f75'


def _response(torrents):
    return {'result': 'success', 'arguments': {'torrents': torrents}}, ''


class GetTorrentFilesTest(LLTestCase):
    """ Transmission returns an empty torrent list rather than an error for an
    id it doesn't hold, which used to be read as "the first torrent in the
    list". """

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        self.action = mock.patch.object(transmission, 'torrent_action').start()
        mock.patch.object(transmission.time, 'sleep').start()
        self.addCleanup(mock.patch.stopall)

    def test_files_are_returned(self):
        files = [{'name': 'book.epub', 'length': 1024}]
        self.action.return_value = _response([{'id': 1, 'files': files}])

        self.assertEqual(transmission.get_torrent_files(HASHID), files)

    def test_unknown_id_returns_no_files(self):
        self.action.return_value = _response([])

        self.assertEqual(transmission.get_torrent_files(HASHID), [])
        self.action.assert_called_once()

    def test_no_response_returns_no_files(self):
        self.action.return_value = (False, 'no response')

        self.assertEqual(transmission.get_torrent_files(HASHID), [])

    def test_an_empty_file_list_is_retried(self):
        # metadata for a magnet may not have arrived yet
        files = [{'name': 'book.epub', 'length': 1024}]
        self.action.side_effect = [_response([{'id': 1, 'files': []}]),
                                   _response([{'id': 1, 'files': files}])]

        self.assertEqual(transmission.get_torrent_files(HASHID), files)
        self.assertEqual(self.action.call_count, 2)

    def test_gives_up_after_three_tries(self):
        self.action.return_value = _response([{'id': 1, 'files': []}])

        self.assertEqual(transmission.get_torrent_files(HASHID), [])
        self.assertEqual(self.action.call_count, 3)


class AddTorrentTest(LLTestCase):

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        self.action = mock.patch.object(transmission, 'torrent_action').start()
        self.addCleanup(mock.patch.stopall)

    def test_a_new_torrent_is_ours(self):
        self.action.return_value = ({'result': 'success',
                                     'arguments': {'torrent-added': {'id': 7}}}, '')

        torrentid, res, adopted = transmission.add_torrent('http://x/t.torrent',
                                                           provider_options={})

        self.assertEqual(torrentid, 7)
        self.assertEqual(res, '')
        self.assertFalse(adopted)

    def test_a_duplicate_is_still_usable_but_not_ours(self):
        # Transmission resolves the duplicate itself and hands back the torrent
        # it already holds, which someone else added
        self.action.return_value = ({'result': 'success',
                                     'arguments': {'torrent-duplicate': {'id': 7}}}, '')

        torrentid, res, adopted = transmission.add_torrent('http://x/t.torrent',
                                                           provider_options={})

        self.assertEqual(torrentid, 7)
        self.assertEqual(res, '')
        self.assertTrue(adopted)

    def test_a_failure_is_not_adopted(self):
        self.action.return_value = ({'result': 'invalid or corrupt torrent file',
                                     'arguments': {}}, '')

        torrentid, res, adopted = transmission.add_torrent('http://x/t.torrent',
                                                           provider_options={})

        self.assertFalse(torrentid)
        self.assertIn('invalid', res)
        self.assertFalse(adopted)


if __name__ == '__main__':
    unittest.main()
