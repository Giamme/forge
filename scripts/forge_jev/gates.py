"""Plan-time `files` drift prediction.

`decompose.md` calls the `files` column "a promise, not a prediction": a task that edits
a file it never declared breaks wave disjointness, which is the only thing bounding
parallelism. Forge already detects this, but afterwards, in `drift.txt` -- by which point
the wave has run. Asking at plan time converts a post-mortem into a warning while the
plan can still change.

Measured before shipping (see references/jev.md): at the default `gate_warn` of 0.60 this
rubric scored precision 1.000 on one repo and 0.934 on another, over 50 commits. Precision
is the number that matters here -- a warning that fires on files nobody edits teaches
people to ignore every future warning.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import threshold
from .client import ask, noul
from .questions import drift_prediction

# One request per task holds this many questions comfortably, and a plan-time gate must
# not turn into the slowest thing in `forge plan`.
CANDIDATE_CAP = 120

# Files nobody means when they say "did this task edit something it did not declare".
_SKIP_PREFIXES = ('.git/', '.forge/', 'node_modules/', 'vendor/', 'target/', 'dist/',
                  'build/', '.venv/', 'venv/', '__pycache__/')
_SKIP_SUFFIXES = ('.lock', '.min.js', '.map', '.png', '.jpg', '.jpeg', '.gif', '.svg',
                  '.ico', '.pdf', '.zip', '.woff', '.woff2', '.ttf')
# Lockfiles whose name the suffix list cannot catch. They are generated, enormous, and
# genuinely do change whenever dependencies do, which is what would make a drift warning
# about them both technically true and completely useless.
_SKIP_NAMES = ('package-lock.json', 'npm-shrinkwrap.json', 'composer.lock',
               'Gemfile.lock', 'go.sum', 'pnpm-lock.yaml', 'yarn.lock')


def _tracked(repo: Path) -> list[str]:
    try:
        out = subprocess.run(['git', '-C', str(repo), 'ls-files', '-z'],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [name for name in out.stdout.split('\0') if name]


def candidates(repo, declared: str) -> list[str]:
    """Tracked files the task does NOT declare, capped.

    Sorted before truncation so the slice is deterministic: an unstable candidate set
    would make the same plan warn differently on two runs.
    """
    owned = set((declared or '').replace(',', ' ').split())
    found = []
    for name in sorted(_tracked(Path(repo))):
        if name in owned:
            continue
        if name.startswith(_SKIP_PREFIXES) or name.endswith(_SKIP_SUFFIXES):
            continue
        if name.rsplit('/', 1)[-1] in _SKIP_NAMES:
            continue
        found.append(name)
        if len(found) >= CANDIDATE_CAP:
            break
    return found


def predict_drift(repo, *, goal: str, task_id: str, title: str, declared: str,
                  approach: str = '', prompt: str = '', run_dir=None,
                  config: dict | None = None):
    """Undeclared files this task looks likely to edit, highest probability first.

    Returns [] when there is nothing to say and None when no judgment could be made, so
    a caller can tell "no drift predicted" from "Jev did not answer".
    """
    pool = candidates(repo, declared)
    if not pool:
        return []
    questions = {f'f{i}': drift_prediction(name) for i, name in enumerate(pool)}
    # Which files a task will really touch is stated in its requirements far more often
    # than in its title -- "extend tests/test_events.py" is in the prompt, not the title.
    state = dict(goal=goal, task=dict(id=task_id, title=title, declared_files=declared),
                 prompt=prompt, approach=approach)
    result = ask(state, questions, site='jev-drift', run_dir=run_dir, config=config)
    if result is None:
        return None

    warn_at = threshold('gate_warn', config=config)
    predicted = []
    for i, name in enumerate(pool):
        probability = noul(result, f'f{i}')
        if probability is not None and probability >= warn_at:
            predicted.append((name, round(probability, 3)))
    predicted.sort(key=lambda item: item[1], reverse=True)
    return predicted
