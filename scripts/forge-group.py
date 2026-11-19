#!/usr/bin/env python3
"""Show a plan's task groups, retry a review=group group, and set a group's title or goal.

  show PLAN [--group GID] [--json]
      Every group in groups.tsv order: id, review scope, reviewer (qa), title, goal,
      and each member with its status. A review=group group also reports its round,
      status, the implicated tasks of the last round, the first line of decision.md
      when the status is NEEDS_DECISION, and whether the group has merged.
      Reads only. Works on a live run and on a finished one.
  retry PLAN GID [--tasks a,b] [--reason TEXT]
      Only a review=group group.
      NEEDS_DECISION / ambiguous: --tasks is required. Runs _group_settle --tasks,
      which makes those members FAIL, then says how the next run retries them.
      A live scheduler picks the FAIL tasks up on its sweep when they have budget left.
      NEEDS_DECISION / oversized: refused. Split the group in groups.tsv and run plan again.
      UNKNOWN, INFRA, TIMEOUT, INTERRUPTED, ERROR: the group status becomes INTERRUPTED,
      so the next run reviews it again. No member attempt is spent.
      Anything else (PASS, merged, FAIL, WAITING, CONFLICT, ...) is refused.

  set PLAN GID [--title TEXT] [--goal TEXT] [--reason TEXT]
      Rewrite a group's title or goal in groups.tsv and freeze the groups again, under the
      control lock, logged in control.log (actor cli). One line each; an empty value means none
      (`-`). Refused (exit 3) when groups.tsv or a member's deps or files already differ from
      what plan froze: freezing again would accept those edits too, so run plan again. Works
      on a live run: title and goal change no membership, and the next prompt that names the
      group (a group review, a brief) uses the new text.

Exit codes: 0 ok, 2 usage or an unknown group or task, 3 refused (no groups.tsv on show,
a state this command does not retry, groups changed since plan, the control lock busy).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_groups  # noqa: E402
import forge_proc  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
PARALLEL = SCRIPTS / 'forge-parallel.sh'
REARM = ('UNKNOWN', 'INFRA', 'TIMEOUT', 'INTERRUPTED', 'ERROR')
REASON_LIMIT = 2000
TEXT_LIMIT = 500

_CONTROL = None
_SWEEP_EVERY = None


class Usage(Exception):
    """Bad invocation or an unknown group or task. Exit 2."""


class Refused(Exception):
    """A well-formed request the group's state does not allow. Exit 3."""


