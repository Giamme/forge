"""Statistics that decide whether a decision-model gate may be promoted to `on`.

Ported from the out-of-repo replay_score.py / analyze.py, so the 2026-10-16 drift numbers
reproduce. Pure functions over rows and pairs: standard library only, no I/O.

A row is a dict. `split` needs `run` and `ts`; `promotes` needs `run`, `label`, `replay`
and `baseline` (scores, higher = more likely positive; `replay` may be None for a
decision the replay did not answer, which then drops out of the replay's AUC).
"""
from __future__ import annotations

import random

MIN_DECISIONS = 30
MIN_RUNS = 8


def auc(pairs):
    """Mann-Whitney AUC of [(score, label)]; tied scores share their average rank.
    None unless both classes are present."""
    pairs = list(pairs)
    positives = sum(1 for _score, label in pairs if label)
    negatives = len(pairs) - positives
    if not positives or not negatives:
        return None
    ordered = sorted(((score, 1 if label else 0) for score, label in pairs), key=lambda item: item[0])
    rank_sum = 0.0
    i = 0
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j][0] == ordered[i][0]:
            j += 1
        rank_sum += (i + j + 1) / 2.0 * sum(ordered[k][1] for k in range(i, j))
        i = j
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def split(rows):
    """(calibrate, judged): runs ordered by their earliest `ts`, the older len//2 to
    calibrate and the rest judged. Each is a list of the rows of those runs."""
    first = {}
    for row in rows:
        first[row['run']] = min(first.get(row['run'], row['ts']), row['ts'])
    ordered = sorted(first, key=lambda run: (first[run], run))
    older = set(ordered[:len(ordered) // 2])
    return ([row for row in rows if row['run'] in older],
            [row for row in rows if row['run'] not in older])


def paired_bootstrap(rows, stat, *, n=2000, seed=7):
    """95% interval of stat(rows), resampling whole runs, since decisions of one run are
    not independent. `stat` returns a number or None (a resample it cannot judge, such as
    one with a single class). None when fewer than n/2 resamples are valid."""
    rng = random.Random(seed)
    by_run = {}
    for row in rows:
        by_run.setdefault(row['run'], []).append(row)
    keys = sorted(by_run)
    if not keys:
        return None
    values = []
    for _ in range(n):
        sample = [row for key in (rng.choice(keys) for _ in keys) for row in by_run[key]]
        value = stat(sample)
        if value is not None:
            values.append(value)
    if len(values) < n / 2:
        return None
    values.sort()
    # No -1 on the upper index, as replay_score._boot has it: the 2026-10-16 interval
    # reproduces only with this rule.
    return values[int(len(values) * 0.025)], values[int(len(values) * 0.975)]


def precision_at(pairs, threshold):
    """{precision, recall, flagged} of the decisions scoring >= threshold. precision is
    None when nothing is flagged, recall None when no decision is positive."""
    pairs = [(score, bool(label)) for score, label in pairs]
    flagged = [label for score, label in pairs if score >= threshold]
    positives = sum(label for _score, label in pairs)
    hits = sum(flagged)
    return dict(precision=hits / len(flagged) if flagged else None,
                recall=hits / positives if positives else None,
                flagged=len(flagged))


def _difference(rows):
    """AUC(replay) - AUC(baseline) over the rows both answered, None when undefined."""
    both = [row for row in rows if row.get('replay') is not None and row.get('baseline') is not None]
    replay = auc([(row['replay'], row['label']) for row in both])
    baseline = auc([(row['baseline'], row['label']) for row in both])
    return None if replay is None or baseline is None else replay - baseline


def promotes(judged_rows, *, margin=0.02, floor=None, threshold=None):
    """The promotion rule over the judged half: (passes, reason). Needs

    1. at least 30 labelled decisions,
    2. from at least 8 runs,
    3. the lower bound of the paired interval of AUC(replay) - AUC(baseline) > -margin,
    4. when `floor` is set, precision of the replay at `threshold` >= floor.

    `reason` names the first failed condition, else "passes"."""
    rows = [row for row in judged_rows if row.get('label') is not None]
    runs = {row['run'] for row in rows}
    if len(rows) < MIN_DECISIONS:
        return False, f'only {len(rows)} labelled decisions in the judged half, need {MIN_DECISIONS}'
    if len(runs) < MIN_RUNS:
        return False, f'only {len(runs)} judged runs, need {MIN_RUNS}'
    interval = paired_bootstrap(rows, _difference)
    if interval is None:
        return False, 'the replay and the baseline cannot be compared (a class is missing or too few resamples)'
    if not interval[0] > -margin:
        return False, (f'replay is worse than the cheap baseline: AUC difference lower bound '
                       f'{interval[0]:.3f} is not above -{margin}')
    if floor is not None:
        if threshold is None:
            return False, 'the precision floor needs a threshold'
        got = precision_at([(row['replay'], row['label']) for row in rows
                            if row.get('replay') is not None], threshold)['precision']
        if got is None:
            return False, f'the replay flags nothing at {threshold}, so precision cannot reach {floor}'
        if got < floor:
            return False, f'precision {got:.3f} at {threshold} is below the floor {floor}'
    return True, 'passes'
