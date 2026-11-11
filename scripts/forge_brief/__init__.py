"""Brief mode: each task's open questions, asked before the task starts (#135).

`write` makes one read-only `side` dispatch of kind `brief` per task and stores the reply:

- `tasks/<id>/brief.json`: `{questions: [{id, text, why, options, default, scope: "task",
  must_answer}], model, generated_at}`. `must_answer` survives only under the `gated` level.
- `tasks/<id>/brief.parked.json`: the same shape for questions whose scope is `wave` or
  `plan`. Only task scope acts in this issue, so wider scopes are parked whatever
  `brief_scope` says.

A broken writer never holds a task: a reply that is not valid JSON, or has the wrong
shape, is retried once, and a second failure (or a failed dispatch) stores
`{"questions": [], "error": "<reason>"}` as `brief.json`.

`show` lists every question with its state: `open`, `answered` (`brief.answers.json`),
`late` (`brief.late.json`) or `possibly_stale` (a flag in brief.json).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import time

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_side  # noqa: E402

MAX_QUESTIONS = 3
DEP_PROMPT_LIMIT = 4096
TASK_TEXT_LIMIT = 16384
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

def build_prompt(plan, task: dict, tasks: list, repo: str) -> str:
    plan = Path(plan)
    tdir = task_dir(plan, task['id'])
    by_id = {item['id']: item for item in tasks}
    parts = [
        'You are preparing the open questions for ONE task of a multi-task coding run, '
        'before an implementer starts it.\n'
        'Read the task below. The repository is at %s: you may read it and you must not '
        'write to it.\n' % repo,
        '## The task: %s\n' % task['id'],
        '### prompt.md\n\n%s\n' % forge_side.cap(_text(tdir / 'prompt.md').strip(), TASK_TEXT_LIMIT),
    ]
    approach = _text(tdir / 'approach.md').strip()
    if approach:
        parts.append('### approach.md\n\n%s\n' % forge_side.cap(approach, TASK_TEXT_LIMIT))
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
    parts.append(
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
        '- scope: "task" when only this task needs the answer, "wave" when the other tasks '
        'that run alongside it do too, "plan" when the whole run does.\n'
        '- must_answer: true only when the decision cannot be defaulted safely.\n' % MAX_QUESTIONS)
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

    Each question is `{text, why, options, default, scope, must_answer}`; a missing
    `why`, `options`, `default`, `scope` or `must_answer` takes its empty value (scope
    `task`). A wrong type, an unknown scope or an empty text makes the reply unusable.
    """
    if not isinstance(reply, str) or not reply.strip():
        return None, 'empty reply'
    text = _strip_fence(reply)
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find('{'), text.rfind('}')
        data = None
        if 0 <= start < end:
            try:
                data = json.loads(text[start:end + 1])
            except ValueError:
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
        text_value = item.get('text')
        if not isinstance(text_value, str) or not text_value.strip():
            return None, '%s has no text' % where
        why = item.get('why', '')
        default = item.get('default', '')
        if not isinstance(why, str) or not isinstance(default, str):
            return None, '%s: why and default must be strings' % where
        options = item.get('options', [])
        if not isinstance(options, list) or not all(isinstance(opt, str) for opt in options):
            return None, '%s: options must be a list of strings' % where
        scope = item.get('scope', 'task')
        if scope not in SCOPES:
            return None, '%s: scope must be task, wave or plan' % where
        must = item.get('must_answer', False)
        if not isinstance(must, bool):
            return None, '%s: must_answer must be true or false' % where
        found.append({'text': text_value.strip(), 'why': why.strip(), 'options': [o.strip() for o in options],
                      'default': default.strip(), 'scope': scope, 'must_answer': must})
    return found, None


def _ask(plan, task: dict, prompt: str, spec: str, timeout: int):
    """One dispatch, a retry on an unusable reply. `(questions, None)`, `(None, error)` or `(None, None)` for a cap skip."""
    reminder = ''
    error = 'no reply'
    for attempt in range(2):
        result = forge_side.run('brief', plan, task['id'], prompt + reminder, spec, timeout=timeout)
        if not result['ok']:
            if result['reason'] == 'cap':
                return None, None
            return None, 'dispatch failed: %s' % (result['reason'] or 'error')
        questions, error = parse_reply(result['last'])
        if questions is not None:
            return questions, None
        reminder = '\n\n' + REMINDER % error
    return None, error


# --- write -----------------------------------------------------------------------

