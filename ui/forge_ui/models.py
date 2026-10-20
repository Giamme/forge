"""Model leaderboard facts: one row per task (with its attempts) and per review, for every run.

Rows are facts, not scores: the page aggregates them. Outcomes follow forge's own labelling
(scripts/forge_dataset/export.py `_attempts`/`_outcome`): an attempt that says nothing about the
implementer is censored, never counted as a failure.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import math
import os
import threading
import time

from forge_ui import executions, reader


_MAX_ROWS = 50000
_MAX_WARNINGS = 500
_RUNNING_S = 120
_SIZE = 4096
_PASS = ('PASS', 'MERGED')
_LOCK = threading.Lock()
_RUNS = OrderedDict()  # realpath -> (signature, computed at, facts)


def _alias(spec):
    return (spec.split(':', 1)[0] or None) if isinstance(spec, str) else None


def _attempt_rows(rd, name):
    """{n: (status, dwarf spec)} from attempts.tsv, oldest first. Infra-refunded rows spent no
    attempt; a QA-only resume reuses its number, so the number's last row holds its status."""
    rows = {}
    for label, row in rd.rows(name, 3):
        try:
            n = int(row[1])
        except ValueError:
            rd.warn(label, 'invalid attempt')
            continue
        status = row[2].strip()
        if not status or status == 'INFRA' or (len(row) > 3 and reader._nullable(row[3].strip())):
            continue
        rows[n] = (status, (row[4].strip() or None) if len(row) > 4 else None)
    return dict(sorted(rows.items()))


def _outcome(status, dwarfs, reviews):
    if status in _PASS:
        return 'pass', None
    if status == 'FAIL':
        return 'fail', None
    if status in ('TIMEOUT', 'ERROR'):
        # Both come from the implementer, the reviewer or forge's own steps between them; only an
        # implementer that ran out of time failed the task.
        if reviews:
            return 'censored', 'reviewer_' + status.lower()
        last = dwarfs[-1] if dwarfs else None
        if status == 'TIMEOUT' and last is not None and (last['exit_code'] == 7 or last['timed_out']):
            return 'fail', None
        code = last['exit_code'] if last is not None else None
        return 'censored', ('dwarf_' if code not in (0, None) else 'forge_') + status.lower()
    return 'censored', status.lower()


def _attempts(rows, execs, plan_spec):
    result = []
    for n, (status, logged) in rows.items():
        dwarfs = [item for item in execs if item['role'] == 'dwarf' and item['attempt'] == n]
        reviews = [item for item in execs if item['role'] == 'qa' and not item['shadow'] and item['attempt'] == n]
        outcome, reason = _outcome(status, dwarfs, reviews)
        last = dwarfs[-1] if dwarfs else None
        model = last['model'] if last is not None else None
        if model:
            spec, source = model['spec'], 'execution'
        elif logged:
            spec, source = logged, 'attempts.tsv'
        elif n == 1 and plan_spec:
            spec, source = plan_spec, 'plan'
        else:
            spec = source = None
        verdict = reviews[-1]['verdict'] if reviews else None
        result.append(dict(n=n, alias=model['alias'] if model else _alias(spec), spec=spec, source=source,
                           outcome=outcome, reason=reason, gate=outcome == 'pass' and verdict == 'FAIL',
                           model_s=last['phases'].get('model') if last is not None else None,
                           duration_s=last['duration_s'] if last is not None else None,
                           tokens=dict(last['tokens']) if last is not None and last['tokens'] else None,
                           timed_out=bool(last is not None and last['timed_out'])))
    return result


def _route(rd, base):
    explored, propensity = rd.exists(base + '/route.explored'), None
    for n, line in enumerate((rd.text(base + '/route.jsonl') or '').splitlines(), 1):
        obj = rd.object(f'{base}/route.jsonl line {n}', line) if line.strip() else None
        if obj is None:
            continue
        explored = explored or obj.get('explored') is True
        value = obj.get('propensity')
        # The newest decision is the one the task ran with.
        propensity = float(value) if type(value) in (int, float) and math.isfinite(value) else propensity
    return explored, propensity


def _reviews(key, ident, execs, shadows):
    rows = []
    for item in execs:
        if item['role'] == 'qa' and not item['shadow'] and item['state'] == 'done':
            model = item['model'] or {}
            rows.append(dict(run=key, task=ident, n=item['attempt'], kind='primary', alias=model.get('alias'),
                             spec=model.get('spec'), verdict=item['verdict'], findings=item['findings'],
                             duration_s=item['duration_s'], tokens=item['tokens'], agreement=None, primary_alias=None))
    for entry, items, _ in shadows:
        if entry['status'] != 'done':
            continue
        model = (items[-1]['model'] if items else None) or {}
        spec = entry['spec'] if isinstance(entry['spec'], str) else model.get('spec')
        primaries = [item for item in execs if item['role'] == 'qa' and not item['shadow']
                     and item['attempt'] == entry['attempt'] and item['model']]
        rows.append(dict(run=key, task=ident, n=entry['attempt'], kind='shadow', alias=model.get('alias') or _alias(spec),
                         spec=spec, verdict=entry['verdict'], findings=entry['findings'],
                         duration_s=entry['duration_s'], tokens=items[-1]['tokens'] if items else None,
                         agreement=entry['agreement'],
                         primary_alias=primaries[-1]['model']['alias'] if primaries else _alias(entry['primary'])))
    return rows


