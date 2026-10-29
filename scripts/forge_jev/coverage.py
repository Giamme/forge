"""Plan covers the request (#47): which requirement of the request no task's criteria cover.

A decomposed run can pass every task and still miss part of what the user asked for. A
requirement dropped while decomposing belongs to no task, so nobody builds it and no reviewer
checks for it. Forge keeps what is needed to ask before dispatch: the request verbatim in
`request.md`, and each task's `## Acceptance criteria` in its `prompt.md`.

`split_requirements` cuts the request into requirements, deterministically. `main` asks, for
each, which task's criteria deliver it (site `jev-coverage`, rubric `requirement_coverage`);
`none` at or above `coverage_warn` means uncovered. The result goes into `plan-gates.json`
(`coverage`, replaced on every call). In `on` each uncovered requirement is also marked pending
for `plan_gates.sendback`, which `plan` runs once after every gate; in `shadow` nothing is.

`decisions`, `score`, `fired` and `baseline` are what forge_decide.sites reads for this site.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from . import gate_mode, load_config, site_ready, threshold
from .client import ask, choice, probabilities
from .questions import requirement_coverage

SITE = 'jev-coverage'
MAX_REQUIREMENTS = 30
MAX_TASKS = 40
MIN_WORDS = 4
REQUEST_CHARS = 6000
CRITERIA_CHARS = 1200

_FENCE = re.compile(r'^\s*(`{3,}|~{3,})(.*)$')
_HEADING = re.compile(r'^\s{0,3}#{1,6}(\s|$)')
_ITEM = re.compile(r'^\s*(?:[-*+]|\d+[.)])\s+(.*)$')
_SENTENCE_END = re.compile(r'(?<=[.?!])\s+(?=[A-Z])')
# The same patterns forge-plan-check.py `has_acceptance_criteria` reads sections with.
_CRITERIA_HEADING = re.compile(r'^(#{1,6})\s+acceptance criteria\s*:?\s*$', re.IGNORECASE)
_ANY_HEADING = re.compile(r'^(#{1,6})\s')
_WORD = re.compile(r'[a-z]{4,}')
_QID = re.compile(r'r(\d+)')


def _fence(line, open_fence):
    """The fence state after `line`: None outside a fence, else (marker char, length). A fence
    closes only on a marker of the same character, at least as long, with nothing after it, so a
    shorter or different marker inside a block is content."""
    found = _FENCE.match(line)
    if not found:
        return open_fence
    marker, rest = found.group(1), found.group(2)
    if open_fence is None:
        return None if marker[0] == '`' and '`' in rest else (marker[0], len(marker))
    if marker[0] == open_fence[0] and len(marker) >= open_fence[1] and not rest.strip():
        return None
    return open_fence


def _collapse(text) -> str:
    return ' '.join(str(text).split())


def sha(text) -> str:
    """The key a requirement goes by in plan-gates.json: sha1 of its whitespace-collapsed text."""
    return hashlib.sha1(_collapse(text).encode('utf-8', 'replace')).hexdigest()


def content_words(text) -> set[str]:
    """The lower-case words of four or more letters."""
    return set(_WORD.findall(str(text).lower()))


# --- the request -------------------------------------------------------------------------

def split_requirements(text) -> list[str]:
    """The requirements of a request, in order, at most MAX_REQUIREMENTS.

    A bullet or numbered item is one requirement, continuation lines joined. The other
    paragraphs split into sentences at `. `, `? ` and `! ` before an uppercase letter. Fenced
    code, headings and anything under four words are dropped; a duplicate (after whitespace
    collapse) is dropped, keeping the first.
    """
    found: list[str] = []
    items: list[str] = []      # lines of the bullet being read
    prose: list[str] = []      # lines of the paragraph being read

    def flush():
        if items:
            found.append(' '.join(items))
        if prose:
            found.extend(_SENTENCE_END.split(' '.join(prose)))
        items.clear()
        prose.clear()

    fence = None
    for line in str(text or '').splitlines():
        after = _fence(line, fence)
        if fence is not None or after is not None:
            flush()
            fence = after
            continue
        if not line.strip() or _HEADING.match(line):
            flush()
            continue
        item = _ITEM.match(line)
        if item:
            flush()
            items.append(item.group(1).strip())
        elif items:
            items.append(line.strip())
        else:
            prose.append(line.strip())
    flush()
    out, seen = [], set()
    for candidate in found:
        candidate = _collapse(candidate)
        if len(candidate.split()) < MIN_WORDS or candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
        if len(out) == MAX_REQUIREMENTS:
            break
    return out


def acceptance_criteria(prompt_text) -> str:
    """The body of the first non-empty `## Acceptance criteria` section, up to the next heading
    of the same or a higher level, at most CRITERIA_CHARS. Headings inside fences do not count
    (the rule of forge-plan-check.py), but fenced lines under the heading are part of the body."""
    fence = None
    level = None
    body: list[str] = []
    for line in str(prompt_text or '').splitlines():
        after = _fence(line, fence)
        if fence is not None or after is not None:
            fence = after
            if level is not None:
                body.append(line)
            continue
        heading = _ANY_HEADING.match(line)
        if level is not None and heading and len(heading.group(1)) <= level:
            text = '\n'.join(body).strip()
            if text:
                return text[:CRITERIA_CHARS]
            level, body = None, []
        elif level is not None:
            body.append(line)
            continue
        match = _CRITERIA_HEADING.match(line)
        if match:
            level, body = len(match.group(1)), []
    return '\n'.join(body).strip()[:CRITERIA_CHARS] if level is not None else ''


def _tasks(plan: Path) -> list[dict]:
    """[{id, title, acceptance_criteria}] from tasks.tsv and each task's prompt.md."""
    try:
        lines = (plan / 'tasks.tsv').read_text(errors='replace').splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        if len(cells) < 7 or not cells[0]:
            continue
        try:
            prompt = (plan / 'tasks' / cells[0] / 'prompt.md').read_text(errors='replace')
        except OSError:
            prompt = ''
        out.append(dict(id=cells[0], title=cells[6], acceptance_criteria=acceptance_criteria(prompt)))
    return out


