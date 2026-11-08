"""`forge dataset tiers`: what explains results inside a difficulty tier (#123), before anyone adds a tier.

Difficulty works as a label, but `high` holds most of the unfinished tasks and may mix groups that behave
very differently. This report looks inside each tier with only what the dataset already has, and changes
nothing in a run.

Population: the tasks `forge dataset routing` counts, whose first attempt says something about the
implementer (fractal runs left out). Each task is described by:

  plan time   known before dispatch, so a rule could route on it: declared files, dependencies, wave,
              the size of the task's prompt (KB)
  afterwards  known only once the first attempt ran, so it can explain a result but never route one:
              lines in that attempt's diff, files it changed

and three outcomes: first attempt passed, attempts taken, unfinished (never passed and not accepted).

For each fact inside a tier:

  bins        the tier split into thirds of that fact, each with tasks, runs, first pass (Wilson 95%),
              mean attempts and unfinished. Descriptive: a run's tasks are correlated and routing chose
              who ran what, so read the intervals as optimistic.
  held out    whether a split on the fact predicts an outcome on runs it never saw. Runs go to FOLDS
              folds by a hash of their id. On the other folds' runs, each candidate threshold (the
              training values' quartiles) is scored on outcomes adjusted for the implementer: an
              outcome minus its model's mean in that tier, shrunk toward the tier's mean by SHRINK
              tasks, so a harder model mix is not read as harder tasks. The best threshold, and the
              side it says scores higher, is then applied to the held-out runs, adjusted by the
              training runs' model means. `lift` is the held-out difference between the side
              predicted higher and the other, in the outcome's units; above 0 means the training
              direction held. Its interval resamples held-out runs (BOOTSTRAP draws). A tier with
              fewer than MIN_RUNS runs, or no threshold leaving MIN_SIDE tasks on each side, gets
              none.

Repos are listed per tier too (first pass and the implementer-adjusted residual, with a run bootstrap),
but not held out: a repo's runs mostly sit in that repo, so a fold would just learn the repo.

Label noise: `--sample N --out DIR` writes N past task prompts (one task per run where it can, tiers
hidden) to DIR/prompts/<n>.md and DIR/sample.tsv; whoever rates them blind writes DIR/ratings.tsv as
`<n><TAB>low|medium|high`. `--blind DIR` then reports how often the blind rating matches the recorded
tier (Wilson 95%), Cohen's kappa and the confusion table. Rating costs model calls or a person's time,
so this report never starts one.

Advisory only: nothing that routes or plans reads it.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from . import connect, dataset_dir
from .export import Runs, Store, _attempts
from .facts import _kappa
from .harvest import split_list
from .routing import tasks as routing_tasks, wilson

LEVELS = ('low', 'medium', 'high')
OUTCOMES = ('first_pass', 'attempts', 'unfinished')
PLAN_FACTS = ('files', 'deps', 'wave', 'prompt_kb')
AFTER_FACTS = ('diff_lines', 'changed_files')
FOLDS = 5
MIN_RUNS = 10
MIN_SIDE = 5
SHRINK = 5
BOOTSTRAP = 1000
MIN_REPO = 10


def _fold(run) -> int:
    return int(hashlib.sha1(str(run).encode()).hexdigest(), 16) % FOLDS


def _held(runs: Runs) -> dict:
    """(run, task) -> (plan revision, row): the latest revision that held the task, as routing reads it."""
    held = {}
    for run, revisions in runs.plans.items():
        for plan in revisions:
            for tid, row in (plan['d'].get('tasks') or {}).items():
                held[(run, tid)] = (plan, row)
    return held


def _diff(first) -> tuple:
    for e in reversed(first['events']):
        numstat = e['d'].get('numstat')
        if isinstance(numstat, dict) and (numstat or e['d'].get('changed_files') is not None):
            lines = sum(x for counts in numstat.values() if isinstance(counts, (list, tuple))
                        for x in counts if isinstance(x, int))
            return lines, len(e['d'].get('changed_files') or numstat)
    return None, None


def _repo_name(runs: Runs, repo, names) -> str | None:
    if repo is None:
        return None
    if names and names.get(repo):
        return names[repo]
    path = runs.store.repo_paths.get(repo)
    return os.path.basename(path.rstrip('/')) if path else repo


def records(runs: Runs, repo: str = None, names: dict = None) -> list:
    held = _held(runs)
    out = []
    for r in routing_tasks(runs, repo=repo):
        if r['first_pass'] is None or r['tier'] not in LEVELS or (r['run'], r['task']) not in held:
            continue
        plan, row = held[(r['run'], r['task'])]
        attempts = _attempts(runs, r['run'], r['task'], runs.attempts.get((r['run'], r['task']), []))
        lines, changed = _diff(attempts[0]) if attempts else (None, None)
        prompt = runs.store.text(row.get('prompt')) or ''
        wave = (plan['d'].get('waves') or {}).get(r['task'])
        out.append(dict(
            run=r['run'], task=r['task'], tier=r['tier'], model=r['model'] or r['ran'] or '?',
            repo=_repo_name(runs, plan.get('repo'), names),
            first_pass=int(bool(r['first_pass'])), attempts=r['attempts'], unfinished=int(not r['passed']),
            facts=dict(files=len(split_list(row.get('files'))), deps=len(split_list(row.get('deps'))),
                       wave=wave if isinstance(wave, int) else None,
                       prompt_kb=round(len(prompt.encode()) / 1024, 1) if prompt.strip() else None,
                       diff_lines=lines, changed_files=changed)))
    return out


def _summary(rows) -> dict:
    n = len(rows)
    k = sum(r['first_pass'] for r in rows)
    low, high = wilson(k, n)
    return dict(tasks=n, runs=len({r['run'] for r in rows}), first_pass=k / n if n else None,
                ci=[low, high], attempts=statistics.mean(r['attempts'] for r in rows) if n else None,
                unfinished=sum(r['unfinished'] for r in rows))


def _bins(rows, fact) -> list:
    rows = sorted((r for r in rows if r['facts'][fact] is not None), key=lambda r: r['facts'][fact])
    values = [r['facts'][fact] for r in rows]
    if not values:
        return []
    cuts = sorted(set(statistics.quantiles(values, n=3, method='inclusive'))) if len(set(values)) > 1 else []
    groups = defaultdict(list)
    for r in rows:
        groups[sum(r['facts'][fact] > c for c in cuts)].append(r)
    out = []
    for _, group in sorted(groups.items()):
        lo, hi = group[0]['facts'][fact], group[-1]['facts'][fact]
        out.append(dict(_summary(group), lo=lo, hi=hi))
    return out


def _residuals(rows, outcome, train) -> list:
    base = statistics.mean(r[outcome] for r in train)
    by = defaultdict(list)
    for r in train:
        by[r['model']].append(r[outcome])
    means = {m: (sum(v) + SHRINK * base) / (len(v) + SHRINK) for m, v in by.items()}
    return [r[outcome] - means.get(r['model'], base) for r in rows]


def _candidates(values) -> list:
    return sorted(set(statistics.quantiles(values, n=4, method='inclusive'))) if len(set(values)) > 1 else []


def _lift(per_run, chosen) -> float | None:
    hi_n = hi_s = lo_n = lo_s = 0
    for run in chosen:
        a, b, c, d = per_run[run]
        hi_n, hi_s, lo_n, lo_s = hi_n + a, hi_s + b, lo_n + c, lo_s + d
    return hi_s / hi_n - lo_s / lo_n if hi_n and lo_n else None


def heldout(rows, fact, outcome, seed: int = 7) -> dict:
    rows = [r for r in rows if r['facts'][fact] is not None]
    runs = {r['run'] for r in rows}
    if len(runs) < MIN_RUNS:
        return dict(lift=None, reason='under %d runs' % MIN_RUNS, runs=len(runs))
    per_run = defaultdict(lambda: [0, 0.0, 0, 0.0])
    picks = Counter()
    for k in range(FOLDS):
        train = [r for r in rows if _fold(r['run']) != k]
        test = [r for r in rows if _fold(r['run']) == k]
        if not train or not test:
            continue
        res = _residuals(train, outcome, train)
        best = None
        for t in _candidates([r['facts'][fact] for r in train]):
            above = [e for r, e in zip(train, res) if r['facts'][fact] > t]
            below = [e for r, e in zip(train, res) if r['facts'][fact] <= t]
            if len(above) < MIN_SIDE or len(below) < MIN_SIDE:
                continue
            d = statistics.mean(above) - statistics.mean(below)
            if best is None or abs(d) > abs(best[1]):
                best = (t, d)
        if best is None:
            continue
        t, d = best
        picks[(t, d > 0)] += 1
        for r, e in zip(test, _residuals(test, outcome, train)):
            cell = per_run[r['run']]
            if (r['facts'][fact] > t) == (d > 0):
                cell[0] += 1
                cell[1] += e
            else:
                cell[2] += 1
                cell[3] += e
    scored = sorted(per_run)
    lift = _lift(per_run, scored)
    if lift is None:
        return dict(lift=None, reason='no split leaves %d tasks a side' % MIN_SIDE, runs=len(runs))
    rng = random.Random(seed)
    draws = sorted(x for x in (_lift(per_run, [rng.choice(scored) for _ in scored]) for _ in range(BOOTSTRAP))
                   if x is not None)
    (t, above_higher), folds = picks.most_common(1)[0]
    return dict(lift=lift, ci=[draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1]] if draws else None,
                tasks=sum(per_run[r][0] + per_run[r][2] for r in scored), runs=len(scored),
                threshold=t, above_higher=above_higher, folds=folds, of=sum(picks.values()))


def _repos(rows, seed: int = 7) -> list:
    res = dict(zip((id(r) for r in rows), _residuals(rows, 'first_pass', rows))) if rows else {}
    counts = Counter(r['repo'] for r in rows)
    groups = defaultdict(list)
    for r in rows:
        groups[r['repo'] if counts[r['repo']] >= MIN_REPO else None].append(r)
    out = []
    rng = random.Random(seed)
    for name, group in sorted(groups.items(), key=lambda kv: (kv[0] is None, -len(kv[1]), str(kv[0]))):
        by_run = defaultdict(list)
        for r in group:
            by_run[r['run']].append(res[id(r)])
        keys = sorted(by_run)
        draws = []
        for _ in range(BOOTSTRAP):
            pick = [x for _ in keys for x in by_run[rng.choice(keys)]]
            draws.append(sum(pick) / len(pick))
        draws.sort()
        values = [res[id(r)] for r in group]
        out.append(dict(_summary(group), repo=name, adjusted=sum(values) / len(values),
                        adjusted_ci=[draws[int(0.025 * BOOTSTRAP)], draws[int(0.975 * BOOTSTRAP) - 1]]))
    return out


def tier_report(rows) -> dict:
    facts = []
    for phase, names in (('plan', PLAN_FACTS), ('after', AFTER_FACTS)):
        for fact in names:
            facts.append(dict(fact=fact, phase=phase, known=sum(r['facts'][fact] is not None for r in rows),
                              bins=_bins(rows, fact), heldout={o: heldout(rows, fact, o) for o in OUTCOMES}))
    return dict(_summary(rows), facts=facts, repos=_repos(rows))


def _recorded(runs: Runs) -> dict:
    return {key: row.get('declared_difficulty') or row.get('difficulty') for key, (_, row) in _held(runs).items()}


def sample(runs: Runs, n: int, out: Path, seed: int = 7) -> int:
    """Write n past prompts for blind rating, spread one task per run before any run gives a second."""
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit('forge dataset tiers: %s is not empty; pick a new directory' % out)
    recorded = _recorded(runs)
    held = _held(runs)
    by_run = defaultdict(list)
    for (run, task), tier in sorted(recorded.items()):
        if tier in LEVELS and (runs.store.text(held[(run, task)][1].get('prompt')) or '').strip():
            by_run[run].append(task)
    rng = random.Random(seed)
    queues = [[(run, task) for task in rng.sample(by_run[run], len(by_run[run]))]
              for run in rng.sample(sorted(by_run), len(by_run))]
    chosen = []
    while len(chosen) < n and any(queues):
        for queue in queues:
            if queue and len(chosen) < n:
                chosen.append(queue.pop(0))
    (out / 'prompts').mkdir(parents=True, exist_ok=True)
    lines = ['# n\trun\ttask  (rate prompts/<n>.md blind; write ratings.tsv as <n><TAB>low|medium|high)']
    for i, (run, task) in enumerate(chosen, 1):
        (out / 'prompts' / ('%d.md' % i)).write_text(runs.store.text(held[(run, task)][1].get('prompt')))
        lines.append('%d\t%s\t%s' % (i, run, task))
    (out / 'sample.tsv').write_text('\n'.join(lines) + '\n')
    return len(chosen)


def blind(runs: Runs, where: Path) -> dict:
    where = Path(where)
    numbered = {}
    for line in (where / 'sample.tsv').read_text().splitlines():
        cells = line.split('\t')
        if len(cells) >= 3 and cells[0].isdigit():
            numbered[cells[0]] = (cells[1], cells[2])
    recorded = _recorded(runs)
    pairs, unknown, invalid = [], 0, 0
    for line in (where / 'ratings.tsv').read_text().splitlines():
        cells = line.strip().split('\t')
        if len(cells) < 2 or not cells[0].isdigit():
            continue
        rating = cells[1].strip().lower()
        key = numbered.get(cells[0])
        if rating not in LEVELS:
            invalid += 1
        elif key is None or recorded.get(key) not in LEVELS:
            unknown += 1
        else:
            pairs.append((recorded[key], rating))
    agree = sum(a == b for a, b in pairs)
    low, high = wilson(agree, len(pairs))
    kappa = _kappa(pairs)
    confusion = {a: {b: sum(1 for x, y in pairs if (x, y) == (a, b)) for b in LEVELS} for a in LEVELS}
    return dict(rated=len(pairs), agree=agree, rate=agree / len(pairs) if pairs else None, ci=[low, high],
                kappa=None if kappa is None else round(kappa, 3), confusion=confusion, unknown=unknown, invalid=invalid)


def report(runs: Runs, repo: str = None, names: dict = None, tier: str = None) -> dict:
    rows = records(runs, repo=repo, names=names)
    tiers = {}
    for level in LEVELS:
        group = [r for r in rows if r['tier'] == level]
        if group and (tier is None or tier == level):
            tiers[level] = tier_report(group)
    return dict(tasks=len(rows), runs=len({r['run'] for r in rows}), tiers=tiers,
                settings=dict(folds=FOLDS, min_runs=MIN_RUNS, min_side=MIN_SIDE, shrink=SHRINK, bootstrap=BOOTSTRAP))


def _names(db) -> dict:
    out = {}
    for repo, origin, path in db.execute('SELECT repo, origin, path FROM repos'):
        source = (origin or '').rstrip('/') or (path or '').rstrip('/')
        name = source.rsplit('/', 1)[-1].rsplit(':', 1)[-1]
        out[repo] = name[:-4] if name.endswith('.git') else (name or None)
    return out


def _runs():
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return None, {}
    db = connect()
    try:
        return Runs(Store(db)), _names(db)
    finally:
        db.close()


def _pct(x):
    return '-' if x is None else '%.0f%%' % (100 * x)


def _signed(x, outcome):
    return ('%+.0f pts' % (100 * x)) if outcome != 'attempts' else '%+.2f' % x


def _range(b):
    return ('%g' % b['lo']) if b['lo'] == b['hi'] else '%g-%g' % (b['lo'], b['hi'])


def _line(s):
    return '%4d tasks %3d runs  first pass %4s (%s-%s)  %.2f attempts  %d unfinished' % (
        s['tasks'], s['runs'], _pct(s['first_pass']), _pct(s['ci'][0]), _pct(s['ci'][1]), s['attempts'], s['unfinished'])


def _held_line(fact, outcome, h):
    if h.get('lift') is None:
        return '%s: none (%s)' % (outcome.replace('_', ' '), h['reason'])
    side = ('> %g' if h['above_higher'] else '<= %g') % h['threshold']
    ci = ' [%s, %s]' % (_signed(h['ci'][0], outcome), _signed(h['ci'][1], outcome)) if h.get('ci') else ''
    return '%s %s%s, higher at %s %s (%d/%d folds), %d runs' % (
        outcome.replace('_', ' '), _signed(h['lift'], outcome), ci, fact, side, h['folds'], h['of'], h['runs'])


def main(args) -> int:
    runs, names = _runs()
    if getattr(args, 'sample', None):
        if not args.out:
            raise SystemExit('forge dataset tiers: --sample needs --out DIR')
        if runs is None:
            raise SystemExit('forge dataset tiers: no dataset yet')
        n = sample(runs, args.sample, Path(args.out))
        print('wrote %d prompts to %s/prompts; rate them blind into %s/ratings.tsv, then: forge dataset tiers --blind %s'
              % (n, args.out, args.out, args.out))
        return 0
    if getattr(args, 'blind', None):
        if runs is None:
            raise SystemExit('forge dataset tiers: no dataset yet')
        rep = blind(runs, Path(args.blind))
        if args.json:
            print(json.dumps(rep, indent=2, sort_keys=True))
            return 0
        print('blind ratings: %d matched to a recorded tier%s' % (
            rep['rated'], ''.join(', %d %s' % (rep[k], k) for k in ('unknown', 'invalid') if rep[k])))
        if rep['rated']:
            kappa = '' if rep['kappa'] is None else ', kappa %.2f' % rep['kappa']
            print('  agree %d of %d: %s (%s-%s)%s' % (rep['agree'], rep['rated'], _pct(rep['rate']),
                                                    _pct(rep['ci'][0]), _pct(rep['ci'][1]), kappa))
            print('  %-10s %s' % ('recorded', ' '.join('%7s' % b for b in LEVELS)))
            for a in LEVELS:
                print('  %-10s %s' % (a, ' '.join('%7d' % rep['confusion'][a][b] for b in LEVELS)))
        return 0
    rep = report(runs, repo=os.path.realpath(args.repo) if args.repo else None, names=names,
                 tier=getattr(args, 'tier', None)) if runs else report_empty()
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0
    print('tiers: %d tasks whose first attempt says something, in %d runs; advisory only' % (rep['tasks'], rep['runs']))
    if not rep['tasks']:
        return 0
    for level, t in rep['tiers'].items():
        print()
        print('%s: %s' % (level, _line(t).strip()))
        for f in t['facts']:
            print('  %s (%s, known for %d)' % (f['fact'], 'plan time' if f['phase'] == 'plan' else 'afterwards', f['known']))
            for b in f['bins']:
                print('    %-10s %s' % (_range(b), _line(b)))
            for o in OUTCOMES:
                print('    held out: %s' % _held_line(f['fact'], o, f['heldout'][o]))
        print('  repos (first pass; adjusted = vs the same implementers elsewhere in the tier)')
        for r in t['repos']:
            print('    %-28s %s  adjusted %s [%s, %s]' % (
                (r['repo'] or 'other (under %d tasks each)' % MIN_REPO)[:28], _line(r),
                _signed(r['adjusted'], 'first_pass'), _signed(r['adjusted_ci'][0], 'first_pass'),
                _signed(r['adjusted_ci'][1], 'first_pass')))
    return 0


def report_empty() -> dict:
    return dict(tasks=0, runs=0, tiers={},
                settings=dict(folds=FOLDS, min_runs=MIN_RUNS, min_side=MIN_SIDE, shrink=SHRINK, bootstrap=BOOTSTRAP))
