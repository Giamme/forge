"""What the plan-time gates did to a plan, and the edits they may make to tasks.tsv.

`$PLAN/plan-gates.json` records it: drift files added (or only predicted), forward dependencies
added, skipped for a cycle or only predicted, prompt scores, requirements no task covers, and
which items were sent back to the planner. It lives across re-plans. `sent_back` is append-only
history, so an item goes back at most once per plan whatever happens to the current state in
between; `pending` is what the current `plan` call marked and `sendback()` turns into
`plan.sendback.md`.

Standard library only (the threshold is read from forge_jev when it is importable). Every write
is a temp file in the same directory and an `os.replace`.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

GATES_FILE = 'plan-gates.json'
SENDBACK_FILE = 'plan.sendback.md'
KINDS = ('prompt', 'coverage')
DEFAULT_PROMPT_WARN = 0.55
FILES_COL, DEPS_COL = 3, 1
MIN_COLS = 7


def _atomic_write(path: Path, data: str | bytes) -> None:
    raw = data.encode('utf-8', 'surrogateescape') if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(raw)
        if path.exists():
            shutil.copymode(str(path), tmp)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(plan) -> dict:
    try:
        value = json.loads((Path(plan) / GATES_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save(plan, data: dict) -> None:
    _atomic_write(Path(plan) / GATES_FILE, json.dumps(data, indent=2) + '\n')


# --- tasks.tsv ---------------------------------------------------------------------------

def _read_tasks(plan: Path):
    """(text, lines) with every byte kept, so a rewrite touches only the cell it means to."""
    try:
        text = (plan / 'tasks.tsv').read_bytes().decode('utf-8', 'surrogateescape')
    except OSError:
        return None, []
    return text, text.split('\n')


def _is_row(line: str) -> bool:
    return bool(line.strip()) and not line.startswith('#') and line.count('\t') + 1 >= MIN_COLS


def _names(cell: str) -> list[str]:
    cell = cell.strip()
    return [] if cell in ('', '-') else [n.strip() for n in cell.split(',') if n.strip()]


def _norm(name: str) -> str:
    name = name.strip()
    while name.startswith('./'):
        name = name[2:]
    return name.rstrip('/') or '.'


def _covered(name: str, listed: list[str]) -> bool:
    name = _norm(name)
    for have in listed:
        have = _norm(have)
        if have == '.' or name == have or name.startswith(have + '/'):
            return True
    return False


def _row_index(lines: list[str], task: str):
    for i, line in enumerate(lines):
        if _is_row(line) and line.split('\t', 1)[0] == task:
            return i
    return None


def _rewrite(plan: Path, lines: list[str], index: int, col: int, value: str) -> None:
    cells = lines[index].split('\t')
    cells[col] = value
    lines[index] = '\t'.join(cells)
    _atomic_write(plan / 'tasks.tsv', '\n'.join(lines))


def add_files(plan, task: str, files) -> list[str]:
    """Append `files` to the task's `files` column; returns the names actually added."""
    plan = Path(plan)
    _text, lines = _read_tasks(plan)
    index = _row_index(lines, task)
    if index is None:
        return []
    listed = _names(lines[index].split('\t')[FILES_COL])
    added = []
    for name in files:
        name = str(name).strip()
        if not name or name == '-' or _covered(name, listed + added):
            continue
        added.append(name)
    if added:
        _rewrite(plan, lines, index, FILES_COL, ','.join(listed + added))
    return added


def _deps(lines: list[str]) -> dict[str, list[str]]:
    out = {}
    for line in lines:
        if _is_row(line):
            cells = line.split('\t')
            out[cells[0]] = _names(cells[DEPS_COL])
    return out


def add_dep(plan, earlier: str, later: str) -> bool:
    """Make `earlier` wait for `later`. False, with no change, when that would form a cycle."""
    plan = Path(plan)
    _text, lines = _read_tasks(plan)
    deps = _deps(lines)
    if earlier not in deps or later not in deps or earlier == later:
        return False
    if later in deps[earlier]:
        return True
    # A cycle forms when `later` already waits for `earlier`, however indirectly.
    seen, stack = set(), [later]
    while stack:
        node = stack.pop()
        if node == earlier:
            return False
        if node in seen:
            continue
        seen.add(node)
        stack.extend(deps.get(node, ()))
    index = _row_index(lines, earlier)
    _rewrite(plan, lines, index, DEPS_COL, ','.join(deps[earlier] + [later]))
    return True


# --- send-back ---------------------------------------------------------------------------

def mark_pending(data: dict, kind: str, key: str) -> bool:
    """Queue `key` for sending back unless it ever was. Returns whether it was queued."""
    if kind not in KINDS:
        raise ValueError('unknown send-back kind: %r' % (kind,))
    sent = data.setdefault('sent_back', {}).setdefault(kind, [])
    if key in sent:
        return False
    sent.append(key)
    data.setdefault('pending', {}).setdefault(kind, []).append(key)
    return True


def _prompt_warn() -> float:
    try:
        import forge_jev
        return float(forge_jev.threshold('prompt_warn'))
    except Exception:
        return DEFAULT_PROMPT_WARN


