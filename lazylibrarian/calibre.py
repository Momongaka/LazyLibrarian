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
import string
import time
import traceback

import cherrypy
from rapidfuzz import fuzz

from lazylibrarian import database
from lazylibrarian.common import get_readinglist, run_script, set_readinglist
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import get_directory
from lazylibrarian.formatter import get_list, unaccented
from lazylibrarian.importer import add_author_name_to_db, import_book, search_for
from lazylibrarian.librarysync import find_book_in_db

# calibredb custom_columns
# calibredb add_custom_column label name bool
# calibredb remove_custom_column --force label
# calibredb set_custom label id value
# calibredb search "#label":"false"  # returns list of ids (slow)


def calibre_list(col_read, col_toread):
    """ Get a list from calibre of all books in its library, including optional 'read' and 'toread' columns
        If success, return list of dicts {"title": "", "id": 0, "authors": ""}
        The "read" and "toread" columns are passed as column names so they can be per-user and may not be present.
        Can be true, false, or empty in which case not included in dict. We only use the "true" state
        If error, return error message (not a dict) """

    fieldlist = 'title,authors'
    if col_read:
        fieldlist += f",*{col_read}"
    if col_toread:
        fieldlist += f",*{col_toread}"
    res, err, rc = calibredb("list", "", ['--for-machine', '--fields', fieldlist])
    if rc:
        if res:
            return res
        return err
    logger = logging.getLogger(__name__)
    logger.debug(f"{res}")
    return json.loads(res)


