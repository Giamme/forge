"""Executions: the attempts/<role>-<uuid>/ dirs forge-runtime.py writes, read without modifying them."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import math
import os
from pathlib import Path
import posixpath
import re
import threading
import time

from forge_ui import findings


NAME = re.compile(r'(task|dwarf|qa|solo|verification|parallel)-[0-9a-f]{32}')
_KIND = re.compile(r'(final-review|integrate)-([0-9]+)-([0-9]+)')
_SHADOW = re.compile(r'[0-9]+-[0-9]+')
CONTAINERS = ('task', 'solo')
_MODEL_ROLES = ('dwarf', 'qa')
# attempts.tsv is appended right after the task execution's EXIT trap ran finish().
_MATCH_S = 120
# A rewrite within one mtime tick that keeps the size keeps the signature too, so a file
# this young is parsed again on every read instead of being cached.
_SETTLE_S = 3
_REQUIRED = ('started', 'wall_started', 'phase', 'phase_started', 'elapsed_s', 'exit_code')


class _SigCache:
    """LRU of parsed values, valid while a file keeps its (dev, inode, size, mtime_ns)."""

    def __init__(self, size=8192):
        self.size = size
        self.lock = threading.Lock()
        self.items = OrderedDict()

    def get(self, key, sig):
        with self.lock:
            item = self.items.get(key)
            if item is None or item[0] != sig:
                return None
            self.items.move_to_end(key)
            return item[1]

    def put(self, key, sig, value):
        with self.lock:
            self.items[key] = (sig, value)
            self.items.move_to_end(key)
            while len(self.items) > self.size:
                self.items.popitem(last=False)


_CACHE = _SigCache()


class Execution(dict):
    """An Execution as the API returns it. `dataset` and `rel` (its dir) stay out of the JSON."""
    __slots__ = ('dataset', 'rel')


def inside(root, path) -> bool:
    try:
        return os.path.commonpath([str(root), str(path)]) == str(root)
    except ValueError:
        return False


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _cached(reader, name, kind, parse):
    """parse(text) of one file through the cache; a hit replays the warnings the parse gave."""
    path = reader.path / name
    try:
        info = path.stat()
    except FileNotFoundError:
        if os.path.lexists(path):
            reader.warn(name, 'dangling symlink')
        return None
    except (OSError, ValueError) as exc:
        reader.warn(name, f'cannot stat ({type(exc).__name__})')
        return None
    key, sig = (kind, str(path)), _signature(info)
    hit = _CACHE.get(key, sig)
    if hit is not None:
        reader.present_files.add(name)
        reader.track(info)
        for reason in hit[1]:
            reader.warn(name, reason)
        return hit[0]
    mark = len(reader.warnings)
    text = reader.text(name)
    value = None if text is None else parse(text)
    new = reader.warnings[mark:]
    prefix = name + ': '
    if text is not None and all(item.startswith(prefix) for item in new) and abs(time.time() - info.st_mtime) > _SETTLE_S:
        try:
            if _signature(path.stat()) == sig:
                _CACHE.put(key, sig, (value, [item[len(prefix):] for item in new]))
        except (OSError, ValueError):
            pass
    return value


def summary_of(text):
    """FindingsSummary of a reply, memoised by content (live files are re-read, not re-parsed)."""
    if not text or not findings.available():
        return None
    key = ('summary', hashlib.sha1(text.encode('utf-8', 'surrogatepass')).hexdigest())
    hit = _CACHE.get(key, 0)
    if hit is None:
        hit = (findings.summarize(text),)
        if hit[0] is not None:
            _CACHE.put(key, 0, hit)
    return dict(hit[0]) if hit[0] is not None else None


def parse_resolved(text) -> dict:
    result = {}
    for line in (text or '').splitlines():
        key, sep, value = line.partition('=')
        if sep and key.strip():
            result[key.strip()] = value.strip()
    return result


def spec_of(resolved):
    alias, effort = resolved.get('alias') or None, resolved.get('effort') or None
    if not alias:
        return None
    # `<harness default>` is the dispatch placeholder, not an effort a caller can select.
    if effort and effort != '<harness default>':
        return alias + ':' + effort
    return alias


def _model(resolved):
    if not resolved or not resolved.get('alias'):
        return None
    effort = resolved.get('effort') or None
    return dict(spec=spec_of(resolved), alias=resolved['alias'], harness=resolved.get('harness') or None,
                model=resolved.get('model') or None,
                effort=None if effort == '<harness default>' else effort,
                clamped=resolved.get('clamped') or None)


def _finite(value):
    return value if value is not None and math.isfinite(value) else None


def _metrics(reader, name, text):
    obj = reader.object(name, text)
    if obj is None:
        return None
    for key in _REQUIRED:
        if key not in obj:
            reader.warn(name, f'missing {key}')

    def number(key, value, kinds=(int, float)):
        if value is None:
            return None
        if type(value) in kinds:  # bool is not a number here
            if kinds == (int,):
                return value
            try:
                return float(value)
            except OverflowError:
                pass
        reader.warn(name, f'{key} is not ' + ('an integer' if kinds == (int,) else 'a number'))
        return None

    wall = number('wall_started', obj.get('wall_started'))
    mono = number('started', obj.get('started'))
    mark = number('phase_started', obj.get('phase_started'))
    exit_code = number('exit_code', obj.get('exit_code'), (int,))
    phases, total = {}, None
    elapsed = obj.get('elapsed_s')
    if isinstance(elapsed, dict):
        for key, value in elapsed.items():
            value = number('elapsed_s.' + key, value)
            if value is None:
                continue
            if key == 'total':
                total = value
            else:
                phases[key] = value
    elif elapsed is not None:
        reader.warn(name, 'elapsed_s is not an object')
    phase = obj.get('phase')
    if phase is not None and not isinstance(phase, str):
        reader.warn(name, 'phase is not a string')
        phase = None
    tokens = {short: number(key, obj.get(key), (int,)) for short, key in (
        ('input', 'input_tokens'), ('cached', 'cached_input_tokens'), ('output', 'output_tokens'))}
    infra = obj.get('infra_class')
    done = exit_code is not None
    return dict(started=wall, ended=_finite(wall + total) if done and None not in (wall, total) else None,
                duration_s=total if done else None, phase=phase,
                phase_started=_finite(wall + (mark - mono)) if None not in (wall, mark, mono) else None,
                phases=phases, exit_code=exit_code, infra_class=infra if isinstance(infra, str) and infra else None,
                tokens=tokens if any(value is not None for value in tokens.values()) else None,
                prompt_bytes=number('prompt_bytes', obj.get('prompt_bytes'), (int,)), shadow=obj.get('shadow') is True)


def _dataset(reader, name, text):
    obj = reader.object(name, text)
    if obj is None:
        return None

    def word(key):
        value = obj.get(key)
        return value if isinstance(value, str) and value else None

    start = obj.get('attempt_start')
    return dict(trigger=word('trigger'), parent=word('parent'),
                attempt_start=start if type(start) is int else None, resume=word('resume'))


def qa_result(reader, name):
    """(verdict, FindingsSummary) of a reply that no longer changes once written (cached)."""
    kind = 'qa' if findings.available() else 'qa-verdict'
    verdict, summary = _cached(reader, name, kind,
                               lambda text: (findings.verdict(text), findings.summarize(text))) or (None, None)
    return verdict, dict(summary) if summary is not None else None


def resolved_of(reader, name):
    return _cached(reader, name, 'resolved', parse_resolved)


def real_dir(reader, rel):
    """realpath of a dir under the run, or None when it resolves outside it."""
    try:
        real = os.path.realpath(reader.path / rel)
    except (OSError, ValueError):
        return None
    if not inside(reader.path, real):
        if os.path.isdir(real):
            reader.warn(rel, 'outside the run dir')
        return None
    return real


def scan(reader, rel_dir, owner_live, roles=None):
    """Every execution in <rel_dir>, ordered by start (unknown last), then id."""
    base = real_dir(reader, rel_dir)
    if base is None:
        return []
    try:
        with os.scandir(base) as stream:
            entries = sorted((entry for entry in stream if NAME.fullmatch(entry.name)), key=lambda entry: entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except (OSError, ValueError) as exc:
        reader.warn(rel_dir, f'cannot list ({type(exc).__name__})')
        return []
    result = []
    for entry in entries:
        role = entry.name.split('-', 1)[0]
        rel = f'{rel_dir}/{entry.name}'
        try:
            if (roles and role not in roles) or not entry.is_dir():
                continue
            if entry.is_symlink() and not inside(reader.path, os.path.realpath(entry.path)):
                reader.warn(rel, 'outside the run dir')
                continue
        except (OSError, ValueError):
            continue
        result.append(_build(reader, rel, entry.name, role))
    result.sort(key=lambda item: (item['started'] is None, item['started'] or 0.0, item['id']))
    newest = {item['role']: item['id'] for item in result}
    for item in result:
        if item['exit_code'] is not None:
            item['state'] = 'done'
        else:
            live = owner_live(item['role']) if callable(owner_live) else owner_live
            # Only the newest execution of a role can still be in flight: dispatches of one
            # role into one dir run one after another.
            item['state'] = 'running' if live and newest[item['role']] == item['id'] else 'orphaned'
    owner = posixpath.dirname(rel_dir)
    for item in result:
        if item['role'] in _MODEL_ROLES and item['state'] == 'running' and item['model'] is None:
            live = parse_resolved(reader.text(posixpath.join(owner, item['role'] + '.resolved')))
            item['model'] = _model(live)
        if item['role'] == 'qa' and item['state'] == 'done':
            item['verdict'], item['findings'] = qa_result(reader, item.rel + '/qa.last')
    return result


def _build(reader, rel, name, role):
    metrics_name, dataset_name = rel + '/metrics.json', rel + '/dataset.json'
    metrics = _cached(reader, metrics_name, 'metrics', lambda text: _metrics(reader, metrics_name, text)) or {}
    dataset = _cached(reader, dataset_name, 'dataset', lambda text: _dataset(reader, dataset_name, text))
    model = timed_out = None
    if role in _MODEL_ROLES:
        model = _model(resolved_of(reader, f'{rel}/{role}.resolved'))
        timed_out = reader.exists(f'{rel}/{role}.timedout')
    item = Execution(id=name, role=role, parent=None, attempt=None, shadow=bool(metrics.get('shadow')),
                     state=None, started=metrics.get('started'), ended=metrics.get('ended'),
                     duration_s=metrics.get('duration_s'), phase=metrics.get('phase'),
                     phase_started=metrics.get('phase_started'), phases=dict(metrics.get('phases') or {}),
                     exit_code=metrics.get('exit_code'), infra_class=metrics.get('infra_class'),
                     timed_out=bool(timed_out), model=model,
                     tokens=dict(metrics['tokens']) if metrics.get('tokens') else None,
                     prompt_bytes=metrics.get('prompt_bytes'), verdict=None, findings=None)
    item.dataset, item.rel = dataset, rel
    return item


def nest(execs, containers=None, adopt_state=True):
    """Give each dwarf/qa execution its parent task (or solo) execution.

    The parent dataset.json names wins; otherwise the latest container whose run contains
    this execution's start. A child cannot run on after its parent stopped.
    """
    pool = [item for item in (execs if containers is None else containers) if item['role'] in CONTAINERS]
    by_id = {item['id']: item for item in pool}
    for item in execs:
        if item['role'] in CONTAINERS:
            continue
        parent = by_id.get((item.dataset or {}).get('parent'))
        if parent is None and item['started'] is not None:
            for candidate in pool:
                if (candidate['started'] is not None and candidate['started'] <= item['started']
                        and (candidate['ended'] is None or item['started'] <= candidate['ended'])):
                    parent = candidate
        item['parent'] = parent['id'] if parent is not None else None
        if adopt_state and parent is not None and item['state'] == 'running' and parent['state'] != 'running':
            item['state'] = 'orphaned'
    return execs


def assign_attempts(execs, attempt_rows, current=None):
    """Attempt numbers: attempts.tsv rows by end time, then dataset.json, then `current` while running."""
    pool = [item for item in execs if item['role'] in CONTAINERS]
    pairs = sorted((abs(item['ended'] - row['at']), i, j)
                   for i, item in enumerate(pool) if item['ended'] is not None
                   for j, row in enumerate(attempt_rows) if abs(item['ended'] - row['at']) <= _MATCH_S)
    taken, used = set(), set()
    for _, i, j in pairs:
        if i not in taken and j not in used:
            taken.add(i)
            used.add(j)
            pool[i]['attempt'] = attempt_rows[j]['attempt']
    for i, item in enumerate(pool):
        if i in taken:
            continue
        start = (item.dataset or {}).get('attempt_start')
        if start is not None:
            # begin() runs before the counter moves; a QA-only resume does not move it.
            item['attempt'] = start if (item.dataset or {}).get('resume') else start + 1
        elif item['state'] == 'running':
            item['attempt'] = current
    by_id = {item['id']: item for item in pool}
    for item in execs:
        if item['role'] not in CONTAINERS:
            parent = by_id.get(item['parent'])
            item['attempt'] = parent['attempt'] if parent is not None else item['attempt']
    return execs


def review_dirs(reader):
    """(name, source, at, rel) of every final review, newest first: final-review-<e>-<p>/ and
    integrate-<e>-<p>/final-review/. Also every integrate-<e>-<p>/, newest first, as
    (name, at, its review's tuple or None)."""
    try:
        with os.scandir(reader.path) as stream:
            names = sorted(entry.name for entry in stream if _KIND.fullmatch(entry.name))
    except (OSError, ValueError) as exc:
        reader.warn('.', f'cannot list ({type(exc).__name__})')
        return [], []
    reviews, integrations = [], []
    for name in names:
        kind, epoch, pid = _KIND.fullmatch(name).groups()
        at = float(epoch)
        if not math.isfinite(at) or len(epoch) > 15:
            reader.warn(name, 'invalid epoch')
            continue
        real = real_dir(reader, name)
        if real is None or not os.path.isdir(real):
            continue
        order = (at, int(pid), name)
        if kind == 'final-review':
            reviews.append((order, (name, 'review', at, name)))
            continue
        sub = real_dir(reader, name + '/final-review')
        review = (name, 'integrate', at, name + '/final-review') if sub is not None and os.path.isdir(sub) else None
        integrations.append((order, (name, at, review)))
        if review is not None:
            reviews.append((order, review))
    reviews.sort(key=lambda item: item[0], reverse=True)
    integrations.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in reviews], [item for _, item in integrations]


def locate(run_path, name, task_ids):
    """Path (relative to the run dir) of execution `name`'s dir, or None.

    Only the dirs executions are written to are looked at, never the whole tree, and a dir
    that resolves outside the run (a symlinked attempts/, say) is never returned.
    """
    if not NAME.fullmatch(name or ''):
        return None
    root = os.path.realpath(run_path)

    def listed(rel, pattern):
        try:
            return sorted(child for child in os.listdir(os.path.join(root, rel)) if pattern.fullmatch(child))
        except (OSError, ValueError):
            return []

    def candidates():
        yield 'attempts'
        for ident in task_ids:
            yield f'tasks/{ident}/attempts'
        for ident in task_ids:
            for child in listed(f'shadow/{ident}', _SHADOW):
                yield f'shadow/{ident}/{child}/attempts'
        kinds = listed('', _KIND)
        for child in kinds:
            if child.startswith('final-review-'):
                yield child + '/attempts'
        for child in kinds:
            if child.startswith('integrate-'):
                yield child + '/final-review/attempts'

    for rel in candidates():
        try:
            real = os.path.realpath(os.path.join(root, rel, name))
            if inside(root, real) and os.path.isdir(real):
                return Path(rel) / name
        except (OSError, ValueError):
            continue
    return None
