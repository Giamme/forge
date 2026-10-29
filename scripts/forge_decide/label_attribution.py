"""Labels for `jev-attribution` (#45): is the suspect a breaking merge?

`culprit:<id>` is true when the run's attribution search verified prefix k-1 (PASS) and prefix k
(FAIL) for the merge k of that task: the gate was right to put it first. It is false when both
prefixes were verified and they are not a pass then a fail. There is no label when either prefix
was never verified, and none at all when the search found the base failing, ended inconclusive,
or left no event.

These are facts about verified prefixes, whichever order the search took: a confirmed guess is
labelled by the same evidence as any other boundary, so a label never comes from the gate's own
choice.
"""
from __future__ import annotations

import json

PREFIX = 'culprit:'


def _events(index) -> dict:
    """run -> the last `attribution` event's parsed blob, built once per collect()."""
    if getattr(index, '_attributions', None) is None:
        found = {}
        for event in sorted(index.store.of('event'), key=lambda e: e['ts']):
            d = event['d']
            if d.get('name') != 'attribution':
                continue
            try:
                doc = json.loads(index.store.text((d.get('blobs') or {}).get('attribution')) or 'null')
            except ValueError:
                continue
            if isinstance(doc, dict):
                found[event['run']] = doc
        index._attributions = found
    return index._attributions


def _statuses(doc) -> dict:
    """prefix -> 'PASS' or 'FAIL', for the verified steps only."""
    out = {}
    for step in doc.get('steps') or []:
        if isinstance(step, dict) and step.get('status') in ('PASS', 'FAIL') and isinstance(step.get('prefix'), int):
            out.setdefault(step['prefix'], step['status'])
    return out


def label(index, record, event, body) -> dict:
    doc = _events(index).get(event['run']) if event.get('run') else None
    if doc is None or doc.get('base_fails') or doc.get('inconclusive'):
        return {}
    numbers = {}
    for k, merge in enumerate(doc.get('merges') or [], 1):
        if isinstance(merge, dict) and merge.get('id') is not None:
            numbers.setdefault(str(merge['id']), k)
    steps = _statuses(doc)
    out = {}
    for qid in record.decisions(body):
        k = numbers.get(qid[len(PREFIX):])
        if k is None or k - 1 not in steps or k not in steps:
            continue
        out[qid] = steps[k - 1] == 'PASS' and steps[k] == 'FAIL'
    return out
