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

import os
import sys

try:
    import psutil
except ImportError:
    psutil = None

import cherrypy

try:
    import cherrypy_cors
except ImportError:
    # noinspection PyUnresolvedReferences
    import lib.cherrypy_cors as cherrypy_cors
import logging
from shutil import copyfile

import lazylibrarian
from lazylibrarian.config2 import CONFIG
from lazylibrarian.filesystem import DIRS, path_exists, syspath
from lazylibrarian.logconfig import LOGCONFIG
from lazylibrarian.webServe import WebInterface

try:
    import portend
except ImportError:
    portend = None


def initialize(options=None):
    logger = logging.getLogger(__name__)
    if options is None:
        options = {}
    https_enabled = options['https_enabled']
    https_cert = options['https_cert']
    https_key = options['https_key']
    if options['http_root'] and not options['http_root'].startswith('/'):
        options['http_root'] = f"/{options['http_root']}"

    if https_enabled and (not (path_exists(https_cert) and path_exists(https_key))):
        logger.warning("Disabled HTTPS because of missing certificate and key.")
        https_enabled = False

    options_dict = {
        'log.screen': False,
        'server.thread_pool': 10,
        'server.socket_port': options['http_port'],
        'server.socket_host': options['http_host'],
        'engine.autoreload.on': False,
        'tools.encode.on': True,
        'tools.encode.encoding': 'utf-8',
        'tools.decode.on': True,
        'error_page.401': error_page_401,
        'error_page.403': error_page_403,
        'error_page.404': error_page_404,
    }

    if https_enabled:
        options_dict['server.ssl_certificate'] = https_cert
        options_dict['server.ssl_private_key'] = https_key
        protocol = "https"
    else:
        protocol = "http"

    logger.info(f"Starting LazyLibrarian web server on {protocol}://{options['http_host']}:"
                f"{options['http_port']}{options['http_root']}")
    cherrypy_cors.install()
    cherrypy.config.update(options_dict)

    conf = {
        '/': {
            # 'tools.staticdir.on': True,
            # 'tools.staticdir.dir': os.path.join(DIRS.PROG_DIR, 'data'),
            'tools.staticdir.root': os.path.join(DIRS.PROG_DIR, 'data'),
            'tools.proxy.on': options['http_proxy']  # pay attention to X-Forwarded-Proto header
        },
        '/api': {
            'cors.expose.on': True,
        },
        '/rss_feed': {
            'tools.auth_basic.on': False
        },
        '/interfaces': {
            'tools.staticdir.on': True,
            'tools.staticdir.dir': 'interfaces'
        },
        '/images': {
            'tools.staticdir.on': True,
            'tools.staticdir.dir': 'images'
        },
        '/cache': {
            'tools.staticdir.on': True,
            'tools.staticdir.dir': DIRS.CACHEDIR
        },
        '/css': {
            'tools.staticdir.on': True,
            'tools.staticdir.dir': 'css'
        },
        '/js': {
            'tools.staticdir.on': True,
            'tools.staticdir.dir': 'js'
        },
        '/favicon.ico': {
            'tools.staticfile.on': True,
            # 'tools.staticfile.filename': "images/favicon.ico"
            'tools.staticfile.filename': os.path.join(DIRS.PROG_DIR, 'data', 'images', 'favicon.ico')
        },
        '/opensearch.xml': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': os.path.join(DIRS.CACHEDIR, 'opensearch.xml')
        },
        '/opensearchbooks.xml': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': os.path.join(DIRS.CACHEDIR, 'opensearchbooks.xml')
        },
        '/opensearchcomics.xml': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': os.path.join(DIRS.CACHEDIR, 'opensearchcomics.xml')
        },
        '/opensearchgenres.xml': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': os.path.join(DIRS.CACHEDIR, 'opensearchgenres.xml')
        },
        '/opensearchmagazines.xml': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': os.path.join(DIRS.CACHEDIR, 'opensearchmagazines.xml')
        },
        '/opensearchseries.xml': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': os.path.join(DIRS.CACHEDIR, 'opensearchseries.xml')
        },
        '/opensearchauthors.xml': {
            'tools.staticfile.on': True,
            'tools.staticfile.filename': os.path.join(DIRS.CACHEDIR, 'opensearchauthors.xml')
        },
        '/nzbfile.nzb': {
            'tools.staticfile.on': True,
            'tools.auth_basic.on': False,
            'tools.staticfile.filename': os.path.join(DIRS.CACHEDIR, 'nzbfile.nzb')
        }
    }

    if CONFIG['PROXY_LOCAL']:
        conf['/'].update({
            # NOTE default if not specified is to use apache style X-Forwarded-Host
            # 'tools.proxy.local': 'X-Forwarded-Host'  # this is for apache2
            # 'tools.proxy.local': 'Host'  # this is for nginx
            # 'tools.proxy.local': 'X-Host'  # this is for lighthttpd
            # 'tools.proxy.local': 'X-Forwarded-For' or 'X-Real-IP' # this is for caddy
            'tools.proxy.local': CONFIG['PROXY_LOCAL']
        })
    if options['http_pass'] != "" and not options['user_accounts']:
        logger.info(f"Web server {options['authentication']} authentication is enabled, "
                    f"username is '{options['http_user']}'")
        if options['authentication'] == 'FORM':
            # Set up a sessions based login page instead of using basic auth,
            # using the credentials set for basic auth. Attempting to browse to
            # a restricted page without a session token will result in a
            # redirect to the login page. A sucessful login should then redirect
            # to the originally requested page.
            #
            # Login sessions timeout after 43800 minutes (1 month) unless
            # changed in the config.
            cherrypy.tools.sessions.timeout = options['login_timeout']
            conf['/'].update({
                'tools.sessions.on': True,
                'tools.auth.on': True,
                'auth.forms_username': options['http_user'],
                'auth.forms_password': options['http_pass'],
                # Set all pages to require authentication with 'auth.require': []
                # You can also set auth requirements on a per-method basis by
                # using the @require_auth() decorator on the methods in webserve.py
            })
        elif options['authentication'] == 'BASIC':
            # noinspection PyUnresolvedReferences
            conf['/'].update({
                'tools.auth_basic.on': True,
                'tools.auth_basic.realm': 'LazyLibrarian',
                'tools.auth_basic.checkpassword': cherrypy.lib.auth_basic.checkpassword_dict({
                    options['http_user']: options['http_pass']
                })
            })
        # exempt api, login page and static elements from authentication requirements
        for i in ('/api', '/auth/login', '/css', '/images', '/js', 'favicon.ico', '/rss_feed', '/nzbfile.nzb'):
            if i in conf:
                conf[i].update({'tools.auth.on': False, 'tools.auth_basic.on': False})
            else:
                conf[i] = {'tools.auth.on': False, 'tools.auth_basic.on': False}

    if options['opds_authentication']:
        user_list = {}
        if len(options['opds_username']) > 0:
            user_list[options['opds_username']] = options['opds_password']
        if options['http_pass'] is not None and options['http_user'] != options['opds_username']:
            user_list[options['http_user']] = options['http_pass']
        # noinspection PyUnresolvedReferences
        conf['/opds'] = {'tools.auth_basic.on': True,
                         'tools.auth_basic.realm': 'LazyLibrarian OPDS',
                         'tools.auth_basic.checkpassword': cherrypy.lib.auth_basic.checkpassword_dict(user_list)}
    else:
        conf['/opds'] = {'tools.auth_basic.on': False}

    opensearch = os.path.join(DIRS.PROG_DIR, 'data', 'opensearch.template')
    if path_exists(opensearch):
        with open(syspath(opensearch)) as s:
            data = s.read().splitlines()
        # (title, function)
        for item in [('Authors', 'Authors'),
                     ('Magazines', 'RecentMags'),
                     ('Books', 'RecentBooks'),
                     ('Comics', 'RecentComics'),
                     ('Genres', 'Genres'),
                     ('Series', 'Series')]:
            with open(syspath(os.path.join(DIRS.CACHEDIR, f'opensearch{item[0].lower()}.xml')), 'w') as t:
                for lyne in data:
                    t.write(lyne.replace('{label}', item[0]).replace(
                                      '{func}', f't={item[1]}&amp;').replace(
                                      '{webroot}', options['http_root']))
                    t.write('\n')

    cherrypy.tree.mount(WebInterface(), str(options['http_root']), config=conf)

    if LOGCONFIG.is_logger_enabled_for('special.cherrypy', logging.DEBUG):
        cherrypy.config.update({
            'log.access_file': DIRS.get_logfile('cherrypy.access.log'),
            'log.error_file': DIRS.get_logfile('cherrypy.error.log'),
        })
    lazylibrarian.STOPTHREADS = False
    cherrypy.engine.autoreload.subscribe()

    try:
        if portend:
            portend.Checker().assert_free(str(options['http_host']), options['http_port'])
        cherrypy.server.start()
        copyfile(os.path.join(DIRS.PROG_DIR, 'data', 'images', 'lazylibrarian.png'),
                 os.path.join(DIRS.CACHEDIR, 'alive.png'))
    except Exception as e:
        msg = f"CherryPy failed to start on port {options['http_port']}"
        if psutil:
            for item in psutil.net_connections():
                txt = str(item)
                if f"port={options['http_port']}" in txt and "status='LISTEN'" in txt:
                    user_pid = int(txt.split('pid=')[1].split(')')[0].split(',')[0])
                    if user_pid:
                        process = psutil.Process(user_pid)
                        process_cmd = process.cmdline()
                        msg += f", port appears to be used by pid {user_pid}, {process_cmd}"
        else:
            msg += ' Please install psutil to get more info'
        logger.warning(msg)
        logger.warning(str(e))
        print(msg)
        print(str(e))
        sys.exit(1)

    cherrypy.server.wait()


def error_page_403(status, message, traceback, version):
    """ Custom handler for 403 error """
    return error_page_401(status=status, message=message, traceback=traceback, version=version)


def error_page_404(status, message, traceback, version):
    """ Custom handler for 404 error """
    return error_page_401(status=status, message=message, traceback=traceback, version=version)


# noinspection PyShadowingNames,PyUnusedLocal
def error_page_401(status, message, traceback, version):
    """ Custom handler for 401 error """
    body = f'Error {status}: '
    if str(status) == '401':
        body += 'You need to provide a valid username and password. '
    if message:
        body += message
    title = "I'm not getting out of bed"
    return rf'''
<html>
    <head>
    <STYLE type="text/css">
      H1 {{ text-align: center}}
      H2 {{ text-align: center}}
      H3 {{ text-align: center}}
    </STYLE>
    <h1>LazyLibrarian<br><br></h1>
    <h3><img src="data:image/png;base64,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
OjAwC98hhgAAAABJRU5ErkJggg==" alt="embedded icon" align="middle"><br><br>
    {title}</h3>
    </head>
    <body>
    <br>
    <h2><font color="#0000FF">{body}</font></h2>
    </body>
</html>
'''
