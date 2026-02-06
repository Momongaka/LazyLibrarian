#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software, you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>

"""
    This file contains common functions used by the information providers
    Each provider needs entries in the database tables to hold their book_id and author_id
    Not all providers have author_id, in which case we use the author_id from a different provider

    Functions the providers should include:
    def get_author_books(authorid, authorname, bookstatus, audiostatus, entrystatus, refresh, reason)
        Given an authorid and/or authorname, find all books for that author and add them to the database
        Statuses may be passed in, or None to use the configured defaults
        Author should be marked as "Loading" while searching
        entrystatus is the author status that should be set when completed
        refresh may be used to optionally cache the results
        reason should indicate why the author or books are being added
        No return value
        To find the author_id, in those providers that have author_id, where possible use author/title
        combination, ie find me the authorid of the author of this book, rather than relying on name only
        Individual books may be added using add_bookid_to_db (see below)
        If no author_id (not all providers have them), could use find_results (see below)
        with a searchterm of "<ll>author name"

    def add_bookid_to_db(bookid, bookstatus, audiostatus, reason)
        Given a bookid from this provider, add the book to the database (and author if not already there)
        Statuses and reason as above
        No return value

    def find_results(searchterm, queue)
        Searchterm may be passed as an isbn, title, author, or title<ll>author
        On return, queue should contain a list of dicts in the standard layout (see below)
        No return value

    Optional functions if the provider has series info available: Function names should match below:
    If the provider doesn't have series info, the function names should not exist in the module

    def get_series_members(series_id, series_title, queue, refresh)
        Find details of all series members using id or title, add members to queue
        Queue format is a list of tuples (position, title, author_name, author_id, book_id, pubdate)
        sorted on ascending position in the series

    def get_bookdict_for_bookid(bookid)
        Could be a subroutine of add_bookid_to_db, gather the data but don't add to database
        Not all fields in bookdict are available at all providers, missing values empty string or 0
        returns a bookdict in the following format:
        bookdict = {    'authorname': ,
                        'authorid': ,
                        'bookid': ,
                        'bookname': ,
                        'booksub': ,
                        'bookisbn': ,
                        'bookpub': , # comma separated list of publishers
                        'bookdate': , # publication date yyyy-mm-dd or yyyy
                        'booklang': , # comma separated list of languages
                        'booklink': , # source page on provider website
                        'bookrate': , # average rating
                        'bookrate_count': , # how many ratings
                        'bookimg': , # local image file or remote http(s)
                        'bookpages': ,
                        'bookgenre': , # comma separated list of genres
                        'bookdesc': ,
                        'author_fuzz': ,
                        'book_fuzz': ,
                        'isbn_fuzz': ,
                        'highest_fuzz': ,
                        'contributors': , # comma separated list of contributing authors, not including primary
                        'series': , # comma separated tuples (name of series, series_id, position_in_series)
                        'source': # name of provider, eg 'GoodReads' or 'HardCover'
}

"""

import logging
import time
import traceback

from rapidfuzz import fuzz

import lazylibrarian
from lazylibrarian import ROLE, database
from lazylibrarian.bookwork import (
    delete_empty_series,
    get_status,
    is_set_or_part,
    isbn_from_words,
    isbnlang,
)
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import (
    check_int,
    get_list,
    now,
    plural,
    replace_all,
    thread_name,
    today,
    unaccented,
)
from lazylibrarian.images import cache_bookimg, get_book_cover

id_key = {'DNB': 'dnb_id', 'HardCover': 'hc_id', 'GoodReads': 'gr_id',
          'GoogleBooks': 'gb_id', 'OpenLibrary': 'ol_id'}


