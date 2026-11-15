#!/usr/bin/env python3
"""forge handoff: a self-contained report of a run for the next session (#146).

  show [RUN_DIR | --repo DIR] [--json]     (the default subcommand: `forge handoff RUN_DIR`)
      Everything a new session, or another harness, needs to carry a run on, in one output:
      the run, the choices it was launched with, whether a scheduler lives, each task, what
      needs a person, the last human changes, tasks left RUNNING by a dead scheduler, the
      handoff notes and driver, and the exact commands to run next.
      With no RUN_DIR it picks --repo's (default: this checkout's) newest run that is live or
      has a task not MERGED, from forge's run registry.
      Read-only: it writes nothing, takes no lock and never waits on one. A file that is
      missing or unreadable becomes null and a `warnings` entry says why.

  note RUN_DIR TEXT [--actor A] [--json]
      Append one session note to handoff.notes.jsonl: what is not on disk, such as an
      acceptance or a hand fix in progress. Whitespace in TEXT collapses to single spaces.
      Empty text, and text over 2000 characters, is refused. The actor defaults to cli.

  claim RUN_DIR --harness H [--session S] [--take-over] [--json]
      Record this session as the driver in handoff.driver.json (harness, session, since,
      last_seen). The same harness and session keep since and refresh last_seen. A different
      driver whose last_seen is under 12 hours ago is refused unless --take-over is given;
      a different driver older than that is replaced. Replacing a different driver appends
      one control.log take-over line through forge-control's audit.

Exit codes: 0 ok, 2 usage or a path that is not a run dir, 3 no run to report, or another
driver still holds the run (claim without --take-over).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import forge_infra  # noqa: E402
import forge_registry  # noqa: E402

PARALLEL = str(SCRIPTS / 'forge-parallel.sh')
READ_CAP = 1 << 20          # bytes read from any one file
LOG_TAIL = 1 << 18          # bytes read from the end of control.log
GOAL_LINES = 5
CHANGES = 10
TASK_OK = frozenset('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-')
# The per-plan option files (forge-lib-plans.sh PLANS_OPTION_FILES, forge-compare.py OPTION_FILES) and the
# other choices a plan keeps. Listed here, in that order, because a shell variable cannot be imported.
OPTION_FILES = (
    'no_ripwire', 'no_memory', 'yolo_dwarf', 'yolo_qa', 'reviewer_session', 'reviewer_session_rate',
    'reviewer_session_tasks', 'implementer_session', 'fast', 'pre_review_tests', 'pre_review_parallel',
    'qa_threshold', 'timeout', 'verify_retries', 'verify_cmd', 'verify_parallel', 'setup_cmd', 'final_qa',
    'shadow_qa', 'shadow_rate', 'explore', 'escalate_after', 'retry_per_model', 'fail_first',
    'fractal-routing.json', 'fractal-selection.json', 'jev-selection.json', 'profile.json', 'routed.json',
    'qa-routing.json', 'retro_mode', 'brief', 'brief_scope', 'brief_model', 'summaries', 'summaries_model',
    'groups.tsv', 'groups.frozen', 'group_max_tasks', 'group_max_diff_kb', 'after', 'max_parallel')
FLAG_KEYS = ('retry_failed', 'infra_retries', 'sweep', 'sweep_max', 'sweep_plausible', 'sweep_ahead',
             'max_parallel', 'timeout', 'verify', 'fractal', 'argv', 'at')
JSON_KEYS = ('run', 'choices', 'flags', 'scheduler', 'tasks', 'needs_you', 'changes', 'stale', 'notes',
             'driver', 'next')
NOTE_MAX = 2000
SESSION_MAX = 200
# A different driver last seen longer ago than this is replaced without --take-over. Tests patch it.
DRIVER_STALE_S = 12 * 3600
HARNESS_RE = re.compile(r'[a-z0-9._-]{1,40}')
# Subcommands. Any other first word is a RUN_DIR for the default, show.
COMMANDS = ('show', 'note', 'claim', '-h', '--help')

_control = None


class Usage(Exception):
    pass


class Nothing(Exception):
    pass


class Refused(Exception):
    """A well-formed request the run's state does not allow. Exit 3."""


