import hashlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from forge_ui import __version__, build_id, reader
from forge_ui.__main__ import main
from forge_ui.server import Handler, make_server


FIXTURES = Path(__file__).parent / 'fixtures'
PLAN = FIXTURES / 'plan-full'
CSP = ("default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'")
INDEX = b'<!doctype html><title>index</title>'
APP = b'window.app = 1;'
STYLE = b'body { color: #222; }'
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'
# The idle thread checks about once a second. A 1s idle exit has to finish inside this
# window on a busy machine; staying up is observed across several of those checks.
IDLE_DONE = 10
IDLE_STAY = 4


def _source_build_id():
    """Spec of build_id: sha256 of each package *.py name and its bytes, names sorted."""
    digest = hashlib.sha256()
    root = Path(__file__).resolve().parents[1] / 'forge_ui'
    for path in sorted(root.glob('*.py'), key=lambda item: item.name):
        digest.update(path.name.encode('utf-8'))
        digest.update(path.read_bytes())
    return digest.hexdigest()


class _LockedBuffer:
    """Text buffer one thread can write while the test waits for a line."""

    def __init__(self):
        self._parts = []
        self._cond = threading.Condition()

    def write(self, text):
        with self._cond:
            self._parts.append(text)
            self._cond.notify_all()
        return len(text)

    def flush(self):
        return None

    def getvalue(self):
        with self._cond:
            return ''.join(self._parts)

    def wait_for(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                value = ''.join(self._parts)
                if predicate(value):
                    return value
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return value
                self._cond.wait(min(0.05, remaining))


class _ThreadCapture:
    """Send one thread's writes to a buffer and leave every other thread's stream alone."""

    def __init__(self, real, buffer):
        self._real = real
        self._buffer = buffer
        self.ident = None

    def write(self, text):
        if threading.get_ident() == self.ident:
            return self._buffer.write(text)
        return self._real.write(text)

    def flush(self):
        if threading.get_ident() == self.ident:
            self._buffer.flush()
        else:
            self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


class FakeCatalog:
    """Records the keys it is asked for; can fail its next runs() call once."""

    def __init__(self, runs=(), fail_next_runs=False):
        self._runs = list(runs)
        self.fail_next_runs = fail_next_runs
        self.gets = []

    def runs(self):
        if self.fail_next_runs:
            self.fail_next_runs = False
            raise RuntimeError('boom')
        return self._runs

    def get(self, key):
        self.gets.append(key)
        return None


class TempCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)


class ServerTestCase(TempCase):
    def setUp(self):
        super().setUp()
        self.static = self.base / 'static'
        self.static.mkdir()
        (self.static / 'index.html').write_bytes(INDEX)
        (self.static / 'app.js').write_bytes(APP)
        (self.static / 'style.css').write_bytes(STYLE)
        (self.static / 'icon.svg').write_bytes(SVG)
        (self.static / 'sub').mkdir()
        (self.static / 'sub' / 'x.js').write_text('x')
        secret = self.base / 'secret.js'
        secret.write_text('secret')
        os.symlink(secret, self.static / 'outside.js')
        self.catalog = reader.Catalog(roots=[FIXTURES], registry=False)
        self.server = self.start(self.catalog)

    def start(self, catalog, quiet=True, static_dir=None):
        server = make_server(catalog, port=0, static_dir=static_dir or self.static, quiet=quiet)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self.stop, server, thread)
        return server

    @staticmethod
    def stop(server, thread):
        server.shutdown()
        server.server_close()
        thread.join()

    def request(self, method, path, server=None, headers=None):
        port = (server or self.server).server_address[1]
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            headers = {name.lower(): value for name, value in response.getheaders()}
            return response.status, headers, response.read()
        finally:
            conn.close()


