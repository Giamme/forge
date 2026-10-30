"""Resolve which repository each forge run belongs to, for new and old runs.

forge-ui used to group runs by the literal text of each run's ``repo`` file, which is the checkout
the run started in. A run started from a git worktree therefore looked like one more repository,
and once the worktree was removed its path could never be resolved again. This module turns a run
into an identity instead. It is read-only: it never writes a run dir or a repository.

Public interface::

    Run = (path, repo, kind)
        path  the run dir
        repo  the text of its ``repo`` file, or None
        kind  'plan' or 'solo'

    resolve(runs) -> {path: Identity}
        Resolves a batch of runs, for example every run the catalog lists. Each Identity is a dict
        {root, worktree, branch, source}:
          root      the repository's main working tree and the grouping key; never None when the
                    run has a ``repo`` text (None only when it has none)
          worktree  the physical checkout when it differs from root, else None
          branch    the branch checked out when the run was created, else None (unknown)
          source    how it was found: 'identity', 'live', 'base-commit' or 'path'

The order for each run:

1. ``repo.identity`` in the run dir (one JSON object written by forge: root, worktree, branch).
   Used when ``root`` is an absolute path; ``worktree`` and ``branch`` are taken when strings.
   ``root`` is not checked for existence: the identity was right when it was written.
2. The ``repo`` path still exists and is a git checkout: root is the first entry of
   ``git worktree list --porcelain``. worktree is the linked checkout holding ``repo`` (None
   when that is root itself); branch is unknown. Source 'live'.
3. The ``repo`` path is gone: the run's base commit (``integration.base`` or else the first
   readable ``tasks/*/base_ref`` for a plan, ``dwarf.head.before`` for a solo run) is looked for in
   the known roots, the roots steps 1 and 2 found in this batch that still exist. Exactly one
   match: root is that one and worktree is the ``repo`` text. Source 'base-commit'.
4. Otherwise today's behaviour: root is the ``repo`` text. Source 'path'. A run is never grouped
   under a repository it might not belong to.

Cost: every git call is an argument list with ``-C <absolute existing dir>``, stdin closed and a
short timeout; a failure or timeout means "not found". Within one ``resolve`` call ``git worktree
list`` runs at most once per distinct repo path and ``cat-file`` at most once per (root, sha).
Results from steps 2 and 3 are cached for the life of the process by run dir (the repository of a
run never changes); a step 4 result is cached with the known roots it was checked against and is
resolved again only when that set changes. Step 1 needs no git and is not cached.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import threading


_GIT_TIMEOUT = 5
# repo.identity is a few hundred bytes; anything bigger is not ours.
_MAX_IDENTITY_BYTES = 64 * 1024
# A commit id is one short line.
_MAX_LINE_BYTES = 4096
_SHA = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')

# (path, repo) -> identity, for runs resolved by step 2 or 3.
_RESOLVED: dict = {}
# (path, repo) -> (frozenset of known roots, repo was a directory, identity), for runs that fell
# through to step 4.
_FALLTHROUGH: dict = {}
_LOCK = threading.Lock()


def _identity(root, worktree=None, branch=None, source='path'):
    return {'root': root, 'worktree': worktree, 'branch': branch, 'source': source}


def _read_file(path, limit):
    """Up to ``limit`` bytes of a regular file as text, or None (missing, unreadable, not a file).

    One byte more than the limit is read so an oversized file is told apart from one that fits:
    that returns None as well.
    """
    fd = None
    try:
        # O_NONBLOCK: opening a fifo someone left in a run dir must not hang the reader.
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        data = b''
        while len(data) <= limit:
            chunk = os.read(fd, limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except (OSError, ValueError):
        return None
    finally:
        if fd is not None:
            os.close(fd)
    if len(data) > limit:
        return None
    return data.decode('utf-8', 'replace')


def _read_line(path):
    text = _read_file(path, _MAX_LINE_BYTES)
    if text is None:
        return None
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else None


def _is_dir(path) -> bool:
    try:
        return os.path.isabs(path) and os.path.isdir(path)
    except (OSError, ValueError):
        return False


def _real(path):
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return path


def _run_git(cwd, *args):
    """CompletedProcess of ``git -C cwd <args>``, or None when git could not run or timed out.

    ``cwd`` is an absolute existing directory. No shell, stdin closed, output captured.
    """
    try:
        return subprocess.run(
            ['git', '-C', cwd, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=_GIT_TIMEOUT, check=False,
            env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


# ---- step 1: repo.identity ---------------------------------------------------------------------

def _from_identity_file(run_dir):
    text = _read_file(os.path.join(run_dir, 'repo.identity'), _MAX_IDENTITY_BYTES)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    root = data.get('root')
    if not isinstance(root, str) or '\0' in root or not os.path.isabs(root):
        return None
    worktree, branch = data.get('worktree'), data.get('branch')
    return _identity(root, worktree if isinstance(worktree, str) else None,
                     branch if isinstance(branch, str) else None, 'identity')


# ---- step 2: the repo path is a live checkout --------------------------------------------------

def _checkout_identity(repo):
    """``(ran, identity)`` for the checkout ``repo``: identity is None when ``repo`` is no checkout
    or its listing could not be read safely; ran is False when git did not run or timed out.

    ``-z`` keeps a path that contains a newline in one piece. A git too old for it (exit 129)
    falls back to ``_legacy_identity``.
    """
    done = _run_git(repo, 'worktree', 'list', '--porcelain', '-z')
    if done is None:
        return False, None
    if done.returncode == 129:
        return _legacy_identity(repo)
    if done.returncode != 0:
        return True, None
    paths = []
    for line in done.stdout.decode('utf-8', 'replace').split('\0'):
        if line.startswith('worktree '):
            path = line[len('worktree '):]
            if os.path.isabs(path):
                paths.append(path)
    return True, _live_identity(repo, paths)


def _rev_parse_path(repo, option):
    """``(ran, path)`` for a ``git rev-parse`` option that prints one path, made absolute.

    One value per call, so a newline inside the path cannot split it: only git's final newline
    is dropped.
    """
    done = _run_git(repo, 'rev-parse', option)
    if done is None:
        return False, None
    out = done.stdout.decode('utf-8', 'replace')
    if done.returncode != 0 or not out.endswith('\n') or out == '\n':
        return True, None
    return True, os.path.join(repo, out[:-1])


def _git_dir_of(root):
    """The git dir of the working tree ``root`` (its ``.git`` directory or the one a ``.git``
    file names), or None."""
    if not _is_dir(root):
        return None
    dot = os.path.join(root, '.git')
    if os.path.isdir(dot):
        return dot
    text = _read_file(dot, _MAX_LINE_BYTES)
    if text is None or not text.startswith('gitdir: '):
        return None
    lines = text[len('gitdir: '):].splitlines()
    return os.path.join(root, lines[0]) if lines and lines[0] else None


def _legacy_identity(repo):
    """``(ran, identity)`` from line-separated porcelain output, for a git without ``-z``.

    Without ``-z`` a path containing a newline is split over several lines, and its fragments can
    look like porcelain fields or whole entries, including a path that names another repository.
    No reading of the lines can tell, so the shape is not trusted at all: the first line's path is
    taken as the main working tree only when its git dir is the common dir of ``repo``'s
    repository. The checkout holding ``repo`` comes from ``--show-toplevel``, which prints one path.
    """
    done = _run_git(repo, 'worktree', 'list', '--porcelain')
    if done is None:
        return False, None
    if done.returncode != 0:
        return True, None
    first = done.stdout.decode('utf-8', 'replace').split('\n', 1)[0]
    root = first[len('worktree '):] if first.startswith('worktree ') else ''
    if not os.path.isabs(root):
        return True, None
    root_git_dir = _git_dir_of(root)
    if root_git_dir is None:
        return True, None
    ran, common = _rev_parse_path(repo, '--git-common-dir')
    if common is None or _real(common) != _real(root_git_dir):
        return ran, None
    ran, top = _rev_parse_path(repo, '--show-toplevel')
    if not ran:
        return False, None
    # No toplevel (repo is inside a git dir): the verified root alone is listed.
    return True, _live_identity(repo, [root] + ([top] if top else []))


def _live_identity(repo, entries):
    """Identity from the worktree list of ``repo``; None when it listed nothing usable."""
    if not entries:
        return None
    root = entries[0]
    here, real_root = _real(repo), _real(root)
    holder = None
    for entry in entries:
        real = _real(entry)
        if (here == real or here.startswith(real.rstrip(os.sep) + os.sep)) and (
                holder is None or len(real) > len(holder[1])):
            holder = (entry, real)
    if holder is None:
        worktree = None if here == real_root else here
    else:
        worktree = None if holder[1] == real_root else holder[0]
    return _identity(root, worktree, None, 'live')


# ---- step 3: the base commit is in exactly one known root --------------------------------------

def _base_commit(run_dir, kind):
    """The run's base commit as lower-case hex, or None when unknown or not a commit id."""
    if kind == 'plan':
        line = _read_line(os.path.join(run_dir, 'integration.base'))
        if not line:
            try:
                with os.scandir(os.path.join(run_dir, 'tasks')) as entries:
                    names = sorted(entry.name for entry in entries)
            except (OSError, ValueError):
                names = []
            for name in names:
                line = _read_line(os.path.join(run_dir, 'tasks', name, 'base_ref'))
                if line:
                    break
    elif kind == 'solo':
        line = _read_line(os.path.join(run_dir, 'dwarf.head.before'))
    else:
        line = None
    line = (line or '').lower()
    # Only a full hex id ever reaches git: nothing that could be read as an option or a ref.
    return line if _SHA.fullmatch(line) else None


