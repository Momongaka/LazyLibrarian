import logging

from lazylibrarian.config2 import CONFIG
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings

try:
    from pynma import pynma
except ImportError:
    from lib.pynma import pynma


class NmaNotifier:

    def __init__(self):
        pass

    @staticmethod
    def _send_nma(nma_api=None, nma_priority=None, event=None, message=None, force=False):
        logger = logging.getLogger(__name__)
        title = "LazyLibrarian"

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG['USE_NMA'] and not force:
            return False

        if nma_api is None:
            nma_api = CONFIG['NMA_APIKEY']

        if nma_priority is None:
            nma_priority = CONFIG['NMA_PRIORITY']

        logger.debug(f"NMA: title: {title}")
        logger.debug(f"NMA: event: {event}")
        logger.debug(f"NMA: message: {message}")

        batch = False

        p = pynma.PyNMA()
        keys = nma_api.split(',')
        p.addkey(keys)

        if len(keys) > 1:
            batch = True

        response = p.push(title, event, message, priority=nma_priority, batch_mode=batch)

        if response[nma_api]["code"] != "200":
            logger.error("NMA: Could not send notification to NotifyMyAndroid")
            return False
        logger.debug(f"NMA: Success. NotifyMyAndroid returned : {response[nma_api]['code']}")
        return True

    #
    # Public functions
    #

    def notify_snatch(self, title, fail=False):
        if CONFIG['NMA_ONSNATCH']:
            if fail:
                self._send_nma(nma_priority=None, event=notify_strings[NOTIFY_FAIL], message=title)
            else:
                self._send_nma(nma_priority=None, event=notify_strings[NOTIFY_SNATCH], message=title)

    def notify_download(self, title):
        if CONFIG['NMA_ONDOWNLOAD']:
            self._send_nma(nma_priority=None, event=notify_strings[NOTIFY_DOWNLOAD], message=title)

    # noinspection PyUnusedLocal
    def test_notify(self, title="Test"):
        return self._send_nma(nma_priority=None, event="Test",
                              message="Testing NMA settings from LazyLibrarian", force=True)

    def update_library(self, show_name=None):
        pass


notifier = NmaNotifier
