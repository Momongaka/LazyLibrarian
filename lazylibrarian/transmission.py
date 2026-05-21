#  This file is part of LazyLibrarian.
#  LazyLibrarian is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  LazyLibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  You should have received a copy of the GNU General Public License
#  along with LazyLibrarian.  If not, see <http://www.gnu.org/licenses/>.

import logging
import time
from urllib.parse import urlparse, urlunparse

import requests

from lazylibrarian.common import proxy_list
from lazylibrarian.config2 import CONFIG

# This is just a simple script to send torrents to transmission. The
# intention is to turn this into a class where we can check the state
# of the download, set the download dir, etc.
#
session_id = None
host_url = None
rpc_version = 0
tr_version = 0


def move_torrent(torrentid, directory):
    logger = logging.getLogger(__name__)
    method = 'torrent-set-location'
    arguments = {'ids': [torrentid], 'location': directory, 'move': True}
    logger.debug(f'move_torrent args({arguments})')
    _, _ = torrent_action(method, arguments)
    return True


def add_torrent(link, directory=None, metainfo=None, provider_options=None):
    logger = logging.getLogger(__name__)
    method = 'torrent-add'
    if metainfo:
        arguments = {'metainfo': metainfo}
    else:
        arguments = {'filename': link}
    if not directory:
        directory = CONFIG['TRANSMISSION_DIR']
    if directory:
        arguments['download-dir'] = directory
    arguments['paused'] = CONFIG.get_bool('TORRENT_PAUSED')

    logger.debug(f'add_torrent args({arguments})')
    response, res = torrent_action(method, arguments)  # type: dict

    if not response:
        return False, res

    if response['result'] == 'success':
        if 'torrent-added' in response['arguments']:
            retid = response['arguments']['torrent-added']['id']
        elif 'torrent-duplicate' in response['arguments']:
            retid = response['arguments']['torrent-duplicate']['id']
        else:
            retid = False
        if retid:
            logger.debug("Torrent sent to Transmission successfully")

            if "seed_ratio" in provider_options:
                set_seed_ratio(retid, provider_options["seed_ratio"])

            return retid, ''

    res = f"Transmission returned {response['result']}"
    logger.debug(res)
    return False, res


def get_torrent_name(torrentid):  # uses hashid
    logger = logging.getLogger(__name__)
    method = 'torrent-get'
    arguments = {'ids': [torrentid], 'fields': ['name', 'percentDone', 'labels']}
    retries = 3
    while retries:
        response, _ = torrent_action(method, arguments)  # type: dict
        if response and len(response['arguments']['torrents']):
            percentdone = response['arguments']['torrents'][0]['percentDone']
            if percentdone:
                return response['arguments']['torrents'][0]['name']
        else:
            logger.debug('get_torrent_name: No response from transmission')
            return ''

        retries -= 1
        if retries:
            time.sleep(5)

    return ''


def get_torrent_folder(torrentid):  # uses hashid
    logger = logging.getLogger(__name__)
    method = 'torrent-get'
    arguments = {'ids': [torrentid], 'fields': ['downloadDir', 'percentDone']}
    retries = 3
    while retries:
        response, _ = torrent_action(method, arguments)  # type: dict
        if response and len(response['arguments']['torrents']):
            percentdone = response['arguments']['torrents'][0]['percentDone']
            if percentdone:
                return response['arguments']['torrents'][0]['downloadDir']
        else:
            logger.debug('get_torrent_folder: No response from transmission')
            return ''

        retries -= 1
        if retries:
            time.sleep(5)

    return ''


def get_torrent_folder_by_id(torrentid):  # uses transmission id
    logger = logging.getLogger(__name__)
    method = 'torrent-get'
    arguments = {'fields': ['name', 'percentDone', 'id']}
    retries = 3
    while retries:
        response, _ = torrent_action(method, arguments)  # type: dict
        if response and len(response['arguments']['torrents']):
            tor = 0
            while tor < len(response['arguments']['torrents']):
                percentdone = response['arguments']['torrents'][tor]['percentDone']
                if percentdone:
                    torid = response['arguments']['torrents'][tor]['id']
                    if str(torid) == str(torrentid):
                        return response['arguments']['torrents'][tor]['name']
                tor += 1
        else:
            logger.debug('get_torrent_folder: No response from transmission')
            return ''

        retries -= 1
        if retries:
            time.sleep(5)

    return ''


