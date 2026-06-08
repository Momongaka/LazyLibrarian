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

import datetime
import logging
import os
import re
import threading
import traceback
import uuid
from hashlib import sha1
from shutil import copyfile

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.bookrename import stripspaces
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import (
    DIRS,
    book_file,
    get_directory,
    make_dirs,
    path_exists,
    path_isdir,
    path_isfile,
    remove_dir,
    safe_move,
    setperm,
    splitext,
    syspath,
)
from lazylibrarian.formatter import (
    check_int,
    check_year,
    get_list,
    make_bytestr,
    month2num,
    plural,
    replace_all,
    replacevars,
    sanitize,
    two_months,
)
from lazylibrarian.images import create_mag_cover, read_pdf_tags, write_pdf_tags
from lazylibrarian.librarysync import get_book_info


def create_id(issuename=None):
    hash_id = sha1(make_bytestr(issuename)).hexdigest()
    # logger.debug('Issue %s Hash: %s' % (issuename, hashID))
    return hash_id


def clean_maglibrary():
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    issues = db.select('select * from Issues')
    mag_count = 0
    issue_count = 0
    # check all the issues are still there, delete entry if not
    for issue in issues:
        title = issue['Title']
        issuedate = issue['IssueDate']
        issuefile = issue['IssueFile']

        if issuefile and not path_isfile(issuefile):
            db.action('DELETE from Issues where issuefile=?', (issuefile,))
            logger.info(f'Issue {title} - {issuedate} deleted as not found on disk')
            issue_count += 1

    # now check the magazine titles and delete any with no issues
    if CONFIG.get_bool('MAG_DELFOLDER'):
        mags = db.select('SELECT Title,count(Title) as counter from issues group by Title')
        for mag in mags:
            title = mag['Title']
            issues = mag['counter']
            if not issues:
                logger.debug(f'Magazine {title} deleted as no issues found')
                db.action('DELETE from magazines WHERE Title=?', (title,))
                mag_count += 1

    # now reset the magazine latest issues
    mags = db.select('SELECT Title from magazines')
    for mag in mags:
        #issues = db.select('select issuedate from issues where title=?', (mag['Title'],))
        control_value_dict = {"Title": mag['Title']}
        new_value_dict = {"IssueStatus": "Open"}
        # Set magazine_issuedate to issuedate of most recent issue we have
        # Set latestcover to most recent issue cover
        # Set magazine_added to acquired date of the earliest issue we have
        # Set magazine_lastacquired to acquired date of most recent issue we have
        # acquired dates are read from magazine file timestamps
        res = db.match('SELECT MagazineAdded from magazines where title=?', (mag['Title'], ))
        magazineadded = res['MagazineAdded']
        new_value_dict["MagazineAdded"] = magazineadded
        maglastacquired = None
        magissuedate = None
        issues = db.select('SELECT Title,IssueFile,IssueDate,Cover from issues WHERE Title=?', (mag['Title'], ))
        for issue in issues:
            mtime = os.path.getmtime(syspath(issue['IssueFile']))
            iss_acquired = datetime.date.isoformat(datetime.date.fromtimestamp(mtime))
            if not magazineadded or magazineadded == 'None' or iss_acquired < magazineadded:
                magazineadded = iss_acquired
                new_value_dict["MagazineAdded"] = magazineadded
            if not maglastacquired or iss_acquired > maglastacquired:
                maglastacquired = iss_acquired
                new_value_dict["LastAcquired"] = maglastacquired
            if not magissuedate or issue['IssueDate'] >= magissuedate:
                magissuedate = issue['IssueDate']
                new_value_dict["IssueDate"] = magissuedate
                new_value_dict["LatestCover"] = issue['Cover']

        # if no issues, but not deleting the folder, keep the details of the last issue we used to have
        if not CONFIG.get_bool('MAG_DELFOLDER') and maglastacquired is None:
            if "LastAcquired" in new_value_dict:
                new_value_dict.pop("LastAcquired")
            if "IssueDate" in new_value_dict:
                new_value_dict.pop("IssueDate")
            new_value_dict["LatestCover"] = ''
        db.upsert("magazines", new_value_dict, control_value_dict)

    return mag_count, issue_count


