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

`brief_diff` events (`{pid, scope, group, state, tasks}`) record proposed, refused, approved and
rejected. The dispatch is a read-only `side` dispatch of kind `brief-change`, on the plan's brief
model, under the same `allowed` checks as the brief writer.
"""
from __future__ import annotations

import contextlib
import difflib
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


def parse_reply(reply):
    """`(data, None)` for a usable reply, else `(None, reason)`.

    `data` is `{summary, changes}`; each change is a dict with a string `task`, and `prompt`,
    `approach` and `files` each a string or null (a missing one is null). A field that is not one
    of those three is kept as it came: confinement refuses it, the parser does not drop it.
    """
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
    summary, changes = data.get('summary'), data.get('changes')
    if not isinstance(summary, str):
        return None, 'reply has no "summary" string'
    if not isinstance(changes, list):
        return None, 'reply has no "changes" list'
    found = []
    for index, item in enumerate(changes, 1):
        where = 'change %d' % index
        if not isinstance(item, dict):
            return None, '%s is not an object' % where
        if not isinstance(item.get('task'), str) or not item['task'].strip():
            return None, '%s has no "task" string' % where
        change = dict(item, task=item['task'].strip())
        for field in CHANGE_FIELDS:
            value = change.setdefault(field, None)
            if value is not None and not isinstance(value, str):
                return None, '%s: %s must be a string or null' % (where, field)
        found.append(change)
    entry = {'summary': ' '.join(summary.split()), 'changes': found}
    try:   # a lone surrogate parses but cannot be stored as UTF-8
        forge_brief._dump(entry).encode('utf-8')
    except UnicodeError:
        return None, 'reply has characters that cannot be stored'
    return entry, None


def _ask(plan, task, prompt, spec, timeout):
    """One dispatch, a retry on an unusable reply. `(data, None)` or `(None, why)`."""
    reminder, error = '', 'no reply'
    for attempt in range(2):
        result = forge_side.run('brief-change', plan, task, prompt + reminder, spec, timeout=timeout)
        if not result['ok']:
            if result['reason'] == 'cap':
                return None, 'the brief-change dispatch cap is reached' + (' (no retry)' if attempt else '')
            return None, 'dispatch failed: %s' % (result['reason'] or 'error')
        try:
            data, error = parse_reply(result['last'])
        except Exception as failure:   # noqa: BLE001 - a broken reply is an unusable reply
            data, error = None, 'reply could not be read (%s)' % type(failure).__name__
        if data is not None:
            return data, None
        reminder = '\n\n' + REMINDER % error
    return None, error


# --- confinement -----------------------------------------------------------------------

def _confine(plan, group, changes: list) -> list:
    """The reasons this proposal must be refused, in change order. Empty when it is confined."""
    reasons, seen = [], set()
    for change in changes:
        task = change['task']
        extra = sorted(key for key in change if key != 'task' and key not in CHANGE_FIELDS)
        if task in seen:
            reasons.append('task %s is changed twice' % task)
        seen.add(task)
        if task not in group.members:
            reasons.append('task %s is not a member of group %s' % (task, group.id))
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


def _emit(plan, record: dict) -> None:
    """One `brief_diff` dataset event. Fail-open, like every other capture."""
    try:
        from forge_dataset.capture import point
        point('brief_diff', str(plan), str(record['task']), {
            'pid': record['pid'], 'scope': record['scope'], 'group': record['group'],
            'state': record['state'], 'tasks': [change['task'] for change in record['changes']]})
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
    """Ask the brief model what the answer to a parked wave question changes, and record the proposal.

    Returns the proposal record (`state` is `proposed` or `refused`; a refused one carries its
    `reason` and still has its diff). Raises `NotAPlan`, `BriefOff` (brief off, or `allowed`
    refuses), `Usage` (no answer yet), `NoSuchTask`, `NoSuchQuestion` and `Refused` (the question
    is not a wave question of a group, or the writer failed). Nothing in the plan changes.
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
    if question.get('scope') != 'wave':
        raise Refused('%s/%s is a %s-scoped question: a proposal acts on one group, so only a wave question '
                      'can be proposed (a plan question needs a new plan)' % (task, qid, question.get('scope')))
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


# --- apply -----------------------------------------------------------------------------

def _validate_new(plan, new_text: str, max_tasks: int) -> None:
    """Run the group validation against the tasks.tsv an apply would write, touching nothing real."""
    with tempfile.TemporaryDirectory(prefix='forge-validate-') as scratch:
        scratch = Path(scratch)
        (scratch / 'tasks.tsv').write_text(new_text, encoding='utf-8')
        groups_file = Path(plan) / 'groups.tsv'
        (scratch / 'groups.tsv').write_bytes(_read_bytes(groups_file) or b'')
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


def apply(plan, pid, approve_hash, actor='cli') -> dict:
    """Write an approved proposal. Raises `Refused` (nothing changed) or `NoSuchProposal`.

    Returns `{pid, state: "approved", files, tasks, warning}`; `files` are the plan files
    written, relative to the plan; `warning` says if waves.tsv could not be refreshed.
    """
    who = answers._actor(actor)
    plan = Path(plan)
    if not plan.is_dir() or not (plan / 'tasks.tsv').is_file():
        raise forge_brief.NotAPlan(str(plan))
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
            if files_edits:
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
    _emit(plan, record)
    names = [str(path.relative_to(plan)) for path, _ in written] + (['groups.frozen'] if files_edits else [])
    return {'pid': pid, 'state': 'approved', 'files': names,
            'tasks': [change['task'] for change in changes], 'warning': warning}


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
    lines = ['%s  %s  scope %s  group %s  (asked by %s/%s)' % (
        item['pid'], item['state'], item.get('scope'), item.get('group'), item.get('task'), item.get('qid')),
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
