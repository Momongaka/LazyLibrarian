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
import time
import traceback
from urllib.parse import urlencode, urlparse

from bs4 import BeautifulSoup

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.cache import fetch_url
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import (
    format_author_name,
    get_list,
    make_unicode,
    make_utf8bytes,
    plural,
    size_in_bytes,
    url_fix,
)
from lazylibrarian.telemetry import TELEMETRY
from lib.zlibrary import Zlibrary


def redirect_url(genhost, url):
    """ libgen.io might have dns blocked, but user can bypass using genhost 93.174.95.27 in config
        libgen might send us a book url that still contains http://libgen.io/  or /libgen.io/
        so we might need to redirect it to users genhost setting """

    logger = logging.getLogger(__name__)
    myurl = urlparse(url)
    if myurl.netloc.lower() != 'libgen.io':
        return url

    host = urlparse(genhost)

    # genhost http://93.174.95.27 -> scheme http, netloc 93.174.95.27, path ""
    # genhost 93.174.95.27 -> scheme "", netloc "", path 93.174.95.27
    if host.netloc:
        if host.netloc.lower() != 'libgen.io':
            # noinspection PyArgumentList,PyProtectedMember
            myurl = myurl._replace(netloc=host.netloc)
            logger.debug(f'Redirected libgen.io to [{host.netloc}]')
    elif host.path and host.path.lower() != 'libgen.io':
        # noinspection PyArgumentList,PyProtectedMember
        myurl = myurl._replace(netloc=host.netloc)
        logger.debug(f'Redirected libgen.io to [{host.netloc}]')
    return myurl.geturl()


def bok_sleep():
    cachelogger = logging.getLogger('special.cache')
    time_now = time.time()
    delay = time_now - lazylibrarian.TIMERS['LAST_BOK']
    limit = CONFIG.get_int('SEARCH_RATELIMIT')
    # make sure bok leaves at least a 2-second delay between calls to prevent "Too many requests from your IP"
    if limit < 2.0:
        limit = 2.0
    if delay < limit:
        sleep_time = limit - delay
        lazylibrarian.TIMERS['SLEEP_BOK'] += sleep_time
        cachelogger.debug(f"B-OK sleep {sleep_time:.3f}, total {lazylibrarian.TIMERS['SLEEP_BOK']:.3f}")
        time.sleep(sleep_time)
    lazylibrarian.TIMERS['LAST_BOK'] = time_now


def session_get(sess, url, headers):
    logger = logging.getLogger(__name__)
    if headers.get('Referer', '').startswith('https') and url.startswith('http:'):
        url = f"https:{url[5:]}"
    if url.startswith('https') and CONFIG.get_bool('SSL_VERIFY'):
        response = sess.get(url, headers=headers, timeout=90,
                            verify=CONFIG['SSL_CERTS'] if CONFIG['SSL_CERTS'] else True)
    else:
        response = sess.get(url, headers=headers, timeout=90, verify=False)
    if response.status_code != 200:
        logger.debug(f"b-ok response: {response.status_code}")
    return response


def bok_login():
    logger = logging.getLogger(__name__)
    try:
        domain = CONFIG['BOK_HOST']
        if '//' in domain:
            domain = domain.split('//')[1]
        if not domain:
            domain = None
        if CONFIG['BOK_REMIX_USERID'] and CONFIG['BOK_REMIX_USERKEY']:
            zlib = Zlibrary(domain=domain, remix_userid=CONFIG['BOK_REMIX_USERID'], remix_userkey=CONFIG['BOK_REMIX_USERKEY'])
        elif CONFIG['BOK_EMAIL'] and CONFIG['BOK_PASS']:
            zlib = Zlibrary(domain=domain, email=CONFIG['BOK_EMAIL'], password=CONFIG['BOK_PASS'])
        else:
            # logger.error("Zlibrary check credentials")
            return None
    except Exception as e:
        logger.error(str(e))
        return None

    if not zlib.isLoggedIn():
        logger.error(f"Zlibrary login error. {zlib.login_status()}")
        return None

    profile = zlib.getProfile()
    if not profile:
        logger.error("Zlibrary profile error")
        return None
    if not CONFIG['BOK_REMIX_USERID'] or not CONFIG['BOK_REMIX_USERKEY']:
        CONFIG['BOK_REMIX_USERID'] = profile["user"]["id"]
        CONFIG['BOK_REMIX_USERKEY'] = profile["user"]["remix_userkey"]
    CONFIG.set_int('BOK_DLLIMIT', profile["user"]["downloads_limit"])
    lazylibrarian.TIMERS['BOK_TODAY'] = profile["user"]["downloads_today"]
    return zlib


