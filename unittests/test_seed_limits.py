#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test the per provider seeding requirement. Telling a client to stop at a
#   ratio does not stop us removing the torrent before it gets there, which on a
#   private tracker is how an account collects a hit and run.

import contextlib
import unittest
from unittest import mock

from lazylibrarian import download_client
from lazylibrarian.database import DBConnection
from lazylibrarian.dbupgrade import db_upgrade, upgrade_needed
from lazylibrarian.download_client import (
    delete_task,
    seed_requirement,
    seeding_incomplete,
)
from lazylibrarian.filesystem import DIRS, remove_file
from unittests.unittesthelpers import LLTestCase, LLTestCaseWithConfigandDIRS

HASHID = '06508f36be6ccdf545f29b81917d0cefeb03b1aa'


class FakeItem:
    def __init__(self, value):
        self.value = value


class FakeProvider:
    def __init__(self, name, ratio=0, duration=0):
        self.values = {'NAME': name, 'DISPNAME': name, 'HOST': f'https://{name}/rss'}
        self.settings = {'SEED_RATIO': ratio, 'SEED_DURATION': duration}

    def __getitem__(self, key):
        return self.values.get(key, '')

    def get_item(self, key):
        return FakeItem(self.settings.get(key, 0))


class FakeConfig:
    def __init__(self, torznab=(), rss=()):
        self.groups = {'TORZNAB': list(torznab), 'RSS': list(rss)}

    def providers(self, group):
        return self.groups.get(group, [])

    def __getitem__(self, key):
        return ''

    def get_bool(self, key):
        return False

    def get_int(self, key):
        return 0


class SeedRequirementTest(LLTestCase):

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)

    def test_a_torznab_provider(self):
        with mock.patch.object(download_client, 'CONFIG',
                               FakeConfig(torznab=[FakeProvider('tracker', ratio=2.0, duration=4320)])):
            self.assertEqual(seed_requirement('tracker'), (2.0, 4320))

    def test_an_rss_provider_asks_for_the_same_thing(self):
        # the gap this closes: an rss feed carried no requirement at all
        with mock.patch.object(download_client, 'CONFIG',
                               FakeConfig(rss=[FakeProvider('feed', ratio=1.5)])):
            self.assertEqual(seed_requirement('feed'), (1.5, 0))

    def test_matched_by_display_name_or_host(self):
        cfg = FakeConfig(rss=[FakeProvider('feed', duration=60)])
        with mock.patch.object(download_client, 'CONFIG', cfg):
            self.assertEqual(seed_requirement('https://feed/rss'), (0, 60))

    def test_an_unknown_provider_asks_for_nothing(self):
        with mock.patch.object(download_client, 'CONFIG',
                               FakeConfig(torznab=[FakeProvider('tracker', ratio=2.0)])):
            self.assertEqual(seed_requirement('somewhere else'), (0, 0))
            self.assertEqual(seed_requirement(''), (0, 0))


