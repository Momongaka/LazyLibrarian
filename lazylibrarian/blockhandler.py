#  This file is part of Lazylibrarian.
#
# Purpose:
#   Handle blocking behaviour, keep track of blocked providers, etc

import logging
import time

from lazylibrarian.configtypes import ConfigDict
from lazylibrarian.formatter import plural, pretty_approx_time, today


class BlockHandler:
    def __init__(self):
        self._nab_apicount_day: str = today()
        self._provider_list: dict[str, dict] = {}  # {name: {resume, reason}} = {}
        self._gb_calls: int = 0
        self._config: ConfigDict | None = None
        self._newznab: ConfigDict | None = None
        self._torznab: ConfigDict | None = None

    def set_config(self, config: ConfigDict, newznab: ConfigDict, torznab: ConfigDict):
        """ Set the configuration used for the BlockHandler. By providing them in this form,
        this module does not need to import config2, which would cause a circular dependency.
        Args:
        config: The base configuration
        newznab, torznab: The (array!) configs for these provider types, needed here to be able to
        handle the counting of API calls. We cannot import arrayconfig, hence just import as a ConfigDict.
        """
        self._config = config  # So we don't need to import config2
        self._newznab = newznab
        self._torznab = torznab

    def remove_provider_entry(self, name: str) -> None:
        self._provider_list.pop(name, None)

    def add_provider_entry(self, name: str, delay: int, reason: str) -> None:
        self._provider_list[name] = {"resume": int(time.time()) + delay, "reason": reason}

    def replace_provider_entry(self, name: str, delay: int, reason: str) -> None:
        # self.remove_provider_entry(name)
        self.add_provider_entry(name, delay, reason)

    def block_provider(self, who: str, why: str, delay: int | None = None) -> int:
        """ Block provider 'who' for reason 'why'. Returns number of seconds block will last """
        logger = logging.getLogger(__name__)
        if delay is None:
            delay = self._config.get_int('BLOCKLIST_TIMER') if self._config is not None else 3600

        if delay == 0:
            logger.debug(f'Not blocking {who},{why} as timer is zero')
            return 0

        if len(why) > 80:
            why = why[:80]

        timestr = pretty_approx_time(delay)
        logger.info(f"Blocking provider {who} for {timestr} because {why}")
        self.replace_provider_entry(who, delay, why)
        logger.debug(
            f"Provider Blocklist contains {len(self._provider_list)} {plural(len(self._provider_list), 'entry')}")
        return delay

    def number_blocked(self) -> int:
        """ Number of blocked providers """
        return len(self._provider_list)

    def clear_all(self) -> int:
        """ Clear all blocks, returning how many were on the list """
        num = self.number_blocked()
        self._provider_list.clear()
        return num

    def check_day(self, pretend_day: str | None = None) -> bool:
        """ Reset api counters if it's a new day since last check. Returns True if values are reset.
         The pretend_day argument is used for testing. """
        daystr = pretend_day if pretend_day else today()
        if self._nab_apicount_day != daystr:
            self._nab_apicount_day = daystr
            if self._newznab:
                for provider in self._newznab:
                    provider.set_int('APICOUNT', 0)
            if self._torznab:
                for provider in self._torznab:
                    provider.set_int('APICOUNT', 0)
            return True
        return False

    def is_blocked(self, name: str) -> bool:
        """ Returns true if the provider is blocked """
        self.check_day()

        timenow = int(time.time())
        if name in self._provider_list:
            entry = self._provider_list[name]
            if timenow < int(entry['resume']):
                return True
            self._provider_list.pop(name, None)
        return False

    def get_text_list_of_blocks(self) -> str:
        result = ''
        for key, line in self._provider_list.items():
            resume = int(line['resume']) - int(time.time())
            if resume > 0:
                time_str = pretty_approx_time(resume)
                new_entry = f"{key} blocked for {time_str}: {line['reason']}\n"
                result += new_entry

        if result == '':
            result = 'No blocked providers'
        return result


# Global blockhandler object

BLOCKHANDLER = BlockHandler()
