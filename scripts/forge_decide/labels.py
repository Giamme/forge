"""Free labels for the live decision-model sites, read from the run dataset (GitHub #37).

A gate earns `on` only by being measured against a label that arrives for free after the
run. `collect(site)` turns the dataset store's stored live requests of one site into rows
of labelled decisions:

    {site, gate, run, run_id, repo, ts, key, model, body, labels: {qid: bool}, live: {qid: p}}

`body` is the decoded request blob ({state, model, questions}); `live` is the site's
`score` of the event's own answers. A request with no labelled decision gives no row.

The labels, per site (forge_decide.sites names each in one sentence):

  drift            the undeclared file was edited by the task's final passing attempt
  memory-slice     the fact's anchor was edited by the task (true), or lies in an area
                   the task never touched (false); a request needs both classes
  verify-discover  the candidate is the command the repo documents
  forward          a later plan revision added the dependency (true); both tasks merged
                   with no such dependency (false)

The joins are the export's (forge_dataset.export: Store, Runs, the final passing
attempt, the memory rule), not a copy. The store is only read: never opened by
connect(), which creates and migrates it. Imported only by `forge decide` commands: the
store is never read by anything that builds a prompt.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import sqlite3

from forge_dataset import dataset_dir
from forge_dataset import export
from forge_decide import sites


def _open_store() -> export.Store | None:
    """The dataset store, read-only; None when there is none yet."""
    path = dataset_dir() / 'dataset.sqlite'
    if not path.is_file():
        return None
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        return export.Store(db)
    finally:
        db.close()


def _body(store: export.Store, ref) -> dict | None:
    """The decoded request body, or None when it is cut, gone or not a request."""
    if not isinstance(ref, dict) or not ref.get('sha') or ref.get('cut'):
        return None
    try:
        body = json.loads(store.text(ref) or 'null')
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) and isinstance(body.get('state'), dict) else None


def requests(store: export.Store, site: str) -> list[tuple[dict, dict]]:
    """The stored live requests of a site, (event, body), the first per `key` by `ts`."""
    out, seen = [], set()
    for event in sorted(store.of('jev'), key=lambda e: e['ts']):
        d = event['d']
        if d.get('site') != site or d.get('ok') is not True or d.get('key') in seen:
            continue
        body = _body(store, (d.get('blobs') or {}).get('request'))
        if body is None:
            continue
        seen.add(d.get('key'))
        out.append((event, body))
    return out


def _state(body) -> dict:
    state = body.get('state')
    return state if isinstance(state, dict) else {}


def _started(rows) -> float:
    return min((e['d'].get('wall_started') or e['ts']) for e in rows)


class _Index:
    """The joins every labeller shares, built once per collect()."""

    def __init__(self, store: export.Store):
        self.store = store
        self.runs = export.Runs(store)
        self._by_task = None
        self._merged = None
        self._documented = {}

    def attempts(self, run, repo, task, ts) -> list:
        """The task's attempts in the request's run. A `legacy-` run uid never joins its
        attempts directly: those join by repo and task id, the run whose attempts started
        first within DRIFT_JOIN_S after the request; two runs in that window are ambiguous."""
        if run and not str(run).startswith('legacy-'):
            return self.runs.attempts.get((run, task), [])
        if self._by_task is None:
            self._by_task = defaultdict(list)
            for (attempt_run, attempt_task), rows in self.runs.attempts.items():
                if rows:
                    self._by_task[(rows[0]['repo'], attempt_task)].append((_started(rows), attempt_run))
        window = [(at, attempt_run) for at, attempt_run in self._by_task.get((repo, task), [])
                  if ts <= at <= ts + export.DRIFT_JOIN_S]
        if len({attempt_run for _at, attempt_run in window}) != 1:
            return []
        return self.runs.attempts.get((window[0][1], task), [])

    def merged(self, run, task) -> bool:
        """Whether the task's last merge event in the run says MERGED."""
        if self._merged is None:
            last = {}
            for e in sorted(self.store.of('event'), key=lambda e: e['ts']):
                if e['d'].get('name') == 'merge' and e['task']:
                    last[(e['run'], e['task'])] = e['d'].get('status')
            self._merged = {key for key, status in last.items() if status == 'MERGED'}
        return (run, task) in self._merged

    def documented(self, repo, ts) -> str | None:
        """The command the repo documents: the first non-comment line of its .forge/verify
        while the repo dir still has one, else the command of the latest verification of the
        same repo after `ts` that did not come from Jev."""
        if repo not in self._documented:
            path = self.store.repo_paths.get(repo)
            command = None
            try:
                if path and (Path(path) / '.forge' / 'verify').is_file():
                    lines = (Path(path) / '.forge' / 'verify').read_text(errors='replace').splitlines()
                    command = next((line.strip() for line in lines
                                    if line.strip() and not line.strip().startswith('#')), None)
            except OSError:
                command = None
            self._documented[repo] = command
        if self._documented[repo]:
            return self._documented[repo]
        later = [e for e in self.store.of('verification')
                 if e['repo'] == repo and e['ts'] > ts and e['d'].get('command')
                 and e['d'].get('command_source') not in (None, 'jev')]
        return max(later, key=lambda e: e['ts'])['d']['command'] if later else None


