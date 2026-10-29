"""Verify-command discovery: select, never generate."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

from . import enabled, load_config, threshold
from .client import ask, choice, noul, probabilities, score
from .questions import runs_tests, verify_runtime, verify_selection

CANDIDATE_CAP = 25  # keeps one discover() request small; deterministic order means the
                    # cap always drops the same (least-likely) tail, never a random one.

_PKG_SCRIPT_RE = re.compile(r'^(test|check|ci|verify)(?:[:\-]|$)', re.I)
_MAKE_TARGETS = ('test', 'tests', 'check', 'verify', 'ci')
_RUNNER_DIR_RE = re.compile(r'(test|check|ci|verify)', re.I)
_WORKFLOW_RUN_RE = re.compile(r'^\s*(?:-\s*)?run:\s*(.+?)\s*$')
_WORKFLOW_TEST_HINT_RE = re.compile(
    r'\b(pytest|npm (?:test|run\s+test\S*)|yarn (?:test|run\s+test\S*)|go test|cargo test|'
    r'make test|tox\b|jest|vitest|mvn test|gradle test|rspec|phpunit|ctest|go\s+vet\s+&&\s+go\s+test)\b',
    re.I)
# A remembered command is read off a rendered fact two ways (#42). A code span is the command as
# its author wrote it, so its content is taken whole, quotes and option values included. Prose is
# scanned for a runner and the arguments after it, and an argument is a flag, a path or a quoted
# string: a plain word ends the command, so "cargo test --workspace runs all tests" names
# `cargo test --workspace`. The rendered source tag ("[tests/]") matches none of these forms.
_CODE_SPAN_RE = re.compile(r'`([^`\n]+)`')
_PROSE_ARG = r'''(?:-{1,2}[^\s`'"\[\]]+|[\w.-]*/[\w./-]*|"[^"\n]*"|'[^'\n]*')'''
_MEMORY_HEADS = (
    r'\bpytest\b',
    r'\bmake\s+\w[\w-]*',
    r'\bnpm run\s+[\w:_-]+',
    r'\bnpm test\b',
    r'\byarn(?:\s+run)?\s+[\w:_-]+',
    r'\bgo test\b',
    r'\bcargo test\b',
    r'\btox\b',
    r'\bnox\b',
    r'\bbash\s+[\w./-]+\.sh\b',
    r'\bpython3?\s+[\w./-]+\.py\b',
)
_MEMORY_HEAD_RES = tuple(re.compile(head) for head in _MEMORY_HEADS)
_MEMORY_PROSE_RES = tuple(re.compile(head + r'(?:\s+' + _PROSE_ARG + r')*') for head in _MEMORY_HEADS)
# Punctuation that ends the sentence a command sits in: "pytest -q." names `pytest -q`, but
# "go test ./..." keeps its dots.
_SENTENCE_END_RE = re.compile(r'(?:[,;:!?)]|(?<=[^.])\.)+$')

# Extension -> language, for languages_present (#42). Anything else is not code here.
_LANGUAGE_BY_EXTENSION = {
    'py': 'python', 'rs': 'rust', 'go': 'go', 'rb': 'ruby', 'swift': 'swift',
    'js': 'javascript', 'jsx': 'javascript', 'ts': 'javascript', 'tsx': 'javascript',
    'mjs': 'javascript', 'cjs': 'javascript',
    'java': 'jvm', 'kt': 'jvm',
    'c': 'c', 'cc': 'c', 'cpp': 'c', 'h': 'c', 'hpp': 'c',
}
# A language is present when it has this many code files and this share of them.
LANGUAGE_MIN_FILES = 5
LANGUAGE_MIN_SHARE = 0.10
# Words that name the language a command runs the tests of (#42). Whole words only.
_COVERAGE_RES = (
    (re.compile(r'\bcargo\b', re.I), 'rust'),
    (re.compile(r'\b(?:pytest|python3?|tox|nox)\b', re.I), 'python'),
    (re.compile(r'\b(?:npm|pnpm|yarn|npx|node|jest|vitest)\b', re.I), 'javascript'),
    (re.compile(r'\bgo[ \t]+(?:test|vet)\b', re.I), 'go'),
    (re.compile(r'\b(?:bundle|rake|rspec)\b', re.I), 'ruby'),
    (re.compile(r'\b(?:gradle|mvn)\b', re.I), 'jvm'),
    (re.compile(r'\bswift\b', re.I), 'swift'),
    (re.compile(r'\b(?:ctest|cmake)\b', re.I), 'c'),
)


def _add(found: list[dict], seen: set[str], command: str, source: str, evidence: str,
         excerpt: str = '') -> None:
    command = command.strip()
    if not command or command in seen or len(found) >= CANDIDATE_CAP:
        return
    seen.add(command)
    entry = dict(command=command, source=source, evidence=evidence)
    if excerpt:
        entry['excerpt'] = excerpt
    found.append(entry)


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


def _remembered_commands(fact: str) -> list[str]:
    """The runner invocations one memory fact names (#42): code spans first, then the prose."""
    commands = []
    for span in _CODE_SPAN_RE.findall(fact):
        if any(pattern.search(span) for pattern in _MEMORY_HEAD_RES):
            commands.append(span.strip())
    # The prose with each span blanked out, so a span's text is not read a second time.
    prose = _CODE_SPAN_RE.sub(lambda match: ' ' * len(match.group(0)), fact)
    for pattern in _MEMORY_PROSE_RES:
        for match in pattern.finditer(prose):
            commands.append(_SENTENCE_END_RE.sub('', match.group(0)))
    return commands


def _memory_verify(repo: Path, found, seen) -> None:
    path = repo / '.forge' / 'memory.md'
    for fact in _memory_facts(repo, 'verify'):
        # A memory fact is prose ("pytest -q runs the suite; make test also lints and
        # is 4x slower"), not a bare command -- pull out anything that looks like a
        # known runner invocation rather than treating the whole sentence as a command.
        for command in _remembered_commands(fact):
            _add(found, seen, command, 'memory', str(path), excerpt=fact)


def candidates(repo) -> list[dict]:
    """Real, existing verify commands. Each: {'command': str, 'source': str, 'evidence': str},
    and 'excerpt' for a memory candidate (the memory fact that named it).

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


# The headings forge-memory.sh:93 renders. The reader below looked for the LEDGER row
# format instead -- `verify |` and `trap |` -- which is what ledger.tsv holds, not what
# `rebuild` writes into memory.md. No line in a rendered memory.md has ever contained
# either, so the list was empty for every repo, always, while jev.md documented it as
# working. Verification discovery never saw a recorded verify command.
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
    # A memory candidate's evidence is memory.md, whose head describes every fact, not
    # just the one that named the command. The fact itself is what it runs.
    if candidate.get('excerpt'):
        return candidate['excerpt'][:EVIDENCE_CHARS]
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


def languages_present(repo) -> list[str]:
    """The languages a repo has, from its tracked files (#42), sorted. A language counts at
    LANGUAGE_MIN_FILES files and LANGUAGE_MIN_SHARE of the code files. Empty when the repo
    is not a git repository."""
    try:
        listed = subprocess.run(['git', '-C', str(repo), 'ls-files', '-z'],
                                capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    counts: dict[str, int] = {}
    for name in listed.decode(errors='replace').split('\0'):
        language = _LANGUAGE_BY_EXTENSION.get(Path(name).suffix[1:].lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
    total = sum(counts.values())
    return sorted(language for language, n in counts.items()
                  if n >= LANGUAGE_MIN_FILES and n / total >= LANGUAGE_MIN_SHARE)


def languages_covered(command, evidence) -> list[str]:
    """The languages whose tests a command runs, judged from the command and the excerpt of
    what it runs (#42), sorted. Empty when nothing is recognised."""
    text = f'{command}\n{evidence}'
    return sorted({language for pattern, language in _COVERAGE_RES if pattern.search(text)})


def discover(repo, *, config=None, run_dir=None) -> dict | None:
    """Ask Jev which candidate actually verifies this repo. None when Jev is
    unavailable, no candidates exist, or it picks `none`.
    -> {'command', 'confidence', 'verifiable', 'runs_tests', 'runtime_level', 'source',
        'evidence', 'coverage': {'present', 'covered'}}
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

    # Gate on "is this repo verifiable at all", not on "which command wins".
    #
    # Choice confidence answers the second question, and for a selection among several
    # ACCEPTABLE options it is the wrong quantity: a repo offering both `bash
    # tests/check.sh` and `python3 -m unittest discover -s tests` splits the mass
    # 0.62/0.31 with only 0.07 on `none`, for a confidence of 0.49 against a 0.70 gate.
    # The model is 93% sure the repo is verifiable and forge reported UNVERIFIED --
    # because two right answers look exactly like doubt to a rubric that measures
    # agreement on one.
    #
    # 1 - P(none) is the quantity `tests_act` was always meant to gate. A split between
    # correct commands leaves it high; genuine doubt piles onto `none` and drops it.
    # Ties are then broken by the winner, which is what Choice is for.
    distribution = probabilities(result, 'choice')
    act_at = threshold('tests_act', config=config, site='jev-verify-discover')
    if distribution:
        if (1.0 - distribution.get('none', 0.0)) < act_at:
            return None
    elif confidence < act_at:
        # No distribution in the response: fall back to the old gate rather than treat a
        # missing field as certainty.
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
    # `verifiable` is the quantity the gate actually used; `confidence` is kept beside it
    # because they answer different questions and a future re-measurement needs both.
    verifiable = (1.0 - distribution.get('none', 0.0)) if distribution else None
    # Coverage (#42) only informs the announcement: the pick stands, and the command's own
    # run still decides verification. `described` already holds the pick's excerpt.
    coverage = dict(present=languages_present(repo_path),
                    covered=languages_covered(picked, described[idx]['runs']))
    return dict(command=picked, confidence=confidence, verifiable=verifiable,
               runs_tests=runs_probability,
               runtime_level=runtime_level, source=picked_candidate['source'],
               evidence=picked_candidate['evidence'], coverage=coverage)

