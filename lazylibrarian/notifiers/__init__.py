# Author: Nic Wolfe <nic@wolfeden.ca>
# URL: http://code.google.com/p/sickbeard/
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
import traceback

from lazylibrarian.config2 import CONFIG

from ..telemetry import TELEMETRY
from . import (
    androidpn,
    apprise_notify,
    boxcar,
    custom_notify,
    email_notify,
    growl,
    prowl,
    pushbullet,
    pushover,
    slack,
    telegram,
    tweet,
)

# online
twitter_notifier = tweet.TwitterNotifier()
boxcar_notifier = boxcar.BoxcarNotifier()
pushbullet_notifier = pushbullet.PushbulletNotifier()
pushover_notifier = pushover.PushoverNotifier()
androidpn_notifier = androidpn.AndroidPNNotifier()
prowl_notifier = prowl.ProwlNotifier()
growl_notifier = growl.GrowlNotifier()
slack_notifier = slack.SlackNotifier()
email_notifier = email_notify.EmailNotifier()
telegram_notifier = telegram.TelegramNotifier()
apprise_notifier = apprise_notify.AppriseNotifier()
#
custom_notifier = custom_notify.CustomNotifier()

notifiers = [
    twitter_notifier,
    boxcar_notifier,
    pushbullet_notifier,
    pushover_notifier,
    androidpn_notifier,
    prowl_notifier,
    growl_notifier,
    slack_notifier,
    email_notifier,
    telegram_notifier,
    apprise_notifier,
]

APPRISE_VER: str = apprise_notify.APPRISE_CANLOAD


def custom_notify_download(bookid):
    logger = logging.getLogger(__name__)
    try:
        TELEMETRY.record_usage_data('Notify/Download/Custom')
        custom_notifier.notify_download(bookid)
    except Exception as e:
        logger.warning(f'Custom notify download failed: {str(e)}')
        logger.error(f'Unhandled exception: {traceback.format_exc()}')


def custom_notify_snatch(bookid, fail=False):
    logger = logging.getLogger(__name__)
    try:
        TELEMETRY.record_usage_data('Notify/Snatch/Custom')
        custom_notifier.notify_snatch(bookid, fail=fail)
    except Exception as e:
        logger.warning(f'Custom notify snatch failed: {str(e)}')
        logger.error(f'Unhandled exception: {traceback.format_exc()}')


def notify_download(title, bookid=None):
    logger = logging.getLogger(__name__)
    for item in CONFIG.REDACTLIST:
        title = title.replace(item, '******')
    try:
        TELEMETRY.record_usage_data('Notify/Download')
        for n in notifiers:
            if 'EmailNotifier' in str(n):
                n.notify_download(title, bookid=bookid)
            else:
                n.notify_download(title)
    except Exception as e:
        logger.warning(f'Notify download failed: {str(e)}')
        logger.error(f'Unhandled exception: {traceback.format_exc()}')


def notify_snatch(title, fail=False):
    logger = logging.getLogger(__name__)
    for item in CONFIG.REDACTLIST:
        title = title.replace(item, '******')
    try:
        TELEMETRY.record_usage_data('Notify/Snatch')
        for n in notifiers:
            n.notify_snatch(title, fail=fail)
    except Exception as e:
        logger.warning(f'Notify snatch failed: {str(e)}')
        logger.error(f'Unhandled exception: {traceback.format_exc()}')
