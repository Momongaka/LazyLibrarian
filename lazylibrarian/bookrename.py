#  This file is part of Lazylibrarian.
#  Lazylibrarian is free software, you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.


import logging
import os
import re
import shutil
import traceback

from rapidfuzz import fuzz

from lazylibrarian import database
from lazylibrarian.common import multibook, only_punctuation
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import (
    copy_tree,
    get_directory,
    listdir,
    opf_file,
    path_isdir,
    path_isfile,
    remove_file,
    safe_move,
    splitext,
    syspath,
)
from lazylibrarian.formatter import (
    check_int,
    get_list,
    make_unicode,
    plural,
    replacevars,
    sanitize,
    sort_definite,
    surname_first,
)
from lazylibrarian.opfedit import opf_read

try:
    from lib.tinytag import TinyTag
except ImportError:
    TinyTag = None


# noinspection PyUnusedLocal
def id3read(filename):
    logger = logging.getLogger(__name__)
    libsynclogger = logging.getLogger('special.libsync')
    mydict = {}
    if not TinyTag:
        logger.warning("TinyTag library not available")
        return mydict

    filename = syspath(filename)
    logger = logging.getLogger(__name__)
    # noinspection PyBroadException
    try:
        res = TinyTag.is_supported(filename)
    except Exception:
        res = False
    if not res:
        logger.warning(f"TinyTag:unsupported [{filename}]")
        return mydict

    try:
        id3r = TinyTag.get(filename)
    except Exception as e:
        logger.debug(str(e))
        return mydict

    try:
        artist = id3r.artist
        composer = id3r.composer
        album = id3r.album
        albumartist = id3r.albumartist
        title = id3r.title
        track = id3r.track
        track_total = id3r.track_total
        comment = id3r.comment

        if artist:
            artist = artist.strip().rstrip('\x00')
        else:
            artist = ''
        if composer:
            composer = composer.strip().rstrip('\x00')
        else:
            composer = ''
        if album:
            album = album.strip().rstrip('\x00')
        else:
            album = ''
        if albumartist:
            albumartist = albumartist.strip().rstrip('\x00')
        else:
            albumartist = ''

        if libsynclogger.isEnabledFor(logging.DEBUG):
            for tag in ['filename', 'artist', 'albumartist', 'composer', 'album', 'title', 'track',
                        'track_total', 'comment']:
                libsynclogger.debug(f"id3r.{tag} [{eval(tag)}]")

        if artist == 'None':
            artist = ''
        if albumartist == 'None':
            albumartist = ''
        if composer == 'None':
            composer = ''

        db = database.DBConnection()
        author = ''
        narrator = ''
        try:
            # Commonly used tags, eg plex:
            # ARTIST  Author or Author, Narrator
            # ALBUMARTIST     Author
            # COMPOSER    Narrator
            author = ''
            narrator = ''
            if albumartist:
                author = albumartist
                if composer:
                    narrator = composer
            elif len(artist.split(',')) == 2:
                author, narrator = artist.split(',')
            elif artist and composer and artist != composer:
                author = artist
                narrator = composer

            # finally override with any opf values found
            opffile = opf_file(os.path.dirname(filename))
            if os.path.exists(opffile):
                opf_template, replaces = opf_read(opffile)
                remove_file(opf_template)
                for item in replaces:
                    if item[0] == 'author':
                        author = item[1]
                    elif item[0] == 'narrator':
                        narrator = item[1]

            if not author:
                # if artist exists in library, probably author, though might get one author narrating anothers books?
                if artist and db.match("select * from authors where authorname=?", (artist,)):
                    author = artist
                elif albumartist and db.match("select * from authors where authorname=?", (albumartist,)):
                    author = albumartist
                elif composer and db.match("select * from authors where authorname=?", (composer,)):
                    author = composer
                elif artist:
                    author = artist
                elif albumartist:
                    author = albumartist
                elif composer:
                    author = composer
        except Exception as e:
            logger.debug(str(e))

        db.close()
        if author and isinstance(author, list):
            lst = ', '.join(author)
            logger.debug(f"id3reader author list [{lst}]")
            author = author[0]  # if multiple authors, just use the first one

        mydict['artist'] = artist
        mydict['composer'] = composer
        mydict['album'] = album
        mydict['albumartist'] = albumartist
        mydict['title'] = title
        mydict['track'] = track
        mydict['track_total'] = track_total
        mydict['comment'] = comment

        mydict['author'] = author
        mydict['narrator'] = narrator
        if not title:
            mydict['title'] = album

        for item in mydict:
            mydict[item] = make_unicode(mydict[item])

    except Exception:
        logger.error(f"tinytag error {traceback.format_exc()}")
    return mydict