def validate_bookdict(bookdict, current_db=None):
    """Validate a book dictionary for required fields and rules.
       Try to look up isbn and/or language if not provided
       Return bookdict with amendments """
    logger = logging.getLogger(__name__)
    rejected = []

    for item in ['authorid', 'authorname', 'bookname']:
        if not bookdict.get(item):
            rejected.append(['name', f"{item} not found"])
            return bookdict, rejected

    if not current_db:
        db = database.DBConnection()
        db.connection.create_collation('fuzzy', lazylibrarian.importer.collate_fuzzy)
    else:
        db = current_db

    # noinspection PyBroadException
    try:
        reason = f"Validate bookdict {bookdict['bookname']}"
        if not bookdict['bookisbn'] and CONFIG.get_bool('ISBN_LOOKUP'):
            # try isbn lookup by name
            try:
                res = isbn_from_words(
                    f"{unaccented(bookdict['bookname'], only_ascii=False)} "
                    f"{unaccented(bookdict['authorname'], only_ascii=False)}"
                )
            except Exception as e:
                res = None
                logger.warning(f"Error from isbn: {e}")
            if res:
                logger.debug(f"isbn found {res} for {bookdict['bookid']}")
                if len(res) in [10, 13]:
                    bookdict['bookisbn'] = res

        wantedlanguages = get_list(CONFIG['IMP_PREFLANG'])
        if wantedlanguages and 'All' not in wantedlanguages:
            lang = ''
            languages = get_list(bookdict.get('booklang'))
            if languages:
                for item in languages:
                    if item in wantedlanguages:
                        lang = item
                        break
            elif bookdict.get('bookisbn'):
                lang, _, _ = isbnlang(bookdict['bookisbn'])
            if not lang and languages:
                lang = languages[0]
            if not lang:
                lang = 'Unknown'

            if lang not in wantedlanguages:
                rejected.append(['lang', f'Invalid language [{lang}]'])

        if bookdict['bookpub']:
            for bookpub in bookdict['bookpub']:
                if bookpub.lower() in get_list(CONFIG['REJECT_PUBLISHER']):
                    rejected.append(['publisher', bookpub])
                    break

        source = bookdict.get('source')
        if not source or source not in id_key:
            logger.warning(f"Invalid source {source} for bookdict {bookdict}")
            source = 'OpenLibrary'  # set a usable default

        auth_name, auth_id = lazylibrarian.importer.get_preferred_author(bookdict['authorname'])
        exists = auth_id
        if auth_id:  # If author exists, let's check if the title does too
            # ensure bookdict author details match database
            bookdict['authorid'] = auth_id
            bookdict['authorname'] = auth_name
            # For some reason, collate fuzzy does not get called if the names match
            # so we try a nocase first, then if that fails try fuzzy.
            cmd = (
                f"SELECT BookID,books.{id_key[source]},bookname FROM books,authors "
                "WHERE books.AuthorID = authors.AuthorID and BookName=? COLLATE NOCASE "
                "and books.AuthorID=? and books.Status != 'Ignored' and AudioStatus != 'Ignored'"
            )
            exists = db.match(cmd, (bookdict['bookname'], auth_id))
            if not exists:
                cmd = cmd.replace("NOCASE", 'FUZZY')
                exists = db.match(cmd, (bookdict['bookname'], auth_id))
        if not exists:
            in_db = lazylibrarian.librarysync.find_book_in_db(
                auth_name, bookdict['bookname'],
                source=id_key[source], ignored=False, library='eBook',
                reason=f"{reason}: {auth_id},{bookdict['bookname']}"
            )
            if not in_db:
                in_db = lazylibrarian.librarysync.find_book_in_db(
                    auth_name, bookdict['bookname'],
                    source='bookid', ignored=False, library='eBook',
                    reason=f"{reason}: {auth_id},{bookdict['bookname']}"
                )
            if in_db and in_db[0]:
                cmd = f"SELECT BookID,{id_key[source]} FROM books WHERE BookID=?"
                exists = db.match(cmd, (in_db[0],))

        if exists and bookdict['bookid'] != exists['BookID']:
            # existing bookid might not still be listed at this source so won't refresh.
            # should we keep new bookid or existing one?
            # existing one might have been user edited, might be locked,
            # might have been merged from another authorid or inherited from goodreads?
            # Should probably use the one with the "best" info but since we don't know
            # which that is, keep the old one which is already linked to other db tables
            # but allow info (dates etc.) to be updated
            rejected.append(['dupe', f"Duplicate id ({bookdict['bookid']}/{exists['BookID']})"])
            if not exists[id_key[source]]:
                db.action(f"UPDATE books SET {id_key[source]}=? WHERE BookID=?",
                          (bookdict['bookid'], exists['BookID']))

        if not bookdict['bookisbn'] and CONFIG.get_bool('NO_ISBN'):
            rejected.append(['isbn', 'No ISBN'])

        dic = {
            '.': ' ', '-': ' ', '/': ' ', '+': ' ', '_': ' ', '(': '', ')': '',
            '[': ' ', ']': ' ', '#': '# ', ':': ' ', ';': ' '
        }
        name = replace_all(bookdict['bookname'], dic).strip()
        name = name.lower()
        # remove extra spaces if they're in a row
        name = " ".join(name.split())
        namewords = name.split(' ')
        badwords = get_list(CONFIG['REJECT_WORDS'], ',')

        for word in badwords:
            if (' ' in word and word in name) or word in namewords:
                rejected.append(['word', f'Name contains [{word}]'])
                break

        book_name = unaccented(bookdict['bookname'], only_ascii=False)
        if CONFIG.get_bool('NO_SETS'):
            # allow date ranges eg 1981-95
            is_set, set_msg = is_set_or_part(book_name)
            if is_set:
                rejected.append(['set', set_msg])

        if CONFIG.get_bool('NO_FUTURE'):
            publish_date = bookdict.get('bookdate', '')
            if not publish_date:
                publish_date = ''
            if publish_date > today()[:len(publish_date)]:
                rejected.append(['future', f'Future publication date [{publish_date}]'])

            if CONFIG.get_bool('NO_PUBDATE') and (not publish_date or publish_date == '0000'):
                rejected.append(['date', 'No publication date'])

    except Exception:
        logger.error(f'Unhandled exception in validate_bookdict: {traceback.format_exc()}')
        logger.error(f"{bookdict}")
    finally:
        if not current_db:
            db.close()
    return bookdict, rejected


