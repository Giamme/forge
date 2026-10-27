"""The checks an agent ran, read from its transcript (transcripts.py): each test, type check, lint or build command
with its exit, how long it took and the counts it printed; the call it is running now; the commands its brief's
acceptance criteria name that it never ran; and the environment problems its failed commands met (Docker, a
sandbox's EPERM, a port in use, …), which forge-ui groups across a run's tasks.

Nothing here opens a file: it reads the events transcripts.transcript() answers and the brief's text.
"""
from __future__ import annotations

import re

KINDS = ('test', 'typecheck', 'lint', 'build')
COMMAND_CAP = 1000          # characters of a command kept in a check
LINE_CAP = 240              # of an environment problem's line
TAIL_LINES = 6              # of a failed check's output, its last non-empty lines
_ANSI = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')

# ---------- what a command is ----------

_SPLIT = re.compile(r'\s*(?:&&|\|\||;|\||\n)\s*')
_WRAPPED = re.compile(r'''^(?:/\S+/)?(?:ba|z)?sh\s+-l?c\s+(['"])(.*)\1$''', re.S)
_PREFIX = re.compile(r'''^(?:[A-Za-z_][A-Za-z0-9_]*=(?:"[^"]*"|'[^']*'|\S*)\s+|env\s+|command\s+|exec\s+|time\s+|nice\s+(?:-n\s*-?\d+\s+)?
    |timeout\s+(?:-\S+\s+)*\d+(?:\.\d+)?[smhd]?\s+|gtimeout\s+(?:-\S+\s+)*\d+[smhd]?\s+|xvfb-run\s+(?:-\S+\s+)*
    |uv\s+run\s+(?:--?[A-Za-z-]+(?:[= ](?!-)\S+)?\s+)*|poetry\s+run\s+|pipenv\s+run\s+|hatch\s+run\s+|pdm\s+run\s+|rye\s+run\s+
    |npx\s+(?:--yes\s+|-y\s+)?|pnpm\s+(?:exec|dlx)\s+|yarn\s+dlx\s+|bunx\s+|bundle\s+exec\s+|dotenv\s+(?:-\S+\s+)*--\s+)''', re.X)
