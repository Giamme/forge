"""The label of the `jev-coverage` site (#47): was the requirement really left uncovered?

True means uncovered: the gate should have sent the requirement back. For `r<i>` the requirement
is `state.requirements[i]` of the stored request.

  true       a later plan revision of the run has a task whose acceptance criteria hold at least
             COVERED_SHARE of the requirement's content words, and the tasks the request asked
             about had none
  false      every task of the run merged, and no later revision changed any task's criteria
  no label   a requirement the gate itself sent back (its sha is in `plan_gates.sent_back.coverage`
             of any revision of the run): the rewrite that followed is the gate's own doing

Imported lazily by forge_decide.labels (`LABELLERS['jev-coverage']`); signature as the labellers
there: `label(index, record, event, body) -> {qid: bool}`.
"""
from __future__ import annotations

COVERED_SHARE = 0.6


def _criteria(index, plan) -> dict[str, str]:
    """task -> its acceptance criteria (whitespace collapsed), from a plan event's stored prompts."""
    from forge_jev import coverage
    out = {}
    tasks = plan['d'].get('tasks')
    for task, row in (tasks if isinstance(tasks, dict) else {}).items():
        ref = row.get('prompt') if isinstance(row, dict) else None
        out[task] = ' '.join(coverage.acceptance_criteria(index.store.text(ref)).split())
    return out


def _holds(words: set, criteria: dict[str, str]) -> bool:
    from forge_jev import coverage
    return any(len(words & coverage.content_words(text)) >= COVERED_SHARE * len(words)
               for text in criteria.values())


def label(index, record, event, body) -> dict:
    from forge_decide import labels
    from forge_jev import coverage

    state = labels._state(body)
    requirements = state.get('requirements')
    run = event['run']
    plans = index.runs.plans.get(run, []) if run else []
    if not plans or not isinstance(requirements, list):
        return {}
    asked_tasks = state.get('tasks') if isinstance(state.get('tasks'), list) else []
    asked = {str(t.get('id')): ' '.join(str(t.get('acceptance_criteria') or '').split())
             for t in asked_tasks if isinstance(t, dict)}
    sent_back = set()
    for plan in plans:
        gates = plan['d'].get('plan_gates')
        sent_back.update(((gates or {}).get('sent_back') or {}).get('coverage') or [])

    later = [_criteria(index, plan) for plan in plans if plan['ts'] > event['ts']]
    # A task the request never saw counts as changed when its criteria say anything.
    changed = any(text != asked.get(task, '') for criteria in later for task, text in criteria.items())
    final = plans[-1]['d'].get('tasks')
    merged = bool(final) and all(index.merged(run, task) for task in final)

    out = {}
    for qid in record.decisions(body):
        i = coverage._index(qid)
        if not 0 <= i < len(requirements):
            continue
        text = str(requirements[i])
        words = coverage.content_words(text)
        if not words or coverage.sha(text) in sent_back:
            continue
        if not _holds(words, asked) and any(_holds(words, criteria) for criteria in later):
            out[qid] = True
        elif merged and not changed:
            out[qid] = False
    return out
