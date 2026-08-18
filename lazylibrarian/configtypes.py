#  This file is part of Lazylibrarian.
#
# Purpose:
#    Defines all of the different types of configs that can be
#    found in LazyLibrarian's config.ini (or eventually DB)
import logging
import os
import sys
from collections import Counter, OrderedDict
from collections.abc import Callable, ItemsView, KeysView, MutableMapping
from configparser import ConfigParser
from re import IGNORECASE, match
from re import compile as re_compile

import lazylibrarian
from lazylibrarian.configenums import (
    Access,
    OnChangeReason,
    TimeUnit,
)

# Type aliases to distinguish types of string
ValidIntTypes = int | bool
ValidStrTypes = str
ValidTypes = ValidStrTypes | ValidIntTypes

# Method or static method that can be called when a value changes
OnChangeCallback = Callable[[str, OnChangeReason], None]


class ConfigItem:
    """ Simple wrapper classes for config values of different types """
    section: str
    key: str
    default: ValidTypes
    value: ValidTypes
    accesses: Counter
    is_new: bool
    persist: bool
    onchange: OnChangeCallback | None

    def __init__(self, section: str, key: str, default: ValidTypes, is_new: bool = False, persist: bool = True,
                 onchange=None):
        self.section = section.upper()
        self.key = key.upper()
        self.default = default
        self.accesses = Counter()
        self.is_new = is_new
        self.persist = persist
        self.onchange = onchange
        if self.is_valid_value(default):
            self.value = default
        else:
            raise RuntimeError(f'Cannot initialize {section}.{key} as {default}')

    def get_full_name(self) -> str:
        if self.section:
            return f"{self.section.upper()}.{self.key}"
        return self.key

    def get_default(self) -> ValidTypes:
        return self.default

    def is_default(self) -> bool:
        return self.value == self.default

    def is_enabled(self) -> bool:
        return self.get_str() != ''

    def is_key(self, key: str) -> bool:
        return key.upper() == self.key

    def update_from_parser(self, parser: ConfigParser, tmpsection: str, name: str) -> bool:
        if tmpsection != self.section:
            logger = logging.getLogger('special.configread')
            logger.debug(f'Loading section {tmpsection} into section {self.section}')
        return self.set_str(parser.get(tmpsection, name))

    def get_str(self) -> str:
        self._on_read(True)
        return str(self.value)  # Everything can be a string

    def __str__(self) -> str:  # Make it the default when accessing the object
        return self.get_str()

    def get_save_str(self) -> str:  # The string used to save this setting
        return self.get_str()

    def set_str(self, value: str) -> bool:
        return False

    def set_from_ui(self, value: str) -> bool:
        if value != self.value:
            # Don't trigger a change if it's the same
            return self.set_str(value)
        return False  # Didn't change

    def get_list(self) -> list[str]:
        return [self.get_str().strip()]

    def get_force_lower(self):
        return False

    def get_int(self) -> int:
        self._on_read(False)
        return 0

    def __int__(self) -> int:  # Make it the default when accessing the object as int
        return self.get_int()

    def set_int(self, value: int) -> bool:
        return False

    def get_bool(self) -> bool:
        self._on_read(False)
        return False

    def set_bool(self, value: bool) -> bool:
        return False

    def get_float(self) -> float:
        self._on_read(False)
        return 0.0

    def set_float(self, value: float) -> float:
        return False

    def reset_to_default(self) -> bool:
        return self._on_set(self.default)

    def is_valid_value(self, value: ValidTypes) -> bool:
        return True

    def get_schedule_name(self) -> str:
        return ''

    def get_connection(self):
        return None

    def set_connection(self, value):
        pass  # Do nothing

    def do_persist(self) -> bool:
        """ Return True if the ConfigItem is one that needs to be saved """
        return self.persist

    def set_onchange(self, onchange: OnChangeCallback | None = None):
        self.onchange = onchange

    def _on_read(self, ok: bool) -> bool:
        if ok:
            self.accesses[Access.READ_OK] += 1
            logger = logging.getLogger('special.configread')
            logger.debug(f"Read config[{self.key}]={self.value}")
        else:
            self.accesses[Access.READ_ERR] += 1
            logger = logging.getLogger(__name__)
            logger.warning(f"Type error reading config[{self.key}] ({self.value})")
        return ok

    def _on_set(self, value: ValidTypes) -> bool:
        if self.is_valid_value(value):
            if self.is_new:
                self.accesses[Access.CREATE_OK] += 1
                self.is_new = False
            elif self.value != value:
                # Don't count a write if the value does not change
                self.accesses[Access.WRITE_OK] += 1
                logger = logging.getLogger('special.configwrite')
                logger.debug(f"Set config[{self.key}]={value}")
            self.value = value
            if self.onchange:
                self.onchange(self.get_str(), OnChangeReason.SETTING)
            return True
        self.accesses[Access.WRITE_ERR] += 1
        logger = logging.getLogger(__name__)
        logger.warning(f"Cannot set config[{self.key}] to {value}")
        return False

    def _on_type_mismatch(self, value, types) -> bool:
        self.accesses[Access.WRITE_ERR] += 1
        logger = logging.getLogger(__name__)
        logger.warning(f"Cannot set config[{self.key}] to {value}: incorrect type {types}")
        return False

    def get_accesses(self):
        """ Get the full list of 'accesses' """
        return self.accesses

    def reset_read_count(self):
        self.accesses[Access.READ_OK] = 0

    def get_read_count(self) -> int:
        """ Get number of successful reads since last reset """
        return self.accesses[Access.READ_OK]

    def get_write_count(self) -> int:
        """ Get number of successful writes since last reset """
        return self.accesses[Access.WRITE_OK]


