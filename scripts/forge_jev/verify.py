"""Verify-command discovery and failure triage: select, never generate, and judge flakes."""
from __future__ import annotations

import json
from pathlib import Path
import re

from . import enabled, load_config, threshold
from .client import ask, choice, noul, score
from .questions import failure_triage, runs_tests, verify_runtime, verify_selection

CANDIDATE_CAP = 25  # keeps one discover() request small; deterministic order means the
                    # cap always drops the same (least-likely) tail, never a random one.
LOG_TAIL_CHARS = 4000  # the failure signature (assertion, traceback) is at the end of a
                       # log; the setup/build noise at the start is not needed to triage it.

_PKG_SCRIPT_RE = re.compile(r'^(test|check|ci|verify)(?:[:\-]|$)', re.I)
_MAKE_TARGETS = ('test', 'tests', 'check', 'verify', 'ci')
_RUNNER_DIR_RE = re.compile(r'(test|check|ci|verify)', re.I)
_WORKFLOW_RUN_RE = re.compile(r'^\s*(?:-\s*)?run:\s*(.+?)\s*$')
_WORKFLOW_TEST_HINT_RE = re.compile(
    r'\b(pytest|npm (?:test|run\s+test\S*)|yarn (?:test|run\s+test\S*)|go test|cargo test|'
    r'make test|tox\b|jest|vitest|mvn test|gradle test|rspec|phpunit|ctest|go\s+vet\s+&&\s+go\s+test)\b',
    re.I)
_MEMORY_CMD_RES = (
    re.compile(r'\bpytest(?:\s+-{1,2}\S+)*'),
    re.compile(r'\bmake\s+\w[\w-]*'),
    re.compile(r'\bnpm run\s+[\w:_-]+'),
    re.compile(r'\bnpm test\b'),
    re.compile(r'\byarn(?:\s+run)?\s+[\w:_-]+'),
    re.compile(r'\bgo test(?:\s+\S+)*'),
    re.compile(r'\bcargo test(?:\s+\S+)*'),
    re.compile(r'\btox\b'),
    re.compile(r'\bnox\b'),
)


def _add(found: list[dict], seen: set[str], command: str, source: str, evidence: str) -> None:
    command = command.strip()
    if not command or command in seen or len(found) >= CANDIDATE_CAP:
        return
    seen.add(command)
    found.append(dict(command=command, source=source, evidence=evidence))


def _forge_verify(repo: Path, found, seen) -> None:
    path = repo / '.forge' / 'verify'
    if not path.is_file():
        return
    text = path.read_text(errors='replace').strip()
    if text:
        _add(found, seen, text, 'forge/verify', str(path))


def _package_json(repo: Path, found, seen) -> None:
    path = repo / 'package.json'
    if not path.is_file():
        return
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    scripts = data.get('scripts') if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return
    names = [n for n in scripts if isinstance(n, str) and _PKG_SCRIPT_RE.match(n)]
    order = {name: i for i, name in enumerate(names)}
    # A bare 'test' is the most likely full-suite entry point; put it and its siblings
    # ('check', 'ci', 'verify') first, then the rest in file order for determinism.
    names = sorted(names, key=lambda n: (n not in ('test', 'check', 'ci', 'verify'), order[n]))
    for name in names:
        _add(found, seen, f'npm run {name}', 'package.json', str(path))


def _makefile(repo: Path, found, seen) -> None:
    path = repo / 'Makefile'
    if not path.is_file():
        return
    for line in path.read_text(errors='replace').splitlines():
        m = re.match(r'^([A-Za-z0-9_-]+)\s*:(?!=)', line)
        if m and m.group(1).lower() in _MAKE_TARGETS:
            _add(found, seen, f'make {m.group(1)}', 'Makefile', str(path))


def _tox_and_nox(repo: Path, found, seen) -> None:
    tox = repo / 'tox.ini'
    if tox.is_file():
        _add(found, seen, 'tox', 'tox.ini', str(tox))
    nox = repo / 'noxfile.py'
    if nox.is_file():
        _add(found, seen, 'nox', 'noxfile.py', str(nox))


# test_foo.py / foo_test.py are individual test modules, not suite runners. Offering
# each one would bury the single real runner under a dozen near-duplicates and eat the
# candidate cap on a repo with many test files.
_MODULE_RE = re.compile(r'^test_.*\.py$|^.*_test\.py$')


