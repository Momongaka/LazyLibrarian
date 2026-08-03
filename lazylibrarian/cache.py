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

import abc
import http.client
import itertools
import json
import logging
import os
import shutil
import time
from abc import ABC
from enum import Enum
from http.client import responses
from typing import Any
from xml.etree import ElementTree

import requests

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.common import get_user_agent, proxy_list
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import (
    DIRS,
    listdir,
    path_isdir,
    path_isfile,
    remove_file,
    splitext,
    syspath,
)
from lazylibrarian.formatter import (
    check_int,
    make_bytestr,
    make_unicode,
    md5_utf8,
    plural,
    seconds_to_midnight,
    thread_name,
)


class ImageType(Enum):
    """ Types of images we cache in separate dirs """
    BOOK = 'book'
    AUTHOR = 'author'
    MAG = 'magazine'
    COMIC = 'comic'
    TEST = 'test'


service_blocked = ['GoodReads', 'LibraryThing', 'Google', 'OpenLibrary', 'HardCover', 'DNB', 'RanobeDB', 'ISBN']


def gr_api_sleep():
    time_now = time.time()
    delay = time_now - lazylibrarian.TIMERS['LAST_GR']
    if delay < 1.0:
        sleep_time = 1.0 - delay
        lazylibrarian.TIMERS['SLEEP_GR'] += sleep_time
        cachelogger = logging.getLogger('special.cache')
        cachelogger.debug(f"GoodReads sleep {sleep_time:.3f}, total {lazylibrarian.TIMERS['SLEEP_GR']:.3f}")
        time.sleep(sleep_time)
    lazylibrarian.TIMERS['LAST_GR'] = time_now


def init_hex_caches() -> bool:
    """ Initialize the directory structure for each of the caches that use a two-layer dir structure for efficiency.
    Returns Success
    """
    logger = logging.getLogger()
    ok = True
    caches = ["WorkCache"]  # This one doesn't have its own handler class
    for cache in [HTMLCacheRequest, JSONCacheRequest, XMLCacheRequest]:
        caches.append(cache.cachedir_name())
    for item in caches:
        pth = DIRS.get_cachedir(item)
        subdirs = itertools.product("0123456789abcdef", repeat=2)
        for i, j in subdirs:
            cachelocation = os.path.join(pth, i, j)
            isok, msg = DIRS.ensure_dir_is_writeable(cachelocation)
            if not isok:
                logger.error(msg)
                ok = False
    return ok


