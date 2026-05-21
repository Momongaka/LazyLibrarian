#  This file is part of Lazylibrarian.
#
# Purpose:
#    Contain core functions that access the filesystem, as well as
#    core global variables containing references to files
# Constraint:
#    Should not depend on any other LazyLibrarian files, except ConfigDict

import logging
import os
import shutil
import sys
import traceback
from datetime import datetime

import lazylibrarian
from lazylibrarian.configtypes import ConfigDict
from lazylibrarian.formatter import get_list, make_bytestr, make_unicode, replace_all, unaccented
from lazylibrarian.processcontrol import get_info_on_caller


class DirectoryHolder:
    """ Holds all the global directories used by LL """
    DATADIR: str  # Where LL stores its data files
    CACHEDIR: str = ''  # Where LL stores its cache
    TMPDIR: str  # Where LL will store temporary files
    FULL_PATH: str  # Fully qualified name of executable running
    PROG_DIR = ''  # The path of LazyLibrarian
    DBFILENAME = 'lazylibrarian.db'
    ARGS: str  # Command line arguments
    config: ConfigDict  # A reference to the config being used
    logger: logging.Logger
    permlogger: logging.Logger

    def __init__(self):
        self.DATADIR = ''
        self.CACHEDIR = ''
        self.TMPDIR = ''
        self.tmpsequence = 0
        self.initialize_logger()

    def initialize_logger(self):
        self.logger = logging.getLogger(__name__)
        self.permlogger = logging.getLogger('special.fileperms')

    def set_fullpath_args(self, fullpath: str, args: str):
        """ Sets the full path of the main program file, plus cmdline args """
        self.FULL_PATH = fullpath
        self.ARGS = args
        self.PROG_DIR = os.path.dirname(self.FULL_PATH)

    def set_datadir(self, datadir: str):
        """ Sets the DATADIR from config, and exits the program if it cannot be created or is not writeable.
        This also updates CACHEDIR and TMPDIR """
        ok, msg = self.ensure_dir_is_writeable(datadir)
        if not ok:
            raise SystemExit(f'{msg} Exiting.')
        self.DATADIR = datadir
        self.CACHEDIR = self.ensure_data_subdir('cache')
        self.TMPDIR = self.ensure_data_subdir('tmp')

    @staticmethod
    def ensure_dir_is_writeable(dirname: str) -> tuple[bool, str]:
        if not path_isdir(dirname):
            try:
                os.makedirs(dirname)
            except OSError:
                return False, f'Could not create directory: {dirname}.'
        if not os.access(dirname, os.W_OK):
            return False, f'Cannot write to the directory: {dirname}.'

        return True, 'ok'

    def ensure_data_subdir(self, subdir: str) -> str:
        """ Returns writeable directory. Tries to make it subdir, but falls back to DATADIR if needed """
        dirname = os.path.join(self.DATADIR, subdir)
        ok, msg = self.ensure_dir_is_writeable(dirname)
        if not ok:
            self.logger.error(msg)
            self.logger.warning(f'Falling back to {self.DATADIR} for {subdir}')
            return self.DATADIR
        return dirname

    def ensure_cache_dir(self):
        """ Make sure the CACHEDIR is not empty """
        if self.CACHEDIR == '':
            self.CACHEDIR = self.ensure_data_subdir('cache')

    def ensure_log_dir(self) -> str:
        """ Make sure the LOGDIR is set correctly, and is writeable """
        ok = False
        if self.config['LOGDIR'] != '':
            ok, _ = self.ensure_dir_is_writeable(self.config['LOGDIR'])
        if not ok:  # Can't make configured logdir writable, pick another one
            self.config['LOGDIR'] = self.ensure_data_subdir('Logs')
        return self.config['LOGDIR']

    def set_config(self, config: ConfigDict):
        self.config = config

    def get_mako_cachedir(self):
        """ Return the name of the mako cache dir """
        return os.path.join(self.CACHEDIR, 'mako')

    def get_dbfile(self):
        """ Return the name of the LL database file """
        return os.path.join(self.DATADIR, self.DBFILENAME)

    def get_logfile(self, filename: str) -> str:
        """ Return the full name of filename in the LOG directory """
        return os.path.join(self.config['LOGDIR'], filename)

    def get_tmpfilename(self, base: str | None = None) -> str:
        """ Get a file named base in the tmp directory.
        If base is not specified, return a unique filename """
        if not base:
            timestr = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            randomstr = str(self.tmpsequence)
            self.tmpsequence += 1
            if self.tmpsequence < 0:
                self.tmpsequence = 0
            base = f'LL-temp-{timestr}-{randomstr}.tmp'
        return syspath(os.path.join(self.TMPDIR, base))

    def get_cachedir(self, subdir: str) -> str:
        """ Return a writeable dir named subdir, off the cache directory"""
        return os.path.join(self.CACHEDIR, subdir)

    def get_cachefile(self, subdir: str, filename: str) -> str:
        """ Get the name of a file in the cache, in subdir, named filename """
        return os.path.join(self.CACHEDIR, subdir, filename)

    def get_testdatafile(self, filename):
        """ Get the full name of a file in the testdata directory """
        return os.path.join(self.PROG_DIR, 'testdata', filename)


