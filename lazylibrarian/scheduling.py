#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.
#
# Purpose:
#   Scheduling functionality

import datetime
import logging
import threading
import time
import traceback
from enum import Enum

from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.background import BackgroundScheduler

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.bookwork import add_series_members
from lazylibrarian.config2 import CONFIG
from lazylibrarian.configtypes import ConfigScheduler
from lazylibrarian.formatter import check_int, plural
from lazylibrarian.importer import add_author_to_db

# Notification Types
NOTIFY_SNATCH = 1
NOTIFY_DOWNLOAD = 2
NOTIFY_FAIL = 3

notify_strings = {NOTIFY_SNATCH: "Started Download",
                  NOTIFY_DOWNLOAD: "Added to Library",
                  NOTIFY_FAIL: "Download failed"}

# Scheduler
SCHED: BackgroundScheduler


class SchedulerCommand(Enum):
    """ Commands that can be given to the scheduler or scheduled tasks """
    START = 'Start'
    STARTNOW = 'StartNow'
    RESTART = 'Restart'
    STOP = 'Stop'
    NONE = 'Noop'


def initscheduler():
    global SCHED
    job_defaults = {
        'missfire_grace_time': 30,
        'max_instances': 1,
        'replace_existing': True,
    }
    SCHED = BackgroundScheduler(job_defaults=job_defaults)


def startscheduler():
    SCHED.start()


def shutdownscheduler():
    try:
        if SCHED and SCHED.running:
            # noinspection PyUnresolvedReferences
            SCHED.shutdown(wait=False)
    except (NameError, SchedulerNotRunningError):
        pass


def next_run_time(when_run: str, test_now: datetime.datetime | None = None):
    """
    Returns a readable approximation of how long until a job will be run,
    given a string representing the last time it was run
    """
    logger = logging.getLogger(__name__)
    try:
        # split timezone off as strptime() doesn't recognise all timezones
        # and now() doesn't include timezone, so assume both times are local timezone
        when_run = ' '.join(when_run.split()[:2])
        when_run = datetime.datetime.strptime(when_run, '%Y-%m-%d %H:%M:%S')
        timenow = test_now if test_now else datetime.datetime.now()
        td = when_run - timenow
        diff = td.total_seconds()  # time difference in seconds
    except ValueError as e:
        logger.error(f"Error getting next run for [{when_run}] {str(e)}")
        diff = 0
        td = ''

    td = str(td)
    if 'days,' in td:  # > 1 day, just return days
        return f"{td.split('s,')[0]}s"
    if 'day,' in td and "0:00:00" not in td:  # 1 day and change, or 1 day?
        diff += 86400

    days, hours, minutes, seconds = get_whole_timediff_from_seconds(diff)

    if days > 1:
        return f"{days} days"
    if hours > 1:
        return f"{hours} hours"
    if minutes > 1:
        return f"{minutes} minutes"
    if seconds == 1:
        return "1 second"
    return f"{seconds} seconds"


def get_whole_timediff_from_seconds(diff):
    # calculate whole units, plus round up by adding 1(true) if remainder >= half
    days = int(diff / 86400) + (diff % 86400 >= 43200)
    hours = int(diff / 3600) + (diff % 3600 >= 1800)
    minutes = int(diff / 60) + (diff % 60 >= 30)
    seconds = int(diff)
    return days, hours, minutes, seconds


