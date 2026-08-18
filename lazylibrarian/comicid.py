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


import contextlib
import logging
import re
import string
import time
import zipfile
from urllib.parse import quote_plus
from xml.etree import ElementTree

from bs4 import BeautifulSoup

import lazylibrarian
from lazylibrarian.cache import html_request, json_request
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import path_isfile
from lazylibrarian.formatter import (
    check_int,
    check_year,
    make_unicode,
    make_utf8bytes,
    plural,
    strip_quotes,
)
from lazylibrarian.telemetry import TELEMETRY


def get_issue_num(words, skipped):
    # try to extract issue number from a list of words if not in skipped list
    # this is so we can tell 007 in "james bond 007" is not an issue number
    # Allow floats as issue could be #0.5
    for word in words:
        if word.startswith('#') and len(word) > 1:
            try:
                return int(word[1:])
            except ValueError:
                try:
                    return float(word[1:])
                except ValueError:
                    pass

    for ln in (3, 2, 1):
        for word in words:
            if len(word) == ln and word not in skipped:
                try:
                    return int(word)
                except ValueError:
                    try:
                        return float(word)
                    except ValueError:
                        pass
    return ''


def name_words(name):
    # sanitize for better matching
    # allow #num and word! or word? but strip other punctuation, allow '&' as a word
    punc_chars = string.punctuation
    for c in '#!?&:':
        punc_chars = punc_chars.replace(c, '')
    punct = re.compile(f"[{re.escape(punc_chars)}]")
    name = punct.sub(' ', name)
    # strip all ascii and non-ascii quotes/apostrophes
    name = strip_quotes(name)
    # special cases, probably need a configurable translation table like we do for genres
    name = name.replace('40 000', '40,000').replace("X Men", "X-Men").replace("X Factor", "X-Factor")
    tempwords = name.lower().split()
    # merge initials together into one "word" for matching
    namewords = []
    buildword = ''
    for word in tempwords:
        if word == '&' and not buildword:
            namewords.append(word)
        elif len(word) == 1 and not word.isdigit():
            buildword = f"{buildword}{word}."
        else:
            if buildword:
                if len(buildword) == 2:
                    buildword = buildword[0]
                namewords.append(buildword)
                buildword = ''
            namewords.append(word)
    if buildword:
        if len(buildword) == 2:
            buildword = buildword[0]
        namewords.append(buildword)
    return namewords


def title_words(words):
    titlewords = []
    skipwords = ['volume', 'vol', 'issue']
    # Extract title from filename
    # stopping when we reach the next number (volume, issue, year)
    # but allow v2 or 40,000
    for word in words:
        if word and word not in skipwords:
            if titlewords and (word[-1].isdigit() and word[0] != 'v' and ',' not in word):
                break
            titlewords.append(word)
    return titlewords


