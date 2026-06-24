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
#   Handle basic, anonymous telemetry gathering for LL, to help prioritise
#   future development.

import datetime
import json
import logging
import os
import sys
import time
from collections import defaultdict
from http.client import responses

import requests

from lazylibrarian import database
from lazylibrarian.common import proxy_list
from lazylibrarian.config2 import CONFIG, LLConfigHandler
from lazylibrarian.formatter import thread_name
from lazylibrarian.processcontrol import get_info_on_caller


class LazyTelemetry:
    """Handles basic telemetry gathering for LazyLibrarian, helping
    developers prioritise future development"""

    _instance = None
    _data = {
        "server": {},
        "config": {
            "switches": '',
            "params": '',
            "BOOK_API": '',
            "NEWZNAB": 0,
            "TORZNAB": 0,
            "RSS": 0,
            "IRC": 0,
            "GEN": 0,
            "APPRISE": 0,
        },
        "usage": defaultdict(int),
    }

    # Singleton; no __init__ method
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # More initialization goes initialization here
            cls._boottime = datetime.datetime.now()
        return cls._instance

    def ensure_server_id(self, _config):
        """ Get unique, anonymous ID for self installation """
        server = self._data["server"]
        if "id" not in server:
            serverid = _config['SERVER_ID']
            if not serverid:
                import uuid
                serverid = uuid.uuid4().hex
                _config['SERVER_ID'] = serverid
            server["id"] = serverid

        return server["id"]

    def clear_id(self, _config):
        """ Clear the server ID. Only needed for testing purposes. """
        server = self._data["server"]
        del server['id']
        _config['SERVER_ID'] = ''

    def get_server_telemetry(self):
        return self._data["server"]

    def get_config_telemetry(self):
        return self._data["config"]

    def get_usage_telemetry(self):
        return self._data["usage"]

    def set_install_data(self, _config: LLConfigHandler, testing=False):
        """ Update telemetry with data bout the installation """
        self.ensure_server_id(_config)  # Make sure it has an ID
        server = self.get_server_telemetry()
        up = datetime.datetime.now() - self._boottime
        server["install_type"] = _config['INSTALL_TYPE']
        server["version"] = _config['CURRENT_VERSION']
        if testing:
            server["os"] = 'nt'
            server["uptime_seconds"] = 0
            server["python_ver"] = '3.11.0 (main, Oct 24 2022, 18:26:48) [MSC v.1933 64 bit (AMD64)]'
        else:
            server["os"] = os.name
            server["uptime_seconds"] = round(up.total_seconds())
            server["python_ver"] = str(sys.version)

    def set_config_data(self, _config: LLConfigHandler):
        cfg_telemetry = self.get_config_telemetry()
        cfg_telemetry['switches'] = ''
        cfg_telemetry["params"] = ''

        # Record whether particular on/off features are configured
        for key in [
            # General
            'USER_ACCOUNTS', 'EBOOK_TAB', 'COMIC_TAB', 'SERIES_TAB',
            'AUDIO_TAB', 'MAG_TAB', 'SHOW_GENRES', 'API_ENABLED',
            # Downloaders
            'NZB_DOWNLOADER_SABNZBD', 'NZB_DOWNLOADER_NZBGET', 'USE_SYNOLOGY',
            'NZB_DOWNLOADER_BLACKHOLE',
            'TOR_DOWNLOADER_DELUGE', 'TOR_DOWNLOADER_TRANSMISSION',
            'TOR_DOWNLOADER_RTORRENT', 'TOR_DOWNLOADER_UTORRENT',
            'TOR_DOWNLOADER_QBITTORRENT', 'TOR_DOWNLOADER_BLACKHOLE',
            # Providers
            'BOK', 'SLSK', 'ANNA',
            # Processing
            'CALIBRE_USE_SERVER', 'OPF_TAGS', 'MULTI_SOURCE',
            # Notifiers
            'USE_TWITTER', 'USE_BOXCAR', 'USE_PUSHBULLET', 'USE_PUSHOVER',
            'USE_ANDROIDPN', 'USE_TELEGRAM', 'USE_PROWL', 'USE_GROWL',
            'USE_SLACK', 'USE_CUSTOM', 'USE_EMAIL',
        ]:
            item = _config.get_item(key)
            if item and item.is_enabled():
                cfg_telemetry['switches'] += f"{key} "

        # Record the actual config of these features
        if _config['BOOK_API'] == 'GoodReads':
            cfg_telemetry["params"] += "PRIMARY_GR "
        elif _config['BOOK_API'] == 'GoogleBooks':
            cfg_telemetry["params"] += "PRIMARY_GB "
        elif _config['BOOK_API'] == 'HardCover':
            cfg_telemetry["params"] += "PRIMARY_HC "
        elif _config['BOOK_API'] == 'OpenLibrary':
            cfg_telemetry["params"] += "PRIMARY_OL "

        # Record whether these are configured differently from the default
        for key in ['GR_API', 'GB_API', 'OL_API', 'DNB_API', 'HC_API', 'GR_SYNC', 'HC_SYNC', 'LT_DEVKEY',
                    'IMP_PREFLANG', 'IMP_CALIBREDB', 'DOWNLOAD_DIR', 'ONE_FORMAT', 'API_KEY']:
            item = _config.get_item(key)
            if item and not item.is_default():
                cfg_telemetry["params"] += f"{key} "

        # Count how many of each provider are configured
        for provider in ["NEWZNAB", "TORZNAB", "RSS", "IRC", "GEN"]:
            cfg_telemetry[provider] = _config.count_in_use(provider)

        # Count how many Apprise notifications are configured
        cfg_telemetry["APPRISE"] = _config.count_in_use('APPRISE')

    def record_usage_data(self, counter: str | None = None):
        usg = self.get_usage_telemetry()
        if not counter:
            # Use the module/name of the caller
            caller_module, caller_function, _ = get_info_on_caller(depth=1)
            if caller_module == 'telemetry' and caller_function == 'record_usage_data':
                # We were called via the helper function, find the real caller:
                caller_module, caller_function, _ = get_info_on_caller(depth=2)
            counter = f'{caller_module}/{caller_function}'
        assert not any(c in counter for c in ' "='), "Counter must be plain text"
        usg[counter] += 1

    def clear_usage_data(self):
        usg = self.get_usage_telemetry()
        usg.clear()

    def get_json(self, send_config: bool, send_usage: bool, pretty=False):
        senddata = {'server': self._data['server']}
        if send_config:
            senddata['config'] = self._data['config']
        if send_usage:
            senddata['usage'] = self._data['usage']
        return json.dumps(senddata, indent=2 if pretty else None)

    def get_data_for_ui_preview(self, send_config: bool, send_usage: bool):
        self.set_install_data(CONFIG, testing=False)
        self.set_config_data(CONFIG)
        return self.get_json(send_config, send_usage, pretty=True)

    def construct_data_string(self, send_config: bool, send_usage: bool, send_server: bool = True):
        """ Returns a data string to send to telemetry server.
        If components = None, includes all parts. Otherwise, includes specified parts only """
        data = []
        if send_server:
            data.append(f"server={json.dumps(obj=self.get_server_telemetry(), separators=(',', ':'))}")
        if send_config:
            data.append(f"config={json.dumps(obj=self.get_config_telemetry(), separators=(',', ':'))}")
        if send_usage:
            data.append(f"usage={json.dumps(obj=self.get_usage_telemetry(), separators=(',', ':'))}")

        datastr = '&'.join(data)
        return datastr

    def get_data_url(self, server: str, send_config: bool, send_usage: bool):
        return f"{server}/send?{self.construct_data_string(send_config, send_usage)}"

    @staticmethod
    def _send_url(url: str):
        """ Sends url to the telemetry server, returns result """
        logger = logging.getLogger(__name__)
        logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)
        proxies = proxy_list()
        timeout = 5
        headers = {'User-Agent': 'LazyLibrarian'}
        if proxies:
            payload = {"timeout": timeout, "proxies": proxies}
        else:
            payload = {"timeout": timeout}
        try:
            logger.debug(f'GET {url}')
            r = requests.get(url, verify=False, params=payload, headers=headers)
        except requests.exceptions.Timeout as e:
            logger.error(f"_send_url: Timeout sending telemetry {url}")
            return f"Timeout {str(e)}", False
        except Exception as e:
            return f"Exception {type(e).__name__}: {str(e)}", False

        if str(r.status_code).startswith('2'):  # (200 OK etc)
            return r.text, True  # Success
        msg = responses.get(r.status_code, r.text)
        return f"Response status {r.status_code}: {msg}", False

    def submit_data(self, server: str, send_config: bool, send_usage: bool):
        """ Submits LL telemetry data
        Returns status message and true/false depending on whether it was successful"""

        logger = logging.getLogger(__name__)
        url = self.get_data_url(server, send_config, send_usage)
        logger.info(f"Sending telemetry data ({len(url)} bytes)")
        return self._send_url(url)

    def test_server(self, server: str) -> str:
        """ Try to connect to the configured server """
        try:
            serverid, ok = self._send_url(server)
            if ok and serverid:
                status, ok = self._send_url(f'{server}/status')
                # Use just the first line of both, in case there is an error
                id1 = serverid.split('\n')[0]
                status1 = status.split('\n')[0] if status else ''
                return f"Server ID: {id1}\n\nStatus:\n{status1}"
            return f"Error connecting to server: {serverid}"
        except requests.exceptions.RequestException:
            return "Error connecting to server"


