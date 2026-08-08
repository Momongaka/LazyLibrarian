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


import logging
import re
import threading
import time
import traceback
from urllib.parse import quote, quote_plus, urlencode

from rapidfuzz import fuzz

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.cache import fetch_url, gr_xml_request, json_request
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import (
    check_int,
    check_year,
    clean_name,
    format_author_name,
    get_list,
    make_utf8bytes,
    plural,
    replace_all,
    split_title,
    thread_name,
    unaccented,
)

if CONFIG.get_bool('GOOGLE_TRANS_ID'):
    try:
        from googletrans import Translator
    except (ModuleNotFoundError, ImportError):
        Translator = None
else:
    Translator = None
import asyncio


def set_all_book_authors():
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    totalauthors = 0
    totalrefs = 0
    try:
        db.action('drop table if exists bookauthors')
        db.action('create table bookauthors (AuthorID TEXT, BookID TEXT, Role TEXT, UNIQUE (AuthorID, BookID, Role))')
        books = db.select('SELECT AuthorID,BookID from books')
        for item in books:
            db.action('insert into bookauthors (AuthorID, BookID, Role) values (?, ?, ?)',
                      (item['AuthorID'], item['BookID'], ''), suppress='UNIQUE')
        books = db.select('select bookid,bookname,authorid from books where workpage is not null and workpage != ""')
        for book in books:
            newauthors, newrefs = set_book_authors(book)
            totalauthors += newauthors
            totalrefs += newrefs
    except Exception as e:
        logger.error(str(e))
    db.close()
    msg = f"Added {totalauthors} new authors to database, {totalrefs} new bookauthors"
    logger.debug(msg)
    return totalauthors, totalrefs


def set_book_authors(book):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    newauthors = 0
    newrefs = 0
    try:
        authorlist = get_book_authors(book['bookid'])
        for author in authorlist:
            role = ''
            if 'id' in author:
                # it's a goodreads data source
                authorname = author['name']
                exists = db.match('select authorid from authors where authorid=?', (author['id'],))
                if 'role' in author:
                    role = author['role']
            else:
                # it's a librarything data source
                authorname = format_author_name(author['name'], postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
                exists = db.match('select authorid from authors where authorname=?', (authorname,))
                if 'type' in author:
                    authtype = author['type']
                    if authtype in ['primary author', 'main author', 'secondary author']:
                        role = authtype
                    elif author['role'] in ['Author', '&mdash;'] and author['work'] == 'all editions':
                        role = 'Author'
            if exists:
                authorid = exists['authorid']
            else:
                # try to add new author to database by name
                reason = f"set_book_authors: {book['bookname']}"
                authorname, authorid, new = lazylibrarian.importer.add_author_name_to_db(authorname,
                                                                                         refresh=False,
                                                                                         addbooks=False,
                                                                                         reason=reason,
                                                                                         title=book['bookname'])
                if new and authorid:
                    newauthors += 1
            if authorid:
                db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                          (authorid, book['bookid'], role), suppress='UNIQUE')
                newrefs += 1
    except Exception as e:
        logger.error(f"Error parsing authorlist for {book['bookname']}: {type(e).__name__} {str(e)}")

    db.close()
    return newauthors, newrefs


def set_all_book_series():
    """ Try to set series details for all books """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        books = db.select("select BookID,WorkID,BookName from books where Manual is not '1'")
    finally:
        db.close()
    counter = 0
    if books:
        logger.info(f"Checking series for {len(books)} {plural(len(books), 'book')}")
        for book in books:
            if CONFIG['BOOK_API'] == 'GoodReads':
                workid = book['WorkID']
                if not workid:
                    logger.debug(f"No workid for book {book['BookID']}: {book['BookName']}")
            elif CONFIG['BOOK_API'] == 'GoogleBooks':
                workid = book['BookID']
                if not workid:
                    logger.debug(f"No bookid for book: {book['BookName']}")
            elif CONFIG['BOOK_API'] == 'OpenLibrary':
                workid = book['WorkID']
                if not workid:
                    logger.debug(f"No workid for book {book['BookID']}: {book['BookName']}")
            else:
                workid = None
            if workid:
                serieslist = get_work_series(workid, "set_all_book_series")
                if serieslist:
                    counter += 1
                    set_series(serieslist, book['BookID'])
    delete_empty_series()
    msg = f"Updated {counter} {plural(counter, 'book')}"
    logger.info(f"Series check complete: {msg}")
    return msg


def set_series(serieslist=None, bookid=None, reason=""):
    """ set series details in series/member tables from the supplied dict
        and a displayable summary in book table
        serieslist is a tuple (SeriesID, SeriesNum, SeriesName)
        Return how many api hits and the original publication date if known """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    api_hits = 0
    originalpubdate = ''
    try:
        if bookid:
            # delete any old series-member entries
            db.action('DELETE from member WHERE BookID=?', (bookid,))
            for item in serieslist:
                item = list(item)  # change tuple to list so we can modify it
                if not item[0] and item[2] and item[2][0].isdigit and ')' in item[2]:
                    # incorrect split
                    item[2] = item[2].rsplit(')', 1)[1].strip().strip('-').strip()
                if item[0]:
                    cmd = 'SELECT SeriesID,SeriesName,Status from series where SeriesID=?'
                    key = 0
                else:
                    cmd = 'SELECT SeriesID,SeriesName,Status from series where SeriesName=?'
                    key = 2
                match = db.match(cmd, (item[key],))

                seriesid = ''
                members = []
                if match:
                    seriesid = match['SeriesID']
                    debug_msg = f"Series {item[2]} exists ({seriesid}) {match['Status']}"
                    logger.info(debug_msg)
                    if match['Status'] not in ['Paused', 'Ignored']:
                        members, _api_hits, _src = get_series_members(seriesid, item[2])
                        debug_msg = f"Existing series {item[2]} has {len(members)} members"
                        logger.info(debug_msg)
                        api_hits += _api_hits
                else:
                    # new series, need to set status and get SeriesID
                    debug_msg = f"Series {item[0]}:{item[2]} is new"
                    logger.info(debug_msg)
                    if item[0]:  # ignore incomplete info with no seriesid
                        seriesid = item[0]
                        members, _api_hits, _src = get_series_members(seriesid, item[2])
                        debug_msg = f"New series {item[2]}:{seriesid} has {len(members)} members"
                        logger.info(debug_msg)
                        api_hits += _api_hits

                book = db.match('SELECT AuthorID,WorkID,LT_WorkID from books where BookID=?', (bookid,))
                authorid = book['AuthorID']
                workid = book['WorkID']
                if not workid:
                    workid = book['LT_WorkID']

                if seriesid:
                    control_value_dict = {"BookID": bookid, "SeriesID": seriesid}
                    new_value_dict = {"SeriesNum": item[1]}
                    if reason and not match:
                        new_value_dict['Reason'] = reason
                    if workid:
                        new_value_dict['WorkID'] = workid
                    db.upsert("member", new_value_dict, control_value_dict)

                    if workid:
                        for member in members:
                            if member[3] == workid and check_year(member[5], past=1800, future=0):
                                bookdate = member[5]
                                if check_int(member[6], 0) and check_int(member[7], 0):
                                    bookdate = f"{member[5]}-{member[6]}-{member[7]}"
                                control_value_dict = {"BookID": bookid}
                                new_value_dict = {"BookDate": bookdate, "OriginalPubDate": bookdate}
                                db.upsert("books", new_value_dict, control_value_dict)
                                originalpubdate = bookdate

                    db.action("INSERT INTO seriesauthors ('SeriesID', 'AuthorID') VALUES (?, ?)",
                              (seriesid, authorid), suppress='UNIQUE')
    except Exception as e:
        logger.error(str(e))

    db.close()
    return api_hits, originalpubdate


