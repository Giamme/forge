"""`forge ui open|status|stop`: one shared, detached forge-ui server for every run."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import fcntl
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import test_forge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
sys.path.insert(0, str(ROOT / 'scripts'))  # forge_fractal, for the Fractal replay test

from forge_ui import build_id  # noqa: E402
from forge_ui.reader import run_key  # noqa: E402
import forge_registry  # noqa: E402

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
    except (ProcessLookupError, OverflowError):  # OverflowError: a pid no process can have
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

    def ui(self, *args, env=None, timeout=60, cwd=None):
        return subprocess.run([sys.executable, str(LAUNCHER), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=timeout, cwd=cwd)

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

        launched = self.ui('open', str(self.plan), env=env)
        first = self.server_json()
        repeated = self.ui('open', str(self.plan), env=env)
        self.open(self.solo, env=env)
        second = self.server_json()
        quiet = self.ui('open', str(self.plan), '--no-browser', env=env)

        # a repeat that opens no browser says why on stderr; a launch and --no-browser stay silent
        self.assertEqual((launched.stderr, quiet.stderr), ('', ''))
        self.assertIn('forge-ui:', repeated.stderr)
        self.assertEqual(repeated.stdout, launched.stdout, 'stdout stays the one link line runners rely on')

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

    def test_a_server_of_the_other_mode_is_never_reused(self):
        readonly = dict(self.env, FORGE_UI_READONLY='1')
        modes = (('write-enabled', self.env, True, False), ('read-only', readonly, False, True),
                 ('write-enabled again', self.env, True, False))
        previous = None
        for label, env, controls, recorded in modes:
            with self.subTest(label):
                self.open(self.plan, env=env)
                current = self.server_json()
                self.assertEqual(get_json(current['port'], '/api/health')['controls'], controls)
                self.assertEqual(current['read_only'], recorded)
                status = json.loads(self.ui('status', '--json', env=env).stdout)
                self.assertEqual(status['read_only'], recorded)
                self.assertIn('read-only' if recorded else 'read-write', self.ui('status', env=env).stdout)
                if previous is not None:
                    self.assertNotEqual(current['pid'], previous['pid'])
                    self.assertFalse(alive(previous['pid']))
                # the same mode asked again is a reuse
                self.open(self.plan, env=env)
                self.assertEqual(self.server_json()['pid'], current['pid'])
                previous = current

    def test_a_record_with_no_mode_reads_as_write_enabled(self):
        # Records written before the mode existed have no read_only key.
        self.open(self.plan)
        old = self.server_json()
        record = dict(old)
        record.pop('read_only')
        (self.state / 'forge/ui/server.json').write_text(json.dumps(record))
        self.open(self.plan)
        self.assertEqual(self.server_json()['pid'], old['pid'])
        self.open(self.plan, env=dict(self.env, FORGE_UI_READONLY='1'))
        self.assertNotEqual(self.server_json()['pid'], old['pid'])

    def _custom_registry(self, name='custom.tsv'):
        path = self.tmp / name
        path.write_text('only-here\tcodex\tgpt-only-here\thigh\t600\n')
        return path

    def test_the_clis_of_writes_get_the_shells_forge_configuration_and_the_server_does_not(self):
        repo = self.tmp / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (self.plan / 'repo').write_text(f'{repo}\n')
        self._custom_registry()
        data = self.tmp / 'data'
        env = dict(self.env, FORGE_REGISTRY='custom.tsv', FORGE_DATA_HOME=str(data), FORGE_DATASET='off',
                   FORGE_UI_STRAY='1')
        result = self.ui('open', str(self.plan), env=env, cwd=self.tmp)  # the registry is relative to the shell
        self.assertEqual(result.returncode, 0, result.stderr)
        server = self.server_json()
        self.assertEqual(stat.S_IMODE((self.state / 'forge/ui/cli-env.json').stat().st_mode), 0o600)

        # GET /api/config runs the real `forge config show` through control.run_cli
        shown = get_json(server['port'], f'/api/config?run={run_key(self.plan)}')
        registry = os.path.realpath(self.tmp / 'custom.tsv')
        self.assertEqual((os.path.realpath(shown['registry']['path']), shown['registry']['overridden']), (registry, True))
        self.assertEqual([row['alias'] for row in shown['registry']['rows']], ['only-here'])
        self.assertEqual(shown['dataset'], {'path': str(data / 'dataset'), 'enabled': False})
        # The CLI's environment is the server's plus the configuration: no other FORGE_* reached either.
        self.assertEqual(sorted(shown['env']), ['FORGE_DATASET', 'FORGE_DATA_HOME', 'FORGE_REGISTRY'])
        environ = Path(f'/proc/{server["pid"]}/environ')
        if environ.exists():  # Linux: the server's own environment has none of them
            names = [entry.split(b'=', 1)[0] for entry in environ.read_bytes().split(b'\0')]
            self.assertEqual([name for name in names if name.startswith(b'FORGE_')], [])

    def test_a_server_with_another_forge_configuration_is_never_reused(self):
        first = dict(self.env, FORGE_REGISTRY=str(self._custom_registry()))
        second = dict(self.env, FORGE_REGISTRY=str(self._custom_registry('other.tsv')))
        unset = dict(self.env)
        unset.pop('FORGE_REGISTRY', None)
        self.open(self.plan, env=first)
        old = self.server_json()
        for label, env in (('another registry', second), ('no registry', unset)):
            with self.subTest(label):
                result = self.ui('open', str(self.plan), env=env)
                self.assertEqual((result.returncode, result.stdout), (5, ''), result.stderr)
                self.assertIn('FORGE_REGISTRY', result.stderr)
                self.assertIn('forge ui stop', result.stderr)
                self.assertTrue(alive(old['pid']))
                self.assertEqual(self.server_json(), old)
        self.open(self.plan, env=first)  # the same configuration is a reuse
        self.assertEqual(self.server_json()['pid'], old['pid'])

        self.assertEqual(self.ui('stop', env=second).returncode, 0)
        self.open(self.plan, env=second)
        self.assertEqual(self.server_json()['cli_env']['FORGE_REGISTRY'], second['FORGE_REGISTRY'])

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

    def test_an_empty_config_home_registers_the_run_in_the_default_one_not_the_current_directory(self):
        # XDG_CONFIG_HOME= taken literally put the registry at ./forge/runs: an untracked
        # forge/ dir in whatever repo `open` ran from, and a server reading a relative path.
        home, work = self.tmp / 'home', self.tmp / 'work'
        work.mkdir()
        repo = (self.plan / 'repo').read_text().strip()
        default = home / '.config/forge/runs'
        for value in ('', 'relative/config'):
            with self.subTest(repr(value)):
                env = dict(self.env, XDG_CONFIG_HOME=value, HOME=str(home))
                result = self.ui('open', str(self.plan), env=env, cwd=work)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sorted(work.rglob('*')), [], 'nothing is written below the current directory')
                self.ui('stop', env=env)
        entry = default / (forge_registry.repo_key(repo) + '.txt')
        self.assertIn(str(self.plan.resolve()), entry.read_text().splitlines())

    def test_open_registers_the_run_under_the_first_line_of_its_repo_file_like_the_runners(self):
        # forge_registry (what the runners call) reads only the first line; `open` read the whole
        # file, so a repo file with a second line put the run under a repo that does not exist.
        repo = (self.plan / 'repo').read_text().strip()
        (self.plan / 'repo').write_text(f'  {repo}  \nwritten by something else\n')
        self.open(self.plan)
        registry = self.tmp / 'config/forge/runs'
        self.assertEqual([p.name for p in registry.iterdir()], [forge_registry.repo_key(repo) + '.txt'])

    def test_exit_codes_for_arguments_that_are_not_runs(self):
        notes = self.tmp / 'notes'
        notes.mkdir()
        (notes / 'prompt.md').write_text('just notes\n')  # prompt.md without repo
        no_repo = self._copy('solo-full', 'repo-is-a-dir')
        (no_repo / 'repo').unlink()
        (no_repo / 'repo').mkdir()  # DIR/repo exists but cannot be read as a file
        garbled = self._copy('solo-full', 'repo-is-not-text')
        (garbled / 'repo').write_bytes(b'/tmp/\xff\xfe\n')
        nul = self._copy('plan-full', 'repo-has-a-nul')
        (nul / 'repo').write_bytes(b'/tmp/example\x00repo\n')  # text, but no path can hold it
        for label, args, code in (('missing dir', ['open', str(self.tmp / 'nope')], 3),
                                  ('prompt without repo', ['open', str(notes)], 3),
                                  ('unreadable repo file', ['open', str(no_repo)], 3),
                                  ('undecodable repo file', ['open', str(garbled)], 3),
                                  ('repo path with a NUL byte', ['open', str(nul)], 3),
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

        huge_pid = json.dumps(dict(pid=2 ** 80, port=1, build='b', registry_dir='r')).encode()
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
            ('pid too large for os.kill', record(huge_pid), False, ['open', str(self.plan)], starts),
            ('pid too large for os.kill', record(huge_pid), False, ['status'], 1),
            ('pid too large for os.kill', record(huge_pid), False, ['stop'], 0),
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


class UiRunnerTests(unittest.TestCase):
    """`--ui` on solo runs and on `plan`, driven through test_forge's fake CLIs and restricted PATH.

    test_forge.ForgeTests is borrowed method by method: subclassing it would re-run all of its tests here.
    """
    setUp_forge = test_forge.ForgeTests.setUp
    git = test_forge.ForgeTests.git
    scenario = test_forge.ForgeTests.scenario
    calls = test_forge.ForgeTests.calls
    run_script = test_forge.ForgeTests.run_script
    plan = test_forge.ForgeTests.plan
    ui = UiLauncherTests.ui
    _recorded_pid = UiLauncherTests._recorded_pid
    _stop_server = UiLauncherTests._stop_server

    def setUp(self):
        self.setUp_forge()
        self.state = self.root / 'state'
        self.config = self.root / 'config'  # private: every run now registers itself there
        self.env.update(XDG_STATE_HOME=str(self.state), XDG_CONFIG_HOME=str(self.config), FORGE_UI_BROWSER='off',
                        FORGE_UI_PORT='0', FORGE_UI_IDLE_S='120')
        self.env.pop('FORGE_RUN_REGISTRY', None)
        self.addCleanup(self._stop_server)
        self.run_dir = self.root / 'solo'

    def registered(self):
        """The run dirs forge's run registry holds for self.repo."""
        path = self.config / 'forge/runs' / (forge_registry.repo_key(self.repo) + '.txt')
        if not path.is_file():
            return []
        return [x for x in path.read_text().splitlines() if x and not x.startswith('#')]

    def server_json(self):
        return json.loads((self.state / 'forge/ui/server.json').read_text())

    def start_solo(self, *args):
        self.run_dir.mkdir(exist_ok=True)
        (self.run_dir / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        proc = subprocess.Popen(['bash', str(ROOT / 'scripts/forge-solo.sh'), str(self.run_dir), '--repo', str(self.repo),
                                 '--dwarf', 'sol', *args], cwd=self.repo, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(proc.kill)
        self.addCleanup(proc.stdout.close)
        return proc

    def solo(self, *args, timeout=30):
        """Output and exit code of a solo run. A server that inherited our pipe holds it past the deadline."""
        proc = self.start_solo(*args)
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail(f'forge-solo.sh did not return within {timeout}s')
        return proc.returncode, out

    def test_solo_ui_prints_the_url_before_the_dwarf_and_serves_the_finished_run(self):
        self.scenario({'*.dwarf.*': {'sleep': 4}})  # the fake writes its CALLS line first, then sleeps
        proc = self.start_solo('--ui')
        lines, calls_at_url = [], None
        for line in proc.stdout:
            lines.append(line)
            if line.startswith('forge-ui: ') and calls_at_url is None:
                calls_at_url = self.calls()
        self.assertEqual(proc.wait(timeout=60), 0, ''.join(lines))
        self.assertEqual(calls_at_url, [], ''.join(lines))  # the dwarf had not been called when the URL went out
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        port, key = self.server_json()['port'], run_key(self.run_dir)
        self.assertIn(f'forge-ui: http://127.0.0.1:{port}/#/runs/{key}\n', lines)
        run = get_json(port, f'/api/runs/{key}')
        task = run['tasks'][0]
        self.assertEqual((run['kind'], task['status']), ('solo', 'PASS'))
        self.assertEqual((task['dwarf'], task['qa']), ('sol', 'opus'))
        self.assertIn('REQUIREMENT_SENTINEL', task['prompt'])

    def test_the_server_inherits_nothing_from_the_run(self):
        rc, out = self.solo('--ui')
        self.assertEqual(rc, 0, out)
        self.assertTrue(alive(self.server_json()['pid']))
        # The lock a solo run holds on its whole pipeline: a server that inherited it would pin the run dir.
        with open(self.run_dir / 'pipeline.lock', 'a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_plan_ui_prints_the_url_under_the_table_and_run_refuses_the_flag(self):
        plan = self.plan()
        result = self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory', '--ui')
        self.assertEqual(result.returncode, 0, result.stdout)
        port = self.server_json()['port']
        url = f'forge-ui: http://127.0.0.1:{port}/#/runs/{run_key(plan)}'
        lines = result.stdout.splitlines()
        self.assertEqual(lines[-1], url, result.stdout)
        run = get_json(port, f'/api/runs/{run_key(plan)}')
        self.assertEqual(run['kind'], 'plan')
        self.assertEqual([t['id'] for t in run['tasks']], ['a', 'b'])
        self.assertEqual(run['waves'], [['a'], ['b']])
        # The choice belongs to one invocation: run has no such option.
        refused = self.run_script('forge-parallel.sh', 'run', plan, '--ui')
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_ui_that_cannot_start_never_changes_the_run(self):
        rc, out = self.solo()
        self.assertEqual(rc, 0, out)
        baseline = (rc, (self.run_dir / 'verdict').read_text())
        base_env = self.env
        for label, extra, note in (('off', {'FORGE_UI': 'off'}, 'FORGE_UI=off'),
                                   ('broken launcher', {'FORGE_UI_PORT': 'x'}, 'forge-ui did not start; the run goes on')):
            with self.subTest(label):
                self.git('clean', '-fdq')  # the dwarf's file from the last run would make this one a no-change run
                shutil.rmtree(self.run_dir)
                self.env = dict(base_env, **extra)
                rc, out = self.solo('--ui')
                self.assertEqual((rc, (self.run_dir / 'verdict').read_text()), baseline, out)
                self.assertEqual(sum(note in line for line in out.splitlines()), 1, out)
                self.assertNotIn('forge-ui: http', out)
                self.assertFalse((self.state / 'forge/ui').exists())

    def test_a_ui_that_cannot_start_does_not_fail_plan(self):
        self.env['FORGE_UI_PORT'] = 'x'
        plan = self.root / 'plan'
        self.plan()  # a plain plan first: the same plan, re-planned below with --ui
        result = self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory', '--ui')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('forge-ui did not start; the run goes on', result.stdout)

    def test_nothing_starts_unless_the_user_said_yes(self):
        rc, out = self.solo()
        self.assertEqual(rc, 0, out)
        plain_plan = self.plan()
        no = self.run_script('forge-parallel.sh', 'plan', plain_plan, '--repo', self.repo, '--no-memory', '--ui', '--no-ui')
        self.assertEqual(no.returncode, 0, no.stdout)
        dry_rc, dry_out = self.solo('--ui', '--dry-run')
        self.assertEqual(dry_rc, 0, dry_out)
        self.assertIn('--ui ignored', dry_out)
        self.assertFalse((self.state / 'forge/ui').exists())
        self.assertNotIn('forge-ui:', out + no.stdout + dry_out)

    # --- the run registry: every run, --ui or not ------------------------------------------
    # forge-ui's view of a repo's runs reads $XDG_CONFIG_HOME/forge/runs, so a run that was
    # never opened in the UI must still be there. Nothing about it may show in a run.

    def test_a_plain_solo_run_registers_its_run_dir_and_a_dry_run_does_not(self):
        rc, out = self.solo('--dry-run')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.registered(), [])
        self.assertFalse((self.config / 'forge/runs').exists(), out)
        rc, out = self.solo()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.registered(), [str(self.run_dir.resolve())])
        self.assertFalse((self.state / 'forge/ui').exists())

    def test_run_records_max_parallel_and_registers_plans_made_before_plan_did(self):
        plan = self.plan()
        self.assertEqual(self.registered(), [str(plan.resolve())])  # every plan registers, --ui or not
        registry = self.config / 'forge/runs' / (forge_registry.repo_key(self.repo) + '.txt')
        registry.unlink()  # as if an older forge had made the plan

        dry = self.run_script('forge-parallel.sh', 'run', plan, '--max-parallel', '2', '--dry-run')
        self.assertEqual(dry.returncode, 0, dry.stdout)
        self.assertFalse((plan / 'max_parallel').exists())
        self.assertEqual(self.registered(), [])

        result = self.run_script('forge-parallel.sh', 'run', plan, '--max-parallel', '2')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((plan / 'max_parallel').read_text(), '2\n')
        self.assertEqual(self.registered(), [str(plan.resolve())])
        self.assertEqual(sorted(self.calls()), ['dwarf', 'dwarf', 'qa', 'qa'])

    @unittest.skipUnless(os.environ.get('FORGE_TEST_FRACTAL_RUNTIME') or (Path.home() / '.local/state/forge/fractal/runtime').is_dir(),
                         'Fractal is not installed (or set FORGE_TEST_FRACTAL_RUNTIME)')
    def test_a_fractal_resume_does_not_replay_the_ui_choice(self):
        from forge_fractal import ARCHIVE_SHA256, REVISION, write_json
        runtime = os.environ.get('FORGE_TEST_FRACTAL_RUNTIME') or str(Path.home() / '.local/state/forge/fractal/runtime')
        managed = self.state / 'forge/fractal'
        managed.mkdir(parents=True)
        (managed / 'runtime').symlink_to(runtime, target_is_directory=True)
        write_json(managed / 'provenance.json', dict(revision=REVISION, archive_sha256=ARCHIVE_SHA256))
        fake = test_forge.FAKE.replace("else 'implemented'", "else 'implemented\\nFORGE_FRACTAL: {\"done\":true}'")
        for name in ('codex', 'claude'):
            (self.bin / name).write_text(fake)
        self.env['PATH'] += os.pathsep + os.environ['PATH']  # Fractal needs tmux
        rc, out = self.solo('--fractal', '--ui', timeout=90)
        self.assertEqual(rc, 0, out)
        recorded = [json.loads(p.read_text()) for p in managed.glob('runs/*/run.json')]
        self.assertEqual(len(recorded), 1)
        argv = recorded[0]['pipeline_argv']
        self.assertIn('--fractal', argv)  # the rest of the command is replayed as typed
        self.assertNotIn('--ui', argv)  # a replay that carried it would open the UI a second time


if __name__ == '__main__':
    unittest.main()
