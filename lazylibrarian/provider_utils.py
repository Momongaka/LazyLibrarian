import logging
import os
import time

from lazylibrarian.filesystem import path_isfile, syspath
from lazylibrarian.formatter import md5_utf8


def is_in_cache(expiry: int, hashfilename: str, myhash: str) -> bool:
    """Check if a cache file is valid."""
    if path_isfile(hashfilename):
        cache_modified_time = os.stat(hashfilename).st_mtime
        time_now = time.time()
        if expiry and cache_modified_time < time_now - expiry:
            # Cache entry is too old, delete it
            logger = logging.getLogger(__name__)
            logger.debug(f"Expiring {myhash}")
            os.remove(syspath(hashfilename))
            return False
        return True
    return False


def read_from_cache(hashfilename: str) -> (str, bool):
    """Read a cached API response from disk."""
    source = ''
    with open(syspath(hashfilename), "rb") as cachefile:
        source = cachefile.read()
    return source, True


def get_hashed_filename(cache_location: str, url: str) -> (str, str):
    """Generate a hashed filename for caching."""
    myhash = md5_utf8(url)
    hashfilename = os.path.join(cache_location, myhash[0], myhash[1], f"{myhash}.xml")
    return hashfilename, myhash
