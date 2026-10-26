#!/usr/bin/env python3
"""Verification lanes: run a project's verification as parallel, named lanes.

``--lanes-dir`` holds one file per lane. A lane is a shell command (run as
``/bin/bash -c <text>`` in the checkout, exactly like the single verify command) with
optional leading ``#forge: key[=value] ...`` lines:

* ``group=<name>``  lanes of one group never run at the same time
* ``first``         the lane runs before all others; if one fails nothing else starts
* ``retries=<n>``, ``reports=<dir>``, ``baseline=<file>`` are validated for form only and
  kept in the lane's ``options`` for the tasks that rerun lanes and read test reports.

    forge-verify.py lanes --lanes-dir DIR [--json]
    forge-verify.py run --checkout DIR --out DIR --lanes-dir DIR [--parallel N]
                        [--fingerprint-cmd CMD]

``run`` writes, into ``--out``: one log per lane (``verification.lane.<name>.log``),
``verification.lanes.json`` (rewritten atomically on every state change and at most once
a second while lanes print) and ``verification.runs.tsv``
(``n kind exit tree_changed duration_s log lane started``, one row per lane run).

Exit codes: 0 every lane passed and nothing mutated, 1 otherwise, 130 interrupted,
2 configuration error (nothing ran).

Structure, for the tasks that extend this: a lane's result is plain data in
``Verification.doc["lanes"]`` (its ``runs`` list holds one entry per execution);
``Verification.run_round`` runs one round of lane runs over the names it is given and
``Verification.run`` is only the sequence of rounds around the mutation check.
"""
from __future__ import annotations

import argparse
import codecs
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

LANE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
HEADER = '#forge:'
KILL_AFTER = 5.0          # seconds between TERM and KILL on an interrupt
WRITE_EVERY = 1.0         # lanes.json refresh while lanes only print
TICK = 0.05
LAST_LINE_MAX = 300
PUMP_MAX = 4 << 20        # bytes read from one lane's log per look, so a flood cannot starve the loop
INTERRUPTED = 130

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]')
STEP = re.compile(r'::forge step name=(\S+) state=(start|end)(?: exit=(-?\d+))?')
OPTION_FORMS = {
    'group': LANE_NAME,
    'retries': re.compile(r'[0-9]'),
    'reports': re.compile(r'\S+'),
    'baseline': re.compile(r'\S+'),
}


# --- declarations ----------------------------------------------------------------------

def parse_lane(name: str, text: str) -> tuple[dict | None, list[str]]:
    """One lane file's text -> (lane, errors). The lane is None when there are errors."""
    errors: list[str] = []
    lines = text.split('\n')
    options: dict = {}
    group = None
    first = False
    seen: set[str] = set()
    body_from = 0
    for body_from, line in enumerate(lines):
        if not line.startswith(HEADER):
            break
        tokens = line[len(HEADER):].split()
        if not tokens:
            errors.append(f'{name}: empty "{HEADER}" line')
        for token in tokens:
            key, eq, value = token.partition('=')
            if key in seen:
                errors.append(f'{name}: option "{key}" given twice')
                continue
            seen.add(key)
            if key == 'first':
                if eq:
                    errors.append(f'{name}: option "first" takes no value')
                first = True
            elif key in OPTION_FORMS:
                if not eq or not OPTION_FORMS[key].fullmatch(value):
                    errors.append(f'{name}: option "{token}" is malformed')
                elif key == 'group':
                    group = value
                elif key == 'retries':
                    options['retries'] = int(value)
                else:
                    options[key] = value
            else:
                errors.append(f'{name}: unknown option "{key}"')
    else:
        body_from = len(lines)
    command = '\n'.join(lines[body_from:]).strip()
    if '\0' in text:
        errors.append(f'{name}: contains a NUL byte')
    if not command:
        errors.append(f'{name}: empty command')
    if errors:
        return None, errors
    return {'name': name, 'command': command, 'group': group, 'first': first,
            'options': options}, []


