#  This file is part of Lazylibrarian.
#  Lazylibrarian is free software you can redistribute it and/or modify
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
import os
import re
import threading
import time
import traceback
import unicodedata
from base64 import b16encode, b32decode, b64encode
from hashlib import sha1, sha256
from urllib.parse import urlsplit

# noinspection PyBroadException
try:
    import magic
except Exception:  # magic might fail for multiple reasons
    magic = None

import logging

import requests
from bs4 import BeautifulSoup
from deluge_client import DelugeRPCClient

from lazylibrarian import (
    TIMERS,
    classes,
    database,
    deluge,
    nzbget,
    qbittorrent,
    rtorrent,
    sabnzbd,
    synology,
    transmission,
    utorrent,
)
from lazylibrarian.annas import annas_download, block_annas
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.cache import fetch_url
from lazylibrarian.common import get_user_agent, proxy_list
from lazylibrarian.config2 import CONFIG
from lazylibrarian.directparser import bok_grabs, bok_login, session_get
from lazylibrarian.download_client import check_contents, delete_task, seed_requirement
from lazylibrarian.filesystem import (
    DIRS,
    get_directory,
    make_dirs,
    path_isdir,
    path_isfile,
    remove_file,
    setperm,
    splitext,
    syspath,
)
from lazylibrarian.formatter import (
    clean_name,
    get_list,
    make_bytestr,
    make_unicode,
    md5_utf8,
    redact_url,
    sanitize,
    unaccented,
)
from lazylibrarian.ircbot import irc_query
from lazylibrarian.soulseek import SLSKD
from lazylibrarian.telemetry import record_usage_data
from lib.bencode import BencodeDecodeError, bdecode, bencode

from .magnet2torrent import magnet2torrent


def use_label(source, library):
    if source in ['DELUGERPC', 'DELUGEWEBUI']:
        labels = CONFIG['DELUGE_LABEL']
    elif source in ['TRANSMISSION', 'UTORRENT', 'RTORRENT', 'QBITTORRENT']:
        labels = CONFIG[f"{source}_LABEL"]
    elif source in ['SABNZBD']:
        labels = CONFIG['SAB_CAT']
    elif source in ['NZBGET']:
        labels = CONFIG['NZBGET_CATEGORY']
    else:
        labels = ''

    if not library or ',' not in labels:
        return labels

    labels = get_list(labels, ',')
    try:
        if library == 'eBook':
            return labels[0]
        if library == 'AudioBook':
            return labels[1]
        if library == 'magazine':
            return labels[2]
        if library == 'Comic':
            return labels[3]
    except IndexError:
        pass

    return ''


def irc_dl_method(bookid=None, dl_title=None, dl_url=None, library='eBook', provider: str = ''):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    resultfile = ''
    msg = ''
    try:
        source = provider
        logger.debug(f"Starting IRC Download for [{dl_title}]")
        fname = ""
        myprov = None

        for item in CONFIG.providers('IRC'):
            if item['NAME'] == provider or item['DISPNAME'] == provider:
                myprov = item
                break

        if not myprov:
            msg = f"{provider} server not found"
        else:
            t = threading.Thread(target=irc_query, name='irc_query', args=(myprov, dl_title, dl_title, dl_url, False,))
            t.start()
            t.join()

            resultfile = os.path.join(DIRS.CACHEDIR, "IRCCache", dl_title)
            fname = dl_title

        # noinspection PyTypeChecker
        download_id = sha1(bencode(dl_url + ':' + dl_title)).hexdigest()

        if path_isfile(resultfile):
            fname = sanitize(fname, is_folder_or_file=True)
            destdir = os.path.join(get_directory('Download'), fname)
            if not path_isdir(destdir):
                _ = make_dirs(destdir)

            destfile = os.path.join(destdir, fname)

            try:
                with open(destfile, 'wb') as bookfile, open(resultfile, 'rb') as sourcefile:
                    bookfile.write(sourcefile.read())
                setperm(destfile)
                remove_file(resultfile)
            except Exception as e:
                msg = f"{type(e).__name__} writing book to {destfile}, {e}"
                logger.error(msg)
                db.close()
                return False, msg

            logger.debug(f"File {dl_title} has been downloaded from {dl_url}")
            if library == 'eBook':
                db.action("UPDATE books SET status='Snatched' WHERE BookID=?", (bookid,))
            elif library == 'AudioBook':
                db.action("UPDATE books SET audiostatus='Snatched' WHERE BookID=?", (bookid,))
            db.action("UPDATE wanted SET status='Snatched', Source=?, DownloadID=? WHERE NZBurl=? and NZBtitle=?",
                      (source, download_id, dl_url, dl_title))
            record_usage_data(f'Download/IRC/{source}/Success')
            db.close()
            return True, ''
        cmd = 'UPDATE wanted SET status="Failed", Source=?, DownloadID=?, DLResult=? '
        cmd += 'WHERE NZBurl=? and NZBtitle=?'
        db.action(cmd, (source, download_id, msg, dl_url, dl_title))
        db.close()
        return False, msg
    except Exception:
        logger.error(f"Error in irc_dl_method: {traceback.format_exc()}")
        db.close()
        return False, msg


