"""Brief mode: each task's open questions, asked before the task starts (#135).

`write` makes one read-only `side` dispatch of kind `brief` per task and stores the reply:

- `tasks/<id>/brief.json`: `{questions: [{id, text, why, options, default, scope: "task",
  must_answer}], model, generated_at}`. `must_answer` survives only under the `gated` level.
- `tasks/<id>/brief.parked.json`: the same shape for questions whose scope is `wave` or
  `plan`. Only task scope acts, so wider scopes are parked whatever `brief_scope` says.
  A wave question from a task that belongs to a group also stores `"group": "<gid>"`;
  a plan question from such a task stores `"group": null`. A task in no group stores
  neither key, so its parked file stays as it was.

When the task belongs to a group (`<plan>/groups.tsv`), the prompt names that group's
title, goal and the other tasks in it, and says what `wave` and `plan` mean for the
group. A task in no group gets the prompt it got before groups existed. A `wave` or
`plan` brief scope with no `groups.tsv` prints one note and continues; `brief.groups.noted`
keeps that note to once per plan. Nothing here acts on an answer.

A broken writer never holds a task: a reply that is not valid JSON, or has the wrong
shape, is retried once, and a second failure (or a failed dispatch) stores
`{"questions": [], "error": "<reason>"}` as `brief.json`.

`show` lists every question with its state: `open`, `answered` (`brief.answers.json`),
`late` (`brief.late.json`) or `possibly_stale` (a flag in brief.json). A task with
`brief.affected` is marked `affected by <pid>`, and `flagged (already started)` when
that marker is `flag_only`. `--json` includes those rows as `affected`.

`answer` and `stale` (answers.py) write those. An answer given before the task starts
goes to `brief.answers.json`; one given after goes to `brief.late.json` and does not
change the task. `stale` sets `possibly_stale` on the open questions of tasks that
depend on a merge which changed files outside the ones it declared.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_groups  # noqa: E402
import forge_side  # noqa: E402

MAX_QUESTIONS = 3
DEP_PROMPT_LIMIT = 4096
SCOPES = ('task', 'wave', 'plan')
DEFAULT_JOBS = 4

REMINDER = (
    'Your last reply was not usable: %s.\n'
    'Reply again with the JSON object only, in exactly the format above: no prose, no code fence.\n'
    'If there is nothing to ask, reply {"questions": []}.\n')


class NotAPlan(Exception):
    """The plan dir is missing or has no tasks.tsv. The command exits 3."""


class BriefOff(Exception):
    """Brief mode is off for this plan. `reason` is why. The command exits 3."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class UnknownTask(Exception):
    """`--task` names a task the plan does not have. The command exits 2."""


# --- plan reading ----------------------------------------------------------------

def _text(path: Path) -> str:
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def _json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return None


def task_dir(plan, task) -> Path:
    return Path(plan) / 'tasks' / str(task)


def read_tasks(plan) -> list:
    """The `tasks.tsv` rows, in file order: `{id, deps, difficulty, files, dwarf, qa, title, row}`."""
    path = Path(plan) / 'tasks.tsv'
    if not path.is_file():
        raise NotAPlan(str(plan))
    tasks = []
    for line in _text(path).splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        cols = line.split('\t')
        cols += [''] * (7 - len(cols))
        deps = [dep for dep in cols[1].split(',') if dep and dep != '-']
        tasks.append({'id': cols[0], 'deps': deps, 'difficulty': cols[2], 'files': cols[3],
                      'dwarf': cols[4], 'qa': cols[5], 'title': '\t'.join(cols[6:]).strip(),
                      'row': line})
    return tasks


def _waves(plan) -> dict:
    found = {}
    for line in _text(Path(plan) / 'waves.tsv').splitlines():
        cols = line.split('\t')
        if len(cols) >= 2 and cols[0].strip().isdigit():
            found.setdefault(cols[1].strip(), int(cols[0]))
    return found


def ordered(plan, tasks: list) -> list:
    """Earliest projected start first: wave order from waves.tsv, then tasks.tsv order."""
    waves = _waves(plan)
    last = max(waves.values(), default=0) + 1
    ranked = sorted(enumerate(tasks), key=lambda item: (waves.get(item[1]['id'], last), item[0]))
    return [task for _, task in ranked]


