#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test functions in postprocess.py

import datetime
import logging
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from lazylibrarian.config2 import CONFIG
from lazylibrarian.database import DBConnection
from lazylibrarian.postprocess import (
    BookState,
    _calculate_download_age,
    _calculate_fuzzy_match,
    _check_and_schedule_next_run,
    _count_zipfiles_in_directory,
    _find_valid_file_in_directory,
    _handle_aborted_download,
    _handle_seeding_status,
    _handle_snatched_timeout,
    _is_valid_media_file,
    _normalize_title,
    _process_matched_directory,
    _should_delete_processed_files,
    _tokenize_file,
    _validate_candidate_directory,
    process_dir,
)
from lazylibrarian.postprocess_metadata import (
    BookType,
    prepare_book_metadata,
    prepare_comic_metadata,
    prepare_magazine_metadata,
)
from lazylibrarian.postprocess_utils import (
    enforce_bytes,
    enforce_str,
)
from lazylibrarian.scheduling import SchedulerCommand
from unittests.unittesthelpers import LLTestCaseWithStartup


class BookStateTest(LLTestCaseWithStartup):
    """Test the BookState dataclass"""

    def test_from_db_row_ebook(self):
        """Test creating BookState from a database row for eBook"""
        book_row = {
            "BookID": "12345",
            "NZBtitle": "Test_Book_Title",
            "NZBmode": "nzb",
            "AuxInfo": "",
            "Completed": str(int(time.time())),
            "Source": "SABnzbd",
            "DownloadID": "download123",
            "NZBurl": "http://example.com/book.nzb",
            "NZBprov": "TestProvider",
            "Status": "Snatched",
            "NZBdate": "2025-12-06 10:00:00",
        }

        book_state = BookState.from_db_row(book_row, CONFIG)

        self.assertEqual(book_state.book_id, "12345")
        self.assertEqual(book_state.download_title, "Test Book Title")
        self.assertEqual(book_state.mode_type, "nzb")
        self.assertEqual(book_state.source, "SABnzbd")
        self.assertEqual(book_state.download_id, "download123")
        self.assertEqual(book_state.download_url, "http://example.com/book.nzb")
        self.assertEqual(book_state.download_provider, "TestProvider")
        self.assertEqual(book_state.status, "Snatched")
        self.assertFalse(book_state.is_torrent())
        self.assertTrue(book_state.is_completed())

    def test_from_db_row_torrent(self):
        """Test creating BookState from torrent download"""
        book_row = {
            "BookID": "67890",
            "NZBtitle": "Audiobook Title",
            "NZBmode": "torrent",
            "AuxInfo": "",
            "Completed": str(int(time.time() - 100)),
            "Source": "Deluge",
            "DownloadID": "abc123",
            "NZBurl": "magnet:?xt=urn:btih:...",
            "NZBprov": "TorrentProvider",
            "Status": "Snatched",
            "NZBdate": "",
        }

        book_state = BookState.from_db_row(book_row, CONFIG)

        self.assertEqual(book_state.book_id, "67890")
        self.assertEqual(book_state.mode_type, "torrent")
        self.assertTrue(book_state.is_torrent())
        self.assertTrue(book_state.is_completed())

    def test_is_completed(self):
        """Test completion status checking"""
        book_state = BookState(
            book_id="123", download_title="Test", aux_type="eBook", completed_at=0
        )
        self.assertFalse(book_state.is_completed())

        book_state.completed_at = int(time.time())
        self.assertTrue(book_state.is_completed())

    def test_seconds_since_completion(self):
        """Test elapsed time calculation"""
        book_state = BookState(
            book_id="123",
            download_title="Test",
            aux_type="eBook",
            completed_at=int(time.time() - 50),
        )

        elapsed = book_state.seconds_since_completion()
        self.assertGreaterEqual(elapsed, 50)
        self.assertLess(elapsed, 55)  # Should be ~50 seconds

    def test_should_delay_processing(self):
        """Test processing delay logic"""
        book_state = BookState(
            book_id="123",
            download_title="Test",
            aux_type="eBook",
            completed_at=int(time.time() - 30),
        )

        # Should delay if completed less than 60 seconds ago
        should_delay, elapsed = book_state.should_delay_processing(60)
        self.assertTrue(should_delay)
        self.assertGreaterEqual(elapsed, 30)

        # Should not delay if completed more than 60 seconds ago
        book_state.completed_at = int(time.time() - 100)
        should_delay, elapsed = book_state.should_delay_processing(60)
        self.assertFalse(should_delay)
        self.assertGreaterEqual(elapsed, 100)

    def test_update_candidate(self):
        """Test candidate pointer updates"""
        book_state = BookState(book_id="123", download_title="Test", aux_type="eBook")

        self.assertIsNone(book_state.candidate_ptr)
        self.assertFalse(book_state.has_candidate())

        book_state.update_candidate("/path/to/file.epub")
        self.assertEqual(book_state.candidate_ptr, "/path/to/file.epub")
        self.assertTrue(book_state.has_candidate())

    def test_mark_skipped(self):
        """Test skip status tracking"""
        book_state = BookState(book_id="123", download_title="Test", aux_type="eBook")

        self.assertFalse(book_state.is_skipped())

        book_state.mark_skipped("No valid files found")
        self.assertTrue(book_state.is_skipped())
        self.assertEqual(book_state.skipped_reason, "No valid files found")

    def test_is_book_type(self):
        """Test book type checking"""
        ebook_state = BookState(book_id="123", download_title="Test", aux_type="eBook")
        self.assertTrue(ebook_state.is_book())
        self.assertFalse(ebook_state.is_magazine())

        audio_state = BookState(
            book_id="123", download_title="Test", aux_type="AudioBook"
        )
        self.assertTrue(audio_state.is_book())

        mag_state = BookState(book_id="123", download_title="Test", aux_type="Magazine")
        self.assertFalse(mag_state.is_book())
        self.assertTrue(mag_state.is_magazine())

    def test_get_book_type_str(self):
        """Test booktype string conversion"""
        test_cases = [
            ("ebook", BookType.EBOOK.value),
            ("eBook", BookType.EBOOK.value),
            ("e Book", BookType.EBOOK.value),
            ("e-book", BookType.EBOOK.value),
            ("e_book", BookType.EBOOK.value),
            ("audiobook", BookType.AUDIOBOOK.value),
            ("AudioBook", BookType.AUDIOBOOK.value),
            ("Audio Book", BookType.AUDIOBOOK.value),
            ("audio-book", BookType.AUDIOBOOK.value),
            ("audio Book", BookType.AUDIOBOOK.value),
            ("AudioBook", BookType.AUDIOBOOK.value),
            ("magazine", BookType.MAGAZINE.value),
            ("Magazine", BookType.MAGAZINE.value),
            (" MaGazine ", BookType.MAGAZINE.value),
            ("comic", BookType.COMIC.value),
            (" comic ", BookType.COMIC.value),
            ("Comic", BookType.COMIC.value),
            ("CoMic", BookType.COMIC.value),
        ]

        for aux_type, expected in test_cases:
            book_state = BookState(
                book_id="123", download_title="Test", aux_type=aux_type
            )
            self.assertEqual(book_state.get_book_type_str(), expected)

    def test_can_delete_from_client(self):
        """Test download client deletion validation"""
        # No source
        book_state = BookState(book_id="123", download_title="Test", aux_type="eBook")
        self.assertFalse(book_state.can_delete_from_client())

        # Has source but no download_id
        book_state.source = "SABnzbd"
        self.assertFalse(book_state.can_delete_from_client())

        # Has download_id = "unknown"
        book_state.download_id = "unknown"
        self.assertFalse(book_state.can_delete_from_client())

        # DIRECT source (can't delete)
        book_state.source = "DIRECT"
        book_state.download_id = "123"
        self.assertFalse(book_state.can_delete_from_client())

        # Valid for deletion
        book_state.source = "SABnzbd"
        book_state.download_id = "valid_id"
        self.assertTrue(book_state.can_delete_from_client())

    def test_status_checks(self):
        """Test status checking methods"""
        book_state = BookState(
            book_id="123", download_title="Test", aux_type="eBook", status="Snatched"
        )
        self.assertTrue(book_state.is_snatched())
        self.assertFalse(book_state.is_seeding())
        self.assertFalse(book_state.is_aborted())

        book_state.status = "Seeding"
        self.assertFalse(book_state.is_snatched())
        self.assertTrue(book_state.is_seeding())

        book_state.status = "Aborted"
        self.assertTrue(book_state.is_aborted())

    def test_enrich_with_download_info_ebook(self):
        """Test enrichment with download folder and book title for ebook"""


        db = DBConnection()
        try:
            # Insert test book
            db.action(
                "INSERT OR REPLACE INTO authors (AuthorID, AuthorName) VALUES (?, ?)",
                ("author1", "Test Author"),
            )
            db.action(
                "INSERT OR REPLACE INTO books (BookID, AuthorID, BookName) VALUES (?, ?, ?)",
                ("book123", "author1", "Test Book"),
            )

            book_state = BookState(
                book_id="book123",
                download_title="Test Download",
                aux_type="eBook",
                source="QBITTORRENT",
                download_id="dl123",
            )

            # Mock get_download_folder where it's used (not where it's defined)
            with mock.patch(
                "lazylibrarian.postprocess.get_download_folder"
            ) as mock_get_folder:
                mock_get_folder.return_value = "/downloads/test_folder"

                book_state.enrich_with_download_info(db)

            # Verify enrichment
            self.assertEqual(book_state.download_folder, "/downloads/test_folder")
            self.assertIn("test author", book_state.book_title.lower())
            self.assertIn("test book", book_state.book_title.lower())
        finally:
            db.close()

    def test_enrich_with_download_info_no_folder(self):
        """Test enrichment when client doesn't provide download folder"""

        db = DBConnection()
        try:
            db.action(
                "INSERT OR REPLACE INTO authors (AuthorID, AuthorName) VALUES (?, ?)",
                ("author1", "Test Author"),
            )
            db.action(
                "INSERT OR REPLACE INTO books (BookID, AuthorID, BookName) VALUES (?, ?, ?)",
                ("book123", "author1", "Test Book"),
            )

            book_state = BookState(
                book_id="book123",
                download_title="Test Download",
                aux_type="eBook",
                source="SABNZBD",
                download_id="nzb123",
            )

            # Mock get_download_folder returning None (mock where it's used)
            with mock.patch(
                "lazylibrarian.postprocess.get_download_folder"
            ) as mock_get_folder:
                mock_get_folder.return_value = None

                book_state.enrich_with_download_info(db)

            # Should still get book_title but no download_folder
            self.assertEqual(book_state.download_folder, "")
            self.assertIn("test author", book_state.book_title.lower())
        finally:
            db.close()

    def test_enrich_with_download_info_magazine(self):
        """Test enrichment for magazine gets title"""

        db = DBConnection()
        try:
            db.action(
                "INSERT OR REPLACE INTO magazines (Title) VALUES (?)",
                ("Test Magazine",),
            )

            book_state = BookState(
                book_id="Test Magazine",
                download_title="Test Magazine Download",
                aux_type="Magazine",
                source="",
                download_id="",
            )

            book_state.enrich_with_download_info(db)

            # Should get magazine title
            self.assertIn("test magazine", book_state.book_title.lower())
            self.assertEqual(book_state.download_folder, "")
        finally:
            db.close()


