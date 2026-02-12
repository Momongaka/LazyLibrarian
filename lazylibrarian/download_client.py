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
from lazylibrarian.telemetry import TELEMETRY


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

            if not rejected and banlist:
                wordlist = get_list(
                    fname.lower().replace(os.sep, " ").replace(".", " ")
                )
                for word in wordlist:
                    if word in banlist:
                        rejected = f"{fname} contains {word}"
                        logger.warning(f"{rejected}. Rejecting download")
                        break

            # only check size on right types of file
            # e.g. don't reject cos jpg is smaller than min file size for a book
            # need to check if we have a size in K M G or just a number. If K M G could be a float.
            unit = ""
            if not rejected and filetypes and extn in filetypes and fsize:
                try:
                    if "G" in str(fsize):
                        fsize = int(float(fsize.split("G")[0].strip()) * 1073741824)
                    elif "M" in str(fsize):
                        fsize = int(float(fsize.split("M")[0].strip()) * 1048576)
                    elif "K" in str(fsize):
                        fsize = int(float(fsize.split("K")[0].strip() * 1024))
                    fsize = round(
                        check_int(fsize, 0) / 1048576.0, 2
                    )  # float to 2dp in Mb
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
                    matched = True
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
    db = database.DBConnection()
    # noinspection PyBroadException
    try:
        if source == "TRANSMISSION":
            progress, errorstring, finished = transmission.get_torrent_progress(
                downloadid
            )
            if errorstring:
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, (errorstring, downloadid, source))
                progress = -1

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
                _ = sabnzbd.check_link()
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
                progress = 0
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
                    progress = 0
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
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, (errorstring, downloadid, source))
                progress = -1

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
            if progress == -1:
                logger.debug(f"{downloadid} not found at {source}")
                # Keep progress as -1 to signal "not found" rather than "0% progress"
            if status == "error":
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, ("QBITTORRENT returned error", downloadid, source))
                progress = -1

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
            if progress == -1:
                logger.debug(f"{downloadid} not found at {source}")
                # Keep progress as -1 to signal "not found" rather than "0% progress"
            if status == "finished":
                progress = 100
                finished = True
            elif status == "error":
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, ("rTorrent returned error", downloadid, source))
                progress = -1

        elif source.startswith("SYNOLOGY"):
            progress, status, finished = synology.get_progress(downloadid)
            if status == "finished":
                progress = 100
            elif status == "error":
                cmd = "UPDATE wanted SET Status='Aborted',DLResult=? WHERE DownloadID=? and Source=?"
                db.action(cmd, ("Synology returned error", downloadid, source))
                progress = -1

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


def delete_task(source, download_id, remove_data):
    logger = logging.getLogger(__name__)
    try:
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
            qbittorrent.remove_torrent(download_id, remove_data)
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