def _runner_scripts(repo: Path, found, seen) -> None:
    for sub in ('tests', 'scripts', 'bin'):
        directory = repo / sub
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file() or not _RUNNER_DIR_RE.search(entry.name):
                continue
            try:
                executable = entry.stat().st_mode & 0o111 != 0
            except OSError:
                continue
            rel = entry.relative_to(repo).as_posix()
            if executable and entry.suffix in ('', '.sh', '.py'):
                _add(found, seen, rel, sub + '/', str(entry))
            # A runner script that was never chmod +x is still the project's real
            # verify command -- forge's own tests/check.sh is mode 644 and its README
            # says to run `bash tests/check.sh`. Naming the interpreter for a file that
            # exists is still selection, not invention, so the candidate is offered and
            # Jev decides. Skipping it left repos like that permanently UNVERIFIED.
            elif not executable and entry.suffix in ('.sh', '.py') and not _MODULE_RE.match(entry.name):
                interpreter = 'bash' if entry.suffix == '.sh' else 'python3'
                _add(found, seen, interpreter + ' ' + rel, sub + '/', str(entry))


def _pytest_configured(repo: Path, found, seen) -> None:
    candidates = (
        (repo / 'pytest.ini', True),
        (repo / 'tox.ini', '[pytest]'),
        (repo / 'setup.cfg', '[tool:pytest]'),
        (repo / 'pyproject.toml', '[tool.pytest.ini_options]'),
    )
    for path, marker in candidates:
        if not path.is_file():
            continue
        if marker is True:
            _add(found, seen, 'pytest -q', path.name, str(path))
            return
        try:
            if marker in path.read_text(errors='replace'):
                _add(found, seen, 'pytest -q', path.name, str(path))
                return
        except OSError:
            continue


def _go_and_cargo(repo: Path, found, seen) -> None:
    go = repo / 'go.mod'
    if go.is_file():
        _add(found, seen, 'go test ./...', 'go.mod', str(go))
    cargo = repo / 'Cargo.toml'
    if cargo.is_file():
        _add(found, seen, 'cargo test', 'Cargo.toml', str(cargo))


def _workflows(repo: Path, found, seen) -> None:
    directory = repo / '.github' / 'workflows'
    if not directory.is_dir():
        return
    for path in sorted(directory.glob('*.yml')) + sorted(directory.glob('*.yaml')):
        try:
            lines = path.read_text(errors='replace').splitlines()
        except OSError:
            continue
        # Plain-text scan on purpose (no YAML dependency): only single-line `run: <cmd>`
        # steps are recognised, which is the common case for a test invocation step. A
        # malformed file just yields no matches rather than raising.
        for line in lines:
            m = _WORKFLOW_RUN_RE.match(line)
            if not m:
                continue
            command = m.group(1).strip().strip('"\'')
            if command and command != '|' and _WORKFLOW_TEST_HINT_RE.search(command):
                _add(found, seen, command, 'workflow', str(path))


def _memory_verify(repo: Path, found, seen) -> None:
    path = repo / '.forge' / 'memory.md'
    if not path.is_file():
        return
    for line in path.read_text(errors='replace').splitlines():
        if 'verify |' not in line:
            continue
        text = line.split('verify |', 1)[1]
        # A memory entry is prose ("pytest -q runs the suite; make test also lints and
        # is 4x slower"), not a bare command -- pull out anything that looks like a
        # known runner invocation rather than treating the whole sentence as a command.
        for pattern in _MEMORY_CMD_RES:
            for match in pattern.finditer(text):
                _add(found, seen, match.group(0), 'memory:verify', str(path))


def candidates(repo) -> list[dict]:
    """Real, existing verify commands. Each: {'command': str, 'source': str, 'evidence': str}.

    Deterministic order, deduplicated, capped at CANDIDATE_CAP. Never executes anything --
    this only enumerates what plausibly exists so Jev can select among real options.
    """
    repo = Path(repo)
    found: list[dict] = []
    seen: set[str] = set()
    for step in (_forge_verify, _package_json, _makefile, _tox_and_nox, _runner_scripts,
                _pytest_configured, _go_and_cargo, _workflows, _memory_verify):
        if len(found) >= CANDIDATE_CAP:
            break
        step(repo, found, seen)
    return found[:CANDIDATE_CAP]


