import logging

from lazylibrarian import ROLE, database
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import path_isfile, splitext
from lazylibrarian.formatter import get_list, split_author_names
from lazylibrarian.hc import HardCover
from lazylibrarian.importer import add_author_name_to_db, add_author_to_db, update_totals
from lazylibrarian.librarysync import get_book_info


def get_authors_from_hc():
    logger = logging.getLogger(__name__)
    searchinglogger = logging.getLogger('special.searching')
    newauthors = 0
    if not CONFIG['HC_API']:
        logger.debug("Not processing, HardCover API is disabled")
        return newauthors
    db = database.DBConnection()
    books = db.select("SELECT hc_id,bookid from books WHERE hc_id is not null")
    logger.debug(f"Processing {len(books)} books with HardCover ID")
    for book in books:
        hc = HardCover()
        bookdict, _ = hc.get_bookdict_for_bookid(book['hc_id'])
        searchinglogger.debug(bookdict['title'], bookdict['contributing_authors'])
        for entry in bookdict['contributing_authors']:
            auth_id = add_author_to_db(authorname=entry[1], refresh=False, authorid=entry[0],
                                       addbooks=False, reason=f"Contributor to {bookdict['title']}")
            if auth_id:
                db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                          (auth_id, book['bookid'], ROLE['CONTRIBUTING']), suppress='UNIQUE')
                newauthors += 1
            else:
                logger.debug(f"Unable to add {entry[0]}:{entry[1]}")
    db.close()
    logger.info(f"Added {newauthors} new authors from hardcover book data")
    if newauthors:
        set_counters()
    return newauthors


def get_authors_from_ol():
    logger = logging.getLogger(__name__)
    newauthors = 0
    db = database.DBConnection()
    authors = db.select("SELECT ol_id,authorid,authorname from authors WHERE ol_id is not null")
    logger.debug(f"Processing {len(authors)} authors with OpenLibrary ID")
    # openlibrary work page doesn't give us the full info we need, we only get authorid and role,
    # so we need to look up the author name separately, and its _very_ slow when you do this for every book
    # We could try refreshing all the authors, but that happens periodically anyway
    db.close()
    logger.info(f"Added {newauthors} new authors from OpenLibrary book data")
    if newauthors:
        set_counters()
    return newauthors


def get_authors_from_book_files():
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    newauthors = 0
    selection = db.select("SELECT BookFile,BookID,BookName,AuthorID,Status from books")

    for entry in selection:
        if entry['Status'] in ['Open', 'Have']:
            fname = entry['BookFile']
            extn = splitext(fname)[1]
            if extn.lower() in [".epub", ".mobi", ".azw", ".azw3"]:
                if not path_isfile(fname):
                    logger.error(f'Unable to find {fname}')
                else:
                    res = []
                    try:
                        res = get_book_info(fname)
                        if 'authors' not in res and 'creator' in res:
                            res['authors'] = [res['creator']]
                        if 'authors' in res:
                            authorlist = split_author_names(res['authors'], get_list(CONFIG['MULTI_AUTHOR_SPLIT']))
                            for auth in authorlist:
                                authorname, authorid, added = (
                                    add_author_name_to_db(auth, addbooks=False,
                                                          reason=f"Contributor to {entry['BookName']}"))
                                if authorid:
                                    # Add any new authors as contributing authors
                                    db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                                              (authorid, entry['BookID'], ROLE['CONTRIBUTING']), suppress='UNIQUE')
                                    newauthors += added
                                elif res.get('type', '') == 'epub':
                                    # some epubs have author name in the title field
                                    authorlist = split_author_names(res['title'],
                                                                    get_list(CONFIG['MULTI_AUTHOR_SPLIT']))
                                    for a_name in authorlist:
                                        authorname, authorid, added = (
                                            add_author_name_to_db(a_name, addbooks=False,
                                                                  reason=f"Contributor to {entry['BookName']}"))
                                        if authorid:
                                            # Add any new authors as contributing authors
                                            db.action('INSERT into bookauthors (AuthorID, BookID, Role) '
                                                      'VALUES (?, ?, ?)',
                                                      (authorid, entry['BookID'], ROLE['CONTRIBUTING']),
                                                      suppress='UNIQUE')
                                            newauthors += added
                                else:
                                    logger.debug(f"Unable to add {auth}")
                                    logger.debug(f"{res}")
                        else:
                            logger.debug(f"No authors in {res}")

                    except Exception as e:
                        logger.error(f'get_book_info failed for {fname}, {res} {str(e)}')
        else:
            # a book we don't have, just copy primary author details
            db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                      (entry['AuthorID'], entry['BookID'], ROLE['PRIMARY']), suppress='UNIQUE')

    logger.info(f"Added {newauthors} new authors from book files")
    if newauthors:
        set_counters()
    return newauthors


def set_counters():
    cnt = 0
    db = database.DBConnection()
    authors = db.select('SELECT AuthorID FROM authors WHERE TotalBooks=0')
    for author in authors:
        update_totals(author['AuthorID'])
        cnt += 1
    db.close()
    return cnt


def rebuild_booktable():
    db = database.DBConnection()
    db.action('PRAGMA foreign_keys = OFF')
    db.action('DROP TABLE IF EXISTS temp')
    db.action('ALTER TABLE books RENAME to temp')
    db.action("CREATE TABLE books (BookName TEXT, BookSub TEXT, BookDesc TEXT, BookGenre TEXT, BookIsbn TEXT,"
              " BookPub TEXT, BookRate INTEGER, BookImg TEXT, BookPages INTEGER, BookLink TEXT, BookID TEXT UNIQUE, "
              "BookFile TEXT, BookDate TEXT, BookLang TEXT, BookAdded TEXT, Status TEXT, WorkPage TEXT, Manual TEXT, "
              "SeriesDisplay TEXT, BookLibrary TEXT, AudioFile TEXT, AudioLibrary TEXT, AudioStatus TEXT, "
              "WorkID TEXT, ScanResult TEXT, OriginalPubDate TEXT, Requester TEXT, AudioRequester TEXT, "
              "LT_WorkID TEXT, gr_id TEXT, Narrator TEXT, ol_id TEXT, gb_id TEXT, hc_id TEXT, dnb_id TEXT, ran_id TEXT)")
    db.action("INSERT INTO books SELECT BookName,BookSub,BookDesc,BookGenre,BookIsbn,BookPub,BookRate,BookImg,"
              "BookPages,BookLink,BookID,BookFile,BookDate,BookLang,BookAdded,Status,WorkPage,Manual,SeriesDisplay,"
              "BookLibrary,AudioFile,AudioLibrary,AudioStatus,WorkID,ScanResult,OriginalPubDate,Requester,"
              "AudioRequester,LT_WorkID,gr_id,Narrator,ol_id,gb_id,hc_id,dnb_id,ran_id FROM temp")
    db.action('DROP TABLE temp')
    db.action('PRAGMA foreign_keys = ON')
    db.action('vacuum')
