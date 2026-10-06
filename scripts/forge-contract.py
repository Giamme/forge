#!/usr/bin/env python3
"""The QA contract: one shared review prompt, a severity vocabulary and a deterministic gate.

Three subcommands, stdlib only, no network:

  qa --part head|tail [--kind task|final|solo] [--threshold P0|P1|P2|P3]
      Prints one piece of the QA prompt. The caller prints everything between and
      after the pieces itself (scope notes, the diff fence, the memory note and the
      "Place any learning notes before the final FORGE_VERDICT ..." sentence).
  dwarf
      Prints the implementer rules block that callers append to the dwarf prompt.
  gate --verdict PASS|FAIL|UNKNOWN --qa-last FILE [--threshold Pn] [--known-issues OUT]
      Prints ONE tab-separated line
          decision <TAB> worst_confirmed <TAB> n_confirmed <TAB> n_unlabelled <TAB> n_below
      and, when findings were tolerated, writes them to OUT.

Severity. Reviewers label every finding P0 (data loss, security hole, crash or wrong
result on the main path), P1 (wrong behaviour on realistic input, breaks an existing
contract), P2 (edge-case bug, unusual-timing race, missing hardening) or P3 (minor).
Severity is impact; confidence stays CONFIRMED vs PLAUSIBLE.

Threshold. Without one nothing changes: any CONFIRMED finding means FAIL and the gate
never overrides a verdict. The threshold is the LEAST severe level that still blocks:
`P2` means P0, P1 and P2 block and only P3 is tolerated; `P0` means only P0 blocks;
`P3` means everything blocks (nothing is ever tolerated). A threshold of "" is "unset".

The gate. decision is `accept` only when ALL of these hold, otherwise `keep`:
  - a threshold is set and the verdict is FAIL (PASS and UNKNOWN are always `keep`);
  - at least one CONFIRMED finding exists (a FAIL with none stays a FAIL);
  - n_unlabelled == 0, and every CONFIRMED finding carries a P0-P3 label strictly less
    severe than the threshold.
`accept` is a statement about the findings only; wiring it into a verdict is the
caller's job. Fail-safe means blocking: anything the parser cannot read confidently
pushes toward `keep`, never toward `accept`.

Fields: worst_confirmed is the most severe label among CONFIRMED labelled findings
(`-` if none); n_confirmed counts CONFIRMED findings; n_unlabelled counts CONFIRMED
findings with no recognisable severity, plus located findings carrying a severity but
no CONFIRMED/PLAUSIBLE tag at all (neither can ever be tolerated); n_below counts
CONFIRMED labelled findings below the threshold (0 with no threshold).

Parsing. The reply is cut into finding blocks. A block starts at a bullet (`-`, `*`,
`+`, `1.`, `1)`), a markdown heading, a table row, or a flush line that leads with a
severity/status tag and a file:line; it runs until the next such line at the same or a
lower indentation. Nested bullets and indented or lazy continuation lines belong to the
block; a flush-left paragraph after a blank line does not. A block is CONFIRMED when it
contains the uppercase word CONFIRMED (or a bracketed/bold `[confirmed]` in its first
line); CONFIRMED beats PLAUSIBLE; PLAUSIBLE-only blocks are ignored; "NOT CONFIRMED" and
lowercase prose do not count. The severity is the first P0-P3 token of the block's first
line (`[P2]`, `(P2)`, `**P2**`, `P2:`, `P2 -` or a standalone `P2`; uppercase only).

Phantom findings. A reviewer may echo the contract it was given. Three layers keep that
from creating findings or blocking: (1) FORGE_VERDICT and FORGE_LEARNING text is removed
before parsing (the verdict line itself says "CONFIRMED"); (2) any line that is a
contiguous fragment of this module's own contract text (all parts, kinds and thresholds,
whitespace-collapsed, at least 25 characters, list marker ignored) is removed, which
covers the example bullet, the severity definitions and the instructions even when
re-wrapped; (3) a block whose first line holds the literal placeholder
`path/file.py:123` is ignored. A leftover false match can only add a finding or a
blocker, and an added CONFIRMED finding without a label blocks.

Exit codes: 0 ok (even for `keep`), 2 usage, 3 unreadable QA file or unwritable OUT
file. On any nonzero exit callers must treat the decision as `keep`.
"""
from __future__ import annotations