def get_status(bookid=None, serieslist=None, default=None, adefault=None, authstatus=None):
    """ Get the status for a book according to series/author/newbook/newauthor preferences
        defaults are passed in as newbook or newauthor status """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    new_status = ''
    new_astatus = ''
    bookname = bookid
    try:
        match = db.match('SELECT Status,AudioStatus,AuthorID,BookName from books WHERE BookID=?', (bookid,))
        if not match:
            db.close()
            logger.debug(f"Status new book {bookid}: {default} {adefault}")
            return default, adefault

        authorid = match['AuthorID']
        bookname = match['BookName']
        threadname = thread_name()
        # Is the book part of any series we want or don't want?
        for item in serieslist:
            match = db.match('SELECT Status from series where SeriesName=? COLLATE NOCASE', (item[2],))
            if match and match['Status'] in ['Wanted', 'Skipped', 'Ignored']:
                if CONFIG.get_bool('EBOOK_TAB'):
                    new_status = match['Status']
                if CONFIG.get_bool('AUDIO_TAB'):
                    new_astatus = match['Status']
                if new_status or new_astatus:
                    logger.debug(f"Marking {bookname} as {match['Status']}, series {item[2]}")
                    msg = f"[{threadname}] Series ({item[2]}) is {match['Status']}"
                    db.action("UPDATE books SET ScanResult=? WHERE BookID=?", (msg, bookid))
                    break

        if not new_status and not new_astatus and authstatus in ['Paused', 'Ignored', 'Wanted']:
            # Author we want or don't want?
            wanted_status = 'Skipped'
            if authstatus == 'Wanted':
                wanted_status = authstatus
            if CONFIG.get_bool('EBOOK_TAB'):
                new_status = wanted_status
            if CONFIG.get_bool('AUDIO_TAB'):
                new_astatus = wanted_status
            if new_status or new_astatus:
                logger.debug(f'Marking {bookname} as {wanted_status}, author {authstatus}')
                match = db.match('SELECT AuthorName from authors where AuthorID=?', (authorid,))
                msg = f"[{threadname}] Author ({match['AuthorName']}) is {authstatus}"
                db.action("UPDATE books SET ScanResult=? WHERE BookID=?", (msg, bookid))
    except Exception as e:
        logger.error(str(e))
    db.close()

    if new_status:
        default = new_status
    if new_astatus:
        adefault = new_astatus

    logger.debug(f"Status {bookname}: {default} {adefault}")
    return default, adefault


def delete_empty_series():
    """ remove any series from series table that have no entries in member table, return how many deleted """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    count = 0
    try:
        series = db.select('SELECT SeriesID,SeriesName from series')
        for item in series:
            match = db.match('SELECT BookID from member where SeriesID=?', (item['SeriesID'],))
            if not match:
                logger.info(f"Deleting empty series {item['SeriesName']}:{item['SeriesID']}")
                count += 1
                db.action('DELETE from series where SeriesID=?', (item['SeriesID'],))
    except Exception as e:
        logger.error(str(e))
    db.close()
    return count


def set_work_id(books=None):
    """ Set the goodreads workid for any books that don't already have one
        books is a comma separated list of bookids or if empty, select from database
        Paginate requests to reduce api hits """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    counter = 0
    try:
        pages = []
        if books:
            page = books
            pages.append(page)
        else:
            cmd = "select BookID,BookName from books where WorkID='' or WorkID is null"
            books = db.select(cmd)
            if books:
                counter = 0
                logger.debug(f"Setting WorkID for {len(books)} {plural(len(books), 'book')}")
                page = ''
                for book in books:
                    bookid = book['BookID']
                    if not bookid:
                        logger.debug(f"No bookid for {book['BookName']}")
                    else:
                        if page:
                            page += ','
                        page += bookid
                        counter += 1
                        if counter == 50:
                            counter = 0
                            pages.append(page)
                            page = ''
                if page:
                    pages.append(page)

        params = {"key": CONFIG['GR_API']}
        for page in pages:
            url = '/'.join([CONFIG['GR_URL'], f"book/id_to_work_id/{page}?{urlencode(params)}"])
            try:
                rootxml, _ = gr_xml_request(url, use_cache=False)
                if rootxml is None:
                    logger.debug("Error requesting id_to_work_id page")
                else:
                    resultxml = rootxml.find('work-ids')
                    if len(resultxml):
                        ids = resultxml.iter('item')
                        books = get_list(page)
                        for cnt, item in enumerate(ids):
                            workid = item.text
                            if not workid:
                                logger.debug(f"No workid returned for {books[cnt]}")
                            else:
                                counter += 1
                                control_value_dict = {"BookID": books[cnt]}
                                new_value_dict = {"WorkID": workid}
                                db.upsert("books", new_value_dict, control_value_dict)

            except Exception as e:
                logger.error(f"{type(e).__name__} parsing id_to_work_id page: {str(e)}")
    except Exception as e:
        logger.error(str(e))
    db.close()

    msg = f"Updated {counter} {plural(counter, 'id')}"
    logger.debug(f"set_work_id complete: {msg}")
    return msg


def librarything_wait():
    """ Wait for a second between librarything api calls """
    logger = logging.getLogger(__name__)
    time_now = time.time()
    delay = time_now - lazylibrarian.TIMERS['LAST_LT']
    if delay < 1.0:
        sleep_time = 1.0 - delay
        lazylibrarian.TIMERS['SLEEP_LT'] += sleep_time
        logger.debug(f"LibraryThing sleep {sleep_time:.3f}, total {lazylibrarian.TIMERS['SLEEP_LT']:.3f}")
        time.sleep(sleep_time)
    lazylibrarian.TIMERS['LAST_LT'] = time_now


