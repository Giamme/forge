#!/usr/bin/env python3
"""forge runs rerun / forge runs compare: the same plan under another implementer, side by side.

`forge runs rerun <run-dir> --dwarf SPEC` copies a plan's tasks and prompts into a NEW plan
directory, with SPEC as every task's implementer, and sets it up the way `forge-parallel.sh plan`
leaves a plan: new task worktrees and branches (forge/<new id>/<task>) and a new integration branch
(forge/<new id>-integration) that starts at the commit the original run started from, so both
runs begin from the same code. It dispatches nothing: it prints what a second run would cost (the
forecast when the dataset has history for it) and the command that starts it. `--run --yes`
starts it. Nothing is ever merged: a rerun only writes its own forge/<new id>... branches and never
touches the branch you work on, the original run or its branches.

`forge runs compare <run-dir-A> <run-dir-B>` sets two runs of one repo side by side, per task:
status, attempts, tokens, time and diff size. It reads the plan directories (and the dataset for
tokens and time, when the runs are in it) and, unless `--no-record` is given, records the pair
as one `comparison` dataset event. Nothing routes on it.

One pair of runs is one sample: a task that passed on one model and failed on the other may just
have been the model's day. The numbers say what happened, not which model is better.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))
sys.path.insert(0, str(SKILL / 'ui'))

# Plan options that travel with a rerun, so it is judged the way the original was. Not the ones
# `plan` writes itself (routing, Jev's frozen selection) or that a fresh plan decides (exploration).
OPTION_FILES = ('no_ripwire', 'no_memory', 'yolo_dwarf', 'yolo_qa', 'reviewer_session', 'implementer_session', 'fast',
                'pre_review_tests', 'pre_review_parallel', 'qa_threshold', 'timeout', 'verify_retries', 'verify_cmd',
                'verify_parallel', 'setup_cmd', 'final_qa', 'shadow_qa', 'shadow_rate', 'escalate_after', 'fail_first', 'profile.json',
                'retro', 'brief', 'brief_scope', 'brief_model', 'summaries', 'summaries_model')
PLAN_FILES = ('repo.identity', 'goal.txt', 'request.md')
TASK_FILES = ('prompt.md', 'approach.md')
NAME = re.compile(r'[^A-Za-z0-9._-]+')


class Refusal(Exception):
    def __init__(self, message, code=3):
        super().__init__(message)
        self.code = code


def git(repo, *args):
    out = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return out.returncode, out.stdout.strip()


def text(path: Path) -> str:
    try:
        return path.read_text(errors='replace').strip()
    except OSError:
        return ''


def table(path: Path) -> list:
    """tasks.tsv as dicts: id, deps, difficulty, files, dwarf, qa, title (blank and # lines skipped)."""
    keys = ('id', 'deps', 'difficulty', 'files', 'dwarf', 'qa', 'title')
    rows = []
    for line in text(path).splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        row = dict(zip(keys, cells + [''] * (len(keys) - len(cells))))
        if row['id']:
            rows.append(row)
    return rows


def fractal_enabled(plan: Path) -> bool:
    raw = text(plan / 'fractal-selection.json')
    if not raw:
        return False
    try:
        return json.loads(raw).get('enabled') is not False
    except (ValueError, AttributeError):
        return True


def source(run_dir: str) -> dict:
    """The plan a rerun starts from, or a Refusal saying what is missing."""
    plan = Path(run_dir).resolve()
    if not (plan / 'tasks.tsv').is_file():
        raise Refusal('%s is not a decomposed run: it has no tasks.tsv (a solo run has no task table to rerun)' % plan)
    rows = table(plan / 'tasks.tsv')
    if not rows:
        raise Refusal('%s has an empty tasks.tsv' % plan)
    repo = text(plan / 'repo')
    if not repo or not Path(repo).is_dir():
        raise Refusal("the plan's repository '%s' does not exist" % repo)
    base = text(plan / 'integration.base')
    if not base:
        raise Refusal('%s has no integration.base: that plan never started a run, so there is no starting commit '
                      'to rerun from (use it as it is: forge-parallel.sh run)' % plan)
    if git(repo, 'rev-parse', '--verify', '--quiet', base + '^{commit}')[0] != 0:
        raise Refusal('the commit %s that the run started from is not in %s any more' % (base[:7], repo))
    if fractal_enabled(plan):
        raise Refusal('this plan runs under Fractal, which binds each task to output paths under the plan directory; '
                      'a Fractal-backed plan cannot be rerun')
    running = [r['id'] for r in rows if text(plan / 'tasks' / r['id'] / 'status') == 'RUNNING']
    if running:
        raise Refusal('task %s is RUNNING (or a run was killed); let it finish, or settle it with '
                      '"forge-parallel.sh run %s", before rerunning' % (', '.join(running), plan))
    return dict(plan=plan, rows=rows, repo=repo, base=base, run_id=text(plan / 'run_id') or plan.name)


# --- the dataset, read-only -------------------------------------------------------------

def history():
    """The dataset's Runs, or None when there is no store (reading must not create one)."""
    try:
        from forge_dataset import connect, dataset_dir
        from forge_dataset.export import Runs, Store
        if not (dataset_dir() / 'dataset.sqlite').exists():
            return None
        db = connect()
        try:
            return Runs(Store(db))
        finally:
            db.close()
    except Exception:
        return None


def cost(runs, uid, task) -> tuple:
    """(tokens, seconds) of a task's dwarf and QA calls over every attempt; None when unrecorded."""
    if runs is None or not uid:
        return None, None
    try:
        from forge_dataset.forecast import _cost
        return _cost(runs, uid, task)
    except Exception:
        return None, None


def human_tokens(n):
    if n is None:
        return '-'
    return '%.1fk' % (n / 1000) if n >= 1000 else '%d' % n


def human_time(s):
    if s is None:
        return '-'
    s = int(round(s))
    return '%dm%02ds' % divmod(s, 60) if s >= 60 else '%ds' % s


# --- rerun ------------------------------------------------------------------------------

def slug(spec: str) -> str:
    return NAME.sub('-', spec).strip('-.') or 'rerun'


def target(src: dict, to, spec: str) -> dict:
    new = Path(to).resolve() if to else src['plan'].with_name('%s-%s' % (src['plan'].name, slug(spec)))
    rid = re.sub(r'^forge-', '', new.name)
    repo = src['repo']
    if new.exists():
        raise Refusal('%s already exists; pick another directory with --to' % new)
    if git(repo, 'check-ref-format', '--branch', 'forge/%s-integration' % rid)[0] != 0 \
            or git(repo, 'check-ref-format', '--branch', 'forge/%s/x' % rid)[0] != 0:
        raise Refusal("'%s' cannot be part of a branch name; pick another directory with --to" % rid, 2)
    clash = git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads/forge/%s' % rid,
                'refs/heads/forge/%s-integration' % rid, 'refs/heads/forge/%s/' % rid)[1]
    if clash:
        raise Refusal('branches already exist for run id %s (%s); pick another directory with --to' % (rid, clash.splitlines()[0]))
    wt_root = Path(os.path.dirname(os.path.abspath(repo))) / '.forge-worktrees' / rid
    if wt_root.exists() and any(wt_root.iterdir()):
        raise Refusal("worktree root '%s' already exists; pick another directory with --to" % wt_root)
    return dict(plan=new, run_id=rid, branch='forge/%s-integration' % rid, wt_root=wt_root)