def direct_bok(book=None, prov=None, test=False):
    logger = logging.getLogger(__name__)
    provider = "zlibrary"
    if not prov:
        prov = 'BOK'
    if BLOCKHANDLER.is_blocked(provider):
        if test:
            return False
        return [], "provider is already blocked"

    zlib = bok_login()
    if not zlib:
        if test:
            return False
        return [], "Invalid credentials"

    limit = 50
    if test:
        book['bookid'] = '0'
        limit = 10

    try:
        langs = CONFIG['BOK_SEARCH_LANG']
        if not langs:  # full language names, lower case, eg spanish, german
            langs = None
        searchresults = zlib.search(book['searchterm'], limit=limit, languages=langs)
    except Exception as e:
        logger.debug(f"Error getting results from zlibrary ({e})")
        searchresults = None
    if not searchresults or not searchresults.get('success', 0):
        if test:
            return False
        return [], "No results from zlibrary"

    logger.debug(f"{provider} returned {len(searchresults['books'])}")
    results = []
    removed = 0
    for item in searchresults['books']:
        author = item['author']
        title = item['title']
        extn = item['extension']
        size = item['filesize']
        dl = f"{item['id']}^{item['hash']}"
        if not author or not title or not size or not dl:
            removed += 1
        else:
            if author:
                title = f"{author.strip()} {title.strip()}"
            if extn:
                title = f"{title}.{extn}"
            results.append({
                'bookid': book['bookid'],
                'tor_prov': provider,
                'tor_title': title,
                'tor_url': dl,
                'tor_size': size,
                'tor_type': 'direct',
                'priority': CONFIG[f"{prov}_DLPRIORITY"],
                'prov_page': item['href']
            })
            logger.debug(f'Found {title}, Size {size}')

    if test:
        logger.debug(f"Test found {len(results)} {plural(len(results), 'result')} ({removed} removed)")
        return len(results)

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {provider} for {book['searchterm']}")
    return results, ''


