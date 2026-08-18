#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test how torrent data and torrent urls from providers are validated, and
#   how the infohash we hand to a downloader is worked out.

import os
import unittest
from hashlib import sha1
from unittest import mock

from lazylibrarian import downloadmethods
from lazylibrarian.downloadmethods import (
    calculate_torrent_hash,
    hash_from_cache_url,
    torrent_data_error,
    torrent_info_hashes,
)
from unittests.unittesthelpers import LLTestCase

# Real torrents built with libtorrent 2.0.13 and loaded into qBittorrent 5.2.3
# to confirm these are the hashes, and the ids, a real client uses.
REAL_TORRENTS = {
    'v1': {'v1': 'ad01877a13a4c886d4682dad54cef29db1a74f75',
           'v2': '',
           'client_id': 'ad01877a13a4c886d4682dad54cef29db1a74f75'},
    'v2': {'v1': '',
           'v2': '3e351f9ba42f901376ed138d69316ba86c4c7521573b3898a598aaf8871fa3f3',
           'client_id': '3e351f9ba42f901376ed138d69316ba86c4c7521'},
    'hybrid': {'v1': '63e587ea7e9998936d2d9a22304ecf27dca545f8',
               'v2': 'e9b3d17f9310b107577c5c28d5b09124298a775bd7084c41d6e99e4d3fcda1f3',
               'client_id': 'e9b3d17f9310b107577c5c28d5b09124298a775b'}}


def real_torrent(name):
    # relative to this file: DIRS.PROG_DIR moves around depending on which
    # tests have run before this one
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'testdata', 'torrents', f'{name}.torrent')
    with open(path, 'rb') as f:
        return f.read()


# a single file torrent, built by hand so the expected hash is not in doubt
TORRENT_INFO = (b"d6:lengthi12e4:name9:book.epub12:piece lengthi16384e6:pieces20:"
                + bytes(range(20)) + b"e")
TORRENT_DATA = b"d8:announce22:http://tracker.example4:info" + TORRENT_INFO + b"e"
TORRENT_HASH = sha1(TORRENT_INFO).hexdigest()

HTML_RESPONSE = (b"<!DOCTYPE html>\n<html><head><title>404 Not Found</title></head>"
                 b"<body><h1>Nothing here</h1><p>The torrent you requested is not "
                 b"in the cache.</p></body></html>")


class TorrentDataErrorTest(LLTestCase):

    def test_valid_torrent(self):
        self.assertEqual(torrent_data_error(TORRENT_DATA), '')

    def test_html_error_page(self):
        self.assertIn('not a bencoded dictionary', torrent_data_error(HTML_RESPONSE))

    def test_empty_response(self):
        self.assertEqual(torrent_data_error(b''), 'empty response')

    def test_deeply_nested_response_is_rejected_not_raised(self):
        # a response built to exhaust the decoder has to come back as a reason,
        # not as an exception out of the download thread
        for depth in (500, 20000):
            data = b'd4:infod' + b'l' * depth + b'e' * depth + b'ee'
            self.assertIn('invalid bencode', torrent_data_error(data), depth)

    def test_truncated_torrent(self):
        self.assertIn('invalid bencode', torrent_data_error(TORRENT_DATA[:-10]))

    def test_bencoded_list_is_not_a_torrent(self):
        self.assertIn('not a bencoded dictionary', torrent_data_error(b"l4:spame"))

    def test_dictionary_without_info(self):
        self.assertEqual(torrent_data_error(b"d8:announce22:http://tracker.examplee"),
                         'no info dictionary')

    def test_info_is_not_a_dictionary(self):
        self.assertEqual(torrent_data_error(b"d4:infoi3ee"), 'no info dictionary')

    def test_empty_info_dictionary(self):
        self.assertEqual(torrent_data_error(b"d4:infodee"), 'info dictionary has no name')

    def test_info_without_pieces_or_file_tree(self):
        self.assertEqual(torrent_data_error(b"d4:infod4:name5:a.txtee"),
                         'info dictionary has no pieces or file tree')

    def test_the_reason_never_quotes_the_response(self):
        # it goes to the log and the history table, and a provider error body
        # can carry an api key
        reason = torrent_data_error(b'{"error":"bad key","apikey":"s3cr3t"}')
        self.assertNotIn('s3cr3t', reason)
        self.assertNotIn('apikey', reason)


