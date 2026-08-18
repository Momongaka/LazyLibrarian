#  This file is part of Lazylibrarian.
#
# Purpose:
#   Type-aware handling config.ini, access to its properties, etc.
#   Intended to entirely replace the previous file, config.py, as
#   well as many global variables

import io
import logging
import os
import re
import shutil
import threading
from collections import Counter
from collections.abc import Generator
from configparser import ConfigParser
from os import sep
from urllib.parse import unquote_plus

from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.configarray import ArrayConfig
from lazylibrarian.configdefs import ARRAY_DEFS, BASE_DEFAULTS, configitem_from_default
from lazylibrarian.configenums import Access, OnChangeReason
from lazylibrarian.configtypes import (
    CaseInsensitiveDict,
    ConfigBool,
    ConfigDict,
    ConfigDictListIterator,
    ConfigItem,
    ConfigScheduler,
    ErrorListIterator,
)
from lazylibrarian.filesystem import DIRS, path_exists, syspath
from lazylibrarian.formatter import plural, thread_name
from lazylibrarian.logconfig import LOGCONFIG


class LLConfigHandler(ConfigDict):
    """ Main configuration handler for LL """
    arrays: dict[str, ArrayConfig]  # (section, array)
    configfilename: str
    REDACTLIST: list[str]
    logger: logging.Logger

    def __init__(self, defaults: list[ConfigItem] | None = None, configfile: str | None = None):
        super().__init__()
        self.initialize_logger()
        self.arrays = {}
        self.defaults = defaults
        self._save_lock = threading.Lock()
        self._copydefaults(defaults)
        self.configfilename = ''
        self.load_configfile(configfile)

    def initialize_logger(self):
        self.logger = logging.getLogger(__name__)

    def load_configfile(self, configfile: str | None = None):
        if self.configfilename:
            # Clear existing before loading another setup, brute force.
            super().clear()
            self.arrays.clear()
            # Re-setup the defaults
            self._copydefaults(self.defaults)
        if configfile:
            self.configfilename = configfile
            # interpolation=None is used to allow passwords containing %
            parser = ConfigParser(dict_type=CaseInsensitiveDict, interpolation=None)
            parser.optionxform = lambda optionstr: optionstr.upper()
            parser.read(syspath(configfile))
            for section in parser.sections():
                if section[-1:].isdigit():
                    try:
                        self._load_array_section(section, parser)
                    except Exception as e:
                        self.logger.warning(f"Error loading {section}: {parser}: {str(e)}")
                else:
                    self._load_section(section, parser, self)
            self._update_redactlist()
        else:
            self.configfilename = ''
        try:
            self.ensure_arrays_have_empty_item()
        except Exception as e:
            self.logger.warning(f"Error ensuring empty items {str(e)}")

    def _copydefaults(self, defaults: list[ConfigItem] | None = None):
        """ Copy the default values and settings for the given config """
        if defaults:
            for config_item in defaults:
                key = config_item.key.upper()
                item = configitem_from_default(config_item)
                self.config[key] = item
                if item.onchange:
                    item.onchange(item.get_str(), OnChangeReason.COPYING)

    def _load_section(self, section: str, parser: ConfigParser, config: ConfigDict):
        """ Load a section of an ini file """
        for option in parser.options(section):
            if option in config:
                config_item = config.get_item(option)
                if not config_item or not config_item.update_from_parser(parser, section, option):
                    self.logger.warning(f"Error loading {section}.{option} as {parser.get(section, option)}")
            else:
                self.logger.warning(f"Unknown option {section}.{option} in config")

    def _load_array_section(self, section: str, parser: ConfigParser):
        """ Load a section of an ini file, where that section is part of an array """
        arrayname, index = re.split(r'(^\D+)', section)[1:]
        arrayname = arrayname.upper()
        if arrayname[-1:] == '_':
            arrayname = arrayname[:-1]
        index = int(index)
        defaults = ARRAY_DEFS.get(arrayname, None)
        if defaults:
            self.logger.debug(f"Loading array {arrayname} index {index}")
            if arrayname not in self.arrays:
                self.arrays[arrayname] = ArrayConfig(arrayname, defaults)

            self.arrays[arrayname].setupitem_at(index)
            array = self.arrays[arrayname][index]
            self._load_section(section, parser, array)
        else:
            self.logger.warning(f"Cannot load array {section}: Undefined")

    def ensure_arrays_have_empty_item(self):
        """ Make sure every array has an empty item for users to configure """
        for name in ARRAY_DEFS:
            if name not in self.arrays:
                self.arrays[name] = ArrayConfig(name, ARRAY_DEFS[name])
            self.arrays[name].ensure_empty_end_item()

    """ Handle array entries """

    def get_array_entries(self, wantname: str) -> int:
        """ Return number of entries in a particular array config """
        rc = 0
        found = False
        if not wantname:
            return rc
        wantname = wantname.upper()
        for name in self.arrays.keys():
            if name[:len(wantname)] == wantname:
                rc += len(self.arrays[name])
                found = True

        if not found:
            self._handle_access_error(wantname, Access.READ_ERR)
        return rc

    def get_array(self, wantname: str) -> ArrayConfig | None:
        """ Return the config for an array, like 'APPRISE', or None """
        if wantname.upper() in self.arrays:
            return self.arrays[wantname.upper()]
        return None

    def get_array_dict(self, wantname: str, wantindex: int) -> ConfigDict | None:
        """ Return the complete config for an entry, like ('APPRISE', 0) """
        wantname = wantname.upper()
        if wantname in self.arrays and self.arrays[wantname].has_index(wantindex):
            return self.arrays[wantname][wantindex]
        return None

    def get_array_str(self, __array: str, index: int, __key: str) -> str:
        """ Access a single array string config directly """
        return self.arrays[__array.upper()][index][__key.upper()]

    def providers(self, name: str) -> ArrayConfig:
        """ Return an iterable list of providers """
        array = self.get_array(name)
        if array:
            return array
        self._handle_access_error(name, Access.READ_ERR)
        raise Exception(f'Cannot iterate over non-existent array {name}')

    @staticmethod
    def provider_names() -> list[str]:
        """ Return the list of all base provider names that can be passed to providers(name) """
        names = []
        for name, _ in ARRAY_DEFS.items():
            names.append(name)
        return names

    def get_schedulers(self, name: str = '') -> Generator[tuple[str, ConfigScheduler], None, None]:
        """ Return an iterable list of schedulers that matches name, or all """
        for key, item in self.config.items():
            if (name == '' or name.lower() in key.lower()) and isinstance(item, ConfigScheduler):
                yield key, item

    def get_all_accesses(self) -> dict[str, Counter]:
        """ Get a list of all config values that have been accessed  """
        result = {}
        for _key, value in self.all_configs():
            a = value.get_accesses()
            if len(a):
                result[value.get_full_name()] = a
        return result

    def all_configs(self) -> ConfigDictListIterator:
        """ Used to iterate over all the config items, whether base or array """
        alldicts = [self]
        for array in self.arrays.values():
            for _, item in enumerate(array):
                alldicts += [item]
        return ConfigDictListIterator(alldicts)

    def all_error_lists(self) -> ErrorListIterator:
        alldicts = [self]
        for array in self.arrays.values():
            for _, item in enumerate(array):
                alldicts += [item]
        return ErrorListIterator(alldicts)

    def clear_access_counters(self):
        """ Clear all counters. Used after sending telemetry or saving config """
        for _, value in self.all_configs():
            value.get_accesses().clear()

        for errors in self.all_error_lists():
            errors.clear()

    def update_providers_from_ui(self, kwargs):
        """ Update all provider arrays with a settings array from the web UI.
            Assumes that all UI settings are of the form section_num_setting=value """
        for pname, array in self.arrays.items():
            for inx, config in enumerate(array):
                # noinspection PyUnresolvedReferences
                for key, item in config.items():
                    setting = f'{pname.lower()}_{inx}_{key.lower()}'
                    value = kwargs.get(setting)
                    if value and key in ['HOST', 'SERVER', 'URL']:
                        unquoted_value = unquote_plus(value)
                        for token in ['<', '&', '>', '=', '"', "'", '+', '(', ')']:
                            if token in unquoted_value:
                                if token in ['&', '='] and key.lower() in ['host']:
                                    pass
                                else:
                                    self.logger.warning(f'Cannot set value of {pname}{inx} {key}, contains "{token}"')
                                    value = None
                                    break
                    if value is not None:
                        if key.lower() == 'apilimit' and isinstance(value, list):
                            value = value[0]
                        item.set_from_ui(value)
                    elif isinstance(item, ConfigBool):  # Bools that are not listed are False
                        item.set_from_ui(False)

    def reset_to_default(self, keys: list[str]):
        for key in keys:
            item = self.get_item(key)
            if item:
                item.reset_to_default()
            else:
                self.logger.warning(f'Cannot reset value of {key} as it does not exist')

    def scheduler_can_run(self, scheduler: ConfigScheduler) -> bool:
        """ Return True if the scheduler's requirements are satisfied """
        ok = scheduler.get_int() > 0  # 0 means schedule is disabled
        if ok and scheduler.needs_provider:
            ok = self.use_any()
        if ok and scheduler.run_name == 'GRSYNC':  # Special case, should maybe add option to object
            ok = self.get_bool('GR_SYNC')
        if ok and scheduler.run_name == 'HCSYNC':  # Special case, should maybe add option to object
            ok = self.get_bool('HC_SYNC')
        if ok and scheduler.run_name == 'AUSYNC':  # Special case, should maybe add option to object
            ok = self.get_bool('AU_SYNC')
        if ok and scheduler.run_name == 'TELEMETRYSEND':  # Special case for telemetry
            ok = self.config['TELEMETRY_ENABLE'].get_bool()
        if ok and scheduler.run_name == 'SEARCHALLRSS':  # Special case for RSS
            ok = self.use_rss()
        return ok

    def save_config_to_string(self, save_all: bool = False, redact: bool = False) -> (int, str):

        def add_to_parser(aparser: ConfigParser, asectionname: str, akey: str, aitem: ConfigItem) -> int:
            """ Add item to parser, return 1 if added, 0 if ignored """
            if aitem.do_persist() and (save_all or not aitem.is_default()):
                if asectionname not in aparser:
                    aparser[asectionname] = {}
                save_str = aitem.get_save_str()
                if redact:
                    for word in self.REDACTLIST:
                        if word in save_str:
                            save_str = save_str.replace(word, '[redacted]')
                aparser[asectionname][akey] = save_str
                return 1
            return 0

        parser = ConfigParser(interpolation=None)
        parser.optionxform = lambda optionstr: optionstr.lower()

        count = 0
        for key, item in self.config.items():
            count += add_to_parser(parser, item.section, key, item)

        for _name, array in self.arrays.items():
            array.cleanup_for_save()
            for inx in range(len(array)):
                try:
                    config = array[inx]
                    sectionname = array.get_section_str(inx)
                    for key, item in config.items():
                        count += add_to_parser(parser, sectionname, key, item)
                except KeyError:
                    pass
            array.ensure_empty_end_item()
        try:
            output = io.StringIO()
            parser.write(output)
            return count, output.getvalue()
        except Exception as e:
            self.logger.warning(f'Error saving config: {type(e).__name__} {str(e)}')
            return -1

    def save_config_to_filename(self, filename: str, save_all: bool = False, redact: bool = False) -> int:
        """
        Save the configuration to a new file. Return number of items stored, -1 if error.
        If save_all, saves all possible config items. If False, saves only changed items
        """
        try:
            count, configstr = self.save_config_to_string(save_all=save_all, redact=redact)
            with open(filename, "w") as configfile:
                configfile.write(configstr)
        except Exception as e:
            self.logger.warning(f'Error saving config file {filename}: {type(e).__name__} {str(e)}')
            count = -1
        finally:
            for _, array in self.arrays.items():
                array.ensure_empty_end_item()
        return count

    def save_config_and_backup_old(self, save_all: bool = False, section: str | None = None,
                                   restart_jobs: bool = False) -> int:
        """
        Renames the old config file to .bak and saves new config file.
        Return number of items stored, -1 if error.
        """

        if not self.configfilename:
            self.logger.error('Cannot save and backup config without a filename')
            return -1

        currentname = thread_name()
        thread_name("CONFIG2_WRITE")
        try:
            with self._save_lock:
                self.logger.debug(f'Saving configuration to {self.configfilename}')
                savecount = self.save_config_to_filename(syspath(f"{self.configfilename}.new"), save_all)
                if savecount == 0:
                    return 0
                if savecount < 0:
                    self.logger.error("Error saving config")
                    return -1
                import os

                msg = ''
                # Keep two generations of backup: a single bad save (e.g. process killed
                # mid-write, or config.ini missing at load time) should not also destroy
                # the last known-good backup.
                try:
                    os.remove(syspath(f"{self.configfilename}.bak2"))
                except OSError as e:
                    if e.errno != 2:  # doesn't exist is ok
                        self.logger.warning(
                            f'{type(e).__name__} deleting backup file:{self.configfilename} .bak2 {e.strerror}')
                try:
                    os.rename(syspath(f"{self.configfilename}.bak"), syspath(f"{self.configfilename}.bak2"))
                except OSError as e:
                    if e.errno != 2:  # doesn't exist is ok as wouldn't exist until second save
                        self.logger.warning(
                            f'Unable to rotate backup file: {self.configfilename} .bak {type(e).__name__} {e.strerror}')
                try:
                    os.rename(syspath(self.configfilename), syspath(f"{self.configfilename}.bak"))
                except OSError as e:
                    if e.errno != 2:  # doesn't exist is ok as wouldn't exist until first save
                        msg = f'Unable to backup config file: {self.configfilename} {type(e).__name__} {e.strerror}'
                        self.logger.warning(msg)
                try:
                    os.rename(syspath(f"{self.configfilename}.new"), syspath(self.configfilename))
                except OSError as e:
                    msg = f'Unable to rename new config file: {self.configfilename} {type(e).__name__} {e.strerror}'
                    self.logger.warning(msg)

                if not msg:
                    if section:
                        msg = f'Config file {self.configfilename} has been saved with {savecount} items ' \
                                  f'(Triggered by {section})'
                    else:
                        msg = f'Config file {self.configfilename} has been saved with {savecount} items'
                    self.logger.info(msg)
                    return savecount
                return -1
        finally:
            from lazylibrarian.telemetry import record_usage_data
            record_usage_data()
            thread_name(currentname)
            # Only clear counters if we save the entire config
            clear: bool = not (section and section != '')
            self.post_save_actions(restart_jobs=restart_jobs, clear_counters=clear)

    def pre_load_fixup(self, configfile: str | None = None) -> int:
        """
        Perform pre-load operations specific to LL.
        Returns 0 if ok, otherwise number of warnings
        Rename hc_api = Bearer...  as hc_api is now BOOL, store old key in hc_key
        """
        if not configfile:
            self.logger.warning("No configfile")
            return 1
        try:
            with open(configfile) as i, open(f"{configfile}.new", 'w') as o:
                for lyne in i.readlines():
                    if lyne.startswith('hc_api') and 'Bearer' in lyne:
                        lyne = lyne.replace('hc_api', 'hc_key')
                        o.write(lyne)
                        o.write('hc_api = True\n')
                    else:
                        o.write(lyne)
            try:
                os.remove(configfile)
                os.rename(f"{configfile}.new", configfile)
            except OSError as e:
                msg = f'Unable to rename new config file: {configfile} {type(e).__name__} {e.strerror}'
                self.logger.warning(msg)
                return 1
            return 0
        except Exception:
            self.logger.warning(f"Unable to pre_load_fixup {configfile}")
            return 1

    def post_load_fixup(self) -> int:
        """
        Perform post-load operations specific to LL.
        Returns 0 if ok, otherwise number of warnings
        """
        warnings = 0
        self.logger.debug('Performing post-load fixup on config')
        DIRS.set_config(self)
        DIRS.ensure_log_dir()

        if str(self.config['AUDIOBOOK_DEST_FOLDER']) == 'None':
            self.config['AUDIOBOOK_DEST_FOLDER'].set_str(self.config['EBOOK_DEST_FOLDER'].get_str())

        for fname in ['EBOOK_DEST_FILE', 'MAG_DEST_FILE', 'AUDIOBOOK_DEST_FILE', 'AUDIOBOOK_SINGLE_FILE']:
            if sep in self.config[fname].get_str():
                self.logger.warning(f'Please check your {fname} setting, contains "{sep}"')
                warnings += 1

        if str(self.config['HTTP_LOOK']) in ['legacy', 'default']:
            self.logger.warning('configured interface is deprecated, new features are in bookstrap')
            self.config['HTTP_LOOK'].set_str('bookstrap')
            warnings += 1

        self.ensure_valid_homepage()

        if self.config['SSL_CERTS'].get_str() != '' and not path_exists(str(self.config['SSL_CERTS'])):
            self.logger.warning(f"SSL_CERTS [{str(self.config['SSL_CERTS'])}] not found")
            self.config['SSL_CERTS'].set_str('')
            warnings += 1

        return warnings

    def ensure_valid_homepage(self):
        """ Make sure the HOMEPAGE is one that is included """
        if str(self.config['HOMEPAGE']) == 'eBooks' and not self.get_bool('EBOOK_TAB') or \
                str(self.config['HOMEPAGE']) == 'AudioBooks' and not self.get_bool('AUDIO_TAB') or \
                str(self.config['HOMEPAGE']) == 'Magazines' and not self.get_bool('MAG_TAB') or \
                str(self.config['HOMEPAGE']) == 'Comics' and not self.get_bool('COMIC_TAB') or \
                str(self.config['HOMEPAGE']) == 'Series' and not self.get_bool('SERIES_TAB'):
            self.config['HOMEPAGE'].set_str('')

    def post_save_actions(self, restart_jobs: bool = True, clear_counters: bool = False):
        """ Run activities after saving, such as rescheduling jobs that may have changed """
        # Re-initialize cached loggers
        self.initialize_logger()
        DIRS.initialize_logger()

        # Clean the mako cache if the interface has changed
        interface = self.config['HTTP_LOOK']
        if interface.get_write_count() > 0:  # It's changed
            mako_dir = DIRS.get_mako_cachedir()
            self.logger.debug("Clearing mako cache")
            shutil.rmtree(mako_dir)
            os.makedirs(mako_dir)

        # Restart all scheduled jobs since the schedules may have changed
        if restart_jobs:
            from lazylibrarian.scheduling import SchedulerCommand, schedule_job
            for _, item in self.config.items():
                schedule = item.get_schedule_name()
                if schedule and isinstance(item, ConfigScheduler):
                    if schedule in ['clean_cache', 'backup']:
                        item.set_int(30)  # dummy value for now, adjusted later
                    if self.scheduler_can_run(item):
                        self.logger.debug(f"Restarting job {schedule}, interval {item.get_int()}")
                        schedule_job(SchedulerCommand.RESTART, schedule)
                    else:
                        self.logger.debug(f"Stopping job {schedule}")
                        schedule_job(SchedulerCommand.STOP, schedule)

        # Update the redact list since things may have changed
        self._update_redactlist()

        if clear_counters:
            # Clear all access counters, so we can tell if something has changed later
            self.clear_access_counters()

    def add_access_errors_to_log(self):
        """ For use at end of program. Add any access errors to the log file, so they
        are easy to find. """
        for _key, value in self.all_configs():
            accesses = value.get_accesses()
            for aname, count in accesses.items():
                if aname in [Access.READ_ERR, Access.WRITE_ERR, Access.FORMAT_ERR]:
                    self.logger.error(f'Config {aname.name}: {value.get_full_name()}, {count} times')

        for errorlist in self.all_error_lists():
            for key, counter in errorlist.items():
                for ename, count in counter.items():
                    self.logger.error(f'Config {ename.name}: {key}, {count} times')

    def create_access_summary(self, saveto: str = '') -> dict:
        """ For debugging: Create a summary of all accesses, potentially
        highlighting places where config2 is used incorrectly or where things
        are highly inefficient """

        # Collate all access attempts to keys that exist
        access_summary = {}
        for a in Access:
            access_summary[a.name] = []

        for _key, value in self.all_configs():
            accesses = value.get_accesses()
            for aname, count in accesses.items():
                access_summary[aname.name].append((value.get_full_name(), count))

        for errorlist in self.all_error_lists():
            for key, counter in errorlist.items():
                for ename, count in counter.items():
                    access_summary[ename.name].append((key, count))

        if saveto:
            self.save_access_summary(saveto, access_summary)

        return access_summary

    @staticmethod
    def save_access_summary(saveto: str, access_summary):
        """ For debugging: Create a summary of all config accesses by type """

        with open(saveto, "w") as file:
            file.write('*** Config Item Access Summary ***\n')
            for sumtype, summary in access_summary.items():
                if len(summary) > 0:
                    file.writelines(f'Access type: {sumtype}\n')
                    for line in summary:
                        # Format:  NameOfKey--------------------- Count--
                        file.writelines(f'  {line[0]:30}: {line[1]:7}\n')

    def _update_redactlist(self):
        """ Update REDACTLIST after config changes """

        self.REDACTLIST = []
        wordlist = ['PASS', 'TOKEN', 'SECRET', '_API', '_USER', '_DEV', '_KEY', 'ADMIN_', '_AUTH_']
        if self.get_bool('HOSTREDACT'):
            wordlist.append('_HOST')
        for key in self.config.keys():
            if key not in ['BOOK_API', 'GIT_USER', 'SINGLE_USER']:
                for word in wordlist:
                    if word in key and self[key] and len(self[key]) > 3:
                        self.REDACTLIST.append(f"{self[key]}")
        for key in ['EMAIL_FROM', 'EMAIL_TO', 'SSL_CERTS']:
            if self[key]:
                self.REDACTLIST.append(f"{self[key]}")

        for name, definitions in ARRAY_DEFS.items():
            key = definitions[0]  # Primary key for this array type
            array = self.get_array(name)
            if array:
                for _inx, config in enumerate(array):
                    if config[key]:
                        self.REDACTLIST.append(f"{config[key]}")
                    if 'API' in config and config['API']:
                        self.REDACTLIST.append(f"{config['API']}")

        LOGCONFIG.redact_list_updated(self.REDACTLIST)
        self.logger.debug(f"Redact list has {len(self.REDACTLIST)} {plural(len(self.REDACTLIST), 'entry')}")

    def get_all_types_list(self) -> list[str]:
        """ Return a list of extensions that includes all types of items """
        return self.get_list('MAG_TYPE') + self.get_list('AUDIOBOOK_TYPE') + \
            self.get_list('EBOOK_TYPE') + self.get_list('COMIC_TYPE')

    def use_any(self, rss=True) -> bool:
        """ Checks for TOR, NZB, Direct and IRC providers (optionally also RSS) """
        ok = bool(self.use_tor() or self.use_nzb() or self.use_direct() or self.use_irc())
        if not ok and rss:
            ok = bool(self.use_rss())
        return ok

    def total_active_providers(self) -> int:
        """ Count total number of valid providers of type TOR, NZB, RSS, Direct and IRC """
        return self.use_tor() + self.use_nzb() + self.use_rss() + self.use_direct() + self.use_irc()

    def count_in_use(self, provider: str, wishlist: bool | None = None) -> int:
        """ Returns # of providers named provider are in use """
        count = 0
        if provider in self.arrays:
            array = self.get_array(provider)
            if array:
                for inx in range(len(array)):
                    try:
                        host = array.primary_host(inx)
                        ok = array.is_in_use(inx) and not BLOCKHANDLER.is_blocked(host)
                        if wishlist is not None:
                            ok = ok and bool(wishlist_type(host)) == wishlist
                        if ok:
                            count += 1
                    except KeyError:
                        pass
        return count

    def use_rss(self) -> int:
        """ Returns number of RSS providers that are not wishlists, and are not blocked """
        return self.count_in_use('RSS', wishlist=False)

    def use_irc(self) -> int:
        """ Returns number of IRC active providers that are not blocked """
        return self.count_in_use('IRC')

    def use_wishlist(self) -> int:
        """Returns number of RSS providers that are wishlists and not blocked """
        return self.count_in_use('RSS', wishlist=True)

    def use_nzb(self) -> int:
        """ Returns number of nzb active providers that are not blocked
        (Includes Newznab and Torznab providers) """
        return self.count_in_use('NEWZNAB') + self.count_in_use('TORZNAB')

    def use_tor(self) -> int:
        """ Returns number of TOR providers that are not blocked """
        count = 0
        for provider in ['KAT', 'TPB', 'LIME', 'TDL', 'ABB']:
            if self.get_bool(provider) and not BLOCKHANDLER.is_blocked(provider):
                count += 1
        return count

    def use_direct(self) -> int:
        """ Returns number of enabled direct book providers """
        count = self.count_in_use('GEN')
        for item in ['BOK', 'ANNA', 'SLSK']:
            if self.get_bool(item) and not BLOCKHANDLER.is_blocked(item):
                count += 1
        return count

    def disp_name(self, provider: str) -> str:
        """
        Strange function. Returns the display name of a provider that
        matches the host name provided as parameter, if any.
        If not, returns the host name provided, shortened if too long.
        """
        provname = ''
        # Iterate over each type of provider
        for name, definitions in ARRAY_DEFS.items():
            key = definitions[0]  # Primary key for this array type
            array = self.providers(name)
            if array and not provname:  # If we have not yet got a result...
                for config in array:
                    if config[key].strip('/') == provider:
                        provname = config['DISPNAME']
                        break

        if not provname:
            provname = provider
        if len(provname) > 20:
            while len(provname) > 20 and '/' in provname:
                provname = provname.split('/', 1)[1]
            provname = provname.replace('/', ' ')
        return provname