def cv_identify(fname, best=True):
    logger = logging.getLogger(__name__)
    matchinglogger = logging.getLogger('special.matching')
    TELEMETRY.record_usage_data('CV/Identify')
    apikey = CONFIG['CV_APIKEY']
    if not apikey:
        # don't nag. Show warning message no more than every 20 mins
        timenow = int(time.time())
        if check_int(lazylibrarian.TIMERS['NO_CV_MSG'], 0) + 1200 < timenow:
            logger.warning(
                "Please obtain an apikey from https://comicvine.gamespot.com/api/ and add as CV_APIKEY in config.ini")
            lazylibrarian.TIMERS['NO_CV_MSG'] = timenow
        return []

    fname = make_unicode(fname)
    words = name_words(fname)
    titlewords = title_words(words)
    minmatch = 1
    # comicvine sometimes misses matches if we include too many words??
    # we can either use less words, or scrape the html...
    matchwords = ' '.join(titlewords)
    if ' ' in matchwords:
        minmatch = 2

    choices = []
    results = []
    offset = 0
    next_page = True
    max_pages = CONFIG.get_int('MAX_PAGES')
    page_number = 0
    while next_page:
        if offset:
            off = f"&offset={offset}"
        else:
            off = ''
        url = '/'.join([CONFIG['CV_URL'], f'api/volumes/?api_key={apikey}'])
        if fname.startswith('CV'):
            url += f'&format=json&sort=name:asc&filter=id:{fname[2:]}{off}'
        else:
            url += f'&format=json&sort=name:asc&filter=name:{quote_plus(make_utf8bytes(matchwords)[0])}{off}'
        cv_api_sleep()
        res, _ = json_request(url)
        if not res:
            next_page = False
        else:
            results = res['results']
            offset = res['offset']
            total = res['number_of_total_results']
            paged = res['number_of_page_results']
            for item in results:
                title = item['name']
                publisher = item['publisher']
                if publisher:
                    publisher = publisher['name']
                else:
                    publisher = ''
                start = item.get('start_year', '')
                link = item['site_detail_url'].replace('\\', '')
                count = item['count_of_issues']
                first = item.get('first_issue', 0)
                if first:
                    first = check_int(first.get('issue_number'), 0)
                last = item.get('last_issue', 0)
                if last:
                    last = check_int(last.get('issue_number'), 0)
                seriesid = item['id']
                description = item['description']
                if description is None:
                    description = ""
                else:
                    soup = BeautifulSoup(description, "html5lib")
                    description = soup.text

                choices.append({"title": title,
                                "publisher": publisher,
                                "start": start,
                                "count": count,
                                "first": first,
                                "last": last,
                                "seriesid": f"CV{seriesid}",
                                "description": description,
                                "searchterm": matchwords.replace('+', ' '),
                                "link": link
                                })
            if paged and len(choices) < total:
                offset += paged
                page_number += 1
                if max_pages and page_number > max_pages:
                    logger.debug("Maximum search pages reached, still more results available")
                    next_page = False
                else:
                    next_page = True
            else:
                next_page = False

    if not best:
        return choices

    if choices:
        matchinglogger.debug(f'Found {len(choices)} possible for {fname}')
        results = []
        year = 0
        # do we have a year to narrow it down
        for w in words:
            if check_year(w):
                year = w
                break

        matchinglogger.debug(f"Checking {len(choices)} {plural(len(choices), 'result')}")
        for item in choices:
            present = 0
            noise = 0
            missing = 0
            rejected = False
            namewords = name_words(item['title'])

            for w in namewords:
                if w not in words:
                    noise += 1

            if year and item['start'] and item["start"] > year:  # series not started yet
                matchinglogger.debug(f"Year {year} out of range (start={item['start']}) {item['title']}")
                rejected = True

            issue = get_issue_num(words, namewords)
            if issue and (issue < check_int(item["first"], 0) or issue > check_int(item["last"], 0)):
                matchinglogger.debug(f"Issue {issue} out of range ({item['first']} to {item['last']}) {item['title']}")
                rejected = True

            for w in titlewords:
                if w not in name_words(item['title']):
                    missing += 1
                else:
                    present += 1

            if not rejected and present >= minmatch:
                results.append([present, noise, missing, item, issue])
            else:
                matchinglogger.debug(f"Only matched {present} {plural(present, 'word')} in {item['title']}")

        results = sorted(results, key=lambda x: (-x[0], x[1], -(check_int(x[3]["start"], 0))))
        matchinglogger.debug(str(results))

        if results:
            return results[0]

    if not CONFIG.get_bool('CV_WEBSEARCH'):
        matchinglogger.debug(f'No match for {fname}')
        return []

    matchinglogger.debug(f'No api match for {fname}, trying websearch')
    # fortunately comicvine sorts the resuts and gives us "best match first"
    # so we only scrape the first page (could add &page=2)
    url = '/'.join([CONFIG['CV_URL'], f'search/?i=volume&q={matchwords}'])
    data, in_cache = html_request(url)
    if not data:
        matchinglogger.debug(f'No match for {fname}')
        return []

    choices = get_volumes_from_search(data)
    if choices:
        matchinglogger.debug(f'Found {len(choices)} possible for {fname}')
        results = []
        year = 0
        # do we have a year to narrow it down
        for w in words:
            if check_year(w):
                year = w
                break

        for item in choices:
            wordcount = 0
            noise = 0
            rejected = False
            namewords = name_words(item['title'])

            for w in namewords:
                if w in words:
                    wordcount += 1
                else:
                    noise += 1

            if year and item["start"] > year:  # series not started yet
                rejected = True

            issue = get_issue_num(words, namewords)

            missing = 0
            for w in titlewords:
                if w not in name_words(item['title']):
                    missing += 1

            if not rejected and wordcount >= minmatch:
                results.append([wordcount, noise, missing, item, issue])

        results = sorted(results, key=lambda x: (-x[0], x[1], -(check_int(x[3]["start"], 0))))

    if results:
        return results[0]

    matchinglogger.debug(f'No match for {fname}')
    return []