def get_next_run_time(target: str, minutes=0, action=SchedulerCommand.NONE) -> datetime:
    """ Check when a job is next due to run and log it
        Return startdate for the job """
    logger = logging.getLogger(__name__)
    if action == SchedulerCommand.STARTNOW:
        lazylibrarian.STOPTHREADS = False
        minutes = 0

    db = database.DBConnection()
    columns = db.select('PRAGMA table_info(jobs)')
    if not columns:  # no such table
        lastrun = 0
    else:
        res = db.match('SELECT Finish from jobs WHERE Name=?', (target,))
        if res and res['Finish']:
            lastrun = res['Finish']
        else:
            lastrun = 0
    db.close()

    nextruntime = ''
    for job in SCHED.get_jobs():
        if target in str(job):
            nextruntime = str(job).split('at: ')[1].split('.')[0].strip(')')
            nextruntime = ' '.join(nextruntime.split()[:2])  # strip any timezone
            break

    if nextruntime:
        startdate = datetime.datetime.strptime(nextruntime, '%Y-%m-%d %H:%M:%S')
        msg = f"{action} {target} job in {next_run_time(nextruntime)}"
    else:
        next_run_in = lastrun + (minutes * 60) - time.time()
        if next_run_in < 60:
            next_run_in = 60  # overdue, start in 1 minute

        startdate = datetime.datetime.fromtimestamp(time.time() + next_run_in)
        startdate = startdate.replace(microsecond=0)  # Whole seconds only

        next_run_in = int(next_run_in / 60)
        if next_run_in < 1:
            next_run_in = 1

        if next_run_in <= 120:
            msg = f"{action.value} {target} job in {next_run_in} {plural(next_run_in, 'minute')}"
        else:
            hours = int(next_run_in / 60)
            if hours <= 48:
                msg = f"{action.value} {target} job in {hours} {plural(hours, 'hour')}"
            else:
                days = int(hours / 24)
                msg = f"{action.value} {target} job in {days} {plural(days, 'day')}"
    if lastrun:
        msg += f" (Last run {ago(lastrun)})"
    logger.debug(msg)

    return startdate


def adjust_schedule(scheduler: ConfigScheduler):
    """ This method makes any adjustments to the scheduler that need to happen,
    but where the code does not belong in the configtypes module """

    logger = logging.getLogger(__name__)
    name = scheduler.get_schedule_name()
    if name in ['clean_cache']:
        # Override the interval with the value from CACHE_AGE
        cdays = CONFIG.get_int('CACHE_AGE')
        scheduler.set_int(cdays)

    elif name in ['backup']:
        # Override the interval with the value from BACKUP_DB
        cdays = CONFIG.get_int('BACKUP_DB')
        scheduler.set_int(cdays)

    elif name in ['author_update', 'series_update']:
        # Disregard configured value of interval, use CACHE_AGE.
        # Then, shorten the interval depending on how much needs to be done
        cdays = CONFIG.get_int('CACHE_AGE')
        if cdays:
            maxhours = cdays * 24

            typ = name.replace('_update', '')
            overdue, total, _, _, days = is_overdue(typ)
            if days == maxhours:
                due = "due"
            else:
                due = "overdue"
            logger.debug(f"Found {overdue} {plural(overdue, typ)} from {total} {due} update")

            interval = maxhours * 60
            interval = interval / max(total, 1)
            interval = int(interval * 0.80)  # allow some update time

            if interval < 5:  # set a minimum interval of 5 minutes, so we don't upset goodreads/librarything api
                interval = 5

            # Update the scheduler with the calculated interval in minutes
            logger.debug(f"Setting interval for {name} to {interval} minutes, found {overdue} to update")
            scheduler.set_int(interval)


def schedule_job(action=SchedulerCommand.START, target: str = ''):
    """ Start or stop or restart a cron job by name e.g.
        target=search_magazines, target=process_dir, target=search_book """
    if target == '':
        return

    stopjob = None
    startjob = None
    logger = logging.getLogger(__name__)
    if action in [SchedulerCommand.STOP, SchedulerCommand.RESTART]:
        for job in SCHED.get_jobs():
            if target in str(job):
                stopjob = job
                break

    if action in [SchedulerCommand.START, SchedulerCommand.RESTART, SchedulerCommand.STARTNOW]:
        if not stopjob:
            for job in SCHED.get_jobs():
                if target in str(job):
                    logger.debug(f"{action.value} {target} job, already scheduled")
                    return  # return if already running, if not, start a new one

        schedule = CONFIG.get_configscheduler(target)
        if schedule:
            if CONFIG.scheduler_can_run(schedule):
                # Perform local adjustments to the schedule before proceeding
                adjust_schedule(schedule)
                # only start job if interval is > 0 after adjustment
                if schedule.get_int():
                    startjob = schedule
                else:
                    logger.warning(f'Scheduler for job {target} is disabled')
        else:
            logger.error(f'Could not find scheduler for job {target}')

    if stopjob and startjob:
        # Make sure we only stop and start jobs where the interval has changed
        res = startjob.get_hour_min_interval()
        hours = res[0]
        minutes = res[1]
        if stopjob.trigger.interval_length - 60 * (hours * 60 + minutes) < 2:
            stopjob = startjob = None  # 2 seconds tolerance: No change

    if stopjob:
        logger.debug(f"Stop {target} job")
        SCHED.remove_job(target)
    if startjob:
        method = startjob.get_method()
        if method:
            hours, minutes = startjob.get_hour_min_interval()
            startdate = get_next_run_time(startjob.run_name, minutes + hours * 60, action)
            SCHED.add_job(method, "interval", hours=hours, minutes=minutes, next_run_time=startdate, id=target)
        else:
            logger.error(f'Cannot find method {startjob.method_name} for scheduled job {target}')


