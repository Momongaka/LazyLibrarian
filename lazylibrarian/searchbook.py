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

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import check_int, plural, thread_name
from lazylibrarian.providers import (
    iterate_over_direct_sites,
    iterate_over_irc_sites,
    iterate_over_rss_sites,
    iterate_over_torrent_sites,
    iterate_over_znab_sites,
)
from lazylibrarian.resultlist import download_result, find_best_result
from lazylibrarian.telemetry import TELEMETRY


def cron_search_book():
    logger = logging.getLogger(__name__)
    if 'SEARCHALLBOOKS' not in [n.name for n in list(threading.enumerate())]:
        search_book()
    else:
        logger.debug("SEARCHALLBOOKS is already running")


def good_enough(match):
    return bool(match and int(match[0]) >= CONFIG.get_int('MATCH_RATIO'))


def warn_mode(mode):
    # don't nag. Show warning messages no more than every 20 mins
    logger = logging.getLogger(__name__)
    timenow = int(time.time())
    if mode == 'rss':
        if check_int(lazylibrarian.TIMERS['NO_RSS_MSG'], 0) + 1200 < timenow:
            lazylibrarian.TIMERS['NO_RSS_MSG'] = timenow
        else:
            return
    elif mode == 'nzb':
        if check_int(lazylibrarian.TIMERS['NO_NZB_MSG'], 0) + 1200 < timenow:
            lazylibrarian.TIMERS['NO_NZB_MSG'] = timenow
        else:
            return
    elif mode == 'tor':
        if check_int(lazylibrarian.TIMERS['NO_TOR_MSG'], 0) + 1200 < timenow:
            lazylibrarian.TIMERS['NO_TOR_MSG'] = timenow
        else:
            return
    elif mode == 'irc':
        if check_int(lazylibrarian.TIMERS['NO_IRC_MSG'], 0) + 1200 < timenow:
            lazylibrarian.TIMERS['NO_IRC_MSG'] = timenow
        else:
            return
    elif mode == 'direct':
        if check_int(lazylibrarian.TIMERS['NO_DIRECT_MSG'], 0) + 1200 < timenow:
            lazylibrarian.TIMERS['NO_DIRECT_MSG'] = timenow
        else:
            return
    else:
        return
    logger.warning(f'No {mode} providers are available. Check config and blocklist')


