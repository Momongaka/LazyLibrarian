#  This file is part of Lazylibrarian.
#  Lazylibrarian is free software, you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

# Heavily modified from code found at https://pypi.org/project/annas-py

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from html import unescape as html_unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from requests import get
from requests.exceptions import JSONDecodeError

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import DIRS, get_directory, path_isfile, remove_file
from lazylibrarian.formatter import check_int, get_list, md5_utf8, plural, sanitize, size_in_bytes

py310 = sys.version_info >= (3, 10)

WIKIPEDIA_URL = "https://en.wikipedia.org/wiki/Anna%27s_Archive"

@dataclass(**({"slots": True} if py310 else {}))
class FileInfo:
    extension: str
    size: str
    language: str


@dataclass(**({"slots": True} if py310 else {}))
class SearchResult:
    id: str
    title: str
    authors: str
    file_info: FileInfo
    thumbnail: str
    publisher: str
    publish_date: str


class OrderBy(Enum):
    MOST_RELEVANT = ""
    NEWEST = "newest"
    OLDEST = "oldest"
    LARGEST = "largest"
    SMALLEST = "smallest"


class FileType(Enum):
    ANY = ""
    PDF = "pdf"
    EPUB = "epub"
    MOBI = "mobi"
    AZW3 = "azw3"
    FB2 = "fb2"
    LIT = "lit"
    DJVU = "djvu"
    RTF = "rtf"
    ZIP = "zip"
    RAR = "rar"
    CBR = "cbr"
    TXT = "txt"
    CBZ = "cbz"
    HTML = "html"
    FB2_ZIP = "fb2.zip"
    DOC = "doc"
    HTM = "htm"
    DOCX = "docx"
    LRF = "lrf"
    MHT = "mht"


class Language(Enum):
    ANY = ""
    EN = "en"
    AR = "ar"
    BE = "be"
    BG = "bg"
    BN = "bn"
    CA = "ca"
    CS = "cs"
    DE = "de"
    EL = "el"
    EO = "eo"
    ES = "es"
    FA = "fa"
    FR = "fr"
    HI = "hi"
    HU = "hu"
    ID = "id"
    IT = "it"
    JA = "ja"
    KO = "ko"
    LT = "lt"
    ML = "ml"
    NL = "nl"
    NO = "no"
    OR = "or"
    PL = "pl"
    PT = "pt"
    RO = "ro"
    RU = "ru"
    SK = "sk"
    SL = "sl"
    SQ = "sq"
    SR = "sr"
    SV = "sv"
    TR = "tr"
    TW = "tw"
    UK = "uk"
    UR = "ur"
    VI = "vi"
    ZH = "zh"


class HTTPFailedError(Exception):
    pass


def extract_file_info(raw: str) -> FileInfo:
    # Extract file info from raw string given from the website
    # it assumes that the string will always have the file format
    # and size, but can have language and file name too
    # > Cases:
    #     Language, format, size and file name is provided;
    #     Language, format and size is provided, file name is omitted;
    #     Format and size is provided, language and name is omitted.

    # sample data:
    #  English [en], pdf, 7.5MB, "Python_Web_Scraping_-_Second_Edition.pdf"
    #  Portuguese [pt], epub, 1.5MB
    #  mobi, 4.1MB
    # English [en], .pdf, 🚀/ia, 14.9MB, 📗 Book (unknown), ia/londonrules0000herr_q7j9.pdf

    info_list = raw.split(", ")
    language = ''
    if "[" in info_list[0]:
        language = info_list.pop(0)
    extension = info_list.pop(0)
    size = info_list.pop(0)
    if not size.endswith('B'):
        size = info_list.pop(0)
    return FileInfo(extension, size, language)


