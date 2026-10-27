#!/usr/bin/env python3
"""The finding sweep (`run --sweep`) and its cheaper first step (`run --sweep-ahead`).

A run can end with findings nobody acts on in the run: CONFIRMED findings `--qa-threshold` tolerated
(`tasks/<id>/known_issues.md`) and, opted in, PLAUSIBLE findings in passed tasks' `qa.last`. Without a
sweep they reach `forge issues` and are fixed later, if at all. With one, the scheduler calls `plan` once
every task has merged: the open findings become a few fix tasks, one per file, added to the live run.

  plan   <plan> --severity P0..P3 [--plausible] [--max N]
      Collect the open findings (forge-issues.py's collector), triage them, group them by file, write
      each fix task (tasks.tsv row, prompt.md, sweep.json) and the sweep plan (sweep/plan.json and
      plan.md, also printed to stderr), then print the new task ids, one per line. A plan is swept at
      most once: an existing sweep/plan.json prints nothing.
  ahead  <plan> <task> [--role dwarf|qa] [--cap N]
      The prompt step: the open findings in files <task> owns, as a prompt section. Picked once per
      task (tasks/<task>/sweep.ahead.json), so every attempt and its reviewer see the same list.
  settle <plan> <task>
      After a sweep task merged: its reviewer's `S<n>: FIXED` / `S<n>: NOT FIXED` lines, per finding,
      in tasks/<task>/sweep.outcome.json. `forge issues` leaves out the findings marked fixed.
  report <plan>
      What the sweep did, for the run's closing summary; nothing when the plan was never swept.

Triage keeps a finding only when it is CONFIRMED and tolerated (or PLAUSIBLE, with --plausible), at
least as severe as --severity, from a task that is not itself a sweep task (one round: what a sweep
review finds goes to `forge issues`), not dismissed (`forge issues --dismiss`), located in a file the
integration branch still has, and still there: when a later merge changed the file and the line the
reviewer saw is gone from it, the finding is left for `forge issues` rather than "fixed" twice. A
person's acceptance of a reviewer's FAIL (`accept`) is theirs to triage, so accepted findings are never
swept. Every finding left out is listed in the plan with its reason.

Each fix task gets at most GROUP_CAP findings; --max (default DEFAULT_MAX) caps the tasks, worst
severity first. Its implementer is the plan's low tier (the first spec of --dwarf-low, else of
--dwarf), else the implementer of the task the worst finding came from; its reviewer is that task's.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tempfile
import time

SCRIPTS = Path(__file__).resolve().parent
SEVERITIES = ('P0', 'P1', 'P2', 'P3')
DEFAULT_MAX = 5     # fix tasks one sweep may add
GROUP_CAP = 8       # findings one fix task is given; the rest of its file stays for forge issues
AHEAD_CAP = 3       # findings the prompt step hands one task
LOCK_WAIT = 5.0
# A reviewer's line per finding: `- S2: NOT FIXED — why`. A placeholder (`S<n>`) never matches.
S_LINE = re.compile(r'^[ \t]*(?:[-*+][ \t]+)?[*_`]*(S\d{1,3})[*_`]*[ \t]*[:—–-][ \t]*[*_`]*(NOT[ \t]+FIXED|FIXED)\b'
                    r'[*_`]*[ \t]*[:—–-]?[ \t]*(.*)$', re.I | re.M)
REASONS = {
    'below-severity': 'less severe than the sweep threshold',
    'dismissed': 'dismissed with forge issues --dismiss',
    'no-location': 'no file in the repository to fix it in',
    'file-gone': 'its file is no longer on the integration branch',
    'changed': 'the code it points at changed after the review',
    'duplicate': 'the same finding was reported again',
    'group-cap': 'over the %d findings one fix task takes' % GROUP_CAP,
    'over-max': 'over --sweep-max fix tasks',
    'no-model': 'no implementer or reviewer to give its fix task',
}

_ISSUES = None


def issues():
    """forge-issues.py, loaded by path: the one owner of finding collection, keys and dismissals."""
    global _ISSUES
    if _ISSUES is None:
        spec = importlib.util.spec_from_file_location('forge_issues', SCRIPTS / 'forge-issues.py')
        _ISSUES = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_ISSUES)
    return _ISSUES


def read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        return ''


def write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def control_lock(plan: Path):
    """The plan's control.lock, as `forge control` takes it, around a tasks.tsv rewrite. Busy for
    LOCK_WAIT seconds: proceed without it, as the scheduler does."""
    fd = os.open(str(plan / 'control.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    try:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def rows(plan: Path) -> list:
    return [line.split('\t') for line in read(plan / 'tasks.tsv').splitlines()
            if line and not line.startswith('#') and len(line.split('\t')) == 7]


def is_sweep_task(plan: Path, task: str) -> bool:
    return (plan / 'tasks' / task / 'sweep.json').is_file()


def git(cwd, *args) -> tuple:
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True)
    return result.returncode, result.stdout


def blob(cwd, commit: str, path: str) -> 'str | None':
    """The file at commit, or None when it is not a regular file there."""
    rc, kind = git(cwd, 'cat-file', '-t', commit + ':' + path)
    if rc or kind.strip() != b'blob':
        return None
    rc, data = git(cwd, 'show', commit + ':' + path)
    return None if rc else data.decode('utf-8', 'replace')


def relative(path: str, roots: list) -> 'str | None':
    """A finding's file as a repository path: a path inside one of the run's checkouts loses its
    checkout; anything else absolute, or climbing out of the repository, is no location."""
    path = (path or '').strip().strip('`')
    if path.startswith('/'):
        for root in roots:
            if root and path.startswith(root.rstrip('/') + '/'):
                path = path[len(root.rstrip('/')) + 1:]
                break
        else:
            return None
    path = posixpath.normpath(path)
    # a tab, newline or comma would break tasks.tsv's files column
    if path in ('', '.') or path.startswith('../') or path == '..' or any(c in path for c in '\t\n\r,'):
        return None
    return path


def merged_since(cwd, commit: str, tip: str, path: str) -> list:
    """Tasks whose merge onto the integration branch after commit changed path."""
    rc, out = git(cwd, 'log', '--first-parent', '--format=%s', commit + '..' + tip, '--', path)
    if rc:
        return []
    found = []
    for subject in out.decode('utf-8', 'replace').splitlines():
        m = re.fullmatch(r'forge: merge (\S+)', subject.strip())
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found[::-1]


def summary(f) -> str:
    return (f['text'].splitlines() or [''])[0][:200]


def location(f, path=None) -> str:
    where = path or f['file']
    return where + (':%d' % f['line'] if f.get('line') else '')


class Context:
    """What triage reads once per plan: the repository, its checkouts, the dismissal marks."""

    def __init__(self, plan: Path):
        self.plan = plan
        self.repo = read(plan / 'repo')
        wt_root = read(plan / 'wt_root')
        self.integration = os.path.join(wt_root, '_integration') if wt_root else ''
        rc, tip = git(self.integration, 'rev-parse', 'HEAD') if self.integration else (1, b'')
        self.tip = tip.decode().strip() if not rc else ''
        self.roots = [self.integration, self.repo] + [os.path.join(wt_root, r[0]) for r in rows(plan)] if wt_root \
            else [self.repo]
        mod = issues()
        self.slug = mod.github_slug(self.repo) if self.repo else None
        self.dismissed = {}
        if self.slug:
            with contextlib.suppress(Exception):
                self.dismissed = mod.load_dismissed(mod.repo_key(self.repo))

    def candidates(self, include_plausible: bool) -> list:
        found, _ = issues().collect_plan(self.plan, include_plausible)
        return found


def triage(ctx: Context, found: list, severity: str, plausible: bool) -> tuple:
    """(kept, dropped). kept: findings with `path` (their repository path) and `key`; dropped: the
    candidates left out, each with its `reason` and a `detail` (merged_since for `changed`)."""
    mod = issues()
    origins = ('tolerated', 'plausible') if plausible else ('tolerated',)
    cut = SEVERITIES.index(severity)
    kept, dropped, seen = [], [], {}
    tip_text = {}
    for f in sorted(found, key=lambda f: SEVERITIES.index(f['severity'])):
        if f['origin'] not in origins or not f.get('task') or is_sweep_task(ctx.plan, f['task']):
            continue
        f = dict(f, key=mod.finding_key(f))

        def drop(reason, **detail):
            dropped.append(dict(f, reason=reason, detail=detail))

        if SEVERITIES.index(f['severity']) > cut:
            drop('below-severity')
            continue
        if ctx.slug and mod.fingerprint(ctx.slug, f) in ctx.dismissed:
            drop('dismissed')
            continue
        if f['key'] in seen:
            drop('duplicate', of=seen[f['key']])
            continue
        path = relative(f['file'], ctx.roots)
        if path not in tip_text:
            tip_text[path] = blob(ctx.integration, ctx.tip, path) if path and ctx.tip else None
        after = tip_text[path]
        if after is None:
            drop('no-location' if path is None or not f.get('line') else 'file-gone')
            continue
        commit = f.get('commit') or ''
        before = blob(ctx.integration, commit, path) if commit and f.get('line') else None
        if before is not None and before != after and 1 <= f['line'] <= len(before.splitlines()):
            line = before.splitlines()[f['line'] - 1].strip()
            if line and line not in {other.strip() for other in after.splitlines()}:
                drop('changed', merged_since=merged_since(ctx.integration, commit, ctx.tip, path))
                continue
        seen[f['key']] = f['task']
        kept.append(dict(f, path=path))
    return kept, dropped


def groups(kept: list, limit: int) -> tuple:
    """(groups, dropped): one group per file, worst first, each at most GROUP_CAP findings."""
    by_path = {}
    for f in kept:
        by_path.setdefault(f['path'], []).append(f)
    ordered = sorted(by_path.items(), key=lambda item: (min(SEVERITIES.index(f['severity']) for f in item[1]),
                                                         -len(item[1]), item[0]))
    chosen, dropped = [], []
    for path, items in ordered:
        items = sorted(items, key=lambda f: SEVERITIES.index(f['severity']))
        if len(chosen) >= limit:
            dropped += [dict(f, reason='over-max', detail={}) for f in items]
            continue
        chosen.append((path, items[:GROUP_CAP]))
        dropped += [dict(f, reason='group-cap', detail={}) for f in items[GROUP_CAP:]]
    return chosen, dropped


def pools(plan: Path) -> dict:
    try:
        data = json.loads(read(plan / 'fractal-routing.json') or '{}')
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def models(plan: Path, source: 'list | None') -> tuple:
    """(dwarf, qa) for a fix task whose worst finding came from the task with row `source` (None when
    that task is not in tasks.tsv); an empty string where there is no spec to use."""
    def usable(spec):
        return spec if isinstance(spec, str) and spec not in ('', '-', 'UNASSIGNED') else ''
    qa = usable(source[5]) if source else ''
    routing = pools(plan)
    for tier in ('low', 'any'):
        specs = routing.get(tier)
        if isinstance(specs, list) and specs and usable(specs[0]):
            return specs[0], qa
    return (usable(source[4]) if source else ''), qa


def free_ids(taken: set, count: int) -> list:
    ids, n = [], 1
    while len(ids) < count:
        name = 'sweep-%d' % n
        if name not in taken:
            ids.append(name)
        n += 1
    return ids


def finding_line(item: dict) -> str:
    return '- %s [%s][%s] %s — %s  (task %s)' % (item['id'], item['severity'], item['confidence'],
                                                 item['location'], item['text'], item['task'])


def prompt_text(run_id: str, path: str, items: list) -> str:
    lines = ['Fix review findings in `%s`' % path, '',
             'The reviewers of earlier tasks in forge run %s reported these findings and let the work merge' % run_id,
             'anyway (below the run\'s QA threshold, or only PLAUSIBLE). Fix them now, while the work is fresh.', '']
    lines += [finding_line(item) for item in items]
    lines += ['',
              'Line numbers are the ones the reviewer saw; later tasks may have moved the code. Fix each finding',
              'with the smallest change that removes it, and add or adjust a test when it is a behaviour bug and',
              'the code has tests nearby. Leave a finding that no longer applies, or that you judge wrong, alone',
              'and say why in your reply. Change nothing else.', '',
              '## Acceptance criteria', '',
              '- Each finding above is fixed, or the reply says why it no longer applies or is wrong.',
              '- No behaviour outside these findings changes.']
    return '\n'.join(lines) + '\n'


def plan_markdown(record: dict) -> str:
    tasks, dropped = record['tasks'], record['dropped']
    scope = '%s and worse, %s' % (record['severity'], 'CONFIRMED and PLAUSIBLE' if record['plausible'] else 'CONFIRMED only')
    n = sum(len(t['findings']) for t in tasks)
    out = ['# Finding sweep: %d fix task(s) for %d finding(s) (%s)' % (len(tasks), n, scope), '']
    if tasks:
        out += ['Each fix task is one implementation and one narrow review: %d dispatches, plus any automatic'
                % record['estimate']['dispatches'], 'retries (--retry-failed). They run on the integration branch at %s.'
                % (record['integration'][:12] or '?'), '']
    for task in tasks:
        out += ['## %s: %s (implementer %s, reviewer %s)' % (task['id'], task['file'], task['dwarf'], task['qa'])]
        out += [finding_line(item) for item in task['findings']] + ['']
    if dropped:
        out += ['## Left for forge issues (%d)' % len(dropped)]
        for item in dropped:
            why = REASONS.get(item['reason'], item['reason'])
            since = item['detail'].get('merged_since') if item['detail'] else None
            if since:
                why += ' (merged since: %s)' % ', '.join(since)
            out.append('- [%s] %s — %s  (task %s): %s' % (item['severity'], location(item), summary(item), item['task'], why))
        out.append('')
    if not tasks and not dropped:
        out += ['No open finding to sweep.', '']
    return '\n'.join(out)


def cmd_plan(args) -> int:
    plan = Path(args.plan).resolve()
    sweep_dir = plan / 'sweep'
    if (sweep_dir / 'plan.json').exists():
        return 0                                   # one sweep per plan
    ctx = Context(plan)
    if not ctx.tip:
        print('forge: sweep: the integration branch cannot be read; nothing swept', file=sys.stderr)
        return 3
    kept, dropped = triage(ctx, ctx.candidates(args.plausible), args.severity, args.plausible)
    chosen, over = groups(kept, args.max)
    dropped += over
    table = rows(plan)
    by_id = {row[0]: row for row in table}
    ids = free_ids(set(by_id) | {p.name for p in (plan / 'tasks').iterdir() if p.is_dir()}, len(chosen))
    run_id = read(plan / 'run_id') or plan.name
    tasks, new_rows = [], []
    for path, items in list(chosen):
        if not all(models(plan, by_id.get(items[0]['task']))):
            chosen.remove((path, items))
            dropped += [dict(f, reason='no-model', detail={}) for f in items]
    for ident, (path, items) in zip(ids, chosen):
        entries = [dict(id='S%d' % n, key=f['key'], severity=f['severity'], confidence=f['confidence'],
                        origin=f['origin'], task=f['task'], file=f['file'], line=f.get('line'), path=path,
                        location=location(f, path), text=f['text'], raw=f['raw'])
                   for n, f in enumerate(items, 1)]
        dwarf, qa = models(plan, by_id.get(items[0]['task']))
        title = 'Fix %d review finding%s in %s' % (len(entries), '' if len(entries) == 1 else 's', path)
        root = plan / 'tasks' / ident
        root.mkdir(parents=True, exist_ok=True)
        write_atomic(root / 'prompt.md', prompt_text(run_id, path, entries))
        write_atomic(root / 'sweep.json', json.dumps(dict(file=path, findings=entries), indent=1) + '\n')
        tasks.append(dict(id=ident, file=path, dwarf=dwarf, qa=qa, title=title, findings=entries))
        new_rows.append('\t'.join([ident, '-', 'low', path, dwarf, qa, title]))
    record = dict(version=1, at=time.time(), severity=args.severity, plausible=args.plausible, max=args.max,
                  integration=ctx.tip, tasks=tasks, estimate=dict(dispatches=2 * len(tasks)),
                  dropped=[dict(task=f['task'], severity=f['severity'], confidence=f['confidence'], origin=f['origin'],
                                file=f['file'], line=f.get('line'), text=f['text'], key=f['key'], reason=f['reason'],
                                detail=f['detail'])
                           for f in dropped])
    sweep_dir.mkdir(exist_ok=True)
    text = plan_markdown(record)
    write_atomic(sweep_dir / 'plan.md', text)
    # The marker before the rows: a crash in between loses this sweep (its findings still reach forge
    # issues), and never plans a second one over tasks already in tasks.tsv.
    write_atomic(sweep_dir / 'plan.json', json.dumps(record, indent=1) + '\n')
    if new_rows:
        with control_lock(plan):
            current = (plan / 'tasks.tsv').read_text(encoding='utf-8')
            write_atomic(plan / 'tasks.tsv', current + ('' if current.endswith('\n') or not current else '\n')
                         + ''.join(row + '\n' for row in new_rows))
    for line in text.rstrip().splitlines():
        print('forge: sweep: ' + line if line else 'forge: sweep:', file=sys.stderr)
    for task in tasks:
        print(task['id'])
    return 0


def owns(scopes: list, path: str) -> bool:
    return any(s == '.' or path == s or path.startswith(s.rstrip('/') + '/') for s in scopes)


def cmd_ahead(args) -> int:
    plan = Path(args.plan).resolve()
    root = plan / 'tasks' / args.task
    record = root / 'sweep.ahead.json'
    if record.is_file():
        try:
            picked = json.loads(read(record) or '[]')
        except ValueError:
            picked = []
    else:
        row = next((r for r in rows(plan) if r[0] == args.task), None)
        if row is None or is_sweep_task(plan, args.task):
            return 0
        scopes = [posixpath.normpath(p) for p in row[3].split(',') if p and p != '-']
        ctx = Context(plan)
        found = [f for f in ctx.candidates(False)
                 if f['origin'] == 'tolerated' and f.get('task') not in (None, args.task)
                 and (plan / 'tasks' / f['task'] / 'merged').exists() and not is_sweep_task(plan, f['task'])]
        mod, picked, seen = issues(), [], set()
        for f in sorted(found, key=lambda f: SEVERITIES.index(f['severity'])):
            path = relative(f['file'], ctx.roots)
            key = mod.finding_key(f)
            if path is None or not owns(scopes, path) or key in seen:
                continue
            if ctx.slug and mod.fingerprint(ctx.slug, f) in ctx.dismissed:
                continue
            seen.add(key)
            picked.append(dict(key=key, severity=f['severity'], confidence=f['confidence'], task=f['task'],
                               location=location(f, path), text=f['text']))
            if len(picked) >= args.cap:
                break
        write_atomic(record, json.dumps(picked, indent=1) + '\n')
    if not picked:
        return 0
    if args.role == 'dwarf':
        out = ['## Open findings on your files', '',
               'Reviews of tasks merged earlier in this run left these findings open in files this task owns.',
               'Fix one only if your change already goes through that code; otherwise leave it, and do not widen',
               'the task for them. Say in your reply which ones you fixed.', '']
    else:
        out = ['Open findings on this task\'s files were passed to the implementer, to fix only if its change',
               'already went through that code. A fix of one is in scope; leaving one alone is not a finding.']
    out += ['- [%s][%s] %s — %s  (task %s)' % (p['severity'], p['confidence'], p['location'], p['text'], p['task'])
            for p in picked]
    print('\n'.join(out) + '\n')
    return 0


def outcome(text: str, findings: list) -> list:
    """Each finding's status from a sweep review: fixed, not-fixed or unreported (the first S line wins)."""
    said = {}
    for m in S_LINE.finditer(text or ''):
        said.setdefault(m.group(1).upper(), ('not-fixed' if m.group(2).upper().startswith('NOT') else 'fixed',
                                             m.group(3).strip()[:300]))
    return [dict(id=f['id'], key=f['key'], task=f['task'], status=said.get(f['id'], ('unreported', ''))[0],
                 note=said.get(f['id'], ('', ''))[1]) for f in findings]


