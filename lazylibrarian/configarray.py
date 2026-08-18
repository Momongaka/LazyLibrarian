#  This file will be part of Lazylibrarian.
#
# Purpose:
#   Handle array-configs, such as providers and notifiers

from collections import OrderedDict

from lazylibrarian.configdefs import DefaultArrayDef, configitem_from_default
from lazylibrarian.configtypes import ConfigDict, ConfigItem


class ArrayConfig:
    """ Handle an array-config, such as for a list of notifiers """
    _name: str  # e.g. 'APPRISE'
    _secstr: str  # e.g. 'APPRISE_%i'
    _primary: str  # e.g. 'URL'
    configs: dict[int, ConfigDict]
    _defaults: list[ConfigItem]

    def __init__(self, arrayname: str, defaults: DefaultArrayDef):
        self._name = arrayname
        self._primary = defaults[0]  # Name of the primary key for this item
        self._secstr = defaults[1]  # Name of the section string template
        self._defaults = defaults[2]  # All the entries
        self.configs = OrderedDict()

    def setupitem_at(self, index: int):
        for config_item in self._defaults:
            key = config_item.key.upper()
            if index not in self.configs:
                self.configs[index] = ConfigDict(self.get_section_str(index))
            item = self.configs[index].set_item(key, configitem_from_default(config_item))
            item.section = self.get_section_str(index)
            if key == 'NAME':  # Override name with section name
                item.value = item.section

    def has_index(self, index: int) -> bool:
        return index in self.configs.keys()

    # Allow ArrayConfig to be accessed as an indexed list
    def __len__(self) -> int:
        return len(self.configs)

    def __getitem__(self, index: int) -> ConfigDict:
        return self.configs[index]

    def primary_host(self, index: int) -> str:
        if index > len(self):
            return ''
        config = self[index]
        if self._primary in config:
            return self.configs[index][self._primary]
        return ''

    # Allow ArrayConfig to be iterated over, ignoring the index
    def __iter__(self):
        return self.configs.values().__iter__()

    def is_in_use(self, index: int) -> bool:
        """ Check if the index'th item is in use, or spare """
        if index > len(self):
            return False
        try:
            res = self.primary_host(index) != ''
            return res
        except KeyError:
            return False

    def get_section_str(self, index: int) -> str:
        return self._secstr % index

    def ensure_empty_end_item(self):
        """ Ensure there is an empty/unused item at the end of the list """
        if len(self.configs) == 0 or self.is_in_use(len(self.configs) - 1):
            self.setupitem_at(len(self))

    def cleanup_for_save(self):
        """ Clean out empty items and renumber items from 0 """
        keepcount = 0
        for index in range(len(self)):
            if not self.is_in_use(index):
                del self.configs[index]
            else:
                keepcount += 1

        # Because we use an OrderedDict, items will be in numeric order
        # Can't modify ordered dict while iterating, so make a copy
        temp_configs = OrderedDict()
        for renum, number in enumerate(self.configs):
            if number > renum:
                config = self.configs[number]
                # Update the section key in each item
                for _name, item in config.items():
                    item.section = self.get_section_str(renum)
                # Update the key of the dict entry
                temp_configs[renum] = config
            else:
                temp_configs[number] = self.configs[number]
        self.configs = temp_configs