# --- labellers: (index, site record, event, body) -> {qid: bool} -------------------------

def _drift(index: _Index, record, event, body) -> dict:
    state = _state(body)
    task = state.get('task') if isinstance(state.get('task'), dict) else {}
    task_id = task.get('id')
    if not task_id:
        return {}
    final = export.final_pass(index.attempts(event['run'], event['repo'], task_id, event['ts']))
    if final is None:
        return {}
    changed = set(export.words(final['d'].get('changed_files')))
    declared = set(export.words(task.get('declared_files'))) | set(export.words(final['d'].get('declared_files')))
    out = {}
    for qid in record.decisions(body):
        path = sites._drift_file(body, qid)
        if path and path not in declared:
            out[qid] = path in changed
    return out


def _memory_slice(index: _Index, record, event, body) -> dict:
    facts = _state(body).get('facts')
    if not event['task'] or not isinstance(facts, list):
        return {}
    final = export.final_pass(index.runs.attempts.get((event['run'], event['task']), []))
    if final is None:
        return {}
    changed = set(export.words(final['d'].get('changed_files')))
    areas = {export.area(f) for f in changed | set(export.words(final['d'].get('declared_files')))}
    out = {}
    for qid in record.decisions(body):
        i = sites._index(qid)
        found = export.ANCHOR.search(str(facts[i])) if 0 <= i < len(facts) else None
        if not found:
            continue
        anchors = export.words(found.group(1))
        if any(a in changed for a in anchors):
            out[qid] = True
        elif not any(export.area(a) in areas for a in anchors):
            out[qid] = False
    # One class alone says nothing a ranking can be judged on.
    return out if set(out.values()) == {True, False} else {}


def _verify_discover(index: _Index, record, event, body) -> dict:
    commands = sites._commands(body) or []
    documented = index.documented(event['repo'], event['ts'])
    if not documented:
        return {}
    out = {}
    for qid in record.decisions(body):
        i = sites._index(qid)
        if 0 <= i < len(commands) and commands[i]:
            out[qid] = commands[i] == documented
    return out


def _depends(plan, earlier, later) -> bool:
    """Whether the plan revision lists `later` in `earlier`'s deps: the dependency the
    forward question is about (coupling.forward_pairs drops a pair that already has it)."""
    row = (plan['d'].get('tasks') or {}).get(earlier) or {}
    return later in export.words(row.get('deps'))


def _forward(index: _Index, record, event, body) -> dict:
    run = event['run']
    plans = index.runs.plans.get(run, []) if run else []
    if not plans:
        return {}
    out = {}
    for qid in record.decisions(body):
        text = sites._instructions(body, qid)
        earlier, later = sites._EARLIER.search(text), sites._LATER.search(text)
        if not earlier or not later:
            continue
        earlier, later = earlier.group('id'), later.group('id')
        if any(_depends(plan, earlier, later) for plan in plans if plan['ts'] > event['ts']):
            out[qid] = True
        elif index.merged(run, earlier) and index.merged(run, later) and not _depends(plans[-1], earlier, later):
            out[qid] = False
    return out


LABELLERS = {
    'jev-drift': _drift,
    'jev-memory-slice': _memory_slice,
    'jev-verify-discover': _verify_discover,
    'jev-forward': _forward,
}


def collect(site: str, *, store: export.Store | None = None, runs=None) -> list[dict]:
    """One row per stored live request of `site` with at least one labelled decision.

    `store` is an already-open forge_dataset.export.Store; without one the store is opened
    read-only. `runs`, when given, keeps only those dataset run uids. A site with no label
    (prompt, memory-curate, memory-stale) or an unknown one returns []."""
    record = sites.site(site)
    labeller = LABELLERS.get(site)
    if record is None or record.label is None or labeller is None:
        return []
    if store is None:
        store = _open_store()
        if store is None:
            return []
    index = _Index(store)
    out = []
    for event, body in requests(store, site):
        if runs is not None and event['run'] not in runs:
            continue
        labels = labeller(index, record, event, body)
        if not labels:
            continue
        answers = event['d'].get('answers') or {}
        live = {}
        for qid in labels:
            p = record.score(answers, qid, body)
            if p is not None:
                live[qid] = float(p)
        out.append(dict(site=site, gate=record.gate, run=event['run'], run_id=event['run_id'],
                        repo=event['repo'], ts=event['ts'], key=event['d'].get('key'),
                        model=event['d'].get('model') or body.get('model'), body=body,
                        labels=labels, live=live))
    return out
