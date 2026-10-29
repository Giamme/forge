#!/usr/bin/env python3
"""forge control <command> <plan-dir> ...: show and change a plan's live controls.

  show  <plan> [--json]                          every control, per-task overrides, pins and the change log
  set   <plan> <key> <value> [--task ID]         qa-threshold, timeout, escalate-after, escalate-effort,
                                                 verify-retries, retry-budget, infra-retries, infra-backoff
                                                 (none drops it), max-parallel (1-64), pre-review-tests
                                                 (on/off), pre-review-parallel (1-64)
  pin   <plan> <task> <spec>                     the model the task's next FULL implementer retry uses
  unpin <plan> <task>
  retry <plan> <task> [--dwarf SPEC]             queue a retry of a stopped or failed task into the live run
  add   <plan> <task>                            queue a row added to tasks.tsv (with its prompt.md) into the live run
  drain <plan>                                   start nothing new; running tasks finish and the scheduler waits
                                                 while tasks remain (with none left to start, the run ends as usual)
  resume <plan>                                  end a drain
  stop  <plan> [--now]                           start nothing new and end the run once running tasks are done
                                                 (exit 130, no verification; when every task has merged by then,
                                                 the run ends as usual: it verifies and exits 0); --now interrupts
                                                 them like SIGTERM

`show` reports escalate-after and verify-retries at their defaults (3 and 1) when unset,
escalate-effort and pre-review-tests as on or off (off when the file is absent), pre-review-parallel
at its default (2) when unset, and the two budgets only while a scheduler is live.

Every change is validated, written atomically under the plan's control lock (`control.lock`),
appended to `control.log` and recorded as a dataset `control` event. This is the only writer of
human changes to a running plan; forge-ui calls it and never writes run files itself. Some
controls can only change while a scheduler is live (`scheduler.live` names a live pid):
retry-budget, infra-retries, max-parallel, drain, resume, stop, retry and add.

retry and add only queue a request in control.requests/ (one JSON file each): the scheduler
applies or refuses it at its next pass and logs the outcome in control.log, with the same
`request` id, as actor `scheduler`. A request never outlives the scheduler that was live
when it was queued. A task-level change or a pin on a MERGED task, and a pin on a
Fractal plan, are refused.

A pin, a retry's --dwarf and an added row's specs are checked with `forge-dispatch.sh doctor
--spec <spec> --role <role>`, which spends no quota, before the lock is taken.
FORGE_CONTROL_DOCTOR replaces `bash forge-dispatch.sh doctor` with another command line, which
then gets `--spec <spec> --role <role>`; tests use it.

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
DEFAULT_PRE_REVIEW_PARALLEL = 2   # forge-artifact.sh forge_pre_review
LEVELS = ('P0', 'P1', 'P2', 'P3')
STOP_MARKERS = ('escalation.stop', 'split.suggested', 'escalation.next')
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

NEXT_ATTEMPT = "at the task's next attempt"
NEXT_DECISION = 'at the next retry decision'
NEXT_RETRY = 'at the next full retry (not QA-only resumes)'
NEXT_VERIFY = 'at the next verification'
NEXT_INFRA_WAIT = 'at the next infrastructure wait'
NEXT_DISPATCH = "at the scheduler's next dispatch decision"


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


_CONTRACT = None


def run_threshold(text):
    """(effective level, 'set' | 'default') of the plan file's text; `none` means P3."""
    global _CONTRACT
    if _CONTRACT is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location('forge_contract', os.path.join(SKILL, 'scripts', 'forge-contract.py'))
        _CONTRACT = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_CONTRACT)
    value = (text.splitlines()[0].replace(' ', '').upper() if text else '')
    if value == 'NONE':
        return LEVELS[-1], 'set'
    if value in LEVELS:
        return value, 'set'
    return _CONTRACT.DEFAULT_THRESHOLD, 'default'


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

    threshold, source = run_threshold(get('qa_threshold'))
    run = dict(qa_threshold=threshold, qa_threshold_source=source, timeout=number(get('timeout')),
               escalate_after=number(get('escalate_after')),
               escalate_effort='on' if get('escalate_effort') == 'on' else 'off',
               verify_retries=number(get('verify_retries')),
               retry_budget=budget('retry_budget'), infra_budget=budget('infra_budget'))
    run['infra_backoff'] = get('infra_backoff') or default_backoff()
    parallel = number(get('max_parallel'))
    run['max_parallel'] = parallel if parallel and parallel <= 64 else None
    stopping = get('stop') if live else None
    run['drained'] = live and has(plan, 'drain')
    run['stopping'] = stopping if stopping in ('graceful', 'now') else None
    run['infra_paused'] = paused_scopes(get('infra.paused')) if live else None
    if run['escalate_after'] is None:
        run['escalate_after'] = DEFAULT_ESCALATE_AFTER
    if run['verify_retries'] is None:
        run['verify_retries'] = DEFAULT_VERIFY_RETRIES
    # Pre-review tests (#85): the plan file's presence turns them on (`plan --pre-review-tests`).
    run['pre_review_tests'] = 'on' if has(plan, 'pre_review_tests') else 'off'
    slots = number(get('pre_review_parallel'))
    run['pre_review_parallel'] = slots if slots and slots <= 64 else DEFAULT_PRE_REVIEW_PARALLEL
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
    if value in LEVELS or value == 'none' or (task and value == 'inherit') or (not task and value == 'default'):
        return value
    raise Invalid('qa-threshold needs P0, P1, P2, P3 or none (none means P3)'
                  + (' (or inherit to drop the override)' if task else ', or default to drop the file'))


