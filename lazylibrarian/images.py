#  This file is part of Lazylibrarian.
#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.


import io
import json
import logging
import os
import string
import subprocess
import tempfile
import traceback
import zipfile
from secrets import choice
from shutil import rmtree
from urllib.parse import quote_plus

import requests

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.blockhandler import BLOCKHANDLER
from lazylibrarian.cache import ImageType, cache_img, fetch_url
from lazylibrarian.common import get_user_agent
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import (
    DIRS,
    jpg_file,
    path_isfile,
    safe_copy,
    safe_move,
    setperm,
    splitext,
    syspath,
)
from lazylibrarian.formatter import (
    check_int,
    get_list,
    is_valid_type,
    make_unicode,
    make_utf8bytes,
    plural,
    safe_unicode,
    sort_definite,
)

try:
    import PIL
except ImportError:
    PIL = None
if PIL:
    # noinspection PyUnresolvedReferences
    from PIL import Image as PILImage

    from lib.icrawler.builtin import (
        BaiduImageCrawler,
        BingImageCrawler,
        FlickrImageCrawler,
        GoogleImageCrawler,
    )
else:
    GoogleImageCrawler = None
    BingImageCrawler = None
    BaiduImageCrawler = None
    FlickrImageCrawler = None
    PILImage = None

# noinspection PyProtectedMember
from pypdf import PdfReader, PdfWriter

# noinspection PyBroadException
try:
    import magic
except Exception:  # magic might fail for multiple reasons
    magic = None

GS = ''
GS_VER = ''
generator = ''

# these are currently blocked, broken, or need api key
# flickr needs an apikey and doesn't seem to have authors or book covers
# baidu doesn't like bots, message: "Forbid spider access"
# librarything currently gived 403 errors
# googleimages and googleisbn now require a browser with javascript enabled
# (both are switched off by googleapis)
# bing gives seemingly random results, some NSFW, sometimes based on first word of
# search query, eg "Michael Agnew" returns lots of pictures of Michael Jackson
force_ignore = 'flikr, baidu, librarything, googleapis, bing'


def img_id(length=10):
    return ''.join([choice(string.ascii_letters + string.digits) for _ in range(length)])


def createthumbs(jpeg):
    if not PIL:
        return
    for basewidth in (100, 200, 300, 500):
        createthumb(jpeg, basewidth, overwrite=False)


def createthumb(jpeg, basewidth=None, overwrite=True):
    if not PILImage:
        return ''
    logger = logging.getLogger(__name__)
    fname, extn = splitext(jpeg)
    outfile = f"{fname}_w{basewidth}{extn}" if basewidth else f"{fname}_thumb{extn}"

    if not overwrite and path_isfile(outfile):
        return outfile

    if not path_isfile(jpeg):
        logger.debug(f"Cannot open {jpeg} for thumbnail")
        return ''

    bwidth = basewidth if basewidth else 300
    try:
        img = PILImage.open(jpeg)
    except Exception as e:
        logger.debug(str(e))
        if magic:
            try:
                mtype = magic.from_file(jpeg).upper()
                logger.debug(f"magic reports {mtype}")
            except Exception as e:
                logger.debug(f"{type(e).__name__} reading magic from {jpeg}, {e}")
        return ''

    wpercent = (bwidth / float(img.size[0]))
    hsize = int(float(img.size[1]) * float(wpercent))
    try:
        # noinspection PyUnresolvedReferences
        img = img.resize((bwidth, hsize), PIL.Image.Resampling.LANCZOS)
        img.save(outfile)
    except OSError:
        try:
            img.convert('RGB').save(outfile)
        except OSError:
            return ''
    logger.debug(f"Created {outfile}")
    setperm(outfile)
    return outfile


def valid_pdf(sourcefile):
    # check we can read/parse the pdf file, ie it's not corrupted
    logger = logging.getLogger(__name__)
    if PdfWriter is None:
        logger.warning("pypdf is not loaded")
        return False
    _, extn = splitext(sourcefile)
    if extn.lower() != '.pdf':
        logger.warning(f"Cannot swap cover on [{sourcefile}]")
        return False

    try:
        writer = PdfWriter()
        with open(sourcefile, "rb") as f:
            reader = PdfReader(f, strict=True)
            cnt = reader.get_num_pages()
            logger.debug(f"{sourcefile} has {cnt} pages")
            p = 0
            while p < cnt:
                writer.add_page(reader.pages[p])
                p += 1
        return True
    except Exception as e:
        logger.warning(str(e))
        return False


def coverswap(sourcefile, coverpage=2):
    logger = logging.getLogger(__name__)
    if PdfWriter is None:
        logger.warning("pypdf is not loaded")
        return False

    _, extn = splitext(sourcefile)
    if extn.lower() != '.pdf':
        logger.warning(f"Cannot swap cover on [{sourcefile}]")
        return False
    try:
        # reordering pages is quite slow if the source is on a networked drive
        # so work on a local copy, then move it over.
        original = sourcefile
        logger.debug(f"Copying {original}")
        try:
            srcfile = safe_copy(original, os.path.join(DIRS.CACHEDIR, os.path.basename(sourcefile)))
        except Exception as e:
            logger.warning(f"Failed to copy source file: {str(e)}")
            return False
        writer = PdfWriter()
        with open(srcfile, "rb") as f:
            reader = PdfReader(f)
            cnt = reader.get_num_pages()
            logger.debug(f"{srcfile} has {cnt} pages, new cover from page {coverpage}")
            coverpage -= 1  # zero based page count
            writer.add_page(reader.pages[coverpage])
            p = 0
            while p < cnt:
                if p != coverpage:
                    writer.add_page(reader.pages[p])
                p += 1
            with open(f"{srcfile}new", "wb") as outputStream:
                writer.write(outputStream)
            logger.debug(f"Written new output file {srcfile}new")
        # windows does not allow rename to overwrite an existing file
        # but shutil.copyfile used by safe_copy should work
        try:
            logger.debug(f"Copying {srcfile}new to {original}")
            _ = safe_copy(f"{srcfile}new", original)
        except Exception as e:
            logger.warning(f"Failed to copy output file: {str(e)}")
            return False
        os.remove(srcfile)
        os.remove(f"{srcfile}new")
        logger.info(f"{sourcefile} has {cnt:d} pages. Swapped pages 1 and 2\n")
        return True

    except Exception as e:
        logger.warning(str(e))
        return False


