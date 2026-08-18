import logging

import requests

from lazylibrarian.config2 import CONFIG
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings


class TelegramNotifier:
    def __init__(self):
        pass

    @staticmethod
    def _notify(telegram_token=None, telegram_userid=None, event=None, message=None, force=False):

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_TELEGRAM') and not force:
            return False

        logger = logging.getLogger(__name__)
        telegram_api = "https://api.telegram.org/bot%s/%s"

        if telegram_token is None:
            telegram_token = CONFIG['TELEGRAM_TOKEN']

        if telegram_userid is None:
            telegram_userid = CONFIG['TELEGRAM_USERID']

        logger.debug(f"Telegram: event: {event}")
        logger.debug(f"Telegram: message: {message}")

        # Construct message
        payload = {'chat_id': telegram_userid, 'text': f"{event}: {message}"}

        # Send message to user using Telegram's Bot API
        try:
            url = telegram_api % (telegram_token, "sendMessage")
            logger.debug(url)
            logger.debug(str(payload))
            response = requests.request('POST', url, data=payload)
        except Exception as e:
            logger.warning(f"Telegram notify failed: {str(e)}")
            return False

        if response.status_code == 200:
            return True
        logger.warning(
            f'Could not send notification to TelegramBot (token={telegram_token}). Response: [{response.text}]')
        return False
        #
        # Public functions
        #

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('TELEGRAM_ONSNATCH'):
            if fail:
                self._notify(telegram_token=None, telegram_userid=None, event=notify_strings[NOTIFY_FAIL],
                             message=title)
            else:
                self._notify(telegram_token=None, telegram_userid=None, event=notify_strings[NOTIFY_SNATCH],
                             message=title)

    def notify_download(self, title):
        if CONFIG.get_bool('TELEGRAM_ONDOWNLOAD'):
            self._notify(telegram_token=None, telegram_userid=None, event=notify_strings[NOTIFY_DOWNLOAD],
                         message=title)

    # noinspection PyUnusedLocal
    def test_notify(self, title="Test"):
        return self._notify(telegram_token=None, telegram_userid=None, event="Test",
                            message="Testing Telegram settings from LazyLibrarian", force=True)


notifier = TelegramNotifier
