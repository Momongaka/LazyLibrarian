#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software':'you can redistribute it and/or modify
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
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import logging
import time
import traceback
from urllib.parse import unquote_plus

from lazylibrarian import database
from lazylibrarian.common import mime_type, path_exists
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import splitext
from lib.rfeed import Enclosure, Feed, Guid, Item, iTunes, iTunesCategory, iTunesItem, iTunesOwner

try:
    from lib.tinytag import TinyTag
except ImportError:
    TinyTag = None


def gen_feed(ftype, limit=10, user=0, baseurl='', authorid=None, onetitle=None):
    res = ''
    if not CONFIG.get_bool('RSS_ENABLED'):
        return res
    logger = logging.getLogger(__name__)
    # noinspection PyBroadException
    try:
        podcast = False
        if ftype == 'eBook':
            cmd = "select AuthorName,BookName,BookDesc,BookLibrary,BookFile,BookID from books,authors where "
            if authorid:
                cmd += "books.AuthorID = ? and "
            cmd += "BookLibrary != '' and books.AuthorID = authors.AuthorID order by BookLibrary desc limit ?"
            baselink = f"{baseurl}/book_wall&have=1"
        elif ftype == 'AudioBook':
            podcast = CONFIG.get_bool('RSS_PODCAST')
            cmd = ("select AuthorName,BookName,BookSub,BookDesc,AudioLibrary,AudioFile,BookID "
                   "from books,authors where ")
            if authorid:
                cmd += "books.AuthorID = ? and "
            cmd += ("AudioLibrary != '' and books.AuthorID = authors.AuthorID "
                    "order by AudioLibrary desc limit ?")
            baselink = f"{baseurl}/audio_wall"
        elif ftype == 'Magazine':
            cmd = "select Title,IssueDate,IssueAcquired,IssueFile,IssueID from issues "
            if onetitle:
                cmd += "where Title = ? "
            cmd += "order by IssueAcquired desc limit ?"
            baselink = f"{baseurl}/mag_wall"
        elif ftype == 'Comic':
            cmd = ("select Title,Publisher,comics.ComicID,IssueAcquired,IssueFile,IssueID from "
                   "comics,comicissues where")
            if onetitle:
                cmd += " Title = ? and"
            cmd += " comics.comicid=comicissues.comicid order by IssueAcquired desc limit ?"
            baselink = f"{baseurl}/comic_wall"
        else:
            logger.debug("Invalid feed type")
            return res

        db = database.DBConnection()
        try:
            if authorid:
                results = db.select(cmd, (authorid, limit))
            elif onetitle:
                results = db.select(cmd, (unquote_plus(onetitle).replace('&amp;', '&'), limit))
            else:
                results = db.select(cmd, (limit,))
        finally:
            db.close()
        items = []
        logger.debug(f"Found {len(results)} {ftype}")

        if not results:
            podcast = False

        for result in results:
            link = ''
            img = ''
            itunes_item = ''
            if ftype == 'eBook':
                pubdate = datetime.datetime.strptime(result['BookLibrary'], '%Y-%m-%d %H:%M:%S')
                title = result['BookName']
                author = result['AuthorName']
                description = result['BookDesc']
                bookid = result['BookID']
                extn = splitext(result['BookFile'])[1]
                if user:
                    link = f"{baseurl}/serve_book/{user}{result['BookID']}{extn}"
                    img = f"{baseurl}/serve_img/{user}{result['BookID']}"

            elif ftype == 'AudioBook':
                pubdate = datetime.datetime.strptime(result['AudioLibrary'], '%Y-%m-%d %H:%M:%S')
                title = result['BookName']
                author = result['AuthorName']
                description = result['BookDesc']
                bookid = result['BookID']
                extn = splitext(result['AudioFile'])[1]
                if user:
                    link = f"{baseurl}/serve_audio/{user}{result['BookID']}{extn}"
                    img = f"{baseurl}/serve_img/{user}{result['BookID']}"

                if TinyTag and TinyTag.is_supported(result['AudioFile']) and path_exists(result['AudioFile']):
                    id3r = TinyTag.get(result['AudioFile'])
                    secs = id3r.duration
                    duration = time.strftime('%H:%M:%S', time.gmtime(secs))
                else:
                    duration = "01:11:02"  # any value as default

                itunes_item = iTunesItem(
                    author=result['AuthorName'],
                    image=f"{baseurl}/serve_img/{user}{result['BookID']}.jpg",
                    duration=duration,
                    explicit="clean",
                    subtitle=result['BookSub'],
                    summary=result['BookDesc'])

            elif ftype == 'Magazine':
                pubdate = datetime.datetime.strptime(result['IssueAcquired'], '%Y-%m-%d')
                title = f"{result['Title']} ({result['IssueDate']})"
                author = result['Title']
                description = title
                bookid = result['IssueID']
                extn = splitext(result['IssueFile'])[1]
                if user:
                    link = f"{baseurl}/serve_issue/{user}{result['IssueID']}{extn}"
                    img = f"{baseurl}/serve_img/{user}{result['IssueID']}"

            else:  # if ftype == 'Comic':
                pubdate = datetime.datetime.strptime(result['IssueAcquired'], '%Y-%m-%d')
                title = result['Title']
                author = result['Publisher']
                description = title
                bookid = result['IssueID']
                extn = splitext(result['IssueFile'])[1]
                if user:
                    link = f"{baseurl}/serve_comic/{user}{result['ComicID']}_{result['IssueID']}{extn}"
                    img = f"{baseurl}/serve_img/{user}{result['ComicID']}_{result['IssueID']}"

            if not description:
                description = ''

            if podcast:
                item = Item(
                    title=title,
                    link=link,
                    description=description,
                    author=author,
                    guid=Guid(bookid),
                    pubDate=pubdate,
                    enclosure=Enclosure(url=link, length=0, type=mime_type(result['AudioFile'])),
                    extensions=[itunes_item]
                )
            else:
                html = f'<![CDATA[<p><img width="500" height="600" src="{img}_w500"'
                html += ' class="webfeedsFeaturedVisual wp-post-image"'
                html += f' alt="{title}" loading="lazy"><p>]]>{description}'

                item = Item(
                    title=title,
                    link=link,
                    description=html,
                    author=author,
                    guid=Guid(bookid),
                    pubDate=pubdate,
                    thumbnail=f"{img}_w100"
                )
            items.append(item)

        itunes = iTunes(
            author="LazyLibrarian",
            subtitle="Podcast of recent audiobooks",
            summary="Audiobooks in the library",
            image=f'{baseurl}/serve_img/{user}.png',
            explicit="clean",
            categories=iTunesCategory(name='AudioBooks', subcategory='Recent AudioBooks'),
            owner=iTunesOwner(name='LazyLibrarian', email=CONFIG['ADMIN_EMAIL']))

        title = f"{ftype} Recent Downloads"
        if authorid and results:
            title = f"{results[0]['AuthorName']} {ftype} Recent Downloads"
        elif onetitle and results:
            title = f"{unquote_plus(onetitle).replace('&amp;', '&')} {ftype} Recent Downloads"

        if podcast:
            feed = Feed(
                title="Podcast rss Feed",
                link=baselink,
                description=f"LazyLibrarian {title}",
                language="en-US",
                lastBuildDate=datetime.datetime.now(),
                items=items,
                extensions=[itunes])
        else:
            feed = Feed(
                title=title,
                link=baselink,
                description=f"LazyLibrarian {title}",
                language="en-US",
                lastBuildDate=datetime.datetime.now(),
                image=f'{baseurl}/serve_img/{user}',
                items=items)

        logger.debug(f"Returning {len(items)} {ftype}")

        res = feed.rss()
    except Exception:
        logger.error(f'Unhandled exception in rssfeed: {traceback.format_exc()}')
    return res