def load_lanes(lanes_dir: str) -> tuple[list[dict], list[str]]:
    """Every lane in the directory, in declaration order (sorted names), and the errors."""
    root = Path(lanes_dir)
    if not root.is_dir():
        return [], [f'{lanes_dir}: not a directory']
    lanes: list[dict] = []
    errors: list[str] = []
    for entry in sorted(os.listdir(root)):
        if entry.startswith('.') or entry.endswith('~'):
            continue
        path = root / entry
        if not LANE_NAME.fullmatch(entry):
            errors.append(f'{entry}: not a valid lane name (letters, digits, "_" and "-"; at most 64)')
            continue
        if path.is_symlink() or not path.is_file():
            errors.append(f'{entry}: not a regular file')
            continue
        try:
            text = path.read_bytes().decode('utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f'{entry}: cannot read: {exc}')
            continue
        lane, problems = parse_lane(entry, text)
        errors.extend(problems)
        if lane:
            lanes.append(lane)
    if not lanes and not errors:
        errors.append(f'{lanes_dir}: no lanes declared')
    return lanes, errors


# --- mutation check --------------------------------------------------------------------

def _text(data: bytes) -> str:
    return data.decode('utf-8', 'replace').strip()


def _git(checkout: str, *args: str, env: dict | None = None) -> str:
    done = subprocess.run(['git', '-C', checkout, *args], capture_output=True, env=env)
    if done.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)}: {_text(done.stderr)}')
    return _text(done.stdout)


def default_fingerprint(checkout: str) -> str:
    """HEAD plus the tree id of the whole working tree (tracked and untracked, ignored files
    left out), by content: the status labels do not change when an already modified file
    is edited again. It is built in a temporary copy of the index, so the real one is
    never touched. ``.forge`` is left out, as in forge_tree."""
    try:
        head = _git(checkout, 'rev-parse', 'HEAD')
    except RuntimeError:
        head = '-'
    real = Path(checkout) / _git(checkout, 'rev-parse', '--git-path', 'index')
    fd, tmp = tempfile.mkstemp(prefix='forge-index.')
    os.close(fd)
    try:
        if real.is_file():
            shutil.copy2(real, tmp)  # keeps the mtime git uses to spot racy entries
        else:
            os.unlink(tmp)
        env = dict(os.environ, GIT_INDEX_FILE=tmp)
        _git(checkout, 'add', '-A', '--', '.', ':(exclude).forge', env=env)
        tree = _git(checkout, 'write-tree', env=env)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return f'{head} {tree}'