def sync_calibre_list(col_read=None, col_toread=None, userid=None):
    """ Get the lazylibrarian bookid for each read/toread calibre book so we can map our id to theirs,
        and sync current/supplied user's read/toread or supplied read/toread columns to calibre database.
        Return message giving totals """
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    msg = ''
    try:
        username = ''
        readlist = []
        toreadlist = []
        if not userid:
            cookie = cherrypy.request.cookie
            if cookie and 'll_uid' in list(cookie.keys()):
                userid = cookie['ll_uid'].value
        if userid:
            res = db.match('SELECT UserName,CalibreRead,CalibreToRead,Perms from users where UserID=?',
                           (userid,))
            if res:
                username = res['UserName']
                if not col_read:
                    col_read = res['CalibreRead']
                if not col_toread:
                    col_toread = res['CalibreToRead']
                toreadlist = get_readinglist("ToRead", userid)
                readlist = get_readinglist("HaveRead", userid)
            else:
                return f"Error: Unable to get user column settings for {userid}"

        if not userid:
            return "Error: Unable to find current userid"

        if not col_read and not col_toread:
            return f"User {username} has no calibre columns set"

        # check user columns exist in calibre and create if not
        res = calibredb('custom_columns')
        columns = res[0].split('\n')
        custom_columns = []
        for column in columns:
            if column:
                custom_columns.append(column.split(' (')[0])

        if col_read not in custom_columns:
            added = calibredb('add_custom_column', [col_read, col_read, 'bool'])
            if "column created" not in added[0]:
                return added
        if col_toread not in custom_columns:
            added = calibredb('add_custom_column', [col_toread, col_toread, 'bool'])
            if "column created" not in added[0]:
                return added

        nomatch = 0
        readcol = ''
        toreadcol = ''
        map_ctol = {}
        map_ltoc = {}
        if col_read:
            readcol = f"*{col_read}"
        if col_toread:
            toreadcol = f"*{col_toread}"

        calibrelist = calibre_list(col_read, col_toread)
        if not isinstance(calibrelist, list):
            # got an error message from calibredb
            return f'"{calibrelist}"'

        for item in calibrelist:
            if toreadcol and toreadcol in item or readcol and readcol in item:
                authorname, _, added = add_author_name_to_db(item['authors'], refresh=False, addbooks=False,
                                                             reason=f"sync_calibre_list: {item['title']}",
                                                             title=item['title'])
                if authorname:
                    if authorname != item['authors']:
                        logger.debug(
                            f"Changed authorname for [{item['title']}] from [{item['authors']}] to [{authorname}]")
                        item['authors'] = authorname
                    bookid, mtype = find_book_in_db(authorname, item['title'], ignored=False, library='eBook',
                                                    reason=f"sync_calibre_list: {item['title']}")
                    if bookid and mtype == "Ignored":
                        logger.warning(
                            f"Book {item['title']} by {authorname} is marked Ignored in database, importing anyway")
                    if not bookid:
                        searchterm = f"{item['title']}<ll>{authorname}"
                        results = search_for(unaccented(searchterm, only_ascii=False))
                        if results:
                            result = results[0]
                            if result['author_fuzz'] > CONFIG.get_int('MATCH_RATIO') \
                                    and result['book_fuzz'] > CONFIG.get_int('MATCH_RATIO'):
                                logger.debug(
                                    f"Found ({round(result['author_fuzz'], 2)}% {round(result['book_fuzz'], 2)}%) "
                                    f"{result['authorname']}: {result['bookname']}")
                                bookid = result['bookid']
                                import_book(bookid, reason="Added by calibre sync")
                    if bookid:
                        # NOTE: calibre bookid is always an integer, lazylibrarian bookid is a string
                        # (goodreads could be used as an int, but googlebooks can't as it's alphanumeric)
                        # so convert all dict items to strings for ease of matching.
                        map_ctol[str(item['id'])] = str(bookid)
                        map_ltoc[str(bookid)] = str(item['id'])
                    else:
                        logger.warning(
                            f"Calibre Book [{item['title']}] by [{authorname}] is not in lazylibrarian database")
                        nomatch += 1
                else:
                    logger.warning(f"Calibre Author [{item['authors']}] not matched in lazylibrarian database")
                    nomatch += 1

        # Now check current users lazylibrarian read/toread against the calibre library, warn about missing ones
        # which might be books calibre doesn't have, or might be minor differences in author or title

        for idlist in [("Read", readlist), ("To_Read", toreadlist)]:
            booklist = idlist[1]
            for bookid in booklist:
                cmd = "SELECT AuthorID,BookName from books where BookID=?"
                book = db.match(cmd, (bookid,))
                if not book:
                    logger.error(f'Error finding bookid {bookid}')
                else:
                    cmd = "SELECT AuthorName from authors where AuthorID=?"
                    author = db.match(cmd, (book['AuthorID'],))
                    if not author:
                        logger.error(f"Error finding authorid {book['AuthorID']}")
                    else:
                        match = False
                        high = 0
                        highname = ''
                        for item in calibrelist:
                            if item['authors'] == author['AuthorName'] and item['title'] == book['BookName']:
                                logger.debug(f"Exact match for {idlist[0]} [{book['BookName']}]")
                                map_ctol[str(item['id'])] = str(bookid)
                                map_ltoc[str(bookid)] = str(item['id'])
                                match = True
                                break
                        if not match:
                            highid = ''
                            for item in calibrelist:
                                if item['authors'] == author['AuthorName']:
                                    n = fuzz.token_sort_ratio(item['title'], book['BookName'])
                                    if n > high:
                                        high = n
                                        highname = item['title']
                                        highid = item['id']

                            if high > 95:
                                logger.debug(
                                    f"Found ratio match {round(high, 2)}% [{highname}] "
                                    f"for {idlist[0]} [{book['BookName']}]")
                                map_ctol[str(highid)] = str(bookid)
                                map_ltoc[str(bookid)] = str(highid)
                                match = True

                        if not match:
                            logger.warning(
                                f"No match for {idlist[0]} {book['BookName']} by {author['AuthorName']} "
                                f"in calibre database, closest match {round(high, 2)}% [{highname}]")
                            nomatch += 1

        logger.debug(f"BookID mapping complete, {username} match {len(map_ctol)}, nomatch {nomatch}")

        # now sync the lists
        if not userid:
            msg = "No userid found"
        else:
            last_read = []
            last_toread = []
            calibre_read = []
            calibre_toread = []

            cmd = "select SyncList from sync where UserID=? and Label=?"
            res = db.match(cmd, (userid, col_read))
            if res:
                last_read = get_list(res['SyncList'])
            res = db.match(cmd, (userid, col_toread))
            if res:
                last_toread = get_list(res['SyncList'])

            for item in calibrelist:
                itemid = str(item['id'])
                if toreadcol and toreadcol in item and item[toreadcol]:  # only if True
                    if itemid in map_ctol:
                        calibre_toread.append(map_ctol[itemid])
                    else:
                        logger.warning(
                            f"Calibre to_read book {item['authors']}:{item['title']} has no lazylibrarian bookid")
                if readcol and readcol in item and item[readcol]:  # only if True
                    if itemid in map_ctol:
                        calibre_read.append(map_ctol[itemid])
                    else:
                        logger.warning(
                            f"Calibre read book {item['authors']}:{item['title']} has no lazylibrarian bookid")

            logger.debug(f"Found {len(calibre_read)} calibre read, {len(calibre_toread)} calibre toread")
            logger.debug(f"Found {len(readlist)} lazylib read, {len(toreadlist)} lazylib toread")

            added_to_ll_toread = list(set(toreadlist) - set(last_toread))
            removed_from_ll_toread = list(set(last_toread) - set(toreadlist))
            added_to_ll_read = list(set(readlist) - set(last_read))
            removed_from_ll_read = list(set(last_read) - set(readlist))
            logger.debug(
                f"lazylibrarian changes to copy to calibre: {len(added_to_ll_toread)} {len(removed_from_ll_toread)}"
                f" {len(added_to_ll_read)} {len(removed_from_ll_read)}")

            added_to_calibre_toread = list(set(calibre_toread) - set(last_toread))
            removed_from_calibre_toread = list(set(last_toread) - set(calibre_toread))
            added_to_calibre_read = list(set(calibre_read) - set(last_read))
            removed_from_calibre_read = list(set(last_read) - set(calibre_read))
            logger.debug(
                f"calibre changes to copy to lazylibrarian: {len(added_to_calibre_toread)} "
                f"{len(removed_from_calibre_toread)} {len(added_to_calibre_read)} {len(removed_from_calibre_read)}")

            calibre_changes = 0
            for item in added_to_calibre_read:
                if item not in readlist:
                    readlist.append(item)
                    logger.debug(f"Lazylibrarian marked {item} as read")
                    calibre_changes += 1
            for item in added_to_calibre_toread:
                if item not in toreadlist:
                    toreadlist.append(item)
                    logger.debug(f"Lazylibrarian marked {item} as to_read")
                    calibre_changes += 1
            for item in removed_from_calibre_read:
                if item in readlist:
                    readlist.remove(item)
                    logger.debug(f"Lazylibrarian removed {item} from read")
                    calibre_changes += 1
            for item in removed_from_calibre_toread:
                if item in toreadlist:
                    toreadlist.remove(item)
                    logger.debug(f"Lazylibrarian removed {item} from to_read")
                    calibre_changes += 1
            if calibre_changes:
                set_readinglist("ToRead", userid, toreadlist)
                set_readinglist("HaveRead", userid, readlist)

            ll_changes = 0
            for item in added_to_ll_toread:
                if item in map_ltoc:
                    res, err, rc = calibredb('set_custom', [col_toread, map_ltoc[item], 'true'], [])
                    if rc:
                        msg = "calibredb set_custom error: "
                        if err:
                            logger.error(msg + err)
                        elif res:
                            logger.error(msg + res)
                        else:
                            logger.error(msg + str(rc))
                    else:
                        ll_changes += 1
                else:
                    logger.warning(f"Unable to set calibre {col_toread} true for {item}")
            for item in removed_from_ll_toread:
                if item in map_ltoc:
                    res, err, rc = calibredb('set_custom', [col_toread, map_ltoc[item], ''], [])
                    if rc:
                        msg = "calibredb set_custom error: "
                        if err:
                            logger.error(msg + err)
                        elif res:
                            logger.error(msg + res)
                        else:
                            logger.error(msg + str(rc))
                    else:
                        ll_changes += 1
                else:
                    logger.warning(f"Unable to clear calibre {col_toread} for {item}")

            for item in added_to_ll_read:
                if item in map_ltoc:
                    res, err, rc = calibredb('set_custom', [col_read, map_ltoc[item], 'true'], [])
                    if rc:
                        msg = "calibredb set_custom error: "
                        if err:
                            logger.error(msg + err)
                        elif res:
                            logger.error(msg + res)
                        else:
                            logger.error(msg + str(rc))
                    else:
                        ll_changes += 1
                else:
                    logger.warning(f"Unable to set calibre {col_read} true for {item}")

            for item in removed_from_ll_read:
                if item in map_ltoc:
                    res, err, rc = calibredb('set_custom', [col_read, map_ltoc[item], ''], [])
                    if rc:
                        msg = "calibredb set_custom error: "
                        if err:
                            logger.error(msg + err)
                        elif res:
                            logger.error(msg + res)
                        else:
                            logger.error(msg + str(rc))
                    else:
                        ll_changes += 1
                else:
                    logger.warning(f"Unable to clear calibre {col_read} for {item}")

            # store current sync list as comparison for next sync
            control_value_dict = {"UserID": userid, "Label": col_read}
            new_value_dict = {"Date": str(time.time()), "Synclist": ', '.join(readlist)}
            db.upsert("sync", new_value_dict, control_value_dict)
            control_value_dict = {"UserID": userid, "Label": col_toread}
            new_value_dict = {"Date": str(time.time()), "Synclist": ', '.join(toreadlist)}
            db.upsert("sync", new_value_dict, control_value_dict)

            msg = f"{username} sync updated: {ll_changes} calibre, {calibre_changes} lazylibrarian"

    except Exception as e:
        logger.error(f"{e}: {traceback.format_exc()}")

    db.close()
    return msg