def _facts(path, summary):
    rd = reader._Read(path)
    key = summary['key']
    base_row = dict(run=key, repo=summary['repo'], created=summary['created'])
    tasks, reviews = [], []
    if summary['kind'] == 'solo':
        execs = reader._solo_execs(rd, reader._solo_status(rd), reader._history(rd, 'attempts.tsv'))
        tasks.append(dict(base_row, task='solo', kind='solo', difficulty=None, explored=False, propensity=None,
                          final_status=reader._solo_status(rd), accepted=False,
                          attempts=_attempts(_attempt_rows(rd, 'attempts.tsv'), execs, reader._role_spec(rd, 'dwarf')),
                          escalations=[]))
        reviews += _reviews(key, 'solo', execs, [])
    else:
        for task in reader._tasks(rd):
            base = f'tasks/{task["id"]}'
            task['attempts'], task['attempt'] = reader._attempt_state(rd, base)
            execs = reader._task_execs(rd, task)
            shadows = reader._shadows(rd, task['id'], execs)
            explored, propensity = _route(rd, base)
            tasks.append(dict(base_row, task=task['id'], kind='plan', difficulty=task['difficulty'] or None,
                              explored=explored, propensity=propensity, final_status=task['status'],
                              accepted=reader._nonempty(rd, base + '/accepted.by'),
                              attempts=_attempts(_attempt_rows(rd, base + '/attempts.tsv'), execs, task['dwarf']),
                              escalations=[{'n': item['attempt'], 'from': item['from'], 'to': item['to']}
                                           for item in reader._history(rd, base + '/escalations.tsv', True)]))
            reviews += _reviews(key, task['id'], execs, shadows)
        items = executions.review_dirs(rd)[0]
        published = reader._published(rd)
        for index, item in enumerate(items):
            review, execs, _ = reader._final_review(rd, item, index == 0, published)
            if review['state'] != 'done':
                continue
            run = execs[-1] if execs else None
            model = (run['model'] if run else None) or {}
            spec = review['spec'] or model.get('spec')
            reviews.append(dict(run=key, task=None, n=None, kind='final', alias=model.get('alias') or _alias(spec),
                                spec=spec, verdict=review['verdict'] if review['verdict'] in ('PASS', 'FAIL') else None,
                                findings=review['findings'], duration_s=review['duration_s'],
                                tokens=run['tokens'] if run else None, agreement=None, primary_alias=None))
    order = ('run', 'task', 'kind', 'repo', 'difficulty', 'created', 'explored', 'propensity', 'final_status',
             'accepted', 'attempts', 'escalations')
    return dict(tasks=[{name: row[name] for name in order} for row in tasks], reviews=reviews,
                warnings=[f'{key}: {item}' for item in rd.warnings])


def _signature(summary):
    return (summary['updated'], summary['task_count'], json.dumps(summary['counts'], sort_keys=True),
            summary['final_review'])


def _cached(path, summary, now):
    real = os.path.realpath(path)
    sig = _signature(summary)
    hit = _RUNS.get(real)
    if hit is not None and hit[0] == sig and not ('RUNNING' in summary['counts'] and now - hit[1] > _RUNNING_S):
        _RUNS.move_to_end(real)
        return hit[2]
    facts = _facts(path, summary)
    _RUNS[real] = (sig, now, facts)
    _RUNS.move_to_end(real)
    while len(_RUNS) > _SIZE:
        _RUNS.popitem(last=False)
    return facts


def collect(found):
    """Models: the facts of every run of `found` ((path, source) pairs), newest run first."""
    # One computation at a time: a cold cache reads every run once, not once per request.
    with _LOCK:
        now = time.time()
        runs = []
        for path, source in found:
            summary = reader.summarize(path, source)
            if reader._kind(path):
                runs.append((path, summary))
        runs.sort(key=lambda item: item[1]['updated'] if item[1]['updated'] is not None else float('-inf'), reverse=True)
        tasks, reviews, warnings, count, truncated = [], [], [], 0, False
        for path, summary in runs:
            if len(tasks) + summary['task_count'] > _MAX_ROWS:
                truncated = True
                break
            facts = _cached(path, summary, now)
            tasks += facts['tasks']
            reviews += facts['reviews']
            warnings += facts['warnings']
            count += 1
        return dict(generated=now, runs=count, truncated=truncated, tasks=tasks, reviews=reviews,
                    warnings=warnings[:_MAX_WARNINGS])
