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

import json
import logging
import os
import shutil
import time

import lazylibrarian
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import DIRS, path_isfile, remove_file, splitext
from lazylibrarian.formatter import check_int, get_list, md5_utf8, plural

try:
    import slskd_api
except ModuleNotFoundError:
    slskd_api = None


def slskd_version_check(version, target="0.22.2"):
    version_tuple = tuple(map(int, version.split('.')))
    target_tuple = tuple(map(int, target.split('.')))
    return version_tuple >= target_tuple


def slsk_search(book=None, searchtype='ebook', test=False):
    logger = logging.getLogger(__name__)
    provider = "soulseek"

    if BLOCKHANDLER.is_blocked(provider):
        if test:
            return False
        return [], "provider is already blocked"

    slsk = SLSKD()
    if not slsk.slskd:
        logger.error("Unable to connect to slskd, is it running?")
        if test:
            return False
        return [], "Unable to connect to slskd"

    cache = True
    cachelogger = logging.getLogger('special.cache')
    cache_location = os.path.join(DIRS.CACHEDIR, "IRCCache")

    if test:
        book['bookid'] = '0'
        cache = False

    if cache:
        myhash = md5_utf8(book['searchterm'])
        valid_cache = False
        hashfilename = os.path.join(cache_location, f"{myhash}.slsk")
        # cache results so we can do multiple searches for the same author
        # or multiple search types for a book without hammering the provider
        # expire cache after 2 hours, there might be new additions
        expiry = check_int(lazylibrarian.IRC_CACHE_EXPIRY, 2 * 3600)

        if path_isfile(hashfilename):
            cache_modified_time = os.stat(hashfilename).st_mtime
            time_now = time.time()
            if cache_modified_time < time_now - expiry:
                # Cache entry is too old, delete it
                cachelogger.debug(f"Expiring {myhash}")
                remove_file(hashfilename)
            else:
                valid_cache = True

        if valid_cache:
            lazylibrarian.CACHE_HIT = int(lazylibrarian.CACHE_HIT) + 1
            cachelogger.debug(f"CacheHandler: Found CACHED response {hashfilename} for {book['searchterm']}")
        else:
            lazylibrarian.CACHE_MISS = int(lazylibrarian.CACHE_MISS) + 1
            hashfilename = slsk.search(book['searchterm'], searchtype=searchtype, test=test)
    else:
        hashfilename = slsk.search(book['searchterm'], searchtype=searchtype, test=test)

    if not hashfilename:
        logger.error("Not connected to slskd")
        if test:
            return 0
        return [], "Not connected to slskd"

    with open(hashfilename) as f:
        searchresults = json.load(f)

    logger.debug(f"{provider} returned {len(searchresults)}")
    results = []
    removed = 0

    for item in searchresults:
        author = book['authorName']
        title = item['filename']
        size = item['size']
        dl = f"{item['username']}^{json.dumps(item['directory'])}"

        if not author or not title or not size or not dl:
            removed += 1
        else:
            if author and author not in title:
                title = f"{author.strip()} {title.strip()}"

            results.append({
                'bookid': book['bookid'],
                'tor_prov': provider,
                'tor_title': title,
                'tor_url': dl,  # username^directory
                'tor_size': size,
                'tor_type': 'direct',
                'priority': CONFIG["SLSK_DLPRIORITY"],
                'hash_file': hashfilename
            })
            logger.debug(f'Found {title}, Size {size}')

    if test:
        logger.debug(f"Test found {len(results)} {plural(len(results), 'result')} ({removed} removed)")
        return len(results)

    logger.debug(f"Found {len(results)} {plural(len(results), 'result')} from {provider} for {book['searchterm']}")
    return results, ''


