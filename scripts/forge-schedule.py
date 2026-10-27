#!/usr/bin/env python3
"""Readiness scheduler. Only the coordinator merges; tasks keep pinned baselines.

Two opt-in behaviours live here, both off by default so a plain `run` is unchanged:

* ``--retry-failed N`` re-queues a task whose reviewer said FAIL/UNKNOWN up to N more
  times *inside the same run*, so one failing task never holds the others back until the
  run exits. Dependents already wait for MERGED, so nothing downstream starts early.
* ``--infra-retries M`` waits out an infrastructure failure (quota, auth, rate limit,
  network, empty output) up to M times instead of stopping. Without it the scheduler
  stops dispatching, lets running tasks drain and exits 8: nothing was the task's fault
  and no attempt was spent, so the next `run` simply resumes.

A third, ``--sweep Pn``, also per invocation: once every task has merged, the open review
findings at Pn or worse become fix tasks (``forge-parallel.sh _sweep``, forge-sweep.py) that
this run then dispatches like any other, once per plan. A fix task that fails leaves its
findings for ``forge issues`` and does not fail the run.

Both numbers are per invocation, but while the scheduler runs they are mirrored into the
plan dir (retry_budget, infra_budget, tasks/<id>/retry_budget) and re-read at every
decision, so `forge control` can change them mid-run. See "live control files" below.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
from pathlib import Path
import posixpath
import signal
import stat
import subprocess
import sys
import tempfile
import time


def dataset_event(name: str, plan: Path, task: str, values: dict) -> None:
    """Record a status only the scheduler sets (INTERRUPTED, BLOCKED, ERROR) in the run
    dataset. No task process is alive to record it. Fail-open, like every capture."""
    try:
        scripts = str(Path(__file__).resolve().parent)
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from forge_dataset.capture import point
        point(name, str(plan), task, values)
    except Exception:
        pass


def overlap(a: str, b: str) -> bool:
    def paths(value: str) -> list[str]:
        return [posixpath.normpath(p) for p in value.split(',') if p and p != '-']

    return any(
        x == '.' or y == '.' or x == y or x.startswith(y + '/') or y.startswith(x + '/')
        for x in paths(a) for y in paths(b)
    )


INFRA_EXIT = 8
# A parsed retry-after beyond this is not a pause worth waiting for inside one run.
INFRA_WAIT_CAP = 6 * 3600


def infra_info(root: Path) -> dict:
    """key=value lines the task wrote when it stopped on an infrastructure failure."""
    info = {}
    try:
        for line in (root / 'infra.txt').read_text(errors='replace').splitlines():
            key, _, value = line.partition('=')
            if key:
                info[key.strip()] = value.strip()
    except OSError:
        pass
    return info


# --- backoff shapes ----------------------------------------------------------------------
# One parser for the --infra-backoff flag (through `forge-schedule.py backoff-check`), for
# `forge control set infra-backoff` (which loads this file) and for the waits themselves.
BACKOFF_FORMS = 'exp:<base>[,<cap>], linear:<start>,<step>[,<cap>], list:<a>,<b>,... or <a>,<b>,... (seconds, positive)'
_NUMBER = re.compile(r'(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)')
_COUNTS = {'exp': (1, 2), 'linear': (2, 3), 'list': (1, 1000)}


def parse_backoff(text: str) -> tuple:
    """(kind, numbers) for a backoff shape; ValueError naming the forms when malformed."""
    shape = (text or '').strip()
    kind, colon, rest = shape.partition(':')
    if not colon:
        kind, rest = 'list', shape
    if kind not in _COUNTS:
        raise ValueError('unknown backoff shape %r: use %s' % (shape, BACKOFF_FORMS))
    numbers = []
    for part in rest.split(','):
        part = part.strip()
        if not _NUMBER.fullmatch(part) or float(part) <= 0:
            raise ValueError('backoff %r needs positive numbers: use %s' % (shape, BACKOFF_FORMS))
        numbers.append(float(part))
    low, high = _COUNTS[kind]
    if not low <= len(numbers) <= high:
        raise ValueError('backoff %r has the wrong number of values: use %s' % (shape, BACKOFF_FORMS))
    return kind, numbers


def backoff_wait(shape: tuple, cycle: int) -> float:
    """Seconds to wait before resuming for a 0-based wait number."""
    kind, numbers = shape
    if kind == 'exp':
        cap = numbers[1] if len(numbers) > 1 else max(numbers[0], 1800.0)
        return min(numbers[0] * 2 ** min(cycle, 64), cap)
    if kind == 'linear':
        wait = numbers[0] + numbers[1] * cycle
        return min(wait, numbers[2]) if len(numbers) > 2 else wait
    return numbers[min(cycle, len(numbers) - 1)]


def default_backoff() -> tuple:
    """Today's shape: exp with FORGE_INFRA_BACKOFF (60 s) as the base. (shape, its name)"""
    try:
        base = float(os.environ.get('FORGE_INFRA_BACKOFF', '60'))
    except ValueError:
        base = 60.0
    return ('exp', [base]), 'exp:%g' % base


def infra_wait(info: dict, cycle: int, backoff: tuple | None = None) -> tuple:
    """(seconds, what decided it) before re-dispatching after an infrastructure failure. A
    provider's retry-after wins; otherwise the backoff shape (shape, name) for this wait."""
    try:
        hinted = float(info.get('retry_after', ''))
    except ValueError:
        hinted = None
    if hinted is not None and hinted >= 0:
        return min(hinted, INFRA_WAIT_CAP), 'provider retry-after'
    shape, name = backoff or default_backoff()
    return backoff_wait(shape, cycle), 'backoff ' + name


# --- pause scopes ----------------------------------------------------------------------
# A capacity error on one model says nothing about another, and a quota or auth stop is one
# harness's account; only the remaining classes (network, empty output, ...) stop everything.
def infra_scope(info: dict) -> tuple:
    """(scope, harness, model) for a stop's infra.txt."""
    cls, harness, model = info.get('class', ''), info.get('harness', ''), info.get('model', '')
    if cls == 'rate_limit' and harness and model:
        return 'model:%s/%s' % (harness, model), harness, model
    if cls in ('quota', 'auth') and harness:
        return 'harness:' + harness, harness, ''
    return 'all', harness, model