def extract_publish_info(raw: str) -> tuple[str, str]:
    # Sample data:
    #  John Wiley and Sons; Wiley (Blackwell Publishing); Blackwell Publishing Inc.;
    #  Wiley; JSTOR (ISSN 0020-6598), International Economic Review, #2, 45, pages 327-350, 2004 may
    #  Cambridge University Press, 10.1017/CBO9780511510854, 2001
    #  Cambridge University Press, 1, 2008
    #  Cambridge University Press, 2014 feb 16
    #  1, 2008
    #  2008

    if raw.strip() == "":
        return '', ''
    info = [i for i in raw.split(", ") if i.strip()]
    last_info = info[-1].split()
    date = ''
    if last_info[0].isdecimal() and last_info[0] != "0":
        info.pop()
        date = last_info.pop(0)
        if last_info:
            date = " ".join(last_info) + " of " + date
        elif info and info[-1].isdecimal():
            date = info.pop() + ", " + date
    publisher = ", ".join(info) or ''
    return publisher, date


# noinspection PyDefaultArgument
def html_parser(url: str, params: dict = None) -> BeautifulSoup:
    if not params:
        params = {}
    params = dict(filter(lambda i: i[1], params.items()))
    response = get(url, params=params)
    if response.status_code >= 400:
        raise HTTPFailedError(f"server returned http status {response.status_code}")
    html = response.text.replace("<!--", "").replace("-->", "")
    return BeautifulSoup(html, "html5lib")


def annas_hosts_prefer(annas_hosts, host):
    # success, move this host to front of list
    annas_hosts.remove(host)
    annas_hosts.insert(0, host)
    CONFIG['ANNA_HOST'] = ','.join(annas_hosts)


def annas_hosts_update():
    annas_hosts = get_list(CONFIG['ANNA_HOST'])
    new_hosts = fetch_annas_archive_domains()
    annas_hosts.extend(new_hosts)
    annas_hosts = set(annas_hosts)
    CONFIG['ANNA_HOST'] = ','.join(annas_hosts)


def annas_search(
    query: str,
    language: Language = Language.ANY,
    file_type: FileType = FileType.ANY,
    order_by: OrderBy = OrderBy.MOST_RELEVANT,
) -> str | None:

    downloadlogger = logging.getLogger('special.dlcomms')
    if not query.strip():
        raise ValueError("query can not be empty")
    params = {
        "q": query,
        "lang": language.value,
        "ext": file_type.value,
        "sort": order_by.value,
    }
    annas_hosts = get_list(CONFIG['ANNA_HOST'])
    if len(annas_hosts) == 1:
        # old style single host, add the wikipedia entries
        annas_hosts_update()
    soup = None
    for host in annas_hosts:
        prefix = ''
        if not host.startswith('http'):
            prefix = "https://"
        try:
            soup = html_parser(urljoin(prefix + host, "search"), params)
            if soup:
                # got some results, prefer this host next time
                annas_hosts_prefer(annas_hosts, host)
                break
        except Exception as e:
            downloadlogger.error(f"{host}: {e}")
    if not soup:
        # no searches returned anything, update the host list
        annas_hosts_update()
        return None

    raw_results = soup.select("div[class*='pt-3'][class*='border-b']")
    results = list(filter(lambda i: i is not None, map(parse_result, raw_results)))
    myhash = md5_utf8(query)
    cache_location = os.path.join(DIRS.CACHEDIR, "IRCCache")
    hashfilename = os.path.join(cache_location, f"{myhash}.anna")
    resultslist = []
    for r in results:
        resultdict = {'title': r.title,
                      'id': r.id,
                      'authors': r.authors,
                      'thumbnail': r.thumbnail,
                      'publisher': r.publisher,
                      'publish_date': r.publish_date,
                      'extension': r.file_info.extension,
                      'size': r.file_info.size,
                      'language': r.file_info.language,
                      }
        resultslist.append(resultdict)

    with open(hashfilename, 'w') as fp:
        json.dump(resultslist, fp)
    return hashfilename


