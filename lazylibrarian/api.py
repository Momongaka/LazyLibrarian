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


import configparser
import contextlib
import json
import logging
import os
import shutil
import sys
import threading
from queue import Queue
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import cherrypy
import dateutil.parser as dp
from cherrypy.lib.static import serve_file

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.bookrename import audio_rename, book_rename, name_vars
from lazylibrarian.bookwork import (
    add_series_members,
    delete_empty_series,
    genre_filter,
    get_book_authors,
    get_book_pubdate,
    get_gb_info,
    get_series_authors,
    get_series_members,
    get_work_series,
    isbn_from_words,
    set_all_book_authors,
    set_all_book_series,
    set_genres,
    set_work_id,
)
from lazylibrarian.cache import ImageType, cache_img, clean_cache
from lazylibrarian.calibre import calibre_list, delete_from_calibre, sync_calibre_list
from lazylibrarian.calibre_integration import (
    send_comic_issue_to_calibre,
    send_ebook_to_calibre,
    send_mag_issue_to_calibre,
)
from lazylibrarian.comicid import comic_metadata, cv_identify, cx_identify
from lazylibrarian.comicscan import comic_scan
from lazylibrarian.comicsearch import search_comics
from lazylibrarian.common import (
    create_support_zip,
    dbbackup,
    get_readinglist,
    log_header,
    mime_type,
    zip_audio,
)
from lazylibrarian.config2 import CONFIG, wishlist_type
from lazylibrarian.configtypes import ConfigBool, ConfigInt
from lazylibrarian.csvfile import dump_table, export_csv, import_csv
from lazylibrarian.download_client import get_download_progress
from lazylibrarian.filesystem import DIRS, path_isfile, setperm, splitext, syspath
from lazylibrarian.formatter import (
    check_int,
    format_author_name,
    get_list,
    is_valid_isbn,
    plural,
    split_author_names,
    thread_name,
    today,
)
from lazylibrarian.grsync import grfollow, grsync
from lazylibrarian.hc import hc_sync
from lazylibrarian.images import (
    create_mag_cover,
    create_mag_covers,
    get_author_image,
    get_author_images,
    get_book_cover,
    get_book_covers,
    read_pdf_tags,
    shrink_mag,
    write_pdf_tags,
)
from lazylibrarian.importer import (
    add_author_name_to_db,
    add_author_to_db,
    de_duplicate,
    get_all_author_details,
    search_for,
    update_totals,
)
from lazylibrarian.librarysync import library_scan
from lazylibrarian.logconfig import LOGCONFIG
from lazylibrarian.magazinescan import (
    clean_maglibrary,
    format_issue_filename,
    get_dateparts,
    magazine_scan,
    rename_issue,
)
from lazylibrarian.manual_import import (
    process_alternate,
    process_book_from_dir,
    process_mag_from_file,
)
from lazylibrarian.manualbook import search_item
from lazylibrarian.metadata_opf import create_opf
from lazylibrarian.multiauth import (
    get_authors_from_book_files,
    get_authors_from_hc,
    get_authors_from_ol,
)
from lazylibrarian.postprocess import (
    process_dir,
    process_img,
)
from lazylibrarian.preprocessor import preprocess_audio, preprocess_ebook, preprocess_magazine
from lazylibrarian.processcontrol import get_cpu_use, get_process_memory, get_threads
from lazylibrarian.providers import get_capabilities
from lazylibrarian.rssfeed import gen_feed
from lazylibrarian.scheduling import (
    SchedulerCommand,
    all_author_update,
    author_update,
    check_running_jobs,
    restart_jobs,
    series_update,
    show_jobs,
    show_stats,
)
from lazylibrarian.searchbook import search_book
from lazylibrarian.searchmag import search_magazines
from lazylibrarian.searchrss import search_rss_book, search_wishlist
from lazylibrarian.telemetry import TELEMETRY, telemetry_send

# dict of known commands. 0 = any valid api key, 1 = not available with the read-only key
cmd_dict = {'help': (0, 'list available commands. Time consuming commands take an optional &wait parameter '
                        'if you want to wait for completion, otherwise they return OK straight away '
                        'and run in the background'),
            'showMonths': (0, 'List installed monthnames'),
            'dumpMonths': (1, 'Save installed monthnames to file'),
            'deduplicate': (1, '&id= [&wait] De-duplicate authors books'),
            'saveTable': (0, '&table= Save a database table to a file'),
            'getIndex': (0, 'list all authors'),
            'getAuthor': (0, '&id= get author by AuthorID and list their books'),
            'getAuthorInfo': (0, '&id= [&name=] get author info from configured sources'),
            'getAuthorImage': (0, '&id= [&refresh] [&max] get one or more images for this author'),
            'setAuthorImage': (1, '&id= &img= set a new image for this author'),
            'setAuthorLock': (1, '&id= lock author name/image/dates'),
            'setAuthorUnlock': (1, '&id= unlock author name/image/dates'),
            'setBookLock': (1, '&id= lock book details'),
            'setBookUnlock': (1, '&id= unlock book details'),
            'setBookImage': (1, '&id= &img= set a new image for this book'),
            'shrinkMag': (1, '&name= &size= shrink magazine size'),
            'getAuthorImages': (1, '[&wait] get images for all authors without one'),
            'getWanted': (0, 'list wanted books'),
            'getRead': (1, '&id= list read books for current user'),
            'getReading': (1, '&id= list currently-reading books for user'),
            'getToRead': (1, '&id= list to-read books for user'),
            'getAbandoned': (1, '&id= list abandoned books for user'),
            'getSnatched': (0, 'list snatched books'),
            'getHistory': (0, 'list history'),
            'getDebug': (0, 'show debug log header'),
            'getModules': (0, 'show installed modules'),
            'checkModules': (0, 'Check using lazylibrarian library modules'),
            'createSupportZip': (0, 'Create support.zip. Requires that LOGFILEREDACT is enabled'),
            'clearLogs': (1, 'clear current log'),
            'getMagazines': (0, 'list magazines'),
            'getIssues': (0, '[&name=] [&sort=] [&limit=] list issues of named magazine'),
            'getIssueName': (0, '&name= [&datestyle=] get name of issue from path/filename'),
            'createMagCovers': (1, '[&wait] [&refresh] create covers for magazines, optionally refresh existing ones'),
            'createMagCover': (1, '&file= [&refresh] [&page=] create cover for magazine issue, optional page number'),
            'forceMagSearch': (1, '[&title=] [&backissues] [&wait] search for wanted magazines'),
            'forceBookSearch': (1, '[&wait] [&type=eBook/AudioBook] search for all wanted books'),
            'forceRSSSearch': (1, '[&wait] search all entries in rss feeds'),
            'forceComicSearch': (1, '[&wait] search for all wanted comics'),
            'getRSSFeed': (0, '&feed= [&limit=] show rss feed entries'),
            'forceWishlistSearch': (1, '[&wait] search all entries in wishlists'),
            'forceProcess': (1, '[&dir] [ignorekeepseeding] process books/mags in download or named dir'),
            'pauseAuthor': (1, '&id= pause author by AuthorID'),
            'resumeAuthor': (1, '&id= resume author by AuthorID'),
            'ignoreAuthor': (1, '&id= ignore author by AuthorID'),
            'refreshAuthor': (1, '&name= [&refresh] reload author (and their books) by name, optionally refresh cache'),
            'authorUpdate': (1, 'update the oldest author'),
            'seriesUpdate': (1, 'update the oldest series'),
            'forceActiveAuthorsUpdate': (1, '[&wait] [&refresh] reload all active authors and book data, '
                                            'refresh cache'),
            'forceLibraryScan': (1, '[&wait] [&remove] [&dir=] [&id=] rescan whole or part book library'),
            'forceComicScan': (1, '[&wait] [&id=] rescan whole or part comic library'),
            'forceAudioBookScan': (1, '[&wait] [&remove] [&dir=] [&id=] rescan whole or part audiobook library'),
            'forceMagazineScan': (1, '[&wait] [&title=] rescan whole or part magazine library'),
            'getVersion': (0, 'show lazylibrarian current/git version'),
            'getCurrentVersion': (0, 'show lazylibrarian current version'),
            'shutdown': (1, 'stop lazylibrarian'),
            'restart': (1, 'restart lazylibrarian'),
            'update': (1, 'update lazylibrarian'),
            'findAuthor': (0, '&name= search goodreads/googlebooks for named author'),
            'findAuthorID': (0, '&name= [&source=] find AuthorID for named author'),
            'findMissingAuthorID': (0, '[&source=] find authorid from named source for any authors without id'),
            'findBook': (0, '&name= search goodreads/googlebooks for named book'),
            'addBook': (1, '&id= [&wait] add one or more books to the database by bookid'),
            'addBookByISBN': (1, '&isbn= [&wait] add one or more books to the database by isbn'),
            'moveBooks': (1, '&fromname= &toname= move all books from one author to another by AuthorName'),
            'moveBook': (1, '&id= &toid= move one book to new author by BookID and AuthorID'),
            'addAuthor': (1, '&name= [&books] add author to database by name, optionally add their books'),
            'addAuthorID': (1, '&id= add author to database by AuthorID, optionally add their books'),
            'removeAuthor': (1, '&id= remove author from database by AuthorID'),
            'addMagazine': (1, '&name= add magazine to database by name'),
            'removeMagazine': (1, '&name= remove magazine and all of its issues from database by name'),
            'queueBook': (1, '&id= [&type=eBook/AudioBook] mark book as Wanted, default eBook'),
            'unqueueBook': (1, '&id= [&type=eBook/AudioBook] mark book as Skipped, default eBook'),
            'readCFG': (1, '&name=&group= read value of config variable "name" in section "group"'),
            'writeCFG': (1, '&name=&group=&value= set config variable "name" in section "group" to value'),
            'loadCFG': (1, 'reload config from file'),
            'getBookCover': (0, '&id= [&src=] fetch cover link from cache/cover/librarything/goodreads/google '
                                'for BookID'),
            'getFileDirect': (0, '&id= [&type=eBook/AudioBook/Comic/Issue] download file directly'),
            'getAllBooks': (0, '[&sort=] [&limit=] [&status=] [&audiostatus=] list all books in the database'),
            'listNoLang': (0, 'list all books in the database with unknown language'),
            'listNoDesc': (0, 'list all books in the database with no description'),
            'listNoISBN': (0, 'list all books in the database with no isbn'),
            'listNoGenre': (0, 'list all books in the database with no genre'),
            'listNoBooks': (0, 'list all authors in the database with no books'),
            'listMissingBookFile': (0, 'list all books in the database with missing bookfile'),
            'listDupeBooks': (0, 'list all books in the database with more than one entry'),
            'listDupeBookStatus': (0, 'list all copies of books in the database with more than one entry'),
            'removeNoBooks': (1, 'delete all authors in the database with no books'),
            'listIgnoredAuthors': (0, 'list all authors in the database marked ignored'),
            'listIgnoredBooks': (0, 'list all books in the database marked ignored'),
            'listIgnoredSeries': (0, 'list all series in the database marked ignored'),
            'searchBook': (1, '&id= [&wait] [&type=eBook/AudioBook] search for one book by BookID'),
            'searchItem': (1, '&item= get search results for an item (author, title, isbn)'),
            'showStats': (0, '[&json] show database statistics'),
            'showJobs': (0, '[&json] show status of running jobs'),
            'restartJobs': (1, 'restart background jobs'),
            'showThreads': (0, 'show threaded processes'),
            'checkRunningJobs': (0, 'ensure all needed jobs are running'),
            'vacuum': (1, 'vacuum the database'),
            'getWorkSeries': (0, '&id= &source= Get series from Librarything using BookID or GoodReads using WorkID'),
            'addSeriesMembers': (1, '&id= [&refresh] add series members to database using SeriesID'
                                    'including paused/ignored series if refresh'),
            'getSeriesMembers': (0, '&id= [&name=] [&refresh] Get list of series members using SeriesID, '
                                    'including paused/ignored series if refresh'),
            'getSeriesAuthors': (1, '&id= Get all authors for a series and import them'),
            'getBookCovers': (1, '[&wait] Check all books for cached cover and download one if missing'),
            'getBookAuthors': (0, '&id= Get list of authors associated with this book'),
            'cleanCache': (1, '[&wait] Clean unused and expired files from the LazyLibrarian caches'),
            'deleteEmptySeries': (1, 'Delete any book series that have no members'),
            'setNoDesc': (1, '[&refresh] Set descriptions for all books, include "No Description" entries on refresh'),
            'setNoGenre': (1, '[&refresh] Set book genre for all books without one, include "Unknown" '
                              'entries on refresh'),
            'setAllBookSeries': (1, '[&wait] Set the series details from goodreads'),
            'setAllBookAuthors': (1, '[&wait] Set all authors for all books'),
            'setWorkID': (1, '[&wait] [&bookids] Set WorkID for all books that dont have one, or bookids'),
            'importAlternate': (1, '[&wait] [&dir=] [&library=] Import ebooks/audiobooks from named or '
                                   'alternate folder and any subfolders'),
            'includeAlternate': (1, '[&wait] [&dir=] [&library=] Include links to ebooks/audiobooks from named '
                                    'or  alternate folder and any subfolders'),
            'importCSVwishlist': (1, '[&wait] [&status=Wanted] [&library=eBook] [&dir=] Import a CSV wishlist '
                                     'from named or alternate directory'),
            'exportCSVwishlist': (1, '[&wait] [&status=Wanted] [&library=eBook] [&dir=] Export a CSV wishlist '
                                     'to named or alternate directory'),
            'grSync': (1, '&status= &shelf= [&library=] [&reset] Sync books with given status to a goodreads '
                          'shelf, or reset goodreads shelf to match lazylibrarian'),
            'grFollow': (1, '&id= Follow an author on goodreads'),
            'grFollowAll': (1, 'Follow all lazylibrarian authors on goodreads'),
            'grUnfollow': (1, '&id= Unfollow an author on goodreads'),
            'hcSync': (1, '[&library=] Sync readinglists to hardcover'),
            'writeOPF': (1, '&id= [&refresh] write out an opf file for a bookid, optionally overwrite existing opf'),
            'writeAllOPF': (1, '[&refresh] write out opf files for all books, optionally overwrite existing opf'),
            'renameAudio': (1, '&id Rename an audiobook using configured pattern'),
            'createPlaylist': (1, '&id Create playlist for an audiobook'),
            'nameVars': (0, '&id Show the name variables that would be used for a bookid'),
            'showCaps': (0, '&provider= get a list of capabilities from a provider'),
            'calibreList': (0, '[&toread=] [&read=] get a list of books in calibre library'),
            'syncCalibreList': (0, '[&toread=] [&read=] sync list of read/toread books with calibre'),
            'logMessage': (1, '&level= &text=  send a message to lazylibrarian logger'),
            'comicid': (0, '&name= &source= [&best] try to identify comic from name'),
            'comicmeta': (0, '&name= [&xml] get metadata from comic archive, xml or dictionary'),
            'getBookPubdate': (0, '&id= get original publication date of a book by bookid'),
            'gc_init': (1, 'Initialise gc_before state'),
            'gc_stats': (1, 'Show difference since gc_init'),
            'gc_collect': (1, 'Run garbage collection & return how many items'),
            'listNewAuthors': (0, '[&limit=] List newest authors and show when added and reason for adding'),
            'listNewBooks': (0, '[&limit=] List newest books and show when added and reason for adding'),
            'importBook': (1, '[&library=] &id= &dir= add library [eBook|Audio] bookid from folder'),
            'importMag': (1, '&title= &num= &file= add magazine issue from file'),
            'preprocessAudio': (1, '&dir= &author= &title= [&id=] [&tag] [&merge] preprocess an audiobook folder'),
            'preprocessBook': (1, '&dir= preprocess an ebook folder'),
            'preprocessAllBooks': (1, 'run preprocessor on all existing ebooks'),
            'preprocessMagazine': (1, '&dir= &cover= preprocess a magazine folder'),
            'memUse': (0, 'memory usage of the program in kB'),
            'cpuUse': (0, 'recent cpu usage of the program'),
            'nice': (0, 'show current nice level'),
            'nicer': (1, 'make a little nicer'),
            'subscribe': (1, '&user= &feed= subscribe a user to a feed'),
            'unsubscribe': (1, '&user= &feed= remove a user from a feed'),
            'listAlienAuthors': (0, 'List authors not matching current book api'),
            'listAlienBooks': (0, 'List books not matching current book api'),
            'listNabProviders': (0, 'List all newznab/torznab providers, prowlarr compatible format'),
            'listRSSProviders': (0, 'List all rss/wishlist providers'),
            'listTorrentProviders': (0, 'List all torrent providers'),
            'listIRCProviders': (0, 'List all irc providers'),
            'listDirectProviders': (0, 'List all direct providers'),
            'listProviders': (0, 'List all providers'),
            'changeProvider': (1, '&name= &xxx= Change values for a provider'),
            'addProvider': (1, '&type= &xxx= Add a new provider'),
            'delProvider': (1, '&name= Delete a provider'),
            'renameBook': (1, '&id= Rename a book to match configured pattern'),
            'newAuthorid': (1, '&id= &newid= update an authorid'),
            'telemetryShow': (0, 'show the current telemetry data'),
            'telemetrySend': (1, 'send the latest telemetry data, if configured'),
            'backup': (1, 'Backup database and config files'),
            'sendEbooktoCalibre': (1, "&id= Send a book that's in LazyLibrarian database to calibre"),
            'sendMagtoCalibre': (1, "&title= Send all issues of a magazine in LazyLibrarian database to calibre"),
            'sendMagIssuetoCalibre': (1, "&id= Send a magazine issue already in LazyLibrarian database to calibre"),
            'sendComicIssuetoCalibre': (1, "&id= Send a comic issue already in LazyLibrarian database to calibre"),
            'ignoredStats': (0, 'show count of reasons books were ignored'),
            'updateBook': (1, "&id= [&BookName=] update book parameters (bookname, booklang, bookdate etc)"),
            'renameissue': (1, '&id= Rename a magazine issue to match configured pattern'),
            'renameissues': (1, '&title= Rename all issues of a magazine to match configured pattern'),
            'splitauthornames': (0, '&names= Show how a list of author names would be split into individual authors'),
            'getauthorsfrombookfiles': (1, 'Scan your book folder and get all secondary authors from epub,mobi,opf'),
            'getauthorsfromhc': (1, 'Scan your database and get all secondary authors from HardCover'),
            'getauthorsfromol': (1, 'Scan your database and get all secondary authors from OpenLibrary'),
            'getpdftags': (0, '&id= Show embedded tags in a pdf issue file'),
            'setpdftags': (1, '&id= &tags= Set embedded tags in a pdf issue file'),
            'listsecondaries': (0, 'list all authors that are not primary author of any book in the database'),
            'deletesecondaries': (1, 'delete all secondary authors in the database'),
            'isbnwords': (0, 'find an isbn for a title'),
            'getDownloadProgress': (0, '[&source=] [&downloadid=] [&limit=] show active download progress'),
            'cleanMagLibrary': (0, 'Clean the library, removing entries for missing issues')
            }