def _int(value, task):
    if re.fullmatch(r'[0-9]{1,9}', value):
        return str(int(value))
    raise Invalid('needs a non-negative integer (up to 9 digits)')


_SCHEDULE = None


def schedule_module():
    """forge-schedule.py, loaded by path: the one owner of the backoff shape grammar."""
    global _SCHEDULE
    if _SCHEDULE is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location('forge_schedule', os.path.join(SKILL, 'scripts', 'forge-schedule.py'))
        _SCHEDULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SCHEDULE)
    return _SCHEDULE


def _backoff(value, task):
    if value == 'none':
        return value
    try:
        schedule_module().parse_backoff(value)
    except ValueError as error:
        raise Invalid(str(error).replace('backoff ', '', 1))
    return value.strip()


def default_backoff():
    return schedule_module().default_backoff()[1]


def _parallel(value, task):
    if re.fullmatch(r'[0-9]{1,2}', value) and 1 <= int(value) <= 64:
        return str(int(value))
    raise Invalid('needs an integer from 1 to 64')


def _budget(value, task):
    return value if task and value == 'inherit' else _int(value, task)


def _onoff(value, _task):
    if value in ('on', 'off'):
        return value
    raise Invalid('needs on or off')


# key -> (plan only or both, validator, file, live only, when it applies)
KEYS = {
    'qa-threshold': ('both', _qa, 'qa_threshold', False, NEXT_ATTEMPT),
    'timeout': ('plan', _int, 'timeout', False, NEXT_ATTEMPT),
    'escalate-after': ('plan', _int, 'escalate_after', False, NEXT_DECISION),
    'escalate-effort': ('plan', _onoff, 'escalate_effort', False, NEXT_DECISION),
    'verify-retries': ('plan', _int, 'verify_retries', False, NEXT_VERIFY),
    'retry-budget': ('both', _budget, 'retry_budget', True, NEXT_DECISION),
    'infra-retries': ('plan', _int, 'infra_budget', True, NEXT_DECISION),
    'infra-backoff': ('plan', _backoff, 'infra_backoff', False, NEXT_INFRA_WAIT),
    'max-parallel': ('plan', _parallel, 'max_parallel', True, NEXT_DISPATCH),
    'pre-review-tests': ('plan', _onoff, 'pre_review_tests', False, NEXT_ATTEMPT),
    'pre-review-parallel': ('plan', _parallel, 'pre_review_parallel', False, NEXT_ATTEMPT),
}


def emit(plan, entry):
    """The dataset `control` event: the log line's fields, the same `at`. Best effort."""
    if os.environ.get('FORGE_DATASET', 'on').lower() in ('off', '0', 'no', 'false'):
        return
    cmd = [sys.executable, os.path.join(SKILL, 'scripts', 'forge-dataset.py'), 'event', 'control', '--dir', plan]
    if entry['task']:
        cmd += ['--task', entry['task']]
    for field in ('at', 'actor', 'action', 'key', 'value', 'previous') + tuple(k for k in ('request',) if k in entry):
        value = entry[field]
        cmd += ['--set', f"{field}={'' if value is None else value}"]
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def audit(plan, actor, action, task, key, value, previous, **extra):
    entry = dict(at=time.time(), actor=actor, action=action, task=task, key=key, value=value, previous=previous,
                 **extra)
    append_line(plan, 'control.log', json.dumps(entry) + '\n')
    emit(plan, entry)
    return entry