def parse_result(raw_content: Tag) -> SearchResult | None:
    try:
        link = raw_content.find("a", class_="js-vim-focus")
        if not link:
            return None

        title = link.text.strip()
        hashid = link.get("href", "").split("md5/")[-1]
        if not hashid:
            return None

    except (AttributeError, IndexError):
        return None

    # Extract author from fallback cover
    authors = ''
    author_div = raw_content.find("div", class_="text-amber-900")
    if author_div:
        authors = author_div.get("data-content", "")

    # Extract file info from metadata div
    file_info = FileInfo("", "", "")
    metadata_div = raw_content.find("div", class_="text-gray-800")
    if not metadata_div:
        for div in raw_content.find_all("div"):
            if "✅" in div.text:
                metadata_div = div
                break

    if metadata_div:
        metadata_text = metadata_div.text

        lang_match = re.search(r'([A-Za-z]+)\s*\[([a-z]{2})\]', metadata_text)
        language = lang_match.group(2) if lang_match else ""

        size_match = re.search(r'(\d+\.?\d*\s*[MKG]B)', metadata_text)
        size = size_match.group(1) if size_match else ""

        format_match = re.search(
            r'·\s*(PDF|EPUB|MOBI|AZW3|FB2|TXT|DJVU|CBR|CBZ|RTF|LIT|DOC|DOCX|HTML|HTM|LRF|MHT|ZIP|RAR)\s*·',
            metadata_text, re.IGNORECASE)
        extension = format_match.group(1).lower() if format_match else ""

        file_info = FileInfo(extension, size, language)

    # Fallback: extract extension from file path
    if not file_info.extension:
        file_path_div = raw_content.find("div", class_="text-gray-500")
        if file_path_div:
            file_path = file_path_div.text.strip()
            if "." in file_path:
                file_info = FileInfo(file_path.split(".")[-1].lower(), file_info.size, file_info.language)

    publisher = ''
    publish_date = ''

    if metadata_div and metadata_div.text:
        metadata_text = metadata_div.text
        year_match = re.search(r'·\s*(\d{4})\s*·', metadata_text)
        if year_match:
            publish_date = year_match.group(1)

    thumbnail = ''
    try:
        img = raw_content.find("img")
        if img:
            thumbnail = img.get("src", "")
    except AttributeError:
        thumbnail = ''

    res = SearchResult(
        id=hashid,
        title=html_unescape(title),
        authors=html_unescape(authors),
        file_info=file_info,
        thumbnail=thumbnail,
        publisher=html_unescape(publisher) if publisher else '',
        publish_date=publish_date,
    )
    return res


def annas_download(md5, folder, title, extn, domain_index=0):
    logger = logging.getLogger(__name__)
    downloadlogger = logging.getLogger('special.dlcomms')
    params = {'md5': md5, 'key': CONFIG['ANNA_KEY']}
    if domain_index:
        params['domain_index'] = domain_index
    annas_hosts = get_list(CONFIG['ANNA_HOST'])
    if not annas_hosts:
        return False, "No Annas hosts found"
    response = None
    url = None
    for host in annas_hosts:
        prefix = ''
        if not host.startswith('http'):
            prefix = "https://"
        url = urljoin(prefix + host, '/dyn/api/fast_download.json')
        try:
            response = get(url, params=params)
        except Exception as e:
            downloadlogger.debug(f"Exception from {host}: {e}")
            response = None
        if response and str(response.status_code).startswith('2'):
            downloadlogger.debug(f"Result {response.status_code} from {host}")
            annas_hosts_prefer(annas_hosts, host)
            break
        downloadlogger.debug(f"Failed to download from {host}: {response.status_code if response else 'No response'}")

    if not response:
        return False, "No response from Annas"

    if str(response.status_code).startswith('2'):
        max_domain_index = check_int(CONFIG['ANNA_MAX_SERVERS'], 0) - 1 # Server indexes are 0-based
        res = response.json()
        downloadlogger.debug(res)
        counters = res['account_fast_download_info']
        CONFIG.set_int('ANNA_DLLIMIT', counters['downloads_per_day'])
        lazylibrarian.TIMERS['ANNA_REMAINING'] = counters['downloads_left']
        if counters['downloads_left'] == 0:
            msg = f"Download limit reached ({counters})"
            block_annas(counters['downloads_per_day'])
            return False, msg
        url = res['download_url']
        if url and url.startswith('http'):
            try:
                r = get(url)
                if not str(r.status_code).startswith('2'):
                    msg = f"Got a {r.status_code} response for {url}"
                    if domain_index < max_domain_index:
                        return annas_download(md5, folder, title, extn, domain_index + 1)
                    downloadlogger.warning(msg)
                    return False, msg
                filedata = r.content
                if not len(filedata):
                    msg = f"Got empty response for {url}"
                    downloadlogger.warning(msg)
                    return False, msg
                if len(filedata) < 100:
                    msg = f"Only got {len(filedata)} bytes for {url}"
                    downloadlogger.warning(msg)
                    return False, msg
                downloadlogger.debug(f"Got {len(filedata)} bytes for {url}")
                download_dir = get_directory('Download')
                if folder:
                    parent = os.path.join(download_dir, folder)
                    if not os.path.isdir(parent):
                        os.mkdir(parent)
                dest_filename = os.path.join(download_dir, folder, sanitize(f"{title}{extn}"))
                with open(dest_filename, 'wb') as f:
                    f.write(filedata)
                logger.debug(f"Data written to file {dest_filename}")
                lazylibrarian.TIMERS['ANNA_REMAINING'] = counters['downloads_left'] - 1
                logger.info(f"Anna {lazylibrarian.TIMERS['ANNA_REMAINING']} remaining "
                            f"of {counters['downloads_per_day']}")
                return True, dest_filename
            except Exception as e:
                logger.debug(str(e))
                if domain_index < max_domain_index:
                    return annas_download(md5, folder, title, extn, domain_index + 1)
        errmsg = f"Invalid url: {url} {res['error']}"
        logger.error(errmsg)
        return False, errmsg
    if response.status_code == 409:
        errmsg = f"Error Status: {response.status_code} Over your daily limit."
    else:
        downloadlogger.debug(url)
        downloadlogger.debug(str(params))
        try:
            data = response.json()
        except JSONDecodeError:
            data = None
        if data and 'error' in data:
            errmsg = f"Error Status: {response.status_code}: {data['error']}"
        else:
            errmsg = (f"Error Status: {response.status_code}: Check your ANNAS key, "
                      f"and make sure you have a PAID subscription")

    logger.error(errmsg)
    return False, errmsg


