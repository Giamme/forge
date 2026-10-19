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

    def _stop_server(self):
        try:
            pid = json.loads((self.state / 'forge/ui/server.json').read_text())['pid']
        except (OSError, ValueError, KeyError):
            return
        self.ui('stop')
        if alive(pid):  # stop did not take it down: do not leave it for the idle timeout
            os.kill(pid, signal.SIGKILL)

    def ui(self, *args, env=None):
        return subprocess.run([sys.executable, str(LAUNCHER), *args], env=env or self.env,
                              capture_output=True, text=True, timeout=60)

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

    def test_exit_codes_for_arguments_that_are_not_runs(self):
        notes = self.tmp / 'notes'
        notes.mkdir()
        (notes / 'prompt.md').write_text('just notes\n')  # prompt.md without repo
        no_repo = self._copy('solo-full', 'repo-is-a-dir')
        (no_repo / 'repo').unlink()
        (no_repo / 'repo').mkdir()  # DIR/repo exists but cannot be read as a file
        for label, args, code in (('missing dir', ['open', str(self.tmp / 'nope')], 3),
                                  ('prompt without repo', ['open', str(notes)], 3),
                                  ('unreadable repo file', ['open', str(no_repo)], 3),
                                  ('unknown command', ['frobnicate'], 2),
                                  ('bad port', ['open', '--port', 'x'], 2)):
            with self.subTest(label):
                self.assertEqual(self.ui(*args).returncode, code)
        self.assertEqual(self.ui('status').returncode, 1, 'a rejected DIR must not start a server')


if __name__ == '__main__':
    unittest.main()
