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


def infra_wait(info: dict, cycle: int) -> float:
    """Seconds to wait before re-dispatching after an infrastructure failure."""
    try:
        base = float(os.environ.get('FORGE_INFRA_BACKOFF', '60'))
    except ValueError:
        base = 60.0
    try:
        hinted = float(info.get('retry_after', ''))
    except ValueError:
        hinted = None
    if hinted is not None and hinted >= 0:
        return min(hinted, INFRA_WAIT_CAP)
    return min(base * (2 ** cycle), max(base, 1800.0))


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


def _pinned(plan: object, name: str) -> bool:
    try:
        return os.path.lexists(_resolve(plan, 'tasks', name, 'escalation.pin'))
    except OSError:
        return False


def live_start(plan: object, names: object, retry_failed: int, infra_retries: int) -> None:
    """Publish the flags and that this scheduler is live. Stale per-task overrides and
    usage counters from an earlier invocation go first."""
    with _control_lock(plan):
        for name in _task_dirs(plan, names):
            _remove(plan, 'tasks', name, 'retry_budget')
            _remove(plan, 'tasks', name, 'retries.used')
        _write_atomic(plan, str(retry_failed), 'retry_budget')
        _write_atomic(plan, str(infra_retries), 'infra_budget')
        _write_atomic(plan, json.dumps({'pid': os.getpid(), 'started': time.time()}), 'scheduler.live')


def live_stop(plan: object, names: object) -> None:
    """Withdraw liveness first, then every budget. retries.used stays for the UI."""
    try:
        with _control_lock(plan):
            _remove(plan, 'scheduler.live')
            _remove(plan, 'retry_budget')
            _remove(plan, 'infra_budget')
            for name in _task_dirs(plan, names):
                _remove(plan, 'tasks', name, 'retry_budget')
    except Exception as error:
        _warn('could not withdraw the live control files: ' + str(error))


def schedule(script: str, plan: str, capacity: int, retry_failed: int = 0,
             infra_retries: int = 0) -> int:
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
    infra_cycles = 0   # how many times this run already waited out an infrastructure pause
    paused = False     # an infrastructure failure was seen: dispatch nothing new
    resume_at = 0.0

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
        retries[name] = retries.get(name, 0) + 1
        _write_atomic(plan, str(retries[name]), 'tasks', name, 'retries.used')
        retried.add(name)
        if name not in pending:
            pending.insert(0, name)
        print('forge: retrying ' + name + ' (automatic retry ' + str(retries[name]) + '/'
              + str(budget(name)) + ')', file=sys.stderr, flush=True)
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
        for name in pending[:]:
            state = status(name)
            if state == 'PASS':
                merge(name)
            if state == 'RUNNING':
                mark(name, 'INTERRUPTED')
            if state in ('FAIL', 'UNKNOWN') and retryable(name):
                requeue(name)
            if status(name) not in ('PENDING', 'BLOCKED', 'INFRA'):
                pending.remove(name)
        swept = time.monotonic()
        while pending or active:
            if interrupted:
                break
            ended = set()
            for name, (process, log) in list(active.items()):
                if process.poll() is not None:
                    ended.add(name)
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
                        paused = True
                        if name not in infra_seen:
                            infra_seen.append(name)
                    elif state in ('FAIL', 'UNKNOWN') and retryable(name):
                        requeue(name)
            if time.monotonic() - swept >= SWEEP_EVERY:
                # A human can raise a budget after a task already ended FAIL while others
                # still run. The initial pass's predicate, for tasks nothing else will look
                # at again; one whose retry could not be prepared waits for a new budget.
                swept = time.monotonic()
                for name in tasks:
                    if name in pending or name in active or name in ended:
                        continue
                    if status(name) in ('FAIL', 'UNKNOWN') and retryable(name) \
                            and refused.get(name) != budget(name):
                        requeue(name)
            if paused and not active and infra_seen and infra_cycles < infra_budget():
                # Everything running has drained. Wait out the outage once for all the
                # tasks it stopped, then dispatch them again.
                wait = max(infra_wait(infra_info(plan / 'tasks' / n), infra_cycles) for n in infra_seen)
                infra_cycles += 1
                print('forge: infrastructure failure on ' + ', '.join(infra_seen)
                      + '; waiting ' + ('%g' % wait) + 's before resuming (no attempt spent)',
                      file=sys.stderr, flush=True)
                resume_at = time.time() + wait
                while time.time() < resume_at and not interrupted:
                    time.sleep(min(0.05, max(resume_at - time.time(), 0)))
                if interrupted:
                    break
                for name in reversed(infra_seen):
                    if name not in pending:
                        pending.insert(0, name)
                infra_seen.clear()
                paused = False
            for name in pending[:]:
                if paused or len(active) >= capacity or interrupted:
                    break
                deps = [d for d in tasks[name][1].split(',') if d and d != '-']
                if any(status(dep) != 'MERGED' for dep in deps):
                    continue
                if any(overlap(tasks[name][3], tasks[other][3]) for other in active):
                    continue
                root = plan / 'tasks' / name
                if not (root / 'base_ref').exists():
                    worktree = Path((plan / 'wt_root').read_text().strip()) / '_integration'
                    base = subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'])
                    (root / 'base_ref').write_bytes(base)
                log = open(root / 'task.out', 'ab')
                # A retried task runs with the same environment `retry` gives it, so the
                # no-progress guard (byte-identical diff) applies under Fractal too.
                env = (dict(os.environ, FORGE_FRACTAL_RETRY='1', FORGE_DATASET_TRIGGER='auto')
                       if name in retried else None)
                process = subprocess.Popen(
                    ['/bin/bash', script, '_task', str(plan), name],
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=env,
                )
                active[name] = process, log
                pending.remove(name)
                print('forge: dispatched ' + name + ' -> ' + str(root), file=sys.stderr, flush=True)
            if not active:
                if paused:
                    # Infrastructure stop: leave everything not yet run PENDING. Marking it
                    # BLOCKED would turn "the provider was down" into a recorded failure.
                    break
                for name in pending:
                    mark(name, 'BLOCKED')
                break
            time.sleep(0.05)
    finally:
        # Not live from here on: a late `forge control` must not edit budgets nobody reads.
        live_stop(plan, tasks)
        for process, log in active.values():
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for name, (process, log) in active.items():
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            # Remove descendants even if the shell exited before its backend.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            log.close()
            mark(name, 'INTERRUPTED')
    if interrupted:
        return 130
    if paused or any(status(name) == 'INFRA' for name in tasks):
        return INFRA_EXIT
    return 0 if all(status(name) == 'MERGED' for name in tasks) else 5


def main() -> None:
    try:
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
        args = parser.parse_args(sys.argv[1:])
        sys.exit(schedule(args.script, args.plan, args.capacity, args.retry_failed,
                          args.infra_retries))
    except (OSError, ValueError, KeyError) as error:
        print('forge scheduler: ' + str(error), file=sys.stderr)
        sys.exit(3)


if __name__ == '__main__':
    main()
