#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test the language identifier to make sure it returns sensible results 
#   This test uses examples from GoodReads Agatha Christie books

import os
import unittest

from unittests.unittesthelpers import LLTestCase
from lazylibrarian.bookwork import language_from_words

TESTS = [
    ("Why Didn't they ask Evans?", 'en'),
    ("Cinque romanzi, 1931-1934", 'it'),
    ("N eller M?", 'fr'),
    ("Una broma extraña", 'es'),
    ("Dubbele Detective Het geheim van de blauwe trein Bewijs met de handschoen", 'nl'),
    ("Gutenacht Geschichten Kriminalgeschichten für eine Gänsehaut vor dem Einschlafen", 'de'),
    ("আগাথা ক্রিস্টি সমগ্র ১৬", 'bn'),
    ("", 'None')
]


class LanguageTest(LLTestCase):

    def test_correct_detection(self):
        for title in TESTS:
            lang, confidence = language_from_words(title[0])
            self.assertEqual(lang, title[1])

if __name__ == '__main__':
    unittest.main()