def write_table(path: Path, rows: list) -> None:
    keys = ('id', 'deps', 'difficulty', 'files', 'dwarf', 'qa', 'title')
    body = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
    body += ''.join('\t'.join(r[k] for k in keys) + '\n' for r in rows)
    path.write_text(body)


def copy_plan(src: dict, new: dict, rows: list) -> None:
    plan, out = src['plan'], new['plan']
    out.mkdir(parents=True)
    write_table(out / 'tasks.tsv', rows)
    (out / 'repo').write_text(src['repo'] + '\n')
    (out / 'integration.base').write_text(src['base'] + '\n')
    for name in PLAN_FILES + OPTION_FILES:
        if (plan / name).is_file():
            shutil.copy2(plan / name, out / name)
    if (plan / 'verify.d').is_dir():
        shutil.copytree(plan / 'verify.d', out / 'verify.d')
    for row in rows:
        for name in TASK_FILES:
            if (plan / 'tasks' / row['id'] / name).is_file():
                (out / 'tasks' / row['id']).mkdir(parents=True, exist_ok=True)
                shutil.copy2(plan / 'tasks' / row['id'] / name, out / 'tasks' / row['id'] / name)
    (out / 'rerun-of.txt').write_text('rerun of %s\nrun_id %s\nstarted from %s\n' % (plan, src['run_id'], src['base']))