# Global configuration holder
CONFIG = LLConfigHandler(defaults=BASE_DEFAULTS)


# Global config related methods that are not part of the config object

# noinspection PyBroadException
def are_equivalent(cfg1: LLConfigHandler, cfg2: LLConfigHandler) -> bool:
    """ Check that the two configs are logically equivalent by comparing all the keys and values """

    def are_configdicts_equivalent(checkcd1: ConfigDict, checkcd2: ConfigDict) -> bool:
        if not checkcd1 or not checkcd2:
            logger.warning(f"Arrays don't exist {not checkcd1}, {not checkcd2}")
            return False
        if len(checkcd1) != len(checkcd2):
            logger.warning(f"Array lengths differ: {len(checkcd1)} != {len(checkcd2)}")
            return False
        for key, item1 in checkcd1.items():
            if key in checkcd2.keys():
                item2 = checkcd2.get_item(key)
                if not item2:
                    logger.warning(f"Array values for [{key}]: {item1.value}: key does not exist")
                elif item2.value != item1.value:
                    logger.warning(f"Array values for [{key}]: {item1.value} != {item2.value}")
                    return False
            else:
                logger.warning(f"Array key [{key}] missing in array 2")
                return False
        return True

    if not cfg1 or not cfg2:  # Both need to exist
        return False

    logger = logging.getLogger(__name__)
    # Compare base configs
    if not are_configdicts_equivalent(cfg1, cfg2):
        logger.warning("Base configs differ")
        return False

    # Compare array configs
    if len(cfg1.arrays) != len(cfg2.arrays):
        logger.warning("Number of array configs differ")
        return False

    for name, array in cfg1.arrays.items():
        for inx in range(len(array)):
            cd1 = array[inx]
            try:
                cd2 = cfg2.arrays[name][inx]
            except Exception:
                logger.warning(f"Error retrieving array config {name}.{inx}")
                return False
            if not are_configdicts_equivalent(cd1, cd2):
                logger.warning(f"Array configs differ in {name}.{inx}")
                return False

    return True