# Global access to directories
DIRS = DirectoryHolder()

# Global to suppress repeat warnings
_OPFWARN = ''


# PATH FUNCTIONS

def path_isfile(name: str) -> bool:
    return os.path.isfile(syspath(name))


def path_isdir(name: str) -> bool:
    return os.path.isdir(syspath(name))


def path_exists(name: str) -> bool:
    return os.path.exists(syspath(name))


def path_islink(name: str) -> bool:
    return os.path.islink(syspath(name))


def splitext(name: str) -> tuple[str, str]:
    base, extn = os.path.splitext(name)
    if extn.startswith('. '):
        return name, ''
    return base, extn


WINDOWS_MAGIC_PREFIX = '\\\\?\\'


def syspath(path: str | bytes, prefix: bool = True) -> str:
    """Convert a path for use by the operating system. In particular,
    paths on Windows must receive a magic prefix and must be converted
    to Unicode before they are sent to the OS. To disable the magic
    prefix on Windows, set `prefix` to False---but only do this if you
    *really* know what you're doing.
    """
    DIRS.permlogger.debug(f"{os.path.__name__}:{sys.version[0:5]} [{repr(path)}] {isinstance(path, str)}")

    if os.path.__name__ != 'ntpath':
        return path

    if not isinstance(path, str):
        # Beets currently represents Windows paths internally with UTF-8
        # arbitrarily. But earlier versions used MBCS because it is
        # reported as the FS encoding by Windows. Try both.
        try:
            # if path is _not_ a string, ie bytes
            # noinspection PyUnresolvedReferences
            path = path.decode('utf-8')
        except UnicodeError:
            # The encoding should always be MBCS, Windows' broken
            # Unicode representation.
            encoding = sys.getfilesystemencoding() or sys.getdefaultencoding()
            path = path.decode(encoding, 'replace')

    if 1 < len(path) < 4 and path[1] == ':':  # it's just a drive letter (E: or E:/)
        return path

    # the html cache addressing uses forwardslash as a separator but Windows file system needs backslash
    s = path.find(DIRS.CACHEDIR)
    if s >= 0 and '/' in path:
        path = path.replace('/', '\\')
        # logger.debug("cache path changed [%s] to [%s]" % (opath, path))

    if not path.startswith('.') and prefix and not path.startswith(WINDOWS_MAGIC_PREFIX):
        # Don't affect relative paths
        # Add the magic prefix if it isn't already there.
        # http://msdn.microsoft.com/en-us/library/windows/desktop/aa365247.aspx
        if path.startswith('\\\\'):
            # UNC path. Final path should look like \\?\UNC\...
            path = f"UNC{path[1:]}"
        path = WINDOWS_MAGIC_PREFIX + path

    return path


