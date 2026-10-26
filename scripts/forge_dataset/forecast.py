"""`forge dataset forecast <plan>`: what a plan is likely to cost, from forge's own history.

For each task of a plan, the tasks in the dataset that ran at the same tier on the same
model (the dwarf spec resolved through the registry; the effort too when the spec names one)
say what to expect: the median and p90 of tokens (input + output, dwarf and QA, over every
attempt) and of wall time (the same dispatches' durations), and the first-attempt PASS rate
with a 95% Wilson interval. Below MIN_HISTORY matching tasks a row says "not enough history"
instead of numbers a handful of tasks would make up, and a task whose rate is under
FLAG_BELOW is flagged as likely to need a retry.

Advisory only: nothing that routes a task, builds a prompt or gates a run reads it. The
rates are observational (who ran what was not randomized unless `--explore` drew it), so a
low rate can be the tasks a model was given rather than the model.

`forge-parallel.sh run` prints a short version before dispatching (`--no-forecast` or
FORGE_FORECAST=off skip it). There it runs under a time budget (FORGE_FORECAST_BUDGET_S,
default 3s), is silent when there is no history, never changes the run's exit code, and
records what it predicted: `<plan>/forecast.json` and a `forecast` dataset event keyed by
the run, so a later report can check the forecast against what happened.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import SKILL, connect, dataset_dir, record
from .export import Runs, Store
from .routing import tasks as routing_tasks, wilson

MIN_HISTORY = 5
FLAG_BELOW = 0.5
BUDGET_S = 3.0
OFF = {'0', 'off', 'false', 'no'}


# --- the registry, as forge-route.py reads it (that module's name is not importable) ---

def registry_models() -> dict:
    """{alias: {harness: model id}}; the first row of an alias is its default."""
    out = {}
    path = Path(os.environ.get('FORGE_REGISTRY') or str(SKILL / 'registry.tsv'))
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        if len(cells) >= 3:
            out.setdefault(cells[0], {}).setdefault(cells[1], cells[2])
    return out


def resolve(spec, registry: dict) -> tuple:
    """(model id, effort or None) a dwarf spec such as `sol`, `sol:high` or `sol:high:codex` runs."""
    parts = str(spec or '').split(':')
    alias = parts[0]
    effort = parts[1] if len(parts) > 1 and parts[1] else None
    harness = parts[2] if len(parts) > 2 else None
    rows = registry.get(alias)
    if not rows:
        return alias or None, effort
    if harness and harness in rows:
        return rows[harness], effort
    return next(iter(rows.values())), effort


# --- reading the plan ---

def _tsv(path: Path) -> list:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    cols = ('id', 'deps', 'difficulty', 'files', 'dwarf', 'qa', 'title')
    return [dict(zip(cols, line.split('\t'))) for line in lines if line.strip() and not line.startswith('#')]


def plan_tasks(plan: Path) -> tuple:
    """([task dicts to forecast], [ids already merged]) from tasks.tsv (declared tier when kept)."""
    declared = {r['id']: r for r in _tsv(plan / 'tasks.declared.tsv')}
    out, merged = [], []
    for row in _tsv(plan / 'tasks.tsv'):
        tid = row.get('id')
        if not tid:
            continue
        if (plan / 'tasks' / tid / 'merged').exists():
            merged.append(tid)
            continue
        tier = (declared.get(tid) or {}).get('difficulty') or row.get('difficulty') or None
        out.append(dict(task=tid, tier=tier, spec=row.get('dwarf') or None))
    return out, merged


def waves(plan: Path) -> dict:
    out = {}
    try:
        lines = (plan / 'waves.tsv').read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        cells = line.split('\t')
        if len(cells) >= 2 and cells[1].strip():
            out[cells[1].strip()] = cells[0].strip()
    return out


def run_uid(plan: Path):
    try:
        value = (plan / 'dataset.uid').read_text().strip()
    except OSError:
        return None
    return value or None


# --- history ---

def _cost(runs: Runs, run, task) -> tuple:
    """(tokens, seconds) a task's dwarf and QA dispatches took over every attempt; None when unrecorded."""
    tokens = seconds = None
    for role in ('dwarf', 'qa'):
        for e in runs.dispatches.get((run, task, role), []):
            t = e['d'].get('tokens') or {}
            used = [t.get(k) for k in ('input', 'output') if isinstance(t.get(k), (int, float))]
            if used:
                tokens = (tokens or 0) + sum(used)
            d = e['d'].get('duration_s')
            if isinstance(d, (int, float)):
                seconds = (seconds or 0) + d
    return tokens, seconds


def history(runs: Runs, registry: dict, *, repo=None, exclude_run=None) -> list:
    """Every task that ran, with the (tier, model, effort) it ran at and what it cost."""
    out = []
    for r in routing_tasks(runs, repo=repo):
        if not r['attempts'] or r['run'] == exclude_run:
            continue
        model, effort = r.get('model_id'), r.get('effort')
        if not model:
            model, effort = resolve(r['ran'], registry)
        if not model:
            continue
        tokens, seconds = _cost(runs, r['run'], r['task'])
        out.append(dict(tier=r['tier'], model=model, effort=effort, first_pass=r['first_pass'],
                        tokens=tokens, seconds=seconds))
    return out


