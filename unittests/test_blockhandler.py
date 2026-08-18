#  This file is part of Lazylibrarian.
#
# Purpose:
#   Test functions in blockhandler.py

import time
import logging

from unittests.unittesthelpers import LLTestCase
from lazylibrarian.config2 import LLConfigHandler
from lazylibrarian.configarray import ArrayConfig
from lazylibrarian.configdefs import BASE_DEFAULTS
from lazylibrarian.blockhandler import BlockHandler


class BlockhandlerTest(LLTestCase):

    def test_set_config(self):
        # Create a new, empty handler for this
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        handler = BlockHandler()
        self.assertIsNone(handler._config)
        self.assertIsNone(handler._newznab)
        self.assertIsNone(handler._torznab)
        handler.set_config(cfg, cfg.providers("NEWZNAB"), cfg.providers("TORZNAB"))
        self.assertIsInstance(handler._config, LLConfigHandler)
        self.assertIsInstance(handler._newznab, ArrayConfig)
        self.assertIsInstance(handler._torznab, ArrayConfig)
        self.assertNotEqual(handler._torznab, handler._newznab)

    def test_remove_provider_entry(self):
        name = 'test'
        handler = BlockHandler()
        with self.assertLogs(self.logger, 'DEBUG'):
            _ = handler.block_provider(name, 'blocked', delay=120)
        self.assertTrue(handler.is_blocked(name), 'Expected this to be blocked')
        self.assertEqual(handler.number_blocked(), 1)

        handler.remove_provider_entry(name)
        self.assertFalse(handler.is_blocked(name))
        self.assertEqual(handler.number_blocked(), 0)

    def test_block_provider(self):
        self.set_loglevel(logging.DEBUG)
        handler = BlockHandler()
        name = 'someone'
        with self.assertLogs(self.logger, 'DEBUG') as cm:
            delay = handler.block_provider(name, 'just because', delay=120)  # Block for 2 minutes
        self.assertListEqual(cm.output, [
            'INFO:lazylibrarian.blockhandler:Blocking provider someone for 2 minutes because just because',
            'DEBUG:lazylibrarian.blockhandler:Provider Blocklist contains 1 entry'
        ])
        self.assertTrue(handler.is_blocked(name), 'Expected this to be blocked')
        self.assertEqual(delay, 120, 'Timeout value is unexpected')

        # Repeat the same block with a shorter time
        newdelay = 2
        with self.assertLogs(self.logger, 'DEBUG'):
            delay = handler.block_provider('someone', 'just because', delay=newdelay)
        self.assertListEqual(cm.output, [
            'INFO:lazylibrarian.blockhandler:Blocking provider someone for 2 minutes because just because',
            'DEBUG:lazylibrarian.blockhandler:Provider Blocklist contains 1 entry'
        ])
        self.assertTrue(handler.is_blocked(name), 'Expected this to be blocked')
        self.assertEqual(delay, newdelay, 'Timeout value is unexpected')
        self.assertEqual(handler.number_blocked(), 1, 'Should be exactly one blocked')

        # Let the block be expired
        time.sleep(newdelay + 0.1)
        self.assertFalse(handler.is_blocked(name), 'Expected this to be blocked')
        self.assertEqual(handler.number_blocked(), 0, 'There should be no more blocks')

        self.set_loglevel(logging.DEBUG)
        # Add a 0-time block, which will be ignored
        delay = handler.block_provider('someone', 'just because', delay=0)
        self.assertEqual(delay, 0, 'Timeout value is unexpected')
        self.assertEqual(handler.number_blocked(), 0, 'Should not have added a block')

        # Add a block where the reason will be truncated to 80 chars
        _ = handler.block_provider('short', 'X' * 200)
        lines = handler.get_text_list_of_blocks().splitlines()
        self.assertListEqual(lines, [
            f"short blocked for 1 hours: {'X' * 80}",
        ])

    def test_number_blocked(self):
        handler = BlockHandler()
        self.assertEqual(handler.number_blocked(), 0, 'There should be no blocks by default')

    def test_clear_all(self):
        handler = BlockHandler()
        self.assertEqual(handler.number_blocked(), 0)
        handler.block_provider('test', 'no reason', )
        self.assertEqual(handler.number_blocked(), 1)
        handler.clear_all()
        self.assertEqual(handler.number_blocked(), 0)

    def test_check_day(self):
        """ Testing check_day requires simulating a day change """
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        handler = BlockHandler()
        handler.set_config(cfg, cfg.providers("NEWZNAB"), cfg.providers("TORZNAB"))
        change = handler.check_day(pretend_day='12345678')
        self.assertTrue(change, 'Expected day to change')

        # Make up some pretend API calls
        for provider in cfg.providers('NEWZNAB'):
            provider.set_int('APICOUNT', 10)
        for provider in cfg.providers('TORZNAB'):
            provider.set_int('APICOUNT', 100)
        handler.check_day(pretend_day='12345678')
        for provider in cfg.providers('NEWZNAB'):
            self.assertEqual(10, provider.get_int('APICOUNT'))
        for provider in cfg.providers('TORZNAB'):
            self.assertEqual(100, provider.get_int('APICOUNT'))

        # Change the day
        handler.check_day(pretend_day='12345700')
        for provider in cfg.providers('NEWZNAB'):
            self.assertEqual(0, provider.get_int('APICOUNT'))
        for provider in cfg.providers('TORZNAB'):
            self.assertEqual(0, provider.get_int('APICOUNT'))

    def test_get_text_list_of_blocks(self):
        handler = BlockHandler()
        txt = handler.get_text_list_of_blocks()
        self.assertEqual(txt, 'No blocked providers')

        handler.block_provider('first', 'abc', 100)
        handler.block_provider('second', 'hello', 10000)
        handler.block_provider('googleapi', 'xyz')
        txt = handler.get_text_list_of_blocks()
        lines = txt.splitlines()
        self.assertListEqual(lines, [
            'first blocked for 1 minutes, 40 seconds: abc',
            'second blocked for 2 hours, 46 minutes, 40 seconds: hello',
            'googleapi blocked for 1 hours: xyz',
        ])
