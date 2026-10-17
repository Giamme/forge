#!/usr/bin/env python3
"""Advisory checks on a plan's tasks, printed under the approval table. Never fails a plan.

  forge-plan-check.py size --plan P
      Names the tasks that are large by the measure that predicts a failed first attempt, and
      the tasks whose prompt.md has no acceptance criteria. Prints nothing when every task is
      fine. Exit status is always 0.

Why these two: in forge's run history (269 first attempts, measured 2026-10-17) the share of
first attempts a reviewer passed falls steadily with prompt length (62% under 3K characters,
51% at 5-7K, 33% at 7-10K, 23% at 14K and over) and with declared files (68% at 2, 29% at 9
or more). Size is free to measure, and a smaller task with stated acceptance criteria is the
intervention the numbers point at, rather than a model's guess at difficulty.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from forge_jev.features import task_size  # noqa: E402

LARGE_CHARS = 7000
LARGE_FILES = 9
# (low, high, first-pass share, n) from the run dataset on 2026-10-17; see the module docstring.
CHAR_BINS = ((7000, 10000, 33, 49), (10000, 14000, 28, 29), (14000, None, 23, 44))
FILE_BIN = (9, 29, 107)
HEADING = re.compile(r'^(#{1,6})\s+acceptance criteria\s*:?\s*$', re.IGNORECASE)
ANY_HEADING = re.compile(r'^(#{1,6})\s')
FENCE = re.compile(r'^\s*(```|~~~)')


def has_acceptance_criteria(text: str) -> bool:
    """A complete `Acceptance criteria` heading outside fenced code, with content under it."""
    fenced = False
    level = None
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            if level is not None:
                return True        # a fenced block under the heading is content
            continue
        if fenced:
            continue
        if level is not None:
            heading = ANY_HEADING.match(line)
            if heading and len(heading.group(1)) <= level:
                level = None       # the section ended empty; another may follow
            elif line.strip():
                return True
        match = HEADING.match(line)
        if match:
            level = len(match.group(1))
    return False


def rows(plan: Path) -> list:
    out = []
    try:
        lines = (plan / 'tasks.tsv').read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        cells = line.split('\t')
        if line.startswith('#') or len(cells) < 7:
            continue
        out.append(dict(id=cells[0], deps=cells[1], files=cells[3]))
    return out


def size_notes(plan: Path) -> list:
    notes = []
    for row in rows(plan):
        try:
            prompt = (plan / 'tasks' / row['id'] / 'prompt.md').read_text(errors='replace')
        except OSError:
            continue
        size = task_size(prompt=prompt, declared_files=row['files'], plan_tasks=1, deps=row['deps'],
                         decompose_level=None)
        chars, files = size['prompt_chars'], size['declared_files']
        if chars >= LARGE_CHARS:
            low, high, share, n = next(b for b in CHAR_BINS if b[1] is None or chars < b[1])
            span = '%d-%dK' % (low // 1000, high // 1000) if high else '%dK and over' % (low // 1000)
            notes.append((row['id'], 'large prompt (%s characters): %d%% of first attempts at %s passed review '
                          'in forge\'s history (n=%d), against 56%% at 3-5K; consider splitting it'
                          % ('{:,}'.format(chars), share, span, n)))
        if files >= LARGE_FILES:
            notes.append((row['id'], 'touches %d declared files: %d%% of first attempts at %d or more files passed '
                          '(n=%d), against 68%% at 2; consider splitting it' % (files, FILE_BIN[1], FILE_BIN[0],
                                                                                FILE_BIN[2])))
        if not has_acceptance_criteria(prompt):
            notes.append((row['id'], 'no acceptance criteria: end prompt.md with an "## Acceptance criteria" '
                          'section of checks the reviewer can verify'))
    return notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge-plan-check.py')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('size')
    p.add_argument('--plan', required=True)
    args = ap.parse_args(argv)
    try:
        notes = size_notes(Path(args.plan))
    except Exception:
        return 0
    if notes:
        print('task size and acceptance criteria (advisory; the table is unchanged):')
        for task, text in notes:
            print('  %-14s %s' % (task, text))
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
