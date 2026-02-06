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

# Purpose:
#   Hold all the global variables needed/used by LL
#   Hold a few basic routines used widely, until they can be moved out


import logging
import os
import sys
import threading

# from lazylibrarian import config2
from lazylibrarian.filesystem import syspath

# Transient globals NOT stored in config
# These are used/modified by LazyLibrarian.py before config.ini is read
DAEMON = False  # True if running as a daemon
SIGNAL = None  # Signals global state of LL to threads/scheduler. 'restart', 'update', 'shutdown' or ''/None
PIDFILE = ''  # If running as a daemon, the name of the file holding the PID
SYS_ENCODING = ''  # A copy of CONFIG['SYS_ENCODING'] that can be overridden
LOGINUSER = None  # UserID of currently logged-in user, if any
COMMIT_LIST = ''  # List of git commits since last update. If it includes "**MANUAL**", don't update.
SHOWLOGOUT = 1  # If 1, the Logout option is shown in the UI.
REQUESTSLOG = 0  # If 1, sets http.client.HTTPConnection.debuglevel=1.
DOCKER = False  # Set to True if we discover LL is running inside of Docker
STOPTHREADS = False  # Part of the scheduling state machine. Should move to a scheduling class?

# These are globals
UPDATE_MSG = ''
INFOSOURCES = {}
TIMERS = {
            'NO_TOR_MSG': 0,
            'NO_RSS_MSG': 0,
            'NO_NZB_MSG': 0,
            'NO_CV_MSG': 0,
            'NO_DIRECT_MSG': 0,
            'NO_IRC_MSG': 0,
            'LAST_GR': 0,
            'LAST_LT': 0,
            'LAST_CV': 0,
            'LAST_BOK': 0,
            'BOK_TODAY': 0,
            'ANNA_REMAINING': 25,
            'SLEEP_GR': 0.0,
            'SLEEP_LT': 0.0,
            'SLEEP_CV': 0.0,
            'SLEEP_BOK': 0.0,
            'LAST_HC': 0,
            'SLEEP_HC': 0.0,
        }
IGNORED_AUTHORS = 0
PRIMARY_AUTHORS = 1
CACHE_HIT = 0
CACHE_MISS = 0
IRC_CACHE_EXPIRY = 2 * 3600
MONTHNAMES = []
SEASONS = []
BOOKSTRAP_THEMELIST = []
USER_BLOCKLIST = []
MAG_UPDATE = 0
EBOOK_UPDATE = 0
AUDIO_UPDATE = 0
COMIC_UPDATE = 0
SERIES_UPDATE = 0
AUTHORS_UPDATE = 0
SEARCHING = 0
LOGIN_MSG = ''
HIST_REFRESH = 1000
MARK_ISSUES = False
DICTS = {}
GITLAB_TOKEN = 'gitlab+deploy-token-26212:Hbo3d8rfZmSx4hL1Fdms@gitlab.com'
GRGENRES = {}
GC_BEFORE = {}
GC_AFTER = {}
UNRARLIB = 0
RARFILE = None
FFMPEGVER = ''
FFMPEGAAC = ''
SAB_VER = (0, 0, 0)
NEWUSER_MSG = ''
NEWFILE_MSG = ''
libraryscan_data = ''
magazinescan_data = ''
test_data = ''

# user permissions
perm_config = 1 << 0  # 1 access to config page
perm_logs = 1 << 1  # 2 access to logs
perm_history = 1 << 2  # 4 access to history
perm_managebooks = 1 << 3  # 8 access to manage page
perm_magazines = 1 << 4  # 16 access to magazines/issues/pastissues
perm_audio = 1 << 5  # 32 access to audiobooks page
perm_ebook = 1 << 6  # 64 can access ebooks page
perm_series = 1 << 7  # 128 access to series/seriesmembers
perm_edit = 1 << 8  # 256 can edit book or author details
perm_search = 1 << 9  # 512 can search goodreads/googlebooks for books/authors
perm_status = 1 << 10  # 1024 can change book status (wanted/skipped etc)
perm_force = 1 << 11  # 2048 can use background tasks (refresh authors/libraryscan/postprocess/searchtasks)
perm_download = 1 << 12  # 4096 can download existing books/mags
perm_comics = 1 << 13  # 8192 access to comics

perm_authorbooks = perm_audio + perm_ebook
perm_guest = perm_download + perm_series + perm_authorbooks + perm_magazines + perm_comics
perm_friend = perm_guest + perm_search + perm_status
perm_admin = 65535

# user prefs
pref_myauthors = 1 << 0
pref_myseries = 1 << 1
pref_mymags = 1 << 2
pref_mycomics = 1 << 3
pref_myfeeds = 1 << 4
pref_myafeeds = 1 << 5

# Shared dictionaries
isbn_979_dict = {
    "10": "fre",
    "11": "kor",
    "12": "ita"
}
isbn_978_dict = {
    "0": "eng",
    "1": "eng",
    "2": "fre",
    "3": "ger",
    "4": "jap",
    "5": "rus",
    "7": "chi",
    "80": "cze",
    "82": "nor",
    "83": "pol",
    "84": "spa",
    "85": "bra",
    "87": "den",
    "88": "ita",
    "89": "kor",
    "91": "swe",
    "93": "ind"
}

ROLE = {
    "UNKNOWN": 0,
    "PRIMARY": 1,
    "WRITER": 1,
    "CREATOR": 1,
    "CONTRIBUTING": 2,
    "ILLUSTRATOR": 3,
    "NARRATOR": 4,
    "EDITOR": 5,
}


def daemonize():
    """
    Fork off as a daemon
    """
    # active_count in python 3.9 but camelCase name still supported
    logger = logging.getLogger(__name__)
    if 'activeCount' in dir(threading):
        # noinspection PyDeprecation
        threadcount = threading.activeCount()
    else:
        threadcount = threading.active_count()
    if threadcount != 1:
        logger.warning(f'There are {threadcount} active threads. Daemonizing may cause strange behavior.')

    sys.stdout.flush()
    sys.stderr.flush()

    # Make a non-session-leader child process
    try:
        pid = os.fork()  # @UndefinedVariable - only available in UNIX
        if pid != 0:
            sys.exit(0)
    except OSError as e:
        raise RuntimeError(f"1st fork failed: {e.strerror} [{e.errno}]") from e

    os.setsid()  # @UndefinedVariable - only available in UNIX

    # Make sure I can read my own files and shut out others
    prev = os.umask(0)
    os.umask(prev and int('077', 8))

    # Make the child a session-leader by detaching from the terminal
    try:
        pid = os.fork()  # @UndefinedVariable - only available in UNIX
        if pid != 0:
            sys.exit(0)
    except OSError as e:
        raise RuntimeError(f"2nd fork failed: {e.strerror} [{e.errno}]") from e

    with open('/dev/null') as dev_null:
        os.dup2(dev_null.fileno(), sys.stdin.fileno())

    with open('/dev/null') as si, open('/dev/null', "a+") as so, open('/dev/null', "a+") as se:
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

    pid = os.getpid()
    logger.debug(f"Daemonized to PID {pid}")

    if PIDFILE:
        logger.debug(f"Writing PID {pid} to {PIDFILE}")
        with open(syspath(PIDFILE), 'w') as pidfile:
            pidfile.write(f"{pid}\n")