# --- storage ---------------------------------------------------------------------

def atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + '\n'


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


# --- the prompt and the reply ----------------------------------------------------

def _task_group(plan, task_id):
    """The task's group, or None when it is in none (or groups.tsv cannot be read).

    A broken groups.tsv must not hold the task: the writer then behaves as it does for a
    task in no group.
    """
    try:
        return forge_groups.group_of(plan, task_id)
    except forge_groups.GroupError:
        return None


def _group_section(group, task: dict, tasks: list) -> str:
    """The group block that follows the task's own prompt.md and approach.md.

    The goal line is left out when the goal is `-`: that cell means the group has none,
    and the prompt does not invent a `(no goal)` line. An empty title stays as `-`, which
    is the cell groups.tsv stores.
    """
    by_id = {item['id']: item for item in tasks}
    lines = ['Group %s: %s' % (group.id, group.title)]
    if group.goal != '-':
        lines.append('Goal: %s' % group.goal)
    others = []
    for member in group.members:
        if member == task['id']:
            continue
        item = by_id.get(member)
        title = (item['title'] if item else '') or '(no title)'
        others.append('%s — %s' % (member, title))
    if others:
        lines.append('Other tasks in this group: %s' % ', '.join(others))
    else:
        lines.append('Other tasks in this group:')
    return '\n'.join(lines) + '\n'


# The scope sentence is the only line that changes for a grouped task. The ungrouped
# wording stays the sentence the prompt had before groups, so that prompt is byte-identical.
_ASK = (
    '## What to ask\n\n'
    'Ask an open question only where a design choice or an ambiguity would make the '
    'implementer guess. "Nothing to ask" is the expected answer for most tasks: when '
    'the prompt and approach are clear, reply {"questions": []}. At most %d questions.\n\n'
    'Each question is answered by a person before or while earlier tasks are built, so '
    'make it short and answerable in a line, and give the options when there are a few.\n\n'
    '## Reply format\n\n'
    'Reply with JSON only, no prose and no code fence:\n\n'
    '{"questions": [{"text": "...", "why": "...", "options": ["..."], "default": "...", '
    '"scope": "task", "must_answer": false}]}\n\n'
    '- text: the question.\n'
    '- why: what depends on the answer.\n'
    '- options: the plausible answers, or [].\n'
    '- default: what the implementer will do if nobody answers.\n'
    '%s'
    '- must_answer: true only when the decision cannot be defaulted safely.\n')
_SCOPE_UNGROUPED = (
    '- scope: "task" when only this task needs the answer, "wave" when the other tasks '
    'that run alongside it do too, "plan" when the whole run does.\n')
_SCOPE_GROUPED = (
    '- scope: "task" when only this task needs the answer, "wave" when the answer would '
    'change how several tasks of this group are done, "plan" when the answer would add, '
    'remove or split tasks, or change their order.\n')


def build_prompt(plan, task: dict, tasks: list, repo: str) -> str:
    plan = Path(plan)
    tdir = task_dir(plan, task['id'])
    by_id = {item['id']: item for item in tasks}
    group = _task_group(plan, task['id'])
    parts = [
        'You are preparing the open questions for ONE task of a multi-task coding run, '
        'before an implementer starts it.\n'
        'Read the task below. The repository is at %s: you may read it and you must not '
        'write to it.\n' % repo,
        '## The task: %s\n' % task['id'],
        '### prompt.md\n\n%s\n' % _text(tdir / 'prompt.md').strip(),
    ]
    approach = _text(tdir / 'approach.md').strip()
    if approach:
        parts.append('### approach.md\n\n%s\n' % approach)
    if group is not None:
        parts.append(_group_section(group, task, tasks))
    parts.append('### Its tasks.tsv row\n\n(id, deps, difficulty, files, dwarf, qa, title)\n\n%s\n' % task['row'])
    parts.append('### All tasks in this run\n\n%s\n' % '\n'.join(
        '- %s: %s' % (item['id'], item['title'] or '(no title)') for item in tasks))
    deps = [by_id[dep] for dep in task['deps'] if dep in by_id]
    if deps:
        parts.append('### Its dependencies\' prompts (each capped at 4 KB)\n')
        for dep in deps:
            parts.append('#### %s\n\n%s\n' % (
                dep['id'], forge_side.cap(_text(task_dir(plan, dep['id']) / 'prompt.md').strip(),
                                          DEP_PROMPT_LIMIT)))
    scope = _SCOPE_GROUPED if group is not None else _SCOPE_UNGROUPED
    parts.append(_ASK % (MAX_QUESTIONS, scope))
    return '\n'.join(parts)


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith('```'):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    return text