def get_author_images():
    """ Try to get an author image for all authors without one"""
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    msg = ''
    try:
        cmd = ("select AuthorID, AuthorName from authors where (instr(AuthorImg, 'nophoto') > 0 or "
               "AuthorImg is null) and Manual is not '1'")
        authors = db.select(cmd)
        if authors:
            logger.info(f'Checking images for {len(authors)} {plural(len(authors), "author")}')
            counter = 0
            for author in authors:
                authorid = author['AuthorID']
                imagelink = get_author_image(authorid)
                new_value_dict = {}
                if not imagelink:
                    logger.debug(f"No image found for {author['AuthorName']}")
                    new_value_dict = {"AuthorImg": 'images/nophoto.png'}
                elif 'nophoto' not in imagelink:
                    logger.debug(f"Updating {author['AuthorName']} image to {imagelink}")
                    new_value_dict = {"AuthorImg": imagelink}

                if new_value_dict:
                    counter += 1
                    control_value_dict = {"AuthorID": authorid}
                    db.upsert("authors", new_value_dict, control_value_dict)

            msg = f"Updated {counter} {plural(counter, 'image')}"
            logger.info(f"Author Image check complete: {msg}")
        else:
            msg = 'No missing author images'
            logger.debug(msg)
    except Exception as e:
        logger.debug(str(e))
    db.close()
    return msg


def get_book_covers():
    """ Try to get a cover image for all books """

    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    msg = ''
    try:
        cmd = ("select BookID,BookImg from books where instr(BookImg,'nocover') > 0 "
               "or instr(BookImg, 'nophoto') > 0 and Manual is not '1'")
        books = db.select(cmd)
        if books:
            logger.info(f"Checking covers for {len(books)} {plural(len(books), 'book')}")
            counter = 0
            for book in books:
                bookid = book['BookID']
                coverlink, _ = get_book_cover(bookid)
                if coverlink and "nocover" not in coverlink and "nophoto" not in coverlink:
                    control_value_dict = {"BookID": bookid}
                    new_value_dict = {"BookImg": coverlink}
                    db.upsert("books", new_value_dict, control_value_dict)
                    counter += 1
                if not coverlink and "http" in book['BookImg']:
                    control_value_dict = {"BookID": bookid}
                    new_value_dict = {"BookImg": "images/nocover.png"}
                    db.upsert("books", new_value_dict, control_value_dict)
            msg = f"Updated {counter} {plural(counter, 'cover')}"
            logger.info(f"Cover check complete: {msg}")
        else:
            msg = 'No missing book covers'
            logger.debug(msg)
    except Exception as e:
        logger.debug(str(e))
    db.close()
    return msg


def cache_bookimg(img, bookid, src, suffix='', imgid=None):
    logger = logging.getLogger(__name__)
    if not imgid:
        imgid = bookid
    if src:
        coverlink, success, _ = cache_img(ImageType.BOOK, imgid + suffix, img)
    else:
        coverlink, success, _ = cache_img(ImageType.BOOK, imgid, img, refresh=True)
        src = suffix

    # if librarything has no image they return a 1x1 gif
    data = ''
    coverfile = os.path.join(DIRS.DATADIR, coverlink)
    if path_isfile(coverfile):
        with open(syspath(coverfile), 'rb') as f:
            data = f.read()
    if len(data) < 50:
        logger.debug(f"Got an empty {src} image for {bookid} [{img}]")
    elif success:
        logger.debug(f"Caching {src} cover for {bookid}")
        return coverlink
    else:
        logger.debug(f"Failed to cache {src} image for {img} [{coverlink}]")
    return ''