def calibre_test():
    logger = logging.getLogger(__name__)
    res, err, rc = calibredb('--version')
    if rc:
        msg = "calibredb communication failed: "
        if err:
            return msg + err
        return msg + res

    if '(calibre ' in res:
        # extract calibredb version number
        vernum = res.split('(calibre ')[1]
        if ')' in vernum:
            vernum = vernum.split(')')[0]
        res = f"calibredb ok, version {vernum}"

        # get a list of categories and counters from the database in CSV format
        cats, err, rc = calibredb('list_categories', ['-ic'])
        logger.debug(f"Calibredb list_categories {cats}")
        cnt = 0
        if not len(cats):
            res += '\nDatabase READ Failed'
        else:
            for entry in cats.split('\n'):
                words = entry.split(',')
                if len(words) >= 2:  # Filter out header and footer
                    item_count = words[2]
                    if item_count.strip('b').strip("'").isdigit():
                        cnt += int(item_count)
        if cnt:
            res += '\nDatabase READ ok'
            wrt, err, rc = calibredb('add', ['--authors', 'LazyLibrarian', '--title', 'dummy', '--empty'], [])
            logger.debug(f"Calibredb add  {wrt}")
            # Answer should look like "Added book ids: bookID" (string may be translated!)
            # or "add Integration status: True Added book ids: bookID"
            try:
                calibre_id = wrt.rsplit(": ", 1)[1].split("\n", 1)[0].strip()
            except IndexError:
                res += '\nDatabase WRITE Failed'
                return res

            # Try to fetch the added book and delete it
            if not calibre_id.isdigit():
                res += '\nDatabase WRITE Failed'
                return res
            if vernum.startswith('2'):
                _, err, rc = calibredb('remove', [calibre_id], [])
            else:
                rmv, err, rc = calibredb('remove', ['--permanent', calibre_id], [])
            if not rc:
                res += '\nDatabase WRITE ok'
            else:
                res += '\nDatabase WRITE2 Failed: '
        else:
            res += '\nDatabase READ Failed or database is empty'
    else:
        res = 'calibredb Failed'
    return res


