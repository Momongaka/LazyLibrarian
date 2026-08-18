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
import socket
import ssl
from time import sleep
from xmlrpc.client import Binary, ServerProxy

from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import get_directory
from lazylibrarian.formatter import versiontuple


def get_server():
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger('special.dlcomms')
    host = CONFIG['RTORRENT_HOST']
    if not host:
        logger.error("rtorrent error: No host found, check your config")
        return None, ''

    host = host.rstrip('/')
    if not host.startswith("http://") and not host.startswith("https://"):
        host = f"http://{host}"

    if CONFIG['RTORRENT_USER']:
        user = CONFIG['RTORRENT_USER']
        password = CONFIG['RTORRENT_PASS']
        parts = host.split('://')
        host = f"{parts[0]}://{user}:{password}@{parts[1]}"

    try:
        socket.setdefaulttimeout(20)  # so we don't freeze if server is not there
        if host.startswith("https://"):
            context = ssl.create_default_context()
            if not CONFIG.get_bool('SSL_VERIFY'):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            server = ServerProxy(host, context=context)
        else:
            server = ServerProxy(host)
        version = server.system.client_version()
        socket.setdefaulttimeout(None)  # reset timeout
        dlcommslogger.debug(f"rTorrent client version = {version}")
    except Exception as e:
        socket.setdefaulttimeout(None)  # reset timeout if failed
        logger.error(f"xmlrpc_client error: {repr(e)}")
        return None, ''
    if version and versiontuple(version) >= versiontuple('0.9.8'):
        return server, version
    if version:
        logger.error(f"rTorrent {version} is not supported, require >= 0.9.8")
        return None, version
    logger.warning('No response from rTorrent server')
    return None, ''


def add_torrent(tor_url, hash_id, data=None):
    logger = logging.getLogger(__name__)
    server, version = get_server()
    if not server:
        if version:
            return False, f'rTorrent {version} is not supported (require >= 0.9.8)'
        return False, 'rTorrent unable to connect to server'
    try:
        paused = CONFIG.get_bool('TORRENT_PAUSED')
        label = CONFIG['RTORRENT_LABEL']
        directory = CONFIG['RTORRENT_DIR']

        post_load_cmds = []
        if label:
            post_load_cmds.append(f'd.custom1.set="{label}"')
        if directory:
            post_load_cmds.append(f'd.directory.set="{directory}"')

        if data:
            logger.debug(f'Sending rTorrent content [{str(data)[:40]}...]')
            if paused:
                _ = server.load.raw('', Binary(data), *post_load_cmds)
            else:
                _ = server.load.raw_start('', Binary(data), *post_load_cmds)
        else:
            logger.debug(f'Sending rTorrent url [{str(tor_url)[:40]}...]')
            if paused:
                _ = server.load.normal('', tor_url, *post_load_cmds)  # response isn't anything useful, always 0
            else:
                _ = server.load.start('', tor_url, *post_load_cmds)
        # need a short pause while rtorrent loads it
        retries = 5
        while retries:
            mainview = server.download_list("", "main")
            if any(tor.upper() == hash_id.upper() for tor in mainview):
                break
            sleep(1)
            retries -= 1

    except Exception as e:
        res = f"rTorrent Error: {type(e).__name__}: {str(e)}"
        logger.error(res)
        return False, res

    # wait a while for download to start, that's when rtorrent fills in the name
    name = get_name(hash_id)
    if name:
        directory = get_directory(hash_id)
        label = server.d.custom1(hash_id)

        if label:
            logger.debug(f'rTorrent downloading {name} to {directory} with label {label}')
        else:
            logger.debug(f'rTorrent downloading {name} to {directory}')
        return hash_id, ''
    return False, 'rTorrent hashid not found'


def get_progress(hash_id):
    server, _ = get_server()
    if not server:
        return -2, 'connection error'
    mainview = server.download_list("", "main")
    for tor in mainview:
        if tor.upper() == hash_id.upper():
            if server.d.complete(tor):
                return 100, 'finished'
            return int((server.d.bytes_done(tor) * 100) / server.d.size_bytes(tor)), 'OK'
    return -1, 'not found'


def get_files(hash_id):
    server, _ = get_server()
    if not server:
        return []

    mainview = server.download_list("", "main")
    for tor in mainview:
        if tor.upper() == hash_id.upper():
            size_files = server.d.size_files(tor)
            cnt = 0
            files = []
            while cnt < size_files:
                target = f"{tor}:f{cnt}"
                path = server.f.path(target)
                size = server.f.size_bytes(target)
                files.append({"path": path, "size": size})
                cnt += 1
            return files
    return []


def get_name(hash_id):
    server, version = get_server()
    if not server:
        return False

    mainview = server.download_list("", "main")
    for tor in mainview:
        if tor.upper() == hash_id.upper():
            retries = 5
            name = ''
            while retries:
                name = server.d.name(tor)
                if tor.upper() not in name:
                    break
                sleep(5)
                retries -= 1
            return name
    return False  # not found


def get_folder(hash_id):
    server, version = get_server()
    if not server:
        return False

    mainview = server.download_list("", "main")
    for tor in mainview:
        if tor.upper() == hash_id.upper():
            retries = 5
            name = ''
            while retries:
                name = get_directory(tor)
                if tor.upper() not in name:
                    break
                sleep(5)
                retries -= 1
            return name
    return False  # not found


# noinspection PyUnusedLocal
def remove_torrent(hash_id, remove_data=False):
    server, _ = get_server()
    if not server:
        return False

    mainview = server.download_list("", "main")
    for tor in mainview:
        if tor.upper() == hash_id.upper():
            return server.d.erase(tor)
    return False  # not found


def check_link():
    server, version = get_server()
    if not server:
        return "rTorrent login FAILED\nCheck debug log"
    return f"rTorrent login successful: rTorrent {version}"