def get_book_cover(bookid=None, src=None, ignore=''):
    """ Return link to a local file containing a book cover image for a bookid, and which source used.
        Try  Local file cached from goodreads/googlebooks when book was imported
             cover.jpg if we have the book
             LibraryThing cover image (if you have a dev key)
             Goodreads search (if book was imported from goodreads)
             OpenLibrary image
             RanobeDB image (if book was imported from ranobedb)
             Google isbn search (if google has a link to book for sale)
             Google images search (if lazylibrarian config allows)

        src = cache, cover, goodreads, librarything, googleisbn, openlibrary, googleimage, ranobedb
        ignore = csv of sources to skip
        Return None if no cover available. """
    logger = logging.getLogger(__name__)
    if not bookid:
        logger.error("get_book_cover- No bookID")
        return None, src

    if not src:
        src = ''
        imgid = img_id()
    else:
        imgid = None

    if ignore:
        ignore += ', '
    ignore += force_ignore

    logger.debug(f"Getting cover for {bookid}, ignore [{ignore}], src {src}")
    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        cachedir = DIRS.CACHEDIR
        item = db.match('select BookImg from books where bookID=?', (bookid,))
        if item and item['BookImg']:
            coverlink = item['BookImg']
            coverfile = os.path.join(cachedir, coverlink.replace('cache/', ''))
            if coverlink != 'images/nocover.png' and 'nocover' in coverlink or 'nophoto' in coverlink:
                coverfile = os.path.join(DIRS.DATADIR, 'images', 'nocover.png')
                coverlink = 'images/nocover.png'
                db.action("UPDATE books SET BookImg=? WHERE BookID=?", (coverlink, bookid))
        else:
            coverlink = f"cache/book/{bookid}.jpg"
            coverfile = os.path.join(cachedir, "book", f"{bookid}.jpg")
        if not src or src == 'cache' or src == 'current':
            if path_isfile(coverfile):  # use cached image if there is one
                lazylibrarian.CACHE_HIT = int(lazylibrarian.CACHE_HIT) + 1
                return coverlink, 'cache'
            if src:
                lazylibrarian.CACHE_MISS = int(lazylibrarian.CACHE_MISS) + 1
                return None, src

        if not src or src == 'cover' and 'cover' not in ignore:
            item = db.match('select BookFile,Audiofile from books where bookID=?', (bookid,))
            if item:
                # get either ebook or audiobook if they exist
                bookfile = item['BookFile'] or item['AudioFile']
                if bookfile and path_isfile(bookfile):  # we may have a cover.jpg in the same folder
                    bookdir = os.path.dirname(bookfile)
                    coverimg = jpg_file(bookdir)
                    if coverimg:
                        if src:
                            extn = '_cover.jpg'
                        else:
                            extn = '.jpg'

                        coverfile = os.path.join(cachedir, "book", bookid + extn)
                        coverlink = f"cache/book/{bookid}{extn}"
                        logger.debug(f"Caching {extn} for {bookid}")
                        try:
                            _ = safe_copy(coverimg, coverfile)
                        except Exception as e:
                            logger.warning(f"Failed to copy cover file: {str(e)}")
                        return coverlink, src
                    logger.debug(f"No cover found for {bookid} in {bookdir}")
                else:
                    if bookfile:
                        logger.debug(f"File {bookfile} not found")
            else:
                logger.debug(f"BookID {bookid} not found")
            if src:
                return None, src

        # see if librarything has a cover
        if not src or src == 'librarything' and 'librarything' not in ignore:
            if CONFIG['LT_DEVKEY']:
                cmd = "select BookISBN from books where bookID=?"
                item = db.match(cmd, (bookid,))
                if item and item['BookISBN']:
                    img = '/'.join([CONFIG['LT_URL'], f"devkey/{CONFIG['LT_DEVKEY']}/large/isbn/{item['BookISBN']}"])
                    coverlink = cache_bookimg(img, bookid, src, suffix='_lt', imgid=imgid)
                    if coverlink:
                        return coverlink, 'librarything'
                else:
                    logger.debug(f"No isbn for {bookid}")
            if src:
                return None, src

        cmd = ("select BookName,AuthorName,BookLink,BookISBN,books.gr_id,books.hc_id,books.ol_id,books.ran_id"
               " from books,authors where bookID=? and books.AuthorID = authors.AuthorID")
        item = db.match(cmd, (bookid,))
        if not item:
            return None, src

        title = safe_unicode(item['BookName'])
        author = safe_unicode(item['AuthorName'])
        safeparams = quote_plus(make_utf8bytes(f"{author} {title}")[0])

        # see if hardcover has a cover
        if not src or src == 'hardcover' and 'hardcover' not in ignore:
            if item['hc_id']:
                h_c = lazylibrarian.hc.HardCover()
                bookdict, _ = h_c.get_bookdict_for_bookid(item['hc_id'])
                img = bookdict.get('bookimg')
                if img:
                    coverlink = cache_bookimg(img, bookid, src, suffix='_hc', imgid=imgid)
                    if coverlink:
                        return coverlink, 'hardcover'
                logger.debug(f"No img in hardcover bookdict {bookdict}")
            if src:
                return None, src

        # see if ranobedb has a cover
        if not src or src == 'ranobedb' and 'ranobedb' not in ignore:
            if item['ran_id']:
                r_a = lazylibrarian.ran.RanobeDB()
                bookdict, _ = r_a.get_bookdict_for_bookid(item['ran_id'])
                img = bookdict.get('bookimg')
                if img:
                    coverlink = cache_bookimg(img, bookid, src, suffix='_ra', imgid=imgid)
                    if coverlink:
                        return coverlink, 'ranobedb'
                logger.debug(f"No img in ranobedb bookdict {bookdict}")
            if src:
                return None, src

        # try to get a cover from goodreads
        if not src or src == 'goodreads' and 'goodreads' not in ignore:
            if item['gr_id']:
                # if we have a goodreads bookid, we can call https://www.goodreads.com/book/show/{bookID}
                # and scrape the page for og:image
                # <meta property="og:image" content="https://i.gr-assets.com/images/S/photo.goodreads.com/books/
                # 1388267702i/16304._UY475_SS475_.jpg"/>
                # to get the cover
                booklink = '/'.join([CONFIG['GR_URL'], 'book', 'show', item['gr_id']])
                result, success = fetch_url(booklink)
                if success:
                    try:
                        img = result.split('id="coverImage"')[1].split('src="')[1].split('"')[0]
                    except IndexError:
                        try:
                            img = result.split('og:image')[1].split('="')[1].split('"')[0]
                        except IndexError:
                            img = None
                    if img and img.startswith('http') and 'nocover' not in img and 'nophoto' not in img:
                        coverlink = cache_bookimg(img, bookid, src, suffix='_gr', imgid=imgid)
                        if coverlink:
                            return coverlink, 'goodreads'
                    else:
                        logger.debug(f"No image found in goodreads page for {bookid}")
                else:
                    logger.debug(f"Error getting goodreads page {booklink}, [{result}]")
            if src:
                return None, src

        # try to get a cover from openlibrary
        if not src or src == 'openlibrary' and 'openlibrary' not in ignore:
            if item and item['ol_id']:
                baseurl = f"https://covers.openlibrary.org/b/olid/{item['ol_id']}-M.jpg"
                result, success = fetch_url(baseurl, raw=True)
                if success:
                    with tempfile.NamedTemporaryFile(delete_on_close=False) as f:
                        f.write(result)
                        f.close()
                        coverlink = cache_bookimg(f.name, bookid, src, suffix='_ol', imgid=imgid)
                        if coverlink:
                            return coverlink, 'openlibrary'

            if item and item['BookISBN']:
                baseurl = '/'.join([CONFIG['OL_URL'],
                                   'api/books?format=json&jscmd=data&bibkeys=ISBN:'])
                result, success = fetch_url(baseurl + item['BookISBN'])
                if success:
                    try:
                        source = json.loads(result)  # type: dict
                    except Exception as e:
                        logger.debug(f"OpenLibrary json error: {e}")
                        source = {}

                    img = ''
                    if source:
                        # noinspection PyUnresolvedReferences
                        k = list(source.keys())[0]
                        try:
                            img = source[k]['cover']['medium']
                        except KeyError:
                            try:
                                img = source[k]['cover']['large']
                            except KeyError:
                                logger.debug(f"No openlibrary image for {item['BookISBN']}")

                    if img and img.startswith('http') and 'nocover' not in img and 'nophoto' not in img:
                        coverlink = cache_bookimg(img, bookid, src, suffix='_ol', imgid=imgid)
                        if coverlink:
                            return coverlink, 'openlibrary'
                else:
                    logger.debug(f"OpenLibrary error: {result}")
                    BLOCKHANDLER.block_provider("openlibrary", result)
            if src:
                return None, src

        if not src or src == 'googleisbn' and 'googleapis' not in ignore:
            # try a google isbn page search...
            if item and item['BookISBN']:
                url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{item['BookISBN']}"
                result, success = fetch_url(url)
                if success:
                    img = ''
                    try:
                        source = json.loads(result)  # type: dict
                        img = source["items"][0]["volumeInfo"]["imageLinks"]["thumbnail"]
                    except (IndexError, KeyError):
                        pass
                    except Exception as e:
                        logger.debug(f"GoogleISBN {type(e).__name__}: {e}")

                    if img:
                        coverlink = cache_bookimg(img, bookid, src, suffix='_gi', imgid=imgid)
                        if coverlink:
                            return coverlink, 'googleisbn'
                    else:
                        logger.debug(f"No image found in google isbn page for {bookid}")
                else:
                    logger.debug("Failed to fetch url from google")
            else:
                logger.debug(f"No isbn to search for {bookid}")
            if src:
                return None, src

        if PIL and safeparams:
            if not src or src == 'baidu' and 'baidu' not in ignore:
                res, src = crawl_image('baidu', src, cachedir, bookid, safeparams, imgid=imgid)
                if res:
                    return res, src
            if not src or src == 'bing' and 'bing' not in ignore:
                res, src = crawl_image('bing', src, cachedir, bookid, safeparams, imgid=imgid)
                if res:
                    return res, src
            if not src or src == 'flikr' and 'flikr' not in ignore:
             res, src = crawl_image('flickr', src, cachedir, bookid, safeparams, imgid=imgid)
             if res:
                 return res, src
            if not src or src == 'googleimage' and 'googleapis' not in ignore:
                res, src = crawl_image('google', src, cachedir, bookid, safeparams, imgid=imgid)
                if res:
                    return res, src
        return None, src
    except Exception:
        logger.error(f'Unhandled exception in get_book_cover: {traceback.format_exc()}')
    finally:
        db.close()
    return None, src


