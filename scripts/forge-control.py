#!/usr/bin/env python3
"""forge control <command> <plan-dir> ...: show and change a plan's live controls.

  show  <plan> [--json]                          every control, per-task overrides, pins and the change log
  set   <plan> <key> <value> [--task ID]         qa-threshold, timeout, escalate-after, verify-retries,
                                                 retry-budget, infra-retries
  pin   <plan> <task> <spec>                     the model the task's next FULL implementer retry uses
  unpin <plan> <task>

`show` reports escalate-after and verify-retries at their defaults (3 and 1) when unset, and the two
budgets only while a scheduler is live.

Every change is validated, written atomically under the plan's control lock (`control.lock`),
appended to `control.log` and recorded as a dataset `control` event. This is the only writer of
human changes to a running plan; forge-ui calls it and never writes run files itself. Some
controls can only change while a scheduler is live (`scheduler.live` names a live pid):
retry-budget and infra-retries. A task-level change or a pin on a MERGED task, and a pin on a
Fractal plan, are refused.

A pin is checked with `forge-dispatch.sh doctor --spec <spec> --role dwarf`, which spends no
quota, before the lock is taken. FORGE_CONTROL_DOCTOR replaces `bash forge-dispatch.sh doctor`
with another command line, which then gets `--spec <spec> --role dwarf`; tests use it.

Exit codes: 0 ok, 2 usage or invalid input (including an unsafe path), 3 refused (not live, MERGED,
Fractal, a spec that fails doctor, the control lock busy for over 5 s).
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time

SKILL = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LOCK_WAIT = 5.0
LOG_SHOWN = 20
LOG_TAIL = 256 * 1024
READ_MAX = 8 * 1024 * 1024
DEFAULT_ESCALATE_AFTER = 3   # forge-route.py
DEFAULT_VERIFY_RETRIES = 1   # forge-parallel.sh
LEVELS = ('P0', 'P1', 'P2', 'P3')
STOP_MARKERS = ('escalation.stop', 'split.suggested', 'escalation.next')
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

NEXT_ATTEMPT = "at the task's next attempt"
NEXT_DECISION = 'at the next retry decision'
NEXT_RETRY = 'at the next full retry (not QA-only resumes)'
NEXT_VERIFY = 'at the next verification'


class Invalid(Exception):
    """Usage or invalid input, an unsafe path included: exit 2."""


class Refused(Exception):
    """A well-formed change the plan's state does not allow: exit 3."""


# ------------------------------------------------------------------------------------ path safety
# Every access walks down from the canonical plan dir one component at a time with O_NOFOLLOW, so
# the parent is a real directory inside the plan by construction, and the leaf is checked to be a
# regular file (or absent). Writes go to a temp file in the same dir and replace the target.

def valid_task_id(name):
    """The scheduler's rule (forge-schedule.py), applied whether or not the id is in tasks.tsv."""
    return bool(name) and name not in ('.', '..') and '/' not in name and '\0' not in name