def get_all_series_authors():
    """ For each entry in the series table, get a list of authors contributing to the series
        and import those authors (but NOT their books) into the database """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        series = db.select('select SeriesID from series')
    finally:
        db.close()
    if series:
        logger.debug(f'Getting series authors for {len(series)} series')
        counter = 0
        total = 0
        for entry in series:
            seriesid = entry['SeriesID']
            result = get_series_authors(seriesid)
            if result:
                counter += 1
                total += result
            else:
                logger.debug(f'No series info found for series {seriesid}')
        msg = f"Updated authors for {counter} series, added {total} new {plural(total, 'author')}"
        logger.debug(f"Series pages complete: {msg}")
    else:
        msg = 'No entries in the series table'
        logger.debug(msg)
    return msg


def get_book_authors(bookid):
    """ Get a list of authors contributing to a book from the goodreads bookpage  """
    logger = logging.getLogger(__name__)
    authorlist = []
    if CONFIG['BOOK_API'] == 'GoodReads':
        params = {"key": CONFIG['GR_API']}
        url = '/'.join([CONFIG['GR_URL'], f"book/show/{bookid}?{urlencode(params)}"])
        try:
            rootxml, _ = gr_xml_request(url)
            if rootxml is None:
                logger.debug(f"Error requesting book {bookid}")
                return []
        except Exception as e:
            logger.error(f"{type(e).__name__} finding book {bookid}: {str(e)}")
            return []

        book = rootxml.find('book')
        authors = book.find('authors')
        anames = authors.iter('author')
        if anames is None:
            logger.warning(f'No authors found for {bookid}')
            return []
        for aname in anames:
            author = {}
            if aname.find('id') is not None:
                author['id'] = aname.find('id').text
            if aname.find('name') is not None:
                author['name'] = aname.find('name').text
            if aname.find('role') is not None:
                role = aname.find('role').text
                if not role:
                    role = ''
                author['role'] = role
            if author:
                authorlist.append(author)
    return authorlist


def add_series_members(seriesid, refresh=False):
    """ Add all members of a series to the database
        Return how many books you added
    """
    count = 0
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        series = db.match('select SeriesName,Status from series where SeriesID=?', (seriesid,))
        if not series:
            logger.error(f"Error getting series name for {seriesid}")
            return 0
        lazylibrarian.SERIES_UPDATE = True
        seriesname = series['SeriesName']
        logger.debug(f"Updating series members for {seriesid}:{seriesname}")
        entrystatus = series['Status']
        if refresh and entrystatus in ['Paused', 'Ignored']:
            db.action("UPDATE series SET Status='Active' WHERE SeriesID=?", (seriesid,))
        members, _api_hits, _src = get_series_members(seriesid, seriesname)
        logger.debug(f"Processing {len(members)} for {seriesname}")
        if refresh and entrystatus in ['Paused', 'Ignored']:
            db.action('UPDATE series SET Status=? WHERE SeriesID=?', (entrystatus, seriesid))
        for member in members:
            # order = member[0]
            # bookname = member[1]
            # authorname = member[2]
            # workid = member[3]
            # authorid = member[4]
            # pubyear = member[5]
            # pubmonth = member[6]
            # pubday = member[7]
            bookid = member[8]
            # Ensure just title, strip out subtitle, series
            member[1], _, _ = split_title(member[2], member[1])
            logger.debug(f"{member[0]}:{member[1]} - {member[2]}")
            book = None
            if bookid:
                keys = lazylibrarian.importer.book_keys()
                cmd = "SELECT * from books WHERE bookid=?"
                for k in keys:
                    cmd += f" or {k}=?"
                book = db.match(cmd, tuple([str(bookid)] * (len(keys) + 1)))
                if not book:
                    cmd = ("SELECT * from books,authors where bookname=? COLLATE NOCASE "
                           "and authorname=? COLLATE NOCASE and books.authorid = authors.authorid")
                    book = db.match(cmd, (member[1], member[2]))
                if book:
                    bookid = book['bookid']
                    db.action("INSERT into member (SeriesID, SeriesNum, WorkID, BookID) VALUES (?, ?, ?, ?)",
                              (seriesid, member[0], member[3], bookid), suppress='UNIQUE')
            if bookid and not book:
                # new addition to series, try to import with default newbook/newauthor statuses
                lazylibrarian.importer.import_book(bookid, "", "", wait=True, reason=f"Series: {seriesname}",
                                                   source=seriesid[:2])
                newbook = db.match("select * from books where bookid=?", (bookid,))
                if newbook:
                    logger.debug(
                        f"Status={newbook['Status']}, AudioStatus={newbook['AudioStatus']}, Series={series['Status']}")
                    # see if this series status overrides defaults
                    if series['Status'] in ['Paused', 'Ignored', 'Wanted']:
                        wanted_status = 'Skipped'
                        if series['Status'] == 'Wanted':
                            wanted_status = 'Wanted'
                        if CONFIG.get_bool('EBOOK_TAB') and newbook['Status'] != wanted_status:
                            db.action("UPDATE books SET Status=? WHERE BookID=?", (wanted_status, bookid))
                            logger.debug(f"Series [{seriesname}] set status to {wanted_status} for {member[1]}")
                        if CONFIG.get_bool('AUDIO_TAB') and newbook['AudioStatus'] != wanted_status:
                            db.action("UPDATE books SET AudioStatus=? WHERE BookID=?", (wanted_status, bookid))
                            logger.debug(f"Series [{seriesname}] set audiostatus to {wanted_status} for {member[1]}")
                    else:
                        # see if author status overrides defaults
                        author = db.match('select Status from authors WHERE AuthorID=?', (member[4],))
                        if author and author['Status'] in ['Paused', 'Ignored', 'Wanted']:
                            wanted_status = 'Skipped'
                            if author['Status'] == 'Wanted':
                                wanted_status = 'Wanted'
                            if CONFIG.get_bool('EBOOK_TAB') and newbook['Status'] != wanted_status:
                                db.action("UPDATE books SET Status=? WHERE BookID=?", (wanted_status, bookid))
                                logger.debug(f"Author {member[4]} set status to {wanted_status} for {member[1]}")
                            if CONFIG.get_bool('AUDIO_TAB') and newbook['AudioStatus'] != wanted_status:
                                db.action("UPDATE books SET AudioStatus=? WHERE BookID=?", (wanted_status, bookid))
                                logger.debug(f"Author {member[4]} set audiostatus to {wanted_status} for {member[1]}")
                    count += 1
        db.action("UPDATE series SET Updated=? WHERE SeriesID=?", (int(time.time()), seriesid))
        logger.debug(f"Found {len(members)} series {plural(len(members), 'member')}, {count} new for {seriesname}")
        searchlogger = logging.getLogger('special.searching')
        for member in members:
            searchlogger.debug(f"{member[0]}: {member[1]} [{member[2]}]")

        cmd = ("select sum(case books.status when 'Ignored' then 0 else 1 end) as Total,"
               "sum(case when books.status == 'Have' then 1 when books.status == 'Open' then 1 "
               "when books.audiostatus == 'Have' then 1 when books.audiostatus == 'Open' then 1 else 0 end) "
               "as Have from books,member,series where member.bookid=books.bookid and "
               "member.seriesid = series.seriesid and series.seriesid=?")
        res = db.match(cmd, (seriesid,))
        if res:
            db.action('UPDATE series SET Have=?, Total=? WHERE SeriesID=?',
                      (check_int(res[1], 0), check_int(res[0], 0), seriesid))
        return count

    except Exception as e:
        logger.error(f"{type(e).__name__} adding series {seriesid}: {str(e)}")
        logger.error(f'{traceback.format_exc()}')
        return count
    finally:
        db.close()
        if 'SERIESMEMBERS' in threading.current_thread().name:
            threading.current_thread().name = 'WEBSERVER'
        lazylibrarian.SERIES_UPDATE = False


