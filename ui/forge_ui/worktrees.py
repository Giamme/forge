"""The Worktrees screen (#125): every worktree forge created, in any repository it knows, with its state and
the command that removes it safely.

Read-only: the commands are text to copy, never run, so the screen works with --read-only too. Who owns a
worktree is forge's own rule (scripts/forge_worktrees.py, which `forge runs rm` uses), loaded read-only the
way findings.py loads forge-contract.py, never a second rule.

Sources, merged by worktree path:
  git        `git worktree list --porcelain -z` in every repository forge-ui knows: the roots of the runs it
             lists and the repositories the dataset names. Then `git status` per forge worktree on disk, one
             `git show` per repository for the last commits, and `for-each-ref --merged` for merged branches.
  disk       folders under each `.forge-worktrees` that no repository lists (unknown to git).
  run dirs   each plan's tasks, statuses and whether its scheduler runs now.
  dataset    `forge dataset worktrees --json` (through the CLI): created and removed events, and the tasks with
             an attempt, which name worktrees from before the events were recorded (derived rows, no dates).

Cost: none of this happens on the normal poll. The screen asks /api/worktrees when it opens and at a slow
interval; the result is cached for CACHE_S and recomputed sooner only on ?refresh=1. Sizes are walked in one
background thread and fill in later (`sizes_pending`).

Row statuses: in_use, dirty, locked, needed, failed, finished, missing, foreign, unknown, removed.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shlex
import stat
import subprocess
import threading
import time

from forge_ui import reader

RULES = Path(__file__).resolve().parents[2] / 'scripts' / 'forge_worktrees.py'
CACHE_S = 60
_GIT_TIMEOUT = 10
_SIZE_BUDGET_S = 120           # one tree's walk stops here and its size is a lower bound
_MAX_ROWS = 5000
STATUSES = ('in_use', 'dirty', 'locked', 'needed', 'failed', 'finished', 'missing', 'foreign', 'unknown', 'removed')
_ON_DISK = ('in_use', 'dirty', 'locked', 'needed', 'failed', 'finished', 'foreign', 'unknown')
_STATE = {}
_LOCK = threading.Lock()


class _ReadOnlyLoader(importlib.machinery.SourceFileLoader):
    def set_data(self, *args, **kwargs):
        pass  # no .pyc beside forge's scripts


def rules():
    """forge's ownership rule module (scripts/forge_worktrees.py), or None when it cannot be loaded."""
    with _LOCK:
        if 'rules' not in _STATE:
            try:
                spec = importlib.util.spec_from_file_location(
                    'forge_worktrees_for_ui', RULES, loader=_ReadOnlyLoader('forge_worktrees_for_ui', str(RULES)))
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if not (callable(module.owner) and callable(module.parse_porcelain)):
                    raise TypeError('not the forge worktree rule module')
                _STATE['rules'] = module
            except Exception:
                _STATE['rules'] = None
        return _STATE['rules']


_LAST_ERROR = threading.local()


def _git(cwd, *args, timeout=_GIT_TIMEOUT):
    """(returncode, stdout text) of `git -C cwd <args>`, or (None, '') when git could not run. The first line of
    what git said on stderr is kept in _LAST_ERROR.text, for a note when a listing fails."""
    _LAST_ERROR.text = ''
    try:
        done = subprocess.run(['git', '--no-optional-locks', '-C', cwd, *args], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
                              env=dict(os.environ, GIT_TERMINAL_PROMPT='0', LC_ALL='C'))
    except subprocess.TimeoutExpired:
        _LAST_ERROR.text = 'git took over %d s' % timeout
        return None, ''
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        _LAST_ERROR.text = str(exc)
        return None, ''
    if done.returncode:
        _LAST_ERROR.text = done.stderr.decode('utf-8', 'replace').strip().split('\n')[0][:300]
    return done.returncode, done.stdout.decode('utf-8', 'surrogateescape')