def get_torrent_files(torrentid):  # uses hashid
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger('special.dlcomms')
    method = 'torrent-get'
    arguments = {'ids': [torrentid], 'fields': ['id', 'files']}
    retries = 3
    while retries:
        response, _ = torrent_action(method, arguments)  # type: dict
        if response:
            if len(response['arguments']['torrents'][0]['files']):
                dlcommslogger.debug(f"get_torrent_files: {str(response['arguments']['torrents'][0]['files'])}")
                return response['arguments']['torrents'][0]['files']
        else:
            logger.debug('get_torrent_files: No response from transmission')
            return []

        retries -= 1
        if retries:
            time.sleep(5)

    return []


def get_torrent_progress(torrentid):  # uses hashid
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger('special.dlcomms')
    method = 'torrent-get'
    arguments = {'ids': [torrentid], 'fields': ['id', 'percentDone', 'errorString', 'status']}
    retries = 3
    while retries:
        response, _ = torrent_action(method, arguments)  # type: dict
        if response:
            try:
                if len(response['arguments']['torrents'][0]):
                    err = response['arguments']['torrents'][0]['errorString']
                    res = response['arguments']['torrents'][0]['percentDone']
                    fin = (response['arguments']['torrents'][0]['status'] == 0)  # TR_STATUS_STOPPED == 0
                    dlcommslogger.debug(f"get_torrent_progress: {err},{res},{fin}")
                    try:
                        res = int(float(res) * 100)
                        return res, err, fin
                    except ValueError:
                        continue
            except IndexError:
                msg = f'{torrentid} not found at transmission'
                logger.debug(msg)
                return -1, msg, False
        else:
            msg = 'No response from transmission'
            logger.debug(msg)
            return -2, msg, False

        retries -= 1
        if retries:
            time.sleep(1)

    msg = f'{torrentid} not found at transmission'
    logger.debug(msg)
    return -1, msg, False


def set_seed_ratio(torrentid, ratio):
    method = 'torrent-set'
    if ratio != 0:
        arguments = {'seedRatioLimit': ratio, 'seedRatioMode': 1, 'ids': [torrentid]}
    else:
        arguments = {'seedRatioMode': 2, 'ids': [torrentid]}

    response, _ = torrent_action(method, arguments)  # type: dict
    return bool(response)


def set_label(torrentid, label):
    method = 'torrent-set'
    arguments = {'labels': [label], 'ids': [torrentid]}
    response, _ = torrent_action(method, arguments)  # type: dict
    return bool(response)

# Pre RPC v14 status codes
#   {
#        1: 'check pending',
#        2: 'checking',
#        4: 'downloading',
#        8: 'seeding',
#        16: 'stopped',
#    }
#    RPC v14 status codes
#    {
#        0: 'stopped',
#        1: 'check pending',
#        2: 'checking',
#        3: 'download pending',
#        4: 'downloading',
#        5: 'seed pending',
#        6: 'seeding',
#        7: 'isolated', # no connection to peers
#    }


def remove_torrent(torrentid, remove_data=False):
    global rpc_version

    logger = logging.getLogger(__name__)
    method = 'torrent-get'
    arguments = {'ids': [torrentid], 'fields': ['isFinished', 'name', 'status']}

    response, _ = torrent_action(method, arguments)  # type: dict
    if not response:
        return False

    try:
        finished = response['arguments']['torrents'][0]['isFinished']
        name = response['arguments']['torrents'][0]['name']
        status = response['arguments']['torrents'][0]['status']
        remove = False
        if finished:
            logger.debug(f'{name} has finished seeding, removing torrent and data')
            remove = True
        elif not CONFIG.get_bool('SEED_WAIT'):
            if (rpc_version < 14 and status == 8) or (rpc_version >= 14 and status in [5, 6]):
                logger.debug(f'{name} is seeding, removing torrent and data anyway')
                remove = True
        if remove:
            method = 'torrent-remove'
            if remove_data:
                arguments = {'delete-local-data': True, 'ids': [torrentid]}
            else:
                arguments = {'ids': [torrentid]}
            _, _ = torrent_action(method, arguments)
            return True
        logger.debug(f'{name} has not finished seeding, torrent will not be removed')
    except IndexError:
        # no torrents, already removed?
        return True
    except Exception as e:
        logger.warning(f'Unable to remove torrent {torrentid}, {type(e).__name__} {str(e)}')
        return False

    return False


def check_link():
    global session_id, host_url, rpc_version, tr_version
    method = 'session-get'
    arguments = {'fields': ['version', 'rpc-version']}
    session_id = None
    host_url = None
    rpc_version = 0
    tr_version = 0
    response, _ = torrent_action(method, arguments)  # type: dict
    if response:
        return f"Transmission login successful, v{tr_version}, rpc v{rpc_version}"
    return "Transmission login FAILED\nCheck debug log"


