# Calibre-Web Automated – fork of Calibre-Web
# Copyright (C) 2018-2025 Calibre-Web contributors
# Copyright (C) 2024-2025 Calibre-Web Automated contributors
# SPDX-License-Identifier: GPL-3.0-or-later
# See CONTRIBUTORS for full list of authors.

import datetime
import logging
import os
import re
import time
import traceback
from queue import Queue
from urllib.parse import quote

import requests

# Two different iso639 libraries.
# The first library used languages, but does not support python 3.12
# The new library is recently updated and uses Lang
try:
    from iso639 import Lang
except ModuleNotFoundError:
    Lang = None
try:
    from iso639 import languages
except ImportError:
    languages = None
try:
    from lxml import etree
except ImportError:
    etree = None

import contextlib

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
from lazylibrarian.filesystem import DIRS, path_isfile, syspath
from lazylibrarian.formatter import (
    get_list,
    is_valid_isbn,
    md5_utf8,
    plural,
    replace_all,
    strip_quotes,
    thread_name,
    unaccented,
)


class DNB:
    def __init__(self):
        return

    __dnbname__ = "Deutsche Nationalbibliothek"
    __dnbid__ = "dnb"

    logger = logging.getLogger(__name__)
    searchinglogger = logging.getLogger('special.searching')
    active = True

    # Configuration defaults from original plugin
    cfg_guess_series = True
    cfg_append_edition_to_title = False
    cfg_fetch_subjects = 2  # Both GND and non-GND subjects
    cfg_skip_series_starting_with_publishers_name = True
    cfg_unwanted_series_names = [
        r'^Roman$', r'^Science-fiction$', r'^\[Ariadne\]$', r'^Ariadne$', r'^atb$', r'^BvT$',
        r'^Bastei L', r'^bb$', r'^Beck Paperback', r'^Beck\-.*berater', r'^Beck\'sche Reihe',
        r'^Bibliothek Suhrkamp$', r'^BLT$', r'^DLV-Taschenbuch$', r'^Edition Suhrkamp$',
        r'^Edition Lingen Stiftung$', r'^Edition C', r'^Edition Metzgenstein$', r'^ETB$', r'^dtv',
        r'^Ein Goldmann', r'^Oettinger-Taschenbuch$', r'^Haymon-Taschenbuch$', r'^Mira Taschenbuch$',
        r'^Suhrkamp-Taschenbuch$', r'^Bastei-L', r'^Hey$', r'^btb$', r'^bt-Kinder', r'^Ravensburger',
        r'^Sammlung Luchterhand$', r'^blanvalet$', r'^KiWi$', r'^Piper$', r'^C.H. Beck', r'^Rororo',
        r'^Goldmann$', r'^Moewig$', r'^Fischer Klassik$', r'^hey! shorties$', r'^Ullstein',
        r'^Unionsverlag', r'^Ariadne-Krimi', r'^C.-Bertelsmann', r'^Phantastische Bibliothek$',
        r'^Beck Paperback$', r'^Beck\'sche Reihe$', r'^Knaur', r'^Volk-und-Welt', r'^Allgemeine',
        r'^Premium', r'^Horror-Bibliothek$'
    ]

    QUERYURL = ('https://services.dnb.de/sru/dnb?version=1.1&maximumRecords=%s'
                '&operation=searchRetrieve&recordSchema=MARC21-xml&query=%s')
    COVERURL = 'https://portal.dnb.de/opac/mvb/cover?isbn=%s'

    def search(self, query: str, generic_cover: str = "", start=0, limit=10) -> list | None:
        try:
            if not self.active:
                return None

            val = []
            in_cache = 0

            # Parse query for special identifiers
            idn = None
            isbn = ''
            title = ''
            author = ''

            # Check if query contains special identifiers
            if query.startswith('dnb-idn:'):
                idn = query.replace('dnb-idn:', '').strip()
            elif query.startswith('isbn:'):
                isbn = query.replace('isbn:', '').strip()
            elif '<ll>' in query:
                title, author = query.split('<ll>')
            else:
                # Treat as title/author search
                title = query

            # Create query variations
            queries = self._create_query_variations(idn, isbn, author, title)
            for query_str in queries:
                try:
                    results, cached = self._execute_query(query_str, start=start, limit=limit)
                    in_cache += cached
                    if not results:
                        continue

                    for record in results:
                        book_data = self._parse_marc21_record(record)
                        if book_data:
                            meta_record = self._create_meta_record(book_data, generic_cover)
                            if meta_record:
                                val.append(meta_record)

                except Exception as e:
                    self.logger.warning(f"DNB search error: {e}")
                    continue

            return val if val else [], bool(in_cache)

        except Exception as e:
            self.logger.error(f"DNB search failed for query '{query}': {e}")
            return [], False

    def _create_query_variations(self, idn=None, isbn=None, authors=None, title=None):
        """Create SRU query variations with increasing fuzziness"""
        if authors is None:
            authors = []

        queries = []

        if idn:
            queries.append(f'num={idn}')
        elif isbn:
            queries.append(f'num={isbn}')
        else:
            if title and authors:
                query_author = " ".join(authors.split())
                # Basic title search - preserve spaces
                title_tokens = title.split()
                if title_tokens:
                    query_title = " ".join(title_tokens)
                    queries.append(f'per="{query_author}" AND tit="{query_title}"')
                # German joiner removal for fuzzy matching
                german_tokens = self._strip_german_joiners(title.split())
                if german_tokens and german_tokens != title_tokens:
                    query_title = " ".join(german_tokens)
                    queries.append(f'per="{query_author}" AND tit="{query_title}"')
            elif title:
                title_tokens = title.split()
                if title_tokens:
                    query_title = " ".join(title_tokens)
                    queries.append(f'tit="{query_title}"')
                german_tokens = self._strip_german_joiners(title.split())
                if german_tokens and german_tokens != title_tokens:
                    query_title = " ".join(german_tokens)
                    queries.append(f'tit="{query_title}"')

            elif authors:
                author_tokens = authors.split()
                if author_tokens:
                    query_author = " ".join(author_tokens)
                    queries.append(f'per="{query_author}"')

        # Add filters to exclude non-book materials
        filtered_queries = []
        for q in queries:
            filtered_q = f'{q} NOT (mat=film OR mat=music OR mat=microfiches OR cod=tt)'
            filtered_queries.append(filtered_q)

        return filtered_queries

    def _execute_query(self, query, timeout=30, start=0, limit=10):
        """Query DNB SRU API"""
        if not etree:
            self.logger.error('DNB query unavailable: lxml module missing')
            return [], False
        headers = {
            'User-Agent': get_user_agent(),
            'Accept': 'application/xml, text/xml',
            'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive'
        }

        self.logger.debug(f'DNB Query: {query}')

        if start:
            query_url = (self.QUERYURL.replace('&maximumRecords=%s', '&maximumRecords=%s&startRecord=%s') %
                         (limit, start, quote(query)))
        else:
            query_url = self.QUERYURL % (limit, quote(query))
        self.logger.debug(f'DNB Query URL: {query_url}')
        try:
            cache_location = DIRS.get_cachedir('XMLCache')
            filename = query_url
            hashfilename, myhash = self._get_hashed_filename(cache_location, filename)
            # CACHE_AGE is in days, so get it to seconds
            expire_older_than = CONFIG.get_int('CACHE_AGE') * 24 * 60 * 60
            valid_cache = self.is_in_cache(expire_older_than, hashfilename, myhash)
            refresh = False
            if valid_cache and not refresh:
                lazylibrarian.CACHE_HIT += 1
                self.logger.debug(f"CacheHandler: Returning CACHED response {hashfilename}")
                source, ok = self._read_from_cache(hashfilename)
                if ok:
                    xml_data = etree.XML(source)
                else:
                    xml_data = ''
            else:
                lazylibrarian.CACHE_MISS += 1
                if BLOCKHANDLER.is_blocked('DNB'):
                    return [], False

                response = requests.get(query_url, headers=headers, timeout=timeout)
                response.raise_for_status()

                self.logger.debug(f"CacheHandler: Storing xml {myhash}")
                with open(syspath(hashfilename), "wb") as cachefile:
                    cachefile.write(response.content)
                xml_data = etree.XML(response.content)

            num_records = xml_data.xpath("./zs:numberOfRecords",
                                         namespaces={"zs": "http://www.loc.gov/zing/srw/"})[0].text.strip()
            self.logger.info(f'DNB found {num_records} records')

            if int(num_records) == 0:
                return [], False  # Return empty list, not None

            return xml_data.xpath("./zs:records/zs:record/zs:recordData/marc21:record",
                                  namespaces={'marc21': 'http://www.loc.gov/MARC21/slim',
                                              "zs": "http://www.loc.gov/zing/srw/"}), valid_cache
        except Exception as e:
            self.logger.error(f'DNB query error: {e}')
            return [], False  # Return empty list, not None

    def is_in_cache(self, expiry: int, hashfilename: str, myhash: str) -> bool:
        """Check if a cache file is valid."""
        if path_isfile(hashfilename):
            cache_modified_time = os.stat(hashfilename).st_mtime
            time_now = time.time()
            if expiry and cache_modified_time < time_now - expiry:
                # Cache entry is too old, delete it
                self.logger.debug(f"Expiring {myhash}")
                os.remove(syspath(hashfilename))
                return False
            return True
        return False

    @staticmethod
    def _read_from_cache(hashfilename: str) -> (str, bool):
        """Read a cached API response from disk."""
        source = ''
        with open(syspath(hashfilename), "rb") as cachefile:
            source = cachefile.read()
        return source, True

    @staticmethod
    def _get_hashed_filename(cache_location: str, url: str) -> (str, str):
        """Generate a hashed filename for caching."""
        myhash = md5_utf8(url)
        hashfilename = os.path.join(cache_location, myhash[0], myhash[1], f"{myhash}.xml")
        return hashfilename, myhash

    def _parse_marc21_record(self, record):
        """Parse MARC21 XML record into book data"""
        ns = {'marc21': 'http://www.loc.gov/MARC21/slim'}

        book = {
            'series': None,
            'series_index': None,
            'pubdate': None,
            'languages': [],
            'title': None,
            'authors': [],
            'comments': None,
            'idn': None,
            'urn': None,
            'isbn': None,
            'ddc': [],
            'subjects_gnd': [],
            'subjects_non_gnd': [],
            'publisher_name': None,
            'publisher_location': None,
        }

        # Skip audio/video content
        try:
            mediatype = record.xpath("./marc21:datafield[@tag='336']/marc21:subfield[@code='a']",
                                     namespaces=ns)[0].text.strip().lower()
            if mediatype in 'gesprochenes wort':
                return None
        except (IndexError, AttributeError):
            pass

        try:
            mediatype = record.xpath("./marc21:datafield[@tag='337']/marc21:subfield[@code='a']",
                                     namespaces=ns)[0].text.strip().lower()
            if mediatype in ('audio', 'video'):
                return None
        except (IndexError, AttributeError):
            pass

        # Extract IDN
        with contextlib.suppress(IndexError, AttributeError):
            book['idn'] = record.xpath("./marc21:datafield[@tag='016']/marc21:subfield[@code='a']",
                                       namespaces=ns)[0].text.strip()

        # Extract title from field 245
        self._extract_title_and_series(record, book, ns)

        # Extract authors from fields 100/700
        self._extract_authors(record, book, ns)

        # Extract publisher info from field 264
        self._extract_publisher_info(record, book, ns)

        # Extract ISBN from field 020
        self._extract_isbn(record, book, ns)

        # Extract subjects
        self._extract_subjects(record, book, ns)

        # Extract languages from field 041
        self._extract_languages(record, book, ns)

        # Extract comments from field 856
        self._extract_comments(record, book, ns)

        # Apply series guessing if enabled
        if self.cfg_guess_series and (not book['series'] or not book['series_index']):
            self._guess_series_from_title(book)

        return book

    def _extract_title_and_series(self, record, book, ns):
        """Extract title and series from MARC21 field 245"""
        for field in record.xpath("./marc21:datafield[@tag='245']", namespaces=ns):

            # Get main title (subfield a)
            code_a = []
            for i in field.xpath("./marc21:subfield[@code='a']", namespaces=ns):
                code_a.append(i.text.strip())

            # Get part numbers (subfield n)
            code_n = []
            for i in field.xpath("./marc21:subfield[@code='n']", namespaces=ns):
                match = re.search(r"(\d+([,\.]\d+)?)", i.text.strip())
                if match:
                    code_n.append(match.group(1))

            # Get part names (subfield p)
            code_p = []
            for i in field.xpath("./marc21:subfield[@code='p']", namespaces=ns):
                code_p.append(i.text.strip())

            title_parts = code_a

            # Handle series extraction
            if code_a and code_n:
                if code_p:
                    title_parts = [code_p[-1]]

                # Build series name
                series_parts = [code_a[0]]
                for i in range(min(len(code_p), len(code_n)) - 1):
                    series_parts.append(code_p[i])

                for i in range(min(len(series_parts), len(code_n) - 1)):
                    series_parts[i] += ' ' + code_n[i]

                book['series'] = ' - '.join(series_parts)
                book['series'] = self._clean_series(book['series'], book['publisher_name'])

                if code_n:
                    book['series_index'] = code_n[-1]

            # Add subtitle (subfield b)
            try:
                subtitle = field.xpath("./marc21:subfield[@code='b']", namespaces=ns)[0].text.strip()
                title_parts.append(subtitle)
            except (IndexError, AttributeError):
                pass

            book['title'] = " : ".join(title_parts)
            book['title'] = self._clean_title(book['title'])

    @staticmethod
    def _extract_authors(record, book, ns):
        """Extract authors from MARC21 fields 100/700"""
        # Primary authors (field 100)
        for i in record.xpath("./marc21:datafield[@tag='100']/marc21:subfield[@code='4' and "
                              "text()='aut']/../marc21:subfield[@code='a']", namespaces=ns):
            name = re.sub(r" \[.*\]$", "", i.text.strip())
            book['authors'].append(name)

        # Secondary authors (field 700)
        for i in record.xpath("./marc21:datafield[@tag='700']/marc21:subfield[@code='4' and "
                              "text()='aut']/../marc21:subfield[@code='a']", namespaces=ns):
            name = re.sub(r" \[.*\]$", "", i.text.strip())
            book['authors'].append(name)

        # If no authors found, use all involved persons
        if not book['authors']:
            for i in record.xpath("./marc21:datafield[@tag='700']/marc21:subfield[@code='a']", namespaces=ns):
                name = re.sub(r" \[.*\]$", "", i.text.strip())
                book['authors'].append(name)

    @staticmethod
    def _extract_publisher_info(record, book, ns):
        """Extract publisher information from MARC21 field 264"""
        for field in record.xpath("./marc21:datafield[@tag='264']", namespaces=ns):
            # Publisher location (subfield a)
            if not book['publisher_location']:
                location_parts = []
                for i in field.xpath("./marc21:subfield[@code='a']", namespaces=ns):
                    location_parts.append(i.text.strip())
                if location_parts:
                    book['publisher_location'] = ' '.join(location_parts).strip('[]')

            # Publisher name (subfield b)
            if not book['publisher_name']:
                with contextlib.suppress(IndexError, AttributeError):
                    book['publisher_name'] = field.xpath("./marc21:subfield[@code='b']", namespaces=ns)[0].text.strip()

            # Publication date (subfield c)
            if not book['pubdate']:
                try:
                    pubdate = field.xpath("./marc21:subfield[@code='c']", namespaces=ns)[0].text.strip()
                    match = re.search(r"(\d{4})", pubdate)
                    if match:
                        year = match.group(1)
                        book['pubdate'] = datetime.datetime(int(year), 1, 1, 12, 30, 0)
                except (IndexError, AttributeError):
                    pass

    @staticmethod
    def _extract_isbn(record, book, ns):
        """Extract ISBN from MARC21 field 020"""
        for i in record.xpath("./marc21:datafield[@tag='020']/marc21:subfield[@code='a']", namespaces=ns):
            try:
                isbn_regex = (r"(?:ISBN(?:-1[03])?:? )?(?=[-0-9 ]{17}|[-0-9X ]{13}|"
                              r"[0-9X]{10})(?:97[89][- ]?)?[0-9]{1,5}[- ]?(?:[0-9]+[- ]?){2}[0-9X]")
                match = re.search(isbn_regex, i.text.strip())
                if match:
                    isbn = match.group()
                    book['isbn'] = isbn.replace('-', '')
                    break
            except AttributeError:
                pass

    def _extract_subjects(self, record, book, ns):
        """Extract subjects from MARC21 fields"""
        # GND subjects from field 689
        for i in record.xpath("./marc21:datafield[@tag='689']/marc21:subfield[@code='a']", namespaces=ns):
            book['subjects_gnd'].append(i.text.strip())

        # GND subjects from fields 600-655
        for f in range(600, 656):
            for i in record.xpath(f"./marc21:datafield[@tag='{f}']/marc21:subfield[@code='2' and "
                                  f"text()='gnd']/../marc21:subfield[@code='a']", namespaces=ns):
                if not i.text.startswith("("):
                    book['subjects_gnd'].append(i.text.strip())

        # Non-GND subjects from fields 600-655
        for f in range(600, 656):
            for i in record.xpath(f"./marc21:datafield[@tag='{f}']/marc21:subfield[@code='a']", namespaces=ns):
                if not i.text.startswith("(") and len(i.text) >= 2:
                    book['subjects_non_gnd'].extend(re.split(',|;', self._remove_sorting_characters(i.text)))

    def _extract_languages(self, record, book, ns):
        """Extract languages from MARC21 field 041"""
        raw_languages = []
        for i in record.xpath("./marc21:datafield[@tag='041']/marc21:subfield[@code='a']", namespaces=ns):
            lang_code = i.text.strip()
            # Convert 'ger' to 'deu' for consistency
            if lang_code == 'ger':
                lang_code = 'deu'

            # Convert ISO code to English language name
            # language_name = isoLanguages.get_language_name(get_locale(), lang_code)
            isodata = None
            if Lang:
                isodata = Lang(lang_code)
            elif languages:
                isodata = languages.get(alpha2=lang_code[:2])

            if isodata and isodata.name:
                language_name = isodata.name
            else:
                language_name = lang_code if lang_code else "Unknown"

            if language_name and language_name != "Unknown":
                raw_languages.append(language_name)
                # self.logger.info(f"Converted {lang_code} to {language_name}")
            else:
                self.logger.warning(f"Unknown language code from DNB: {lang_code}")

        book['languages'] = raw_languages

    @staticmethod
    def _extract_comments(record, book, ns):
        """Extract comments from MARC21 field 856"""
        for url_elem in record.xpath("./marc21:datafield[@tag='856']/marc21:subfield[@code='u']", namespaces=ns):
            url = url_elem.text.strip()
            if url.startswith(("http://deposit.dnb.de/", "https://deposit.dnb.de/")):
                try:
                    response = requests.get(url, timeout=30)
                    response.raise_for_status()

                    comments_text = response.text
                    if 'Zugriff derzeit nicht möglich' in comments_text:
                        continue

                    # Clean up comments
                    comments_text = re.sub(
                        r'(\s|<br>|<p>|\n)*Angaben aus der Verlagsmeldung(\s|<br>|<p>|\n)*'
                        r'(<h3>.*?</h3>)*(\s|<br>|<p>|\n)*',
                        '', comments_text, flags=re.IGNORECASE)
                    book['comments'] = comments_text
                    break
                except Exception:
                    continue

    @staticmethod
    def _extract_edition(record, book, ns):
        """Extract edition from MARC21 field 250"""
        with contextlib.suppress(IndexError, AttributeError):
            book['edition'] = record.xpath("./marc21:datafield[@tag='250']/marc21:subfield[@code='a']",
                                           namespaces=ns)[0].text.strip()

    def _get_cover_url(self, book_data, generic_cover):
        if not book_data.get('isbn'):
            return generic_cover

        cover_url = self.COVERURL % book_data['isbn']

        try:
            # Test the actual response from DNB
            response = requests.head(cover_url, timeout=10)
            # self.logger.info(f"DNB cover response status: {response.status_code}")
            # self.logger.info(f"DNB cover content-type: {response.headers.get('content-type')}")

            if response.status_code == 200:
                return cover_url
        except Exception as e:
            self.logger.error(f"DNB cover test failed: {e}")

        return generic_cover

    def _extract_image_url_from_html(self, html_content, original_url):
        """Extract actual image URL from DNB's HTML wrapper"""
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_content, 'html.parser')
            img_tag = soup.find('img')
            if img_tag and img_tag.get('src'):
                img_src = img_tag['src']
                # If it's a relative URL, make it absolute
                if img_src.startswith('/'):
                    from urllib.parse import urljoin
                    return urljoin(original_url, img_src)
                if img_src.startswith('http'):
                    return img_src
                # If it's the same URL, we have a problem
                if img_src == original_url:
                    return None
                return img_src
        except Exception as e:
            self.logger.error(f"Failed to extract image URL from HTML: {e}")
        return None

    def _get_validated_cover_url(self, book_data, generic_cover):
        """Get and validate DNB cover URL, handling HTML responses"""
        if not book_data.get('isbn'):
            return generic_cover

        cover_url = self.COVERURL % book_data['isbn']

        try:
            response = requests.get(cover_url, timeout=10)
            response.raise_for_status()

            content_type = response.headers.get('content-type').lower()
            # content_type = content_type.split(';')[0].strip() # Test remove charset=utf-8

            # self.logger.info(f"DNB cover response content-type: {content_type}")

            # Clean content-type by removing charset and other parameters
            main_content_type = content_type.split(';')[0].strip()

            # Check if it's a valid image type
            if main_content_type in ('image/jpeg', 'image/jpg', 'image/png', 'image/webp', 'image/bmp'):
                # Modify the response headers to remove charset
                response.headers['content-type'] = main_content_type
                # self.logger.info("Test: _get_validated_cover_url: if main_content_type")
                # self.logger.info(cover_url)
                # self.logger.info(response.headers['content-type'])
                return cover_url
            if 'text/html' in content_type:
                # Handle HTML wrapper case as before
                self.logger.debug("main_content_type: text/html")
                # Verify the response actually contains image data
                if len(response.content) > 0 and response.content[:4] in [b'\xff\xd8\xff', b'\x89PNG']:
                    self.logger.debug("response.content>0 and ..etc")
                    actual_image_url = self._extract_image_url_from_html(response.text, cover_url)
                    if actual_image_url and actual_image_url != cover_url:
                        self.logger.debug(actual_image_url)
                        return actual_image_url

        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                self.logger.warning(f"DNB cover not found for ISBN: {book_data.get('isbn')}")
            else:
                self.logger.error(f"DNB cover validation failed: {e}")
        except Exception as e:
            self.logger.error(f"DNB cover validation failed: {e}")

        return generic_cover

    def _create_meta_record(self, book_data, generic_cover):
        """Create MetaRecord from parsed book data"""
        if not book_data.get('title') or not book_data.get('authors'):
            return None

        # Apply edition to title if configured
        title = book_data['title']
        if self.cfg_append_edition_to_title and book_data.get('edition'):
            title = f"{title} : {book_data['edition']}"

        # Clean author names
        authors = [self._remove_sorting_characters(author) for author in book_data['authors']]
        authors = [re.sub(r"^(.+), (.+)$", r"\2 \1", author) for author in authors]

        # Get validated cover URL
        cover_url = self._get_validated_cover_url(book_data, generic_cover)
        # self.logger.info(cover_url)

        # Build publisher string
        publisher_parts = []
        if book_data.get('publisher_location'):
            publisher_parts.append(book_data['publisher_location'])
        if book_data.get('publisher_name'):
            publisher_parts.append(self._remove_sorting_characters(book_data['publisher_name']))
        publisher = " ; ".join(publisher_parts) if publisher_parts else ""

        # Select subjects based on configuration
        tags = []
        if self.cfg_fetch_subjects == 0:  # Only GND
            tags = self._uniq(book_data['subjects_gnd'])
        elif self.cfg_fetch_subjects == 1:  # GND if available, else non-GND
            tags = self._uniq(book_data['subjects_gnd']) if book_data['subjects_gnd'] else (
                self._uniq(book_data['subjects_non_gnd']))
        elif self.cfg_fetch_subjects == 2:  # Both GND and non-GND
            tags = self._uniq(book_data['subjects_gnd'] + book_data['subjects_non_gnd'])
        elif self.cfg_fetch_subjects == 3:  # Non-GND if available, else GND
            tags = self._uniq(book_data['subjects_non_gnd']) if book_data['subjects_non_gnd'] else (
                self._uniq(book_data['subjects_gnd']))
        elif self.cfg_fetch_subjects == 4:  # Only non-GND
            tags = self._uniq(book_data['subjects_non_gnd'])
        # cfg_fetch_subjects == 5: No subjects

        # Build identifiers
        identifiers = {}
        if book_data.get('idn'):
            identifiers['dnb-idn'] = book_data['idn']
        if book_data.get('isbn'):
            identifiers['isbn'] = book_data['isbn']
        if book_data.get('urn'):
            identifiers['urn'] = book_data['urn']
        if book_data.get('ddc'):
            identifiers['ddc'] = ",".join(book_data['ddc'])

        # Get cover URL
        # cover_url = generic_cover
        # if book_data.get('isbn'):
        #    cover_url = self.COVERURL % book_data['isbn']
        try:
            seriesindex = float(book_data.get('series_index', 0)) if book_data.get('series_index') else 0
        except Exception:
            seriesindex = 0

        return {
            "id": book_data.get('idn', ''),
            "title": self._remove_sorting_characters(title),
            "authors": authors,
            "url": f"https://portal.dnb.de/opac.htm?method=simpleSearch&query={book_data.get('idn', '')}",
            "source": {
                "id": self.__dnbid__,
                "description": self.__dnbname__,
                "link": "https://portal.dnb.de/",
            },
            "cover": cover_url,
            "description": book_data.get('comments', ''),
            "series": self._remove_sorting_characters(book_data.get('series', '')) if book_data.get('series') else '',
            "series_index": seriesindex,
            "identifiers": identifiers,
            "publisher": publisher,
            "publishedDate": book_data['pubdate'].strftime('%Y-%m-%d') if book_data.get('pubdate') else '',
            "languages": book_data.get('languages', []),
            "tags": tags,
        }

    # Helper functions adapted from original plugin
    @staticmethod
    def _remove_sorting_characters(text):
        """Remove sorting word markers"""
        if text:
            return ''.join([c for c in text if ord(c) != 152 and ord(c) != 156])
        return None

    def _clean_title(self, title):
        """Clean up title"""
        if title:
            # Remove name of translator from title
            match = re.search(r'^(.+) [/:] [Aa]us dem .+? von(\s\w+)+$', self._remove_sorting_characters(title))
            if match:
                title = match.group(1)
        return title

    def _clean_series(self, series, publisher_name):
        """Clean up series"""
        if not series:
            return None

        # Series must contain at least one character
        if not re.search(r'\S', series):
            return None

        # Remove sorting word markers
        series = self._remove_sorting_characters(series)

        # Skip series starting with publisher name if configured
        if self.cfg_skip_series_starting_with_publishers_name and publisher_name:
            if publisher_name.lower() == series.lower():
                return None

            match = re.search(r'^(\w\w\w\w+)', self._remove_sorting_characters(publisher_name))
            if match:
                pubcompany = match.group(1)
                if re.search(r'^\W*' + pubcompany, series, flags=re.IGNORECASE):
                    return None

        # Check against unwanted series patterns
        for pattern in self.cfg_unwanted_series_names:
            try:
                if re.search(pattern, series, flags=re.IGNORECASE):
                    return None
            except Exception:
                pass

        return series

    @staticmethod
    def _strip_german_joiners(wordlist):
        """Remove German joiners from list of words"""
        tokens = []
        for word in wordlist:
            if word.lower() not in ('ein', 'eine', 'einer', 'der', 'die', 'das', 'und', 'oder'):
                tokens.append(word)
        return tokens

    def _guess_series_from_title(self, book):
        """Try to extract Series and Series Index from a book's title"""
        if not book.get('title'):
            return

        title = book['title']
        parts = self._remove_sorting_characters(title).split(' (')
        if len(parts) == 2:
            indexpart = parts[1]
            textpart = parts[0]

            # Clean textpart
            match = re.match(r"^[\s\-–—:]*(.+?)[\s\-–—:]*$", textpart)
            if match:
                textpart = match.group(1)

            # Extract series and index
            try:
                series, series_index = indexpart.split(')', 1)[0].rsplit(' ', 1)
                series = self._clean_series(series, book.get('publisher_name'))
                if series and series_index and series_index[-1].isdigit():
                    book['series'] = series
                    book['series_index'] = series_index
                    book['title'] = textpart
            except IndexError:
                pass

    @staticmethod
    def _uniq(list_with_duplicates):
        """Remove duplicates from a list while preserving order"""
        unique_list = []
        for item in list_with_duplicates:
            if item not in unique_list:
                unique_list.append(item)
        return unique_list

    def get_author_books(self, authorid=None, authorname=None, bookstatus="Skipped",
                         audiostatus="Skipped", entrystatus='Active', refresh=False, reason='dnb.get_author_books'):
        if not CONFIG['DNB_API']:
            self.logger.warning('DNB API not enabled, check config')
            return
        if not etree or (not Lang and not languages):
            self.logger.warning('Required modules missing, lxml and/or iso639')
            return

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

            self.logger.debug(f'[{auth_id}:{auth_name}] Now processing books with DNB API')
            # Artist is loading
            db.action("UPDATE authors SET Status='Loading' WHERE AuthorID=?", (auth_id,))

            resultqueue = Queue()
            if not self.find_results(f"<ll>{auth_name}", resultqueue):
                self.logger.warning(f"No results from DNB for {auth_name}")
                return

            _ = add_author_books_to_db(resultqueue, bookstatus, audiostatus, entrystatus, entryreason,
                                       auth_id, None, self.get_bookdict_for_bookid, cache_hits=0)

        except Exception:
            self.logger.error(f'Unhandled exception in dnb_get_author_books: {traceback.format_exc()}')
        finally:
            db.action("UPDATE authors SET Status=? WHERE AuthorID=?", (entrystatus, auth_id,))
            db.close()

    def get_bookdict_for_bookid(self, bookid=None):
        results, in_cache = self.search(f"dnb-idn:{bookid}", start=0, limit=5)
        # shouldn't need many results as based on bookid
        bookdict = None
        for bk in results:
            bookdict = self.dnb_book_dict(bk)
            if bookdict and bookdict['bookid'] == bookid:
                break
        return bookdict, in_cache

    def add_bookid_to_db(self, bookid=None, bookstatus=None, audiostatus=None, reason='dnb.add_bookid_to_db'):
        # Find details on a book from provider using bookid
        # Ensure author in database, add book to database using provided or default statuses and reason
        if not CONFIG['DNB_API']:
            self.logger.warning('DNB API not enabled, check config')
            return False
        if not etree or (not Lang and not languages):
            self.logger.warning('Required modules missing, lxml and/or iso639')
            return False
        if not bookstatus:
            bookstatus = CONFIG['NEWBOOK_STATUS']
        if not audiostatus:
            audiostatus = CONFIG['NEWAUDIO_STATUS']

        bookdict, _ = self.get_bookdict_for_bookid(bookid)
        if not bookdict or bookdict['bookid'] != bookid:
            self.logger.debug(f'BookID {bookid} not found at dnb')
            return False

        authorname = bookdict.get('authorname')
        if authorname:
            auth_name, auth_id = lazylibrarian.importer.get_preferred_author(authorname)
            db = database.DBConnection()
            res = db.match('SELECT authorid from authors WHERE authorname=?', (auth_name,))
            if res:
                bookdict['authorid'] = res['authorid']
            else:
                authorname, authorid, _ = lazylibrarian.importer.add_author_name_to_db(author=auth_name,
                                                                                       refresh=False,
                                                                                       addbooks=False,
                                                                                       reason=f"DNB add {bookid}:{bookdict['bookname']}",
                                                                                       title=bookdict['bookname'])
                if authorname:
                    bookdict['authorname'] = authorname
                    bookdict['authorid'] = authorid
            db.close()
        # validate bookdict, reject if unwanted or incomplete
        bookdict, rejected = validate_bookdict(bookdict)
        if rejected:
            if reason.startswith("Series:") or 'bookname' not in bookdict or 'authorname' not in bookdict:
                self.logger.error(f"Rejected {bookid}:{rejected}")
                return False
            for reject in rejected:
                if reject[0] == 'name':
                    self.logger.error(f"Rejected {bookid}:{rejected}")
                    return False
        # show any non-fatal warnings
        warn_about_bookdict(bookdict)
        # Use the author ID we already have from the book data
        # This avoids an author search that can return the wrong author
        authorid = bookdict.get('authorid')

        if authorid:
            # Add book to database using bookdict
            bookdict['status'] = bookstatus
            bookdict['audiostatus'] = audiostatus
            bookdict['reason'] = f"[{thread_name()}] {reason}"
            res = add_bookdict_to_db(bookdict)
            lazylibrarian.importer.update_totals(authorid)
            return res
        return False

    def find_results(self, searchterm=None, queue=None):
        if not CONFIG['DNB_API']:
            self.logger.warning('DNB API not enabled, check config')
            return False
        if not etree or (not Lang and not languages):
            self.logger.warning('Required modules missing, lxml and/or iso639')
            return False
        try:
            resultlist = []
            resultcount = 0
            ignored = 0
            total_count = 0
            no_author_count = 0
            title = ''
            authorname = ''

            if is_valid_isbn(searchterm):
                searchterm = f'isbn: {searchterm}'

            if '<ll>' in searchterm:  # special token separates title from author
                title, authorname = searchterm.split('<ll>')

            # strip all ascii and non-ascii quotes/apostrophes
            searchterm = strip_quotes(searchterm)

            fullterm = searchterm.replace('<ll>', ' ')
            self.logger.debug(f'Now searching DNB with searchterm: {fullterm}')
            # max limit = 100 per search
            start = 0
            limit = 100
            next_page = True
            while next_page:
                results, in_cache = self.search(searchterm, start=start, limit=limit)
                self.logger.debug(f"Search {start}:{limit} returned {len(results)}")
                for item in results:
                    book = self.dnb_book_dict(item)
                    if not book['authorname']:
                        self.logger.debug('Skipped a result without authorfield.')
                        no_author_count += 1
                        continue
                    if not book['bookname']:
                        self.logger.debug('Skipped a result without title.')
                        continue

                    valid_langs = get_list(CONFIG['IMP_PREFLANG'])
                    if "All" not in valid_langs:  # don't care about languages, accept all
                        try:
                            # skip if no language in valid list -
                            booklangs = get_list(book['booklang'])
                            if not booklangs:
                                booklangs = ['Unknown']
                            valid = False
                            for lang in booklangs:
                                if lang in valid_langs:
                                    valid = True
                                    break
                            if not valid:
                                self.logger.debug(f"Skipped {book['bookname']} with language {booklangs}")
                                ignored += 1
                                continue
                        except KeyError:
                            ignored += 1
                            self.logger.debug(f"Skipped {book['bookname']} where no language is found")
                            continue

                    if authorname:
                        author_fuzz = fuzz.token_sort_ratio(book['authorname'], authorname)
                    else:
                        author_fuzz = fuzz.token_sort_ratio(book['authorname'], fullterm)

                    if title:
                        if title.endswith(')'):
                            title = title.rsplit('(', 1)[0]
                        book_fuzz = fuzz.token_set_ratio(book['bookname'].lower(), title.lower())
                        # lose a point for each extra word in the fuzzy matches so we get the closest match
                        words = len(get_list(book['bookname']))
                        words -= len(get_list(title))
                        book_fuzz -= abs(words)
                    else:
                        book_fuzz = fuzz.token_set_ratio(book['bookname'].lower(), fullterm.lower())
                    isbn_fuzz = 0
                    if is_valid_isbn(fullterm):
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
                        'bookid': book['bookid'],
                        'bookname': bookname,
                        'booksub': book['booksub'],
                        'bookisbn': book['bookisbn'],
                        'bookpub': book['bookpub'],
                        'bookdate': book['bookdate'],
                        'booklang': ', '.join(set(book['booklang'])),
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
                        'source': 'DNB'
                    })

                    resultcount += 1

                if len(results) < limit:
                    next_page = False
                else:
                    start += limit

            self.logger.debug(
                f"Returning {resultcount} {plural(resultcount, 'result')} for {searchterm}")

            self.logger.debug(f"Found {total_count} {plural(total_count, 'result')}")
            self.logger.debug(f"Removed {ignored} unwanted language {plural(ignored, 'result')}")
            self.logger.debug(f"Removed {no_author_count} {plural(no_author_count, 'book')} with no author")
            queue.put(resultlist)

        except Exception:
            self.logger.error(f'Unhandled exception in DNB.find_results: {traceback.format_exc()}')

    @staticmethod
    def dnb_book_dict(item):
        """ Return all the book info we need as a dictionary or default value if no key """
        mydict = {}
        for val, idx, default in [
            ('bookid', 'id', ''),
            ('authorname', 'authors', ''),
            ('bookname', 'title', ''),
            ('identifiers', 'identifiers', ''),
            ('booklang', 'languages', ''),
            ('bookpub', 'publisher', ''),
            ('booksub', 'subtitle', ''),
            ('bookdate', 'publishedDate', '0000'),
            ('bookrate', 'averageRating', 0),
            ('bookrate_count', 'ratingsCount', 0),
            ('bookpages', 'pageCount', 0),
            ('series_name', 'series', ''),
            ('series_index', 'series_index', ''),
            ('bookdesc', 'description', 'Not available'),
            ('booklink', 'url', ''),
            ('bookimg', 'cover', 'images/nocover.png'),
            ('bookgenre', 'tags', '')
        ]:
            try:
                mydict[val] = item[idx]
            except KeyError:
                mydict[val] = default

        # massage into a standard layout across all providers
        if mydict['bookname'] and ':' in mydict['bookname']:
            title, subtitle = mydict['bookname'].split(':', 1)
            mydict['bookname'] = title.strip()
            mydict['booksub'] = subtitle.strip()

        if isinstance(mydict['booklang'], list):
            mydict['booklang'] = ','.join(mydict['booklang'])
        if isinstance(mydict['bookgenre'], list):
            if lazylibrarian.GRGENRES:
                genre_limit = lazylibrarian.GRGENRES.get('genreLimit', 3)
            else:
                genre_limit = 3
            mydict['bookgenre'] = ','.join(mydict['bookgenre'][:genre_limit])

        mydict['contributors'] = []
        authornames = mydict['authorname']
        if len(authornames) > 1:
            for authorname in authornames[1:]:
                authorname, _ = lazylibrarian.importer.get_preferred_author(authorname)
                mydict['contributors'].append(["", " ".join(authorname.split())])
        mydict['authorname'], _ = lazylibrarian.importer.get_preferred_author(authornames[0])

        mydict['bookisbn'] = ''
        if mydict['identifiers'] and 'isbn' in mydict['identifiers']:
            mydict['bookisbn'] = mydict['identifiers']['isbn']

        mydict['series'] = []
        if mydict['series_name'] and mydict['series_index']:
            # no series_id from dnb, so use bookid. DNB only gives one series per book.
            # we can merge the series together in the database by matching series_name where series_id starts with 'DN'
            # so the series id becomes the bookid of the first book in the series added to the database
            mydict['series'] = [(mydict['series_name'], f"DN{mydict['bookid']}", mydict['series_index'])]
        mydict['source'] = 'DNB'
        return mydict
