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

import json
import logging
import os
import re
import threading
import time
from copy import deepcopy
from urllib.parse import urlencode, urlparse
from xml.etree import ElementTree

try:
    import xmltodict
except ImportError:
    xmltodict = None

from bs4 import BeautifulSoup

import lazylibrarian
import lib.feedparser as feedparser
from lazylibrarian import database
from lazylibrarian.annas import anna_search, block_annas
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.cache import fetch_url
from lazylibrarian.config2 import CONFIG, wishlist_type
from lazylibrarian.configtypes import ConfigDict
from lazylibrarian.directparser import bok_grabs, direct_bok, direct_gen
from lazylibrarian.filesystem import DIRS, path_isfile, remove_file, syspath
from lazylibrarian.formatter import (
    age,
    check_int,
    clean_name,
    get_list,
    make_unicode,
    md5_utf8,
    month2num,
    plural,
    seconds_to_midnight,
    today,
    unaccented,
)
from lazylibrarian.ircbot import irc_query, irc_results
from lazylibrarian.soulseek import slsk_search
from lazylibrarian.torrentparser import (
    torrent_abb,
    torrent_kat,
    torrent_lime,
    torrent_tdl,
    torrent_tpb,
)


def test_provider(name: str, host=None, api=None):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    res = db.match("SELECT authorname,authorid from authors order by totalbooks desc")
    if res:
        testname = res['authorname']
        testid = res['authorid']
        res = db.match("SELECT bookname from books where authorid=? order by bookrate desc", (testid,))
        if res:
            testbook = res['bookname']
        else:
            testbook = ''
    else:
        testname = "Agatha Christie"
        testbook = "Poirot"
    db.close()

    book = {'searchterm': testname, 'authorName': testname, 'library': 'eBook', 'bookid': '1',
            'bookName': testbook, 'bookSub': ''}

    if name == 'TPB':
        logger.debug(f"Testing provider {name}")
        if host:
            CONFIG.set_str('TPB_HOST', host)
        return torrent_tpb(book, test=True), "Pirate Bay"
    if name == 'KAT':
        logger.debug(f"Testing provider {name}")
        if host:
            CONFIG.set_str('KAT_HOST', host)
        return torrent_kat(book, test=True), "KickAss Torrents"
    if name == 'LIME':
        logger.debug(f"Testing provider {name}")
        if host:
            CONFIG.set_str('LIME_HOST', host)
        return torrent_lime(book, test=True), "LimeTorrents"
    if name == 'TDL':
        logger.debug(f"Testing provider {name}")
        if host:
            CONFIG.set_str('TDL_HOST', host)
        return torrent_tdl(book, test=True), "TorrentDownloads"
    if name == 'ABB':
        logger.debug(f"Testing provider {name}")
        if host:
            CONFIG.set_str('ABB_HOST', host)
        return torrent_abb(book, test=True), "AudioBookBay"

    if name.startswith('gen_'):
        for provider in CONFIG.providers('GEN'):
            if provider['NAME'].lower() == name:
                if provider['DISPNAME']:
                    name = provider['DISPNAME']
                logger.debug(f"Testing {name}")
                if host:
                    provider['HOST'] = host
                if api:
                    provider['SEARCH'] = api
                return direct_gen(book, prov=provider['NAME'].lower(), test=True), name

    if name == 'BOK':
        if host:
            CONFIG.set_str('BOK_HOST', host)
        if api:
            email, pwd, langs = api.split(' : ')
            CONFIG.set_str('BOK_EMAIL', email)
            CONFIG.set_str('BOK_PASS', pwd)
            CONFIG.set_str('BOK_SEARCH_LANG', langs)
        logger.debug(f"Testing provider {name}:{CONFIG['BOK_HOST']}")
        return direct_bok(book, prov=name, test=True), "ZLibrary"

    if name.startswith('rss_'):
        try:
            for provider in CONFIG.providers('RSS'):
                if provider['NAME'].lower() == name:
                    if provider['DISPNAME']:
                        name = provider['DISPNAME']
                    logger.debug(f"Testing provider {name}")
                    label = provider['LABEL']
                    if not host:
                        host = provider['HOST']
                    if 'goodreads' in host:
                        if 'list_rss' in host:
                            return goodreads(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                             provider['DISPNAME'], test=True), provider['DISPNAME']
                        if '/show/' in host or '/book/' in host:
                            # goodreads listopia html page
                            return listopia(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                            provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'amazon' in host and '/charts' in host:
                        return amazon(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                      provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'nytimes' in host:
                        return ny_times(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                        provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'publishersweekly' in host:
                        return publishersweekly(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                                provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'apps.npr.org' in host:
                        return appsnprorg(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                          provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'penguinrandomhouse' in host:
                        return penguinrandomhouse(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                                  provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'barnesandnoble' in host:
                        return barnesandnoble(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                              provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'bookdepository' in host:
                        return bookdepository(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                              provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'indigo' in host:
                        return indigo(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                      provider['DISPNAME'], test=True), provider['DISPNAME']
                    elif 'mam' in label.lower() and 'wish' in label.lower():
                        return mam(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                   provider['DISPNAME'], test=True), provider['DISPNAME']
                    else:
                        return rss(host, provider['NAME'], provider.get_int('DLPRIORITY'),
                                   provider['DISPNAME'], test=True), provider['DISPNAME']
        except IndexError:
            pass
        except Exception as e:
            logger.debug(f"Exception: {str(e)}")

    if name.startswith('apprise_'):
        for provider in CONFIG.providers('APPRISE'):
            if provider['NAME'].lower() == name:
                if provider['DISPNAME']:
                    name = provider['DISPNAME']
                logger.debug(f"Testing notifier {name}")
                # noinspection PyUnresolvedReferences
                noti = lazylibrarian.notifiers.apprise_notify.AppriseNotifier()
                return noti.test_notify(host), name
        return False, name

    # for torznab/newznab get capabilities first, unless locked,
    # then try book search if enabled, fall back to general search
    if name.startswith('torznab_'):
        caps_changed = []
        try:
            for provider in CONFIG.providers('TORZNAB'):
                if provider['NAME'].lower() == name:
                    if provider['DISPNAME']:
                        name = provider['DISPNAME']
                    logger.debug(f"Testing provider {name}")
                    if provider.get_bool('MANUAL'):
                        logger.debug(f"Capabilities are set to manual for {provider['NAME']}")
                    else:
                        if host:
                            if host[-1:] == '/':
                                host = host[:-1]
                            provider['HOST'] = host
                        if api:
                            ap, seed = api.split(' : ', 1)
                            provider['API'] = ap
                            provider.set_int('SEEDERS', seed)

                        provider_copy = deepcopy(provider)
                        updated = get_capabilities(provider_copy, force=True)
                        if updated:
                            for item in provider:
                                if provider[item] != provider_copy[item]:
                                    caps_changed.append([provider, item, provider_copy[item]])

                    if provider['BOOKSEARCH']:
                        success, error_msg = newznab_plus(book, provider, 'book', 'torznab', True)
                        if not success and cancel_search_type('book', error_msg, provider):
                            caps_changed.append([provider, 'BOOKSEARCH', ''])
                            success, _ = newznab_plus(book, provider, 'generalbook', 'torznab', True)
                    else:
                        success, _ = newznab_plus(book, provider, 'generalbook', 'torznab', True)

                    return success, provider['DISPNAME']
        except IndexError:
            pass
        except Exception as e:
            logger.debug(f"Exception: {str(e)}")
        finally:
            for item in caps_changed:
                item[0][item[1]] = item[2]
            if caps_changed:
                CONFIG.save_config_and_backup_old(section='Capabilities')

    if name.startswith('newznab_'):
        caps_changed = []
        try:
            for provider in CONFIG.providers('NEWZNAB'):
                if provider['NAME'].lower() == name:
                    if provider['DISPNAME']:
                        name = provider['DISPNAME']
                    logger.debug(f"Testing provider {name}")
                    if provider.get_bool('MANUAL'):
                        logger.debug(f"Capabilities are set to manual for {provider['NAME']}")
                    else:
                        if host:
                            provider['HOST'] = host
                        if api:
                            provider['API'] = api

                        provider_copy = deepcopy(provider)
                        updated = get_capabilities(provider_copy, force=True)
                        if updated:
                            for item in provider:
                                if provider[item] != provider_copy[item]:
                                    caps_changed.append([provider, item, provider_copy[item]])

                    if provider['BOOKSEARCH']:
                        success, error_msg = newznab_plus(book, provider, 'book', 'newznab', True)
                        if not success and cancel_search_type('book', error_msg, provider):
                            caps_changed.append([provider, 'BOOKSEARCH', ''])
                            success, _ = newznab_plus(book, provider, 'generalbook', 'newznab', True)
                    else:
                        success, _ = newznab_plus(book, provider, 'generalbook', 'newznab', True)
                    return success, provider['DISPNAME']
        except IndexError:
            pass
        except Exception as e:
            logger.debug(f"Exception: {str(e)}")
        finally:
            for item in caps_changed:
                item[0][item[1]] = item[2]
            if caps_changed:
                CONFIG.save_config_and_backup_old(section='Capabilities')

    if name.startswith('irc_'):
        try:
            for provider in CONFIG.providers('IRC'):
                if provider['NAME'].lower() == name:
                    if provider['DISPNAME']:
                        name = provider['DISPNAME']
                        if host:
                            server, channel = host.split(' : ', 1)
                            provider['SERVER'] = server
                            provider['CHANNEL'] = channel
                        if api:
                            snick, ssearch = api.split(' : ', 1)
                            provider['BOTNICK'] = snick
                            provider['SEARCH'] = ssearch
                    logger.debug(f"Testing provider {name}")
                    filename = f"{book['searchterm']}.zip"
                    t = threading.Thread(target=irc_query, name='irc_query',
                                         args=(provider, filename, book['searchterm'], None, False,))
                    t.start()
                    t.join()

                    resultfile = os.path.join(DIRS.CACHEDIR, "IRCCache", filename)
                    if path_isfile(resultfile):
                        results = irc_results(provider, resultfile)
                        logger.debug(f"Found {len(results)} results")
                        logger.debug(f"Removing File: {resultfile}")
                        remove_file(resultfile)  # remove the test search .zip
                        return len(results), name
                    return False, name
        except IndexError:
            pass
        except Exception as e:
            logger.debug(f"Exception: {str(e)}")

    if name == 'SLSK':
        logger.debug(f"Testing provider {name}")
        if host:
            CONFIG.set_str('SLSK_HOST', host)
        if api:
            CONFIG.set_str('SLSK_API', api)
        return slsk_search(book, test=True), "SLSK"

    if name == 'ANNA':
        logger.debug(f"Testing provider {name}")
        if host:
            CONFIG.set_str('ANNA_HOST', host)
        if api:
            CONFIG.set_str('ANNA_KEY', api)
        return anna_search(book, test=True), "ANNA"

    msg = f"Unknown provider [{name}]"
    logger.error(msg)
    return False, msg


def get_searchterm(book, search_type):
    authorname = clean_name(book['authorName'], "'")
    bookname = clean_name(book['bookName'], "'")
    if search_type in ['book', 'audio'] or 'short' in search_type:
        if bookname == authorname and book['bookSub']:
            # books like "Spike Milligan: Man of Letters"
            # where we split the title/subtitle on ':'
            bookname = clean_name(book['bookSub'], "'")
        if bookname.startswith(authorname) and len(bookname) > len(authorname):
            # books like "Spike Milligan In his own words"
            # where we don't want to look for "Spike Milligan Spike Milligan In his own words"
            bookname = bookname[len(authorname) + 1:]
        bookname = bookname.strip()

        # no initials or extensions after surname eg L. E. Modesitt Jr. -> Modesitt
        # and Charles H. Elliott, Phd -> Charles Elliott
        # but Tom Holt -> Tom Holt
        # Calibre directories may have trailing '.' replaced by '_'  eg Jr_
        authorname = authorname.replace('.', ' ').replace('_', ' ')
        if ' ' in authorname:
            authorname_exploded = authorname.split()
            authorname = ''
            postfix = get_list(CONFIG['NAME_POSTFIX'])
            for word in authorname_exploded:
                # word = word.rstrip('.').rstrip('_')
                if len(word) > 1 and word.lower() not in postfix:
                    if authorname:
                        authorname += ' '
                    authorname += word

        if 'short' in search_type and '(' in bookname:
            bookname = bookname.split('(')[0].strip()

    bookname = bookname.replace('#', '_').replace('/', '_')
    return authorname, bookname


def get_capabilities(provider: ConfigDict, force=False):
    """
    query provider for caps if none loaded yet, or if config entry is too old and not set manually.
    """
    logger = logging.getLogger(__name__)
    if not force and len(provider['UPDATED']) == 10:  # any stored values?
        updated = False
        if (age(provider['UPDATED']) > CONFIG.get_int('CACHE_AGE')) and not provider.get_bool('MANUAL'):
            logger.debug(f"Stored capabilities for {provider['HOST']} are too old")
            updated = True
    else:
        updated = True

    if not updated:
        logger.debug(f"Using stored capabilities for {provider['HOST']}")
    else:
        host = provider['HOST']
        if str(host[:4]) != "http":
            host = f"http://{host}"
        if host[-1:] == '/':
            host = host[:-1]
        if host[-4:] == '/api':
            url = f"{host}?t=caps"
        else:
            url = f"{host}/api?t=caps"

        # most providers will give you caps without an api key
        logger.debug(f'Requesting capabilities for {url}')
        source_xml, success = fetch_url(url, retry=False, raw=True)
        data = None
        if not success:
            logger.debug(f"Error getting xml from {url}, {source_xml}")
        else:
            try:
                data = ElementTree.fromstring(source_xml)
                if data.tag == 'error':
                    logger.debug(f"Unable to get capabilities: {data.attrib}")
                    success = False
            except (ElementTree.ParseError, UnicodeEncodeError):
                logger.debug(f"Error parsing xml from {url}, {repr(source_xml)}")
                success = False
        if not success:
            # If it failed, retry with api key
            if provider['API']:
                url = f"{url}&apikey={provider['API']}"
                logger.debug(f'Retrying capabilities with apikey for {url}')
                source_xml, success = fetch_url(url, raw=True)
                if not success:
                    logger.debug(f"Error getting xml from {url}, {source_xml}")
                else:
                    try:
                        data = ElementTree.fromstring(source_xml)
                        if data.tag == 'error':
                            logger.debug(f"Unable to get capabilities: {data.attrib}")
                            success = False
                    except (ElementTree.ParseError, UnicodeEncodeError):
                        logger.debug(f"Error parsing xml from {url}, {repr(source_xml)}")
                        success = False
            else:
                logger.debug(f'Unable to retry capabilities, no apikey for {url}')
        if not success:
            logger.warning(f"Unable to get capabilities for {url}: No data returned")
            # might be a temporary error
            if provider['BOOKCAT'] or provider['MAGCAT'] or provider['AUDIOCAT']:
                logger.debug(f"Using old stored capabilities for {provider['HOST']}")
            else:
                # or might be provider doesn't do caps
                logger.debug(f"Using default capabilities for {provider['HOST']}")
                for key in ['GENERALSEARCH', 'EXTENDED', 'BOOKCAT', 'AUDIOCAT', 'COMICCAT', 'MAGCAT',
                            'BOOKSEARCH', 'MAGSEARCH', 'AUDIOSEARCH', 'COMICSEARCH']:
                    item = provider.get_item(key)
                    if item:
                        item.reset_to_default()
                provider['UPDATED'] = str(today)
                provider.set_int('APILIMIT', 0)
                provider.set_int('RATELIMIT', 0)
                # CONFIG.save_config_and_backup_old(section=provider['NAME'])
        elif data is not None:
            logger.debug(f"Parsing xml for capabilities of {url}")
            if xmltodict:
                dlcommslogger = logging.getLogger('special.dlcomms')
                data_dict = xmltodict.parse(source_xml)
                dlcommslogger.debug(data_dict)
            # book search isn't mentioned in the caps xml returned by
            # nzbplanet,jackett,oznzb,usenet-crawler, so we can't use it as a test
            # but the newznab+ ones usually support t=book and categories in 7000 range
            # whereas nZEDb ones don't support t=book and use categories in 8000 range
            # also some providers give searchtype but no supportedparams, so we still
            # can't tell what queries will be accepted
            # also category names can be lowercase or Mixed, magazine subcat name isn't
            # consistent, and subcat can be just subcat or category/subcat subcat > lang
            # eg "Magazines" "Mags" or "Books/Magazines" "Mags > French"
            # Load all languages for now as we don't know which the user might want
            #
            #  set some defaults
            #
            for key in ['GENERALSEARCH', 'EXTENDED', 'BOOKCAT', 'AUDIOCAT', 'COMICCAT', 'MAGCAT',
                        'BOOKSEARCH', 'MAGSEARCH', 'AUDIOSEARCH', 'COMICSEARCH']:
                item = provider.get_item(key)
                if item:
                    item.reset_to_default()
            search = data.find('searching/search')
            if search is not None and 'available' in search.attrib and search.attrib['available'] == 'yes':
                provider['GENERALSEARCH'] = 'search'
            limits = data.find('limits')
            if limits is not None:
                try:
                    limit = limits.attrib.get('apimax')
                    if limit:
                        provider.set_int('APILIMIT', check_int(limit, 0))
                        logger.debug(f"{provider['HOST']} apilimit {limit}")
                except Exception as e:
                    logger.debug(f"Error getting apilimit from {provider['HOST']}: {type(e).__name__} {str(e)}")

            categories = data.iter('category')
            ebooksubs = ''
            magsubs = ''
            comicsubs = ''
            audiosubs = ''
            for cat in categories:
                if 'name' in cat.attrib:
                    if cat.attrib['name'].lower().startswith('audio'):
                        if not provider['AUDIOCAT']:
                            provider['AUDIOCAT'] = cat.attrib['id']
                        subcats = cat.iter('subcat')
                        if not subcats:
                            subcats = cat.iter('subCategories')
                        for subcat in subcats:
                            if 'audiobook' in subcat.attrib['name'].lower():
                                if audiosubs:
                                    audiosubs += ','
                                audiosubs += subcat.attrib['id']
                    elif cat.attrib['name'].lower().startswith('books'):
                        if not provider['BOOKCAT']:
                            provider['BOOKCAT'] = cat.attrib['id']
                        # if no specific magazine/comic subcategory, use books
                        if not provider['MAGCAT']:
                            provider['MAGCAT'] = cat.attrib['id']
                        if not provider['COMICCAT']:
                            provider['COMICCAT'] = cat.attrib['id']
                        # set default booksearch
                        if provider['BOOKCAT'] == '7000':
                            # looks like newznab+, should support book-search
                            provider['BOOKSEARCH'] = 'book'
                        else:
                            # looks like nZEDb, probably no book-search
                            provider['BOOKSEARCH'] = ''  # but check in case we got some settings back
                        search = data.find('searching/book-search')
                        if search is not None and 'available' in search.attrib:
                            # noinspection PyUnresolvedReferences
                            if (search.attrib['available'] == 'yes' and
                                    ('supportedParams' not in search.attrib or
                                     ('author' in search.attrib['supportedParams'] and
                                      'title' in search.attrib['supportedParams']))):
                                # only use book search if author and title are supported
                                # (if supportedParams are specified)
                                provider['BOOKSEARCH'] = 'book'
                            else:
                                provider['BOOKSEARCH'] = ''
                        # subcategories override main category (not in addition to)
                        # but allow multile subcategories (mags->english, mags->french)
                        subcats = cat.iter('subcat')
                        if not subcats:
                            subcats = cat.iter('subCategories')
                        for subcat in subcats:
                            if 'ebook' in subcat.attrib['name'].lower():
                                if ebooksubs:
                                    ebooksubs += ','
                                ebooksubs += subcat.attrib['id']
                            if 'magazines' in subcat.attrib['name'].lower() or 'mags' in subcat.attrib['name'].lower():
                                if magsubs:
                                    magsubs += ','
                                magsubs += subcat.attrib['id']
                            if 'comic' in subcat.attrib['name'].lower():
                                if comicsubs:
                                    comicsubs += ','
                                comicsubs += subcat.attrib['id']
            if ebooksubs:
                provider['BOOKCAT'] = ebooksubs
            if magsubs:
                provider['MAGCAT'] = magsubs
            if comicsubs:
                provider['COMICCAT'] = comicsubs
            if audiosubs:
                provider['AUDIOCAT'] = audiosubs
            logger.info(
                f"Categories: Books {provider['BOOKCAT']} : Mags {provider['MAGCAT']} : Audio "
                f"{provider['AUDIOCAT']} : Comic {provider['COMICCAT']} : BookSearch '{provider['BOOKSEARCH']}'")
            provider['UPDATED'] = today()
            # CONFIG.save_config_and_backup_old(section=provider['NAME'])
    return updated


def iterate_over_znab_sites(book=None, search_type=None):
    """
    Purpose of this function is to read the config file, and loop through all active NewsNab+ and Torznab
    sites and return the compiled results list from all sites back to the caller
    We get called with book[] and searchType of "book", "mag", "general" etc
    """

    logger = logging.getLogger(__name__)
    iterateproviderslogger = logging.getLogger('special.iterateproviders')
    iterateproviderslogger.debug(f"ZNAB: Book:{book}, SearchType:{search_type}")
    resultslist = []
    providers = 0
    last_used = []
    api_count = []
    try:
        for prov in CONFIG.providers('NEWZNAB'):
            provider = deepcopy(prov)
            dispname = provider['DISPNAME']
            if not dispname:
                dispname = provider['HOST']
            iterateproviderslogger.debug(f"DLTYPES: {dispname}: {bool(provider['ENABLED'])} {provider['DLTYPES']}")
            if provider['ENABLED'] and search_type:
                ignored = False
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f'{dispname} is BLOCKED')
                    ignored = True
                elif "book" in search_type and 'E' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {dispname} for eBook")
                    ignored = True
                elif "audio" in search_type and 'A' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {dispname} for AudioBook")
                    ignored = True
                elif "mag" in search_type and 'M' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {dispname} for Magazine")
                    ignored = True
                elif "comic" in search_type and 'C' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {dispname} for Comic")
                    ignored = True
                if not ignored:
                    if provider.get_int('APILIMIT'):
                        if 'APICOUNT' in provider:
                            res = provider.get_int('APICOUNT')
                        else:
                            res = 0
                        if res >= provider.get_int('APILIMIT'):
                            BLOCKHANDLER.block_provider(provider['HOST'],
                                                        f"Reached Daily API limit ({provider['APILIMIT']})",
                                                        delay=seconds_to_midnight())
                        else:
                            api_count.append([prov, res + 1])

                    if not BLOCKHANDLER.is_blocked(provider['HOST']):
                        ratelimit = provider.get_int('RATELIMIT')
                        if ratelimit:
                            if provider.get_int('LASTUSED') > 0:
                                delay = provider.get_int('LASTUSED') + ratelimit - time.time()
                                if delay > 0:
                                    time.sleep(delay)
                            last_used.append([prov, int(time.time())])

                        providers += 1
                        logger.debug(f'Querying provider {dispname}')
                        resultslist += newznab_plus(book, provider, search_type, "nzb")[1]
    except RuntimeError:
        logger.debug("Error iterating newznab")

    for item in last_used:
        logger.debug(f"Updating LASTUSED for {item[0]['NAME']}")
        item[0].set_int('LASTUSED', item[1])
    for item in api_count:
        logger.debug(f"Updating APICOUNT for {item[0]['NAME']}")
        item[0].set_int('APICOUNT', item[1])

    last_used = []
    api_count = []
    try:
        for prov in CONFIG.providers('TORZNAB'):
            provider = deepcopy(prov)
            dispname = provider['DISPNAME']
            if not dispname:
                dispname = provider['HOST']
            iterateproviderslogger.debug(f"DLTYPES: {dispname}: {bool(provider['ENABLED'])} {provider['DLTYPES']}")
            if provider['ENABLED'] and search_type:
                ignored = False
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f'{dispname} is BLOCKED')
                    ignored = True
                elif 'book' in search_type and 'E' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {dispname} for eBook")
                    ignored = True
                elif "audio" in search_type and 'A' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {dispname} for AudioBook")
                    ignored = True
                elif "mag" in search_type and 'M' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {dispname} for Magazine")
                    ignored = True
                elif "comic" in search_type and 'C' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {dispname} for Comic")
                    ignored = True
                if not ignored:
                    if provider.get_int('APILIMIT'):
                        if 'APICOUNT' in provider:
                            res = provider.get_int('APICOUNT')
                        else:
                            res = 0
                        if res >= provider.get_int('APILIMIT'):
                            BLOCKHANDLER.block_provider(provider['HOST'],
                                                        f"Reached Daily API limit ({provider['APILIMIT']})",
                                                        delay=seconds_to_midnight())
                        else:
                            api_count.append([prov, res + 1])

                    if not BLOCKHANDLER.is_blocked(provider['HOST']):
                        ratelimit = provider.get_int('RATELIMIT')
                        if ratelimit:
                            if provider.get_int('LASTUSED') > 0:
                                delay = provider.get_int('LASTUSED') + ratelimit - time.time()
                                if delay > 0:
                                    time.sleep(delay)
                            last_used.append([prov, int(time.time())])

                        providers += 1
                        logger.debug(f'Querying provider {dispname}')
                        resultslist += newznab_plus(book, provider, search_type, "torznab")[1]
    except RuntimeError:
        logger.debug("Error iterating torznab")

    for item in last_used:
        logger.debug(f"Updating LASTUSED for {item[0]['NAME']}")
        item[0].set_int('LASTUSED', item[1])
    for item in api_count:
        logger.debug(f"Updating APICOUNT for {item[0]['NAME']}")
        item[0].set_int('APICOUNT', item[1])

    return resultslist, providers


def iterate_over_torrent_sites(book=None, search_type=None):
    logger = logging.getLogger(__name__)
    iterateproviderslogger = logging.getLogger('special.iterateproviders')
    iterateproviderslogger.debug(f"Torrents: Book:{book}, SearchType:{search_type}")
    resultslist = []
    providers = 0

    if search_type and search_type not in ['mag', 'comic'] and not search_type.startswith('general'):
        authorname, bookname = get_searchterm(book, search_type)
        if 'title' in search_type:
            book['searchterm'] = bookname
        else:
            book['searchterm'] = f"{authorname} {bookname}"

    for prov in ['KAT', 'TPB', 'TDL', 'LIME', 'ABB']:
        iterateproviderslogger.debug(f"DLTYPES: {prov}: {CONFIG[prov]} {CONFIG[prov + '_DLTYPES']}")
        if CONFIG[prov]:
            ignored = False
            if BLOCKHANDLER.is_blocked(prov):
                logger.debug(f'{CONFIG[prov + "_HOST"]} is BLOCKED')
                ignored = True
            elif search_type in ['book', 'shortbook', 'titlebook'] and \
                    'E' not in CONFIG[f"{prov}_DLTYPES"]:
                logger.debug(f"Ignoring {prov} for eBook")
                ignored = True
            elif "audio" in search_type and 'A' not in CONFIG[f"{prov}_DLTYPES"]:
                logger.debug(f"Ignoring {prov} for AudioBook")
                ignored = True
            elif "mag" in search_type and 'M' not in CONFIG[f"{prov}_DLTYPES"]:
                logger.debug(f"Ignoring {prov} for Magazine")
                ignored = True
            elif "comic" in search_type and 'C' not in CONFIG[f"{prov}_DLTYPES"]:
                logger.debug(f"Ignoring {prov} for Comic")
                ignored = True
            if not ignored:
                logger.debug(f'[iterate_over_torrent_sites] - {CONFIG[prov + "_HOST"]}')
                if prov == 'KAT':
                    results, error = torrent_kat(book)
                elif prov == 'TPB':
                    results, error = torrent_tpb(book)
                elif prov == 'TDL':
                    results, error = torrent_tdl(book)
                elif prov == 'LIME':
                    results, error = torrent_lime(book)
                elif prov == 'ABB':
                    results, error = torrent_abb(book)
                else:
                    results = ''
                    error = ''
                    logger.error(f'iterate_over_torrent_sites called with unknown provider [{prov}]')

                if error:
                    BLOCKHANDLER.block_provider(prov, error)
                else:
                    resultslist += results
                    providers += 1

    return resultslist, providers


def iterate_over_direct_sites(book=None, search_type=None):
    logger = logging.getLogger(__name__)
    iterateproviderslogger = logging.getLogger('special.iterateproviders')
    iterateproviderslogger.debug(f"Direct: Book:{book}, SearchType:{search_type}")
    resultslist = []
    providers = 0
    book['searchtype'] = search_type
    if search_type not in ['mag', 'comic'] and not search_type.startswith('general'):
        authorname, bookname = get_searchterm(book, search_type)
        if 'title' in search_type:
            book['searchterm'] = bookname
        else:
            book['searchterm'] = f"{authorname} {bookname}"
    try:
        for provider in CONFIG.providers('GEN'):
            prov = deepcopy(provider)
            iterateproviderslogger.debug(f"DLTYPES: {prov['NAME']}: {prov['ENABLED']} {prov['DLTYPES']}")
            if prov.get_bool('ENABLED'):
                ignored = False
                if BLOCKHANDLER.is_blocked(prov['NAME']):
                    logger.debug(f"{prov['NAME']} is BLOCKED")
                    ignored = True
                elif search_type in ['book', 'shortbook', 'titlebook'] and 'E' not in prov['DLTYPES']:
                    logger.debug(f"Ignoring {prov['NAME']} for eBook")
                    ignored = True
                elif "audio" in search_type and 'A' not in prov['DLTYPES']:
                    logger.debug(f"Ignoring {prov['NAME']} for AudioBook")
                    ignored = True
                elif "mag" in search_type and 'M' not in prov['DLTYPES']:
                    logger.debug(f"Ignoring {prov['NAME']} for Magazine")
                    ignored = True
                elif "comic" in search_type and 'C' not in prov['DLTYPES']:
                    logger.debug(f"Ignoring {prov['NAME']} for Comic")
                    ignored = True
                if not ignored:
                    logger.debug(f"Querying {prov['NAME']}")
                    results, error = direct_gen(book, prov['NAME'])
                    if error:
                        BLOCKHANDLER.block_provider(prov['NAME'], error)
                    else:
                        resultslist += results
                        providers += 1
    except RuntimeError:
        logger.debug("Error iterating gen")

    prov = 'BOK'
    if CONFIG[prov]:
        iterateproviderslogger.debug(f"DLTYPES: {prov}: {CONFIG[prov]} {CONFIG[prov + '_DLTYPES']}")
        ignored = False
        if BLOCKHANDLER.is_blocked('zlibrary'):
            logger.debug('zlibrary is BLOCKED')
            ignored = True
        elif search_type in ['book', 'shortbook', 'titlebook'] and \
                'E' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for eBook")
            ignored = True
        elif "audio" in search_type and 'A' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for AudioBook")
            ignored = True
        elif "mag" in search_type and 'M' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for Magazine")
            ignored = True
        elif "comic" in search_type and 'C' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for Comic")
            ignored = True
        if not ignored:
            logger.debug(f'Querying {prov}')
            results, error = direct_bok(book, prov)
            if error:
                # use a short delay for site unavailable etc
                delay = CONFIG.get_int('BLOCKLIST_TIMER')
                dl_limit = CONFIG.get_int('BOK_DLLIMIT')
                count = lazylibrarian.TIMERS['BOK_TODAY']
                if count and count >= dl_limit:
                    # rolling 24hr delay if limit reached
                    grabs, oldest = bok_grabs()
                    delay = oldest + 24 * 60 * 60 - time.time()
                    error = f"Reached Daily download limit ({grabs}/{dl_limit})"
                BLOCKHANDLER.block_provider('zlibrary', error, delay=delay)
            else:
                resultslist += results
                providers += 1

    prov = 'SLSK'
    if CONFIG[prov]:
        iterateproviderslogger.debug(f"DLTYPES: {prov}: {CONFIG[prov]} {CONFIG[prov + '_DLTYPES']}")
        provider = 'soulseek'
        ignored = False
        if BLOCKHANDLER.is_blocked(provider):
            logger.debug('soulseek is BLOCKED')
            ignored = True
        elif search_type in ['book', 'shortbook', 'titlebook'] and \
                'E' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for eBook")
            ignored = True
        elif "audio" in search_type and 'A' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for AudioBook")
            ignored = True
        elif "mag" in search_type and 'M' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for Magazine")
            ignored = True
        elif "comic" in search_type and 'C' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for Comic")
            ignored = True
        if not ignored:
            logger.debug(f'Querying {search_type} {provider}')
            searchtype = 'audio' if 'audio' in search_type else 'ebook'
            results, error = slsk_search(book, searchtype=searchtype)
            if error:
                # use a short delay for site unavailable etc
                delay = CONFIG.get_int('BLOCKLIST_TIMER')
                BLOCKHANDLER.block_provider(provider, error, delay=delay)
            else:
                resultslist += results
                providers += 1

    prov = 'ANNA'
    if CONFIG[prov]:
        iterateproviderslogger.debug(f"DLTYPES: {prov}: {CONFIG[prov]} {CONFIG[prov + '_DLTYPES']}")
        provider = 'annas'
        ignored = False
        if BLOCKHANDLER.is_blocked(provider):
            logger.debug('annas is BLOCKED')
            ignored = True
        elif search_type in ['book', 'shortbook', 'titlebook'] and \
                'E' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for eBook")
            ignored = True
        elif "audio" in search_type and 'A' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for AudioBook")
            ignored = True
        elif "mag" in search_type and 'M' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for Magazine")
            ignored = True
        elif "comic" in search_type and 'C' not in CONFIG[f"{prov}_DLTYPES"]:
            logger.debug(f"Ignoring {prov} for Comic")
            ignored = True
        if not ignored:
            logger.debug(f'Querying {provider}')
            searchtype = 'ebook'
            if 'audio' in search_type:
                searchtype = 'audio'
            if 'mag' in search_type:
                searchtype = 'mag'
            results, error = anna_search(book, searchtype)
            if error:
                dl_limit = CONFIG.get_int('ANNA_DLLIMIT')
                count = lazylibrarian.TIMERS['ANNA_REMAINING']
                if dl_limit and count <= 0:
                    lazylibrarian.TIMERS['ANNA_REMAINING'] = 0
                    block_annas(dl_limit)
                else:
                    # use a short delay for site unavailable etc
                    BLOCKHANDLER.block_provider(provider, error, delay=CONFIG.get_int('BLOCKLIST_TIMER'))
            else:
                resultslist += results
                providers += 1

    return resultslist, providers


def iterate_over_rss_sites():
    logger = logging.getLogger(__name__)
    iterateproviderslogger = logging.getLogger('special.iterateproviders')
    resultslist = []
    providers = 0
    dltypes = ''
    for provider in CONFIG.providers('RSS'):
        iterateproviderslogger.debug(
            f"DLTYPES: {provider['DISPNAME']}: {provider['ENABLED']} {provider['DLTYPES']} {provider['LABEL']}")
        if provider['ENABLED'] and not wishlist_type(provider['HOST']):
            if BLOCKHANDLER.is_blocked(provider['HOST']):
                logger.debug(f"{provider['HOST']} is BLOCKED")
            else:
                providers += 1
                logger.debug(f"[iterate_over_rss_sites] - {provider['HOST']}")
                resultslist += rss(provider['HOST'], provider['NAME'], provider.get_int('DLPRIORITY'),
                                   provider['DISPNAME'], provider['DLTYPES'], False, provider['LABEL'])
                dltypes += provider['DLTYPES']

    return resultslist, providers, ''.join(set(dltypes))


def iterate_over_wishlists():
    logger = logging.getLogger(__name__)
    iterateproviderslogger = logging.getLogger('special.iterateproviders')
    resultslist = []
    providers = 0
    for provider in CONFIG.providers('RSS'):
        iterateproviderslogger.debug(
            f"DLTYPES: {provider['DISPNAME']}: {provider['ENABLED']} {provider['DLTYPES']} {provider['LABEL']}")
        if provider['ENABLED']:
            wishtype = wishlist_type(provider['HOST'])
            if wishtype == 'goodreads':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += goodreads(provider['HOST'], provider['NAME'],
                                             provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                             provider['DLTYPES'], False, provider['LABEL'])
            elif wishtype == 'listopia':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += listopia(provider['HOST'], provider['NAME'],
                                            provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                            provider['DLTYPES'], False, provider['LABEL'])
            elif wishtype == 'amazon':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += amazon(provider['HOST'], provider['NAME'],
                                          provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                          provider['DLTYPES'], False, provider['LABEL'])
            elif wishtype == 'ny_times':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += ny_times(provider['HOST'], provider['NAME'],
                                            provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                            provider['DLTYPES'], False, provider['LABEL'])
            elif wishtype == 'publishersweekly':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += publishersweekly(provider['HOST'], provider['NAME'],
                                                    provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                                    provider['DLTYPES'], False, provider['LABEL'])

            elif wishtype == 'apps.npr.org':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += appsnprorg(provider['HOST'], provider['NAME'],
                                              provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                              provider['DLTYPES'], False, provider['LABEL'])

            elif wishtype == 'penguinrandomhouse':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += penguinrandomhouse(provider['HOST'], provider['NAME'],
                                                      provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                                      provider['DLTYPES'], False, provider['LABEL'])
            elif wishtype == 'barnesandnoble':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += barnesandnoble(provider['HOST'], provider['NAME'],
                                                  provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                                  provider['DLTYPES'], False, provider['LABEL'])

            elif wishtype == 'bookdepository':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += bookdepository(provider['HOST'], provider['NAME'],
                                                  provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                                  provider['DLTYPES'], False, provider['LABEL'])
            elif wishtype == 'indigo':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += indigo(provider['HOST'], provider['NAME'],
                                          provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                          provider['DLTYPES'], False, provider['LABEL'])
            elif wishtype == 'myanonamouse':
                if BLOCKHANDLER.is_blocked(provider['HOST']):
                    logger.debug(f"{provider['HOST']} is BLOCKED")
                else:
                    providers += 1
                    logger.debug(f"[iterate_over_wishlists] - {provider['HOST']}")
                    resultslist += mam(provider['HOST'], provider['NAME'],
                                       provider.get_int('DLPRIORITY'), provider['DISPNAME'],
                                       provider['DLTYPES'], False, provider['LABEL'])
            else:
                logger.debug(f"Unrecognised wishlist {wishtype} for {provider['HOST']}")

    return resultslist, providers


def iterate_over_irc_sites(book=None, search_type=None):
    logger = logging.getLogger(__name__)
    iterateproviderslogger = logging.getLogger('special.iterateproviders')
    resultslist = []
    providers = 0
    try:
        for provider in CONFIG.providers('IRC'):
            iterateproviderslogger.debug(
                f"DLTYPES: {provider['DISPNAME']}: {provider['ENABLED']} {provider['DLTYPES']}")
            if provider['ENABLED']:
                ignored = False
                if BLOCKHANDLER.is_blocked(provider['SERVER']):
                    logger.debug(f"{provider['SERVER']} is BLOCKED")
                    ignored = True
                elif search_type in ['book', 'shortbook', 'titlebook'] and 'E' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {provider['DISPNAME']} for eBook")
                    ignored = True
                elif "audio" in search_type and 'A' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {provider['DISPNAME']} for AudioBook")
                    ignored = True
                elif "mag" in search_type and 'M' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {provider['DISPNAME']} for Magazine")
                    ignored = True
                elif "comic" in search_type and 'M' not in provider['DLTYPES']:
                    logger.debug(f"Ignoring {provider['DISPNAME']} for Comic")
                    ignored = True
                elif not search_type or 'general' in search_type:
                    logger.debug(f"Ignoring {provider['DISPNAME']} for General search")
                    ignored = True
                if not ignored:
                    providers += 1
                    logger.debug(f"[iterate_over_irc_sites] - {provider['SERVER']}")
                    # For irc search we use just the author name and cache the results
                    # so we can search long and short from the same resultset
                    # but allow a separate "title only" search
                    # irchighway says search results without both author and title will be
                    # silently rejected but that doesn't seem to be actioned...
                    authorname, bookname = get_searchterm(book, search_type)
                    if 'title' in search_type:
                        book['searchterm'] = bookname
                    else:
                        book['searchterm'] = authorname
                    logger.debug(f"Searching {provider['DISPNAME']}:{provider['CHANNEL']} for {book['searchterm']}")

                    myhash = md5_utf8(provider['SERVER'] + provider['CHANNEL'] + book['searchterm'])
                    t = threading.Thread(target=irc_query, name='irc_query', args=(provider, f"{myhash}.irc",
                                                                                   book['searchterm'], None, True,))
                    t.start()
                    t.join()

                    hashfilename = os.path.join(DIRS.CACHEDIR, "IRCCache", f"{myhash}.irc")
                    if path_isfile(hashfilename):
                        results = irc_results(provider, hashfilename)
                        resultslist += results
    except Exception as e:
        logger.error(str(e))

    return resultslist, providers


def ny_times(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    ny_times best-sellers query function, return all the results in a list
    """
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"

    url = host
    provider = host.split('best-sellers')[1].strip('/')
    if provider:
        provider = provider.split('/')[0]
    else:
        provider = 'best-sellers'
    if not dispname:
        dispname = provider

    result, success = fetch_url(url)

    if not success:
        logger.error(f'Error fetching data from {url}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(basehost, result)

    elif result:
        logger.debug(f'Parsing results from {url}')
        data = result.split('itemProp="itemListElement"')
        for entry in data[1:]:
            try:
                title = make_unicode(entry.split('itemProp="name">')[1].split('<')[0])
                author_name = make_unicode(entry.split('itemProp="author">by ')[1].split('<')[0])
                author_name = author_name.split(' and ')[0].strip()  # multi-author, use first one
                results.append({
                    'rss_prov': provider,
                    'rss_feed': feednr,
                    'rss_title': title,
                    'rss_author': author_name,
                    'rss_bookid': '',
                    'rss_isbn': '',
                    'priority': priority,
                    'dispname': dispname,
                    'types': types,
                    'label': label,
                })
            except IndexError:
                pass
    else:
        logger.debug(f'No data returned from {url}')

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    if test:
        return len(results)
    return results


def amazon(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    Amazon charts html page
    """
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"

    if '/charts/' in host:
        provider = host.split('/charts')[1]
    else:
        provider = host

    url = host
    result, success = fetch_url(url)
    if not success:
        logger.error(f'Error fetching data from {url}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(basehost, result)
    elif result:
        logger.debug(f'Parsing results from {url}')
        soup = BeautifulSoup(result, 'html5lib')
        authors = soup.find_all("div", {"class": "kc-rank-card-author"})
        titles = soup.find_all("div", {"class": "kc-rank-card-title"})

        if len(authors) == len(titles):
            res = []
            authnames = []
            for item in authors:
                authnames.append(item.get('title'))
            booknames = []
            for item in titles:
                booknames.append(item.text.replace('\n', '').strip())
            temp_res = list(zip(authnames, booknames, strict=True))
            # suppress blanks and duplicates
            for item in temp_res:
                if item[0] and item[1] and item not in res:
                    res.append(item)

            for item in res:
                results.append({
                    'rss_prov': provider,
                    'rss_feed': feednr,
                    'rss_title': item[1],
                    'rss_author': item[0],
                    'rss_bookid': '',
                    'rss_isbn': '',
                    'priority': priority,
                    'dispname': dispname,
                    'types': types,
                    'label': label,
                })

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    if test:
        return len(results)
    return results


def publishersweekly(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    publishersweekly best-sellers voir dans configLazy folder pour les commentaires
    """
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"

    url = host
    provider = host.split('/pw/nielsen/')[1].strip('.html')
    if provider:
        provider = provider.split('/')[0]
    else:
        provider = 'best-sellers'
    if not dispname:
        dispname = provider

    result, success = fetch_url(url)
    if not success:
        logger.error(f'Error fetching data from {url}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(basehost, result)
    elif result:
        logger.debug(f'Parsing results from {url}')
        data = result.split('class="nielsen-bookinfo"')
        for entry in data[1:]:
            try:
                title = make_unicode(entry.split('<div')[1])
                title = re.sub('<.*?>', '', title)
                title = title.split('">')[1].strip()
                author_name = make_unicode(entry.split('<div>')[1].split(', Author')[0])
                rss_isbn = make_unicode(entry.split('<div')[3].split('<br>')[1])
                author_name = author_name.split(' and ')[0].strip()  # multi-author, use first one
                results.append({
                    'rss_prov': provider,
                    'rss_feed': feednr,
                    'rss_title': title,
                    'rss_author': author_name,
                    'rss_bookid': '',
                    'rss_isbn': rss_isbn,
                    'priority': priority,
                    'dispname': dispname,
                    'types': types,
                    'label': label,
                })
            except IndexError:
                pass
    else:
        logger.debug(f'No data returned from {url}')
    if test:
        return len(results)
    return results


def appsnprorg(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    best-book aoos,npr.org
    """
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    booknames = []
    authnames = []
    if str(host)[:4] != "http":
        host = f"http://{host}"

    url = host
    provider = host.split('/best-books/')[0]
    if provider:
        provider = provider.split('/')[0]
    else:
        provider = 'apps.nprorg'
    if not dispname:
        dispname = provider

    try:
        year_url = url.split('year=')[1]
        url_to_json = f"{url.split('#')[0] + year_url}-detail.json"
        result, success = fetch_url(url_to_json)
    except IndexError as e:
        success = False
        result = str(e)

    if not success:
        logger.error(f'Error fetching data from {url}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(basehost, result)
    elif result:
        data = json.loads(result)
        res = []
        isbn = []
        for books in data:
            temp_dic = data[books]
            booknames.append(temp_dic["title"])
            authnames.append(temp_dic["author"])
            isbn.append(temp_dic["isbn"])
        temp_res = list(zip(authnames, booknames, isbn, strict=True))
        for item in temp_res:
            if item[0] and item[1] and item not in res:
                res.append(item)
        for item in res:
            results.append({
                'rss_prov': provider,
                'rss_feed': feednr,
                'rss_title': item[1],
                'rss_author': item[0],
                'rss_bookid': '',
                'rss_isbn': item[2],
                'priority': priority,
                'dispname': dispname,
                'types': types,
                'label': label,
            })

    else:
        logger.debug(f'No data returned from {url}')

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    if test:
        return len(results)
    return results


def penguinrandomhouse(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    penguinrandomhouse html page
    """
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"

    if '/books/' in host:
        provider = host.split('/books')[0]
    else:
        provider = "penguinrandomhouse"

    url = host
    result, success = fetch_url(url)
    if not success:
        logger.error(f'Error fetching data from {url}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(basehost, result)
    elif result:
        logger.debug(f'Parsing results from {url}')

        soup = BeautifulSoup(result, 'html5lib')
        data = soup.find_all(id="tmpl-indc")
        resultnumber = data[0].get('totalresults')
        if resultnumber:
            authnames = []
            booknames = []
            res = []
            book_cat = data[0].get('cat')
            # requesting ajax page
            url = (f"https://www.penguinrandomhouse.com/ajaxc/categories/books/?from=0&to={resultnumber}"
                   f"&contentId={book_cat.lower()}&elClass=book&dataType=html&catFilter=best-sellers"
                   f"&sortType=frontlistiest_onsale")
            # page of all the book
            result, success = fetch_url(url)
            soup = BeautifulSoup(result, 'html5lib')
            titles = soup.find_all('div', {'class': 'title'})
            authors = soup.find_all('div', {'class': 'contributor'})

            if len(titles) == len(authors):
                for item in authors:
                    tmp = item.text.strip()
                    if ' and ' in tmp:
                        if ',' in tmp:
                            authnames.append(tmp.split(',')[0].strip())
                        else:
                            authnames.append(tmp.split(' and ')[0].strip())
                    else:
                        authnames.append(tmp)
                for item in titles:
                    booknames.append(item.text.replace('\n', '').strip())
                temp_res = list(zip(authnames, booknames, strict=True))
                for item in temp_res:
                    if item[0] and item[1] and item not in res:
                        res.append(item)

                for item in res:
                    results.append({
                        'rss_prov': provider,
                        'rss_feed': feednr,
                        'rss_title': item[1],
                        'rss_author': item[0],
                        'rss_bookid': '',
                        'rss_isbn': '',
                        'priority': priority,
                        'dispname': dispname,
                        'types': types,
                        'label': label,
                    })
    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    if test:
        return len(results)
    return results


def barnesandnoble(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    Barneandnoble charts html page
    """
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"
    provider = "barnesandnoble"

    url = host
    result, success = fetch_url(url)
    if not success:
        logger.error(f'Error fetching data from {url}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(basehost, result)
    elif result:
        logger.debug(f'Parsing results from {url}')
        soup = BeautifulSoup(result, 'html5lib')
        titles = soup.find_all("div", {"class": "product-shelf-title product-info-title pt-xs"})
        authors = soup.find_all("div", {"class": "product-shelf-author pt-0 mt-1"})

        if len(authors) and len(authors) == len(titles):
            res = []
            authnames = []
            for item in authors:
                tmp = item.text.strip()
                if "," in tmp:
                    authnames.append(tmp.split(',')[0].split('by')[1].strip())
                else:
                    authnames.append(tmp.split('by')[1].strip())
            booknames = []
            for item in titles:
                booknames.append(str(item.contents[1]).split('title="')[1].split('"')[0].strip())
            temp_res = list(zip(authnames, booknames, strict=True))
            # suppress blanks and duplicates
            for item in temp_res:
                if item[0] and item[1] and item not in res:
                    res.append(item)

            for item in res:
                results.append({
                    'rss_prov': provider,
                    'rss_feed': feednr,
                    'rss_title': item[1],
                    'rss_author': item[0],
                    'rss_bookid': '',
                    'rss_isbn': '',
                    'priority': priority,
                    'dispname': dispname,
                    'types': types,
                    'label': label,
                })
    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    if test:
        return len(results)
    return results


def bookdepository(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    bookdepository
    """
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"
    provider = "bookdepository"
    page = 1
    next_page = True
    url = host

    while next_page:
        time.sleep(1)
        if 'page=' in host:
            host = str(url.split('page=')[0])
        if '?' in host:
            url = f"{host}&page={page}"
        else:
            url = f"{host}?page={page}"

        result, success = fetch_url(url)

        next_page = False
        if not success:
            logger.error(f'Error fetching data from {url}: {result}')
            if not test:
                BLOCKHANDLER.block_provider(basehost, result)
        elif result:
            logger.debug(f'Parsing results from {url}')
            soup = BeautifulSoup(result, 'html5lib')
            titles = soup.find_all("h3", {"class": "title"})
            authors = soup.find_all("p", {"class": "author"})
            if len(authors) and len(authors) == len(titles):
                # take the number of result to see when max page
                resultnumber = soup.find_all("div", {"class": "search-info"})
                actual_result = str(resultnumber[0].text).split()[3].strip()
                max_result = str(resultnumber[0].text).split()[5].strip()
                next_page = actual_result != max_result
                res = []
                authnames = []
                for item in authors:
                    authnames.append(item.text.strip())
                booknames = []
                for item in titles:
                    booknames.append(item.text.strip())
                temp_res = list(zip(authnames, booknames, strict=True))
                for item in temp_res:
                    if item[0] and item[1] and item not in res:
                        res.append(item)

                for item in res:
                    results.append({
                        'rss_prov': provider,
                        'rss_feed': feednr,
                        'rss_title': item[1],
                        'rss_author': item[0],
                        'rss_bookid': '',
                        'rss_isbn': '',
                        'priority': priority,
                        'dispname': dispname,
                        'types': types,
                        'label': label,
                    })

        if test:
            logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
            return len(results)

        page += 1

    return results


def indigo(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    indigo book list
    """
    # May have to check again, the api seems to return XML in browser from time to time,
    # may have to do a checkup to see if the result is xml
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"
    provider = "indigo"
    # The first page is 0
    page = 0
    next_page = True
    url = host

    result, success = fetch_url(url)
    if not success:
        logger.error(f'Error fetching data from {url}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(basehost, result)
    elif result:
        logger.debug(f'Parsing results from {url}')
        api = 'https://www.chapters.indigo.ca/en-ca/api/v1/merchandising/GetCmsProductList/?sortDirection=0'
        api += '&sortKey=Default&rangeLength=0&rangeStart=0&pageSize=12'
        list_id = re.findall(r'(?<="productLists":\[{"ContentID":).*?,', result)[0].split(',')[0]
        list_id = f"&id={str(list_id)}"
        while next_page:
            time.sleep(1)
            if '?' in api:
                urlapi = f"{api}&page={page}{list_id}"
            else:
                urlapi = f"{api}?page={page}{list_id}"

            # Response in List format
            apiresult, success = fetch_url(urlapi)
            soup = BeautifulSoup(apiresult, 'html5lib')
            # Test if page result is empty
            if soup.text.strip() == "[]" or soup.text.strip() == "<feff>":
                return len(results)
            next_page = True

            # replace weird character , delete new line
            apiresult = apiresult.replace('\n', '').strip().replace(':', ': ').replace("“", "'").replace("”", "'")

            # conver to list of dict
            apilist = list(eval(apiresult))

            # List containt word without quote
            titles = []
            authors = []
            isbn = []
            for item in apilist:
                tmp_dic = item
                titles.append(tmp_dic['FullTitle'])
                authors.append(tmp_dic['MajorContributorName'])
                isbn.append(tmp_dic['ExternalProductId'])

            if len(authors) == len(titles):
                res = []
                temp_res = list(zip(authors, titles, isbn, strict=True))
                # suppress blanks and duplicates
                for item in temp_res:
                    if item[0] and item[1] and item not in res:
                        res.append(item)

                for item in res:
                    results.append({
                        'rss_prov': provider,
                        'rss_feed': feednr,
                        'rss_title': item[1],
                        'rss_author': item[0],
                        'rss_bookid': '',
                        'rss_isbn': item[2],
                        'priority': priority,
                        'dispname': dispname,
                        'types': types,
                        'label': label,
                    })
            page += 1
            if test:
                logger.debug(f"Test found {len(results)} {plural(len(results), 'result')} from {host}")
                return len(results)
    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    return results


def listopia(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    Goodreads Listopia query function, return all the results in a list
    """
    logger = logging.getLogger(__name__)
    results = []
    maxpage = priority
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"

    page = 1
    next_page = True
    if '/show/' in host:
        provider = host.split('/show/')[1]
    elif '/book/' in host:
        provider = host.split('/book/')[1]
    else:
        provider = host

    if not dispname:
        dispname = provider

    while next_page:
        if '?' in host:
            url = f"{host}&page={page}"
        else:
            url = f"{host}?page={page}"

        result, success = fetch_url(url)

        next_page = False

        if not success:
            logger.error(f'Error fetching data from {url}: {result}')
            if not test:
                BLOCKHANDLER.block_provider(basehost, result)
        elif result:
            logger.debug(f'Parsing results from {url}')
            data = result.split('<td valign="top" class="number">')
            for entry in data[1:]:
                try:
                    # index = entry.split('<')[0]
                    title = make_unicode(entry.split('<a title="')[1].split('"')[0])
                    book_id = entry.split('data-resource-id="')[1].split('"')[0]
                    author_name = make_unicode(entry.split('<a class="authorName"')[1].split(
                        '"name">')[1].split('<')[0])
                    results.append({
                        'rss_prov': provider,
                        'rss_feed': feednr,
                        'rss_title': title,
                        'rss_author': author_name,
                        'rss_bookid': book_id,
                        'rss_isbn': '',
                        'priority': priority,
                        'dispname': dispname,
                        'types': types,
                        'label': label,
                    })
                    if '/show/' in host:  # listopia can be multiple pages
                        next_page = True
                except IndexError:
                    pass
        else:
            logger.debug(f'No data returned from {url}')

        if test:
            logger.debug(f"Test found {len(results)} {plural(len(results), 'result')} from {host}")
            return len(results)

        page += 1
        if maxpage and page > maxpage:
            logger.warning('Maximum results page reached, still more results available')
            next_page = False

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    return results


def mam(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"

    url = host

    result, success = fetch_url(url, timeout=False)

    if success:
        data = feedparser.parse(result)
    else:
        logger.error(f'Error fetching data from {host}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(basehost, result)
        data = None

    with open(DIRS.get_logfile('mam.data'), 'w') as mamlog:
        mamlog.write(result)

    if data:
        logger.debug(f'Parsing results from {url}')
        provider = data['feed']['link']
        desc = data['feed']['description']
        if desc.startswith('Error'):
            logger.error(f'Error fetching data from {host}: {desc}')
            if test:
                return 0
            return []
        if not dispname:
            dispname = provider
        logger.debug(f"rss {provider} returned {len(data.entries)} {plural(len(data.entries), 'result')}")
        for post in data.entries:
            title = ''
            book_link = ''
            author_name = ''
            isbn = ''
            category = ''
            if 'title' in post:
                title = post.title
            if 'link' in post:
                book_link = post.link
            if 'category' in post:
                category = post.category
            if 'isbn' in post:
                isbn = post.isbn

            for key in post:
                try:
                    author_name = post[key].split('Author(s):')[1].split('<')[0].strip()
                    if author_name:
                        break
                except (IndexError, AttributeError):
                    pass

            # mam uses period as a separator between multiple author names
            # but fortunately doesn't add a period after initials
            if '.' in author_name and ' ' in author_name.split('.')[0]:
                # more than one word before the first period, assume first author is primary
                # and author names are always more than one word
                author_name = author_name.split('.')[0]

            if title and author_name:
                results.append({
                    'rss_prov': provider,
                    'rss_feed': feednr,
                    'rss_title': title,
                    'rss_author': author_name,
                    'rss_bookid': '',
                    'rss_link': book_link,
                    'rss_isbn': isbn,
                    'rss_category': category,
                    'priority': priority,
                    'dispname': dispname,
                    'types': types,
                    'label': label,
                })
        logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    else:
        logger.debug(f'No data returned from {host}')
    if test:
        return len(results)
    return results


def goodreads(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    Goodreads rss query function, return all the results in a list, can handle multiple wishlists
    but expects goodreads format (looks for goodreads category names)
    """
    logger = logging.getLogger(__name__)
    results = []
    basehost = host
    if str(host)[:4] != "http":
        host = f"http://{host}"

    url = host
    maxpage = priority
    page = 1
    next_page = True

    while next_page:
        if page > 1:
            if '?' in host:
                url = f"{host}&page={page}"
            else:
                url = f"{host}?page={page}"

        result, success = fetch_url(url)
        next_page = False

        if success:
            data = feedparser.parse(result)
        else:
            logger.error(f'Error fetching data from {host}: {result}')
            if not test:
                BLOCKHANDLER.block_provider(basehost, result)
            data = None

        if data:
            logger.debug(f'Parsing results from {url}')
            provider = data['feed']['link']
            if not dispname:
                dispname = provider
            logger.debug(f"rss {provider} returned {len(data.entries)} {plural(len(data.entries), 'result')}")
            for post in data.entries:
                title = ''
                book_id = ''
                author_name = ''
                isbn = ''
                if 'title' in post:
                    title = post.title
                if 'book_id' in post:
                    book_id = post.book_id
                if 'author_name' in post:
                    author_name = post.author_name
                if 'isbn' in post:
                    isbn = post.isbn
                if title and author_name:
                    results.append({
                        'rss_prov': provider,
                        'rss_feed': feednr,
                        'rss_title': title,
                        'rss_author': author_name,
                        'rss_bookid': book_id,
                        'rss_isbn': isbn,
                        'priority': priority,
                        'dispname': dispname,
                        'types': types,
                        'label': label,
                    })
                    next_page = True
        if test:
            logger.debug(f"Test found {len(results)} {plural(len(results), 'result')} from {host}")
            return len(results)

        page += 1
        if maxpage and page > maxpage:
            logger.warning('Maximum results page reached, still more results available')
            next_page = False

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {host}")
    return results


def rss(host=None, feednr=None, priority=0, dispname=None, types='E', test=False, label=''):
    """
    Generic rss query function, just return all the results from the rss feed in a list
    """
    logger = logging.getLogger(__name__)
    results = []
    success = False
    result = ''

    url = str(host)
    if str(url)[:4] != "http" and str(url)[:4] != "file":
        url = f"http://{url}"

    if str(url)[:4] == "http":
        result, success = fetch_url(url)
    elif str(url)[:4] == "file":
        success = False
        file_path = urlparse(url).path
        # noinspection PyBroadException
        try:
            with open(syspath(file_path)) as rss_provider:
                success = True
                result = rss_provider.read()
        except Exception:
            logger.error(f"{url} rss file provider doesn't exist")

    if success:
        data = feedparser.parse(result)
    else:
        logger.error(f'Error fetching data from {host}: {result}')
        if not test:
            BLOCKHANDLER.block_provider(host, result)
        data = None

    if data:
        # to debug because of api
        logger.debug(f'Parsing results from {url}')
        try:
            provider = data['feed']['link']
        except KeyError:
            provider = f'rss_{feednr}'
        if not dispname:
            dispname = provider
        logger.debug(f"rss {provider} returned {len(data.entries)} {plural(len(data.entries), 'result')}")
        for post in data.entries:
            title = None
            magnet = None
            size = 0
            torrent = None
            nzb = None
            url = None
            tortype = 'torrent'

            if 'title' in post:
                title = post.title
            if 'links' in post:
                for f in post.links:
                    if 'x-bittorrent' in f['type']:
                        size = f['length']
                        torrent = f['href']
                        break
                    if 'x-nzb' in f['type']:
                        size = f['length']
                        nzb = f['href']
                        break
                    if f['href'].startswith('magnet'):
                        magnet = f['href']
                        if 'length' in f:
                            size = f['length']
                        break

            if 'torrent_magneturi' in post:
                magnet = post.torrent_magneturi

            if torrent:
                url = torrent
                tortype = 'torrent'

            if magnet and not url or (url and CONFIG.get_bool('PREFER_MAGNET')):
                url = magnet
                tortype = 'magnet'

            if nzb:  # prefer nzb over torrent/magnet
                url = nzb
                tortype = 'nzb'

            if not url and 'link' in post:
                url = post.link

            tor_date = 'Fri, 01 Jan 1970 00:00:00 +0100'
            if 'newznab_attr' in post and post.newznab_attr['name'] == 'usenetdate':
                tor_date = post.newznab_attr['value']

            if not size:
                size = 1000
            if title and url:
                results.append({
                    'tor_prov': provider,
                    'tor_title': title,
                    'tor_url': url,
                    'tor_size': str(size),
                    'tor_date': tor_date,
                    'tor_feed': feednr,
                    'tor_type': tortype,
                    'priority': priority,
                    'dispname': dispname,
                    'types': types,
                    'label': label,
                })
    else:
        logger.debug(f'No data returned from {host}')
    if test:
        return len(results)
    return results


def cancel_search_type(search_type: str, error_msg: str, provider: ConfigDict, errorcode=0):
    """ See if errorMsg contains a known error response for an unsupported search function
    depending on which searchType. If it does, disable that searchtype for the relevant provider
    return True if cancelled
    """
    logger = logging.getLogger(__name__)

    if (provider['BOOKSEARCH'] and search_type in ["book", "shortbook", 'titlebook']) or \
            (provider['AUDIOSEARCH'] and search_type in ["audio", "shortaudio"]):

        match = (200 <= errorcode < 300)  # 200-299 are API call specific error codes

        if not match:
            errorlist = ['no such function', 'unknown parameter', 'unknown function', 'bad_gateway',
                         'bad request', 'bad_request', 'incorrect parameter', 'does not support', 'validation failed']

            errormsg = make_unicode(error_msg).lower()

            for item in errorlist:
                if item in errormsg:
                    match = True
                    break

        if match:
            if search_type in ["book", "shortbook", 'titlebook']:
                msg = 'BOOKSEARCH'
            elif search_type in ["audio", "shortaudio"]:
                msg = 'AUDIOSEARCH'
            else:
                msg = ''

            if msg:
                for providertype in ['NEWZNAB', 'TORZNAB']:
                    for prov in CONFIG.providers(providertype):
                        if prov['HOST'] == provider['HOST'] and not prov['MANUAL']:
                            logger.error(f"Disabled {msg}={prov[msg]} for {prov['DISPNAME']}")
                            prov[msg] = ""
                            # CONFIG.save_config_and_backup_old(section=prov['NAME'])
                            return True
            logger.error(f"Unable to disable searchtype [{search_type}] for {provider['DISPNAME']}")
    return False


def newznab_plus(book: dict, provider: ConfigDict, search_type: str, search_mode=None, test=False):
    """
    Generic NewzNabplus query function
    takes in host+key+type and returns the result set regardless of who
    based on site running NewzNab+
    ref http://usenetreviewz.com/nzb-sites/
    """
    logger = logging.getLogger(__name__)
    host = provider['HOST']
    api_key = provider['API']
    logger.debug(
        f'SearchType [{search_type}] with Host [{host}] mode [{search_mode}] using api [{api_key}] '
        f'for item [{str(book)}]')

    results = []
    if not host:
        return False, []

    params = return_search_structure(provider, api_key, book, search_type, search_mode)

    if not params:
        return False, []
    if str(host[:4]) != "http":
        host = f"http://{host}"
    if host[-1:] == '/':
        host = host[:-1]
    if host[-4:] == '/api':
        url = f"{host}?{urlencode(params)}"
    else:
        url = f"{host}/api?{urlencode(params)}"

    sterm = make_unicode(book['searchterm'])

    rootxml = None
    logger.debug(f"URL = {url}")
    result, success = fetch_url(url, raw=True)

    if test:
        try:
            result = result.decode('utf-8')
        except UnicodeDecodeError:
            result = result.decode('latin-1')
        except AttributeError:
            pass

        if result.startswith('<') and result.endswith('/>') and "error code" in result:
            result = result[1:-2]
            success = False
        if not success:
            logger.debug(result)
            return success, result

    if success:
        try:
            rootxml = ElementTree.fromstring(result)
        except Exception as e:
            logger.error(f'Error parsing data from {host}: {type(e).__name__} {str(e)}')
            logger.debug(repr(result))
            rootxml = None
            success = False
    else:
        try:
            result = result.decode('utf-8')
        except UnicodeDecodeError:
            result = result.decode('latin-1')
        except AttributeError:
            pass

        if not result or result == "''":
            result = "Got an empty response"
        logger.error(f'Error reading data from {host}: {result}')

    if not success:
        if '429' in result:
            # too many requests...
            BLOCKHANDLER.block_provider(provider['HOST'], "Too Many Requests", delay=30)
        else:
            # maybe the host doesn't support the search type
            cancelled = cancel_search_type(search_type, result, provider)
            if not cancelled:  # it was some other problem
                BLOCKHANDLER.block_provider(provider['HOST'], result)

    if success and rootxml is not None:
        # to debug because of api
        logger.debug(f'Parsing results from <a href="{url}">{host}</a>')
        if rootxml.tag == 'error':
            # noinspection PyTypeChecker
            errormsg = rootxml.get('description', default='unknown error')
            errormsg = errormsg[:200]  # sometimes get huge error messages from jackett
            errorcode = int(rootxml.get('code', default=900))  # 900 is "Unknown Error"
            logger.error(f"{host} - {errormsg}")
            # maybe the host doesn't support the search type
            cancelled = cancel_search_type(search_type, errormsg, provider, errorcode)
            if not cancelled:  # it was some other problem
                BLOCKHANDLER.block_provider(provider['HOST'], errormsg)

            if search_type == 'book' and cancelled:
                return newznab_plus(book, provider, 'generalbook', search_mode, test)
        else:
            channel = rootxml.find('channel')
            if channel:
                for item in channel:
                    if 'apilimits' in str(item):
                        limits = item
                        apimax = limits.get('apimax')
                        if apimax:
                            provider.set_int('APILIMIT', int(apimax))
                        apicurrent = limits.get('apicurrent')
                        if apicurrent:
                            provider.set_int('APICOUNT', int(apicurrent))
                        logger.debug(
                            f"{provider['DISPNAME']} used {provider['APICOUNT']} of {provider['APILIMIT']}")
                        break
            resultxml = rootxml.iter('item')
            nzbcount = 0
            maxage = CONFIG.get_int('USENET_RETENTION')
            for nzb in resultxml:
                try:
                    thisnzb = return_results_by_search_type(book, nzb, host, search_mode, provider.
                                                            get_int('DLPRIORITY'))
                    thisnzb['dispname'] = provider['DISPNAME']
                    if search_type in ['book', 'shortbook', 'titlebook']:
                        thisnzb['booksearch'] = provider['BOOKSEARCH']

                    if 'seeders' in thisnzb:
                        if 'SEEDERS' not in provider:
                            # might have provider in newznab instead of torznab slot?
                            logger.warning(f"{provider['DISPNAME']} does not support seeders")
                        else:
                            # its torznab, check if minimum seeders relevant
                            if check_int(thisnzb['seeders'], 0) >= check_int(provider['SEEDERS'], 0):
                                nzbcount += 1
                                results.append(thisnzb)
                            else:
                                logger.debug(
                                    f"Rejecting {thisnzb['nzbtitle']} has {thisnzb['seeders']} "
                                    f"{plural(thisnzb['seeders'], 'seeder')}")
                    else:
                        # its newznab, check if too old
                        if not maxage:
                            nzbcount += 1
                            results.append(thisnzb)
                        else:
                            # example nzbdate format: Mon, 27 May 2013 02:12:09 +0200
                            nzbdate = thisnzb['nzbdate']
                            try:
                                parts = nzbdate.split(' ')
                                nzbage = age(f'{int(parts[3]):04d}-{month2num(parts[2]):02d}-{int(parts[1]):02d}')
                            except Exception as e:
                                logger.warning(
                                    f"Unable to get age from [{thisnzb['nzbdate']}] {type(e).__name__} {str(e)}")
                                nzbage = 0
                            if nzbage <= maxage:
                                nzbcount += 1
                                results.append(thisnzb)
                            else:
                                logger.debug(f"{thisnzb['nzbtitle']} is too old ({nzbage} {plural(nzbage, 'day')})")

                except IndexError:
                    logger.debug(f'No results from {host} for {sterm}')
            logger.debug(f'Found {nzbcount} results at {host} for: {sterm}')
    else:
        logger.debug(f'No data returned from {host} for {sterm}')
    if test:
        return len(results), host
    return True, results


def return_search_structure(provider: ConfigDict, api_key, book, search_type, search_mode):
    logger = logging.getLogger(__name__)
    params = None
    if search_type in ["book", "shortbook", 'titlebook']:
        authorname, bookname = get_searchterm(book, search_type)
        if provider['BOOKSEARCH'] and provider['BOOKCAT']:  # if specific booksearch, use it
            if provider['BOOKSEARCH'] == 'bibliotik':
                params = {
                    "t": provider['GENERALSEARCH'],
                    "apikey": api_key,
                    "q": (f"@title {bookname} @authors {authorname}").encode(),
                    "cat": provider['BOOKCAT']
                }
            else:
                params = {
                    "t": provider['BOOKSEARCH'],
                    "apikey": api_key,
                    "title": bookname.encode("utf-8"),
                    "author": (authorname).encode("utf-8"),
                    "cat": provider['BOOKCAT']
                }
        elif provider['GENERALSEARCH'] and provider['BOOKCAT']:  # if not, try general search
            params = {
                "t": provider['GENERALSEARCH'],
                "apikey": api_key,
                "q": (f"{authorname} {bookname}").encode(),
                "cat": provider['BOOKCAT']
            }
    elif search_type in ["audio", "shortaudio"]:
        authorname, bookname = get_searchterm(book, search_type)
        if provider['AUDIOSEARCH'] and provider['AUDIOCAT']:  # if specific audiosearch, use it
            params = {
                "t": provider['AUDIOSEARCH'],
                "apikey": api_key,
                "title": (bookname).encode("utf-8"),
                "author": (authorname).encode("utf-8"),
                "cat": provider['AUDIOCAT']
            }
        elif provider['GENERALSEARCH'] and provider['AUDIOCAT']:  # if not, try general search
            params = {
                "t": provider['GENERALSEARCH'],
                "apikey": api_key,
                "q": (f"{authorname} {bookname}").encode(),
                "cat": provider['AUDIOCAT']
            }
    elif search_type == "mag":
        if provider['MAGSEARCH'] and provider['MAGCAT']:  # if specific magsearch, use it
            params = {
                "t": provider['MAGSEARCH'],
                "apikey": api_key,
                "cat": provider['MAGCAT'],
                "q": (book['searchterm'].replace(':', '')).encode("utf-8"),
            }
        elif provider['GENERALSEARCH'] and provider['MAGCAT']:
            params = {
                "t": provider['GENERALSEARCH'],
                "apikey": api_key,
                "cat": provider['MAGCAT'],
                "q": (book['searchterm'].replace(':', '')).encode("utf-8"),
            }
    else:
        if provider['GENERALSEARCH']:
            if "shortgeneral" in search_type:
                searchterm = unaccented(book['searchterm'].split('(')[0], only_ascii=False)
                searchterm = searchterm.replace('/', '_').replace('#', '_').replace(':', '')
            elif 'title' in search_type:
                _, searchterm = get_searchterm(book, search_type)
                searchterm = unaccented(searchterm.replace(':', ''), only_ascii=False)
            else:
                searchterm = unaccented(book['searchterm'], only_ascii=False)
                searchterm = searchterm.replace('/', '_').replace('#', '_').replace(':', '')
            params = {
                "t": provider['GENERALSEARCH'],
                "apikey": api_key,
                "q": (searchterm).encode("utf-8"),
            }
    if params:
        if provider['EXTENDED']:
            extends = provider['EXTENDED'].split('&')
            if extends[0] in ['1', '0']:
                params["extended"] = extends[0]
            if '=' not in extends[0]:
                extends.pop(0)
            for item in extends:
                try:
                    key, value = item.split('=')
                    params[key] = value
                except ValueError:
                    pass

        logger.debug(f'{search_mode} Search parameters set to {str(params)}')
    else:
        logger.debug(f'{search_mode} No matching search parameters for {search_type}')

    return params


def return_results_by_search_type(book: dict, nzbdetails, host=None, search_mode=None, priority=0) -> dict:
    """
    # searchType has multiple query params for t=, which return different results sets.
    # books have a dedicated check, so will use that.
    # mags don't so will have more generic search term.
    # http://newznab.readthedocs.org/en/latest/misc/api/#predefined-categories
    # results when searching for t=book
    #    <item>
    #       <title>David Gemmell - Troy 03 - Fall of Kings</title>
    #       <guid isPermaLink="true">
    #           https://www.usenet-crawler.com/details/091c8c0e18ca34201899b91add52e8c0
    #       </guid>
    #       <link>
    #           https://www.usenet-crawler.com/getnzb/091c8c0e18ca34201899b91add52e8c0.nzb&i=155518&r=78c0509
    #       </link>
    #       <comments>
    # https://www.usenet-crawler.com/details/091c8c0e18ca34201899b91add52e8c0#comments
    #       </comments>
    #       <pubDate>Fri, 11 Jan 2013 16:49:34 +0100</pubDate>
    #       <category>Books > Ebook</category>
    #       <description>David Gemmell - Troy 03 - Fall of Kings</description>
    #       <enclosure url="https://www.usenet-crawler.com/getnzb/091c8c0e18ca34201899b91add52e8c0.nzb&i=155518&r=78c0>
    #       <newznab:attr name="category" value="7000"/>
    #       <newznab:attr name="category" value="7020"/>
    #       <newznab:attr name="size" value="4909563"/>
    #       <newznab:attr name="guid" value="091c8c0e18ca34201899b91add52e8c0"/>
    #       </item>
    #
    # t=search results
    # <item>
    #   <title>David Gemmell - [Troy 03] - Fall of Kings</title>
    #   <guid isPermaLink="true">
    #       https://www.usenet-crawler.com/details/5d7394b2386683d079d8bd8f16652b18
    #   </guid>
    #   <link>
    #       https://www.usenet-crawler.com/getnzb/5d7394b2386683d079d8bd8f16652b18.nzb&i=155518&r=78c0509bc6bb9174
    #   </link>
    #   <comments>
    # https://www.usenet-crawler.com/details/5d7394b2386683d079d8bd8f16652b18#comments
    #   </comments>
    #   <pubDate>Mon, 27 May 2013 02:12:09 +0200</pubDate>
    #   <category>Books > Ebook</category>
    #   <description>David Gemmell - [Troy 03] - Fall of Kings</description>
    #   <enclosure url="https://www.usenet-crawler.com/getnzb/5d7394b2386683d079d8bd8f16652b18.nzb&i=155518&r=78c05>
    #   <newznab:attr name="category" value="7000"/>
    #   <newznab:attr name="category" value="7020"/>
    #   <newznab:attr name="size" value="4909563"/>
    #   <newznab:attr name="guid" value="5d7394b2386683d079d8bd8f16652b18"/>
    #   <newznab:attr name="files" value="2"/>
    #   <newznab:attr name="poster" value="nerdsproject@gmail.com (N.E.R.Ds)"/>
    #   <newznab:attr name="grabs" value="0"/>
    #   <newznab:attr name="comments" value="0"/>
    #   <newznab:attr name="password" value="0"/>
    #   <newznab:attr name="usenetdate" value="Fri, 11 Mar 2011 13:45:15 +0100"/>
    #   <newznab:attr name="group" value="alt.binaries.e-book.flood"/>
    # </item>
    # -------------------------------TORZNAB RETURN DATA-- book ---------------------------------------------
    # <item>
    #  <title>Tom Holt - Blonde Bombshell (Dystop; SFX; Humour) ePUB+MOBI</title>
    #  <guid>https://getstrike.net/torrents/1FDBE6466738EED3C7FD915E1376BA0A63088D4D</guid>
    #  <comments>https://getstrike.net/torrents/1FDBE6466738EED3C7FD915E1376BA0A63088D4D</comments>
    #  <pubDate>Sun, 27 Sep 2015 23:10:56 +0200</pubDate>
    #  <size>24628</size>
    #  <description>Tom Holt - Blonde Bombshell (Dystop; SFX; Humour) ePUB+MOBI</description>
    #  <link>http://192.168.2.2:9117/dl/strike/pkl4u83iz41up73m4zsigqsd4zyie50r/aHR0cHM6Ly9nZXRzdHJpa2UubmV0L3RvcnJl
    #  bnRzL2FwaS9kb3dubG9hZC8xRkRCRTY0NjY3MzhFRUQzQzdGRDkxNUUxMzc2QkEwQTYzMDg4RDRELnRvcnJlbnQ1/t.torrent</link>
    #  <category>8000</category>
    #  <enclosure url="http://192.168.2.2:9117/dl/strike/pkl4u83iz41up73m4zsigqsd4zyie50r/aHR0cHM6Ly9nZXRzdHJpa2UubmV
    #  0L3RvcnJlbnRzL2FwaS9kb3dubG9hZC8xRkRCRTY0NjY3MzhFRUQzQzdGRDkxNUUxMzc2QkEwQTYzMDg4RDRELnRvcnJlbnQ1/t.torrent"
    #  length="24628" type="application/x-bittorrent" />
    #  <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:1FDBE6466738EED3C7FD915E1376BA0A63088D4D&amp;
    #  dn=Tom+Holt+-+Blonde+Bombshell+(Dystop%3B+SFX%3B+Humour)+ePUB%2BMOBI&amp;tr=udp://open.demonii.com:1337&amp;
    #  tr=udp://tracker.coppersurfer.tk:6969&amp;tr=udp://tracker.leechers-paradise.org:6969&amp;
    #  tr=udp://exodus.desync.com:6969" />
    #  <torznab:attr name="seeders" value="1" />
    #  <torznab:attr name="peers" value="2" />
    #  <torznab:attr name="infohash" value="1FDBE6466738EED3C7FD915E1376BA0A63088D4D" />
    #  <torznab:attr name="minimumratio" value="1" />
    #  <torznab:attr name="minimumseedtime" value="172800" />
    # </item>
    # ---------------------------------------- magazine ----------------------------------------
    # <item>
    #  <title>Linux Format Issue 116 - KDE Issue</title>
    #  <guid>https://getstrike.net/torrents/f3fc8df4fdd850132072a435a7d112d6c9d77d16</guid>
    #  <comments>https://getstrike.net/torrents/f3fc8df4fdd850132072a435a7d112d6c9d77d16</comments>
    #  <pubDate>Wed, 04 Mar 2009 01:57:20 +0100</pubDate>
    #  <size>1309195</size>
    #  <description>Linux Format Issue 116 - KDE Issue</description>
    #  <link>http://192.168.2.2:9117/dl/strike/pkl4u83iz41up73m4zsigqsd4zyie50r/aHR0cHM6Ly9nZXRzdHJpa2UubmV0L3R
    #  vcnJlbnRzL2FwaS9kb3dubG9hZC9mM2ZjOGRmNGZkZDg1MDEzMjA3MmE0MzVhN2QxMTJkNmM5ZDc3ZDE2LnRvcnJlbnQ1/t.torrent</link>
    #  <enclosure url="http://192.168.2.2:9117/dl/strike/pkl4u83iz41up73m4zsigqsd4zyie50r/aHR0cHM6Ly9nZXRzdHJpa2Uubm
    #  V0L3RvcnJlbnRzL2FwaS9kb3dubG9hZC9mM2ZjOGRmNGZkZDg1MDEzMjA3MmE0MzVhN2QxMTJkNmM5ZDc3ZDE2LnRvcnJlbnQ1/t.torrent"
    #  length="1309195" type="application/x-bittorrent" />
    #  <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:f3fc8df4fdd850132072a435a7d112d6c9d77d16&amp;
    #  dn=Linux+Format+Issue+116+-+KDE+Issue&amp;tr=udp://open.demonii.com:1337&amp;tr=udp://tracker.coppersurfer.
    #  tk:6969&amp;tr=udp://tracker.leechers-paradise.org:6969&amp;tr=udp://exodus.desync.com:6969" />
    #  <torznab:attr name="seeders" value="2" />
    #  <torznab:attr name="peers" value="3" />
    #  <torznab:attr name="infohash" value="f3fc8df4fdd850132072a435a7d112d6c9d77d16" />
    #  <torznab:attr name="minimumratio" value="1" />
    #  <torznab:attr name="minimumseedtime" value="172800" />
    #  </item>
    """

    logger = logging.getLogger(__name__)
    nzbtitle = ''
    nzbdate = ''
    nzburl = ''
    nzbsize = 0
    seeders = None
    comments = ''

    n = 0
    while n < len(nzbdetails):
        tag = str(nzbdetails[n].tag).lower()

        if tag == 'title':
            nzbtitle = nzbdetails[n].text
        elif tag == 'size':
            nzbsize = nzbdetails[n].text
        elif tag == 'comments':
            comments = nzbdetails[n].text
        elif tag == 'pubdate':
            nzbdate = nzbdetails[n].text
        elif tag == 'link':
            if not nzburl or (nzburl and not CONFIG.get_bool('PREFER_MAGNET')):
                nzburl = nzbdetails[n].text
        elif nzbdetails[n].attrib.get('name') == 'magneturl':
            nzburl = nzbdetails[n].attrib.get('value')
        elif nzbdetails[n].attrib.get('name') == 'size':
            nzbsize = nzbdetails[n].attrib.get('value')
        elif nzbdetails[n].attrib.get('name') == 'seeders':
            seeders = nzbdetails[n].attrib.get('value')
        n += 1

    result_fields = {
        'bookid': book['bookid'],
        'nzbprov': host,
        'nzbtitle': nzbtitle,
        'nzburl': nzburl,
        'nzbdate': nzbdate,
        'nzbsize': nzbsize,
        'nzbmode': search_mode,
        'priority': priority
    }
    if seeders is not None:  # only if torznab
        result_fields['seeders'] = check_int(seeders, 0)
    if comments and comments.startswith('http'):  # torznab may have a provider page link here
        result_fields['prov_page'] = comments

    logger.debug(f"Result fields from NZB are {str(result_fields)}")
    return result_fields