_PY = r'(?:python(?:\d+(?:\.\d+)?)?|py)'
_PKG = r'(?:npm|pnpm|yarn|bun)'
# (kind, pattern), tried in this order on each part of a command
_PATTERNS = tuple((kind, re.compile(pattern)) for kind, pattern in (
    ('test', _PY + r'\s+-m\s+(?:pytest|unittest|nose2?|tox|nox|green)\b'),
    ('test', r'(?:pytest|py\.test|tox|nox|nose2?|ward)\b'),
    ('test', _PY + r'\s+(?:-\S+\s+)*(?:\S*/)?(?:test_\S*|\S*_tests?|run_?tests|runtests|tests)\.py\b'),
    ('test', _PKG + r'\s+(?:run(?:-script)?\s+)?(?:test|tests|t)(?:[:\-]\S*)?(?:\s|$)'),
    ('test', r'node\s+(?:--\S+\s+)*--test\b'),
    ('test', r'(?:jest|vitest|mocha|ava|tap|jasmine|karma|cypress\s+run|playwright\s+test|deno\s+test|bun\s+test)\b'),
    ('test', r'go\s+test\b|cargo\s+(?:test|nextest)\b|(?:make|just|task)\s+(?:-\S+\s+)*(?:test|tests|check|test-\S+|check-\S+)\b'),
    ('test', r'(?:\./)?(?:\S*/)?(?:test|tests|check|run-tests|run_tests)\.sh\b|(?:ba|z)?sh\s+(?:\S*/)?\S*(?:test|check)\S*\.sh\b'),
    ('test', r'(?:rspec|rake\s+test|phpunit|vendor/bin/phpunit|dotnet\s+test|mvn\s+(?:-\S+\s+)*(?:test|verify)\b|'
             r'(?:\./)?gradlew?\s+(?:-\S+\s+)*(?:test|check)\b|swift\s+test|ctest|mix\s+test|stack\s+test|cabal\s+test)'),
    ('typecheck', r'(?:tsc|vue-tsc|mypy|dmypy|pyright|basedpyright|pyre|pytype|flow)\b|' + _PY + r'\s+-m\s+(?:mypy|pyright|py_compile|compileall)\b'),
    ('typecheck', _PKG + r'\s+(?:run\s+)?(?:typecheck|type-check|types|tsc|check-types|check:types)\b'),
    ('typecheck', r'cargo\s+check\b|go\s+vet\b|node\s+--check\b|(?:ba|z)?sh\s+-n\b'),
    ('lint', r'(?:eslint|ruff|flake8|pylint|pycodestyle|pyflakes|golangci-lint|shellcheck|stylelint|biome|rubocop|hadolint|'
             r'markdownlint|yamllint|actionlint|oxlint|swiftlint|ktlint|tflint|deno\s+lint|cargo\s+clippy)\b'),
    ('lint', _PY + r'\s+-m\s+(?:ruff|flake8|pylint|pycodestyle)\b|' + _PKG + r'\s+(?:run\s+)?lint\S*'),
    ('lint', r'(?:prettier|black|isort|gofmt|rustfmt|cargo\s+fmt|ruff\s+format)\b.*(?:--check|--diff|\s-l\b|\s-d\b)'),
    ('build', _PKG + r'\s+(?:run\s+)?build\S*|(?:cargo|go|swift|dotnet|zig)\s+build\b|(?:\./)?gradlew?\s+(?:-\S+\s+)*build\b|'
              r'mvn\s+(?:-\S+\s+)*(?:package|install|compile)\b|(?:vite|webpack|next|tsup|esbuild|rollup|parcel)\s+build\b|'
              r'make(?:\s+-j\s*\d*)?(?:\s+(?:all|build))?$|docker\s+(?:compose\s+)?build\b'),
))
# programs that only read: a failed one says nothing about the environment
_READERS = frozenset(('rg', 'grep', 'egrep', 'fgrep', 'cat', 'sed', 'awk', 'head', 'tail', 'less', 'more', 'ls', 'find', 'fd',
                      'wc', 'nl', 'file', 'stat', 'tree', 'jq', 'diff', 'cmp', 'echo', 'printf', 'test', '[', 'which', 'type',
                      'git', 'pwd', 'cd', 'true', 'false', 'basename', 'dirname', 'realpath', 'readlink', 'sort', 'uniq', 'cut'))


def parts(command):
    """The commands a shell line runs, each without its env assignments and wrappers (`timeout 600`, `uv run`,
    `npx`, …); `cd` steps are left out."""
    out = []
    for part in _SPLIT.split(command or ''):
        part = part.strip().strip('()').strip()
        inner = _WRAPPED.match(part)
        if inner:
            out.extend(parts(inner.group(2)))
            continue
        while True:
            m = _PREFIX.match(part)
            if not m or not m.group(0):
                break
            part = part[m.end():]
        if part and not re.match(r'(?:cd|pushd|popd|export|set|source|\.)\b', part):
            out.append(part)
    return out


def classify(command):
    """test, typecheck, lint or build when some part of the command is one (the first of those, in that order),
    else None."""
    found = set()
    for part in parts(command):
        for kind, pattern in _PATTERNS:
            if pattern.match(part):
                found.add(kind)
                break
    return next((kind for kind in KINDS if kind in found), None)


def _readonly(command):
    programs = [part.split()[0].rsplit('/', 1)[-1] for part in parts(command) if part.split()]
    return bool(programs) and all(program in _READERS for program in programs)


# ---------- what it printed ----------

_WORDS = {'passed': 'passed', 'passing': 'passed', 'pass': 'passed', 'xpassed': 'passed', 'ok': None,
          'failed': 'failed', 'failing': 'failed', 'fail': 'failed', 'error': 'failed', 'errors': 'failed',
          'skipped': 'skipped', 'pending': 'skipped', 'todo': 'skipped', 'xfailed': 'skipped', 'skip': 'skipped'}
