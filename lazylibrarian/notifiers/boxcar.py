# Author: Marvin Pinto <me@marvinp.ca>
# Author: Dennis Lutter <lad1337@gmail.com>
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

# API_URL = "https://boxcar.io/devices/providers/MH0S7xOFSwVLNvNhTpiC/notifications"
# changed to boxcar2
API_URL = 'https://new.boxcar.io/api/notifications'


class BoxcarNotifier:
    def __init__(self):
        pass

    def _send_boxcar(self, msg, title, token, subscribe=False):
        """
        Sends a boxcar notification to the address provided

        msg: The message to send (unicode)
        title: The title of the message
        email: The email address to send the message to (or to subscribe with)
        subscribe: If true then instead of sending a message this function will send
        a subscription notification (optional, default is False)

        returns: True if the message succeeded, False otherwise
        """
        logger = logging.getLogger(__name__)
        logger.debug(f'Boxcar notification: {msg}')
        logger.debug(f'Title: {title}')
        logger.debug(f'Token: {token}')
        logger.debug(f'Subscribe: {subscribe}')

        # build up the URL and parameters
        msg = msg.strip()
        cur_url = API_URL

        # if this is a subscription notification then act accordingly
        if subscribe:
            data = {'email': token}
            cur_url += "/subscribe"

        # for normal requests we need all these parameters
        else:
            data = {
                'user_credentials': token,
                'notification[title]': title,
                'notification[long_message]': msg,
                'notification[sound]': "done"
            }
        proxies = proxy_list()
        # send the request to boxcar
        try:
            timeout = CONFIG.get_int('HTTP_TIMEOUT')
            r = requests.get(cur_url, params=data, timeout=timeout, proxies=proxies)
            status = str(r.status_code)
            if status == 200:
                logger.debug("BOXCAR: Notification successful.")
                return True

            # HTTP status 404 if the provided email address isn't a Boxcar user.
            if status == '404':
                logger.warning("BOXCAR: Username is wrong/not a boxcar email. Boxcar will send an email to it")
            # For HTTP status code 401's, it is because you are passing in either an
            # invalid token, or the user has not added your service.
            elif status == '401':
                # If the user has already added your service, we'll return an HTTP status code of 401.
                if subscribe:
                    logger.error("BOXCAR: Already subscribed to service")
                # HTTP status 401 if the user doesn't have the service added
                else:
                    subscribe_note = self._send_boxcar(msg, title, token, True)
                    if subscribe_note:
                        logger.debug("BOXCAR: Subscription sent.")
                        return True
                    logger.error("BOXCAR: Subscription could not be sent.")
            # If you receive an HTTP status code of 400, it is because you failed to send the proper parameters
            elif status == '400':
                logger.error("BOXCAR: Wrong data send to boxcar.")
            else:
                logger.error(f"BOXCAR: Got error code {status}")
            return False

        except Exception as e:
            # if we get an error back that doesn't have an error code then who knows what's really happening
            # URLError doesn't return a code, just a reason. HTTPError gives a code
            if not hasattr(e, 'code'):
                logger.error(f"BOXCAR: Boxcar notification failed: {str(e)}")
            else:
                # noinspection PyUnresolvedReferences
                logger.error(f"BOXCAR: Boxcar notification failed. Error code: {str(e.code)}")
            return False

    def _notify(self, title, message, username=None, force=False):
        """
        Sends a boxcar notification based on the provided info or SB config

        title: The title of the notification to send
        message: The message string to send
        username: The username to send the notification to (optional, defaults to the username in the config)
        force: If True then the notification will be sent even if Boxcar is disabled in the config
        """

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_BOXCAR') and not force:
            return False

        # if no username was given then use the one from the config
        if not username:
            username = CONFIG['BOXCAR_TOKEN']

        return self._send_boxcar(message, title, username)

    #
    # Public functions
    #

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('BOXCAR_NOTIFY_ONSNATCH'):
            if fail:
                self._notify(notify_strings[NOTIFY_FAIL], title)
            else:
                self._notify(notify_strings[NOTIFY_SNATCH], title)

    def notify_download(self, title):
        if CONFIG.get_bool('BOXCAR_NOTIFY_ONDOWNLOAD'):
            self._notify(notify_strings[NOTIFY_DOWNLOAD], title)

    def test_notify(self, title="Test"):
        return self._notify("This is a test notification from LazyLibrarian", title, force=True)

    def update_library(self, show_name=None):
        pass


notifier = BoxcarNotifier
