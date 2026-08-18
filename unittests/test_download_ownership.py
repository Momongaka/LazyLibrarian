#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test what LazyLibrarian is allowed to delete from a download client.
#   A torrent it did not add belongs to whoever did, and a qBittorrent lookup
#   reaches every category, so the guard runs against a real database rather
#   than an in-memory flag.

import contextlib
import unittest
from unittest import mock

from lazylibrarian import download_client
from lazylibrarian.database import DBConnection
from lazylibrarian.dbupgrade import db_upgrade, upgrade_needed
from lazylibrarian.download_client import delete_task, download_ownership, may_delete_data
from lazylibrarian.filesystem import DIRS, remove_file
from unittests.unittesthelpers import LLTestCaseWithConfigandDIRS

HASHID = '06508f36be6ccdf545f29b81917d0cefeb03b1aa'
OTHER_HASHID = 'cd814087ffcd3bd18f6748838ecadf4085ad560d'


class OwnershipTest(LLTestCaseWithConfigandDIRS):
    """ download_ownership answers from the database, so it survives the
    restart between snatching something and cleaning it up. """

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        DIRS.DBFILENAME = "test-ownership.db"
        with contextlib.suppress(FileNotFoundError):
            remove_file(DIRS.get_dbfile())
        db_upgrade(upgrade_needed(), restartjobs=False)

    def tearDown(self):
        remove_file(DIRS.get_dbfile())
        super().tearDown()

    @staticmethod
    def add_row(nzburl, origin, category, download_id=HASHID, source='QBITTORRENT'):
        db = DBConnection()
        try:
            db.action("INSERT into wanted (NZBurl, Status, Source, DownloadID, Origin, Category) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      (nzburl, 'Snatched', source, download_id, origin, category))
        finally:
            db.close()

    def test_a_torrent_we_added_is_owned(self):
        self.add_row('http://x/1', 'new', 'books')
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (True, 'books'))

    def test_an_adopted_torrent_is_not_owned(self):
        self.add_row('http://x/1', 'adopted', '')
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (False, None))

    def test_a_legacy_row_is_not_owned(self):
        # snatched before the column existed, so we cannot prove anything
        self.add_row('http://x/1', None, None)
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (False, None))

    def test_no_row_at_all_is_not_owned(self):
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (False, None))

    def test_an_adopted_row_settles_it_for_a_shared_torrent(self):
        # the same torrent serving an ebook and an audiobook request: one of
        # them took it on, so neither may delete it
        self.add_row('http://x/ebook', 'new', 'books')
        self.add_row('http://x/audio', 'adopted', '')
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (False, None))

    def test_disagreeing_categories_are_not_owned(self):
        self.add_row('http://x/ebook', 'new', 'books')
        self.add_row('http://x/audio', 'new', 'audiobooks')
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (False, None))

    def test_a_row_with_no_category_recorded_does_not_contradict_one(self):
        # a row snatched before the category column existed, sharing a torrent
        # with a newer one. It cannot say what the category is, so it does not
        # get a vote, and the recorded one still stands.
        self.add_row('http://x/older', 'new', None)
        self.add_row('http://x/newer', 'new', 'books')
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (True, 'books'))

    def test_no_category_recorded_anywhere_is_not_the_same_as_uncategorised(self):
        # None means we cannot check, and falls back to the configured
        # categories. An empty string means expect no category at all.
        self.add_row('http://x/1', 'new', None)
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (True, None))

    def test_ownership_is_read_back_from_disk(self):
        # what a restart really means here: a fresh connection to the file
        self.add_row('http://x/1', 'new', 'books')
        del_db = DBConnection()
        del_db.close()
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (True, 'books'))

    def test_another_torrent_is_not_confused_with_this_one(self):
        self.add_row('http://x/1', 'new', 'books', download_id=OTHER_HASHID)
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (False, None))

    def test_the_same_id_on_another_client_is_not_ours(self):
        self.add_row('http://x/1', 'new', 'books', source='TRANSMISSION')
        self.assertEqual(download_ownership('QBITTORRENT', HASHID), (False, None))