def audio_parts(folder, bookname, authorname):
    logger = logging.getLogger(__name__)
    parts = []
    cnt = 0
    audio_file = ''
    abridged = ''
    tokmatch = ''
    total_parts = 0
    wholebook = ''
    partlist = []
    # there seem to be two common numbering systems x-y
    # for single-part audiobooks, x=part, y=total
    # for multi-part, x=chapter, y=part_in_chapter
    for f in listdir(folder):
        if CONFIG.is_valid_booktype(f, booktype='audiobook'):
            res = re.search(r'([0-9-]+\-[0-9]+)', f)
            if res and res.group():
                entry = res.group().split('-')
                chap = entry[0]
                prt = entry[-1]
                partlist.append([chap, prt, f])
                parts.append(prt)
                if not abridged and 'unabr' in f.lower():
                    # unabridged is sometimes shortened to unabr.
                    abridged = 'Unabridged'
                if not abridged and 'abridged' in f.lower():
                    abridged = 'Abridged'

    parts = list(set(parts))
    if len(parts) > 1:
        multi_chapter = True
        logger.debug(f"Multi chapter found {len(partlist)} parts")
    else:
        multi_chapter = False
    parts = []

    if not multi_chapter:
        # scan again using tags first
        for f in listdir(folder):
            if CONFIG.is_valid_booktype(f, booktype='audiobook'):
                # if no number_period or number_space in filename assume its whole-book
                if not re.findall(r'\d+\b', f):
                    wholebook = f
                else:
                    cnt += 1
                    audio_file = f

                    try:
                        audio_path = os.path.join(folder, audio_file)
                        id3r = id3read(audio_path)
                        # artist = id3r['artist']
                        # composer = id3r['composer']
                        # albumartist = id3r['albumartist']
                        book = id3r['album']
                        # title = id3r['title']
                        # comment = id3r['comment']
                        track = id3r['track']
                        total = id3r['track_total']
                        if track:
                            track = check_int(track.strip('\x00'), 0)
                        else:
                            track = 0
                        if total:
                            total = check_int(total.strip('\x00'), 0)
                        else:
                            total = 0

                        author = id3r['author']
                        if not book:
                            book = id3r['title']

                        if author and book and track != 0 and not (track == 1 and total == 1):
                            # ignore part0 and part1of1
                            parts.append([track, book, author, f])

                    except Exception as e:
                        logger.error(f"id3tag {type(e).__name__} {str(e)}")
        logger.debug(f"ID3read found {len(parts)}")
        total_parts = cnt

    failed = False
    if multi_chapter:
        chapters = []
        for part in partlist:
            chapters.append(int(part[0]))
        chapters = sorted(set(chapters))
        num_chapters = len(chapters)
        cnt = 1
        while cnt <= num_chapters:
            if cnt not in chapters:
                logger.warning(f"No chapter {cnt} found")
                failed = True
                return parts, failed, '', abridged
            cnt += 1

        cnt = 1
        total_parts = 0
        for chapter in chapters:
            chapterparts = []
            for part in partlist:
                if int(part[0]) == chapter:
                    chapterparts.append([int(part[1]), part[2]])
            num_chapterparts = len(chapterparts)
            chapterparts = sorted(chapterparts)
            total_parts += len(chapterparts)
            ccnt = 1
            while ccnt <= num_chapterparts:
                match = [x for x in chapterparts if x[0] == ccnt]
                if not match:
                    logger.warning(f"No chapter {chapter} part {ccnt} found")
                    failed = True
                    return parts, failed, '', abridged
                ccnt += 1

            for entry in chapterparts:
                parts.append([cnt, bookname, authorname, entry[1]])
                cnt += 1
        cnt -= 1

    if cnt < 2 and not parts:  # single file audiobook with number but no tags
        parts = [[1, bookname, authorname, audio_file]]

    if cnt == 0 and wholebook:  # only single file audiobook, no part files
        cnt = 1
        parts = [[1, bookname, authorname, wholebook]]

    try:
        if cnt != len(parts):
            logger.warning(f"{bookname}: Incorrect number of parts (found {len(parts)} from {cnt})")
            failed = True

        if total_parts and total_parts != cnt:
            logger.warning(f"{bookname}: Reported {total_parts} parts, got {cnt}")
            failed = True

        # check all parts have the same author and title
        if len(parts) > 1:
            book = parts[0][1]
            author = parts[0][2]
            for part in parts:
                match = fuzz.partial_ratio(part[1], book)
                if match < 95:
                    logger.warning(f"{bookname}: Inconsistent title: [{part[1]}][{book}] ({round(match, 2)}%)")
                    failed = True

                match = fuzz.partial_ratio(part[2], author)
                if match < 95:
                    logger.warning(f"{bookname}: Inconsistent author: [{part[2]}][{author} ({round(match, 2)}%)]")
                    failed = True

        # do we have any usable track info from id3 tags
        partlist = []
        for part in parts:
            partlist.append(part[0])
        if failed or parts[0][0] == 0 or len(partlist) != len(set(partlist)):
            logger.debug("No usable track info from id3")
            if len(parts) == 1:
                return parts, failed, '', abridged
            # try to extract part information from filename. Search for token style of part 1 in this order...
            for token in [' 001.', ' 01.', ' 1.', ' 001 ', ' 01 ', ' 1 ', '001', '01']:
                if tokmatch:
                    break
                for part in parts:
                    if token in part[3]:
                        logger.debug(f"Using token '{token}' from {part[3]}")
                        tokmatch = token
                        break
            if tokmatch:  # we know the numbering style, get numbers for the other parts
                cnt = 0
                while cnt < len(parts):
                    cnt += 1
                    if tokmatch == ' 001.':
                        pattern = f' {str(cnt).zfill(3)}.'
                    elif tokmatch == ' 01.':
                        pattern = f' {str(cnt).zfill(2)}.'
                    elif tokmatch == ' 1.':
                        pattern = f' {str(cnt)}.'
                    elif tokmatch == ' 001 ':
                        pattern = f' {str(cnt).zfill(3)} '
                    elif tokmatch == ' 01 ':
                        pattern = f' {str(cnt).zfill(2)} '
                    elif tokmatch == ' 1 ':
                        pattern = f' {str(cnt)} '
                    elif tokmatch == '001':
                        pattern = f'{str(cnt).zfill(3)}'
                    else:
                        pattern = f'{str(cnt).zfill(2)}'
                    # standardise numbering of the parts
                    for part in parts:
                        if pattern in part[3]:
                            part[0] = cnt
                            break

    except Exception as e:
        logger.error(str(e))

    logger.debug(f"Checking numbering of {len(parts)} {plural(len(parts), 'part')}")
    parts.sort(key=lambda x: int(x[0]))

    # check all parts are present, ignore any part 0
    nparts = []
    for part in parts:
        if part[0]:
            nparts.append(part)
    parts = nparts

    cnt = 0
    while cnt < len(parts):
        if cnt and parts[cnt][0] == cnt:
            logger.error(f"{bookname}: Duplicate part {cnt} found")
            logger.error(f"{parts[cnt][3]} : {parts[cnt-1][3]}")
            failed = True
            break
        if parts[cnt][0] != cnt + 1:
            logger.warning(f'{bookname}: No part {cnt + 1} found, "{parts[cnt][0]}" '
                           f'for token "{tokmatch}" {parts[cnt][3]}')
            failed = True
            break
        cnt += 1
    logger.debug(f"Numbering of {len(parts)} {plural(len(parts), 'part')} {not failed}")
    return parts, failed, tokmatch, abridged