def get_volumes_from_search(page_content):
    # Return list of volumes for the Comics Series
    choices = []
    soup = BeautifulSoup(page_content, "html5lib")
    h2 = soup.find('h2', class_='header-border')
    if h2:
        matchwords = h2.span.text
    else:
        matchwords = ''

    extracted_volumes = soup.find_all('ul', class_='search-results')
    for item in extracted_volumes:
        try:
            title = item.find('h3').text.strip('\n').strip().strip('\n')
            info = item.find('p').text.strip('\n').strip().strip('\n')
            href = item.find('a', href=True)['href']
            seriesid = href.rsplit('-', 1)[1].strip('/')
            publisher = info.split('(')[-1].split(')')[0]
            start = info.split('(')[0].split(' ', 1)[1].strip()
            count = info.split('(')[1].split(' ')[0]
            match = True
        except (IndexError, AttributeError):
            title = ''
            publisher = ''
            href = ''
            seriesid = ''
            start = ''
            count = ''
            match = False

        if match:
            first = 0
            last = 0
            description = ''

            choices.append({"title": title,
                            "publisher": publisher,
                            "start": start,
                            "count": count,
                            "first": first,
                            "last": last,
                            "seriesid": f"CV{seriesid}",
                            "description": description,
                            "searchterm": matchwords.replace('+', ' '),
                            "link": CONFIG['CV_URL'] + href
                            })
    return choices


def remove_attributes_from_link(link_list, publisher=None):
    # Remove attributes from links in a list and return "clean" list
    clean_link_list = []
    for link in link_list:
        new_link = re.sub(r"(\?.+)", "", link)
        if publisher:
            clean_link_list.append([new_link, publisher])
        else:
            clean_link_list.append(new_link)
    return clean_link_list


def get_series_links_from_search(page_content):
    # Return list of links for the Comics Series
    series_links = []
    soup = BeautifulSoup(page_content, "html5lib")
    res = soup.find_all('div', class_='content-cover')
    extracted_series_links = []
    for item in res:
        extracted_series_links.append(item.find('a', href=True)['href'])
    clean_series_links = remove_attributes_from_link(extracted_series_links)
    series_links.extend(clean_series_links)
    return series_links


def get_series_detail_from_search(page_content):
    # Return details for the Comics Series
    series_detail = {}
    soup = BeautifulSoup(page_content, "html5lib")
    try:
        series_detail['title'] = soup.find('h1', itemprop='name').text
    except AttributeError:
        series_detail['title'] = ''
    with contextlib.suppress(AttributeError):
        series_detail['publisher'] = soup.find('h3', class_="name").text.strip('\n').strip()
    with contextlib.suppress(AttributeError):
        series_detail['description'] = soup.find('div', itemprop='description').text
    issues = soup.find('div', class_="list Issues")
    if issues:
        series_detail['issues'] = issues.find_all('h6')
    else:
        series_detail['issues'] = []
    return series_detail