def calibredb(cmd=None, prelib=None, postlib=None):
    """ calibre-server needs to be started with --enable-auth and needs user/password to add/remove books
        only basic features are available without auth. calibre_server should look like  http://address:port/#library
        default library is used if no #library in the url
        or calibredb can talk to the database file as long as there is no running calibre """

    logger = logging.getLogger(__name__)
    if not CONFIG['IMP_CALIBREDB']:
        return "No calibredb set in config", '', 1

    params = [CONFIG['IMP_CALIBREDB'], cmd]
    if CONFIG.get_bool('CALIBRE_USE_SERVER'):
        dest_url = CONFIG['CALIBRE_SERVER']
        if not dest_url.startswith('http'):
            if CONFIG.get_bool('HTTPS_ENABLED'):
                dest_url = 'https://' + dest_url
            else:
                dest_url = 'http://' + dest_url

        if CONFIG['CALIBRE_USER'] and CONFIG['CALIBRE_PASS']:
            params.extend(['--username', CONFIG['CALIBRE_USER'],
                           '--password', CONFIG['CALIBRE_PASS']])
    else:
        dest_url = get_directory('eBook')
    if prelib:
        params.extend(prelib)

    if cmd != "--version":
        params.extend(['--with-library', f'{dest_url}'])
    if postlib:
        params.extend(postlib)

    logger.debug(f"Run calibre: '{params}'")
    rc, res, err = run_script(params)
    logger.debug(f"calibredb rc {rc}")
    wsp = re.escape(string.whitespace)
    nres = re.sub(r'[' + wsp + ']', ' ', res)
    nerr = re.sub(r'[' + wsp + ']', ' ', err)
    logger.debug(f"calibredb res {len(nres)}[{nres}]")
    logger.debug(f"calibredb err {len(nerr)}[{nerr}]")

    if rc:
        if 'Errno 111' in err:
            logger.warning("calibredb returned Errno 111: Connection refused")
        elif 'Errno 13' in err:
            logger.warning("calibredb returned Errno 13: Permission denied")
        elif cmd == 'list_categories' and len(res):
            rc = 0  # false error return of 1 on v2.xx calibredb
    if 'already exist' in err:
        dest_url = err

    if rc:
        return res, err, rc
    return res, dest_url, 0