def torrent_action(method, arguments):
    global session_id, host_url, rpc_version, tr_version

    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger('special.dlcomms')
    logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)
    username = CONFIG['TRANSMISSION_USER']
    password = CONFIG['TRANSMISSION_PASS']

    if host_url:
        dlcommslogger.debug(f"Using existing host {host_url}")
    else:
        host = CONFIG['TRANSMISSION_HOST']
        port = CONFIG.get_int('TRANSMISSION_PORT')

        if not host or not port:
            res = 'Invalid transmission host or port, check your config'
            logger.error(res)
            return False, res

        if not host.startswith("http"):
            host = f"http://{host}"

        host = host.strip('/')

        # Fix the URL. We assume that the user does not point to the RPC endpoint,
        # so add it if it is missing.
        parts = list(urlparse(host))

        if parts[0] not in ("http", "https"):
            parts[0] = "http"

        if ':' not in parts[1]:
            parts[1] += f":{port}"

        if not parts[2].endswith("/rpc"):
            if CONFIG['TRANSMISSION_BASE']:
                parts[2] += f"/{CONFIG['TRANSMISSION_BASE'].strip('/')}/rpc"
            else:
                parts[2] += "/transmission/rpc"

        host_url = urlunparse(parts)
        dlcommslogger.debug(f'Transmission host {host_url}')

    # blank username is valid
    auth = (username, password) if password else None
    proxies = proxy_list()
    timeout = CONFIG.get_int('HTTP_TIMEOUT')
    # Retrieve session id
    if session_id:
        dlcommslogger.debug(f'Using existing session_id {session_id}')
    else:
        dlcommslogger.debug('Requesting session_id')
        try:
            if host_url.startswith('https') and CONFIG.get_bool('SSL_VERIFY'):
                response = requests.get(host_url, auth=auth, proxies=proxies, timeout=timeout,
                                        verify=CONFIG['SSL_CERTS']
                                        if CONFIG['SSL_CERTS'] else True)
            else:
                response = requests.get(host_url, auth=auth, proxies=proxies, timeout=timeout, verify=False)
        except Exception as e:
            res = f'Transmission {type(e).__name__}: {str(e)}'
            logger.error(res)
            return False, res

        if response is None:
            res = "Error getting Transmission session ID"
            logger.error(res)
            return False, res

        # Parse response
        if response.status_code == 401:
            if auth:
                res = "Username and/or password not accepted by Transmission"
            else:
                res = "Transmission authorization required"
            logger.error(res)
            return False, res
        if response.status_code == 409:
            session_id = response.headers['x-transmission-session-id']

        if not session_id:
            res = f"Expected a Session ID from Transmission, got {response.status_code}"
            logger.error(res)
            return False, res

    if not tr_version or not rpc_version:
        headers = {'x-transmission-session-id': session_id}
        data = {'method': 'session-get', 'arguments': {'fields': ['version', 'rpc-version']}}
        response = requests.post(host_url, json=data, headers=headers, proxies=proxies,
                                 auth=auth, timeout=timeout)

        if response and str(response.status_code).startswith('2'):
            res = response.json()
            tr_version = res['arguments']['version']
            rpc_version = res['arguments']['rpc-version']
            logger.debug(f"Transmission v{tr_version}, rpc v{rpc_version}")

    # Prepare real request
    headers = {'x-transmission-session-id': session_id}
    data = {'method': method, 'arguments': arguments}
    dlcommslogger.debug(f'Transmission request {str(data)}')
    try:
        response = requests.post(host_url, json=data, headers=headers, proxies=proxies,
                                 auth=auth, timeout=timeout)
        if response.status_code == 409:
            session_id = response.headers['x-transmission-session-id']
            logger.debug(f"Retrying with new session_id {session_id}")
            headers = {'x-transmission-session-id': session_id}
            response = requests.post(host_url, json=data, headers=headers, proxies=proxies,
                                     auth=auth, timeout=timeout)
        if not str(response.status_code).startswith('2'):
            res = f"Expected a response from Transmission, got {response.status_code}"
            logger.error(res)
            return False, res
        try:
            res = response.json()
            dlcommslogger.debug(f'Transmission returned {str(res)}')
        except ValueError:
            res = f"Expected json, Transmission returned {response.text}"
            logger.error(res)
            return False, res
        return res, ''

    except Exception as e:
        res = f'Transmission {type(e).__name__}: {str(e)}'
        logger.error(res)
        return False, res
