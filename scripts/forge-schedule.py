#!/usr/bin/env python3
"""Readiness scheduler. Only the coordinator merges; tasks keep pinned baselines."""
from __future__ import annotations

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


def schedule(script: str, plan: str, capacity: int) -> int:
    if capacity <= 0:
        raise ValueError('capacity must be positive')
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

    def stop(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        # Failures require explicit retry. Saved PASS results use the same
        # fingerprint checks without paying for implementation or QA again.
        for name in pending[:]:
            state = status(name)
            if state == 'PASS':
                merge(name)
            if state == 'RUNNING':
                mark(name, 'INTERRUPTED')
            if status(name) not in ('PENDING', 'BLOCKED'):
                pending.remove(name)
        while pending or active:
            if interrupted:
                break
            for name, (process, log) in list(active.items()):
                if process.poll() is not None:
                    log.close()
                    del active[name]
                    if os.environ.get('FORGE_OUTPUT') == 'full':
                        print((plan / 'tasks' / name / 'task.out').read_text(), end='', flush=True)
                    if status(name) == 'PASS':
                        merge(name)
                    elif status(name) == 'RUNNING':
                        mark(name, 'ERROR')
            for name in pending[:]:
                if len(active) >= capacity or interrupted:
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
                process = subprocess.Popen(
                    ['/bin/bash', script, '_task', str(plan), name],
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                )
                active[name] = process, log
                pending.remove(name)
                print('forge: dispatched ' + name + ' -> ' + str(root), file=sys.stderr, flush=True)
            if not active:
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
        script, plan, capacity = sys.argv[1:]
        sys.exit(schedule(script, plan, int(capacity)))
    except (OSError, ValueError, KeyError) as error:
        print('forge scheduler: ' + str(error), file=sys.stderr)
        sys.exit(3)


if __name__ == '__main__':
    main()