class CalculateTorrentHashTest(LLTestCase):

    def test_hash_from_torrent_data(self):
        self.assertEqual(calculate_torrent_hash('http://tracker.example/t.torrent', TORRENT_DATA),
                         TORRENT_HASH)

    def test_hash_from_magnet(self):
        magnet = f"magnet:?xt=urn:btih:{TORRENT_HASH}&dn=book"
        self.assertEqual(calculate_torrent_hash(magnet), TORRENT_HASH)

    def test_hash_from_base32_magnet(self):
        # b32 magnets have to come back as a hex string, not bytes, or nothing
        # downstream can use them
        magnet = "magnet:?xt=urn:btih:MFRGGZDFMZTWQ2LKNNWG23TPOBYXE43U&dn=book"
        result = calculate_torrent_hash(magnet)
        self.assertIsInstance(result, str)
        # b32decode('MFRGG...') is b'abcdefghijklmnopqrst'
        self.assertEqual(result, b'abcdefghijklmnopqrst'.hex())

    def test_lowercase_base32_magnet(self):
        magnet = "magnet:?xt=urn:btih:mfrggzdfmztwq2lknnwg23tpobyxe43u&dn=book"
        self.assertEqual(calculate_torrent_hash(magnet), b'abcdefghijklmnopqrst'.hex())

    def test_magnet_wins_over_data(self):
        magnet = f"magnet:?xt=urn:btih:{'a' * 40}"
        self.assertEqual(calculate_torrent_hash(magnet, TORRENT_DATA), 'a' * 40)

    def test_html_response_gives_no_hash(self):
        self.assertEqual(calculate_torrent_hash('http://tracker.example/t.torrent', HTML_RESPONSE), '')

    def test_empty_response_gives_no_hash(self):
        self.assertEqual(calculate_torrent_hash('http://tracker.example/t.torrent', b''), '')

    def test_malformed_bencode_gives_no_hash(self):
        self.assertEqual(calculate_torrent_hash('http://tracker.example/t.torrent', TORRENT_DATA[:-10]), '')

    def test_no_link_and_no_data(self):
        self.assertEqual(calculate_torrent_hash('http://tracker.example/t.torrent'), '')

    def test_invalid_data_is_reported_as_a_provider_problem(self):
        with self.assertLogs('lazylibrarian.downloadmethods', level='ERROR') as logs:
            calculate_torrent_hash('http://tracker.example/t.torrent', HTML_RESPONSE)
        self.assertTrue(any('Provider returned invalid torrent data' in line for line in logs.output))


class BitTorrentV2Test(LLTestCase):
    """ A v2 torrent has no v1 hash at all, and a hybrid torrent has both. We
    keep handing downloaders the v1 hash where there is one, because that is
    what they key on, and only fall back to the truncated sha256 when there
    isn't. qBittorrent is the exception: it files v2 and hybrid torrents under
    the truncated sha256, and tells us so when it accepts the add. """

    def test_info_hashes(self):
        for name, expected in REAL_TORRENTS.items():
            self.assertEqual(torrent_info_hashes(real_torrent(name)),
                             (expected['v1'], expected['v2']), name)

    def test_v1_torrent_hash_is_unchanged(self):
        self.assertEqual(calculate_torrent_hash('http://x/t.torrent', real_torrent('v1')),
                         REAL_TORRENTS['v1']['v1'])

    def test_hybrid_torrent_keeps_the_v1_hash(self):
        self.assertEqual(calculate_torrent_hash('http://x/t.torrent', real_torrent('hybrid')),
                         REAL_TORRENTS['hybrid']['v1'])

    def test_v2_torrent_uses_the_truncated_sha256(self):
        # previously this returned nothing at all and the download failed
        self.assertEqual(calculate_torrent_hash('http://x/t.torrent', real_torrent('v2')),
                         REAL_TORRENTS['v2']['client_id'])

    def test_v2_torrent_is_not_reported_as_invalid_data(self):
        self.assertEqual(torrent_data_error(real_torrent('v2')), '')
        self.assertEqual(torrent_data_error(real_torrent('hybrid')), '')

    def test_v2_magnet(self):
        magnet = 'magnet:?xt=urn:btmh:1220' + REAL_TORRENTS['v2']['v2'] + '&dn=payload'
        self.assertEqual(calculate_torrent_hash(magnet), REAL_TORRENTS['v2']['client_id'])

    def test_hybrid_magnet_prefers_the_v1_hash(self):
        magnet = ('magnet:?xt=urn:btih:' + REAL_TORRENTS['hybrid']['v1'] +
                  '&xt=urn:btmh:1220' + REAL_TORRENTS['hybrid']['v2'] + '&dn=payload')
        self.assertEqual(calculate_torrent_hash(magnet), REAL_TORRENTS['hybrid']['v1'])

    def test_malformed_multihash_is_ignored(self):
        # only sha256 (0x12) of 32 bytes (0x20) is a torrent v2 infohash
        self.assertEqual(calculate_torrent_hash('magnet:?xt=urn:btmh:1114' + 'ab' * 32), '')
        self.assertEqual(calculate_torrent_hash('magnet:?xt=urn:btmh:1220' + 'ab' * 10), '')


