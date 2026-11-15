#!/usr/bin/env python3
"""forge worktrees rm PATH... [--branches] [--dry-run] [--json] [--actor A]

Remove worktrees by the rules of forge-ui's Worktrees screen (#147): the statuses come from
ui/forge_ui/worktrees.py `compute`, the one rule set, never a copy of it.

  failed / finished   `git -C <repo> worktree remove <path>`, without the force flag. With --branches
                      a `finished` worktree whose branch is merged also gets the safe branch delete
                      (`git branch -d <branch>`, which refuses an unmerged branch).
  unknown + leftover  the folder is deleted (shutil.rmtree), after checking again that git does
                      not list it as a worktree.
  anything else       skipped with a reason: in_use, dirty, locked, needed, missing (run
                      `git worktree prune`), foreign, removed, and a path that is no row.

Each removal is recorded the way `forge runs rm` records one: a `worktree_removed` dataset event
(with the actor) and a `gone` entry in forge's location list.

--dry-run computes the same actions and changes nothing (`would-remove`).
--actor is who asked: `cli` (the default) or `ui`.

--json prints exactly one object on stdout, on failure too:
  {"ok": bool, "dry_run": bool,
   "rows": [{"path", "action": "remove"|"remove+branch"|"delete-folder"|"none",
             "status": "removed"|"skipped"|"failed"|"would-remove", "reason": str|null}]}

Exit codes: 0 no row failed (a skip is not a failure), 4 a row failed, 2 usage.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

SKILL = Path(__file__).resolve().parent.parent
# `-I` keeps the script's own directory off sys.path, so both are added by hand (as forge-ui.py does).
sys.path.insert(0, str(SKILL / 'scripts'))
sys.path.insert(0, str(SKILL / 'ui'))

import forge_worktrees  # noqa: E402
from forge_dataset import harvest  # noqa: E402
from forge_ui import reader, worktrees  # noqa: E402

OK, USAGE, FAILED = 0, 2, 4
ACTORS = ('cli', 'ui')
HISTORY_TIMEOUT_S = 120

SKIPS = {
    'in_use': 'its run is active',
    'dirty': 'it has uncommitted changes',
    'locked': 'it is locked by git',
    'needed': 'the integration worktree of a run not integrated yet',
    'missing': 'its folder is gone: run git worktree prune',
    'foreign': "not forge's worktree",
    'removed': 'already removed',
    'unknown': 'a folder git does not list, and not a leftover: look inside before deleting it',
}


def load_runs_rm():
    """scripts/forge-runs-rm.py as a module: the recording of a removal is its code, reused."""
    path = SKILL / 'scripts' / 'forge-runs-rm.py'
    spec = importlib.util.spec_from_file_location('forge_runs_rm_for_worktrees', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def history():
    """`forge dataset worktrees --json` through this checkout's launcher, or None."""
    try:
        done = subprocess.run([str(SKILL / 'forge'), 'dataset', 'worktrees', '--json'], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=HISTORY_TIMEOUT_S)
        if done.returncode != 0:
            return None
        found = json.loads(done.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return found if isinstance(found, dict) else None


def real(path):
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def first_line(text):
    lines = (text or '').strip().splitlines()
    return lines[0] if lines else ''


def plan(path, row, branches):
    """(action, skip reason) for one path: what would be done to it, or why nothing is."""
    if row is None:
        return 'none', 'not a worktree forge knows'
    status = row['status']
    if status in ('failed', 'finished'):
        if not (row.get('git_dir') or row.get('repo')):
            return 'none', 'its repository is not known'
        if branches and status == 'finished' and row.get('merged') and row.get('branch'):
            return 'remove+branch', None
        return 'remove', None
    if status == 'unknown' and row.get('leftover'):
        return 'delete-folder', None
    return 'none', SKIPS.get(status, 'status %s' % status)


def is_registered(repo, path):
    """True when git lists `path` as a worktree of `repo`; True too when git cannot say (never delete then)."""
    code, out = worktrees._git(repo, 'worktree', 'list', '--porcelain', '-z')
    z = True
    if code == 129:
        code, out = worktrees._git(repo, 'worktree', 'list', '--porcelain')
        z = False
    if code != 0:
        return True
    return any(real(wt['path']) == real(path) for wt in forge_worktrees.parse_porcelain(out, z=z))


def record(rm, row, actor):
    """The dataset event and the location-list entry of a removal, as `forge runs rm` writes them."""
    by = 'forge worktrees rm (%s)' % actor
    run_path = row.get('run_path')
    try:
        uid = harvest.run_uid(Path(run_path), create=False) if run_path else None
    except Exception:   # noqa: BLE001  -- an identity we cannot read only costs the event its run column
        uid = None
    run = SimpleNamespace(uid=uid, run_id=row.get('run_id'), mode='parallel', path=run_path or row['path'],
                          repo=row.get('repo'))
    kind = row.get('kind') or 'task'
    event = rm.event(run, 'worktree_removed', path=row['path'], branch=row.get('branch') or '-', kind=kind,
                     repo_path=row.get('repo'), by=by, actor=actor)
    if kind == 'task' and row.get('branch'):
        event['task'] = row['branch'].split('/', 2)[-1]
    elif row.get('task'):
        event['task'] = row['task']
    rm.record([event], repo=row.get('repo'), where='event:worktree_removed')
    rm.forge_locations.gone('worktree', row['path'], by=by)


def act(rm, row, action, actor):
    """Do one planned action. Returns None on success, else the failure text."""
    path = row['path']
    repo = row.get('git_dir') or row.get('repo')
    if action == 'delete-folder':
        if worktrees._layout(path) is None:
            return 'not inside a .forge-worktrees folder'
        if not worktrees._leftover(path):
            return 'it is a worktree again: not deleted'
        if row.get('repo') and is_registered(row['repo'], path):
            return 'git lists it as a worktree: not deleted'
        try:
            shutil.rmtree(path)      # never follows a symlink: it refuses one
        except OSError as error:
            return '%s: %s' % (path, error.strerror or error)
        record(rm, row, actor)
        return None
    code, _, err = rm.git(repo, 'worktree', 'remove', path)
    if code != 0:
        return first_line(err) or 'git worktree remove exit %d' % code
    record(rm, row, actor)
    if action == 'remove+branch':
        code, _, err = rm.git(row.get('repo') or repo, 'branch', '-d', row['branch'])
        if code != 0:
            return 'worktree removed, but the branch stays: ' + (first_line(err) or 'git branch -d exit %d' % code)
    return None


def run(args):
    paths = []
    for given in args.paths:
        if real(given) not in {real(p) for p in paths}:
            paths.append(given)
    rows, error = {}, None
    try:
        computed = worktrees.compute(reader.Catalog(), history())
        rows = {real(r['path']): r for r in computed['rows']}
        if not computed['rows'] and any('could not be loaded' in n for n in computed.get('notes') or []):
            error = first_line(computed['notes'][0])
    except Exception as exc:   # noqa: BLE001  -- reported per row, still one JSON object
        error = 'could not compute the worktree states: %s' % exc
    out = []
    rm = None
    for given in paths:
        if error:
            out.append(dict(path=given, action='none', status='failed', reason=error))
            continue
        row = rows.get(real(given))
        action, why = plan(given, row, args.branches)
        if action == 'none':
            out.append(dict(path=given, action='none', status='skipped', reason=why))
        elif args.dry_run:
            out.append(dict(path=given, action=action, status='would-remove', reason=None))
        else:
            try:
                rm = rm or load_runs_rm()
                failure = act(rm, row, action, args.actor)
            except Exception as exc:   # noqa: BLE001  -- one row's failure never stops the others
                failure = str(exc) or exc.__class__.__name__
            out.append(dict(path=given, action=action, status='failed' if failure else 'removed', reason=failure))
    return dict(ok=not any(r['status'] == 'failed' for r in out), dry_run=bool(args.dry_run), rows=out)


def show(result):
    for r in result['rows']:
        extra = '' if r['action'] == 'none' else ' (%s)' % r['action']
        print('%-12s %s%s%s' % (r['status'], r['path'], extra, ': ' + r['reason'] if r['reason'] else ''))


def parser():
    top = argparse.ArgumentParser(prog='forge worktrees', description='Worktrees forge created, by forge-ui\'s rules.')
    sub = top.add_subparsers(dest='command', required=True, metavar='{rm}')
    rm = sub.add_parser('rm', help='remove worktrees (a dirty, locked or live one is skipped, nothing is forced)',
                        description='Remove worktrees by the rules of forge-ui\'s Worktrees screen. Exit 0 when no row '
                                    'failed, 4 when one did, 2 for usage.')
    rm.add_argument('paths', nargs='+', metavar='PATH')
    rm.add_argument('--branches', action='store_true', help='also delete the merged branch of a finished worktree (git branch -d)')
    rm.add_argument('--dry-run', action='store_true', help='say what would be removed, change nothing')
    rm.add_argument('--json', action='store_true', help='print one JSON object: ok, dry_run, rows')
    rm.add_argument('--actor', default='cli', choices=ACTORS, help='who asked (recorded with the removal; default cli)')
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    result = run(args)
    if args.json:
        print(json.dumps(result))
    else:
        show(result)
    return OK if result['ok'] else FAILED


if __name__ == '__main__':
    sys.exit(main())
