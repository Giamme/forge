"""Plan-time semantic wave coupling.

`compute_waves` bounds parallelism structurally: two tasks share a wave only when their
declared `files` are disjoint. Disjointness is blind to the conflict where task A changes
an interface task B consumes -- both diffs pass review against their own baseline, and the
merge is broken (see questions.wave_coupling). This asks about every pair `compute_waves`
would actually run concurrently, before the wave runs, so the warning still lands while the
plan can change.

Nothing here writes to tasks.tsv or waves.tsv. Scoring is advisory and silent in shadow
mode, exactly like routing.py and gates.py.
"""
from __future__ import annotations

from pathlib import Path

from . import threshold
from .client import ask, noul
from .questions import wave_coupling

# A wave of 12 tasks is 66 pairs; a plan-time gate must not become the slowest thing in
# `forge plan`. Sorted before truncation (see pairs()) so the same plan always produces
# the same capped set.
MAX_PAIRS = 60


def parse_tasks(plan: Path) -> dict[str, dict]:
    """id -> {id, title, files, difficulty}, skipping comments and malformed rows."""
    path = plan / 'tasks.tsv'
    tasks = {}
    try:
        lines = path.read_text(errors='replace').splitlines()
    except OSError:
        return tasks
    for line in lines:
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            continue
        task_id, _deps, difficulty, files, _dwarf, _qa, title = parts[:7]
        if not task_id:
            continue
        tasks[task_id] = dict(id=task_id, title=title, files=files, difficulty=difficulty)
    return tasks


def parse_waves(plan: Path) -> dict[str, list[str]]:
    """wave number (as it appears in waves.tsv) -> ordered list of task ids."""
    path = plan / 'waves.tsv'
    waves: dict[str, list[str]] = {}
    try:
        lines = path.read_text(errors='replace').splitlines()
    except OSError:
        return waves
    for line in lines:
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        wave, task_id = parts[0], parts[1]
        if not wave or not task_id:
            continue
        waves.setdefault(wave, []).append(task_id)
    return waves


def pairs(tasks: dict, waves: dict) -> list[tuple[str, str]]:
    """Every same-wave task pair with both ids known, capped at MAX_PAIRS.

    Sorted before truncation, same reasoning as gates.candidates: an unstable slice would
    make the same plan warn about a different subset of pairs on two runs.
    """
    found = []
    for ids in waves.values():
        ordered = sorted(i for i in ids if i in tasks)
        for i, first in enumerate(ordered):
            for second in ordered[i + 1:]:
                found.append((first, second))
    found.sort()
    return found[:MAX_PAIRS]


def coupled_pairs(repo, *, tasks, waves, run_dir=None, config: dict | None = None):
    """Same-wave task pairs Jev thinks would still conflict once merged.

    `tasks` is a list of dicts with id/title/files/difficulty; `waves` maps a wave number
    to the list of task ids placed in it -- the same shapes `compute_waves` produces.
    Returns [(id_a, id_b, probability), ...] at or above threshold('gate_warn'), highest
    probability first. Returns [] when there is nothing to warn about and None when no
    judgment could be made, so a caller can tell "no coupling found" from "Jev did not
    answer".
    """
    by_id = {t['id']: t for t in tasks if isinstance(t, dict) and t.get('id')}
    candidate_pairs = pairs(by_id, waves)
    if not candidate_pairs:
        return []

    questions = {}
    for i, (first, second) in enumerate(candidate_pairs):
        first_desc = f"{first} ({by_id[first]['title']}) files: {by_id[first]['files']}"
        second_desc = f"{second} ({by_id[second]['title']}) files: {by_id[second]['files']}"
        questions[f'p{i}'] = wave_coupling(first_desc, second_desc)

    # No file contents in state: the rubric asks about interfaces between tasks, not
    # implementations, and sending diffs nobody wrote yet would only add noise and cost.
    # Only the tasks that actually appear in a candidate pair are sent, not the whole plan.
    involved = sorted({task_id for pair in candidate_pairs for task_id in pair})
    state = dict(tasks=[dict(id=by_id[i]['id'], title=by_id[i]['title'],
                             files=by_id[i]['files']) for i in involved])
    result = ask(state, questions, site='jev-coupling', run_dir=run_dir, config=config)
    if result is None:
        return None

    warn_at = threshold('gate_warn', config=config)
    found = []
    for i, (first, second) in enumerate(candidate_pairs):
        probability = noul(result, f'p{i}')
        if probability is not None and probability >= warn_at:
            found.append((first, second, round(probability, 3)))
    found.sort(key=lambda item: item[2], reverse=True)
    return found