def _record(plan: Path, task: str, model: str, level: str, questions, error) -> dict:
    """Store brief.json (and brief.parked.json) for one task and emit the events."""
    stamp = _now()
    kept, parked = [], []
    for number, question in enumerate((questions or [])[:MAX_QUESTIONS], 1):
        entry = dict(question, id='q%d' % number)
        entry['must_answer'] = bool(question['must_answer']) and level == 'gated'
        (kept if question['scope'] == 'task' else parked).append(entry)
    brief = {'questions': kept, 'model': model, 'generated_at': stamp}
    if error:
        brief['error'] = error
    tdir = task_dir(plan, task)
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
    """One `brief_question` dataset event. Fail-open, like every other capture."""
    try:
        from forge_dataset.capture import point
        point('brief_question', str(plan), str(task), {
            'task': str(task), 'qid': question['id'], 'scope': question['scope'],
            'must_answer': question['must_answer'], 'parked': parked})
    except Exception:
        pass


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
    """
    plan, found = _guard(plan)
    tasks = read_tasks(plan)
    if task is not None and task not in {item['id'] for item in tasks}:
        raise UnknownTask(task)
    ok, reason = forge_side.allowed('brief', plan)
    if not ok and reason != 'cap':
        raise BriefOff(reason)
    chosen = [item for item in ordered(plan, tasks) if task is None or item['id'] == task]
    spec, level = found['model'], found['level']
    repo = _text(plan / 'repo').strip()
    report = {}

    def say(item, status, reason=None, stats=None):
        stats = stats or {}
        report[item['id']] = {'task': item['id'], 'status': status, 'reason': reason,
                              'questions': stats.get('questions', 0), 'parked': stats.get('parked', 0)}
        if progress:
            progress(report[item['id']])

    todo = []
    for item in chosen:
        if (task_dir(plan, item['id']) / 'brief.json').exists() and not force:
            say(item, 'skipped', 'exists')
        else:
            todo.append(item)

    def work(item):
        ok_now, why = forge_side.allowed('brief', plan)
        if not ok_now:
            return item, None, why
        prompt = build_prompt(plan, item, tasks, repo)
        questions, error = _ask(plan, item, prompt, spec, timeout)
        if questions is None and error is None:
            return item, None, 'cap'
        return item, (questions, error), None

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(jobs)))
    try:
        futures = [pool.submit(work, item) for item in todo]
        for future in concurrent.futures.as_completed(futures):
            item, result, skipped = future.result()
            if skipped:
                say(item, 'skipped', skipped)
                continue
            questions, error = result
            if not force and (task_dir(plan, item['id']) / 'brief.json').exists():
                say(item, 'skipped', 'exists')
                continue
            stats = _record(plan, item['id'], spec, level, questions, error)
            say(item, 'error' if error else 'written', error, stats)
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


def collect(plan, task=None) -> dict:
    """Every question of the plan (or one task) with its state, flagged open ones first.

    `{questions: [...], parked: [...], errors: [{task, error}]}`. A question carries
    `task`, `id`, `text`, `why`, `options`, `default`, `scope`, `must_answer`, `state`
    and, once answered, `answer`.
    """
    plan, _ = _guard(plan)
    tasks = read_tasks(plan)
    if task is not None and task not in {item['id'] for item in tasks}:
        raise UnknownTask(task)
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
            parked.append({'task': item['id'], 'id': str(q.get('id', '')), 'text': q.get('text', ''),
                           'why': q.get('why', ''), 'options': q.get('options') or [],
                           'default': q.get('default', ''), 'scope': q.get('scope', 'wave'),
                           'must_answer': bool(q.get('must_answer')), 'state': 'parked'})

    def rank(row):
        if row['state'] in ('open', 'possibly_stale'):
            return 0 if row['must_answer'] else 1
        return 2
    questions.sort(key=rank)   # stable: wave order and question order inside a rank
    return {'questions': questions, 'parked': parked, 'errors': errors}


def render(found: dict) -> str:
    lines = []

    def block(row):
        flag = ' [must answer]' if row['must_answer'] else ''
        lines.append('%s/%s  %s%s' % (row['task'], row['id'], row['state'], flag))
        lines.append('    %s' % row['text'])
        if row['options']:
            lines.append('    options: %s' % ' | '.join(row['options']))
        if row['default']:
            lines.append('    default: %s' % row['default'])
        if row.get('answer'):
            lines.append('    answer: %s (%s)' % (row['answer'].get('text', ''), row['answer'].get('actor', '')))
    for row in found['questions']:
        block(row)
    if found['parked']:
        lines.append('')
        lines.append('Parked (wider scope):')
        for row in found['parked']:
            block(row)
    for err in found['errors']:
        lines.append('%s: the question writer failed: %s' % (err['task'], err['error']))
    if not lines:
        lines.append('no questions')
    return '\n'.join(lines) + '\n'