class DeleteTaskTest(LLTestCaseWithConfigandDIRS):
    """ No cleanup path may delete a task we cannot prove we created, whatever
    DEL_FAILED and DEL_COMPLETED are set to. """

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        DIRS.DBFILENAME = "test-ownership-delete.db"
        with contextlib.suppress(FileNotFoundError):
            remove_file(DIRS.get_dbfile())
        db_upgrade(upgrade_needed(), restartjobs=False)
        self.qbittorrent = mock.patch.object(download_client, 'qbittorrent').start()
        self.transmission = mock.patch.object(download_client, 'transmission').start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        remove_file(DIRS.get_dbfile())
        super().tearDown()

    @staticmethod
    def add_row(origin, category, source='QBITTORRENT', nzburl='http://x/1'):
        db = DBConnection()
        try:
            db.action("INSERT into wanted (NZBurl, Status, Source, DownloadID, Origin, Category) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      (nzburl, 'Snatched', source, HASHID, origin, category))
        finally:
            db.close()

    def test_an_adopted_torrent_is_never_deleted(self):
        self.add_row('adopted', '')
        for remove_data in (True, False):
            delete_task('QBITTORRENT', HASHID, remove_data)
        self.qbittorrent.remove_torrent.assert_not_called()

    def test_an_adopted_transmission_torrent_is_never_deleted(self):
        self.add_row('adopted', '', source='TRANSMISSION')
        delete_task('TRANSMISSION', HASHID, True)
        self.transmission.remove_torrent.assert_not_called()

    def test_an_adopted_torrent_in_our_own_category_is_still_not_deleted(self):
        # the category matching is not what makes it ours
        self.add_row('adopted', 'books')
        delete_task('QBITTORRENT', HASHID, True)
        self.qbittorrent.remove_torrent.assert_not_called()

    def test_a_legacy_row_is_never_deleted(self):
        self.add_row(None, None)
        delete_task('QBITTORRENT', HASHID, True)
        self.qbittorrent.remove_torrent.assert_not_called()

    def test_a_torrent_we_added_is_still_deleted_with_its_data(self):
        self.add_row('new', 'books')
        delete_task('QBITTORRENT', HASHID, True)
        self.qbittorrent.remove_torrent.assert_called_once_with(
            HASHID, True, expect_category='books')

    def test_a_torrent_we_added_is_still_deleted_without_its_data(self):
        self.add_row('new', 'books')
        delete_task('QBITTORRENT', HASHID, False)
        self.qbittorrent.remove_torrent.assert_called_once_with(
            HASHID, False, expect_category='books')

    def test_a_shared_torrent_with_one_adopted_row_is_not_deleted(self):
        self.add_row('new', 'books', nzburl='http://x/ebook')
        self.add_row('adopted', '', nzburl='http://x/audio')
        delete_task('QBITTORRENT', HASHID, True)
        self.qbittorrent.remove_torrent.assert_not_called()

    def test_a_usenet_download_is_still_deleted(self):
        # only a torrent can turn out to be someone else's. Nothing records an
        # origin for a usenet task, and it was never adoptable, so the guard
        # must not quietly switch off sab and nzbget cleanup.
        with mock.patch.object(download_client, 'nzbget') as nzbget:
            delete_task('NZBGET', 'nzo_1', True)
        nzbget.delete_nzb.assert_called_once_with('nzo_1', True)

    def test_nothing_else_is_asked_to_move_or_relabel_it(self):
        self.add_row('adopted', '')
        delete_task('QBITTORRENT', HASHID, True)
        for method in ('delete', 'delete_permanently', 'set_torrent_location',
                       'set_torrent_name', 'setCategory'):
            self.assertFalse(getattr(self.qbittorrent, method).called, method)


class MayDeleteDataTest(LLTestCaseWithConfigandDIRS):
    """ The files on disk need the same answer as the task, and a torrent that
    has been moved into somebody's keep-forever category is no longer ours even
    though we added it. """

    def setUp(self):
        super().setUp()
        self.set_loglevel(50)
        DIRS.DBFILENAME = "test-ownership-data.db"
        with contextlib.suppress(FileNotFoundError):
            remove_file(DIRS.get_dbfile())
        db_upgrade(upgrade_needed(), restartjobs=False)
        self.qbittorrent = mock.patch.object(download_client, 'qbittorrent').start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        remove_file(DIRS.get_dbfile())
        super().tearDown()

    @staticmethod
    def add_row(origin, category):
        db = DBConnection()
        try:
            db.action("INSERT into wanted (NZBurl, Status, Source, DownloadID, Origin, Category) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      ('http://x/1', 'Seeding', 'QBITTORRENT', HASHID, origin, category))
        finally:
            db.close()

    def test_our_own_torrent_still_in_place(self):
        self.add_row('new', 'books')
        self.qbittorrent.category_matches.return_value = True
        self.assertTrue(may_delete_data('QBITTORRENT', HASHID))
        self.qbittorrent.category_matches.assert_called_once_with(HASHID, 'books')

    def test_our_own_torrent_moved_to_another_category(self):
        # qBittorrent refuses the torrent removal in this case, and the files
        # under it have to be refused with it
        self.add_row('new', 'books')
        self.qbittorrent.category_matches.return_value = False
        self.assertFalse(may_delete_data('QBITTORRENT', HASHID))

    def test_a_client_we_cannot_reach_says_nothing_rather_than_no(self):
        # the difference matters: no means finish up and leave the files, while
        # nothing means ask again next run
        self.add_row('new', 'books')
        self.qbittorrent.category_matches.return_value = None
        self.assertIsNone(may_delete_data('QBITTORRENT', HASHID))

    def test_an_adopted_torrent_never_gets_that_far(self):
        self.add_row('adopted', '')
        self.assertFalse(may_delete_data('QBITTORRENT', HASHID))
        self.qbittorrent.category_matches.assert_not_called()

    def test_a_usenet_download_is_ours_by_definition(self):
        self.assertTrue(may_delete_data('SABNZBD', 'nzo_1'))


if __name__ == '__main__':
    unittest.main()
