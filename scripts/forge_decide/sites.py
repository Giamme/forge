"""The live decision-model sites: what each acts on, what judges it, what it must beat.

`forge decide report` and `replay` read this registry. A record holds, per site:

- `fired(answers, threshold_value, request)`: the question ids the site acted on, mirroring
  the site's own code (forge_jev/gates.py, coupling.py, memory.py, verify.py). `answers` is a
  request's answers, {question id: answer}, as jev.jsonl and the dataset keep them.
  `request` is the stored request body {state, model, questions}, or the list of question ids
  jev.jsonl logs: the answers alone cannot say which lines were asked or which `runs_<i>` is
  which command. A site that cannot be read without the body (verify-discover) returns None
  for "unknown" when it lacks it.
- `decisions(body)`: the question ids of a stored request body that are scored decisions
  (a label can exist for them).
- `score(answers, qid, request)`: the decision's probability.
- `label`: one sentence naming the label, or None with the issue that will define it.
- `baseline(body, qid)`: the cheap heuristic the site must not lose to; higher = more
  likely positive.
- `floor`: the precision a promoted gate must reach at its threshold.

Pure functions over answers and bodies: no I/O, and nothing here asks a model.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

from forge_decide import SITE_GATE
from forge_jev.client import probabilities
from forge_jev.coupling import MAX_WARNINGS
from forge_jev.memory import SLICE_KEEP

# Verify-discover is decided per candidate; its decision for candidate i is the question
# the request already asks about it, `runs_<i>`.
_INDEXED = re.compile(r'^(?:f|l|runs_)(\d+)$')
_FILE = re.compile(r'''the file (?P<q>['"])(?P<path>.+?)(?P=q)(?:\s|\?|,|$)''')
_EARLIER = re.compile(r'''Task EARLIER: ['"](?P<id>\S+) \(''')
_LATER = re.compile(r'''task LATER: ['"](?P<id>\S+) \(''')


def _noul(answers, qid) -> float | None:
    answer = (answers or {}).get(qid)
    if isinstance(answer, dict) and answer.get('type', 'noul') == 'noul':
        value = answer.get('noul')
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _index(qid: str) -> int:
    found = _INDEXED.match(qid)
    return int(found.group(1)) if found else -1


def _ids(answers, prefix: str) -> list[str]:
    """The answered question ids that are `<prefix><n>`, in index order."""
    found = [qid for qid in (answers or {}) if re.fullmatch(prefix + r'\d+', str(qid))]
    return sorted(found, key=_index)


def _questions(body) -> dict:
    """The body's questions by id. Anything but a stored body, which has them as a dict, has
    none: a jev.jsonl entry's id list is read by _asked."""
    questions = body.get('questions') if isinstance(body, dict) else None
    return questions if isinstance(questions, dict) else {}


def _asked(request) -> list[str]:
    """The question ids a request asked: a stored body's questions, or the id list jev.jsonl
    logs. Empty when the caller has neither."""
    if isinstance(request, dict):
        questions = request.get('questions')
        request = list(questions) if isinstance(questions, (dict, list)) else []
    return [str(qid) for qid in request] if isinstance(request, (list, tuple)) else []


def _state(body) -> dict:
    state = (body or {}).get('state')
    return state if isinstance(state, dict) else {}


def _question_ids(request, prefix: str) -> list[str]:
    return sorted((qid for qid in _asked(request) if re.fullmatch(prefix + r'\d+', qid)), key=_index)


def _instructions(body, qid) -> str:
    question = _questions(body).get(qid)
    return str(question.get('instructions') or '') if isinstance(question, dict) else ''


def _files(text) -> list[str]:
    return [name for name in str(text or '').replace(',', ' ').split() if name]


# --- fired ------------------------------------------------------------------------------

def _fired_at_least(prefix: str, keep: int | None = None):
    def fired(answers, threshold_value, request=None):
        found = []
        for qid in _ids(answers, prefix):
            probability = _noul(answers, qid)
            if probability is not None and probability >= threshold_value:
                found.append((probability, qid))
        # The site lists its warnings highest first; ties stay in question order.
        found.sort(key=lambda item: (-item[0], _index(item[1])))
        return [qid for _p, qid in found[:keep]]
    return fired


