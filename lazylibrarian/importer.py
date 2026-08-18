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
import sqlite3
import string
import threading
import time
import traceback
from queue import Queue
from urllib.parse import unquote_plus

from rapidfuzz import fuzz

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.au import Audible
from lazylibrarian.cache import cache_img, ImageType
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import today, unaccented, format_author_name, \
    get_list, check_int, thread_name, plural
from lazylibrarian.gb import GoogleBooks
from lazylibrarian.gr import GoodReads
from lazylibrarian.grsync import grfollow
from lazylibrarian.hc import HardCover
from lazylibrarian.images import get_author_image, img_id
from lazylibrarian.ol import OpenLibrary
from lazylibrarian.processcontrol import get_info_on_caller


def is_valid_authorid(authorid: str, api=None) -> bool:
    if not authorid or not isinstance(authorid, str):
        return False  # Reject blank, or non-string
    if api is None:
        api = CONFIG['BOOK_API']
    # GoogleBooks doesn't provide authorid, so we use one of the other sources
    if authorid.isdigit() and api in ['GoodReads', 'GoogleBooks', 'HardCover', 'Audible']:
        return True
    if authorid.startswith('OL') and authorid.endswith('A') and api in ['OpenLibrary', 'GoogleBooks']:
        return True
    return False


def get_preferred_author_name(author: str) -> (str, bool):
    # Look up an authorname in the database, if not found try fuzzy match
    # Return possibly changed authorname and whether found in library
    logger = logging.getLogger(__name__)
    author = format_author_name(author, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
    match = ''
    db = database.DBConnection()
    try:
        check_exist_author = db.match('SELECT * FROM authors where AuthorName=?', (author,))
        if check_exist_author:
            match = check_exist_author['AuthorID']
        else:  # If no exact match, look for a close fuzzy match to handle misspellings, accents or AKA
            match_name = author.lower().replace('.', '')
            res = db.action('select AuthorID,AuthorName,AKA from authors')
            for item in res:
                aname = item['AuthorName']
                if aname:
                    match_fuzz = fuzz.ratio(aname.lower().replace('.', ''), match_name)
                    if match_fuzz >= CONFIG.get_int('NAME_RATIO'):
                        logger.debug(f"Fuzzy match [{item['AuthorName']}] {round(match_fuzz, 2)}% for [{author}]")
                        author = item['AuthorName']
                        match = item['AuthorID']
                        break
                akas = get_list(item['AKA'], ',')
                if akas:
                    for aka in akas:
                        match_fuzz = fuzz.token_set_ratio(aka.lower().replace('.', '').replace(',', ''), match_name)
                        if match_fuzz >= CONFIG.get_int('NAME_RATIO'):
                            logger.debug(f"Fuzzy AKA match [{aka}] {round(match_fuzz, 2)}% for [{author}]")
                            author = item['AuthorName']
                            match = item['AuthorID']
                            break
    finally:
        db.close()
    return author, match


def add_author_name_to_db(author=None, refresh=False, addbooks=None, reason=None, title=None):
    # get authors name in a consistent format, look them up in the database
    # if not in database, try to import them.
    # return authorname,authorid,new where new=False if author already in db, new=True if added
    # authorname returned is our preferred name, or empty string if not found or unable to add
    logger = logging.getLogger(__name__)
    if not reason:
        program, method, lineno = get_info_on_caller(depth=1)
        if lineno > 0:
            reason = f"{program}:{method}:{lineno}"
        else:
            reason = 'Unknown reason in add_author_name_to_db'

    if addbooks is None:  # we get passed True/False or None
        addbooks = CONFIG.get_bool('NEWAUTHOR_BOOKS')

    new = False
    author_info = {}
    if not author or len(author) < 2 or 'unknown' in author.lower() or 'anonymous' in author.lower():
        logger.debug(f'Invalid Author Name [{author}]')
        return "", "", False

    unquoted_author = unquote_plus(author)
    for token in ['<', '>', '=', '"']:
        if token in unquoted_author:
            logger.warning(f'Cannot set authorname, contains "{token}"')
            return "", "", False

    db = database.DBConnection()
    check_exist_author = None
    try:
        # Check if the author exists, and import the author if not,
        req_author = author
        author, exists = get_preferred_author_name(req_author)
        if exists:
            check_exist_author = db.match('SELECT * FROM authors where AuthorName=?', (author,))

        if not exists and (CONFIG.get_bool('ADD_AUTHOR') or reason.startswith('API')):
            logger.debug(f'Author {author} not found in database, adding...')
            # no match for supplied author, but we're allowed to add new ones
            if title:
                search = f"{author}<ll>{title}"
            else:
                search = author

            api_sources = [
                ['OL', OpenLibrary(search), 'ol_id', 'OL_API'],
                ['GR', GoodReads(search), 'gr_id', 'GR_API'],
                ['HC', HardCover(search), 'hc_id', 'HC_API'],
                ['AU', Audible(search), 'au_id', 'AU_API'],
                ['GB', None, 'authorid', 'GB_API'],
            ]

            # GB doesn't have authorid so we use one of the others...
            if CONFIG['OL_API']:
                api_sources[3][1] = api_sources[0][1]
            elif CONFIG['GR_API']:
                api_sources[3][1] = api_sources[1][1]
            elif CONFIG['HC_API']:
                api_sources[3][1] = api_sources[2][1]
            elif CONFIG['AU_API']:
                api_sources[3][1] = api_sources[3][1]

            if CONFIG['BOOK_API'] == "GoodReads":
                api_sources.insert(0, api_sources.pop(1))
            elif CONFIG['BOOK_API'] == "HardCover":
                api_sources.insert(0, api_sources.pop(2))
            elif CONFIG['BOOK_API'] == "GoogleBooks":
                api_sources.insert(0, api_sources.pop(3))
            elif CONFIG['BOOK_API'] == "Audible":
                api_sources.insert(0, api_sources.pop(4))
            if not CONFIG.get_bool('MULTI_SOURCE'):
                api_sources = [api_sources[0]]

            match_fuzz = 0
            for api_source in api_sources:
                if not CONFIG[api_source[3]] or not api_source[1]:
                    logger.debug(f"{api_source[3]} is disabled")
                else:
                    logger.debug(f"Finding {api_source[0]} author ID for {author}")
                    book_api = api_source[1]
                    author_info = book_api.find_author_id(refresh=True)
                    if author_info:
                        # only try to add if data matches found author data
                        authorname = author_info['authorname']
                        # "J.R.R. Tolkien" is the same person as "J. R. R. Tolkien" and "J R R Tolkien"
                        match_auth = author.replace('.', ' ')
                        match_auth = ' '.join(match_auth.split())
                        match_name = authorname.replace('.', ' ')
                        match_name = ' '.join(match_name.split())
                        match_name = unaccented(match_name, only_ascii=False)
                        match_auth = unaccented(match_auth, only_ascii=False)
                        # allow a degree of fuzziness to cater for different accented character handling.
                        # filename may have the accented or un-accented version of the character
                        # We stored GoodReads/OpenLibrary author name in author_info, so store in LL db under that
                        # fuzz.ratio doesn't lowercase for us
                        match_fuzz = fuzz.ratio(match_auth.lower(), match_name.lower())
                        if match_fuzz >= CONFIG.get_int('NAME_RATIO'):
                            break
                        match_fuzz = fuzz.partial_ratio(match_auth.lower(), match_name.lower())
                        if match_fuzz >= CONFIG.get_int('NAME_PARTNAME'):
                            break
                        else:
                            logger.debug(
                                f"Failed to match author [{author}] to authorname [{match_name}] fuzz [{match_fuzz}]")

            if not author_info:
                return "", "", "", False

            # To save loading hundreds of books by unknown authors at GR or GB, ignore unknown
            if "unknown" not in author.lower() and 'anonymous' not in author.lower() and \
                    match_fuzz >= CONFIG.get_int('NAME_RATIO'):
                # use "intact" name for author that we stored in
                # author_dict, not one of the various mangled versions
                # otherwise the books appear to be by a different author!
                author = author_info['authorname']
                authorid = author_info['authorid']
                # this new authorname may already be in the
                # database, so check again
                check_exist_author = db.match('SELECT * FROM authors where AuthorID=?', (authorid,))
                if not check_exist_author:
                    check_exist_author = db.match('SELECT * FROM authors where AuthorName=? COLLATE NOCASE', (author,))
                if check_exist_author:
                    logger.debug(f'Found authorname {author} in database')
                    new = False
                else:
                    logger.info(f"Adding new author [{author}] {authorid} {reason} addbooks={addbooks}")
                    try:
                        ret_id = add_author_to_db(authorname=author, refresh=refresh, authorid=authorid,
                                                  addbooks=addbooks, reason=reason)
                        if ret_id and ret_id != authorid:
                            logger.debug(f"Authorid mismatch {authorid}/{ret_id}")
                            authorid = ret_id
                        check_exist_author = db.match('SELECT * FROM authors where AuthorID=?', (authorid,))
                        if not check_exist_author:
                            check_exist_author = db.match('SELECT * FROM authors where AuthorName=? '
                                                          'COLLATE NOCASE', (author,))
                        if check_exist_author:
                            logger.debug(f"Added new author [{check_exist_author['AuthorName']}] "
                                         f"{check_exist_author['AuthorID']}")
                            new = True
                        else:
                            logger.debug(f"Failed to add author [{author}] {authorid} to database")
                    except Exception as e:
                        logger.error(f'Failed to add author [{author}] to db: {type(e).__name__} {str(e)}')

        # check author exists in db, either newly loaded or already there, maybe under aka
        if check_exist_author:
            akas = get_list(check_exist_author['AKA'], ',')
            new_aka = False
            aka = author_info.get('aka', '').replace(',', '')
            if aka and aka not in akas:
                akas.append(aka)
                new_aka = True
            req_author = req_author.replace(',', '')
            if author != req_author and req_author not in akas:
                akas.append(req_author)
                new_aka = True
            if new_aka:
                db.action("UPDATE authors SET AKA=? WHERE AuthorID=?",
                          (', '.join(akas), check_exist_author['AuthorID']))
        else:
            logger.debug(f"Failed to match author [{author}] in database")
            return "", "", "", False
    finally:
        db.close()
        return check_exist_author['AuthorName'], check_exist_author['AuthorID'], check_exist_author['asin'], new


def merge(author, src_dict, src_key, api_name):
    if src_dict:
        author[src_key] = src_dict['authorid']
        for k, v in src_dict.items():
            if not author.get(k) or (v and CONFIG['BOOK_API'] == api_name):
                author[k] = v


def get_all_author_details(authorid='', authorname=None):
    # fetch as much data as you can on an author using all configured sources
    #
    logger = logging.getLogger(__name__)
    # this keys in this dict must match the database columns
    author = {}
    ol_id = gr_id = hc_id = au_id = None
    gr_name = ol_name = hc_name = au_name = ''
    ol_author = gr_author = hc_author = au_author = {}

    db = database.DBConnection()
    match = db.match(
        'SELECT ol_id, gr_id, hc_id, au_id, asin, authorname FROM authors WHERE authorid=?',
        (authorid,)
    )
    if match:
        ol_id = match['ol_id']
        gr_id = match['gr_id']
        hc_id = match['hc_id']
        au_id = match['au_id']
        if not authorname:
            authorname = match['authorname']

    if CONFIG['OL_API'] and (CONFIG['BOOK_API'] in ['OpenLibrary', 'GoogleBooks'] or CONFIG.get_bool('MULTI_SOURCE')):
        if not ol_id and authorid.startswith('OL'):
            ol_id = authorid
        if not ol_id and authorname and 'unknown' not in authorname.lower() and 'anonymous' not in authorname.lower():
            searchterm = authorname
            match = db.match('SELECT bookname FROM books WHERE authorid=?', (authorid,))
            if match:
                searchterm = f"{authorname}<ll>{match['bookname']}"
            ol = OpenLibrary(searchterm)
            ol_author = ol.find_author_id()
            if ol_author:
                ol_id = ol_author['authorid']
        if ol_id:
            ol = OpenLibrary(ol_id)
            ol_author = ol.get_author_info(authorid=ol_id)
            if not authorname and 'authorname' in ol_author:
                authorname = ol_author['authorname']

    if CONFIG['BOOK_API'] == 'Audible' or CONFIG.get_bool('MULTI_SOURCE'):
        asin = None
        if not au_id and authorname and 'unknown' not in authorname.lower() and 'anonymous' not in authorname.lower():
            au = Audible(authorname)
            au_author = au.find_author_id()
            if au_author:
                au_id = au_author['authorid']
                asin = au_author['asin']
        if au_id:
            au = Audible(au_id)
            au_author = au.get_author_info(authorid=au_id, asin=asin)
            if not authorname and 'authorname' in au_author:
                authorname = au_author['authorname']

    if CONFIG['HC_API'] and (CONFIG['BOOK_API'] in ['HardCover', 'GoogleBooks'] or CONFIG.get_bool('MULTI_SOURCE')):
        # if not hc_id and not CONFIG['GR_API'] and authorid.isnumeric():  # could be gr or hc, won't be both!
        #     hc_id = authorid
        if not hc_id and authorname and 'unknown' not in authorname.lower() and 'anonymous' not in authorname.lower():
            searchterm = authorname
            match = db.match('SELECT bookname FROM books WHERE authorid=?', (authorid,))
            if match:
                searchterm = f"{authorname}<ll>{match['bookname']}"
            hc = HardCover(searchterm)
            hc_author = hc.find_author_id()
            if hc_author:
                hc_id = hc_author['authorid']
        if hc_id:
            hc = HardCover(authorname if authorname else hc_id)
            hc_author = hc.get_author_info(authorid=hc_id)
            if not authorname and 'authorname' in hc_author:
                authorname = hc_author['authorname']

# make this compare to GOODREADS
    if CONFIG['GR_API'] and (CONFIG['BOOK_API'] not in ['OpenLibrary', 'HardCover'] or
                             CONFIG.get_bool('MULTI_SOURCE')):
        # if not CONFIG['HC_API'] and not gr_id and authorid.isnumeric():  # could be gr or hc, won't be both!
        #     gr_id = authorid
        if not gr_id and authorname and 'unknown' not in authorname.lower() and 'anonymous' not in authorname.lower():
            gr = GoodReads(authorname)
            gr_author = gr.find_author_id()
            if gr_author:
                gr_id = gr_author['authorid']
        if gr_id:
            gr = GoodReads(gr_id)
            gr_author = gr.get_author_info(authorid=gr_id)
            # uncomment the next 2 lines if any additional sources added later
            # if not authorname:
            #    authorname = gr_author['authorname']
    # ---------- Audible ----------
    if CONFIG.get_bool('MULTI_SOURCE'):
        asin = None
        if not au_id and authorname and 'unknown' not in authorname.lower() and 'anonymous' not in authorname.lower():
            au = Audible(authorname)
            au_author = au.find_author_id()
            if au_author:
                au_id = au_author['authorid']
                asin = au_author['asin']
        if au_id:
            au = Audible(au_id)
            au_author = au.get_author_info(authorid=au_id, asin=asin)

    # which source do we prefer
    merge(author, ol_author, 'ol_id', 'OpenLibrary')
    merge(author, gr_author, 'gr_id', 'GoodReads')
    merge(author, hc_author, 'hc_id', 'HardCover')
    merge(author, au_author, 'au_id', 'Audible')

    if author:
        if authorid:
            author['authorid'] = authorid
        if not author.get('authorid'):
            if CONFIG['BOOK_API'] == 'HardCover' and hc_author.get('authorid'):
                author['authorid'] = hc_author['authorid']
            elif CONFIG['BOOK_API'] == 'OpenLibrary' and ol_author.get('authorid'):
                author['authorid'] = ol_author['authorid']
            elif CONFIG['BOOK_API'] == 'GoodReads' and gr_author.get('authorid'):
                author['authorid'] = gr_author['authorid']
            elif CONFIG['BOOK_API'] == 'Audible' and au_author.get('authorasin'):
                author['asin'] = au_author['authorasin']

        # Handle AKAs
        akas = get_list(author.get('AKA', ''), ',')
        for src_name, src_val in [('GoodReads', gr_name), ('OpenLibrary', ol_name), ('HardCover', hc_name), ('Audible', au_name)]:
            if src_val:
                src_val = src_val.replace(',', '')
                if author['authorname'] != src_val and src_val not in akas:
                    logger.warning(
                        f"Conflicting {src_name} authorname for {author['authorid']} "
                        f"[{author['authorname']}] vs [{src_val}] -> setting AKA"
                    )
                akas.append(src_val)
        author['AKA'] = ', '.join(akas)

    db.close()
    return author


def add_author_to_db(authorname=None, refresh=False, authorid='', addbooks=True, reason=None):
    """
    Add an author to the database by name or id, and optionally get a list of all their books
    If author already exists in database, refresh their details and optionally booklist
    Returns the author ID
    """
    logger = logging.getLogger(__name__)
    if not reason:
        program, method, lineno = get_info_on_caller(depth=1)
        if lineno > 0:
            reason = f"{program}:{method}:{lineno}"
        else:
            reason = "Unknown reason in add_author_to_db"

    threadname = thread_name()
    if "Thread" in threadname:
        thread_name("AddAuthorToDB")
    db = database.DBConnection()
    ret_id = None
    asin = None

    try:
        new_author = True
        if authorid:
            cmd = "SELECT * from authors WHERE AuthorID=? or ol_id=? or gr_id=? or hc_id=? or au_id=?"
            dbauthor = db.match(cmd, (authorid, authorid, authorid, authorid, authorid))
        else:
            dbauthor = []

        if dbauthor:
            new_author = False
            authorid = dbauthor['AuthorID']
            asin = dbauthor['asin']
            authorname = dbauthor['AuthorName']
        elif authorname and 'unknown' not in authorname.lower() and 'anonymous' not in authorname.lower():
            dbauthor = db.match("SELECT * from authors WHERE AuthorName=?", (authorname,))
            if dbauthor:
                new_author = False
                authorid = dbauthor['AuthorID']
            else:
                dbauthor = db.match("SELECT * from authors WHERE instr(AKA, ?) > 0", (authorname,))
                if dbauthor:
                    new_author = False
                    authorid = dbauthor['AuthorID']
                    asin = dbauthor['asin']
                    authorname = dbauthor['AuthorName']

        if new_author or refresh:
            current_author = get_all_author_details(authorid, authorname)
            current_author['authorid'] = authorid
        else:
            current_author = {}
            for item in dict(dbauthor):
                current_author[item.lower()] = dbauthor[item]

        if new_author and not authorname and current_author['authorname']:
            dbauthor = db.match("SELECT * from authors WHERE AuthorName=? COLLATE NOCASE",
                                (current_author['authorname'],))
            if dbauthor:
                new_author = False
                current_author['authorid'] = dbauthor['AuthorID']
                current_author['authorname'] = dbauthor['AuthorName']
            else:
                dbauthor = db.match("SELECT * from authors WHERE instr(AKA, ?) > 0",
                                    (current_author['authorname'],))
                if dbauthor:
                    new_author = False
                    current_author['authorid'] = dbauthor['AuthorID']
                    current_author['authorname'] = dbauthor['AuthorName']

        current_author['manual'] = False
        if new_author:
            current_author['status'] = CONFIG['NEWAUTHOR_STATUS']
        else:
            if dbauthor['manual'] in [True, 'True', 1, '1']:
                current_author['manual'] = True
            current_author['status'] = dbauthor['status']

        if not current_author or not current_author.get('authorid'):
            logger.warning(f"No author info found for {authorid}:{authorname}:{reason}")
            if authorid:
                db.action("UPDATE authors SET Updated=? WHERE AuthorID=?", (int(time.time()), authorid))
            return ret_id, asin

        if authorname and current_author['authorname'] != authorname:
            dbauthor = db.match("SELECT * from authors WHERE AuthorName=?", (current_author['authorname'],))
            if dbauthor:
                logger.warning(
                    f"Authorname {current_author['authorname']} already exists with id {dbauthor['authorID']}")
                aka = authorname.replace(',', '')
                akas = get_list(dbauthor['AKA'], ',')
                if aka and aka not in akas:
                    akas.append(aka)
                    db.action("UPDATE authors SET AKA=? WHERE AuthorID=?", (', '.join(akas), dbauthor['authorid']))
                return dbauthor['authorid'], dbauthor['asin']
            else:
                logger.warning(
                    f"Updating authorname for {current_author['authorid']} (new:{current_author['authorname']} "
                    f"old:{authorname})")
                db.action('UPDATE authors SET AuthorName=? WHERE AuthorID=?',
                          (current_author['authorname'], current_author['authorid']))

        control_value_dict = {"AuthorID": current_author['authorid']}
        if not current_author['manual']:
            new_value_dict = current_author
            new_value_dict.pop('authorid')
            try:
                db.upsert("authors", new_value_dict, control_value_dict)
            except sqlite3.IntegrityError as err:
                logger.error(str(err))
                logger.error(str(new_value_dict))
                logger.error(str(control_value_dict))
                logger.error(f"{authorname}, {new_author}")
                logger.error(traceback.format_exc())
                control_value_dict = {"AuthorName": current_author['authorname']}
                new_value_dict = current_author
                new_value_dict.pop('authorname')
                try:
                    db.upsert("authors", new_value_dict, control_value_dict)
                    logger.debug(f"Retry {current_author['authorid']} using authorname "
                                 f"{current_author['authorname']} succeeded")
                except sqlite3.IntegrityError as err:
                    logger.error(str(err))
                    logger.error(traceback.format_exc())

        entry_status = current_author['status']
        new_value_dict = {
            "Status": "Loading",
            "Updated": int(time.time())
        }
        if not current_author.get('authorid'):
            current_author['authorid'] = authorid
        if new_author:
            new_value_dict["AuthorImg"] = "images/nophoto.png"
            new_value_dict['Reason'] = reason
            new_value_dict['DateAdded'] = today()
            refresh = True
            logger.debug(
                f"Adding new author id {current_author['authorid']} ({current_author['authorname']}) to database "
                f"{reason}, Addbooks={addbooks}")
        else:
            logger.debug(
                f"Updating author {current_author['authorid']} ({current_author['authorname']}) {entry_status}, "
                f"Addbooks={addbooks}, Manual={current_author['manual']}")
        db.upsert("authors", new_value_dict, control_value_dict)

        new_img = False
        authorimg = current_author.get('authorimg')
        if new_author or authorimg and 'nophoto' in authorimg:
            newimg = get_author_image(current_author['authorid'])
            if newimg:
                authorimg = newimg
                new_img = True

        if authorimg and authorimg.startswith('http'):
            newimg, success, _ = cache_img(ImageType.AUTHOR, img_id(), authorimg, refresh=refresh)
            if success:
                authorimg = newimg
                new_img = True
            else:
                logger.debug(f'Failed to cache image for {authorimg} ({newimg})')

        if new_img:
            db.action("UPDATE authors SET AuthorIMG=? WHERE AuthorID=?", (authorimg, current_author['authorid']))

        if not current_author['manual'] and addbooks:
            if new_author:
                bookstatus = CONFIG['NEWAUTHOR_STATUS']
                audiostatus = CONFIG['NEWAUTHOR_AUDIO']
            else:
                bookstatus = CONFIG['NEWBOOK_STATUS']
                audiostatus = CONFIG['NEWAUDIO_STATUS']

            if entry_status not in ['Active', 'Wanted', 'Ignored', 'Paused']:
                entry_status = 'Active'
            if entry_status not in ['Ignored', 'Paused']:
                api_sources = [
                    ['OL', OpenLibrary(current_author['authorname']), 'ol_id', 'OL_API'],
                    ['GR', GoodReads(current_author['authorname']), 'gr_id', 'GR_API'],
                    ['HC', HardCover(current_author['authorname']), 'hc_id', 'HC_API'],
                    ['AU', Audible(current_author['authorname']), 'au_id', 'AU_API'],
                    ['GB', GoogleBooks(current_author['authorname']), 'authorid', 'GB_API'],
                ]

                if CONFIG['BOOK_API'] == "GoodReads":
                    api_sources.insert(0, api_sources.pop(1))
                elif CONFIG['BOOK_API'] == "HardCover":
                    api_sources.insert(0, api_sources.pop(2))
                elif CONFIG['BOOK_API'] == "GoogleBooks":
                    api_sources.insert(0, api_sources.pop(4))
                elif CONFIG['BOOK_API'] == "Audible":
                    api_sources.insert(0, api_sources.pop(3))
                if not CONFIG.get_bool('MULTI_SOURCE'):
                    api_sources = [api_sources[0]]

                for api_source in api_sources:
                    if not CONFIG[api_source[3]]:
                        logger.debug(f"{api_source[3]} is disabled")
                    else:
                        current_id = current_author.get(api_source[2], '')
                        if not current_id:
                            if api_source[0] != 'GB':  # GB doesn't have authorid
                                logger.debug(f"Finding {api_source[0]} author ID for {current_author['authorname']}")
                                book_api = api_source[1]
                                res = book_api.find_author_id(refresh=True)
                                if res and res.get('authorid'):
                                    current_id = res.get('authorid')
                                    cmd = f"UPDATE authors SET {api_source[2]}=? WHERE AuthorName=?"
                                    db.action(cmd, (current_id, current_author['authorname']))
                        if current_id:
                            logger.debug(f"Book query {api_source[0]} for {current_id}:{current_author['authorname']}")
                            book_api = api_source[1]
                            book_api.get_author_books(current_id, current_author['authorname'],
                                                      bookstatus=bookstatus,
                                                      audiostatus=audiostatus, entrystatus=entry_status,
                                                      refresh=refresh, reason=reason)
                de_duplicate(current_author['authorid'])
                update_totals(current_author['authorid'])

        if current_author:
            db.action("UPDATE authors SET Status=? WHERE AuthorID=?", (entry_status,
                                                                       current_author['authorid']))
            msg = (f"{current_author['authorid']} [{current_author['authorname']}] Author update complete, "
                   f"status {entry_status}")
            logger.info(msg)
            ret_id = current_author['authorid']
            asin = current_author['asin']
        else:
            logger.warning(f"Authorid {authorid} ({authorname}) not found in database")
        return ret_id, asin

    except Exception:
        msg = f'Unhandled exception: {traceback.format_exc()}'
        logger.debug(msg)
        return None
    finally:
        db.close()

# translations: e.g. allow "fire & fury" to match "fire and fury"
# or "the lord of the rings" to match "lord of the rings"
title_translates = [
    [' & ', ' and '],
    [' + ', ' plus '],
    ['the ', ''],
    [', the', '']
]


def collate_nopunctuation(string1, string2):
    string1 = string1.lower()
    string2 = string2.lower()
    for entry in title_translates:
        string1 = string1.replace(entry[0], entry[1])
        string2 = string2.replace(entry[0], entry[1])
    # strip all punctuation so things like "it's" matches "its"
    str1 = string1.translate(str.maketrans('', '', string.punctuation))
    str2 = string2.translate(str.maketrans('', '', string.punctuation))
    if str1 < str2:
        return -1
    elif str1 > str2:
        return 1
    return 0


def collate_fuzzy(string1, string2):
    string1 = string1.lower()
    string2 = string2.lower()
    for entry in title_translates:
        string1 = string1.replace(entry[0], entry[1])
        string2 = string2.replace(entry[0], entry[1])
    # strip all punctuation so things like "it's" matches "its"
    str1 = string1.translate(str.maketrans('', '', string.punctuation))
    str2 = string2.translate(str.maketrans('', '', string.punctuation))
    if str1 == str2:
        return 0
    match_fuzz = fuzz.ratio(str1, str2)
    if match_fuzz >= CONFIG.get_int('NAME_RATIO'):
        return 0
    if str1 < str2:
        return -1
    elif str1 > str2:
        return 1
    return 0


def de_duplicate(authorid):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    author = db.match("SELECT AuthorName from authors where AuthorID=?", (authorid,))
    db.connection.create_collation('fuzzy', collate_fuzzy)
    total = 0
    authorname = ''
    if author:
        authorname = author['AuthorName']
    # noinspection PyBroadException
    try:
        # check/delete any duplicate titles - with fuzz
        cmd = ("select count('bookname'),bookname from books where authorid=? "
               "group by bookname COLLATE FUZZY having ( count(bookname) > 1 )")
        res = db.select(cmd, (authorid,))
        dupes = len(res)
        if not dupes:
            logger.debug("No duplicates to merge")
        else:
            logger.warning(f"There {plural(dupes, 'is')} {dupes} duplicate {plural(dupes, 'title')} "
                           f"for {authorid}:{authorname}")
            for item in res:
                logger.debug(f"{item[1]} has {item[0]} entries")
                favourite = ''
                copies = db.select("SELECT * from books where AuthorID=? and BookName=? COLLATE FUZZY",
                                   (authorid, item[1]))
                for copy in copies:
                    if (copy['Status'] in ['Open', 'Have'] or
                            copy['AudioStatus'] in ['Open', 'Have']):
                        favourite = copy
                        break
                if not favourite:
                    for copy in copies:
                        if (copy['Status'] in ['Wanted'] or
                                copy['AudioStatus'] in ['Wanted']):
                            favourite = copy
                            break
                if not favourite:
                    for copy in copies:
                        if copy['Status'] not in ['Ignored'] and copy['AudioStatus'] not in ['Ignored']:
                            favourite = copy
                            break
                if not favourite and copies:
                    favourite = copies[0]
                if favourite:
                    logger.debug(f"Favourite {favourite['BookID']} {favourite['BookName']} "
                                 f"({favourite['Status']}/{favourite['AudioStatus']})")
                for copy in copies:
                    if copy['BookID'] != favourite['BookID']:
                        members = db.select("SELECT SeriesID,SeriesNum from member WHERE BookID=?",
                                            (copy['BookID'],))
                        if members:
                            for member in members:
                                logger.debug(f"Updating BookID for member {member['SeriesNum']} of series "
                                             f"{member['SeriesID']}")
                                db.action("UPDATE member SET BookID=? WHERE BookID=? and SeriesID=?",
                                          (favourite['BookID'], copy['BookID'], member['SeriesID']), suppress='UNIQUE')
                        for key in ['BookSub', 'BookDesc', 'BookGenre', 'BookIsbn', 'BookPub', 'BookRate',
                                    'BookImg', 'BookPages', 'BookLink', 'BookFile', 'BookDate', 'BookLang',
                                    'BookAdded', 'WorkPage', 'Manual', 'SeriesDisplay', 'BookLibrary',
                                    'AudioFile', 'AudioLibrary', 'WorkID', 'ScanResult', 'gr_id', 'ol_id', 'gb_id',
                                    'hc_id', 'OriginalPubDate', 'Requester', 'AudioRequester', 'LT_WorkID', 'Narrator']:
                            if not favourite[key] and copy[key]:
                                cmd = f"UPDATE books SET {key}=? WHERE BookID=?"
                                logger.debug(f"Copy {key} from {copy['BookID']}: {copy['BookName']}")
                                db.action(cmd, (copy[key], favourite['BookID']))
                                if copy['Status'] not in ['Ignored'] and copy['AudioStatus'] not in ['Ignored']:
                                    if key == 'BookFile' and favourite['Status'] not in ['Open', 'Have']:
                                        logger.debug(f"Copy Status from {copy['BookID']}")
                                        db.action('UPDATE books SET Status=? WHERE BookID=?',
                                                  (copy['Status'], favourite['BookID']))
                                    if key == 'AudioFile' and favourite['AudioStatus'] not in ['Open', 'Have']:
                                        logger.debug(f"Copy AudioStatus from {copy['BookID']}")
                                        db.action('UPDATE books SET AudioStatus=? WHERE BookID=?',
                                                  (copy['AudioStatus'], favourite['BookID']))

                        if copy['Status'] in ['Ignored'] or copy['AudioStatus'] in ['Ignored']:
                            logger.debug(f"Keeping duplicate {copy['BookID']},  {copy['Status']}/{copy['AudioStatus']}")
                        else:
                            logger.debug(f"Delete {copy['BookID']} keeping {favourite['BookID']}")
                            db.action('DELETE from books WHERE BookID=?', (copy['BookID'],))
                            db.action("UPDATE readinglists SET Bookid=? WHERE BookID=?",
                                      (favourite['BookID'], copy['BookID']))
                            total += 1
    except Exception:
        msg = f'Unhandled exception in de_duplicate: {traceback.format_exc()}'
        logger.warning(msg)
    finally:
        db.close()
    logger.info(f"Deleted {total} duplicate {plural(total, 'entry')} for {authorname}")


def update_totals(authorid):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        # author totals needs to be updated every time a book is marked differently
        match = db.select('SELECT AuthorID from authors WHERE AuthorID=?', (authorid,))
        if not match:
            logger.debug(f'Update_totals - authorid [{authorid}] not found')
            return

        cmd = ("SELECT BookName, BookLink, BookDate, books.BookID from books,bookauthors WHERE "
               "books.bookid=bookauthors.bookid and bookauthors.AuthorID=? and Status != 'Ignored' "
               "order by BookDate DESC")
        lastbook = db.match(cmd, (authorid,))

        cmd = ("select sum(case status when 'Ignored' then 0 else 1 end) as unignored,sum(case when status == 'Have' "
               "then 1 when status == 'Open' then 1 else 0 end) as EHave, sum(case when audiostatus == 'Have' "
               "then 1 when audiostatus == 'Open' then 1 else 0 end) as AHave, sum(case when status == 'Have' "
               "then 1 when status == 'Open' then 1 when audiostatus == 'Have' then 1 when audiostatus == 'Open' "
               "then 1 else 0 end) as Have, count(*) as total from books,bookauthors where "
               "books.bookid=bookauthors.bookid and bookauthors.authorid=?")
        totals = db.match(cmd, (authorid,))

        control_value_dict = {"AuthorID": authorid}
        new_value_dict = {
            "TotalBooks": check_int(totals['total'], 0),
            "UnignoredBooks": check_int(totals['unignored'], 0),
            "HaveBooks": check_int(totals['Have'], 0),
            "HaveEBooks": check_int(totals['EHave'], 0),
            "HaveAudioBooks": check_int(totals['AHave'], 0),
            "LastBook": lastbook['BookName'] if lastbook else None,
            "LastLink": lastbook['BookLink'] if lastbook else None,
            "LastBookID": lastbook['BookID'] if lastbook else None,
            "LastDate": lastbook['BookDate'] if lastbook else None
        }
        db.upsert("authors", new_value_dict, control_value_dict)

        cmd = ("select series.seriesid as Series,sum(case books.status when 'Ignored' then 0 else 1 end) "
               "as Total,sum(case when books.status == 'Have' then 1 when books.status == 'Open' then 1 "
               "when books.audiostatus == 'Have' then 1 when books.audiostatus == 'Open' then 1 else 0 end) "
               "as Have from books,member,series,seriesauthors where member.bookid=books.bookid and "
               "member.seriesid = series.seriesid and seriesauthors.seriesid = series.seriesid and "
               "seriesauthors.authorid=? group by series.seriesid")
        res = db.select(cmd, (authorid,))
        if len(res):
            for series in res:
                db.action('UPDATE series SET Have=?, Total=? WHERE SeriesID=?',
                          (check_int(series['Have'], 0), check_int(series['Total'], 0), series['Series']))

        res = db.match('SELECT AuthorName from authors WHERE AuthorID=?', (authorid,))
    finally:
        db.close()
    logger.debug(
        f"Updated totals for [{res['AuthorName']}] {new_value_dict['HaveBooks']}/{new_value_dict['TotalBooks']}")


def import_book(bookid, ebook=None, audio=None, wait=False, reason='importer.import_book', source=None):
    """ search goodreads or googlebooks for a bookid and import the book
        ebook/audio=None makes find_book use configured default """
    logger = logging.getLogger(__name__)
    if not source:
        source = CONFIG['BOOK_API']
    if source in ["GB", "GoogleBooks"]:
        gb = GoogleBooks(bookid)
        if not wait:
            threading.Thread(target=gb.find_book, name='GB-IMPORT', args=[bookid, ebook, audio, reason]).start()
        else:
            gb.find_book(bookid, ebook, audio, reason)
    elif source in ["OL", "OpenLibrary"]:
        ol = OpenLibrary(bookid)
        logger.debug(f"bookstatus={ebook}, audiostatus={audio}")
        if not wait:
            threading.Thread(target=ol.find_book, name='OL-IMPORT', args=[bookid, ebook, audio, reason]).start()
        else:
            ol.find_book(bookid, ebook, audio, reason)
    elif source in ["HC", "HardCover"]:
        hc = HardCover(bookid)
        logger.debug(f"bookstatus={ebook}, audiostatus={audio}")
        if not wait:
            threading.Thread(target=hc.find_book, name='HC-IMPORT', args=[bookid, ebook, audio, reason]).start()
        else:
            hc.find_book(bookid, ebook, audio, reason)
    elif source in ["AU", "Audible"]:
        au = Audible(bookid)
        logger.debug(f"bookstatus={ebook}, audiostatus={audio}")
        if not wait:
            threading.Thread(target=au.find_book, name='AU-IMPORT', args=[bookid, ebook, audio, reason]).start()
        else:
            au.find_book(bookid, ebook, audio, reason)
    else:
        gr = GoodReads(bookid)
        if not wait:
            threading.Thread(target=gr.find_book, name='GR-IMPORT', args=[bookid, ebook, audio, reason]).start()
        else:
            gr.find_book(bookid, ebook, audio, reason)


def search_for(searchterm, source=None):
    """
    Search OpenLibrary, GoodReads, GoogleBooks, HardCover, or Audible for a searchterm.
    Returns a list of results.
    """
    loggersearching = logging.getLogger('special.searching')
    if not source:
        source = CONFIG['BOOK_API']
    loggersearching.debug(f"{source} {searchterm}")

    myqueue = Queue()
    search_api = None

    if source in ["GoogleBooks", "GB"] and CONFIG['GB_API']:
        gb = GoogleBooks(searchterm)
        search_api = threading.Thread(target=gb.find_results, name='GB-RESULTS', args=[searchterm, myqueue])
    elif source in ["GoodReads", "GR"] and CONFIG['GR_API']:
        gr = GoodReads(searchterm)
        search_api = threading.Thread(target=gr.find_results, name='GR-RESULTS', args=[searchterm, myqueue])
    elif source in ["OpenLibrary", "OL"] and CONFIG['OL_API']:
        ol = OpenLibrary(searchterm)
        search_api = threading.Thread(target=ol.find_results, name='OL-RESULTS', args=[searchterm, myqueue])
    elif source in ["HardCover", "HC"] and CONFIG['HC_API']:
        hc = HardCover(searchterm)
        search_api = threading.Thread(target=hc.find_results, name='HC-RESULTS', args=[searchterm, myqueue])
    elif source in ["Audible", "AU"]:
        au = Audible(searchterm)
        search_api = threading.Thread(target=au.find_results, name='AU-RESULTS', args=[searchterm, myqueue])
    else:
        search_api = None
        myqueue = None

    if search_api:
        search_api.start()
        search_api.join()
        return myqueue.get()

    return []

