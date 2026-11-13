"""Wave-scoped proposals: an answer to a parked wave question becomes a diff that waits for approval (#136).

forge never re-plans silently. A person answers a parked `wave` question
(`brief.parked.answers.json`, see answers.py); `propose` asks the plan's brief model what that
answer changes in the question's group, and records the result as `<plan>/proposals/<pid>/`:

- `proposal.json`: `{pid, scope: "wave", group, task, qid, answer, summary, changes, state,
  reason, created, actor}`. `state` is `proposed`, `refused`, `approved` or `rejected`.
- `diff.patch`: a unified diff (`a/` and `b/` paths relative to the plan) of every file the
  proposal would change: `tasks/<id>/prompt.md`, `tasks/<id>/approach.md` and, for a `files`
  change, `tasks.tsv`.
- `hash`: the sha256 of `diff.patch` and the current contents of every target (a `files` target
  is the task's `tasks.tsv` row). `apply --approve <hash>` must quote it, so an approval is for
  exactly the diff that was shown.

Wave confinement. Every change must name a member of the question's group that has not started
(a status other than PENDING, an `attempt` file or an `attempts.tsv` row) and may only carry
`prompt`, `approach` and `files`. A change that names another task, a started member or any other
field is not dropped: the whole proposal is `refused` with the reason, its diff is still written
for information, and `apply` never takes it. A reply that changes nothing is refused as well.

`apply` takes the plan's control lock (and a proposals lock that `reject` shares) and refuses,
with nothing changed, when the hash differs, the state is not `proposed`, a target has started,
or a target changed since the proposal. A `files` change is validated with `forge_groups.validate`
against a copy of the new tasks.tsv before anything is written; if it fails nothing is written.
Otherwise the files are written atomically, `groups.frozen` is re-frozen in the same step (the
fingerprint covers each member's files) and `waves.tsv` is recomputed the way `plan` does
(`forge-parallel.sh _waves`; skipped when the plan has no waves.tsv yet, since that file is what
marks a plan as planned). A write that fails halfway restores what it had written.

A live run. `_task` (`do_task_body` in forge-parallel.sh) builds `dwarf.input` at the start of
every attempt from `tasks/<id>/prompt.md` and `approach.md` (`forge-prompt.py --requirements ...
--approach ...`), read at dispatch: an unstarted task picks up an applied prompt or approach
change. The scheduler, though, reads tasks.tsv once when it starts and decides overlap from those
cached rows (`schedule()` in forge-schedule.py, `tasks[name][3]`; only `forge control add` and an
escalation refresh a row). So while a scheduler is live `apply` refuses a `files` change and says
to apply it after the run; prompt and approach changes go through. Apply checks the start state
under the lock, but the scheduler does not take that lock: a task that starts in the instant
between the check and the write reads whichever prompt it finds, as it would for any edit.

Plan scope (`scope: "plan"`). A parked `plan` question (answerable under `--brief-scope plan`) can
change the plan's structure: `changes` is then `{edits, add, remove, deps}`: `edits` are the wave
kind of change (prompt, approach, files) on any unstarted task, `add` new tasks, `remove` unstarted
ones and `deps` new dependency lists for unstarted tasks (a split is a remove plus adds). Validation
(`_plan_check`) refuses, with the reason, whatever would not be a plan `plan` could have written. The
hash covers the diff, `tasks.tsv`, `groups.tsv` and every edited prompt or approach file. `apply`
with no live run writes `tasks.tsv`, `groups.tsv` and the task dirs (a removed task's dir moves to
`tasks/.removed/<id>-<pid>/`, never deleted; added rows get dwarf and qa `-`) and writes a stale
marker into `groups.frozen` (the fingerprint covers only groups.tsv and the members' rows, so an
ungrouped change, or a plan with no groups, would not otherwise stop `run`): `run` then refuses until
`plan` validates, routes and freezes again, which overwrites the marker or removes it. A wave apply
meanwhile keeps the marker. With a live run only additions apply, queued in dependency order, each
through `forge control add`; the task joins no group.

`brief_diff` events (`{pid, scope, group, state, tasks}`) record proposed, refused, approved and
rejected. An approved event also carries `affected` and `flagged`, the task ids `apply` marked.
The dispatch is a read-only `side` dispatch of kind `brief-change`, on the plan's brief
model, under the same `allowed` checks as the brief writer.

After a successful apply, every task the proposal changed or added, and every unstarted task
that depends (directly or transitively) on a changed, added or removed task, is taken from the
post-apply `tasks.tsv` plus the removed ids. Each gets `tasks/<id>/brief.affected`. An unstarted
one is `{pid, reason, t}` with reason `changed`, `added` or `dependent`, and its questions are
regenerated with `write --force`: an answer whose question text is still there is kept, and any
other answer goes to `brief.answers.prev.json`. A task that has already started (running, merged,
or anything else past PENDING) is not rewritten and not dispatched; its marker sets `flag_only`.
`apply --no-regenerate` writes the markers and skips the dispatches. The next approved proposal
that touches a task replaces that task's marker; earlier proposals stay where they are.
"""
from __future__ import annotations

import argparse
import contextlib
import difflib
import io
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import calendar
import time

import forge_brief
import forge_brief.answers as answers
import forge_groups
import forge_side

CHANGE_FIELDS = ('prompt', 'approach', 'files')
FILE_CAP = 6 * 1024
DIFFICULTIES = ('low', 'medium', 'high')
ACCEPTANCE_RE = re.compile(r'^#{2,}\s+Acceptance criteria\s*$', re.IGNORECASE | re.MULTILINE)
PLAN_KEYS = ('edits', 'add', 'remove', 'deps')
ADD_KEYS = ('id', 'deps', 'difficulty', 'files', 'title', 'prompt', 'approach', 'group')
PID_RE = re.compile(r'p[1-9][0-9]*')
STATES = ('proposed', 'refused', 'approved', 'rejected')

REMINDER = (
    'Your last reply was not usable: %s.\n'
    'Reply again with the JSON object only, in exactly the format above: no prose, no code fence.\n')


class Refused(Exception):
    """The proposal, the apply or the reject cannot go ahead. `reason` is why. The command exits 3."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class NoSuchProposal(Exception):
    """No `proposals/<pid>/proposal.json`. `valid` is the ids the plan does have. The command exits 2."""

    def __init__(self, pid, valid):
        super().__init__(pid)
        self.pid = pid
        self.valid = list(valid)


# --- reading the plan ------------------------------------------------------------------

def _read_bytes(path):
    try:
        return Path(path).read_bytes()
    except OSError:
        return None


def _text(data) -> str:
    return '' if data is None else data.decode('utf-8', errors='replace')


def started(plan, task) -> bool:
    """Whether the task is past PENDING: any status but PENDING (or merged), an `attempt` file or an `attempts.tsv` row."""
    if (forge_brief.task_dir(plan, task) / 'attempt').exists():
        return True
    return answers.past_start(plan, task)


def _group_of(plan, gid):
    try:
        groups = forge_groups.load(plan)
    except forge_groups.GroupError as error:
        raise Refused('groups.tsv cannot be read: %s' % ' '.join(str(error).split()))
    for group in groups:
        if group.id == gid:
            return group
    return None


def _task_file(plan, task, name) -> Path:
    return forge_brief.task_dir(plan, task) / name


def _rel(task, field) -> str:
    return 'tasks/%s/%s.md' % (task, field)


def _tsv_lines(plan) -> list:
    return _text(_read_bytes(Path(plan) / 'tasks.tsv')).splitlines(True)


def _row_files(plan, task):
    """The task's `tasks.tsv` row and its files column, or `(None, None)`."""
    for item in forge_brief.read_tasks(plan):
        if item['id'] == task:
            return item['row'], item['files']
    return None, None


def _with_files(lines: list, new_files: dict) -> list:
    """The tasks.tsv lines with the files column of each `{task: files}` replaced. Every other byte stays."""
    out = []
    for line in lines:
        body = line.rstrip('\n')
        cols = body.split('\t')
        if body.strip() and not body.startswith('#') and len(cols) >= 7 and cols[0] in new_files:
            cols[3] = new_files[cols[0]]
            line = '\t'.join(cols) + ('\n' if line.endswith('\n') else '')
        out.append(line)
    return out


# --- the diff and its hash -------------------------------------------------------------

def _udiff(rel: str, old, new: str) -> str:
    """A unified diff of one file. `old` is None when the file does not exist yet."""
    old_lines = [] if old is None else old.splitlines(True)
    out = []
    for line in difflib.unified_diff(old_lines, new.splitlines(True),
                                     '/dev/null' if old is None else 'a/' + rel, 'b/' + rel):
        if not line.endswith('\n'):
            line += '\n\\ No newline at end of file\n'
        out.append(line)
    return ''.join(out)


