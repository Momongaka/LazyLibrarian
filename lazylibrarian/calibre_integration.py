#  This file is part of Lazylibrarian.
#
#  Lazylibrarian is free software, you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

"""
Calibre Integration Module

This module handles sending ebooks, audiobooks, magazines, and comics to a Calibre library.
Provides functions to import items into Calibre and update their metadata using calibredb.

Key Functions:
- send_ebook_to_calibre: Send ebook to Calibre library
- send_mag_issue_to_calibre: Send magazine issue to Calibre
- send_comic_issue_to_calibre: Send comic issue to Calibre
- send_to_calibre: Generic function to send any media type to Calibre
"""

import json
import logging
import os
import shutil
import tempfile
import traceback

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.cache import ImageType
from lazylibrarian.calibre import calibredb, get_calibre_id
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import (
    book_file,
    get_directory,
    listdir,
    path_isdir,
    path_isfile,
    remove_file,
    safe_copy,
    safe_move,
    setperm,
    syspath,
)
from lazylibrarian.formatter import make_unicode, unaccented
from lazylibrarian.images import create_mag_cover
from lazylibrarian.magazinescan import create_id, format_issue_filename, get_dateparts
from lazylibrarian.metadata_opf import create_comic_opf, create_mag_opf, create_opf


def send_to_calibre(booktype, global_name, folder, data):
    """
    booktype = ebook audiobook magazine comic
    global_name = standardised filename used for item/opf/jpg
    folder = folder containing the file(s)
    data = various data for the item, varies according to booktype

    return True,filename,folder (Filename empty if already exists)
    on fail return False,message,folder
    """
    issueid = data.get("IssueDate", "")  # comic issueid
    authorname = data.get("AuthorName", "")
    bookname = data.get("BookName", "")
    bookid = data.get("BookID", "")  # ebook/audiobook/comic
    title = data.get("Title", "")
    issuedate = data.get("IssueDate", "")  # magazine issueid
    coverpage = data.get("cover", "")
    bestformat = data.get("bestformat", "")
    mag_genres = data.get("mag_genres")

    logger = logging.getLogger(__name__)
    try:
        if not os.path.isdir(folder):
            return (
                False,
                f"calibredb import failed, Invalid folder name [{folder}]",
                folder,
            )
        logger.debug(f"Importing {booktype} {global_name} into calibre library")
        # calibre may ignore metadata.opf and book_name.opf depending on calibre settings,
        # and ignores opf data if there is data embedded in the book file,
        # so we send separate "set_metadata" commands after the import
        for fname in listdir(folder):
            extn = os.path.splitext(fname)[1]
            srcfile = os.path.join(folder, fname)
            if CONFIG.is_valid_booktype(fname, booktype=booktype) or extn in [
                ".opf",
                ".jpg",
            ]:
                if (
                    bestformat
                    and not fname.endswith(bestformat)
                    and extn not in [".opf", ".jpg"]
                ):
                    logger.debug(f"Removing {fname} as not {bestformat}")
                    remove_file(srcfile)
                else:
                    dstfile = os.path.join(folder, global_name.replace('"', "_") + extn)
                    # calibre does not like quotes in author names
                    try:
                        _ = safe_move(srcfile, dstfile)
                    except Exception as e:
                        logger.warning(f"Failed to move file: {str(e)}")
                        return False, str(e), folder
            else:
                logger.debug(f"Removing {fname} as not wanted")
                if path_isfile(srcfile):
                    remove_file(srcfile)
                elif path_isdir(srcfile):
                    logger.debug(f"Removing directory {srcfile}")
                    shutil.rmtree(srcfile)

        identifier = ""
        if booktype in ["ebook", "audiobook"]:
            if bookid.startswith("OL"):
                identifier = f"OpenLibrary:{bookid}"
            elif data.get("hc_id") == bookid:
                identifier = f"hardcover:{bookid}"
            elif data.get("gr_id") == bookid:
                identifier = f"goodreads:{bookid}"
            elif data.get("gb_id") == bookid:
                identifier = f"google:{bookid}"
            elif data.get("dnb_id") == bookid:
                identifier = f"dnb:{bookid}"
        elif booktype == "comic":
            if bookid.startswith("CV"):
                identifier = f"ComicVine:{bookid[2:]}"
            else:  # bookid.startswith('CX'):
                identifier = f"Comixology:{bookid[2:]}"

        if booktype == "magazine":
            issueid = create_id(f"{title} {issuedate}")
            identifier = f"lazylibrarian:{issueid}"
            magfile = book_file(folder, "magazine", config=CONFIG)
            coverfile = os.path.join(folder, "cover.jpg")
            # calibre likes "cover.jpg"
            jpgfile = f"{os.path.splitext(magfile)[0]}.jpg"
            if path_isfile(jpgfile):
                try:
                    jpgfile = safe_copy(jpgfile, coverfile)
                except Exception as e:
                    logger.warning(f"Failed to copy jpeg file: {str(e)}")
                    return False, str(e), folder
            elif magfile:
                if not coverpage:
                    coverpage = 1  # if not set, default to page 1
                jpgfile = create_mag_cover(magfile, pagenum=coverpage, refresh=True)
                if jpgfile:
                    try:
                        jpgfile = safe_copy(jpgfile, coverfile)
                    except Exception as e:
                        logger.warning(f"Failed to copy jpeg file: {str(e)}")
                        return False, str(e), folder

            if CONFIG.get_bool("IMP_CALIBRE_MAGTITLE"):
                authors = title
                global_name = issuedate
            else:
                authors = "magazines"
                global_name = format_issue_filename(
                    CONFIG["MAG_DEST_FILE"], title, get_dateparts(issuedate)
                )

            tags = "Magazine"
            if CONFIG.get_bool("TEST_TAGS") and mag_genres:
                tags = f"{tags}, {mag_genres}"

            params = [
                magfile,
                "--duplicates",
                "--authors",
                authors,
                "--series",
                title,
                "--title",
                global_name,
                "--tags",
                tags,
            ]
            if jpgfile:
                image = ["--cover", jpgfile]
                params.extend(image)
            res, err, rc = calibredb("add", params)
        else:
            if CONFIG.get_bool("IMP_CALIBREOVERWRITE"):
                res, err, rc = calibredb(
                    "add", ["-1", "--automerge", "overwrite"], [folder]
                )
            else:
                res, err, rc = calibredb("add", ["-1"], [folder])

        if rc:
            return False, f"calibredb rc {rc} from {CONFIG['IMP_CALIBREDB']}", folder
        if booktype == "ebook" and (" --duplicates" in res or " --duplicates" in err):
            logger.warning(
                f'Calibre failed to import {authorname} {bookname}, already exists, marking book as "Have"'
            )
            db = database.DBConnection()
            try:
                control_value_dict = {"BookID": bookid}
                new_value_dict = {"Status": "Have"}
                db.upsert("books", new_value_dict, control_value_dict)
            finally:
                db.close()
            return True, "", folder
        # Answer should look like "Added book ids : bookID" (string may be translated!)
        try:
            calibre_id = res.rsplit(": ", 1)[1].split("\n", 1)[0].split(",")[0].strip()
        except IndexError:
            return (
                False,
                f"Calibre failed to import {authorname} {bookname}, no added bookids",
                folder,
            )

        if calibre_id.isdigit():
            logger.debug(f"Calibre ID: [{calibre_id}]")
        else:
            logger.warning(f"Calibre ID looks invalid: [{calibre_id}]")

        our_opf = False
        rc = 0
        if (booktype == "magazine" and not CONFIG.get_bool("IMP_AUTOADD_MAGONLY")) or (
            booktype != "magazine" and not CONFIG.get_bool("IMP_AUTOADD_BOOKONLY")
        ):
            # we can pass an opf with all the info, and a cover image
            db = database.DBConnection()
            if booktype in ["ebook", "audiobook"]:
                cmd = (
                    "SELECT AuthorName,BookID,BookName,BookDesc,BookIsbn,BookImg,BookDate,BookLang,"
                    "BookPub,BookRate,Requester,AudioRequester,BookGenre,Narrator from books,authors "
                    "WHERE BookID=? and books.AuthorID = authors.AuthorID"
                )
                data = db.match(cmd, (bookid,))
            elif booktype == "comic":
                cmd = (
                    "SELECT Title,comicissues.ComicID,IssueID,IssueAcquired,IssueFile,comicissues.Cover,"
                    "Publisher,Contributors from comics,comicissues WHERE "
                    "comics.ComicID = comicissues.ComicID and IssueID=? and comicissues.ComicID=?"
                )
                data = db.match(cmd, (issueid, bookid))
                bookid = f"{bookid}_{issueid}"
            else:
                data = db.match(
                    "SELECT Language,Genre from magazines WHERE Title=? COLLATE NOCASE",
                    (title,),
                )
            db.close()

            if not data:
                logger.error(f"No data found for bookid {bookid}")
            else:
                opfpath = ""
                if booktype in ["ebook", "audiobook"]:
                    lazylibrarian.postprocess.process_img(
                        folder, bookid, data["BookImg"], global_name, ImageType.BOOK
                    )
                    opfpath, our_opf = create_opf(folder, data, global_name, True)
                    # if we send an opf, does calibre update the book-meta as well?
                elif booktype == "comic":
                    if data.get("Cover"):
                        lazylibrarian.postprocess.process_img(
                            folder, bookid, data["Cover"], global_name, ImageType.COMIC
                        )
                    if not CONFIG.get_bool("IMP_COMICOPF"):
                        logger.debug("create_comic_opf is disabled")
                    else:
                        opfpath, our_opf = create_comic_opf(
                            folder, data, global_name, True
                        )
                else:
                    if not CONFIG.get_bool("IMP_MAGOPF"):
                        logger.debug("create_mag_opf is disabled")
                    else:
                        opfpath, our_opf = create_mag_opf(
                            folder,
                            title,
                            issuedate,
                            issueid,
                            language=data["Language"],
                            genres=mag_genres,
                            overwrite=True,
                        )
                # calibre likes "metadata.opf"
                opffile = os.path.basename(opfpath)
                if opffile != "metadata.opf":
                    try:
                        opfpath = safe_copy(
                            opfpath, opfpath.replace(opffile, "metadata.opf")
                        )
                    except Exception as e:
                        logger.warning(f"Failed to copy opf file: {str(e)}")
                        opfpath = ""
                if opfpath:
                    _, _, rc = calibredb("set_metadata", None, [calibre_id, opfpath])
                    if rc:
                        logger.warning("calibredb unable to set opf")

                tags = ""
                if CONFIG.get_bool("OPF_TAGS"):
                    if booktype == "magazine":
                        tags = "Magazine"
                        if CONFIG.get_bool("TEST_TAGS") and mag_genres:
                            tags = f"{tags}, {mag_genres}"
                    if booktype == "ebook":
                        if CONFIG.get_bool("GENRE_TAGS") and data["BookGenre"]:
                            tags = data["BookGenre"]
                        if CONFIG.get_bool("WISHLIST_TAGS"):
                            if data["Requester"] is not None:
                                tag = data["Requester"].replace(" ", ",")
                                if tag not in tags:
                                    if tags:
                                        tags += ", "
                                    tags += tag
                            elif data["AudioRequester"] is not None:
                                tag = data["AudioRequester"].replace(" ", ",")
                                if tag not in tags:
                                    if tags:
                                        tags += ", "
                                    tags += tag
                if tags:
                    _, _, rc = calibredb(
                        "set_metadata", ["--field", f"tags:{tags}"], [calibre_id]
                    )
                    if rc:
                        logger.warning("calibredb unable to set tags")

        if (
            not our_opf and not rc
        ):  # pre-existing opf might not have our preferred authorname/title/identifier
            if booktype == "magazine":
                if CONFIG.get_bool("IMP_CALIBRE_MAGTITLE"):
                    authorname = title
                    global_name = issuedate
                else:
                    authorname = "magazines"
                    global_name = format_issue_filename(
                        CONFIG["MAG_DEST_FILE"], title, get_dateparts(issuedate)
                    )
                _, _, rc = calibredb(
                    "set_metadata", ["--field", f"pubdate:{issuedate}"], [calibre_id]
                )
                if rc:
                    logger.warning("calibredb unable to set pubdate")
            _, _, rc = calibredb(
                "set_metadata",
                ["--field", f"authors:{unaccented(authorname, only_ascii=False)}"],
                [calibre_id],
            )
            if rc:
                logger.warning("calibredb unable to set author")
            _, _, rc = calibredb(
                "set_metadata",
                ["--field", f"title:{unaccented(global_name, only_ascii=False)}"],
                [calibre_id],
            )
            if rc:
                logger.warning("calibredb unable to set title")

            _, _, rc = calibredb(
                "set_metadata", ["--field", f"identifiers:{identifier}"], [calibre_id]
            )
            if rc:
                logger.warning("calibredb unable to set identifier")

        if (
            booktype == "comic"
        ):  # for now assume calibredb worked, and didn't move the file
            return True, data["IssueFile"], folder

        # Ask calibre for the author/title, so we can construct the likely location
        target_dir = ""
        calibre_authorname = ""
        dest_dir = get_directory("eBook")
        res, err, rc = calibredb(
            "list",
            ["--fields", "title,authors", "--search", f"id:{calibre_id}"],
            ["--for-machine"],
        )
        if not rc:
            try:
                res = f"{{ {res.split('{')[1].split('}')[0]} }}"
                res = json.loads(res)
                if booktype == "magazine":
                    dest_dir = CONFIG["MAG_DEST_FOLDER"]
                    if CONFIG.get_bool("MAG_RELATIVE"):
                        dest_dir = os.path.join(get_directory("eBook"), dest_dir)
                elif booktype == "comic":
                    dest_dir = CONFIG["COMIC_DEST_FOLDER"]
                    if CONFIG.get_bool("COMIC_RELATIVE"):
                        dest_dir = os.path.join(get_directory("eBook"), dest_dir)

                while "$" in dest_dir:
                    dest_dir = os.path.dirname(dest_dir)

                logger.debug(
                    f"[{dest_dir}][{res['authors']}][{res['title']}][{res['id']}]"
                )
                target_dir = os.path.join(
                    dest_dir, res["authors"], f"{res['title']} ({res['id']})"
                )
                logger.debug(f"Calibre target: {target_dir}")
                calibre_authorname = res["authors"]
                calibre_id = res["id"]
            except Exception as e:
                logger.debug(f"Unable to read json response; {str(e)}")
                target_dir = ""

            if not target_dir or not path_isdir(target_dir) and calibre_authorname:
                author_dir = os.path.join(dest_dir, calibre_authorname)
                if path_isdir(author_dir):  # assumed author directory
                    our_id = f"({calibre_id})"
                    entries = listdir(author_dir)
                    for entry in entries:
                        if entry.endswith(our_id):
                            target_dir = os.path.join(author_dir, entry)
                            break

                    if not target_dir or not path_isdir(target_dir):
                        logger.debug(
                            f"Failed to locate calibre folder with id {our_id} in {author_dir}"
                        )
                else:
                    logger.debug(f"Failed to locate calibre author folder {author_dir}")

        if not target_dir or not path_isdir(target_dir):
            # calibre does not like accents or quotes in names
            if authorname.endswith(
                "."
            ):  # calibre replaces trailing dot with underscore e.g. Jr. becomes Jr_
                authorname = f"{authorname[:-1]}_"
            author_dir = os.path.join(
                dest_dir, unaccented(authorname.replace('"', "_"), only_ascii=False), ""
            )
            if path_isdir(author_dir):  # assumed author directory
                our_id = f"({calibre_id})"
                entries = listdir(author_dir)
                for entry in entries:
                    if entry.endswith(our_id):
                        target_dir = os.path.join(author_dir, entry)
                        break

                if not target_dir or not path_isdir(target_dir):
                    return (
                        False,
                        f"Failed to locate folder with calibre_id {our_id} in {author_dir}",
                        folder,
                    )
            else:
                return False, f"Failed to locate author folder {author_dir}", folder

        if booktype == "ebook":
            remv = CONFIG.get_bool("FULL_SCAN")
            logger.debug(f"Scanning directory [{target_dir}]")
            _ = lazylibrarian.librarysync.library_scan(target_dir, remove=remv)

        newbookfile = book_file(target_dir, booktype=booktype, config=CONFIG)
        # should we be setting permissions on calibres directories and files?
        if newbookfile:
            setperm(target_dir)
            if booktype in ["magazine", "comic"]:
                try:
                    ignorefile = os.path.join(target_dir, ".ll_ignore")
                    with open(syspath(ignorefile), "w", encoding="utf-8") as f:
                        f.write(make_unicode(booktype))
                except OSError as e:
                    logger.warning(f"Unable to create/write to ignorefile: {str(e)}")

            for fname in listdir(target_dir):
                setperm(os.path.join(target_dir, fname))

            # clear up any residual non-calibre folder
            shutil.rmtree(target_dir.rsplit("(", 1)[0].strip(), ignore_errors=True)
            return True, newbookfile, folder
        return False, f"Failed to find a valid {booktype} in [{target_dir}]", folder
    except Exception as e:
        logger.error(
            f"Unhandled exception importing to calibre: {traceback.format_exc()}"
        )
        return False, f"calibredb import failed, {type(e).__name__} {str(e)}", folder


