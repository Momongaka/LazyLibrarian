#  This file is part of Lazylibrarian.
#
# Purpose:
#    Handles logging configuration, stored in logging.yaml but with some
#    overrides from config.ini

import glob
import logging
import logging.config
import logging.handlers
import os

from lazylibrarian.configenums import OnChangeReason
from lazylibrarian.filesystem import DIRS, splitext, syspath


class RecentMemoryHandler(logging.handlers.MemoryHandler):
    """ Memory handler that only empties if it's asked to; flushing simply
    makes sure it doesn't go beyond capacity """

    def __init__(self, capacity):
        super().__init__(capacity)
        self.name = 'LazyLibrarian In-Memory Log'

    def flush(self):
        self.acquire()
        try:
            while len(self.buffer) > self.capacity:
                del self.buffer[0]  # Just delete the last entry
        finally:
            self.release()

    def clear(self):
        self.acquire()
        try:
            self.buffer.clear()
        finally:
            self.release()


class RedactFilter(logging.Filter):
    """ Filter used to remove sensitive information from logs """

    def __init__(self):
        super().__init__()
        self.redacted = 0
        self.redactset: set[str] = set()

    def update_redactlist(self, redactlist: list[str]):
        # Store the list as a set to make it more efficient to use
        self.redactset = set(redactlist)

    def filter(self, record: logging.LogRecord):
        """ Filter is called for every log message when redact is on """
        changed = False
        if hasattr(record, 'message'):
            for word in self.redactset:
                if word in record.message:
                    record.message = record.message.replace(word, '[redacted]')
                    changed = True
        elif hasattr(record, 'msg'):
            for word in self.redactset:
                if word in record.msg:
                    record.msg = record.msg.replace(word, '[redacted]')

        if changed:
            self.redacted += 1
            # This is a hack: The RotatingFileHandler recalculates message twice after filtering, this prevents that :(
            record.msg = record.message
            record.args = None
        return True


