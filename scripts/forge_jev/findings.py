"""Findings duplicates and triage (#46): which review findings restate an open issue, and which
PLAUSIBLE ones read as a preference.

A finished run can leave findings nobody will act on. `forge-issues.py` turns them into GitHub
issues, guarded by an exact fingerprint and an open issue with the same title; a finding
reworded from an open issue slips through both. This verb asks two reading questions about a
batch of findings, and prints the answers. It decides nothing: the caller applies the
`findings_dup` and `findings_concrete` thresholds and the gate's mode (`forge-issues.py` does,
and gh-proman can). It never changes a finding, a severity or a verdict.

    forge-jev.py findings --repo R --input F [--run-dir D]

`--run-dir` is the plan or run dir the findings came from, used for the dataset record and the
request cache; call the verb once per plan.

Input, a JSON object in file F:

    {"findings":   [{"fp", "plan", "severity", "status", "location", "text",
                     "candidates": [id, ...]}],
     "candidates": [{"id", "title", "body"}]}

`status` is CONFIRMED or PLAUSIBLE. The optional `candidates` of a finding narrows the issues it
is compared with (ids from the top-level list); without it, every candidate is an option.

Questions, one request, site `jev-findings`:
- `d<i>` per finding that has at least one candidate: rubric `finding_duplicate`, a Choice over
  the candidate ids plus `none`.
- `c<i>` per PLAUSIBLE finding: rubric `finding_concrete`.
Both quote the finding. The state is `{findings: [{i, fp, severity, status, location, text}],
candidates: {id: {title, body[:600]}}}`.

Output, a JSON list on stdout, one object per input finding, in input order:

    [{"fp", "duplicate_of": id|null, "dup_p": p|null, "concrete": p|null}]

- `duplicate_of` is the candidate id the model chose, null when it chose `none` or was not
  asked. `dup_p` is that choice's probability; when it chose `none`, the highest probability of
  any candidate (the closest duplicate); null when it was not asked.
- `concrete` is the probability that the finding names a concrete defect: one minus the Noul of
  `c<i>`, because the rubric asks the opposite way (does it read as a preference?) so that a
  high Noul keeps the direction every other gate has, "the gate acts". Null for a CONFIRMED
  finding or an unanswered question.

Exit 3 is no opinion and prints nothing: the gate is off, the model did not answer, or the input
is unusable. Shadow prints the same JSON as on: the caller records it, and the mode decides
whether it acts.

`decisions`, `score`, `fired` and `baseline` are what forge_decide.sites reads for this site.
Like every site's, `score` and `baseline` point at acting, here deselecting: `score` is the Noul
itself (the preference), one minus the output's `concrete`, and `baseline` is 1.0 for a finding
that looks like a preference. The threshold `findings_concrete` is on the `concrete` scale, so the
site is inverted (forge_decide.sites `invert`): replay flags a score at or above 1 - threshold.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import site_ready
from .client import ask, choice, noul, probabilities
from .questions import finding_concrete, finding_duplicate

SITE = 'jev-findings'
BODY_CHARS = 600

_CONCRETE = re.compile(r'c(\d+)')
_BASELINE_WORDS = re.compile(r'\b(?:when|if|returns|raises|crash|wrong)\b', re.I)


def _probability(value) -> float | None:
    """`value` as a probability: a finite number in [0, 1], else None (no opinion)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def _concreteness(preference) -> float | None:
    """P(concrete defect) from the Noul of `finding_concrete`, which asks whether the finding is
    a preference; None when it is not a probability."""
    p = _probability(preference)
    return None if p is None else 1.0 - p


def _text(value) -> str:
    return value if isinstance(value, str) else ''


def _line(item: dict) -> str:
    """The finding as one review line: `[P1][CONFIRMED] a.py:12 — text`."""
    return f"[{item['severity']}][{item['status']}] {item['location']} — {item['text']}"


