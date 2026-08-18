# Author: Marvin Pinto <me@marvinp.ca>
# Author: Dennis Lutter <lad1337@gmail.com>
# URL: http://code.google.com/p/lazylibrarian/
#
# This file is part of LazyLibrarian.
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
from http.client import HTTPSConnection
from urllib.parse import urlencode

from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import unaccented
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings


class PushoverNotifier:

    def __init__(self):
        pass

    @staticmethod
    def _send_pushover(message=None, event=None, pushover_apitoken=None, pushover_keys=None,
                       pushover_device=None, notification_type=None, method=None, force=False):

        if not CONFIG.get_bool('USE_PUSHOVER') and not force:
            return False

        logger = logging.getLogger(__name__)
        if pushover_apitoken is None:
            pushover_apitoken = CONFIG['PUSHOVER_APITOKEN']
        if pushover_keys is None:
            pushover_keys = CONFIG['PUSHOVER_KEYS']
        if pushover_device is None:
            pushover_device = CONFIG['PUSHOVER_DEVICE']
        if method is None:
            method = 'POST'
        if notification_type is None:
            test_message = True
            uri = "/1/users/validate.json"
            logger.debug("Testing Pushover authentication and retrieving the device list.")
        else:
            test_message = False
            uri = "/1/messages.json"
        logger.debug(f"Pushover event: {str(event)}")
        logger.debug(f"Pushover message: {str(message)}")
        logger.debug(f"Pushover api: {str(pushover_apitoken)}")
        logger.debug(f"Pushover keys: {str(pushover_keys)}")
        logger.debug(f"Pushover device: {str(pushover_device)}")
        logger.debug(f"Pushover notification type: {str(notification_type)}")

        http_handler = HTTPSConnection('api.pushover.net')

        try:
            data = {'token': pushover_apitoken,
                    'user': pushover_keys,
                    'title': event,
                    'message': message,
                    'device': pushover_device,
                    'priority': CONFIG['PUSHOVER_PRIORITY']}
            http_handler.request(method,
                                 uri,
                                 headers={'Content-type': "application/x-www-form-urlencoded"},
                                 body=urlencode(data))
        except Exception as e:
            logger.error(str(e))
            return False

        response = http_handler.getresponse()
        request_body = response.read()
        request_status = response.status
        logger.debug(f"Pushover Response: {request_status}")
        logger.debug(f"Pushover Reason: {response.reason}")

        if request_status == 200:
            if test_message:
                logger.debug(request_body)
                request_body = request_body.decode()
                if 'devices' in request_body:
                    return f"Devices: {request_body.split('[')[1].split(']')[0]}"
                return request_body
            return True
        if 400 <= request_status < 500:
            logger.error(f"Pushover request failed: {str(request_body)}")
            return False
        logger.error(f"Pushover notification failed: {request_status}")
        return False

    def _notify(self, message=None, event=None, pushover_apitoken=None, pushover_keys=None,
                pushover_device=None, notification_type=None, method=None, force=False):
        """
        Sends a pushover notification based on the provided info or LL config

        title: The title of the notification to send
        message: The message string to send
        username: The username to send the notification to (optional, defaults to the username in the config)
        force: If True then the notification will be sent even if pushover is disabled in the config
        """
        logger = logging.getLogger(__name__)
        try:
            message = unaccented(message)
        except Exception as e:
            logger.warning(f"Pushover: could not convert  message: {e}")
        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_PUSHOVER') and not force:
            return False

        logger.debug(f"Pushover: Sending notification {str(message)}")

        return self._send_pushover(message, event, pushover_apitoken, pushover_keys,
                                   pushover_device, notification_type, method, force)

    #
    # Public functions
    #

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('PUSHOVER_ONSNATCH'):
            if fail:
                self._notify(message=title, event=notify_strings[NOTIFY_FAIL], notification_type='note')
            else:
                self._notify(message=title, event=notify_strings[NOTIFY_SNATCH], notification_type='note')

    def notify_download(self, title):
        if CONFIG.get_bool('PUSHOVER_ONDOWNLOAD'):
            self._notify(message=title, event=notify_strings[NOTIFY_DOWNLOAD], notification_type='note')

    def test_notify(self, title="Test"):
        res = self._notify(message="This notification asks for the device list",
                           event=title, notification_type=None, force=True)
        if res:
            _ = self._notify(message="This is a test notification from LazyLibrarian",
                             event=title, notification_type='note', force=True)
        return res

    def update_library(self, show_name=None):
        pass


notifier = PushoverNotifier