# noinspection PyBroadException

def send_mag_issue_to_calibre(data):
    logger = logging.getLogger(__name__)
    calibre_id = get_calibre_id(data, try_filename=False)
    logger.debug(f"Calibre ID {calibre_id}")
    if calibre_id:
        logger.debug(
            f"Calibre ID {calibre_id} exists: {data['Title']} {data['IssueDate']}"
        )
        filename = os.path.basename(data["IssueFile"])
        pp_path = os.path.dirname(data["IssueFile"])
        return "Exists", filename, pp_path

    logger.debug(
        f"Calibre ID does not exist: {data['Title']}:{data['IssueDate']}:{data['IssueFile']}"
    )
    global_name = os.path.splitext(os.path.basename(data["IssueFile"]))[0]
    logger.debug(f" Global name = [{global_name}]")
    sourcedir = os.path.dirname(data["IssueFile"])
    logger.debug(f" Source Dir = [{sourcedir}]")
    with tempfile.TemporaryDirectory() as temp_dir:
        for item in listdir(sourcedir):
            if item.startswith(global_name):
                logger.debug(f"Copy file [{item}]")
                shutil.copyfile(
                    os.path.join(sourcedir, item), os.path.join(temp_dir, item)
                )
        return send_to_calibre("magazine", global_name, temp_dir, data)


