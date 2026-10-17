"""`forge dataset routing`: how each model does per tier, and what the randomized picks say.

Two tables, kept apart because they answer different questions:

- **Observational**: every task's first attempt, by declared tier and the model and effort
  that ran it (resolved, so `sol` and `gpt-6.1-sol:medium:codex` are one row). First-pass rate
  (Wilson 95%), the share that passed review at some attempt (or that a human accepted),
  unfinished tasks, attempts per task and the implementer's runtime. Routing chose who ran what, so a model sent the hard tasks looks
  worse here than it is. Fractal runs are left out: their dispatches have their own identities
  and their runtime would be missing.
- **Randomized**: the `plan --explore` draws (forge-route.py), one row per (tier, default,
  alternative) pair, since round-robin pools make the pair differ between rows and a spec has
  no chance at all outside its own pairs. Each task counts in the arm it was *assigned*
  (intent to treat), whatever the approval gate or a retry did afterwards; tasks with no
  outcome are attrition, reported per arm. First-pass per arm is a Hajek (self-normalized
  inverse-propensity) mean, the difference has a run-cluster bootstrap interval, and Kish's
  effective n and the number of runs say how much the interval can be trusted.

The decision rule is fixed in advance and only advises: a pair is judged at the first of 30,
60 or 120 alternative assignments it reaches, on exactly the assignments up to that point.
The alternative is the stronger model for a low-tier pair (switch if the interval is above
0) and the cheaper one for medium and high (non-inferior if the interval's lower end is above
-0.10 and the alternative's runtime is lower). Nothing here changes a plan.
"""
from __future__ import annotations

import json
import math
import os
import random
import statistics
import time
from collections import defaultdict

from . import connect
from .export import Runs, Store, _attempts

WINDOWS = (30, 60, 120)
NONINFERIORITY = -0.10
BOOTSTRAP = 2000
MIN_ALTERNATIVES, MIN_RUNS = 10, 3


def wilson(k: int, n: int, z: float = 1.96):
    if not n:
        return None, None
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def decision_before(decisions, started):
    """The routing decision attempt 1 ran under: the latest recorded before it started.

    A re-plan after attempt 1 began (or one with a new draw) must not relabel what that
    attempt was assigned. Decisions repeat across plan revisions, so they are deduplicated.
    """
    unique = {}
    for d in decisions or ():
        unique[d.get('decision') or json.dumps(d, sort_keys=True)] = d
    ordered = sorted(unique.values(), key=lambda d: d.get('at') or 0)
    if not ordered:
        return None
    if started is None:
        return ordered[-1]
    before = [d for d in ordered if (d.get('at') or 0) <= started + 1]
    return before[-1] if before else None


def _started(runs: Runs, run, task, first) -> float | None:
    for e in runs.dispatches.get((run, task, 'dwarf'), []):
        if e['attempt'] == first['n'] and e['d'].get('wall_started'):
            return float(e['d']['wall_started'])
    return first['events'][0]['ts'] if first else None


def _runtime(runs: Runs, run, task) -> float | None:
    durations = [e['d'].get('duration_s') for e in runs.dispatches.get((run, task, 'dwarf'), [])]
    durations = [x for x in durations if isinstance(x, (int, float))]
    return sum(durations) if durations else None


def tasks(runs: Runs, *, repo=None, since=None) -> list:
    """One record per (run, task) that a plan held, with its routing decision and outcome."""
    out = []
    for run, revisions in runs.plans.items():
        if any((p['d'].get('fractal_selection') or {}).get('enabled') for p in revisions):
            continue
        rows, decisions = {}, defaultdict(list)
        for plan in revisions:
            for tid, row in (plan['d'].get('tasks') or {}).items():
                rows[tid] = row
                decisions[tid].extend(row.get('route') or [])
        for tid, row in rows.items():
            events = runs.attempts.get((run, tid), [])
            attempts = _attempts(runs, run, tid, events)
            first = attempts[0] if attempts else None
            if repo and not (events and runs.store.repo_paths.get(events[0]['repo']) == repo):
                continue
            if since and not (events and events[0]['ts'] >= since):
                continue
            started = _started(runs, run, tid, first) if first else None
            decision = decision_before(decisions.get(tid), started)
            resolved = runs.resolved(run, tid, 'dwarf', first['n']) if first else {}
            model = ':'.join(x for x in (resolved.get('model'), resolved.get('effort')) if x) or None
            out.append(dict(
                run=run, task=tid, tier=row.get('declared_difficulty') or row.get('difficulty'),
                ran=first['events'][0]['d'].get('dwarf') if first else None, model=model,
                first_pass=first['outcome'] if first else None,
                censored=(first['reason'] if first and first['outcome'] is None else (None if first else 'not_run')),
                passed=any(a['outcome'] for a in attempts) or (run, tid) in runs.accepted,
                attempts=len(attempts), runtime_s=_runtime(runs, run, tid), decision=decision))
    return out


