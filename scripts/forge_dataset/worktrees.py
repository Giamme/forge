"""`forge dataset worktrees`: what the dataset knows about the worktrees forge created (#125).

forge-ui's Worktrees screen reads this through the CLI (forge_ui never opens the store itself) and adds
what git and the run dirs say now. Three kinds of record:

  events      every `worktree_created` and `worktree_removed` event: when forge added or removed a
              worktree, with its repository, run, task, path, branch and kind
  attempts    each (repository, run id, task) a parallel attempt was recorded for: a task worktree
              existed at the derived path, before the events were recorded (no dates)
  integrated  each (repository, run id) whose integrate moved the user's branch (`integrate_advanced`)

`since` is the time of the first worktree event: the list of removed worktrees is complete only from
then on. With the dataset off, or no store yet, everything is empty and `enabled` says which.
Read-only: it never creates a store.
"""
from __future__ import annotations

import json
import time

from . import connect, dataset_dir, enabled

_NAMES = ('worktree_created', 'worktree_removed')
_FIELDS = ('path', 'branch', 'kind', 'repo_path', 'by')


def _empty(on):
    return dict(enabled=on, since=None, events=[], attempts=[], integrated=[], repos=[])


def load() -> dict:
    if not enabled():
        return _empty(False)
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return _empty(True)
    db = connect()
    try:
        paths = dict(db.execute('SELECT repo, path FROM repos'))
        out = _empty(True)
        out['repos'] = [dict(repo=key, path=path) for key, path in sorted(paths.items()) if path]
        rows = db.execute("SELECT ts, repo, run, run_id, task, data FROM events WHERE kind = 'event' "
                          "AND (data LIKE '%\"name\": \"worktree_created\"%' OR data LIKE '%\"name\": \"worktree_removed\"%' "
                          "OR data LIKE '%\"name\": \"integrate_advanced\"%') ORDER BY ts")
        integrated = set()
        for ts, repo, run, run_id, task, data in rows:
            try:
                d = json.loads(data)
            except ValueError:
                continue
            name = d.get('name')
            if name == 'integrate_advanced':
                if run_id and paths.get(repo):
                    integrated.add((paths[repo], run_id))
                continue
            if name not in _NAMES or not isinstance(d.get('path'), str):
                continue
            event = dict(name=name, ts=ts, run=run, run_id=run_id, task=task)
            event.update({k: d.get(k) if isinstance(d.get(k), str) else None for k in _FIELDS})
            if event['branch'] == '-':
                event['branch'] = None
            event['repo_path'] = event['repo_path'] or paths.get(repo)
            out['events'].append(event)
        out['since'] = out['events'][0]['ts'] if out['events'] else None
        out['integrated'] = [dict(repo_path=p, run_id=r) for p, r in sorted(integrated)]
        seen = set()
        for repo, run_id, task in db.execute("SELECT DISTINCT repo, run_id, task FROM events WHERE kind = 'attempt' "
                                             "AND mode = 'parallel' AND task IS NOT NULL AND run_id IS NOT NULL"):
            if paths.get(repo) and (paths[repo], run_id, task) not in seen:
                seen.add((paths[repo], run_id, task))
        out['attempts'] = [dict(repo_path=p, run_id=r, task=t) for p, r, t in sorted(seen)]
        return out
    finally:
        db.close()


def main(args) -> int:
    rep = load()
    if args.json:
        print(json.dumps(rep, sort_keys=True))
        return 0
    if not rep['enabled']:
        print('the dataset is off (FORGE_DATASET=off): no worktree history is recorded')
        return 0
    created = sum(1 for e in rep['events'] if e['name'] == 'worktree_created')
    removed = sum(1 for e in rep['events'] if e['name'] == 'worktree_removed')
    print('worktree events: %d created, %d removed%s' % (created, removed, '' if rep['since'] is None else
                                                          ' (recorded since %s)' % _when(rep['since'])))
    print('tasks with an attempt (worktrees before the record): %d' % len(rep['attempts']))
    print("forge-ui's Worktrees screen (#/worktrees) adds what git and the run dirs say now.")
    return 0


def _when(ts):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ts))

