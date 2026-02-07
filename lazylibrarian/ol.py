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
import time
import traceback
from urllib.parse import quote_plus

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

import lazylibrarian
from lazylibrarian import ROLE, database
from lazylibrarian.bookdict import add_bookdict_to_db, validate_bookdict, warn_about_bookdict
from lazylibrarian.bookwork import (
    delete_empty_series,
    genre_filter,
    get_gb_info,
    get_status,
    is_set_or_part,
    isbn_from_words,
    isbnlang,
    librarything_wait,
)
from lazylibrarian.cache import html_request, json_request
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import (
    check_float,
    check_int,
    date_format,
    format_author_name,
    get_list,
    is_valid_isbn,
    make_utf8bytes,
    now,
    plural,
    replace_all,
    thread_name,
    today,
    unaccented,
)
from lazylibrarian.images import cache_bookimg, get_book_cover


class OpenLibrary:
    # https://openlibrary.org/developers/api
    def __init__(self):
        self.OL_SEARCH = '/'.join([CONFIG['OL_URL'], "search.json?"])
        self.OL_AUTHOR = '/'.join([CONFIG['OL_URL'], "authors/"])
        self.OL_ISBN = '/'.join([CONFIG['OL_URL'], "isbn/"])
        self.OL_WORK = '/'.join([CONFIG['OL_URL'], "works/"])
        self.OL_BOOK = '/'.join([CONFIG['OL_URL'], "books/"])
        self.LT_NSERIES = '/'.join([CONFIG['LT_URL'], 'nseries/'])
        self.LT_SERIES = '/'.join([CONFIG['LT_URL'], 'series/'])
        self.LT_WORK = '/'.join([CONFIG['LT_URL'], "work/"])
        self.lt_cache = False
        self.logger = logging.getLogger(__name__)
        self.searchinglogger = logging.getLogger('special.searching')
        self.matchinglogger = logging.getLogger('special.matching')

    def find_results(self, searchterm=None, queue=None):
        # noinspection PyBroadException
        try:
            resultlist = []
            resultcount = 0
            api_hits = 0
            searchtitle = ''
            searchauthorname = ''
            offset = 0
            next_page = True
            loop_count = 1

            if '<ll>' in searchterm:  # special token separates title from author
                searchtitle, searchauthorname = searchterm.split('<ll>')
                searchterm = searchterm.replace('<ll>', ' ')
                searchtitle = searchtitle.split(' (')[0]  # without any series info

            self.logger.debug(f'Now searching OpenLibrary API with searchterm: {searchterm}')
            searchbytes, _ = make_utf8bytes(searchterm)
            searchbytes = searchbytes.replace(b'#', b'').replace(b'/', b'_')
            baseurl = f"{self.OL_SEARCH}q={quote_plus(searchbytes)}"
            self.searchinglogger.debug(baseurl)

            while next_page:
                url = baseurl
                if offset:
                    url += f"&offset={offset}"
                results, in_cache = json_request(url)
                if not in_cache:
                    api_hits += 1
                if results and 'numFound' in results:
                    self.logger.debug(f"Found {results['numFound']} results for searchterm, page {loop_count - 1}")
                else:
                    break

                for book in results['docs']:
                    author_name = book.get('author_name')
                    if author_name:
                        author_name = author_name[0]
                    booklink = book.get('key')
                    bookid = ''
                    if booklink:
                        bookid = booklink.split('/')[-1]
                        booklink = '/'.join([CONFIG['OL_URL'], booklink])
                    authorid = book.get('author_key')
                    if authorid:
                        authorid = authorid[0]
                    book_title = book.get('title')
                    booksub = ''
                    bookisbn = book.get('isbn')
                    if bookisbn:
                        bookisbn = bookisbn[0]
                    bookpub = book.get('first_publish_year')
                    booklang = book.get('lang')
                    bookdate = ''
                    if isinstance(booklang, list):
                        booklang = ', '.join(booklang)
                    bookrate = 0
                    bookrate_count = 0
                    cover = book.get('cover_i')
                    if not cover:
                        cover = 'images/nocover.png'
                    else:
                        cover = f'http://covers.openlibrary.org/b/id/{cover}-S.jpg'
                    bookpages = 0
                    bookgenre = ''
                    bookdesc = ''
                    workid = book.get('id_librarything')
                    if workid:
                        workid = workid[0]

                    if searchauthorname:
                        author_fuzz = fuzz.token_sort_ratio(author_name, searchauthorname)
                    else:
                        author_fuzz = fuzz.token_sort_ratio(author_name, searchterm)
                    if searchtitle:
                        if book_title.endswith(')'):
                            book_title = book_title.rsplit(' (', 1)[0]
                        book_fuzz = fuzz.token_set_ratio(book_title.lower(), searchtitle.lower())
                        # lose a point for each extra word in the fuzzy matches so we get the closest match
                        words = len(get_list(book_title))
                        words -= len(get_list(searchtitle))
                        book_fuzz -= abs(words)
                    else:
                        book_fuzz = fuzz.token_set_ratio(book_title.lower(), searchterm.lower())
                        words = len(get_list(book_title))
                        words -= len(get_list(searchterm))
                        book_fuzz -= abs(words)
                    isbn_fuzz = 0
                    if is_valid_isbn(searchterm):
                        isbn_fuzz = 100
                        bookisbn = searchterm

                    highest_fuzz = max((author_fuzz + book_fuzz) / 2, isbn_fuzz)

                    if bookid and authorid:
                        resultlist.append({
                            'authorname': author_name,
                            'bookid': bookid,
                            'authorid': authorid,
                            'bookname': book_title,
                            'booksub': booksub,
                            'bookisbn': bookisbn,
                            'bookpub': bookpub,
                            'bookdate': bookdate,
                            'booklang': booklang,
                            'booklink': booklink,
                            'bookrate': bookrate,
                            'bookrate_count': bookrate_count,
                            'bookimg': cover,
                            'bookpages': bookpages,
                            'bookgenre': bookgenre,
                            'bookdesc': bookdesc,
                            'workid': workid,
                            'author_fuzz': round(author_fuzz, 2),
                            'book_fuzz': round(book_fuzz, 2),
                            'isbn_fuzz': round(isbn_fuzz, 2),
                            'highest_fuzz': round(highest_fuzz, 2),
                            'source': "OpenLibrary"
                        })
                        resultcount += 1

                loop_count += 1
                if 0 < CONFIG.get_int('MAX_PAGES') < loop_count:
                    self.logger.warning('Maximum results page search reached, still more results available')
                    next_page = False

                offset += len(results['docs'])
                if offset >= check_int(results["numFound"], 0):
                    next_page = False

            self.logger.debug(f"Found {resultcount} {plural(resultcount, 'result')} with keyword: {searchterm}")
            self.logger.debug(
                f"The OpenLibrary API was hit {api_hits} {plural(api_hits, 'time')} for keyword {searchterm}")

            queue.put(resultlist)

        except Exception:
            self.logger.error(f'Unhandled exception in OL.find_results: {traceback.format_exc()}')

    def find_author_id(self, authorname='', title='', refresh=False):
        authorname = authorname.replace('#', '').replace('/', '_')
        authorname = format_author_name(authorname, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
        self.logger.debug(f"Getting OL author id for {authorname}, refresh={refresh}")
        if title:
            authorbooks, in_cache = json_request(
                f"{self.OL_SEARCH}author={quote_plus(authorname)}&title={quote_plus(title)}", use_cache=not refresh)
        else:
            authorbooks, in_cache = json_request(f"{self.OL_SEARCH}author={quote_plus(authorname)}",
                                                 use_cache=not refresh)
        if authorbooks and authorbooks["docs"]:
            for book in authorbooks['docs']:
                if not book.get('author_name'):
                    continue
                cnt = 0
                while cnt < len(book['author_name']):
                    author_name = format_author_name(book['author_name'][cnt],
                                                     postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
                    if fuzz.token_set_ratio(author_name, authorname) >= CONFIG.get_int('NAME_RATIO'):
                        key = book.get('author_key')[cnt]
                        if key:
                            key = key.split('/')[-1]
                        res = self.get_author_info(key, authorname)
                        if res and res['authorname'] != authorname:
                            res['aka'] = authorname
                        return res
                    cnt += 1

        if title:  # no results using author/title, try author only
            authorbooks, in_cache = json_request(f"{self.OL_SEARCH}author={quote_plus(authorname)}",
                                                 use_cache=not refresh)
            if not authorbooks or not authorbooks["docs"]:
                self.logger.debug(f"No books found for {authorname}")
                return {}
            for book in authorbooks['docs']:
                if not book.get('author_name'):
                    continue
                cnt = 0
                while cnt < len(book['author_name']):
                    author_name = format_author_name(book['author_name'][cnt],
                                                     postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
                    if fuzz.token_set_ratio(author_name, authorname) >= CONFIG.get_int('NAME_RATIO'):
                        key = book.get('author_key')[cnt]
                        if key:
                            key = key.split('/')[-1]
                        res = self.get_author_info(key, authorname, refresh=refresh)
                        if res and res['authorname'] != authorname:
                            res['aka'] = authorname
                        return res
                    cnt += 1
        return {}

    def get_author_info(self, authorid=None, authorname=None, refresh=False):
        authorinfo = {}
        if authorid and not authorid.startswith('OL'):
            self.logger.debug(f"Invalid OL authorid: {authorid}")
            return {}
        if authorid:
            self.logger.debug(f"Getting OL author info for {authorid}:{authorname}, refresh={refresh}")
            authorinfo, in_cache = json_request(f"{self.OL_AUTHOR + authorid}.json", use_cache=not refresh)
        if not authorinfo:
            self.logger.debug(f"No info found for {authorid}:{authorname}")
            return {}

        try:
            if authorinfo['type']['key'] == '/type/redirect':
                newauthorid = authorinfo['location'].rsplit('/', 1)[1]
                self.logger.debug(f"Authorid {authorid} redirected to {newauthorid}")
                authorid = newauthorid
                authorinfo, in_cache = json_request(f"{self.OL_AUTHOR + authorid}.json", use_cache=not refresh)
                if not authorinfo:
                    self.logger.debug(f"No info found for redirect {authorid}")
                    return {}
        except (IndexError, KeyError):
            pass

        bio = authorinfo.get('bio', '')
        if bio and isinstance(bio, dict):
            about = bio.get('value', '')
        else:
            about = ''

        photos = authorinfo.get('photos', '')
        if photos:
            if isinstance(photos, list):
                photos = photos[0]
            author_img = f'http://covers.openlibrary.org/a/id/{photos}-M.jpg'
        else:
            author_img = 'images/nophoto.png'

        author_link = self.OL_AUTHOR + authorid
        author_name = authorinfo.get('name', '')
        author_born = authorinfo.get('birth_date', '')
        author_died = authorinfo.get('death_date', '')

        if "," in author_name:
            postfix = get_list(CONFIG.get_csv('NAME_POSTFIX'))
            words = author_name.split(',')
            if len(words) == 2 and words[0].strip().strip('.').lower in postfix:
                author_name = f"{words[1].strip()} {words[0].strip()}"

        if not author_name:
            self.logger.warning(f"Rejecting authorid {authorid}, no authorname")
            self.matchinglogger.debug(str(authorinfo))
            return {}

        self.logger.debug(f"[{author_name}] Returning OL info for authorID: {authorid}")
        author_dict = {
            'authorid': authorid,
            'authorlink': author_link,
            'authorimg': author_img,
            'authorborn': author_born,
            'authordeath': author_died,
            'about': about,
            'totalbooks': '0',
            'authorname': format_author_name(author_name, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
        }
        return author_dict

    def get_series_members(self, series_id, series_name=''):
        if not self.lt_cache:
            librarything_wait()

        data, self.lt_cache = html_request(self.LT_NSERIES + str(series_id)[2:])
        results = []
        if data:
            try:
                try:
                    seriesname = data.split(b'Series: ')[1].split(b'</')[0].decode('utf-8')
                except IndexError:
                    try:
                        seriesname = data.split(b'<h1')[1].split(b'</h1>')[0].split(b'>')[-1].decode('utf-8')
                    except IndexError:
                        seriesname = ''
                if seriesname:
                    match = fuzz.partial_ratio(seriesname, series_name)
                    if match < 95:
                        self.logger.debug(f"Series name mismatch for {series_id}, "
                                          f"{round(match, 2)}% {seriesname}/{series_name}")
                    else:
                        table = data.split(b'<table>')[1].split(b'</table>')[0]
                        rows = table.split(b'<tr>')
                        for row in rows:
                            row = row.decode('utf-8')
                            if 'href=' in row:
                                try:
                                    workid = row.split('data-workid="')[1].split('"')[0].split('/')[-1]
                                except IndexError:
                                    workid = ''
                                try:
                                    bookname = row.split('data-title="')[1].split('>')[1].split('<')[0]
                                except IndexError:
                                    bookname = ''
                                try:
                                    authorlink = row.split('href="')[2].split('">')[0]
                                    authorname = row.split('href="')[2].split('">')[1].split('<')[0]
                                except IndexError:
                                    authorlink = ''
                                    authorname = ''
                                try:
                                    order = row.split('<td class="right">')[1].split('</label>')[1].split('<')[0]
                                except IndexError:
                                    order = ''
                                onum = check_float(order, 0)
                                if onum == int(onum):  # drop the ".0"
                                    onum = int(onum)
                                if onum:  # might not be numeric
                                    order = onum
                                results.append([order, bookname, authorname, authorlink, workid])
                        self.logger.debug(f"Found {len(results)} for seriesid {series_id}")
                if results:
                    return results

            except IndexError:
                if b'<table>' in data:  # error parsing, or just no series data available?
                    self.logger.debug(f'Error parsing series table for {series_id}')
                else:
                    self.logger.debug(f"SeriesID {series_id} not found at librarything")
                if results:
                    return results

        db = database.DBConnection()
        try:
            res = db.match("SELECT SeriesName from series where SeriesID=?", (series_id,))
        finally:
            db.close()
        if res:
            series_name = res['SeriesName']
            data, self.lt_cache = html_request(self.LT_SERIES + series_name)
            if data:
                try:
                    try:
                        res = data.split(b'Works (')[1].split(b')')[0]
                        works = check_int(res, 0)
                    except IndexError:
                        works = 0
                    self.logger.debug(f"Found {works} for {series_name}")
                    if works:
                        try:
                            seriesid = f"LT{data.split(b'/nseries/')[1].split(b'/')[0].decode('utf-8')}"
                        except IndexError:
                            seriesid = ''
                        if seriesid and seriesid != series_id:
                            self.logger.debug(f"SeriesID mismatch {seriesid}/{series_id} for {series_name}")
                        table = data.split(b'class="worksinseries"')[1].split(b'</table>')[0]
                        rows = table.split(b'<tr')
                        for row in rows:
                            row = row.decode('utf-8')
                            if 'href=' in row:
                                booklink = row.split('href="')[1]
                                bookname = booklink.split('">')[1].split('<')[0]
                                booklink = booklink.split('"')[0]
                                workid = booklink.split('/')[-1]
                                try:
                                    authorlink = row.split('href="')[2]
                                    authorname = authorlink.split('">')[1].split('<')[0]
                                    authorlink = authorlink.split('">')[0]
                                    order = row.split('class="order">')[1].split('<')[0]
                                    results.append([order, bookname, authorname, authorlink, workid])
                                except IndexError:
                                    self.logger.debug(f'Incomplete data in series table for series {series_id}')
                except IndexError:
                    if b'class="worksinseries"' in data:  # error parsing, or just no series data available?
                        self.logger.debug(f'Error in series table for series {series_id}')
        else:
            self.logger.debug(f"SeriesID {series_id} not found in database")
        return results

    def lt_workinfo(self, lt_id):
        if not self.lt_cache:
            librarything_wait()
        result, self.lt_cache = html_request(self.LT_WORK + lt_id)
        if not result:
            return -1, [], []

        soup = BeautifulSoup(result, "html5lib")
        try:
            rating = check_float(result.split(b'<span class="dark_hint">(', 1)[1].split(b')</span>', 1)[0], 0)
            if rating == int(rating):  # drop the ".0"
                rating = int(rating)
        except IndexError:
            rating = 0

        genrelist = []
        tags = soup.find_all("div", class_="tags")
        if tags:
            taglines = tags[0].text
            for lyne in taglines.split('\n'):
                if lyne:
                    try:
                        name, count = lyne.rsplit('(', 1)
                        count = check_int(count.split(')', 1)[0], 0)
                        name = name.strip()
                        accept = True
                        if lazylibrarian.GRGENRES:
                            if name in lazylibrarian.GRGENRES.get('genreExclude', []):
                                accept = False
                            if accept:
                                for item in lazylibrarian.GRGENRES.get('genreExcludeParts', []):
                                    if item in name:
                                        accept = False
                                        break
                            if accept:
                                for item in lazylibrarian.GRGENRES.get('genreReplace', []):
                                    if name == item:
                                        name = lazylibrarian.GRGENRES['genreReplace'][item]
                                        break
                        if accept and count > 1:
                            genrelist.append([name, count])
                    except (IndexError, ValueError):
                        self.logger.error(f"Split genre error [{lyne}]")
            if genrelist:
                genrelist.sort(key=lambda x: x[1], reverse=True)
                limit = lazylibrarian.GRGENRES.get('genreLimit', 0)
                if limit:
                    genrelist = genrelist[:limit]

        serieslist = []
        try:
            seriesinfo = result.split(b"<h2>Belongs to Series</h2><div>")[1].split(b"</div>")[0]
            seriesinfo = seriesinfo.split(b'href="')[1:]
            for item in seriesinfo:
                name, count = item.split(b"</a>")
                count = count.split(b'(')[1].split(b')')[0]
                seriesid, name = name.split(b'>')
                seriesid = seriesid.split(b'"')[0]
                count = check_float(count, 0)
                if count == int(count):  # drop the ".0"
                    count = int(count)
                name = name.decode('utf-8')
                seriesid = seriesid.decode('utf-8').split('/')[2]
                serieslist.append([name, count, f"LT{seriesid}"])
        except (IndexError, ValueError):
            pass

        return rating, genrelist, serieslist

    def get_author_books(self, authorid=None, authorname=None, bookstatus="Skipped", audiostatus='Skipped',
                         entrystatus='Active', refresh=False, reason='ol.get_author_books'):
        offset = 0
        next_page = True
        entryreason = reason
        removed_results = 0
        duplicates = 0
        bad_lang = 0
        not_cached = 0
        added_count = 0
        updated_count = 0
        book_ignore_count = 0
        total_count = 0
        locked_count = 0
        loop_count = 0
        cover_count = 0
        isbn_count = 0
        cover_time = 0
        isbn_time = 0
        api_hits = 0
        gr_lang_hits = 0
        lt_lang_hits = 0
        gb_lang_change = 0
        auth_start = time.time()
        series_updates = []
        cache_hits = 0

        # these are reject reasons we might want to override, so optionally add to database as "ignored"
        ignorable = ['future', 'date', 'isbn', 'set', 'word', 'publisher']
        if CONFIG.get_bool('NO_LANG'):
            ignorable.append('lang')

        db = database.DBConnection()
        try:
            ol_id = ''
            match = db.match('SELECT authorid,ol_id FROM authors where authorid=? or ol_id=?', (authorid, authorid))
            if match:
                ol_id = match['ol_id']
                authorid = match['authorid']
            if not ol_id:
                ol_id = authorid

            # Artist is loading
            db.action("UPDATE authors SET Status='Loading' WHERE AuthorID=?", (authorid,))

            while next_page:
                loop_count += 1
                url = f"{self.OL_SEARCH}author={ol_id}"
                if offset:
                    url += f"&offset={offset}"
                authorbooks, in_cache = json_request(url, use_cache=not refresh)
                api_hits += not in_cache
                cache_hits += in_cache
                if not authorbooks or not authorbooks["docs"]:
                    self.logger.debug(f"No books found for key {ol_id}")
                    docs = []
                else:
                    docs = authorbooks.get('docs', [])
                hit = 0
                miss = 0
                for book in docs:
                    lt_id = book.get('id_librarything')
                    if lt_id and lt_id[0]:
                        hit += 1
                    else:
                        miss += 1
                self.logger.debug(f"{hit + miss} books on page, {hit} with LT_ID, {miss} without")
                total_count += hit + miss
                for book in docs:
                    if not book.get('author_name'):
                        continue
                    auth_name = book.get('author_name')[0]
                    auth_id = book.get('author_key')[0]
                    title = book.get('title')
                    cover = book.get('cover_i')
                    isbns = book.get('isbn')
                    link = book.get('key')
                    isbn = ''
                    lang = ''
                    if isbns:
                        isbn = isbns[0]
                    bookpages = 0
                    bookdesc = ''
                    if title.endswith(')') and '(' not in title:  # openlibrary oddity, "book title )"
                        title = title.rstrip(')').strip()
                    key = book.get('key').split('/')[-1]
                    first_publish_year = book.get('first_publish_year')
                    auth_key = book.get('author_key')[0]
                    languages = book.get('language')
                    publish_date = book.get('publish_date', '')
                    publishers = book.get('publisher')
                    id_librarything = book.get('id_librarything')
                    if id_librarything:
                        id_librarything = id_librarything[0]
                    if not publish_date and first_publish_year:
                        publish_date = [str(first_publish_year)]
                    if publish_date:
                        publish_date = date_format(publish_date[0], context=f"{auth_name}/{title}",
                                                   datelang=CONFIG['DATE_LANG'])

                    rejected = []
                    wantedlanguages = get_list(CONFIG['IMP_PREFLANG'])
                    if wantedlanguages and 'All' not in wantedlanguages:
                        if languages:
                            for item in languages:
                                if item in wantedlanguages:
                                    lang = item
                                    break
                            if not lang:
                                rejected.append(['lang', f'Invalid language [{str(languages)}]'])

                        if not lang and isbn:
                            lang, cache_hit, thing_hit = isbnlang(isbn)
                            if thing_hit:
                                lt_lang_hits += 1
                                if lang not in wantedlanguages:
                                    rejected.append(['lang', f'Invalid language [{lang}]'])

                    if not title:
                        rejected.append(['name', 'No title'])

                    cmd = ("SELECT BookID,LT_WorkID,books.ol_id FROM books,authors "
                           "WHERE books.AuthorID = authors.AuthorID "
                           "and BookName=? COLLATE NOCASE and AuthorName=? COLLATE NOCASE "
                           "and books.Status != 'Ignored' and AudioStatus != 'Ignored'")
                    exists = db.match(cmd, (title, auth_name))
                    if not exists:
                        if auth_id != ol_id:
                            rejected.append(['name', f'Different author ({ol_id}/{auth_id}/{auth_name})'])
                        else:
                            in_db = lazylibrarian.librarysync.find_book_in_db(auth_name, title, source='ol_id',
                                                                              ignored=False, library='eBook',
                                                                              reason=f'ol_get_author_books '
                                                                                     f'{authorid},{title}')
                            if in_db and in_db[0]:
                                cmd = "SELECT BookID,LT_WorkID,ol_id FROM books WHERE BookID=?"
                                exists = db.match(cmd, (in_db[0],))

                    if exists and id_librarything and not exists['LT_WorkID']:
                        db.action("UPDATE books SET LT_WorkID=? WHERE BookID=?",
                                  (id_librarything, exists['BookID']))
                    if exists:
                        # existing bookid might not still be listed at openlibrary so won't refresh.
                        # should we keep new bookid or existing one?
                        # existing one might have been user edited, might be locked,
                        # might have been merged from another authorid or inherited from goodreads?
                        # Should probably use the one with the "best" info but since we don't know
                        # which that is, keep the old one which is already linked to other db tables
                        # but allow info (dates etc.) to be updated
                        if key != exists['BookID']:
                            self.logger.debug(
                                f"Rejecting bookid {key} for [{auth_name}][{title}] already got {exists['BookID']}")
                            if not exists['ol_id']:
                                db.action("UPDATE books SET ol_id=? WHERE BookID=?", (key, exists['BookID']))
                            rejected.append(['name', f"Duplicate id ({key}/{exists['BookID']})"])

                        exists = db.match("SELECT * from books WHERE LT_WorkID=? and BookName !=? COLLATE NOCASE",
                                          (id_librarything, title))
                        if exists:
                            rejected.append(['name', f"Duplicate LT_ID ({title}/{exists['BookName']})"])

                    if publishers:
                        for bookpub in publishers:
                            if bookpub.lower() in get_list(CONFIG['REJECT_PUBLISHER']):
                                rejected.append(['publisher', bookpub])
                                break

                    if not isbn and CONFIG.get_bool('ISBN_LOOKUP') and title:
                        # try lookup by name
                        try:
                            start = time.time()
                            res = isbn_from_words(
                                f"{unaccented(title, only_ascii=False)} {unaccented(auth_name, only_ascii=False)}")
                            isbn_time += (time.time() - start)
                            isbn_count += 1
                        except Exception as e:
                            res = None
                            self.logger.warning(f"Error from isbn: {e}")
                        if res:
                            self.logger.debug(f"isbn found {res} for {key}")
                            isbn = res

                    if not isbn and CONFIG.get_bool('NO_ISBN'):
                        rejected.append(['isbn', 'No ISBN'])

                    dic = {'.': ' ', '-': ' ', '/': ' ', '+': ' ', '_': ' ', '(': '', ')': '',
                           '[': ' ', ']': ' ', '#': '# ', ':': ' ', ';': ' '}
                    name = replace_all(title, dic).strip()
                    name = name.lower()
                    # remove extra spaces if they're in a row
                    name = " ".join(name.split())
                    namewords = name.split(' ')
                    badwords = get_list(CONFIG['REJECT_WORDS'], ',')
                    for word in badwords:
                        if (' ' in word and word in name) or word in namewords:
                            rejected.append(['word', f'Name contains [{word}]'])
                            break

                    bookname = unaccented(title, only_ascii=False)
                    if CONFIG.get_bool('NO_SETS'):
                        is_set, set_msg = is_set_or_part(bookname)
                        if is_set:
                            rejected.append(['set', set_msg])

                    if CONFIG.get_bool('NO_FUTURE') and publish_date > today()[:len(publish_date)]:
                        rejected.append(['future', f'Future publication date [{publish_date}]'])

                    if CONFIG.get_bool('NO_PUBDATE') and (not publish_date or publish_date == '0000'):
                        rejected.append(['date', 'No publication date'])

                    fatal = False
                    reason = ''
                    ignore_book = False
                    ignore_audio = False
                    if rejected:
                        for reject in rejected:
                            if reject[0] not in ignorable:
                                if reject[0] == 'lang':
                                    bad_lang += 1
                                if reject[0] == 'dupe':
                                    duplicates += 1
                                if reject[0] == 'name':
                                    removed_results += 1
                                fatal = True
                                reason = reject[1]
                                break

                        if not CONFIG['IMP_IGNORE']:
                            reason = str(rejected)
                            fatal = True

                        if not fatal:
                            for reject in rejected:
                                if reject[0] in ignorable:
                                    ignore_book = True
                                    ignore_audio = True
                                    book_ignore_count += 1
                                    reason = f"Ignored: {reject[1]}"
                                    break
                    if fatal:
                        self.logger.debug(f"Rejected {key} {reason}")
                        continue  # next book in docs

                    if 'author_update' in entryreason:
                        reason = f'Author: {auth_name}'
                    else:
                        reason = entryreason

                    if not cover:
                        cover = 'images/nocover.png'
                    else:
                        if isinstance(cover, list):
                            cover = cover[0]
                        cover = f'http://covers.openlibrary.org/b/id/{cover}-M.jpg'
                    rating = 0

                    book_status = 'Ignored' if ignore_book else bookstatus
                    audio_status = 'Ignored' if ignore_audio else audiostatus
                    # If we have a librarything ID we can look up series info as openlibrary doesn't
                    # include any. Sadly librarything have disabled whatwork and thingtitle apis
                    # so we can't look up missing IDs and their web pages are not scrapable for the info
                    if id_librarything:
                        rating, genrelist, serieslist = self.lt_workinfo(id_librarything)
                        if rating >= 0:
                            genrenames = []
                            for item in genrelist:
                                genrenames.append(genre_filter(item[0]))
                            genres = ', '.join(set(genrenames))
                            update_value_dict = {}
                            exists = db.match("SELECT * from books WHERE BookID=?", (key,))
                            if not exists:
                                exists = db.match("SELECT * from books WHERE LT_Workid=?", (id_librarything,))
                            if exists:
                                book_status = exists['Status']
                                audio_status = exists['AudioStatus']
                                locked = exists['Manual']
                                if locked is None:
                                    locked = False
                                if locked.isdigit():
                                    locked = bool(int(locked))
                            else:
                                locked = False
                                if ignore_book:
                                    book_status = 'Ignored'
                                if ignore_audio:
                                    audio_status = 'Ignored'
                                bookdate = publish_date
                                bookrate = rating
                                if 'Invalid language [' in reason:
                                    with contextlib.suppress(IndexError):
                                        lang = reason.split('Invalid language [')[1].split("'")[1]
                                infodict = get_gb_info(isbn=isbn, author=auth_name, title=title, expire=False)
                                if infodict:
                                    gbupdate = []
                                    if infodict['desc']:
                                        bookdesc = infodict['desc']
                                        gbupdate.append("Description")
                                    else:
                                        bookdesc = 'No Description'
                                    if not genres and infodict['genre']:
                                        genres = genre_filter(infodict['genre'])
                                        gbupdate.append('Genres')
                                    if not bookdate or bookdate == '0000' or len(infodict['date']) > len(bookdate):
                                        bookdate = infodict['date']
                                        gbupdate.append('Publication Date')
                                    if infodict['pub'] and not publishers:
                                        publishers = infodict['pub']
                                        gbupdate.append('Publisher')
                                    if infodict['rate'] and not bookrate:
                                        bookrate = infodict['rate']
                                        gbupdate.append('Rating')
                                    if infodict['pages'] and not bookpages:
                                        bookpages = infodict['pages']
                                        gbupdate.append('Pages')
                                    if infodict['lang'] and not lang:
                                        lang = infodict['lang']
                                        gbupdate.append('Language')
                                    if gbupdate:
                                        self.logger.debug(f"Updated {', '.join(gbupdate)} from googlebooks")
                                        gb_lang_change += 1

                                reason = f"[{thread_name()}] {reason}"

                                if isinstance(publishers, list):
                                    publishers = ', '.join(publishers)
                                cover_link = cover
                                if 'nocover' in cover or 'nophoto' in cover:
                                    start = time.time()
                                    cover_link, _ = get_book_cover(key, ignore='openlibrary')
                                    cover_time += (time.time() - start)
                                    cover_count += 1
                                elif cover and cover.startswith('http'):
                                    cover_link = cache_bookimg(cover, key, 'ol')
                                if not cover_link:  # no results on search or failed to cache it
                                    cover_link = 'images/nocover.png'

                                rejected = False
                                if CONFIG.get_bool('NO_FUTURE') and publish_date > today()[:len(publish_date)]:
                                    rejected = True
                                    reason = f'Future publication date [{publish_date}]'

                                if CONFIG.get_bool('NO_PUBDATE') and (not publish_date or publish_date == '0000'):
                                    rejected = True
                                    reason = 'No publication date'

                                if not rejected:
                                    wantedlanguages = get_list(CONFIG['IMP_PREFLANG'])
                                    if (wantedlanguages and 'All' not in wantedlanguages and
                                            (not lang or lang not in wantedlanguages)):
                                        reason = f"Invalid language {lang}"
                                        if 'lang' not in ignorable:
                                            bad_lang += 1
                                            rejected = True
                                        else:
                                            book_status = 'Ignored'
                                            audio_status = 'Ignored'
                                if not rejected:
                                    db.action('INSERT INTO books (AuthorID, BookName, BookDesc, BookGenre, '
                                              'BookIsbn, BookPub, BookRate, BookImg, BookLink, BookID, BookDate, '
                                              'BookLang, BookAdded, Status, WorkPage, AudioStatus, LT_WorkID, '
                                              'ScanResult, OriginalPubDate, BookPages, ol_id) '
                                              'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                              (authorid, title, bookdesc, genres, isbn, publishers, bookrate,
                                               cover_link, link, key, bookdate, lang, now(), book_status, '',
                                               audio_status, id_librarything, reason, first_publish_year, bookpages,
                                               key))
                                else:
                                    self.logger.debug(f"Rejected {key} {reason}")
                                    continue  # next book in docs

                            # Leave alone if locked
                            if locked:
                                locked_count += 1
                            else:
                                if exists and exists['ScanResult'] and ' publication date' in exists['ScanResult'] \
                                        and publish_date and publish_date != '0000' and \
                                        publish_date <= today()[:len(publish_date)]:
                                    # was rejected on previous scan but bookdate has become valid
                                    self.logger.debug(
                                        f"valid bookdate [{publish_date}] previous scanresult "
                                        f"[{exists['ScanResult']}]")

                                    update_value_dict["ScanResult"] = f"bookdate {publish_date} is now valid"
                                    self.searchinglogger.debug(
                                        f"entry status {entrystatus} {bookstatus},{audiostatus}")
                                    book_stat, audio_stat = get_status(key, serieslist, bookstatus,
                                                                       audiostatus, entrystatus)
                                    if book_status not in ['Wanted', 'Open', 'Have'] and not ignore_book:
                                        update_value_dict["Status"] = book_stat
                                    if audio_status not in ['Wanted', 'Open', 'Have'] and not ignore_book:
                                        update_value_dict["AudioStatus"] = audio_stat
                                    self.searchinglogger.debug(f"status is now {book_status},{audio_status}")
                                elif not exists:
                                    update_value_dict["ScanResult"] = reason

                            if update_value_dict:
                                control_value_dict = {"LT_WorkID": id_librarything}
                                db.upsert("books", update_value_dict, control_value_dict)

                            if not exists:
                                typ = 'Added'
                                added_count += 1
                            else:
                                typ = 'Updated'
                                updated_count += 1
                            msg = f"[{auth_name}] {typ} book: {title} [{lang}] status {bookstatus}"
                            if CONFIG.get_bool('AUDIO_TAB'):
                                msg += f" audio {audiostatus}"
                            self.logger.debug(msg)

                            if CONFIG.get_bool('ADD_SERIES'):
                                for series in serieslist:
                                    newseries = f"{series[0]} {series[1]}"
                                    newseries.strip()
                                    seriesid = series[2]
                                    exists = db.match("SELECT * from series WHERE seriesid=?", (seriesid,))
                                    if not exists:
                                        exists = db.match("SELECT * from series WHERE seriesname=? "
                                                          "and instr(seriesid, 'LT') = 1", (series[0],))
                                    if not exists:
                                        self.logger.debug(f"New series: {series[0]}")
                                        db.action('INSERT INTO series (SeriesID, SeriesName, Status, '
                                                  'Updated, Reason) VALUES (?,?,?,?,?)',
                                                  (seriesid, series[0], 'Paused', time.time(),
                                                   id_librarything), suppress='UNIQUE')
                                        db.commit()
                                        exists = {'Status': 'Paused'}
                                    seriesmembers = None
                                    cmd = "SELECT * from member WHERE seriesid=? AND WorkID=?"
                                    if not db.match(cmd, (seriesid, id_librarything)):
                                        seriesmembers = [[series[1], title, auth_name, auth_key, id_librarything]]
                                        if seriesid in series_updates:
                                            self.logger.debug(f"Series {seriesid} already updated")
                                        elif exists['Status'] in ['Paused', 'Ignored']:
                                            self.logger.debug(
                                                f"Not getting additional series members for "
                                                f"{seriesid}:{series[0]}, status is {exists['Status']}")
                                        else:
                                            seriesmembers = self.get_series_members(seriesid, series[0])
                                            series_updates.append(seriesid)
                                            if not seriesmembers:
                                                self.logger.warning(
                                                    f"Series {series[0]} ({seriesid}) has no members "
                                                    f"at librarything")
                                    if seriesmembers:
                                        if len(seriesmembers) == 1:
                                            self.logger.debug(
                                                f"Found member {seriesmembers[0][0]} for series {series[0]}")
                                        else:
                                            self.logger.debug(
                                                f"Found {len(seriesmembers)} members for series {series[0]}")
                                        for member in seriesmembers:
                                            # member[order, bookname, authorname, authorlink, workid]
                                            # remove any old entries for this series member
                                            db.action("DELETE from member WHERE SeriesID=? AND SeriesNum=?",
                                                      (seriesid, member[0]))
                                            auth_name, exists = lazylibrarian.importer.get_preferred_author(
                                                member[2])
                                            if not exists:
                                                reason = f"Series author {series[0]}:{member[1]}"
                                                lazylibrarian.importer.add_author_name_to_db(author=member[2],
                                                                                             refresh=False,
                                                                                             addbooks=False,
                                                                                             reason=reason
                                                                                             )
                                                auth_name, exists = \
                                                    lazylibrarian.importer.get_preferred_author(member[2])
                                                if exists:
                                                    auth_name = member[2]
                                                else:
                                                    self.logger.debug(
                                                        f"Unable to add {member[2]} for {member[1]}, "
                                                        f"author not found")
                                                    continue
                                            else:
                                                cmd = "SELECT * from authors WHERE authorname=?"
                                                exists = db.match(cmd, (auth_name,))
                                                if exists:
                                                    auth_key = exists['AuthorID']
                                                    if fuzz.ratio(auth_name.lower().replace('.', ''),
                                                                  member[2].lower().replace('.', '')) < 95:
                                                        akas = get_list(exists['AKA'], ',')
                                                        if member[2] not in akas:
                                                            akas.append(member[2])
                                                            db.action("UPDATE authors SET AKA=? WHERE AuthorID=?",
                                                                      (', '.join(akas), auth_key))
                                                    match = db.match(
                                                        "SELECT * from seriesauthors WHERE SeriesID=? "
                                                        "and AuthorID=?",
                                                        (seriesid, auth_key))
                                                    if not match:
                                                        self.logger.debug(
                                                            f"Adding {auth_name} as series author for {series[0]}")
                                                        db.action('INSERT INTO seriesauthors (SeriesID, '
                                                                  'AuthorID) VALUES (?, ?)',
                                                                  (seriesid, auth_key), suppress='UNIQUE')

                                            # if book not in library, use librarything workid to get an isbn
                                            # use that to get openlibrary workid
                                            # add book to library, then add seriesmember
                                            exists = db.match("SELECT * from books WHERE LT_Workid=?",
                                                              (member[4],))
                                            if exists:
                                                match = db.match(
                                                    "SELECT * from member WHERE SeriesID=? AND BookID=?",
                                                    (seriesid, exists['BookID']))
                                                if not match:
                                                    self.logger.debug(
                                                        f"Inserting new member [{member[0]}] for {series[0]}")
                                                    db.action(
                                                        "INSERT INTO member (SeriesID, BookID, WorkID, "
                                                        "SeriesNum) VALUES (?,?,?,?)",
                                                        (seriesid, exists['BookID'], member[4], member[0]),
                                                        suppress='UNIQUE')
                                                ser = db.match(
                                                    "select count(*) as counter from member where seriesid=?",
                                                    (seriesid,))
                                                if ser:
                                                    counter = check_int(ser['counter'], 0)
                                                    db.action("UPDATE series SET Total=? WHERE SeriesID=?",
                                                              (counter, seriesid))
                                            else:
                                                if not self.lt_cache:
                                                    librarything_wait()
                                                bookresult, self.lt_cache = html_request(self.LT_WORK + member[4])
                                                if bookresult:
                                                    isbns = bookresult.split(b'ISBN:')
                                                    isbnlist = []
                                                    for item in isbns:
                                                        item = item.decode('utf-8')
                                                        isbn = item.split('&', 1)[0].split(',', 1)[0]
                                                        if is_valid_isbn(isbn):
                                                            isbnlist.append(isbn)
                                                    workid = None
                                                    worklink = None
                                                    for isbn in isbnlist:
                                                        bookinfo, in_cache = json_request(
                                                            f"{self.OL_ISBN + isbn}.json")
                                                        api_hits += not in_cache
                                                        cache_hits += in_cache
                                                        if bookinfo and bookinfo.get('works'):
                                                            try:
                                                                for work in bookinfo.get('works'):
                                                                    worklink = work.get('key')
                                                                    workid = work.get('key').split('/')[-1]
                                                            except IndexError:
                                                                workid = None
                                                            if workid:
                                                                break
                                                    if workid:
                                                        workinfo, in_cache = json_request(
                                                            f"{self.OL_WORK + workid}.json")
                                                        api_hits += not in_cache
                                                        cache_hits += in_cache
                                                        if workinfo and 'title' in workinfo:
                                                            title = workinfo.get('title')
                                                            covers = workinfo.get('covers')
                                                            if covers:
                                                                if isinstance(covers, list):
                                                                    covers = covers[0]
                                                                cover = 'http://covers.openlibrary.org/b/id/'
                                                                cover += f'{covers}-M.jpg'
                                                            else:
                                                                cover = 'images/nocover.png'
                                                            publish_date = date_format(workinfo.get('publish_date',
                                                                                                    ''),
                                                                                       context=title,
                                                                                       datelang=CONFIG['DATE_LANG'])
                                                            rating, genrelist, _ = self.lt_workinfo(member[4])
                                                            genrenames = []
                                                            for item in genrelist:
                                                                genrenames.append(item[0])
                                                            genres = ', '.join(genrenames)
                                                            lang = ''
                                                            match = db.match(
                                                                "SELECT * from authors WHERE AuthorName=? "
                                                                "COLLATE NOCASE",
                                                                (auth_name,))
                                                            if match:
                                                                bauth_key = match['AuthorID']
                                                            else:
                                                                reason = f"Series author {series[0]}:{member[1]}"
                                                                lazylibrarian.importer.add_author_name_to_db(
                                                                    author=auth_name, refresh=False,
                                                                    addbooks=False, reason=reason)
                                                                match = db.match(
                                                                    "SELECT * from authors WHERE "
                                                                    "AuthorName=? COLLATE NOCASE",
                                                                    (auth_name,))
                                                                if match:
                                                                    bauth_key = match['AuthorID']
                                                                else:
                                                                    msg = f"Unable to add {auth_name} for {title}"
                                                                    msg += ", author not in database"
                                                                    self.logger.debug(msg)
                                                                    continue

                                                            match = db.match("SELECT * from books WHERE BookID=?",
                                                                             (workid,))
                                                            rejected = False
                                                            if not match:
                                                                self.logger.debug(
                                                                    f"Insert new member [{member[0]}] for "
                                                                    f"{series[0]}")

                                                                reason = f"Member {member[0]} of series {series[0]}"
                                                                reason = f"[{thread_name()}] {reason}"
                                                                added_count += 1
                                                                if not lang:
                                                                    lang = 'Unknown'
                                                                if (wantedlanguages and 'All' not in wantedlanguages
                                                                        and lang not in wantedlanguages):
                                                                    self.logger.debug(
                                                                        f"Invalid language {lang}")
                                                                    if 'lang' not in ignorable:
                                                                        bad_lang += 1
                                                                        rejected = True
                                                                    else:
                                                                        book_status = 'Ignored'
                                                                        audio_status = 'Ignored'
                                                                if not rejected:
                                                                    if 'nocover' in cover or 'nophoto' in cover:
                                                                        start = time.time()
                                                                        cover, _ = get_book_cover(
                                                                            workid, ignore='openlibrary')
                                                                        cover_time += (time.time() - start)
                                                                        cover_count += 1
                                                                    elif cover and cover.startswith('http'):
                                                                        cover = cache_bookimg(
                                                                            cover, workid, 'ol')
                                                                    if not cover:  # no results or failed to cache
                                                                        cover = 'images/nocover.png'
                                                                    db.action('INSERT INTO books (AuthorID, '
                                                                              'BookName, BookDesc, BookGenre, '
                                                                              'BookIsbn, BookPub, BookRate, '
                                                                              'BookImg, BookLink, BookID, '
                                                                              'BookDate, BookLang, BookAdded, '
                                                                              'Status, WorkPage, AudioStatus, '
                                                                              'LT_WorkID, ScanResult, '
                                                                              'OriginalPubDate, ol_id) '
                                                                              'VALUES (?,?,?,?,?,?,?,?,?,?,?,'
                                                                              '?,?,?,?,?,?,?,?,?)',
                                                                              (bauth_key, title, '', genres, '',
                                                                               '', rating, cover, worklink, workid,
                                                                               publish_date, lang, '', bookstatus,
                                                                               '', audiostatus, member[4],
                                                                               reason, publish_date, workid))
                                                            if not rejected:
                                                                match = db.match(
                                                                    "SELECT * from seriesauthors WHERE "
                                                                    "SeriesID=? AND AuthorID=?",
                                                                    (seriesid, bauth_key))
                                                                if not match:
                                                                    self.logger.debug(
                                                                        f'Add {auth_name} as series author for '
                                                                        f'{series[0]}')
                                                                    db.action(
                                                                        "INSERT INTO seriesauthors ('SeriesID', "
                                                                        "\"AuthorID\") VALUES (?, ?)",
                                                                        (seriesid, bauth_key), suppress='UNIQUE')

                                                                match = db.match(
                                                                    "SELECT * from member WHERE SeriesID=? "
                                                                    "AND BookID=?",
                                                                    (seriesid, workid))
                                                                if not match:
                                                                    db.action(
                                                                        "INSERT INTO member (SeriesID, BookID, "
                                                                        "WorkID, SeriesNum) VALUES (?,?,?,?)",
                                                                        (seriesid, workid, member[4],
                                                                         member[0]), suppress='UNIQUE')
                                                                    ser = db.match(
                                                                        "select count(*) as counter from member "
                                                                        "where seriesid=?",
                                                                        (seriesid,))
                                                                    if ser:
                                                                        counter = check_int(ser['counter'], 0)
                                                                        db.action(
                                                                            "UPDATE series SET Total=? WHERE "
                                                                            "SeriesID=?", (counter, seriesid))
                    if rating == 0:
                        self.logger.debug("No additional librarything info")
                    exists = db.match("SELECT * from books WHERE BookID=?", (key,))
                    if not exists:
                        self.logger.debug(f"Inserting new book for {authorid}")
                        if 'author_update' in entryreason:
                            reason = f'Author: {auth_name}'
                        else:
                            reason = entryreason
                        reason = f"[{thread_name()}] {reason}"
                        rejected = False
                        if not lang:
                            lang = 'Unknown'
                        if wantedlanguages and 'All' not in wantedlanguages and lang not in wantedlanguages:
                            self.logger.debug(f"Invalid language {lang} {ignorable}")
                            if 'lang' not in ignorable:
                                bad_lang += 1
                                rejected = True
                            else:
                                book_status = 'Ignored'
                                audio_status = 'Ignored'
                        if not rejected:
                            added_count += 1
                            if 'nocover' in cover or 'nophoto' in cover:
                                start = time.time()
                                cover, _ = get_book_cover(key, ignore='openlibrary')
                                cover_time += (time.time() - start)
                                cover_count += 1
                            elif cover and cover.startswith('http'):
                                cover = cache_bookimg(cover, key, 'ol')
                            if not cover:  # no results on search or failed to cache it
                                cover = 'images/nocover.png'

                            db.action(
                                "INSERT INTO books (AuthorID, BookName, BookImg, BookLink, BookID, "
                                "BookDate, BookLang, BookAdded, Status, WorkPage, AudioStatus, ScanResult, "
                                "OriginalPubDate, ol_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (authorid, title, cover, link, key, publish_date, lang, now(),
                                 book_status, '', audio_status, reason, first_publish_year, key))

                    if not rejected:
                        db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                                  (authorid, key, ROLE['PRIMARY']), suppress='UNIQUE')

                        # add any additional contributing authors
                        # ol gives us a list of names and a list of keys
                        authornames = book.get('author_name')
                        authorkeys = book.get('author_key')
                        contributing_authors = []
                        cnt = 1
                        while cnt < len(authornames):
                            contributing_authors.append([authorkeys[cnt], " ".join(authornames[cnt].split())])
                            cnt += 1

                        if CONFIG.get_bool('CONTRIBUTING_AUTHORS'):
                            for entry in contributing_authors:
                                auth_id = lazylibrarian.importer.add_author_to_db(authorname=entry[1], refresh=False,
                                                                                  authorid=entry[0],
                                                                                  addbooks=False,
                                                                                  reason=f"Contributor to {title}")
                                if auth_id:
                                    db.action('INSERT into bookauthors (AuthorID, BookID, Role) VALUES (?, ?, ?)',
                                              (auth_id, key, ROLE['CONTRIBUTING']), suppress='UNIQUE')
                                    lazylibrarian.importer.update_totals(auth_id)
                                else:
                                    self.logger.debug(f"Unable to add {auth_id}")

                next_page = True
                if authorbooks and authorbooks.get("docs"):
                    offset += len(authorbooks['docs'])
                    if offset >= check_int(authorbooks["numFound"], 0):
                        next_page = False
                else:
                    next_page = False

            lazylibrarian.importer.update_totals(authorid)
            delete_empty_series()
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

            resultcount = added_count + updated_count
            self.logger.debug(
                f"Found {total_count} {plural(total_count, 'result')} in {loop_count} {plural(loop_count, 'page')}")
            self.logger.debug(f"Found {locked_count} locked {plural(locked_count, 'book')}")
            self.logger.debug(f"Removed {bad_lang} unwanted language {plural(bad_lang, 'result')}")
            self.logger.debug(f"Removed {removed_results} incorrect/incomplete {plural(removed_results, 'result')}")
            self.logger.debug(f"Removed {duplicates} duplicate {plural(duplicates, 'result')}")
            self.logger.debug(f"Ignored {book_ignore_count} {plural(book_ignore_count, 'book')}")
            self.logger.debug(
                f"Imported/Updated {resultcount} {plural(resultcount, 'book')} in "
                f"{int(time.time() - auth_start)} secs using {api_hits} api {plural(api_hits, 'hit')}")
            if cover_count:
                self.logger.debug(f"Fetched {cover_count} {plural(cover_count, 'cover')} in {cover_time:.2f} sec")
            if isbn_count:
                self.logger.debug(f"Fetched {isbn_count} ISBN in {isbn_time:.2f} sec")

            control_value_dict = {"authorname": authorname.replace('"', '""')}
            new_value_dict = {
                "GR_book_hits": api_hits,
                "GR_lang_hits": gr_lang_hits,
                "LT_lang_hits": lt_lang_hits,
                "GB_lang_change": gb_lang_change,
                "cache_hits": cache_hits,
                "bad_lang": bad_lang,
                "bad_char": removed_results,
                "uncached": not_cached,
                "duplicates": duplicates
            }
            db.upsert("stats", new_value_dict, control_value_dict)
        finally:
            db.close()

    def get_bookdict_for_bookid(self, bookid=None):
        bookdict = {}
        url = f"{self.OL_WORK + bookid}.json"
        try:
            self.searchinglogger.debug(url)
            workinfo, in_cache = json_request(url)
            if not workinfo:
                self.logger.debug(f"OL no bookinfo for {bookid}")
                return None, False
        except Exception as e:
            self.logger.error(f"{type(e).__name__} finding book: {str(e)}")
            return None, False

        authors = workinfo.get('authors')
        authorid = ''
        if authors:
            try:
                authorid = authors[0]['author']['key']
                authorid = authorid.split('/')[-1]
            except KeyError:
                try:
                    authorid = authors[0]['key']
                    authorid = authorid.split('/')[-1]
                except KeyError:
                    authorid = ''

        bookdict['authorid'] = authorid
        bookdict['bookid'] = bookid
        auth = self.get_author_info(authorid)
        if not auth or not auth.get('authorname'):
            self.logger.debug(f"OL no authorname for {bookid}")
            return None, False
        bookdict['authorname'] = auth['authorname']
        bookdict['bookname'] = workinfo.get('title', '')
        bookdict['booksub'] = ''
        bookdict['bookisbn'] = workinfo.get('isbn_13', '')
        if not bookdict['bookisbn']:
            bookdict['bookisbn'] = workinfo.get('isbn_10', '')
        if isinstance(bookdict['bookisbn'], list):
            bookdict['bookisbn'] = ', '.join(bookdict['bookisbn'])
        else:
            try:
                res = isbn_from_words(f"{bookdict['bookname']} {unaccented(bookdict['authorname'], only_ascii=False)}")
            except Exception as e:
                res = None
                self.logger.warning(f"Error from isbn: {e}")
            if res:
                self.logger.debug(f"isbn found {res} for {bookdict['bookname']}")
                bookdict['bookisbn'] = res
        bookdict['bookpub'] = workinfo.get('publishers', '')
        if isinstance(bookdict['bookpub'], list):
            bookdict['bookpub'] = ', '.join(bookdict['bookpub'])
        bookdict['bookdate'] = date_format(workinfo.get('publish_date', ''),
                                           context=bookdict['bookname'], datelang=CONFIG['DATE_LANG'])
        bookdict['booklang'] = "Unknown"
        bookdict['booklink'] = workinfo.get('key')
        bookdict['bookrate'] = 0
        bookdict['bookrate_count'] = 0
        covers = workinfo.get('covers', '')
        if covers:
            if isinstance(covers, list):
                covers = covers[0]
            cover = 'http://covers.openlibrary.org/b/id/'
            cover += f'{covers}-M.jpg'
        else:
            cover = 'images/nocover.png'
        bookdict['bookimg'] = cover
        bookdict['bookpages'] = int(workinfo.get('number_of_pages', 0))
        bookdict['bookgenre'] = ''
        bookdict['bookdesc'] = ''
        bookdict['contributors'] = []
        bookdict['series'] = []
        bookdict['source'] = 'OpenLibrary'
        return bookdict, in_cache

    def add_bookid_to_db(self, bookid=None, bookstatus=None, audiostatus=None, reason='ol.add_bookid'):

        bookdict, _ = self.get_bookdict_for_bookid(bookid)
        authorname = bookdict.get('authorname')
        if not authorname:
            self.logger.warning(f"No AuthorName for {bookid}, unable to add book")
            return False
        title = bookdict.get('bookname')
        if not title:
            self.logger.warning(f"No title for {bookid}, unable to add book")
            return False

        db = database.DBConnection()
        auth_name, exists = lazylibrarian.importer.get_preferred_author(authorname)
        if exists:
            match = db.match('SELECT AuthorName,AuthorID from authors WHERE AuthorName=?', (auth_name,))
            bookdict['authorname'] = match['AuthorName']
            bookdict['authorid'] = match['AuthorID']
        else:
            auth_id = lazylibrarian.importer.add_author_name_to_db(author=authorname,
                                                                   refresh=False,
                                                                   addbooks=False,
                                                                   reason=f"ol.add_bookid {bookid}")
            # authorid may have changed on importing
            match = db.match('SELECT AuthorName,AuthorID from authors '
                             'WHERE AuthorID=? or ol_id=?', (auth_id, auth_id))
            if match:
                bookdict['authorname'] = match['AuthorName']
                bookdict['authorid'] = match['AuthorID']
            else:
                self.logger.warning(f"No match for {auth_id}, unable to add book {title}")
                db.close()
                return False
        db.close()

        # validate bookdict, reject if unwanted or incomplete
        bookdict, rejected = validate_bookdict(bookdict)
        if rejected:
            if reason.startswith("Series:") or 'bookname' not in bookdict or 'authorname' not in bookdict:
                return False
            for reject in rejected:
                if reject[0] == 'name':
                    return False
        # show any non-fatal warnings
        warn_about_bookdict(bookdict)

        # Add book to database using bookdict
        bookdict['status'] = bookstatus
        bookdict['audiostatus'] = audiostatus
        bookdict['reason'] = f"[{thread_name()}] {reason}"
        res = add_bookdict_to_db(bookdict)
        lazylibrarian.importer.update_totals(bookdict['authorid'])
        return res
