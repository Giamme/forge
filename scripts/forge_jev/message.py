"""The dwarf's final message (#44): did the implementer say it did not finish, or ask the user?

After every implementer attempt forge pays for a review, whatever `dwarf.last` says. This reads
that message first, in one request with three questions: `unfinished`, `blocked` and `question`.
The shell sends an attempt back, without a review, when the implementer says it did not finish
or was blocked, and stops the task for the user's answer when it asks a question.

The gate only ever stops work early: it never accepts anything. It asks nothing when there is no
message. Run, task and attempt are part of the state, so the request key (and the cache) follow
them and the same words on a later attempt, or in another run, are asked and recorded again.

Prints `<which>TAB<p>` for the first question that fires, in the priority `question`, `blocked`,
`unfinished`, or `ok TAB <max p>` when none does. Exit 3 is no opinion: the gate is off or in
shadow, there is no message, or the model did not answer.
"""
from __future__ import annotations

from pathlib import Path

from . import gate_mode, site_ready, threshold
from .client import ask, noul
from .questions import message_blocked, message_question, message_unfinished

SITE = 'jev-dwarf-message'
MESSAGE_CHARS = 6000
# The order the shell acts in: a question outranks "I could not finish", which outranks it.
PRIORITY = ('question', 'blocked', 'unfinished')
THRESHOLD = dict(unfinished='message_warn', blocked='message_warn', question='message_ask')
# Per kind, the words the plain baseline looks for in the message.
WORDS = dict(unfinished=('TODO', 'not finished', 'did not finish', 'partially'),
             blocked=('could not', "couldn't", 'unable to', 'blocked'),
             question=('?',))


def _read(path: Path) -> str:
    try:
        return path.read_text(errors='replace')
    except OSError:
        return ''


def _run_id(plan: Path) -> str:
    return _read(plan / 'run_id').strip() or plan.name


def _title(plan: Path, task: str) -> str:
    """The task's title: tasks.tsv column 7."""
    for line in _read(plan / 'tasks.tsv').splitlines():
        cells = line.split('\t')
        if len(cells) >= 7 and not line.startswith('#') and cells[0] == task:
            return cells[6]
    return ''


def _questions() -> dict:
    return dict(unfinished=message_unfinished(), blocked=message_blocked(), question=message_question())


def main(args) -> int:
    if not site_ready(SITE):
        return 3
    plan = Path(args.plan)
    tdir = plan / 'tasks' / args.task
    message = _read(tdir / 'dwarf.last')
    if not message.strip():
        return 3

    attempt = int(args.attempt)
    state = dict(run=_run_id(plan), task=args.task, attempt=attempt, title=_title(plan, args.task),
                 message=message[-MESSAGE_CHARS:])
    result = ask(state, _questions(), site=SITE, run_dir=str(tdir),
                 context=dict(task=args.task, attempt=attempt))
    if result is None or gate_mode(SITE) != 'on':
        return 3

    seen = {}
    for which in PRIORITY:
        probability = noul(result, which)
        if probability is None:
            continue
        seen[which] = probability
        if probability >= threshold(THRESHOLD[which], site=SITE, model=result.get('model')):
            print(f'{which}\t{probability:.3f}')
            return 0
    if not seen:
        return 3
    print(f'ok\t{max(seen.values()):.3f}')
    return 0


# --- measurement: what forge_decide.sites reads for this site --------------------------------

def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def decisions(body) -> list[str]:
    """The questions of `unfinished`, `blocked` and `question` that the request asked."""
    questions = body.get('questions') if isinstance(body, dict) else None
    asked = set(questions) if isinstance(questions, (dict, list)) else set()
    return [qid for qid in THRESHOLD if qid in asked]


def score(answers, qid, request=None) -> float | None:
    """The Noul of the question."""
    answer = (answers or {}).get(qid)
    if isinstance(answer, dict) and answer.get('type', 'noul') == 'noul':
        return _number(answer.get('noul'))
    return None


def fired(answers, threshold_value=None, request=None) -> list[str]:
    """The questions at or above their own threshold, in the priority order the gate acts in.

    `threshold_value` is None for this site: two thresholds belong to it, so it resolves
    `message_warn` and `message_ask` itself, for the model the request names. `request` is a stored
    body or a `{model, questions}` dict, or a bare list of question ids (no model)."""
    model = request.get('model') if isinstance(request, dict) else None
    out = []
    for qid in PRIORITY:
        probability = score(answers, qid)
        if probability is not None and probability >= threshold(THRESHOLD[qid], site=SITE, model=model):
            out.append(qid)
    return out


def baseline(body, qid) -> float:
    """1.0 when the message holds one of a few plain words for the kind of question, else 0.0:
    the rule the model's score is measured against."""
    state = body.get('state') if isinstance(body, dict) else None
    message = str(state.get('message') or '') if isinstance(state, dict) else ''
    return 1.0 if any(word in message for word in WORDS.get(qid, ())) else 0.0
