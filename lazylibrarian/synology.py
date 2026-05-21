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


import json
import logging
import re
from urllib.parse import urlencode

from lazylibrarian.cache import fetch_url
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import check_int, make_unicode
from lazylibrarian.telemetry import TELEMETRY


def _get_json(url, params):
    # Get JSON response from URL
    # Return json,True or error_msg,False

    url += f"/?{urlencode(params)}"
    result, success = fetch_url(url, retry=False)
    if success:
        try:
            result_json = json.loads(result)
            return result_json, True
        except (ValueError, AttributeError):
            return "Could not convert response to json", False

    return f"getJSON returned {result}", False


def _error_msg(errnum, api):
    # Convert DownloadStation errnum to an error message depending on which api call
    generic_errors = {
        100: 'Unknown error',
        101: 'Invalid parameter',
        102: 'The requested API does not exist',
        103: 'The requested method does not exist',
        104: 'The requested version does not support the functionality',
        105: 'The logged in session does not have permission',
        106: 'Session timeout',
        107: 'Session interrupted by duplicate login',
    }
    create_errors = {
        400: 'File upload failed',
        401: 'Max number of tasks reached',
        402: 'Destination denied',
        403: 'Destination does not exist',
        404: 'Invalid task id',
        405: 'Invalid task action',
        406: 'No default destination',
        407: 'Set destination failed',
        408: 'File does not exist'
    }
    login_errors = {
        400: 'No such account or incorrect password',
        401: 'Account disabled',
        402: 'Permission denied',
        403: '2-step verification code required',
        404: 'Failed to authenticate 2-step verification code'
    }
    if errnum in generic_errors:
        return generic_errors[errnum]
    if api == "login" and errnum in login_errors:
        return login_errors[errnum]
    if errnum in create_errors:
        return create_errors[errnum]
    return f"Unknown error code in {api}: {str(errnum)}"


def _login(hosturl):
    # Query the DownloadStation for api info and then log user in
    # return auth_cgi,task_cgi,sid or "","",""
    logger = logging.getLogger(__name__)
    url = f"{hosturl}query.cgi"
    params = {
        "api": "SYNO.API.Info",
        "version": "1",
        "method": "query",
        "query": "SYNO.API.Auth,SYNO.DownloadStation.Task"
    }

    result, success = _get_json(url, params)
    if not success:
        logger.debug(f"Synology Failed to get API info: {result}")
        return "", "", ""
    if not result['success']:
        errnum = result['error']['code']
        logger.debug(f"Synology API Error: {_error_msg(errnum, 'query')}")
        return "", "", ""
    auth_cgi = result['data']['SYNO.API.Auth']['path']
    auth_version = result['data']['SYNO.API.Auth']['maxVersion']
    task_cgi = result['data']['SYNO.DownloadStation.Task']['path']

    url = hosturl + auth_cgi
    params = {
        "api": "SYNO.API.Auth",
        "version": auth_version,
        "method": "login",
        "account": CONFIG['SYNOLOGY_USER'],
        "passwd": CONFIG['SYNOLOGY_PASS'],
        "session": "DownloadStation",
        "format": "sid"
    }

    result, success = _get_json(url, params)
    if success:
        if not result['success']:
            errnum = result['error']['code']
            logger.debug(f"Synology v{params['version']} Login Error: {_error_msg(errnum, 'login')}")
            return "", "", ""
        TELEMETRY.record_usage_data(f"Synology/Login/v{params['version']}")
        return hosturl + auth_cgi, hosturl + task_cgi, result['data']['sid']
    logger.debug(f"Synology v{params['version']} Failed to login: {repr(result)}")
    return "", "", ""


def _logout(auth_cgi, sid):
    # Logout from session, return True or False

    params = {
        "api": "SYNO.API.Auth",
        "version": "1",
        "method": "logout",
        "session": "DownloadStation",
        "_sid": sid
    }

    _, success = _get_json(auth_cgi, params)
    return success


def _list_tasks(task_cgi, sid):
    # Get a list of running downloads and return as json, or "" if fail

    params = {
        "api": "SYNO.DownloadStation.Task",
        "version": "1",
        "method": "list",
        "session": "DownloadStation",
        "_sid": sid
    }

    logger = logging.getLogger(__name__)
    result, success = _get_json(task_cgi, params)

    if success:
        if not result['success']:
            errnum = result['error']['code']
            logger.debug(f"Synology Task Error: {_error_msg(errnum, 'list')}")
        else:
            items = result['data']
            logger.debug(f"Synology Nr. Tasks = {items['total']}")
            return items['tasks']
    else:
        logger.debug(f"Synology Failed to get task list: {result}")
    return ""