def cmd_settle(args) -> int:
    root = Path(args.plan).resolve() / 'tasks' / args.task
    try:
        findings = json.loads(read(root / 'sweep.json'))['findings']
    except (ValueError, KeyError, TypeError):
        return 0
    result = dict(at=time.time(), findings=outcome(read(root / 'qa.last'), findings))
    write_atomic(root / 'sweep.outcome.json', json.dumps(result, indent=1) + '\n')
    return 0


def task_state(root: Path) -> str:
    if (root / 'merged').exists():
        return 'MERGED'
    return (read(root / 'status').split() or ['PENDING'])[0]


def cmd_report(args) -> int:
    plan = Path(args.plan).resolve()
    try:
        record = json.loads(read(plan / 'sweep' / 'plan.json'))
    except ValueError:
        return 0
    tasks, fixed, total = record.get('tasks') or [], 0, 0
    lines = []
    for task in tasks:
        root = plan / 'tasks' / task['id']
        state = task_state(root)
        done = 0
        if state == 'MERGED':
            try:
                done = sum(1 for f in json.loads(read(root / 'sweep.outcome.json'))['findings'] if f.get('status') == 'fixed')
            except (ValueError, KeyError, TypeError):
                pass
        fixed += done
        total += len(task['findings'])
        lines.append('  %-14s %-9s %d of %d fixed  %s' % (task['id'], state, done, len(task['findings']), task['file']))
    left = len(record.get('dropped') or []) + total - fixed
    print('finding sweep (%s and worse): %d fix task(s), %d of %d finding(s) fixed; %d left for forge issues'
          % (record.get('severity'), len(tasks), fixed, total, left))
    print('\n'.join(lines + ['  plan: %s' % (plan / 'sweep' / 'plan.md'), '']))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='forge-sweep.py', description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('plan')
    p.add_argument('--severity', choices=SEVERITIES, required=True)
    p.add_argument('--plausible', action='store_true')
    p.add_argument('--max', type=int, default=DEFAULT_MAX)
    a = sub.add_parser('ahead')
    a.add_argument('plan')
    a.add_argument('task')
    a.add_argument('--role', choices=('dwarf', 'qa'), default='dwarf')
    a.add_argument('--cap', type=int, default=AHEAD_CAP)
    s = sub.add_parser('settle')
    s.add_argument('plan')
    s.add_argument('task')
    r = sub.add_parser('report')
    r.add_argument('plan')
    args = parser.parse_args(argv)
    if args.command == 'plan' and args.max < 1:
        parser.error('--max must be at least 1')
    return {'plan': cmd_plan, 'ahead': cmd_ahead, 'settle': cmd_settle, 'report': cmd_report}[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
