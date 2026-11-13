"""What forge keeps on disk, what can go, and taking it back (#139).

One set of rules for `forge du` (what it reports), `forge runs compact` (what it removes or
gzips), forge-ui's Disk screen and the storage policy at run start, so they cannot disagree.

A run dir holds, by what this module calls them:

  snapshots   review snapshots: a directory named `review` or `review-*` that holds a `.git`
              directory and sits beside a `review.base` file (forge_review_snapshot writes
              both). Task reviews (`tasks/<id>/review-<n>-<pid>`), final reviews
              (`final-review-*/review-<pid>`), shadow reviews (`shadow/<task>/<n>-<k>/review-snapshot`),
              adopt (`<dir>/review`) and solo runs (`review-<time>-<pid>`). Nothing reads one after
              its review ends; since 0.18 each is removed then, so these are leftovers of older runs
              or of a review stopped by a signal.
  logs        `*.log`, `*.jsonl` and their `.gz`. `compressible` lists the ones `compact --logs`
              gzips: a dwarf's or reviewer's own log in its execution record,
              `attempts/<role>-<32 hex>/<role>.log` (role dwarf or qa), of at least LOG_MIN bytes,
              outside a Fractal tree. Those copies are written once, when the execution ends; the
              log a running agent writes, and every log a retry reads, is the one in the task dir,
              which is never touched. forge-ui reads the copy, plain or gzipped.
  strays      what forge does not create: a dependency directory (DEP_DIRS) or a `.git` outside a
              snapshot, such as a dependency install someone ran by hand inside a run dir. Reported
              with its size, never removed.
  fractal     Fractal's own workspaces (`.../nodes/<id>/product`), opt-in and managed by Fractal.
  other       everything else: prompts, diffs, verdicts, metrics, the run's own records.

Sizes are disk usage (allocated blocks, as `du` counts them), and a file with several hard links
counts once. Nothing here follows a symbolic link: a snapshot links the attempt's dependency
directories into itself, and those are the worktree's, not the run's.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import stat
import time
from pathlib import Path

DEP_DIRS = frozenset('node_modules bower_components .venv venv vendor .bundle Pods deps _build .dart_tool elm-stuff .build'.split())
LOG_MIN = 64 << 10                  # a smaller log gains next to nothing from gzip
LOG_ROLES = ('dwarf', 'qa')
ATTEMPT_DIR = re.compile(r'(dwarf|qa)-[0-9a-f]{32}')
PART = '.gz.part'                   # a gzip being written; one left by an interrupted compact is removed
GB = 1 << 30
DEFAULT_WARN_GB = 20.0
COMPACT_MODES = ('auto', 'off')


def usage(info) -> int:
    """Bytes a file takes on disk: its allocated blocks, else its size."""
    blocks = getattr(info, 'st_blocks', None)
    return blocks * 512 if blocks is not None else info.st_size


def _lstat(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def is_snapshot(path) -> bool:
    """Whether `path` is a review snapshot by the rule above (never a link)."""
    path = os.fspath(path)
    name = os.path.basename(path.rstrip('/'))
    if name != 'review' and not name.startswith('review-'):
        return False
    info, git = _lstat(path), _lstat(os.path.join(path, '.git'))
    base = _lstat(os.path.join(os.path.dirname(path.rstrip('/')), 'review.base'))
    return (info is not None and stat.S_ISDIR(info.st_mode) and git is not None and stat.S_ISDIR(git.st_mode)
            and base is not None and stat.S_ISREG(base.st_mode))


def is_compressible_log(path, info=None) -> bool:
    """Whether `compact --logs` gzips this file (the rule above); `info` is its lstat when known."""
    path = os.fspath(path)
    parent = os.path.dirname(path)
    match = ATTEMPT_DIR.fullmatch(os.path.basename(parent))
    if not match or os.path.basename(path) != match.group(1) + '.log':
        return False
    if os.path.basename(os.path.dirname(parent)) != 'attempts' or 'nodes' in Path(parent).parts:
        return False
    info = info if info is not None else _lstat(path)
    return info is not None and stat.S_ISREG(info.st_mode) and info.st_size >= LOG_MIN


class _Seen:
    """Hard-linked files count once across one scan."""

    def __init__(self):
        self.inodes = set()

    def first(self, info) -> bool:
        if info.st_nlink <= 1:
            return True
        key = (info.st_dev, info.st_ino)
        if key in self.inodes:
            return False
        self.inodes.add(key)
        return True


def tree_usage(path, seen=None, deadline=None) -> tuple[int, bool]:
    """(disk usage of everything under `path`, complete). Stops at `deadline` (time.monotonic) with a lower bound."""
    seen = seen or _Seen()
    total, stack = 0, [os.fspath(path)]
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            return total, False
        directory = stack.pop()
        try:
            with os.scandir(directory) as listing:
                entries = list(listing)
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                stack.append(entry.path)
            elif seen.first(info):
                total += usage(info)
    return total, True


def scan(run_dir, deadline=None) -> dict:
    """What one run dir holds, by kind (see the module docstring). Paths in the answer are absolute.

    {path, total, snapshots: {bytes, count, items: [{path, bytes}]},
     logs: {bytes, gz_bytes, compressible_bytes, compressible: [{path, bytes, size}]},
     strays: [{path, bytes, kind}], fractal, other, parts: [path], complete}
    """
    root = os.path.realpath(os.fspath(run_dir))
    seen = _Seen()
    out = dict(path=root, total=0, snapshots=dict(bytes=0, count=0, items=[]),
               logs=dict(bytes=0, gz_bytes=0, compressible_bytes=0, compressible=[]),
               strays=[], fractal=0, other=0, parts=[], complete=True)
    stack = [root]
    while stack:
        if deadline is not None and time.monotonic() > deadline:
            out['complete'] = False
            break
        directory = stack.pop()
        try:
            with os.scandir(directory) as listing:
                entries = list(listing)
        except OSError:
            continue
        for entry in entries:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                if is_snapshot(entry.path):
                    size, done = tree_usage(entry.path, seen, deadline)
                    out['snapshots']['items'].append(dict(path=entry.path, bytes=size))
                    out['snapshots']['bytes'] += size
                    out['snapshots']['count'] += 1
                    out['complete'] = out['complete'] and done
                elif entry.name == 'product' and 'nodes' in Path(directory).parts:
                    size, done = tree_usage(entry.path, seen, deadline)
                    out['fractal'] += size
                    out['complete'] = out['complete'] and done
                elif entry.name in DEP_DIRS or entry.name == '.git':
                    size, done = tree_usage(entry.path, seen, deadline)
                    out['strays'].append(dict(path=entry.path, bytes=size,
                                              kind='git' if entry.name == '.git' else 'dependencies'))
                    out['complete'] = out['complete'] and done
                else:
                    stack.append(entry.path)
                continue
            if not seen.first(info):
                continue
            size = usage(info)
            name = entry.name
            if name.endswith(PART):
                out['parts'].append(entry.path)
            if name.endswith(('.log', '.jsonl')):
                out['logs']['bytes'] += size
                if is_compressible_log(entry.path, info):
                    out['logs']['compressible'].append(dict(path=entry.path, bytes=size, size=info.st_size))
                    out['logs']['compressible_bytes'] += size
            elif name.endswith(('.log.gz', '.jsonl.gz')):
                out['logs']['gz_bytes'] += size
            else:
                out['other'] += size
    stray = sum(s['bytes'] for s in out['strays'])
    out['total'] = (out['snapshots']['bytes'] + out['logs']['bytes'] + out['logs']['gz_bytes'] + stray
                    + out['fractal'] + out['other'])
    out['snapshots']['items'].sort(key=lambda s: s['path'])
    out['strays'].sort(key=lambda s: -s['bytes'])
    return out


GZIP_RATIO = 0.25                   # what a dwarf's or reviewer's log shrinks to, measured on real runs


def reclaimable(found: dict, logs: bool = True) -> int:
    """Bytes `compact` would take back: every snapshot, and (logs) an estimate for the compressible logs."""
    estimate = int(found['logs']['compressible_bytes'] * (1 - GZIP_RATIO)) if logs else 0
    return found['snapshots']['bytes'] + estimate


# ---------------------------------------------------------------------------------- changing things

def remove_snapshot(path, run_dir) -> str | None:
    """Removes one review snapshot under `run_dir`. Returns None, or why it was not removed."""
    path, root = os.fspath(path), os.path.realpath(os.fspath(run_dir))
    real = os.path.realpath(path)
    if real == root or not real.startswith(root.rstrip('/') + '/'):
        return 'outside the run dir'
    if not is_snapshot(path):
        return 'no longer a review snapshot'
    errors = []
    shutil.rmtree(path, onerror=lambda _f, p, e: errors.append('%s: %s' % (p, e[1])))   # never follows a link
    return ('; '.join(errors[:3])) if errors else None


def compress_log(path) -> tuple[int, str | None]:
    """Gzips one log into `<log>.gz` with the same mode and times, then removes the plain file.

    Returns (bytes taken back, None) or (0, why not). The plain file goes only once the gzip is
    complete on disk and the log did not change meanwhile; a `.gz` already there is never replaced.
    """
    path = os.fspath(path)
    target, part = path + '.gz', path + PART
    before = _lstat(path)
    if before is None or not stat.S_ISREG(before.st_mode):
        return 0, 'not a regular file'
    if os.path.lexists(target):
        return 0, target + ' exists'
    if os.path.lexists(part):
        try:
            os.unlink(part)
        except OSError as error:
            return 0, 'cannot remove %s: %s' % (part, error.strerror or error)
    try:
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, stat.S_IMODE(before.st_mode))
        with os.fdopen(fd, 'wb') as raw, open(path, 'rb') as source:
            with gzip.GzipFile(filename=os.path.basename(path), mode='wb', fileobj=raw, compresslevel=6,
                               mtime=int(before.st_mtime)) as packed:
                shutil.copyfileobj(source, packed, 1 << 20)
            raw.flush()
            os.fsync(raw.fileno())
        os.utime(part, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = _lstat(path)
        if after is None or (after.st_size, after.st_mtime_ns, after.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
            os.unlink(part)
            return 0, 'changed while it was being compressed'
        os.link(part, target)            # fails when a .gz appeared meanwhile: nothing is replaced
        os.unlink(part)
        packed_info = os.lstat(target)
        os.unlink(path)
    except OSError as error:
        try:
            os.unlink(part)
        except OSError:
            pass
        return 0, error.strerror or str(error)
    return max(0, usage(before) - usage(packed_info)), None


def read_log(path, cap=None) -> bytes | None:
    """The bytes of a log, plain or gzipped (`.gz`), the last `cap` of them when given; None when unreadable."""
    path = os.fspath(path)
    try:
        if path.endswith('.gz'):
            with gzip.open(path, 'rb') as stream:
                data = stream.read()
        else:
            with open(path, 'rb') as stream:
                data = stream.read()
    except (OSError, EOFError, gzip.BadGzipFile):
        return None
    return data[-cap:] if cap is not None and len(data) > cap else data


# ---------------------------------------------------------------------------------- policy and footprint

def settings(features: dict | None) -> dict:
    """{compact: auto|off, warn_gb: float|None} from the user `features` object (decide.json).

    FORGE_STORAGE_COMPACT=off and FORGE_STORAGE_WARN_GB=off turn either off for one process.
    """
    storage = (features or {}).get('storage')
    storage = storage if isinstance(storage, dict) else {}
    compact = storage.get('compact') if storage.get('compact') in COMPACT_MODES else 'auto'
    warn = parse_gb(storage.get('warn_gb'), DEFAULT_WARN_GB)
    if os.environ.get('FORGE_STORAGE_COMPACT', '').strip().lower() == 'off':
        compact = 'off'
    env_warn = os.environ.get('FORGE_STORAGE_WARN_GB', '').strip()
    if env_warn:
        warn = parse_gb(env_warn, warn)
    return dict(compact=compact, warn_gb=warn)


def parse_gb(value, default):
    """A positive number of gigabytes, None for `off`, else `default`."""
    if isinstance(value, str) and value.strip().lower() == 'off':
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 and number == number and number != float('inf') else default


def footprint_path(home: Path) -> Path:
    return Path(home) / 'storage' / 'footprint.json'


def load_footprints(home: Path) -> dict:
    """{repo real path: {bytes, runs, at}} as the last scan left it; {} when there is none."""
    try:
        data = json.loads(footprint_path(home).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_footprints(home: Path, updates: dict) -> None:
    """Merges {repo: {bytes, runs}} into the footprint file, stamped now. Never raises."""
    try:
        path = footprint_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = load_footprints(home)
        now = time.time()
        for repo, entry in updates.items():
            data[str(repo)] = dict(entry, at=now)
        tmp = path.with_name('.footprint.%d.tmp' % os.getpid())
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, path)
    except OSError:
        pass


def size_text(count) -> str:
    value = float(count or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'TB':
            return ('%d %s' % (value, unit)) if unit == 'B' else ('%.1f %s' % (value, unit))
        value /= 1024
    return '%.1f TB' % value


# ---------------------------------------------------------------------------------- the whole report

def _scripts():
    import sys
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)


def _first_line(path) -> str | None:
    try:
        with open(path, 'rb') as stream:
            line = stream.read(64 * 1024).decode('utf-8', 'replace').split('\n', 1)[0].strip()
    except OSError:
        return None
    return line or None


def _matches(entry, repo_real) -> bool:
    """Whether a location entry belongs to the repository at `repo_real`: as its checkout or its main working tree."""
    return repo_real in (entry.get('checkout'), entry.get('root'))


def run_dirs(repo=None) -> list[tuple[str, str | None]]:
    """[(run dir, repo the run names)] for `repo` (a path) or, None, for every repo: what the run registry lists,
    then the run dirs forge's location list (forge_locations.py) knows and the registry does not."""
    _scripts()
    import forge_locations
    import forge_registry
    files = [forge_registry.runs_registry(repo)] if repo is not None else sorted(forge_registry.registry_dir().glob('*.txt'))
    found, seen = [], set()
    for path in files:
        try:
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#') or not os.path.isabs(line) or not os.path.isdir(line):
                continue
            real = os.path.realpath(line)
            if real in seen:
                continue
            seen.add(real)
            named = _first_line(os.path.join(real, 'repo'))
            found.append((real, os.path.realpath(named) if named and os.path.isabs(named) else None))
    repo_real = os.path.realpath(repo) if repo is not None else None
    for entry in forge_locations.entries('run'):
        real = entry['path']
        if real in seen or not os.path.isdir(real) or (repo_real and not _matches(entry, repo_real)):
            continue
        seen.add(real)
        found.append((real, entry.get('checkout')))
    return found


