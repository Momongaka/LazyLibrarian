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


EXISTING_HASH = '329df5974a730b22e6a9a5d873399014dae7ea83'

# a hybrid torrent, as qBittorrent 5.2.3 reports it: the id is the truncated v2
# hash, while the v1 hash we calculate from the metadata is only in infohash_v1
HYBRID_V1 = '63e587ea7e9998936d2d9a22304ecf27dca545f8'
HYBRID_V2 = 'e9b3d17f9310b107577c5c28d5b09124298a775bd7084c41d6e99e4d3fcda1f3'
HYBRID_ID = HYBRID_V2[:40]


def _http_error(status_code, text=''):
    response = mock.Mock()
    response.status_code = status_code
    response.text = text
    return requests.HTTPError(response=response)


def _label(name):
    """ Pretend QBITTORRENT_LABEL is set to name """
    return mock.patch.object(type(qbittorrent.CONFIG), '__getitem__',
                             lambda _self, key: name if key == 'QBITTORRENT_LABEL' else '')


def _torrent(hashid=EXISTING_HASH, **kwargs):
    """ A torrents/info entry, with the fields we care about """
    entry = {'hash': hashid, 'name': "Feynman's Lost Lecture", 'state': 'stalledUP',
             'category': '', 'progress': 1.0, 'content_path': '/downloads/feynman'}
    entry.update(kwargs)
    return entry


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

        status, res, adopted = qbittorrent.wait_for_torrent(
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

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertTrue(status)
        self.assertTrue(any('partial add failure' in str(call)
                             for call in dlcommslogger.error.call_args_list))

    def test_legacy_response_succeeds_once_hash_appears(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.side_effect = [_http_error(404), {'hash': 'deadbeef'}]
        qbclient.qbittorrent_version = 'v4.6.0'

        status, res, adopted = qbittorrent.wait_for_torrent(
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
            status, res, adopted = qbittorrent.wait_for_torrent(
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

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertTrue(status)
        self.assertEqual(res, '')
        self.assertEqual(qbclient.get_torrent.call_count, 16)

    def test_pending_response_times_out_after_sixty_seconds(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.side_effect = _http_error(404)
        result = {"added_torrent_ids": [], "failure_count": 0,
                   "pending_count": 1, "success_count": 0}

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertFalse(status)
        self.assertIn('not found in torrent list', res)
        self.assertEqual(qbclient.get_torrent.call_count, qbittorrent.QBIT_ADD_PENDING_POLL_SECONDS)

    def test_non_404_http_error_fails_immediately(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.side_effect = _http_error(500)
        result = {"added_torrent_ids": [], "failure_count": 0,
                   "pending_count": 1, "success_count": 0}

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, 'deadbeef', result, 'add_torrent')

        self.assertFalse(status)
        self.assertEqual(qbclient.get_torrent.call_count, 1)

    def test_the_calculated_hash_is_returned_for_a_v1_torrent(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.return_value = {'hash': EXISTING_HASH}
        qbclient.qbittorrent_version = 'v5.2.3'
        result = {"added_torrent_ids": [EXISTING_HASH], "failure_count": 0,
                   "pending_count": 0, "success_count": 1}

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, EXISTING_HASH, result, 'add_file')

        self.assertEqual(status, EXISTING_HASH)

    def test_qbittorrents_own_id_wins_for_a_hybrid_torrent(self):
        # we calculate the v1 hash from the metadata, but qBittorrent files a
        # hybrid torrent under its truncated v2 hash and says so in the response
        qbclient = mock.Mock()
        qbclient.get_torrent.return_value = {'hash': HYBRID_ID}
        qbclient.qbittorrent_version = 'v5.2.3'
        result = {"added_torrent_ids": [HYBRID_ID], "failure_count": 0,
                   "pending_count": 0, "success_count": 1}

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, HYBRID_V1, result, 'add_file')

        self.assertEqual(status, HYBRID_ID)
        qbclient.get_torrent.assert_called_once_with(HYBRID_ID)

    def test_a_junk_added_id_is_ignored(self):
        qbclient = mock.Mock()
        qbclient.get_torrent.return_value = {'hash': EXISTING_HASH}
        qbclient.qbittorrent_version = 'v5.2.3'
        result = {"added_torrent_ids": ["not a hash"], "failure_count": 0,
                   "pending_count": 0, "success_count": 1}

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, EXISTING_HASH, result, 'add_file')

        self.assertEqual(status, EXISTING_HASH)
        qbclient.get_torrent.assert_called_once_with(EXISTING_HASH)

    def test_a_batch_of_added_ids_is_ignored(self):
        # we only ever submit one torrent, so more than one id means we cannot
        # tell which is ours
        qbclient = mock.Mock()
        qbclient.get_torrent.return_value = {'hash': EXISTING_HASH}
        qbclient.qbittorrent_version = 'v5.2.3'
        result = {"added_torrent_ids": [HYBRID_ID, EXISTING_HASH], "failure_count": 0,
                   "pending_count": 0, "success_count": 2}

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, EXISTING_HASH, result, 'add_file')

        self.assertEqual(status, EXISTING_HASH)

    def test_hybrid_url_add_is_found_by_the_final_full_scan(self):
        # a pending url add gives us no id up front, and polling for the v1
        # hash of what turns out to be a hybrid torrent never resolves
        qbclient = mock.Mock()
        qbclient.get_torrent.side_effect = _http_error(404)
        qbclient.qbittorrent_version = 'v5.2.3'
        qbclient.torrents.side_effect = [[], [_torrent(hashid=HYBRID_ID, infohash_v1=HYBRID_V1,
                                                       infohash_v2=HYBRID_V2)]]
        result = {"added_torrent_ids": [], "failure_count": 0,
                   "pending_count": 1, "success_count": 0}

        status, res, adopted = qbittorrent.wait_for_torrent(
            qbclient, self.dlcommslogger, HYBRID_V1, result, 'add_torrent')

        self.assertEqual(status, HYBRID_ID)
        self.assertEqual(res, '')


class FindTorrentTest(LLTestCase):

    def test_matches_exact_hash(self):
        qbclient = mock.Mock()
        qbclient.torrents.return_value = [_torrent()]

        self.assertEqual(qbittorrent.find_torrent(qbclient, EXISTING_HASH), _torrent())
        qbclient.torrents.assert_called_once_with(hashes=EXISTING_HASH)

    def test_lookup_is_not_filtered_by_category(self):
        # a duplicate can sit in any category, so asking for one would hide it
        qbclient = mock.Mock()
        qbclient.torrents.return_value = [_torrent(category='films')]

        self.assertTrue(qbittorrent.find_torrent(qbclient, EXISTING_HASH))
        self.assertNotIn('category', qbclient.torrents.call_args.kwargs)

    def test_matches_uppercase_hash_from_qbittorrent(self):
        qbclient = mock.Mock()
        qbclient.torrents.return_value = [_torrent(hashid=EXISTING_HASH.upper())]

        self.assertTrue(qbittorrent.find_torrent(qbclient, EXISTING_HASH))

    def test_matches_hybrid_torrent_on_infohash_v1(self):
        qbclient = mock.Mock()
        qbclient.torrents.return_value = [_torrent(hashid='b' * 64, infohash_v1=EXISTING_HASH,
                                                   infohash_v2='b' * 64)]

        self.assertTrue(qbittorrent.find_torrent(qbclient, EXISTING_HASH))

    def test_ignores_other_torrents_when_server_does_not_filter(self):
        # pre-2.0.1 web api ignores the hashes parameter and returns everything
        qbclient = mock.Mock()
        qbclient.torrents.return_value = [_torrent(hashid='a' * 40), _torrent(hashid='c' * 40)]

        self.assertEqual(qbittorrent.find_torrent(qbclient, EXISTING_HASH), {})

    def test_unusable_hashid_looks_up_nothing(self):
        # a DownloadID from another downloader, or a missing one, must not
        # reach the api or raise
        qbclient = mock.Mock()

        for value in [None, '', 0, b'deadbeef']:
            self.assertEqual(qbittorrent.find_torrent(qbclient, value), {}, value)
        qbclient.torrents.assert_not_called()

    def test_unexpected_response_type(self):
        qbclient = mock.Mock()
        qbclient.torrents.return_value = 'Forbidden'

        self.assertEqual(qbittorrent.find_torrent(qbclient, EXISTING_HASH), {})

    def test_hybrid_torrent_is_not_found_by_the_hashes_filter_alone(self):
        # qBittorrent 5.2.3 returns nothing for torrents/info?hashes=<v1 of a
        # hybrid torrent>: the filter only matches the id it assigned
        qbclient = mock.Mock()
        qbclient.torrents.return_value = []

        self.assertEqual(qbittorrent.find_torrent(qbclient, HYBRID_V1), {})
        qbclient.torrents.assert_called_once_with(hashes=HYBRID_V1)

    def test_full_scan_finds_a_hybrid_torrent_by_its_v1_hash(self):
        qbclient = mock.Mock()
        hybrid = _torrent(hashid=HYBRID_ID, infohash_v1=HYBRID_V1, infohash_v2=HYBRID_V2)
        qbclient.torrents.side_effect = [[], [_torrent(hashid='a' * 40), hybrid]]

        found = qbittorrent.find_torrent(qbclient, HYBRID_V1, full_scan=True)

        self.assertEqual(found, hybrid)
        self.assertEqual(qbittorrent.torrent_id(found), HYBRID_ID)
        self.assertEqual(qbclient.torrents.call_args_list[1], mock.call())

    def test_full_scan_is_skipped_when_the_filter_already_matched(self):
        qbclient = mock.Mock()
        qbclient.torrents.return_value = [_torrent()]

        qbittorrent.find_torrent(qbclient, EXISTING_HASH, full_scan=True)

        self.assertEqual(qbclient.torrents.call_count, 1)

    def test_v2_torrent_matches_on_its_truncated_id(self):
        qbclient = mock.Mock()
        qbclient.torrents.return_value = [_torrent(hashid=HYBRID_V2[:40], infohash_v1='',
                                                    infohash_v2=HYBRID_V2)]

        self.assertTrue(qbittorrent.find_torrent(qbclient, HYBRID_V2[:40]))


class ValidInfohashTest(LLTestCase):

    def test_v1_infohash(self):
        self.assertTrue(qbittorrent.valid_infohash(EXISTING_HASH))

    def test_v2_infohash(self):
        self.assertTrue(qbittorrent.valid_infohash('a' * 64))

    def test_rejects_short_and_non_hex(self):
        for value in ['', None, 'deadbeef', 'z' * 40, EXISTING_HASH + 'a', 40 * 'a' + '\n']:
            self.assertFalse(qbittorrent.valid_infohash(value), value)


class RemoveTorrentTest(LLTestCase):

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        self.qbclient = mock.Mock()
        patcher = mock.patch.object(qbittorrent, 'get_client', return_value=self.qbclient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deletes_by_the_id_qbittorrent_filed_it_under(self):
        # find_torrent matches on infohash_v1 and infohash_v2 as well as the
        # id, so the entry it returns is not always keyed by the hash we asked
        # for, and the delete has to follow the entry
        self.qbclient.torrents.return_value = [_torrent(hashid=HYBRID_ID, infohash_v1=HYBRID_V1,
                                                        infohash_v2=HYBRID_V2, state='pausedUP')]

        self.assertTrue(qbittorrent.remove_torrent(HYBRID_V1, remove_data=True))
        self.qbclient.delete_permanently.assert_called_once_with(HYBRID_ID)

    def test_deletes_a_v1_torrent_by_its_own_hash(self):
        self.qbclient.torrents.return_value = [_torrent(state='pausedUP')]

        self.assertTrue(qbittorrent.remove_torrent(EXISTING_HASH))
        self.qbclient.delete.assert_called_once_with(EXISTING_HASH)

    def test_a_torrent_in_another_category_is_left_alone(self):
        # the case that cost someone a private tracker seed: we can reach a
        # torrent in any category by hash, so the category has to be a veto
        # rather than something we note on the way past
        self.qbclient.torrents.return_value = [_torrent(name='The Windsors At War',
                                                        category='Long-term Seeding',
                                                        state='pausedUP')]

        with self.assertLogs(level='WARNING') as logged:
            self.assertFalse(qbittorrent.remove_torrent(EXISTING_HASH, remove_data=True,
                                                        expect_category='books'))

        self.qbclient.delete_permanently.assert_not_called()
        self.qbclient.delete.assert_not_called()
        said = ' '.join(logged.output)
        self.assertIn('Skipping deletion', said)
        self.assertIn('Long-term Seeding', said)
        self.assertNotIn('removing', said)

    def test_data_is_removed_for_a_torrent_in_the_category_we_recorded(self):
        self.qbclient.torrents.return_value = [_torrent(category='llbooks', state='pausedUP')]

        self.assertTrue(qbittorrent.remove_torrent(EXISTING_HASH, remove_data=True,
                                                   expect_category='llbooks'))

        self.qbclient.delete_permanently.assert_called_once_with(EXISTING_HASH)

    def test_a_recategorised_torrent_of_ours_is_left_alone(self):
        # we added it, then someone moved it somewhere they are keeping it
        self.qbclient.torrents.return_value = [_torrent(category='Long-term Seeding',
                                                        state='pausedUP')]

        self.assertFalse(qbittorrent.remove_torrent(EXISTING_HASH, remove_data=True,
                                                    expect_category='llbooks'))
        self.qbclient.delete.assert_not_called()
        self.qbclient.delete_permanently.assert_not_called()

    def test_with_nothing_recorded_the_configured_categories_decide(self):
        self.qbclient.torrents.return_value = [_torrent(category='Long-term Seeding',
                                                        state='pausedUP')]

        with _label('llbooks'):
            self.assertFalse(qbittorrent.remove_torrent(EXISTING_HASH, remove_data=True))
        self.qbclient.delete_permanently.assert_not_called()

    def test_with_nothing_recorded_our_own_category_still_passes(self):
        self.qbclient.torrents.return_value = [_torrent(category='llbooks', state='pausedUP')]

        with _label('llbooks'):
            self.assertTrue(qbittorrent.remove_torrent(EXISTING_HASH, remove_data=True))
        self.qbclient.delete_permanently.assert_called_once_with(EXISTING_HASH)

    def test_a_per_library_label_list_still_matches(self):
        # QBITTORRENT_LABEL can be a list that resolves per library, so both
        # halves of it are categories of ours
        self.qbclient.torrents.return_value = [_torrent(category='llaudio', state='pausedUP')]

        with _label('llbooks, llaudio'):
            self.assertTrue(qbittorrent.remove_torrent(EXISTING_HASH, remove_data=True))
        self.qbclient.delete_permanently.assert_called_once_with(EXISTING_HASH)

    def test_an_uncategorised_setup_still_deletes(self):
        self.qbclient.torrents.return_value = [_torrent(category='', state='pausedUP')]

        with _label(''):
            self.assertTrue(qbittorrent.remove_torrent(EXISTING_HASH, remove_data=True))
        self.qbclient.delete_permanently.assert_called_once_with(EXISTING_HASH)

    def test_seeding_is_still_respected_for_a_torrent_of_ours(self):
        self.qbclient.torrents.return_value = [_torrent(category='llbooks', state='uploading')]

        def config(_self, key):
            return 'llbooks' if key == 'QBITTORRENT_LABEL' else ''

        with mock.patch.object(type(qbittorrent.CONFIG), '__getitem__', config), \
                mock.patch.object(type(qbittorrent.CONFIG), 'get_bool', lambda _s, k: k == 'SEED_WAIT'):
            self.assertFalse(qbittorrent.remove_torrent(EXISTING_HASH, remove_data=True,
                                                        expect_category='llbooks'))
        self.qbclient.delete_permanently.assert_not_called()

    def test_unknown_hash_removes_nothing(self):
        self.qbclient.torrents.return_value = []

        self.assertFalse(qbittorrent.remove_torrent(EXISTING_HASH))
        self.qbclient.delete.assert_not_called()


class ConfiguredCategoriesTest(LLTestCase):

    def test_a_single_label(self):
        with _label('books'):
            self.assertEqual(qbittorrent.configured_categories(), {'books'})

    def test_a_per_library_list(self):
        with _label('books, audiobooks'):
            self.assertEqual(qbittorrent.configured_categories(),
                             {'books, audiobooks', 'books', 'audiobooks'})

    def test_no_label_at_all(self):
        with _label(''):
            self.assertEqual(qbittorrent.configured_categories(), set())


class AddDuplicateTest(LLTestCase):
    """ qBittorrent answers torrents/add with 409 Conflict when it already has
    the torrent. The add is only really a failure if the hash we wanted isn't
    there, so these cover both readings of a 409. """

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        self.qbclient = mock.Mock()
        self.qbclient.qbittorrent_version = 'v5.2.1'
        patcher = mock.patch.object(qbittorrent, 'get_client', return_value=self.qbclient)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch.object(qbittorrent.time, 'sleep')
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_new_torrent_accepted(self):
        self.qbclient.download_from_file.return_value = {
            "added_torrent_ids": [EXISTING_HASH], "failure_count": 0,
            "pending_count": 0, "success_count": 1}
        self.qbclient.get_torrent.return_value = {'hash': EXISTING_HASH}

        status, res, adopted = qbittorrent.add_file(b'data', EXISTING_HASH, 'a title', {})

        self.assertTrue(status)
        self.assertEqual(res, '')
        self.assertFalse(adopted)

    def test_duplicate_data_add_uses_existing_hash(self):
        self.qbclient.download_from_file.side_effect = _http_error(409)
        self.qbclient.torrents.return_value = [_torrent(state='downloading', progress=0.4)]

        status, res, adopted = qbittorrent.add_file(b'data', EXISTING_HASH, 'a title', {})

        self.assertEqual(status, EXISTING_HASH)
        self.assertEqual(res, '')
        # the torrent was already there, so it is not ours to delete later
        self.assertTrue(adopted)

    def test_duplicate_hybrid_torrent_returns_the_id_qbittorrent_uses(self):
        # the v1 hash we submitted is not the id it is filed under, so the
        # DownloadID we record has to be the one its api answers to
        self.qbclient.download_from_file.side_effect = _http_error(409)
        self.qbclient.torrents.side_effect = [[], [_torrent(hashid=HYBRID_ID, infohash_v1=HYBRID_V1,
                                                            infohash_v2=HYBRID_V2)]]

        status, res, adopted = qbittorrent.add_file(b'data', HYBRID_V1, 'a title', {})

        self.assertEqual(status, HYBRID_ID)
        self.assertEqual(res, '')

    def test_duplicate_private_torrent_trackers_not_merged(self):
        # "Trackers cannot be merged because it is a private torrent" - the
        # torrent is still there, which is all we need
        self.qbclient.download_from_file.side_effect = _http_error(
            409, 'Trackers cannot be merged because it is a private torrent')
        self.qbclient.torrents.return_value = [_torrent(state='pausedUP')]

        status, res, adopted = qbittorrent.add_file(b'data', EXISTING_HASH, 'a title', {})

        self.assertTrue(status)
        self.assertEqual(res, '')

    def test_duplicate_public_torrent_trackers_merged(self):
        self.qbclient.download_from_link.side_effect = _http_error(
            409, 'Trackers are merged from new source')
        self.qbclient.torrents.return_value = [_torrent(hashid='b2f98397ebee101bdbbb9c371252116d06736577')]

        status, res, adopted = qbittorrent.add_torrent('http://tracker.example/t.torrent',
                                              'b2f98397ebee101bdbbb9c371252116d06736577', {})

        self.assertTrue(status)
        self.assertEqual(res, '')

    def test_duplicate_under_different_name_category_and_path(self):
        self.qbclient.download_from_file.side_effect = _http_error(409)
        self.qbclient.torrents.return_value = [_torrent(name='The Great Courses (2011)',
                                                        category='lectures',
                                                        content_path='/elsewhere/lectures')]

        status, res, adopted = qbittorrent.add_file(b'data', EXISTING_HASH, 'a title', {})

        self.assertTrue(status)

    def test_genuine_conflict_without_matching_hash_stays_failed(self):
        self.qbclient.download_from_file.side_effect = _http_error(409, 'Unable to add torrent')
        self.qbclient.torrents.return_value = []

        status, res, adopted = qbittorrent.add_file(b'data', EXISTING_HASH, 'a title', {})

        self.assertFalse(status)
        self.assertFalse(adopted)
        self.assertIn('409', res)
        self.assertIn(EXISTING_HASH, res)
        self.assertIn('Unable to add torrent', res)

    def test_conflict_lookup_failure_stays_failed(self):
        self.qbclient.download_from_file.side_effect = _http_error(409)
        self.qbclient.torrents.side_effect = requests.ConnectionError('dropped')

        status, res, adopted = qbittorrent.add_file(b'data', EXISTING_HASH, 'a title', {})

        self.assertFalse(status)
        self.assertIn('lookup failed', res)

    def test_other_http_errors_are_not_treated_as_duplicates(self):
        self.qbclient.download_from_file.side_effect = _http_error(415, 'Torrent file is not valid')

        status, res, adopted = qbittorrent.add_file(b'data', EXISTING_HASH, 'a title', {})

        self.assertFalse(status)
        self.qbclient.torrents.assert_not_called()

    def test_conflict_reason_is_redacted(self):
        # the body is normally just "Conflict", but it is the one place
        # qBittorrent could hand back the url we submitted
        self.qbclient.download_from_link.side_effect = _http_error(
            409, 'Unable to add torrent from http://tracker.example/get.torrent?passkey=s3cr3t')
        self.qbclient.torrents.return_value = []

        status, res, adopted = qbittorrent.add_torrent('http://tracker.example/get.torrent?passkey=s3cr3t',
                                              EXISTING_HASH, {})

        self.assertFalse(status)
        self.assertNotIn('s3cr3t', res)
        self.assertIn('[redacted]', res)

    def test_conflict_with_unusable_hash_stays_failed(self):
        self.qbclient.download_from_link.side_effect = _http_error(409)

        status, res, adopted = qbittorrent.add_torrent('http://tracker.example/t.torrent', 'not-a-hash', {})

        self.assertFalse(status)
        self.assertIn('not a usable infohash', res)
        self.qbclient.torrents.assert_not_called()


class GetProgressTest(LLTestCase):
    """ get_progress delegates the 'keep seeding?' decision to the client's state.

    A completed torrent is only 'finished' once qBittorrent has stopped seeding it
    (stoppedUP, or pausedUP before web API 2.11.0); while it is still uploading it must
    stay unfinished so KEEP_SEEDING is honoured, and it must never hang in 'Seeding'
    just because no share-ratio/seeding-time limit happens to be configured.
    """

    def setUp(self):
        super().setUp()
        self.qbclient = mock.Mock()
        patcher = mock.patch.object(qbittorrent, 'get_client', return_value=self.qbclient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _progress_for(self, state):
        self.qbclient.torrents.return_value = [_torrent(state=state)]
        return qbittorrent.get_progress(EXISTING_HASH)

    def test_stopped_is_finished(self):
        _progress, _state, finished = self._progress_for('stoppedUP')
        self.assertTrue(finished)

    def test_paused_is_finished(self):
        # pre-2.11.0 web api name for a stopped-seeding torrent
        _progress, _state, finished = self._progress_for('pausedUP')
        self.assertTrue(finished)

    def test_active_seeding_states_are_not_finished(self):
        for state in ('uploading', 'stalledUP', 'forcedUP', 'queuedUP'):
            _progress, _state, finished = self._progress_for(state)
            self.assertFalse(finished, state)

    def test_downloading_is_not_finished(self):
        _progress, _state, finished = self._progress_for('downloading')
        self.assertFalse(finished)

    def test_progress_is_scaled_to_percent(self):
        self.qbclient.torrents.return_value = [_torrent(state='stoppedUP', progress=0.5)]
        progress, _state, _finished = qbittorrent.get_progress(EXISTING_HASH)
        self.assertEqual(progress, 50)

    def test_unknown_hash_returns_not_found(self):
        self.qbclient.torrents.return_value = []
        progress, _msg, finished = qbittorrent.get_progress(EXISTING_HASH)
        self.assertEqual(progress, -1)
        self.assertFalse(finished)


if __name__ == '__main__':
    unittest.main()