def direct_gen(book=None, prov=None, test=False):
    logger = logging.getLogger(__name__)
    errmsg = ''
    host = ''
    search = ''
    priority = 0
    provider = "libgen"
    if not prov:
        prov = 'GEN_0'
    if BLOCKHANDLER.is_blocked(prov):
        if test:
            return False
        return [], "provider_is_blocked"
    for entry in CONFIG.providers('GEN'):
        if entry['NAME'].lower() == prov.lower():
            host = entry['HOST'].rstrip('/')
            if not host.startswith('http'):
                host = f"http://{host}"
            search = entry['SEARCH']
            if not search:
                search = 'search.php'
            if search[0] == '/':
                search = search[1:]
            priority = entry['DLPRIORITY']
            break

    if not host:
        return [], f"Unknown Provider [{prov}]"

    sterm = make_unicode(book['searchterm'])

    page = 1
    results = []
    next_page = True
    maxresults = 100
    if test:
        book['bookid'] = '0'
        maxresults = 25

    while next_page:
        if 'index.php' in search:
            params = {
                "f_lang": "All",
                "f_columns": 0,
                "f_ext": "All"
            }
            if "?req=" in search or "&req=" in search:
                search = search.replace("?req=", "").replace("&req=", "")
                params['req'] = make_utf8bytes(book['searchterm'])[0]
            else:
                if "?s=" in search or "&s=" in search:
                    search = search.replace("?req=", "").replace("&req=", "")
                params["s"] = make_utf8bytes(book['searchterm'])[0]
        elif 'search.php' in search:
            params = {
                "view": "simple",
                "open": 0,
                "phrase": 0,
                "column": "def",
                "lg_topic": "libgen",
                "res": maxresults
            }
            # for search.php, default to req=
            if "?s=" in search or "&s=" in search:
                search = search.replace("?s=", "").replace("&s=", "")
                params['s'] = make_utf8bytes(book['searchterm'])[0]
            else:
                if "?req=" in search or "&req=" in search:
                    search = search.replace("?req=", "").replace("&req=", "")
                params["req"] = make_utf8bytes(book['searchterm'])[0]
        elif 'comic' in search:
            params = {
                "s": make_utf8bytes(book['searchterm'])[0]
            }
        else:  # elif 'fiction' in search:
            params = {
                "q": make_utf8bytes(book['searchterm'])[0]
            }

        if page > 1:
            params['page'] = page

        providerurl = url_fix(f"{host}/{search}")
        search_url = f"{providerurl}?{urlencode(params)}"
        next_page = False
        result, success = fetch_url(search_url)
        if not success:
            # may return 404 if no results, not really an error
            if '404' in result:
                logger.debug(f"No results found from {provider} for {sterm}, got 404 for {search_url}")
            elif '111' in result:
                # looks like libgen has ip based access limits
                logger.error(f'Access forbidden. Please wait a while before trying {provider} again.')
                errmsg = result
                BLOCKHANDLER.block_provider(prov, errmsg)
            else:
                logger.debug(search_url)
                logger.debug(f'Error fetching page data from {provider}: {result}')
                errmsg = result
                TELEMETRY.record_usage_data("libgenError")
            if test:
                return False
            return results, errmsg

        if len(result):
            logger.debug(f'Parsing results from <a href="{search_url}">{provider}</a>')
            try:
                soup = BeautifulSoup(result, 'html5lib')
                rows = []
                tabletype = None
                try:
                    if 'comic' in search:
                        tabletype = 'comic'
                        tables = soup.find_all('table', align='center')
                    else:
                        tabletype = 'libgen'
                        tables = soup.find_all('table', id='tablelibgen')
                    if not tables:
                        tabletype = 'rows'
                        tables = soup.find_all('table', rules='rows')  # the last table with rules=rows
                    if not tables:
                        tabletype = 'table'
                        tables = soup.find_all('table')
                    if tables:
                        # all rows from the last matching table
                        rows = tables[-1].find_all('tr')
                except IndexError:  # no results table in result page
                    logger.debug("No table found in results")
                    rows = []

                if len(rows) > 1:  # skip table headers
                    rows = rows[1:]

                logger.debug(f"libgen returned {len(rows)} {plural(len(rows), 'row')}")
                for row in rows:
                    author = ''
                    title = ''
                    size = ''
                    extn = ''
                    td = row.find_all('td')
                    links = []
                    prov_page = ''
                    if td and tabletype == 'comic':
                        try:
                            if 'FILE' in str(td[-1]):
                                newsoup = BeautifulSoup(str(td[3]), 'html5lib')
                                data = newsoup.find_all('a')
                                for d in data:
                                    prov_page = d.get('href')
                                    break
                                title = td[3].text.strip()
                                newsoup = BeautifulSoup(str(td[1]), 'html5lib')
                                data = newsoup.find_all('a')
                                for d in data:
                                    links.append(d.get('href'))
                                issue = ''
                                year = ''
                                publisher = ''
                                language = ''
                                for f in range(4, len(td) - 1):
                                    if 'Issue: ' in td[f].text:
                                        issue = td[f].text.split('Issue: ')[1].strip()
                                    elif 'Year: ' in td[f].text:
                                        year = td[f].text.split('Year: ')[1].strip()
                                    elif 'Publisher: ' in td[f].text:
                                        publisher = td[f].text.split('Publisher: ')[1].strip()
                                    elif 'Language: ' in td[f].text:
                                        language = td[f].text.split('Language: ')[1].strip()
                                    elif not size and '<br' in str(td[f]) and td[f].text[0].isdigit():
                                        size = str(td[f]).split('>')[1].split('<br')[0]
                                        extn = str(td[f]).split('<br')[1].split('>')[1].split('<')[0]
                                    logger.debug(
                                        f"Title: {title} Issue:{issue} Year:{year} Pub:{publisher} "
                                        f"Lang:{language} Size: {size}")
                        except Exception as e:
                            logger.debug(f'Error parsing libgen comic results: {str(e)}')
                            TELEMETRY.record_usage_data("libgenComicError")

                    elif td and tabletype == 'libgen':
                        try:
                            author = format_author_name(td[1].text, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
                            title = td[0].text.split('\n')[0].strip()
                            # publisher = td[2].text
                            # year = td[3].text
                            # language = td[4].text
                            size = td[6].text.upper()
                            extn = td[7].text
                            newsoup = BeautifulSoup(str(td[8]), 'html5lib')
                            data = newsoup.find_all('a')
                            for d in data:
                                links.append(d.get('href'))
                        except Exception as e:
                            logger.debug(f'Error parsing libgen results: {str(e)}')
                            TELEMETRY.record_usage_data("libgenError")

                    elif ('fiction' in search or 'index.php' in search) and len(td) > 3:
                        try:
                            author = format_author_name(td[0].text, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
                            title = td[2].text
                            newsoup = BeautifulSoup(str(td[2]), 'html5lib')
                            data = newsoup.find_all('a')
                            for d in data:
                                prov_page = d.get('href')
                                break
                            newsoup = None
                            if '/' in td[4].text:
                                extn = td[4].text.split('/')[0].strip()
                                size = td[4].text.split('/')[1].strip()
                                newsoup = BeautifulSoup(str(td[5]), 'html5lib')
                            elif '(' in td[4].text:
                                extn = td[4].text.split('(')[0].strip()
                                size = td[4].text.split('(')[1].split(')')[0]
                                newsoup = BeautifulSoup(str(td[4]), 'html5lib')
                            size = size.upper()
                            if newsoup:
                                data = newsoup.find_all('a')
                                for d in data:
                                    links.append(d.get('href'))
                        except IndexError as e:
                            logger.debug(f'Error parsing libgen fiction results: {str(e)}')
                            TELEMETRY.record_usage_data("libgenFictionError")

                    elif 'search.php' in search and len(td) > 8:
                        # Non-fiction
                        try:
                            author = format_author_name(td[1].text, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
                            title = td[2].text
                            newsoup = BeautifulSoup(str(td[2]), 'html5lib')
                            data = newsoup.find_all('a')
                            for d in data:
                                prov_page = d.get('href')
                                break
                            size = td[7].text.upper()
                            extn = td[8].text
                            td = td[9:-1]
                            for lnk in td:
                                newsoup = BeautifulSoup(str(lnk), 'html5lib')
                                data = newsoup.find_all('a')
                                for d in data:
                                    links.append(d.get('href'))
                        except IndexError as e:
                            logger.debug(f'Error parsing libgen search.php results; {str(e)}')
                            TELEMETRY.record_usage_data("libgenSearchError")

                    size = size_in_bytes(size)

                    if links and title:
                        if author:
                            title = f"{author.strip()} {title.strip()}"
                        if extn:
                            title = f"{title}.{extn}"

                        success = False
                        bookresult = None
                        url = None
                        for link in links:
                            if link.startswith('magnet'):
                                url = link
                            elif "comic" in search or "booksdescr.org" in link:
                                # booksdescr is a direct link to book
                                url = link
                                if not url.startswith('http'):
                                    url = url_fix(f"{host}/{url}")
                                    logger.debug(url)
                                success = True
                                break
                            elif link.startswith('http'):
                                url = redirect_url(host, link)
                            elif tabletype == 'libgen' and link.startswith('/ads'):
                                url = 'https://libgen.li' + link
                            else:
                                if "/index.php?" in link:
                                    link = f"md5{link.split('md5')[1]}"
                                if "/ads.php?" in link:
                                    url = url_fix(f"{host}/{link}")
                                else:
                                    url = url_fix(f"{host}/ads.php?{link}")

                            # redirect page for other sources [libgen.me, libgen.li, library1.org, booksdl.org]
                            bookresult, success = fetch_url(url)
                            if not success:
                                logger.debug(f'Error fetching link data from {provider}: {bookresult}')
                                logger.debug(url)
                            else:
                                break

                        if success and bookresult:
                            try:
                                new_soup = BeautifulSoup(bookresult, 'html5lib')
                                for link in new_soup.find_all('a'):
                                    output = link.get('href')
                                    if output:
                                        if ('/get.php' in output or '/download/' in output
                                                or '/book/' in output or '/fiction/' in output
                                                or '/main/' in output) and output.startswith('http'):
                                            url = output
                                            break
                                        nhost = urlparse(url)
                                        nurl = urlparse(output)
                                        # noinspection PyProtectedMember
                                        nurl = nurl._replace(scheme=nhost.scheme)
                                        # noinspection PyProtectedMember
                                        nurl = nurl._replace(netloc=nhost.netloc)
                                        url = nurl.geturl()
                                        break
                                if url:
                                    url = make_unicode(url)
                                    if not url.startswith('http'):
                                        url = url_fix(host + url)
                                    else:
                                        url = redirect_url(host, url)
                                    logger.debug(f"Download URL: {url}")
                            except Exception as e:
                                logger.error(f'{type(e).__name__} parsing bookresult: {str(e)}')
                                url = None

                        if url:
                            if prov_page:
                                prov_page = url_fix(host + prov_page)
                            results.append({
                                'bookid': book['bookid'],
                                'tor_prov': f"{provider}/{search}",
                                'tor_title': title,
                                'tor_url': url,
                                'tor_size': str(size),
                                'tor_type': 'direct',
                                'priority': priority,
                                'prov_page': prov_page
                            })
                            logger.debug(f'Found {title}, Size {size}')
                        next_page = True

            except Exception as e:
                logger.error(f"An error occurred in the {provider} parser: {str(e)}")
                logger.debug(f'{provider}: {traceback.format_exc()}')
                TELEMETRY.record_usage_data("libgenParserError")

            if test:
                logger.debug(f"Test found {len(results)} {plural(len(results), 'result')}")
                return len(results)

        page += 1
        if 0 < CONFIG.get_int('MAX_PAGES') < page:
            logger.warning('Maximum results page search reached, still more results available')
            next_page = False

        # try to detect libgen mirrors not honouring "page="
        if results:
            last_result_url = results[-1]['tor_url']
            cnt = 0
            for item in results:
                if item['tor_url'] == last_result_url:
                    cnt += 1
                if cnt > 1:
                    break
            if cnt > 1:
                logger.warning('Duplicate results page found from provider')
                next_page = False
        else:
            logger.warning('No results found from provider')
            next_page = False

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {provider} for {sterm}")
    return results, errmsg


def bok_grabs() -> (int, int):
    # we might be out of sync with zlibrary download counter, eg we might not be the only downloader
    # so although we can count how many we downloaded, normally we ask zlibrary and use their counter
    # If we are over limit we try to use our datestamp to find out when the counter will reset
    db = database.DBConnection()
    yesterday = time.time() - 24 * 60 * 60
    grabs = db.select("SELECT completed from wanted WHERE nzbprov='zlibrary' and completed > ? order by completed",
                      (yesterday,))
    db.close()
    if grabs:
        return len(grabs), grabs[0]['completed']
    return 0, 0
