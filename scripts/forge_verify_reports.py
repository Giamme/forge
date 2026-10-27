"""Test reports for forge-verify.py: JUnit XML and TAP 12-14. Stdlib only, offline.

``scan(directory, since)`` reads every ``*.xml`` and ``*.tap`` under a lane's report
directory that was written at or after ``since`` (an epoch float: the start of that run),
recursively and without following symlinks, and returns the lane's ``tests``:

    {"pass", "fail", "skip", "error",
     "failing": [{"name", "suite", "file", "message", "excerpt", "known", "log_line"}],
     "reports": [<paths relative to the directory>], "notes": [...], "truncated": bool}

plus what a baseline verdict needs and the display does not keep: every failing name (not
only the first 200) and whether the evidence is complete. A report that cannot be used
(too big, unreadable, not parseable, a DOCTYPE or ENTITY declaration) is skipped with a
note and makes the evidence incomplete; nothing here raises on a hostile file.
"""
from __future__ import annotations

import os
import re
import stat
import xml.etree.ElementTree as ET

FILE_MAX = 32 << 20
FAILING_MAX = 200
MESSAGE_MAX = 500
EXCERPT_MAX = 2000
LOG_SEARCH_MAX = 64 << 20

ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]')
# Refused before parsing: no external entities, no entity expansion bombs.
DECLARATIONS = re.compile(rb'<!\s*(?:DOCTYPE|ENTITY)', re.I)
TAP_LINE = re.compile(r'(not\s+)?ok\b\s*(\d+)?\s*(?:-\s*)?(.*)$')
TAP_PLAN = re.compile(r'1\.\.(\d+)\b')
TAP_BAIL = re.compile(r'Bail out!\s*(.*)$')
TAP_DIRECTIVE = re.compile(r'\s*(?<!\\)#\s*(SKIP|TODO)\b', re.I)


def clean(text: str | None, cap: int) -> str:
    return ANSI.sub('', text or '').strip()[:cap]


def first_line(text: str | None) -> str:
    for line in (text or '').splitlines():
        if line.strip():
            return line.strip()
    return ''


def junit_cases(data: bytes) -> list[dict]:
    """[{name, suite, outcome, message, excerpt}] from a JUnit XML document; ValueError when unusable."""
    # The declaration check reads bytes, so only an ASCII-compatible encoding is let through:
    # UTF-16 or UTF-32 (a byte-order mark, or NUL bytes) could hide a DOCTYPE from it.
    if data[:2] in (b'\xff\xfe', b'\xfe\xff') or b'\x00' in data:
        raise ValueError('only UTF-8 reports are read')
    if DECLARATIONS.search(data):
        raise ValueError('DOCTYPE or ENTITY declarations are refused')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError('not well-formed XML (%s)' % error)
    if root.tag not in ('testsuite', 'testsuites'):
        raise ValueError('no testsuite or testsuites element')
    parents = {child: parent for parent in root.iter() for child in parent}
    cases = []
    for case in root.iter('testcase'):
        name, classname = case.get('name') or '?', case.get('classname')
        suite = parents.get(case)
        problem = case.find('failure')
        outcome = 'fail'
        if problem is None:
            problem, outcome = case.find('error'), 'error'
        if problem is None:
            outcome = 'skip' if case.find('skipped') is not None else 'pass'
        entry = {'name': '%s.%s' % (classname, name) if classname else name,
                 'suite': suite.get('name') if suite is not None and suite.tag == 'testsuite' else None,
                 'outcome': outcome, 'message': '', 'excerpt': ''}
        if problem is not None:
            text = problem.text or ''
            entry['message'] = problem.get('message') or first_line(text)
            parts = [text] + [(case.findtext(tag) or '') for tag in ('system-out', 'system-err')]
            entry['excerpt'] = '\n'.join(p.strip('\n') for p in parts if p.strip())
        cases.append(entry)
    return cases


