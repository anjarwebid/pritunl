from constants import *
from database import Database
from config import Config
import os
import logging
import signal
import time
import json
import urllib2
import threading
import flask
import hashlib
import subprocess

logger = None

class AppServer(Config):
    bool_options = ['debug', 'log_debug', 'auto_start_servers']
    int_options = ['port', 'session_timeout', 'key_bits', 'dh_param_bits']
    path_options = ['log_path', 'db_path', 'www_path', 'data_path',
        'server_cert_path', 'server_key_path']
    str_options = ['bind_addr', 'password']

    def __init__(self):
        Config.__init__(self)
        self.app = None
        self.app_db = None
        self.mem_db = None
        self.interrupt = False

    def _get_public_ip(self):
        logger.debug('Getting public ip address...')
        try:
            request = urllib2.Request(PUBLIC_IP_SERVER)
            response = urllib2.urlopen(request, timeout=10)
            self.public_ip = json.load(response)['ip']
        except:
            logger.debug('Failed to get public ip address...')

    def _setup_public_ip(self):
        self.public_ip = None
        threading.Thread(target=self._get_public_ip).start()

    def _setup_app(self):
        self.app = flask.Flask(APP_NAME)
        self.app.secret_key = os.urandom(32)

        global logger
        logger = self.app.logger

    def auth(self, call):
        def _wrapped(*args, **kwargs):
            if 'timestamp' not in flask.session:
                raise flask.abort(401)

            # Disable session timeout if set to 0
            if self.session_timeout is not None:
                session_timeout = self.session_timeout or None
            else:
                session_timeout = DEFAULT_SESSION_TIMEOUT

            if session_timeout and time.time() - flask.session[
                    'timestamp'] > session_timeout:
                flask.session.pop('timestamp', None)
                raise flask.abort(401)
            return call(*args, **kwargs)
        _wrapped.__name__ = '%s_auth' % call.__name__
        return _wrapped

    def _setup_conf(self):
        self.set_path(self.conf_path)

    def _setup_log(self):
        if self.log_debug:
            self.log_level = logging.DEBUG
        else:
            self.log_level = logging.INFO

        if self.log_path:
            self.log_handler = logging.FileHandler(self.log_path)
        else:
            self.log_handler = logging.StreamHandler()

        global logger
        if not logger:
            logger = logging.getLogger(APP_NAME)

        logger.setLevel(self.log_level)
        self.log_handler.setLevel(self.log_level)

        self.log_handler.setFormatter(logging.Formatter(
            '[%(asctime)s][%(levelname)s][%(module)s][%(lineno)d] ' +
            '%(message)s'))

        logger.addHandler(self.log_handler)

    def _setup_db(self):
        self.mem_db = Database(None)
        self.app_db = Database(self.db_path or DEFAULT_DB_PATH)

    def _close_db(self):
        self.mem_db.close()
        self.app_db.close()

    def _setup_handlers(self):
        import handlers

    def _setup_static_handler(self):
        www_path = self.www_path or DEFAULT_WWW_PATH

        from werkzeug import SharedDataMiddleware

        self.app.wsgi_app = SharedDataMiddleware(self.app.wsgi_app, {
            '/': os.path.normpath(www_path),
        }, cache=False)

        @self.app.route('/', methods=['GET'])
        def index_get():
            with open(os.path.join(www_path, 'index.html'), 'r') as fd:
                return fd.read()

    def _hash_password(self, password):
        password_hash = hashlib.sha512()
        password_hash.update(password)
        password_hash.update(PASSWORD_SALT)
        return password_hash.hexdigest()

    def check_password(self, password_attempt):
        if not self.password:
            if password_attempt == DEFAULT_PASSWORD:
                return True
            return False

        password_attempt = self._hash_password(password_attempt)
        if password_attempt == self.password:
            return True
        return False

    def set_password(self, password):
        self.password = self._hash_password(password)
        self.commit()

    def _setup_all(self):
        self._setup_app()
        self._setup_conf()
        self._setup_log()
        self._setup_public_ip()
        self._setup_db()
        self._setup_handlers()
        self._setup_static_handler()

    def _setup_server_cert(self):
        if self.server_cert_path and self.server_key_path:
            self._server_cert_path = self.server_cert_path
            self._server_key_path = self.server_key_path
        else:
            self._server_cert_path = os.path.join(self.data_path,
                SERVER_CERT_NAME)
            self._server_key_path = os.path.join(self.data_path,
                SERVER_KEY_NAME)

            if not os.path.isfile(self._server_cert_path) or \
                    not os.path.isfile(self._server_key_path):
                logger.info('Generating server ssl cert...')
                try:
                    subprocess.check_call([
                        'openssl', 'req', '-batch', '-x509', '-nodes',
                        '-newkey', 'rsa:4096',
                        '-days', '3652',
                        '-keyout', self._server_key_path,
                        '-out', self._server_cert_path,
                    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                except subprocess.CalledProcessError:
                    logger.exception('Failed to generate server ssl cert.')
                    raise
                os.chmod(self._server_key_path, 0600)

    def _run_wsgi(self):
        self._setup_server_cert()
        import cherrypy.wsgiserver
        import cherrypy.wsgiserver.ssl_builtin
        from log_entry import LogEntry
        logger.info('Starting server...')

        if self.auto_start_servers != False:
            from pritunl.server import Server
            for server in Server.get_servers():
                if server.get_orgs():
                    server.start()

        server = cherrypy.wsgiserver.CherryPyWSGIServer(
            (self.bind_addr, self.port), self.app)
        server.ssl_adapter = cherrypy.wsgiserver.ssl_builtin.BuiltinSSLAdapter(
            self._server_cert_path, self._server_key_path)
        try:
            server.start()
        except (KeyboardInterrupt, SystemExit), exc:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            LogEntry(message='Web server stopped.')
            self.interrupt = True
            logger.info('Stopping server...')
            server.stop()
            self._close_db()

    def _run_wsgi_debug(self):
        from log_entry import LogEntry
        logger.info('Starting debug server...')

        # App.run server uses werkzeug logger
        werkzeug_logger = logging.getLogger('werkzeug')
        werkzeug_logger.setLevel(self.log_level)
        werkzeug_logger.addHandler(self.log_handler)

        try:
            self.app.run(host=self.bind_addr, port=self.port, threaded=True)
        finally:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            LogEntry(message='Web server stopped.')
            self.interrupt = True
            # Possible data loss here db closing before debug server
            self._close_db()
            logger.info('Stopping debug server...')

    def _run_server(self):
        from log_entry import LogEntry
        LogEntry(message='Web server started.')
        if self.debug:
            self._run_wsgi_debug()
        else:
            self._run_wsgi()

    def run_server(self):
        self._setup_all()
        self._run_server()

    def run_all(self):
        self._setup_all()
        self._run_server()
