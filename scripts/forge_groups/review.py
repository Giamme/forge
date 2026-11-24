#!/usr/bin/env python3
"""The text side of one group review (#140): the prompt, the decision files, the round's facts.

forge-lib-group.sh builds the candidate and dispatches the reviewer; this prints what the reviewer
reads and what a person reads when the round stops for a decision. Stdlib only.

    python3 -I scripts/forge_groups/review.py prompt PLAN GID [--threshold P2]
    python3 -I scripts/forge_groups/review.py decision PLAN GID oversized --size BYTES --limit KB
    python3 -I scripts/forge_groups/review.py decision PLAN GID ambiguous --attribution FILE
    python3 -I scripts/forge_groups/review.py facts ATTRIBUTION_JSON

`prompt` reads, under the plan and `groups/GID/`: groups.tsv, tasks.tsv, each member's task dir, the
group's `candidate.base`, `candidate.commit`, `members.tsv` and `round`, and the combined diff in
`combined.diff` (computed from the repository when absent). `facts` prints one line:
implicated<TAB>ambiguous<TAB>findings<TAB>blocking, the two lists comma separated or `-`.
Exit 0, or 2 on bad input with the reason on stderr.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import forge_groups  # noqa: E402
from forge_groups import attribute  # noqa: E402


class ReviewError(Exception):
    """Bad input; the message is one line."""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def _git(repo: str, *args) -> str:
    done = subprocess.run(['git', '-C', repo, '-c', 'core.quotepath=off', *args],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if done.returncode != 0:
        raise ReviewError('git %s failed' % ' '.join(args[:2]))
    return done.stdout.decode('utf-8', 'replace')


def _group(plan: Path, gid: str):
    for group in forge_groups.load(plan):
        if group.id == gid:
            return group
    raise ReviewError('%s is not a group of %s' % (gid, plan))


def _tasks(plan: Path) -> dict:
    return forge_groups._tasks(plan)


def _rows(path: Path) -> list:
    """members.tsv rows as (round, task, commit, attempt)."""
    rows = []
    for line in _read(path).splitlines():
        parts = line.split('\t')
        if len(parts) == 4 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1], parts[2], parts[3]))
    return rows


def _round(gdir: Path) -> int:
    text = _read(gdir / 'round').strip()
    return int(text) if text.isdigit() else 1


def _attribution(gdir: Path):
    try:
        data = json.loads(_read(gdir / 'attribution.json'))
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get('findings'), list) else None


def _finding_text(finding: dict, blocks: dict) -> str:
    text = blocks.get(finding.get('n'))
    if text:
        return text.strip('\n')
    where = finding.get('file') or 'no file'
    if finding.get('line'):
        where += ':%s' % finding['line']
    return '- [%s][%s] %s' % (finding.get('severity') or '?', finding.get('status') or '?', where)


def _earlier(gdir: Path):
    """The findings of the last reviewed round: (round, rows, {n: text}), or None."""
    data = _attribution(gdir)
    if not data or not isinstance(data.get('round'), int) or data['round'] >= _round(gdir):
        return None
    reply = _read(gdir / ('qa.round-%d.last' % data['round']))
    blocks = {number: finding.text for number, finding in
              enumerate(attribute._contract().parse_findings(reply), 1)}
    return data['round'], [f for f in data['findings'] if isinstance(f, dict)], blocks


def _stat(repo: str, base: str, commit: str) -> str:
    try:
        return _git(repo, 'diff', '--stat', base, commit, '--', '.', ':(exclude).forge').rstrip('\n')
    except ReviewError:
        return '(the diff stat could not be read)'


def _member(plan: Path, repo: str, task: str, row: list, gdir: Path, current: int, earlier, history: dict) -> list:
    tdir = plan / 'tasks' / task
    commit = _read(tdir / 'reviewed.commit').strip()
    base = _read(tdir / 'base_ref').strip()
    out = ['## Task %s: %s' % (task, row[6] if len(row) > 6 else task), '', _read(tdir / 'prompt.md').rstrip('\n')]
    approach = _read(tdir / 'approach.md').strip('\n')
    if approach:
        out += ['', 'Intended approach:', approach]
    out += ['', 'Files this task declared it would touch: %s' % (row[3] if len(row) > 3 else '-'),
            'Reviewed commit: %s' % commit]
    if base and commit:
        out += ['Its own diff against its base (%s):' % base[:7], _stat(repo, base, commit)]
    # The same wording the task review uses, so a reviewer reads one vocabulary.
    prereview = _read(tdir / 'prereview.txt').rstrip('\n')
    if prereview.strip():
        out += ['', prereview]
    guard = [line for line in _read(tdir / 'guard.txt').splitlines() if line.strip()]
    if guard:
        out += ['', 'Implementer guard notes (observed by forge, not claimed by the implementer):']
        out += ['  - ' + line for line in guard]
        out += ["A promise of later work means a check may never have run; judge the diff on its own merits."]
    drift = [line for line in _read(tdir / 'drift.txt').splitlines() if line.strip()]
    if drift:
        out += ['', 'Scope note: this task declared it would touch %s, and the' % (row[3] if len(row) > 3 else '-'),
                'diff also changes files it did not declare:']
        out += ['  - ' + line for line in drift]
        out += ['That is not automatically wrong — judge whether each was actually necessary. It',
                'matters because another task may own those files.']
    if earlier:
        number, findings, blocks = earlier
        mine = [f for f in findings if task in (f.get('tasks') or [])]
        if mine:
            out += ['', "Round %d findings attributed to this task (check each one against the code now):" % number]
            for finding in mine:
                out += [_finding_text(finding, blocks)]
        previous = history.get(task)
        now = history.get((task, current))
        if previous is not None and now is not None and previous != now:
            reply = _read(tdir / 'dwarf.last').strip('\n')
            out += ['', 'This task was reworked since round %d (attempt %s, then attempt %s).' % (number, previous, now)]
            if reply:
                out += ["The implementer's reply for the new attempt, to check against the code:", reply]
    return out


def _combined(repo: str, gdir: Path, base: str, commit: str) -> str:
    text = _read(gdir / 'combined.diff')
    if text:
        return text
    return _git(repo, 'diff', '--binary', '--irreversible-delete', base, commit, '--', '.', ':(exclude).forge')


def prompt(plan: Path, gid: str, threshold) -> str:
    group = _group(plan, gid)
    tasks = _tasks(plan)
    repo = _read(plan / 'repo').strip()
    gdir = plan / 'groups' / gid
    base = _read(gdir / 'candidate.base').strip()
    commit = _read(gdir / 'candidate.commit').strip()
    if not base or not commit:
        raise ReviewError('group %s has no candidate yet' % gid)
    current = _round(gdir)
    rows = _rows(gdir / 'members.tsv')
    history = {}
    for number, task, _, attempt in rows:
        if number < current:
            history[task] = attempt          # the last round before this one that held the task
        elif number == current:
            history[(task, current)] = attempt
    earlier = _earlier(gdir)
    contract = attribute._contract()
    out = [contract.qa_text('head', 'group').rstrip('\n'), '']
    deps = _read(gdir / 'review.deps').strip('\n')
    if deps:
        out += [deps, '']
    out += ['# Group %s: %s' % (gid, group.title if group.title != '-' else gid)]
    if group.goal != '-':
        out += ['', 'Goal of the group: %s' % group.goal]
    out += ['', 'Members, in merge order: %s.' % ', '.join(group.members), '']
    if earlier:
        number, findings, blocks = earlier
        loose = [f for f in findings if not f.get('tasks')]
        if loose:
            out += ['## Round %d findings that named no task' % number]
            out += [_finding_text(f, blocks) for f in loose] + ['']
    for task in group.members:
        out += _member(plan, repo, task, tasks.get(task, []), gdir, current, earlier, history) + ['']
    raw_mins = _read(plan / 'qa_test_minutes').strip()
    test_minutes = int(raw_mins) if raw_mins.isdigit() else None
    out += [contract.qa_text('tail', 'group', threshold, test_minutes).rstrip('\n'), '']
    out += ['```diff', _combined(repo, gdir, base, commit).rstrip('\n'), '```']
    return '\n'.join(out) + '\n'


def decision(plan: Path, gid: str, reason: str, size: int, limit: int, attribution: str) -> str:
    gdir = plan / 'groups' / gid
    lines = ['reasons: %s' % reason, '']
    if reason == 'oversized':
        lines += [
            'Group %s was not reviewed: its combined diff is %d bytes (%.1f KB), over the limit of %d KB'
            % (gid, size, size / 1024.0, limit),
            '(group_max_diff_kb in the plan). Nothing was dispatched, truncated or re-routed.',
            '',
            'Choices:',
            '  split the group:        edit %s/groups.tsv so its members form smaller groups, then run plan again' % plan,
            '  review each task alone: set the review of %s to `task` in %s/groups.tsv, then run plan again' % (gid, plan),
        ]
    else:
        try:
            data = json.loads(_read(Path(attribution)))
        except ValueError:
            data = None
        wanted = set((data or {}).get('ambiguous') or [])
        reply = _read(gdir / 'qa.last')
        blocks = {number: finding.text for number, finding in
                  enumerate(attribute._contract().parse_findings(reply), 1)}
        lines += ['Group %s stopped for your decision: the reviewer reported a blocking finding that names no' % gid,
                  'member task (no file of any member, or a task id outside the group), so forge cannot tell',
                  'which task has to change. Nothing retried.', '', 'Ambiguous blocking findings:']
        for finding in (data or {}).get('findings') or []:
            if finding.get('n') in wanted:
                lines += ['', _finding_text(finding, blocks)]
        lines += ['', 'The full review is in %s/qa.last.' % gdir, '',
                  'Choices:',
                  '  name the task:          answer with the member task id(s) each finding belongs to, then review again',
                  '  review each task alone: set the review of %s to `task` in %s/groups.tsv, then run plan again' % (gid, plan)]
    return '\n'.join(lines) + '\n'


def facts(path: str) -> str:
    try:
        data = json.loads(_read(Path(path)))
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise ReviewError('%s is not an attribution' % path)

    def listed(values):
        return ','.join(str(v) for v in values) if values else '-'

    return '%s\t%s\t%d\t%d' % (listed(data.get('implicated')), listed(data.get('ambiguous')),
                               len(data.get('findings') or []), 1 if data.get('blocking') else 0)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='review.py', description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prompt')
    p.add_argument('plan')
    p.add_argument('gid')
    p.add_argument('--threshold', default=None)
    d = sub.add_parser('decision')
    d.add_argument('plan')
    d.add_argument('gid')
    d.add_argument('reason', choices=('oversized', 'ambiguous'))
    d.add_argument('--size', type=int, default=0)
    d.add_argument('--limit', type=int, default=0)
    d.add_argument('--attribution', default='')
    f = sub.add_parser('facts')
    f.add_argument('attribution')
    args = parser.parse_args(argv)
    try:
        if args.command == 'facts':
            text = facts(args.attribution) + '\n'
        else:
            plan = Path(args.plan)
            if args.command == 'prompt':
                text = prompt(plan, args.gid, args.threshold)
            else:
                _group(plan, args.gid)
                text = decision(plan, args.gid, args.reason, args.size, args.limit, args.attribution)
    except (ReviewError, forge_groups.GroupError, ValueError) as error:
        sys.stderr.write('review: %s\n' % ' '.join(str(error).split()))
        return 2
    sys.stdout.write(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
