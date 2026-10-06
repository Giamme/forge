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
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import posixpath
import signal
import subprocess
import sys
import time


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
        if not name or name in ('.', '..') or '/' in name:
            raise ValueError('invalid task ID: ' + name)
        (plan / 'tasks' / name).mkdir(parents=True, exist_ok=True)
    active = {}
    pending = list(tasks)
    interrupted = False
    retries = {}       # automatic reviewer-driven retries used per task, this run
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

    def merge(name: str) -> None:
        result = subprocess.run(['/bin/bash', script, '_merge', str(plan), name])
        if result.returncode and status(name) == 'PASS':
            mark(name, 'ERROR')

    def retryable(name: str) -> bool:
        return (
            retry_failed > 0
            and retries.get(name, 0) < retry_failed
            and status(name) in ('FAIL', 'UNKNOWN')
            # A task that produced no new work (empty diff, byte-identical retry) would
            # only repeat itself; its marker says so and it is left for a human.
            and not (plan / 'tasks' / name / 'noretry').exists()
        )

    def requeue(name: str) -> bool:
        """Prepare a retry exactly as `retry` does, then put the task at the queue head."""
        result = subprocess.run(['/bin/bash', script, '_prepare_retry', str(plan), name])
        if result.returncode:
            return False
        retries[name] = retries.get(name, 0) + 1
        retried.add(name)
        if name not in pending:
            pending.insert(0, name)
        print('forge: retrying ' + name + ' (automatic retry ' + str(retries[name]) + '/'
              + str(retry_failed) + ')', file=sys.stderr, flush=True)
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
        while pending or active:
            if interrupted:
                break
            for name, (process, log) in list(active.items()):
                if process.poll() is not None:
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
            if paused and not active and infra_seen and infra_cycles < infra_retries:
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
                env = dict(os.environ, FORGE_FRACTAL_RETRY='1') if name in retried else None
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
