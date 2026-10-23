#!/usr/bin/env python3
"""Assemble context from explicit sources; never summarize or trim requirements."""
from __future__ import annotations

import argparse
from pathlib import Path

DIFF_INLINE_CAP = 100 * 1024
REPLY_CAP = 8 * 1024

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


def read(path: str | None, errors: str = 'strict') -> str:
    if path and Path(path).is_file():
        with Path(path).open(newline="", errors=errors) as source:
            return source.read()
    return ''


def carried(text: str) -> str:
    # The dataset and forge-ui take the LAST ```diff fence of a QA prompt as the reviewed
    # diff, so nothing carried from an earlier round may open one.
    return text.replace('```diff', '```text')


def qa_round(findings: str, diff: str | None, span: str | None, stat: str | None, reply: str | None) -> str:
    parts = ['## Findings from the previous review round\n' + carried(findings).rstrip('\n') + '\n\n' + QA_ROUND_NOTE]
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


def context(
    requirements: str, approach: str | None = None, capsule: str | None = None,
    retry: str | None = None, goal: str | None = None, memories: tuple[str, ...] = (),
    role: str = 'dwarf', retry_diff: str | None = None, retry_range: str | None = None,
    retry_stat: str | None = None, reply: str | None = None,
) -> str:
    chunks = []
    for title, path in [('Ownership and baseline', capsule), ('Overall goal', goal),
                        ('Complete implementation requirements', requirements),
                        ('Intended approach', approach),
                        ('Your previous attempt was reviewed and rejected', retry)]:
        data = read(path) if path and Path(path).is_file() else ''
        if data:
            if role == 'qa' and path == retry:
                chunks.append(qa_round(data, retry_diff, retry_range, retry_stat, reply).rstrip('\n'))
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
    args = vars(parser.parse_args())
    args['memories'] = args.pop('memory')
    print(context(**args), end='')
