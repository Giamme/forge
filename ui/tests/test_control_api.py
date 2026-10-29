"""The write path: authentication, the forge CLI call, error mapping, timeouts, read-only mode."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from forge_ui import control, reader, server as ui_server
from forge_ui.__main__ import _parser, read_only_requested
from forge_ui.server import make_server, page_csp, with_token


FIXTURES = Path(__file__).parent / 'fixtures'
ROOT = Path(__file__).resolve().parents[2]
JSON = 'application/json'

FAKE = '''#!{python}
import json, os, signal, subprocess, sys, time
home = {home!r}
behavior = json.load(open(os.path.join(home, 'behavior.json')))
stdin = sys.stdin.read()
with open(os.path.join(home, 'calls.jsonl'), 'a') as out:
    out.write(json.dumps({{'argv': sys.argv[1:], 'leader': os.getpgrp() == os.getpid(), 'stdin': stdin}}) + '\\n')
if behavior.get('ignore_term'):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if behavior.get('pid_file'):
    open(behavior['pid_file'], 'w').write(str(os.getpid()))
if behavior.get('late_file'):
    subprocess.Popen([sys.executable, '-c', 'import sys, time; time.sleep(3); open(sys.argv[1], "w").write("late")',
                      behavior['late_file']])
time.sleep(behavior.get('sleep', 0))
sys.stdout.write(behavior.get('stdout', ''))
sys.stderr.write(behavior.get('stderr', ''))
if behavior.get('signal'):
    os.kill(os.getpid(), behavior['signal'])
sys.exit(behavior.get('code', 0))
'''


class ControlApiCase(unittest.TestCase):
    """A server on a temp copy of the plan fixture, calling a fake forge that records its argv."""

    read_only = False

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.plan = self.base / 'plan'
        shutil.copytree(FIXTURES / 'plan-full', self.plan)
        self.solo = self.base / 'solo'
        shutil.copytree(FIXTURES / 'solo-full', self.solo)
        self.key = reader.run_key(self.plan)
        self.solo_key = reader.run_key(self.solo)
        self.home = self.base / 'fake'
        self.home.mkdir()
        self.fake = self.home / 'forge'
        self.fake.write_text(FAKE.format(python=sys.executable, home=str(self.home)))
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.behave()
        patcher = patch.object(ui_server, 'forge_cli', self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = self.start()
        self.seen = []  # every response, to prove the token is in none of the wrong ones

    def start(self, **options):
        catalog = reader.Catalog(plans=[self.plan, self.solo], registry=False)
        options.setdefault('read_only', self.read_only)
        server = make_server(catalog, port=0, quiet=options.pop('quiet', True), **options)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), thread.join()))
        return server

    def behave(self, **behavior):
        (self.home / 'behavior.json').write_text(json.dumps(behavior))

    def calls(self):
        path = self.home / 'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for_call(self, seconds=10):
        deadline = time.monotonic() + seconds
        while not self.calls() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(self.calls(), 'the CLI was never called')

    # -- requests
    @property
    def port(self):
        return self.server.server_address[1]

    def headers(self, server=None, **changes):
        headers = {'X-Forge-UI-Token': (server or self.server).token, 'Content-Type': JSON}
        headers.update(changes)
        return {name: value for name, value in headers.items() if value is not None}

    def send(self, method, path, body=None, headers=None, server=None):
        server = server or self.server
        conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=20)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            data = response.read()
            result = response.status, {name.lower(): value for name, value in response.getheaders()}, data
        finally:
            conn.close()
        self.seen.append(result)
        return result

    def post(self, obj, path=None, server=None, **changes):
        body = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        status, headers, data = self.send('POST', path or f'/api/runs/{self.key}/controls', body,
                                          self.headers(server, **changes), server)
        self.assertFalse([name for name in headers if name.startswith('access-control-')], headers)
        return status, json.loads(data)

    def raw(self, data):
        """Send bytes as they are; (status, headers, body)."""
        with socket.create_connection(('127.0.0.1', self.port), timeout=20) as sock:
            sock.sendall(data)
            reply = b''
            while chunk := sock.recv(65536):
                reply += chunk
        head, _, body = reply.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        self.seen.append((int(lines[0].split()[1]), {}, body))
        return int(lines[0].split()[1]), {n.lower(): v.strip() for n, _, v in (x.partition(':') for x in lines[1:])}, body

    def tearDown(self):
        token = self.server.token.encode()
        for status, headers, data in self.seen:
            self.assertNotIn(token, data)
            self.assertNotIn(self.server.token, ' '.join(map(str, headers.values())))


class TokenPlacementTests(ControlApiCase):
    def test_the_page_carries_the_token_and_nothing_else_does(self):
        meta = f'<meta name="forge-ui-token" content="{self.server.token}">'.encode()
        for path in ('/', '/index.html'):
            status, headers, body = self.send('GET', path)
            self.assertEqual(status, 200)
            self.assertEqual(body.count(meta), 1, path)
            self.assertGreater(body.index(meta), body.lower().index(b'<head'), path)
            self.assertLess(body.index(meta), body.lower().index(b'</head>'), path)
        # the CSP is that of the file on disk: the token is not a script
        on_disk = (ui_server.STATIC_DIR / 'index.html').read_bytes()
        self.assertEqual(headers['content-security-policy'], page_csp(on_disk))
        self.assertEqual(self.server.token, self.server.token.encode('ascii').decode())
        self.assertGreaterEqual(len(self.server.token), 43)  # token_urlsafe(32)
        self.seen.clear()  # the page is the one place the token belongs
        self.behave(stdout='{"run": {}}')
        token = self.server.token.encode()
        for path in ['/api/health', '/api/runs', f'/api/runs/{self.key}', f'/api/runs/{self.key}/export',
                     f'/api/runs/{self.key}/export?texts=1&paths=1', f'/api/runs/{self.key}/controls',
                     '/app.js', '/style.css', '/api/models']:
            status, headers, body = self.send('GET', path)
            self.assertIn(status, (200, 404), path)
            self.assertNotIn(token, body, path)
            self.assertNotIn(self.server.token, repr(headers), path)
        self.assertNotIn(b'forge-ui-token', self.send('GET', f'/api/runs/{self.key}/export')[2])

    def test_every_server_process_has_its_own_token(self):
        other = self.start()
        self.assertNotEqual(other.token, self.server.token)
        # and a token of one is not accepted by the other
        status, body = self.post({'action': 'unpin', 'task': 'page'}, server=other,
                                 **{'X-Forge-UI-Token': self.server.token})
        self.assertEqual(status, 403)
        self.assertEqual(self.calls(), [])

    def test_with_token_goes_into_the_head_or_after_the_doctype(self):
        meta = b'<meta name="forge-ui-token" content="T">'
        self.assertEqual(with_token(b'<!doctype html><HTML><HEAD lang=x><title>t</title></HEAD>', 'T'),
                         b'<!doctype html><HTML><HEAD lang=x>' + meta + b'<title>t</title></HEAD>')
        self.assertEqual(with_token(b'<!doctype html><title>t</title>', 'T'), b'<!doctype html>' + meta + b'<title>t</title>')
        self.assertEqual(with_token(b'<p>x', 'T'), meta + b'<p>x')


class RefusalTests(ControlApiCase):
    BODY = {'action': 'set', 'key': 'qa-threshold', 'value': 'P2'}

    def refused(self, label, status, body=None, **changes):
        got, answer = self.post(self.BODY if body is None else body, **changes)
        self.assertEqual(got, status, (label, answer))
        self.assertEqual(self.calls(), [], label)  # the CLI never ran
        return answer

    def test_authentication_is_checked_before_the_cli(self):
        port = self.port
        self.refused('no token', 403, **{'X-Forge-UI-Token': None})
        self.refused('wrong token', 403, **{'X-Forge-UI-Token': 'x' * 43})
        self.refused('empty token', 403, **{'X-Forge-UI-Token': ''})
        self.refused('prefix of the token', 403, **{'X-Forge-UI-Token': self.server.token[:-1]})
        self.refused('longer token', 403, **{'X-Forge-UI-Token': self.server.token + 'x'})
        self.refused('non-ASCII token', 403, **{'X-Forge-UI-Token': 'é' * 43})
        self.refused('bad host', 403, Host=f'evil.example:{port}')
        self.refused('bad host without port', 403, Host='evil.example')
        self.refused('foreign origin', 403, Origin='http://evil.example')
        self.refused('origin of another port', 403, Origin=f'http://127.0.0.1:{port + 1}')
        self.refused('https origin', 403, Origin=f'https://127.0.0.1:{port}')
        self.refused('null origin', 403, Origin='null')
        self.refused('origin that is not the Host', 403, Origin=f'http://localhost:{port}')
        for label, kind in (('text', 'text/plain'), ('form', 'application/x-www-form-urlencoded'),
                            ('none', None), ('json suffix', 'application/jsonx'), ('multipart', 'multipart/form-data')):
            self.refused('content type ' + label, 400, **{'Content-Type': kind})

    def test_the_right_origin_and_content_type_variants_pass(self):
        for changes in ({}, {'Origin': f'http://127.0.0.1:{self.port}'},
                        {'Content-Type': 'application/json; charset=utf-8'}, {'Content-Type': 'Application/JSON'}):
            self.behave(stdout='{"ok": true}')
            self.assertEqual(self.post(self.BODY, **changes), (200, {'ok': True}), changes)

    def test_body_limits_and_shape(self):
        for label, body in (('not JSON', b'{"action":'), ('array', b'[]'), ('string', b'"x"'), ('null', b'null'),
                            ('number', b'1'), ('not UTF-8', b'{"a": "\xff"}'), ('empty', b'')):
            self.refused(label, 400, body)
        padded = lambda size: b'{"action": "bogus"}' + b' ' * (size - len(b'{"action": "bogus"}'))
        # exactly 64 KiB is read (and then refused by the route, not by the size check)
        self.assertEqual(self.post(padded(65536))[0], 400)
        self.assertEqual(self.post(padded(65537))[0], 413)
        self.assertEqual(self.post(padded(300000))[0], 413)
        self.assertEqual(self.calls(), [])
        # a body that claims far more than it sends is refused without waiting for it
        started = time.monotonic()
        status, _, body = self.raw(self.request_bytes(b'', length=10 ** 10))
        self.assertEqual((status, json.loads(body)), (413, {'error': 'payload too large'}))
        self.assertLess(time.monotonic() - started, 10)

    def request_bytes(self, body, length=None, extra=b''):
        head = (b'POST /api/runs/%s/controls HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Forge-UI-Token: %s\r\n'
                b'Content-Type: application/json\r\nConnection: close\r\n' % (self.key.encode(), self.port, self.server.token.encode()))
        if length is not None:
            head += b'Content-Length: %d\r\n' % length
        return head + extra + b'\r\n' + body

    def test_a_body_without_a_usable_length_is_refused(self):
        body = json.dumps(self.BODY).encode()
        self.assertEqual(self.raw(self.request_bytes(body))[0], 400)  # no Content-Length
        self.assertEqual(self.raw(self.request_bytes(body, extra=b'Content-Length: abc\r\n'))[0], 400)
        self.assertEqual(self.raw(self.request_bytes(body, extra=b'Content-Length: -5\r\n'))[0], 400)
        chunked = b'%x\r\n%s\r\n0\r\n\r\n' % (len(body), body)
        self.assertEqual(self.raw(self.request_bytes(chunked, extra=b'Transfer-Encoding: chunked\r\n'))[0], 400)
        self.assertEqual(self.calls(), [])

    def test_a_preflight_is_a_405_without_cors_headers(self):
        path = f'/api/runs/{self.key}/controls'
        for headers in ({}, {'Origin': 'http://evil.example', 'Access-Control-Request-Method': 'POST',
                             'Access-Control-Request-Headers': 'x-forge-ui-token, content-type'},
                        self.headers()):
            status, answer, body = self.send('OPTIONS', path, headers=headers)
            self.assertEqual((status, json.loads(body)), (405, {'error': 'method not allowed'}))
            self.assertFalse([name for name in answer if name.startswith('access-control-')], answer)
            self.assertEqual(answer['allow'], 'GET, HEAD, POST')
        self.assertEqual(self.send('OPTIONS', path, headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.calls(), [])

    def test_a_post_to_a_read_route_is_a_405_after_authentication(self):
        for path in ('/api/runs', f'/api/runs/{self.key}', '/api/health', '/', '/api/nowhere',
                     f'/api/runs/{self.key}/controls/x', f'/api/runs/{self.key}/export'):
            self.assertEqual(self.post({}, path=path)[0], 405, path)
            self.assertEqual(self.post({}, path=path, **{'X-Forge-UI-Token': None})[0], 403, path)
        self.assertEqual(self.calls(), [])


class ArgvAndMappingTests(ControlApiCase):
    def last(self):
        return self.calls()[-1]

    def test_each_action_builds_its_exact_argv(self):
        plan = str(Path(os.path.realpath(self.plan)))
        self.behave(stdout='{"ok": true, "changed": {}, "applies": "now", "controls": {}}')
        cases = [
            ({'action': 'set', 'key': 'qa-threshold', 'value': 'P2'},
             ['control', 'set', plan, 'qa-threshold', 'P2', '--actor', 'ui', '--json']),
            ({'action': 'set', 'key': 'retry-budget', 'value': 'inherit', 'task': 'reader'},
             ['control', 'set', plan, 'retry-budget', 'inherit', '--task', 'reader', '--actor', 'ui', '--json']),
            ({'action': 'pin', 'task': 'graph', 'spec': 'sol:high:codex'},
             ['control', 'pin', plan, 'graph', 'sol:high:codex', '--actor', 'ui', '--json']),
            ({'action': 'unpin', 'task': 'graph'}, ['control', 'unpin', plan, 'graph', '--actor', 'ui', '--json']),
        ]
        for body, argv in cases:
            status, answer = self.post(body)
            self.assertEqual((status, answer['ok'], self.last()['argv']), (200, True, argv), body)
        # a call is bare: no stdin, a process group of its own
        self.assertEqual((self.last()['stdin'], self.last()['leader']), ('', True))
        status, headers, data = self.send('GET', f'/api/runs/{self.key}/controls')
        self.assertEqual((status, json.loads(data)['ok'], self.last()['argv']), (200, True, ['control', 'show', plan, '--json']))
        self.assertEqual(headers['content-type'], 'application/json; charset=utf-8')

    def test_the_run_actions_and_retry_build_their_exact_argv(self):
        plan = str(Path(os.path.realpath(self.plan)))
        self.behave(stdout='{"ok": true}')
        cases = [
            ({'action': 'drain'}, ['control', 'drain', plan, '--actor', 'ui', '--json']),
            ({'action': 'resume'}, ['control', 'resume', plan, '--actor', 'ui', '--json']),
            ({'action': 'stop'}, ['control', 'stop', plan, '--actor', 'ui', '--json']),
            ({'action': 'stop', 'now': False}, ['control', 'stop', plan, '--actor', 'ui', '--json']),
            ({'action': 'stop', 'now': True}, ['control', 'stop', plan, '--now', '--actor', 'ui', '--json']),
            ({'action': 'retry', 'task': 'graph'}, ['control', 'retry', plan, 'graph', '--actor', 'ui', '--json']),
            ({'action': 'retry', 'task': 'graph', 'dwarf': 'sol:high:codex'},
             ['control', 'retry', plan, 'graph', '--dwarf', 'sol:high:codex', '--actor', 'ui', '--json']),
        ]
        for body, argv in cases:
            status, _answer = self.post(body)
            self.assertEqual((status, self.last()['argv']), (200, argv), body)

    def test_the_cli_is_called_by_argv_with_values_it_cannot_misread(self):
        self.behave(stdout='{"ok": true}')
        value = 'x; touch /tmp/pwned $(id) `id` "\' *'
        self.assertEqual(self.post({'action': 'set', 'key': 'timeout', 'value': value})[0], 200)
        self.assertEqual(self.last()['argv'][3:5], ['timeout', value])

    def test_exit_codes_map_to_statuses_and_keep_the_result(self):
        result = {'ok': False, 'why': 'x'}
        body = {'action': 'unpin', 'task': 'page'}
        long = 'z' * 3000
        # A refusal (exit 3) keeps its whole message: what was refused, then what to run instead.
        cases = [(2, 400, 'invalid', 'last line', 300), (3, 409, 'refused', 'first line\nlast line', 2000),
                 (4, 500, 'failed', 'last line', 300), (1, 502, 'bad gateway', 'last line', 300),
                 (7, 502, 'bad gateway', 'last line', 300), (127, 502, 'bad gateway', 'last line', 300)]
        for code, status, error, detail, cap in cases:
            self.behave(code=code, stdout=json.dumps(result), stderr='first line\n\n  last line  \n')
            got, answer = self.post(body)
            self.assertEqual((got, answer), (status, {'error': error, 'detail': detail, 'result': result}), code)
            self.behave(code=code, stdout='not json', stderr=long + '\n\n')
            got, answer = self.post(body)
            self.assertEqual((got, answer), (status, {'error': error, 'detail': 'z' * cap}), code)
            self.behave(code=code, stdout='[1, 2]')  # JSON, but not an object
            got, answer = self.post(body)
            self.assertEqual((got, 'result' in answer, answer['detail'] != ''), (status, False, True), code)
        # a call killed by a signal is a gateway failure; so is a 0 that printed no JSON object
        self.behave(signal=9)
        self.assertEqual(self.post(body)[0], 502)
        for stdout in ('', 'oops', '[]'):
            self.behave(stdout=stdout)
            self.assertEqual(self.post(body)[0], 502, stdout)
        self.behave(code=3, stdout='{"a": 1}', stderr='forge control: busy\n')
        self.assertEqual(self.send('GET', f'/api/runs/{self.key}/controls')[0], 409)

    def test_a_missing_forge_cli_is_a_502(self):
        with patch.object(ui_server, 'forge_cli', self.base / 'no-such-forge'):
            status, answer = self.post({'action': 'unpin', 'task': 'page'})
        self.assertEqual((status, answer['error']), (502, 'bad gateway'))

    def test_shapes_and_types_are_checked_before_the_cli(self):
        bad = [
            {}, {'action': 'delete'}, {'action': 5}, {'action': None}, {'action': 'set'},
            {'action': 'set', 'key': 'qa-threshold'}, {'action': 'set', 'key': 'qa-threshold', 'value': 2},
            {'action': 'set', 'key': 'qa-threshold', 'value': None}, {'action': 'set', 'key': 3, 'value': 'P2'},
            {'action': 'set', 'key': '', 'value': 'P2'}, {'action': 'set', 'key': 'qa-threshold', 'value': ''},
            {'action': 'set', 'key': 'qa-threshold', 'value': 'P2', 'task': 5},
            {'action': 'set', 'key': 'qa-threshold', 'value': 'P2', 'task': ''},
            {'action': 'set', 'key': 'qa-threshold', 'value': ['P2']},
            {'action': 'set', 'key': 'qa-threshold', 'value': 'P2', 'extra': 1},
            {'action': 'set', 'key': 'qa-threshold', 'value': '--json'},
            {'action': 'set', 'key': 'qa-threshold', 'value': 'P2', 'task': '--task'},
            {'action': 'set', 'key': 'qa-threshold', 'value': 'P2\x00'}, {'action': 'set', 'key': 'x', 'value': 'a\nb'},
            {'action': 'set', 'key': 'qa-threshold', 'value': 'P' * 300},
            {'action': 'pin', 'task': 'page'}, {'action': 'pin', 'spec': 'sol'}, {'action': 'pin', 'task': 1, 'spec': 'sol'},
            {'action': 'pin', 'task': 'page', 'spec': ['sol']}, {'action': 'pin', 'task': 'page', 'spec': '-x'},
            {'action': 'pin', 'task': 'page', 'spec': 'sol', 'value': 'x'},
            {'action': 'unpin'}, {'action': 'unpin', 'task': ''}, {'action': 'unpin', 'task': 'page', 'spec': 'sol'},
            {'action': 'unpin', 'task': True},
            {'action': 'stop', 'now': 'yes'}, {'action': 'stop', 'now': 1}, {'action': 'stop', 'now': None},
            {'action': 'retry'}, {'action': 'retry', 'task': ''}, {'action': 'retry', 'task': 'graph', 'dwarf': '-x'},
            {'action': 'retry', 'task': 'graph', 'dwarf': 5}, {'action': 'retry', 'task': 'graph', 'spec': 'sol'},
            {'action': 'drain', 'task': 'graph'}, {'action': 'resume', 'now': True}, {'action': 'add', 'task': 'graph'},
        ]
        for body in bad:
            status, answer = self.post(body)
            self.assertEqual((status, answer['error']), (400, 'invalid'), body)
            self.assertTrue(answer['detail'], body)
        self.assertEqual(self.calls(), [])

    def test_only_a_plan_run_found_by_its_key_has_controls(self):
        body = {'action': 'unpin', 'task': 'page'}
        for key in ('0' * 12, self.solo_key):
            self.assertEqual(self.post(body, path=f'/api/runs/{key}/controls')[0], 404, key)
            self.assertEqual(self.send('GET', f'/api/runs/{key}/controls')[0], 404, key)
        # a path, a relative path or a short key never names a run
        for key in (str(self.plan), '..%2fplan', self.key[:11], self.key.upper(), self.key + '0'):
            self.assertEqual(self.send('GET', f'/api/runs/{key}/controls')[0], 404, key)
            self.assertEqual(self.post(body, path=f'/api/runs/{key}/controls')[0], 405, key)
        self.assertEqual(self.calls(), [])
        # the run dir is the one the catalog knows for the key, whatever the body says
        self.behave(stdout='{"ok": true}')
        self.post(dict(body, plan='/etc', dir='/etc', path='/etc'))
        self.assertEqual(self.calls(), [])  # unknown fields are refused


class LoggingTests(ControlApiCase):
    def test_a_write_logs_its_route_key_and_status_but_never_the_token(self):
        self.behave(stdout='{"ok": true}')
        loud = self.start(quiet=False)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.post({'action': 'unpin', 'task': 'page'}, server=loud)
            self.behave(code=3, stdout='{"ok": false}', stderr='refused\n')
            self.post({'action': 'unpin', 'task': 'page'}, server=loud)
            self.post({'action': 'bogus'}, server=loud)
            self.post({'action': 'unpin', 'task': 'page'}, server=loud, **{'X-Forge-UI-Token': 'nope'})
        lines = stderr.getvalue().splitlines()
        self.assertIn(f'forge-ui: control unpin {self.key} -> 200', lines)
        self.assertIn(f'forge-ui: control unpin {self.key} -> 409', lines)
        self.assertIn(f'forge-ui: control controls {self.key} -> 400', lines)
        self.assertEqual(len([line for line in lines if line.startswith('forge-ui: control ')]), 3)  # not the refusal
        self.assertNotIn(loud.token, stderr.getvalue())
        quiet = io.StringIO()
        with contextlib.redirect_stderr(quiet):
            self.behave(stdout='{"ok": true}')
            self.post({'action': 'unpin', 'task': 'page'})
        self.assertEqual(quiet.getvalue(), '')


class TimeoutAndBusyTests(ControlApiCase):
    def test_a_timed_out_call_is_killed_with_its_children(self):
        late = self.base / 'late'
        self.behave(sleep=30, late_file=str(late))
        started = time.monotonic()
        with patch.object(control, 'TIMEOUT_S', 1):
            status, answer = self.post({'action': 'unpin', 'task': 'page'})
        self.assertEqual((status, answer), (504, {
            'error': 'timeout', 'detail': 'the operation may have partly completed; reload to see the current state'}))
        self.assertLess(time.monotonic() - started, 8)
        time.sleep(4)  # the child would have written its file after 3 s
        self.assertFalse(late.exists())
        # the run is not left locked
        self.behave(stdout='{"ok": true}')
        self.assertEqual(self.post({'action': 'unpin', 'task': 'page'})[0], 200)

    def test_a_call_that_ignores_sigterm_is_killed_and_reaped_before_the_lock_is_released(self):
        pid_file = self.base / 'pid'
        self.behave(sleep=30, ignore_term=True, pid_file=str(pid_file))
        # long enough for the fake to start and ignore TERM even on a busy machine: a fake TERMed
        # before that dies at once and the path under test is never taken
        with patch.object(control, 'TIMEOUT_S', 4), patch.object(control, 'TERM_WAIT_S', 0.5):
            status, answer = self.post({'action': 'unpin', 'task': 'page'})
        self.assertEqual((status, answer['error']), (504, 'timeout'))
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)  # gone, and reaped (a zombie would still answer)

    def test_a_timeout_on_the_show_route_is_a_504_too(self):
        self.behave(sleep=30)
        with patch.object(control, 'TIMEOUT_S', 1):
            status, _, data = self.send('GET', f'/api/runs/{self.key}/controls')
        self.assertEqual((status, json.loads(data)['error']), (504, 'timeout'))

    def test_a_second_write_to_a_run_that_is_being_changed_is_busy(self):
        self.behave(sleep=2, stdout='{"ok": true}')
        first = []
        thread = threading.Thread(target=lambda: first.append(self.post({'action': 'unpin', 'task': 'page'})))
        thread.start()
        self.wait_for_call()
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.post({'action': 'unpin', 'task': 'reader'}), (409, {'error': 'busy'}))
        self.assertEqual(self.post({'action': 'set', 'key': 'timeout', 'value': '5'}), (409, {'error': 'busy'}))
        thread.join()
        self.assertEqual(first, [(200, {'ok': True})])
        self.assertEqual(len(self.calls()), 1)  # the refused ones never reached the CLI
        # once it is done the run takes writes again
        self.behave(stdout='{"ok": true}')
        self.assertEqual(self.post({'action': 'unpin', 'task': 'page'}), (200, {'ok': True}))

    def test_writes_to_other_runs_are_not_blocked_by_a_busy_one(self):
        other = self.base / 'plan2'
        shutil.copytree(FIXTURES / 'plan-full', other)
        server = self.start()
        server.catalog = reader.Catalog(plans=[self.plan, other], registry=False)
        self.behave(sleep=1.5, stdout='{"ok": true}')
        thread = threading.Thread(target=lambda: self.post({'action': 'unpin', 'task': 'page'}, server=server))
        thread.start()
        self.wait_for_call()
        self.behave(stdout='{"ok": true}')
        status, _ = self.post({'action': 'unpin', 'task': 'page'}, path=f'/api/runs/{reader.run_key(other)}/controls',
                              server=server)
        thread.join()
        self.assertEqual(status, 200)


class ReadOnlyTests(ControlApiCase):
    read_only = True

    def test_every_post_is_a_405_and_the_page_has_no_token(self):
        for obj in ({'action': 'unpin', 'task': 'page'}, {}):
            for changes in ({}, {'X-Forge-UI-Token': None}, {'Content-Type': 'text/plain'}):
                status, answer = self.post(obj, **changes)
                self.assertEqual((status, answer), (405, {'error': 'method not allowed'}))
        status, headers, _ = self.send('POST', f'/api/runs/{self.key}/controls', b'{}', self.headers())
        self.assertEqual((status, headers['allow']), (405, 'GET, HEAD'))
        self.assertEqual(self.send('POST', '/api/runs', b'{}', self.headers(Host='evil.example'))[0], 403)
        self.assertEqual(self.send('OPTIONS', f'/api/runs/{self.key}/controls')[2], b'{"error": "method not allowed"}')
        self.assertEqual(self.calls(), [])
        status, _, body = self.send('GET', '/')
        self.assertEqual(status, 200)
        self.assertNotIn(b'forge-ui-token', body)
        self.assertEqual(body, (ui_server.STATIC_DIR / 'index.html').read_bytes())
        health = json.loads(self.send('GET', '/api/health')[2])
        self.assertIs(health['controls'], False)
        # reading the controls is still a read
        self.behave(stdout='{"run": {}}')
        self.assertEqual(self.send('GET', f'/api/runs/{self.key}/controls')[0], 200)

    def test_the_write_enabled_server_says_so(self):
        writable = self.start(read_only=False)
        self.assertIs(json.loads(self.send('GET', '/api/health', server=writable)[2])['controls'], True)

    def test_the_flag_and_the_environment_select_the_mode(self):
        self.assertTrue(_parser().parse_args(['--read-only']).read_only)
        self.assertFalse(_parser().parse_args([]).read_only)
        self.assertTrue(read_only_requested(True, {}))
        for value in ('1', 'true', 'YES', ' on '):
            self.assertTrue(read_only_requested(False, {'FORGE_UI_READONLY': value}), value)
        for value in ('', '0', 'no', 'off', 'false'):
            self.assertFalse(read_only_requested(False, {'FORGE_UI_READONLY': value}), value)
        self.assertFalse(read_only_requested(False, {}))


class CliEnvTests(unittest.TestCase):
    """The forge configuration `forge ui open` hands the server reaches every CLI, and nothing else does."""

    def test_every_cli_gets_the_configuration_on_top_of_the_servers_environment(self):
        given = {'FORGE_REGISTRY': '/elsewhere/custom.tsv', 'FORGE_DATA_HOME': '/elsewhere/data'}
        before = dict(os.environ)
        printer = [sys.executable, '-c', 'import json, os; print(json.dumps(dict(os.environ)))']
        with patch.object(control, 'cli_env', given):
            seen = json.loads(control.run_cli(printer, 30).stdout)
        self.assertEqual({name: seen.get(name) for name in given}, given)
        self.assertEqual(seen.get('PATH'), os.environ.get('PATH'))  # the server's environment is still there
        self.assertEqual(dict(os.environ), before)  # and the server's own is unchanged

    def test_only_the_named_configuration_is_taken_from_the_shell(self):
        shell = {'FORGE_REGISTRY': 'custom.tsv', 'FORGE_DATA_HOME': '~/data', 'FORGE_DATASET': 'off',
                 'XDG_DATA_HOME': '', 'FORGE_UI_PORT': '0', 'FORGE_SHADOW': '1', 'FORGE_TYPESAFE_KEY': 'k', 'HOME': '/h'}
        self.assertEqual(control.cli_env_from(shell), {
            'FORGE_REGISTRY': os.path.abspath('custom.tsv'),  # relative to the shell, not to the server
            'FORGE_DATA_HOME': '~/data', 'FORGE_DATASET': 'off'})
        self.assertEqual(control.check_cli_env({'FORGE_DATASET': 'off'}), {'FORGE_DATASET': 'off'})
        # a leading ~ is kept only for the reader that expands it: nothing expands FORGE_REGISTRY's
        self.assertEqual(control.cli_env_from({'FORGE_REGISTRY': '~/x.tsv', 'FORGE_DATA_HOME': '~/d'}), {
            'FORGE_REGISTRY': os.path.abspath('~/x.tsv'), 'FORGE_DATA_HOME': '~/d'})
        self.assertTrue(control.cli_env_from({'FORGE_REGISTRY': '~/x.tsv'})['FORGE_REGISTRY'].endswith('/~/x.tsv'))
        for bad in ([], {'FORGE_SHADOW': '1'}, {'FORGE_REGISTRY': 3}, {'FORGE_REGISTRY': ''}, {'FORGE_REGISTRY': 'a\x00b'}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                control.check_cli_env(bad)


class RealForgeTests(ControlApiCase):
    """The actual `forge control` on a copy of plan-full, through the API."""

    def setUp(self):
        super().setUp()
        for name, value in (('forge_cli', ROOT / 'forge'),):
            patcher = patch.object(ui_server, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict(os.environ, {'FORGE_DATASET': 'off'})  # a test must not add to a real dataset
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_show_and_set_run_the_real_cli(self):
        path = f'/api/runs/{self.key}/controls'
        status, _, data = self.send('GET', path)
        shown = json.loads(data)
        self.assertEqual((status, shown['run']['qa_threshold'], shown['live']), (200, 'P2', False))
        self.assertEqual(sorted(shown['tasks']), ['graph', 'page', 'reader'])

        status, answer = self.post({'action': 'set', 'key': 'qa-threshold', 'value': 'P2'})
        self.assertEqual(status, 200, answer)
        self.assertTrue(answer['ok'])
        self.assertEqual((answer['changed']['key'], answer['changed']['value']), ('qa-threshold', 'P2'))
        self.assertEqual(answer['controls']['run']['qa_threshold'], 'P2')
        self.assertEqual((self.plan / 'qa_threshold').read_text().strip(), 'P2')
        shown = json.loads(self.send('GET', path)[2])
        self.assertEqual(shown['run']['qa_threshold'], 'P2')
        self.assertEqual([(entry['actor'], entry['key'], entry['value']) for entry in shown['log']],
                         [('ui', 'qa-threshold', 'P2')])

    def test_the_cli_refusals_come_back_with_their_statuses(self):
        status, answer = self.post({'action': 'set', 'key': 'qa-threshold', 'value': 'P9'})
        self.assertEqual((status, answer['error']), (400, 'invalid'))
        self.assertIn('qa-threshold', answer['detail'])
        status, answer = self.post({'action': 'set', 'key': 'no-such-key', 'value': 'P2'})
        self.assertEqual((status, answer['error']), (400, 'invalid'))
        status, answer = self.post({'action': 'set', 'key': 'retry-budget', 'value': '2'})  # no live scheduler
        self.assertEqual((status, answer['error']), (409, 'refused'))
        self.assertIn('live', answer['detail'])
        self.assertFalse((self.plan / 'retry_budget').exists())
        status, answer = self.post({'action': 'unpin', 'task': 'reader'})  # MERGED: refused by the CLI
        self.assertEqual((status, answer['error']), (409, 'refused'))
        self.assertIn('MERGED', answer['detail'])
        status, answer = self.post({'action': 'drain'})                    # no live scheduler to drain
        self.assertEqual((status, answer['error']), (409, 'refused'))
        self.assertIn('live', answer['detail'])
        self.assertFalse((self.plan / 'drain').exists())


if __name__ == '__main__':
    unittest.main()
