"""The end-of-run retro report: `<plan>/retro.md` from `retro/notes.jsonl`.

Notes that share a type and a normalised evidence command are one pattern. A note with no
command is matched on its normalised summary instead. Normalising lowercases, collapses
whitespace, and replaces absolute paths, hex ids and numbers with placeholders, so two
tasks that failed the same way under different worktree paths are one pattern.

Who can fix a pattern is decided after that merge. Notes that name different owners stay
one pattern: the majority `fixable_by` is the group, and a tie is unsure.
`forge retro report` writes the file atomically.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from forge_retro import TYPES

# The error shown for a pattern. The stored text can be 2 KB; the report keeps the head.
ERROR_LINES = 10

GROUPS = (
    ('user', 'You can fix this on this machine'),
    ('forge', 'forge should fix this'),
    ('unsure', 'Not sure who should fix this'),
)

# What the user can do about a pattern, and why that makes the next run faster or more
# reliable. Sandbox is built below: the setting depends on the harness that blocked it.
_FIXES = {
    'environment': (
        'Install the missing dependency, or set `.forge/setup` so worktrees get it.',
        'The next run starts with that dependency already installed, so agents do not spend an attempt finding it.',
    ),
    'tooling': (
        'Install the missing tool, or add that install to `.forge/setup` so worktrees get it.',
        'The next run finds the tool on the first try, so agents do not retry a command that cannot exist yet.',
    ),
    'prompt': (
        'Change the task prompt so the instruction matches what the agent is allowed to do.',
        'The next run spends its attempt on the task, not on an instruction it cannot follow.',
    ),
    'forge-bug': (
        'Update forge, or stop asking agents to run the command that fails.',
        'The next run does not hit the same forge failure, so the attempt is spent on the task.',
    ),
    'other': (
        'Remove the obstacle named above before starting the next run.',
        'The next run does not stop on the same obstacle, so it finishes in fewer attempts.',
    ),
}
_SANDBOX_WHY = ('The next run is not stopped by the same permission denial, so the agent can finish the command.')

# The flag forge's `--yolo-dwarf` / `--yolo-qa` turns on for that harness (references/harnesses.md).
_BYPASS = {
    'codex': '--dangerously-bypass-approvals-and-sandbox',
    'claude': '--dangerously-skip-permissions',
    'openclaude': '--dangerously-skip-permissions',
    'opencode': '--auto',
    'antigravity': '--dangerously-skip-permissions',
    'agy': '--dangerously-skip-permissions',
    'grok': '--always-approve',
}

# A quoted absolute path may contain spaces (`"/tmp/Review One/project"`). The quotes are
# part of the match, so two worktrees whose names differ only inside them collapse to one
# placeholder. `_quoted_path` decides whether the quotes hold a path or a command line.
# Replaced before the unquoted forms, which stop at whitespace.
_QUOTED_PATH = re.compile(
    r'''(?<![a-z0-9_])(?:"(?:~|/|[a-z]:[\\/])[^"]*"|'(?:~|/|[a-z]:[\\/])[^']*')''')
# Stands in for a shell-escaped space (`/tmp/Review\ One`) while a path is matched.
# It is not whitespace, so the escaped space stays inside the path token.
_ESC_SPACE = '\ue000'
# A filesystem path, not the slash in https://host/path. The lookbehind rejects a scheme
# colon, a doubled slash, and a leading tilde, so a URL stays a URL and `~/...` is one
# home path rather than a unix path with the tilde left in front.
_UNIX_PATH = re.compile(r'(?<![a-z0-9_.:/~])/\S+')
_HOME_PATH = re.compile(r'(?<![a-z0-9_.:/])~/\S+')
_WIN_PATH = re.compile(r'(?<![a-z0-9_])[a-z]:[\\/]\S+')
_TRAILING = '.,;:)]}\'"'

# UUIDs are replaced whole, before the hex run, or the dashes would split them. A hex id
# is seven or more characters with a letter: `git rev-parse --short` prints seven. A long
# decimal has no letter, so a port or a size stays a number.
_UUID = re.compile(
    r'(?<![0-9a-f])[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?![0-9a-f])')
_HEX = re.compile(r'(?<![0-9a-f])(?=[0-9a-f]*[a-f])[0-9a-f]{7,}(?![0-9a-f])')
_NUMBER = re.compile(r'\d+')

EMPTY = "Nothing got in the agents' way in this run.\n"


def normalise(text: str) -> str:
    """Lowercase, collapse whitespace, and replace paths, hex ids and numbers.

    Paths are replaced before hex ids and hex ids before numbers, so a path is one
    placeholder and a hex id is not split into numbers. A shell-escaped space stays
    inside the path (`/tmp/Review\\ One`). A quoted command keeps a flag that follows
    the path (`bash -c "/tmp/wt/test.sh --unit"`).
    """
    if not isinstance(text, str):
        return ''
    collapsed = text.lower().replace('\\ ', _ESC_SPACE)
    collapsed = ' '.join(collapsed.split())
    collapsed = _QUOTED_PATH.sub(_quoted_path, collapsed)
    collapsed = _WIN_PATH.sub(_path_token, collapsed)
    collapsed = _UNIX_PATH.sub(_path_token, collapsed)
    collapsed = _HOME_PATH.sub(_path_token, collapsed)
    collapsed = ' '.join(collapsed.replace(_ESC_SPACE, ' ').split())
    collapsed = _UUID.sub('<hex>', collapsed)
    collapsed = _HEX.sub('<hex>', collapsed)
    return _NUMBER.sub('<n>', collapsed)


_COMMAND_LINE = re.compile(r'(?:^|\s)(?:-[a-z]*c|eval)\s*$')
# Commands whose quoted argument is a command line for another shell.
_REMOTE = re.compile(r'(?:^|\s)(?:ssh|mosh)\s')


def _quoted_path(match) -> str:
    """Replace the quoted absolute path. Keep the arguments of a quoted command line.

    A quoted string is one of two things. The text before it decides which:

    - a command line, when it follows `-c` / `eval` or when the command is `ssh` / `mosh`
      (`bash -c "/usr/bin/npm test tests/unit"`, `ssh host "/usr/bin/npm test"`). The path is
      the first word only; every other word is an argument and is kept, so `"/usr/bin/npm test"`
      and `"/usr/bin/npm install"` stay two patterns, and so do `"/usr/bin/pytest tests/unit -q"`
      and `"/usr/bin/pytest tests/integration -q"`.
    - otherwise one path, spaces and dashes included: `cat "/tmp/Review One"` and
      `cat "/tmp/Review -One"` are both `cat <path>`. A word that looks like a flag does not
      make the quote a command line.

    A space inside a directory name of a quoted command line (`"/tmp/Review One/t.sh -q"`)
    cannot be told from an argument, so the rest of that name is kept and two worktrees
    named differently stay two patterns. Splitting is the safe failure: the report shows
    the same problem twice, never two different problems as one.
    """
    parts = match.group(0)[1:-1].split(' ')
    before = match.string[:match.start()]
    if not (_COMMAND_LINE.search(before) or _REMOTE.search(before)):
        return '<path>'
    return ' '.join(['<path>'] + parts[1:])


def build(plan) -> dict:
    """The report structure for a plan directory. Raises FileNotFoundError when it is not one."""
    plan = Path(plan)
    if not plan.is_dir():
        raise FileNotFoundError(plan)
    notes, malformed = _load(plan / 'retro' / 'notes.jsonl')
    rows = _task_rows(plan)
    run_models = _run_models(rows, notes)
    earlier = _earlier_index(_current_ids(plan))
    grouped = _patterns(notes, len(rows), run_models, earlier)
    groups = []
    for key, title in GROUPS:
        if grouped[key]:
            groups.append({'id': key, 'title': title, 'patterns': grouped[key]})
    return {
        'empty': not notes and not malformed,
        'tasks': len(rows),
        'models': run_models,
        'groups': groups,
        'malformed': malformed,
    }


def render(document: dict) -> str:
    """The Markdown report. Empty when nothing was recorded."""
    if document.get('empty'):
        return EMPTY
    out = ['# Retro', '']
    for group in document['groups']:
        out.append('## ' + group['title'])
        out.append('')
        for pattern in group['patterns']:
            out.extend(_render_pattern(pattern))
            out.append('')
    malformed = document.get('malformed') or []
    if malformed:
        noun = 'note' if len(malformed) == 1 else 'notes'
        out.append('## Malformed notes')
        out.append('')
        out.append('%d %s could not be parsed.' % (len(malformed), noun))
        out.append('')
        for text in malformed:
            out.append('- %s' % text)
        out.append('')
    return '\n'.join(out).rstrip() + '\n'


def write(plan) -> tuple[dict, Path]:
    """Write `<plan>/retro.md` atomically and return the structure and the path."""
    plan = Path(plan)
    document = build(plan)
    path = plan / 'retro.md'
    _atomic(path, render(document))
    return document, path


# --- patterns --------------------------------------------------------------------

def _pattern_key(note: dict):
    if not isinstance(note, dict) or note.get('malformed') is True:
        return None
    kind = note.get('type') if note.get('type') in TYPES else 'other'
    command = _command(note)
    if command is not None:
        text = normalise(command)
        if text:
            return (kind, 'command', text)
    summary = note.get('summary')
    if not isinstance(summary, str):
        summary = ''
    return (kind, 'summary', normalise(summary))


def _owner(notes) -> str:
    """The group for one pattern.

    Merging happens first, so a failure that several tasks recorded stays one pattern
    when those notes disagree about who should fix it. The majority `fixable_by` wins.
    A tie is `unsure`: the notes do not agree, and an unknown value counts as that.
    """
    counts = {'user': 0, 'forge': 0, 'unsure': 0}
    for note in notes:
        bucket = note.get('fixable_by')
        counts[bucket if bucket in counts else 'unsure'] += 1
    best = max(counts.values())
    winners = [name for name in counts if counts[name] == best]
    if len(winners) == 1:
        return winners[0]
    return 'unsure'


def _patterns(notes, task_total, run_models, earlier) -> dict:
    order = []
    buckets = {}
    for note in notes:
        key = _pattern_key(note)
        if key not in buckets:
            order.append(key)
            buckets[key] = []
        buckets[key].append(note)
    grouped = {key: [] for key, _title in GROUPS}
    for key in order:
        owner = _owner(buckets[key])
        grouped[owner].append(
            _pattern(buckets[key], task_total, run_models, earlier, owner))
    for items in grouped.values():
        items.sort(key=lambda item: (
            -item['tasks_hit'], item['summary'].lower(), item['type'], item['command'] or ''))
    return grouped


def _pattern(notes, task_total, run_models, earlier, group_id) -> dict:
    tasks = []
    seen = set()
    models, harnesses = [], []
    verified = claimed = 0
    blocking = False
    for note in notes:
        task = _text_field(note.get('task'))
        if task and task not in seen:
            seen.add(task)
            tasks.append(task)
        _remember(models, _text_field(note.get('alias')))
        _remember(harnesses, _text_field(note.get('harness')))
        if note.get('check') == 'verified':
            verified += 1
        elif note.get('check') == 'claimed':
            claimed += 1
        if note.get('blocking') is True:
            blocking = True
    models.sort()
    harnesses.sort()
    representative = _representative(notes)
    kind = representative.get('type') if representative.get('type') in TYPES else 'other'
    specific = models[0] if len(run_models) >= 2 and len(models) == 1 else None
    key = _pattern_key(representative)
    return {
        'type': kind,
        'summary': _summary(notes, representative),
        'command': _command(representative),
        'exit': _exit(representative),
        'error': _cap_lines(_error(representative)),
        'tasks_hit': len(tasks),
        'tasks': task_total,
        'earlier_runs': len(earlier.get(key, ())),
        'models': models,
        'harnesses': harnesses,
        'verified': verified,
        'claimed': claimed,
        'blocking': blocking,
        'model_specific': specific,
        'fix': _fix(kind, harnesses) if group_id == 'user' else None,
    }


def _representative(notes) -> dict:
    """The first note that carries an error, else the first note. File order."""
    for note in notes:
        if _error(note):
            return note
    return notes[0]


def _summary(notes, representative) -> str:
    for note in (representative, *notes):
        text = _one_line(note.get('summary'))
        if text:
            return text
    return ''


def _fix(kind: str, harnesses: list):
    if kind == 'sandbox':
        return {'action': _sandbox_action(harnesses), 'why': _SANDBOX_WHY}
    action, why = _FIXES.get(kind) or _FIXES['other']
    return {'action': action, 'why': why}


def _sandbox_action(harnesses: list) -> str:
    flags = []
    for name in harnesses:
        flag = _BYPASS.get(name.lower())
        if flag and flag not in flags:
            flags.append(flag)
    if not harnesses:
        subject = 'the harness sandbox'
    elif len(harnesses) == 1:
        subject = 'the %s sandbox' % harnesses[0]
    else:
        subject = 'the %s sandboxes' % _and(harnesses)
    loosening = '`--yolo-dwarf` or `--yolo-qa`'
    if flags:
        passed = ' and '.join('`%s`' % flag for flag in flags)
        return 'Loosen %s with %s, which passes %s.' % (subject, loosening, passed)
    return 'Loosen %s with %s.' % (subject, loosening)


def _and(names: list) -> str:
    if len(names) == 2:
        return '%s and %s' % (names[0], names[1])
    return '%s, and %s' % (', '.join(names[:-1]), names[-1])


# --- markdown --------------------------------------------------------------------

def _render_pattern(pattern: dict) -> list:
    out = ['### ' + (pattern['summary'] or pattern['type']), '']
    bits = [pattern['type']]
    if pattern['blocking']:
        bits.append('blocking')
    if pattern['model_specific']:
        bits.append('possibly specific to %s, not forge' % pattern['model_specific'])
    out.append('. '.join(bits) + '.')
    earlier = pattern['earlier_runs']
    if earlier == 1:
        earlier_text = 'and 1 earlier run'
    else:
        earlier_text = 'and %d earlier runs' % earlier
    out.append('%d of %d tasks in this run, %s.' % (pattern['tasks_hit'], pattern['tasks'], earlier_text))
    out.append('Models: %s. Harnesses: %s.' % (_names(pattern['models']), _names(pattern['harnesses'])))
    out.append('Checks: %d verified, %d claimed.' % (pattern['verified'], pattern['claimed']))
    out.extend(_render_evidence(pattern))
    if pattern.get('fix'):
        out.append('')
        out.append('Fix: ' + pattern['fix']['action'])
        out.append(pattern['fix']['why'])
    return out


def _render_evidence(pattern: dict) -> list:
    """The command, exit code and error, each only when the note recorded it.

    A note can carry an exit code and an error with no command. Those still belong in
    the report; the command is not what makes the other two real.
    """
    command = pattern.get('command') or None
    exit_code = pattern.get('exit')
    error = pattern.get('error') or None
    if command is None and exit_code is None and error is None:
        return []
    if command and exit_code is not None:
        line = 'Evidence: `%s` exited %s.' % (command, exit_code)
    elif command:
        line = 'Evidence: `%s`.' % command
    elif exit_code is not None:
        line = 'Evidence: exited %s.' % exit_code
    else:
        line = None
    out = ['']
    if line:
        out.append(line)
    if error:
        if line:
            out.append('')
        out.append(_fence(error))
    return out


def _fence(text: str) -> str:
    body = text.splitlines() or ['']
    marker = '```'
    while any(line.startswith(marker) for line in body):
        marker += '`'
    return marker + '\n' + text + '\n' + marker


def _names(values) -> str:
    return ', '.join(values) if values else 'none'


# --- notes and tasks -------------------------------------------------------------

def _load(path: Path):
    notes, malformed = [], []
    for line in _text(path).splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            malformed.append(_clip(line))
            continue
        if not isinstance(obj, dict):
            malformed.append(_clip(line))
            continue
        if obj.get('malformed') is True:
            summary = obj.get('summary')
            malformed.append(_clip(summary if isinstance(summary, str) and summary.strip() else line))
            continue
        notes.append(obj)
    return notes, malformed


def _task_rows(plan: Path) -> list:
    rows = []
    for line in _text(plan / 'tasks.tsv').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        parts = line.split('\t')
        if parts and parts[0].strip():
            rows.append(parts)
    return rows


def _run_models(rows, notes) -> list:
    found = []
    for row in rows:
        _remember(found, _spec_alias(row, 4))
        _remember(found, _spec_alias(row, 5))
    for note in notes:
        _remember(found, _text_field(note.get('alias')))
    found.sort()
    return found


def _spec_alias(row, index):
    if len(row) <= index:
        return None
    text = row[index].strip()
    if not text or text == '-' or text.upper() == 'UNASSIGNED':
        return None
    head = text.split(':', 1)[0].strip()
    if not head or head == '-' or head.upper() == 'UNASSIGNED':
        return None
    return head


def _current_ids(plan: Path) -> set:
    """Ids that can mean this plan, so its own retro_note events are not earlier runs.

    The dataset uid and the legacy uid identify the run. The run id is the plan's
    display name: directory basenames repeat, so it is only a fallback for an event
    that has no uid at all.

    Note capture resolves the plan (`locate` → `Path.resolve`) before `run_uid` hashes
    a legacy id. Hash that same absolute path. `report ./plan` on a plan with no
    `dataset.uid` must not treat this plan's own events as another run.
    """
    found = set()
    for name in ('dataset.uid', 'run_id'):
        value = _first_line(plan / name)
        if value:
            found.add(value)
    try:
        from forge_dataset.harvest import legacy_uid
        found.add(legacy_uid(plan.resolve()))
    except Exception:
        pass
    return found


def _earlier_index(current: set) -> dict:
    """pattern key -> set of other runs that recorded a retro_note of that pattern."""
    found = {}
    for run, run_id, data in _retro_events():
        if _is_current(run, run_id, data, current):
            continue
        key = _pattern_key(data)
        if key is None:
            continue
        identity = _run_identity(run, run_id) or _run_identity(data.get('run'), data.get('run_id'))
        if not identity:
            continue
        found.setdefault(key, set()).add(identity)
    return found


def _retro_events() -> list:
    """retro_note events from the local dataset. A missing store is no events, and is not created."""
    try:
        from forge_dataset import connect, dataset_dir, enabled
        if not enabled() or not (dataset_dir() / 'dataset.sqlite').is_file():
            return []
        db = connect()
        try:
            # json.dumps writes `"name": "retro_note"` (default separators). The name check below is the filter.
            rows = db.execute(
                "SELECT run, run_id, data FROM events WHERE kind = 'event' AND data LIKE ?",
                ('%"name": "retro_note"%',)).fetchall()
        finally:
            db.close()
    except Exception:
        return []
    found = []
    for run, run_id, raw in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get('name') == 'retro_note':
            found.append((run, run_id, data))
    return found


def _is_current(run, run_id, data, current: set) -> bool:
    """True when this event is the plan being reported.

    `events.run` is the dataset uid. Another plan with the same display name and a
    different uid is an earlier run, not this one. An event with no uid falls back to
    the display name, which is the only link it has.
    """
    if not current:
        return False
    if not isinstance(data, dict):
        data = {}
    event_uid = _text_field(run) or _text_field(data.get('run'))
    if event_uid:
        return event_uid in current
    return _in(run_id, current) or _in(data.get('run_id'), current)


def _in(value, current: set) -> bool:
    return isinstance(value, str) and value.strip() in current


def _run_identity(run, run_id):
    if isinstance(run, str) and run.strip():
        return run.strip()
    if isinstance(run_id, str) and run_id.strip():
        return run_id.strip()
    return None


# --- fields ----------------------------------------------------------------------

def _command(note: dict):
    evidence = note.get('evidence')
    if not isinstance(evidence, dict):
        return None
    command = evidence.get('command')
    if not isinstance(command, str):
        return None
    command = command.strip()
    return command or None


def _exit(note: dict):
    evidence = note.get('evidence')
    if not isinstance(evidence, dict):
        return None
    value = evidence.get('exit')
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _error(note: dict):
    evidence = note.get('evidence')
    if not isinstance(evidence, dict):
        return None
    error = evidence.get('error')
    if not isinstance(error, str) or not error.strip():
        return None
    return error


def _cap_lines(text):
    if not text:
        return None
    lines = text.splitlines()
    if len(lines) > ERROR_LINES:
        lines = lines[:ERROR_LINES]
    return '\n'.join(lines)


def _path_token(match) -> str:
    token = match.group(0)
    stripped = token.rstrip(_TRAILING)
    if len(stripped) < 2 or re.fullmatch(r'[a-z]:[\\/]', stripped):
        return token
    return '<path>' + token[len(stripped):]


def _one_line(value) -> str:
    if not isinstance(value, str):
        return ''
    return ' '.join(value.split())


def _text_field(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _clip(value) -> str:
    return _one_line(value)[:200]


def _remember(found: list, value) -> None:
    if value and value not in found:
        found.append(value)


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
