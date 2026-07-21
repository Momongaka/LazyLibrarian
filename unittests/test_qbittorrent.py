#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test the qBittorrent downloader, in particular how it interprets the
#   response from torrents/add across old and new qBittorrent Web API
#   versions.

import unittest
from unittest import mock

import requests

from lazylibrarian import qbittorrent
from lib.qbittorrent import Client
from unittests.unittesthelpers import LLTestCase


def _http_error(status_code):
    response = mock.Mock()
    response.status_code = status_code
    return requests.HTTPError(response=response)


class ClassifyAddResponseTest(LLTestCase):

    def test_legacy_text_response(self):
        self.assertEqual(qbittorrent.classify_add_response("Ok."), ('legacy', []))

    def test_legacy_empty_response(self):
        self.assertEqual(qbittorrent.classify_add_response({}), ('legacy', []))

    def test_modern_accepted_response(self):
        result = {"added_torrent_ids": ["abc123"], "failure_count": 0,
                   "pending_count": 0, "success_count": 1}
        self.assertEqual(qbittorrent.classify_add_response(result), ('accepted', ["abc123"]))

    def test_modern_pending_response(self):
        result = {"added_torrent_ids": [], "failure_count": 0,
                   "pending_count": 1, "success_count": 0}
        self.assertEqual(qbittorrent.classify_add_response(result), ('pending', []))

    def test_modern_rejected_response(self):
        result = {"added_torrent_ids": [], "failure_count": 1,
                   "pending_count": 0, "success_count": 0}
        self.assertEqual(qbittorrent.classify_add_response(result), ('rejected', []))


class ClientGetTorrent404Test(unittest.TestCase):
    """ wait_for_torrent's 404-retry branch assumes lib.qbittorrent.Client
    surfaces a not-yet-indexed torrent as requests.HTTPError with a
    populated .response. Confirm that against the real Client, not just a
    mocked qbclient, since Client._request's raise_for_status() behaviour
    isn't part of the qbittorrent.py diff itself. """

    def test_get_torrent_404_raises_http_error_with_response(self):
        client = Client('http://localhost:1', '', '', verify=False)

        response = requests.Response()
        response.status_code = 404
        response.url = 'http://localhost:1/api/v2/torrents/properties?hash=deadbeef'
        response._content = b''

        with mock.patch.object(client._session, 'get', return_value=response):
            with self.assertRaises(requests.HTTPError) as ctx:
                client.get_torrent('deadbeef')

        self.assertIsNotNone(ctx.exception.response)
        self.assertEqual(ctx.exception.response.status_code, 404)


class WaitForTorrentTest(LLTestCase):

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)  # silence logging during the test run
        self.dlcommslogger = self.logger
        self.sleep_patcher = mock.patch.object(qbittorrent.time, 'sleep')
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def test_rejected_response_fails_without_polling(self):
        qbclient = mock.Mock()
        result = {"failure_count": 1, "pending_count": 0, "success_count": 0}

        status, res = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertFalse(status)
        self.assertIn('rejected', res)
        qbclient.get_torrent.assert_not_called()

    def test_partial_failure_is_logged_but_does_not_block(self):
        # qBittorrent's batch add response isn't mutually exclusive - a mixed
        # success_count/failure_count is theoretically possible even though
        # this codebase only ever submits a single url per call. Make sure
        # that isn't silently dropped once success_count makes it 'accepted'.
        qbclient = mock.Mock()
        qbclient.get_torrent.return_value = {'hash': 'deadbeef'}
        dlcommslogger = mock.Mock()
        result = {"added_torrent_ids": ["deadbeef"], "failure_count": 1,
                   "pending_count": 0, "success_count": 1}

        status, res = qbittorrent.wait_for_torrent(
            qbclient, dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertTrue(status)
        self.assertTrue(any('partial add failure' in str(call)
                             for call in dlcommslogger.error.call_args_list))

    def test_legacy_response_succeeds_once_hash_appears(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.side_effect = [_http_error(404), {'hash': 'deadbeef'}]
        qbclient.qbittorrent_version = 'v4.6.0'

        status, res = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, 'deadbeef', "Ok.", 'add_torrent')

        self.assertTrue(status)
        self.assertEqual(res, '')
        self.assertEqual(qbclient.get_torrent.call_count, 2)

    def test_pause_failure_does_not_fail_an_otherwise_successful_add(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.return_value = {'hash': 'deadbeef'}
        qbclient.qbittorrent_version = 'v4.6.0'
        qbclient.pause.side_effect = requests.ConnectionError('dropped')

        with mock.patch.object(qbittorrent.CONFIG, 'get_bool', return_value=True):
            status, res = qbittorrent.wait_for_torrent(
                qbclient, self.dlcommslogger, 'deadbeef', "Ok.", 'add_torrent')

        self.assertTrue(status)
        qbclient.pause.assert_called_once()

    def test_pending_response_retries_past_ten_seconds(self):
        # A pending url can legitimately still be unresolved after the old
        # ten second window - this is the core regression from issue #2393.
        qbclient = mock.Mock()
        qbclient.get_torrent.side_effect = [_http_error(404)] * 15 + [{'hash': 'deadbeef'}]
        qbclient.qbittorrent_version = 'v5.2.1'
        result = {"added_torrent_ids": [], "failure_count": 0,
                   "pending_count": 1, "success_count": 0}

        status, res = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertTrue(status)
        self.assertEqual(res, '')
        self.assertEqual(qbclient.get_torrent.call_count, 16)

    def test_pending_response_times_out_after_sixty_seconds(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.side_effect = _http_error(404)
        result = {"added_torrent_ids": [], "failure_count": 0,
                   "pending_count": 1, "success_count": 0}

        status, res = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertFalse(status)
        self.assertIn('not found in torrent list', res)
        self.assertEqual(qbclient.get_torrent.call_count, qbittorrent.QBIT_ADD_PENDING_POLL_SECONDS)

    def test_non_404_http_error_fails_immediately(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.side_effect = _http_error(500)
        result = {"added_torrent_ids": [], "failure_count": 0,
                   "pending_count": 1, "success_count": 0}

        status, res = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertFalse(status)
        self.assertEqual(qbclient.get_torrent.call_count, 1)


if __name__ == '__main__':
    unittest.main()