def parse_reply(reply):
    """`(questions, None)` for a usable reply, else `(None, reason)`.

    Each question is `{text, why, options, default, scope, must_answer}` and all six must
    be there: a question without a `default` leaves the implementer to guess, which is what
    the brief is for. A missing key, a wrong type, an empty `text`, `why` or `default`, or
    an unknown scope makes the reply unusable.
    """
    if not isinstance(reply, str) or not reply.strip():
        return None, 'empty reply'
    text = _strip_fence(reply)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        start, end = text.find('{'), text.rfind('}')
        data = None
        if 0 <= start < end:
            try:
                data = json.loads(text[start:end + 1])
            except (ValueError, RecursionError):
                data = None
        if data is None:
            return None, 'reply is not valid JSON'
    if not isinstance(data, dict) or not isinstance(data.get('questions'), list):
        return None, 'reply has no "questions" list'
    found = []
    for index, item in enumerate(data['questions'], 1):
        where = 'question %d' % index
        if not isinstance(item, dict):
            return None, '%s is not an object' % where
        missing = [key for key in ('text', 'why', 'options', 'default', 'scope', 'must_answer') if key not in item]
        if missing:
            return None, '%s has no %s' % (where, ', '.join(missing))
        text_value, why, default = item['text'], item['why'], item['default']
        for name, value in (('text', text_value), ('why', why), ('default', default)):
            if not isinstance(value, str) or not value.strip():
                return None, '%s: %s must be a non-empty string' % (where, name)
        options = item['options']
        if not isinstance(options, list) or not all(isinstance(opt, str) for opt in options):
            return None, '%s: options must be a list of strings' % where
        scope = item['scope']
        if scope not in SCOPES:
            return None, '%s: scope must be task, wave or plan' % where
        must = item['must_answer']
        if not isinstance(must, bool):
            return None, '%s: must_answer must be true or false' % where
        entry = {'text': text_value.strip(), 'why': why.strip(), 'options': [o.strip() for o in options],
                 'default': default.strip(), 'scope': scope, 'must_answer': must}
        try:   # a lone surrogate (JSON "\\ud800") parses but cannot be stored as UTF-8
            _dump(entry).encode('utf-8')
        except UnicodeError:
            return None, '%s has characters that cannot be stored' % where
        found.append(entry)
    return found, None


def _parse_safely(reply):
    """`parse_reply` that never raises: any surprise is an unusable reply."""
    try:
        return parse_reply(reply)
    except Exception as error:   # noqa: BLE001 - a broken reply must not hold the task
        return None, 'reply could not be read (%s)' % type(error).__name__


def _ask(plan, task: dict, prompt: str, spec: str, timeout: int):
    """One dispatch, a retry on an unusable reply. `(questions, None)`, `(None, error)` or `(None, None)` for a cap skip."""
    reminder = ''
    error = 'no reply'
    for attempt in range(2):
        result = forge_side.run('brief', plan, task['id'], prompt + reminder, spec, timeout=timeout)
        if not result['ok']:
            if result['reason'] == 'cap' and attempt == 0:
                return None, None
            if result['reason'] == 'cap':
                # The first reply used the last slot: the task still gets its error brief.
                return None, '%s (no retry: dispatch cap reached)' % error
            return None, 'dispatch failed: %s' % (result['reason'] or 'error')
        questions, error = _parse_safely(result['last'])
        if questions is not None:
            return questions, None
        reminder = '\n\n' + REMINDER % error
    return None, error


# --- write -----------------------------------------------------------------------

STORED = ('brief.json', 'brief.parked.json', 'brief.answers.json', 'brief.late.json')