class ConfigStr(ConfigItem):
    """ A config item that is a string """

    def __init__(self, section: str, key: str, default: str, force_lower: bool = False, is_new: bool = False,
                 persist: bool = True, onchange=None):
        self.force_lower = force_lower
        super().__init__(section, key, default, is_new=is_new, persist=persist, onchange=onchange)

    def set_str(self, value: str) -> bool:
        if self.force_lower:
            return self._on_set(value.lower())
        return self._on_set(value)

    def get_force_lower(self):
        return self.force_lower

    def set_int(self, value: int) -> bool:
        return self._on_type_mismatch(value, 'str/int')

    def set_bool(self, value: int) -> bool:
        return self._on_type_mismatch(value, 'str/bool')


class ConfigInt(ConfigItem):
    """ A config item that is an int """

    def __init__(self, section: str, key: str, default: int, is_new: bool = False, persist: bool = True,
                 onchange=None):
        super().__init__(section, key, default, is_new, persist, onchange)

    def get_int(self) -> int:
        if self._on_read(type(self.value) in [int, bool]):
            return int(self.value)
        return 0

    def set_int(self, value: int) -> bool:
        if not isinstance(value, int):
            try:
                value = int(value)
            except (ValueError, TypeError):
                return self._on_type_mismatch(value, f"int/{type(value)}")
        return self._on_set(value)

    def set_str(self, value: str) -> bool:
        return self._on_type_mismatch(value, 'int/str')

    def set_from_ui(self, value: str) -> bool:
        try:
            ivalue = int(value)
        except (ValueError, TypeError):
            ivalue = self.get_default()

        if ivalue != self.value:
            # Don't trigger a change if it's the same
            return self.set_int(ivalue)
        return False

    def set_bool(self, value: bool) -> bool:
        return self._on_type_mismatch(value, 'int/bool')

    def update_from_parser(self, parser: ConfigParser, tmpsection: str, name: str) -> bool:
        if tmpsection != self.section:
            logger = logging.getLogger('special.configread')
            logger.debug(f'Loading int {name} from section {tmpsection} into section {self.section}')
        # noinspection PyBroadException
        try:
            value = parser.getint(tmpsection, name, fallback=0)
        except Exception:
            value = 0
        return self.set_int(value)


