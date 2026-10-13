"""Forge's run dataset: the inputs and outcomes of every run, kept after run dirs are gone.

Run and plan directories hold nearly everything a decision model could learn from --
prompts, diffs, the resolved model of every dispatch, verdicts, tokens, Jev judgments --
but they live in /tmp and scratchpads and get deleted. The only durable record,
<repo>/.forge/ledger.tsv, keeps spec strings and verdicts and none of the inputs. This
package keeps a global, append-only copy of each run's raw events outside every repo:

    ${FORGE_DATA_HOME:-${XDG_DATA_HOME:-~/.local/share}/forge}/dataset/
        dataset.sqlite      events (rid-keyed, INSERT OR IGNORE) and repos
        blobs/<aa>/<sha>.gz capped, gzipped text, addressed by the sha of the STORED bytes
        errors.log          capture failures

What it stores is raw state and outcomes, never training examples: an exporter derives
(state, question, label) rows per decision later, so a question nobody has thought of yet
can still be answered from old runs.

Capture is fail-open in the same way Jev is. Every entry point swallows its own errors
into errors.log, never prints, never raises, and bounds its wait on the database with the
SQLite busy timeout. It uses no signals, because Fractal captures from worker threads.
FORGE_DATASET=off turns it off.

Nothing that builds a prompt may ever read this store: a reviewer that could see earlier
runs' verdicts would no longer be independent.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import time
import traceback
import uuid
from pathlib import Path

SCHEMA = 1
SCRIPTS = Path(__file__).resolve().parent.parent
SKILL = SCRIPTS.parent
BUSY_TIMEOUT_S = 1.0
ERRORS_MAX = 1 << 20
OFF = ('off', '0', 'no', 'false')
DRY = False   # import --dry-run: build events and blob references, write nothing

# (head, tail) bytes kept per kind of content. A cut keeps both ends because the end is
# often what matters: a QA prompt ends with the diff, a log ends with the failure.
CAPS = {
    'prompt': (256 << 10, 768 << 10),
    'diff': (1536 << 10, 512 << 10),
    'message': (64 << 10, 192 << 10),
    'log': (16 << 10, 256 << 10),
    'text': (64 << 10, 192 << 10),
    'request': (768 << 10, 256 << 10),
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    rid TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    ts REAL NOT NULL,
    src TEXT NOT NULL,
    repo TEXT, run TEXT, run_id TEXT, mode TEXT, task TEXT, attempt INTEGER,
    parent TEXT, inv TEXT, forge_version TEXT, registry_sha TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events(run, task);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS repos (
    repo TEXT PRIMARY KEY, path TEXT, origin TEXT, first_seen REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

EVENT_COLUMNS = ('rid', 'kind', 'ts', 'src', 'repo', 'run', 'run_id', 'mode', 'task',
                 'attempt', 'parent', 'inv', 'forge_version', 'registry_sha', 'data')


def enabled() -> bool:
    return os.environ.get('FORGE_DATASET', 'on').strip().lower() not in OFF


def data_home() -> Path:
    explicit = os.environ.get('FORGE_DATA_HOME')
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local' / 'share')
    return Path(base).expanduser() / 'forge'


def dataset_dir() -> Path:
    return data_home() / 'dataset'


def config_runs_dir() -> Path:
    """The Jev runs registry (forge_jev.cli.runs_registry): plan dirs per repo."""
    return Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))).expanduser() / 'forge' / 'runs'


def log_error(where: str, error: BaseException | str) -> None:
    """Record a capture failure. Never raises: this is the last line of defence."""
    try:
        directory = dataset_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / 'errors.log'
        if path.exists() and path.stat().st_size > ERRORS_MAX:
            path.replace(directory / 'errors.log.1')
        if isinstance(error, BaseException):
            detail = ''.join(traceback.format_exception_only(type(error), error)).strip()
            frames = traceback.extract_tb(error.__traceback__)
            if frames:
                last = frames[-1]
                detail += ' (%s:%s)' % (Path(last.filename).name, last.lineno)
        else:
            detail = str(error)
        with path.open('a') as handle:
            handle.write('%s\t%s\t%s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'), where,
                                           detail.replace('\n', ' ')[:2000]))
        path.chmod(0o600)
    except Exception:
        pass


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def location_refused(repo: str | Path | None) -> str | None:
    """Why the store must not be written for this repo, or None.

    A store inside the repo would dirty the user's tree, and `integrate` refuses to run
    on a dirty tree. Inside a forge worktree it would land in a task's diff.
    """
    store = dataset_dir()
    if '.forge-worktrees' in store.resolve().parts:
        return 'the dataset dir is inside a forge worktree'
    if repo and inside(store, Path(repo)):
        return 'the dataset dir is inside the repository %s' % repo
    return None


def connect() -> sqlite3.Connection:
    directory = dataset_dir()
    _private_dir(directory)
    path = directory / 'dataset.sqlite'
    new = not path.exists()
    db = sqlite3.connect(str(path), timeout=BUSY_TIMEOUT_S, isolation_level=None)
    if new:
        try:
            path.chmod(0o600)
        except OSError:
            pass
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA),))
    return db


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cut(data: bytes, kind: str) -> tuple[bytes, list | None]:
    head, tail = CAPS.get(kind, CAPS['text'])
    if len(data) <= head + tail:
        return data, None
    marker = b'\n[... forge-dataset: %d bytes cut ...]\n' % (len(data) - head - tail)
    kept = data[:head] + marker + (data[-tail:] if tail else b'')
    return kept, [head, len(data) - tail]


def put_blob(data: bytes | str, kind: str = 'text') -> dict:
    """Store content once, capped; return the reference an event carries.

    The address is the sha of the stored bytes, and the original's sha and size travel
    in the reference: two different originals can share a cut prefix, and the same
    original cut under two caps must not collide.
    """
    if isinstance(data, str):
        data = data.encode('utf-8', 'replace')
    stored, span = cut(data, kind)
    stored_sha = sha256(stored)
    directory = dataset_dir() / 'blobs' / stored_sha[:2]
    path = directory / (stored_sha + '.gz')
    if not DRY and not path.exists():
        _private_dir(dataset_dir() / 'blobs')
        _private_dir(directory)
        temporary = directory / ('.%s.%s' % (stored_sha, uuid.uuid4().hex))
        with gzip.open(temporary, 'wb', compresslevel=6) as handle:
            handle.write(stored)
        temporary.chmod(0o600)
        temporary.replace(path)
    return dict(sha=stored_sha, original_sha=sha256(data) if span else stored_sha,
                n=len(data), kept=len(stored), cut=span)


def put_file(path: str | Path, kind: str = 'text') -> dict | None:
    path = Path(path)
    try:
        if not path.is_file():
            return None
        return put_blob(path.read_bytes(), kind)
    except OSError:
        return None


def read_blob(stored_sha: str) -> bytes:
    with gzip.open(dataset_dir() / 'blobs' / stored_sha[:2] / (stored_sha + '.gz'), 'rb') as handle:
        return handle.read()


_PROVENANCE: dict | None = None


def provenance() -> dict:
    """The forge version and registry sha, so prompt and contract drift is visible later."""
    global _PROVENANCE
    if _PROVENANCE is None:
        version = None
        try:
            version = (SKILL / 'VERSION').read_text().strip() or None
        except OSError:
            pass
        registry = os.environ.get('FORGE_REGISTRY') or str(SKILL / 'registry.tsv')
        try:
            registry_sha = sha256(Path(registry).read_bytes())[:12]
        except OSError:
            registry_sha = None
        _PROVENANCE = dict(forge_version=version, registry_sha=registry_sha)
    return _PROVENANCE


def repo_id(path: str | Path) -> str:
    """Same scheme as the Jev runs registry, so the two join on one key."""
    return sha256(str(Path(path).resolve()).encode())[:16]


def git_origin(repo: str | Path) -> str | None:
    """The origin URL, read from .git/config without running git."""
    try:
        config = Path(repo) / '.git' / 'config'
        if not config.is_file():
            return None
        section = None
        for line in config.read_text(errors='replace').splitlines():
            stripped = line.strip()
            if stripped.startswith('['):
                section = stripped
            elif section == '[remote "origin"]' and stripped.startswith('url'):
                return stripped.split('=', 1)[1].strip()
    except OSError:
        return None
    return None


def register_repo(db: sqlite3.Connection, path: str | Path | None) -> str | None:
    if not path:
        return None
    key = repo_id(path)
    now = time.time()
    db.execute('INSERT OR IGNORE INTO repos(repo, path, origin, first_seen, last_seen) '
               'VALUES (?, ?, ?, ?, ?)', (key, str(Path(path).resolve()), git_origin(path), now, now))
    db.execute('UPDATE repos SET last_seen = ? WHERE repo = ?', (now, key))
    return key


def insert(db: sqlite3.Connection, event: dict) -> bool:
    """Append one event. The first writer of a rid wins, so import and live never duplicate."""
    row = dict(event)
    row.update({k: v for k, v in provenance().items() if row.get(k) is None})
    row.setdefault('ts', time.time())
    row.setdefault('src', 'live')
    row['data'] = json.dumps(row.get('data') or {}, sort_keys=True, default=str)
    values = [row.get(column) for column in EVENT_COLUMNS]
    cursor = db.execute('INSERT OR IGNORE INTO events(%s) VALUES (%s)'
                        % (', '.join(EVENT_COLUMNS), ', '.join('?' * len(EVENT_COLUMNS))), values)
    return cursor.rowcount == 1


def record(events: list[dict], repo: str | Path | None = None, where: str = 'record') -> int:
    """Write events in one transaction. Fail-open: returns how many were new, 0 on error."""
    if not enabled() or not events:
        return 0
    try:
        # Refused silently: errors.log lives in the same refused place.
        if location_refused(repo):
            return 0
        db = connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            key = register_repo(db, repo) if repo else None
            added = 0
            for event in events:
                if key and not event.get('repo'):
                    event['repo'] = key
                added += insert(db, event)
            db.execute('COMMIT')
            return added
        except BaseException:
            try:
                db.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        finally:
            db.close()
    except Exception as error:
        log_error(where, error)
        return 0


def create_once(path: Path, text: str) -> str:
    """Write `text` to `path` unless it exists; return whichever content won.

    O_EXCL makes two concurrent first captures agree on one value instead of each
    replacing the other's.
    """
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        for _ in range(20):
            existing = path.read_text().strip()
            if existing:
                return existing
            time.sleep(0.01)
        return path.read_text().strip() or text
    with os.fdopen(fd, 'w') as handle:
        handle.write(text + '\n')
    return text