class SeedingIncompleteTest(LLTestCase):

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        self.qbittorrent = mock.patch.object(download_client, 'qbittorrent').start()
        self.addCleanup(mock.patch.stopall)

    def provider_config(self, **kwargs):
        return mock.patch.object(download_client, 'CONFIG',
                                 FakeConfig(torznab=[FakeProvider('tracker', **kwargs)]))

    def test_nothing_asked_for_holds_nothing_up(self):
        with self.provider_config():
            self.assertEqual(seeding_incomplete('QBITTORRENT', HASHID, ['tracker']), '')
        self.qbittorrent.seed_state.assert_not_called()

    def test_a_ratio_still_short(self):
        self.qbittorrent.seed_state.return_value = (0.4, 99999)
        with self.provider_config(ratio=2.0):
            self.assertIn('ratio 0.40 of 2.00', seeding_incomplete('QBITTORRENT', HASHID, ['tracker']))

    def test_a_ratio_met(self):
        self.qbittorrent.seed_state.return_value = (2.5, 0)
        with self.provider_config(ratio=2.0):
            self.assertEqual(seeding_incomplete('QBITTORRENT', HASHID, ['tracker']), '')

    def test_a_seed_time_still_short(self):
        self.qbittorrent.seed_state.return_value = (0, 3600)  # an hour
        with self.provider_config(duration=4320):  # three days
            self.assertIn('60 of 4320 minutes', seeding_incomplete('QBITTORRENT', HASHID, ['tracker']))

    def test_a_seed_time_met(self):
        self.qbittorrent.seed_state.return_value = (0, 4320 * 60)
        with self.provider_config(duration=4320):
            self.assertEqual(seeding_incomplete('QBITTORRENT', HASHID, ['tracker']), '')

    def test_either_limit_satisfies_it(self):
        # the client stops seeding at the first limit it reaches, so holding out
        # for the second would wait for a number that can no longer go up
        self.qbittorrent.seed_state.return_value = (5.0, 60)
        with self.provider_config(ratio=2.0, duration=4320):
            self.assertEqual(seeding_incomplete('QBITTORRENT', HASHID, ['tracker']), '')

    def test_neither_limit_met_names_both(self):
        self.qbittorrent.seed_state.return_value = (0.5, 60)
        with self.provider_config(ratio=2.0, duration=4320):
            owing = seeding_incomplete('QBITTORRENT', HASHID, ['tracker'])
        self.assertIn('ratio 0.50 of 2.00', owing)
        self.assertIn('1 of 4320 minutes', owing)

    def test_a_client_that_cannot_say_is_not_held_up(self):
        # refusing forever on a client we cannot ask leaves downloads in place
        # with nothing to show the user why
        self.qbittorrent.seed_state.return_value = None
        with self.provider_config(ratio=2.0):
            self.assertEqual(seeding_incomplete('QBITTORRENT', HASHID, ['tracker']), '')

    def test_the_strictest_provider_wins_for_a_shared_torrent(self):
        # an ebook and an audiobook request from different trackers, sharing one
        # torrent: it owes whichever asks for more
        self.qbittorrent.seed_state.return_value = (1.0, 0)
        cfg = FakeConfig(torznab=[FakeProvider('easy', ratio=0.5)],
                         rss=[FakeProvider('strict', ratio=3.0)])
        with mock.patch.object(download_client, 'CONFIG', cfg):
            self.assertIn('3.00', seeding_incomplete('QBITTORRENT', HASHID, ['easy', 'strict']))

    def test_a_client_with_no_seed_reporting_is_not_held_up(self):
        with self.provider_config(ratio=2.0):
            self.assertEqual(seeding_incomplete('RTORRENT', HASHID, ['tracker']), '')


class DeleteTaskSeedingTest(LLTestCaseWithConfigandDIRS):
    """ The requirement has to hold at the point something wants to delete, which
    is a later run than the one that snatched it. """

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        DIRS.DBFILENAME = "test-seed-limits.db"
        with contextlib.suppress(FileNotFoundError):
            remove_file(DIRS.get_dbfile())
        db_upgrade(upgrade_needed(), restartjobs=False)
        db = DBConnection()
        try:
            db.action("INSERT into wanted (NZBurl, NZBprov, Status, Source, DownloadID, Origin, "
                      "Category) VALUES (?, ?, ?, ?, ?, ?, ?)",
                      ('http://x/1', 'tracker', 'Seeding', 'QBITTORRENT', HASHID, 'new', 'books'))
        finally:
            db.close()
        self.qbittorrent = mock.patch.object(download_client, 'qbittorrent').start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        remove_file(DIRS.get_dbfile())
        super().tearDown()

    def provider_config(self, **kwargs):
        return mock.patch.object(download_client, 'CONFIG',
                                 FakeConfig(torznab=[FakeProvider('tracker', **kwargs)]))

    def test_a_torrent_still_owing_seeding_is_kept(self):
        self.qbittorrent.seed_state.return_value = (0.2, 0)
        with self.provider_config(ratio=2.0):
            self.assertFalse(delete_task('QBITTORRENT', HASHID, True))
        self.qbittorrent.remove_torrent.assert_not_called()

    def test_a_torrent_that_has_paid_its_way_is_removed(self):
        self.qbittorrent.seed_state.return_value = (2.0, 0)
        with self.provider_config(ratio=2.0):
            self.assertTrue(delete_task('QBITTORRENT', HASHID, True))
        self.qbittorrent.remove_torrent.assert_called_once_with(
            HASHID, True, expect_category='books')

    def test_a_usenet_download_is_not_held_up_by_a_seed_requirement(self):
        # seeding is a torrent idea. Nothing should be able to wedge sab or
        # nzbget cleanup behind a ratio.
        db = DBConnection()
        try:
            db.action("INSERT into wanted (NZBurl, NZBprov, Status, Source, DownloadID, Origin) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      ('http://x/2', 'tracker', 'Snatched', 'NZBGET', 'nzo_1', 'new'))
        finally:
            db.close()
        with mock.patch.object(download_client, 'nzbget') as nzbget, \
                self.provider_config(ratio=9.0):
            delete_task('NZBGET', 'nzo_1', True)
        nzbget.delete_nzb.assert_called_once_with('nzo_1', True)

    def test_no_requirement_leaves_the_old_behaviour(self):
        with self.provider_config():
            delete_task('QBITTORRENT', HASHID, True)
        self.qbittorrent.remove_torrent.assert_called_once()
        self.qbittorrent.seed_state.assert_not_called()


if __name__ == '__main__':
    unittest.main()