def fetch_url(url: str, headers: dict | None = None, retry=True, timeout=True,
              raw: bool = False) -> tuple[str | bytes, bool]:
    """ Return the result of fetching a URL and True if success
        Otherwise return error message and False
        Return data as raw/bytes, if raw == True
        Default to unicode, need to set raw=True for images/data
        Allow one retry on timeout by default """
    logger = logging.getLogger(__name__)
    cachelogger = logging.getLogger('special.cache')
    http.client.HTTPConnection.debuglevel = 1 if lazylibrarian.REQUESTSLOG else 0
    # for key in logging.Logger.manager.loggerDict:
    #     print(key)
    logging.getLogger('chardet').setLevel(logging.CRITICAL)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)

    url = make_unicode(url)

    for blk in service_blocked:
        if blk in url and BLOCKHANDLER.is_blocked(blk.split('.')[0]):
            return 'Blocked', False

    if headers is None:
        # some sites insist on having a user-agent, default is to add one
        # if you don't want any headers, pass headers={}
        headers = {'User-Agent': get_user_agent()}

    proxies = proxy_list()

    # jackett query all indexers needs a longer timeout
    # /torznab/all/api?q=  or v2.0/indexers/all/results/torznab/api?q=
    if timeout:
        if '/torznab/' in url and ('/all/' in url or '/aggregate/' in url):
            timeout = CONFIG.get_int('HTTP_EXT_TIMEOUT')
        else:
            timeout = CONFIG.get_int('HTTP_TIMEOUT')

    req_kwargs = {}
    if timeout:
        req_kwargs["timeout"] = timeout
    if proxies:
        req_kwargs["proxies"] = proxies
    verify = False
    if url.startswith('https') and CONFIG.get_bool('SSL_VERIFY'):
        verify = True
        if CONFIG['SSL_CERTS']:
            verify = CONFIG['SSL_CERTS']
    try:
        r = requests.get(url, verify=verify, headers=headers, **req_kwargs)
    except requests.exceptions.TooManyRedirects as e:
        # This is to work around an oddity (bug??) with verified https goodreads requests
        # Goodreads sometimes redirects back to the same page in a loop using code 301,
        # and after a variable number of tries it might then return 200
        # but if it takes more than 30 loops the requests library stops trying
        # Retrying with verify off seems to clear it
        if not retry:
            logger.error(f"fetch_url: TooManyRedirects getting response from {url}")
            return f"TooManyRedirects {str(e)}", False
        logger.debug(f"Retrying - got TooManyRedirects on {url}")
        try:
            r = requests.get(url, verify=False, headers=headers, **req_kwargs)
            logger.debug(f"TooManyRedirects retry status code {r.status_code}")
        except Exception as e:
            return f"Exception {type(e).__name__}: {str(e)}", False
    except requests.exceptions.Timeout as e:
        if not retry:
            logger.error(f"fetch_url: Timeout getting response from {url}")
            return f"Timeout {str(e)}", False
        logger.debug(f"fetch_url: retrying - got timeout on {url}")
        try:
            r = requests.get(url, verify=verify, headers=headers, **req_kwargs)
        except Exception as e:
            return f"Exception {type(e).__name__}: {str(e)}", False
    except Exception as e:
        return f"Exception {type(e).__name__}: {str(e)}", False

    if r.status_code == 202:
        h = r.headers
        cachelogger.debug(f"{h}")
        logger.debug(f"{r.status_code} {len(r.content)}, {h.get('X-Cache')}")
        r = requests.get(url, verify=verify, headers=headers, **req_kwargs)
        logger.debug(f"Retry: {r.status_code} {len(r.content)}, {h.get('X-Cache')}")
    if r.status_code == 200:
        if raw:
            return r.content, True
        return r.text, True

    # we got an error response...
    # noinspection PyBroadException
    try:
        source = r.json()
        msg = source['error']['message']
    except Exception:
        msg = f"Error {r.status_code}"

    if '503' in msg:
        to_block = ''
        for blk in service_blocked:
            if blk in url:
                to_block = blk
                break
        if to_block:
            delay = 10
            logger.debug(f'Request denied, {r.status_code}, blocking {to_block} for {delay} seconds')
            BLOCKHANDLER.replace_provider_entry(to_block, delay, msg)
        else:
            logger.debug(f"Error {r.status_code} url={url}")

    elif 'googleapis' in url or 'google.com/search?q=ISBN' in url:
        prov = 'googleapis' if 'googleapis' in url else 'ISBN'
        if 'Limit Exceeded' in msg:
            # how long until midnight Pacific Time when google reset the quotas
            delay = seconds_to_midnight() + 28800  # PT is 8hrs behind UTC
            if delay > 86400:
                delay -= 86400  # no roll-over to next day
        elif r.status_code == 429:  # too many requests
            delay = 60
        else:
            # might be forbidden for a different reason where midnight might not matter
            # eg "Cannot determine user location for geographically restricted operation"
            delay = 3600

        logger.debug(f'Request denied, {r.status_code}, blocking {prov} for {delay} seconds: {msg}')
        BLOCKHANDLER.replace_provider_entry(prov, delay, msg)
    else:
        logger.debug(f"Error {r.status_code} url={url}")

    msg = responses.get(r.status_code, r.text)
    return f"Response status {r.status_code}: {msg}", False


