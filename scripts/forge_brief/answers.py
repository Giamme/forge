"""Record brief answers, and flag questions a merge may have made stale (#135).

`answer` writes `tasks/<id>/brief.answers.json` as `{qid: {text, actor, t}}` while the
task is still PENDING and has no `attempts.tsv` row. Once it is past starting, nothing
about the task changes: the same object goes to `brief.late.json` instead, and the
command tells the caller the `forge control add` line for a follow-up task.

`stale` runs after a task has merged. When that merge's `changes.diff` (or, if the diff
is gone, the task's attempt events) names a file the task did not declare, every open
question of a direct dependent that has not started gets `possibly_stale: true`.
"""
from __future__ import annotations

import importlib.util
import json
import shlex
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import forge_brief

# Bytes, matching the cap forge-prompt.py uses when it quotes an answer.
ANSWER_CAP = 4 * 1024
# Room to strip control characters before the cap, without reading an unbounded file.
READ_LIMIT = 1024 * 1024

_CONTROL = None


class Usage(Exception):
    """The answer text or the actor is not usable. The command exits 2."""


class NoSuchTask(Exception):
    """The task is not in this plan. `valid` is the ids the plan does have."""

    def __init__(self, task, valid):
        super().__init__(task)
        self.task = task
        self.valid = list(valid)


class NoSuchQuestion(Exception):
    """The task has no question with this id. `valid` is the ids in its brief.json."""

    def __init__(self, task, qid, valid):
        super().__init__(qid)
        self.task = task
        self.qid = qid
        self.valid = list(valid)


def _control():
    """forge-control.py, loaded once. `audit` is the control.log writer to reuse."""
    global _CONTROL
    if _CONTROL is None:
        path = Path(forge_brief.SCRIPTS) / 'forge-control.py'
        spec = importlib.util.spec_from_file_location('forge_control_cli', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTROL = module
    return _CONTROL


def _norm(path) -> str:
    """A repo-relative path, comparable between tasks.tsv and a diff."""
    text = str(path).strip().replace('\\', '/')
    while text.startswith('./'):
        text = text[2:]
    return text


def status_of(plan, task) -> str:
    """The task's status, read the way `task_status` reads it.

    `tasks/<id>/merged` is MERGED even when `status` still says PASS: the merge
    touches that file and does not rewrite status.
    """
    tdir = forge_brief.task_dir(plan, task)
    if (tdir / 'merged').is_file():
        return 'MERGED'
    try:
        words = (tdir / 'status').read_text(encoding='utf-8', errors='replace').split()
    except OSError:
        return 'PENDING'
    return words[0] if words else 'PENDING'


def _attempt_rows(plan, task) -> bool:
    path = forge_brief.task_dir(plan, task) / 'attempts.tsv'
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return False
    return any(line.strip() and not line.startswith('#') for line in text.splitlines())


def past_start(plan, task) -> bool:
    """Whether an answer can no longer change this task.

    Past starting when `status` is RUNNING or any finished state (MERGED, FAIL,
    BLOCKED, NEEDS_*, ACCEPTED, and every other status forge writes), or when
    `attempts.tsv` has a row. Only PENDING with no attempt row is still in time.
    """
    if _attempt_rows(plan, task):
        return True
    return status_of(plan, task) != 'PENDING'


def question_ids(plan, task) -> list:
    """Ids of the questions in `brief.json`, in file order. Parked questions are not answers."""
    data = forge_brief._json(forge_brief.task_dir(plan, task) / 'brief.json')
    found = []
    for question in forge_brief._questions(data if isinstance(data, dict) else None):
        qid = question.get('id')
        if isinstance(qid, str) and qid:
            found.append(qid)
    return found


def clean_answer(text) -> str:
    """Drop control characters, then keep the leading 4 KB of what remains."""
    if text is None:
        text = ''
    elif not isinstance(text, str):
        text = str(text)
    kept = ''.join(ch for ch in text if unicodedata.category(ch) != 'Cc')
    try:
        raw = kept.encode('utf-8')
    except UnicodeEncodeError:
        raw = kept.encode('utf-8', errors='ignore')
        kept = raw.decode('utf-8')
    if len(raw) <= ANSWER_CAP:
        return kept
    return raw[:ANSWER_CAP].decode('utf-8', errors='ignore')


def _read_file(file) -> str:
    if file == '-':
        return sys.stdin.read(READ_LIMIT)
    path = Path(file)
    try:
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            return handle.read(READ_LIMIT)
    except OSError as error:
        raise Usage('cannot read %s: %s' % (file, error.strerror or error)) from error


def _actor(value) -> str:
    if value is None:
        return 'cli'
    actor = ''.join(ch for ch in str(value) if unicodedata.category(ch) != 'Cc').strip()
    if not actor or any(ch.isspace() for ch in actor):
        raise Usage('actor must be a single token')
    if len(actor) > 64:
        raise Usage('actor is too long')
    return actor


def _load_map(path) -> dict:
    return forge_brief._entries(forge_brief._json(path))


def _store(path, data: dict) -> None:
    forge_brief.atomic_write(path, forge_brief._dump(data))


def _put(path, qid, record) -> str | None:
    """Replace `qid` and keep every other answer. The previous text, if there was one."""
    current = _load_map(path)
    old = current.get(qid)
    previous = old.get('text') if isinstance(old, dict) and isinstance(old.get('text'), str) else None
    current[qid] = record
    _store(path, current)
    return previous


def _follow_up_id(tasks, task, qid) -> str:
    """A task id that is not already in the plan, so `control add` can queue a new row."""
    taken = {item['id'] for item in tasks}
    base = ('%s-%s' % (task, qid)).replace('/', '-').replace('\0', '')
    if base in ('', '.', '..'):
        base = 'follow-up-%s' % qid.replace('/', '-').replace('\0', '') or 'follow-up'
    candidate = base
    number = 2
    while candidate in taken or candidate in ('', '.', '..'):
        candidate = '%s-%d' % (base, number)
        number += 1
    return candidate


def follow_up_line(plan, follow_id) -> str:
    """The `forge control add` invocation: plan, then the task id. Nothing else is required."""
    return 'forge control add %s %s' % (shlex.quote(str(plan)), shlex.quote(follow_id))


def _log(plan, actor, task, qid, text, previous, late) -> None:
    """One control.log line in the shape `audit` writes, then the `brief_answer` event."""
    try:
        control = _control()
    except Exception:
        entry = {'at': time.time(), 'actor': actor, 'action': 'answer', 'task': task,
                 'key': qid, 'value': text, 'previous': previous, 'late': bool(late)}
        with (Path(plan) / 'control.log').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry) + '\n')
    else:
        with control.control_lock(str(plan)):
            control.audit(str(plan), actor, 'answer', task, qid, text, previous, late=bool(late))
    _emit(plan, task, qid, actor, late)