def nzb_dl_method(bookid=None, nzbtitle=None, nzburl=None, library='eBook', label=''):
    logger = logging.getLogger(__name__)
    source = ''
    download_id = ''

    if CONFIG.get_bool('NZB_DOWNLOADER_SABNZBD') and CONFIG['SAB_HOST']:
        source = "SABNZBD"
        if CONFIG['SAB_EXTERNAL_HOST']:
            # new method, download nzb data, write to file, send file to sab, delete file
            data, success = fetch_url(nzburl, raw=True)
            if not success:
                res = f"Failed to read nzb data for sabnzbd: {data}"
                logger.debug(res)
                download_id = ''
            else:
                logger.debug(f"Got {len(data)} bytes data")
                temp_filename = os.path.join(DIRS.CACHEDIR, "nzbfile.nzb")
                with open(syspath(temp_filename), 'wb') as f:
                    f.write(data)
                logger.debug("Data written to file")
                nzb_url = CONFIG['SAB_EXTERNAL_HOST']
                if not nzb_url.startswith('http'):
                    if CONFIG.get_bool('HTTPS_ENABLED'):
                        nzb_url = 'https://' + nzb_url
                    else:
                        nzb_url = 'http://' + nzb_url
                if CONFIG['HTTP_ROOT']:
                    nzb_url += '/' + CONFIG['HTTP_ROOT']
                nzb_url += '/nzbfile.nzb'
                logger.debug(f"nzb_url [{nzb_url}]")
                download_id, res = sabnzbd.sab_nzbd(nzbtitle, nzb_url, remove_data=False, library=library, label=label)
                # returns nzb_ids or False
                logger.debug(f"Sab returned {download_id}/{res}")
                # os.unlink(temp_filename)
                # logger.debug("Temp file deleted")
        else:
            download_id, res = sabnzbd.sab_nzbd(nzbtitle, nzburl, remove_data=False, library=library, label=label)
            # returns nzb_ids or False
        if download_id and CONFIG.get_bool('NZB_PAUSED'):
            _ = sabnzbd.sab_nzbd(nzbtitle, 'pause', False, None, download_id, library=library, label=label)

    if CONFIG.get_bool('NZB_DOWNLOADER_NZBGET') and CONFIG['NZBGET_HOST']:
        source = "NZBGET"
        data, success = fetch_url(nzburl, raw=True)
        if not success:
            res = f"Failed to read nzb data for nzbget: {data}"
            logger.debug(res)
            download_id = ''
        else:
            nzb = classes.NZBDataSearchResult()
            nzb.extraInfo.append(data)
            nzb.name = nzbtitle
            nzb.url = nzburl
            download_id, res = nzbget.send_nzb(nzb, library=library, label=label)
            if download_id and CONFIG.get_bool('NZB_PAUSED'):
                _ = nzbget.send_nzb(nzb, 'GroupPause', download_id)

    if CONFIG.get_bool('NZB_DOWNLOADER_SYNOLOGY') and CONFIG.get_bool('USE_SYNOLOGY') and \
            CONFIG['SYNOLOGY_HOST']:
        source = "SYNOLOGY_NZB"
        download_id, res = synology.add_torrent(nzburl)  # returns nzb_ids or False

    if CONFIG.get_bool('NZB_DOWNLOADER_BLACKHOLE'):
        source = "BLACKHOLE"
        nzbfile, success = fetch_url(nzburl, raw=True)
        if not success:
            res = f"Error fetching nzb from url [{nzburl}]: {nzbfile}"
            logger.warning(res)
            return False, res

        if nzbfile:
            nzbname = str(nzbtitle) + '.nzb'
            nzbpath = os.path.join(CONFIG['NZB_BLACKHOLEDIR'], nzbname)
            try:
                with open(syspath(nzbpath), 'wb') as f:
                    if isinstance(nzbfile, str):
                        nzbfile = nzbfile.encode('iso-8859-1')
                    f.write(nzbfile)
                logger.debug('NZB file saved to: ' + nzbpath)
                setperm(nzbpath)
                download_id = nzbname

            except Exception as e:
                res = f"{nzbpath} not writable, NZB not saved. {type(e).__name__}: {e}"
                logger.error(res)
                return False, res

    if not source:
        res = 'No NZB download method is enabled, check config.'
        logger.warning(res)
        return False, res

    if download_id:
        db = database.DBConnection()
        logger.debug('Nzbfile has been downloaded from ' + str(nzburl))
        if library == 'eBook':
            db.action("UPDATE books SET status='Snatched' WHERE BookID=?", (bookid,))
        elif library == 'AudioBook':
            db.action("UPDATE books SET audiostatus = 'Snatched' WHERE BookID=?", (bookid,))
        db.action("UPDATE wanted SET status='Snatched', Source=?, DownloadID=? WHERE NZBurl=?",
                  (source, download_id, nzburl))
        db.close()
        record_usage_data(f'Download/NZB/{source}/Success')
        return True, ''
    res = f'Failed to send nzb to @ <a href="{nzburl}">{source}</a>'
    logger.error(res)
    record_usage_data(f'Download/NZB/{source}/Failed')
    return False, res


