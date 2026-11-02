"""The one table of infrastructure causes, for every Python reader (the scheduler, forge-control.py,
the summary). Standard library only.

forge-runtime.py gives each infrastructure stop a class, the name written to <role>.infra and
infra.txt. A class names a symptom, not a recovery: `quota` once covered a window that resets and
money that is gone. CAUSES says what each cause means for a recovery. Each entry is a dict with:

  scope         model | harness | all: what a pause holds back (one model, one harness, everything)
  move          lateral | wait | retry: what to do first. lateral moves the work to a peer (another
                model or harness); wait pauses until the cause clears; retry dispatches again
  without_peer  wait | ask | wait_if_reset: what to do when no peer can take the work. wait_if_reset
                waits when the reset is known and inside WAIT_CAP_S, and asks otherwise
  hint          a sentence for a person, with {harness} filled in by hint(); None when there is
                nothing to say

LEGACY maps the class names runs recorded before the split still carry.

The run summary and `forge control show` both say what a stop is (label, reset_text, stop_of, the
summary lines), so the wording lives here too. The summary (forge-lib-task.sh) calls this file as a
program: see main().
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

# The longest wait worth holding a run for; the same value as INFRA_WAIT_CAP in forge-schedule.py
# (a test keeps the two equal).
WAIT_CAP_S = 6 * 3600

CAUSES = {
    'capacity': {'scope': 'model', 'move': 'lateral', 'without_peer': 'wait', 'hint': None},
    'rate_limit': {'scope': 'model', 'move': 'wait', 'without_peer': 'wait', 'hint': None},
    'usage_limit': {'scope': 'harness', 'move': 'lateral', 'without_peer': 'wait_if_reset',
                    'hint': "wait for the reset, or add another harness to this tier's pool"},
    'budget': {'scope': 'harness', 'move': 'lateral', 'without_peer': 'ask',
               'hint': 'add credits, or check the billing of {harness}'},
    'auth': {'scope': 'harness', 'move': 'lateral', 'without_peer': 'ask',
             'hint': 'log in to {harness} again'},
    'network': {'scope': 'all', 'move': 'wait', 'without_peer': 'wait', 'hint': None},
    'provider_down': {'scope': 'harness', 'move': 'lateral', 'without_peer': 'wait', 'hint': None},
    'empty': {'scope': 'model', 'move': 'retry', 'without_peer': 'wait', 'hint': None},
    'cancelled': {'scope': 'model', 'move': 'retry', 'without_peer': 'ask',
                  'hint': "check {harness}'s approval and permission settings"},
}

LEGACY = {'quota': 'usage_limit'}


def cause_of(cls) -> str:
    """The cause a stored class stands for: a legacy name is mapped, an unknown name is returned
    as it is, and None or empty gives ''."""
    if not cls:
        return ''
    return LEGACY.get(cls, cls)


def entry(cls) -> dict:
    """The table entry for a stored class. An unknown cause acts like network: pause everything, wait."""
    found = CAUSES.get(cause_of(cls))
    return dict(found if found is not None else CAUSES['network'])


def needs_you(cls, has_peer, reset_known, reset_within_cap) -> bool:
    """True when the recovery ends in a person: no peer to move to, and the cause will not clear
    by itself (it asks outright, or it waits for a reset that is unknown or past the cap)."""
    if has_peer:
        return False
    without_peer = entry(cls)['without_peer']
    if without_peer == 'ask':
        return True
    return without_peer == 'wait_if_reset' and not (reset_known and reset_within_cap)


def hint(cls, harness) -> str | None:
    """What to tell a person about this cause for this harness, or None when there is nothing to say."""
    text = entry(cls)['hint']
    return None if text is None else text.format(harness=harness or 'the harness')


def label(cls) -> str:
    """The cause as a person reads it: the stored class as written (a legacy `quota` stays `quota`),
    or `unknown` when there is none."""
    return cls or 'unknown'


def reset_text(epoch, now) -> str:
    """`resets HH:MM` for when a limit lifts, in local time; the date follows when it is not today
    (as of `now`). '' when the reset is unknown."""
    if epoch is None:
        return ''
    try:
        at, today = time.localtime(epoch), time.localtime(now)
    except (OverflowError, OSError, TypeError, ValueError):
        return ''
    if time.strftime('%Y-%m-%d', at) == time.strftime('%Y-%m-%d', today):
        return 'resets ' + time.strftime('%H:%M', at)
    return 'resets ' + time.strftime('%Y-%m-%d %H:%M', at)


def _epoch(value) -> float | int | None:
    """A finite non-negative number from a stored value, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number == int(number) else number


def parse_stop(text) -> dict:
    """The key=value lines of a task's infra.txt, `stage=` included; a later line of a key wins."""
    info = {}
    for line in (text or '').splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip():
            info[key.strip()] = value.strip()
    return info


def parse_pauses(text) -> list:
    """The scopes of an infra.paused file; [] when it is absent or unreadable."""
    try:
        scopes = json.loads(text or '')['scopes']
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(scopes, list):
        return []
    return [scope for scope in scopes if isinstance(scope, dict)]


