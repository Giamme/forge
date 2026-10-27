#!/usr/bin/env python3
"""Verification lanes: run a project's verification as parallel, named lanes.

``--lanes-dir`` holds one file per lane. A lane is a shell command (run as
``/bin/bash -c <text>`` in the checkout, exactly like the single verify command) with
optional leading ``#forge: key[=value] ...`` lines:

* ``group=<name>``  lanes of one group never run at the same time
* ``first``         the lane runs before all others; if one fails nothing else starts
* ``retries=<n>``   reruns of the lane when it fails (else ``--retries``)
* ``reports=<dir>``  JUnit (``*.xml``) and TAP (``*.tap``) reports the lane writes, relative to
  the checkout: only files written during a run count, re-read at most every 10 s while it
  runs, and the result is the lane's ``tests`` (counts and failing tests)
* ``baseline=<file>`` known failing test names, one per line, relative to the checkout: a
  lane whose every failing test is known ends ``known``, which passes; incomplete
  evidence (a report that could not be read) never does

    forge-verify.py lanes --lanes-dir DIR [--checkout DIR] [--json]
    forge-verify.py run --checkout DIR --out DIR --lanes-dir DIR [--parallel N]
                        [--retries N] [--fingerprint-cmd CMD]

``run`` writes, into ``--out``: one log per lane (``verification.lane.<name>.log``, the run in
progress, and ``verification.lane.<name>.run-<k>.log`` for each finished run),
``verification.lanes.json`` (rewritten atomically on every state change and at most once
a second while lanes print) and ``verification.runs.tsv``
(``n kind exit tree_changed duration_s log lane started``, one row per lane run).

A failed lane is rerun alone, in rounds after the initial one, up to its budget
(``--retries``, default 1, or the lane's ``retries=``); a pass on a rerun makes it ``flaky``.
Nothing reruns once the checkout changed: a rerun on another tree proves nothing.

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
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import forge_verify_reports as reports  # noqa: E402  next to this script

LANE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
HEADER = '#forge:'
KILL_AFTER = 5.0          # seconds between TERM and KILL on an interrupt
WRITE_EVERY = 1.0         # lanes.json refresh while lanes only print
TICK = 0.05
SCAN_EVERY = 10.0         # seconds between looks at a running lane's reports
PASSING = ('pass', 'flaky', 'known')
LAST_LINE_MAX = 300
PUMP_MAX = 4 << 20        # bytes read from one lane's log per look, so a flood cannot starve the loop
TAIL_KEEP = 64 << 10      # what is still read of a finished lane's backlog: its last line and markers
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


def path_errors(lane: dict, checkout: str | None) -> list[str]:
    """reports= and baseline= stay inside the checkout; with one, the baseline must exist."""
    errors = []
    for key in ('reports', 'baseline'):
        value = lane['options'].get(key)
        if value is None:
            continue
        norm = os.path.normpath(value)
        if os.path.isabs(value) or norm == '..' or norm.startswith('..' + os.sep):
            errors.append(f'{lane["name"]}: {key}={value} must be a path inside the checkout')
            continue
        if not checkout:
            continue
        root = os.path.realpath(checkout)
        real = os.path.realpath(os.path.join(checkout, value))
        if real != root and not real.startswith(root + os.sep):
            errors.append(f'{lane["name"]}: {key}={value} leads out of the checkout')
        elif key == 'baseline':
            try:
                regular = stat.S_ISREG(os.lstat(os.path.join(checkout, value)).st_mode)
            except OSError:
                regular = False
            if not regular:
                errors.append(f'{lane["name"]}: baseline={value} is not a regular file in the checkout')
    return errors


def load_lanes(lanes_dir: str, checkout: str | None = None) -> tuple[list[dict], list[str]]:
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
            problems = path_errors(lane, checkout)
            errors.extend(problems)
            if not problems:
                lanes.append(lane)
    if not lanes and not errors:
        errors.append(f'{lanes_dir}: no lanes declared')
    return lanes, errors


# --- mutation check --------------------------------------------------------------------

def _text(data: bytes) -> str:
    return data.decode('utf-8', 'replace').strip()


class Stopped(Exception):
    """A stop arrived while a fingerprint command ran; the command was stopped."""


def run_stoppable(argv: list[str], stopped=None, **kwargs) -> tuple[int, bytes, bytes]:
    """Run a command in its own process group and wait for it, stopping the group (TERM, then
    KILL after KILL_AFTER seconds) and raising Stopped as soon as ``stopped()`` is true."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True, **kwargs)
    while True:
        try:
            out, err = proc.communicate(timeout=TICK)
            return proc.returncode, out, err
        except subprocess.TimeoutExpired:
            if stopped is None or not stopped():
                continue
        for sig, wait in ((signal.SIGTERM, KILL_AFTER), (signal.SIGKILL, KILL_AFTER)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        raise Stopped()


def _git(checkout: str, *args: str, env: dict | None = None, stopped=None) -> str:
    rc, out, err = run_stoppable(['git', '-C', checkout, *args], stopped, env=env)
    if rc != 0:
        raise RuntimeError(f'git {" ".join(args)}: {_text(err)}')
    return _text(out)


def default_fingerprint(checkout: str, stopped=None) -> str:
    """HEAD plus the tree id of the whole working tree (tracked and untracked, ignored files
    left out), by content: the status labels do not change when an already modified file
    is edited again. It is built in a temporary copy of the index, so the real one is
    never touched. ``.forge`` is left out, as in forge_tree."""
    try:
        head = _git(checkout, 'rev-parse', 'HEAD', stopped=stopped)
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
        _git(checkout, 'add', '-A', '--', '.', ':(exclude).forge', env=env, stopped=stopped)
        tree = _git(checkout, 'write-tree', env=env, stopped=stopped)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    return f'{head} {tree}'


def fingerprint(checkout: str, command: str | None, stopped=None) -> str:
    """The checkout's fingerprint; Stopped when ``stopped()`` turns true meanwhile."""
    if not command:
        return default_fingerprint(checkout, stopped)
    rc, out, err = run_stoppable(['/bin/bash', '-c', command], stopped, cwd=checkout)
    if rc != 0:
        raise RuntimeError(f'fingerprint command exited {rc}: {_text(err)}')
    return _text(out)


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


def run_log(name: str, k: int) -> str:
    """Where run k of a lane is kept once it is over; no dots in names, so no collision."""
    return f'verification.lane.{name}.run-{k}.log'


class Live:
    """A started lane process and what is read from its log so far."""

    def __init__(self, proc: subprocess.Popen, log: Path, offset: int):
        self.proc = proc
        self.reader = open(log, 'rb')
        self.reader.seek(offset)
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.partial = ''
        self.exited = False  # the leader has exited and its group was sent the TERM
        self.scanned = time.monotonic()   # its reports are first looked at after SCAN_EVERY

    def close(self) -> None:
        self.reader.close()


class Verification:
    def __init__(self, checkout: str, out: str, lanes: list[dict], parallel: int,
                 fingerprint_cmd: str | None = None, retries: int = 1):
        self.checkout = checkout
        self.out = Path(out)
        self.parallel = parallel
        self.retries = retries
        self.before: str | None = None
        self.fingerprint_cmd = fingerprint_cmd
        self.stop = False
        self.running: dict[str, Live] = {}
        # (pgid, KILL time, the lane's group, give-up time) of finished lanes whose leftovers still run
        self.lingering: list[tuple[int, float, str | None, float]] = []
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
            for name, live in running.items():
                if not live.exited and live.proc.poll() is not None:
                    self._leader_exited(name, live)
            drained = self._pump(running)
            self._scan_running(running)
            for name in [n for n, live in running.items() if live.exited and n in drained]:
                self._finish(name, running.pop(name))
            self._reap_lingering()
            # a group is free once no process of its last lane is left (a server still shutting
            # down can hold the port the next lane of the group needs)
            busy = ({self.by_name[n]['group'] for n in running} | {g for _, _, g, _ in self.lingering}) - {None}
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
        k = len(lane['runs']) + 1
        run = {'k': k, 'kind': kind, 'started': now, 'ended': None, 'exit': None,
               'log': run_log(name, k)}
        lane['runs'].append(run)
        lane.update(state='running', started=now, ended=None, exit=None, step=None)
        try:
            if os.path.islink(log):
                os.unlink(log)   # the log name is ours: never written through a link left in --out
            fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o666)
            with os.fdopen(fd, 'ab') as fh:
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

    def _leader_exited(self, name: str, live: Live) -> None:
        """The lane's leader has exited. Whatever it left running in its process group (a
        background server, a watcher) is terminated now, so a leftover that keeps printing
        stops: no lane process outlives the script. The lane stays running until its log is
        read to the end, which the main loop does a capped step at a time."""
        live.exited = True
        pgid = live.proc.pid
        if self._group_alive(pgid):
            self._signal(pgid, signal.SIGTERM)
            now = time.monotonic()
            self.lingering.append((pgid, now + KILL_AFTER, self.by_name[name]['group'], now + 4 * KILL_AFTER))

    def _finish(self, name: str, live: Live, interrupted: bool = False) -> None:
        """Record the end of a lane whose leader has exited (or was killed)."""
        lane = self.by_name[name]
        rc = live.proc.poll()
        if rc is None:  # still there after the KILL: a process stuck in the kernel
            rc = -signal.SIGKILL
        self._pump({name: live})  # what is left to read, capped; an interrupt still gets through
        live.close()
        self._end(lane, lane['runs'][-1], rc if rc >= 0 else 128 - rc, interrupted)

    def _end(self, lane: dict, run: dict, code: int, interrupted: bool = False) -> None:
        now = time.time()
        run.update(ended=now, exit=code)
        lane.update(state='interrupted' if interrupted else self._judge(lane, run, code),
                    exit=code, ended=now, step=None)
        self.save()
        self.write_runs()

    def _pump(self, running: dict[str, Live], limit: int = PUMP_MAX) -> set[str]:
        """Read what the lanes printed since the last look: last_line and the step markers.
        At most ``limit`` bytes per lane, so the caller gets back to its stop check, the
        reaping and the refresh however fast a lane prints. Returns the names of the lanes whose
        log was read to the end."""
        drained: set[str] = set()
        for name, live in running.items():
            lane = self.by_name[name]
            exited = live.proc.poll() is not None  # asked first: all of its output is then readable
            if exited and live.exited:
                self._skip_backlog(live)
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
            if eof:
                drained.add(name)
            if eof and exited:
                tail = live.partial + live.decoder.decode(b'', final=True)
                live.partial = ''
                if tail:
                    self._line(lane, tail)
        return drained

    @staticmethod
    def _skip_backlog(live: Live) -> None:
        """A finished lane's unread backlog is not read through: only its last line matters
        now, and the log keeps every byte. Otherwise a leftover that ignores the TERM and
        floods until its KILL would keep the lane draining long after it ended."""
        try:
            size = os.fstat(live.reader.fileno()).st_size
        except OSError:
            return
        if size - live.reader.tell() > TAIL_KEEP + PUMP_MAX:
            live.reader.seek(size - TAIL_KEEP)
            live.decoder.reset()
            live.partial = ''

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

    # reports and the baseline -------------------------------------------------------------

    def _scan(self, lane: dict, run: dict):
        """The run's reports (written since it started) as the lane's tests; None without reports=."""
        rel = lane['options'].get('reports')
        if not rel:
            return None
        tests, names, complete = reports.scan(os.path.join(self.checkout, rel), run['started'])
        # Counts once a report exists: a run that wrote none has nothing to count.
        lane['tests'] = tests if tests['reports'] or tests['notes'] else None
        return tests, names, complete

    def _scan_running(self, running: dict) -> None:
        now = time.monotonic()
        for name, live in running.items():
            lane = self.by_name[name]
            if live.exited or not lane['options'].get('reports') or now - live.scanned < SCAN_EVERY:
                continue
            live.scanned = now
            previous = lane['tests']
            self._scan(lane, lane['runs'][-1])
            if lane['tests'] != previous:
                self.save(force=False)

    def _judge(self, lane: dict, run: dict, code: int) -> str:
        """pass, fail, or known: a non-zero exit whose every failing test, on complete
        evidence, is in the lane's baseline."""
        scanned = self._scan(lane, run)
        if scanned is None:
            return 'pass' if code == 0 else 'fail'
        tests, names, complete = scanned
        if code != 0 and lane['options'].get('baseline') and lane['tests'] is None:
            lane['tests'] = tests     # where the note below can be read
        if tests['failing']:
            at = reports.log_lines(str(self.out / lane['log']), [f['name'] for f in tests['failing']])
            for entry in tests['failing']:
                entry['log_line'] = at.get(entry['name'])
        baseline = lane['options'].get('baseline')
        if code == 0 or not baseline:
            return 'pass' if code == 0 else 'fail'
        try:
            known = reports.load_baseline(os.path.join(self.checkout, baseline))
        except OSError as exc:
            tests['notes'].append(f'judged by its exit code: the baseline could not be read ({exc})')
            return 'fail'
        for entry in tests['failing']:
            entry['known'] = entry['name'] in known
        if not complete:
            tests['notes'].append('judged by its exit code: a report written during this run could not be read')
            return 'fail'
        if not names:
            tests['notes'].append('judged by its exit code: no failing test was found in its reports')
            return 'fail'
        # Over every failure, not only the 200 kept for display.
        return 'known' if all(name.strip() in known for name in names) else 'fail'

    # reruns ---------------------------------------------------------------------------------

    def budget(self, name: str) -> int:
        return self.by_name[name]['options'].get('retries', self.retries)

    def changed(self) -> bool:
        """The mutation check, after a round: leftovers are stopped first, and once the tree
        changed it stays changed."""
        if not self.doc['mutated']:
            self._kill_all()
            try:
                self.doc['mutated'] = fingerprint(self.checkout, self.fingerprint_cmd,
                                                  lambda: self.stop) != self.before
            except Stopped:
                return False          # interrupted: nothing is judged, the caller sees self.stop
            except (RuntimeError, OSError):
                self.doc['mutated'] = True
            if self.doc['mutated']:
                self.save()
        return self.doc['mutated']

    def rerun(self, names: list[str]) -> None:
        """Rounds of reruns of the failed lanes among ``names`` with budget left, each round
        under the same parallel and group rules; a pass on a rerun is flaky."""
        while not self.stop and not self.changed():
            if self.stop:     # it came during that look at the tree
                return
            due = [n for n in names if self.by_name[n]['state'] == 'fail'
                   and len(self.by_name[n]['runs']) - 1 < self.budget(n)]
            if not due:
                return
            for name in due:
                self._archive(name, truncate=True)
            self.run_round(due, kind='rerun')
            for name in due:
                lane = self.by_name[name]
                if lane['state'] == 'pass':
                    lane['state'] = 'flaky'
            self.save()

    def _archive(self, name: str, truncate: bool = False) -> None:
        """Keep the lane's last run under its run-<k> name; a rerun then starts a fresh log."""
        lane = self.by_name[name]
        if not lane['runs']:
            return
        live, kept = self.out / lane['log'], self.out / lane['runs'][-1]['log']
        try:
            if os.path.islink(live) or os.path.islink(kept):
                raise OSError('a symbolic link is not written through')
            if live.exists() and not kept.exists():
                shutil.copyfile(live, kept)
            if truncate:
                os.close(os.open(live, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW))
        except OSError as exc:
            print(f'forge-verify: cannot keep {kept.name}: {exc}', file=sys.stderr)

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
        groups = {live.proc.pid for live in self.running.values()} | {entry[0] for entry in self.lingering}
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
        """KILL a finished lane's leftover processes that ignored the TERM sent at its end, and
        forget them once they are gone (or, stuck in the kernel, after the give-up time)."""
        now = time.monotonic()
        keep = []
        for pgid, deadline, group, give_up in self.lingering:
            if not self._group_alive(pgid) or now >= give_up:
                continue
            if now >= deadline:
                self._signal(pgid, signal.SIGKILL)
            keep.append((pgid, deadline, group, give_up))
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
            self.before = fingerprint(self.checkout, self.fingerprint_cmd, lambda: self.stop)
        except Stopped:
            pass                      # self.stop is set: no lane starts, every lane ends interrupted
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
            self.rerun(firsts)   # before the gate: a flaky first lane lets the rest run
        if not self.stop:
            if any(self.by_name[n]['state'] not in PASSING for n in firsts) or self.doc['mutated']:
                for n in rest:
                    self.by_name[n]['state'] = 'skipped'
            else:
                self.run_round(rest)
                self.rerun(rest)
        if not self.stop:
            self.changed()   # stops the last lanes' leftovers, then looks at the tree again
        if self.stop:        # also when the stop came during that last look
            for lane in lanes:
                if lane['state'] in ('waiting', 'running'):
                    lane['state'] = 'interrupted'
            self.doc['state'] = 'interrupted'
        else:
            ok = all(lane['state'] in PASSING for lane in lanes) and not self.doc['mutated']
            self.doc['state'] = 'pass' if ok else 'fail'
        for name in names:
            self._archive(name)   # every run has its own log, the last one included
        self.doc['ended'] = time.time()
        self.save()
        self.write_runs()
        self.summary()
        return INTERRUPTED if self.stop else 0 if self.doc['state'] == 'pass' else 1

    def summary(self) -> None:
        for lane in self.doc['lanes']:
            took = '' if lane['started'] is None or lane['ended'] is None \
                else f'{int(lane["ended"] - lane["started"])}s'
            runs = len(lane['runs'])
            extra = [f'({runs} runs)'] if runs > 1 else []
            tests = lane['tests']
            if tests:
                extra.append(f'tests {tests["pass"]} passed, {tests["fail"] + tests["error"]} failed, '
                             f'{tests["skip"]} skipped')
                known = sum(1 for f in tests['failing'] if f['known'])
                if known:
                    extra.append(f'known {known}')
            print(f'  {lane["name"]:<24} {lane["state"]:<12} {took:<6} {" ".join(extra)}'.rstrip())
        flaky = [lane['name'] for lane in self.doc['lanes'] if lane['state'] == 'flaky']
        if flaky:
            print('  flaky (failed, then passed on a rerun): ' + ', '.join(flaky))
        if self.doc['mutated']:
            print('  the working tree changed while the lanes ran; with lanes in parallel the '
                  'change cannot be pinned to one lane (mutated: true)')
        print(f'verification: {self.doc["state"]}')


