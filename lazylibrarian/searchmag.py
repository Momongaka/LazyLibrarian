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
import threading
import time
import traceback

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.config2 import CONFIG
from lazylibrarian.downloadmethods import direct_dl_method, nzb_dl_method, tor_dl_method
from lazylibrarian.formatter import (
    age,
    check_int,
    datecompare,
    get_list,
    now,
    nzbdate2format,
    plural,
    replace_all,
    thread_name,
    unaccented,
)
from lazylibrarian.magazinescan import get_dateparts
from lazylibrarian.notifiers import custom_notify_snatch, notify_snatch
from lazylibrarian.providers import (
    iterate_over_direct_sites,
    iterate_over_irc_sites,
    iterate_over_rss_sites,
    iterate_over_torrent_sites,
    iterate_over_znab_sites,
)
from lazylibrarian.scheduling import SchedulerCommand, schedule_job
from lazylibrarian.telemetry import TELEMETRY


def cron_search_magazines():
    if 'SEARCHALLMAG' not in [n.name for n in list(threading.enumerate())]:
        search_magazines()


def search_magazines(mags=None, reset=False, backissues=False):
    # produce a list of magazines to search for, then search all enabled providers
    TELEMETRY.record_usage_data('Search/Magazine')
    logger = logging.getLogger(__name__)
    searchinglogger = logging.getLogger('special.searching')
    threadname = thread_name()
    if "Thread" in threadname:
        if not mags:
            thread_name("SEARCHALLMAG")
            threadname = "SEARCHALLMAG"
        else:
            thread_name("SEARCHMAG")
    db = database.DBConnection()
    # noinspection PyBroadException
    try:

        db.upsert("jobs", {"Start": time.time()}, {"Name": thread_name()})
        searchlist = []

        if not mags:  # backlog search
            searchmags = db.select("SELECT Title,Regex,DateType,LastAcquired,IssueDate from magazines "
                                   "WHERE Status='Active'")
        else:
            searchmags = []
            for magazine in mags:
                searchinglogger.debug(str(magazine))
                searchmags_temp = db.select("SELECT Title,Regex,DateType,LastAcquired,IssueDate from magazines "
                                            "WHERE Title=? AND Status='Active'", (magazine['bookid'],))
                for terms in searchmags_temp:
                    searchmags.append(terms)

        if len(searchmags) == 0:
            logger.debug("No magazines to search for")
            db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})
            thread_name("WEBSERVER")
            return

        logger.info(f"Searching for {len(searchmags)} {plural(len(searchmags), 'magazine')}")

        for searchmag in searchmags:
            bookid = searchmag['Title']
            searchterms = get_list(searchmag['Regex'], ',')
            datetype = searchmag['DateType']
            if not datetype:
                datetype = ''

            if not searchterms:
                dic = {'...': '', ' & ': ' and ', ' + ': ' plus ', ' = ': ' ', '?': '', '$': 's',
                       '"': '', ',': '', '*': ''}
                searchterm = replace_all(searchmag['Title'], dic)
                searchterms = [re.sub(r'[.\-/]', ' ', searchterm)]  # single item in a list

            for searchterm in searchterms:
                searchlist.append({"bookid": bookid, "searchterm": searchterm, "datetype": datetype,
                                   "library": 'magazine'})

        if not searchlist:
            logger.warning('There is nothing to search for.  Mark some magazines as active.')

        for book in searchlist:
            if lazylibrarian.STOPTHREADS and threadname == "SEARCHALLMAG":
                logger.debug(f"Aborting {threadname}")
                break

            resultlist = []

            if CONFIG.use_nzb():
                resultlist, nproviders = iterate_over_znab_sites(book, 'mag')
                if not nproviders:
                    # don't nag. Show warning message no more than every 20 mins
                    timenow = int(time.time())
                    if check_int(lazylibrarian.TIMERS['NO_NZB_MSG'], 0) + 1200 < timenow:
                        logger.warning('No nzb providers are available. Check config and blocklist')
                        lazylibrarian.TIMERS['NO_NZB_MSG'] = timenow
                else:
                    # prefer larger nzb over smaller ones which may be par2 repair files?
                    resultlist = sorted(resultlist, key=lambda d: check_int(d['nzbsize'], 0), reverse=True)

            if CONFIG.use_direct():
                dir_resultlist, nproviders = iterate_over_direct_sites(book, 'mag')
                if not nproviders:
                    # don't nag. Show warning message no more than every 20 mins
                    timenow = int(time.time())
                    if check_int(lazylibrarian.TIMERS['NO_DIRECT_MSG'], 0) + 1200 < timenow:
                        logger.warning('No direct providers are available. Check config and blocklist')
                        lazylibrarian.TIMERS['NO_DIRECT_MSG'] = timenow

                if dir_resultlist:
                    for item in dir_resultlist:  # reformat the results so they look like nzbs
                        resultlist.append({
                            'bookid': item['bookid'],
                            'nzbprov': item['tor_prov'],
                            'nzbtitle': item['tor_title'],
                            'nzburl': item['tor_url'],
                            'nzbdate': 'Fri, 01 Jan 1970 00:00:00 +0100',  # fake date as none returned
                            'nzbsize': item['tor_size'],
                            'nzbmode': 'direct'
                        })

            if CONFIG.use_irc():
                irc_resultlist, nproviders = iterate_over_irc_sites(book, 'mag')
                if not nproviders:
                    # don't nag. Show warning message no more than every 20 mins
                    timenow = int(time.time())
                    if check_int(lazylibrarian.TIMERS['NO_IRC_MSG'], 0) + 1200 < timenow:
                        logger.warning('No irc providers are available. Check config and blocklist')
                        lazylibrarian.TIMERS['NO_IRC_MSG'] = timenow

                if irc_resultlist:
                    for item in irc_resultlist:  # reformat the results so they look like nzbs
                        resultlist.append({
                            'bookid': item['bookid'],
                            'nzbprov': item['tor_prov'],
                            'nzbtitle': item['tor_title'],
                            'nzburl': item['tor_url'],
                            'nzbdate': 'Fri, 01 Jan 1970 00:00:00 +0100',  # fake date as none returned
                            'nzbsize': item['tor_size'],
                            'nzbmode': 'irc'
                        })

            if CONFIG.use_tor():
                tor_resultlist, nproviders = iterate_over_torrent_sites(book, 'mag')
                if not nproviders:
                    # don't nag. Show warning message no more than every 20 mins
                    timenow = int(time.time())
                    if check_int(lazylibrarian.TIMERS['NO_TOR_MSG'], 0) + 1200 < timenow:
                        logger.warning('No tor providers are available. Check config and blocklist')
                        lazylibrarian.TIMERS['NO_TOR_MSG'] = timenow

                if tor_resultlist:
                    for item in tor_resultlist:  # reformat the torrent results so they look like nzbs
                        resultlist.append({
                            'bookid': item['bookid'],
                            'nzbprov': item['tor_prov'],
                            'nzbtitle': item['tor_title'],
                            'nzburl': item['tor_url'],
                            'nzbdate': 'Fri, 01 Jan 1970 00:00:00 +0100',  # fake date as none returned from torrents
                            'nzbsize': item['tor_size'],
                            'nzbmode': 'torrent'
                        })

            if CONFIG.use_rss():
                rss_resultlist, nproviders, dltypes = iterate_over_rss_sites()
                if not nproviders or 'M' not in dltypes:
                    # don't nag. Show warning message no more than every 20 mins
                    timenow = int(time.time())
                    if check_int(lazylibrarian.TIMERS['NO_RSS_MSG'], 0) + 1200 < timenow:
                        logger.warning('No rss providers are available. Check config and blocklist')
                        lazylibrarian.TIMERS['NO_RSS_MSG'] = timenow

                if rss_resultlist:
                    for item in rss_resultlist:  # reformat the rss results so they look like nzbs
                        if 'M' in item['types']:
                            resultlist.append({
                                'bookid': book['bookid'],
                                'nzbprov': item['tor_prov'],
                                'nzbtitle': item['tor_title'],
                                'nzburl': item['tor_url'],
                                'nzbdate': item['tor_date'],  # may be fake date as none returned from rss torrents
                                'nzbsize': item['tor_size'],
                                'nzbmode': item['tor_type']
                            })

            if not resultlist:
                logger.debug(f"No results for magazine {book['searchterm']}")
            else:
                bad_name = 0
                bad_date = 0
                old_date = 0
                rejects = 0
                total_nzbs = 0
                new_date = 0
                new_list = []
                maglist = []
                issues = []
                bookid = ''
                wanted_list = []
                res = db.match("SELECT Regex from magazines WHERE Title=? AND Status='Active'", (book['bookid'],))
                searchterms = get_list(res['Regex'], ',')
                for nzb in resultlist:
                    total_nzbs += 1
                    bookid = nzb['bookid']
                    nzbtitle = nzb['nzbtitle']
                    nzbtitle = nzbtitle.replace('"', '').replace("'", "")  # suppress " in titles
                    nzburl = nzb['nzburl']
                    nzbprov = nzb['nzbprov']
                    nzbdate_temp = nzb['nzbdate']
                    nzbsize_temp = nzb['nzbsize']
                    nzbsize_temp = check_int(nzbsize_temp, 1000)  # not all torrents returned by torznab have a size
                    nzbsize = round(float(nzbsize_temp) / 1048576, 2)
                    nzbdate = nzbdate2format(nzbdate_temp)
                    nzbmode = nzb['nzbmode']

                    # Need to make sure that substrings of magazine titles don't get found
                    # (e.g. Maxim USA will find Maximum PC USA) so split into "words"
                    dic = {'.': ' ', '-': ' ', '/': ' ', '_': ' ', '(': '', ')': '', '[': ' ', ']': ' ',
                           '#': '# ', '!': ' '}
                    nzbtitle_formatted = replace_all(nzbtitle, dic)

                    # remove extra spaces if they're in a row
                    nzbtitle_formatted = " ".join(nzbtitle_formatted.split())
                    nzbtitle_exploded = nzbtitle_formatted.split()

                    results = db.match('SELECT * from magazines WHERE Title=? COLLATE NOCASE', (bookid,))
                    if not results:
                        logger.debug(f'Magazine [{nzbtitle}] does not match search term [{bookid}].')
                        bad_name += 1
                    else:
                        rejected = False
                        maxsize = CONFIG.get_int('REJECT_MAGSIZE')
                        if maxsize and nzbsize > maxsize:
                            logger.debug(f"Rejecting {nzbtitle}, too large ({nzbsize}Mb)")
                            rejected = True

                        if not rejected:
                            minsize = CONFIG.get_int('REJECT_MAGMIN')
                            if minsize and nzbsize < minsize:
                                logger.debug(f"Rejecting {nzbtitle}, too small ({nzbsize}Mb)")
                                rejected = True

                        if not rejected:
                            bookid_exploded = replace_all(bookid, dic).split()

                            # Check nzb has magazine title and a date/issue nr
                            # eg The MagPI July 2015
                            if len(nzbtitle_exploded) > len(bookid_exploded):
                                # needs to be longer as it has to include a date
                                # check all the words in the mag title are in the nzbtitle
                                # or one of the regex options for this title
                                if not searchterms:
                                    searchterms = [bookid]
                                wlist = []
                                for word in nzbtitle_exploded:
                                    if word == '&' or word == '+':
                                        word = 'and'
                                    wlist.append(word.lower())
                                for term in searchterms:
                                    rejected = False
                                    term_exploded = replace_all(term, dic).split()
                                    for word in term_exploded:
                                        if word == '&' or word == '+':
                                            word = 'and'
                                        if word.lower() not in wlist:
                                            logger.debug(f"Rejecting {nzbtitle}, missing [{word}]")
                                            rejected = True
                                            break
                                    if not rejected:
                                        break

                                if rejected:
                                    logger.debug(
                                        f"Magazine title match failed {bookid} for {nzbtitle_formatted}")
                                else:
                                    logger.debug(
                                        f"Magazine title matched {bookid} for {nzbtitle_formatted}")
                            else:
                                logger.debug(f"Magazine name too short ({len(nzbtitle_exploded)})")
                                rejected = True

                        if not rejected and CONFIG.get_bool('BLACKLIST_FAILED'):
                            blocked = db.match("SELECT * from wanted WHERE NZBurl=? and Status='Failed'", (nzburl,))
                            if blocked:
                                logger.debug(f"Rejecting {nzbtitle_formatted}, blacklisted at {blocked['NZBprov']}")
                                rejected = True

                        if not rejected and CONFIG.get_bool('BLACKLIST_PROCESSED'):
                            blocked = db.match('SELECT * from wanted WHERE NZBurl=?', (nzburl,))
                            if blocked:
                                logger.debug(f"Rejecting {nzbtitle_formatted}, blacklisted at {blocked['NZBprov']}")
                                rejected = True

                        if not rejected:
                            reject_list = get_list(results['Reject'])
                            reject_list += get_list(CONFIG['REJECT_MAGS'], ',')
                            lower_title = unaccented(nzbtitle_formatted, only_ascii=False).lower().split()
                            lower_bookid = unaccented(bookid, only_ascii=False).lower().split()
                            if reject_list:
                                searchinglogger.debug(f'Reject: {reject_list}')
                                searchinglogger.debug(f'Title: {lower_title}')
                                searchinglogger.debug(f'Bookid: {lower_bookid}')
                            for word in reject_list:
                                word = unaccented(word).lower()
                                if word in lower_title and word not in lower_bookid:
                                    rejected = True
                                    logger.debug(f"Rejecting {nzbtitle_formatted}, contains {word}")
                                    break
                            if not rejected:
                                reject_list = get_list(results['Reject'])
                                if '*' in reject_list:  # strict rejection mode, no extraneous words
                                    nouns = get_list(CONFIG['ISSUE_NOUNS'])
                                    nouns.extend(get_list(CONFIG['VOLUME_NOUNS']))
                                    nouns.extend(get_list(CONFIG['MAG_NOUNS']))
                                    nouns.extend(get_list(CONFIG['MAG_TYPE']))
                                    nouns.extend(list(lazylibrarian.SEASONS.keys()))
                                    nouns = set(nouns)
                                    for word in lower_title:
                                        if word not in lower_bookid and word not in nouns and not word.isdigit():
                                            valid = False
                                            for f in range(1, 13):
                                                if (word in lazylibrarian.MONTHNAMES[0][f] or
                                                        unaccented(word).lower() in lazylibrarian.MONTHNAMES[1][f]):
                                                    valid = True
                                                    break
                                            if not valid:
                                                rejected = True
                                                logger.debug(
                                                    f"Rejecting {nzbtitle_formatted}, strict, contains {word}")
                                                break
                        if rejected:
                            rejects += 1
                        else:
                            datetype = book['datetype']
                            dateparts = get_dateparts(nzbtitle_formatted, datetype=datetype)
                            if dateparts['style']:
                                logger.debug(f"Match {dateparts['dbdate']} (datestyle {dateparts['style']}) "
                                             f"for {nzbtitle_formatted}, [{datetype}]")
                            else:
                                logger.debug(
                                    f'Magazine {nzbtitle_formatted} not in a recognised date format [{datetype}]')
                                bad_date += 1
                            # wanted issues go into wanted table marked "Wanted"
                            #  the rest into pastissues table marked "Skipped" or "Have"
                            insert_table = "pastissues"
                            comp_date = 0
                            issuedate = "1970-01-01"  # provide a fake date for bad-date issues
                            if dateparts.get('dbdate'):
                                issuedate = dateparts['dbdate']
                                control_date = results['IssueDate']
                                logger.debug(f"Latest IssueDate: [{control_date}]")
                                if not control_date:  # we haven't got any copies of this magazine yet
                                    # get a rough time just over MAX_AGE days ago to compare to, in format yyyy-mm-dd
                                    # could perhaps calc differently for weekly, biweekly etc.
                                    # For magazines with only an issue number use current year as we can't tell age
                                    start_time = time.time()
                                    start_time -= CONFIG.get_int('MAG_AGE') * 24 * 60 * 60
                                    if start_time < 0:  # limit of unixtime (1st Jan 1970)
                                        start_time = 0
                                    if str(issuedate).isdigit():
                                        control_date = time.strftime("%Y0000", time.localtime(start_time))
                                        logger.debug(f'Magazine comparing issue numbers to {control_date}')
                                    elif re.match(r'\d+-\d\d-\d\d', str(issuedate)):
                                        control_date = time.strftime("%Y-%m-%d", time.localtime(start_time))
                                        logger.debug(f'Magazine date comparing to {control_date}')
                                    else:
                                        logger.debug(f'Magazine unable to find comparison type [{issuedate}]')
                                        control_date = 0

                                if str(control_date).isdigit() and str(issuedate).isdigit():
                                    if not control_date:
                                        comp_date = CONFIG.get_int('MAG_AGE') - age(nzbdate)
                                    else:
                                        comp_date = int(issuedate) - int(control_date)
                                elif re.match(r'\d+-\d\d-\d\d', str(control_date)) and \
                                        re.match(r'\d+-\d\d-\d\d', str(issuedate)):
                                    # only grab a copy if it's newer than the most recent we have,
                                    # or newer than a month ago if we have none
                                    # unless backissues is True
                                    comp_date = datecompare(issuedate, control_date)
                                elif backissues:
                                    comp_date = 1
                                else:
                                    # invalid comparison of date and issue number
                                    comp_date = 0
                                    if re.match(r'\d+-\d\d-\d\d', str(control_date)):
                                        if dateparts['style'] > 9 and dateparts['year']:
                                            # we assumed it was an issue number, but it could be a date
                                            year = check_int(dateparts['year'], 0)
                                            if dateparts['style'] in [10, 12, 13]:
                                                issuedate = int(issuedate[:4])
                                            issuenum = check_int(issuedate, 0)
                                            if year and 1 <= issuenum <= 12:
                                                issuedate = f"{year:04d}-{issuenum:02d}-01"
                                                comp_date = datecompare(issuedate, control_date)
                                        if not comp_date:
                                            logger.debug(f'Magazine {nzbtitle_formatted} failed: Expecting a date')
                                    else:
                                        logger.debug(f'Magazine {nzbtitle_formatted} failed: Expecting issue number')
                                    if not comp_date:
                                        bad_date += 1
                                        issuedate = "1970-01-01"

                            if issuedate == "1970-01-01":
                                logger.debug(f'This issue of {nzbtitle_formatted} is unknown age; skipping.')
                            elif not dateparts['style']:
                                logger.debug(f'This issue of {nzbtitle_formatted} not in a wanted date format.')
                            elif comp_date > 0:
                                # keep track of what we're going to download, so we don't download dupes
                                new_date += 1
                                new_list.append(issuedate)
                                issue = f"{bookid},{issuedate}"
                                if issue not in issues:
                                    maglist.append({
                                        'bookid': bookid,
                                        'nzbprov': nzbprov,
                                        'nzbtitle': nzbtitle,
                                        'nzburl': nzburl,
                                        'nzbmode': nzbmode
                                    })
                                    logger.debug(f'This issue ({issuedate}) of {nzbtitle_formatted} is new, downloading')
                                    issues.append(issue)
                                    logger.debug(f'Magazine request number {len(issues)}')
                                    searchinglogger.debug(str(issues))
                                    insert_table = "wanted"
                                    nzbdate = now()  # when we asked for it
                                else:
                                    logger.debug(f'This issue of {issue} is already flagged for download; skipping')
                                    continue
                            else:
                                searchinglogger.debug(f'This issue of {nzbtitle_formatted} is old; skipping.')
                                old_date += 1

                            mag_entry = db.match('SELECT * from issues WHERE title=? and issuedate=?',
                                                 (bookid, issuedate))
                            if mag_entry:
                                logger.info(f'This issue of {nzbtitle_formatted} is already downloaded; skipping')
                                continue
                            # store only the _new_ matching results
                            #  Don't add a new entry if this issue has been found on an earlier search
                            #  and status has been user-set ( we only delete the "Skipped" ones )
                            #  In "wanted" table it might be already snatched/downloading/processing
                            mag_entry = db.match(f'SELECT Status from {insert_table} WHERE NZBtitle=? and NZBprov=?',
                                                 (nzbtitle, nzbprov))
                            if mag_entry and insert_table != 'wanted':
                                logger.info(
                                    f"{nzbtitle} is already in {insert_table} marked {mag_entry['Status']}; skipping")
                                continue
                            control_value_dict = {
                                "NZBtitle": nzbtitle,
                                "NZBprov": nzbprov
                            }
                            new_value_dict = {
                                "NZBurl": nzburl,
                                "BookID": bookid,
                                "NZBdate": nzbdate,
                                "AuxInfo": issuedate,
                                "Status": "Wanted",
                                "NZBsize": nzbsize,
                                "NZBmode": nzbmode
                            }
                            if insert_table == 'pastissues':
                                # try to mark ones we've already got
                                match = db.match("SELECT * from issues WHERE Title=? AND IssueDate=?",
                                                 (bookid, issuedate))
                                if match:
                                    new_value_dict["Status"] = "Have"
                                else:
                                    new_value_dict["Status"] = "Skipped"
                                new_value_dict["Added"] = int(time.time())
                                db.upsert(insert_table, new_value_dict, control_value_dict)
                                logger.info(f"Added {nzbtitle} to {insert_table} marked {new_value_dict['Status']}")
                            else:
                                wanted_list.append([new_value_dict, control_value_dict])

                if str(control_date).isdigit():
                    # for mags with issue number rather than date, use MAX_AGE as a counter of how many issues to grab
                    sorted_list = sorted(wanted_list, key=lambda x: x[0][3], reverse=True)  # sort on descending issuedate
                    logger.debug(CONFIG.get_int('MAG_AGE'))
                    for item in sorted_list:
                        logger.debug(f"{item[1][0]}:{item[0][3]}")
                    if CONFIG.get_int('MAG_AGE') and len(sorted_list) > CONFIG.get_int('MAG_AGE'):
                        wanted_list = sorted_list[:CONFIG.get_int('MAG_AGE')]  # and limit
                    else:
                        wanted_list = sorted_list

                for item in wanted_list:
                    new_value_dict = item[0]
                    control_value_dict = item[1]
                    db.upsert("wanted", new_value_dict, control_value_dict)
                    logger.info(f"Added {control_value_dict['NZBtitle']} to wanted marked {new_value_dict['Status']}")

                msg = f"Found {total_nzbs} {plural(total_nzbs, 'result')} for {bookid}. {new_date} new,{new_list}"
                msg += f' {old_date} old, {bad_date} fail date, {bad_name} fail name,'
                msg += f' {rejects} rejected: {len(wanted_list)} to download'
                logger.info(msg)

                threading.Thread(target=download_maglist, name='DL-MAGLIST', args=[maglist, 'pastissues']).start()

            time.sleep(CONFIG.get_int('SEARCH_RATELIMIT'))

        logger.info("Search for magazines complete")
        if reset:
            schedule_job(action=SchedulerCommand.RESTART, target='search_magazines')

    except Exception:
        logger.error(f'Unhandled exception in search_magazines: {traceback.format_exc()}')
    finally:
        db.upsert("jobs", {"Finish": time.time()}, {"Name": thread_name()})
        db.close()
        thread_name("WEBSERVER")


