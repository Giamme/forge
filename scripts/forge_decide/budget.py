"""Spend caps for decision-model requests, in tokens and estimated cost (GitHub #36).

Two counters, each a JSON file {"tokens": int, "usd": float}: one per run
(<run_dir>/decide.budget.json) and one per day for the user
(<state_dir>/spend/<YYYY-MM-DD>.json, local date; a new day is a new file and old files are
left alone). The transports call check() before a request and charge() after it; a cap that
has been reached turns every remaining call into a skip with reason 'budget'.

Fail-open throughout: check() and charge() never raise, a counter that cannot be read or
parsed counts as zero, and a lock or write failure skips the charge. Several processes of one
run may charge at once, so each read-modify-write holds flock(LOCK_EX) on a sibling .lock
file and the write is a temp file plus os.replace. Standard library only.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import time

from . import state_dir

RUN_FILE = 'decide.budget.json'
RUN_DIR_ENV = 'FORGE_DECIDE_RUN_DIR'
SKIP = 'budget'
USD_SLACK = 1e-9  # relative: float drift in summed charges (ten $0.10 ones) must not miss a cap


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return value if math.isfinite(value) and value > 0 else 0
    except OverflowError:  # an integer too big for a float is corrupt, not a huge spend
        return 0


def usage_tokens(usage) -> int:
    """Tokens a response used: input+output, else prompt+completion, else total."""
    if not isinstance(usage, dict):
        return 0
    for keys in (('input_tokens', 'output_tokens'), ('prompt_tokens', 'completion_tokens')):
        if any(k in usage for k in keys):
            return int(sum(_number(usage.get(k)) for k in keys))
    return int(_number(usage.get('total_tokens')))


def cost(provider, usage) -> float:
    """Estimated USD for one response; a local provider, or one with no price, costs 0."""
    if not isinstance(provider, dict) or provider.get('local'):
        return 0.0
    price = _number(provider.get('price_usd_per_mtok'))
    return usage_tokens(usage) / 1e6 * price if price else 0.0


def _run_path(run_dir) -> Path | None:
    run_dir = run_dir or os.environ.get(RUN_DIR_ENV)
    return Path(run_dir) / RUN_FILE if run_dir else None


def _day(now) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(now))


def _daily_path(now) -> Path:
    return state_dir() / 'spend' / f'{_day(now)}.json'


def _read(path: Path | None) -> dict:
    zero = {'tokens': 0, 'usd': 0.0}
    if path is None:
        return zero
    try:
        data = json.loads(path.read_text())
        return {'tokens': int(_number(data['tokens'])), 'usd': float(_number(data['usd']))}
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError):
        return zero


def _add(path: Path, tokens: int, usd: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(f'{path}.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            total = _read(path)
            total['tokens'] += tokens
            total['usd'] += usd
            tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
            try:
                tmp.write_text(json.dumps(total))
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
    except Exception:  # fail-open: a charge that cannot be recorded is dropped
        pass


def _reached(counter: dict, tokens, usd) -> bool:
    return ((tokens is not None and counter['tokens'] >= tokens)
            or (usd is not None and counter['usd'] >= usd * (1 - USD_SLACK)))


def check(caps, *, run_dir=None, now=None) -> str | None:
    """'budget' once any set cap (run or day, tokens or usd) has been reached, else None."""
    try:
        now = time.time() if now is None else now
        caps = caps or {}
        if _reached(_read(_run_path(run_dir)), caps.get('run_tokens'), caps.get('run_usd')):
            return SKIP
        if _reached(_read(_daily_path(now)), caps.get('daily_tokens'), caps.get('daily_usd')):
            return SKIP
    except Exception:
        pass
    return None


def charge(provider, usage, *, run_dir=None, now=None) -> None:
    """Add one response's tokens and estimated cost to the run counter and today's."""
    try:
        now = time.time() if now is None else now
        tokens, usd = usage_tokens(usage), cost(provider, usage)
        if not tokens and not usd:
            return
        run = _run_path(run_dir)
        if run is not None:
            _add(run, tokens, usd)
        _add(_daily_path(now), tokens, usd)
    except Exception:
        pass


def spend(*, run_dir=None, now=None) -> dict:
    """{'run': counter | None, 'today': counter, 'date': 'YYYY-MM-DD'}; run is None with no run dir."""
    now = time.time() if now is None else now
    run = _run_path(run_dir)
    return {'run': _read(run) if run else None, 'today': _read(_daily_path(now)),
            'date': _day(now)}