def cx_identify(fname, best=True):
    logger = logging.getLogger(__name__)
    matchinglogger = logging.getLogger('special.matching')
    TELEMETRY.record_usage_data('CX/Identify')
    res = []
    fname = make_unicode(fname)
    words = name_words(fname)
    titlewords = title_words(words)
    minmatch = 1
    matchwords = '+'.join(titlewords)
    if '+' in matchwords:
        minmatch = 2
    max_pages = CONFIG.get_int('MAX_PAGES')

    url = '/'.join([CONFIG['CX_URL'], f'search/series?search={matchwords}'])
    data, _ = html_request(url)

    if not data:
        matchinglogger.debug(f'No match for {fname}')
        return []

    series_links = get_series_links_from_search(data)
    for link in series_links:
        page_number = 1
        next_page = True
        first = 0
        last = 0
        start = ''

        while next_page:
            if page_number == 1:
                data, in_cache = html_request(link)
            else:
                data, in_cache = html_request(f"{link}?Issues_pg={page_number}")
            try:
                soup = BeautifulSoup(data, "html5lib")
                pager = soup.find('div', class_="list Issues").find(
                    'div', class_="pager-text").text.strip('\n').strip()
            except (TypeError, AttributeError):
                pager = None

            if pager:
                # eg '1 TO 18 OF 27'
                matchinglogger.debug(pager)

                pager_words = pager.split()
                if pager_words[2] == pager_words[4]:
                    next_page = False
                else:
                    page_number += 1
                    if max_pages and page_number > max_pages:
                        logger.debug("Maximum search pages reached, still more results available")
                        next_page = False
                    else:
                        next_page = True
            else:
                next_page = False

            if data:
                series_detail = get_series_detail_from_search(data)
                for item in series_detail['issues']:
                    # noinspection PyBroadException
                    try:
                        num = item.split('#')[1].split(' ')[0]
                        num = check_int(num, 0)
                        if not first:
                            first = num
                        else:
                            first = min(first, num)
                        last = max(last, num)
                    except Exception:
                        pass
                with contextlib.suppress(IndexError):
                    start = series_detail['title'].rsplit('(', 1)[1].split('-')[0].rstrip(')')

                series_detail['seriesid'] = f"CX{link.rsplit('/', 1)[1]}"
                series_detail['start'] = start
                series_detail['first'] = first
                series_detail['last'] = last
                series_detail['searchterm'] = matchwords.replace('+', ' ')
                series_detail['link'] = link
                series_detail.pop('issues')
                res.append(series_detail)

    if not best:
        return res

    choices = []
    if res:
        matchinglogger.debug(f'Found {len(res)} possible for {fname}')
        year = 0
        # do we have a year to narrow it down
        for w in words:
            if check_year(w):
                year = w
                break

        for item in res:
            wordcount = 0
            noise = 0
            missing = 0
            y1 = 0
            y2 = 0
            rejected = False
            if year:  # get year or range from title
                for y in name_words(item['title']):
                    if check_year(y):
                        if not y1:
                            y1 = y
                        else:
                            y2 = y
                            if y1 > y2:
                                y0 = y2
                                y2 = y1
                                y1 = y0
                            break

            for w in name_words(item['title']):
                if w in words:
                    if check_year(w):
                        matchinglogger.debug(f"Match {item['title']} year {year}")
                    else:
                        wordcount += 1
                else:
                    if check_year(w):
                        if y1 and y2 and int(y1) <= int(year) <= int(y2):
                            matchinglogger.debug(f"Match {item['title']} ({year} is between {y1}-{y2})")
                            rejected = False
                            break
                        if y1 and not y2 and int(year) >= int(y1):
                            matchinglogger.debug(f"Accept {item['title']} ({year} is in {y1}-)")
                            rejected = False
                            break
                        matchinglogger.debug(f"Rejecting {item['title']}, need {year}")
                        rejected = True
                        noise += 1
                        break
                    noise += 1

            for w in titlewords:
                if w not in name_words(item['title']):
                    missing += 1

            issue = get_issue_num(words, name_words(item['title'].split('(')[0]))
            if year and item["start"] > year:  # series not started yet
                rejected = True

            if not rejected and wordcount >= minmatch:
                if (missing + noise) / 2 >= wordcount:
                    matchinglogger.debug(f"Rejecting {item['title']} (noise {missing + noise})")
                else:
                    choices.append([wordcount, noise, missing, item, issue])

        if choices:
            choices = sorted(choices, key=lambda x: (-x[0], x[1]))
            return choices[0]

    matchinglogger.debug(f'No match for {fname}')
    return []


def comic_metadata(archivename, xml=False):
    logger = logging.getLogger(__name__)
    matchinglogger = logging.getLogger('special.matching')
    archivename = make_unicode(archivename)
    if not path_isfile(archivename):  # regular files only
        logger.debug(f"{archivename} is not a file")
        return {}

    if zipfile.is_zipfile(archivename):
        try:
            z = zipfile.ZipFile(archivename)
        except Exception as e:
            logger.error(f"Failed to unzip {archivename}: {e}")
            return {}

        namelist = z.namelist()
        for item in namelist:
            if item.endswith('ComicInfo.xml'):
                if xml:
                    res = z.read(item)
                    logger.debug(f"{len(res)} bytes xml")
                    return res
                return meta_dict(z.read(item))
        matchinglogger.debug(f'ComicInfo.xml not found in {archivename}')
        return {}

    if lazylibrarian.UNRARLIB == 1 and lazylibrarian.RARFILE.is_rarfile(archivename):
        try:
            z = lazylibrarian.RARFILE.RarFile(archivename)
        except Exception as e:
            logger.error(f"Failed to unrar lib_1 {archivename}: {e}")
            return {}

        namelist = z.namelist()
        for item in namelist:
            if item.endswith('ComicInfo.xml'):
                if xml:
                    res = z.read(item)
                    logger.debug(f"{len(res)} bytes xml")
                    return res
                return meta_dict(z.read(item))
        matchinglogger.debug(f'ComicInfo.xml not found in {archivename}')
        return {}

    if lazylibrarian.UNRARLIB == 2:
        try:
            rarc = lazylibrarian.RARFILE(archivename)
        except Exception as e:
            logger.error(f"Failed to unrar lib_2 {archivename}: {e}")
            return {}

        data = rarc.read_files('ComicInfo.xml')
        if data:
            if xml:
                res = data[0][1]
                logger.debug(f"{len(res)} bytes xml")
                return res
            return meta_dict(data[0][1])
        matchinglogger.debug(f'ComicInfo.xml not found in {archivename}')
        return {}

    logger.debug(f"{archivename} is not an archive we can unpack")
    return {}


