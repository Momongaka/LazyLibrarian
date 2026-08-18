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

import logging
from urllib.parse import urlencode

import requests

import lazylibrarian
from lazylibrarian.common import proxy_list
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import make_utf8bytes, versiontuple


def check_link():
    logger = logging.getLogger(__name__)
    # connection test, check host/port
    auth, _ = sab_nzbd(nzburl='auth')
    if not auth:
        return "Unable to talk to sab_nzbd, check HOST/PORT/SUBDIR"
    vers, _ = sab_nzbd(nzburl='version')
    if not vers or 'version' not in vers:
        lazylibrarian.SAB_VER = (0, 0, 0)
        vers = {'version': 'unknown'}
    else:
        lazylibrarian.SAB_VER = versiontuple(vers['version'])
        logger.debug(f"SAB version tuple {str(lazylibrarian.SAB_VER)}")
    # check apikey is valid
    cats, _ = sab_nzbd(nzburl='get_cats')  # type: dict
    if not cats:
        return "Unable to talk to sab_nzbd, check APIKEY"
    # check category exists
    if CONFIG['SAB_CAT']:
        if 'categories' not in cats or not len(cats['categories']):
            return f"Failed to get sab_nzbd categories: {str(cats)}"
        if CONFIG['SAB_CAT'].split(',')[0] not in cats['categories']:
            return f"sab_nzbd: Unknown category [{CONFIG['SAB_CAT']}]\nValid categories:\n{str(cats['categories'])}"
    return f"sab_nzbd connection successful, version {vers['version']}"


def sab_nzbd(title=None, nzburl=None, remove_data=False, search=None, nzo_ids=None, library='eBook', label=''):
    logger = logging.getLogger(__name__)

    if nzburl in ['delete', 'delhistory', 'pause'] and title == 'unknown':
        res = f'{nzburl} function unavailable in this version of sabnzbd, no nzo_ids'
        logger.debug(res)
        return False, res

    hostname = CONFIG['SAB_HOST']
    port = CONFIG.get_int('SAB_PORT')
    if not hostname or not port:
        res = 'Invalid sabnzbd host or port, check your config'
        logger.error(res)
        return False, res

    hostname = hostname.rstrip('/')
    if not hostname.startswith("http://") and not hostname.startswith("https://"):
        hostname = f"http://{hostname}"

    host = f"{hostname}:{port}"

    if CONFIG['SAB_SUBDIR']:
        host = f"{host}/{CONFIG['SAB_SUBDIR'].strip('/')}"

    params = {}

    if nzburl in ['auth', 'get_cats', 'version']:
        # connection test
        params['mode'] = nzburl
        params['output'] = 'json'
        if CONFIG['SAB_API']:
            params['apikey'] = CONFIG['SAB_API']
        title = f'LL.({nzburl})'
    elif nzburl == 'queue':
        params['mode'] = 'queue'
        params['limit'] = '50'
        params['output'] = 'json'
        if search:
            params['search'] = search
        if nzo_ids:
            params['nzo_ids'] = nzo_ids
        if CONFIG['SAB_CAT']:
            if label:
                params['category'] = label
            else:
                params['category'] = lazylibrarian.downloadmethods.use_label('SABNZBD', library)
        if CONFIG['SAB_USER']:
            params['ma_username'] = CONFIG['SAB_USER']
        if CONFIG['SAB_PASS']:
            params['ma_password'] = CONFIG['SAB_PASS']
        if CONFIG['SAB_API']:
            params['apikey'] = CONFIG['SAB_API']
        title = 'LL.(Queue)'
    elif nzburl == 'history':
        params['mode'] = 'history'
        params['limit'] = '50'
        params['output'] = 'json'
        if search:
            params['search'] = search
        if nzo_ids:
            params['nzo_ids'] = nzo_ids
        if CONFIG['SAB_CAT']:
            if label:
                params['category'] = label
            else:
                params['category'] = lazylibrarian.downloadmethods.use_label('SABNZBD', library)
        if CONFIG['SAB_USER']:
            params['ma_username'] = CONFIG['SAB_USER']
        if CONFIG['SAB_PASS']:
            params['ma_password'] = CONFIG['SAB_PASS']
        if CONFIG['SAB_API']:
            params['apikey'] = CONFIG['SAB_API']
        title = 'LL.(History)'
    elif nzburl == 'delete':
        # only deletes tasks if still in the queue, ie NOT completed tasks
        params['mode'] = 'queue'
        params['output'] = 'json'
        params['name'] = nzburl
        params['value'] = make_utf8bytes(title)[0]
        if CONFIG['SAB_USER']:
            params['ma_username'] = CONFIG['SAB_USER']
        if CONFIG['SAB_PASS']:
            params['ma_password'] = CONFIG['SAB_PASS']
        if CONFIG['SAB_API']:
            params['apikey'] = CONFIG['SAB_API']
        if remove_data:
            params['del_files'] = 1
        title = f"LL.(Delete) {title}"
    elif nzburl == 'delhistory':
        params['mode'] = 'history'
        params['output'] = 'json'
        params['name'] = 'delete'
        params['value'] = make_utf8bytes(title)[0]
        if CONFIG['SAB_USER']:
            params['ma_username'] = CONFIG['SAB_USER']
        if CONFIG['SAB_PASS']:
            params['ma_password'] = CONFIG['SAB_PASS']
        if CONFIG['SAB_API']:
            params['apikey'] = CONFIG['SAB_API']
        if remove_data:
            params['del_files'] = 1
        title = f"LL.(DelHistory) {title}"
    elif nzburl == 'pause':
        params['mode'] = 'queue'
        params['output'] = 'json'
        params['name'] = 'pause'
        params['value'] = nzo_ids
        if CONFIG['SAB_USER']:
            params['ma_username'] = CONFIG['SAB_USER']
        if CONFIG['SAB_PASS']:
            params['ma_password'] = CONFIG['SAB_PASS']
        if CONFIG['SAB_API']:
            params['apikey'] = CONFIG['SAB_API']
        title = f"LL.(Pause) {title}"
    else:
        params['mode'] = 'addurl'
        params['output'] = 'json'
        if nzburl:
            params['name'] = make_utf8bytes(nzburl)[0]
        if title:
            params['nzbname'] = make_utf8bytes(title)[0]
        if CONFIG['SAB_USER']:
            params['ma_username'] = CONFIG['SAB_USER']
        if CONFIG['SAB_PASS']:
            params['ma_password'] = CONFIG['SAB_PASS']
        if CONFIG['SAB_API']:
            params['apikey'] = CONFIG['SAB_API']
        if CONFIG['SAB_CAT']:
            if label:
                params['category'] = label
            else:
                params['cat'] = lazylibrarian.downloadmethods.use_label('SABNZBD', library)
        if CONFIG.get_int('USENET_RETENTION'):
            params["maxage"] = CONFIG['USENET_RETENTION']

