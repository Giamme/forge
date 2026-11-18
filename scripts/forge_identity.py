"""Which repository a checkout or a run belongs to, and every checkout forge has seen of it.

The run registry and the dismissal store are keyed by the checkout path, so a run
started from a linked worktree is filed under that worktree. Once the worktree is
gone, that path no longer names the repository. This module answers for the
repository itself: the main working tree, the same identity key `forge_registry`
uses for that tree, the checkouts forge has recorded and the run dirs registered
under any of them.

It only reads. A missing path, an unreadable file and a git failure are unknown,
never an exception. `identity` and `checkouts` are cached per process.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess

import forge_locations
import forge_registry

# A file in <run>/tasks/<id>/ sits three directories under the run dir.
_RUN_DIR_WALK = 3
_REPO_READ_MAX = 64 * 1024
_REGISTRY_READ_MAX = 1 << 20
_GIT_TIMEOUT = 10
# Same shape as forge-issues.py github_slug.
_ORIGIN_SLUG = re.compile(r'github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$')

_identity_cache: dict = {}
_checkouts_cache: dict = {}


def identity(path) -> dict | None:
    """`{"root", "git", "key", "checkout"}` for a checkout, a run dir, or a file inside a run dir.

    None when `path` is neither an existing directory nor a run dir with a `repo` file.
    """
    key = _cache_key(path)
    if key is not None and key in _identity_cache:
        return _copy(_identity_cache[key])
    try:
        result = _identity_uncached(path)
    except Exception:  # noqa: BLE001 -- a missing path or a git failure is unknown, not an error
        result = _directory_fallback(path)
    if key is not None:
        _identity_cache[key] = result
    return _copy(result)


def checkouts(ident) -> list[str]:
    """Every checkout of this repository forge has seen, root first, then sorted.

    The root, the `checkout` and `root` of every locations entry for the same git dir
    or the same root (gone entries included), and the checkout named by the `repo` file
    of every run dir in those checkouts' registry files. Existing or not. Deduplicated
    by realpath.
    """
    try:
        if not isinstance(ident, dict) or not isinstance(ident.get('root'), str) or not ident['root']:
            return []
        root = ident['root']
        git = ident.get('git') if isinstance(ident.get('git'), str) else None
        cache_key = (_safe_real(root), git)
        if cache_key in _checkouts_cache:
            return list(_checkouts_cache[cache_key])
        result = _checkouts_uncached(root, git)
        _checkouts_cache[cache_key] = result
        return list(result)
    except Exception:  # noqa: BLE001 -- an unreadable locations or registry file is unknown
        return []


def run_dirs(ident) -> list[str]:
    """Run dirs registered under any checkout of `ident`, existing directories, newest first."""
    try:
        found: dict[str, str] = {}
        for checkout in checkouts(ident):
            for line in _registry_lines(checkout):
                if not os.path.isdir(line):
                    continue
                real = _safe_real(line)
                if real not in found and os.path.isdir(real):
                    found[real] = real
        paths = list(found)
        paths.sort(key=lambda item: (-_mtime(item), item))
        return paths
    except Exception:  # noqa: BLE001 -- a missing registry is an empty list
        return []


def github_slug(ident) -> str | None:
    """`owner/repo` from `origin` of the first existing checkout, or None when none answers."""
    try:
        candidates = checkouts(ident)
    except Exception:  # noqa: BLE001
        return None
    for checkout in candidates:
        try:
            exists = bool(checkout) and os.path.isdir(checkout)
        except OSError:
            continue
        if not exists:
            continue
        url = _origin(checkout)
        if not url:
            return None
        match = _ORIGIN_SLUG.search(url.strip())
        return f'{match.group(1)}/{match.group(2)}' if match else None
    return None


def _identity_uncached(path) -> dict | None:
    text = os.fspath(path)
    found = _find_run(text)
    if found is not None:
        run_dir, checkout = found
        if os.path.isdir(checkout):
            return _live(checkout)
        return _gone(run_dir, checkout)
    if os.path.isdir(text):
        return _live(text)
    return None


def _directory_fallback(path) -> dict | None:
    try:
        text = os.fspath(path)
        if os.path.isdir(text):
            return _live(text)
    except Exception:  # noqa: BLE001
        return None
    return None


def _find_run(path: str) -> tuple[str, str] | None:
    """`(run dir, checkout path)` when `path` is a run dir, or a file at most three levels inside one."""
    if os.path.isdir(path):
        line = _first_line(os.path.join(path, 'repo'))
        return (path, line) if line else None
    current = os.path.dirname(os.path.abspath(path))
    for _ in range(_RUN_DIR_WALK):
        line = _first_line(os.path.join(current, 'repo'))
        if line:
            return current, line
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _live(checkout: str) -> dict:
    git, root = forge_locations.repository(checkout)
    checkout_real = _safe_real(checkout)
    if not root:
        root = checkout_real
    return {'root': root, 'git': git, 'key': _key(root), 'checkout': checkout_real}


def _gone(run_dir: str, checkout: str) -> dict:
    # The checkout is gone, so git cannot be asked. repo.identity keeps the main
    # working tree; without it the path recorded in `repo` is all forge still has.
    root = _recorded_root(run_dir) or checkout
    return {'root': root, 'git': None, 'key': _key(root), 'checkout': None}


def _checkouts_uncached(root: str, git: str | None) -> list[str]:
    paths = [root]
    for entry in forge_locations.entries(present=False):
        if _matches(entry, git, root):
            for name in ('checkout', 'root'):
                value = entry.get(name)
                if isinstance(value, str) and value:
                    paths.append(value)
    # One pass over the checkouts already collected. A checkout that shows up only
    # as a run's `repo` path is not itself scanned for further runs.
    for checkout in list(paths):
        for run_dir in _registry_lines(checkout):
            recorded = _first_line(os.path.join(run_dir, 'repo'))
            if recorded:
                paths.append(recorded)
    return _ordered(root, paths)


def _matches(entry: dict, git: str | None, root: str) -> bool:
    entry_root = entry.get('root')
    if isinstance(entry_root, str) and _same_path(entry_root, root):
        return True
    entry_git = entry.get('git')
    return git is not None and isinstance(entry_git, str) and _same_path(entry_git, git)


def _ordered(root: str, paths: list[str]) -> list[str]:
    seen = {_safe_real(root)}
    extras = []
    for path in paths:
        if not isinstance(path, str) or not path:
            continue
        real = _safe_real(path)
        if real in seen:
            continue
        seen.add(real)
        extras.append(real)
    extras.sort()
    return [root, *extras]


def _recorded_root(run_dir: str) -> str | None:
    data = _read_bytes(os.path.join(run_dir, 'repo.identity'), _REPO_READ_MAX)
    if not data:
        return None
    try:
        parsed = json.loads(data.decode('utf-8', 'replace'))
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    recorded = parsed.get('root')
    if not isinstance(recorded, str):
        return None
    return recorded or None


def _registry_lines(checkout: str) -> list[str]:
    try:
        raw = _read_bytes(forge_registry.runs_registry(checkout), _REGISTRY_READ_MAX)
    except (OSError, ValueError, RuntimeError, TypeError):
        return []
    if not raw:
        return []
    lines = []
    for line in raw.decode('utf-8', 'replace').splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            lines.append(line)
    return lines


def _first_line(path: str) -> str | None:
    data = _read_bytes(path, _REPO_READ_MAX)
    if not data:
        return None
    parts = data.splitlines()
    if not parts:
        return None
    line = parts[0].decode('utf-8', 'replace').strip()
    return line or None


def _read_bytes(path, limit: int) -> bytes | None:
    """The start of a regular file. A FIFO, a directory or a missing path is unknown, and never blocks."""
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        left = limit
        while left > 0:
            try:
                block = os.read(fd, left)
            except OSError:
                return None
            if not block:
                break
            chunks.append(block)
            left -= len(block)
        return b''.join(chunks)
    finally:
        os.close(fd)


def _origin(checkout: str) -> str | None:
    try:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('GIT_') or k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')}
        done = subprocess.run(
            ['git', '--no-optional-locks', '-C', os.fspath(checkout), 'remote', 'get-url', 'origin'],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT,
            errors='replace', env=env)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def _key(root: str) -> str:
    try:
        return forge_registry.repo_key(root)
    except (OSError, ValueError, RuntimeError, UnicodeError):
        return hashlib.sha256(_safe_real(root).encode('utf-8', 'replace')).hexdigest()[:16]


def _cache_key(path) -> str | None:
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, ValueError, TypeError):
        return None


def _safe_real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError, TypeError):
        return path


def _same_path(left: str, right: str) -> bool:
    return left == right or _safe_real(left) == _safe_real(right)


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def _copy(result):
    return None if result is None else dict(result)