def magazine_scan(title=None):
    logger = logging.getLogger(__name__)
    matchinglogger = logging.getLogger('special.matching')
    lazylibrarian.MAG_UPDATE = 1

    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        mag_path = CONFIG['MAG_DEST_FOLDER']
        if CONFIG.get_bool('MAG_RELATIVE'):
            mag_path = os.path.join(get_directory('eBook'), mag_path)

        onetitle = title
        if onetitle and '$Title' in mag_path:
            mag_path = mag_path.replace('$Title', onetitle)

        while '$' in mag_path:
            mag_path = os.path.dirname(mag_path)

        if CONFIG.get_bool('FULL_SCAN') and not onetitle:
            clean_maglibrary()

        logger.info(f" Checking [{mag_path}] for {CONFIG['MAG_TYPE']}")

        issue_cnt = 0
        for _, _, filenames in os.walk(mag_path):
            for fname in filenames:
                if CONFIG.is_valid_booktype(fname, booktype='mag'):
                    issue_cnt += 1
                    lazylibrarian.magazinescan_data = f"Counting issues: {issue_cnt}"
        msg = f"Found {issue_cnt} issues"
        logger.debug(msg)
        lazylibrarian.magazinescan_data = msg

        booktypes = ''
        count = -1
        booktype_list = get_list(CONFIG['MAG_TYPE'])
        for book_type in booktype_list:
            count += 1
            if count == 0:
                booktypes = book_type
            else:
                booktypes = f"{booktypes}|{book_type}"

        # massage the MAG_DEST_FILE config parameter into something we can use
        # with regular expression matching
        # only escape the non-alpha characters as python 3.7 reserves escaped alpha
        match_string = ''
        matchto = CONFIG['MAG_DEST_FILE']
        for char in matchto:
            if not char.isalpha():
                match_string += '\\'
            match_string = match_string + char

        match = match_string.replace(
            "\\$IssueDate", "(?P<issuedate>.*?)").replace(
            "\\$Title", "(?P<title>.*?)") + r'\.[' + booktypes + ']'
        matchinglogger.debug(f"Pattern [{match}]")

        # noinspection PyBroadException
        try:
            pattern = re.compile(match, re.VERBOSE | re.IGNORECASE)
        except Exception as e:
            logger.error(f"Pattern failed for [{matchto}] {str(e)}")
            pattern = None

        if pattern:
            total_items = issue_cnt
            current_item = 0
            for rootdir, _, filenames in os.walk(mag_path):
                for fname in filenames:
                    # maybe not all magazines will be pdf?
                    if CONFIG.is_valid_booktype(fname, booktype='mag'):
                        issuedate = ''
                        issuefile = os.path.join(rootdir, fname)  # full path to issue.pdf
                        current_item += 1
                        current_percent = int(current_item * 100 / total_items)
                        lazylibrarian.magazinescan_data = f"{current_item}/{total_items}/{current_percent}"

                        # noinspection PyBroadException
                        try:
                            match = db.match('SELECT Title,IssueDate from issues WHERE IssueFile=?', (issuefile,))
                            if match:  # issue is already in the database
                                title = match['Title']
                                issuedate = match['IssueDate']
                                logger.debug(f"Using {title}:{issuedate} from database issuefile")

                            if not match:  # try for data in an opf file (faster than reading pdf tags)
                                opf_file = f"{splitext(issuefile)[0]}.opf"
                                if path_isfile(opf_file):
                                    res = get_book_info(opf_file)
                                    if res['Authors'] and res['Authors'][0] != "magazines":
                                        title = res['Authors'][0]
                                    elif res['Creator']:
                                        title = res['Creator']
                                    if title:
                                        res = get_dateparts(issuefile)
                                        if res:
                                            issuedate = res['dbdate']
                                if title and issuedate:
                                    match = True
                                    logger.debug(f"Using {title}:{issuedate} from opf file")

                            if not match:  # try our custom tags in the pdf
                                metadata = read_pdf_tags(issuefile)
                                if metadata:
                                    issuedate = metadata.get('/LL_Issue')
                                    title = metadata.get('/LL_Title')
                                if title and issuedate:
                                    logger.debug(f"Using {title}:{issuedate} from pdf tags")
                                    match = True

                            if not match:  # last resort
                                match = pattern.match(fname)
                                if match:
                                    title = match.group("title").strip()
                                    issuedate = match.group("issuedate").strip()
                                    matchinglogger.debug(f"Title pattern [{title}][{issuedate}] {fname}")
                                    if title and issuedate:
                                        match = True
                                        if title.rsplit('(', 1)[1].split(')').isdigit():
                                            # it's a calibre folder, title is probably parent folder
                                            issuefolder = os.path.dirname(issuefile)
                                            parent = os.path.dirname(issuefolder)
                                            title = os.path.basename(parent)
                                            logger.debug(f"Using {title}:{issuedate} from "
                                                         f"{issuefile} calibre parent folder")
                                        else:
                                            logger.debug(f"Using {title}:{issuedate} from "
                                                         f"filename {fname} pattern match")
                                else:
                                    logger.debug(f"Pattern match failed for [{fname}]")
                        except Exception:
                            match = False
                        if not match:
                            title = os.path.basename(rootdir).strip()
                            issuedate = ''
                            try:
                                # noinspection PyTypeChecker
                                calibreid = title.rsplit('(', 1)[1].split(')')[0]
                                if not calibreid.isdigit():
                                    calibreid = ''
                            except IndexError:
                                calibreid = ''

                            if calibreid:
                                # it's a calibre folder, title is probably parent folder
                                parent = os.path.dirname(rootdir)
                                title = os.path.basename(parent)
                                logger.debug(f"Using {title} from calibre folder {rootdir}")
                            else:
                                try:
                                    parts = CONFIG['MAG_DEST_FOLDER'].split(os.sep)
                                    parts.reverse()
                                    title_part = parts.index('$Title')
                                    title_part = title_part * -1 - 1
                                    title = rootdir.split(os.sep)[title_part]
                                    logger.debug(f"Using {title} as part {title_part} from {rootdir}")
                                except (ValueError, IndexError):
                                    dateparts = get_dateparts(title)
                                    if dateparts['style']:
                                        parent = os.path.dirname(rootdir)
                                        title = os.path.basename(parent)
                                        logger.debug(f"Using {title} from parent folder {rootdir}")
                                    else:
                                        logger.debug(f"Using {title} from basename {rootdir}")

                        datetype = ''
                        # is this magazine already in the database?
                        cmd = ("SELECT Title,LastAcquired,IssueDate,MagazineAdded,CoverPage,DateType,Language "
                               "from magazines WHERE Title=? COLLATE NOCASE")
                        mag_entry = db.match(cmd, (title,))
                        if mag_entry:
                            datetype = mag_entry['DateType']
                        if not datetype:
                            datetype = ''

                        if issuedate:
                            dateparts = get_dateparts(issuedate, datetype=datetype)
                            issuenum_type = dateparts['style']
                            issuedate = dateparts['dbdate']
                            matchinglogger.debug(f"Date style [{issuenum_type}][{issuedate}]")

                        if not issuedate:
                            dateparts = get_dateparts(fname, datetype=datetype)
                            issuenum_type = dateparts['style']
                            issuedate = dateparts['dbdate']
                            matchinglogger.debug(f"Filename date style [{issuenum_type}][{issuedate}]")

                        if not issuedate:
                            logger.warning(f"Invalid name format for [{fname}]")
                            continue

                        mtime = os.path.getmtime(syspath(issuefile))
                        iss_acquired = datetime.date.isoformat(datetime.date.fromtimestamp(mtime))

                        logger.debug(f"Found {title} Issue {issuedate}")

                        if not mag_entry:
                            # need to add a new magazine to the database
                            # title = title.title()
                            control_value_dict = {"Title": title}
                            new_value_dict = {
                                "Reject": None,
                                "Status": "Active",
                                "MagazineAdded": None,
                                "LastAcquired": None,
                                "LatestCover": None,
                                "IssueDate": None,
                                "IssueStatus": "Skipped",
                                "Regex": None,
                                "CoverPage": 1,
                                "Language": CONFIG['PREF_MAGLANG'],
                            }
                            logger.debug(f"Adding magazine {title}")
                            db.upsert("magazines", new_value_dict, control_value_dict)
                            magissuedate = None
                            magazineadded = None
                            maglastacquired = None
                            magcoverpage = 1
                            maglanguage = CONFIG['PREF_MAGLANG']
                        else:
                            title = mag_entry['Title']
                            maglastacquired = mag_entry['LastAcquired']
                            magissuedate = mag_entry['IssueDate']
                            magazineadded = mag_entry['MagazineAdded']
                            magissuedate = str(magissuedate).zfill(4)
                            magcoverpage = mag_entry['CoverPage']
                            maglanguage = mag_entry['Language']

                        issuedate = str(issuedate).zfill(4)  # for sorting issue numbers

                        # is this issue already in the database?
                        issue_id = create_id(f"{title} {issuedate}")
                        iss_entry = db.match('SELECT IssueFile,Cover from issues WHERE IssueID=?', (issue_id, ))

                        if iss_entry and CONFIG['MAG_RENAME']:
                            new_issuefile, msg = rename_issue(issue_id)
                            if not msg:
                                issuefile = new_issuefile

                        new_entry = False
                        myhash = uuid.uuid4().hex
                        if not iss_entry or iss_entry['IssueFile'] != issuefile:
                            coverfile = create_mag_cover(issuefile, pagenum=magcoverpage, refresh=new_entry)
                            if coverfile:
                                hashname = os.path.join(DIRS.CACHEDIR, 'magazine', f'{myhash}.jpg')
                                copyfile(coverfile, hashname)
                                setperm(hashname)
                                cover = f'cache/magazine/{myhash}.jpg'
                            else:
                                cover = 'data/images/nocover.jpg'
                            new_entry = True  # new entry or name changed
                            if not iss_entry:
                                logger.debug(f"Adding issue {title} {issuedate}")
                            else:
                                logger.debug(f"Updating issue {title} {issuedate}")
                            control_value_dict = {"Title": title, "IssueDate": issuedate}
                            new_value_dict = {
                                "IssueAcquired": iss_acquired,
                                "IssueID": issue_id,
                                "IssueFile": issuefile,
                                "Cover": cover
                            }
                            db.upsert("Issues", new_value_dict, control_value_dict)
                        else:
                            logger.debug(f"Issue {title} {issuedate} already exists")
                            cover = iss_entry['Cover']

                        ignorefile = os.path.join(os.path.dirname(issuefile), '.ll_ignore')
                        try:
                            with open(syspath(ignorefile), 'w', encoding='utf-8') as f:
                                f.write("magazine")
                        except OSError as e:
                            logger.warning(f"Unable to create/write to ignorefile: {str(e)}")

                        if not CONFIG.get_bool('IMP_MAGOPF'):
                            logger.debug('create_mag_opf is disabled')
                        else:
                            lazylibrarian.metadata_opf.create_mag_opf(issuefile, title, issuedate,
                                                                      issue_id, language=maglanguage,
                                                                      overwrite=new_entry)
                        # see if this issues date values are useful
                        control_value_dict = {"Title": title}
                        if not mag_entry:  # new magazine, this is the only issue
                            # controlValueDict = {"Title": title.title()}
                            new_value_dict = {
                                "MagazineAdded": iss_acquired,
                                "LastAcquired": iss_acquired,
                                "LatestCover": cover,
                                "IssueDate": issuedate,
                                "IssueStatus": "Open"
                            }
                            db.upsert("magazines", new_value_dict, control_value_dict)
                        else:
                            # Set magazine_issuedate to issuedate of most recent issue we have
                            # Set latestcover to most recent issue cover
                            # Set magazine_added to acquired date of the earliest issue we have
                            # Set magazine_lastacquired to acquired date of most recent issue we have
                            # acquired dates are read from magazine file timestamps
                            new_value_dict = {"IssueStatus": "Open"}
                            if not magazineadded or iss_acquired < magazineadded:
                                new_value_dict["MagazineAdded"] = iss_acquired
                            if not maglastacquired or iss_acquired > maglastacquired:
                                new_value_dict["LastAcquired"] = iss_acquired
                            if not magissuedate or magissuedate == 'None' or issuedate >= magissuedate:
                                new_value_dict["IssueDate"] = issuedate
                                new_value_dict["LatestCover"] = cover
                            db.upsert("magazines", new_value_dict, control_value_dict)

            if CONFIG.get_bool('FULL_SCAN') and not onetitle:
                magcount = db.match("select count(*) from magazines")
                isscount = db.match("select count(*) from issues")
                logger.info(
                    f"Magazine scan complete, found {magcount['count(*)']} "
                    f"{plural(magcount['count(*)'], 'magazine')}, "
                    f"{isscount['count(*)']} {plural(isscount['count(*)'], 'issue')}")
            else:
                logger.info("Magazine scan complete")
        lazylibrarian.MAG_UPDATE = 0

    except Exception:
        lazylibrarian.MAG_UPDATE = 0
        logger.error(f'Unhandled exception in magazine_scan: {traceback.format_exc()}')
    finally:
        if 'MAGAZINE_SCAN' in threading.current_thread().name:
            threading.current_thread().name = 'WEBSERVER'
        db.close()


