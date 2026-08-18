# Author: Nic Wolfe <nic@wolfeden.ca>
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
from urllib.parse import parse_qsl

import lib.oauth2 as oauth
import lib.pythontwitter as twitter
from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import make_bytestr, now
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings


class TwitterNotifier:

    def __init__(self):
        pass

    consumer_key = "208JPTMMnZjtKWA4obcH8g"
    consumer_secret = "BKaHzaQRd5PK6EH8EqPZ1w8mz6NSk9KErArarinHutk"

    REQUEST_TOKEN_URL = 'https://api.twitter.com/oauth/request_token'
    ACCESS_TOKEN_URL = 'https://api.twitter.com/oauth/access_token'
    AUTHORIZATION_URL = 'https://api.twitter.com/oauth/authorize'
    SIGNIN_URL = 'https://api.twitter.com/oauth/authenticate'

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('TWITTER_NOTIFY_ONSNATCH'):
            if fail:
                self._notify_twitter(f"{notify_strings[NOTIFY_FAIL]}: {title}")
            else:
                self._notify_twitter(f"{notify_strings[NOTIFY_SNATCH]}: {title}")

    def notify_download(self, title):
        if CONFIG.get_bool('TWITTER_NOTIFY_ONDOWNLOAD'):
            self._notify_twitter(f"{notify_strings[NOTIFY_DOWNLOAD]}: {title}")

    def test_notify(self):
        return self._notify_twitter(f"This is a test notification from LazyLibrarian / {now()}", force=True)

    def _get_authorization(self):
        logger = logging.getLogger(__name__)
        _ = oauth.SignatureMethod_HMAC_SHA1()
        oauth_consumer = oauth.Consumer(key=self.consumer_key, secret=self.consumer_secret)
        oauth_client = oauth.Client(oauth_consumer)

        logger.debug('Requesting temp token from Twitter')

        resp, content = oauth_client.request(self.REQUEST_TOKEN_URL, 'GET')

        if resp['status'] != '200':
            logger.error(f"Invalid respond from Twitter requesting temp token: {resp['status']}")
            return None

        # noinspection PyDeprecation
        request_token = dict(parse_qsl(content))
        CONFIG.set_str('TWITTER_USERNAME', request_token['oauth_token'])
        CONFIG.set_str('TWITTER_PASSWORD', request_token['oauth_token_secret'])
        logger.debug(
            f"Twitter oauth_token = {CONFIG['TWITTER_USERNAME']} oauth_secret = {CONFIG['TWITTER_PASSWORD']}")
        return f"{self.AUTHORIZATION_URL}?oauth_token={request_token['oauth_token']}"

    def _get_credentials(self, key):
        logger = logging.getLogger(__name__)
        request_token = {'oauth_token': CONFIG['TWITTER_USERNAME'],
                         'oauth_token_secret': CONFIG['TWITTER_PASSWORD'],
                         'oauth_callback_confirmed': 'true'}
        token = oauth.Token(request_token['oauth_token'], request_token['oauth_token_secret'])
        token.set_verifier(key)

        logger.debug(f"Generating and signing request for an access token using key {key}")

        _ = oauth.SignatureMethod_HMAC_SHA1()
        oauth_consumer = oauth.Consumer(key=self.consumer_key, secret=self.consumer_secret)
        logger.debug(f"Twitter oauth_consumer: {str(oauth_consumer)}")
        oauth_client = oauth.Client(oauth_consumer, token)
        resp, content = oauth_client.request(self.ACCESS_TOKEN_URL, method='POST', body=f'oauth_verifier={key}')
        logger.debug(f"resp, content: {str(resp)},{str(content)}")
        if resp['status'] != '200':
            logger.error(f"The request for an access token did not succeed: {str(resp['status'])}")
            return False
        # noinspection PyDeprecation
        access_token = dict(parse_qsl(content))
        logger.debug(f"access_token: {str(access_token)}")
        logger.debug(f"Your Twitter Access Token key: {access_token['oauth_token']}")
        logger.debug(f"Access Token secret: {access_token['oauth_token_secret']}")
        CONFIG.set_str('TWITTER_USERNAME', access_token['oauth_token'])
        CONFIG.set_str('TWITTER_PASSWORD', access_token['oauth_token_secret'])
        return True

    def _send_tweet(self, message=None):
        logger = logging.getLogger(__name__)

        username = self.consumer_key
        password = self.consumer_secret
        access_token_key = CONFIG['TWITTER_USERNAME']
        access_token_secret = CONFIG['TWITTER_PASSWORD']
        if not access_token_key or not access_token_secret:
            logger.error("No authorization found for twitter")
            return False

        logger.info(f"Sending tweet: {message}")

        api = twitter.Api(username, password, access_token_key, access_token_secret)
        message = make_bytestr(message)

        try:
            api.PostUpdate(message)
        except Exception as e:
            logger.error(f"Error Sending Tweet: {e}")
            return False
        return True

    def _notify_twitter(self, message='', force=False):
        prefix = CONFIG['TWITTER_PREFIX']

        if not CONFIG.get_bool('USE_TWITTER') and not force:
            return False

        return self._send_tweet(f"{prefix}: {message}")


notifier = TwitterNotifier