def delete_from_calibre(calibre_id):
    logger = logging.getLogger(__name__)
    if calibre_id:
        res, err, rc = calibredb('remove', [calibre_id])
        logger.debug(f"Delete result: {res} [{err}] {rc}")
        return rc == 0
    logger.debug("Missing calibre ID")
    return False


def get_calibre_id(data, try_filename=True):
    """ Get the Calibre ID for 'data', which may be a book or a magazine """
    logger = logging.getLogger(__name__)
    logger.debug(str(data))
    calibre_id = ''
    if not isinstance(data, dict):  # could be sqlite3 row
        try:
            data = dict(data)
        except ValueError:
            return ''
    fname = data.get('BookFile', '')
    if fname:  # it's a book
        author = data.get('AuthorName', '')
        title = data.get('BookName', '')
    else:
        title = data.get('IssueDate', '')
        if title:  # it's a magazine issue
            author = data.get('Title', '')
            fname = data.get('IssueFile', '')
        else:  # assume it's a comic issue
            title = data.get('IssueID', '')
            author = data.get('ComicID', '')
            fname = data.get('IssueFile', '')
    if try_filename:
        try:
            fname = os.path.dirname(fname)
            calibre_id = fname.rsplit('(', 1)[1].split(')')[0]
            if not calibre_id.isdigit():
                calibre_id = ''
        except IndexError:
            calibre_id = ''

    if not calibre_id:
        # ask calibre for id of this issue
        res, err, rc = calibredb('search', [f'author:"{author}" title:"{title}"'])
        if rc:
            if res:
                logger.debug(f'Calibre rc {rc} res [{res}]')
            else:
                logger.debug(f'Calibre rc {rc} err [{err}]')
        else:
            logger.debug(f'Calibre res [{res}]')
            try:
                calibre_id = res.split(',')[0].strip()
            except IndexError:
                calibre_id = ''
    logger.debug(f'Calibre ID [{calibre_id}]')
    return calibre_id