def search_book(books=None, library=None):
    """
    books is a list of new books to add, or None for backlog search
    library is "eBook" or "AudioBook" or None to search all book types
    """
    TELEMETRY.record_usage_data('Search/Book')
    logger = logging.getLogger(__name__)
    searchinglogger = logging.getLogger('special.searching')
    searchinglogger.debug(f"search_book: {books}")
    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        threadname = thread_name()
        if 'SEARCHALL' in threadname or 'API-SEARCH' in threadname or 'FORCE-SEARCH' in threadname:
            force = True
        else:
            force = False

        if "Thread" in threadname:
            if not books:
                thread_name("SEARCHALLBOOKS")
                threadname = "SEARCHALLBOOKS"
            else:
                thread_name("SEARCHBOOKS")

        logger.debug(f"Storing start time for {thread_name()}")
        db.upsert("jobs", {"Start": time.time()}, {"Name": thread_name()})
        searchlist = []
        searchbooks = []

        if not books:
            # We are performing a backlog search
            cmd = ("SELECT BookID, AuthorName, Bookname, BookSub, BookAdded, books.Status, AudioStatus "
                   "from books,authors WHERE (books.Status='Wanted' OR AudioStatus='Wanted') and "
                   "books.AuthorID = authors.AuthorID order by BookAdded desc")
            results = db.select(cmd)
            for terms in results:
                searchbooks.append(terms)
        else:
            # The user has added new books
            if library:
                logger.debug(f"Searching for {len(books)} {plural(len(books), library)}")
                searchinglogger.debug(f"{books}")
            for book in books:
                if book['bookid'] not in ['booklang', 'library', 'ignored']:
                    cmd = ("SELECT BookID, AuthorName, BookName, BookSub, books.Status, AudioStatus "
                           "from books,authors WHERE BookID=? AND books.AuthorID = authors.AuthorID")
                    results = db.select(cmd, (book['bookid'],))
                    if results:
                        for terms in results:
                            searchbooks.append(terms)
                    else:
                        logger.debug(f"SearchBooks - BookID {book['bookid']} is not in the database")

        if len(searchbooks) == 0:
            logger.debug("No books to search for")
            db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})
            return

        nprov = CONFIG.total_active_providers()
        if nprov == 0:
            msg = "SearchBooks - No providers to search"
            blocked = BLOCKHANDLER.number_blocked()
            if blocked:
                msg += f" (there {plural(blocked, 'is')} {blocked} in blocklist)"
            else:
                msg += " (check you have some enabled)"
            logger.debug(msg)
            db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})
            return

        modelist = []
        if CONFIG.use_nzb():
            modelist.append('nzb')
        if CONFIG.use_tor():
            modelist.append('tor')
        if CONFIG.use_direct():
            modelist.append('direct')
        if CONFIG.use_rss():
            modelist.append('rss')
        if CONFIG.use_irc():
            modelist.append('irc')

        if not library:
            library = 'item'

        logger.info(
            f"Searching {nprov} {plural(nprov, 'provider')} {str(modelist)} for {len(searchbooks)} "
            f"{plural(len(searchbooks), library)}")
        logger.info(
            f"Provider Blocklist contains {BLOCKHANDLER.number_blocked()} "
            f"{plural(BLOCKHANDLER.number_blocked(), 'entry')}")

        for searchbook in searchbooks:
            if lazylibrarian.STOPTHREADS and threadname == "SEARCHALLBOOKS":
                logger.debug(f"Aborting {threadname}")
                break

            # searchterm is only used for display purposes
            searchterm = ''
            if searchbook['AuthorName']:
                searchterm = searchbook['AuthorName']
            else:
                logger.warning(f"No AuthorName for {searchbook['BookID']}")

            if searchbook['BookName']:
                if len(searchterm):
                    searchterm += ' '
                searchterm += searchbook['BookName']
            else:
                logger.warning(f"No BookName for {searchbook['BookID']}")

            if searchbook['BookSub']:
                if len(searchterm):
                    searchterm += ': '
                searchterm += searchbook['BookSub']

            if searchbook['Status'] == "Wanted":
                cmd = "SELECT BookID from wanted WHERE BookID=? and AuxInfo='eBook' and Status='Snatched'"
                snatched = db.match(cmd, (searchbook["BookID"],))
                if snatched:
                    logger.warning(
                        f"eBook {searchbook['AuthorName']} {searchbook['BookName']} "
                        f"already marked snatched in wanted table")
                else:
                    searchlist.append(
                        {"bookid": searchbook['BookID'],
                         "bookName": searchbook['BookName'],
                         "bookSub": searchbook['BookSub'],
                         "authorName": searchbook['AuthorName'],
                         "library": "eBook",
                         "searchterm": searchterm})

            if searchbook['AudioStatus'] == "Wanted":
                cmd = "SELECT BookID from wanted WHERE BookID=? and AuxInfo='AudioBook' and Status='Snatched'"
                snatched = db.match(cmd, (searchbook["BookID"],))
                if snatched:
                    logger.warning(
                        f"AudioBook {searchbook['AuthorName']} {searchbook['BookName']} "
                        f"already marked snatched in wanted table")
                else:
                    searchlist.append(
                        {"bookid": searchbook['BookID'],
                         "bookName": searchbook['BookName'],
                         "bookSub": searchbook['BookSub'],
                         "authorName": searchbook['AuthorName'],
                         "library": "AudioBook",
                         "searchterm": searchterm})

        # only get rss results once per run, as they are not search specific
        rss_resultlist = None
        if CONFIG.use_rss():
            rss_resultlist, nprov, dltypes = iterate_over_rss_sites()
            if not nprov or (library == 'Audiobook' and 'A' not in dltypes) or \
                    (library == 'eBook' and 'E' not in dltypes) or \
                    (library is None and ('E' in dltypes or 'A' in dltypes)):
                warn_mode('rss')

        book_count = 0
        for book in searchlist:
            if lazylibrarian.STOPTHREADS and threadname == "SEARCHALLBOOKS":
                logger.debug(f"Aborting {threadname}")
                break
            do_search = True
            if CONFIG.get_bool('DELAYSEARCH') and not force:
                res = db.match('SELECT * FROM failedsearch WHERE BookID=? AND Library=?',
                               (book['bookid'], book['library']))
                if not res:
                    logger.debug(f"SearchDelay: {book['library']} {book['bookid']} has not failed before")
                else:
                    skipped = check_int(res['Count'], 0)
                    interval = check_int(res['Interval'], 0)
                    if skipped < interval:
                        logger.debug(f"SearchDelay: {book['library']} {book['bookid']} not due ({skipped}/{interval})")
                        db.action("UPDATE failedsearch SET Count=? WHERE BookID=? AND Library=?",
                                  (skipped + 1, book['bookid'], book['library']))
                        do_search = False
                    else:
                        logger.debug(
                            f"SearchDelay: {book['library']} {book['bookid']} due this time ({skipped}/{interval})")

            matches = []
            if do_search:
                # first attempt, try author/title in category "book"
                if book['library'] == 'AudioBook':
                    searchtype = 'audio'
                else:
                    searchtype = 'book'

                if CONFIG.use_nzb():
                    resultlist, nprov = iterate_over_znab_sites(book, searchtype)
                    if not nprov:
                        warn_mode('nzb')
                    elif resultlist:
                        match = find_best_result(resultlist, book, searchtype, 'nzb')
                        if not good_enough(match):
                            logger.info(f"NZB search for {book['library']} {book['searchterm']} returned no results.")
                        else:
                            logger.info(
                                f"Found NZB result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                f"priority {match[3]}")
                            matches.append(match)

                if CONFIG.use_tor():
                    resultlist, nprov = iterate_over_torrent_sites(book, searchtype)
                    if not nprov:
                        warn_mode('tor')
                    elif resultlist:
                        match = find_best_result(resultlist, book, searchtype, 'tor')
                        if not good_enough(match):
                            logger.info(
                                f"Torrent search for {book['library']} {book['searchterm']} returned no results.")
                        else:
                            logger.info(
                                f"Found Torrent result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                f"priority {match[3]}")
                            matches.append(match)

                if CONFIG.use_direct():
                    resultlist, nprov = iterate_over_direct_sites(book, searchtype)
                    if not nprov:
                        warn_mode('direct')
                    elif resultlist:
                        match = find_best_result(resultlist, book, searchtype, 'direct')
                        if not good_enough(match):
                            logger.info(
                                f"Direct search for {book['library']} {book['searchterm']} returned no results.")
                        else:
                            logger.info(
                                f"Found Direct result: {searchtype} {round(match[0], 2)}%, "
                                f"{match[1]['NZBprov']} priority {match[3]}")
                            matches.append(match)

                if CONFIG.use_irc():
                    resultlist, nprov = iterate_over_irc_sites(book, searchtype)
                    if not nprov:
                        warn_mode('irc')
                    elif resultlist:
                        match = find_best_result(resultlist, book, searchtype, 'irc')
                        if not good_enough(match):
                            logger.info(f"IRC search for {book['library']} {book['searchterm']} returned no results.")
                        else:
                            logger.info(
                                f"Found IRC result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                f"priority {match[3]}")
                            matches.append(match)

                if CONFIG.use_rss() and rss_resultlist:
                    match = find_best_result(rss_resultlist, book, searchtype, 'rss')
                    if not good_enough(match):
                        logger.info(f"RSS search for {book['library']} {book['searchterm']} returned no results.")
                    else:
                        logger.info(
                            f"Found RSS result: {searchtype} {round(match[0], 2)}%, "
                            f"{match[1]['NZBprov']} priority {match[3]}")
                        matches.append(match)

                # if you can't find the book, try author/title without any "(extended details, series etc)"
                if not matches and '(' in book['bookName']:
                    if CONFIG.use_nzb():
                        resultlist, nprov = iterate_over_znab_sites(book, f"short{searchtype}")
                        if not nprov:
                            warn_mode('nzb')
                        elif resultlist:
                            match = find_best_result(resultlist, book, searchtype, 'nzb')
                            if not good_enough(match):
                                logger.info(
                                    f"NZB short search for {book['library']} {book['searchterm']} returned no results.")
                            else:
                                logger.info(
                                    f"Found NZB result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                    f"priority {match[3]}")
                                matches.append(match)

                    if CONFIG.use_tor():
                        resultlist, nprov = iterate_over_torrent_sites(book, f"short{searchtype}")
                        if not nprov:
                            warn_mode('tor')
                        elif resultlist:
                            match = find_best_result(resultlist, book, searchtype, 'tor')
                            if not good_enough(match):
                                logger.info(
                                    f"Torrent short search for {book['library']} {book['searchterm']} "
                                    f"returned no results.")
                            else:
                                logger.info(
                                    f"Found Torrent result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                    f"priority {match[3]}")
                                matches.append(match)

                    if CONFIG.use_direct():
                        resultlist, nprov = iterate_over_direct_sites(book, f"short{searchtype}")
                        if not nprov:
                            warn_mode('direct')
                        elif resultlist:
                            match = find_best_result(resultlist, book, searchtype, 'direct')
                            if not good_enough(match):
                                logger.info(
                                    f"Direct short search for {book['library']} {book['searchterm']} "
                                    f"returned no results.")
                            else:
                                logger.info(
                                    f"Found Direct result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                    f"priority {match[3]}")
                                matches.append(match)

                    if CONFIG.use_irc():
                        resultlist, nprov = iterate_over_irc_sites(book, f"short{searchtype}")
                        if not nprov:
                            warn_mode('irc')
                        elif resultlist:
                            match = find_best_result(resultlist, book, searchtype, 'irc')
                            if not good_enough(match):
                                logger.info(
                                    f"IRC short search for {book['library']} {book['searchterm']} returned no results.")
                            else:
                                logger.info(
                                    f"Found IRC result: {searchtype} {round(match[0], 2)}%, "
                                    f"{match[1]['NZBprov']} priority {match[3]}")
                                matches.append(match)

                    if CONFIG.use_rss() and rss_resultlist:
                        match = find_best_result(rss_resultlist, book, searchtype, 'rss')
                        if not good_enough(match):
                            logger.info(
                                f"RSS short search for {book['library']} {book['searchterm']} returned no results.")
                        else:
                            logger.info(
                                f"Found RSS result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                f"priority {match[3]}")
                            matches.append(match)

                # if you can't find the book under "books", you might find under general search
                # general search is the same as booksearch for torrents, irc and rss, no need to check again
                if not matches and CONFIG.use_nzb():
                    resultlist, nprov = iterate_over_znab_sites(book, f"general{searchtype}")
                    if not nprov:
                        warn_mode('nzb')
                    elif resultlist:
                        match = find_best_result(resultlist, book, searchtype, 'nzb')
                        if not good_enough(match):
                            logger.info(
                                f"NZB general search for {book['library']} {book['searchterm']} returned no results.")
                        else:
                            logger.info(
                                f"Found NZB result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                f"priority {match[3]}")
                            matches.append(match)

                # if still not found, try general search again without any "(extended details, series etc)"
                # shortgeneral is the same as shortbook for torrents, irc and rss, no need to check again
                if not matches and CONFIG.use_nzb() and '(' in book['searchterm']:
                    resultlist, nprov = iterate_over_znab_sites(book, f"shortgeneral{searchtype}")
                    if not nprov:
                        warn_mode('nzb')
                    elif resultlist:
                        match = find_best_result(resultlist, book, searchtype, 'nzb')
                        if not good_enough(match):
                            logger.info(
                                f"NZB shortgeneral search for {book['library']} {book['searchterm']} "
                                f"returned no results.")
                        else:
                            logger.info(
                                f"Found NZB result: {searchtype} {round(match[0], 2)}%, "
                                f"{match[1]['NZBprov']} priority {match[3]}")
                            matches.append(match)

                # if still not found, try general search again with title only
                if not matches:
                    if CONFIG.use_nzb():
                        resultlist, nprov = iterate_over_znab_sites(book, f"title{searchtype}")
                        if not nprov:
                            warn_mode('nzb')
                        elif resultlist:
                            match = find_best_result(resultlist, book, searchtype, 'nzb')
                            if not good_enough(match):
                                logger.info(
                                    f"NZB title search for {book['library']} {book['searchterm']} returned no results.")
                            else:
                                logger.info(
                                    f"Found NZB result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                    f"priority {match[3]}")
                                matches.append(match)

                    if CONFIG.use_tor():
                        resultlist, nprov = iterate_over_torrent_sites(book, f"title{searchtype}")
                        if not nprov:
                            warn_mode('tor')
                        elif resultlist:
                            match = find_best_result(resultlist, book, searchtype, 'tor')
                            if not good_enough(match):
                                logger.info(
                                    f"Torrent title search for {book['library']} {book['searchterm']} "
                                    f"returned no results.")
                            else:
                                logger.info(
                                    f"Found Torrent result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                    f"priority {match[3]}")
                                matches.append(match)

                    if CONFIG.use_direct():
                        resultlist, nprov = iterate_over_direct_sites(book, f"title{searchtype}")
                        if not nprov:
                            warn_mode('direct')
                        elif resultlist:
                            match = find_best_result(resultlist, book, searchtype, 'direct')
                            if not good_enough(match):
                                logger.info(
                                    f"Direct title search for {book['library']} {book['searchterm']}"
                                    f" returned no results.")
                            else:
                                logger.info(
                                    f"Found Direct result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                    f"priority {match[3]}")
                                matches.append(match)

                    # irchighway says search results without both author and title will be
                    # silently rejected but that doesn't seem to be actioned...
                    if CONFIG.use_irc():
                        resultlist, nprov = iterate_over_irc_sites(book, f"title{searchtype}")
                        if not nprov:
                            warn_mode('irc')
                        elif resultlist:
                            match = find_best_result(resultlist, book, searchtype, 'irc')
                            if not good_enough(match):
                                logger.info(
                                    f"IRC title search for {book['library']} {book['searchterm']} returned no results.")
                            else:
                                logger.info(
                                    f"Found IRC result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                    f"priority {match[3]}")
                                matches.append(match)

                    if CONFIG.use_rss():
                        match = find_best_result(rss_resultlist, book, searchtype, 'rss')
                        if not good_enough(match):
                            logger.info(
                                f"RSS title search for {book['library']} {book['searchterm']} returned no results.")
                        else:
                            logger.info(
                                f"Found RSS result: {searchtype} {round(match[0], 2)}%, {match[1]['NZBprov']} "
                                f"priority {match[3]}")
                            matches.append(match)

            if matches:
                try:
                    highest = max(matches, key=lambda s: (s[0], s[3]))  # sort on percentage and priority
                except TypeError:
                    highest = max(matches, key=lambda s: (str(s[0]), str(s[3])))

                logger.info(
                    f"Requesting {book['library']} download: {round(highest[0], 2)}% {highest[1]['NZBprov']}: "
                    f"{highest[1]['NZBtitle']}")
                if download_result(highest, book) > 1:
                    book_count += 1  # we found it
                db.action("DELETE from failedsearch WHERE BookID=? AND Library=?",
                          (book['bookid'], book['library']))
            elif CONFIG.get_bool('DELAYSEARCH') and not force and do_search and len(modelist):
                res = db.match('SELECT * FROM failedsearch WHERE BookID=? AND Library=?',
                               (book['bookid'], book['library']))
                if res:
                    interval = check_int(res['Interval'], 0)
                else:
                    interval = 0

                db.upsert("failedsearch",
                          {'Count': 0, 'Interval': interval + 1, 'Time': time.time()},
                          {'BookID': book['bookid'], 'Library': book['library']})

            time.sleep(CONFIG.get_int('SEARCH_RATELIMIT'))

        logger.info(f"Search for Wanted items complete, found {book_count} {plural(book_count, 'book')}")

    except Exception:
        logger.error(f'Unhandled exception in search_book: {traceback.format_exc()}')
    finally:
        logger.debug(f"Storing finish time for {thread_name()}")
        db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})
        db.close()
        thread_name("WEBSERVER")
