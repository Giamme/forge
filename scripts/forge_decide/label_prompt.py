"""The free label of the prompt adequacy gate (GitHub #40): was the task's prompt inadequate?

`label(index, record, event, body) -> {qid: bool}` for qid `prompt_adequacy`, the signature of
the labellers in forge_decide.labels. **True means inadequate**: the gate should have sent the
task back. The site is inverted (forge_decide.sites: `invert`), so a fired task labelled true
is fired-right.

  true   the first attempt was reviewed, and either its review has a finding saying the task is
         ambiguous, or the prompt it was dispatched with (the last plan revision at or before
         its start) is a later revision's, with other text than the request scored
  false  the first attempt was reviewed, and neither
  none   the task is in `plan_gates.sent_back.prompt` of any revision (the gate acted, so what
         followed is its own doing), the task never ran, its first review is missing or cannot
         be read (a rewrite alone is then no label), or the rewrite cannot be told (the request
         has no prompt text, or the attempt's start is unknown)

The prompt of a revision is the plan event's per-task `prompt` blob; the scored one is the
request's `state.prompt`. The attempt starts at its dwarf dispatch's `wall_started`. The review
is the `qa` dispatch of attempt 1, its `blobs.message` parsed by forge-issues.py's
`parse_findings`.
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


def _text(index, plan, task) -> str:
    """The prompt of `task` in a revision: its plan-event blob, '' without one."""
    row = (plan['d'].get('tasks') or {}).get(task)
    return index.store.text(row.get('prompt') if isinstance(row, dict) else None).strip()


def _started(index, first, run, task):
    """When attempt 1 started: its dwarf dispatch's `wall_started`, else the attempt event's.
    Never the event's `ts`, which records completion; None when neither says."""
    for e in index.runs.dispatches.get((run, task, 'dwarf'), []):
        if e['attempt'] == 1 and e['d'].get('wall_started'):
            return float(e['d']['wall_started'])
    return float(first['d']['wall_started']) if first['d'].get('wall_started') else None


def _rewritten(index, plans, task, scored, asked, started):
    """Whether the prompt the first attempt was dispatched with is not the one the request
    scored: the last revision holding the task at or before `started`, if later than the
    request, has other text than `scored` (state.prompt). Snapshots are taken after the gate
    scores, so the request's own text, not an earlier snapshot, is the baseline. None when
    that cannot be told: no scored text, or an unknown start."""
    if not scored or started is None:
        return None
    held = [plan for plan in plans if task in (plan['d'].get('tasks') or {}) and plan['ts'] <= started]
    if not held or held[-1]['ts'] <= asked:
        return False
    return _text(index, held[-1], task) != scored


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
    review = _review(index, run, task)
    if review is None or review['d'].get('findings_parse') == 'unparseable':
        return {}
    found = _parse_findings(index.store.text((review['d'].get('blobs') or {}).get('message')))
    if any(AMBIGUOUS.search(item.get('text') or '') for item in found):
        return {QID: True}
    scored = state.get('prompt')
    rewritten = _rewritten(index, plans, task, scored.strip() if isinstance(scored, str) else '',
                           event['ts'], _started(index, first, run, task))
    return {} if rewritten is None else {QID: rewritten}