def add_interval_job(method, hours, minutes, startdate, target):
    """ Add a scheduled job """
    SCHED.add_job(method, "interval", hours=hours, minutes=minutes, next_run_time=startdate, id=target)


def author_update(restart=True, only_overdue=True):
    logger = logging.getLogger(__name__)
    msg = ''

    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        logger.debug("Storing start time for AUTHORUPDATE")
        db.upsert("jobs", {"Start": time.time()}, {"Name": "AUTHORUPDATE"})
        if CONFIG.get_int('CACHE_AGE'):
            overdue, total, name, ident, days = is_overdue('author')
            if not total:
                msg = "There are no monitored authors"
            elif not overdue and only_overdue:
                msg = f"Oldest author info ({name}) is {days} {plural(days, 'day')} old, no update due"
            else:
                logger.info(f'Starting update for {name}:{ident}')
                _ = add_author_to_db(refresh=True, authorid=ident, reason=f"author_update {name}")
                if lazylibrarian.STOPTHREADS:
                    return ''
                msg = f'Updated author {name}'
            if total and restart and not lazylibrarian.STOPTHREADS:
                schedule_job(SchedulerCommand.RESTART, "author_update")
        return msg

    except Exception:
        logger.error(f'Unhandled exception in AuthorUpdate: {traceback.format_exc()}')
        return "Unhandled exception in AuthorUpdate"

    finally:
        logger.debug("Storing finish time for AUTHORUPDATE")
        db.upsert("jobs", {"Finish": time.time()}, {"Name": "AUTHORUPDATE"})
        db.close()


def series_update(restart=True, only_overdue=True):
    logger = logging.getLogger(__name__)
    msg = ''

    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        logger.debug("Storing start time for SERIESUPDATE")
        db.upsert("jobs", {"Start": time.time()}, {"Name": "SERIESUPDATE"})
        if CONFIG.get_int('CACHE_AGE'):
            overdue, total, name, ident, days = is_overdue('series')
            if not total:
                msg = "There are no monitored series"
            elif not overdue and only_overdue:
                msg = f"Oldest series info ({name}) is {days} {plural(days, 'day')} old, no update due"
            else:
                logger.info(f'Starting series update for {name}')
                add_series_members(ident)
                msg = f'Updated series {name}'
            logger.debug(msg)
            if total and restart and not lazylibrarian.STOPTHREADS:
                schedule_job(SchedulerCommand.RESTART, "series_update")
        return msg

    except Exception:
        logger.error(f'Unhandled exception in series_update: {traceback.format_exc()}')
        return "Unhandled exception in series_update"

    finally:
        logger.debug("Storing finish time for SERIESUPDATE")
        db.upsert("jobs", {"Finish": time.time()}, {"Name": "SERIESUPDATE"})
        db.close()