def crawl_image(crawler_name, src, cachedir, bookid, safeparams, imgid=None):
    """ Searches for images, finds at most one image.
      crawler_name: baidu, bing, flickr or google
      src: Used to name the file. Same as crawler, or googleimage
      cachedir: Files found will be stored in a subdir of this
      bookid: ID of the book
      safeparams: Search used in the search
    Returns
      None, src if the search is unsuccessful
      coverlink, crawler_name if successful
    """
    logger = logging.getLogger(__name__)
    # for key in logging.Logger.manager.loggerDict:
    #      print(key)
    logging.getLogger('lib.icrawler').setLevel(logging.CRITICAL)
    logging.getLogger('feeder').setLevel(logging.CRITICAL)
    logging.getLogger('downloader').setLevel(logging.CRITICAL)
    logging.getLogger('parser').setLevel(logging.CRITICAL)

    icrawlerdir = os.path.join(cachedir, 'icrawler', bookid)
    if crawler_name == 'baidu':
        crawler = BaiduImageCrawler(storage={'root_dir': icrawlerdir})
    elif crawler_name == 'bing':
        crawler = BingImageCrawler(storage={'root_dir': icrawlerdir})
    elif crawler_name == 'flickr':
        crawler = FlickrImageCrawler(storage={'root_dir': icrawlerdir})
    else:
        crawler_name = 'google'
        crawler = GoogleImageCrawler(storage={'root_dir': icrawlerdir})

    crawler.crawl(keyword=safeparams, max_num=1)
    if os.path.exists(icrawlerdir):
        res = len(os.listdir(icrawlerdir))
    else:
        res = 0
    logger.debug(f"{crawler_name} found {res} {plural(res, 'image')}")
    if res:
        img = os.path.join(icrawlerdir, os.listdir(icrawlerdir)[0])

        coverlink = cache_bookimg(img, bookid, src, suffix=f"_{crawler_name[:2]}", imgid=imgid)
        rmtree(icrawlerdir, ignore_errors=True)
        if coverlink:
            return coverlink, crawler_name
    else:
        logger.debug(f"No images found in {crawler_name} page for {bookid}")
    # rmtree(icrawlerdir, ignore_errors=True)
    return None, src


