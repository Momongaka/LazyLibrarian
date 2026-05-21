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
import os
import shutil
import threading
import traceback

from lazylibrarian import database
from lazylibrarian.config2 import CONFIG
from lazylibrarian.configtypes import ConfigDict
from lazylibrarian.filesystem import DIRS, csv_file, path_isdir, remove_file, safe_move, syspath
from lazylibrarian.formatter import (
    format_author_name,
    get_list,
    is_valid_isbn,
    make_unicode,
    now,
    plural,
    split_title,
    unaccented,
)
from lazylibrarian.importer import add_author_name_to_db, import_book, search_for, update_totals
from lazylibrarian.librarysync import find_book_in_db

try:
    from csv import QUOTE_MINIMAL, reader, writer
except ImportError:
    from lib.csv import QUOTE_MINIMAL, reader, writer


# noinspection PyArgumentList
def dump_table(table, savedir=None, status=None):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        columns = db.select(f'PRAGMA table_info({table})')
        if not columns:  # no such table
            logger.warning(f"No such table [{table}]")
            return 0

        if not path_isdir(savedir):
            savedir = DIRS.DATADIR
        else:
            savedir = str(savedir)

        headers = ''
        for item in columns:
            if headers:
                headers += ','
            headers += item[1]
        if status:
            cmd = f"SELECT {headers} from {table} WHERE status='{status}'"
        else:
            cmd = f"SELECT {headers} from {table}"
        data = db.select(cmd)
        count = 0
        if data is not None:
            label = table
            if status:
                label += f'_{status}'
            csvfile = os.path.join(savedir, f"{label}.csv")
            headers = headers.split(',')
            with open(syspath(csvfile), 'w', encoding='utf-8', newline='') as outfile:
                # noinspection PyTypeChecker
                csvwrite = writer(outfile, delimiter=',', quotechar='"', quoting=QUOTE_MINIMAL)
                csvwrite.writerow(headers)
                for item in data:
                    csvwrite.writerow([str(s) if s else '' for s in item])
                    count += 1
            msg = f"Exported {count} {plural(count, 'item')} to {csvfile}"
            logger.info(msg)
        return count
    except Exception:
        msg = f'Unhandled exception in dump_table: {traceback.format_exc()}'
        logger.error(msg)
        return 0
    finally:
        db.close()


def restore_table(table, savedir=None, status=None):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        columns = db.select(f'PRAGMA table_info({table})')
        if not columns:  # no such table
            logger.warning(f"No such table [{table}]")
            return 0

        if not path_isdir(savedir):
            savedir = DIRS.DATADIR
        else:
            savedir = str(savedir)

        headers = ''

        label = table
        if status:
            label += f'_{status}'
        csvfile = os.path.join(savedir, f"{label}.csv")

        logger.debug(f'Reading file {csvfile}')
        csvreader = reader(open(csvfile, encoding='utf-8', newline=''))
        count = 0
        for row in csvreader:
            if csvreader.line_num == 1:
                headers = row
            else:
                item = dict(list(zip(headers, row, strict=True)))

                if table == 'magazines':
                    control_value_dict = {"Title": make_unicode(item['Title'])}
                    new_value_dict = {"Regex": make_unicode(item['Regex']),
                                      "Reject": make_unicode(item['Reject']),
                                      "Status": item['Status'],
                                      "MagazineAdded": item['MagazineAdded'],
                                      "IssueStatus": item['IssueStatus'],
                                      "CoverPage": item['CoverPage'],
                                      "Language": item['Language']}
                    db.upsert("magazines", new_value_dict, control_value_dict)
                    count += 1

                elif table == 'users':
                    control_value_dict = {"UserID": item['UserID']}
                    new_value_dict = {"UserName": item['UserName'],
                                      "Password": item['Password'],
                                      "Email": item['Email'],
                                      "Name": item['Name'],
                                      "Perms": item['Perms'],
                                      "CalibreRead": item['CalibreRead'],
                                      "CalibreToRead": item['CalibreToRead'],
                                      "BookType": item['BookType']
                                      }
                    db.upsert("users", new_value_dict, control_value_dict)
                    count += 1
                else:
                    logger.error(f"Invalid table [{table}]")
                    return 0
        msg = f"Imported {count} {plural(count, 'item')} from {csvfile}"
        logger.info(msg)
        return count

    except Exception:
        msg = f'Unhandled exception in restore_table: {traceback.format_exc()}'
        logger.error(msg)
        return 0
    finally:
        db.close()