def _isdir(path):
    """An absolute path to a directory, through symlinks: a repository is often reached through one."""
    try:
        return os.path.isabs(path) and os.path.isdir(path)
    except (OSError, ValueError):
        return False


def _leftover(path):
    """True when the folder is no git worktree any more: no `.git`, or a `.git` file whose gitdir is gone."""
    dotgit = os.path.join(path, '.git')
    try:
        if not os.path.lexists(dotgit):
            return True
        if os.path.isdir(dotgit):
            return False
        with open(dotgit, 'r', encoding='utf-8', errors='replace') as stream:
            line = stream.read(4096).strip()
    except (OSError, ValueError):
        return False
    if not line.startswith('gitdir:'):
        return False
    target = line[len('gitdir:'):].strip()
    target = target if os.path.isabs(target) else os.path.join(path, target)
    return not os.path.exists(target)


def _exists(path):
    try:
        return os.path.lexists(path)
    except (OSError, ValueError):
        return False


def _real(path):
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def quote(text):
    return shlex.quote(str(text))


def commands(row):
    """What to copy for a row: [{label, command}]. Never --force; paths quoted for the shell."""
    st, repo, path, branch = row['status'], row.get('git_dir') or row.get('repo'), row['path'], row.get('branch')
    if st == 'dirty':
        return [dict(label='see what is uncommitted', command='git -C %s status' % quote(path))]
    if st == 'missing' and repo:
        return [dict(label='forget the missing folder', command='git -C %s worktree prune' % quote(repo))]
    if st == 'unknown' and row.get('leftover'):
        return [dict(label='delete the leftover folder: git no longer knows it as a worktree (look inside first)',
                     command='rm -rf -- %s' % quote(path))]
    if st in ('failed', 'finished') and repo:
        out = [dict(label='remove the worktree', command='git -C %s worktree remove %s' % (quote(repo), quote(path)))]
        if st == 'finished' and branch and row.get('merged'):
            out.append(dict(label='delete its merged branch', command='git -C %s branch -d %s' % (quote(repo), quote(branch))))
        return out
    return []


def _note(row):
    st = row['status']
    if st == 'in_use':
        return 'its run %s is active' % (row.get('run_id') or '')
    if st == 'locked':
        return ('locked by git' + (': ' + row['locked'] if row.get('locked') else '')
                + ('' if os.path.lexists(row['path']) else '; its folder is gone'))
    if st == 'needed':
        return 'the integration worktree of a run not integrated yet'
    if st == 'failed':
        return ('the branch stays, so a retry can recreate the worktree, but it runs the worktree setup again'
                if row.get('branch') else 'not merged')
    if st == 'foreign':
        return row.get('why') or "not forge's: it fails forge's ownership rule"
    if st == 'unknown':
        if row.get('leftover'):
            return 'a folder left where a worktree was: git no longer knows it, and nothing of forge reads it'
        return row.get('why') or 'a folder git does not list as a worktree'
    if st == 'removed':
        if row.get('derived'):
            return 'derived from the run dir or the dataset; removed at an unknown date'
        return 'removed' if row.get('removed') else 'removed outside forge, at an unknown date'
    return ''


# ---- the run dirs ------------------------------------------------------------------------------

def _runs(catalog):
    """[{key, path, run_id, repo, root, tasks: {id: status}, live, integration}] for each plan forge-ui lists."""
    out = []
    try:
        listed = catalog.runs()
    except Exception:
        return out
    for run in listed:
        if run.get('kind') != 'plan' or not run.get('repo') or not run.get('run_id'):
            continue
        read = reader._Read(run['path'])
        try:
            live = reader._scheduler(read)['live']
        except Exception:
            live = False
        tasks = {t['id']: t['status'] for t in run.get('task_statuses') or [] if t.get('id')}
        live = live or any(s == 'RUNNING' for s in tasks.values())
        out.append(dict(key=run['key'], path=run['path'], run_id=run['run_id'], repo=run['repo'],
                        root=run.get('repo_root') or run['repo'], tasks=tasks, live=live,
                        integration=read.exists('accepted.integration') or read.exists('integration.base')))
    return out


