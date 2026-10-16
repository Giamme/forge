"""Local HTTP server: the JSON API of docs/api.md and the static page. Read-only."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sys


STATIC_DIR = Path(__file__).resolve().parent / 'static'

_RUN_PATH = re.compile(r'/api/runs/([0-9a-f]{12})')
_STATIC_PATH = re.compile(r'/([A-Za-z0-9_.-]+\.(?:html|js|css|svg))')
_TYPES = {
    'html': 'text/html; charset=utf-8',
    'js': 'text/javascript; charset=utf-8',
    'css': 'text/css; charset=utf-8',
    'svg': 'image/svg+xml',
}
_JSON = 'application/json; charset=utf-8'
_CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'")


def _log(message):
    print(f'forge-ui: {message}', file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    # A client that connects and then stalls must not hold its thread forever.
    timeout = 30

    def do_GET(self):
        self._serve(head=False)

    def do_HEAD(self):
        self._serve(head=True)

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
        if not self._host_allowed():
            self._send(*self._json(403, {'error': 'forbidden'}))
            return
        self._send(*self._json(405, {'error': 'method not allowed'}, {'Allow': 'GET, HEAD'}))

    def _serve(self, head):
        path = self.path.partition('?')[0]
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
        catalog = self.server.catalog
        if path == '/api/runs':
            return self._json(200, {'runs': catalog.runs()})
        match = _RUN_PATH.fullmatch(path)
        if match:
            run = catalog.get(match.group(1))
            if run is None:
                return self._json(404, {'error': 'not found'})
            return self._json(200, run)
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
        return 200, _TYPES[name.rsplit('.', 1)[1]], body, {}

    @staticmethod
    def _json(status, obj, extra=None):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        return status, _JSON, body, extra or {}

    def _send(self, status, ctype, body, extra, head=False):
        try:
            self.send_response(int(status))
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', _CSP)
            for name, value in extra.items():
                self.send_header(name, value)
            self.end_headers()
            if not head:
                self.wfile.write(body)
        except OSError:
            pass  # the client hung up (BrokenPipeError, ConnectionResetError, ...): nothing to tell it


def make_server(catalog, host='127.0.0.1', port=0, static_dir=None, quiet=False):
    server = ThreadingHTTPServer((host, port), Handler)
    server.catalog = catalog
    server.static_dir = STATIC_DIR if static_dir is None else static_dir
    server.quiet = quiet
    return server