def warn_about_bookdict(bookdict):
    logger = logging.getLogger(__name__)
    # user said they wanted this book, just warn about failed prefs and allow it
    valid_langs = get_list(CONFIG['IMP_PREFLANG'])
    if 'All' not in valid_langs:
        valid = False
        for item in get_list(bookdict['booklang']):
            if item in valid_langs:
                valid = True
                break
        if not valid:
            msg = f"Book {bookdict['bookname']} language does not match preference, {bookdict['booklang']}"
            logger.warning(msg)

    if CONFIG.get_bool('NO_PUBDATE') and (not bookdict['bookdate'] or bookdict['bookdate'] == '0000'):
        msg = f"Book {bookdict['bookname']} No Publication date: does not match preference"
        logger.warning(msg)

    if CONFIG.get_bool('NO_FUTURE') and bookdict['bookdate'] > today()[:len(bookdict['bookdate'])]:
        msg = (f"Book {bookdict['bookname']} Future publication date does not match preference, "
               f"{bookdict['bookdate']}")
        logger.warning(msg)

    if CONFIG.get_bool('NO_SETS'):
        is_set, set_msg = is_set_or_part(bookdict['bookname'])
        if is_set:
            msg = f"Book {bookdict['bookname']} {set_msg}"
            logger.warning(msg)


def add_bookdict_to_db(book, reason, source):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    locked = False
    exists = db.match('SELECT * from books where bookid=?', (book['bookid'], ))
    if exists:
        locked = exists['Manual']
        if not locked:
            locked = False
        elif locked.isdigit():
            locked = bool(int(locked))

    if locked:
        logger.warning(f"Not updating {book['bookid']} as locked")
        db.close()
        return False

    control_value_dict = {"BookID": book['bookid']}
    new_value_dict = {
        "AuthorID": book['authorid'],
        "BookName": book['bookname'],
        "BookSub": book['booksub'],
        "BookDesc": book['bookdesc'],
        "BookIsbn": book['bookisbn'],
        "BookPub": book['bookpub'],
        "BookGenre": book['bookgenre'],
        "BookImg": book['bookimg'],
        "BookLink": book['booklink'],
        "BookRate": float(book['bookrate']),
        "BookPages": book['bookpages'],
        "BookDate": book['bookdate'],
        "BookLang": book['booklang'],
        "Status": book['status'],
        "AudioStatus": book['audiostatus'],
        "ScanResult": reason,
    }
    if not exists:
        new_value_dict["BookAdded"] = today()
    this_key = id_key[source]
    new_value_dict[this_key] = book['bookid']

    db.upsert("books", new_value_dict, control_value_dict)

    author = db.match('SELECT authorname from authors where authorid=?', (book['authorid'],))
    db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
              (book['authorid'], book['bookid'], ROLE['PRIMARY']), suppress='UNIQUE')
    logger.debug(f"Add primary {book['authorid']}{author['authorname']}")
    if CONFIG.get_bool('CONTRIBUTING_AUTHORS') and book.get('contributors'):
        for entry in book['contributors']:
            if entry[0] and entry[0] != '0':
                auth_id = lazylibrarian.importer.add_author_to_db(authorname=entry[1], refresh=False,
                                                                  authorid=entry[0], addbooks=False,
                                                                  reason=f"Contributor to {book['bookname']}")
            else:
                _, auth_id, _ = lazylibrarian.importer.add_author_name_to_db(author=entry[1], refresh=False,
                                                                             addbooks=False,
                                                                             reason=f"Contributor to {book['bookname']}",
                                                                             title=book['bookname'])
            if auth_id:
                # Add any others as contributing authors
                logger.debug(f"Add secondary {auth_id}:{entry[1]}")
                db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                          (auth_id, book['bookid'], ROLE['CONTRIBUTING']), suppress='UNIQUE')
                lazylibrarian.importer.update_totals(auth_id)
            else:
                logger.debug(f"Unable to add contributor {entry[1]} for {book['bookname']}")

    # Handle series data if present
    if CONFIG.get_bool('ADD_SERIES') and book.get('series'):
        for item in book['series']:
            ser_name = item[0].strip()
            ser_id = str(item[1]).strip()
            src = id_key[source][:2].upper()
            exists = db.match("SELECT * from series WHERE seriesid=?", (ser_id,))
            if not exists:
                if src:
                    exists = db.match("SELECT * from series WHERE seriesname=? "
                                      "and instr(seriesid, ?) = 1", (ser_name, src))
                else:
                    exists = db.match("SELECT * from series WHERE seriesname=? ", (ser_name,))

                if exists:
                    ser_id = exists['SeriesID']
            if not exists:
                logger.debug(f"New series: {ser_id}:{ser_name}: {CONFIG['NEWSERIES_STATUS']}")
                db.action('INSERT INTO series (SeriesID, SeriesName, Status, '
                          'Updated, Reason) VALUES (?,?,?,?,?)',
                          (ser_id, ser_name, CONFIG['NEWSERIES_STATUS'], time.time(), ser_name))
                db.commit()

            # Add author to series
            authmatch = db.match("SELECT * from seriesauthors WHERE "
                                 "SeriesID=? and AuthorID=?", (ser_id, book['authorid']))
            if not authmatch:
                logger.debug(f"Adding {author['authorname']} as series author for {ser_name}")
                db.action('INSERT INTO seriesauthors (SeriesID, AuthorID) VALUES (?, ?)',
                          (ser_id, book['authorid']), suppress='UNIQUE')

            # Add book to series
            match = db.match("SELECT * from member WHERE SeriesID=? AND BookID=?",
                             (ser_id, book['bookid']))
            if not match:
                logger.debug(f"Inserting new member [{item[2]}] for {ser_id}")
                db.action(
                    "INSERT INTO member (SeriesID, BookID, WorkID, SeriesNum) VALUES (?,?,?,?)",
                    (ser_id, book['bookid'], '', item[2]), suppress='UNIQUE')

            # Update series total
            ser = db.match(
                "select count(*) as counter from member where seriesid=?",
                (ser_id,))
            if ser:
                counter = check_int(ser['counter'], 0)
                db.action("UPDATE series SET Total=? WHERE SeriesID=?",
                          (counter, ser_id))
    if exists:
        added = 'updated'
    else:
        added = 'added'
    logger.info(f"{book['bookname']} by {book['authorname']} {added}, {book['status']}/{book['audiostatus']}")
    db.close()
    return True


