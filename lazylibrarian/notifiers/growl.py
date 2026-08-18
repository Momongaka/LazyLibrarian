import logging
import os

from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import DIRS, syspath
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings

try:
    import gntp.notifier as gntp_notifier
except ImportError:
    import lib.gntp.notifier as gntp_notifier


class GrowlNotifier:
    def __init__(self):
        pass

    @staticmethod
    def _send_growl(growl_host=None, growl_password=None, event=None, message=None, force=False):
        logger = logging.getLogger(__name__)
        title = "LazyLibrarian"

        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_GROWL') and not force:
            return False

        if not growl_host:
            growl_host = CONFIG['GROWL_HOST']

        if growl_password is None:
            growl_password = CONFIG['GROWL_PASSWORD']

        logger.debug(f"Growl: title: {title}")
        logger.debug(f"Growl: event: {event}")
        logger.debug(f"Growl: message: {message}")

        # Split host and port
        try:
            host, port = growl_host.split(':', 1)
            port = int(port)
        except ValueError:
            logger.debug("Invalid growl host, using default")
            host, port = 'localhost', 23053

        # If password is empty, assume none
        if not growl_password:
            growl_password = None

        try:
            # Register notification
            growl = gntp_notifier.GrowlNotifier(
                applicationName='LazyLibrarian',
                notifications=['New Event'],
                defaultNotifications=['New Event'],
                hostname=host,
                port=port,
                password=growl_password
            )
        except Exception as e:
            logger.error(e)
            return False

        try:
            growl.register()
        except gntp_notifier.errors.NetworkError:
            logger.warning('Growl notification failed: network error')
            return False

        except gntp_notifier.errors.AuthError:
            logger.warning('Growl notification failed: authentication error')
            return False

        # Send it, including an image if available
        image_file = os.path.join(DIRS.PROG_DIR, "data", "images", "ll.png")
        if os.path.exists(image_file):
            with open(syspath(image_file), 'rb') as f:
                image = f.read()
        else:
            image = None

        try:
            # noinspection PyTypeChecker
            growl.notify(
                noteType='New Event',
                title=event,
                description=message,
                icon=image
            )
        except gntp_notifier.errors.NetworkError:
            logger.warning('Growl notification failed: network error')
            return False

        logger.info("Growl notification sent.")
        return True

    #
    # Public functions
    #

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('GROWL_ONSNATCH'):
            if fail:
                self._send_growl(growl_host='', growl_password=None, event=notify_strings[NOTIFY_FAIL], message=title)
            else:
                self._send_growl(growl_host='', growl_password=None, event=notify_strings[NOTIFY_SNATCH], message=title)

    def notify_download(self, title):
        if CONFIG.get_bool('GROWL_ONDOWNLOAD'):
            self._send_growl(growl_host='', growl_password=None, event=notify_strings[NOTIFY_DOWNLOAD], message=title)

    # noinspection PyUnusedLocal
    def test_notify(self, title="Test"):
        return self._send_growl(growl_host='', growl_password=None, event="Test",
                                message="Testing Growl settings from LazyLibrarian", force=True)


notifier = GrowlNotifier
