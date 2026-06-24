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
import threading
import time
import traceback

from rapidfuzz import fuzz

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.comicid import cv_identify, cx_identify
from lazylibrarian.config2 import CONFIG
from lazylibrarian.downloadmethods import direct_dl_method, nzb_dl_method, tor_dl_method
from lazylibrarian.formatter import (
    check_int,
    date_format,
    get_list,
    now,
    plural,
    replace_all,
    thread_name,
    unaccented,
)
from lazylibrarian.notifiers import custom_notify_snatch, notify_snatch
from lazylibrarian.providers import (
    iterate_over_direct_sites,
    iterate_over_irc_sites,
    iterate_over_rss_sites,
    iterate_over_torrent_sites,
    iterate_over_znab_sites,
)
from lazylibrarian.scheduling import SchedulerCommand, schedule_job
from lazylibrarian.telemetry import TELEMETRY

# '0': '', '1': '', '2': '', '3': '', '4': '', '5': '', '6': '', '7': '', '8': '', '9': '',
dictrepl = {'...': '', '.': ' ', ' & ': ' ', ' = ': ' ', '?': '', '$': 's', ' + ': ' ', '"': '',
            ',': ' ', '*': '', '(': '', ')': '', '[': '', ']': '', '#': '', '\'': '',
            ':': '', '!': '', '-': ' ', r'\s\s': ' '}


def search_item(comicid=None):
    """
    Call all active search providers to search for comic by id
    return a list of results, each entry in list containing percentage_match, title, provider, size, url
    """
    logger = logging.getLogger(__name__)
    results = []
    TELEMETRY.record_usage_data('Search/Comic')
    if not comicid:
        return results

    db = database.DBConnection()
    try:
        cmd = "SELECT Title,SearchTerm from comics WHERE Status='Active' and ComicID=?"
        match = db.match(cmd, (comicid,))
    finally:
        db.close()
    if not match:
        logger.debug(f"No comic match for {comicid}")
        return results

    cat = 'comic'
    book = {'library': cat, 'bookid': comicid, 'bookName': match['Title']}
    searchterm = match['SearchTerm']
    if not searchterm:
        searchterm = match['Title']
    book['searchterm'] = searchterm.replace('+', ' ')

    nprov = CONFIG.total_active_providers()
    logger.debug(f"Searching {nprov} {plural(nprov, 'provider')} ({cat}) for {searchterm}")

    if CONFIG.use_nzb():
        resultlist, nprov = iterate_over_znab_sites(book, cat)
        if nprov:
            results += resultlist
    if CONFIG.use_tor():
        resultlist, nprov = iterate_over_torrent_sites(book, cat)
        if nprov:
            results += resultlist
    if CONFIG.use_direct():
        resultlist, nprov = iterate_over_direct_sites(book, cat)
        if nprov:
            results += resultlist
    if CONFIG.use_irc():
        resultlist, nprov = iterate_over_irc_sites(book, cat)
        if nprov:
            results += resultlist
    if CONFIG.use_rss():
        resultlist, nprov, dltypes = iterate_over_rss_sites()
        if nprov and dltypes != 'C':
            results += resultlist

    # reprocess to get consistent results
    searchresults = []
    for item in results:
        provider = ''
        title = ''
        url = ''
        size = ''
        date = ''
        mode = ''
        if 'dispname' in item:
            provider = item['dispname']
        elif 'nzbprov' in item:
            provider = item['nzbprov']
        elif 'tor_prov' in item:
            provider = item['tor_prov']
        elif 'rss_prov' in item:
            provider = item['rss_prov']
        if 'nzbtitle' in item:
            title = item['nzbtitle']
        if 'nzburl' in item:
            url = item['nzburl']
        if 'nzbsize' in item:
            size = item['nzbsize']
        if 'nzbdate' in item:
            date = item['nzbdate']
        if 'nzbmode' in item:
            mode = item['nzbmode']
        if 'tor_title' in item:
            title = item['tor_title']
        if 'tor_url' in item:
            url = item['tor_url']
        if 'tor_size' in item:
            size = item['tor_size']
        if 'tor_date' in item:
            date = item['tor_date']
        if 'tor_type' in item:
            mode = item['tor_type']

        if title and provider and mode and url:
            # Not all results have a date or a size
            if not size:
                size = '1000'
            if date:
                date = date_format(date, context=title, datelang=CONFIG['DATE_LANG'])
            url = url.encode('utf-8')
            if mode == 'torznab' and url.startswith(b'magnet'):
                mode = 'magnet'

            # calculate match percentage - torrents might have words_with_underscore_separator
            part_title = title.replace('_', ' ').split('(')[0]
            # strip any trailing words that are just digits
            while part_title and part_title[-1].isdigit():
                part_title = part_title[:-1].strip()
            searchmatch = searchterm.replace('+', ' ')
            score = fuzz.token_set_ratio(searchmatch, part_title)

            # lose a point for each extra word in the title, so we get the closest match
            words = len(get_list(searchterm))
            words -= len(get_list(title))
            score -= abs(words)
            rejected = False
            if score >= 40:  # ignore wildly wrong results?

                maxsize = CONFIG.get_int('REJECT_MAXCOMIC')
                minsize = CONFIG.get_int('REJECT_MINCOMIC')
                filetypes = get_list(CONFIG['COMIC_TYPE'])
                banwords = CONFIG.get_csv('REJECT_COMIC')
                size_mb = check_int(size, 1000)
                size_mb = round(float(size_mb) / 1048576, 2)

                if not rejected and maxsize and size_mb > maxsize:
                    rejected = True
                    logger.debug(f"Rejecting {title}, too large ({size_mb}Mb)")

                if not rejected and minsize and size_mb < minsize:
                    rejected = True
                    logger.debug(f"Rejecting {title}, too small ({size_mb}Mb)")

                if not rejected:
                    result_title = unaccented(replace_all(title, dictrepl), only_ascii=False).strip()
                    words = get_list(result_title.lower())
                    for word in words:
                        if word in banwords:
                            logger.debug(f"Rejecting {title}, contains {word}")
                            rejected = True
                            break
                    if not rejected and filetypes:
                        for word in filetypes:
                            if word in words:
                                score += 1
                if not rejected:
                    result = {'score': score, 'title': title, 'provider': provider, 'size': size_mb, 'date': date,
                              'url': url, 'mode': mode, 'bookid': comicid}
                    searchresults.append(result)

    logger.debug(f'Found {len(searchresults)} {cat} results for {searchterm}')
    return searchresults


