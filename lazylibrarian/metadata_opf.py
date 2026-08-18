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
OPF metadata file generation.

Creates Calibre-compatible metadata.opf files for books, magazines, and comics.
OPF (Open Packaging Format) files contain metadata like title, author, description,
ISBN, publication date, and other bibliographic information.
"""

import datetime
import logging
import os
import subprocess

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.common import calibre_prg
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import jpg_file, listdir, path_exists, setperm, syspath
from lazylibrarian.formatter import (
    check_int,
    get_list,
    make_unicode,
    replace_all,
    sanitize,
    surname_first,
)


def create_comic_opf(pp_path, data, global_name, overwrite=False):
    """Needs calibre to be configured to read metadata from file contents, not filename"""
    title = data["Title"]
    issue = data["IssueID"]
    contributors = data.get("Contributors", "")
    issue_id = f"{data['ComicID']}_{data['IssueID']}"
    iname = f"{data['Title']}: {data['IssueID']}"
    publisher = data["Publisher"]
    mtime = os.path.getmtime(data["IssueFile"])
    iss_acquired = datetime.date.isoformat(datetime.date.fromtimestamp(mtime))

    opf_dict = {
        "AuthorName": title,
        "BookID": issue_id,
        "BookName": iname,
        "FileAs": iname,
        "BookDesc": "",
        "BookIsbn": "",
        "BookDate": iss_acquired,
        "BookLang": "",
        "BookImg": f"{global_name}.jpg",
        "BookPub": publisher,
        "Series": title,
        "Series_index": issue,
    }  # type: dict
    if contributors:
        opf_dict["Contributors"] = contributors
    # noinspection PyTypeChecker
    return create_opf(pp_path, opf_dict, global_name, overwrite=overwrite)


def create_mag_opf(
    issuefile, title, issue, issue_id, language="en", genres="", overwrite=False
):
    """Needs calibre to be configured to read metadata from file contents, not filename"""
    logger = logging.getLogger(__name__)

    if CONFIG.get_bool("IMP_CALIBRE_MAGTITLE"):
        authors = title
    else:
        authors = "magazines"

    logger.debug(
        f"Creating opf with file:{issuefile} authors:{authors} title:{title} issue:{issue} "
        f"issueid:{issue_id} language:{language} overwrite:{overwrite}"
    )
    dest_path, global_name = os.path.split(issuefile)
    global_name = os.path.splitext(global_name)[0]

    if CONFIG.get_bool("IMP_CALIBRE_MAGISSUE"):
        iname = issue
    elif (
        issue
        and len(issue) == 10
        and issue[8:] == "01"
        and issue[4] == "-"
        and issue[7] == "-"
    ):  # yyyy-mm-01
        yr = issue[0:4]
        mn = issue[5:7]
        lang = 0
        cnt = 0
        while cnt < len(lazylibrarian.MONTHNAMES[0][0]):
            if lazylibrarian.MONTHNAMES[0][0][cnt] == CONFIG["DATE_LANG"]:
                lang = cnt
                break
            cnt += 1
        # monthnames for this month, eg ["January", "Jan", "enero", "ene"]
        monthname = lazylibrarian.MONTHNAMES[0][int(mn)]
        month = monthname[lang]  # lang = full name, lang+1 = short name
        iname = f"{title} - {month} {yr}"  # The Magpi - January 2017
    elif title in issue:
        iname = issue  # 0063 - Android Magazine -> 0063
    else:
        iname = f"{title} - {issue}"  # Android Magazine - 0063

    mtime = os.path.getmtime(issuefile)
    iss_acquired = datetime.date.isoformat(datetime.date.fromtimestamp(mtime))

    opf_dict = {
        "AuthorName": authors,
        "BookID": issue_id,
        "BookName": iname,
        "FileAs": authors,
        "BookDesc": "",
        "BookIsbn": "",
        "BookDate": iss_acquired,
        "BookLang": language,
        "BookImg": f"{global_name}.jpg",
        "BookPub": "",
        "Series": title,
        "Series_index": issue,
        "Scheme": "lazylibrarian",
        "BookGenre": genres,
    }  # type: dict
    # noinspection PyTypeChecker
    return create_opf(dest_path, opf_dict, global_name, overwrite=overwrite)


def create_opf(dest_path, data_row, global_name=None, overwrite=False):
    logger = logging.getLogger(__name__)
    # titles can legitimately contain a "/" (eg an omnibus title like "Broken Angels /
    # Woken Furies / Altered Carbon"); sanitize() only collapses doubled separators, so
    # replace real ones first or they get read as path separators
    safe_name = (global_name or '').replace('/', '-').replace('\\', '-')
    opfpath = os.path.join(dest_path, f"{sanitize(safe_name, is_folder_or_file=True)}.opf")
    if not overwrite and path_exists(opfpath):
        logger.debug(f"{opfpath} already exists. Did not create one.")
        setperm(opfpath)
        return opfpath, False

    data_dict = dict(data_row)

    bookid = data_dict["BookID"]
    if bookid.startswith("CV"):
        scheme = "COMICVINE"
    elif bookid.startswith("CX"):
        scheme = "COMIXOLOGY"
    elif "Scheme" in data_dict:
        scheme = data_dict["Scheme"]
    elif bookid.isdigit():
        # TODO could be goodreads or hardcover, can't be sure
        scheme = "goodreads"
        if CONFIG["BOOK_API"] == "HardCover":
            scheme = "HardCover"
    elif bookid.startswith("OL"):
        scheme = "OpenLibrary"
    elif bookid.startswith("B0") and len(bookid) == 10:
        scheme = "Audible"
    else:
        scheme = "GoogleBooks"

    seriesname = ""
    seriesnum = ""
    if "Series_index" not in data_dict:
        # no series details passed in data dictionary, look them up in db
        db = database.DBConnection()

        results = {}
        if "LT_WorkID" in data_dict and data_dict["LT_WorkID"]:
            cmd = "SELECT SeriesID,SeriesNum from member WHERE workid=?"
            results = dict(db.match(cmd, (data_dict["LT_WorkID"],)))
        if not results and "WorkID" in data_dict and data_dict["WorkID"]:
            cmd = "SELECT SeriesID,SeriesNum from member WHERE workid=?"
            results = dict(db.match(cmd, (data_dict["WorkID"],)))
        if not results:
            cmd = "SELECT SeriesID,SeriesNum from member WHERE bookid=?"
            results = dict(db.match(cmd, (bookid,)))
        if results:
            seriesid = results.get("SeriesID")
            serieslist = get_list(results.get("SeriesNum"))
            # might be "Book 3.5" or similar, just get the numeric part
            while serieslist:
                seriesnum = serieslist.pop()
                try:
                    _ = float(seriesnum)
                    break
                except ValueError:
                    seriesnum = ""

            if not seriesnum:
                # couldn't figure out number, keep everything we got, could be something like "Book Two"
                serieslist = results.get("SeriesNum")

            cmd = "SELECT SeriesName from series WHERE seriesid=?"
            results = dict(db.match(cmd, (seriesid,)))
            if results:
                seriesname = results.get("SeriesName", "")
                if not seriesnum:
                    # add what we got to series name and set seriesnum to 1 so user can sort it out manually
                    seriesname = f"{seriesname} {serieslist}"
                    seriesnum = 1
        db.close()

    opfinfo = (
        f'<?xml version="1.0"  encoding="UTF-8"?>\n\
<package version="2.0" xmlns="http://www.idpf.org/2007/opf" >\n\
    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">\n\
        <dc:title>{data_dict.get("BookName", "")}</dc:title>\n\
        <dc:language>{data_dict.get("BookLang", "")}</dc:language>\n'
    )

    opfinfo += (
        f'        <dc:identifier opf:scheme="{scheme}">{bookid}</dc:identifier>\n'
    )

    if "Contributors" in data_dict:
        # what calibre does is split into individuals and add a line for each, e.g.
        # <dc:creator opf:file-as="Pastoras, Das &amp; Ribic, Esad &amp; Aaron, Jason"
        # opf:role="aut">Das Pastoras</dc:creator>
        # <dc:creator opf:file-as="Pastoras, Das &amp; Ribic, Esad &amp; Aaron, Jason"
        # opf:role="aut">Esad Ribic</dc:creator>
        # <dc:creator opf:file-as="Pastoras, Das &amp; Ribic, Esad &amp; Aaron, Jason"
        # opf:role="aut">Jason Aaron</dc:creator>
        #
        entries = []
        names = ""
        for contributor in get_list(data_dict.get("Contributors"), ","):
            if ":" in contributor:
                role, name = contributor.split(":", 1)
            else:
                name = contributor
                role = "Unknown"
            if name and role:
                entries.append([name.strip(), role.strip()])
                if names:
                    names += " &amp; "
                names += surname_first(
                    name, postfixes=get_list(CONFIG.get_csv("NAME_POSTFIX"))
                )
        for entry in entries:
            opfinfo += f'        <dc:creator opf:file-as="{names}" opf:role="{entry[1]}">{entry[0]}</dc:creator>\n'
    elif data_dict.get("FileAs", ""):
        opfinfo += (
            f'        <dc:creator opf:file-as="{data_dict.get("FileAs")}" opf:role="aut">'
            f"{data_dict.get('FileAs')}</dc:creator>\n"
        )
    else:
        aname = surname_first(data_dict.get("AuthorName", ""), postfixes=get_list(CONFIG.get_csv("NAME_POSTFIX")))
        opfinfo += (
            f'        <dc:creator opf:file-as="{aname}" opf:role="aut">{data_dict.get("AuthorName")}</dc:creator>\n'
        )
    if data_dict.get("BookIsbn", ""):
        opfinfo += f'        <dc:identifier opf:scheme="ISBN">{data_dict.get("BookIsbn")}</dc:identifier>\n'

    if data_dict.get("BookPub", ""):
        opfinfo += f"        <dc:publisher>{data_dict.get('BookPub')}</dc:publisher>\n"

    if data_dict.get("BookDate", ""):
        opfinfo += f"        <dc:date>{data_dict.get('BookDate')}</dc:date>\n"

    if data_dict.get("BookDesc", ""):
        opfinfo += (
            f"        <dc:description>{data_dict.get('BookDesc')}</dc:description>\n"
        )

    if CONFIG.get_bool("GENRE_TAGS") and data_dict.get("BookGenre", ""):
        for genre in get_list(data_dict["BookGenre"], ","):
            opfinfo += f"        <dc:subject>{genre}</dc:subject>\n"

    if data_dict.get("BookRate", ""):
        rate = check_int(data_dict.get("BookRate", 0), 0)
        rate = int(round(rate * 2))  # calibre uses 0-10, goodreads 0-5
        opfinfo += f'        <meta content="{rate}" name="calibre:rating"/>\n'

    if seriesname:
        opfinfo += f'        <meta content="{seriesname}" name="calibre:series"/>\n'
    elif "Series" in data_dict:
        opfinfo += f'        <meta content="{data_dict.get("Series")}" name="calibre:series"/>\n'

    if seriesnum:
        opfinfo += (
            f'        <meta content="{seriesnum}" name="calibre:series_index"/>\n'
        )
    elif "Series_index" in data_dict:
        opfinfo += f'        <meta content="{data_dict.get("Series_index")}" name="calibre:series_index"/>\n'
    if data_dict.get("Narrator", ""):
        opfinfo += f'        <meta content="{data_dict.get("Narrator")}" name="lazylibrarian:narrator"/>\n'

    coverfile = jpg_file(dest_path)
    if coverfile:
        coverfile = os.path.basename(coverfile)
    else:
        coverfile = "cover.jpg"

    opfinfo += (
        f'        <guide>\n\
                <reference href="{coverfile}" type="cover" title="Cover"/>\n\
            </guide>\n\
        </metadata>\n\
    </package>'
    )  # file in current directory, not full path

    dic = {"...": "", " & ": " ", " = ": " ", "$": "s", " + ": " ", "*": ""}
    opfinfo = str(make_unicode(replace_all(opfinfo, dic)))
    try:
        with open(syspath(opfpath), "w", encoding="utf-8") as opf:
            opf.write(opfinfo)
        logger.debug(f"Saved metadata to: {opfpath}")
        setperm(opfpath)
        return opfpath, True
    except Exception as e:
        logger.error(f"Error creating opf {opfpath}, {type(e).__name__} {str(e)}")
        return "", False


def write_meta(book_folder, opf):
    logger = logging.getLogger(__name__)
    if not path_exists(opf):
        logger.error(f"No opf file [{opf}]")
        return

    ebook_meta = calibre_prg("ebook-meta")
    if not ebook_meta:
        logger.debug("No ebook-meta found")
        return

    flist = listdir(book_folder)
    for fname in flist:
        fname = str(fname)
        if CONFIG.is_valid_booktype(fname, booktype="ebook"):
            book = os.path.join(book_folder, fname)
            params = [ebook_meta, book, "--write_meta", opf]
            logger.debug(f"Writing metadata to [{fname}]")
            try:
                if os.name != "nt":
                    _ = subprocess.check_output(
                        params, preexec_fn=lambda: os.nice(10), stderr=subprocess.STDOUT
                    )
                else:
                    _ = subprocess.check_output(params, stderr=subprocess.STDOUT)
                logger.debug(f"Metadata written from {opf}")
            except Exception as e:
                logger.error(str(e))