class SettleFailed(Exception):
    """_group_settle refused or failed. Its exit code is the command's."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write(f'forge group: {message}\n')
        raise SystemExit(2)


def control():
    """forge-control.py, loaded once. `audit` and `control_lock` are the writers to reuse."""
    global _CONTROL
    if _CONTROL is None:
        spec = importlib.util.spec_from_file_location('forge_control_cli', SCRIPTS / 'forge-control.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTROL = module
    return _CONTROL


def sweep_every() -> float:
    """forge-schedule.py's SWEEP_EVERY: seconds between looks at tasks that already ended FAIL."""
    global _SWEEP_EVERY
    if _SWEEP_EVERY is None:
        spec = importlib.util.spec_from_file_location('forge_schedule_cli', SCRIPTS / 'forge-schedule.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SWEEP_EVERY = float(module.SWEEP_EVERY)
    return _SWEEP_EVERY


def parser() -> argparse.ArgumentParser:
    top = Parser(prog='forge group', description="Show a plan's task groups, and retry a review=group group.")
    sub = top.add_subparsers(dest='command', required=True, parser_class=Parser)
    show = sub.add_parser('show', help='every group, its members and, for a review group, its round')
    show.add_argument('plan', help='the plan directory')
    show.add_argument('--group', metavar='GID', help='only this group')
    show.add_argument('--json', action='store_true', help='print a JSON list')
    retry = sub.add_parser('retry', help='answer an ambiguous group decision, or re-arm a review that did not finish')
    retry.add_argument('plan', help='the plan directory')
    retry.add_argument('gid', help='the group id')
    retry.add_argument('--tasks', help='comma list of member tasks; required when the decision is ambiguous')
    retry.add_argument('--reason', help="why, recorded in the plan's control.log (actor cli)")
    setter = sub.add_parser('set', help="a group's title or goal, then the groups frozen again")
    setter.add_argument('plan', help='the plan directory')
    setter.add_argument('gid', help='the group id')
    setter.add_argument('--title', help='one line; empty for none')
    setter.add_argument('--goal', help='one line: what the group is for; empty for none')
    setter.add_argument('--reason', help="why, recorded in the plan's control.log (actor cli)")
    return top


def plan_dir(path: str) -> str:
    return os.path.realpath(path)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def member_status(plan, task: str) -> str:
    """The task's status. `merged` wins over the status file, as task_status does."""
    tdir = Path(plan) / 'tasks' / task
    if (tdir / 'merged').is_file():
        return 'MERGED'
    words = (read_text(tdir / 'status') or '').split()
    return words[0] if words else 'PENDING'


def group_status(plan, gid: str) -> str | None:
    words = (read_text(Path(plan) / 'groups' / gid / 'status') or '').split()
    return words[0] if words else None


def group_merged(plan, gid: str) -> bool:
    return (Path(plan) / 'groups' / gid / 'merged').is_file()


def last_round_row(plan, gid: str) -> list | None:
    text = read_text(Path(plan) / 'groups' / gid / 'rounds.tsv')
    if text is None:
        return None
    last = None
    for line in text.splitlines():
        if line.strip() and not line.startswith('#'):
            last = line.split('\t')
    return last


def group_round(plan, gid: str, row: list | None) -> int | None:
    digits = ''.join(ch for ch in (read_text(Path(plan) / 'groups' / gid / 'round') or '') if ch.isdigit())
    if digits:
        return int(digits)
    if row and row[0].isdigit():
        return int(row[0])
    return None


def implicated_of(row: list | None) -> list:
    if not row or len(row) < 4 or row[3].strip() in ('', '-'):
        return []
    return [task for task in (part.strip() for part in row[3].split(',')) if task and task != '-']


def decision_line(plan, gid: str) -> str | None:
    text = read_text(Path(plan) / 'groups' / gid / 'decision.md')
    if text is None:
        return None
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return None


def decision_kind(line: str | None) -> str | None:
    """The reason word of a decision.md first line (`reasons: ambiguous`)."""
    if not line or not line.lower().startswith('reasons:'):
        return None
    rest = line.split(':', 1)[1].strip()
    token = rest.split()[0].strip(',') if rest else ''
    return token or None


def record(plan: Path, group) -> dict:
    members = [{'id': task, 'status': member_status(plan, task)} for task in group.members]
    out = {
        'id': group.id,
        'review': group.review,
        'qa': group.qa,
        'title': group.title,
        'goal': group.goal,
        'members': members,
    }
    if group.review != 'group':
        return out
    row = last_round_row(plan, group.id)
    status = group_status(plan, group.id)
    out['round'] = group_round(plan, group.id, row)
    out['status'] = status
    out['implicated'] = implicated_of(row)
    out['decision'] = decision_line(plan, group.id) if status == 'NEEDS_DECISION' else None
    out['merged'] = group_merged(plan, group.id)
    return out


def format_block(rec: dict) -> str:
    lines = [f"{rec['id']}  review={rec['review']}  qa={rec['qa']}"]
    lines.append(f"  title: {rec['title']}")
    lines.append(f"  goal: {rec['goal']}")
    lines.append('  members:')
    for member in rec['members']:
        lines.append(f"    {member['id']}  {member['status']}")
    if rec['review'] != 'group':
        return '\n'.join(lines)
    lines.append(f"  round: {'-' if rec['round'] is None else rec['round']}")
    lines.append(f"  status: {rec['status'] or '-'}")
    lines.append(f"  implicated: {', '.join(rec['implicated']) if rec['implicated'] else '-'}")
    if rec['status'] == 'NEEDS_DECISION':
        lines.append(f"  decision: {rec['decision'] or '-'}")
    if rec['merged']:
        lines.append('  merged: yes')
    return '\n'.join(lines)


def load_groups(plan: str) -> list:
    try:
        return forge_groups.load(plan)
    except forge_groups.GroupError as error:
        raise Usage(str(error)) from error
    except OSError as error:
        raise Usage(f'groups.tsv could not be read: {error.strerror or error}') from error


def require_groups_file(plan: str) -> None:
    if not os.path.isfile(os.path.join(plan, 'groups.tsv')):
        raise Refused(f'{plan} has no groups.tsv')


def find_group(groups, gid: str, plan: str):
    found = [group for group in groups if group.id == gid]
    if not found:
        known = ', '.join(group.id for group in groups) or '(none)'
        raise Usage(f'{gid} is not a group of {plan} (groups: {known})')
    return found[0]


def clean_reason(text: str | None) -> str | None:
    if text is None:
        return None
    reason = re.sub(r'\s+', ' ', text).strip()
    if not reason:
        return None
    if len(reason) > REASON_LIMIT:
        raise Usage(f'--reason is limited to {REASON_LIMIT} characters')
    return reason


def parse_tasks(text: str, members) -> list:
    names = [part.strip() for part in text.split(',')]
    if not names or any(not name for name in names):
        raise Usage('--tasks needs a comma list of member tasks')
    unknown = [name for name in names if name not in members]
    if unknown:
        raise Usage(f"--tasks: {', '.join(unknown)} is not a member of this group ({', '.join(members)})")
    return [member for member in members if member in names]


def state_message(plan: str, gid: str, status: str | None, members) -> str:
    script = PARALLEL.name
    if status == 'PASS':
        return f'group {gid} is PASS: nothing to retry; the next run merges it'
    if status == 'FAIL':
        return (f'group {gid} is FAIL: its members were already sent back; '
                f'the next {script} run {shlex.quote(plan)} --retry-failed N retries the ones that failed')
    if status == 'WAITING':
        return f'group {gid} is WAITING: a member is not IMPLEMENTED yet; nothing to retry'
    if status == 'CONFLICT':
        who = ', '.join(members) or '<id>'
        return (f"group {gid} is CONFLICT: the members' branches conflict; "
                f'fix one member with {script} retry {shlex.quote(plan)} <id> ({who}) and the group is re-reviewed')
    if status == 'REVIEWING':
        return f'group {gid} is REVIEWING: a review is running; nothing to retry'
    if status == 'INVALIDATED':
        return (f'group {gid} is INVALIDATED: the next run reviews it again, '
                f'unless the integration branch moved or is dirty')
    if status == 'NEEDS_DECISION':
        return (f'group {gid} is NEEDS_DECISION but the reason is not ambiguous or oversized; '
                f'see {plan}/groups/{gid}/decision.md')
    if not status:
        return f'group {gid} has not been reviewed; nothing to retry'
    return f'group {gid} is {status}: forge group retry does not retry this state'


def retry_budget(plan: str) -> str | None:
    text = control().text_of(plan, 'retry_budget')
    return text if text and text.isdigit() else None


def next_run_lines(plan: str) -> list:
    """How the FAIL members get another attempt: the next run, and a live scheduler's sweep."""
    budget = retry_budget(plan)
    number = budget if budget is not None else 'N'
    lines = [f'next run retries them: {PARALLEL} run {shlex.quote(plan)} --retry-failed {number}']
    if not control().is_live(plan):
        return lines
    every = '%g' % sweep_every()
    if budget == '0':
        lines.append(f"a live scheduler is running, but its retry budget is 0, so its sweep (every {every}s) "
                     f'leaves these FAIL tasks until the budget is raised')
    else:
        lines.append(f'a live scheduler picks up FAIL tasks with budget left on its sweep (every {every}s)')
    return lines


def audit(plan: str, gid: str, value: str, previous: str | None, reason: str) -> None:
    """One control.log line, actor cli, in the shape forge control writes. Called under the lock."""
    control().probe(plan, 'control.log')
    control().audit(plan, 'cli', 'retry', None, gid, value, previous, reason=reason)


def rearm(plan: str, gid: str, seen: str, reason: str) -> None:
    """Reset the group to INTERRUPTED under the control lock. No member file is touched."""
    ctrl = control()
    with ctrl.control_lock(plan):
        ctrl.probe(plan, 'control.log')
        if group_merged(plan, gid):
            raise Refused(f'group {gid} is merged: nothing to retry')
        current = group_status(plan, gid)
        if current != seen:
            raise Refused(f'group {gid} changed while waiting for the control lock (now {current or "unset"})')
        ctrl.write_atomic(plan, f'groups/{gid}/status', 'INTERRUPTED\n')
        ctrl.audit(plan, 'cli', 'retry', None, gid, 'INTERRUPTED', seen, reason=reason)


def run_settle(plan: str, gid: str, tasks: list) -> int:
    """_group_settle --tasks. Outside the control lock: post_attempt takes that same lock
    when the retry policy writes a marker, and holding it here would time that write out."""
    command = forge_proc.bash_cmd(PARALLEL, '_group_settle', plan, gid, '--tasks', ','.join(tasks))
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except OSError as error:
        raise Refused(f'could not run _group_settle: {error}') from error
    except subprocess.TimeoutExpired as error:
        raise Refused(f'_group_settle did not finish: {error}') from error
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def settle(plan: str, gid: str, tasks: list, reason: str) -> None:
    """Confirm the decision is still ambiguous, settle it, then record the retry."""
    ctrl = control()
    with ctrl.control_lock(plan):
        ctrl.probe(plan, 'control.log')
        if group_merged(plan, gid):
            raise Refused(f'group {gid} is merged: nothing to retry')
        if group_status(plan, gid) != 'NEEDS_DECISION' or decision_kind(decision_line(plan, gid)) != 'ambiguous':
            raise Refused(f'group {gid} changed while waiting for the control lock; it is no longer an ambiguous decision')
    code = run_settle(plan, gid, tasks)
    if code != 0:
        raise SettleFailed(code)
    named = ','.join(tasks)
    with ctrl.control_lock(plan):
        audit(plan, gid, named, 'NEEDS_DECISION', reason)
    sys.stdout.write(f'group {gid}: assigned the ambiguous findings to {named}\n')
    sys.stdout.write('\n'.join(next_run_lines(plan)) + '\n')


def cmd_show(args) -> int:
    plan = plan_dir(args.plan)
    require_groups_file(plan)
    groups = load_groups(plan)
    if args.group:
        groups = [find_group(groups, args.group, plan)]
    records = [record(Path(plan), group) for group in groups]
    if args.json:
        json.dump(records, sys.stdout, indent=2)
        sys.stdout.write('\n')
    elif records:
        sys.stdout.write('\n\n'.join(format_block(rec) for rec in records) + '\n')
    return 0


def cmd_retry(args) -> int:
    plan = plan_dir(args.plan)
    if not os.path.isdir(plan):
        raise Usage(f'{args.plan} is not a plan directory')
    if not os.path.isfile(os.path.join(plan, 'groups.tsv')):
        raise Usage(f'{plan} has no groups.tsv')
    group = find_group(load_groups(plan), args.gid, plan)
    if group.review != 'group':
        raise Refused(f'group {group.id} has review={group.review}: forge group retry only retries a review=group group')
    root = Path(plan)
    if group_merged(root, group.id):
        raise Refused(f'group {group.id} is merged: nothing to retry')
    status = group_status(root, group.id)
    kind = decision_kind(decision_line(root, group.id)) if status == 'NEEDS_DECISION' else None
    if status == 'NEEDS_DECISION' and kind == 'oversized':
        raise Refused(f'group {group.id} is NEEDS_DECISION (oversized): '
                      f'split the group in {plan}/groups.tsv and run plan again')
    reason = clean_reason(args.reason)
    if status == 'NEEDS_DECISION' and kind == 'ambiguous':
        if not args.tasks:
            raise Refused(f'group {group.id} is NEEDS_DECISION (ambiguous): --tasks is required; '
                          f"name the member tasks the findings belong to ({', '.join(group.members)})")
        tasks = parse_tasks(args.tasks, group.members)
        settle(plan, group.id, tasks, reason or f"assigned ambiguous findings to {','.join(tasks)}")
        return 0
    message = state_message(plan, group.id, status, group.members)
    if args.tasks:
        raise Refused(f'--tasks answers an ambiguous NEEDS_DECISION. {message}')
    if status in REARM:
        rearm(plan, group.id, status, reason or f'review the group again after {status}; no member attempt spent')
        sys.stdout.write(f'group {group.id}: reset to INTERRUPTED (was {status}); '
                         f'the next run reviews it again, and no member attempt was spent\n')
        return 0
    raise Refused(message)


def one_line(text: str, flag: str) -> str:
    value = ' '.join(text.split())
    if len(value) > TEXT_LIMIT:
        raise Usage(f'{flag} is limited to {TEXT_LIMIT} characters')
    return value or '-'


def cmd_set(args) -> int:
    plan = plan_dir(args.plan)
    if not os.path.isdir(plan):
        raise Usage(f'{args.plan} is not a plan directory')
    if not os.path.isfile(os.path.join(plan, 'groups.tsv')):
        raise Usage(f'{plan} has no groups.tsv')
    if args.title is None and args.goal is None:
        raise Usage('set needs --title or --goal')
    title = None if args.title is None else one_line(args.title, '--title')
    goal = None if args.goal is None else one_line(args.goal, '--goal')
    reason = clean_reason(args.reason)
    find_group(load_groups(plan), args.gid, plan)
    ctrl = control()
    with ctrl.control_lock(plan):
        ctrl.probe(plan, 'control.log')
        groups = load_groups(plan)
        group = find_group(groups, args.gid, plan)
        stale = forge_groups.check_frozen(plan)
        if stale:
            raise Refused(f'{stale}: forge group set would freeze those changes too')
        new = group._replace(title=group.title if title is None else title, goal=group.goal if goal is None else goal)
        if new == group:
            sys.stdout.write(f'group {group.id}: unchanged\n')
            return 0
        forge_groups.write(plan, [new if g.id == group.id else g for g in groups])
        forge_groups.freeze(plan)
        for key, before, after in (('title', group.title, new.title), ('goal', group.goal, new.goal)):
            if before != after:
                ctrl.audit(plan, 'cli', 'group-set', None, f'{group.id}.{key}', after, before,
                           **({'reason': reason} if reason else {}))
    sys.stdout.write(f'group {group.id}: title {new.title!r}, goal {new.goal!r}; groups frozen again\n')
    return 0


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == 'show':
            return cmd_show(args)
        if args.command == 'set':
            return cmd_set(args)
        return cmd_retry(args)
    except Usage as error:
        sys.stderr.write(f'forge group: {error}\n')
        return 2
    except control().Invalid as error:
        sys.stderr.write(f'forge group: {error}\n')
        return 2
    except SettleFailed as error:
        return error.code
    except (Refused, control().Refused) as error:
        sys.stderr.write(f'forge group: {error}\n')
        return 3


if __name__ == '__main__':
    sys.exit(main())