def get_author_image(authorid=None, refresh=False, max_num=1, ignore=''):
    logger = logging.getLogger(__name__)
    if not authorid:
        logger.error("get_author_image: No authorid")
        return None
    db = database.DBConnection()
    try:
        keys = lazylibrarian.importer.author_keys()
        cmd = "SELECT * from authors WHERE AuthorID=?"
        for k in keys:
            cmd += f" or {k}=?"
        author = db.match(cmd, tuple([str(authorid)] * (len(keys) + 1)))
    finally:
        db.close()

    if ignore:
        ignore += ', '
    ignore += force_ignore

    cachedir = DIRS.CACHEDIR
    datadir = DIRS.DATADIR
    got_images = 0
    cnt = 0

    icrawlerdir = os.path.join(cachedir, 'icrawler', str(authorid))
    rmtree(icrawlerdir, ignore_errors=True)
    if not os.path.isdir(icrawlerdir):
        os.mkdir(icrawlerdir)

    if author:
        coverfile = os.path.join(datadir, author['AuthorIMG'])
    else:
        coverfile = os.path.join(cachedir, "author", f"{authorid}.jpg")

    if path_isfile(coverfile):
        if max_num == 1 and not refresh:  # use cached image if there is one
            lazylibrarian.CACHE_HIT = int(lazylibrarian.CACHE_HIT) + 1
            logger.debug(f"get_author_image: Returning Cached response for {coverfile}")
            coverlink = coverfile.lstrip(datadir)
            return coverlink
        _ = safe_copy(coverfile, os.path.join(icrawlerdir, 'cover.jpg'))
        got_images += 1
    else:
        lazylibrarian.CACHE_MISS = int(lazylibrarian.CACHE_MISS) + 1

    if PIL and author:
        authorname = safe_unicode(author['AuthorName'])
        safeparams = authorname.replace('. ', ' ').replace(' ', '_')
        headers = {
            'User-Agent': get_user_agent(),
            'Accept': 'application/xml, text/xml',
            'Accept-Language': 'en-US,en;q=0.9,de;q=0.8',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive'
        }

        for api_source in lazylibrarian.INFOSOURCES.keys():
            if got_images >= max_num:
                break
            this_source = lazylibrarian.INFOSOURCES[api_source]
            # 2-letter_code, class, author_key, api_enabled
            if this_source['author_key'] != 'authorid' and CONFIG[this_source['enabled']] and api_source not in ignore:
                crawler_name = api_source
                book_api = this_source['api']
                book_api = book_api()
                img = book_api.get_author_image(authorname=authorname, authorid=author[this_source['author_key']])
                if img.startswith('http'):
                    img_data = requests.get(img, headers=headers)
                    if str(img_data.status_code).startswith('2'):
                        img_file = os.path.join(icrawlerdir, f"{api_source}.jpg")
                        with open(img_file, 'wb') as f:
                            f.write(img_data.content)
                            got_images += 1
                            logger.debug(f"{crawler_name} found an image")
                    else:
                        logger.debug(f"Got {img_data.status_code} from {crawler_name} image {img}")
                else:
                    logger.debug(f"No image from {crawler_name} for {author[this_source['author_key']]}")

        if got_images < max_num:
            crawler_name = 'wikipedia'
            if crawler_name not in ignore:
                try:
                    url = f"https://en.wikipedia.org/wiki/{safeparams}"
                    response = requests.get(url, headers=headers)
                    if str(response.status_code).startswith('2'):
                        try:
                            img_name = make_unicode(response.content.split(b"infobox-image")[1].split(b'src="')[1].split(b'"')[0])
                        except IndexError:
                            logger.debug(f"No image from wikipedia for {safeparams}")
                            img_name = None
                        if img_name:
                            img_data = requests.get(f"https:{img_name}", headers=headers)
                            if str(img_data.status_code).startswith('2'):
                                img_file = os.path.join(icrawlerdir, f'{crawler_name}.jpg')
                                with open(img_file, 'wb') as f:
                                    f.write(img_data.content)
                                    got_images += 1
                                    logger.debug(f"{crawler_name} found an image")
                            else:
                                logger.debug(f"Got a {img_data.status_code} from {crawler_name} image {img_name}")
                    else:
                        logger.debug(f"Got a {response.status_code} from {crawler_name} search {url}")
                except Exception as e:
                    logger.debug(str(e))

        if got_images < max_num:
            safeparams = quote_plus(make_utf8bytes(f"author {authorname}")[0])
            crawler_name = 'google'
            if crawler_name not in ignore:
                gc = GoogleImageCrawler(storage={'root_dir': icrawlerdir})
                gc.crawl(keyword=safeparams, max_num=int(max_num - got_images), file_idx_offset='auto')
                if os.path.exists(icrawlerdir):
                    cnt = len(os.listdir(icrawlerdir))
                    logger.debug(f"{crawler_name} found {cnt - got_images} {plural(cnt - got_images, 'image')}")
                if cnt < max_num:
                    got_images = cnt
                    # not enough results, try bing
                    crawler_name = 'bing'
                    if crawler_name not in ignore:
                        safeparams = quote_plus(make_utf8bytes(f"{authorname.replace('. ', ' ')}")[0])
                        bc = BingImageCrawler(storage={'root_dir': icrawlerdir})
                        bc.crawl(keyword=safeparams, max_num=int(max_num - got_images), file_idx_offset='auto')
                        if os.path.exists(icrawlerdir):
                            cnt = len(os.listdir(icrawlerdir))
                            logger.debug(f"{crawler_name} found {cnt - got_images} {plural(cnt - got_images, 'image')}")
                        else:
                            cnt = 0
        if max_num == 1:
            if got_images:
                img = os.path.join(icrawlerdir, os.listdir(icrawlerdir)[0])
                coverlink, success, _ = cache_img(ImageType.AUTHOR, img_id(), img, refresh=refresh)
                if success:
                    logger.debug(f"Cached {crawler_name} image for {authorname}")
                    return coverlink
            else:
                logger.debug(f"No {crawler_name} images found for {authorname}")
            rmtree(icrawlerdir, ignore_errors=True)
        else:
            return icrawlerdir
    elif not PIL:
        logger.debug("PIL not installed, not looking for author image")
    else:
        logger.debug(f"No author found for {authorid}")
    return None


