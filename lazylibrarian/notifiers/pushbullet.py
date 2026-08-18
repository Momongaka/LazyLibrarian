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

from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import unaccented
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings

from .pushbullet2 import PushBullet


class PushbulletNotifier:

    def __init__(self):
        pass

    @staticmethod
    def _send_pushbullet(message=None, event=None, pushbullet_token=None, pushbullet_deviceid=None, force=False):

        if not CONFIG['USE_PUSHBULLET'] and not force:
            return False

        logger = logging.getLogger(__name__)
        if pushbullet_token is None:
            pushbullet_token = CONFIG['PUSHBULLET_TOKEN']
        if pushbullet_deviceid is None and CONFIG['PUSHBULLET_DEVICEID']:
            pushbullet_deviceid = CONFIG['PUSHBULLET_DEVICEID']

        logger.debug(f"Pushbullet event: {str(event)}")
        logger.debug(f"Pushbullet message: {str(message)}")
        logger.debug(f"Pushbullet api: {str(pushbullet_token)}")
        logger.debug(f"Pushbullet devices: {str(pushbullet_deviceid)}")

        pb = PushBullet(str(pushbullet_token))

        if event == 'DeviceList':  # special case, return device list
            devices = pb.get_devices()
            ret = ""
            for device in devices:
                if device["active"]:
                    logger.info(f"Pushbullet: {device['nickname']} [{device['iden']}]")
                    ret += f"\nPushbullet: {device['nickname']} [{device['iden']}]"
            _ = pb.push_note(pushbullet_deviceid, str(event), str(message))
            return ret
        push = pb.push_note(pushbullet_deviceid, str(event), str(message))
        return push

    def _notify(self, message=None, event=None, pushbullet_token=None, pushbullet_deviceid=None, force=False):
        """
        Sends a pushbullet notification based on the provided info or LL config

        title: The title of the notification to send
        message: The message string to send
        username: The username to send the notification to (optional, defaults to the username in the config)
        force: If True then the notification will be sent even if pushbullet is disabled in the config
        """
        logger = logging.getLogger(__name__)
        try:
            message = unaccented(message)
        except Exception as e:
            logger.warning(f"Pushbullet: could not convert  message: {e}")

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG['USE_PUSHBULLET'] and not force:
            return False
        logger.debug(f"Pushbullet: Sending notification {str(message)}")

        return self._send_pushbullet(message, event, pushbullet_token, pushbullet_deviceid, force=force)

    #
    # Public functions
    #

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('PUSHBULLET_NOTIFY_ONSNATCH'):
            if fail:
                self._notify(message=title, event=notify_strings[NOTIFY_FAIL])
            else:
                self._notify(message=title, event=notify_strings[NOTIFY_SNATCH])

    def notify_download(self, title):
        if CONFIG.get_bool('PUSHBULLET_NOTIFY_ONDOWNLOAD'):
            self._notify(message=title, event=notify_strings[NOTIFY_DOWNLOAD])

    def test_notify(self, title="Test"):
        res = self._notify("This test notification asks for the device list", event='DeviceList', force=True)
        if res:
            _ = self._notify("This is a test notification from LazyLibrarian", event=title, force=True)
        return res

    def update_library(self, show_name=None):
        pass


notifier = PushbulletNotifier