import argparse
import collections
import functools
import os
import re
import sys

SEVERITIES = ('P0', 'P1', 'P2', 'P3')
PLACEHOLDER = 'path/file.py:123'
KINDS = ('task', 'final', 'solo')

Finding = collections.namedtuple('Finding', 'status severity located line text')
Finding.__doc__ = (
    'One finding block. status: CONFIRMED, PLAUSIBLE or UNSTATED (a located, severity-'
    'tagged block with neither status word). severity: 0-3 or None. line: 1-based '
    'line number of the block start in the original text. text: the block, verbatim.')
GateResult = collections.namedtuple(
    'GateResult', 'decision worst n_confirmed n_unlabelled n_below tolerated')
GateResult.__doc__ = (
    'decision accept|keep, worst most severe confirmed label or "-", counters, and the '
    'tuple of tolerated Findings (CONFIRMED, labelled, below the threshold).')


# --- prompt text -----------------------------------------------------------------

def _rank(threshold) -> 'int | None':
    if threshold is None or str(threshold).strip() == '':
        return None
    value = str(threshold).strip().upper()
    if value not in SEVERITIES:
        raise ValueError('threshold must be one of ' + ', '.join(SEVERITIES))
    return int(value[1])


_NO_TRUST = [
    "Do not trust the implementer's own reports of which checks ran or passed (the task",
    'text may quote them); judge the diff itself.',
]

_HEAD = {
    'task': [
        'The implementer was asked to do the task above. Review the diff below for correctness',
        'bugs: logic errors, broken edge cases, behaviour that does not match what was asked.',
        "Also say if it solved a different problem, or touched files outside the task's scope.",
    ] + _NO_TRUST,
    'solo': [
        'Review the diff below for correctness bugs: logic errors, broken edge cases, wrong',
        'behaviour versus what was asked. Also say if it solved a different problem than the',
        "one stated, or changed files outside the goal's scope.",
    ] + _NO_TRUST,
    'final': [
        'This is the final, whole-run review. The diff below is the COMBINED result of several',
        'tasks that were implemented independently and each reviewed on its own slice only.',
        'Review it for correctness bugs, and look especially for what per-task review could not',
        'see: interactions between tasks, contract or interface mismatches between them,',
        'async or ordering races across modules, regressions of existing contracts, and',
        'anything that only breaks when the pieces run together. Also say if the combined',
        "change solved a different problem than the goal and tasks above, or touched files",
        'outside their scope.',
        "Do not trust the implementers' own reports of which checks ran or passed; judge the",
        'diff itself.',
    ],
}

_FINDINGS = [
    'Report findings only — do not edit any file. For each finding give the file and line,',
    'what breaks, and a concrete input that triggers it. Mark each CONFIRMED if you traced',
    'it in the code, or PLAUSIBLE if you could not fully verify it.',
]
_SOLO_EXTRA = ['If the diff is correct, say so plainly rather than inventing something to report.']

_SEVERITY = [
    'Label every finding with its severity at the start of its own bullet, in exactly this form:',
    '  - [P1][CONFIRMED] path/file.py:123 — what breaks (trigger input)',
    'One finding per bullet. Severity is about impact, not confidence; confidence is the',
    'CONFIRMED or PLAUSIBLE tag.',
    '  P0  data loss, security hole, crash or wrong result on the main path',
    '  P1  wrong behaviour on realistic input, or breaks an existing contract',
    '  P2  edge-case bug, race that needs unusual timing, missing hardening',
    '  P3  minor: cosmetic, naming, nit-level correctness',
]

_NO_THRESHOLD = ['The severity tag does not change the verdict rule below.']

_PASS = '  FORGE_VERDICT: PASS   — no confirmed correctness bug (style nits are not failures)'
_FAIL = '  FORGE_VERDICT: FAIL   — at least one CONFIRMED correctness bug'


