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

import contextlib
import logging
import re
import sqlite3
import string
import threading
import time
import traceback
from hashlib import sha1
from queue import Queue
from urllib.parse import unquote_plus

from rapidfuzz import fuzz

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.cache import ImageType, cache_img
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import (
    check_int,
    format_author_name,
    get_list,
    make_bytestr,
    plural,
    thread_name,
    today,
    unaccented,
)
from lazylibrarian.grsync import grfollow
from lazylibrarian.images import get_author_image, img_id
from lazylibrarian.processcontrol import get_info_on_caller


def is_valid_authorid(authorid: str, api=None) -> bool:
    if not authorid or not isinstance(authorid, str):
        return False  # Reject blank, or non-string
    if api is None:
        api = CONFIG['BOOK_API']
    # Not all providers have authorid, so we use one of the other sources
    has_authorkey = []
    for item in lazylibrarian.INFOSOURCES.keys():
        this_source = lazylibrarian.INFOSOURCES[item]
        if this_source['author_key'] and this_source['author_key'] != 'authorid':
            has_authorkey.append(item)

    if authorid.startswith('OL') and (api == 'OpenLibrary' or api not in has_authorkey):
        return True
    return bool(authorid.isdigit() and api != 'OpenLibrary')


def get_preferred_author(author, authorid=''):
    # Look up an author in the database, if not found try fuzzy match
    # Return possibly changed authorname and authorid if found in library
    logger = logging.getLogger(__name__)
    author = format_author_name(author, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
    db = database.DBConnection()
    check_exist_author = None
    if authorid:
        keys = author_keys()
        cmd = "SELECT * from authors WHERE AuthorID=?"
        for k in keys:
            cmd += f" or {k}=?"
        check_exist_author = db.match(cmd, tuple([str(authorid)] * (len(keys) + 1)))
    if not check_exist_author:
        check_exist_author = db.match('SELECT * FROM authors where AuthorName=?', (author,))
    if check_exist_author:
        db.close()
        return check_exist_author['AuthorName'], check_exist_author['AuthorID']

    # If no exact match, look for a close fuzzy match to handle misspellings, accents or AKA
    match_name = author.lower().replace('.', '')
    res = db.action('select AuthorID,AuthorName,AKA from authors')
    for item in res:
        aname = item['AuthorName']
        if aname:
            match_fuzz = fuzz.ratio(aname.lower().replace('.', ''), match_name)
            if match_fuzz >= CONFIG.get_int('NAME_RATIO'):
                logger.debug(f"Fuzzy match [{item['AuthorName']}] {round(match_fuzz, 2)}% for [{author}]")
                author = item['AuthorName']
                authorid = item['AuthorID']
                break
        akas = get_list(item['AKA'], ',')
        if akas:
            for aka in akas:
                match_fuzz = fuzz.token_set_ratio(aka.lower().replace('.', '').replace(',', ''), match_name)
                if match_fuzz >= CONFIG.get_int('NAME_RATIO'):
                    logger.debug(f"Fuzzy AKA match [{aka}] {round(match_fuzz, 2)}% for [{author}]")
                    author = item['AuthorName']
                    authorid = item['AuthorID']
                    break
    db.close()
    return author, authorid


def available_author_sources():
    author_sources = []
    source_dict = {}
    pref = ''
    # only return enabled and not-blocked sources
    for item in lazylibrarian.INFOSOURCES.keys():
        # fullname, 2-letter_code, class, author_key, api_enabled
        this_source = lazylibrarian.INFOSOURCES[item]
        if CONFIG[this_source['enabled']] and not BLOCKHANDLER.is_blocked(item):
            source_dict[item] = [this_source['src'], this_source['api'],
                                 this_source['author_key'], this_source['enabled']]
    # GB/DNB don't have authorid so we use one of the others...
    # prefer CONFIG['BOOK_API'] if it has authorid
    # 2nd choice, one that's enabled with an apikey
    # 3rd choice, openlibrary if enabled (doesn't need apikey)
    if source_dict[CONFIG['BOOK_API']][3] and source_dict[CONFIG['BOOK_API']][2] != 'authorid':
        pref = CONFIG['BOOK_API']
    else:
        for item in source_dict:
            if (source_dict[CONFIG['BOOK_API']][3] and
                source_dict[CONFIG['BOOK_API']][2] != 'authorid' and
                    source_dict[CONFIG['BOOK_API']][0] != 'OL'):
                pref = item
                break
        if not pref and source_dict['OpenLibrary'][3]:
            pref = 'OpenLibrary'
    if not pref:
        logger = logging.getLogger(__name__)
        logger.warning("No suitable source for authorid, using OpenLibrary")
        pref = 'OpenLibrary'

    author_sources.append(source_dict[pref])
    if CONFIG.get_bool('MULTI_SOURCE'):
        for item in source_dict:
            if item != pref and source_dict[item][1] and source_dict[item][2] and source_dict[item][2] != 'authorid':
                author_sources.append(source_dict[item])
    return author_sources


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
        return "", "", new

    unquoted_author = unquote_plus(author)
    for token in ['<', '>', '=', '"']:
        if token in unquoted_author:
            logger.warning(f'Cannot set authorname, contains "{token}"')
            return "", "", new

    db = database.DBConnection()
    try:
        # Check if the author exists, and import the author if not,
        req_author = author
        author, exists = get_preferred_author(req_author)
        if exists:
            check_exist_author = db.match('SELECT * FROM authors where AuthorName=?', (author,))
        else:
            check_exist_author = None
        if not exists and (CONFIG.get_bool('ADD_AUTHOR') or reason.startswith('API')):
            logger.debug(f'Author {author} not found in database, adding...')
            # no match for supplied author, but we're allowed to add new ones
            api_sources = available_author_sources()
            match_fuzz = 0
            for api_source in api_sources:
                logger.debug(f"Finding {api_source[0]} author ID for {author}")
                book_api = api_source[1]
                book_api = book_api()
                author_info = book_api.find_author_id(authorname=author, title=title, refresh=True)
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
                    logger.debug(
                        f"Failed to match author [{author}] to authorname [{match_name}] fuzz [{match_fuzz}]")

            if not api_sources:
                # no available non-blocked author sources
                match_fuzz = 0
            elif not author_info:
                # not found at any enabled provider. Fake it, generate our own ID...
                author_info['authorname'] = author
                author_info['authorid'] = f'LL{sha1(make_bytestr(author)).hexdigest()}'
                logger.debug(f"Generated ID {author_info['authorid']} for {author_info['authorname']}")
                match_fuzz = 100

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
            return "", "", new
        return check_exist_author['AuthorName'], check_exist_author['AuthorID'], new
    finally:
        db.close()


def author_keys():
    keys = []
    for item in lazylibrarian.INFOSOURCES.keys():
        this_source = lazylibrarian.INFOSOURCES[item]
        if this_source['author_key'] and this_source['author_key'] != 'authorid':
            keys.append(this_source['author_key'])
    return keys


def book_keys():
    keys = []
    for item in lazylibrarian.INFOSOURCES.keys():
        this_source = lazylibrarian.INFOSOURCES[item]
        if this_source['book_key'] and this_source['book_key'] != 'bookid':
            keys.append(this_source['book_key'])
    return keys


def get_all_author_details(authorid='', authorname=None):
    # fetch as much data as you can on an author using all configured sources

    logger = logging.getLogger(__name__)
    searchinglogger = logging.getLogger('special.searching')
    sources = available_author_sources()
    searchinglogger.debug(f"{authorid}:{authorname}:{sources}")
    keys = author_keys()
    author_info = {}
    pref = ''
    match = {}
    db = database.DBConnection()
    if authorid:
        cmd = f"SELECT {','.join(keys)},authorid,authorname from authors WHERE authorid=?"
        for k in keys:
            cmd += f" or {k}=?"
        match = db.match(cmd, tuple([str(authorid)] * (len(keys) + 1)))
    if not match and authorname:
        a_name, a_id = get_preferred_author(authorname, authorid=authorid)
        if a_id:
            cmd = f"SELECT {','.join(keys)},authorid,authorname from authors WHERE authorname=? COLLATE NOCASE"
            name_match = db.match(cmd, (a_name,))
            if name_match:
                for k in keys:
                    if name_match[k] == authorid:
                        match = name_match
                        break
    if match:
        authorname = match['authorname']
        authorid = match['authorid']

    merged_info = {}
    for src in sources:
        cl = src[1]
        cl = cl()
        auth_id = ''
        if match:
            auth_id = match[src[2]]  # authorid for this source, eg hc_id
        elif authorid and CONFIG['BOOK_API'] in str(cl):
            # no match in db but we already have an authorid for default api
            auth_id = authorid
        if not auth_id and authorname and 'unknown' not in authorname and 'anonymous' not in authorname:
            book = db.match('SELECT bookname from books WHERE authorid=?', (authorid,))
            title = ''
            if book:
                title = book['bookname']
            aid = cl.find_author_id(authorname=authorname, title=title)
            if aid:
                db.action(f"UPDATE authors SET {src[2]}=? WHERE authorid=?",
                          (aid['authorid'], authorid))
                auth_id = aid['authorid']
        if not auth_id and authorid:
            auth_id = authorid
        if auth_id:
            res = cl.get_author_info(authorid=auth_id, authorname=authorname)
            if res:
                author_info[src[0]] = res
                author_info[src[0]][src[2]] = auth_id
                if not merged_info:
                    pref = src[0]
                    merged_info = author_info[pref]
    akas = []
    if merged_info.get('AKA'):
        akas = get_list(merged_info.get('AKA', ''), ',')
    authorname = merged_info.get('authorname')
    searchinglogger.debug(str(author_info))
    for entry in author_info:
        if entry != pref:
            author_key = 'authorid'
            for item in sources:
                if item[0] == entry:
                    author_key = item[2]
                    break
            if author_info[entry].get('authorid'):
                merged_info[author_key] = author_info[entry]['authorid']
            auth_name = author_info[entry].get('authorname')
            if auth_name and auth_name != authorname and auth_name not in akas:
                logger.warning(
                    f"Conflicting {entry} authorname for {authorid} [{auth_name}]"
                    f" expecting [{authorname}] setting AKA")
                akas.append(auth_name)

            for item in author_info[entry]:
                if item == 'authorimg':
                    if not merged_info.get(item) or 'nophoto' in merged_info.get(item) and author_info[entry][item]:
                        merged_info[item] = author_info[entry][item]
                elif item not in merged_info or not merged_info.get(item):
                    merged_info[item] = author_info[entry][item]

    if akas:
        merged_info['AKA'] = ', '.join(akas)
    if authorid:
        merged_info['authorid'] = authorid  # keep original entry authorid if we have one
    db.close()
    searchinglogger.debug(str(merged_info))
    return merged_info


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

    if "AUTHOR" not in thread_name().upper():
        thread_name("ADDAUTHORTODB")
    db = database.DBConnection()
    ret_id = None
    current_author = {}
    # noinspection PyBroadException
    try:
        logger.debug(f"Storing start time for {thread_name()}")
        db.upsert("jobs", {"Start": time.time()}, {"Name": thread_name()})
        authorkeys = author_keys()
        new_author = True
        if authorid:
            cmd = "SELECT * from authors WHERE AuthorID=?"
            for k in authorkeys:
                cmd += f" or {k}=?"
            dbauthor = db.match(cmd, tuple([str(authorid)] * (len(authorkeys) + 1)))
        else:
            dbauthor = {}
        if dbauthor:
            new_author = False
            authorid = dbauthor['AuthorID']
            authorname = dbauthor['AuthorName']
        elif authorname and 'unknown' not in authorname and 'anonymous' not in authorname:
            dbauthor = db.match("SELECT * from authors WHERE AuthorName=? or instr(AKA, ?) > 0", (authorname, authorname))
            if dbauthor:  # same name or AKA but different id
                for k in authorkeys:
                    if dbauthor[k] == authorid:
                        new_author = False
                        authorid = dbauthor['AuthorID']
                        authorname = dbauthor['AuthorName']
                        break
        if new_author or refresh:
            current_author = get_all_author_details(authorid, authorname)
            if authorid:
                current_author['authorid'] = authorid  # keep entry authorid
            if authorname:
                current_author['authorname'] = authorname  # and entry authorname
        elif dbauthor:
            current_author = {}
            for item in dict(dbauthor):
                current_author[item.lower()] = dbauthor[item]

        if new_author and not authorid and current_author.get('authorname'):
            # maybe we only had authorid(s) to search for
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
            current_author['manual'] = bool(dbauthor['manual'] in [True, 'True', 1, '1'])
            current_author['status'] = dbauthor['status']

        if not current_author.get('authorid'):
            # goodreads sometimes changes authorid
            # maybe change of provider or no reply from provider
            logger.warning(f"No author info found for {authorid}:{authorname}:{reason}")
            if authorid:
                db.action("UPDATE authors SET Updated=? WHERE AuthorID=?", (int(time.time()), authorid))
            return ret_id

        if authorname and current_author.get('authorname') and current_author.get('authorname') != authorname:
            dbauthor = db.match("SELECT * from authors WHERE AuthorName=? COLLATE NOCASE",
                                (current_author['authorname'],))
            if dbauthor:
                logger.warning(
                    f"Authorname {current_author['authorname']} already exists with id {dbauthor['authorID']}")
                # current_author['authorid'] = dbauthor['authorid']
                aka = authorname.replace(',', '')
                akas = get_list(dbauthor['AKA'], ',')
                if aka and aka not in akas:
                    akas.append(aka)
                    db.action("UPDATE authors SET AKA=? WHERE AuthorID=?", (', '.join(akas), dbauthor['authorid']))
                current_author['authorid'] = dbauthor['authorid']
                current_author['AKA'] = ', '.join(akas)
            else:
                logger.warning(
                    f"Updating authorname for {current_author['authorid']} (new:{current_author['authorname']} "
                    f"old:{authorname})")
                db.action('UPDATE authors SET AuthorName=? WHERE AuthorID=?',
                          (current_author['authorname'], current_author['authorid']))

        if authorid and not current_author.get('authorid'):
            current_author['authorid'] = authorid
        if not current_author['authorid']:
            logger.debug(
                f"Unable to update author: AuthorID missing: Entry ID {authorid}, Entry name {authorname}, dbauthor {dict(dbauthor)}")
            return None
        if authorname and not current_author.get('authorname'):
            current_author['authorname'] = authorname
        if not current_author['authorname']:
            logger.debug(
                f"Unable to update author {current_author['authorid']}: Authorname missing: Entry name {authorname}, dbauthor {dict(dbauthor)}")
            return None

        control_value_dict = {"AuthorID": current_author['authorid']}
        if not current_author['manual']:
            new_value_dict = current_author.copy()
            new_value_dict.pop('authorid')
            try:
                # at this point we should be able to add as a new author
                # but the name may collide if an existing author has the same name
                # but different authorid, eg there are several "David Mitchell" in openlibrary
                if new_author:
                    new_name = current_author['authorname']
                    while True:
                        existing = db.match("SELECT * from authors WHERE AuthorName=? COLLATE NOCASE",
                                            (new_name,))
                        if existing and existing['AuthorID'] != current_author['authorid']:
                            words = new_name.split()
                            if len(words) > 1:  # add an extra dot to make the name different
                                new_name = words[0] + '. ' + ' '.join(words[1:])
                            else:
                                new_name = f"{new_name}."
                            logger.warning(f"Authorname {current_author['authorname']} already exists, Trying {new_name}")
                        else:
                            break
                    new_value_dict['authorname'] = new_name
                    current_author['authorname'] = new_name

                db.upsert("authors", new_value_dict, control_value_dict)
            except sqlite3.IntegrityError as err:
                # Had a report of authorname constraint failed here but currently can't see why. Need more info
                logger.error(str(err))
                logger.error(str(new_value_dict))
                logger.error(str(control_value_dict))
                logger.error(f"{authorname}, {new_author}")
                logger.error(traceback.format_exc())
                # retry using authorname instead of authorid
                control_value_dict = {"AuthorName": current_author['authorname']}
                new_value_dict = current_author.copy()
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
        if new_author:
            new_value_dict["AuthorImg"] = "images/nophoto.png"
            new_value_dict["AuthorName"] = current_author['authorname']
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

        # if author is set to manual, should we allow replacing 'nophoto' ?
        new_img = False
        authorimg = current_author.get('authorimg')
        if new_author or not authorimg or 'nophoto' in authorimg:
            newimg = get_author_image(current_author['authorid'])
            if newimg:
                authorimg = newimg
                new_img = True

        # allow caching new image
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
                entry_status = 'Active'  # default for invalid/unknown or "loading"
            if entry_status not in ['Ignored', 'Paused']:
                # process books
                authorname = current_author['authorname']
                api_sources = []
                for item in lazylibrarian.INFOSOURCES.keys():
                    this_source = lazylibrarian.INFOSOURCES[item]
                    api_sources.append([item, this_source['src'], this_source['api'],
                                        this_source['author_key'], this_source['enabled']])

                # get preferred source first but keep all other enabled ones in any order
                current_sources = []
                for api_source in api_sources:
                    if CONFIG[api_source[4]]:  # only include if source is enabled
                        if api_source[0] == CONFIG['BOOK_API']:
                            current_sources.insert(0, api_source)
                        else:
                            current_sources.append(api_source)
                if not CONFIG.get_bool('MULTI_SOURCE'):
                    current_sources = [current_sources[0]]
                for api_source in current_sources:
                    current_id = current_author.get(api_source[3], '')
                    if not current_id and api_source[3] and api_source[3] != 'authorid':
                        logger.debug(f"Finding {api_source[0]} author ID for {current_author['authorname']}")
                        book_api = api_source[2]
                        book_api = book_api()
                        res = book_api.find_author_id(authorname=authorname, title='', refresh=True)
                        if res and res.get('authorid'):
                            current_id = res.get('authorid')
                            cmd = f"UPDATE authors SET {api_source[3]}=? WHERE AuthorName=? COLLATE NOCASE"
                            db.action(cmd, (current_id, current_author['authorname']))
                    if current_id:
                        logger.debug(f"Book query {api_source[0]} for {current_id}:{current_author['authorname']}")
                        book_api = api_source[2]
                        book_api = book_api()
                        book_api.get_author_books(current_id, current_author['authorname'],
                                                  bookstatus=bookstatus,
                                                  audiostatus=audiostatus, entrystatus=entry_status,
                                                  refresh=refresh, reason=reason)
                de_duplicate(current_author['authorid'])
                update_totals(current_author['authorid'])

            if lazylibrarian.STOPTHREADS and thread_name() == "AUTHORUPDATE":
                logger.debug(f"STOPTHREADS [{current_author['authorname']}] Author update aborted, status {entry_status}")
                return ret_id

            if new_author and CONFIG['GR_FOLLOWNEW']:
                res = grfollow(current_author['authorid'], True)
                if res.startswith('Unable'):
                    logger.warning(res)
                try:
                    followid = res.split("followid=")[1]
                    logger.debug(f"{current_author['authorname']} marked followed")
                except IndexError:
                    followid = ''
                db.action('UPDATE authors SET GRfollow=? WHERE AuthorID=?', (followid, current_author['authorid']))
        else:
            # if we're not loading any books, and it's a new author,
            # mark author as paused in case it's a wishlist or a series contributor
            if new_author and not addbooks:
                entry_status = 'Paused'

        if current_author:
            db.action("UPDATE authors SET Status=? WHERE AuthorID=?", (entry_status,
                                                                       current_author['authorid']))
            msg = (f"{current_author['authorid']} [{current_author['authorname']}] Author update complete, "
                   f"status {entry_status}")
            logger.info(msg)
            db.commit()
            ret_id = current_author['authorid']
        else:
            logger.warning(f"Authorid {authorid} ({authorname}) not found in database")
        return ret_id

    except Exception:
        msg = f'Unhandled exception: {traceback.format_exc()}'
        logger.debug(msg)
        return None
    finally:
        logger.debug(f"Storing finish time for {thread_name()}")
        db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})
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
    if str1 > str2:
        return 1
    return 0