def _archive(tdir: Path, stamp: str) -> None:
    """Set a task's earlier brief aside before a new one replaces it.

    Question ids restart at q1, so answers or late notes left next to a regenerated
    brief.json would attach to unrelated new questions. They move to
    `brief.history/<stamp>/` together with the old brief.json and parked file.
    """
    present = [name for name in STORED if (tdir / name).exists()]
    if not present:
        return
    base = tdir / 'brief.history'
    dest = base / stamp.replace(':', '')
    number = 1
    while dest.exists():
        number += 1
        dest = base / ('%s-%d' % (stamp.replace(':', ''), number))
    dest.mkdir(parents=True)
    for name in present:
        if name == 'brief.json':
            shutil.copy2(tdir / name, dest / name)   # stays until the new one replaces it
        else:
            os.replace(tdir / name, dest / name)


@contextlib.contextmanager
def _task_lock(plan: Path, task: str):
    """Exclusive per-task lock: two writers on one task take turns, so the second sees the first's brief.json."""
    tdir = task_dir(plan, task)
    tdir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(tdir / '.brief.lock'), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _record(plan: Path, task: str, model: str, level: str, questions, error) -> dict:
    """Store brief.json (and brief.parked.json) for one task and emit the events.

    A parked question from a grouped task records `group`: the group id for `wave`, null
    for `plan`. An ungrouped task's parked questions have no `group` key.
    """
    stamp = _now()
    group = _task_group(plan, task)
    kept, parked = [], []
    for number, question in enumerate((questions or [])[:MAX_QUESTIONS], 1):
        entry = dict(question, id='q%d' % number)
        entry['must_answer'] = bool(question['must_answer']) and level == 'gated'
        if question['scope'] == 'task':
            kept.append(entry)
        else:
            if group is not None:
                entry['group'] = group.id if question['scope'] == 'wave' else None
            parked.append(entry)
    brief = {'questions': kept, 'model': model, 'generated_at': stamp}
    if error:
        brief['error'] = error
    tdir = task_dir(plan, task)
    _archive(tdir, stamp)
    atomic_write(tdir / 'brief.json', _dump(brief))
    parked_path = tdir / 'brief.parked.json'
    if parked:
        atomic_write(parked_path, _dump({'questions': parked, 'model': model, 'generated_at': stamp}))
    else:
        try:
            parked_path.unlink()
        except OSError:
            pass
    for entry, is_parked in [(q, False) for q in kept] + [(q, True) for q in parked]:
        _emit(plan, task, entry, is_parked)
    return {'questions': len(kept), 'parked': len(parked), 'error': error}


def _emit(plan: Path, task: str, question: dict, parked: bool) -> None:
    """One `brief_question` dataset event. Fail-open, like every other capture.

    `group` is set only for a parked question that names a group. A plan question, an
    ungrouped question and a task question omit the field, so those events stay as they were.
    """
    try:
        from forge_dataset.capture import point
        body = {
            'task': str(task), 'qid': question['id'], 'scope': question['scope'],
            'must_answer': question['must_answer'], 'parked': parked}
        if question.get('group'):
            body['group'] = question['group']
        point('brief_question', str(plan), str(task), body)
    except Exception:
        pass


# Said once per plan, when brief is on at wave or plan scope and the plan has no groups.
GROUPS_NOTE = (
    'brief scope %s: no groups.tsv — run plan --groups waves, or let the planner write groups; '
    'wave questions stay parked\n')


def _note_missing_groups(plan: Path, scope: str) -> None:
    """One note when wave or plan scope has nothing to attach a wave question to.

    The marker `brief.groups.noted` is created first, so a second writer, in this process
    or another, does not say it again. The write itself is unchanged.
    """
    if scope not in ('wave', 'plan'):
        return
    if (plan / 'groups.tsv').is_file():
        return
    marker = plan / 'brief.groups.noted'
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return
    except OSError:
        sys.stderr.write(GROUPS_NOTE % scope)
        sys.stderr.flush()
        return
    try:
        os.write(fd, (scope + '\n').encode())
    except OSError:
        pass
    finally:
        os.close(fd)
    sys.stderr.write(GROUPS_NOTE % scope)
    sys.stderr.flush()


def _guard(plan) -> tuple:
    """`(plan Path, brief settings)`, or raise `NotAPlan` / `BriefOff`."""
    plan = Path(plan)
    if not plan.is_dir():
        raise NotAPlan(str(plan))
    found = forge_side.settings(plan)['brief']
    if found['level'] == 'off':
        raise BriefOff('env' if os.environ.get('FORGE_BRIEF', '').strip().lower() == 'off' else 'off')
    return plan, found