def audio_rename(bookid, rename=False, playlist=False, overwrite=False):
    """
    :param bookid: book to process
    :param rename: rename to match audiobook filename pattern
    :param playlist: generate a playlist for popup
    :return: filename of part 01 of the audiobook
    """
    logger = logging.getLogger(__name__)
    if rename:
        if '$Part' not in CONFIG['AUDIOBOOK_DEST_FILE']:
            logger.error("Unable to rename, no $Part in AUDIOBOOK_DEST_FILE")
            return ''
        if '$Title' not in CONFIG['AUDIOBOOK_DEST_FILE'] and \
                '$SortTitle' not in CONFIG['AUDIOBOOK_DEST_FILE']:
            logger.error("Unable to rename, no $Title or $SortTitle in AUDIOBOOK_DEST_FILE")
            return ''

    db = database.DBConnection()
    cmd = ('select AuthorName,BookName,AudioFile from books,authors where '
           'books.AuthorID = authors.AuthorID and bookid=?')
    exists = db.match(cmd, (bookid,))
    db.close()
    if exists:
        book_filename = exists['AudioFile']
        if book_filename:
            old_path = os.path.dirname(book_filename)
        else:
            logger.debug(f"No filename for {bookid} in audio_rename")
            return ''
    else:
        logger.debug(f"Invalid bookid in audio_rename {bookid}")
        return ''

    if not TinyTag:
        logger.warning("TinyTag library not available")
        return ''

    parts, failed, _, abridged = audio_parts(old_path, exists['BookName'], exists['AuthorName'])

    if failed or not parts:
        return exists['AudioFile']

    if abridged:
        abridged = f' ({abridged})'
    # if we get here, looks like we have all the parts needed to rename properly
    seriesinfo = name_vars(bookid, abridged)
    dest_path = seriesinfo['AudioFolderName']
    dest_dir = get_directory('Audio')
    dest_path = os.path.join(dest_dir, dest_path)
    old_path = old_path.rstrip(os.path.sep)
    dest_path = os.path.normpath(dest_path)

    # check for windows case-insensitive
    if os.name == 'nt' and old_path.lower() == dest_path.lower():
        dest_path = old_path

    if rename and old_path != dest_path:
        try:
            if len(old_path) > len(dest_path) and old_path.startswith(dest_path):
                # old_path is a subdir within new correct destination
                logger.debug(f"move contents of folder {old_path} to {dest_path}")
                failed, err = copy_tree(old_path, dest_path)
                if failed:
                    logger.error(f"Failed to copy {failed} files to {dest_path}")
                    logger.debug(f"{err}")
                    return ''
                shutil.rmtree(old_path)
            else:
                if path_isdir(dest_path) and not overwrite:
                    logger.debug(f"Not moving folder {old_path} as {dest_path} exists")
                else:
                    logger.debug(f"moving folder {old_path} to {dest_path}")
                    dest_path = safe_move(old_path, dest_path)
                    book_filename = os.path.join(dest_path, os.path.basename(book_filename))
        except Exception as why:
            msg = f'Rename failed: {why}'
            logger.error(msg)
            return ''

    if not rename:
        dest_path = old_path

    if playlist:
        try:
            playlist = open(os.path.join(dest_path, 'playlist.ll'), "w")
        except Exception as why:
            logger.error(f'Unable to create playlist in {dest_path}: {why}')
            playlist = None

    if len(parts) == 1:
        part = parts[0]
        namevars = name_vars(bookid, abridged)
        bookfile = namevars['AudioSingleFile']
        if not bookfile:
            bookfile = f"{exists['AuthorName']} - {exists['BookName']}"
        bookfile = sanitize(bookfile, True)
        out_type = splitext(part[3])[1]
        outfile = bookfile + out_type
        if playlist:
            if rename:
                playlist.write(f"{make_unicode(outfile)}\n")
            else:
                playlist.write(f"{make_unicode(part[3])}\n")
        if rename:
            n = os.path.join(make_unicode(dest_path), make_unicode(outfile))
            o = os.path.join(make_unicode(dest_path), make_unicode(part[3]))
            # check for windows case-insensitive
            if os.name == 'nt' and n.lower() == o.lower():
                n = o
            if o != n:
                try:
                    if path_isfile(n) and not overwrite:
                        logger.debug(f"Not moving {o} as {n} exists")
                        n = o
                    else:
                        logger.debug(f"{exists['BookName']}: audio_rename [{o}] to [{n}]")
                        n = safe_move(o, n)
                    book_filename = n  # return part 1 of set
                except Exception as e:
                    logger.error(f'Unable to rename [{o}] to [{n}] {type(e).__name__} {str(e)}')
    else:
        for part in parts:
            pattern = seriesinfo['AudioFile']
            pattern = pattern.replace(
                '$Part', str(part[0]).zfill(len(str(len(parts))))).replace(
                '$Total', str(len(parts)))
            pattern = ' '.join(pattern.split()).strip()
            pattern = pattern + splitext(part[3])[1]
            if rename:
                pattern = sanitize(pattern, True)

            if playlist:
                if rename:
                    playlist.write(f"{make_unicode(pattern)}\n")
                else:
                    playlist.write(f"{make_unicode(part[3])}\n")
            if rename:
                n = os.path.join(make_unicode(dest_path), make_unicode(pattern))
                o = os.path.join(make_unicode(dest_path), make_unicode(part[3]))
                # check for windows case-insensitive
                if os.name == 'nt' and n.lower() == o.lower():
                    n = o
                if o != n:
                    try:
                        if path_isfile(n) and not overwrite:
                            logger.debug(f"Not renaming [{o}] as [{n}] exists")
                            n = o
                        else:
                            n = safe_move(o, n)
                        if part[0] == 1:
                            book_filename = n  # return part 1 of set
                        logger.debug(f"{exists['BookName']}: audio_rename [{o}] to [{n}]")
                    except Exception as e:
                        logger.error(f'Unable to rename [{o}] to [{n}] {type(e).__name__} {str(e)}')
    if playlist:
        playlist.close()
    return book_filename


