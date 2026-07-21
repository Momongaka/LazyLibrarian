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
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>

import logging
import os
import time

import requests

from lazylibrarian.config2 import CONFIG
from lib.qbittorrent import Client, WrongCredentialsError

# qBittorrent Web API 2.14+ can accept a torrents/add request before it has
# actually retrieved and parsed the url (pending_count > 0, HTTP 202). While
# that is in progress, torrents/properties for the new hash legitimately 404,
# so a short poll window isn't enough to tell a slow fetch from a real failure.
QBIT_ADD_POLL_SECONDS = 10
QBIT_ADD_PENDING_POLL_SECONDS = 60


def get_client():
    logger = logging.getLogger(__name__)

    host = CONFIG['QBITTORRENT_HOST']
    port = CONFIG.get_int('QBITTORRENT_PORT')
    if not host.startswith("http"):
        host = f"http://{host}"
    host = host.strip('/')

    if CONFIG['QBITTORRENT_BASE']:
        url = f"{host}:{port}/{CONFIG['QBITTORRENT_BASE'].strip('/')}"
    else:
        url = f"{host}:{port}"

    try:
        verify_ssl = not CONFIG.get_bool('QBITTORRENT_IGNORE_SSL')
        qb = Client(url, CONFIG['QBITTORRENT_USER'], CONFIG['QBITTORRENT_PASS'], verify=verify_ssl)
    except WrongCredentialsError:
        logger.error("qBittorrent reports Wrong Credentials")
        return None
    except Exception as e:
        logger.error(f"qBittorrent login Error: {e}")
        return None

    try:
        api = qb.api_version
    except Exception as e:
        logger.error(f"qBittorrent api_version Error: {e}")
        return None

    if not api:
        logger.debug("Failed to login to qBittorrent")
        return None
    return qb


def get_files(hashid):
    dlcommslogger = logging.getLogger('special.dlcomms')

    dlcommslogger.debug(f'get_torrent_files({hashid})')
    hashid = hashid.lower()
    qbclient = get_client()
    if not qbclient:
        return ''
    retries = 5

    while retries:
        try:
            files = qbclient.get_torrent_files(hashid)
        except Exception as e:
            dlcommslogger.error(f"Failed to get_files: {e}")
            return ''
        if files:
            return files
        time.sleep(2)
        retries -= 1
    return ''


def get_name(hashid):
    dlcommslogger = logging.getLogger('special.dlcomms')

    dlcommslogger.debug(f'get_name({hashid})')
    hashid = hashid.lower()
    qbclient = get_client()
    if not qbclient:
        return ''

    retries = 5
    cat = CONFIG['QBITTORRENT_LABEL']
    if not cat:
        cat = None
    while retries:
        # get_torrent(hashid) gets info on one torrent but doesn't return all the information
        # eg we are missing name, state, progress
        # so get all of our torrents and then look for the hashid
        try:
            torrents = qbclient.torrents(category=cat)
        except Exception as e:
            dlcommslogger.error(f" Failed to get_name: {e}")
            return ''
        for torrent in torrents:
            if torrent.get('hash') == hashid and torrent.get('name'):
                return torrent['name']
        time.sleep(2)
        retries -= 1
    return ''


def get_folder(hashid):
    dlcommslogger = logging.getLogger('special.dlcomms')

    dlcommslogger.debug(f'get_folder({hashid})')
    hashid = hashid.lower()
    qbclient = get_client()
    if not qbclient:
        return ''

    retries = 5
    save_path = ''
    cat = CONFIG['QBITTORRENT_LABEL']
    if not cat:
        cat = None
    while retries:
        try:
            torrents = qbclient.torrents(category=cat)
        except Exception as e:
            dlcommslogger.error(f"Failed to get_folder: {e}")
            torrents = ''
        for torrent in torrents:
            if torrent.get('hash') == hashid and torrent.get('content_path'):
                # return absolute path of single file, or folder contaning multi files
                return torrent['content_path']
        time.sleep(6)
        retries -= 1
    if not save_path:
        return ''
    if os.name != 'nt':
        save_path = save_path.replace('\\', '/')
    return os.path.basename(os.path.normpath(save_path))


