#!/usr/bin/env python3
"""Advisory checks on a plan's tasks, printed under the approval table. Never fails a plan.

  forge-plan-check.py size --plan P
      Names the tasks that are large by the measure that predicts a failed first attempt, the
      tasks whose prompt.md has no acceptance criteria, and the tasks whose acceptance criteria
      ask for the whole suite (each attempt would run it; the run's verification already does,
      after each merge). Prints nothing when every task is fine. Exit status is always 0.

  forge-plan-check.py gates PLAN
      Prints the decision-model gate block (what drift, forward, prompt and coverage did to this
      plan) from PLAN/plan-gates.json. Prints nothing when no gate recorded anything.

  forge-plan-check.py sendback PLAN
      Writes PLAN/plan.sendback.md from the items the gates marked in this `plan` call and prints
      it, exiting 4. Prints nothing and exits 0 when nothing was marked.

Both of the last two print nothing and exit 0 if forge_jev.plan_gates cannot be imported.

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
from forge_dataset.states import task_size  # noqa: E402

LARGE_CHARS = 7000
LARGE_FILES = 9
# (low, high, first-pass share, n) from the run dataset on 2026-10-17; see the module docstring.
CHAR_BINS = ((7000, 10000, 33, 49), (10000, 14000, 28, 29), (14000, None, 23, 44))
FILE_BIN = (9, 29, 107)
HEADING = re.compile(r'^(#{1,6})\s+acceptance criteria\s*:?\s*$', re.IGNORECASE)
ANY_HEADING = re.compile(r'^(#{1,6})\s')
FENCE = re.compile(r'^\s*(```|~~~)')
# A whole-suite run named as a command: forge's own check.sh, bare discovery, or an ecosystem's
# default test target with no selection. A command that names tests (`cargo test foo`) is fine.
WHOLE_SUITE = re.compile(
    r'(?:(?:ba)?sh\s+|\./)?[\w./-]*check\.sh'
    r'|python3?\s+-m\s+(?:unittest(?:\s+discover(?:\s+-s\s+\S+)?)?|pytest)|pytest|tox'
    r'|(?:npm|pnpm|yarn)(?:\s+run)?\s+test|make\s+(?:test|check)|cargo\s+test|go\s+test\s+\./\.\.\.')
COMMAND_SPAN = re.compile(r'`([^`\n]+)`')


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


def acceptance_lines(text: str) -> list:
    """The lines of the `Acceptance criteria` sections, fenced code included."""
    out, fenced, level = [], False, None
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if not fenced:
            heading = ANY_HEADING.match(line)
            if level is not None and heading and len(heading.group(1)) <= level:
                level = None
            match = HEADING.match(line)
            if match:
                level = len(match.group(1))
                continue
        if level is not None:
            out.append(line)
    return out


def verify_commands(plan: Path) -> set:
    """Each command of the plan's --verify and the repository's .forge/verify, whitespace collapsed."""
    sources = [plan / 'verify_cmd']
    try:
        sources.append(Path((plan / 'repo').read_text().strip()) / '.forge' / 'verify')
    except OSError:
        pass
    out = set()
    for source in sources:
        try:
            text = source.read_text(errors='replace')
        except OSError:
            continue
        for part in re.split(r'&&|\|\||;|\n', text):
            part = ' '.join(part.split())
            if len(part) >= 6:
                out.add(part)
    return out


def whole_suite_asked(prompt: str, configured: set):
    """The first whole-suite command the acceptance criteria name, or None."""
    for line in acceptance_lines(prompt):
        spans = COMMAND_SPAN.findall(line) or [line]
        for span in spans:
            command = ' '.join(span.split())
            if WHOLE_SUITE.fullmatch(command) or command in configured:
                return command
    return None


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
    configured = verify_commands(plan)
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
        command = whole_suite_asked(prompt, configured)
        if command:
            notes.append((row['id'], 'acceptance criteria ask for the whole suite (`%s`): every attempt would run '
                          'it; name the task\'s own tests instead, the run\'s verification runs the suite after '
                          'each merge' % command))
    return notes


def plan_gates():
    try:
        from forge_jev import plan_gates as module
    except Exception:
        return None
    return module


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge-plan-check.py')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('size')
    p.add_argument('--plan', required=True)
    for name in ('gates', 'sendback'):
        sub.add_parser(name).add_argument('plan')
    args = ap.parse_args(argv)
    if args.command in ('gates', 'sendback'):
        module = plan_gates()
        if module is None:
            return 0
        try:
            if args.command == 'gates':
                for line in module.marks(args.plan):
                    print(line)
            elif module.sendback(args.plan):
                sys.stdout.write((Path(args.plan) / module.SENDBACK_FILE).read_text())
                return 4
        except Exception:
            return 0
        return 0
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