def change(args, plan, rows, action, task, key, work, applies, summary, live_only=False, pin_guard=False):
    """Run work() -> (value, previous, changed) under the control lock, audit a change, report the result."""
    with control_lock(plan):
        probe(plan, 'control.log')   # an unsafe log refuses the change instead of leaving it unaudited
        if live_only and not is_live(plan):
            raise Refused(f'{key or action} can only change while a run is live (no live scheduler for this plan)')
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
        was = '' if key is None else f" (was {'unset' if previous is None else previous})"
        print(f"{summary(value)}{was}; applies {applies}" + ('' if changed else ' (nothing changed, not logged)'))
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
    drop = (value in ('inherit', 'default') or (args.key in ('escalate-effort', 'pre-review-tests') and value == 'off')
            or (args.key == 'infra-backoff' and value == 'none'))
    # Plan-wide stop markers go when the policy that wrote them changes: the policy turned off
    # (escalate-after 0), or the effort step turned on, which may yet have a move for them.
    wide = (args.key == 'escalate-after' and value == '0') or (args.key == 'escalate-effort' and value == 'on')
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
        swept = [remove_safe(plan, target) for target in markers]   # as `run --escalate-after 0` and `--escalate-effort`
        return value, previous, changed or any(swept)

    where = f'task {task}' if task else 'the plan'
    return change(args, plan, rows, 'set', task, args.key, work, applies,
                  lambda v: f'set {args.key} {v} on {where}', live_only=live_only)


