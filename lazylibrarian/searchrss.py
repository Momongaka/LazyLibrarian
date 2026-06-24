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

import html
import logging
import threading
import time
import traceback

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.config2 import CONFIG
from lazylibrarian.csvfile import finditem
from lazylibrarian.downloadmethods import tor_dl_method
from lazylibrarian.formatter import (
    format_author_name,
    get_list,
    now,
    plural,
    split_author_names,
    split_title,
    thread_name,
    unaccented,
)
from lazylibrarian.images import get_book_cover
from lazylibrarian.importer import add_author_name_to_db, import_book, search_for
from lazylibrarian.librarysync import find_book_in_db
from lazylibrarian.notifiers import custom_notify_snatch, notify_snatch
from lazylibrarian.providers import iterate_over_rss_sites, iterate_over_wishlists
from lazylibrarian.resultlist import process_result_list
from lazylibrarian.scheduling import SchedulerCommand, schedule_job
from lazylibrarian.telemetry import TELEMETRY


def cron_search_rss_book():
    logger = logging.getLogger(__name__)
    if 'SEARCHALLRSS' not in [n.name for n in list(threading.enumerate())]:
        search_rss_book()
    else:
        logger.debug("SEARCHALLRSS is already running")


def cron_search_wishlist():
    logger = logging.getLogger(__name__)
    if 'SEARCHWISHLIST' not in [n.name for n in list(threading.enumerate())]:
        search_wishlist()
    else:
        logger.debug("SEARCHWISHLIST is already running")


def calc_status(bookmatch, search_start, ebook_status, audio_status):
    # calculate status according to existing/author/series statuses
    # return whether status/audiostatus is "Wanted""
    logger = logging.getLogger(__name__)
    want_book = False
    want_audio = False
    db = database.DBConnection()
    try:
        bookid = bookmatch['BookID']
        authorname = bookmatch['AuthorName']
        bookname = bookmatch['BookName']
        cmd = "SELECT Status,AudioStatus,BookName from books WHERE bookid=?"
        book_res = db.match(cmd, (bookid,))
        if book_res:
            logger.debug(f"Found book in db {bookid}:{book_res['BookName']} "
                         f"[{book_res['Status']}:{book_res['AudioStatus']}]")
            bookmatch['Status'] = book_res['Status']
            bookmatch['AudioStatus'] = book_res['AudioStatus']
        cmd = ("SELECT authors.Status,Updated,authors.AuthorID,AuthorName from authors,books "
               "WHERE authors.authorid=books.authorid and bookid=?")
        auth_res = db.match(cmd, (bookid,))
        if auth_res:
            logger.debug(f"Found author in db {auth_res['AuthorID']}:{auth_res['AuthorName']} [{auth_res['Status']}]")
            auth_status = auth_res['Status']
        else:
            auth_status = 'Unknown'
        # not wanted if _any_ series the book is part of is ignored (changed, was paused/ignored)
        cmd = "SELECT SeriesName,Status from series,member where series.SeriesID=member.SeriesID and member.BookID=?"
        series = db.select(cmd, (bookid,))
        reject_series = None
        for ser in series:
            logger.debug(f"Series:{ser['Status']}:{ser['SeriesName']}")
            if ser['Status'] in ['Ignored']:
                reject_series = {"Name": ser['SeriesName'], "Status": ser['Status']}
                break
        # not wanted if currently have book or already marked wanted
        if bookmatch.get('Status', '') in ['Open', 'Wanted', 'Have', 'Ignored']:
            logger.info(
                f"Found book {bookname} by {authorname}, already marked as \"{bookmatch['Status']}\"")
        elif auth_status in ['Ignored'] and auth_res['Updated'] < search_start:
            # not wanted if author is ignored (changed, was paused/ignored)
            logger.info(f'Found book {bookname}, but author is "{auth_status}"')
        elif reject_series and auth_res['Updated'] < search_start:
            logger.info(f"Found book {bookname}, but series \"{reject_series['Name']}\" is {reject_series['Status']}")
        elif ebook_status == 'Wanted':
            # still wanted...
            logger.info(f"Book {bookname} by {authorname} is Wanted (was {bookmatch.get('Status', 'new')})")
            want_book = True
        if bookmatch.get('AudioStatus', '') in ['Open', 'Wanted', 'Have', 'Ignored']:
            logger.info(f"Found audiobook {bookname} by {authorname}, already marked as \"{bookmatch['AudioStatus']}\"")
        elif auth_status in ['Ignored'] and auth_res['Updated'] < search_start:
            # not wanted if author is ignored (changed, was paused/ignored)
            logger.info(f'Found book {bookname}, but author is "{auth_status}"')
        elif reject_series and auth_res['Updated'] < search_start:
            logger.info(f"Found book {bookname}, but series \"{reject_series['Name']}\" is {reject_series['Status']}")
        elif audio_status == 'Wanted':
            # still wanted...
            logger.info(f"Audiobook {bookname} by {authorname} is Wanted (was {bookmatch.get('AudioStatus', 'new')})")
            want_audio = True
    except Exception as e:
        logger.error(str(e))
        logger.error(f"{traceback.format_exc()}")

    db.close()
    return want_book, want_audio