def get_series_authors(seriesid):
    """ Get a list of authors contributing to a series
        and import those authors (but NOT their books) into the database
        Return how many authors you added """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        result = db.match("select count(*) as counter from authors")
        start = int(result['counter'])
        result = db.match('select SeriesName,Status from series where SeriesID=?', (seriesid,))
        if not result:
            logger.error(f"Error getting series name for {seriesid}")
            return 0
        if result['Status'] in ['Paused', 'Ignored']:
            logger.debug(
                f"Not getting additional series authors for {result['SeriesName']}, status is {result['Status']}")
            return 0

        seriesname = result['SeriesName']
        members, api_hits, _src = get_series_members(seriesid, seriesname)

        if members:
            for member in members:
                # order = member[0]
                bookname = member[1]
                authorname = member[2]
                # workid = member[3]
                authorid = member[4]
                # pubyear = member[5]
                # pubmonth = member[6]
                # pubday = member[7]
                # bookid = member[8]
                bookname = replace_all(bookname, lazylibrarian.DICTS.get('apostrophe_dict', {}))
                if not authorid:
                    # goodreads gives us all the info we need, librarything/google doesn't
                    base_url = '/'.join([CONFIG['GR_URL'], 'search.xml?q='])
                    params = {"key": CONFIG['GR_API']}
                    searchname = f"{clean_name(bookname)} {clean_name(authorname)}"
                    searchterm = quote_plus(make_utf8bytes(searchname)[0])
                    set_url = f"{base_url + searchterm}&{urlencode(params)}"
                    try:
                        rootxml, in_cache = gr_xml_request(set_url)
                        if not in_cache:
                            api_hits += 1
                        if rootxml is None:
                            logger.warning(f'Error getting XML for {searchname}')
                        else:
                            resultxml = rootxml.iter('work')
                            for item in resultxml:
                                try:
                                    booktitle = item.find('./best_book/title').text
                                    booktitle = replace_all(booktitle, lazylibrarian.DICTS.get('apostrophe_dict', {}))
                                except (KeyError, AttributeError):
                                    booktitle = ""
                                book_fuzz = fuzz.token_set_ratio(booktitle, bookname)
                                if book_fuzz >= 98:
                                    try:
                                        author = item.find('./best_book/author/name').text
                                    except (KeyError, AttributeError):
                                        author = ""
                                    # try:
                                    #     workid = item.find('./work/id').text
                                    # except (KeyError, AttributeError):
                                    #     workid = ""
                                    try:
                                        authorid = item.find('./best_book/author/id').text
                                    except (KeyError, AttributeError):
                                        authorid = ""
                                    logger.debug(f"Author Search found {author} {booktitle}, authorid {authorid}")
                                    break
                        if not authorid:  # try again with title only
                            searchname = clean_name(bookname)
                            if not searchname:
                                searchname = bookname
                            searchterm = quote_plus(make_utf8bytes(searchname)[0])
                            set_url = f"{base_url + searchterm}&{urlencode(params)}"
                            rootxml, in_cache = gr_xml_request(set_url)
                            if not in_cache:
                                api_hits += 1
                            if rootxml is None:
                                logger.warning(f'Error getting XML for {searchname}')
                            else:
                                resultxml = rootxml.iter('work')
                                for item in resultxml:
                                    booktitle = item.find('./best_book/title').text
                                    booktitle = replace_all(booktitle, lazylibrarian.DICTS.get('apostrophe_dict', {}))
                                    book_fuzz = fuzz.token_set_ratio(booktitle, bookname)
                                    if book_fuzz >= 98:
                                        try:
                                            author = item.find('./best_book/author/name').text
                                        except (KeyError, AttributeError):
                                            author = ""
                                        # try:
                                        #     workid = item.find('./work/id').text
                                        # except (KeyError, AttributeError):
                                        #     workid = ""
                                        try:
                                            authorid = item.find('./best_book/author/id').text
                                        except (KeyError, AttributeError):
                                            authorid = ""
                                        logger.debug(f"Title Search found {author} {booktitle}, authorid {authorid}")
                                        break
                        if not authorid:
                            logger.warning(f"GoodReads doesn't know about {authorname} {bookname}")
                    except Exception as e:
                        logger.error(f"Error finding goodreads results: {type(e).__name__} {str(e)}")

                if authorid:
                    lazylibrarian.importer.add_author_to_db(refresh=False, authorid=authorid, addbooks=False,
                                                            reason=f"get_series_authors: {seriesname}")

        result = db.match("select count(*) as counter from authors")
    finally:
        db.close()
    finish = int(result['counter'])
    newauth = finish - start
    logger.info(f"Added {newauth} new {plural(newauth, 'author')} for {seriesname}")
    return newauth


def is_set_or_part(title):
    logger = logging.getLogger(__name__)
    rejected = False
    msg = ''
    if not title:
        return rejected, msg
    m = re.search(r'(\d+)-(\d+)', title)
    if m:
        if check_year(m.group(1), past=1400, future=0):
            logger.debug(f"Allow {title}, looks like a date range")
        else:
            rejected = True
            msg = f'Set or Part {m.group(0)}'
    if re.search(r'\d+ of \d+', title) or \
            re.search(r'\d+/\d+', title) and not re.search(r'\d+/\d+/\d+', title) or re.search(r'\w+\s*/\s*\w+', title):
        rejected = True
        msg = 'Set or Part'
    return rejected, msg


