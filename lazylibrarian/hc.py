import contextlib
import enum
import http.client
import json
import logging
import os
import platform
import threading
import time
import traceback
from queue import Queue

import cherrypy
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
from lazylibrarian.common import get_readinglist, set_readinglist
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import DIRS, path_isfile, syspath
from lazylibrarian.formatter import (
    check_int,
    date_format,
    format_author_name,
    get_list,
    is_valid_isbn,
    md5_utf8,
    now,
    plural,
    thread_name,
)


class ReadStatus(enum.Enum):
    unknown = 0
    wanttoread = 1
    reading = 2
    read = 3
    paused = 4
    dnf = 5
    ignored = 6


def test_auth(userid=None, token=None):
    """Test HardCover authentication for a user."""
    logger = logging.getLogger(__name__)
    msg = ''
    if not userid:
        userid, msg = get_current_userid()

    # Still no userid, return error. Fail early.
    if not userid:
        logger.error(f"No userid found for test_auth: {msg}")
        return msg

    logger.info(f"Testing auth for userid: {userid}")
    if userid:
        h_c = HardCover()
        if BLOCKHANDLER.is_blocked('HardCover'):
            BLOCKHANDLER.remove_provider_entry('HardCover')
        msg = h_c.hc_whoami(userid=userid, token=token)
    return msg


def hc_api_sleep(limit=1.1):  # official limit is 60 requests per minute. limit=1.1 gives us about 55
    """Sleep to respect HardCover API rate limits."""
    time_now = time.time()
    delay = time_now - lazylibrarian.TIMERS['LAST_HC']
    if delay < limit:
        sleep_time = limit - delay
        lazylibrarian.TIMERS['SLEEP_HC'] += sleep_time
        cachelogger = logging.getLogger('special.cache')
        cachelogger.debug(f"HardCover sleep {sleep_time:.3f}, total {lazylibrarian.TIMERS['SLEEP_HC']:.3f}")
        time.sleep(sleep_time)
    lazylibrarian.TIMERS['LAST_HC'] = time_now


def get_current_userid():
    """Get the current user's ID from cookies."""
    userid = ''
    msg = ''
    cookie = cherrypy.request.cookie
    if 'll_uid' in list(cookie.keys()):
        userid = cookie['ll_uid'].value
    else:
        msg = 'No current lazylibrarian userid'
        if not CONFIG.get_bool('USER_ACCOUNTS'):
            msg += ', you need to enable user accounts in LazyLibrarian for this feature'
    return userid, msg


def hc_sync(library='', userid=None, confirmed=False, readonly=False):
    """Sync reading lists between LazyLibrarian and HardCover.
        Called from webserver with threadname 'WEB-HCSYNC'
        or api with threadname 'API-HCSYNC'
        or scheduled task with threadname 'HCSYNC'

        Args:
            library: 'eBook', 'AudioBook' or empty for both
            userid: User ID to sync, or None for all users
            confirmed: True if user has confirmed large sync operations
            readonly: Forced readonly mode
        """
    logger = logging.getLogger(__name__)
    if ','.join([n.name.upper() for n in list(threading.enumerate())]).count('HCSYNC') > 1:
        msg = 'Another HardCover Sync is already running'
        logger.warning(msg)
        return json.dumps({
            'multi_user': False,
            'total_users': 1,
            'results': [{
                'userid': userid or 'unknown',
                'status': 'error',
                'message': msg
            }]
        })

    if not userid:
        # If no specific userid is provided, sync all users with HC tokens
        logger.info("No specific userid provided, syncing all users with HC tokens")
        logger.debug("Taking multi-user sync path")
        db = database.DBConnection()
        try:
            # Get all users with HC tokens
            users = db.select("SELECT UserID, hc_token FROM users WHERE hc_token IS NOT NULL AND hc_token != ''")
            if not users:
                msg = "No users with HC tokens found"
                logger.info(msg)
                return json.dumps({
                    'multi_user': True,
                    'total_users': 0,
                    'results': [],
                    'message': msg
                })

            # For testing multi-user sync UI when admin debug is enabled
            debug_duplicated = False
            if logging.getLogger('special.admin').isEnabledFor(logging.DEBUG) and len(users) == 1:
                logger.debug("Debug mode: Duplicating single user for multi-user sync UI testing")
                users = users * 5  # Create 5 copies of the same user for testing
                debug_duplicated = True

            logger.info(f"Starting HardCover sync for {len(users)} {plural(len(users), 'user')}")
            user_results = []
            for user in users:
                user_id = user['UserID']
                logger.info(f"Starting HC sync for user: {user_id}")
                # Create a HardCover instance with this user's token
                hc = HardCover()

                # Check if user has a hc_id, if not try to get one
                user_data = db.match("SELECT hc_id, hc_token FROM users WHERE UserID=?", (user_id,))
                if not user_data or not user_data['hc_id']:
                    logger.info(f"User {user_id} has no hc_id, attempting to get one")
                    whoami_result = hc.hc_whoami(userid=user_id, token=user_data['hc_token'])
                    if not str(whoami_result).isdigit():
                        logger.warning(f"Failed to get hc_id for user {user_id}: {whoami_result}")
                        user_results.append({
                            'userid': user_id,
                            'status': 'error',
                            'message': f"Failed to get hc_id: {whoami_result}"
                        })
                        continue

                # Now sync this user
                try:
                    user_msg = hc.sync(library, user_id, confirmed, readonly)
                    logger.info(f"Completed HC sync for user: {user_id}")

                    # Check if this user needs confirmation
                    if 'CONFIRMATION REQUIRED:' in user_msg:
                        # Return special JSON for confirmation
                        return json.dumps({
                            'multi_user': True,
                            'confirmation_required': True,
                            'userid': user_id,
                            'message': user_msg
                        })

                    user_results.append({
                        'userid': user_id,
                        'status': 'success',
                        'message': user_msg
                    })
                except Exception as e:
                    error_msg = f"Error syncing user {user_id}: {str(e)}"
                    logger.error(error_msg)
                    user_results.append({
                        'userid': user_id,
                        'status': 'error',
                        'message': str(e)
                    })

            # Return structured data for multi-user sync
            response = {
                'multi_user': True,
                'total_users': len(users),
                'results': user_results
            }

            # Add debug notice if users were duplicated for testing
            if debug_duplicated:
                response['debug_notice'] = "DEBUG MODE: Users duplicated for multi-user sync UI testing"

            return json.dumps(response)
        finally:
            db.close()
    else:
        # If a specific userid is provided, just sync that one
        logger.info(f"Starting hc_sync for userid: {userid}")
        logger.debug("Taking single-user sync path")
        hc = HardCover()
        try:
            msg = hc.sync(library, userid, confirmed, readonly)
            logger.info(f"Completed hc_sync for userid: {userid}")

            # Check if confirmation is required
            if 'CONFIRMATION REQUIRED:' in msg:
                return json.dumps({
                    'multi_user': False,
                    'confirmation_required': True,
                    'userid': userid,
                    'message': msg
                })

            # Return JSON for single user too
            return json.dumps({
                'multi_user': False,
                'total_users': 1,
                'results': [{
                    'userid': userid,
                    'status': 'success',
                    'message': msg
                }]
            })
        except Exception as e:
            error_msg = f"Error syncing user {userid}: {str(e)}"
            logger.error(error_msg)
            return json.dumps({
                'multi_user': False,
                'total_users': 1,
                'results': [{
                    'userid': userid,
                    'status': 'error',
                    'message': str(e)
                }]
            })


