"""Completeness checks for `forge dataset status`.

Each check is specific to a mode, because each mode legitimately lacks different
things: a solo run has no separate verification, and a final review's QA dispatch has
no task attempt. A check reports what it expected and did not find. It never repairs
anything, since the run dir that could fill the gap is usually gone.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from . import dataset_dir

RUNNING_GRACE_S = 6 * 3600   # a start this recent may simply still be running


def _events(db: sqlite3.Connection, where: str = '', params: tuple = ()) -> list[dict]:
    cursor = db.execute('SELECT rid, kind, ts, src, run, mode, task, parent, data FROM events %s' % where, params)
    names = [d[0] for d in cursor.description]
    out = []
    for row in cursor:
        event = dict(zip(names, row))
        event['data'] = json.loads(event['data'])
        out.append(event)
    return out


def _refs(value) -> list[str]:
    found = []
    if isinstance(value, dict):
        if set(value) >= {'sha', 'n', 'kept'} and isinstance(value.get('sha'), str):
            found.append(value['sha'])
        for item in value.values():
            found.extend(_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_refs(item))
    return found


def run(db: sqlite3.Connection, verbose: bool = False) -> dict:
    now = time.time()
    events = _events(db)
    by_rid = {e['rid'] for e in events}
    results = {}

    unfinished = [e for e in events if e['kind'] == 'start' and e['rid'][:-len(':start')] not in by_rid]
    results['incomplete'] = dict(
        what='executions that started but never recorded an end (killed, or still running)',
        items=sorted(e['rid'][:-len(':start')] for e in unfinished if now - e['ts'] > RUNNING_GRACE_S),
        running=sum(1 for e in unfinished if now - e['ts'] <= RUNNING_GRACE_S))

    attempts = {e['rid'] for e in events if e['kind'] == 'attempt'}
    orphan_qa = [e['rid'] for e in events if e['kind'] == 'dispatch' and e['data'].get('role') == 'qa'
                 and e['data'].get('scope') == 'task' and e['src'] == 'live'
                 and e.get('parent') and e['parent'] not in attempts and now - e['ts'] > RUNNING_GRACE_S]
    results['qa_without_attempt'] = dict(what='task QA dispatches whose task attempt was never recorded',
                                         items=sorted(orphan_qa))

    ended = {e['run'] for e in events if e['kind'] == 'run_end'}
    merges = {(e['run'], e['task']) for e in events if e['kind'] == 'event' and e['data'].get('name') == 'merge'}
    unmerged = sorted('%s/%s' % (e['run'], e['task']) for e in events
                      if e['kind'] == 'attempt' and e['mode'] == 'parallel' and e['src'] == 'live'
                      and e['data'].get('status') == 'PASS' and e['run'] in ended
                      and (e['run'], e['task']) not in merges)
    results['pass_without_merge'] = dict(what='parallel PASS attempts with no merge outcome in a finished run',
                                         items=unmerged)

    last_seen = {}
    for e in events:
        if e['mode'] == 'parallel' and e['kind'] == 'attempt' and e['src'] == 'live':
            last_seen[e['run']] = max(last_seen.get(e['run'], 0), e['ts'])
    results['run_without_end'] = dict(what='parallel runs with task attempts but no run end',
                                      items=sorted(r for r, ts in last_seen.items()
                                                   if r not in ended and now - ts > RUNNING_GRACE_S))

    blobs = dataset_dir() / 'blobs'
    missing = sorted({sha for e in events for sha in _refs(e['data'])
                      if not (blobs / sha[:2] / (sha + '.gz')).exists()})
    results['missing_blobs'] = dict(what='blob references with no stored blob', items=missing)

    if not verbose:
        for check in results.values():
            check['count'] = len(check['items'])
            check['items'] = check['items'][:5]
    else:
        for check in results.values():
            check['count'] = len(check['items'])
    return results


def report(results: dict) -> list[str]:
    lines = []
    problems = {name: c for name, c in results.items() if c['count']}
    running = results.get('incomplete', {}).get('running')
    if not problems:
        lines.append('checks: complete' + (' (%d execution(s) still running)' % running if running else ''))
        return lines
    lines.append('checks: %d issue(s)' % sum(c['count'] for c in problems.values()))
    for name, check in problems.items():
        lines.append('  %s: %d — %s' % (name, check['count'], check['what']))
        for item in check['items']:
            lines.append('    %s' % item)
        if check['count'] > len(check['items']):
            lines.append('    … (--verbose lists all)')
    return lines