# --- the measurement functions forge_decide.sites reads ----------------------------------

def _index(qid) -> int:
    found = _QID.fullmatch(str(qid))
    return int(found.group(1)) if found else -1


def decisions(body) -> list[str]:
    """The `r<i>` question ids of a stored request body, in index order."""
    questions = body.get('questions') if isinstance(body, dict) else None
    return sorted((q for q in questions or () if _index(q) >= 0), key=_index)


def _probability(value) -> float | None:
    """`value` as a probability: a finite number in [0, 1], else None. A malformed score is
    no opinion, so the gate fails open instead of sending a requirement back."""
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


def _p_none(answers, qid) -> float | None:
    """P(`none`) of a question: the Choice's distribution, else its confidence when it chose
    `none`, else 0.0 for a choice of a task. None when the question has no choice answer or
    its score is not a probability."""
    if not isinstance(answers, dict) or not isinstance(answers.get(qid), dict):
        return None
    result = dict(answers=answers)
    found = probabilities(result, qid)
    if 'none' in found:
        return _probability(found['none'])
    picked, confidence = choice(result, qid)
    if picked is None:
        return None
    if picked != 'none':
        return 0.0
    return _probability(confidence)


def score(answers, qid, request=None) -> float | None:
    return _p_none(answers, qid)


def fired(answers, threshold_value, request=None) -> list[str]:
    """The questions whose requirement is uncovered: P(`none`) at or above the threshold."""
    out = []
    for qid in sorted((q for q in answers or () if _index(q) >= 0), key=_index):
        p = _p_none(answers, qid)
        if p is not None and p >= threshold_value:
            out.append(qid)
    return out


def baseline(body, qid) -> float:
    """The share of the requirement's content words that no task's criteria use: the cheap
    guess the gate must beat. Higher = more likely uncovered."""
    state = body.get('state') if isinstance(body, dict) else None
    state = state if isinstance(state, dict) else {}
    requirements = state.get('requirements')
    i = _index(qid)
    if not isinstance(requirements, list) or not 0 <= i < len(requirements):
        return 0.0
    words = content_words(requirements[i])
    if not words:
        return 0.0
    used = set()
    for task in state.get('tasks') or ():
        if isinstance(task, dict):
            used |= content_words(task.get('acceptance_criteria') or '')
    return len(words - used) / len(words)


# --- the gate ----------------------------------------------------------------------------

def main(args) -> int:
    """`forge-jev.py coverage --plan P --repo R`. Exit 3 is no opinion: skipped, shadow, or
    nothing uncovered; otherwise one `r<i><TAB>p<TAB>text` row per uncovered requirement."""
    plan = Path(args.plan)
    config = load_config()
    if not site_ready(SITE, config):
        return 3
    try:
        request = (plan / 'request.md').read_text(errors='replace')
    except OSError:
        return 3
    requirements = split_requirements(request)
    tasks = _tasks(plan)
    if len(requirements) < 2 or not tasks or len(tasks) > MAX_TASKS:
        return 3

    ids = [t['id'] for t in tasks]
    state = dict(request=request[:REQUEST_CHARS], requirements=requirements, tasks=tasks)
    questions = {f'r{i}': requirement_coverage(text, ids) for i, text in enumerate(requirements)}
    result = ask(state, questions, site=SITE, run_dir=str(plan), config=config)
    if result is None:
        return 3

    warn_at = threshold('coverage_warn', config=config, site=SITE, model=result.get('model'))
    shadow = gate_mode(SITE) == 'shadow'
    uncovered = []
    for i, text in enumerate(requirements):
        p = _p_none(result['answers'], f'r{i}')
        if p is not None and p >= warn_at:
            uncovered.append(dict(index=i, text=text, sha=sha(text), p=round(p, 3), shadow=shadow))

    from . import plan_gates
    data = plan_gates.load(plan)
    data['coverage'] = dict(requirements=requirements,
                            uncovered=[{k: u[k] for k in ('text', 'sha', 'p', 'shadow')} for u in uncovered])
    if not shadow:
        for item in uncovered:
            plan_gates.mark_pending(data, 'coverage', item['sha'])
    plan_gates.save(plan, data)

    if shadow or not uncovered:
        return 3
    if getattr(args, 'json', False):
        print(json.dumps([dict(id=f"r{u['index']}", p=u['p'], text=u['text']) for u in uncovered], indent=2))
    else:
        for item in uncovered:
            print('\t'.join((f"r{item['index']}", str(item['p']), item['text'])))
    return 0
