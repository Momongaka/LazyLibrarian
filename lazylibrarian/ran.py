import json
import logging
import traceback
from queue import Queue

import requests
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
from lazylibrarian.common import get_user_agent
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import DIRS, syspath
from lazylibrarian.formatter import (
    format_author_name,
    get_list,
    is_valid_isbn,
    plural,
    replace_all,
    strip_quotes,
    unaccented,
)
from lazylibrarian.provider_utils import get_hashed_filename, is_in_cache, read_from_cache


class RanobeDB:
    def __init__(self):
        self.base_url = 'https://ranobedb.org/api/v0'
        self.image_url = 'https://images.ranobedb.org/'
        return

    logger = logging.getLogger(__name__)
    searchinglogger = logging.getLogger('special.searching')
    cachelogger = logging.getLogger('special.cache')
    active = True
    providername = 'ranobedb'


    def get_author_books(self, authorid=None, authorname=None, bookstatus="Skipped",
                         audiostatus="Skipped", entrystatus='Active', refresh=False, reason=''):
        """
        Given an authorid and/or authorname, find all books for that author and add them to the database
        Statuses may be passed in, or None to use the configured defaults
        Author should be marked as "Loading" while searching
        entrystatus is the author status that should be set when completed
        refresh may be used to optionally cache the results
        reason should indicate why the author or books are being added
        No return value
        To find the author_id, in those providers that have author_id, where possible use author/title
        combination, ie find me the authorid of the author of this book, rather than relying on name only
        Individual books may be added using add_bookid_to_db (see below)
        If no author_id (not all providers have them), could use find_results (see below)
        with a searchterm of "<ll>author name"

        ranobedb.org/api/v0/books?staff=authorid

        """

        if not CONFIG['RAN_API']:
            self.logger.warning(f'{self.__class__.__name__} API not enabled, check config')
            return

        if not reason:
            reason = f'{self.__class__.__name__}.get_author_books'
        db = database.DBConnection()
        auth_id = authorid
        entryreason = reason
        auth_name = authorname
        try:
            if authorid:
                res = db.match('SELECT AuthorName from authors WHERE Authorid=?', (authorid, ))
                if res:
                    auth_name = res['AuthorName']
            else:
                auth_name, auth_id = lazylibrarian.importer.get_preferred_author(authorname)

            self.logger.debug(f'[{auth_id}:{auth_name}] Now processing books with {self.__class__.__name__} API')
            # Artist is loading
            db.action("UPDATE authors SET Status='Loading' WHERE AuthorID=?", (auth_id,))

            res = db.match('SELECT ran_id from authors WHERE Authorid=?', (auth_id, ))
            resultqueue = Queue()
            searchterm = f"/books?staff={res['ran_id']}"
            if not self.find_results(searchterm, resultqueue, authorname=auth_name):
                self.logger.warning(f"No results from {self.__class__.__name__} for {auth_name}")
                return

            _ = add_author_books_to_db(resultqueue, bookstatus, audiostatus, entrystatus, entryreason,
                                       auth_id, self.get_series_members, self.get_bookdict_for_bookid, cache_hits=0)

        except Exception:
            self.logger.error(f'Unhandled exception in {self.__class__.__name__}_get_author_books: {traceback.format_exc()}')
        finally:
            db.action("UPDATE authors SET Status=? WHERE AuthorID=?", (entrystatus, auth_id,))
            db.close()


    def find_results(self, searchterm='', queue=None, authorname=''):
        """
        Searchterm may be passed as a searchterm, isbn, title, author, or title<ll>author
        (isbn13 only, 13 digits starting 978 or 979)
        On return, queue should contain a list of dicts in the standard layout (see below)
        No return value

        ranobedb.org/api/v0/books?title goes here[&staff=authorid]

        /books to get bookid, title, lang, image/filename
        /book/id to get description, staff, series, publishers, tags (genres)
        /series?q=series_name to get seriesid
        /series/id to get books in series, bookids, order, image, staff etc
        /staff?q=name to get id from name
        /staff/id to get website links for born/died etc
        """
        if not CONFIG['RAN_API']:
            self.logger.warning(f'{self.__class__.__name__} API not enabled, check config')
            return False
        try:
            resultlist = []
            resultcount = 0
            ignored = 0
            no_author_count = 0
            title = ''

            if '<ll>' in searchterm:  # special token separates title from author
                title, authorname = searchterm.split('<ll>')
            if not searchterm.startswith('/'):
                if authorname and title:
                    searchterm = searchterm.replace('<ll>', ' ')
                    searchterm = f"/books?q={searchterm}"
                elif authorname:
                    searchterm = f"/staff?q={authorname}"
                elif title:
                    searchterm = f"/books?q={title}"
                else:
                    # default to booke
                    searchterm = f"/books?q={searchterm}"

            # strip all ascii and non-ascii quotes/apostrophes
            searchterm = strip_quotes(searchterm)

            self.logger.debug(f'Now searching {self.__class__.__name__} with searchterm: {searchterm}')
            searchterm = searchterm.lstrip('/')
            results, in_cache = self._result_from_cache(searchterm)

            if results and 'books' in results:
                self.logger.debug(f"Search returned {len(results['books'])}")
                for item in results['books']:
                    # for each book, call /book/id to get full info
                    booksearchterm = f"book/{item['id']}"
                    book_result, in_cache = self._result_from_cache(booksearchterm)
                    book = self._build_book_dict(book_result.get('book'))

                    if not book['authorname']:
                        self.logger.debug('Skipped a result without authorfield.')
                        no_author_count += 1
                        continue
                    if not book['bookname']:
                        self.logger.debug('Skipped a result without title.')
                        continue
                    valid_langs = get_list(CONFIG['IMP_PREFLANG'])
                    if valid_langs and "All" not in valid_langs:  # don't care about languages, accept all
                        try:
                            # skip if no language in valid list -
                            if not ['booklang']:
                                self.logger.debug(f"Skipped {book['bookname']} with no language")
                                ignored += 1
                                continue
                            if book['booklang'] not in valid_langs:
                                self.logger.debug(f"Skipped {book['bookname']} with language {book['booklang']}")
                                ignored += 1
                                continue
                        except KeyError:
                            ignored += 1
                            self.logger.debug(f"Skipped {book['bookname']} where no language is found")
                            continue

                    if authorname:
                        author_fuzz = fuzz.token_sort_ratio(book['authorname'], authorname)
                    else:
                        author_fuzz = fuzz.token_sort_ratio(book['authorname'], searchterm)

                    if title:
                        if title.endswith(')'):
                            title = title.rsplit('(', 1)[0]
                        book_fuzz = fuzz.token_set_ratio(book['bookname'].lower(), title.lower())
                        # lose a point for each extra word in the fuzzy matches so we get the closest match
                        words = len(get_list(book['bookname']))
                        words -= len(get_list(title))
                        book_fuzz -= abs(words)
                    else:
                        book_fuzz = fuzz.token_set_ratio(book['bookname'].lower(), searchterm.lower())
                    isbn_fuzz = 0
                    if is_valid_isbn(searchterm):
                        isbn_fuzz = 100
                    highest_fuzz = max((author_fuzz + book_fuzz) / 2, isbn_fuzz)

                    dic = {':': '.', '"': '', '\'': ''}
                    bookname = replace_all(book['bookname'], dic)

                    bookname = unaccented(bookname, only_ascii=False)

                    author_id = ''
                    if book['authorname']:
                        db = database.DBConnection()
                        match = db.match('SELECT AuthorID FROM authors WHERE AuthorName=?', (authorname,))
                        if match:
                            author_id = match['AuthorID']
                        db.close()

                    resultlist.append({
                        'authorname': book['authorname'],
                        'authorid': author_id,
                        'bookid': str(book['bookid']),
                        'bookname': bookname,
                        'booksub': book['booksub'],
                        'bookisbn': book['bookisbn'],
                        'bookpub': ','.join(book['bookpub']),
                        'bookdate': str(book['bookdate']),
                        'booklang': book['booklang'],
                        'booklink': book['booklink'],
                        'bookrate': float(book['bookrate']),
                        'bookrate_count': book['bookrate_count'],
                        'bookimg': book['bookimg'],
                        'bookpages': book['bookpages'],
                        'bookgenre': book['bookgenre'],
                        'bookdesc': book['bookdesc'],
                        'author_fuzz': author_fuzz,
                        'book_fuzz': book_fuzz,
                        'isbn_fuzz': isbn_fuzz,
                        'highest_fuzz': highest_fuzz,
                        'contributors': book['contributors'],
                        'series': book['series'],
                        'source': 'RanobeDB'
                    })

                    resultcount += 1

            self.logger.debug(
                f"Returning {resultcount} {plural(resultcount, 'result')} for {searchterm}")

            self.logger.debug(f"Removed {ignored} unwanted language {plural(ignored, 'result')}")
            self.logger.debug(f"Removed {no_author_count} {plural(no_author_count, 'book')} with no author")
            queue.put(resultlist)
            return len(resultlist)
        except Exception:
            self.logger.error(f'Unhandled exception in {self.__class__.__name__}.find_results: {traceback.format_exc()}')
            return 0

    def _build_book_dict(self, book):
        """ Return all the book info we need as a dictionary or default value if no key """
        mydict = {'authorname': '', 'authorid': '', 'bookdesc': '', 'booklink': '', 'bookisbn': '',
                  'bookimg': 'images/nocover.png', 'bookpages': 0}
        mydict['bookid'] = book.get('id', '')
        mydict['bookname'] = book.get('romaji') if book.get('romaji') else book.get('title')
        if book.get('image') and book['image'].get('filename'):
            mydict['bookimg'] = self.image_url + book['image']['filename']
        mydict['bookdesc'] = book.get('description', '')
        if not mydict['bookdesc']:
            mydict['bookdesc'] = book.get('description_ja', '')
        mydict['booklang'] = book.get('lang', '')
        if not mydict['booklang']:
            mydict['booklang'] = book.get('olang', '')
        mydict['bookdate'] = book.get('c_release_date', '')
        mydict['bookrate'] = book.get('rating') if book.get('rating') else 0
        if mydict['bookrate'] and book.get('num_reviews'):
            mydict['bookrate_count'] = book['num_reviews'].get('count', 0)
        if 'score' in mydict['bookrate'] and 'count' in mydict['bookrate']:
            mydict['bookrate_count'] = mydict['bookrate']['count']
            mydict['bookrate'] = mydict['bookrate']['score']
        contributors = []
        # build a list of contributors (id, name, role)
        for edition in book.get('editions'):
            for staff in edition.get('staff'):
                # use first author found as primary
                if not mydict['authorname'] and staff['role_type'] == 'author':
                    aname = staff.get('romaji') if staff.get('romaji') else staff.get('name', '')
                    mydict['authorname'], _ = lazylibrarian.importer.get_preferred_author(aname)
                    mydict['authorid'] = staff['staff_id']
                else:
                    aname = staff.get('romaji') if staff.get('romaji') else staff.get('name', '')
                    a_name, _ = lazylibrarian.importer.get_preferred_author(aname)
                    contributors.append([staff['staff_id'], a_name, staff['role_type']])
        mydict['contributors'] = contributors

        for release in book.get('releases'):
            if not mydict['bookisbn'] and release.get('isbn13'):
                mydict['bookisbn'] = release.get('isbn13')
            if not mydict['bookdesc'] and release.get('description'):
                mydict['bookdesc'] = release.get('description')
            if not mydict['booklink'] and release.get('website'):
                mydict['booklink'] = release.get('website')

        publishers = []
        for pub in book.get('publishers'):
            if pub.get('romaji'):
                publishers.append(pub['romaji'])
        mydict['bookpub'] = publishers

        mydict['bookgenre'] = book.get('tags', '')

        if book.get('series') and 'books' in book['series']:
            if book['series'].get('title'):
                mydict['series_name'] = book['series'].get('title')
            else:
                mydict['series_name'] = book['series'].get('romaji')
            mydict['series_id'] = book['series'].get('id')

            searchterm = f"series/{mydict['series_id']}"
            index = None
            series_result, in_cache = self._result_from_cache(searchterm)
            if series_result:
                for bk in series_result['series']['books']:
                    if bk['id'] == mydict['bookid'] and bk.get('sort_order'):
                        index = bk['sort_order']
                        break

            # not all series provide an index, so use position in book list
            if index is None:
                for bk, ind in enumerate(book['series']['books'], start=1):
                    if bk.get('id') == mydict['bookid']:
                        index = ind
                        break
            if index:
                mydict['series_index'] = index

        # massage into a standard layout across all providers
        mydict['booksub'] = ''
        if mydict['bookname'] and ':' in mydict['bookname']:
            title, subtitle = mydict['bookname'].split(':', 1)
            mydict['bookname'] = title.strip()
            mydict['booksub'] = subtitle.strip()

        if isinstance(mydict['bookgenre'], list):
            if lazylibrarian.GRGENRES:
                genre_limit = lazylibrarian.GRGENRES.get('genreLimit', 3)
            else:
                genre_limit = 3
            mydict['bookgenre'] = ','.join(mydict['bookgenre'][:genre_limit])

        mydict['series'] = []
        if mydict['series_name'] and mydict['series_index']:
            # might have no series_id, in which case use bookid.
            # we can merge the series together in the database by matching series_name where series_id starts with provider
            # so the series id becomes the bookid of the first book in the series added to the database
            if not mydict.get('series_id', ''):
                mydict['series_id'] = mydict['bookid']
            mydict['series'] = [(mydict['series_name'], f"{self.__class__.__name__[:2]}{mydict['series_id']}", mydict['series_index'])]
        mydict['source'] = self.__class__.__name__
        return mydict

    def get_author_image(self, authorid=None, authorname=None):
        res = self.get_author_info(authorid=authorid, authorname=authorname)
        return res.get('authorimg', '')

    def find_author_id(self, authorname=None, title=None, refresh=False):
        res = self.get_author_info(authorid=None, authorname=authorname, refresh=refresh)
        return res

    def get_author_info(self, authorid=None, authorname=None, refresh=False):
        """Get detailed info for an author."""
        api_hits = 0
        cache_hits = 0

        self.logger.debug(f"Getting {self.__class__.__name__} author info for {authorid}:{authorname}, refresh={refresh}")
        author_name = ''
        author_born = ''
        author_died = ''
        author_link = ''
        author_id = ''
        author_img = ''
        about = ''
        totalbooks = 0

        if authorid:
            searchcmd = f'staff/{authorid}'
        elif authorname:
            searchcmd = f'staff?q={authorname}'
        else:
            return {}
        results, in_cache = self._result_from_cache(searchcmd, refresh=refresh)
        if in_cache:
            cache_hits += 1
        else:
            api_hits += 1
        if not results or not results.get('staff'):
            return {}
        if authorid:
            item = results['staff']
            if item['id'] == authorid:
                if item['romaji']:
                    author_name = item['romaji']
                else:
                    author_name = item['name']
                author_id = authorid
                about = item['description']
                if item['website']:
                    author_link = item['website']
        if authorname and not authorid:
            for item in results['staff']:
                if item['name'] == authorname or item['romaji'] == authorname:
                    author_id = item['id']
                    author_name = authorname
                    break
            searchcmd = f'staff/{author_id}'
            results, in_cache = self._result_from_cache(searchcmd, refresh=refresh)
            if in_cache:
                cache_hits += 1
            else:
                api_hits += 1
            if not results or not results.get('staff'):
                return {}

            item = results.get('staff')
            authorid = item['id']
            about = item['description']
            if item['website']:
                author_link = item['website']

        author_name = authorname
        if "," in author_name:
            postfix = get_list(CONFIG.get_csv('NAME_POSTFIX'))
            words = author_name.split(',')
            if len(words) == 2:
                if words[0].strip().strip('.').lower in postfix:
                    author_name = f"{words[1].strip()} {words[0].strip()}"
                else:
                    author_name = author_name.split(',')[0]

        if not author_name:
            self.logger.warning(f"Rejecting authorid {authorid}, no authorname")
            return {}

        self.logger.debug(f"[{author_name}] Returning {self.__class__.__name__} info for authorID: {authorid}")
        author_dict = {
            'authorid': str(authorid),
            'authorlink': author_link,
            'authorborn': author_born,
            'authordeath': author_died,
            'authorimg': author_img,
            'about': about,
            'totalbooks': totalbooks,
            'authorname': format_author_name(author_name, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
        }
        self.logger.debug(f"AuthorInfo used {api_hits} api hit, {cache_hits} in cache")
        return author_dict


    def add_bookid_to_db(self, bookid=None, bookstatus=None, audiostatus=None, reason='ran.add_bookid_to_db', bookdict=None):
        """
        Given a bookid from this provider, add the book to the database (and author if not already there)
        Statuses and reason as above
        No return value
        """
        if not bookdict:
            bookdict, _ = self.get_bookdict_for_bookid(bookid)
        if not bookdict:
            self.logger.warning(f"No RanobeDB metadata for {bookid}, unable to add book")
            return False
        if not bookstatus:
            bookstatus = CONFIG['NEWBOOK_STATUS']
            self.logger.debug(f"No bookstatus passed, using default {bookstatus}")
        if not audiostatus:
            audiostatus = CONFIG['NEWAUDIO_STATUS']
            self.logger.debug(f"No audiostatus passed, using default {audiostatus}")
        self.logger.debug(f"bookstatus={bookstatus}, audiostatus={audiostatus}")
        bookdict['status'] = bookstatus
        bookdict['audiostatus'] = audiostatus
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
        bookdict['reason'] = reason
        res = add_bookdict_to_db(bookdict)
        lazylibrarian.importer.update_totals(bookdict['authorid'])
        return res


    def get_series_members(self, series_id, series_title, queue, refresh):
        """
        Find details of all series members using id or title, add members to queue
        Queue format is a list of tuples [position, title, author_name, author_id, book_id, pubdate]
        sorted on ascending position in the series
        """
        resultlist = []
        author_name = ''
        api_hits = 0
        cache_hits = 0

        if not series_id:
            searchcmd = f'series?q={series_title}'
            results, in_cache = self._result_from_cache(searchcmd, refresh=refresh)
            if in_cache:
                cache_hits += 1
            else:
                api_hits += 1
            if not results or not results.get('series'):
                return {}
            ser = results['series'][0]
            series_id = ser['id']
            series_title = ser['title']
            if not series_title:
                series_title = ser['romaji']
            if not series_title:
                series_title = ser['romaji_orig']
            if not series_title:
                series_title = ''

        if not series_id:
            return {}

        searchcmd = f'series/{series_id}'
        results, in_cache = self._result_from_cache(searchcmd, refresh=refresh)
        if in_cache:
            cache_hits += 1
        else:
            api_hits += 1
        if not results or not results.get('series') or not results['series'].get('books'):
            return {}

        for item in results['series']['books']:
            # missing items need lookup in database or call book/id
            position = item.get('sort_order', 0)
            title = item.get('title')
            if not title:
                title = item.get('romaji')
            if not title:
                title = item.get('romaji_orig')
            if not title:
                title = ''
            bookid = item.get('id', '')
            pubdate = item.get('c_release_date', '')

            bookdict, _ = self.get_bookdict_for_bookid(bookid)
            author_name = bookdict.get('authorname', '')
            author_id = bookdict.get('authorid', '')

            resultlist.append((position, title, author_name, author_id, bookid, pubdate))

        resultlist = sorted(resultlist)
        self.logger.debug(f"Found {len(resultlist)} for series {series_id}: {series_title}")
        self.logger.debug(f"Used {api_hits} api hit, {cache_hits} in cache")

        if not queue:
            return resultlist

        queue.put(resultlist)
        return None


    def get_bookdict_for_bookid(self, bookid):
        """
        Gather the data but don't add to database
        """
        searchterm = f"book/{bookid}"
        results, in_cache = self._result_from_cache(searchterm, refresh=False)
        bookdict = {}
        if 'error' in results:
            self.logger.error(str(results['error']))
        if 'book' in results:
            bookdict = self._build_book_dict(results['book'])
        return bookdict, in_cache



    def _result_from_cache(self, searchcmd: str, refresh=False) -> (str, bool):
        """Get API result from cache or fetch if needed."""
        headers = {'Content-Type': 'application/json',
                   'User-Agent': get_user_agent(),
                   }
        cache_location = DIRS.get_cachedir('JSONCache')
        filename = f"{self.base_url}/{searchcmd}"
        hashfilename, myhash = get_hashed_filename(cache_location, filename)
        # CACHE_AGE is in days, so get it to seconds
        expire_older_than = CONFIG.get_int('CACHE_AGE') * 24 * 60 * 60
        valid_cache = is_in_cache(expire_older_than, hashfilename, myhash)
        if valid_cache and not refresh:
            lazylibrarian.CACHE_HIT += 1
            self.cachelogger.debug(f"CacheHandler: Returning CACHED response {hashfilename}")
            source, ok = read_from_cache(hashfilename)
            if ok:
                res = json.loads(source)
            else:
                res = {}
            return res, True

        lazylibrarian.CACHE_MISS += 1
        if BLOCKHANDLER.is_blocked(self.providername):
            return {}, False
        # send query, cache result and return it
        url = f"{self.base_url}/{searchcmd}"
        r = requests.get(url, headers=headers)
        success = str(r.status_code).startswith('2')

        if success:
            res = r.json()
            self.cachelogger.debug(f"CacheHandler: Storing json {myhash}")
            with open(syspath(hashfilename), "w") as cachefile:
                cachefile.write(json.dumps(res))
            return res, False

        delay = 2 * 3600
        msg = f"{self.__class__.__name__} error {r.status_code}"
        self.logger.error(msg)
        BLOCKHANDLER.block_provider(self.providername, msg, delay=delay)
        return {}, False