def direct_dl_method(bookid=None, dl_title=None, dl_url=None, library='eBook', provider=''):
    logger = logging.getLogger(__name__)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)
    source = "DIRECT"
    logger.debug(f"Starting Direct Download from {provider} for [{dl_title}]")
    # is library actually auxinfo magazine date
    auxinfo = library if library not in ['eBook', 'AudioBook', 'Comic'] else ''
    if auxinfo:
        library = 'magazine'
    if provider == 'soulseek':
        slsk = SLSKD()
        if not slsk.slskd:
            return False, "Unable to connect to slskd, is it running?"
        try:
            slsk_username, slsk_dir = dl_url.split('^')
        except IndexError:
            msg = f"Failed to get username and dir from url {dl_url}"
            logger.debug(msg)
            return False, msg

        directory = json.loads(slsk_dir)
        queued = slsk.enqueue(slsk_username, directory)
        if not queued:
            return False, f'Unable to queue {dl_title}'

        wanted = [{'username': slsk_username, 'directory': directory}]
        hashid = sha1(bencode(dl_url)).hexdigest()
        db = database.DBConnection()
        try:
            res = slsk.download(wanted)
        except Exception as e:
            logger.error(f"slsk download error: {e}")
            res = None
        if res:
            if library == 'eBook':
                db.action("UPDATE books SET status='Snatched' WHERE BookID=?", (bookid,))
            elif library == 'AudioBook':
                db.action("UPDATE books SET audiostatus='Snatched' WHERE BookID=?", (bookid,))
            if auxinfo:  # magazine issue
                cmd = ("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, completed=? "
                       "WHERE NZBUrl=?")
                db.action(cmd, (source, dl_url, int(time.time()), dl_url))
            else:
                cmd = ("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, completed=? "
                       "WHERE BookID=? and NZBProv=?")
                db.action(cmd, (source, hashid, int(time.time()), bookid, provider))
            db.close()
            record_usage_data(f'Download/Direct/{provider}/Success')
            return True, ''

        if auxinfo:  # magazine issue
            cmd = ("UPDATE wanted SET status='Failed', dlresult=?, Source=?, DownloadID=?, completed=? "
                   "WHERE NZBUrl=?")
            db.action(cmd, ('SLSK download failed', source, hashid, int(time.time()), dl_url))
        else:
            cmd = ("UPDATE wanted SET status='Failed', dlresult=?, Source=?, DownloadID=?, completed=? "
                   "WHERE BookID=? and NZBProv=?")
            db.action(cmd, ('SLSK download failed', source, hashid, int(time.time()), bookid, provider))
        db.close()
        record_usage_data(f'Download/Direct/{provider}/Failed')
        return False, ''

    if provider == 'annas':
        count = TIMERS['ANNA_REMAINING']
        dl_limit = CONFIG.get_int('ANNA_DLLIMIT')
        if dl_limit and count <= 0:
            TIMERS['ANNA_REMAINING'] = 0
            block_annas(dl_limit)
            return False, f"Download limit {dl_limit} reached"

        title, extn = splitext(dl_title)
        folder = ''
        db = database.DBConnection()
        res = db.match('SELECT bookname from books WHERE bookid=?', (bookid,))
        if res and res['bookname']:
            folder = res['bookname']
        try:
            success, fname = annas_download(dl_url, folder, title, extn)
        except Exception as e:
            logger.error(f"Annas download error: {e}")
            success = False
            fname = ''

        if success:
            if library == 'eBook':
                db.action("UPDATE books SET status='Snatched' WHERE BookID=?", (bookid,))
            elif library == 'AudioBook':
                db.action("UPDATE books SET audiostatus='Snatched' WHERE BookID=?", (bookid,))
            if auxinfo:  # magazine issue
                cmd = ("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, completed=? "
                       "WHERE NZBUrl=?")
                db.action(cmd, (source, dl_url, int(time.time()), dl_url))
            else:
                cmd = ("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, completed=? "
                       "WHERE BookID=? and NZBProv=?")
                db.action(cmd, (source, dl_url, int(time.time()), bookid, provider))

            record_usage_data(f'Download/Direct/{provider}/Success')
            db.close()
            return True, ''

        if auxinfo:  # magazine issue
            cmd = ("UPDATE wanted SET status='Failed', dlresult=?, Source=?, DownloadID=?, completed=? "
                   "WHERE NZBUrl=?")
            db.action(cmd, (fname, source, dl_url, int(time.time()), dl_url))
        else:
            cmd = ("UPDATE wanted SET status='Failed', dlresult=?, Source=?, DownloadID=?, completed=? "
                   "WHERE BookID=? and NZBProv=?")
            db.action(cmd, (fname, source, dl_url, int(time.time()), bookid, provider))
        record_usage_data(f'Download/Direct/{provider}/Failed')
        db.close()
        return False, fname

    if provider == 'zlibrary':
        zlib = bok_login()
        if not zlib:
            return False, 'Login failed'

        count = TIMERS['BOK_TODAY']
        dl_limit = CONFIG.get_int('BOK_DLLIMIT')
        if count and count >= dl_limit:
            grabs, oldest = bok_grabs()
            # rolling 24hr delay if limit reached
            delay = oldest + 24 * 60 * 60 - time.time()
            res = f"Reached Daily download limit ({grabs}/{dl_limit})"
            BLOCKHANDLER.block_provider(provider, res, delay=delay)
            return False, res
        try:
            zlib_bookid, zlib_hash = dl_url.split('^')
        except IndexError:
            msg = f"Failed to get id and hash from url {dl_url}"
            logger.debug(msg)
            return False, msg

        hashid = sha1(bencode(dl_url)).hexdigest()
        db = database.DBConnection()
        try:
            filename, filecontent = zlib.downloadBook({"id": zlib_bookid, "hash": zlib_hash})
        except Exception as e:
            logger.error(f"Zlib download error: {e}")
            filename = None
        if not filename:
            logger.error(filecontent)
            if auxinfo:  # magazine issue
                cmd = ("UPDATE wanted SET status='Failed', dlresult=?, Source=?, DownloadID=?, completed=? "
                       "WHERE NZBUrl=?")
                db.action(cmd, (filecontent, source, hashid, int(time.time()), dl_url))
            else:
                cmd = ("UPDATE wanted SET status='Failed', dlresult=?, Source=?, DownloadID=?, completed=? "
                       "WHERE BookID=? and NZBProv=?")
                db.action(cmd, (filecontent, source, hashid, int(time.time()), bookid, provider))
            db.close()
            record_usage_data(f'Download/Direct/{provider}/Failed')
            return False, filecontent

        logger.debug(f"File download got {len(filecontent)} bytes for {filename}")
        basename = sanitize(dl_title, is_folder_or_file=True)
        # zlib sometimes includes the filetype as an extension in the title
        # strip from dl_title so we don't include extension in destdir, or twice in destfile
        basename, _ = splitext(basename)
        destdir = os.path.join(get_directory('Download'), basename)
        if not path_isdir(destdir):
            _ = make_dirs(destdir)
        _, extn = splitext(filename)
        destfile = os.path.join(destdir, basename + extn)
        if os.name == 'nt':  # Windows has max path length of 256
            destfile = '\\\\?\\' + destfile
        logger.debug(f"Saving as {destfile}")
        try:
            with open(destfile, "wb") as bookfile:
                bookfile.write(filecontent)
        except Exception as e:
            res = f"{type(e).__name__} writing book to {destfile}, {e}"
            if auxinfo:
                cmd = ("UPDATE wanted SET status='Failed', dlresult=?, Source=?, DownloadID=?, completed=? "
                       "WHERE NZBUrl=?")
                db.action(cmd, (res, source, hashid, int(time.time()), dl_url))
            else:
                cmd = ("UPDATE wanted SET status='Failed', dlresult=?, Source=?, DownloadID=?, completed=? "
                       "WHERE BookID=? and NZBProv=?")
                db.action(cmd, (res, source, hashid, int(time.time()), bookid, provider))
            db.close()
            record_usage_data(f'Download/Direct/{provider}/Failed')
            logger.error(res)
            return False, res

        logger.debug(f"File {dl_title} has been downloaded from z-library")
        setperm(destfile)
        if library == 'eBook':
            db.action("UPDATE books SET status='Snatched' WHERE BookID=?", (bookid,))
        elif library == 'AudioBook':
            db.action("UPDATE books SET audiostatus='Snatched' WHERE BookID=?", (bookid,))
        if auxinfo:
            cmd = ("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, completed=? "
                   "WHERE NZBUrl=?")
            db.action(cmd, (source, hashid, int(time.time()), dl_url))
        else:
            cmd = ("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, completed=? "
                   "WHERE BookID=? and NZBProv=?")
            db.action(cmd, (source, hashid, int(time.time()), bookid, provider))
        db.close()
        record_usage_data(f'Download/Direct/{provider}/Success')
        return True, ''

    # libgen...
    headers = {'Accept-encoding': 'gzip', 'User-Agent': get_user_agent()}
    dl_url = make_unicode(dl_url)
    s = requests.Session()
    proxies = proxy_list()
    if proxies:
        s.proxies.update(proxies)

    redirects = 0
    while redirects < 5:
        redirects += 1
        try:
            logger.debug(f"{redirects}: [{provider}] {headers}")
            r = session_get(s, dl_url, headers)
        except requests.exceptions.Timeout:
            res = f"Timeout fetching file from url: {dl_url}"
            logger.warning(res)
            return False, res
        except Exception as e:
            res = f"{type(e).__name__} fetching file from url: {dl_url}, {e}"
            logger.warning(res)
            return False, res

        if str(r.status_code) in ['502', '504']:
            time.sleep(2)
        elif not str(r.status_code).startswith('2'):
            res = f"Got a {r.status_code} response for {dl_url}"
            logger.debug(res)
            return False, res
        elif len(r.content) < 1000:
            res = f"Only got {len(r.content)} bytes for {dl_title}"
            logger.debug(res)
            return False, res
        elif 'application' in r.headers['Content-Type']:
            # application/octet-stream, application/epub+zip, application/x-mobi8-ebook etc.
            extn = ''
            basename = ''
            dl_title = dl_title.strip()
            if ' ' in dl_title:
                basename, extn = dl_title.rsplit(' ', 1)  # last word is often the extension - but not always...
            if extn and extn.lower() not in get_list(CONFIG['EBOOK_TYPE']):
                basename = ''
                extn = ''
            if not basename and '.' in dl_title:
                basename, extn = dl_title.rsplit('.', 1)
            if extn and extn.lower() not in get_list(CONFIG['EBOOK_TYPE']):
                basename = ''
                extn = ''
            if not basename and magic:
                try:
                    mtype = magic.from_buffer(r.content).upper()
                    logger.debug(f"magic reports {mtype}")
                except Exception as e:
                    logger.debug(f"{type(e).__name__} reading magic from {dl_title}, {e}")
                    mtype = ''
                if 'EPUB' in mtype:
                    extn = 'epub'
                elif 'MOBIPOCKET' in mtype:  # also true for azw and azw3, does it matter?
                    extn = 'mobi'
                elif 'PDF' in mtype:
                    extn = 'pdf'
                elif 'RAR' in mtype:
                    extn = 'cbr'
                elif 'ZIP' in mtype:
                    extn = 'cbz'
                basename = dl_title
            if not extn:
                logger.warning(f"Don't know the filetype for [{dl_title}]")
                basename = dl_title
            else:
                extn = extn.lower()
            if '/' in basename:
                basename = basename.split('/')[0]

            logger.debug(f"File download got {len(r.content)} bytes for {basename}")

            basename = sanitize(basename, is_folder_or_file=True)
            destdir = os.path.join(get_directory('Download'), basename)
            if not path_isdir(destdir):
                _ = make_dirs(destdir)

            try:
                hashid = dl_url.split("md5=")[1].split("&")[0]
            except IndexError:
                # noinspection PyTypeChecker
                hashid = sha1(bencode(dl_url)).hexdigest()

            destfile = os.path.join(destdir, basename + '.' + extn)

            if os.name == 'nt':  # Windows has max path length of 256
                destfile = '\\\\?\\' + destfile

            db = database.DBConnection()
            try:
                with open(syspath(destfile), 'wb') as bookfile:
                    bookfile.write(r.content)
                setperm(destfile)
                logger.debug(f"File {dl_title} has been downloaded from {dl_url}")
                if library == 'eBook':
                    db.action("UPDATE books SET status='Snatched' WHERE BookID=?", (bookid,))
                elif library == 'AudioBook':
                    db.action("UPDATE books SET audiostatus='Snatched' WHERE BookID=?", (bookid,))
                if auxinfo:  # magazine issue
                    cmd = ("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, completed=? "
                           "WHERE NZBUrl=?")
                    db.action(cmd, (source, hashid, int(time.time()), dl_url))
                else:
                    cmd = ("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, completed=? "
                           "WHERE BookID=? and NZBProv=?")
                    db.action(cmd, (source, hashid, int(time.time()), bookid, provider))
                db.close()
                record_usage_data(f'Download/Direct/{provider}/Success')
                return True, ''
            except Exception as e:
                res = f"{type(e).__name__} writing book to {destfile}, {e}"
                logger.error(res)
                if auxinfo:
                    cmd = ("UPDATE wanted SET status='Snatched', dlresult=?, Source=?, DownloadID=?, completed=? "
                           "WHERE NZBUrl=?")
                    db.action(cmd, (res, source, hashid, int(time.time()), dl_url))
                else:
                    cmd = ("UPDATE wanted SET status='Snatched', dlresult=?, Source=?, DownloadID=?, completed=? "
                           "WHERE BookID=? and NZBProv=?")
                    db.action(cmd, (res, source, hashid, int(time.time()), bookid, provider))
                db.close()
                record_usage_data(f'Download/Direct/{provider}/Failed')
                return False, res
        else:
            res = f"Got unexpected response type ({r.headers['Content-Type']}) for {dl_title}"
            logger.debug(res)
            # see if there is a redirect...
            redirect = False
            if 'text/html' in r.headers['Content-Type']:
                result, success = fetch_url(dl_url)
                if success:
                    newsoup = BeautifulSoup(result, 'html5lib')
                    data = newsoup.find_all('a')
                    link = None
                    for d in data:
                        link = d.get('href')
                        if link.startswith('get.php'):
                            break
                    if link:
                        dl_url = dl_url.rsplit('/', 1)[0] + '/' + link
                        logger.debug(f"File {dl_title} redirected to {dl_url}")
                        redirect = True
                if 'get.php' in dl_url:
                    redirect = True

            if not redirect:
                cache_location = os.path.join(DIRS.CACHEDIR, "HTMLCache")
                myhash = md5_utf8(dl_url)
                hashfilename = os.path.join(cache_location, myhash[0], myhash[1], myhash + ".html")
                with open(syspath(hashfilename), "wb") as cachefile:
                    cachefile.write(r.content)
                logger.debug(f"Saved error page: {hashfilename}")
                return False, res

    res = f'Failed to download file @ <a href="{dl_url}">{dl_url}</a>'
    logger.error(res)
    return False, res