class HashFromCacheUrlTest(LLTestCase):
    """ The hash in a torrent cache url is only trustworthy because we know how
    that host names its files. Any other 40 character string in a url is not an
    infohash. """

    def test_recognised_cache_url(self):
        url = ("https://itorrents.net/torrent/FE5ABFBAD81412F477A3C0E7D979415EAC27C545.torrent"
               "?title=Moore--Harold-G-We-Were-Soldiers-Once-and-Young--epub--zeke23")
        self.assertEqual(hash_from_cache_url(url), 'fe5abfbad81412f477a3c0e7d979415eac27c545')

    def test_unknown_host_is_not_trusted(self):
        url = "https://random.example/torrent/FE5ABFBAD81412F477A3C0E7D979415EAC27C545.torrent"
        self.assertEqual(hash_from_cache_url(url), '')

    def test_a_lookalike_host_is_not_trusted(self):
        for host in ['itorrents.net.attacker.example', 'notitorrents.net', 'itorrents.net.evil']:
            url = f"https://{host}/torrent/FE5ABFBAD81412F477A3C0E7D979415EAC27C545.torrent"
            self.assertEqual(hash_from_cache_url(url), '', host)

    def test_non_hex_name_is_not_a_hash(self):
        url = "https://itorrents.net/torrent/we-were-soldiers-once-and-young-epub-zzzz.torrent"
        self.assertEqual(hash_from_cache_url(url), '')

    def test_short_hex_name_is_not_a_hash(self):
        url = "https://itorrents.net/torrent/FE5ABFBAD81412F477A3C0E7D979415EAC27C5.torrent"
        self.assertEqual(hash_from_cache_url(url), '')

    def test_not_a_torrent_path(self):
        self.assertEqual(hash_from_cache_url("https://itorrents.net/torrent/" + 'a' * 40), '')

    def test_magnet_and_empty(self):
        self.assertEqual(hash_from_cache_url("magnet:?xt=urn:btih:" + 'a' * 40), '')
        self.assertEqual(hash_from_cache_url(''), '')


class FakeConfig:
    """ Enough of the config for tor_dl_method to reach a downloader """

    def __init__(self, **values):
        self.values = {'QBITTORRENT_HOST': 'localhost', 'TOR_DOWNLOADER_QBITTORRENT': True}
        self.values.update(values)

    def get_bool(self, key):
        return bool(self.values.get(key, False))

    def __getitem__(self, key):
        return self.values.get(key, '')