class Held(Refused):
    """A different driver was seen inside the stale window. Nothing was written."""

    def __init__(self, driver):
        self.driver = driver

    def __str__(self):
        return held_message(self.driver)


class Plan:
    """A run dir, read without ever blocking or writing. warnings collects what could not be read."""

    def __init__(self, path):
        # absolute, so the next commands still work from another directory
        self.path = Path(os.path.abspath(path))
        self.warnings = []

    def warn(self, text):
        if text not in self.warnings:
            self.warnings.append(text)

    def read(self, rel, tail=False):
        """The text of a regular file under the run dir, or None (missing: silent; anything else warns)."""
        try:
            # O_NONBLOCK: a FIFO with no writer would block an open; it is not a regular file anyway.
            fd = os.open(self.path / rel, os.O_RDONLY | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            self.warn(f'{rel}: unreadable ({error.__class__.__name__})')
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                self.warn(f'{rel}: not a regular file')
                return None
            if tail and info.st_size > LOG_TAIL:
                os.lseek(fd, info.st_size - LOG_TAIL, os.SEEK_SET)
            raw = os.read(fd, LOG_TAIL if tail else READ_CAP)
            if not tail and info.st_size > READ_CAP:
                self.warn(f'{rel}: longer than {READ_CAP} bytes, cut')
        except OSError as error:
            self.warn(f'{rel}: unreadable ({error.__class__.__name__})')
            return None
        finally:
            os.close(fd)
        return raw.decode('utf-8', errors='replace')

    def exists(self, rel):
        try:
            return (self.path / rel).exists()
        except (OSError, ValueError):
            return False

    def line(self, rel):
        """The first line of a file, stripped; None when the file is missing, '' when it is empty."""
        text = self.read(rel)
        if text is None:
            return None
        return text.splitlines()[0].strip() if text.strip() else ''

    def json(self, rel, kind=dict):
        """A parsed JSON file of the given type, or None (a missing file is silent, bad content warns)."""
        text = self.read(rel)
        if text is None:
            return None
        try:
            value = json.loads(text)
        except ValueError:
            self.warn(f'{rel}: not valid JSON')
            return None
        if not isinstance(value, kind):
            self.warn(f'{rel}: not a JSON {"object" if kind is dict else "list"}')
            return None
        return value


# ------------------------------------------------------------------------------------ the sections

def kind_of(plan, fractal):
    if (plan.path / 'tasks.tsv').is_file():
        return 'fractal' if fractal else 'decomposed'
    if (plan.path / 'prompt.md').is_file() and (plan.path / 'repo').is_file():
        return 'solo'
    return None


def is_fractal(plan):
    data = plan.json('fractal-selection.json')
    return bool(data and data.get('enabled'))


def run_section(plan, kind):
    goal = None
    text = plan.read('request.md')
    if text is not None:
        lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        goal = lines[:GOAL_LINES]
    run_id = plan.line('run_id') or None
    return dict(
        run_id=run_id, kind=kind, goal=goal, plan=str(plan.path), repo=plan.line('repo') or None,
        worktree_root=plan.line('wt_root') or None,
        integration_branch=f'forge/{run_id}-integration' if run_id else None,
        integration_base=plan.line('integration.base') or None,
        accepted_integration=plan.line('accepted.integration') or None)


def choices_section(plan):
    found = {}
    for name in OPTION_FILES:
        if not plan.exists(name):
            continue
        if name.endswith('.json'):
            data = plan.json(name, (dict, list))
            found[name] = json.dumps(data, separators=(',', ':'), ensure_ascii=False) if data is not None else None
        else:
            found[name] = plan.line(name)
    return found


def flags_section(plan):
    if not plan.exists('run.flags.json'):
        plan.warn('run.flags.json missing (run from before 0.19)')
        return None
    data = plan.json('run.flags.json')
    if data is None:
        return None
    return {key: data.get(key) for key in FLAG_KEYS} | {key: value for key, value in data.items() if key not in FLAG_KEYS}


def pid_alive(pid):
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


def scheduler_section(plan):
    """scheduler.live names the pid; nothing here probes schedule.lock (a probe could fail a starting run)."""
    live = plan.json('scheduler.live') or {}
    holder = plan.json('schedule.holder') or {}
    pid = live.get('pid') if type(live.get('pid')) is int else None
    return dict(live=pid_alive(pid), pid=pid, started=live.get('started'),
                holder=dict(pid=holder.get('pid'), command=holder.get('command'), at=holder.get('at')) if holder else None)


def tasks_section(plan):
    rows, text = [], plan.read('tasks.tsv')
    for line in (text or '').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        cols = line.split('\t')
        task = cols[0]
        if not task or not set(task) <= TASK_OK or task in ('.', '..'):
            plan.warn(f'tasks.tsv: skipped a row with the task id {task!r}')
            continue
        if plan.exists(f'tasks/{task}/merged'):
            status = 'MERGED'
        else:
            words = (plan.read(f'tasks/{task}/status') or '').split()
            status = words[0] if words else 'PENDING'
        rows.append(dict(id=task, status=status, dwarf=(cols[4].strip() or None) if len(cols) > 4 else None))
    return rows


def needs_you_section(plan, tasks, now):
    items = []
    pauses = None
    for task in tasks:
        base = f'tasks/{task["id"]}/'
        if task['status'] == 'NEEDS_INPUT':
            question = plan.read(base + 'needs_input.md')
            items.append(dict(task=task['id'], kind='NEEDS_INPUT', question=question.rstrip() if question else None))
        elif task['status'] == 'NEEDS_DECISION':
            text = plan.read(base + 'decision.md')
            first = text.splitlines()[0].strip() if text and text.strip() else None
            items.append(dict(task=task['id'], kind='NEEDS_DECISION', reasons=first))
        elif task['status'] == 'INFRA':
            if pauses is None:
                pauses = forge_infra.parse_pauses(plan.read('infra.paused')) or forge_infra.parse_pauses(plan.read('infra.last'))
            info = forge_infra.parse_stop(plan.read(base + 'infra.txt'))
            stop = forge_infra.stop_of(task['id'], info, pauses, now)
            items.append(dict(task=task['id'], kind='INFRA', cause=stop['cause'], harness=stop['harness'],
                              model=stop['model'], stage=stop['stage'], reset_at=stop['reset_at'],
                              needs_you=stop['needs_you'], hint=stop['hint']))
    return items


def changes_section(plan):
    text = plan.read('control.log', tail=True)
    entries, bad = [], 0
    for line in (text or '').splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(entry, dict):
            entries.append(entry)
        else:
            bad += 1
    # The first line of a tail read may be cut in the middle; that is not worth a warning.
    if bad and not (bad == 1 and text and len(text) >= LOG_TAIL):
        plan.warn(f'control.log: {bad} line(s) are not JSON objects, skipped')
    return list(reversed(entries))[:CHANGES]


def notes_section(plan):
    text = plan.read('handoff.notes.jsonl')
    notes, bad = [], 0
    for line in (text or '').splitlines():
        if not line.strip():
            continue
        try:
            note = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(note, dict):
            notes.append(dict(at=note.get('at'), actor=note.get('actor'), text=note.get('text')))
        else:
            bad += 1
    if bad:
        plan.warn(f'handoff.notes.jsonl: {bad} line(s) are not JSON objects, skipped')
    return list(reversed(notes))


def driver_section(plan):
    data = plan.json('handoff.driver.json')
    if data is None:
        return None
    return {key: data.get(key) for key in ('harness', 'session', 'since', 'last_seen')}


def next_section(plan, run, scheduler, tasks, needs):
    here = str(plan.path)
    q = shlex.quote
    steps = []
    open_tasks = [task for task in tasks if task['status'] != 'MERGED']
    live = scheduler['live']
    if live:
        steps.append(dict(command=f'forge control show {q(here)}', why='the run is live: its state and controls'))
    elif open_tasks:
        steps.append(dict(command=f'forge handoff resume {q(here)}', why='no scheduler is live and work is left'))
        if run['kind'] == 'fractal':
            steps.append(dict(command=f'forge fractal resume {q(run["run_id"] or here)}',
                              why='a Fractal-backed run: recover its recorded executions'))
    for item in needs:
        if item['kind'] == 'NEEDS_INPUT':
            verb = f'forge control retry {q(here)} {q(item["task"])}' if live else f'{PARALLEL} retry {q(here)} {q(item["task"])}'
            steps.append(dict(command=verb + ' --answer "…"', why=f'answer the question of {item["task"]}'))
        elif item['kind'] == 'NEEDS_DECISION' and live:
            steps.append(dict(command=f'forge control retry {q(here)} {q(item["task"])} --decide retry=N --reason "…" --approved',
                              why=f'{item["task"]} needs a decision (or --decide threshold=Pn, or --dwarf SPEC)'))
    if tasks and not open_tasks and run['kind'] != 'solo':
        steps.append(dict(command=f'{PARALLEL} integrate {q(here)} --approved',
                          why='every task is merged; run this only on the user\'s word'))
    return steps


# ------------------------------------------------------------------------------------ the report

def report(path):
    plan = Plan(path)
    fractal = is_fractal(plan)
    kind = kind_of(plan, fractal)
    if kind is None:
        raise Usage(f'{path}: not a forge run dir (no tasks.tsv, and no prompt.md with a repo file)')
    run = run_section(plan, kind)
    scheduler = scheduler_section(plan)
    tasks = tasks_section(plan)
    needs = needs_you_section(plan, tasks, time.time())
    stale = [] if scheduler['live'] else [dict(task=task['id'], note='left RUNNING with no live process')
                                          for task in tasks if task['status'] == 'RUNNING']
    out = dict(run=run, choices=choices_section(plan), flags=flags_section(plan), scheduler=scheduler, tasks=tasks,
               needs_you=needs, changes=changes_section(plan), stale=stale, notes=notes_section(plan),
               driver=driver_section(plan), next=next_section(plan, run, scheduler, tasks, needs))
    out['warnings'] = plan.warnings
    return out


def when(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value))
        except (ValueError, OverflowError, OSError):
            pass
    return '' if value is None else str(value)


