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


import datetime
import logging
import os
import threading
import traceback
import uuid
from shutil import copyfile

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.comicid import comic_metadata, cv_identify, cv_issue, cx_identify, cx_issue
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import (
    DIRS,
    get_directory,
    path_isfile,
    setperm,
    splitext,
    syspath,
)
from lazylibrarian.formatter import check_int, get_list, now, plural, sanitize
from lazylibrarian.images import create_mag_cover
from lazylibrarian.metadata_opf import create_comic_opf


def comic_scan(comicid=None):
    logger = logging.getLogger(__name__)
    lazylibrarian.COMIC_UPDATE = 1
    title = ''
    # noinspection PyBroadException
    db = database.DBConnection()
    try:
        if comicid:
            mags = db.match('select Title from comics WHERE ComicID=?', (comicid,))
            if mags:
                title = mags['Title']
        mag_path = CONFIG['COMIC_DEST_FOLDER']
        if title and '$Title' in mag_path:
            comic_name = sanitize(title)
            mag_path = mag_path.replace('$Title', comic_name)
            onetitle = comic_name
        else:
            onetitle = None

        while '$' in mag_path:
            mag_path = os.path.dirname(mag_path)

        if CONFIG.get_bool('COMIC_RELATIVE'):
            mag_path = os.path.join(get_directory('eBook'), mag_path)

        if CONFIG.get_bool('FULL_SCAN') and not onetitle:
            cmd = ("select Title,IssueID,IssueFile,comics.ComicID from comics,comicissues "
                   "WHERE comics.ComicID = comicissues.ComicID")
            mags = db.select(cmd)
            # check all the issues are still there, delete entry if not
            for mag in mags:
                title = mag['Title']
                issueid = mag['IssueID']
                comicid = mag['ComicID']
                issuefile = mag['IssueFile']
                control_value_dict = {"ComicID": comicid}

                if issuefile and not path_isfile(issuefile):
                    db.action('DELETE from comicissues where issuefile=?', (issuefile,))
                    logger.info(f'Issue {title} - {issueid} deleted as not found on disk')

                    new_value_dict = {
                        "LastAcquired": None,  # clear magazine dates
                        "LatestIssue": None,  # we will fill them in again later
                        "LatestCover": None,
                        "IssueStatus": "Skipped"  # assume there are no issues now
                    }
                    db.upsert("comics", new_value_dict, control_value_dict)
                    logger.debug(f'Comic {title} ({comicid}) details reset')

            # now check the comic titles and delete any with no issues
            if CONFIG.get_bool('COMIC_DELFOLDER'):
                cmd = ("select Title,ComicID,(select count(*) as counter from comicissues "
                       "where comics.comicid = comicissues.comicid) as issues from comics order by Title")
                mags = db.select(cmd)
                for mag in mags:
                    title = mag['Title']
                    comicid = mag['ComicID']
                    issues = mag['issues']
                    if not issues:
                        logger.debug(f'Comic {title} deleted as no issues found')
                        db.action('DELETE from comics WHERE ComicID=?', (comicid,))

        logger.info(f" Checking [{mag_path}] for {CONFIG['COMIC_TYPE']}")

        for rootdir, _, filenames in os.walk(mag_path):
            for fname in filenames:
                if CONFIG.is_valid_booktype(fname, booktype='comic'):
                    title = ''
                    issue = ''
                    start = ''
                    publisher = ''
                    searchterm = ''
                    issuelink = ''
                    comicid = ''
                    issuedescription = ''
                    contributors = ''
                    aka = ''
                    res = comic_metadata(os.path.join(rootdir, fname))
                    if res:
                        title = res.get('Series')
                        issue = str(check_int(res.get('Number'), 0))
                        comicid = res.get('ComicID')
                        if title and issue and comicid:
                            publisher = res.get('Publisher')
                            start = res.get('Year')
                            searchterm = title
                            issuelink = res.get('Web')
                            issuedescription = res.get('Summary')
                            logger.debug(f"Metadata found {title} ({comicid}) Issue {issue}")

                    res = cv_identify(fname)
                    if not res:
                        res = cx_identify(fname)
                    if res:
                        if not comicid:
                            comicid = res[3]['seriesid']
                        elif comicid and res[3]['seriesid'] and comicid != res[3]['seriesid']:
                            # stick with comicid from metadata and use identify result as aka
                            aka = res[3]['seriesid']

                        if not issue:
                            issue = str(res[4])
                        if not title:
                            title = res[3]['title']
                        if not publisher:
                            publisher = res[3]['publisher']
                        if not start:
                            start = res[3]['start']
                        if not searchterm:
                            searchterm = res[3]['searchterm']
                        first = res[3]['first']
                        last = res[3]['last']
                        serieslink = res[3]['link']
                        seriesdescription = res[3]['description']
                        logger.debug(f"Found {title} ({comicid}) Issue {issue}")

                        # is this comicid already in the database?
                        mag_entry = db.match('SELECT * from comics WHERE ComicID=?', (comicid,))
                        if mag_entry:
                            logger.debug(f"ComicID {comicid} already exists")
                            if aka:
                                akas = get_list(mag_entry['aka'])
                                if aka not in akas:
                                    logger.debug(f"Adding aka {aka} to {comicid}")
                                    akas.append(aka)
                                    new_value_dict = {"aka": ','.join(akas)}
                                    control_value_dict = {"ComicID": comicid}
                                    db.upsert("comics", new_value_dict, control_value_dict)
                        elif aka:
                            # is the aka id in the database
                            mag_entry = db.match("SELECT * from comics WHERE instr(aka, ?) > 0", (aka,))
                            if mag_entry:
                                logger.debug(f"aka {aka} exists for {comicid}")
                                comicid = mag_entry['ComicID']  # use aka as comicid
                        if not mag_entry:
                            mag_entry = db.match('SELECT * from comics WHERE Title=?', (title,))
                            if mag_entry:
                                aka = comicid
                                comicid = mag_entry['ComicID']
                                logger.debug(f"{comicid} exists for {title}")
                                akas = get_list(mag_entry['aka'])
                                if aka not in akas:
                                    logger.debug(f"Adding aka {aka} to {comicid}")
                                    akas.append(aka)
                                    control_value_dict = {"ComicID": comicid}
                                    new_value_dict = {"aka": ','.join(akas)}
                                    db.upsert("comics", new_value_dict, control_value_dict)
                        if not mag_entry:
                            # need to add a new comic to the database
                            control_value_dict = {"ComicID": comicid}
                            new_value_dict = {
                                "Title": title,
                                "Status": "Active",
                                "Added": now(),
                                "LastAcquired": None,
                                "Updated": now(),
                                "LatestIssue": issue,
                                "IssueStatus": "Skipped",
                                "LatestCover": None,
                                "Start": start,
                                "First": first,
                                "Last": last,
                                "Publisher": publisher,
                                "SearchTerm": searchterm,
                                "Link": serieslink,
                                "Description": seriesdescription,
                                "aka": aka
                            }
                            logger.debug(f"Adding comic {title} ({comicid})")
                            db.upsert("comics", new_value_dict, control_value_dict)
                            lastacquired = None
                            latestissue = issue
                            added = None
                        else:
                            lastacquired = mag_entry['LastAcquired']
                            latestissue = mag_entry['LatestIssue']
                            added = mag_entry['Added']

                        # is this issue already in the database?
                        iss_entry = db.match('SELECT IssueFile from comicissues WHERE ComicID=? and IssueID=?',
                                             (comicid, issue))
                        issuefile = os.path.join(rootdir, fname)  # full path to issue.cbr
                        mtime = os.path.getmtime(issuefile)
                        iss_acquired = datetime.date.isoformat(datetime.date.fromtimestamp(mtime))
                        myhash = uuid.uuid4().hex

                        if not iss_entry or (iss_entry['IssueFile'] != issuefile):
                            new_value_dict = {
                                "IssueAcquired": iss_acquired,
                                "IssueFile": issuefile
                            }
                            if not iss_entry:
                                logger.debug(f"Adding issue {title} {issue}")
                                coverfile = create_mag_cover(issuefile, refresh=True)
                                if coverfile and path_isfile(coverfile):
                                    hashname = os.path.join(DIRS.CACHEDIR, 'comic', f'{myhash}.jpg')
                                    copyfile(coverfile, hashname)
                                    setperm(hashname)
                                    new_value_dict['Cover'] = f'cache/comic/{myhash}.jpg'
                                else:
                                    new_value_dict['Cover'] = 'images/nocover.png'
                                new_value_dict['Description'] = issuedescription
                                new_value_dict['Link'] = issuelink
                            else:
                                logger.debug(f"Updating issue {title} {issue}")
                            if not issuedescription or not issuelink or not contributors:
                                # get issue details from series page
                                res = ''
                                if comicid.startswith('CV'):
                                    res = cv_issue(comicid[2:], issue)
                                elif comicid.startswith('CX'):
                                    res = cx_issue(serieslink, issue)
                                if res:  # type: dict
                                    for item in ['Description', 'Link', 'Contributors']:
                                        # noinspection PyTypeChecker
                                        if res[item]:
                                            # noinspection PyTypeChecker
                                            new_value_dict[item] = res[item]

                            control_value_dict = {"ComicID": comicid, "IssueID": issue}
                            db.upsert("comicissues", new_value_dict, control_value_dict)
                            if not iss_entry:
                                dest_path, global_name = os.path.split(issuefile)
                                global_name = splitext(global_name)[0]
                                data = control_value_dict
                                data.update(new_value_dict)
                                data['Title'] = title
                                data['Publisher'] = publisher
                                if not CONFIG.get_bool('IMP_COMICOPF'):
                                    logger.debug('create_comic_opf is disabled')
                                else:
                                    _ = create_comic_opf(dest_path, data, global_name, overwrite=True)

                        ignorefile = os.path.join(os.path.dirname(issuefile), '.ll_ignore')
                        try:
                            with open(syspath(ignorefile), 'w', encoding='utf-8') as f:
                                f.write('comic')
                        except OSError as e:
                            logger.warning(f"Unable to create/write to ignorefile: {str(e)}")

                        # see if this issues date values are useful
                        control_value_dict = {"ComicID": comicid}
                        if not mag_entry:  # new magazine, this is the only issue
                            new_value_dict = {
                                "Added": iss_acquired,
                                "LastAcquired": iss_acquired,
                                "LatestCover": f'cache/comic/{myhash}.jpg',
                                "LatestIssue": latestissue,
                                "IssueStatus": "Open"
                            }
                            db.upsert("comics", new_value_dict, control_value_dict)
                        else:
                            # Set magazine_issuedate to issuedate of most recent issue we have
                            # Set latestcover to most recent issue cover
                            # Set magazine_added to acquired date of earliest issue we have
                            # Set magazine_lastacquired to acquired date of most recent issue we have
                            # acquired dates are read from magazine file timestamps
                            new_value_dict = {"IssueStatus": "Open"}
                            if not added or iss_acquired < added:
                                new_value_dict["Added"] = iss_acquired
                            if not lastacquired or iss_acquired > lastacquired:
                                new_value_dict["LastAcquired"] = iss_acquired

                            if not latestissue or issue >= latestissue:
                                new_value_dict["LatestIssue"] = issue
                                new_value_dict["LatestCover"] = f'cache/comic/{myhash}.jpg'
                            db.upsert("comics", new_value_dict, control_value_dict)
                    else:
                        logger.debug(f"No match for {fname}")
        if CONFIG.get_bool('FULL_SCAN') and not onetitle:
            magcount = db.match("select count(*) from comics")
            isscount = db.match("select count(*) from comicissues")
            logger.info(
                f"Comic scan complete, found {magcount['count(*)']} {plural(magcount['count(*)'], 'comic')}, "
                f"{isscount['count(*)']} {plural(isscount['count(*)'], 'issue')}")
        else:
            logger.info("Comic scan complete")
        lazylibrarian.COMIC_UPDATE = 0
    except Exception:
        lazylibrarian.COMIC_UPDATE = 0
        logger.error(f'Unhandled exception in comic_scan: {traceback.format_exc()}')
    finally:
        if 'COMICSCAN' in threading.current_thread().name:
            threading.current_thread().name = 'WEBSERVER'
        db.close()
