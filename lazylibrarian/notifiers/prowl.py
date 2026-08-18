import logging
from http.client import HTTPSConnection
from urllib.parse import urlencode

from lazylibrarian.config2 import CONFIG
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings


class ProwlNotifier:
    def __init__(self):
        pass

    @staticmethod
    def _send_prowl(prowl_api=None, prowl_priority=None, event=None, message=None, force=False):
        logger = logging.getLogger(__name__)
        title = "LazyLibrarian"

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_PROWL') and not force:
            return False

        if prowl_api is None:
            prowl_api = CONFIG['PROWL_APIKEY']

        if prowl_priority is None:
            prowl_priority = CONFIG.get_int('PROWL_PRIORITY')

        logger.debug(f"Prowl: title: {title}")
        logger.debug(f"Prowl: event: {event}")
        logger.debug(f"Prowl: message: {message}")

        data = {'event': event,
                'description': message,
                'application': title,
                'apikey': prowl_api,
                'priority': prowl_priority
                }

        try:
            http_handler = HTTPSConnection("api.prowlapp.com")

            http_handler.request("POST",
                                 "/publicapi/add",
                                 headers={'Content-type': "application/x-www-form-urlencoded"},
                                 body=urlencode(data))

            response = http_handler.getresponse()
            request_status = response.status

            if request_status == 200:
                logger.info('Prowl notifications sent.')
                return True
            if request_status == 401:
                logger.info(f'Prowl auth failed: {response.reason}')
                return False
            logger.info('Prowl notification failed.')
            return False

        except Exception as e:
            logger.warning(f'Error sending to Prowl: {e}')
            return False

    #
    # Public functions
    #

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('PROWL_ONSNATCH'):
            if fail:
                self._send_prowl(prowl_api=None, prowl_priority=None, event=notify_strings[NOTIFY_FAIL],
                                 message=title)
            else:
                self._send_prowl(prowl_api=None, prowl_priority=None, event=notify_strings[NOTIFY_SNATCH],
                                 message=title)

    def notify_download(self, title):
        if CONFIG.get_bool('PROWL_ONDOWNLOAD'):
            self._send_prowl(prowl_api=None, prowl_priority=None, event=notify_strings[NOTIFY_DOWNLOAD],
                             message=title)

    # noinspection PyUnusedLocal
    def test_notify(self, title="Test"):
        return self._send_prowl(prowl_api=None, prowl_priority=None, event="Test",
                                message="Testing Prowl settings from LazyLibrarian", force=True)

    def update_library(self, show_name=None):
        pass


notifier = ProwlNotifier