def get_case_insensitive_key_value(input_dict, key):
    return next((value for dict_key, value in input_dict.items() if dict_key.lower() == key.lower()), None)


class Api:
    def __init__(self):

        self.apikey = None
        self.cmd = None
        self.id = None
        self.kwargs = None
        self.data = None
        self.callback = None
        self.file_response = None
        self.lower_cmds = [key.lower() for key, _ in cmd_dict.items()]
        self.logger = logging.getLogger(__name__)
        self.dlcommslogger = logging.getLogger('special.dlcomms')

    def check_params(self, **kwargs):
        TELEMETRY.record_usage_data()

        if not CONFIG.get_bool('API_ENABLED'):
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 501, 'Message': 'API not enabled'}}
            return
        if not CONFIG.get_str('API_KEY') and not CONFIG.get_str('API_RO_KEY'):
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 501, 'Message': 'No API key'}}
            return
        if CONFIG.get_str('API_KEY') and len(CONFIG.get_str('API_KEY')) != 32:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 503, 'Message': 'Invalid API key'}}
            return
        if CONFIG.get_str('API_RO_KEY') and len(CONFIG.get_str('API_RO_KEY')) != 32:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 503, 'Message': 'Invalid Read-Only API key'}}
            return

        if 'apikey' not in kwargs:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: apikey'}}
            return

        if kwargs['apikey'] != CONFIG.get_str('API_KEY') and kwargs['apikey'] != CONFIG.get_str('API_RO_KEY'):
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 401, 'Message': 'Incorrect API key'}}
            return
        self.apikey = kwargs.pop('apikey')

        if 'cmd' not in kwargs:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 405,
                                                                 'Message': 'Missing parameter: cmd, try cmd=help'}}
            return

        if kwargs['cmd'].lower() not in self.lower_cmds:
            self.data = {'Success': False, 'Data': '',
                         'Error': {'Code': 405, 'Message': f"Unknown command: {kwargs['cmd']}, try cmd=help"}}
            return

        if get_case_insensitive_key_value(cmd_dict, kwargs['cmd'])[0] != 0 and self.apikey != CONFIG.get_str('API_KEY'):
            self.data = {'Success': False, 'Data': '',
                         'Error': {'Code': 405,
                                   'Message': f"Command: {kwargs['cmd']} "
                                              f"not available with read-only api access key, try cmd=help"}}
            return

        self.cmd = kwargs.pop('cmd')
        self.kwargs = kwargs
        self.data = 'OK'

    # noinspection PyUnreachableCode
    @property
    def fetch_data(self):
        TELEMETRY.record_usage_data()
        thread_name("API")
        if self.data == 'OK':
            remote_ip = cherrypy.request.headers.get('X-Forwarded-For')  # apache2
            if not remote_ip:
                remote_ip = cherrypy.request.headers.get('X-Host')  # lighthttpd
            if not remote_ip:
                remote_ip = cherrypy.request.headers.get('Remote-Addr')
            if not remote_ip:
                remote_ip = cherrypy.request.remote.ip
            self.logger.debug(f'Received API command from {remote_ip}: {self.cmd} {self.kwargs}')
            method_to_call = getattr(self, f"_{self.cmd.lower()}")
            method_to_call(**self.kwargs)

            if self.file_response:
                file_path, file_name = self.file_response
                return serve_file(file_path, mime_type(file_path), "attachment", name=file_name)

            if 'callback' not in self.kwargs:
                self.dlcommslogger.debug(str(self.data))
                if isinstance(self.data, str):
                    return self.data
                return json.dumps(self.data)
            self.callback = self.kwargs['callback']
            self.data = json.dumps(self.data)
            self.data = f"{self.callback}({self.data});"
            return self.data
        if isinstance(self.data, str):
            return self.data
        return json.dumps(self.data)

    @staticmethod
    def _dic_from_query(query):

        db = database.DBConnection()
        try:
            rows = db.select(query)
        finally:
            db.close()

        rows_as_dic = []

        for row in rows:
            # noinspection PyTypeChecker
            row_as_dic = dict(list(zip(list(row.keys()), row, strict=True)))
            for key in ['BookLibrary', 'AudioLibrary', 'BookAdded']:
                if row_as_dic.get(key):
                    with contextlib.suppress(dp.ParserError):
                        row_as_dic[key] = f"{dp.parse(row_as_dic[key]).isoformat()}Z"
            rows_as_dic.append(row_as_dic)

        return rows_as_dic

    def _backup(self):
        TELEMETRY.record_usage_data()
        backup_file, err = dbbackup('api')
        success = backup_file != ''
        self.data = {'Success': success != '', 'Data': backup_file, 'Error': {'Code': 200, 'Message': err}}

    def _renamebook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: id'}}
        else:
            fname, err = book_rename(kwargs['id'])
            self.data = {'Success': fname != '', 'Data': fname, 'Error': {'Code': 200, 'Message': err}}
        return

    def _renameissue(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: id'}}
        else:
            fname, err = rename_issue(kwargs['id'])
            self.data = {'Success': fname != '', 'Data': fname, 'Error': {'Code': 200, 'Message': err}}
        return

    def _renameissues(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'title' not in kwargs:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: title'}}
            return
        db = database.DBConnection()
        cmd = f"SELECT issueid,issuefile from issues WHERE title=\'{kwargs['title']}\'"
        issues = db.select(cmd)
        db.close()

        if not issues:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 200,
                                                                 'Message': f"No Issues for {kwargs['title']}"}}
        else:
            hit = 0
            miss = 0
            for item in issues:
                if not path_isfile(item['issuefile']):
                    self.logger.debug(f"Missing file {item['issuefile']}")
                    miss += 1
                else:
                    fname, err = rename_issue(item['issueid'])
                    if err:
                        self.logger.debug(f"Failed to rename {item['issuefile']} {err}")
                        miss += 1
                    else:
                        hit += 1
            self.data = {'Success': miss == 0, 'Data': f"Renamed {hit}: Failed {miss}",
                         'Error': {'Code': 200, 'Message': ''}}
        return

    def _getdownloadprogress(self, **kwargs):
        TELEMETRY.record_usage_data()
        source = kwargs.get('source')
        downloadid = kwargs.get('downloadid')
        limit = check_int(kwargs.get('limit'), 0)

        if (source and not downloadid) or (downloadid and not source):
            self.data = {'Success': False, 'Data': '',
                         'Error': {'Code': 400, 'Message': 'Both source and downloadid are required'}}
            return

        if source and downloadid:
            progress, finished = get_download_progress(source, downloadid)
            self.data = {'Success': True,
                         'Data': {'source': source, 'downloadid': downloadid,
                                  'progress': progress, 'finished': finished},
                         'Error': {'Code': 200, 'Message': 'OK'}}
            return

        db = database.DBConnection()
        try:
            cmd = ("SELECT NZBTitle,BookID,AuxInfo,NZBProv,NZBmode,Status,Source,DownloadID,Completed,DLResult,rowid "
                   "FROM wanted WHERE Status IN ('Snatched','Seeding') ORDER BY rowid DESC")
            rows = db.select(cmd)
        finally:
            db.close()

        items = []
        for row in rows[:limit] if limit else rows:
            row = dict(row)
            dl_source = row.get('Source')
            dl_id = row.get('DownloadID')
            if dl_source and dl_id:
                progress, finished = get_download_progress(dl_source, dl_id)
            else:
                progress, finished = -1, False
            row['progress'] = progress
            row['finished'] = finished
            items.append(row)

        self.data = {'Success': True, 'Data': items, 'Error': {'Code': 200, 'Message': 'OK'}}
        return

    def _newauthorid(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: id'}}
        elif 'newid' not in kwargs:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: newid'}}
        elif (kwargs['id'].startswith('OL') and not kwargs['newid'].startswith('OL')
              or not kwargs['id'].startswith('OL') and kwargs['newid'].startswith('OL')):
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Invalid parameter: newid'}}
        else:
            db = database.DBConnection()
            try:
                res = db.match('SELECT * from authors WHERE authorid=?', (kwargs['id'],))
                if not res:
                    self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                         'Message': 'Invalid parameter: id'}}
                else:
                    db.action("PRAGMA foreign_keys = OFF")
                    db.action('UPDATE books SET AuthorID=? WHERE AuthorID=?',
                              (kwargs['newid'], kwargs['id']))
                    db.action('UPDATE seriesauthors SET AuthorID=? WHERE AuthorID=?',
                              (kwargs['newid'], kwargs['id']), suppress='UNIQUE')
                    if kwargs['newid'].startswith('OL'):
                        db.action('UPDATE authors SET AuthorID=?,ol_id=? WHERE AuthorID=?',
                                  (kwargs['newid'], kwargs['newid'], kwargs['id']), suppress='UNIQUE')
                    else:
                        db.action('UPDATE authors SET AuthorID=?,gr_id=? WHERE AuthorID=?',
                                  (kwargs['newid'], kwargs['newid'], kwargs['id']), suppress='UNIQUE')

                    db.action("PRAGMA foreign_keys = ON")
                    self.data = {'Success': True,
                                 'Data': {'AuthorID': kwargs['newid']},
                                 'Error': {'Code': 200, 'Message': 'OK'}
                                 }
            finally:
                db.close()
        return

    @staticmethod
    def _provider_array(prov_type):
        # convert provider config values to a regular array of dicts with correct types
        array = CONFIG.providers(prov_type)
        providers = []
        for provider in array:
            thisprov = {}
            for key in provider:
                if isinstance(key, ConfigBool):
                    thisprov[key] = provider.get_bool(key)
                elif isinstance(key, ConfigInt):
                    thisprov[key] = provider.get_int(key)
                else:
                    thisprov[key] = provider.get_str(key)
            providers.append(thisprov)
        return providers

    def _listproviders(self):
        TELEMETRY.record_usage_data()
        self._listdirectproviders()
        direct = self.data
        self._listtorrentproviders()
        torrent = self.data
        self.data = {'newznab': self._provider_array('NEWZNAB'),
                     'torznab': self._provider_array('TORZNAB'),
                     'rss': self._provider_array('RSS'),
                     'irc': self._provider_array('IRC'),
                     'torrent': torrent,
                     'direct': direct,
                     }
        tot = 0
        for item in self.data:
            tot += len(item)
        self.logger.debug(f"Returning {tot} {plural(tot, 'entry')}")

    def _listnabproviders(self):
        TELEMETRY.record_usage_data()
        # custom output format for prowlarr
        oldnewzlist = self._provider_array('NEWZNAB')
        newzlist = []
        for item in oldnewzlist:
            entry = {'Name': item['DISPNAME'], 'Dispname': item['DISPNAME'], 'Host': item['HOST'],
                     'Apikey': item['API'], 'Enabled': 1 if bool(item['ENABLED']) else 0, 'Categories': ''}
            # merge prowlarr categories
            for key in ['BOOKCAT', 'MAGCAT', 'AUDIOCAT', 'COMICCAT']:
                if item[key]:
                    if entry['Categories']:
                        entry['Categories'] += ','
                    entry['Categories'] += item[key]
            newzlist.append(entry)

        oldtorzlist = self._provider_array('TORZNAB')
        torzlist = []
        for item in oldtorzlist:
            entry = {'Name': item['DISPNAME'], 'Dispname': item['DISPNAME'], 'Host': item['HOST'],
                     'Apikey': item['API'], 'Enabled': 1 if bool(item['ENABLED']) else 0, 'Categories': ''}
            for key in ['BOOKCAT', 'MAGCAT', 'AUDIOCAT', 'COMICCAT']:
                if item[key]:
                    if entry['Categories']:
                        entry['Categories'] += ','
                    entry['Categories'] += item[key]
            torzlist.append(entry)

        tot = len(newzlist) + len(torzlist)
        self.logger.debug(f"Returning {tot} {plural(tot, 'entry')}")
        self.data = {'Success': True,
                     'Data': {
                         'Newznabs': newzlist,
                         'Torznabs': torzlist,
                     },
                     'Error': {'Code': 200, 'Message': 'OK'}
                     }

    def _listrssproviders(self):
        TELEMETRY.record_usage_data()
        providers = self._provider_array('RSS')
        tot = len(providers)
        self.logger.debug(f"Returning {tot} {plural(tot, 'entry')}")
        self.data = providers

    def _listircproviders(self):
        TELEMETRY.record_usage_data()
        providers = self._provider_array('IRC')
        tot = len(providers)
        self.logger.debug(f"Returning {tot} {plural(tot, 'entry')}")
        self.data = providers

    def _listtorrentproviders(self):
        TELEMETRY.record_usage_data()
        providers = []
        for provider in ['KAT', 'TPB', 'LIME', 'TDL']:
            mydict = {'NAME': provider, 'ENABLED': CONFIG.get_bool(provider)}
            for item in ['HOST', 'DLTYPES']:
                name = f"{provider}_{item}"
                mydict[name] = CONFIG.get_str(name)
            for item in ['DLPRIORITY', 'SEEDERS']:
                name = f"{provider}_{item}"
                mydict[name] = CONFIG.get_int(name)
            providers.append(mydict)
        self.logger.debug(f"Returning {len(providers)} {plural(len(providers), 'entry')}")
        self.data = providers

    def _listdirectproviders(self):
        TELEMETRY.record_usage_data()
        providers = self._provider_array('GEN')
        mydict = {'NAME': 'BOK', 'ENABLED': CONFIG.get_bool('BOK')}
        for item in ['HOST', 'LOGIN', 'USER', 'PASS', 'DLTYPES']:
            name = f"BOK_{item}"
            mydict[name] = CONFIG.get_str(name)
        for item in ['DLPRIORITY', 'DLLIMIT']:
            name = f"BOK_{item}"
            mydict[name] = CONFIG.get_int(name)
        providers.append(mydict)
        tot = len(providers)
        self.logger.debug(f"Returning {tot} {plural(tot, 'entry')}")
        self.data = providers

    def _delprovider(self, **kwargs):
        TELEMETRY.record_usage_data()
        if not kwargs.get('name', '') and not kwargs.get('NAME', ''):
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: name'}}
            return

        name = kwargs.get('name', '')
        if not name:
            name = kwargs.get('NAME', '')

        if name.startswith('Newznab') or kwargs.get('providertype', '') == 'newznab':
            providers = CONFIG.providers('NEWZNAB')
            section = 'newznab'
            clear = 'HOST'
        elif name.startswith('Torznab') or kwargs.get('providertype', '') == 'torznab':
            providers = CONFIG.providers('TORZNAB')
            section = 'torznab'
            clear = 'HOST'
        elif name.startswith('RSS_'):
            providers = CONFIG.providers('RSS')
            section = 'rss_'
            clear = 'HOST'
        elif name.startswith('GEN_'):
            providers = CONFIG.providers('GEN')
            section = 'GEN_'
            clear = 'HOST'
        elif name.startswith('IRC_'):
            providers = CONFIG.providers('IRC')
            section = 'IRC_'
            clear = 'SERVER'
        else:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Invalid parameter: name'}}
            return

        for item in providers:
            if item['NAME'] == name or (kwargs.get('providertype', '') and item['DISPNAME'] == name):
                item[clear] = ''
                CONFIG.save_config_and_backup_old(section=section)
                self.data = {'Success': True, 'Data': f'Deleted {name}',
                             'Error': {'Code': 200, 'Message': 'OK'}}
                return
        self.data = {'Success': False, 'Data': '', 'Error': {'Code': 404,
                                                             'Message': f'Provider {name} not found'}}
        return

    def _changeprovider(self, **kwargs):
        TELEMETRY.record_usage_data()
        if not kwargs.get('name', '') and not kwargs.get('NAME', ''):
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: name'}}
            return

        hit = []
        miss = []
        name = kwargs.get('NAME', '')
        if not name:
            name = kwargs.get('name', '')

        # prowlarr gives us  providertype
        if name.startswith('Newznab') or kwargs.get('providertype', '') == 'newznab':
            providers = CONFIG.providers('NEWZNAB')
        elif name.startswith('Torznab') or kwargs.get('providertype', '') == 'torznab':
            providers = CONFIG.providers('TORZNAB')
        elif name.startswith('RSS_'):
            providers = CONFIG.providers('RSS')
        elif name.startswith('IRC_'):
            providers = CONFIG.providers('IRC')
        elif name.startswith('GEN_'):
            providers = CONFIG.providers('GEN')
        elif name in ['BOK', 'KAT', 'TPB', 'LIME', 'TDL']:
            for arg in kwargs:
                if arg in ['HOST', 'DLTYPES', 'DLPRIORITY', 'DLLIMIT', 'SEEDERS']:
                    itemname = f"{name}_{arg}"
                    if itemname in CONFIG:
                        if arg in ['DLPRIORITY', 'DLLIMIT', 'SEEDERS']:
                            CONFIG.set_int(itemname, kwargs[arg])
                        else:
                            CONFIG.set_str(itemname, kwargs[arg])
                        hit += arg
                elif arg == 'ENABLED':
                    hit.append(arg)
                    if kwargs[arg] in ['1', 1, True, 'True', 'true']:
                        val = True
                    else:
                        val = False
                    CONFIG.set_bool(name, val)
                else:
                    miss.append(arg)
            CONFIG.save_config_and_backup_old(section=name)
            self.data = {'Success': True, 'Data': f"Changed {name} [{','.join(hit)}]",
                         'Error': {'Code': 200, 'Message': 'OK'}}
            if miss:
                self.data['Data'] += f" Invalid parameters [{','.join(miss)}]"
            return
        else:
            self.data = {'Success': False, 'Data': '',
                         'Error': {'Code': 400, 'Message': 'Invalid parameter: name'}}
            return

        for item in providers:
            if item['NAME'] == name or (kwargs.get('providertype', '') and item['DISPNAME'] == name):
                for arg in kwargs:
                    if arg.upper() == 'NAME':
                        # don't allow api to change our internal name
                        continue
                    if arg == 'altername':  # prowlarr
                        hit.append(arg)
                        item['DISPNAME'] = kwargs[arg]
                    elif arg.upper() in ['ENABLED', 'MANUAL']:
                        hit.append(arg)
                        if kwargs[arg] in ['1', 1, True, 'True', 'true']:
                            val = True
                        else:
                            val = False
                        item.set_bool(arg.upper(), val)
                    elif arg.upper() in item:
                        hit.append(arg)
                        if arg.upper() in ['EXTENDED', 'APICOUNT', 'APILIMIT', 'RATELIMIT', 'DLPRIORITY', 'LASTUSED',
                                           'SEEDERS', 'SEED_DURATION']:
                            item.set_int(arg.upper(), kwargs[arg])
                        elif arg.upper() in ['SEED_RATIO']:
                            item.set_float(arg.upper(), kwargs[arg])
                        else:
                            item.set_str(arg.upper(), kwargs[arg])
                    elif arg == 'prov_apikey':  # prowlarr
                        hit.append(arg)
                        item.set_str('API', kwargs[arg])
                    elif arg == 'categories' and 'BOOKCAT' in providers[0]:
                        hit.append(arg)
                        # prowlarr only gives us one category list
                        catlist = get_list(kwargs[arg])
                        bookcat = ''
                        audiocat = ''
                        for catnum in catlist:
                            if catnum.startswith('3'):
                                if audiocat:
                                    audiocat += ','
                                audiocat += catnum
                            elif catnum.startswith('7'):
                                if bookcat:
                                    bookcat += ','
                                bookcat += catnum
                        item.set_str('BOOKCAT', bookcat)
                        item.set_str('AUDIOCAT', audiocat)
                    else:
                        miss.append(arg)

                get_capabilities(item, True)

                CONFIG.save_config_and_backup_old(section=item['NAME'])
                self.data = {'Success': True, 'Data': f"Changed {item['NAME']} [{','.join(hit)}]",
                             'Error': {'Code': 200, 'Message': 'OK'}}
                if miss:
                    self.data['Data'] += f" Invalid parameters [{','.join(miss)}]"
                return
        self.data = {'Success': False, 'Data': '',
                     'Error': {'Code': 404, 'Message': f'Provider {name} not found'}}
        return

    def _addprovider(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'type' not in kwargs and 'providertype' not in kwargs:
            self.data = {'Success': False, 'Data': '',
                         'Error': {'Code': 400, 'Message': 'Missing parameter: type'}}
            return
        if 'HOST' not in kwargs and 'SERVER' not in kwargs and 'host' not in kwargs:
            self.data = {'Success': False, 'Data': '',
                         'Error': {'Code': 400, 'Message': 'Missing parameter: HOST or SERVER'}}
            return
        if kwargs.get('type', '') == 'newznab' or kwargs.get('providertype', '') == 'newznab':
            providers = CONFIG.providers('NEWZNAB')
            provname = 'Newznab'
            section = 'Newznab'
        elif kwargs.get('type', '') == 'torznab' or kwargs.get('providertype', '') == 'torznab':
            providers = CONFIG.providers('TORZNAB')
            provname = 'Torznab'
            section = 'Torznab'
        elif kwargs['type'] == 'rss':
            providers = CONFIG.providers('RSS')
            provname = 'RSS_'
            section = 'rss_'
        elif kwargs['type'] == 'gen':
            providers = CONFIG.providers('GEN')
            provname = 'GEN_'
            section = 'GEN_'
        elif kwargs['type'] == 'irc':
            providers = CONFIG.providers('IRC')
            provname = 'IRC_'
            section = 'IRC_'
        else:
            self.data = {'Success': False,
                         'Data': '',
                         'Error': {'Code': 400,
                                   'Message': 'Invalid parameter: type. Should be newznab,torznab,rss,gen,irc'}
                         }
            return

        num = len(providers)
        empty_slot = providers[len(providers) - 1]

        hit = []
        miss = []
        provname = f"{provname}_{num - 1}"
        empty_slot['DISPNAME'] = provname
        for arg in kwargs:
            if arg == 'prov_apikey':
                hit.append(arg)
                empty_slot['API'] = kwargs[arg]
            elif arg == 'enabled':
                hit.append(arg)
                empty_slot['ENABLED'] = kwargs[arg] == 'true'
            elif arg in ['altername', 'name']:
                for existing in providers:
                    if kwargs[arg] and existing['DISPNAME'] == kwargs[arg]:
                        self.data = {'Success': False,
                                     'Data': '',
                                     'Error': {'Code': 409,
                                               'Message': f'{kwargs[arg]} Already Exists'}
                                     }
                        return
                hit.append(arg)
                empty_slot['DISPNAME'] = kwargs[arg]
            elif arg == 'categories' and 'BOOKCAT' in providers[0]:
                hit.append(arg)
                # prowlarr only gives us one category list
                catlist = get_list(kwargs[arg])
                bookcat = ''
                audiocat = ''
                for item in catlist:
                    if item.startswith('3'):
                        if audiocat:
                            audiocat += ','
                        audiocat += item
                    else:
                        if bookcat:
                            bookcat += ','
                        bookcat += item
                empty_slot['BOOKCAT'] = bookcat
                empty_slot['AUDIOCAT'] = audiocat
            elif arg in ['providertype', 'type']:
                hit.append(arg)
            elif arg.upper() in providers[0]:
                hit.append(arg)
                empty_slot[arg.upper()] = kwargs[arg]
                if arg.upper() in ['EXTENDED', 'APICOUNT', 'APILIMIT', 'RATELIMIT', 'DLPRIORITY', 'LASTUSED',
                                   'SEEDERS', 'SEED_DURATION']:
                    empty_slot.set_int(arg.upper(), kwargs[arg])
                elif arg.upper() in ['SEED_RATIO']:
                    empty_slot.set_float(arg.upper(), kwargs[arg])
                else:
                    empty_slot.set_str(arg.upper(), kwargs[arg])
            else:
                miss.append(arg)

        get_capabilities(empty_slot, True)

        CONFIG.save_config_and_backup_old(section=section)
        self.data = {'Success': True, 'Data': f"Added {section} [{','.join(hit)}]",
                     'Error': {'Code': 200, 'Message': 'OK'}}
        if miss:
            self.data['Data'] += f" Invalid parameters [{','.join(miss)}]"
        return

    def _memuse(self):
        TELEMETRY.record_usage_data()
        """ Current Memory usage in kB """
        if os.name == 'nt':
            ok, self.data = get_process_memory()
            if not ok:
                self.data = {'Success': False, 'Data': '', 'Error': {'Code': 501,
                                                                     'Message': 'Needs psutil module installed'}}
        else:
            with open('/proc/self/status') as f:
                memusage = f.read().split('VmRSS:')[1].split('\n')[0][:-3]
            self.data = memusage.strip()

    def _cpuuse(self):
        TELEMETRY.record_usage_data()
        ok, self.data = get_cpu_use()
        if not ok:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 501,
                                                                 'Message': 'Needs psutil module installed'}}

    def _nice(self):
        TELEMETRY.record_usage_data()
        if os.name == 'nt':
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 501, 'Message': 'Unsupported in Windows'}}
        else:
            self.data = os.nice(0)

    def _nicer(self):
        TELEMETRY.record_usage_data()
        if os.name == 'nt':
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 501, 'Message': 'Unsupported in Windows'}}
        else:
            self.data = os.nice(1)

    @staticmethod
    def _gc_init():
        TELEMETRY.record_usage_data()
        from collections import defaultdict
        from gc import get_objects
        lazylibrarian.GC_BEFORE = defaultdict(int)
        for i in get_objects():
            lazylibrarian.GC_BEFORE[type(i)] += 1

    def _gc_collect(self):
        TELEMETRY.record_usage_data()
        from gc import collect
        self.data = collect()

    def _gc_stats(self):
        TELEMETRY.record_usage_data()
        if not lazylibrarian.GC_BEFORE:
            self.data = 'Not initialised'
            return
        from collections import defaultdict
        from gc import get_objects
        lazylibrarian.GC_AFTER = defaultdict(int)
        for i in get_objects():
            lazylibrarian.GC_AFTER[type(i)] += 1

        res = ''
        for k in lazylibrarian.GC_AFTER.keys():
            if k in lazylibrarian.GC_BEFORE:
                n = int(lazylibrarian.GC_AFTER[k] - lazylibrarian.GC_BEFORE[k])
            else:
                n = int(lazylibrarian.GC_AFTER[k])
            if n:
                k = str(k).split("'")[1]
                changed = f"{n} {k}<br>"
                res += changed
        self.data = res

    def _getrssfeed(self, **kwargs):
        TELEMETRY.record_usage_data()
        ftype = kwargs.get('feed', 'eBook')
        limit = kwargs.get('limit', 10)
        authorid = kwargs.get('authorid', '')

        # url might end in .xml
        if not str(limit).isdigit():
            try:
                limit = int(str(limit).split('.')[0])
            except (IndexError, ValueError):
                limit = 10

        userid = 0
        scheme, netloc, path, qs, anchor = urlsplit(cherrypy.url())
        netloc = cherrypy.request.headers.get('X-Forwarded-Host')
        if not netloc:
            netloc = cherrypy.request.headers.get('Host')
        path = path.replace('rss_feed', '').rstrip('/')
        baseurl = urlunsplit((scheme, netloc, path, qs, anchor))
        self.data = gen_feed(ftype, limit=limit, user=userid, baseurl=baseurl, authorid=authorid)

    def _synccalibrelist(self, **kwargs):
        TELEMETRY.record_usage_data()
        col1 = kwargs.get('read')
        col2 = kwargs.get('toread')
        self.data = sync_calibre_list(col1, col2)

    def _subscribe(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['user', 'feed']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        db = database.DBConnection()
        res = db.match('SELECT UserID from users WHERE userid=?', (kwargs['user'],))
        if not res:
            self.data = 'Invalid userid'
            db.close()
            return
        for provider in CONFIG.providers('RSS'):
            if provider['DISPNAME'] == kwargs['feed'] and wishlist_type(provider['HOST']):
                db.action('INSERT into subscribers (UserID , Type, WantID ) VALUES (?, ?, ?)',
                          (kwargs['user'], 'feed', kwargs['feed']))
                self.data = 'OK'
                db.close()
                return
        db.close()
        self.data = 'Invalid feed'
        return

    def _unsubscribe(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['user', 'feed']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        db = database.DBConnection()
        db.action('DELETE FROM subscribers WHERE UserID=? and Type=? and WantID=?',
                  (kwargs['user'], 'feed', kwargs['feed']))
        db.close()
        self.data = 'OK'
        return

    def _calibrelist(self, **kwargs):
        TELEMETRY.record_usage_data()
        col1 = kwargs.get('read')
        col2 = kwargs.get('toread')
        self.data = calibre_list(col1, col2)

    def _showcaps(self, **kwargs):
        TELEMETRY.record_usage_data()
        prov = kwargs.get('provider')
        if not prov:
            self.data = 'Missing parameter: provider'
            return
        match = False
        for provider in CONFIG.providers('NEWZNAB'):
            if prov == provider['HOST']:
                prov = provider
                match = True
                break
        if not match:
            for provider in CONFIG.providers('TORZNAB'):
                if prov == provider['HOST']:
                    prov = provider
                    match = True
                    break
        if not match:
            self.data = 'Invalid parameter: provider'
            return
        self.data = get_capabilities(prov, True)

    def _help(self):
        TELEMETRY.record_usage_data()
        res = '<html>' \
              '<p>Sample use: http://localhost:5299/api?apikey=VALIDKEYHERE?cmd=COMMAND</p>' \
              '<p>Valid commands:</p><table><tr><th style="text-align: left;">Command</th>' \
              '<th style="text-align: left;">Parameters</th></tr>'
        for key in sorted(cmd_dict):
            # list all commands if full access api_key, or only the read-only commands
            if self.apikey == CONFIG.get_str('API_KEY') or cmd_dict[key][0] == 0:
                res += f"<tr><td>{key}</td><td>{cmd_dict[key][1]}</td></tr>"
        res += '</table></html>'
        self.data = res

    def _ignoredstats(self):
        TELEMETRY.record_usage_data()
        cmd = "select scanresult,count(*) counter from books where status='Ignored' group by scanresult"
        self.data = self._dic_from_query(cmd)

    def _listalienauthors(self):
        TELEMETRY.record_usage_data()
        cmd = "SELECT AuthorID,AuthorName from authors WHERE AuthorID "
        if CONFIG.get_str('BOOK_API') != 'OpenLibrary':
            cmd += "NOT "
        cmd += "LIKE 'OL%A'"
        self.data = self._dic_from_query(cmd)

    def _listalienbooks(self):
        TELEMETRY.record_usage_data()
        cmd = "SELECT BookID,BookName from books WHERE BookID "
        if CONFIG.get_str('BOOK_API') != 'OpenLibrary':
            cmd += "NOT "
        cmd += "LIKE 'OL%W'"
        self.data = self._dic_from_query(cmd)

    def _gethistory(self):
        TELEMETRY.record_usage_data()
        self.data = self._dic_from_query(
            "SELECT * from wanted WHERE Status != 'Skipped' and Status != 'Ignored'")

    def _listnewauthors(self, **kwargs):
        TELEMETRY.record_usage_data()
        limit = kwargs.get('limit', '')
        if limit:
            limit = f"limit {limit}"
        self.data = self._dic_from_query(
            f"SELECT authorid,authorname,dateadded,reason,status from authors order by dateadded desc {limit}")

    def _listnewbooks(self, **kwargs):
        TELEMETRY.record_usage_data()
        limit = kwargs.get('limit', '')
        if limit:
            limit = f"limit {limit}"
        self.data = self._dic_from_query(
            f"SELECT bookid,bookname,bookadded,scanresult,status from books order by bookadded desc {limit}")

    def _showthreads(self):
        TELEMETRY.record_usage_data()
        self.data = get_threads()

    def _showmonths(self):
        TELEMETRY.record_usage_data()
        self.data = f"{lazylibrarian.MONTHNAMES[0]}, {lazylibrarian.SEASONS}"

    def _renameaudio(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            self.data = audio_rename(kwargs['id'], rename=True)

    def _getbookpubdate(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            self.data = get_book_pubdate(kwargs['id'])

    def _getfiledirect(self, **kwargs):
        TELEMETRY.record_usage_data()
        bookid = kwargs.get('id') or kwargs.get('bookid')
        file_type = (kwargs.get('type') or '').strip().lower()
        if file_type and file_type not in ['ebook', 'book', 'audiobook', 'audio', 'comic', 'issue', 'magazine']:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Invalid parameter: type'}}
            return
        if not bookid:
            self.data = {'Success': False, 'Data': '', 'Error': {'Code': 400,
                                                                 'Message': 'Missing parameter: id'}}
            return

        if not file_type:
            file_type = 'ebook'

        if file_type in ['comic']:
            try:
                comicid, issueid = str(bookid).split('_')
            except ValueError as err:
                raise cherrypy.HTTPError(400, 'Invalid parameter: id') from err
            db = database.DBConnection()
            try:
                cmd = ("SELECT Title,IssueFile from comics,comicissues WHERE comics.ComicID=comicissues.ComicID "
                       "and comics.ComicID=? and IssueID=?")
                res = db.match(cmd, (comicid, issueid))
            finally:
                db.close()
            if not res or not res['IssueFile']:
                raise cherrypy.HTTPError(404, f"No file found for comic {bookid}")
            myfile = res['IssueFile']
            if not path_isfile(myfile):
                raise cherrypy.HTTPError(404, f"No file found for comic {bookid}")
            name = f"{res['Title']} {issueid}{splitext(myfile)[1]}"
            self.logger.debug(f'API comic download {myfile}')
            self.file_response = (myfile, name)
            return

        if file_type in ['issue', 'magazine']:
            db = database.DBConnection()
            try:
                res = db.match('SELECT Title,IssueFile from issues WHERE IssueID=?', (bookid,))
            finally:
                db.close()
            if not res or not res['IssueFile']:
                raise cherrypy.HTTPError(404, f"No file found for issue {bookid}")
            myfile = res['IssueFile']
            if not path_isfile(myfile):
                raise cherrypy.HTTPError(404, f"No file found for issue {bookid}")
            name = f"{res['Title']} {bookid}{splitext(myfile)[1]}"
            self.logger.debug(f'API issue download {myfile}')
            self.file_response = (myfile, name)
            return

        bookid_key = 'BookID'
        for item, info_source in lazylibrarian.INFOSOURCES.items():
            if CONFIG['BOOK_API'] == item:
                bookid_key = info_source['book_key']
                break

        db = database.DBConnection()
        try:
            cmd = f"SELECT BookFile,AudioFile,BookName from books WHERE {bookid_key}=? or BookID=?"
            res = db.match(cmd, (bookid, bookid))
        finally:
            db.close()

        if not res:
            raise cherrypy.HTTPError(404, f"No file found for book {bookid}")

        if file_type in ['audio', 'audiobook']:
            myfile = res['AudioFile']
            if not myfile:
                raise cherrypy.HTTPError(404, f"No file found for book {bookid}")

            cnt = 0
            if path_isfile(myfile):
                parentdir = os.path.dirname(myfile)
                for _, _, filenames in os.walk(parentdir):
                    for filename in filenames:
                        if CONFIG.is_valid_booktype(filename, 'audiobook'):
                            cnt += 1

            if cnt > 1 and not CONFIG.get_bool('RSS_PODCAST'):
                target = zip_audio(os.path.dirname(myfile), res['BookName'], bookid)
                if target and path_isfile(target):
                    self.logger.debug(f'API audio download {target}')
                    self.file_response = (target, res['BookName'] + '.zip')
                    return

            if not path_isfile(myfile):
                raise cherrypy.HTTPError(404, f"No file found for book {bookid}")

            self.logger.debug(f'API audio download {myfile}')
            self.file_response = (myfile, os.path.basename(myfile))
            return

        if not res['BookFile']:
            raise cherrypy.HTTPError(404, f"No file found for book {bookid}")

        myfile = res['BookFile']
        fname, extn = splitext(myfile)
        types = []
        for item in get_list(CONFIG['EBOOK_TYPE']):
            target = fname + '.' + item
            if path_isfile(target):
                types.append(item)

        if not types and path_isfile(myfile):
            extn = extn.lstrip('.')
            if extn:
                types = [extn]

        if not types:
            raise cherrypy.HTTPError(404, f"No file found for book {bookid}")
        extn = types[0]

        if types:
            myfile = fname + '.' + extn

        if not path_isfile(myfile):
            raise cherrypy.HTTPError(404, f"No file found for book {bookid}")

        name = f"{res['BookName']}.{extn}" if extn else res['BookName']
        self.logger.debug(f'API book download {myfile}')
        self.file_response = (myfile, name)

    def _createplaylist(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            self.data = audio_rename(kwargs['id'], playlist=True)

    def _preprocessaudio(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['dir', 'title', 'author']:
            if item not in kwargs:
                self.data = f'Missing parameter: {item}'
                return
        tag = True if 'tag' in kwargs else None
        merge = True if 'merge' in kwargs else None
        bookid = kwargs.get('id', 0)
        preprocess_audio(kwargs['dir'], bookid, kwargs['author'], kwargs['title'], merge=merge, tag=tag)
        self.data = 'OK'

    def _preprocessbook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'dir' not in kwargs:
            self.data = 'Missing parameter: dir'
            return
        preprocess_ebook(kwargs['dir'])
        self.data = 'OK'

    def _preprocessallbooks(self):
        TELEMETRY.record_usage_data()
        q = "SELECT BookFile from books where BookFile != '' and BookFile is not null"
        res = self._dic_from_query(q)
        for cnt, item in enumerate(res, start=1):
            folder = os.path.dirname(item['BookFile'])
            if os.path.isdir(folder):
                self.logger.debug(f"Preprocessing {cnt} of {len(res)}")
                preprocess_ebook(folder)
        self.data = 'OK'

    def _preprocessmagazine(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['dir', 'cover']:
            if item not in kwargs:
                self.data = f'Missing parameter: {item}'
                return
        if 'tag' in kwargs:
            for item in ['title', 'issue']:
                if item not in kwargs:
                    self.data = f'Missing parameter: {item}'
                    return
            preprocess_magazine(kwargs['dir'], check_int(kwargs['cover'], 0), tag=True,
                                title=kwargs['title'], issue=kwargs['issue'], genres=kwargs.get('genres', ''))
        else:
            preprocess_magazine(kwargs['dir'], check_int(kwargs['cover'], 0))
        self.data = 'OK'

    def _importbook(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['id', 'dir']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        library = kwargs.get('library', 'eBook')
        self.data = process_book_from_dir(kwargs['dir'], library, kwargs['id'])

    def _importmag(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['title', 'num', 'file']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        self.data = process_mag_from_file(kwargs['file'], kwargs['title'], kwargs['num'])

    def _namevars(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            self.data = name_vars(kwargs['id'])

    def _savetable(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'table' not in kwargs:
            self.data = 'Missing parameter: table'
            return
        valid = ['users', 'magazines']
        if kwargs['table'] not in valid:
            self.data = f'Invalid table. Only {str(valid)}'
            return
        self.data = f"Saved {dump_table(kwargs['table'], DIRS.DATADIR)}"

    def _writeallopf(self, **kwargs):
        TELEMETRY.record_usage_data()
        db = database.DBConnection()
        try:
            books = db.select('select BookID from books where BookFile is not null')
        finally:
            db.close()
        counter = 0
        if books:
            for book in books:
                bookid = book['BookID']
                if 'refresh' in kwargs:
                    self._writeopf(id=bookid, refresh=True)
                else:
                    self._writeopf(id=bookid)
                try:
                    if self.data[1] is True:
                        counter += 1
                except IndexError:
                    continue
        self.data = f"Updated opf for {counter} {plural(counter, 'book')}"

    def _writeopf(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
            return

        db = database.DBConnection()
        try:
            cmd = ("SELECT AuthorName,BookID,BookName,BookDesc,BookIsbn,BookImg,BookDate,BookLang,BookPub,BookFile,"
                   "BookRate from books,authors WHERE BookID=? and books.AuthorID = authors.AuthorID")
            res = db.match(cmd, (kwargs['id'],))
        finally:
            db.close()
        if not res:
            self.data = f"No data found for bookid {kwargs['id']}"
            return
        if not res['BookFile'] or not path_isfile(res['BookFile']):
            self.data = f"No bookfile found for bookid {kwargs['id']}"
            return
        dest_path = os.path.dirname(res['BookFile'])
        global_name = splitext(os.path.basename(res['BookFile']))[0]
        refresh = 'refresh' in kwargs
        process_img(dest_path, kwargs['id'], res['BookImg'], global_name, overwrite=refresh)
        self.data = create_opf(dest_path, res, global_name, refresh)

    @staticmethod
    def _dumpmonths():
        TELEMETRY.record_usage_data()
        json_file = os.path.join(DIRS.DATADIR, 'monthnames.json')
        with open(syspath(json_file), 'w') as f:
            json.dump(lazylibrarian.MONTHNAMES[0], f)
        json_file = os.path.join(DIRS.DATADIR, 'seasons.json')
        with open(syspath(json_file), 'w') as f:
            json.dump(lazylibrarian.SEASONS, f)

    def _getwanted(self):
        TELEMETRY.record_usage_data()
        self.data = self._dic_from_query(
            "SELECT * from books WHERE Status='Wanted'")

    def _getread(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self.data = get_readinglist("HaveRead", self.id)

    def _gettoread(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self.data = get_readinglist("ToRead", self.id)

    def _getreading(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self.data = get_readinglist("Reading", self.id)

    def _getabandoned(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self.data = get_readinglist("Abandoned", self.id)

    def _vacuum(self):
        TELEMETRY.record_usage_data()
        msg1 = self._dic_from_query("vacuum")
        msg2 = self._dic_from_query("pragma integrity_check")
        self.data = str(msg1) + str(msg2)

    def _getsnatched(self):
        TELEMETRY.record_usage_data()
        cmd = ("SELECT * from books,wanted WHERE books.bookid=wanted.bookid and books.Status='Snatched' "
               "or AudioStatus='Snatched'")
        self.data = self._dic_from_query(cmd)

    def _logmessage(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['level', 'text']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        self.data = kwargs['text']
        if kwargs['level'].upper() == 'INFO':
            self.logger.info(self.data)
        elif kwargs['level'].upper() == 'WARN':
            self.logger.warning(self.data)
        elif kwargs['level'].upper() == 'ERROR':
            self.logger.error(self.data)
        elif kwargs['level'].upper() == 'DEBUG':
            self.logger.debug(self.data)
        else:
            self.data = f"Invalid level: {kwargs['level']}"
        return

    def _getdebug(self):
        TELEMETRY.record_usage_data()
        self.data = log_header().replace('\n', '<br>')

    def _getmodules(self):
        TELEMETRY.record_usage_data()
        lst = ''
        for item in sys.modules:
            lst += f"{item}: {str(sys.modules[item]).replace('<', '').replace('>', '')}<br>"
        self.data = lst

    def _checkmodules(self):
        TELEMETRY.record_usage_data()
        lst = []
        for item in sys.modules:
            data = str(sys.modules[item]).replace('<', '').replace('>', '')
            for libname in ['apscheduler', 'mobi', 'oauth2', 'pynma', 'pythontwitter', 'unrar']:
                if libname in data and 'dist-packages' in data:
                    lst.append(f"{item}: {data}")
        self.data = lst

    def _clearlogs(self):
        TELEMETRY.record_usage_data()
        LOGCONFIG.clear_ui_log()
        self.data = LOGCONFIG.delete_log_files(CONFIG['LOGDIR'])

    def _createsupportzip(self):
        TELEMETRY.record_usage_data()
        msg, filename = create_support_zip()
        self.data = f'{msg}\nFile created is {filename}'

    def _getindex(self):
        TELEMETRY.record_usage_data()
        self.data = self._dic_from_query(
            'SELECT * from authors order by AuthorName COLLATE NOCASE')

    def _listmissingbookfile(self):
        TELEMETRY.record_usage_data()
        db = database.DBConnection()
        q = "SELECT BookID,BookFile,BookName,AuthorName from books,authors where "
        q += "(BookFile is not NULL and bookfile != '') and books.AuthorID = authors.AuthorID"
        rows = db.select(q)
        rows_as_dic = []
        for row in rows:
            if not path_isfile(row['BookFile']):
                rows_as_dic.append(dict(row))
        self.data = rows_as_dic

    def _listnolang(self):
        TELEMETRY.record_usage_data()
        q = 'SELECT BookID,BookISBN,BookName,AuthorName from books,authors where '
        q += '(BookLang="Unknown" or BookLang="" or BookLang is NULL) and books.AuthorID = authors.AuthorID'
        self.data = self._dic_from_query(q)

    def _listnogenre(self):
        TELEMETRY.record_usage_data()
        q = 'SELECT BookID,BookName,AuthorName from books,authors where books.Status != "Ignored" and '
        q += '(BookGenre="Unknown" or BookGenre="" or BookGenre is NULL) and books.AuthorID = authors.AuthorID'
        self.data = self._dic_from_query(q)

    def _listnodesc(self):
        TELEMETRY.record_usage_data()
        q = 'SELECT BookID,BookName,AuthorName from books,authors where books.Status != "Ignored" and '
        q += '(BookDesc="" or BookDesc is NULL) and books.AuthorID = authors.AuthorID'
        self.data = self._dic_from_query(q)

    def _setnodesc(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'refresh' in kwargs:
            expire = True
            extra = ' or BookDesc="No Description"'
        else:
            expire = False
            extra = ''
        q = 'SELECT BookID,BookName,AuthorName,BookISBN from books,authors where books.Status != "Ignored" and '
        q += f"(BookDesc=\"\" or BookDesc is NULL{extra}) and books.AuthorID = authors.AuthorID"
        db = database.DBConnection()
        res = db.select(q)
        descs = 0
        cnt = 0
        self.logger.debug(f"Checking description for {len(res)} {plural(len(res), 'book')}")
        # ignore all errors except blocked (not found etc.)
        blocked = False
        for item in res:
            cnt += 1
            isbn = item['BookISBN']
            auth = item['AuthorName']
            book = item['BookName']
            data = get_gb_info(isbn, auth, book, expire=expire)
            if data and data['desc']:
                descs += 1
                self.logger.debug(f"Updated description for {auth}:{book}")
                db.action('UPDATE books SET bookdesc=? WHERE bookid=?', (data['desc'], item['BookID']))
            elif data is None:  # error, see if it's because we are blocked
                if BLOCKHANDLER.is_blocked('googleapis'):
                    blocked = True
                if blocked:
                    break
        db.close()
        msg = f"Scanned {cnt} {plural(cnt, 'book')}, found {descs} new {plural(descs, 'description')} from {len(res)}"
        if blocked:
            msg += ': Access Blocked'
        self.data = msg
        self.logger.info(self.data)

    def _setnogenre(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'refresh' in kwargs:
            expire = True
            extra = ' or BookGenre="Unknown"'
        else:
            expire = False
            extra = ''
        q = 'SELECT BookID,BookName,AuthorName,BookISBN from books,authors where books.Status != "Ignored" and '
        q += f"(BookGenre=\"\" or BookGenre is NULL{extra}) and books.AuthorID = authors.AuthorID"
        db = database.DBConnection()
        try:
            res = db.select(q)
        finally:
            db.close()
        genre = 0
        cnt = 0
        self.logger.debug(f"Checking genre for {len(res)} {plural(len(res), 'book')}")
        # ignore all errors except blocked (not found etc.)
        blocked = False
        for item in res:
            cnt += 1
            isbn = item['BookISBN']
            auth = item['AuthorName']
            book = item['BookName']
            data = get_gb_info(isbn, auth, book, expire=expire)
            if data and data['genre']:
                genre += 1
                newgenre = genre_filter(data['genre'])
                self.logger.debug(f"Updated genre for {auth}:{book} [{newgenre}]")
                set_genres([newgenre], item['BookID'])
            elif data is None:
                if BLOCKHANDLER.is_blocked('googleapis'):
                    blocked = True
                if blocked:
                    break
        msg = f"Scanned {cnt} {plural(cnt, 'book')}, found {genre} new {plural(genre, 'genre')} from {len(res)}"
        if blocked:
            msg += ': Access Blocked'
        self.data = msg
        self.logger.info(self.data)

    def _listnoisbn(self):
        TELEMETRY.record_usage_data()
        q = 'SELECT BookID,BookName,AuthorName from books,authors where books.AuthorID = authors.AuthorID'
        q += ' and (BookISBN="" or BookISBN is NULL)'
        self.data = self._dic_from_query(q)

    def _listnobooks(self):
        TELEMETRY.record_usage_data()
        q = 'select authorid,authorname,reason from authors where haveebooks+haveaudiobooks=0 and '
        q += "instr(reason, 'Series') = 0 except select authors.authorid,authorname,reason from books,authors where "
        q += 'books.authorid=authors.authorid and books.status=="Wanted";'
        self.data = self._dic_from_query(q)

    def _removenobooks(self):
        TELEMETRY.record_usage_data()
        self._listnobooks()
        if self.data:
            db = database.DBConnection()
            try:
                for auth in self.data:
                    self.logger.debug(f"Deleting {auth['AuthorName']}")
                    db.action("DELETE from authors WHERE authorID=?", (auth['AuthorID'],))
            finally:
                db.close()

    def _listignoredseries(self):
        TELEMETRY.record_usage_data()
        q = "SELECT SeriesID,SeriesName from series where Status='Ignored'"
        self.data = self._dic_from_query(q)

    def _listdupebooks(self):
        TELEMETRY.record_usage_data()
        self.data = []
        q = "select authorid,authorname from authors"
        res = self._dic_from_query(q)
        for author in res:
            q = "select count('bookname'),authorid,bookname from books where "
            q += f"AuthorID={author['AuthorID']} "
            q += "and ( Status != 'Ignored' or AudioStatus != 'Ignored' ) "
            q += "group by bookname having ( count(bookname) > 1 )"
            r = self._dic_from_query(q)
            self.data += r

    def _listdupebookstatus(self):
        TELEMETRY.record_usage_data()
        self._listdupebooks()
        res = self.data
        self.data = []
        for item in res:
            q = 'select BookID,BookName,AuthorName,books.Status,AudioStatus from books,authors where '
            q += f"books.authorid=authors.authorid and books.authorid={item['AuthorID']} "
            q += f"and BookName=\"{item['BookName']}\" "
            q += "and ( books.Status != 'Ignored' or AudioStatus != 'Ignored' )"
            r = self._dic_from_query(q)
            self.data += r

    def _listignoredbooks(self):
        TELEMETRY.record_usage_data()
        q = "SELECT BookID,BookName from books where Status='Ignored'"
        self.data = self._dic_from_query(q)

    def _listignoredauthors(self):
        TELEMETRY.record_usage_data()
        q = "SELECT AuthorID,AuthorName from authors where Status='Ignored'"
        self.data = self._dic_from_query(q)

    def _getauthor(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
            return
        author = self._dic_from_query(
            f"SELECT * from authors WHERE AuthorID=\"{self.id}\"")
        books = self._dic_from_query(
            f"SELECT * from books WHERE AuthorID=\"{self.id}\"")

        self.data = {'author': author, 'books': books}

    def _getmagazines(self):
        TELEMETRY.record_usage_data()
        self.data = self._dic_from_query('SELECT * from magazines order by Title COLLATE NOCASE')

    def _getallbooks(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.limit = kwargs.get('limit')
        self.sort = kwargs.get('sort')
        self.status = kwargs.get('status')
        self.audiostatus = kwargs.get('audiostatus')
        q = '''SELECT authors.AuthorID,AuthorName,AuthorLink,BookName,BookSub,BookGenre,BookIsbn,BookPub,
                BookRate,BookImg,BookPages,BookLink,BookID,BookDate,BookLang,BookAdded,books.Status,
                audiostatus,booklibrary,audiolibrary from books,authors where books.AuthorID = authors.AuthorID'''

        if self.status:
            q += f" and books.Status='{self.status}'"
        if self.audiostatus:
            q += f" and books.AudioStatus='{self.audiostatus}'"
        if self.sort:
            q += f' order by {self.sort}'
        if self.limit and self.limit.isnumeric():
            q += f' limit {self.limit}'
        self.data = self._dic_from_query(q)

    def _getissues(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('name')
        self.limit = kwargs.get('limit')
        self.sort = kwargs.get('sort')
        if self.id:
            magazine = self._dic_from_query(
                f"SELECT * from magazines WHERE Title='{self.id}' COLLATE NOCASE")
            q = f"SELECT * from issues WHERE Title='{self.id}' COLLATE NOCASE"
            if self.sort:
                q += f' order by {self.sort}'
            if self.limit and self.limit.isnumeric():
                q += f' limit {self.limit}'
            issues = self._dic_from_query(q)
            self.data = {'magazine': magazine, 'issues': issues}
        else:
            q = 'SELECT * from issues'
            if self.sort:
                q += f' order by {self.sort}'
            if self.limit and self.limit.isnumeric():
                q += f' limit {self.limit}'
            self.data = self._dic_from_query(q)

    def _shrinkmag(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['name', 'dpi']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        self.data = shrink_mag(kwargs['name'], check_int(kwargs['dpi'], 0))

    def _getissuename(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'name' not in kwargs:
            self.data = 'Missing parameter: name'
            return
        self.data = ''

        if os.path.isfile(kwargs['name']):
            dirname = os.path.dirname(kwargs['name'])
        else:
            dirname = ''

        dateparts = get_dateparts(kwargs['name'])
        issuedate = dateparts.get('dbdate', '')

        if dateparts['style'] and dirname:
            title = os.path.basename(dirname)
            global_name = format_issue_filename(CONFIG['MAG_DEST_FILE'], title, dateparts)
            self.data = os.path.join(dirname, f"{global_name}.{splitext(kwargs['name'])[1]} {dateparts}")
            return
        self.data = f"Regex {dateparts['style']} [{issuedate}] {dateparts}"

    def _createmagcovers(self, **kwargs):
        TELEMETRY.record_usage_data()
        refresh = 'refresh' in kwargs
        if 'wait' in kwargs:
            self.data = create_mag_covers(refresh=refresh)
        else:
            threading.Thread(target=create_mag_covers, name='API-MAGCOVERS', args=[refresh]).start()

    def _createmagcover(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'file' not in kwargs:
            self.data = 'Missing parameter: file'
            return
        refresh = 'refresh' in kwargs
        if 'page' in kwargs:
            self.data = create_mag_cover(issuefile=kwargs['file'], refresh=refresh, pagenum=kwargs['page'])
        else:
            self.data = create_mag_cover(issuefile=kwargs['file'], refresh=refresh)

    def _getbook(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
            return
        book = self._dic_from_query(f"SELECT * from books WHERE BookID=\"{self.id}\"")
        self.data = {'book': book}

    def _queuebook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            db = database.DBConnection()
            try:
                res = db.match('SELECT Status,AudioStatus from books WHERE BookID=?', (kwargs['id'],))
                if not res:
                    self.data = f"Invalid id: {kwargs['id']}"
                else:
                    if kwargs.get('type', '') == 'AudioBook':
                        db.action("UPDATE books SET AudioStatus='Wanted' WHERE BookID=?", (kwargs["id"],))
                    else:
                        db.action("UPDATE books SET Status='Wanted' WHERE BookID=?", (kwargs["id"],))
                    self.data = 'OK'
            finally:
                db.close()

    def _unqueuebook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            db = database.DBConnection()
            try:
                res = db.match('SELECT Status,AudioStatus from books WHERE BookID=?', (kwargs['id'],))
                if not res:
                    self.data = f"Invalid id: {kwargs['id']}"
                else:
                    if kwargs.get('type', '') == 'AudioBook':
                        db.action("UPDATE books SET AudioStatus='Skipped' WHERE BookID=?", (kwargs["id"],))
                    else:
                        db.action("UPDATE books SET Status='Skipped' WHERE BookID=?", (kwargs["id"],))
                    self.data = 'OK'
            finally:
                db.close()

    def _addmagazine(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('name')
        if not self.id:
            self.data = 'Missing parameter: name'
            return
        control_value_dict = {"Title": self.id}
        new_value_dict = {
            "Regex": None,
            "Status": "Active",
            "MagazineAdded": today(),
            "IssueStatus": "Wanted",
            "Reject": None
        }
        db = database.DBConnection()
        try:
            db.upsert("magazines", new_value_dict, control_value_dict)
        finally:
            db.close()

    def _removemagazine(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('name')
        if not self.id:
            self.data = 'Missing parameter: name'
            return
        db = database.DBConnection()
        try:
            db.action('DELETE from magazines WHERE Title=? COLLATE NOCASE', (self.id,))
            db.action('DELETE from wanted WHERE BookID=? COLLATE NOCASE', (self.id,))
        finally:
            db.close()

    def _pauseauthor(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            db = database.DBConnection()
            try:
                res = db.match('SELECT AuthorName from authors WHERE AuthorID=?', (kwargs['id'],))
                if not res:
                    self.data = f"Invalid id: {kwargs['id']}"
                else:
                    db.action("UPDATE authors SET Status='Paused' WHERE AuthorID=?", (kwargs["id"],))
                    self.data = 'OK'
            finally:
                db.close()

    def _ignoreauthor(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            db = database.DBConnection()
            try:
                res = db.match('SELECT AuthorName from authors WHERE AuthorID=?', (kwargs['id'],))
                if not res:
                    self.data = f"Invalid id: {kwargs['id']}"
                else:
                    db.action("UPDATE authors SET Status='Ignored' WHERE AuthorID=?", (kwargs["id"],))
                    self.data = 'OK'
            finally:
                db.close()

    def _resumeauthor(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
        else:
            db = database.DBConnection()
            try:
                res = db.match('SELECT AuthorName from authors WHERE AuthorID=?', (kwargs['id'],))
                if not res:
                    self.data = f"Invalid id: {kwargs['id']}"
                else:
                    db.action("UPDATE authors SET Status='Active' WHERE AuthorID=?", (kwargs["id"],))
                    self.data = 'OK'
            finally:
                db.close()

    def _authorupdate(self):
        TELEMETRY.record_usage_data()
        try:
            self.data = author_update(restart=False, only_overdue=False)
        except Exception as e:
            self.data = f"{type(e).__name__} {str(e)}"

    def _seriesupdate(self):
        TELEMETRY.record_usage_data()
        try:
            self.data = series_update(restart=False, only_overdue=False)
        except Exception as e:
            self.data = f"{type(e).__name__} {str(e)}"

    def _refreshauthor(self, **kwargs):
        TELEMETRY.record_usage_data()
        refresh = 'refresh' in kwargs
        self.id = kwargs.get('name')
        if not self.id:
            self.data = 'Missing parameter: name'
            return
        try:
            self.data = add_author_to_db(self.id, refresh=refresh, reason=f"API refresh_author {self.id}")
        except Exception as e:
            self.data = f"{type(e).__name__} {str(e)}"

    def _forceactiveauthorsupdate(self, **kwargs):
        TELEMETRY.record_usage_data()
        refresh = 'refresh' in kwargs
        if 'wait' in kwargs:
            self.data = all_author_update(refresh=refresh)
        else:
            threading.Thread(target=all_author_update, name='API-AAUPDATE', args=[refresh]).start()

    def _forcemagsearch(self, **kwargs):
        TELEMETRY.record_usage_data()
        mags = None
        if 'title' in kwargs:
            title = unquote_plus(kwargs['title'])
            db = database.DBConnection()
            try:
                bookdata = db.match('SELECT * from magazines WHERE Title=? COLLATE NOCASE', (title,))
            finally:
                db.close()
            if bookdata:
                mags = [{"bookid": bookdata['Title']}]
        backissues = 'backissues' in kwargs
        if CONFIG.use_any():
            if 'wait' in kwargs:
                search_magazines(mags, True, backissues)
            else:
                threading.Thread(target=search_magazines, name='API-SEARCHMAGS', args=[mags, True, backissues]).start()
        else:
            self.data = 'No search methods set, check config'

    def _forcersssearch(self, **kwargs):
        TELEMETRY.record_usage_data()
        if CONFIG.use_rss():
            if 'wait' in kwargs:
                search_rss_book()
            else:
                threading.Thread(target=search_rss_book, name='API-SEARCHRSS', args=[]).start()
        else:
            self.data = 'No rss feeds set, check config'

    def _forcecomicsearch(self, **kwargs):
        TELEMETRY.record_usage_data()
        if CONFIG.use_any():
            if 'wait' in kwargs:
                search_comics()
            else:
                threading.Thread(target=search_comics, name='API-SEARCHCOMICS', args=[]).start()
        else:
            self.data = 'No search methods set, check config'

    def _forcewishlistsearch(self, **kwargs):
        TELEMETRY.record_usage_data()
        if CONFIG.use_wishlist():
            if 'wait' in kwargs:
                search_wishlist()
            else:
                threading.Thread(target=search_wishlist, name='API-SEARCHWISHLIST', args=[]).start()
        else:
            self.data = 'No wishlists set, check config'

    def _forcebooksearch(self, **kwargs):
        TELEMETRY.record_usage_data()
        library = kwargs.get('type')
        if CONFIG.use_any():
            if 'wait' in kwargs:
                search_book(library=library)
            else:
                threading.Thread(target=search_book, name='API-SEARCHALLBOOK', args=[None, library]).start()
        else:
            self.data = "No search methods set, check config"

    @staticmethod
    def _forceprocess(**kwargs):
        TELEMETRY.record_usage_data()
        startdir = kwargs.get('dir')
        ignoreclient = 'ignoreclient' in kwargs
        process_dir(startdir=startdir, ignoreclient=ignoreclient)

    @staticmethod
    def _forcelibraryscan(**kwargs):
        TELEMETRY.record_usage_data()
        startdir = kwargs.get('dir')
        authid = kwargs.get('id')
        remove = 'remove' in kwargs
        if 'wait' in kwargs:
            library_scan(startdir=startdir, library='eBook', authid=authid, remove=remove)
        else:
            threading.Thread(target=library_scan, name='API-LIBRARYSCAN',
                             args=[startdir, 'eBook', authid, remove]).start()

    @staticmethod
    def _forcecomicscan(**kwargs):
        TELEMETRY.record_usage_data()
        comicid = kwargs.get('id')
        if 'wait' in kwargs:
            comic_scan(comicid=comicid)
        else:
            threading.Thread(target=comic_scan, name='API-COMICSCAN',
                             args=[comicid]).start()

    @staticmethod
    def _forceaudiobookscan(**kwargs):
        TELEMETRY.record_usage_data()
        startdir = kwargs.get('dir')
        authid = kwargs.get('id')
        remove = 'remove' in kwargs
        if 'wait' in kwargs:
            library_scan(startdir=startdir, library='AudioBook', authid=authid, remove=remove)
        else:
            threading.Thread(target=library_scan, name='API-LIBRARYSCAN',
                             args=[startdir, 'AudioBook', authid, remove]).start()

    @staticmethod
    def _forcemagazinescan(**kwargs):
        TELEMETRY.record_usage_data()
        title = kwargs.get('title')
        if 'wait' in kwargs:
            magazine_scan(title)
        else:
            threading.Thread(target=magazine_scan, name='API-MAGSCAN', args=[title]).start()

    def _deleteemptyseries(self):
        TELEMETRY.record_usage_data()
        self.data = delete_empty_series()

    def _cleancache(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'wait' in kwargs:
            self.data = clean_cache()
        else:
            threading.Thread(target=clean_cache, name='API-CLEANCACHE', args=[]).start()

    def _setworkid(self, **kwargs):
        TELEMETRY.record_usage_data()
        ids = kwargs.get('bookids')
        if 'wait' in kwargs:
            self.data = set_work_id(ids)
        else:
            threading.Thread(target=set_work_id, name='API-SETWORKID', args=[ids]).start()

    def _setallbookseries(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'wait' in kwargs:
            self.data = set_all_book_series()
        else:
            threading.Thread(target=set_all_book_series, name='API-SETALLBOOKSERIES', args=[]).start()

    def _setallbookauthors(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'wait' in kwargs:
            self.data = set_all_book_authors()
        else:
            threading.Thread(target=set_all_book_authors, name='API-SETALLBOOKAUTHORS', args=[]).start()

    def _getbookcovers(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'wait' in kwargs:
            self.data = get_book_covers()
        else:
            threading.Thread(target=get_book_covers, name='API-GETBOOKCOVERS', args=[]).start()

    def _getauthorimages(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'wait' in kwargs:
            self.data = get_author_images()
        else:
            threading.Thread(target=get_author_images, name='API-GETAUTHORIMAGES', args=[]).start()

    def _getversion(self):
        TELEMETRY.record_usage_data()
        self.data = {
            'Success': True,
            'install_type': CONFIG.get_str('INSTALL_TYPE'),
            'current_version': CONFIG.get_str('CURRENT_VERSION'),
            'latest_version': CONFIG.get_str('LATEST_VERSION'),
            'commits_behind': CONFIG.get_int('COMMITS_BEHIND'),
        }

    def _getcurrentversion(self):
        TELEMETRY.record_usage_data()
        self.data = {
            'Success': True,
            'Data': CONFIG.get_str('CURRENT_VERSION'),
            'Error': {'Code': 200, 'Message': 'OK'}
        }

    @staticmethod
    def _shutdown():
        TELEMETRY.record_usage_data()
        lazylibrarian.SIGNAL = 'shutdown'

    @staticmethod
    def _restart():
        TELEMETRY.record_usage_data()
        lazylibrarian.SIGNAL = 'restart'

    @staticmethod
    def _update():
        TELEMETRY.record_usage_data()
        lazylibrarian.SIGNAL = 'update'

    def _findauthorid(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'name' not in kwargs:
            self.data = 'Missing parameter: name'
            return
        if 'source' in kwargs:
            source = kwargs['source']
        else:
            source = CONFIG.get_str('BOOK_API')
        authorname = format_author_name(kwargs['name'], postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))

        if source in lazylibrarian.INFOSOURCES.keys():
            this_source = lazylibrarian.INFOSOURCES[source]
            api = this_source['api']
            api = api()
            res = api.find_author_id(authorname=authorname)
            self.data = str(res)

    def _findmissingauthorid(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'source' in kwargs:
            source = kwargs['source']
        else:
            source = CONFIG.get_str('BOOK_API')
        cnt = 0
        db = database.DBConnection()
        key = ''
        this_source = None
        if source in lazylibrarian.INFOSOURCES.keys():
            this_source = lazylibrarian.INFOSOURCES[source]
            key = this_source['author_key']
            if key == 'authorid':  # not all providers have authorid
                key = ''
        if not key:
            self.data = f"Invalid or disabled source [{source}]"
            return

        authordata = db.select(f"SELECT AuthorName from authors WHERE {key}='' or {key} is null")
        api = this_source['api']
        api = api()
        for author in authordata:
            res = api.find_author_id(authorname=author['AuthorName'])
            if res.get('authorid'):
                db.action(f"update authors set {key}=? where authorname=?",
                          (res.get('authorid'), author['AuthorName']))
                cnt += 1
        db.close()
        self.data = f"Updated {source} authorid for {cnt} authors"

    def _findauthor(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'name' not in kwargs:
            self.data = 'Missing parameter: name'
            return

        authorname = format_author_name(kwargs['name'], postfix=get_list(CONFIG.get_csv('NAME_POSTFIX')))
        this_source = lazylibrarian.INFOSOURCES[CONFIG['BOOK_API']]
        api = this_source['api']
        api = api()
        myqueue = Queue()
        search_api = threading.Thread(target=api.find_results,
                                      name=f"API-{this_source['src']}RESULTS",
                                      args=[f"<ll>{authorname}", myqueue])
        search_api.start()
        search_api.join()
        self.data = myqueue.get()

    def _findbook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'name' not in kwargs:
            self.data = 'Missing parameter: name'
            return

        this_source = lazylibrarian.INFOSOURCES[CONFIG['BOOK_API']]
        api = this_source['api']
        api = api()
        myqueue = Queue()
        search_api = threading.Thread(target=api.find_results,
                                      name=f"API-{this_source['src']}RESULTS",
                                      args=[f"{kwargs['name']}<ll>", myqueue])
        search_api.start()
        search_api.join()
        self.data = myqueue.get()

    def _addbookbyisbn(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'isbn' not in kwargs:
            self.data = 'Missing parameter: isbn'
            return
        pass_kwargs = dict(kwargs)
        pass_kwargs.pop('isbn', None)
        summary = ''
        for item in get_list(kwargs['isbn']):
            self._addonebookbyisbn(isbn=item, **pass_kwargs)
            summary += self.data + '<br>'
        self.data = summary

    def _addonebookbyisbn(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'isbn' not in kwargs:
            self.data = 'Missing parameter: isbn'
            return
        if not is_valid_isbn(kwargs['isbn']):
            self.data = f"Invalid isbn {kwargs['isbn']}"
            return

        searchresults = search_for(kwargs['isbn'])
        self.data = f"No results for {kwargs['isbn']}"
        if searchresults:
            sortedlist = sorted(searchresults, key=lambda x: (x['highest_fuzz'], x['bookrate_count']),
                                reverse=True)
            if sortedlist[0].get('bookid'):
                self._addonebook(id=sortedlist[0].get('bookid'), **kwargs)
                self.data = f"Added {kwargs['isbn']}:{sortedlist[0].get('authorname')}:{sortedlist[0].get('bookname')}"

    def _addbook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        pass_kwargs = dict(kwargs)
        pass_kwargs.pop('id', None)
        for item in get_list(kwargs['id']):
            self._addonebook(id=item, **pass_kwargs)

    def _addonebook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        this_source = lazylibrarian.INFOSOURCES[CONFIG['BOOK_API']]
        api = this_source['api']
        api = api()
        if 'wait' in kwargs:
            self.data = api.add_bookid_to_db(kwargs['id'], None, None, "Added by API")
        else:
            threading.Thread(target=api.add_bookid_to_db,
                             name=f"API-{this_source['src']}RESULTS",
                             args=[kwargs['id'], None, None, "Added by API"]).start()

    def _movebook(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['id', 'toid']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        db = database.DBConnection()
        try:
            authordata = db.match('SELECT AuthorName from authors WHERE AuthorID=?', (kwargs['toid'],))
            if not authordata:
                self.data = f"No destination author [{kwargs['toid']}] in the database"
            else:
                bookdata = db.match('SELECT AuthorID, BookName from books where BookID=?', (kwargs['id'],))
                if not bookdata:
                    self.data = f"No bookid [{kwargs['id']}] in the database"
                else:
                    control_value_dict = {'BookID': kwargs['id']}
                    new_value_dict = {'AuthorID': kwargs['toid']}
                    db.upsert("books", new_value_dict, control_value_dict)
                    update_totals(bookdata[0])  # we moved from here
                    update_totals(kwargs['toid'])  # to here
                    self.data = f"Moved book [{bookdata[1]}] to [{authordata[0]}]"
            db.close()
            self.logger.debug(self.data)
        except Exception as e:
            db.close()
            self.data = f"{type(e).__name__} {str(e)}"

    def _movebooks(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['fromname', 'toname']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return

        db = database.DBConnection()
        try:
            q = 'SELECT bookid,books.authorid from books,authors where books.AuthorID = authors.AuthorID'
            q += ' and authorname=?'
            fromhere = db.select(q, (kwargs['fromname'],))

            tohere = db.match('SELECT authorid from authors where authorname=?', (kwargs['toname'],))
            if not len(fromhere):
                self.data = f"No books by [{kwargs['fromname']}] in the database"
            else:
                if not tohere:
                    self.data = f"No destination author [{kwargs['toname']}] in the database"
                else:
                    db.action('UPDATE books SET authorid=?, where authorname=?', (tohere[0], kwargs['fromname']))
                    self.data = f"Moved {len(fromhere)} books from {kwargs['fromname']} to {kwargs['toname']}"
                    update_totals(fromhere[0][1])  # we moved from here
                    update_totals(tohere[0])  # to here
            db.close()
            self.logger.debug(self.data)
        except Exception as e:
            db.close()
            self.data = f"{type(e).__name__} {str(e)}"

    def _comicmeta(self, **kwargs):
        TELEMETRY.record_usage_data()
        name = kwargs.get('name')
        if not name:
            self.data = 'Missing parameter: name'
            return
        xml = 'xml' in kwargs
        self.data = comic_metadata(name, xml=xml)

    def _comicid(self, **kwargs):
        TELEMETRY.record_usage_data()
        name = kwargs.get('name')
        if not name:
            self.data = 'Missing parameter: name'
            return
        source = kwargs.get('source')
        if source not in ['cv', 'cx']:
            self.data = 'Invalid parameter: source'
            return
        best = 'best' in kwargs
        if source == 'cv':
            self.data = cv_identify(name, best=best)
        else:
            self.data = cx_identify(name, best=best)

    def _addauthor(self, **kwargs):
        TELEMETRY.record_usage_data()
        name = kwargs.get('name')
        if not name:
            self.data = 'Missing parameter: name'
            return
        books = bool(kwargs.get('books'))
        try:
            self.data = add_author_name_to_db(author=name, refresh=False, addbooks=books,
                                              reason=f"API add_author {name}")
        except Exception as e:
            self.data = f"{type(e).__name__} {str(e)}"

    def _addauthorid(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
            return
        books = bool(kwargs.get('books'))
        try:
            self.data = add_author_to_db(refresh=False, authorid=self.id, addbooks=books,
                                         reason=f"API add_author_id {self.id}")
        except Exception as e:
            self.data = f"{type(e).__name__} {str(e)}"

    def _grfollowall(self):
        TELEMETRY.record_usage_data()
        db = database.DBConnection()
        try:
            cmd = ("SELECT AuthorName,AuthorID,GRfollow FROM authors where Status='Active' "
                   "or Status='Wanted' or Status='Loading'")
            authors = db.select(cmd)
            count = 0
            for author in authors:
                followid = check_int(author['GRfollow'], 0)
                if followid > 0:
                    self.logger.debug(f"{author['AuthorName']} is already followed")
                elif author['GRfollow'] == "0":
                    self.logger.debug(f"{author['AuthorName']} is manually unfollowed")
                else:
                    res = grfollow(author['AuthorID'], True)
                    if res.startswith('Unable'):
                        self.logger.warning(res)
                    try:
                        followid = res.split("followid=")[1]
                        self.logger.debug(f"{author['AuthorName']} marked followed")
                        count += 1
                    except IndexError:
                        followid = ''
                    db.action('UPDATE authors SET GRfollow=? WHERE AuthorID=?', (followid, author['AuthorID']))
            db.close()
            self.data = f"Added follow to {count} {plural(count, 'author')}"
        except Exception as e:
            db.close()
            self.data = f"{type(e).__name__} {str(e)}"

    def _hcsync(self, **kwargs):
        TELEMETRY.record_usage_data()
        library = kwargs.get('library', '')

        # If no user specified and HC_SYNC is enabled, sync all users with tokens
        if not kwargs.get('user') and CONFIG.get_bool('HC_SYNC'):
            try:
                # This will sync all users
                threading.Thread(target=hc_sync, name='API-HCSYNC', args=[library, None]).start()
            except Exception as e:
                self.data = f"{type(e).__name__} {str(e)}"
        if kwargs.get('user'):
            # If specific user requested, sync just that user
            try:
                threading.Thread(target=hc_sync, name='API-HCSYNC', args=[library, kwargs.get('user')]).start()
            except Exception as e:
                self.data = f"{type(e).__name__} {str(e)}"
        else:
            self.data = "HC_SYNC is disabled and no specific user requested"

    def _grsync(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['shelf', 'status']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        library = kwargs.get('library', 'eBook')
        reset = 'reset' in kwargs
        try:
            threading.Thread(target=grsync, name='API-GRSYNC', args=[kwargs['status'],
                                                                     kwargs['shelf'], library, reset]).start()
        except Exception as e:
            self.data = f"{type(e).__name__} {str(e)}"

    def _grfollow(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        try:
            self.data = grfollow(authorid=kwargs['id'], follow=True)
        except Exception as e:
            self.data = f"{type(e).__name__} {str(e)}"

    def _grunfollow(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        try:
            self.data = grfollow(authorid=kwargs['id'], follow=False)
        except Exception as e:
            self.data = f"{type(e).__name__} {str(e)}"

    def _searchitem(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'item' not in kwargs:
            self.data = 'Missing parameter: item'
            return
        self.data = search_item(kwargs['item'])

    def _searchbook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        books = [{"bookid": kwargs['id']}]
        library = kwargs.get('type')
        if CONFIG.use_any():
            if 'wait' in kwargs:
                search_book(books=books, library=library)
            else:
                threading.Thread(target=search_book, name='API-SEARCHBOOK', args=[books, library]).start()
        else:
            self.data = "No search methods set, check config"

    def _removeauthor(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
            return
        db = database.DBConnection()
        try:
            authorsearch = db.select('SELECT AuthorName from authors WHERE AuthorID=?', (kwargs['id'],))
            if len(authorsearch):  # to stop error if try to remove an author while they are still loading
                author_name = authorsearch[0]['AuthorName']
                self.logger.debug(f"Removing all references to author: {author_name}")
                db.action('DELETE from authors WHERE AuthorID=?', (kwargs['id'],))
        finally:
            db.close()

    def _writecfg(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['name', 'value', 'group']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        try:
            item = CONFIG.get_item(kwargs['name'])
            if item:
                item.set_from_ui(kwargs['value'])
            CONFIG.save_config_and_backup_old(save_all=False, section=kwargs['group'])
        except Exception as e:
            self.data = f"Unable to update CFG entry for {kwargs['group']}: {kwargs['name']}, {str(e)}"

    def _readcfg(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['name', 'group']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        try:
            self.data = f"[{CONFIG.get_str(kwargs['name'])}]"
        except configparser.Error:
            self.data = f"No config entry for {kwargs['group']}: {kwargs['name']}"

    @staticmethod
    def _loadcfg():
        TELEMETRY.record_usage_data()
        # No need to reload the config

    def _getseriesauthors(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            count = get_series_authors(self.id)
            self.data = f"Added {count}"

    def _addseriesmembers(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        self.refresh = 'refresh' in kwargs
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self.data = add_series_members(self.id, self.refresh)

    def _getseriesmembers(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id', '')
        self.name = kwargs.get('name', '')
        self.refresh = 'refresh' in kwargs
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self.data = get_series_members(self.id, self.name, self.refresh)

    def _getbookauthors(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self.data = get_book_authors(self.id)

    def _getworkseries(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        if not kwargs.get('source'):
            self.data = 'Missing parameter: source'
        else:
            self.data = get_work_series(self.id, kwargs.get('source'), reason="API get_work_series")

    def _getbookcover(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            if 'src' in kwargs:
                self.data = get_book_cover(self.id, kwargs['src'])
            else:
                self.data = get_book_cover(self.id)

    def _getauthorimage(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
            return
        refresh = 'refresh' in kwargs
        max_num = kwargs.get('max', 1)
        self.data = get_author_image(self.id, refresh=refresh, max_num=max_num)

    def _lock(self, table, itemid, state):
        TELEMETRY.record_usage_data()
        db = database.DBConnection()
        try:
            dbentry = db.match(f'SELECT {table}ID from {table}s WHERE {table}ID={itemid}')
            if dbentry:
                db.action(f"UPDATE {table}s SET Manual='{state}' WHERE {table}ID={itemid}")
            else:
                self.data = f"{table}ID {itemid} not found"
        finally:
            db.close()

    def _setauthorlock(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self._lock("author", kwargs['id'], "1")

    def _setauthorunlock(self, **kwargs):
        TELEMETRY.record_usage_data()
        self.id = kwargs.get('id')
        if not self.id:
            self.data = 'Missing parameter: id'
        else:
            self._lock("author", kwargs['id'], "0")

    def _setauthorimage(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['id', 'img']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        self._setimage("author", kwargs['id'], kwargs['img'])

    def _setbookimage(self, **kwargs):
        TELEMETRY.record_usage_data()
        for item in ['id', 'img']:
            if item not in kwargs:
                self.data = f"Missing parameter: {item}"
                return
        self._setimage("book", kwargs['id'], kwargs['img'])

    def _setimage(self, table, itemid, img):
        TELEMETRY.record_usage_data()
        msg = f"{table} Image [{img}] rejected"
        # Cache file image
        if path_isfile(img):
            extn = splitext(img)[1].lower()
            if extn and extn in ['.jpg', '.jpeg', '.png']:
                destfile = os.path.join(DIRS.CACHEDIR, table, f"{itemid}.jpg")
                try:
                    shutil.copy(img, destfile)
                    setperm(destfile)
                    msg = ''
                except Exception as why:
                    msg += f" Failed to copy file: {type(why).__name__} {str(why)}"
            else:
                msg += " invalid extension"

        if img.startswith('http'):
            # cache image from url
            extn = splitext(img)[1].lower()
            if extn and extn in ['.jpg', '.jpeg', '.png']:
                _, success, _ = cache_img(ImageType(table), itemid, img)
                if success:
                    msg = ''
                else:
                    msg += " Failed to cache file"
            else:
                msg += " invalid extension"
        elif msg:
            msg += " Not found"

        if msg:
            self.data = msg
            return

        db = database.DBConnection()
        try:
            dbentry = db.match(f"SELECT {table}ID from {table}s WHERE {table}ID={itemid}")
            if dbentry:
                subcache = 'cache' + os.path.sep + itemid + '.jpg'
                db.action(f"UPDATE {table}s SET {table}Img='{subcache}' WHERE {table}ID={itemid}")
            else:
                self.data = f"{table}ID {itemid} not found"
        finally:
            db.close()

    def _setbooklock(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        self._lock("book", kwargs['id'], "1")

    def _deduplicate(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        if 'wait' in kwargs:
            de_duplicate(kwargs['id'])
            self.data = 'Completed. See log for details'
        else:
            threading.Thread(target=de_duplicate, name=f"API-DEDUPLICATE_{kwargs['id']}",
                             args=[kwargs['id']]).start()

    def _setbookunlock(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        self._lock("book", kwargs['id'], "0")

    @staticmethod
    def _restartjobs():
        TELEMETRY.record_usage_data()
        restart_jobs(command=SchedulerCommand.RESTART)

    @staticmethod
    def _checkrunningjobs():
        TELEMETRY.record_usage_data()
        check_running_jobs()

    def _showjobs(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'json' in kwargs:
            self.data = show_jobs(json=True)
        else:
            self.data = show_jobs()

    def _showstats(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'json' in kwargs:
            self.data = show_stats(json=True)
        else:
            self.data = show_stats()

    def _importalternate(self, **kwargs):
        TELEMETRY.record_usage_data()
        usedir = kwargs.get('dir', CONFIG.get_str('ALTERNATE_DIR'))
        library = kwargs.get('library', 'eBook')
        if 'wait' in kwargs:
            self.data = process_alternate(usedir, library)
        else:
            threading.Thread(target=process_alternate, name='API-IMPORTALT', args=[usedir, library]).start()

    def _includealternate(self, **kwargs):
        TELEMETRY.record_usage_data()
        startdir = kwargs.get('dir', CONFIG.get_str('ALTERNATE_DIR'))
        library = kwargs.get('library', 'eBook')
        if 'wait' in kwargs:
            self.data = library_scan(startdir, library, None, False)
        else:
            threading.Thread(target=library_scan, name='API-INCLUDEALT',
                             args=[startdir, library, None, False]).start()

    def _importcsvwishlist(self, **kwargs):
        TELEMETRY.record_usage_data()
        usedir = kwargs.get('dir', CONFIG.get_str('ALTERNATE_DIR'))
        status = kwargs.get('status', 'Wanted')
        library = kwargs.get('library', 'eBook')
        if 'wait' in kwargs:
            self.data = import_csv(usedir, status, library)
        else:
            threading.Thread(target=import_csv, name='API-IMPORTCSV', args=[usedir, status, library]).start()

    def _exportcsvwishlist(self, **kwargs):
        TELEMETRY.record_usage_data()
        usedir = kwargs.get('dir', CONFIG.get_str('ALTERNATE_DIR'))
        status = kwargs.get('status', 'Wanted')
        library = kwargs.get('library', 'eBook')
        if 'wait' in kwargs:
            self.data = export_csv(usedir, status, library)
        else:
            threading.Thread(target=export_csv, name='API-EXPORTCSV', args=[usedir, status, library]).start()

    def _telemetryshow(self):
        TELEMETRY.record_usage_data()
        self.data = TELEMETRY.get_data_for_ui_preview(send_usage=True, send_config=True)

    def _telemetrysend(self):
        TELEMETRY.record_usage_data()
        self.data = telemetry_send()

    def deletefromcalibre(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return False
        rc = delete_from_calibre(kwargs['id'])
        self.data = f"Delete result: {rc}"
        return True

    def _sendmagtocalibre(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'title' not in kwargs:
            self.data = 'Missing parameter: title'
            return

        title = unquote_plus(kwargs['title'])
        title = title.replace('&amp;', '&')
        db = database.DBConnection()
        dbentry = db.select("SELECT issueid from issues WHERE Title=?", (title,))
        db.close()
        if not dbentry:
            self.data = f"Magazine {title} not found in database"
            return
        cnt = 0
        for issue in dbentry:
            if self._sendmagissuetocalibre(id=issue['issueid']):
                cnt += 1
        self.data = f"Sent {cnt} issues of {title} to calibre"

    def _sendmagissuetocalibre(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return False

        db = database.DBConnection()
        dbentry = db.match("SELECT * from issues WHERE IssueID=?", (kwargs['id'],))
        db.close()
        if not dbentry:
            self.data = f"IssueID {kwargs['id']} not found in database"
            return False

        data = dict(dbentry)
        if not data['IssueFile'] or not path_isfile(data['IssueFile']):
            self.data = f"IssueID {kwargs['id']} IssueFile {data['IssueFile']} not found"
            return False

        res, filename, pp_path = send_mag_issue_to_calibre(data)
        self.data = f"{res}: {filename}: {pp_path}"
        return res is not False

    def _sendcomictocalibre(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'title' not in kwargs:
            self.data = 'Missing parameter: title'
            return

        title = unquote_plus(kwargs['title'])
        title = title.replace('&amp;', '&')
        db = database.DBConnection()
        dbentry = db.select("select title,comics.comicid,issueid from comics,comicissues "
                            "where comics.comicid = comicissues.comicid and Title=?", (title,))
        db.close()
        if not dbentry:
            self.data = f"Magazine {title} not found in database"
            return
        for issue in dbentry:
            self._sendcomicissuetocalibre(id=f"{issue['ComicID']}_{issue['issueid']}")
        self.data = f"Sent {len(dbentry)} issues of {title} to calibre"

    def _sendcomicissuetocalibre(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return False
        if '_' not in kwargs['id']:
            self.data = 'Invalid parameter: id'
            return False

        comicid, issueid = kwargs['id'].split('_')
        db = database.DBConnection()
        dbentry = db.match("SELECT * from comicissues,comics WHERE comics.comicid=comicissues.comicid "
                           "and comics.comicid=? and IssueID=?", (comicid, issueid))
        db.close()
        if not dbentry:
            self.data = f"ComicID_IssueID {kwargs['id']} not found in database"
            return False

        data = dict(dbentry)
        if not data['IssueFile'] or not path_isfile(data['IssueFile']):
            self.data = f"IssueID {kwargs['id']} IssueFile {data['IssueFile']} not found"
            return False

        res, filename, pp_path = send_comic_issue_to_calibre(data)
        self.data = f"{res}: {filename}: {pp_path}"
        return res is not False

    def _sendebooktocalibre(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return False

        db = database.DBConnection()
        dbentry = db.match("SELECT * from books WHERE BookID=?", (kwargs['id'],))
        db.close()
        if not dbentry:
            self.data = f"IssueID {kwargs['id']} not found in database"
            return False

        data = dict(dbentry)
        if not data['IssueFile'] or not path_isfile(data['IssueFile']):
            self.data = f"Book {kwargs['id']} BookFile {data['BookFile']} not found"
            return False

        res, filename, pp_path = send_ebook_to_calibre(data)
        self.data = f"{res}: {filename}: {pp_path}"
        return res is not False

    def _updatebook(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return

        updated = []
        missed = []
        db = database.DBConnection()
        dbentry = db.match("SELECT * from books WHERE BookID=?", (kwargs['id'],))
        if not dbentry:
            self.data = f"BookID {kwargs['id']} not found in database"
            db.close()
            return
        for item in ['BookName', 'BookSub', 'BookAdded', 'BookDate', 'BookLang', 'BookISBN',
                     'BookDesc', 'BookPub', 'OriginalPubDate', 'hc_id', 'ol_id', 'gr_id', 'gb_id']:
            if item in kwargs:
                db.action(f"UPDATE Books SET {item}=? WHERE BookID=?", (kwargs[item], kwargs['id']))
                updated.append(item)
            else:
                missed.append(item)
        db.close()
        self.data = f"Updated: {','.join(updated)} Missed: {','.join(missed)}"

    def _splitauthornames(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'names' not in kwargs:
            self.data = 'Missing parameter: names'
            return
        self.data = split_author_names(kwargs['names'], get_list(CONFIG['MULTI_AUTHOR_SPLIT']))

    def _getauthorsfrombookfiles(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'wait' in kwargs:
            self.data = get_authors_from_book_files()
        else:
            threading.Thread(target=get_authors_from_book_files, name='API-AUTH_FROM_BOOK').start()

    def _getauthorsfromhc(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'wait' in kwargs:
            self.data = get_authors_from_hc()
        else:
            threading.Thread(target=get_authors_from_hc, name='API-AUTH_FROM_HC').start()

    def _getauthorsfromol(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'wait' in kwargs:
            self.data = get_authors_from_ol()
        else:
            threading.Thread(target=get_authors_from_ol, name='API-AUTH_FROM_OL').start()

    def _getpdftags(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        db = database.DBConnection()
        dbentry = db.match("SELECT IssueFile from issues WHERE IssueID=?", (kwargs['id'],))
        if not dbentry:
            self.data = f"IssueID {kwargs['id']} not found in database"
            db.close()
            return
        issuefile = dbentry['IssueFile']
        db.close()
        if not path_isfile(issuefile):
            self.data = f"Unable to read source file for IssueID {kwargs['id']}"
            return
        self.data = read_pdf_tags(issuefile)

    def _setpdftags(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        db = database.DBConnection()
        dbentry = db.match("SELECT IssueFile,Title,IssueDate from issues WHERE IssueID=?", (kwargs['id'],))
        if not dbentry:
            self.data = f"IssueID {kwargs['id']} not found in database"
            db.close()
            return
        issuefile = dbentry['IssueFile']
        title = dbentry['Title']
        issuedate = dbentry['IssueDate']
        db.close()
        if 'tags' not in kwargs:
            tags = {}
        else:
            try:
                tags = eval(kwargs['tags'])
            except SyntaxError:
                tags = None
            if not isinstance(tags, dict):
                self.data = "Invalid tags dictionary"
                return
        if not path_isfile(issuefile):
            self.data = f"Unable to read source file for IssueID {kwargs['id']}"
            return
        self.data = write_pdf_tags(issuefile, title, issuedate, tags)

    def _listsecondaries(self):
        TELEMETRY.record_usage_data()
        db = database.DBConnection()
        res = db.select('select distinct authorid from bookauthors except select authorid from books')
        self.data = {}
        for item in res:
            auth = db.match('select authorname from authors where authorid=?', (item['authorid'], ))
            if auth:
                self.data[item['authorid']] = auth['authorname']

    def _deletesecondaries(self):
        TELEMETRY.record_usage_data()
        db = database.DBConnection()
        res = db.select('select distinct authorid from bookauthors except select authorid from books')
        cnt = 0
        for item in res:
            cnt += 1
            auth = db.match('select authorname from authors where authorid=?', (item['authorid'], ))
            self.logger.debug(f"Deleting {item['authorid']}: {auth['authorname']}")
            db.action('delete from authors where authorid=?', (item['authorid'], ))
        db.action('vacuum')
        self.data = f"Removed {cnt} secondary authors"

    def _cleanmaglibrary(self):
        TELEMETRY.record_usage_data()
        mags, issues = clean_maglibrary()
        self.data = f"Removed {issues} {plural(issues, 'issue')}, {mags} {plural(mags, 'title')}"

    def _isbnwords(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'words' not in kwargs:
            self.data = 'Missing parameter: words'
            return
        self.data = isbn_from_words(kwargs['words'])

    def _getauthorinfo(self, **kwargs):
        TELEMETRY.record_usage_data()
        if 'id' not in kwargs:
            self.data = 'Missing parameter: id'
            return
        res = get_all_author_details(kwargs['id'], kwargs.get('name'))
        self.data = str(res)
