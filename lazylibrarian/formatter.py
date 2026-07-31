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


# import chardet
import datetime
import logging
import os
import re
import threading
import unicodedata
from functools import wraps
from hashlib import md5
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

import lazylibrarian
from lazylibrarian.configenums import OnChangeReason


class ImportPrefs:
    LANG_LIST = []
    SPLIT_LIST = []

    @classmethod
    def contrib_changed(cls, value: str, reason: OnChangeReason = OnChangeReason.SETTING):
        """ Called automatically when CONFIG[CONTRIBUTING_AUTHORS] changes value """
        logger = logging.getLogger(__name__)
        if value != '1':
            db = lazylibrarian.database.DBConnection()
            res = db.select('select distinct authorid from bookauthors except select authorid from books')
            cnt = 0
            for item in res:
                cnt += 1
                db.action('delete from authors where authorid=?', (item['authorid'], ))
            logger.debug(f"Disabled Contributing Authors: Removed {cnt} authors")

    @classmethod
    def lang_changed(cls, languages: str, reason: OnChangeReason = OnChangeReason.SETTING):
        """ Called automatically when CONFIG[IMP_PREFLANG] changes value """
        cls.LANG_LIST = get_list(languages, ',')

    @classmethod
    def nosplit_changed(cls, nosplits: str, reason: OnChangeReason = OnChangeReason.SETTING):
        """ Called automatically when CONFIG[IMP_NOSPLIT] changes value """
        cls.SPLIT_LIST = get_list(nosplits, ',')


def thread_name(name=None) -> str:
    if name:
        threading.current_thread().name = name
        return name
    return threading.current_thread().name