def _score(value) -> str:
    return ' (%.2f)' % value if isinstance(value, (int, float)) else ''


def _line(text) -> str:
    return ' '.join(str(text).split())


def _titles(plan: Path) -> dict[str, str]:
    _text, lines = _read_tasks(plan)
    return {l.split('\t')[0]: l.split('\t')[6] for l in lines if _is_row(l)}


def _sendback_text(plan: Path, data: dict) -> str:
    pending = data.get('pending') or {}
    threshold = _prompt_warn()
    titles = _titles(plan)
    prompts = []
    for task in pending.get('prompt') or []:
        title = titles.get(task)
        entry = (data.get('prompt') or {}).get(task) or {}
        prompts.append('- %s%s%s: tasks/%s/prompt.md' % (
            task, ' — "%s"' % _line(title) if title else '', _score(entry.get('p')), task))
    uncovered = {u.get('sha'): u for u in (data.get('coverage') or {}).get('uncovered') or []
                 if isinstance(u, dict)}
    covers = []
    for sha in pending.get('coverage') or []:
        entry = uncovered.get(sha)
        if entry and entry.get('text'):
            covers.append('- "%s"%s: add it to a task\'s ## Acceptance criteria, or add a task'
                          % (_line(entry['text']), _score(entry.get('p'))))
    if not prompts and not covers:
        return ''
    out = ['# Sent back by forge\'s plan gates', '',
           'Rewrite what is listed here, then run `plan` again. Forge sends each task and requirement back once:',
           'anything still flagged next time is listed under the table instead.']
    if prompts:
        out += ['', '## Task prompts a reviewer could not judge (prompt gate, below %g)' % threshold] + prompts
    if covers:
        out += ['', '## Requirements no task covers (coverage gate)'] + covers
    return '\n'.join(out) + '\n'


def sendback(plan) -> bool:
    """Write (or remove) plan.sendback.md from the pending items, then empty `pending`."""
    plan = Path(plan)
    data = load(plan)
    body = _sendback_text(plan, data)
    path = plan / SENDBACK_FILE
    if body:
        _atomic_write(path, body)
    else:
        try:
            path.unlink()
        except OSError:
            pass
    pending = data.get('pending') or {}
    if any(pending.get(kind) for kind in pending):
        data['pending'] = {kind: [] for kind in KINDS}
        save(plan, data)
    return bool(body)


# --- the block under the table -----------------------------------------------------------

def _entries(value) -> list[dict]:
    return [e for e in value or [] if isinstance(e, dict)]


def marks(plan) -> list[str]:
    data = load(Path(plan))
    out = []
    drift = data.get('drift') if isinstance(data.get('drift'), dict) else {}
    for task in sorted(drift):
        entry = drift[task] if isinstance(drift[task], dict) else {}
        for item in _entries(entry.get('added')):
            out.append('drift added to %s: %s%s' % (task, item.get('file'), _score(item.get('p'))))
        for item in _entries(entry.get('would_add')):
            out.append('drift (shadow) would add to %s: %s%s' % (task, item.get('file'), _score(item.get('p'))))
    forward = data.get('forward') if isinstance(data.get('forward'), dict) else {}

    def pairs(name):
        return sorted(_entries(forward.get(name)), key=lambda e: (str(e.get('earlier')), str(e.get('later'))))
    for item in pairs('added'):
        out.append('forward added a dependency: %s now waits for %s%s'
                   % (item.get('earlier'), item.get('later'), _score(item.get('p'))))
    for item in pairs('would_add'):
        out.append('forward (shadow) would add a dependency: %s would wait for %s%s'
                   % (item.get('earlier'), item.get('later'), _score(item.get('p'))))
    for item in pairs('skipped_cycle'):
        out.append('forward skipped a dependency that would form a cycle: %s on %s%s'
                   % (item.get('earlier'), item.get('later'), _score(item.get('p'))))
    prompt = data.get('prompt') if isinstance(data.get('prompt'), dict) else {}
    sent = (data.get('sent_back') or {}).get('prompt') or []
    threshold = _prompt_warn()
    # The shadow lines come first, then the rewrites that did not help; ids are sorted within each.
    shadows, stuck = [], []
    for task in sorted(prompt):
        entry = prompt[task] if isinstance(prompt[task], dict) else {}
        p, after = entry.get('p'), entry.get('after')
        if entry.get('shadow'):
            if isinstance(p, (int, float)) and p < threshold:
                shadows.append('prompt (shadow) %s: %.2f, below %g' % (task, p, threshold))
        elif task in sent and isinstance(after, (int, float)) and after < threshold:
            stuck.append('prompt still below %g after its rewrite: %s (%.2f)' % (threshold, task, after))
    out += shadows + stuck
    coverage = data.get('coverage') if isinstance(data.get('coverage'), dict) else {}
    sent = (data.get('sent_back') or {}).get('coverage') or []
    uncovered = sorted((e for e in _entries(coverage.get('uncovered')) if e.get('sha') in sent),
                       key=lambda e: str(e.get('text')))
    for item in uncovered:
        out.append('requirements no task covers: "%s"%s' % (_line(item.get('text')), _score(item.get('p'))))
    return ['decision-model gates:'] + ['  ' + line for line in out] if out else []
