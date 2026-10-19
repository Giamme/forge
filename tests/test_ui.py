"""`forge ui open|status|stop`: one shared, detached forge-ui server for every run."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))

from forge_ui import build_id  # noqa: E402
from forge_ui.reader import run_key  # noqa: E402

LAUNCHER = ROOT / 'scripts/forge-ui.py'
FIXTURES = ROOT / 'ui/tests/fixtures'


def get_json(port, path):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        conn.request('GET', path)
        return json.loads(conn.getresponse().read())
    finally:
        conn.close()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class UiLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='forge-ui-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = self.tmp / 'state'
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.state), XDG_CONFIG_HOME=str(self.tmp / 'config'),
                        FORGE_UI_BROWSER='off', FORGE_UI_PORT='0', FORGE_UI_IDLE_S='120')
        self.addCleanup(self._stop_server)
        self.plan = self._copy('plan-full')
        self.solo = self._copy('solo-full')

    def _copy(self, name, as_name=None):
        target = self.tmp / (as_name or name)
        shutil.copytree(FIXTURES / name, target)
        return target

    def _recorded_pid(self, state):
        path = state / 'forge/ui/server.json'
        try:
            pid = json.loads(path.read_text())['pid'] if path.is_file() else None  # never read a FIFO
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return pid if isinstance(pid, int) else None

    def _stop_server(self, state=None):
        pid = self._recorded_pid(state or self.state)
        if pid is None:
            return
        self.ui('stop', env=dict(self.env, XDG_STATE_HOME=str(state or self.state)))
        if alive(pid):  # stop did not take it down: do not leave it for the idle timeout
            os.kill(pid, signal.SIGKILL)

    def ui(self, *args, env=None, timeout=60):
        return subprocess.run([sys.executable, str(LAUNCHER), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=timeout)

    def server_json(self):
        return json.loads((self.state / 'forge/ui/server.json').read_text())

    def open(self, directory, *extra, env=None):
        result = self.ui('open', str(directory), *extra, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_open_serves_the_run_until_stopped(self):
        self.assertEqual(self.ui('status').returncode, 1)

        out = self.open(self.plan)
        port = self.server_json()['port']
        self.assertEqual(out, f'forge-ui: http://127.0.0.1:{port}/#/runs/{run_key(self.plan)}')

        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
        conn.request('GET', '/')
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        keys = [run['key'] for run in get_json(port, '/api/runs')['runs']]
        self.assertIn(run_key(self.plan), keys)
        status = self.ui('status', '--json')
        self.assertEqual(status.returncode, 0)
        self.assertEqual(json.loads(status.stdout)['port'], port)

        pid = self.server_json()['pid']
        self.assertEqual(self.ui('stop').returncode, 0)
        self.assertFalse(alive(pid))
        self.assertEqual(self.ui('status').returncode, 1)
        self.assertEqual(self.ui('stop').returncode, 0)  # stopping a stopped server is fine

    def test_forge_ui_is_routed_from_the_forge_command(self):
        result = subprocess.run([str(ROOT / 'forge'), 'ui', 'status'], env=self.env, capture_output=True,
                                text=True, timeout=60)
        self.assertEqual((result.returncode, result.stdout.strip()), (1, 'forge-ui: not running'))

    def test_a_second_open_reuses_the_server_and_opens_the_browser_once_per_run(self):
        calls = self.tmp / 'browser.log'
        stub = self.tmp / 'browser.sh'
        stub.write_text(f'#!/bin/sh\necho "$1" >> {calls}\n')
        stub.chmod(0o755)
        env = dict(self.env, BROWSER=str(stub))
        env.pop('FORGE_UI_BROWSER')

        self.open(self.plan, env=env)
        first = self.server_json()
        self.open(self.plan, env=env)
        self.open(self.solo, env=env)
        second = self.server_json()

        self.assertEqual((second['pid'], second['port']), (first['pid'], first['port']))
        keys = [run['key'] for run in get_json(first['port'], '/api/runs')['runs']]
        self.assertIn(run_key(self.plan), keys)
        self.assertIn(run_key(self.solo), keys)
        base = f'http://127.0.0.1:{first["port"]}/#/runs/'
        self.assertEqual(calls.read_text().split(), [base + run_key(self.plan), base + run_key(self.solo)])

    def _browser_stub(self, body):
        stub = self.tmp / 'browser.sh'
        stub.write_text('#!/bin/sh\n' + body)
        stub.chmod(0o755)
        env = dict(self.env, BROWSER=str(stub))
        env.pop('FORGE_UI_BROWSER')
        return env

    def test_a_run_opened_with_no_browser_still_opens_one_when_asked_later(self):
        calls = self.tmp / 'browser.log'
        env = self._browser_stub(f'echo "$1" >> {calls}\n')
        self.open(self.plan, '--no-browser', env=env)
        self.assertFalse(calls.exists())
        self.open(self.plan, env=env)
        self.open(self.plan, env=env)
        self.assertEqual(len(calls.read_text().split()), 1)

    def test_a_browser_command_that_hangs_does_not_hold_open(self):
        pidfile = self.tmp / 'browser.pid'
        env = self._browser_stub(f'echo $$ > {pidfile}\nexec sleep 30\n')
        self.addCleanup(lambda: pidfile.exists() and subprocess.run(['kill', pidfile.read_text().strip()], capture_output=True))
        self.open(self.plan, '--no-browser', env=env)  # server up: only the browser launch is timed
        start = time.monotonic()
        self.open(self.solo, env=env)
        self.assertLess(time.monotonic() - start, 6)
        self.assertTrue(pidfile.exists(), 'the browser command was never run')

    def test_no_browser_flag_and_forge_ui_browser_off_both_keep_the_browser_closed(self):
        calls = self.tmp / 'browser.log'
        stub = self.tmp / 'browser.sh'
        stub.write_text(f'#!/bin/sh\necho "$1" >> {calls}\n')
        stub.chmod(0o755)
        env = dict(self.env, BROWSER=str(stub))
        self.open(self.plan, env=env)  # FORGE_UI_BROWSER=off from setUp
        env.pop('FORGE_UI_BROWSER')
        self.open(self.solo, '--no-browser', env=env)
        self.assertFalse(calls.exists())

    def test_a_dead_pid_in_the_state_file_gives_a_new_server(self):
        gone = subprocess.Popen(['true'])
        gone.wait()
        (self.state / 'forge/ui').mkdir(parents=True)
        (self.state / 'forge/ui/server.json').write_text(json.dumps(
            {'pid': gone.pid, 'port': 1, 'build': build_id(), 'registry': str(self.tmp / 'config/forge/runs')}))

        self.open(self.plan)
        recorded = self.server_json()
        self.assertNotEqual(recorded['pid'], gone.pid)
        self.assertEqual(get_json(recorded['port'], '/api/health')['pid'], recorded['pid'])

    def test_a_busy_preferred_port_falls_back_to_a_free_one(self):
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen(1)
            taken = busy.getsockname()[1]
            self.open(self.plan, '--port', str(taken))
            port = self.server_json()['port']
            self.assertNotEqual(port, taken)
            self.assertEqual(get_json(port, '/api/health')['app'], 'forge-ui')

    def test_a_live_pid_that_is_not_forge_ui_is_never_signalled(self):
        bystander = subprocess.Popen(['sleep', '60'])
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        (self.state / 'forge/ui').mkdir(parents=True)
        (self.state / 'forge/ui/server.json').write_text(json.dumps(
            {'pid': bystander.pid, 'port': 1, 'build': 'old', 'registry': 'elsewhere'}))

        self.open(self.plan)
        self.assertNotEqual(self.server_json()['pid'], bystander.pid)
        self.assertEqual(self.ui('stop').returncode, 0)
        self.assertIsNone(bystander.poll(), 'the launcher signalled a pid it never confirmed as forge-ui')

    def test_a_forge_ui_answering_with_another_pid_is_not_the_recorded_server(self):
        # The recorded pid was reused by an unrelated process and the port by another forge-ui.
        bystander = subprocess.Popen(['sleep', '60'])
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        answer = json.dumps({'app': 'forge-ui', 'pid': bystander.pid + 1, 'build': build_id()}).encode()

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        impostor = HTTPServer(('127.0.0.1', 0), Health)
        threading.Thread(target=impostor.serve_forever, daemon=True).start()
        self.addCleanup(impostor.server_close)
        self.addCleanup(impostor.shutdown)
        (self.state / 'forge/ui').mkdir(parents=True)
        (self.state / 'forge/ui/server.json').write_text(json.dumps(
            {'pid': bystander.pid, 'port': impostor.server_address[1], 'build': build_id(),
             'registry': str(self.tmp / 'config/forge/runs')}))

        self.open(self.plan)
        self.assertNotEqual(self.server_json()['pid'], bystander.pid)
        self.ui('stop')
        self.assertIsNone(bystander.poll())

    def test_a_server_running_other_code_or_another_registry_is_replaced(self):
        for label, change in (('build', {'build': 'old'}), ('registry', {'registry': '/elsewhere/runs'})):
            with self.subTest(label):
                self.open(self.plan)
                old = self.server_json()
                (self.state / 'forge/ui/server.json').write_text(json.dumps(dict(old, **change)))

                self.open(self.plan)
                new = self.server_json()
                self.assertNotEqual(new['pid'], old['pid'])
                self.assertFalse(alive(old['pid']))
                self.assertEqual(new['build'], build_id())
                self.assertEqual(get_json(new['port'], '/api/health')['pid'], new['pid'])
                self.ui('stop')

    def test_a_server_whose_health_reports_another_build_is_replaced(self):
        # server.json still claims the current build; only the live answer says the code is old.
        self.open(self.plan)
        registry = self.server_json()['registry']
        self.assertEqual(self.ui('stop').returncode, 0)
        old = subprocess.Popen(['sleep', '60'])
        self.addCleanup(old.wait)
        self.addCleanup(old.kill)
        answer = json.dumps({'app': 'forge-ui', 'pid': old.pid, 'build': 'old'}).encode()

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)

            def log_message(self, *args):
                pass

        stale = HTTPServer(('127.0.0.1', 0), Health)
        threading.Thread(target=stale.serve_forever, daemon=True).start()
        self.addCleanup(stale.server_close)
        self.addCleanup(stale.shutdown)
        (self.state / 'forge/ui/server.json').write_text(json.dumps(
            {'pid': old.pid, 'port': stale.server_address[1], 'build': build_id(), 'registry': registry}))

        self.open(self.plan)
        self.assertEqual(old.wait(timeout=5), -signal.SIGTERM)
        self.assertNotEqual(self.server_json()['pid'], old.pid)

    def test_exit_codes_for_arguments_that_are_not_runs(self):
        notes = self.tmp / 'notes'
        notes.mkdir()
        (notes / 'prompt.md').write_text('just notes\n')  # prompt.md without repo
        no_repo = self._copy('solo-full', 'repo-is-a-dir')
        (no_repo / 'repo').unlink()
        (no_repo / 'repo').mkdir()  # DIR/repo exists but cannot be read as a file
        garbled = self._copy('solo-full', 'repo-is-not-text')
        (garbled / 'repo').write_bytes(b'/tmp/\xff\xfe\n')
        for label, args, code in (('missing dir', ['open', str(self.tmp / 'nope')], 3),
                                  ('prompt without repo', ['open', str(notes)], 3),
                                  ('unreadable repo file', ['open', str(no_repo)], 3),
                                  ('undecodable repo file', ['open', str(garbled)], 3),
                                  ('unknown command', ['frobnicate'], 2),
                                  ('bad port', ['open', '--port', 'x'], 2)):
            with self.subTest(label):
                self.assertEqual(self.ui(*args).returncode, code)
        self.assertEqual(self.ui('status').returncode, 1, 'a rejected DIR must not start a server')

    def test_a_repo_file_that_is_a_fifo_is_refused_without_blocking(self):
        fifo = self._copy('plan-full', 'repo-is-a-fifo')
        (fifo / 'repo').unlink()
        os.mkfifo(fifo / 'repo')  # no writer: reading it would block forever
        start = time.monotonic()
        try:
            result = self.ui('open', str(fifo), timeout=8)
        except subprocess.TimeoutExpired:
            self.fail('open blocked on a repo file that is a FIFO')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertLess(time.monotonic() - start, 4)

    @unittest.skipIf(os.geteuid() == 0, 'root can read a chmod 000 directory')
    def test_an_unreadable_dir_is_refused_without_a_traceback(self):
        locked = self._copy('plan-full', 'locked')
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o755)
        result = self.ui('open', str(locked))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_hostile_state_gives_the_documented_exit_code_without_a_traceback(self):
        # open: state it cannot use is exit 4 naming the path; a server.json it cannot read
        # or parse means no server, so it starts one. status: unreadable state is exit 1.
        # stop: exit 0 either way.
        def home_read_only(home, env):
            home.mkdir()
            home.chmod(0o500)
            self.addCleanup(home.chmod, 0o755)

        def record(content):
            def setup(home, env):
                (home / 'forge/ui').mkdir(parents=True)
                (home / 'forge/ui/server.json').write_bytes(content)
            return setup

        def record_unreadable(home, env):
            record(b'{}')(home, env)
            (home / 'forge/ui/server.json').chmod(0)

        def record_is_a_fifo(home, env):  # reading it would block forever
            (home / 'forge/ui').mkdir(parents=True)
            os.mkfifo(home / 'forge/ui/server.json')

        def lock_is_a_dir(home, env):
            (home / 'forge/ui/lock').mkdir(parents=True)

        def live_server_recorded_with_a_list_build(home, env):
            self.assertEqual(self.ui('open', '--no-browser', env=env).returncode, 0)
            path = home / 'forge/ui/server.json'
            path.write_text(json.dumps(dict(json.loads(path.read_text()), build=['old'])))

        looped = self._copy('solo-full', 'repo-is-a-symlink-loop')
        os.symlink(self.tmp / 'loop-b', self.tmp / 'loop-a')
        os.symlink(self.tmp / 'loop-a', self.tmp / 'loop-b')
        (looped / 'repo').write_text(f'{self.tmp / "loop-a"}\n')

        starts = 'a new server'
        cases = (
            # label, setup, needs a non-root user, command, exit code
            ('read-only state home', home_read_only, True, ['open', str(self.plan)], 4),
            ('read-only state home', home_read_only, True, ['status'], 1),
            ('read-only state home', home_read_only, True, ['stop'], 0),
            ('unreadable server.json', record_unreadable, True, ['open', str(self.plan)], starts),
            ('unreadable server.json', record_unreadable, True, ['status'], 1),
            ('server.json not JSON', record(b'{"pid": '), False, ['open', str(self.plan)], starts),
            ('server.json not JSON', record(b'{"pid": '), False, ['status'], 1),
            ('server.json not UTF-8', record(b'{"pid": "\xff"}'), False, ['status'], 1),
            ('server.json is a FIFO', record_is_a_fifo, False, ['open', str(self.plan)], starts),
            ('server.json is a FIFO', record_is_a_fifo, False, ['status'], 1),
            ('lock is a directory', lock_is_a_dir, False, ['open', str(self.plan)], 4),
            ('lock is a directory', lock_is_a_dir, False, ['stop'], 0),
            ('recorded build is a list', live_server_recorded_with_a_list_build, False, ['open'], starts),
            ('repo marker is a symlink loop', None, False, ['open', str(looped)], starts),
        )
        for number, (label, setup, needs_user, args, expected) in enumerate(cases):
            with self.subTest(label, command=args[0]):
                if needs_user and os.geteuid() == 0:
                    self.skipTest('root ignores file modes')
                home = self.tmp / f'hostile-{number}'
                env = dict(self.env, XDG_STATE_HOME=str(home))
                self.addCleanup(self._stop_server, home)
                if setup:
                    setup(home, env)
                before = self._recorded_pid(home)
                try:
                    result = self.ui(*args, env=env, timeout=15)
                except subprocess.TimeoutExpired:
                    self.fail(f'{args[0]} blocked')
                self.assertNotIn('Traceback', result.stderr)
                if expected == starts:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    recorded = json.loads((home / 'forge/ui/server.json').read_text())
                    self.assertNotEqual(recorded['pid'], before)
                    self.assertEqual(get_json(recorded['port'], '/api/health')['pid'], recorded['pid'])
                else:
                    self.assertEqual(result.returncode, expected, result.stderr)
                    if expected == 4:
                        self.assertIn(str(home), result.stderr, 'the message must name the path')


if __name__ == '__main__':
    unittest.main()
