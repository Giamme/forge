"""Labels for `jev-retry-addressed` (#43): was the finding left unaddressed?

`u<i>` is true when the review of the same run, task and attempt raises the same finding again
-- the retry did not fix it, so the gate should have sent the attempt back. It is false when that
review parsed completely and does not raise it.

There is no label when the attempt has no review (the gate, or another send-back, bounced it:
a label must never come from the gate's own action), and none when the review's findings cannot
be read in full (`unparseable`, `ungraded`, or a message the dataset cut): such a review may hold
findings the strict parser misses, so "not raised again" cannot be read from it.
"""
from __future__ import annotations

import re

from forge_jev.addressed import finding_file, parse_findings

JACCARD = 0.5
_WORDS = re.compile(r'[a-z]{4,}')


def _words(text) -> set[str]:
    return set(_WORDS.findall(str(text or '').lower()))


def _same(item: dict, found: dict) -> bool:
    """The same finding: the same file, and content words that overlap by at least JACCARD."""
    if finding_file(item) != str(found.get('file') or '').removeprefix('./'):
        return False
    ours, theirs = _words(item.get('text')), _words(found.get('text'))
    union = ours | theirs
    return bool(union) and len(ours & theirs) / len(union) >= JACCARD


def _reviews(index) -> dict:
    """(run, task, attempt) -> the last `qa` dispatch event, built once per collect()."""
    if getattr(index, '_qa_reviews', None) is None:
        found = {}
        for event in sorted(index.store.of('dispatch'), key=lambda e: e['ts']):
            if event['d'].get('role') == 'qa':
                found[(event['run'], event['task'], event['attempt'])] = event
        index._qa_reviews = found
    return index._qa_reviews


def _attempt(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def label(index, record, event, body) -> dict:
    state = body.get('state') if isinstance(body, dict) else None
    state = state if isinstance(state, dict) else {}
    task, attempt = state.get('task'), _attempt(state.get('attempt'))
    if not task or attempt is None:
        return {}
    review = _reviews(index).get((event['run'], task, attempt))
    if review is None:
        return {}
    d = review['d']
    if d.get('findings_parse') not in ('clean', 'graded'):
        return {}
    ref = (d.get('blobs') or {}).get('message')
    raised = parse_findings(index.store.text(ref))
    complete = not (isinstance(ref, dict) and ref.get('cut'))
    items = {item.get('i'): item for item in state.get('findings') or [] if isinstance(item, dict)}
    out = {}
    for qid in record.decisions(body):
        item = items.get(int(qid[1:]))
        if item is None:
            continue
        if any(_same(item, found) for found in raised):
            out[qid] = True
        elif complete:
            out[qid] = False
    return out