class HardCover:
    def __init__(self, name='', userid=None):
        """Initialize HardCover API handler."""
        self.hc_url = 'https://api.hardcover.app/'
        self.graphql_url = f"{self.hc_url}v1/graphql"
        self.book_url = f"{self.hc_url.replace('api.', '')}books/"
        self.auth_url = f"{self.hc_url.replace('api.', '')}authors/"
        self.HC_WHOAMI = 'query whoami { me { id } }'
        self.apikey = None
        self.logger = logging.getLogger(__name__)
        self.lt_cache = False
        self.searchinglogger = logging.getLogger('special.searching')
        self.syncinglogger = logging.getLogger('special.grsync')
        self.matchinglogger = logging.getLogger('special.matching')
        self.cachelogger = logging.getLogger('special.cache')
        self.provider = "HardCover"
        self.user_agent = 'LazyLibrarian ('
        if CONFIG['CURRENT_VERSION']:
            self.user_agent += CONFIG['CURRENT_VERSION']
        else:
            self.user_agent += f"{platform.system()} {platform.release()}"
        self.user_agent += ')'

        # If userid is provided, try to fetch the user's hc_token from the database
        if not userid:
            userid, _ = get_current_userid()
        if userid:
            db = database.DBConnection()
            res = db.match("SELECT hc_token FROM users WHERE UserID=?", (userid,))
            try:
                if res and res['hc_token']:
                    self.apikey = res['hc_token']
                    self.searchinglogger.debug(f"Using database token for user: {userid}")
                else:
                    self.searchinglogger.debug(f"No token found for user: {userid}, trying admin")
                    res = db.match("select hc_token from users where perms=65535 and hc_token is not null")
                    if res and res['hc_token']:
                        self.apikey = res['hc_token']
                        self.searchinglogger.debug("Using database token for admin")
                    else:
                        self.searchinglogger.debug("No admin HC token found")
            finally:
                db.close()
        else:
            db = database.DBConnection()
            # No userid provided, could be an api call? use admin token
            res = db.match("select hc_token from users where perms=65535 and hc_token is not null")
            if res and res['hc_token']:
                self.apikey = res['hc_token']
                self.searchinglogger.debug("Using database token for admin")
            db.close()

        #       user_id = result of whoami/userid
        self.HC_USERBOOKS = '''
            query mybooks { user_books(order_by: {date_added: desc} where:
              {status_id: {_eq: [status]}, user_id: {_eq: [whoami]}})
              {
                id
                book {
                       id
                       title
                }
              }
            }
'''
        self.HC_FINDBOOK = '''
query FindBook { books([order] where: [where])
   {
    title
    id
    cached_image
    description
    rating
    ratings_count
    pages
    subtitle
    slug
    release_date
    release_year
    cached_tags
    contributions:cached_contributors
    editions {
      isbn_10
      isbn_13
      publisher {
        name
      }
      language {
        language
      }
    }
    book_series {
      position
      series {
        name
        id
      }
    }
  }
}
'''

        self.HC_FINDAUTHORID = '''
query FindAuthorID {
  authors(order_by: {books_count: desc} where: {name: {_eq: "[authorname]"}}) {
    id
    name
    books_count
    slug
  }
}
'''
        self.HC_FINDAUTHORBYNAME = '''
query FindAuthorByName {
    search(query: "[authorname]", query_type: "author", sort:"_text_match:desc,books_count:desc") {
    results
  }
}
'''
        self.HC_FINDBOOKBYNAME = '''
query FindBookByName {
    search(query: "[title]", query_type: "book", sort:"_text_match:desc,users_count:desc") {
    results
  }
}
'''

        self.HC_ISBN13_BOOKS = self.HC_FINDBOOK.replace('[where]', '{isbn_13: {_eq: "[isbn]"}}'
                                                        ).replace('[order]', '')
        self.HC_ISBN10_BOOKS = self.HC_FINDBOOK.replace('[where]', '{isbn_10: {_eq: "[isbn]"}}'
                                                        ).replace('[order]', '')

        # queries using bookid, authorid, seriesid should be faster if we query using _by_pk e.g.
        # query books_by_pk rather than query books but at the moment that's not the case.

        self.HC_BOOKID_BOOKS = self.HC_FINDBOOK.replace('books([order] where: [where])', 'books_by_pk(id: [bookid])')

        self.HC_AUTHORID_BOOKS = self.HC_FINDBOOK.replace('[where]',
                                                          '{contributions: {author: {id: {_eq: "[authorid]"}}}}'
                                                          ).replace('[order]', '')

        self.HC_EDITION_BY_PK = '''
query EditionByPk {
  editions_by_pk(id: [editionid]) {
    language {
      language
    }
    title
    book_id
    contributions:cached_contributors
  }
}
'''
        self.HC_BOOK_SERIES = '''
query FindSeries { book_series(where: {series_id: {_eq: [seriesid]}})
    {
      position
      series {
        name
        id
      }
      book {
        id
        title
        release_date
        release_year
        contributions:cached_contributors
        editions {
          language {
            language
          }
        }
      }
    }
}
'''

        # 5 deep because of default_physical_edition/language/language
        # when hardcover implement the 3 level deep limit we will have to get the additional data
        # for each series member using default_physical_edition_id to get edition_by_pk, but it's much slower...
        self.HC_BOOK_SERIES_BY_PK = '''
query SeriesByPK {
  series_by_pk(id: "[seriesid]") {
    id
    name
    books_count
    primary_books_count
    book_series
        (where: {book: {book_status_id: {_eq: "1"}, compilation: {_eq: false}}}
        order_by: [{position: asc}, {book: {users_count: desc}}])
    {
      book_id
      position
      book {
        title
        release_date
        release_year
        default_physical_edition{
          language {
            language
          }
        }
        contributions:cached_contributors
        compilation
      }
    }
  }
}
'''
        self.HC_EDITIONS = '''
query FindEdition { editions(where: {book_id: {_eq: [bookid]}})
  {
    isbn_10
    isbn_13
    language {
      language
    }
    title
    publisher {
      name
    }
  }
}
'''
        self.HC_AUTHORINFO = '''
query FindAuthor { authors_by_pk(id: [authorid])
  {
    id
    name
    death_year
    death_date
    born_year
    born_date
    bio
    cached_image
    slug
  }
}'''

        self.HC_ADDUSERBOOK = '''
    mutation AddUserBook { insert_user_book (object: {book_id: [bookid], status_id: [status]}) { id, error }}
'''
        self.HC_DELUSERBOOK = '''
    mutation DelUserBook { delete_user_book (id: [bookid]) { id }}
'''

    def is_in_cache(self, expiry: int, hashfilename: str, myhash: str) -> bool:
        """Check if a cache file is valid."""
        if path_isfile(hashfilename):
            cache_modified_time = os.stat(hashfilename).st_mtime
            time_now = time.time()
            if expiry and cache_modified_time < time_now - expiry:
                # Cache entry is too old, delete it
                self.cachelogger.debug(f"Expiring {myhash}")
                os.remove(syspath(hashfilename))
                return False
            return True
        return False

    @staticmethod
    def read_from_cache(hashfilename: str) -> (str, bool):
        """Read a cached API response from disk."""
        with open(syspath(hashfilename), "rb") as cachefile:
            source = cachefile.read()
        return source, True

    @staticmethod
    def get_hashed_filename(cache_location: str, url: str) -> (str, str):
        """Generate a hashed filename for caching."""
        myhash = md5_utf8(url)
        hashfilename = os.path.join(cache_location, myhash[0], myhash[1], f"{myhash}.json")
        return hashfilename, myhash

    def result_from_cache(self, searchcmd: str, refresh=False) -> (str, bool):
        """Get API result from cache or fetch if needed."""
        headers = {'Content-Type': 'application/json',
                   'User-Agent': self.user_agent,
                   'authorization': self.apikey
                   }
        query = {'query': searchcmd}
        cache_location = DIRS.get_cachedir('JSONCache')
        filename = f"{self.graphql_url}/{str(query)}"
        hashfilename, myhash = self.get_hashed_filename(cache_location, filename)
        # CACHE_AGE is in days, so get it to seconds
        expire_older_than = CONFIG.get_int('CACHE_AGE') * 24 * 60 * 60
        valid_cache = self.is_in_cache(expire_older_than, hashfilename, myhash)
        if valid_cache and not refresh:
            lazylibrarian.CACHE_HIT += 1
            self.cachelogger.debug(f"CacheHandler: Returning CACHED response {hashfilename}")
            source, ok = self.read_from_cache(hashfilename)
            if ok:
                res = json.loads(source)
            else:
                res = {}
        else:
            lazylibrarian.CACHE_MISS += 1
            if BLOCKHANDLER.is_blocked(self.provider):
                return {}, False
            hc_api_sleep()
            try:
                http.client.HTTPConnection.debuglevel = 1 if lazylibrarian.REQUESTSLOG else 0
                r = requests.post(self.graphql_url, json=query, headers=headers)
                success = str(r.status_code).startswith('2')
            except requests.exceptions.ConnectionError as e:
                self.logger.error(str(e))
                success = False
                r = None
            if success:
                res = r.json()
                self.cachelogger.debug(f"CacheHandler: Storing json {myhash}")
                with open(syspath(hashfilename), "w") as cachefile:
                    cachefile.write(json.dumps(res))
            else:
                # expected failure codes...
                # 401 expired or invalid api token
                # 403 blocked action (invalid query type, depth limit exceeded, multiple queries)
                # 429 rate limit of 60 per minute exceeded
                # 500 internal server error
                # On 429 error we should get headers
                # RateLimit-Limit 60 (requests per minute)
                # RateLimit-Remaining 0 (none left)
                # RateLimit-Reset 1735843440 (unix seconds count when reset)
                delay = 0
                if r.status_code == 429:
                    limit = r.headers.get('RateLimit-Limit', 'Unknown')
                    remaining = r.headers.get('RateLimit-Remaining', 'Unknown')
                    reset = r.headers.get('RateLimit-Reset', 'Unknown')
                    sleep_time = 0.0
                    reset = check_int(reset, 0)
                    if reset:
                        sleep_time = reset - time.time()
                        reset = time.strftime("%H:%M:%S", time.localtime(reset))
                    self.logger.debug(f"429 error. Limit {limit}, Remaining {remaining}, Reset {reset}")
                    if sleep_time > 0.0:
                        if sleep_time < 5.0:  # short waits just sleep a bit
                            time.sleep(sleep_time)
                            lazylibrarian.TIMERS['SLEEP_HC'] += sleep_time
                        else:  # longer waits block provider and continue
                            delay = int(sleep_time)
                elif r.status_code in [401, 403]:
                    # allow time for user to update
                    delay = 24 * 3600
                    if r.status_code == 401:
                        self.logger.error("Invalid or missing HardCover API key")
                    else:
                        self.logger.error(f"Invalid HardCover request: Status code {r.status_code}")
                elif r.status_code == 500:
                    # time for hardcover to fix error
                    delay = 2 * 3600
                    self.logger.error("HardCover internal server error")
                else:
                    # unexpected error code, short delay
                    delay = 60
                    self.logger.error(f"Unexpected HardCover error: Status code {r.status_code}")
                # noinspection PyBroadException
                try:
                    res = r.json()
                    msg = str(r.status_code)
                except Exception:
                    res = {}
                    msg = "Unknown reason"
                if 'error' in res:
                    msg = str(res['error'])
                    self.logger.error(msg)
                if delay:
                    BLOCKHANDLER.block_provider(self.provider, msg, delay=delay)
        return res, valid_cache

    def get_series_members(self, series_ident=None, series_title='', queue=None, refresh=False):
        """Get all books in a series from HardCover."""
        resultlist = []
        resultdict = {}
        author_name = ''
        api_hits = 0
        cache_hits = 0
        searchcmd = self.HC_BOOK_SERIES_BY_PK.replace('[seriesid]', str(series_ident)[2:])
        results, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
        api_hits += not in_cache
        cache_hits += in_cache
        if 'error' in results:
            self.logger.error(str(results['error']))
        if 'data' in results and 'series_by_pk' in results['data'] and results['data']['series_by_pk']:
            wantedlanguages = get_list(CONFIG['IMP_PREFLANG'])
            series_id = f"HC{str(results['data']['series_by_pk']['id'])}"
            if series_id != series_ident:
                self.logger.debug(f"Series id mismatch for {series_id}, {series_ident}")
            series_name = results['data']['series_by_pk']['name']
            # primary_books_count = results['data']['series_by_pk']['primary_books_count']
            if series_name != series_title:
                match = fuzz.partial_ratio(series_name, series_title)
                if match < 95:
                    self.logger.debug(f"Series name mismatch for {series_id}, "
                                      f"{round(match, 2)}% {series_name}/{series_title}")

            for entry in results['data']['series_by_pk']['book_series']:
                # use HC_EDITION_BY_PK to get language, authorid, authorname
                # This will be needed if hardcover impose query depth limits
                # editionid = entry['book']['default_physical_edition_id']
                # searchcmd = self.HC_EDITION_BY_PK.replace('[editionid]', str(editionid))
                # editions, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
                # api_hits += not in_cache
                # cache_hits += in_cache
                # language = ''
                # authorname = ''
                # authorlink = ''
                # if 'errors' in editions:
                #    self.logger.error(str(editions['errors']))
                # elif 'data' in editions and 'editions_by_pk' in editions['data']:
                #    edition = editions['data']['editions_by_pk']
                #    if edition['language']:
                #        language = edition['language']['language']
                #    if edition['contributions']:
                #        authorname = edition['contributions'][0]['author']['name']
                #        authorlink = edition['contributions'][0]['author']['id']
                try:
                    authorname = entry['book']['contributions'][0]['author']['name']
                    authorlink = entry['book']['contributions'][0]['author']['id']
                    edition = entry['book']['default_physical_edition']
                    language = ''
                    if edition and 'language' in edition and edition.get('language'):
                        language = edition['language']['language']

                    book_id = entry['book_id']
                    position = entry['position']
                    if not position or str(position) == 'None':
                        position = 0
                    book_title = entry['book']['title']
                    # pubyear = entry['book']['release_year']
                    pubdate = entry['book']['release_date']
                    compilation = entry['book']['compilation']

                    if not author_name:
                        author_name = authorname
                    if not language:
                        language = 'Unknown'
                    # pick the first entry for each position that is non compilation and in a language we want
                    if not compilation and position and (position not in resultdict or
                                                         resultdict[position][1] != author_name):
                        if 'All' in wantedlanguages or language in wantedlanguages:
                            resultdict[position] = [book_title, authorname, authorlink, book_id, pubdate, language]
                        else:
                            self.logger.debug(f"Rejecting {position}:{book_title} as language {language}")
                except (IndexError, KeyError, TypeError):
                    self.logger.debug(f"Error in HC data: {entry}")

            for item in resultdict:
                res = [item]
                res.extend(resultdict[item])
                resultlist.append(res)
            resultlist = sorted(resultlist)
            self.logger.debug(f"Found {len(resultlist)} for series {series_id}: {series_name}")
            self.logger.debug(f"Used {api_hits} api hit, {cache_hits} in cache")

        if not queue:
            return resultlist

        queue.put(resultlist)
        return None

    def find_results(self, searchterm=None, queue=None, refresh=False):
        """Search for books or authors in HardCover."""
        # noinspection PyBroadException
        try:
            resultlist = []
            resultcount = 0
            api_hits = 0
            cache_hits = 0
            searchtitle = ''
            searchauthorname = ''
            searchcmd = ''
            resultbooks = []
            authids = []
            if is_valid_isbn(searchterm):
                if len(searchterm) == 13:
                    searchcmd = self.HC_ISBN13_BOOKS.replace('[isbn]', searchterm)
                else:
                    searchcmd = self.HC_ISBN10_BOOKS.replace('[isbn]', searchterm)
                self.searchinglogger.debug(f"ISBN_BOOKS {searchterm}")
                results, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
                api_hits += not in_cache
                cache_hits += in_cache
                with contextlib.suppress(IndexError, KeyError, TypeError):
                    resultbooks = results['data']['books']

            if not searchcmd:  # not isbn search, could be author, title, both
                if '<ll>' in searchterm:  # special token separates title from author
                    searchtitle, searchauthorname = searchterm.split('<ll>')
                    searchterm = searchterm.replace('<ll>', ' ').strip()
                    searchtitle = searchtitle.split(' (')[0].strip()  # without any series info
                else:
                    # could be either... Try title first, if no results try authorname
                    searchtitle = searchterm
                    searchauthorname = None

                if searchtitle:
                    searchcmd = self.HC_FINDBOOKBYNAME.replace('[title]', searchtitle)
                    self.searchinglogger.debug(f"FINDBOOKBYNAME {searchtitle}")
                    bookresults, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
                    api_hits += not in_cache
                    cache_hits += in_cache
                    with contextlib.suppress(IndexError, KeyError, TypeError):
                        for item in bookresults['data']['search']['results']['hits']:
                            resultbooks.append(item['document'])
                    if not resultbooks:
                        searchauthorname = searchterm

                if searchauthorname:
                    searchcmd = self.HC_FINDAUTHORBYNAME.replace('[authorname]', searchauthorname)
                    self.searchinglogger.debug(f"FINDAUTHORBYNAME {searchauthorname}")
                    authresults, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
                    api_hits += not in_cache
                    cache_hits += in_cache
                    with contextlib.suppress(IndexError, KeyError, TypeError):
                        for item in authresults['data']['search']['results']['hits']:
                            authids.append(item['document']['id'])

                if authids:
                    for authid in authids:
                        searchcmd = self.HC_AUTHORID_BOOKS.replace('[authorid]', authid)
                        self.searchinglogger.debug(f"AUTHORID_BOOKS {authid}")
                        results, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
                        api_hits += not in_cache
                        cache_hits += in_cache
                        if 'error' in results:
                            self.logger.error(str(results['error']))
                        if "data" in results:
                            books = results['data']['books']
                            for book in books:
                                if book not in resultbooks:
                                    resultbooks.append(book)

            if not resultbooks:
                if not queue:
                    return resultlist
                queue.put(resultlist)

            for book_data in resultbooks:
                if 'users_count' in book_data:
                    # search results return a different layout to books
                    bookdict = self.get_searchdict(book_data)
                else:
                    bookdict = self.build_bookdict(book_data)

                if searchauthorname:
                    author_fuzz = fuzz.token_sort_ratio(bookdict['authorname'], searchauthorname)
                else:
                    author_fuzz = fuzz.token_sort_ratio(bookdict['authorname'], searchterm)
                book_title = bookdict['bookname']
                if book_title:
                    if searchtitle:
                        if book_title.endswith(')'):
                            book_title = book_title.rsplit(' (', 1)[0]
                        book_fuzz = fuzz.token_set_ratio(book_title.lower(), searchtitle.lower())
                        # lose a point for each extra word in the fuzzy matches, so we get the closest match
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
                        bookdict['isbn'] = searchterm

                    highest_fuzz = max((author_fuzz + book_fuzz) / 2, isbn_fuzz)

                    resultlist.append({
                        'authorname': bookdict['authorname'],
                        'bookid': bookdict['bookid'],
                        'authorid': bookdict['authorid'],
                        'bookname': bookdict['bookname'],
                        'booksub': bookdict['booksub'],
                        'bookisbn': bookdict['bookisbn'],
                        'bookpub': bookdict['bookpub'],
                        'bookdate': bookdict['bookdate'],
                        'booklang': bookdict['booklang'],
                        'booklink': bookdict['booklink'],
                        'bookrate': bookdict['bookrate'],
                        'bookrate_count': bookdict['bookrate_count'],
                        'bookimg': bookdict['bookimg'],
                        'bookpages': bookdict['bookpages'],
                        'bookgenre': bookdict['bookgenre'],
                        'bookdesc': bookdict['bookdesc'],
                        'workid': bookdict['bookid'],  # TODO should this be canonical id?
                        'author_fuzz': round(author_fuzz, 2),
                        'book_fuzz': round(book_fuzz, 2),
                        'isbn_fuzz': round(isbn_fuzz, 2),
                        'highest_fuzz': round(highest_fuzz, 2),
                        'source': "HardCover"
                    })
                    resultcount += 1

            self.logger.debug(f"Found {len(resultlist)} {plural(len(resultlist), 'result')}, "
                              f"Used {api_hits} api hit, {cache_hits} in cache")
            queue.put(resultlist)

        except Exception:
            self.logger.error(f'Unhandled exception in HC.find_results: {traceback.format_exc()}')

    def find_author_id(self, authorname='', title='', refresh=False):
        """Find HardCover author ID for a name or title."""
        api_hits = 0
        authorname = authorname.replace('#', '').replace('/', '_')
        authorname = format_author_name(authorname, postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))

        if not title:
            # we only have an authorname. Return id of matching author with the most books
            self.logger.debug(f"Searching for author {authorname}, refresh={refresh}")
            searchcmd = self.HC_FINDAUTHORBYNAME.replace('[authorname]', authorname)
            results, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
            api_hits += not in_cache
            authorid = None
            matches = []
            if results:
                with contextlib.suppress(IndexError, KeyError, TypeError):
                    for item in results['data']['search']['results']['hits']:
                        name = item['document']['name']
                        altnames = item['document']['alternate_names']
                        books_count = item['document']['books_count']
                        author_id = item['document']['id']
                        if authorname == name or authorname in altnames:
                            matches.append([books_count, author_id, name, altnames])
                    matches = sorted(matches, reverse=True)
                    authorid = matches[0][1]

            if authorid:
                res = self.get_author_info(authorid)
                if res:
                    if res['authorname'] != authorname:
                        res['aka'] = authorname
                    self.logger.debug(f"Authorname search used {api_hits} api hit")
                    return res
        else:
            # search for the title and then check the authorname matches
            self.logger.debug(f"Searching for title {title}, refresh={refresh}")
            searchcmd = self.HC_FINDBOOKBYNAME.replace('[title]', title)
            results, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
            api_hits += not in_cache
            bookid = None
            if results:
                with contextlib.suppress(IndexError, KeyError, TypeError):
                    for item in results['data']['search']['results']['hits']:
                        if authorname in item['document']['author_names']:
                            bookid = item['document']['id']
                            break
            if bookid:
                url = None
                try:
                    for item in results['data']['search']['results']['hits']:
                        # Check contributions for cached image
                        if item['contributions'] and 'cachedImage' in item['contributions'][0]['author']:
                            url = item['contributions'][0]['author']['cachedImage']['url']
                            break
                except (IndexError, KeyError, TypeError):
                    pass

                if url:
                    # try to extract the authorid from the image url
                    parts = url.split('/')
                    if len(parts) == 6:
                        authorid = parts[4]
                        res = self.get_author_info(authorid)
                        if res:
                            if res['authorname'] != authorname:
                                res['aka'] = authorname
                            self.logger.debug(f"Title search used {api_hits} api hit")
                            return res

                # get the authorid from the book page as it's not in the title search results
                bookidcmd = self.HC_BOOKID_BOOKS.replace('[bookid]', str(bookid))
                results, in_cache = self.result_from_cache(bookidcmd, refresh=refresh)
                api_hits += not in_cache
                if results and 'data' in results:
                    book_data = results['data'].get('books_by_pk', {})
                    # Check all contributions for author match
                    for contrib in book_data.get('contributions', []):
                        # might be more than one author listed
                        author_name = contrib['author']['name']
                        authorid = str(contrib['author']['id'])
                        res = None
                        match = fuzz.ratio(author_name.lower(), authorname.lower())
                        if match >= CONFIG.get_int('NAME_RATIO'):
                            res = self.get_author_info(authorid)
                        if not res:
                            match = fuzz.token_sort_ratio(author_name.lower(), authorname.lower())
                            if match >= CONFIG.get_int('NAME_RATIO'):
                                res = self.get_author_info(authorid)
                        if not res:
                            match = fuzz.partial_ratio(author_name.lower(), authorname.lower())
                            if match >= CONFIG.get_int('NAME_PARTNAME'):
                                res = self.get_author_info(authorid)
                        if res:
                            if res['authorname'] != authorname:
                                res['aka'] = authorname
                            self.logger.debug(f"Author/book search used {api_hits} api hit")
                            return res

        self.logger.debug(f"No results. Used {api_hits} api hit")
        return {}

    def get_author_info(self, authorid=None, authorname=None, refresh=False):
        """Get detailed info for a HardCover author."""
        author_name = ''
        author_born = ''
        author_died = ''
        author_link = ''
        author_img = ''
        about = ''
        totalbooks = 0
        api_hits = 0
        cache_hits = 0

        self.logger.debug(f"Getting HC author info for {authorid}, refresh={refresh}")
        searchcmd = self.HC_AUTHORINFO.replace('[authorid]', str(authorid))
        results, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
        api_hits += not in_cache
        cache_hits += in_cache
        if 'error' in results:
            self.logger.error(str(results['error']))
        if results and 'data' in results:
            author = results['data'].get('authors_by_pk', {})
            if author and str(author['id']) == str(authorid):
                author_name = author.get('name', '')
                # hc sometimes returns multiple comma separated names, use the one we are looking for
                if authorname and authorname in author_name:
                    author_name = authorname
                author_born = author.get('born_date', '')
                author_died = author.get('death_date', '')
                totalbooks = author.get('books_count', 0)
                about = author.get('bio', '')
                author_link = author.get('slug', '')
                if author_link:
                    author_link = self.auth_url + author_link

                if 'cached_image' in author:
                    img = author['cached_image'].get('url', '')
                    if img and '/books/' not in img:
                        # hardcover image bug, sometimes gives us a book cover instead of author image
                        author_img = author['cached_image']['url']

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

        self.logger.debug(f"[{author_name}] Returning HC info for authorID: {authorid}")
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

    def build_bookdict(self, book_data):
        """Convert HardCover book data to a standard dict."""
        bookdict = {'booklang': '', 'bookpub': '', 'authorname': '', 'authorid': '0',
                    'bookcover': '', 'bookisbn': '', 'series': [], 'contributors': [], 'source': 'HardCover'}

        # Filter and select primary author from contributions
        if 'contributions' in book_data and len(book_data['contributions']):
            contributions = book_data['contributions']
            # Filter for only null or "Author" contributions
            author_contributions = [c for c in contributions if c.get('contribution') is None
                                    or c.get('contribution') == "Author"]
            if not author_contributions:
                # If no author contributions found, fall back to the original list
                author_contributions = contributions

            # Sort contributions by author name
            sorted_contributions = sorted(author_contributions, key=lambda x: x['author']['name'])

            author = sorted_contributions[0]
            bookdict['authorname'] = " ".join(author['author']['name'].split())
            # not all hardcover entries have an id???
            bookdict['authorid'] = str(author['author'].get('id', '0'))
            if len(sorted_contributions) > 1:
                sorted_contributions.pop(0)
                for item in sorted_contributions:
                    bookdict['contributors'].append([str(item['author'].get('id', '')),
                                                     " ".join(item['author']['name'].split())])

        bookdict['bookname'] = book_data.get('title', '')
        bookdict['booksub'] = book_data.get('subtitle', '')
        if 'cached_image' in book_data and book_data['cached_image'].get('url'):
            bookdict['bookimg'] = book_data['cached_image']['url']
        editions = book_data.get('editions', [])
        for edition in editions:
            if edition.get('isbn_13'):
                bookdict['bookisbn'] = edition['isbn_13']
                break
            if edition.get('isbn_10'):
                bookdict['bookisbn'] = edition['isbn_10']
                break
        bookseries = book_data.get('book_series', [])
        for series in bookseries:
            bookdict['series'].append([series['series']['name'], f"HC{series['series']['id']}", series['position']])
        bookdict['booklink'] = book_data.get('slug', '')
        if bookdict['booklink']:
            bookdict['booklink'] = self.book_url + bookdict['booklink']
        bookdict['bookrate'] = book_data.get('rating', 0)
        bookdict['bookrate_count'] = book_data.get('ratings_count', 0)
        if bookdict['bookrate'] is None:
            bookdict['bookrate'] = 0
        else:
            bookdict['bookrate'] = round(bookdict['bookrate'], 2)

        bookdict['bookpages'] = book_data.get('pages', 0)
        if bookdict['bookpages'] is None:
            bookdict['bookpages'] = 0
        bookdict['bookdesc'] = book_data.get('description', '')
        bookdict['bookid'] = str(book_data.get('id', ''))
        bookdict['bookdate'] = book_data.get('release_date', '')
        if not bookdict['bookdate']:
            bookdict['bookdate'] = ''
        else:
            bookdict['bookdate'] = date_format(bookdict['bookdate'],
                                               context=f"{bookdict['authorname']}/{bookdict['bookname']}",
                                               datelang=CONFIG['DATE_LANG'])
        bookdict['first_publish_year'] = book_data.get('release_year', '')
        bookgenre = ''
        genres = []
        cached_tags = book_data['cached_tags']
        if 'Genre' in cached_tags:
            book_genres = cached_tags['Genre']
            for genre in book_genres:
                genres.append(genre['tag'])
        if genres:
            if lazylibrarian.GRGENRES:
                genre_limit = lazylibrarian.GRGENRES.get('genreLimit', 3)
            else:
                genre_limit = 3
            genres = list(set(genres))
            bookgenre = ', '.join(genres[:genre_limit])
        bookdict['bookgenre'] = bookgenre
        langs = []
        for edition in editions:
            if edition.get('language'):
                lang = edition['language']['language']
                if lang:
                    langs.append(lang)
        if langs:
            bookdict['booklang'] = ', '.join(set(langs))
        pubs = []
        for edition in editions:
            if edition.get('publisher'):
                pub = edition['publisher']['name']
                if pub:
                    pubs.append(pub)
        if pubs:
            bookdict['bookpub'] = ', '.join(set(pubs))
        bookdict['id_librarything'] = ""
        if not bookdict.get('bookimg'):
            bookdict['bookimg'] = 'images/nocover.png'
        return bookdict

    def get_searchdict(self, book_data):
        """Convert HardCover search result to a book dict."""
        bookdict = {'authorid': '0', 'authorname': '', 'source': 'HardCover'}

        # Filter and select primary author from contributions
        if 'contributions' in book_data and len(book_data['contributions']):
            contributions = book_data['contributions']
            # Filter for only null or "Author" contributions
            author_contributions = [c for c in contributions if c.get('contribution') is None
                                    or c.get('contribution') == "Author"]
            if not author_contributions:
                # If no author contributions found, fall back to the original list
                author_contributions = contributions
            # Sort contributions by author name in asscending order
            sorted_contributions = sorted(author_contributions, key=lambda x: x['author']['name'])

            author = sorted_contributions[0]
            bookdict['authorname'] = " ".join(author['author']['name'].split())
            # not all hardcover entries have an id???
            bookdict['authorid'] = str(author['author'].get('id', '0'))

        bookdict['bookname'] = book_data.get('title', '')
        bookdict['booksub'] = book_data.get('subtitle', '')
        bookdict['bookimg'] = ""
        if 'image' in book_data and book_data['image'].get('url'):
            bookdict['bookimg'] = book_data['image']['url']
        isbns = book_data.get('isbns', [])
        bookdict['bookisbn'] = ""
        if isbns:
            bookdict['bookisbn'] = isbns[0]
        bookdict['series'] = []
        bookdict['booklink'] = book_data.get('slug', '')
        if bookdict['booklink']:
            bookdict['booklink'] = self.book_url + bookdict['booklink']
        bookdict['bookrate'] = book_data.get('rating', 0)
        bookdict['bookrate_count'] = book_data.get('ratings_count', 0)
        if bookdict['bookrate'] is None:
            bookdict['bookrate'] = 0
        else:
            bookdict['bookrate'] = round(bookdict['bookrate'], 2)

        bookdict['bookpages'] = book_data.get('pages', 0)
        if bookdict['bookpages'] is None:
            bookdict['bookpages'] = 0
        bookdict['bookdesc'] = book_data.get('description', '')
        bookdict['bookid'] = str(book_data.get('id', ''))
        bookdict['bookdate'] = book_data.get('release_date', '')
        if not bookdict['bookdate']:
            bookdict['bookdate'] = ''
        else:
            bookdict['bookdate'] = date_format(bookdict['bookdate'],
                                               context=f"{bookdict['authorname']}/{bookdict['bookname']}",
                                               datelang=CONFIG['DATE_LANG'])
        bookdict['first_publish_year'] = book_data.get('release_year', '')
        bookgenre = ''
        genres = []
        cached_tags = book_data['tags']
        if 'Genre' in cached_tags:
            book_genres = cached_tags['Genre']
            for genre in book_genres:
                genres.append(genre['tag'])
        if genres:
            if lazylibrarian.GRGENRES:
                genre_limit = lazylibrarian.GRGENRES.get('genreLimit', 3)
            else:
                genre_limit = 3
            genres = list(set(genres))
            bookgenre = ', '.join(genres[:genre_limit])
        bookdict['bookgenre'] = bookgenre
        bookdict['booklang'] = ""
        bookdict['bookpub'] = ""
        bookdict['id_librarything'] = ""
        if not bookdict['bookimg']:
            bookdict['bookimg'] = 'images/nocover.png'
        return bookdict

    def get_author_books(self, authorid=None, authorname=None, bookstatus="Skipped", audiostatus='Skipped',
                         entrystatus='Active', refresh=False, reason='hc.get_author_books'):
        """Import all books for an author from HardCover."""
        cache_hits = 0
        hc_id = ''
        db = database.DBConnection()
        try:
            match = db.match('SELECT authorid,hc_id FROM authors where authorid=? or hc_id=?', (authorid, authorid))
            if match:
                hc_id = match['hc_id']
                authorid = match['authorid']
            if not hc_id:
                hc_id = authorid

            # Artist is loading
            db.action("UPDATE authors SET Status='Loading' WHERE AuthorID=?", (authorid,))

            searchcmd = self.HC_AUTHORID_BOOKS.replace('[authorid]', hc_id)
            results, in_cache = self.result_from_cache(searchcmd, refresh=refresh)
            cache_hits += in_cache
            if 'error' in results:
                self.logger.error(str(results['error']))
            if not results or 'data' not in results:
                db.action("UPDATE authors SET Status=? WHERE AuthorID=?", (entrystatus, authorid))
                return

            self.logger.debug(f"HC found {len(results['data']['books'])} results")
            resultqueue = Queue()
            resultlist = []
            for book_data in results['data']['books']:
                bookdict = self.build_bookdict(book_data)
                if (bookdict['authorname'] != authorname and 'contributions' in book_data
                        and len(book_data['contributions'])):
                    # not our author, might be a contributor to an anthology?
                    # Check all contributions (already filtered in build_bookdict) for name match
                    for contrib in book_data['contributions']:
                        if (fuzz.token_set_ratio(contrib['author']['name'], authorname) >=
                                CONFIG.get_int('NAME_RATIO')):
                            bookdict['authorname'] = " ".join(contrib['author']['name'].split())
                            bookdict['authorid'] = str(contrib['author']['id'])
                            break

                bookdict['status'] = bookstatus
                bookdict['audiostatus'] = audiostatus

                resultlist.append(bookdict)

            resultqueue.put(resultlist)
            _ = add_author_books_to_db(resultqueue, bookstatus, audiostatus, entrystatus, reason, authorid,
                                       self.get_series_members, self.get_bookdict_for_bookid, cache_hits=cache_hits)

        finally:
            db.close()

    def get_bookdict_for_bookid(self, bookid=None):
        """Get a book's details from HardCover by ID."""
        bookidcmd = self.HC_BOOKID_BOOKS.replace('[bookid]', str(bookid))
        results, in_cache = self.result_from_cache(bookidcmd, refresh=False)
        bookdict = {}
        if 'error' in results:
            self.logger.error(str(results['error']))
        if 'data' in results and results['data'].get('books_by_pk'):
            bookdict = self.build_bookdict(results['data']['books_by_pk'])
        return bookdict, in_cache

    def add_bookid_to_db(self, bookid=None, bookstatus=None, audiostatus=None,
                         reason='hc.add_bookid_to_db', bookdict=None):
        """Import a single book from HardCover by ID."""
        if not bookdict:
            bookdict, _ = self.get_bookdict_for_bookid(bookid)
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
        bookdict['reason'] = f"[{thread_name()}] {reason}"
        res = add_bookdict_to_db(bookdict)
        lazylibrarian.importer.update_totals(bookdict['authorid'])
        return res

    def hc_whoami(self, userid=None, token=None):
        """Get the HardCover user ID for the current token."""
        logger = logging.getLogger(__name__)
        msg = ''
        if token:
            logger.debug(f"Sending whoami with token: {token}")

        if userid:
            logger.debug(f"Sending whoami with userid: {userid}")

        if not userid:
            userid, msg = get_current_userid()

        if not userid:
            logger.error(f"No userid found for whoami: {msg}")
            return msg

        if token:  # We're doing an update of the user's hc_id
            self.apikey = token
            logger.debug(f"Using supplied token for userid {userid}")

        # Make sure we're using the right token for this user, if one wasn't directly supplied
        # This theoretically shouldn't get hit since we fetch the token at initialization
        if userid and not token:
            db = database.DBConnection()
            try:
                res = db.match("SELECT hc_token FROM users WHERE UserID=?", (userid,))
                if res and res['hc_token'] and self.apikey != res['hc_token']:
                    logger.debug(f"Incorrect token fetched. Updating token for whoami request for user: {userid}")
                    self.apikey = res['hc_token']
            finally:
                db.close()

        searchcmd = self.HC_WHOAMI
        results, _ = self.result_from_cache(searchcmd, refresh=True)
        logger.debug(f"whoami results for user {userid}: {results}")

        if 'error' in results:
            logger.error(f"Error in whoami for user {userid}: {str(results['error'])}")
            return str(results['error'])

        if 'data' in results and 'me' in results['data']:
            res = results['data']['me']
            whoami = res[0]['id']
            if whoami:
                db = database.DBConnection()
                try:
                    db.upsert("users", {'hc_id': whoami}, {'UserID': userid})
                    logger.info(f"whoami success: {whoami} for userid {userid}")
                    return whoami
                finally:
                    db.close()

        logger.warning(f"whoami fallback result for user {userid}: {results}")
        return str(results)

    def _fetch_hc_books_by_status(self, whoami, status_value, status_name):
        """Fetch books from HardCover for a specific reading status."""
        searchcmd = self.HC_USERBOOKS.replace('[whoami]', str(whoami)).replace('[status]', str(status_value))
        results, _ = self.result_from_cache(searchcmd, refresh=True)

        if 'error' in results:
            self.logger.error(str(results['error']))
            return []

        if 'data' in results and 'user_books' in results['data']:
            book_count = len(results['data']['user_books'])
            self.syncinglogger.debug(f"HardCover {status_name} contains {book_count}")
            return results['data']['user_books']
        return []

    def _process_hc_book(self, item, db, remapped, sync_dict, stats, readonly=False):
        """Process a single HardCover book entry."""
        hc_id = item['book']['id']
        res = db.match("SELECT bookid from books WHERE hc_id=?", (hc_id,))

        if res and res['bookid']:
            if res['bookid'] in remapped:
                if not CONFIG.get_bool('HC_SYNCREADONLY') and not readonly:
                    self.syncinglogger.debug(f"Duplicated entry {hc_id} for {res['bookid']}")
                    delcmd = self.HC_DELUSERBOOK.replace('[bookid]', str(item['id']))
                    results, _ = self.result_from_cache(delcmd, refresh=True)
                    if 'error' in results:
                        self.logger.error(str(results['error']))
                else:
                    self.syncinglogger.debug(f"Duplicated entry {hc_id} for {res['bookid']},"
                                             f" but two-way sync is disabled")
            else:
                remapped.append(res['bookid'])
                sync_dict[res['bookid']] = item['id']
                stats['books_matched'] += 1
                return res['bookid']
        else:
            # Book not found in database, try to add it
            book_id = self._add_missing_book(hc_id, item, db, remapped, sync_dict, stats)
            return book_id

        return None

    def _add_missing_book(self, hc_id, item, db, remapped, sync_dict, stats):
        """Add a book that's missing from the database."""
        self.syncinglogger.warning(f"Book {hc_id} not found in database")
        newbookdict, _ = self.get_bookdict_for_bookid(str(hc_id))

        if not newbookdict:
            self.syncinglogger.debug(f"No bookdict found for {hc_id}")
            return None

        auth_name, auth_id = lazylibrarian.importer.get_preferred_author(newbookdict['authorname'])

        # Check for exact matches first
        exact_match = self._find_exact_book_match(db, newbookdict, auth_name)

        if exact_match:
            self._handle_exact_match(exact_match, hc_id, item, db, remapped, sync_dict, stats)
        else:
            # No exact match found - add as new book
            if not auth_id:
                # need to add the author first...
                auth_id = lazylibrarian.importer.add_author_to_db(authorname=auth_name, refresh=False,
                                                                  authorid=newbookdict['authorid'], addbooks=False,
                                                                  reason=f"HC sync {newbookdict['bookname']}")

            newbookdict['authorid'] = auth_id  # use LL preferred authorid, not necessarily hc_id
            self.syncinglogger.debug(f"No exact match found for {hc_id} {auth_id}:{auth_name} "
                                     f"'{newbookdict['bookname']}' - adding as new book")
            self.add_bookid_to_db(str(hc_id), bookdict=newbookdict)
            stats['new_books_added'] += 1

            # Update tracking structures for newly added book
            added_book = db.match("SELECT bookid FROM books WHERE hc_id=?", (str(hc_id),))
            if added_book and added_book['bookid']:
                book_id = added_book['bookid']
                if book_id not in remapped:
                    remapped.append(book_id)
                    sync_dict[book_id] = item['id']
                    self.syncinglogger.debug(f"Added newly created book {book_id} to tracking")
                    return book_id
        return None

    @staticmethod
    def _find_exact_book_match(db, bookdict, auth_name):
        """Find exact book match by ISBN or title/author."""
        exact_match = None

        if bookdict.get('bookisbn'):
            # First try ISBN match - most reliable
            exact_match = db.match(
                "SELECT BookID,hc_id,bookname FROM books WHERE BookISBN=? AND "
                "AuthorID=(SELECT AuthorID FROM authors WHERE AuthorName=?)",
                (bookdict['bookisbn'], auth_name)
            )

        if not exact_match:
            # Try exact title and author match
            exact_match = db.match(
                "SELECT books.BookID,books.hc_id,books.bookname FROM books,authors WHERE "
                "books.AuthorID=authors.AuthorID AND books.BookName=? AND authors.AuthorName=?",
                (bookdict['bookname'], auth_name)
            )

        return exact_match

    def _handle_exact_match(self, exact_match, hc_id, item, db, remapped, sync_dict, stats, readonly=False):
        """Handle when an exact book match is found."""
        self.syncinglogger.debug(f"Found exact match {hc_id} for bookid {exact_match['BookID']}, "
                                 f"current hc_id {exact_match['hc_id']}")

        if exact_match['BookID'] in remapped:
            if not CONFIG.get_bool('HC_SYNCREADONLY') and not readonly:
                self.syncinglogger.debug(f"Duplicated entry {hc_id} for {exact_match['BookID']}")
                delcmd = self.HC_DELUSERBOOK.replace('[bookid]', str(item['id']))
                results, _ = self.result_from_cache(delcmd, refresh=True)
                if 'error' in results:
                    self.logger.error(str(results['error']))
            else:
                self.syncinglogger.debug(f"Duplicated entry {hc_id} for {exact_match['BookID']}, "
                                         f"but two-way sync is disabled")
        else:
            remapped.append(exact_match['BookID'])
            sync_dict[exact_match['BookID']] = item['id']
            stats['books_matched'] += 1
            db.action("UPDATE books SET hc_id=? WHERE bookid=?", (str(hc_id), exact_match['BookID']))

    def _get_last_sync_lists(self, db, userid):
        """Get the last synced reading lists for a user."""
        lists = {
            'toread': [],
            'reading': [],
            'read': [],
            'dnf': []
        }

        for list_name in lists:
            res = db.match("select SyncList from sync where UserID=? and Label=?", (userid, f"hc_{list_name}"))
            if res:
                lists[list_name] = get_list(res['SyncList'])
                self.syncinglogger.debug(f"last {list_name} contains {len(lists[list_name])} for user {userid}")

        return lists

    @staticmethod
    def _process_reading_list_changes(mapping):
        """Process changes between HardCover and LazyLibrarian reading lists."""
        reading_list_changes = []

        for mapp in mapping:
            hc_list, last_list, ll_list, list_name = mapp

            added_to_shelf = list(set(hc_list) - set(last_list))
            removed_from_shelf = list(set(last_list) - set(hc_list))
            added_to_ll = list(set(ll_list) - set(hc_list))
            removed_from_ll = list(set(last_list) - set(ll_list))

            additions = set(added_to_shelf + added_to_ll)
            removals = set(removed_from_shelf + removed_from_ll)

            cnt = 0
            for item in additions:
                if item not in ll_list:
                    ll_list.append(item)
                    cnt += 1
            if cnt:
                reading_list_changes.append(f"• Added {cnt} {plural(cnt, 'book')} to {list_name} list")

            cnt = 0
            for item in removals:
                if item in ll_list:
                    ll_list.remove(item)
                    cnt += 1
            if cnt:
                reading_list_changes.append(f"• Removed {cnt} {plural(cnt, 'book')} from {list_name} list")

        return reading_list_changes

    def _process_wanted_books(self, db, added_to_wanted, stats):
        """Process books that were added to the wanted list."""
        ebook_wanted = []
        audio_wanted = []
        search_activities = []

        cmd = "select Status,AudioStatus,BookName from books where hc_id=?"
        for item in added_to_wanted:
            res = db.match(cmd, (item,))
            if not res:
                self.syncinglogger.warning(f'Book {item} not found in database')
                continue

            if CONFIG.get_bool('EBOOK_TAB') and CONFIG['NEWBOOK_STATUS'] not in ['Ignored']:
                if res['Status'] not in ['Wanted', 'Have', 'Open']:
                    db.action("update books set status='Wanted' where bookid=?", (item,))
                    self.syncinglogger.debug(f"Marked ebook {item} wanted")
                    ebook_wanted.append({"bookid": item})
                    stats['marked_wanted'] += 1
                else:
                    self.syncinglogger.debug(f"ebook {item} already marked {res['Status']}")

            if CONFIG.get_bool('AUDIO_TAB') and CONFIG['NEWAUDIO_STATUS'] not in ['Ignored']:
                if res['AudioStatus'] not in ['Wanted', 'Have', 'Open']:
                    db.action("update books set audiostatus='Wanted' where bookid=?", (item,))
                    self.syncinglogger.debug(f"Marked audiobook {item} wanted")
                    audio_wanted.append({"bookid": item})
                    stats['marked_wanted'] += 1
                else:
                    self.syncinglogger.debug(f"audiobook {item} already marked {res['AudioStatus']}")

        # Start search threads if needed
        if ebook_wanted:
            search_activities.append(f"• Searching for {len(ebook_wanted)} new {plural(len(ebook_wanted), 'ebook')}")
            stats['searches_started'] += len(ebook_wanted)
            threading.Thread(target=lazylibrarian.searchrss.search_rss_book, name='HCSYNCRSSBOOKS',
                             args=[ebook_wanted, 'eBook']).start()
            threading.Thread(target=lazylibrarian.searchbook.search_book, name='HCSYNCBOOKS',
                             args=[ebook_wanted, 'eBook']).start()

        if audio_wanted:
            search_activities.append(f"• Searching for {len(audio_wanted)} new "
                                     f"{plural(len(audio_wanted), 'audiobook')}")
            stats['searches_started'] += len(audio_wanted)
            threading.Thread(target=lazylibrarian.searchrss.search_rss_book, name='HCSYNCRSSAUDIO',
                             args=[audio_wanted, 'AudioBook']).start()
            threading.Thread(target=lazylibrarian.searchbook.search_book, name='HCSYNCAUDIO',
                             args=[audio_wanted, 'AudioBook']).start()

        return search_activities

    def _send_updates_to_hardcover(self, db, updates, deleted_items, sync_dict, stats, miss):
        """Send updates and deletions to HardCover."""
        sync_activities = []
        # Track status changes vs new additions separately
        status_changes = {
            'wanttoread': 0,
            'reading': 0,
            'read': 0,
            'dnf': 0
        }
        new_additions = {
            'wanttoread': 0,
            'reading': 0,
            'read': 0,
            'dnf': 0
        }
        deletion_details = []

        # Send deletions
        if deleted_items:
            self.logger.info(f"Sending {len(deleted_items)} deletions to HardCover")

            for item in deleted_items:
                book = db.match("SELECT hc_id, BookName from books WHERE bookid=?", (item,))
                if book and book['hc_id'] and item in sync_dict:
                    delcmd = self.HC_DELUSERBOOK.replace('[bookid]', str(sync_dict[item]))
                    results, _ = self.result_from_cache(delcmd, refresh=True)
                    if 'error' in results:
                        self.logger.error(str(results['error']))
                    else:
                        stats['deletions_sent'] += 1
                        if 'BookName' in book and book['BookName']:
                            book_title = book['BookName']
                        else:
                            book_title = 'Unknown Title'
                        deletion_details.append(book_title)

        # Send updates
        if updates:
            for item in updates:
                res = item[1]  # hc_id,readinglists.status,bookname
                hc_id = res['hc_id']
                book_title = res.get('BookName') or 'Unknown Title'
                status_val = res.get('Status') or 0
                is_status_change = item[2]  # True if changing existing status, False if new addition

                if is_status_change:
                    self.syncinglogger.debug(
                        f"Setting status of HardCover {res['hc_id']} to {ReadStatus(status_val).name}, (was {item[3]})")
                else:
                    self.syncinglogger.debug(
                        f"Adding new entry {res['hc_id']} {book_title} to HardCover, "
                        f"status {ReadStatus(status_val).name}")

                addcmd = self.HC_ADDUSERBOOK.replace('[bookid]', str(hc_id)).replace('[status]',
                                                                                     str(status_val))
                results, _ = self.result_from_cache(addcmd, refresh=True)
                if 'error' in results:
                    self.logger.error(str(results['error']))
                else:
                    stats['updates_sent'] += 1
                    # Track whether it's a status change or new addition
                    status_name = ReadStatus(status_val).name
                    if is_status_change:
                        status_changes[status_name] += 1
                    else:
                        new_additions[status_name] += 1

        # Build detailed sync activities with proper formatting
        if deletion_details:
            sync_activities.append(f"• Removed {len(deletion_details)} {plural(len(deletion_details), 'book')} "
                                   f"from HardCover reading lists")

        # Format status names properly
        status_display_names = {
            'wanttoread': 'Want to Read',
            'reading': 'Reading',
            'read': 'Read',
            'dnf': 'DNF'
        }

        # Add status changes first
        for status, count in status_changes.items():
            if count > 0:
                display_name = status_display_names[status]
                sync_activities.append(f"• Marked {count} {plural(count, 'book')} as '{display_name}'")

        # Add new additions
        for status, count in new_additions.items():
            if count > 0:
                display_name = status_display_names[status]
                sync_activities.append(f"• Marked {count} {plural(count, 'book')} as '{display_name}' (newly added)")

        if miss:
            sync_activities.append(f"• Unable to update {len(miss)} {plural(len(miss), 'item')} "
                                   f"at HardCover (no hc_id found)")

        # Store details for summary message
        stats['status_changes'] = status_changes
        stats['new_additions'] = new_additions
        stats['deletion_details'] = deletion_details

        return sync_activities

    @staticmethod
    def _build_sync_message(stats, reading_list_changes, search_activities, sync_activities, final_status,
                            miss, userid, readonly=False):
        """Build the final sync result message."""
        msg = f"User {userid} HardCover sync complete\n"

        # Add reading list changes
        if reading_list_changes:
            msg += "\n--- Reading List Changes ---\n"
            msg += "\n".join(reading_list_changes) + "\n"

        # Add search activities
        if search_activities:
            msg += "\n--- Search Activities ---\n"
            msg += "\n".join(search_activities) + "\n"

        # Add HardCover sync status
        if CONFIG.get_bool('HC_SYNCREADONLY') or readonly:
            msg += "\n--- HardCover Sync Status ---\n"
            if readonly and not CONFIG.get_bool('HC_SYNCREADONLY'):
                msg += "• One-way sync mode: Changes not sent to HardCover\n"
            else:
                msg += "• Two-way sync disabled, not sending changes to HardCover\n"
        elif sync_activities:
            msg += "\n--- HardCover Sync Activities ---\n"
            msg += "\n".join(sync_activities) + "\n"

        # Add warnings if any
        if miss:
            msg += "\n--- Sync Warnings ---\n"
            msg += f"• Unable to update {len(miss)} {plural(len(miss), 'item')} at HardCover (no hc_id found)\n"

        # Add final reading list status
        if final_status:
            msg += "\n--- Final Reading List Status ---\n"
            msg += "\n".join(final_status) + "\n"

        # Build summary
        summary_items = []
        if stats['hc_books_found'] > 0:
            summary_items.append(f"• HardCover books found: {stats['hc_books_found']}")
        if stats['books_matched'] > 0:
            summary_items.append(f"• Books matched to database: {stats['books_matched']}")
        if stats['new_books_added'] > 0:
            summary_items.append(f"• New books added to database: {stats['new_books_added']}")
        if stats['marked_wanted'] > 0:
            summary_items.append(f"• Books marked as wanted: {stats['marked_wanted']}")
        if stats['searches_started'] > 0:
            summary_items.append(f"• Search tasks started: {stats['searches_started']}")
        if stats['hc_overrides'] > 0:
            summary_items.append(f"• Conflicts resolved: {stats['hc_overrides']}")
        if not CONFIG.get_bool('HC_SYNCREADONLY') and not readonly:
            if stats['updates_sent'] > 0:
                summary_items.append(f"• Updates sent to HardCover: {stats['updates_sent']}")
            if stats['deletions_sent'] > 0:
                summary_items.append(f"• Books removed from HardCover: {stats['deletions_sent']}")

        if summary_items:
            msg += "\n--- Sync Summary ---\n"
            msg += "\n".join(summary_items) + "\n"
        else:
            msg += "\n--- Sync Summary ---\n• No changes detected\n"

        return msg

    def sync(self, library='', userid=None, confirmed=False, readonly=False):
        """Sync reading lists between LazyLibrarian and HardCover for a user.

        Args:
            self:
            library: 'eBook', 'AudioBook' or empty for both
            userid: User ID to sync, or None to get from cookies
            confirmed: True if user has confirmed large sync operations
            readonly: Forced readonly mode
        """
        msg = ''
        if not userid:
            if not CONFIG.get_bool('USER_ACCOUNTS'):
                msg = "User accounts are not enabled"
                self.logger.error(msg)

            cookie = cherrypy.request.cookie
            if 'll_uid' in list(cookie.keys()):
                userid = cookie['ll_uid'].value
            if not userid:
                msg = "No userid to sync"
                self.logger.error(msg)
        if msg:
            return msg

        db = database.DBConnection()
        miss = []
        ll_userid_context = userid  # Store the LazyLibrarian UserID for context in logs and operations

        # Statistics tracking for summary
        stats = {
            'hc_books_found': 0,
            'new_books_added': 0,
            'books_matched': 0,
            'deletions_sent': 0,
            'updates_sent': 0,
            'marked_wanted': 0,
            'searches_started': 0,
            'hc_overrides': 0
        }

        try:
            # Log which user we're syncing
            self.logger.info(f"HCsync starting for user: {ll_userid_context}")

            # Get the HC ID for this user
            res = db.match("SELECT hc_id from users where userid=?", (ll_userid_context,))
            if res and not res['hc_id']:
                msg = f"No hc_id for user {ll_userid_context}, first sync?"
                self.logger.warning(msg)

            db.upsert("jobs", {"Start": time.time()}, {"Name": "HCSYNC"})

            # Get all the user's reading lists
            ll_haveread = get_readinglist('haveread', ll_userid_context)
            self.syncinglogger.debug(f"ll have read contains {len(ll_haveread)} for user {ll_userid_context}")
            ll_toread = get_readinglist('toread', ll_userid_context)
            self.syncinglogger.debug(f"ll to read contains {len(ll_toread)} for user {ll_userid_context}")
            ll_reading = get_readinglist('reading', ll_userid_context)
            self.syncinglogger.debug(f"ll reading contains {len(ll_reading)} for user {ll_userid_context}")
            ll_dnf = get_readinglist('dnf', ll_userid_context)
            self.syncinglogger.debug(f"ll have dnf contains {len(ll_dnf)} for user {ll_userid_context}")
            ll_owned = []
            if library == 'eBook':
                for item in db.select("SELECT bookid from books where status in ('Open', 'Have')"):
                    ll_owned.append(item['bookid'])
            elif library == 'AudioBook':
                for item in db.select("SELECT bookid from books where audiostatus in ('Open', 'Have')"):
                    ll_owned.append(item['bookid'])
            else:
                for item in db.select("SELECT bookid from books where status in ('Open', 'Have') "
                                      "or audiostatus in ('Open', 'Have')"):
                    ll_owned.append(item['bookid'])
            self.syncinglogger.debug(f"ll owned contains {len(ll_owned)}")

            # First check if we have a stored hc_id for this user
            res = db.match("SELECT hc_id, hc_token FROM users WHERE UserID=?", (ll_userid_context,))
            if res and res['hc_id']:
                whoami = res['hc_id']
                self.syncinglogger.debug(f"Using stored hc_id: {whoami} for user {ll_userid_context}")
            else:
                # No stored hc_id, need to call hc_whoami to get it
                self.syncinglogger.debug(f"No stored hc_id for user {ll_userid_context}, calling hc_whoami")
                user_token = res['hc_token'] if res else None
                if not user_token:
                    msg = f"No HC token found for user {ll_userid_context}"
                    self.logger.error(msg)
                    return msg

                whoami_result = self.hc_whoami(userid=ll_userid_context, token=user_token)
                if str(whoami_result).isdigit():
                    whoami = int(whoami_result)
                    self.syncinglogger.debug(f"Got hc_id from whoami: {whoami} for user {ll_userid_context}")
                else:
                    self.logger.error(f"Error getting hc_id for user {ll_userid_context}: {whoami_result}")
                    return f"Error getting hc_id for user {ll_userid_context}: {whoami_result}"

            if not whoami:
                self.logger.error(f"No hc_id for user {ll_userid_context}")
                return f"No hc_id for user {ll_userid_context}"

            self.syncinglogger.debug(f"whoami = {whoami} for user {ll_userid_context}")

            # Fetch HardCover books by status
            hc_toread = []
            hc_reading = []
            hc_read = []
            hc_dnf = []
            hc_owned = []
            remapped = []
            sync_dict = {}

            hc_mapping = [
                [hc_dnf, ReadStatus.dnf.value, 'DNF'],
                [hc_reading, ReadStatus.reading.value, 'Reading'],
                [hc_read, ReadStatus.read.value, 'Read'],
                [hc_toread, ReadStatus.wanttoread.value, 'ToRead'],
                [hc_owned, ReadStatus.paused.value, 'Owned']
            ]

            # Fetch and process books from HardCover
            for mapp in hc_mapping:
                books = self._fetch_hc_books_by_status(whoami, mapp[1], mapp[2])
                stats['hc_books_found'] += len(books)

                for item in books:
                    book_id = self._process_hc_book(item, db, remapped, sync_dict, stats, readonly)
                    if book_id:
                        mapp[0].append(book_id)

            # Get last sync data
            last_sync = self._get_last_sync_lists(db, ll_userid_context)

            # Process reading list changes
            mapping = [
                [hc_toread, last_sync['toread'], ll_toread, 'toread'],
                [hc_read, last_sync['read'], ll_haveread, 'read'],
                [hc_reading, last_sync['reading'], ll_reading, 'reading'],
                [hc_dnf, last_sync['dnf'], ll_dnf, 'dnf']
            ]

            reading_list_changes = self._process_reading_list_changes(mapping)

            # Handle complete removals - books that were in last sync but not in any current HC list
            all_hc_current = set(hc_toread + hc_read + hc_reading + hc_dnf)
            all_last_sync = set(last_sync['toread'] + last_sync['read'] + last_sync['reading'] + last_sync['dnf'])
            completely_removed = all_last_sync - all_hc_current

            if completely_removed:
                removal_count = 0
                for book_id in completely_removed:
                    self.syncinglogger.debug(f"Book {book_id} completely removed from HardCover. Checking LL lists...")
                    # Remove from all LazyLibrarian lists
                    removed_from = []
                    if book_id in ll_toread:
                        ll_toread.remove(book_id)
                        removed_from.append('toread')
                    if book_id in ll_haveread:
                        ll_haveread.remove(book_id)
                        removed_from.append('read')
                    if book_id in ll_reading:
                        ll_reading.remove(book_id)
                        removed_from.append('reading')
                    if book_id in ll_dnf:
                        ll_dnf.remove(book_id)
                        removed_from.append('dnf')

                    if removed_from:
                        removal_count += 1
                        self.syncinglogger.debug(f"Book {book_id} completely removed from HardCover, "
                                                 f"removed from LL lists: {', '.join(removed_from)}")

                    # Also remove from database reading lists
                    db.action("DELETE FROM readinglists WHERE userid=? AND bookid=?", (ll_userid_context, book_id))

                if removal_count:
                    reading_list_changes.append(f"• Removed {removal_count} {plural(removal_count, 'book')} "
                                                f"completely removed from HardCover")

            # Process wanted books
            added_to_wanted = list(set(hc_toread) - set(last_sync['toread']))
            search_activities = []
            if added_to_wanted:
                search_activities = self._process_wanted_books(db, added_to_wanted, stats)

            # Prepare updates to send to HardCover
            new_set = set()
            cmd = "SELECT books.bookid from readinglists,books WHERE books.bookid=readinglists.bookid and userid=?"
            res = db.select(cmd, (ll_userid_context,))
            for item in res:
                new_set.add(item[0])

            old_set = set(hc_toread + hc_reading + hc_read + hc_dnf)
            deleted_items = old_set - new_set

            # Build update list
            updates = []
            cmd = ("SELECT hc_id,readinglists.status,bookname from readinglists,books WHERE "
                   "books.bookid=readinglists.bookid and userid=? and books.bookid=?")

            for item in new_set:
                res = db.match(cmd, (ll_userid_context, item))
                if res and res['hc_id']:
                    remote_status = ReadStatus.unknown
                    if item in hc_toread:
                        remote_status = ReadStatus.wanttoread
                    elif item in hc_reading:
                        remote_status = ReadStatus.reading
                    elif item in hc_read:
                        remote_status = ReadStatus.read
                    elif item in hc_dnf:
                        remote_status = ReadStatus.dnf

                    # Check what the status was at last sync
                    last_sync_status = ReadStatus.unknown
                    if item in last_sync['toread']:
                        last_sync_status = ReadStatus.wanttoread
                    elif item in last_sync['reading']:
                        last_sync_status = ReadStatus.reading
                    elif item in last_sync['read']:
                        last_sync_status = ReadStatus.read
                    elif item in last_sync['dnf']:
                        last_sync_status = ReadStatus.dnf

                    # Handle sync conflicts: check what changed since last sync
                    if res['Status'] != remote_status.value:
                        ll_changed = res['Status'] != last_sync_status.value
                        hc_changed = remote_status.value != last_sync_status.value

                        if ll_changed and not hc_changed:
                            # Only LazyLibrarian changed, send change to HardCover
                            sync_id = sync_dict.get(item, res['hc_id']) if remote_status.value else res['hc_id']
                            updates.append([sync_id, dict(res), remote_status.value, remote_status.name])
                        else:
                            # Either only HardCover changed, or both changed - accept HardCover as master
                            if ll_changed and hc_changed:
                                # Only log as a conflict when both sides changed
                                self.syncinglogger.debug(
                                    f"Book {item} status conflict (both changed): LL={ReadStatus(res['Status']).name}, "
                                    f"HC={remote_status.name}, Last={last_sync_status.name}, accepting HC as master")
                                # Track this override
                                stats['hc_overrides'] += 1
                            else:
                                # Just a normal HC update
                                self.syncinglogger.debug(
                                    f"Book {item} updated in HC: HC={remote_status.name}, "
                                    f"Last={last_sync_status.name}, updating LL")

                            # Update LazyLibrarian database to match HardCover
                            db.action("UPDATE readinglists SET status=? WHERE userid=? AND bookid=?",
                                      (remote_status.value, ll_userid_context, item))

                            # Update the appropriate LazyLibrarian list
                            # First remove from all lists
                            for lst in [ll_toread, ll_reading, ll_haveread, ll_dnf]:
                                if item in lst:
                                    lst.remove(item)

                            # Then add to the correct list based on HardCover status
                            if remote_status == ReadStatus.wanttoread:
                                ll_toread.append(item)
                            elif remote_status == ReadStatus.reading:
                                ll_reading.append(item)
                            elif remote_status == ReadStatus.read:
                                ll_haveread.append(item)
                            elif remote_status == ReadStatus.dnf:
                                ll_dnf.append(item)

                else:
                    if res:
                        resdict = dict(res)
                        book_title = resdict.get('BookName') or ''
                    else:
                        book_title = ''
                    miss.append((item, book_title))
                    for mapp in mapping:
                        if item in mapp[2]:
                            mapp[2].remove(item)

            if stats['hc_overrides']:
                override_count = stats['hc_overrides']
                hc_override_activity = (f"• Accepted HardCover due to conflict: {override_count} "
                                        f"{plural(override_count, 'book')}")
                self.syncinglogger.debug(hc_override_activity)
                reading_list_changes.append(hc_override_activity)

            # Handle sync limits and confirmations
            sync_limit = CONFIG.get_int('HC_SYNC_LIMIT')
            sync_activities = []

            if CONFIG.get_bool('HC_SYNCREADONLY') or readonly:
                if not msg:
                    msg = ""
                msg += "\n--- HardCover Sync Status ---\n"
                if readonly and not CONFIG.get_bool('HC_SYNCREADONLY'):
                    msg += "• One-way sync mode: Changes not sent to HardCover\n"
                else:
                    msg += "• Two-way sync disabled, not sending changes to HardCover\n"
                msg += (f"• Would have sent {len(deleted_items)} {plural(len(deleted_items), 'deletion')} "
                        f"and processed {len(updates)} {plural(len(updates), 'update')}\n")
                self.logger.info("Two-way sync disabled, not sending changes to HardCover")
            else:
                # Check sync safety and determine action
                safety_check = self._check_sync_safety(len(deleted_items), len(updates), sync_limit, confirmed)

                if safety_check == 'block':
                    # Auto sync blocked due to safety limits
                    if not msg:
                        msg = ""
                    msg += "\n--- AUTO SYNC BLOCKED: \n"
                    msg += (f"• Auto sync blocked: {len(updates)} {plural(len(updates), 'update')} "
                            f"and {len(deleted_items)} {plural(len(deleted_items), 'deletion')} "
                            f"exceed safety limit of {sync_limit}\n")
                    msg += "• Please perform manual sync from LazyLibrarian manage page to proceed\n"
                    self.logger.warning(f"Auto sync blocked due to safety limits: {len(updates)} updates, "
                                        f"{len(deleted_items)} deletions")
                elif safety_check == 'confirm':
                    # Manual sync requires confirmation
                    if not msg:
                        msg = ""
                    msg += "\n--- CONFIRMATION REQUIRED: \n"
                    msg += (f"• This sync would make {len(updates)} {plural(len(updates), 'update')} "
                            f"and {len(deleted_items)} {plural(len(deleted_items), 'deletion')} to HardCover\n")
                    msg += f"• This exceeds the safety limit of {sync_limit} items\n"
                    msg += "• Please confirm you want to proceed with these changes\n"
                    msg += "• Or choose 'Ignore Updates' to treat this as a one-way sync\n"
                    return msg
                else:
                    # safety_check == 'proceed' - Send updates to HardCover
                    sync_activities = self._send_updates_to_hardcover(db, updates, deleted_items,
                                                                      sync_dict, stats, miss)

            # Update final reading lists and sync records
            final_status = []
            for mapp in mapping:
                list_name = mapp[3].capitalize() if mapp[3] != 'dnf' else 'DNF'
                final_status.append(f"• {list_name}: {len(mapp[2])} {plural(len(mapp[2]), 'book')}")
                self.syncinglogger.debug(f"HardCover {mapp[3]} contains {len(mapp[2])}")
                set_readinglist(mapp[3], ll_userid_context, mapp[2])
                label = f"hc_{mapp[3]}"
                booklist = ','.join(mapp[2])
                db.upsert("sync", {'SyncList': booklist}, {'UserID': ll_userid_context, 'Label': label})

            # Update sync records
            db.action("DELETE from sync WHERE UserID=? AND Label LIKE 'hc_%'", (ll_userid_context,))
            db.action("INSERT INTO sync VALUES (?, ?, ?, ?)", (ll_userid_context, 'hc_toread',
                                                               now(), ','.join(ll_toread)))
            db.action("INSERT INTO sync VALUES (?, ?, ?, ?)", (ll_userid_context, 'hc_reading',
                                                               now(), ','.join(ll_reading)))
            db.action("INSERT INTO sync VALUES (?, ?, ?, ?)", (ll_userid_context, 'hc_read',
                                                               now(), ','.join(ll_haveread)))
            db.action("INSERT INTO sync VALUES (?, ?, ?, ?)", (ll_userid_context, 'hc_dnf',
                                                               now(), ','.join(ll_dnf)))

            # Build final message
            msg = self._build_sync_message(stats, reading_list_changes, search_activities,
                                           sync_activities, final_status, miss, ll_userid_context, readonly)
            return msg

        except Exception as e:
            error_msg = f"Error during HardCover sync for user {ll_userid_context}: {str(e)}"
            self.logger.error(error_msg)
            import traceback
            self.logger.error(traceback.format_exc())
            return f"User {ll_userid_context} HardCover sync failed: {str(e)}"

        finally:
            db.upsert("jobs", {"Finish": time.time()}, {"Name": "HCSYNC"})
            db.close()
            self.logger.info(f"HCsync completed for {ll_userid_context}")
            for missed in miss:
                self.syncinglogger.warning(f"Unable to add bookid {missed[0]} ({missed[1]}) at HardCover, no hc_id")
            thread_name('WEBSERVER')

    def _check_sync_safety(self, deleted_count, update_count, sync_limit, confirmed):
        """Check if sync can proceed and return appropriate action.

        Returns:
            'proceed': Sync can proceed normally
            'block': Auto sync blocked due to safety limits
            'confirm': Manual sync requires confirmation
        """
        # If sync_limit is 0, there's no limit - proceed normally
        if sync_limit == 0:
            return 'proceed'

        if thread_name() == 'HCSYNC' and (deleted_count > sync_limit or update_count > sync_limit):
            # Auto sync with too many changes - block it
            warnmsg = (f"Too many changes or deletions to autosync to HardCover "
                       f"(update:{update_count}, delete:{deleted_count}) "
                       f'Please sync manually from lazylibrarian "manage" page')
            self.logger.warning(warnmsg)
            return 'block'  # Block auto-sync

        if (thread_name() == 'WEB-HCSYNC' and (deleted_count > sync_limit or update_count > sync_limit)
                and not confirmed):
            # Manual sync with too many changes - require confirmation
            return 'confirm'  # Require confirmation

        return 'proceed'  # Normal sync can proceed
