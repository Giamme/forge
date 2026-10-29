"""Plan-time forward-dependency check.

`compute_waves` orders tasks by their declared `deps` and `files`. It cannot see that an
earlier-wave task is written against an interface a later-wave task builds (see
questions.forward_dependency). This asks about the cross-wave pairs nothing declares, before
the waves run, so the warning still lands while the plan can change.

Nothing here writes to tasks.tsv or waves.tsv: scoring is advisory and silent in shadow mode,
the same way the other plan-time gates are. When the gate is `on`, `cmd_coupling` (cli.py)
turns each warned pair into a dependency through plan_gates.add_dep; the waves are recomputed
by the caller. The same-wave rubric is gone.
"""
from __future__ import annotations

from pathlib import Path

from . import threshold
from .client import ask, noul
from .questions import forward_dependency

# How many pairs a plan may warn about, however many clear the threshold.
#
# Rank is the signal here, not the absolute value. Over 11 pairs with known outcomes the
# genuinely coupled ones took 4 of the top 5 places, but the absolute scores moved with
# the plan: the same rubric put the whole distribution at 0.09-0.27 in one measurement
# and 0.20-0.38 on the live 8-task plan. At a fixed 0.20 that live plan warned about 12
# of its 21 pairs, and a gate that fires on more than half of everything teaches people
# to ignore it -- which is the argument this file's own threshold comment makes about
# using one number for several rubrics.
#
# So the threshold decides whether anything is worth saying and the ordering decides
# what gets said. Three is what fits in a plan summary a human will actually read.
MAX_WARNINGS = 3


# A plan-time gate must not become the slowest thing in `forge plan`, so the number of
# cross-wave pairs asked about is capped. forward_pairs sorts before truncating so the
# same plan always produces the same capped set.
MAX_FORWARD_PAIRS = 40

PROMPT_CHARS = 2500


def parse_tasks(plan: Path) -> dict[str, dict]:
    """id -> {id, title, files, deps, difficulty, prompt}, skipping comments and malformed rows."""
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
        task_id, deps, difficulty, files, _dwarf, _qa, title = parts[:7]
        if not task_id:
            continue
        # The requirements, not the implementation. An interface dependency between two
        # tasks is written down here and nowhere else -- certainly not in a one-line
        # title, which is all this rubric used to get.
        prompt = ''
        prompt_file = plan / 'tasks' / task_id / 'prompt.md'
        try:
            if prompt_file.is_file():
                prompt = prompt_file.read_text(errors='replace')[:PROMPT_CHARS]
        except OSError:
            prompt = ''
        tasks[task_id] = dict(id=task_id, title=title, files=files, deps=deps,
                              difficulty=difficulty, prompt=prompt)
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


def _wave_order(waves: dict) -> dict[str, int]:
    """Task id -> wave position. Waves are numbered as strings in waves.tsv; sort them
    numerically where possible so '10' follows '9' rather than '1'."""
    def key(name):
        try:
            return (0, int(name))
        except ValueError:
            return (1, name)

    order = {}
    for position, wave in enumerate(sorted(waves, key=key)):
        for task_id in waves[wave]:
            order[task_id] = position
    return order


def forward_pairs(tasks: dict, waves: dict) -> list[tuple[str, str]]:
    """Ordered (earlier, later) pairs across waves that nothing forces to be safe.

    "Task A is written against an interface task B builds later" is what shipped:
    jevscope's `readme` ran in wave 1 documenting the CLI that wave 2 built, passed its
    own review, and described an interface nobody ever wrote.

    A declared dependency is excluded, in the direction that matters: if LATER is named
    in EARLIER's deps, forge would not have placed EARLIER first, so the pair cannot
    arise. Nothing else about wave placement makes the order safe.
    """
    order = _wave_order(waves)
    found = []
    for earlier in sorted(tasks):
        for later in sorted(tasks):
            if earlier == later:
                continue
            if earlier not in order or later not in order:
                continue
            if order[earlier] >= order[later]:
                continue
            declared = {d.strip() for d in str(tasks[earlier].get('deps', '')).split(',')}
            if later in declared:
                continue
            found.append((earlier, later))
    found.sort()
    return found[:MAX_FORWARD_PAIRS]


def forward_dependencies(repo, *, tasks, waves, run_dir=None, config: dict | None = None):
    """Earlier-wave tasks Jev thinks were written against work that lands later.

    `tasks` is a list of dicts with id/title/files/difficulty; `waves` maps a wave number
    to the list of task ids placed in it -- the same shapes `compute_waves` produces.
    Returns [(earlier, later, probability), ...] highest first, [] for "nothing to say",
    None for "no judgment could be made".
    """
    by_id = {t['id']: t for t in tasks if isinstance(t, dict) and t.get('id')}
    candidates = forward_pairs(by_id, waves)
    if not candidates:
        return []

    questions = {}
    for i, (earlier, later) in enumerate(candidates):
        earlier_desc = f"{earlier} ({by_id[earlier]['title']}) files: {by_id[earlier]['files']}"
        later_desc = f"{later} ({by_id[later]['title']}) files: {by_id[later]['files']}"
        questions[f'f{i}'] = forward_dependency(earlier_desc, later_desc)

    involved = sorted({task_id for pair in candidates for task_id in pair})
    state = dict(tasks=[dict(id=by_id[i]['id'], title=by_id[i]['title'],
                             files=by_id[i]['files'],
                             requirements=by_id[i].get('prompt', ''))
                        for i in involved])
    result = ask(state, questions, site='jev-forward', run_dir=run_dir, config=config)
    if result is None:
        return None

    warn_at = threshold('forward_warn', config=config, site='jev-forward')
    found = []
    for i, (earlier, later) in enumerate(candidates):
        probability = noul(result, f'f{i}')
        if probability is not None and probability >= warn_at:
            found.append((earlier, later, probability))
    found.sort(key=lambda row: (-row[2], row[0], row[1]))
    return found[:MAX_WARNINGS]