def _fired_prompt(answers, threshold_value, request=None):
    # `forge jev score-plan` warns BELOW prompt_warn: the probability is that a reviewer
    # could judge the task from its text, and the site acts when it is too low.
    probability = _noul(answers, 'prompt_adequacy')
    return ['prompt_adequacy'] if probability is not None and probability < threshold_value else []


def _fired_slice(answers, threshold_value=None, request=None):
    """The lines relevant_lines keeps: the top SLICE_KEEP by p, an unscored line as 1.0,
    ties by index; every line when it was asked about no more than SLICE_KEEP. The asked
    lines are the `l<i>` ids of the request. None without it."""
    if request is None:
        return None
    ids = _question_ids(request, 'l')
    if len(ids) <= SLICE_KEEP:
        return ids
    scored = []
    for qid in ids:
        probability = _noul(answers, qid)
        scored.append((1.0 if probability is None else probability, _index(qid), qid))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return sorted((qid for _p, _i, qid in scored[:SLICE_KEEP]), key=_index)


def _commands(request) -> list | None:
    """The candidates' commands in the order `runs_<i>` follows: the request's
    state.candidates. The choice question's criteria are sorted when sent and the answer's
    probability map follows them, so neither says which command `runs_<i>` is. None when
    the caller has no stored body."""
    if not isinstance(request, dict):
        return None
    candidates = _state(request).get('candidates')
    if not isinstance(candidates, list):
        return None
    return [candidate.get('command') if isinstance(candidate, dict) else None for candidate in candidates]


def _fired_verify(answers, threshold_value, request=None):
    """The chosen candidate's runs_<i>, when 1 - P(none) >= tests_act and its runs_<i> Noul
    is >= 0.5, as verify.discover gates it. Without a distribution it falls back to the
    choice's own confidence, as discover does. None without the body: `runs_<i>` names a
    candidate by its place in the request, which the answers do not record."""
    commands = _commands(request)
    if commands is None:
        return None
    answers = answers or {}
    answer = answers.get('choice')
    if not isinstance(answer, dict) or answer.get('type') != 'choice':
        return []
    picked = answer.get('choice')
    if picked is None or picked == 'none':
        return []
    distribution = probabilities({'answers': answers}, 'choice')
    if distribution:
        if (1.0 - distribution.get('none', 0.0)) < threshold_value:
            return []
    elif (answer.get('confidence') or 0.0) < threshold_value:
        return []
    if picked not in commands:
        return []
    qid = f'runs_{commands.index(picked)}'
    runs = _noul(answers, qid)
    return [qid] if runs is not None and runs >= 0.5 else []


# --- decisions and scores ----------------------------------------------------------------

def _decisions_prefix(prefix: str):
    return lambda body: _question_ids(body, prefix)


def _decisions_prompt(body):
    return ['prompt_adequacy'] if 'prompt_adequacy' in _asked(body) else []


def _score_noul(answers, qid, request=None):
    return _noul(answers, qid)


def _score_verify(answers, qid, request=None):
    """The choice distribution's probability of the candidate's command. None without the
    body (see _fired_verify)."""
    commands = _commands(request)
    index = _index(qid)
    if commands is None or not 0 <= index < len(commands) or commands[index] is None:
        return None
    return probabilities({'answers': answers or {}}, 'choice').get(commands[index])


def _decisions_verify(body):
    """One `runs_<i>` per candidate of the body."""
    commands = _commands(body)
    return [f'runs_{i}' for i in range(len(commands))] if commands is not None else _question_ids(body, 'runs_')


# --- baselines ---------------------------------------------------------------------------

def _drift_file(body, qid) -> str:
    found = _FILE.search(_instructions(body, qid))
    return found.group('path') if found else ''


def _baseline_drift(body, qid) -> float:
    """near(): same directory as a declared file +1, file stem in prompt+approach +2, full
    path in that text +4."""
    path = _drift_file(body, qid)
    if not path:
        return 0.0
    state = _state(body)
    task = state.get('task') if isinstance(state.get('task'), dict) else {}
    declared = _files(task.get('declared_files'))
    directories = {name.rsplit('/', 1)[0] for name in declared if '/' in name}
    text = (str(state.get('prompt') or '') + str(state.get('approach') or '')).lower()
    stem = path.split('/')[-1].split('.')[0].lower()
    return float((path.rsplit('/', 1)[0] in directories) + 2 * (stem in text) + 4 * (path.lower() in text))


