#  This file is part of Lazylibrarian.

#  Lazylibrarian is free software':'you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  Lazylibrarian is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU General Public License
#  along with Lazylibrarian.  If not, see <http://www.gnu.org/licenses/>.

# Purpose:
#   Provider functionality related to process control

import inspect
import logging
import os
import threading
import time
from datetime import timedelta

try:
    # noinspection PyUnresolvedReferences
    import psutil

    PSUTIL = True
except ImportError:
    PSUTIL = False


def get_info_on_caller(depth=1, filenamewithoutpath=True, filenamewithoutext=True) -> (str, str, int):
    """ Return file and module name, plus line number, for the caller that called this.
    To get an even earlier caller, use a higher value for depth. If depth is too high,
    return '', '', 0 to indicate error.
    If filenamewithoutpath, return just the filename, otherwise include the full path.
    If filenamewithoutext, return just the base name, otherwise include the extension. """
    depth += 1  # We want to look at a level deeper than this call
    if len(inspect.stack()) > depth >= 1:
        caller_info = inspect.getframeinfo(inspect.stack()[depth][0])
        filename = os.path.basename(caller_info.filename) if filenamewithoutpath else caller_info.filename
        if filenamewithoutext:
            filename, _ = os.path.splitext(filename)
        caller_function = caller_info.function
        lineno = caller_info.lineno
        return filename, caller_function, lineno
    return '', '', 0


def elapsed_since(start: float) -> str:
    elapsed_time = time.perf_counter() - start
    return str(timedelta(seconds=elapsed_time))


def get_process_memory() -> (bool, int):
    """ Return memory used by process, in bytes """
    if PSUTIL:
        process = psutil.Process(os.getpid())
        return True, process.memory_info().rss
    return False, 0


def get_threads_cpu_percent(p, interval=0.1):
    total_percent = p.cpu_percent(interval)
    total_time = sum(p.cpu_times())
    names = [n.name for n in list(threading.enumerate())]
    percents = [total_percent * ((t.system_time + t.user_time)/total_time) for t in p.threads()]
    return list(zip(names, percents, strict=True))


def get_threads():
    if PSUTIL:
        myproc = psutil.Process(os.getpid())
        return get_threads_cpu_percent(myproc)
    return {'Success': False, 'Data': '', 'Error': {'Code': 501, 'Message': 'Needs psutil module'}}


def track_resource_usage(func):
    # decorator to show memory usage and running time of a function
    # to use, from lazylibrarian.processcontrol import track_resource_usage
    # then decorate the function(s) to track  eg...
    # @track_resource_usage
    # def search_book():
    def wrapper(*args, **kwargs):
        logger = logging.getLogger(__name__)
        ok, mem_before = get_process_memory()
        if ok:
            start = time.perf_counter()
            result = func(*args, **kwargs)
            elapsed_time = elapsed_since(start)
            _, mem_after = get_process_memory()
            logger.debug(
                f"{func.__name__}: memory before: {mem_before:,}, after: {mem_after:,}, consumed: "
                f"{mem_after - mem_before:,}; exec time: {elapsed_time}")
        else:
            logger.debug("psutil is not installed")
            result = func(*args, **kwargs)
        return result

    return wrapper


def get_cpu_use() -> (bool, str):
    """ Returns True if ok, False if it can't get the data.
    If it can, returns CPU usage data for right now as a string. """
    if PSUTIL:
        p = psutil.Process()
        blocking = p.cpu_percent(interval=1)
        nonblocking = p.cpu_percent(interval=None)
        return True, f"Blocking {blocking}% Non-Blocking {nonblocking}% {p.cpu_times()}"
    return False, "Unknown - install psutil"