class ConfigFloat(ConfigItem):
    """ A config item that is a float """

    def __init__(self, section: str, key: str, default: float, is_new: bool = False, persist: bool = True,
                 onchange=None):
        super().__init__(section, key, default, is_new, persist, onchange)

    def get_float(self) -> float:
        if self._on_read(type(self.value) in [int, float, bool]):
            return float(self.value)
        return 0

    def set_int(self, value: int) -> bool:
        if type(value) not in [float, int]:
            try:
                value = float(value)
            except (ValueError, TypeError):
                return self._on_type_mismatch(value, f"float/{type(value)}")
        return self._on_set(float(value))

    def set_float(self, value: float) -> bool:
        if not isinstance(value, float):
            try:
                value = float(value)
            except (ValueError, TypeError):
                return self._on_type_mismatch(value, f"float/{type(value)}")
        return self._on_set(value)

    def set_str(self, value: str) -> bool:
        return self._on_type_mismatch(value, 'float/str')

    def set_from_ui(self, value: str) -> bool:
        try:
            ivalue = float(value)
        except (ValueError, TypeError):
            ivalue = self.get_default()

        if ivalue != self.value:
            # Don't trigger a change if it's the same
            return self.set_float(ivalue)
        return False

    def set_bool(self, value: bool) -> bool:
        return self._on_type_mismatch(value, 'float/bool')

    def update_from_parser(self, parser: ConfigParser, tmpsection: str, name: str) -> bool:
        if tmpsection != self.section:
            logger = logging.getLogger('special.configread')
            logger.debug(f'Loading float {name} from section {tmpsection} into section {self.section}')
        # noinspection PyBroadException
        try:
            value = parser.getfloat(tmpsection, name, fallback=0)
        except Exception:
            value = 0
        return self.set_float(value)


class ConfigRangedInt(ConfigInt):
    """ An int config item that must be in a particular range """

    def __init__(self, section: str, key: str, default: int,
                 range_min: int, range_max: int, is_new: bool = False, persist: bool = True, onchange=None):
        self.range_min = range_min
        self.range_max = range_max
        super().__init__(section, key, default, is_new, persist, onchange)

    def is_valid_value(self, value: ValidTypes) -> bool:
        return self.range_min <= int(value) <= self.range_max


class ConfigScheduler(ConfigRangedInt):
    """ An int config that is used to hold a scheduling interval and associated info to run it """

    def __init__(self, section: str, key: str, schedule_name: str, default: int, unit: TimeUnit,
                 run_name: str, method_name: str, friendly_name: str,
                 needs_provider: bool, is_new: bool = False, persist: bool = True):
        if not schedule_name:
            raise RuntimeError(f'Schedule name for {section}.{key} cannot be empty')

        self.schedule_name = schedule_name  # The name of the schedule, like 'search_book'
        self.run_name = run_name  # The name of the run, like 'SEARCHALLBOOKS'
        self.method_name = method_name  # The method to call
        self.friendly_name = friendly_name  # The name to show in the UI
        self.needs_provider = needs_provider  # Whether it needs a provider to work
        self.unit = unit  # Is the value in Minutes or Hours?
        super().__init__(section, key, default, range_min=0, range_max=100000, is_new=is_new, persist=persist)

    def get_method(self):
        """ Return the method to call for this scheduler """
        module, function = self.method_name.rsplit('.', 1)
        # noinspection PyBroadException
        try:
            return getattr(sys.modules[module], function)
        except Exception:
            return None

    def is_valid_value(self, value: ValidTypes) -> bool:
        ok = super().is_valid_value(value)
        # Interval must be positive
        ok = ok and int(value) >= 0
        return ok

    def get_hour_min_interval(self) -> tuple[int, int]:
        """ Return (hours, minutes) tuple for the schedule """
        value = self.get_int()
        hours, minutes = 0, 0
        if self.unit == TimeUnit.DAY:
            hours = value * 24
        elif self.unit == TimeUnit.HOUR:
            hours = value
        else:
            if value <= 600:
                minutes = value  # Just minutes, if < 10 hours
            else:
                hours = int(value / 60)  # Just whole hours, if longer

        # No interval < 5 minutes
        if 60 * hours + minutes < 5:
            hours, minutes = 0, 5

        return hours, minutes

    def get_schedule_name(self) -> str:
        return self.schedule_name


