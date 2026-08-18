#!/usr/bin/env python
"""
Created on Apr 19, 2012
@author: dan, Faless

    GNU GENERAL PUBLIC LICENSE - Version 3

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

    http://www.gnu.org/licenses/gpl-3.0.txt

    modified by PAB for lazylibrarian...
    Added timeout to metadata download, warn about shutl.rmtree errors
    check if libtorrent available (it's architecture specific)
"""

import logging
import os
import shutil
import tempfile
from time import sleep

from lazylibrarian.config2 import CONFIG
from lazylibrarian.formatter import sanitize


# noinspection PyArgumentList
def magnet2torrent(magnet, output_name=None):
    logger = logging.getLogger(__name__)
    try:
        import libtorrent as lt
    except ImportError:
        try:
            # noinspection PyUnresolvedReferences
            from lib.libtorrent import libtorrent as lt
        except ImportError:
            logger.error("Unable to import libtorrent, disabling magnet conversion")
            CONFIG.set_bool('TOR_CONVERT_MAGNET', False)
            return False

    if output_name and \
            not os.path.isdir(output_name) and \
            not os.path.isdir(os.path.dirname(os.path.abspath(output_name))):
        logger.debug(f"Invalid output folder: {os.path.dirname(os.path.abspath(output_name))}")
        return False

    tempdir = tempfile.mkdtemp()

    ses = lt.session()
    params = {
        'url': magnet,
        'save_path': tempdir,
        'storage_mode': lt.storage_mode_t(0),
        # 'paused': False,
        # 'auto_managed': True,
        # 'duplicate_is_error': True
        'flags': 0x0e0,
    }
    # add_magnet_uri is deprecated
    # http://www.rasterbar.com/products/libtorrent/manual.html#add-magnet-uri
    # handle = lt.add_magnet_uri(ses, magnet, params)
    handle = ses.add_torrent(params)

    logger.debug("Downloading Metadata (this may take a while)")
    counter = 90
    while counter and not handle.has_metadata():
        try:
            sleep(1)
            counter -= 1
        except KeyboardInterrupt:
            counter = 0
    if not counter:
        logger.debug("magnet2Torrent Aborting...")
        ses.pause()
        logger.debug(f"Cleanup dir {tempdir}")
        try:
            shutil.rmtree(tempdir)
        except Exception as e:
            logger.error(f"{type(e).__name__} removing directory: {str(e)}")
        return False
    ses.pause()

    torinfo = handle.get_torrent_info()
    # noinspection PyUnresolvedReferences
    torfile = lt.create_torrent(torinfo)
    # noinspection PyUnresolvedReferences
    torcontent = lt.bencode(torfile.generate())
    ses.remove_torrent(handle)
    sanitized_name = sanitize(torinfo.name(), is_folder_or_file=True)
    output = os.path.abspath(f"{sanitized_name}.torrent")
    if output_name:
        if os.path.isdir(output_name):
            output = os.path.abspath(os.path.join(
                output_name, f"{sanitized_name}.torrent"))
        elif os.path.isdir(os.path.dirname(os.path.abspath(output_name))):
            output = os.path.abspath(output_name)

    logger.debug(f"Saving torrent file here : {output} ...")
    with open(output, 'wb') as f:
        f.write(torcontent)
    logger.debug(f"Saved! Cleaning up dir: {tempdir}")
    try:
        shutil.rmtree(tempdir)
    except Exception as e:
        logger.error(f"{type(e).__name__} removing directory: {str(e)}")
    return output
