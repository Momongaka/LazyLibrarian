#  This file is part of Lazylibrarian.
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

# Purpose:
#   Contains global startup and initialization code for LL

import contextlib
import json
import locale
import logging
import os
import sqlite3
import subprocess
import sys
import tarfile
import time
import traceback
from shutil import move, rmtree
from typing import Any

import cherrypy
import requests
import urllib3

import lazylibrarian
from lazylibrarian import au, database, dnb, gb, gr, hc, ol, ran, versioncheck
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.cache import fetch_url, init_hex_caches
from lazylibrarian.cleanup import UNBUNDLER
from lazylibrarian.common import docker, log_header, validate_monthtable
from lazylibrarian.config2 import CONFIG, LLConfigHandler
from lazylibrarian.configtypes import ConfigDict
from lazylibrarian.dbupgrade import check_db, db_current_version, db_upgrade, upgrade_needed
from lazylibrarian.filesystem import DIRS, path_isdir, path_isfile, remove_file, syspath
from lazylibrarian.formatter import check_int, make_unicode, unaccented
from lazylibrarian.logconfig import LOGCONFIG
from lazylibrarian.notifiers import APPRISE_VER
from lazylibrarian.providers import get_capabilities
from lazylibrarian.scheduling import (
    SchedulerCommand,
    initscheduler,
    restart_jobs,
    shutdownscheduler,
    startscheduler,
)