class ConfigPerm(ConfigStr):
    """ Represents UNIX file permissions. Emitted as an Octal string """

    def __init__(self, section: str, key: str, default: str, is_new: bool = False, persist: bool = True):
        super().__init__(section, key, default, is_new=is_new, persist=persist)

    def set_int(self, value: int) -> bool:
        # It's a string, but can be set with an int value
        return self.set_str(oct(value))

    def get_int(self) -> int:
        self._on_read(True)
        return int(str(self.value), 8)

    def set_from_ui(self, value: str) -> bool:
        # UI providers a 3-digit octal string
        return super().set_from_ui(f'0o{value}')

    def is_valid_value(self, value: ValidTypes) -> bool:
        try:
            if isinstance(value, str):
                octvalue = oct(int(str(value), 8))  # Must now be a valid Oct string
                if octvalue != value:
                    return False
            else:
                return False

            if octvalue[:2] != '0o':
                return False

            intval = int(octvalue[2:], 8)
            return 0 <= intval <= 0o777
        except ValueError:
            return False


class ConfigBool(ConfigInt):
    """ A config item that is a bool """

    def __init__(self, section: str, key: str, default: bool | int, is_new: bool = False, persist: bool = True,
                 onchange=None):
        super().__init__(section, key, default, is_new, persist, onchange)

    def get_bool(self) -> bool:
        if self._on_read(type(self.value) in [bool, int]):  # We're ok with ints
            return bool(self.value)
        return False

    def set_bool(self, value: bool | int) -> bool:
        return self._on_set(value)

    def set_int(self, value: int) -> bool:
        return self.set_bool(bool(value))

    def set_str(self, value: str) -> bool:
        return self._on_type_mismatch(value, 'bool/str')

    def set_from_ui(self, value: bool) -> bool:
        if bool(value in ['1', 1, True, 'True', 'true']) != self.value:
            # Don't trigger a change if it's the same
            return self.set_bool(bool(value))
        return False

    def get_str(self) -> str:
        """ For a Bool, return '' for False, 'True' for True """
        self._on_read(True)
        if self.value:
            return '1'
        return ''  # Evaluates as False in if statements

    def get_save_str(self) -> str:
        self._on_read(True)
        return str(bool(self.value))

    def is_enabled(self) -> bool:
        return self.get_bool()

    def update_from_parser(self, parser: ConfigParser, tmpsection: str, name: str) -> bool:
        if tmpsection != self.section:
            logger = logging.getLogger(__name__)
            logger.debug(f'Loading bool {name} from section {tmpsection} into section {self.section}')
        return self.set_bool(parser.getboolean(tmpsection, name, fallback=False))


class ConfigEmail(ConfigStr):
    """ A config item that is a string that must be a valid email address or comma separated list of valid addresses"""

    def __init__(self, section: str, key: str, default: str, is_new: bool = False, persist: bool = True):
        super().__init__(section, key, default, force_lower=False, is_new=is_new, persist=persist)
        # kindle email addresses are case sensitive

    def get_email(self) -> str:
        return self.get_str()

    def is_valid_value(self, value: ValidTypes) -> bool:
        value = str(value)
        if value == '':
            return True
        # Regular expression pattern to match email addresses
        pattern = r"^[\w.+-]+@[\w.-]+\.[\w]+$"  # Allow + in emails

        if ',' in value:
            values = value.split(',')
        else:
            values = [value]

        for value in values:
            value = value.strip()
            # Check if email matches pattern
            if not match(pattern, value):
                return False
            # Check if local part of email is not too long
            if len(value.split("@")[0]) > 64:
                return False
            # Check if domain name of email is not too long
            if len(value.split("@")[1]) > 255:
                return False
        return True


class ConfigCSV(ConfigStr):
    """ A config item that is a string that must be a valid CSV """

    def get_csv(self) -> str:
        return self.get_str()

    def get_list(self) -> list[str]:
        """ Return a list like ['abc', 'def'] from 'abc, def'. Leading and trailing spaces are stripped. """
        return [item.strip() for item in self.get_csv().split(',')]

    def is_valid_value(self, value: ValidTypes) -> bool:
        if isinstance(value, str):
            if value == '':
                return True
            # Check if the string only contains alphanumeric characters, and select symbols
            if all(c.isalnum() or c in ', _/!-+#.' for c in value):
                # Split the string by the comma and check if the resulting parts are not empty
                parts = value.split(',')
                return all(part.strip() for part in parts)
        return False


