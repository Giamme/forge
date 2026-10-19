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
import subprocess
import time
import traceback
import uuid
from pathlib import Path

import forge_registry

SCHEMA = 2
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
    data TEXT NOT NULL, code_sha TEXT, forge_rev TEXT
);
CREATE INDEX IF NOT EXISTS events_run ON events(run, task);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS repos (
    repo TEXT PRIMARY KEY, path TEXT, origin TEXT, first_seen REAL, last_seen REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

EVENT_COLUMNS = ('rid', 'kind', 'ts', 'src', 'repo', 'run', 'run_id', 'mode', 'task',
                 'attempt', 'parent', 'inv', 'forge_version', 'registry_sha', 'data', 'code_sha', 'forge_rev')
# Columns added after schema 1, in order. connect() adds any a store is missing.
ADDED_COLUMNS = (('code_sha', 'TEXT'), ('forge_rev', 'TEXT'))
PROVENANCE = ('forge_version', 'registry_sha', 'code_sha', 'forge_rev')


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
    """The runs registry (forge_registry.registry_dir): plan dirs per repo."""
    return forge_registry.registry_dir()


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
    # Switching to WAL needs the file to itself, and SQLite does not wait on the busy
    # timeout for it: two first writers to a brand-new store (threads of one plan's
    # predictions) would see "database is locked" and drop an event. Only a store that is
    # not WAL yet asks, and losing that race is harmless -- the winner makes it WAL, and
    # every write below waits on the busy timeout either way.
    try:
        if str(db.execute('PRAGMA journal_mode').fetchone()[0]).lower() != 'wal':
            db.execute('PRAGMA journal_mode=WAL')
    except sqlite3.OperationalError:
        pass
    db.execute('PRAGMA synchronous=NORMAL')
    db.executescript(SCHEMA_SQL)
    db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA),))
    migrate(db)
    return db


def _columns(db: sqlite3.Connection) -> set:
    return {row[1] for row in db.execute('PRAGMA table_info(events)')}


def migrate(db: sqlite3.Connection) -> None:
    """Bring a schema-1 store up to date, once, under the write lock.

    Concurrent first captures after an upgrade all try this: the column set is re-read
    inside BEGIN IMMEDIATE, so exactly one of them alters the table and the others see
    it done. ADD COLUMN never rewrites the table, so the lock is held for milliseconds.
    The schema number moves last, so an interrupted migration is simply redone.
    """
    if all(name in _columns(db) for name, _ in ADDED_COLUMNS):
        return
    db.execute('BEGIN IMMEDIATE')
    try:
        have = _columns(db)
        for name, kind in ADDED_COLUMNS:
            if name not in have:
                db.execute('ALTER TABLE events ADD COLUMN %s %s' % (name, kind))
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA),))
        db.execute('COMMIT')
    except BaseException:
        try:
            db.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise


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
    """Which forge produced an event, so prompt, contract and registry drift is visible later.

    forge_version is the release, which the dev checkout outlives by weeks of edits.
    code_sha is the content of every file that shapes a run (scripts, SKILL.md, the
    references the orchestrator follows, the effective registry), so two runs share it
    only if they ran the same prompts and contracts. forge_rev is the checkout's commit,
    a readable anchor for it; code_sha also covers uncommitted edits on top.
    """
    global _PROVENANCE
    if _PROVENANCE is None:
        version = None
        try:
            version = (SKILL / 'VERSION').read_text().strip() or None
        except OSError:
            pass
        registry = Path(os.environ.get('FORGE_REGISTRY') or str(SKILL / 'registry.tsv'))
        try:
            registry_bytes = registry.read_bytes()
        except OSError:
            registry_bytes = None
        _PROVENANCE = dict(forge_version=version,
                           registry_sha=sha256(registry_bytes)[:12] if registry_bytes is not None else None,
                           code_sha=code_sha(registry_bytes), forge_rev=forge_rev())
    return _PROVENANCE


CODE_FILES = ('SKILL.md', 'VERSION', 'forge')
CODE_TREES = ('scripts', 'references')


def code_sha(registry_bytes: bytes | None) -> str | None:
    try:
        digest = hashlib.sha256()
        paths = [SKILL / name for name in CODE_FILES]
        for tree in CODE_TREES:
            paths.extend(p for p in (SKILL / tree).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
        for path in sorted(paths):
            try:
                content = path.read_bytes()
            except OSError:
                continue
            digest.update(b'%s\0%s\n' % (path.relative_to(SKILL).as_posix().encode(), sha256(content).encode()))
        digest.update(b'registry\0%s\n' % (sha256(registry_bytes).encode() if registry_bytes is not None else b'-'))
        return digest.hexdigest()[:16]
    except Exception:
        return None


def forge_rev() -> str | None:
    """HEAD of the forge checkout, or None for a copied skill dir. Read-only: no index refresh."""
    if not (SKILL / '.git').exists():
        return None
    try:
        result = subprocess.run(['git', '-C', str(SKILL), 'rev-parse', 'HEAD'], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=2, env=dict(os.environ, GIT_OPTIONAL_LOCKS='0'))
    except (OSError, subprocess.SubprocessError):
        return None
    rev = result.stdout.decode('ascii', 'replace').strip()
    return rev if result.returncode == 0 and len(rev) >= 40 else None


def repo_id(path: str | Path) -> str:
    """Same scheme as the runs registry, so the two join on one key."""
    return forge_registry.repo_key(path)


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
    row.setdefault('src', 'live')
    if row['src'] != 'import':
        # An imported run was produced by whatever forge ran it, not by the importer, so its
        # provenance stays unknown. Rows imported before schema 2 carry the importer's
        # version: readers treat forge_version/registry_sha on src='import' as unknown.
        row.update({k: v for k, v in provenance().items() if row.get(k) is None})
    row.setdefault('ts', time.time())
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
        # Fingerprinting the code reads every shipped file and runs git once per process:
        # done under the write lock, it held every other writer past its busy timeout.
        provenance()
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
