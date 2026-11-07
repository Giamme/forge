"""`forge dataset pairs`: what a second reviewer of the same attempt saw, per pair of reviewer settings (#121).

`--shadow-qa` reviews a sampled attempt twice: the primary that decides, and a shadow that nothing reads.
`forge dataset status` says how often the two agree per model pair. This report splits the pairs by
what differs between the two reviewers, so an effort comparison is never mixed with a model one:

  effort     the same harness and model at another effort or harness (`sol:medium` beside `sol:high`)
  model      another model of the same family
  family     another model family
  control    identical harness, model and effort: two runs of one reviewer, so their disagreement is
             noise. A control is never an effort comparison; it is the baseline the others are read
             against.

For every pair of settings, the two sides are ordered (lower effort, then model, first) and it gives:
  - pairs, verdict agreement and its Wilson 95% interval; `enough` from MIN_PAIRS pairs, the bar
    `forge dataset routing` uses for its randomized arms
  - blocking findings (CONFIRMED, at the attempt's threshold or worse, or unlabelled) found by only
    one side: the other side named nothing within LINE_WINDOW lines of it in the same file. A finding
    both name at different severities is a severity disagreement, not a unique find
  - the medians, over pairs, of b's tokens and time as a multiple of a's

"Found more" is not "better": whether a unique find was real is judged afterwards (outcomes, hidden
tests). Advisory only: nothing that routes or gates reads this report.
"""
from __future__ import annotations

import importlib.util
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path

from . import connect, dataset_dir
from .export import Runs, Store, shadow_pairs
from .facts import LINE_WINDOW, _matched, _threshold
from .routing import wilson

MIN_PAIRS = 30
KINDS = ('effort', 'model', 'family', 'control')
EFFORT_RANK = dict(minimal=0, low=1, medium=2, high=3, xhigh=4, max=5)
_SHADOW = None


def family(model: str) -> str:
    """forge-shadow.py's model family, the one its pick prefers to differ from."""
    global _SHADOW
    if _SHADOW is None:
        spec = importlib.util.spec_from_file_location('forge_shadow', Path(__file__).resolve().parents[1] / 'forge-shadow.py')
        _SHADOW = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_SHADOW)
    return _SHADOW.family(model)


def settings(d) -> tuple:
    return d.get('harness'), d.get('model'), d.get('effort')


def kind(a: tuple, b: tuple) -> str:
    if a == b:
        return 'control'
    if a[1] == b[1]:
        return 'effort'
    return 'model' if family(a[1] or '') == family(b[1] or '') else 'family'


def label(s: tuple) -> str:
    harness, model, effort = s
    return '%s%s%s' % (model or '?', ':' + effort if effort else '', ' (%s)' % harness if harness else '')


def _order(s: tuple):
    return (EFFORT_RANK.get(s[2] or '', -1), s[1] or '', s[0] or '', s[2] or '')


def _blocking(f, rank) -> bool:
    return f.get('status') == 'CONFIRMED' and (f.get('severity') is None or f['severity'] <= rank)


def _unique(mine, theirs, rank) -> int:
    """Blocking findings of `mine` with nothing of `theirs` near them (unlocated ones always count)."""
    matched = {id(a) for a, _ in _matched(mine, theirs)}
    return sum(1 for f in mine if _blocking(f, rank) and id(f) not in matched)


def _tokens(d):
    t = d.get('tokens') or {}
    values = [t.get('input'), t.get('output')]
    return sum(v for v in values if isinstance(v, (int, float))) if any(isinstance(v, (int, float)) for v in values) else None


def _time(d):
    for key in ('elapsed_s', 'duration_s'):
        value = d.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None


def _ratio(xs):
    return round(statistics.median(xs), 2) if xs else None


def report(runs: Runs, repo: str = None) -> dict:
    paths = runs.store.repo_paths
    rows = defaultdict(lambda: dict(pairs=0, agree=0, only_a=0, only_b=0, tokens=[], time=[]))
    for (run, task, n), (primary, shadow) in shadow_pairs(runs).items():
        if repo and paths.get(primary['repo']) != repo:
            continue
        sides = [primary['d'], shadow['d']]
        sides.sort(key=lambda d: _order(settings(d)))
        a, b = sides
        row = rows[(kind(settings(a), settings(b)), settings(a), settings(b))]
        rank = _threshold(runs, run, task, n)
        found_a = [f for f in a.get('findings') or [] if isinstance(f, dict)]
        found_b = [f for f in b.get('findings') or [] if isinstance(f, dict)]
        row['pairs'] += 1
        row['agree'] += a.get('verdict') == b.get('verdict')
        row['only_a'] += _unique(found_a, found_b, rank)
        row['only_b'] += _unique(found_b, found_a, rank)
        for key, measure in (('tokens', _tokens), ('time', _time)):
            x, y = measure(a), measure(b)
            if x and y:
                row[key].append(y / x)
    out = []
    for (k, a, b), row in rows.items():
        lo, hi = wilson(row['agree'], row['pairs'])
        out.append(dict(kind=k, a=label(a), b=label(b), pairs=row['pairs'], agree=row['agree'],
                        agreement=round(row['agree'] / row['pairs'], 3), ci=[round(lo, 3), round(hi, 3)],
                        enough=row['pairs'] >= MIN_PAIRS, only_a=row['only_a'], only_b=row['only_b'],
                        tokens_ratio=_ratio(row['tokens']), time_ratio=_ratio(row['time'])))
    out.sort(key=lambda r: (KINDS.index(r['kind']), -r['pairs'], r['a'], r['b']))
    return dict(min_pairs=MIN_PAIRS, line_window=LINE_WINDOW, pairs=sum(r['pairs'] for r in out),
                by_kind={k: sum(r['pairs'] for r in out if r['kind'] == k) for k in KINDS}, comparisons=out)


def load(repo: str = None) -> dict:
    # Reading must not create a store: a fresh machine gets an empty report.
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return dict(min_pairs=MIN_PAIRS, line_window=LINE_WINDOW, pairs=0, by_kind={k: 0 for k in KINDS}, comparisons=[])
    db = connect()
    try:
        runs = Runs(Store(db))
    finally:
        db.close()
    return report(runs, repo=repo)


HEADINGS = dict(effort='effort, within one model', model='another model, one family',
                family='across families', control='controls: identical settings, so disagreement is noise')


def main(args) -> int:
    rep = load(os.path.realpath(args.repo) if args.repo else None)
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0
    print('pairs: %d usable shadow pairs (%s); advisory only' % (
        rep['pairs'], ', '.join('%d %s' % (rep['by_kind'][k], k) for k in KINDS)))
    if not rep['comparisons']:
        print('  none yet: `plan --shadow-qa <spec>` reviews a sampled share of attempts twice')
    for k in KINDS:
        rows = [r for r in rep['comparisons'] if r['kind'] == k]
        if not rows:
            continue
        print('%s:' % HEADINGS[k])
        for r in rows:
            sample = '' if r['enough'] else '  (under %d pairs)' % rep['min_pairs']
            cost = ', b costs %sx tokens and %sx time' % (r['tokens_ratio'] if r['tokens_ratio'] is not None else '?',
                                                          r['time_ratio'] if r['time_ratio'] is not None else '?')
            print('  a %s  vs  b %s: %d pairs, %d agree (%.0f%% [%.0f-%.0f%%]), blocking found only by a %d, only by b %d%s%s'
                  % (r['a'], r['b'], r['pairs'], r['agree'], 100 * r['agreement'], 100 * r['ci'][0], 100 * r['ci'][1],
                     r['only_a'], r['only_b'], cost, sample))
    return 0