def load_registry() -> list:
    path = os.environ.get('FORGE_REGISTRY') or str(Path(__file__).resolve().parent.parent / 'registry.tsv')
    rows = []
    try:
        with open(path, errors='replace') as handle:
            for line in handle:
                columns = line.rstrip('\n').split('\t')
                if not line.startswith('#') and len(columns) >= 3:
                    rows.append(columns)
    except OSError:
        pass
    return rows


def spec_pair(registry: list, spec: str) -> tuple | None:
    """(harness, model) for a spec, resolved the way forge-dispatch.sh's lookup does: an
    alias's first row is its default harness, alias:effort:harness picks that row, and an
    unknown alias with a harness is a literal model id. None when it cannot be resolved."""
    if not spec or spec in ('-', 'UNASSIGNED') or ',' in spec:
        return None
    parts = spec.split(':')
    alias, harness = parts[0], ':'.join(parts[2:])
    if not alias:
        return None
    for row in registry:
        if row[0] == alias and (not harness or row[1] == harness):
            return row[1], row[2]
    return (harness, alias) if harness else None


def holds(pause: dict, pairs: list) -> bool:
    """True when a task whose next dispatch uses these pairs falls under this pause."""
    for pair in pairs:
        if pair is None:
            continue
        if pause['model'] is not None and pair == (pause['harness'], pause['model']):
            return True
        if pause['model'] is None and pair[0] == pause['harness']:
            return True
    return False


# --- live control files ------------------------------------------------------------------
# Created at start and removed at exit, so a later `run` never inherits a budget it did not
# ask for. Writers replace files atomically, so readers need no lock; the control lock only
# orders a lifecycle write against `forge control`'s liveness check. It is never held across
# a model dispatch and never taken together with schedule.lock.
CONTROL_LOCK_WAIT = 5.0   # seconds; a busy lock must never fail a run
SWEEP_EVERY = 1.0         # seconds between looks at tasks that already ended FAIL/UNKNOWN


def _warn(message: str) -> None:
    print('forge: ' + message, file=sys.stderr, flush=True)


def _task_id_ok(name: str) -> bool:
    return bool(name) and name not in ('.', '..') and '/' not in name and '\0' not in name


def _resolve(plan: object, *parts: str) -> str:
    """plan/<parts> if every directory on the way is a real one (no symlink) inside the
    canonical plan dir and an existing target is a regular file. OSError otherwise."""
    if not all(_task_id_ok(part) for part in parts):
        raise OSError(errno.EINVAL, 'invalid path component', '/'.join(parts))
    path = os.path.realpath(plan)
    for part in parts[:-1]:
        path = os.path.join(path, part)
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            raise OSError(errno.ENOTDIR, 'not a real directory', path)
    path = os.path.join(path, parts[-1])
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise OSError(errno.EPERM, 'not a regular file', path)
    except FileNotFoundError:
        pass
    return path


def _write_atomic(plan: object, text: str, *parts: str) -> bool:
    """Temp file in the same directory, then rename. A failure warns and returns False."""
    tmp = None
    try:
        path = _resolve(plan, *parts)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.' + parts[-1] + '.')
        with os.fdopen(fd, 'w') as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(text)
        os.replace(tmp, path)
        return True
    except OSError as error:
        _warn('could not write ' + '/'.join(parts) + ': ' + str(error))
        if tmp:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False


def _remove(plan: object, *parts: str) -> None:
    try:
        os.unlink(_resolve(plan, *parts))
    except FileNotFoundError:
        pass
    except OSError as error:
        _warn('could not remove ' + '/'.join(parts) + ': ' + str(error))


