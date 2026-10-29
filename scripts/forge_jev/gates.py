"""Plan-time gates: prompt adequacy (site jev-prompt) and `files` drift (site jev-drift).

Both are advisory. `score-plan` asks them before `plan` prints its table, and nothing
they answer is ever written into tasks.tsv.

Prompt adequacy asks whether a reviewer could judge correctness from the task's own
text. Its `prompt_warn` cutoff was measured on exactly the state `task_state` builds --
goal, task (id, title, files, deps), prompt, approach, plan_tasks, file_excerpts and
memory -- so that state must not change shape without re-measuring it.

`decompose.md` calls the `files` column "a promise, not a prediction": a task that edits
a file it never declared breaks wave disjointness, which is the only thing bounding
parallelism. Forge already detects this, but afterwards, in `drift.txt` -- by which point
the wave has run. Asking at plan time converts a post-mortem into a warning while the
plan can still change.

Drift asks about at most CANDIDATE_CAP (25) tracked files, not every one. `candidates`
ranks them by cheap signals -- named in the task's prompt or approach, a test paired with
a declared file, committed with a declared file in the last 200 commits, or in a declared
file's directory -- and keeps the top 25. Each question carries the same ~1 KB rubric, so
the cap is most of what drift costs. Ranking rather than alphabetical order is what lets
`tests/` and `ui/` be asked about at all: on this repository the old first-120 cut never
reached them.

Measured before shipping (see references/decision-models.md): at the default `gate_warn` of 0.60 this
rubric scored precision 1.000 on one repo and 0.934 on another, over 50 commits. That was the
alphabetical pool, not the ranked one. Precision is the number that matters here -- a warning
that fires on files nobody edits teaches people to ignore every future warning.
"""
from __future__ import annotations

import posixpath
import re
import subprocess
from collections import Counter
from pathlib import Path

from . import threshold
from .client import ask, noul
from .questions import drift_prediction, prompt_adequacy as prompt_adequacy_question

# One request per task, and one Cloudflare chunk: the most questions it holds comfortably.
# A plan-time gate must not turn into the slowest thing in `forge plan`.
CANDIDATE_CAP = 25
# The co-change signal looks this far back, and one file scores at most CO_CHANGE_CAP from it.
CO_CHANGE_COMMITS = 200
CO_CHANGE_CAP = 3

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


FILE_EXCERPT_CHARS = 600
MAX_EXCERPT_FILES = 6
MEMORY_CHARS = 4000

# The other tasks in the plan, as sent to the model. Capped because a large plan would
# otherwise dominate the payload, and truncated per field for the same reason: the model
# needs their identity and file scope, not their full text.
MAX_SIBLINGS = 40
SIBLING_TITLE_CHARS = 120


def _excerpt(repo: Path, files: str) -> dict:
    out = {}
    for rel in (files or '').replace(',', ' ').split():
        if len(out) >= MAX_EXCERPT_FILES:
            break
        path = repo / rel
        if not path.is_file():
            # A file the task will CREATE is normal and informative in itself.
            out[rel] = '(does not exist yet)'
            continue
        try:
            out[rel] = path.read_text(errors='replace')[:FILE_EXCERPT_CHARS]
        except OSError:
            continue
    return out


def _memory(repo: Path) -> str:
    path = repo / '.forge' / 'memory.md'
    try:
        return path.read_text(errors='replace')[:MEMORY_CHARS] if path.is_file() else ''
    except OSError:
        return ''


def _siblings(siblings, task_id: str) -> list[dict]:
    """Compact view of the rest of the plan, excluding the task being asked about."""
    out = []
    for sibling in siblings or []:
        if sibling.get('id') == task_id:
            continue
        out.append(dict(id=sibling.get('id', ''),
                        title=str(sibling.get('title', ''))[:SIBLING_TITLE_CHARS],
                        files=sibling.get('files', ''),
                        deps=sibling.get('deps', '')))
        if len(out) >= MAX_SIBLINGS:
            break
    return out


def task_state(repo, *, goal: str, task_id: str, title: str, files: str, deps: str = '',
               approach: str = '', prompt: str = '', siblings=None) -> dict:
    """The state prompt adequacy is asked against: the one `prompt_warn` was measured on.

    `prompt` is tasks/<id>/prompt.md, the text the dwarf is dispatched with and the
    reviewer reads. With only a title in state, the honest answer to "could a reviewer
    judge correctness" is no, so a 460-word spec once scored 0.23 and tripped its own
    warning. The planner's declared difficulty is deliberately not sent.
    """
    repo_path = Path(repo)
    return dict(goal=goal, task=dict(id=task_id, title=title, files=files, deps=deps),
                prompt=prompt, approach=approach, plan_tasks=_siblings(siblings, task_id),
                file_excerpts=_excerpt(repo_path, files), memory=_memory(repo_path))


def prompt_adequacy(repo, *, goal: str, task_id: str, title: str, files: str, deps: str = '',
                    approach: str = '', prompt: str = '', siblings=None, run_dir=None,
                    config: dict | None = None) -> float | None:
    """The probability that a reviewer could judge this task's work from its text.

    One request, one rubric. None on any failure: a plan that cannot be checked must
    still be a plan.
    """
    state = task_state(repo, goal=goal, task_id=task_id, title=title, files=files, deps=deps,
                       approach=approach, prompt=prompt, siblings=siblings)
    result = ask(state, dict(prompt_adequacy=prompt_adequacy_question()), site='jev-prompt',
                 run_dir=run_dir, config=config)
    if result is None:
        return None
    return noul(result, 'prompt_adequacy')