# ---- the computation ---------------------------------------------------------------------------

def _repo_listing(R, repo, failures=None):
    """(main working tree, [worktree records]) of the repository holding `repo`, or (None, []). A failure is added
    to `failures` as {repo: why}."""
    code, out = _git(repo, 'worktree', 'list', '--porcelain', '-z')
    z = True
    if code == 129:
        code, out = _git(repo, 'worktree', 'list', '--porcelain')
        z = False
    if code != 0:
        if failures is not None:
            failures[repo] = _LAST_ERROR.text or ('exit %s' % code if code is not None else 'git could not run')
        return None, []
    found = R.parse_porcelain(out, z=z)
    return (found[0]['path'], found) if found else (None, [])


def _forge_folders(folders):
    """The worktree folders on disk under each `.forge-worktrees` folder: `<folder>/<run id>/<name>` and
    `<folder>/<name>` that hold a `.git` whose repository is still there (a leftover is no repository to ask)."""
    out = []
    for folder in sorted(folders):
        for first in _children(folder):
            if _exists(os.path.join(first, '.git')):
                out += [] if _leftover(first) else [first]
                continue
            out += [second for second in _children(first) if _exists(os.path.join(second, '.git')) and not _leftover(second)]
    return out


def _layout(path):
    """(the `.forge-worktrees` folder, run id, name) when `path` is `<x>/.forge-worktrees/<run id>/<name>`,
    (folder, None, name) when it sits directly in that folder, else None."""
    real = _real(path)
    parent, name = os.path.split(real)
    grand, run_id = os.path.split(parent)
    if os.path.basename(grand) == '.forge-worktrees':
        return grand, run_id, name
    if os.path.basename(parent) == '.forge-worktrees':
        return parent, None, name
    return None