def cache_img(img_type: ImageType, img_id: str, img_url: str, refresh=False) -> tuple[str, bool, bool]:
    """ Cache the image from the given filename or URL in the local images cache
        linked to the id.
        On success, return the link to the cached file, True, was_in_cache
        On error, return message, False, False """

    logger = logging.getLogger(__name__)
    logging.getLogger('urllib3.connectionpool').setLevel(logging.CRITICAL)
    had_cache = False
    cachefile = DIRS.get_cachefile(img_type.value, f"{img_id}.jpg")
    link = f'cache/{img_type.value}/{img_id}.jpg'
    if path_isfile(cachefile):
        if not refresh:  # overwrite any cached image
            cachelogger = logging.getLogger('special.cache')
            cachelogger.debug(f"Cached {img_type.name} image exists {cachefile}")
            return link, True, True
        had_cache = True

    if img_url.startswith('http'):
        result, success = fetch_url(img_url, raw=True)
        if success:
            try:
                with open(syspath(cachefile), 'wb') as img:
                    img.write(result)
                return link, True, False
            except Exception as e:
                logger.error(f"{type(e).__name__} writing image to {cachefile}, {str(e)}")
                logger.error(f"Image url: {img_url}")
                return str(e), False, False
        return result, False, False

    if not path_isfile(img_url) and img_url.endswith('.jpg'):
        # icrawler might give us jpg or png
        img_url = f"{img_url[:-4]}.png"
    if path_isfile(img_url):
        try:
            shutil.copyfile(img_url, cachefile)
            return link, True, had_cache
        except Exception as e:
            logger.error(f"{type(e).__name__} copying image to {cachefile}, {str(e)}")
            return str(e), False, False
    msg = f"No file [{img_url}]"
    logger.debug(msg)
    return msg, False, False


def gr_xml_request(my_url, use_cache=True, expire=True) -> tuple[Any, bool]:
    # respect goodreads api limit
    result, in_cache = XMLCacheRequest(url=my_url, use_cache=use_cache, expire=expire).get_cached_request()
    return result, in_cache


def json_request(my_url, use_cache=True, expire=True) -> tuple[Any, bool]:
    result, in_cache = JSONCacheRequest(url=my_url, use_cache=use_cache, expire=expire).get_cached_request()
    return result, in_cache


def html_request(my_url, use_cache=True, expire=True) -> tuple[Any, bool]:
    result, in_cache = HTMLCacheRequest(url=my_url, use_cache=use_cache, expire=expire).get_cached_request()
    return result, in_cache


class CacheRequest(ABC):
    """ Handle cache requests for LazyLibrarian. Use a concrete subclass of this """

    def __init__(self, url: str, use_cache: bool, expire: bool):
        self.url = url
        self.use_cache = use_cache
        self.expire = expire
        self.logger = logging.getLogger()
        self.cachelogger = logging.getLogger('special.cache')

    @classmethod
    @abc.abstractmethod
    def name(cls) -> str:
        """ Return the name of the cache, such as XML, HTML or JSON """

    @classmethod
    def cachedir_name(cls) -> str:
        return f"{cls.name()}Cache"

    @abc.abstractmethod
    def read_from_cache(self, hashfilename: str) -> tuple[str, bool]:
        """ Read the source from cache """

    def fetch_data(self) -> tuple[str, bool]:
        """ Fetch the data; called if it's not in the cache """
        return fetch_url(self.url, headers=None)

    @abc.abstractmethod
    def load_from_result_and_cache(self, result: str, filename: str, docache: bool) -> tuple[str, bool]:
        """ Load the value from result and store it in cache if docache is True """

    def get_cached_request(self) -> tuple[Any, bool]:
        # hashfilename = hash of url
        # if hashfilename exists in cache and isn't too old, return its contents
        # if not, read url and store the result in the cache
        # return the result, and boolean True if source was cache
        cache_location = DIRS.get_cachedir(self.cachedir_name())
        hashfilename, myhash = self.get_hashed_filename(cache_location)
        # CACHE_AGE is in days, so get it to seconds
        expire_older_than = CONFIG.get_int('CACHE_AGE') * 24 * 60 * 60 if self.expire else 0
        valid_cache = self.is_in_cache(expire_older_than, hashfilename, myhash)

        if valid_cache:
            lazylibrarian.CACHE_HIT += 1
            self.cachelogger.debug(f"CacheHandler: Returning CACHED response {hashfilename} for {self.url}")
            source, ok = self.read_from_cache(hashfilename)
            if not ok:
                self.logger.debug(f"CacheHandler: Failed to read {hashfilename} for {self.url}")
                return None, False
        else:
            lazylibrarian.CACHE_MISS += 1
            for blk in service_blocked:
                if blk in self.url and BLOCKHANDLER.is_blocked(blk):
                    return None, False

            result, success = self.fetch_data()
            if success:
                self.cachelogger.debug(f"CacheHandler: Storing {self.name()} {myhash} {len(result)} bytes for {self.url}")
                source, result = self.load_from_result_and_cache(result, hashfilename, expire_older_than)
                self.cachelogger.debug(result)
            elif '404' in result or '202' in result:  # don't block on "not found" or "accepted"
                return None, False
            else:
                msg = f"Got error response for {self.url}: {result.split('<')[0]}"
                self.logger.debug(msg)
                to_block = ''
                for blk in service_blocked:
                    if blk.lower() in self.url:
                        to_block = blk
                        break
                if to_block:
                    delay = 30
                    self.logger.debug(f'Blocking {to_block} for {delay} seconds')
                    BLOCKHANDLER.replace_provider_entry(to_block, delay, msg)
                return None, False
        return source, valid_cache

    def is_in_cache(self, expiry: int, hashfilename: str, myhash: str) -> bool:
        if self.use_cache and path_isfile(hashfilename):
            file_size = os.stat(hashfilename).st_size
            cache_modified_time = os.stat(hashfilename).st_mtime
            time_now = time.time()
            if not file_size or (self.expire and cache_modified_time < time_now - expiry):
                # Cache entry is empty or too old, delete it
                cachelogger = logging.getLogger('special.cache')
                cachelogger.debug(f"Expiring {myhash}")
                os.remove(syspath(hashfilename))
                return False
            return True
        return False

    def get_hashed_filename(self, cache_location: str) -> tuple[str, str]:
        myhash = md5_utf8(self.url)
        hashfilename = os.path.join(cache_location, myhash[0], myhash[1], f"{myhash}.{self.name().lower()}")
        return hashfilename, myhash