def _digest(diff_text: str, targets: list) -> str:
    """sha256 of the diff text and the current content of every target, in a fixed order."""
    h = hashlib.sha256()
    h.update(diff_text.encode('utf-8'))
    for key, content in sorted(targets, key=lambda item: item[0]):
        h.update(b'\0' + key.encode('utf-8') + b'\0')
        h.update(b'<missing>' if content is None else content)
    return h.hexdigest()


def _plan_changes(plan, changes: list):
    """`(diff text, targets, edits)` for the changes against the plan as it is now.

    `targets` is `[(key, current bytes or None)]`, the inputs of the hash: a prompt or approach
    file, or `tasks.tsv#<task>` (that task's row) for a files change. `edits` is what apply
    writes: `[(task, field, new text)]`. A field that is null or equal to what is there is not an
    edit. Only the first change of a task counts; a duplicate is refused before this matters.
    """
    diffs, targets, edits, new_files = [], [], [], {}
    seen = set()
    for change in changes:
        task = change['task']
        if task in seen or not forge_side._safe_component(task):
            continue
        seen.add(task)
        for field in ('prompt', 'approach'):
            new = change.get(field)
            if not isinstance(new, str):
                continue
            current = _read_bytes(_task_file(plan, task, field + '.md'))
            if current is not None and _text(current) == new:
                continue
            targets.append((_rel(task, field), current))
            diffs.append(_udiff(_rel(task, field), None if current is None else _text(current), new))
            edits.append((task, field, new))
        files = change.get('files')
        if isinstance(files, str):
            row, old = _row_files(plan, task)
            if row is not None and old != files:
                targets.append(('tasks.tsv#' + task, row.encode('utf-8')))
                edits.append((task, 'files', files))
                new_files[task] = files
    if new_files:
        old_lines = _tsv_lines(plan)
        diffs.append(_udiff('tasks.tsv', ''.join(old_lines), ''.join(_with_files(old_lines, new_files))))
    return ''.join(diffs), targets, edits


# --- the prompt and the reply ----------------------------------------------------------

def _capped(plan, task, name) -> str:
    text = _text(_read_bytes(_task_file(plan, task, name))).strip()
    if len(text) <= FILE_CAP:
        return text
    return forge_side.cap(text, FILE_CAP) + (
        '\n[truncated to 6 KB: this is not the whole file. The full file is %s. '
        'Leave this field null unless you can restate the whole file.]' % _task_file(plan, task, name))


def build_prompt(plan, group, asking: str, question: dict, answer: dict, repo: str) -> str:
    plan = Path(plan)
    by_id = {item['id']: item for item in forge_brief.read_tasks(plan)}
    done = [member for member in group.members if started(plan, member)]
    parts = [
        'You are revising the tasks of ONE group in a multi-task coding run, because a person '
        'answered an open question that affects several of them.\n'
        'The repository is at %s: you may read it and you must not write to it.\n' % repo,
        '## The question (asked for task %s)\n\n%s\n\nwhy: %s\noptions: %s\ndefault: %s\n' % (
            asking, question.get('text', ''), question.get('why', ''),
            ' | '.join(question.get('options') or []) or '(none)', question.get('default', '')),
        '## The answer\n\n%s\n' % answer.get('text', ''),
        '## The group: %s\n\ntitle: %s\ngoal: %s\n' % (
            group.id, group.title, group.goal if group.goal != '-' else '(none)'),
        '## Which members have started\n\n%s\n' % (
            ', '.join(done) + ' (you may not change them)' if done else 'none'),
        '## The members (each file capped at 6 KB)\n',
    ]
    for member in group.members:
        item = by_id.get(member)
        parts.append('### %s\n\ntasks.tsv row (id, deps, difficulty, files, dwarf, qa, title):\n%s\n\n'
                     '#### prompt.md\n\n%s\n\n#### approach.md\n\n%s\n' % (
                         member, item['row'] if item else '(not in tasks.tsv)',
                         _capped(plan, member, 'prompt.md'), _capped(plan, member, 'approach.md') or '(none)'))
    parts.append(
        '## What to change\n\n'
        'Decide what the answer changes in the members above. Change only members that have not '
        'started. For each one you change, give the FULL new text of any file you change, never a '
        'patch. Leave a field null when it stays as it is. The only fields you may change are '
        'prompt (prompt.md), approach (approach.md) and files (the comma-separated files column '
        'of tasks.tsv). Do not touch any other task, any started member, deps or any other column. '
        'If the answer changes nothing, reply with an empty list of changes.\n\n'
        '## Reply format\n\n'
        'Reply with JSON only, no prose and no code fence:\n\n'
        '{"summary": "...", "changes": [{"task": "id", "prompt": "<full new prompt.md>"|null, '
        '"approach": "<full new approach.md>"|null, "files": "<new files column>"|null}]}\n')
    return '\n'.join(parts)


def _reply_object(reply):
    """`(object, None)` for a reply holding one JSON object (a fence or prose around it is dropped), else `(None, reason)`."""
    if not isinstance(reply, str) or not reply.strip():
        return None, 'empty reply'
    text = forge_brief._strip_fence(reply)
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
    if not isinstance(data, dict):
        return None, 'reply is not a JSON object'
    return data, None


def _edit_list(items, where='change'):
    """`(edits, None)` or `(None, reason)`: each edit a dict with a string `task`, and `prompt`,
    `approach` and `files` each a string or null (a missing one is null). A field that is not one
    of those three is kept as it came: confinement refuses it, the parser does not drop it."""
    found = []
    for index, item in enumerate(items, 1):
        label = '%s %d' % (where, index)
        if not isinstance(item, dict):
            return None, '%s is not an object' % label
        if not isinstance(item.get('task'), str) or not item['task'].strip():
            return None, '%s has no "task" string' % label
        change = dict(item, task=item['task'].strip())
        for field in CHANGE_FIELDS:
            value = change.setdefault(field, None)
            if value is not None and not isinstance(value, str):
                return None, '%s: %s must be a string or null' % (label, field)
        found.append(change)
    return found, None


def _storable(entry: dict):
    try:   # a lone surrogate parses but cannot be stored as UTF-8
        forge_brief._dump(entry).encode('utf-8')
    except UnicodeError:
        return 'reply has characters that cannot be stored'
    return None


def parse_reply(reply):
    """`(data, None)` for a usable reply, else `(None, reason)`. `data` is `{summary, changes}`."""
    data, reason = _reply_object(reply)
    if data is None:
        return None, reason
    summary, changes = data.get('summary'), data.get('changes')
    if not isinstance(summary, str):
        return None, 'reply has no "summary" string'
    if not isinstance(changes, list):
        return None, 'reply has no "changes" list'
    found, reason = _edit_list(changes)
    if found is None:
        return None, reason
    entry = {'summary': ' '.join(summary.split()), 'changes': found}
    reason = _storable(entry)
    return (None, reason) if reason else (entry, None)


def parse_plan_reply(reply):
    """`(data, None)` for a usable plan-scope reply, else `(None, reason)`.

    `data` is `{summary, changes: {edits, add, remove, deps}}`. `changes` is that object, or a list
    (the edits, as in wave scope) with `add`, `remove` and `deps` beside it; either spelling of those
    three keys is read. Only the shape is checked here (lists of objects, of strings, with a string
    `id` or `task`); whether the structure is a plan is `_plan_check`'s decision.
    """
    data, reason = _reply_object(reply)
    if data is None:
        return None, reason
    summary, raw = data.get('summary'), data.get('changes')
    if not isinstance(summary, str):
        return None, 'reply has no "summary" string'
    if isinstance(raw, list):
        raw = {'edits': raw}
    elif raw is None and any(key in data for key in ('add', 'remove', 'deps')):
        raw = {}
    if not isinstance(raw, dict):
        return None, 'reply has no "changes" object or list'
    unknown = sorted(set(raw) - set(PLAN_KEYS))
    if unknown:
        return None, '"changes" has unknown key(s): %s' % ', '.join(unknown)
    pick = lambda key: raw.get(key) if raw.get(key) is not None else data.get(key)   # noqa: E731
    edits, add, remove, deps = pick('edits') or [], pick('add') or [], pick('remove') or [], pick('deps') or []
    for name, value in (('edits', edits), ('add', add), ('remove', remove), ('deps', deps)):
        if not isinstance(value, list):
            return None, '"%s" is not a list' % name
    edits, reason = _edit_list(edits)
    if edits is None:
        return None, reason
    for index, item in enumerate(add, 1):
        if not isinstance(item, dict):
            return None, 'add %d is not an object' % index
    if not all(isinstance(item, str) and item.strip() for item in remove):
        return None, '"remove" must be a list of task ids'
    for index, item in enumerate(deps, 1):
        if not isinstance(item, dict) or not isinstance(item.get('task'), str) or not item['task'].strip():
            return None, 'deps %d has no "task" string' % index
    entry = {'summary': ' '.join(summary.split()),
             'changes': {'edits': edits, 'add': add, 'remove': [item.strip() for item in remove],
                         'deps': [dict(item, task=item['task'].strip()) for item in deps]}}
    reason = _storable(entry)
    return (None, reason) if reason else (entry, None)


