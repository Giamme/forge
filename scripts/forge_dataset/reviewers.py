"""`forge dataset reviewers`: how the work each reviewer passed held up afterwards.

A report card per reviewer (qa) model. For every attempt a primary reviewer PASSed whose
reviewed commit was integrated and then followed by `forge dataset outcomes`, the outcome
at one horizon (default 14 days) says whether the work held: it did not if a later commit
reverted it, or if fewer than 80% of the lines it added were still there (rewritten; the
same rule as the export's `survives` label). The rate of work that did not hold has a
95% Wilson interval, and is only given from MIN_LANDED outcomes on: below that the row
says "not enough data" rather than a rate a handful of tasks would make up.

Advisory only. Nothing that builds a prompt or routes a task reads this report, and it
counts only what forge saw: survival is followed for parallel integrations
(`integrate_advanced`), so solo runs and work merged by hand are not here. A row is keyed by
the reviewer's alias, as the forge-ui Models view is, or by its model when no alias was recorded.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict

from . import connect, dataset_dir
from .export import SURVIVES_AT, Runs, Store
from .routing import wilson

MIN_LANDED = 10
HORIZON_DAYS = 14


def _empty(horizon_days):
    return dict(horizon_days=horizon_days, min_landed=MIN_LANDED, survives_at=SURVIVES_AT, reviewers=[],
                unattributed=dict(not_passed=0, no_review=0), skipped=dict(lost=0, no_additions=0),
                other_horizons=[])


def report(runs: Runs, *, horizon_days: int = HORIZON_DAYS, repo: str = None) -> dict:
    out = _empty(horizon_days)
    paths = runs.store.repo_paths
    # Which attempts reviewed each commit: a QA-only resume or a combined task can share one.
    attempts_of = defaultdict(list)
    for key, commit in runs.reviewed.items():
        attempts_of[commit].append(key)
    outcomes, horizons = {}, set()
    for e in runs.store.of('outcome'):
        d = e['d']
        if d.get('name') != 'survival' or not d.get('reviewed'):
            continue
        if repo and paths.get(e['repo']) != repo:
            continue
        if d.get('horizon_days') != horizon_days:
            horizons.add(d.get('horizon_days'))
            continue
        outcomes.setdefault(d['reviewed'], d)     # one outcome per commit and horizon
    out['other_horizons'] = sorted(h for h in horizons if isinstance(h, int))
    rows = {}
    for commit, d in sorted(outcomes.items()):
        if d.get('status') == 'lost':
            out['skipped']['lost'] += 1
            continue
        if d.get('status') != 'ok' or not d.get('added'):
            out['skipped']['no_additions'] += 1
            continue
        reviews = []
        for run, task, attempt in attempts_of.get(commit, ()):
            reviews += [r for r in runs.dispatches.get((run, task, 'qa'), ()) if r['attempt'] == attempt]
        passed = {}
        for r in sorted(reviews, key=lambda r: r['ts']):
            name = r['d'].get('alias') or r['d'].get('model')
            if r['d'].get('verdict') == 'PASS' and name:
                passed[name] = r['d']
        if not passed:
            # Reviewed and FAILed, then accepted by the severity gate or a human; or no review recorded.
            out['unattributed']['not_passed' if reviews else 'no_review'] += 1
            continue
        reverted = bool(d.get('reverted_by'))
        rewritten = not reverted and d['surviving'] / d['added'] < SURVIVES_AT
        for name, r in passed.items():
            row = rows.setdefault(name, dict(reviewer=name, specs=set(), landed=0, reverted=0, rewritten=0))
            spec = ':'.join(x for x in (r.get('model'), r.get('effort')) if x)
            if spec:
                row['specs'].add(spec)
            row['landed'] += 1
            row['reverted'] += reverted
            row['rewritten'] += rewritten
    for row in rows.values():
        row['specs'] = sorted(row['specs'])
        row['did_not_hold'] = row['reverted'] + row['rewritten']
        row['enough'] = row['landed'] >= MIN_LANDED
        if row['enough']:
            row['rate'] = row['did_not_hold'] / row['landed']
            row['ci'] = list(wilson(row['did_not_hold'], row['landed']))
        else:
            row['rate'], row['ci'] = None, None
            row['note'] = 'not enough data (%d/%d)' % (row['landed'], MIN_LANDED)
    out['reviewers'] = sorted(rows.values(), key=lambda r: (not r['enough'], -r['landed'], r['reviewer']))
    return out


def load(horizon_days: int = HORIZON_DAYS, repo: str = None) -> dict:
    # Reading must not create a store: a fresh machine (or forge-ui asking) gets an empty report.
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return _empty(horizon_days)
    db = connect()
    try:
        runs = Runs(Store(db))
    finally:
        db.close()
    return report(runs, horizon_days=horizon_days, repo=repo)


def _pct(v):
    return '%.0f%%' % (v * 100)


def main(args) -> int:
    if args.horizon_days < 1:
        print('forge dataset reviewers: --horizon-days must be at least 1', file=sys.stderr)
        return 2
    rep = load(args.horizon_days, os.path.realpath(args.repo) if args.repo else None)
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0
    print('reviewers: PASSed work that did not hold up %d days after integrate' % rep['horizon_days'])
    print('  (reverted, or under %d%% of its added lines kept; advisory only, routing never reads it)'
          % (SURVIVES_AT * 100))
    if not rep['reviewers']:
        print('  no PASSed work with a %d-day outcome yet; `forge dataset outcomes --horizon-days %d` records them'
              % (rep['horizon_days'], rep['horizon_days']))
    else:
        print('  %-24s %6s %8s %9s  %s' % ('reviewer', 'landed', 'reverted', 'rewritten', 'did not hold'))
    for r in rep['reviewers']:
        held = r['note'] if not r['enough'] else '%s [%s-%s] (%d/%d)' % (
            _pct(r['rate']), _pct(r['ci'][0]), _pct(r['ci'][1]), r['did_not_hold'], r['landed'])
        print('  %-24s %6d %8d %9d  %s' % (r['reviewer'], r['landed'], r['reverted'], r['rewritten'], held))
    u, s = rep['unattributed'], rep['skipped']
    if u['not_passed'] or u['no_review']:
        print('  not counted for any reviewer: %d landed after a FAIL (accepted by the gate or by hand), %d with no review'
              % (u['not_passed'], u['no_review']))
    if s['lost'] or s['no_additions']:
        print('  skipped: %d lost (history rewritten past the integration), %d added no lines' % (s['lost'], s['no_additions']))
    if rep['other_horizons']:
        print('  outcomes also recorded at %s days: pass --horizon-days to see them'
              % ', '.join(str(h) for h in rep['other_horizons']))
    return 0
