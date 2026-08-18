# This file is part of LazyLibrarian.
#
# LazyLibrarian is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# LazyLibrarian is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with LazyLibrarian.  If not, see <http://www.gnu.org/licenses/>.

import logging

from lazylibrarian import database
from lazylibrarian.common import run_script
from lazylibrarian.config2 import CONFIG
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings


class CustomNotifier:
    def __init__(self):
        pass

    @staticmethod
    def _notify(message, event, force=False):

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_CUSTOM') and not force:
            return False

        logger = logging.getLogger(__name__)
        logger.debug(f'Custom Event: {event}')
        logger.debug(f'Custom Message: {message}')
        db = database.DBConnection()
        try:
            if event == "Test":
                # grab the first entry in the book table and wanted table
                book = db.match('SELECT * from books')
                wanted = db.match('SELECT * from wanted')
                ident = 'eBook'
            else:
                # message is a bookid followed by type (eBook/AudioBook)
                # or a magazine title followed by it's NZBUrl
                words = message.split()
                ident = words[-1]
                if ident == 'ebook':
                    ident = 'eBook'
                if ident == 'audiobook':
                    ident = 'AudioBook'
                bookid = " ".join(words[:-1])
                book = db.match('SELECT * from books where BookID=?', (bookid,))
                if not book:
                    book = db.match('SELECT * from magazines where Title=?', (bookid,))

                if event == 'Added to Library':
                    wanted_status = " in ('Processed', 'Seeding')"
                else:
                    wanted_status = "='Snatched'"

                if ident in ['eBook', 'AudioBook']:
                    cmd = f"SELECT * from wanted where BookID=? AND AuxInfo=? AND Status{wanted_status}"
                    wanted = db.match(cmd, (bookid, ident))
                else:
                    cmd = f"SELECT * from wanted where BookID=? AND NZBUrl=? AND Status{wanted_status}"
                    wanted = db.match(cmd, (bookid, ident))
        finally:
            db.close()

        if book:
            dictionary = dict(book)
        else:
            dictionary = {}

        dictionary['Event'] = event

        if wanted:
            # noinspection PyTypeChecker
            wanted_dictionary = dict(wanted)
            for item in wanted_dictionary:
                if item in ['Status', 'BookID']:  # rename to avoid clash
                    dictionary[f"Wanted_{item}"] = wanted_dictionary[item]
                else:
                    dictionary[item] = wanted_dictionary[item]

        if 'AuxInfo' not in dictionary or not dictionary['AuxInfo']:
            if ident in ['eBook', 'AudioBook']:
                dictionary['AuxInfo'] = ident
            else:
                dictionary['AuxInfo'] = 'Magazine'

        try:
            # call the custom notifier script here, passing dictionary deconstructed as strings
            if CONFIG['CUSTOM_SCRIPT']:
                params = [CONFIG['CUSTOM_SCRIPT']]
                for item in dictionary:
                    params.append(item)
                    if isinstance(dictionary[item], bytes):
                        params.append(dictionary[item].decode('utf-8'))
                    elif isinstance(dictionary[item], (int | float | type(None))):
                        params.append(str(dictionary[item]))
                    else:
                        params.append(dictionary[item])
                rc, res, err = run_script(params)
                if rc:
                    logger.error(f"Custom notifier returned {rc}: res[{res}] err[{err}]")
                    return False
                logger.debug(res)
                return True
            logger.warning('Error sending custom notification: Check config')
            return False

        except Exception as e:
            logger.warning(f'Error sending custom notification: {e}')
            return False

    #
    # Public functions
    #

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('CUSTOM_NOTIFY_ONSNATCH'):
            if fail:
                self._notify(message=title, event=notify_strings[NOTIFY_FAIL])
            else:
                self._notify(message=title, event=notify_strings[NOTIFY_SNATCH])

    def notify_download(self, title):
        if CONFIG.get_bool('CUSTOM_NOTIFY_ONDOWNLOAD'):
            self._notify(message=title, event=notify_strings[NOTIFY_DOWNLOAD])

    def test_notify(self, title="Test"):
        return self._notify(message=title, event="Test", force=True)


notifier = CustomNotifier