class PostprocessHelperTest(LLTestCaseWithStartup):
    """Test postprocess helper functions"""

    def test_normalize_title(self):
        """Test title normalization"""
        test_cases = [
            ("Test_Book_Title", "Test Book Title"),
            ("Book LL.(12345)", "Book"),
            ("Über_Book_Name", "Uber Book Name"),
            ("  Spaces   Everywhere  ", "Spaces   Everywhere"),
            ("Book/With:Special*Chars", "Book_WithSpecialChars"),
            # Test extension stripping (new behavior)
            ("Book Name.epub", "Book Name"),
            ("Book Name.2013", "Book Name"),
            ("folder.unpack", "folder"),
            ("AudioBook.mp3", "AudioBook"),
            # Test that periods in names are preserved
            ("J.R.R. Tolkien - Book", "J.R.R. Tolkien - Book"),
            ("Dr. Seuss - The Cat", "Dr. Seuss - The Cat"),
            ("H.P. Lovecraft - Story", "H.P. Lovecraft - Story"),
        ]

        for input_title, expected in test_cases:
            result = _normalize_title(input_title)
            self.assertEqual(result, expected, f"Failed for input: {input_title}")

    def test_enforce_str(self):
        """Test enforce_str wrapper"""

        # Test with string (passthrough)
        result = enforce_str("test string")
        self.assertEqual(result, "test string")
        self.assertIsInstance(result, str)

        # Test with ASCII bytes
        result = enforce_str(b"/path/to/file")
        self.assertEqual(result, "/path/to/file")
        self.assertIsInstance(result, str)

        # Test with UTF-8 bytes (special characters)
        result = enforce_str(b"Fran\xc3\xa7ois")  # François in UTF-8
        self.assertIn("Fran", result)
        self.assertIsInstance(result, str)

        # Test with None (should raise)
        with self.assertRaises(ValueError):
            enforce_str(None)

    def test_enforce_bytes(self):
        """Test enforce_bytes wrapper"""

        # Test with string
        result = enforce_bytes("test string")
        self.assertEqual(result, b"test string")
        self.assertIsInstance(result, bytes)

        # Test with bytes (passthrough after validation)
        result = enforce_bytes(b"/path/to/file")
        self.assertEqual(result, b"/path/to/file")
        self.assertIsInstance(result, bytes)

        # Test with special characters
        result = enforce_bytes("François")
        self.assertIsInstance(result, bytes)
        # Should contain UTF-8 encoded version
        self.assertIn(b"Fran", result)

        # Test with None (should raise)
        with self.assertRaises(ValueError):
            enforce_bytes(None)

    def test_tokenize_file(self):
        """Test filename tokenization"""
        test_cases = [
            ("/path/to/book.epub", ("book", "epub")),
            ("file.mp3", ("file", "mp3")),
            ("/no/extension", ("extension", "")),
            ("multiple.dots.in.name.pdf", ("multiple.dots.in.name", "pdf")),
        ]

        for filepath, expected in test_cases:
            stem, ext = _tokenize_file(filepath)
            self.assertEqual((stem, ext), expected)

    def test_calculate_fuzzy_match(self):
        """Test fuzzy matching calculation"""
        # Perfect match
        match = _calculate_fuzzy_match("test book", "test book", None)
        self.assertEqual(match, 100)

        # Partial match (token_set_ratio matches all tokens, so "test" matches "test book" 100%)
        match = _calculate_fuzzy_match("test book", "test", None)
        self.assertEqual(match, 100)  # token_set_ratio behavior

        # Different tokens
        match = _calculate_fuzzy_match("test book", "other", None)
        self.assertLess(match, 100)

        # No match
        match = _calculate_fuzzy_match("completely different", "nothing alike", None)
        self.assertLess(match, 35)

    def test_should_delete_processed_files(self):
        """Test file deletion decision logic"""
        download_dir = "/downloads"

        # .unpack directory - always delete
        should_delete, path = _should_delete_processed_files(
            "/downloads/book.unpack", download_dir
        )
        self.assertTrue(should_delete)
        self.assertIn(".unpack", path)

        # Download root - never delete
        should_delete, path = _should_delete_processed_files("/downloads", download_dir)
        self.assertFalse(should_delete)

    @mock.patch.object(CONFIG, "get_bool")
    def test_should_delete_with_destination_copy(self, mock_get_bool):
        """Test deletion decision with DESTINATION_COPY enabled"""
        mock_get_bool.return_value = True  # DESTINATION_COPY enabled

        should_delete, _ = _should_delete_processed_files(
            "/downloads/subfolder", "/downloads"
        )
        self.assertFalse(should_delete)

    def test_validate_candidate_directory_empty(self):
        """Test validation of empty directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            logger = logging.getLogger(__name__)
            is_valid, reason = _validate_candidate_directory(tmpdir, logger)

            self.assertFalse(is_valid)
            self.assertIn("empty", reason.lower())

    def test_validate_candidate_directory_with_files(self):
        """Test validation of directory with files"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file
            test_file = os.path.join(tmpdir, "test.txt")
            with open(test_file, "w") as f:
                f.write("test")

            logger = logging.getLogger(__name__)
            is_valid, reason = _validate_candidate_directory(tmpdir, logger)

            self.assertTrue(is_valid)
            self.assertEqual(reason, "")