def forecast_lines(new: Path) -> list:
    try:
        from forge_dataset import forecast
        fc = forecast.build(new)
        if fc and fc['tasks']:
            return forecast.lines(fc, brief=True)
    except Exception:
        pass
    return []


def warning(rows: list, spec: str, src: dict, runs) -> list:
    qas = sorted({r['qa'] for r in rows if r['qa']})
    out = ['This spends a second run\'s tokens: %d task%s on %s, each reviewed by %s (more when one is sent back), '
           'then verification.' % (len(rows), '' if len(rows) == 1 else 's', spec, ', '.join(qas) or 'its reviewer')]
    uid = text(src['plan'] / 'dataset.uid')
    spent = [cost(runs, uid, r['id'])[0] for r in rows]
    if any(v is not None for v in spent):
        out.append('The original run recorded about %s tokens for these tasks (dwarf and QA); another model may use '
                   'more or less.' % human_tokens(sum(v for v in spent if v)))
    return out


def rerun(argv) -> int:
    parser = argparse.ArgumentParser(
        prog='forge runs rerun', description="Copy a finished plan into a new one with another implementer, from the same "
        "starting commit. Dispatches nothing unless --run --yes is given; never merges.")
    parser.add_argument('run_dir', metavar='RUN_DIR', help="the plan's run directory")
    parser.add_argument('--dwarf', required=True, metavar='SPEC', help='the implementer for every task (a registry alias, '
                        'optionally :effort or :effort:harness)')
    parser.add_argument('--qa', metavar='SPEC', help='another reviewer too (default: each task keeps its own, so the comparison '
                        'changes one thing)')
    parser.add_argument('--to', metavar='DIR', help='the new plan directory (default: <run-dir>-<spec>)')
    parser.add_argument('--max-parallel', type=int, metavar='N', help='passed to run (with --run)')
    parser.add_argument('--run', action='store_true', help='start the new plan now; needs --yes')
    parser.add_argument('--yes', action='store_true', help='say that spending a second run\'s tokens is wanted (with --run)')
    parser.add_argument('--dry-run', action='store_true', help='say what would be created and change nothing')
    args = parser.parse_args(argv)
    if args.run and not args.yes:
        parser.error('--run spends a second run\'s tokens: read the forecast first (leave --run off to see it), then add --yes')
    if args.yes and not args.run:
        parser.error('--yes goes with --run')
    if args.max_parallel is not None and args.max_parallel < 1:
        parser.error('--max-parallel must be positive')
    try:
        return rerun_plan(args)
    except Refusal as refusal:
        print('forge runs rerun: %s' % refusal, file=sys.stderr)
        return refusal.code