class SLSKD:
    def __init__(self):

        self.logger = logging.getLogger(__name__)
        self.slskd = None
        self.version = None
        if not slskd_api:
            self.logger.error("slskd_api module is not loaded")
            return

        self.api_key = CONFIG['SLSK_API']
        self.host_url = CONFIG['SLSK_HOST']
        self.download_dir = get_list(CONFIG['DOWNLOAD_DIR'])[0]
        self.url_base = CONFIG['SLSK_URLBASE']
        self.ignored_users = []
        try:
            self.slskd = slskd_api.SlskdClient(host=self.host_url, api_key=self.api_key, url_base=self.url_base)
            self.stalled_timeout = 3600
            self.delete_searches = True
            self.version = self.slskd.application.version()
            self.logger.debug(f"SLSKD version {self.version}")
        except Exception as e:
            self.slskd = None
            self.logger.error(str(e))

    def search(self, searchterm, searchtype='ebook', test=False):
        results = []
        if not self.slskd:
            self.logger.error("Not connected to slskd")
            return ''

        try:
            self.logger.debug(f"Searching for {searchterm}")
            search = self.slskd.searches.search_text(searchText=searchterm,
                                                     searchTimeout=50000,
                                                     filterResponses=True,
                                                     maximumPeerQueueLength=50,
                                                     minimumPeerUploadSpeed=0)
        except Exception as e:
            self.logger.error(f"Error searching slskd: {e}")
            return ''

        lazylibrarian.test_data = "Waiting for SoulSeek<br>search to complete"
        cnt = 0
        while True:
            state = self.slskd.searches.state(search['id'])['state']
            if state != 'InProgress':
                self.logger.debug(state)
                break

            lazylibrarian.test_data = f"Soulseek searching<br>{cnt} seconds..."
            if cnt > 120:
                break
            time.sleep(2)
            cnt += 2

        lazylibrarian.test_data = ''
        res = len(self.slskd.searches.search_responses(search['id']))
        msg = f"Search returned results from {res} users"
        limit = 0
        if test:
            limit = 10
            msg += f'Limiting test to {limit}'
        self.logger.info(msg)
        user_count = 0
        try:
            for result in self.slskd.searches.search_responses(search['id']):
                user_count += 1
                if limit and user_count > limit:
                    break
                username = result['username']
                if username in self.ignored_users:
                    self.logger.info(f"Ignoring user {username}")
                else:
                    files = result['files']
                    msg = f"Parsing result from user: {username}, {len(files)} results"
                    self.logger.info(msg)
                    rejects = 0
                    for file in files:
                        if username in self.ignored_users:
                            break
                        try:
                            file_dir, file_name = file['filename'].rsplit("\\", 1)
                            if slskd_version_check(self.version):
                                directory = self.slskd.users.directory(username=username, directory=file_dir)[0]
                            else:
                                directory = self.slskd.users.directory(username=username, directory=file_dir)
                        except Exception as e:
                            self.logger.warning(f"Ignoring {username}, {str(e)}")
                            self.ignored_users.append(username)
                            continue

                        if not CONFIG.is_valid_booktype(file_name, booktype=searchtype):
                            rejects += 1  # self.logger.debug(f"Rejecting {file_name}")
                            if rejects > 20:
                                self.logger.debug(f"Ignoring {username}, too many rejects")
                                self.ignored_users.append(username)
                                break
                        else:
                            # Parse the directory['files'] and only include the files we want
                            # Include opf, jpg  adjust directory['fileCount'] to match
                            new_dir = {}
                            for item in directory:
                                if item != 'files':
                                    new_dir[item] = directory[item]
                            new_files = []
                            extns = []
                            for item in directory['files']:
                                title, extn = splitext(item['filename'])
                                extn = extn.lstrip('.').lower()
                                if extn in extns:  # reject if multiple files of the same type
                                    new_files = []
                                    self.logger.debug(f"Rejecting {title}, multiple {extn}")
                                    self.ignored_users.append(username)
                                    break
                                extns.append(extn)
                                title = title.lower()
                                if (CONFIG.is_valid_booktype(item['filename'], booktype=searchtype) or
                                        extn in ['opf', 'jpg']):
                                    accept = True
                                    for word in get_list(searchterm):
                                        if word.lower() not in title:
                                            accept = False
                                            break
                                    if accept or title in ['metadata', 'cover']:
                                        item['filename'] = file_dir + "\\" + item['filename']
                                        new_files.append(item)
                            if new_files:
                                new_dir['fileCount'] = len(new_files)
                                new_dir['files'] = new_files
                                data = {
                                    "dir": file_dir.split("\\")[-1],
                                    "filename": file_name,
                                    "username": username,
                                    "directory": new_dir,
                                    "size": file['size'],
                                }
                                results.append(data)

                    self.logger.info(f"Finished processing user: {username}, rejected {rejects}")
            self.logger.info(f"Processed results from {len(self.slskd.searches.search_responses(search['id']))} users")
        except Exception as e:
            self.logger.error(f"Error getting responses: {e}")
        if self.delete_searches:
            self.slskd.searches.delete(search['id'])
        myhash = md5_utf8(searchterm)
        cache_location = os.path.join(DIRS.CACHEDIR, "IRCCache")
        hashfilename = os.path.join(cache_location, f"{myhash}.slsk")
        with open(hashfilename, 'w') as fp:
            json.dump(results, fp)
        return hashfilename

    def enqueue(self, username, directory):
        if not self.slskd:
            self.logger.error("Not connected to slskd")
            return False
        try:
            self.slskd.transfers.enqueue(username=username, files=directory['files'])
            return True
        except Exception as e:
            self.logger.warning(f"Error {e} enqueueing. Adding {username} to ignored users list.")
            downloads = self.slskd.transfers.get_downloads(username)
            for cancel_directory in downloads["directories"]:
                if cancel_directory["directory"] == directory["name"]:
                    self.cancel_and_delete(directory["name"].split("\\")[-1], username, cancel_directory["files"])
                    self.ignored_users.append(username)
            return False

    def cancel_and_delete(self, delete_dir, username, files):
        if not self.slskd:
            self.logger.error("Not connected to slskd")
            return
        for file in files:
            self.slskd.transfers.cancel_download(username=username, id=file['id'])

        os.chdir(self.download_dir)

        if os.path.exists(delete_dir):
            shutil.rmtree(delete_dir)

    def download(self, results):
        time_count = 0
        while True:
            unfinished = 0
            for result in list(results):
                username, folder = result['username'], result['directory']
                try:
                    downloads = self.slskd.transfers.get_downloads(username)
                except Exception as e:
                    self.logger.warning(f"Error {e} getting downloads. Adding {username} to ignored users list.")
                    self.ignored_users.append(username)
                    continue
                for directory in downloads["directories"]:
                    if directory["directory"] == folder["name"]:
                        # Generate list of errored or failed downloads
                        errored_files = [file for file in directory["files"] if file["state"] in [
                            'Completed, Cancelled',
                            'Completed, TimedOut',
                            'Completed, Errored',
                            'Completed, Rejected',
                        ]]
                        # Generate list of downloads still pending
                        pending_files = [file for file in directory["files"] if 'Completed' not in file["state"]]

                        # If we have errored files, cancel and remove ALL files so we can retry next time
                        if len(errored_files) > 0:
                            self.logger.error(f"FAILED: Username: {username} Directory: {folder['name']}")
                            self.cancel_and_delete(result['dir'], result['username'], directory["files"])
                        elif len(pending_files) > 0:
                            unfinished += 1

            if unfinished == 0:
                self.logger.info("All slsk items finished downloading!")
                return True

            time_count += 10

            if time_count > self.stalled_timeout:
                self.logger.info("Stall timeout reached! Removing stuck downloads...")
                for result in list(results):
                    username, folder = result['username'], result['directory']
                    if not self.slskd:
                        self.logger.error("Not connected to slskd")
                        return False
                    downloads = self.slskd.transfers.get_downloads(username)
                    for directory in downloads["directories"]:
                        if directory["directory"] == folder["name"]:
                            # TODO: This does not seem to account for directories where
                            # the whole dir is stuck as queued.
                            # Either it needs to account for those or maybe soularr should just
                            # force clear out the downloads screen when it exits.
                            pending_files = [file for file in directory["files"] if 'Completed' not in file["state"]]
                            if len(pending_files) > 0:
                                self.logger.error(f"Removing Stalled Download: "
                                                  f"Username: {username} Directory: {folder['name']}")
                                self.cancel_and_delete(result['dir'], result['username'], directory["files"])
                return False

            time.sleep(10)