def format_issue_filename(base, mag_title, dateparts):
    logger = logging.getLogger(__name__)
    lang = 0
    cnt = 0
    while cnt < len(lazylibrarian.MONTHNAMES[0][0]):
        if lazylibrarian.MONTHNAMES[0][0][cnt] == CONFIG['DATE_LANG']:
            lang = cnt
            break
        cnt += 1

    if dateparts['months']:
        # month might be single or range
        startmonth = dateparts['months'][0]
        issuemonth = lazylibrarian.MONTHNAMES[0][startmonth][lang]
        issuemonthnum = startmonth
        if len(dateparts['months']) > 1:
            endmonth = dateparts['months'][-1]
            issuemonth = f"{issuemonth}-{lazylibrarian.MONTHNAMES[0][endmonth][lang]}"
            issuemonthnum = f"{startmonth}-{endmonth}"
    else:
        issuemonth = ''
        issuemonthnum = ''

    mydict = {"Title": mag_title,
              "IssueYear": str(dateparts['year']),
              "IssueNum": str(dateparts['issue']).zfill(4),
              "IssueDay": str(dateparts['day']).zfill(2),
              "IssueVol": str(dateparts['volume']).zfill(4),
              "IssueDate": str(dateparts['dbdate']),
              "IssueMonth": issuemonth,
              "IssueMonthNum": issuemonthnum
              }

    if base == CONFIG['MAG_DEST_FOLDER']:
        # No special requirements on folder name
        valid_format = True
        is_folder = True
    else:
        is_folder = False
        # We need to be able to identify the issue after renaming, so...
        # filename must contain minimum of
        # IssueDate
        # or year and issuenum
        # or year and month
        # or volume and issuenum
        # or title and issuenum
        valid_format = False
        if '$IssueDate' in base:
            valid_format = True
        if '$IssueYear' in base and '$IssueNum' in base and mydict['IssueYear'] and mydict['IssueNum']:
            valid_format = True
            if mydict['IssueDay'] and mydict['IssueDay'] != '01' and '$IssueDay' not in base:
                valid_format = False
        if '$Title' in base and '$IssueNum' in base and mydict['Title'] and mydict['IssueNum']:
            valid_format = True
        if '$IssueVol' in base and '$IssueNum' in base and mydict['IssueVol'] and mydict['IssueNum']:
            valid_format = True
        if '$IssueYear' in base and '$IssueMonthNum' in base and mydict['IssueYear'] and mydict['IssueMonthNum']:
            valid_format = True
        if '$IssueYear' in base and '$IssueMonth' in base and mydict['IssueYear'] and mydict['IssueMonth']:
            valid_format = True
            if mydict['IssueDay'] and mydict['IssueDay'] != '01' and '$IssueDay' not in base:
                valid_format = False

    if valid_format:
        issue_name = replacevars(base, mydict)
    else:
        logger.debug(f"Invalid format {base}:{mag_title}:{dateparts}")
        issue_name = f"{mag_title} - {dateparts['dbdate']}"
        # issue_name = unaccented(issue_name, only_ascii=False)
        issue_name = sanitize(issue_name, is_folder)
    return issue_name