def meta_dict(data):
    logger = logging.getLogger(__name__)
    rootxml = ElementTree.fromstring(data)
    datadict = {}
    for item in ['Series', 'Title', 'Number', 'Summary', 'Year', 'Publisher', 'Web']:
        res = rootxml.find(item)
        if res is not None and res.text is not None:
            datadict[item] = res.text
    if 'Web' in datadict:
        if 'comicvine' in datadict['Web']:
            datadict['ComicID'] = f"CV{datadict['Web'].rsplit('-', 1)[-1].strip('/')}"
        elif 'comixology' in datadict['Web']:
            datadict['ComicID'] = f"CX{datadict['Web'].rsplit('/', 1)[-1]}"
    else:
        res = rootxml.find('Notes')
        if res is not None and res.text is not None:
            notes = res.text
            if 'Comic Vine' in notes and 'Issue ID ' in notes:
                datadict['ComicID'] = f"CV{notes.split('Issue ID ')[1].split(']')[0].strip()}"
    logger.debug(str(datadict))
    return datadict


def cv_issue(seriesid, issuenum):
    logger = logging.getLogger(__name__)
    res = {'Description': '', 'Link': '', 'Contributors': ''}
    apikey = CONFIG['CV_APIKEY']
    url = '/'.join([CONFIG['CV_URL'], f'api/issues/?api_key={apikey}'])
    url += f'&format=json&filter=volume:{seriesid},issue_number:{issuenum}'
    cv_api_sleep()
    data, _ = json_request(url)
    # comicvine api "/issue" and "issue_detail_url" seem broken, always get 404
    # so try to extract site-detail-url from data, then page scrape
    # to get names and roles
    contributors = ''
    if data:
        try:
            res['Link'] = data['results'][0]['site_detail_url']
            res['Description'] = data['results'][0]['description']
        except IndexError:
            logger.debug(f"No link/description from {url}")
            return res
    else:
        logger.debug(f"No data from {url}")
        return res

    if res['Link']:
        data, _ = html_request(res['Link'])
    else:
        logger.debug(f"No site_detail_url from {url}")
        return res

    if data:
        soup = BeautifulSoup(data, "html5lib")
        table = soup.find('div', attrs={'id': 'wiki-relatedList'}
                          ).find('div', attrs={'class': 'wiki-details-object'})
        for entry in table.find_all('li'):
            name = entry.find('a').text.replace('\n', '').strip()
            role = entry.find('span').text.replace('\n', '').replace(',', '')
            role = " ".join(role.split()).strip()
            if name and role:
                if contributors:
                    contributors += ', '
                contributors += role + ': ' + name
        res['Contributors'] = contributors
    return res


def cx_issue(url, issuenum):
    res = {'Description': '', 'Link': '', 'Contributors': ''}
    data, _ = html_request(url)
    if data:
        issue_links = get_series_links_from_search(data)
        for link in issue_links:
            if f"-{issuenum}/" in link or f"-{issuenum}-" in link:
                res['Link'] = link
                break
    if res['Link']:
        data, _ = html_request(res['Link'])
        if data:
            soup = BeautifulSoup(data, "html5lib")
            desc = soup.find('section', class_='item-description')
            if desc:
                res['Description'] = desc.text.strip()
            cred = soup.find_all('div', class_='credits')
            contributors = ''
            for item in cred:
                role = item.find('h2')
                if role:
                    role = str(role).split('"')[1]
                else:
                    role = ''
                name = item.find('a').text.strip('\n').strip().strip('\n')
                if name and role:
                    if contributors:
                        contributors += ', '
                    contributors += role + ': ' + name
            res['Contributors'] = contributors
    return res


def cv_api_sleep():
    time_now = time.time()
    delay = time_now - lazylibrarian.TIMERS['LAST_CV']
    if delay < 1.0:
        sleep_time = 1.0 - delay
        lazylibrarian.TIMERS['SLEEP_CV'] += sleep_time
        cachelogger = logging.getLogger('special.cache')
        cachelogger.debug(f"ComicVine sleep {sleep_time:.3f}, total {lazylibrarian.TIMERS['SLEEP_CV']:.3f}")
        time.sleep(sleep_time)
    lazylibrarian.TIMERS['LAST_CV'] = time_now