def restore_thread_name(thread_prefix, restore_to='WEBSERVER'):
    """
    Decorator to restore thread name on function exit.

    Used for functions that run in spawned threads (IMPORTISSUES, IMPORTALT, etc.)
    which need to reset their thread name before the thread terminates.

    This decorator ensures the thread name is always restored, even if the
    function returns early or raises an exception.

    Args:
        thread_prefix: Prefix to check for in thread name (e.g., 'IMPORTISSUES', 'IMPORTALT')
        restore_to: Thread name to restore to (default: 'WEBSERVER')

    Example:
        @restore_thread_name('IMPORTISSUES')  # Restores to 'WEBSERVER' (default)
        def process_issues(source_dir, title):
            if not source_dir:
                return False  # Thread name automatically restored to 'WEBSERVER'
            # ... processing ...
            return True

        @restore_thread_name('CUSTOMTASK', restore_to='SCHEDULER')  # Restores to 'SCHEDULER'
        def custom_task():
            # ... processing ...
            return  # Thread name automatically restored to 'SCHEDULER'
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            finally:
                # Restore thread name if this is a spawned thread with our prefix
                if thread_prefix in threading.current_thread().name:
                    threading.current_thread().name = restore_to
        return wrapper
    return decorator


def split_author_names(namelist, splitlist):
    split_words = []
    # split on " and " or " & " or ", " etc.
    for item in splitlist:
        split_words.append(f' {item} ')
    split_words.append(', ')
    split_words.append(';')
    split_words.append(' & ')
    if isinstance(namelist, str):
        namelist = [namelist]

    authornames = []
    for entry in namelist:
        for token in split_words:
            entry = entry.replace(token, '|')
        names = entry.split('|')
        for name in names:
            name = name.strip()
            if ' ' not in name:
                # single word on its own isn't an authorname.
                # Something like Robert & Gilles Néret Descharnes
                # where we just have Robert
                # or L.E. Modesitt, Jnr
                # where we just have Jnr
                # we should probably join them back up, but ignore for now
                # don't know whether to join to preceding or following part
                # location = names.index(name)
                continue
            if name not in authornames:
                name, _ = lazylibrarian.importer.get_preferred_author(name)
                if name not in authornames:
                    authornames.append(name)
    return authornames


def sanitize(name, is_folder_or_file=False):
    """
    Sanitizes a string so it can be used as a file name or foldername, normalized as Unicode
    Returns a sanitized string
    """
    if not name:
        return ''
    filename = make_unicode(name)
    # replace non-ascii quotes with regular ones
    filename = replace_all(filename, lazylibrarian.DICTS.get('apostrophe_dict', {}))
    # strip characters we don't want in a filename/foldername
    dic = lazylibrarian.DICTS.get('filename_dict', {}).copy()
    if is_folder_or_file and os.path.__name__ == 'ntpath':
        dic.pop(':')
    filename = replace_all(filename, dic)
    # Remove all characters below code point 32
    filename = "".join(c for c in filename if ord(c) > 31)
    filename = unicodedata.normalize('NFC', filename)
    # windows filenames can't end in space or dot
    while filename and filename[-1] in '. ':
        filename = filename[:-1]
    return filename


def versiontuple(versionstring):
    """
    Convert a version string into 3 part tuple without using packaging.version.parse
    as we can't be sure that's installed
    Note this assumes bugfix component starts with a digit eg 1.2.3-beta4
    and drops any extension part, eg 1.2.3-beta4 becomes (1, 2, 3)
    which is not strictly correct so we need to check for version > too_low
    rather than >= lowest acceptable
    """
    major = 0
    minor = 0
    bugfix = 0
    parts = versionstring.split('.', 2)
    nparts = len(parts)
    if nparts:
        major = check_int(parts[0], 0)
    if nparts > 1:
        minor = check_int(parts[1], 0)
    if nparts > 2:
        part = ''
        for i in parts[2]:
            if i.isdigit():
                part += i
            else:
                break
        bugfix = check_int(part, 0)
    return major, minor, bugfix


def url_fix(s, charset='utf-8'):
    """
    Return the argument so it's valid in a web context
    """
    if not isinstance(s, str):
        s = s.decode(charset)
    scheme, netloc, path, qs, anchor = urlsplit(s)
    path = quote(path, '/%')
    qs = quote_plus(qs, ':&=')
    return urlunsplit((scheme, netloc, path, qs, anchor))


# Query parameters that carry a credential of some sort. Private trackers and
# indexers put passkeys and api keys straight in the download url, so a url
# can't be logged as-is.
_DEFAULT_CREDENTIAL_PARAMS = (
    'apikey', 'api_key', 'auth', 'authkey', 'key', 'passkey', 'passwd',
    'password', 'rss_key', 'rsskey', 'secret', 'session', 'sessionid',
    'token', 'torrent_pass',
)

# A credential can turn up percent encoded inside another url, which is how a
# private tracker's announce url reaches us in a magnet's tr= parameter, so
# match an encoded separator as well as a literal one. Anything up to the next
# separator goes, and over-redacting a log line is the safe way to be wrong.
USERINFO_RE = re.compile(r'(?<=//)[^/@\s]+@')

_credential_re_lock = threading.Lock()
_credential_re_cache = {'params': None, 'regex': None}

_DEFAULT_CREDENTIAL_RE = re.compile(
    r'(?:(?<![A-Za-z0-9_])|(?<=%3F)|(?<=%26))('
    + '|'.join(_DEFAULT_CREDENTIAL_PARAMS)
    + r')(=|%3D)[^&;#\s]*',
    re.IGNORECASE)


def _credential_re():
    try:
        from lazylibrarian.config2 import CONFIG
        extra = get_list(CONFIG['REDACT_PARAMS'], ',')
    except Exception:
        extra = []
    all_params = sorted(set(_DEFAULT_CREDENTIAL_PARAMS) | {p.lower() for p in extra if p})
    key = tuple(all_params)
    with _credential_re_lock:
        if _credential_re_cache['params'] == key:
            return _credential_re_cache['regex']
    try:
        regex = re.compile(
            r'(?:(?<![A-Za-z0-9_])|(?<=%3F)|(?<=%26))('
            + '|'.join(re.escape(p) for p in all_params)
            + r')(=|%3D)[^&;#\s]*',
            re.IGNORECASE)
    except re.error:
        return _DEFAULT_CREDENTIAL_RE
    with _credential_re_lock:
        _credential_re_cache['regex'] = regex
        _credential_re_cache['params'] = key
    return regex


def _redact_secrets_in_path(url):
    try:
        from lazylibrarian.config2 import CONFIG
        secrets = CONFIG.REDACTLIST
        for secret in secrets:
            if isinstance(secret, str) and len(secret) > 3 and secret in url:
                url = url.replace(secret, '[redacted]')
    except Exception:
        pass
    return url


def redact_url(url):
    if not url:
        return ''
    url = make_unicode(url)
    if not isinstance(url, str):
        return '[unprintable url]'
    url = USERINFO_RE.sub('[redacted]@', url, count=1)
    url = _credential_re().sub(r'\1\2[redacted]', url)
    url = _redact_secrets_in_path(url)
    return url


def book_series(bookname):
    """
    Try to get a book series/seriesnum from a bookname, or return empty string
    See if book is in multiple series first, if so return first one
    eg "The Shepherds Crown (Discworld, #41; Tiffany Aching, #5)"
    if no match, try single series, eg Mrs Bradshaws Handbook (Discworld, #40.5)
    Returns seriesname, number if a series, otherwise "", ""
    """
    # \(            Must have (
    # ([\S\s]+      followed by a group of one or more non whitespace
    # [^)])        not ending in )
    # ,? #?         followed by optional comma, then space optional hash
    # (             start next group
    # \d+           must have one or more digits
    # \.?           then optional decimal point, (. must be escaped)
    # -?            optional dash for a range
    # \d{0,}        zero or more digits
    # [;,]          a semicolon or comma if multiple series
    # )             end group
    series = ""
    seriesnum = ""

    # First handle things like "(Book 3: series name)"
    if ':' in bookname and bookname[0] == "(" and bookname[-1] == ")":
        # change to "(series name, Book 3)"
        parts = bookname[1:-1].split(':', 1)
        if parts[0][-1].isdigit():
            bookname = f'({parts[1]}, {parts[0]})'

    # These are words that don't indicate a following series name/number eg "FIRST 3 chapters"
    non_series_words = ['series', 'unabridged', 'volume', 'phrase', 'from', 'chapters', 'season',
                        'the first', 'includes', 'paperback', 'first', 'books', 'large print', 'of',
                        'rrp', '2 in', '&', 'v.']

    # First look for multi series
    result = re.search(r"\(([\S\s]+[^)]),? #?(\d+\.?-?\d*[;,])", bookname)
    if result:
        series = result.group(1)
        while series[-1] in ',)':
            series = series[:-1]
        seriesnum = result.group(2)
        while seriesnum[-1] in ';,':
            seriesnum = seriesnum[:-1]
    else:
        result = re.search(r"\(([\S\s]+[^)]),? #?(\d+\.?-?\d*)", bookname)
        if result:
            series = result.group(1)
            while series[-1] in ',)':
                series = series[:-1]
            seriesnum = result.group(2)

    for word in [' novel', ' book', ' part', ' -']:
        if series and series.lower().endswith(word):
            series = series[:-len(word)]
            break

    for word in non_series_words:
        if series.lower().startswith(word):
            return "", ""

    series = clean_name(unaccented(series, only_ascii=False)).strip()
    seriesnum = seriesnum.strip()
    if series.lower().strip('.') == 'vol':
        series = ''
    if series.lower().strip('.').endswith('vol'):
        series = series.strip('.')
        series = series[:-3].strip()
    return series, seriesnum


def now():
    dtnow = datetime.datetime.now()
    return dtnow.strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    dtnow = datetime.datetime.now()
    return dtnow.strftime("%Y-%m-%d")


def seconds_to_midnight():
    """Get the number of seconds to midnight."""
    tomorrow = datetime.datetime.now() + datetime.timedelta(1)
    midnight = datetime.datetime(year=tomorrow.year, month=tomorrow.month,
                                 day=tomorrow.day, hour=0, minute=0, second=0)
    return (midnight - datetime.datetime.now()).seconds


def age(histdate):
    """
    Return how many days since histdate
    histdate = yyyy-mm-dd
    return 0 for today, or if invalid histdate
    """
    return datecompare(today(), histdate)


def check_year(num, past=1800, future=1):
    # See if num looks like a valid year
    # for a magazine allow forward dated by a year, eg Jan 2017 issues available in Dec 2016
    n = check_int(num, 0)
    if past < n <= int(now()[:4]) + future:
        return n
    return 0


def nzbdate2format(nzbdate):
    """
    Returns an "nzb date" in yyyy-mm-dd format
    Returns 1970-01-01 if the date can't be parsed
    """
    try:
        mmname = nzbdate.split()[2].zfill(2)
        day = nzbdate.split()[1]
        # nzbdates are mostly english short month names, but not always
        month = month2num(mmname)
        if month == 0:
            month = 1  # hopefully won't hit this, but return a default value rather than error
        year = nzbdate.split()[3]
        return f"{year}-{month:02d}-{day}"
    except IndexError:
        return "1970-01-01"


def date_format(datestr, formatstr="$Y-$m-$d", context='', datelang=''):
    # return date formatted for display in requested style
    # $d	Day of the month as a zero-padded decimal number
    # $D    Day of month, zero padded, suppress if 01
    # $b	Month as abbreviated name
    # $B	Month as full name
    # $m	Month as a zero-padded decimal number
    # $y	Year without century as a zero-padded decimal number
    # $Y	Year with century as a decimal number
    # datestr are stored in lazylibrarian database as YYYY-MM-DD or IIII for issue number, VVVVIIII etc
    # If context is provided as a parameter, it will be used to provide more informative error messages.

    # Dates from providers are in various formats, need to consolidate them so we can sort...
    # Newznab/Torznab Tue, 23 Aug 2016 17:33:26 +0100
    # LimeTorrent 13 Nov 2014 05:01:18 +0200
    # torrent_tpb 04-25 23:46 or 2018-04-25
    # openlibrary May 1995 or June 20, 2008
    # sometimes one "word" eg 28Dec2008
    # We could use dateutil module but it's not standard library and we only have a few formats
    # so we can roll our own. To make it simple we'll ignore timezone and seconds

    if not datestr:
        return ''

    if datestr.isdigit():  # just issue number or year
        return datestr

    logger = logging.getLogger(__name__)
    dateparts = datestr.split(' +')[0].split(';')[0].replace(
        '-', ' ').replace(':', ' ').replace(',', ' ').replace('/', ' ').split()
    if len(dateparts) == 1:  # one "word" might need splitting
        dateparts = []
        word = ''
        digits = True
        for c in datestr:
            if digits and c.isdigit() or not digits and not c.isdigit():
                word += c
            elif word:
                dateparts.append(word)
                word = c
                digits = not digits
            else:
                word = c
                digits = c.isdigit()
        if word:
            dateparts.append(word)

    if len(dateparts) == 8:  # remove the time offset
        dateparts = dateparts[:7]
    if len(dateparts) == 7:  # Tue, 23 Aug 2016 17:33:26
        _, d, m, y, hh, mm, _ = dateparts
    elif len(dateparts) == 6:
        if check_year(dateparts[0]):  # YYYY-MM-DD HH:MM:SS
            y, m, d, hh, mm, _ = dateparts
        elif check_year(dateparts[1]):  # MM-YYYY-DD HH:MM:SS
            m, y, d, hh, mm, _ = dateparts
        elif check_year(dateparts[2]):  # 13 Nov 2014 05:01:18
            d, m, y, hh, mm, _ = dateparts
        else:
            d, m, y, hh, mm = 0, 0, 0, 0, 0
    elif len(dateparts) == 5:  # 2018-04-25 23:46
        y, m, d, hh, mm = dateparts
    elif len(dateparts) == 4:  # 04-25 23:46 (this year)
        m, d, hh, mm = dateparts
        y = now()[:4]
    elif len(dateparts) == 3:  # 2018-04-25 or June 20 2008 or 20 June 2008
        y, m, d, = 0, 0, 0
        if check_year(dateparts[0]) and dateparts[2].isdigit():
            y, m, d = dateparts
        elif check_year(dateparts[2]):
            if dateparts[0].isdigit():
                d, m, y = dateparts
            elif dateparts[1].isdigit():
                m, d, y = dateparts
        hh = '00'
        mm = '00'
    elif len(dateparts) == 2 and check_year(dateparts[1]):  # May 1995
        m, y = dateparts
        d = '01'
        hh = '00'
        mm = '00'
    elif len(dateparts) == 2 and check_year(dateparts[0]):  # 1995-05
        y, m = dateparts
        d = '01'
        hh = '00'
        mm = '00'
    else:
        d, m, y, hh, mm = 0, 0, 0, 0, 0

    try:
        _ = int(m)
    except ValueError:
        try:
            m = f"{month2num(m):02d}"
        except IndexError:
            m = 0
    if not m or m == "00":
        msg = f"Unrecognised datestr [{datestr[:40]}]"
        if context:
            msg = f'{msg} for {context}'
        logger.warning(msg)
        return datestr

    m = m.zfill(2)
    d = d.zfill(2)
    datestr = f"{y}-{m}-{d} {hh}:{mm}:00"
    if not formatstr:
        if len(dateparts) == 1:
            return datestr[:4]  # only year
        return datestr[:11]  # default yyyy-mm-dd

    dd = d
    if d == '01':
        dd = ''

    formattedstr = formatstr.replace(
        '$Y', y).replace(
        '$y', y[2:]).replace(
        '$m', m).replace(
        '$D', dd).replace(
        '$d', d)
    try:
        if '$B' in formatstr or '$b' in formatstr:
            lang = 0
            cnt = 0
            while cnt < len(lazylibrarian.MONTHNAMES[0][0]):
                if lazylibrarian.MONTHNAMES[0][0][cnt] == datelang:
                    lang = cnt
                    break
                cnt += 1
            monthname = lazylibrarian.MONTHNAMES[0][int(m)]
            formattedstr = formattedstr.replace('$B', monthname[lang]).replace('$b', monthname[lang + 1])
        formattedstr = ' '.join(formattedstr.split()).strip()
        return formattedstr
    except (NameError, IndexError):
        logger.error(f"Invalid datestr [{datestr}] for {formatstr}")
        return datestr


def two_months(word):
    # compound months as a single word eg AprilMay
    # found in some magazine titles
    a = 0
    b = 0
    cleanword = unaccented(word).lower()
    for f in range(1, 13):
        for month in lazylibrarian.MONTHNAMES[0][f]:
            if word.startswith(month):
                a = f
                break
        if not a:
            for month in lazylibrarian.MONTHNAMES[1][f]:
                if cleanword.startswith(month):
                    a = f
        if a:
            break
    if a:
        for f in range(1, 13):
            for month in lazylibrarian.MONTHNAMES[0][f]:
                if word.endswith(month):
                    b = f
                    break
            if not b:
                for month in lazylibrarian.MONTHNAMES[1][f]:
                    if cleanword.endswith(month):
                        b = f
            if b:
                break
    if a == b:
        return 0, 0
    return a, b


def month2num(month):
    """
    Return a month number
     - given a month name (long or short) in requested locales
     - or given a season name in the seasons dictionary
    """
    cleanmonth = unaccented(month).lower()
    for f in range(1, 13):
        if month in lazylibrarian.MONTHNAMES[0][f] or cleanmonth in lazylibrarian.MONTHNAMES[1][f]:
            return f

    if cleanmonth in lazylibrarian.SEASONS:
        return lazylibrarian.SEASONS[cleanmonth]
    return 0


def datecompare(nzbdate, control_date):
    """
    Return how many days between two dates given in yy-mm-dd format or yyyy-mm-dd format
    or zero if error (not a valid date)
    """
    try:
        y1, m1, d1 = (int(x) for x in nzbdate.split('-'))
        y2, m2, d2 = (int(x) for x in control_date.split('-'))
        if y1 < 100:
            y1 += 1900
        if y2 < 100:
            y2 += 1900
        date1 = datetime.date(y1, m1, d1)
        date2 = datetime.date(y2, m2, d2)
        dtage = date1 - date2
        return dtage.days
    except ValueError:
        return 0


def plural(var, phrase=""):
    """
    Convenience function for pluralising log messages
    if var = 1 return phrase unchanged
    if var is anything else return phrase + 's'
    so book -> books, seeder -> seeders  etc
    or return translation
    so copy -> copies, entry -> entries  etc
    """
    translates = {
        'copy': 'copies',
        'entry': 'entries',
        'shelf': 'shelves',
        'series': 'series',
        'is': 'are',
    }
    if check_int(var, 0) == 1:
        return phrase
    res = translates.get(phrase, '')
    if res:
        return res
    return f'{phrase}s'


def check_int(var, default, positive=True):
    """
    Return an integer representation of var
    or return default value if var is not a positive integer
    """
    try:
        res = int(var)
        if positive and res < 0:
            return default
        return res
    except (ValueError, TypeError):
        try:
            return int(default)
        except (ValueError, TypeError):
            return 0


def check_float(var, default):
    """
    Return a float representation of var
    or return default value if var is not a float
    """
    try:
        return float(var)
    except (ValueError, TypeError):
        try:
            return float(default)
        except (ValueError, TypeError):
            return 0.0


def pretty_approx_time(seconds: int) -> str:
    """ Return a string representing the parameter in a nice human readable (approximate) way """
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    locals_ = locals()
    magnitudes_str = (f"{int(locals_[magnitude])} {magnitude}"
                      for magnitude in ("days", "hours", "minutes", "seconds") if locals_[magnitude])
    return ", ".join(magnitudes_str)


def human_size(num):
    num = check_int(num, 0)
    for unit in ['B', 'KiB', 'MiB', 'GiB', 'TiB']:
        if abs(num) < 1024.0:
            return f"{num:3.2f}{unit}"
        num /= 1024.0
    return f"{num:.2f}PiB"


def size_in_bytes(size):
    """
    Take a size string with units eg 10 Mb, 5.3Kb
    and return integer bytes
    """
    if not size:
        return 0
    mult = 1
    try:
        if 'K' in size:
            size = size.split('K')[0]
            mult = 1024
        elif 'M' in size:
            size = size.split('M')[0]
            mult = 1024 * 1024
        elif 'G' in size:
            size = size.split('G')[0]
            mult = 1024 * 1024 * 1024
        size = int(float(size) * mult)
    except (ValueError, IndexError):
        size = 0
    return size


def md5_utf8(txt):
    if isinstance(txt, str):
        txt = txt.encode('utf-8')
    return md5(txt).hexdigest()


# CP850: 0x80-0xA5 (fortunately not used in ISO-8859-15) aka latin-1
# UTF-8: 1st hex code 0xC2-0xC3 followed by a 2nd hex code 0xA1-0xFF
# ISO-8859-15: 0xA6-0xFF
# The function will detect if string contains a special character
# If there is special character, detects if it is a UTF-8, CP850 or ISO-8859-15 encoding
def make_utf8bytes(txt):
    name = make_bytestr(txt)
    # parse to detect if CP850/ISO-8859-15 is used
    # and return tuple of bytestring encoded in utf-8, detected encoding
    for idx in range(len(name)):
        # /!\ detection is done 2char by 2char for UTF-8 special character
        ch = chr(name[idx])
        if idx < (len(name) - 1):
            chx = chr(name[idx + 1])
            # Detect UTF-8
            if ((ch == '\xC2') | (ch == '\xC3')) & ((chx >= '\xA0') & (chx <= '\xFF')):
                return name, 'UTF-8'
        # Detect CP850 or Windows CP1252 (latin-1)
        if (ch >= '\x80') & (ch <= '\xA5'):
            name = name.decode('cp850')
            return name.encode('utf-8'), 'CP850'
        # Detect ISO-8859-15 (latin-9)
        if (ch >= '\xA6') & (ch <= '\xFF'):
            name = name.decode('iso-8859-15')
            return name.encode('utf-8'), 'ISO-8859-15'
    return name, ''


_encodings = ['utf-8', 'iso-8859-15', 'cp850']


def make_unicode(txt: str | bytes) -> str | bytes | None:
    # convert a bytestring to unicode, don't know what encoding it might be so try a few
    # it could be a file on a windows filesystem, unix...
    # return is unicode if possible, else bytestring
    if txt is None:
        return txt
    if isinstance(txt, str):  # nothing to do if already unicode
        return txt
    if not isinstance(txt, bytes):  # list, int etc
        txt = str(txt)
        if isinstance(txt, str):
            return txt
    if lazylibrarian.SYS_ENCODING.lower() not in _encodings:
        _encodings.insert(0, lazylibrarian.SYS_ENCODING)
    for encoding in _encodings:
        try:
            return txt.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    logger = logging.getLogger(__name__)
    logger.debug(f"Unable to decode name [{repr(txt)}]")
    return txt


def make_bytestr(txt):
    """
    Turn text into a binary byte string
    """
    if txt is None:
        return txt
    if isinstance(txt, bytes):  # nothing to do if already bytestring
        return txt
    if not isinstance(txt, str):  # list, int etc
        txt = str(txt)
        if isinstance(txt, bytes):
            return txt
    if lazylibrarian.SYS_ENCODING.lower() not in _encodings:
        _encodings.insert(0, lazylibrarian.SYS_ENCODING)
    for encoding in _encodings:
        try:
            return txt.encode(encoding)
        except (UnicodeDecodeError, LookupError):
            pass
    logger = logging.getLogger(__name__)
    logger.debug(f"Unable to encode name [{repr(txt)}]")
    return txt


def is_valid_isbn(isbn):
    """
    Return True if parameter looks like a valid isbn
    either 13 digits, 10 digits, or 9 digits followed by 'x'
    """
    if not isbn:
        return False
    isbn = isbn.replace('-', '').replace(' ', '')
    if len(isbn) == 13 and isbn.isdigit():
        return True
    if len(isbn) == 10 and isbn[:9].isdigit():  # Validate checksum
        xsum = 0
        for i in range(9):
            xsum += check_int(isbn[i], 0) * (10 - i)
        if isbn[9] in "Xx":
            xsum += 10
        else:
            xsum += check_int(isbn[9], 0)
        return xsum % 11 == 0
    return False


def is_valid_type(filename: str, extensions: list[str], extras='jpg, opf') -> bool:
    """
    Check if filename has an extension we can process.
    returns True or False
    """
    type_list = extensions + get_list(extras)
    extn = lazylibrarian.filesystem.splitext(filename)[1].lstrip('.')
    return extn and extn.lower() in type_list


def get_list(st, c=None):
    """
    Split a string/unicode into a list on whitespace or plus or comma
    or single character split eg filenames with spaces split on comma only
    Returns list of same type as st
    """
    lst = []
    if st:
        if c is not None and len(c) == 1:
            x = st.split(c)
            for item in x:
                lst.append(item.strip())
        else:
            st = st.replace(',', ' ').replace('+', ' ')
            lst = ' '.join(st.split()).split()
    return lst


# noinspection PyArgumentList
def safe_unicode(obj, *args):
    """ return the unicode representation of obj """
    return str(obj, *args)


def split_title(author, book):
    """
    Strips the author name from book title and
    returns the book name part split into (name, subtitle and series)
    """
    matchlogger = logging.getLogger('special.matching')
    matchlogger.debug(f"{author} [{book}]")
    bookseries = ''
    # Strip author from title, eg Tom Clancy: Ghost Protocol
    if book.startswith(f"{author}:"):
        book = book.split(f"{author}:")[1].strip()

    if '(' in book and book.endswith(')'):
        # if title ends with words in braces, split on last brace
        # as this always seems to be a subtitle or series info
        # If there is a digit before the closing brace assume it's series
        # eg Abraham Lincoln: Vampire Hunter (Abraham Lincoln: Vampire Hunter, #1)
        # unless the part in braces is one word, eg (TM) or (Annotated) or (Unabridged)
        if book[-2].isdigit():
            # separate out the series part
            book, bookseries = book.rsplit('(', 1)
            bookseries = bookseries.strip(')')
            book = book.strip().rstrip(':').strip()
        else:
            parts = book.rsplit('(', 1)
            parts[1] = f"({parts[1]}"
            bookname = parts[0].strip()
            booksub = parts[1].rstrip(':').strip()
            if ' ' not in booksub:
                booksub = ''
            if booksub.find(')'):
                for item in ImportPrefs.SPLIT_LIST:
                    if f"({item})" == booksub.lower():
                        booksub = ""
            matchlogger.debug(f"[{bookname}][{booksub}][{bookseries}]")
            return bookname, booksub, bookseries

    # if not (words in braces at end of string)
    # split subtitle on last colon
    # eg The Land: Awakening: The Saga Continues
    # title = The Land: Awakening
    bookname = book
    booksub = ''
    if ':' in book:
        parts = book.rsplit(':', 1)
        bookname = parts[0].strip()
        booksub = parts[1].rstrip(':').strip()
        bookname_lower = bookname.lower()
        booksub_lower = booksub.lower()
        for item in ImportPrefs.SPLIT_LIST:
            if item and booksub_lower.startswith(item) or bookname_lower.startswith(item):
                bookname = book
                booksub = ''
                break

    logger = logging.getLogger(__name__)
    logger.debug(f"Name[{bookname}] Sub[{booksub}] Series[{bookseries}]")
    return bookname, booksub, bookseries


def format_author_name(author: str, postfix: list[str]) -> str:
    """ get authorname in a consistent format """
    fuzzlogger = logging.getLogger('special.fuzz')
    author = make_unicode(author)
    author = unicodedata.normalize('NFKD', author)
    # if multiple authors assume the first one is primary
    # except if only one word before '&', e.g. Robert & Gilles Néret Descharnes
    if '& ' in author:
        words = author.split('& ')[0].strip().split(' ')
        if len(words) > 1:
            author = author.split('& ')[0].strip()
    if "," in author:
        words = author.split(',')
        if len(words) == 2:
            # Need to handle names like "L. E. Modesitt, Jr." or "J. Springmann, Phd"
            # use an exceptions list for now, there might be a better way...
            if words[1].strip().strip('.').strip('_').lower() in postfix:
                surname = words[1].strip()
                forename = words[0].strip()
            else:
                # guess its "surname, forename" or "surname, initial(s)" so swap them round
                forename = words[1].strip()
                # openlibrary adds period to shortened fornames, eg "Will."
                # make sure we don't interfere with initials...
                if forename.endswith('.') and len(forename) > 2 and forename.count('.') == 1:
                    forename = forename.strip('.')
                surname = words[0].strip()
            if author != f"{forename} {surname}":
                fuzzlogger.debug(f'Formatted authorname [{author}] to [{forename} {surname}]')
                author = f"{forename} {surname}"
    # ensure initials have a '.' on the end
    words = author.replace('.', ' ').split()
    author = ''
    for word in words:
        if len(word) == 1 and word.isalpha():
            word = word + '.'
        author = f"{author} {word}"
    # reformat any initials, we want to end up with L.E. Modesitt Jr, Charles H. Elliott PhD
    if '.' in author:
        forename, surname = author.rsplit('.', 1)
        forename = forename.replace('. ', '.')
        author = f"{forename}. {surname}"

    res = ' '.join(author.split())  # ensure no extra whitespace
    if res.isupper() or res.islower():
        res = res.title()
    return res


def sort_definite(title: str, articles=list[str]) -> str:
    """
    Return the sort string for a title, moving prefixes
    we want to ignore to the end, like The or A
    """
    words = get_list(title)
    if len(words) < 2:
        return title
    word = words.pop(0)
    if word.lower() in articles:
        return f"{' '.join(words)}, {word}"
    return title


def surname_first(authorname: str, postfixes: list[str]) -> str:
    """ Swap authorname round into surname, forenames for display and sorting"""
    words = get_list(authorname)
    if len(words) < 2:
        return authorname
    res = words.pop()

    if res.strip('.').lower() in postfixes:
        res = f"{words.pop()} {res}"
    return res + ', ' + ' '.join(words)


def clean_name(name, extras=None):
    if not name:
        return ''

    if extras and "'" in extras:
        name = replace_all(name, lazylibrarian.DICTS.get('apostrophe_dict', {}))

    valid_name_chars = f"-_.() {extras}"
    cleaned = ''.join(c for c in name if c in valid_name_chars or c.isalnum())
    cleaned = cleaned.strip()
    if cleaned:
        return cleaned
    return name


def unaccented(str_or_unicode, only_ascii=True):
    if not str_or_unicode:
        return ''
    return make_unicode(unaccented_bytes(str_or_unicode, only_ascii=only_ascii))


def unaccented_bytes(str_or_unicode, only_ascii=True):
    if not str_or_unicode:
        return ''.encode('ASCII')  # ensure bytestring for python3
    # use long form to separate out the accents into combining type
    try:
        cleaned = unicodedata.normalize('NFKD', str_or_unicode)
    except TypeError:
        cleaned = unicodedata.normalize('NFKD', str_or_unicode.decode('utf-8', 'replace'))

    # replace all non-ascii quotes/apostrophes with ascii ones eg "Collector's"
    stripped = replace_all(cleaned, lazylibrarian.DICTS.get('apostrophe_dict', {}))
    if only_ascii:
        stripped = ''.join([c for c in stripped if not unicodedata.combining(c)])
        # Other characters not converted by unicodedata.combining
        # c6 Ae, d0 Eth, d7 multiply, d8 Ostroke, de Thorn, df sharpS
        # e6 ae, f0 eth, f7 divide, f8 ostroke, fe thorn
        dic = {'\xc6': 'A', '\xd0': 'D', '\xd7': '*', '\xd8': 'O', '\xde': 'P', '\xdf': 's',
                '\xe6': 'a', '\xf0': 'o', '\xf7': '/', '\xf8': 'o', '\xfe': 'p'}
        stripped = replace_all(stripped, dic)
        stripped = stripped.encode('ASCII', 'ignore')
    else:  # replace with specified char (use '_' for goodreads author names)
        stripped = stripped.encode('ASCII', 'replace')  # replaces with '?'
        if not isinstance(only_ascii, bool):
            stripped = stripped.replace(b'?', make_bytestr(str(only_ascii)[0]))
    stripped = stripped.strip()
    if not stripped:
        stripped = str_or_unicode
    return stripped  # return bytestring


def replace_all(text, dic):
    if not text:
        return ''
    for item in dic:
        text = text.replace(item, dic[item])
    return text


def strip_quotes(text):
    """
    Strips every occurrence of quote characters in "text"
    """
    if not text:
        return ''
    for item in lazylibrarian.DICTS.get('apostrophe_dict', {}):
        text = text.replace(item, '')
    return text


def replacevars(base, mydict, is_folder=False):
    if not base:
        return ''
    matchinglogger = logging.getLogger('special.matching')
    matchinglogger.debug(base)
    vardict = ['$Author', '$SortAuthor', '$Title', '$SortTitle', '$Series', '$FmtName', '$FmtNum', '$Language',
               '$SerName', '$SerNum', '$PadNum', '$PubYear', '$SerYear', '$Part', '$Total', '$Abridged',
               '$IssueDate', '$IssueNum', '$IssueVol', '$IssueMonth', '$IssueMNum', '$IssueYear', '$IssueDay']

    # first strip any braced expressions where any var in the expression is empty
    # eg {$SerName - $SerNum} becomes '' if either var is empty
    # but the braced expression may have OR options, eg  a|b|c so stop on first expression to pass
    while '{' in base and '}' in base and base.index('{') < base.index('}'):
        left, rest = base.split('{', 1)
        middle, right = rest.split('}', 1)
        expressions = middle.split('|')
        valid = False
        for expression in expressions:
            for item in vardict:
                if item in expression and item[1:] in mydict and mydict[item[1:]] == '':
                    expression = ''
                    break
            if expression:
                base = f"{left}{expression}{right}"
                valid = True
                break
        if not valid:
            base = f"{left}{right}"

    for item in vardict:
        if item[1:] in mydict:
            base = base.replace(item, sanitize(mydict[item[1:]], is_folder_or_file=is_folder))
    base = base.replace('$$', ' ')
    matchinglogger.debug(base)
    return base