# FUTURE-CODE
#    if lazylibrarian.SAB_PRIO:
#        params["priority"] = lazylibrarian.SAB_PRIO
#    if lazylibrarian.SAB_PP:
#        params["script"] = lazylibrarian.SAB_SCRIPT

    dlcommslogger = logging.getLogger('special.dlcomms')
    dlcommslogger.debug(f'sab params: {repr(params)}')
    logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)

    url = f"{host}/api?{urlencode(params)}"

    dlcommslogger.debug(f'Request url for <a href="{url}">sab_nzbd</a>')
    proxies = proxy_list()
    try:
        timeout = CONFIG.get_int('HTTP_TIMEOUT')
        if url.startswith('https') and CONFIG.get_bool('SSL_VERIFY'):
            r = requests.get(url, timeout=timeout, proxies=proxies,
                             verify=CONFIG['SSL_CERTS'] if CONFIG['SSL_CERTS'] else True)
        else:
            r = requests.get(url, timeout=timeout, proxies=proxies, verify=False)
        result = r.json()
    except requests.exceptions.Timeout:
        res = f"Timeout connecting to SAB with URL: {url}"
        logger.error(res)
        return False, res
    except Exception as e:
        res = f"Unable to connect to SAB with URL: {url}, {type(e).__name__}:{str(e)}"
        logger.error(res)
        return False, res
    dlcommslogger.debug(f"Result text from SAB: {str(result)}")

    if title and title.startswith('LL.('):
        return result, ''

    if result.get('status') is True:
        logger.info(f"{title} sent to SAB successfully.")
        # sab versions earlier than 0.8.0 don't return nzo_ids
        if 'nzo_ids' in result and result['nzo_ids']:  # check its not empty
            return result['nzo_ids'][0], ''
        return 'unknown', ''

    res = f"Send to SAB failed: {str(result)}"
    logger.error(res)
    return False, res
