"""Local HTTP server: the JSON API of docs/api.md and the static page.

Reads are open to the local page. Writes (POST) pass the checks of `_write_response` and then run one
forge CLI call through control.py; the server itself never writes a run dir, a repo or config.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import urllib.parse

from forge_ui import __version__, build_id, control


STATIC_DIR = Path(__file__).resolve().parent / 'static'
# The build of the code this process loaded. Files edited later must not change it: the launcher
# compares /api/health with its own build_id() to find a server still running old code.
BUILD = build_id()
# The forge command every write calls. A test points it at a fake that records its argv.
forge_cli = Path(__file__).resolve().parents[2] / 'forge'
MAX_BODY = 64 * 1024
DRAIN_WAIT = 1.0
DRAIN_MAX = 1024 * 1024  # an unread request body is read (to here) before a refusal, so the client sees the answer
TOKEN_HEADER = 'X-Forge-UI-Token'

_RUN_PATH = re.compile(r'/api/runs/([0-9a-f]{12})')
_TASK_PATH = re.compile(r'/api/runs/([0-9a-f]{12})/tasks/([^/]+)')
_EXEC_PATH = re.compile(r'/api/runs/([0-9a-f]{12})/executions/((?:task|dwarf|qa|solo|verification|parallel)-[0-9a-f]{32})')
_REVIEW_PATH = re.compile(r'/api/runs/([0-9a-f]{12})/final-reviews/((?:final-review|integrate)-[0-9]+-[0-9]+)')
_TASK_DIFF_PATH = re.compile(_TASK_PATH.pattern + r'/diff(?:/(current|prev|committed|existing))?')
_EXEC_DIFF_PATH = re.compile(_EXEC_PATH.pattern + '/diff')
_TRANSCRIPT_PATH = re.compile(_EXEC_PATH.pattern + '/transcript')
_REVIEW_DIFF_PATH = re.compile(_REVIEW_PATH.pattern + '/diff')
_FILES_PATH = re.compile(r'/api/runs/([0-9a-f]{12})/files')
_FINDINGS_PATH = re.compile(r'/api/runs/([0-9a-f]{12})/findings')
_EXPORT_PATH = re.compile(r'/api/runs/([0-9a-f]{12})/export')
_FRACTAL_PATH = re.compile(_TASK_PATH.pattern + r'/fractal(?:/nodes/([A-Za-z0-9][A-Za-z0-9_-]{0,100})(/diff)?)?')
_STATIC_PATH = re.compile(r'/([A-Za-z0-9_.-]+\.(?:html|js|css|svg|woff2))')
_TYPES = {
    'html': 'text/html; charset=utf-8',
    'js': 'text/javascript; charset=utf-8',
    'css': 'text/css; charset=utf-8',
    'svg': 'image/svg+xml',
    'woff2': 'font/woff2',
}
_JSON = 'application/json; charset=utf-8'
_CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'")
# The page may load three.js, pinned, from the CDN (only when the 3D view is opened); its inline
# import map is allowed by hash, computed from the file actually served.
_PAGE_CSP = ("default-src 'self'; style-src 'self'; script-src 'self' https://cdn.jsdelivr.net/npm/three@0.160.0/{}; "
             "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
_IMPORTMAP = re.compile(r'<script type="importmap">(.*?)</script>', re.DOTALL | re.IGNORECASE)
_HEAD_TAG = re.compile(rb'<head(?:\s[^>]*)?>', re.IGNORECASE)
_DOCTYPE = re.compile(rb'<!doctype[^>]*>', re.IGNORECASE)


def _log(message):
    print(f'forge-ui: {message}', file=sys.stderr)


def page_csp(body):
    """The CSP for an HTML page: _PAGE_CSP plus the sha256 of each inline import map in it."""
    # Browsers hash the script text after normalising newlines, so the hash is taken the same way.
    text = body.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
    hashes = ''.join(" 'sha256-%s'" % base64.b64encode(hashlib.sha256(script.encode('utf-8')).digest()).decode('ascii')
                     for script in _IMPORTMAP.findall(text))
    return _PAGE_CSP.format(hashes)


def with_token(body, token):
    """index.html with `<meta name="forge-ui-token">` as the first thing in its <head>."""
    meta = b'<meta name="forge-ui-token" content="%s">' % token.encode('ascii')
    # Without a <head> tag the meta goes after the doctype (before it the page would render in quirks mode).
    found = _HEAD_TAG.search(body) or _DOCTYPE.search(body)
    end = found.end() if found else 0
    return body[:end] + meta + body[end:]


def _task_id(segment):
    """A task id from its URL segment, or None when it cannot name a task dir."""
    try:
        value = urllib.parse.unquote(segment, errors='strict')
    except UnicodeDecodeError:
        return None
    if not value or value in ('.', '..') or '/' in value or '\x00' in value or '\ufffd' in value:
        return None
    return value


NO_ROUTE = object()


def api_get(catalog, path):
    """What a GET of the JSON API path `path` (no query) answers: the object, None for a 404, or NO_ROUTE
    when `path` is not one of these routes. /api/health and /api/runs/<key>/export are the handler's."""
    if path == '/api/runs':
        return {'runs': catalog.runs()}
    if path == '/api/models':
        return catalog.models()
    match = _RUN_PATH.fullmatch(path)
    if match:
        return catalog.get(match.group(1))
    match = _TASK_PATH.fullmatch(path)
    if match:
        task_id = _task_id(match.group(2))
        return None if task_id is None else catalog.task(match.group(1), task_id)
    match = _EXEC_PATH.fullmatch(path)
    if match:
        return catalog.execution(*match.groups())
    match = _REVIEW_PATH.fullmatch(path)
    if match:
        return catalog.final_review(*match.groups())
    match = _TASK_DIFF_PATH.fullmatch(path)
    if match:
        task_id = _task_id(match.group(2))
        return None if task_id is None else catalog.diff(match.group(1), task_id, match.group(3) or 'current')
    match = _EXEC_DIFF_PATH.fullmatch(path)
    if match:
        return catalog.execution_diff(*match.groups())
    match = _REVIEW_DIFF_PATH.fullmatch(path)
    if match:
        return catalog.final_review_diff(*match.groups())
    match = _TRANSCRIPT_PATH.fullmatch(path)
    if match:
        return catalog.transcript(*match.groups())
    match = _FILES_PATH.fullmatch(path)
    if match:
        return catalog.files(match.group(1))
    match = _FINDINGS_PATH.fullmatch(path)
    if match:
        return catalog.findings(match.group(1))
    match = _FRACTAL_PATH.fullmatch(path)
    if match:
        key, task_id, node, diff = match.group(1), _task_id(match.group(2)), match.group(3), match.group(4)
        if task_id is None:
            return None
        if node is None:
            return catalog.fractal(key, task_id)
        read = catalog.fractal_node_diff if diff else catalog.fractal_node
        return read(key, task_id, node)
    return NO_ROUTE