def _get_info(task_cgi, sid, download_id):
    # Get additional info on a download_id, return json or "" if fail

    params = {
        "api": "SYNO.DownloadStation.Task",
        "version": "1",
        "method": "getinfo",
        "id": download_id,
        "additional": "detail,file",
        "session": "DownloadStation",
        "_sid": sid
    }

    logger = logging.getLogger(__name__)
    result, success = _get_json(task_cgi, params)
    logger.debug(f"Result from getInfo = {repr(result)}")
    if success:
        if not result['success']:
            errnum = result['error']['code']
            logger.debug(f"Synology GetInfo Error: {_error_msg(errnum, 'getinfo')}")
        else:
            if result and 'data' in result:
                try:
                    return result['data']['tasks'][0]
                except KeyError:
                    logger.debug(f"Synology GetInfo invalid result: {repr(result['data'])}")
                    return ""
    return ""


def _delete_task(task_cgi, sid, download_id, remove_data):
    # Delete a download task, return True or False

    params = {
        "api": "SYNO.DownloadStation.Task",
        "version": "1",
        "method": "delete",
        "id": download_id,
        "force_complete": remove_data,
        "session": "DownloadStation",
        "_sid": sid
    }

    logger = logging.getLogger(__name__)
    result, success = _get_json(task_cgi, params)
    logger.debug(f"Result from delete: {repr(result)}")
    if success:
        if not result['success']:
            errnum = result['error']['code']
            logger.debug(f"Synology Delete Error: {_error_msg(errnum, 'delete')}")
        else:
            try:
                errnum = result['data'][0]['error']
            except (KeyError, TypeError):
                errnum = 0
            if errnum:
                logger.debug(f"Synology Delete exited: {_error_msg(errnum, 'delete')}")
                return False
            return True
    return False


def _pause_task(task_cgi, sid, download_id):
    # Pause a download task, return True or False

    params = {
        "api": "SYNO.DownloadStation.Task",
        "version": "1",
        "method": "pause",
        "id": download_id,
        "session": "DownloadStation",
        "_sid": sid
    }

    logger = logging.getLogger(__name__)
    result, success = _get_json(task_cgi, params)
    logger.debug(f"Result from pause: {repr(result)}")
    if success:
        if not result['success']:
            errnum = result['error']['code']
            logger.debug(f"Synology Pause Error: {_error_msg(errnum, 'pause')}")
        else:
            try:
                errnum = result['data'][0]['error']
            except (KeyError, TypeError):
                errnum = 0
            if errnum:
                logger.debug(f"Synology Pause exited: {_error_msg(errnum, 'pause')}")
                return False
            return True
    return False


def _add_torrent_uri(task_cgi, sid, torurl):
    # Sends a magnet, Torrent url or NZB url to DownloadStation
    # Return task ID, or False if failed
    params = {
        "api": "SYNO.DownloadStation.Task",
        "version": "1",
        "method": "create",
        "session": "DownloadStation",
        "uri": torurl,
        "destination": CONFIG['SYNOLOGY_DIR'],
        "_sid": sid
    }

    logger = logging.getLogger(__name__)
    result, success = _get_json(task_cgi, params)
    logger.debug(f"Result from create = {repr(result)}")
    res = ''
    if success:
        errnum = 0
        if not result['success']:
            errnum = result['error']['code']
            res = f"Synology Create Error: {_error_msg(errnum, 'create')}"
            logger.debug(res)
        if not errnum or errnum == 100:
            # DownloadStation doesn't return the download_id for the newly added uri
            # which we need for monitoring progress & deleting etc.
            # so we have to scan the task list to get the id
            logger.warning(torurl)  # REMOVE ME
            try:
                matchstr = re.findall(r"urn:btih:([\w]{32,40})", torurl)[0]
            except (re.error, IndexError, TypeError):
                matchstr = torurl.replace(' ', '+')
            matchstr = make_unicode(matchstr)
            logger.warning(matchstr)  # REMOVE ME
            for task in _list_tasks(task_cgi, sid):  # type: dict
                logger.warning(str(task))  # REMOVE ME
                if task['id']:
                    info = _get_info(task_cgi, sid, task['id'])  # type: dict
                    logger.warning(str(info))  # REMOVE ME
                    try:
                        uri = info['additional']['detail']['uri']
                        if matchstr in uri:  # this might be us
                            if task['status'] == 'error':
                                try:
                                    errmsg = task['status_extra']['error_detail']
                                except KeyError:
                                    errmsg = "No error details"
                                logger.warning(errmsg)  # REMOVE ME
                                if errmsg == 'torrent_duplicate':
                                    # should we delete the duplicate here, or just return the id?
                                    # if the original is still active we might find it further down the list
                                    _ = _delete_task(task_cgi, sid, task['id'], False)
                                else:
                                    res = f"Synology task [{task['title']}] failed: {errmsg}"
                                    logger.warning(res)
                                    return False, res
                            else:
                                logger.debug(f"Synology task {task['id']} for {task['title']}")
                                return task['id'], ''
                    except KeyError:
                        res = f"Unable to get uri for [{task['title']}] from getInfo"
                        logger.debug(res)
            res = f"Synology URL [{torurl}] was not found in tasklist"
            logger.debug(res)
            return False, res
    else:
        res = f"Synology Failed to add task: {result}"
        logger.debug(res)
    return False, res