def nearest_rank(values: list, q: float):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def _spread(values: list) -> dict:
    if len(values) < MIN_HISTORY:
        return dict(n=len(values), median=None, p90=None)
    return dict(n=len(values), median=statistics.median(values), p90=nearest_rank(values, 0.9))


def forecast_task(task: dict, past: list, registry: dict) -> dict:
    model, effort = resolve(task['spec'], registry) if task['spec'] else (None, None)
    same = [h for h in past if model and h['tier'] == task['tier'] and h['model'] == model
            and (effort is None or h['effort'] == effort)]
    outcomes = [h['first_pass'] for h in same if h['first_pass'] is not None]
    k, n = sum(1 for x in outcomes if x), len(outcomes)
    enough = n >= MIN_HISTORY
    low, high = wilson(k, n) if enough else (None, None)
    rate = k / n if enough else None
    return dict(task=task['task'], tier=task['tier'], spec=task['spec'], model=model, effort=effort,
                history=len(same), enough=enough,
                first_pass=dict(k=k, n=n, rate=rate, ci=[low, high] if enough else None),
                tokens=_spread([h['tokens'] for h in same if h['tokens'] is not None]),
                time_s=_spread([h['seconds'] for h in same if h['seconds'] is not None]),
                flag=bool(enough and rate < FLAG_BELOW),
                note=None if enough else 'not enough history (%d/%d)' % (n, MIN_HISTORY))


def totals(rows: list, wave_of: dict) -> dict:
    tok = [r for r in rows if r['tokens']['median'] is not None]
    sec = [r for r in rows if r['time_s']['median'] is not None]
    rated = [r for r in rows if r['enough']]
    by_wave = defaultdict(float)
    for r in sec:
        w = wave_of.get(r['task'], r['task'])
        by_wave[w] = max(by_wave[w], r['time_s']['median'])
    return dict(
        tasks=len(rows), forecast=len(rated), flagged=sum(1 for r in rows if r['flag']),
        tokens=dict(tasks=len(tok), median=sum(r['tokens']['median'] for r in tok) if tok else None,
                    p90=sum(r['tokens']['p90'] for r in tok) if tok else None),
        time_s=dict(tasks=len(sec), median=sum(r['time_s']['median'] for r in sec) if sec else None,
                    p90=sum(r['time_s']['p90'] for r in sec) if sec else None,
                    by_waves=sum(by_wave.values()) if (sec and wave_of) else None),
        first_pass_expected=sum(r['first_pass']['rate'] for r in rated) if rated else None)


def build(plan: Path, *, repo=None) -> dict | None:
    """The forecast for a plan, or None when there is no history at all (no store, no tasks)."""
    # Reading must not create a store: a fresh machine gets nothing, not an empty database.
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return None
    db = connect()
    try:
        runs = Runs(Store(db))
    finally:
        db.close()
    registry = registry_models()
    uid = run_uid(plan)
    past = history(runs, registry, repo=repo, exclude_run=uid)
    if not past:
        return None
    todo, merged = plan_tasks(plan)
    rows = [forecast_task(t, past, registry) for t in todo]
    return dict(plan=str(plan), run=uid, at=time.time(), min_history=MIN_HISTORY, flag_below=FLAG_BELOW,
                history_tasks=len(past), tasks=rows, merged=merged, total=totals(rows, waves(plan)))


# --- output ---

def _tokens(v):
    if v is None:
        return '?'
    return '%.1fM' % (v / 1e6) if v >= 1e6 else ('%.0fk' % (v / 1e3) if v >= 1e3 else '%d' % v)


