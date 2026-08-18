# This file is modified to work with lazylibrarian by CurlyMo <curlymoo1@gmail.com>
# as a part of XBian - XBMC on the Raspberry Pi

# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
#
# This file is part of LazyLibrarian.
#
# LazyLibrarian is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# LazyLibrarian is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with LazyLibrarian.  If not, see <http://www.gnu.org/licenses/>.

import logging
from base64 import standard_b64encode
from http.client import HTTPException
from urllib.parse import quote
from xmlrpc.client import ProtocolError, ServerProxy

import lazylibrarian
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import make_unicode


def check_link():
    # socket.setdefaulttimeout(2)
    test, res = send_nzb(cmd="test")
    # socket.setdefaulttimeout(None)
    if test:
        return "NZBget connection successful"
    return f"NZBget connection FAILED\n{res}"


def delete_nzb(nzbid, remove_data=False):
    if remove_data:
        send_nzb(cmd='GroupFinalDelete', nzbid=nzbid)
        return send_nzb(cmd='HistoryFinalDelete', nzbid=nzbid)
    send_nzb(cmd='GroupDelete', nzbid=nzbid)
    return send_nzb(cmd='HistoryDelete', nzbid=nzbid)


def send_nzb(nzb=None, cmd=None, nzbid=None, library='eBook', label=''):
    # we can send a new nzb, or commands to act on an existing nzbID (or array of nzbIDs)
    # by setting nzbID and cmd (we currently only use test, history, listgroups and delete)
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger('special.dlcomms')
    host = CONFIG['NZBGET_HOST']
    port = CONFIG.get_int('NZBGET_PORT')
    if not host or not port:
        res = 'Invalid NZBget host or port, check your config'
        logger.error(res)
        return False, res

    add_to_top = False
    nzbget_xml_rpc = "%(username)s:%(password)s@%(host)s:%(port)s/xmlrpc"

    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"http://{host}"

    host = host.rstrip('/')
    hostparts = host.split('://')

    url = hostparts[0] + '://' + nzbget_xml_rpc % {"host": hostparts[1],
                                                   "username": quote(CONFIG['NZBGET_USER'], safe=''),
                                                   "port": port,
                                                   "password": quote(CONFIG['NZBGET_PASS'], safe='')}
    try:
        nzb_get_rpc = ServerProxy(url)
    except Exception as e:
        res = f"NZBget connection to {url} failed: {type(e).__name__} {str(e)}"
        logger.error(res)
        return False, res

    if cmd == "test":
        msg = "lazylibrarian connection test"
    elif cmd == 'history':
        msg = "lazylibrarian requesting history"
    elif cmd == 'listgroups':
        msg = "lazylibrarian requesting listgroups"
    elif cmd == 'GroupPause':
        msg = "lazylibrarian requesting pause"
    elif nzbid:
        msg = f"lazylibrarian connected to {cmd} {nzbid}"
    else:
        msg = f"lazylibrarian connected to drop off {nzb.name}.nzb any moment now."

    try:
        if nzb_get_rpc.writelog("INFO", msg):
            dlcommslogger.debug("Successfully connected to NZBget")
            if cmd == "test":
                # should check nzbget category is valid
                return True, ''
        else:
            if nzbid is not None:
                res = "Successfully connected to NZBget, unable to send message"
                logger.debug(res)
                return False, res
            logger.warning(f"Successfully connected to NZBget, but unable to send {nzb.name}.nzb")

    except HTTPException as e:
        res = "Please check your NZBget host and port (if it is running). "
        res += f"NZBget is not responding to this combination: {e}"
        logger.error(res)
        logger.error(f"NZBget url is [{url}]")
        return False, res

    except ProtocolError as e:
        if e.errmsg == "Unauthorized":
            res = "NZBget password is incorrect."
        else:
            res = f"Protocol Error: {e.errmsg}"
        logger.error(res)
        return False, res

    except Exception as e:
        res = f"nzbGet Exception: {e}"
        logger.error(res)
        logger.error(f"NZBget url [{url}]")
        return False, res

    if cmd == 'history':
        try:
            res = nzb_get_rpc.history()
            return res, ''
        except Exception as e:
            logger.error(f"NZBget history: {e}")
            logger.error(f"NZBget url [{url}]")
            return '' , str(e)

    if cmd == 'listgroups':
        try:
            res = nzb_get_rpc.listgroups()
            return res, ''
        except Exception as e:
            logger.error(f"NZBget listgroups: {e}")
            logger.error(f"NZBget url [{url}]")
            return '' , str(e)

    if nzbid is not None:
        # its a command for an existing task
        id_array = [int(nzbid)]
        if cmd in ['GroupDelete', 'GroupFinalDelete', 'HistoryDelete', 'HistoryFinalDelete', 'GroupPause']:
            return nzb_get_rpc.editqueue(cmd, 0, "", id_array), ''
        res = f'Unsupported nzbget command {repr(cmd)}'
        logger.debug(res)
        return False, res

    nzbcontent64 = None
    if nzb.result_type == "nzbdata":
        data = nzb.extraInfo[0]
        nzbcontent64 = make_unicode(standard_b64encode(data))

    logger.info("Sending NZB to NZBget")
    dlcommslogger.debug(f"URL: {url}")

    dupekey = ""
    dupescore = 0

    if not label:
        label = lazylibrarian.downloadmethods.use_label('NZBGET', library)

    try:
        # Find out if nzbget supports priority (Version 9.0+), old versions
        # beginning with a 0.x will use the old command
        nzbget_version_str = nzb_get_rpc.version()
        nzbget_version = int(nzbget_version_str[:nzbget_version_str.find(".")])
        dlcommslogger.debug(f"NZB Version {nzbget_version}")
        # for some reason 14 seems to not work with >= 13 method? I get invalid param autoAdd
        # PAB think its fixed now, code had autoAdd param as "False", it's not a string, it's bool so False
        if nzbget_version == 0:  # or nzbget_version == 14:
            if nzbcontent64:
                nzbget_result = nzb_get_rpc.append(f"{nzb.name}.nzb", label, add_to_top, nzbcontent64)
            else:
                return False, "No nzbcontent64 found"
        elif nzbget_version == 12:
            if nzbcontent64:
                nzbget_result = nzb_get_rpc.append(f"{nzb.name}.nzb", label,
                                                   CONFIG.get_int('NZBGET_PRIORITY'), False,
                                                   nzbcontent64, False, dupekey, dupescore, "score")
            else:
                nzbget_result = nzb_get_rpc.appendurl(f"{nzb.name}.nzb", label,
                                                      CONFIG.get_int('NZBGET_PRIORITY'), False, nzb.url, False,
                                                      dupekey, dupescore, "score")
        # v13+ has a new combined append method that accepts both (url and content)
        # also the return value has changed from boolean to integer
        # (Positive number representing NZBID of the queue item. 0 and negative numbers represent error codes.)
        elif nzbget_version >= 13:
            nzbget_result = nzb_get_rpc.append(f"{nzb.name}.nzb", nzbcontent64 if nzbcontent64 is not None else nzb.url,
                                               label, CONFIG.get_int('NZBGET_PRIORITY'), False, False, dupekey,
                                               dupescore, "score")
            if nzbget_result <= 0:
                nzbget_result = False
        else:
            if nzbcontent64:
                nzbget_result = nzb_get_rpc.append(f"{nzb.name}.nzb", label,
                                                   CONFIG.get_int('NZBGET_PRIORITY'), False, nzbcontent64)
            else:
                nzbget_result = nzb_get_rpc.appendurl(f"{nzb.name}.nzb", label,
                                                      CONFIG.get_int('NZBGET_PRIORITY'), False, nzb.url)

        if nzbget_result:
            logger.debug("NZB sent to NZBget successfully")
            return nzbget_result, ''
        res = f"NZBget could not add {nzb.name}.nzb to the queue"
        logger.error(res)
        return False, res
    except Exception as e:
        res = f"Connect Error to NZBget: could not add {nzb.name}.nzb to the queue: {type(e).__name__} {e}"
        logger.error(res)
        return False, res
