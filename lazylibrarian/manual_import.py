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
Manual Import Module

Handles user-initiated imports of books, audiobooks, and magazines from
alternate directories or specific files/folders. These are manual operations
triggered by the user through the web interface or API, NOT automatic download
postprocessing.

Functions:
- process_mag_from_file: Import a single magazine issue from a file
- process_book_from_dir: Import a single book/audiobook from a directory
- process_issues: Scan alternate directory for magazine issues matching a title
- process_alternate: Scan alternate directory for books/audiobooks

Used by:
- Web interface manual import actions
- API manual import endpoints
- Library synchronization operations
"""

import logging
import os
import shutil
import tempfile
import traceback
import uuid

import lazylibrarian
from lazylibrarian import database, searchmag
from lazylibrarian.archive_utils import unpack_archive as _unpack_archive
from lazylibrarian.bookrename import id3read, stripspaces
from lazylibrarian.common import multibook, remove_file, setperm
from lazylibrarian.config2 import CONFIG, DIRS
from lazylibrarian.filesystem import (
    book_file,
    get_directory,
    listdir,
    make_dirs,
    opf_file,
    path_isdir,
    path_isfile,
    remove_dir,
    safe_copy,
)
from lazylibrarian.formatter import (
    check_int,
    get_list,
    make_utf8bytes,
    replace_all,
    restore_thread_name,
    sanitize,
    split_title,
    today,
    unaccented,
)
from lazylibrarian.gr import GoodReads
from lazylibrarian.hc import HardCover
from lazylibrarian.images import create_mag_cover, createthumbs
from lazylibrarian.importer import (
    add_author_name_to_db,
    add_author_to_db,
    import_book,
    search_for,
    update_totals,
)
from lazylibrarian.librarysync import find_book_in_db, get_book_info, get_book_meta
from lazylibrarian.magazinescan import create_id, format_issue_filename, get_dateparts
from lazylibrarian.metadata_opf import create_mag_opf
from lazylibrarian.ol import OpenLibrary
from lazylibrarian.postprocess import (
    _process_auto_add as process_auto_add,
)
from lazylibrarian.postprocess import (
    _process_destination as process_destination,
)

# Import from postprocess for core processing function
from lazylibrarian.postprocess import (
    _process_ll_bookid_folders_from_list,
    is_valid_type,
    process_book,
)
from lazylibrarian.postprocess_metadata import BookType
from lazylibrarian.telemetry import TELEMETRY


def process_mag_from_file(source_file, title, issuenum):
    # import a magazine issue by title/num
    # Assumes the source file is the correct file for the issue and renames it to match
    # Adds the magazine id to the database if not already there
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        if not source_file or not path_isfile(source_file):
            logger.warning(f"{source_file} is not a file")
            return False
        _, extn = os.path.splitext(source_file)
        extn = extn.lstrip(".")
        if not extn or extn not in get_list(CONFIG["MAG_TYPE"]):
            logger.warning(f"{source_file} is not a valid issue file")
            return False
        title = unaccented(sanitize(title), only_ascii=False)
        if not title:
            logger.warning(f"No title for {source_file}, rejecting")
            return False

        TELEMETRY.record_usage_data("Process/Magazine/FromFile")
        entry = db.match("SELECT * FROM magazines where Title=?", (title,))
        if not entry:
            logger.debug(f"Magazine title [{title}] not found, adding it")
            control_value_dict = {"Title": title}
            new_value_dict = {
                "LastAcquired": today(),
                "IssueStatus": CONFIG["FOUND_STATUS"],
                "IssueDate": "",
                "LatestCover": "",
            }
            db.upsert("magazines", new_value_dict, control_value_dict)
        # rename issuefile to match pattern
        # update magazine lastissue/cover as required
        entry = db.match("SELECT * FROM magazines where Title=?", (title,))
        mostrecentissue = entry["IssueDate"]
        dateparts = get_dateparts(issuenum)
        dest_path = format_issue_filename(CONFIG["MAG_DEST_FOLDER"], title, dateparts)

        if CONFIG.get_bool("MAG_RELATIVE"):
            dest_dir = str(get_directory("eBook"))
            dest_path = stripspaces(os.path.join(dest_dir, dest_path))
            dest_path = make_utf8bytes(dest_path)[0]
        else:
            dest_path = make_utf8bytes(dest_path)[0]

        if not dest_path or not make_dirs(dest_path):
            logger.error(f"Unable to create destination directory {dest_path}")
            return False
        global_name = format_issue_filename(CONFIG["MAG_DEST_FILE"], title, dateparts)
        tempdir = tempfile.mkdtemp()
        try:
            _ = safe_copy(source_file, os.path.join(tempdir, f"{global_name}.{extn}"))
        except Exception as e:
            logger.warning(f"Failed to copy source file: {str(e)}")
            return False
        data = {"IssueDate": issuenum, "Title": title}
        success, dest_file, _ = process_destination(
            tempdir, dest_path, global_name, data, "magazine"
        )
        shutil.rmtree(tempdir, ignore_errors=True)
        if not success:
            logger.error(f"Unable to import {source_file}: {dest_file}")
            return False

        old_folder = os.path.dirname(source_file)
        basename, extn = os.path.splitext(source_file)
        remove_file(source_file)
        remove_file(f"{basename}.opf")
        remove_file(f"{basename}.jpg")
        if len(listdir(old_folder)) == 0:
            remove_dir(old_folder)

        if mostrecentissue:
            if mostrecentissue.isdigit() and str(issuenum).isdigit():
                older = int(mostrecentissue) > int(issuenum)  # issuenumber
            else:
                older = mostrecentissue > issuenum  # YYYY-MM-DD
        else:
            older = False

        maginfo = db.match("SELECT CoverPage from magazines WHERE Title=?", (title,))
        # create a thumbnail cover for the new issue
        coverfile = create_mag_cover(
            dest_file, pagenum=check_int(maginfo["CoverPage"], 1), refresh=True
        )
        if coverfile:
            myhash = uuid.uuid4().hex
            hashname = os.path.join(DIRS.CACHEDIR, "magazine", f"{myhash}.jpg")
            shutil.copyfile(coverfile, hashname)
            setperm(hashname)
            coverfile = f"cache/magazine/{myhash}.jpg"
            createthumbs(hashname)

        issueid = create_id(f"{title} {issuenum}")
        control_value_dict = {"Title": title, "IssueDate": issuenum}
        new_value_dict = {
            "IssueAcquired": today(),
            "IssueFile": dest_file,
            "IssueID": issueid,
            "Cover": coverfile,
        }
        db.upsert("issues", new_value_dict, control_value_dict)

        control_value_dict = {"Title": title}
        if older:  # check this in case processing issues arriving out of order
            new_value_dict = {
                "LastAcquired": today(),
                "IssueStatus": CONFIG["FOUND_STATUS"],
            }
        else:
            new_value_dict = {
                "LastAcquired": today(),
                "IssueStatus": CONFIG["FOUND_STATUS"],
                "IssueDate": issuenum,
                "LatestCover": coverfile,
            }
        db.upsert("magazines", new_value_dict, control_value_dict)

        if not CONFIG.get_bool("IMP_MAGOPF"):
            logger.debug("create_mag_opf is disabled")
        else:
            basename, _ = os.path.splitext(source_file)
            opffile = f"{basename}.opf"
            remove_file(opffile)
            _ = create_mag_opf(
                dest_file,
                title,
                issuenum,
                issueid,
                language=entry["Language"],
                genres=entry["Genre"],
                overwrite=True,
            )
        if CONFIG["IMP_AUTOADDMAG"]:
            dest_path = os.path.dirname(dest_file)
            process_auto_add(dest_path, BookType.MAGAZINE)
        return True

    except Exception:
        logger.error(f"Unhandled exception in import_mag: {traceback.format_exc()}")
        return False
    finally:
        db.close()


def process_book_from_dir(source_dir=None, library="eBook", bookid=None):
    # import a book by id from a directory
    # Assumes the book is the correct file for the id and renames it to match
    # Adds the id to the database if not already there
    logger = logging.getLogger(__name__)
    if not source_dir or not path_isdir(source_dir):
        logger.warning(f"{source_dir} is not a directory")
        return False
    if source_dir.startswith(get_directory(library)):
        logger.warning("Source directory must not be the same as or inside library")
        return False

    TELEMETRY.record_usage_data("Process/Book/FromDir")
    reject = multibook(source_dir)
    if reject:
        logger.debug(f"Not processing {source_dir}, found multiple {reject}")
        return False

    if library not in ["eBook", "Audio"]:
        logger.error(f"book_from_dir not implemented for {library}")
        return False

    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        logger.debug(f"Processing {library} directory {source_dir}")
        book = db.match("SELECT * from books where BookID=?", (bookid,))
        if not book:
            logger.warning(f"Bookid [{bookid}] not found in database, trying to add...")
            this_source = lazylibrarian.INFOSOURCES[CONFIG["BOOK_API"]]
            api = this_source["api"]()
            _ = api.add_bookid_to_db(bookid, None, None, f"Added by book_from_dir {source_dir}")
            # see if it's there now...
            book = db.match("SELECT * from books where BookID=?", (bookid,))
        db.close()
        if not book:
            logger.debug(f"Unable to add bookid {bookid} to database")
            return False
        return process_book(source_dir, bookid, library=library)
    except Exception:
        logger.error(f"Unhandled exception in book_from_dir: {traceback.format_exc()}")
        db.close()
        return False


@restore_thread_name('IMPORTISSUES')
def process_issues(source_dir=None, title=""):
    # import magazine issues for a given title from an alternate directory
    # noinspection PyBroadException
    logger = logging.getLogger(__name__)
    matchinglogger = logging.getLogger("special.matching")
    # noinspection PyBroadException
    try:
        if not source_dir:
            logger.warning("Alternate Directory not configured")
            return False
        if not path_isdir(source_dir):
            logger.warning(f"{source_dir} is not a directory")
            return False

        TELEMETRY.record_usage_data("Process/Issues")
        logger.debug(f"Looking for {title} issues in {source_dir}")
        # first, recursively process any items in subdirectories
        flist = str(listdir(source_dir))
        for fname in flist:
            subdir = os.path.join(source_dir, fname)
            if path_isdir(subdir):
                process_issues(subdir, title)

        dic = {
            ".": " ",
            "-": " ",
            "/": " ",
            "+": " ",
            "_": " ",
            "(": "",
            ")": "",
            "[": " ",
            "]": " ",
            "#": "# ",
        }
        db = database.DBConnection()
        try:
            res = db.match(
                "SELECT Reject,DateType from magazines WHERE Title=?", (title,)
            )
        finally:
            db.close()
        if not res:
            logger.error(f"{title} not found in database")
            return False

        rejects = get_list(res["Reject"])
        title_words = replace_all(title.lower(), dic).split()

        # import any files in this directory that match the title, are a magazine file, and have a parseable date
        for f in listdir(source_dir):
            _, extn = os.path.splitext(str(f))
            extn = extn.lstrip(".")
            if not extn or extn.lower() not in get_list(CONFIG["MAG_TYPE"]):
                continue

            matchinglogger.debug(f"Trying to match {f}")
            filename_words = replace_all(f.lower(), dic).split()
            found_title = True
            for word in title_words:
                if word not in filename_words:
                    matchinglogger.debug(f"[{word}] not found in {f}")
                    found_title = False
                    break

            if found_title:
                for item in rejects:
                    if item in filename_words:
                        matchinglogger.debug(f"Rejecting {f}, contains {item}")
                        found_title = False
                        break

            if found_title and "*" in rejects:  # strict rejection mode, no extraneous words
                nouns = get_list(CONFIG["ISSUE_NOUNS"])
                nouns.extend(get_list(CONFIG["VOLUME_NOUNS"]))
                nouns.extend(get_list(CONFIG["MAG_NOUNS"]))
                nouns.extend(get_list(CONFIG["MAG_TYPE"]))
                # this unusual construct is because if we just extend(lazylibrarian.SEASONS)
                # we get reports that docker complains about unhashable type
                # but it works fine with python in a terminal.
                # Some docker quirk, or the python version in the docker ???
                # nouns.extend(lazylibrarian.SEASONS)
                nouns.extend(list(lazylibrarian.SEASONS.keys()))
                nouns = set(nouns)
                valid = True
                for word in filename_words:
                    if (
                        word not in title_words
                        and word not in nouns
                        and not word.isdigit()
                    ):
                        cleanword = unaccented(word).lower()
                        valid = False
                        for month in range(1, 13):
                            if (
                                word in lazylibrarian.MONTHNAMES[0][month]
                                or cleanword in lazylibrarian.MONTHNAMES[1][month]
                            ):
                                valid = True
                                break
                        if not valid:
                            logger.debug(f"Rejecting {f}, strict, contains {word}")
                            break
                if not valid:
                    found_title = False

            if found_title:
                dateparts = get_dateparts(f, res["DateType"])
                issuenum_type = dateparts["style"]
                issuedate = searchmag.get_default_date(dateparts)
                if issuenum_type:
                    if process_mag_from_file(
                        os.path.join(source_dir, f), title, issuedate
                    ):
                        logger.debug(f"Processed {title} issue {issuedate}")
                    else:
                        logger.warning(f"Failed to process {f}")
                else:
                    matchinglogger.debug(f"Unrecognised date style for {f}")
        return True

    except Exception:
        logger.error(f"Unhandled exception in process_issues: {traceback.format_exc()}")
        return False


@restore_thread_name('IMPORTALT')
def process_alternate(source_dir=None, library="eBook"):
    # import a book/audiobook from an alternate directory
    # noinspection PyBroadException
    logger = logging.getLogger(__name__)
    # noinspection PyBroadException
    try:
        if not source_dir:
            logger.warning("Alternate Directory not configured")
            return False
        if not path_isdir(source_dir):
            logger.warning(f"{source_dir} is not a directory")
            return False
        if source_dir.startswith(get_directory(library)):
            logger.warning(
                "Alternate directory must not be the same as or inside Destination"
            )
            return False

        TELEMETRY.record_usage_data("Process/Alternate")
        logger.debug(f"Processing {library} directory {source_dir}")
        # first, recursively process any books in subdirectories
        flist = listdir(source_dir)
        for fname in flist:
            subdir = os.path.join(source_dir, fname)
            if path_isdir(subdir):
                process_alternate(subdir, library=library)

        metadata = {}
        bookid = ""

        if "LL.(" in source_dir:
            bookid = source_dir.split("LL.(")[1].split(")")[0]
            db = database.DBConnection()
            res = db.match(
                "SELECT BookName,AuthorName from books,authors WHERE books.AuthorID = authors.AuthorID "
                "AND BookID=?",
                (bookid,),
            )
            if res:
                metadata = {"title": res["BookName"], "creator": res["AuthorName"]}
                logger.debug(
                    f"Importing {library} bookid {bookid} for {res['AuthorName']} {res['BookName']}"
                )
            else:
                logger.warning(f"Failed to find LL bookid {bookid} in database")
            db.close()

        if library == "eBook":
            # only import one book from each alternate (sub)directory, this is because
            # the importer may delete the directory after importing a book,
            # depending on lazylibrarian.CONFIG['DESTINATION_COPY'] setting
            # also if multiple books in a folder and only a "metadata.opf"
            # or "cover.jpg" which book is it for?
            reject = multibook(source_dir)
            if reject:
                logger.debug(f"Not processing {source_dir}, found multiple {reject}")
                return False

            new_book = book_file(source_dir, booktype="ebook", config=CONFIG)
            if not new_book:
                # check if an archive in this directory
                for f in listdir(source_dir):
                    if not is_valid_type(f, extensions=CONFIG.get_all_types_list()):
                        # Is file an archive, if so look inside and extract to new dir
                        res = _unpack_archive(os.path.join(source_dir, f), source_dir, f)
                        if res:
                            source_dir = res
                            break
                new_book = book_file(source_dir, booktype="ebook", config=CONFIG)
            if not new_book:
                logger.warning(f"No book file found in {source_dir}")
                return False

            if not metadata:
                # if we haven't already got metadata from an LL.num
                # see if there is a metadata file in this folder with the info we need
                # try book_name.opf first, or fall back to any filename.opf
                metafile = f"{os.path.splitext(new_book)[0]}.opf"
                if not path_isfile(metafile):
                    metafile = opf_file(source_dir)
                if metafile and path_isfile(metafile):
                    try:
                        metadata = get_book_info(metafile)
                    except Exception as e:
                        logger.warning(
                            f"Failed to read metadata from {metafile}, {type(e).__name__} {str(e)}"
                        )
                else:
                    logger.debug(f"No metadata file found for {new_book}")

            if "title" not in metadata or "creator" not in metadata:
                # if not got both, try to get metadata from the book file
                extn = os.path.splitext(new_book)[1]
                if extn.lower() in [".epub", ".mobi"]:
                    try:
                        metadata = get_book_info(new_book)
                    except Exception as e:
                        logger.warning(
                            f"No metadata found in {new_book}, {type(e).__name__} {str(e)}"
                        )
        else:
            new_book = book_file(source_dir, booktype="audiobook", config=CONFIG)
            if not new_book:
                logger.warning(f"No audiobook file found in {source_dir}")
                return False
            if not metadata:
                id3r = id3read(new_book)
                author = id3r.get("author")
                book = id3r.get("title")
                # use album instead of title if it is set
                if "album" in id3r and id3r.get("album"):
                    book = id3r["album"]

                if author and book:
                    metadata["creator"] = author
                    metadata["title"] = book
                    metadata["narrator"] = id3r.get("narrator")

        if "title" in metadata and "creator" in metadata:
            authorname = metadata["creator"]
            bookname = metadata["title"]
            db = database.DBConnection()
            try:
                authorid = ""
                # noinspection PyUnusedLocal
                results = None  # pycharm incorrectly thinks this isn't needed
                authmatch = db.match(
                    "SELECT * FROM authors where AuthorName=?", (authorname,)
                )

                if not authmatch:
                    # try goodreads/openlibrary preferred authorname
                    if CONFIG["BOOK_API"] in ["OpenLibrary", "GoogleBooks"]:
                        logger.debug(f"Checking OpenLibrary for [{authorname}]")
                        ol = OpenLibrary()
                        try:
                            author_gr = ol.find_author_id(authorname=authorname)
                        except Exception as e:
                            author_gr = {}
                            logger.warning(
                                f"No author id for [{authorname}] {type(e).__name__}"
                            )
                    elif CONFIG["BOOK_API"] in ["HardCover"]:
                        logger.debug(f"Checking HardCover for [{authorname}]")
                        hc = HardCover()
                        try:
                            author_gr = hc.find_author_id(
                                authorname=authorname, title=bookname
                            )
                        except Exception as e:
                            author_gr = {}
                            logger.warning(
                                f"No author id for [{authorname}] {type(e).__name__}"
                            )
                    else:
                        logger.debug(f"Checking GoodReads for [{authorname}]")
                        gr = GoodReads()
                        try:
                            author_gr = gr.find_author_id(
                                authorname=authorname, title=bookname
                            )
                        except Exception as e:
                            author_gr = {}
                            logger.warning(
                                f"No author id for [{authorname}] {type(e).__name__}"
                            )
                    if author_gr:
                        grauthorname = author_gr["authorname"]
                        authorid = author_gr["authorid"]
                        logger.debug(f"Found [{grauthorname}] for [{authorname}]")
                        authorname = grauthorname
                        authmatch = db.match(
                            "SELECT * FROM authors where AuthorID=?", (authorid,)
                        )

                if authmatch:
                    logger.debug(f"Author {authorname} found in database")
                    authorid = authmatch["authorid"]
                else:
                    logger.debug(f"Author {authorname} not found, adding to database")
                    if authorid:
                        ret_id = add_author_to_db(
                            authorid=authorid,
                            addbooks=CONFIG.get_bool("NEWAUTHOR_BOOKS"),
                            reason=f"process_alternate: {bookname}",
                        )
                        if ret_id and ret_id != authorid:
                            logger.debug(f"Authorid mismatch {authorid}/{ret_id}")
                            authorid = ret_id
                    else:
                        aname, authorid, _ = add_author_name_to_db(
                            author=authorname,
                            reason=f"process_alternate: {bookname}",
                            title=bookname,
                        )
                        if aname and aname != authorname:
                            authorname = aname
                        if not aname:
                            authorid = ""

                if authorid:
                    bookid, _ = find_book_in_db(
                        authorname,
                        bookname,
                        ignored=False,
                        library=library,
                        reason=f"process_alternate: {bookname}",
                    )

                if authorid and not bookid:
                    # new book, or new author where we didn't want to load their back catalog
                    searchterm = (
                        f"{unaccented(bookname, only_ascii=False)}<ll>"
                        f"{unaccented(authorname, only_ascii=False)}"
                    )
                    match = {}
                    search_match_threshold = CONFIG.get_int("MATCH_RATIO")
                    results = search_for(searchterm)
                    for result in results:
                        if (
                            result["book_fuzz"] >= search_match_threshold
                            and result["authorid"] == authorid
                        ):
                            match = result
                            break
                    if not match:  # no match on full searchterm, try splitting out subtitle and series
                        newtitle, _, _ = split_title(authorname, bookname)
                        if newtitle != bookname:
                            bookname = newtitle
                            searchterm = (
                                f"{unaccented(bookname, only_ascii=False)}<ll>"
                                f"{unaccented(authorname, only_ascii=False)}"
                            )
                            results = search_for(searchterm)
                            for result in results:
                                if (
                                    result["book_fuzz"] >= search_match_threshold
                                    and result["authorid"] == authorid
                                ):
                                    match = result
                                    break
                    if match:
                        logger.info(
                            f"Found ({round(match['book_fuzz'], 2)}%) {match['authorname']}: {match['bookname']} for "
                            f"{authorname}: {bookname}"
                        )
                        import_book(
                            match["bookid"],
                            ebook="Skipped",
                            audio="Skipped",
                            wait=True,
                            reason="Added from alternate dir",
                        )
                        imported = db.match(
                            "select * from books where BookID=?", (match["bookid"],)
                        )
                        if imported:
                            bookid = match["bookid"]
                            update_totals(authorid)
                db.close()

            except Exception as e:
                db.close()
                logger.error(f"Exception in process_alternate: {e}")
                return False

            if not bookid:
                author, book, forced_bookid = get_book_meta(source_dir, "postprocess")
                if process_book_from_dir(
                    source_dir=source_dir, library=library, bookid=forced_bookid
                ):
                    return True

            if not bookid:
                msg = f"{library} {bookname} by {authorname} not found in database"
                if not results:
                    msg += ", No results returned"
                    logger.warning(msg)
                else:
                    msg += ", No match found"
                    logger.warning(msg)
                    msg = (
                        f"Closest match ({round(results[0]['author_fuzz'], 2)}% "
                        f"{round(results[0]['book_fuzz'], 2)}%) "
                        f"{results[0]['authorname']}: {results[0]['bookname']}"
                    )
                    if results[0]["authorid"] != authorid:
                        msg += " wrong authorid"
                    logger.warning(msg)
                return False

            db = database.DBConnection()
            if library == "eBook":
                res = db.match("SELECT Status from books WHERE BookID=?", (bookid,))
                if res and res["Status"] == "Ignored":
                    logger.warning(
                        f"{library} {bookname} by {authorname} is marked Ignored in database, importing anyway"
                    )
            else:
                res = db.match(
                    "SELECT AudioStatus,Narrator from books WHERE BookID=?", (bookid,)
                )
                if metadata.get("narrator", "") and res and not res["Narrator"]:
                    db.action(
                        "update books set narrator=? where bookid=?",
                        (metadata["narrator"], bookid),
                    )
                if res and res["AudioStatus"] == "Ignored":
                    logger.warning(
                        f"{library} {bookname} by {authorname} is marked Ignored in database, importing anyway"
                    )
            db.close()
            return process_book(source_dir, bookid, library=library)

        logger.warning(f"{library} {new_book} has no metadata")
        db = database.DBConnection()
        res = _process_ll_bookid_folders_from_list(source_dir, db, logger)
        db.close()
        if not res:
            logger.warning(f"{source_dir} has no book with LL.number")
            return False
    except Exception:
        logger.error(
            f"Unhandled exception in process_alternate: {traceback.format_exc()}"
        )
        return False