def _time(v):
    if v is None:
        return '?'
    v = int(round(v))
    return '%dh%02dm' % (v // 3600, v % 3600 // 60) if v >= 3600 else ('%dm%02ds' % (v // 60, v % 60) if v >= 60 else '%ds' % v)


def _pct(v):
    return '%.0f%%' % (v * 100)


def _names(rows: list, most: int = 8) -> str:
    names = [r['task'] for r in rows]
    return ', '.join(names[:most]) + (', ...' if len(names) > most else '')


def lines(fc: dict, brief: bool = False) -> list:
    """The forecast as text. brief (the block `run` prints) gives tasks without enough history
    one line together instead of one row each, and no table at all when no task has enough."""
    thin = [r for r in fc['tasks'] if not r['enough']]
    if brief and len(thin) == len(fc['tasks']):
        return ['no forecast yet: none of the %d task(s) has %d past tasks at its tier and model '
                '(forge dataset forecast %s shows each count)' % (len(thin), MIN_HISTORY, fc['plan'])]
    out = ['forecast from %d past task(s) at the same tier and model (advisory; routing never reads it)'
           % fc['history_tasks']]
    for r in fc['tasks']:
        where = '%s %s' % (r['tier'] or '?', r['spec'] or '?')
        if not r['enough']:
            if not brief:
                out.append('  %-14s %-22s %s' % (r['task'], where, r['note']))
            continue
        fp = r['first_pass']
        t, s = r['tokens'], r['time_s']
        out.append('  %-14s %-22s first try %s [%s-%s] (%d/%d)  tokens %s (p90 %s)  time %s (p90 %s)%s' % (
            r['task'], where, _pct(fp['rate']), _pct(fp['ci'][0]), _pct(fp['ci'][1]), fp['k'], fp['n'],
            _tokens(t['median']), _tokens(t['p90']), _time(s['median']), _time(s['p90']),
            '  <- likely to need a retry' if r['flag'] else ''))
    if brief and thin:
        out.append('  %d task(s) without enough history yet (under %d past tasks): %s'
                   % (len(thin), MIN_HISTORY, _names(thin)))
    if fc['merged']:
        out.append('  %d task(s) already merged, not forecast' % len(fc['merged']))
    tot = fc['total']
    if tot['forecast'] or tot['tokens']['tasks']:
        tk, tm = tot['tokens'], tot['time_s']
        summary = '  total for %d/%d task(s) with history: tokens %s (p90 %s), time %s (p90 %s) summed' % (
            max(tk['tasks'], tm['tasks']), tot['tasks'], _tokens(tk['median']), _tokens(tk['p90']),
            _time(tm['median']), _time(tm['p90']))
        if tm['by_waves'] is not None:
            summary += ', about %s by waves (ignores --max-parallel)' % _time(tm['by_waves'])
        out.append(summary)
        if tot['first_pass_expected'] is not None:
            out.append('  expected first-try passes: %.1f of %d forecast task(s)%s' % (
                tot['first_pass_expected'], tot['forecast'],
                ', %d flagged under %s' % (tot['flagged'], _pct(fc['flag_below'])) if tot['flagged'] else ''))
    return out


def save(plan: Path, fc: dict) -> None:
    """Keep what was predicted, so it can be checked against what happened. Fail-open."""
    body = {k: v for k, v in fc.items() if k != 'at'}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    try:
        tmp = plan / ('.forecast.json.%d' % os.getpid())
        tmp.write_text(json.dumps(fc, indent=2, sort_keys=True) + '\n')
        tmp.replace(plan / 'forecast.json')
    except OSError:
        pass
    if not fc.get('run'):   # record() itself does nothing under FORGE_DATASET=off
        return
    from .harvest import first_line, locate
    run_id = locate(plan).get('run_id') or plan.name
    repo = first_line(plan / 'repo') or None
    record([dict(rid='forecast:%s:%s' % (fc['run'], digest), kind='forecast', ts=fc['at'], src='live',
                 run=fc['run'], run_id=run_id, mode='parallel', task=None, data=fc)], repo=repo, where='forecast')


def advise(plan: Path, shown: list) -> None:
    """The run's advisory block: print it, record it. Never raises."""
    try:
        fc = build(plan)
    except Exception:
        return
    if not fc or not fc['tasks']:
        return
    for line in lines(fc, brief=True):
        print(line)
    sys.stdout.flush()
    shown.append(True)
    try:
        save(plan, fc)
    except Exception:
        pass


def budget() -> float:
    try:
        value = float(os.environ.get('FORGE_FORECAST_BUDGET_S') or BUDGET_S)
    except ValueError:
        return BUDGET_S
    return value if value > 0 else BUDGET_S


def main(args) -> int:
    plan = Path(args.plan).resolve()
    if args.run:
        # Called by forge-parallel.sh run: silent on anything unexpected, exit 0, and never longer than the budget.
        if os.environ.get('FORGE_FORECAST', 'on').strip().lower() in OFF or not (plan / 'tasks.tsv').is_file():
            return 0
        shown = []
        worker = threading.Thread(target=advise, args=(plan, shown), daemon=True)
        worker.start()
        worker.join(budget())
        if worker.is_alive():
            # Past the budget the run goes on without it. Once the block is out, only the
            # recording is cut short, which is not worth a line.
            if not shown:
                print('forecast skipped: reading the history took over %gs' % budget())
                sys.stdout.flush()
            os._exit(0)
        return 0
    if not (plan / 'tasks.tsv').is_file():
        print('forge dataset forecast: no tasks.tsv in %s' % plan, file=sys.stderr)
        return 2
    fc = build(plan, repo=os.path.realpath(args.repo) if args.repo else None)
    if args.json:
        print(json.dumps(fc, indent=2, sort_keys=True))
        return 0
    if not fc:
        print('no task history in the dataset yet, so nothing to forecast from')
        return 0
    for line in lines(fc):
        print(line)
    print('  tokens are input + output of the dwarf and QA dispatches over every attempt; time is their summed duration')
    print('  rates are observational: a low one can be the tasks a model was given rather than the model')
    return 0