def _threshold_clause(rank: int) -> list:
    name = SEVERITIES[rank]
    blocking = ', '.join(SEVERITIES[:rank + 1])
    if rank == len(SEVERITIES) - 1:
        lines = ['Blocking threshold: %s. Every severity (%s) is BLOCKING, so any CONFIRMED' % (name, blocking),
                 'finding justifies FAIL.']
    else:
        tolerated = ', '.join(SEVERITIES[rank + 1:])
        lines = ['Blocking threshold: %s. Findings at %s or worse (%s) are BLOCKING. A' % (name, name, blocking),
                 'CONFIRMED finding at a lower severity (%s) is recorded as a known issue and does' % tolerated,
                 'not by itself justify FAIL. Give FAIL if and only if at least one CONFIRMED',
                 'finding is blocking.']
    return lines + ['A CONFIRMED finding without a severity tag counts as blocking.']


def qa_text(part: str, kind: str = 'task', threshold=None) -> str:
    """One shared piece of the QA prompt, newline-terminated, no leading/trailing blank."""
    if kind not in KINDS:
        raise ValueError('kind must be one of ' + ', '.join(KINDS))
    rank = _rank(threshold)
    if part == 'head':
        return '\n'.join(_HEAD[kind]) + '\n'
    if part != 'tail':
        raise ValueError('part must be head or tail')
    lines = list(_FINDINGS) + (_SOLO_EXTRA if kind == 'solo' else [])
    lines += [''] + _SEVERITY
    lines += [''] + (_NO_THRESHOLD if rank is None else _threshold_clause(rank))
    if rank is None:
        verdicts = [_PASS, _FAIL]
    else:
        where = '%s or worse' % SEVERITIES[rank]
        verdicts = ['  FORGE_VERDICT: PASS   — no CONFIRMED finding at the blocking threshold (%s)' % where,
                    '  FORGE_VERDICT: FAIL   — at least one CONFIRMED finding at the blocking threshold (%s)' % where]
    lines += ['', 'End your reply with exactly one line:'] + verdicts
    return '\n'.join(lines) + '\n'


_DWARF = [
    '## Implementer rules',
    '- Do not run git commit, checkout, switch, reset, stash or rebase. The harness captures',
    '  the working tree itself; a self-commit makes the change set ambiguous.',
    '- Do not start background processes, and leave nothing running when you finish.',
    '- Finish every measurement and test run BEFORE your last message and report its actual',
    '  result. If you could not run something, say so explicitly instead of claiming it passed.',
    '- State precisely which verification commands you ran and what each one returned.',
]


def dwarf_text() -> str:
    """The implementer rules block, newline-terminated."""
    return '\n'.join(_DWARF) + '\n'


# --- parsing ---------------------------------------------------------------------

_PROTOCOL = re.compile(r'FORGE_(?:VERDICT|LEARNING):.*$')
_QUOTE = re.compile(r'^\s*(?:>[ \t]?)+')
_MARKER = re.compile(r'^(?:[-*+•–]|\d{1,3}[.)]|#{1,6})\s+')
_HEADING = re.compile(r'^ {0,3}#{1,6}[ \t]+\S')
_BULLET = re.compile(r'^\s*(?:[-*+•–]|\d{1,3}[.)])[ \t]+\S')
_TABLE = re.compile(r'^\s*\|')
_LEAD = re.compile(r'^\s*[*_\[(]*(?:P[0-3]|CONFIRMED|PLAUSIBLE)(?![\w])')
_LOCATION = re.compile(r'[\w./\\-]+\.\w+:\d+|\b[Ll]ines? \d+')
# P0-P3 as a standalone token: not glued to a word, path or range (P1.py, src/P1/x, P1-P2).
_SEVERITY_TOKEN = re.compile(r'(?<![\w/.\\-])P([0-3])(?![\w/\\])(?!\.\w)(?!-\w)')
_CONFIRMED = re.compile(r'\bCONFIRMED\b')
_NOT_CONFIRMED = re.compile(r'\b(?i:not)\s+CONFIRMED\b|\bNON-CONFIRMED\b')
_CONFIRMED_TAG = re.compile(r'\[\s*confirmed\s*\]|\*\*\s*confirmed\s*\*\*', re.IGNORECASE)
_PLAUSIBLE = re.compile(r'\bPLAUSIBLE\b')
_MIN_ECHO = 25