def add_author_books_to_db(resultqueue, bookstatus, audiostatus, entrystatus, entryreason, authorid,
                           get_series_members=None, get_bookdict_for_bookid=None, cache_hits=0):
    """ There are some time.sleep() calls in the loops to stop the method hogging all the cpu,
        possibly the database calls? Needs more investigation """
    logger = logging.getLogger(__name__)
    searchinglogger = logging.getLogger('special.searching')
    db = database.DBConnection()
    db.connection.create_collation('fuzzy', lazylibrarian.importer.collate_fuzzy)
    threadname = thread_name()
    authorname = ''
    auth_start = time.time()

    # these are reject reasons we might want to override, so optionally add to database as "ignored"
    ignorable = ['future', 'date', 'isbn', 'set', 'word', 'publisher']
    if CONFIG.get_bool('NO_LANG'):
        ignorable.append('lang')

    summary = {'total': 0,
               'bad_lang': 0,
               'duplicates': 0,
               'removed': 0,
               'ignored': 0,
               'added': 0,
               'cover_time': 0,
               'covers': 0,
               'locked': 0,
               'updated': 0,
               'GR_book': 0,
               'GR_lang': 0,
               'LT_lang': 0,
               'GB_lang': 0,
               'cache_hits': cache_hits,
               'uncached': 0
               }

    for bookdict in resultqueue.get():
        time.sleep(0.25)
        """  resultqueue returns dicts...
        'authorname'
        'authorid'
        'bookid'
        'bookname'
        'booksub'
        'bookisbn'
        'bookpub'
        'bookdate'
        'booklang'
        'booklink'
        'bookrate'
        'bookrate_count'
        'bookimg'
        'bookpages'
        'bookgenre'
        'bookdesc'
        'author_fuzz'
        'book_fuzz'
        'isbn_fuzz'
        'highest_fuzz'
        'contributors'
        'series'
        'source'
        """
        if lazylibrarian.STOPTHREADS and threadname == "AUTHORUPDATE":
            logger.debug(f"Aborting {threadname}")
            break
        summary['total'] += 1
        bookdict['status'] = bookstatus
        bookdict['audiostatus'] = audiostatus
        # since we're using this in a loop, pass a preconfigured db
        # to save opening a new cursor and setting fuzzy collation for each entry
        bookdict, rejected = validate_bookdict(bookdict, current_db=db)
        fatal = False
        reason = ''
        ignore_book = False
        ignore_audio = False
        if rejected:
            for reject in rejected:
                if reject[0] not in ignorable:
                    if reject[0] == 'lang':
                        summary['bad_lang'] += 1
                    if reject[0] == 'dupe':
                        summary['duplicates'] += 1
                    if reject[0] == 'name':
                        summary['removed'] += 1
                    fatal = True
                    reason = reject[1]
                    break

            if not CONFIG['IMP_IGNORE']:
                fatal = True

            if not fatal:
                for reject in rejected:
                    if reject[0] in ignorable:
                        ignore_book = True
                        ignore_audio = True
                        summary['ignored'] += 1
                        reason = f"Ignored: {reject[1]}"
                        break

        elif 'author_update' in entryreason:
            reason += f" Author: {bookdict['authorname']}"
        else:
            reason = entryreason

        if fatal:
            logger.debug(f"Rejected {bookdict['bookid']} {reason}")
        else:
            author_id = bookdict['authorid']
            authorname = bookdict['authorname']
            update_value_dict = {}
            exists = db.match("SELECT * from books WHERE BookID=?", (bookdict['bookid'],))
            if authorid != author_id:
                msg = f"Different authorid for {authorname}: {authorid}/{author_id}"
                logger.warning(msg)
            if exists:
                series = db.select('select seriesname from series,member where '
                                   'series.seriesid=member.seriesid and bookid=?', (exists['BookID'],))
                serieslist = []
                for n in series:
                    serieslist.append(n[0])

                locked = exists['Manual']
                if locked is None:
                    locked = False
                elif locked.isdigit():
                    locked = bool(int(locked))
            else:
                serieslist = []
                locked = False
                logger.debug(f"Inserting new book [{bookdict['bookname']}] for [{bookdict['authorname']}]")
                if 'author_update' in entryreason:
                    reason = f"Author: {bookdict['authorname']}"
                else:
                    reason = entryreason
                reason = f"[{thread_name()}] {reason}"
                summary['added'] += 1
                if isinstance(bookdict['booklang'], list):
                    bookdict['booklang'] = ','.join(bookdict['booklang'])
                if not bookdict['booklang']:
                    bookdict['booklang'] = 'Unknown'
                    if bookdict['bookisbn']:
                        booklang, cache_hit, _ = isbnlang(bookdict['bookisbn'])
                        summary['cache_hits'] += cache_hit
                        if booklang:
                            bookdict['booklang'] = booklang

                cover_link = bookdict['bookimg']
                if 'nocover' in cover_link or 'nophoto' in cover_link:
                    start = time.time()
                    cover_link, _ = get_book_cover(bookdict['bookid'], ignore='dnb')
                    summary['cover_time'] += (time.time() - start)
                    summary['covers'] += 1
                elif cover_link and cover_link.startswith('http'):
                    cover_link = cache_bookimg(cover_link, bookdict['bookid'], 'dn')
                if not cover_link:  # no results on search or failed to cache it
                    cover_link = 'images/nocover.png'

                if ignore_book:
                    bookdict['status'] = 'Ignored'
                if ignore_audio:
                    bookdict['audiostatus'] = 'Ignored'

                bookdict['first_publish_year'] = bookdict['bookdate']
                if len(bookdict['first_publish_year']) > 4:
                    bookdict['first_publish_year'] = bookdict['first_publish_year'][:4]

                db.action(
                    f"INSERT INTO books (AuthorID, BookName, BookSub, BookImg, BookLink, BookID, BookDate, "
                    f"BookLang, BookAdded, Status, WorkPage, AudioStatus, ScanResult, OriginalPubDate, "
                    f"BookDesc, BookGenre, BookIsbn, BookPub, BookRate, BookPages,"
                    f"{id_key[bookdict['source']]}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (authorid, bookdict['bookname'], bookdict['booksub'], cover_link, bookdict['booklink'],
                     bookdict['bookid'], bookdict['bookdate'], bookdict['booklang'], now(),
                     bookdict['status'], '', bookdict['audiostatus'], reason, bookdict['first_publish_year'],
                     bookdict['bookdesc'], bookdict['bookgenre'], bookdict['bookisbn'], bookdict['bookpub'],
                     bookdict['bookrate'], bookdict['bookpages'], bookdict['bookid']))

                db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                          (authorid, bookdict['bookid'], ROLE['PRIMARY']), suppress='UNIQUE')

                # NOTE dnb contributing authors do not include authorid, so we add them by name
                if CONFIG.get_bool('CONTRIBUTING_AUTHORS'):
                    for entry in bookdict['contributors']:
                        reason = f"Contributor to {bookdict['bookname']}"
                        auth_id = lazylibrarian.importer.add_author_to_db(authorname=entry[1],
                                                                          refresh=False,
                                                                          authorid=entry[0],
                                                                          addbooks=False,
                                                                          reason=reason)
                        if auth_id:
                            # Add any others as contributing authors
                            db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                                      (auth_id, bookdict['bookid'], ROLE['CONTRIBUTING']), suppress='UNIQUE')
                            lazylibrarian.importer.update_totals(auth_id)
                        else:
                            logger.debug(f"Unable to add {auth_id}")
                        time.sleep(0.25)

            # Leave alone if locked
            if locked:
                summary['locked'] += 1
            else:
                if exists and exists['ScanResult'] and ' publication date' in exists['ScanResult'] \
                        and bookdict['bookdate'] and bookdict['bookdate'] != '0000' \
                        and bookdict['bookdate'] <= today()[:len(bookdict['bookdate'])]:
                    # was rejected on previous scan but bookdate has become valid
                    logger.debug(f"valid bookdate [{bookdict['bookdate']}] previous scanresult "
                                 f"[{exists['ScanResult']}]")

                    update_value_dict["ScanResult"] = f"bookdate {bookdict['bookdate']} is now valid"
                    searchinglogger.debug(f"entry status {entrystatus} {bookstatus},{audiostatus}")
                    book_status, audio_status = get_status(bookdict['bookid'], serieslist, bookstatus,
                                                           audiostatus, entrystatus)
                    if bookdict['status'] not in ['Wanted', 'Open', 'Have'] and not ignore_book:
                        update_value_dict["Status"] = book_status
                    if bookdict['audiostatus'] not in ['Wanted', 'Open', 'Have'] and not ignore_audio:
                        update_value_dict["AudioStatus"] = audio_status
                    searchinglogger.debug(f"status is now {book_status},{audio_status}")
                elif not exists:
                    update_value_dict["ScanResult"] = reason

            if update_value_dict:
                control_value_dict = {"BookID": bookdict['bookid']}
                db.upsert("books", update_value_dict, control_value_dict)

            if CONFIG.get_bool('ADD_SERIES') and get_series_members and get_bookdict_for_bookid:
                try:
                    add_series_entries(bookdict, get_series_members, get_bookdict_for_bookid)
                except Exception as e:
                    logger.error(str(e))
            else:
                logger.debug(f"Not getting series details: {CONFIG.get_bool('ADD_SERIES')}:"
                             f"{bool(get_series_members)}:{bool(get_bookdict_for_bookid)}")

            if not exists:
                typ = 'Added'
                summary['added'] += 1
            else:
                typ = 'Updated'
                summary['updated'] += 1
            msg = (f"[{bookdict['authorname']}] {typ} book: {bookdict['bookname']} [{bookdict['booklang']}] "
                   f"status {bookdict['status']}")
            if CONFIG.get_bool('AUDIO_TAB'):
                msg += f" audio {bookdict['audiostatus']}"
            logger.debug(msg)

    lazylibrarian.importer.update_totals(authorid)
    delete_empty_series()
    # no more books to process, update summaries
    cmd = ("SELECT BookName, BookLink, BookDate, BookImg, BookID from books WHERE AuthorID=? and "
           "Status != 'Ignored' order by BookDate DESC")
    lastbook = db.match(cmd, (authorid,))
    if lastbook:
        lastbookname = lastbook['BookName']
        lastbooklink = lastbook['BookLink']
        lastbookdate = lastbook['BookDate']
        lastbookid = lastbook['BookID']
        lastbookimg = lastbook['BookImg']
    else:
        lastbookname = ""
        lastbooklink = ""
        lastbookdate = ""
        lastbookid = ""
        lastbookimg = ""

    control_value_dict = {"AuthorID": authorid}
    new_value_dict = {
        "Status": entrystatus,
        "LastBook": lastbookname,
        "LastLink": lastbooklink,
        "LastDate": lastbookdate,
        "LastBookID": lastbookid,
        "LastBookImg": lastbookimg
    }
    db.upsert("authors", new_value_dict, control_value_dict)

    resultcount = summary['added'] + summary['updated']
    logger.debug(f"Found {summary['locked']} locked {plural(summary['locked'], 'book')}")
    logger.debug(f"Removed {summary['bad_lang']} unwanted language {plural(summary['bad_lang'], 'result')}")
    logger.debug(f"Removed {summary['removed']} incorrect/incomplete {plural(summary['removed'], 'result')}")
    logger.debug(f"Removed {summary['duplicates']} duplicate {plural(summary['duplicates'], 'result')}")
    logger.debug(f"Ignored {summary['ignored']} {plural(summary['ignored'], 'book')}")
    logger.debug(f"Imported/Updated {resultcount} {plural(resultcount, 'book')} in "
                 f"{int(time.time() - auth_start)} secs")
    if summary['covers']:
        logger.debug(f"Fetched {summary['covers']} {plural(summary['covers'], 'cover')} in "
                     f"{summary['cover_time']:.2f} sec")

    if authorname:
        control_value_dict = {"authorname": authorname}
        new_value_dict = {
            "GR_book_hits": summary['GR_book'],
            "GR_lang_hits": summary['GR_lang'],
            "LT_lang_hits": summary['LT_lang'],
            "GB_lang_change": summary['GB_lang'],
            "cache_hits": summary['cache_hits'],
            "bad_lang": summary['bad_lang'],
            "bad_char": summary['removed'],
            "uncached": summary['uncached'],
            "duplicates": summary['duplicates']
        }
        db.upsert("stats", new_value_dict, control_value_dict)
    db.close()
    return summary