def data_home_parts(home: Path, deadline=None) -> dict:
    """{path, bytes, parts: {dataset, blobs, archive, exports, legacy, storage, other}} of forge's data home."""
    home = Path(home)
    parts = dict(dataset=0, blobs=0, archive=0, exports=0, legacy=0, storage=0, other=0)
    seen = _Seen()
    try:
        entries = list(os.scandir(home))
    except OSError:
        entries = []
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if entry.name == 'dataset' and stat.S_ISDIR(info.st_mode):
            for inner in os.scandir(entry.path):
                try:
                    inner_info = inner.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(inner_info.st_mode):
                    parts['blobs' if inner.name == 'blobs' else 'dataset'] += tree_usage(inner.path, seen, deadline)[0]
                elif seen.first(inner_info):
                    parts['dataset'] += usage(inner_info)
        elif stat.S_ISDIR(info.st_mode):
            key = entry.name if entry.name in parts else 'other'
            parts[key] += tree_usage(entry.path, seen, deadline)[0]
        elif seen.first(info):
            parts['other'] += usage(info)
    return dict(path=str(home), bytes=sum(parts.values()), parts=parts)


def report(repo=None, home=None, budget_s: float = 600.0, progress=None) -> dict:
    """What forge takes on disk, for one repository (a path) or, None, every one (`forge du`, forge-ui's Disk screen).

    {repo, computed, complete, runs: [{path, run_id, repo, updated, total, snapshots, snapshot_count, logs, gz,
     compressible, strays, fractal, other, reclaimable, complete}],
     strays: [{path, bytes, kind, run}], worktrees: [{repo, run_id, path, bytes, complete}],
     data_home: {path, bytes, parts} | None, totals: {runs, worktrees, data_home, reclaimable, snapshots, logs}}
    """
    _scripts()
    import forge_locations
    import forge_worktrees
    deadline = time.monotonic() + budget_s
    found = run_dirs(repo)
    runs, strays, worktrees, seen_roots = [], [], [], set()
    complete = True
    for index, (path, named) in enumerate(found):
        if progress:
            progress(index, len(found), path)
        scanned = scan(path, deadline=deadline)
        complete = complete and scanned['complete']
        run_id = _first_line(os.path.join(path, 'run_id')) or os.path.basename(path)
        try:
            updated = max((e.stat(follow_symlinks=False).st_mtime for e in os.scandir(path)), default=None)
        except OSError:
            updated = None
        runs.append(dict(path=path, run_id=run_id, repo=named, updated=updated, total=scanned['total'],
                         snapshots=scanned['snapshots']['bytes'], snapshot_count=scanned['snapshots']['count'],
                         logs=scanned['logs']['bytes'], gz=scanned['logs']['gz_bytes'],
                         compressible=scanned['logs']['compressible_bytes'], fractal=scanned['fractal'],
                         strays=sum(s['bytes'] for s in scanned['strays']), other=scanned['other'],
                         reclaimable=reclaimable(scanned), complete=scanned['complete']))
        strays.extend(dict(s, run=path) for s in scanned['strays'])
        if named and forge_worktrees.RUN_ID.fullmatch(run_id):
            root = forge_worktrees.wt_root(named, run_id)
            if root not in seen_roots and os.path.isdir(root) and not os.path.islink(root):
                seen_roots.add(root)
                size, done = tree_usage(root, deadline=deadline)
                complete = complete and done
                worktrees.append(dict(repo=named, run_id=run_id, path=root, bytes=size, complete=done))
    # worktree folders the location list knows that no registered run led to (a run dir deleted, a checkout gone)
    repo_real = os.path.realpath(repo) if repo is not None else None
    for entry in forge_locations.entries('worktree'):
        root = os.path.dirname(entry['path'])
        if os.path.basename(os.path.dirname(root)) != '.forge-worktrees' or root in seen_roots or not os.path.isdir(root) \
                or os.path.islink(root) or (repo_real and not _matches(entry, repo_real)):
            continue
        seen_roots.add(root)
        size, done = tree_usage(root, deadline=deadline)
        complete = complete and done
        worktrees.append(dict(repo=entry.get('checkout'), run_id=entry.get('run_id') or os.path.basename(root), path=root,
                              bytes=size, complete=done))
    home_parts = data_home_parts(home, deadline) if home is not None else None
    runs.sort(key=lambda r: -r['total'])
    worktrees.sort(key=lambda w: -w['bytes'])
    totals = dict(runs=sum(r['total'] for r in runs), worktrees=sum(w['bytes'] for w in worktrees),
                  data_home=home_parts['bytes'] if home_parts else 0, reclaimable=sum(r['reclaimable'] for r in runs),
                  snapshots=sum(r['snapshots'] for r in runs), logs=sum(r['logs'] + r['gz'] for r in runs),
                  strays=sum(s['bytes'] for s in strays))
    return dict(repo=str(repo) if repo is not None else None, computed=time.time(), complete=complete, runs=runs,
                strays=sorted(strays, key=lambda s: -s['bytes']), worktrees=worktrees, data_home=home_parts, totals=totals)


def footprints_of(result: dict) -> dict:
    """{repo: {bytes, runs, worktrees, count}} from a report, for save_footprints."""
    out: dict = {}
    for run in result['runs']:
        if run['repo']:
            entry = out.setdefault(run['repo'], dict(bytes=0, runs=0, worktrees=0, count=0))
            entry['runs'] += run['total']
            entry['bytes'] += run['total']
            entry['count'] += 1
    for wt in result['worktrees']:
        entry = out.setdefault(wt['repo'], dict(bytes=0, runs=0, worktrees=0, count=0))
        entry['worktrees'] += wt['bytes']
        entry['bytes'] += wt['bytes']
    return out