class XMLCacheRequest(CacheRequest):
    @classmethod
    def name(cls) -> str:
        return "XML"

    def read_from_cache(self, hashfilename: str) -> tuple[str | None, bool]:
        with open(syspath(hashfilename), "rb") as cachefile:
            result = cachefile.read()
        source = None
        if result and result.startswith(b'<?xml'):
            try:
                source = ElementTree.fromstring(result)
            except UnicodeEncodeError:
                # seems sometimes the page contains utf-16 but the header says it's utf-8
                try:
                    result = result.decode('utf-16').encode('utf-8')
                    source = ElementTree.fromstring(result)
                except (ElementTree.ParseError, UnicodeEncodeError, UnicodeDecodeError):
                    self.logger.error(f"Error parsing xml from {hashfilename}")
                    source = None
            except ElementTree.ParseError:
                self.logger.error(f"Error parsing xml from {hashfilename}")
                source = None
        if source is None:
            self.logger.error(f"Error reading xml from {hashfilename}")
            # normally delete bad data, but keep for inspection if debug logging cache
            if not self.cachelogger.isEnabledFor(logging.DEBUG):
                remove_file(hashfilename)
            return None, False
        return str(source), True

    def fetch_data(self) -> tuple[str, bool]:
        gr_api_sleep()
        headers = {'User-Agent': get_user_agent(), 'Accept': 'application/xml', 'Accept-Language': 'en-US,en;q=0.5'}
        return fetch_url(self.url, raw=True, headers=headers)

    def load_from_result_and_cache(self, result: str, filename: str, docache: bool) -> tuple[str | None, bool]:
        source = None
        result = make_bytestr(result)
        if not result:
            self.cachelogger.debug("No result returned")
        else:
            if not result.startswith(b'<?xml'):
                self.cachelogger.debug(f"{len(result)} bytes is not xml")
            else:
                try:
                    source = ElementTree.fromstring(result)
                    if not docache:
                        self.cachelogger.debug(f"Returning {len(source)} bytes xml uncached")
                        return str(source), False
                except UnicodeEncodeError:
                    # sometimes we get utf-16 data labelled as utf-8
                    try:
                        result = result.decode('utf-16').encode('utf-8')
                        source = ElementTree.fromstring(result)
                        if not docache:
                            self.cachelogger.debug(f"Returning {len(source)} bytes xml uncached")
                            return str(source), False
                    except (ElementTree.ParseError, UnicodeEncodeError, UnicodeDecodeError):
                        self.logger.error(f"Error parsing xml from {self.url}")
                        source = None
                except ElementTree.ParseError:
                    self.logger.error(f"Error parsing xml from {self.url}")
                    source = None

        if source is not None:
            try:
                with open(syspath(filename), "wb") as cachefile:
                    cachefile.write(result)
                    self.cachelogger.debug(f"Cached {len(result)} bytes xml {filename}")
            except Exception as e:
                self.logger.error(f"Exception {e} writing {filename}")
                return str(source), False
        else:
            self.logger.error(f"Error getting xml data from {self.url}")
            if result:
                self.logger.error(f"Result: {result[:80]}")
                try:
                    with open(syspath(f"{filename}.err"), "wb") as cachefile:
                        cachefile.write(result)
                        self.logger.error(f"Cached {len(result)} bytes {filename}.err")
                except Exception as e:
                    self.logger.error(f"Exception {e} writing {filename}.err")
            return None, False
        return str(source), True