def all_author_update(refresh=False):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        cmd = ("SELECT AuthorID,AuthorName from authors WHERE Status='Active' or Status='Loading' or Status='Wanted'"
               " order by Updated ASC")
        activeauthors = db.select(cmd)
        lazylibrarian.AUTHORS_UPDATE = 1
        logger.info(f"Starting update for {len(activeauthors)} active {plural(len(activeauthors), 'author')}")
        for author in activeauthors:
            if lazylibrarian.STOPTHREADS:
                logger.debug("Aborting ActiveAuthorUpdate")
                break
            _ = add_author_to_db(refresh=refresh, authorid=author['AuthorID'], authorname=author['AuthorName'],
                                 reason="all_author_update")
        logger.info('Active author update complete')
        msg = f"Updated {len(activeauthors)} active {plural(len(activeauthors), 'author')}"
        logger.debug(msg)
    except Exception:
        msg = f'Unhandled exception in all_author_update: {traceback.format_exc()}'
        logger.error(msg)
    finally:
        db.close()
        if 'AAUPDATE' in threading.current_thread().name:
            threading.current_thread().name = 'WEBSERVER'
        lazylibrarian.AUTHORS_UPDATE = 0
    return msg


def restart_jobs(command=SchedulerCommand.RESTART):
    lazylibrarian.STOPTHREADS = command == SchedulerCommand.STOP
    for _name, scheduler in CONFIG.get_schedulers():
        schedule_job(command, scheduler.get_schedule_name())


def ensure_running(jobname: str):
    """ Ensure that the job named jobname is running """
    lazylibrarian.STOPTHREADS = False
    if not any(jobname in str(job) for job in SCHED.get_jobs()):
        schedule_job(SchedulerCommand.START, jobname)


def show_running():
    """ return a list of currently running tasks """
    db = database.DBConnection()
    running = []
    jobs = db.select("SELECT Name from jobs WHERE Start>Finish")
    db.close()
    for entry in jobs:
        if 'Thread' not in entry['Name']:
            running.append(entry['Name'])
    return running


def check_running_jobs():
    # make sure the relevant jobs are running
    # search jobs start when something gets marked "wanted" but are
    # not aware of any config changes that happen later, ie enable or disable providers,
    # so we check whenever config is saved
    # postprocessor is started when something gets marked "snatched"
    # and cancels itself once everything is processed so should be ok
    # but check anyway for completeness...

    lazylibrarian.STOPTHREADS = False
    db = database.DBConnection()
    try:
        snatched = db.match("SELECT count(*) as counter from wanted WHERE Status = 'Snatched'")
        seeding = db.match("SELECT count(*) as counter from wanted WHERE Status = 'Seeding'")
        wanted = db.match("SELECT count(*) as counter FROM books WHERE Status = 'Wanted'")
    finally:
        db.close()
    if snatched or seeding:
        ensure_running('PostProcessor')
    if wanted:
        if CONFIG.use_any(rss=False):
            ensure_running('search_book')
        if CONFIG.use_rss():
            ensure_running('search_rss_book')
    else:
        schedule_job(SchedulerCommand.STOP, 'search_book')
        schedule_job(SchedulerCommand.STOP, 'search_rss_book')
    if CONFIG.use_wishlist():
        ensure_running('search_wishlist')
    else:
        schedule_job(SchedulerCommand.STOP, 'search_wishlist')

    if CONFIG.use_any():
        ensure_running('search_magazines')
        ensure_running('search_comics')
    else:
        schedule_job(SchedulerCommand.STOP, 'search_magazines')
        schedule_job(SchedulerCommand.STOP, 'search_comics')

    ensure_running('author_update')
    ensure_running('series_update')


def is_overdue(which="author") -> (int, int, str, str, int):
    """ Determines how many items of type 'author' or 'series'are overdue for an update, because
    the entries are older than CACHE_AGE.
    Returns
        overdue: Number of items
        total: Total number of items, including those not overdue
        name: The Author or Series name
        ident: The ID for the Author or Series
        days
    """

    def get_overdue_from_dbrows(results):
        dtnow = time.time()
        found = 0
        thedays = int((dtnow - results[0]['Updated']) / (24 * 60 * 60))
        for item in results:
            diff = (dtnow - item['Updated']) / (24 * 60 * 60)
            if diff > maxage:
                found += 1
            else:
                break
        return thedays, found

    overdue = total = days = 0
    name = ident = ''
    maxage = CONFIG.get_int('CACHE_AGE')
    if maxage:
        db = database.DBConnection()
        logger = logging.getLogger(__name__)
        try:
            if which == 'author':
                cmd = "SELECT AuthorName,AuthorID,Updated from authors WHERE Status='Active' or Status='Loading'"
                cmd += " or Status='Wanted' and AuthorID !='' order by Updated ASC"
                res = db.select(cmd)
                total = len(res)
                if total:
                    name = res[0]['AuthorName']
                    ident = res[0]['AuthorID']
                    days, overdue = get_overdue_from_dbrows(res)
            if which == 'series':
                cmd = ("SELECT SeriesName,SeriesID,Updated from Series where Status='Active' or Status='Wanted' "
                       "order by Updated ASC")
                res = db.select(cmd)
                total = len(res)
                if total:
                    name = res[0]['SeriesName']
                    ident = res[0]['SeriesID']
                    days, overdue = get_overdue_from_dbrows(res)
        except Exception as e:
            logger.debug(f"Error: {e}")

        db.close()
    return overdue, total, name, ident, days


