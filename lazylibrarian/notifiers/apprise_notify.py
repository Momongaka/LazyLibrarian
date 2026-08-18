# This file is part of Lazylibrarian.
#
# Purpose:
#  Handle the general purpose Apprise notification engine. It is optional and is
#  only available if the Apprise module is installed.
import logging

from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import plural
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings

try:
    # noinspection PyUnresolvedReferences
    import apprise

    # noinspection PyUnresolvedReferences
    from apprise import Apprise, AppriseAsset, NotifyType

    APPRISE_CANLOAD = getattr(apprise, '__version__', 'Unknown Version')
except ImportError:
    APPRISE_CANLOAD = ''


# noinspection PyUnresolvedReferences
class AppriseNotifier:

    def __init__(self):
        pass

    @staticmethod
    def _send_apprise(event=None, message=None, url=None):
        logger = logging.getLogger(__name__)
        commslogger = logging.getLogger('special.dlcomms')
        try:
            asset = AppriseAsset()
            asset.default_extension = ".png"
            asset.image_path_mask = "/opt/LazyLibrarian/data/images/{TYPE}-{XY}{EXTENSION}"
            asset.image_url_logo = "https://lazylibrarian.gitlab.io/assets/logo.png"
            asset.image_url_mask = "https://lazylibrarian.gitlab.io/assets/{TYPE}-{XY}{EXTENSION}"
            asset.app_id = "LazyLibrarian"
            asset.app_desc = "LazyLibrarian Announcement"
            asset.app_url = "https://gitlab.com/LazyLibrarian/LazyLibrarian"
            apobj = Apprise(asset=asset)
        except Exception as err:
            logger.error(str(err))
            return False

        if url is not None:
            apobj.add(url)
        else:
            for item in CONFIG.providers('APPRISE'):
                if (event == notify_strings[NOTIFY_DOWNLOAD] and item['DOWNLOAD'] and item['URL']
                        or event == notify_strings[NOTIFY_SNATCH] and item['SNATCH'] and item['URL']
                        or event == notify_strings[NOTIFY_FAIL] and item['SNATCH'] and item['URL']
                        or event == 'Test' and item['URL']):
                    apobj.add(item['URL'])

        if apobj is None:
            commslogger.warning("Apprise notifier is not initialised")
            return False

        if not len(apobj):
            commslogger.debug("Apprise has no matching notifiers configured")
            return False

        title = "LazyLibrarian"

        logger.debug(f"Apprise: event: {event}")
        logger.debug(f"Apprise: message: {message}")
        logger.debug(f"Apprise: url: {str(url)}")
        logger.debug(f"Using {len(apobj)} notification {plural(len(apobj), 'service')}")
        logger.debug(str(asset.details()))
        if event == notify_strings[NOTIFY_SNATCH]:
            notifytype = NotifyType.INFO
        elif event == notify_strings[NOTIFY_DOWNLOAD]:
            notifytype = NotifyType.SUCCESS
        else:
            notifytype = NotifyType.WARNING

        return apobj.notify(title=title, body=f"{event}\n{message}", notify_type=notifytype)

    def _notify(self, event, message, url=None):
        """
        event: The title of the notification to send
        message: The message string to send
        url: to send to one notifier. If None send to all enabled notifiers
        """
        return self._send_apprise(event, message, url)

    #
    # Public functions
    #
    # noinspection PyUnresolvedReferences
    def notify_snatch(self, title, fail=False):
        if APPRISE_CANLOAD:
            if fail:
                return self._notify(event=notify_strings[NOTIFY_FAIL], message=title, url=None)
            return self._notify(event=notify_strings[NOTIFY_SNATCH], message=title, url=None)
        return True

    def notify_download(self, title):
        # noinspection PyUnresolvedReferences
        if APPRISE_CANLOAD:
            return self._notify(event=notify_strings[NOTIFY_DOWNLOAD], message=title, url=None)
        return True

    def test_notify(self, url=None):
        return self._notify(event="Test", message="Testing Apprise settings from LazyLibrarian", url=url)

    # noinspection PyUnresolvedReferences
    @staticmethod
    def notify_types():
        res = []
        if not APPRISE_CANLOAD:
            return res
        try:
            apobj = Apprise()
            schemas = apobj.details()['schemas']
            for item in schemas:
                res.append(item['service_name'])
        except (NameError, AttributeError):
            pass
        return res

    @staticmethod
    def version():
        try:
            apobj = Apprise()
            return apobj.details()['version']
        except NameError:
            return ''


notifier = AppriseNotifier