def cron_search_comics():
    logger = logging.getLogger(__name__)
    if 'SEARCHALLCOMICS' not in [n.name for n in list(threading.enumerate())]:
        search_comics()
    else:
        logger.debug("SEARCHALLCOMICS is already running")


def search_comics(comicid=None):
    logger = logging.getLogger(__name__)
    searchinglogger = logging.getLogger('special.searching')
    TELEMETRY.record_usage_data('Search/Comics')
    threadname = thread_name()
    if "Thread" in threadname:
        if not comicid:
            thread_name("SEARCHALLCOMICS")
        else:
            thread_name("SEARCHCOMIC")

    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        logger.debug(f"Storing start time for {thread_name()}")
        db.upsert("jobs", {"Start": time.time()}, {"Name": thread_name()})
        cmd = "SELECT ComicID,Title, aka from comics WHERE Status='Active'"
        if comicid:
            # single comic search
            cmd += " AND ComicID=?"
            comics = db.select(cmd, (comicid,))
        else:
            # search for all active comics
            comics = db.select(cmd)
            logger.debug(f"Found {len(comics)} active comics")

        for comic in comics:
            if lazylibrarian.STOPTHREADS and threadname == "SEARCHALLCOMICS":
                logger.debug(f"Aborting {threadname}")
                break
            comicid = comic['ComicID']
            aka = get_list(comic['aka'])
            id_list = comicid
            if len(aka):
                id_list = f"{id_list}, {', '.join(aka)}"
            found = 0
            notfound = 0
            foundissues = {}
            res = search_item(comicid)
            for item in res:
                match = None
                if item['score'] >= 85:
                    searchinglogger.debug(f"Trying to match {item['title']}")
                    if comic['ComicID'].startswith('CV'):
                        match = cv_identify(item['title'])
                    elif comic['ComicID'].startswith('CX'):
                        match = cx_identify(item['title'])
                if match:
                    if match[3]['seriesid'] == comicid or match[3]['seriesid'] in aka:
                        found += 1
                        if match[4] and match[4] not in foundissues:
                            foundissues[match[4]] = item
                    else:
                        searchinglogger.debug(f"No match ({match[3]['seriesid']}) want {id_list}: {item['title']}")
                        notfound += 1
                else:
                    searchinglogger.debug(f"No match [{item['score']}%] {item['title']}")
                    notfound += 1

            total = len(foundissues)
            haveissues = db.select("SELECT IssueID from comicissues WHERE ComicID=?", (comicid,))
            have = []
            located = []
            for item in haveissues:
                have.append(int(item['IssueID']))
            for item in foundissues:
                located.append(item)
            for item in located:
                if item in have:
                    foundissues.pop(item)

            logger.debug(
                f"Found {len(res)} results, {found} match, {notfound} fail, {total} distinct, Have {sorted(have)}, "
                f"Missing {sorted(foundissues.keys())}")
            threading.Thread(target=download_comiclist, name='DL-COMICLIST', args=[foundissues]).start()

            time.sleep(CONFIG.get_int('SEARCH_RATELIMIT'))
        logger.info("ComicSearch for Wanted items complete")
        logger.debug(f"Storing finish time for {thread_name()}")
        db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})
    except Exception:
        logger.error(f'Unhandled exception in search_comics: {traceback.format_exc()}')
    finally:
        db.close()
        thread_name("WEBSERVER")


