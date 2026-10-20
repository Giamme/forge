"""Fractal child tasks of a forge task: the managed run scripts/forge_fractal keeps under
$XDG_STATE_HOME/forge/fractal, read without importing that package and without writing.

Every open walks directory fds one component at a time with O_NOFOLLOW, so a symlink anywhere
under the state root is refused. Only run.json, a task's request/state/outcome/events files,
nodes/, nodes/<n>/node.json and nodes/<n>/candidate.diff are ever opened: never control/,
product/, planning/, steps/ or the ledger.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import time

from forge_ui import diffs, executions


SELECTION = 'fractal-selection.json'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}')
_RUN = re.compile(r'run-[0-9a-f]{20}')
_DIR = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)
_FILE = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_CLOEXEC', 0)
_KIB = 1024
_LIMITS = {SELECTION: 64 * _KIB, 'run.json': 1024 * _KIB, 'request.json': 2048 * _KIB, 'state.json': 256 * _KIB,
           'outcome.json': 256 * _KIB, 'node.json': 4096 * _KIB, 'events.jsonl': 64 * _KIB}
_MAX_NODES = 500
_EVENTS = 50
_HISTORY = 20
_UI_STATUS = dict(pending='PENDING', active='RUNNING', waiting='RUNNING', paused='BLOCKED', completed='MERGED',
                  timeout='TIMEOUT', interrupted='INTERRUPTED', stopped='INTERRUPTED', reserved='PENDING')
_EVENT_FIELDS = ('phase', 'decision', 'status', 'acceptance', 'exit_code', 'iteration', 'cost', 'difficulty', 'model')
_CACHE = executions._SigCache(4096)


class _Skip(Exception):
    """A file or dir that is refused or unreadable; its message is the reason."""


def supported():
    return (os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd and os.scandir in os.supports_fd
            and bool(getattr(os, 'O_NOFOLLOW', 0)) and bool(getattr(os, 'O_DIRECTORY', 0)))


def state_root():
    """forge_fractal.state_root(), except that an empty XDG_STATE_HOME counts as unset (forge
    resolves '' to its working directory)."""
    base = os.environ.get('XDG_STATE_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'state')
    return os.path.join(os.path.realpath(os.path.expanduser(base)), 'forge', 'fractal')


def run_id(path):
    return 'run-' + hashlib.sha256(str(os.path.realpath(path)).encode()).hexdigest()[:20]


def task_id(target):
    return 'task-' + hashlib.sha256(str(target).encode()).hexdigest()[:16]


def _why(exc):
    if isinstance(exc, _Skip):
        return str(exc)
    if isinstance(exc, FileNotFoundError):
        return 'missing'
    if isinstance(exc, OSError) and exc.errno in (errno.ELOOP, errno.EMLINK):  # O_NOFOLLOW met a symlink
        return 'a symlink'
    if isinstance(exc, OSError) and exc.errno == errno.ENOTDIR:  # Linux says so for O_DIRECTORY on a symlink
        return 'a symlink or not a directory'
    return f'cannot read ({type(exc).__name__})'


def _dirs(names):
    """fd of state_root()/names..., each component opened without following a symlink."""
    fd = os.open(state_root(), _DIR)
    try:
        for name in names:
            child = os.open(name, _DIR, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _where(run, *parts):
    """The path a managed file has, for cache keys and nothing else: reads go through dir fds."""
    if parts and parts[0].startswith('task-'):
        parts = ('tasks',) + parts
    return os.path.join(state_root(), 'runs', run, *parts)


def _subdir(parent, name):
    return os.open(name, _DIR, dir_fd=parent)


def _read(parent, name, limit, key=None, parse=None, over=None):
    """parse(bytes, cut, size) of a regular file in a dir fd, kept by its signature under `key` (a parse
    kind and the file's path: an inode is reused, a path seldom is). A file over `limit` bytes is
    refused, unless `over` says which `limit` bytes to read: 'head' or 'tail'."""
    fd = os.open(name, _FILE, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise _Skip('not a regular file')
        if info.st_size > limit and over is None:
            raise _Skip(f'larger than {limit // _KIB} KiB')
        sig = executions._signature(info)
        if key is not None:
            hit = _CACHE.get(key, sig)
            if hit is not None:
                return hit[0]
        cut = info.st_size > limit
        if cut and over == 'tail':
            os.lseek(fd, info.st_size - limit, os.SEEK_SET)
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        value = parse(b''.join(chunks), cut, info.st_size)
        if key is not None and abs(time.time() - info.st_mtime) > executions._SETTLE_S:
            _CACHE.put(key, sig, (value,))
        return value
    finally:
        os.close(fd)


def _object(raw, cut=False):
    def invalid(value):
        raise ValueError('non-finite JSON constant')
    try:
        obj = json.loads(raw.decode('utf-8', 'replace'), parse_constant=invalid)
        if not isinstance(obj, dict):
            raise ValueError()
        json.dumps(obj, allow_nan=False)
        return obj
    except (ValueError, RecursionError, OverflowError):
        raise _Skip('not a JSON object')


# --- small projections ------------------------------------------------------------------------

def _word(value, size=240):
    if not isinstance(value, str):
        return None
    return value if len(value) <= size else value[:size] + '…'


def _tail(value, size):
    if not isinstance(value, str):
        return None
    return value if len(value) <= size else '…' + value[-size:]


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _int(value):
    return value if type(value) is int else None


def _strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _tokens(value):
    if not isinstance(value, dict):
        return None
    return {key: _int(value.get(key)) for key in ('input_tokens', 'cached_input_tokens', 'output_tokens')}


def _admitted(decision):
    """The valid child names one decomposition decision admitted, with their rows."""
    rows = decision.get('admission') if isinstance(decision, dict) else None
    return [row for row in rows if isinstance(row, dict) and isinstance(row.get('name'), str)
            and _NAME.fullmatch(row['name'])] if isinstance(rows, list) else []


def _decision(value, size, full=False):
    if not isinstance(value, dict):
        return None
    result = dict(phase=_word(value.get('phase'), 64), decision=_word(value.get('decision'), 64),
                  reason=_word(value.get('reason'), size))
    if full:
        result.update(attempts=_int(value.get('attempts')), error=_word(value.get('error'), size),
                      admission=[dict(name=row['name'], goal=_word(row.get('goal'), size),
                                      difficulty=_word(row.get('difficulty'), 64), paths=_strings(row.get('paths')),
                                      deps=_strings(row.get('deps')), model=_word(row.get('model')))
                                 for row in _admitted(value)])
    else:
        result['children'] = [row['name'] for row in _admitted(value)]
    return result


def _status(value):
    status = value if isinstance(value, str) and value else None
    return status, 'UNKNOWN' if status is None else _UI_STATUS.get(status, 'FAIL')


def _project(name, node):
    """A node.json as the list shows it. Absolute paths (control, workspace, source, candidate,
    ...) and the large texts are left out."""
    status, ui_status = _status(node.get('status'))
    parent = node.get('parent')
    history = node.get('decomposition_history')
    # Admitted children not yet created are listed as reserved (forge_fractal.decomposition.reserved_nodes).
    admitted = [row for item in (history if isinstance(history, list) else []) + [node.get('decomposition')]
                for row in _admitted(item)]
    parent = parent if isinstance(parent, str) and _NAME.fullmatch(parent) else None
    return dict(_admitted=admitted, id=name, parent=parent, depth=0,
                goal=_word(node.get('goal')), difficulty=_word(node.get('difficulty'), 64),
                paths=_strings(node.get('paths')), deps=_strings(node.get('deps')), model=_word(node.get('model')),
                resolved_model=_word(node.get('resolved_model')), status=status, ui_status=ui_status,
                phase=_word(node.get('phase'), 64), iteration=_int(node.get('iteration')),
                started_at=_number(node.get('started_at')), ended_at=_number(node.get('ended_at')),
                cost=_number(node.get('cost')), unknown_cost_steps=_int(node.get('unknown_cost_steps')),
                tokens=_tokens(node.get('tokens')), decomposition=_decision(node.get('decomposition'), 2000),
                error=_word(node.get('error')), has_diff=False, reserved=False)


def _reserved(name, parent, row):
    return dict(id=name, parent=parent, depth=0, goal=_word(row.get('goal')), difficulty=_word(row.get('difficulty'), 64),
                paths=_strings(row.get('paths')), deps=_strings(row.get('deps')), model=_word(row.get('model')),
                resolved_model=None, status='reserved', ui_status='PENDING', phase=None, iteration=None,
                started_at=None, ended_at=None, cost=None, unknown_cost_steps=None, tokens=None, decomposition=None,
                error=None, has_diff=False, reserved=True)


def _detail(node):
    """What the node endpoint adds to the projection, and the names of the texts it cut."""
    history = node.get('decomposition_history')
    history = [_decision(item, 2000, True) for item in (history if isinstance(history, list) else [])
               if isinstance(item, dict)]
    texts = ('goal', 'last', 'planning_last', 'error')
    cut = [name for name in texts if isinstance(node.get(name), str) and len(node[name]) > 20000]
    if len(history) > _HISTORY:
        cut.append('decomposition_history')
    return dict(goal=_word(node.get('goal'), 20000), last=_tail(node.get('last'), 20000),
                planning_last=_tail(node.get('planning_last'), 20000), error=_word(node.get('error'), 20000),
                decomposition=_decision(node.get('decomposition'), 20000, True),
                planning_tokens=_tokens(node.get('planning_tokens')), decomposition_history=history[-_HISTORY:],
                truncated=cut)


def _state(obj):
    return dict(status=_word(obj.get('status'), 64), acceptance=_word(obj.get('acceptance'), 64),
                elapsed=_number(obj.get('elapsed')), started_at=_number(obj.get('started_at')),
                ended_at=_number(obj.get('ended_at')), exit_code=_int(obj.get('exit_code')),
                error=_word(obj.get('error'), 2000), qa_status=_word(obj.get('qa_status'), 64))


def _event(obj):
    event = dict(at=_number(obj.get('at')), kind=_word(obj.get('kind'), 64), node=_word(obj.get('node'), 101))
    for key in _EVENT_FIELDS:
        value = obj.get(key)
        if isinstance(value, str):
            event[key] = _word(value)
        elif type(value) in (int, float) and math.isfinite(value):
            event[key] = value
    if isinstance(obj.get('reason'), str):
        event['reason'] = _word(obj['reason'])
    return event


def _events(raw, cut, size):
    lines = raw.decode('utf-8', 'replace').split('\n')
    if cut:
        lines = lines[1:]  # a tail read starts inside a line
    events, bad = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = _object(line.encode('utf-8'))
        except _Skip:
            bad += 1
            continue
        if isinstance(obj.get('kind'), str):
            events.append(_event(obj))
    return events[-_EVENTS:], bad


# --- the run, its selection and its tasks -----------------------------------------------------

def _selection(reader):
    """(enabled, run id or None, reason or None) of fractal-selection.json, or None without one."""
    if not os.path.lexists(reader.path / SELECTION):
        return None

    def parse(text, cut, size):
        if size > _LIMITS[SELECTION]:
            reader.warn(SELECTION, 'larger than 64 KiB')
            return False, None, 'invalid ' + SELECTION
        obj = reader.object(SELECTION, text)
        if obj is None:
            return False, None, 'invalid ' + SELECTION
        if obj.get('enabled') is not True:
            return False, None, 'Fractal is off for this run'
        run = obj.get('run')
        if not isinstance(run, str) or not _RUN.fullmatch(run):
            reader.warn(SELECTION, 'invalid run id')
            return True, None, 'invalid run id'
        return True, run, None
    return diffs.cached(reader, SELECTION, 'fractal-selection', parse, limit=_LIMITS[SELECTION] + 1) or (
        False, None, 'invalid ' + SELECTION)


def _run_config(run, warn):
    """run.json of a managed run, or a reason it cannot be used."""
    if not supported():
        return None, 'not supported on this platform'
    try:
        fd = _dirs(['runs', run])
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            warn(f'fractal {run}', _why(exc))
        return None, 'managed run not found'
    try:
        return _read(fd, 'run.json', _LIMITS['run.json'], 'object:' + _where(run, 'run.json'),
                     lambda raw, *_: _object(raw)), None
    except (OSError, _Skip) as exc:
        warn(f'fractal {run}/run.json', _why(exc))
        return None, 'invalid run.json'
    finally:
        os.close(fd)


def summary(reader):
    """RunDetail `fractal`: {enabled, run, available, reason}, or None without fractal-selection.json."""
    selected = _selection(reader)
    if selected is None:
        return None
    enabled, run, reason = selected
    if reason is None:
        config, reason = _run_config(run, reader.warn)
        if config is not None and config.get('runner') != str(reader.path):
            reason = 'runner mismatch'
    return dict(enabled=enabled, run=run, available=reason is None, reason=reason)


def _open(reader, key, ident, target):
    """(TaskFractal skeleton, fd of the managed task dir or None). The caller closes the fd."""
    result = dict(run=key, task=ident, fractal_run=None, fractal_task=task_id(target), available=False, reason=None,
                  state=None, outcome=None, nodes=[], events=[], warnings=reader.warnings, truncated=[])
    head = summary(reader)
    if head is None:
        result['reason'] = 'Fractal is not selected for this run'
        return result, None
    result['fractal_run'] = head['run']
    if not head['available']:
        result['reason'] = head['reason']
        return result, None
    label = result['fractal_task']
    try:
        fd = _dirs(['runs', head['run'], 'tasks', label])
    except OSError as exc:
        if not isinstance(exc, FileNotFoundError):
            reader.warn(f'fractal {label}', _why(exc))
        result['reason'] = 'no Fractal task for this task yet'
        return result, None
    try:
        request = _read(fd, 'request.json', _LIMITS['request.json'],
                        'object:' + _where(head['run'], label, 'request.json'), lambda raw, *_: _object(raw))
        if request.get('output') != str(target):
            result['reason'] = 'request does not match this task'
    except FileNotFoundError:
        result['reason'] = 'no Fractal task for this task yet'
    except (OSError, _Skip) as exc:
        reader.warn(f'fractal {label}/request.json', _why(exc))
        result['reason'] = 'invalid request.json'
    if result['reason'] is not None:
        os.close(fd)
        return result, None
    result['available'] = True
    return result, fd


def _object_file(reader, fd, label, where, name):
    try:
        return _read(fd, name, _LIMITS[name], 'object:' + os.path.join(where, name), lambda raw, *_: _object(raw))
    except FileNotFoundError:
        return None
    except (OSError, _Skip) as exc:
        reader.warn(f'{label}/{name}', _why(exc))
        return None


def _nodes(reader, fd, label, where, truncated):
    """The projection of every node dir; its private `_admitted` rows name the reserved children."""
    try:
        nodes_fd = _subdir(fd, 'nodes')
    except FileNotFoundError:
        return []
    except OSError as exc:
        reader.warn(f'{label}/nodes', _why(exc))
        return []
    found = []
    try:
        with os.scandir(nodes_fd) as stream:
            names = sorted(entry.name for entry in stream
                           if _NAME.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False))
        if len(names) > _MAX_NODES:
            names = names[:_MAX_NODES]
            truncated.append('nodes')
        for name in names:
            try:
                node_fd = _subdir(nodes_fd, name)
            except OSError as exc:
                reader.warn(f'{label}/nodes/{name}', _why(exc))
                continue
            try:
                projection = dict(_read(node_fd, 'node.json', _LIMITS['node.json'],
                                        'node:' + os.path.join(where, 'nodes', name, 'node.json'),
                                        lambda raw, *_: _project(name, _object(raw))))
                try:
                    projection['has_diff'] = stat.S_ISREG(os.stat('candidate.diff', dir_fd=node_fd,
                                                                  follow_symlinks=False).st_mode)
                except (OSError, NotImplementedError):
                    pass
                found.append(projection)
            except FileNotFoundError:
                continue
            except (OSError, _Skip) as exc:
                reader.warn(f'{label}/nodes/{name}/node.json', _why(exc))
            finally:
                os.close(node_fd)
    except OSError as exc:
        reader.warn(f'{label}/nodes', _why(exc))
    finally:
        os.close(nodes_fd)
    return found


def _tree(nodes, reserved):
    """Nodes in tree order (each parent before its children), reserved children included, with depths."""
    by_id = {node['id']: node for node in nodes}
    for parent, rows in reserved:
        for row in rows:
            if row['name'] not in by_id:
                by_id[row['name']] = _reserved(row['name'], parent, row)
    children = {}
    for node in by_id.values():
        children.setdefault(node['parent'] if node['parent'] in by_id else None, []).append(node)
    ordered, seen = [], set()

    def visit(node, depth):
        if node['id'] in seen:
            return
        seen.add(node['id'])
        node['depth'] = depth
        ordered.append(node)
        for child in sorted(children.get(node['id'], []), key=lambda item: item['id']):
            visit(child, depth + 1)
    for root in sorted(children.get(None, []), key=lambda item: item['id']):
        visit(root, 0)
    for node in sorted(by_id.values(), key=lambda item: item['id']):  # a parent cycle has no root
        visit(node, 0)
    return ordered


def _all_nodes(reader, fd, label, where, truncated):
    nodes = _nodes(reader, fd, label, where, truncated)
    reserved = []
    for node in nodes:
        reserved.append((node['id'], node.pop('_admitted', [])))
    return _tree(nodes, reserved)


def task(reader, key, ident, target):
    """TaskFractal of one forge task; `target` is the realpath forge handed Fractal as --run-dir."""
    result, fd = _open(reader, key, ident, target)
    if fd is None:
        return result
    label, where = 'fractal ' + result['fractal_task'], _where(result['fractal_run'], result['fractal_task'])
    try:
        state = _object_file(reader, fd, label, where, 'state.json')
        outcome = _object_file(reader, fd, label, where, 'outcome.json')
        result['state'] = _state(state) if state is not None else None
        result['outcome'] = None if outcome is None else dict(
            acceptance=_word(outcome.get('acceptance'), 64), verification=_word(outcome.get('verification'), 64),
            task_status=_word(outcome.get('task_status'), 64))
        result['nodes'] = _all_nodes(reader, fd, label, where, result['truncated'])
        try:
            events, bad = _read(fd, 'events.jsonl', _LIMITS['events.jsonl'], None, _events, over='tail')
            result['events'] = events
            if bad:
                reader.warn(f'{label}/events.jsonl', f'{bad} lines are not JSON objects')
        except FileNotFoundError:
            pass
        except (OSError, _Skip) as exc:
            reader.warn(f'{label}/events.jsonl', _why(exc))
    finally:
        os.close(fd)
    return result


def _node_fd(fd, name):
    nodes_fd = _subdir(fd, 'nodes')
    try:
        return _subdir(nodes_fd, name)
    finally:
        os.close(nodes_fd)


def node(reader, key, ident, target, name):
    """NodeDetail of one node (reserved ones too), or None when there is no such node."""
    if not _NAME.fullmatch(name or ''):
        return None
    result, fd = _open(reader, key, ident, target)
    if fd is None:
        return None
    label, where = 'fractal ' + result['fractal_task'], _where(result['fractal_run'], result['fractal_task'])
    try:
        listed = next((item for item in _all_nodes(reader, fd, label, where, []) if item['id'] == name), None)
        if listed is None:
            return None
        extra = dict(goal=listed['goal'], last=None, planning_last=None, error=listed['error'], decomposition=None,
                     planning_tokens=None, decomposition_history=[], truncated=[])
        if not listed['reserved']:
            try:
                node_fd = _node_fd(fd, name)
                try:
                    extra = _read(node_fd, 'node.json', _LIMITS['node.json'],
                                  'detail:' + os.path.join(where, 'nodes', name, 'node.json'),
                                  lambda raw, *_: _detail(_object(raw)))
                finally:
                    os.close(node_fd)
            except (OSError, _Skip) as exc:
                reader.warn(f'{label}/nodes/{name}/node.json', _why(exc))
                return None
    finally:
        os.close(fd)
    detail = dict(listed, **extra)
    detail.update(run=key, task=ident, fractal_run=result['fractal_run'], fractal_task=result['fractal_task'],
                  warnings=reader.warnings, truncated=list(extra['truncated']))
    return detail


def node_diff(reader, key, ident, target, name):
    """DiffDetail of a node's candidate.diff, or None when there is no such node."""
    if not _NAME.fullmatch(name or ''):
        return None
    result, fd = _open(reader, key, ident, target)
    if fd is None:
        return None
    label, where = 'fractal ' + result['fractal_task'], _where(result['fractal_run'], result['fractal_task'])
    loaded = reason = None
    try:
        if not any(item['id'] == name for item in _all_nodes(reader, fd, label, where, [])):
            return None
        try:
            node_fd = _node_fd(fd, name)
            try:
                loaded = _read(node_fd, 'candidate.diff', diffs.MAX_BYTES, None, lambda *read: read, 'head')
            finally:
                os.close(node_fd)
        except FileNotFoundError:
            reason = 'no candidate diff yet'
        except (OSError, _Skip) as exc:
            reason = 'unreadable (see warnings)'
            reader.warn(f'{label}/nodes/{name}/candidate.diff', _why(exc))
    finally:
        os.close(fd)
    parsed = None
    if loaded is not None:
        raw, cut, size = loaded
        if cut and b'\n' in raw:
            raw = raw[:raw.rindex(b'\n') + 1]
        parsed = diffs.parsed(raw.decode('utf-8', 'replace'), cut, size)
    rel = f'{result["fractal_task"]}/nodes/{name}/candidate.diff'
    return dict(run=key, source=dict(kind='fractal', id=name, which='candidate', execution=None, attempt=None, file=rel),
                sources=[dict(id='candidate', label='candidate.diff', bytes=parsed['bytes'])] if parsed else [],
                available=parsed is not None, reason=reason if parsed is None else None,
                bytes=parsed['bytes'] if parsed else None, truncated=bool(parsed and parsed['truncated']),
                files=parsed['files'] if parsed else [], findings=[], warnings=reader.warnings, truncated_fields=[])