class TorDlMethodInvalidDataTest(LLTestCase):
    """ Whatever a provider hands back, malformed data must not reach a
    downloader, and the reason must say so. """

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        self.qbittorrent = mock.patch.object(downloadmethods, 'qbittorrent').start()
        mock.patch.object(downloadmethods, 'record_usage_data').start()
        mock.patch.object(downloadmethods, 'CONFIG', FakeConfig()).start()
        self.addCleanup(mock.patch.stopall)

    def fetch(self, content, status_code=200, content_type='text/html'):
        response = mock.Mock()
        response.status_code = status_code
        response.content = content
        response.headers = {'Content-Type': content_type}
        return mock.patch.object(downloadmethods.requests, 'get', return_value=response)

    def test_html_response_is_rejected(self):
        with self.fetch(HTML_RESPONSE):
            status, res = downloadmethods.tor_dl_method(
                bookid='1', tor_title='a book', tor_url='https://provider.example/get/1.torrent')

        self.assertFalse(status)
        self.assertIn('Provider returned invalid torrent data', res)
        self.qbittorrent.add_file.assert_not_called()
        self.qbittorrent.add_torrent.assert_not_called()

    def test_empty_response_is_rejected(self):
        with self.fetch(b''):
            status, res = downloadmethods.tor_dl_method(
                bookid='1', tor_title='a book', tor_url='https://provider.example/get/1.torrent')

        self.assertFalse(status)
        self.assertIn('empty response', res)
        self.qbittorrent.add_file.assert_not_called()

    def test_malformed_bencode_is_rejected(self):
        with self.fetch(TORRENT_DATA[:-10] + b'x' * 100, content_type='application/x-bittorrent'):
            status, res = downloadmethods.tor_dl_method(
                bookid='1', tor_title='a book', tor_url='https://provider.example/get/1.torrent')

        self.assertFalse(status)
        self.assertIn('Provider returned invalid torrent data', res)
        self.qbittorrent.add_file.assert_not_called()

    def test_valid_torrent_data_still_reaches_the_downloader(self):
        self.qbittorrent.add_file.return_value = (False, 'downloader said no', False)
        with self.fetch(TORRENT_DATA, content_type='application/x-bittorrent'):
            downloadmethods.tor_dl_method(
                bookid='1', tor_title='a book', tor_url='https://provider.example/get/1.torrent')

        self.qbittorrent.add_file.assert_called_once()
        self.assertEqual(self.qbittorrent.add_file.call_args.args[1], TORRENT_HASH)

    def test_bad_data_from_a_torrent_cache_falls_back_to_the_url(self):
        # the cache serves us an error page but names its files after the
        # infohash, so the downloader can be asked to fetch it instead
        url = "https://itorrents.net/torrent/FE5ABFBAD81412F477A3C0E7D979415EAC27C545.torrent"
        self.qbittorrent.add_torrent.return_value = (False, 'downloader could not fetch it either', False)
        with self.fetch(HTML_RESPONSE):
            status, res = downloadmethods.tor_dl_method(
                bookid='1', tor_title='a book', tor_url=url)

        self.qbittorrent.add_file.assert_not_called()
        self.qbittorrent.add_torrent.assert_called_once()
        self.assertEqual(self.qbittorrent.add_torrent.call_args.args[0], url)
        self.assertEqual(self.qbittorrent.add_torrent.call_args.args[1],
                         'fe5abfbad81412f477a3c0e7d979415eac27c545')
        # and a downloader that can't fetch it either is still a failure
        self.assertFalse(status)

    def test_bad_data_from_a_torrent_cache_is_not_written_to_a_blackhole(self):
        url = "https://itorrents.net/torrent/FE5ABFBAD81412F477A3C0E7D979415EAC27C545.torrent"
        with mock.patch.object(downloadmethods, 'CONFIG', FakeConfig(TOR_DOWNLOADER_BLACKHOLE=True)), \
                self.fetch(HTML_RESPONSE):
            status, res = downloadmethods.tor_dl_method(
                bookid='1', tor_title='a book', tor_url=url)

        self.assertFalse(status)
        self.assertIn('Provider returned invalid torrent data', res)


