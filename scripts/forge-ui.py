#!/usr/bin/env python3
"""forge ui open|status|stop: one shared, detached forge-ui server for every run.

`open` starts the server when none is running (or replaces one that runs old code) and
prints a link to the run; every later `open` reuses it. State lives in
`${XDG_STATE_HOME:-~/.local/state}/forge/ui`: `server.json` (what we started), `server.log`
and `lock` (serialises launchers). The server exits by itself after FORGE_UI_IDLE_S idle seconds.
"""
from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
# `-I` keeps the script's own directory off sys.path, so both are added by hand.
sys.path.insert(0, str(SKILL / 'scripts'))
sys.path.insert(0, str(SKILL / 'ui'))

HOST = '127.0.0.1'
LISTENING = re.compile(r'^forge-ui on http://127\.0\.0\.1:(\d+)/', re.M)
DEFAULT_PORT = 8765
DEFAULT_IDLE_S = 14400
LOCK_WAIT_S = 10
START_WAIT_S = 5
STOP_WAIT_S = 2
BROWSER_WAIT_S = 2  # a browser command that hangs must not hold `open` past its budget


def state_dir() -> Path:
    base = os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local/state')
    return Path(base).expanduser() / 'forge/ui'


def _say(text):
    print(f'forge-ui: {text}', file=sys.stderr)


def _env_int(name, default):
    raw = os.environ.get(name, '')
    if not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError(f'{name}={raw!r} is not a non-negative integer')
    return value


def pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid, seconds) -> bool:
    deadline = time.monotonic() + seconds
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def health(port):
    """The /api/health answer of whatever listens on 127.0.0.1:port, or None."""
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return None
    conn = http.client.HTTPConnection(HOST, port, timeout=1)  # direct: no proxy handling
    try:
        conn.request('GET', '/api/health')
        response = conn.getresponse()
        if response.status != 200:
            return None
        data = json.loads(response.read(65536))
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()
    return data if isinstance(data, dict) else None