def remove_file(name: str) -> bool:
    """ Remove the file. On error, log an error message. Returns True if successful """
    logger = logging.getLogger(__name__)
    ok = False
    try:
        os.remove(syspath(name))
        ok = True
    except OSError as err:
        if err.errno == 2:  # does not exist is ok
            pass
        else:
            logger.warning(f"Failed to remove {name} : {err.strerror}")
    except Exception as err:
        logger.warning(f"Failed to remove {name} : {str(err)}")
    return ok


def remove_dir(name: str, remove_contents: bool = False) -> bool:
    """ Remove the directory. On error, log an error message. Returns True if successful """
    logger = logging.getLogger(__name__)
    ok = False
    try:
        if remove_contents:
            shutil.rmtree(syspath(name))  # , ignore_errors=True)
        else:
            os.rmdir(syspath(name))
        logger.debug(f"Removed {syspath(name)}")
        ok = True
    except OSError as err:
        if err.errno == 2:  # does not exist is ok
            pass
        else:
            logger.warning(f"Failed to remove {name} : {err.strerror}")
    except Exception as err:
        logger.warning(f"Failed to remove {name} : {str(err)}")
    return ok


def listdir(name: str):
    """
    listdir ensuring bytestring for unix,
    so we don't baulk if filename doesn't fit utf-8 on return
    and ensuring utf-8 and adding path requirements for windows
    All returns are unicode
    """
    if os.path.__name__ == 'ntpath':
        dname = syspath(name)
        if not dname.endswith('\\'):
            dname += '\\'
        try:
            return os.listdir(dname)
        except Exception as err:
            logger = logging.getLogger(__name__)
            logger.error(f"Listdir [{name}][{dname}] failed: {str(err)}")
            return []

    return [make_unicode(item) for item in os.listdir(make_bytestr(name))]


def setperm(file_or_dir) -> bool:
    """
    Force newly created directories to rwxr-xr-x and files to rw-r--r--
    or other value as set in config
    """
    logger = logging.getLogger(__name__)
    if not file_or_dir:
        return False

    if path_isdir(file_or_dir):
        perm = octal(DIRS.config['DIR_PERM'], 0o755)
    elif path_isfile(file_or_dir):
        perm = octal(DIRS.config['FILE_PERM'], 0o644)
    else:
        # not a file or a directory (symlink?)
        return False

    want_perm = oct(perm)[-3:].zfill(3)
    st = os.stat(syspath(file_or_dir))
    old_perm = oct(st.st_mode)[-3:].zfill(3)
    if old_perm == want_perm:
        DIRS.permlogger.debug(f"Permission for {file_or_dir} is already {want_perm}")
        return True

    try:
        os.chmod(syspath(file_or_dir), perm)
    except Exception as err:
        logger.debug(f"Error setting permission {want_perm} for {file_or_dir}: {type(err).__name__} {str(err)}")
        return False

    st = os.stat(syspath(file_or_dir))
    new_perm = oct(st.st_mode)[-3:].zfill(3)

    if new_perm == want_perm:
        DIRS.permlogger.debug(f"Set permission {want_perm} for {file_or_dir}, was {old_perm}")
        return True
    if os.name == 'nt':
        logger.debug(f"Windows can't set permission {want_perm} for {file_or_dir}; this is expected")
        return True
    logger.debug(f"Failed to set permission {want_perm} for {file_or_dir}, got {new_perm}")
    return False


def octal(value, default: int) -> int:
    """ Return value as int, if it's a valid base-8 number, otherwise default """
    if not value:
        return default
    try:
        value = int(str(value), 8)
        return value
    except ValueError:
        return default


