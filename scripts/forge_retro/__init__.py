"""Retro notes: what got in an agent's way, checked against the dispatch and stored locally.

A note is one line in a final message, `FORGE_RETRO: {json}`, or the same JSON object as one
line in a dwarf worktree's `.forge/retro-inbox.jsonl`. Parsing keeps a line it cannot read.
`record` adds forge's context, checks the evidence against that dispatch's log, and writes
the notes to the plan. Nothing here is written to project memory, the ledger, or a prompt.

Stored notes carry the agent's fields plus role, spec, alias, harness, effort, tier, task,
attempt, run_id, version, os, t and check (`verified` or `claimed`). An orchestrator note,
from `note`, is always `claimed`.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

SKILL = Path(__file__).resolve().parents[2]
TYPES = ('environment', 'sandbox', 'tooling', 'prompt', 'forge-bug', 'other')
FIXABLE = ('user', 'forge', 'unsure')
MARKER = 'FORGE_RETRO:'
ERROR_CAP = 2048          # 2 KB, on the error text
ERROR_LINE = 120          # how much of the first error line the evidence check looks for
MALFORMED_CAP = 200
_EFFORT_UNSET = '<harness default>'


def parse(text: str) -> list:
    """The retro notes in a final message, in order. A line that is not a note is ignored."""
    notes = []
    for line in (text or '').replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        start = line.find(MARKER)
        if start < 0:
            continue
        payload = line[start + len(MARKER):].strip()
        notes.append(_parse_payload(payload, line.strip()))
    return notes


def check(note, log_text) -> str:
    """`verified` when the log contains the command and the first error line, else `claimed`.

    Both are compared with whitespace collapsed. The error contributes only its first
    non-blank line, cut at 120 characters before collapsing. No evidence is `claimed`.
    """
    evidence = note.get('evidence') if isinstance(note, dict) else None
    if not isinstance(evidence, dict):
        return 'claimed'
    command = evidence.get('command')
    error = evidence.get('error')
    if not isinstance(command, str) or not isinstance(error, str):
        return 'claimed'
    command = _collapse(command)
    first = next((line for line in error.splitlines() if line.strip()), '')
    snippet = _collapse(first[:ERROR_LINE])
    if not command or not snippet:
        return 'claimed'
    haystack = _collapse(log_text or '')
    if command in haystack and snippet in haystack:
        return 'verified'
    return 'claimed'


def record(plan, role, dispatch_dir, task=None, attempt=None, inbox=None) -> list:
    """Read one dispatch's notes, stamp them with forge's context, and store them.

    Reads `<dispatch_dir>/<role>.last` and `<role>.log`. A dwarf also reads the worktree
    inbox at `inbox`, or the path derived from the plan's repo, run id and task. The inbox
    is deleted after the notes are stored. Returns the stored notes, which is empty when
    the dispatch said nothing.
    """
    plan = _require_plan(plan)
    dispatch = Path(dispatch_dir)
    resolved = _kv(dispatch / ('%s.resolved' % role))
    last = _text(dispatch / ('%s.last' % role))
    log_text = _text(dispatch / ('%s.log' % role))
    notes = parse(last)
    inbox_path = None
    if role == 'dwarf':
        inbox_path = Path(inbox) if inbox else _derived_inbox(plan, task)
        inbox_notes, inbox_path = _load_inbox(inbox_path)
        notes = inbox_notes + notes
    if not notes:
        _delete(inbox_path)
        return []
    context = _context(plan, role, resolved, task, attempt)
    stored = [_stamp(_complete(note), context, check(note, log_text)) for note in notes]
    _persist(plan, stored, task)
    _delete(inbox_path)
    for note in stored:
        _emit(plan, note)
    return stored


def note(plan, **fields) -> list:
    """Record one orchestrator note. It is always `claimed`: there is no dispatch log."""
    plan = _require_plan(plan)
    agent = _from_object({
        'type': fields.get('type'),
        'fixable_by': fields.get('fixable_by'),
        'summary': '' if fields.get('summary') is None else fields.get('summary'),
        'blocking': fields.get('blocking') is True,
        'tried': fields.get('tried'),
        'evidence': _parts_evidence(fields),
    })
    stored = _stamp(agent, _context(plan, 'orchestrator', {}, None, None), 'claimed')
    _persist(plan, [stored], None)
    _emit(plan, stored)
    return [stored]


# --- parsing ---------------------------------------------------------------------

def _parse_payload(payload: str, raw: str) -> dict:
    try:
        obj = json.loads(payload) if payload else None
    except json.JSONDecodeError:
        obj = None
    if not isinstance(obj, dict):
        # Kept, not dropped. The summary is the text that failed to parse (not the marker),
        # capped at 200 characters, so a later report can show what the agent wrote.
        text = payload if payload else raw.strip()
        return {'type': 'other', 'summary': text[:MALFORMED_CAP], 'malformed': True}
    return _from_object(obj)


def _from_object(obj: dict) -> dict:
    return {
        'type': obj.get('type') if obj.get('type') in TYPES else 'other',
        'fixable_by': obj.get('fixable_by') if obj.get('fixable_by') in FIXABLE else 'unsure',
        'summary': _one_line(obj.get('summary')),
        'evidence': _evidence(obj.get('evidence')),
        'blocking': obj.get('blocking') is True,
        'tried': _tried(obj.get('tried')),
    }


def _parts_evidence(fields: dict):
    command, exit_code, error = fields.get('command'), fields.get('exit'), fields.get('error')
    if command is None and exit_code is None and error is None:
        return None
    return {'command': command, 'exit': exit_code, 'error': error}


def _evidence(value):
    if not isinstance(value, dict):
        return None
    command, error = value.get('command'), value.get('error')
    return {
        'command': command if isinstance(command, str) else None,
        'exit': _exit_code(value.get('exit')),
        'error': _cap_error(error) if isinstance(error, str) else None,
    }


def _exit_code(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _tried(value):
    if not isinstance(value, str):
        return None
    text = _one_line(value)
    return text or None


def _one_line(value) -> str:
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())


def _cap_error(text: str) -> str:
    raw = text.encode('utf-8', 'replace')
    if len(raw) <= ERROR_CAP:
        return text
    return raw[:ERROR_CAP].decode('utf-8', 'ignore')


def _collapse(text: str) -> str:
    return ' '.join(text.split())


def _complete(note: dict) -> dict:
    note = dict(note)
    note.setdefault('evidence', None)
    note.setdefault('blocking', False)
    note.setdefault('tried', None)
    return note


def _parse_inbox(text: str) -> list:
    notes = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if MARKER in line:
            notes.extend(parse(line))
        else:
            notes.append(_parse_payload(line.strip(), line.strip()))
    return notes


# --- context ---------------------------------------------------------------------

def _context(plan: Path, role: str, resolved: dict, task, attempt) -> dict:
    name = _task_name(task)
    return {
        'role': resolved.get('role') or role,
        'spec': _spec(resolved),
        'alias': _field(resolved, 'alias'),
        'harness': _field(resolved, 'harness'),
        'effort': _field(resolved, 'effort'),
        'tier': _tier(plan, name),
        'task': name,
        'attempt': _attempt(attempt),
        'run_id': _first_line(plan / 'run_id'),
        'version': _first_line(SKILL / 'VERSION'),
        'os': os.uname().sysname,
        't': int(time.time()),
    }


def _spec(resolved: dict):
    """The spec line, or alias:effort when the dispatch did not record one.

    `<role>.resolved` names the alias, harness and effort. The spec the caller asked for
    is not one of its lines today, so a missing `spec=` is rebuilt the way forge-ui does.
    """
    spec = (resolved.get('spec') or '').strip()
    if spec:
        return spec
    alias = (resolved.get('alias') or '').strip()
    if not alias:
        return None
    effort = (resolved.get('effort') or '').strip()
    if effort and effort != _EFFORT_UNSET:
        return alias + ':' + effort
    return alias


def _field(resolved: dict, key: str):
    value = (resolved.get(key) or '').strip()
    return value or None


def _tier(plan: Path, task: str | None):
    if not task:
        return None
    for line in _text(plan / 'tasks.tsv').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 3 and parts[0] == task:
            return parts[2].strip() or None
    return None


def _attempt(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip('-').isdigit():
        return int(value.strip())
    return None


def _task_name(task):
    if task is None:
        return None
    text = str(task).strip()
    if not text or text in ('.', '..') or '/' in text or '\\' in text or '\x00' in text:
        return None
    return text


def _stamp(note: dict, context: dict, verdict: str) -> dict:
    stored = _complete(note)
    stored.update(context)
    stored['check'] = verdict
    return stored


def _derived_inbox(plan: Path, task) -> Path | None:
    name = _task_name(task)
    repo, run_id = _first_line(plan / 'repo'), _first_line(plan / 'run_id')
    if not name or not repo or not run_id:
        return None
    from forge_worktrees import wt_root
    return Path(wt_root(repo, run_id)) / name / '.forge' / 'retro-inbox.jsonl'


def _load_inbox(path: Path | None):
    if path is None or not path.is_file():
        return [], None
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return [], None
    return _parse_inbox(text), path


def _delete(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


# --- storage ---------------------------------------------------------------------

def _require_plan(plan) -> Path:
    path = Path(plan)
    if not path.is_dir():
        raise FileNotFoundError(path)
    return path


def _persist(plan: Path, notes: list, task) -> None:
    if not notes:
        return
    directory = plan / 'retro'
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / '.lock'
    with open(lock_path, 'a', encoding='utf-8') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            _append(directory / 'notes.jsonl', notes)
            name = _task_name(task)
            if name:
                _rewrite(plan / 'tasks' / name / 'retro.json', notes)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _append(path: Path, notes: list) -> None:
    blob = ''.join(_dump(note) + '\n' for note in notes)
    with path.open('a', encoding='utf-8', newline='\n') as handle:
        handle.write(blob)


def _rewrite(path: Path, notes: list) -> None:
    """Replace the task's note array, including the notes already there, in one rename."""
    existing = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, list):
            existing = loaded
    existing.extend(notes)
    _atomic(path, json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + '\n')


def _atomic(path: Path, text: str) -> None:
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


def _dump(note: dict) -> str:
    return json.dumps(note, ensure_ascii=False, sort_keys=True)


def _emit(plan: Path, note: dict) -> None:
    """One retro_note dataset event. Fail-open: the plan files are the record."""
    try:
        from forge_dataset.capture import point
        point('retro_note', str(plan), note.get('task'), dict(note))
    except Exception:
        return


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def _first_line(path: Path):
    for line in _text(path).splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _kv(path: Path) -> dict:
    found = {}
    for line in _text(path).splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip():
            found[key.strip()] = value.strip()
    return found