def tor_dl_method(bookid=None, tor_title=None, tor_url=None, library='eBook', label='', provider=''):
    logger = logging.getLogger(__name__)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)
    download_id = False
    source = ''
    torrent = ''
    # a torrent the client already held before we asked for it is not ours to
    # delete, however this request turns out
    adopted = False
    # the downloader category we actually asked for, recorded so a later run can
    # tell our torrent from one that happens to share the id
    snatch_category = ''

    full_url = tor_url  # keep the url as stored in "wanted" table
    tor_url = make_unicode(tor_url)
    if 'magnet:?' in tor_url:
        # discard any other parameters and just use the magnet link
        tor_url = 'magnet:?' + tor_url.split('magnet:?')[1]
    else:
        # h = HTMLParser()
        # tor_url = h.unescape(tor_url)
        # HTMLParser is probably overkill, we only seem to get &amp;
        #
        tor_url = tor_url.replace('&amp;', '&')

        if '&file=' in tor_url:
            # torznab results need to be re-encoded
            # had a problem with torznab utf-8 encoded strings not matching
            # our utf-8 strings because of long/short form differences
            url, value = tor_url.split('&file=', 1)
            value = unicodedata.normalize('NFC', value)  # normalize to short form
            value = value.encode('unicode-escape')  # then escape the result
            value = make_unicode(value)  # ensure unicode
            value = value.replace(' ', '%20')  # and encode any spaces
            tor_url = url + '&file=' + value

        # strip url back to the .torrent as some sites add extra parameters
        if not tor_url.endswith('.torrent') and '.torrent' in tor_url:
            tor_url = tor_url.split('.torrent')[0] + '.torrent'

        headers = {'Accept-encoding': 'gzip', 'User-Agent': get_user_agent()}
        proxies = proxy_list()
        safe_url = redact_url(tor_url)

        try:
            logger.debug(f"Fetching {safe_url}")
            if tor_url.startswith('https') and CONFIG.get_bool('SSL_VERIFY'):
                r = requests.get(tor_url, headers=headers, timeout=90, proxies=proxies,
                                 verify=CONFIG['SSL_CERTS']
                                 if CONFIG['SSL_CERTS'] else True)
            else:
                r = requests.get(tor_url, headers=headers, timeout=90, proxies=proxies, verify=False)
            if str(r.status_code).startswith('2'):
                torrent = r.content
                content_type = r.headers.get('Content-Type', 'unknown')
                if not len(torrent):
                    res = f"Provider returned invalid torrent data for {safe_url}: empty response"
                    logger.warning(res)
                    return False, res
                if len(torrent) < 100:
                    res = (f"Provider returned invalid torrent data for {safe_url}: "
                           f"only got {len(torrent)} bytes")
                    logger.warning(res)
                    return False, res
                logger.debug(f"Got {len(torrent)} bytes ({content_type}) for {safe_url}")
            else:
                res = f"Got a {r.status_code} response for {safe_url}"
                logger.warning(res)
                return False, res

        except requests.exceptions.Timeout:
            res = f"Timeout fetching file from url: {safe_url}"
            logger.warning(res)
            return False, res
        except Exception as e:
            # some jackett providers redirect internally using http 301 to a magnet link
            # which requests can't handle, so throws an exception
            logger.debug(f"Requests exception: {redact_url(str(e))}")
            if "magnet:?" in str(e):
                tor_url = 'magnet:?' + str(e).split('magnet:?')[1].strip("'")
                logger.debug("Redirecting to magnet link")
            else:
                res = f"{type(e).__name__} fetching file from url: {safe_url}, {redact_url(str(e))}"
                logger.warning(res)
                return False, res

    # A provider that hands back an error page, an interstitial, or a
    # truncated file gives us something that looks like data but isn't a
    # torrent. Say so here rather than reporting it as a hashing failure, and
    # never pass it on to a downloader.
    cache_hash = ''
    if torrent:
        invalid = torrent_data_error(torrent)
        if invalid:
            res = f"Provider returned invalid torrent data for {tor_title}: {invalid}"
            logger.warning(res)
            logger.debug(f"url: {redact_url(tor_url)}, {len(torrent)} bytes")
            torrent = ''
            if not CONFIG.get_bool('TOR_DOWNLOADER_BLACKHOLE'):
                # the downloader may have better luck fetching it than we did,
                # but only if we can tell which torrent it should end up with
                cache_hash = hash_from_cache_url(tor_url)
            if not cache_hash:
                return False, res
            logger.debug(f"Sending url to the downloader, expecting hash {cache_hash}")

    if not torrent and not cache_hash and not tor_url.startswith('magnet:?'):
        res = "No magnet or data, cannot continue"
        logger.warning(res)
        return False, res

    if CONFIG.get_bool('TOR_DOWNLOADER_BLACKHOLE'):
        source = "BLACKHOLE"
        logger.debug(f"Sending {tor_title} to blackhole")
        tor_name = clean_name(tor_title).replace(' ', '_')
        if tor_url and tor_url.startswith('magnet'):
            hashid = calculate_torrent_hash(tor_url)
            if not hashid:
                hashid = tor_name
            if CONFIG.get_bool('TOR_CONVERT_MAGNET'):
                tor_name = 'meta-' + hashid + '.torrent'
                tor_path = os.path.join(CONFIG['TORRENT_DIR'], tor_name)
                result = magnet2torrent(tor_url, tor_path)
                if result is not False:
                    logger.debug(f"Magnet file saved as: {tor_path}")
                    download_id = hashid
            else:
                tor_name += '.magnet'
                tor_path = os.path.join(CONFIG['TORRENT_DIR'], tor_name)
                msg = ''
                try:
                    msg = 'Opening '
                    with open(syspath(tor_path), 'wb') as torrent_file:
                        msg += 'Writing '
                        if isinstance(torrent, str):
                            torrent = torrent.encode('iso-8859-1')
                        torrent_file.write(torrent)
                    msg += 'SettingPerm '
                    setperm(tor_path)
                    msg += 'Saved '
                    logger.debug(f"Magnet file saved: {tor_path}")
                    download_id = hashid
                except Exception as e:
                    res = f"Failed to write magnet to file: {type(e).__name__} {e}"
                    logger.warning(res)
                    logger.debug(f"Progress: {msg} Filename [{tor_path}]")
                    return False, res
        else:
            tor_name += '.torrent'
            tor_path = os.path.join(CONFIG['TORRENT_DIR'], tor_name)
            msg = ''
            try:
                msg = 'Opening '
                with open(syspath(tor_path), 'wb') as torrent_file:
                    msg += 'Writing '
                    if isinstance(torrent, str):
                        torrent = torrent.encode('iso-8859-1')
                    torrent_file.write(torrent)
                msg += 'SettingPerm '
                setperm(tor_path)
                msg += 'Saved '
                logger.debug(f"Torrent file saved: {tor_name}")
                download_id = source
            except Exception as e:
                res = f"Failed to write torrent to file: {type(e).__name__} {e}"
                logger.warning(res)
                logger.debug(f"Progress: {msg} Filename [{tor_path}]")
                return False, res

    else:
        hashid = cache_hash or calculate_torrent_hash(tor_url, torrent)
        if not hashid:
            res = "Unable to calculate torrent hash from url/data"
            logger.error(res)
            logger.debug(f"url: {redact_url(tor_url)}")
            logger.debug(f"data: {make_unicode(str(torrent[:50]))}")
            return False, res

        # the same seeding requirement whichever provider group served the
        # result: an rss feed can be a private tracker's too
        provider_options = {}
        seed_ratio, seed_duration = seed_requirement(provider)
        if seed_ratio:
            provider_options['seed_ratio'] = seed_ratio
        if seed_duration:
            provider_options['seed_duration'] = seed_duration

        if CONFIG.get_bool('TOR_DOWNLOADER_UTORRENT') and CONFIG['UTORRENT_HOST']:
            logger.debug(f"Sending {tor_title} to Utorrent")
            source = "UTORRENT"
            download_id, res = utorrent.add_torrent(tor_url, hashid, provider_options)  # returns hash or False
            if download_id:
                if CONFIG.get_bool('TORRENT_PAUSED'):
                    utorrent.pause_torrent(download_id)
                if not label:
                    label = use_label(source, library)
                if label:
                    utorrent.label_torrent(download_id, label)
                tor_title = utorrent.name_torrent(download_id)

        if CONFIG.get_bool('TOR_DOWNLOADER_RTORRENT') and CONFIG['RTORRENT_HOST']:
            logger.debug(f"Sending {tor_title} to rTorrent")
            source = "RTORRENT"
            if not torrent and tor_url.startswith('magnet:?'):
                logger.debug("Converting magnet to data for rTorrent")
                torrentfile = magnet2torrent(tor_url)
                if torrentfile:
                    with open(syspath(torrentfile), 'rb') as f:
                        torrent = f.read()
                    remove_file(torrentfile)
                if not torrent:
                    logger.debug("Unable to convert magnet")
            if torrent:
                logger.debug(f"Sending {tor_title} data to rTorrent")
                download_id, res = rtorrent.add_torrent(tor_title, hashid, data=torrent)
            else:
                logger.debug(f"Sending {tor_title} url to rTorrent")
                download_id, res = rtorrent.add_torrent(tor_url, hashid)  # returns hash or False
            if download_id:
                tor_title = rtorrent.get_name(download_id)

        if CONFIG.get_bool('TOR_DOWNLOADER_QBITTORRENT') and CONFIG['QBITTORRENT_HOST']:
            source = "QBITTORRENT"
            # resolved here rather than read from config inside the client: the
            # label can be a per library list, and the category we send is the
            # one we have to recognise the torrent by later. Kept local so it
            # cannot leak into another downloader's block.
            qb_label = label or use_label(source, library)
            if torrent:
                logger.debug(f"Sending {tor_title} data to qBittorrent")
                download_id, res, adopted = qbittorrent.add_file(torrent, hashid, tor_title,
                                                                 provider_options, label=qb_label)
            else:
                logger.debug(f"Sending {tor_title} url to qBittorrent")
                download_id, res, adopted = qbittorrent.add_torrent(tor_url, hashid,
                                                                    provider_options, label=qb_label)
            # qBittorrent files v2 and hybrid torrents under their truncated v2
            # hash, so the id it returns is not always the hash we calculated
            if download_id:
                snatch_category = qb_label
                # keep the name we already have if the client has none for us:
                # an empty title skips the content checks further down
                client_name = qbittorrent.get_name(download_id)
                if client_name:
                    tor_title = client_name

        if CONFIG.get_bool('TOR_DOWNLOADER_TRANSMISSION') and CONFIG['TRANSMISSION_HOST']:
            source = "TRANSMISSION"
            if not label:
                label = use_label(source, library)
            directory = CONFIG['TRANSMISSION_DIR']
            if label and not directory.endswith(label):
                directory = os.path.join(directory, label)

            if torrent:
                logger.debug(f"Sending {tor_title} data to Transmission:{directory}")
                # transmission needs b64encoded metainfo to be unicode, not bytes
                download_id, res, adopted = transmission.add_torrent(
                    None, directory=directory, metainfo=make_unicode(b64encode(torrent)),
                    provider_options=provider_options)
            else:
                logger.debug(f"Sending {tor_title} url to Transmission:{directory}")
                download_id, res, adopted = transmission.add_torrent(
                    tor_url, directory=directory,
                    provider_options=provider_options)  # returns id or False
            if download_id:
                # transmission returns its own int, but we store hashid instead
                download_id = hashid
                snatch_category = label
                if label and not adopted:
                    transmission.set_label(download_id, label)
                client_name = transmission.get_torrent_name(download_id)
                if client_name:
                    tor_title = client_name
                tor_folder = transmission.get_torrent_folder(download_id)
                tor_files = transmission.get_torrent_files(download_id)
                logger.debug(f"{tor_title}: Folder is {tor_folder}")
                filenames = []
                for entry in tor_files:
                    filenames.append(entry['name'])
                logger.debug(f"Filenames: {', '.join(filenames)}")
                in_subdir = True
                for fname in filenames:
                    if not fname.startswith(tor_title + os.sep):
                        in_subdir = False
                        break
                if filenames and not in_subdir:
                    if adopted:
                        # someone else is seeding this from where it already is
                        logger.debug(f"{tor_title}: existing torrent, leaving it in {tor_folder}")
                    else:
                        directory = os.path.join(tor_folder, tor_title)
                        logger.debug(f"{tor_title}: Moving torrent to {directory}")
                        transmission.move_torrent(download_id, directory)

        if CONFIG.get_bool('TOR_DOWNLOADER_SYNOLOGY') and CONFIG.get_bool('USE_SYNOLOGY') and \
                CONFIG['SYNOLOGY_HOST']:
            logger.debug(f"Sending {tor_title} url to Synology")
            source = "SYNOLOGY_TOR"
            download_id, res = synology.add_torrent(tor_url)  # returns id or False
            if download_id:
                tor_title = synology.get_name(download_id)
                if CONFIG.get_bool('TORRENT_PAUSED'):
                    synology.pause_torrent(download_id)

        if CONFIG.get_bool('TOR_DOWNLOADER_DELUGE') and CONFIG['DELUGE_HOST']:
            if not CONFIG['DELUGE_USER']:
                # no username, talk to the webui
                source = "DELUGEWEBUI"
                if torrent:
                    logger.debug(f"Sending {tor_title} data to Deluge")
                    download_id, res = deluge.add_torrent(tor_title, data=b64encode(torrent),
                                                          provider_options=provider_options)
                else:
                    logger.debug(f"Sending {tor_title} url to Deluge")
                    download_id, res = deluge.add_torrent(tor_url,
                                                          provider_options=provider_options)
                    # can be link or magnet, returns hash or False
                if download_id:
                    if not label:
                        label = use_label(source, library)
                    if label:
                        deluge.set_torrent_label(download_id, label)
                    result = deluge.get_torrent_status(download_id, {})
                    if 'name' in result:
                        tor_title = result['name']
                else:
                    return False, res
            else:
                # have username, talk to the daemon
                source = "DELUGERPC"
                client = DelugeRPCClient(CONFIG['DELUGE_HOST'],
                                         int(CONFIG['DELUGE_PORT']),
                                         CONFIG['DELUGE_USER'],
                                         CONFIG['DELUGE_PASS'],
                                         decode_utf8=True)
                try:
                    client.connect()
                    args = {"name": tor_title}
                    if tor_url.startswith('magnet'):
                        res = f"Sending {tor_title} magnet to DelugeRPC"
                        logger.debug(res)
                        download_id = client.call('core.add_torrent_magnet', tor_url, args)
                    elif torrent:
                        res = f"Sending {tor_title} data to DelugeRPC"
                        logger.debug(res)
                        download_id = client.call('core.add_torrent_file', tor_title,
                                                  b64encode(torrent), args)
                    else:
                        res = f"Sending {tor_title} url to DelugeRPC" % tor_title
                        logger.debug(res)
                        download_id = client.call('core.add_torrent_url', tor_url, args)
                    if download_id:
                        if CONFIG.get_bool('TORRENT_PAUSED'):
                            _ = client.call('core.pause_torrent', download_id)
                        if not label:
                            label = use_label(source, library)
                        if label:
                            _ = client.call('label.set_torrent', download_id, label.lower())
                        if "seed_ratio" in provider_options:
                            _ = client.call('core.set_torrent_stop_at_ratio', download_id, True)
                            _ = client.call('core.set_torrent_stop_ratio', download_id, provider_options["seed_ratio"])
                        result = client.call('core.get_torrent_status', download_id, {})
                        if 'name' in result:
                            tor_title = result['name']
                    else:
                        res += ' failed'
                        logger.error(res)
                        return False, res

                except Exception as e:
                    res = f"DelugeRPC failed {type(e).__name__} {e}"
                    logger.error(res)
                    return False, res

    if not source:
        res = 'No torrent download method is enabled, check config.'
        logger.warning(res)
        return False, res

    if download_id:
        db = database.DBConnection()
        # record how we got the torrent before anything can reject it, so that
        # delete_task knows whose data it would be deleting
        origin = 'adopted' if adopted else 'new'
        try:
            if tor_title:
                if make_unicode(download_id).upper() in make_unicode(tor_title).upper():
                    logger.warning(f"{source}: name contains hash, probably unresolved magnet")
                else:
                    tor_title = unaccented(tor_title, only_ascii=False)
                    # need to check against reject words list again as the name may have changed
                    # library = magazine eBook AudioBook to determine which reject list,
                    # but we can't easily do the per-magazine rejects
                    if library == 'magazine':
                        reject_list = get_list(CONFIG['REJECT_MAGS'], ',')
                    elif library == 'eBook':
                        reject_list = get_list(CONFIG['REJECT_WORDS'], ',')
                    elif library == 'AudioBook':
                        reject_list = get_list(CONFIG['REJECT_AUDIO'], ',')
                    elif library == 'Comic':
                        reject_list = get_list(CONFIG['REJECT_COMIC'], ',')
                    else:
                        logger.debug(f"Invalid library [{library}] in tor_dl_method")
                        reject_list = []

                    rejected = False
                    lower_title = tor_title.lower()
                    for word in reject_list:
                        if word in lower_title:
                            rejected = f"Rejecting torrent name {tor_title}, contains {word}"
                            logger.debug(rejected)
                            break
                    if not rejected:
                        rejected = check_contents(source, download_id, library, tor_title)
                    if rejected:
                        # Source and DownloadID go in even though this failed:
                        # delete_task looks the row up by them to find out
                        # whether the torrent was ours to delete
                        db.action("UPDATE wanted SET status='Failed',DLResult=?,Source=?,DownloadID=?,"
                                  "Origin=?,Category=? WHERE NZBurl=?",
                                  (rejected, source, download_id, origin, snatch_category, full_url))
                        if CONFIG.get_bool('DEL_FAILED'):
                            delete_task(source, download_id, True)
                        return False, rejected
                    logger.debug(f"{source} setting torrent name to [{tor_title}]")
                    db.action('UPDATE wanted SET NZBtitle=? WHERE NZBurl=?', (tor_title, full_url))

            if library == 'eBook':
                db.action("UPDATE books SET status='Snatched' WHERE BookID=?", (bookid,))
            elif library == 'AudioBook':
                db.action("UPDATE books SET audiostatus='Snatched' WHERE BookID=?", (bookid,))
            db.action("UPDATE wanted SET status='Snatched', Source=?, DownloadID=?, Origin=?, "
                      "Category=? WHERE NZBurl=?",
                      (source, download_id, origin, snatch_category, full_url))
            record_usage_data(f'Download/TOR/{source}/Success')
            db.close()
            return True, ''
        except Exception:
            logger.error(f"Error in tor_dl_method: {traceback.format_exc()}")
            db.close()

    res = f"Failed to send torrent to {source}"
    logger.error(res)
    record_usage_data(f'Download/TOR/{source}/Failed')
    return False, res