class ConfigDownloadTypes(ConfigCSV):
    """ A config item that holds a CSV of download types (letters A, C, E and M) """

    def set_str(self, value: str) -> bool:
        return super().set_str(value.upper())

    def is_valid_value(self, value: ValidTypes) -> bool:
        if super().is_valid_value(value):
            parts = str(value).upper().split(',')
            ok = all(len(part) == 1 for part in parts) and all(part in 'ACEM' for part in parts)
            return ok
        return False


class ConfigURL(ConfigStr):
    """ A config item that is a string that must be a valid URL """

    def get_url(self) -> str:
        return self.get_str()

    def set_str(self, value: str) -> bool:
        value = value.rstrip('/')
        return super().set_str(value)

    def is_valid_value(self, value: ValidTypes) -> bool:
        if isinstance(value, str):
            if value == '':
                return True
            regex = re_compile(
                r'^(?:http|ftp)s?://'  # http:// or https://
                r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'  # domain...
                r'localhost|'  # localhost...
                r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'  # ...or ip
                r'(?::\d+)?'  # optional port
                r'(?:/?|[/?]\S+)$', IGNORECASE)

            # check if the URL matches the regular expression
            return regex.match(value) is not None
        return False


class ConfigFolder(ConfigStr):
    """ A config item that holds a folder name or template. It may hold path separators
    that are OS-specific, but will always be saved to file using unix-style (/) separators
    and will always be accessed at run-time with the OS-specific separator
    """
    as_loaded = ''

    def __init__(self, section: str, key: str, default: str, force_lower: bool = False, is_new: bool = False,
                 persist: bool = True):
        super().__init__(section, key, self.fix_separator(default), force_lower, is_new, persist)

    def update_from_parser(self, parser: ConfigParser, tmpsection: str, name: str) -> bool:
        """ For Folders, save the config as-is to be able to preserve relative paths """
        if tmpsection != self.section:
            logger = logging.getLogger(__name__)
            logger.debug(f'Loading folder {name} from section {tmpsection} into section {self.section}')
        self.as_loaded = parser.get(tmpsection, name)
        return self.set_str(self.as_loaded)

    def set_str(self, value: str) -> bool:
        return super().set_str(self.fix_separator(value))

    def get_save_str(self) -> str:
        if self.as_loaded and self.as_loaded[0] == '.':
            return self.as_loaded  # Relative path
        tosave = self.get_str()
        if '\\' in tosave:  # Never save \\ in the ini file
            tosave = tosave.replace('\\', '/')
        return tosave

    @staticmethod
    def fix_separator(value: str) -> str:
        if os.name == 'nt' and '/' in value:
            return value.replace('/', '\\')
        if os.name != 'nt' and '\\' in value:
            return value.replace('\\', '/')
        return value


class ConfigConnection(ConfigItem):
    """ A virtal config item that is used to hold a connection. Not persisted. """

    def __init__(self, section: str, key: str):
        self._connection = None
        super().__init__(section, key, 0, is_new=False, persist=False)

    # Introduce connection as a property for easy access
    def get_connection(self):
        return self._connection

    def set_connection(self, value) -> bool:
        self._connection = value
        return True


# This is to have section names be case insensitive.
# Built from https://stackoverflow.com/questions/49755480/case-insensitive-sections-in-configparser
class CaseInsensitiveDict(MutableMapping):
    """ Ordered case insensitive mutable mapping class. """

    def __init__(self, *args, **kwargs):
        self._d = OrderedDict(*args, **kwargs)
        self._convert_keys()

    def _convert_keys(self):
        for k in list(self._d.keys()):
            v = self._d.pop(k)
            self._d.__setitem__(k, v)

    def __len__(self):
        return len(self._d)

    def __iter__(self):
        return iter(self._d)

    def __setitem__(self, k, v):
        self._d[k.upper()] = v

    def __getitem__(self, k):
        return self._d[k.upper()]

    def __delitem__(self, k):
        del self._d[k.upper()]

    def copy(self):
        return CaseInsensitiveDict(self._d.copy())