class TorDlMethodRejectionTest(LLTestCase):
    """ A torrent we had to take on from the client has to be recorded as such
    before anything is allowed to reject it. """

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        self.qbittorrent = mock.patch.object(downloadmethods, 'qbittorrent').start()
        self.qbittorrent.get_name.return_value = 'The Book'
        mock.patch.object(downloadmethods, 'record_usage_data').start()
        mock.patch.object(downloadmethods, 'CONFIG', FakeConfig(DEL_FAILED=True)).start()
        self.check_contents = mock.patch.object(downloadmethods, 'check_contents',
                                                return_value='The Book has no ebook files').start()
        self.delete_task = mock.patch.object(downloadmethods, 'delete_task').start()
        self.db = mock.Mock()
        mock.patch.object(downloadmethods.database, 'DBConnection', return_value=self.db).start()
        self.addCleanup(mock.patch.stopall)

    def fetch(self, content):
        response = mock.Mock()
        response.status_code = 200
        response.content = content
        response.headers = {'Content-Type': 'application/x-bittorrent'}
        return mock.patch.object(downloadmethods.requests, 'get', return_value=response)

    def snatch(self):
        with self.fetch(TORRENT_DATA):
            return downloadmethods.tor_dl_method(
                bookid='1', tor_title='The Book',
                tor_url='https://provider.example/get/1.torrent')

    def failed_row_update(self):
        for call in self.db.action.call_args_list:
            if "status='Failed'" in call.args[0]:
                return call.args[1]
        return None

    def snatched_row_update(self):
        for call in self.db.action.call_args_list:
            if 'UPDATE wanted' in call.args[0] and 'Snatched' in call.args[0]:
                return call.args[1]
        return None

    def test_a_rejected_adopted_torrent_is_recorded_as_adopted(self):
        # delete_task looks the row up by DownloadID and Source, so a failed
        # row that carries neither reads as a torrent we added ourselves and
        # its data gets deleted
        self.qbittorrent.add_file.return_value = (TORRENT_HASH, '', True)

        status, res = self.snatch()

        self.assertFalse(status)
        args = self.failed_row_update()
        self.assertIn(TORRENT_HASH, args)
        self.assertIn('QBITTORRENT', args)
        self.assertIn('adopted', args)
        self.delete_task.assert_called_once()

    def test_a_rejected_new_torrent_is_recorded_as_ours(self):
        self.qbittorrent.add_file.return_value = (TORRENT_HASH, '', False)

        self.snatch()

        self.assertIn('new', self.failed_row_update())

    def test_a_client_with_no_name_yet_still_gets_content_checked(self):
        # transmission withholds a name until a torrent has made progress, so
        # right after adding it returns nothing. Taking that as the title left
        # tor_title empty, and an empty title skips the content checks.
        self.qbittorrent.add_file.return_value = (TORRENT_HASH, '', False)
        self.qbittorrent.get_name.return_value = ''

        status, res = self.snatch()

        self.check_contents.assert_called_once()
        self.assertEqual(self.check_contents.call_args.args[3], 'The Book')
        self.assertFalse(status)
        self.assertIn('no ebook files', res)

    def test_the_category_we_asked_for_is_recorded(self):
        # what a later cleanup compares the torrent's own category against
        self.qbittorrent.add_file.return_value = (TORRENT_HASH, '', False)

        with mock.patch.object(downloadmethods, 'CONFIG',
                               FakeConfig(DEL_FAILED=True, QBITTORRENT_LABEL='books')):
            self.snatch()

        self.assertIn('books', self.failed_row_update())
        self.assertEqual(self.qbittorrent.add_file.call_args.kwargs['label'], 'books')

    def test_a_per_library_label_list_is_resolved_before_it_is_sent(self):
        # QBITTORRENT_LABEL can be a list, ebook first then audiobook. Sending
        # it unresolved filed the torrent under the whole string.
        self.qbittorrent.add_file.return_value = (TORRENT_HASH, '', False)
        self.check_contents.return_value = ''  # let it get as far as Snatched

        with mock.patch.object(downloadmethods, 'CONFIG',
                               FakeConfig(QBITTORRENT_LABEL='books, audiobooks')), \
                self.fetch(TORRENT_DATA):
            downloadmethods.tor_dl_method(bookid='1', tor_title='The Book',
                                          tor_url='https://provider.example/get/1.torrent',
                                          library='AudioBook')

        self.assertEqual(self.qbittorrent.add_file.call_args.kwargs['label'], 'audiobooks')
        self.assertIn('audiobooks', self.snatched_row_update())


if __name__ == '__main__':
    unittest.main()