class RoutesTests(ServerTestCase):
    def test_lists_the_fixture_runs(self):
        status, _, body = self.request('GET', '/api/runs')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {'runs': self.catalog.runs()})
        self.assertEqual([run['key'] for run in json.loads(body)['runs']], [reader.run_key(PLAN)])

    def test_health_reports_version_build_and_pid(self):
        status, headers, body = self.request('GET', '/api/health')
        self.assertEqual(status, 200)
        self.assertEqual(headers['content-type'], 'application/json; charset=utf-8')
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertEqual(headers['x-content-type-options'], 'nosniff')
        self.assertEqual(headers['content-security-policy'], CSP)
        self.assertEqual(__version__, '0.2.0')
        self.assertEqual(build_id(), _source_build_id())
        self.assertEqual(json.loads(body), {
            'app': 'forge-ui',
            'version': '0.2.0',
            'build': build_id(),
            'pid': os.getpid(),
        })
        # The build is that of the code this process loaded, not of the files on disk now.
        with patch('forge_ui.server.build_id', return_value='f' * 64):
            _, _, body = self.request('GET', '/api/health')
        self.assertEqual(json.loads(body)['build'], _source_build_id())
        status, headers, head = self.request('HEAD', '/api/health')
        self.assertEqual((status, head), (200, b''))
        self.assertEqual(headers['content-type'], 'application/json; charset=utf-8')
        self.assertEqual(headers['cache-control'], 'no-store')
        self.assertEqual(headers['content-length'], str(len(body)))
        for method, empty in (('GET', False), ('HEAD', True)):
            status, _, denied = self.request(method, '/api/health', headers={'Host': 'evil.example'})
            self.assertEqual(status, 403, method)
            self.assertEqual(denied, b'' if empty else b'{"error": "forbidden"}')

    def test_detail_matches_the_reader(self):
        status, _, body = self.request('GET', f'/api/runs/{reader.run_key(PLAN)}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), reader.detail(PLAN, source='root'))

    def test_bad_keys_are_404_without_touching_the_catalog(self):
        catalog = FakeCatalog()
        server = self.start(catalog)
        for key in ['../x', '%2e%2e', 'ABCDEF123456', 'abcdef12345', 'abcdef1234567', 'g' * 12]:
            status, _, body = self.request('GET', f'/api/runs/{key}', server)
            self.assertEqual((status, json.loads(body)), (404, {'error': 'not found'}), key)
        self.assertEqual(catalog.gets, [])

    def test_unknown_valid_key_is_404(self):
        catalog = FakeCatalog()
        server = self.start(catalog)
        status, _, body = self.request('GET', '/api/runs/' + '0' * 12, server)
        self.assertEqual((status, json.loads(body)), (404, {'error': 'not found'}))
        self.assertEqual(catalog.gets, ['0' * 12])

    def test_index_and_static_files_with_content_types(self):
        cases = [
            ('/', 'text/html; charset=utf-8', INDEX),
            ('/index.html', 'text/html; charset=utf-8', INDEX),
            ('/app.js', 'text/javascript; charset=utf-8', APP),
            ('/app.js?v=2', 'text/javascript; charset=utf-8', APP),
            ('/style.css', 'text/css; charset=utf-8', STYLE),
            ('/icon.svg', 'image/svg+xml', SVG),
        ]
        for path, ctype, data in cases:
            status, headers, body = self.request('GET', path)
            self.assertEqual((status, headers['content-type'], body), (200, ctype, data), path)

    def test_missing_static_file_is_a_json_404(self):
        status, headers, body = self.request('GET', '/graph.js')
        self.assertEqual(status, 404)
        self.assertEqual(headers['content-type'], 'application/json; charset=utf-8')
        self.assertEqual(json.loads(body), {'error': 'not found'})

    def test_page_not_built_yet_is_a_json_404(self):
        empty = self.base / 'empty'
        empty.mkdir()
        server = self.start(self.catalog, static_dir=empty)
        status, _, body = self.request('GET', '/', server)
        self.assertEqual((status, json.loads(body)), (404, {'error': 'not found'}))

    def test_traversal_nested_and_symlink_paths_are_404(self):
        for path in ['/../server.py', '/%2e%2e/server.py', '/%2e%2e%2fsecret.js', '/..js',
                     '/sub/x.js', '/outside.js', '/api/runs/', '/nope']:
            status, _, body = self.request('GET', path)
            self.assertEqual((status, json.loads(body)), (404, {'error': 'not found'}), path)
            self.assertNotIn(b'secret', body)

    def test_head_has_headers_but_no_body(self):
        status, headers, body = self.request('HEAD', '/app.js')
        self.assertEqual((status, body, headers['content-length']), (200, b'', str(len(APP))))
        status, headers, body = self.request('HEAD', '/api/runs')
        self.assertEqual((status, body), (200, b''))
        self.assertEqual(headers['content-length'], str(len(self.request('GET', '/api/runs')[2])))

    def test_other_methods_are_405_with_allow(self):
        for method in ['POST', 'PUT', 'DELETE', 'BOGUS']:
            status, headers, body = self.request(method, '/api/runs')
            self.assertEqual(status, 405, method)
            self.assertEqual(headers['allow'], 'GET, HEAD')
            self.assertEqual(json.loads(body), {'error': 'method not allowed'})

    def test_malformed_request_gets_a_json_400(self):
        with socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=10) as sock:
            sock.sendall(b'NOT A REQUEST\r\n\r\n')
            data = b''
            while chunk := sock.recv(4096):
                data += chunk
        # Python 3.9-3.13 answer an unparseable request line as HTTP/0.9 (body only); 3.14+ send a full
        # status line first. Either way the body must be the JSON error and any status line a 400.
        self.assertTrue(data.endswith(b'{"error": "bad request"}'), data)
        if data.startswith(b'HTTP/'):
            self.assertEqual(data.split(b' ', 2)[1], b'400', data)

    def test_headers_on_json_and_static_responses(self):
        for path, body_of in [('/api/runs', None), ('/app.js', APP)]:
            status, headers, body = self.request('GET', path)
            self.assertEqual(headers['cache-control'], 'no-store', path)
            self.assertEqual(headers['content-security-policy'], CSP, path)
            self.assertEqual(headers['x-content-type-options'], 'nosniff', path)
            self.assertEqual(headers['content-length'], str(len(body)), path)
            if body_of is not None:
                self.assertEqual(body, body_of)

    def test_json_is_utf8_not_escaped(self):
        server = self.start(FakeCatalog(runs=[{'goal': 'café'}]))
        status, headers, body = self.request('GET', '/api/runs', server)
        self.assertEqual(headers['content-type'], 'application/json; charset=utf-8')
        self.assertIn('café'.encode('utf-8'), body)

    def test_reader_error_is_500_and_the_server_keeps_serving(self):
        catalog = FakeCatalog(fail_next_runs=True)
        server = self.start(catalog)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status, _, body = self.request('GET', '/api/runs', server)
        self.assertEqual((status, json.loads(body)), (500, {'error': 'internal error'}))
        self.assertNotIn(b'boom', body)
        lines = stderr.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('boom', lines[0])
        status, _, body = self.request('GET', '/api/runs', server)
        self.assertEqual((status, json.loads(body)), (200, {'runs': []}))

    def test_logs_one_line_per_request_without_query_values(self):
        server = self.start(self.catalog, quiet=False)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.request('GET', '/api/runs?token=s3cret', server)
        self.assertEqual(stderr.getvalue(), 'forge-ui: GET /api/runs 200\n')

    def test_quiet_server_logs_nothing(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.request('GET', '/api/runs')
        self.assertEqual(stderr.getvalue(), '')

    def test_quiet_server_stays_silent_when_an_idle_connection_times_out(self):
        port = self.server.server_address[1]
        stderr = io.StringIO()
        with patch.object(Handler, 'timeout', 0.2), redirect_stderr(stderr):
            with socket.create_connection(('127.0.0.1', port), timeout=10) as sock:
                self.assertEqual(sock.recv(1), b'')  # the server gives up on the silent client and closes
        self.assertEqual(stderr.getvalue(), '')

    def test_host_header_must_be_the_local_address(self):
        port = self.server.server_address[1]
        for host in [f'evil.example:{port}', 'evil.example', f'127.0.0.1.evil.example:{port}', '127.0.0.2',
                     f'127.0.0.2:{port}', f'localhost.evil.example:{port}']:
            for path, method in [('/api/runs', 'GET'), ('/api/health', 'GET'), ('/app.js', 'GET'),
                                 ('/', 'GET'), ('/api/runs', 'POST')]:
                status, _, body = self.request(method, path, headers={'Host': host})
                self.assertEqual((status, json.loads(body)), (403, {'error': 'forbidden'}), (host, path))

    def test_http10_request_without_host_is_served(self):
        with socket.create_connection(('127.0.0.1', self.server.server_address[1]), timeout=10) as sock:
            sock.sendall(b'GET /api/runs HTTP/1.0\r\n\r\n')
            data = b''
            while chunk := sock.recv(4096):
                data += chunk
        self.assertTrue(data.startswith(b'HTTP/1.0 200'), data[:40])
        self.assertIn(b'"runs"', data)

    def test_local_host_names_are_accepted_with_any_port_or_none(self):
        # A port forward (ssh -L 9000:127.0.0.1:<port>) sends the forwarded port in Host.
        port = self.server.server_address[1]
        for host in [f'127.0.0.1:{port}', f'localhost:{port}', f'LOCALHOST:{port}', 'localhost:9000',
                     '127.0.0.1:65535', '127.0.0.1', 'localhost']:
            status, _, _ = self.request('GET', '/api/runs', headers={'Host': host})
            self.assertEqual(status, 200, host)

    def test_client_reset_while_sending_the_request_is_silent(self):
        port = self.server.server_address[1]
        baseline = threading.active_count()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            sock = socket.create_connection(('127.0.0.1', port), timeout=10)
            sock.sendall(b'GET /api/ru')  # the request line is never finished
            # Wait for the server to start a handler thread for this connection, then for it to finish.
            deadline = time.monotonic() + 5
            while threading.active_count() <= baseline and time.monotonic() < deadline:
                time.sleep(0.01)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            sock.close()  # SO_LINGER 0: the close sends a reset, not a FIN
            deadline = time.monotonic() + 5
            while threading.active_count() > baseline and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(stderr.getvalue(), '')
        status, _, _ = self.request('GET', '/api/runs')
        self.assertEqual(status, 200)

    def test_head_error_has_headers_but_no_body(self):
        # More than 100 header lines is a 431 from the base class; HEAD must not get a body for it.
        port = self.server.server_address[1]
        headers = ''.join(f'X-{i}: a\r\n' for i in range(150))
        request = f'HEAD /api/runs HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{headers}\r\n'.encode('ascii')
        with socket.create_connection(('127.0.0.1', port), timeout=10) as sock:
            sock.sendall(request)
            data = b''
            while chunk := sock.recv(4096):
                data += chunk
        head, _, body = data.partition(b'\r\n\r\n')
        self.assertRegex(head, rb'^HTTP/1\.[01] 431 ')
        self.assertEqual(body, b'')


class MainTests(TempCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def serve_once_then_ctrl_c(self):
        return patch.object(ThreadingHTTPServer, 'serve_forever', side_effect=KeyboardInterrupt)

    def test_version(self):
        code, out, _ = self.run_main(['--version'])
        self.assertEqual((code, out), (0, f'forge-ui {__version__}\n'))

    def test_port_in_use_is_exit_2(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(('127.0.0.1', 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        code, out, err = self.run_main(['--no-registry', '--plan', str(PLAN), '--port', str(port)])
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(err.count('\n'), 1)
        self.assertIn(str(port), err)
        self.assertIn('--port', err)

    def test_port_held_on_all_interfaces_is_exit_2(self):
        # On macOS a 127.0.0.1 bind succeeds next to a 0.0.0.0 listener; the startup probe must still refuse.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(blocker.close)
        blocker.bind(('0.0.0.0', 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        code, out, err = self.run_main(['--no-registry', '--plan', str(PLAN), '--port', str(port)])
        self.assertEqual((code, out), (2, ''))
        self.assertIn(str(port), err)
        self.assertIn('--port', err)

    def test_starts_prints_the_url_and_exits_0_on_ctrl_c(self):
        with self.serve_once_then_ctrl_c():
            code, out, err = self.run_main(['--no-registry', '--plan', str(PLAN), '--port', '0'])
        self.assertEqual(code, 0)
        self.assertRegex(out, r'^forge-ui on http://127\.0\.0\.1:\d+/ \(1 runs\)\n$')
        self.assertEqual(err, '')

    def test_missing_paths_warn_and_keep_going(self):
        missing = self.base / 'nope'
        with self.serve_once_then_ctrl_c():
            code, out, err = self.run_main(['--no-registry', '--plan', str(missing),
                                            '--root', str(missing), '--port', '0'])
        self.assertEqual(code, 0)
        self.assertEqual(err.count(str(missing)), 2)
        self.assertIn('(0 runs)', out)

    def test_open_flag_opens_the_browser_once_started(self):
        with self.serve_once_then_ctrl_c(), patch('webbrowser.open') as opener:
            self.run_main(['--no-registry', '--plan', str(PLAN), '--port', '0', '--open'])
        opener.assert_called_once()
        self.assertTrue(opener.call_args[0][0].startswith('http://127.0.0.1:'))

    def test_browser_is_not_opened_by_default(self):
        with self.serve_once_then_ctrl_c(), patch('webbrowser.open') as opener:
            self.run_main(['--no-registry', '--plan', str(PLAN), '--port', '0'])
        opener.assert_not_called()

    def test_there_is_no_host_option(self):
        code, _, err = self.run_main(['--host', '0.0.0.0'])
        self.assertEqual(code, 2)
        self.assertIn('--host', err)

    def _start_main(self, argv):
        out, err = _LockedBuffer(), _LockedBuffer()
        stdout, stderr = _ThreadCapture(sys.stdout, out), _ThreadCapture(sys.stderr, err)
        box = {}
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr

        def restore():
            sys.stdout, sys.stderr = old_out, old_err

        def run():
            stdout.ident = stderr.ident = threading.get_ident()
            try:
                box['code'] = main(argv)
            except Exception as exc:
                box['error'] = exc

        thread = threading.Thread(target=run, daemon=True)
        self.addCleanup(restore)
        thread.start()
        self.addCleanup(lambda: thread.join(IDLE_DONE + 5))
        return thread, out, err, box

    def _port_from_startup(self, out, thread, box, timeout):
        def ready(text):
            return 'forge-ui on http://127.0.0.1:' in text or not thread.is_alive()

        text = out.wait_for(ready, timeout)
        match = re.search(r'forge-ui on http://127\.0\.0\.1:(\d+)/', text)
        if match is None:
            self.fail(f'no startup line (alive={thread.is_alive()}, code={box.get("code")}, '
                      f'error={box.get("error")!r}): {text!r}')
        return int(match.group(1))

    def _get(self, port, path):
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        try:
            conn.request('GET', path)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_idle_exit_returns_0_when_unused(self):
        thread, out, err, box = self._start_main(
            ['--no-registry', '--port', '0', '--idle-exit', '1', '--quiet'])
        thread.join(IDLE_DONE)
        self.assertFalse(thread.is_alive(), f'server did not exit on its own: {out.getvalue()!r}')
        self.assertIsNone(box.get('error'))
        self.assertEqual(box['code'], 0)
        self.assertRegex(out.getvalue(), r'^forge-ui on http://127\.0\.0\.1:\d+/ \(0 runs\)\n$')
        self.assertEqual(err.getvalue(), '')

    def test_a_request_postpones_idle_exit(self):
        thread, out, err, box = self._start_main(
            ['--no-registry', '--port', '0', '--idle-exit', '1', '--quiet'])
        port = self._port_from_startup(out, thread, box, timeout=IDLE_DONE)
        # Keep asking well past the 1s limit. If a request did not stamp the server,
        # it would shut down while this loop is still running.
        deadline = time.monotonic() + 3
        answered = False
        while time.monotonic() < deadline:
            if not thread.is_alive():
                self.fail(f'server exited while a request should have postponed it '
                          f'(code={box.get("code")}, out={out.getvalue()!r}, err={err.getvalue()!r})')
            try:
                status, _ = self._get(port, '/api/runs')
            except OSError:
                if not thread.is_alive():
                    self.fail('server closed before the postponing request was answered')
                time.sleep(0.02)
                continue
            self.assertEqual(status, 200)
            answered = True
            time.sleep(0.05)
        self.assertTrue(answered)
        self.assertTrue(thread.is_alive())
        thread.join(IDLE_DONE)
        self.assertFalse(thread.is_alive(), 'server did not exit after requests stopped')
        self.assertIsNone(box.get('error'))
        self.assertEqual(box['code'], 0)
        self.assertRegex(out.getvalue(), rf'^forge-ui on http://127\.0\.0\.1:{port}/ \(0 runs\)\n$')
        self.assertEqual(err.getvalue(), '')

    def test_a_slow_request_is_not_cut_off_by_idle_exit(self):
        # A request arrives before the 1s limit and its catalog read outlasts the limit.
        entered, release = threading.Event(), threading.Event()
        started = []

        def slow_runs(catalog):
            if started:  # the startup count is the first call and must not block
                entered.set()
                release.wait(20)
            started.append(1)
            return []

        result = {}

        def ask(port):
            try:
                result['reply'] = self._get(port, '/api/runs')
            except OSError as exc:
                result['error'] = exc

        with patch.object(reader.Catalog, 'runs', slow_runs):
            thread, out, err, box = self._start_main(
                ['--no-registry', '--port', '0', '--idle-exit', '1', '--quiet'])
            port = self._port_from_startup(out, thread, box, timeout=IDLE_DONE)
            started.append(1)
            asker = threading.Thread(target=ask, args=(port,), daemon=True)
            asker.start()
            self.addCleanup(release.set)
            self.assertTrue(entered.wait(IDLE_DONE), 'the request never reached the catalog')
            # Several watcher checks pass, all after the idle limit, with the request still in flight.
            deadline = time.monotonic() + IDLE_STAY
            while time.monotonic() < deadline:
                self.assertTrue(thread.is_alive(), f'server exited under a request in flight: {box}')
                time.sleep(0.05)
            release.set()
            asker.join(IDLE_DONE)
        self.assertEqual(result, {'reply': (200, b'{"runs": []}')})
        thread.join(IDLE_DONE)
        self.assertFalse(thread.is_alive(), 'server did not exit after the slow request ended')
        self.assertEqual(box['code'], 0)

    def test_idle_exit_zero_is_accepted_and_does_not_stop_the_server(self):
        # Positive control for the negative-value case: 0 is a real value, not a usage error.
        with self.serve_once_then_ctrl_c():
            code, out, err = self.run_main(['--no-registry', '--port', '0', '--idle-exit', '0'])
        self.assertEqual(code, 0)
        self.assertRegex(out, r'^forge-ui on http://127\.0\.0\.1:\d+/ \(0 runs\)\n$')
        self.assertEqual(err, '')

        captured = []

        def capture(*args, **kwargs):
            server = make_server(*args, **kwargs)
            captured.append(server)
            return server

        with patch('forge_ui.__main__.make_server', side_effect=capture):
            thread, out, err, box = self._start_main(
                ['--no-registry', '--port', '0', '--idle-exit', '0'])
            self.addCleanup(self._stop_captured, captured, thread)
            self._port_from_startup(out, thread, box, timeout=IDLE_DONE)
            deadline = time.monotonic() + IDLE_STAY
            while time.monotonic() < deadline:
                if not thread.is_alive():
                    self.fail(f'--idle-exit 0 exited on its own (code={box.get("code")}, '
                              f'out={out.getvalue()!r}, err={err.getvalue()!r})')
                time.sleep(0.05)
            self.assertTrue(thread.is_alive())
            self.assertEqual(err.getvalue(), '')
            captured[0].shutdown()
            thread.join(IDLE_DONE)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(box.get('error'))
        self.assertEqual(box['code'], 0)
        self.assertRegex(out.getvalue(), r'^forge-ui on http://127\.0\.0\.1:\d+/ \(0 runs\)\n$')

    @staticmethod
    def _stop_captured(captured, thread):
        if captured and thread.is_alive():
            stopper = threading.Thread(target=captured[0].shutdown, daemon=True)
            stopper.start()
            stopper.join(5)
        thread.join(5)

    def test_negative_idle_exit_is_exit_2(self):
        thread, out, err, box = self._start_main(['--idle-exit', '-1'])
        thread.join(IDLE_DONE)
        self.assertFalse(thread.is_alive(), 'negative --idle-exit kept the server running')
        self.assertIsNone(box.get('error'))
        self.assertEqual(box['code'], 2)
        self.assertEqual(out.getvalue(), '')
        self.assertIn('--idle-exit', err.getvalue())
        self.assertNotIn('unrecognized arguments', err.getvalue())
        self.assertNotIn('Traceback', err.getvalue())


if __name__ == '__main__':
    unittest.main()