def send_comic_issue_to_calibre(data):
    logger = logging.getLogger(__name__)
    calibre_id = get_calibre_id(data, try_filename=False)
    if calibre_id:
        logger.debug(
            f"Calibre ID {calibre_id} exists: {data['ComicID']} {data['IssueID']}"
        )
        filename = os.path.basename(data["IssueFile"])
        pp_path = os.path.dirname(data["IssueFile"])
        return "Exists", filename, pp_path

    logger.debug(f"Calibre ID does not exist: {data['ComicID']}:{data['IssueID']}")
    global_name = os.path.splitext(os.path.basename(data["IssueFile"]))[0]
    sourcedir = os.path.dirname(data["IssueFile"])
    with tempfile.TemporaryDirectory() as temp_dir:
        for item in listdir(sourcedir):
            if item.startswith(global_name):
                logger.debug(f"Copy file [{item}]")
                shutil.copyfile(
                    os.path.join(sourcedir, item), os.path.join(temp_dir, item)
                )
        return send_to_calibre("comic", global_name, temp_dir, data)


def send_ebook_to_calibre(data):
    logger = logging.getLogger(__name__)
    calibre_id = get_calibre_id(data, try_filename=False)
    if calibre_id:
        logger.debug(
            f"Calibre ID {calibre_id} exists: {data['AuthorName']} {data['BookName']}"
        )
        filename = os.path.basename(data["BookFile"])
        pp_path = os.path.dirname(data["BookFile"])
        return "Exists", filename, pp_path

    logger.debug(f"Calibre ID does not exist: {data['AuthorName']} {data['BookName']}")
    global_name = os.path.splitext(os.path.basename(data["BookFile"]))[0]
    with tempfile.TemporaryDirectory() as temp_dir:
        sourcedir = os.path.dirname(data["IssueFile"])
        for item in listdir(sourcedir):
            if item.startswith(global_name):
                logger.debug(f"Copy file [{item}]")
                shutil.copyfile(
                    os.path.join(sourcedir, item), os.path.join(temp_dir, item)
                )
        return send_to_calibre("ebook", global_name, temp_dir, data)