def create_mag_covers(refresh=False):
    logger = logging.getLogger(__name__)
    if not CONFIG.get_bool('IMP_MAGCOVER'):
        logger.info('Cover creation is disabled in config')
        return ''
    db = database.DBConnection()
    cnt = 0
    try:
        #  <> '' ignores empty string or NULL
        issues = db.select("SELECT Title,IssueFile from issues WHERE IssueFile <> ''")
        if refresh:
            logger.info(f"Creating covers for {len(issues)} {plural(len(issues), 'issue')}")
        else:
            logger.info(f"Checking covers for {len(issues)} {plural(len(issues), 'issue')}")
        for item in issues:
            try:
                maginfo = db.match("SELECT CoverPage from magazines WHERE Title=?", (item['Title'],))
                create_mag_cover(item['IssueFile'], refresh=refresh, pagenum=maginfo['CoverPage'])
                cnt += 1
            except Exception as why:
                logger.warning(f"Unable to create cover for {item['IssueFile']}, {type(why).__name__} {str(why)}")
    except Exception as e:
        logger.debug(str(e))

    db.close()
    logger.info("Cover creation completed")
    if refresh:
        return f"Created covers for {cnt} {plural(cnt, 'issue')}"
    return f"Checked covers for {cnt} {plural(cnt, 'issue')}"


def find_gs():
    global GS, GS_VER, generator
    logger = logging.getLogger(__name__)
    if not GS:
        if os.name == 'nt':
            GS = os.path.join(os.getcwd(), "gswin64c.exe")
            generator = "local gswin64c"
            if not path_isfile(GS):
                logger.debug(f"{GS} not found")
                GS = os.path.join(os.getcwd(), "gswin32c.exe")
                generator = "local gswin32c"
            if not path_isfile(GS):
                logger.debug(f"{GS} not found")
                params = ["where", "gswin64c"]
                try:
                    GS = subprocess.check_output(params, stderr=subprocess.STDOUT)
                    GS = make_unicode(GS).strip()
                    generator = "gswin64c"
                except Exception as e:
                    logger.debug(f"where gswin64c failed: {type(e).__name__} {str(e)}")
                    GS = ''
            if not path_isfile(GS):
                params = ["where", "gswin32c"]
                try:
                    GS = subprocess.check_output(params, stderr=subprocess.STDOUT)
                    GS = make_unicode(GS).strip()
                    generator = "gswin32c"
                except Exception as e:
                    logger.debug(f"where gswin32c failed: {type(e).__name__} {str(e)}")
            if not path_isfile(GS):
                logger.debug("No gswin found")
                generator = "(no windows ghostscript found)"
                GS = ''
        else:
            GS = os.path.join(os.getcwd(), "gs")
            generator = "local gs"
            if not path_isfile(GS):
                logger.debug(f"{GS} not found")
                GS = ''
                params = ["which", "gs"]
                try:
                    GS = subprocess.check_output(params, stderr=subprocess.STDOUT)
                    GS = make_unicode(GS).strip()
                    generator = GS
                except subprocess.CalledProcessError as e:
                    if e.returncode == 1:
                        logger.debug("Could not find gs in your system path")
                    else:
                        logger.debug(f"which gs failed: {type(e).__name__} {str(e)}")
            if not path_isfile(GS):
                logger.debug("Cannot find gs")
                generator = "(no gs found)"
                GS = ''
        if GS:
            GS_VER = ''
            # noinspection PyBroadException
            try:
                params = [GS, "--version"]
                res = subprocess.check_output(params, stderr=subprocess.STDOUT)
                res = make_unicode(res).strip()
                logger.debug(f"Found {generator} [{GS}] version {res}")
                generator = f"{generator} version {res}"
                GS_VER = res
            except Exception as e:
                logger.debug(f"gs --version failed: {type(e).__name__} {str(e)}")

    return GS, GS_VER, generator


def shrink_mag(issuefile, dpi=0):
    global GS, GS_VER, generator
    logger = logging.getLogger(__name__)
    if not issuefile or not path_isfile(issuefile):
        logger.warning(f'No issuefile {issuefile}')
        return ''
    if not GS:
        GS, GS_VER, generator = find_gs()
    if GS_VER:
        outfile = f"{issuefile}_{dpi}.pdf"
        params = [GS, "-sDEVICE=pdfwrite", "-dNOPAUSE", "-dBATCH", "-dSAFER",
                  "-dCompatibilityLevel=1.3", "-dPDFSETTINGS=/screen",
                  "-dEmbedAllFonts=true", "-dSubsetFonts=true",
                  "-dAutoRotatePages=/None",
                  "-dColorImageDownsampleType=/Bicubic",
                  f"-dColorImageResolution={dpi}",
                  "-dGrayImageDownsampleType=/Bicubic",
                  f"-dGrayImageResolution={dpi}",
                  "-dMonoImageDownsampleType=/Subsample",
                  f"-dMonoImageResolution={dpi}",
                  "-dUseCropBox", f"-sOutputFile={outfile}", issuefile]
        try:
            logger.debug(str(params))
            res = subprocess.check_output(params, stderr=subprocess.STDOUT)
            res = make_unicode(res).strip()
            if res:
                logger.debug(f"shrink_mag reports: {res}")
            logger.debug(f"Resized file: {outfile}")
            return outfile
        except Exception as e:
            logger.debug(f"Failed to shrink file with {str(params)} [{e}]")
            return ''
    return ''