def compute(catalog, history):
    """The screen's rows, without sizes. `history` is the dataset's answer (a dict) or None."""
    R = rules()
    if R is None:
        return dict(rows=[], runs=[], repos=[], notes=["forge's worktree rule (scripts/forge_worktrees.py) could not be loaded"],
                    dataset=None, computed=time.time())
    hist = history if isinstance(history, dict) else None
    runs = _runs(catalog)
    by_root = {}                     # real wt_root -> run
    for run in runs:
        by_root[R.wt_root(run['repo'], run['run_id'])] = run
    integrated = {(_real(i['repo_path']), i['run_id']) for i in (hist or {}).get('integrated') or []
                  if isinstance(i, dict) and i.get('repo_path') and i.get('run_id')}
    dataset_tasks = {}               # (real wt_root) -> set of task ids with an attempt
    for a in (hist or {}).get('attempts') or []:
        if isinstance(a, dict) and a.get('repo_path') and a.get('run_id') and a.get('task'):
            dataset_tasks.setdefault((R.wt_root(a['repo_path'], a['run_id']), a['repo_path'], a['run_id']), set()).add(a['task'])
    events = [e for e in (hist or {}).get('events') or [] if isinstance(e, dict) and isinstance(e.get('path'), str)]

    # every repository to ask: run checkouts and roots, then the dataset's
    candidates = []
    for run in runs:
        candidates += [run['root'], run['repo']]
    for r in (hist or {}).get('repos') or []:
        if isinstance(r, dict) and isinstance(r.get('path'), str):
            candidates.append(r['path'])
    for e in events:
        if e.get('repo_path'):
            candidates.append(e['repo_path'])
    # Then every worktree folder on disk, asked through its own .git: a checkout the dataset names may have
    # been deleted by hand, and git answers for the whole repository from any of its worktrees.
    known_folders = set()
    for repo in candidates:
        if repo and os.path.isabs(repo):
            known_folders.add(os.path.join(os.path.dirname(_real(repo)), '.forge-worktrees'))
    for e in events:
        where = _layout(e['path']) if os.path.isabs(e['path']) else None
        if where:
            known_folders.add(where[0])
    for (wroot, _repo, _rid) in dataset_tasks:
        known_folders.add(os.path.dirname(wroot))
    candidates += _forge_folders(known_folders)
    roots, listed, asked, failures = {}, {}, set(), {}
    for repo in candidates:
        if not repo or not _isdir(repo) or _real(repo) in asked:
            continue
        root, found = _repo_listing(R, repo, failures)
        for wt in found:
            asked.add(_real(wt['path']))
        asked.add(_real(repo))
        if root is None or _real(root) in roots:
            continue
        roots[_real(root)] = dict(root=root, worktrees=found)
        for wt in found:
            listed[_real(wt['path'])] = (root, wt)

    rows = {}

    def tasks_of(root_key, repo, run_id):
        run = by_root.get(root_key)
        if run is not None:
            return set(run['tasks']), run
        out = set()
        for (wroot, _repo, _rid), ids in dataset_tasks.items():
            if wroot == root_key:
                out |= ids
        for e in events:
            if e.get('run_id') == run_id and e.get('task') and e.get('repo_path') and R.wt_root(e['repo_path'], run_id) == root_key:
                out.add(e['task'])
        return out, None

    # merged branches per repository: into HEAD (for `branch -d`), and task branches into each integration branch
    merged_head = {}
    for key, info in roots.items():
        code, out = _git(info['root'], 'for-each-ref', '--merged', 'HEAD', '--format=%(refname)', 'refs/heads/forge/')
        merged_head[key] = set(out.split()) if code == 0 else set()

    pending_status = []
    heads = {}
    for real, (root, wt) in listed.items():
        where = _layout(wt['path'])
        if where is None or wt.get('bare'):
            continue
        folder, run_id, name = where
        root_key = _real(root)
        row = dict(path=wt['path'], repo=root, git_dir=root, run=None, run_id=run_id, run_path=None, task=None,
                   branch=(wt['branch'] or '').replace('refs/heads/', '', 1) or None, kind=None, status=None,
                   merged=None, head=wt.get('head'), commit=None, created=None, removed=None, derived=False,
                   locked=wt.get('locked'), size=None, why=None)
        if run_id is None:
            row.update(status='foreign', why="not forge's: not inside a run's folder of .forge-worktrees")
        else:
            wroot = os.path.join(folder, run_id)
            ids, run = tasks_of(wroot, root, run_id)
            if wt['branch'] == 'refs/heads/forge/%s/%s' % (run_id, name):
                # forge names a task's worktree and branch after it: a task split or dropped since is still the run's
                ids = set(ids) | {name}
            kind = R.owner(wt['path'], wt['branch'], run_id, ids, wroot)
            if run is not None:
                row.update(run=run['key'], run_path=run['path'])
            row['kind'] = kind
            if kind == 'task':
                row['task'] = row['branch'].split('/', 2)[-1]
            if not os.path.lexists(wt['path']) or wt.get('prunable') is not None:
                # git keeps a locked worktree whose folder is gone (an unplugged disk): prune would not forget it
                row['status'] = 'locked' if row['locked'] is not None else 'missing'
            elif kind is None:
                row.update(status='foreign', why="not forge's: " + ("its branch is not one of the run's" if run is not None or ids
                                                                    else 'no run dir or dataset record names its run'))
            elif run is not None and run['live']:
                row['status'] = 'in_use'
            else:
                pending_status.append(row)
            if row['branch']:
                row['merged'] = ('refs/heads/' + row['branch']) in merged_head.get(root_key, set())
        if row['head']:
            heads.setdefault(root, set()).add(row['head'])
        rows[real] = row

    for row in pending_status:
        code, out = _git(row['path'], 'status', '--porcelain', '--untracked-files=normal')
        if code != 0 or out.strip():
            row['status'] = 'dirty'
            if code != 0:
                row['why'] = 'git status failed, treated as uncommitted changes'
        elif row['locked'] is not None:
            row['status'] = 'locked'
        else:
            run = by_root.get(_real(os.path.dirname(row['path'])))
            row['status'] = _settled(row, run, integrated, roots)

    # the last commit of each worktree, one call per repository
    for root, shas in heads.items():
        code, out = _git(root, 'show', '-s', '--format=%H%x00%ct%x00%s%x1e', *sorted(shas))
        if code != 0:
            continue
        info = {}
        for record in out.split('\x1e'):
            parts = record.strip('\n').split('\x00')
            if len(parts) == 3 and parts[0]:
                try:
                    info[parts[0]] = dict(sha=parts[0], time=int(parts[1]), subject=parts[2][:200])
                except ValueError:
                    pass
        for row in rows.values():
            if row['repo'] == root and row['head'] in info:
                row['commit'] = info[row['head']]

    # folders git does not list
    folders = set()
    for info in roots.values():
        for wt in info['worktrees']:
            folders.add(os.path.join(os.path.dirname(_real(wt['path'])), '.forge-worktrees'))
            where = _layout(wt['path'])
            if where:
                folders.add(where[0])
    for run in runs:
        folders.add(os.path.join(os.path.dirname(_real(run['repo'])), '.forge-worktrees'))
    folders |= {_real(f) for f in known_folders if _isdir(f)}
    for folder in sorted(folders):
        for first in _children(folder):
            if _real(first) in listed:
                continue
            if _exists(os.path.join(first, '.git')):
                _unknown(rows, first, None, by_root)
                continue
            for second in _children(first):
                if _real(second) not in listed:
                    _unknown(rows, second, os.path.basename(first), by_root)

    # what the dataset and the run dirs say was there: removed unless it is on disk now
    last_created, last_removed, by = {}, {}, {}
    for e in events:
        real = _real(e['path']) if os.path.isabs(e['path']) else e['path']
        if e['name'] == 'worktree_created':
            last_created[real] = e
        else:
            last_removed[real] = e
        by.setdefault(real, e)
    for real in set(last_created) | set(last_removed):
        created, removed = last_created.get(real), last_removed.get(real)
        if removed is not None and created is not None and removed['ts'] < created['ts']:
            removed = None
        if real in rows:
            if created is not None:
                rows[real]['created'] = created['ts']
            continue
        e = created or removed
        if os.path.isabs(e['path']) and _exists(e['path']):
            # on disk, yet no repository listed it: never call it removed
            _unknown(rows, e['path'], e.get('run_id'), by_root)
            rows[real].update(repo=rows[real]['repo'] or e.get('repo_path'), task=rows[real]['task'] or e.get('task'),
                              branch=e.get('branch'), created=created['ts'] if created else None,
                              why='on disk, but git did not list it as a worktree of %s' % (e.get('repo_path') or 'its repository'))
            continue
        root_key = R.wt_root(e['repo_path'], e['run_id']) if e.get('repo_path') and e.get('run_id') else None
        run = by_root.get(root_key) if root_key else None
        rows[real] = dict(path=e['path'], repo=e.get('repo_path'), git_dir=None, run=run['key'] if run else None,
                          run_id=e.get('run_id'), run_path=run['path'] if run else None, task=e.get('task'),
                          branch=e.get('branch'), kind=e.get('kind'), status='removed', merged=None, head=None,
                          commit=None, created=created['ts'] if created else None,
                          removed=removed['ts'] if removed else None, derived=False, locked=None, size=None, why=None)
    for run in runs:
        wroot = R.wt_root(run['repo'], run['run_id'])
        names = [t for t, s in run['tasks'].items() if s not in ('PENDING', 'BLOCKED', None)]
        if run['integration']:
            names.append('_integration')
        for name in names:
            _derived(rows, os.path.join(wroot, name), run['repo'], run['run_id'], name, run)
    for (wroot, repo, run_id), ids in sorted(dataset_tasks.items()):
        run = by_root.get(wroot)
        for name in sorted(ids) + ['_integration']:
            _derived(rows, os.path.join(wroot, name), repo, run_id, name, run)

    out = []
    for row in rows.values():
        row['commands'] = commands(row)
        row['note'] = _note(row)
        row.pop('why', None)
        out.append(row)
    out.sort(key=lambda r: (r['status'] == 'removed', STATUSES.index(r['status']), -(r['removed'] or r['created'] or 0), r['path']))
    run_groups = []
    for run in runs:
        if not any(r['run'] == run['key'] for r in out):
            continue
        group = dict(key=run['key'], run_id=run['run_id'], path=run['path'], repo=run['root'], active=run['live'], command=None)
        if not run['live'] and _isdir(run['path']):
            group['command'] = 'forge runs rm %s --worktrees --branches --dry-run' % quote(run['path'])
        run_groups.append(group)
    # A path that is no repository any more (a checkout deleted and its folder reused) has nothing to list; a real
    # failure (a timeout, git's safe.directory refusal) is named, since it hides that repository's worktrees.
    notes = ['git could not list the worktrees of %s (%s): its worktrees are shown from what is on disk and the dataset'
             % (repo, why) for repo, why in sorted(failures.items())
             if _real(repo) not in roots and 'not a git repository' not in why]
    if hist is None:
        notes.append('the dataset could not be read: only what git and the run dirs say is listed')
    elif not hist.get('enabled'):
        notes.append('the dataset is off (FORGE_DATASET=off): forge keeps no history of its worktrees, so only what git and the run dirs say is listed')
    return dict(rows=out[:_MAX_ROWS], truncated=len(out) > _MAX_ROWS, runs=run_groups,
                repos=sorted(info['root'] for info in roots.values()), notes=notes,
                dataset=None if hist is None else dict(enabled=bool(hist.get('enabled')), since=hist.get('since')),
                computed=time.time())