def doctor(spec, role='dwarf'):
    """Offline capability check of a spec: Refused when it fails, no quota spent."""
    override = os.environ.get('FORGE_CONTROL_DOCTOR')
    base = shlex.split(override) if override else ['bash', os.path.join(SKILL, 'scripts', 'forge-dispatch.sh'), 'doctor']
    try:
        done = subprocess.run(base + ['--spec', spec, '--role', role], capture_output=True, text=True,
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


def run_command(args, action, rel, value, summary):
    """drain, resume and stop: a live-only plan file the scheduler reads at every pass.
    value None removes the file; logged with the action and no key."""
    plan, rows = load(args.plan)

    def work():
        previous = text_of(plan, rel, strict=True)
        present = previous is not None or has(plan, rel)
        if value is None:
            return None, previous, remove_safe(plan, rel)
        write_atomic(plan, rel, value + ('\n' if value else ''))
        return value or None, previous, not present or (previous or '') != value

    return change(args, plan, rows, action, None, None, work, NEXT_DISPATCH, summary, live_only=True)


def cmd_drain(args):
    return run_command(args, 'drain', 'drain', '', lambda v: 'drained the plan: nothing new starts')


def cmd_resume(args):
    return run_command(args, 'resume', 'drain', None, lambda v: 'resumed the plan')


def cmd_stop(args):
    mode = 'now' if args.now else 'graceful'
    return run_command(args, 'stop', 'stop', mode, lambda v: 'stopping the run now' if args.now else
                       'stopping the run once running tasks are done')


# ------------------------------------------------------------------------------------ requests

REQUESTS = 'control.requests'
DIFFICULTIES = ('low', 'medium', 'high')


def clean_spec(spec, what):
    if not spec or re.search(r'[\s\x00-\x1f\x7f,]', spec) or spec in ('-', 'UNASSIGNED') or spec.startswith('-'):
        raise Invalid(f'{what} must be one explicit spec such as sol:high:codex')
    return spec


def check_row(plan, rows, task):
    """The checks `plan` would need before a row may join a live run; Invalid otherwise."""
    if not valid_task_id(task):
        raise Invalid(f'invalid task id: {task!r}')
    found = [row for row in rows if row[0] == task]
    if not found:
        raise Invalid(f'task {task!r} is not in tasks.tsv')
    if len(found) > 1:
        raise Invalid(f'task {task!r} appears more than once in tasks.tsv')
    row = found[0]
    if len(row) != 7:
        raise Invalid(f'task {task!r}: tasks.tsv rows need 7 tab-separated columns, this one has {len(row)}')
    _, deps, difficulty, files, dwarf, qa, _ = row
    if difficulty not in DIFFICULTIES:
        raise Invalid(f'task {task!r}: difficulty must be low, medium or high')
    ids = {r[0] for r in rows}
    wanted = [d for d in deps.split(',') if d and d != '-']
    missing = [d for d in wanted if d not in ids]
    if missing:
        raise Invalid(f'task {task!r} depends on unknown task(s): {", ".join(missing)}')
    graph = {r[0]: [d for d in (r[1].split(',') if len(r) > 1 else []) if d and d != '-'] for r in rows}
    state = {}

    def cyclic(node):
        state[node] = 1
        for dep in graph.get(node, []):
            if state.get(dep) == 1 or (dep not in state and dep in graph and cyclic(dep)):
                return True
        state[node] = 2
        return False

    if cyclic(task):
        raise Invalid(f'task {task!r}: its dependencies form a cycle')
    if not files.strip() or files.strip() == '-':
        raise Invalid(f'task {task!r}: the files column is empty')
    clean_spec(dwarf, f'task {task!r}: the dwarf column')
    clean_spec(qa, f'task {task!r}: the qa column')
    prompt = read_file(plan, f'tasks/{task}/prompt.md', strict=True)
    if not (prompt or '').strip():
        raise Invalid(f'task {task!r}: tasks/{task}/prompt.md is missing or empty')
    return dwarf, qa


def queue(args, plan, verb, task, dwarf):
    """Write the request under the control lock, live only; log it with its id."""
    with control_lock(plan):
        probe(plan, 'control.log')
        if not is_live(plan):
            raise Refused(f'{verb} needs a live run (no live scheduler for this plan)')
        if verb == 'retry':
            refuse_task(plan, task, pin=True)
        name = f'{time.time_ns()}-{os.getpid()}.json'
        request = dict(verb=verb, task=task, dwarf=dwarf, at=time.time(), actor=args.actor)
        write_atomic(plan, f'{REQUESTS}/{name}', json.dumps(request) + '\n')
        audit(plan, args.actor, verb, task, None, dwarf, None, request=name)
    if args.json:
        json.dump(dict(queued=True, request=name), sys.stdout)
        sys.stdout.write('\n')
    else:
        print(f'queued {verb} of task {task}' + (f' on {dwarf}' if dwarf else '') + f' (request {name}); '
              'the scheduler applies or refuses it at its next pass and logs the outcome in control.log')
    return 0


def cmd_retry(args):
    plan, rows = load(args.plan)
    require_task(rows, args.task)
    dwarf = None
    if args.dwarf is not None:
        dwarf = clean_spec(args.dwarf, '--dwarf')
    refuse_task(plan, args.task, pin=True)   # before the doctor: MERGED and Fractal need no model check
    if dwarf:
        doctor(dwarf)
    return queue(args, plan, 'retry', args.task, dwarf)


def cmd_add(args):
    plan, rows = load(args.plan)
    dwarf, qa = check_row(plan, rows, args.task)
    doctor(dwarf, 'dwarf')
    doctor(qa, 'qa')
    return queue(args, plan, 'add', args.task, None)


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
    print('  ' + '  '.join(f"{k.replace('_', '-')} {cell(v)}" + (' (default)' if k == 'qa_threshold' and run['qa_threshold_source'] == 'default' else '')
                           for k, v in run.items() if k not in ('infra_paused', 'qa_threshold_source')))
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
    sub = top.add_subparsers(dest='command', required=True,
                             metavar='{show,set,pin,unpin,retry,add,drain,resume,stop}')

    def common(p, mutator=True):
        p.add_argument('plan', metavar='PLAN_DIR', help="the plan dir (the one with tasks.tsv)")
        p.add_argument('--json', action='store_true', help='print JSON, for scripts and forge-ui')
        if mutator:
            p.add_argument('--actor', choices=('ui', 'cli'), default='cli', help='who is asking, for control.log (default cli)')

    p = sub.add_parser('show', help='every control, per-task overrides, pins and the change log')
    common(p, mutator=False)
    p.set_defaults(func=cmd_show)
    p = sub.add_parser('set', help='change a control', description='Keys: ' + ', '.join(KEYS) + '. '
                       'retry-budget, infra-retries and max-parallel change only while a run is live. '
                       'qa-threshold and retry-budget also take --task (value inherit drops the override; at run level, default drops the file and none means P3).')
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
    p = sub.add_parser('retry', help='queue a retry of a stopped or failed task into the live run')
    common(p)
    p.add_argument('task')
    p.add_argument('--dwarf', metavar='SPEC', help='the implementer for this retry (an explicit escalation)')
    p.set_defaults(func=cmd_retry)
    p = sub.add_parser('add', help='queue a row added to tasks.tsv into the live run')
    common(p)
    p.add_argument('task')
    p.set_defaults(func=cmd_add)
    p = sub.add_parser('drain', help='start nothing new; running tasks finish and the scheduler waits while tasks remain (live only)')
    common(p)
    p.set_defaults(func=cmd_drain)
    p = sub.add_parser('resume', help='end a drain (live only)')
    common(p)
    p.set_defaults(func=cmd_resume)
    p = sub.add_parser('stop', help='end the run once running tasks finish, exit 130 without verifying unless every task merged (live only)')
    common(p)
    p.add_argument('--now', action='store_true', help='interrupt running tasks, as SIGTERM does')
    p.set_defaults(func=cmd_stop)
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