def download_maglist(maglist, table='wanted'):
    logger = logging.getLogger(__name__)
    snatched = 0
    db = database.DBConnection()
    try:
        for magazine in maglist:
            if magazine['nzbmode'] in ["torznab", "torrent", "magnet"]:
                snatch, res = tor_dl_method(
                    magazine['bookid'],
                    magazine['nzbtitle'],
                    magazine['nzburl'],
                    'magazine',
                    provider=magazine['nzbprov'])
            elif magazine['nzbmode'] == 'direct':
                snatch, res = direct_dl_method(
                    magazine['bookid'],
                    magazine['nzbtitle'],
                    magazine['nzburl'],
                    'magazine',
                    magazine['nzbprov'])
            elif magazine['nzbmode'] == 'nzb':
                snatch, res = nzb_dl_method(
                    magazine['bookid'],
                    magazine['nzbtitle'],
                    magazine['nzburl'],
                    'magazine')
            else:
                res = f"Unhandled NZBmode [{magazine['nzbmode']}] for {magazine['nzburl']}"
                logger.error(res)
                snatch = 0
            if snatch:
                snatched += 1
                if table == 'pastissues':
                    db.action("UPDATE pastissues set status=? WHERE NZBurl=?", ('Snatched', magazine["nzburl"]))
                logger.info(f"Downloading {magazine['nzbtitle']} from {magazine['nzbprov']}")
                custom_notify_snatch(f"{magazine['bookid']} {magazine['nzburl']}")
                notify_snatch(
                    f"Magazine {unaccented(magazine['nzbtitle'], only_ascii=False)} from "
                    f"{CONFIG.disp_name(magazine['nzbprov'])} at {now()}")
            else:
                db.action(f"UPDATE {table} SET status='Failed',DLResult=? WHERE NZBurl=?",
                          (res, magazine["nzburl"]))
    except Exception as e:
        logger.error(str(e))
    finally:
        db.close()
        if snatched:
            schedule_job(action=SchedulerCommand.START, target='PostProcessor')


