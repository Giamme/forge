"""Which worktrees under `.forge-worktrees` are forge's, and what kind each is.

One rule for `forge runs rm` (what it may remove) and forge-ui's Worktrees screen (what it offers to
remove), so the two cannot disagree (#125).

forge-parallel.sh puts a run's worktrees in `<checkout>/../.forge-worktrees/<run id>/`, derived with a
logical `cd`, never read from the run's `wt_root` file. A worktree there is the run's when:

  task         it has the branch forge/<run id>/<task> checked out, for a task of the run
  integration  it has the branch forge/<run id>-integration checked out
  candidate    it is detached, directly in the run's folder, and named `_candidate-<n>` (integrate's
               scratch worktree, left behind when integrate stopped)
  attribute    it is detached, directly in the run's folder, and named `_attribute` or
               `_attribute-<n>` (the culprit search's scratch worktree), and no task has that name

Anything else there, and any worktree of the run's branches outside that folder, is not forge's.
"""
from __future__ import annotations

import os
import re

RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_SCRATCH = (('candidate', re.compile(r'_candidate-[0-9]+')), ('attribute', re.compile(r'_attribute(?:-[0-9]+)?')))
KINDS = ('task', 'integration', 'candidate', 'attribute')


def contains(parent, child):
    """True when `child` is `parent` or inside it (both real paths)."""
    parent = parent.rstrip(os.sep) or os.sep
    return child == parent or child.startswith(parent + os.sep)


def wt_root(repo, run_id):
    """The run's worktree folder, as forge-parallel.sh derives it: `$(cd "$REPO/.." && pwd)/.forge-worktrees/<run id>`."""
    return os.path.realpath(os.path.join(os.path.normpath(os.path.join(repo, '..')), '.forge-worktrees', run_id))


def owner(path, branch, run_id, tasks, root):
    """The kind of forge worktree at `path` (one of KINDS), or None when it is not the run's.

    `branch` is the full ref git lists (refs/heads/...), or None for a detached worktree; `tasks` the run's
    task ids; `root` the run's worktree folder (`wt_root`).
    """
    real = os.path.realpath(path)
    if real == root or not contains(root, real):
        return None
    if branch == 'refs/heads/forge/%s-integration' % run_id:
        return 'integration'
    prefix = 'refs/heads/forge/%s/' % run_id
    if isinstance(branch, str) and branch.startswith(prefix) and branch[len(prefix):] in set(tasks):
        return 'task'
    name = os.path.basename(real)
    if branch is None and os.path.dirname(real) == root and name not in set(tasks):
        for kind, pattern in _SCRATCH:
            if pattern.fullmatch(name):
                return kind
    return None


def parse_porcelain(out, z=True):
    """`git worktree list --porcelain [-z]` output (text) as a list of dicts:
    {path, head, branch (full ref or None), detached, bare, locked (reason or None), prunable (reason or None)}.

    `locked` and `prunable` are '' when git gave no reason. With `z` a path may hold a newline; without it
    such a path is split and its record is malformed, so that output is only for a git too old for -z.
    """
    found, current = [], None
    fields = out.split('\0') if z else out.split('\n')
    for field in fields:
        if not field:
            current = None
            continue
        key, _, value = field.partition(' ')
        if key == 'worktree':
            current = dict(path=value, head=None, branch=None, detached=False, bare=False, locked=None, prunable=None)
            found.append(current)
        elif current is None:
            continue
        elif key == 'HEAD':
            current['head'] = value
        elif key == 'branch':
            current['branch'] = value
        elif key in ('detached', 'bare'):
            current[key] = True
        elif key in ('locked', 'prunable'):
            current[key] = value
    return found
