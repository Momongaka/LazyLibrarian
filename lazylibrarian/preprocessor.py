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
import subprocess
from urllib.parse import unquote_plus

import lazylibrarian
from lazylibrarian import database
from lazylibrarian.bookrename import audio_parts, id3read, name_vars
from lazylibrarian.common import calibre_prg, zip_audio
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import (
    DIRS,
    listdir,
    path_exists,
    remove_file,
    safe_copy,
    safe_move,
    setperm,
    splitext,
)
from lazylibrarian.formatter import (
    check_float,
    check_int,
    get_list,
    human_size,
    make_unicode,
    now,
    plural,
)
from lazylibrarian.images import coverswap, shrink_mag, valid_pdf, write_pdf_tags


def preprocess_ebook(bookfolder):
    logger = logging.getLogger(__name__)
    postprocesslogger = logging.getLogger('special.postprocess')
    logger.debug(f"Preprocess ebook {bookfolder}")
    ebook_convert = calibre_prg('ebook-convert')
    if not ebook_convert:
        logger.error("No ebook-convert found")
        return False

    sourcefile = None
    created = ''
    for fname in listdir(bookfolder):
        _, extn = splitext(fname)
        if extn.lower() == '.epub':
            sourcefile = fname
            break
    if not sourcefile:
        for fname in listdir(bookfolder):
            filename, extn = splitext(fname)
            if extn.lower() in ['.mobi', ".azw", '.azw3']:
                sourcefile = fname
                break

    if not sourcefile:
        logger.error(f"No suitable sourcefile found in {bookfolder}")
        return False

    basename, source_extn = splitext(sourcefile)
    logger.debug(f"Wanted formats: {CONFIG['EBOOK_WANTED_FORMATS']}")
    wanted_formats = get_list(CONFIG['EBOOK_WANTED_FORMATS'])
    for ftype in wanted_formats:
        if not path_exists(os.path.join(bookfolder, basename + '.' + ftype)):
            logger.debug(f"No {ftype}")
            params = [ebook_convert, os.path.join(bookfolder, sourcefile),
                      os.path.join(bookfolder, basename + '.' + ftype)]
            if ftype in ['mobi', 'azw', 'azw3']:
                params.extend(['--output-profile', 'kindle'])
            postprocesslogger.debug(str(params))
            try:
                if os.name != 'nt':
                    _ = subprocess.check_output(params, preexec_fn=lambda: os.nice(10),
                                                stderr=subprocess.STDOUT)
                else:
                    _ = subprocess.check_output(params, stderr=subprocess.STDOUT)

                if created:
                    created += ' '
                created += ftype
            except Exception as e:
                logger.error(f"{e}")
                logger.error(repr(params))
                return False
        else:
            logger.debug(f"Found {ftype}")

    if wanted_formats and CONFIG.get_bool('DELETE_OTHER_FORMATS'):
        if CONFIG.get_bool('KEEP_OPF'):
            wanted_formats.append('opf')
        if CONFIG.get_bool('KEEP_JPG'):
            wanted_formats.append('jpg')
        for fname in listdir(bookfolder):
            filename, extn = splitext(fname)
            if not extn or extn.lstrip('.').lower() not in wanted_formats:
                logger.debug(f"Deleting {fname}")
                remove_file(os.path.join(bookfolder, fname))
    if created:
        logger.debug(f"Created {created} from {source_extn}")
    else:
        logger.debug("No extra ebook formats created")
    return True