_COUNTED = re.compile(r'(\d+)\s+(passed|failed|skipped|errors?|xfailed|xpassed|pending|todo)\b')
_SUMMARY = re.compile(r'^.*\b\d+\s+(?:passed|failed)\b.*$', re.M)


def _ints(**values):
    return {key: int(value) for key, value in values.items() if value is not None}


def counts(output, kind=None):
    """What a check's output says it found: {passed, failed, skipped} for tests, {errors, warnings} for type
    checks and linters, or None. Read from the end of the output, where the summaries are."""
    text = _ANSI.sub('', output or '')[-8000:]
    if not text.strip():
        return None
    if kind in ('lint', 'typecheck'):
        return _problems(text) or _tests(text)
    return _tests(text) or _problems(text)


def _tests(text):
    # cargo test: one line per test binary, summed
    rows = re.findall(r'test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored', text)
    if rows:
        return {key: sum(int(row[i]) for row in rows) for i, key in enumerate(('passed', 'failed', 'skipped'))}
    # forge's tests/run_tests.py: "1639 tests in 312 classes: …; 75 skipped", then "OK" or "FAILED: 2 failures, 1 errors in …"
    m = None
    for m in re.finditer(r'^(\d+) tests in \d+ classes: .*; (\d+) skipped\s*\n(?:FAILED: (\d+) failures, (\d+) errors|OK)', text, re.M):
        pass
    if m:
        total, skipped = int(m.group(1)), int(m.group(2))
        failed = int(m.group(3) or 0) + int(m.group(4) or 0)
        return {'passed': max(0, total - failed - skipped), 'failed': failed, 'skipped': skipped}
    # unittest: "Ran 12 tests in 0.3s" then "OK (skipped=2)" or "FAILED (failures=1, errors=2)"
    ran = list(re.finditer(r'^Ran (\d+) tests? in [\d.]+s\s*\n+\s*(OK|FAILED)\b(?: \(([^)]*)\))?', text, re.M))
    if ran:
        m = ran[-1]
        detail = {key: int(value) for key, value in re.findall(r'(\w+)=(\d+)', m.group(3) or '')}
        failed = detail.get('failures', 0) + detail.get('errors', 0) + detail.get('unexpected_successes', 0)
        skipped = detail.get('skipped', 0) + detail.get('expected_failures', 0)
        return {'passed': max(0, int(m.group(1)) - failed - skipped), 'failed': failed, 'skipped': skipped}
    # node --test: "# pass 5", "# fail 1" (tap) or "ℹ pass 5" (spec)
    node = dict(re.findall(r'^[#ℹ]\s+(pass|fail|skipped|todo|cancelled)\s+(\d+)\s*$', text, re.M))
    if 'pass' in node:
        n = lambda key: int(node.get(key, 0))
        return {'passed': n('pass'), 'failed': n('fail') + n('cancelled'), 'skipped': n('skipped') + n('todo')}
    # mocha: "5 passing (20ms)", "1 failing", "2 pending", each on its own line
    mocha = dict((word, int(n)) for n, word in re.findall(r'^\s*(\d+) (passing|failing|pending)\b', text, re.M))
    if 'passing' in mocha or 'failing' in mocha:
        return {'passed': mocha.get('passing', 0), 'failed': mocha.get('failing', 0), 'skipped': mocha.get('pending', 0)}
    # playwright: one line per outcome, the count first ("  1 failed", "  5 passed (10.2s)")
    found = {}
    for n, word in re.findall(r'^\s*(\d+) (passed|failed|flaky|skipped|did not run|interrupted)\b', '\n'.join(text.splitlines()[-40:]), re.M):
        key = {'flaky': 'passed', 'did not run': 'skipped', 'interrupted': 'failed'}.get(word, word)
        found[key] = found.get(key, 0) + int(n)
    if found:
        return {key: found.get(key, 0) for key in ('passed', 'failed', 'skipped')}
    # pytest, jest ("Tests: 1 failed, 5 passed, 6 total"), vitest ("Tests  1 failed | 5 passed (6)"): the last line that
    # counts passes or failures
    lines = _SUMMARY.findall(text)
    if lines:
        found = {}
        for n, word in _COUNTED.findall(lines[-1]):
            key = _WORDS.get(word)
            if key:
                found[key] = found.get(key, 0) + int(n)
        if found:
            return {key: found.get(key, 0) for key in ('passed', 'failed', 'skipped')}
    # go test: one line per test with -v, else one per package
    go = re.findall(r'^\s*--- (PASS|FAIL|SKIP):', text, re.M)
    if go:
        return {'passed': go.count('PASS'), 'failed': go.count('FAIL'), 'skipped': go.count('SKIP')}
    return None


