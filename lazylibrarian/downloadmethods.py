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
from hashlib import sha1

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
from lazylibrarian.download_client import check_contents, delete_task
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
    make_unicode,
    md5_utf8,
    sanitize,
    unaccented,
)
from lazylibrarian.ircbot import irc_query
from lazylibrarian.soulseek import SLSKD
from lazylibrarian.telemetry import record_usage_data
from lib.bencode import bdecode, bencode

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

        try:
            logger.debug(f"Fetching {tor_url}")
            if tor_url.startswith('https') and CONFIG.get_bool('SSL_VERIFY'):
                r = requests.get(tor_url, headers=headers, timeout=90, proxies=proxies,
                                 verify=CONFIG['SSL_CERTS']
                                 if CONFIG['SSL_CERTS'] else True)
            else:
                r = requests.get(tor_url, headers=headers, timeout=90, proxies=proxies, verify=False)
            if str(r.status_code).startswith('2'):
                torrent = r.content
                if not len(torrent):
                    res = f"Got empty response for {tor_url}"
                    logger.warning(res)
                    return False, res
                if len(torrent) < 100:
                    res = f"Only got {len(torrent)} bytes for {tor_url}"
                    logger.warning(res)
                    return False, res
                logger.debug(f"Got {len(torrent)} bytes for {tor_url}")
            else:
                res = f"Got a {r.status_code} response for {tor_url}"
                logger.warning(res)
                return False, res

        except requests.exceptions.Timeout:
            res = f"Timeout fetching file from url: {tor_url}"
            logger.warning(res)
            return False, res
        except Exception as e:
            # some jackett providers redirect internally using http 301 to a magnet link
            # which requests can't handle, so throws an exception
            logger.debug(f"Requests exception: {e}")
            if "magnet:?" in str(e):
                tor_url = 'magnet:?' + str(e).split('magnet:?')[1].strip("'")
                logger.debug(f"Redirecting to {tor_url}")
            else:
                res = f"{type(e).__name__} fetching file from url: {tor_url}, {e}"
                logger.warning(res)
                return False, res

    if not torrent and not tor_url.startswith('magnet:?'):
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
        hashid = calculate_torrent_hash(tor_url, torrent)
        if not hashid:
            res = "Unable to calculate torrent hash from url/data"
            logger.error(res)
            logger.debug(f"url: {tor_url}")
            logger.debug(f"data: {make_unicode(str(torrent[:50]))}")
            return False, res

        provider_options = {}
        if provider:
            for item in CONFIG.providers('TORZNAB'):
                if item['NAME'] == provider or item['DISPNAME'] == provider or item['HOST'] == provider:
                    seed_ratio = item.get_item("SEED_RATIO").value
                    if seed_ratio:
                        provider_options['seed_ratio'] = seed_ratio
                    seed_duration = item.get_item("SEED_DURATION").value
                    if seed_duration:
                        provider_options['seed_duration'] = seed_duration
                    break

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
            if torrent:
                logger.debug(f"Sending {tor_title} data to qBittorrent")
                status, res = qbittorrent.add_file(torrent, hashid, tor_title, provider_options)
                # returns True or False
            else:
                logger.debug(f"Sending {tor_title} url to qBittorrent")
                status, res = qbittorrent.add_torrent(tor_url, hashid, provider_options)  # returns True or False
            if status:
                download_id = hashid
                tor_title = qbittorrent.get_name(hashid)

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
                download_id, res = transmission.add_torrent(None, directory=directory,
                                                            metainfo=make_unicode(b64encode(torrent)),
                                                            provider_options=provider_options)
            else:
                logger.debug(f"Sending {tor_title} url to Transmission:{directory}")
                download_id, res = transmission.add_torrent(tor_url, directory=directory,
                                                            provider_options=provider_options)  # returns id or False
            if download_id:
                # transmission returns its own int, but we store hashid instead
                download_id = hashid
                if label:
                    transmission.set_label(download_id, label)
                tor_title = transmission.get_torrent_name(download_id)
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
                        db.action("UPDATE wanted SET status='Failed',DLResult=? WHERE NZBurl=?",
                                  (rejected, full_url))
                        if CONFIG.get_bool('DEL_FAILED'):
                            delete_task(source, download_id, True)
                        return False, rejected
                    logger.debug(f"{source} setting torrent name to [{tor_title}]")
                    db.action('UPDATE wanted SET NZBtitle=? WHERE NZBurl=?', (tor_title, full_url))

            if library == 'eBook':
                db.action("UPDATE books SET status='Snatched' WHERE BookID=?", (bookid,))
            elif library == 'AudioBook':
                db.action("UPDATE books SET audiostatus='Snatched' WHERE BookID=?", (bookid,))
            db.action("UPDATE wanted SET status='Snatched', Source=?, DownloadID=? WHERE NZBurl=?",
                      (source, download_id, full_url))
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


def calculate_torrent_hash(link, data=None):
    """
    Calculate the torrent hash from a magnet link or data. Returns empty string
    when it cannot create a torrent hash given the input data.
    """
    logger = logging.getLogger(__name__)
    try:
        torrent_hash = re.findall(r"urn:btih:(\w{32,40})", link)[0]
        if len(torrent_hash) == 32:
            torrent_hash = b16encode(b32decode(torrent_hash)).lower()
    except (re.error, IndexError, TypeError):
        if data:
            try:
                # noinspection PyUnresolvedReferences
                info = bdecode(data)["info"]
                # noinspection PyTypeChecker
                torrent_hash = sha1(bencode(info)).hexdigest()
            except Exception as e:
                logger.error(f"Error calculating hash: {e}")
                return ''
        else:
            logger.error("Cannot calculate torrent hash without magnet link or data")
            return ''
    logger.debug(f"Torrent Hash: {torrent_hash}")
    return torrent_hash