# noinspection PyUnresolvedReferences
def create_mag_cover(issuefile=None, refresh=False, pagenum=1):
    global GS, GS_VER, generator
    logger = logging.getLogger(__name__)
    if is_valid_type(issuefile, extensions=get_list(CONFIG['MAG_TYPE'])) and (not CONFIG.get_bool('IMP_MAGCOVER')
                                                                              or not pagenum):
        logger.warning(f'No cover required for {issuefile}')
        return ''
    if is_valid_type(issuefile, extensions=get_list(CONFIG['COMIC_TYPE'])) and not CONFIG.get_bool('IMP_COMICCOVER'):
        logger.warning(f'No cover required for {issuefile}')
        return ''
    if not issuefile or not path_isfile(issuefile):
        logger.warning(f'No issuefile {issuefile}')
        return ''

    base, extn = splitext(issuefile)
    if not extn:
        logger.warning(f'Unable to create cover for {issuefile}, no extension?')
        return ''

    coverfile = f"{base}.jpg"

    if path_isfile(coverfile):
        if refresh:
            os.remove(syspath(coverfile))
        else:
            logger.debug(f'Cover for {issuefile} exists')
            return coverfile  # quit if cover already exists and we didn't want to refresh

    logger.debug(f'Creating cover for {issuefile}, page {pagenum}')
    data = ''  # result from unzip or unrar
    extn = extn.lower()
    if extn in ['.cbz', '.epub']:
        try:
            data = zipfile.ZipFile(issuefile)
        except Exception as why:
            logger.error(f"Failed to read zip file {issuefile}, {type(why).__name__} {str(why)}")
            data = ''
    elif extn in ['.cbr'] and lazylibrarian.UNRARLIB:
        try:
            if lazylibrarian.UNRARLIB == 1:
                data = lazylibrarian.RARFILE.RarFile(issuefile)
            elif lazylibrarian.UNRARLIB == 2:
                data = lazylibrarian.RARFILE(issuefile)
        except Exception as why:
            logger.error(f"Failed to read rar file {issuefile}, {type(why).__name__} {str(why)}")
            data = ''
    if data:
        img = None
        fextn = ''
        try:
            for item in ['cover.', '000.', '001.', '00.', '01.']:
                if getattr(data, 'infoiter', None):
                    for member in data.infoiter():
                        fname = member.filename.lower()
                        if item in fname:
                            _, fextn = splitext(fname)
                            if fextn in ['.jpg', '.jpeg', '.png', '.webp']:
                                r = data.read_files(member.filename)
                                img = r[0][1]
                                break
                else:
                    for member in data.namelist():
                        fname = member.lower()
                        if item in fname:
                            _, fextn = splitext(fname)
                            if fextn in ['.jpg', '.jpeg', '.png', '.webp']:
                                img = data.read(member)
                                break
                if img:
                    break
            if img:
                if PIL and fextn in ['.png', '.webp']:
                    image = PILImage.open(io.BytesIO(img))
                    image = image.convert('RGB')
                    image.save(syspath(coverfile), 'jpeg')
                else:
                    with open(syspath(coverfile), 'wb') as f:
                        f.write(img)
                return coverfile
            logger.debug(f"Failed to find image in {issuefile}")
        except Exception as why:
            logger.error(f"Failed to extract image from {issuefile}, {type(why).__name__} {str(why)}")

    elif extn == '.pdf':
        if len(CONFIG['IMP_CONVERT']):  # allow external convert to override libraries
            generator = f"external program: {CONFIG['IMP_CONVERT']}"
            if "gsconvert.py" in CONFIG['IMP_CONVERT']:
                msg = "Use of gsconvert.py is deprecated, equivalent functionality is now built in. "
                msg += "Support for gsconvert.py may be removed in a future release. See wiki for details."
                logger.warning(msg)
            converter = CONFIG['IMP_CONVERT']
            postfix = ''
            # if not path_isfile(converter):  # full path given, or just program_name?
            #     converter = os.path.join(os.getcwd(), lazylibrarian.CONFIG['IMP_CONVERT'])
            if 'convert' in converter and 'gs' not in converter:
                # tell imagemagick to only convert first page
                postfix = '[0]'
            params = []
            try:
                params = [converter, f'{issuefile}{postfix}', f'{coverfile}']
                if os.name != 'nt':
                    res = subprocess.check_output(params, preexec_fn=lambda: os.nice(10),
                                                  stderr=subprocess.STDOUT)
                else:
                    res = subprocess.check_output(params, stderr=subprocess.STDOUT)

                res = make_unicode(res).strip()
                if res:
                    logger.debug(f"{CONFIG['IMP_CONVERT']} reports: {res}")
            except Exception as e:
                if params:
                    logger.debug(params)
                logger.warning(f'External "convert" failed {type(e).__name__} {str(e)}')

        elif os.name == 'nt':
            if not GS:
                GS, GS_VER, generator = find_gs()
            if GS_VER:
                issuefile = issuefile.split('[')[0]
                # could use "-sDEVICE=png16m" here instead?
                params = [GS, "-sDEVICE=jpeg", "-dJPEGQ=100", "-dNOPAUSE", "-dBATCH", "-dSAFER",
                          f"-dFirstPage={check_int(pagenum, 1):d}",
                          f"-dLastPage={check_int(pagenum, 1):d}",
                          "-dUseCropBox", f"-sOutputFile={coverfile}", issuefile]
                try:
                    res = subprocess.check_output(params, stderr=subprocess.STDOUT)
                    res = make_unicode(res).strip()
                    if not path_isfile(coverfile):
                        logger.debug(f"Failed to create jpg: {res}")
                except Exception as e:
                    logger.debug(f"Failed to create cover with {str(params)} [{e}]")
            else:
                logger.warning(f"Failed to create jpg for {issuefile}")
        else:  # not windows
            try:
                # noinspection PyUnresolvedReferences,PyPep8Naming
                from wand.image import Image as wand_Image
                interface = "wand"
            except ImportError:
                wand_Image = None
                interface = ""
            try:
                if interface == 'wand' and wand_Image:
                    generator = "wand interface"
                    # noinspection PyCallingNonCallable
                    with wand_Image(filename=f"{issuefile}[{str(check_int(pagenum, 1) - 1)}]") as img:
                        img.save(filename=coverfile)
                else:
                    if not GS:
                        GS, GS_VER, generator = find_gs()
                    if GS_VER:
                        issuefile = issuefile.split('[')[0]
                        params = [GS, "-sDEVICE=jpeg", "-dJPEGQ=100", "-dNOPAUSE", "-dBATCH", "-dSAFER",
                                  f"-dFirstPage={check_int(pagenum, 1):d}",
                                  f"-dLastPage={check_int(pagenum, 1):d}",
                                  "-dUseCropBox", f"-sOutputFile={coverfile}", issuefile]
                        try:
                            res = subprocess.check_output(params, preexec_fn=lambda: os.nice(10),
                                                          stderr=subprocess.STDOUT)
                            res = make_unicode(res).strip()
                            if not path_isfile(coverfile):
                                logger.debug(f"Failed to create jpg: {res}")
                        except Exception as e:
                            logger.debug(f"Failed to create cover with {str(params)} [{e}]")
                    else:
                        logger.warning(f"Failed to create jpg for {issuefile}")
            except Exception as e:
                logger.warning(f"Unable to create cover for {issuefile} using {type(e).__name__} {generator}")
                logger.debug(f"Exception in create_cover: {traceback.format_exc()}")

        if path_isfile(coverfile):
            setperm(coverfile)
            logger.debug(f"Created cover (page {check_int(pagenum, 1):d}) for {issuefile} using {generator}")
            return coverfile

    # if not recognised extension or cover creation failed
    try:
        coverfile = safe_copy(os.path.join(DIRS.PROG_DIR, 'data', 'images', 'nocover.jpg'), coverfile)
        setperm(coverfile)
        return coverfile
    except Exception as why:
        logger.error(f"Failed to copy nocover file, {type(why).__name__} {str(why)}")
    return ''