def tap_cases(data: bytes) -> tuple[list[dict], str | None]:
    """Top-level test points of a TAP stream, each with its YAML diagnostic block if any, and
    why the stream is incomplete (a `Bail out!`, no plan, or fewer or more points than its
    plan), else None. An incomplete stream's points still count, but cannot prove a baseline."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ValueError('not UTF-8 text')
    cases: list[dict] = []
    block: list[str] | None = None
    planned = bail = None
    for line in text.splitlines():
        if block is not None:
            if line.strip() == '...':
                if cases:
                    diag = '\n'.join(block)
                    cases[-1]['excerpt'] = diag
                    found = re.search(r'^\s*message:\s*(.*)$', diag, re.M)
                    if found:
                        cases[-1]['message'] = found.group(1).strip().strip('\'"')
                block = None
            else:
                block.append(line)
            continue
        if line.strip() == '---' and line.startswith(' ') and cases:
            block = []
            continue
        stop = TAP_BAIL.match(line)
        if stop:
            bail = stop.group(1).strip() or 'no reason given'
            break
        plan = TAP_PLAN.match(line)
        if plan and planned is None:
            planned = int(plan.group(1))
            continue
        match = TAP_LINE.match(line)     # top level only: subtests are indented
        if not match:
            continue
        description = match.group(3)
        directive = TAP_DIRECTIVE.search(description)
        name = description[:directive.start()].strip() if directive else description.strip()
        if directive:
            outcome = 'skip'
        else:
            outcome = 'fail' if match.group(1) else 'pass'
        cases.append({'name': name or 'test %s' % (match.group(2) or len(cases) + 1), 'suite': None,
                      'outcome': outcome, 'message': '', 'excerpt': ''})
    if not cases:
        raise ValueError('no TAP test points')
    if bail is not None:
        return cases, 'it bailed out (%s)' % clean(bail, MESSAGE_MAX)
    if planned is None:
        return cases, 'it has no plan line (1..N)'
    if planned != len(cases):
        return cases, 'its plan says %d tests and it has %d' % (planned, len(cases))
    return cases, None


def scan(directory: str, since: float) -> tuple[dict, list[str], bool]:
    """(tests, every failing name, whether the evidence is complete) for one lane run."""
    tests = {'pass': 0, 'fail': 0, 'skip': 0, 'error': 0, 'failing': [], 'reports': [],
             'notes': [], 'truncated': False}
    names: list[str] = []
    complete = True
    found = []
    for top, dirs, files in os.walk(directory, followlinks=False):
        dirs.sort()
        for name in sorted(files):
            if name.endswith(('.xml', '.tap')):
                found.append(os.path.join(top, name))
    for path in found:
        rel = os.path.relpath(path, directory)
        try:
            info = os.lstat(path)
        except OSError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_mtime < since:
            continue          # a symlink, or a report an earlier run left
        try:
            if info.st_size > FILE_MAX:
                raise ValueError('larger than %d MB' % (FILE_MAX >> 20))
            with open(path, 'rb') as handle:
                data = handle.read(FILE_MAX + 1)
            if len(data) > FILE_MAX:
                raise ValueError('larger than %d MB' % (FILE_MAX >> 20))
            cases, problem = (junit_cases(data), None) if rel.endswith('.xml') else tap_cases(data)
        except (OSError, ValueError, RecursionError) as error:
            tests['notes'].append('%s skipped: %s' % (rel, error))
            complete = False
            continue
        tests['reports'].append(rel)
        if problem:
            tests['notes'].append('%s is incomplete: %s' % (rel, problem))
            complete = False
        for case in cases:
            tests[case['outcome']] += 1
            if case['outcome'] not in ('fail', 'error'):
                continue
            names.append(case['name'])
            if len(tests['failing']) >= FAILING_MAX:
                tests['truncated'] = True
                continue
            tests['failing'].append({'name': clean(case['name'], MESSAGE_MAX), 'suite': case['suite'],
                                     'file': rel, 'message': clean(case['message'], MESSAGE_MAX),
                                     'excerpt': clean(case['excerpt'], EXCERPT_MAX),
                                     'known': False, 'log_line': None})
    return tests, names, complete


def log_lines(log: str, names: list[str]) -> dict:
    """{name: 1-based number of the first log line containing it}, within the first 64 MB."""
    wanted = sorted({n for n in names if n}, key=len, reverse=True)
    if not wanted:
        return {}
    pattern = re.compile('|'.join(re.escape(n) for n in wanted))
    found: dict = {}
    read = 0
    try:
        with open(log, 'rb') as handle:
            for number, raw in enumerate(handle, 1):
                read += len(raw)
                if read > LOG_SEARCH_MAX:
                    break
                line = raw.decode('utf-8', 'replace')
                if not pattern.search(line):    # one search per line; most lines name no test
                    continue
                for name in wanted:
                    if name not in found and name in line:
                        found[name] = number
                if len(found) == len(wanted):
                    break
    except OSError:
        pass
    return found


def load_baseline(path: str) -> set:
    """Known failing test names: one per line, blank lines and # comments ignored."""
    with open(path, encoding='utf-8', errors='replace') as handle:
        return {line.strip() for line in handle if line.strip() and not line.strip().startswith('#')}