def torrent_data_error(data):
    """
    Check that a provider response really is a torrent file before we do
    anything with it. Providers hand back error pages, empty bodies and
    interstitial html often enough that "couldn't hash it" is a misleading
    thing to report.
    Returns an empty string if the data is a torrent, else a short reason. The
    reason is logged and kept in the history table, so it describes the shape
    of the response and never quotes it: an error body can carry an api key.
    """
    if not data:
        return "empty response"
    if isinstance(data, str):
        data = make_bytestr(data)
    if not data.startswith(b'd'):
        return f"not a bencoded dictionary ({len(data)} bytes)"
    try:
        decoded = bdecode(data)
    except (BencodeDecodeError, ValueError, IndexError, KeyError, RecursionError) as e:
        return f"invalid bencode, {type(e).__name__}"
    if not isinstance(decoded, dict):
        return f"bencoded {type(decoded).__name__}, not a torrent dictionary"

    info = decoded.get('info')
    if not isinstance(info, dict):
        return "no info dictionary"
    if 'name' not in info:
        return "info dictionary has no name"
    # v1 keeps its piece hashes in "pieces", v2 in a "file tree" (BEP 52), and
    # a hybrid torrent carries both. Anything with neither is not a torrent.
    if 'pieces' not in info and 'file tree' not in info:
        return "info dictionary has no pieces or file tree"
    return ''


