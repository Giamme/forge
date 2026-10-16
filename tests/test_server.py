import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from forge_ui import __version__, reader
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
        # The request line is unparseable, so the base class treats it as HTTP/0.9: only the body is sent.
        self.assertEqual(data, b'{"error": "bad request"}')

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
        for host in [f'evil.example:{port}', f'127.0.0.1.evil.example:{port}', '127.0.0.1', f'127.0.0.2:{port}']:
            for path, method in [('/api/runs', 'GET'), ('/app.js', 'GET'), ('/', 'GET'), ('/api/runs', 'POST')]:
                status, _, body = self.request(method, path, headers={'Host': host})
                self.assertEqual((status, json.loads(body)), (403, {'error': 'forbidden'}), (host, path))

    def test_local_host_names_are_accepted(self):
        port = self.server.server_address[1]
        for host in [f'127.0.0.1:{port}', f'localhost:{port}', f'LOCALHOST:{port}']:
            status, _, _ = self.request('GET', '/api/runs', headers={'Host': host})
            self.assertEqual(status, 200, host)


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


if __name__ == '__main__':
    unittest.main()