def write(plan, task=None, jobs=DEFAULT_JOBS, force=False, timeout=900, progress=None) -> list:
    """Write the questions of every task that has no brief.json (or only `task`).

    Returns one `{task, status, reason, questions, parked}` per task considered, in the
    order they were dispatched: status is `written`, `error` (an empty brief.json with the
    error recorded) or `skipped` (with a reason: `exists`, `cap`, ...). Raises `NotAPlan`,
    `BriefOff` (brief is off, or `allowed` refuses for a plan-wide reason) and `UnknownTask`.

    With brief scope `wave` or `plan` and no `groups.tsv`, prints one note to stderr and
    continues. `brief.groups.noted` keeps the note to once per plan.
    """
    plan, found = _guard(plan)
    tasks = read_tasks(plan)
    if task is not None and task not in {item['id'] for item in tasks}:
        raise UnknownTask(task)
    ok, reason = forge_side.allowed('brief', plan)
    if not ok and reason != 'cap':
        raise BriefOff(reason)
    # plan resolves these flags in the shell, before this writer starts. Say it here, once.
    _note_missing_groups(plan, found['scope'])
    chosen = [item for item in ordered(plan, tasks) if task is None or item['id'] == task]
    spec, level = found['model'], found['level']
    repo = _text(plan / 'repo').strip()
    report = {}

    mute = threading.Lock()

    def say(item, status, reason=None, stats=None):
        stats = stats or {}
        with mute:
            report[item['id']] = {'task': item['id'], 'status': status, 'reason': reason,
                                  'questions': stats.get('questions', 0), 'parked': stats.get('parked', 0)}
            if progress:
                progress(report[item['id']])

    # Workers prepare in parallel but dispatch in order. A worker may start only once the one
    # before it has really begun its dispatch (its `side/brief/<task>/` gained a new run dir)
    # or has finished without one (skipped, capped, failed; a skip waits its own turn first, so
    # the chain of earlier tasks is never cut short): saying "I am about to" is not
    # enough, the thread can still be descheduled before it dispatches.
    todo = []
    for item in chosen:
        if (task_dir(plan, item['id']) / 'brief.json').exists() and not force:
            say(item, 'skipped', 'exists')
        else:
            todo.append(item)
    intent = [threading.Event() for _ in todo]
    finished = [threading.Event() for _ in todo]
    baseline = [0] * len(todo)

    def runs(item) -> int:
        try:
            return sum(1 for _ in (plan / 'side' / 'brief' / item['id']).iterdir())
        except OSError:
            return 0

    turned = [False] * len(todo)

    def wait_for_turn(index):
        if turned[index]:
            return
        turned[index] = True
        if not index:
            return
        before = todo[index - 1]
        intent[index - 1].wait()
        while not finished[index - 1].is_set() and runs(before) <= baseline[index - 1]:
            finished[index - 1].wait(0.01)

    def work(index, item):
        try:
            with _task_lock(plan, item['id']):
                if (task_dir(plan, item['id']) / 'brief.json').exists() and not force:
                    say(item, 'skipped', 'exists')
                    return
                ok_now, why = forge_side.allowed('brief', plan)
                if not ok_now:
                    say(item, 'skipped', why)
                    return
                wait_for_turn(index)
                prompt = build_prompt(plan, item, tasks, repo)
                baseline[index] = runs(item)
                intent[index].set()
                questions, error = _ask(plan, item, prompt, spec, timeout)
                if questions is None and error is None:
                    say(item, 'skipped', 'cap')
                    return
                try:
                    stats = _record(plan, item['id'], spec, level, questions, error)
                except Exception as failure:   # noqa: BLE001
                    error = 'could not store the reply (%s)' % type(failure).__name__
                    stats = _record(plan, item['id'], spec, level, [], error)
                say(item, 'error' if error else 'written', error, stats)
        except BaseException as failure:
            say(item, 'error', 'internal error: %s' % failure)
            try:   # a broken writer must not leave the task without a brief.json
                if not (task_dir(plan, item['id']) / 'brief.json').exists():
                    _record(plan, item['id'], spec, level, [], 'internal error: %s' % type(failure).__name__)
            except Exception:   # noqa: BLE001
                pass
        finally:
            # A worker that skips still takes its turn before it signals: otherwise the one
            # after it would see "done" and overtake an earlier task that has not dispatched.
            wait_for_turn(index)
            intent[index].set()
            finished[index].set()

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(jobs)))
    try:
        for future in [pool.submit(work, index, item) for index, item in enumerate(todo)]:
            future.result()
    finally:
        pool.shutdown(wait=True)
    return [report[item['id']] for item in chosen]