def anna_search(book=None, searchtype='ebook', test=False):
    logger = logging.getLogger(__name__)
    provider = "annas"
    lang = CONFIG['ANNA_SEARCH_LANG'].split(',')[0].strip().upper()
    if lang and lang in Language.__members__:
        language = Language[lang]
    else:
        language = Language.ANY

    if BLOCKHANDLER.is_blocked(provider):
        if test:
            return False
        return [], "provider is already blocked"

    cache = True
    cachelogger = logging.getLogger('special.cache')
    cache_location = os.path.join(DIRS.CACHEDIR, "IRCCache")

    if test:
        book['bookid'] = '0'
        cache = False

    if cache:
        myhash = md5_utf8(book['searchterm'])
        valid_cache = False
        hashfilename = os.path.join(cache_location, f"{myhash}.anna")
        # cache results so we can do multiple searches for the same author
        # or multiple search types for a book without hammering the provider
        # expire cache after 2 hours, there might be new additions
        expiry = check_int(lazylibrarian.IRC_CACHE_EXPIRY, 2 * 3600)

        if path_isfile(hashfilename):
            cache_modified_time = os.stat(hashfilename).st_mtime
            time_now = time.time()
            if cache_modified_time < time_now - expiry:
                # Cache entry is too old, delete it
                cachelogger.debug(f"Expiring {myhash}")
                remove_file(hashfilename)
            else:
                valid_cache = True

        if valid_cache:
            lazylibrarian.CACHE_HIT = int(lazylibrarian.CACHE_HIT) + 1
            cachelogger.debug(f"CacheHandler: Found CACHED response {hashfilename} for {book['searchterm']}")
        else:
            lazylibrarian.CACHE_MISS = int(lazylibrarian.CACHE_MISS) + 1
            hashfilename = annas_search(book['searchterm'], language=language)
    else:
        hashfilename = annas_search(book['searchterm'], language=language)

    if not hashfilename:
        return [], ''

    with open(hashfilename) as f:
        searchresults = json.load(f)

    logger.debug(f"{provider} returned {len(searchresults)}")
    results = []
    removed = 0
    for item in searchresults:
        author = item['authors']
        title = item['title']
        extn = item['extension']
        size = str(size_in_bytes(item['size']))
        lang = item['language']
        dl = item['id']
        title = title.split('\n')[0]

        if searchtype == 'mag':  # magazines don't have an author
            author = title
        if not author or not title or not size or not dl:
            removed += 1
            logger.debug(f"Rejecting Author:{author}, Title:{title}, Size:{size}, DL:{dl}")
        else:
            if author and author not in title:
                title = f"{author.strip()} {title.strip()}"

            results.append({
                'bookid': book['bookid'],
                'tor_prov': provider,
                'tor_title': f"{title}.{extn}",
                'tor_url': dl,
                'tor_size': size,
                'tor_type': 'direct',
                'tor_extn': extn,
                'tor_lang': lang,
                'priority': CONFIG["ANNA_DLPRIORITY"],
                'hash_file': hashfilename
            })
            logger.debug(f'Found {title}, Size {size}')

    if test:
        logger.debug(f"Test found {len(results)} {plural(len(results), 'result')} ({removed} removed)")
        return len(results)

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {provider} "
                 f"for {book['searchterm']} ({removed} removed)")
    return results, ''