def _distinct_roots(roots):
    """Known roots without duplicates that are only different spellings of one directory."""
    seen = {}
    for root in sorted(roots):
        seen.setdefault(_real(root), root)
    return list(seen.values())


def _by_base_commit(sha, roots, has_commit):
    matches = [root for root in _distinct_roots(roots) if has_commit(root, sha)]
    return matches[0] if len(matches) == 1 else None


# ---- the batch ---------------------------------------------------------------------------------

def resolve(runs):
    """{run dir: Identity} for every ``(path, repo, kind)`` in ``runs``; see the module docstring."""
    runs = list(runs)
    result = {}
    checkouts = {}
    commit_checks = {}

    def checkout(repo):
        if repo not in checkouts:
            checkouts[repo] = _checkout_identity(repo)
        found = checkouts[repo][1]
        return dict(found) if found is not None else None

    def has_commit(root, sha):
        key = (root, sha)
        if key not in commit_checks:
            done = _run_git(root, 'cat-file', '-e', sha + '^{commit}')
            commit_checks[key] = done is not None and done.returncode == 0
        return commit_checks[key]

    known_roots = set()
    pending = []
    # Steps 1 and 2 for the whole batch first: step 3 needs every root they found.
    for given, repo, kind in runs:
        path = os.fspath(given)
        repo = repo if isinstance(repo, str) and repo.strip() else None
        if repo is None:
            result[given] = _identity(None)
            continue
        found = _from_identity_file(path)
        if found is None:
            with _LOCK:
                found = _RESOLVED.get((path, repo))
            if found is not None:
                found = dict(found)
        if found is None and _is_dir(repo):
            with _LOCK:
                cached = _FALLTHROUGH.get((path, repo))
            # A directory that was no checkout last time: no git call until the process ends. A
            # path that was missing then (a restored worktree) is looked at again.
            if cached is None or not cached[1]:
                found = checkout(repo)
                if found is not None:
                    with _LOCK:
                        _RESOLVED[(path, repo)] = dict(found)
        if found is None:
            pending.append((given, path, repo, kind))
            continue
        result[given] = found
        if found['source'] != 'base-commit' and _is_dir(found['root']):
            known_roots.add(found['root'])

    roots_key = frozenset(known_roots)
    for given, path, repo, kind in pending:
        key = (path, repo)
        with _LOCK:
            cached = _FALLTHROUGH.get(key)
        live = _is_dir(repo)
        # Known roots only matter to a run whose repo is gone; a live directory keeps its answer.
        # An answer from before the path appeared or vanished is stale either way.
        if cached is not None and cached[1] == live and (live or cached[0] == roots_key):
            result[given] = dict(cached[2])
            continue
        found = None
        if not live:
            sha = _base_commit(path, kind)
            root = _by_base_commit(sha, known_roots, has_commit) if sha else None
            if root is not None:
                found = _identity(root, repo, None, 'base-commit')
                with _LOCK:
                    _RESOLVED[key] = dict(found)
        if found is None:
            found = _identity(repo)
            # A live repo whose git call timed out is retried next time rather than remembered.
            if not (live and not checkouts.get(repo, (True, None))[0]):
                with _LOCK:
                    _FALLTHROUGH[key] = (roots_key, live, dict(found))
        result[given] = found
    return result
