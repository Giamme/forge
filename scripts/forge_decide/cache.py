"""Request cache for decision-model calls (GitHub #37): a question already answered in a run is
never paid for twice.

One file per request in <run>/decide.cache, named by sha256 of the request key (the sha256 of
the canonical body, which carries state, model and questions) plus the provider's name and
url, so a body sent to another provider or url is a miss. FORGE_DECIDE_RUN_DIR beats the
caller's run_dir, as in budget.py, so every task of one plan shares one cache.
FORGE_DECIDE_CACHE=off (0, no, false) turns it off.

Fail-open: get() treats a missing, unreadable or malformed file as a miss, put() ignores a
failed write. Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time

DIR_NAME = 'decide.cache'
RUN_DIR_ENV = 'FORGE_DECIDE_RUN_DIR'
SWITCH_ENV = 'FORGE_DECIDE_CACHE'


def cache_dir(run_dir) -> Path | None:
    if os.environ.get(SWITCH_ENV, '').strip().lower() in ('off', '0', 'no', 'false'):
        return None
    run_dir = os.environ.get(RUN_DIR_ENV) or run_dir
    return Path(run_dir) / DIR_NAME if run_dir else None


def entry_name(key, spec) -> str:
    parts = (str(key), str(spec.get('name')), str(spec.get('url')))
    return hashlib.sha256('\0'.join(parts).encode()).hexdigest()


def get(run_dir, key, spec) -> dict | None:
    directory = cache_dir(run_dir)
    if directory is None:
        return None
    try:
        entry = json.loads((directory / (entry_name(key, spec) + '.json')).read_text())
    except (OSError, ValueError, RecursionError):
        return None
    return entry if isinstance(entry, dict) and isinstance(entry.get('answers'), dict) else None


def put(run_dir, key, spec, site, model, answers, usage) -> None:
    directory = cache_dir(run_dir)
    if directory is None:
        return
    entry = dict(site=site, key=key, provider=spec.get('name'), model=model, answers=answers, usage=usage,
                 at=time.time())
    temp = directory / f'.{os.getpid()}-{os.urandom(4).hex()}.tmp'
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(json.dumps(entry))
        os.replace(temp, directory / (entry_name(key, spec) + '.json'))
    except (OSError, TypeError, ValueError):
        try:
            temp.unlink()
        except OSError:
            pass