def get_series_members(seriesid=None, seriesname=None, refresh=False):
    """ Ask librarything, hardcover or goodreads for details on all books in a series
        order, bookname, authorname, workid, authorid, pubyear, pubmonth, pubday, bookid
        (workid, authorid, pubdates, bookid are currently goodreads only)
        Return as a list of lists """
    results = []
    api_hits = 0
    source = ''
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        result = db.match('select SeriesName,Status from series where SeriesID=?', (seriesid,))
        if result:
            source = seriesid[:2]
            if not refresh and result['Status'] in ['Paused', 'Ignored']:
                logger.debug(
                    f"Not getting additional series members for {result['SeriesName']}, status is {result['Status']}")
                return results, api_hits, source

        if source == 'GR':
            params = {"format": "xml", "key": CONFIG['GR_API']}
            url = '/'.join([CONFIG['GR_URL'], f'series/{seriesid[2:]}?{urlencode(params)}'])
            try:
                rootxml, in_cache = gr_xml_request(url)
                if not in_cache:
                    api_hits += 1
            except Exception as e:
                logger.error(f"{type(e).__name__} finding series {seriesid}: {str(e)}")
                return [], api_hits, source

            if rootxml is None:
                logger.debug(f"Series {seriesid[2:]}:{seriesname} not recognised at goodreads")
            else:
                works = rootxml.find('series/series_works')
                books = works.iter('series_work')
                if books is None:
                    logger.warning(f'No books found for {seriesid}')
                    return [], api_hits, source
                for book in books:
                    mydict = {}
                    for mykey, location in [('order', 'user_position'),
                                            ('bookname', 'work/best_book/title'),
                                            ('authorname', 'work/best_book/author/name'),
                                            ('workid', 'work/id'),
                                            ('authorid', 'work/best_book/author/id'),
                                            ('pubyear', 'work/original_publication_year'),
                                            ('pubmonth', 'work/original_publication_month'),
                                            ('pubday', 'work/original_publication_day'),
                                            ('bookid', 'work/best_book/id')
                                            ]:
                        if book.find(location) is not None:
                            mydict[mykey] = book.find(location).text
                        else:
                            mydict[mykey] = ""
                    results.append([mydict['order'], mydict['bookname'], mydict['authorname'],
                                    mydict['workid'], mydict['authorid'], mydict['pubyear'], mydict['pubmonth'],
                                    mydict['pubday'], mydict['bookid']])
                logger.debug(f"{source} found {len(results)} members for {seriesname}")

        if source == 'HC':
            results = []
            hc = lazylibrarian.hc.HardCover()
            res = hc.get_series_members(seriesid, seriesname)
            if res:
                source = 'HC'
                for item in res:
                    # item[6] is pubdate, can extract y/m/d
                    results.append([item[0], item[1], item[2], item[4], item[3], item[5], '', '', item[4]])
                logger.debug(f"{source} found {len(results)} members for {seriesname}")

        if source in ['OL', 'LT']:  # googlebooks and openlibrary use librarything series info
            api_hits = 0
            results = []
            if source == 'OL':
                ol = lazylibrarian.ol.OpenLibrary()
                res = ol.get_series_members(seriesid, seriesname)
                if res:
                    for item in res:
                        book = db.match("SELECT authorid,bookid from books WHERE LT_WorkID=?", (item[4],))
                        if book:
                            results.append([item[0], item[1], item[2], item[4], book[0], '', '', '', book[1]])
                        else:
                            results.append([item[0], item[1], item[2], item[4], '', '', '', '', ''])
            logger.debug(f"{source} found {len(results)} members for {seriesname}")
    except Exception as e:
        logger.error(str(e))
    db.close()
    first = False
    filtered = []
    for item in results:
        rejected = False
        try:
            bookname = unaccented(item[1], only_ascii=False)
            order = item[0]
        except (TypeError, IndexError):
            order = 0
            bookname = ''
            rejected = True
        if item[0] is None:
            rejected = 'no index'
            logger.debug(f'Rejected {bookname}: {rejected}')
        if not rejected and CONFIG.get_bool('NO_SETS'):
            is_set, msg = is_set_or_part(str(order))
            if is_set:
                rejected = msg
                logger.debug(f'Rejected {bookname}: {order}, {rejected}')

        if not rejected and CONFIG.get_bool('NO_NONINTEGER_SERIES') and '.' in str(item[0]):
            rejected = f'non-integer {item[0]}'
            logger.debug(f'Rejected {bookname}, {rejected}')
        if not rejected:
            word = str(item[0]).replace('-', '')
            try:
                valid = float(re.findall(r'\d+\.\d+', word)[0])
            except IndexError:
                try:
                    valid = int(re.findall(r'\d+', word)[0])
                except IndexError:
                    valid = False
            if valid is False:
                rejected = f'non-indexed {item[0]}'
                logger.debug(f'Rejected {bookname}, {rejected}')
        if not rejected and check_int(item[0], 0) == 1:
            first = True

        if not rejected:
            filtered.append(item)
    if len(filtered) and not first:
        logger.warning(f"Series {seriesid} ({seriesname}) has {len(filtered)} members but no book 1")
    return filtered, api_hits, source


def get_gb_info(isbn=None, author=None, title=None, expire=False):
    """ GoodReads/OpenLibrary do not always have a book description in api results
        due to restrictive TOS from some providers, and goodreads may not have genre
        Try to get missing info from googlebooks
        Return info dictionary, None if error"""
    if not author or not title or not CONFIG['GB_API']:
        return {}

    logger = logging.getLogger(__name__)
    author = clean_name(author)
    title = clean_name(title)

    baseurl = '/'.join([CONFIG['GB_URL'], 'books/v1/volumes?q='])

    urls = [baseurl + quote_plus(f'inauthor:{author} intitle:{title}')]
    if isbn:
        urls.insert(0, baseurl + quote_plus(f"isbn:{isbn}"))

    for url in urls:
        url += f"&key={CONFIG['GB_API']}"
        if CONFIG['GB_COUNTRY'] and len(CONFIG['GB_COUNTRY']) == 2:
            url += f"&country={CONFIG['GB_COUNTRY']}"
        results, cached = json_request(url, expire=expire)
        if results is None:  # there was an error
            return None
        if results and not cached:
            time.sleep(1)
        if results and 'items' in results:
            high_fuzz = 0
            high_parts = []
            for item in results['items']:
                res = google_book_dict(item)
                book_fuzz = fuzz.token_set_ratio(res['name'], title)
                auth_fuzz = fuzz.token_set_ratio(res['author'], author)
                total_fuzz = int(book_fuzz + auth_fuzz) / 2
                if total_fuzz > high_fuzz:
                    high_fuzz = total_fuzz
                    high_parts = [book_fuzz, auth_fuzz, res['name'], title, res['author'], author]
                fuzzlogger = logging.getLogger('special.fuzz')
                if book_fuzz < CONFIG.get_int('MATCH_RATIO'):
                    fuzzlogger.debug(f"Book fuzz failed, {book_fuzz} [{res['name']}][{title}]")
                elif auth_fuzz < CONFIG.get_int('MATCH_RATIO'):
                    fuzzlogger.debug(f"Author fuzz failed, {auth_fuzz} [{res['author']}][{author}]")
                else:
                    return res
            if 'isbn:' in url:
                stype = 'isbn result'
            else:
                stype = 'inauthor:intitle result'
            if high_parts:
                logger.debug(
                    f"No GoogleBooks match in {len(results['items'])} {stype}{plural(len(results['items']))} "
                    f"({round(high_parts[0], 2)}%/{round(high_parts[1], 2)}%) cached={cached} [{author}:{title}]")
            else:
                logger.debug(
                    f"No GoogleBooks match in {len(results['items'])} {stype}{plural(len(results['items']))} "
                    f"cached={cached} [{author}:{title}]")
            fuzzlogger = logging.getLogger('special.fuzz')
            fuzzlogger.debug(str(high_parts))
    return {}


