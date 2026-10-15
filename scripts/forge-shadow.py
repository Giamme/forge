#!/usr/bin/env python3
"""A shadow review: a second, independent review of a sampled task attempt, never consumed.

Nothing in forge has ever reviewed one diff twice, so how often two reviewers agree
-- the noise in every review label -- is unknown. `plan --shadow-qa <spec>[,<spec>...]`
turns this on for a sampled share of reviewed attempts (`--shadow-rate`, default 0.1).

  forge-shadow.py run --plan P --task ID --worktree W --base B --reviewed R --attempt N --qa SPEC [--yolo]
      Started in the background by the task, just before the primary review. Exits at once
      unless this attempt is sampled. Otherwise it builds its own review snapshot and run
      dir under <plan>/shadow/<id>/<attempt>-<k>/, reviews a byte-identical copy of the
      primary's qa.input, and returns. Never exits nonzero.
  forge-shadow.py preflight --plan P
      Which listed shadow specs resolve here; writes <plan>/shadow_qa.ok. Never fails a run.

Guarantees, because the task waits for this process before it writes any status:
  - nothing reads the shadow's result in the run path; it changes no status, memory,
    ledger, gate or retry, and its files never share a name or a dir with the primary's
  - it holds no inherited file descriptor (close_fds), so no lock outlives the task
  - it dies with the task: when its parent goes away (an interrupt, a killed scheduler,
    Ctrl-C on a manual retry, which never reaches a background job) it stops the review
  - it is bounded: once the task signals that the primary review finished (SIGUSR1), the
    shadow gets as long again as the primary took, at least SHADOW_GRACE_S, then stops
<shadow dir>/shadow.status (and a `shadow_review` dataset event) says how it ended: done,
timeout, error, invalid (the reviewer changed its snapshot), overtime, stopped, orphaned.
Only a `done` review counts as a second opinion.
Forge never picks a model: the reviewer is one the user listed, preferring a different
model family from the primary reviewer's, so two reviewers do not share one blind spot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
DISPATCH = SCRIPTS / 'forge-dispatch.sh'
ARTIFACT = SCRIPTS / 'forge-artifact.sh'
SHADOW_GRACE_S = float(os.environ.get('FORGE_SHADOW_GRACE_S') or 300)
STOP_WAIT_S = 10
PREPARE_TIMEOUT_S = 120


def rate_of(plan: Path) -> float:
    try:
        rate = float((plan / 'shadow_rate').read_text().strip())
    except (OSError, ValueError):
        rate = 0.1
    return min(max(rate, 0.0), 1.0)


def sampled(plan: Path, task: str, attempt: str, rate: float) -> bool:
    """Deterministic per attempt: a QA-only resume reaches the same answer."""
    run = ''
    for name in ('dataset.uid', 'run_id'):
        try:
            run = (plan / name).read_text().strip()
            break
        except OSError:
            continue
    digest = hashlib.sha256(('%s:%s:%s' % (run, task, attempt)).encode()).digest()
    return int.from_bytes(digest[:8], 'big') < rate * 2 ** 64


def family(model: str) -> str:
    m = (model or '').lower().rsplit('/', 1)[-1]
    if m.startswith(('claude', 'opus', 'sonnet', 'haiku', 'fable')):
        return 'claude'
    if m.startswith(('gpt', 'sol', 'astra', 'luna', 'terra', 'o1', 'o3', 'o4')) or 'gpt-' in m:
        return 'gpt'
    if m.startswith('gemini'):
        return 'gemini'
    return m


def resolved_model(spec: str) -> str | None:
    """The model a qa dispatch of `spec` would use, by dispatch's own dry run."""
    with tempfile.TemporaryDirectory(prefix='forge-shadow-resolve-') as tmp:
        prompt = Path(tmp) / 'input'
        prompt.write_text('Offline resolution')
        try:
            out = subprocess.run(['/bin/bash', str(DISPATCH), 'qa', spec, '--run-dir', tmp, '--prompt-file',
                                  str(prompt), '--dry-run'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 timeout=60, text=True)
        except (OSError, subprocess.SubprocessError):
            return None
    fields = dict(line.split('=', 1) for line in out.stdout.splitlines() if '=' in line)
    return fields.get('model') or None


def candidates(plan: Path) -> list:
    path = plan / 'shadow_qa.ok' if (plan / 'shadow_qa.ok').exists() else plan / 'shadow_qa'
    try:
        text = path.read_text()
    except OSError:
        return []
    return [s.strip() for s in text.replace('\n', ',').split(',') if s.strip()]


def pick(listed: list, primary: str) -> tuple[str | None, str | None]:
    """(spec, why): the first listed spec of another family than the primary's, else the first."""
    if not listed:
        return None, None
    mine = family(resolved_model(primary) or primary)
    for spec in listed:
        if family(resolved_model(spec) or spec) != mine:
            return spec, 'other_family'
    return listed[0], 'same_family'


def fresh_dir(base: Path, attempt: str) -> Path:
    """A dir no earlier try used: a snapshot refuses an existing destination."""
    base.mkdir(parents=True, exist_ok=True)
    k = 1
    while True:
        candidate = base / ('%s-%d' % (attempt, k))
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            k += 1


def bash(function: str, *args) -> subprocess.CompletedProcess:
    return subprocess.run(['/bin/bash', '-c', 'source "$1"; shift; "$@"', '_', str(ARTIFACT), function, *args],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=PREPARE_TIMEOUT_S)


def write(path: Path, text: str) -> None:
    try:
        path.write_text(text)
    except OSError:
        pass


def dataset_event(out: Path, task: str, values: dict) -> None:
    """How the shadow ended, in the run dataset. Its dispatch was captured when it exited,
    before the snapshot was checked again, so an `invalid` verdict can only be told here."""
    try:
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        from forge_dataset.capture import point
        point('shadow_review', str(out), task, values)
    except Exception:
        pass


class Stopped(Exception):
    pass


STATE = dict(child=None, primary_done=None, stop=False, started=time.time())


def _primary_done(_signum, _frame):
    if STATE['primary_done'] is None:
        STATE['primary_done'] = time.time()


def _stop(_signum, _frame):
    # Before the review starts there is nothing to stop but this process; afterwards the
    # watch loop stops the review itself, so the status file is always written.
    if STATE['child'] is None:
        raise Stopped()
    STATE['stop'] = True


def run(args) -> int:
    # First, before anything slow: the task may signal that its review finished at any time,
    # and SIGUSR1's default action would kill a launcher that is still preparing.
    signal.signal(signal.SIGUSR1, _primary_done)
    signal.signal(signal.SIGTERM, _stop)
    plan = Path(args.plan)
    parent = os.getppid()
    status, out, spec, why = 'skipped', None, None, None
    try:
        if os.environ.get('FORGE_FRACTAL_RUN') or not sampled(plan, args.task, args.attempt, rate_of(plan)):
            return 0
        spec, why = pick(candidates(plan), args.qa)
        if not spec:
            return 0
        out = fresh_dir(plan / 'shadow' / args.task, args.attempt)
        meta = dict(task=args.task, attempt=args.attempt, reviewed=args.reviewed, primary=args.qa, spec=spec,
                    pick=why, started=time.time())
        write(out / 'shadow.json', json.dumps(meta, indent=1))
        review = out / 'review-snapshot'        # review-*: the importer never walks into a snapshot
        if bash('forge_review_snapshot', args.worktree, args.base, args.reviewed, str(review)).returncode:
            status = 'error'
            return 0
        before = bash('forge_fingerprint', str(review)).stdout.strip()
        shutil.copyfile(Path(args.tdir) / 'qa.input', out / 'qa.input')
        env = dict(os.environ, FORGE_SHADOW='1', FORGE_SHADOW_ATTEMPT=args.attempt,
                   FORGE_SHADOW_REVIEWED=args.reviewed, FORGE_SHADOW_FROM=args.qa)
        argv = ['/bin/bash', str(DISPATCH), 'qa', spec, '--repo', str(review), '--run-dir', str(out),
                '--review-base', (out / 'review.base').read_text().strip(),
                '--ripwire-query-file', str(Path(args.tdir) / 'prompt.md'), '--prompt-file', str(out / 'qa.input'),
                '--output', 'summary']
        if args.yolo:
            argv.append('--yolo')
        with open(out / 'shadow.out', 'wb') as log:
            STATE['child'] = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                              env=env, close_fds=True)
        status = watch(STATE['child'], parent)
        after = bash('forge_fingerprint', str(review)).stdout.strip()
        if status == 'done' and (not before or after != before):
            status = 'invalid'          # the reviewer changed its snapshot: its verdict counts for nothing
    except Stopped:
        status = 'stopped'
    except Exception as error:          # a shadow never fails the task
        status = 'error'
        if out is not None:
            write(out / 'shadow.error', '%s: %s\n' % (type(error).__name__, error))
    finally:
        if out is not None:
            write(out / 'shadow.status', status + '\n')
            dataset_event(out, args.task, dict(status=status, attempt=args.attempt, reviewed=args.reviewed,
                                               primary=args.qa, spec=spec, pick=why))
    return 0