def make_dirs(dest_path, new=False) -> bool:
    """ os.makedirs only seems to set the right permission on the final leaf directory
        not any intermediate parents it creates on the way, so we'll try to do it ourselves
        setting permissions as we go. Could use recursion but probably aren't many levels to do...
        Build a list of missing intermediate directories in reverse order, exit when we encounter
        an existing directory or hit root level. Set permission on any directories we create.
        If new, try to remove any pre-existing directory and contents.
        return True or False """

    to_make = []
    dest_path = syspath(dest_path)
    if new:
        shutil.rmtree(dest_path, ignore_errors=True)

    while not path_isdir(dest_path):
        # noinspection PyUnresolvedReferences
        to_make.insert(0, dest_path)
        parent = os.path.dirname(dest_path)
        if parent == dest_path:
            to_make.pop(0)
            break
        dest_path = parent

    for entry in to_make:
        DIRS.permlogger.debug(f"mkdir: [{repr(entry)}]")
        try:
            os.mkdir(entry)  # mkdir uses umask, so set perm ourselves
            _ = setperm(entry)  # failing to set perm might not be fatal
        except OSError as why:
            # os.path.isdir() has some odd behaviour on Windows, says the directory does NOT exist
            # then when you try to mkdir complains it already exists.
            # Ignoring the error might just move the problem further on?
            # Something similar seems to occur on Google Drive filestream
            # but that returns Error 5 Access is denied
            # Trap errno 17 (linux file exists) and 183 (windows already exists)
            if why.errno in [17, 183]:
                DIRS.permlogger.debug(f"Ignoring mkdir already exists errno {why.errno}: [{repr(entry)}]")
            elif 'exists' in str(why):
                DIRS.permlogger.debug(f"Ignoring {why}: [{repr(entry)}]")
            else:
                logger = logging.getLogger(__name__)
                logger.error(f'Unable to create directory {why}: [{repr(entry)}]')
                return False
    return True


def safe_move(src, dst, action='move'):
    """ Move or copy or symlink src to dst
        Retry without accents if unicode error as some file systems can't handle (some) accents
        Retry with some characters stripped if bad filename
        e.g. Windows can't handle <>?"*:| (and maybe others) in filenames
        Return (new) dst if success """

    if src == dst:  # nothing to do
        return dst

    logger = logging.getLogger(__name__)
    logger.debug(f"{action}:[{src}]:[{dst}]")
    while action:  # might have more than one problem...
        try:
            if action == 'copy':
                shutil.copyfile(syspath(src), syspath(dst))
            elif action == 'symlink':
                os.symlink(syspath(src), syspath(dst))
            elif path_isdir(src) and dst.startswith(src):
                _ = copy_tree(syspath(src), syspath(dst))
            else:
                shutil.move(syspath(src), syspath(dst))
            return dst

        except UnicodeEncodeError:
            newdst = unaccented(dst)
            if newdst != dst:
                dst = newdst
            else:
                raise

        except OSError as err:  # both needed for different python versions
            if err.errno == 22:  # bad mode or filename
                drive, path = os.path.splitdrive(dst)
                logger.debug(f"drive=[{drive}] path=[{path}]")
                # strip some characters windows can't handle
                newpath = replace_all(path, lazylibrarian.DICTS.get('filename_dict', {}))
                # windows filenames can't end in space or dot
                while newpath and newpath[-1] in '. ':
                    newpath = newpath[:-1]
                # anything left? has it changed?
                if newpath and newpath != path:
                    dst = os.path.join(drive, newpath)
                    logger.debug(f"dst=[{dst}]")
                else:
                    raise
            else:
                raise
        except Exception:
            raise
    return dst


def safe_copy(src, dst):
    return safe_move(src, dst, action='copy')


def any_file(search_dir: str, extn: str) -> str:
    """ Find a file with specified extension in a directory, any will do.
    Return full pathname of file, or empty string if none found """
    if search_dir is None or extn is None:
        return ""
    if path_isdir(search_dir):
        for fname in listdir(search_dir):
            if fname.endswith(extn):
                return os.path.join(search_dir, fname)
    return ""