def _read_budget(plan: object, *parts: str) -> int | None:
    """A non-negative integer from a control file; None when it is missing, empty, a
    symlink or anything else, so the caller falls through to its next source."""
    try:
        fd = os.open(_resolve(plan, *parts), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            data = os.read(fd, 65)
        finally:
            os.close(fd)
        text = data.decode('ascii').strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if len(data) <= 64 and text.isdigit() else None


@contextlib.contextmanager
def _control_lock(plan: object):
    """flock on control.lock, retried for CONTROL_LOCK_WAIT seconds. On timeout or any
    error: warn and proceed without it (yielding False), never fail the run."""
    fd = None
    try:
        fd = os.open(_resolve(plan, 'control.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        deadline = time.monotonic() + CONTROL_LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError(errno.EWOULDBLOCK, 'busy for ' + ('%g' % CONTROL_LOCK_WAIT) + 's')
                time.sleep(0.05)
    except OSError as error:
        _warn('control.lock not taken, continuing without it: ' + str(error))
        if fd is not None:
            os.close(fd)
            fd = None
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def _task_dirs(plan: object, names: object) -> list[str]:
    """The planned tasks plus any other task dir, since `forge control` accepts an id
    that is not in tasks.tsv and its file must not outlive the invocation either."""
    found = set(names)
    try:
        with os.scandir(os.path.join(os.path.realpath(plan), 'tasks')) as entries:
            found.update(e.name for e in entries if _task_id_ok(e.name) and e.is_dir(follow_symlinks=False))
    except OSError:
        pass
    return sorted(found)


def _read_small(plan: object, name: str) -> str | None:
    """A short plan-level control file's stripped text; None when missing or unsafe."""
    try:
        fd = os.open(_resolve(plan, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            return os.read(fd, 65).decode('ascii').strip()
        finally:
            os.close(fd)
    except (OSError, UnicodeDecodeError):
        return None


def stop_mode(plan: object) -> str | None:
    """`forge control stop` wrote `graceful` or `now`; anything else is no stop."""
    text = _read_small(plan, 'stop')
    return text if text in ('graceful', 'now') else None


def drained(plan: object) -> bool:
    return _read_small(plan, 'drain') is not None


def _fractal_plan(plan: object) -> bool:
    """fractal-selection.json enabled (unreadable counts: fail closed, as forge-control does)."""
    try:
        text = Path(_resolve(plan, 'fractal-selection.json')).read_text()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not text.strip():
        return False
    try:
        return bool(json.loads(text).get('enabled'))
    except (ValueError, AttributeError):
        return True


def live_capacity(plan: object, default: int) -> int:
    """`forge control set max-parallel` (1-64), else the argument the run started with."""
    text = _read_small(plan, 'max_parallel')
    return int(text) if text and text.isdigit() and 1 <= int(text) <= 64 else default


def _pinned(plan: object, name: str) -> bool:
    try:
        return os.path.lexists(_resolve(plan, 'tasks', name, 'escalation.pin'))
    except OSError:
        return False


# --- interrupted tasks ---------------------------------------------------------------------
# A task's harness leads its own session (forge-guard.py session), so the SIGTERM sent to the
# task's group reaches dispatch but not the harness: dispatch's EXIT trap stops it. The grace
# lets that trap finish; the guard's `stop` then acts on whatever <role>.harness record a
# dispatch that died too early left behind.
GUARD = str(Path(__file__).resolve().parent / 'forge-guard.py')
HARNESS_ROLES = ('dwarf', 'qa')
GUARD_STOP_WAIT = 15.0    # seconds; the guard's own bound is about 3 s plus two ps calls


def interrupt_grace() -> float:
    """Seconds an interrupted task group gets to empty before SIGKILL."""
    try:
        value = float(os.environ.get('FORGE_INTERRUPT_GRACE', '10'))
    except ValueError:
        return 10.0
    return value if 0 <= value < float('inf') else 10.0


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def stop_harnesses(plan: object, names: object) -> None:
    """Run the guard's identity-checked `stop` on every harness record of these tasks, all
    at once, so the bound does not grow with the number of tasks. Never raises."""
    stopping = []
    for name in names:
        for role in HARNESS_ROLES:
            try:
                record = _resolve(plan, 'tasks', name, role + '.harness')
                if not os.path.lexists(record):
                    continue
                stopping.append((name, role, subprocess.Popen(
                    [sys.executable, GUARD, 'stop', record], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)))
            except (OSError, ValueError) as error:
                _warn('could not stop the ' + role + ' harness of ' + name + ': ' + str(error))
    deadline = time.monotonic() + GUARD_STOP_WAIT
    for name, role, process in stopping:
        try:
            out, _ = process.communicate(timeout=max(deadline - time.monotonic(), 0.1))
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            _warn('stopping the ' + role + ' harness of ' + name + ' took too long; gave up')
            continue
        count = (out or '').strip()
        if count.isdigit() and count != '0':
            _warn(name + ': stopped ' + count + ' process(es) its ' + role + ' harness left running')


# --- control requests ----------------------------------------------------------------------
# `forge control retry|add` queue one JSON file each in control.requests/, under control.lock and
# only while scheduler.live says a scheduler reads them. The scheduler applies or refuses each,
# logs one outcome line with the same `request` id, and removes it. live_stop settles whatever
# is left after it withdraws liveness, under the same lock, so no request outlives its run.
REQUESTS = 'control.requests'
REQUEST_MAX = 64 * 1024
RETRYABLE = ('FAIL', 'UNKNOWN', 'TIMEOUT', 'ERROR', 'INTERRUPTED', 'BLOCKED')


def list_requests(plan: object) -> list:
    """Request file names in order; temp (dot) files and anything not a regular file are skipped."""
    directory = os.path.join(os.path.realpath(plan), REQUESTS)
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            return []
        with os.scandir(directory) as entries:
            return sorted(e.name for e in entries
                          if not e.name.startswith('.') and e.is_file(follow_symlinks=False) and _task_id_ok(e.name))
    except OSError:
        return []


def read_request(plan: object, name: str) -> dict | None:
    try:
        fd = os.open(_resolve(plan, REQUESTS, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            data = os.read(fd, REQUEST_MAX + 1)
        finally:
            os.close(fd)
        request = json.loads(data.decode('utf-8'))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if len(data) > REQUEST_MAX or not isinstance(request, dict) or request.get('verb') not in ('retry', 'add'):
        return None
    task, dwarf = request.get('task'), request.get('dwarf')
    if not isinstance(task, str) or not _task_id_ok(task):
        return None
    if dwarf is not None and (not isinstance(dwarf, str) or not dwarf or dwarf.startswith('-')
                              or any(c.isspace() or c == ',' for c in dwarf)):
        return None
    return request


def settle_request(plan: object, name: str, request: dict | None, outcome: str, reason: str | None) -> None:
    """The outcome line in control.log, then the request goes. Called under control.lock."""
    verb = request.get('verb') if request else None
    task = request.get('task') if request else None
    entry = {'at': time.time(), 'actor': 'scheduler', 'action': verb, 'task': task, 'key': None,
             'value': request.get('dwarf') if request else None, 'previous': None, 'request': name,
             'outcome': outcome, 'reason': reason}
    try:
        fd = os.open(_resolve(plan, 'control.log'), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        try:
            os.write(fd, (json.dumps(entry) + '\n').encode('utf-8'))
        finally:
            os.close(fd)
    except OSError as error:
        _warn('could not log the outcome of request ' + name + ': ' + str(error))
    _remove(plan, REQUESTS, name)
    print('forge: control: ' + (verb or 'request') + ' ' + (task or name) + ' '
          + (outcome if outcome == 'applied' else 'refused: ' + (reason or '')), file=sys.stderr, flush=True)


def refuse_requests(plan: object, reason: str) -> None:
    for name in list_requests(plan):
        settle_request(plan, name, read_request(plan, name), 'refused', reason)


def live_start(plan: object, names: object, retry_failed: int, infra_retries: int) -> None:
    """Publish the flags and that this scheduler is live. Stale per-task overrides and
    usage counters from an earlier invocation go first."""
    with _control_lock(plan):
        for name in _task_dirs(plan, names):
            _remove(plan, 'tasks', name, 'retry_budget')
            _remove(plan, 'tasks', name, 'retries.used')
        _remove(plan, 'infra.paused')
        _remove(plan, 'drain')
        _remove(plan, 'stop')
        # Queued for a scheduler that died: never applied to this run.
        refuse_requests(plan, 'left by an earlier run')
        _write_atomic(plan, str(retry_failed), 'retry_budget')
        _write_atomic(plan, str(infra_retries), 'infra_budget')
        _write_atomic(plan, json.dumps({'pid': os.getpid(), 'started': time.time()}), 'scheduler.live')


def live_stop(plan: object, names: object) -> None:
    """Withdraw liveness first, then every budget. retries.used stays for the UI. A request
    queued before liveness went is settled here: none can arrive once the lock is released."""
    try:
        with _control_lock(plan):
            _remove(plan, 'scheduler.live')
            refuse_requests(plan, 'run ended')
            _remove(plan, 'retry_budget')
            _remove(plan, 'infra_budget')
            _remove(plan, 'infra.paused')
            _remove(plan, 'drain')
            _remove(plan, 'stop')
            for name in _task_dirs(plan, names):
                _remove(plan, 'tasks', name, 'retry_budget')
    except Exception as error:
        _warn('could not withdraw the live control files: ' + str(error))


def schedule(script: str, plan: str, capacity: int, retry_failed: int = 0,
             infra_retries: int = 0, sweep: str | None = None, sweep_max: int = 5,
             sweep_plausible: bool = False) -> int:
    if capacity <= 0:
        raise ValueError('capacity must be positive')
    if retry_failed < 0 or infra_retries < 0:
        raise ValueError('retry counts must not be negative')
    plan = Path(plan)
    rows = [
        line.split('\t') for line in (plan / 'tasks.tsv').read_text().splitlines()
        if line and not line.startswith('#')
    ]
    if any(len(row) != 7 for row in rows):
        raise ValueError('tasks.tsv requires seven columns')
    tasks = {row[0]: row for row in rows}
    if len(tasks) != len(rows):
        raise ValueError('duplicate task IDs')
    for name in tasks:
        if not _task_id_ok(name):
            raise ValueError('invalid task ID: ' + name)
        (plan / 'tasks' / name).mkdir(parents=True, exist_ok=True)
    active = {}
    pending = list(tasks)
    interrupted = False
    retries = {}       # automatic reviewer-driven retries used per task, this run
    refused = {}       # task -> budget at the time its retry could not be prepared
    retried = set()    # tasks whose next dispatch is a retry (FORGE_FRACTAL_RETRY=1)
    offsets = {}       # how much of each task.out was already echoed under --output full
    infra_seen = []    # tasks stopped by an infrastructure failure, awaiting resume
    held = set()       # INFRA tasks not resumed: their pin could not be applied
    infra_cycles = 0   # how many times this run already waited out an infrastructure pause
    paused = False     # an `all`-scope infrastructure failure was seen: dispatch nothing new
    resume_at = 0.0
    all_class = ''     # the class of the first `all`-scope stop, for infra.paused
    pauses = {}        # scope -> its pause, for model: and harness: scopes; `all` is the above
    scope_waits = {}   # scope -> waits it already completed this run
    pairs_cache = {}   # task -> (harness, model) pairs its next dispatch uses
    registry = load_registry()
    paused_text = None # what infra.paused holds now
    backoff_warned = [] # an invalid infra_backoff file is reported once
    graceful = False   # `forge control stop`: start nothing more, end once running tasks are done
    human = set()      # tasks whose next dispatch is a `forge control retry` (dataset trigger human)
    fractal = _fractal_plan(plan)
    drain_told = False # the drained message was printed for this drain
    sweep_asked = False  # this invocation already asked for the finding sweep

    def status(name: str) -> str:
        root = plan / 'tasks' / name
        if (root / 'merged').exists():
            return 'MERGED'
        file = root / 'status'
        return file.read_text().strip() if file.exists() else 'PENDING'

    def mark(name: str, state: str) -> None:
        (plan / 'tasks' / name / 'status').write_text(state + '\n')
        dataset_event('sched_terminal', plan, name, dict(status=state))

    def merge(name: str) -> None:
        result = subprocess.run(['/bin/bash', script, '_merge', str(plan), name])
        if result.returncode and status(name) == 'PASS':
            mark(name, 'ERROR')

    def budget(name: str) -> int:
        """Automatic retries this task may use. Read at every decision, never cached: the
        task's own file, then the run-wide file, then the flag."""
        for parts in (('tasks', name, 'retry_budget'), ('retry_budget',)):
            value = _read_budget(plan, *parts)
            if value is not None:
                return value
        return retry_failed

    def infra_budget() -> int:
        value = _read_budget(plan, 'infra_budget')
        return infra_retries if value is None else value

    def retryable(name: str) -> bool:
        root = plan / 'tasks' / name
        return (
            status(name) in ('FAIL', 'UNKNOWN')
            and retries.get(name, 0) < budget(name)
            # A task that produced no new work (empty diff, byte-identical retry) would
            # only repeat itself; its marker says so and it is left for a human.
            and not (root / 'noretry').exists()
            # The retry policy found no stronger model after repeated failures on one: a
            # split, or an explicit `retry --dwarf`, is the human's call. A pin is exactly
            # that call, made ahead of time: the next full retry applies it.
            and (not (root / 'escalation.stop').exists() or _pinned(plan, name))
        )

    def requeue(name: str) -> bool:
        """Prepare a retry exactly as `retry` does, then put the task at the queue head."""
        result = subprocess.run(['/bin/bash', script, '_prepare_retry', str(plan), name])
        if result.returncode:
            refused[name] = budget(name)
            return False
        refused.pop(name, None)
        pairs_cache.pop(name, None)   # the preparation may have escalated the implementer
        retries[name] = retries.get(name, 0) + 1
        _write_atomic(plan, str(retries[name]), 'tasks', name, 'retries.used')
        retried.add(name)
        if name not in pending:
            pending.insert(0, name)
        print('forge: retrying ' + name + ' (automatic retry ' + str(retries[name]) + '/'
              + str(budget(name)) + ')', file=sys.stderr, flush=True)
        return True

    def resume(name: str) -> bool:
        """Prepare an INFRA task's next entry. Not a retry, so nothing is counted, but a
        pending pin applies when that entry is a full implementer attempt. False when a
        claimed pin could not be put in place: the task then stays INFRA, as `retry` would."""
        if not _pinned(plan, name):
            return True
        result = subprocess.run(['/bin/bash', script, '_prepare_retry', str(plan), name, '--resume'])
        if result.returncode:
            held.add(name)
            print('forge: ' + name + ' is not resumed: its escalation pin could not be applied',
                  file=sys.stderr, flush=True)
            return False
        return True

    def echo_output(name: str) -> None:
        # task.out is opened in append mode, so a retried task's file keeps its earlier
        # attempts; print only what is new.
        try:
            data = (plan / 'tasks' / name / 'task.out').read_bytes()
        except OSError:
            return
        start = offsets.get(name, 0)
        offsets[name] = len(data)
        print(data[start:].decode('utf-8', 'replace'), end='', flush=True)

    def row_now(name: str) -> list:
        """The task's tasks.tsv row as it is now: an escalation rewrites its dwarf column."""
        try:
            for line in (plan / 'tasks.tsv').read_text().splitlines():
                columns = line.split('\t')
                if len(columns) == 7 and columns[0] == name:
                    return columns
        except OSError:
            pass
        return tasks[name]

    def next_pairs(name: str) -> list:
        """The pairs the task's next dispatch uses: QA only when its next entry is a QA-only
        resume (decided by forge-parallel.sh's own check), the implementer and QA otherwise."""
        if name not in pairs_cache:
            row = row_now(name)
            qa_only = (plan / 'tasks' / name / 'resume').exists() and subprocess.run(
                ['/bin/bash', script, '_qa_only', str(plan), name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            pairs_cache[name] = [spec_pair(registry, spec) for spec in ([row[5]] if qa_only else [row[4], row[5]])]
        return pairs_cache[name]

    def scope_held(name: str) -> bool:
        return any(holds(pause, next_pairs(name)) for pause in pauses.values())

    def live_pause(pause: dict) -> bool:
        """A pause still holding something: its own stopped tasks, or a pending task on its pair.
        One whose tasks all fell back to another model holds nothing."""
        return bool(pause['tasks']) or any(holds(pause, next_pairs(name)) for name in pending)

    def waiting_scopes() -> bool:
        return any(not pause['exhausted'] and live_pause(pause) for pause in pauses.values())

    def fall_back(name: str, info: dict, scope: str) -> None:
        """--infra-fallback: a dwarf-stage capacity stop moves to the next healthy model of its
        tier pool. The stop was already recorded as its scope's pause, so every paused or
        exhausted pair is excluded and a pool all at capacity cannot bounce the task forever."""
        if interrupted or graceful or fractal or _read_small(plan, 'infra_fallback') != 'on' or _pinned(plan, name):
            return
        if info.get('stage') != 'dwarf' or info.get('class') != 'rate_limit' or not scope.startswith('model:'):
            return
        excluded = {(info['harness'], info['model'])}
        excluded.update((p['harness'], p['model'] or '*') for p in pauses.values())
        command = ['/bin/bash', script, '_prepare_retry', str(plan), name, '--fallback',
                   ','.join(sorted(h + '/' + m for h, m in excluded))]
        if subprocess.run(command).returncode:
            return
        pauses[scope]['tasks'].remove(name)
        pairs_cache.pop(name, None)
        pending.insert(0, name)

    def write_paused(until_all: float | None = None) -> None:
        """infra.paused for forge-ui and `forge control show`; gone when nothing is paused."""
        nonlocal paused_text
        scopes = []
        if paused:
            scopes.append({'scope': 'all', 'class': all_class or None, 'harness': None, 'model': None,
                           'tasks': list(infra_seen), 'cycle': infra_cycles + (1 if until_all else 0),
                           'until': until_all, 'exhausted': not until_all and infra_cycles >= infra_budget()})
        for pause in pauses.values():
            scopes.append({key: pause[key] for key in
                           ('scope', 'class', 'harness', 'model', 'tasks', 'cycle', 'until', 'exhausted')})
        text = json.dumps({'scopes': scopes}) if scopes else None
        if text == paused_text:
            return
        if text is None:
            _remove(plan, 'infra.paused')
        else:
            _write_atomic(plan, text, 'infra.paused')
        paused_text = text

    def backoff() -> tuple:
        """The plan's infra_backoff, read at every wait so a change applies to the next one."""
        try:
            fd = os.open(_resolve(plan, 'infra_backoff'), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                text = os.read(fd, 4097).decode('ascii').strip()
            finally:
                os.close(fd)
        except FileNotFoundError:
            return default_backoff()
        except (OSError, UnicodeDecodeError) as error:
            text, problem = None, str(error)
        else:
            problem = None
        if text is not None:
            try:
                return parse_backoff(text), text
            except ValueError as error:
                problem = str(error)
        if not backoff_warned:
            backoff_warned.append(problem)
            _warn('infra_backoff ignored, using the default: ' + problem)
        return default_backoff()

    def start_wait(pause: dict, info: dict, name: str) -> None:
        """Begin the scope's next wait, or mark it exhausted when its budget is spent."""
        done, budget = scope_waits.get(pause['scope'], 0), infra_budget()
        where = pause['harness'] + ('/' + pause['model'] if pause['model'] else '')
        head = 'forge: infrastructure failure (' + pause['class'] + ') on ' + where + ' for ' + name
        if done >= budget:
            pause.update(cycle=done, until=None, exhausted=True)
            print(head + '; its waits are used up (' + str(done) + ' of ' + str(budget)
                  + '), so it stays paused; other tasks continue (no attempt spent)', file=sys.stderr, flush=True)
            return
        wait, source = infra_wait(info, done, backoff())
        pause.update(cycle=done + 1, until=time.time() + wait, exhausted=False)
        print(head + '; pausing it, waiting ' + ('%g' % wait) + 's before resuming (' + source + ', wait '
              + str(done + 1) + ' of ' + str(budget) + '); other tasks continue (no attempt spent)',
              file=sys.stderr, flush=True)

    def infra_stopped(name: str) -> None:
        nonlocal paused, all_class
        pairs_cache.pop(name, None)
        info = infra_info(plan / 'tasks' / name)
        scope, harness, model = infra_scope(info)
        if scope == 'all':
            paused = True
            all_class = all_class or info.get('class', '')
            if name not in infra_seen:
                infra_seen.append(name)
        elif scope in pauses:
            # A stop in a scope already paused joins its wait and is not counted again.
            if name not in pauses[scope]['tasks']:
                pauses[scope]['tasks'].append(name)
        else:
            pauses[scope] = {'scope': scope, 'class': info.get('class', ''), 'harness': harness,
                             'model': model if scope.startswith('model:') else None, 'tasks': [name],
                             'cycle': 0, 'until': None, 'exhausted': False, 'info': info}
            start_wait(pauses[scope], info, name)
        if scope != 'all':
            fall_back(name, info, scope)
        write_paused()

    def end_scoped_waits() -> None:
        """Resume the tasks of every scope whose wait is over; an exhausted scope whose
        budget was raised meanwhile starts its next wait."""
        for scope, pause in list(pauses.items()):
            if interrupted:
                return
            if pause['exhausted']:
                if scope_waits.get(scope, 0) < infra_budget():
                    start_wait(pause, pause['info'], ', '.join(pause['tasks']))
                continue
            if time.time() < pause['until']:
                continue
            scope_waits[scope] = scope_waits.get(scope, 0) + 1
            del pauses[scope]
            for name in reversed(pause['tasks']):
                pairs_cache.pop(name, None)
                if name not in pending and status(name) == 'INFRA' and resume(name):
                    pending.insert(0, name)
            print('forge: the pause on ' + scope.partition(':')[2] + ' is over'
                  + ('; resuming ' + ', '.join(pause['tasks']) + ' (no attempt spent)' if pause['tasks'] else ''),
                  file=sys.stderr, flush=True)
        write_paused()

    def apply_retry(request: dict) -> tuple:
        name, dwarf = request['task'], request.get('dwarf')
        if name not in tasks:
            return 'refused', 'this run does not know task ' + name
        if name in active:
            return 'refused', 'the task is running'
        if name in pending:
            return 'refused', 'the task is already waiting to run'
        state = status(name)
        if state == 'MERGED':
            return 'refused', 'the task is merged'
        if state == 'INFRA':
            return 'refused', 'an infrastructure stop is resumed by the scheduler itself'
        if state not in RETRYABLE:
            return 'refused', 'a ' + state + ' task is not retried'
        command = ['/bin/bash', script, '_prepare_retry', str(plan), name] + (['--dwarf', dwarf] if dwarf else [])
        rc = subprocess.run(command).returncode
        if rc == 3:
            try:
                why = (plan / 'tasks' / name / 'split.suggested').read_text().strip()
            except OSError:
                why = ''
            return 'refused', why or 'the retry policy stopped this task: split it or name a --dwarf'
        if rc:
            return 'refused', 'the retry could not be prepared (exit ' + str(rc) + '; see the run output)'
        # A human's retry: no automatic budget is spent, and a stop marker the
        # preparation cleared (an explicit --dwarf) no longer holds the task.
        pairs_cache.pop(name, None)
        refused.pop(name, None)
        retried.add(name)
        human.add(name)
        pending.insert(0, name)
        return 'applied', None

    def apply_add(request: dict) -> tuple:
        name = request['task']
        if name in tasks:
            return 'refused', 'the task is already in this run'
        try:
            rows = [line.split('\t') for line in (plan / 'tasks.tsv').read_text().splitlines()
                    if line and not line.startswith('#')]
        except OSError as error:
            return 'refused', 'tasks.tsv could not be read: ' + str(error)
        if any(len(row) != 7 for row in rows):
            return 'refused', 'tasks.tsv requires seven columns'
        if len({row[0] for row in rows}) != len(rows):
            return 'refused', 'tasks.tsv has duplicate task IDs'
        found = [row for row in rows if row[0] == name]
        if not found:
            return 'refused', 'the row is not in tasks.tsv'
        deps = [d for d in found[0][1].split(',') if d and d != '-']
        unknown = [d for d in deps if d not in tasks]
        if unknown:
            # Every known task was read at start, so a dep among them cannot close a cycle.
            return 'refused', 'it depends on task(s) this run does not know: ' + ', '.join(unknown)
        root = plan / 'tasks' / name
        try:
            root.mkdir(parents=True, exist_ok=True)
            if not stat.S_ISDIR(os.lstat(root).st_mode):
                return 'refused', 'tasks/' + name + ' is not a real directory'
        except OSError as error:
            return 'refused', 'tasks/' + name + ' could not be created: ' + str(error)
        tasks[name] = found[0]
        pending.append(name)
        if subprocess.run(['/bin/bash', script, '_waves', str(plan)]).returncode:
            _warn('waves.tsv was not refreshed for ' + name + '; the task still runs')
        return 'applied', None

    def is_fix(name: str) -> bool:
        """A finding sweep's fix task."""
        return (plan / 'tasks' / name / 'sweep.json').is_file()

    def sweep_due() -> bool:
        """--sweep was given, every task merged, nothing stops or holds the run, and this plan was
        never swept (sweep/plan.json is the planner's record, and its marker)."""
        return (bool(sweep) and not sweep_asked and not interrupted and not graceful and not paused
                and not drained(plan) and not (plan / 'sweep' / 'plan.json').exists()
                and all(status(name) == 'MERGED' for name in tasks))

    def start_sweep() -> int:
        """Plan the finding sweep and queue its fix tasks; how many were queued. Whatever goes
        wrong here ends the run without a sweep, never with a failure."""
        nonlocal sweep_asked
        sweep_asked = True
        command = ['/bin/bash', script, '_sweep', str(plan), '--severity', sweep, '--max', str(sweep_max)]
        result = subprocess.run(command + (['--plausible'] if sweep_plausible else []),
                                stdout=subprocess.PIPE, text=True)
        if result.returncode:
            _warn('the finding sweep could not be planned (exit ' + str(result.returncode)
                  + '); its findings stay for forge issues')
            return 0
        queued = 0
        for name in result.stdout.split():
            outcome, reason = apply_add({'task': name})
            if outcome == 'applied':
                queued += 1
            else:
                _warn('sweep task ' + name + ' was not queued: ' + str(reason))
        return queued

    def handle_requests() -> None:
        """Apply or refuse each queued request; the outcome is logged under the control lock."""
        for name in list_requests(plan):
            request = read_request(plan, name)
            if request is None:
                outcome, reason = 'refused', 'not a valid request'
            elif interrupted or graceful:
                outcome, reason = 'refused', 'run is stopping'
            elif request['verb'] == 'retry':
                outcome, reason = apply_retry(request)
            else:
                outcome, reason = apply_add(request)
            with _control_lock(plan):
                settle_request(plan, name, request, outcome, reason)

    def reap_exited(into: set[str] | None = None, *, allow_requeue: bool = True) -> None:
        """Reap every active task whose process has already exited.

        Merging is serial and can take long enough for another task to finish, so
        repeat until a pass finds nothing. PASS is merged and a process that died
        still RUNNING becomes ERROR. Once `interrupted` is set, every other status
        is kept and nothing is requeued: the next `run --retry-failed` decides.
        `finally` passes allow_requeue=False for the same reason. It also runs when
        the loop raises, and a retry prepared on the way out would clear a recorded
        failure that nothing is left to dispatch.
        """
        while True:
            reaped = False
            for name, (process, log) in list(active.items()):
                if process.poll() is None:
                    continue
                reaped = True
                if into is not None:
                    into.add(name)
                log.close()
                del active[name]
                if os.environ.get('FORGE_OUTPUT') == 'full':
                    echo_output(name)
                state = status(name)
                if state == 'PASS':
                    merge(name)
                elif state == 'RUNNING':
                    mark(name, 'ERROR')
                elif state == 'INFRA':
                    infra_stopped(name)
                elif allow_requeue and not interrupted and not graceful \
                        and state in ('FAIL', 'UNKNOWN') and retryable(name):
                    requeue(name)
            if not reaped:
                return

    def stop(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        live_start(plan, tasks, retry_failed, infra_retries)
        # Failures require an explicit retry unless this run asked for automatic ones.
        # Saved PASS results use the same fingerprint checks without paying for
        # implementation or QA again; an INFRA stop is not a failure of the work, so it
        # is simply dispatched again.
        # RUNNING at start means a scheduler was killed without its cleanup: its tasks'
        # harnesses may still be running and editing their worktrees.
        stop_harnesses(plan, [name for name in pending if status(name) == 'RUNNING'])
        for name in pending[:]:
            state = status(name)
            if state == 'PASS':
                merge(name)
            if state == 'RUNNING':
                mark(name, 'INTERRUPTED')
            if state in ('FAIL', 'UNKNOWN') and not interrupted and retryable(name):
                requeue(name)
            if state == 'INFRA' and not interrupted and not resume(name):
                pending.remove(name)
                continue
            if status(name) not in ('PENDING', 'BLOCKED', 'INFRA'):
                pending.remove(name)
        swept = time.monotonic()
        while pending or active or waiting_scopes() or sweep_due():
            # Reap before honouring the stop. A signal during the sleep, and a task
            # that exits while another merge is still running, have already written
            # a result; breaking first used to skip them and mark them INTERRUPTED.
            ended = set()
            # `stop --now` is SIGTERM by another route; a graceful stop lets the running finish.
            mode = stop_mode(plan)
            if mode == 'now':
                interrupted = True
            graceful = graceful or mode == 'graceful'
            reap_exited(ended)
            if interrupted:
                break
            draining = drained(plan)
            drain_told = drain_told and draining
            if time.monotonic() - swept >= SWEEP_EVERY:
                # A human can raise a budget after a task already ended FAIL while others
                # still run. The initial pass's predicate, for tasks nothing else will look
                # at again; one whose retry could not be prepared waits for a new budget.
                swept = time.monotonic()
                for name in tasks:
                    if name in pending or name in active or name in ended:
                        continue
                    if not interrupted and not graceful and status(name) in ('FAIL', 'UNKNOWN') \
                            and retryable(name) and refused.get(name) != budget(name):
                        requeue(name)
            if paused and not active and infra_seen and not graceful and infra_cycles < infra_budget():
                # Everything running has drained. Wait out the outage once for all the
                # tasks it stopped, then dispatch them again.
                shape = backoff()
                wait, source = max(infra_wait(infra_info(plan / 'tasks' / n), infra_cycles, shape) for n in infra_seen)
                infra_cycles += 1
                print('forge: infrastructure failure on ' + ', '.join(infra_seen)
                      + '; waiting ' + ('%g' % wait) + 's before resuming (' + source + ', wait '
                      + str(infra_cycles) + ' of ' + str(infra_budget()) + ') (no attempt spent)',
                      file=sys.stderr, flush=True)
                resume_at = time.time() + wait
                write_paused(resume_at)
                while time.time() < resume_at and not interrupted:
                    # A stop of either kind ends the wait: nothing is active, so it ends the run.
                    mode = stop_mode(plan)
                    if mode:
                        interrupted = interrupted or mode == 'now'
                        graceful = graceful or mode == 'graceful'
                        break
                    time.sleep(min(0.05, max(resume_at - time.time(), 0)))
                if interrupted or graceful:
                    if graceful:
                        print('forge: stopped by forge control stop; ' + str(len(pending) + len(infra_seen))
                              + ' task(s) not started', file=sys.stderr, flush=True)
                    break
                # A pin set before or during the wait applies to the entries that run the
                # implementer again; a QA-stage stop keeps it for the next full retry.
                for name in reversed(infra_seen):
                    if interrupted:
                        break
                    if name not in pending and resume(name):
                        pending.insert(0, name)
                infra_seen.clear()
                paused = False
                all_class = ''
                write_paused()
            end_scoped_waits()
            handle_requests()
            cap = live_capacity(plan, capacity)
            draining = drained(plan)   # again: the infrastructure wait above can last minutes
            for name in pending[:]:
                if paused or len(active) >= cap or interrupted or graceful or draining:
                    break
                deps = [d for d in tasks[name][1].split(',') if d and d != '-']
                if any(status(dep) != 'MERGED' for dep in deps):
                    continue
                if any(overlap(tasks[name][3], tasks[other][3]) for other in active):
                    continue
                if pauses and scope_held(name):
                    continue
                root = plan / 'tasks' / name
                if not (root / 'base_ref').exists():
                    worktree = Path((plan / 'wt_root').read_text().strip()) / '_integration'
                    base = subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'])
                    (root / 'base_ref').write_bytes(base)
                log = open(root / 'task.out', 'ab')
                # A retried task runs with the same environment `retry` gives it, so the
                # no-progress guard (byte-identical diff) applies under Fractal too.
                env = (dict(os.environ, FORGE_FRACTAL_RETRY='1',
                            FORGE_DATASET_TRIGGER='human' if name in human else 'auto')
                       if name in retried else None)
                human.discard(name)
                process = subprocess.Popen(
                    ['/bin/bash', script, '_task', str(plan), name],
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env,
                )
                active[name] = process, log
                pending.remove(name)
                print('forge: dispatched ' + name + ' -> ' + str(root), file=sys.stderr, flush=True)
            if not active and graceful:
                # Not BLOCKED: a stop is not a dependency failure, so the next run starts them.
                print('forge: stopped by forge control stop; ' + str(len(pending)) + ' task(s) not started',
                      file=sys.stderr, flush=True)
                break
            if not active and draining and (pending or waiting_scopes()):
                if not drain_told:
                    drain_told = True
                    print('forge: drained — ' + str(len(pending)) + ' task(s) waiting; forge control resume '
                          + str(plan) + ' continues', file=sys.stderr, flush=True)
            elif not active and not waiting_scopes():
                if paused or held or any(live_pause(pause) for pause in pauses.values()):
                    # Infrastructure stop: leave everything not yet run PENDING. Marking it
                    # BLOCKED would turn "the provider was down" into a recorded failure.
                    # A task held back by its pin is still an infrastructure stop.
                    break
                if not pending and sweep_due() and start_sweep():
                    continue
                for name in pending:
                    mark(name, 'BLOCKED')
                break
            time.sleep(0.05)
    finally:
        # Not live from here on: a late `forge control` must not edit budgets nobody reads.
        live_stop(plan, tasks)
        # Poll before any signal. A process that has already exited keeps the result
        # it wrote (PASS is merged); only a process that is still alive is stopped.
        # Never requeue here: this block also runs when the loop raises, and a retry
        # prepared then would erase the recorded failure and spend its budget.
        reap_exited(allow_requeue=False)
        stopping = list(active.items())
        for name, (process, log) in stopping:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
        # The group outlives its leader while dispatch's EXIT trap stops the harness, so wait
        # for the group to empty, not for the leader. Reaping the leader matters too: its
        # zombie would keep the group alive.
        deadline = time.monotonic() + interrupt_grace()
        waiting = {name for name, _ in stopping}
        while waiting:
            for name, (process, log) in stopping:
                if name in waiting:
                    process.poll()
                    if not _group_alive(process.pid):
                        waiting.discard(name)
            if not waiting or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        for name, (process, log) in stopping:
            if name in waiting:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            log.close()
        stop_harnesses(plan, [name for name, _ in stopping])
        for name, _ in stopping:
            try:
                mark(name, 'INTERRUPTED')
            except OSError as error:
                _warn('could not mark ' + name + ' INTERRUPTED: ' + str(error))
    if interrupted or (graceful and not all(status(name) == 'MERGED' for name in tasks)):
        return 130
    if paused or any(live_pause(pause) for pause in pauses.values()) \
            or any(status(name) == 'INFRA' for name in tasks):
        return INFRA_EXIT
    # A fix task that did not merge leaves its findings for `forge issues`; the run's own work is done.
    return 0 if all(status(name) == 'MERGED' for name in tasks if not is_fix(name)) else 5


def main() -> None:
    try:
        if sys.argv[1] == 'backoff-check':
            # The --infra-backoff flag's validation: one parser for every reader of the shape.
            try:
                parse_backoff(sys.argv[2] if len(sys.argv) > 2 else '')
            except ValueError as error:
                print('forge: --infra-backoff: ' + str(error), file=sys.stderr)
                sys.exit(2)
            sys.exit(0)
        if sys.argv[1] == 'lock':
            _, script, command, plan, *args = sys.argv[1:]
            # The shell inherits the lock for its entire operation, including
            # preflight, setup, scheduling, verification and cleanup.
            lock = open(Path(plan) / 'schedule.lock', 'a')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(lock.fileno(), True)
            env = dict(os.environ, FORGE_PLAN_LOCK=plan)
            os.execve('/bin/bash', ['/bin/bash', script, command, plan, *args], env)
        parser = argparse.ArgumentParser(prog='forge-schedule.py')
        parser.add_argument('script')
        parser.add_argument('plan')
        parser.add_argument('capacity', type=int)
        parser.add_argument('--retry-failed', type=int, default=0, metavar='N')
        parser.add_argument('--infra-retries', type=int, default=0, metavar='M')
        parser.add_argument('--sweep', choices=('P0', 'P1', 'P2', 'P3'))
        parser.add_argument('--sweep-max', type=int, default=5, metavar='N')
        parser.add_argument('--sweep-plausible', action='store_true')
        args = parser.parse_args(sys.argv[1:])
        sys.exit(schedule(args.script, args.plan, args.capacity, args.retry_failed,
                          args.infra_retries, args.sweep, args.sweep_max, args.sweep_plausible))
    except (OSError, ValueError, KeyError) as error:
        print('forge scheduler: ' + str(error), file=sys.stderr)
        sys.exit(3)


if __name__ == '__main__':
    main()