# noinspection PyBroadException
def search_wishlist():
    TELEMETRY.record_usage_data('Search/Wishlist')
    logger = logging.getLogger(__name__)
    thread_name("SEARCHWISHLIST")
    new_books = []
    new_audio = []
    search_start = time.time()
    db = database.DBConnection()
    try:
        logger.debug(f"Storing start time for {thread_name()}")
        db.upsert("jobs", {"Start": time.time()}, {"Name": thread_name()})
        try:
            resultlist, wishproviders = iterate_over_wishlists()
            if not wishproviders:
                logger.debug('No wishlists are set')
                schedule_job(action=SchedulerCommand.STOP, target='search_wishlist')
                return  # No point in continuing

            # for each item in resultlist, add to database if necessary, and mark as wanted
            logger.debug(f"Processing {len(resultlist)} {plural(len(resultlist), 'item')} in wishlists")
            for book in resultlist:
                # we get rss_author, rss_title, maybe rss_isbn, rss_bookid, rss_link
                # we can just use bookid if exists, or try isbn and name matching on author/title if not
                # e.g. NYTimes wishlist
                if lazylibrarian.STOPTHREADS and thread_name() == "SEARCHWISHLIST":
                    logger.debug("Aborting SEARCHWISHLIST")
                    break

                if 'E' in book['types']:
                    ebook_status = "Wanted"
                else:
                    ebook_status = "Skipped"
                if 'A' in book['types']:
                    audio_status = "Wanted"
                else:
                    audio_status = "Skipped"

                if '&#x' in book['rss_title']:
                    book['rss_title'] = html.unescape(book['rss_title'])
                if '&#x' in book['rss_author']:
                    book['rss_author'] = html.unescape(book['rss_author'])
                item = {'Title': book['rss_title']}
                if book.get('rss_bookid'):
                    item['BookID'] = book['rss_bookid']
                if book.get('rss_isbn'):
                    item['ISBN'] = book['rss_isbn']
                if book.get('rss_link'):
                    item['link'] = book['rss_link']
                if book.get('rss_category'):
                    item['category'] = book['rss_category']
                bookmatch = {}
                bookid, _ = find_book_in_db(book['rss_author'], book['rss_title'], ignored=None, library='eBook',
                                            reason=f"wishlist: {book['dispname']}", source='')
                wishlist = book.get('dispname', '')
                want_book = want_audio = False
                if bookid:  # it's in the database
                    bookmatch = db.match('SELECT * from books WHERE bookid=?', (bookid,))
                    authorid = bookmatch['AuthorID']
                    authormatch = db.match('SELECT AuthorName from authors WHERE AuthorID=?', (authorid, ))
                    bookmatch = dict(bookmatch)
                    authormatch = dict(authormatch)
                    logger.debug(f"Found in database, {authormatch['AuthorName']}:{bookmatch['BookName']} "
                                 f"Status {bookmatch['Status']}:{bookmatch['AudioStatus']}")
                    bookmatch['AuthorName'] = authormatch['AuthorName']
                    want_book, want_audio = calc_status(bookmatch, search_start,
                                                        ebook_status, audio_status)
                    item['BookID'] = bookmatch['BookID']
                    if want_book and bookmatch['Status'] not in ['Wanted', 'Ignored', 'Open', 'Have']:
                        cmd = "SELECT BookID from wanted WHERE BookID=? and AuxInfo='eBook' and Status='Snatched'"
                        snatched = db.match(cmd, (bookmatch["BookID"],))
                        if not snatched:
                            new_books.append(item)
                    if want_audio and bookmatch['AudioStatus'] not in ['Wanted', 'Ignored', 'Open', 'Have']:
                        cmd = "SELECT BookID from wanted WHERE BookID=? and AuxInfo='AudioBook' and Status='Snatched'"
                        snatched = db.match(cmd, (bookmatch["BookID"],))
                        if not snatched:
                            new_audio.append(item)
                else:  # bookid not in database yet
                    results = []
                    authorid = None
                    authorlist = split_author_names(book['rss_author'], get_list(CONFIG['MULTI_AUTHOR_SPLIT']))
                    if len(authorlist) > 1:  # use first author if multiples
                        book['rss_author'] = authorlist[0]
                    authorname = format_author_name(book['rss_author'],
                                                    postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
                    authmatch = db.match('SELECT * FROM authors where AuthorName=?', (authorname,))
                    if authmatch:
                        logger.debug(f"Author {authorname} found in database, {authmatch['Status']}")
                        authorid = authmatch['AuthorID']
                        if authmatch['Status'] == 'Ignored':
                            authorname = ''

                    else:
                        logger.debug(f"Author {authorname} not found")
                        newauthor, newid, _ = add_author_name_to_db(author=authorname, addbooks=False,
                                                                    reason=f"wishlist: {book['rss_title']}",
                                                                    title=book['rss_title'])
                        if newauthor and newauthor != authorname:
                            logger.debug(f"Preferred authorname changed from [{authorname}] to [{newauthor}]")
                            authorname = newauthor
                            authorid = newid
                        if not newauthor:
                            logger.warning(f"Authorname {authorname} not added to database")
                            authorname = ''

                    if authorname and book['rss_isbn']:
                        logger.debug(f"Searching using isbn {book['rss_isbn']}:{authorname}")
                        results = search_for(book['rss_isbn'])
                        for result in results:
                            if result['isbn_fuzz'] > CONFIG.get_int('MATCH_RATIO'):
                                logger.info(
                                    f"Found {result['bookid']} ({round(result['isbn_fuzz'], 2)}%) "
                                    f"{result['authorname']}: {result['bookname']}")
                                if result['authorname'] != authorname:
                                    logger.debug(f"isbn authorname mismatch {result['authorname']}:{authorname}")
                                    authorname = result['authorname']
                                    bookmatch = finditem(item, authorname,
                                                         reason=f"wishlist: {book['dispname']}")
                                    if bookmatch:  # it's in the database under isbn authorname
                                        bookmatch = dict(bookmatch)
                                        want_book, want_audio = calc_status(bookmatch, search_start,
                                                                            ebook_status, audio_status)
                                        item['BookID'] = bookmatch['BookID']
                                        if want_book:
                                            new_books.append(item)
                                        if want_audio:
                                            new_audio.append(item)
                                        authorname = None  # to skip adding it again
                                else:
                                    bookmatch = result
                                break

                    if authorname and not bookmatch:
                        searchterm = f"{book['rss_title']}<ll>{authorname}"
                        logger.debug(f"Searching using title {book['rss_title']}:{authorname}")
                        results = search_for(unaccented(searchterm, only_ascii=False))
                        for result in results:
                            if result['author_fuzz'] > CONFIG.get_int('MATCH_RATIO') \
                                    and result['book_fuzz'] > CONFIG.get_int('MATCH_RATIO'):
                                logger.info(
                                    f"Found {result['bookid']} ({round(result['author_fuzz'], 2)}% "
                                    f"{round(result['book_fuzz'], 2)}%) "
                                    f"{result['authorname']}: {result['bookname']}")
                                bookmatch = result
                                break

                    if authorname and not bookmatch:
                        # no match on full searchterm, try splitting out subtitle and series
                        newtitle, _, _ = split_title(authorname, book['rss_title'])
                        if newtitle != book['rss_title']:
                            logger.debug(f"Searching using newtitle {newtitle}:{authorname}")
                            title = newtitle
                            searchterm = f"{title}<ll>{authorname}"
                            results = search_for(unaccented(searchterm, only_ascii=False))
                            for result in results:
                                if result['author_fuzz'] > CONFIG.get_int('MATCH_RATIO') \
                                        and result['book_fuzz'] > CONFIG.get_int('MATCH_RATIO'):
                                    logger.info(
                                        f"Found {result['bookid']} ({round(result['author_fuzz'], 2)}% "
                                        f"{round(result['book_fuzz'], 2)}%) "
                                        f"{result['authorname']}: {result['bookname']}")
                                    bookmatch = result
                                    break

                    if authorname and bookmatch:
                        bookmatch = dict(bookmatch)
                        # dict may have come from results or a database search
                        # results keys are all lowercase, database is mixed case
                        # TODO this needs tidying up
                        for key in ['authorid', 'authorname', 'bookid', 'bookname']:
                            if key in bookmatch:
                                newkey = key.replace('author', 'Author').replace(
                                    'book', 'Book').replace(
                                    'id', 'ID').replace(
                                    'name', 'Name')
                                bookmatch[newkey] = bookmatch[key]

                        bookmatch['AuthorName'] = authorname
                        bookmatch['AuthorID'] = authorid
                        item['BookID'] = bookmatch['BookID']
                        want_book, want_audio = calc_status(bookmatch, search_start, ebook_status, audio_status)
                        if want_book:
                            ebook_status = "Wanted"
                            new_books.append(item)
                        if want_audio:
                            audio_status = "Wanted"
                            new_audio.append(item)

                        book_res = db.match("SELECT * from books WHERE bookid=?", (bookmatch['BookID'],))
                        if not book_res:
                            import_book(bookmatch['BookID'], ebook_status, audio_status,
                                        reason=f"Added from wishlist {wishlist}")
                            # see if it's there now
                            book_res = db.match("SELECT * from books WHERE bookid=?", (bookmatch['BookID'],))
                            if book_res:
                                bookmatch = dict(book_res)
                                bookmatch['AuthorName'] = authorname
                                bookmatch['AuthorID'] = authorid

                    if not bookmatch:
                        msg = f"Skipping book {book['rss_title']} by {book['rss_author']}"
                        if not results:
                            msg += ', No results returned'
                            logger.warning(msg)
                        else:
                            msg += ', No match found'
                            logger.warning(msg)
                            logger.warning(
                                f"Closest match ({round(results[0]['author_fuzz'], 2)}% "
                                f"{round(results[0]['book_fuzz'], 2)}%) "
                                f"{results[0]['authorname']}: {results[0]['bookname']}")
                if bookmatch:
                    if not authorid and bookmatch.get('AuthorID'):
                        authorid = bookmatch.get('AuthorID')
                    new_value_dict = {'AuthorID': authorid, 'BookName': bookmatch['BookName']}
                    try:
                        if want_book:
                            requester = bookmatch.get('Requester')
                            if not requester:
                                requester = ''
                            if wishlist and wishlist not in requester:
                                new_value_dict["Requester"] = f"{' '.join([requester, wishlist]).strip()}"
                            new_value_dict['Status'] = "Wanted"
                        if want_audio:
                            audiorequester = bookmatch.get('AudioRequester')
                            if not audiorequester:
                                audiorequester = ''
                            if wishlist and wishlist not in audiorequester:
                                new_value_dict["AudioRequester"] = f"{' '.join([audiorequester, wishlist]).strip()}"
                            new_value_dict['AudioStatus'] = "Wanted"
                    except Exception as e:
                        logger.debug(f"{type(bookmatch)}{bookmatch}, {new_value_dict}, {e}")

                    if new_value_dict:
                        control_value_dict = {"BookID": bookmatch['BookID']}
                        db.upsert("books", new_value_dict, control_value_dict)

            if new_books or new_audio:
                tot = len(new_books) + len(new_audio)
                logger.info(f"Wishlist marked {tot} {plural(tot, 'item')} as Wanted")
            else:
                logger.debug("Wishlist marked no new items as Wanted")
        except Exception:
            logger.error(f'Unhandled exception in search_wishlist: {traceback.format_exc()}')
        finally:
            logger.debug(f"Storing finish time for {thread_name()}")
            db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})

        logger.debug(f"Wishlist found eBook:{len(new_books)}, Audio:{len(new_audio)}")

        for item in new_books + new_audio:
            bookid = item.get('BookID')
            res = db.match("SELECT BookImg from books where bookid=?", (bookid,))
            coverlink = res['BookImg']
            if not coverlink or 'nocover' in coverlink:
                coverlink, _ = get_book_cover(bookid)
                if coverlink and "nocover" not in coverlink:
                    db.action('UPDATE books SET BookImg=? WHERE BookID=?', (coverlink, bookid))

        search_books = []
        dl_books = []
        for item in new_books:
            link = item.get('link', '')
            cat = item.get('category', '')
            if link and (not cat or 'Ebook' in cat):
                dl_books.append(item)
            else:
                search_books.append({'bookid': item['BookID']})

        logger.debug(f"eBooks: Download {len(dl_books)}, Search {len(search_books)}")

        search_audio = []
        dl_audio = []
        for item in new_audio:
            link = item.get('link', '')
            cat = item.get('category', '')
            if link and (not cat or 'Audiobook' in cat):
                dl_audio.append(item)
            else:
                search_audio.append({'bookid': item['BookID']})

        logger.debug(f"Audio: Download {len(dl_audio)}, Search {len(search_audio)}")

        for item in dl_books:
            logger.info(f"Downloading eBook {item['Title']} from mam")
            success, msg = tor_dl_method(bookid=item['BookID'], tor_title=item['Title'],
                                         tor_url=item['link'], library='eBook', provider='mam')
            if success:
                custom_notify_snatch(f"{item['BookID']} eBook")
                notify_snatch(f"eBook {item['Title']} from mam at {now()}")
                schedule_job(SchedulerCommand.START, target='PostProcessor')
            else:
                logger.error(f"Failed to download {item['Title']}: {msg}")

        for item in dl_audio:
            logger.info(f"Downloading AudioBook {item['Title']} from mam")
            success, msg = tor_dl_method(bookid=item['BookID'], tor_title=item['Title'],
                                         tor_url=item['link'], library='AudioBook', provider='mam')
            if success:
                custom_notify_snatch(f"{item['BookID']} AudioBook")
                notify_snatch(f"AudioBook {item['Title']} from mam at {now()}")
                schedule_job(SchedulerCommand.START, target='PostProcessor')
            else:
                logger.error(f"Failed to download {item['Title']}: {msg}")

        if search_books:
            threading.Thread(target=search_rss_book, name='WISHLISTRSSBOOKS',
                             args=[search_books, 'eBook']).start()
            threading.Thread(target=lazylibrarian.searchbook.search_book, name='WISHLISTBOOKS',
                             args=[search_books, 'eBook']).start()
        if search_audio:
            threading.Thread(target=search_rss_book, name='WISHLISTRSSAUDIO',
                             args=[search_audio, 'AudioBook']).start()
            threading.Thread(target=lazylibrarian.searchbook.search_book, name='WISHLISTAUDIO',
                             args=[search_audio, 'AudioBook']).start()
    except Exception:
        logger.error(f'Unhandled exception in search_wishlist: {traceback.format_exc()}')
    finally:
        db.close()
        thread_name("WEBSERVER")


