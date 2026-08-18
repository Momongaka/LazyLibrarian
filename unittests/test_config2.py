#  This file is part of Lazylibrarian.
#
# Purpose:
#   Testing the new config2 module

import logging
from collections import Counter
from typing import List, Dict

import mock

from lazylibrarian.config2 import LLConfigHandler, are_equivalent
from lazylibrarian.configdefs import BASE_DEFAULTS
from lazylibrarian.configdefs import get_default
from lazylibrarian.configenums import Access
from lazylibrarian.filesystem import DIRS, syspath, remove_file, path_isfile, safe_copy
from lazylibrarian.formatter import ImportPrefs
from unittests.unittesthelpers import LLTestCaseWithConfigandDIRS

# Ini files used for testing load/save functions.
# If these change, many test cases need to be updated. Run to find out which ones
SMALL_INI_FILE = './unittests/testdata/testconfig-defaults.ini'
ERROR_INI_FILE = './unittests/testdata/testconfig-errors.ini'


# noinspection PyBroadException
class Config2Test(LLTestCaseWithConfigandDIRS):

    def test_log_catching(self):
        """ Test that we can test for log events """
        logger = logging.getLogger('unittest')
        logger.setLevel(logging.INFO)
        # Test checking that a single message can be captured
        with self.assertLogs(logger, level='ERROR') as cm:
            logger.error('test error')
        self.assertListEqual(cm.output, ['ERROR:unittest:test error'], 'Did not log a message as expected')

        # Check more error levels, but with debug messages ignored
        with self.assertLogs(logger, level='DEBUG') as cm:
            logger.error('test error')
            logger.warning('test warn')
            logger.info('test info')
            logger.debug('test debug')
        self.assertListEqual(cm.output, [
            'ERROR:unittest:test error',
            'WARNING:unittest:test warn',
            'INFO:unittest:test info',
            'DEBUG:unittest:test debug'
        ], 'Expected an error, a warning, an info and a debug message')

        # Test capturing debug messages
        self.set_loglevel(logging.DEBUG)
        with self.assertLogs(logger, level='DEBUG') as cm:
            logger.info('test info')
            logger.debug('test debug')
        self.assertListEqual(cm.output, [
            'INFO:unittest:test info', 'DEBUG:unittest:test debug'
        ], 'Expected an info and a debug message')

        # Test capturing only INFO and above
        self.set_loglevel(logging.INFO)
        with self.assertLogs(logger, level='INFO') as cm:
            logger.info('test info')
            logger.debug('test debug')
        self.assertListEqual(cm.output, [
            'INFO:unittest:test info'
        ], 'Expected an info message')

    def test_compare_basic_configs(self):
        """ Test that we can compare basic configs and tell if they differ """
        cfg1 = LLConfigHandler()
        cfg2 = LLConfigHandler()

        with self.assertLogs(self.logger, level='INFO') as cm:
            self.set_basic_test_values(cfg1)
            self.set_basic_test_values(cfg2)
            self.assertTrue(are_equivalent(cfg1, cfg2))

            cfg1.set_int('a-new-int', 1)
            self.assertFalse(are_equivalent(cfg1, cfg2))
        self.assertListEqual(cm.output, [
            'WARNING:lazylibrarian.config2:Array lengths differ: 6 != 5',
            'WARNING:lazylibrarian.config2:Base configs differ'
        ])

        with self.assertLogs(self.logger, level='INFO') as cm:
            cfg2.set_int('a-new-int', 1)
            self.assertTrue(are_equivalent(cfg1, cfg2))

            cfg2.set_str('another-str', 'help')
            self.assertFalse(are_equivalent(cfg1, cfg2))
        self.assertListEqual(cm.output, [
            'WARNING:lazylibrarian.config2:Array lengths differ: 6 != 7',
            'WARNING:lazylibrarian.config2:Base configs differ'
        ])

    def do_access_compare(self, got: Dict[str, Counter], expected: Dict[str, Counter], exclude: List[Access],
                          error: str):
        """ Helper function, validates that two access lists are the same """
        if not exclude:
            self.assertEqual(len(got), len(expected))
        for key in got:
            for access in got[key]:
                if access not in exclude:
                    self.assertTrue(access in expected[key], f'Expected [{key}.{access}')
                    vgot = got[key][access]
                    vexp = expected[key][access]
                    self.assertEqual(vgot, vexp, f'[{key}.{access}]:{vgot}!={vexp}: {error}')

    def set_basic_test_values(self, cfg: LLConfigHandler):
        """ Helper function, sets some basic config values """
        with self.assertLogs(self.logger, level='INFO') as cm:
            cfg.set_str('somestr', 'abc')
            cfg.set_int('someint', 123)
            cfg.set_int('someint', 45)
            cfg.set_bool('abool', False)
            cfg.set_bool('boo', True)
            email = 'name@gmail.com'
            cfg.set_email('mail', email)

            cfg.set_email('mail2', 'name@gmailmissingcom')  # Format Error
        self.assertEqual(cm.output, [
            'ERROR:lazylibrarian.configtypes:Config[MAIL2]: format_error'
        ])

    def test_basic_types(self):
        """ Tests basic config types inside a ConfigHandler """
        cfg = LLConfigHandler()
        self.set_basic_test_values(cfg)

        with self.assertLogs(self.logger, level='INFO') as cm:
            self.assertEqual('abc', cfg.get_str('somestr'))
            self.assertEqual('abc', cfg['somestr'])
            self.assertEqual(45, cfg.get_int('someint'))
            self.assertEqual('45', cfg['someint'])
            self.assertEqual('name@gmail.com', cfg.get_email('mail'))
            self.assertFalse(cfg.get_bool('abool'))
            self.assertTrue(cfg.get_bool('boo'))
            self.assertEqual('1', cfg['boo'])
            self.assertEqual('', cfg.get_email('mail2'))  # Read Error
        self.assertListEqual(cm.output, [
            'ERROR:lazylibrarian.configtypes:Config[MAIL2]: read_error'
        ])

    def do_csv_ops(self, cfg: LLConfigHandler):
        with self.assertLogs(self.logger, level='INFO') as cm:
            cfg.set_csv('csv', 'allan,bob,fred')
            cfg.set_csv('csv2', '')
            cfg.set_csv('csv3', ',,test')  # Format error
            cfg.set_csv('csv4', '"fred" bob and alice,test')  # Format error
            cfg.set_csv('csv5', 'single')
        self.assertListEqual(cm.output, [
            'ERROR:lazylibrarian.configtypes:Config[CSV3]: format_error',
            'ERROR:lazylibrarian.configtypes:Config[CSV4]: format_error'
        ])
        ecs = cfg.get_error_counters()
        expectedecs = {
            'CSV3': Counter({Access.FORMAT_ERR: 1}),
            'CSV4': Counter({Access.FORMAT_ERR: 1}),
        }
        self.do_access_compare(ecs, expectedecs, [], 'Expected two format errors')

    def test_csv(self):
        """ Test ConfigCSV handling """
        cfg = LLConfigHandler()
        self.do_csv_ops(cfg)

        with self.assertLogs(self.logger, level='INFO') as cm:
            self.assertEqual('allan,bob,fred', cfg.get_csv('csv'))
            self.assertEqual('', cfg.get_csv('csv2'))
            self.assertEqual('single', cfg.get_csv('csv5'))
            self.assertEqual('', cfg.get_csv('csv3'))  # Read error
            self.assertEqual('', cfg.get_csv('csv4'))  # Read error
        self.assertListEqual(cm.output, [
            'ERROR:lazylibrarian.configtypes:Config[CSV3]: read_error',
            'ERROR:lazylibrarian.configtypes:Config[CSV4]: read_error'
        ])

        # Test CSV as list
        csv_list = cfg.get_list('csv')
        self.assertEqual(csv_list, ['allan', 'bob', 'fred'])

    def test_read_error_counters(self):
        """ Test that read error counters are correct in lots of cases """
        cfg = LLConfigHandler()

        # Try to access non-existing keys
        with self.assertLogs(self.logger, level='INFO') as cm:
            self.assertEqual('', cfg.get_str('does-not-exist'))
            self.assertEqual(0, cfg.get_int('does-not-exist'))
            self.assertEqual(False, cfg.get_bool('does-not-exist'))
            self.assertEqual('', cfg.get_csv('also-does-not'))
            self.assertEqual('', cfg['KeyDoesNotExist'])
        self.assertListEqual(cm.output, [
            'ERROR:lazylibrarian.configtypes:Config[DOES-NOT-EXIST]: read_error',
            'ERROR:lazylibrarian.configtypes:Config[DOES-NOT-EXIST]: read_error',
            'ERROR:lazylibrarian.configtypes:Config[DOES-NOT-EXIST]: read_error',
            'ERROR:lazylibrarian.configtypes:Config[ALSO-DOES-NOT]: read_error',
            'ERROR:lazylibrarian.configtypes:Config[KEYDOESNOTEXIST]: read_error'
        ])

        ecs = cfg.get_error_counters()
        expectedecs = {
            'KEYDOESNOTEXIST': Counter({Access.READ_ERR: 1}),
            'DOES-NOT-EXIST': Counter({Access.READ_ERR: 3}),
            'ALSO-DOES-NOT': Counter({Access.READ_ERR: 1})
        }
        self.do_access_compare(ecs, expectedecs, [], 'Errors  not as expected')

    def test_all_error_lists(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)

        allerrorlists = cfg.all_error_lists()
        for errorlist in allerrorlists:
            self.assertEqual(len(errorlist), 0, 'Expect all error lists to be empty')

        self.assertEqual(len(allerrorlists), 10, 'Expect there to be 1 base error list, plus one per array instance')

    def test_all_configs(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        allconfigs = cfg.all_configs()
        defaults = nondefaults = 0
        for name, item in allconfigs:
            if item.is_default():
                defaults += 1
            else:
                nondefaults += 1
        self.assertEqual(len(allconfigs), defaults + nondefaults, 'Inconsistent results from iterating in two ways')
        self.assertEqual(nondefaults, 48, 'Unexpected number of non-default entries in config file')

    def test_access_counters(self):
        """ Test that read/create counters work correctly when there are no errors """
        cfg = LLConfigHandler()

        self.do_csv_ops(cfg)
        self.set_basic_test_values(cfg)

        # Access some of these items
        self.assertEqual('abc', cfg['somestr'])
        for _ in range(3):
            self.assertEqual(45, cfg.get_int('someint'))
        self.assertEqual('name@gmail.com', cfg.get_email('mail'))
        self.assertTrue(cfg.get_bool('boo'))

        self.assertEqual('allan,bob,fred', cfg.get_csv('csv'))
        for _ in range(3):
            self.assertEqual('single', cfg.get_csv('csv5'))

        acs = cfg.get_all_accesses()
        expectedacs = {
            'CSV': Counter({Access.CREATE_OK: 1, Access.READ_OK: 1}),
            'CSV2': Counter({Access.CREATE_OK: 1}),
            'CSV5': Counter({Access.READ_OK: 3, Access.CREATE_OK: 1}),
            'SOMESTR': Counter({Access.CREATE_OK: 1, Access.READ_OK: 1}),
            'SOMEINT': Counter({Access.READ_OK: 3, Access.CREATE_OK: 1, Access.WRITE_OK: 1}),
            'ABOOL': Counter({Access.CREATE_OK: 1}),
            'BOO': Counter({Access.CREATE_OK: 1, Access.READ_OK: 1}),
            'MAIL': Counter({Access.CREATE_OK: 1, Access.READ_OK: 1})
        }
        self.do_access_compare(acs, expectedacs, [], 'Access patterns not as expected')
        expectedacs = {
            'CSV': Counter({Access.READ_OK: 1}),
            'CSV5': Counter({Access.READ_OK: 3}),
            'SOMESTR': Counter({Access.READ_OK: 1}),
            'SOMEINT': Counter({Access.READ_OK: 3, Access.WRITE_OK: 1}),
            'BOO': Counter({Access.READ_OK: 1}),
            'MAIL': Counter({Access.READ_OK: 1})
        }
        self.do_access_compare(acs, expectedacs, [Access.CREATE_OK], 'Comparing with excluded sections not working')

        errors = cfg.get_error_counters()
        expectederrors = {
            'CSV3': Counter({Access.FORMAT_ERR: 1}),
            'CSV4': Counter({Access.FORMAT_ERR: 1}),
            'MAIL2': Counter({Access.FORMAT_ERR: 1}),
        }
        self.assertDictEqual(errors, expectederrors, 'Errors were not as expected')

        cfg.clear_access_counters()
        acs = cfg.get_all_accesses()
        errors = cfg.get_error_counters()
        self.do_access_compare(acs, {}, [], 'Clearing all access patterns did not work')
        self.assertEqual(errors, {}, 'Clearing all access patterns did not clear errors')

    def test_LLdefaults(self):
        """ Test setting the default LL config """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)
        self.assertEqual(len(cfg.config), len(BASE_DEFAULTS),
                         'Maybe there is a duplicate entry in BASE_DEFAULTS')
        self.assertEqual(cfg.get_str('AUTH_TYPE'), 'BASIC')

    def test_schedule_list(self):
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)

        keynames = []
        schednames = []
        persistcount = 0
        canruncount = 0
        for name, scheduler in cfg.get_schedulers():
            keynames.append(name)
            schednames.append(scheduler.get_schedule_name())
            if scheduler.do_persist():
                persistcount += 1
            if cfg.scheduler_can_run(scheduler):
                canruncount += 1

        self.assertListEqual(keynames, [
            'TELEMETRY_INTERVAL', 'SEARCH_BOOKINTERVAL', 'SEARCH_MAGINTERVAL',  # Disabled by default
            'SCAN_INTERVAL', 'SEARCHRSS_INTERVAL', 'WISHLIST_INTERVAL',
            'SEARCH_COMICINTERVAL',  # Disabled by default
            'VERSIONCHECK_INTERVAL',
            'GOODREADS_INTERVAL', 'HARDCOVER_INTERVAL', # Disabled by default
            'CLEAN_CACHE_INTERVAL', 'BACKUP_INTERVAL', 'AUTHORUPDATE_INTERVAL', 'SERIESUPDATE_INTERVAL'])
        self.assertEqual(schednames,
                         ['telemetry_send', 'search_book', 'search_magazines', 'PostProcessor', 'search_rss_book',
                          'search_wishlist', 'search_comics', 'check_for_updates', 'sync_to_gr', 'hc_sync',
                          'clean_cache', 'backup', 'author_update', 'series_update'])
        self.assertEqual(persistcount, 10)
        self.assertEqual(canruncount, 7)

    def test_force_lower(self):
        """ Test various string configss that have force_lower and make sure they are. """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)

        for key, item in cfg.config.items():
            if item.get_force_lower():
                self.assertEqual(item.get_str(), str(item).lower(), f'force_lower has not worked for {key}')

        has_uppercase = cfg['API_KEY']
        self.assertNotEqual(has_uppercase, has_uppercase.lower())

    def test_configread_nodefs_defaultini(self):
        """ Test reading a near-default ini file, but without base definitions """
        self.set_loglevel(logging.INFO)
        with self.assertLogs(self.logger, level='INFO'):
            # Because no defaults are loaded, every item will case a warning
            cfg = LLConfigHandler(defaults=None, configfile=SMALL_INI_FILE)
        acs = cfg.get_all_accesses()
        self.do_access_compare(acs, {}, [], 'Loading ini without defaults should not load anything')

    def test_configread_defaultini(self):
        """ Test reading a near-default ini file, with all the base definitions loads """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=SMALL_INI_FILE)
        acs = cfg.get_all_accesses()  # We just want to know the right things were updated
        expectedacs = {
            'LOGGING.LOGLEVEL': Counter({Access.WRITE_OK: 1}),
            'LOGGING.LOGDIR': Counter({Access.WRITE_OK: 1}),
            'POSTPROCESS.AUDIOBOOK_DEST_FOLDER': Counter({Access.WRITE_OK: 1}),
            'GENERAL.NO_IPV6': Counter({Access.WRITE_OK: 1}),
            'GENERAL.IMP_PREFLANG': Counter({Access.WRITE_OK: 1}),
            'GENERAL.HTTP_EXT_TIMEOUT': Counter({Access.WRITE_OK: 1}),
            'GENERAL.EBOOK_DIR': Counter({Access.WRITE_OK: 1}),
            'GENERAL.AUDIO_DIR': Counter({Access.WRITE_OK: 1}),
            'GENERAL.ALTERNATE_DIR': Counter({Access.WRITE_OK: 1}),
            'GENERAL.TESTDATA_DIR': Counter({Access.WRITE_OK: 1}),
            'GENERAL.DOWNLOAD_DIR': Counter({Access.WRITE_OK: 1})
        }
        self.do_access_compare(acs, expectedacs, [Access.READ_OK],
                               'Loading ini file did not modify the expected values')

    def test_configread_nondefault(self):
        """ Test reading a more complex config.ini file """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        acs = cfg.get_all_accesses()
        expectedacs = {
            "LOGGING.LOGDIR": Counter({Access.WRITE_OK: 1}),
            "LOGGING.LOGLIMIT": Counter({Access.WRITE_OK: 1}),
            "LOGGING.LOGFILES": Counter({Access.WRITE_OK: 1}),
            "LOGGING.LOGSIZE": Counter({Access.WRITE_OK: 1}),
            "LOGGING.LOGLEVEL": Counter({Access.WRITE_OK: 1}),
            "MAGAZINES.MAG_TAB": Counter({Access.WRITE_OK: 1}),
            "COMICS.COMIC_TAB": Counter({Access.WRITE_OK: 1}),
            "GENERAL.AUDIO_TAB": Counter({Access.WRITE_OK: 1}),
            "API.API_ENABLED": Counter({Access.WRITE_OK: 1}),
            "API.API_KEY": Counter({Access.WRITE_OK: 1}),
            "CALIBRE.IMP_CALIBREDB": Counter({Access.WRITE_OK: 1}),
            "CALIBRE.CALIBRE_USE_SERVER": Counter({Access.WRITE_OK: 1}),
            "CALIBRE.CALIBRE_SERVER": Counter({Access.WRITE_OK: 1}),
            "GENERAL.IMP_NOSPLIT": Counter({Access.WRITE_OK: 1}),
            "GENERAL.IMP_PREFLANG": Counter({Access.WRITE_OK: 1}),
            "TELEMETRY.SERVER_ID": Counter({Access.WRITE_OK: 1}),
            "GENERAL.EBOOK_DIR": Counter({Access.WRITE_OK: 1}),
            "GENERAL.AUDIO_DIR": Counter({Access.WRITE_OK: 1}),
            "GENERAL.ALTERNATE_DIR": Counter({Access.WRITE_OK: 1}),
            "GENERAL.TESTDATA_DIR": Counter({Access.WRITE_OK: 1}),
            "GENERAL.DOWNLOAD_DIR": Counter({Access.WRITE_OK: 1}),
            "POSTPROCESS.AUDIOBOOK_DEST_FOLDER": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.DISPNAME": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.ENABLED": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.HOST": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.API": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.GENERALSEARCH": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.BOOKSEARCH": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.BOOKCAT": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.UPDATED": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.APILIMIT": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.RATELIMIT": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_0.DLTYPES": Counter({Access.WRITE_OK: 1}),
            "NEWZNAB_1.DISPNAME": Counter({Access.WRITE_OK: 1}),
            'NEWZNAB_1.HOST': Counter({Access.WRITE_OK: 1, Access.READ_OK: 1}),
            'APPRISE_0.DISPNAME': Counter({Access.WRITE_OK: 1}),
            'APPRISE_0.SNATCH': Counter({Access.WRITE_OK: 1}),
            'APPRISE_0.DOWNLOAD': Counter({Access.WRITE_OK: 1}),
            'APPRISE_0.URL': Counter({Access.WRITE_OK: 1, Access.READ_OK: 1}),
        }
        self.do_access_compare(acs, expectedacs, [Access.READ_OK],
                               'Loading complex ini file did not modify the expected values')

    def test_configread_witherrors(self):
        """ Test reading a config.ini file with errors we should be able to correct """
        self.set_loglevel(logging.DEBUG)
        with self.assertLogs(self.logger, level='DEBUG'):
            cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=ERROR_INI_FILE)
        # The ini file had array sections without _ in the name - check it's correct now
        for name in cfg.provider_names():
            array = cfg.get_array(name)
            for inx, arrayitem in enumerate(array):
                secstr = array.get_section_str(inx)
                self.assertTrue('_' in secstr, 'All array sections must have an underscore')
                for key, item in arrayitem.items():
                    self.assertEqual(secstr, item.section, f"The item {key} has a wrong section value")

    def test_configread_correcterrors(self):
        """ Read config file with errors and make sure they are gone on save/reload """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=ERROR_INI_FILE)

        testfile = DIRS.get_tmpfilename('test-fixed.ini')
        try:
            count = cfg.save_config_to_filename(testfile, False)  # Save only non-default values
            self.assertTrue(count > 20, 'Saving default config.ini has unexpected # of changes')
            cfgnew = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=testfile)
            self.assertTrue(are_equivalent(cfg, cfgnew),
                            f'Save error: {testfile} is not the same as original file!')
        finally:
            self.assertTrue(remove_file(testfile), 'Could not remove test-fixed.ini')

    def test_provider_iterator(self):
        """ Test the iterator function used to access providers """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        # Test reading items
        names = []
        for item in cfg.providers('NewzNab'):
            names.append(item['DISPNAME'])  # Access item as string directly
        self.assertEqual(names, ['NZBtester', 'AnotherTest', ''])

        # Test writing re-accessing data
        for index, item in enumerate(cfg.providers('rss')):
            item['HOST'] = f'TestHost-{index}'
            item.set_int('DLPRIORITY', index)
        for index, item in enumerate(cfg.providers('rss')):
            self.assertEqual(item['HOST'], f'TestHost-{index}')
            self.assertEqual(item['DLPRIORITY'], f'{index}')
            self.assertEqual(item.get_int('DLPRIORITY'), index)

        # Test accessing a provider array that doesn't exist
        cm = None
        try:
            with self.assertLogs(self.logger, level='ERROR') as cm:
                _ = cfg.providers('DoesNotExist')
            self.assertTrue(False, 'Should never get here')
        except Exception:
            if cm:
                self.assertListEqual(cm.output, [
                    'ERROR:lazylibrarian.configtypes:Config[DOESNOTEXIST]: read_error'
                ], 'message')

    def test_configread_nondefault_access(self):
        """ Test accessing a more complex config.ini file """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        cfg.clear_access_counters()

        with self.assertLogs(self.logger, level='ERROR'):  # There will be errors; catch them
            self.assertEqual(cfg['BaseInvalid'], '',
                             'Retrieving invalid base key does not work as expected')  # Read error

            self.assertEqual(cfg.get_array_entries('APPRISE'), 2, 'Expected two entries for APPRISE')
            self.assertEqual(cfg.get_array_entries('NEWZNAB'), 3, 'Expected two entries for NEWZNAB')
            self.assertEqual(cfg.get_array_entries('RSS'), 1, 'Expected one empty entry for RSS')
            self.assertEqual(cfg.get_array_entries('DOESNOTEXIST'), 0, 'Expected no entries')
            self.assertEqual(cfg.get_array_entries('AlsoFake'), 0, 'Expected no entries')

            newznab = cfg.get_array_dict('NEWZNAB', 0)
            self.assertIsNotNone(newznab, 'Expected to get a NEWZNAB object')
            if newznab:
                self.assertEqual(newznab['DISPNAME'], 'NZBtester', 'NEWZNAB.0.DISPNAME not loaded correctly')
                self.assertEqual(str(newznab['DISPNAME']), 'NZBtester', 'Default string return on array is not working')
                self.assertTrue(newznab.get_bool('ENABLED'), 'NEWZNAB.0.ENABLED not loaded correctly')
                self.assertEqual(newznab.get_int('APILIMIT'), 12345, 'NEWZNAB.0.APILIMIT not loaded correctly')
                self.assertEqual(newznab['InvalidKey'], '')  # Generate a read error
                self.assertEqual(newznab['InvalidKey'], '')  # Generate a read error
                self.assertEqual(newznab['InvalidKey_2'], '')  # Generate a read error

        summary = cfg.create_access_summary(saveto='')
        expected_summary = {
            'READ_OK': [('NEWZNAB_0.DISPNAME', 2), ('NEWZNAB_0.ENABLED', 1), ('NEWZNAB_0.APILIMIT', 1)],
            'WRITE_OK': [],
            'READ_ERR': [('BASEINVALID', 1), ('DOESNOTEXIST', 1), ('ALSOFAKE', 1), ('NEWZNAB_0.INVALIDKEY', 2),
                         ('NEWZNAB_0.INVALIDKEY_2', 1)],
            'WRITE_ERR': [],
            'CREATE_OK': [],
            'FORMAT_ERR': []
        }
        self.assertDictEqual(summary, expected_summary, 'Access Summary is not as expected')

    def test_save_config(self):
        """ Test saving config file """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=SMALL_INI_FILE)
        testfile = DIRS.get_tmpfilename('test-small.ini')
        try:
            count = cfg.save_config_to_filename(testfile, False)  # Save only non-default values
            self.assertEqual(count, 11, 'Saving default config.ini has unexpected # of changes')
            cfgnew = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=testfile)
            self.assertTrue(are_equivalent(cfg, cfgnew),
                            f'Save error: {testfile} is not the same as original file!')
        finally:
            self.assertTrue(remove_file(testfile), 'Could not remove test-small.ini')

        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        with self.assertLogs(self.logger, level='WARN'):
            count = cfg.save_config_to_filename('?*/\\invalid<>file', False)  # Save only non-default values
        self.assertEqual(count, -1, 'Should not be able to save to invalid file name')
        try:
            testfile = DIRS.get_tmpfilename('test-changed.ini')
            count = cfg.save_config_to_filename(testfile, False)  # Save only non-default values
            self.assertEqual(count, 39, 'Saving config.ini has unexpected # of non-default items')
            cfgnew = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=testfile)
            self.assertTrue(are_equivalent(cfg, cfgnew),
                            f'Save error: {testfile} is not the same as original file!')
        finally:
            self.assertTrue(remove_file(testfile), 'Could not remove test-changed.ini')

        try:
            testfile = DIRS.get_tmpfilename('test-all.ini')
            _ = cfg.save_config_to_filename(testfile, True)  # Save everything.
            cfgnew = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=testfile)
            self.assertTrue(are_equivalent(cfg, cfgnew),
                            f'Save error: {testfile} is not the same as original file!')
        finally:
            self.assertTrue(remove_file(testfile), 'Could not remove test-all.ini')

    def test_persistence_flag(self):
        """ Test whether the persist flag is obeyed when saving """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=SMALL_INI_FILE)
        initial = cfg['Unpersisted_test']
        cfg.set_int('Unpersisted_test', 17)
        testfile = DIRS.get_tmpfilename('test-small.ini')
        try:
            count = cfg.save_config_to_filename(testfile, False)  # Save only non-default values
            self.assertEqual(count, 11, 'Saving default config.ini has unexpected # of changes')
            cfgnew = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=testfile)
            with self.assertLogs(self.logger, level='WARN'):
                self.assertFalse(are_equivalent(cfg, cfgnew),
                                 f'Save error: {testfile} is identical to the original')
        finally:
            self.assertTrue(remove_file(testfile), 'Could not remove test-small.ini')

        self.assertEqual(cfgnew['Unpersisted_test'], initial, 'The unpersisted item was persisted!')

    def test_save_config_and_backup_old(self):
        """ Test saving config file while keeping the old one as a .bak file """
        self.set_loglevel(logging.INFO)
        test_file = DIRS.get_tmpfilename('test.ini')
        safe_copy(self.COMPLEX_INI_FILE, test_file)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=test_file)

        # delete potential backup file before starting
        backupfile = syspath(cfg.configfilename + '.bak')
        remove_file(backupfile)

        try:
            with self.assertLogs(self.logger, level='INFO') as cm:  # Expect only INFO messages
                count = cfg.save_config_and_backup_old(restart_jobs=False)
            self.assertEqual(len(cm), 2, 'Expected 2 INFO messages')
            self.assertEqual(count, 39, 'Saving config.ini has unexpected total # of items')
            self.assertTrue(path_isfile(backupfile), 'Backup file does not exist')
            acs = cfg.get_all_accesses()
            self.do_access_compare(acs, {}, [], 'Expect all accesses cleared after saving')

            cfgbak = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=backupfile)
            self.assertTrue(are_equivalent(cfg, cfgbak), '.bak file is not the same as original file!')

            # Verify that it works when .bak file exists as well:
            with self.assertLogs(self.logger, level='INFO') as cm:  # Expect only INFO messages
                count = cfg.save_config_and_backup_old(restart_jobs=False)
            self.assertEqual(len(cm), 2, 'Expected 2 INFO messages here')
            self.assertEqual(count, 39, 'Saving config.ini has unexpected total # of items')
            self.assertTrue(remove_file(backupfile), 'Could not delete backup file')
            acs = cfg.get_all_accesses()
            self.do_access_compare(acs, {}, [], 'Expect all accesses cleared after saving')
        finally:
            remove_file(test_file)
            remove_file(backupfile)

    @mock.patch('shutil.rmtree')
    @mock.patch('os.makedirs')
    @mock.patch('builtins.open')  # Need to be declared in reverse order below:
    def test_post_save_actions(self, mock_open, mock_makedirs, mock_rmtree):
        """ Test that the things done after saving and backing up are done correctly """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)

        # The only test is to make sure the mako cache is clearer
        cfg.config['HTTP_LOOK'].set_str('a_special_ui')  # Force the mako cache to get cleared
        cfg.post_save_actions(clear_counters=True, restart_jobs=False)
        self.do_access_compare(cfg.get_all_accesses(), {}, [], 'Expected all accesses cleared after saving')

        mako_dir = DIRS.get_mako_cachedir()
        mock_rmtree.assert_called_with(mako_dir)
        mock_makedirs.assert_called_with(mako_dir)
        self.assertEqual(cfg.config['HTTP_LOOK'].get_str(), 'a_special_ui', 'HTTP_LOOK did not change')

        # TODO: Add tests for schedulers and database changes

    def test_post_load_fixup(self):
        """ Verify that the post_load_fixup routine does the right thing """
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)

        # Set some values that trigger warnings/fixes
        import os
        for fname in ['EBOOK_DEST_FILE', 'MAG_DEST_FILE', 'AUDIOBOOK_DEST_FILE', 'AUDIOBOOK_SINGLE_FILE']:
            value = cfg.config[fname].get_str() + os.sep  # These will be removed in post
            cfg.config[fname].set_str(value)
        cfg.config['HTTP_LOOK'].set_str('default')

        # Set some values that cause changes
        cfg.config['EBOOK_TAB'].set_bool(True)
        cfg.config['AUDIO_TAB'].set_bool(False)
        cfg.config['MAG_TAB'].set_bool(True)
        cfg.config['COMIC_TAB'].set_bool(True)
        cfg.config['HOMEPAGE'].set_str('AudioBooks')
        cfg.config['SSL_CERTS'].set_str('dir-doesnot-exist')

        with self.assertLogs(self.logger, level='WARN') as cm:
            warnings = cfg.post_load_fixup()
        # Do not test for specific messages as they depend on the OS
        self.assertEqual(len(cm.output), 6, 'Unexpected # of log messages')
        self.assertEqual(cfg.config['LOGDIR'].get_str()[-4:], 'Logs', 'LOGDIR not set')
        self.assertEqual(str(cfg.config['AUDIOBOOK_DEST_FOLDER']), str(cfg.config['EBOOK_DEST_FOLDER']))
        self.assertEqual(str(cfg.config['HTTP_LOOK']), 'bookstrap')

        self.assertTrue(cfg.get_bool('EBOOK_TAB'))
        self.assertFalse(cfg.get_bool('AUDIO_TAB'))
        self.assertTrue(cfg.get_bool('MAG_TAB'))
        self.assertTrue(cfg.config['COMIC_TAB'].get_bool())

        self.assertEqual(str(cfg.config['HOMEPAGE']), '', 'HOMEPAGE cannot be audio if that is disabled')
        self.assertEqual(warnings, 6, 'Unexpected # of warnings from fixup')

        # Second run with different inputs
        for fname in ['EBOOK_DEST_FILE', 'MAG_DEST_FILE', 'AUDIOBOOK_DEST_FILE', 'AUDIOBOOK_SINGLE_FILE']:
            cfg.config[fname].reset_to_default()
        cfg.config['HOMEPAGE'].set_str('eBooks')
        warnings = cfg.post_load_fixup()
        self.assertEqual(str(cfg.config['HOMEPAGE']), 'eBooks', 'Should not have changed HOMEPAGE')
        self.assertEqual(warnings, 0, 'Expected no warnings here')

    def test_array_entry_usage(self):
        """ Verify that array entries can be added to and deleted """
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        # with self.assertLogs(self.logger, level='WARN'):

        array = cfg.get_array('NOPEDOESNOTEXIST')
        self.assertIsNone(array, 'Non-existent array type must not be found')

        self.assertEqual(cfg.get_array_entries('APPRISE'), 2, 'This test assumes 2 APPRISE entries')
        array = cfg.get_array('APPRISE')
        self.assertIsNotNone(array, 'APPRISE array must exist')
        if array:
            self.assertTrue(array.is_in_use(0), 'This test assumes there is an Apprise[0] entry in use')
            self.assertFalse(array.is_in_use(1), 'This test assumes there is an empty Apprise[1] entry')
            self.assertFalse(array.is_in_use(10), 'Too high an index should be False')
            # A user is removing the URL from the first APPRISE entry, making it invalid
            array[0].set_str('URL', '')
            self.assertFalse(array.is_in_use(0), 'An empty URL should mean this item is not in use!')

            # A user adds a URL to the formerly empty item, making it valid
            array[1]['URL'] = 'http://testing'
            self.assertTrue(array.is_in_use(1), 'The entry should now be in use as the URL is not empty')

            # We now save, clean up empty items and rename them
            array.cleanup_for_save()

        # Re-get the array and make sure it's valid
        self.assertEqual(cfg.get_array_entries('APPRISE'), 1, 'There should be just one entry left')
        array = cfg.get_array('APPRISE')
        if array:
            self.assertTrue(array.is_in_use(0), 'The renumbering did not work correctly')
            array.ensure_empty_end_item()
            self.assertEqual(cfg.get_array_entries('APPRISE'), 2, 'We should now have two entries')
            self.assertFalse(array.is_in_use(1), 'The last entry must be empty at this stage')

    def test_case_tolerance(self):
        """ Make sure the config object is as Case TOLErant as possible """
        self.set_loglevel(logging.INFO)
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)

        with self.assertLogs(self.logger, level='INFO') as cm:
            # Access an entry that doesn't exist
            test1: str = cfg['HELLO']  # Doesn't exist: Return empty string
            test2: str = cfg['hello']  # Doesn't exist: Return empty string
            test3: str = cfg.get_str('hello')  # Doesn't exist: Return empty string
            testi1: int = cfg.get_int('hello')  # Doesn't exist: Return 0
            self.assertEqual(test1, '', '1: Expected non-existent entry HELLO to be empty')
            self.assertEqual(test2, '', '2: Expected non-existent entry hello to be empty')
            self.assertEqual(test3, '', '3: Expected non-existent entry hello to be empty')
            self.assertEqual(testi1, 0, '4: Expected non-existent entry hello to be 0')

            # Access an entry in various cases
            test1: str = cfg['BOOKSTRAP_THEME']  # Defaults to 'slate'
            test2: str = cfg['bookstrap_theme']  # Should be the same
            test3 = cfg.get_str('bookstrap_THEME')  # should be the same
            self.assertEqual(test1, get_default('BOOKSTRAP_THEME'), 'Expected to get default value')
            self.assertEqual(test1, test2, 'Expected key lookup to be case insensitive')
            self.assertEqual(test1, test3, 'Different ways of getting same key should be the same')
            self.assertIsNone(get_default('UNVALID-KEY'), 'Expected None from invalid key')

            # Arrays
            array = cfg.get_array('NEWZNAB')
            self.assertTrue(array, 'Need to get array for testing')
            if array:
                count = cfg.get_array_entries('newznab')
                self.assertEqual(len(array), count, 'Expect array lengths to be the same')
                items = array[0]  # This is a ConfigDict
                test1: str = items['DISPNAME']
                test2: str = items['dispname']
                test3: str = cfg.get_array_str('newznab', 0, 'dispname')
                self.assertEqual(test1, 'NZBtester', 'Did not read as expected from ini file')
                self.assertEqual(test1, test2, 'Expected key lookup to be case insensitive')
                self.assertEqual(test1, test3, 'Different ways of getting same key should be the same')

        self.assertListEqual(cm.output, [
            'ERROR:lazylibrarian.configtypes:Config[HELLO]: read_error',
            'ERROR:lazylibrarian.configtypes:Config[HELLO]: read_error',
            'ERROR:lazylibrarian.configtypes:Config[HELLO]: read_error',
            'ERROR:lazylibrarian.configtypes:Config[HELLO]: read_error'
        ], 'Unexpected log messages when testing tolerance')

    def test_use_rss(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)
        self.assertFalse(cfg.use_rss())

    def test_use_wishlist(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)
        self.assertFalse(cfg.use_wishlist())

    def test_use_irc(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)
        self.assertFalse(cfg.use_irc())

    def test_use_nzb(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)
        self.assertFalse(cfg.use_nzb())

    def test_use_tor(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)
        self.assertFalse(cfg.use_tor())

    def test_use_direct(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)
        self.assertFalse(cfg.use_direct())

    def test_use_any(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        self.assertTrue(cfg.use_any(), 'There should be some providers in use')

    def test_count_all_providers(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        count = cfg.total_active_providers()
        self.assertEqual(count, 2, 'Expected 2 active providers from ini file')

    def test_add_access_errors_to_log(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)
        # Make sure there are no errors from load
        allerrorlists = cfg.all_error_lists()
        for errorlist in allerrorlists:
            self.assertEqual(len(errorlist), 0, 'Expect all error lists to be empty')
        # Create some errors
        _ = cfg.get_bool('HOMEPAGE')  # Read error (type)
        cfg.set_bool('HOMEPAGE', True)  # Write error (type)
        cfg.set_int('HOMEPAGE', 2)  # Write error (type)
        _ = cfg.get_str('NotAValidKey')  # Read error (key)
        rss = cfg.get_array_dict('RSS', 0)
        rss.set_int('HOST', 2)  # Write error (type)
        _ = rss.get_str('NotAValidRSSKey')  # Read error (key)
        with self.assertLogs(self.logger, level=logging.ERROR) as cm:
            cfg.add_access_errors_to_log()
        # The order is always type, then key errors
        self.assertListEqual(cm.output, [
            'ERROR:lazylibrarian.config2:Config READ_ERR: GENERAL.HOMEPAGE, 1 times',
            'ERROR:lazylibrarian.config2:Config WRITE_ERR: GENERAL.HOMEPAGE, 2 times',
            'ERROR:lazylibrarian.config2:Config WRITE_ERR: RSS_0.HOST, 1 times',
            'ERROR:lazylibrarian.config2:Config READ_ERR: NOTAVALIDKEY, 1 times',
            'ERROR:lazylibrarian.config2:Config READ_ERR: RSS_0.NOTAVALIDRSSKEY, 1 times'
        ])

    def test_update_providers_from_ui(self):
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=self.COMPLEX_INI_FILE)

        rss0 = cfg.get_array_dict('rss', 0)
        self.assertIsNotNone(rss0, 'Config initialization error, expect an RSS entry')
        # Validate values before assigning
        self.assertEqual(rss0['dispname'], '')
        self.assertEqual(rss0.get_bool('enabled'), False)
        self.assertEqual(rss0.get_int('DLPriority'), 0)
        changes = {'rss_0_dispname': 'test', 'rss_0_enabled': '1', 'rss_0_dlpriority': 1}
        cfg.update_providers_from_ui(changes)
        # Validate that values were assigned
        self.assertEqual(rss0['dispname'], 'test')
        self.assertEqual(rss0.get_bool('enabled'), True)
        self.assertEqual(rss0.get_int('DLPriority'), 1)

    def test_onchange(self):
        """ Test the onchange mechanism that calls a method when a config changes """
        cfg = LLConfigHandler(defaults=BASE_DEFAULTS)
        self.assertEqual(ImportPrefs.LANG_LIST, ['en', 'eng', 'en-US', 'en-GB', 'English'])

        lang1 = cfg.get_str('IMP_PREFLANG')  # This item has an onchange method
        cfg.load_configfile(self.COMPLEX_INI_FILE)  # This changes the IMP_PREFLANG value
        lang2 = cfg.get_str('IMP_PREFLANG')
        self.assertNotEqual(lang1, lang2)
        self.assertEqual(ImportPrefs.LANG_LIST, ['de', 'fr', 'en'])

        ci = cfg.get_item('IMP_PREFLANG')
        ci.set_str('')
        self.assertEqual(ImportPrefs.LANG_LIST, [])