class LogConfig:
    """ Central log configuration for LazyLibrarian. Mainly to hold the in-memory
    log handler necessary to display the log in the UI. """

    DefaultConfig = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "simple": {
                "format": "%(asctime)s %(levelname)s: %(message)s [%(module)s.py:%(lineno)s (%(threadName)s)]"
            },
            "detail": {
                "format": "%(asctime)s %(levelname)s  %(filename)s.%(funcName)s(): %(message)s (%(threadName)s)"
            },
            "timing": {
                "format": "%(asctime)s %(threadName)s %(levelname)s, %(filename)s.%(funcName)s() "
                          "(line %(lineno)s): %(message)s"
            },
            "special": {
                "format": "%(asctime)s %(levelname)s: %(message)s [%(module)s.py:%(lineno)s (%(threadName)s/%(name)s)]"
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "DEBUG",
                "formatter": "simple",
                "stream": "ext://sys.stdout",
            },
            "logfile": {
                "class": "logging.handlers.RotatingFileHandler",
                "level": "DEBUG",
                "formatter": "simple",
                "maxBytes": 10485760,
                "backupCount": 5,
                "encoding": "utf8",
            },
        },
        "loggers": {
            "special": {"level": "DEBUG"},
            "special.admin": {"level": "DEBUG"},
            "special.cache": {"level": "DEBUG"},
            "special.configread": {"level": "DEBUG"},
            "special.configwrite": {"level": "DEBUG"},
            "special.dbcomms": {"level": "DEBUG"},
            "special.dlcomms": {"level": "DEBUG"},
            "special.fileperms": {"level": "DEBUG"},
            "special.fuzz": {"level": "DEBUG"},
            "special.grsync": {"level": "DEBUG"},
            "special.iterateproviders": {"level": "DEBUG"},
            "special.libsync": {"level": "DEBUG"},
            "special.matching": {"level": "DEBUG"},
            "special.postprocess": {"level": "DEBUG"},
            "special.requests": {"level": "DEBUG"},
            "special.searching": {"level": "DEBUG"},
            "special.serverside": {"level": "DEBUG"},
            "special.cherrypy": {"level": "INFO", "propagate": False},
            "unittest": {"level": "INFO"},
        },
        "root": {"handlers": ["console", "logfile"]},
    }

    StartupLoggerConfig = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "simple": {
                "format": "%(asctime)s %(levelname)s: %(message)s [%(module)s.py:%(lineno)s (%(threadName)s/%(name)s)]"
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": "DEBUG",
                "formatter": "simple",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "special": {
                "level": "DEBUG",
            },
        },
        "root": {"handlers": ["console"]},
    }

    basefilename = 'lazylibrarian.log'
    _memorybuffer: RecentMemoryHandler

    def __init__(self):
        self._memorybuffer = None
        self.redact_filter = RedactFilter()  # Need just one instance
        logging.basicConfig()  # Make sure there is a valid root logger
        self.ensure_memoryhandler_for_ui(capacity_lines=400, redact=False)

    def get_default_logconfig(self, console_only: bool):
        """ Get the default log config for either console-only or everything """
        return self.StartupLoggerConfig if console_only else self.DefaultConfig

    def initialize_console_only_log(self, redact: bool | None) -> dict:
        """ Initialize the console-only log used for startup """
        settings = self.get_default_logconfig(console_only=True)
        logging.config.dictConfig(settings)
        self.ensure_memoryhandler_for_ui(capacity_lines=-1, redact=redact)
        return settings

    def initialize_log_config(self, max_size: int, max_number: int, redactui: bool, redactfiles: bool) -> dict:
        """ Apply a fresh configuration """
        settings = self.get_default_logconfig(console_only=False)
        # Apply LOGDIR and LOGSIZE to all file-based loggers
        for _name, handler in settings['handlers'].items():
            if 'FileHandler' in handler['class']:
                handler['filename'] = self.get_full_filename(self.basefilename, redactfiles)
                handler['maxBytes'] = max_size
                handler['backupCount'] = max_number
        # Set the configuration
        logging.config.dictConfig(settings)
        self.ensure_memoryhandler_for_ui(capacity_lines=-1, redact=redactui)
        # Make sure all special loggers are initialized
        for name in settings['loggers']:
            _ = logging.getLogger(name)

        self.set_file_redact_filter(redactfiles)
        return settings

    # Methods for dealing with in-memory log for UI display

    def ensure_memoryhandler_for_ui(self, capacity_lines, redact: bool | None):
        """ Ensure there is a memory handler for displaying the log in the UI.
         If capacity_lines is > 0, set the capacity, otherwise leave as-is. """
        logger = logging.getLogger()
        if self._memorybuffer:
            if capacity_lines > 0:
                self._memorybuffer.capacity = capacity_lines
        else:
            self._memorybuffer = RecentMemoryHandler(capacity=capacity_lines)

        self._memorybuffer.removeFilter(self.redact_filter)
        if redact:
            self._memorybuffer.addFilter(self.redact_filter)
        # Make sure it's part of the root logger
        logger.addHandler(self._memorybuffer)

    def set_file_redact_filter(self, redact: bool | None):
        """ Apply the redact filter to all handlers, if redact is true """
        if redact is None:
            return  # No change

        root = logging.getLogger()
        for handler in root.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()
                handler.removeFilter(self.redact_filter)
                handler.baseFilename = self.get_full_filename(self.basefilename, redact)
                if redact:
                    handler.addFilter(self.redact_filter)
                    handler.mode = 'w'  # Start new file
                else:
                    handler.mode = 'a'  # Append to existing file

    def get_ui_loghandler(self) -> RecentMemoryHandler:
        return self._memorybuffer

    def clear_ui_log(self):
        self._memorybuffer.clear()

    def get_ui_logrows(self, filterstr: str = '') -> (list, int):
        """ Return log rows to show in the UI, filtered by lowercase(filter), as
         well as the total number of items that could be displayed """
        filterstr = filterstr.lower() if filterstr else ''

        rows = []
        handler = self.get_ui_loghandler()
        for logrec in handler.buffer:
            if hasattr(logrec, 'message'):  # If not, it's a sign it was added to the log without a formatter
                # Timestamp, level, thread, file, method, line, message
                line = [logrec.asctime, logrec.levelname, logrec.threadName, logrec.filename, logrec.funcName,
                        logrec.lineno, logrec.message]
                if not filterstr or filterstr in str(line).lower():
                    rows.append(line)
        return rows, len(handler.buffer)

    #
    # Event handlers for when CONFIG changes - onchange methods
    #

    @staticmethod
    def change_memory_limit(limitstr: str, reason: OnChangeReason = OnChangeReason.SETTING):
        """ Method used as onchange event for LOGLIMIT """
        if reason != OnChangeReason.COPYING:
            LOGCONFIG.ensure_memoryhandler_for_ui(capacity_lines=int(limitstr), redact=None)

    # noinspection PyUnusedLocal
    @staticmethod
    def change_loguiredact(redactstr: str, reason: OnChangeReason = OnChangeReason.SETTING):
        """ The LOGREDACT setting changes """
        redact = redactstr and redactstr in ['True', '1', 'TRUE', 'On']
        LOGCONFIG.ensure_memoryhandler_for_ui(capacity_lines=-1, redact=redact)

    @staticmethod
    def change_logfileredact(redactstr: str, reason: OnChangeReason = OnChangeReason.SETTING):
        """ The LOGFILEREDACT setting changes """
        if reason != OnChangeReason.COPYING:
            logger = logging.getLogger(__name__)
            redact = redactstr and redactstr in ['True', '1', 'TRUE', 'On']
            # Add a final message to the no-longer-active log
            if redact:
                logger.info('Switching to REDACTED logging.')
            else:
                logger.info('Switching to unredacted logging.')
            LOGCONFIG.set_file_redact_filter(redact)
            if redact:
                logger.info('Started REDACTED logging.')
            else:
                logger.info('Started unredacted logging.')

    #
    # Methods for dealing with normal loggers
    #

    @staticmethod
    def change_root_loglevel(value: str, reason: OnChangeReason = OnChangeReason.SETTING):
        """ Onchange event for LOGLEVEL """
        if reason != OnChangeReason.COPYING:
            logger = logging.getLogger()
            try:
                level = int(value)
                # Translate prior log level to standard scheme:
                if level < 10 or level > 50:
                    oldlevel = level
                    if oldlevel == 0:
                        level = logging.ERROR  # 40
                    elif oldlevel == 1:
                        level = logging.INFO  # 20
                    elif oldlevel >= 2:
                        level = logging.DEBUG  # 10
                    logger.warning(f"Translating prior LOGLEVEL of {oldlevel} to {level}, which is the new value.")
            except ValueError:
                level = logging.getLevelName(value.upper())
            logger.info(f"Changing loglevel to {level}")
            logger.setLevel(level)

        if value.isdigit() and int(value) > 10 or value.isupper() and value != 'DEBUG':
            # when you go out of debug mode, disable all special logs
            # TODO: Ideally we would re-enable the ones in LOGSPECIALDEBUG when debug logging resumes
            LogConfig.enable_only_these_special_debuglogs("")

    @staticmethod
    def enable_logger(logname: str, enabled: bool = True) -> logging.Logger:
        """ Enable/disable the logger named logname, return the logger """
        logger = logging.getLogger(logname)
        # Use the disabled property of the logger, though the logic is reversed
        logger.disabled = not enabled
        return logger

    @staticmethod
    def get_loglevel(logname: str = '') -> int:
        return logging.getLogger(logname).getEffectiveLevel()

    @staticmethod
    def get_loglevel_name(logname: str = '') -> str:
        return logging.getLevelName(LogConfig.get_loglevel(logname))

    @staticmethod
    def set_loglevel(level: int = logging.INFO, logname: str = '') -> int:
        """ Set the log level for the logger, return the effective log level """
        logger = logging.getLogger(logname)
        logger.setLevel(level)
        return logger.getEffectiveLevel()

    @staticmethod
    def is_logger_enabled_for(name: str, level: int = logging.DEBUG) -> bool:
        logger = logging.getLogger(name)
        return logger.isEnabledFor(level)

    @staticmethod
    def remove_console_handlers_from_logger(name: str) -> int:
        """ Remove all handlers named console* from the logger named name.
        Returns number of handlers removed. Not reversible without reloading config. """
        logger = logging.getLogger(name)
        count = 0
        for handler in logger.handlers:
            if handler.get_name().startswith('console'):
                logger.removeHandler(handler)
                count += 1
        return count

    @staticmethod
    def remove_console_handlers() -> int:
        """ Called on --quiet, to make sure all LL handlers named console* are removed """
        # The only predefined ones are in root and special:
        removed = LogConfig.remove_console_handlers_from_logger('') + \
            LogConfig.remove_console_handlers_from_logger('special')
        return removed

    # Methods for dealing with special loggers

    @staticmethod
    def get_special_logger_list() -> list:
        """ Get the list of special loggers and their current state """
        loggers = [
            logging.getLogger(name)
            for name in logging.Logger.manager.loggerDict
            if name.startswith("special.")
        ]
        return loggers

    @staticmethod
    def is_special_logger_enabled(shortname: str) -> bool:
        return LogConfig.is_logger_enabled_for(f'special.{shortname}', logging.DEBUG)

    @staticmethod
    def enable_special_logger(shortname: str, enabled: bool) -> logging.Logger:
        logger = LogConfig.enable_logger(f'special.{shortname}', enabled)
        if enabled:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)
        return logger

    @staticmethod
    def get_short_special_logger_name(fullname: str) -> str:
        """ Return the name of the logger without 'special.' at the beginning """
        return fullname[8:]

    @staticmethod
    def enable_only_these_special_debuglogs(speciallist: str):
        """ Uses the LOGSPECIALDEBUG setting. The speciallist string is expected to hold a comma-separated
        list of 'short names' for special loggers, like 'fuzz, configwrite'
        All special loggers in the list will be enabled, all others disabled """
        specialsenabled = [item.strip() for item in speciallist.split(',')]
        allspecials = LogConfig.get_special_logger_list()
        for logger in allspecials:
            shortname = LogConfig.get_short_special_logger_name(logger.name)
            enableit = shortname in specialsenabled
            logger = LogConfig.enable_special_logger(shortname=shortname, enabled=enableit)
            if enableit:
                logger.debug(f'Enabling logging with special logger {logger.name}')
            if shortname == 'cherrypy':
                # Cherrypy logger gets special treatment as it has its own logger we need to control
                cherrypylogger = logging.getLogger('cherrypy')
                if enableit:
                    cherrypylogger.disabled = False
                    cherrypylogger.propagate = True
                    cherrypylogger.setLevel(logging.DEBUG)
                else:
                    cherrypylogger.disabled = True
                    cherrypylogger.propagate = False

    # Other methods for log management

    def redact_list_updated(self, redactlist: list[str]):
        self.redact_filter.update_redactlist(redactlist)

    @staticmethod
    def get_full_filename(filename: str, redact: bool | None) -> str:
        """ Return the fully qualified log file name that uses filename as the basis.
        If redact is true, insert '-redacted' in the name. """
        if redact:
            justfilename = os.path.basename(filename)
            basefilename, ext = splitext(justfilename)
            filename = f"{basefilename}-redacted{ext}"
        return DIRS.get_logfile(filename)

    def get_redacted_logfilenames(self) -> list[str]:
        """ Return list of redacted log file names currently in use """
        names = []
        logger = logging.getLogger()
        for handler in logger.handlers:
            if (isinstance(handler, logging.FileHandler) and
                    any(afilter == self.redact_filter for afilter in handler.filters)):
                names.append(handler.baseFilename)
        return names

    @staticmethod
    def delete_log_files(logdir: str) -> str:
        """ Delete on-disc log files, return status string """
        # Close all file-based handlers owned by LL
        logger = logging.getLogger()
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()
        logger = logging.getLogger('special')
        for handler in logger.handlers:
            if isinstance(handler, logging.FileHandler):
                handler.close()

        # Delete everything in the LOGDIR
        error = False
        deleted = 0
        for f in glob.glob(f"{logdir}/*.log*"):
            try:
                os.remove(syspath(f))
                deleted += 1
            except OSError as err:
                error = err.strerror
                logger.debug(f"Failed to remove {f} : {error}")

        # Let the user know what happened
        if deleted == 0:
            if error:
                return f'Failed to clear logfiles: {error}'
            return 'No log files to delete'
        if error:
            return f"{deleted} log file(s) deleted from {logdir}. An error also occurred: {error}"
        return f"{deleted} log file(s) deleted from {logdir}"


# Global access variable
LOGCONFIG = LogConfig()
