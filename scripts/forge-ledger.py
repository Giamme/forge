#!/usr/bin/env python3
"""The findings ledger (GitHub #69, part 1): what each task's reviews found, and whether they converge.

A task can fail review round after round, each round with new findings, and nothing in forge
notices. This keeps one row per review of a task in tasks/<id>/ledger.tsv and computes a signal
over the last rows. A later change wires the signal into the runner; this file is the measurement.

  append <plan> <id>
      Appends the row of the task's latest review with a PASS or FAIL verdict. A review already in
      the ledger is not added twice. Exits 0 when there is nothing to append.
  signal <task-dir> [--window N] [--share S]
      Prints `fire<TAB><summary>` and exits 0 when the signal fires; exits 1 (printing nothing) when not.
  replay <plan-dir> [--window N]
      Rebuilds every task's rows from its review dirs, in order, and prints them with the row where
      the signal would first fire. Writes nothing. For calibration, and for runs recorded before
      the ledger existed. Unlike the runner it does not apply decisions.tsv.

The window defaults to <plan>/converge_window when that file exists, else 4. Stdlib only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

COLUMNS = ('epoch', 'attempt', 'review', 'verdict', 'gate', 'worst', 'confirmed', 'new', 'carried',
           'diff_bytes', 'delta_bytes')
DEFAULT_WINDOW = 4
DEFAULT_SHARE = 0.5
JACCARD = 0.5

_VERDICT = re.compile(r'^FORGE_VERDICT: (PASS|FAIL)([ \t]+(—|–|-)( .*)?)?[ \t]*$')
# What a finding block leads with before its location: the bullet, then tags such as `[P1]`,
# `[CONFIRMED]`, `P1:` or `(P2)`. A bracketed or parenthesised tag never holds a line number, so a
# location wrapped in brackets is not taken for a tag.
_TAGS = re.compile(r'(?:\s*(?:\[(?![^\]\n]*:\d)[^\]\n]*\]|\((?![^)\n]*:\d)[^)\n]*\)|[-+•–]|\d{1,3}[.)]\s|#{1,6}'
                   r'|P[0-3](?![\w/.\\-])[:.]?|CONFIRMED\b|PLAUSIBLE\b|UNSTATED\b))*\s*')
# Markdown emphasis and code marks are never part of a path: `**src/a.py:10**` names src/a.py.
_MARKUP = re.compile(r'[*`]')
# The file of a finding's location: everything up to the `:line` that follows it. A path may hold spaces
# (`src/first dir/cache.py:10`), so it is not cut at the first space; it stops before a ` — ` or `: `.
_LOCATION = re.compile(r'((?:(?!\s[—–-]\s|:\s).)+?):\d+')
_LEADING = re.compile(r'^[^\w./\\-]+')
_EXTENSION = re.compile(r'\.\w+$')
_WORD = re.compile(r'[a-z]{4,}')


def _load_contract():
    """forge-contract.py, loaded by path: its file name is not importable (forge-issues.py does the same)."""
    path = Path(__file__).resolve().parent / 'forge-contract.py'
    spec = importlib.util.spec_from_file_location('forge_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = _load_contract()


# --- matching findings -----------------------------------------------------------

def _file(finding):
    """The file a finding names, from its first line; None when the finding names no file."""
    first = _MARKUP.sub('', finding.text.split('\n', 1)[0])
    match = _LOCATION.match(first, _TAGS.match(first).end())
    if match is None:
        return None
    path = _LEADING.sub('', match.group(1)).removeprefix('./')
    return path if _is_file(path) else None


def _is_file(path: str) -> bool:
    """A path names a file when its last part has an extension (`cache.py`) or has no space (`Dockerfile`,
    `one/Makefile`). A last part with a space is prose that happens to end in `:digits`, as in `runs 3:30`."""
    last = re.split(r'[/\\]', path)[-1]
    return bool(last) and (bool(_EXTENSION.search(last)) or ' ' not in last)


def _words(finding) -> set:
    # #119: the facts suffix's words (reach, origin, evidence, ...) would make any two findings look alike
    text = '\n'.join(contract.split_facts(line)[0] for line in finding.text.split('\n'))
    return set(_WORD.findall(text.lower()))


def same_finding(a, b) -> bool:
    """The same problem, reworded: the same file (two unlocated findings count), and content words
    that overlap by at least JACCARD. Both arguments are Findings from forge-contract.parse_findings."""
    if _file(a) != _file(b):
        return False
    ours, theirs = _words(a), _words(b)
    union = ours | theirs
    return bool(union) and len(ours & theirs) / len(union) >= JACCARD


def verdict_of(text: str) -> str:
    """forge_verdict (forge-artifact.sh) in Python: only a standalone last line counts."""
    lines = [line for line in text.splitlines() if line.strip()]
    match = _VERDICT.match(lines[-1].rstrip('\r')) if lines else None
    return match.group(1) if match else 'UNKNOWN'


def _confirmed(text: str) -> list:
    return [finding for finding in contract.parse_findings(text) if finding.status == 'CONFIRMED']


# --- reading a task dir ----------------------------------------------------------

def _read(path: Path):
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _int(text):
    return int(text.strip()) if text is not None and text.strip().isdigit() else None


def _size(path: Path):
    return path.stat().st_size if path.is_file() else '-'


def _started(review: Path) -> float:
    started = _json(review / 'metrics.json').get('wall_started')
    if isinstance(started, (int, float)):
        return float(started)
    return (review / 'qa.last').stat().st_mtime


def _attempt_rows(task_dir: Path) -> list:
    rows = []
    for line in (_read(task_dir / 'attempts.tsv') or '').splitlines():
        fields = line.split('\t')
        if len(fields) >= 2 and fields[0].isdigit() and fields[1].isdigit():
            rows.append((int(fields[0]), int(fields[1])))
    return rows


def _attempt_of(task_dir: Path, started: float) -> int:
    """The attempt a review belongs to. A review dir records no attempt number, but attempts.tsv
    gets one row per attempt, written when the attempt ends. So the first row written at or after
    the review started is its attempt. The row's epoch is whole seconds (date +%s), but the review's
    start is compared exactly: a review that starts at 100.9 after an attempt recorded at 100 is the
    next attempt, not that one. A review after the last recorded attempt (still running, or not yet
    recorded) is the attempt the counter holds. A QA-only resume after an infrastructure stop maps
    to the attempt after the stopped one: the mapping is approximate there."""
    for epoch, attempt in _attempt_rows(task_dir):
        if started <= epoch:
            return attempt
    return _int(_read(task_dir / 'attempt')) or 0


def _gate(task_dir: Path) -> str:
    """The decision column of qa.gate: the latest review's gate, or `-`."""
    fields = (_read(task_dir / 'qa.gate') or '').split('\n', 1)[0].split('\t')
    return fields[1] if len(fields) > 1 and fields[1] else '-'