def grace_ends() -> float | None:
    """When the shadow must stop: as long again as the primary took, at least SHADOW_GRACE_S."""
    done = STATE['primary_done']
    if done is None:
        return None
    return done + max(SHADOW_GRACE_S, done - STATE['started'])


def watch(child: subprocess.Popen, parent: int) -> str:
    """Wait for the review; stop it when the task dies or stops it, or its grace runs out."""
    while True:
        try:
            child.wait(timeout=1)
            return 'done' if child.returncode == 0 else ('timeout' if child.returncode == 7 else 'error')
        except subprocess.TimeoutExpired:
            pass
        ends = grace_ends()
        if os.getppid() != parent:
            reason = 'orphaned'         # the task is gone: an interrupt, or a killed scheduler
        elif STATE['stop']:
            reason = 'stopped'
        elif ends is not None and time.time() >= ends:
            reason = 'overtime'
        else:
            continue
        child.terminate()               # dispatch's TERM trap reaps the harness's process group
        try:
            child.wait(timeout=STOP_WAIT_S)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        return reason


def preflight(args) -> int:
    plan = Path(args.plan)
    listed = [s.strip() for s in (plan / 'shadow_qa').read_text().replace('\n', ',').split(',') if s.strip()] \
        if (plan / 'shadow_qa').exists() else []
    ok = []
    for spec in listed:
        flags = ['--yolo'] if (plan / 'yolo_qa').exists() else []
        try:
            result = subprocess.run(['/bin/bash', str(DISPATCH), 'doctor', '--spec', spec, '--role', 'qa', *flags],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
            good = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            good = False
        if good:
            ok.append(spec)
        else:
            print('forge: shadow review spec %s is not usable here; it is skipped' % spec, file=sys.stderr)
    write(plan / 'shadow_qa.ok', ','.join(ok) + '\n')
    if listed and not ok:
        print('forge: no shadow review spec is usable; shadow reviews are off for this run', file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge-shadow.py')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('run')
    for name in ('plan', 'task', 'worktree', 'base', 'reviewed', 'attempt', 'qa', 'tdir'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--yolo', action='store_true')
    p = sub.add_parser('preflight')
    p.add_argument('--plan', required=True)
    args = ap.parse_args(argv)
    try:
        return run(args) if args.command == 'run' else preflight(args)
    except Exception:
        return 0


if __name__ == '__main__':
    sys.exit(main())
