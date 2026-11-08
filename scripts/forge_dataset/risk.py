"""`forge dataset risk`: a task's risk beside its difficulty (#122), measured in shadow.

Difficulty is what a task demands of the model; risk is what a mistake that gets through would cost
(what breaks, for whom, how hard it is to undo). The planner, or whoever writes the plan, records risk
in `tasks/<id>/risk`; nothing routes on it. This report says whether it should:

  rated       tasks with a risk, and how often it differs from their difficulty (the share that match
              and Cohen's kappa: a rating that only echoes difficulty adds nothing, and the issue says
              to stop there)
  cells       every (difficulty, risk) pair: tasks, first attempts that passed, tasks a primary review
              found a CONFIRMED P0 or P1 in, and landed work that did not hold at the longest horizon
              `forge dataset outcomes` followed (reverted, or under SURVIVES_AT of its lines kept).
              Low difficulty with high risk is the case the question is about.

A cell under MIN_CELL tasks is flagged: outcomes take 14 days to come due and the interesting cell is
rare, so a handful of tasks is reported as such rather than read as an answer. Each task counts once,
as its run's latest plan revision rated it. Advisory only: nothing that routes or gates reads it.
"""
from __future__ import annotations

import json
import os

from . import connect, dataset_dir
from .export import SURVIVES_AT, Runs, Store
from .facts import _kappa

LEVELS = ('low', 'medium', 'high')
MIN_CELL = 10


def _empty():
    return dict(tasks=0, rated=0, same=0, kappa=None, min_cell=MIN_CELL, survives_at=SURVIVES_AT, cells=[])


def report(runs: Runs, repo: str = None) -> dict:
    out = _empty()
    paths = runs.store.repo_paths
    cells = {}
    pairs = []
    for run, revisions in runs.plans.items():
        plan = revisions[-1]
        if repo and paths.get(plan['repo']) != repo:
            continue
        for task, row in (plan['d'].get('tasks') or {}).items():
            out['tasks'] += 1
            difficulty, risk = row.get('difficulty'), row.get('risk')
            if difficulty not in LEVELS or risk not in LEVELS:
                continue
            out['rated'] += 1
            out['same'] += difficulty == risk
            pairs.append((difficulty, risk))
            cell = cells.setdefault((difficulty, risk), dict(difficulty=difficulty, risk=risk, tasks=0, attempted=0,
                                                             first_pass=0, serious=0, landed=0, did_not_hold=0))
            cell['tasks'] += 1
            attempts = runs.attempts.get((run, task)) or []
            if attempts:
                cell['attempted'] += 1
                cell['first_pass'] += attempts[0]['d'].get('status') == 'PASS'
            reviews = runs.dispatches.get((run, task, 'qa')) or []
            cell['serious'] += any(f.get('status') == 'CONFIRMED' and f.get('severity') in (0, 1)
                                   for e in reviews for f in e['d'].get('findings') or [] if isinstance(f, dict))
            landed = next((runs.survival[c] for c in (runs.reviewed.get((run, task, a['attempt'])) for a in attempts)
                           if c in runs.survival), None)
            if landed:
                cell['landed'] += 1
                cell['did_not_hold'] += (landed['surviving'] / landed['added'] < SURVIVES_AT
                                         or bool(landed.get('reverted_by')))
    kappa = _kappa(pairs)
    out['kappa'] = None if kappa is None else round(kappa, 3)
    out['cells'] = [dict(cell, enough=cell['tasks'] >= MIN_CELL)
                    for _, cell in sorted(cells.items(), key=lambda kv: (LEVELS.index(kv[0][0]), LEVELS.index(kv[0][1])))]
    return out


def load(repo: str = None) -> dict:
    # Reading must not create a store: a fresh machine gets an empty report.
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return _empty()
    db = connect()
    try:
        runs = Runs(Store(db))
    finally:
        db.close()
    return report(runs, repo=repo)


def _rate(k, n):
    return '%d/%d' % (k, n) if n else '-'


def main(args) -> int:
    rep = load(os.path.realpath(args.repo) if args.repo else None)
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
        return 0
    print('risk: %d of %d tasks rated; advisory only, nothing routes on it' % (rep['rated'], rep['tasks']))
    if not rep['rated']:
        print('  none yet: the planner rates it, or write low, medium or high to tasks/<id>/risk before `plan`')
        return 0
    kappa = '' if rep['kappa'] is None else ', kappa %.2f' % rep['kappa']
    print('  risk equals difficulty for %d of %d (%.0f%%%s)' % (rep['same'], rep['rated'], 100 * rep['same'] / rep['rated'], kappa))
    print('  %-10s %-6s %5s %10s %8s %14s' % ('difficulty', 'risk', 'tasks', 'first pass', 'P0/P1', 'did not hold'))
    for c in rep['cells']:
        print('  %-10s %-6s %5d %10s %8s %14s%s' % (
            c['difficulty'], c['risk'], c['tasks'], _rate(c['first_pass'], c['attempted']), _rate(c['serious'], c['tasks']),
            _rate(c['did_not_hold'], c['landed']), '' if c['enough'] else '  (under %d tasks)' % rep['min_cell']))
    return 0