def google_book_dict(item):
    """ Return all the book info we need as a dictionary or default value if no key """
    mydict = {}
    for val, idx1, idx2, default in [
        ('author', 'authors', 0, ''),
        ('name', 'title', None, ''),
        ('lang', 'language', None, ''),
        ('pub', 'publisher', None, ''),
        ('sub', 'subtitle', None, ''),
        ('date', 'publishedDate', None, '0000'),
        ('rate', 'averageRating', None, 0),
        ('rate_count', 'ratingsCount', None, 0),
        ('pages', 'pageCount', None, 0),
        ('desc', 'description', None, 'Not available'),
        ('link', 'canonicalVolumeLink', None, ''),
        ('img', 'imageLinks', 'thumbnail', 'images/nocover.png'),
        ('genre', 'categories', 0, '')
    ]:
        try:
            if idx2 is None:
                mydict[val] = item['volumeInfo'][idx1]
            else:
                mydict[val] = item['volumeInfo'][idx1][idx2]
        except KeyError:
            mydict[val] = default

    # grab any multiauthor details here
    contributors = []
    try:
        cnt = 1
        while cnt:
            contrib = item['volumeInfo']['authors'][cnt]
            contributors.append(contrib)
            cnt += 1
    except (KeyError, IndexError):
        mydict['contributors'] = contributors

    try:
        if item['volumeInfo']['industryIdentifiers'][0]['type'] in ['ISBN_10', 'ISBN_13']:
            mydict['isbn'] = item['volumeInfo']['industryIdentifiers'][0]['identifier']
        else:
            mydict['isbn'] = ""
    except KeyError:
        mydict['isbn'] = ""

    # googlebooks has a few series naming systems in the authors books page...
    # title or subtitle (seriesname num) eg (Discworld 24)
    # title or subtitle (seriesname #num) eg (Discworld #24)
    # title or subtitle (seriesname Series num)  eg (discworld Series 24)
    # subtitle Book num of seriesname  eg Book 24 of Discworld
    # There may be others...
    #
    try:
        series_num, series = mydict['sub'].split('Book ')[1].split(' of ')
    except (IndexError, ValueError):
        series = ""
        series_num = ""

    if not series:
        for item in [mydict['name'], mydict['sub']]:
            if ' Series ' in item:
                try:
                    series, series_num = item.split('(')[1].split(' Series ')
                    series_num = series_num.rstrip(')').lstrip('#')
                except (IndexError, ValueError):
                    series = ""
                    series_num = ""
            if not series and '#' in item:
                try:
                    series, series_num = item.rsplit('#', 1)
                    series = series.split('(')[1].strip()
                    series_num = series_num.rstrip(')')
                except (IndexError, ValueError):
                    series = ""
                    series_num = ""
            if not series and ' ' in item:
                try:
                    series, series_num = item.rsplit(' ', 1)
                    series = series.split('(')[1].strip()
                    series_num = series_num.rstrip(')')
                    # has to be unicode for isnumeric()
                    if not f"{series_num}".isnumeric():
                        series = ""
                        series_num = ""
                except (IndexError, ValueError):
                    series = ""
                    series_num = ""
            if series and series_num:
                break

    mydict['series'] = series
    mydict['seriesNum'] = series_num
    mydict['genre'] = genre_filter(mydict['genre'])
    return mydict


def ensure_series_in_db(seriesid, seriesname, bookid, reason):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        match = db.match('SELECT Status from series WHERE SeriesName=? and seriesid=?', (seriesname, seriesid))
        if match:
            if match['Status'] not in ['Paused', 'Ignored']:
                add_series_members(seriesid)
        else:
            match = db.match('SELECT SeriesName,Status from series WHERE SeriesID=?', (seriesid,))
            if match:
                logger.warning(f"Ignoring name mismatch for series {seriesid}, [{seriesname}][{match['SeriesName']}]")
                if match['Status'] not in ['Paused', 'Ignored']:
                    add_series_members(seriesid)
            else:
                match = db.match('SELECT SeriesID,Status from series WHERE SeriesName=?', (seriesname,))
                if match:
                    logger.warning(f"SeriesID mismatch for series {seriesname} [{seriesid}][{match['SeriesID']}]")
                # new series or series with new provider
                reason = f"Bookid {bookid}: {reason}"
                db.action('INSERT INTO series (SeriesID, SeriesName, Status, Updated, Reason) '
                          'VALUES (?, ?, ?, ?, ?)',
                          (seriesid, seriesname, CONFIG['NEWSERIES_STATUS'], time.time(), reason))
                if CONFIG['NEWSERIES_STATUS'] not in ['Paused', 'Ignored']:
                    add_series_members(seriesid)
    finally:
        db.close()