def _settled(row, run, integrated, roots):
    """finished, failed or needed for a clean, unlocked forge worktree of a run that is not active."""
    kind = row['kind']
    if kind == 'integration':
        if run is None:
            return 'finished'          # no run dir is left to need it
        # Integrated per the dataset, or its branch is in the user's and no task is left to retry into it: a branch
        # nothing was merged into yet is an ancestor of HEAD too, and the run still needs it.
        done = (_real(run['repo']), run['run_id']) in integrated or (
            bool(row['merged']) and all(status == 'MERGED' for status in run['tasks'].values()))
        return 'finished' if done else 'needed'
    if kind == 'task':
        if run is not None and row['task'] in run['tasks']:
            return 'finished' if run['tasks'][row['task']] == 'MERGED' else 'failed'
        if row['merged']:
            return 'finished'
        int_br = 'forge/%s-integration' % row['run_id']
        code, _ = _git(row['git_dir'], 'merge-base', '--is-ancestor', 'refs/heads/' + row['branch'], 'refs/heads/' + int_br)
        return 'finished' if code == 0 else 'failed'
    return 'finished'      # a scratch worktree forge left behind


def _children(folder):
    try:
        with os.scandir(folder) as entries:
            return sorted(os.path.join(folder, e.name) for e in entries if e.is_dir(follow_symlinks=False))
    except (OSError, ValueError):
        return []