def _collapse(text: str) -> str:
    return ' '.join(text.split())


@functools.lru_cache(maxsize=None)
def _contract_flat() -> str:
    parts = [dwarf_text()]
    for kind in KINDS:
        for part in ('head', 'tail'):
            for threshold in (None,) + SEVERITIES:
                parts.append(qa_text(part, kind, threshold))
    return _collapse(' '.join(parts))


def _clean(text: str) -> list:
    """(line number, line) pairs with protocol lines, quotes and echoed contract removed."""
    flat = _contract_flat()
    out = []
    for number, raw in enumerate(text.replace('\r\n', '\n').replace('\r', '\n').split('\n'), 1):
        raw = raw.expandtabs(4)
        line = _PROTOCOL.sub('', raw)
        if line != raw and not line.strip():
            continue
        line = _QUOTE.sub('', line).rstrip()
        core = _collapse(_MARKER.sub('', line.strip()))
        if len(core) >= _MIN_ECHO and core in flat:
            continue
        out.append((number, line))
    return out


def _starts_block(line: str) -> bool:
    if _HEADING.match(line) or _BULLET.match(line) or _TABLE.match(line):
        return True
    return bool(_LEAD.match(line) and _LOCATION.search(line))


def _blocks(lines: list) -> list:
    blocks = []  # [start number, indent, [lines]]
    current = None
    blank = False
    for number, line in lines:
        if not line.strip():
            blank = True
            if current is not None:
                current[2].append('')
            continue
        indent = len(line) - len(line.lstrip(' '))
        if _starts_block(line):
            nested = (current is not None and indent > current[1]
                      and not _HEADING.match(line))
            if nested:
                current[2].append(line)
            else:
                current = [number, indent, [line]]
                blocks.append(current)
        elif current is not None:
            if blank and indent <= current[1]:
                current = None
            else:
                current[2].append(line)
        blank = False
    return blocks


def _severity(first_line: str):
    match = _SEVERITY_TOKEN.search(first_line)
    return int(match.group(1)) if match else None


def _finding(number: int, lines: list):
    while lines and not lines[-1].strip():
        lines = lines[:-1]
    first = lines[0]
    if PLACEHOLDER in first:
        return None
    body = _NOT_CONFIRMED.sub('', '\n'.join(lines))
    severity = _severity(first)
    located = bool(_LOCATION.search(first))
    if _CONFIRMED.search(body) or _CONFIRMED_TAG.search(first):
        status = 'CONFIRMED'
    elif _PLAUSIBLE.search(body):
        status = 'PLAUSIBLE'
    elif severity is not None and located:
        status = 'UNSTATED'
    else:
        return None
    return Finding(status, severity, located, number, '\n'.join(lines))


def parse_findings(text: str) -> list:
    """Finding blocks of a QA reply, in order; see the module docstring for the rules."""
    found = (_finding(start, lines) for start, _, lines in _blocks(_clean(text)))
    return [finding for finding in found if finding is not None]


# --- the gate --------------------------------------------------------------------

def gate(verdict: str, text: str, threshold=None) -> GateResult:
    """Decide whether a QA FAIL may be accepted as PASS-with-known-issues."""
    rank = _rank(threshold)
    findings = parse_findings(text)
    confirmed = [f for f in findings if f.status == 'CONFIRMED']
    labelled = [f for f in confirmed if f.severity is not None]
    unlabelled = (len(confirmed) - len(labelled)
                  + sum(1 for f in findings if f.status == 'UNSTATED'))
    worst = 'P%d' % min(f.severity for f in labelled) if labelled else '-'
    below = tuple(f for f in labelled if rank is not None and f.severity > rank)
    accept = (rank is not None and verdict == 'FAIL' and bool(confirmed)
              and unlabelled == 0 and len(below) == len(confirmed))
    return GateResult('accept' if accept else 'keep', worst, len(confirmed),
                      unlabelled, len(below), below)


