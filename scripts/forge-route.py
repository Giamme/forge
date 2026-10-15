#!/usr/bin/env python3
"""Exploratory routing: now and then give a task another of the user's models, and record the odds.

Every task in the run history ran on the model the router chose for it, so the data says
how that model did and nothing about how another would have done on the same task. A
small, recorded share of random assignments is what lets that be estimated later without
bias (inverse-propensity weighting needs the probability each choice had).

  forge-route.py explore --plan P --task ID --tier T --epsilon E --default SPEC --candidates A,B,...
      Called by `plan` for a row the user left to forge, after the pool's own turn picked
      SPEC (the pool counters advance exactly as without exploration). With probability
      1-E it keeps SPEC; with probability E it picks uniformly among the candidates, which
      are only the specs the user passed to this `plan` (`--dwarf`, `--dwarf-<tier>`).
      Prints the spec to use. Appends the decision to <plan>/tasks/<id>/route.jsonl (never
      rewritten) and marks a changed pick in tasks/<id>/route.explored for the table.

The propensity of the chosen spec is (1-E)*[chosen == SPEC] + E/len(candidates). Forge
still never picks a model the user did not name. FORGE_ROUTE_EXPLORE_SEED makes the draws
reproducible (tests only). Any failure prints SPEC: exploration never changes a plan by
breaking.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import secrets
import sys
import time
from pathlib import Path


def rng_for(task: str):
    seed = os.environ.get('FORGE_ROUTE_EXPLORE_SEED')
    return random.Random('%s:%s' % (seed, task)) if seed else secrets.SystemRandom()


def row_inputs(plan: Path, task: str) -> str:
    """The sha of what the task is, so a decision can be told apart from one about a rewritten task."""
    row = ''
    try:
        for line in (plan / 'tasks.tsv').read_text().splitlines():
            cells = line.split('\t')
            if cells and cells[0] == task:
                row = '\t'.join(cells[i] for i in (1, 3, 6) if i < len(cells))   # deps, files, title
                break
    except OSError:
        pass
    try:
        prompt = (plan / 'tasks' / task / 'prompt.md').read_text()
    except OSError:
        prompt = ''
    return hashlib.sha256(('%s\0%s' % (row, prompt)).encode()).hexdigest()[:16]


def explore(args) -> str:
    epsilon = float(args.epsilon)
    candidates = list(dict.fromkeys(s.strip() for s in args.candidates.split(',') if s.strip()))
    if not (0 < epsilon <= 1) or len(candidates) < 2 or args.default not in candidates:
        return args.default
    rng = rng_for(args.task)
    draw = rng.random()
    chosen = rng.choice(candidates) if draw < epsilon else args.default
    propensity = (1 - epsilon) * (chosen == args.default) + epsilon / len(candidates)
    plan = Path(args.plan)
    tdir = plan / 'tasks' / args.task
    tdir.mkdir(parents=True, exist_ok=True)
    decision = dict(at=time.time(), task=args.task, tier=args.tier, epsilon=epsilon, candidates=candidates,
                    default=args.default, chosen=chosen, explored=chosen != args.default,
                    propensity=round(propensity, 6), inputs=row_inputs(plan, args.task))
    decision['decision'] = hashlib.sha256(json.dumps(decision, sort_keys=True).encode()).hexdigest()[:16]
    with open(tdir / 'route.jsonl', 'a') as log:      # one line per resolution, never rewritten
        log.write(json.dumps(decision, sort_keys=True) + '\n')
    marker = tdir / 'route.explored'
    if decision['explored']:
        marker.write_text(chosen + '\n')
    elif marker.exists():
        marker.unlink()
    return chosen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge-route.py')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('explore')
    for name in ('plan', 'task', 'tier', 'epsilon', 'default', 'candidates'):
        p.add_argument('--' + name, required=True)
    args = ap.parse_args(argv)
    try:
        print(explore(args))
    except Exception:
        print(args.default)
    return 0


if __name__ == '__main__':
    sys.exit(main())