def opf_file(search_dir: str) -> str:
    global _OPFWARN
    """ Look for .opf files in search_dir, returning the file name.
    If metadata.opf exists and no other opf file does, return metatadata.
    If metadata.opf and another .opf file exists, return the other one.
    If two or more other .opf files exist, we don't know which one to use.
    Warn and return any"""
    cnt = 0
    res = ''
    meta = ''
    if path_isdir(search_dir):
        for fname in listdir(search_dir):
            if fname.endswith('.opf'):
                if fname == 'metadata.opf':
                    meta = os.path.join(search_dir, fname)
                else:
                    res = os.path.join(search_dir, fname)
                cnt += 1
        if cnt > 2 or cnt == 2 and not meta:
            logger = logging.getLogger(__name__)
            program, method, lineno = get_info_on_caller(depth=1)
            warn = f"{program}:{method}:{lineno} Found {cnt} conflicting opf in {search_dir}"
            if warn != _OPFWARN:
                logger.debug(warn)
                _OPFWARN = warn
        elif res:  # prefer bookname.opf over metadata.opf
            return res
        elif meta:
            return meta
    return res


def bts_file(search_dir: str) -> str:
    """ Find the first .bts file in search_dir, unless .bts files are 'skipped' in config """
    if 'bts' in DIRS.config['SKIPPED_EXT']:
        return ''
    return any_file(search_dir, '.bts')


def csv_file(search_dir: str, library: str) -> str:
    """ Returns the first CSV file in search_dir that matches the library name.
    The library name is 'eBook', 'AudioBook', etc.
    If library name is blank, just return the first CSV file. """
    if search_dir and path_isdir(search_dir):
        try:
            for fname in listdir(search_dir):
                if fname.endswith('.csv') and (not library or library.lower() in fname.lower()):
                    return os.path.join(search_dir, fname)
        except Exception as err:
            logger = logging.getLogger(__name__)
            logger.warning(f'Listdir error [{search_dir}]: {type(err).__name__} {str(err)}')
    return ''


def jpg_file(search_dir: str) -> str:
    """ Returns the name of the first .jpg file in search_dir """
    return any_file(search_dir, '.jpg')


def book_file(search_dir: str, booktype: str, config: ConfigDict, recurse=False) -> str:
    """ Find the first book/mag file in this directory (tree), any book will do.
    Return full pathname of book/mag as bytes, or empty bytestring if none found
    """
    if booktype == '':
        return ""

    if path_isdir(search_dir):
        logger = logging.getLogger(__name__)
        if recurse:
            # noinspection PyBroadException
            try:
                for r, _, f in os.walk(search_dir):
                    for item in f:
                        if config.is_valid_booktype(item, booktype=booktype):
                            return os.path.join(r, item)
            except Exception:
                logger.error(f'Unhandled exception in book_file: {traceback.format_exc()}')
        else:
            # noinspection PyBroadException
            try:
                for fname in listdir(search_dir):
                    if config.is_valid_booktype(fname, booktype=booktype):
                        return os.path.join(make_unicode(search_dir), fname)
            except Exception:
                logger.error(f'Unhandled exception in book_file: {traceback.format_exc()}')
    return ""