def get_ffmpeg_details():
    logger = logging.getLogger(__name__)
    ffmpeg = CONFIG['FFMPEG']
    if not ffmpeg:
        logger.error("Check config setting for ffmpeg")
        return ''
    ff_ver = lazylibrarian.FFMPEGVER
    if not ff_ver:
        try:
            params = [ffmpeg, "-version"]
            res = subprocess.check_output(params, stderr=subprocess.STDOUT)
            res = make_unicode(res).strip().split("Copyright")[0].split()[-1]
            logger.debug(f"Found ffmpeg version {res}")
            ff_ver = res
        except Exception as e:
            logger.debug(f"ffmpeg -version failed: {type(e).__name__} {str(e)}")
            ff_ver = ''
        finally:
            lazylibrarian.FFMPEGVER = ff_ver

    ff_aac = lazylibrarian.FFMPEGAAC
    if ff_ver and not ff_aac:
        try:
            params = [ffmpeg, "-codecs"]
            if os.name != 'nt':
                res = subprocess.check_output(params, preexec_fn=lambda: os.nice(10),
                                              stderr=subprocess.STDOUT)
            else:
                res = subprocess.check_output(params, stderr=subprocess.STDOUT)
            res = make_unicode(res)
            for lyne in res.split('\n'):
                if 'AAC' in lyne:
                    ff_aac = lyne.strip().split(' ')[0]
                    break
        except Exception as e:
            logger.debug(f"ffmpeg -codecs failed: {type(e).__name__} {str(e)}")
            ff_aac = ''
        finally:
            lazylibrarian.FFMPEGAAC = ff_aac
    if ff_aac and ('D' not in ff_aac or 'E' not in ff_aac):
        logger.warning(f"Your version of ffmpeg does not report supporting read/write aac ({ff_aac})")
    return ffmpeg