def get_dateparts(title_or_issue, datetype=''):
    # Magazine names have many different styles of date
    # These are the ones we can currently match...
    # 1 MonthName MonthName YYYY (bi-monthly just use first month as date)
    # 2 nn, MonthName YYYY  where nn is an assumed issue number (use issue OR month with/without year)
    # 3 DD MonthName YYYY or DD MM YYYY (daily, weekly, bi-weekly, monthly)
    # 4 MonthName YYYY (monthly)
    # 5 MonthName DD YYYY or MonthName DD, YYYY (daily, weekly, bi-weekly, monthly)
    # 6 YYYY MM DD or YYYY MonthName DD (daily, weekly, bi-weekly, monthly)
    # 7 YYYY MM or YYYY MonthName (monthly)
    # 8 Volume x Issue y in either order, with year
    # 9 Volume x Issue y in either order, without year
    # 10 Issue/No/Nr/Vol/# nn, YYYY (prepend year to zero filled issue number)
    # 11 Issue/No/Nr/Vol/# nn (no year found, hopefully rolls on year on year)
    # 12 nn YYYY issue number without Issue/No/Nr/Vol/# in front, or YYYY nn (nn could be issue or month number)
    # 13 issue and year as a single 6 digit string eg 222015 (some uploaders use this, reverse it to YYYYIIII)
    # 14 3 or more digit zero padded issue number eg 0063 (issue with no year)
    # 15 just a year (annual)
    # 16 to 18 internal issuedates used for filenames, YYYYIIII, VVVVIIII, YYYYVVVVIIII
    #
    logger = logging.getLogger(__name__)
    dic = {'.': ' ', '-': ' ', '/': ' ', '+': ' ', '_': ' ', '(': '', ')': '', '[': ' ', ']': ' ', '#': '# '}
    words = replace_all(title_or_issue, dic).split()
    issuenouns = get_list(CONFIG['ISSUE_NOUNS'])
    volumenouns = get_list(CONFIG['VOLUME_NOUNS'])
    nouns = issuenouns + volumenouns

    year = 0
    months = []
    day = 0
    issue = 0
    volume = 0
    style = 0
    month = 0
    mname = ''
    inoun = ''
    vnoun = ''

    # First, collect the parts we're sure of
    pos = 0
    while pos < len(words):
        if not year:
            year = check_year(words[pos])
        month = month2num(words[pos])
        if month:
            mname = words[pos]
            months.append(month)
        else:
            month_a, month_b = two_months(words[pos])
            # compound months as a single word eg AprilMay
            if month_a:
                months.append(month_a)
                months.append(month_b)
        if words[pos].lower().strip('.') in issuenouns:
            if pos + 1 < len(words):
                inoun = words[pos]
                pos += 1
                issue = check_int(words[pos], 0)
        elif words[pos].lower().strip('.') in volumenouns and pos + 1 < len(words):
            vnoun = words[pos]
            pos += 1
            volume = check_int(words[pos], 0)
        pos += 1

    months = sorted(set(months))
    if len(months) > 1:
        style = 1
    if months:
        month = months[0]
    pos = 0

    # Radio.Times.31.May-06.June.2025 should return 31 May 2025
    # and Radio.Times.08-14.November.2025 should return 08 November 2025
    if months:
        logger.debug(f"Months:[{months}] Words:[{words}]")
        while pos < len(words):
            if pos > 0 and month2num(words[pos]) == month:
                first = check_int(re.sub(r"\D", "", words[pos - 1]), 0)
                if first and first < 32:
                    style = 3
                    day = first
                if pos > 1:
                    first = check_int(re.sub(r"\D", "", words[pos - 2]), 0)
                    if first and first < 32:
                        style = 3
                        day = first
                break
            pos += 1

    if volume and issue:
        if year:
            style = 8
        else:
            style = 9

    # now check the single string compound dates
    pos = 0
    while pos < len(words):
        data = words[pos]
        if data.isdigit():
            if len(data) == 4 and check_year(data):  # YYYY
                year = int(data)
                # style = 15
            elif len(data) == 6:
                if check_year(data[:4]):  # YYYYMM
                    year = int(data[:4])
                    months.append(int(data[4:]))
                    issue = int(data[4:])
                    style = 13
                elif check_year(data[2:]):  # MMYYYY
                    year = int(data[2:])
                    months.append(int(data[:2]))
                    issue = int(data[:2])
                    style = 13
            elif len(data) == 8:
                if check_year(data[:4]):  # YYYYIIII
                    year = int(data[:4])
                    issue = int(data[4:])
                    style = 16
                else:
                    volume = int(data[:4])  # VVVVIIII
                    issue = int(data[4:])
                    style = 17
            elif len(data) == 12:  # YYYYVVVVIIII
                year = int(data[:4])
                volume = int(data[4:8])
                issue = int(data[8:])
                style = 18
            elif len(data) > 2:
                issue = int(data)
                # style = 14
        pos += 1

    dateparts = {"year": year, "months": months, "day": day, "issue": issue, "volume": volume,
                 "month": month, "mname": mname, "inoun": inoun, "vnoun": vnoun, "style": style}

    if not dateparts['style']:
        # now the more complicated positional styles
        # 2 nn, MonthName YYYY  where nn is an assumed issue number (use issue OR month with/without year)
        # 3 DD MonthName YYYY (daily, weekly, bi-weekly, monthly)
        # 4 MonthName YYYY (monthly)
        # 5 MonthName DD YYYY or MonthName DD, YYYY (daily, weekly, bi-weekly, monthly)
        # 6 YYYY MM DD or YYYY MonthName DD (daily, weekly, bi-weekly, monthly)
        # 7 YYYY MM or YYYY MonthName (monthly)
        # 10 Issue/No/Nr/Vol/# nn, YYYY  or YYYY nn
        # 11 Issue/No/Nr/Vol/# nn (no year found, hopefully rolls on year on year)
        # 12 nn YYYY issue number without Issue/No/Nr/Vol/# in front, or YYYY nn (nn could be issue or month number)
        pos = 0
        while pos < len(words):
            year = check_year(words[pos])
            if year and pos:
                month = month2num(words[pos - 1])
                if month:
                    if pos > 1:
                        day = check_int(re.sub(r"\D", "", words[pos - 2]), 0)
                        if pos > 2 and words[pos - 3].lower().strip('.') in issuenouns:
                            dateparts['issue'] = day
                            dateparts['inoun'] = words[pos - 3]
                            dateparts['style'] = 10
                            break
                        if pos > 2 and words[pos - 3].lower().strip('.') in volumenouns:
                            dateparts['volume'] = day
                            dateparts['vnoun'] = words[pos - 3]
                            dateparts['style'] = 10
                            break
                        if day > 31:  # probably issue/volume number nn
                            if 'I' in datetype:
                                dateparts['issue'] = day
                                dateparts['style'] = 10
                                break
                            if 'V' in datetype:
                                dateparts['volume'] = day
                                dateparts['style'] = 10
                                break
                            dateparts['issue'] = day
                            dateparts['style'] = 2
                            break
                        if day:
                            dateparts['style'] = 3
                            dateparts['day'] = day
                            break
                        dateparts['style'] = 4
                        dateparts['day'] = 1
                        break
                    dateparts['style'] = 4
                    dateparts['day'] = 1
                    break
            pos += 1

        # MonthName DD YYYY or MonthName DD, YYYY
        if not dateparts['style']:
            pos = 0
            while pos < len(words):
                year = check_year(words[pos])
                if year and (pos > 1):
                    month = month2num(words[pos - 2])
                    if month:
                        day = check_int(re.sub(r"\D", "", words[pos - 1]), 0)
                        try:
                            _ = datetime.date(year, month, day)
                            dateparts['year'] = year
                            dateparts['month'] = month
                            dateparts['day'] = day
                            dateparts['style'] = 5
                            break
                        except (ValueError, OverflowError):
                            pass
                pos += 1

        # YYYY MM_or_MonthName or YYYY MM_or_MonthName DD
        if not dateparts['style']:
            pos = 0
            while pos < len(words):
                year = check_year(words[pos])
                if year and pos + 1 < len(words):
                    month = month2num(words[pos + 1])
                    if not month:
                        month = check_int(words[pos + 1], 0)
                        if month > 12:
                            month = 0
                    if month:
                        if pos + 2 < len(words):
                            day = check_int(re.sub(r"\D", "", words[pos + 2]), 0)
                            if day:
                                style = 6
                            else:
                                day = 1
                                style = 7
                        else:
                            day = 1
                            style = 7
                        try:
                            _ = datetime.date(year, month, day)
                            dateparts['year'] = year
                            dateparts['month'] = month
                            if not dateparts['months']:
                                dateparts['months'].append(month)
                            dateparts['day'] = day
                            dateparts['style'] = style
                        except (ValueError, OverflowError):
                            dateparts['issue'] = day
                            dateparts['day'] = 0
                            dateparts['style'] = 2
                pos += 1

        # Issue/No/Nr/Vol/# nn with/without year in any position
        if not dateparts['style']:
            pos = 0
            while pos < len(words):
                # might be "Vol.3" or "#12" with no space between noun and number
                splitted = re.split(r'(\d+)', words[pos].lower())
                if splitted[0].strip('.') in nouns:
                    if len(splitted) > 1:
                        issue = check_int(splitted[1], 0)
                        if issue:
                            dateparts['issue'] = issue
                            if dateparts['year']:
                                dateparts['style'] = 10  # Issue/No/Nr/Vol nn, YYYY
                            else:
                                dateparts['style'] = 11  # Issue/No/Nr/Vol nn
                            break
                    if pos + 1 < len(words):
                        issue = check_int(words[pos + 1], 0)
                        if issue:
                            dateparts['issue'] = issue
                            if dateparts['year']:
                                dateparts['style'] = 10  # Issue/No/Nr/Vol nn, YYYY
                            else:
                                dateparts['style'] = 11  # Issue/No/Nr/Vol nn
                            break
                        # No. 19.2 -> 2019 02 but 02 might be a number, not a month
                        issue = words[pos + 1]
                        if issue.count('.') == 1 and issue.replace('.', '').isdigit():
                            year, issue = issue.split('.')
                            if len(year) == 2:
                                year = f'20{year}'
                            if len(issue) == 1:
                                issue = f'0{issue}'
                            if len(year) == 4 and len(issue) == 2:
                                dateparts['year'] = int(year)
                                dateparts['issue'] = int(issue)
                                dateparts['style'] = 10
                                break
                pos += 1

        # nn YYYY issue number without "Nr" before it, or YYYY nn
        # or DD MM YYYY (datestyle 3) or MM DD YYYY
        if not dateparts['style'] and dateparts['year']:
            pos = 1
            while pos < len(words):
                if check_year(words[pos]):
                    if words[pos - 1].isdigit():
                        if pos > 1 and words[pos - 2].isdigit():
                            # we assume dd mm yyyy rather than mm dd yyyy
                            m = int(words[pos - 1])
                            d = int(words[pos - 2])
                            # unless overridden by per-title config
                            if "MDY" in datetype:
                                m, d = d, m
                            # if only one of the numbers is < 13 assume month
                            if m < 13:
                                dateparts['months'] = [m]
                                dateparts['day'] = d
                                dateparts['style'] = 3
                            elif d < 13:
                                dateparts['months'] = [d]
                                dateparts['day'] = m
                                dateparts['style'] = 3
                        if not dateparts['style']:
                            dateparts['issue'] = int(words[pos - 1])
                            dateparts['style'] = 12
                        break
                    if pos + 1 < len(words) and words[pos + 1].isdigit():
                        dateparts['issue'] = int(words[pos + 1])
                        dateparts['style'] = 12
                        break
                pos += 1

    if dateparts['months']:
        dateparts['month'] = dateparts['months'][0]
    else:
        dateparts['month'] = 0

    if dateparts['year'] and not dateparts['style']:
        dateparts['style'] = 15

    if dateparts['issue'] and not dateparts['style']:
        dateparts['style'] = 14

    if not dateparts['style'] and datetype == 'I':
        # if we haven't found a matching datestyle, and we're expecting just an issuenum
        # and if only one word is digits, assume that's the issuenum
        numbers = [word for word in words if word.isdigit()]
        if len(numbers) == 1:
            dateparts['issue'] = numbers[0]
            dateparts['style'] = 14

    datetype_ok = True
    if dateparts['year'] and dateparts['month'] and dateparts['day']:
        # Check the components are a valid date, checks days in month and leap years
        try:
            _ = datetime.datetime.strptime(f"{dateparts['year']}-{dateparts['month']:02d}-{dateparts['day']:02d}", "%Y-%m-%d")
        except ValueError:
            datetype_ok = False

    if datetype_ok and datetype and dateparts['style']:
        # check all wanted parts are in the result
        if 'M' in datetype and (dateparts['style'] not in [1, 2, 3, 4, 5, 6, 7, 12] or not dateparts['month']):
            datetype_ok = False
        if 'D' in datetype and (dateparts['style'] not in [3, 5, 6] or not dateparts['day']):
            datetype_ok = False
        if 'MM' in datetype and (dateparts['style'] not in [1] or len(dateparts['months']) < 2):
            datetype_ok = False
        if 'V' in datetype and (dateparts['style'] not in [2, 8, 9, 10, 11, 12, 13, 14, 17, 18]
                                or not dateparts['volume']):
            datetype_ok = False
        if 'I' in datetype and (dateparts['style'] not in [2, 10, 11, 12, 13, 14, 16, 17, 18]
                                or not dateparts['issue']):
            datetype_ok = False
        if 'Y' in datetype and (dateparts['style'] not in [1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 13, 15, 16, 18]
                                or not dateparts['year']):
            datetype_ok = False

    if not datetype_ok:
        dateparts['style'] = 0
        dateparts['dbdate'] = ''
    else:
        if dateparts['issue'] and ('I' in datetype or dateparts['inoun']):
            issuenum = str(dateparts['issue']).zfill(4)
            if dateparts['year']:
                issuenum = f"{dateparts['year']}{issuenum}"
        else:
            if not dateparts['day']:
                dateparts['day'] = 1
            if dateparts['style'] == 12:
                issuenum = f"{dateparts['year']}{dateparts['issue']:04d}"
            elif dateparts['style'] == 14:
                issuenum = f"{dateparts['issue']:04d}"
            elif dateparts['style'] == 15:
                issuenum = f"{dateparts['year']}"
            elif dateparts['style'] == 16:
                issuenum = f"{dateparts['year']}{dateparts['issue']:04d}"
            elif dateparts['style'] == 17:
                issuenum = f"{dateparts['volume']:04d}{dateparts['issue']:04d}"
            elif dateparts['style'] == 18:
                issuenum = f"{dateparts['year']}{dateparts['volume']:04d}{dateparts['issue']:04d}"
            else:
                issuenum = f"{dateparts['year']}-{dateparts['month']:02d}-{dateparts['day']:02d}"
        dateparts['dbdate'] = issuenum

    return dateparts