def _emit(plan, task, qid, actor, late) -> None:
    """One `brief_answer` dataset event. Fail-open, like every other capture."""
    try:
        from forge_dataset.capture import point
        point('brief_answer', str(plan), str(task), {
            'task': str(task), 'qid': qid, 'actor': actor, 'late': bool(late)})
    except Exception:
        pass


def answer(plan, task, qid, text=None, actor='cli', file=None) -> dict:
    """Record one answer. Raises `Usage`, `NotAPlan`, `NoSuchTask` or `NoSuchQuestion`.

    Returns `{task, qid, late, text, message}`. `message` is what the command prints.
    A late answer's message names the `forge control add` line for a follow-up task.
    """
    if file is not None and text is not None:
        raise Usage('give the answer as text or --file, not both')
    if file is None and text is None:
        raise Usage('give the answer as text or --file')
    who = _actor(actor)
    cleaned = clean_answer(_read_file(file) if file is not None else text)
    if not cleaned:
        raise Usage('the answer is empty')
    plan = Path(plan)
    tasks = forge_brief.read_tasks(plan)
    ids = [item['id'] for item in tasks]
    if task not in ids:
        raise NoSuchTask(task, ids)
    valid = question_ids(plan, task)
    if qid not in valid:
        raise NoSuchQuestion(task, qid, valid)
    tdir = forge_brief.task_dir(plan, task)
    with forge_brief._task_lock(plan, task):
        # The brief may have been regenerated while this answer waited for the lock.
        if qid not in question_ids(plan, task):
            raise NoSuchQuestion(task, qid, question_ids(plan, task))
        late = past_start(plan, task)
        record = {'text': cleaned, 'actor': who, 't': time.time()}
        target = tdir / ('brief.late.json' if late else 'brief.answers.json')
        previous = _put(target, qid, record)
    _log(plan, who, task, qid, cleaned, previous, late)
    if late:
        follow = _follow_up_id(tasks, task, qid)
        message = ('too late for task %s: the answer does not change this task\n%s\n'
                   % (task, follow_up_line(plan, follow)))
    else:
        message = '%s/%s answered\n' % (task, qid)
    return {'task': task, 'qid': qid, 'late': late, 'text': cleaned, 'message': message}


