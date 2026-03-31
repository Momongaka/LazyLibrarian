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
Archive extraction utilities.

Handles extraction of zip, tar, and rar archives, including multipart archive assembly.
Supports various archive formats commonly used for ebook/magazine distribution.
"""

import logging
import os
import shutil
import tarfile
import traceback
import zipfile

import lazylibrarian
from lazylibrarian import RARFILE
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import listdir, make_dirs, path_isfile, syspath
from lazylibrarian.formatter import is_valid_type, make_unicode
from lazylibrarian.postprocess_utils import enforce_str
from lazylibrarian.telemetry import TELEMETRY


def unpack_multipart(source_dir, download_dir, title):
    """
    Unpack multipart zip/rar files into one directory.

    Some magazines are packaged as multiple zip files, each containing a rar file.
    This function extracts all zips, then combines the rar files into the final content.

    Args:
        source_dir (str): Directory containing multipart archives.
        download_dir (str): Parent download directory.
        title (str): Title for naming the extraction directory.

    Returns:
        str: Path to extraction directory with unpacked content, or empty string on failure.
    """
    logger = logging.getLogger(__name__)
    TELEMETRY.record_usage_data("Process/MultiPart")
    # noinspection PyBroadException
    try:
        targetdir = os.path.join(download_dir, f"{title}.unpack")
        if not make_dirs(targetdir, new=True):
            logger.error(f"Failed to create target dir {targetdir}")
            return ""
        for f in listdir(source_dir):
            archivename = os.path.join(source_dir, str(f))
            xtn = os.path.splitext(archivename)[1].lower()
            if xtn not in [".epub", ".cbz"] and zipfile.is_zipfile(archivename):
                try:
                    z = zipfile.ZipFile(archivename)
                    for item in z.namelist():
                        if not item.endswith("/"):
                            # not if it's a directory
                            logger.debug(f"Extracting {item} to {targetdir}")
                            if os.path.__name__ == "ntpath":
                                dst = os.path.join(targetdir, item.replace("/", "\\"))
                            else:
                                dst = os.path.join(targetdir, item)
                            with open(syspath(dst), "wb") as d:
                                d.write(z.read(item))
                except Exception as e:
                    logger.error(f"Failed to unzip {archivename}: {e}")
                    return ""
        for f in listdir(targetdir):
            f = str(f)
            if f.endswith(".rar"):
                resultdir = unpack_archive(
                    os.path.join(targetdir, f), targetdir, title, targetdir=targetdir
                )
                if resultdir != targetdir:
                    for d in listdir(resultdir):
                        d = str(d)
                        shutil.move(
                            os.path.join(resultdir, d), os.path.join(targetdir, d)
                        )
                break
        return targetdir
    except Exception:
        logger.error(
            f"Unhandled exception in unpack_multipart: {traceback.format_exc()}"
        )
        return ""


def unpack_archive(archivename, download_dir, title, targetdir=""):
    """
    Extract an archive file (zip, tar, or rar) containing books/magazines.

    Checks if the archive contains valid book files and extracts them to a new directory.
    Supports zip, tar, and rar formats (rar support depends on available libraries).

    Args:
        archivename (str): Path to the archive file to extract.
        download_dir (str): Parent download directory.
        title (str): Title for naming the extraction directory.
        targetdir (str, optional): Specific target directory, or auto-generate if empty.

    Returns:
        str: Path to extraction directory with unpacked content, or empty string if not an archive
             or extraction failed.
    """
    logger = logging.getLogger(__name__)
    postprocesslogger = logging.getLogger("special.postprocess")
    archivename = enforce_str(make_unicode(archivename))
    if not path_isfile(archivename):  # regular files only
        return ""

    # noinspection PyBroadException
    try:
        xtn = os.path.splitext(archivename)[1].lower()
        if xtn not in [".epub", ".cbz"] and zipfile.is_zipfile(archivename):
            TELEMETRY.record_usage_data("Process/Archive/Zip")
            postprocesslogger.debug(f"{archivename} is a zip file")
            try:
                z = zipfile.ZipFile(archivename)
            except Exception as e:
                logger.error(f"Failed to unzip {archivename}: {e}")
                return ""
            if not targetdir:
                targetdir = os.path.join(download_dir, f"{title}.unpack")
            if not make_dirs(targetdir, new=True):
                logger.error(f"Failed to create target dir {targetdir}")
                return ""

            logger.debug(f"Created target {targetdir}")
            # Look for any wanted files (inc jpg for cbr/cbz)
            for item in z.namelist():
                if is_valid_type(
                    item, extensions=CONFIG.get_all_types_list()
                ) and not item.endswith("/"):
                    # not if it's a directory
                    logger.debug(f"Extracting {item} to {targetdir}")
                    if os.path.__name__ == "ntpath":
                        dst = os.path.join(targetdir, item.replace("/", "\\"))
                    else:
                        dst = os.path.join(targetdir, item)
                    dstdir = os.path.dirname(dst)
                    if not make_dirs(dstdir):
                        logger.error(f"Failed to create directory {dstdir}")
                        return ""
                    try:
                        with open(syspath(dst), "wb") as f:
                            f.write(z.read(item))
                    except Exception as e:
                        logger.error(f"Unable to extract {item} from {archivename}: {e}")
                        continue

        elif tarfile.is_tarfile(archivename):
            TELEMETRY.record_usage_data("Process/Archive/Tar")
            postprocesslogger.debug(f"{archivename} is a tar file")
            try:
                z = tarfile.TarFile(archivename)
            except Exception as e:
                logger.error(f"Failed to untar {archivename}: {e}")
                return ""

            targetdir = os.path.join(download_dir, f"{title}.unpack")
            if not make_dirs(targetdir, new=True):
                logger.error(f"Failed to create target dir {targetdir}")
                return ""

            logger.debug(f"Created target {targetdir}")
            for item in z.getnames():
                if is_valid_type(
                    item, extensions=CONFIG.get_all_types_list()
                ) and not item.endswith("/"):
                    # not if it's a directory
                    logger.debug(f"Extracting {item} to {targetdir}")
                    if os.path.__name__ == "ntpath":
                        dst = os.path.join(targetdir, item.replace("/", "\\"))
                    else:
                        dst = os.path.join(targetdir, item)
                    dstdir = os.path.dirname(dst)
                    if not make_dirs(dstdir):
                        logger.error(f"Failed to create directory {dstdir}")
                        return ""
                    with open(syspath(dst), "wb") as f:
                        f.write(z.extractfile(item).read())

        elif lazylibrarian.UNRARLIB == 1 and RARFILE.is_rarfile(archivename):
            TELEMETRY.record_usage_data("Process/Archive/RarOne")
            postprocesslogger.debug(f"{archivename} is a rar file")
            try:
                z = RARFILE.RarFile(archivename)
            except Exception as e:
                logger.error(f"Failed to unrar {archivename}: {e}")
                return ""

            targetdir = os.path.join(download_dir, f"{title}.unpack")
            if not make_dirs(targetdir, new=True):
                logger.error(f"Failed to create target dir {targetdir}")
                return ""

            logger.debug(f"Created target {targetdir}")
            for item in z.namelist():
                if is_valid_type(
                    item, extensions=CONFIG.get_all_types_list()
                ) and not item.endswith("/"):
                    # not if it's a directory
                    logger.debug(f"Extracting {item} to {targetdir}")
                    if os.path.__name__ == "ntpath":
                        dst = os.path.join(targetdir, item.replace("/", "\\"))
                    else:
                        dst = os.path.join(targetdir, item)
                    dstdir = os.path.dirname(dst)
                    if not make_dirs(dstdir):
                        logger.error(f"Failed to create directory {dstdir}")
                        return ""
                    with open(syspath(dst), "wb") as f:
                        f.write(z.read(item))

        elif lazylibrarian.UNRARLIB == 2:
            # noinspection PyBroadException
            try:
                z = lazylibrarian.RARFILE(archivename)
                postprocesslogger.debug(f"{archivename} is a rar file")
                TELEMETRY.record_usage_data("Process/Archive/RarTwo")
            except Exception as e:
                if archivename.endswith(".rar"):
                    logger.debug(str(e))
                z = None  # not a rar archive

            if z:
                targetdir = os.path.join(download_dir, f"{title}.unpack")
                if not make_dirs(targetdir, new=True):
                    logger.error(f"Failed to create target dir {targetdir}")
                    return ""

                logger.debug(f"Created target {targetdir}")
                wanted_files = []
                for item in z.infoiter():
                    if not item.isdir and is_valid_type(
                        item.filename, extensions=CONFIG.get_all_types_list()
                    ):
                        wanted_files.append(item.filename)

                data = z.read_files("*")
                for entry in data:
                    for item in wanted_files:
                        if entry[0].filename.endswith(item):
                            logger.debug(f"Extracting {item} to {targetdir}")
                            if os.path.__name__ == "ntpath":
                                dst = os.path.join(targetdir, item.replace("/", "\\"))
                            else:
                                dst = os.path.join(targetdir, item)
                            dstdir = os.path.dirname(dst)
                            if not make_dirs(dstdir):
                                logger.error(f"Failed to create directory {dstdir}")
                            else:
                                with open(syspath(dst), "wb") as f:
                                    f.write(entry[1])
                            break
        if not targetdir:
            postprocesslogger.debug(
                f"[{archivename}] doesn't look like an archive we can unpack"
            )
            return ""

        return targetdir

    except Exception:
        logger.error(
            f"Unhandled exception in unpack_archive: {traceback.format_exc()}"
        )
        return ""