# Caches that name the torrent file after its infohash. The hash in one of
# these urls is worth having as a fallback, but only for hosts we recognise:
# any other 40 character string in a url is just a 40 character string.
TORRENT_CACHE_HOSTS = ('btcache.me', 'itorrents.net', 'itorrents.org',
                       'torcache.net', 'torrage.info')


def hash_from_cache_url(url):
    """
    Return the v1 infohash a recognised torrent cache url is named after, or an
    empty string. Only used when the cache serves us something that isn't a
    torrent, so the downloader can be asked to fetch the url itself.
    """
    if not url:
        return ''
    url = make_unicode(url)
    if not isinstance(url, str):
        return ''
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        return ''
    if (parts.hostname or '').lower() not in TORRENT_CACHE_HOSTS:
        return ''
    name = parts.path.rsplit('/', 1)[-1]
    if not name.lower().endswith('.torrent'):
        return ''
    candidate = name[:-len('.torrent')]
    if not re.fullmatch(r'[0-9a-fA-F]{40}', candidate):
        return ''
    return candidate.lower()


MAGNET_BTIH = re.compile(r"urn:btih:([0-9a-fA-F]{40}|[A-Za-z2-7]{32})")
# A v2 magnet carries a multihash rather than a bare hash: 1220 is the prefix
# for sha256 (function 0x12) of length 32 (0x20), then the hash itself.
MAGNET_BTMH = re.compile(r"urn:btmh:1220([0-9a-fA-F]{64})")

