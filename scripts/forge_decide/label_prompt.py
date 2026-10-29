"""The free label of the prompt adequacy gate (GitHub #40): was the task's prompt inadequate?

`label(index, record, event, body) -> {qid: bool}` for qid `prompt_adequacy`, the signature of
the labellers in forge_decide.labels. **True means inadequate**: the gate should have sent the
task back. The site is inverted (forge_decide.sites: `invert`), so a fired task labelled true
is fired-right.

  true   a later plan revision of the run changed the task's prompt before its first attempt
         started, or the first attempt's review has a finding saying the task is ambiguous
  false  neither, and the first attempt was reviewed
  none   the task is in `plan_gates.sent_back.prompt` of any revision (the gate acted, so what
         followed is its own doing), the task never ran, or its first review cannot be read

The prompt of a revision is the plan event's per-task `prompt` blob. The review is the `qa`
dispatch of attempt 1, its `blobs.message` parsed by forge-issues.py's `parse_findings`.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import re

QID = 'prompt_adequacy'
AMBIGUOUS = re.compile(r'\b(ambiguous|unclear|underspecified|under-specified|not specified|'
                       r'unspecified|does not (say|specify))\b', re.I)
_issues = None


def _parse_findings(text):
    """forge-issues.py's parse_findings: its file name is not importable, so loaded by path."""
    global _issues
    if _issues is None:
        path = Path(__file__).resolve().parents[1] / 'forge-issues.py'
        spec = importlib.util.spec_from_file_location('forge_issues', path)
        _issues = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_issues)
    return _issues.parse_findings(text)


def _sent_back(plans, task) -> bool:
    for plan in plans:
        gates = plan['d'].get('plan_gates')
        sent = (gates.get('sent_back') if isinstance(gates, dict) else None)
        listed = sent.get('prompt') if isinstance(sent, dict) else None
        if isinstance(listed, list) and task in listed:
            return True
    return False


def _prompt_key(plan, task):
    """What identifies the prompt of `task` in a revision: its blob sha, None without one."""
    row = (plan['d'].get('tasks') or {}).get(task)
    ref = row.get('prompt') if isinstance(row, dict) else None
    if isinstance(ref, dict) and ref.get('sha'):
        return ref['sha']
    return None


def _rewritten(plans, task, asked, started) -> bool:
    """Whether a revision after the one the request was asked on, and before the first attempt
    started, holds another prompt for the task. The request was asked on the last revision
    at or before it (the first one when it precedes them all)."""
    held = [(plan, _prompt_key(plan, task)) for plan in plans]
    held = [(plan, key) for plan, key in held if key is not None]
    if not held:
        return False
    base = max((i for i, (plan, _key) in enumerate(held) if plan['ts'] <= asked), default=0)
    return any(key != held[base][1] for plan, key in held[base + 1:] if plan['ts'] <= started)


def _review(index, run, task):
    """The first attempt's reviewer dispatch, or None."""
    rows = [e for e in index.runs.dispatches.get((run, task, 'qa'), []) if e['attempt'] == 1]
    return min(rows, key=lambda e: e['ts']) if rows else None


def label(index, record, event, body) -> dict:
    from forge_decide import labels
    state = labels._state(body)
    task_state = state.get('task') if isinstance(state.get('task'), dict) else {}
    task = task_state.get('id') or event.get('task')
    if not task or QID not in record.decisions(body):
        return {}
    attempts = index.attempts(event['run'], event['repo'], task, event['ts'])
    first = next((a for a in attempts if a['attempt'] == 1), None)
    if first is None:
        return {}
    run = first['run']
    plans = index.runs.plans.get(run, []) if run else []
    if _sent_back(plans, task):
        return {}
    started = first['d'].get('wall_started') or first['ts']
    if _rewritten(plans, task, event['ts'], started):
        return {QID: True}
    review = _review(index, run, task)
    if review is None or review['d'].get('findings_parse') == 'unparseable':
        return {}
    found = _parse_findings(index.store.text((review['d'].get('blobs') or {}).get('message')))
    return {QID: any(AMBIGUOUS.search(item.get('text') or '') for item in found)}
