#  This file is part of Lazylibrarian.
#
# Purpose:
#   Remove bundled libraries and use a system version if available or installable
#   Only do this if not on docker as we can't modify the contents of a docker container
#   Record the results in a file. Don't keep trying to find/install libraries
# Constraint:
#   Should not depend on any other LazyLibrarian files, only standard python system libraries

import logging
import os
import shutil
import subprocess
import sys
from importlib import invalidate_caches
from importlib.util import resolve_name
from pathlib import Path

DependencyList = tuple[tuple[str, str, str]]
ll_dependencies = (
    # pip name, bundled name, aka
    ('bs4', '', ''),
    ('html5lib', '', ''),
    ('webencodings', '', ''),
    ('requests', '', ''),
    ('chardet', '', ''),
    ('urllib3', '', ''),
    ('pyOpenSSL', '', 'OpenSSL'),
    ('cherrypy', '', ''),
    ('cherrypy_cors', 'cherrypy_cors.py', ''),
    ('httpagentparser', '', ''),
    ('mako', '', ''),
    ('httplib2', '', ''),
    ('Pillow', '', 'PIL'),
    ('apprise', '', ''),
    ('python_magic', 'magic', 'magic'),
    ('RapidFuzz', 'thefuzz', ''),
    ('Levenshtein', '', ''),
    ('deluge_client', '', ''),
    ('irc', '', ''),
)


class ModuleUnbundler:
    """ This class handled unbundling modules from LL, moving to use system
    libraries instead. """

    def __init__(self):
        self.basedir = str(Path(__file__).parent.parent.resolve())
        self.docker = '/config' in sys.argv and sys.argv[0].startswith('/app/')
        self.bypass_file = os.path.join(self.basedir, 'unbundled.libs')
        self.deletable = []

    def prepare_module_unbundling(self, dependencies: DependencyList = ll_dependencies) -> list[str]:
        """ Prepare to unbundle the dependencies passed, stored in subdirs off where basefile resides.
        Returns a list of dependent libraries that can be removed in the process.
        Saves a file called unbundled.libs with these names; if this file exists, this routine does nothing.
        """
        # This file must be in the lazylibrarian/ directory; the parent dir is the main one
        if not self.docker and not os.path.isfile(self.bypass_file):
            bundled, distro = self._get_library_locations(dependencies)
            distro = self._install_missing_libraries(bundled, distro)
            self.deletable = self._calc_libraries_to_delete(dependencies, bundled, distro)
        return self.deletable

    def remove_bundled_modules(self) -> list[str]:
        """ Remove the libraries identified in the prepare step, then add the list to the
        bypass file. Return the list of removed modules. """
        if self.deletable:
            logger = logging.getLogger()
            logger.info(f'Removing {len(self.deletable)} bundled libraries')
            removed = self._delete_libraries(self.deletable)
            with open(self.bypass_file, 'w') as f:
                f.write(str(removed))
            return removed
        return []

    def _get_library_locations(self, dependencies: DependencyList) -> (dict[str, str], dict[str, str]):
        """ Go through dependencies, return two dicts:
        1) A dict of dependencies where we need to use the bundled version, and
        2) a dict of dependencies where we can use a separate installation
        """
        paths = sys.path
        bundled = {}
        distro = {}
        for item in dependencies:
            if item[1] is not None:  # there may be a bundled version
                name = item[0]
                for finder in sys.meta_path:
                    if hasattr(finder, 'find_spec'):
                        spec = finder.find_spec(resolve_name(name, None), None)
                        if spec is not None:
                            if self.basedir in spec.origin:
                                bundled[name] = spec.origin
                            else:
                                distro[name] = spec.origin

        # Look again, but not in the base LL directory where the bundles are
        removed_base = False
        for index, item in enumerate(paths):
            if item == self.basedir:
                paths.pop(index)
                removed_base = True
        try:
            for item in dependencies:
                name = item[2] if item[2] else item[0]
                if name not in distro:
                    for finder in sys.meta_path:
                        if hasattr(finder, 'find_spec'):
                            spec = finder.find_spec(resolve_name(name, None), None)
                            if spec is not None:
                                distro[name] = spec.origin
                                break
        finally:
            if removed_base:
                sys.path.insert(0, self.basedir)
        return bundled, distro

    @staticmethod
    def _install_missing_libraries(bundled: dict[str, str], distro: dict[str, str]) -> dict[str, str]:
        """ Attempt to install the libraries that are in 'bundled' but not in 'distro'.
        Return an updated dict of libraries that can now be unbundled. """
        for item in bundled:
            if item not in distro:
                print(f"Installing {item}")  # TODO: warn about potentially long installs here
                try:
                    res = subprocess.run([sys.executable, '-m', 'pip', 'install', item], check=True,
                                         capture_output=True, text=True).stdout
                    distro[item] = 'new install'
                    print(res)
                    # Tell the loader that things have changed
                    invalidate_caches()
                except subprocess.CalledProcessError as e:
                    print(f'Error: {str(e)}')
        return distro

    @staticmethod
    def _calc_libraries_to_delete(dependencies: DependencyList, bundled: dict[str, str],
                                  distro: dict[str, str]) -> list[str]:
        """ Returns a list of libraries from dependencies that can be deleted, because
        they can be satisfied by a library in the distro list. """
        deletable = []
        for item in dependencies:
            # Unfold item for readability
            (pipname, bundledname, aka) = item
            if aka and aka in bundled or pipname in bundled and (aka and aka in distro or pipname in distro):
                # Is it bundled and could be deleted?
                # Is there a distro version?
                deletable.append(bundledname if bundledname else pipname)
        return deletable

    def _delete_libraries(self, deletable: list[str]) -> list[str]:
        """ Delete all of the libraries in deletable, return a list of those deleted """
        logger = logging.getLogger()
        removed = []
        for item in deletable:
            f = os.path.join(self.basedir, item)
            # might have already been deleted
            if os.path.isdir(f):
                shutil.rmtree(f)
                logger.info(f'Removed bundled dir {item}')
                removed.append(item)
            if os.path.isfile(f):
                os.remove(f)
                logger.info(f'Removed bundled file {item}')
                removed.append(item)
        return removed


UNBUNDLER = ModuleUnbundler()