def _host_url():
    # Build webapi_url from config settings
    logger = logging.getLogger(__name__)
    host = CONFIG['SYNOLOGY_HOST']
    port = CONFIG.get_int('SYNOLOGY_PORT')
    if not host or not port:
        logger.debug(f"Invalid Synology host or port, check your config: {host}:{port}")
        return False
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"http://{host}"
    host = host.rstrip('/')
    return f"{host}:{port}/webapi/"


#
# Public functions
#
def check_link():
    # Make sure we can login to the synology drivestation
    # This function is used by the "test synology" button
    # to return a message giving success or fail
    msg = "Synology login FAILED\nCheck debug log"
    hosturl = _host_url()
    if hosturl:
        auth_cgi, _, sid = _login(hosturl)
        if sid:
            msg = "Synology login successful"
            _logout(auth_cgi, sid)
    return msg


def remove_torrent(hash_id, remove_data=False):
    # remove a torrent using hashID, and optionally delete the data
    # return True/False
    hosturl = _host_url()
    if hosturl:
        auth_cgi, task_cgi, sid = _login(hosturl)
        if sid:
            result = _delete_task(task_cgi, sid, hash_id, remove_data)
            _logout(auth_cgi, sid)
            return result
    return False


def get_name(download_id):
    # get the name of a download from it's download_id
    # return "" if not found
    hosturl = _host_url()
    if hosturl:
        auth_cgi, task_cgi, sid = _login(hosturl)
        if sid:
            result = _get_info(task_cgi, sid, download_id)  # type: dict
            _logout(auth_cgi, sid)
            if result and 'title' in result:
                return result['title']
    return ""


def get_folder(download_id):
    # get the name of a download from it's download_id
    # return "" if not found
    logger = logging.getLogger(__name__)
    hosturl = _host_url()
    if hosturl:
        auth_cgi, task_cgi, sid = _login(hosturl)
        if sid:
            result = _get_info(task_cgi, sid, download_id)  # type: dict
            _logout(auth_cgi, sid)
            if result:
                try:
                    return result['additional']['detail']['destination']
                except Exception as e:
                    logger.warning(e)
    return ""


def get_progress(download_id):
    # get the progress/status of a download from it's download_id
    # return "" if not found
    hosturl = _host_url()
    if hosturl:
        auth_cgi, task_cgi, sid = _login(hosturl)
        if not sid:
            return -2, 'connection error', False
        result = _get_info(task_cgi, sid, download_id)  # type: dict
        _logout(auth_cgi, sid)
        if result:
            status = result.get("status", "")
            # can't see how to get a % from synology, so have to work it out ourselves...
            if 'additional' in result:
                try:
                    files = result['additional']['file']
                except KeyError:
                    files = []
            else:
                files = []
            tot_size = 0
            got_size = 0
            for item in files:
                tot_size += check_int(item['size'], 0)
                got_size += check_int(item['size_downloaded'], 0)

            if tot_size:
                pc = int((got_size * 100) / tot_size)
            else:
                pc = 0
            return pc, status, (status == 'finished')
    return -1, 'not found', False


def get_files(download_id):
    # get the files in a download from it's download_id
    # return "" if not found
    hosturl = _host_url()
    if hosturl:
        auth_cgi, task_cgi, sid = _login(hosturl)
        if sid:
            result = _get_info(task_cgi, sid, download_id)  # type: dict
            _logout(auth_cgi, sid)
            if result and 'additional' in result:
                try:
                    return result['additional']['file']
                except KeyError:
                    return ""
    return ""


def pause_torrent(download_id):
    hosturl = _host_url()
    if hosturl:
        auth_cgi, task_cgi, sid = _login(hosturl)
        if sid:
            result = _pause_task(task_cgi, sid, download_id)
            _logout(auth_cgi, sid)
            return result
    return False


def add_torrent(tor_url):
    # add a torrent/magnet/nzb to synology downloadstation
    # return it's id, or return False if error
    hosturl = _host_url()
    if hosturl:
        auth_cgi, task_cgi, sid = _login(hosturl)
        if sid:
            result, res = _add_torrent_uri(task_cgi, sid, tor_url)
            _logout(auth_cgi, sid)
            return result, res
    return False, "Invalid synology host"