def _enter(fd, part, create, rel):
    try:
        return os.open(part, DIR_FLAGS, dir_fd=fd)
    except FileNotFoundError:
        if not create:
            return None
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Invalid(f'{rel}: {part} is not a real directory')
        raise
    try:
        os.mkdir(part, dir_fd=fd)
    except FileExistsError:
        pass
    try:
        return os.open(part, DIR_FLAGS, dir_fd=fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Invalid(f'{rel}: {part} is not a real directory')
        raise


def _open_parent(plan, rel, create=False):
    """(fd of rel's parent dir, leaf name); (None, leaf) when a parent does not exist and create is off."""
    *dirs, leaf = rel.split('/')
    fd = os.open(plan, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in dirs:
            nxt = _enter(fd, part, create, rel)
            os.close(fd)
            fd = nxt
            if fd is None:
                return None, leaf
        return fd, leaf
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise


def _regular(fd, leaf, rel):
    """The lstat of an existing regular file, None when absent; anything else is unsafe."""
    try:
        st = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise Invalid(f'{rel} is not a regular file')
    return st


def probe(plan, rel):
    """Whether plan/rel exists as a safe regular file; Invalid when its path or type is unsafe."""
    fd, leaf = _open_parent(plan, rel)
    if fd is None:
        return False
    try:
        return _regular(fd, leaf, rel) is not None
    finally:
        os.close(fd)


def _read(plan, rel, tail):
    fd, leaf = _open_parent(plan, rel)
    if fd is None:
        return None
    try:
        try:
            rfd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise Invalid(f'{rel} is not a regular file')
            raise
        try:
            st = os.fstat(rfd)
            if not stat.S_ISREG(st.st_mode):
                raise Invalid(f'{rel} is not a regular file')
            cut = bool(tail) and st.st_size > tail
            if cut:
                os.lseek(rfd, st.st_size - tail, os.SEEK_SET)
            data = os.read(rfd, tail if cut else READ_MAX)
        finally:
            os.close(rfd)
    finally:
        os.close(fd)
    if cut:
        data = data.partition(b'\n')[2]   # the first line is cut in half
    return data.decode('utf-8', 'replace')


def read_file(plan, rel, strict=False, tail=0):
    """The file's text, None when absent. An unsafe path or type is Invalid when strict, else None."""
    try:
        return _read(plan, rel, tail)
    except Invalid:
        if strict:
            raise
        return None


def text_of(plan, rel, strict=False):
    """The file's stripped text; empty counts as unset."""
    return (read_file(plan, rel, strict) or '').strip() or None


def has(plan, rel):
    try:
        return probe(plan, rel)
    except Invalid:
        return False


def write_atomic(plan, rel, text):
    fd, leaf = _open_parent(plan, rel, create=True)
    tmp = f'.{leaf}.{os.getpid()}.{time.time_ns()}.tmp'
    try:
        _regular(fd, leaf, rel)
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd)
        try:
            os.write(tfd, text.encode('utf-8'))
        finally:
            os.close(tfd)
        os.replace(tmp, leaf, src_dir_fd=fd, dst_dir_fd=fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=fd)
        raise
    finally:
        os.close(fd)


def remove_safe(plan, rel):
    """Delete plan/rel if it is a safe regular file; whether anything was deleted."""
    fd, leaf = _open_parent(plan, rel)
    if fd is None:
        return False
    try:
        if _regular(fd, leaf, rel) is None:
            return False
        os.unlink(leaf, dir_fd=fd)
        return True
    except FileNotFoundError:
        return False
    finally:
        os.close(fd)


def append_line(plan, rel, line):
    fd, leaf = _open_parent(plan, rel)
    # A missing parent leaves no directory descriptor. Opening the leaf then would pass
    # dir_fd=None and create it in the process cwd, outside the plan, before the close failed.
    if fd is None:
        raise Invalid(f'{rel}: a parent directory is missing')
    try:
        _regular(fd, leaf, rel)
        afd = os.open(leaf, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(afd).st_mode):
                raise Invalid(f'{rel} is not a regular file')
            os.write(afd, line.encode('utf-8'))
        finally:
            os.close(afd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def control_lock(plan):
    """flock on control.lock, retried for LOCK_WAIT seconds. Never touches schedule.lock."""
    fd, leaf = _open_parent(plan, 'control.lock')
    try:
        _regular(fd, leaf, 'control.lock')   # a FIFO would block the open
        try:
            lock = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644, dir_fd=fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise Invalid('control.lock is not a regular file')
            raise
    finally:
        os.close(fd)
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
            if time.monotonic() >= deadline:
                raise Refused('another change holds the control lock (busy); try again')
            time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        os.close(lock)


# ------------------------------------------------------------------------------------ plan state

def load(plan_arg):
    """(canonical plan dir, tasks.tsv rows); Invalid when this is not a plan dir."""
    plan = os.path.realpath(plan_arg)
    text = read_file(plan, 'tasks.tsv', strict=True) if os.path.isdir(plan) else None
    if text is None:
        raise Invalid(f'{plan_arg}: not a forge plan (no tasks.tsv)')
    rows = [line.split('\t') for line in text.splitlines() if line.strip() and not line.startswith('#')]
    return plan, rows


def require_task(rows, task):
    if not valid_task_id(task):
        raise Invalid(f'invalid task id: {task!r}')
    if not any(row[0] == task for row in rows):
        raise Invalid(f'task {task!r} is not in this plan')


def is_live(plan):
    """scheduler.live names a live pid. Nothing probes schedule.lock: a probe could fail a starting run."""
    try:
        pid = json.loads(read_file(plan, 'scheduler.live') or '')['pid']
    except (ValueError, KeyError, TypeError):
        return False
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def is_fractal(plan):
    """fractal-selection.json present and not explicitly disabled (unreadable counts: fail closed)."""
    raw = read_file(plan, 'fractal-selection.json')
    if raw is None or not raw.strip():
        return False
    try:
        return bool(json.loads(raw).get('enabled'))
    except (ValueError, AttributeError):
        return True


def task_status(plan, task, strict=False):
    try:
        if probe(plan, f'tasks/{task}/merged'):
            return 'MERGED'
        words = (read_file(plan, f'tasks/{task}/status', strict) or '').split()
    except Invalid:
        if strict:
            raise
        return 'PENDING'
    return words[0] if words else 'PENDING'


def refuse_task(plan, task, pin=False):
    if task_status(plan, task, strict=True) == 'MERGED':
        raise Refused(f'task {task} is MERGED: nothing left to change')
    if pin and is_fractal(plan):
        raise Refused('this plan runs under Fractal, which never applies pins')


def number(text):
    return int(text) if text and re.fullmatch(r'[0-9]{1,9}', text) else None


def level(text, task_level=False):
    text = text.splitlines()[0].replace(' ', '') if text else ''
    return text if text in LEVELS or (task_level and text == 'none') else None


def nullable(value):
    return None if value in (None, '', '-', 'UNASSIGNED') else value


PAUSE_KEYS = ('scope', 'class', 'harness', 'model', 'tasks', 'cycle', 'until', 'exhausted')


def paused_scopes(text):
    """The scheduler's infra.paused scopes; [] when nothing is paused or the file is unreadable."""
    try:
        scopes = json.loads(text or '')['scopes']
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(scopes, list):
        return []
    return [{key: scope.get(key) for key in PAUSE_KEYS} for scope in scopes if isinstance(scope, dict)]


def show(plan, rows):
    live = is_live(plan)

    def get(rel):
        return text_of(plan, rel)

    def budget(rel):
        return number(get(rel)) if live else None

    run = dict(qa_threshold=level(get('qa_threshold')), timeout=number(get('timeout')),
               escalate_after=number(get('escalate_after')), verify_retries=number(get('verify_retries')),
               retry_budget=budget('retry_budget'), infra_budget=budget('infra_budget'))
    run['infra_paused'] = paused_scopes(get('infra.paused')) if live else None
    if run['escalate_after'] is None:
        run['escalate_after'] = DEFAULT_ESCALATE_AFTER
    if run['verify_retries'] is None:
        run['verify_retries'] = DEFAULT_VERIFY_RETRIES
    tasks = {}
    for row in rows:
        task = row[0]
        if not valid_task_id(task):
            continue
        base = f'tasks/{task}/'
        tasks[task] = dict(status=task_status(plan, task), dwarf=nullable(row[4] if len(row) > 4 else None),
                           qa=nullable(row[5] if len(row) > 5 else None),
                           qa_threshold=level(get(base + 'qa_threshold'), task_level=True),
                           retry_budget=number(get(base + 'retry_budget')),
                           retries_used=number(get(base + 'retries.used')) or 0,
                           pin=get(base + 'escalation.pin'), escalation_stop=has(plan, base + 'escalation.stop'),
                           escalation_next=get(base + 'escalation.next'))
    log = []
    for line in reversed((read_file(plan, 'control.log', tail=LOG_TAIL) or '').splitlines()):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            log.append(entry)
            if len(log) == LOG_SHOWN:
                break
    return dict(plan=plan, run_id=get('run_id') or os.path.basename(plan).removeprefix('forge-'),
                live=live, fractal=is_fractal(plan), run=run, tasks=tasks, log=log)


# ------------------------------------------------------------------------------------ changes

def _qa(value, task):
    if value in LEVELS or value == 'none' or (task and value == 'inherit'):
        return value
    raise Invalid('qa-threshold needs P0, P1, P2, P3 or none' + (' (or inherit for a task)' if task else ''))


def _int(value, task):
    if re.fullmatch(r'[0-9]{1,9}', value):
        return str(int(value))
    raise Invalid('needs a non-negative integer (up to 9 digits)')


def _budget(value, task):
    return value if task and value == 'inherit' else _int(value, task)


# key -> (plan only or both, validator, file, live only, when it applies)
KEYS = {
    'qa-threshold': ('both', _qa, 'qa_threshold', False, NEXT_ATTEMPT),
    'timeout': ('plan', _int, 'timeout', False, NEXT_ATTEMPT),
    'escalate-after': ('plan', _int, 'escalate_after', False, NEXT_DECISION),
    'verify-retries': ('plan', _int, 'verify_retries', False, NEXT_VERIFY),
    'retry-budget': ('both', _budget, 'retry_budget', True, NEXT_DECISION),
    'infra-retries': ('plan', _int, 'infra_budget', True, NEXT_DECISION),
}


def emit(plan, entry):
    """The dataset `control` event: the log line's fields, the same `at`. Best effort."""
    if os.environ.get('FORGE_DATASET', 'on').lower() in ('off', '0', 'no', 'false'):
        return
    cmd = [sys.executable, os.path.join(SKILL, 'scripts', 'forge-dataset.py'), 'event', 'control', '--dir', plan]
    if entry['task']:
        cmd += ['--task', entry['task']]
    for field in ('at', 'actor', 'action', 'key', 'value', 'previous'):
        value = entry[field]
        cmd += ['--set', f"{field}={'' if value is None else value}"]
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def audit(plan, actor, action, task, key, value, previous):
    entry = dict(at=time.time(), actor=actor, action=action, task=task, key=key, value=value, previous=previous)
    append_line(plan, 'control.log', json.dumps(entry) + '\n')
    emit(plan, entry)
    return entry


def change(args, plan, rows, action, task, key, work, applies, summary, live_only=False, pin_guard=False):
    """Run work() -> (value, previous, changed) under the control lock, audit a change, report the result."""
    with control_lock(plan):
        probe(plan, 'control.log')   # an unsafe log refuses the change instead of leaving it unaudited
        if live_only and not is_live(plan):
            raise Refused(f'{key} can only change while a run is live (no live scheduler for this plan)')
        if task:
            refuse_task(plan, task, pin_guard)
        value, previous, changed = work()
        if changed:   # setting a value it already has, or unpinning nothing, is not a change
            audit(plan, args.actor, action, task, key, value, previous)
    controls = show(plan, rows)
    if args.json:
        record = dict(action=action, task=task, key=key, value=value, previous=previous)
        json.dump(dict(ok=True, changed=record, applies=applies, controls=controls), sys.stdout, indent=2)
        sys.stdout.write('\n')
    else:
        print(f"{summary(value)} (was {'unset' if previous is None else previous}); applies {applies}"
              + ('' if changed else ' (nothing changed, not logged)'))
    return 0


def cmd_set(args):
    plan, rows = load(args.plan)
    scope, check, name, live_only, applies = KEYS[args.key]
    task = args.task
    if task is not None:
        require_task(rows, task)
        if scope != 'both':
            raise Invalid(f'{args.key} applies to the whole plan, not to a task')
    try:
        value = check(args.value, task)
    except Invalid as error:
        raise Invalid(f'{args.key}: {error}')
    rel = f'tasks/{task}/{name}' if task else name
    drop = value == 'inherit' or (value == 'none' and not task)
    wide = args.key == 'escalate-after' and value == '0'
    markers = [f'tasks/{row[0]}/{m}' for row in rows if valid_task_id(row[0]) for m in STOP_MARKERS] if wide else []

    def work():
        previous = text_of(plan, rel, strict=True)
        for target in markers:   # all checked before the first change: nothing is half done
            probe(plan, target)
        if drop:
            changed = remove_safe(plan, rel)
        else:
            write_atomic(plan, rel, value + '\n')
            changed = previous != value
        swept = [remove_safe(plan, target) for target in markers]   # as `run --escalate-after 0`: off means off
        return value, previous, changed or any(swept)

    where = f'task {task}' if task else 'the plan'
    return change(args, plan, rows, 'set', task, args.key, work, applies,
                  lambda v: f'set {args.key} {v} on {where}', live_only=live_only)


def doctor(spec):
    """Offline capability check of a dwarf spec: Refused when it fails, no quota spent."""
    override = os.environ.get('FORGE_CONTROL_DOCTOR')
    base = shlex.split(override) if override else ['bash', os.path.join(SKILL, 'scripts', 'forge-dispatch.sh'), 'doctor']
    try:
        done = subprocess.run(base + ['--spec', spec, '--role', 'dwarf'], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise Refused(f'could not check the spec: {error}')
    if done.returncode != 0:
        lines = [x.strip() for x in (done.stderr + done.stdout).splitlines() if x.strip()]
        raise Refused(f'spec {spec!r} does not pass the doctor check' + (f': {lines[0]}' if lines else ''))


def cmd_pin(args):
    plan, rows = load(args.plan)
    require_task(rows, args.task)
    spec = args.spec
    if not spec or re.search(r'[\s\x00-\x1f\x7f]', spec):
        raise Invalid('a pin is one dwarf spec such as sol:high:codex, without whitespace')
    refuse_task(plan, args.task, pin=True)
    doctor(spec)   # before the lock: it takes a moment, and the lock is for check-and-write only
    rel = f'tasks/{args.task}/escalation.pin'

    def work():
        previous = text_of(plan, rel, strict=True)
        write_atomic(plan, rel, spec + '\n')
        return spec, previous, previous != spec

    return change(args, plan, rows, 'pin', args.task, 'pin', work, NEXT_RETRY,
                  lambda v: f'pinned task {args.task} to {v}', pin_guard=True)


def cmd_unpin(args):
    plan, rows = load(args.plan)
    require_task(rows, args.task)
    rel = f'tasks/{args.task}/escalation.pin'

    def work():
        previous = text_of(plan, rel, strict=True)
        remove_safe(plan, rel)
        return None, previous, previous is not None

    return change(args, plan, rows, 'unpin', args.task, 'pin', work, NEXT_RETRY,
                  lambda v: f'unpinned task {args.task}')


# ------------------------------------------------------------------------------------ show

def stamp(epoch):
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))
    except (TypeError, ValueError, OverflowError, OSError):
        return '?'


def cmd_show(args):
    plan, rows = load(args.plan)
    controls = show(plan, rows)
    if args.json:
        json.dump(controls, sys.stdout, indent=2)
        sys.stdout.write('\n')
        return 0
    cell = lambda v: '-' if v is None else str(v)  # noqa: E731
    run = controls['run']
    print(f"{plan}  run {cell(controls['run_id'])}  live {'yes' if controls['live'] else 'no'}"
          f"  fractal {'yes' if controls['fractal'] else 'no'}")
    print('  ' + '  '.join(f"{k.replace('_', '-')} {cell(v)}" for k, v in run.items() if k != 'infra_paused'))
    for p in run['infra_paused'] or []:
        tasks = ','.join(t for t in (p['tasks'] or []) if isinstance(t, str)) or '-'
        if p['exhausted']:
            when = 'waits used up'
        elif isinstance(p['until'], (int, float)):
            when = f"wait {cell(p['cycle'])} until {stamp(p['until'])}"
        else:
            when = 'waiting for running tasks to finish'
        print(f"  paused {p['scope']} ({cell(p['class'])})  tasks {tasks}  {when}")
    columns = ('task', 'status', 'dwarf', 'qa', 'threshold', 'budget', 'used', 'pin', 'stop', 'next')
    table = [columns]
    for task, t in controls['tasks'].items():
        table.append((task, t['status'], cell(t['dwarf']), cell(t['qa']), cell(t['qa_threshold']), cell(t['retry_budget']),
                      cell(t['retries_used']), cell(t['pin']), 'yes' if t['escalation_stop'] else '-',
                      cell(t['escalation_next'])))
    widths = [max(len(r[i]) for r in table) for i in range(len(columns))]
    for r in table:
        print('  ' + '  '.join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    if controls['log']:
        print('changes, newest first:')
        for e in controls['log']:
            scope = f" {e['task']}" if e.get('task') else ''
            was = '' if e.get('previous') is None else f" (was {e['previous']})"
            print(f"  {stamp(e.get('at'))}  {e.get('actor')}  {e.get('action')} {e.get('key')}{scope}"
                  f" = {cell(e.get('value'))}{was}")
    return 0


# ------------------------------------------------------------------------------------ entry point

def parser():
    top = argparse.ArgumentParser(prog='forge control', description="Show and change a plan's live controls.")
    sub = top.add_subparsers(dest='command', required=True, metavar='{show,set,pin,unpin}')

    def common(p, mutator=True):
        p.add_argument('plan', metavar='PLAN_DIR', help="the plan dir (the one with tasks.tsv)")
        p.add_argument('--json', action='store_true', help='print JSON, for scripts and forge-ui')
        if mutator:
            p.add_argument('--actor', choices=('ui', 'cli'), default='cli', help='who is asking, for control.log (default cli)')

    p = sub.add_parser('show', help='every control, per-task overrides, pins and the change log')
    common(p, mutator=False)
    p.set_defaults(func=cmd_show)
    p = sub.add_parser('set', help='change a control', description='Keys: ' + ', '.join(KEYS) + '. '
                       'retry-budget and infra-retries change only while a run is live. '
                       'qa-threshold and retry-budget also take --task (value inherit drops the override).')
    common(p)
    p.add_argument('key', choices=list(KEYS))
    p.add_argument('value')
    p.add_argument('--task', metavar='ID', help='change this task only (qa-threshold, retry-budget)')
    p.set_defaults(func=cmd_set)
    p = sub.add_parser('pin', help="set the model a task's next full retry uses")
    common(p)
    p.add_argument('task')
    p.add_argument('spec', help='a dwarf spec, e.g. sol:high:codex')
    p.set_defaults(func=cmd_pin)
    p = sub.add_parser('unpin', help='drop a task pin')
    common(p)
    p.add_argument('task')
    p.set_defaults(func=cmd_unpin)
    return top


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except Invalid as error:
        print(f'forge control: {error}', file=sys.stderr)
        return 2
    except Refused as error:
        print(f'forge control: {error}', file=sys.stderr)
        return 3
    except OSError as error:
        print(f'forge control: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