def get_work_series(bookid=None, source='GR', reason=""):
    """ Return the series names and numbers in series for the given id as a list of tuples
        For goodreads the id is a WorkID, for librarything/hardcover it's a BookID """
    logger = logging.getLogger(__name__)
    serieslist = []
    if not bookid:
        logger.error("get_work_series - No bookID")
        return serieslist

    if source == 'GR':
        url = '/'.join([CONFIG['GR_URL'], "work/"])
        seriesurl = f"{url + bookid}/series?format=xml&key={CONFIG['GR_API']}"

        rootxml, _ = gr_xml_request(seriesurl)
        if rootxml is None:
            logger.warning(f'Error getting XML for {seriesurl}')
        else:
            resultxml = rootxml.iter('series_work')
            for item in resultxml:
                try:
                    seriesname = item.find('./series/title').text
                    seriesname = seriesname.strip('\n').strip('\n').strip()
                    seriesid = item.find('./series/id').text
                    seriesnum = item.find('./user_position').text
                    seriescount = item.find('./series/series_works_count').text
                except (KeyError, AttributeError):
                    continue

                if CONFIG.get_bool('NO_SINGLE_BOOK_SERIES') and seriescount == '1':
                    logger.debug(f"Ignoring goodreads single-book-series ({seriesid}) {seriesname}")
                elif CONFIG.get_bool('NO_NONINTEGER_SERIES') and seriesnum and '.' in seriesnum:
                    logger.debug(f"Ignoring non-integer series member ({seriesnum}) {seriesname}")
                elif CONFIG.get_bool('NO_SETS') and seriesnum and any(is_set_or_part(seriesnum)):
                    logger.debug(f"Ignoring set or part ({seriesnum}) {seriesname}")
                elif seriesname and seriesid:
                    seriesname = clean_name(seriesname, '&/')
                    if seriesname:
                        seriesnum = clean_name(seriesnum)
                        serieslist.append((f"GR{seriesid}", seriesnum, seriesname))
                        ensure_series_in_db(f"GR{seriesid}", seriesname, bookid, reason)

    elif source == 'HC':
        series_results = []
        hc = lazylibrarian.hc.HardCover()
        res, _ = hc.get_bookdict_for_bookid(bookid)
        if 'series' in res:
            series_results = res['series']
        for item in series_results:
            seriesname = item[0]
            seriesid = item[1]
            seriesnum = item[2]

            if CONFIG.get_bool('NO_NONINTEGER_SERIES') and seriesnum and '.' in seriesnum:
                logger.debug(f"Ignoring non-integer series member ({seriesnum}) {seriesname}")
            elif CONFIG.get_bool('NO_SETS') and any(is_set_or_part(seriesnum)):
                logger.debug(f"Ignoring set or part ({seriesnum}) {seriesname}")
            elif seriesname and seriesid:
                seriesname = clean_name(seriesname, '&/')
                if seriesname:
                    seriesnum = clean_name(seriesnum)
                    serieslist.append((f"HC{seriesid}", seriesnum, seriesname))
                    ensure_series_in_db(f"HC{seriesid}", seriesname, bookid, reason)

    return serieslist


def set_genres(genrelist=None, bookid=None):
    """ set genre details in genres/genrebooks tables from the supplied list
        and a displayable summary in book table """
    if bookid:
        db = database.DBConnection()
        try:
            # delete any old genrebooks entries
            db.action('DELETE from genrebooks WHERE BookID=?', (bookid,))
            for item in genrelist:
                match = db.match('SELECT GenreID from genres where GenreName=? COLLATE NOCASE', (item,))
                if not match:
                    db.action('INSERT into genres (GenreName) VALUES (?)', (item,), suppress='UNIQUE')
                    match = db.match('SELECT GenreID from genres where GenreName=?', (item,))
                db.action('INSERT into genrebooks (GenreID, BookID) VALUES (?,?)',
                          (match['GenreID'], bookid), suppress='UNIQUE')
            if CONFIG.get_bool('WISHLIST_GENRES'):
                book = db.match('SELECT Requester,AudioRequester from books WHERE BookID=?', (bookid,))
                if book['Requester'] and book['Requester'] not in genrelist:
                    genrelist.insert(0, book['Requester'])
                if book['AudioRequester'] and book['AudioRequester'] not in genrelist:
                    genrelist.insert(0, book['AudioRequester'])
            db.action('UPDATE books set BookGenre=? WHERE BookID=?', (', '.join(genrelist), bookid))
        finally:
            db.close()


def genre_filter(genre):
    """
        Filter/replace genre name
        Return new genre name or empty string if rejected
    """

    try:  # google genres sometimes contain commas which we use as list separator
        genre = ' '.join(genre.replace(',', ' ').split())
    except AttributeError:
        return ""

    if not genre:
        return ""

    if lazylibrarian.GRGENRES:
        genre_exclude = lazylibrarian.GRGENRES.get('genreExclude', [])
        genre_exclude_parts = lazylibrarian.GRGENRES.get('genreExcludeParts', [])
        genre_replace = lazylibrarian.GRGENRES.get('genreReplace', {})
    else:
        genre_exclude = []
        genre_exclude_parts = []
        genre_replace = {}

    g_lower = genre.lower()
    # do replacements first so we can merge and then exclude on results
    for item in genre_replace:
        if item.lower() == g_lower:
            genre = genre_replace[item]
            g_lower = genre.lower()
            break

    for item in genre_exclude:
        if item.lower() == g_lower:
            return ""

    for item in genre_exclude_parts:
        if item.lower() in g_lower:
            return ""

    # try to reject author names, check both tom-holt and holt-tom
    words = genre.replace('-', ' ').rsplit(None, 1)
    if len(words) == 2:
        db = database.DBConnection()
        try:
            res = db.match('SELECT authorid from authors WHERE authorname=? COLLATE NOCASE',
                           (f"{words[0]} {words[1]}",))
            if len(res):
                return ""

            res = db.match('SELECT authorid from authors WHERE authorname=? COLLATE NOCASE',
                           (f"{words[1]} {words[0]}",))
        finally:
            db.close()
        if len(res):
            return ""
    return genre


def get_gr_genres(bookid, refresh=False):
    logger = logging.getLogger(__name__)
    if lazylibrarian.GRGENRES:
        genre_users = lazylibrarian.GRGENRES.get('genreUsers', 10)
        genre_limit = lazylibrarian.GRGENRES.get('genreLimit', 3)
    else:
        genre_users = 10
        genre_limit = 3

    url = '/'.join([CONFIG['GR_URL'],
                    f"book/show/{bookid}.xml?key={CONFIG['GR_API']}"])
    genres = []
    try:
        rootxml, in_cache = gr_xml_request(url, use_cache=not refresh)
    except Exception as e:
        logger.error(f"{type(e).__name__} fetching book genres: {str(e)}")
        return genres, False

    if rootxml is None:
        logger.debug("Error requesting book genres")
        return genres, in_cache

    shelves = []
    try:
        shelves = rootxml.find('book/popular_shelves')
        if shelves is None:
            return genres, in_cache
    except (KeyError, AttributeError):
        logger.error(f"Error reading shelves for GoodReads bookid {bookid}")

    for shelf in shelves:
        # check shelf name is used by >= users
        if check_int(shelf.attrib['count'], 0) >= genre_users:
            genres.append([int(shelf.attrib['count']), shelf.attrib['name']])

    # return max (limit) genres sorted by most popular first
    res = sorted(genres, key=lambda x: x[0], reverse=True)
    res = [item[1] for item in res]
    cnt = genre_limit
    genres = []
    for item in res:
        item = genre_filter(item)
        if item and item not in genres:
            genres.append(item)
            cnt -= 1
            if not cnt:
                break
    logger.debug(
        f"GoodReads bookid {bookid} {len(genres)} from {len(res)} {plural(len(res), 'genre')}, cached={in_cache}")
    return genres, in_cache