class StartupLazyLibrarian:
    logger: logging.Logger

    def startup_parsecommandline(self, mainfile, args, testing=False) -> (Any, str):
        """ Parse command line, return options and configfile to use """
        # All initializartion that needs to happen before logging starts
        self.logger.debug('Parsing command line')
        if hasattr(sys, 'frozen'):
            DIRS.set_fullpath_args(os.path.abspath(sys.executable), sys.argv[1:])
        else:
            DIRS.set_fullpath_args(os.path.abspath(mainfile), sys.argv[1:])

        lazylibrarian.SYS_ENCODING = None

        try:
            locale.setlocale(locale.LC_ALL, "")
            lazylibrarian.SYS_ENCODING = locale.getpreferredencoding()
        except (OSError, locale.Error):
            pass

        # for OSes that are poorly configured I'll just force UTF-8
        # windows cp1252 can't handle some accented author names,
        # eg "Marie Kondō" U+014D: LATIN SMALL LETTER O WITH MACRON, but utf-8 does
        if not lazylibrarian.SYS_ENCODING or lazylibrarian.SYS_ENCODING in (
                'ANSI_X3.4-1968', 'US-ASCII', 'ASCII') or '1252' in lazylibrarian.SYS_ENCODING:
            lazylibrarian.SYS_ENCODING = 'UTF-8'

        # Set arguments
        from optparse import OptionParser

        p = OptionParser()
        p.add_option('-d', '--daemon', action="store_true",
                     dest='daemon', help="Run the server as a daemon")
        p.add_option('-q', '--quiet', action="store_true",
                     dest='quiet', help="Don't log to console")
        p.add_option('-j', '--nojobs', action="store_true",
                     dest='nojobs', help="Don't start background tasks")
        p.add_option('--nolaunch', action="store_true",
                     dest='nolaunch', help="Don't start browser")
        p.add_option('--update', action="store_true",
                     dest='update', help="Update to latest version (only git or source installs)")
        p.add_option('--upgrade', action="store_true",
                     dest='update', help="Update to latest version (only git or source installs)")
        p.add_option('--port',
                     dest='port', default=None,
                     help="Force webinterface to listen on this port")
        p.add_option('--noipv6',
                     dest='noipv6', default=None, action="store_true",
                     help="Do not attempt to use IPv6")
        p.add_option('--datadir',
                     dest='datadir', default=None,
                     help="Path to the data directory")
        p.add_option('--debug',
                     dest='debug', default=None, action="store_true",
                     help="Set DEBUG level logging")
        p.add_option('--config',
                     dest='config', default=None,
                     help="Path to config.ini file")
        p.add_option('-p', '--pidfile',
                     dest='pidfile', default=None,
                     help="Store the process id in the given file")
        p.add_option('-u', '--userid',
                     dest='userid', default=None,
                     help="Login as this userid")
        p.add_option('--loglevel',
                     dest='loglevel', default=None,
                     help="Set loglevel to either DEBUG, INFO or ERROR, or 10, 20 or 40.")
        options, _ = p.parse_args(args)

        if options.quiet:
            # Don't output anything at all to the console
            LOGCONFIG.remove_console_handlers()

        if options.loglevel:
            try:
                LOGCONFIG.change_root_loglevel(options.loglevel)
                self.logger.info(f"Set loglevel to {LOGCONFIG.get_loglevel_name('root')}")
            except ValueError as e:
                self.logger.warning(f'loglevel parameter must be a valid log level, error {str(e)}')

        elif options.debug:
            LOGCONFIG.change_root_loglevel('DEBUG')
            self.logger.info('Enabled option DEBUG level logging.')

        else:
            loglevel = CONFIG['LOGLEVEL']
            LOGCONFIG.change_root_loglevel(loglevel)
            self.logger.info(f"Enabled configured {LOGCONFIG.get_loglevel_name('root')} level logging.")

        if options.noipv6:
            # A hack, found here: https://stackoverflow.com/questions/33046733/force-requests-to-use-ipv4-ipv6
            urllib3.util.connection.HAS_IPV6 = False  # type: ignore

        if options.daemon:
            if os.name != 'nt':
                lazylibrarian.DAEMON = True
            # lazylibrarian.daemonize()
            else:
                print("Daemonize not supported under Windows, starting normally")

        if options.port:
            options.port = check_int(options.port, 0)

        if options.nojobs:
            lazylibrarian.STOPTHREADS = True
        else:
            lazylibrarian.STOPTHREADS = False

        if options.datadir:
            DIRS.set_datadir(str(options.datadir))
        else:
            DIRS.set_datadir(DIRS.PROG_DIR)

        if options.config:
            configfile = str(options.config)
        else:
            configfile = os.path.join(DIRS.DATADIR, "config.ini")

        if options.pidfile and lazylibrarian.DAEMON:
            lazylibrarian.PIDFILE = str(options.pidfile)

        if options.update:
            lazylibrarian.SIGNAL = 'update'
            # This is the "emergency recovery" update in case lazylibrarian won't start.
            # Set up some dummy values for the update as we have not read the config file yet
            CONFIG.reset_to_default([
                'GIT_PROGRAM', 'GIT_USER', 'GIT_REPO', 'USER_AGENT', 'HTTP_TIMEOUT', 'PROXY_HOST',
                'SSL_CERTS', 'SSL_VERIFY', 'LOGLIMIT', 'BACKUP_DB', 'GIT_PROJECT',
            ])
            DIRS.ensure_cache_dir()
            CONFIG['LOGDIR'] = DIRS.ensure_data_subdir('Logs')

            versioncheck.get_install_type()
            if CONFIG['INSTALL_TYPE'] not in ['git', 'source']:
                lazylibrarian.SIGNAL = None
                print('Cannot update, not a git or source installation')
            else:
                self.shutdown(update=True, doquit=True, testing=False)

        if not testing:
            self.logger.info(f"Lazylibrarian (pid {os.getpid()}) is starting up...")
            # allow a bit of time for old task to exit if restarting. Needs to free logfile and server port.
            time.sleep(2)
        return options, configfile

    @staticmethod
    def load_config(configfile: str):
        """ Load the config file, perform post-load fixups to ensure consistent states """
        config = lazylibrarian.config2.CONFIG  # Don't create a new instance
        config.pre_load_fixup(configfile=configfile)
        config.load_configfile(configfile=configfile)
        config.post_load_fixup()
        DIRS.ensure_log_dir()

    def init_loggers(self, console_only: bool):
        """ Initialize log files. Until this is done, do not use the logger """
        if console_only:
            LOGCONFIG.initialize_console_only_log(redact=False)
        else:
            LOGCONFIG.initialize_log_config(
                max_size=CONFIG.get_int('LOGSIZE'),
                max_number=CONFIG.get_int('LOGFILES'),
                redactui=CONFIG.get_bool('LOGREDACT'),
                redactfiles=CONFIG.get_bool('LOGFILEREDACT'))
            LOGCONFIG.enable_only_these_special_debuglogs(CONFIG['LOGSPECIALDEBUG'])
        self.logger = logging.getLogger(__name__)

    def init_misc(self, config: ConfigDict):
        """ Other initialization."""
        BLOCKHANDLER.set_config(CONFIG, CONFIG.providers("NEWZNAB"), CONFIG.providers("TORZNAB"))
        initscheduler()
        lazylibrarian.UNRARLIB, lazylibrarian.RARFILE = self.get_unrarlib(config)

        if config.get_bool('NO_IPV6'):
            # A hack, found here: https://stackoverflow.com/questions/33046733/force-requests-to-use-ipv4-ipv6
            urllib3.util.connection.HAS_IPV6 = False  # type: ignore

        logger = logging.getLogger(__name__)
        if APPRISE_VER:  # If APPRISE can't be found, show old notifiers
            logger.info(f"Apprise library ({APPRISE_VER}) installed")
        else:
            logger.warning("Did not find Apprise notifications library")
            CONFIG.set_bool('HIDE_OLD_NOTIFIERS', False)

    def init_caches(self, config: LLConfigHandler):
        # override detected encoding if required
        if config['SYS_ENCODING']:
            lazylibrarian.SYS_ENCODING = config['SYS_ENCODING']

        for item in ['book', 'author', 'SeriesCache', 'magazine', 'comic', 'IRCCache', 'icrawler', 'mako']:
            cachelocation = DIRS.get_cachedir(item)
            ok, msg = DIRS.ensure_dir_is_writeable(cachelocation)
            if not ok:
                self.logger.error(msg)

        _ = init_hex_caches()
        makocache = DIRS.get_mako_cachedir()
        self.logger.debug("Clearing mako cache")
        with contextlib.suppress(FileNotFoundError):
            rmtree(makocache)
        os.makedirs(makocache)
        remove_file(os.path.join(DIRS.CACHEDIR, 'alive.png'))
        # keep track of last api calls so we don't call more than once per second
        # to respect api terms, but don't wait un-necessarily either
        # keep track of how long we slept
        time_now = int(time.time())
        lazylibrarian.TIMERS['LAST_LT'] = time_now
        lazylibrarian.TIMERS['LAST_GR'] = time_now
        lazylibrarian.TIMERS['LAST_CV'] = time_now
        lazylibrarian.TIMERS['LAST_BOK'] = time_now
        lazylibrarian.TIMERS['SLEEP_GR'] = 0.0
        lazylibrarian.TIMERS['SLEEP_LT'] = 0.0
        lazylibrarian.TIMERS['SLEEP_CV'] = 0.0
        lazylibrarian.TIMERS['SLEEP_BOK'] = 0.0
        lazylibrarian.TIMERS['LAST_HC'] = time_now
        lazylibrarian.TIMERS['SLEEP_HC'] = 0.0

    def init_database(self):
        # Initialize the database
        db = database.DBConnection()
        try:
            result = db.match('PRAGMA user_version')
            check = db.match('PRAGMA integrity_check')
            dbfile = db.match('PRAGMA database_list')
            if result:
                version = result[0]
            else:
                version = 0
            self.logger.info(f"Database is v{version}, integrity check: {check[0]}")
            self.logger.info(f"Database {dict(dbfile)}")
        except Exception as e:
            self.logger.error(f"Can't connect to the database: {type(e).__name__} {str(e)}")
            sys.exit(0)

        curr_ver = upgrade_needed()
        if curr_ver:
            lazylibrarian.UPDATE_MSG = f'Updating database to version {curr_ver}'
            db_upgrade(curr_ver)

        if version:
            db_changes = check_db()
            if db_changes:
                db.action(f'PRAGMA user_version={db_current_version}')
                db.action('vacuum')
                self.logger.debug(f"Upgraded database schema to v{db_current_version} with {db_changes} changes")

        db.close()
        # group_concat needs sqlite3 >= 3.5.4
        # foreign_key needs sqlite3 >= 3.6.19 (Oct 2009)
        try:
            sqlv = getattr(sqlite3, 'sqlite_version', None)
            parts = sqlv.split('.')
            if int(parts[0]) == 3 and int(parts[1]) < 6 or int(parts[1]) == 6 and int(parts[2]) < 19:
                self.logger.error("Your version of sqlite3 is too old, please upgrade to at least v3.6.19")
                sys.exit(0)
        except Exception as e:
            self.logger.warning(f"Unable to parse sqlite3 version: {type(e).__name__} {str(e)}")

    def init_build_debug_header(self, online):
        debuginfo = log_header(online)
        for item in debuginfo.splitlines():
            if 'missing' in item:
                self.logger.warning(item)

    def init_build_lists(self, config: ConfigDict):
        lazylibrarian.GRGENRES = self.build_genres()
        lazylibrarian.DICTS = self.build_dicts()
        lazylibrarian.MONTHNAMES, lazylibrarian.SEASONS = self.build_monthtable(config)
        lazylibrarian.NEWUSER_MSG = self.build_logintemplate()
        lazylibrarian.NEWFILE_MSG = self.build_filetemplate()
        lazylibrarian.BOOKSTRAP_THEMELIST = self.build_bookstrap_themes(DIRS.PROG_DIR)
        lazylibrarian.INFOSOURCES = self.build_sources()

    @staticmethod
    def build_sources():
        info_sources = {
                'OpenLibrary': {'src': 'OL', 'author_key': 'ol_id', 'book_key': 'ol_id', 'enabled': 'OL_API',
                                'api': ol.OpenLibrary, 'has_subs': 0},
                'GoodReads': {'src': 'GR', 'author_key': 'gr_id', 'book_key': 'gr_id', 'enabled': 'GR_API',
                              'api': gr.GoodReads, 'has_subs': 0},
                'HardCover': {'src': 'HC', 'author_key': 'hc_id', 'book_key': 'hc_id', 'enabled': 'HC_API',
                              'api': hc.HardCover, 'has_subs': 1},
                'GoogleBooks': {'src': 'GB', 'author_key': 'authorid', 'book_key': 'gb_id', 'enabled': 'GB_API',
                                'api': gb.GoogleBooks, 'has_subs': 1},
                'DNB': {'src': 'DN', 'author_key': 'authorid', 'book_key': 'dnb_id', 'enabled': 'DNB_API',
                        'api': dnb.DNB, 'has_subs': 1},
                'RanobeDB': {'src': 'RA', 'author_key': 'ran_id', 'book_key': 'ran_id', 'enabled': 'RAN_API',
                        'api': ran.RanobeDB, 'has_subs': 0},
                'Audible': {'src': 'AU', 'author_key': 'au_id', 'book_key': 'au_id', 'enabled': 'AU_API',
                        'api': au.Audible, 'has_subs': 0},
                }
        adminlogger = logging.getLogger('special.admin')
        adminlogger.debug(info_sources)
        return info_sources

    @staticmethod
    def get_unrarlib(config: ConfigDict):
        """ Detect presence of unrar library
            Return type of library and rarfile()
        """
        adminlogger = logging.getLogger('special.admin')
        rarfile = None
        # noinspection PyBroadException
        try:
            # noinspection PyUnresolvedReferences
            # pylint: disable=import-error
            from unrar import rarfile
            # pylint: enable=import-error
            if config.get_int('PREF_UNRARLIB') == 1:
                adminlogger.debug("Using unrar")
                return 1, rarfile
        except Exception:
            # noinspection PyBroadException
            try:
                from lib.unrar import rarfile
                if config.get_int('PREF_UNRARLIB') == 1:
                    adminlogger.debug("Using unrar")
                    return 1, rarfile
            except Exception:
                pass

        if not rarfile or config.get_int('PREF_UNRARLIB') == 2:
            # noinspection PyBroadException
            try:
                from lib.UnRAR2 import RarFile
                adminlogger.debug("Using unrar2")
                return 2, RarFile
            except Exception:
                if rarfile:
                    adminlogger.debug("Using unrar")
                    return 1, rarfile
        return 0, None

    def build_bookstrap_themes(self, prog_dir):
        themelist = []
        if not path_isdir(os.path.join(prog_dir, 'data', 'interfaces', 'bookstrap')):
            return themelist  # return empty if bookstrap interface not installed

        url = 'http://bootswatch.com/api/3.json'
        result, success = fetch_url(url, headers=None, retry=False)
        if not success:
            self.logger.debug(f"Error getting bookstrap themes : {result}")
            return themelist

        try:
            results = json.loads(result)
            for theme in results['themes']:
                themelist.append(theme['name'].lower())
        except Exception as e:
            # error reading results
            self.logger.warning(f'JSON Error reading bookstrap themes, {type(e).__name__} {str(e)}')

        self.logger.info(f"Bookstrap found {len(themelist)} themes")
        return themelist

    def build_logintemplate(self):
        default_msg = "Your lazylibrarian username is {username}\nYour password is {password}\n"
        default_msg += "You can log in to lazylibrarian and change these to something more memorable\n"
        default_msg += "You have been given {permission} access\n"
        msg_file = os.path.join(DIRS.DATADIR, 'logintemplate.text')
        if path_isfile(msg_file):
            try:
                # noinspection PyArgumentList
                with open(syspath(msg_file), encoding='utf-8') as msg_data:
                    res = msg_data.read()
                for item in ["{username}", "{password}", "{permission}"]:
                    if item not in res:
                        self.logger.warning(f"Invalid login template in {msg_file}, no {item}")
                        return default_msg
                self.logger.info(f"Loaded login template from {msg_file}")
                return res
            except Exception as e:
                self.logger.error(f'Failed to load {msg_file}, {type(e).__name__} {str(e)}')
        self.logger.debug("Using default login template")
        return default_msg

    def build_filetemplate(self):
        default_msg = "{name}{method}{link}"
        msg_file = os.path.join(DIRS.DATADIR, 'filetemplate.text')
        if path_isfile(msg_file):
            try:
                with open(syspath(msg_file), encoding='utf-8') as msg_data:
                    res = msg_data.read()
                for item in ["{name}", "{method}", "{link}"]:
                    if item not in res:
                        self.logger.warning(f"Invalid attachment template in {msg_file}, no {item}")
                        return default_msg
                self.logger.info(f"Loaded attachment template from {msg_file}")
                return res
            except Exception as e:
                self.logger.error(f'Failed to load {msg_file}, {type(e).__name__} {str(e)}')
        self.logger.debug("Using default attachment template")
        return default_msg

    def build_genres(self):
        for json_file in [os.path.join(DIRS.DATADIR, 'genres.json'),
                          os.path.join(DIRS.PROG_DIR, 'example.genres.json')]:
            if path_isfile(json_file):
                try:
                    with open(syspath(json_file), encoding='utf-8') as json_data:
                        res = json.load(json_data)
                    self.logger.info(f"Loaded genres from {json_file}")
                    return res
                except Exception as e:
                    self.logger.error(f'Failed to load {json_file}, {type(e).__name__} {str(e)}')
        self.logger.error('No valid genres.json file found')
        return {"genreLimit": 4, "genreUsers": 10, "genreExclude": [], "genreExcludeParts": [], "genreReplace": {}}

    def build_dicts(self):

        # list of all ascii and non-ascii quotes/apostrophes
        # quote list: https://en.wikipedia.org/wiki/Quotation_mark
        # https://hexdocs.pm/ex_unicode/Unicode.Category.QuoteMarks.html

        quotes = {
            '\u0022': "'",  # quotation mark (")
            '\u0027': "'",  # apostrophe (')
            '\u0060': "'",  # grave-accent
            '\u00a8': '"',  # DIAERESIS
            '\u00ab': '"',  # left-pointing double-angle quotation mark
            '\u00b4': "'",  # acute accent
            '\u00bb': '"',  # right-pointing double-angle quotation mark
            '\u2018': "'",  # left single quotation mark
            '\u2019': "'",  # right single quotation mark
            '\u201a': "'",  # single low-9 quotation mark
            '\u201b': "'",  # single high-reversed-9 quotation mark
            '\u201c': '"',  # left double quotation mark
            '\u201d': '"',  # right double quotation mark
            '\u201e': '"',  # double low-9 quotation mark
            '\u201f': '"',  # double high-reversed-9 quotation mark
            '\u2039': "'",  # single left-pointing angle quotation mark
            '\u203a': "'",  # single right-pointing angle quotation mark
            '\u300c': "'",  # left corner bracket
            '\u300d': "'",  # right corner bracket
            '\u300e': "'",  # left white corner bracket
            '\u300f': "'",  # right white corner bracket
            '\u301d': '"',  # reversed double prime quotation mark
            '\u301e': '"',  # double prime quotation mark
            '\u301f': '"',  # low double prime quotation mark
            '\ufe41': "'",  # presentation form for vertical left corner bracket
            '\ufe42': "'",  # presentation form for vertical right corner bracket
            '\ufe43': "'",  # presentation form for vertical left corner white bracket
            '\ufe44': "'",  # presentation form for vertical right corner white bracket
            '\uff02': "'",  # fullwidth quotation mark
            '\uff07': "'",  # fullwidth apostrophe
            '\uff62': "'",  # halfwidth left corner bracket
            '\uff63': "'",  # halfwidth right corner bracket
        }
        for json_file in [os.path.join(DIRS.DATADIR, 'dicts.json')]:
            if path_isfile(json_file):
                try:
                    with open(syspath(json_file), encoding='utf-8') as json_data:
                        res = json.load(json_data)
                    self.logger.info(f"Loaded dicts from {json_file}")
                    return res
                except Exception as e:
                    self.logger.error(f'Failed to load {json_file}, {type(e).__name__} {str(e)}')
        self.logger.debug('No valid dicts.json file found, using defaults')
        return {"filename_dict": {'<': '', '>': '', '...': '', ' = ': ' ', '?': '', '$': 's', '|': '',
                                  ' + ': ' ', '"': '', ',': '', '*': '', ':': '', ';': '', '\'': '', '/': '_',
                                  '\\\\': '\\'},
                "apostrophe_dict": quotes
                }

    def build_monthtable(self, config: ConfigDict):
        adminlogger = logging.getLogger('special.admin')
        seasons = {}
        table = None
        json_file = os.path.join(DIRS.DATADIR, 'seasons.json')
        if path_isfile(json_file):
            try:
                with open(syspath(json_file)) as json_data:
                    seasons = json.load(json_data)
                self.logger.debug(f'Loaded seasons.json : {seasons}')
            except Exception as e:
                self.logger.error(f'Failed to load seasons.json, {type(e).__name__} {str(e)}')
        if not seasons:
            seasons = {"winter": 1, "spring": 4, "summer": 7, "fall": 10,
                       "autumn": 10, "christmas": 12}
        adminlogger.debug(seasons)

        json_file = os.path.join(DIRS.DATADIR, 'monthnames.json')
        if path_isfile(json_file):
            try:
                with open(syspath(json_file)) as json_data:
                    table = json.load(json_data)
                mlist = ''
                if not validate_monthtable(table):
                    self.logger.error("monthnames.json is invalid")
                    table = []
                if table:
                    # only print alternate entries as each language is in twice (long and short month names)
                    for item in table[0][::2]:
                        mlist += f"{item} "
                    self.logger.debug(f'Loaded monthnames.json : {mlist}')
            except Exception as e:
                self.logger.error(f'Failed to load monthnames.json, {type(e).__name__} {str(e)}')

        if not table:
            # Default Month names table to hold long/short month names for multiple languages
            # which we can match against magazine issues
            self.logger.debug('Using default monthnames')
            table = [
                ['en_GB.UTF-8', 'en_GB.UTF-8'],
                ['January', 'Jan'],
                ['February', 'Feb'],
                ['March', 'Mar'],
                ['April', 'Apr'],
                ['May', 'May'],
                ['June', 'Jun'],
                ['July', 'Jul'],
                ['August', 'Aug'],
                ['September', 'Sep'],
                ['October', 'Oct'],
                ['November', 'Nov'],
                ['December', 'Dec']
            ]

            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(table, f, ensure_ascii=False)

        adminlogger.debug(table)
        # Create a second copy of the monthnames without accents and lowercased to speed up matching
        cleantable = []
        for lyne in table:
            cleanlyne = []
            for item in lyne:
                cleanlyne.append(unaccented(item).lower().strip('.'))
            cleantable.append(cleanlyne)

        monthnames = [table, cleantable]
        return monthnames, seasons

    def update_znab_caps(self):
        caps_changed = False
        for provider in CONFIG.providers('NEWZNAB'):
            if provider['ENABLED']:
                updated = get_capabilities(provider)
                if updated:
                    self.logger.debug(f"Updated caps for {provider['DISPNAME']}")
                    caps_changed = True
        for provider in CONFIG.providers('TORZNAB'):
            if provider['ENABLED']:
                updated = get_capabilities(provider)
                if updated:
                    self.logger.debug(f"Updated caps for {provider['DISPNAME']}")
                    caps_changed = True
        if caps_changed:
            CONFIG.save_config_and_backup_old(section='Capabilities')

    def create_version_file(self, filename):
        # flatpak insists on PROG_DIR being read-only so we have to move version.txt into CACHEDIR
        old_file = os.path.join(DIRS.PROG_DIR, filename)
        version_file = os.path.join(DIRS.CACHEDIR, filename)
        if path_isfile(old_file):
            if not path_isfile(version_file):
                try:
                    with open(syspath(old_file)) as s, open(syspath(version_file), 'w') as d:
                        d.write(s.read())
                except OSError:
                    self.logger.warning(f"Unable to copy {filename}")
            with contextlib.suppress(OSError):
                os.remove(old_file)
        return version_file

    def init_version_checks(self, version_file):
        if CONFIG.get_int('VERSIONCHECK_INTERVAL') == 0:
            self.logger.debug('Automatic update checks are disabled')
            # pretend we're up to date so we don't keep warning the user
            # version check button will still override this if you want to
            CONFIG.set_str('LATEST_VERSION', CONFIG['CURRENT_VERSION'])
            CONFIG.set_int('COMMITS_BEHIND', 0)
        else:
            # Set the install type (win,git,source) &
            # check the version when the application starts
            versioncheck.check_for_updates()

            self.logger.debug(
                f"Current Version [{CONFIG['CURRENT_VERSION']}] - Latest remote version "
                f"[{CONFIG['LATEST_VERSION']}] - Install type [{CONFIG['INSTALL_TYPE']}]")

            if CONFIG.get_int('GIT_UPDATED') == 0 and CONFIG['LATEST_VERSION'].startswith(CONFIG['CURRENT_VERSION']):
                # we don't know when the last update was
                # (docker doesn't set timestamp or it's a first time install)
                # allow comparison of long and short hashes
                CONFIG.set_int('GIT_UPDATED', int(time.time()))
                self.logger.debug('Setting update timestamp to now')

        # if gitlab doesn't recognise a hash it returns 0 commits
        if not CONFIG['LATEST_VERSION'].startswith(CONFIG['CURRENT_VERSION']) \
                and CONFIG.get_int('COMMITS_BEHIND') == 0:
            if CONFIG['INSTALL_TYPE'] == 'git':
                res, _ = versioncheck.run_git('remote -v')
                if 'gitlab.com' in str(res):
                    self.logger.warning('Unrecognised version, LazyLibrarian may have local changes')
            elif CONFIG['INSTALL_TYPE'] == 'source':
                self.logger.warning(
                    f"Unrecognised version [{CONFIG['CURRENT_VERSION']}] to force upgrade delete {version_file}")

        if not path_isfile(version_file) and CONFIG['INSTALL_TYPE'] == 'source':
            # User may be running an old source zip, so try to force update
            CONFIG.set_int('COMMITS_BEHIND', 1)
            lazylibrarian.SIGNAL = 'update'
            # but only once in case the update fails, don't loop
            with open(syspath(version_file), 'w') as f:
                f.write("UNKNOWN SOURCE")

        if CONFIG.get_int('COMMITS_BEHIND') <= 0:
            lazylibrarian.SIGNAL = None
            if CONFIG.get_int('COMMITS_BEHIND') == 0:
                self.logger.debug('Not updating, LazyLibrarian is already up to date')
            else:
                self.logger.debug('Not updating, LazyLibrarian has local changes')

        if '** MANUAL **' in lazylibrarian.COMMIT_LIST:
            lazylibrarian.SIGNAL = None
            self.logger.info("Update available, but needs manual installation")

    def launch_browser(self, host, port, root):
        import webbrowser
        if host == '0.0.0.0':
            host = 'localhost'

        if CONFIG.get_bool('HTTPS_ENABLED'):
            protocol = 'https'
        else:
            protocol = 'http'
        if root and not root.startswith('/'):
            root = f"/{root}"
        try:
            webbrowser.open(f'{protocol}://{host}:{port}{root}/home')
        except Exception as e:
            self.logger.error(f'Could not launch browser:{type(e).__name__}  {str(e)}')

    @staticmethod
    def start_schedulers():
        if CONFIG['GR_URL'] == 'https://goodreads.org':
            CONFIG.set_url('GR_URL', 'https://www.goodreads.com')
        if CONFIG['TELEMETRY_SERVER'] == 'https://conceded-moose-5564.dataplicity.io/telemetry':
            CONFIG.set_url('TELEMETRY_SERVER', 'https://lazylibrarian.telem.ch')
        # Crons and scheduled jobs started here
        # noinspection PyUnresolvedReferences
        startscheduler()
        # wipe any aborted entries if running when shutdown
        db = database.DBConnection()
        columns = db.select('PRAGMA table_info(jobs)')
        if columns:  # check for no such table
            db.action("UPDATE jobs SET Finish=Start WHERE Finish<Start")
        db.close()
        if not lazylibrarian.STOPTHREADS:
            restart_jobs(command=SchedulerCommand.START)

    def shutdown(self, restart=False, update=False, doquit=False, testing=False):
        shutdownscheduler()
        if not testing and not (update and doquit):  # commandline update, don't save config as no filename
            if self.logger.isEnabledFor(logging.DEBUG):  # TODO add a separate setting
                CONFIG.create_access_summary(syspath(DIRS.get_logfile('configaccess.log')))
            CONFIG.add_access_errors_to_log()
            CONFIG.save_config_and_backup_old(restart_jobs=False)

        if not restart and not update:
            self.logger.info(f'LazyLibrarian (pid {os.getpid()}) is shutting down...')
        # We are now shutting down. Remove all file handlers from the logger, keeping only console handlers
        rootlogger = logging.getLogger('root')
        for handler in rootlogger.handlers:
            if handler.name != 'console':
                rootlogger.removeHandler(handler)

        updated = False
        if update:
            self.logger.info('LazyLibrarian is updating...')
            try:
                updated = versioncheck.update()
                if updated:
                    self.logger.info('Lazylibrarian version updated')
                    makocache = os.path.join(DIRS.CACHEDIR, 'mako')
                    with contextlib.suppress(FileNotFoundError):
                        rmtree(makocache)
                    os.makedirs(makocache)
                    if CONFIG.configfilename:
                        # won't have one if  --update
                        CONFIG.save_config_and_backup_old(section='Git')
            except Exception as e:
                self.logger.warning(f'LazyLibrarian failed to update: {type(e).__name__} {str(e)}. Restarting.')
                self.logger.error(str(traceback.format_exc()))

        if lazylibrarian.PIDFILE:
            self.logger.info(f'Removing pidfile {lazylibrarian.PIDFILE}')
            os.remove(syspath(lazylibrarian.PIDFILE))

        if not doquit and not docker():
            self.logger.info('LazyLibrarian is restarting ...')
            # Try to use the currently running python executable, as it is known to work
            # if not able to determine, sys.executable returns empty string or None
            # and we have to go looking for it...
            executable = sys.executable

            if not executable:
                prg = "python3"
                if os.name == 'nt':
                    params = ["where", prg]
                    try:
                        executable = subprocess.check_output(params, stderr=subprocess.STDOUT)
                        executable = make_unicode(executable).strip()
                    except Exception as e:
                        self.logger.debug(f"where {prg} failed: {type(e).__name__} {str(e)}")
                else:
                    params = ["which", prg]
                    try:
                        executable = subprocess.check_output(params, stderr=subprocess.STDOUT)
                        executable = make_unicode(executable).strip()
                    except Exception as e:
                        self.logger.debug(f"which {prg} failed: {type(e).__name__} {str(e)}")

            if not executable:
                executable = 'python'  # default if not found

            popen_list = [executable, DIRS.FULL_PATH]
            popen_list += DIRS.ARGS
            while '--update' in popen_list:
                popen_list.remove('--update')
            while '--upgrade' in popen_list:
                popen_list.remove('--upgrade')
            if '--nolaunch' not in popen_list:
                popen_list += ['--nolaunch']

            with open(syspath(DIRS.get_logfile('upgrade.log')), 'a') as upgradelog:
                if updated:
                    upgradelog.write(f"{time.ctime()} {'Restarting LazyLibrarian with ' + str(popen_list)}\n")
                subprocess.Popen(popen_list, cwd=os.getcwd())
                doquit = True
                if cherrypy.server.httpserver is not None:
                    # updating a running instance, not an --update
                    # wait for the new instance to open the httpserver
                    cherrypy.engine.stop()
                    cherrypy.server.httpserver = None
                    host = CONFIG['HTTP_HOST']
                    if '0.0.0.0' in host:
                        host = 'localhost'  # windows doesn't like 0.0.0.0

                    if not host.startswith('http'):
                        host = f"http://{host}"

                    # depending on proxy might need host:port/root or just host/root
                    if CONFIG['HTTP_ROOT']:
                        server1 = f"{host}:{CONFIG['HTTP_PORT']}/{CONFIG['HTTP_ROOT'].lstrip('/')}"
                        server2 = f"{host}/{CONFIG['HTTP_ROOT'].lstrip('/')}"
                    else:
                        server1 = f"{host}:{CONFIG['HTTP_PORT']}"
                        server2 = ''

                    msg = f"Waiting for {server1} to start"
                    if updated:
                        upgradelog.write(f"{time.ctime()} {msg}\n")
                    self.logger.info(msg)
                    pawse = 18
                    success = False
                    res = ''
                    while pawse:
                        # noinspection PyBroadException
                        try:
                            r = requests.get(server1)
                            res = r.status_code
                            if res == 200 or res == 401:
                                success = True
                                break
                        except Exception:
                            r = None

                        if not r and server2:
                            # noinspection PyBroadException
                            try:
                                r = requests.get(server2)
                                res = r.status_code
                                if res == 200 or res == 401:
                                    success = True
                                    break
                            except Exception:
                                pass

                        print(f"Waiting... {pawse} {res}")
                        time.sleep(5)
                        pawse -= 1
                    if update:
                        archivename = 'backup.tgz'
                        if success:
                            msg = f'Reached webserver page {res}, deleting backup'
                            doquit = True
                            if updated:
                                upgradelog.write(f"{time.ctime()} {msg}\n")
                            self.logger.info(msg)
                            try:
                                os.remove(syspath(archivename))
                            except OSError as e:
                                if e.errno != 2:  # doesn't exist is ok
                                    msg = f'{type(e).__name__} deleting backup file: {archivename} {e.strerror}'
                                    self.logger.warning(msg)
                        else:
                            msg = 'Webserver failed to start, reverting update'
                            upgradelog.write(f"{time.ctime()} {msg}\n")
                            self.logger.info(msg)
                            cherrypy.engine.start()
                            if tarfile.is_tarfile(archivename):
                                try:
                                    with tarfile.open(archivename) as tar:
                                        tar.extractall()
                                    success = True
                                except Exception as e:
                                    msg = f'Failed to unpack tarfile {type(e).__name__} ({archivename}): {str(e)}'
                                    upgradelog.write(f"{time.ctime()} {msg}\n")
                                    self.logger.warning(msg)
                            else:
                                msg = "Invalid archive"
                                upgradelog.write(f"{time.ctime()} {msg}\n")
                                self.logger.warning(msg)
                            if success:
                                current_version = ''
                                version_file = os.path.join(DIRS.CACHEDIR, 'version.txt')
                                old_location = os.path.join(DIRS.PROG_DIR, 'version.txt')
                                if os.path.isfile(old_location):
                                    with open(old_location) as fp:
                                        current_version = fp.read().strip(' \n\r')
                                    self.logger.debug(f'Moving {old_location} to {version_file}')
                                    move(old_location, version_file)
                                msg = f"Restarting {current_version} from backup"
                                upgradelog.write(f"{time.ctime()} {msg}\n")
                                self.logger.info(msg)
                                subprocess.Popen(popen_list, cwd=os.getcwd())

        if doquit and not testing:
            self.logger.info(f'Lazylibrarian (pid {os.getpid()}) is exiting')
            cherrypy.engine.stop()
            # Do this as the last step before existing
            UNBUNDLER.remove_bundled_modules()
            sys.exit(0)