# noinspection PyBroadException
def search_rss_book(books=None, library=None):
    """
    books is a list of new books to add, or None for backlog search
    library is "eBook" or "AudioBook" or None to search all book types
    """
    TELEMETRY.record_usage_data('Search/Book/RSS')
    logger = logging.getLogger(__name__)
    if not (CONFIG.use_rss()):
        logger.warning('rss search is disabled')
        schedule_job(action=SchedulerCommand.STOP, target='search_rss_book')
        return
    threadname = thread_name()
    if "Thread" in threadname:
        if not books:
            thread_name("SEARCHALLRSS")
        else:
            thread_name("SEARCHRSS")

    db = database.DBConnection()
    try:
        searchbooks = []
        if not books:
            # We are performing a backlog search
            cmd = ("SELECT BookID, AuthorName, Bookname, BookSub, BookAdded, books.Status, AudioStatus from "
                   "books,authors WHERE (books.Status='Wanted' OR AudioStatus='Wanted') and "
                   "books.AuthorID = authors.AuthorID order by BookAdded desc")
            results = db.select(cmd)
            for terms in results:
                searchbooks.append(terms)
        else:
            # The user has added a new book
            for book in books:
                cmd = ("SELECT BookID, AuthorName, BookName, BookSub, books.Status, AudioStatus from books,authors "
                       "WHERE BookID=? AND books.AuthorID = authors.AuthorID")
                results = db.select(cmd, (book['bookid'],))
                for terms in results:
                    searchbooks.append(terms)

        if len(searchbooks) == 0:
            logger.debug("SearchRSS - No books to search for")
            return

        resultlist, nproviders, _ = iterate_over_rss_sites()
        if not nproviders:
            logger.warning('No rss providers are available')
            schedule_job(action=SchedulerCommand.STOP, target='search_rss_book')
            return  # No point in continuing

        logger.info(f"rss Searching for {len(searchbooks)} {plural(len(searchbooks), 'book')}")

        searchlist = []
        for searchbook in searchbooks:
            if lazylibrarian.STOPTHREADS and threadname == "SEARCHALLRSS":
                logger.debug(f"Aborting {threadname}")
                break

            # searchterm is only used for display purposes
            searchterm = f"{searchbook['AuthorName']} {searchbook['BookName']}"
            if searchbook['BookSub']:
                searchterm = f"{searchterm}: {searchbook['BookSub']}"

            if library is None or library == 'eBook' and searchbook['Status'] == "Wanted":
                cmd = "SELECT BookID from wanted WHERE BookID=? and AuxInfo='eBook' and Status='Snatched'"
                snatched = db.match(cmd, (searchbook["BookID"],))
                if snatched:
                    logger.warning(
                        f"eBook {searchbook['AuthorName']} {searchbook['BookName']} already marked "
                        f"snatched in wanted table")
                else:
                    searchlist.append(
                        {"bookid": searchbook['BookID'],
                         "bookName": searchbook['BookName'],
                         "bookSub": searchbook['BookSub'],
                         "authorName": searchbook['AuthorName'],
                         "library": "eBook",
                         "searchterm": searchterm})

            if library is None or library == 'AudioBook' and searchbook['AudioStatus'] == "Wanted":
                cmd = "SELECT BookID from wanted WHERE BookID=? and AuxInfo='AudioBook' and Status='Snatched'"
                snatched = db.match(cmd, (searchbook["BookID"],))
                if snatched:
                    logger.warning(
                        f"AudioBook {searchbook['AuthorName']} {searchbook['BookName']} already marked "
                        f"snatched in wanted table")
                else:
                    searchlist.append(
                        {"bookid": searchbook['BookID'],
                         "bookName": searchbook['BookName'],
                         "bookSub": searchbook['BookSub'],
                         "authorName": searchbook['AuthorName'],
                         "library": "AudioBook",
                         "searchterm": searchterm})

        rss_count = 0
        for book in searchlist:
            if lazylibrarian.STOPTHREADS and threadname == "SEARCHALLRSS":
                logger.debug(f"Aborting {threadname}")
                break
            if book['library'] == 'AudioBook':
                searchtype = 'audio'
            else:
                searchtype = 'book'
            found = process_result_list(resultlist, book, searchtype, 'rss')

            # if you can't find the book, try title without any "(extended details, series etc.)"
            if not found and '(' in book['bookName']:  # anything to shorten?
                searchtype = f"short{searchtype}"
                found = process_result_list(resultlist, book, searchtype, 'rss')

            if not found:
                logger.info(f"rss Searches for {book['library']} {book['searchterm']} returned no results.")
            if found > 1:
                rss_count += 1

        logger.info(f"rss Search for Wanted items complete, found {rss_count} {plural(rss_count, 'book')}")
        logger.debug(f"Storing finish time for {thread_name()}")
        db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})

    except Exception:
        logger.error(f'Unhandled exception in search_rss_book: {traceback.format_exc()}')
    finally:
        db.close()
        thread_name("WEBSERVER")