def ago(when):
    """ Return human-readable string of how long ago something happened
        when = seconds count """

    diff = time.time() - when
    days, hours, minutes, seconds = get_whole_timediff_from_seconds(diff)

    if days > 1:
        return f"{days} days ago"
    if hours > 1:
        return f"{hours} hours ago"
    if minutes > 1:
        return f"{minutes} minutes ago"
    if seconds > 1:
        return f"{seconds} seconds ago"
    return "just now"


def show_jobs(json=False):
    result = []
    resultdict = {}
    db = database.DBConnection()
    for job in SCHED.get_jobs():
        job = str(job)
        jobname = ''
        threadname = ''
        for _key, scheduler in CONFIG.get_schedulers():
            method_name = scheduler.method_name.split('.')[-1]
            if method_name in job:
                jobname = scheduler.friendly_name
                threadname = scheduler.run_name
                break
        if not jobname:
            jobname = job.split(' ')[0].split('.')[2]
            threadname = jobname.upper()

        # jobinterval = job.split('[')[1].split(']')[0]
        jobtime = job.split('at: ')[1].split('.')[0].strip(')')
        jobtime = next_run_time(jobtime)
        timeparts = jobtime.split(' ')
        if timeparts[0] == '1' and timeparts[1].endswith('s'):
            timeparts[1] = timeparts[1][:-1]
        jobinfo = f"{jobname}: Next run in {timeparts[0]} {timeparts[1]}"
        resultdict[jobname] = {}
        resultdict[jobname]['next'] = f"Next run in {timeparts[0]} {timeparts[1]}"
        res = db.match(f"SELECT Start,Finish from jobs WHERE Name='{threadname}'")

        if res:
            if res['Start'] > res['Finish']:
                resultdict[jobname] = {}
                resultdict[jobname]['last'] = f"Running since {ago(res['Start'])}"
                jobinfo += f" (Running since {ago(res['Start'])})"
            elif res['Finish']:
                resultdict[jobname]['last'] = f"Last run {ago(res['Finish'])}"
                jobinfo += f" (Last run {ago(res['Finish'])})"
        result.append(jobinfo)

    result.append(' ')
    overdue, total, name, _, days = is_overdue('author')
    resultdict['Author'] = {}
    if name:
        resultdict['Author']['Name'] = name
        resultdict['Author']['Overdue'] = days
        result.append(f"Oldest author info ({name}) is {days} {plural(days, 'day')} old")
    if not overdue:
        resultdict['Author']['Overdue'] = 0
        result.append("There are no authors needing update")
    elif days == CONFIG.get_int('CACHE_AGE'):
        resultdict['Author']['Due'] = overdue
        result.append(f"Found {overdue} {plural(overdue, 'author')} from {total} due update")
    else:
        resultdict['Author']['Late'] = overdue
        result.append(f"Found {overdue} {plural(overdue, 'author')} from {total} overdue update")

    overdue, total, name, _, days = is_overdue('series')
    resultdict['Series'] = {}
    if name:
        resultdict['Series']['Name'] = name
        resultdict['Series']['Overdue'] = days
        result.append(f"Oldest series info ({name}) is {days} {plural(days, 'day')} old")
    if not overdue:
        resultdict['Series']['Overdue'] = 0
        result.append("There are no series needing update")
    elif days == CONFIG.get_int('CACHE_AGE'):
        resultdict['Series']['Due'] = overdue
        result.append(f"Found {overdue} series from {total} due update")
    else:
        resultdict['Series']['Late'] = overdue
        result.append(f"Found {overdue} series from {total} overdue update")
    if json:
        return resultdict
    return result