class ConfigDict:
    """ A class for managing access to a dict of configs in a convenient way """

    def __init__(self, single_section: str = ''):
        self.config: dict[str, ConfigItem] = CaseInsensitiveDict()  # type: ignore
        self.errors: dict[str, Counter] = {}
        self.single_section = single_section.upper()  # Set if the dict represents an array entry

    def clear(self):
        self.config.clear()
        self.errors.clear()

    """ As an object that acts like a Dict """

    def __iter__(self):
        return iter(self.config)

    def items(self) -> ItemsView[str, ConfigItem]:
        return self.config.items()

    def keys(self) -> KeysView[str]:
        return self.config.keys()

    def __len__(self):
        return len(self.config)

    """ As generic object """

    def get_item(self, key: str) -> ConfigItem | None:
        if key.upper() in self.config:
            return self.config[key.upper()]
        self._handle_access_error(key, Access.READ_ERR)
        return None

    def set_item(self, key: str, item: ConfigItem) -> ConfigItem:
        self.config[key.upper()] = item
        return item

    def set_from_ui(self, key: str, value) -> bool:
        """ Set the value from UI, where value may need to be coerced. Returns True if key existes """
        if key.upper() in self.config:
            item = self.config[key.upper()]
            return item.set_from_ui(value)
        return False

    """ Plain strings """

    def get_str(self, key: str) -> str:
        if key.upper() in self.config:
            return self.config[key.upper()].get_str()
        self._handle_access_error(key, Access.READ_ERR)
        return ''

    def __getitem__(self, __name: str) -> str:
        """ Make it possible to use CONFIG['name'] to access a string config directly """
        if __name:
            return self.get_str(__name.upper())
        return ''

    def __setitem__(self, __name: str, value: str):
        self.set_str(__name.upper(), value)

    def set_str(self, key: str, value: str):
        if key.upper() in self.config:
            self.config[key.upper()].set_str(value)
        else:
            self.create_str_key(ConfigStr, key, value)

    """ Integers """

    def get_int(self, key: str) -> int:
        if key.upper() in self.config:
            return self.config[key.upper()].get_int()
        self._handle_access_error(key, Access.READ_ERR)
        return 0

    def set_int(self, key: str, value: int):
        if key.upper() in self.config:
            self.config[key.upper()].set_int(value)
        else:
            self.config[key.upper()] = ConfigInt('', key, 0, is_new=True, persist=False)
            self.set_int(key, value)

    """ Booleans (0/1, False/True) """

    def get_bool(self, key: str) -> bool:
        if key.upper() in self.config:
            return self.config[key.upper()].get_bool()
        self._handle_access_error(key, Access.READ_ERR)
        return False

    def set_bool(self, key: str, value: bool):
        if key.upper() in self.config:
            self.config[key.upper()].set_bool(value)
        else:
            self.config[key.upper()] = ConfigBool('', key, False, is_new=True, persist=False)
            self.set_bool(key, value)

    """ Floats """

    def get_float(self, key: str) -> float:
        if key.upper() in self.config:
            return self.config[key.upper()].get_float()
        self._handle_access_error(key, Access.READ_ERR)
        return 0

    def set_float(self, key: str, value: float):
        if key.upper() in self.config:
            self.config[key.upper()].set_float(value)
        else:
            self.config[key.upper()] = ConfigFloat('', key, 0, is_new=True, persist=False)
            self.set_float(key, value)

    """ Email addresses """

    def get_email(self, key: str) -> str:
        return self.get_str(key)

    def set_email(self, key: str, value: str):
        if key.upper() in self.config:
            self.config[key.upper()].set_str(value)
        else:
            self.create_str_key(ConfigEmail, key, value)

    """ CSV strings """

    def get_csv(self, key: str) -> str:
        return self.get_str(key)

    def get_list(self, key: str) -> list[str]:
        """ Return the items as a list """
        return self.config[key.upper()].get_list()

    def set_csv(self, key: str, value: str):
        if key.upper() in self.config:
            self.config[key.upper()].set_str(value)
        else:
            self.create_str_key(ConfigCSV, key, value)

    """ URL strings """

    def get_url(self, key: str) -> str:
        return self.get_str(key)

    def set_url(self, key: str, value: str):
        if key.upper() in self.config:
            self.config[key.upper()].set_str(value)
        else:
            self.create_str_key(ConfigURL, key, value)

    """ Connection objects """

    def get_connection(self, key: str = ''):
        """ Return the named connection, or the first valid one if not specified """
        conn = None
        if key:
            if key.upper() in self.config:
                conn = self.config[key.upper()].get_connection()
        else:
            for _, item in self.config.items():
                conn = item.get_connection()
                if conn:
                    break
        return conn

    def set_connection(self, value=None, key: str = ''):
        """ Set the named connection, or any connections if key=='' """
        if key:
            if key.upper() in self.config:
                self.config[key.upper()].set_connection(value)
        else:
            for _, item in self.config.items():
                item.set_connection(value)

    def create_str_key(self, aclass: type[ConfigItem], key: str, value: ValidStrTypes):
        """ Function for creating new config items on the fly. Should be rare in LL. """
        new_entry = aclass('', key, '', is_new=True, persist=False)
        if new_entry.is_valid_value(value):
            self.config[key.upper()] = new_entry
            self.config[key.upper()].set_str(value)
        else:
            self._handle_access_error(key, Access.FORMAT_ERR)

    def get_configscheduler(self, schedule_name: str) -> ConfigScheduler | None:
        """ Look for a config with the specified target """
        for _key, value in self.config.items():
            if isinstance(value, ConfigScheduler) and value.schedule_name.lower() == schedule_name.lower():
                return value
        return None

    def _handle_access_error(self, key: str, status: Access):
        """ Handle accesses to invalid keys """
        from lazylibrarian.telemetry import TELEMETRY
        TELEMETRY.record_usage_data(f'Config/AccessError/{status.name}')
        key = key.upper()
        if self.single_section:
            name = f"{self.single_section}.{key}"
        else:
            name = key
        if name not in self.errors:
            self.errors[name] = Counter()
        self.errors[name][status] += 1
        logger = logging.getLogger(__name__)
        logger.error(f"Config[{name}]: {status.value}")

    def get_error_counters(self) -> dict[str, Counter]:
        """ Get a list of all access errors """
        return self.errors

    def clear_error_counters(self):
        self.errors.clear()

    def is_valid_booktype(self, filename: str, booktype: str) -> bool:
        """ Check if filename extension is one that is of the right type """
        if booktype.startswith('mag'):  # default is book
            booktype_list = self.get_list('MAG_TYPE')
        elif booktype.startswith('audio'):
            booktype_list = self.get_list('AUDIOBOOK_TYPE')
        elif booktype == 'comic':
            booktype_list = self.get_list('COMIC_TYPE')
        else:
            booktype_list = self.get_list('EBOOK_TYPE')
        extn = lazylibrarian.filesystem.splitext(filename)[1].lstrip('.')
        return extn and booktype_list and extn.lower() in booktype_list