def get_progress(hashid):
    # returns int(progress/error), state/errormessage, bool(complete)
    # error codes -1 not found, -2 communication error
    dlcommslogger = logging.getLogger('special.dlcomms')
    dlcommslogger.debug(f'get_progress({hashid})')
    hashid = hashid.lower()
    qbclient = get_client()
    if not qbclient:
        return -2, 'error connecting', False
    failure = ''
    try:
        preferences = qbclient.preferences()
    except Exception as e:
        dlcommslogger.error(f"Failed to get_progress: {e}")
        preferences = {}
        failure = str(e)
    dlcommslogger.debug(str(preferences))
    max_ratio = 0.0
    if 'max_ratio_enabled' in preferences and 'max_ratio' in preferences and preferences['max_ratio_enabled']:
        max_ratio = float(preferences['max_ratio'])
    max_seeding_time = 0
    if preferences.get('max_seeding_time_enabled') and 'max_seeding_time' in preferences:
        max_seeding_time = int(preferences['max_seeding_time'])
    cat = CONFIG['QBITTORRENT_LABEL']
    if not cat:
        cat = None
    try:
        torrents = qbclient.torrents(category=cat)
    except Exception as e:
        dlcommslogger.error(f"Failed to get torrents: {e}")
        return -2, 'error getting torrents', False

    for torrent in torrents:
        if torrent.get('hash') == hashid:
            state = torrent.get('state', '')
            if 'ratio' in torrent:
                ratio = float(torrent['ratio'])
            else:
                ratio = 0.0
            if 'progress' in torrent:
                try:
                    progress = int(100 * float(torrent['progress']))
                except ValueError:
                    progress = 0
            else:
                progress = 0
            finished = False

            # state was changed from pausedUP to stoppedUP in web API 2.11.0, but wiki doesn't reflect change
            # See: https://qbittorrent-api.readthedocs.io/en/latest/apidoc/definitions.html
            if state == 'pausedUP' or state == 'stoppedUP':
                ratio_met = max_ratio > 0 and ratio >= max_ratio
                seeding_time = torrent.get('seeding_time', 0)
                time_met = max_seeding_time > 0 and seeding_time >= max_seeding_time * 60
                if ratio_met or time_met:
                    finished = True
            return progress, state, finished
    return -1, failure if failure else 'error hash not found', False


def remove_torrent(hashid, remove_data=False):
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger('special.dlcomms')
    dlcommslogger.debug(f'remove_torrent({hashid},{remove_data})')
    hashid = hashid.lower()
    qbclient = get_client()
    if not qbclient:
        return False

    cat = CONFIG['QBITTORRENT_LABEL']
    if not cat:
        cat = None
    try:
        torrents = qbclient.torrents(category=cat)
    except Exception as e:
        dlcommslogger.error(f" Failed to remove_torrent: {e}")
        return False
    for torrent in torrents:
        if torrent.get('hash') == hashid:
            remove = True
            if torrent['state'] == 'uploading' or torrent['state'] == 'stalledUP':
                if not CONFIG.get_bool('SEED_WAIT'):
                    logger.debug(f"{torrent['name']} is seeding, removing torrent and data anyway")
                else:
                    logger.info(f"{torrent['name']} has not finished seeding yet, torrent will not be removed")
                    remove = False
            if remove:
                if remove_data:
                    try:
                        qbclient.delete_permanently(hashid)
                        logger.info(f"{torrent['name']} removing torrent and data")
                    except Exception as e:
                        dlcommslogger.error(f"Failed to delete_permanently: {e}")
                        return False
                else:
                    try:
                        qbclient.delete(hashid)
                        logger.info(f"{torrent['name']} removing torrent")
                    except Exception as e:
                        dlcommslogger.error(f"Failed to delete: {e}")
                        return False
                return True
    return False


def check_link():
    """ Check we can talk to qbittorrent"""
    try:
        qb_api = ''
        qbclient = get_client()
        if qbclient:
            qb_api = qbclient.api_version
        if qb_api:
            qb_version = qbclient.qbittorrent_version
            return f"qBittorrent login successful, api: {qb_api} version: {qb_version}"
        return "qBittorrent login FAILED\nCheck debug log"
    except Exception as err:
        return f"qBittorrent login FAILED: {type(err).__name__} {str(err)}"


def classify_add_response(result):
    """ Interpret the response from qBittorrent's torrents/add endpoint.

    Pre-2.14 Web API responses (plain "Ok." text, or an empty body) carry no
    structured info, so there's nothing to classify - treat as 'legacy' and
    fall back to polling for the hash as before. Web API 2.14+ returns JSON
    with success_count/pending_count/failure_count and added_torrent_ids.

    :return: (state, added_ids) where state is one of
             'legacy', 'accepted', 'pending', 'rejected'
    """
    if not isinstance(result, dict):
        return 'legacy', []

    added_ids = result.get('added_torrent_ids') or []
    success_count = result.get('success_count', 0)
    pending_count = result.get('pending_count', 0)
    failure_count = result.get('failure_count', 0)

    if failure_count and not (success_count or pending_count):
        return 'rejected', added_ids
    # Check pending ahead of success: if a batch response is ever a mix of
    # the two, we still need the longer pending poll window, not the short
    # one that's only safe when everything in the batch was already added.
    if pending_count:
        return 'pending', added_ids
    if success_count:
        return 'accepted', added_ids
    return 'legacy', added_ids