def get_directory(dirname):
    usedir = ''
    if dirname == "eBook":
        usedir = DIRS.config['EBOOK_DIR']
    elif dirname == "AudioBook" or dirname == "Audio":
        usedir = DIRS.config['AUDIO_DIR']
    elif dirname == "Download":
        try:
            usedir = get_list(DIRS.config.get_str('DOWNLOAD_DIR'), ',')[0]
        except IndexError:
            usedir = ''
    elif dirname == "Alternate":
        usedir = DIRS.config['ALTERNATE_DIR']
    elif dirname == "Testdata":
        usedir = DIRS.config['TESTDATA_DIR']
    else:
        return usedir
    # ./ and .\ denotes relative to program path, useful for testing
    logger = logging.getLogger(__name__)
    if usedir and len(usedir) >= 2 and usedir[0] == "." and (usedir[1] == "/" or usedir[1] == "\\"):
        usedir = f"{DIRS.PROG_DIR}/{usedir[2:]}"
        if os.path.__name__ == 'ntpath':
            usedir = usedir.replace('/', '\\')
    if usedir and not path_isdir(usedir):
        try:
            os.makedirs(syspath(usedir))
            logger.info(f"Created new {dirname} folder: {usedir}")
        except OSError as e:
            logger.warning(f'Unable to create folder {usedir}: {str(e)}, using {DIRS.DATADIR}')
            usedir = DIRS.DATADIR
    if usedir and path_isdir(usedir):
        try:
            with open(syspath(os.path.join(usedir, 'll_temp')), 'w') as f:
                f.write('test')
            os.remove(syspath(os.path.join(usedir, 'll_temp')))
        except Exception as why:
            logger.warning(f"{dirname} dir [{repr(usedir)}] not writeable, using {DIRS.DATADIR}: {str(why)}")
            usedir = syspath(usedir)
            logger.debug(
                f"Folder: {usedir} Mode: {oct(os.stat(usedir).st_mode)} UID: {os.stat(usedir).st_uid} "
                f"GID: {os.stat(usedir).st_gid} W_OK: {os.access(usedir, os.W_OK)} X_OK: {os.access(usedir, os.X_OK)}")
            usedir = DIRS.DATADIR
    else:
        logger.warning(f"{dirname} dir [{repr(usedir)}] not found, using {DIRS.DATADIR}")
        usedir = DIRS.DATADIR

    return make_unicode(usedir)


def copy_tree(src_dir, dest_dir):
    # rolled our own copytree using walk and copyfile as odd permission errors from the library version
    # This should hopefully just work, or if not at least it will show what's going on
    failed = 0
    err = []
    logger = logging.getLogger(__name__)
    for rootdir, _dirnames, filenames in os.walk(src_dir):
        for f in filenames:
            src = os.path.join(rootdir, f)
            dst = src.replace(src_dir, dest_dir, 1)
            got_dir = make_dirs(os.path.dirname(os.path.abspath(dst)))
            if not got_dir:
                failed += 1
                err.append([src, dst, "Failed to create destination folder"])
                continue  # try next file
            try:
                shutil.copyfile(src, dst)
                logger.debug(f"Copied {f}")
            except Exception as e:
                msg = f"Failed to copy {src} to {dst}: {e}"
                err.append([src, dst, f"Copy failed: {e}"])
                logger.error(msg)
                failed += 1
                if not os.path.exists(src):
                    logger.debug(f"{src} does not exist")
                else:
                    st = os.stat(src)
                    readable = os.access(src, os.R_OK)
                    if not readable:
                        err.append([src, dst, f"Cannot read source: {oct(st.st_mode)}"])
                        DIRS.permlogger.debug(f"My uid/gid {os.getuid()}/{os.getgid()}")
                        DIRS.permlogger.debug(f"Cannot read {src}, {oct(st.st_mode)}, {st.st_uid}, {st.st_gid}")
                if not os.path.exists(dst):
                    logger.debug(f"{dst} does not exist")
                else:
                    st = os.stat(dst)
                    writeable = os.access(dst, os.W_OK)
                    if not writeable:
                        err.append([src, dst, f"Cannot write to destination: {oct(st.st_mode)}"])
                        DIRS.permlogger.debug(f"My uid/gid {os.getuid()}/{os.getgid()}")
                        DIRS.permlogger.debug(f"Cannot write to {dst}, {oct(st.st_mode)}, {st.st_uid}, {st.st_gid}")
    return failed, err