def stripspaces(pathname):
    # windows doesn't allow directory names to end in a space or a period
    # but allows starting with a period (not sure about starting with a space, but it looks messy anyway)
    parts = pathname.split(os.path.sep)
    new_parts = []
    for part in parts:
        while part and part[-1] in ' .':
            part = part[:-1]
        part = part.lstrip(' ')
        new_parts.append(part)
    pathname = os.path.sep.join(new_parts)
    return pathname


def book_rename(bookid, overwrite=False):
    logger = logging.getLogger(__name__)
    db = database.DBConnection()
    cmd = ('select AuthorName,BookName,BookFile from books,authors where '
           'books.AuthorID = authors.AuthorID and bookid=?')
    exists = db.match(cmd, (bookid,))
    db.close()

    if not exists:
        msg = f"Invalid bookid in book_rename {bookid}"
        logger.debug(msg)
        return '', msg

    fullname = exists['BookFile']
    if not fullname:
        msg = f"No filename for {bookid} in BookRename"
        logger.debug(msg)
        return '', msg

    old_path = os.path.dirname(fullname)
    if CONFIG.get_bool('IMP_CALIBRE_EBOOK'):
        try:
            # noinspection PyTypeChecker
            calibreid = old_path.rsplit('(', 1)[1].split(')')[0]
            if not calibreid.isdigit():
                calibreid = ''
        except IndexError:
            calibreid = ''

        if calibreid:
            msg = f'[{os.path.basename(old_path)}] looks like a calibre directory: not renaming book'
            logger.debug(msg)
            return fullname, msg

    reject = multibook(old_path)
    if reject:
        msg = f"Not renaming {fullname}, found multiple {reject}"
        logger.debug(msg)
        return fullname, msg

    namevars = name_vars(bookid)
    dest_path = namevars['FolderName']
    dest_dir = get_directory('eBook')
    dest_path = os.path.join(dest_dir, dest_path)
    dest_path = stripspaces(dest_path.rstrip(os.path.sep))
    new_basename = namevars['BookFile']

    if fullname and not os.path.isfile(fullname):
        _, extn = splitext(fullname)
        if extn:
            new_location = os.path.join(dest_path, new_basename + extn)
            if os.path.isfile(new_location):
                msg = f"Source file for {bookid} already moved"
                logger.debug(msg)
                return new_location, msg

    if ' / ' in new_basename:  # used as a separator in goodreads omnibus
        msg = f"[{new_basename}] looks like an omnibus? Not renaming"
        logger.warning(msg)
        return fullname, msg

    # check for windows case-insensitive
    if os.name == 'nt' and old_path.lower() == dest_path.lower():
        dest_path = old_path

    if old_path != dest_path:
        try:
            os.makedirs(dest_path, exist_ok=True)
        except Exception as why:
            msg = f'makedirs failed: {why}'
            logger.error(msg)
            return fullname, msg

    msg = ''
    # only rename bookname.type, bookname.jpg, bookname.opf
    # not cover.jpg or metadata.opf or anything else in the folder
    for fname in listdir(old_path):
        extn = ''
        if CONFIG.is_valid_booktype(fname, booktype='ebook'):
            extn = splitext(fname)[1]
        elif fname.endswith('.opf') and fname != 'metadata.opf':
            extn = '.opf'
        elif fname.endswith('.jpg') and fname != 'cover.jpg':
            extn = '.jpg'
        if extn:
            ofname = os.path.join(old_path, fname)
            nfname = os.path.join(dest_path, new_basename + extn)
            # check for windows case-insensitive
            if os.name == 'nt' and nfname.lower() == ofname.lower():
                nfname = ofname
            if ofname != nfname:
                try:
                    if path_isfile(nfname) and not overwrite:
                        m = f"Not moving {ofname} as {nfname} exists"
                        nfname = ofname
                    else:
                        nfname = safe_move(ofname, nfname)
                        m = f"move file {ofname} to {nfname} "
                    logger.debug(m)
                    msg += m
                    if ofname == exists['BookFile']:  # if we renamed/moved the preferred file, return new name
                        fullname = nfname
                except Exception as e:
                    m = f'Unable to move [{ofname}] to [{nfname}] {type(e).__name__} {str(e)} '
                    logger.error(m)
                    msg += m
        else:
            # just move everything else without renaming
            ofname = os.path.join(old_path, fname)
            nfname = os.path.join(dest_path, fname)
            if os.name == 'nt' and nfname.lower() == ofname.lower():
                nfname = ofname
            if ofname != nfname:
                try:
                    if path_isfile(nfname) and not overwrite:
                        m = f"Not moving {ofname} as {nfname} exists"
                        nfname = ofname
                    else:
                        nfname = safe_move(ofname, nfname)
                        m = f"move file {ofname} to {nfname} "
                    logger.debug(m)
                    msg += m
                except Exception as e:
                    m = f'Unable to move [{ofname}] to [{nfname}] {type(e).__name__} {str(e)} '
                    logger.error(m)
                    msg += m

    if not len(listdir(old_path)):
        # everything moved out...
        os.rmdir(old_path)

    return fullname, msg