def show_stats(json=False):
    """ Return status of activity suitable for display, or json if requested """
    resultdict = {}
    cache = {'hit': check_int(lazylibrarian.CACHE_HIT, 0), 'miss': check_int(lazylibrarian.CACHE_MISS, 0)}
    sleep = {'goodreads': lazylibrarian.TIMERS['SLEEP_GR'], 'librarything': lazylibrarian.TIMERS['SLEEP_LT'],
             'comicvine': lazylibrarian.TIMERS['SLEEP_CV'], 'hardcover': lazylibrarian.TIMERS['SLEEP_HC']}
    resultdict['cache'] = cache
    resultdict['sleep'] = sleep
    result = [
        f"Cache {check_int(lazylibrarian.CACHE_HIT, 0)} {plural(check_int(lazylibrarian.CACHE_HIT, 0), 'hit')}, "
        f"{check_int(lazylibrarian.CACHE_MISS, 0)} miss, ",
        f"Sleep {lazylibrarian.TIMERS['SLEEP_GR']:.3f} goodreads, {lazylibrarian.TIMERS['SLEEP_LT']:.3f} librarything, "
        f"{lazylibrarian.TIMERS['SLEEP_CV']:.3f} comicvine, {lazylibrarian.TIMERS['SLEEP_HC']:.3f} hardcover"]

    db = database.DBConnection()
    try:
        snatched = db.match("SELECT count(*) as counter from wanted WHERE Status = 'Snatched'")
        if snatched['counter']:
            resultdict['snatched'] = snatched['counter']
            result.append(f"{snatched['counter']} Snatched {plural(snatched['counter'], 'item')}")
        result.append("No Snatched items")

        series_stats = []
        res = db.match("SELECT count(*) as counter FROM series")
        series_stats.append(['Series', res['counter']])
        res = db.match("SELECT count(*) as counter FROM series WHERE Total>0 and Have=0")
        series_stats.append(['Empty', res['counter']])
        res = db.match("SELECT count(*) as counter FROM series WHERE Total>0 AND Have=Total")
        series_stats.append(['Full', res['counter']])
        res = db.match("SELECT count(*) as counter FROM series WHERE Status='Ignored'")
        series_stats.append(['Ignored', res['counter']])
        res = db.match("SELECT count(*) as counter FROM series WHERE Total=0")
        series_stats.append(['Blank', res['counter']])
        res = db.match("SELECT count(*) as counter FROM series WHERE Updated>0")
        series_stats.append(['Monitor', res['counter']])
        overdue = is_overdue('series')[0]
        series_stats.append(['Overdue', overdue])
        seriesstats = {}
        for item in series_stats:
            seriesstats[item[0]] = item[1]
        resultdict['series_stats'] = seriesstats

        mag_stats = []
        if CONFIG.get_bool('MAG_TAB'):
            res = db.match("SELECT count(*) as counter FROM magazines")
            mag_stats.append(['Magazine', res['counter']])
            res = db.match("SELECT count(*) as counter FROM issues")
            mag_stats.append(['Issues', res['counter']])
            cmd = ("select (select count(*) as counter from issues where magazines.title = issues.title) "
                   "as counter from magazines where counter=0")
            res = db.match(cmd)
            mag_stats.append(['Empty', len(res)])
            magstats = {}
            for item in mag_stats:
                magstats[item[0]] = item[1]
            resultdict['mag_stats'] = magstats

        if CONFIG.get_bool('COMIC_TAB'):
            comicstats = {}
            res = db.match("SELECT count(*) as counter FROM comics")
            mag_stats.append(['Comics', res['counter']])
            comicstats['Comics'] = res['counter']
            res = db.match("SELECT count(*) as counter FROM comicissues")
            mag_stats.append(['Issues', res['counter']])
            comicstats['Issues'] = res['counter']
            cmd = ("select (select count(*) as counter from comicissues where comics.comicid = comicissues.comicid) "
                   "as counter from comics where counter=0")
            res = db.match(cmd)
            mag_stats.append(['Empty', len(res)])
            comicstats['Empty'] = len(res)
            resultdict['comic_stats'] = comicstats

        book_stats = []
        audio_stats = []
        missing_stats = []
        res = db.match("SELECT count(*) as counter FROM books")
        book_stats.append(['eBooks', res['counter']])
        audio_stats.append(['Audio', res['counter']])
        res = db.select("SELECT Status,count(*) as counter from books group by Status")
        statusdict = {}
        for item in res:
            statusdict[item['Status']] = item['counter']
        for item in ['Have', 'Open', 'Wanted', 'Ignored']:
            book_stats.append([item, statusdict.get(item, 0)])
        bookstats = {}
        for item in book_stats:
            bookstats[item[0]] = item[1]
        resultdict['book_stats'] = bookstats

        res = db.select("SELECT AudioStatus,count(*) as counter from books group by AudioStatus")
        statusdict = {}
        for item in res:
            statusdict[item['AudioStatus']] = item['counter']
        for item in ['Have', 'Open', 'Wanted', 'Ignored']:
            audio_stats.append([item, statusdict.get(item, 0)])
        audiostats = {}
        for item in audio_stats:
            audiostats[item[0]] = item[1]
        resultdict['audio_stats'] = audiostats

        for column in ['BookGenre', 'BookDesc']:
            cmd = ("SELECT count(*) as counter FROM books WHERE Status != 'Ignored' "
                   "and (%s is null or %s = '')")
            res = db.match(cmd % (column, column))
            missing_stats.append([column.replace('Book', 'No'), res['counter']])
        cmd = "SELECT count(*) as counter FROM books WHERE Status != 'Ignored' and BookGenre='Unknown'"
        res = db.match(cmd)
        missing_stats.append(['X_Genre', res['counter']])
        cmd = "SELECT count(*) as counter FROM books WHERE Status != 'Ignored' and BookDesc='No Description'"
        res = db.match(cmd)
        missing_stats.append(['X_Desc', res['counter']])
        for column in ['BookISBN', 'BookLang']:
            cmd = "SELECT count(*) as counter FROM books WHERE (%s is null or %s = '' or %s = 'Unknown')"
            res = db.match(cmd % (column, column, column))
            missing_stats.append([column.replace('Book', 'No'), res['counter']])
        cmd = "SELECT count(*) as counter FROM genres"
        res = db.match(cmd)
        missing_stats.append(['Genres', res['counter']])
        missingstats = {}
        for item in missing_stats:
            missingstats[item[0]] = item[1]
        resultdict['missing_stats'] = missingstats

        if not CONFIG.get_bool('AUDIO_TAB'):
            audio_stats = []

        author_stats = []
        res = db.match("SELECT count(*) as counter FROM authors")
        author_stats.append(['Authors', res['counter']])
        for status in ['Active', 'Wanted', 'Ignored', 'Paused']:
            res = db.match(f"SELECT count(*) as counter FROM authors WHERE Status='{status}'")
            author_stats.append([status, res['counter']])
        res = db.match("SELECT count(*) as counter FROM authors WHERE HaveEBooks+HaveAudioBooks=0")
        author_stats.append(['Empty', res['counter']])
        res = db.match("SELECT count(*) as counter FROM authors WHERE TotalBooks=0")
        author_stats.append(['Blank', res['counter']])
        overdue = is_overdue('author')[0]
        author_stats.append(['Overdue', overdue])
        res = db.select('select distinct authorid from bookauthors except select authorid from books')
        author_stats.append(['Secondary', len(res)])
        authorstats = {}
        for item in author_stats:
            authorstats[item[0]] = item[1]
        resultdict['author_stats'] = authorstats
    finally:
        db.close()

    if json:
        return resultdict

    for stats in [author_stats, book_stats, missing_stats, series_stats, audio_stats, mag_stats]:
        if len(stats):
            header = ''
            data = ''
            for item in stats:
                header += f"{item[0]:<8s}"
                data += f"{item[1]:<8d}"
            result.append('')
            result.append(header)
            result.append(data)
    return result
