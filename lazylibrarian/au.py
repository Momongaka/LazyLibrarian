import logging
import time
import traceback
from urllib.parse import quote, quote_plus, urlencode

import requests
import unicodedata
from rapidfuzz import fuzz

import lazylibrarian
from lazylibrarian import database, ROLE
from lazylibrarian.bookwork import (
    get_work_series, delete_empty_series, set_series, get_status,
    audible_book_dict, isbnlang, is_set_or_part, google_book_dict
)
from lazylibrarian.cache import json_request, gr_xml_request
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import (today, replace_all, unaccented, is_valid_isbn,
                                     get_list, clean_name, make_unicode, make_utf8bytes, strip_quotes,
                                     thread_name, plural, format_author_name, check_float
                                     )
from lazylibrarian.hc import HardCover
from lazylibrarian.images import cache_bookimg, get_book_cover
from lazylibrarian.ol import OpenLibrary


class Audible:
    def __init__(self, name=None):
        self.name = make_unicode(name)  # keep consistent with GR
        self.logger = logging.getLogger(__name__)
        self.loggersearching = logging.getLogger('special.searching')
        # Audible doesn't require an API key, so no warning needed
        self.params = {}  # placeholder if needed later for query params

    def find_results(self, searchterm=None, queue=None):
        """
        Search Audible for a term (author/title) and return results in the same format
        as GR.find_results, for queue.put.
        """
        try:
            resultlist = []
            api_hits = 0
            searchtitle = ''
            searchauthorname = ''

            if ' <ll> ' in searchterm:  # special token separates title from author
                searchtitle, searchauthorname = searchterm.split(' <ll> ')
                searchterm = searchterm.replace(' <ll> ', ' ')
                searchtitle = searchtitle.split(' (')[0]  # remove series info

            self.logger.debug(f'Now searching Audible API with searchterm: {searchterm}')
            url = (
                f"https://api.audible.com/1.0/catalog/products?"
                f"author={quote_plus(searchterm)}&keywords=english"
                f"&response_groups=product_attrs,product_extended_attrs,product_desc,series"
                f"&products_sort_by=ReleaseDate&num_results=50"
            )
            self.loggersearching.debug(url)

            try:
                resp = requests.get(url)
                api_hits += 1
                if resp.status_code != 200:
                    self.logger.warning(f'Audible search returned status {resp.status_code}')
                    queue.put(resultlist)
                    return

                data = resp.json()
            except Exception as e:
                self.logger.error(f"{type(e).__name__} fetching Audible results: {str(e)}")
                queue.put(resultlist)
                return

            for product in data.get('products', []):
                bookid = product.get('asin', '')
                book_title = product.get('publication_name', '')
                bookdesc = product.get('merchandising_summary') or product.get('publisher_summary', '')
                bookdate = product.get('release_date') or product.get('issue_date', '')
                booklang = product.get('language', 'English')
                bookpages = str(product.get('runtime_length_min', 0))
                booklink = f"https://www.audible.com{product.get('series', [{}])[0].get('url', '')}"
                author_name_result = searchterm  # Audible does not return separate author field

                # Fuzzy matching
                author_fuzz = fuzz.token_sort_ratio(author_name_result, searchauthorname or searchterm)
                book_fuzz = fuzz.token_set_ratio(book_title, searchtitle or searchterm)
                words = len(get_list(book_title)) - len(get_list(searchtitle or searchterm))
                book_fuzz -= abs(words)
                highest_fuzz = max((author_fuzz + book_fuzz) / 2, 0)

                sequence = product.get('series', [{}])[0].get('sequence', '')

                resultlist.append({
                    'authorname': author_name_result,
                    'authorid': author_name_result,
                    'bookid': bookid,
                    'bookname': book_title,
                    'booksub': '',
                    'bookisbn': '',
                    'bookpub': '',
                    'bookdate': bookdate,
                    'booklang': booklang,
                    'booklink': booklink,
                    'bookrate': 0.0,
                    'bookrate_count': 0,
                    'bookimg': 'images/nocover.png',
                    'bookpages': bookpages,
                    'bookgenre': '',
                    'bookdesc': bookdesc,
                    'workid': '',
                    'author_fuzz': round(author_fuzz, 2),
                    'book_fuzz': round(book_fuzz, 2),
                    'isbn_fuzz': 0,
                    'highest_fuzz': round(highest_fuzz, 2),
                    'source': 'Audible',
                    'sequence': sequence
                })

            self.logger.debug(f"Found {len(resultlist)} {plural(len(resultlist), 'result')} with keyword: {searchterm}")
            self.logger.debug(f"Audible API was hit {api_hits} {plural(api_hits, 'time')} for keyword {searchterm}")

            queue.put(resultlist)

        except Exception:
            self.logger.error(f'Unhandled exception in Audible.find_results: {traceback.format_exc()}')
            queue.put(resultlist)

    def find_author_id(self, refresh=False):
        """
        Get the Audible author ID (via Audnex API) for self.name.
        Falls back to Goodreads API if needed.
        """
        author = self.name
        if '<ll>' in author:
            author, _ = author.split('<ll>')
        author = format_author_name(
            unaccented(author, only_ascii='_'),
            postfix=get_list(CONFIG.get_csv('NAME_POSTFIX'))
        )
        author = make_unicode(author)
        author = unicodedata.normalize('NFC', author)

        self.logger.debug(f"Getting author id for {author}, refresh={refresh}")

        # --- Step 1: Try Audible/Audnex API first ---
        try:
            url = f"https://api.audnex.us/authors?name={quote_plus(author)}&region=us"
            self.loggersearching.debug(url)
            resp = requests.get(url)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    asin = data[0].get("asin")
                    name = data[0].get("name")
                    if asin and name:
                        self.logger.info(f"Found Audible author: {name} [{asin}]")
                        return {
                            "authorid": asin,
                            "authorname": name,
                            "source": "Audible"
                        }
            else:
                self.logger.warning(f"Audnex API returned status {resp.status_code}")
        except Exception as e:
            self.logger.error(f"{type(e).__name__} fetching Audnex author id: {str(e)}")

        # --- Step 2: Fall back to Goodreads ---
        url = '/'.join([CONFIG['GR_URL'], 'api/author_url/'])
        try:
            url += f"{quote(make_utf8bytes(author)[0])}?{urlencode(self.params)}"
            self.loggersearching.debug(url)
            rootxml, _ = gr_xml_request(url, use_cache=not refresh)
        except Exception as e:
            self.logger.error(f"{type(e).__name__} finding authorid: {url}, {str(e)}")
            return {}

        if rootxml is None:
            self.logger.debug("Error requesting authorid")
            return {}

        resultxml = rootxml.iter('author')
        if resultxml is None:
            self.logger.warning(f'No authors found with name: {author}')
            return {}

        for res in resultxml:
            authorid = res.attrib.get("id")
            authorname = res.find('name').text
            authorname = format_author_name(
                unaccented(authorname, only_ascii=False),
                postfix=get_list(CONFIG.get_csv('NAME_POSTFIX'))
            )

            match = fuzz.ratio(author, authorname)
            if match >= CONFIG.get_int('NAME_RATIO'):
                return self.get_author_info(authorid)

            match = fuzz.partial_ratio(author, authorname)
            if match >= CONFIG.get_int('NAME_PARTNAME'):
                return self.get_author_info(authorid)

            self.logger.debug(f"Fuzz failed: {round(match, 2)} [{author}][{authorname}]")

        return {}

    def get_author_info(self, authorid=None):

        url = '/'.join([CONFIG['GR_URL'],
                        f"author/show/{authorid}.xml?{urlencode(self.params)}"])

        try:
            self.loggersearching.debug(url)
            rootxml, _ = gr_xml_request(url)
        except Exception as e:
            self.logger.error(f"{type(e).__name__} getting author info: {str(e)}")
            return {}
        if rootxml is None:
            self.logger.debug(f"Failed to get author info for {authorid}")
            return {}

        resultxml = rootxml.find('author')
        if resultxml is None:
            self.logger.warning(f"No author found with ID: {authorid}")
            return {}

        # added authorname to author_dict - this holds the intact name preferred by GR
        # except GR messes up names like "L. E. Modesitt, Jr." where it returns <name>Jr., L. E. Modesitt</name>
        authorname = format_author_name(resultxml[1].text, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
        self.logger.debug(f"[{authorname}] Returning GR info for authorID: {authorid}")
        author_dict = {
            'authorid': resultxml[0].text,
            'authorlink': resultxml.find('link').text,
            'authorimg': resultxml.find('image_url').text,
            'authorborn': resultxml.find('born_at').text,
            'authordeath': resultxml.find('died_at').text,
            'about': resultxml.find('about').text,
            'totalbooks': resultxml.find('works_count').text,
            'authorname': authorname
        }
        return author_dict

    @staticmethod
    def get_bookdict(book):
        """ Return all the book info we need as a dictionary or default value if no key """
        mydict = {}
        for val, idx, default in [
            ('name', 'title', ''),
            ('shortname', 'title_without_series', ''),
            ('id', 'id', ''),
            ('desc', 'description', ''),
            ('pub', 'publisher', ''),
            ('link', 'link', ''),
            ('rate', 'average_rating', 0.0),
            ('pages', 'num_pages', 0),
            ('pub_year', 'publication_year', '0000'),
            ('pub_month', 'publication_month', '0'),
            ('pub_day', 'publication_day', '0'),
            ('workid', 'work/id', ''),
            ('isbn13', 'isbn13', ''),
            ('isbn10', 'isbn', ''),
            ('img', 'image_url', '')
        ]:

            value = default
            res = book.find(idx)
            if res is not None:
                value = res.text
            if value is None:
                value = default
            if idx == 'rate':
                value = check_float(value, 0.0)
            mydict[val] = value

        return mydict

    def get_author_books(self, authorid=None, authorname=None, bookstatus="Skipped",
                         audiostatus="Skipped", entrystatus='Active', refresh=False,
                         reason='audible.get_author_books'):

        self.logger.debug(f'[{authorname}] Now processing books with Audible API')
        db = database.DBConnection()
        try:
            api_hits = 0
            total_count = 0
            added_count = 0
            updated_count = 0
            duplicates = 0
            bad_lang = 0
            book_ignore_count = 0
            removed_results = 0
            locked_count = 0

            db.action("UPDATE authors SET Status='Loading' WHERE AuthorID=?", (authorid,))

            page = 1
            while True:
                url = (f"https://api.audible.com/1.0/catalog/products?"
                       f"author={quote_plus(authorname)}&keywords=english"
                       f"&response_groups=product_attrs,product_extended_attrs,"
                       f"product_desc,series,rating&products_sort_by=ReleaseDate"
                       f"&num_results=50&page={page}")
                self.loggersearching.debug(f"Fetching Audible URL: {url}")

                try:
                    resp = requests.get(url)
                    api_hits += 1
                    if resp.status_code != 200:
                        self.logger.warning(f'Audible API returned status {resp.status_code}')
                        break
                    data = resp.json()
                except Exception as e:
                    self.logger.error(f"{type(e).__name__} fetching Audible results: {str(e)}")
                    break

                books = data.get('products', [])
                if not books:
                    break

                for product in books:
                    total_count += 1
                    asin = product.get('asin')
                    bookname = product.get('publication_name') or product.get('title') or ''
                    subtitle = product.get('subtitle') or ''
                    bookdesc = product.get('merchandising_summary') or product.get('publisher_summary', '')
                    bookdate = product.get('release_date') or product.get('issue_date', '')
                    booklang = product.get('language', 'Unknown').capitalize()
                    bookpages = str(product.get('runtime_length_min', 0))
                    bookimg = product.get('image_url', 'images/nocover.png')
                    booklink = f"https://www.audible.com/pd/{asin}"
                    workid = ''
                    serieslist = []
                    if product.get('series'):
                        series = product['series'][0]
                        serieslist = [('', series.get('sequence', ''), series.get('title', ''))]

                    # ratings
                    rating = product.get('rating', {})
                    overall = rating.get('overall_distribution', {})
                    bookrate = float(overall.get('average_rating', 0.0))
                    bookrate_count = overall.get('num_ratings', 0)

                    # validate + reject checks here (similar to GB version)
                    rejected = []
                    if not bookname:
                        rejected.append(['name', 'No bookname'])
                    if booklang not in get_list(CONFIG['IMP_PREFLANG'], ',') and "All" not in CONFIG['IMP_PREFLANG']:
                        rejected.append(['lang', f'Invalid language [{booklang}]'])

                    if rejected:
                        # count + log reasons
                        continue

                    # fallback to Google Books if missing fields
                    if (not bookimg or 'nocover' in bookimg) or not product.get('isbn'):
                        gbdata = get_book_cover(authorname, bookname)  # helper you already have
                        if gbdata:
                            if (not bookimg or 'nocover' in bookimg) and gbdata.get('img'):
                                bookimg = gbdata['img']
                            if not product.get('isbn') and gbdata.get('isbn'):
                                bookisbn = gbdata['isbn']
                            else:
                                bookisbn = ''
                        else:
                            bookisbn = ''
                    else:
                        bookisbn = ''

                    # db upsert
                    control_value_dict = {"BookID": asin}
                    new_value_dict = {
                        "AuthorID": authorid,
                        "BookName": bookname,
                        "BookSub": subtitle,
                        "BookDesc": bookdesc,
                        "BookIsbn": bookisbn,
                        "BookPub": '',
                        "BookGenre": '',
                        "BookImg": bookimg,
                        "BookLink": booklink,
                        "BookRate": bookrate,
                        "BookRateCount": bookrate_count,
                        "BookPages": bookpages,
                        "BookDate": bookdate,
                        "BookLang": booklang,
                        "Status": bookstatus,
                        "AudioStatus": audiostatus,
                        "BookAdded": today(),
                        "WorkID": workid,
                        "ScanResult": reason,
                        "aud_id": asin
                    }

                    db.upsert("books", new_value_dict, control_value_dict)
                    self.logger.debug(f"[{authorname}] Added Audible book: {bookname} [{booklang}]")
                    added_count += 1

                if 'next_page' in data and data['next_page']:
                    page += 1
                else:
                    break

            # finalize author
            db.upsert("authors", {"Status": entrystatus}, {"AuthorID": authorid})
            self.logger.info(
                f"[{authorname}] Audible books processed: {total_count} total, {added_count} added, {updated_count} updated")

        except Exception:
            self.logger.error(f'Unhandled exception in Audible.get_author_books: {traceback.format_exc()}')
        finally:
            db.close()

    def find_book(self, bookid=None, bookstatus=None, audiostatus=None, reason='find_book'):
        if not bookstatus:
            bookstatus = CONFIG['NEWBOOK_STATUS']
        if not audiostatus:
            audiostatus = CONFIG['NEWAUDIO_STATUS']

        book = None
        source = None

        # --- Try Audible (Audnex) first if ASIN ---
        if bookid and len(bookid) == 10 and bookid.startswith("B0"):
            url = f"https://api.audnex.us/books/{bookid}"
            self.loggersearching.debug(f"Fetching Audible book info: {url}")
            try:
                resp = requests.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("asin"):
                        source = "audible"
                        dic = {':': '.', '"': ''}
                        bookname = replace_all(data.get("title", ""), dic).strip()

                        book = {
                            "id": data.get("asin"),
                            "name": bookname,
                            "sub": data.get("subtitle", ""),
                            "desc": data.get("description") or data.get("summary", ""),
                            "isbn": data.get("isbn", ""),
                            "pub": data.get("publisherName", ""),
                            "genre": ", ".join([g.get("name") for g in data.get("genres", [])]) if data.get(
                                "genres") else "",
                            "img": data.get("image", "images/nocover.png"),
                            "link": f"https://www.audible.com/pd/{data.get('asin')}",
                            "rate": check_float(data.get("rating", 0), 0),
                            "pages": str(data.get("runtimeLengthMin", 0)),
                            "date": data.get("releaseDate", "")[:10] if data.get("releaseDate") else "",
                            "lang": data.get("language", "English").title(),
                            "author": data["authors"][0]["name"] if data.get("authors") else "",
                            "series": data.get("seriesPrimary", {}).get("name", ""),
                            "seriesNum": data.get("seriesPrimary", {}).get("position", "")
                        }
                else:
                    self.logger.warning(f"Audnex API returned {resp.status_code} for {bookid}")
            except Exception as e:
                self.logger.error(f"{type(e).__name__} fetching Audible book {bookid}: {str(e)}")

        # --- Fall back to Google Books if Audible not used or failed ---
        if not book:
            if not CONFIG['GB_API']:
                self.logger.warning('No GoogleBooks API key, check config')
                return
            url = '/'.join([CONFIG['GB_URL'], f"books/v1/volumes/{str(bookid)}?key={CONFIG['GB_API']}"])
            jsonresults, _ = json_request(url)

            if not jsonresults:
                self.logger.debug(f'No results found for {bookid}')
                return

            source = "google"
            book = google_book_dict(jsonresults)
            dic = {':': '.', '"': ''}
            book["name"] = replace_all(book['name'], dic).strip()

        # --- If still no book, stop ---
        if not book or not book.get("author"):
            self.logger.debug(f"Book {bookid} has no valid author field, skipping")
            return

        # --- Language/pubdate/set checks (same as original) ---
        valid_langs = get_list(CONFIG['IMP_PREFLANG'])
        if book['lang'] not in valid_langs and 'All' not in valid_langs:
            msg = f"Book {book['name']} {source} language does not match preference, {book['lang']}"
            self.logger.warning(msg)
            if reason.startswith("Series:"):
                return

        if CONFIG.get_bool('NO_PUBDATE'):
            if not book['date'] or book['date'] == '0000':
                msg = f"Book {book['name']} Publication date does not match preference, {book['date']}"
                self.logger.warning(msg)
                if reason.startswith("Series:"):
                    return

        if CONFIG.get_bool('NO_FUTURE'):
            if book['date'] > today()[:4]:
                msg = f"Book {book['name']} Future publication date does not match preference, {book['date']}"
                self.logger.warning(msg)
                if reason.startswith("Series:"):
                    return

        if CONFIG.get_bool('NO_SETS'):
            is_set, set_msg = is_set_or_part(book['name'])
            if is_set:
                msg = f"Book {book['name']} {set_msg}"
                self.logger.warning(msg)
                if reason.startswith("Series:"):
                    return

        # --- DB handling ---
        db = database.DBConnection()
        try:
            authorname = book['author']
            if CONFIG['BOOK_API'] == "HardCover":
                hc = HardCover(f"{authorname}<ll>{book['name']}")
                author = hc.find_author_id()
            else:
                ol = OpenLibrary(f"{authorname}<ll>{book['name']}")
                author = ol.find_author_id()

            if author:
                author_id = author['authorid']
                match = db.match('SELECT AuthorID from authors WHERE AuthorID=?', (author_id,))
                if not match:
                    match = db.match('SELECT AuthorID from authors WHERE AuthorName=?', (author['authorname'],))
                    if match:
                        self.logger.debug(
                            f"{author['authorname']}: Changing authorid from {author_id} to {match['AuthorID']}")
                        author_id = match['AuthorID']
                    else:
                        newauthor_status = 'Active'
                        if CONFIG['NEWAUTHOR_STATUS'] in ['Skipped', 'Ignored']:
                            newauthor_status = 'Paused'
                        if reason.startswith('Series:'):
                            newauthor_status = 'Paused'
                        control_value_dict = {"AuthorID": author_id}
                        new_value_dict = {
                            "AuthorName": author['authorname'],
                            "AuthorImg": author['authorimg'],
                            "AuthorLink": author['authorlink'],
                            "AuthorBorn": author['authorborn'],
                            "AuthorDeath": author['authordeath'],
                            "DateAdded": today(),
                            "Updated": int(time.time()),
                            "Status": newauthor_status,
                            "Reason": reason
                        }
                        if CONFIG['BOOK_API'] == "HardCover":
                            new_value_dict['hc_id'] = author_id
                        else:
                            new_value_dict['ol_id'] = author_id
                        authorname = author['authorname']
                        db.upsert("authors", new_value_dict, control_value_dict)
                        if CONFIG.get_bool('NEWAUTHOR_BOOKS') and newauthor_status != 'Paused':
                            self.get_author_books(author_id, entrystatus=CONFIG['NEWAUTHOR_STATUS'],
                                                  reason=reason)
            else:
                self.logger.warning(f"No AuthorID for {book['author']}, unable to add book {book['name']}")
                return

            reason = f"[{thread_name()}] {reason}"
            control_value_dict = {"BookID": bookid}
            new_value_dict = {
                "AuthorID": author_id,
                "BookName": book['name'],
                "BookSub": book['sub'],
                "BookDesc": book['desc'],
                "BookIsbn": book['isbn'],
                "BookPub": book['pub'],
                "BookGenre": book['genre'],
                "BookImg": book['img'],
                "BookLink": book['link'],
                "BookRate": float(book['rate']),
                "BookPages": book['pages'],
                "BookDate": book['date'],
                "BookLang": book['lang'],
                "Status": bookstatus,
                "AudioStatus": audiostatus,
                "ScanResult": reason,
                "BookAdded": today(),
            }

            # add source-specific id
            if source == "audible":
                new_value_dict["au_id"] = bookid
            else:
                new_value_dict["gb_id"] = bookid

            if 'nocover' in book['img'] or 'nophoto' in book['img']:
                link, _ = get_book_cover(bookid, ignore='googleapis')
                if link:
                    new_value_dict["BookImg"] = link
                elif book['img'] and book['img'].startswith('http'):
                    link = cache_bookimg(book['img'], bookid, source[:2])
                    new_value_dict["BookImg"] = link

            db.upsert("books", new_value_dict, control_value_dict)
            self.logger.info(f"{book['name']} by {authorname} added to the books database, {bookstatus}/{audiostatus}")

            serieslist = []
            if book.get('series'):
                serieslist = [('', book.get('seriesNum'), clean_name(book['series'], '&/'))]
            if CONFIG.get_bool('ADD_SERIES') and "Ignored:" not in reason:
                newserieslist = get_work_series(bookid, 'LT', reason=reason)
                if newserieslist:
                    serieslist = newserieslist
                    self.logger.debug(f'Updated series: {bookid} [{serieslist}]')
                set_series(serieslist, bookid, reason=reason)

        finally:
            db.close()