def collate_fuzzy(string1, string2):
    fuzzlogger = logging.getLogger('special.fuzz')
    string1 = string1.lower()
    string2 = string2.lower()
    for entry in title_translates:
        string1 = string1.replace(entry[0], entry[1])
        string2 = string2.replace(entry[0], entry[1])
    # strip all punctuation so things like "it's" matches "its"
    str1 = string1.translate(str.maketrans('', '', string.punctuation))
    str2 = string2.translate(str.maketrans('', '', string.punctuation))
    if str1 == str2:
        fuzzlogger.debug(f"[{string1}][{string2}] match")
        return 0

    # make sure "The Lord of the Rings" matches "Lord of the Rings"
    set1 = set(str1.split())
    set2 = set(str2.split())
    for word in get_list(CONFIG.get_csv('NAME_DEFINITE')):
        set1.discard(word)
        set2.discard(word)
    if set1 == set2:
        fuzzlogger.debug(f"[{set1}][{set2}] match")
        return 0

    match_fuzz = fuzz.ratio(str1, str2)
    fuzzlogger.debug(f"[{string1}][{string2}]{match_fuzz}")
    if match_fuzz >= CONFIG.get_int('NAME_RATIO'):
        # if it's a close enough match, check for purely number differences
        num1 = []
        num2 = []
        for word in set1:
            # see if word coerces to an integer or a float
            word = word.replace('-', '')
            try:
                num1.append(float(re.findall(r'\d+\.\d+', word)[0]))
            except IndexError:
                with contextlib.suppress(IndexError):
                    num1.append(int(re.findall(r'\d+', word)[0]))
        for word in set2:
            word = word.replace('-', '')
            try:
                num2.append(float(re.findall(r'\d+\.\d+', word)[0]))
            except IndexError:
                with contextlib.suppress(IndexError):
                    num2.append(int(re.findall(r'\d+', word)[0]))
        fuzzlogger.debug(f"[{string1}][{string2}]{num1}:{num2}")
        if num1 == num2:
            return 0
        return 1
    if str1 < str2:
        return -1
    return 1