def _repo_files(repo: Path, limit: int = 200) -> list[str]:
    try:
        entries = sorted(e.name for e in repo.iterdir())
    except OSError:
        return []
    return entries[:limit]


# The headings forge-memory.sh:93 renders. Both readers below looked for the LEDGER row
# format instead -- `verify |` and `trap |` -- which is what ledger.tsv holds, not what
# `rebuild` writes into memory.md. No line in a rendered memory.md has ever contained
# either, so both lists were empty for every repo, always, while jev.md documented them
# as working. Verification discovery never saw a recorded verify command, and
# known_traps never corroborated anything -- which made the flake re-run unreachable,
# since its third condition is a trap sharing a distinctive token with the failure.
_MEMORY_SECTIONS = {'verify': '## Verify', 'trap': '## Known traps',
                    'finding': '## Recurring QA findings'}


def _memory_facts(repo: Path, category: str) -> list[str]:
    """The `- ` bullets under one heading of a rendered .forge/memory.md."""
    heading = _MEMORY_SECTIONS.get(category)
    path = Path(repo) / '.forge' / 'memory.md'
    if not heading or not path.is_file():
        return []
    try:
        lines = path.read_text(errors='replace').splitlines()
    except OSError:
        return []
    facts, inside = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('## '):
            inside = stripped == heading
            continue
        if inside and stripped.startswith('- '):
            fact = stripped[2:].strip()
            # `rebuild` prefixes a recurring finding with its count ("3x: ..."), which is
            # bookkeeping about the fact rather than part of it.
            if fact[:1].isdigit() and 'x: ' in fact[:6]:
                fact = fact.split('x: ', 1)[1].strip()
            if fact:
                facts.append(fact)
    return facts


def _memory_verify_lines(repo: Path) -> list[str]:
    return _memory_facts(repo, 'verify')


def known_traps(repo) -> list[str]:
    """Recorded 'trap' facts from .forge/memory.md (flaky tests, generated files, sharp
    edges), for the CLI to hand to triage() -- see references/memory.md."""
    path = Path(repo) / '.forge' / 'memory.md'
    if not path.is_file():
        return []
    try:
        lines = path.read_text(errors='replace').splitlines()
    except OSError:
        return []
    return _memory_facts(path.parent.parent, 'trap')


# Enough of an evidence file to tell a test command from a build one, and no more: the
# request already carries one Choice and two Nouls per candidate.
EVIDENCE_CHARS = 400


def _evidence_excerpt(candidate: dict) -> str:
    """What the command actually runs, read off the file that produced the candidate.

    Without this the model is handed the string 'make test' plus the repo's top-level
    file list and asked whether it runs the suite. It cannot know: the answer is inside
    the Makefile. Measured on a repo with a textbook `test:` target, discovery picked
    `make test` at 0.58 while leaving 0.40 on `none`, for a Choice confidence of 0.37
    against a 0.70 gate -- so it declined, and a verifiable repo reported UNVERIFIED.
    """
    path = candidate.get('evidence') or ''
    try:
        text = Path(path).read_text(errors='replace')
    except OSError:
        return ''
    command = candidate.get('command') or ''
    # A make target's recipe is the only part of a Makefile that answers the question,
    # and it is rarely near the top of the file.
    if command.startswith('make ') and Path(path).name.lower().startswith('makefile'):
        target = command.split(None, 1)[1].strip()
        recipe, collecting = [], False
        for line in text.splitlines():
            if collecting:
                if line.startswith(('\t', ' ')) or not line.strip():
                    recipe.append(line.strip())
                    continue
                break
            if ':' in line and line.split(':')[0].strip() == target:
                collecting = True
        joined = '\n'.join(x for x in recipe if x)
        if joined:
            return joined[:EVIDENCE_CHARS]
    return text[:EVIDENCE_CHARS]