def get_book_pubdate(bookid, refresh=False):
    logger = logging.getLogger(__name__)
    bookdate = "0000"
    if bookid.isdigit():  # goodreads bookid
        url = '/'.join([CONFIG['GR_URL'],
                        f"book/show/{bookid}.xml?key={CONFIG['GR_API']}"])
        try:
            rootxml, in_cache = gr_xml_request(url, use_cache=not refresh)
        except Exception as e:
            logger.error(f"{type(e).__name__} fetching book publication date: {str(e)}")
            return bookdate, False

        if rootxml is None:
            logger.debug("Error requesting book publication date")
            return bookdate, in_cache

        try:
            bookdate = rootxml.find('book/work/original_publication_year').text
            if bookdate is None:
                bookdate = '0000'
            elif check_int(bookdate, 0, positive=False):  # changed to allow any year for classics
                try:
                    mn = check_int(rootxml.find(
                        './book/work/original_publication_month').text, 0)
                    dy = check_int(rootxml.find(
                        './book/work/original_publication_day').text, 0)
                    if mn and dy:
                        bookdate = f"{bookdate}-{mn:02d}-{dy:02d}"
                except (KeyError, AttributeError):
                    pass
            else:
                bookdate = '0000'
        except (KeyError, AttributeError):
            logger.error(f"Error reading pubdate for GoodReads bookid {bookid} pubdate [{bookdate}]")

        logger.debug(f"GoodReads bookid {bookid} pubdate [{bookdate}] cached={in_cache}")
        return bookdate, in_cache
    if not CONFIG['GB_API']:
        logger.warning('No GoogleBooks API key, check config')
        return bookdate, False

    url = '/'.join([CONFIG['GB_URL'],
                    f"books/v1/volumes/{bookid}"])
    if CONFIG['GB_API']:
        url += f"?key={CONFIG['GB_API']}"

    jsonresults, in_cache = json_request(url)
    if not jsonresults:
        logger.debug(f'No results found for {bookid}')
    else:
        book = google_book_dict(jsonresults)
        if book['date']:
            bookdate = book['date']
    return bookdate, in_cache


def isbnlang(isbn):
    # Try to use shortcut of ISBN identifier codes described here...
    # http://en.wikipedia.org/wiki/List_of_ISBN_identifier_groups
    isbnhead = ""
    cache_hit = False
    thing_hit = False
    book_language = ""
    if len(isbn) == 10:
        isbnhead = isbn[0:3]
    elif len(isbn) == 13:
        isbnhead = isbn[3:6]

    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        if isbnhead:
            if len(isbn) == 13 and isbn.startswith('979'):
                for item in lazylibrarian.isbn_979_dict:
                    if isbnhead.startswith(item):
                        book_language = lazylibrarian.isbn_979_dict[item]
                        break
                if book_language:
                    logger.debug(f"ISBN979 returned {book_language} for {isbnhead}")
            elif len(isbn) == 10 or (len(isbn) == 13 and isbn.startswith('978')):
                for item in lazylibrarian.isbn_978_dict:
                    if isbnhead.startswith(item):
                        book_language = lazylibrarian.isbn_978_dict[item]
                        break
                if book_language:
                    logger.debug(f"ISBN978 returned {book_language} for {isbnhead}")

            if not book_language:
                # Nothing in the isbn dictionary, try any cached results
                match = db.match('SELECT lang FROM languages where isbn=?', (isbnhead,))
                if match:
                    book_language = match['lang']
                    cache_hit = True
                    logger.debug(f"Found cached language [{book_language}] for  {isbnhead}")
    except Exception as e:
        logger.error(str(e))
    db.close()
    return book_language, cache_hit, thing_hit


def language_from_words(words):
    # couldn't be loaded, or configured off
    if not Translator or not CONFIG.get_bool('GOOGLE_TRANS_ID'):
        return 0, 0
    logger = logging.getLogger(__name__)
    logging.getLogger('googletrans').setLevel(logging.CRITICAL)
    logging.getLogger('asyncio').setLevel(logging.CRITICAL)
    logging.getLogger('hpack').setLevel(logging.CRITICAL)
    logging.getLogger('httpcore').setLevel(logging.CRITICAL)
    async def lang_detect(s):
        async with Translator() as translator:
            result = await translator.detect(s)
            return result

    try:
        res = asyncio.run(asyncio.wait_for(lang_detect(words), timeout=10))
        logger.debug(f"Detected {res.lang}:{res.confidence} for {words}")
        return res.lang, res.confidence
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(str(e))
        return 0, 0


def isbn_from_words(words):
    """ Use Google to get an ISBN for a book from words in title and authors name.
        Store the results in the database """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        res = db.match("SELECT ISBN from isbn WHERE Words=?", (words,))
        if res:
            logger.debug(f'Found cached ISBN for {words}')
            db.close()
            return res['ISBN']

        baseurl = "http://www.google.com/search?q=ISBN+"
        search_url = baseurl + quote(words.replace(' ', '+'))

        headers = {'User-Agent': 'w3m/0.5.3',
                   'Content-Type': 'text/plain; charset="UTF-8"',
                   'Content-Transfer-Encoding': 'Quoted-Printable',
                   }
        content, _ = fetch_url(search_url, headers=headers)
        # noinspection Annotator
        re_isbn13 = re.compile(r'97[89](?:-?\d){10,16}|97[89][- 0-9]{10,16}')
        re_isbn10 = re.compile(r'ISBN\x20(?=.{13}$)\d{1,5}([- ])\d{1,7}\1\d{1,6}\1(\d|X)$|[- 0-9X]{10,16}')

        # take the first valid looking answer
        res = re_isbn13.findall(content)
        logger.debug(f'Found {len(res)} ISBN13 for {words}')
        for item in res:
            if len(item) > 13:
                item = item.replace('-', '').replace(' ', '')
            if len(item) == 13:
                db.action("INSERT into isbn (Words, ISBN) VALUES (?, ?)", (words, item))
                db.close()
                return item

        res = re_isbn10.findall(content)
        logger.debug(f'Found {len(res)} ISBN10 for {words}')
        for item in res:
            if len(item) > 10:
                item = item.replace('-', '').replace(' ', '')
            if len(item) == 10:
                db.action("INSERT into isbn (Words, ISBN) VALUES (?, ?)", (words, item))
                db.close()
                return item
    except Exception as e:
        logger.error(str(e))
    db.close()

    logger.debug(f'No valid ISBN found for {words}')
    return None