# --- stale -----------------------------------------------------------------------

def _declared(files_field) -> set:
    if not files_field or str(files_field).strip() in ('', '-'):
        return set()
    return {_norm(part) for part in str(files_field).split(',') if part.strip() and part.strip() != '-'}


def _diff_files(plan, task) -> set | None:
    """Files in `changes.diff`, or None when that diff is not on disk."""
    path = forge_brief.task_dir(plan, task) / 'changes.diff'
    if not path.is_file():
        return None
    from forge_dataset.harvest import diff_summary
    text = path.read_text(encoding='utf-8', errors='replace')
    summary = diff_summary(text)
    return {_norm(name) for name in summary.get('changed_files') or [] if isinstance(name, str)}


def _dataset_files(plan, task) -> set:
    """`changed_files` of this plan's attempt events for the task, when the diff is gone."""
    repo = forge_brief._text(Path(plan) / 'repo').strip()
    if not repo:
        return set()
    try:
        from forge_dataset import dataset_dir, repo_id
        path = dataset_dir() / 'dataset.sqlite'
        if not path.is_file():
            return set()
        # The events table stores repo_id(repo), the same key live capture writes.
        query = 'SELECT data FROM events WHERE kind = ? AND task = ? AND repo = ?'
        params = ['attempt', task, repo_id(repo)]
        run_id = forge_brief._text(Path(plan) / 'run_id').strip()
        if run_id:
            query += ' AND run_id = ?'
            params.append(run_id)
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute(query, params).fetchall()
        finally:
            db.close()
    except Exception:
        return set()
    found = set()
    for (raw,) in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        files = data.get('changed_files') if isinstance(data, dict) else None
        if isinstance(files, list):
            found.update(_norm(name) for name in files if isinstance(name, str))
    return found


def changed_files(plan, task) -> set:
    """What the merge changed. The diff on disk wins: an earlier attempt may have been reverted."""
    found = _diff_files(plan, task)
    if found is not None:
        return found
    return _dataset_files(plan, task)


def _flag_open(plan, task) -> list:
    """Set `possibly_stale` on this task's open questions. `[(task, qid), ...]` in file order."""
    tdir = forge_brief.task_dir(plan, task)
    path = tdir / 'brief.json'
    if not path.is_file():
        return []
    with forge_brief._task_lock(plan, task):
        if past_start(plan, task):
            return []
        brief = forge_brief._json(path)
        if not isinstance(brief, dict) or not isinstance(brief.get('questions'), list):
            return []
        answered = set(_load_map(tdir / 'brief.answers.json'))
        answered.update(_load_map(tdir / 'brief.late.json'))
        flagged = []
        changed = False
        for question in brief['questions']:
            if not isinstance(question, dict):
                continue
            qid = question.get('id')
            if not isinstance(qid, str) or not qid or qid in answered:
                continue
            flagged.append((task, qid))
            if question.get('possibly_stale') is not True:
                question['possibly_stale'] = True
                changed = True
        if changed:
            forge_brief.atomic_write(path, forge_brief._dump(brief))
        return flagged


def stale(plan, task) -> list:
    """Flag direct dependents' open questions when `task` merged outside its declared files.

    Returns `[(dependent, qid), ...]` in tasks.tsv order, then question order. An unknown
    task raises `NoSuchTask`. A task that is not merged, or whose changes stay inside its
    declared files, flags nothing.
    """
    plan = Path(plan)
    tasks = forge_brief.read_tasks(plan)
    by_id = {item['id']: item for item in tasks}
    if task not in by_id:
        raise NoSuchTask(task, [item['id'] for item in tasks])
    if status_of(plan, task) != 'MERGED':
        return []
    if not (changed_files(plan, task) - _declared(by_id[task]['files'])):
        return []
    flagged = []
    for item in tasks:
        if task not in item['deps'] or past_start(plan, item['id']):
            continue
        flagged.extend(_flag_open(plan, item['id']))
    return flagged
