#  This file is part of Lazylibrarian.
#
# Purpose:
#   Hold helper functions only needed for testing

import logging
import os
import sys
import unittest
from collections import Counter
from shutil import rmtree
from typing import List

import lazylibrarian
from lazylibrarian.dbupgrade import upgrade_needed, db_upgrade
from lazylibrarian.configenums import Access
from lazylibrarian.filesystem import DIRS, path_isdir
from lazylibrarian.startup import StartupLazyLibrarian
from lazylibrarian.config2 import LLConfigHandler, CONFIG  # One day, won't need this any more
from lazylibrarian.configdefs import BASE_DEFAULTS
from lazylibrarian.logconfig import LOGCONFIG


# noinspection PyBroadException
class LLTestCase(unittest.TestCase):
    COMPLEX_INI_FILE = './unittests/testdata/testconfig-complex.ini'

    def setUp(self):
        super().setUp()
        self.logger = logging.getLogger('')
        self.logger.setLevel(logging.INFO)

    def set_loglevel(self, level):
        """ Set the root log level per request; the calling test function depends on it to test log messages """
        self.logger.setLevel(level)

    @classmethod
    def clearGlobals(cls):
        # Clear configuration variables to have a clean slate for any further test runs
        lazylibrarian.DAEMON = False
        lazylibrarian.SIGNAL = None
        lazylibrarian.SYS_ENCODING = ''
        lazylibrarian.LOGINUSER = None
        lazylibrarian.COMMIT_LIST = None
        lazylibrarian.STOPTHREADS = False

    @classmethod
    def disableHTTPSWarnings(cls):
        import urllib3
        urllib3.disable_warnings()

    @classmethod
    def removeDirectory(cls, cachedir: str, reason: str):
        if len(cachedir):
            logging.getLogger(None).debug(reason)
            try:
                rmtree(cachedir)
            except Exception:
                pass

    @classmethod
    def prepareTestDB(cls):
        curr_ver = upgrade_needed()
        if curr_ver:
            db_upgrade(curr_ver)

    @classmethod
    def removetestDB(cls):
        # Delete the database that was created for unit testing
        if len(DIRS.get_dbfile()):
            logging.getLogger(None).debug("Deleting unit test database")
            try:
                os.remove(DIRS.get_dbfile())
                os.remove(DIRS.get_dbfile() + "-shm")
                os.remove(DIRS.get_dbfile() + "-wal")
            except Exception:
                pass

    def assertEndsWith(self, teststr, end):
        self.assertEqual(teststr[-len(end):], end)

    def single_access_compare(self, got: Counter, expected: Counter, exclude: List[Access], error: str = ''):
        """ Helper function, validates that two access counters are the same """
        for access in got:
            if access not in exclude:
                self.assertTrue(access in expected, f'Excected {access}')
                vgot = got[access]
                vexp = expected[access]
                self.assertEqual(vgot, vexp, f'{access}:{vgot}!={vexp}: {error}')

    @classmethod
    def cfg(cls) -> LLConfigHandler:
        pass


class LLTestCaseWithConfigandDIRS(LLTestCase):
    """ Test case that needs a config and DIRS object to function """
    from lazylibrarian.config2 import LLConfigHandler

    CONFIGFILE = './unittests/testdata/testconfig-defaults.ini'
    config: LLConfigHandler

    @classmethod
    def setUpClass(cls) -> None:
        lazylibrarian.SYS_ENCODING = 'UTF-8'
        DIRS.set_fullpath_args(os.path.abspath(__file__), sys.argv[1:])
        LOGCONFIG.initialize_console_only_log(redact=False)
        cls.config = LLConfigHandler(defaults=BASE_DEFAULTS, configfile=cls.CONFIGFILE)
        DIRS.set_config(cls.config)
        DIRS.initialize_logger()
        DIRS.set_datadir(DIRS.PROG_DIR)
        DIRS.ensure_log_dir()

    @classmethod
    def cfg(cls) -> LLConfigHandler:
        return cls.config

    @classmethod
    def removetestCache(cls):
        super().removeDirectory(DIRS.CACHEDIR, "Deleting unit test cache directory")

    @classmethod
    def delete_test_logs(cls):
        if path_isdir(cls.config['LOGDIR']) and len(cls.config['LOGDIR']) > 3:
            try:  # Do not delete if there is a risk that it's the root of somewhere important
                rmtree(cls.config['LOGDIR'], ignore_errors=False)
            except Exception as e:
                print(str(e))


class LLTestCaseWithStartup(LLTestCase):
    """ LL test case that uses global CONFIG as well as some of the standard LL startup sequence.
    These should get increasingly rare as the code gets parameterised. """
    CONFIGFILE = './unittests/testdata/testconfig-defaults.ini'
    starter: StartupLazyLibrarian

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.starter = StartupLazyLibrarian()
        cls.starter.init_loggers(console_only=True)
        options, configfile = cls.starter.startup_parsecommandline(__file__, args=[''], testing=True)
        cls.starter.load_config(cls.CONFIGFILE)
        # Only log errors during the rest of startup
        cls.starter.init_loggers(console_only=False)
        logging.getLogger('root').setLevel(logging.ERROR)
        cls.starter.init_misc(CONFIG)
        LLTestCase.disableHTTPSWarnings()
        cls.starter.init_caches(CONFIG)
        cls.starter.init_database()
        cls.prepareTestDB()
        cls.starter.init_build_lists(CONFIG)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.starter.shutdown(restart=False, update=False, doquit=False, testing=True)
        cls.removetestDB()
        cls.removetestCache()
        cls.delete_test_logs()
        cls.clearGlobals()
        cls.CONFIGFILE = './unittests/testdata/testconfig-defaults.ini'
        return super().tearDownClass()

    @classmethod
    def cfg(cls) -> LLConfigHandler:
        return CONFIG

    @classmethod
    def removetestCache(cls):
        super().removeDirectory(DIRS.CACHEDIR, "Deleting unit test cache directory")

    @classmethod
    def delete_test_logs(cls):
        if path_isdir(CONFIG['LOGDIR']) and len(CONFIG['LOGDIR']) > 3:
            try:  # Do not delete if there is a risk that it's the root of somewhere important
                rmtree(CONFIG['LOGDIR'], ignore_errors=False)
            except Exception as e:
                print(str(e))


def false_method() -> bool:
    """ A method that returns False. used for testing. """
    return False