def export_csv(search_dir=None, status="Wanted", library=''):
    """ Write a csv file to the search_dir containing all books marked as "Wanted" """
    logger = logging.getLogger(__name__)
    if not library:
        if CONFIG.get_bool('AUDIO_TAB'):
            library = 'AudioBook'
        else:
            library = 'eBook'
    # noinspection PyBroadException
    try:
        if not search_dir:
            msg = "Alternate Directory not configured"
            logger.warning(msg)
            return msg
        if not path_isdir(search_dir):
            msg = f"Alternate Directory [{search_dir}] not found"
            logger.warning(msg)
            return msg
        if not os.access(syspath(search_dir), os.W_OK | os.X_OK):
            msg = f"Alternate Directory [{search_dir}] not writable"
            logger.warning(msg)
            return msg

        csvfile = os.path.join(search_dir, f"{status} {library} - {now().replace(':', '-')}.csv")

        db = database.DBConnection()
        try:
            cmd = "SELECT BookID,AuthorName,BookName,BookIsbn,books.AuthorID FROM books,authors "
            if library == 'eBook':
                cmd += "WHERE books.Status=? and books.AuthorID = authors.AuthorID"
            else:
                cmd += "WHERE AudioStatus=? and books.AuthorID = authors.AuthorID"
            find_status = db.select(cmd, (status,))
        finally:
            db.close()

        if not find_status:
            msg = f"No {library} marked as {status}"
            logger.warning(msg)
            return msg
        count = 0
        # noinspection PyArgumentList
        with open(syspath(csvfile), 'w', encoding='utf-8', newline='') as outfile:
            # noinspection PyTypeChecker
            csvwrite = writer(outfile, delimiter=',', quotechar='"', quoting=QUOTE_MINIMAL)

            # write headers, change AuthorName BookName BookIsbn to match import csv names
            csvwrite.writerow(['BookID', 'Author', 'Title', 'ISBN', 'AuthorID'])

            for resulted in find_status:
                logger.debug(f"Exported CSV for {library} {resulted['BookName']}")
                row = ([resulted['BookID'], resulted['AuthorName'], resulted['BookName'],
                        resulted['BookIsbn'], resulted['AuthorID']])
                csvwrite.writerow([f"{s}" for s in row])
                count += 1
        msg = f"CSV exported {count} {plural(count, library)} to {csvfile}"
        logger.info(msg)
    except Exception:
        msg = f'Unhandled exception in export_csv: {traceback.format_exc()}'
        logger.error(msg)

    return msg


def finditem(item, preferred_authorname, library='eBook', reason='csv.finditem'):
    """
    Try to find book matching the csv item in the database
    Return database entry, or empty dict if not found
    """
    db = database.DBConnection()
    bookmatch = {}
    try:
        isbn10 = ""
        isbn13 = ""
        bookid = ""
        bookname = item['Title']

        bookname = make_unicode(bookname)
        if 'ISBN' in item:
            isbn10 = item['ISBN']
        if 'ISBN13' in item:
            isbn13 = item['ISBN13']
        if 'BookID' in item:
            bookid = item['BookID']

        # try to find book in our database using bookid or isbn, or if that fails, name matching
        cmd = ("SELECT AuthorName,BookName,BookID,books.Status,AudioStatus,Requester,AudioRequester FROM "
               "books,authors where books.AuthorID = authors.AuthorID ")
        if bookid:
            fullcmd = f"{cmd}and BookID=?"
            bookmatch = db.match(fullcmd, (bookid,))
        if not bookmatch and is_valid_isbn(isbn10):
            fullcmd = f"{cmd}and BookIsbn=?"
            bookmatch = db.match(fullcmd, (isbn10,))
        if not bookmatch and is_valid_isbn(isbn13):
            fullcmd = f"{cmd}and BookIsbn=?"
            bookmatch = db.match(fullcmd, (isbn13,))
        if not bookmatch:
            bookid, _ = find_book_in_db(preferred_authorname, bookname, ignored=False, library=library,
                                        reason=reason)
            if bookid:
                fullcmd = f"{cmd}and BookID=?"
                bookmatch = db.match(fullcmd, (bookid,))
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(str(e))
    db.close()
    return bookmatch


