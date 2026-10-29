"""A retry touched its blocking findings (#43).

A retry implementer is handed the blocking findings of the review that rejected its
predecessor. Before the next review is paid for, this asks per CONFIRMED P0/P1 finding whether
the retry's diff (`changes.since.diff`, everything changed since the reviewed attempt) leaves it
unaddressed. The shell sends an attempt back, without a review, when a finding clearly is.

The gate can only send work back: it never accepts anything and never changes a verdict. It
asks nothing when there is nothing to compare (no delta), when the findings did not come from
a review (a bounced or pre-review retry has none to check) or when none of them blocks.

Prints one `u<i>TAB<p>TAB<finding>` row per finding at or above `untouched_warn`. Exit 3 is no
opinion: nothing to ask, the gate is off or in shadow, or the model did not answer.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from . import gate_mode, site_ready, threshold
from .client import ask, noul
from .questions import finding_untouched

SITE = 'jev-retry-addressed'
BLOCKING = ('P0', 'P1')
DIFF_CHARS = 40000

_issues = None
_INDEXED = re.compile(r'u(\d+)$')
_LOCATION = re.compile(r'(.+):\d+$')
_DIFF_FILE = re.compile(r'^\+\+\+ b/(.+)$', re.M)
_DIFF_GIT = re.compile(r'^diff --git a/.+ b/(.+)$', re.M)


def parse_findings(text: str) -> list[dict]:
    """forge-issues.py's `parse_findings`, loaded by path: its file name is not importable."""
    global _issues
    if _issues is None:
        path = Path(__file__).resolve().parent.parent / 'forge-issues.py'
        spec = importlib.util.spec_from_file_location('forge_issues', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _issues = module
    return _issues.parse_findings(text)


def blocking_findings(text: str) -> list[dict]:
    """The CONFIRMED P0/P1 findings of a findings text, in the order written."""
    return [f for f in parse_findings(text)
            if f.get('severity') in BLOCKING and f.get('confidence') == 'CONFIRMED']


def _location(finding: dict) -> str:
    return f"{finding['file']}:{finding['line']}" if finding.get('line') is not None else str(finding['file'])


def _read(path: Path) -> str:
    try:
        return path.read_text(errors='replace')
    except OSError:
        return ''


def _run_id(plan: Path) -> str:
    return _read(plan / 'run_id').strip() or plan.name


def main(args) -> int:
    if not site_ready(SITE):
        return 3
    plan = Path(args.plan)
    tdir = plan / 'tasks' / args.task
    diff = _read(tdir / 'changes.since.diff')
    if not diff.strip():
        return 3
    if _read(tdir / 'retry_findings.source').strip() != 'review':
        return 3
    findings = blocking_findings(_read(tdir / 'retry_findings.md'))
    if not findings:
        return 3

    state = dict(
        run=_run_id(plan), task=args.task, attempt=args.attempt,
        findings=[dict(i=i, severity=f['severity'], location=_location(f), text=f['text'])
                  for i, f in enumerate(findings)],
        diff=diff[:DIFF_CHARS] + ('\n[cut]' if len(diff) > DIFF_CHARS else ''))
    questions = {f'u{i}': finding_untouched(f['raw']) for i, f in enumerate(findings)}
    result = ask(state, questions, site=SITE, run_dir=str(tdir),
                 context=dict(task=args.task, attempt=args.attempt))
    if result is None or gate_mode(SITE) != 'on':
        return 3

    warn_at = threshold('untouched_warn', site=SITE, model=result.get('model'))
    rows = []
    for i, finding in enumerate(findings):
        probability = noul(result, f'u{i}')
        if probability is not None and probability >= warn_at:
            rows.append(f"u{i}\t{probability:.3f}\t{finding['raw']}")
    if not rows:
        return 3
    print('\n'.join(rows))
    return 0


# --- measurement: what forge_decide.sites reads for this site --------------------------------

def _index(qid) -> int:
    found = _INDEXED.match(str(qid))
    return int(found.group(1)) if found else -1


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def decisions(body) -> list[str]:
    """The `u<i>` ids a request asked, in finding order."""
    questions = body.get('questions') if isinstance(body, dict) else None
    ids = list(questions) if isinstance(questions, (dict, list)) else []
    return sorted((str(qid) for qid in ids if _index(qid) >= 0), key=_index)


def score(answers, qid, request=None) -> float | None:
    """The Noul of the question: the probability that the finding was left unaddressed."""
    answer = (answers or {}).get(qid)
    if isinstance(answer, dict) and answer.get('type', 'noul') == 'noul':
        return _number(answer.get('noul'))
    return None


def fired(answers, threshold_value, request=None) -> list[str]:
    """The questions at or above the threshold: the findings the gate would send back."""
    limit = _number(threshold_value)
    if limit is None:
        return []
    out = []
    for qid in sorted((q for q in (answers or {}) if _index(q) >= 0), key=_index):
        probability = score(answers, qid)
        if probability is not None and probability >= limit:
            out.append(qid)
    return out


def _state_finding(body, qid) -> dict:
    state = body.get('state') if isinstance(body, dict) else None
    findings = state.get('findings') if isinstance(state, dict) else None
    for item in findings if isinstance(findings, list) else []:
        if isinstance(item, dict) and item.get('i') == _index(qid):
            return item
    return {}


def finding_file(item: dict) -> str:
    """The file a state finding names: its location without the line."""
    location = str(item.get('location') or '')
    found = _LOCATION.match(location)
    return (found.group(1) if found else location).removeprefix('./')


def baseline(body, qid) -> float:
    """1.0 when the finding's file is not among the diff's files, else 0.0: the plain rule the
    model's score is measured against."""
    path = finding_file(_state_finding(body, qid))
    state = body.get('state') if isinstance(body, dict) else None
    diff = str(state.get('diff') or '') if isinstance(state, dict) else ''
    if not path:
        return 0.0
    touched = set(_DIFF_FILE.findall(diff)) | set(_DIFF_GIT.findall(diff))
    return 0.0 if path in touched else 1.0