class HTMLCacheRequest(CacheRequest):
    @classmethod
    def name(cls) -> str:
        return "HTML"

    def read_from_cache(self, hashfilename: str) -> tuple[bytes, bool]:
        try:
            with open(syspath(hashfilename), "rb") as cachefile:
                source = cachefile.read()
            return source, True
        except Exception as e:
            self.logger.error(f"Exception {e} reading {hashfilename}")
            return b'', False


    def load_from_result_and_cache(self, result: str, filename, docache) -> tuple[str, bool]:
        source = make_bytestr(result)
        try:
            with open(syspath(filename), "wb") as cachefile:
                cachefile.write(source)
            return source, True
        except Exception as e:
            self.logger.error(f"Exception {e} writing {filename}")
            return source, False


class JSONCacheRequest(CacheRequest):
    @classmethod
    def name(cls) -> str:
        return "JSON"

    def read_from_cache(self, hashfilename: str) -> tuple[str | None, bool]:
        try:
            try:
                with open(hashfilename) as f:
                    source = json.load(f)
            finally:
                f.close()
        except ValueError:
            self.logger.error(f"Error decoding json from {hashfilename}")
            # normally delete bad data, but keep for inspection if debug logging cache
            # if not self.cachelogger.isEnabledFor(logging.DEBUG):
            remove_file(hashfilename)
            return None, False
        return source, True

    def load_from_result_and_cache(self, result: str, filename: str, docache) -> tuple[str | None, bool]:
        try:
            source = json.loads(result)
            if not docache:
                return source, False
        except Exception as e:
            self.logger.error(f"{type(e).__name__} decoding json from {self.url}")
            self.logger.debug(f"{e} : {result}")
            return None, False
        try:
            with open(filename, "w") as outfile:
                json.dump(source, outfile)
            return source, True
        except Exception as e:
            self.logger.error(f"Exception {e} writing {outfile}")
            return source, False