# Global functions

TELEMETRY = LazyTelemetry()


def record_usage_data(counter: str | None = None):
    """ Convenience function for recording usage """
    TELEMETRY.record_usage_data(counter)


def telemetry_send() -> str:
    """ Routine called by scheduler, to regularly send telemetry data """
    threadname = thread_name()
    if "Thread" in threadname:
        thread_name("TELEMETRYSEND")
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        logger.info(f"Storing start time for {thread_name()}")
        db.upsert("jobs", {"Start": time.time()}, {"Name": thread_name()})
        TELEMETRY.set_install_data(CONFIG, testing=False)
        TELEMETRY.set_config_data(CONFIG)
        if CONFIG['TELEMETRY_SERVER'] == '':
            result, status = 'No telemetry server configured', False
        else:
            server = CONFIG['TELEMETRY_SERVER']
            send_config = CONFIG.get_bool('TELEMETRY_SEND_CONFIG')
            send_usage = CONFIG.get_bool('TELEMETRY_SEND_USAGE')
            result, status = TELEMETRY.submit_data(server, send_config, send_usage)
            if result:
                result = result.splitlines()[0]  # Return only the first line
        logger.debug(f'Telemetry data sending: {result}, {status}')
    finally:
        logger.info(f"Storing finish time for {thread_name()}")
        db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})
        db.close()
        thread_name(threadname)
    return result