def render(data):
    out = []
    run, sched = data['run'], data['scheduler']

    def head(title):
        out.extend(['', title])

    out.append(f'forge handoff: run {run["run_id"] or "(no run_id)"}')
    head('The run')
    out.append(f'  kind         {run["kind"]}')
    if run['goal'] is None:
        out.append('  goal         (no request.md)')
    else:
        out.extend(['  goal         ' + (line if i == 0 else '               ' + line) for i, line in enumerate(run['goal'])] or ['  goal         (empty)'])
    for label, key in (('plan', 'plan'), ('repo', 'repo'), ('worktrees', 'worktree_root'),
                       ('integration', 'integration_branch'), ('base', 'integration_base'),
                       ('accepted', 'accepted_integration')):
        if run[key]:
            out.append(f'  {label:<12} {run[key]}')
    head('Launch choices')
    for name, value in data['choices'].items():
        out.append(f'  {name:<24} {"(present)" if value == "" else "(unreadable)" if value is None else value}')
    if not data['choices']:
        out.append('  (no option files)')
    if data['flags'] is None:
        out.append('  run.flags.json missing (run from before 0.19)' if not any('run.flags.json:' in w for w in data['warnings'])
                   else '  run.flags.json unreadable')
    else:
        for key, value in data['flags'].items():
            shown = when(value) if key == 'at' else json.dumps(value, ensure_ascii=False)
            out.append(f'  {key:<24} {shown}')
    head('Scheduler')
    if sched['live']:
        out.append(f'  live, pid {sched["pid"]}, started {when(sched["started"])}')
    else:
        out.append('  not live' + (f' (scheduler.live names pid {sched["pid"]}, which is gone)' if sched['pid'] else ''))
    if sched['holder']:
        h = sched['holder']
        out.append(f'  schedule.holder: {h["command"]} (pid {h["pid"]}, since {when(h["at"])})')
    head('Tasks')
    for task in data['tasks']:
        out.append(f'  {task["id"]:<24} {task["status"]:<15} {task["dwarf"] or "-"}')
    if not data['tasks']:
        out.append('  (none)')
    head('Needs you')
    for item in data['needs_you']:
        if item['kind'] == 'NEEDS_INPUT':
            out.append(f'  {item["task"]}: NEEDS_INPUT, the question:')
            out.extend('    ' + line for line in (item['question'] or '(needs_input.md missing)').splitlines())
        elif item['kind'] == 'NEEDS_DECISION':
            out.append(f'  {item["task"]}: NEEDS_DECISION  {item["reasons"] or "(decision.md missing)"}')
        else:
            where = '/'.join(part for part in (item['harness'], item['model']) if part) or 'unknown'
            line = f'  {item["task"]}: INFRA, {item["cause"]} on {where}'
            if item['stage']:
                line += f' ({item["stage"]} stage)'
            if item['needs_you']:
                line += ', needs you'
            out.append(line)
            if item['hint']:
                out.append(f'    {item["hint"]}')
    if not data['needs_you']:
        out.append('  nothing')
    head(f'Human changes (last {CHANGES}, newest first)')
    for entry in data['changes']:
        bits = [when(entry.get('at')), str(entry.get('actor') or '?'), str(entry.get('action') or '?')]
        for key in ('task', 'key', 'value'):
            if entry.get(key) not in (None, ''):
                bits.append(f'{key}={entry[key]}')
        if entry.get('previous') not in (None, ''):
            bits.append(f'(was {entry["previous"]})')
        out.append('  ' + '  '.join(bits))
    if not data['changes']:
        out.append('  none')
    head('Stale')
    for item in data['stale']:
        out.append(f'  {item["task"]}: {item["note"]}')
    if not data['stale']:
        out.append('  none')
    head('Notes and driver')
    driver = data['driver']
    if driver:
        out.append('  driver: ' + ', '.join(f'{k} {when(v) if k in ("since", "last_seen") else v}' for k, v in driver.items() if v is not None))
    else:
        out.append('  driver: none')
    for note in data['notes']:
        out.append(f'  {when(note["at"])}  {note["actor"] or "?"}: {note["text"]}')
    if not data['notes']:
        out.append('  notes: none')
    head('Next commands')
    for step in data['next']:
        out.append(f'  {step["command"]}')
        out.append(f'      # {step["why"]}')
    if not data['next']:
        out.append('  (nothing to run)')
    if data['warnings']:
        head('Warnings')
        out.extend('  ' + w for w in data['warnings'])
    return '\n'.join(out) + '\n'