def known_issues_text(threshold, tolerated) -> str:
    name = SEVERITIES[_rank(threshold)]
    lines = ['## Known issues (QA findings below blocking threshold %s)' % name, '',
             'QA confirmed these findings at a severity less than %s, the blocking' % name,
             'threshold (%s and worse block). They are recorded for follow-up and do not' % name,
             'by themselves block the task.']
    for finding in tolerated:
        lines += ['', finding.text]
    return '\n'.join(lines) + '\n'


# --- command line ----------------------------------------------------------------

def _threshold_arg(value: str):
    value = value.strip().upper()
    if value == '':
        return None
    if value not in SEVERITIES:
        raise argparse.ArgumentTypeError('invalid threshold %r (choose P0, P1, P2 or P3)' % value)
    return value


def _emit(text: str) -> None:
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode('utf-8'))
    sys.stdout.buffer.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='forge-contract.py', formatter_class=argparse.RawDescriptionHelpFormatter,
        description='Shared QA contract, implementer rules and severity gate for forge.',
        epilog='threshold: the LEAST severe level that still blocks. P2 = P0, P1 and P2 '
               'block, only P3 is tolerated; P0 = only P0 blocks; P3 = everything blocks. '
               'Unset (or empty) = no gate, today\'s semantics.')
    sub = parser.add_subparsers(dest='command', required=True)
    qa = sub.add_parser('qa', help='print one piece of the QA prompt')
    qa.add_argument('--part', required=True, choices=('head', 'tail'),
                    help='head = review preamble, tail = findings format + verdict contract')
    qa.add_argument('--kind', choices=KINDS, default='task',
                    help='task (default), final (whole-run review) or solo (solo wording)')
    qa.add_argument('--threshold', type=_threshold_arg, default=None, metavar='P0..P3',
                    help='least severe level that still blocks (default: none)')
    sub.add_parser('dwarf', help='print the implementer rules block')
    gate_parser = sub.add_parser(
        'gate', help='print: decision, worst_confirmed, n_confirmed, n_unlabelled, n_below')
    gate_parser.add_argument('--verdict', required=True, choices=('PASS', 'FAIL', 'UNKNOWN'))
    gate_parser.add_argument('--qa-last', required=True, metavar='FILE', help="the QA reply")
    gate_parser.add_argument('--threshold', type=_threshold_arg, default=None, metavar='P0..P3',
                             help='least severe level that still blocks (default: none)')
    gate_parser.add_argument('--known-issues', metavar='OUT', default=None,
                             help='write tolerated findings here when there are any')
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == 'qa':
        _emit(qa_text(args.part, args.kind, args.threshold))
        return 0
    if args.command == 'dwarf':
        _emit(dwarf_text())
        return 0
    try:
        with open(args.qa_last, 'rb') as handle:
            text = handle.read().decode('utf-8', 'replace')
    except OSError as error:
        sys.stderr.write('forge-contract: cannot read %s: %s\n' % (args.qa_last, error))
        return 3
    result = gate(args.verdict, text, args.threshold)
    if args.known_issues and result.n_below >= 1:
        temporary = args.known_issues + '.%d.tmp' % os.getpid()
        try:
            with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
                handle.write(known_issues_text(args.threshold, result.tolerated))
            os.replace(temporary, args.known_issues)
        except OSError as error:
            sys.stderr.write('forge-contract: cannot write %s: %s\n' % (args.known_issues, error))
            try:
                os.unlink(temporary)
            except OSError:
                pass
            return 3
    _emit('%s\t%s\t%d\t%d\t%d\n' % (result.decision, result.worst, result.n_confirmed,
                                    result.n_unlabelled, result.n_below))
    return 0


if __name__ == '__main__':
    sys.exit(main())