def observational(records) -> list:
    cells = defaultdict(list)
    for r in records:
        if (r['model'] or r['ran']) and r['first_pass'] is not None:
            cells[(r['tier'], r['model'] or r['ran'])].append(r)
    rows = []
    for (tier, spec), group in sorted(cells.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        k = sum(1 for r in group if r['first_pass'])
        low, high = wilson(k, len(group))
        runtimes = [r['runtime_s'] for r in group if r['runtime_s'] is not None]
        rows.append(dict(tier=tier, spec=spec, tasks=len(group), first_pass=k / len(group), ci=[low, high],
                         passed=sum(1 for r in group if r['passed']) / len(group),
                         unfinished=sum(1 for r in group if not r['passed']),
                         attempts=statistics.mean(r['attempts'] for r in group),
                         runtime_min=statistics.median(runtimes) / 60 if runtimes else None))
    return rows


def _hajek(items):
    """(weighted first-pass, Kish effective n) over items with an outcome."""
    known = [(1 / i['propensity'], 1.0 if i['first_pass'] else 0.0) for i in items if i['first_pass'] is not None]
    total = sum(w for w, _ in known)
    if not total:
        return None, 0.0
    return sum(w * y for w, y in known) / total, total * total / sum(w * w for w, _ in known)


def _difference(items):
    default = [i for i in items if i['arm'] == 'default']
    alternative = [i for i in items if i['arm'] == 'alternative']
    a, _ = _hajek(alternative)
    d, _ = _hajek(default)
    return None if a is None or d is None else a - d


def _bootstrap(items, rng):
    by_run = defaultdict(list)
    for i in items:
        by_run[i['run']].append(i)
    runs = sorted(by_run, key=str)
    diffs = []
    for _ in range(BOOTSTRAP):
        sample = [x for run in (rng.choice(runs) for _ in runs) for x in by_run[run]]
        diff = _difference(sample)
        if diff is not None:
            diffs.append(diff)
    if len(diffs) < BOOTSTRAP // 2:
        return None
    diffs.sort()
    return [diffs[int(0.025 * len(diffs))], diffs[int(0.975 * len(diffs)) - 1]]


def randomized(records, seed: int = 7) -> list:
    pairs = defaultdict(list)
    old_draws = 0
    for r in records:
        d = r['decision']
        candidates = (d or {}).get('candidates') or []
        if not d:
            continue
        if len(candidates) != 2 or not d.get('propensity'):
            old_draws += 1                # an old all-specs draw has no fixed pair to compare in
            continue
        alternative = next(c for c in candidates if c != d.get('default'))
        pairs[(d.get('tier'), d.get('default'), alternative)].append(dict(
            r, arm='alternative' if d.get('chosen') == alternative else 'default',
            propensity=float(d['propensity']), at=d.get('at') or 0,
            followed=r['ran'] == d.get('chosen') if r['ran'] else None))
    rows = []
    rng = random.Random(seed)
    if old_draws:
        rows.append(dict(note='%d older exploratory draw(s) picked among every spec of their plan; they have no '
                              'fixed pair to compare in and are left out' % old_draws))
    for (tier, default, alternative), items in sorted(pairs.items(), key=lambda kv: tuple(map(str, kv[0]))):
        items.sort(key=lambda i: i['at'])
        n_alt = sum(1 for i in items if i['arm'] == 'alternative')
        window = max((w for w in WINDOWS if n_alt >= w), default=None)
        judged = items
        if window:
            cut = [i for i in items if i['arm'] == 'alternative'][window - 1]['at']
            judged = [i for i in items if i['at'] <= cut]
        arms = {}
        for arm in ('default', 'alternative'):
            group = [i for i in judged if i['arm'] == arm]
            rate, n_eff = _hajek(group)
            runtimes = [i['runtime_s'] for i in group if i['runtime_s'] is not None]
            arms[arm] = dict(spec=default if arm == 'default' else alternative, assigned=len(group),
                             first_pass=rate, n_eff=round(n_eff, 1),
                             not_followed=sum(1 for i in group if i['followed'] is False),
                             no_outcome=sum(1 for i in group if i['first_pass'] is None),
                             runtime_min=statistics.median(runtimes) / 60 if runtimes else None)
        run_count = len({i['run'] for i in judged})
        diff = _difference(judged)
        ci = (_bootstrap(judged, rng) if arms['alternative']['assigned'] >= MIN_ALTERNATIVES
              and run_count >= MIN_RUNS else None)
        stronger_alternative = tier == 'low'
        if not window:
            verdict = 'not enough randomized data (%d/%d alternative assignments)' % (n_alt, WINDOWS[0])
        elif ci is None:
            verdict = 'window %d reached, but too few runs for an interval' % window
        elif stronger_alternative:
            verdict = ('switch to %s: it passes more (interval above 0)' % alternative if ci[0] > 0
                       else 'keep %s: %s is not shown to pass more' % (default, alternative))
        else:
            cheaper_faster = (arms['alternative']['runtime_min'] or math.inf) < (arms['default']['runtime_min'] or 0)
            verdict = ('%s is non-inferior and faster: worth making the default' % alternative
                       if ci[0] > NONINFERIORITY and cheaper_faster
                       else 'keep %s: %s is not shown to be non-inferior' % (default, alternative))
        rows.append(dict(tier=tier, default=default, alternative=alternative, alternative_is='stronger'
                         if stronger_alternative else 'cheaper', window=window, runs=run_count, arms=arms,
                         difference=diff, ci=ci, verdict=verdict))
    return rows


def _pct(x):
    return '   -' if x is None else '%3.0f%%' % (100 * x)


def main(args) -> int:
    db = connect()
    try:
        runs = Runs(Store(db))
    finally:
        db.close()
    repo = os.path.realpath(args.repo) if args.repo else None
    since = time.time() - args.since * 86400 if args.since else None
    records = tasks(runs, repo=repo, since=since)
    report = dict(observational=observational(records), randomized=randomized(records))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    print('observational: first attempts by declared tier and the model that ran them')
    print('  (routing chose who ran what, so a model given the hard tasks looks worse than it is)')
    print('  %-7s %-26s %5s %6s %-13s %6s %10s %8s %8s' % ('tier', 'model:effort', 'tasks', 'first', '95%',
                                                          'passed', 'unfinished', 'attempts', 'dwarf min'))
    for r in report['observational']:
        low, high = r['ci']
        print('  %-7s %-26s %5d %6s [%s-%s] %6s %10d %8.1f %8s' % (
            r['tier'], r['spec'], r['tasks'], _pct(r['first_pass']), _pct(low).strip(), _pct(high).strip(),
            _pct(r['passed']), r['unfinished'], r['attempts'],
            '-' if r['runtime_min'] is None else '%.1f' % r['runtime_min']))
    print()
    print('randomized (plan --explore): each pair of models, tasks counted in the arm they were assigned')
    pairs = [r for r in report['randomized'] if 'note' not in r]
    for r in report['randomized']:
        if 'note' in r:
            print('  (%s)' % r['note'])
    if not pairs:
        print('  no exploratory decisions in pairs yet; `plan` explores at 0.2 by default')
    for r in pairs:
        d, a = r['arms']['default'], r['arms']['alternative']
        print('  %s: %s (default) vs %s (%s), %d run(s)%s' % (
            r['tier'], r['default'], r['alternative'], r['alternative_is'], r['runs'],
            ', judged at window %d' % r['window'] if r['window'] else ''))
        for name, arm in (('default', d), ('alternative', a)):
            print('    %-11s %-24s assigned %3d  first-pass %s  n_eff %5.1f  not followed %d  no outcome %d  dwarf min %s' % (
                name, arm['spec'], arm['assigned'], _pct(arm['first_pass']), arm['n_eff'], arm['not_followed'],
                arm['no_outcome'], '-' if arm['runtime_min'] is None else '%.1f' % arm['runtime_min']))
        if r['difference'] is not None:
            print('    difference %+.2f%s' % (r['difference'], '' if not r['ci'] else '  95%% [%+.2f, %+.2f]' % tuple(r['ci'])))
        print('    -> %s' % r['verdict'])
    return 0