def add_series_entries(bookdict, get_series_members, get_bookdict_for_bookid):
    # bookdict = standard keys
    # get_bookdict_for_bookid function returns standard bookdict and bool in_cache
    # get_series_members returns a list of lists
    # [position, book_title, authorname, authorlink, book_id, pubyear, pubdate]
    logger = logging.getLogger(__name__)
    summary = {'new_authors': 0, 'api_hits': 0, 'cache_hits': 0, 'cover_time': 0, 'cover_count': 0}
    if bookdict['source'] in id_key:
        source = id_key[bookdict['source']]
        source = source.split('_')[0].upper()
    else:
        logger.debug(f"Invalid source in bookdict: {bookdict['source']}")
        return summary

    series_updates = []
    db = database.DBConnection()
    for item in bookdict['series']:
        ser_name = item[0].strip()
        reason = ser_name
        ser_id = str(item[1])
        if not ser_id.startswith(source):
            ser_id = f"{source}{str(item[1])}"
        exists = db.match("SELECT * from series WHERE seriesid=?", (ser_id,))
        if not exists:
            exists = db.match("SELECT * from series WHERE seriesname=? "
                              "and instr(seriesid, ?) = 1", (ser_name, source))
            if exists:
                ser_id = exists['SeriesID']
        if not exists:
            logger.debug(f"New series: {ser_id}:{ser_name}: {CONFIG['NEWSERIES_STATUS']}")
            db.action('INSERT INTO series (SeriesID, SeriesName, Status, '
                      'Updated, Reason) VALUES (?,?,?,?,?)',
                      (ser_id, ser_name, CONFIG['NEWSERIES_STATUS'], time.time(), ser_name))
            db.commit()
            exists = {'Status': CONFIG['NEWSERIES_STATUS']}

        # books in series might be by different authors
        match = db.match(f"SELECT AuthorID from authors WHERE AuthorID=? or {source.lower()}_id=?",
                         (bookdict['authorid'], bookdict['authorid']))
        if match:
            auth_id = match['AuthorID']
        else:
            auth_id = bookdict['authorid']

        authmatch = db.match("SELECT * from seriesauthors WHERE "
                             "SeriesID=? and AuthorID=?", (ser_id, auth_id))
        if not authmatch:
            logger.debug(f"Adding {bookdict['authorname']} as series author for {ser_name}")
            db.action('INSERT INTO seriesauthors (SeriesID, AuthorID) VALUES (?, ?)',
                      (ser_id, auth_id), suppress='UNIQUE')

        match = db.match("SELECT * from member WHERE SeriesID=? AND BookID=?",
                         (ser_id, bookdict['bookid']))
        if item[2] and not match:
            logger.debug(f"Inserting new member [{item[2]}] for {ser_id}")
            db.action(
                "INSERT INTO member (SeriesID, BookID, WorkID, SeriesNum) VALUES (?,?,?,?)",
                (ser_id, bookdict['bookid'], '', item[2]), suppress='UNIQUE')
        ser = db.match(
            "select count(*) as counter from member where seriesid=?",
            (ser_id,))
        if ser:
            counter = check_int(ser['counter'], 0)
            db.action("UPDATE series SET Total=? WHERE SeriesID=?",
                      (counter, ser_id))

        if exists['Status'] in ['Paused', 'Ignored']:
            logger.debug(
                f"Not getting additional series members for {ser_name}, status is "
                f"{exists['Status']}")
        elif ser_id in series_updates:
            logger.debug(f"Series {ser_id}:{ser_name} already updated")
        else:
            seriesmembers = get_series_members(ser_id, ser_name)
            series_updates.append(ser_id)
            if len(seriesmembers) == 1:
                logger.debug(f"Found member {seriesmembers[0][1]} for series {ser_name}")
            else:
                logger.debug(f"Found {len(seriesmembers)} members for series {ser_name}")
            # position, book_title, author_name, hc_author_id, book_id
            for member in seriesmembers:
                db.action("DELETE from member WHERE SeriesID=? AND SeriesNum=?",
                          (ser_id, member[0]))
                auth_name, exists = lazylibrarian.importer.get_preferred_author(member[2])
                if not exists:
                    reason = f"Series contributor {ser_name}:{member[1]}"
                    # Use add_author_to_db with the author ID we already have from the series data
                    # This avoids the author search that can return the wrong author
                    if CONFIG.get_bool('ADD_AUTHOR'):
                        # Only add series author if the global config is set
                        lazylibrarian.importer.add_author_to_db(authorname=auth_name,
                                                                authorid=member[3],
                                                                refresh=False,
                                                                addbooks=False,
                                                                reason=reason
                                                                )
                    else:
                        logger.debug(f"Skipping adding {member[2]}({member[3]}) "
                                     f"for series {ser_name}, "
                                     f"author not in database and ADD_AUTHOR is disabled")
                        continue
                    auth_name, exists = lazylibrarian.importer.get_preferred_author(member[2])
                    if not exists:
                        logger.debug(f"Unable to add {member[2]}({member[3]}) "
                                     f"for series {ser_name}, author not in database")
                        continue

                cmd = f"SELECT * from authors WHERE authorname=? or {source.lower()}_id=?"
                exists = db.match(cmd, (auth_name, member[3]))
                if exists:
                    auth_id = exists['AuthorID']
                    if fuzz.ratio(auth_name.lower().replace('.', ''),
                                  member[2].lower().replace('.', '')) < 95:
                        akas = get_list(exists['AKA'], ',')
                        if member[2] not in akas:
                            akas.append(member[2])
                            db.action("UPDATE authors SET AKA=? WHERE AuthorID=?",
                                      (', '.join(akas), auth_id))
                    match = db.match(
                        "SELECT * from seriesauthors WHERE SeriesID=? and AuthorID=?",
                        (ser_id, auth_id))
                    if not match:
                        logger.debug(f"Adding {auth_name} as series author for {ser_name}")
                        summary['new_authors'] += 1
                        db.action('INSERT INTO seriesauthors (SeriesID, AuthorID) VALUES (?, ?)',
                                  (ser_id, auth_id), suppress='UNIQUE')

                cmd = "SELECT BookID FROM books WHERE BookID=?"
                # make sure bookid is in database, if not, add it
                match = db.match(cmd, (str(member[4]),))
                if not match:
                    newbookdict, in_cache = get_bookdict_for_bookid(str(member[4]))
                    summary['api_hits'] += not in_cache
                    summary['cache_hits'] += in_cache
                    if not newbookdict:
                        logger.debug(f"Unable to add bookid {member[4]} to database")
                        continue

                    cover_link = newbookdict['bookimg']
                    if 'nocover' in cover_link or 'nophoto' in cover_link:
                        start = time.time()
                        cover_link, _ = get_book_cover(newbookdict['bookid'],
                                                       ignore=bookdict['source'].lower())
                        summary['cover_time'] += (time.time() - start)
                        summary['cover_count'] += 1
                    elif cover_link and cover_link.startswith('http'):
                        cover_link = cache_bookimg(cover_link,
                                                   newbookdict['bookid'], source.lower())
                    if not cover_link:  # no results or failed to cache it
                        cover_link = 'images/nocover.png'

                    cmd = ('INSERT INTO books (AuthorID, BookName, BookImg, '
                           'BookLink, BookID, BookDate, BookLang, BookAdded, '
                           'Status, WorkPage, AudioStatus, ScanResult, '
                           f'OriginalPubDate, {source.lower()}_id) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)')

                    if (not newbookdict.get('status') or not
                            newbookdict.get('audiostatus')):
                        newbookdict['status'] = bookdict['status']
                        newbookdict['audiostatus'] = bookdict['audiostatus']

                    db.action(cmd, (auth_id, newbookdict['bookname'],
                                    cover_link, newbookdict['booklink'],
                                    newbookdict['bookid'],
                                    newbookdict['bookdate'],
                                    newbookdict['booklang'], now(),
                                    newbookdict['status'], '',
                                    newbookdict['audiostatus'], reason,
                                    newbookdict['first_publish_year'],
                                    newbookdict['bookid']))

                logger.debug(
                    f"Inserting new member [{member[0]}] for {ser_name}")
                db.action('INSERT INTO member (SeriesID, BookID, SeriesNum) VALUES (?,?,?)',
                          (ser_id, member[4], member[0]), suppress="UNIQUE")

                ser = db.match("select count(*) as counter from member where seriesid=?",
                               (ser_id,))
                if ser:
                    counter = check_int(ser['counter'], 0)
                    db.action("UPDATE series SET Total=? WHERE SeriesID=?", (counter, ser_id))

                lazylibrarian.importer.update_totals(auth_id)
    db.close()
    return summary
