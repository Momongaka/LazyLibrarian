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
Post-processing metadata models and factory functions.

This module provides type-safe metadata classes for books, magazines, and comics,
along with factory functions to retrieve and prepare metadata from the database.
"""

import contextlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from lazylibrarian.bookrename import name_vars, stripspaces
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import get_directory
from lazylibrarian.formatter import (
    check_int,
    make_unicode,
    sanitize,
    unaccented,
)
from lazylibrarian.magazinescan import create_id, format_issue_filename, get_dateparts
from lazylibrarian.postprocess_utils import enforce_bytes, enforce_str


class BookType(str, Enum):
    AUDIOBOOK = "audiobook"
    EBOOK = "ebook"
    MAGAZINE = "magazine"
    COMIC = "comic"

    @classmethod
    def from_string(cls, value: str) -> "BookType":
        # Accept flexible user input like "AudioBook", "audio book", "audio_book"
        normalized = value.strip().lower().replace("_", " ").replace("-", " ")
        normalized = "".join(normalized.split())  # collapse whitespace

        mapping = {  # Allows flexibility as we get new variants
            "audiobook": cls.AUDIOBOOK,
            "ebook": cls.EBOOK,
            "magazine": cls.MAGAZINE,
            "mag": cls.MAGAZINE,
            "comic": cls.COMIC,
        }

        try:
            return mapping[normalized]
        except KeyError as e:
            allowed = ", ".join(t.value for t in cls)
            raise ValueError(f"Invalid book_type {value!r}. Allowed: {allowed}") from e


@dataclass
class BookMetadata(ABC):
    """
    Abstract base class for book metadata used during post-processing.

    This replaces the unstructured 'data' dict with typed attributes,
    providing better IDE support, type safety, and validation.

    Each subclass must implement book_type_enum property to identify itself.
    """

    book_id: str

    book_type_enum: BookType = BookType.EBOOK

    # Destination paths (computed during prepare phase)
    dest_path: str = ""
    global_name: str = ""

    @property
    def book_type(self) -> str:
        """Return string represenation of this metadata object"""
        return self.book_type_enum.value

    @abstractmethod
    def get_display_name(self) -> str:
        """Get display name for logging/notifications"""

    @abstractmethod
    def get_opf_data(self) -> dict:
        """Get data formatted for OPF/metadata file creation"""

    @abstractmethod
    def get_processing_fields(self) -> dict:
        """
        Get fields needed for preprocessing and file operations.

        Returns dict with standardized keys:
            - authorname, bookname, issueid, title, issuedate, mag_genres, cover
        """


@dataclass
class EbookMetadata(BookMetadata):
    """Metadata for ebooks and audiobooks"""

    author_name: str = ""
    book_name: str = ""
    book_desc: str = ""
    book_isbn: str = ""
    book_img: str = ""
    book_date: str = ""
    book_lang: str = ""
    book_pub: str = ""
    book_rate: str = ""
    book_genre: str = ""
    narrator: str = ""
    requester: str = ""
    audio_requester: str = ""

    # External IDs
    gr_id: str = ""
    gb_id: str = ""
    ol_id: str = ""
    hc_id: str = ""
    dnb_id: str = ""
    ran_id: str = ""

    def __post_init__(self):
        """Validate required fields"""
        if not self.book_id:
            raise ValueError("book_id is required for EbookMetadata")
        if not self.author_name and not self.book_name:
            raise ValueError(
                "Either author_name or book_name is required for EbookMetadata"
            )

    def get_display_name(self) -> str:
        return f"{self.author_name} - {self.book_name}"

    def get_processing_fields(self) -> dict:
        """Get fields needed for preprocessing and file operations"""
        return {
            "authorname": self.author_name,
            "bookname": self.book_name,
            "issueid": "",
            "title": "",
            "issuedate": "",
            "mag_genres": "",
            "cover": "",
        }

    def get_opf_data(self) -> dict:
        """Get data formatted for OPF creation"""
        return {
            "AuthorName": self.author_name,
            "BookName": self.book_name,
            "BookID": self.book_id,
            "BookDesc": self.book_desc,
            "BookIsbn": self.book_isbn,
            "BookImg": self.book_img,
            "BookDate": self.book_date,
            "BookLang": self.book_lang,
            "BookPub": self.book_pub,
            "BookRate": self.book_rate,
            "BookGenre": self.book_genre,
            "Narrator": self.narrator,
            "Requester": self.requester,
            "AudioRequester": self.audio_requester,
        }


@dataclass
class MagazineMetadata(BookMetadata):
    """Metadata for magazines"""

    title: str = ""
    issue_date: str = ""
    issue_id: str = ""
    language: str = ""
    genres: str = ""
    cover_page: int = 0
    most_recent_issue: str = ""  # For tracking most recent issue

    def __post_init__(self):
        self.book_type_enum = BookType.MAGAZINE

        """Validate required fields"""
        if not self.book_id:
            raise ValueError("book_id is required for MagazineMetadata")
        if not self.title:
            raise ValueError("title is required for MagazineMetadata")

    def get_display_name(self) -> str:
        return f"{self.title} - {self.issue_date}"

    def get_processing_fields(self) -> dict:
        """Get fields needed for preprocessing and file operations"""
        return {
            "authorname": "",
            "bookname": "",
            "issueid": self.issue_id,
            "title": self.title,
            "issuedate": self.issue_date,
            "mag_genres": self.genres,
            "cover": self.cover_page,
        }

    def get_opf_data(self) -> dict:
        """Get data formatted for magazine OPF"""
        return {
            "Title": self.title,
            "IssueDate": self.issue_date,
            "IssueID": self.issue_id,
            "Language": self.language,
            "Genre": self.genres,
        }


@dataclass
class ComicMetadata(BookMetadata):
    """Metadata for comics"""

    title: str = ""
    comic_id: str = ""
    issue_id: str = ""
    issue_date: str = ""
    issue_acquired: str = ""
    issue_file: str = ""
    cover: str = ""
    publisher: str = ""
    contributors: str = ""
    most_recent_issue: str = ""  # For tracking most recent issue

    def __post_init__(self):
        self.book_type_enum = BookType.COMIC
        """Validate required fields"""
        if not self.book_id:
            raise ValueError("book_id is required for ComicMetadata")
        if not self.comic_id or not self.issue_id:
            raise ValueError(
                "Both comic_id and issue_id are required for ComicMetadata"
            )

    def get_display_name(self) -> str:
        return f"{self.title} #{self.issue_id}"

    def get_full_id(self) -> str:
        """Get combined comic+issue ID"""
        return f"{self.comic_id}_{self.issue_id}"

    def get_processing_fields(self) -> dict:
        """Get fields needed for preprocessing and file operations"""
        return {
            "authorname": "",
            "bookname": "",
            "issueid": self.issue_id,
            "title": self.title,
            "issuedate": self.issue_date,
            "mag_genres": "",
            "cover": "",
        }

    def get_opf_data(self) -> dict:
        """Get data formatted for comic OPF"""
        return {
            "Title": self.title,
            "ComicID": self.comic_id,
            "IssueID": self.issue_id,
            "IssueDate": self.issue_date,
            "IssueAcquired": self.issue_acquired,
            "IssueFile": self.issue_file,
            "Cover": self.cover,
            "Publisher": self.publisher,
            "Contributors": self.contributors,
        }


def prepare_book_metadata(book_id: str, book_type: str, db) -> EbookMetadata | None:
    """
    Retrieve book metadata and prepare destination paths.

    Args:
        book_id: Book ID to look up
        book_type: Type of book in AUX_INFO format (eBook, AudioBook)
        db: Database connection

    Returns:
        EbookMetadata object with all book data and destination paths, or None if not found
    """
    # Query all fields we'll need (consolidated from multiple queries)
    query = (
        "SELECT AuthorName,BookName,BookDesc,BookIsbn,BookImg,BookDate,BookLang,BookPub,BookRate,"
        "Requester,AudioRequester,BookGenre,Narrator,"
        "books.gr_id,books.ol_id,books.gb_id,books.hc_id,books.dnb_id,books.ran_id "
        "from books,authors WHERE BookID=? and books.AuthorID = authors.AuthorID"
    )
    result = db.match(query, (book_id,))

    if not result:
        return None

    book_data = dict(result)
    namevars = name_vars(book_id)

    # book_type can come in many forms depending on the source (e.g., AUX_INFO column, CONFIG, etc)
    # this will normalize it into a common format for post processing
    book_type_enum = BookType.EBOOK

    with contextlib.suppress(ValueError):
        book_type_enum = BookType.from_string(book_type)

    if book_type_enum == BookType.AUDIOBOOK and get_directory("Audio"):
        dest_path = str(namevars["AudioFolderName"])
        dest_dir = str(get_directory("Audio"))
    else:
        dest_path = str(namevars["FolderName"])
        dest_dir = str(get_directory("eBook"))

    dest_path = str(stripspaces(os.path.join(dest_dir, dest_path)))
    # Validate encoding via make_utf8bytes, then decode to string for metadata
    dest_path = enforce_str(enforce_bytes(dest_path))
    global_name = str(namevars["BookFile"])

    return EbookMetadata(
        book_id=book_id,
        book_type_enum=book_type_enum,
        dest_path=dest_path,
        global_name=global_name,
        author_name=book_data.get("AuthorName", ""),
        book_name=book_data.get("BookName", ""),
        book_desc=book_data.get("BookDesc", ""),
        book_isbn=book_data.get("BookIsbn", ""),
        book_img=book_data.get("BookImg", ""),
        book_date=book_data.get("BookDate", ""),
        book_lang=book_data.get("BookLang", ""),
        book_pub=book_data.get("BookPub", ""),
        book_rate=book_data.get("BookRate", ""),
        book_genre=book_data.get("BookGenre", ""),
        narrator=book_data.get("Narrator", ""),
        requester=book_data.get("Requester", ""),
        audio_requester=book_data.get("AudioRequester", ""),
        gr_id=book_data.get("gr_id", ""),
        gb_id=book_data.get("gb_id", ""),
        ol_id=book_data.get("ol_id", ""),
        hc_id=book_data.get("hc_id", ""),
        dnb_id=book_data.get("dnb_id", ""),
        ran_id=book_data.get("ran_id", ""),
    )


def prepare_magazine_metadata(title, aux_info, db) -> MagazineMetadata | None:
    """
    Retrieve magazine metadata and prepare destination paths.

    Args:
        title: Magazine title to look up
        aux_info: Auxiliary information (issue date)
        db: Database connection

    Returns:
        MagazineMetadata object with all magazine data and destination paths, or None if not found
    """
    issue_id = create_id(f"{title} {aux_info}")

    # Get additional metadata from magazines table
    result = db.match(
        "SELECT IssueDate,Language,Genre,CoverPage from magazines WHERE Title=?",
        (title,),
    )
    mag_data = dict(result) if result else {}

    if not mag_data:
        return None

    mostrecentissue = mag_data.get("IssueDate", "")
    language = mag_data.get("Language", "")
    genres = mag_data.get("Genre", "")
    cover_page = check_int(mag_data.get("CoverPage", 0), 0)

    dateparts = get_dateparts(aux_info)
    dest_path: str = format_issue_filename(CONFIG["MAG_DEST_FOLDER"], title, dateparts)

    if CONFIG.get_bool("MAG_RELATIVE"):
        dest_dir = str(get_directory("eBook"))
        dest_path = stripspaces(os.path.join(dest_dir, dest_path))

    # Validate encoding via make_utf8bytes, then decode to string for metadata
    dest_path = enforce_str(enforce_bytes(dest_path))
    global_name = format_issue_filename(CONFIG["MAG_DEST_FILE"], title, dateparts)

    return MagazineMetadata(
        book_id=title,
        dest_path=dest_path,
        global_name=global_name,
        title=title,
        issue_date=aux_info,
        issue_id=issue_id,
        language=language,
        genres=genres,
        cover_page=cover_page,
        most_recent_issue=mostrecentissue,
    )


def prepare_comic_metadata(book_id, db) -> ComicMetadata | None:
    """
    Retrieve comic metadata and prepare destination paths.

    Args:
        book_id: Comic ID in format "ComicID_IssueID"
        db: Database connection

    Returns:
        ComicMetadata object with all comic data and destination paths, or None if not found
    """
    if not book_id or "_" not in book_id:
        return None

    comicid, issueid = book_id.split("_")
    result = db.match(
        "SELECT Title,Publisher,LatestIssue from comics WHERE ComicID=?", (comicid,)
    )

    if not result:
        return None

    comic_data = dict(result)

    # Get issue-specific data
    result = db.match(
        "SELECT IssueDate,IssueAcquired,IssueFile,Cover,Contributors "
        "from comicissues WHERE ComicID=? AND IssueID=?",
        (comicid, issueid),
    )
    issue_data = dict(result) if result else {}

    mostrecentissue = comic_data["LatestIssue"]

    comic_name = enforce_str(
        make_unicode(unaccented(sanitize(comic_data["Title"]), only_ascii=False) or "")
    )
    publisher = enforce_str(
        make_unicode(unaccented(sanitize(comic_data["Publisher"]), only_ascii=False) or "")
    )

    dest_path = (
        CONFIG["COMIC_DEST_FOLDER"]
        .replace("$Issue", issueid)
        .replace("$Publisher", publisher)
        .replace("$Title", comic_name)
    )

    if CONFIG.get_bool("COMIC_RELATIVE"):
        dest_dir = enforce_str(
            make_unicode(get_directory("eBook") or "")
        )  # Enforce string for join
        dest_path = stripspaces(os.path.join(dest_dir, dest_path))

    # Validate encoding via make_utf8bytes, then decode to string for metadata
    dest_path = enforce_str(enforce_bytes(dest_path))

    global_name = sanitize(unaccented(f"{comic_name} {issueid}", only_ascii=False))

    return ComicMetadata(
        book_id=book_id,
        dest_path=dest_path,
        global_name=global_name,
        title=comic_name,
        comic_id=comicid,
        issue_id=issueid,
        issue_date=issue_data.get("IssueDate", issueid),
        issue_acquired=issue_data.get("IssueAcquired", ""),
        issue_file=issue_data.get("IssueFile", ""),
        cover=issue_data.get("Cover", ""),
        publisher=publisher,
        contributors=issue_data.get("Contributors", ""),
        most_recent_issue=mostrecentissue,
    )