def _problems(text):
    m = None
    for m in re.finditer(r'✖ (\d+) problems? \((\d+) errors?, (\d+) warnings?\)', text):   # eslint
        pass
    if m:
        return _ints(errors=m.group(2), warnings=m.group(3))
    m = re.search(r'(\d+) errors?, (\d+) warnings?, \d+ (?:informations|notes)\s*$', text, re.M)        # pyright
    if m:
        return _ints(errors=m.group(1), warnings=m.group(2))
    found = re.findall(r'^Found (\d+) errors?\b', text, re.M)                                          # tsc, mypy, ruff
    if found:
        return {'errors': int(found[-1])}
    if re.search(r'^(?:Success: no issues found|All checks passed!|0 errors, 0 warnings)', text, re.M):
        return {'errors': 0}
    return None


def _tail(output):
    lines = [line.rstrip() for line in _ANSI.sub('', output or '').splitlines() if line.strip()]
    return '\n'.join(lines[-TAIL_LINES:]) or None


# ---------- environment problems ----------

# (id, label, pattern), in the order a banner lists them
ENVIRONMENT = tuple((key, label, re.compile(pattern, re.I | re.M)) for key, label, pattern in (
    ('docker-daemon', 'Docker is not running', r'Cannot connect to the Docker daemon|Is the docker daemon running|'
                                               r'docker daemon is not running|error during connect:.*docker'),
    ('docker-pools', 'Docker ran out of network address pools', r'all predefined address pools have been fully subnetted|'
                                                                r'could not find an available, non-overlapping IPv4 address pool'),
    ('container', 'A container is missing or its name is taken', r'No such container|container name "[^"]*" is already in use|'
                                                                 r'Conflict\. The container name'),
    ('port', 'A port is already in use', r'EADDRINUSE|address already in use|port is already allocated'),
    ('sandbox', 'The sandbox refused an operation (EPERM)', r'\bEPERM\b|Operation not permitted|PermissionError: \[Errno 1\]'),
    ('refused', 'A local service refused the connection', r'ECONNREFUSED|Connection refused|could not connect to server'),
    ('network', 'No network or DNS', r'Could not resolve host|getaddrinfo (?:ENOTFOUND|EAI_AGAIN)|Temporary failure in name '
                                     r'resolution|Network is unreachable|ENETUNREACH|nodename nor servname provided'),
    ('disk', 'The disk is full', r'ENOSPC|No space left on device'),
    ('missing-tool', 'A command is not installed', r'command not found|^\S+: not found$|is not recognized as an internal or external command'),
))
LABELS = {key: label for key, label, _ in ENVIRONMENT}


def environment(output):
    """[(id, the first line that shows it)] for each environment problem in a failed command's output."""
    text = _ANSI.sub('', output or '')
    found = []
    for key, _, pattern in ENVIRONMENT:
        m = pattern.search(text)
        if m:
            start = text.rfind('\n', 0, m.start()) + 1
            end = text.find('\n', m.end())
            line = text[start:end if end >= 0 else len(text)].strip()
            found.append((key, line[:LINE_CAP]))
    return found


# ---------- one transcript ----------

def _failed(ev):
    return bool(ev.get('error')) or ev.get('exit') not in (None, 0)