def review_dirs(task_dir: Path) -> list:
    """The task's review dirs (attempts/qa-<uuid>/) with a PASS or FAIL verdict, oldest first."""
    found = []
    for review in (task_dir / 'attempts').glob('qa-*'):
        text = _read(review / 'qa.last')
        if text is not None and verdict_of(text) != 'UNKNOWN':
            found.append(review)
    return sorted(found, key=lambda review: (_started(review), review.name))


def review_row(task_dir: Path, review: Path, previous, gate: str = '-') -> dict:
    """One ledger row for a review. `previous` is the review before it, or None for the first.

    A CONFIRMED finding is carried when a CONFIRMED finding of the previous review matches it
    (same_finding); each previous finding carries at most one. Otherwise it is new. The first
    review's findings are all new. diff_bytes and delta_bytes are the task's current changes.diff
    and changes.since.diff, so they are the reviewed diff for the latest review only.
    """
    text = _read(review / 'qa.last') or ''
    confirmed = _confirmed(text)
    unused = _confirmed(_read(previous / 'qa.last') or '') if previous is not None else []
    carried = 0
    for finding in confirmed:
        hit = next((i for i, other in enumerate(unused) if same_finding(finding, other)), None)
        if hit is not None:
            del unused[hit]
            carried += 1
    labelled = [finding.severity for finding in confirmed if finding.severity is not None]
    started = _started(review)
    return dict(
        epoch=int(started), attempt=_attempt_of(task_dir, started), review=review.name,
        verdict=verdict_of(text), gate=gate, worst='P%d' % min(labelled) if labelled else '-',
        confirmed=len(confirmed), new=len(confirmed) - carried, carried=carried,
        diff_bytes=_size(task_dir / 'changes.diff'), delta_bytes=_size(task_dir / 'changes.since.diff'))