def delete_empty_folders(startdir):
    deleted = set()
    for current_dir, subdirs, files in os.walk(startdir, topdown=False):
        still_has_subdirs = False
        for subdir in subdirs:
            if os.path.join(current_dir, subdir) not in deleted:
                still_has_subdirs = True
                break

        if not any(files) and not still_has_subdirs:
            os.rmdir(current_dir)
            deleted.add(current_dir)
    return deleted


def name_vars(bookid, abridged=''):
    """ Return name variables for a bookid as a dict of formatted strings
        The strings are configurable, but by default...
        Series returns ( Lord of the Rings 2 )
        FmtName returns Lord of the Rings (with added Num part if that's not numeric, eg Lord of the Rings Book One)
        FmtNum  returns Book #1 -    (or empty string if no numeric part)
        so you can combine to make Book #1 - Lord of the Rings
        PadNum is zero padded numeric part or empty string
        SerName and SerNum are the unformatted base strings
        PubYear is the publication year of the book or empty string
        SerYear is the publication year of the first book in the series or empty string
        Language is the language(s) if known or empty string
        """
    matchinglogger = logging.getLogger('special.matching')
    mydict = {}
    seriesnum = ''
    seriesname = ''
    db = database.DBConnection()
    try:
        if bookid == 'test':
            seriesid = '66175'
            serieslist = ['3']
            pubyear = '1955'
            seryear = '1954'
            lang = 'Eng'
            seriesname = 'The Lord of the Rings'
            mydict['Author'] = 'J.R.R. Tolkien'
            mydict['Title'] = 'The Fellowship of the Ring'
            mydict['SortAuthor'] = surname_first(mydict['Author'], postfixes=get_list(CONFIG.get_csv('NAME_POSTFIX')))
            mydict['SortTitle'] = sort_definite(mydict['Title'], articles=get_list(CONFIG.get_csv('NAME_DEFINITE')))
            mydict['Part'] = '1'
            mydict['Total'] = '3'
            res = {}
        else:
            cmd = "SELECT SeriesID,SeriesNum from member,books WHERE books.bookid = member.bookid and books.bookid=?"
            res = db.match(cmd, (bookid,))
            if res:
                seriesid = res['SeriesID']
                serieslist = get_list(res['SeriesNum'])

                cmd = ('SELECT BookDate from member,books WHERE books.bookid = member.bookid and '
                       'SeriesNum=1 and SeriesID=?')
                res_date = db.match(cmd, (seriesid,))
                if res_date:
                    seryear = res_date['BookDate']
                    if not seryear or seryear == '0000':
                        seryear = ''
                    seryear = seryear[:4]
                else:
                    seryear = ''
            else:
                seriesid = ''
                serieslist = []
                seryear = ''

            cmd = "SELECT BookDate,BookLang from books WHERE bookid=?"
            bookres = db.match(cmd, (bookid,))
            if bookres:
                pubyear = bookres['BookDate']
                if not pubyear or pubyear == '0000':
                    pubyear = ''
                pubyear = pubyear[:4]  # googlebooks sometimes has month or full date
                lang = bookres['BookLang']
            else:
                pubyear = ''
                lang = ''

        # might be "Book 3.5" or similar, just get the numeric part
        while serieslist:
            seriesnum = serieslist.pop()
            seriesnum = seriesnum.lstrip('#')
            try:
                _ = float(seriesnum)
                break
            except ValueError:
                seriesnum = ''

        padnum = ''
        if res and seriesnum == '':
            # couldn't figure out number, keep everything we got, could be something like "Book Two"
            serieslist = res['SeriesNum']
        elif seriesnum.isdigit():
            padnum = str(int(seriesnum)).zfill(2)
        else:
            try:
                padnum = str(float(seriesnum))
                if padnum[1] == '.':
                    padnum = f"0{padnum}"
            except (ValueError, IndexError):
                padnum = ''

        if seriesid and bookid != 'test':
            cmd = "SELECT SeriesName from series WHERE seriesid=?"
            res = db.match(cmd, (seriesid,))
            if res:
                seriesname = res['SeriesName']
                if seriesnum == '' and seriesname and serieslist:
                    # add what we got back to end of series name
                    seriesname = f"{seriesname} {serieslist}"

        seriesname = ' '.join(seriesname.split())  # strip extra spaces
        if only_punctuation(seriesname):  # but don't return just whitespace or punctuation
            seriesname = ''

        if seriesname:
            fmtname = CONFIG['FMT_SERNAME'].replace('$SerName', seriesname).replace(
                                                                  '$PubYear', pubyear).replace(
                                                                  '$SerYear', seryear).replace(
                                                                  '$Language', lang).replace(
                                                                  '$$', ' ')
        else:
            fmtname = ''

        if only_punctuation(fmtname):
            fmtname = ''

        if seriesnum != '':  # allow 0
            fmtnum = CONFIG['FMT_SERNUM'].replace('$SerNum', seriesnum).replace(
                                                                '$PubYear', pubyear).replace(
                                                                '$SerYear', seryear).replace(
                                                                '$PadNum', padnum).replace('$$', ' ')
        else:
            fmtnum = ''

        if only_punctuation(fmtnum):
            fmtnum = ''

        if fmtnum != '' or fmtname:
            fmtseries = CONFIG['FMT_SERIES'].replace('$SerNum', seriesnum).replace(
                                                                 '$SerName', seriesname).replace(
                                                                 '$PadNum', padnum).replace(
                                                                 '$PubYear', pubyear).replace(
                                                                 '$SerYear', seryear).replace(
                                                                 '$Language', lang).replace(
                                                                 '$FmtName', fmtname).replace(
                                                                 '$FmtNum', fmtnum).replace('$$', ' ')
        else:
            fmtseries = ''

        if only_punctuation(fmtseries):
            fmtseries = ''

        mydict['FmtName'] = fmtname
        mydict['FmtNum'] = fmtnum
        mydict['Series'] = fmtseries
        mydict['PadNum'] = padnum
        mydict['SerName'] = seriesname
        mydict['SerNum'] = seriesnum
        mydict['PubYear'] = pubyear
        mydict['SerYear'] = seryear
        mydict['Abridged'] = abridged
        mydict['Language'] = lang

        if bookid != 'test':
            cmd = "select AuthorName,BookName from books,authors where books.AuthorID = authors.AuthorID and bookid=?"
            exists = db.match(cmd, (bookid,))
            if exists:
                mydict['Author'] = exists['AuthorName']
                mydict['Title'] = exists['BookName']
                mydict['SortAuthor'] = surname_first(mydict['Author'],
                                                     postfixes=get_list(CONFIG.get_csv('NAME_POSTFIX')))
                mydict['SortTitle'] = sort_definite(mydict['Title'],
                                                    articles=get_list(CONFIG.get_csv('NAME_DEFINITE')))
            else:
                mydict['Author'] = ''
                mydict['Title'] = ''
                mydict['SortAuthor'] = ''
                mydict['SortTitle'] = ''

    except Exception as e:
        matchinglogger.debug(str(e))
    db.close()

    mydict['FolderName'] = stripspaces(replacevars(CONFIG['EBOOK_DEST_FOLDER'], mydict, True))
    mydict['AudioFolderName'] = stripspaces(replacevars(CONFIG['AUDIOBOOK_DEST_FOLDER'], mydict, True))
    mydict['BookFile'] = stripspaces(replacevars(CONFIG['EBOOK_DEST_FILE'], mydict, True))
    mydict['AudioFile'] = (stripspaces(replacevars(CONFIG['AUDIOBOOK_DEST_FILE'], mydict))
                           .replace('sPart', '$Part')
                           .replace('sTotal', '$Total'))
    mydict['AudioSingleFile'] = (stripspaces(replacevars(CONFIG['AUDIOBOOK_SINGLE_FILE'], mydict, True))
                                 .replace('sPart', '$Part')
                                 .replace('sTotal', '$Total'))
    if bookid != 'test':
        matchinglogger.debug(str(mydict))
    return mydict