def get_default_date(dateparts):
    # $V = Volume, zero filled, 4 digit
    # $v = Volume, no padding
    # $I = Issue, zero filled, 4 digit
    # $i = Issue, no padding
    # $Y = Year, 4 digit
    # $M = Month number, 2 digit
    # $m = Month name
    # $D = Day number, 2 digit
    layout = {
                1: "$m - $m $Y",
                2: "#$I, $m",
                3: "$D $m $Y",
                4: "$m $Y",
                5: "$m $D $Y",
                6: "$Y $M $D",
                7: "$Y $M",
                8: "Volume $v Issue $i $Y",
                9: "Volume $v Issue $i",
                10: "#$I $Y",
                11: "#$I",
                12: "#$I $Y",
                13: "$Y$I",
                14: "$I",
                15: "$Y",
                16: "$Y$I",
                17: "$V$I",
                18: "$Y$V$I"
            }

    if dateparts['style'] not in layout:
        return f"Invalid layout style {dateparts['style']}"

    preformat = layout[dateparts['style']]
    if dateparts['style'] in [2] and dateparts["year"]:
        preformat += ' $Y'
    if dateparts['style'] in [10, 11, 13] and not dateparts["issue"]:
        # we guessed issue, could be volume
        dateparts['issue'] = dateparts['volume']
    if dateparts['style'] in [12] and not dateparts["month"] and dateparts['issue'] < 13:
        # we guessed issue, could be month
        dateparts['month'] = dateparts['issue']

    if preformat.count('$m') == 2:
        # change string to start-end
        preformat = preformat.replace('$m', '$s', 1)
        preformat = preformat.replace('$m', '$e', 1)

    if preformat.count('$M') == 2:
        # change two months to start-end
        preformat = preformat.replace('$M', '$S', 1)
        preformat = preformat.replace('$M', '$E', 1)

    lang = 0
    cnt = 0
    while cnt < len(lazylibrarian.MONTHNAMES[0][0]):
        if lazylibrarian.MONTHNAMES[0][0][cnt] == CONFIG['DATE_LANG']:
            lang = cnt
            break
        cnt += 1

    if dateparts['month'] and int(dateparts['month']) < 13:
        monthname = lazylibrarian.MONTHNAMES[0][int(dateparts['month'])]
        month_name = monthname[lang]
        month_num = dateparts['month']
    else:
        month_name = ''
        month_num = 1
    if dateparts['months'] and dateparts['months'][0]:
        startname = lazylibrarian.MONTHNAMES[0][dateparts['months'][0]]
        start_name = startname[lang]
        start_month = dateparts['months'][0]
    else:
        start_name = ''
        start_month = 1
    if dateparts['months'] and dateparts['months'][-1]:
        endname = lazylibrarian.MONTHNAMES[0][dateparts['months'][-1]]
        end_name = endname[lang]
        end_month = dateparts['months'][-1]
    else:
        end_name = ''
        end_month = 1

    return preformat.replace(
        '$V', str(dateparts['volume']).zfill(4)).replace(
        '$v', str(dateparts['volume'])).replace(
        '$I', str(dateparts['issue']).zfill(4)).replace(
        '$i', str(dateparts['issue'])).replace(
        '$Y', str(dateparts['year'])).replace(
        '$D', str(dateparts['day']).zfill(2)).replace(
        '$M', str(month_num).zfill(2)).replace(
        '$m', month_name).replace(
        '$S', str(start_month).zfill(2)).replace(
        '$s', start_name).replace(
        '$E', str(end_month).zfill(2)).replace(
        '$e', end_name)