def wishlist_type(host: str) -> str:
    """
    Return type of wishlist at host, or empty string if host is not a wishlist
    (Quite fragile, take care)
    """
    # GoodReads rss feeds
    if 'goodreads' in host and 'list_rss' in host:
        return 'goodreads'
    # GoodReads Listopia html pages
    if 'goodreads' in host and '/list/show/' in host:
        return 'listopia'
    # GoodReads most_read html pages (Listopia format)
    if 'goodreads' in host and '/book/' in host:
        return 'listopia'
    # Amazon charts html pages
    if 'amazon' in host and '/charts' in host:
        return 'amazon'
    # NYTimes best-sellers html pages
    if 'nytimes' in host and 'best-sellers' in host:
        return 'ny_times'
    # Publisherweekly bestseller in category
    if 'publishersweekly' in host and '/pw/' in host:
        return 'publishersweekly'
    # Publisherweekly bestseller in category
    if 'apps.npr.org' in host and '/best-books/' in host:
        return 'apps.npr.org'
    if 'penguinrandomhouse' in host:
        return 'penguinrandomhouse'
    if 'barnesandnoble' in host:
        return 'barnesandnoble'

    array = CONFIG.get_array('RSS')
    if array:
        for inx in range(len(array)):
            try:
                config = dict(array.configs[inx])
                if config['HOST'] == host and 'mam' in config['LABEL'].lower() and 'wish' in config['LABEL'].lower():
                    return 'myanonamouse'
            except KeyError:
                pass
    return ''