# --- show ------------------------------------------------------------------------

def _entries(data) -> dict:
    """`{qid: entry}` from an answers or late file: a dict by qid, or a list of entries with a `qid`."""
    if isinstance(data, dict):
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    if isinstance(data, list):
        return {str(v['qid']): v for v in data if isinstance(v, dict) and 'qid' in v}
    return {}


def _questions(data) -> list:
    items = data.get('questions') if isinstance(data, dict) else None
    return [q for q in items if isinstance(q, dict)] if isinstance(items, list) else []


def _affected_rows(plan, tasks, only=None) -> list:
    """`brief.affected` markers in tasks.tsv order: `{task, pid, reason, t, flag_only}`.

    A task with no brief.json is included when it has a marker (an added task that was
    marked and not regenerated yet). A marker that is not the object `apply` writes is skipped.
    """
    rows = []
    for item in tasks:
        if only is not None and item['id'] != only:
            continue
        data = _json(task_dir(plan, item['id']) / 'brief.affected')
        if not isinstance(data, dict):
            continue
        pid, reason = data.get('pid'), data.get('reason')
        if not isinstance(pid, str) or not pid or not isinstance(reason, str) or not reason:
            continue
        rows.append({'task': item['id'], 'pid': pid, 'reason': reason, 't': data.get('t'),
                     'flag_only': data.get('flag_only') is True})
    return rows


def collect(plan, task=None) -> dict:
    """Every question of the plan (or one task) with its state, flagged open ones first.

    `{questions: [...], parked: [...], errors: [{task, error}], affected: [...]}`. A question
    carries `task`, `id`, `text`, `why`, `options`, `default`, `scope`, `must_answer`, `state`
    and, once answered, `answer`. A parked question stores `group` when its file has that
    key: the group id, or null for a plan question. `affected` is the `brief.affected` markers
    (`task`, `pid`, `reason`, `t`, `flag_only`), in tasks.tsv order.
    """
    plan, _ = _guard(plan)
    tasks = read_tasks(plan)
    if task is not None and task not in {item['id'] for item in tasks}:
        raise UnknownTask(task)
    affected = _affected_rows(plan, tasks, task)
    questions, parked, errors = [], [], []
    for item in ordered(plan, tasks):
        if task is not None and item['id'] != task:
            continue
        tdir = task_dir(plan, item['id'])
        brief = _json(tdir / 'brief.json')
        if brief is None:
            continue
        if isinstance(brief, dict) and brief.get('error'):
            errors.append({'task': item['id'], 'error': str(brief['error'])})
        answers = _entries(_json(tdir / 'brief.answers.json'))
        late = _entries(_json(tdir / 'brief.late.json'))
        whole_stale = isinstance(brief, dict) and brief.get('possibly_stale') is True
        for q in _questions(brief):
            qid = str(q.get('id', ''))
            row = {'task': item['id'], 'id': qid, 'text': q.get('text', ''), 'why': q.get('why', ''),
                   'options': q.get('options') or [], 'default': q.get('default', ''),
                   'scope': q.get('scope', 'task'), 'must_answer': bool(q.get('must_answer'))}
            if qid in answers:
                row['state'], row['answer'] = 'answered', answers[qid]
            elif qid in late:
                row['state'], row['answer'] = 'late', late[qid]
            elif q.get('possibly_stale') is True or whole_stale:
                row['state'] = 'possibly_stale'
            else:
                row['state'] = 'open'
            questions.append(row)
        for q in _questions(_json(tdir / 'brief.parked.json')):
            row = {'task': item['id'], 'id': str(q.get('id', '')), 'text': q.get('text', ''),
                   'why': q.get('why', ''), 'options': q.get('options') or [],
                   'default': q.get('default', ''), 'scope': q.get('scope', 'wave'),
                   'must_answer': bool(q.get('must_answer')), 'state': 'parked'}
            if 'group' in q:
                row['group'] = q.get('group')
            parked.append(row)

    def rank(row):
        if row['state'] in ('open', 'possibly_stale'):
            return 0 if row['must_answer'] else 1
        return 2
    questions.sort(key=rank)   # stable: wave order and question order inside a rank
    return {'questions': questions, 'parked': parked, 'errors': errors, 'affected': affected}


