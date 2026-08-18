# Author: Marvin Pinto <me@marvinp.ca>
# Author: Dennis Lutter <lad1337@gmail.com>
# Author: Aaron Bieber <deftly@gmail.com>
# URL: http://code.google.com/p/lazylibrarian/
#
# This file is part of Sick Beard.
#
# Sick Beard is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Sick Beard is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Sick Beard.  If not, see <http://www.gnu.org/licenses/>.

import logging

import requests

from lazylibrarian.common import proxy_list
from lazylibrarian.config2 import CONFIG
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings


class AndroidPNNotifier:
    def __init__(self):
        pass

    def _send_android_pn(self, title, msg, url, username, broadcast):

        # build up the URL and parameters
        msg = msg.strip()

        data = {
            'action': "send",
            'broadcast': broadcast,
            'uri': "",
            'title': title,
            'username': username,
            'message': msg,
        }
        logger = logging.getLogger(__name__)
        proxies = proxy_list()
        # send the request
        try:
            timeout = CONFIG.get_int('HTTP_TIMEOUT')
            r = requests.get(url, params=data, timeout=timeout, proxies=proxies)
            if r.status_code == 200:
                logger.debug("ANDROIDPN: Notification successful.")
                return True

            # HTTP status 404 if the provided email address isn't a AndroidPN user.
            if r.status_code == 404:
                logger.warning("ANDROIDPN: Username is wrong/not a AndroidPN email. AndroidPN will send an email to it")
            # For HTTP status code 401's, it is because you are passing in either an
            # invalid token, or the user has not added your service.
            elif r.status_code == 401:
                subscribe_note = self._send_android_pn(title, msg, url, username, broadcast)
                if subscribe_note:
                    logger.debug("ANDROIDPN: Subscription sent")
                    return True
                logger.error("ANDROIDPN: Subscription could not be sent")

            # If you receive an HTTP status code of 400, it is because you failed to send the proper parameters
            elif r.status_code == 400:
                logger.error("ANDROIDPN: Wrong data sent to AndroidPN")
            else:
                logger.error(f"ANDROIDPN: Got error code {r.status_code}")
            return False

        except Exception as e:
            # URLError only returns a reason, not a code. HTTPError gives a code
            # FIXME: Python 2.5 hack, it wrongly reports 201 as an error
            # noinspection PyUnresolvedReferences
            if hasattr(e, 'code') and e.code == 201:
                logger.debug("ANDROIDPN: Notification successful.")
                return True

            # if we get an error back that doesn't have an error code then who knows what's really happening
            if not hasattr(e, 'code'):
                logger.error("ANDROIDPN: Notification failed.")
            else:
                # noinspection PyUnresolvedReferences
                logger.error(f"ANDROIDPN: Notification failed. Error code: {str(e.code)}")
            return False

    def _notify(self, title, message, url=None, username=None, broadcast=None, force=False):
        """
        Sends a pushover notification based on the provided info or SB config
        """

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_ANDROIDPN') and not force:
            return False

        logger = logging.getLogger(__name__)
        # fill in omitted parameters
        if not username:
            username = CONFIG['ANDROIDPN_USERNAME']
        if not url:
            url = CONFIG['ANDROIDPN_URL']
        if not broadcast:
            broadcast = CONFIG.get_bool('ANDROIDPN_BROADCAST')
            if broadcast:
                broadcast = 'Y'
            else:
                broadcast = 'N'

        logger.debug(
            f'ANDROIDPN: Sending notice: title="{title}", message="{message}", username={username}, '
            f'url={url}, broadcast={broadcast}')

        if not username or not url:
            return False

        return self._send_android_pn(title, message, url, username, broadcast)

    #
    # Public functions
    #

    def notify_snatch(self, ep_name, fail=False):
        if CONFIG.get_bool('ANDROIDPN_NOTIFY_ONSNATCH'):
            if fail:
                self._notify(notify_strings[NOTIFY_FAIL], ep_name)
            else:
                self._notify(notify_strings[NOTIFY_SNATCH], ep_name)

    def notify_download(self, ep_name):
        if CONFIG.get_bool('ANDROIDPN_NOTIFY_ONDOWNLOAD'):
            self._notify(notify_strings[NOTIFY_DOWNLOAD], ep_name)

    def test_notify(self):
        return self._notify("Test", "This is a test notification from LazyLibrarian", force=True)

    def update_library(self, ep_obj=None):
        pass


notifier = AndroidPNNotifier