# --- command line ----------------------------------------------------------------------

def positive(value: str) -> int:
    if not re.fullmatch(r'[0-9]+', value) or int(value) < 1:
        raise argparse.ArgumentTypeError('must be an integer of at least 1')
    return int(value)


def retries_value(value: str) -> int:
    if not re.fullmatch(r'[0-9]', value):
        raise argparse.ArgumentTypeError('must be an integer from 0 to 9')
    return int(value)


def cmd_lanes(args: argparse.Namespace) -> int:
    lanes, errors = load_lanes(args.lanes_dir, args.checkout)
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
    lanes, errors = load_lanes(args.lanes_dir, args.checkout)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 2
    verification = Verification(args.checkout, args.out, lanes, args.parallel, args.fingerprint_cmd,
                                args.retries)

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
    lanes.add_argument('--checkout', help='also check reports= and baseline= against this checkout')
    lanes.add_argument('--json', action='store_true')
    lanes.set_defaults(func=cmd_lanes)
    run = sub.add_parser('run', help='run the lanes')
    run.add_argument('--checkout', required=True)
    run.add_argument('--out', required=True)
    run.add_argument('--lanes-dir', required=True)
    run.add_argument('--parallel', type=positive, default=3)
    run.add_argument('--retries', type=retries_value, default=1,
                     help='reruns of a failed lane (default 1; a lane\'s retries= wins)')
    run.add_argument('--fingerprint-cmd')
    run.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