def clean_cache():
    """ Remove unused files from the cache - delete if expired or unused.
        Check JSONCache  WorkCache  XMLCache  SeriesCache Author  Book  Magazine  Comic  IRC
        Check covers and authorimages etc. referenced in the database exist
        and change database entry if missing, expire old pastissues table entries """

    threadname = thread_name()
    if "Thread" in threadname:
        thread_name("CLEANCACHE")

    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    result = []
    try:
        logger.debug("Storing start time for CLEANCACHE")
        db.upsert("jobs", {'Start': time.time()}, {'Name': 'CLEANCACHE'})
        result = [
            # Remove files that are too old from cache directories
            FileExpirer("IRCCache", False, check_int(lazylibrarian.IRC_CACHE_EXPIRY, 0)).clean(),
            FileExpirer("JSONCache", True, CONFIG.get_int('CACHE_AGE') * 24 * 60 * 60).clean(),
            FileExpirer("XMLCache", True, CONFIG.get_int('CACHE_AGE') * 24 * 60 * 60).clean(),

            # Remove files not referenced by relevant item in the DB
            OrphanCleaner("WorkCache", True, db, 'BookID', 'books', '%s', True).clean(),
            OrphanCleaner("SeriesCache", False, db, 'SeriesID', 'series', '%s', True).clean(),
            OrphanCleaner("magazine", False, db, 'cover', 'issues', 'cache/magazine/%s', False).clean(),

            # Remove files no longer referenced by the database
            UnreferencedCleaner("author", "Author cache", db, 'AuthorImg', 13,
                                "authors where instr(AuthorImg, 'cache/author/') = 1").clean(),
            UnreferencedCleaner("book", "Book cache", db, 'BookImg', 11,
                                "books where instr(BookImg, 'cache/book/') = 1").clean(),

            # At this point there should be no more .jpg files in the root of the cachedir
            # Any that are still there are for books/authors deleted from database
            ExtensionCleaner("root", ".jpg").clean(),

            # Verify the cover images referenced in the database are present, replace if not
            DBCleaner("book", "Cover", db, "books", "BookImg", "BookName", "BookID", 'images/nocover.png').clean(),
            DBCleaner("author", "Image", db, "authors", "AuthorImg", "AuthorName", "AuthorID",
                      'images/nophoto.png').clean(),
        ]

        expiry = CONFIG.get_int('CACHE_AGE')
        if expiry:
            time_now = time.time()
            too_old = time_now - (expiry * 24 * 60 * 60)
            # delete any pastissues table entries that are too old
            count = db.match('SELECT COUNT(*) as counter from pastissues')
            if count:
                total = count['counter']
            else:
                total = 0

            count = db.match("SELECT COUNT(*) as counter from pastissues WHERE Added>0 and Added<?", (too_old,))
            if count:
                old = count['counter']
            else:
                old = 0
            db.action("DELETE from pastissues WHERE Added>0 and Added<?", (too_old,))
            msg = f"Cleaned {old} old pastissues, kept {total - old}"
            result.append(msg)
            logger.debug(msg)
    except Exception as e:
        logger.error(str(e))

    logger.debug("Storing finish time for CLEANCACHE")
    db.upsert("jobs", {'Finish': time.time()}, {'Name': 'CLEANCACHE'})
    db.close()
    thread_name(threadname)
    return result


class CacheCleaner(ABC):
    def __init__(self, basedir: str):
        self.logger: logging.Logger = logging.getLogger()
        self.basedir: str = basedir
        self.cache: str = os.path.join(DIRS.CACHEDIR, basedir)
        self.cleaned: int = 0
        self.kept: int = 0

    @abc.abstractmethod
    def clean(self) -> str: pass


class FileCleaner(CacheCleaner):
    """ Cleaners that delete files from cache """

    def __init__(self, basedir: str, hexdirs: bool):
        super().__init__(basedir)
        self.hexdirs: bool = hexdirs

    def clean(self) -> str:
        """ Generic cleaning routine, iterates directories """
        if path_isdir(self.cache):
            if self.hexdirs:
                subdirs = itertools.product("0123456789abcdef", repeat=2)
                for i, j in subdirs:
                    dirname = os.path.join(self.cache, i, j)
                    for cached_file in listdir(dirname):
                        self.clean_file(os.path.join(dirname, cached_file))
            else:
                for cached_file in listdir(self.cache):
                    self.clean_file(os.path.join(self.cache, cached_file))
        msg = (f"Cleaned {self.cleaned} {self.name()} {plural(self.cleaned, 'file')} from {self.source()}, "
               f"kept {self.kept}")
        self.logger.debug(msg)
        return msg

    def remove_if(self, filename, condition):
        if condition:
            remove_file(filename)
            self.cleaned += 1
        else:
            self.kept += 1

    def source(self) -> str:
        return self.basedir

    @abc.abstractmethod
    def name(self) -> str:
        pass

    @abc.abstractmethod
    def clean_file(self, filename):
        pass