def write_metadata(source_file, metadata_file):
    logger = logging.getLogger(__name__)
    postprocesslogger = logging.getLogger('special.postprocess')
    ffmpeg = CONFIG['FFMPEG']
    params = [ffmpeg, '-i', source_file,
              '-f', 'ffmetadata', '-y', metadata_file]
    if postprocesslogger.isEnabledFor(logging.DEBUG):
        params.append('-report')
        logger.debug(str(params))
        ffmpeg_env = os.environ.copy()
        ffmpeg_env["FFREPORT"] = "file=" + \
            DIRS.get_tmpfilename(f"ffmpeg-meta-{now().replace(':', '-').replace(' ', '-')}.log")
    else:
        ffmpeg_env = None
    try:
        if os.name != 'nt':
            _ = subprocess.check_output(params, preexec_fn=lambda: os.nice(10),
                                        stderr=subprocess.STDOUT, env=ffmpeg_env)
        else:
            _ = subprocess.check_output(params, stderr=subprocess.STDOUT, env=ffmpeg_env)
        return True
    except subprocess.CalledProcessError as e:
        logger.debug(f"Error reading metadata from {source_file}, aborting")
        logger.error(f"{type(e).__name__}: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"{type(e).__name__}: {str(e)}")
        return False


def read_part_durations(bookfolder, parts, metadata_file, duration_file):
    logger = logging.getLogger(__name__)
    postprocesslogger = logging.getLogger('special.postprocess')
    ffmpeg = CONFIG['FFMPEG']
    part_durations = []
    highest_bitrate = 0
    for part in parts:
        # we don't actually need the output file here, but ffmpeg insists on having one.
        # we get the duration data from the subprocess response
        params = [ffmpeg, '-i', os.path.join(bookfolder, part[3]),
                  '-f', 'ffmetadata', '-y', os.path.join(bookfolder, "partmeta.ll")]
        if postprocesslogger.isEnabledFor(logging.DEBUG):
            params.append('-report')
            logger.debug(str(params))
            ffmpeg_env = os.environ.copy()
            ffmpeg_env["FFREPORT"] = "file=" + \
                DIRS.get_tmpfilename(f"ffmpeg-part-{now().replace(':', '-').replace(' ', '-')}.log")
        else:
            ffmpeg_env = None
        try:
            if os.name != 'nt':
                res = subprocess.check_output(params, preexec_fn=lambda: os.nice(10),
                                              stderr=subprocess.STDOUT, env=ffmpeg_env)
            else:
                res = subprocess.check_output(params, stderr=subprocess.STDOUT, env=ffmpeg_env)

            res = res.decode('utf-8')
            if 'Duration: ' in res:
                try:
                    duration = res.split('Duration: ', 1)[1].split(',')[0]
                    bitrate = res.split('bitrate: ', 1)[1].split(' ')[0]
                    h, m, s = duration.split(':')
                    secs = check_float(s, 0) + (check_int(m, 0) * 60) + (check_int(h, 0) * 3600)
                    part_durations.append([part[0], secs])
                    bitrate = check_int(bitrate, 0)
                    logger.debug(f"Part {part[0]}, duration {secs}, bitrate {bitrate}")
                    if bitrate > highest_bitrate:
                        highest_bitrate = bitrate
                except IndexError:
                    logger.debug(f"Error reading duration from {part[3]}, assume 0")
                    part_durations.append([part[0], 0])
            else:
                logger.debug(f"No duration found in {part[3]}, assume 0")
                part_durations.append([part[0], 0])

        except subprocess.CalledProcessError as e:
            logger.debug(f"Error getting duration from {part[3]}, assume 0")
            logger.error(f"{type(e).__name__}: {str(e)}")
            part_durations.append([part[0], 0])
        except Exception as e:
            logger.error(f"{type(e).__name__}: {str(e)}")
            return part_durations, highest_bitrate

    if part_durations:
        part_durations.sort(key=lambda x: x[0])
        start = 0
        with (open(metadata_file, encoding="utf-8") as f,
              open(os.path.join(bookfolder, duration_file), 'w', encoding="utf-8") as o):
            for lyne in f.readlines():
                if (not lyne.startswith('[CHAPTER]') and not lyne.startswith('TIMEBASE=') and
                    not lyne.startswith('START=') and not lyne.startswith('END=') and
                        not lyne.startswith('title=')):
                    o.write(lyne)

        with open(duration_file, 'a', encoding="utf-8") as f:
            for item in part_durations:
                if item[0]:
                    f.write("[CHAPTER]\nTIMEBASE=1/1000\n")
                    f.write(f"START={int(start)}\n")
                    start = start + (1000 * item[1])
                    f.write(f"END={int(start)}\n")
                    f.write(f"title=Chapter {item[0]}\n")

    if highest_bitrate:
        logger.debug(f"Highest bitrate is {highest_bitrate}")
    remove_file(os.path.join(bookfolder, "partmeta.ll"))
    return part_durations, highest_bitrate


# noinspection PyUnusedLocal
def get_metatags(bookid, bookfile, authorname, bookname, source_file):
    db = database.DBConnection()
    match = db.match('SELECT * from books WHERE bookid=?', (bookid,))

    if bookfile:
        title = bookfile
    else:
        title = f"{authorname} - {bookname}"

    if match:
        id3r = id3read(source_file)
        if not match['Narrator'] and id3r.get('narrator'):
            db.action("UPDATE books SET Narrator=? WHERE BookID=?", (id3r['narrator'], bookid))
        id3dict = {'artist': id3r.get('artist'), 'composer': id3r.get('composer'),
                   'album_artist': id3r.get('albumartist'), 'album': id3r.get('album'), 'comment': id3r.get('comment'),
                   'author': authorname, 'media_type': "Audiobook", 'genre': match['BookGenre'],
                   'description': match['BookDesc'], 'date': match['BookDate']}
        if match['BookDate'] == '0000':
            id3dict['date'] = ''
        if match['SeriesDisplay']:
            series = match['SeriesDisplay'].split('<br>')[0].strip()
            if series and '$SerName' in CONFIG['AUDIOBOOK_DEST_FILE']:
                title = f"{title} ({series})"
        metatags = ['-metadata', f"title={title}"]
        for item in id3dict:
            if id3dict[item]:
                metatags.extend(['-metadata', f"{item}={id3dict[item]}"])
    else:
        metatags = ['-metadata', f"album={bookname}",
                    '-metadata', f"artist={authorname}",
                    '-metadata', f"title={bookfile}"]
    db.close()
    return metatags


def write_audio_tags(bookfolder, filename, track, metatags):
    logger = logging.getLogger(__name__)
    postprocesslogger = logging.getLogger('special.postprocess')
    ffmpeg = CONFIG['FFMPEG']
    try:
        extn = splitext(filename)[1]
        # ffmpeg will detect cover art in m4a as a video
        # and try to convert to mjpeg to h264 and will fail when codec is not installed.
        # This copies image as is
        params = [ffmpeg, '-i', os.path.join(bookfolder, filename),
                  '-y', '-c:a', 'copy',
                  '-c:v', 'copy']
        # ffmpeg will detect cover art in m4a as a video and try to convert to mjpeg to h264
        # and will fail when codec is not installed. This copies image as is
        params.extend(metatags)
        params.extend(['-metadata', f'track={track}'])
        tempfile = os.path.join(bookfolder, f"tempaudio{extn}")
        if extn == '.m4b':
            # some versions of ffmpeg will not add tags to m4b files, but they will add them to m4a
            b2a = True
            tempfile = tempfile.replace('.m4b', '.m4a')
        else:
            b2a = False

        params.append(tempfile)
        if postprocesslogger.isEnabledFor(logging.DEBUG):
            params.append('-report')
            logger.debug(str(params))
            ffmpeg_env = os.environ.copy()
            ffmpeg_env["FFREPORT"] = "file=" + \
                DIRS.get_tmpfilename(f"ffmpeg-merge_tag-{now().replace(':', '-').replace(' ', '-')}.log")
        else:
            ffmpeg_env = None
        try:
            if os.name != 'nt':
                _ = subprocess.check_output(params, preexec_fn=lambda: os.nice(10),
                                            stderr=subprocess.STDOUT, env=ffmpeg_env)
            else:
                _ = subprocess.check_output(params, stderr=subprocess.STDOUT, env=ffmpeg_env)

            outfile = os.path.join(bookfolder, filename)
            remove_file(outfile)
            if b2a:
                tempfile.replace('.m4a', '.m4b')
            safe_move(tempfile, outfile)
            logger.debug(f"Metadata written to {outfile}")
        except subprocess.CalledProcessError as e:
            logger.debug(f"Error writing metadata to {filename}")
            logger.error(f"{type(e).__name__}: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"{type(e).__name__}: {str(e)}")
            return False
    except Exception as e:
        logger.error(f" Error writing tags to files: {e}")
        return False
    return True


def preprocess_audio(bookfolder, bookid='', authorname='', bookname='', merge=None, tag=None, zipp=None):
    logger = logging.getLogger(__name__)
    postprocesslogger = logging.getLogger('special.postprocess')
    if merge is None:
        merge = CONFIG.get_bool('CREATE_SINGLEAUDIO')
    if tag is None:
        tag = CONFIG.get_bool('WRITE_AUDIOTAGS')
    if zipp is None:
        zipp = CONFIG.get_bool('ZIP_AUDIOPARTS')
    if not merge and not tag and not zipp:
        return True  # nothing to do

    logger.debug(f"Preprocess audio {bookfolder} {authorname} {bookname}, merge={merge}, tag={tag}, zip={zipp}")

    ffmpeg = get_ffmpeg_details()
    if not ffmpeg:
        return False

    partslist_file = os.path.join(bookfolder, "partslist.ll")
    metadata_file = os.path.join(bookfolder, "metadata.ll")
    duration_file = os.path.join(bookfolder, "durationdata.ll")

    # this is to work around an ffmpeg oddity...
    if os.path.__name__ == 'ntpath':
        partslist_file = partslist_file.replace("\\", "/")
        metadata_file = metadata_file.replace("\\", "/")
        duration_file = duration_file.replace("\\", "/")

    parts, failed, token, abridged = audio_parts(bookfolder, bookname, authorname)

    if failed or not parts:
        return False

    namevars = name_vars(bookid, abridged)

    # if we get here, looks like we have all the parts
    # output file will be the same type as the first input file
    # unless the user supplies a parameter to override it
    out_type = ''
    if CONFIG['FFMPEG_OUT']:
        out_type = '.' + CONFIG['FFMPEG_OUT'].lower().lstrip('.')
        unquoted_type = unquote_plus(out_type)
        for token in ['<', '>', '=', '"']:
            if token in unquoted_type:
                logger.warning(f'Cannot set output type, contains "{token}"')
    if not out_type:
        out_type = splitext(parts[0][3])[1]

    if '-f ' in CONFIG['AUDIO_OPTIONS']:
        force_type = '.' + CONFIG['AUDIO_OPTIONS'].split('-f ', 1)[1].split(',')[0].split(' ')[0].strip()
    else:
        force_type = ''

    force_mp4 = False
    if out_type in ['.m4b', '.m4a', '.aac', '.mp4']:
        force_mp4 = True
    # else:  # should we force mp4 if input is mp4 but output is mp3?
    #     for part in parts:
    #         if splitext(part[3])[1] in ['.m4b', '.m4a', '.aac', '.mp4']:
    #             force_mp4 = True
    #            break

    if force_mp4 and force_type != 'mp4':
        if force_type:
            pre, post = CONFIG['AUDIO_OPTIONS'].split('-f ', 1)
            post = post.lstrip()
            post = post[len(force_type) + 1:]
            ffmpeg_options = pre + '-f mp4 ' + post
        else:
            ffmpeg_options = CONFIG['AUDIO_OPTIONS'] + ' -f mp4'
    else:
        ffmpeg_options = CONFIG['AUDIO_OPTIONS']

    logger.debug(f"ffmpeg options: {ffmpeg_options}")

    with open(partslist_file, 'w', encoding="utf-8") as f:
        for part in parts:
            # Use single quotes and escape apostrophes for FFmpeg compatibility
            escaped_name = part[3].replace("'", "'\\''")
            f.write(f"file '{escaped_name}'\n")

    bookfile = namevars['AudioSingleFile'] if namevars['Author'] else ''
    # might not have any namevars (eg no bookid)
    if not bookfile:
        bookfile = f"{authorname} - {bookname}"
    outfile = bookfile + out_type

    if len(parts) == 1:
        if out_type == splitext(parts[0][3])[1]:
            logger.info("Only one audio file found, nothing to merge")
            merge = False
        else:
            logger.info(f"Only one audio file found, changing format from "
                        f"{splitext(parts[0][3])[1]} to {out_type}")
            merge = True

    ff_ver = lazylibrarian.FFMPEGVER
    if ff_ver and merge:
        # read metadata from first file
        source_file = os.path.join(bookfolder, parts[0][3])
        if not os.path.isfile(source_file):
            logger.error(f"Source file {source_file} not found, aborting")
            return False

        if not write_metadata(source_file, metadata_file):
            return False

        _, highest_bitrate = read_part_durations(bookfolder, parts, metadata_file, duration_file)

        params = [ffmpeg]

        # this produces a single file audiobook from partslist and adds metadata from file
        ffmpeg_params = ['-f', 'concat', '-safe', '0', '-i', partslist_file, '-f', 'ffmetadata',
                         '-i', duration_file, '-map_metadata', '1', '-id3v2_version', '3']

        params.extend(ffmpeg_params)
        options = get_list(ffmpeg_options)
        if '-b:a' in options:
            config_bitrate = check_int(''.join(c for c in options[options.index('-b:a') + 1] if c.isdigit()), 128)
            if highest_bitrate and highest_bitrate < config_bitrate:
                options[options.index('-b:a') + 1] = f"{highest_bitrate}k"
                logger.debug(f"Dropping output bitrate to {highest_bitrate}k")
        params.extend(options)
        params.append('-y')
        params.append(os.path.join(bookfolder, outfile))
        if postprocesslogger.isEnabledFor(logging.DEBUG):
            params.append('-report')
            logger.debug(str(params))
            ffmpeg_env = os.environ.copy()
            ffmpeg_env["FFREPORT"] = "file=" + \
                DIRS.get_tmpfilename(f"ffmpeg-merge-{now().replace(':', '-').replace(' ', '-')}.log")
        else:
            ffmpeg_env = None
        res = ''
        try:
            logger.debug(f"Merging {len(parts)} {plural(len(parts), 'file')} to {outfile}")
            if os.name != 'nt':
                res = subprocess.check_output(params, preexec_fn=lambda: os.nice(10),
                                              stderr=subprocess.STDOUT, env=ffmpeg_env)
            else:
                res = subprocess.check_output(params, stderr=subprocess.STDOUT, env=ffmpeg_env)
        except subprocess.CalledProcessError as e:
            logger.debug("Error merging files, aborting")
            logger.error(f"{type(e).__name__}: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"{type(e).__name__}: {str(e)}")
            if res:
                logger.error(res)
            return False

        if not CONFIG.get_bool('KEEP_SEPARATEAUDIO'):
            logger.debug(f"Removing {len(parts)} part {plural(len(parts), 'file')}")
            for part in parts:
                remove_file(os.path.join(bookfolder, part[3]))
            parts = parts[:1]
            # track, title, author, file
            parts[0][3] = outfile
            parts[0][0] = 0

    if tag:
        source_file = os.path.join(bookfolder, parts[0][3])
        metatags = get_metatags(bookid, bookfile, authorname, bookname, source_file)
        logger.debug(f"Writing new tags: {metatags}")

        errored = 0
        for part in parts:
            res = write_audio_tags(bookfolder, part[3], part[0], metatags)
            if not res:
                errored += 1
        logger.debug(f"Written tags to {len(parts)}, errors on {errored}")

    if zipp:
        _ = zip_audio(bookfolder, bookname, bookid)

    remove_file(partslist_file)
    remove_file(metadata_file)
    remove_file(duration_file)
    return True


def preprocess_magazine(bookfolder, cover=0, tag=False, title='', issue='', genres=''):
    logger = logging.getLogger(__name__)
    logger.debug(f"Preprocess magazine {bookfolder} cover={cover}")
    try:
        sourcefile = None
        source_extn = ''
        for fname in listdir(bookfolder):
            _, extn = splitext(fname)
            lower_extn = extn.lower()
            if lower_extn == '.pdf':
                sourcefile = fname
                source_extn = lower_extn
                break
            if not sourcefile and lower_extn in ['.cbz', '.cbr']:
                sourcefile = fname
                source_extn = lower_extn

        if not sourcefile:
            msg = f"No suitable sourcefile found in {bookfolder}"
            logger.error(msg)
            return False, msg

        cover = check_int(cover, 0)
        dpi = CONFIG.get_int('SHRINK_MAG')

        if source_extn in ['.cbz', '.cbr']:
            if dpi or (CONFIG.get_bool('SWAP_COVERPAGE') and cover > 1) or tag:
                logger.debug(f"Magazine preprocessing options apply to pdf only, leaving {sourcefile} unchanged")
            else:
                logger.debug(f"No preprocessing required for {source_extn} magazine archive")
            return True, ''

        if not valid_pdf(os.path.join(bookfolder, sourcefile)):
            msg = f"Invalid pdf {sourcefile} in {bookfolder}"
            return False, msg

        if not dpi and not (CONFIG.get_bool('SWAP_COVERPAGE') and cover > 1) and not tag:
            logger.debug("No preprocessing required")
            return True, ''

        # reordering or shrinking pages is quite slow if the source is on a networked drive
        # so work on a local copy, then move it over.
        original = os.path.join(bookfolder, sourcefile)
        try:
            srcfile = safe_copy(original, os.path.join(DIRS.CACHEDIR, sourcefile))
        except Exception as e:
            logger.warning(f"Failed to copy source file: {str(e)}")
            return False, str(e)
        if dpi:
            logger.debug(f"Resizing {srcfile} to {dpi} dpi")
            shrunkfile = shrink_mag(srcfile, dpi)
            old_size = os.stat(srcfile).st_size
            try:
                new_size = os.stat(shrunkfile).st_size
            except OSError as e:
                logger.error(str(e))
                new_size = 0
            logger.debug(f"New size {human_size(new_size)}, was {human_size(old_size)}")
            if new_size:
                if new_size < old_size:
                    safe_move(shrunkfile, srcfile)
                    _ = setperm(srcfile)
                else:
                    remove_file(shrunkfile)

        if CONFIG.get_bool('SWAP_COVERPAGE') and cover > 1:
            coverswap(srcfile, cover)

        if tag:
            tags = {}
            cnt = 1
            for item in get_list(genres):
                tags[f'/Genre_{cnt}'] = item
                cnt += 1
            _ = write_pdf_tags(srcfile, title, issue, tags)

        safe_move(srcfile, original)
        _ = setperm(original)
    except Exception as e:
        logger.error(str(e))
        return False, str(e)
    return True, ''