def read_state(state):
    try:
        data = json.loads((state / 'server.json').read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(state, data):
    handle, temp = tempfile.mkstemp(prefix='server.json.', dir=str(state))
    try:
        with os.fdopen(handle, 'w') as out:
            json.dump(data, out, indent=1, sort_keys=True)
            out.write('\n')
        os.replace(temp, state / 'server.json')
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def confirmed(recorded):
    """The health answer if the recorded server is alive and is a forge-ui with that pid."""
    if not recorded or not pid_alive(recorded.get('pid')):
        return None
    answer = health(recorded.get('port'))
    if answer and answer.get('app') == 'forge-ui' and answer.get('pid') == recorded['pid']:
        return answer
    return None


class Lock:
    """An flock on <state>/lock, so concurrent launchers see one server."""

    def __init__(self, state):
        self.path = state / 'lock'
        self.handle = None

    def __enter__(self):
        self.handle = open(self.path, 'a')
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    raise TimeoutError(f'another forge ui is holding {self.path}')
                time.sleep(0.05)

    def __exit__(self, *exc):
        self.handle.close()  # closing releases the lock


def terminate(pid) -> bool:
    """SIGTERM a pid that health has confirmed; True once it is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return wait_dead(pid, STOP_WAIT_S)


def _spawn(state, port, registry, idle):
    """Start the server; returns (process, port) or (None, error text)."""
    log = state / 'server.log'
    env = {k: v for k, v in os.environ.items() if not k.startswith('FORGE_')}
    command = [sys.executable, '-I', str(Path(__file__).resolve()), '_serve', '--port', str(port),
               '--quiet', '--registry-dir', registry, '--idle-exit', str(idle)]
    with open(log, 'w') as out:
        proc = subprocess.Popen(command, cwd=str(state), stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                start_new_session=True, close_fds=True, env=env)
    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline:
        try:
            match = LISTENING.search(log.read_text(errors='replace'))
        except OSError:
            match = None
        if match:
            return proc, int(match.group(1))
        if proc.poll() is not None:
            return None, proc.returncode
        time.sleep(0.05)
    proc.terminate()
    return None, None


def start_server(state, port, registry, idle):
    """Spawn a server, falling back once to a free port when the preferred one is taken."""
    proc, result = _spawn(state, port, registry, idle)
    if proc is None and result == 2 and port != 0:
        proc, result = _spawn(state, 0, registry, idle)
    if proc is None:
        return None
    return {'pid': proc.pid, 'port': result}


def gui_available() -> bool:
    if sys.platform == 'darwin':
        return True
    return any(os.environ.get(name) for name in ('DISPLAY', 'WAYLAND_DISPLAY', 'BROWSER'))


def browser_wanted(args) -> bool:
    if args.no_browser or os.environ.get('FORGE_UI_BROWSER', '').strip().lower() in ('off', '0', 'false', 'no'):
        return False
    return gui_available()


def launch_browser(url):
    """Open url, waiting at most BROWSER_WAIT_S for a browser command that blocks."""
    def go():
        try:
            webbrowser.open(url)
        except Exception:  # a missing browser must not fail the run
            pass
    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    worker.join(BROWSER_WAIT_S)


def run_info(directory):
    """(repo, None) for a run dir, or (None, why not)."""
    plan = (directory / 'tasks.tsv').is_file()
    solo = (directory / 'prompt.md').is_file() and (directory / 'repo').is_file()
    if not (plan or solo):
        return None, 'not a forge run (no tasks.tsv, and no prompt.md with repo)'
    try:
        repo = (directory / 'repo').read_text().strip()
    except OSError as exc:
        return None, f'cannot read {directory / "repo"}: {exc.strerror or exc}'
    if not repo:
        return None, f'{directory / "repo"} is empty'
    return repo, None


def cmd_open(args):
    import forge_registry
    from forge_ui import __version__, build_id
    from forge_ui.reader import run_key

    try:
        port = args.port if args.port is not None else _env_int('FORGE_UI_PORT', DEFAULT_PORT)
        idle = _env_int('FORGE_UI_IDLE_S', DEFAULT_IDLE_S)
    except ValueError as exc:
        _say(f'error: {exc}')
        return 2
    if not 0 <= port <= 65535:
        _say(f'error: port {port} is out of range (0-65535)')
        return 2

    key = None
    if args.dir:
        directory = Path(os.path.realpath(args.dir))
        repo, why = run_info(directory)
        if why:
            _say(f'error: {directory}: {why}')
            return 3
        forge_registry.register_run_dir(Path(repo), directory)
        key = run_key(directory)

    state = state_dir()
    state.mkdir(parents=True, exist_ok=True)
    registry = str(forge_registry.registry_dir())
    build = build_id()
    log = state / 'server.log'
    try:
        with Lock(state):
            server = read_state(state)
            answer = confirmed(server) if server else None
            if answer:
                # the record and the live answer must both name this code
                if {server.get('build'), answer.get('build')} == {build} and server.get('registry') == registry:
                    pass  # reuse
                else:
                    terminate(server['pid'])
                    server = None
            else:
                server = None  # dead, or a pid that is not our forge-ui: never signal it
            if server is None:
                started = start_server(state, port, registry, idle)
                if started is None:
                    _say(f'error: the server did not start; see {log}')
                    return 4
                server = dict(started, version=__version__, build=build, registry=registry, log=str(log),
                              started=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), opened=[])
            opened = server.get('opened')
            opened = opened if isinstance(opened, list) else []
            mark = key or ''
            # a key counts as opened only when a browser is really launched for it
            first = mark not in opened and browser_wanted(args)
            if first:
                opened.append(mark)
            server['opened'] = opened
            write_state(state, server)
    except TimeoutError as exc:
        _say(f'error: {exc}')
        return 4

    url = f'http://{HOST}:{server["port"]}/'
    if key:
        url += f'#/runs/{key}'
    print(f'forge-ui: {url}')
    if first:
        launch_browser(url)
    return 0


def cmd_status(args):
    server = read_state(state_dir())
    answer = confirmed(server)
    if not answer:
        if args.json:
            print(json.dumps({'running': False}))
        else:
            print('forge-ui: not running')
        return 1
    info = {'running': True, 'pid': server['pid'], 'port': server['port'],
            'url': f'http://{HOST}:{server["port"]}/', 'version': answer.get('version'),
            'build': answer.get('build'), 'started': server.get('started'), 'log': server.get('log')}
    if args.json:
        print(json.dumps(info, sort_keys=True))
    else:
        print(f'forge-ui: running at {info["url"]} (pid {info["pid"]}, started {info["started"]})')
    return 0


def cmd_stop(args):
    state = state_dir()
    if not state.is_dir():
        print('forge-ui: not running')
        return 0
    try:
        with Lock(state):
            server = read_state(state)
            if server and confirmed(server):
                if terminate(server['pid']):
                    print('forge-ui: stopped')
                else:
                    _say(f'warning: pid {server["pid"]} did not exit within {STOP_WAIT_S}s')
                    return 0
            else:
                print('forge-ui: not running')
                if server and pid_alive(server.get('pid')):
                    return 0  # a live pid health could not vouch for: leave it and its record alone
            try:
                (state / 'server.json').unlink()
            except OSError:
                pass
    except TimeoutError as exc:
        _say(f'warning: {exc}')
    return 0


def serve(argv):
    from forge_ui.__main__ import main
    return main(argv)


def build_parser():
    parser = argparse.ArgumentParser(prog='forge ui', description='Open runs in the shared forge-ui server.')
    sub = parser.add_subparsers(dest='command', required=True)
    opened = sub.add_parser('open', help='start or reuse the server and open a run')
    opened.add_argument('dir', nargs='?', metavar='DIR', help='a run dir (plan or solo); omit to open the run list')
    opened.add_argument('--no-browser', action='store_true', help='print the link without opening it')
    opened.add_argument('--port', type=int, metavar='N', help='preferred port (default FORGE_UI_PORT or 8765)')
    status = sub.add_parser('status', help='exit 0 when the server is running, 1 when not')
    status.add_argument('--json', action='store_true')
    sub.add_parser('stop', help='stop the server')
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == '_serve':
        return serve(argv[1:])
    args = build_parser().parse_args(argv)  # a usage error exits 2
    return {'open': cmd_open, 'status': cmd_status, 'stop': cmd_stop}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