def _tracked(repo: Path) -> list[str]:
    try:
        out = subprocess.run(['git', '-C', str(repo), 'ls-files', '-z'],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [name for name in out.stdout.split('\0') if name]


def _history(repo: Path) -> list[set[str]]:
    """The files each of the last CO_CHANGE_COMMITS commits touched, one set per commit.

    Unfiltered on purpose: a `-- <paths>` filter would hide the other files of each commit,
    which are the ones this signal is for. Empty on a git failure or a timeout.
    """
    try:
        out = subprocess.run(['git', '-C', str(repo), '-c', 'core.quotePath=false', 'log',
                              '-n', str(CO_CHANGE_COMMITS), '--name-only', '--format=%x00%H'],
                             capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    # Each commit opens with a NUL, then its hash line, then its file names, one per line.
    return [set(chunk.split('\n')[1:]) - {''} for chunk in out.stdout.split('\0')[1:]]


def _co_changes(repo: Path, declared: set[str]) -> Counter:
    """For each file, how many of the recent commits it shares with a declared file."""
    counts = Counter()
    if not declared:
        return counts
    for files in _history(repo):
        if files & declared:
            counts.update(files - declared)
    return counts


def _stem(name: str) -> str:
    return posixpath.splitext(posixpath.basename(name))[0]


def _test_stem(name: str) -> str | None:
    """The stem a test file pairs by, its name without the test marker; None if not a test."""
    stem = _stem(name)
    if stem.startswith('test_'):
        return stem[len('test_'):] or None
    for suffix in ('_test', '.test', '.spec'):
        if stem.endswith(suffix):
            return stem[:-len(suffix)] or None
    return None


def _paired_stems(declared: list[str]) -> set[str]:
    """The test stems that pair with a declared file: its stem, and that stem minus its
    first segment. So `forge-route.py` pairs with `tests/test_route.py`, whose stem is `route`.
    """
    stems = set()
    for name in declared:
        stem = _stem(name)
        stems.add(stem)
        parts = re.split(r'[-_]', stem, maxsplit=1)
        if len(parts) == 2 and parts[1]:
            stems.add(parts[1])
    return stems


def _named(text: str, name: str) -> bool:
    """Whether `name` appears in `text` as a whole name, not as part of a longer one.

    `forge` is not named by `forge-route.py`, nor by `forge.py`; it is named by "forge." at
    the end of a sentence.
    """
    pattern = r'(?<![\w.-])' + re.escape(name) + r'(?![\w-]|\.\w)'
    return name in text and re.search(pattern, text) is not None


def _name_score(name: str, text: str) -> int:
    """8 when the full path is named in the text, and 4 more when its basename is."""
    score = 8 if _named(text, name) else 0
    return score + (4 if _named(text, posixpath.basename(name)) else 0)


def _excluded(name: str, declared: set[str]) -> bool:
    """Noise, the declared files themselves, and anything under a declared directory."""
    return (name in declared
            or name.startswith(_SKIP_PREFIXES) or name.endswith(_SKIP_SUFFIXES)
            or name.rsplit('/', 1)[-1] in _SKIP_NAMES
            or any(name.startswith(d.rstrip('/') + '/') for d in declared))


def candidates(repo, declared: str, text: str = '') -> list[str]:
    """Tracked files the task probably touches but does not declare: the best CANDIDATE_CAP.

    Each file scores on cheap signals, and a file scoring zero is not a candidate:
      its full path named in `text` (+8), and its basename named in `text` (+4) on top;
      a test file paired with a declared file (+3);
      committed with a declared file in the last CO_CHANGE_COMMITS commits (+1 each, up to
      CO_CHANGE_CAP);
      in the same directory as a declared file (+1).
    Ordered by (-score, path), so the same plan always asks the same questions.
    """
    repo = Path(repo)
    named = [token for token in (declared or '').replace(',', ' ').split() if token != '-']
    declared_set = set(named)
    dirs = {posixpath.dirname(name) for name in named}
    stems = _paired_stems(named)
    together = _co_changes(repo, declared_set)
    ranked = []
    for name in _tracked(repo):
        if _excluded(name, declared_set):
            continue
        score = (_name_score(name, text)
                 + (3 if _test_stem(name) in stems else 0)
                 + min(together[name], CO_CHANGE_CAP)
                 + (1 if posixpath.dirname(name) in dirs else 0))
        if score:
            ranked.append((-score, name))
    ranked.sort()
    return [name for _score, name in ranked[:CANDIDATE_CAP]]


def predict_drift(repo, *, goal: str, task_id: str, title: str, declared: str,
                  approach: str = '', prompt: str = '', run_dir=None,
                  config: dict | None = None):
    """Undeclared files this task looks likely to edit, highest probability first.

    Returns [] when there is nothing to say and None when no judgment could be made, so
    a caller can tell "no drift predicted" from "Jev did not answer".
    """
    pool = candidates(repo, declared, text=f'{prompt}\n{approach}')
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

    # drift_warn reads gate_warn until a model has its own measured value: a cutoff fitted
    # to one model's probabilities is not a cutoff on another's.
    warn_at = threshold('drift_warn', config=config, site='jev-drift', model=result.get('model'))
    predicted = []
    for i, name in enumerate(pool):
        probability = noul(result, f'f{i}')
        if probability is not None and probability >= warn_at:
            predicted.append((name, round(probability, 3)))
    predicted.sort(key=lambda item: item[1], reverse=True)
    return predicted
