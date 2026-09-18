"""Run the tests a change most likely broke, early, in a throwaway copy.

Read the deferral note in references/jev.md first: there is no safe window to run tests
inside a task worktree. An artifact created before the diff is captured reaches QA as if
the dwarf wrote it; one created after trips the fingerprint re-checks in `do_task` and
`merge_task`, which write INVALIDATED and block the merge. So this never touches the
worktree. It exports the reviewed commit to a disposable directory and runs there.

**This is early feedback, not verification, and not a speed-up.** The full suite still
runs at integration exactly as before, so this ADDS a run rather than replacing one. What
it buys is that a reviewer learns "your diff breaks test_x" while it can still act on
that, instead of the failure surfacing after the wave. Anyone reading this expecting the
wall-clock lever the plan described should know the lever was not available: it required
replacing the full verification, which the fingerprint rules forbid.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import threshold
from .client import ask, noul
from .questions import test_relevance

CANDIDATE_CAP = 100
HEADER_LINES = 12

# Two conditions, not one. Matching "anything under tests/" pulled in README.md, JSON
# fixtures and benchmark output -- files no runner will execute, each costing a question
# and a slot under the cap.
_TEST_PATH_RE = re.compile(r'(^|/)(tests?|spec|specs|__tests__)/'
                           r'|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$')
_CODE_SUFFIXES = ('.py', '.go', '.js', '.jsx', '.ts', '.tsx', '.rb', '.rs', '.java',
                  '.kt', '.php', '.sh', '.mjs', '.cjs')

# Copied rather than rebuilt when present. Rebuilding a dependency tree per task would
# cost far more than the feedback is worth; a symlink keeps it near-free.
_DEP_DIRS = ('node_modules', '.venv', 'venv', 'vendor')


def candidate_tests(repo) -> list[str]:
    """Tracked test files, sorted and capped, from the repo's own index."""
    try:
        out = subprocess.run(['git', '-C', str(repo), 'ls-files', '-z'],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    found = sorted(name for name in out.stdout.split('\0')
                   if name and name.endswith(_CODE_SUFFIXES) and _TEST_PATH_RE.search(name))
    return found[:CANDIDATE_CAP]


def _header(repo: Path, rel: str) -> str:
    try:
        with (repo / rel).open(errors='replace') as handle:
            return ''.join(next(handle, '') for _ in range(HEADER_LINES))
    except OSError:
        return ''


def select(repo, *, task: str, changed: list, candidates: list, run_dir=None,
           config: dict | None = None):
    """The tests worth running for this change, most likely first.

    Returns None when no judgment could be made. The threshold is `tests_act`'s sibling
    `gate_warn` deliberately NOT reused here -- see the caller, which uses the measured
    per-repo value when one exists and otherwise runs nothing rather than guess.
    """
    candidates = [c for c in (candidates or []) if c]
    if not candidates or not (task or '').strip():
        return None
    repo_path = Path(repo)
    questions = {f't{i}': test_relevance(name, _header(repo_path, name))
                 for i, name in enumerate(candidates)}
    state = dict(task=task, changed_files=list(changed or []))
    result = ask(state, questions, site='jev-test-subset', run_dir=run_dir, config=config)
    if result is None:
        return None
    scored = []
    for i, name in enumerate(candidates):
        probability = noul(result, f't{i}')
        if probability is not None:
            scored.append((probability, name))
    scored.sort(reverse=True)
    return scored


def export(repo, commit: str, destination) -> bool:
    """Copy one commit's tree into a throwaway directory. Never touches the worktree."""
    destination = Path(destination)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(['git', '-C', str(repo), 'archive', commit],
                                 capture_output=True, timeout=120)
        if archive.returncode != 0:
            return False
        extract = subprocess.run(['tar', '-x', '-C', str(destination)],
                                 input=archive.stdout, capture_output=True, timeout=120)
        if extract.returncode != 0:
            return False
    except (OSError, subprocess.SubprocessError):
        return False

    # Dependencies are not in the tree. Link them so the export can actually run; a
    # missing link simply means the command fails and the caller reports nothing.
    for name in _DEP_DIRS:
        source = Path(repo) / name
        target = destination / name
        if source.is_dir() and not target.exists():
            try:
                os.symlink(source, target)
            except OSError:
                pass
    return True


def cleanup(destination) -> None:
    shutil.rmtree(str(destination), ignore_errors=True)