def fingerprint(checkout: str, command: str | None) -> str:
    if not command:
        return default_fingerprint(checkout)
    done = subprocess.run(['/bin/bash', '-c', command], cwd=checkout, capture_output=True,
                          stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise RuntimeError(f'fingerprint command exited {done.returncode}: {_text(done.stderr)}')
    return _text(done.stdout)


# --- running ---------------------------------------------------------------------------

def _file_mode() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def write_atomic(path: Path, text: str) -> None:
    """Write through a temp file and rename. mkstemp makes the file 0600; the lane logs
    follow the umask, so the state files get the same mode (a forge-ui server may read them
    as another user)."""
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        os.fchmod(fd, _file_mode())
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def lane_log(name: str) -> str:
    return f'verification.lane.{name}.log'


class Live:
    """A started lane process and what is read from its log so far."""

    def __init__(self, proc: subprocess.Popen, log: Path, offset: int):
        self.proc = proc
        self.reader = open(log, 'rb')
        self.reader.seek(offset)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.partial = ''

    def close(self) -> None:
        self.reader.close()


class Verification:
    def __init__(self, checkout: str, out: str, lanes: list[dict], parallel: int,
                 fingerprint_cmd: str | None = None):
        self.checkout = checkout
        self.out = Path(out)
        self.parallel = parallel
        self.fingerprint_cmd = fingerprint_cmd
        self.stop = False
        self.running: dict[str, Live] = {}
        self.lingering: list[tuple[int, float]] = []  # (pgid, KILL time) of finished lanes
        self.dirty = False
        self.written = 0.0
        self.doc: dict = {
            'version': 1, 'state': 'running', 'started': time.time(), 'ended': None,
            'parallel': parallel, 'mutated': False,
            'lanes': [{'name': lane['name'], 'command': lane['command'], 'group': lane['group'],
                       'first': lane['first'], 'options': lane['options'], 'state': 'waiting',
                       'exit': None, 'started': None, 'ended': None, 'runs': [], 'step': None,
                       'last_line': None, 'last_output_at': None, 'log': lane_log(lane['name']),
                       'tests': None} for lane in lanes],
        }
        self.by_name = {lane['name']: lane for lane in self.doc['lanes']}

    # state files ----------------------------------------------------------------------

    def save(self, force: bool = True) -> None:
        """Rewrite lanes.json. State changes force it; output only marks it due."""
        now = time.time()
        if not force:
            self.dirty = True
            if now - self.written < WRITE_EVERY:
                return
        self.dirty = False
        self.written = now
        write_atomic(self.out / 'verification.lanes.json', json.dumps(self.doc, indent=1) + '\n')

    def flush(self) -> None:
        if self.dirty:
            self.save(force=False)

    def write_runs(self) -> None:
        """verification.runs.tsv from the lanes' run entries: n kind exit tree_changed
        duration_s log lane started. n counts runs in the order they started. Lanes run
        in parallel, so a change to the tree is not pinned to a run; the one exception is
        a verification that made a single run."""
        runs = [(r, lane['name']) for lane in self.doc['lanes'] for r in lane['runs']
                if r['exit'] is not None]
        runs.sort(key=lambda item: (item[0]['started'], item[1]))
        single = len([r for lane in self.doc['lanes'] for r in lane['runs']]) == 1
        rows = []
        for n, (run, name) in enumerate(runs, 1):
            changed = 1 if self.doc['mutated'] and single else 0
            rows.append('\t'.join([str(n), run['kind'], str(run['exit']), str(changed),
                                   str(int(run['ended'] - run['started'])), run['log'], name,
                                   f'{run["started"]:.3f}']))
        write_atomic(self.out / 'verification.runs.tsv', '\n'.join(rows) + ('\n' if rows else ''))

    # one round -----------------------------------------------------------------------

    def run_round(self, names: list[str], kind: str = 'initial') -> None:
        """Run the named lanes: up to ``parallel`` at once, never two of one group, started
        in the order given. Returns when all have finished, or when stopped (the running
        ones are then terminated and finished as interrupted, the others stay waiting)."""
        pending = list(names)
        running = self.running
        while (pending or running) and not self.stop:
            self._pump(running)
            for name in [n for n, live in running.items() if live.proc.poll() is not None]:
                self._finish(name, running.pop(name))
            self._reap_lingering()
            busy = {self.by_name[n]['group'] for n in running} - {None}
            for name in list(pending):
                if len(running) >= self.parallel:
                    break
                group = self.by_name[name]['group']
                if group in busy:
                    continue
                pending.remove(name)
                if self._start(name, kind) and group:
                    busy.add(group)
            self.flush()
            if pending or running:
                time.sleep(TICK)
        if self.stop and running:
            self._terminate()

    def _start(self, name: str, kind: str) -> Live | None:
        """Start one lane run; the process is in ``self.running`` from the moment it exists,
        so no later failure can leave it unaccounted for."""
        lane = self.by_name[name]
        now = time.time()
        log = self.out / lane['log']
        run = {'k': len(lane['runs']) + 1, 'kind': kind, 'started': now, 'ended': None,
               'exit': None, 'log': lane['log']}
        lane['runs'].append(run)
        lane.update(state='running', started=now, ended=None, exit=None, step=None)
        try:
            with open(log, 'ab') as fh:
                offset = fh.tell()
                proc = subprocess.Popen(['/bin/bash', '-c', lane['command']], cwd=self.checkout,
                                        stdin=subprocess.DEVNULL, stdout=fh,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        except (OSError, ValueError) as exc:  # ValueError: an embedded NUL in the command
            with open(log, 'ab') as fh:
                fh.write(f'forge-verify: cannot start lane: {exc}\n'.encode())
            self._end(lane, run, 126)
            return None
        live = self.running[name] = Live(proc, log, offset)
        self.save()
        return live

    def _finish(self, name: str, live: Live, interrupted: bool = False) -> None:
        """The lane's leader has exited. Whatever it left running in its process group (a
        background server, a watcher) is terminated: no lane process outlives the script."""
        lane = self.by_name[name]
        rc = live.proc.poll()
        if rc is None:  # still there after the KILL: a process stuck in the kernel
            rc = -signal.SIGKILL
        pgid = live.proc.pid
        # a finished lane's backlog is read in capped steps, so an interrupt still gets through
        while not self._pump({name: live}) and not (interrupted or self.stop):
            self.flush()
        live.close()
        if self._group_alive(pgid):
            self._signal(pgid, signal.SIGTERM)
            self.lingering.append((pgid, time.monotonic() + KILL_AFTER))
        self._end(lane, lane['runs'][-1], rc if rc >= 0 else 128 - rc, interrupted)

    def _end(self, lane: dict, run: dict, code: int, interrupted: bool = False) -> None:
        now = time.time()
        run.update(ended=now, exit=code)
        lane.update(state='interrupted' if interrupted else 'pass' if code == 0 else 'fail',
                    exit=code, ended=now, step=None)
        self.save()
        self.write_runs()

    def _pump(self, running: dict[str, Live], limit: int = PUMP_MAX) -> bool:
        """Read what the lanes printed since the last look: last_line and the step markers.
        At most ``limit`` bytes per lane, so the caller gets back to its stop check, the
        reaping and the refresh however fast a lane prints. True when nothing is left unread."""
        drained = True
        for name, live in running.items():
            lane = self.by_name[name]
            exited = live.proc.poll() is not None  # asked first: all of its output is then readable
            left = limit
            eof = False
            while left > 0:
                chunk = live.reader.read(min(left, 1 << 20))
                if not chunk:
                    eof = True
                    break
                left -= len(chunk)
                lane['last_output_at'] = time.time()
                self.dirty = True
                parts = re.split(r'[\r\n]', live.partial + live.decoder.decode(chunk))
                live.partial = parts.pop()[-65536:]
                for line in parts:
                    self._line(lane, line)
            if not eof:
                drained = False
            elif exited:
                tail = live.partial + live.decoder.decode(b'', final=True)
                live.partial = ''
                if tail:
                    self._line(lane, tail)
        return drained

    def _line(self, lane: dict, raw: str) -> None:
        line = ANSI.sub('', raw).strip()
        if not line:
            return
        lane['last_line'] = line[:LAST_LINE_MAX]
        marker = STEP.fullmatch(line)
        if marker:
            step, state = marker.group(1), marker.group(2)
            if state == 'start':
                lane['step'] = {'name': step, 'started': time.time()}
            elif lane['step'] and lane['step']['name'] == step:
                lane['step'] = None

    # process groups ---------------------------------------------------------------------

    def _terminate(self) -> None:
        """Interrupt: stop every lane, then finish the running ones as interrupted."""
        running = dict(self.running)
        self._kill_all()
        for name, live in running.items():
            self._finish(name, live, interrupted=True)
        self.running.clear()

    def _kill_all(self) -> None:
        """TERM every lane process group (running lanes and what finished lanes left), KILL
        what is left after KILL_AFTER seconds, and wait until no process of any group is
        left. Safe to call again; used on every way out of ``run``."""
        groups = {live.proc.pid for live in self.running.values()} | {g for g, _ in self.lingering}
        for pgid in groups:
            self._signal(pgid, signal.SIGTERM)
        deadline = time.monotonic() + KILL_AFTER
        while time.monotonic() < deadline and any(self._group_alive(g) for g in groups):
            time.sleep(TICK)
        for pgid in groups:
            if self._group_alive(pgid):
                self._signal(pgid, signal.SIGKILL)
        deadline = time.monotonic() + 3 * KILL_AFTER  # SIGKILL cannot be caught; do not hang on D state
        while time.monotonic() < deadline and any(self._group_alive(g) for g in groups):
            time.sleep(TICK)
        self.lingering.clear()

    def _reap_lingering(self) -> None:
        """KILL a finished lane's leftover processes that ignored the TERM sent at its end."""
        now = time.monotonic()
        keep = []
        for pgid, deadline in self.lingering:
            if not self._group_alive(pgid):
                continue
            if now >= deadline:
                self._signal(pgid, signal.SIGKILL)
                continue
            keep.append((pgid, deadline))
        self.lingering = keep

    @staticmethod
    def _signal(pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def _group_alive(self, pgid: int) -> bool:
        for live in self.running.values():
            if live.proc.pid == pgid:
                live.proc.poll()  # reap a leader that exited, or it stays a zombie of the group
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    # the whole verification ------------------------------------------------------------

    def run(self) -> int:
        """Run everything. Whatever happens, no lane process is left behind; an unexpected
        error finishes lanes.json as failed instead of leaving it saying ``running``."""
        try:
            return self._run()
        except Exception as exc:
            print(f'forge-verify: {type(exc).__name__}: {exc}', file=sys.stderr)
            self._kill_all()
            self._abort()
            return 1
        finally:
            self._kill_all()
            for live in self.running.values():
                live.close()

    def _abort(self) -> None:
        now = time.time()
        for lane in self.doc['lanes']:
            if lane['state'] == 'running':
                lane.update(state='interrupted', ended=now, step=None)
                lane['runs'][-1]['ended'] = now
            elif lane['state'] == 'waiting':
                lane['state'] = 'skipped'
        self.doc.update(state='fail', ended=now)
        for write in (self.save, self.write_runs):  # best effort: the disk may be what failed
            try:
                write()
            except OSError:
                pass

    def _run(self) -> int:
        self.out.mkdir(parents=True, exist_ok=True)
        try:
            before = fingerprint(self.checkout, self.fingerprint_cmd)
        except (RuntimeError, OSError) as exc:
            print(f'forge-verify: cannot fingerprint {self.checkout}: {exc}', file=sys.stderr)
            self._abort()
            return 2
        self.save()
        lanes = self.doc['lanes']
        names = [lane['name'] for lane in lanes]
        firsts = [n for n in names if self.by_name[n]['first']]
        rest = [n for n in names if n not in firsts]
        if firsts:
            self.run_round(firsts)
        if not self.stop:
            if any(self.by_name[n]['state'] != 'pass' for n in firsts):
                for n in rest:
                    self.by_name[n]['state'] = 'skipped'
            else:
                self.run_round(rest)
        if self.stop:
            for lane in lanes:
                if lane['state'] in ('waiting', 'running'):
                    lane['state'] = 'interrupted'
            self.doc['state'] = 'interrupted'
        else:
            self._kill_all()  # leftovers of the last lanes, before the tree is looked at again
            try:
                self.doc['mutated'] = fingerprint(self.checkout, self.fingerprint_cmd) != before
            except (RuntimeError, OSError):
                self.doc['mutated'] = True
            ok = all(lane['state'] == 'pass' for lane in lanes) and not self.doc['mutated']
            self.doc['state'] = 'pass' if ok else 'fail'
        self.doc['ended'] = time.time()
        self.save()
        self.write_runs()
        self.summary()
        return INTERRUPTED if self.stop else 0 if self.doc['state'] == 'pass' else 1

    def summary(self) -> None:
        for lane in self.doc['lanes']:
            took = '' if lane['started'] is None or lane['ended'] is None \
                else f'{int(lane["ended"] - lane["started"])}s'
            print(f'  {lane["name"]:<24} {lane["state"]:<12} {took}'.rstrip())
        if self.doc['mutated']:
            print('  the working tree changed while the lanes ran; with lanes in parallel the '
                  'change cannot be pinned to one lane (mutated: true)')
        print(f'verification: {self.doc["state"]}')


# --- command line ----------------------------------------------------------------------

def positive(value: str) -> int:
    if not re.fullmatch(r'[0-9]+', value) or int(value) < 1:
        raise argparse.ArgumentTypeError('must be an integer of at least 1')
    return int(value)


def cmd_lanes(args: argparse.Namespace) -> int:
    lanes, errors = load_lanes(args.lanes_dir)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({'lanes': lanes}, indent=1))
        return 0
    for lane in lanes:
        flags = ' '.join(filter(None, ['first' if lane['first'] else '',
                                       f'group={lane["group"]}' if lane['group'] else ''] +
                                [f'{k}={v}' for k, v in lane['options'].items()]))
        print(f'{lane["name"]}\t{flags or "-"}\t{lane["command"].splitlines()[0]}')
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not Path(args.checkout).is_dir():
        print(f'{args.checkout}: not a directory', file=sys.stderr)
        return 2
    lanes, errors = load_lanes(args.lanes_dir)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 2
    verification = Verification(args.checkout, args.out, lanes, args.parallel, args.fingerprint_cmd)

    def interrupt(signum, frame):
        verification.stop = True

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    return verification.run()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='forge-verify.py', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    lanes = sub.add_parser('lanes', help='validate and list the declared lanes')
    lanes.add_argument('--lanes-dir', required=True)
    lanes.add_argument('--json', action='store_true')
    lanes.set_defaults(func=cmd_lanes)
    run = sub.add_parser('run', help='run the lanes')
    run.add_argument('--checkout', required=True)
    run.add_argument('--out', required=True)
    run.add_argument('--lanes-dir', required=True)
    run.add_argument('--parallel', type=positive, default=3)
    run.add_argument('--fingerprint-cmd')
    run.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