class PostprocessMetadataTest(LLTestCaseWithStartup):
    """Test metadata preparation functions"""

    def test_prepare_book_metadata_not_found(self):
        """Test book metadata when book doesn't exist"""
        db = DBConnection()
        try:
            metadata = prepare_book_metadata("nonexistent_id", "eBook", db)
            self.assertIsNone(metadata)
        finally:
            db.close()

    def test_prepare_magazine_metadata_not_found(self):
        """Test magazine metadata when magazine doesn't exist"""
        db = DBConnection()
        try:
            metadata = prepare_magazine_metadata("nonexistent_title", "2025-01-01", db)
            self.assertIsNone(metadata)
        finally:
            db.close()

    def test_prepare_comic_metadata_invalid_id(self):
        """Test comic metadata with invalid ID format"""
        db = DBConnection()
        try:
            # No underscore in ID
            metadata = prepare_comic_metadata("invalidid", db)
            self.assertIsNone(metadata)

            # Empty ID
            metadata = prepare_comic_metadata("", db)
            self.assertIsNone(metadata)
        finally:
            db.close()


class PostprocessFileOperationsTest(LLTestCaseWithStartup):
    """Test file operation helper functions"""

    def test_tokenize_file_with_path_object(self):
        """Test tokenize_file handles Path objects"""
        test_path = Path("/home/user/books/my_book.epub")
        stem, ext = _tokenize_file(str(test_path))

        self.assertEqual(stem, "my_book")
        self.assertEqual(ext, "epub")

    def test_count_zipfiles_in_empty_directory(self):
        """Test counting zipfiles in empty directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            count = _count_zipfiles_in_directory(tmpdir)
            self.assertEqual(count, 0)

    def test_find_valid_file_in_directory_empty(self):
        """Test finding valid file in empty directory"""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = _find_valid_file_in_directory(tmpdir, book_type="ebook")
            self.assertEqual(result, "")

    @mock.patch("lazylibrarian.postprocess.CONFIG")
    def test_is_valid_media_file(self, mock_config):
        """Test media file validation"""
        mock_config.get_all_types_list.return_value = ["epub", "mobi", "pdf"]
        mock_config.is_valid_booktype.return_value = True

        # Should delegate to CONFIG methods
        result = _is_valid_media_file("test.epub", book_type="ebook")
        self.assertTrue(result)

    def test_process_matched_directory_finds_nested_audiobook(self):
        """Test that _process_matched_directory recurses into subfolders for nested media.

        Torrent clients often place multi-part audiobooks under a year subfolder, e.g.:
          <Title>/
            <Title> (2009)/
              Part1.mp3
        The non-recursive first-pass finds nothing; the fix must recurse before giving up.
        """
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "Good Omens (2009)")
            os.makedirs(sub)
            open(os.path.join(sub, "Good Omens Unabridged~Part1.mp3"), "w").close()

            book_state = BookState(
                book_id="434342", download_title="Good Omens", aux_type="AudioBook"
            )
            book_state.update_candidate(root)

            logger = logging.getLogger("test")
            download_dir = os.path.dirname(root)
            is_valid, skip_reason = _process_matched_directory(
                book_state, download_dir, 100.0, logger, logger
            )

            self.assertTrue(
                is_valid,
                f"expected nested audiobook to validate, got: {skip_reason}",
            )
            self.assertEqual(book_state.candidate_ptr, sub)

    def test_process_matched_directory_nested_ebook_not_recursed(self):
        """Test that the recursive fallback is scoped to audiobooks only.

        The recursive find returns the first valid-extension file with no sample
        filtering or name matching, so eBooks must NOT recurse into subfolders here:
        they keep the original bail and let the archive/collection best-match logic
        handle nested layouts. A nested eBook with no top-level file must fail.
        """
        with tempfile.TemporaryDirectory() as root:
            sub = os.path.join(root, "Some Book (2009)")
            os.makedirs(sub)
            open(os.path.join(sub, "Some Book.epub"), "w").close()

            book_state = BookState(
                book_id="999", download_title="Some Book", aux_type="eBook"
            )
            book_state.update_candidate(root)

            logger = logging.getLogger("test")
            download_dir = os.path.dirname(root)
            is_valid, skip_reason = _process_matched_directory(
                book_state, download_dir, 100.0, logger, logger
            )

            self.assertFalse(
                is_valid,
                "eBooks must not recurse into subfolders in the no-archive fallback",
            )
            self.assertEqual(skip_reason, "No valid file or archives found")


class PostprocessIntegrationTest(LLTestCaseWithStartup):
    """Integration tests for postprocess workflows"""

    def setUp(self):
        super().setUp()
        self.test_dir = tempfile.mkdtemp()
        self.logger = logging.getLogger(__name__)

    def tearDown(self):
        super().tearDown()
        if os.path.exists(self.test_dir):

            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_normalize_and_match_workflow(self):
        """Test complete normalize and fuzzy match workflow"""
        # Simulate a torrent with underscores
        torrent_name = "The_Great_Book_2025"
        expected_name = "The Great Book 2025"

        normalized = _normalize_title(torrent_name)
        self.assertEqual(normalized, "The Great Book 2025")

        # Should match even with different formatting
        match_percent = _calculate_fuzzy_match(
            normalized, _normalize_title(expected_name), None
        )
        self.assertEqual(match_percent, 100)

    def test_validate_directory_workflow(self):
        """Test directory validation workflow"""
        # Empty directory should fail
        empty_dir = os.path.join(self.test_dir, "empty")
        os.makedirs(empty_dir)

        is_valid, reason = _validate_candidate_directory(empty_dir, self.logger)
        self.assertFalse(is_valid)
        self.assertIn("empty", reason.lower())

        # Directory with file should pass
        valid_dir = os.path.join(self.test_dir, "valid")
        os.makedirs(valid_dir)
        with open(os.path.join(valid_dir, "book.epub"), "w") as f:
            f.write("test")

        is_valid, reason = _validate_candidate_directory(valid_dir, self.logger)
        self.assertTrue(is_valid)
        self.assertEqual(reason, "")

    def test_bookstate_complete_workflow(self):
        """Test complete BookState workflow"""
        # Create a book state
        book_row = {
            "BookID": "workflow_test",
            "NZBtitle": "Workflow Test Book",
            "NZBmode": "torrent",
            "AuxInfo": "",
            "Completed": str(int(time.time() - 10)),
            "Source": "Deluge",
            "DownloadID": "dl123",
            "NZBurl": "http://test.com/book",
            "NZBprov": "Provider",
            "Status": "Snatched",
            "NZBdate": "2025-12-06 10:00:00",
        }

        book_state = BookState.from_db_row(book_row, CONFIG)

        # Should be completed
        self.assertTrue(book_state.is_completed())

        # Should not need delay (only 10s elapsed vs typical 60s delay)
        should_delay, _ = book_state.should_delay_processing(5)
        self.assertFalse(should_delay)

        # Should be able to delete from client
        self.assertTrue(book_state.can_delete_from_client())

        # Update candidate
        book_state.update_candidate("/downloads/book.epub")
        self.assertTrue(book_state.has_candidate())

        # Test status checks
        self.assertTrue(book_state.is_snatched())
        self.assertTrue(book_state.is_torrent())


class UnprocessedDownloadsTest(LLTestCaseWithStartup):
    """Test unprocessed download handling functions"""

    def test_calculate_download_age_valid(self):
        """Test age calculation with valid date"""

        # 2 hours 15 minutes ago
        past_date = datetime.datetime.now() - datetime.timedelta(hours=2, minutes=15)
        date_str = past_date.strftime("%Y-%m-%d %H:%M:%S")

        hours, mins, secs = _calculate_download_age(date_str)

        self.assertEqual(hours, 2)
        self.assertGreaterEqual(mins, 134)
        self.assertLessEqual(mins, 136)
        self.assertGreaterEqual(secs, 8040)
        self.assertLessEqual(secs, 8160)

    def test_calculate_download_age_invalid(self):
        """Test age calculation with invalid date"""
        hours, mins, secs = _calculate_download_age("invalid date")
        self.assertEqual((hours, mins, secs), (0, 0, 0))

    def test_handle_seeding_status_torrent_removed(self):
        """Test that torrent removed from client changes status to Snatched"""

        book_state = BookState(
            book_id="book123",
            download_title="Test Book",
            source="TRANSMISSION",
            download_id="dl123",
            progress=-1,  # Not found in client
            finished=False,
            mode_type="torrent",
        )

        mock_db = mock.Mock()
        mock_logger = mock.Mock()

        result = _handle_seeding_status(book_state, False, True, mock_db, mock_logger)

        # Should update status to Snatched
        mock_db.action.assert_called_once()
        call_args = mock_db.action.call_args[0]
        self.assertIn("Snatched", call_args[0])
        self.assertEqual(call_args[1], ("dl123",))

        # Should skip to next item
        self.assertTrue(result)

    @mock.patch("lazylibrarian.postprocess.get_download_folder")
    @mock.patch("lazylibrarian.postprocess.CONFIG.get_bool")
    @mock.patch("lazylibrarian.postprocess.get_list")
    @mock.patch("lazylibrarian.postprocess.now")
    def test_handle_seeding_status_completed(
        self, mock_now, mock_get_list, mock_get_bool, mock_get_download_folder
    ):
        """Test that seeding complete changes status to Processed"""

        mock_now.return_value = "2025-12-06 12:00:00"
        mock_get_bool.return_value = False
        mock_get_download_folder.return_value = "/downloads/book"
        mock_get_list.return_value = []

        book_state = BookState(
            book_id="book123",
            download_title="Test Book",
            source="TRANSMISSION",
            download_id="dl123",
            progress=100,
            finished=True,
            mode_type="torrent",
        )

        mock_db = mock.Mock()
        mock_logger = mock.Mock()

        result = _handle_seeding_status(book_state, False, True, mock_db, mock_logger)

        # Should update status to Processed
        self.assertTrue(
            any("Processed" in str(call) for call in mock_db.action.call_args_list)
        )
        self.assertTrue(result)

    def test_handle_snatched_timeout_slow_magnet(self):
        """Test that downloads < 30 mins with no progress are not aborted"""

        book_state = BookState(
            download_title="Test Book",
            source="QBITTORRENT",
            download_id="dl123",
            progress=-1,
            aux_type="eBook",
            book_id="book123",
        )

        mock_logger = mock.Mock()

        should_abort, should_skip = _handle_snatched_timeout(
            book_state, 0, 25, 24, mock_logger
        )

        self.assertFalse(should_abort)
        self.assertFalse(should_skip)

    def test_handle_snatched_timeout_missing_after_30min(
        self,
    ):
        """Test that downloads > 30 mins with no progress are aborted"""

        book_state = BookState(
            download_title="Test Book",
            source="QBITTORRENT",
            download_id="dl123",
            progress=-1,
            aux_type="eBook",
            book_id="book123",
        )

        mock_logger = mock.Mock()

        should_abort, should_skip = _handle_snatched_timeout(
            book_state, 1, 35, 24, mock_logger
        )

        self.assertTrue(should_abort)
        self.assertFalse(should_skip)

    def test_handle_snatched_timeout_100_percent_no_abort(self):
        """Test that 100% complete timed-out downloads are not aborted"""

        book_state = BookState(
            download_title="Test Book",
            source="QBITTORRENT",
            download_id="dl123",
            progress=100,
            aux_type="eBook",
            book_id="book123",
        )

        mock_logger = mock.Mock()

        should_abort, should_skip = _handle_snatched_timeout(
            book_state, 25, 1500, 24, mock_logger
        )

        # Should not abort 100% downloads, let them retry
        self.assertFalse(should_abort)
        self.assertFalse(should_skip)

    def test_handle_snatched_timeout_stuck_at_90(self):
        """Test that downloads stuck < 95% after timeout are aborted"""

        book_state = BookState(
            download_title="Test Book",
            source="QBITTORRENT",
            download_id="dl123",
            progress=90,
            aux_type="eBook",
            book_id="book123",
        )

        mock_logger = mock.Mock()

        should_abort, should_skip = _handle_snatched_timeout(
            book_state, 25, 1500, 24, mock_logger
        )

        self.assertTrue(should_abort)
        self.assertFalse(should_skip)

    def test_handle_snatched_timeout_95_99_gets_extra_hour(self):
        """Test that downloads at 95-99% get an extra hour"""

        book_state = BookState(
            download_title="Test Book",
            source="QBITTORRENT",
            download_id="dl123",
            progress=97,
            aux_type="eBook",
            book_id="book123",
        )

        mock_logger = mock.Mock()

        # Just at timeout
        should_abort, _ = _handle_snatched_timeout(
            book_state, 24, 1440, 24, mock_logger
        )
        self.assertFalse(should_abort)  # Don't abort yet

        # Past extra hour
        should_abort, _ = _handle_snatched_timeout(
            book_state, 25, 1500, 24, mock_logger
        )
        self.assertTrue(should_abort)  # Now abort

    @mock.patch("lazylibrarian.postprocess.custom_notify_snatch")
    @mock.patch("lazylibrarian.postprocess.notify_snatch")
    @mock.patch("lazylibrarian.postprocess.CONFIG.get_bool")
    def test_handle_aborted_download_sends_notifications(
        self, mock_get_bool, mock_notify, mock_custom
    ):
        """Test that aborted downloads send failure notifications"""
        mock_get_bool.return_value = False  # DEL_FAILED = False

        book_state = BookState(
            book_id="book123",
            download_title="Test Book",
            source="TRANSMISSION",
            download_id="dl123",
            status="Aborted",
            aux_type="eBook",
            download_url="http://test.com/book.nzb",
            progress=50,
        )

        mock_db = mock.Mock()
        mock_logger = mock.Mock()

        _handle_aborted_download(book_state, 12, mock_db, mock_logger)

        # Should send failure notifications
        mock_custom.assert_called_once_with("book123 TRANSMISSION", fail=True)
        self.assertTrue(mock_notify.called)

    @mock.patch("lazylibrarian.postprocess.CONFIG.get_bool")
    def test_handle_aborted_download_updates_status(
        self, mock_get_bool
    ):
        """Test that aborted downloads update status to Failed"""
        mock_get_bool.return_value = False

        book_state = BookState(
            book_id="book123",
            download_title="Test Book",
            source="TRANSMISSION",
            download_id="dl123",
            status="Aborted",
            aux_type="eBook",
            download_url="http://test.com/book.nzb",
            progress=50,
        )

        mock_db = mock.Mock()
        mock_logger = mock.Mock()

        _handle_aborted_download(book_state, 12, mock_db, mock_logger)

        # Should update database
        self.assertTrue(mock_db.action.called)
        # Should update wanted to Failed
        calls = [str(call) for call in mock_db.action.call_args_list]
        self.assertTrue(any("Failed" in call for call in calls))

    @mock.patch("lazylibrarian.postprocess.delete_task")
    @mock.patch("lazylibrarian.postprocess.CONFIG.get_bool")
    def test_handle_aborted_download_deletes_if_configured(
        self, mock_get_bool, mock_delete_task
    ):
        """Test that when DEL_FAILED=True, aborted downloads are deleted from client"""

        mock_get_bool.return_value = True  # DEL_FAILED = True

        book_state = BookState(
            book_id="book123",
            download_title="Test Book",
            source="QBITTORRENT",
            download_id="dl123",
            status="Aborted",
            aux_type="eBook",
            download_url="http://test.com/book.nzb",
            progress=50,
        )

        mock_db = mock.Mock()
        mock_logger = mock.Mock()

        _handle_aborted_download(book_state, 12, mock_db, mock_logger)

        # Should delete from client
        mock_delete_task.assert_called_once_with("QBITTORRENT", "dl123", True)

    @mock.patch("lazylibrarian.postprocess.schedule_job")
    def test_check_and_schedule_next_run_stop_when_empty(self, mock_schedule):
        """Test that postprocessor stops when no items remain"""
        mock_db = mock.Mock()
        mock_db.select.return_value = []  # No snatched or seeding
        mock_logger = mock.Mock()

        _check_and_schedule_next_run(mock_db, mock_logger, False)

        # Should stop the postprocessor
        mock_schedule.assert_called_once()
        call_args = mock_schedule.call_args
        self.assertEqual(call_args[0][0], SchedulerCommand.STOP)

    @mock.patch("lazylibrarian.postprocess.schedule_job")
    def test_check_and_schedule_next_run_restart_when_seeding(self, mock_schedule):
        """Test that postprocessor restarts when items are seeding"""
        mock_db = mock.Mock()
        mock_db.select.side_effect = [
            [],  # No snatched
            [{"BookID": "123"}],  # Has seeding
        ]
        mock_logger = mock.Mock()

        _check_and_schedule_next_run(mock_db, mock_logger, False)

        # Should restart the postprocessor
        mock_schedule.assert_called_once()
        call_args = mock_schedule.call_args
        self.assertEqual(call_args[0][0], SchedulerCommand.RESTART)


class ProcessDirEndToEndTest(LLTestCaseWithStartup):
    """End-to-end tests for process_dir with real file operations"""

    def setUp(self):
        super().setUp()
        # Create temporary directories
        self.test_root = tempfile.mkdtemp()
        self.download_dir = os.path.join(self.test_root, "downloads")
        self.library_dir = os.path.join(self.test_root, "library")
        self.audio_library = os.path.join(self.test_root, "audiobooks")

        os.makedirs(self.download_dir)
        os.makedirs(self.library_dir)
        os.makedirs(self.audio_library)

        # Save original config values
        self.original_download_dir = CONFIG["DOWNLOAD_DIR"]
        self.original_ebook_dir = CONFIG["EBOOK_DIR"]
        self.original_audio_dir = CONFIG["AUDIO_DIR"]
        self.original_dest_copy = CONFIG["DESTINATION_COPY"]
        self.original_pp_delay = CONFIG["PP_DELAY"]

        # Configure test directories
        CONFIG["DOWNLOAD_DIR"] = self.download_dir
        CONFIG["EBOOK_DIR"] = self.library_dir
        CONFIG["AUDIO_DIR"] = self.audio_library
        # Note: DESTINATION_COPY is a ConfigBool and cannot be set via dictionary access
        # Tests that need to change it should mock CONFIG.get_bool() instead
        CONFIG.set_int("PP_DELAY", 0)  # Use set_int for integer configs

        self.db = DBConnection()

    def tearDown(self):
        super().tearDown()
        self.db.close()

        # Restore original config values
        CONFIG["DOWNLOAD_DIR"] = self.original_download_dir
        CONFIG["EBOOK_DIR"] = self.original_ebook_dir
        CONFIG["AUDIO_DIR"] = self.original_audio_dir
        CONFIG["DESTINATION_COPY"] = self.original_dest_copy
        CONFIG["PP_DELAY"] = self.original_pp_delay

        # Clean up test directories
        if os.path.exists(self.test_root):
            shutil.rmtree(self.test_root, ignore_errors=True)

    def create_test_author_and_book(self, book_id, author_name, book_name):
        """Create test author and book in database"""
        author_id = str(uuid.uuid4())

        self.db.action(
            "INSERT OR REPLACE INTO authors (AuthorID, AuthorName, Status) VALUES (?, ?, ?)",
            (author_id, author_name, "Active")
        )

        self.db.action(
            """INSERT OR REPLACE INTO books
               (BookID, AuthorID, BookName, BookDate, BookDesc, BookGenre, BookLang)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (book_id, author_id, book_name, "2024-01-01", "Test book", "Fiction", "en")
        )

        return author_id, book_id

    def create_snatched_download(self, book_id, download_title, book_type="eBook",
                                source="SABnzbd", download_id=None):
        """Create a snatched download entry"""
        if download_id is None:
            download_id = str(uuid.uuid4())

        completed_time = int(time.time())

        self.db.action(
            """INSERT OR REPLACE INTO wanted
               (BookID, NZBtitle, NZBmode, AuxInfo, Completed, Source, DownloadID,
                Status, NZBurl, NZBprov, NZBdate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (book_id, download_title, "nzb", book_type, completed_time, source,
             download_id, "Snatched", "http://test.com/book.nzb", "TestProvider",
             datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )

        return download_id

    def create_ebook_file(self, filepath, content="Test ebook content"):
        """Create a fake ebook file"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w') as f:
            f.write(content)
        return filepath

    def create_audiobook_files(self, directory, num_files=10):
        """Create multiple audio files for audiobook"""
        os.makedirs(directory, exist_ok=True)
        files = []
        for i in range(1, num_files + 1):
            filepath = os.path.join(directory, f"chapter_{i:02d}.mp3")
            with open(filepath, 'w') as f:
                f.write(f"Audio chapter {i}")
            files.append(filepath)
        return files

    def create_zip_archive(self, zip_path, files_dict):
        """Create a zip archive with specified files

        Args:
            zip_path: Path to create zip file
            files_dict: Dict of {filename: content} to add to zip
        """
        os.makedirs(os.path.dirname(zip_path), exist_ok=True)
        with zipfile.ZipFile(zip_path, 'w') as zf:
            for filename, content in files_dict.items():
                zf.writestr(filename, content)
        return zip_path

    def assert_file_in_library(self, author, book_name, extension=".epub", library_type="ebook"):
        """Helper to assert file exists in correct library location"""
        if library_type == "ebook":
            base_dir = self.library_dir
        elif library_type == "audio":
            base_dir = self.audio_library
        else:
            raise ValueError(f"Unknown library type: {library_type}")

        expected = os.path.join(base_dir, author, book_name + extension)
        self.assertTrue(os.path.exists(expected),
                       f"Expected file at {expected}")
        return expected

    def assert_status_updated(self, book_id, expected_status):
        """Helper to verify database status was updated"""
        result = self.db.match("SELECT Status FROM wanted WHERE BookID=?", (book_id,))
        self.assertIsNotNone(result, f"Book {book_id} not found in wanted table")
        self.assertEqual(result['Status'], expected_status,
                        f"Expected status {expected_status}, got {result['Status']}")

    @mock.patch('lazylibrarian.postprocess.check_contents')
    @mock.patch('lazylibrarian.postprocess.get_download_progress')
    @mock.patch('lazylibrarian.postprocess.get_download_name')
    def test_process_single_epub_flat_structure(self, mock_get_name, mock_get_progress, mock_check_contents):
        """Test processing a single EPUB in flat download structure"""
        # Mock download client functions
        mock_check_contents.return_value = None  # No rejection
        mock_get_progress.return_value = (100, True)  # Complete
        mock_get_name.return_value = None  # No name change

        # Setup
        book_id = "test_book_001"
        author_name = "John Doe"
        book_name = "Test Book One"
        download_title = "John_Doe-Test_Book_One"

        # Create database entries
        self.create_test_author_and_book(book_id, author_name, book_name)
        self.create_snatched_download(book_id, download_title, "eBook")

        # Create download file
        download_file = os.path.join(self.download_dir, "John_Doe-Test_Book_One.epub")
        self.create_ebook_file(download_file)

        # Execute
        process_dir(ignoreclient=True)

        # Verify
        # 1. File moved to library (in a book-specific directory)
        author_dir = os.path.join(self.library_dir, author_name)
        self.assertTrue(os.path.exists(author_dir), f"Author directory should exist at {author_dir}")

        # The postprocessor creates: Author/BookName/BookName - Author.epub
        book_dir = os.path.join(author_dir, book_name)
        self.assertTrue(os.path.exists(book_dir), f"Book directory should exist at {book_dir}")

        # Check for the epub file (name format: "BookName - Author.epub")
        epub_files = [f for f in os.listdir(book_dir) if f.endswith('.epub')]
        self.assertEqual(len(epub_files), 1, f"Should have exactly 1 epub file, found {epub_files}")
        self.assertIn(book_name, epub_files[0], "EPUB filename should contain book name")

        # Check that metadata.opf was created
        opf_files = [f for f in os.listdir(book_dir) if f.endswith('.opf')]
        self.assertEqual(len(opf_files), 1, "Should have metadata.opf file")

        # 2. File removed from download dir (moved, not copied)
        self.assertFalse(os.path.exists(download_file),
                        "Original file should be moved (not copied)")

        # 3. Database updated
        self.assert_status_updated(book_id, 'Processed')

    @mock.patch('lazylibrarian.postprocess._is_valid_media_file')
    @mock.patch('lazylibrarian.postprocess.CONFIG.get_bool')
    @mock.patch('lazylibrarian.postprocess.check_contents')
    @mock.patch('lazylibrarian.postprocess.get_download_progress')
    @mock.patch('lazylibrarian.postprocess.get_download_name')
    def test_process_audiobook_collection_with_subdirs(self, mock_get_name, mock_get_progress, mock_check_contents, mock_get_bool, mock_is_valid_media):
        """Test processing audiobook from collection with subdirectories

        Tests the drill-down matching functionality for collections like:
        Lord of the Rings Trilogy/
          |-- Lord of the Rings 1 - Fellowship of the Ring/
          |-- Lord of the Rings 2 - The Two Towers/
          |-- Lord of the Rings 3 - Return of the King/
        """
        # Mock download client functions
        mock_check_contents.return_value = None  # No rejection
        mock_get_progress.return_value = (100, True)  # Complete
        mock_get_name.return_value = None  # No name change

        # Mock file validation to accept our empty test files
        mock_is_valid_media.return_value = True  # All files are valid

        # Mock CONFIG.get_bool to ensure audiobooks are enabled
        def get_bool_side_effect(key):
            return key == "AUDIO_TAB"

        mock_get_bool.side_effect = get_bool_side_effect

        # Setup
        book_id = "audio_002"
        author_name = "J.R.R. Tolkien"
        book_name = "Lord of the Rings: The Two Towers"  # Series name in book title
        download_title = "J.R.R._Tolkien-Lord_of_the_Rings_Trilogy"

        self.create_test_author_and_book(book_id, author_name, book_name)
        self.create_snatched_download(book_id, download_title, "AudioBook")

        # Create collection structure
        collection_root = os.path.join(self.download_dir, "J.R.R._Tolkien-Lord_of_the_Rings_Trilogy")

        # Create subdirectories for each book
        book1_dir = os.path.join(collection_root, "Lord of the Rings 1 - Fellowship of the Ring")
        book2_dir = os.path.join(collection_root, "Lord of the Rings 2 - The Two Towers")
        book3_dir = os.path.join(collection_root, "Lord of the Rings 3 - Return of the King")

        self.create_audiobook_files(book1_dir, num_files=35)
        self.create_audiobook_files(book2_dir, num_files=30)  # This is the one we want
        self.create_audiobook_files(book3_dir, num_files=38)

        # Execute
        process_dir(ignoreclient=True)

        # Verify
        # 1. Correct book extracted to audio library
        author_dir = os.path.join(self.audio_library, author_name)
        self.assertTrue(os.path.exists(author_dir), f"Author directory should exist at {author_dir}")

        # The book directory name will be sanitized (colons removed)
        # "Lord of the Rings: The Two Towers" → "Lord of the Rings The Two Towers"
        book_dirs = os.listdir(author_dir)
        self.assertEqual(len(book_dirs), 1, f"Should have exactly 1 book directory, found: {book_dirs}")

        book_dir = os.path.join(author_dir, book_dirs[0])
        self.assertIn("Two Towers", book_dirs[0], "Book directory should contain 'Two Towers'")

        # 2. Verify it has the right number of audio files
        audio_files = [f for f in os.listdir(book_dir) if f.endswith('.mp3')]
        self.assertEqual(len(audio_files), 30,
                        "Should have exactly 30 audio files from The Two Towers")

        # 3. Other books should NOT be processed
        # Verify only 1 book directory exists (not all 3 from the trilogy)
        self.assertEqual(len(book_dirs), 1,
                        "Should only process The Two Towers, not all 3 books in trilogy")

        # 4. Database updated
        self.assert_status_updated(book_id, 'Processed')

    @mock.patch('lazylibrarian.postprocess.check_contents')
    @mock.patch('lazylibrarian.postprocess.get_download_progress')
    @mock.patch('lazylibrarian.postprocess.get_download_name')
    def test_process_epub_in_zip(self, mock_get_name, mock_get_progress, mock_check_contents):
        """Test extracting and processing EPUB from ZIP archive"""
        # Mock download client functions
        mock_check_contents.return_value = None  # No rejection
        mock_get_progress.return_value = (100, True)  # Complete
        mock_get_name.return_value = None  # No name change

        # Setup
        book_id = "test_book_003"
        author_name = "Jane Smith"
        book_name = "Archive Test"
        download_title = "Jane_Smith-Archive_Test"

        self.create_test_author_and_book(book_id, author_name, book_name)
        self.create_snatched_download(book_id, download_title, "eBook")

        # Create ZIP with EPUB inside in a matching folder
        # The postprocessor looks for directories matching the download title
        download_folder = os.path.join(self.download_dir, "Jane_Smith-Archive_Test")
        os.makedirs(download_folder)
        zip_path = os.path.join(download_folder, "book.zip")
        self.create_zip_archive(zip_path, {
            "Jane_Smith-Archive_Test.epub": "Ebook content here",
            "cover.jpg": "Image data",
            "metadata.opf": "<metadata></metadata>"
        })

        # Execute
        process_dir(ignoreclient=True)

        # Verify
        # Archive should be extracted and book moved to library
        author_dir = os.path.join(self.library_dir, author_name)
        self.assertTrue(os.path.exists(author_dir), f"Author directory should exist at {author_dir}")

        book_dir = os.path.join(author_dir, book_name)
        self.assertTrue(os.path.exists(book_dir), f"Book directory should exist at {book_dir}")

        # Check for the epub file
        epub_files = [f for f in os.listdir(book_dir) if f.endswith('.epub')]
        self.assertGreaterEqual(len(epub_files), 1, "Should have at least 1 epub file after extraction")

        # Database should be updated
        self.assert_status_updated(book_id, 'Processed')

    @mock.patch('lazylibrarian.postprocess.CONFIG.get_bool')
    @mock.patch('lazylibrarian.postprocess.check_contents')
    @mock.patch('lazylibrarian.postprocess.get_download_progress')
    @mock.patch('lazylibrarian.postprocess.get_download_name')
    def test_process_with_destination_copy_enabled(self, mock_get_name, mock_get_progress, mock_check_contents, mock_get_bool):
        """Test that DESTINATION_COPY copies instead of moving files"""
        # Mock download client functions
        mock_check_contents.return_value = None  # No rejection
        mock_get_progress.return_value = (100, True)  # Complete
        mock_get_name.return_value = None  # No name change

        # Mock CONFIG.get_bool to return True for DESTINATION_COPY
        def get_bool_side_effect(key):
            if key == "DESTINATION_COPY":
                return True
            if key == "KEEP_SEEDING":
                return False
            return False
        mock_get_bool.side_effect = get_bool_side_effect

        # Setup
        book_id = "test_book_004"
        author_name = "Copy Test"
        book_name = "Copy Mode Test"
        download_title = "Copy_Test-Copy_Mode_Test"

        self.create_test_author_and_book(book_id, author_name, book_name)
        self.create_snatched_download(book_id, download_title, "eBook")

        download_file = os.path.join(self.download_dir, "Copy_Test-Copy_Mode_Test.epub")
        self.create_ebook_file(download_file)

        # Execute
        process_dir(ignoreclient=True)  # Ignore download client checks since we're testing file processing

        # Verify
        # 1. File copied to library (in book directory structure)
        author_dir = os.path.join(self.library_dir, author_name)
        self.assertTrue(os.path.exists(author_dir), "Author directory should exist")

        book_dir = os.path.join(author_dir, book_name)
        self.assertTrue(os.path.exists(book_dir), "Book directory should exist")

        epub_files = [f for f in os.listdir(book_dir) if f.endswith('.epub')]
        self.assertEqual(len(epub_files), 1, "Should have exactly 1 epub file")

        # 2. Original file STILL in download dir (because DESTINATION_COPY is enabled)
        self.assertTrue(os.path.exists(download_file),
                       "Original file should remain when DESTINATION_COPY is True")

    @mock.patch('lazylibrarian.postprocess.check_contents')
    @mock.patch('lazylibrarian.postprocess.get_download_progress')
    @mock.patch('lazylibrarian.postprocess.get_download_name')
    def test_process_file_in_download_root_with_unpack_isolation(self, mock_get_name, mock_get_progress, mock_check_contents):
        """Test that files in download root are isolated to .unpack directory to protect other files

        When a single file exists in the download root alongside other unrelated files,
        the postprocessor should:
        1. Create a .unpack subdirectory
        2. Copy/move only files matching this book's name to .unpack
        3. Process from .unpack (protecting other files in root)
        4. Clean up .unpack after processing
        """
        # Mock download client functions
        mock_check_contents.return_value = None  # No rejection
        mock_get_progress.return_value = (100, True)  # Complete
        mock_get_name.return_value = None  # No name change

        # Setup
        book_id = "test_book_005"
        author_name = "Isolation Test"
        book_name = "Root File Test"
        download_title = "Isolation_Test-Root_File_Test"

        self.create_test_author_and_book(book_id, author_name, book_name)
        self.create_snatched_download(book_id, download_title, "eBook")

        # Create the target book file in download ROOT
        target_file = os.path.join(self.download_dir, "Isolation_Test-Root_File_Test.epub")
        self.create_ebook_file(target_file, "Target book content")

        # Create other unrelated files in download root that should be protected
        other_file1 = os.path.join(self.download_dir, "Other_Book.epub")
        other_file2 = os.path.join(self.download_dir, "Random_File.txt")
        self.create_ebook_file(other_file1, "Other book content")
        self.create_ebook_file(other_file2, "Random content")

        # Create a matching metadata file (should be isolated with the book)
        cover_file = os.path.join(self.download_dir, "Isolation_Test-Root_File_Test.jpg")
        self.create_ebook_file(cover_file, "Cover image")

        # Execute
        process_dir(ignoreclient=True)

        # Verify
        # 1. Book processed to library
        author_dir = os.path.join(self.library_dir, author_name)
        self.assertTrue(os.path.exists(author_dir), "Author directory should exist")

        book_dirs = os.listdir(author_dir)
        self.assertEqual(len(book_dirs), 1, "Should have 1 book directory")

        book_dir = os.path.join(author_dir, book_dirs[0])
        epub_files = [f for f in os.listdir(book_dir) if f.endswith('.epub')]
        self.assertEqual(len(epub_files), 1, "Should have the processed epub")

        # 2. Target file and matching files removed from root
        self.assertFalse(os.path.exists(target_file), "Target file should be moved from root")
        self.assertFalse(os.path.exists(cover_file), "Cover file should be moved with book")

        # 3. OTHER FILES PROTECTED - should still exist in download root
        self.assertTrue(os.path.exists(other_file1), "Other book file should NOT be touched")
        self.assertTrue(os.path.exists(other_file2), "Random file should NOT be touched")

        # 4. .unpack directory cleaned up
        if os.path.exists(self.download_dir):
            unpack_dirs = [d for d in os.listdir(self.download_dir) if '.unpack' in d]
            self.assertEqual(len(unpack_dirs), 0, ".unpack directory should be cleaned up after processing")

        # 5. Database updated
        self.assert_status_updated(book_id, 'Processed')

    @mock.patch('lazylibrarian.postprocess._is_valid_media_file')
    @mock.patch('lazylibrarian.postprocess.CONFIG.get_bool')
    @mock.patch('lazylibrarian.postprocess.check_contents')
    @mock.patch('lazylibrarian.postprocess.get_download_progress')
    @mock.patch('lazylibrarian.postprocess.get_download_name')
    def test_audiobook_in_download_root_does_not_copy_everything(
        self, mock_get_name, mock_get_progress, mock_check_contents,
        mock_get_bool, mock_is_valid_media
    ):
        """Regression test for #2901: loose audiobook in download root must not
        cause copy_tree to copy the entire download directory.

        When candidate_ptr is the download root itself and book_file(recurse=True)
        finds a .m4b there, pp_path becomes the download root and copy_tree copies
        everything in it.  The guard should reject the download root as a candidate
        before book_file ever runs.
        """
        mock_check_contents.return_value = None
        mock_get_progress.return_value = (100, True)
        mock_get_name.return_value = None
        mock_is_valid_media.return_value = True

        def get_bool_side_effect(key):
            if key == "AUDIO_TAB":
                return True
            if key == "DESTINATION_COPY":
                return True
            return False

        mock_get_bool.side_effect = get_bool_side_effect

        book_id = "audio_root_001"
        author_name = "Joe Dunthorne"
        book_name = "Children of Radium"
        download_title = "Joe_Dunthorne-Children_of_Radium"

        self.create_test_author_and_book(book_id, author_name, book_name)
        self.create_snatched_download(book_id, download_title, "AudioBook")

        # Place a loose .m4b in the download root (no subfolder)
        loose_m4b = os.path.join(self.download_dir, "Children of Radium.m4b")
        with open(loose_m4b, 'w') as f:
            f.write("audiobook data")

        # Other unrelated files that must survive
        other_epub = os.path.join(self.download_dir, "Unrelated_Book.epub")
        with open(other_epub, 'w') as f:
            f.write("unrelated ebook")

        process_dir(ignoreclient=True)

        # The unrelated file must still be in the download root untouched
        self.assertTrue(
            os.path.exists(other_epub),
            "Unrelated files in download root must not be copied or moved"
        )