def wait_for_torrent(qbclient, dlcommslogger, hashid, result, label):
    """ Poll qBittorrent until a just-added torrent shows up, or give up.

    :param result: the response already returned by download_from_link/file
    :param label: 'add_torrent' or 'add_file', used in the failure message
    """
    state, added_ids = classify_add_response(result)
    dlcommslogger.debug(f"torrents/add response: {result} (state={state}, added_ids={added_ids})")

    if state == 'rejected':
        res = f"qBittorrent rejected the torrent: {result}"
        dlcommslogger.error(res)
        return False, res
    if isinstance(result, dict) and result.get('failure_count'):
        # Only reachable when success_count or pending_count is also set, ie a
        # partial failure on a multi-url add. We only ever submit one url, so
        # this shouldn't happen in practice, but don't discard it silently.
        dlcommslogger.error(f"qBittorrent reported a partial add failure: {result}")

    max_wait = QBIT_ADD_PENDING_POLL_SECONDS if state == 'pending' else QBIT_ADD_POLL_SECONDS
    count = 0
    while count < max_wait:
        count += 1
        time.sleep(1)
        try:
            torrent = qbclient.get_torrent(hashid)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # Not indexed yet, e.g. qBittorrent is still fetching a pending url
                continue
            dlcommslogger.error(f" Failed {label}: {e}")
            return False, str(e)
        except Exception as e:
            dlcommslogger.error(f" Failed {label}: {e}")
            return False, str(e)
        if torrent:
            # Add explicit pause as qbittorrent v5 seems to ignore start paused arg
            if CONFIG.get_bool('TORRENT_PAUSED'):
                try:
                    paused = not qbclient.qbittorrent_version.startswith('v5')
                    dlcommslogger.debug(f"Pausing torrent {hashid}")
                    qbclient.pause(hashid, paused)
                except Exception as e:
                    dlcommslogger.error(f" Failed to pause torrent {hashid}: {e}")
            if count > 1:
                dlcommslogger.debug(f"hashid found in torrent list after {count} seconds")
            return True, ''
    res = f"hashid not found in torrent list, {label} failed"
    dlcommslogger.debug(res)
    return False, res


def add_file(data, hashid, title, provider_options):
    dlcommslogger = logging.getLogger('special.dlcomms')

    dlcommslogger.debug(f'add_file(data){title}')
    hashid = hashid.lower()
    qbclient = get_client()
    if not qbclient:
        return False, "Failed to login to qbittorrent"

    kwargs = get_args(provider_options)
    dlcommslogger.debug(f'{kwargs}')
    try:
        result = qbclient.download_from_file(data, **kwargs)
    except Exception as e:
        dlcommslogger.error(f"Failed to download_from_file: {e}")
        return False, str(e)

    return wait_for_torrent(qbclient, dlcommslogger, hashid, result, 'add_file')


def add_torrent(link, hashid, provider_options):
    dlcommslogger = logging.getLogger('special.dlcomms')

    dlcommslogger.debug(f'add_torrent({link})')

    qbclient = get_client()
    if not qbclient:
        return False, "Failed to login to qbittorrent"

    hashid = hashid.lower()
    kwargs = get_args(provider_options)
    dlcommslogger.debug(f'{kwargs}')
    try:
        result = qbclient.download_from_link(link, **kwargs)
    except Exception as e:
        dlcommslogger.error(f" Failed to download_from_link: {e}")
        return False, str(e)

    return wait_for_torrent(qbclient, dlcommslogger, hashid, result, 'add_torrent')


def get_args(provider_options):
    """ Get optional arguments based on configuration"""
    args = {'paused': bool(CONFIG.get_bool('TORRENT_PAUSED'))}
    if CONFIG['QBITTORRENT_DIR']:
        args['savepath'] = CONFIG['QBITTORRENT_DIR']

    if CONFIG['QBITTORRENT_LABEL']:
        args['category'] = CONFIG['QBITTORRENT_LABEL']

    if "seed_ratio" in provider_options:
        args['ratioLimit'] = provider_options["seed_ratio"]
    if "seed_duration" in provider_options:
        args['seedingTimeLimit'] = provider_options["seed_duration"]

    return args