class FileExpirer(FileCleaner):
    """ Delete files in dirname that are older than expiry_seconds old.
    Return a string with a summary for printing. """

    def __init__(self, basedir: str, hexdirs: bool, expiry_sec: int):
        super().__init__(basedir, hexdirs)
        self.time_now = time.time()
        self.expiry_sec = expiry_sec

    def name(self) -> str:
        return 'expired'

    def clean_file(self, filename):
        if path_isfile(filename):
            cache_modified_time = os.stat(filename).st_mtime
            self.remove_if(filename, cache_modified_time < self.time_now - self.expiry_sec)


class ExtensionCleaner(FileCleaner):
    """ Delete all files in basedir with the right extension """

    def __init__(self, basedir: str, ext: str):
        super().__init__(basedir, False)
        self.ext = ext

    def name(self):
        return 'superfluous'

    def clean_file(self, filename):
        if self.ext:
            self.remove_if(filename, filename.endswith(self.ext))


class OrphanCleaner(FileCleaner):
    """ Delete files in dirname that don't have a corresponding entry in the DB,
    where the ID is the filename without the extension.
    Return a string with a summary for printing. """

    def __init__(self, basedir: str, hexdirs: bool, db, field: str, table: str, matcher: str, dotsplit: bool):
        super().__init__(basedir, hexdirs)
        self.db = db
        self.field = field
        self.table = table
        self.matcher = matcher
        self.dotsplit = dotsplit

    def name(self) -> str:
        return 'orphan'

    def getid(self, filename) -> str:
        name = os.path.basename(filename)
        if self.dotsplit:
            return name.split('.')[0]
        # Magazines use a different encoding scheme in the file name.
        fname, extn = splitext(name)
        return fname.split('_')[0] + extn

    def clean_file(self, filename):
        try:
            dbid = self.getid(filename)
            query = f'select {self.field} from {self.table} where {self.field}=?'
            match = self.matcher % dbid
            item = self.db.match(query, (match,))
            self.remove_if(filename, not item)
        except IndexError:
            self.logger.error(f'Clean Cache: Error splitting {filename}')


class UnreferencedCleaner(FileCleaner):
    """ Delete files that are no longer referenced in the database """

    def __init__(self, basedir: str, taskname: str, db, field: str, fieldcut: int, query: str):
        super().__init__(basedir, False)
        self.taskname = taskname
        self.db = db
        fullquery = f'SELECT {field} from {query}'
        res = db.select(fullquery)
        self.items = [item[field][fieldcut:] for item in res]
        self.logger.debug(f"Checking {len(self.items)} {field} images")

    def name(self) -> str:
        return 'orphan'

    def source(self) -> str:
        return self.taskname

    def clean_file(self, filename):
        name = os.path.basename(filename)
        self.remove_if(filename, name not in self.items)


class DBCleaner(CacheCleaner):
    """ Where the database refers to an image that no longer exists, replace it
    with the fallback image """

    def __init__(self, basedir: str, typestr: str, db, table: str, fimg: str, fname: str, fid: str, fallback: str):
        super().__init__(basedir)
        self.typestr = typestr
        self.db = db
        self.table = table
        self.fimg = fimg
        self.fname = fname
        self.fid = fid
        self.fallback = fallback
        query = f'SELECT {fimg},{fname},{fid} from {table}'
        self.items = db.select(query)

    def clean(self) -> str:
        for item in self.items:
            keep = True
            imgfile = ''
            if item[self.fimg] is None or item[self.fimg] == '':
                keep = False
            if keep and not item[self.fimg].startswith('http') and item[self.fimg] != self.fallback:
                # html uses '/' as separator, but os might not
                imgname = item[self.fimg].rsplit('/')[-1]
                imgfile = os.path.join(self.cache, imgname)
                if not path_isfile(imgfile):
                    keep = False
            if keep:
                self.kept += 1
            else:
                self.cleaned += 1
                self.logger.debug(f'{self.typestr} missing for {item[self.fname]} {imgfile}')
                updatequery = f"update {self.table} set {self.fimg}='{self.fallback}' where {self.fid}=?"
                self.db.action(updatequery, (item[self.fid],))

        msg = f"Cleaned {self.cleaned} missing {plural(self.cleaned, self.typestr)}, kept {self.kept}"
        self.logger.debug(msg)
        return msg