def import_csv(search_dir: str, status: str = 'Wanted', library: str = '', config: ConfigDict = CONFIG) -> str:
    """ Find a csv file in the search_dir and process all the books in it,
        adding authors to the database if not found
        and marking the books as "Wanted"
        Delete the file on successful completion if 'DELETE_CSV' is True
    """
    logger = logging.getLogger(__name__)
    if not library:
        library = 'AudioBook' if CONFIG.get_bool('AUDIO_TAB') else 'eBook'
    if not search_dir:
        msg = "Alternate Directory not configured"
        logger.warning(msg)
        return msg
    if not path_isdir(search_dir):
        msg = f"Alternate Directory [{search_dir}] not found"
        logger.warning(msg)
        return msg

    # noinspection PyBroadException
    db = database.DBConnection()
    try:
        csvfile = csv_file(search_dir, library=library)

        headers = None

        bookcount = 0
        authcount = 0
        skipcount = 0
        total = 0
        existing = 0

        if not csvfile:
            msg = f"No {library} CSV file found in {search_dir}"
            logger.warning(msg)
            return msg

        logger.debug(f'Reading file {csvfile}')
        csvreader = reader(open(csvfile, encoding='utf-8', newline=''))
        for row in csvreader:
            if csvreader.line_num == 1:
                # If we are on the first line, create the headers list from the first row
                headers = row
                if 'Author' not in headers or 'Title' not in headers:
                    msg = f'Invalid CSV file found {csvfile}'
                    logger.warning(msg)
                    return msg
            elif row:
                total += 1
                item = dict(list(zip(headers, row, strict=True)))
                authorname = format_author_name(item['Author'], postfix=get_list(config.get_csv('NAME_POSTFIX')))
                title = make_unicode(item['Title'])

                authmatch = db.match('SELECT * FROM authors where AuthorName=?', (authorname,))

                if authmatch:
                    logger.debug(f"CSV: Author {authorname} found in database")
                    authorid = authmatch['authorid']
                else:
                    logger.debug(f"CSV: Author {authorname} not found")
                    newauthor, authorid, new = add_author_name_to_db(author=authorname, addbooks=False,
                                                                     reason=f"import_csv {csvfile}",
                                                                     title=title)
                    if newauthor and newauthor != authorname:
                        logger.debug(f"Preferred authorname changed from [{authorname}] to [{newauthor}]")
                        authorname = newauthor
                    if new:
                        authcount += 1
                    if not authorid:
                        logger.warning(f"Authorname {authorname} not added to database")

                    if authorid:
                        bookmatch = finditem(item, authorname, library=library, reason=f'import_csv: {csvfile}')
                    else:
                        bookmatch = {}

                    imported = False
                    results = []
                    if bookmatch:
                        authorname = bookmatch['AuthorName']
                        bookname = bookmatch['BookName']
                        bookid = bookmatch['BookID']
                        bookstatus = bookmatch['Status'] if library == 'eBook' else bookmatch['AudioStatus']
                        if bookstatus in ['Open', 'Wanted', 'Have']:
                            existing += 1
                            logger.info(f'Found {library} {bookname} by {authorname}, already marked as "{bookstatus}"')
                            imported = True
                        else:  # skipped/ignored
                            logger.info(f'Found {library} {bookname} by {authorname}, marking as "{status}"')
                            control_value_dict = {"BookID": bookid}
                            new_value_dict = {"Status": status} if library == 'eBook' else {"AudioStatus": status}
                            db.upsert("books", new_value_dict, control_value_dict)
                            bookcount += 1
                    elif authorid:
                        searchterm = f"{title}<ll>{authorname}"
                        results = search_for(unaccented(searchterm, only_ascii=False))
                        for result in results:
                            if result['book_fuzz'] >= CONFIG.get_int('MATCH_RATIO') \
                                    and result['authorid'] == authorid:
                                bookmatch = result
                                break
                        if not bookmatch:  # no match on full searchterm, try splitting out subtitle and series
                            newtitle, _, _ = split_title(authorname, title)
                            if newtitle != title:
                                title = newtitle
                                searchterm = f"{title}<ll>{authorname}"
                                results = search_for(unaccented(searchterm, only_ascii=False))
                                for result in results:
                                    if result['book_fuzz'] >= CONFIG.get_int('MATCH_RATIO') \
                                            and result['authorid'] == authorid:
                                        bookmatch = result
                                        break
                        if bookmatch:
                            logger.info(
                                f"Found ({round(bookmatch['book_fuzz'], 2)}%) {bookmatch['authorname']}: "
                                f"{bookmatch['bookname']} for {authorname}: {title}")
                            if library == 'eBook':
                                import_book(bookmatch['bookid'], ebook=status, wait=True,
                                            reason=f"Added by import_csv {csvfile}")
                            else:
                                import_book(bookmatch['bookid'], audio=status, wait=True,
                                            reason=f"Added by import_csv {csvfile}")
                            imported = db.match('select * from books where BookID=?', (bookmatch['bookid'],))
                            if imported:
                                bookcount += 1

                    if bookmatch and imported:
                        update_totals(authorid)
                    else:
                        msg = f"Skipping book {title} by {authorname}"
                        if not results:
                            msg += ', No results found'
                            logger.warning(msg)
                        elif bookmatch and not imported:
                            msg += f", Failed to import {bookmatch['bookid']}"
                            logger.warning(msg)
                        else:
                            msg += ', No match found'
                            logger.warning(msg)
                            msg = (f"Closest match ({round(results[0]['author_fuzz'], 2)}% "
                                   f"{round(results[0]['book_fuzz'], 2)}%) "
                                   f"{results[0]['authorname']}: {results[0]['bookname']}")
                            if results[0]['authorid'] != authorid:
                                msg += ' wrong authorid'
                            logger.warning(msg)
                        skipcount += 1

        msg = f"Found {total} {library}{plural(total)} in csv file, {existing} already existing or Wanted"
        logger.info(msg)
        msg = (f"Added {authcount} new {plural(authcount, 'author')}, marked {bookcount} "
               f"{plural(bookcount, library)} as '{status}', {skipcount} {plural(skipcount, library)} not found")
        logger.info(msg)
        if CONFIG.get_bool('DELETE_CSV'):
            if skipcount == 0:
                logger.info(f"Deleting {csvfile} on successful completion")
                try:
                    remove_file(csvfile)
                except OSError as why:
                    logger.warning(f'Unable to delete {csvfile}: {why.strerror}')
            else:
                logger.warning(f"Not deleting {csvfile} as not all books found")
                if path_isdir(f"{csvfile}.fail"):
                    try:
                        shutil.rmtree(f"{csvfile}.fail")
                        logger.debug(f"Removed folder {csvfile}.fail")
                    except Exception as why:
                        logger.warning(f"Unable to remove {csvfile}.fail, {type(why).__name__} {why}")
                try:
                    _ = safe_move(csvfile, f"{csvfile}.fail")
                except Exception as e:
                    logger.error(f"Unable to rename {csvfile}, {type(e).__name__} {str(e)}")
                    if not os.access(syspath(csvfile), os.R_OK):
                        logger.error(f"{csvfile} is not readable")
                    if not os.access(syspath(csvfile), os.W_OK):
                        logger.error(f"{csvfile} is not writeable")
                    parent = os.path.dirname(csvfile)
                    try:
                        with open(syspath(os.path.join(parent, 'll_temp')), 'w') as f:
                            f.write('test')
                        remove_file(os.path.join(parent, 'll_temp'))
                    except Exception as why:
                        logger.error(f"Directory {parent} is not writeable: {why}")
    except Exception:
        msg = f'Unhandled exception in import_csv: {traceback.format_exc()}'
        logger.error(msg)

    db.close()
    if 'IMPORTCSV' in threading.current_thread().name:
        threading.current_thread().name = 'WEBSERVER'
    return msg
