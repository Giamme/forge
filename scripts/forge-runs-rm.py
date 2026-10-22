#!/usr/bin/env python3
"""forge runs rm <run-dir> [--dry-run] [--yes] [--no-archive] [--worktrees] [--branches] [--json]

Delete a run directory without losing what it taught the dataset. In order, with the run's own
lock held from the first step to the last (`schedule.lock` of a plan, `pipeline.lock` of a solo
run, the locks the runners take), so a run started meanwhile fails to start instead of racing
the delete:

  preserve    a strict dataset import of the run: every event must be stored
  archive     a self-describing .tar.gz under <data home>/archive/ (--no-archive skips it)
  intent      the dataset event run_delete_started
  worktrees   --worktrees: remove the run's clean worktrees, then `git worktree prune`
  branches    --branches: `git branch -D` the run's branches not checked out anywhere
  remove      shutil.rmtree of the run dir
  record      the dataset event run_deleted, from the identity read before the removal
  unregister  drop the path from every run registry file that lists it

A failure in preserve, archive or intent stops everything and deletes nothing. A failure later is
reported per step; the run dir is kept when worktrees or branches could not be dealt with, and
after a failed removal the files that identify the run are the last to go, so the same command
finishes the job. `forge runs rm` on a path that no longer exists only unregisters it.

What the run owns on the repo's side is decided here, not read from the run dir: a worktree is the
run's only under <repo>/../.forge-worktrees/<run id>/ (as forge-parallel.sh derives it, not from
the `wt_root` file) on a branch forge/<run id>/<task> for a task in tasks.tsv or
forge/<run id>-integration. Any other worktree in that place is `foreign`, and never touched. A
worktree with uncommitted or untracked changes is `dirty`: never removed, and --worktrees then
keeps the run dir too.

--json prints one object on stdout, on failure too:
  {run_dir, run_id, dry_run, ok, steps: [{step, ok, detail}], archive, bytes_freed,
   branches: [{name, sha, status: merged|UNMERGED, checked_out, deleted, note}],
   worktrees: [{path, branch, kind: run|foreign, dirty, removed, note}],
   size_bytes, archive_bytes, archive_estimate_bytes}
`ok` means the requested deletion is complete (a dry run says what a real one would do).
Exit 0 ok, 2 refused input, 3 busy or active, 4 failed during deletion (or the run dir kept).
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))

import forge_registry  # noqa: E402
from forge_dataset import DatasetError, data_home, dataset_dir, enabled, harvest, importer, record_strict  # noqa: E402

OK, REFUSED, BUSY, FAILED = 0, 2, 3, 4
# fullmatch: with `$` alone, a trailing newline would pass
RUN_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
TASK_ID = RUN_ID
GIT_TIMEOUT_S = 120
READ_MAX = 8 << 20
# Test hook: a file path. Once the lock is held the command creates that file and waits (at
# most PAUSE_MAX_S) for it to be removed, so a test can try the lock from outside.
PAUSE_ENV = 'FORGE_RUNS_RM_PAUSE_AFTER_LOCK'
PAUSE_MAX_S = 30
# What identifies a run dir (and, dataset.uid, names it in the dataset). Removed after everything
# else, so a removal that fails half way leaves a dir this command still accepts, and a rerun
# imports it as the run it was.
IDENTITY = ('tasks.tsv', 'prompt.md', 'repo', 'run_id', 'dataset.uid')
GIT_ENV_DROP = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_NAMESPACE', 'GIT_PREFIX')


class Refused(Exception):
    """Input this command will not act on (exit 2)."""


class Busy(Exception):
    """The run is, or may be, in use (exit 3)."""


class Failed(Exception):
    """Something that had to work did not (exit 4)."""


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


# ------------------------------------------------------------------------------ report

class Report:
    """What happened, kept as data so --json can always print it, and printed live otherwise."""

    def __init__(self, args):
        self.run_dir = args.run_dir
        self.run_id = None
        self.dry_run = args.dry_run
        self.json = args.json
        self.ok = True
        self.steps = []
        self.archive = None
        self.bytes_freed = 0
        self.branches = []
        self.worktrees = []
        self.extra = {}

    def step(self, name, ok, detail):
        self.steps.append({'step': name, 'ok': bool(ok), 'detail': detail})
        if not ok:
            self.ok = False
        if not self.json:
            print('  %-4s %-10s %s' % ('ok' if ok else 'FAIL', name, detail), file=sys.stdout if ok else sys.stderr)

    def warn(self, text):
        print('forge runs rm: WARNING: ' + text, file=sys.stderr)

    def data(self):
        return dict(run_dir=self.run_dir, run_id=self.run_id, dry_run=self.dry_run, ok=self.ok, steps=self.steps,
                    archive=self.archive, bytes_freed=self.bytes_freed, branches=self.branches,
                    worktrees=self.worktrees, **self.extra)

    def finish(self, code):
        if self.json:
            json.dump(self.data(), sys.stdout, indent=2)
            sys.stdout.write('\n')
        else:
            for wt in self.worktrees:
                state = wt['kind'] + (', dirty' if wt['dirty'] else '') + (', removed' if wt['removed'] else '')
                print('  worktree  %s  %s  (%s)%s' % (wt['path'], wt['branch'] or 'detached', state,
                                                      ' ' + wt['note'] if wt['note'] else ''))
            for branch in self.branches:
                print('  branch    %s  %s%s%s' % (branch['name'], branch['status'], ', deleted' if branch['deleted'] else '',
                                                  ' ' + branch['note'] if branch['note'] else ''))
            if not self.ok:
                print('forge runs rm: not completed (exit %d), see the steps above' % code, file=sys.stderr)
            elif self.dry_run:
                print('forge runs rm: dry run, nothing was changed')
            else:
                print('forge runs rm: done')
        return code


def interactive():
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (ValueError, OSError):
        return False


def size_text(count):
    value = float(count)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return ('%d %s' % (value, unit)) if unit == 'B' else ('%.1f %s' % (value, unit))
        value /= 1024


# ------------------------------------------------------------------------------ the run dir

def contains(parent, child):
    """Whether `child` is `parent` or below it (both real, absolute paths)."""
    return child == parent or child.startswith(parent.rstrip('/') + '/')


class Target:
    def __init__(self, path, info):
        self.path = path
        self.info = info


def locate(arg):
    """The real path the argument names. The argument itself must not be a symlink."""
    if not arg:
        raise Refused('no run dir given')
    raw = arg if os.path.isabs(arg) else os.path.join(os.getcwd(), arg)
    raw = raw.rstrip('/') or '/'
    try:
        info = os.lstat(raw)
    except (FileNotFoundError, NotADirectoryError):
        return Target(os.path.join(os.path.realpath(os.path.dirname(raw)), os.path.basename(raw)), None)
    except OSError as error:
        raise Refused('cannot look at %s: %s' % (arg, error.strerror or error))
    if stat.S_ISLNK(info.st_mode):
        raise Refused('%s is a symbolic link: name the run dir itself' % arg)
    if not stat.S_ISDIR(info.st_mode):
        raise Refused('%s is not a directory' % arg)
    return Target(os.path.realpath(raw), info)


def read_text(path, cap=READ_MAX):
    """A run file's text: None if absent, Refused if it is not a plain readable file. Never follows a link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise Refused('cannot read %s: %s' % (path, error.strerror or error))
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused('%s is not a regular file' % path)
        return handle.read(cap).decode('utf-8', 'replace')


