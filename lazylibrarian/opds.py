#!/usr/bin/env python

#  This file is part of LazyLibrarian.
#
#  LazyLibrarian is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  LazyLibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with LazyLibrarian.  If not, see <http://www.gnu.org/licenses/>.
#  Adapted for LazyLibrarian from Mylar

import datetime
import logging
import os
from urllib.parse import quote_plus

import cherrypy
from cherrypy.lib.static import serve_file

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.bookrename import name_vars
from lazylibrarian.cache import ImageType, cache_img
from lazylibrarian.common import get_readinglist, mime_type, zip_audio
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import any_file, listdir, path_isfile, splitext
from lazylibrarian.formatter import check_int, get_list, make_unicode, plural

searchable = ['EAuthors', 'AAuthors', 'Magazines', 'Series', 'EAuthor', 'AAuthor', 'RecentBooks',
              'RecentAudio', 'RecentMags', 'RatedBooks', 'RatedAudio', 'ReadBooks', 'ToReadBooks',
              'Genre', 'Genres', 'Comics', 'Comic', 'RecentComics']

cmd_list = searchable + ['root', 'Serve', 'search', 'Members', 'Magazine']


class OPDS:

    def __init__(self):
        self.cmd = None
        self.img = None
        self.filepath = None
        self.filename = None
        self.kwargs = None
        self.data = None
        self.user_agent = ''
        self.reader = ''

        self.PAGE_SIZE = CONFIG.get_int('OPDS_PAGE')

        if CONFIG['HTTP_ROOT'] is None:
            self.opdsroot = '/opds'
        elif CONFIG['HTTP_ROOT'].endswith('/'):
            self.opdsroot = CONFIG['HTTP_ROOT'] + 'opds'
        else:
            self.opdsroot = CONFIG['HTTP_ROOT'] + '/opds'
        """
        my_ip = cherrypy.request.headers.get('X-Forwarded-Host')
        if not my_ip:
            my_ip = cherrypy.request.headers.get('Host')

        self.opdsroot = '%s://%s%s' % (cherrypy.request.scheme, my_ip, self.opdsroot)
        """
        self.searchroot = self.opdsroot.replace('/opds', '')
        self.logger = logging.getLogger(__name__)
        self.dlcommslogger = logging.getLogger('special.dlcomms')

    def check_params(self, **kwargs):
        if 'cmd' not in kwargs:
            self.cmd = 'root'

        if not CONFIG.get_bool('OPDS_ENABLED'):
            self.data = self._error_with_message('OPDS not enabled')
            return

        if not self.cmd:
            if kwargs['cmd'] not in cmd_list:
                self.data = self._error_with_message(f"Unknown command: {kwargs['cmd']}")
                return
            self.cmd = kwargs.pop('cmd')

        self.kwargs = kwargs
        self.data = 'OK'

    def fetch_data(self):
        if self.data == 'OK':
            remote_ip = cherrypy.request.headers.get('X-Forwarded-For')  # apache2
            if not remote_ip:
                remote_ip = cherrypy.request.headers.get('X-Host')  # lighthttpd
            if not remote_ip:
                remote_ip = cherrypy.request.headers.get('Remote-Addr')
            if not remote_ip:
                remote_ip = cherrypy.request.remote.ip

            self.user_agent = cherrypy.request.headers.get('User-Agent')
            self.dlcommslogger.debug(self.user_agent)

            # NOTE Moon+ identifies as Aldiko/Moon+  so check for Moon+ first
            # at the moment we only need to identify Aldiko as it doesn't paginate properly
            reader_ids = ['Moon+', 'FBReader', 'Aldiko']
            for item in reader_ids:
                if item in self.user_agent:
                    self.reader = ' (' + item + ')'
                    break

            self.logger.debug(f"Received OPDS command from {remote_ip}{self.reader} {self.cmd} {self.kwargs}")

            if self.cmd == 'search':
                if 't' in self.kwargs and self.kwargs['t'] in searchable:
                    self.cmd = self.kwargs['t']
                else:
                    self.cmd = 'RecentBooks'
            method_to_call = getattr(self, "_" + self.cmd.lower())
            try:
                _ = method_to_call(**self.kwargs)
                if self.img:
                    return serve_file(self.img, content_type='image/jpeg')
                if self.filepath and self.filename:
                    self.logger.debug(f"Downloading {self.filename}: {self.filepath}")
                    return serve_file(self.filepath, mime_type(self.filename), 'attachment', name=self.filename)
                if isinstance(self.data, str):
                    return self.data
                cherrypy.response.headers['Content-Type'] = "text/xml"
                self.logger.debug(f"Returning {self.data['title']}: {len(self.data['entries'])} entries")
                # noinspection PyUnresolvedReferences
                return lazylibrarian.webServe.serve_template(templatename="opds.html",
                                                             title=self.data['title'], opds=self.data)
            except Exception as e:
                self.logger.error(f"Unhandled OPDS {self.cmd} error: {e}")
        else:
            return self.data

    def multi_link(self, bookfile, bookid):
        types = []
        multi = ''
        basename, _ = splitext(bookfile)
        if not isinstance(basename, str):
            basename = basename.decode('utf-8')
        for item in get_list(CONFIG['EBOOK_TYPE']):
            target = basename + '.' + item
            if path_isfile(target):
                types.append(item)
        if len(types) > 1:
            for fmt in types:
                multi += '<link href="'
                multi += f'{self.opdsroot}?cmd=Serve&amp;bookid={quote_plus(bookid)}&amp;fmt={fmt}'
                multi += '" rel="http://opds-spec.org/acquisition" type="' + mime_type('.' + fmt) + '"/>'
        return multi

    @staticmethod
    def _error_with_message(message):
        error = f'<feed><error>{message}</error></feed>'
        cherrypy.response.headers['Content-Type'] = "text/xml"
        return error

    def _root(self, **kwargs):
        db = database.DBConnection()
        try:
            feed = {'title': 'LazyLibrarian OPDS', 'id': 'OPDSRoot', 'updated': now()}
            links = []
            entries = []

            userid = ''
            if 'user' in kwargs:
                userid = f'&amp;user={kwargs["user"]}'

            links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; '
                                                           'kind=navigation',
                                 rel='start', title='Home'))
            links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; '
                                                           'kind=navigation',
                                 rel='self'))
            links.append(getlink(href=f'{self.searchroot}/opensearchbooks.xml',
                                 ftype='application/opensearchdescription+xml', rel='search', title='Search Books'))

            res = db.match("select count(*) as counter from books where Status='Open'")
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Recent eBooks ({res["counter"]})',
                        'id': 'RecentBooks',
                        'updated': now(),
                        'content': 'Recently Added eBooks',
                        'href': f'{self.opdsroot}?cmd=RecentBooks{userid}',
                        'kind': 'acquisition',
                        'rel': 'subsection',
                    }
                )

            res = db.match("select count(*) as counter from books where AudioStatus='Open'")
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Recent AudioBooks ({res["counter"]})',
                        'id': 'RecentAudio',
                        'updated': now(),
                        'content': 'Recently Added AudioBooks',
                        'href': f'{self.opdsroot}?cmd=RecentAudio{userid}',
                        'kind': 'acquisition',
                        'rel': 'subsection',
                    }
                )

            res = db.match("SELECT count(*) as counter from comics WHERE LastAcquired != ''")
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Recent Comics ({res["counter"]})',
                        'id': 'RecentComics',
                        'updated': now(),
                        'content': 'Recently Added Comic Issues',
                        'href': f'{self.opdsroot}?cmd=RecentComics{userid}',
                        'kind': 'acquisition',
                        'rel': 'subsection',
                    }
                )

            res = db.match("select count(*) as counter from issues where IssueFile != ''")
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Recent Magazine Issues ({res["counter"]})',
                        'id': 'RecentMags',
                        'updated': now(),
                        'content': 'Recently Added Magazine Issues',
                        'href': f'{self.opdsroot}?cmd=RecentMags{userid}',
                        'kind': 'acquisition',
                        'rel': 'subsection',
                    }
                )

            res = db.match("select count(*) as counter from books where Status='Open' "
                           "and CAST(BookRate AS INTEGER) > 0")
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Best Rated eBooks ({res["counter"]})',
                        'id': 'RatedBooks',
                        'updated': now(),
                        'content': 'Best Rated eBooks',
                        'href': f'{self.opdsroot}?cmd=RatedBooks{userid}',
                        'kind': 'acquisition',
                        'rel': 'subsection',
                    }
                )

            cmd = ("select count(*) as counter from books where AudioStatus='Open' "
                   "and CAST(BookRate AS INTEGER) > 0")
            res = db.match(cmd)
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Best Rated AudioBooks ({res["counter"]})',
                        'id': 'RatedAudio',
                        'updated': now(),
                        'content': 'Best Rated AudioBooks',
                        'href': f'{self.opdsroot}?cmd=RatedAudio{userid}',
                        'kind': 'acquisition',
                        'rel': 'subsection',
                    }
                )

            if userid:
                readfilter = get_readinglist("HaveRead", kwargs['user'])
                if len(readfilter) > 0:
                    entries.append(
                        {
                            'title': f'Read Books ({len(readfilter)})',
                            'id': 'ReadBooks',
                            'updated': now(),
                            'content': 'Books marked as Read',
                            'href': f'{self.opdsroot}?cmd=ReadBooks{userid}',
                            'kind': 'acquisition',
                            'rel': 'subsection',
                        }
                    )
                readfilter = get_readinglist("ToRead", kwargs['user'])
                if len(readfilter) > 0:
                    entries.append(
                        {
                            'title': f'To Read Books ({len(readfilter)})',
                            'id': 'ToReadBooks',
                            'updated': now(),
                            'content': 'Books marked as To-Read',
                            'href': f'{self.opdsroot}?cmd=ToReadBooks{userid}',
                            'kind': 'acquisition',
                            'rel': 'subsection',
                        }
                    )

            cmd = "SELECT count(*) as counter from authors WHERE Status != 'Ignored' and HaveEBooks > 0"
            res = db.match(cmd)
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Ebook Authors ({res["counter"]})',
                        'id': 'EAuthors',
                        'updated': now(),
                        'content': 'List of Ebook Authors',
                        'href': f'{self.opdsroot}?cmd=EAuthors{userid}',
                        'kind': 'navigation',
                        'rel': 'subsection',
                    }
                )

            cmd = "SELECT count(*) as counter from authors WHERE Status != 'Ignored' and HaveAudioBooks > 0"
            res = db.match(cmd)
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Audiobook Authors ({res["counter"]})',
                        'id': 'AAuthors',
                        'updated': now(),
                        'content': 'List of Audiobook Authors',
                        'href': f'{self.opdsroot}?cmd=AAuthors{userid}',
                        'kind': 'navigation',
                        'rel': 'subsection',
                    }
                )

            res = db.match("SELECT count(*) as counter from series WHERE CAST(Have AS INTEGER) > 0")
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Series ({res["counter"]})',
                        'id': 'Series',
                        'updated': now(),
                        'content': 'List of Series',
                        'href': f'{self.opdsroot}?cmd=Series{userid}',
                        'kind': 'navigation',
                        'rel': 'subsection',
                    }
                )
            cmd = ("select genrename,(select count(*) as counter from genrebooks,books where genrebooks."
                   "genreid = genres.genreid and books.status='Open' and books.bookid=genrebooks.bookid) "
                   "as cnt from genres where cnt > 0")
            # cmd = "select distinct BookGenre from books where Status='Open' and BookGenre != ''
            # and BookGenre !='Unknown'"
            res = db.select(cmd)
            if res and len(res) > 0:
                entries.append(
                    {
                        'title': f'Genres ({len(res)})',
                        'id': 'Genres',
                        'updated': now(),
                        'content': 'Genres',
                        'href': f'{self.opdsroot}?cmd=Genres{userid}',
                        'kind': 'acquisition',
                        'rel': 'subsection',
                    }
                )

            res = db.match("SELECT count(*) as counter from magazines WHERE LastAcquired != ''")
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Magazines ({res["counter"]})',
                        'id': 'Magazines',
                        'updated': now(),
                        'content': 'List of Magazines',
                        'href': f'{self.opdsroot}?cmd=Magazines{userid}',
                        'kind': 'navigation',
                        'rel': 'subsection',
                    }
                )

            res = db.match("SELECT count(*) as counter from comics WHERE LastAcquired != ''")
            if res['counter'] > 0:
                entries.append(
                    {
                        'title': f'Comics ({res["counter"]})',
                        'id': 'Comics',
                        'updated': now(),
                        'content': 'List of Comics',
                        'href': f'{self.opdsroot}?cmd=Comics{userid}',
                        'kind': 'navigation',
                        'rel': 'subsection',
                    }
                )
        finally:
            db.close()

        feed['links'] = links
        feed['entries'] = entries
        self.data = feed
        return

    def _genres(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'Aldiko' in self.reader:  # Aldiko doesn't paginate long lists
            limit = 0

        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': 'LazyLibrarian OPDS - Genres', 'id': 'Genres', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=Genres{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchgenres.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Genre'))

        cmd = ("select genrename,(select count(*) as counter from genrebooks,books where "
               "genrebooks.genreid = genres.genreid and books.status='Open' and "
               "books.bookid=genrebooks.bookid) as cnt from genres where cnt > 0")
        if 'query' in kwargs:
            cmd += f" and instr(genrename, '{kwargs['query']}') > 0"
        cmd += " order by cnt DESC,genrename ASC"
        db = database.DBConnection()
        try:
            results = db.select(cmd)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for genre in page:
            totalbooks = genre['cnt']
            name = make_unicode(genre['genrename'])
            entry = {
                    'title': escape(f"{name} ({totalbooks})"),
                    'id': escape(f'genre:{genre["genrename"]}'),
                    'updated': now(),
                    'content': escape(f'{name} ({totalbooks})'),
                    'href': f'{self.opdsroot}?cmd=Genre&amp;genre={quote_plus(genre["genrename"])}{userid}',
                    'author': escape(f'{name}'),
                    'kind': 'navigation',
                    'rel': 'subsection',
                }

            entries.append(entry)

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Genres&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Genres&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'genre')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _genre(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        if 'genre' not in kwargs:
            self.data = self._error_with_message('No Genre Provided')
            return
        links = []
        entries = []

        db = database.DBConnection()
        try:
            cmd = ("SELECT BookName,BookDate,BookAdded,BookDesc,BookImg,BookFile,AudioFile,books.BookID "
                   "from genrebooks,genres,books WHERE (books.Status='Open' or books.AudioStatus='Open') "
                   "and books.Bookid=genrebooks.BookID AND genrebooks.genreid=genres.genreid and"
                   " genrename=?order by BookName")
            results = db.select(cmd, (kwargs['genre'],))
            if not len(results):
                self.data = self._error_with_message(f"No results for Genre \"{kwargs['genre']}\"")
                return

            if limit:
                page = results[index:(index + limit)]
            else:
                page = results
                limit = len(page)
            for book in page:
                mimetype = None
                rel = 'file'
                if book['BookFile']:
                    mimetype = self.multi_link(book['BookFile'], book['BookID'])
                    if mimetype:
                        rel = 'multi'
                    else:
                        mimetype = mime_type(book['BookFile'])

                elif book['AudioFile']:
                    mimetype = mime_type(book['AudioFile'])
                if mimetype:
                    cmd = ("SELECT AuthorName from authors,books WHERE authors.authorid = books.authorid "
                           "and books.bookid=?")
                    res = db.match(cmd, (book['BookID'],))
                    author = res['AuthorName']
                    entry = {'title': escape(f'{book["BookName"]}'),
                             'id': escape(f'book:{book["BookID"]}'),
                             'updated': opdstime(book['BookAdded']),
                             'href': f'{self.opdsroot}?cmd=Serve&amp;bookid={book["BookID"]}{userid}',
                             'kind': 'acquisition',
                             'rel': rel,
                             'author': escape(f"{author}"),
                             'type': mimetype}

                    if CONFIG.get_bool('OPDS_METAINFO'):
                        entry['image'] = self.searchroot + '/' + book['BookImg']
                        entry['thumbnail'] = entry['image']
                        entry['content'] = escape(f'{book["BookName"]} {book["BookDesc"]}')
                    else:
                        entry['content'] = escape(f'{book["BookName"]} {book["BookAdded"]}')
                    entries.append(entry)
        finally:
            db.close()

        feed = {'title': f'LazyLibrarian OPDS - Genre {escape(kwargs["genre"])}',
                'id': f'genre:{escape(kwargs["genre"])}', 'updated': now()}
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=Genres{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchbooks.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Books'))

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Genre&amp;genre={quote_plus(kwargs["genre"])}&amp;index='
                             f'{index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Genre&amp;genre={quote_plus(kwargs["genre"])}&amp;index='
                             f'{index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {escape(kwargs['genre'])} {plural(len(entries), 'book')}, "
            f"{index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _eauthors(self, **kwargs):
        # select authorname,authors.authorid,(select count(*) from books
        # where books.authorid=authors.authorid and books.status='Open')
        # as books from books,authors where books > 1 group by authorname;
        index = 0
        limit = self.PAGE_SIZE
        if 'Aldiko' in self.reader:  # Aldiko doesn't paginate long lists
            limit = 0

        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': 'LazyLibrarian OPDS - Ebook Authors', 'id': 'EAuthors', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=EAuthors{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchauthors.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Authors'))
        cmd = "SELECT AuthorName,AuthorID,HaveEBooks,TotalBooks,Updated,AuthorImg from Authors WHERE "
        if 'query' in kwargs:
            cmd += f"instr(AuthorName, '{kwargs['query']}') > 0 AND "
        cmd += "HaveEBooks > 0 order by AuthorName"
        db = database.DBConnection()
        try:
            results = db.select(cmd)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for author in page:
            totalbooks = check_int(author['TotalBooks'], 0)
            havebooks = check_int(author['HaveEBooks'], 0)
            lastupdated = datetime.datetime.fromtimestamp(author['Updated']).strftime("%Y-%m-%d")
            name = make_unicode(author['AuthorName'])
            entry = {
                    'title': escape(f'{name} ({havebooks}/{totalbooks})'),
                    'id': escape(f'author:{author["AuthorID"]}'),
                    'updated': opdstime(lastupdated),
                    'content': escape(f'{name} ({havebooks})'),
                    'href': f'{self.opdsroot}?cmd=EAuthor&amp;authorid={author["AuthorID"]}{userid}',
                    'author': escape(f'{name}'),
                    'kind': 'navigation',
                    'rel': 'subsection',
                }

            if CONFIG.get_bool('OPDS_METAINFO'):
                entry['thumbnail'] = '/' + author['AuthorImg']
            entries.append(entry)

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=EAuthors&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=EAuthors&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'author')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _aauthors(self, **kwargs):
        # select authorname,authors.authorid,(select count(*) from books
        # where books.authorid=authors.authorid and books.status='Open')
        # as books from books,authors where books > 1 group by authorname;
        index = 0
        limit = self.PAGE_SIZE
        if 'Aldiko' in self.reader:  # Aldiko doesn't paginate long lists
            limit = 0

        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': 'LazyLibrarian OPDS - Audiobook Authors', 'id': 'AAuthors', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=AAuthors{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchauthors.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Authors'))
        cmd = "SELECT AuthorName,AuthorID,HaveAudioBooks,TotalBooks,Updated,AuthorImg from Authors WHERE "
        if 'query' in kwargs:
            cmd += f"instr(AuthorName, '{kwargs['query']}') > 0 AND "
        cmd += "HaveAudioBooks > 0 order by AuthorName"
        db = database.DBConnection()
        try:
            results = db.select(cmd)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for author in page:
            totalbooks = check_int(author['TotalBooks'], 0)
            havebooks = check_int(author['HaveAudioBooks'], 0)
            lastupdated = datetime.datetime.fromtimestamp(author['Updated']).strftime("%Y-%m-%d")
            name = make_unicode(author['AuthorName'])
            entry = {
                    'title': escape(f'{name} ({havebooks}/{totalbooks})'),
                    'id': escape(f'author:{author["AuthorID"]}'),
                    'updated': opdstime(lastupdated),
                    'content': escape(f'{name} ({havebooks})'),
                    'href': f'{self.opdsroot}?cmd=AAuthor&amp;authorid={author["AuthorID"]}{userid}',
                    'author': escape(f'{name}'),
                    'kind': 'navigation',
                    'rel': 'subsection',
                }

            if CONFIG.get_bool('OPDS_METAINFO'):
                entry['thumbnail'] = '/' + author['AuthorImg']
            entries.append(entry)

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=AAuthors&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=AAuthors&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'author')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _comics(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'Aldiko' in self.reader:
            limit = 0

        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': 'LazyLibrarian OPDS - Comics', 'id': 'Comics', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=Comics{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchcomics.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Comics'))
        cmd = ("select comics.*,(select count(*) as counter from comicissues where "
               "comics.ComicID = comicissues.ComicID) as Iss_Cnt from comics ")
        args = ()
        if 'query' in kwargs:
            cmd += "WHERE instr(comics.title, ?) > 0 "
            args = (kwargs['query'], )
        cmd += "order by comics.title"
        db = database.DBConnection()
        try:
            if args:
                results = db.select(cmd, args)
            else:
                results = db.select(cmd)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for mag in page:
            if mag['Iss_Cnt'] > 0:
                title = make_unicode(mag['Title'])
                entry = {
                    'title': escape(f'{title} {mag["Start"]} ({mag["Iss_Cnt"]})'),
                    'id': escape(f'comic:{mag["ComicID"]}'),
                    'updated': opdstime(mag['LastAcquired']),
                    'content': escape(f'{title}'),
                    'href': f'{self.opdsroot}?cmd=Comic&amp;magid={mag["ComicID"]}{userid}',
                    'kind': 'navigation',
                    'rel': 'subsection',
                }

                # if lazylibrarian.CONFIG.get_bool('OPDS_METAINFO'):
                #     res = cache_img(ImageType.MAG, md5_utf8(mag['LatestCover']), mag['LatestCover'], refresh=True)
                #     entry['thumbnail'] = '/' + res[0]
                entries.append(entry)

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Comics&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Comics&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'comic')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _magazines(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'Aldiko' in self.reader:
            limit = 0

        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': 'LazyLibrarian OPDS - Magazines', 'id': 'Magazines', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=Magazines{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchmagazines.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Magazines'))
        cmd = ("select magazines.*,(select count(*) as counter from issues where magazines.title = issues.title) "
               "as Iss_Cnt from magazines ")
        args = ()
        if 'query' in kwargs:
            cmd += "WHERE instr(magazines.title, ?) > 0 "
            args = (kwargs['query'], )
        cmd += "order by magazines.title"
        db = database.DBConnection()
        try:
            if args:
                results = db.select(cmd, args)
            else:
                results = db.select(cmd)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for mag in page:
            if mag['Iss_Cnt'] > 0:
                title = make_unicode(mag['Title'])
                entry = {
                    'title': escape(f'{title} ({mag["Iss_Cnt"]})'),
                    'id': escape(f'magazine:{title}'),
                    'updated': opdstime(mag['LastAcquired']),
                    'content': escape(f'{title}'),
                    'href': f'{self.opdsroot}?cmd=Magazine&amp;magid={quote_plus(title)}{userid}',
                    'kind': 'navigation',
                    'rel': 'subsection',
                }

                # if lazylibrarian.CONFIG.get_bool('OPDS_METAINFO'):
                #     res = cache_img(ImageType.MAG, md5_utf8(mag['LatestCover']), mag['LatestCover'], refresh=True)
                #     entry['thumbnail'] = '/' + res[0]
                entries.append(entry)

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Magazines&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Magazines&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'magazine')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _series(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'Aldiko' in self.reader:
            limit = 0

        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': 'LazyLibrarian OPDS - Series', 'id': 'Series', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=Series{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchseries.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Series'))
        cmd = "SELECT SeriesName,SeriesID,Have,Total from Series WHERE CAST(Have AS INTEGER) > 0 "
        if 'query' in kwargs:
            cmd += f"AND instr(SeriesName, '{kwargs['query']}') > 0 "
        cmd += "order by SeriesName"
        db = database.DBConnection()
        try:
            results = db.select(cmd)
            if limit:
                page = results[index:(index + limit)]
            else:
                page = results
                limit = len(page)
            for series in page:
                cmd = ("SELECT books.BookID,SeriesNum from books,member where SeriesID=? and "
                       "books.bookid = member.bookid order by CAST(SeriesNum AS INTEGER)")
                firstbook = db.match(cmd, (series['SeriesID'],))
                if firstbook:
                    cmd = ("SELECT AuthorName from authors,books WHERE authors.authorid = books.authorid "
                           "AND books.bookid=?")
                    res = db.match(cmd, (firstbook['BookID'],))
                    author = res['AuthorName']
                else:
                    author = 'Unknown'
                totalbooks = check_int(series['Total'], 0)
                havebooks = check_int(series['Have'], 0)
                sername = make_unicode(series['SeriesName'])
                entries.append(
                    {
                        'title': escape(f'{sername} ({havebooks}/{totalbooks}) {author}'),
                        'id': escape(f'series:{series["SeriesID"]}'),
                        'updated': now(),
                        'content': escape(f'{sername} ({havebooks})'),
                        'href': f'{self.opdsroot}?cmd=Members&amp;seriesid={series["SeriesID"]}{userid}',
                        'kind': 'navigation',
                        'rel': 'subsection',
                    }
                )
        finally:
            db.close()

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Series&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Series&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(f"Returning {len(entries)} series, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _comic(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        if 'magid' not in kwargs:
            self.data = self._error_with_message('No ComicID Provided')
            return
        links = []
        entries = []
        title = ''
        db = database.DBConnection()
        try:
            comic = db.match("SELECT Title from comics WHERE ComicID=?", (kwargs['magid'],))
            if comic:
                title = make_unicode(comic['Title'])
            cmd = ("SELECT IssueID,IssueAcquired,IssueFile from comicissues WHERE ComicID=? "
                   "order by IssueID DESC")
            results = db.select(cmd, (kwargs['magid'],))
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for issue in page:
            issueid = f'{kwargs["magid"]}_{issue["IssueID"]}'
            entry = {'title': escape(f'{title} ({issue["IssueID"]})'),
                     'id': f'issue:{issueid}',
                     'updated': opdstime(issue['IssueAcquired']),
                     'content': escape(f'{title} - {issue["IssueID"]}'),
                     'href': f'{self.opdsroot}?cmd=Serve&amp;comicissueid={issueid}{userid}',
                     'kind': 'acquisition',
                     'rel': 'file',
                     'type': mime_type(issue['IssueFile'])}
            if CONFIG.get_bool('OPDS_METAINFO'):
                fname = splitext(issue['IssueFile'])[0]
                res = cache_img(ImageType.COMIC, issueid, fname + '.jpg')
                entry['image'] = self.searchroot + '/' + res[0]
                entry['thumbnail'] = entry['image']
            entries.append(entry)

        feed = {}
        title = f'{escape(title)} ({len(entries)})'
        feed['title'] = f'LazyLibrarian OPDS - {title}'
        feed['id'] = f'comic:{kwargs["magid"]}'
        feed['updated'] = now()
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=Comic&amp;magid={kwargs["magid"]}{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Comic&amp;magid={kwargs["magid"]}&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Comic&amp;magid={kwargs["magid"]}&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'issue')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _magazine(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        if 'magid' not in kwargs:
            self.data = self._error_with_message('No Magazine Provided')
            return
        links = []
        entries = []
        title = ''
        cmd = ("SELECT Title,IssueID,IssueDate,IssueAcquired,IssueFile from issues WHERE Title=? "
               "order by IssueDate DESC")
        db = database.DBConnection()
        try:
            results = db.select(cmd, (kwargs['magid'], ))
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for issue in page:
            title = make_unicode(issue['Title'])
            entry = {'title': escape(f'{title} ({issue["IssueDate"]})'),
                     'id': escape(f'issue:{issue["IssueID"]}'),
                     'updated': opdstime(issue['IssueAcquired']),
                     'content': escape(f'{title} - {issue["IssueDate"]}'),
                     'href': f'{self.opdsroot}?cmd=Serve&amp;issueid={quote_plus(issue["IssueID"])}{userid}',
                     'kind': 'acquisition',
                     'rel': 'file',
                     'type': mime_type(issue['IssueFile'])}
            if CONFIG.get_bool('OPDS_METAINFO'):
                fname = splitext(issue['IssueFile'])[0]
                res = cache_img(ImageType.MAG, issue['IssueID'], fname + '.jpg')
                entry['image'] = self.searchroot + '/' + res[0]
                entry['thumbnail'] = entry['image']
            entries.append(entry)

        feed = {}
        title = f'{escape(title)} ({len(entries)})'
        feed['title'] = f'LazyLibrarian OPDS - {title}'
        feed['id'] = f'magazine:{escape(kwargs["magid"])}'
        feed['updated'] = now()
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=Magazine&amp;magid={quote_plus(kwargs["magid"])}{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Magazine&amp;magid={quote_plus(kwargs["magid"])}&amp;index='
                             f'{index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Magazine&amp;magid={quote_plus(kwargs["magid"])}&amp;index='
                             f'{index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'issue')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _eauthor(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        if 'authorid' not in kwargs:
            self.data = self._error_with_message('No Author Provided')
            return
        links = []
        entries = []
        links.append(getlink(href=f'{self.searchroot}/opensearchbooks.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Books'))
        db = database.DBConnection()
        try:
            author = db.match("SELECT AuthorName from authors WHERE AuthorID=?", (kwargs['authorid'],))
            author = make_unicode(author['AuthorName'])
            cmd = "SELECT BookName,BookDate,BookID,BookAdded,BookDesc,BookImg,BookFile from books WHERE "
            args = ()
            if 'query' in kwargs:
                cmd += "instr(BookName, ? > 0 AND "
                args += (kwargs['query'], )
            cmd += "Status='Open' and AuthorID=? order by BookDate DESC"
            args += (kwargs['authorid'], )
            results = db.select(cmd, args)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for book in page:
            mimetype = None
            rel = 'file'
            if book['BookFile']:
                mimetype = self.multi_link(book['BookFile'], book['BookID'])
                if mimetype:
                    rel = 'multi'
                else:
                    mimetype = mime_type(book['BookFile'])

            if mimetype:
                if book['BookDate'] and book['BookDate'] != '0000':
                    disptitle = escape(f'{book["BookName"]} ({book["BookDate"]})')
                else:
                    disptitle = escape(f'{book["BookName"]}')
                entry = {'title': disptitle,
                         'id': escape(f'book:{book["BookID"]}'),
                         'updated': opdstime(book['BookAdded']),
                         'href': f'{self.opdsroot}?cmd=Serve&amp;bookid={book["BookID"]}{userid}',
                         'kind': 'acquisition',
                         'rel': rel,
                         'type': mimetype}
                if CONFIG.get_bool('OPDS_METAINFO'):
                    entry['image'] = self.searchroot + '/' + book['BookImg']
                    entry['thumbnail'] = entry['image']
                    entry['content'] = escape(f'{book["BookName"]} - {book["BookDesc"]}')
                    entry['author'] = escape(f'{author}')
                else:
                    entry['content'] = escape(f'{book["BookName"]} ({book["BookAdded"]})')
                entries.append(entry)

        feed = {}
        authorname = f'{escape(author)} ({len(entries)})'
        feed['title'] = f'LazyLibrarian OPDS - {authorname}'
        feed['id'] = f'author:{escape(kwargs["authorid"])}'
        feed['updated'] = now()
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=EAuthors{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=EAuthor&amp;authorid={quote_plus(kwargs["authorid"])}&amp;index='
                             f'{index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=EAuthor&amp;authorid={quote_plus(kwargs["authorid"])}&amp;index='
                             f'{index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))
        feed['links'] = links
        feed['entries'] = entries
        self.data = feed
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'book')}, {index + 1} to {fin} from {len(results)}")
        return

    def _aauthor(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        if 'authorid' not in kwargs:
            self.data = self._error_with_message('No Author Provided')
            return
        links = []
        entries = []
        links.append(getlink(href=f'{self.searchroot}/opensearchbooks.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Books'))
        db = database.DBConnection()
        try:
            author = db.match("SELECT AuthorName from authors WHERE AuthorID=?", (kwargs['authorid'],))
            author = make_unicode(author['AuthorName'])
            cmd = "SELECT BookName,BookDate,BookID,BookAdded,BookDesc,BookImg,AudioFile from books WHERE "
            args = ()
            if 'query' in kwargs:
                cmd += "instr(BookName, ?) > 0 AND "
                args += (kwargs['query'], )
            cmd += "AudioStatus='Open' and AuthorID=? order by BookDate DESC"
            args += (kwargs['authorid'], )
            results = db.select(cmd, args)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for book in page:
            mimetype = None
            rel = 'file'
            if book['AudioFile']:
                mimetype = mime_type(book['AudioFile'])

            if mimetype:
                if book['BookDate'] and book['BookDate'] != '0000':
                    disptitle = escape(f'{book["BookName"]} ({book["BookDate"]})')
                else:
                    disptitle = escape(f'{book["BookName"]}')
                entry = {'title': disptitle,
                         'id': escape(f'book:{book["BookID"]}'),
                         'updated': opdstime(book['BookAdded']),
                         'href': f'{self.opdsroot}?cmd=Serve&amp;audioid={book["BookID"]}{userid}',
                         'kind': 'acquisition',
                         'rel': rel,
                         'type': mimetype}
                if CONFIG.get_bool('OPDS_METAINFO'):
                    entry['image'] = self.searchroot + '/' + book['BookImg']
                    entry['thumbnail'] = entry['image']
                    entry['content'] = escape(f'{book["BookName"]} - {book["BookDesc"]}')
                    entry['author'] = escape(f'{author}')
                else:
                    entry['content'] = escape(f'{book["BookName"]} ({book["BookAdded"]})')
                entries.append(entry)

        feed = {}
        authorname = f'{escape(author)} ({len(entries)})'
        feed['title'] = f'LazyLibrarian OPDS - {authorname}'
        feed['id'] = f'author:{escape(kwargs["authorid"])}'
        feed['updated'] = now()
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=AAuthors{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=AAuthor&amp;authorid={quote_plus(kwargs["authorid"])}&amp;index='
                             f'{index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=AAuthor&amp;authorid={quote_plus(kwargs["authorid"])}&amp;index='
                             f'{index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))
        feed['links'] = links
        feed['entries'] = entries
        self.data = feed
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'book')}, {index + 1} to {fin} from {len(results)}")
        return

    def _members(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        if 'seriesid' not in kwargs:
            self.data = self._error_with_message('No Series Provided')
            return
        links = []
        entries = []
        db = database.DBConnection()
        try:
            series = db.match("SELECT SeriesName from Series WHERE SeriesID=?", (kwargs['seriesid'],))
            cmd = ("SELECT BookName,BookDate,BookAdded,BookDesc,BookImg,BookFile,AudioFile,books.BookID,SeriesNum "
                   "from books,member where (Status='Open' or AudioStatus='Open') and SeriesID=? and "
                   "books.bookid = member.bookid order by CAST(SeriesNum AS INTEGER)")
            results = db.select(cmd, (kwargs['seriesid'],))
            cmd = "SELECT AuthorName from authors,books WHERE authors.authorid = books.authorid and books.bookid=?"
            res = db.match(cmd, (results[0]['BookID'],))
        finally:
            db.close()
        author = res['AuthorName']
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for book in page:
            mimetype = None
            rel = 'file'
            if book['BookFile']:
                mimetype = self.multi_link(book['BookFile'], book['BookID'])
                if mimetype:
                    rel = 'multi'
                else:
                    mimetype = mime_type(book['BookFile'])

            elif book['AudioFile']:
                mimetype = mime_type(book['AudioFile'])
            if mimetype:
                if book['SeriesNum']:
                    snum = f' ({book["SeriesNum"]})'
                else:
                    snum = ''
                entry = {'title': escape(f'{book["BookName"]}{snum}'),
                         'id': escape(f'book:{book["BookID"]}'),
                         'updated': opdstime(book['BookAdded']),
                         'href': f'{self.opdsroot}?cmd=Serve&amp;bookid={book["BookID"]}{userid}',
                         'kind': 'acquisition',
                         'rel': rel,
                         'author': escape(f"{author}"),
                         'type': mimetype}

                if CONFIG.get_bool('OPDS_METAINFO'):
                    entry['image'] = self.searchroot + '/' + book['BookImg']
                    entry['thumbnail'] = entry['image']
                    entry['content'] = escape(
                        f'{book["BookName"]} ({series["SeriesName"]} {book["SeriesNum"]}) {book["BookDesc"]}')
                else:
                    entry['content'] = escape(
                        f'{book["BookName"]} ({series["SeriesName"]} {book["SeriesNum"]}) {book["BookAdded"]}')
                entries.append(entry)

        feed = {}
        seriesname = f'{escape(series["SeriesName"])} ({len(entries)}) {author}'
        feed['title'] = f'LazyLibrarian OPDS - {seriesname}'
        feed['id'] = f'series:{escape(kwargs["seriesid"])}'
        feed['updated'] = now()
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=Series{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Members&amp;seriesid={kwargs["seriesid"]}&amp;index='
                             f'{index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=Members&amp;seriesid={kwargs["seriesid"]}&amp;index='
                             f'{index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'book')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _recentmags(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': 'LazyLibrarian OPDS - Recent Magazines', 'id': 'Recent Magazines', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=RecentMags{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchmagazines.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Magazines'))
        cmd = "select Title,IssueID,IssueAcquired,IssueDate,IssueFile,Cover from issues "
        cmd += "where IssueFile != '' "
        args = ()
        if 'query' in kwargs:
            cmd += "AND instr(Title, ?) > 0 "
            args += (kwargs['query'], )
        cmd += "order by IssueAcquired DESC"
        db = database.DBConnection()
        try:
            if args:
                results = db.select(cmd, args)
            else:
                results = db.select(cmd)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for mag in page:
            title = make_unicode(mag['Title'])
            entry = {'title': escape(f'{mag["IssueDate"]}'),
                     'id': escape(f'issue:{mag["IssueID"]}'),
                     'updated': opdstime(mag['IssueAcquired']),
                     'content': escape(f'{title} - {mag["IssueDate"]}'),
                     'href': f'{self.opdsroot}?cmd=Serve&amp;issueid={quote_plus(mag["IssueID"])}',
                     'kind': 'acquisition',
                     'rel': 'file',
                     'author': escape(title),
                     'type': mime_type(mag['IssueFile'])}
            if CONFIG.get_bool('OPDS_METAINFO'):
                entry['image'] = self.searchroot + '/' + mag['Cover']
                entry['thumbnail'] = entry['image']
            entries.append(entry)

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=RecentMags&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=RecentMags&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'issue')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _recentcomics(self, **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': 'LazyLibrarian OPDS - Recent Comics', 'id': 'Recent Comics', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd=RecentComics{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchcomics.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Comics'))
        cmd = "select comics.ComicID,Title,IssueID,IssueAcquired,IssueFile,Start from comics,comicissues "
        cmd += "where comics.ComicID = comicissues.ComicID and IssueFile != '' "
        args = ()
        if 'query' in kwargs:
            cmd += "AND instr(Title, ?) > 0 "
            args += (kwargs['query'], )
        cmd += "order by IssueAcquired DESC"
        db = database.DBConnection()
        try:
            if args:
                results = db.select(cmd, args)
            else:
                results = db.select(cmd)
        finally:
            db.close()
        if limit:
            page = results[index:(index + limit)]
        else:
            page = results
            limit = len(page)
        for mag in page:
            title = make_unicode(mag['Title'])
            issueid = f'{mag["ComicID"]}_{mag["IssueID"]}'
            entry = {'title': escape(f'{title} {mag["Start"]}'),
                     'id': escape(f'issue:{issueid}'),
                     'updated': opdstime(mag['IssueAcquired']),
                     'content': escape(f'{title} - {mag["IssueID"]}'),
                     'href': f'{self.opdsroot}?cmd=Serve&amp;comicissueid={issueid}',
                     'kind': 'acquisition',
                     'rel': 'file',
                     'author': escape(title),
                     'type': mime_type(mag['IssueFile'])}
            if CONFIG.get_bool('OPDS_METAINFO'):
                fname = splitext(mag['IssueFile'])[0]
                res = cache_img(ImageType.COMIC, issueid, fname + '.jpg')
                entry['image'] = self.searchroot + '/' + res[0]
                entry['thumbnail'] = entry['image']
            entries.append(entry)

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=RecentComics&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd=RecentComics&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'issue')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _readbooks(self, sorder='Read', **kwargs):
        if 'user' not in kwargs:
            sorder = ''
        return self._books(sorder, **kwargs)

    def _toreadbooks(self, sorder='ToRead', **kwargs):
        if 'user' not in kwargs:
            sorder = ''
        return self._books(sorder, **kwargs)

    def _ratedbooks(self, sorder='Rated', **kwargs):
        return self._books(sorder, **kwargs)

    def _recentbooks(self, sorder='Recent', **kwargs):
        return self._books(sorder, **kwargs)

    def _books(self, sorder='Recent', **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': f'LazyLibrarian OPDS - {sorder} Books', 'id': f'{sorder} Books', 'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd={sorder}Books{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchbooks.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Books'))
        cmd = ("select BookName,BookID,BookLibrary,BookDate,BookImg,BookDesc,BookRate,BookAdded,BookFile,AuthorID "
               "from books where Status='Open' ")
        args = ()
        if 'query' in kwargs:
            cmd += "AND instr(BookName, ?) > 0 "
            args += (kwargs['query'], )
        if sorder == 'Recent':
            cmd += "order by BookLibrary DESC, BookName ASC"
        if sorder == 'Rated':
            cmd += "and CAST(BookRate AS INTEGER) > 0 order by BookRate DESC, BookDate DESC"

        db = database.DBConnection()
        try:
            if args:
                results = db.select(cmd, args)
            else:
                results = db.select(cmd)
            self.dlcommslogger.debug(f"Initial select found {len(results)}")

            readfilter = None
            if sorder == 'Read' and 'user' in kwargs:
                readfilter = get_readinglist("HaveRead", kwargs['user'])
            elif sorder == 'ToRead' and 'user' in kwargs:
                readfilter = get_readinglist("ToRead", kwargs['user'])
            if readfilter is not None:
                self.dlcommslogger.debug(f"Filter length {len(readfilter)}")
                filtered = []
                for res in results:
                    if res['BookID'] in readfilter:
                        filtered.append(res)
                results = filtered
                self.dlcommslogger.debug(f"Filter matches {len(results)}")

            if limit:
                page = results[index:(index + limit)]
            else:
                page = results
                limit = len(page)
            for book in page:
                mimetype = None
                rel = 'file'
                if book['BookFile']:
                    mimetype = self.multi_link(book['BookFile'], book['BookID'])
                    if mimetype:
                        rel = 'multi'
                    else:
                        mimetype = mime_type(book['BookFile'])

                elif book['AudioFile']:
                    mimetype = mime_type(book['AudioFile'])
                if mimetype:
                    title = make_unicode(book['BookName'])
                    if sorder == 'Rated':
                        dispname = escape(f"{title} ({book['BookRate']})")
                    else:
                        dispname = escape(title)
                    entry = {'title': dispname,
                             'id': escape(f'book:{book["BookID"]}'),
                             'updated': opdstime(book['BookLibrary']),
                             'href': f'{self.opdsroot}?cmd=Serve&amp;bookid={quote_plus(book["BookID"])}{userid}',
                             'kind': 'acquisition',
                             'rel': rel,
                             'type': mimetype}

                    if CONFIG.get_bool('OPDS_METAINFO'):
                        auth = db.match("SELECT AuthorName from authors WHERE AuthorID=?",
                                        (book['AuthorID'],))
                        if auth:
                            author = make_unicode(auth['AuthorName'])
                            entry['image'] = self.searchroot + '/' + book['BookImg']
                            entry['thumbnail'] = entry['image']
                            entry['content'] = escape(f'{title} - {book["BookDesc"]}')
                            entry['author'] = escape(f'{author}')
                    else:
                        entry['content'] = escape(f'{title} ({book["BookAdded"]})')
                    entries.append(entry)

                """
                    <link type="application/epub+zip" rel="http://opds-spec.org/acquisition"
                    title="EPUB (no images)" length="18552" href="//www.gutenberg.org/ebooks/57490.epub.noimages"/>
                    <link type="application/x-mobipocket-ebook" rel="http://opds-spec.org/acquisition"
                    title="Kindle (no images)" length="110360" href="//www.gutenberg.org/ebooks/57490.kindle.noimages"/>
                """
        finally:
            db.close()

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd={sorder}Books&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd={sorder}Books&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'book')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _ratedaudio(self, sorder='Rated', **kwargs):
        return self._audio(sorder, **kwargs)

    def _recentaudio(self, sorder='Recent', **kwargs):
        return self._audio(sorder, **kwargs)

    def _audio(self, sorder='Recent', **kwargs):
        index = 0
        limit = self.PAGE_SIZE
        if 'index' in kwargs:
            index = check_int(kwargs['index'], 0)
        userid = ''
        if 'user' in kwargs:
            userid = f'&amp;user={kwargs["user"]}'

        feed = {'title': f'LazyLibrarian OPDS - {sorder} AudioBooks', 'id': f'{sorder} AudioBooks',
                'updated': now()}
        links = []
        entries = []
        links.append(getlink(href=self.opdsroot, ftype='application/atom+xml; profile=opds-catalog; kind=navigation',
                             rel='start', title='Home'))
        links.append(getlink(href=f'{self.opdsroot}?cmd={sorder}Audio{userid}',
                             ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='self'))
        links.append(getlink(href=f'{self.searchroot}/opensearchbooks.xml',
                             ftype='application/opensearchdescription+xml', rel='search', title='Search Books'))

        cmd = ("select BookName,BookID,AudioLibrary,BookDate,BookImg,BookDesc,BookRate,BookAdded,AuthorID"
               " from books WHERE ")
        args = ()
        if 'query' in kwargs:
            cmd += "instr(BookName, ?) > 0 AND "
            args += (kwargs['query'], )
        cmd += "AudioStatus='Open'"
        if sorder == 'Recent':
            cmd += " order by AudioLibrary DESC, BookName ASC"
        if sorder == 'Rated':
            cmd += " order by BookRate DESC, BookDate DESC"
        db = database.DBConnection()
        try:
            if args:
                results = db.select(cmd, args)
            else:
                results = db.select(cmd)
            if limit:
                page = results[index:(index + limit)]
            else:
                page = results
                limit = len(page)
            for book in page:
                title = make_unicode(book['BookName'])
                if sorder == 'Rated':
                    dispname = escape(f"{title} ({book['BookRate']})")
                else:
                    dispname = escape(title)
                entry = {'title': dispname,
                         'id': escape(f'audio:{book["BookID"]}'),
                         'updated': opdstime(book['AudioLibrary']),
                         'href': f'{self.opdsroot}?cmd=Serve&amp;audioid={quote_plus(book["BookID"])}{userid}',
                         'kind': 'acquisition',
                         'rel': 'file',
                         'type': mime_type("we_send.zip")}
                if CONFIG.get_bool('OPDS_METAINFO'):
                    auth = db.match("SELECT AuthorName from authors WHERE AuthorID=?", (book['AuthorID'],))
                    if auth:
                        author = make_unicode(auth['AuthorName'])
                        entry['image'] = self.searchroot + '/' + book['BookImg']
                        entry['thumbnail'] = entry['image']
                        entry['content'] = escape(f'{title} - {book["BookDesc"]}')
                        entry['author'] = escape(f'{author}')
                else:
                    entry['content'] = escape(f'{title} ({book["BookAdded"]})')
                entries.append(entry)
        finally:
            db.close()

        if len(results) > (index + limit):
            links.append(
                getlink(href=f'{self.opdsroot}?cmd={sorder}Audio&amp;index={index + limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='next'))
        if index >= limit:
            links.append(
                getlink(href=f'{self.opdsroot}?cmd={sorder}Audio&amp;index={index - limit}{userid}',
                        ftype='application/atom+xml; profile=opds-catalog; kind=navigation', rel='previous'))

        feed['links'] = links
        feed['entries'] = entries
        fin = index + limit
        if fin > len(results):
            fin = len(results)
        self.logger.debug(
            f"Returning {len(entries)} {plural(len(entries), 'audiobook')}, {index + 1} to {fin} from {len(results)}")
        self.data = feed
        return

    def _serve(self, **kwargs):
        if 'bookid' in kwargs:
            fmt = kwargs.get('fmt', '')
            myid = kwargs['bookid']
            db = database.DBConnection()
            try:
                res = db.match('SELECT BookFile,BookName from books where bookid=?', (myid,))
            finally:
                db.close()
            bookfile = res['BookFile']
            if fmt:
                bookfile = splitext(bookfile)[0] + '.' + fmt
            self.filepath = bookfile
            self.filename = os.path.split(bookfile)[1]
            return
        if 'issueid' in kwargs:
            myid = kwargs['issueid']
            db = database.DBConnection()
            try:
                res = db.match('SELECT IssueFile from issues where issueid=?', (myid,))
            finally:
                db.close()
            self.filepath = res['IssueFile']
            self.filename = os.path.split(res['IssueFile'])[1]
            return
        if 'comicissueid' in kwargs:
            myid = kwargs['comicissueid']
            try:
                comicid, issueid = myid.split('_')
            except ValueError:
                return
            db = database.DBConnection()
            try:
                res = db.match('SELECT IssueFile from comicissues where comicid=? and issueid=?',
                               (comicid, issueid))
            finally:
                db.close()
            self.filepath = res['IssueFile']
            self.filename = os.path.split(res['IssueFile'])[1]
            return
        if 'audioid' in kwargs:
            myid = kwargs['audioid']
            db = database.DBConnection()
            try:
                res = db.match('SELECT AudioFile,BookName from books where BookID=?', (myid,))
            finally:
                db.close()
            basefile = res['AudioFile']
            # see if we need to zip up all the audiobook parts
            if basefile and path_isfile(basefile):
                foldername = os.path.dirname(basefile)
                # is there already a zipfile
                zipped = any_file(foldername, '.zip')
                if zipped:
                    self.filepath = zipped
                    self.filename = res['BookName'] + '.zip'
                else:
                    cnt = 0
                    target = ''
                    namevars = name_vars(myid)
                    singlefile = namevars['AudioSingleFile']
                    # noinspection PyBroadException
                    try:
                        for fname in listdir(foldername):
                            if CONFIG.is_valid_booktype(fname, booktype='audio'):
                                cnt += 1
                                target = fname
                                bname, extn = splitext(fname)
                                if bname == singlefile:
                                    # found name matching the AudioSingleFile
                                    cnt = 1
                                    break
                    except Exception:
                        pass
                    if cnt == 1:
                        # only one audio file or a singlefile match, just send it
                        self.filepath = os.path.join(foldername, target)
                        self.filename = target
                    else:
                        self.filepath = zip_audio(foldername, res['BookName'], myid)
                        self.filename = res['BookName'] + '.zip'
            return


def getlink(href=None, ftype=None, rel=None, title=None):
    link = {}
    if href:
        link['href'] = href
    if ftype:
        link['type'] = ftype
    if rel:
        link['rel'] = rel
    if title:
        link['title'] = title
    return link


def escape(data):
    """Escape &, <, and > in a string of data.
    """
    # must do ampersand first
    data = data.replace("&", "&amp;")
    data = data.replace(">", "&gt;")
    data = data.replace("<", "&lt;")
    return data


def now():
    dtnow = datetime.datetime.now()
    return dtnow.strftime("%Y-%m-%dT%H:%M:%SZ")


def opdstime(datestr):
    # YYYY-MM-DDTHH:MM:SSZ
    if not datestr:
        return now()
    if len(datestr) == 10:
        return f"{datestr}{'T00:00:00Z'}"
    if len(datestr) == 19:
        return f"{datestr[:10]}T{datestr[11:]}Z"
    return now()