def read_input(path) -> tuple[list[dict], dict, list[list[str]]] | None:
    """(findings, candidates {id: {title, body}}, candidate ids per finding), or None when the
    file is not the input of the module docstring. A finding without text or a fingerprint is
    not usable, so it makes the whole input unusable rather than shifting the indexes."""
    try:
        data = json.loads(Path(path).read_text(errors='replace'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('findings'), list):
        return None
    candidates: dict[str, dict] = {}
    raw = data.get('candidates')
    for item in raw if isinstance(raw, list) else ():
        if isinstance(item, dict) and item.get('id') not in (None, '') and not isinstance(item['id'], bool):
            candidates.setdefault(str(item['id']), dict(title=_text(item.get('title')),
                                                        body=_text(item.get('body'))[:BODY_CHARS]))
    findings, options = [], []
    for item in data['findings']:
        if not isinstance(item, dict) or not _text(item.get('fp')) or not _text(item.get('text')):
            return None
        findings.append(dict(i=len(findings), fp=item['fp'], severity=_text(item.get('severity')),
                             status=_text(item.get('status')), location=_text(item.get('location')),
                             text=item['text']))
        narrowed = item.get('candidates')
        ids = [str(c) for c in narrowed] if isinstance(narrowed, list) else list(candidates)
        options.append([c for c in dict.fromkeys(ids) if c in candidates])
    return findings, candidates, options


def _duplicate(result, qid: str, options: list[str]) -> tuple[str | None, float | None]:
    """(candidate id or None, probability) of a `d<i>` answer."""
    picked, confidence = choice(result, qid)
    if picked is None:
        return None, None
    spread = probabilities(result, qid)
    if picked != 'none':
        if picked not in options:
            return None, None
        return picked, _probability(spread.get(picked, confidence))
    rivals = [p for c, p in ((c, _probability(spread.get(c))) for c in options) if p is not None]
    return None, max(rivals) if rivals else 0.0


def main(args) -> int:
    if not site_ready(SITE):
        return 3
    parsed = read_input(args.input)
    if parsed is None:
        return 3
    findings, candidates, options = parsed
    if not findings:
        return 3

    state = dict(findings=findings,
                 candidates={cid: dict(title=c['title'], body=c['body']) for cid, c in candidates.items()})
    questions = {}
    for item, ids in zip(findings, options):
        if ids:
            questions[f"d{item['i']}"] = finding_duplicate(_line(item), {cid: candidates[cid]['title'] for cid in ids})
        if item['status'] == 'PLAUSIBLE':
            questions[f"c{item['i']}"] = finding_concrete(_line(item))
    if not questions:
        return 3
    result = ask(state, questions, site=SITE, run_dir=args.run_dir)
    if result is None:
        return 3

    out = []
    for item, ids in zip(findings, options):
        duplicate_of, dup_p = _duplicate(result, f"d{item['i']}", ids) if ids else (None, None)
        concrete = _concreteness(noul(result, f"c{item['i']}")) if item['status'] == 'PLAUSIBLE' else None
        out.append(dict(fp=item['fp'], duplicate_of=duplicate_of,
                        dup_p=dup_p, concrete=concrete))
    print(json.dumps(out, indent=2))
    return 0


# --- measurement: what forge_decide.sites reads for this site ---------------------------------

def _index(qid) -> int:
    found = _CONCRETE.fullmatch(str(qid))
    return int(found.group(1)) if found else -1


def decisions(body) -> list[str]:
    """The `c<i>` ids a request asked, in finding order."""
    questions = body.get('questions') if isinstance(body, dict) else None
    ids = list(questions) if isinstance(questions, (dict, list)) else []
    return sorted((str(qid) for qid in ids if _index(qid) >= 0), key=_index)


def score(answers, qid, request=None) -> float | None:
    """P(preference), the probability of deselecting: the Noul of a `c<i>` question (see the module
    docstring); None for any other id or an answer that is not a probability."""
    answer = (answers or {}).get(qid)
    if _index(qid) >= 0 and isinstance(answer, dict) and answer.get('type', 'noul') == 'noul':
        return _probability(answer.get('noul'))
    return None


def fired(answers, threshold_value, request=None) -> list[str]:
    """The `c<i>` whose concreteness is below the threshold: the findings the gate would deselect."""
    limit = _probability(threshold_value)
    if limit is None:
        return []
    out = []
    for qid in sorted((q for q in (answers or {}) if _index(q) >= 0), key=_index):
        p = score(answers, qid)
        if p is not None and 1.0 - p < limit:
            out.append(qid)
    return out


def baseline(body, qid) -> float:
    """0.0 when the finding's text has a digit, a backtick or one of when, if, returns, raises,
    crash, wrong (it looks concrete); else 1.0 (it looks like a preference): the plain rule the
    model's Noul is measured against. 0.0 for a question with no finding."""
    state = body.get('state') if isinstance(body, dict) else None
    findings = state.get('findings') if isinstance(state, dict) else None
    for item in findings if isinstance(findings, list) else ():
        if isinstance(item, dict) and item.get('i') == _index(qid):
            text = _text(item.get('text'))
            return 0.0 if re.search(r'\d|`', text) or _BASELINE_WORDS.search(text) else 1.0
    return 0.0