def _unknown(rows, path, run_id, by_root):
    real = _real(path)
    if real in rows:
        return
    run = by_root.get(_real(os.path.dirname(path))) if run_id else None
    name = os.path.basename(path)
    kind = 'integration' if name == '_integration' else 'candidate' if name.startswith('_candidate-') else \
        'attribute' if name.startswith('_attribute') else 'task' if run_id else None
    rows[real] = dict(path=path, repo=run['root'] if run else None, git_dir=None, run=run['key'] if run else None,
                      run_id=run_id, run_path=run['path'] if run else None, task=name if kind == 'task' else None,
                      branch=None, kind=kind, status='unknown', merged=None, head=None, commit=None, created=None,
                      removed=None, derived=False, locked=None, size=None, why=None, leftover=_leftover(path))


def _derived(rows, path, repo, run_id, name, run):
    real = _real(path)
    if real in rows or _exists(path):
        return
    kind = 'integration' if name == '_integration' else 'task'
    rows[real] = dict(path=path, repo=repo, git_dir=None, run=run['key'] if run else None, run_id=run_id,
                      run_path=run['path'] if run else None, task=name if kind == 'task' else None,
                      branch='forge/%s-integration' % run_id if kind == 'integration' else 'forge/%s/%s' % (run_id, name),
                      kind=kind, status='removed', merged=None, head=None, commit=None, created=None, removed=None,
                      derived=True, locked=None, size=None, why=None)


