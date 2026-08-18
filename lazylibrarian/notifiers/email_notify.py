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
import os
import smtplib
import ssl
import traceback
import uuid
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

import cherrypy

import lazylibrarian
from lazylibrarian import database, ebook_convert
from lazylibrarian.common import is_valid_email, mime_type, run_script
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import DIRS, path_isfile, splitext, syspath
from lazylibrarian.formatter import check_int, get_list, unaccented
from lazylibrarian.librarysync import get_book_info
from lazylibrarian.scheduling import NOTIFY_DOWNLOAD, NOTIFY_FAIL, NOTIFY_SNATCH, notify_strings


class EmailNotifier:
    def __init__(self):
        pass

    @staticmethod
    def _notify(message, event, force=False, files=None, to_addr=None):
        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_EMAIL') and not force:
            return False

        logger = logging.getLogger(__name__)
        subject = event
        text = message
        oversize = False

        if not to_addr:
            to_addr = CONFIG['EMAIL_TO']
            logger.debug(f"Using default to_addr={to_addr}")
        if not is_valid_email(to_addr):
            logger.warning("Invalid TO address, check users email and/or config")
            return False

        if ',' in to_addr:
            toaddrs = get_list(to_addr)
        else:
            toaddrs = [to_addr]

        for toaddr in toaddrs:
            if files:
                if text[:15].lower() == '<!doctype html>':
                    message = MIMEMultipart("related")
                    message.attach(MIMEText(text, 'html'))
                    if 'cid:logo' in text:
                        image_location = os.path.join(DIRS.PROG_DIR, "data", "images", "ll.png")
                        with open(image_location, "rb") as fp:
                            img = MIMEImage(fp.read())
                        img.add_header("Content-ID", "<logo>")
                        message.attach(img)
                else:
                    message = MIMEMultipart()
                    message.attach(MIMEText(text, 'plain', "utf-8"))
            else:
                if text[:15].lower() == '<!doctype html>':
                    message = MIMEText(text, 'html')
                else:
                    message = MIMEText(text, 'plain', "utf-8")

            message['Subject'] = subject

            if is_valid_email(CONFIG['ADMIN_EMAIL']):
                from_addr = CONFIG['ADMIN_EMAIL']
            elif is_valid_email(CONFIG['EMAIL_FROM']):
                from_addr = CONFIG['EMAIL_FROM']
            else:
                logger.warning("Invalid FROM address, check config settings")
                return False

            message['From'] = formataddr(('LazyLibrarian', from_addr))
            message['To'] = toaddr
            message['Date'] = formatdate(localtime=True)
            message['Message-ID'] = f"<{uuid.uuid4()}@{from_addr.split('@')[1]}>"

            logger.debug(f"Email notification: {message['Subject']}")
            logger.debug(f"Email from: {message['From']}")
            logger.debug(f"Email to: {message['To']}")
            logger.debug(f"Email ID: {message['Message-ID']}")
            if text[:15].lower() == '<!doctype html>':
                logger.debug(f'Email text: {text[:15]}')
            else:
                logger.debug(f'Email text: {text}')
            logger.debug(f'Files: {files}')

            if files:
                for f in files:
                    fsize = check_int(os.path.getsize(syspath(f)), 0)
                    limit = CONFIG.get_int('EMAIL_LIMIT')
                    title = unaccented(os.path.basename(f))
                    if limit and fsize > limit * 1024 * 1024:
                        oversize = True
                        msg = f'{title} is too large ({fsize}) to email'
                        logger.debug(msg)
                        if CONFIG['CREATE_LINK']:
                            logger.debug(f"Creating link to {f}")
                            params = [CONFIG['CREATE_LINK'], f]
                            rc, res, err = run_script(params)
                            if res and res.startswith('http'):
                                msg = f"{title} is available to download, {res}"
                                logger.debug(msg)
                        message.attach(MIMEText(msg))
                    else:
                        if '@kindle.com' in toaddr:
                            # send-to-kindle needs embedded metadata
                            metadata = get_book_info(f)
                            if 'title' not in metadata or 'creator' not in metadata:
                                basename, _ = splitext(f)
                                if os.path.exists(f"{basename}.opf"):
                                    lazylibrarian.metadata_opf.write_meta(os.path.dirname(f), f"{basename}.opf")
                        subtype = mime_type(syspath(f)).split('/')[1]
                        logger.debug(f'Attaching {subtype} {title}')
                        with open(syspath(f), "rb") as fil:
                            part = MIMEApplication(fil.read(), _subtype=subtype, Name=title)
                            part['Content-Disposition'] = f'attachment; filename="{title}"'
                            message.attach(part)

            try:
                # Create a secure SSL context
                context = ssl.create_default_context()
                # but allow no certificate check so self-signed work
                if not CONFIG.get_bool('SSL_VERIFY'):
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE

                if CONFIG.get_bool('EMAIL_SSL'):
                    mailserver = smtplib.SMTP_SSL(CONFIG['EMAIL_SMTP_SERVER'],
                                                  CONFIG.get_int('EMAIL_SMTP_PORT'),
                                                  context=context)
                else:
                    mailserver = smtplib.SMTP(CONFIG['EMAIL_SMTP_SERVER'],
                                              CONFIG.get_int('EMAIL_SMTP_PORT'))

                if CONFIG.get_bool('EMAIL_TLS'):
                    mailserver.starttls(context=context)
                else:
                    mailserver.ehlo()

                if CONFIG['EMAIL_SMTP_USER']:
                    mailserver.login(CONFIG['EMAIL_SMTP_USER'],
                                     CONFIG['EMAIL_SMTP_PASSWORD'])

                logger.debug(f"Sending email to {toaddr}")
                mailserver.sendmail(from_addr, toaddr, message.as_string())
                mailserver.quit()
                if oversize:
                    return False

            except Exception as e:
                logger.warning(f'Error sending Email: {e}')
                logger.error(f'Email traceback: {traceback.format_exc()}')
                return False

        return True

        #
        # Public functions
        #

    def notify_message(self, subject, message, to_addr):
        return self._notify(message=message, event=subject, force=True, to_addr=to_addr)

    def email_file(self, subject, message, to_addr, files):
        logger = logging.getLogger(__name__)
        logger.debug(f"to_addr={to_addr}")
        res = self._notify(message=message, event=subject, force=True, files=files, to_addr=to_addr)
        return res

    def notify_snatch(self, title, fail=False):
        if CONFIG.get_bool('EMAIL_NOTIFY_ONSNATCH'):
            if fail:
                return self._notify(message=title, event=notify_strings[NOTIFY_FAIL])
            return self._notify(message=title, event=notify_strings[NOTIFY_SNATCH])
        return False

    def notify_download(self, title, bookid=None, force=False):
        # suppress notifications if the notifier is disabled but the notify options are checked
        if not CONFIG.get_bool('USE_EMAIL') and not force:
            return False

        logger = logging.getLogger(__name__)
        if CONFIG.get_bool('EMAIL_NOTIFY_ONDOWNLOAD') or force:
            files = None
            event = notify_strings[NOTIFY_DOWNLOAD]
            logger.debug(f"Email send attachment is {CONFIG['EMAIL_SENDFILE_ONDOWNLOAD']}")
            if CONFIG.get_bool('EMAIL_SENDFILE_ONDOWNLOAD'):
                if not bookid:
                    logger.debug('Email request to attach book, but no bookid')
                else:
                    filename = None
                    preftype = None
                    custom_typelist = get_list(CONFIG['EMAIL_SEND_TYPE'])
                    typelist = get_list(CONFIG['EBOOK_TYPE'])

                    if not CONFIG.get_bool('USER_ACCOUNTS'):
                        if custom_typelist:
                            preftype = custom_typelist[0]
                            logger.debug(f'Preferred filetype = {preftype}')
                        elif typelist:
                            preftype = typelist[0]
                            logger.debug(f'Default preferred filetype = {preftype}')
                    else:
                        cookie = cherrypy.request.cookie
                        if cookie and 'll_uid' in list(cookie.keys()):
                            db = database.DBConnection()
                            try:
                                res = db.match('SELECT BookType from users where UserID=?', (cookie['ll_uid'].value,))
                            finally:
                                db.close()
                            if res and res['BookType']:
                                preftype = res['BookType']
                                logger.debug(f'User preferred filetype = {preftype}')
                            else:
                                logger.debug(f"No user preference for {cookie['ll_uid'].value}")
                        else:
                            logger.debug('No user login cookie')
                        if not preftype and typelist:
                            preftype = typelist[0]
                            logger.debug(f'Default preferred filetype = {preftype}')

                    db = database.DBConnection()
                    try:
                        data = db.match('SELECT BookFile,BookName from books where BookID=?', (bookid,))
                        if data:
                            bookfile = data['BookFile']
                            types = []
                            if bookfile and path_isfile(bookfile):
                                basename, extn = splitext(bookfile)
                                for item in set(
                                        typelist + custom_typelist):
                                    # Search download and email formats for existing book formats
                                    target = f"{basename}.{item}"
                                    if path_isfile(target):
                                        types.append(item)

                                logger.debug(f'Available filetypes: {str(types)}')

                                # if the format we want to send is available, select it
                                if preftype in types:
                                    filename = f"{basename}.{preftype}"
                                    logger.debug(f'Found preferred filetype {preftype}')
                                # if the format is not available, see if it's a type we want to convert,
                                # otherwise send the first available format
                                else:
                                    # if there is a type we want to convert from in the available formats,
                                    # convert it
                                    for convertable_format in get_list(CONFIG['EMAIL_CONVERT_FROM']):
                                        if convertable_format in types:
                                            logger.debug(
                                                f'Converting {convertable_format} to preferred filetype {preftype}')
                                            # noinspection PyBroadException
                                            try:
                                                filename = ebook_convert.convert(f"{basename}.{convertable_format}",
                                                                                 preftype)
                                                logger.debug(
                                                    f'Converted {convertable_format} to preferred filetype {preftype}')
                                                break
                                            except Exception:
                                                logger.debug(f"Conversion {convertable_format} to {preftype} failed")
                                                continue
                                    # If no convertable formats found, revert to default behavior of sending
                                    # first available format
                                    else:
                                        logger.debug(f'Preferred filetype {preftype} not found, sending {types[0]}')
                                        filename = f"{basename}.{types[0]}"

                            if force:
                                event = title
                                if filename:
                                    title = lazylibrarian.NEWFILE_MSG.replace('{name}', data['BookName']).replace(
                                        '{method}', ' is attached').replace('{link}', '')
                                else:
                                    title = lazylibrarian.NEWFILE_MSG.replace('{name}', data['BookName']).replace(
                                        '{method}', ' is not available').replace('{link}', '')
                            else:
                                title = data['BookName']
                            logger.debug(f'Found {filename} for bookid {bookid}')
                        else:
                            logger.debug(f'[{bookid}] is not a valid bookid')
                            data = db.match('SELECT IssueFile,Title,IssueDate from issues where IssueID=?', (bookid,))
                            if data:
                                filename = data['IssueFile']
                                title = f"{data['Title']} - {data['IssueDate']}"
                                logger.debug(f'Found {filename} for issueid {bookid}')
                            else:
                                logger.debug(f'[{bookid}] is not a valid issueid')
                                filename = ''
                    finally:
                        db.close()
                    if filename:
                        files = [filename]  # could add cover_image, opf
                        event = "LazyLibrarian Download"
            return self._notify(message=title, event=event, force=force, files=files)
        return False

    def test_notify(self, title='This is a test notification from LazyLibrarian'):
        if CONFIG.get_bool('EMAIL_SENDFILE_ONDOWNLOAD'):
            db = database.DBConnection()
            try:
                data = db.match("SELECT bookid from books where bookfile <> ''")
            finally:
                db.close()
            if data:
                return self.notify_download(title=title, bookid=data['bookid'], force=True)
        return self.notify_download(title=title, bookid=None, force=True)


notifier = EmailNotifier
