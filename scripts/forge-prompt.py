#!/usr/bin/env python3
"""Assemble context from explicit sources; never summarize or trim requirements."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

DIFF_INLINE_CAP = 100 * 1024
REPLY_CAP = 8 * 1024
# A brief answer, and each brief section, stay small enough to leave the task itself
# in the prompt. The cut matches the reply cap: keep the leading bytes, then a marker.
ANSWER_CAP = 4 * 1024
BRIEF_CHUNK_CAP = 16 * 1024

# What a reviewer is asked to do with the previous round's report. The labelled-line rule
# matters: the severity gate, `forge issues` and forge-ui count every `- [Px][...]` line as open.
QA_ROUND_NOTE = """\
Check each finding above first: is it fixed, still present, or validly rejected by the
implementer (see their reply below)? Report a finding that is still present again, in the
usual labelled format with its severity. Mention a fixed or rejected finding only in a plain
sentence, never as a `- [Px][CONFIRMED]` or `- [Px][PLAUSIBLE]` line: every labelled line is
counted as an open finding. This round's threshold, in the contract below, governs; it may
differ from the last round's. Then review the full task diff below: the changes since that
round are where new problems are most likely."""


# The dwarf's heading for the retry findings, by where they came from (retry_findings.source).
# An unknown word reads as a review, as a plan from before the source existed does.
RETRY_HEADINGS = {
    'review': 'Your previous attempt was reviewed and rejected',
    'prereview': "Your previous attempt failed forge's tests before any review",
    'bounce': 'Your previous attempt left blocking review findings unaddressed',
    'message': 'Your previous attempt said it did not finish',
    'answer': 'The user answered your question',
}


def read(path: str | None, errors: str = 'strict') -> str:
    if path and Path(path).is_file():
        with Path(path).open(newline="", errors=errors) as source:
            return source.read()
    return ''


def carried(text: str) -> str:
    # The dataset and forge-ui take the LAST ```diff fence of a QA prompt as the reviewed
    # diff, so nothing carried from an earlier round may open one.
    return text.replace('```diff', '```text')


def qa_round(findings: str, diff: str | None, span: str | None, stat: str | None, reply: str | None,
             source: str = 'review') -> str:
    if source == 'review':
        head = '## Findings from the previous review round\n'
    else:
        head = ('## forge sent the previous attempt back without a review (' + source
                + '); what it was told:\n')
    parts = [head + carried(findings).rstrip('\n') + '\n\n' + QA_ROUND_NOTE]
    # A git diff carries raw source bytes, so a file that is not UTF-8 must not abort the prompt.
    patch = read(diff, 'replace')
    if patch:
        label = ' (' + span.strip() + ')' if span and span.strip() else ''
        if len(patch.encode()) <= DIFF_INLINE_CAP:
            body = '```patch\n' + patch.rstrip('\n') + '\n```'
        else:
            body = ('The diff since that review is too large to inline; its stat:\n\n```text\n'
                    + read(stat, 'replace').rstrip('\n') + '\n```')
        parts.append('### Changes since that review' + label + '\n' + body)
    said = read(reply, 'replace')
    if said.strip():
        raw = said.encode()
        if len(raw) > REPLY_CAP:
            said = raw[:REPLY_CAP].decode(errors='ignore') + '\n[... the reply continues; cut here ...]\n'
        parts.append("### The implementer's reply (its own account; verify every claim against the code)\n"
                     + carried(said).rstrip('\n'))
    return '\n\n'.join(parts) + '\n'


# Brief is opt-in. `<plan>/brief` names the level and `<plan>/tasks/<id>/brief.json` holds
# the questions. Answers given before the task starts live in `brief.answers.json`.
# `brief.late.json` is written after the start and is never read here. No level file, no
# brief.json, or no questions: nothing is added, and the prompt stays byte-identical.
DECISIONS_TITLE = 'Decisions the user made'
OPEN_TITLE = 'Open questions: follow the stated default'
DECISIONS_NOTE = (
    'Treat these as constraints that override anything in the requirements or approach '
    'they contradict.'
)
DECISIONS_QA = (
    "These are the user's own decisions, so following them is not a deviation and must "
    'not be reported as a finding.'
)
OPEN_NOTE = (
    'Follow the default for each question below, and say in the final message which '
    'defaults were used.'
)
OPEN_QA = (
    'These are the defaults the implementer was told to follow. The implementer was told '
    'to say in the final message which defaults were used.'
)
OPEN_LEVELS = ('advisory', 'gated')


def clip(text: str, limit: int, marker: str) -> str:
    """Keep the leading bytes. A cut keeps the marker, as a long reply does."""
    raw = text.encode()
    if len(raw) <= limit:
        return text
    return raw[:limit].decode(errors='ignore') + marker


def load_json(path: Path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def task_dir_of(requirements: str | None, approach: str | None) -> Path | None:
    """`<plan>/tasks/<id>`, from the prompt.md or approach.md path callers already pass.

    A solo run and a fixture that is not that shape have no plan brief, so they get nothing.
    """
    found = None
    for path in (requirements, approach):
        if not path:
            continue
        candidate = Path(path)
        if candidate.name not in ('prompt.md', 'approach.md'):
            continue
        # candidate = <plan>/tasks/<id>/<file>
        if candidate.parent.parent.name != 'tasks':
            continue
        if candidate.name == 'prompt.md':
            return candidate.parent
        found = candidate.parent
    return found


def brief_rows(questions: list, answers: dict) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    decided, pending = [], []
    for question in questions:
        if not isinstance(question, dict):
            continue
        qid, text = question.get('id'), question.get('text')
        if not isinstance(qid, str) or not qid or not isinstance(text, str) or not text.strip():
            continue
        entry = answers.get(qid)
        answer = entry.get('text') if isinstance(entry, dict) else None
        if isinstance(answer, str):
            decided.append((text.strip(), clip(answer, ANSWER_CAP,
                                                '\n[... the answer continues; cut here ...]')))
        else:
            default = question.get('default')
            pending.append((text.strip(), default if isinstance(default, str) else ''))
    return decided, pending


def brief_section(title: str, note: str, rows: list[tuple[str, str]], label: str) -> str:
    items = '\n\n'.join('- ' + text + '\n  ' + label + ': ' + value for text, value in rows)
    # Trailing newline matches the other sections, whose source files end with one.
    body = '## ' + title + '\n' + note + '\n\n' + items + '\n'
    return clip(body, BRIEF_CHUNK_CAP, '\n[... the brief continues; cut here ...]\n')


def brief_chunks(requirements: str | None, approach: str | None, role: str) -> list[str]:
    task = task_dir_of(requirements, approach)
    if task is None:
        return []
    plan = task.parent.parent
    if not (plan / 'brief').is_file() or not (task / 'brief.json').is_file():
        return []
    spec = load_json(task / 'brief.json')
    questions = spec.get('questions') if isinstance(spec, dict) else None
    if not isinstance(questions, list) or not questions:
        return []
    answers = load_json(task / 'brief.answers.json')
    decided, pending = brief_rows(questions, answers if isinstance(answers, dict) else {})
    chunks = []
    if decided:
        note = DECISIONS_NOTE + (' ' + DECISIONS_QA if role == 'qa' else '')
        chunks.append(brief_section(DECISIONS_TITLE, note, decided, 'Answer'))
    # strict has no open questions: run refuses to start until every one is answered.
    if pending and read(str(plan / 'brief')).strip() in OPEN_LEVELS:
        note = OPEN_QA if role == 'qa' else OPEN_NOTE
        chunks.append(brief_section(OPEN_TITLE, note, pending, 'Default'))
    return chunks


def context(
    requirements: str, approach: str | None = None, capsule: str | None = None,
    retry: str | None = None, goal: str | None = None, memories: tuple[str, ...] = (),
    role: str = 'dwarf', retry_diff: str | None = None, retry_range: str | None = None,
    retry_stat: str | None = None, reply: str | None = None, retry_source: str = 'review',
) -> str:
    if retry_source not in RETRY_HEADINGS:
        retry_source = 'review'
    chunks = []
    for title, path in [('Ownership and baseline', capsule), ('Overall goal', goal),
                        ('Complete implementation requirements', requirements),
                        ('Intended approach', approach),
                        (RETRY_HEADINGS[retry_source], retry)]:
        if title == RETRY_HEADINGS[retry_source]:
            chunks.extend(brief_chunks(requirements, approach, role))
        data = read(path) if path and Path(path).is_file() else ''
        if data:
            if role == 'qa' and path == retry:
                chunks.append(qa_round(data, retry_diff, retry_range, retry_stat, reply, retry_source).rstrip('\n'))
            else:
                chunks.append('## ' + title + '\n' + data)
    # Memory injection emits headings and prose around bullet facts. Union only
    # exact fact lines, preserving distinct facts even when they share a key.
    seen = set()
    facts = []
    for path in memories:
        if Path(path).is_file():
            for line in Path(path).read_text().splitlines():
                if line.startswith('- ') and line not in seen:
                    seen.add(line)
                    facts.append(line)
    if facts:
        chunks.append('## Project memory (context from earlier runs)\n' + '\n'.join(facts))
    return '\n\n'.join(chunks) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for key in ('requirements', 'approach', 'capsule', 'retry', 'goal'):
        parser.add_argument('--' + key, required=key == 'requirements')
    parser.add_argument('--memory', action='append', default=[])
    parser.add_argument('--role', choices=('dwarf', 'qa'), default='dwarf')
    parser.add_argument('--retry-diff')
    parser.add_argument('--retry-range')
    parser.add_argument('--retry-stat')
    parser.add_argument('--reply')
    parser.add_argument('--retry-source', default='review')
    args = vars(parser.parse_args())
    args['memories'] = args.pop('memory')
    print(context(**args), end='')