def _command(text):
    text = text or ''
    return text if len(text) <= COMMAND_CAP else text[:COMMAND_CAP] + '…'


def analyze(events):
    """{checks, call, environment} of one transcript's events.

    checks: [{index, command, kind, exit, failed, running, started, ended, duration_s, counts, tail}], in order; index
      is the event's place in the transcript (the conversation view's `ev-<index>`).
    call: the call not answered yet ({index, kind: command|tool, command, tool, check, started}), the newest when
      several run at once, or None.
    environment: [{index, command, problem, label, line}] for each problem a failed command met.
    """
    checks, problems, call = [], [], None
    for index, ev in enumerate(events):
        kind = ev.get('kind')
        if kind not in ('command', 'tool', 'edit'):
            continue
        if ev.get('running'):
            call = dict(index=index, kind=kind, command=_command(ev.get('command')) if kind == 'command' else None,
                        tool=ev.get('tool') or None, started=ev.get('t'),
                        check=classify(ev.get('command')) if kind == 'command' else None)
        if kind != 'command':
            continue
        command, check = ev.get('command') or '', classify(ev.get('command'))
        failed = not ev.get('running') and _failed(ev)
        if check:
            started, ended = ev.get('t'), ev.get('ended')
            checks.append(dict(index=index, command=_command(command), kind=check, exit=ev.get('exit'), failed=failed,
                               running=bool(ev.get('running')), started=started, ended=ended,
                               duration_s=round(ended - started, 3) if started is not None and ended is not None else None,
                               counts=counts(ev.get('output'), check), tail=_tail(ev.get('output')) if failed else None))
        if failed and not _readonly(command):
            for key, line in environment(ev.get('output')):
                problems.append(dict(index=index, command=_command(command), problem=key, label=LABELS[key], line=line))
    return dict(checks=checks, call=call, environment=problems)


# ---------- the brief's acceptance criteria ----------

_HEADING = re.compile(r'^(#{1,6})\s+acceptance criteria\s*:?\s*$', re.I)
_ANY_HEADING = re.compile(r'^(#{1,6})\s')
_FENCE = re.compile(r'^\s*(```|~~~)')
_CODE = re.compile(r'(`+)(.+?)\1')


def criteria(brief):
    """The check commands an `## Acceptance criteria` section of a brief names: in `code` spans or in its fenced
    blocks, each once, in order: [{command, kind}]."""
    found, seen, level, fenced = [], set(), None, False
    for line in (brief or '').splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        heading = None if fenced else _ANY_HEADING.match(line)
        if heading and level is not None and len(heading.group(1)) <= level:
            level = None
        if not fenced and _HEADING.match(line):
            level = len(_HEADING.match(line).group(1))
            continue
        if level is None:
            continue
        candidates = [line.strip().lstrip('$ ').strip()] if fenced else [m.group(2).strip() for m in _CODE.finditer(line)]
        for text in candidates:
            kind = classify(text)
            if kind and text not in seen:
                seen.add(text)
                found.append(dict(command=text, kind=kind))
    return found


def _tokens(command):
    out = []
    for part in parts(command):
        out.extend(re.sub(r'^python(?:\d+(?:\.\d+)?)?$', 'python', word.strip('\'"')) for word in part.split())
    return out


def coverage(wanted, ran):
    """Each wanted check against the commands that ran: `ran` when one has every word of it, `similar` when one is
    the same kind of check by the same program (the whole suite where one module was asked, say), else `missing`.
    ran = [(command, info)]; the matching run's info rides along as `by`."""
    out = []
    runs = [(set(_tokens(command)), (_tokens(command) or [None])[0], classify(command), info) for command, info in ran]
    for item in wanted:
        words = _tokens(item['command'])
        status, by = 'missing', None
        for tokens, program, kind, info in runs:
            if words and set(words) <= tokens:
                status, by = 'ran', info
                break
            if status == 'missing' and words and program == words[0] and kind == item['kind']:
                status, by = 'similar', info
        out.append(dict(item, status=status, by=by))
    return out