def task_rows(task_dir: Path) -> list:
    """Every review row of a task, rebuilt from its review dirs in order."""
    reviews = review_dirs(task_dir)
    rows = []
    for index, review in enumerate(reviews):
        previous = reviews[index - 1] if index else None
        gate = _gate(task_dir) if index == len(reviews) - 1 else '-'
        rows.append(review_row(task_dir, review, previous, gate))
    return rows


def _value(text: str):
    return int(text) if text.isdigit() else text


def read_ledger(path: Path) -> list:
    rows = []
    for line in (_read(path) or '').splitlines():
        fields = line.split('\t')
        if len(fields) == len(COLUMNS):
            rows.append({name: _value(text) for name, text in zip(COLUMNS, fields)})
    return rows


def format_row(row: dict) -> str:
    return '\t'.join(str(row[name]) for name in COLUMNS)


def after_decision(rows: list, decisions_path: Path) -> list:
    """The rows whose attempt is later than the last attempt in decisions.tsv (epoch, attempt,
    choice, reason). A decision restarts the count: with no decisions file, every row counts."""
    decided = []
    for line in (_read(Path(decisions_path)) or '').splitlines():
        fields = line.split('\t')
        if len(fields) > 1 and fields[1].isdigit():
            decided.append(int(fields[1]))
    if not decided:
        return list(rows)
    last = max(decided)
    return [row for row in rows if row['attempt'] > last]


# --- the signal ------------------------------------------------------------------

def _failed(row: dict) -> bool:
    return row['verdict'] == 'FAIL' or row['gate'] == 'fail'


def _severity(worst: str) -> int:
    """P0 is the most severe. An unlabelled CONFIRMED finding blocks at every threshold, so `-` ranks as P0."""
    return 0 if worst == '-' else int(worst[1:])


def converging(rows: list, window: int = DEFAULT_WINDOW, share: float = DEFAULT_SHARE):
    """Whether the last `window` reviews are a moving target, and a one-line summary either way.

    It fires when every one of those reviews failed (verdict FAIL, or the gate turned it into a
    fail) with at least one CONFIRMED finding; when the findings of reviews 2..window are new at
    least `share` of the time; and when the last review's worst severity is no better than the
    first's. Window 0 never fires, and neither does window 1: there is nothing to compare.
    """
    if window < 2:
        return False, 'window %d never fires' % window
    if len(rows) < window:
        return False, 'fewer than %d reviews' % window
    recent = rows[-window:]
    if not all(_failed(row) and row['confirmed'] for row in recent):
        return False, 'a review in the window passed, or had no CONFIRMED finding'
    later = recent[1:]
    new = sum(row['new'] for row in later)
    confirmed = sum(row['confirmed'] for row in later)
    first, last = recent[0]['worst'], recent[-1]['worst']
    if new / confirmed < share:
        return False, '%d of %d findings new' % (new, confirmed)
    if _severity(last) > _severity(first):
        return False, 'worst improved %s → %s' % (first, last)
    return True, '%d failed reviews in a row, %d of %d findings new, worst %s → %s' % (
        window, new, confirmed, first, last)


def first_fire(rows: list, window: int, share: float):
    """(row count, summary) at the first row where the signal fires, or None."""
    for count in range(1, len(rows) + 1):
        fired, summary = converging(rows[:count], window, share)
        if fired:
            return count, summary
    return None


