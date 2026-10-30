"""Labels for `jev-dwarf-message` (#44): should the attempt have been sent back?

`unfinished` and `blocked` are true when the QA verdict of the request's task and attempt, in the
same run, was FAIL: the review rejected work the implementer had already said was not finished.
They are false when it was PASS. There is no label when that attempt has no review (the gate
sent it back, so a label must never come from the gate's own action), and `question` has none:
nothing downstream says whether the user needed to be asked.
"""
from __future__ import annotations

LABELLED = ('unfinished', 'blocked')


def _attempt(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _verdicts(index) -> dict:
    """(run, task, attempt) -> the verdict of the last `qa` dispatch event, built once per collect()."""
    if getattr(index, '_message_verdicts', None) is None:
        found = {}
        for event in sorted(index.store.of('dispatch'), key=lambda e: e['ts']):
            if event['d'].get('role') == 'qa':
                found[(event['run'], event['task'], event['attempt'])] = event['d'].get('verdict')
        index._message_verdicts = found
    return index._message_verdicts


def label(index, record, event, body) -> dict:
    state = body.get('state') if isinstance(body, dict) else None
    state = state if isinstance(state, dict) else {}
    task, attempt = state.get('task'), _attempt(state.get('attempt'))
    if not task or attempt is None:
        return {}
    verdict = _verdicts(index).get((event['run'], task, attempt))
    if verdict not in ('PASS', 'FAIL'):
        return {}
    return {qid: verdict == 'FAIL' for qid in record.decisions(body) if qid in LABELLED}