def de_duplicate(authorid):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    author = db.match("SELECT AuthorName from authors where AuthorID=?", (authorid,))
    db.connection.create_collation('fuzzy', collate_fuzzy)
    total = 0
    authorname = ''
    booktable_keys = ['BookSub', 'BookDesc', 'BookGenre', 'BookIsbn', 'BookPub', 'BookRate',
                      'BookImg', 'BookPages', 'BookLink', 'BookFile', 'BookDate', 'BookLang',
                      'BookAdded', 'WorkPage', 'Manual', 'SeriesDisplay', 'BookLibrary',
                      'AudioFile', 'AudioLibrary', 'WorkID', 'ScanResult', 'OriginalPubDate',
                      'Requester', 'AudioRequester', 'LT_WorkID', 'Narrator', 'BookID']
    booktable_keys.extend(book_keys())

    if author:
        authorname = author['AuthorName']
    # noinspection PyBroadException
    try:
        # check/delete any duplicate titles - with separate fuzz
        # we do a nocase first, as for some reason fuzzy doesn't get called if the names match
        for collation in ['NOCASE', 'FUZZY']:
            cmd = ("select count('bookname'),bookname from books where authorid=? "
                   f"group by bookname COLLATE {collation} having ( count(bookname) > 1 )")
            res = db.select(cmd, (authorid,))
            dupes = len(res)
            if not dupes:
                logger.debug(f"No {collation} duplicates to merge")
            else:
                logger.warning(f"There {plural(dupes, 'is')} {dupes} duplicate {collation} {plural(dupes, 'title')} "
                               f"for {authorid}:{authorname}")
                for item in res:
                    logger.debug(f"{item[1]} has {item[0]} entries")
                    favourite = {}
                    copies = db.select(f"SELECT * from books where AuthorID=? and BookName=? COLLATE {collation}",
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
                            logger.debug(f"Copy {copy['BookID']} {copy['BookName']} "
                                         f"({copy['Status']}/{copy['AudioStatus']})")
                    for copy in copies:
                        if copy['BookID'] != favourite['BookID']:
                            members = db.select("SELECT SeriesID,SeriesNum from member WHERE BookID=?",
                                                (copy['BookID'],))
                            if members:
                                for member in members:
                                    logger.debug(f"Updating BookID for member {member['SeriesNum']} of series "
                                                 f"{member['SeriesID']}")
                                    db.action("UPDATE member SET BookID=? WHERE BookID=? and SeriesID=?",
                                              (favourite['BookID'], copy['BookID'], member['SeriesID']),
                                              suppress='UNIQUE')
                            for key in booktable_keys:
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
                                logger.debug(f"Keeping duplicate {copy['BookID']},  {copy['Status']}/"
                                             f"{copy['AudioStatus']}")
                            else:
                                logger.debug(f"Delete {copy['BookID']} keeping {favourite['BookID']}")
                                db.action('DELETE from books WHERE BookID=?', (copy['BookID'],))
                                db.action("UPDATE readinglists SET Bookid=? WHERE BookID=?",
                                          (favourite['BookID'], copy['BookID']), suppress='UNIQUE')
                                total += 1
    except Exception:
        msg = f'Unhandled exception in de_duplicate: {traceback.format_exc()}'
        logger.warning(msg)
    finally:
        db.close()
    logger.info(f"Deleted {total} duplicate {plural(total, 'entry')} for {authorname}")


def update_totals(authorid):
    logger = logging.getLogger(__name__)
    if not authorid:
        logger.error("update_totals called with no authorid")
        program, method, lineno = get_info_on_caller(depth=1)
        logger.error(f"{program}:{method}:{lineno}")
        return
    db = database.DBConnection()
    try:
        # author totals needs to be updated every time a book is marked differently
        match = db.match('SELECT AuthorName from authors WHERE AuthorID=?', (authorid,))
        if not match:
            logger.debug(f'Update_totals - authorid [{authorid}] not found')
            return
        authorname = match['AuthorName']

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
        db.close()
        logger.debug(
            f"Updated totals for [{authorname}] {new_value_dict['HaveBooks']}/{new_value_dict['TotalBooks']}")
    except Exception as e:
        logger.error(str(e))
        db.close()


def import_book(bookid, ebook=None, audio=None, wait=False, reason='importer.import_book', source=None):
    """ search goodreads or googlebooks for a bookid and import the book
        ebook/audio=None makes add_bookid_to_db use configured default """
    logger = logging.getLogger(__name__)
    if not source:
        source = CONFIG['BOOK_API']
    else:
        # we may be passed a 2 letter code, eg GR, OL and need to get the source api from that
        # or may have full source eg GoodReads, OpenLibrary which we can look up in infosources
        for item in lazylibrarian.INFOSOURCES.keys():
            if lazylibrarian.INFOSOURCES[item]['src'] == source:
                source = item
                break

    if source not in lazylibrarian.INFOSOURCES.keys():
        logger.error(f"Invalid source {source} in import_book")
        return

    api = lazylibrarian.INFOSOURCES[source]['api']
    api = api()
    if not wait:
        threading.Thread(target=api.add_bookid_to_db, name=f"{lazylibrarian.INFOSOURCES[source]['src']}-IMPORT",
                         args=[bookid, ebook, audio, reason]).start()
    else:
        _ = api.add_bookid_to_db(bookid, ebook, audio, reason)


def search_for(searchterm, source=None):
    """
        search openlibrary/goodreads/googlebooks for a searchterm, return a list of results
    """
    searchinglogger = logging.getLogger('special.searching')
    if not source:
        source = CONFIG['BOOK_API']
    searchinglogger.debug(f"{source} {searchterm}")
    this_source = lazylibrarian.INFOSOURCES[source]
    api = this_source['api']
    api = api()  #api.__init__()
    if CONFIG[this_source['enabled']]:
        myqueue = Queue()
        search_api = threading.Thread(target=api.find_results,
                                      name=f"{this_source['src']}-RESULTS",
                                      args=[searchterm, myqueue])
        search_api.start()
        search_api.join()
        return myqueue.get()
    return []
