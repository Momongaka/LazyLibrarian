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

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.common import run_script
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import path_exists, splitext, syspath
from lazylibrarian.formatter import check_int, get_list, plural
from lazylibrarian.notifiers import email_notifier


def mailing_list(book_type, global_name, book_id):
    if not CONFIG.get_bool('USER_ACCOUNTS'):
        return
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    try:
        columns = db.select('PRAGMA table_info(subscribers)')
        if not columns:  # no such table
            return

        # if book check users who subscribe to author or series
        # if from a wishlist or rss check which users are subscribed to the feed
        # if magazine or comic issue check which users subscribe to the title
        booktype = book_type.lower()
        if booktype in ['ebook', 'audiobook']:
            if booktype == 'ebook':
                data = db.match("SELECT Requester,AuthorID,BookFile as filename from books where bookid=?", (book_id,))
                feeds = get_list(data['Requester'])
            else:
                data = db.match("SELECT AudioRequester,AuthorID,AudioFile as filename from books where bookid=?",
                                (book_id,))
                feeds = get_list(data['AudioRequester'])

            users = db.select("SELECT UserID from subscribers WHERE Type='author' and WantID=?", (data["AuthorID"],))
            cnt = 0
            for user in users:
                db.action('INSERT into subscribers (UserID, Type, WantID) VALUES (?, ?, ?)',
                          (user['UserID'], booktype, book_id))
                cnt += 1
            if cnt:
                logger.debug(f"{book_id} wanted by {cnt} {plural(cnt, 'subscriber')} to author {data['AuthorID']}")

            series = db.select('SELECT SeriesID from member WHERE BookID=?', (book_id,))
            for item in series:
                users = db.select("SELECT UserID  from subscribers WHERE Type='series' and WantID=?",
                                  (item["SeriesID"],))
                cnt = 0
                for user in users:
                    db.action('INSERT into subscribers (UserID , Type, WantID) VALUES (?, ?, ?)',
                              (user['UserID'], booktype, book_id))
                    cnt += 1
                if cnt:
                    logger.debug(f"{book_id} wanted by {cnt} {plural(cnt, 'subscriber')} to series {item['SeriesID']}")

            for item in feeds:
                users = db.select("SELECT UserID from subscribers WHERE Type='feed' and WantID=?", (item,))
                cnt = 0
                for user in users:
                    db.action('INSERT into subscribers (UserID, Type, WantID) VALUES (?, ?, ?)',
                              (user['UserID'], booktype, book_id))
                    cnt += 1
                if cnt:
                    logger.debug(f"{book_id} wanted by {cnt} {plural(cnt, 'subscriber')} to feed {item}")

        elif booktype == 'magazine':
            data = db.match("SELECT Title,IssueFile as filename from issues where IssueID=?", (book_id,))
            if not data:
                logger.error(f'Invalid issueid [{book_id}]')
                return
            users = db.select("SELECT UserID from subscribers WHERE Type='magazine' and WantID=?", (data["Title"],))
            cnt = 0
            for user in users:
                db.action('INSERT into subscribers (UserID, type, WantID) VALUES (?, ?, ?)',
                          (user['UserID'], booktype, book_id))
                cnt += 1
            if cnt:
                logger.debug(f"{book_id} wanted by {cnt} {plural(cnt, 'subscriber')} to magazine {data['Title']}")
        elif booktype == 'comic':
            try:
                comicid, issueid = book_id.split('_')
            except ValueError:
                logger.error(f"Invalid comicid/issueid [{book_id}]")
                return
            data = db.match("SELECT IssueFile as filename from comicissues where comicid=? and issueid=?",
                            (comicid, issueid))
            users = db.select("SELECT UserID from subscribers WHERE Type='comic' and WantID=?", (comicid,))
            cnt = 0
            for user in users:
                db.action('INSERT into subscribers (UserID, Type, WantID) VALUES (?, ?, ?)',
                          (user['UserID'], booktype, book_id))
                cnt += 1
            if cnt:
                logger.debug(f"{book_id} wanted by {cnt} {plural(cnt, 'subscriber')} to comic {comicid}")
        else:
            logger.error(f"Invalid booktype [{book_type}]")
            return

        # now send to all users requesting it
        users = db.select('SELECT UserID from subscribers WHERE Type=? and WantID=?', (booktype, book_id))
        userlist = []
        for user in users:
            userlist.append(user['UserID'])

        userlist = set(userlist)  # eg in case subscribed to author and series or book in multiple wishlist

        if not len(userlist):
            logger.debug(f"{book_type} {global_name} not wanted by any users")
            return
        logger.debug(f"{book_type} {global_name} wanted by {len(userlist)} {plural(len(userlist), 'user')}")

        if not data or not data['filename'] or not path_exists(data['filename']):
            logger.error(f"Unable to locate {booktype} {book_id}")
            return

        filename = data['filename']
        fsize = check_int(os.path.getsize(syspath(filename)), 0)
        limit = CONFIG.get_int('EMAIL_LIMIT')
        link = None
        if limit and fsize > limit * 1024 * 1024:
            msg = f'{os.path.split(filename)[1]} is too large ({fsize}) to email'
            logger.debug(msg)
            if CONFIG['CREATE_LINK']:
                logger.debug(f"Creating link to {filename}")
                params = [CONFIG['CREATE_LINK'], filename]
                rc, res, err = run_script(params)
                if res and res.startswith('http'):
                    msg = f"{os.path.basename(filename)} is available to download, {res}"
                    logger.debug(msg)
                    link = res
                    filename = ''
            else:
                filename = ''

        count = 0
        for user in userlist:
            msg = ''
            res = db.match('SELECT SendTo,BookType from users where UserID=?', (user,))
            if res and res['SendTo']:
                if booktype == 'ebook':
                    pref = res['BookType']
                    basename, extn = splitext(filename)
                    prefname = f"{basename}.{pref}"
                    if path_exists(prefname):
                        filename = prefname
                    else:
                        msg = lazylibrarian.NEWFILE_MSG.replace('{name}', global_name).replace('{link}', '').replace(
                            '{method}', f" is available for download, but not as {pref}")
                        filename = ''
                result = None
                if not link:
                    link = ''
                if ',' in res['SendTo']:
                    addrs = get_list(res['SendTo'])
                else:
                    addrs = [res['SendTo']]
                if filename:
                    for addr in addrs:
                        logger.debug(f"Emailing {filename} to {addr}")
                        msg = lazylibrarian.NEWFILE_MSG.replace('{name}', global_name).replace(
                            '{method}', ' is attached').replace('{link}', '')
                        result = email_notifier.email_file(subject="Message from LazyLibrarian",
                                                           message=msg, to_addr=addr, files=[filename])
                        if not result:
                            break
                else:
                    for addr in addrs:
                        if not addr.endswith('@kindle.com'):  # don't send to kindle if no attachment
                            logger.debug(f"Notifying {global_name} available to {res['SendTo']}")
                            if not msg:
                                msg = lazylibrarian.NEWFILE_MSG.replace('{name}', global_name).replace(
                                    '{link}', link).replace('{method}', ' is available for download ')
                            result = email_notifier.email_file(subject="Message from LazyLibrarian",
                                                               message=msg, to_addr=addr, files=[])
                if result:
                    count += 1
                    db.action("DELETE from subscribers WHERE UserID=? and Type=? and WantID=?",
                              (user, booktype, book_id))
                else:
                    # should we also delete from mailing list if email failed?
                    msg = f"Failed to email file {os.path.split(filename)[1]} to {res['SendTo']}"
                    logger.error(msg)
    finally:
        db.close()

    logger.debug(f"Emailed/Notified {book_type} {global_name} to {count} {plural(count, 'user')}")