# --- command line ----------------------------------------------------------------

def _window(plan: Path, given) -> int:
    if given is not None:
        return given
    text = _read(plan / 'converge_window')
    try:
        return int(text) if text is not None else DEFAULT_WINDOW
    except ValueError:
        return DEFAULT_WINDOW


def _append(plan: Path, task_id: str) -> int:
    task_dir = plan / 'tasks' / task_id
    reviews = review_dirs(task_dir) if task_dir.is_dir() else []
    if not reviews:
        return 0
    latest = reviews[-1]
    ledger = task_dir / 'ledger.tsv'
    if any(row['review'] == latest.name for row in read_ledger(ledger)):
        return 0
    previous = reviews[-2] if len(reviews) > 1 else None
    row = review_row(task_dir, latest, previous, _gate(task_dir))
    with ledger.open('a', encoding='utf-8') as handle:
        handle.write(format_row(row) + '\n')
    return 0


def _signal(task_dir: Path, window, share: float) -> int:
    task_dir = task_dir.resolve()
    rows = after_decision(read_ledger(task_dir / 'ledger.tsv'), task_dir / 'decisions.tsv')
    fired, summary = converging(rows, _window(task_dir.parent.parent, window), share)
    if not fired:
        return 1
    print('fire\t' + summary)
    return 0


def _replay(plan: Path, window) -> int:
    if not plan.is_dir():
        sys.stderr.write('forge-ledger: no plan directory %s\n' % plan)
        return 2
    size = _window(plan, window)
    for task_dir in sorted(path for path in (plan / 'tasks').glob('*') if path.is_dir()):
        rows = task_rows(task_dir)
        if not rows:
            continue
        fire = first_fire(rows, size, DEFAULT_SHARE)
        print(task_dir.name)
        print('  attempt  verdict  gate    confirmed  new  worst')
        for count, row in enumerate(rows, 1):
            line = '  %7d  %-7s  %-6s  %9d  %3d  %s' % (
                row['attempt'], row['verdict'], row['gate'], row['confirmed'], row['new'], row['worst'])
            if fire is not None and count == fire[0]:
                line += '  <- signal fires: ' + fire[1]
            print(line)
        if fire is None:
            print('  the signal does not fire')
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='forge-ledger.py', formatter_class=argparse.RawDescriptionHelpFormatter,
        description='The findings ledger of a task, and the signal for reviews that do not converge.',
        epilog='The signal fires when the last N reviews of a task all failed, mostly with new findings, '
               'and the worst severity did not improve. Calibrated: window 4 (the default) stopped no task '
               'that later passed. Exit codes: append 0; signal 0 fired, 1 did not; replay 0 (2 for a missing plan).')
    sub = parser.add_subparsers(dest='command', required=True)
    append = sub.add_parser('append', help="append the task's latest review to tasks/<id>/ledger.tsv")
    append.add_argument('plan', help='the plan directory')
    append.add_argument('id', help='the task id')
    signal = sub.add_parser('signal', help='print fire<TAB>summary when the signal fires (exit 0), else exit 1')
    signal.add_argument('task_dir', help='tasks/<id> of the plan')
    signal.add_argument('--window', type=int, default=None, metavar='N',
                        help='reviews the signal looks at (default: <plan>/converge_window, else %d)' % DEFAULT_WINDOW)
    signal.add_argument('--share', type=float, default=DEFAULT_SHARE, metavar='S',
                        help='least share of the findings of reviews 2..N that must be new (default %s)' % DEFAULT_SHARE)
    replay = sub.add_parser('replay', help='rebuild every task of a plan from its review dirs and print the signal, writing nothing')
    replay.add_argument('plan_dir', help='the plan directory')
    replay.add_argument('--window', type=int, default=None, metavar='N', help='as for signal')
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'append':
        return _append(Path(args.plan), args.id)
    if args.command == 'signal':
        return _signal(Path(args.task_dir), args.window, args.share)
    return _replay(Path(args.plan_dir), args.window)


if __name__ == '__main__':
    sys.exit(main())
