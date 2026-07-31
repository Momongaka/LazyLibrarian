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
Download Client Abstraction Module

This module provides a unified interface for interacting with various download clients
(both torrent and usenet) used by LazyLibrarian. It abstracts the differences between
clients to provide consistent functionality for:

- Checking download contents and validating files
- Retrieving download names, file lists, and folder locations
- Monitoring download progress
- Deleting completed or failed tasks

Supported Download Clients:
- Torrent: Transmission, qBittorrent, uTorrent, rTorrent, Deluge (WebUI/RPC), Synology
- Usenet: SABnzbd, NZBGet
- Direct: DIRECT and IRC downloads

Key Functions:
- check_contents: Validates download contents against rejection criteria
- get_download_progress: Monitors download progress and completion status
- delete_task: Removes tasks from download clients
- get_download_name: Retrieves the display name from a download client
- get_download_files: Gets the file list from a download client
- get_download_folder: Gets the download folder path from a download client
"""

import logging
import os
import re
import time
import traceback

from deluge_client import DelugeRPCClient

import lazylibrarian
from lazylibrarian import (
    database,
    deluge,
    nzbget,
    qbittorrent,
    rtorrent,
    sabnzbd,
    synology,
    transmission,
    utorrent,
)
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import path_isfile
from lazylibrarian.formatter import check_int, get_list, unaccented
from lazylibrarian.processcontrol import get_info_on_caller
from lazylibrarian.telemetry import TELEMETRY

# A release that ships its content inside an archive says nothing about the
# format until it is unpacked, so we cannot judge it from the file list.
ARCHIVE_EXTENSIONS = ('7z', 'bz2', 'gz', 'rar', 'tar', 'tgz', 'xz', 'zip')
# multipart archives: name.r00, name.z01, name.7z.001
ARCHIVE_PART_RE = re.compile(r'^(r\d{2}|z\d{2}|\d{3})$')


def is_archive_file(fname):
    """Return True if a file needs unpacking before we know what is in it."""
    extn = os.path.splitext(fname)[1].lstrip(".").lower()
    if not extn:
        return False
    return extn in ARCHIVE_EXTENSIONS or bool(ARCHIVE_PART_RE.match(extn))


def check_contents(source, downloadid, booktype, title):
    """Check contents list of a download against various reject criteria
    name, size, filetype, banned words
    Return empty string if ok, or error message if rejected
    Error message gets logged and then passed back to history table
    """
    logger = logging.getLogger(__name__)
    rejected = ""
    matched = False
    banned_extensions = get_list(CONFIG["BANNED_EXT"])
    if booktype.lower() == "ebook":
        maxsize = CONFIG.get_int("REJECT_MAXSIZE")
        minsize = CONFIG.get_int("REJECT_MINSIZE")
        filetypes = CONFIG["EBOOK_TYPE"]
        banwords = CONFIG["REJECT_WORDS"]
    elif booktype.lower() == "audiobook":
        maxsize = CONFIG.get_int("REJECT_MAXAUDIO")
        # minsize = lazylibrarian.CONFIG['REJECT_MINAUDIO']
        minsize = 0  # individual audiobook chapters can be quite small
        filetypes = CONFIG["AUDIOBOOK_TYPE"]
        banwords = CONFIG["REJECT_AUDIO"]
    elif booktype.lower() == "magazine":
        maxsize = CONFIG.get_int("REJECT_MAGSIZE")
        minsize = CONFIG.get_int("REJECT_MAGMIN")
        filetypes = CONFIG["MAG_TYPE"]
        banwords = CONFIG["REJECT_MAGS"]
    else:  # comics
        maxsize = CONFIG.get_int("REJECT_MAXCOMIC")
        minsize = CONFIG.get_int("REJECT_MINCOMIC")
        filetypes = CONFIG["COMIC_TYPE"]
        banwords = CONFIG["REJECT_COMIC"]

    if banwords:
        banlist = get_list(banwords, ",")
    else:
        banlist = []

    wanted_types = get_list(filetypes)
    unknown_contents = False  # an archive we cannot see inside
    seen_extensions = []
    downloadfiles = get_download_files(source, downloadid)

    # Downloaders return varying amounts of info using varying names
    if not downloadfiles:  # empty
        if source not in [
            "DIRECT",
            "NZBGET",
            "SABNZBD",
        ]:  # these don't give us a contents list
            logger.debug(f"No filenames returned by {source} for {title}")
    else:
        logger.debug(f"Checking files in {title}")
        for entry in downloadfiles:
            fname = ""
            fsize = 0
            if "path" in entry:  # deluge, rtorrent
                fname = entry["path"]
            if "name" in entry:  # transmission, qbittorrent
                fname = entry["name"]
            if "filename" in entry:  # utorrent, synology
                fname = entry["filename"]
            if "size" in entry:  # deluge, qbittorrent, synology, rtorrent
                fsize = entry["size"]
            if "filesize" in entry:  # utorrent
                fsize = entry["filesize"]
            if "length" in entry:  # transmission
                fsize = entry["length"]
            extn = os.path.splitext(fname)[1].lstrip(".").lower()
            if extn and extn in banned_extensions:
                rejected = f"{title} extension {extn}"
                logger.warning(f"{rejected}. Rejecting download")
                break

            if extn and extn not in seen_extensions:
                seen_extensions.append(extn)
            is_wanted = extn in wanted_types
            is_archive = not is_wanted and is_archive_file(fname)
            if is_wanted:
                matched = True
            elif is_archive:
                unknown_contents = True

            # Reject words describe a release, so only the files that make it
            # the release we asked for get a say. A note or a cover that
            # mentions the audiobook edition doesn't make an epub an audiobook,
            # and an m4b sitting alongside an epub doesn't stop it being one.
            # An archive is named after the release and hides what is inside,
            # so it gets checked too.
            if not rejected and banlist and (is_wanted or is_archive):
                wordlist = get_list(fname.lower().replace("\\", " ").replace("/", " ").replace(".", " "))
                for word in wordlist:
                    if word in banlist:
                        rejected = f"{fname} contains {word}"
                        logger.warning(f"{rejected}. Rejecting download")
                        break

            # only check size on right types of file
            # e.g. don't reject cos jpg is smaller than min file size for a book
            # need to check if we have a size in K M G or just a number. If K M G could be a float.
            unit = ""
            if not rejected and is_wanted and fsize:
                try:
                    if "G" in str(fsize):
                        fsize = int(float(fsize.split("G")[0].strip()) * 1073741824)
                    elif "M" in str(fsize):
                        fsize = int(float(fsize.split("M")[0].strip()) * 1048576)
                    elif "K" in str(fsize):
                        fsize = int(float(fsize.split("K")[0].strip()) * 1024)
                    mb_size = check_int(fsize, 0) / 1048576.0
                    fsize = round(mb_size, 2)  # float to 2dp in Mb
                    if mb_size and not fsize:  # small file, don't round to zero
                        fsize = 0.01
                    unit = "Mb"
                except ValueError:
                    fsize = 0
                if fsize:
                    if maxsize and fsize > maxsize:
                        rejected = f"{fname} is too large ({fsize}{unit})"
                        logger.warning(f"{rejected}. Rejecting download")
                        break
                    if minsize and fsize < minsize:
                        rejected = f"{fname} is too small ({fsize}{unit})"
                        logger.warning(f"{rejected}. Rejecting download")
                        break
                if not rejected:
                    logger.debug(f"{fname}: ({fsize}{unit}) is wanted")

        if not rejected and not matched and wanted_types and not unknown_contents:
            # the downloader gave us a full file list and nothing in it is a
            # file we could process for this library. Catching it here keeps a
            # wrong format release out of the library instead of leaving it to
            # fail at postprocessing time.
            rejected = f"{title} has no {booktype} files"
            if seen_extensions:
                rejected = f"{rejected} ({', '.join(seen_extensions[:5])})"
            logger.warning(f"{rejected}. Rejecting download")
    if matched and not rejected:
        logger.debug(f"{title} accepted")
    else:
        logger.debug(f"{title}: {rejected}")
    return rejected


def get_download_name(title, source, downloadid):
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger("special.dlcomms")
    dlname = None
    try:
        logger.debug(f"{title} was sent to {source}")
        if source == "TRANSMISSION":
            dlname = transmission.get_torrent_name(downloadid)
        elif source == "QBITTORRENT":
            dlname = qbittorrent.get_name(downloadid)
        elif source == "UTORRENT":
            dlname = utorrent.name_torrent(downloadid)
        elif source == "RTORRENT":
            dlname = rtorrent.get_name(downloadid)
        elif source == "SYNOLOGY_TOR":
            dlname = synology.get_name(downloadid)
        elif source == "DELUGEWEBUI":
            dlname = deluge.get_torrent_name(downloadid)
        elif source == "DELUGERPC":
            client = DelugeRPCClient(
                CONFIG["DELUGE_HOST"],
                int(CONFIG["DELUGE_PORT"]),
                CONFIG["DELUGE_USER"],
                CONFIG["DELUGE_PASS"],
                decode_utf8=True,
            )
            try:
                client.connect()
                result = client.call("core.get_torrent_status", downloadid, {})
                dlcommslogger.debug(f"Deluge RPC Status [{str(result)}]")
                if "name" in result:
                    dlname = unaccented(result["name"], only_ascii=False)
            except Exception as e:
                logger.error(f"DelugeRPC failed {type(e).__name__} {str(e)}")
        elif source == "SABNZBD":
            data = {}
            if not lazylibrarian.SAB_VER[0]:
                _ = sabnzbd.check_link()
            if lazylibrarian.SAB_VER > (3, 2, 0):
                # we can filter on nzo_ids
                res, _ = sabnzbd.sab_nzbd(nzburl="queue", nzo_ids=downloadid)
            else:
                db = database.DBConnection()
                try:
                    cmd = "SELECT * from wanted WHERE DownloadID=? and Source=?"
                    data = db.match(cmd, (downloadid, source))
                finally:
                    db.close()
                if data and data["NZBtitle"]:
                    res, _ = sabnzbd.sab_nzbd(nzburl="queue", search=data["NZBtitle"])
                else:
                    res, _ = sabnzbd.sab_nzbd(nzburl="queue")

            if res and "queue" in res:
                logger.debug(
                    f"SAB queue returned {len(res['queue']['slots'])} for {downloadid}"
                )
                for item in res["queue"]["slots"]:
                    if item["nzo_id"] == downloadid:
                        dlname = item["filename"]
                        break

            if not dlname:  # not in queue, try history in case completed or error
                if lazylibrarian.SAB_VER > (3, 2, 0):
                    res, _ = sabnzbd.sab_nzbd(nzburl="history", nzo_ids=downloadid)
                elif data and data["NZBtitle"]:
                    res, _ = sabnzbd.sab_nzbd(nzburl="history", search=data["NZBtitle"])
                else:
                    res, _ = sabnzbd.sab_nzbd(nzburl="history")

                if res and "history" in res:
                    logger.debug(
                        f"SAB history returned {len(res['history']['slots'])} for {downloadid}"
                    )
                    for item in res["history"]["slots"]:
                        if item["nzo_id"] == downloadid:
                            dlname = item["name"]
                            break
        return dlname

    except Exception as e:
        logger.error(
            f"Failed to get filename from {source} for {downloadid}: {type(e).__name__} {str(e)}"
        )
        return None


def get_download_files(source, downloadid):
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger("special.dlcomms")
    dlfiles = None
    TELEMETRY.record_usage_data("Get/DownloadFiles")
    try:
        if source == "TRANSMISSION":
            dlfiles = transmission.get_torrent_files(downloadid)
        elif source == "UTORRENT":
            dlfiles = utorrent.list_torrent(downloadid)
        elif source == "RTORRENT":
            dlfiles = rtorrent.get_files(downloadid)
        elif source == "SYNOLOGY_TOR":
            dlfiles = synology.get_files(downloadid)
        elif source == "QBITTORRENT":
            dlfiles = qbittorrent.get_files(downloadid)
        elif source == "DELUGEWEBUI":
            dlfiles = deluge.get_torrent_files(downloadid)
        elif source == "DELUGERPC":
            client = DelugeRPCClient(
                CONFIG["DELUGE_HOST"],
                int(CONFIG["DELUGE_PORT"]),
                CONFIG["DELUGE_USER"],
                CONFIG["DELUGE_PASS"],
                decode_utf8=True,
            )
            try:
                client.connect()
                result = client.call("core.get_torrent_status", downloadid, {})
                dlcommslogger.debug(f"Deluge RPC Status [{str(result)}]")
                if "files" in result:
                    dlfiles = result["files"]
            except Exception as e:
                logger.error(f"DelugeRPC failed {type(e).__name__} {str(e)}")
        else:
            dlcommslogger.debug(
                f"Unable to get file list from {source} (not implemented)"
            )
        return dlfiles

    except Exception as e:
        logger.error(
            f"Failed to get list of files from {source} for {downloadid}: {type(e).__name__} {str(e)}"
        )
        return None


def get_download_folder(source, downloadid):
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger("special.dlcomms")
    dlfolder = None
    # noinspection PyBroadException
    TELEMETRY.record_usage_data("Get/DownloadFolder")
    # noinspection PyBroadException
    try:
        if source == "TRANSMISSION":
            dlfolder = transmission.get_torrent_folder(downloadid)
        elif source == "UTORRENT":
            dlfolder = utorrent.dir_torrent(downloadid)
        elif source == "RTORRENT":
            dlfolder = rtorrent.get_folder(downloadid)
        elif source == "SYNOLOGY_TOR":
            dlfolder = synology.get_folder(downloadid)
        elif source == "QBITTORRENT":
            dlfolder = qbittorrent.get_folder(downloadid)
        elif source == "DELUGEWEBUI":
            dlfolder = deluge.get_torrent_folder(downloadid)
        elif source == "DELUGERPC":
            client = DelugeRPCClient(
                CONFIG["DELUGE_HOST"],
                int(CONFIG["DELUGE_PORT"]),
                CONFIG["DELUGE_USER"],
                CONFIG["DELUGE_PASS"],
                decode_utf8=True,
            )
            try:
                client.connect()
                result = client.call("core.get_torrent_status", downloadid, {})
                dlcommslogger.debug(f"Deluge RPC Status [{str(result)}]")
                if "save_path" in result:
                    dlfolder = result["save_path"]
            except Exception as e:
                logger.error(f"DelugeRPC failed {type(e).__name__} {str(e)}")

        elif source == "SABNZBD":
            data = {}
            if not lazylibrarian.SAB_VER[0]:
                _ = sabnzbd.check_link()
            if lazylibrarian.SAB_VER > (3, 2, 0):
                # we can filter on nzo_ids
                res, _ = sabnzbd.sab_nzbd(nzburl="queue", nzo_ids=downloadid)
            else:
                db = database.DBConnection()
                try:
                    cmd = "SELECT * from wanted WHERE DownloadID=? and Source=?"
                    data = db.match(cmd, (downloadid, source))
                finally:
                    db.close()
                if data and data["NZBtitle"]:
                    res, _ = sabnzbd.sab_nzbd(nzburl="queue", search=data["NZBtitle"])
                else:
                    res, _ = sabnzbd.sab_nzbd(nzburl="queue")
            if res and "queue" in res:
                logger.debug(
                    f"SAB queue returned {len(res['queue']['slots'])} for {downloadid}"
                )
                for item in res["queue"]["slots"]:
                    if item["nzo_id"] == downloadid:
                        dlfolder = None  # still in queue, not unpacked
                        break
            if not dlfolder:  # not in queue, try history
                if lazylibrarian.SAB_VER > (3, 2, 0):
                    res, _ = sabnzbd.sab_nzbd(nzburl="history", nzo_ids=downloadid)
                elif data and data["NZBtitle"]:
                    res, _ = sabnzbd.sab_nzbd(nzburl="history", search=data["NZBtitle"])
                else:
                    res, _ = sabnzbd.sab_nzbd(nzburl="history")

                if res and "history" in res:
                    logger.debug(
                        f"SAB history returned {len(res['history']['slots'])} for {downloadid}"
                    )
                    for item in res["history"]["slots"]:
                        if item["nzo_id"] == downloadid:
                            dlfolder = item.get("storage")
                            # SABnzbd's storage field can be either a directory or a file path
                            # If it's a file, we need the parent directory
                            if dlfolder and path_isfile(dlfolder):
                                dlfolder = os.path.dirname(dlfolder)
                            break

        elif source == "NZBGET":
            res, _ = nzbget.send_nzb(cmd="listgroups")
            dlcommslogger.debug(str(res))
            if res:
                for item in res:
                    if item["NZBID"] == check_int(downloadid, 0):
                        dlfolder = item.get("DestDir")
                        break
            if not dlfolder:  # not in queue, try history
                res, _ = nzbget.send_nzb(cmd="history")
                dlcommslogger.debug(str(res))
                if res:
                    for item in res:
                        if item["NZBID"] == check_int(downloadid, 0):
                            dlfolder = item.get("DestDir")
                            break

        # Remote to local mapping for docker containers
        # Convert source to match config keys
        if source.startswith('DELUGE'):
            source = 'DELUGE'
        elif source.startswith('SYNOLOGY'):
            source = 'SYNOLOGY'
        elif source == 'SABNZBD':
            source = 'SAB'

        if (source in ['SAB', 'NZBGET', 'RTORRENT', 'UTORRENT', 'QBITTORRENT', 'TRANSMISSION', 'DELUGE', 'SYNOLOGY', 'SLSK'] and
            CONFIG[f"{source}_REMOTE"] and CONFIG[f"{source}_LOCAL"] and dlfolder.startswith(CONFIG[f"{source}_REMOTE"])):
                logger.debug(f"Replacing {CONFIG[f'{source}_REMOTE']} with {CONFIG[f'{source}_LOCAL']}")
                dlfolder = dlfolder.replace(CONFIG[f"{source}_REMOTE"], CONFIG[f"{source}_LOCAL"])

        return dlfolder

    except Exception:
        logger.warning(f"Failed to get folder from {source} for {downloadid}")
        logger.error(
            f"Unhandled exception in get_download_folder: {traceback.format_exc()}"
        )
        return None


def get_download_progress(source, downloadid):
    logger = logging.getLogger(__name__)
    dlcommslogger = logging.getLogger("special.dlcomms")
    progress = 0
    finished = False
    if not source or not downloadid:
        program, method, lineno = get_info_on_caller(depth=1)
        logger.error(
            f"Unable to get download progress from {source} for {downloadid}: {program}:{method}:{lineno}"
        )
        return progress, finished
    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        if source == "TRANSMISSION":
            progress, errorstring, finished = transmission.get_torrent_progress(
                downloadid
            )
            if errorstring and progress == -1:
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, (errorstring, downloadid, source))

        elif source == "DIRECT" or str(source).startswith("IRC"):
            cmd = "SELECT * from wanted WHERE DownloadID=? and Source=?"
            data = db.match(cmd, (downloadid, source))
            if data:
                progress = 100
                finished = True
            else:
                progress = 0

        elif source == "SABNZBD":
            data = {}
            if not lazylibrarian.SAB_VER[0]:
                res = sabnzbd.check_link()
                if 'successful' not in res:
                    progress = -2  # connection error
            if lazylibrarian.SAB_VER > (3, 2, 0):
                res, _ = sabnzbd.sab_nzbd(nzburl="queue", nzo_ids=downloadid)
            else:
                cmd = "SELECT * from wanted WHERE DownloadID=? and Source=?"
                data = db.match(cmd, (downloadid, source))
                if data and data["NZBtitle"]:
                    res, _ = sabnzbd.sab_nzbd(nzburl="queue", search=data["NZBtitle"])
                else:
                    res, _ = sabnzbd.sab_nzbd(nzburl="queue")

            found = False
            if not res or "queue" not in res:
                progress = -2
            else:
                logger.debug(
                    f"SAB queue returned {len(res['queue']['slots'])} for {downloadid}"
                )
                for item in res["queue"]["slots"]:
                    if item["nzo_id"] == downloadid:
                        found = True
                        progress = item["percentage"]
                        break
            if not found:  # not in queue, try history in case completed or error
                if lazylibrarian.SAB_VER > (3, 2, 0):
                    res, _ = sabnzbd.sab_nzbd(nzburl="history", nzo_ids=downloadid)
                elif data and data["NZBtitle"]:
                    res, _ = sabnzbd.sab_nzbd(nzburl="history", search=data["NZBtitle"])
                else:
                    res, _ = sabnzbd.sab_nzbd(nzburl="history")

                if not res or "history" not in res:
                    progress = -2
                else:
                    logger.debug(
                        f"SAB history returned {len(res['history']['slots'])} for {downloadid}"
                    )
                    for item in res["history"]["slots"]:
                        if item["nzo_id"] == downloadid:
                            found = True
                            # 100% if completed, 99% if still extracting or repairing, -1 if not found or failed
                            if (
                                item["status"] == "Completed"
                                and not item["fail_message"]
                            ):
                                progress = 100
                                finished = True
                            elif item["status"] in ["Extracting", "Fetching"]:
                                progress = 99
                            elif item["status"] == "Failed" or item["fail_message"]:
                                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                                db.action(
                                    cmd, (item["fail_message"], downloadid, source)
                                )
                                progress = -1
                            break
            if not found:
                errorstring = f"{downloadid} not found at {source}"
                logger.debug(errorstring)
                if progress == -1:
                    cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                    db.action(cmd, (errorstring, downloadid, source))

        elif source == "NZBGET":
            res, _ = nzbget.send_nzb(cmd="listgroups")
            dlcommslogger.debug(str(res))
            found = False
            if res:
                for item in res:
                    # nzbget NZBIDs are integers
                    if item["NZBID"] == check_int(downloadid, 0):
                        found = True
                        logger.debug(f"NZBID {item['NZBID']} status {item['Status']}")
                        total = item["FileSizeHi"] << 32 + item["FileSizeLo"]
                        if total:
                            remaining = (
                                item["RemainingSizeHi"] << 32 + item["RemainingSizeLo"]
                            )
                            done = total - remaining
                            progress = int(done * 100 / total)
                            if progress == 100:
                                finished = True
                        break
            if not found:  # not in queue, try history in case completed or error
                res, _ = nzbget.send_nzb(cmd="history")
                dlcommslogger.debug(str(res))
                if res:
                    for item in res:
                        if item["NZBID"] == check_int(downloadid, 0):
                            found = True
                            logger.debug(
                                f"NZBID {item['NZBID']} status {item['Status']}"
                            )
                            # 100% if completed, -1 if not found or failed
                            if "SUCCESS" in item["Status"]:
                                progress = 100
                                finished = True
                            elif (
                                "WARNING" in item["Status"]
                                or "FAILURE" in item["Status"]
                            ):
                                cmd = (
                                    "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? "
                                    "and Source=?"
                                )
                                db.action(cmd, (item["Status"], downloadid, source))
                                progress = -1
                            break
            if not found:
                errorstring = f"{downloadid} not found at {source}"
                logger.debug(errorstring)
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, (errorstring, downloadid, source))
                progress = -1

        elif source == "QBITTORRENT":
            progress, status, finished = qbittorrent.get_progress(downloadid)
            # progress -2 is a communication error which we can retry on next run
            if progress < 0:
                msg = f"{downloadid} not found at {source}"
                if status:
                    msg += f" Status: {status} Progress: {progress}"
                logger.debug(msg)
            if status == "error" or progress == -1:
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, (f"QBITTORRENT returned {status}:{progress}", downloadid, source))

        elif source == "UTORRENT":
            progress, status, finished = utorrent.progress_torrent(downloadid)
            if progress == -1:
                logger.debug(f"{downloadid} not found at {source}")
                # Keep progress as -1 to signal "not found" rather than "0% progress"
            if status & 16:  # Error
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(
                    cmd,
                    (f"UTORRENT returned error status {status}", downloadid, source),
                )
                progress = -1

        elif source == "RTORRENT":
            progress, status = rtorrent.get_progress(downloadid)
            if progress < 0:
                logger.debug(f"{downloadid} not found at {source}")
            elif status == "finished":
                progress = 100
                finished = True
            if progress == -1:
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, (f"rTorrent returned {status}", downloadid, source))

        elif source and source.startswith("SYNOLOGY"):
            progress, status, finished = synology.get_progress(downloadid)
            if status == "finished":
                progress = 100
            elif progress == -1:
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, (f"Synology returned {status}", downloadid, source))

        elif source == "DELUGEWEBUI":
            progress, message, finished = deluge.get_torrent_progress(downloadid)
            if message and message != "OK":
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, (message, downloadid, source))
                progress = -1

        elif source == "DELUGERPC":
            client = DelugeRPCClient(
                CONFIG["DELUGE_HOST"],
                int(CONFIG["DELUGE_PORT"]),
                CONFIG["DELUGE_USER"],
                CONFIG["DELUGE_PASS"],
                decode_utf8=True,
            )
            try:
                client.connect()
                result = client.call("core.get_torrent_status", downloadid, {})
                dlcommslogger.debug(f"Deluge RPC Status [{str(result)}]")

                if "progress" in result:
                    progress = result["progress"]
                    try:
                        finished = (
                            result["is_auto_managed"]
                            and result["stop_at_ratio"]
                            and result["state"].lower() == "paused"
                            and result["ratio"] >= result["stop_ratio"]
                        )
                    except (KeyError, AttributeError):
                        finished = False
                else:
                    progress = -1
                    finished = False
                if "message" in result and result["message"] != "OK":
                    cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                    db.action(cmd, (result["message"], downloadid, source))
                    progress = -1
            except Exception as e:
                logger.error(f"DelugeRPC failed {type(e).__name__} {str(e)}")
                progress = 0

        else:
            dlcommslogger.debug(
                f"Unable to get progress from {source} (not implemented)"
            )
            progress = 0
        try:
            progress = int(progress)
        except ValueError:
            logger.debug(f"Progress value error {source} [{progress}] {downloadid}")
            progress = 0

        if finished:  # store when we noticed it was completed (can ask some downloaders, but not all)
            res = db.match(
                "SELECT Completed from wanted WHERE DownloadID=? and Source=?",
                (downloadid, source),
            )
            if res and not res["Completed"]:
                db.action(
                    "UPDATE wanted SET Completed=? WHERE DownloadID=? and Source=?",
                    (int(time.time()), downloadid, source),
                )
    except Exception:
        logger.warning(
            f"Failed to get download progress from {source} for {downloadid}"
        )
        logger.error(
            f"Unhandled exception in get_download_progress: {traceback.format_exc()}"
        )
        progress = 0
        finished = False

    db.close()
    return progress, finished


# Only a torrent can turn out to be someone else's already: it is found by
# infohash in a client that may hold anything. A usenet or direct download is
# created by the request that asked for it and by nothing else.
ADOPTABLE_SOURCES = ("QBITTORRENT", "TRANSMISSION", "UTORRENT", "RTORRENT",
                     "DELUGEWEBUI", "DELUGERPC", "SYNOLOGY_TOR")


def download_ownership(source, download_id):
    """What we know about a downloader task, as (owned, category).

    owned is True only where a record says we created the task ourselves.
    Anything else counts as not ours: a task we took on from the client, a row
    from before we recorded this, or no row at all. Removing a torrent someone
    else added takes their seed with it and cannot be undone, so an absent
    record has to mean no rather than yes.

    One torrent can serve an ebook and an audiobook request at once, so a single
    adopted row settles it for every row that shares the id.

    category is the downloader category recorded when the request was snatched,
    or None where we have no record of it. It is not proof of anything on its
    own, it is what the torrent's current category gets compared against.
    """
    if not download_id:
        return False, None
    db = database.DBConnection()
    try:
        entries = db.select(
            "SELECT Origin, Category from wanted WHERE DownloadID=? and Source=?",
            (download_id, source),
        )
    finally:
        db.close()
    if not entries:
        return False, None
    if any(entry["Origin"] != "new" for entry in entries):
        return False, None
    # A row that recorded no category does not get a vote on what the category
    # is. Rows written before the column existed have None here, and an install
    # with no label of its own records an empty string, which is a different
    # thing: one means we cannot check, the other means expect no category.
    categories = {entry["Category"] for entry in entries if entry["Category"] is not None}
    if len(categories) > 1:
        # two requests recorded different categories for one torrent, so we
        # cannot say which one it is filed under
        return False, None
    return True, categories.pop() if categories else None


def may_delete_data(source, download_id):
    """ Whether the files under a download are ours to delete.

    Ownership comes from our own record, and for qBittorrent the torrent also
    has to still be in the category we filed it under: someone can move a
    torrent we added into a category they are keeping, and the files under it
    then belong to that, not to us. Ask this before deleting anything, while
    the torrent is still there to be asked about.
    """
    if source not in ADOPTABLE_SOURCES:
        return True
    owned, category = download_ownership(source, download_id)
    if not owned:
        return False
    if source == "QBITTORRENT":
        if qbittorrent.category_matches(download_id, category):
            return True
        # said no, or could not be reached to answer. Either way the files stay,
        # so say so rather than leaving someone to wonder where they went.
        logging.getLogger(__name__).warning(
            f"Leaving the files for {download_id}: qBittorrent does not have it in "
            f"category [{category}], or could not be asked")
        return False
    return True


def seed_requirement(provider):
    """ What a provider asks us to seed to, as (ratio, minutes).

    Zero for either means the provider does not ask for it. Both the torznab
    and the rss provider groups can be a private tracker's, so both carry it.
    """
    if not provider:
        return 0, 0
    for group in ("TORZNAB", "RSS"):
        for item in CONFIG.providers(group):
            if provider in (item["NAME"], item["DISPNAME"], item["HOST"]):
                return (item.get_item("SEED_RATIO").value,
                        item.get_item("SEED_DURATION").value)
    return 0, 0


def seed_state(source, download_id):
    """ What a download has seeded so far, as (ratio, seconds), or None where the
    client cannot tell us. """
    if source == "QBITTORRENT":
        return qbittorrent.seed_state(download_id)
    if source == "TRANSMISSION":
        return transmission.seed_state(download_id)
    return None


def seeding_incomplete(source, download_id, providers):
    """ Say why a torrent still owes its tracker some seeding, or return ''.

    Setting a ratio or a time on the client only asks the client to stop at that
    point, it does not stop us removing the torrent before it gets there, which
    on a private tracker is how an account collects a hit and run. Nothing is
    enforced unless the provider asks for something, so this is inert until
    somebody fills the fields in.

    A client that cannot report what it has seeded is not held up: refusing
    forever on a client we cannot ask would leave downloads in place with no way
    for the user to see why.
    """
    # one torrent can serve two requests from different providers, so it owes
    # whatever the strictest of them asks for
    want_ratio = want_minutes = 0
    for provider in providers:
        ratio, minutes = seed_requirement(provider)
        want_ratio = max(want_ratio, ratio)
        want_minutes = max(want_minutes, minutes)
    if not want_ratio and not want_minutes:
        return ''
    state = seed_state(source, download_id)
    if state is None:
        return ''
    ratio, seconds = state
    # Either limit satisfies it. A tracker asking for both normally means one or
    # the other will do, and more to the point the clients stop seeding at the
    # first limit they reach, so waiting for both would hold a torrent forever
    # that the client has already stopped: the second limit would never arrive.
    if want_ratio and ratio >= want_ratio:
        return ''
    if want_minutes and seconds >= want_minutes * 60:
        return ''
    owing = []
    if want_ratio:
        owing.append(f"ratio {ratio:.2f} of {want_ratio:.2f}")
    if want_minutes:
        owing.append(f"{int(seconds / 60)} of {want_minutes} minutes seeded")
    return ', '.join(owing)


def download_providers(source, download_id):
    """ Every provider that asked for this download, as recorded when snatched. """
    if not download_id:
        return []
    db = database.DBConnection()
    try:
        entries = db.select(
            "SELECT NZBprov from wanted WHERE DownloadID=? and Source=?",
            (download_id, source),
        )
    finally:
        db.close()
    return [entry["NZBprov"] for entry in entries if entry["NZBprov"]]


def delete_task(source, download_id, remove_data):
    logger = logging.getLogger(__name__)
    owned, category = download_ownership(source, download_id)
    if source in ADOPTABLE_SOURCES and not owned:
        # Removing the torrent would stop someone else's seed and removing the
        # data would take files we never downloaded, so this needs a record
        # saying the task is ours before it goes anywhere near the client. Our
        # own request has already been marked failed or processed by the caller.
        logger.info(
            f"Not deleting {download_id} from {source}: nothing on record says we added it"
        )
        return True
    try:
        if source in ADOPTABLE_SOURCES:
            owing = seeding_incomplete(source, download_id,
                                       download_providers(source, download_id))
            if owing:
                logger.info(f"Not deleting {download_id} from {source} yet: {owing}")
                return False

        if source == "BLACKHOLE":
            logger.warning(
                f"Download {download_id} has not been processed from blackhole"
            )
        elif source == "SABNZBD":
            if CONFIG.get_bool("SAB_DELETE"):
                sabnzbd.sab_nzbd(download_id, "delete", remove_data)
                sabnzbd.sab_nzbd(download_id, "delhistory", remove_data)
        elif source == "NZBGET":
            nzbget.delete_nzb(download_id, remove_data)
        elif source == "UTORRENT":
            utorrent.remove_torrent(download_id, remove_data)
        elif source == "RTORRENT":
            rtorrent.remove_torrent(download_id, remove_data)
        elif source == "QBITTORRENT":
            # qBittorrent finds a torrent by hash whatever category it sits in,
            # so it gets the category we recorded to check against as well
            qbittorrent.remove_torrent(download_id, remove_data, expect_category=category)
        elif source == "TRANSMISSION":
            transmission.remove_torrent(download_id, remove_data)
        elif source.startswith("SYNOLOGY"):
            synology.remove_torrent(download_id, remove_data)
        elif source == "DELUGEWEBUI":
            deluge.remove_torrent(download_id, remove_data)
        elif source == "DELUGERPC":
            client = DelugeRPCClient(
                CONFIG["DELUGE_HOST"],
                int(CONFIG["DELUGE_PORT"]),
                CONFIG["DELUGE_USER"],
                CONFIG["DELUGE_PASS"],
                decode_utf8=True,
            )
            try:
                client.connect()
                client.call("core.remove_torrent", download_id, remove_data)
            except Exception as e:
                logger.error(f"DelugeRPC failed {type(e).__name__} {str(e)}")
        elif source == "DIRECT" or source.startswith("IRC"):
            return True
        else:
            logger.debug(f"Unknown source [{source}] in delete_task")
            return False
        return True

    except Exception as e:
        logger.warning(
            f"Failed to delete task {download_id} from {source}: {type(e).__name__} {str(e)}"
        )
        return False