class ErrorListIterator:
    """ Helper to iterate over all Error Lists in a list of ConfigDicts """

    def __init__(self, config_dicts: list[ConfigDict]):
        self.errordicts = []
        for cd in config_dicts:
            self.errordicts.append(cd.errors)
        self.index = 0

    def __len__(self):
        return len(self.errordicts)

    def __iter__(self):
        return self

    def __next__(self) -> dict[str, Counter]:
        if self.index >= len(self.errordicts):
            raise StopIteration
        item = self.errordicts[self.index]
        self.index += 1
        return item


class ConfigDictListIterator:
    """ Helper to iterate over all ConfigItems in a list of ConfigDicts """

    def __init__(self, config_dicts: list[ConfigDict]):
        self.config_dicts = config_dicts
        self.dict_index = 0
        self.item_index = 0

    def __len__(self):
        res = 0
        for cd in self.config_dicts:
            res += len(cd)
        return res

    def __iter__(self):
        return self

    def __next__(self) -> tuple[str, ConfigItem]:
        if self.dict_index >= len(self.config_dicts):
            raise StopIteration
        config_dict = self.config_dicts[self.dict_index]
        if self.item_index >= len(config_dict):
            self.dict_index += 1
            self.item_index = 0
            return self.__next__()
        name, item = list(config_dict.items())[self.item_index]
        self.item_index += 1
        return name, item