# Clients identify a v2 torrent by the first 40 hex characters of its sha256
# infohash, so a v2 id is the same width as a v1 one.
V2_ID_LENGTH = 40


def torrent_info_hashes(data):
    """
    Return the (v1, v2) infohashes of torrent data, either of which may be ''.

    A v1 torrent has only the sha1 hash, a v2 torrent (BEP 52) only the
    sha256, and a hybrid torrent carries both over the same info dictionary.
    """
    info = bdecode(data)["info"]
    # noinspection PyTypeChecker
    encoded = bencode(info)
    is_v2 = info.get('meta version') == 2
    # a hybrid torrent is a v2 torrent that also keeps the v1 piece list
    v1 = sha1(encoded).hexdigest() if not is_v2 or 'pieces' in info else ''
    v2 = sha256(encoded).hexdigest() if is_v2 else ''
    return v1, v2


def calculate_torrent_hash(link, data=None):
    """
    Calculate the torrent hash from a magnet link or data. Returns empty string
    when it cannot create a torrent hash given the input data.

    Prefers the v1 hash where a torrent has one, including hybrid torrents:
    it is what most downloaders key on. Only a v2 only torrent, which has no
    v1 hash at all, gets the truncated sha256 that clients use as its id.
    """
    logger = logging.getLogger(__name__)
    link = make_unicode(link) if link else ''
    magnet = MAGNET_BTIH.search(link)
    if magnet:
        torrent_hash = magnet.group(1)
        if len(torrent_hash) == 32:
            # some indexers use the base32 form of the infohash
            torrent_hash = make_unicode(b16encode(b32decode(torrent_hash.upper())))
        torrent_hash = torrent_hash.lower()
        logger.debug(f"Torrent Hash: {torrent_hash}")
        return torrent_hash

    magnet = MAGNET_BTMH.search(link)
    if magnet:
        torrent_hash = magnet.group(1).lower()[:V2_ID_LENGTH]
        logger.debug(f"Torrent Hash (v2): {torrent_hash}")
        return torrent_hash

    if not data:
        logger.error("Cannot calculate torrent hash without magnet link or data")
        return ''

    invalid = torrent_data_error(data)
    if invalid:
        logger.error(f"Provider returned invalid torrent data: {invalid}")
        return ''

    try:
        v1, v2 = torrent_info_hashes(data)
    except (BencodeDecodeError, KeyError, TypeError, ValueError, RecursionError) as e:
        logger.error(f"Error calculating hash: {type(e).__name__} {e}")
        return ''

    torrent_hash = v1 or v2[:V2_ID_LENGTH]
    logger.debug(f"Torrent Hash: {torrent_hash}")
    return torrent_hash
