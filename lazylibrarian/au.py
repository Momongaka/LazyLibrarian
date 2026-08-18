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
import re
import traceback
from queue import Queue
from urllib.parse import quote_plus

from rapidfuzz import fuzz

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.bookdict import (
    add_author_books_to_db,
    add_bookdict_to_db,
    validate_bookdict,
    warn_about_bookdict,
)
from lazylibrarian.cache import json_request
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import (
    check_float,
    check_int,
    format_author_name,
    get_list,
    plural,
    replace_all,
)


class Audible:
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.searchinglogger = logging.getLogger('special.searching')
        self.catalog_url = 'https://api.audible.com/1.0/catalog/products'
        self.audnex_url = 'https://api.audnex.us'
        self.providername = 'audible'

    def find_results(self, searchterm=None, queue=None):
        """
        Searchterm may be passed as a title, author, or title<ll>author
        On return, queue should contain a list of dicts in the standard layout
        """
        resultlist = []
        try:
            if not CONFIG['AU_API']:
                self.logger.warning('Audible API not enabled, check config')
                queue.put(resultlist)
                return 0
            if BLOCKHANDLER.is_blocked(self.providername):
                queue.put(resultlist)
                return 0

            api_hits = 0
            title = ''
            authorname = ''
            if searchterm and '<ll>' in searchterm:  # special token separates title from author
                title, authorname = searchterm.split('<ll>')
                title = title.split(' (')[0]  # remove series info

            keywords = title or (searchterm or '').replace('<ll>', ' ').strip()
            url = (f"{self.catalog_url}?author={quote_plus(authorname)}&keywords={quote_plus(keywords)}"
                   f"&response_groups=product_attrs,product_extended_attrs,product_desc,series"
                   f"&products_sort_by=ReleaseDate&num_results=50")
            self.searchinglogger.debug(url)

            data, in_cache = json_request(url)
            api_hits += not in_cache
            if not data or not data.get('products'):
                self.logger.debug(f"No Audible results for {searchterm}")
                queue.put(resultlist)
                return 0

            db = database.DBConnection()
            try:
                for product in data['products']:
                    book = self._build_catalog_book_dict(product)
                    if not book['bookname']:
                        continue

                    author_fuzz = fuzz.token_sort_ratio(authorname, authorname or searchterm)
                    book_fuzz = fuzz.token_set_ratio(book['bookname'], title or searchterm)
                    words = len(get_list(book['bookname'])) - len(get_list(title or searchterm))
                    book_fuzz -= abs(words)
                    highest_fuzz = max((author_fuzz + book_fuzz) / 2, 0)

                    author_id = ''
                    if authorname:
                        match = db.match('SELECT AuthorID FROM authors WHERE AuthorName=?', (authorname,))
                        if match:
                            author_id = match['AuthorID']

                    resultlist.append({
                        'authorname': authorname,
                        'authorid': author_id,
                        'bookid': book['bookid'],
                        'bookname': book['bookname'],
                        'booksub': book['booksub'],
                        'bookisbn': book['bookisbn'],
                        'bookpub': book['bookpub'],
                        'bookdate': book['bookdate'],
                        'booklang': book['booklang'],
                        'booklink': book['booklink'],
                        'bookrate': book['bookrate'],
                        'bookrate_count': book['bookrate_count'],
                        'bookimg': book['bookimg'],
                        'bookpages': book['bookpages'],
                        'bookgenre': book['bookgenre'],
                        'bookdesc': book['bookdesc'],
                        'author_fuzz': round(author_fuzz, 2),
                        'book_fuzz': round(book_fuzz, 2),
                        'isbn_fuzz': 0,
                        'highest_fuzz': round(highest_fuzz, 2),
                        'contributors': [],
                        'series': book['series'],
                        'source': 'Audible',
                    })
            finally:
                db.close()

            self.logger.debug(f"Found {len(resultlist)} {plural(len(resultlist), 'result')} with keyword: {searchterm}")
            self.logger.debug(f"Audible API was hit {api_hits} {plural(api_hits, 'time')} for keyword {searchterm}")
            queue.put(resultlist)
            return len(resultlist)
        except Exception:
            self.logger.error(f'Unhandled exception in Audible.find_results: {traceback.format_exc()}')
            queue.put(resultlist)
            return 0

    @staticmethod
    def _build_catalog_book_dict(product):
        """ Parse one product entry from the Audible catalog search response """
        asin = product.get('asin', '')
        bookname = product.get('publication_name') or product.get('title') or ''
        title_slug = re.sub(r'[^A-Za-z0-9]+', '-', bookname).strip('-')
        series = []
        if product.get('series'):
            ser = product['series'][0]
            series_asin = ser.get('asin') or asin
            if ser.get('title'):
                series = [(ser['title'], f"AU{series_asin}", ser.get('sequence', ''))]
        rating = product.get('rating', {}).get('overall_distribution', {})
        return {
            'bookid': asin,
            'bookname': bookname,
            'booksub': product.get('subtitle', ''),
            'bookisbn': '',
            'bookpub': product.get('publisher_name', ''),
            'bookdate': product.get('release_date') or product.get('issue_date', ''),
            'booklang': (product.get('language') or 'English').capitalize(),
            'booklink': f"https://www.audible.com/pd/{title_slug}-Audiobook/{asin}" if asin else '',
            'bookrate': check_float(rating.get('average_rating'), 0.0),
            'bookrate_count': check_int(rating.get('num_ratings'), 0),
            'bookimg': product.get('product_images', {}).get('500') or 'images/nocover.png',
            'bookpages': str(product.get('runtime_length_min', 0)),
            'bookgenre': '',
            'bookdesc': product.get('merchandising_summary') or product.get('publisher_summary', ''),
            'series': series,
        }

    def find_author_id(self, authorname=None, title=None, refresh=False):
        return self.get_author_info(authorname=authorname, refresh=refresh)

    def get_author_info(self, authorid=None, authorname=None, refresh=False):
        """ Get detailed info for an author, via the Audnex API """
        self.logger.debug(f"Getting Audible author info for {authorid}:{authorname}, refresh={refresh}")
        if not CONFIG['AU_API']:
            self.logger.warning('Audible API not enabled, check config')
            return {}
        if BLOCKHANDLER.is_blocked(self.providername):
            return {}

        if not authorid and authorname:
            url = f"{self.audnex_url}/authors?name={quote_plus(authorname)}&region=us"
            self.searchinglogger.debug(url)
            data, _ = json_request(url, use_cache=not refresh)
            if isinstance(data, list) and data:
                authorid = data[0].get('asin')
                if not authorname:
                    authorname = data[0].get('name')

        if not authorid:
            return {}

        url = f"{self.audnex_url}/authors/{authorid}"
        self.searchinglogger.debug(url)
        data, _ = json_request(url, use_cache=not refresh)
        if not data or not data.get('asin'):
            self.logger.debug(f"No Audible author found for {authorid}:{authorname}")
            return {}

        authorname = format_author_name(data.get('name') or authorname or '',
                                        postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
        if not authorname:
            self.logger.warning(f"Rejecting authorid {authorid}, no authorname")
            return {}

        return {
            'authorid': data['asin'],
            'authorname': authorname,
            'authorlink': f"https://www.audible.com/author/{data['asin']}",
            'authorimg': data.get('image', ''),
            'authorborn': '',
            'authordeath': '',
            'about': data.get('description', ''),
            'totalbooks': 0,
        }

    def get_author_books(self, authorid=None, authorname=None, bookstatus="Skipped",
                         audiostatus="Skipped", entrystatus='Active', refresh=False,
                         reason='au.get_author_books'):
        """ Given an authorid and/or authorname, find all books for that author and add them to the database """
        if not CONFIG['AU_API']:
            self.logger.warning('Audible API not enabled, check config')
            return

        db = database.DBConnection()
        auth_id = authorid
        auth_name = authorname
        entryreason = reason
        try:
            if authorid:
                res = db.match('SELECT AuthorName from authors WHERE AuthorID=?', (authorid,))
                if res:
                    auth_name = res['AuthorName']
            else:
                auth_name, auth_id = lazylibrarian.importer.get_preferred_author(authorname)

            self.logger.debug(f"[{auth_name}] Now processing books with Audible API")
            db.action("UPDATE authors SET Status='Loading' WHERE AuthorID=?", (auth_id,))

            resultqueue = Queue()
            hits = self.find_results(f"<ll>{auth_name}", resultqueue)
            if not hits:
                self.logger.warning(f"No results from Audible for {auth_name}")
                return

            _ = add_author_books_to_db(resultqueue, bookstatus, audiostatus, entrystatus, entryreason,
                                       auth_id, None, self.get_bookdict_for_bookid, cache_hits=0)
        except Exception:
            self.logger.error(f'Unhandled exception in Audible.get_author_books: {traceback.format_exc()}')
        finally:
            db.action("UPDATE authors SET Status=? WHERE AuthorID=?", (entrystatus, auth_id))
            db.close()

    def get_bookdict_for_bookid(self, bookid):
        """ Gather book data for a bookid, but don't add to database """
        if not bookid:
            return {}, False
        url = f"{self.audnex_url}/books/{bookid}"
        self.searchinglogger.debug(url)
        data, in_cache = json_request(url)
        if not data or not data.get('asin'):
            return {}, in_cache
        return self._build_audnex_book_dict(data), in_cache

    @staticmethod
    def _build_audnex_book_dict(data):
        """ Parse a single-book response from the Audnex API into the standard bookdict layout """
        dic = {':': '.', '"': ''}
        bookname = replace_all(data.get('title', ''), dic).strip()

        authorname = ''
        authorid = ''
        contributors = []
        authors = data.get('authors') or []
        if authors:
            authorname, _ = lazylibrarian.importer.get_preferred_author(authors[0].get('name', ''))
            # native asin (if provided) used as a placeholder id, resolved to a real
            # authorid later by validate_bookdict's name lookup
            authorid = authors[0].get('asin') or authorname
            for extra in authors[1:]:
                a_name, _ = lazylibrarian.importer.get_preferred_author(extra.get('name', ''))
                contributors.append(['', a_name, 'author'])

        series = []
        seriesprimary = data.get('seriesPrimary')
        if seriesprimary and seriesprimary.get('name'):
            series = [(seriesprimary['name'], f"AU{seriesprimary.get('asin') or data.get('asin', '')}",
                      seriesprimary.get('position', ''))]

        return {
            'authorname': authorname,
            'authorid': authorid,
            'bookid': data.get('asin', ''),
            'bookname': bookname,
            'booksub': data.get('subtitle', ''),
            'bookisbn': data.get('isbn', ''),
            'bookpub': data.get('publisherName', ''),
            'bookdate': (data.get('releaseDate') or '')[:10],
            'booklang': (data.get('language') or 'English').title(),
            'booklink': f"https://www.audible.com/pd/{data.get('asin', '')}",
            'bookrate': check_float(data.get('rating'), 0.0),
            'bookrate_count': 0,
            'bookimg': data.get('image') or 'images/nocover.png',
            'bookpages': str(data.get('runtimeLengthMin', 0)),
            'bookgenre': ', '.join(g.get('name', '') for g in data.get('genres', []) if g.get('name')),
            'bookdesc': data.get('description') or data.get('summary', ''),
            'contributors': contributors,
            'series': series,
            'source': 'Audible',
        }

    def add_bookid_to_db(self, bookid=None, bookstatus=None, audiostatus=None,
                         reason='au.add_bookid_to_db', bookdict=None):
        """ Given a bookid from Audible, add the book to the database (and author if not already there) """
        if not bookdict:
            bookdict, _ = self.get_bookdict_for_bookid(bookid)
        if not bookdict:
            self.logger.warning(f"No Audible metadata for {bookid}, unable to add book")
            return False
        if not bookstatus:
            bookstatus = CONFIG['NEWBOOK_STATUS']
        if not audiostatus:
            audiostatus = CONFIG['NEWAUDIO_STATUS']
        bookdict['status'] = bookstatus
        bookdict['audiostatus'] = audiostatus
        bookdict, rejected = validate_bookdict(bookdict)

        if rejected:
            if reason.startswith("Series:") or 'bookname' not in bookdict or 'authorname' not in bookdict:
                return False
            for reject in rejected:
                if reject[0] == 'name':
                    return False
        warn_about_bookdict(bookdict)

        bookdict['status'] = bookstatus
        bookdict['audiostatus'] = audiostatus
        bookdict['reason'] = reason
        res = add_bookdict_to_db(bookdict)
        lazylibrarian.importer.update_totals(bookdict['authorid'])
        return res