# ------------------------------------------------------------------------------------ choosing a run

def git_top(directory):
    try:
        result = subprocess.run(['git', '-C', str(directory), 'rev-parse', '--show-toplevel'], capture_output=True,
                                text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


def newest_open_run(repo):
    """The repo's newest registered run (by run dir mtime) that is live or has a task not MERGED."""
    registry = forge_registry.runs_registry(repo)
    try:
        lines = registry.read_text(errors='replace').splitlines()
    except OSError:
        return None
    candidates = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or not os.path.isabs(line):
            continue
        try:
            candidates.append((os.stat(line).st_mtime, line))
        except (OSError, ValueError):
            continue
    for _, path in sorted(candidates, reverse=True):
        plan = Plan(path)
        if not (plan.path / 'tasks.tsv').is_file():
            continue
        if scheduler_section(plan)['live'] or any(task['status'] != 'MERGED' for task in tasks_section(plan)):
            return path
    return None


def resolve(args):
    if args.run_dir and args.repo:
        raise Usage('give a RUN_DIR or --repo, not both')
    if args.run_dir:
        return args.run_dir
    if args.repo:
        if not os.path.isdir(args.repo):
            raise Usage(f'{args.repo}: not a directory')
        repo = git_top(args.repo) or args.repo
    else:
        repo = git_top(os.getcwd())
        if repo is None:
            raise Usage('not inside a git checkout: give a RUN_DIR or --repo DIR')
    found = newest_open_run(repo)
    if found is None:
        raise Nothing(f'no active or unfinished run for {repo}')
    return found


def cmd_show(args):
    try:
        path = resolve(args)
        data = report(path)
    except Usage as error:
        print(f'forge handoff: {error}', file=sys.stderr)
        return 2
    except Nothing as error:
        print(f'forge handoff: {error}', file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(render(data))
    return 0


# ------------------------------------------------------------------------------------ notes and driver

def control():
    """forge-control.py, loaded by path: its file name is not importable.

    audit, append_line, write_atomic and control_lock are the writers to reuse.
    """
    global _control
    if _control is None:
        spec = importlib.util.spec_from_file_location('forge_control_handoff', SCRIPTS / 'forge-control.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _control = module
    return _control


def require_run(path):
    """The canonical plan dir, or Usage. The same shape show accepts."""
    message = f'{path}: not a forge run dir (no tasks.tsv, and no prompt.md with a repo file)'
    if not os.path.isdir(path):
        raise Usage(message)
    plan = os.path.realpath(path)
    root = Path(plan)
    if (root / 'tasks.tsv').is_file() or ((root / 'prompt.md').is_file() and (root / 'repo').is_file()):
        return plan
    raise Usage(message)


def collapse_text(text):
    return re.sub(r'\s+', ' ', text).strip()


def note_text(text):
    collapsed = collapse_text(text)
    if not collapsed:
        raise Usage('the note is empty')
    if len(collapsed) > NOTE_MAX:
        raise Usage(f'the note is longer than {NOTE_MAX} characters')
    return collapsed


def check_harness(harness):
    if not isinstance(harness, str) or HARNESS_RE.fullmatch(harness) is None:
        raise Usage('harness must match [a-z0-9._-]{1,40}')


def check_session(session):
    """None when omitted. A present id has no whitespace and is at most 200 characters."""
    if session is None:
        return None
    if (not isinstance(session, str) or not session or any(ch.isspace() for ch in session)
            or len(session) > SESSION_MAX):
        raise Usage('session must be an id with no whitespace, at most 200 characters')
    return session


def _epoch(value):
    """A finite JSON number, or None. Bool is a subclass of int and is not a timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float('inf'), float('-inf')):
        return None
    return value


def parse_driver(raw):
    """The driver record, or None when the file is missing, unreadable or not that record."""
    if not raw or not str(raw).strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    harness = data.get('harness')
    if not isinstance(harness, str) or HARNESS_RE.fullmatch(harness) is None:
        return None
    if 'session' not in data or data.get('session') is None:
        session = None
    else:
        session = data.get('session')
        if (not isinstance(session, str) or not session or any(ch.isspace() for ch in session)
                or len(session) > SESSION_MAX):
            return None
    since, last_seen = _epoch(data.get('since')), _epoch(data.get('last_seen'))
    if since is None or last_seen is None:
        return None
    return {'harness': harness, 'session': session, 'since': since, 'last_seen': last_seen}


def read_driver(ctrl, plan):
    """None when the file is absent, unreadable, or not a driver record."""
    try:
        raw = ctrl.read_file(plan, 'handoff.driver.json')
    except OSError:
        return None
    return parse_driver(raw)


def driver_ref(driver):
    """'<harness>/<session>' as control.log records a driver. An omitted session is empty."""
    session = driver.get('session') or ''
    return f"{driver['harness']}/{session}"


def held_message(driver):
    session = driver.get('session')
    name = driver['harness'] if not session else f"{driver['harness']}/{session}"
    return f'{name} holds this run, last seen {when(driver["last_seen"])} ({driver["last_seen"]})'


def under_lock(ctrl, plan, work):
    """Run work(ctrl) while holding the plan's control lock. Map control's errors to ours."""
    try:
        with ctrl.control_lock(plan):
            return work(ctrl)
    except Held:
        raise  # a refused claim, not a control error: nothing was written
    except ctrl.Invalid as error:
        raise Usage(str(error)) from error
    except ctrl.Refused as error:
        raise Refused(str(error)) from error


def cmd_note(args):
    text = note_text(args.text)
    actor = args.actor.strip()
    if not actor:
        raise Usage('the actor is empty')
    plan = require_run(args.run_dir)
    line = json.dumps({'at': time.time(), 'actor': actor, 'text': text}, ensure_ascii=False) + '\n'

    def write(ctrl):
        ctrl.append_line(plan, 'handoff.notes.jsonl', line)

    under_lock(control(), plan, write)
    print(json.dumps({'noted': True}) if args.json else 'noted')
    return 0


def cmd_claim(args):
    check_harness(args.harness)
    session = check_session(args.session)
    plan = require_run(args.run_dir)

    def decide(ctrl):
        now = time.time()
        previous = read_driver(ctrl, plan)
        if previous is None:
            record = {'harness': args.harness, 'session': session, 'since': now, 'last_seen': now}
            took_over = False
        elif previous['harness'] == args.harness and previous['session'] == session:
            record = {'harness': args.harness, 'session': session, 'since': previous['since'], 'last_seen': now}
            took_over = False
        elif now - previous['last_seen'] < DRIVER_STALE_S and not args.take_over:
            raise Held(previous)
        else:
            record = {'harness': args.harness, 'session': session, 'since': now, 'last_seen': now}
            took_over = True
        ctrl.write_atomic(plan, 'handoff.driver.json', json.dumps(record, ensure_ascii=False) + '\n')
        if took_over:
            ctrl.audit(plan, 'cli', 'take-over', None, 'driver', driver_ref(record), driver_ref(previous))
        return record, took_over, previous

    record, took_over, previous = under_lock(control(), plan, decide)
    if args.json:
        print(json.dumps({'driver': record, 'took_over': took_over, 'previous': previous}, ensure_ascii=False))
    else:
        print('took over' if took_over else 'claimed')
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog='forge handoff', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', metavar='{show,note,claim}')
    show = sub.add_parser('show', help='report a run (the default)')
    show.add_argument('run_dir', nargs='?', metavar='RUN_DIR')
    show.add_argument('--repo', metavar='DIR', help="report DIR's newest active or unfinished run")
    show.add_argument('--json', action='store_true', help='one JSON object instead of text')
    show.set_defaults(func=cmd_show)
    note = sub.add_parser('note', help='append a session note to handoff.notes.jsonl')
    note.add_argument('run_dir', metavar='RUN_DIR')
    note.add_argument('text', metavar='TEXT')
    note.add_argument('--actor', default='cli', help='who is writing (default cli)')
    note.add_argument('--json', action='store_true', help='print {"noted": true}')
    note.set_defaults(func=cmd_note)
    claim = sub.add_parser('claim', help='claim the run, or take it over from another driver')
    claim.add_argument('run_dir', metavar='RUN_DIR')
    claim.add_argument('--harness', required=True, metavar='H', help='a short word: [a-z0-9._-]{1,40}')
    claim.add_argument('--session', metavar='S', help='an opaque id, no whitespace, at most 200 characters')
    claim.add_argument('--take-over', action='store_true', help='replace a different driver that was seen recently')
    claim.add_argument('--json', action='store_true', help='print the driver, whether it took over, and the previous one')
    claim.set_defaults(func=cmd_claim)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    # `forge handoff RUN_DIR` and `forge handoff --repo DIR` mean `show`; a bare `forge handoff` too.
    if not argv or argv[0] not in COMMANDS:
        argv = ['show'] + argv
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 2
    try:
        return args.func(args)
    except Usage as error:
        print(f'forge handoff: {error}', file=sys.stderr)
        return 2
    except Refused as error:
        print(f'forge handoff: {error}', file=sys.stderr)
        return 3


if __name__ == '__main__':
    sys.exit(main())