def block_annas(dl_limit=0):
    logger = logging.getLogger(__name__)
    grabs, oldest = anna_grabs()
    if dl_limit and grabs >= dl_limit:
        old_datestr = datetime.fromtimestamp(oldest, UTC).strftime('%Y-%m-%d %H:%M:%S')
        # rolling delay if limit reached
        resume = oldest + (18 * 60 * 60)
        if resume > time.time():
            delay = time.time() - resume
        else:
            # default delay if our grab was too old (it wasn't us)
            delay = (30 * 60)

        logger.debug(f"Grabs: {grabs}, Oldest: {old_datestr}, Limit: {dl_limit}, Delay: {delay}")
        res = f"Reached Daily download limit ({grabs}/{dl_limit})"
        BLOCKHANDLER.block_provider("annas", res, delay=delay)
    else:
        # we haven't grabbed enough, someone else is also using annas, wait 30 mins...
        res = f"Anna reports 0 left of {dl_limit}"
        BLOCKHANDLER.block_provider("annas", res, delay=30 * 60)


def anna_grabs() -> tuple[int, int]:
    # we might be out of sync with download counter, eg we might not be the only downloader
    # so although we can count how many we downloaded, normally we ask anna and use their counter
    # If we are over limit we try to use our datestamp to find out when the counter will reset
    db = database.DBConnection()
    eighteen_hours_ago = time.time() - (18 * 60 * 60)
    grabs = db.select("SELECT completed from wanted WHERE nzbprov='annas' and completed > ? order by completed",
                      (eighteen_hours_ago,))
    db.close()
    if grabs:
        return len(grabs), int(grabs[0]['completed'])
    return 0, 0


def fetch_annas_archive_domains():
    """Fetch current Anna's Archive domains from Wikipedia. Returns list of bare domains."""
    logger = logging.getLogger(__name__)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    try:
        response = get(WIKIPEDIA_URL, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Wikipedia returned status {response.status_code}")
            return []
    except Exception as e:
        logger.warning(f"Failed to fetch domains from Wikipedia: {e}")
        return []

    pattern_table = re.compile(
        r'<table[^>]*class="[^"]*infobox vcard[^"]*"[^>]*>(.*?)(?:<h2|</table)',
        re.DOTALL | re.IGNORECASE
    )
    pattern_span = re.compile(
        r'<span[^>]*class="[^"]*url[^"]*"[^>]*>(.*?)</span>',
        re.DOTALL | re.IGNORECASE
    )
    pattern_link = re.compile(
        r'<a[^>]*class="[^"]*external text[^"]*"[^>]*href="([^"]+)"',
        re.IGNORECASE
    )

    table_match = pattern_table.search(response.text)
    if not table_match:
        logger.warning("Could not find infobox table in Wikipedia page")
        return []

    domains = []
    for span_html in pattern_span.findall(table_match.group(0)):
        for href in pattern_link.findall(span_html):
            parsed = urlparse(href if '://' in href else 'https:' + href)
            if parsed.netloc:
                domains.append(parsed.netloc)

    logger.info(f"Fetched {len(domains)} domain(s) from Wikipedia: {domains}")
    return domains