def _ask(plan, task, prompt, spec, timeout, parse=None):
    """One dispatch, a retry on an unusable reply. `(data, None)` or `(None, why)`."""
    reminder, error = '', 'no reply'
    for attempt in range(2):
        result = forge_side.run('brief-change', plan, task, prompt + reminder, spec, timeout=timeout)
        if not result['ok']:
            if result['reason'] == 'cap':
                return None, 'the brief-change dispatch cap is reached' + (' (no retry)' if attempt else '')
            return None, 'dispatch failed: %s' % (result['reason'] or 'error')
        try:
            data, error = (parse or parse_reply)(result['last'])
        except Exception as failure:   # noqa: BLE001 - a broken reply is an unusable reply
            data, error = None, 'reply could not be read (%s)' % type(failure).__name__
        if data is not None:
            return data, None
        reminder = '\n\n' + REMINDER % error
    return None, error


# --- confinement -----------------------------------------------------------------------

def _confine_edits(plan, changes: list, where) -> list:
    """The reasons these per-task edits must be refused. `where(task)` is None when the task may be
    changed, else why not (a started task is always refused here)."""
    reasons, seen = [], set()
    for change in changes:
        task = change['task']
        extra = sorted(key for key in change if key != 'task' and key not in CHANGE_FIELDS)
        if task in seen:
            reasons.append('task %s is changed twice' % task)
        seen.add(task)
        why = where(task)
        if why:
            reasons.append(why)
        elif started(plan, task):
            reasons.append('task %s has started' % task)
        if extra:
            reasons.append('change for %s has field(s) other than prompt, approach and files: %s'
                           % (task, ', '.join(extra)))
        for field in ('prompt', 'approach'):
            if isinstance(change.get(field), str) and not change[field].strip():
                reasons.append('%s for %s is empty' % (field, task))
        files = change.get('files')
        if isinstance(files, str) and (not files.strip() or any(ch in files for ch in '\t\r\n')):
            reasons.append('files for %s must be one non-empty line without tabs' % task)
    return reasons


def _confine(plan, group, changes: list) -> list:
    """The reasons this proposal must be refused, in change order. Empty when it is confined."""
    return _confine_edits(plan, changes, lambda task: None if task in group.members
                          else 'task %s is not a member of group %s' % (task, group.id))


def _normalize(changes: list) -> list:
    """Each change with its three fields; a prompt or approach ends with a newline, files is stripped."""
    out = []
    for change in changes:
        entry = dict(change)
        for field in ('prompt', 'approach'):
            value = entry.get(field)
            if isinstance(value, str) and value and not value.endswith('\n'):
                entry[field] = value + '\n'
        if isinstance(entry.get('files'), str):
            entry['files'] = entry['files'].strip()
        out.append(entry)
    return out


# --- storage ---------------------------------------------------------------------------

def _proposals(plan) -> Path:
    return Path(plan) / 'proposals'


@contextlib.contextmanager
def _proposals_lock(plan):
    base = _proposals(plan)
    base.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(base / '.lock'), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def _control_lock(plan):
    """The plan's control lock, as `forge control` takes it; a busy lock is a refusal."""
    control = answers._control()
    try:
        with control.control_lock(os.path.realpath(str(plan))):
            yield control
    except (control.Refused, control.Invalid) as error:
        raise Refused(str(error))


def _numbers(plan) -> list:
    try:
        names = [entry.name for entry in _proposals(plan).iterdir() if entry.is_dir()]
    except OSError:
        return []
    return sorted(int(name[1:]) for name in names if PID_RE.fullmatch(name))


def _load(plan, pid) -> dict:
    if not isinstance(pid, str) or not PID_RE.fullmatch(pid):
        raise NoSuchProposal(pid, ['p%d' % n for n in _numbers(plan)])
    data = forge_brief._json(_proposals(plan) / pid / 'proposal.json')
    if not isinstance(data, dict):
        raise NoSuchProposal(pid, ['p%d' % n for n in _numbers(plan)])
    return data


def _store(plan, record: dict) -> None:
    forge_brief.atomic_write(_proposals(plan) / record['pid'] / 'proposal.json', forge_brief._dump(record))


def _stored_hash(plan, pid) -> str:
    return _text(_read_bytes(_proposals(plan) / pid / 'hash')).strip()


def _emit(plan, record: dict, extra: dict | None = None) -> None:
    """One `brief_diff` dataset event. Fail-open, like every other capture.

    `extra` is how an approved apply adds `affected` and `flagged`. Other states omit them,
    so a proposed or refused event stays the shape it had.
    """
    body = {
        'pid': record['pid'], 'scope': record['scope'], 'group': record['group'],
        'state': record['state'], 'tasks': touched(record)}
    if extra:
        body.update(extra)
    try:
        from forge_dataset.capture import point
        point('brief_diff', str(plan), str(record['task']), body)
    except Exception:
        pass


def _generated_at(plan, task):
    data = forge_brief._json(forge_brief.task_dir(plan, task) / 'brief.parked.json')
    try:
        return calendar.timegm(time.strptime(data['generated_at'], '%Y-%m-%dT%H:%M:%SZ'))
    except (TypeError, KeyError, ValueError):
        return None


# --- propose ---------------------------------------------------------------------------

def propose(plan, task, qid, actor='cli', timeout=900) -> dict:
    """Ask the brief model what the answer to a parked wave or plan question changes, and record the proposal.

    Returns the proposal record (`state` is `proposed` or `refused`; a refused one carries its
    `reason` and still has its diff). Raises `NotAPlan`, `BriefOff` (brief off, or `allowed`
    refuses), `Usage` (no answer yet), `NoSuchTask`, `NoSuchQuestion` and `Refused` (the question
    is not a wave question of a group or a plan question, or the writer failed). Nothing in the plan changes.
    """
    who = answers._actor(actor)
    plan, found = forge_brief._guard(plan)
    tasks = forge_brief.read_tasks(plan)
    if task not in {item['id'] for item in tasks}:
        raise answers.NoSuchTask(task, [item['id'] for item in tasks])
    parked = answers.parked_questions(plan, task)
    if qid not in parked:
        raise answers.NoSuchQuestion(task, qid, list(parked))
    question = parked[qid]
    scope = question.get('scope')
    if scope not in ('wave', 'plan'):
        raise Refused('%s/%s is a %s-scoped question: only a parked wave or plan question can be proposed'
                      % (task, qid, scope))
    gid, group = None, None
    if scope == 'plan':
        if qid not in answers.answerable_parked(plan, task):
            raise Refused('%s/%s is a plan question: the plan needs --brief-scope plan to act on it' % (task, qid))
    else:
        gid = question.get('group')
        if not isinstance(gid, str) or not gid:
            raise Refused('%s/%s names no group, so there is nothing for a wave answer to act on' % (task, qid))
        group = _group_of(plan, gid)
        if group is None:
            raise Refused('group %s is not in groups.tsv' % gid)
    answer = answers._load_map(forge_brief.task_dir(plan, task) / 'brief.parked.answers.json').get(qid)
    if not isinstance(answer, dict) or not isinstance(answer.get('text'), str) or not answer['text'].strip():
        raise answers.Usage('%s/%s has no answer yet: forge brief answer %s %s %s TEXT' % (
            task, qid, shlex.quote(str(plan)), task, qid))
    made, written = answer.get('t'), _generated_at(plan, task)
    if isinstance(made, (int, float)) and made > 0 and written is not None and made < written:
        raise Refused('%s/%s was answered before its question was last written: answer it again' % (task, qid))
    if scope == 'plan':
        return _propose_plan(plan, task, qid, question, answer, who, found, timeout)
    ok, reason = forge_side.allowed('brief-change', plan)
    if not ok:
        raise forge_brief.BriefOff(reason)
    repo = forge_brief._text(plan / 'repo').strip()
    prompt = build_prompt(plan, group, task, question, answer, repo)
    data, error = _ask(plan, task, prompt, found['model'], timeout)
    if data is None:
        raise Refused('the change writer failed: %s' % error)
    changes = _normalize(data['changes'])
    reasons = _confine(plan, group, changes)
    diff_text, targets, _ = _plan_changes(plan, changes)
    if not reasons and not diff_text:
        reasons.append('the proposal changes nothing')
    state = 'refused' if reasons else 'proposed'
    with _proposals_lock(plan):
        number = (_numbers(plan) or [0])[-1] + 1
        pid = 'p%d' % number
        directory = _proposals(plan) / pid
        directory.mkdir(parents=True)
        record = {'pid': pid, 'scope': 'wave', 'group': gid, 'task': task, 'qid': qid,
                  'answer': answer['text'], 'summary': data['summary'], 'changes': changes,
                  'state': state, 'reason': '; '.join(reasons) or None,
                  'created': forge_brief._now(), 'actor': who}
        forge_brief.atomic_write(directory / 'diff.patch', diff_text)
        forge_brief.atomic_write(directory / 'hash', _digest(diff_text, targets) + '\n')
        _store(plan, record)
    _emit(plan, record)
    return record