# ---- sizes, in the background ------------------------------------------------------------------

def tree_size(path, budget=_SIZE_BUDGET_S):
    """(bytes on disk, complete) of a tree, symlinks not followed; stops after `budget` seconds."""
    total, stack, start = 0, [path], time.monotonic()
    while stack:
        if time.monotonic() - start > budget:
            return total, False
        here = stack.pop()
        try:
            with os.scandir(here) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += getattr(info, 'st_blocks', 0) * 512 or info.st_size
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
        except (OSError, ValueError):
            continue
    return total, True


class Sizes:
    """Sizes of worktree folders, walked one at a time in a daemon thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.known = {}           # path -> bytes
        self.queue = []
        self.thread = None

    def want(self, paths, again=False):
        with self.lock:
            for path in paths:
                if (again or path not in self.known) and path not in self.queue:
                    self.queue.append(path)
            if self.queue and (self.thread is None or not self.thread.is_alive()):
                self.thread = threading.Thread(target=self._work, name='forge-ui-sizes', daemon=True)
                self.thread.start()

    def _work(self):
        while True:
            with self.lock:
                if not self.queue:
                    return
                path = self.queue[0]
            size, _complete = tree_size(path)
            with self.lock:
                self.known[path] = size
                if self.queue and self.queue[0] == path:
                    self.queue.pop(0)

    def pending(self):
        with self.lock:
            return len(self.queue)

    def get(self, path):
        with self.lock:
            return self.known.get(path)


class Screen:
    """The cached answer of /api/worktrees for one server."""

    def __init__(self):
        self.lock = threading.Lock()
        self.result = None
        self.sizes = Sizes()

    def answer(self, catalog, history, refresh=False, now=None):
        """The rows with the sizes known so far. `history()` is called only when the cache is recomputed."""
        now = time.time() if now is None else now
        with self.lock:
            if refresh or self.result is None or now - self.result['computed'] > CACHE_S:
                self.result = compute(catalog, history())
                on_disk = [r['path'] for r in self.result['rows'] if r['status'] in _ON_DISK]
                self.sizes.want(on_disk, again=refresh)
            result = self.result
        rows = []
        totals = dict(count=0, on_disk=0, bytes=0, by_status={s: 0 for s in STATUSES})
        for row in result['rows']:
            row = dict(row)
            if row['status'] in _ON_DISK:
                row['size'] = self.sizes.get(row['path'])
                totals['on_disk'] += 1
                totals['bytes'] += row['size'] or 0
            totals['count'] += 1
            totals['by_status'][row['status']] += 1
            rows.append(row)
        out = dict(result, rows=rows, totals=totals, sizes_pending=self.sizes.pending())
        return out


def screen(server):
    """The server's Screen, made on first use."""
    with _LOCK:
        found = getattr(server, 'worktree_screen', None)
        if found is None:
            found = server.worktree_screen = Screen()
        return found