def _question_lines(row) -> list:
    flag = ' [must answer]' if row['must_answer'] else ''
    out = ['%s/%s  %s%s' % (row['task'], row['id'], row['state'], flag), '    %s' % row['text']]
    if row['options']:
        out.append('    options: %s' % ' | '.join(row['options']))
    if row['default']:
        out.append('    default: %s' % row['default'])
    if row.get('answer'):
        out.append('    answer: %s (%s)' % (row['answer'].get('text', ''), row['answer'].get('actor', '')))
    return out


def _group_titles(plan) -> dict:
    if not plan:
        return {}
    try:
        return {g.id: g.title for g in forge_groups.load(plan)}
    except forge_groups.GroupError:
        return {}


def _grouped_parked_lines(rows, plan) -> list:
    """Parked questions that carry a `group` key, listed by group, then under `Plan:`.

    A wave question is `Group <gid> (<title>): <question>`, with the asking task on the
    next line. The title comes from groups.tsv and the parentheses are left off when it
    is `-` or the group is gone. A plan question (`group` null) goes under `Plan:`.
    A question with no `group` key, in a plan that also has grouped ones, stays in the
    old parked block so an older file is still shown.
    """
    titles = _group_titles(plan)
    order, by, plans, loose = [], {}, [], []
    for row in rows:
        if 'group' not in row:
            loose.append(row)
            continue
        gid = row.get('group')
        if not isinstance(gid, str) or not gid:
            plans.append(row)
            continue
        if gid not in by:
            order.append(gid)
            by[gid] = []
        by[gid].append(row)
    lines = []
    for gid in order:
        title = titles.get(gid, '-')
        head = 'Group %s (%s)' % (gid, title) if title and title != '-' else 'Group %s' % gid
        for row in by[gid]:
            lines.append('%s: %s' % (head, row['text']))
            flag = ' [must answer]' if row['must_answer'] else ''
            lines.append('    %s/%s  %s%s' % (row['task'], row['id'], row['state'], flag))
            if row['options']:
                lines.append('    options: %s' % ' | '.join(row['options']))
            if row['default']:
                lines.append('    default: %s' % row['default'])
    if plans:
        lines.append('Plan:')
        for row in plans:
            lines.extend(_question_lines(row))
    if loose:
        lines.append('Parked (wider scope):')
        for row in loose:
            lines.extend(_question_lines(row))
    return lines


def _affected_lines(rows) -> list:
    """One line per marked task: `a  affected by p3`, plus `flagged (already started)`."""
    lines = []
    for row in rows:
        line = '%s  affected by %s' % (row['task'], row['pid'])
        if row.get('flag_only'):
            line += '  flagged (already started)'
        lines.append(line)
    return lines


def render(found: dict, plan=None) -> str:
    """Text for `forge brief show`. `plan` is where group titles are read.

    A plan whose parked questions have no `group` key renders as it did before groups.
    Marked tasks are listed after the questions (`affected by <pid>`).
    """
    lines = []
    for row in found['questions']:
        lines.extend(_question_lines(row))
    parked = found['parked']
    if parked:
        lines.append('')
        if any('group' in row for row in parked):
            lines.extend(_grouped_parked_lines(parked, plan))
        else:
            lines.append('Parked (wider scope):')
            for row in parked:
                lines.extend(_question_lines(row))
    for err in found['errors']:
        lines.append('%s: the question writer failed: %s' % (err['task'], err['error']))
    affected = _affected_lines(found.get('affected') or [])
    if affected:
        if lines:
            lines.append('')
        lines.extend(affected)
    if not lines:
        lines.append('no questions')
    return '\n'.join(lines) + '\n'
