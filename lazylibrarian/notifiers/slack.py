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

import requests

from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import unaccented
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings


class SlackNotifier:

    def __init__(self):
        pass

    @staticmethod
    def _send_slack(message=None, event=None, slack_token=None,
                    method=None, force=False):
        if not CONFIG.get_bool('USE_SLACK') and not force:
            return False

        logger = logging.getLogger(__name__)
        url = CONFIG['SLACK_URL']
        if not url.startswith("http"):
            url = f"https://{url}"
        if not url.endswith("/"):
            url = f"{url}/"

        if slack_token is None:
            slack_token = CONFIG['SLACK_TOKEN']
        if method is None:
            method = 'POST'
        if event == "Test":
            logger.debug("Testing Slack notification")
        else:
            logger.debug(f"Slack message: {event}: {message}")

        if slack_token.startswith(url):
            url = slack_token
        else:
            url = url + slack_token
        headers = {"Content-Type": "application/json"}

        postdata = '{"username": "LazyLibrarian", '
        #   Removed attachment approach to text and icon_url in slack formatting cleanup effort - bbq 20180724
        postdata += (f'"icon_url": "https://{CONFIG["GIT_HOST"]}/{CONFIG["GIT_USER"]}/{CONFIG["GIT_REPO"]}'
                     f'/raw/master/data/images/ll.png", ')
        postdata += f'"text":"{message} {event}"'
        r = requests.request(method,
                             url,
                             data=postdata,
                             headers=headers
                             )
        if r.text.startswith('<!DOCTYPE html>'):
            logger.debug("Slack returned html errorpage")
            return "Invalid or missing Webhook"
        logger.debug(f"Slack returned [{r.text}]")
        return r.text

    def _notify(self, message=None, event=None, slack_token=None, method=None, force=False):
        """
        Sends a slack incoming-webhook notification based on the provided info or LL config

        message: The message string to send
        force: If True then the notification will be sent even if slack is disabled in the config
        """
        logger = logging.getLogger(__name__)
        try:
            message = unaccented(message)
        except Exception as e:
            logger.warning(f"Slack: could not convert message: {e}")
        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_SLACK') and not force:
            return False

        return self._send_slack(message, event, slack_token, method, force)

    #
    # Public functions
    #

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('SLACK_NOTIFY_ONSNATCH'):
            if fail:
                self._notify(message=title, event=notify_strings[NOTIFY_FAIL])
            else:
                self._notify(message=title, event=notify_strings[NOTIFY_SNATCH])

    def notify_download(self, title):
        if CONFIG.get_bool('SLACK_NOTIFY_ONDOWNLOAD'):
            self._notify(message=title, event=notify_strings[NOTIFY_DOWNLOAD])

    def test_notify(self, title="Test"):
        return self._notify(message="This is a test notification from LazyLibrarian",
                            event=title, force=True)


notifier = SlackNotifier