def rerun_plan(args) -> int:
    from forge_dataset.forecast import registry_models
    src = source(args.run_dir)
    registry = registry_models()
    for label, spec in (('--dwarf', args.dwarf), ('--qa', args.qa)):
        if spec and registry and spec.split(':')[0] not in registry:
            raise Refusal("%s: '%s' is not an alias in the registry (%s)" % (label, spec, ', '.join(sorted(registry))), 2)
    rows = [dict(r, dwarf=args.dwarf, qa=args.qa or r['qa']) for r in src['rows']]
    if all(a['dwarf'] == b['dwarf'] and a['qa'] == b['qa'] for a, b in zip(src['rows'], rows)):
        raise Refusal('every task already ran on %s with that reviewer: a rerun would compare a run with itself' % args.dwarf)
    new = target(src, args.to, args.dwarf)
    runs = history()
    print('rerun of %s (run %s, started from %s)' % (src['plan'], src['run_id'], src['base'][:7]))
    print('  new plan:  %s' % new['plan'])
    print('  branches:  %s and forge/%s/<task>; worktrees in %s' % (new['branch'], new['run_id'], new['wt_root']))
    print('  implementer %s for %d task%s; %s' % (args.dwarf, len(rows), '' if len(rows) == 1 else 's',
                                                  'reviewer %s' % args.qa if args.qa else 'reviewers as in the original'))
    if args.dry_run:
        for line in warning(rows, args.dwarf, src, runs):
            print(line)
        print('(dry run: nothing created, nothing dispatched)')
        return 0
    created = []
    try:
        copy_plan(src, new, rows)
        created.append('plan')
        if git(src['repo'], 'branch', new['branch'], src['base'])[0] != 0:
            raise Refusal('could not create %s' % new['branch'])
        created.append('branch')
        # The source's frozen profile was copied and plan keeps it. A source that froze none must not
        # take the user's default now: it would replace the options just copied.
        profile = [] if (new['plan'] / 'profile.json').is_file() else ['--no-profile']
        planned = subprocess.run(['bash', str(SKILL / 'scripts' / 'forge-parallel.sh'), 'plan', str(new['plan']),
                                  '--explore', '0', *profile], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if planned.returncode != 0:
            raise Refusal('forge-parallel.sh plan failed (%d):\n%s' % (planned.returncode, (planned.stdout + planned.stderr).strip()))
        changed = [r['id'] for r in table(new['plan'] / 'tasks.tsv') if r['dwarf'] != args.dwarf]
        if changed:
            raise Refusal('plan chose another implementer for %s; a head-to-head needs %s on every task' % (', '.join(changed), args.dwarf))
    except BaseException:
        if 'branch' in created:
            git(src['repo'], 'branch', '-D', new['branch'])
        if 'plan' in created:
            shutil.rmtree(new['plan'], ignore_errors=True)
        raise
    print('created. Nothing is dispatched yet.')
    for line in warning(rows, args.dwarf, src, runs):
        print(line)
    shown = forecast_lines(new['plan'])
    for line in shown:
        print(line)
    if not shown:
        print('(no forecast: the dataset has no matching history for %s yet)' % args.dwarf)
    command = 'bash %s run %s --no-forecast%s' % (SKILL / 'scripts' / 'forge-parallel.sh', new['plan'],
                                                  ' --max-parallel %d' % args.max_parallel if args.max_parallel else '')
    if not args.run:
        print('start it:    %s     (or: forge runs rerun ... --run --yes)' % command)
        print('then compare: forge runs compare %s %s' % (src['plan'], new['plan']))
        return 0
    print('starting: %s' % command)
    sys.stdout.flush()
    rc = subprocess.run(command.split(), stdin=subprocess.DEVNULL).returncode
    print('compare: forge runs compare %s %s' % (src['plan'], new['plan']))
    return rc


# --- compare ----------------------------------------------------------------------------

def attempts_of(plan: Path, task: str) -> tuple:
    """(attempts, first attempt's status) from attempts.tsv: ts, number, status, ..."""
    lines = [l.split('\t') for l in text(plan / 'tasks' / task / 'attempts.tsv').splitlines() if l.strip()]
    return len(lines), (lines[0][2] if lines and len(lines[0]) > 2 else None)


def diff_size(side: dict, task: str):
    """(added, deleted) lines on the task's branch since the commit it started from, or None."""
    base = text(side['plan'] / 'tasks' / task / 'base_ref')
    branch = 'forge/%s/%s' % (side['run_id'], task)
    if not base or git(side['repo'], 'rev-parse', '--verify', '--quiet', branch)[0] != 0:
        return None
    code, out = git(side['repo'], 'diff', '--numstat', base, branch)
    if code != 0:
        return None
    added = deleted = 0
    for line in out.splitlines():
        a, d = line.split('\t')[:2]
        added += int(a) if a.isdigit() else 0
        deleted += int(d) if d.isdigit() else 0
    return added, deleted


def load_side(run_dir: str, runs) -> dict:
    from forge_ui import reader
    plan = Path(run_dir).resolve()
    if not (plan / 'tasks.tsv').is_file():
        raise Refusal('%s is not a decomposed run: it has no tasks.tsv' % plan)
    rows = table(plan / 'tasks.tsv')
    statuses = {t['id']: t['status'] for t in reader.summarize(plan).get('task_statuses') or []}
    side = dict(plan=plan, run_id=text(plan / 'run_id') or plan.name, repo=text(plan / 'repo'), uid=text(plan / 'dataset.uid') or None,
                base=text(plan / 'integration.base') or None, tasks={})
    for row in rows:
        tid = row['id']
        n, first = attempts_of(plan, tid)
        tokens, seconds = cost(runs, side['uid'], tid)
        side['tasks'][tid] = dict(task=tid, tier=row['difficulty'], dwarf=row['dwarf'], qa=row['qa'],
                                  status=statuses.get(tid) or text(plan / 'tasks' / tid / 'status') or 'NOT RUN',
                                  attempts=n, first=first, tokens=tokens, seconds=seconds,
                                  diff=diff_size(side, tid))
    return side


def summary(a: dict, b: dict) -> dict:
    ids = [t for t in a['tasks'] if t in b['tasks']]
    out = dict(tasks=len(ids), only_a=[t for t in a['tasks'] if t not in b['tasks']],
               only_b=[t for t in b['tasks'] if t not in a['tasks']], both_merged=[], merged_only_a=[], merged_only_b=[], neither=[])
    for t in ids:
        ma, mb = a['tasks'][t]['status'] == 'MERGED', b['tasks'][t]['status'] == 'MERGED'
        out['both_merged' if ma and mb else 'merged_only_a' if ma else 'merged_only_b' if mb else 'neither'].append(t)
    for label, side in (('a', a), ('b', b)):
        rows = [side['tasks'][t] for t in ids]
        out[label] = dict(first_try=sum(1 for r in rows if r['first'] == 'PASS'), attempts=sum(r['attempts'] for r in rows),
                          tokens=sum(r['tokens'] for r in rows if r['tokens'] is not None) if any(r['tokens'] is not None for r in rows) else None,
                          tokens_known=sum(1 for r in rows if r['tokens'] is not None),
                          seconds=sum(r['seconds'] for r in rows if r['seconds'] is not None) if any(r['seconds'] is not None for r in rows) else None)
    return out


def cell(a, b, fmt=str):
    return '%s → %s' % (fmt(a) if a is not None else '-', fmt(b) if b is not None else '-')


def diff_text(d):
    return '-' if d is None else '+%d/-%d' % d


def report(a: dict, b: dict, s: dict) -> list:
    out = ['forge runs compare']
    for label, side in (('A', a), ('B', b)):
        specs = sorted({t['dwarf'] for t in side['tasks'].values()})
        out.append('  %s  %s  (%s; %d task%s)  %s' % (label, side['run_id'], ', '.join(specs) or '?', len(side['tasks']),
                                                     '' if len(side['tasks']) == 1 else 's', side['plan']))
    if a['base'] and b['base'] and a['base'] != b['base']:
        out.append('  note: the runs started from different commits (%s and %s), so the tasks did not see the same code' % (a['base'][:7], b['base'][:7]))
    rows = [('task', 'tier', 'status A → B', 'attempts', 'tokens', 'time', 'diff lines')]
    for t in [t for t in a['tasks'] if t in b['tasks']]:
        x, y = a['tasks'][t], b['tasks'][t]
        rows.append((t, x['tier'] or '-', cell(x['status'], y['status']), cell(x['attempts'], y['attempts']),
                     cell(x['tokens'], y['tokens'], human_tokens), cell(x['seconds'], y['seconds'], human_time),
                     cell(x['diff'], y['diff'], diff_text)))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out.append('')
    for r in rows:
        out.append('  ' + '  '.join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    for label, ids in (('only in A', s['only_a']), ('only in B', s['only_b'])):
        if ids:
            out.append('  %s: %s' % (label, ', '.join(ids)))
    out.append('')
    out.append('  %d task%s in both: merged in both %d, only A %d, only B %d, neither %d' % (
        s['tasks'], '' if s['tasks'] == 1 else 's', len(s['both_merged']), len(s['merged_only_a']), len(s['merged_only_b']), len(s['neither'])))
    out.append('  first try PASS: A %d, B %d; attempts: A %d, B %d' % (s['a']['first_try'], s['b']['first_try'], s['a']['attempts'], s['b']['attempts']))
    if s['a']['tokens'] is not None or s['b']['tokens'] is not None:
        out.append('  tokens: %s; time: %s' % (cell(s['a']['tokens'], s['b']['tokens'], human_tokens),
                                               cell(s['a']['seconds'], s['b']['seconds'], human_time)))
        if s['tasks'] and (s['a']['tokens_known'] < s['tasks'] or s['b']['tokens_known'] < s['tasks']):
            out.append('  (totals cover only the tasks the dataset recorded)')
    else:
        out.append('  tokens and time: not in the dataset for these runs')
    out.append('  One pair of runs is one sample: a different outcome can be the model\'s day, not the model.')
    return out


def record_pair(a: dict, b: dict, s: dict) -> str:
    """Write the comparison as one dataset event; return a line saying what happened, or ''."""
    missing = [label for label, side in (('A', a), ('B', b)) if not side['uid']]
    if missing:
        return 'not recorded: run %s has no dataset uid (it has not run yet)' % ' and '.join(missing)
    idle = [label for label, side in (('A', a), ('B', b)) if not any(t['attempts'] for t in side['tasks'].values())]
    if idle:
        return 'not recorded: run %s has not attempted a task yet' % ' and '.join(idle)
    from forge_dataset import record
    body = dict(a=dict(run=a['uid'], run_id=a['run_id'], base=a['base'], tasks=a['tasks']),
                b=dict(run=b['uid'], run_id=b['run_id'], base=b['base'], tasks=b['tasks']), summary=s)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]
    event = dict(rid='comparison:%s:%s:%s' % (a['uid'], b['uid'], digest), kind='comparison', ts=time.time(), src='live',
                 run=b['uid'], run_id=b['run_id'], mode='parallel', task=None, data=body)
    added = record([event], repo=b['repo'] or None, where='comparison')
    return 'recorded in the dataset as a comparison event' if added else ''


def compare(argv) -> int:
    parser = argparse.ArgumentParser(prog='forge runs compare', description='Two runs of one plan, side by side, per task. '
                                     'Records the pair in the dataset unless --no-record.')
    parser.add_argument('run_a', metavar='RUN_DIR_A')
    parser.add_argument('run_b', metavar='RUN_DIR_B')
    parser.add_argument('--json', action='store_true', help='print the comparison as JSON')
    parser.add_argument('--no-record', action='store_true', help='do not write the comparison event to the dataset')
    args = parser.parse_args(argv)
    try:
        runs = history()
        a, b = load_side(args.run_a, runs), load_side(args.run_b, runs)
        if a['plan'] == b['plan']:
            raise Refusal('both arguments are the same run', 2)
        if a['repo'] and b['repo'] and os.path.realpath(a['repo']) != os.path.realpath(b['repo']):
            raise Refusal('the runs belong to different repositories (%s and %s)' % (a['repo'], b['repo']))
        s = summary(a, b)
        if not s['tasks']:
            raise Refusal('the runs have no task in common')
    except Refusal as refusal:
        print('forge runs compare: %s' % refusal, file=sys.stderr)
        return refusal.code
    note = '' if args.no_record else record_pair(a, b, s)
    if args.json:
        json.dump(dict(a=a, b=b, summary=s), sys.stdout, indent=2, default=str)
        sys.stdout.write('\n')
        return 0
    for line in report(a, b, s):
        print(line)
    if note:
        print('  ' + note)
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ['rerun']:
        return rerun(argv[1:])
    if argv[:1] == ['compare']:
        return compare(argv[1:])
    print('usage: forge-compare.py rerun|compare ...', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