def first_line(path):
    text = read_text(path, 64 * 1024)
    line = (text or '').split('\n', 1)[0].strip()
    return line or None


def homes():
    found = {os.path.realpath(os.path.expanduser('~'))}
    try:
        import pwd
        found.add(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError, OSError):
        pass
    return found


class Run:
    """A run dir that passed the identity and safety rules."""

    def __init__(self, target):
        self.path = target.path
        self.dev, self.ino = target.info.st_dev, target.info.st_ino
        self.kind = self.run_id = self.repo = self.repo_real = self.lock_path = self.mode = None
        self.tasks = []
        self.ignored_tasks = []
        # filled in under the lock
        self.size = self.estimate = self.left_out = 0
        self.uid = None
        self.git_error = None
        self.worktrees = []
        self.branches = []
        self.wt_root = None


def identify(target):
    path = target.path
    if path == '/' or any(contains(path, home) for home in homes()):
        raise Refused('refusing to delete %s: it is, or contains, the home directory' % path)
    if os.path.lexists(os.path.join(path, '.git')):
        raise Refused('refusing to delete %s: it has a .git at its top (a repository, not a run)' % path)
    run = Run(target)
    if os.path.isfile(os.path.join(path, 'tasks.tsv')):
        run.kind, run.mode, run.lock_path = 'plan', 'parallel', os.path.join(path, 'schedule.lock')
    elif os.path.isfile(os.path.join(path, 'prompt.md')) and os.path.isfile(os.path.join(path, 'repo')):
        run.kind, run.mode, run.lock_path = 'solo', 'solo', os.path.join(path, 'pipeline.lock')
    else:
        raise Refused('%s is not a forge run dir (no tasks.tsv, and no prompt.md with a repo file)' % path)
    run.run_id = first_line(os.path.join(path, 'run_id')) or os.path.basename(path)
    if not RUN_ID.fullmatch(run.run_id):
        raise Refused('run id %r is not safe as a file name and a ref component (%s)' % (run.run_id, RUN_ID.pattern))
    run.repo = first_line(os.path.join(path, 'repo'))
    if not run.repo or not os.path.isabs(run.repo) or '\x00' in run.repo:
        raise Refused('%s has no usable repo file: cannot tell which repo the run belongs to' % path)
    run.repo_real = os.path.realpath(run.repo)
    if contains(path, run.repo_real) or contains(path, os.path.abspath(run.repo)):
        raise Refused('refusing to delete %s: it is, or contains, the run\'s repo %s' % (path, run.repo))
    if run.kind == 'plan':
        seen = set()
        for line in (read_text(os.path.join(path, 'tasks.tsv')) or '').splitlines():
            if not line.strip() or line.startswith('#'):
                continue
            task = line.split('\t', 1)[0]
            if not TASK_ID.fullmatch(task):
                run.ignored_tasks.append(task)
            elif task not in seen:
                seen.add(task)
                run.tasks.append(task)
    # forge-parallel.sh: wt_root="$(cd "$REPO/.." && pwd)/.forge-worktrees/$run_id", a logical cd
    run.wt_root = os.path.realpath(os.path.join(os.path.normpath(os.path.join(run.repo, '..')), '.forge-worktrees', run.run_id))
    if contains(path, run.wt_root) or contains(run.wt_root, path):
        raise Refused('refusing to delete %s: it overlaps the run\'s worktree root %s' % (path, run.wt_root))
    return run