def discover(repo, *, config=None, run_dir=None) -> dict | None:
    """Ask Jev which candidate actually verifies this repo. None when Jev is
    unavailable, no candidates exist, or it picks `none`.
    -> {'command', 'confidence', 'runs_tests', 'runtime_level', 'source', 'evidence'}
    """
    config = config or load_config()
    if not enabled('tests', config=config):
        return None
    repo_path = Path(repo)
    cand = candidates(repo_path)
    if not cand:
        return None
    commands = [c['command'] for c in cand]

    # One request covers the Choice and a runs_tests/verify_runtime pair per candidate --
    # they all evaluate in parallel server-side, so asking about every candidate here
    # costs exactly one call, not one per candidate.
    questions = {'choice': verify_selection(commands)}
    for i in range(len(cand)):
        questions[f'runs_{i}'] = runs_tests(commands[i])
        questions[f'runtime_{i}'] = verify_runtime(commands[i])

    # The candidates carry where they came from and what they run. Sending only the
    # command strings threw away the single thing that answers the question being asked.
    described = [dict(command=c['command'], source=c.get('source', ''),
                      runs=_evidence_excerpt(c)) for c in cand]
    state = dict(repo_files=_repo_files(repo_path), candidates=described,
                memory_verify=_memory_verify_lines(repo_path))
    result = ask(state, questions, site='jev-verify-discover', run_dir=run_dir, config=config)
    if result is None:
        return None

    picked, confidence = choice(result, 'choice')
    if picked is None or picked == 'none':
        return None
    if confidence < threshold('tests_act', config=config):
        return None
    try:
        idx = commands.index(picked)
    except ValueError:
        # Choice is constrained to the options we sent, so this should not happen -- but
        # a command we never enumerated must never be reported as "selected", so bail.
        return None

    runs_probability = noul(result, f'runs_{idx}')
    if runs_probability is None or runs_probability < 0.5:
        # A command that only builds or lints must not be presented as verification,
        # even if the Choice rubric itself scored it confidently.
        return None

    runtime_score, _runtime_conf = score(result, f'runtime_{idx}')
    runtime_levels = questions[f'runtime_{idx}']['criteria']
    runtime_level = None
    if runtime_score is not None and 0 <= int(runtime_score) < len(runtime_levels):
        runtime_level = runtime_levels[int(runtime_score)]

    picked_candidate = cand[idx]
    return dict(command=picked, confidence=confidence, runs_tests=runs_probability,
               runtime_level=runtime_level, source=picked_candidate['source'],
               evidence=picked_candidate['evidence'])


# A token is distinctive enough to corroborate only if it looks like an identifier or a
# path -- `test_timeout`, `tests/test_api.py`, `CamelCase` -- rather than an English word
# that any failure log would contain.
_TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_./:-]{3,}')


def _distinctive(text: str) -> set[str]:
    tokens = set()
    for match in _TOKEN_RE.findall(text or ''):
        # Split on path and node-id separators: a trap says `test_api.py::test_timeout`
        # while the log says `tests/test_api.py::test_timeout`, which never compare equal
        # as whole tokens. Atoms do.
        for token in re.split(r'[/:]+', match):
            token = token.strip('.-_')
            if len(token) < 4:
                continue
            # A bare short word like `tests` matches every repo; require either an
            # identifier/path shape or enough length to be specific.
            if '_' in token or '.' in token or len(token) >= 8:
                tokens.add(token)
    return tokens


def corroborating_traps(log_tail: str, traps) -> list[str]:
    """Traps that share a distinctive token with the failure output.

    The re-run gate needs evidence that THIS failure matches a recorded trap, not merely
    that the repo has recorded some trap at some point. Without that, one flaky test
    noted months ago would excuse every future failure in the repo -- which is precisely
    how a real regression gets waved through.
    """
    haystack = _distinctive(log_tail)
    return [trap for trap in (traps or []) if _distinctive(trap) & haystack]


def triage(repo, *, command, log_tail, traps, config=None, run_dir=None) -> dict | None:
    """Classify a verification failure. -> {'verdict', 'confidence'} or None."""
    config = config or load_config()
    if not enabled('tests', config=config):
        return None
    tail = (log_tail or '')[-LOG_TAIL_CHARS:]
    # The diff is never sent: recognising a flake needs the failure signature (timeout,
    # ordering, port-in-use, a named trap), not the change itself, and the diff is a
    # larger payload for no benefit here.
    state = dict(command=command, log_tail=tail, known_traps=list(traps or []))
    result = ask(state, {'triage': failure_triage(list(traps or []))}, site='jev-verify-triage',
                run_dir=run_dir, config=config)
    if result is None:
        return None
    verdict, confidence = choice(result, 'triage')
    if verdict is None:
        return None
    matched = corroborating_traps(tail, traps)
    return dict(verdict=verdict, confidence=confidence,
                corroborated=bool(matched), corroborating_traps=matched)