class Handler(BaseHTTPRequestHandler):
    # A client that connects and then stalls must not hold its thread forever.
    timeout = 30

    def do_GET(self):
        self._serve(head=False)

    def do_HEAD(self):
        self._serve(head=True)

    def do_POST(self):
        with self._active():
            self._send(*self._write_response())

    def do_OPTIONS(self):
        # No CORS: a preflight is never answered, so no other origin can learn that a write is allowed.
        self._method_not_allowed()

    def __getattr__(self, name):
        # Every other method (POST, PUT, a made-up verb, ...) gets a 405, not the 501 of the base class.
        if name.startswith('do_'):
            return self._method_not_allowed
        raise AttributeError(name)

    def handle(self):
        # The client may reset the connection while the request line is still being read. That error
        # comes from rfile.readline(), before any response exists, so the base class would print a traceback.
        try:
            super().handle()
        except ConnectionError:
            pass  # the client hung up (ConnectionResetError, BrokenPipeError, ...): nothing to tell it

    def send_error(self, code, message=None, explain=None):
        # The base class answers malformed requests with an HTML page; keep the JSON contract.
        with self._active():
            self._send(*self._json(code, {'error': self.responses[code][0].lower()}), head=self.command == 'HEAD')

    def log_request(self, code='-', size='-'):
        if not self._quiet():
            path = (getattr(self, 'path', None) or '').partition('?')[0]
            _log(f'{self.command or "-"} {path} {code}')

    def log_message(self, format, *args):
        # Also reached by the base class for stalled connections (log_error): --quiet silences those too.
        if not self._quiet():
            _log(f'{self.address_string()} {format % args}')

    def _quiet(self):
        return getattr(self.server, 'quiet', False)

    def _host_allowed(self):
        # DNS rebinding: a hostile page can make its own domain resolve to 127.0.0.1 and read us same-origin.
        # Only the local names are accepted, with any port: a port forward (ssh -L, VS Code) changes the port.
        host = (self.headers.get('Host') or '').lower()
        if not host:
            return True  # HTTP/1.0 clients may omit Host; a rebinding page always sends its own name
        name = host.rsplit(':', 1)[0] if ':' in host else host
        return name in ('127.0.0.1', 'localhost')

    def _method_not_allowed(self):
        with self._active():
            self._refuse_method()

    def _refuse_method(self):
        if not self._host_allowed():
            self._send(*self._json(403, {'error': 'forbidden'}))
            return
        allow = 'GET, HEAD'
        if not self.server.read_only and control.has_route('POST', self.path.partition('?')[0]):
            allow += ', POST'
        self._send(*self._json(405, {'error': 'method not allowed'}, {'Allow': allow}))

    @contextmanager
    def _active(self):
        # Idle exit measures from the last activity, and a request in flight is activity: stamp when it
        # arrives and when it ends, and count it so a slow one is not cut off at the idle deadline.
        server = self.server
        # The stamp and the count change together under busy_lock: the idle watcher reads both under it.
        with server.busy_lock:
            server.last_request = time.monotonic()
            server.busy += 1
        try:
            yield
        finally:
            with server.busy_lock:
                server.busy -= 1
                server.last_request = time.monotonic()

    def _serve(self, head):
        with self._active():
            self._respond(head)

    def _respond(self, head):
        path, _, self.query = self.path.partition('?')
        if not self._host_allowed():
            response = self._json(403, {'error': 'forbidden'})
        else:
            try:
                response = self._route(path)
            except Exception as exc:  # one bad request must not kill the server
                detail = ' '.join(str(exc).split())
                _log(f'internal error on {self.command} {path}: {type(exc).__name__}: {detail}')
                response = self._json(500, {'error': 'internal error'})
        self._send(*response, head=head)

    def _route(self, path):
        if path == '/api/health':
            return self._json(200, {
                'app': 'forge-ui',
                'version': __version__,
                'build': BUILD,
                'pid': os.getpid(),
                'controls': not self.server.read_only,
            })
        catalog = self.server.catalog
        match = _EXPORT_PATH.fullmatch(path)
        if match:
            return self._export(catalog, match.group(1))
        value = api_get(catalog, path)
        if value is not NO_ROUTE:
            return self._found(value)
        routed = control.dispatch(self.server, forge_cli, 'GET', path, None)
        if routed:
            return self._json(routed[0], routed[1])
        if path == '/':
            return self._static('index.html')
        match = _STATIC_PATH.fullmatch(path)
        if match:
            return self._static(match.group(1))
        return self._json(404, {'error': 'not found'})

    def _static(self, name):
        root = Path(self.server.static_dir).resolve()
        body = None
        if '..' not in name:
            try:
                target = (root / name).resolve()
                # Directly inside the static dir, after symlinks are resolved: no subdirs, no escapes.
                if target.parent == root and target.is_file():
                    body = target.read_bytes()
            except (OSError, RuntimeError, ValueError):
                body = None
        if body is None:
            return self._json(404, {'error': 'not found'})
        kind = name.rsplit('.', 1)[1]
        if name == 'index.html' and not self.server.read_only:
            body = with_token(body, self.server.token)
        return 200, _TYPES[kind], body, {'Content-Security-Policy': page_csp(body)} if kind == 'html' else {}

    # ------------------------------------------------------------------------------ writes

    def _drain(self):
        """Read (and drop) the request body that is already on its way, up to DRAIN_MAX, so a refusal is not
        lost to a connection reset. A client that claims more than it sends costs DRAIN_WAIT, not the timeout."""
        self.close_connection = True
        try:
            left = min(int(self.headers.get('Content-Length') or 0), DRAIN_MAX)
            self.connection.settimeout(DRAIN_WAIT)
            while left > 0:
                chunk = self.rfile.read1(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
        except (OSError, ValueError):
            pass

    def _refuse(self, status, error):
        self._drain()
        return self._json(status, {'error': error})

    def _body(self):
        """(object, None) for a valid JSON object body, else (None, refusal response)."""
        if self.headers.get('Transfer-Encoding') is not None:
            return None, self._refuse(400, 'bad request')
        length = (self.headers.get('Content-Length') or '').strip()
        if not length.isascii() or not length.isdigit():
            return None, self._refuse(400, 'bad request')
        if int(length) > MAX_BODY:
            return None, self._refuse(413, 'payload too large')
        try:
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise ValueError('short body')
            body = json.loads(raw.decode('utf-8'))
        except (OSError, ValueError, RecursionError):  # not UTF-8, not JSON, or the client hung up
            self.close_connection = True
            return None, self._json(400, {'error': 'bad request'})
        if not isinstance(body, dict):
            return None, self._json(400, {'error': 'bad request'})
        return body, None

    def _write_response(self):
        """The answer to a POST. A request passes every check, in this order, before any route sees it:
        the Host, read-only mode (405), the token, the content type, the Origin and the body."""
        server = self.server
        path = self.path.partition('?')[0]
        if not self._host_allowed():
            return self._refuse(403, 'forbidden')
        if server.read_only:
            self._drain()
            return self._json(405, {'error': 'method not allowed'}, {'Allow': 'GET, HEAD'})
        # compare_digest takes bytes here: a non-ASCII header value must not raise.
        sent = (self.headers.get(TOKEN_HEADER) or '').encode('utf-8', 'surrogateescape')
        if not hmac.compare_digest(sent, server.token.encode('ascii')):
            return self._refuse(403, 'forbidden')
        kind = (self.headers.get('Content-Type') or '').partition(';')[0].strip().lower()
        if kind != 'application/json':
            return self._refuse(400, 'content type must be application/json')
        origin = self.headers.get('Origin')
        if origin is not None and origin != 'http://' + (self.headers.get('Host') or ''):
            return self._refuse(403, 'forbidden')
        body, refusal = self._body()
        if refusal:
            return refusal
        try:
            routed = control.dispatch(server, forge_cli, 'POST', path, body)
        except Exception as exc:  # one bad request must not kill the server
            detail = ' '.join(str(exc).split())
            _log(f'internal error on POST {path}: {type(exc).__name__}: {detail}')
            return self._json(500, {'error': 'internal error'})
        if routed is None:
            return self._json(405, {'error': 'method not allowed'}, {'Allow': 'GET, HEAD'})
        status, obj, route, key = routed
        if not self._quiet():
            _log(f'control {route} {key} -> {status}')
        return self._json(status, obj)

    def _export(self, catalog, key):
        # One run as one HTML file, to download: never rendered from this origin (sandbox, no sources).
        from forge_ui import export
        params = urllib.parse.parse_qs(getattr(self, 'query', ''), max_num_fields=8)
        flag = lambda name: (params.get(name) or ['0'])[0] == '1'
        built = export.build(catalog, key, texts=flag('texts'), paths=flag('paths'))
        if built is None:
            return self._json(404, {'error': 'not found'})
        name, body = built
        return 200, _TYPES['html'], body, {'Content-Disposition': f'attachment; filename="{name}"',
                                           'Content-Security-Policy': "default-src 'none'; sandbox"}

    def _found(self, value):
        return self._json(404, {'error': 'not found'}) if value is None else self._json(200, value)

    @staticmethod
    def _json(status, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        return status, _JSON, body, extra or {}

    def _send(self, status, ctype, body, extra, head=False):
        extra = dict(extra)
        try:
            self.send_response(int(status))
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', extra.pop('Content-Security-Policy', _CSP))
            self.send_header('Referrer-Policy', 'no-referrer')
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            if not head:
                self.wfile.write(body)
        except OSError:
            pass  # the client hung up (BrokenPipeError, ConnectionResetError, ...): nothing to tell it


def make_server(catalog, host='127.0.0.1', port=0, static_dir=None, quiet=False, read_only=False):
    server = ThreadingHTTPServer((host, port), Handler)
    server.catalog = catalog
    server.read_only = read_only
    server.token = secrets.token_urlsafe(32)  # per process; only the page this process serves carries it
    server.run_locks = control.RunLocks()
    server.static_dir = STATIC_DIR if static_dir is None else static_dir
    server.quiet = quiet
    server.last_request = time.monotonic()  # stamped when a request arrives and when it ends
    server.busy = 0  # requests in flight; idle exit waits for them
    server.busy_lock = threading.Lock()
    return server