def _baseline_slice(body, qid) -> float:
    """named(): 2 if the fact's [anchor] is in the task text, +1 if the anchor's directory is."""
    state = _state(body)
    facts = state.get('facts')
    index = _index(qid)
    if not isinstance(facts, list) or not 0 <= index < len(facts):
        return 0.0
    line = str(facts[index])
    if '[' not in line:
        return 0.0
    anchor = line.rsplit('[', 1)[-1].split(']')[0]
    if not anchor:
        return 0.0
    task = str(state.get('task') or '')
    return float(2 * (anchor in task) + (anchor.rsplit('/', 1)[0] in task))


def _baseline_verify(body, qid) -> float:
    """Candidate order: the first candidate scores highest."""
    index = _index(qid)
    count = len(_decisions_verify(body))
    return float(count - index) if 0 <= index < count else 0.0


def _baseline_forward(body, qid) -> float:
    """1 when a path in the earlier task's files appears in the later task's requirements."""
    text = _instructions(body, qid)
    earlier, later = _EARLIER.search(text), _LATER.search(text)
    if not earlier or not later:
        return 0.0
    tasks = {str(task.get('id')): task for task in (_state(body).get('tasks') or []) if isinstance(task, dict)}
    first, second = tasks.get(earlier.group('id')), tasks.get(later.group('id'))
    if not first or not second:
        return 0.0
    requirements = str(second.get('requirements') or '')
    return 1.0 if any(path in requirements for path in _files(first.get('files'))) else 0.0


def _baseline_none(body, qid) -> float:
    return 0.0


@dataclass(frozen=True)
class Site:
    name: str
    gate: str
    threshold: str | None
    fired: Callable
    decisions: Callable
    score: Callable
    label: str | None
    baseline: Callable
    floor: float | None = None
    # The issue that will define the label, for a site that has none.
    label_issue: str | None = None


def _site(name, threshold, fired, decisions, score, label, baseline, floor=None, label_issue=None):
    return Site(name, SITE_GATE[name], threshold, fired, decisions, score, label, baseline, floor, label_issue)


def _no_fire(answers, threshold_value=None, request=None):
    return []


SITES: dict[str, Site] = {record.name: record for record in (
    _site('jev-drift', 'drift_warn', _fired_at_least('f'), _decisions_prefix('f'), _score_noul,
          "the file was edited by the task's final passing attempt (and was not declared)",
          _baseline_drift, floor=0.5),
    _site('jev-forward', 'forward_warn', _fired_at_least('f', MAX_WARNINGS), _decisions_prefix('f'),
          _score_noul,
          'a later plan revision of the same run added the dependency (true); both tasks merged '
          'with no such dependency (false)',
          _baseline_forward, floor=0.5),
    _site('jev-prompt', 'prompt_warn', _fired_prompt, _decisions_prompt, _score_noul,
          None, _baseline_none, label_issue='#40'),
    _site('jev-memory-curate', None, _no_fire, lambda body: [], _score_noul,
          None, _baseline_none, label_issue='#41: needs a contradiction record'),
    _site('jev-memory-slice', None, _fired_slice, _decisions_prefix('l'), _score_noul,
          "the fact's anchor was edited by the task (true), or lies in an area the task never "
          'touched (false)',
          _baseline_slice),
    _site('jev-memory-stale', None, _no_fire, lambda body: [], _score_noul,
          None, _baseline_none, label_issue='#41: needs a contradiction record'),
    _site('jev-verify-discover', 'tests_act', _fired_verify, _decisions_verify, _score_verify,
          'the candidate is the command the repo documents: `.forge/verify`, or the command a later '
          'run of the same repo verified with, from `--verify` or `repo.verify` (command_source not '
          '`jev`)',
          _baseline_verify, floor=0.7),
)}


def site(name: str) -> Site | None:
    """The record for a live site; None for an unknown or retired one (jev-score-plan)."""
    return SITES.get(name)
