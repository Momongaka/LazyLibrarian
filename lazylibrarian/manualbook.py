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
from urllib.parse import quote, quote_plus

from rapidfuzz import fuzz

from lazylibrarian import database
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import date_format, get_list, plural, unaccented
from lazylibrarian.providers import (
    iterate_over_direct_sites,
    iterate_over_irc_sites,
    iterate_over_rss_sites,
    iterate_over_torrent_sites,
    iterate_over_znab_sites,
)


def search_item(item=None, bookid=None, cat=None):
    """
    Call all active search providers to search for item
    return a list of results, each entry in list containing percentage_match, title, provider, size, url
    item = searchterm to use for general search
    bookid = link to data for book/audio searches
    cat = category to search [general, book, audio]
    """
    results = []

    if not item:
        return results

    logger = logging.getLogger(__name__)
    book = {}
    searchterm = unaccented(item, only_ascii=False)

    book['searchterm'] = searchterm
    if bookid:
        book['bookid'] = bookid
    else:
        book['bookid'] = searchterm

    if cat in ['book', 'audio']:
        db = database.DBConnection()
        try:
            cmd = ("SELECT authorName,bookName,bookSub from books,authors WHERE books.AuthorID=authors.AuthorID "
                   "and bookID=?")
            match = db.match(cmd, (bookid,))
        finally:
            db.close()
        if match:
            book['authorName'] = match['authorName']
            book['bookName'] = match['bookName']
            book['bookSub'] = match['bookSub']
        else:
            logger.debug('Forcing general search')
            cat = 'general'

    nprov = CONFIG.total_active_providers()
    logger.debug(f"Searching {nprov} {plural(nprov, 'provider')} ({cat}) for {searchterm}")

    if CONFIG.use_nzb():
        resultlist, nprov = iterate_over_znab_sites(book, cat)
        if nprov:
            results += resultlist
    if CONFIG.use_tor():
        resultlist, nprov = iterate_over_torrent_sites(book, cat)
        if nprov:
            results += resultlist
    if CONFIG.use_direct():
        resultlist, nprov = iterate_over_direct_sites(book, cat)
        if nprov:
            results += resultlist
    if CONFIG.use_irc():
        resultlist, nprov = iterate_over_irc_sites(book, cat)
        if nprov:
            results += resultlist
    if CONFIG.use_rss():
        resultlist, nprov, dltypes = iterate_over_rss_sites()
        if nprov and dltypes != 'M':
            results += resultlist

    # reprocess to get consistent results
    searchresults = []
    for item in results:
        provider = ''
        title = ''
        url = ''
        size = ''
        date = ''
        mode = ''
        prov_page = ''
        if 'dispname' in item:
            provider = item['dispname']
        elif 'nzbprov' in item:
            provider = item['nzbprov']
        elif 'tor_prov' in item:
            provider = item['tor_prov']
        elif 'rss_prov' in item:
            provider = item['rss_prov']
        if 'nzbtitle' in item:
            title = item['nzbtitle']
        if 'nzburl' in item:
            url = item['nzburl']
        if 'nzbsize' in item:
            size = item['nzbsize']
        if 'nzbdate' in item:
            date = item['nzbdate']
        if 'nzbmode' in item:
            mode = item['nzbmode']
        if 'tor_title' in item:
            title = item['tor_title']
        if 'tor_url' in item:
            url = item['tor_url']
        if 'tor_size' in item:
            size = item['tor_size']
        if 'tor_date' in item:
            date = item['tor_date']
        if 'tor_type' in item:
            mode = item['tor_type']
        if 'prov_page' in item:
            prov_page = item['prov_page']

        if title and provider and mode and url:
            # Not all results have a date or a size
            if not size:
                size = '1000'
            if date:
                date = date_format(date, context=title, datelang=CONFIG['DATE_LANG'])
            url = url.encode('utf-8')
            if mode == 'torznab' and url.startswith(b'magnet'):
                mode = 'magnet'

            # calculate match percentage - torrents might have words_with_underscore_separator
            score = fuzz.token_set_ratio(searchterm, title.replace('_', ' '))
            # lose a point for each extra word in the title to get the closest match
            words = len(get_list(searchterm))
            words -= len(get_list(title))
            score -= abs(words)
            if score >= 40:  # ignore wildly wrong results?
                result = {'score': round(score, 2), 'title': title, 'provider': provider, 'size': size, 'date': date,
                          'url': quote_plus(url), 'mode': mode, 'url_title': quote(title), 'prov_page': prov_page}

                searchresults.append(result)

    logger.debug(f'Found {len(searchresults)} {cat} results for {searchterm}')
    return searchresults