def stop_of(task, info, scopes, now) -> dict:
    """What a person needs to know about an INFRA task: its cause, where it stopped, when its limit
    resets and whether a person must act. The scope that names the task in its `tasks` decides
    needs_you and the hint when one does; otherwise the cause decides as it would with no peer."""
    cls = info.get('class', '')
    harness, model = info.get('harness', ''), info.get('model', '')
    reset = _epoch(info.get('reset_at'))
    scope = next((s for s in scopes if isinstance(s.get('tasks'), list) and task in s['tasks']), None)
    if scope is not None:
        needs, text = bool(scope.get('needs_you')), scope.get('hint')
    else:
        within = reset is not None and reset - now <= WAIT_CAP_S
        needs, text = needs_you(cls, False, reset is not None, within), hint(cls, harness)
    return dict(cause=label(cls), harness=harness, model=model, stage=info.get('stage', ''),
                reset_at=reset, needs_you=needs, hint=text)


def stop_line(task, record, now) -> str:
    """The run summary's line for an INFRA task, from its stop_of record."""
    where = '/'.join(part for part in (record['harness'], record['model']) if part) or 'unknown'
    line = '  %-14s %s on %s (%s stage)' % (task, record['cause'], where, record['stage'] or 'unknown')
    reset = reset_text(record['reset_at'], now)
    if reset:
        line += ', ' + reset
    if record['needs_you']:
        line += ' — needs you' + (': ' + record['hint'] if isinstance(record['hint'], str) and record['hint'] else '')
    return line


def parse_laterals(task, text, since) -> list:
    """The lateral moves in one task's lateral.tsv made at or after `since`, an epoch in seconds. The rows
    hold whole seconds, so `since` is taken down to one. Each move is a dict: task, role, frm, to, cause."""
    moves, floor = [], math.floor(since)
    for line in (text or '').splitlines():
        cols = line.split('\t')
        if len(cols) < 5 or not cols[0].isdigit() or int(cols[0]) < floor:
            continue
        cause = cols[5] if len(cols) > 5 and cols[5] != '-' else ''
        moves.append(dict(task=task, role=cols[2], frm=cols[3], to=cols[4], cause=cause))
    return moves


def held_lines(scopes) -> list:
    """The run summary's lines for the tasks a pause held back without a dispatch (infra.last's `held`):
    they never stopped, so they have no stop record of their own, but the pause may need a person."""
    lines = []
    for scope in scopes:
        held = [task for task in scope.get('held') or [] if isinstance(task, str)]
        if not held:
            continue
        where = '/'.join(part for part in (scope.get('harness'), scope.get('model')) if part) or 'everything'
        line = '  %-14s held back: %s on %s' % (', '.join(held), label(scope.get('class')), where)
        if scope.get('needs_you') is True:
            hint = scope.get('hint')
            line += ' — needs you' + (': ' + hint if isinstance(hint, str) and hint else '')
        lines.append(line)
    return lines


def moved_line(move) -> str:
    """The run summary's line for a lateral move."""
    return 'moved sideways: %s %s %s → %s (%s)' % (move['task'], move['role'], move['frm'], move['to'],
                                                   label(move['cause']))


# --- the summary's helper ------------------------------------------------------------------------
def _read(plan, name) -> str:
    try:
        with open(os.path.join(plan, name), errors='replace') as handle:
            return handle.read()
    except OSError:
        return ''


def _task_ids(plan) -> list:
    """The task ids of tasks.tsv, in its order (the first field of each row that is not a comment)."""
    return [line.split('\t')[0] for line in _read(plan, 'tasks.tsv').splitlines() if line and not line.startswith('#')]


def _stop(plan, task, scopes, now) -> dict:
    return stop_of(task, parse_stop(_read(plan, os.path.join('tasks', task, 'infra.txt'))), scopes, now)


def pauses_of(plan) -> list:
    """The pauses of a live scheduler (infra.paused), else those its end left (infra.last)."""
    return parse_pauses(_read(plan, 'infra.paused')) or parse_pauses(_read(plan, 'infra.last'))


def main(argv=None) -> int:
    """forge-lib-task.sh's run summary calls this for the infrastructure block. `stop` prints one
    line for an INFRA task; `needs-you` exits 0 when one of the tasks needs a person and 1 when none
    does; `held` prints a line for the tasks each pause held back undispatched; `moved` prints a line
    for each lateral move made at or after the given epoch time."""
    parser = argparse.ArgumentParser(prog='forge_infra.py', description=main.__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    stop = commands.add_parser('stop', help='the summary line for one INFRA task')
    stop.add_argument('plan')
    stop.add_argument('task')
    needs = commands.add_parser('needs-you', help='exit 0 when one of these INFRA tasks needs a person')
    needs.add_argument('plan')
    needs.add_argument('tasks', nargs='+')
    moved = commands.add_parser('moved', help='the lateral moves made at or after a time')
    moved.add_argument('plan')
    moved.add_argument('since', type=float)
    held = commands.add_parser('held', help='the tasks a pause held back when the run ended; exit 0 when one '
                                            'of those pauses needs a person')
    held.add_argument('plan')
    args = parser.parse_args(argv)
    now = time.time()
    scopes = pauses_of(args.plan)
    if args.command == 'held':
        for line in held_lines(scopes):
            print(line)
        return 0 if any(s.get('needs_you') is True and s.get('held') for s in scopes) else 1
    if args.command == 'stop':
        print(stop_line(args.task, _stop(args.plan, args.task, scopes, now), now))
        return 0
    if args.command == 'needs-you':
        return 0 if any(_stop(args.plan, task, scopes, now)['needs_you'] for task in args.tasks) else 1
    for task in _task_ids(args.plan):
        for move in parse_laterals(task, _read(args.plan, os.path.join('tasks', task, 'lateral.tsv')), args.since):
            print(moved_line(move))
    return 0


if __name__ == '__main__':
    sys.exit(main())