def read_pdf_tags(srcfile):
    logger = logging.getLogger(__name__)
    if not path_isfile(srcfile):
        logger.error(f"Unable to read source file {srcfile}")
        return {}
    src_pdf = PdfReader(srcfile)
    if not src_pdf:
        logger.error(f"Unable to read source file {src_pdf}")
        return {}
    try:
        return dict(src_pdf.metadata)
    except TypeError:
        # no metadata found in source file
        return {}


def write_pdf_tags(srcfile, title, issue, tags=None):
    logger = logging.getLogger(__name__)
    if not title or not issue:
        logger.error('Unable to tag, need title and issue')
        return {}
    if not path_isfile(srcfile):
        logger.error(f"Unable to read source file {srcfile}")
        return {}

    try:
        src_pdf = PdfReader(srcfile)
    except Exception as e:
        logger.error(f"{srcfile}:{e}")
        return {}

    if not src_pdf:
        logger.error(f"Unable to read source file {srcfile}")
        return {}

    try:
        dst_pdf = PdfWriter(clone_from=src_pdf)
    except Exception as e:
        logger.error(f"{srcfile}:{e}")
        return {}
    try:
        metadata = dict(src_pdf.metadata)
    except Exception as e:
        logger.debug(f"No metadata in {srcfile}:{e}")
        metadata = {}

    magname = title
    magissue = issue
    sorted_title = sort_definite(title, articles=get_list(CONFIG.get_csv('NAME_DEFINITE')))
    if CONFIG.get_bool('IMP_CALIBRE_MAGISSUE'):
        iname = issue
        sorted_iname = iname
        series = f"{title} #{issue}"
    elif issue and len(issue) == 10 and issue[8:] == '01' and issue[4] == '-' and issue[7] == '-':
        yr = issue[0:4]
        mn = issue[5:7]
        lang = 0
        cnt = 0
        while cnt < len(lazylibrarian.MONTHNAMES[0][0]):
            if lazylibrarian.MONTHNAMES[0][0][cnt] == CONFIG['DATE_LANG']:
                lang = cnt
                break
            cnt += 1
        # monthnames for this month, eg ["January", "Jan", "enero", "ene"]
        monthname = lazylibrarian.MONTHNAMES[0][int(mn)]
        month = monthname[lang]  # lang = full name, lang+1 = short name

        iname = f"{title} - {month} {yr}"  # The Magpi - January 2017
        sorted_iname = f"{sorted_title} - {month} {yr}"  # Magpi, The - January 2017
        series = f"{title} #{yr}.{mn}"
    elif title in issue:
        iname = issue  # 0063 - Android Magazine -> 0063
        sorted_iname = iname
        series = f"{title} #{issue}"
    else:
        iname = f"{title} - {issue}"  # Android Magazine - 0063
        sorted_iname = f"{sorted_title} - {issue}"  # Android Magazine - 0063
        series = f"{title} #{issue}"

    db = database.DBConnection()
    match = db.match('SELECT Language from magazines WHERE title=? COLLATE NOCASE', (title,))
    language = ''
    if match:
        language = match['Language']
    db.close()

    metadata['/LL_Issue'] = magissue
    metadata['/LL_Title'] = magname
    # Write original filename into file so we can recover it if needed
    if not metadata.get('/LL_Filename'):
        metadata['/LL_Filename'] = os.path.basename(srcfile)
    metadata['/Title'] = iname
    metadata['/Title sort'] = sorted_iname
    metadata['/Author'] = title
    metadata['/Series'] = series
    if language:
        metadata['/Languages'] = language

    if tags:  # were we passed any customised tags
        for item in tags:
            # overwrite generated ones with customised, add extras
            metadata[item] = tags[item]

    newmetadata = {}
    for item in metadata:  # Remove any empty tags
        if metadata[item]:
            newmetadata[item] = metadata[item]

    try:
        dst_pdf.metadata = newmetadata
        dst_pdf.write(srcfile + '.tag')
        safe_move(srcfile + '.tag', srcfile)
    except Exception as e:
        logger.error(f"Unable to write tags: {e}")
        logger.error(f"{newmetadata}")
        newmetadata = {}
    return newmetadata
