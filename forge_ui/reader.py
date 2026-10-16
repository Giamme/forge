"""Read live forge plan directories without modifying them."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat


# Waves are returned as a dense, indexed list, including empty waves. Bound
# that expansion so a corrupt integer cannot allocate an arbitrary-sized list.
_MAX_WAVE = 10000


def run_key(path) -> str:
    return hashlib.sha1(os.fsencode(os.path.realpath(path))).hexdigest()[:12]


def _is_plan(path):
    try:
        # Check the directory first: stale registry entries cost just one stat.
        return stat.S_ISDIR(path.stat().st_mode) and (path / 'tasks.tsv').is_file()
    except (OSError, ValueError):
        return False


def discover(plans=(), roots=(), registry=True, registry_dir=None):
    found, seen = [], set()

    def add(path, source):
        path = Path(path)
        if not _is_plan(path):
            return False
        real = Path(os.path.realpath(path))
        if real not in seen:
            seen.add(real)
            found.append((real, source))
        return True

    def scan(path, depth=0):
        if add(path, 'root') or depth == 2:
            return
        try:
            children = sorted(path.iterdir())
        except (OSError, ValueError):
            return
        for child in children:
            try:
                if not child.name.startswith('.') and not child.is_symlink() and child.is_dir():
                    scan(child, depth + 1)
            except (OSError, ValueError):
                continue

    for path in plans:
        add(path, 'plan')
    for path in roots:
        scan(Path(path))
    if registry:
        base = Path(registry_dir) if registry_dir is not None else Path(
            os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'forge' / 'runs'
        try:
            entries = sorted(base.glob('*.txt'))
        except OSError:
            entries = []
        for entry in entries:
            try:
                with entry.open(encoding='utf-8', errors='replace') as stream:
                    for line in stream:
                        line = line.strip()
                        if line and not line.startswith('#') and os.path.isabs(line):
                            add(line, 'registry')
            except (OSError, ValueError):
                continue
    return found


class _Read:
    def __init__(self, path):
        self.path = Path(os.path.realpath(path))
        self.warnings = []
        self.truncated = []
        self.present_files = set()
        self.updated = None
        self.exists('')

    def warn(self, name, reason):
        self.warnings.append(f'{name}: {reason}')

    def track(self, info):
        value = float(info.st_mtime)
        if self.updated is None or value > self.updated:
            self.updated = value

    def exists(self, name):
        try:
            self.track((self.path / name).stat())
            return True
        except FileNotFoundError:
            if os.path.lexists(self.path / name):
                self.warn(name, 'dangling symlink')
        except (OSError, ValueError) as exc:
            self.warn(name, f'cannot stat ({type(exc).__name__})')
        return False

    def text(self, name, last_bytes=None):
        try:
            path = self.path / name
            # Do not open special files (a FIFO could otherwise block forever).
            info = path.stat()
            self.present_files.add(name)
            if not stat.S_ISREG(info.st_mode):
                self.warn(name, 'not a regular file')
                return None
            with path.open('rb') as stream:
                info = os.fstat(stream.fileno())
                self.track(info)
                if not stat.S_ISREG(info.st_mode):
                    self.warn(name, 'not a regular file')
                    return None
                if last_bytes is not None:
                    stream.seek(max(0, info.st_size - last_bytes))
                offset = stream.tell()
                raw = stream.read(last_bytes if last_bytes is not None else -1)
                if offset:
                    # A byte-limited tail may begin inside a UTF-8 character.
                    start = 0
                    while start < min(3, len(raw)) and raw[start] & 0xC0 == 0x80:
                        start += 1
                    raw = raw[start:]
                value = raw.decode('utf-8', errors='replace')
            invalid_encoding = False
            if '\ufffd' in value:
                try:
                    raw.decode('utf-8')
                except UnicodeDecodeError:
                    invalid_encoding = True
            if invalid_encoding or '\x00' in value:
                self.warn(name, 'invalid text encoding or NUL byte')
            return value
        except FileNotFoundError:
            if os.path.lexists(self.path / name):
                self.warn(name, 'dangling symlink')
        except (OSError, ValueError) as exc:
            self.warn(name, f'cannot read ({type(exc).__name__})')
        return None

    def word(self, name):
        value = self.text(name)
        return value.split()[0] if value and value.split() else None

    def line(self, name):
        value = self.text(name)
        return (value.splitlines()[0].strip() or None) if value else None

    def number(self, name, kind=float):
        value = self.text(name)
        if value is None:
            return None
        try:
            result = kind(value.strip())
            if not math.isfinite(result):
                raise ValueError()
            return result
        except (ValueError, OverflowError):
            self.warn(name, 'not a number' if kind is float else 'not an integer')
            return None

    def rows(self, name, columns):
        value = self.text(name)
        for n, line in enumerate((value or '').splitlines(), 1):
            if not line.strip() or line.startswith('#'):
                continue
            parts = line.split('\t')
            label = f'{name} line {n}'
            if len(parts) < columns:
                self.warn(label, f'expected at least {columns} columns')
            else:
                yield label, parts

    def object(self, name, value=None):
        if value is None:
            value = self.text(name)
        if value is None:
            return None
        try:
            def invalid_constant(value):
                raise ValueError('non-finite JSON constant')

            obj = json.loads(value, parse_constant=invalid_constant)
            if not isinstance(obj, dict):
                raise ValueError()
            # Huge JSON exponents can decode to infinity despite parse_constant.
            json.dumps(obj, allow_nan=False)
            return obj
        except (ValueError, RecursionError, OverflowError):
            self.warn(name, 'not a JSON object')
            return None

    def cap(self, value, field, size=20000, tail=False):
        if value is not None and len(value) > size:
            self.truncated.append(field)
            return '…' + value[-size:] if tail else value[:size] + '…'
        return value

    def children(self, name):
        try:
            return sorted((self.path / name).iterdir())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            self.warn(name, f'cannot list ({type(exc).__name__})')
            return []


def _list(value):
    return [part.strip() for part in value.split(',') if part.strip() not in ('', '-')]


def _nullable(value):
    return None if value in ('', '-', 'UNASSIGNED') else value


def _verdict(value):
    matches = re.findall(r'^FORGE_VERDICT:[ \t]*(PASS|FAIL)[ \t]*(?:[—–-][^\r\n]*)?\r?$',
                         value or '', re.MULTILINE)
    return matches[-1] if matches else None


def _tasks(reader):
    tasks = []
    for label, row in reader.rows('tasks.tsv', 7):
        ident, deps, difficulty, files, dwarf, qa = row[:6]
        if not ident or ident in ('.', '..') or '/' in ident or '\x00' in ident or '\ufffd' in ident:
            reader.warn(label, 'invalid task id')
            continue
        prefix = f'tasks/{ident}'
        status = 'MERGED' if reader.exists(prefix + '/merged') else reader.word(prefix + '/status') or 'PENDING'
        tasks.append(dict(id=ident, title='\t'.join(row[6:]), deps=_list(deps),
                          difficulty=difficulty, files=_list(files), dwarf=_nullable(dwarf),
                          qa=_nullable(qa), status=status))
    return tasks


def _summary(reader, source):
    result = dict(key=run_key(reader.path), path=str(reader.path), source=source,
                  run_id=reader.line('run_id'), repo=reader.line('repo'),
                  goal=reader.line('goal.txt'), decompose_level=reader.word('decompose_level'),
                  verification_status=reader.word('verification.status'))
    tasks = _tasks(reader)
    counts = {}
    for task in tasks:
        counts[task['status']] = counts.get(task['status'], 0) + 1
    result.update(updated=reader.updated, task_count=len(tasks), counts=counts)
    return result, tasks


def summarize(path, source='plan') -> dict:
    return _summary(_Read(path), source)[0]


def _history(reader, name, escalation=False):
    result = []
    for label, row in reader.rows(name, 4 if escalation else 3):
        try:
            at, attempt = float(row[0]), int(row[1])
            if not math.isfinite(at):
                raise ValueError()
        except (ValueError, OverflowError):
            reader.warn(label, 'invalid epoch or attempt')
            continue
        entry = dict(at=at, attempt=attempt)
        if escalation:
            entry.update({'from': row[2], 'to': row[3]})
        else:
            entry.update(status=row[2], infra=_nullable(row[3]) if len(row) > 3 else None)
        result.append(entry)
    return result


def _task_detail(reader, task, run_id, waves, branches):
    ident = task['id']
    prefix = f'tasks/{ident}'
    task.update(wave=waves.get(ident), branch=branches.get(ident))
    if not task['branch'] and run_id:
        try:
            if (reader.path / prefix).is_dir():
                task['branch'] = f'forge/{run_id}/{ident}'
        except (OSError, ValueError):
            pass
    task['attempts'] = _history(reader, prefix + '/attempts.tsv')
    task['attempt'] = reader.number(prefix + '/attempt', int)
    if task['attempt'] is None:
        task['attempt'] = max((item['attempt'] for item in task['attempts']), default=None)
    task['escalations'] = _history(reader, prefix + '/escalations.tsv', True)
    qa = reader.text(prefix + '/qa.last')
    task['verdict'] = _verdict(qa)
    task['qa_tail'] = reader.cap(qa, prefix + '/qa_tail', 4000, True)
    task['accepted_by'] = reader.text(prefix + '/accepted.by')
    for field, filename in [('prompt', 'prompt.md'), ('approach', 'approach.md'), ('retry_findings', 'retry_findings.md')]:
        task[field] = reader.cap(reader.text(prefix + '/' + filename), prefix + '/' + field)
    task['route'] = []
    for n, line in enumerate((reader.text(prefix + '/route.jsonl') or '').splitlines(), 1):
        if line.strip():
            obj = reader.object(f'{prefix}/route.jsonl line {n}', line)
            if obj is not None:
                task['route'].append(obj)
    task['explored'] = reader.exists(prefix + '/route.explored') or any(
        item.get('explored') is True for item in task['route'])
    task['clef'] = {'first_pass': reader.object(prefix + '/clef.first_pass'), 'retry_pass': {}}
    for child in reader.children(prefix):
        if child.name.startswith('clef.retry_pass.'):
            suffix = child.name[len('clef.retry_pass.'):]
            if not re.fullmatch(r'[0-9]+', suffix):
                reader.warn(prefix + '/' + child.name, 'invalid attempt suffix')
                continue
            obj = reader.object(prefix + '/' + child.name)
            if obj is not None:
                task['clef']['retry_pass'][suffix] = obj
    task['shadow'] = []
    for child in reader.children(f'shadow/{ident}'):
        match = re.fullmatch(r'([0-9]+)-([0-9]+)', child.name)
        if not match:
            continue
        try:
            if not child.is_dir():
                continue
            attempt, k = map(int, match.groups())
        except (OSError, ValueError):
            continue
        sub = f'shadow/{ident}/{child.name}'
        obj = reader.object(sub + '/shadow.json') or {}
        task['shadow'].append(dict(attempt=attempt, k=k, spec=obj.get('spec'),
                                   primary=obj.get('primary'), started=obj.get('started'),
                                   status=reader.word(sub + '/shadow.status'),
                                   verdict=_verdict(reader.text(sub + '/qa.last'))))
    task['shadow'].sort(key=lambda item: (item['attempt'], item['k']))


def detail(path, source='plan') -> dict:
    reader = _Read(path)
    result, tasks = _summary(reader, source)
    result['request'] = reader.cap(reader.text('request.md'), 'request')
    result['settings'] = dict(explore=reader.number('explore'), shadow_rate=reader.number('shadow_rate'),
                              shadow_qa=_list(reader.text('shadow_qa') or ''), timeout=reader.number('timeout', int))
    command = reader.text('verification.command')
    verification = dict(status=result['verification_status'], command=command.strip() if command is not None else None,
                        log_tail=reader.cap(reader.text('verification.log', 65536), 'verification.log_tail', 4000, True))
    result['verification'] = verification if any('verification.' + suffix in reader.present_files
                                               for suffix in ('status', 'command', 'log')) else None
    waves, wave_map = {}, {}
    for label, row in reader.rows('waves.tsv', 2):
        try:
            wave = int(row[0])
            if not 1 <= wave <= _MAX_WAVE:
                raise ValueError()
        except ValueError:
            reader.warn(label, f'invalid wave number (expected 1..{_MAX_WAVE})')
            continue
        waves.setdefault(wave, []).append(row[1])
        wave_map[row[1]] = wave
    result['waves'] = [waves.get(n, []) for n in range(1, max(waves, default=0) + 1)]
    branches = {row[0]: _nullable(row[2]) for _, row in reader.rows('results.tsv', 3)}
    for task in tasks:
        _task_detail(reader, task, result['run_id'], wave_map, branches)
    result.update(tasks=tasks, warnings=reader.warnings, truncated=reader.truncated, updated=reader.updated)
    return result


class Catalog:
    def __init__(self, plans=(), roots=(), registry=True, registry_dir=None):
        self.options = dict(plans=tuple(plans), roots=tuple(roots), registry=registry, registry_dir=registry_dir)

    def runs(self):
        runs = []
        for path, source in discover(**self.options):
            result = summarize(path, source)
            if _is_plan(path):
                runs.append(result)
        return sorted(runs, key=lambda item: item['updated'] if item['updated'] is not None else float('-inf'), reverse=True)

    def get(self, key):
        for path, source in discover(**self.options):
            if run_key(path) == key:
                result = detail(path, source)
                return result if _is_plan(path) else None
        return None