def download_comiclist(foundissues):
    logger = logging.getLogger(__name__)
    loggesearching = logging.getLogger('special.searching')
    TELEMETRY.record_usage_data('Download/Comiclist')
    db = database.DBConnection()
    try:
        snatched = 0
        for issue in foundissues:
            item = foundissues[issue]
            match = db.match('SELECT Status from wanted WHERE NZBtitle=? and NZBprov=?',
                             (item['title'], item['provider']))
            if match:
                loggesearching.debug(f"{item['title']} is already marked {match['Status']}")
            else:
                bookid = f"{item['bookid']}_{issue}"
                control_value_dict = {
                    "NZBtitle": item['title'],
                    "NZBprov": item['provider']
                }
                new_value_dict = {
                    "NZBurl": item['url'],
                    "BookID": bookid,
                    "NZBdate": item['date'],
                    "AuxInfo": "comic",
                    "Status": "Matched",
                    "NZBsize": item['size'],
                    "NZBmode": item['mode']
                }
                db.upsert("wanted", new_value_dict, control_value_dict)

                if item['mode'] in ["torznab", "torrent", "magnet"]:
                    snatch, res = tor_dl_method(
                        bookid,
                        item['title'],
                        item['url'],
                        'comic',
                        provider=item['provider'])
                elif item['mode'] == 'direct':
                    snatch, res = direct_dl_method(
                        bookid,
                        item['title'],
                        item['url'],
                        'comic',
                        item['provider'])
                elif item['mode'] == 'nzb':
                    snatch, res = nzb_dl_method(
                        bookid,
                        item['title'],
                        item['url'],
                        'comic')
                else:
                    res = f"Unhandled mode [{item['mode']}] for {item['url']}"
                    logger.error(res)
                    snatch = 0

                if snatch:
                    snatched += 1
                    logger.info(f"Downloading {item['title']} from {item['provider']}")
                    db.action("UPDATE wanted SET nzbdate=? WHERE NZBurl=?", (now(), item['url']))
                    custom_notify_snatch(f"{bookid} {item['url']}")
                    notify_snatch(
                        f"Comic {unaccented(item['title'], only_ascii=False)} from "
                        f"{CONFIG.disp_name(item['provider'])} at {now()}")
                else:
                    db.action("UPDATE wanted SET status='Failed',DLResult=? WHERE NZBurl=?",
                              (res, item["url"]))
    finally:
        db.close()
    if snatched:
        schedule_job(SchedulerCommand.START, target='PostProcessor')