def refuse_destinations(run, args):
    """The archive and the dataset must be written somewhere that outlives the run dir."""
    spots = [] if args.no_archive else [('archive', data_home() / 'archive')]
    if enabled():
        spots.append(('dataset', dataset_dir()))
    for name, spot in spots:
        if contains(run.path, os.path.realpath(spot)):
            raise Refused('the %s destination %s is inside the run dir being deleted' % (name, spot))


def check_unchanged(run, why):
    try:
        now = os.lstat(run.path)
    except OSError:
        raise Busy('the run dir disappeared %s' % why)
    if (now.st_dev, now.st_ino) != (run.dev, run.ino) or not stat.S_ISDIR(now.st_mode):
        raise Busy('the run dir was replaced %s' % why)


def scheduler_pid(run):
    """The pid of a live scheduler, from scheduler.live, else None."""
    if run.kind != 'plan':
        return None
    try:
        pid = json.loads(read_text(os.path.join(run.path, 'scheduler.live'), 64 * 1024) or '')['pid']
    except (ValueError, KeyError, TypeError, Refused):
        return None
    if type(pid) is not int or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    except (OSError, OverflowError):
        return None
    return pid


def take_lock(run):
    """The run's operation lock, non-blocking, as the runners take it. Returns the open fd."""
    try:
        fd = os.open(run.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)
    except OSError as error:
        raise Failed('cannot open %s: %s' % (run.lock_path, error.strerror or error))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused('%s is not a regular file' % run.lock_path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                raise Busy('%s is held by a running forge command' % os.path.basename(run.lock_path)) from error
            raise Failed('cannot lock %s: %s' % (run.lock_path, error.strerror or error)) from error
        check_unchanged(run, 'while it was being locked')
        held, linked = os.fstat(fd), os.lstat(run.lock_path)
        if (held.st_dev, held.st_ino) != (linked.st_dev, linked.st_ino):
            raise Busy('%s was replaced while it was being locked' % os.path.basename(run.lock_path))
    except BaseException:
        os.close(fd)
        raise
    return fd


def pause_for_test():
    marker = os.environ.get(PAUSE_ENV)
    if not marker:
        return
    Path(marker).write_text(str(os.getpid()))
    deadline = time.monotonic() + PAUSE_MAX_S
    while os.path.exists(marker) and time.monotonic() < deadline:
        time.sleep(0.02)


def scan(run):
    """Bytes in the run dir, and what archive_run would put in an archive of it."""
    total = estimate = left_out = 0
    stack = [(run.path, True)]
    while stack:
        directory, included = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append((entry.path, included and entry.name not in importer.SKIP_DIRS
                              and not entry.name.startswith('review-')))
                continue
            total += info.st_size
            if included:
                if stat.S_ISREG(info.st_mode) and info.st_size <= importer.ARCHIVE_MAX_FILE:
                    estimate += info.st_size
                else:
                    left_out += 1
    run.size, run.estimate, run.left_out = total, estimate, left_out


# ------------------------------------------------------------------------------ git

def git(repo, *args):
    env = {k: v for k, v in os.environ.items() if k not in GIT_ENV_DROP}
    env.update(GIT_TERMINAL_PROMPT='0', LC_ALL='C')
    try:
        done = subprocess.run(['git', '--no-optional-locks', '-C', repo, *args], capture_output=True, text=True,
                              errors='replace', stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        return 255, '', str(error)
    return done.returncode, done.stdout, done.stderr.strip()


def list_worktrees(repo):
    """`git worktree list --porcelain` as dicts; raises Failed if git cannot say."""
    code, out, err = git(repo, 'worktree', 'list', '--porcelain')
    if code != 0:
        raise Failed('git worktree list failed in %s: %s' % (repo, err or 'exit %d' % code))
    found, current = [], None
    for line in out.splitlines():
        if not line.strip():
            current = None
            continue
        key, _, value = line.partition(' ')
        if key == 'worktree':
            current = {'path': value, 'branch': None, 'head': None}
            found.append(current)
        elif current is not None:
            if key == 'branch':
                current['branch'] = value
            elif key == 'HEAD':
                current['head'] = value
            else:
                current[key] = value or True
    return found


def inspect_git(run):
    """Classify the repo's worktrees and the run's branches. Sets run.git_error if git cannot tell."""
    try:
        listed = list_worktrees(run.repo)
    except Failed as error:
        run.git_error = str(error)
        return
    branch_refs = {'refs/heads/forge/%s/%s' % (run.run_id, task) for task in run.tasks}
    branch_refs.add('refs/heads/forge/%s-integration' % run.run_id)
    for wt in listed:
        real = os.path.realpath(wt['path'])
        in_root = real != run.wt_root and contains(run.wt_root, real)
        ours = wt['branch'] in branch_refs
        if not in_root and not ours:
            continue   # the main worktree, or another run's: not this command's business
        entry = dict(path=wt['path'], branch=(wt['branch'] or '').replace('refs/heads/', '', 1) or None,
                     kind='run' if in_root and ours else 'foreign', dirty=None, removed=False, note='')
        if entry['kind'] == 'run':
            if not os.path.isdir(wt['path']):
                entry.update(dirty=False, note='missing on disk; `git worktree prune` forgets it')
            else:
                code, out, err = git(wt['path'], 'status', '--porcelain', '--untracked-files=normal')
                entry['dirty'] = code != 0 or bool(out.strip())
                if code != 0:
                    entry['note'] = 'git status failed, treated as dirty: ' + (err or 'exit %d' % code)
            if wt.get('locked'):
                entry['note'] = (entry['note'] + ' ' if entry['note'] else '') + 'locked'
        else:
            entry['note'] = 'not this run\'s: ' + ('its branch is not one of the run\'s' if in_root else 'outside the run\'s worktree root')
        run.worktrees.append(entry)
    code, out, err = git(run.repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/forge/')
    if code != 0:
        run.git_error = 'git for-each-ref failed in %s: %s' % (run.repo, err or 'exit %d' % code)
        return
    checked_out = {wt['branch']: wt['path'] for wt in listed if wt['branch']}
    for line in sorted(out.splitlines()):
        ref, _, sha = line.rpartition(' ')
        if ref not in branch_refs:
            continue
        merged = git(run.repo, 'merge-base', '--is-ancestor', sha, 'HEAD')[0] == 0
        run.branches.append(dict(name=ref.replace('refs/heads/', '', 1), sha=sha, merged=merged,
                                 status='merged' if merged else 'UNMERGED', checked_out=checked_out.get(ref),
                                 deleted=False, note=''))


# ------------------------------------------------------------------------------ the steps

def archive_target(run):
    base = data_home() / 'archive' / ('%s-%s' % (run.run_id, time.strftime('%Y%m%d-%H%M%S')))
    candidate, count = Path(str(base) + '.tar.gz'), 0
    while candidate.exists():
        count += 1
        candidate = Path('%s-%d.tar.gz' % (base, count))
    return str(candidate)


def plan_steps(run, args, dest, keep):
    """[(step, active, what it does, or why it is skipped)] in execution order."""
    on = enabled()
    dirty = [wt for wt in run.worktrees if wt['kind'] == 'run' and wt['dirty']]
    clean = [wt for wt in run.worktrees if wt['kind'] == 'run' and not wt['dirty']]
    standing = sum(1 for wt in run.worktrees if wt['kind'] == 'run')
    names = [b['name'] for b in run.branches]
    archive = 'skipped (--no-archive)'
    if not args.no_archive:
        archive = 'write %s (about %s before compression%s)' % (
            dest, size_text(run.estimate), '; %d file(s) left out' % run.left_out if run.left_out else '')
    worktrees = 'skipped (no --worktrees)' + ('; %d run worktree(s) stay in place' % standing if standing else '')
    if args.worktrees:
        worktrees = 'remove %d clean worktree(s)%s, then git worktree prune' % (
            len(clean), ', keep %d dirty' % len(dirty) if dirty else '')
    branches = 'skipped (no --branches)' + ('; %d run branch(es) stay' % len(names) if names else '')
    if args.branches:
        branches = 'delete %s, except any checked out in a remaining worktree' % (', '.join(names) or 'no branch (the run has none)')
    return [
        ('preserve', on, 'import the run into the dataset, strictly: every event must be stored' if on else
         'skipped: FORGE_DATASET=off, so this run\'s traces will NOT be kept in the dataset'),
        ('archive', not args.no_archive, archive),
        ('intent', on, 'record the dataset event run_delete_started' if on else 'skipped: FORGE_DATASET=off'),
        ('worktrees', args.worktrees, worktrees),
        ('branches', args.branches, branches),
        ('remove', not keep, ('remove %s (%s)' % (run.path, size_text(run.size))) if not keep else 'kept: ' + keep),
        ('record', on and not keep, 'record the dataset event run_deleted' if on and not keep else
         'skipped: ' + ('the run dir is kept' if keep else 'FORGE_DATASET=off')),
        ('unregister', not keep, 'remove the path from every registry file that lists it' if not keep else
         'skipped: the run dir is kept'),
    ]


def event(run, name, **values):
    stamp = json.dumps([run.uid, name, run.path, time.time_ns()])
    return dict(rid='event:%s:%s' % (name, hashlib.sha256(stamp.encode()).hexdigest()[:24]), kind='event',
                ts=time.time(), run=run.uid, run_id=run.run_id, mode=run.mode, task=None,
                parent=os.environ.get('FORGE_DATASET_PARENT') or None,
                inv=os.environ.get('FORGE_DATASET_INVOCATION') or None,
                data=dict(values, name=name, blobs={}))


def remove_entry(entry, errors):
    try:
        if entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)   # never follows a symlink
        else:
            os.unlink(entry.path)
    except OSError as error:
        errors.append('%s: %s' % (entry.path, error.strerror or error))


def remove_run_dir(run):
    """rmtree the run dir, keeping the lock file and the files that identify the run for last.

    The lock file stays until the dir is about to go, so a runner starting meanwhile finds
    the lock we hold instead of creating a fresh one. If anything fails the identity files
    are still there and `forge runs rm` accepts the dir again.
    """
    with os.scandir(run.path) as listing:
        entries = list(listing)
    lock = os.path.basename(run.lock_path)
    errors = []
    for entry in entries:
        if entry.name != lock and entry.name not in IDENTITY:
            remove_entry(entry, errors)
    if errors:
        raise Failed('%d error(s), the run dir stays: %s' % (len(errors), '; '.join(errors[:3])))
    for entry in sorted((e for e in entries if e.name == lock or e.name in IDENTITY), key=lambda e: e.name == lock):
        remove_entry(entry, errors)
    if errors:
        raise Failed('; '.join(errors[:3]))
    try:
        os.rmdir(run.path)
    except OSError as error:
        raise Failed('cannot remove %s: %s' % (run.path, error.strerror or error))


def remove_worktrees(run):
    """Step 4: `git worktree remove` (never --force) on the run's clean worktrees, then prune. Returns an error text, or None."""
    problems = []
    for entry in run.worktrees:
        if entry['kind'] != 'run' or entry['dirty'] or entry['note'].startswith('missing'):
            continue
        code, _, err = git(run.repo, 'worktree', 'remove', entry['path'])
        if code == 0:
            entry['removed'] = True
        else:
            entry['note'] = (err or 'git worktree remove exit %d' % code).splitlines()[0]
            problems.append('%s: %s' % (entry['path'], entry['note']))
    code, _, err = git(run.repo, 'worktree', 'prune')
    if code != 0:
        problems.append('git worktree prune: ' + (err or 'exit %d' % code))
    if os.path.isdir(run.wt_root) and not os.path.islink(run.wt_root):
        try:
            os.rmdir(run.wt_root)   # only succeeds when it is empty: a foreign worktree keeps it
        except OSError:
            pass
    return '; '.join(problems) or None


def delete_branches(run):
    """Step 5: `git branch -D` the run's branches that no remaining worktree has checked out. Returns an error text, or None."""
    remaining = {wt['branch']: wt['path'] for wt in list_worktrees(run.repo) if wt['branch']}
    problems = []
    for branch in run.branches:
        where = remaining.get('refs/heads/' + branch['name'])
        if where:
            branch['note'] = 'kept: checked out in ' + where
            continue
        code, _, err = git(run.repo, 'branch', '-D', branch['name'])
        if code == 0:
            branch['deleted'] = True
            branch['note'] = 'was %s; restore with: git branch %s %s' % (branch['sha'][:12], branch['name'], branch['sha'])
        else:
            branch['note'] = err or 'git branch -D exit %d' % code
            problems.append('%s: %s' % (branch['name'], branch['note']))
    return '; '.join(problems) or None


def execute(run, args, report, dest, keep):
    """Steps 1 to 8 with the lock held. Returns the exit code."""
    plan = {name: (active, text) for name, active, text in plan_steps(run, args, dest, keep)}

    def skipped(name):
        if plan[name][0]:
            return False
        report.step(name, True, plan[name][1])
        return True

    if not skipped('preserve'):
        try:
            result = importer.import_run(run.path, strict=True)
        except (DatasetError, OSError) as error:
            report.step('preserve', False, 'the dataset could not take the run, nothing was deleted: %s' % error)
            return FAILED
        unfinished = result.get('skipped_unfinished')
        report.step('preserve', True, 'imported %d event(s), %d new%s' % (
            result['events'], result['new'], ', %d unfinished execution(s) left to live capture' % unfinished if unfinished else ''))
    if not skipped('archive'):
        try:
            made = importer.archive_run(run.path, dest)
        except (DatasetError, OSError) as error:
            report.step('archive', False, 'no archive, nothing was deleted: %s' % error)
            return FAILED
        report.archive = made['path']
        report.extra['archive_bytes'] = made['bytes']
        report.step('archive', True, 'wrote %s (%s%s)' % (made['path'], size_text(made['bytes']),
                                                           '; %d file(s) left out, listed inside' % len(made['excluded'])
                                                           if made['excluded'] else ''))
    if not skipped('intent'):
        try:
            record_strict([event(run, 'run_delete_started', path=run.path, run_id=run.run_id, archive=report.archive)],
                          repo=run.repo, where='event:run_delete_started')
        except DatasetError as error:
            report.step('intent', False, 'could not record the intent, nothing was deleted: %s' % error)
            return FAILED
        report.step('intent', True, 'recorded run_delete_started')
    blocked = False   # a requested worktree or branch cleanup did not complete: keep the run dir
    if not skipped('worktrees'):
        try:
            problem = remove_worktrees(run)
        except Failed as error:
            problem = str(error)
        blocked = blocked or bool(problem)
        report.step('worktrees', not problem, problem or '%d worktree(s) removed' % sum(wt['removed'] for wt in run.worktrees))
    if not skipped('branches'):
        try:
            problem = delete_branches(run)
        except Failed as error:
            problem = str(error)
        blocked = blocked or bool(problem)
        report.step('branches', not problem, problem or '%d branch(es) deleted' % sum(b['deleted'] for b in run.branches))
    if keep or blocked:
        report.step('remove', False, 'kept: ' + (keep or 'a requested worktree or branch step failed, see above'))
        report.step('record', True, 'skipped: the run dir is kept')
        report.step('unregister', True, 'skipped: the run dir is kept')
        return FAILED
    try:
        check_unchanged(run, 'during the operation, so it is not removed')
        remove_run_dir(run)
    except (Busy, Failed) as error:
        report.step('remove', False, 'the run dir was not removed: %s' % error)
        report.step('record', True, 'skipped: the run dir is kept')
        report.step('unregister', True, 'skipped: the run dir is kept')
        return FAILED
    report.bytes_freed = run.size
    report.step('remove', True, 'removed %s (%s)' % (run.path, size_text(run.size)))
    code = OK
    if not skipped('record'):
        try:
            record_strict([event(run, 'run_deleted', path=run.path, run_id=run.run_id, archive=report.archive, bytes_freed=run.size,
                                 archive_bytes=report.extra.get('archive_bytes'))],
                          repo=run.repo, where='event:run_deleted')
            report.step('record', True, 'recorded run_deleted')
        except DatasetError as error:
            report.step('record', False, 'removed, but run_deleted could not be recorded: %s' % error)
            code = FAILED
    try:
        changed = forge_registry.unregister_run_dir(run.path)
        report.step('unregister', True, 'removed from %d registry file(s)' % len(changed) if changed else 'it was not registered')
    except OSError as error:
        report.step('unregister', False, 'removed, registry needs repair: rerun `forge runs rm %s` (%s)' % (shlex.quote(run.path), error))
        code = FAILED
    return code


def confirm(run):
    sys.stderr.write('delete %s (%s, run id %s)?\ntype the run id to delete: ' % (run.path, size_text(run.size), run.run_id))
    sys.stderr.flush()
    if sys.stdin.readline().strip() != run.run_id:
        raise Refused('aborted: that is not the run id, nothing was changed')


# ------------------------------------------------------------------------------ command

def forget(target, args, report):
    """The path is gone: all that is left to do is the registry."""
    name = os.path.basename(target.path)
    report.run_id = name if RUN_ID.fullmatch(name) else None
    report.step('check', True, 'no such directory; only the registry entries are removed')
    if args.dry_run:
        report.step('unregister', True, 'remove the path from every registry file that lists it')
        return OK
    try:
        changed = forge_registry.unregister_run_dir(target.path)
    except OSError as error:
        report.step('unregister', False, 'registry needs repair: rerun `forge runs rm %s` (%s)' % (shlex.quote(target.path), error))
        return FAILED
    report.step('unregister', True, 'removed from %d registry file(s)' % len(changed) if changed else 'it was not registered')
    return OK


def run_command(args, report):
    target = locate(args.run_dir)
    report.run_dir = target.path
    if target.info is None:
        return forget(target, args, report)
    run = identify(target)
    report.run_id = run.run_id
    refuse_destinations(run, args)
    if not args.dry_run and not args.yes and not interactive():
        raise Refused('nothing asked for the delete and this is not a terminal: pass --yes to delete %s' % run.path)
    if not enabled():
        report.warn('FORGE_DATASET=off: the traces of %s will NOT be kept in the dataset' % run.path)
    fd = None
    try:
        if args.dry_run:
            check_unchanged(run, 'while it was being inspected')   # never probes the lock: a probe could fail a starting run
        else:
            fd = take_lock(run)
            pause_for_test()
        pid = scheduler_pid(run)
        if pid:
            raise Busy('scheduler.live names a live process (pid %d): the run is active; if that is not a forge '
                       'scheduler, delete %s/scheduler.live' % (pid, run.path))
        scan(run)
        inspect_git(run)
        report.worktrees, report.branches = run.worktrees, run.branches
        if (args.worktrees or args.branches) and run.git_error:
            raise Refused('cannot clean up worktrees or branches: ' + run.git_error)
        try:
            run.uid = harvest.run_uid(Path(run.path), create=False)
        except Exception:   # noqa: BLE001  -- an identity we cannot read only costs the event its run column
            run.uid = None
        dest = None if args.no_archive else archive_target(run)
        report.archive = dest if args.dry_run else None
        report.extra.update(size_bytes=run.size, archive_bytes=None,
                            archive_estimate_bytes=None if args.no_archive else run.estimate)
        dirty = [wt['path'] for wt in run.worktrees if wt['kind'] == 'run' and wt['dirty']]
        keep = ('dirty worktree(s) hold uncommitted work: %s; commit or discard it there, then rerun' % ', '.join(dirty)
                if args.worktrees and dirty else None)
        report.step('check', True, '%s dir, run id %s, %d task(s), %s%s%s' % (
            run.kind, run.run_id, len(run.tasks), size_text(run.size),
            '; ignoring task id(s) unsafe as ref names: %s' % ', '.join(run.ignored_tasks) if run.ignored_tasks else '',
            '; worktrees and branches unknown: ' + run.git_error if run.git_error else ''))
        if args.dry_run:
            report.step('lock', True, 'would hold %s from here to the end (not probed in a dry run)' % os.path.basename(run.lock_path))
            for name, active, text in plan_steps(run, args, dest, keep):
                report.step(name, name != 'remove' or active, ('would ' if active else '') + text)
            return OK if report.ok else FAILED
        report.step('lock', True, 'holding %s' % os.path.basename(run.lock_path))
        if not args.yes:
            confirm(run)
        return execute(run, args, report, dest, keep)
    finally:
        if fd is not None:
            os.close(fd)


def parser():
    p = Parser(prog='forge runs rm', description='Delete a run dir, keeping its traces in the dataset. '
               'See the module docstring of scripts/forge-runs-rm.py for the steps and the safety rules.')
    p.add_argument('run_dir', metavar='RUN_DIR', help='the plan dir or solo run dir to delete')
    p.add_argument('--dry-run', action='store_true', help='change nothing; report what would happen')
    p.add_argument('--yes', action='store_true', help='do not ask for the run id (needed when there is no terminal)')
    p.add_argument('--no-archive', action='store_true', help='keep the traces in the dataset only, write no .tar.gz')
    p.add_argument('--worktrees', action='store_true', help="also remove the run's clean worktrees")
    p.add_argument('--branches', action='store_true', help="also delete the run's branches that no worktree has checked out")
    p.add_argument('--json', action='store_true', help='print one JSON object, on failure too')
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser().parse_args(argv)
    except UsageError as error:
        args = argparse.Namespace(run_dir=argv[0] if argv and not argv[0].startswith('-') else None, dry_run='--dry-run' in argv,
                                  json='--json' in argv)
        report = Report(args)
        report.step('check', False, 'usage: %s' % error)
        if not args.json:
            parser().print_usage(sys.stderr)
        return report.finish(REFUSED)
    report = Report(args)
    try:
        code = run_command(args, report)
    except Refused as error:
        report.step('check', False, str(error))
        code = REFUSED
    except Busy as error:
        report.step('lock', False, str(error))
        code = BUSY
    except Failed as error:
        report.step('check', False, str(error))
        code = FAILED
    except KeyboardInterrupt:
        report.step('interrupted', False, 'interrupted; see the steps above for what was done')
        code = 130
    except Exception as error:   # noqa: BLE001  -- --json must still print one object
        report.step('internal', False, '%s: %s' % (type(error).__name__, error))
        code = FAILED
    return report.finish(code)


if __name__ == '__main__':
    sys.exit(main())