# --- who an approved proposal leaves affected ------------------------------------------

def _touch_sets(record: dict) -> tuple:
    """`(changed, added, removed)` task ids the proposal names.

    A wave proposal only changes tasks. A plan proposal's edits and dep rewrites are changes,
    its adds are added, and its removes are seeds for the dependency walk (the task itself is
    gone, so it is not marked).
    """
    def clean(items):
        return {item for item in items if isinstance(item, str) and item}

    if record.get('scope') == 'plan':
        changes = plan_changes_of(record)
        changed = clean(item.get('task') for item in changes['edits'] if isinstance(item, dict)) | clean(
            item.get('task') for item in changes['deps'] if isinstance(item, dict))
        added = clean(item.get('id') for item in changes['add'] if isinstance(item, dict))
        return changed, added, clean(changes['remove'])
    changes = record.get('changes') if isinstance(record.get('changes'), list) else []
    return clean(item.get('task') for item in changes if isinstance(item, dict)), set(), set()


def _dependents(tasks: list, seeds: set) -> set:
    """Task ids that depend, directly or transitively, on `seeds`, from the post-apply rows.

    A removed id is a seed even though it is no longer a row: a task whose deps still name it
    is reached from here. Walking does not stop at a started task, so an unstarted task behind
    a merged one is still found.
    """
    children = {}
    for item in tasks:
        for dep in item['deps']:
            children.setdefault(dep, []).append(item['id'])
    seen, stack = set(), list(seeds)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(children.get(node, ()))
    return {task for task in seen if task not in seeds}


def _classify(plan, record: dict) -> tuple:
    """`(reasons, affected, flagged)` from the post-apply tasks.tsv and the removed ids.

    `reasons` maps a still-present task to `changed`, `added` or `dependent` (a task the
    proposal itself changed or added keeps that reason when it also sits downstream).
    `affected` is the unstarted ids, `flagged` the ones that have already started, both in
    tasks.tsv order.
    """
    changed, added, removed = _touch_sets(record)
    tasks = forge_brief.read_tasks(plan)
    downstream = _dependents(tasks, changed | added | removed)
    reasons = {}
    for item in tasks:
        task = item['id']
        if task in removed:
            continue
        if task in changed:
            reasons[task] = 'changed'
        elif task in added:
            reasons[task] = 'added'
        elif task in downstream:
            reasons[task] = 'dependent'
    affected, flagged = [], []
    for item in tasks:
        task = item['id']
        if task not in reasons:
            continue
        if started(plan, task):
            flagged.append(task)
        else:
            affected.append(task)
    return reasons, affected, flagged


def _write_marker(plan, task: str, pid: str, reason: str, flag_only: bool, stamp: str) -> None:
    """Replace `tasks/<id>/brief.affected`. The next approved proposal overwrites it again."""
    body = {'pid': pid, 'reason': reason, 't': stamp}
    if flag_only:
        body['flag_only'] = True
    forge_brief.atomic_write(forge_brief.task_dir(plan, task) / 'brief.affected', forge_brief._dump(body))


def _carry_answers(old_questions, old_answers, new_questions) -> tuple:
    """`(kept, dropped)`: match answers to new questions by question text, first unused wins.

    `kept` is `{new qid: answer}`. `dropped` is `{old qid: answer}` for every answer whose
    question text is not in the new brief. An empty text never matches, so it is dropped
    rather than glued to another blank question.
    """
    texts = {}
    for question in old_questions:
        qid, text = question.get('id'), question.get('text')
        if isinstance(qid, str) and qid and isinstance(text, str):
            texts.setdefault(qid, text)
    pool = [{'qid': qid, 'text': texts.get(qid, ''), 'answer': answer, 'used': False}
            for qid, answer in old_answers.items()]
    kept = {}
    for question in new_questions:
        qid, text = question.get('id'), question.get('text')
        if not isinstance(qid, str) or not qid or not isinstance(text, str) or not text:
            continue
        for item in pool:
            if not item['used'] and item['text'] and item['text'] == text:
                kept[qid] = item['answer']
                item['used'] = True
                break
    dropped = {item['qid']: item['answer'] for item in pool if not item['used']}
    return kept, dropped


def _regenerate_one(plan, task: str) -> None:
    """Rewrite one task's questions and put the answers that still fit back by their text.

    `write --force` archives the old brief and its answers (question ids restart at q1).
    Answers whose question text is in the new brief are written back under the new id; the
    others go to `brief.answers.prev.json`, next to the archive, so nothing is thrown away.
    A skipped write (the dispatch cap, or a brief that appeared under the lock) leaves the
    answers where they were.
    """
    tdir = forge_brief.task_dir(plan, task)
    old_brief = forge_brief._json(tdir / 'brief.json')
    old_questions = forge_brief._questions(old_brief if isinstance(old_brief, dict) else None)
    old_answers = forge_brief._entries(forge_brief._json(tdir / 'brief.answers.json'))
    report = forge_brief.write(plan, task=task, force=True)
    if not report or report[0].get('status') == 'skipped':
        return
    new_brief = forge_brief._json(tdir / 'brief.json')
    new_questions = forge_brief._questions(new_brief if isinstance(new_brief, dict) else None)
    kept, dropped = _carry_answers(old_questions, old_answers, new_questions)
    if kept:
        forge_brief.atomic_write(tdir / 'brief.answers.json', forge_brief._dump(kept))
    if dropped:
        forge_brief.atomic_write(tdir / 'brief.answers.prev.json', forge_brief._dump(dropped))


def _regenerate(plan, tasks: list) -> None:
    """One brief dispatch per unstarted affected task, in tasks.tsv order.

    This runs after the apply has been stored. A failure here does not undo the apply:
    `BriefOff` stops the rest (every later task would fail the same way); any other error
    is left to the caller only when the writer itself raises, which it does not for a bad reply.
    """
    for task in tasks:
        try:
            _regenerate_one(plan, task)
        except forge_brief.BriefOff:
            return


def _settle(plan, record: dict, result: dict, regenerate: bool) -> dict:
    """Mark affected tasks, emit the approved event, then regenerate the unstarted ones.

    Markers are written before the event and before any dispatch, so a writer failure still
    leaves `brief.affected` and the event. Regeneration holds no plan lock: each `write` takes
    only that task's brief lock. `regenerate` False writes the same markers and dispatches nothing.
    """
    reasons, affected, flagged = _classify(plan, record)
    stamp = forge_brief._now()
    for task in affected:
        _write_marker(plan, task, record['pid'], reasons[task], False, stamp)
    for task in flagged:
        _write_marker(plan, task, record['pid'], reasons[task], True, stamp)
    _emit(plan, record, {'affected': list(affected), 'flagged': list(flagged)})
    notes = list(result.get('notes') or [])
    if affected:
        if regenerate:
            _regenerate(plan, affected)
            notes.append('regenerating questions for %d tasks: %s' % (len(affected), ', '.join(affected)))
        else:
            notes.append('marked affected, not regenerating: %s' % ', '.join(affected))
    if flagged:
        notes.append('flagged (already started): %s' % ', '.join(flagged))
    result['notes'] = notes
    result['affected'] = affected
    result['flagged'] = flagged
    return result


# --- apply -----------------------------------------------------------------------------

def _validate_new(plan, new_text: str, max_tasks: int, groups_text=None) -> None:
    """Run the group validation against the tasks.tsv (and groups.tsv) an apply would write, touching nothing real."""
    with tempfile.TemporaryDirectory(prefix='forge-validate-') as scratch:
        scratch = Path(scratch)
        (scratch / 'tasks.tsv').write_text(new_text, encoding='utf-8')
        groups_file = Path(plan) / 'groups.tsv'
        (scratch / 'groups.tsv').write_bytes(
            groups_text.encode('utf-8') if groups_text is not None else _read_bytes(groups_file) or b'')
        os.symlink(str(Path(plan) / 'tasks'), str(scratch / 'tasks'))   # sweep.json is read from it
        forge_groups.validate(scratch, forge_groups.load(scratch), max_tasks)


def _max_tasks(plan) -> int:
    try:
        return int(forge_brief._text(Path(plan) / 'group_max_tasks').split()[0])
    except (IndexError, ValueError):
        return 6


