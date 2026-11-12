"""`forge dataset history`: the reviews' findings and the learnings the dataset keeps, for runs whose dir is gone (#126).

forge-ui's Projects screen reads run dirs first; this is what it adds for the runs and checkouts that no longer
exist. Each run is one record with its repository and base commit, so the screen can tell which project it
belongs to (the same base-commit rule forge-ui's repos.py uses for a run whose checkout is gone):

  runs       {run (dataset uid), run_id, mode, repo_path, base, first, last}
  findings   one per review dispatch (qa, qa_shadow) that listed findings. A side dispatch
             (`side_*`, or the bare role `side`) is not a review and is left out: {run, run_id, task, attempt, role,
             scope, reviewer, verdict, ts, findings: [{head, severity (0-3 or null), status, line}]}, as the
             capture parsed them with forge-contract.py (no second parser)
  learnings  one per FORGE_LEARNING line a dispatch emitted: {run, run_id, task, role, model, ts, category, text}
  ledger     the ledger rows `forge dataset import --ledger` stored: {repo_path, ts, run_id, task, role, model,
             category, key, text, injectable}

Read-only: it never creates a store. With the dataset off everything is empty and `enabled` is false.
"""
from __future__ import annotations

import json

from . import connect, dataset_dir, enabled, is_side_role

_HEAD_MAX = 400
_TEXT_MAX = 2000


def _empty(on):
    return dict(enabled=on, runs=[], findings=[], learnings=[], ledger=[])


def load() -> dict:
    if not enabled():
        return _empty(False)
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return _empty(True)
    db = connect()
    try:
        paths = dict(db.execute('SELECT repo, path FROM repos'))
        out = _empty(True)
        runs = {}

        def note(run, run_id, mode, repo, ts):
            if not run:
                return None
            r = runs.setdefault(run, dict(run=run, run_id=run_id, mode=mode, repo_path=paths.get(repo), base=None,
                                          first=ts, last=ts))
            r['first'], r['last'] = min(r['first'], ts), max(r['last'], ts)
            r['run_id'] = r['run_id'] or run_id
            r['repo_path'] = r['repo_path'] or paths.get(repo)
            return r

        attempt_task = {}
        for rid, run, run_id, mode, repo, task, attempt, ts, data in db.execute(
                "SELECT rid, run, run_id, mode, repo, task, attempt, ts, data FROM events WHERE kind = 'attempt' ORDER BY ts"):
            r = note(run, run_id, mode, repo, ts)
            attempt_task[rid] = (task, attempt)
            if r is not None and r['base'] is None:
                try:
                    base = json.loads(data).get('base_ref')
                except ValueError:
                    base = None
                if isinstance(base, str) and len(base) in (40, 64):
                    r['base'] = base.lower()
        for run, run_id, mode, repo, task, attempt, parent, ts, data in db.execute(
                "SELECT run, run_id, mode, repo, task, attempt, parent, ts, data FROM events WHERE kind = 'dispatch' ORDER BY ts"):
            try:
                d = json.loads(data)
            except ValueError:
                continue
            note(run, run_id, mode, repo, ts)
            if task is None and parent in attempt_task:
                task, attempt = task or attempt_task[parent][0], attempt or attempt_task[parent][1]
            role = d.get('role')
            spec = d.get('alias') or d.get('model')
            if d.get('effort') and spec and ':' not in str(spec):
                spec = '%s:%s' % (spec, d['effort'])
            found = d.get('findings')
            # qa and qa_shadow only. side_* is not a review, so its findings are not history.
            if role in ('qa', 'qa_shadow') and not is_side_role(role) and isinstance(found, list) and found:
                items = []
                for f in found:
                    if not isinstance(f, dict) or not isinstance(f.get('head'), str):
                        continue
                    sev = f.get('severity')
                    items.append(dict(head=f['head'][:_HEAD_MAX], severity=sev if type(sev) is int and 0 <= sev <= 3 else None,
                                      status=f.get('status') if f.get('status') in ('CONFIRMED', 'PLAUSIBLE', 'UNSTATED') else None,
                                      line=f.get('line') if type(f.get('line')) is int else None))
                if items:
                    out['findings'].append(dict(run=run, run_id=run_id, task=task, attempt=attempt, role=role,
                                                scope=d.get('scope'), reviewer=spec, verdict=d.get('verdict'), ts=ts,
                                                findings=items))
            for line in d.get('learnings') or []:
                if not isinstance(line, str):
                    continue
                category, sep, text = line.partition('|')
                if not sep:
                    continue
                out['learnings'].append(dict(run=run, run_id=run_id, task=task, role=role, model=spec, ts=ts,
                                             category=category.strip().lower(), text=text.strip()[:_TEXT_MAX]))
        for repo, data in db.execute("SELECT repo, data FROM events WHERE kind = 'ledger' ORDER BY ts"):
            try:
                d = json.loads(data)
            except ValueError:
                continue
            if not isinstance(d.get('text'), str) or d.get('category') in (None, '-'):
                continue
            out['ledger'].append(dict(repo_path=paths.get(repo), ts=d.get('ts'), run_id=d.get('run_id'), task=d.get('task'),
                                      role=d.get('role'), model=d.get('model'), category=d.get('category'),
                                      key=d.get('key'), text=d['text'][:_TEXT_MAX], injectable=d.get('injectable') != '0'))
        out['runs'] = sorted(runs.values(), key=lambda r: r['first'])
        return out
    finally:
        db.close()


def main(args) -> int:
    rep = load()
    if args.json:
        print(json.dumps(rep, sort_keys=True))
        return 0
    if not rep['enabled']:
        print('the dataset is off (FORGE_DATASET=off)')
        return 0
    print('runs: %d, reviews with findings: %d, learnings: %d, ledger rows: %d' % (
        len(rep['runs']), len(rep['findings']), len(rep['learnings']), len(rep['ledger'])))
    print("forge-ui's Projects screen (#/projects) shows them by repository.")
    return 0
