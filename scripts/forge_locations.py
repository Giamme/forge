"""Where forge has put things on this machine: every run dir and every worktree it created.

forge's index of its own footprint, so `forge du`, `forge runs compact`, forge-ui's Disk and Worktrees
screens and `forge runs rm` know where to look without guessing from the run registry, the dataset and
scans of `.forge-worktrees` folders. It records what forge did, never what is there now: every reader
still asks the disk and git, and marks an entry's path that is gone. It is housekeeping like the run
registry, so it is written whatever FORGE_DATASET says; FORGE_RUN_REGISTRY=off turns both off.

The file is `<config home>/forge/locations.jsonl` (beside the `runs` registry), append-only, one JSON
object per line:

  {"v": 1, "at": <unix time>, "op": "add" | "gone", "kind": "run" | "worktree", "path": <real path>,
   "git": <the repository's common git dir>, "root": <its main working tree>, "checkout": <the checkout
   forge ran in>, "run_id", "run_dir", "task", "branch", "wt_kind": task | integration | candidate |
   attribute, "by": <what wrote it>}

`git` and `root` name the repository itself, so a checkout deleted by hand, or reached through a
symlink, hides nothing: git answers for a repository from its main working tree or any worktree.
`load()` folds the lines by (kind, path): the last `add` wins and a later `gone` marks it gone (with its
time) until another `add`. A line that is not one of these objects is skipped.

Writing never fails or slows a run: the lock is tried briefly and the line is appended without it when
it stays busy (one write per call, so lines do not interleave); every error is swallowed. The first
`ensure_imported()` (from `forge du`, `forge runs compact` and the background check at run start) fills
the file from the run registry and the worktree folders already on disk, once.

CLI, for the runners (always exits 0, prints nothing unless asked):
  forge_locations.py run <run dir>                  a run dir, its repository read from <run dir>/repo
  forge_locations.py worktree created|removed <run dir> <checkout> <path> <branch|-> <kind> [task]
  forge_locations.py list [--json] [--all]          what it holds (--all: gone entries too)
  forge_locations.py import [--force]               the one-time import, again with --force
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

VERSION = 1
LOCK_WAIT_S = 0.5
READ_MAX = 64 << 20
KINDS = ('run', 'worktree')
WT_KINDS = ('task', 'integration', 'candidate', 'attribute')
_GIT_TIMEOUT = 10
_IDENTITY: dict = {}


def config_dir() -> Path:
    """`<config home>/forge`, resolved as the run registry resolves it."""
    value = os.environ.get('XDG_CONFIG_HOME') or ''
    base = Path(value).expanduser() if value else None
    if base is None or not base.is_absolute():
        base = Path.home() / '.config'
    return base / 'forge'


def file_path() -> Path:
    return config_dir() / 'locations.jsonl'


def enabled() -> bool:
    return os.environ.get('FORGE_RUN_REGISTRY', '').strip().lower() != 'off' and config_dir().is_absolute()


def _real(path) -> str:
    try:
        return os.path.realpath(os.fspath(path))
    except (OSError, ValueError, TypeError):
        return os.fspath(path)


def _git(cwd, *args):
    try:
        done = subprocess.run(['git', '--no-optional-locks', '-C', os.fspath(cwd), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT, errors='replace',
                              env={k: v for k, v in os.environ.items() if not k.startswith('GIT_') or k in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM')})
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, ''
    return done.returncode, done.stdout


def repository(checkout) -> tuple[str | None, str | None]:
    """(the common git dir, the main working tree) of the repository `checkout` belongs to; (None, None) when
    git cannot say. Cached per checkout for the process."""
    if not checkout or not os.path.isdir(checkout):
        return None, None
    key = _real(checkout)
    if key in _IDENTITY:
        return _IDENTITY[key]
    found = (None, None)
    code, out = _git(checkout, 'rev-parse', '--git-common-dir')
    if code == 0 and out.strip():
        common = out.strip()
        common = _real(common if os.path.isabs(common) else os.path.join(checkout, common))
        root = None
        code, listed = _git(checkout, 'worktree', 'list', '--porcelain')
        if code == 0:
            first = next((line[len('worktree '):] for line in listed.splitlines() if line.startswith('worktree ')), None)
            root = _real(first) if first else None
        found = (common, root)
    _IDENTITY[key] = found
    return found


# ---------------------------------------------------------------------------------- writing

@contextlib.contextmanager
def _lock(wait_s):
    """The file's lock, tried for `wait_s`; yields whether it is held. Never raises."""
    fd = None
    try:
        fd = os.open(config_dir() / 'locations.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        yield True
    except OSError:
        yield False
    finally:
        if fd is not None:
            os.close(fd)


def append(records: list[dict]) -> bool:
    """Appends records in one write. Never raises; False when nothing could be written."""
    if not records or not enabled():
        return False
    try:
        config_dir().mkdir(parents=True, exist_ok=True)
        data = ''.join(json.dumps(dict(r, v=VERSION), sort_keys=True, separators=(',', ':')) + '\n' for r in records).encode()
        with _lock(LOCK_WAIT_S):
            fd = os.open(file_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW, 0o644)
            try:
                if not os.path.isfile(file_path()):
                    return False
                os.write(fd, data)
            finally:
                os.close(fd)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _repo_fields(checkout) -> dict:
    git, root = repository(checkout)
    return dict(checkout=_real(checkout) if checkout else None, git=git, root=root)


def run_record(run_dir, checkout=None, by='forge') -> dict | None:
    run_dir = _real(run_dir)
    if checkout is None:
        checkout = _first_line(os.path.join(run_dir, 'repo'))
    if not checkout or not os.path.isabs(checkout):
        return None
    run_id = _first_line(os.path.join(run_dir, 'run_id')) or os.path.basename(run_dir)
    return dict(at=time.time(), op='add', kind='run', path=run_dir, run_id=run_id, run_dir=run_dir, by=by,
                **_repo_fields(checkout))


def add_run(run_dir, checkout=None, by='forge') -> bool:
    """Records a run dir; a run already listed, present and with the same repository, is not written again
    (every `run` and `retry` registers its run)."""
    record = run_record(run_dir, checkout, by)
    if record is None or not enabled():
        return False
    known = load().get(('run', record['path']))
    if known and not known['gone'] and all(known.get(k) == record.get(k) for k in ('checkout', 'git', 'root', 'run_id')):
        return True
    return append([record])


def worktree_record(op, path, checkout=None, run_dir=None, run_id=None, branch=None, wt_kind=None, task=None, by='forge') -> dict:
    record = dict(at=time.time(), op='add' if op in ('add', 'created') else 'gone', kind='worktree', path=_real(path),
                  run_dir=_real(run_dir) if run_dir else None, run_id=run_id, task=task or None,
                  branch=None if branch in (None, '', '-') else branch, wt_kind=wt_kind if wt_kind in WT_KINDS else None, by=by)
    if record['op'] == 'add' or checkout:
        record.update(_repo_fields(checkout))
    if record['run_id'] is None and run_dir:
        record['run_id'] = _first_line(os.path.join(run_dir, 'run_id')) or os.path.basename(_real(run_dir))
    return record


def add_worktree(path, checkout, **fields) -> bool:
    return append([worktree_record('add', path, checkout, **fields)])


def gone(kind, path, by='forge') -> bool:
    if kind not in KINDS:
        return False
    return append([dict(at=time.time(), op='gone', kind=kind, path=_real(path), by=by)])


def _first_line(path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not os.path.isfile(path):
                return None
            line = stream.readline(65536).decode('utf-8', 'replace').strip()
    except OSError:
        return None
    return line or None


# ---------------------------------------------------------------------------------- reading

def load(path=None) -> dict:
    """{(kind, path): entry} folded from the file: each entry is the merged fields of its adds, with `created`
    (the first add), `updated`, and `gone` (the time of a `gone` after the last add, else None). Read only."""
    target = Path(path) if path is not None else file_path()
    out: dict = {}
    try:
        with open(target, 'rb') as stream:
            data = stream.read(READ_MAX)
    except OSError:
        return out
    for raw in data.splitlines():
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('kind') not in KINDS or not isinstance(record.get('path'), str) \
                or not os.path.isabs(record['path']) or record.get('op') not in ('add', 'gone'):
            continue
        at = record.get('at') if isinstance(record.get('at'), (int, float)) else None
        key = (record['kind'], record['path'])
        entry = out.get(key)
        if record['op'] == 'gone':
            if entry is not None:
                entry['gone'] = at or entry.get('updated') or 0
            continue
        if entry is None:
            entry = out[key] = dict(kind=record['kind'], path=record['path'], created=at, updated=at, gone=None)
        for name, value in record.items():
            if name not in ('v', 'op', 'at', 'kind', 'path') and value is not None:
                entry[name] = value
        entry.update(updated=at, gone=None)
    return out


def entries(kind=None, present=True, path=None) -> list[dict]:
    """The folded entries, of one kind when given, only those not marked gone when `present`."""
    return [e for (k, _), e in sorted(load(path).items()) if (kind is None or k == kind) and (not present or not e['gone'])]


def repositories(path=None) -> list[str]:
    """Every repository the file names, as paths git can be asked from: main working trees, then checkouts."""
    seen, out = set(), []
    for e in load(path).values():
        for candidate in (e.get('root'), e.get('checkout')):
            if isinstance(candidate, str) and os.path.isabs(candidate) and candidate not in seen:
                seen.add(candidate)
                out.append(candidate)
    return out


# ---------------------------------------------------------------------------------- the one-time import

def marker() -> Path:
    return config_dir() / 'locations.imported'


def ensure_imported(force=False) -> dict | None:
    """Fills the file from the run registry and the worktree folders on disk, once (a marker records it).
    Returns what was added, or None when it had been done already or is off. Never raises."""
    if not enabled() or (marker().exists() and not force):
        return None
    try:
        return _import()
    except Exception:   # noqa: BLE001 -- housekeeping never fails its caller
        return None


def _import() -> dict:
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import forge_registry
    import forge_worktrees
    known = load()
    runs, records, seen_repos = [], [], {}
    registry = forge_registry.registry_dir()
    for listing in sorted(registry.glob('*.txt')) if registry.is_dir() else []:
        try:
            lines = listing.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line and not line.startswith('#') and os.path.isabs(line) and os.path.isdir(line):
                runs.append(_real(line))
    for run_dir in dict.fromkeys(runs):
        record = run_record(run_dir, by='import')
        if record is None:
            continue
        if ('run', run_dir) not in known:
            records.append(record)
        checkout = record['checkout']
        if not checkout or not os.path.isdir(checkout) or not forge_worktrees.RUN_ID.fullmatch(record['run_id'] or ''):
            continue
        root = forge_worktrees.wt_root(checkout, record['run_id'])
        if checkout not in seen_repos:
            code, out = _git(checkout, 'worktree', 'list', '--porcelain', '-z')
            seen_repos[checkout] = forge_worktrees.parse_porcelain(out, z=True) if code == 0 else []
        tasks = _tasks(run_dir)
        for wt in seen_repos[checkout]:
            kind = forge_worktrees.owner(wt['path'], wt['branch'], record['run_id'], tasks, root)
            if kind and ('worktree', _real(wt['path'])) not in known:
                branch = (wt['branch'] or '').replace('refs/heads/', '', 1) or None
                records.append(worktree_record('add', wt['path'], checkout, run_dir=run_dir, run_id=record['run_id'],
                                               branch=branch, wt_kind=kind, task=branch.split('/', 2)[-1] if kind == 'task' and branch else None,
                                               by='import'))
    append(records)
    config_dir().mkdir(parents=True, exist_ok=True)
    marker().write_text('%d\n' % int(time.time()))
    return dict(runs=sum(1 for r in records if r['kind'] == 'run'), worktrees=sum(1 for r in records if r['kind'] == 'worktree'))


def _tasks(run_dir) -> list[str]:
    try:
        text = Path(run_dir, 'tasks.tsv').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    return [line.split('\t', 1)[0] for line in text.splitlines() if line.strip() and not line.startswith('#')]


# ---------------------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args[:1] == ['run'] and len(args) == 2:
            add_run(args[1])
        elif args[:1] == ['worktree'] and len(args) in (7, 8) and args[1] in ('created', 'removed'):
            _, op, run_dir, checkout, path, branch, kind, *task = args
            append([worktree_record(op, path, checkout, run_dir=run_dir, branch=branch, wt_kind=kind,
                                    task=task[0] if task else None)])
        elif args[:1] == ['list']:
            found = entries(present='--all' not in args)
            if '--json' in args:
                print(json.dumps(found, indent=2))
            else:
                for e in found:
                    print('%-8s %s%s' % (e['kind'], e['path'], '  (gone)' if e['gone'] else ''))
        elif args[:1] == ['import']:
            result = ensure_imported(force='--force' in args)
            print(json.dumps(result))
    except Exception:   # noqa: BLE001 -- the runners call this: it must never reach a run
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