def _recompute_waves(plan) -> str | None:
    """`forge-parallel.sh _waves`, the way `plan` computes waves. A failure message, or None."""
    if not (Path(plan) / 'waves.tsv').exists():
        return None   # an unplanned plan gains nothing, and waves.tsv is what marks one as planned
    script = Path(forge_brief.SCRIPTS) / 'forge-parallel.sh'
    try:
        done = subprocess.run(['/bin/bash', str(script), '_waves', str(plan)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as error:
        return 'waves.tsv was not refreshed: %s' % error
    return None if done.returncode == 0 else 'waves.tsv was not refreshed: %s' % (done.stderr or done.stdout).strip()


def apply(plan, pid, approve_hash, actor='cli', regenerate=True) -> dict:
    """Write an approved proposal. Raises `Refused` (nothing changed) or `NoSuchProposal`.

    Returns `{pid, state: "approved", files, tasks, warning, affected, flagged, notes}`.
    `files` are the plan files written, relative to the plan; `warning` says if waves.tsv
    could not be refreshed. `affected` and `flagged` are the task ids marked after the write
    (see `_settle`). `regenerate` asks the brief writer for new questions of the unstarted
    affected tasks; `False` (`apply --no-regenerate`) only writes their markers.
    """
    who = answers._actor(actor)
    plan = Path(plan)
    if not plan.is_dir() or not (plan / 'tasks.tsv').is_file():
        raise forge_brief.NotAPlan(str(plan))
    if _load(plan, pid).get('scope') == 'plan':   # the scope never changes, so reading it unlocked is safe
        return _apply_plan(plan, pid, approve_hash, who, regenerate)
    with _proposals_lock(plan), _control_lock(plan) as control:
        record = _load(plan, pid)
        if record.get('state') != 'proposed':
            raise Refused('proposal %s is %s, not proposed' % (pid, record.get('state')))
        stored = _stored_hash(plan, pid)
        if not stored or str(approve_hash).strip().lower() != stored:
            raise Refused('the hash does not match proposal %s: read its diff again and quote its hash' % pid)
        changes = record.get('changes') if isinstance(record.get('changes'), list) else []
        group = _group_of(plan, record.get('group'))
        if group is None:
            raise Refused('group %s is no longer in groups.tsv' % record.get('group'))
        reasons = _confine(plan, group, changes)
        if reasons:
            raise Refused('proposal %s no longer fits its group: %s' % (pid, '; '.join(reasons)))
        diff_text = _text(_read_bytes(_proposals(plan) / pid / 'diff.patch'))
        current_diff, targets, edits = _plan_changes(plan, changes)
        if _digest(diff_text, targets) != stored:
            raise Refused('a target file changed since proposal %s: propose again' % pid)
        files_edits = {task: value for task, field, value in edits if field == 'files'}
        if files_edits and control.is_live(os.path.realpath(str(plan))):
            raise Refused('a run is live and its scheduler keeps the files column it read at start: '
                          'apply the files change after the run')
        old_lines = _tsv_lines(plan)
        new_lines = _with_files(old_lines, files_edits) if files_edits else old_lines
        if files_edits:
            try:
                _validate_new(plan, ''.join(new_lines), _max_tasks(plan))
            except forge_groups.GroupError as error:
                raise Refused('group validation fails after the files change: %s' % ' '.join(str(error).split()))
        written = [(plan / ('tasks/%s/%s.md' % (task, field)), value)
                   for task, field, value in edits if field != 'files']
        if files_edits:
            written.append((plan / 'tasks.tsv', ''.join(new_lines)))
        before = {path: _read_bytes(path) for path, _ in written}
        before_frozen = _read_bytes(plan / 'groups.frozen')
        try:
            for path, value in written:
                forge_brief.atomic_write(path, value)
            if files_edits and not plan_stale(plan):   # a plan change awaiting `plan` stays stale
                forge_groups.freeze(plan)
        except BaseException:
            for path, old in before.items():
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    forge_brief.atomic_write(path, _text(old))
            if before_frozen is None:
                (plan / 'groups.frozen').unlink(missing_ok=True)
            else:
                forge_brief.atomic_write(plan / 'groups.frozen', _text(before_frozen))
            raise
        warning = _recompute_waves(plan) if files_edits else None
        record['state'] = 'approved'
        record['applied'] = forge_brief._now()
        record['applied_by'] = who
        _store(plan, record)
    names = [str(path.relative_to(plan)) for path, _ in written] + (['groups.frozen'] if files_edits else [])
    result = {'pid': pid, 'state': 'approved', 'files': names,
              'tasks': [change['task'] for change in changes], 'warning': warning,
              'message': None, 'notes': [], 'live': False}
    return _settle(plan, record, result, regenerate)


# --- plan scope: structural changes ----------------------------------------------------

PLAN_MESSAGE = 'structural changes apply after the run ends (forge control stop, or wait), then plan again'


def plan_changes_of(record) -> dict:
    """A stored plan-scope proposal's `changes` as `{edits, add, remove, deps}`, whatever was stored."""
    raw = record.get('changes') if isinstance(record, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    return {key: list(raw.get(key) or []) if isinstance(raw.get(key) or [], list) else [] for key in PLAN_KEYS}


def touched(record) -> list:
    """The task ids a proposal names, in a stable order, for the `brief_diff` event."""
    if record.get('scope') != 'plan':
        return [change['task'] for change in record.get('changes') or []]
    changes = plan_changes_of(record)
    found = [item.get('task') for item in changes['edits']] + [item.get('id') for item in changes['add']] \
        + list(changes['remove']) + [item.get('task') for item in changes['deps']]
    return list(dict.fromkeys(item for item in found if isinstance(item, str)))


def _id_problem(name):
    """Why `name` is not an id a new task may have, or None. The scheduler's rule (`_task_id_ok`), and no
    whitespace, comma or backslash (they break tasks.tsv and deps), no leading `.` or `-`."""
    if not isinstance(name, str) or not name:
        return 'the id must be a non-empty string'
    if name in ('.', '..') or '/' in name or '\0' in name:
        return 'the id %r is not a valid task id' % name
    if any(ch.isspace() or ch in ',\\' for ch in name) or name[0] in '.-':
        return 'the id %r may not contain whitespace, commas or backslashes, nor start with . or -' % name
    return None


def _normalize_plan(changes: dict) -> dict:
    """Each added task cleaned: prompt and approach end with a newline, files a list, text fields stripped."""
    out = {'edits': _normalize(changes['edits']), 'remove': [str(item).strip() for item in changes['remove']],
           'deps': [], 'add': []}
    for item in changes['deps']:
        entry = dict(item)
        if isinstance(entry.get('deps'), list):
            entry['deps'] = [str(dep).strip() if isinstance(dep, str) else dep for dep in entry['deps']]
        out['deps'].append(entry)
    for item in changes['add']:
        entry = dict(item)
        for field in ('id', 'difficulty', 'group'):
            if isinstance(entry.get(field), str):
                entry[field] = entry[field].strip()
        if isinstance(entry.get('title'), str):
            entry['title'] = ' '.join(entry['title'].split())
        if isinstance(entry.get('files'), str):
            entry['files'] = [part for part in entry['files'].split(',')]
        if isinstance(entry.get('files'), list):
            entry['files'] = [part.strip() if isinstance(part, str) else part for part in entry['files']]
        if isinstance(entry.get('deps'), list):
            entry['deps'] = [dep.strip() if isinstance(dep, str) else dep for dep in entry['deps']]
        for field in ('prompt', 'approach'):
            value = entry.get(field)
            if isinstance(value, str) and value and not value.endswith('\n'):
                entry[field] = value + '\n'
        out['add'].append(entry)
    return out


def _check_added(plan, entry, taken, group_ids, reasons):
    """Append to `reasons` what is wrong with one `add` entry; True when it is sound enough to build a row from."""
    name = entry.get('id')
    label = 'add %s' % (name if isinstance(name, str) and name else '(no id)')
    before = len(reasons)
    extra = sorted(key for key in entry if key not in ADD_KEYS)
    if extra:
        reasons.append('%s: unknown field(s) %s' % (label, ', '.join(extra)))
    problem = _id_problem(name)
    if problem:
        reasons.append('%s: %s' % (label, problem))
    elif name in taken:
        reasons.append('%s: the id is already a task (an added id must be new)' % label)
    elif name in group_ids:
        reasons.append('%s: the id is also a group id' % label)
    elif (Path(plan) / 'tasks' / name).exists():
        reasons.append('%s: tasks/%s already exists (an added id must be new)' % (label, name))
    if entry.get('difficulty') not in DIFFICULTIES:
        reasons.append('%s: difficulty must be low, medium or high' % label)
    files = entry.get('files')
    if (not isinstance(files, list) or not files or not all(isinstance(part, str) and part for part in files)
            or any(ch in part for part in files if isinstance(part, str) for ch in ',\t\r\n')):
        reasons.append('%s: files must be a non-empty list of paths without tabs or commas' % label)
    title = entry.get('title')
    if not isinstance(title, str) or not title or '\t' in title:
        reasons.append('%s: title must be one non-empty line without tabs' % label)
    prompt = entry.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip():
        reasons.append('%s: prompt is empty' % label)
    elif not ACCEPTANCE_RE.search(prompt):
        reasons.append('%s: prompt has no "## Acceptance criteria" section (the plan rule: references/decompose.md)' % label)
    approach = entry.get('approach')
    if approach is not None and (not isinstance(approach, str) or not approach.strip()):
        reasons.append('%s: approach must be a non-empty string or null' % label)
    group = entry.get('group')
    if group is not None and group not in group_ids:
        reasons.append('%s: group %s is not in groups.tsv' % (label, group))
    deps = entry.get('deps')
    if not isinstance(deps, list) or not all(isinstance(dep, str) and dep for dep in deps):
        reasons.append('%s: deps must be a list of task ids' % label)
    return len(reasons) == before


def _rewrite_rows(lines: list, drop=(), files=None, deps=None) -> list:
    """The tasks.tsv lines without the dropped rows and with the files or deps column of the named rows replaced."""
    files, deps, out = files or {}, deps or {}, []
    for line in lines:
        body = line.rstrip('\n')
        cols = body.split('\t')
        if body.strip() and not body.startswith('#') and len(cols) >= 7:
            if cols[0] in drop:
                continue
            if cols[0] in files or cols[0] in deps:
                if cols[0] in files:
                    cols[3] = files[cols[0]]
                if cols[0] in deps:
                    cols[1] = ','.join(deps[cols[0]]) or '-'
                line = '\t'.join(cols) + ('\n' if line.endswith('\n') else '')
        out.append(line)
    return out


def _rewrite_groups(lines: list, drop: set, join: dict) -> tuple:
    """`(new lines, reasons)`: each group row without the dropped members and with the joining ones appended."""
    out, reasons = [], []
    for line in lines:
        body = line.rstrip('\n')
        cols = body.split('\t')
        if body.strip() and not body.startswith('#') and len(cols) == 6:
            members = [member.strip() for member in cols[3].split(',')]
            kept = [member for member in members if member not in drop] + join.get(cols[0], [])
            if kept != members:
                if not kept:
                    reasons.append('group %s would lose every member: take it out of groups.tsv by hand first' % cols[0])
                else:
                    cols[3] = ','.join(kept)
                    line = '\t'.join(cols) + ('\n' if line.endswith('\n') else '')
        out.append(line)
    return out, reasons


def _has_cycle(graph: dict) -> list:
    """A cycle in `{task: [deps]}` as a list of ids, or []."""
    state, stack = {}, []

    def visit(node):
        state[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                return stack[stack.index(dep):] + [dep]
            if dep not in state and dep in graph:
                found = visit(dep)
                if found:
                    return found
        state[node] = 2
        stack.pop()
        return []

    for node in graph:
        if node not in state:
            found = visit(node)
            if found:
                return found
    return []


def _plan_check(plan, changes: dict) -> tuple:
    """`(reasons, built)` for plan-scope `changes` against the plan as it is now.

    `reasons` is why the proposal is refused (empty when it is a plan). `built` is what applying
    writes, computed from the sound parts even when there are reasons, so a refused proposal
    still shows its diff: `{tasks_old, tasks_new, groups_old, groups_new (None without a groups.tsv),
    edits [(task, field, text)], add [entry], remove [id], rows [new tasks.tsv rows added]}`.
    """
    plan = Path(plan)
    reasons = []
    rows = forge_brief.read_tasks(plan)
    by_id = {item['id']: item for item in rows}
    try:
        groups = forge_groups.load(plan)
    except forge_groups.GroupError as error:
        raise Refused('groups.tsv cannot be read: %s' % ' '.join(str(error).split()))
    group_ids = {group.id for group in groups}

    removed = []
    for name in changes['remove']:
        if name in removed:
            reasons.append('task %s is removed twice' % name)
        elif name not in by_id:
            reasons.append('cannot remove %s: it is not in tasks.tsv' % name)
        elif started(plan, name):
            reasons.append('cannot remove %s: it has started' % name)
        else:
            removed.append(name)

    added, taken = {}, set(by_id)
    for entry in changes['add']:
        sound = _check_added(plan, entry, taken | set(added), group_ids, reasons)
        name = entry.get('id')
        if isinstance(name, str) and name in added:
            reasons.append('add %s: the id is added twice' % name)
        elif sound:
            added[name] = entry

    new_deps = {}
    for entry in changes['deps']:
        name, deps = entry['task'], entry.get('deps')
        extra = sorted(key for key in entry if key not in ('task', 'deps'))
        if extra:
            reasons.append('deps entry for %s has unknown field(s): %s' % (name, ', '.join(extra)))
        if not isinstance(deps, list) or not all(isinstance(dep, str) and dep for dep in deps):
            reasons.append('deps for %s must be a list of task ids' % name)
        elif name in new_deps:
            reasons.append('deps of %s are changed twice' % name)
        elif name in added:
            reasons.append('deps of %s: it is added in this proposal, so its deps are in its add entry' % name)
        elif name not in by_id:
            reasons.append('cannot change the deps of %s: it is not in tasks.tsv' % name)
        elif name in removed:
            reasons.append('cannot change the deps of %s: it is removed in this proposal' % name)
        elif started(plan, name):
            reasons.append('cannot change the deps of %s: it has started' % name)
        else:
            new_deps[name] = list(dict.fromkeys(deps))

    edits_reasons = _confine_edits(plan, changes['edits'], lambda task: (
        'task %s is removed in this proposal' % task if task in removed
        else 'task %s is not in tasks.tsv' % task if task not in by_id else None))
    reasons.extend(edits_reasons)

    graph = {}
    for name, item in by_id.items():
        if name not in removed:
            graph[name] = new_deps.get(name, item['deps'])
    for name, entry in added.items():
        graph[name] = list(dict.fromkeys(entry.get('deps') or []))
    for name, deps in graph.items():
        for dep in deps:
            if dep == name:
                reasons.append('task %s depends on itself' % name)
            elif dep in removed:
                reasons.append('task %s depends on removed task %s: rewrite its deps in the same proposal' % (name, dep))
            elif dep not in graph:
                reasons.append('task %s depends on %s, which will not exist' % (name, dep))
    cycle = _has_cycle({name: [dep for dep in deps if dep in graph and dep != name] for name, deps in graph.items()})
    if cycle:
        reasons.append('the dependencies form a cycle: %s' % ' -> '.join(cycle))

    files_new, edits = {}, []
    for change in changes['edits']:
        task = change['task']
        if task not in by_id or task in removed or not forge_side._safe_component(task):
            continue
        for field in ('prompt', 'approach'):
            new = change.get(field)
            if isinstance(new, str) and new.strip():
                current = _read_bytes(_task_file(plan, task, field + '.md'))
                if current is None or _text(current) != new:
                    edits.append((task, field, new))
        files = change.get('files')
        if isinstance(files, str) and files.strip() and by_id[task]['files'] != files:
            files_new[task] = files
            edits.append((task, 'files', files))

    tasks_old = _text(_read_bytes(plan / 'tasks.tsv'))
    lines = tasks_old.splitlines(True)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    lines = _rewrite_rows(lines, drop=set(removed), files=files_new, deps=new_deps)
    for name, entry in added.items():
        lines.append('\t'.join([name, ','.join(dict.fromkeys(entry.get('deps') or [])) or '-', entry['difficulty'],
                                ','.join(entry['files']), '-', '-', entry['title']]) + '\n')
    tasks_new = ''.join(lines)

    groups_old = _read_bytes(plan / 'groups.tsv')
    groups_old = None if groups_old is None else _text(groups_old)
    groups_new = groups_old
    if groups_old is not None:
        join = {}
        for name, entry in added.items():
            if entry.get('group'):
                join.setdefault(entry['group'], []).append(name)
        glines, more = _rewrite_groups(groups_old.splitlines(True), set(removed), join)
        reasons.extend(more)
        groups_new = ''.join(glines)

    built = {'tasks_old': tasks_old, 'tasks_new': tasks_new, 'groups_old': groups_old, 'groups_new': groups_new,
             'edits': edits, 'add': list(added.values()), 'remove': removed, 'deps': new_deps}
    if not reasons:
        if not (edits or added or removed or new_deps):
            reasons.append('the proposal changes nothing')
        else:
            try:
                _validate_new(plan, tasks_new, _max_tasks(plan), groups_new)
            except forge_groups.GroupError as error:
                reasons.append('group validation fails: %s' % ' '.join(str(error).split()))
    return reasons, built


def _plan_diff(plan, built: dict) -> tuple:
    """`(diff text, targets)` of a built plan change. The hash covers tasks.tsv and groups.tsv whole."""
    diffs, targets = [], []
    if built['tasks_new'] != built['tasks_old']:
        diffs.append(_udiff('tasks.tsv', built['tasks_old'], built['tasks_new']))
    if built['groups_new'] != built['groups_old']:
        diffs.append(_udiff('groups.tsv', built['groups_old'], built['groups_new']))
    for task, field, new in built['edits']:
        if field == 'files':
            continue   # the tasks.tsv diff shows it
        current = _read_bytes(_task_file(plan, task, field + '.md'))
        diffs.append(_udiff(_rel(task, field), None if current is None else _text(current), new))
        targets.append((_rel(task, field), current))
    for entry in built['add']:
        diffs.append(_udiff(_rel(entry['id'], 'prompt'), None, entry['prompt']))
        if entry.get('approach'):
            diffs.append(_udiff(_rel(entry['id'], 'approach'), None, entry['approach']))
        targets.append(('tasks/%s' % entry['id'], None))
    targets.append(('tasks.tsv', _read_bytes(Path(plan) / 'tasks.tsv')))
    targets.append(('groups.tsv', _read_bytes(Path(plan) / 'groups.tsv')))
    return ''.join(diffs), targets


def build_plan_prompt(plan, asking: str, question: dict, answer: dict, repo: str) -> str:
    plan = Path(plan)
    rows = forge_brief.read_tasks(plan)
    groups = forge_groups.load(plan) if (plan / 'groups.tsv').is_file() else []
    parts = [
        'You are revising the PLAN of a multi-task coding run (its tasks and their dependencies), because a '
        'person answered an open question that affects the plan as a whole.\n'
        'The repository is at %s: you may read it and you must not write to it.\n' % repo,
        '## The question (asked for task %s)\n\n%s\n\nwhy: %s\noptions: %s\ndefault: %s\n' % (
            asking, question.get('text', ''), question.get('why', ''),
            ' | '.join(question.get('options') or []) or '(none)', question.get('default', '')),
        '## The answer\n\n%s\n' % answer.get('text', ''),
        '## Every task (tasks.tsv: id, deps, difficulty, files, dwarf, qa, title) and its status\n',
    ]
    for item in rows:
        parts.append('%s   [%s]' % (item['row'].rstrip('\n'), 'STARTED: you may not change it'
                                    if started(plan, item['id']) else 'unstarted'))
    parts.append('\n## groups.tsv (id, review, qa, members, title, goal)\n\n%s\n' % (
        _text(_read_bytes(plan / 'groups.tsv')).strip() if groups else '(no groups)'))
    parts.append('## The unstarted tasks (each file capped at 6 KB)\n')
    for item in rows:
        if not started(plan, item['id']):
            parts.append('### %s\n\n#### prompt.md\n\n%s\n\n#### approach.md\n\n%s\n' % (
                item['id'], _capped(plan, item['id'], 'prompt.md'), _capped(plan, item['id'], 'approach.md') or '(none)'))
    parts.append(
        '## What to change\n\n'
        'Decide what the answer changes in the plan. You may change only tasks that have not started: edit '
        'their prompt (prompt.md), approach (approach.md) or files column, remove them, or give them new '
        'deps. You may add tasks. A split is a removal plus the added tasks that replace it; every task that '
        'depended on a removed one needs its deps rewritten in the same reply. Added ids must be new. '
        'An added task\'s deps must name tasks that will exist, with no cycle; its group is null or one '
        'existing group id. Every added prompt must end with an "## Acceptance criteria" section of '
        'observable checks. Give the FULL new text of any file you change, never a patch. Leave a field '
        'null when it stays as it is. If the answer changes nothing, reply with empty lists.\n\n'
        '## Reply format\n\n'
        'Reply with JSON only, no prose and no code fence:\n\n'
        '{"summary": "...", "changes": {\n'
        '  "edits": [{"task": "id", "prompt": "<full new prompt.md>"|null, "approach": "<full new approach.md>"|null, '
        '"files": "<new files column>"|null}],\n'
        '  "add": [{"id": "new-id", "deps": ["id"], "difficulty": "low|medium|high", "files": ["path"], '
        '"title": "...", "prompt": "<full prompt.md>", "approach": "<full approach.md>"|null, "group": "group id"|null}],\n'
        '  "remove": ["id"],\n'
        '  "deps": [{"task": "id", "deps": ["id"]}]}}\n')
    return '\n'.join(parts)


def _split_word(changes: dict) -> bool:
    return bool(changes['remove']) and len(changes['add']) >= 1


def _propose_plan(plan, task, qid, question, answer, who, found, timeout) -> dict:
    ok, reason = forge_side.allowed('brief-change', plan)
    if not ok:
        raise forge_brief.BriefOff(reason)
    repo = forge_brief._text(plan / 'repo').strip()
    prompt = build_plan_prompt(plan, task, question, answer, repo)
    data, error = _ask(plan, task, prompt, found['model'], timeout, parse=parse_plan_reply)
    if data is None:
        raise Refused('the change writer failed: %s' % error)
    changes = _normalize_plan(data['changes'])
    reasons, built = _plan_check(plan, changes)
    diff_text, targets = _plan_diff(plan, built)
    summary = data['summary']
    if _split_word(changes) and 'split' not in summary.lower():
        summary = 'split: ' + summary
    state = 'refused' if reasons else 'proposed'
    with _proposals_lock(plan):
        number = (_numbers(plan) or [0])[-1] + 1
        pid = 'p%d' % number
        directory = _proposals(plan) / pid
        directory.mkdir(parents=True)
        record = {'pid': pid, 'scope': 'plan', 'group': None, 'task': task, 'qid': qid,
                  'answer': answer['text'], 'summary': summary, 'changes': changes,
                  'state': state, 'reason': '; '.join(reasons) or None,
                  'created': forge_brief._now(), 'actor': who}
        forge_brief.atomic_write(directory / 'diff.patch', diff_text)
        forge_brief.atomic_write(directory / 'hash', _digest(diff_text, targets) + '\n')
        _store(plan, record)
    _emit(plan, record)
    return record


# --- applying a plan-scope proposal ----------------------------------------------------

def _decompose_hint(plan) -> str:
    level = forge_brief._text(Path(plan) / 'decompose_level').strip()
    return ' --decompose-level %s' % level if level in DIFFICULTIES else ''


def plan_again_message(plan) -> str:
    return 'plan changed; run forge-parallel.sh plan %s …%s again before run' % (shlex.quote(str(plan)), _decompose_hint(plan))


STALE = 'stale: plan changed by proposal'


def plan_stale(plan) -> bool:
    """True when a plan-scope apply left `groups.frozen` as a stale marker: `plan` has not run again."""
    return forge_brief._text(Path(plan) / 'groups.frozen').startswith(STALE)


def _routing_for(rows: list, difficulty: str):
    """`(dwarf, qa)` an added row can take in a live run: explicit specs copied from an existing row
    (the same difficulty first), since `forge control add` refuses `-` and UNASSIGNED."""
    def explicit(item):
        return all(value not in ('', '-', 'UNASSIGNED') for value in (item['dwarf'], item['qa']))
    pool = [item for item in rows if explicit(item)]
    same = [item for item in pool if item['difficulty'] == difficulty]
    chosen = (same or pool or [None])[0]
    return (chosen['dwarf'], chosen['qa']) if chosen else None


def _write_edits(written: list) -> list:
    """Write `[(path, text)]` atomically, returning the undo list `[(path, old bytes or None)]`."""
    undo = []
    for path, value in written:
        undo.append((path, _read_bytes(path)))
        forge_brief.atomic_write(path, value)
    return undo


def _undo_files(undo: list) -> None:
    for path, old in reversed(undo):
        try:
            if old is None:
                Path(path).unlink(missing_ok=True)
            else:
                forge_brief.atomic_write(path, _text(old))
        except OSError:
            pass


def _apply_plan_offline(plan, pid, built: dict) -> list:
    """Write the new plan files, create the new task dirs, move the removed ones. Rolled back whole on a failure."""
    import shutil
    moved, created, undo = [], [], []
    try:
        for entry in built['add']:
            tdir = plan / 'tasks' / entry['id']
            tdir.mkdir(parents=True)
            created.append(tdir)
            forge_brief.atomic_write(tdir / 'prompt.md', entry['prompt'])
            forge_brief.atomic_write(tdir / 'approach.md', entry.get('approach') or '')
        for name in built['remove']:
            source = plan / 'tasks' / name
            if source.is_dir():
                target = plan / 'tasks' / '.removed' / ('%s-%s' % (name, pid))
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(str(source), str(target))
                moved.append((source, target))
        written = [(plan / ('tasks/%s/%s.md' % (task, field)), value)
                   for task, field, value in built['edits'] if field != 'files']
        written.append((plan / 'tasks.tsv', built['tasks_new']))
        if built['groups_new'] != built['groups_old']:
            written.append((plan / 'groups.tsv', built['groups_new']))
        # The frozen fingerprint covers groups.tsv and the rows of group members only, so a change to
        # an ungrouped task, or a plan with no groups, would leave `run` free to go on with rows `plan`
        # never routed. A stale marker never matches a fingerprint, and with no groups.tsv its mere
        # presence is refused by `check_frozen`: either way `run` stops until `plan` freezes again
        # (it overwrites the marker, or removes it when there are no groups).
        written.append((plan / 'groups.frozen', '%s %s\n' % (STALE, pid)))
        undo = _write_edits(written)
    except BaseException:
        _undo_files(undo)
        for source, target in reversed(moved):
            try:
                os.rename(str(target), str(source))
            except OSError:
                pass
        for tdir in created:
            shutil.rmtree(str(tdir), ignore_errors=True)
        raise
    names = [str(path.relative_to(plan)) for path, _ in written]
    names += ['tasks/%s/' % entry['id'] for entry in built['add']]
    names += [str(target.relative_to(plan)) + '/' for _, target in moved]
    return names


def _control_args(plan, task, actor):
    return argparse.Namespace(plan=str(plan), task=task, json=False, actor='ui' if actor == 'ui' else 'cli')


def _dependency_order(adds: list) -> list:
    """The added entries, each after the added entries it depends on (proposal order otherwise):
    `forge control add` refuses a row whose dependency is not in tasks.tsv yet."""
    ids = {entry['id'] for entry in adds}
    ordered, placed, pending = [], set(), list(adds)
    while pending:
        ready = [entry for entry in pending
                 if all(dep in placed or dep not in ids for dep in (entry.get('deps') or []))]
        if not ready:   # a cycle among the adds: _plan_check refuses it, never reached
            ready = pending[:1]
        ordered.append(ready[0])
        placed.add(ready[0]['id'])
        pending.remove(ready[0])
    return ordered


def _apply_plan_live(plan, pid, built: dict, control, actor, rows) -> tuple:
    """Queue each added task through `forge control add`. Returns `(names, notes)`; raises `Refused`."""
    import shutil
    adds = _dependency_order(built['add'])
    routes = {}
    for entry in adds:
        route = _routing_for(rows, entry['difficulty'])
        if route is None:
            raise Refused('a live run needs explicit dwarf and qa specs for an added task, and no task of '
                          'this plan has them yet: apply after the run ends, then plan again')
        routes[entry['id']] = route
    queued, notes = [], []
    for entry in adds:
        name, (dwarf, qa) = entry['id'], routes[entry['id']]
        tdir = plan / 'tasks' / name
        undo = []
        try:
            with control.control_lock(os.path.realpath(str(plan))):
                tdir.mkdir(parents=True)
                forge_brief.atomic_write(tdir / 'prompt.md', entry['prompt'])
                forge_brief.atomic_write(tdir / 'approach.md', entry.get('approach') or '')
                old = _text(_read_bytes(plan / 'tasks.tsv'))
                if old and not old.endswith('\n'):
                    old += '\n'
                row = '\t'.join([name, ','.join(dict.fromkeys(entry.get('deps') or [])) or '-', entry['difficulty'],
                                 ','.join(entry['files']), dwarf, qa, entry['title']]) + '\n'
                undo = _write_edits([(plan / 'tasks.tsv', old + row)])
            with contextlib.redirect_stdout(io.StringIO()):
                control.cmd_add(_control_args(plan, name, actor))
        except (control.Refused, control.Invalid, OSError) as error:
            with contextlib.suppress(Exception), control.control_lock(os.path.realpath(str(plan))):
                _undo_files(undo)
            shutil.rmtree(str(tdir), ignore_errors=True)
            done = ('; already queued: %s' % ', '.join(queued)) if queued else ''
            failure = Refused('forge control add refused %s: %s%s' % (name, error, done))
            failure.queued = queued
            raise failure
        queued.append(name)
        if entry.get('group'):
            notes.append('task %s joins no group (a live-added task joins none), whatever the proposal says' % name)
    names = ['tasks.tsv'] + ['tasks/%s/' % name for name in queued]
    return names, notes


def _apply_plan(plan, pid, approve_hash, who, regenerate=True) -> dict:
    """`apply` for a plan-scope proposal. No live run: the plan files change and `plan` must run again.
    A live run: only added tasks, each through `forge control add`; anything else refuses, nothing changed."""
    with _proposals_lock(plan):
        queued = None
        with _control_lock(plan) as control:
            record = _load(plan, pid)
            if record.get('state') != 'proposed':
                raise Refused('proposal %s is %s, not proposed' % (pid, record.get('state')))
            stored = _stored_hash(plan, pid)
            if not stored or str(approve_hash).strip().lower() != stored:
                raise Refused('the hash does not match proposal %s: read its diff again and quote its hash' % pid)
            changes = plan_changes_of(record)
            reasons, built = _plan_check(plan, changes)
            if reasons:
                raise Refused('proposal %s no longer fits the plan: %s' % (pid, '; '.join(reasons)))
            _, targets = _plan_diff(plan, built)
            if _digest(_text(_read_bytes(_proposals(plan) / pid / 'diff.patch')), targets) != stored:
                raise Refused('a target file changed since proposal %s: propose again' % pid)
            live = control.is_live(os.path.realpath(str(plan)))
            if live and (changes['edits'] or changes['remove'] or changes['deps']):
                raise Refused(PLAN_MESSAGE)
            notes = []
            if not live:
                names = _apply_plan_offline(plan, pid, built)
                message = plan_again_message(plan)
                record.update(state='approved', applied=forge_brief._now(), applied_by=who, live=False)
                _store(plan, record)
        if live:   # forge control add takes the control lock itself
            rows = forge_brief.read_tasks(plan)
            try:
                names, notes = _apply_plan_live(plan, pid, built, answers._control(), who, rows)
            except Refused as failure:
                queued = getattr(failure, 'queued', None)
                if queued:   # some rows are in the plan and queued: the proposal cannot be applied again
                    record.update(state='approved', applied=forge_brief._now(), applied_by=who, live=True,
                                  queued=queued, partial=failure.reason)
                    _store(plan, record)
                    _emit(plan, record)
                raise
            order = [entry['id'] for entry in _dependency_order(built['add'])]
            message = 'queued %s into the live run through forge control add' % ', '.join(order)
            record.update(state='approved', applied=forge_brief._now(), applied_by=who, live=True, queued=order)
            _store(plan, record)
    result = {'pid': pid, 'state': 'approved', 'files': names, 'tasks': touched(record), 'warning': None,
              'message': message, 'notes': notes, 'live': live}
    return _settle(plan, record, result, regenerate)


# --- reject and list -------------------------------------------------------------------

def reject(plan, pid, actor='cli', reason=None) -> dict:
    """State `rejected`, the event, and no plan file touched. A proposed or refused proposal can be rejected."""
    who = answers._actor(actor)
    plan = Path(plan)
    if not plan.is_dir():
        raise forge_brief.NotAPlan(str(plan))
    with _proposals_lock(plan):
        record = _load(plan, pid)
        if record.get('state') not in ('proposed', 'refused'):
            raise Refused('proposal %s is %s: only a proposed or refused one can be rejected' % (pid, record.get('state')))
        record['state'] = 'rejected'
        record['rejected'] = forge_brief._now()
        record['rejected_by'] = who
        if reason:
            record['reject_reason'] = ' '.join(str(reason).split())
        _store(plan, record)
    _emit(plan, record)
    return record


def apply_line(plan, pid, digest) -> str:
    return 'forge brief apply %s %s --approve %s' % (shlex.quote(str(plan)), pid, digest)


def listing(plan) -> list:
    """Every proposal, oldest first: its record plus `hash`, `diff` and, when it can be applied, `apply`."""
    plan = Path(plan)
    out = []
    for number in _numbers(plan):
        pid = 'p%d' % number
        record = forge_brief._json(_proposals(plan) / pid / 'proposal.json')
        if not isinstance(record, dict):
            continue
        item = dict(record, hash=_stored_hash(plan, pid),
                    diff=_text(_read_bytes(_proposals(plan) / pid / 'diff.patch')))
        if record.get('state') == 'proposed':
            item['apply'] = apply_line(plan, pid, item['hash'])
        out.append(item)
    return out


def render_one(item: dict) -> str:
    """The text `forge brief proposals` shows for one proposal."""
    lines = ['%s  %s  scope %s%s  (asked by %s/%s)' % (
        item['pid'], item['state'], item.get('scope'),
        '  group %s' % item.get('group') if item.get('group') else '', item.get('task'), item.get('qid')),
        '    %s' % (item.get('summary') or '(no summary)')]
    if item.get('reason'):
        lines.append('    refused: %s' % item['reason'] if item['state'] == 'refused' else '    %s' % item['reason'])
    if item.get('diff'):
        lines.append('')
        lines.append(item['diff'].rstrip('\n'))
        lines.append('')
    if item.get('apply'):
        lines.append('    apply: %s' % item['apply'])
    return '\n'.join(lines) + '\n'


def render(items: list) -> str:
    if not items:
        return 'no proposals\n'
    return '\n'.join(render_one(item) for item in items)
