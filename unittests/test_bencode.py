#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test the bundled bencode library against real torrents, including the
#   BitTorrent v2 ones whose "piece layers" dictionary is keyed by raw 32 byte
#   merkle roots rather than by text.
#
#   The fixtures in testdata/torrents were produced with libtorrent 2.0.13 and
#   loaded into qBittorrent 5.2.3 to confirm the hashes below are the ones a
#   real client calculates.

import os
import unittest
from hashlib import sha1, sha256

from lib.bencode import bdecode, bencode
from unittests.unittesthelpers import LLTestCase

TORRENTS = {
    'v1': {
        'v1': 'ad01877a13a4c886d4682dad54cef29db1a74f75',
        'v2': '',
    },
    'v2': {
        'v1': '',
        'v2': '3e351f9ba42f901376ed138d69316ba86c4c7521573b3898a598aaf8871fa3f3',
    },
    'hybrid': {
        'v1': '63e587ea7e9998936d2d9a22304ecf27dca545f8',
        'v2': 'e9b3d17f9310b107577c5c28d5b09124298a775bd7084c41d6e99e4d3fcda1f3',
    },
}


def torrent_data(name):
    # relative to this file: DIRS.PROG_DIR moves around depending on which
    # tests have run before this one
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'testdata', 'torrents', f'{name}.torrent')
    with open(path, 'rb') as f:
        return f.read()


class BencodeTorrentTest(LLTestCase):

    def test_decodes_every_torrent_version(self):
        for name in TORRENTS:
            decoded = bdecode(torrent_data(name))
            self.assertIn('info', decoded, name)

    def test_round_trip_is_byte_identical(self):
        # the infohash is the hash of the re-encoded info dictionary, so an
        # encoder that does not reproduce the original bytes gives wrong hashes
        for name in TORRENTS:
            data = torrent_data(name)
            self.assertEqual(bencode(bdecode(data)), data, name)

    def test_infohashes_match_a_real_client(self):
        for name, expected in TORRENTS.items():
            info = bdecode(torrent_data(name))['info']
            encoded = bencode(info)
            if expected['v1']:
                self.assertEqual(sha1(encoded).hexdigest(), expected['v1'], name)
            if expected['v2']:
                self.assertEqual(sha256(encoded).hexdigest(), expected['v2'], name)

    def test_binary_dictionary_keys_survive(self):
        # "piece layers" is keyed by the raw sha256 merkle root of each file
        for name in ('v2', 'hybrid'):
            layers = bdecode(torrent_data(name))['piece layers']
            self.assertTrue(layers, name)
            for key in layers:
                self.assertIsInstance(key, bytes, name)
                self.assertEqual(len(key), 32, name)

    def test_text_keys_are_still_text(self):
        info = bdecode(torrent_data('v1'))['info']
        for key in info:
            self.assertIsInstance(key, str)


if __name__ == '__main__':
    unittest.main()