def rename_issue(issueid, tags=None):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    if not issueid:
        msg = "No issueID provided"
        logger.warning(msg)
        db.close()
        return '', msg
    match = db.match('select Title,IssueDate,IssueFile from issues where issueid=?', (issueid,))
    if not match:
        msg = f"Invalid issueID {issueid}"
        logger.warning(msg)
        db.close()
        return '', msg

    parts = get_dateparts(match['IssueDate'])
    new_name = format_issue_filename(CONFIG['MAG_DEST_FILE'], match['Title'], parts)
    old_name, extn = splitext(os.path.basename(match['IssueFile']))
    old_folder = os.path.dirname(match['IssueFile'])
    new_folder = format_issue_filename(CONFIG['MAG_DEST_FOLDER'], match['Title'], parts)
    if CONFIG.get_bool('MAG_RELATIVE'):
        dest_dir = get_directory('eBook')
        new_folder = stripspaces(os.path.join(dest_dir, new_folder))

    new_filename = os.path.join(new_folder, new_name + extn)

    if new_filename == match['IssueFile']:
        logger.debug(f"Filename {match['IssueFile']} is unchanged")
        return match['IssueFile'], ''

    # create dest folder if required
    if old_folder != new_folder and not path_isdir(new_folder):
        if not make_dirs(new_folder):
            msg = f"Unable to create target folder {new_folder}"
            logger.error(msg)
            db.close()
            return '', msg

        ignorefile = os.path.join(new_folder, '.ll_ignore')
        try:
            with open(syspath(ignorefile), 'w', encoding='utf-8') as f:
                f.write("magazine")
        except OSError as e:
            logger.warning(f"Unable to create/write to ignorefile: {str(e)}")

    # rename opf, jpg, then issue
    for extension in ['.jpg', '.opf', extn]:
        src = os.path.join(old_folder, old_name + extension)
        dst = os.path.join(new_folder, new_name + extension)
        # check for windows case-insensitive
        if ((os.name == 'nt' and src.lower() != dst.lower()) or
                (os.name != 'nt' and src != dst)):
            if path_exists(src):
                _ = safe_move(src, dst)
            else:
                msg = f"File not found: {src}"
                logger.warning(msg)
                if extension == extn:
                    db.close()
                    return '', msg

    remove_if_empty(old_folder)
    # update issuefile in database
    new_filename = os.path.join(new_folder, new_name + extn)
    db.action("UPDATE issues SET IssueFile=? WHERE IssueID=?", (new_filename, issueid))

    if CONFIG.get_bool('TAG_PDF'):
        logger.debug(f"Tagging {new_filename}")
        _ = write_pdf_tags(new_filename, match['Title'], match['IssueDate'], tags=tags)

    if CONFIG.get_bool('IMP_MAGOPF'):
        logger.debug(f"Writing opf for {new_filename}")
        entry = db.match('SELECT Language FROM magazines where Title=?', (match['Title'],))
        _, _ = lazylibrarian.metadata_opf.create_mag_opf(new_filename, match['Title'],
                                                         match['IssueDate'], issueid, language=entry[0],
                                                         overwrite=True)
    db.close()
    return new_filename, ''


def remove_if_empty(foldername, booktype='mag'):
    logger = logging.getLogger(__name__)
    # if no magazine issues left in the folder, delete it
    # (removes any trailing cover images, opf, ignorefile etc)
    if not book_file(foldername, booktype=booktype, config=CONFIG, recurse=True):
        logger.debug(f"Removing empty directory {foldername}")
        remove_dir(foldername, remove_contents=True)
        parent = os.path.dirname(foldername)
        # if parent folder is now empty, delete that too, issue might have been in an issue folder
        if not book_file(parent, booktype=booktype, config=CONFIG, recurse=True):
            logger.debug(f"Removing empty parent directory {parent}")
            remove_dir(parent, remove_contents=True)
