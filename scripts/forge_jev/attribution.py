"""Integration attribution (#45): which merged task most likely broke the combined verification.

`forge-parallel.sh attribute` searches the merge order for a merge that breaks the run's
verification, one full verification per step. This asks one Choice, `culprit`, over every merged
task id plus `none`, given the end of the failing log and each merge's diff, and prints the
ranking: `<id>TAB<p>` per line, highest first, `none` included. The shell decides what to do with
it by the gate's mode (`on` probes the top suspect's boundary first; `shadow` only records it).
The gate never reaches a verdict by itself: a wrong guess only costs the shell one more step.

    forge-jev.py attribution --plan P --repo R --log L

Exit 3 is no opinion: the gate is off, the log is missing, fewer than two merges, or the model
did not answer.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import site_ready
from .client import ask, probabilities
from .questions import culprit

SITE = 'jev-attribution'
LOG_LINES = 150
DIFF_CHARS = 3000
# The subject forge-lib-attribute.sh keeps: `<sha> forge: merge <id>` (an awk match on `%H %s`).
_MERGE = re.compile(r'([0-9a-f]+) forge: merge ([^ ]+) *')


def _read(path: Path) -> str:
    try:
        return path.read_text(errors='replace')
    except OSError:
        return ''


def _git(repo, *args) -> str | None:
    try:
        done = subprocess.run(['git', '-C', str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, errors='replace', timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def merges(plan, repo) -> list[tuple[str, str]]:
    """[(commit, task id)], oldest first: the integration branch's first-parent `forge: merge <id>`
    commits after its base, as forge-lib-attribute.sh reads them. [] when the base or the branch
    is missing."""
    plan = Path(plan)
    run_id = _read(plan / 'run_id').strip()
    base = _read(plan / 'integration.base').strip()
    if not run_id or not base:
        return []
    base_commit = (_git(repo, 'rev-parse', base + '^{commit}') or '').strip()
    tip = (_git(repo, 'rev-parse', f'forge/{run_id}-integration^{{commit}}') or '').strip()
    if not base_commit or not tip:
        return []
    listing = _git(repo, 'log', '--first-parent', '--reverse', '--format=%H %s', f'{base_commit}..{tip}') or ''
    out = []
    for line in listing.splitlines():
        found = _MERGE.fullmatch(line)
        if found:
            out.append((found.group(1), found.group(2)))
    return out


def _tasks_tsv(plan: Path) -> dict[str, tuple[str, list[str]]]:
    """id -> (title, files): tasks.tsv columns 7 and 4."""
    out = {}
    for line in _read(plan / 'tasks.tsv').splitlines():
        cells = line.split('\t')
        if len(cells) < 7 or line.startswith('#'):
            continue
        files = [name.strip() for name in cells[3].split(',') if name.strip() and name.strip() != '-']
        out[cells[0]] = (cells[6], files)
    return out


def _diff(repo, commit: str) -> str:
    text = _git(repo, 'diff', f'{commit}^1', commit) or ''
    return text[:DIFF_CHARS] + ('\n[cut]' if len(text) > DIFF_CHARS else '')


def _tail(path: Path) -> str:
    return '\n'.join(_read(path).splitlines()[-LOG_LINES:])


def state_of(plan, repo, log) -> dict | None:
    """The request's state, or None with fewer than two merges."""
    plan = Path(plan)
    found = merges(plan, repo)
    if len(found) < 2:
        return None
    listed = _tasks_tsv(plan)
    tasks = []
    for commit, task in found:
        title, files = listed.get(task, ('', []))
        tasks.append(dict(id=task, title=title, files=files, diff=_diff(repo, commit)))
    return dict(run=_read(plan / 'run_id').strip() or plan.name, log=_tail(Path(log)), tasks=tasks)


def ranking(probs: dict, order: list) -> list[tuple[str, float]]:
    """(option, p), highest first; equal probabilities keep the order the options were asked in."""
    place = {option: i for i, option in enumerate(order)}
    return sorted(probs.items(), key=lambda item: (-item[1], place.get(item[0], len(place))))


def main(args) -> int:
    if not site_ready(SITE):
        return 3
    plan, log = Path(args.plan), Path(args.log)
    if not log.is_file():
        return 3
    state = state_of(plan, args.repo, log)
    if state is None:
        return 3
    ids = [task['id'] for task in state['tasks']]
    result = ask(state, {'culprit': culprit(ids)}, site=SITE, run_dir=str(plan))
    order = ranking(probabilities(result, 'culprit'), ids + ['none'])
    if not order:
        return 3
    print('\n'.join(f'{option}\t{p:.3f}' for option, p in order))
    return 0


# --- measurement: what forge_decide.sites reads for this site --------------------------------

PREFIX = 'culprit:'


def _options(body) -> list[str]:
    questions = body.get('questions') if isinstance(body, dict) else None
    question = questions.get('culprit') if isinstance(questions, dict) else None
    criteria = question.get('criteria') if isinstance(question, dict) else None
    return [str(option) for option in criteria] if isinstance(criteria, dict) else []


def decisions(body) -> list[str]:
    """`culprit:<id>` per option of the request except `none`, in the order asked."""
    return [PREFIX + option for option in _options(body) if option != 'none']


def score(answers, qid, request=None) -> float | None:
    """The option's probability in the Choice's distribution."""
    if not str(qid).startswith(PREFIX):
        return None
    probs = probabilities({'answers': answers or {}}, 'culprit')
    return probs.get(str(qid)[len(PREFIX):])


def _asked_order(request) -> list[str]:
    """The options in the order the run asked them: the merge order of the request's tasks, then
    `none`. A stored request sorts its criteria, so they cannot say; its state's task list does."""
    if not isinstance(request, dict):
        return []
    state = request.get('state')
    tasks = state.get('tasks') if isinstance(state, dict) else None
    if isinstance(tasks, list):
        ids = [str(item['id']) for item in tasks if isinstance(item, dict) and 'id' in item]
        if ids:
            return ids + ['none']
    return _options(request)


def fired(answers, threshold_value=None, request=None) -> list[str] | None:
    """The top option when it is not `none` (there is no threshold). Ties break as `main` ranks
    them, by the order the run asked the options in. None when options tie at the top and the
    request does not give that order (the report has only the question names when it finds no
    stored body): the winner cannot be known then."""
    probs = probabilities({'answers': answers or {}}, 'culprit')
    order = _asked_order(request)
    ordered = ranking(probs, order)
    if not ordered:
        return []
    tied = [option for option, p in ordered if p == ordered[0][1]]
    if len(tied) > 1 and any(option not in order for option in tied):
        return None
    if ordered[0][0] == 'none':
        return []
    return [PREFIX + ordered[0][0]]


def baseline(body, qid) -> float:
    """How many of the task's declared files appear in the failing log: the plain rule the
    model's probability is measured against."""
    state = body.get('state') if isinstance(body, dict) else None
    if not isinstance(state, dict):
        return 0.0
    log = str(state.get('log') or '')
    task = str(qid)[len(PREFIX):]
    for item in state.get('tasks') if isinstance(state.get('tasks'), list) else []:
        if isinstance(item, dict) and item.get('id') == task:
            files = item.get('files')
            return float(sum(1 for name in files if str(name) in log)) if isinstance(files, list) else 0.0
    return 0.0
