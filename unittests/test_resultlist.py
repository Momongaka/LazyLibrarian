#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test functions in resultlist.py

from unittest import mock

from lazylibrarian.config2 import CONFIG
from lazylibrarian.resultlist import find_best_result
from unittests.unittesthelpers import LLTestCaseWithStartup


def _make_tor_result(title, url="http://test.com/book.torrent", size="50000000",
                     prov="TestProvider", priority=0):
    return {
        "tor_title": title,
        "tor_url": url,
        "tor_size": size,
        "tor_prov": prov,
        "tor_type": "torrent",
        "priority": priority,
    }


def _make_book(author, title, bookid="test_001", library="eBook"):
    return {
        "authorName": author,
        "bookName": title,
        "bookid": bookid,
        "library": library,
    }


class LanguageRejectionTest(LLTestCaseWithStartup):

    def setUp(self):
        super().setUp()
        self.original_preflang = CONFIG["IMP_PREFLANG"]

    def tearDown(self):
        CONFIG["IMP_PREFLANG"] = self.original_preflang
        super().tearDown()

    def test_rejects_non_preferred_language_tag(self):
        CONFIG["IMP_PREFLANG"] = "en, eng, English"
        book = _make_book("Some Author", "Some Book")
        results = [_make_tor_result("Some Author - Some Book [SWE]")]

        match = find_best_result(results, book, "book", "tor")
        self.assertIsNone(match, "Swedish release should be rejected for English preference")

    def test_accepts_preferred_language_tag(self):
        CONFIG["IMP_PREFLANG"] = "en, eng, English"
        book = _make_book("Some Author", "Some Book")
        results = [_make_tor_result("Some Author - Some Book [ENG]")]

        match = find_best_result(results, book, "book", "tor")
        self.assertIsNotNone(match, "English release should be accepted")

    def test_german_wife_not_rejected(self):
        """Token boundaries: 'German' in 'The German Wife' is part of the
        title, not a language indicator."""
        CONFIG["IMP_PREFLANG"] = "en, eng, English"
        book = _make_book("Pam Jenoff", "The German Wife")
        results = [_make_tor_result("Pam Jenoff - The German Wife epub")]

        match = find_best_result(results, book, "book", "tor")
        self.assertIsNotNone(match, "'German' in the book title should not trigger rejection")

    def test_language_all_disables_filter(self):
        CONFIG["IMP_PREFLANG"] = "All"
        book = _make_book("Some Author", "Some Book")
        results = [_make_tor_result("Some Author - Some Book [SWE]")]

        match = find_best_result(results, book, "book", "tor")
        self.assertIsNotNone(match, "IMP_PREFLANG=All should accept any language")

    def test_rejects_full_language_name(self):
        CONFIG["IMP_PREFLANG"] = "en, eng, English"
        book = _make_book("Some Author", "Some Book")
        results = [_make_tor_result("Some Author - Some Book Swedish Edition")]

        match = find_best_result(results, book, "book", "tor")
        self.assertIsNone(match, "Full language name 'Swedish' should trigger rejection")

    def test_three_letter_code_in_brackets(self):
        CONFIG["IMP_PREFLANG"] = "en, eng, English"
        book = _make_book("Some Author", "Some Book")
        results = [_make_tor_result("Some Author - Some Book [FRE]")]

        match = find_best_result(results, book, "book", "tor")
        self.assertIsNone(match, "French 3-letter code should be rejected for English preference")

    def test_author_name_matching_language_not_rejected(self):
        """An author named 'Danish' should not trigger language rejection."""
        CONFIG["IMP_PREFLANG"] = "en, eng, English"
        book = _make_book("Barbara Danish", "The Puzzle")
        results = [_make_tor_result("Barbara Danish - The Puzzle epub")]

        match = find_best_result(results, book, "book", "tor")
        self.assertIsNotNone(match, "'Danish' in author name should not trigger rejection")


class CrossFormatRejectionTest(LLTestCaseWithStartup):

    def test_audiobook_format_rejected_for_ebook_search(self):
        book = _make_book("Some Author", "Some Book", library="eBook")
        results = [_make_tor_result("Some Author - Some Book m4b")]
        match = find_best_result(results, book, "book", "tor")
        self.assertIsNone(match, "m4b-only result should be rejected for eBook search")

    def test_ebook_format_rejected_for_audiobook_search(self):
        book = _make_book("Some Author", "Some Book", library="AudioBook")
        results = [_make_tor_result("Some Author - Some Book epub")]
        match = find_best_result(results, book, "audiobook", "tor")
        self.assertIsNone(match, "epub-only result should be rejected for AudioBook search")

    def test_correct_format_not_rejected(self):
        """epub in an eBook search should pass the cross-format check."""
        book = _make_book("Some Author", "Some Book", library="eBook")
        results = [_make_tor_result("Some Author - Some Book epub")]
        match = find_best_result(results, book, "book", "tor")
        self.assertIsNotNone(match, "Correct format should not be rejected")

    def test_no_format_tokens_not_rejected(self):
        book = _make_book("Some Author", "Some Book", library="eBook")
        results = [_make_tor_result("Some Author - Some Book")]
        match = find_best_result(results, book, "book", "tor")
        self.assertIsNotNone(match, "No format tokens should not trigger rejection")
