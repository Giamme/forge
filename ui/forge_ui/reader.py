"""Read live forge plan and solo run directories without modifying them."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import posixpath
import re
import stat
import time
import urllib.parse

from forge_ui import checks, diffs, executions, findings, fractal, transcripts
from forge_ui.findings import verdict as _verdict


# Waves are returned as a dense, indexed list, including empty waves. Bound
# that expansion so a corrupt integer cannot allocate an arbitrary-sized list.
_MAX_WAVE = 10000
# RunDetail lists the newest final reviews only; older ones stay reachable by name.
_MAX_REVIEWS = 20


def run_key(path) -> str:
    return hashlib.sha1(os.fsencode(os.path.realpath(path))).hexdigest()[:12]


def _kind(path):
    try:
        # Check the directory first: stale registry entries cost just one stat.
        path = Path(path)
        if not stat.S_ISDIR(path.stat().st_mode):
            return None
        if (path / 'tasks.tsv').is_file():
            return 'plan'
        # Both markers are required, so a notes directory that only has prompt.md is not a run.
        if (path / 'prompt.md').is_file() and (path / 'repo').is_file():
            return 'solo'
    except (OSError, ValueError):
        return None
    return None


def _default_registry_dir():
    """forge's run registry, resolved the way forge writes it (scripts/forge_registry.registry_dir).

    ``XDG_CONFIG_HOME`` (``~`` expanded) when it is an absolute path, otherwise ``~/.config``: an
    empty or relative value counts as unset (XDG spec), as it does when forge registers a run.
    None only when even that is not absolute (a relative HOME): forge never registers a run
    there, so reading it would only pick up whatever the current directory happens to hold.
    """
    value = os.environ.get('XDG_CONFIG_HOME') or ''
    base = Path(value).expanduser() if value else None
    if base is None or not base.is_absolute():
        base = Path.home() / '.config'
    return base / 'forge' / 'runs' if base.is_absolute() else None


def discover(plans=(), roots=(), registry=True, registry_dir=None):
    found, seen = [], set()

    def add(path, source):
        path = Path(path)
        kind = _kind(path)
        # `--root` stays plans-only: a tree of unrelated prompt.md files is not a run list.
        if kind is None or (source == 'root' and kind != 'plan'):
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
        base = Path(registry_dir) if registry_dir is not None else _default_registry_dir()
        try:
            entries = sorted(base.glob('*.txt')) if base is not None else []
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

    def stat(self, name):
        try:
            info = (self.path / name).stat()
            self.track(info)
            return info
        except FileNotFoundError:
            if os.path.lexists(self.path / name):
                self.warn(name, 'dangling symlink')
        except (OSError, ValueError) as exc:
            self.warn(name, f'cannot stat ({type(exc).__name__})')
        return None

    def exists(self, name):
        return self.stat(name) is not None

    def _decode(self, name, *chunks):
        values, invalid = [], False
        for raw in chunks:
            value = raw.decode('utf-8', errors='replace')
            if '�' in value:
                try:
                    raw.decode('utf-8')
                except UnicodeDecodeError:
                    invalid = True
            values.append(value)
        if invalid or any('\x00' in value for value in values):
            self.warn(name, 'invalid text encoding or NUL byte')
        return values

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
            return self._decode(name, raw)[0]
        except FileNotFoundError:
            if os.path.lexists(self.path / name):
                self.warn(name, 'dangling symlink')
        except (OSError, ValueError) as exc:
            self.warn(name, f'cannot read ({type(exc).__name__})')
        return None

    def excerpt(self, name, head, tail, field):
        """The text of a file, or its first `head` and last `tail` characters when it is longer.

        A big file costs two bounded reads, never a read of the whole file.
        """
        try:
            path = self.path / name
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
                # A UTF-8 character is at most 4 bytes, so a file this small has at most head+tail characters.
                if info.st_size <= 4 * (head + tail):
                    return self.clip(self._decode(name, stream.read(4 * (head + tail) + 4))[0], head, tail, field)
                first = stream.read(4 * head)
                stream.seek(info.st_size - 4 * tail)
                last = stream.read(4 * tail)
        except FileNotFoundError:
            if os.path.lexists(self.path / name):
                self.warn(name, 'dangling symlink')
            return None
        except (OSError, ValueError) as exc:
            self.warn(name, f'cannot read ({type(exc).__name__})')
            return None
        # Drop a character the head read cut in half, and the tail's leading continuation bytes.
        for back in range(1, min(4, len(first)) + 1):
            byte = first[-back]
            if byte & 0xC0 != 0x80:
                size = 2 if byte >> 5 == 0b110 else 3 if byte >> 4 == 0b1110 else 4 if byte >> 3 == 0b11110 else 1
                first = first[:-back] if size > back else first
                break
        start = 0
        while start < min(3, len(last)) and last[start] & 0xC0 == 0x80:
            start += 1
        first, last = self._decode(name, first, last[start:])
        self.truncated.append(field)
        return first[:head] + '\n\u2026\n' + last[-tail:]

    def clip(self, value, head, tail, field):
        if value is not None and len(value) > head + tail:
            self.truncated.append(field)
            return value[:head] + '\n\u2026\n' + value[-tail:]
        return value

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


def _tasks(reader):
    tasks = []
    for label, row in reader.rows('tasks.tsv', 7):
        ident, deps, difficulty, files, dwarf, qa = row[:6]
        if not ident or ident in ('.', '..') or '/' in ident or '\x00' in ident or '�' in ident:
            reader.warn(label, 'invalid task id')
            continue
        prefix = f'tasks/{ident}'
        # One stat: the run list needs the mtime, and detail reads the same marker again.
        merged = reader.stat(prefix + '/merged')
        status = 'MERGED' if merged is not None else reader.word(prefix + '/status') or 'PENDING'
        tasks.append(dict(id=ident, title='\t'.join(row[6:]), deps=_list(deps),
                          difficulty=difficulty, files=_list(files), dwarf=_nullable(dwarf),
                          qa=_nullable(qa), status=status,
                          merged_at=float(merged.st_mtime) if merged is not None else None,
                          accepted=_nonempty(reader, prefix + '/accepted.by')))
    return tasks


_TITLE_CAP = 160
# Files a run writes once, when it starts: their earliest time is when the run began. Stat'ed
# outside _Read.track, so they never move `updated`.
_PLAN_START_FILES = ('run_id', 'repo', 'request.md', 'goal.txt', 'tasks.tsv')
_SOLO_START_FILES = ('prompt.md', 'repo', 'request.md', 'goal.txt')


def _created(path, names):
    times = []
    for name in names:
        try:
            info = os.stat(path / name)
        except (OSError, ValueError):
            continue
        value = getattr(info, 'st_birthtime', None) or info.st_mtime
        if math.isfinite(value):
            times.append(float(value))
    return min(times) if times else None


def _brief(title):
    return title if len(title) <= _TITLE_CAP else title[:_TITLE_CAP] + '…'


def _summary(reader, source):
    result = dict(key=run_key(reader.path), path=str(reader.path), source=source, kind='plan',
                  run_id=reader.line('run_id'), repo=reader.line('repo'),
                  goal=reader.line('goal.txt'), decompose_level=reader.word('decompose_level'),
                  verification_status=reader.word('verification.status'))
    tasks = _tasks(reader)
    counts = {}
    for task in tasks:
        counts[task['status']] = counts.get(task['status'], 0) + 1
    final_review = reader.word('final-review.verdict')
    dwarf_models = sorted({task['dwarf'] for task in tasks if task['dwarf']})
    qa_models = sorted({task['qa'] for task in tasks if task['qa']})
    # Before snapshotting `updated`: these reads are files the summary read, so a newer
    # accepted.integration (or the published verdict / review.meta) moves the activity time.
    final_review_outdated = _published_outdated(reader, tasks)
    result.update(updated=reader.updated, created=_created(reader.path, _PLAN_START_FILES),
                  task_count=len(tasks), counts=counts,
                  task_statuses=[dict(id=task['id'], status=task['status'], difficulty=task['difficulty'] or None,
                                      title=_brief(task['title']), accepted=task['accepted']) for task in tasks],
                  models=sorted(set(dwarf_models) | set(qa_models)), dwarf_models=dwarf_models, qa_models=qa_models,
                  final_review=final_review, final_review_outdated=final_review_outdated)
    return result, tasks


def _resolved_spec(value):
    return executions.spec_of(executions.parse_resolved(value))


def _role_spec(reader, role):
    spec = reader.line(role + '.spec')
    if spec:
        return spec
    return _resolved_spec(reader.text(role + '.resolved'))


def _solo_status(reader):
    return reader.word('status') or reader.word('verdict') or 'UNKNOWN'


def _solo_summary(reader, source):
    status = _solo_status(reader)
    # Stat the live logs without reading them, so `updated` moves while a model writes.
    reader.exists('dwarf.log')
    reader.exists('qa.log')
    dwarf, qa = _role_spec(reader, 'dwarf'), _role_spec(reader, 'qa')
    goal = reader.line('goal.txt')
    return dict(key=run_key(reader.path), path=str(reader.path), source=source, kind='solo',
                run_id=reader.path.name, repo=reader.line('repo'), goal=goal,
                decompose_level=None, verification_status=None, updated=reader.updated,
                created=_created(reader.path, _SOLO_START_FILES),
                task_count=1, counts={status: 1},
                task_statuses=[dict(id='solo', status=status, difficulty=None, title=_brief(goal or 'Solo run'),
                                    accepted=False)],
                models=sorted({dwarf, qa} - {None}), dwarf_models=[dwarf] if dwarf else [],
                qa_models=[qa] if qa else [], final_review=None, final_review_outdated=None)


def summarize(path, source='plan') -> dict:
    reader = _Read(path)
    if _kind(reader.path) == 'solo':
        return _solo_summary(reader, source)
    return _summary(reader, source)[0]


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
            # the 5th column says who decided (policy, a human's retry --dwarf, a pin); older rows have 4
            source = row[4].strip() if len(row) > 4 else ''
            entry.update({'from': row[2], 'to': row[3], 'source': source or 'policy'})
        else:
            entry.update(status=row[2], infra=_nullable(row[3]) if len(row) > 3 else None)
        result.append(entry)
    return result


def _attempt_state(reader, prefix):
    attempts = _history(reader, prefix + '/attempts.tsv')
    attempt = reader.number(prefix + '/attempt', int)
    if attempt is None:
        attempt = max((item['attempt'] for item in attempts), default=None)
    return attempts, attempt


def _gate(reader, name):
    text = reader.text(name)
    gate = findings.parse_gate(text)
    if text is not None and gate is None:
        reader.warn(name, 'not a gate line')
    return gate


def _nonempty(reader, name):
    info = reader.stat(name)
    return info is not None and stat.S_ISREG(info.st_mode) and info.st_size > 0


def _task_execs(reader, task):
    execs = executions.scan(reader, f'tasks/{task["id"]}/attempts', task['status'] == 'RUNNING', ('task', 'dwarf', 'qa'))
    executions.nest(execs)
    return executions.assign_attempts(execs, task['attempts'], task['attempt'])


def _solo_execs(reader, status, attempts):
    execs = executions.scan(reader, 'attempts', status == 'RUNNING', ('solo', 'dwarf', 'qa'))
    executions.nest(execs)
    # A solo run appends its attempts.tsv row on exit, so the running attempt is the next one.
    return executions.assign_attempts(execs, attempts, len(attempts) + 1)


def _run_execs(reader):
    return executions.scan(reader, 'attempts', True, ('parallel', 'verification'))


def _shadows(reader, ident, task_execs):
    """(entry, executions, dir) for every shadow/<id>/<attempt>-<k>/, sorted by attempt then k."""
    result = []
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
        status = reader.word(sub + '/shadow.status')
        # shadow.status is written when the shadow ends, whatever the outcome.
        execs = executions.scan(reader, sub + '/attempts', status is None, ('qa',))
        # A shadow may outlive the task execution that started it, so its state stays its own.
        executions.nest(execs, task_execs, adopt_state=False)
        for item in execs:
            item['attempt'] = attempt
        run = execs[-1] if execs else None
        verdict, summary = executions.qa_result(reader, sub + '/qa.last')
        primaries = [item for item in task_execs if item['role'] == 'qa' and not item['shadow'] and item['attempt'] == attempt]
        primary = primaries[-1]['verdict'] if primaries else None
        agreement = 'unknown'
        if verdict in ('PASS', 'FAIL') and primary in ('PASS', 'FAIL'):
            agreement = 'agree' if verdict == primary else 'disagree'
        started = obj.get('started')
        if started is None and run is not None:
            started = run['started']
        entry = dict(attempt=attempt, k=k, spec=obj.get('spec'), primary=obj.get('primary'), started=started,
                     status=status, verdict=verdict, reviewed=obj.get('reviewed'), pick=obj.get('pick'),
                     execution=run['id'] if run else None, findings=summary, primary_verdict=primary,
                     agreement=agreement, ended=run['ended'] if run else None,
                     duration_s=run['duration_s'] if run else None)
        result.append((entry, execs, sub))
    result.sort(key=lambda item: (item[0]['attempt'], item[0]['k']))
    return result


def _accepted_fields(reader, name, text):
    """(attempt, over) from an accepted.by body. A non-integer attempt= is null and warned.

    status= FAIL or UNKNOWN is what was accepted over; any other or missing value is null
    (a PASS record is an acceptance with nothing to say beyond "accepted").
    """
    if not text:
        return None, None
    fields = executions.parse_resolved(text)
    attempt = None
    raw = fields.get('attempt')
    if raw:
        try:
            attempt = int(raw)
        except (ValueError, OverflowError):
            reader.warn(name, 'attempt is not an integer')
    status = fields.get('status')
    return attempt, status if status in ('FAIL', 'UNKNOWN') else None


def _log_path(reader, rel):
    """The regular file `rel` of the run dir as a Path, or None; a path that resolves outside the run is never read."""
    try:
        real = Path(os.path.realpath(reader.path / rel))
        if executions.inside(reader.path, str(real)) and stat.S_ISREG(real.stat().st_mode):
            return real
        if real.exists():
            reader.warn(rel, 'outside the run dir or not a regular file')
    except (OSError, ValueError):
        pass
    return None


_CALL_CAP = 400                # characters of the running command in RunDetail


def _now(reader, owner_dir, execs):
    """Task.now: where a running task (or solo run) is. The dwarf or reviewer running in it, with the call it is
    making and when its log last moved; else the task execution's own phase."""
    running = [item for item in execs if item['state'] == 'running' and not item['shadow']]
    agent = next((item for item in reversed(running) if item['role'] in ('dwarf', 'qa')), None)
    item = agent or next((item for item in reversed(running) if item['role'] in executions.CONTAINERS), None)
    if item is None:
        return None
    now = dict(role=item['role'], execution=item['id'], attempt=item['attempt'], phase=item['phase'],
               phase_started=item['phase_started'], started=item['started'], last_output_at=None, call=None)
    if agent is not None:
        rel = posixpath.join(owner_dir, agent['role'] + '.log')
        reader.stat(rel)                       # the page redraws while the agent writes
        log = _log_path(reader, rel)
        state = transcripts.live(log, (agent['model'] or {}).get('harness')) if log is not None else None
        if state is not None:
            call = state['call']
            if call is not None:
                command = call['command']
                call = dict(call, command=command if not command or len(command) <= _CALL_CAP else command[:_CALL_CAP] + '…',
                            check=checks.classify(command) if command else None)
            now.update(last_output_at=state['last_output_at'], call=call)
    return now


def _scheduler(reader):
    """RunDetail.scheduler: whether a scheduler runs this plan now, and, while one does, what holds it: a drain, a
    stop, infrastructure pauses (scheduler-written infra.paused) and the capacity it dispatches up to."""
    pid = (reader.object('scheduler.live') or {}).get('pid') if reader.exists('scheduler.live') else None
    live = False
    if type(pid) is int and pid > 0:
        try:
            os.kill(pid, 0)
            live = True
        except PermissionError:
            live = True
        except (OSError, OverflowError):
            live = False
    result = dict(live=live, drained=False, stopping=None, pauses=[], max_parallel=None)
    if not live:
        return result
    stop = reader.line('stop')
    capacity = reader.number('max_parallel', int)
    result.update(drained=reader.exists('drain'), stopping=stop if stop in ('graceful', 'now') else None,
                  max_parallel=capacity if capacity is not None and 1 <= capacity <= 64 else None)
    paused = reader.object('infra.paused') if reader.exists('infra.paused') else None
    for scope in (paused or {}).get('scopes') or []:
        if isinstance(scope, dict):
            result['pauses'].append({key: scope.get(key) for key in
                                     ('scope', 'class', 'harness', 'model', 'tasks', 'cycle', 'until', 'exhausted')})
    return result


_STOPPED = ('FAIL', 'BLOCKED', 'INFRA', 'INTERRUPTED', 'TIMEOUT', 'ERROR', 'CONFLICT', 'NOCHANGES', 'INVALIDATED', 'UNKNOWN')


def _escalation_stop(reader, ident):
    """The retry policy stopped this task: {attempts, model, split} from escalation.stop and split.suggested."""
    text = reader.text(f'tasks/{ident}/escalation.stop')
    if text is None:
        return None
    cells = (text.splitlines() or [''])[0].split('\t')
    attempts = int(cells[0]) if cells[0].strip().isdigit() else None
    split = (reader.text(f'tasks/{ident}/split.suggested') or '').strip()
    return dict(kind='escalation', attempts=attempts, model=cells[1].strip() if len(cells) > 1 and cells[1].strip() else None,
                split=reader.cap(split, f'tasks/{ident}/split.suggested', 600) or None)


def _waiting(reader, tasks, scheduler):
    """Task.waiting: why a task that is not running is not, as far as the run dir tells.

    escalation (the retry policy stopped it, whether or not a scheduler runs); and while one runs: infra (a pause
    holds it), deps (on tasks not merged yet), drained, stopping, overlap (it shares files with a running task),
    slot (the run is at its capacity) or next (nothing holds it: it starts at the scheduler's next turn)."""
    status = {task['id']: task['status'] for task in tasks}
    running = [task for task in tasks if task['status'] == 'RUNNING']
    for task in tasks:
        task['waiting'] = None
        if task['status'] in ('RUNNING', 'MERGED'):
            continue
        stop = _escalation_stop(reader, task['id'])
        if stop is not None:
            task['waiting'] = stop
            continue
        if not scheduler['live'] or task['status'] not in ('PENDING', 'INFRA'):
            continue
        pause = next((p for p in scheduler['pauses'] if p['scope'] == 'all' or task['id'] in (p['tasks'] or [])), None)
        if pause is not None:
            task['waiting'] = dict(pause, kind='infra')
            continue
        if task['status'] != 'PENDING':
            continue
        on = [dep for dep in task['deps'] if status.get(dep) != 'MERGED']
        if on:
            task['waiting'] = dict(kind='deps', on=on, stopped=[dep for dep in on if status.get(dep) in _STOPPED])
        elif scheduler['stopping']:
            task['waiting'] = dict(kind='stopping', mode=scheduler['stopping'])
        elif scheduler['drained']:
            task['waiting'] = dict(kind='drained')
        else:
            mine = [_scope(path) for path in task['files'] if path and path != '-']
            shared = [other['id'] for other in running if _overlap(mine, [_scope(p) for p in other['files'] if p and p != '-'])]
            cap = scheduler['max_parallel']
            if shared:
                task['waiting'] = dict(kind='overlap', on=shared)
            elif cap is not None and len(running) >= cap:
                task['waiting'] = dict(kind='slot', running=len(running), max_parallel=cap)
            else:
                task['waiting'] = dict(kind='next')


def _task_detail(reader, task, run_id, waves, branches):
    """Fill in a plan task; returns every execution it read (its own and its shadows')."""
    ident = task['id']
    prefix = f'tasks/{ident}'
    task.update(wave=waves.get(ident), branch=branches.get(ident))
    if not task['branch'] and run_id:
        try:
            if (reader.path / prefix).is_dir():
                task['branch'] = f'forge/{run_id}/{ident}'
        except (OSError, ValueError):
            pass
    task['attempts'], task['attempt'] = _attempt_state(reader, prefix)
    task['escalations'] = _history(reader, prefix + '/escalations.tsv', True)
    qa = reader.text(prefix + '/qa.last')
    task['verdict'] = _verdict(qa)
    task['qa_tail'] = reader.cap(qa, prefix + '/qa_tail', 4000, True)
    task['dwarf_tail'] = reader.cap(reader.text(prefix + '/dwarf.last'), prefix + '/dwarf_tail', 4000, True)
    task['accepted_by'] = reader.text(prefix + '/accepted.by')
    task['accepted_attempt'], task['accepted_over'] = _accepted_fields(
        reader, prefix + '/accepted.by', task['accepted_by'])
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
    execs = _task_execs(reader, task)
    task['executions'] = execs
    task['now'] = _now(reader, prefix, execs) if task['status'] == 'RUNNING' else None
    merged = reader.stat(prefix + '/merged') if task['status'] == 'MERGED' else None
    task['merged_at'] = float(merged.st_mtime) if merged is not None else None
    task['findings'] = executions.summary_of(qa)
    task['gate'] = _gate(reader, prefix + '/qa.gate')
    task['known_issues'] = _nonempty(reader, prefix + '/known_issues.md')
    task['fail_first'] = _fail_first(reader, prefix + '/failfirst.json')
    shadows = _shadows(reader, ident, execs)
    task['shadow'] = [entry for entry, _, _ in shadows]
    # What `forge control show` reads of the task. A change there (a write, or a delete: the dir's mtime) moves `updated`,
    # so the page redraws and the Controls panel asks again.
    reader.stat(prefix)
    for name in _TASK_CONTROLS:
        reader.stat(f'{prefix}/{name}')
    return execs + [item for _, items, _ in shadows for item in items]


# The control files (CONTRACT.md) nothing else here reads. `forge control` appends control.log and writes the task's
# files without touching the run dir, so they are stat'd for `updated` only.
# Read only for their mtimes, so the run's `updated` stamp moves when a control changes.
# control.requests is a directory: its mtime moves when a request is queued or settled.
_RUN_CONTROLS = ('control.log', 'retry_budget', 'infra_budget', 'drain', 'stop', 'max_parallel', 'infra.paused',
                 'infra_backoff', 'escalate_effort', 'control.requests')
_TASK_CONTROLS = ('qa_threshold', 'retry_budget', 'retries.used', 'escalation.pin', 'escalation.stop', 'escalation.next')


_FAIL_FIRST_RESULTS = ('proven', 'passes_without_change', 'inconclusive', 'error')


def _fail_first(reader, name):
    """failfirst.json (forge-fail-first.py, opt-in): did each new test fail without the change?"""
    obj = reader.object(name)
    if obj is None:
        return None
    status = obj.get('status')
    if status not in ('ran', 'skipped', 'error'):
        reader.warn(name, 'unknown fail-first status')
        return None
    tests = []
    for item in obj.get('tests') if isinstance(obj.get('tests'), list) else []:
        if isinstance(item, dict) and isinstance(item.get('path'), str) and item.get('result') in _FAIL_FIRST_RESULTS:
            runs = {}
            for side in ('with_change', 'without_change'):
                run = item.get(side) if isinstance(item.get(side), dict) else {}
                rc = run.get('rc')
                runs[side] = dict(rc=rc if type(rc) is int else None,
                                  tail=run['tail'][-2000:] if isinstance(run.get('tail'), str) else None)
            tests.append(dict(path=item['path'], result=item['result'], **runs))
    counts = dict.fromkeys(_FAIL_FIRST_RESULTS, 0)
    for item in tests:
        counts[item['result']] += 1
    return dict(status=status, reason=obj.get('reason') if isinstance(obj.get('reason'), str) else None,
                command=obj.get('command') if isinstance(obj.get('command'), str) else None,
                tests=tests, counts=counts)


def _published(reader):
    """realpath of the review the plan's `final-review` link names, if it stays inside the plan."""
    link = reader.path / 'final-review'
    try:
        if not os.path.islink(link):
            return None
        real = os.path.realpath(link)
    except (OSError, ValueError):
        return None
    return real if executions.inside(reader.path, real) else None


def _review_clock(reader, name):
    """(source, at) of a `final-review-<e>-<p>` or `integrate-<e>-<p>` dir name, else None.

    `at` is the whole-second epoch in the name. The same clock `executions.review_dirs` uses,
    so a summary does not have to scan the plan to know when the published review started.
    """
    match = executions._KIND.fullmatch(name)
    if not match:
        return None
    kind, epoch, _pid = match.groups()
    if len(epoch) > 15:
        reader.warn(name, 'invalid epoch')
        return None
    try:
        at = float(epoch)
        if not math.isfinite(at):
            raise ValueError()
    except (ValueError, OverflowError):
        reader.warn(name, 'invalid epoch')
        return None
    return ('review' if kind == 'final-review' else 'integrate'), at


def _published_identity(reader):
    """(source, at) of the dir `final-review` links to, when the link stays inside the plan."""
    real = _published(reader)
    if real is None:
        return None
    path = Path(real)
    # An integrate review is `<integrate-dir>/final-review`; a final-review dir is named itself.
    if path.name == 'final-review':
        found = _review_clock(reader, path.parent.name)
        return found if found is not None and found[0] == 'integrate' else None
    found = _review_clock(reader, path.name)
    return found if found is not None and found[0] == 'review' else None


def _merge_pairs(tasks):
    """(id, merged_at) of every merged task, oldest merge first, then id."""
    pairs = [(task['id'], task['merged_at']) for task in tasks if task.get('merged_at') is not None]
    pairs.sort(key=lambda item: (item[1], item[0]))
    return pairs


# What `git rev-parse` writes for a commit: sha1 (40) or sha256 (64), lowercase hex.
_COMMIT = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})')


def _meta_integration(reader, name, meta):
    """The `integration=` commit of a review.meta, or None.

    Missing or empty is absent (older runs record no integration commit). Any other value
    that is not a commit object name is malformed: null, and a warning, so freshness uses
    the next rule instead of treating the text as the commit the review judged.
    """
    value = meta.get('integration') or None
    if not value:
        return None
    if _COMMIT.fullmatch(value):
        return value
    reader.warn(name, 'integration is not a commit')
    return None


def _review_freshness(accepted, integration, tip, source, at, merges, verdict):
    """(outdated, merged_since) for one final review.

    Null when the review has no verdict or there is no `accepted.integration` to compare with.
    Otherwise the first rule that applies: a known `integration` commit, else `tip` when
    `source` is `review`, else (an older integrate review) any task merged after `at`.
    `integration` is a commit object name or None (a malformed value is None). A merge counts
    when its marker mtime is `>= at + 1`. `merged_since` is those ids, oldest first, and is
    empty unless the review is outdated.
    """
    later = []
    if at is not None:
        later = [item for item in merges if item[1] is not None and item[1] >= at + 1]
        later.sort(key=lambda item: (item[1], item[0]))
    ids = [task_id for task_id, _merged_at in later]
    if not verdict or not accepted:
        return None, []
    if integration:
        moved = integration != accepted
    elif source == 'review':
        moved = tip != accepted
    else:
        moved = bool(ids)
    return (True, ids) if moved else (False, [])


def _stamp_review(summary, accepted, merges):
    summary['outdated'], summary['merged_since'] = _review_freshness(
        accepted, summary['integration'], summary['tip'], summary['source'], summary['at'], merges,
        summary['verdict'])
    return summary


def _published_outdated(reader, tasks):
    """final_review_outdated for the published review only.

    Reads `accepted.integration` and, through the `final-review` link, that dir's `verdict`
    and `review.meta`. Does not scan other review dirs or open a review's attempts.
    """
    found = _published_identity(reader)
    if found is None:
        return None
    source, at = found
    verdict = reader.word('final-review/verdict')
    meta = executions.parse_resolved(reader.text('final-review/review.meta'))
    return _review_freshness(reader.line('accepted.integration'),
                             _meta_integration(reader, 'final-review/review.meta', meta),
                             meta.get('tip') or None, source, at, _merge_pairs(tasks), verdict)[0]


def _final_review(reader, item, newest, published):
    """(FinalReviewSummary, its executions, review.meta) of one review dir."""
    name, source, at, rel = item
    verdict = reader.word(rel + '/verdict')
    if verdict:
        state = 'done'
    elif reader.exists(rel + '/qa.infra'):
        state = 'infra'
    elif newest and reader.exists(rel + '/qa.input'):
        state = 'running'
    else:
        state = 'orphaned'
    meta = executions.parse_resolved(reader.text(rel + '/review.meta'))
    execs = executions.scan(reader, rel + '/attempts', state == 'running', ('qa',))
    run = execs[-1] if execs else None
    threshold = meta.get('threshold') or None
    try:
        real = os.path.realpath(reader.path / rel)
    except (OSError, ValueError):
        real = None
    summary = dict(name=name, source=source, at=at, published=published is not None and published == real,
                   verdict=verdict, state=state, spec=meta.get('spec') or None,
                   threshold=None if threshold == 'none' else threshold,
                   base=meta.get('base') or None, tip=meta.get('tip') or None,
                   integration=_meta_integration(reader, rel + '/review.meta', meta),
                   outdated=None, merged_since=[],
                   gate=_gate(reader, rel + '/qa.gate'), findings=executions.qa_result(reader, rel + '/qa.last')[1],
                   execution=run['id'] if run else None, started=run['started'] if run else None,
                   ended=run['ended'] if run else None, duration_s=run['duration_s'] if run else None)
    return summary, execs, meta


def _verification_runs(reader):
    """6 columns from a single command, 8 (adding lane and started) from lanes."""
    runs = []
    for label, row in reader.rows('verification.runs.tsv', 6):
        try:
            if len(row) not in (6, 8):
                raise ValueError()
            n, duration = int(row[0]), float(row[4])
            code = int(row[2]) if row[2].strip() else None
            started = float(row[7]) if len(row) == 8 else None
            if row[3].strip() not in ('0', '1') or not math.isfinite(duration) \
                    or (started is not None and not math.isfinite(started)):
                raise ValueError()
        except (ValueError, OverflowError):
            reader.warn(label, 'invalid verification run')
            continue
        runs.append(dict(n=n, kind=row[1], exit=code, tree_changed=row[3].strip() == '1',
                         duration_s=duration, log=row[5].strip() or None,
                         lane=(row[6].strip() or None) if len(row) == 8 else None, started=started))
    return runs


# --- live verification and lanes -----------------------------------------------------------

_LANES_MAX = 4 << 20           # verification.lanes.json beyond this is not read
_LIVE_TAIL = 65536             # what is read of a single-command log for its step and last line
_FAILING_MAX = 200
_LAST_LINE_MAX = 300
_STEP = re.compile(r'::forge step name=(\S+) state=(start|end)(?: exit=(-?\d+))?')
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]')
_LANE_STATES = ('waiting', 'running', 'pass', 'fail', 'flaky', 'known', 'skipped', 'interrupted')


def _finite(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) else None


def _str(value):
    return value if isinstance(value, str) else None


def _last_line(text):
    for line in reversed((text or '').splitlines()):
        line = _ANSI.sub('', line).strip()
        if line:
            return line[:_LAST_LINE_MAX]
    return None


def _open_step(text):
    """The last step marker started and not yet ended in the text, or None."""
    open_steps = []
    for line in (text or '').splitlines():
        match = _STEP.fullmatch(_ANSI.sub('', line).strip())
        if not match:
            continue
        if match.group(2) == 'start':
            open_steps.append(match.group(1))
        elif match.group(1) in open_steps:
            del open_steps[len(open_steps) - 1 - open_steps[::-1].index(match.group(1))]
    return open_steps[-1] if open_steps else None


def _lanes_doc(reader):
    """(the lanes document, its stat) or (None, stat): a missing, oversized or invalid file
    is warned about and never fails the request."""
    name = 'verification.lanes.json'
    info = reader.stat(name)
    if info is None:
        return None, None
    if info.st_size > _LANES_MAX:
        reader.warn(name, 'larger than 4 MB, not read')
        return None, info
    doc = reader.object(name)
    if doc is not None and not isinstance(doc.get('lanes'), list):
        reader.warn(name, 'no lanes list')
        doc = None
    return doc, info


def _items(value):
    """A JSON list, or nothing in place of anything else."""
    return value if isinstance(value, list) else []


def _tests(reader, value, field):
    if not isinstance(value, dict):
        return None
    counts = {key: int(value[key]) if isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
              else 0 for key in ('pass', 'fail', 'skip', 'error')}
    failing = []
    for entry in value.get('failing') if isinstance(value.get('failing'), list) else []:
        if not isinstance(entry, dict) or len(failing) >= _FAILING_MAX:
            continue
        line = entry.get('log_line')
        failing.append(dict(name=reader.cap(_str(entry.get('name')), field + '.name', 500),
                            suite=_str(entry.get('suite')), file=_str(entry.get('file')),
                            message=reader.cap(_str(entry.get('message')), field + '.message', 500),
                            excerpt=reader.cap(_str(entry.get('excerpt')), field + '.excerpt', 2000),
                            known=entry.get('known') is True,
                            log_line=line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else None))
    return dict(counts, failing=failing,
                reports=[r for r in _items(value.get('reports')) if isinstance(r, str)][:200],
                notes=[reader.cap(n, field + '.notes', 500) for n in _items(value.get('notes')) if isinstance(n, str)][:50],
                truncated=value.get('truncated') is True)


def _lanes(reader, doc, now):
    lanes = []
    for index, lane in enumerate(doc['lanes']):
        if not isinstance(lane, dict) or not _str(lane.get('name')):
            reader.warn('verification.lanes.json', f'lane {index + 1} is not a lane')
            continue
        name = lane['name']
        started, ended = _finite(lane.get('started')), _finite(lane.get('ended'))
        state = lane.get('state') if lane.get('state') in _LANE_STATES else _str(lane.get('state'))
        last_output = _finite(lane.get('last_output_at'))
        step = lane.get('step') if isinstance(lane.get('step'), dict) else None
        runs = []
        for run in lane.get('runs') if isinstance(lane.get('runs'), list) else []:
            if isinstance(run, dict):
                runs.append(dict(k=run.get('k') if isinstance(run.get('k'), int) else None, kind=_str(run.get('kind')),
                                 started=_finite(run.get('started')), ended=_finite(run.get('ended')),
                                 exit=run.get('exit') if isinstance(run.get('exit'), int) else None,
                                 log=_str(run.get('log'))))
        options = lane.get('options') if isinstance(lane.get('options'), dict) else {}
        lanes.append(dict(
            name=name, command=reader.cap(_str(lane.get('command')), f'verification.lanes.{name}.command'),
            group=_str(lane.get('group')), first=lane.get('first') is True,
            retries=options.get('retries') if isinstance(options.get('retries'), int) else None,
            options={k: v for k, v in options.items() if isinstance(v, (str, int)) and not isinstance(v, bool)},
            state=state, exit=lane.get('exit') if isinstance(lane.get('exit'), int) else None,
            started=started, ended=ended,
            duration_s=((ended if ended is not None else now) - started) if started is not None else None,
            runs=runs,
            step=dict(name=_str(step.get('name')), started=_finite(step.get('started'))) if step else None,
            last_line=reader.cap(_str(lane.get('last_line')), f'verification.lanes.{name}.last_line', _LAST_LINE_MAX),
            last_output_at=last_output,
            idle_s=(now - last_output) if state == 'running' and last_output is not None else None,
            log=_str(lane.get('log')),
            tests=_tests(reader, lane.get('tests'), f'verification.lanes.{name}.tests')))
    return lanes


def _live_single(reader, started, now):
    """The running command's live state from whichever of its two logs moved last: a rerun
    writes verification.retry.log."""
    logs = [(info.st_mtime, name) for name in ('verification.log', 'verification.retry.log')
            for info in [reader.stat(name)] if info is not None]
    if not logs:
        return dict(started=started, step=None, last_line=None, last_output_at=None,
                    idle_s=(now - started) if started is not None else None, log=None)
    mtime, name = max(logs)
    text = reader.text(name, _LIVE_TAIL)
    step = _open_step(text)
    return dict(started=started, step=dict(name=step, started=None) if step else None,
                last_line=_last_line(text), last_output_at=float(mtime), idle_s=max(0.0, now - mtime), log=name)


def _live_lanes(lanes, started, now):
    """The running lane that printed most recently speaks for the verification."""
    running = [lane for lane in lanes if lane['state'] == 'running']
    printed = [lane for lane in running if lane['last_output_at'] is not None]
    lane = max(printed, key=lambda item: item['last_output_at']) if printed else (running[0] if running else None)
    last = lane['last_output_at'] if lane else None
    return dict(started=started, step=lane['step'] if lane else None, last_line=lane['last_line'] if lane else None,
                last_output_at=last, idle_s=max(0.0, now - (last if last is not None else started))
                if (last is not None or started is not None) else None, log=None)


def _verification(reader, status, verifications):
    """RunDetail.verification: the result files, plus whether it is running now, in which
    mode, its live state, its lanes and every failing test."""
    now = time.time()
    newest = max(verifications, key=lambda item: (item['started'] is not None, item['started'] or 0.0), default=None)
    state = None if newest is None else 'running' if newest['state'] == 'running' else 'done'
    started = newest['started'] if newest is not None else None
    doc, info = _lanes_doc(reader)
    lanes_file = info is not None
    mode = 'lanes' if lanes_file and (started is None or info.st_mtime >= started) else 'single'
    command = reader.text('verification.command')
    log_tail = reader.cap(reader.text('verification.log', 65536), 'verification.log_tail', 4000, True)
    present = any('verification.' + suffix in reader.present_files for suffix in ('status', 'command', 'log'))
    if not (present or state or lanes_file):
        return None
    lanes = _lanes(reader, doc, now) if mode == 'lanes' and doc is not None else None
    if state == 'running':
        live = _live_lanes(lanes or [], started, now) if mode == 'lanes' else _live_single(reader, started, now)
    else:
        live = None
    failing = []
    for lane in lanes or []:
        for entry in (lane['tests'] or {}).get('failing', []):
            if len(failing) < _FAILING_MAX:
                failing.append(dict(entry, lane=lane['name']))
    runs = _verification_runs(reader)
    # A lane or a run of the command changed the checkout: later reruns were skipped.
    mutated = doc.get('mutated') is True if lanes is not None else any(run['tree_changed'] for run in runs)
    return dict(status='RUNNING' if state == 'running' else status,
                command=command.strip() if command is not None else None,
                log_tail=log_tail, runs=runs, commit=reader.line('verification.commit'),
                state=state, mode=mode, live=live, lanes=lanes, failing=failing, mutated=mutated)


def _integration(reader, tasks, integrations, reviews, published):
    base, accepted = reader.line('integration.base'), reader.line('accepted.integration')
    merged = sorted((dict(id=task['id'], at=task['merged_at']) for task in tasks if task['merged_at'] is not None),
                    key=lambda item: (item['at'], item['id']))
    recorded = {'integration.base', 'accepted.integration'} & reader.present_files
    if not (recorded or merged or integrations):
        return None
    by_name = {summary['name']: summary for summary in reviews if summary['source'] == 'integrate'}
    entries = []
    for index, (name, at, item) in enumerate(integrations):
        review = by_name.get(name)
        if review is None and item is not None:  # older than the reviews RunDetail lists
            review = _final_review(reader, item, False, published)[0]
        # Nothing records when an integration ended: its verification and its review did.
        ends = [item['ended'] for item in executions.scan(reader, name + '/attempts', index == 0, ('verification',))]
        if review is not None:
            ends.append(review['ended'])
        entries.append(dict(name=name, at=at, verification_status=reader.word(name + '/verification.status'),
                            final_review=name + '/final-review' if review is not None else None,
                            ended=max(ends) if ends and None not in ends else None))
    return dict(base=base, accepted=accepted, merged=merged, integrations=entries)


def _timeline(pool, runs, verifications):
    starts = [item['started'] for item in pool if item['started'] is not None]
    ends = [item['ended'] for item in pool if item['ended'] is not None]
    running = any(item['state'] == 'running' for item in pool)
    return dict(started=min(starts) if starts else None, ended=max(ends) if ends and not running else None,
                runs=runs, verifications=verifications)


_SOLO_SETTINGS = ('explore', 'shadow_rate', 'shadow_qa', 'timeout', 'max_parallel', 'qa_threshold',
                  'escalate_after', 'verify_retries', 'final_qa')


def _solo_detail(reader, source):
    result = _solo_summary(reader, source)
    status = next(iter(result['counts']))
    result['request'] = reader.cap(reader.text('request.md'), 'request')
    result['settings'] = dict.fromkeys(_SOLO_SETTINGS)
    result['verification'] = None
    result['waves'] = [['solo']]
    attempts = _history(reader, 'attempts.tsv')
    qa = reader.text('qa.last')
    execs = _solo_execs(reader, status, attempts)
    task = dict(id='solo', title=result['goal'] or 'Solo run', deps=[], difficulty=None, files=[],
                dwarf=_role_spec(reader, 'dwarf'), qa=_role_spec(reader, 'qa'), wave=1, status=status,
                branch=None, attempt=max((item['attempt'] for item in attempts), default=None),
                attempts=attempts, escalations=[], verdict=_verdict(qa), accepted_by=None,
                accepted=False, accepted_attempt=None, accepted_over=None,
                explored=False, route=[], clef={'first_pass': None, 'retry_pass': {}}, shadow=[],
                prompt=reader.cap(reader.text('prompt.md'), 'tasks/solo/prompt'), approach=None,
                retry_findings=None, qa_tail=reader.cap(qa, 'tasks/solo/qa_tail', 4000, True),
                dwarf_tail=reader.cap(reader.text('dwarf.last'), 'tasks/solo/dwarf_tail', 4000, True),
                executions=execs, merged_at=None, findings=executions.summary_of(qa),
                gate=_gate(reader, 'qa.gate'), known_issues=_nonempty(reader, 'known_issues.md'),
                fail_first=_fail_first(reader, 'failfirst.json'),
                now=_now(reader, '', execs) if status == 'RUNNING' else None, waiting=None)
    result.update(timeline=_timeline(execs, [], []), integration=None, final_review=None, final_reviews=[],
                  deferrals=[], findings_available=findings.available(), fractal=fractal.summary(reader),
                  scheduler=None)
    result.update(tasks=[task], warnings=reader.warnings, truncated=reader.truncated, updated=reader.updated)
    return result


def _waves(reader):
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
    return waves, wave_map


def detail(path, source='plan') -> dict:
    reader = _Read(path)
    if _kind(reader.path) == 'solo':
        return _solo_detail(reader, source)
    result, tasks = _summary(reader, source)
    result['request'] = reader.cap(reader.text('request.md'), 'request')
    result['settings'] = dict(explore=reader.number('explore'), shadow_rate=reader.number('shadow_rate'),
                              shadow_qa=_list(reader.text('shadow_qa') or ''), timeout=reader.number('timeout', int),
                              max_parallel=reader.number('max_parallel', int), qa_threshold=reader.line('qa_threshold'),
                              escalate_after=reader.number('escalate_after', int),
                              verify_retries=reader.number('verify_retries', int), final_qa=reader.line('final_qa'))
    waves, wave_map = _waves(reader)
    result['waves'] = [waves.get(n, []) for n in range(1, max(waves, default=0) + 1)]
    branches = {row[0]: _nullable(row[2]) for _, row in reader.rows('results.tsv', 3)}
    pool = []
    for task in tasks:
        pool += _task_detail(reader, task, result['run_id'], wave_map, branches)
    for name in _RUN_CONTROLS:
        reader.stat(name)
    result['scheduler'] = _scheduler(reader)
    if result['scheduler']['max_parallel'] is None and result['scheduler']['live']:
        result['scheduler']['max_parallel'] = result['settings']['max_parallel']
    _waiting(reader, tasks, result['scheduler'])
    run_execs = _run_execs(reader)
    result['verification'] = _verification(reader, result['verification_status'],
                                           [item for item in run_execs if item['role'] == 'verification'])
    items, integrations = executions.review_dirs(reader)
    published = _published(reader)
    reviews = []
    for index, item in enumerate(items[:_MAX_REVIEWS]):
        summary, execs, _ = _final_review(reader, item, index == 0, published)
        reviews.append(summary)
        pool += execs
    result['timeline'] = _timeline(pool + run_execs, [item for item in run_execs if item['role'] == 'parallel'],
                                   [item for item in run_execs if item['role'] == 'verification'])
    result['integration'] = _integration(reader, tasks, integrations, reviews, published)
    accepted = result['integration']['accepted'] if result['integration'] is not None else None
    merges = [(item['id'], item['at']) for item in result['integration']['merged']] if result['integration'] else []
    for summary in reviews:
        _stamp_review(summary, accepted, merges)
    result['final_review'] = reviews[0] if reviews else None
    result['final_reviews'] = reviews
    result['deferrals'] = [line.strip() for line in (reader.text('deferrals.txt') or '').splitlines() if line.strip()]
    result['findings_available'] = findings.available()
    result['fractal'] = fractal.summary(reader)
    result.update(tasks=tasks, warnings=reader.warnings, truncated=reader.truncated, updated=reader.updated)
    return result


# --- lazy details: one task, one execution, one final review ------------------------------

_TEXTS = (('prompt', 'prompt.md'), ('approach', 'approach.md'), ('retry_findings', 'retry_findings.md'),
          ('accepted_by', 'accepted.by'), ('known_issues', 'known_issues.md'), ('dwarf_input', 'dwarf.input'),
          ('qa_input', 'qa.input'))
# A solo run has no approach, retry findings or human acceptance (as in its RunDetail task).
_SOLO_TEXTS = ('prompt', 'known_issues', 'dwarf_input', 'qa_input')


def _plan_task(reader, tasks, task_id):
    task = next((item for item in tasks if item['id'] == task_id), None)
    if task is not None:
        task['attempts'], task['attempt'] = _attempt_state(reader, f'tasks/{task_id}')
    return task


def _review(kind, item, text):
    return dict(kind=kind, execution=item['id'], attempt=item['attempt'], spec=(item['model'] or {}).get('spec'),
                verdict=item['verdict'], state=item['state'], findings=findings.listing(text))


def task_detail(path, task_id, source='plan'):
    """TaskDetail of one task (`solo` for a solo run), or None when there is no such task."""
    reader = _Read(path)
    kind = _kind(reader.path)
    if kind == 'solo' and task_id == 'solo':
        base, shadows, wanted = '', [], _SOLO_TEXTS
        attempts = _history(reader, 'attempts.tsv')
        execs = _solo_execs(reader, _solo_status(reader), attempts)
        attempt, spec = max((item['attempt'] for item in attempts), default=None), _role_spec(reader, 'qa')
    elif kind == 'plan':
        task = _plan_task(reader, _tasks(reader), task_id)
        if task is None:
            return None
        base, wanted = f'tasks/{task_id}/', [field for field, _ in _TEXTS]
        execs = _task_execs(reader, task)
        shadows = _shadows(reader, task_id, execs)
        attempt, spec = task['attempt'], task['qa']
    else:
        return None
    texts = {field: reader.excerpt(base + filename, 20000, 8000, field) if field in wanted else None
             for field, filename in _TEXTS}
    reviews, seen = [], set()
    for item in execs:
        if item['role'] == 'qa' and not item['shadow']:
            text = reader.text(item.rel + '/qa.last') if item['state'] == 'done' else None
            if text is not None:
                seen.add(text)
            reviews.append(_review('primary', item, text))
    live = reader.text(base + 'qa.last')
    # The reply in the task dir is normally the copy of the last review; only an unmatched one is listed.
    if live is not None and live not in seen:
        reviews.append(dict(kind='primary', execution=None, attempt=attempt, spec=spec, verdict=_verdict(live),
                            state='done', findings=findings.listing(live)))
    for entry, items, sub in shadows:
        run = items[-1] if items else None
        reviews.append(dict(kind='shadow', execution=entry['execution'], attempt=entry['attempt'],
                            spec=entry['spec'] if isinstance(entry['spec'], str) else None, verdict=entry['verdict'],
                            state=run['state'] if run else None, findings=findings.listing(reader.text(sub + '/qa.last'))))
    return dict(run=run_key(reader.path), id=task_id, texts=texts, gate=_gate(reader, base + 'qa.gate'),
                reviews=reviews, truncated=reader.truncated, warnings=reader.warnings)


def _execution(reader, name):
    """(Execution, owner, owner dir) of the execution `name` of the run, or None."""
    kind = _kind(reader.path)
    if kind is None or not executions.NAME.fullmatch(name or ''):
        return None
    tasks = _tasks(reader) if kind == 'plan' else []
    rel = executions.locate(reader.path, name, [task['id'] for task in tasks])
    if rel is None:
        return None
    parts = rel.parts
    if parts[0] == 'attempts' and kind == 'solo':
        execs = _solo_execs(reader, _solo_status(reader), _history(reader, 'attempts.tsv'))
        owner, owner_dir = dict(kind='task', id='solo'), ''
    elif parts[0] == 'attempts':
        execs, owner, owner_dir = _run_execs(reader), dict(kind='run', id=None), ''
    elif parts[0] in ('tasks', 'shadow'):
        task = _plan_task(reader, tasks, parts[1])
        execs = _task_execs(reader, task)
        owner, owner_dir = dict(kind='task', id=parts[1]), f'tasks/{parts[1]}'
        if parts[0] == 'shadow':
            owner_dir = f'shadow/{parts[1]}/{parts[2]}'
            execs = next((items for _, items, sub in _shadows(reader, parts[1], execs) if sub == owner_dir), [])
            owner = dict(kind='shadow', id=f'{parts[1]}/{parts[2]}')
    else:
        items = executions.review_dirs(reader)[0]
        index = next((i for i, item in enumerate(items) if item[0] == parts[0]), None)
        if index is None:
            return None
        execs = _final_review(reader, items[index], index == 0, _published(reader))[1]
        owner, owner_dir = dict(kind='final_review', id=parts[0]), items[index][3]
    item = next((candidate for candidate in execs if candidate['id'] == name), None)
    return None if item is None else (item, owner, owner_dir)


def execution_detail(path, name, source='plan'):
    """ExecutionDetail of one attempts/<role>-<uuid>/ dir of the run, or None."""
    reader = _Read(path)
    found = _execution(reader, name)
    if found is None:
        return None
    item, owner, owner_dir = found
    role, running = item['role'], item['state'] == 'running'
    resolved = executions.resolved_of(reader, f'{item.rel}/{role}.resolved')
    prompt = last = listed = None
    if role in ('dwarf', 'qa'):
        if resolved is None and running:
            resolved = executions.parse_resolved(reader.text(posixpath.join(owner_dir, role + '.resolved'))) or None
        prompt = reader.excerpt(f'{item.rel}/{role}.prompt', 40000, 20000, 'prompt')
        # While it runs, the attempt dir has no copies yet: show what the dispatch is working from.
        for suffix in ('delivered', 'prompt', 'input'):
            if prompt is not None or not running:
                break
            prompt = reader.excerpt(posixpath.join(owner_dir, f'{role}.{suffix}'), 40000, 20000, 'prompt')
        last = reader.cap(reader.text(f'{item.rel}/{role}.last', 4 * 20000 + 4), 'last', 20000, True)
        if role == 'qa' and item['state'] == 'done':
            listed = findings.listing(reader.text(f'{item.rel}/qa.last'))
    result = dict(item)
    result.update(resolved=dict(resolved) if resolved is not None else None,
                  dataset=dict(item.dataset) if item.dataset is not None else None, prompt=prompt, last=last,
                  findings=listed, owner=owner, truncated=reader.truncated, warnings=reader.warnings)
    return result


def execution_transcript(path, name, source='plan'):
    """ExecutionTranscript of a dwarf or qa execution: its agent's conversation, read from its `.log`; None
    when there is no such execution or it is of another role."""
    reader = _Read(path)
    found = _execution(reader, name)
    if found is None or found[0]['role'] not in ('dwarf', 'qa'):
        return None
    item, owner, owner_dir = found
    role, running = item['role'], item['state'] == 'running'
    # A finished execution's log is its copy; the owner dir's is the one being written, or the next one's.
    log = _agent_log(reader, item, owner_dir)
    resolved = executions.resolved_of(reader, f'{item.rel}/{role}.resolved')
    if resolved is None and running:
        resolved = executions.parse_resolved(reader.text(posixpath.join(owner_dir, role + '.resolved'))) or None
    model = item['model'] or {}
    result = transcripts.transcript(log, model.get('harness'), running)
    result.update(run=run_key(reader.path), execution=name, role=role, state=item['state'], attempt=item['attempt'],
                  spec=model.get('spec'), harness=model.get('harness'), repo=(resolved or {}).get('repo') or None,
                  owner=owner, warnings=reader.warnings + result['warnings'])
    return result


def _agent_log(reader, item, owner_dir):
    """The log of a dwarf or reviewer execution: its copy once it ended, the one being written while it runs."""
    role = item['role']
    return _log_path(reader, posixpath.join(owner_dir, role + '.log') if item['state'] == 'running' else f'{item.rel}/{role}.log')


def _checks_of(reader, item, owner_dir):
    """checks.analyze() of one dwarf or reviewer execution, with what it is and, while it runs, when its log moved."""
    log = _agent_log(reader, item, owner_dir)
    model = item['model'] or {}
    running = item['state'] == 'running'
    result = transcripts.transcript(log, model.get('harness'), running)
    found = checks.analyze(result['events'])
    moved = None
    if running and log is not None:
        try:
            moved = log.stat().st_mtime
        except OSError:
            pass
    found.update(execution=item['id'], role=item['role'], state=item['state'], attempt=item['attempt'],
                 spec=model.get('spec'), harness=model.get('harness'), source=result['source'],
                 truncated=result['truncated'], last_output_at=moved)
    return found


def _brief_dir(kind, owner):
    """Where the brief of an execution's owner is, or None for a final review or a shadow."""
    if owner['kind'] == 'task':
        return '' if owner['id'] == 'solo' and kind == 'solo' else f"tasks/{owner['id']}"
    return None


def execution_checks(path, name, source='plan'):
    """ExecutionChecks: the checks a dwarf or reviewer ran, the call it makes now, the environment problems it met
    and, for a task's execution, its brief's acceptance-criteria commands against them. None for another role."""
    reader = _Read(path)
    found = _execution(reader, name)
    if found is None or found[0]['role'] not in ('dwarf', 'qa'):
        return None
    item, owner, owner_dir = found
    result = _checks_of(reader, item, owner_dir)
    brief = _brief_dir(_kind(reader.path), owner)
    wanted = checks.criteria(reader.text(posixpath.join(brief, 'prompt.md'))) if brief is not None else []
    result.update(run=run_key(reader.path), owner=owner,
                  criteria=checks.coverage(wanted, [(c['command'], dict(index=c['index'])) for c in result['checks']]),
                  warnings=reader.warnings)
    return result


def _owners(reader):
    """(task id, owner dir, executions) of each task of the run (the one `solo` of a solo run), or None."""
    kind = _kind(reader.path)
    if kind == 'solo':
        return [('solo', '', _solo_execs(reader, _solo_status(reader), _history(reader, 'attempts.tsv')))]
    if kind != 'plan':
        return None
    out = []
    for task in _tasks(reader):
        task['attempts'], task['attempt'] = _attempt_state(reader, f"tasks/{task['id']}")
        out.append((task['id'], f"tasks/{task['id']}", _task_execs(reader, task)))
    return out


def task_checks(path, task_id, source='plan'):
    """TaskChecks: attempt by attempt, the checks each dwarf and reviewer ran, and which of the commands the task's
    acceptance criteria name ran, ran another way, or did not run."""
    reader = _Read(path)
    owners = _owners(reader)
    owner = next((item for item in owners or [] if item[0] == task_id), None)
    if owner is None:
        return None
    _, owner_dir, execs = owner
    wanted = checks.criteria(reader.text(posixpath.join(owner_dir, 'prompt.md')))
    by_attempt = {}
    for item in execs:
        if item['role'] in ('dwarf', 'qa') and not item['shadow']:
            by_attempt.setdefault(item['attempt'], []).append(_checks_of(reader, item, owner_dir))
    attempts = []
    for attempt in sorted(by_attempt, key=lambda value: (value is None, value or 0)):
        runs = by_attempt[attempt]
        ran = [(c['command'], dict(execution=r['execution'], role=r['role'], index=c['index'])) for r in runs for c in r['checks']]
        attempts.append(dict(attempt=attempt, running=any(r['state'] == 'running' for r in runs), executions=runs,
                             criteria=checks.coverage(wanted, ran)))
    return dict(run=run_key(reader.path), task=task_id, criteria=wanted, attempts=attempts,
                truncated=reader.truncated, warnings=reader.warnings)


_ENVIRONMENT_HITS = 50         # a problem's hits answered (its tasks are all named)
_ENV_CACHE = executions._SigCache(4096)


def _environment_of(reader, item, owner_dir):
    """The environment problems one execution's failed commands met, cached while its log is unchanged."""
    log = _agent_log(reader, item, owner_dir)
    if log is None:
        return []
    try:
        info = log.stat()
    except OSError:
        return []
    clock = log.with_name(log.stem + '.clock')
    sig = (executions._signature(info), item['state'], transcripts._signature(clock))
    hit = _ENV_CACHE.get(('environment', str(log)), sig)
    if hit is None:
        result = transcripts.transcript(log, (item['model'] or {}).get('harness'), item['state'] == 'running')
        hit = checks.analyze(result['events'])['environment']
        _ENV_CACHE.put(('environment', str(log)), sig, hit)
    return hit


def run_environment(path, source='plan'):
    """RunEnvironment: each environment problem the run's dwarfs and reviewers met (Docker, a sandbox's EPERM, a
    port in use, …), with the tasks it hit and where: the commands that failed on it."""
    reader = _Read(path)
    owners = _owners(reader)
    if owners is None:
        return None
    found = {}
    for task_id, owner_dir, execs in owners:
        for item in execs:
            if item['role'] not in ('dwarf', 'qa'):
                continue
            for hit in _environment_of(reader, item, owner_dir):
                entry = found.setdefault(hit['problem'], dict(problem=hit['problem'], label=checks.LABELS[hit['problem']],
                                                              tasks=[], count=0, hits=[]))
                entry['count'] += 1
                if task_id not in entry['tasks']:
                    entry['tasks'].append(task_id)
                if len(entry['hits']) < _ENVIRONMENT_HITS:
                    entry['hits'].append(dict(task=task_id, attempt=item['attempt'], role=item['role'], execution=item['id'],
                                              index=hit['index'], command=hit['command'], line=hit['line']))
    problems = [found[key] for key, _, _ in checks.ENVIRONMENT if key in found]
    return dict(run=run_key(reader.path), problems=problems, warnings=reader.warnings)


def final_review_detail(path, name, source='plan'):
    """FinalReviewDetail of final-review-<e>-<p> or integrate-<e>-<p>, or None."""
    reader = _Read(path)
    if _kind(reader.path) != 'plan':
        return None
    items = executions.review_dirs(reader)[0]
    index = next((i for i, item in enumerate(items) if item[0] == name), None)
    if index is None:
        return None
    summary, _, meta = _final_review(reader, items[index], index == 0, _published(reader))
    _stamp_review(summary, reader.line('accepted.integration'), _merge_pairs(_tasks(reader)))
    rel = items[index][3]
    qa_last = reader.text(rel + '/qa.last')
    result = dict(summary)
    result.update(meta=meta, tasks_summary=reader.excerpt(rel + '/tasks.summary', 20000, 8000, 'tasks_summary'),
                  qa_input=reader.excerpt(rel + '/qa.input', 40000, 20000, 'qa_input'),
                  qa_last=reader.clip(qa_last, 20000, 8000, 'qa_last'),
                  known_issues=reader.excerpt(rel + '/known_issues.md', 20000, 8000, 'known_issues'),
                  findings=findings.listing(qa_last), truncated=reader.truncated, warnings=reader.warnings)
    return result


# --- diffs: what a task changed and what each review read, its findings pinned -------------

_PLAN_DIFFS = (('current', 'changes.diff'), ('prev', 'changes.prev.diff'), ('committed', 'committed.diff'))
_SOLO_DIFFS = (('current', 'changes.diff'), ('existing', 'existing.diff'))
_NOT_INLINED = 'not inlined (larger than 400 KB)'


def _url(key, *parts):
    return '/api/runs/' + key + ''.join('/' + urllib.parse.quote(part, safe='') for part in parts)


def _first(reader, rels):
    return next((rel for rel in rels if os.path.lexists(reader.path / rel)), None)


def _regular(reader, rel):
    """fstat of a regular file under the run dir, quietly; None otherwise."""
    try:
        real = os.path.realpath(reader.path / rel)
        info = os.stat(real)
    except (OSError, ValueError):
        return None
    return info if executions.inside(reader.path, real) and stat.S_ISREG(info.st_mode) else None


def _diff_review(kind, item, attempt, spec, verdict, last, prompts, execution=None):
    return dict(kind=kind, item=item, execution=item['id'] if item is not None else execution, attempt=attempt,
                spec=spec, verdict=verdict, last=last, prompts=prompts)


def _prompts(item, owner_dir):
    """Where the diff a qa execution reviewed is: its prompt copy, made when it finishes; while it
    runs, what its dispatch works from."""
    prompts = [item.rel + '/qa.prompt']
    if item['state'] == 'running':
        prompts += [posixpath.join(owner_dir, 'qa.' + suffix) for suffix in ('delivered', 'prompt', 'input')]
    return prompts


def _diff_task(reader, task_id):
    """What a task's diffs need: dir, diff files, attempt, executions and every review; None if no such task."""
    kind = _kind(reader.path)
    if kind == 'solo' and task_id == 'solo':
        attempts = _history(reader, 'attempts.tsv')
        execs = _solo_execs(reader, _solo_status(reader), attempts)
        base, names, shadows = '', _SOLO_DIFFS, []
        attempt, spec = max((item['attempt'] for item in attempts), default=None), _role_spec(reader, 'qa')
    elif kind == 'plan':
        task = _plan_task(reader, _tasks(reader), task_id)
        if task is None:
            return None
        base, names = f'tasks/{task_id}', _PLAN_DIFFS
        execs = _task_execs(reader, task)
        shadows = _shadows(reader, task_id, execs)
        attempt, spec = task['attempt'], task['qa']
    else:
        return None
    reviews, copies = [], set()
    for item in execs:
        if item['role'] == 'qa' and not item['shadow']:
            last = item.rel + '/qa.last' if item['state'] == 'done' else None
            copies.add(reader.text(last) if last else None)
            reviews.append(_diff_review('primary', item, item['attempt'], (item['model'] or {}).get('spec'),
                                        item['verdict'], last, _prompts(item, base)))
    live = posixpath.join(base, 'qa.last')
    text = reader.text(live)
    # As in TaskDetail: the task's own qa.last counts only when no execution's copy is identical.
    if text is not None and text not in copies:
        reviews.append(_diff_review('primary', None, attempt, spec, _verdict(text), live, []))
    shadowed = []
    for entry, items, sub in shadows:
        run = items[-1] if items else None
        shadowed += items
        reviews.append(_diff_review('shadow', run, entry['attempt'], entry['spec'] if isinstance(entry['spec'], str)
                                    else None, entry['verdict'], sub + '/qa.last',
                                    ([run.rel + '/qa.prompt'] if run else []) + [sub + '/qa.input']))
    return dict(kind='task', id=task_id, dir=base, names=names, attempt=attempt, execs=execs + shadowed,
                reviews=reviews)


def _diff_final(reader, name):
    items = executions.review_dirs(reader)[0]
    index = next((i for i, item in enumerate(items) if item[0] == name), None)
    if index is None:
        return None
    summary, execs, _ = _final_review(reader, items[index], index == 0, _published(reader))
    reviews = [_diff_review('final', item, None, summary['spec'] or (item['model'] or {}).get('spec'),
                            item['verdict'], item.rel + '/qa.last' if item['state'] == 'done' else None,
                            [item.rel + '/qa.prompt'])
               for item in execs]
    return dict(kind='final_review', id=name, dir=items[index][3], items=items, summary=summary, execs=execs,
                reviews=reviews)


def _diff_sources(reader, key, ctx):
    sources = []
    if ctx['kind'] == 'final_review':
        for name, _, _, rel in ctx['items'][:_MAX_REVIEWS]:
            info = _regular(reader, rel + '/combined.diff')
            if info is not None or name == ctx['id']:
                sources.append(dict(id=name, label='combined.diff' if info else 'reviewed diff', kind='final',
                                    verdict=reader.word(rel + '/verdict'), bytes=info.st_size if info else None,
                                    url=_url(key, 'final-reviews', name, 'diff')))
        return sources
    for which, name in ctx['names']:
        info = _regular(reader, posixpath.join(ctx['dir'], name))
        if info is not None:
            sources.append(dict(id=which, label=name, bytes=info.st_size,
                                url=_url(key, 'tasks', ctx['id'], 'diff', *([which] if which != 'current' else []))))
    for review in ctx['reviews']:
        rel = _first(reader, review['prompts']) if review['item'] is not None else None
        size = diffs.fence_bytes(reader, rel) if rel else None
        if size is not None:
            label = 'reviewed diff' if review['kind'] == 'primary' else 'shadow review'
            if review['attempt'] is not None:
                label += f', attempt {review["attempt"]}'
            sources.append(dict(id=review['execution'], label=label, attempt=review['attempt'],
                                verdict=review['verdict'], kind=review['kind'], bytes=size,
                                url=_url(key, 'executions', review['execution'], 'diff')))
    return sources


def _diff_detail(reader, key, origin, sources, loaded, reason, reviews):
    files = loaded['files'] if loaded is not None else []
    found = []
    for review in reviews:
        text = reader.text(review['last']) if review['last'] else None
        for index, item in enumerate(findings.listing(text) or []):
            found.append(dict(id=_finding_id(review, index), review={key: review[key] for key in ('kind', 'execution', 'attempt', 'spec')},
                              severity=item['severity'], status=item['status'], location=item['location'],
                              summary=item['summary'], text=item['text']))
    return dict(run=key, source=origin, sources=sources, available=loaded is not None,
                reason=None if loaded is not None else reason, bytes=loaded['bytes'] if loaded else None,
                truncated=bool(loaded and loaded['truncated']), files=files, findings=diffs.pin(found, files),
                warnings=reader.warnings, truncated_fields=reader.truncated)


def _missing(reader, rel):
    return 'unreadable (see warnings)' if os.path.lexists(reader.path / rel) else 'no ' + posixpath.basename(rel)


def task_diff(path, task_id, which='current', source='plan'):
    """DiffDetail of a task's changes.diff (`current`), changes.prev.diff, committed.diff or, for a
    solo run, existing.diff; None when the task or that kind of diff does not exist."""
    reader = _Read(path)
    ctx = _diff_task(reader, task_id)
    names = dict(ctx['names']) if ctx is not None else {}
    if which not in names:
        return None
    rel = posixpath.join(ctx['dir'], names[which])
    loaded = diffs.load(reader, rel)
    latest = ctx['attempt']
    if latest is None:
        latest = max((review['attempt'] for review in ctx['reviews'] if review['attempt'] is not None), default=None)
    key = run_key(reader.path)
    origin = dict(kind='task', id=task_id, which=which, execution=None,
                  attempt=latest if which in ('current', 'committed') else None, file=rel)
    return _diff_detail(reader, key, origin, _diff_sources(reader, key, ctx), loaded, _missing(reader, rel),
                        [review for review in ctx['reviews'] if review['attempt'] == latest])


def execution_diff(path, name, source='plan'):
    """DiffDetail of the diff a qa execution reviewed (for a dwarf: its attempt's review), or None."""
    reader = _Read(path)
    kind = _kind(reader.path)
    if kind is None or not executions.NAME.fullmatch(name or '') or name.split('-', 1)[0] not in ('qa', 'dwarf'):
        return None
    rel = executions.locate(reader.path, name, [task['id'] for task in _tasks(reader)] if kind == 'plan' else [])
    if rel is None:
        return None
    parts = rel.parts
    if parts[0] == 'attempts':
        ctx = _diff_task(reader, 'solo') if kind == 'solo' else None
    elif parts[0] in ('tasks', 'shadow'):
        ctx = _diff_task(reader, parts[1])
    else:
        ctx = _diff_final(reader, parts[0])
    item = next((candidate for candidate in ctx['execs'] if candidate['id'] == name), None) if ctx else None
    if item is None:
        return None
    review = next((review for review in ctx['reviews'] if review['execution'] == name), None)
    if review is None and item['role'] == 'qa':  # an older run of a shadow dir
        review = _diff_review('shadow', item, item['attempt'], (item['model'] or {}).get('spec'), item['verdict'],
                              item.rel + '/qa.last' if item['state'] == 'done' else None, [item.rel + '/qa.prompt'])
    elif review is None:
        # A dwarf's diff is the one its attempt's review read.
        def same(other):
            if item['parent']:
                return other['parent'] == item['parent']
            return item['attempt'] is not None and other['attempt'] == item['attempt']
        matches = [other for other in ctx['reviews']
                   if other['kind'] == 'primary' and other['item'] and same(other['item'])]
        review = matches[-1] if matches else None
    prompt = _first(reader, review['prompts']) if review else None
    loaded = diffs.load(reader, prompt, fence=True) if prompt else None
    if review is None:
        reason = 'no review of this attempt'
    elif prompt is None:
        reason = 'no review prompt'
    elif _regular(reader, prompt) is None:
        reason = 'unreadable (see warnings)'
    else:
        reason = _NOT_INLINED if ctx['kind'] == 'final_review' else 'no diff in the review prompt'
    reviews = [review] if review else []
    if review and review['kind'] == 'primary' and review['attempt'] is not None:
        reviews += [other for other in ctx['reviews']
                    if other['kind'] == 'shadow' and other['attempt'] == review['attempt']]
    key = run_key(reader.path)
    origin = dict(kind='execution', id=name, which='reviewed', execution=review['execution'] if review else None,
                  attempt=review['attempt'] if review else item['attempt'], file=prompt)
    return _diff_detail(reader, key, origin, _diff_sources(reader, key, ctx), loaded, reason, reviews)


def final_review_diff(path, name, source='plan'):
    """DiffDetail of a final review: combined.diff, else the ```diff fence of its prompt, or None."""
    reader = _Read(path)
    if _kind(reader.path) != 'plan':
        return None
    ctx = _diff_final(reader, name)
    if ctx is None:
        return None
    base, summary = ctx['dir'], ctx['summary']
    candidates = [(base + '/combined.diff', False)]
    candidates += [(review['prompts'][0], True) for review in reversed(ctx['reviews'])] + [(base + '/qa.input', True)]
    loaded = rel = None
    for rel, fence in candidates:
        if os.path.lexists(reader.path / rel):
            loaded = diffs.load(reader, rel, fence)
            if loaded is not None:
                break
    prompted = any(os.path.lexists(reader.path / candidate) for candidate, fence in candidates if fence)
    key = run_key(reader.path)
    origin = dict(kind='final_review', id=name, which='reviewed', execution=summary['execution'], attempt=None,
                  file=rel if loaded is not None else None)
    review = _diff_review('final', None, None, summary['spec'], summary['verdict'], base + '/qa.last', [],
                          summary['execution'])
    return _diff_detail(reader, key, origin, _diff_sources(reader, key, ctx), loaded,
                        _NOT_INLINED if prompted else 'no review prompt yet', [review])


# --- findings: every review's findings of a run, for the stage's marks ---------------------

def _finding_id(review, index):
    """A Finding's id: its review's execution (its kind when it has none) and its place in that reply."""
    return f"{review['execution'] or review['kind']}:{index}"


def _findings_of(reader, review):
    listed = findings.listing(reader.text(review['last'])) if review['last'] else None
    return [dict(id=_finding_id(review, index), severity=item['severity'], status=item['status'],
                 location=item['location'], summary=item['summary'])
            for index, item in enumerate(listed or [])]


def run_findings(path, source='plan'):
    """RunFindings: each review's Findings, without their text, by task and by final review; None if no run."""
    reader = _Read(path)
    kind = _kind(reader.path)
    if kind is None:
        return None
    keys = ('kind', 'execution', 'attempt', 'spec', 'verdict')
    if kind == 'solo':
        task_ids, merges, accepted = ['solo'], [], None
    else:
        rows = _tasks(reader)
        task_ids, merges, accepted = [task['id'] for task in rows], _merge_pairs(rows), reader.line('accepted.integration')
    tasks = []
    for task_id in task_ids:
        ctx = _diff_task(reader, task_id)
        reviews = [dict({key: review[key] for key in keys}, findings=found)
                   for review in (ctx['reviews'] if ctx else []) for found in [_findings_of(reader, review)] if found]
        if reviews:
            tasks.append(dict(id=task_id, reviews=reviews))
    finals = []
    if kind == 'plan':
        items, published = executions.review_dirs(reader)[0], _published(reader)
        for index, item in enumerate(items[:_MAX_REVIEWS]):
            summary = _final_review(reader, item, index == 0, published)[0]
            _stamp_review(summary, accepted, merges)
            # the same review final_review_diff pins, so a Finding's id opens it there
            review = _diff_review('final', None, None, summary['spec'], summary['verdict'], item[3] + '/qa.last', [],
                                  summary['execution'])
            found = _findings_of(reader, review)
            if found:
                finals.append(dict(name=item[0], execution=summary['execution'], spec=summary['spec'],
                                   verdict=summary['verdict'], outdated=summary['outdated'],
                                   merged_since=summary['merged_since'], findings=found))
    return dict(run=run_key(reader.path), available=findings.available(), tasks=tasks, final_reviews=finals,
                truncated=reader.truncated, warnings=reader.warnings)


# --- files: who planned, changed, drifted into or conflicted on each file --------------------

_MAX_FILES, _MAX_TASKS, _MAX_OVERLAPS = 2000, 300, 5000


def _lines(reader, name):
    return [line.strip() for line in (reader.text(name) or '').splitlines() if line.strip()]


def _scope(path):
    return posixpath.normpath(path.replace('\\', '/'))


def _covers(scope, path):
    return path == scope or path.startswith(scope + '/')


def _overlap(left, right):
    """forge-schedule.py's overlap(): equal, one a dir prefix of the other, or either `.`."""
    return any(x == '.' or y == '.' or _covers(x, y) or _covers(y, x) for x in left for y in right)


def files_detail(path, source='plan'):
    """Files of a run: which tasks planned, touched, drifted into or conflicted on each one."""
    reader = _Read(path)
    kind = _kind(reader.path)
    if kind is None:
        return None
    truncated, rows, wave_map, deferrals = False, [], {}, []
    if kind == 'solo':
        attempts = _history(reader, 'attempts.tsv')
        status = _solo_status(reader)
        rows.append((dict(id='solo', status=status, files=[]), '', _solo_execs(reader, status, attempts)))
        wave_map = {'solo': 1}
    else:
        tasks = _tasks(reader)
        if len(tasks) > _MAX_TASKS:
            tasks, truncated = tasks[:_MAX_TASKS], True
        wave_map = _waves(reader)[1]
        for task in tasks:
            task['attempts'], task['attempt'] = _attempt_state(reader, f'tasks/{task["id"]}')
            rows.append((task, f'tasks/{task["id"]}', _task_execs(reader, task)))
        deferrals = _lines(reader, 'deferrals.txt')
    entries, raw = [], {}
    for task, base, execs in rows:
        attempts = []
        for item in execs:
            if item['role'] == 'qa' and not item['shadow']:
                prompt = _first(reader, _prompts(item, base))
                paths = diffs.touched_of(reader, prompt, fence=True) if prompt else None
                if paths is not None:
                    attempts.append(dict(attempt=item['attempt'], execution=item['id'], files=paths))
        planned = [_scope(entry) for entry in task['files']]
        raw[task['id']] = task['files']
        entry = dict(id=task['id'], wave=wave_map.get(task['id']), status=task['status'], planned=planned,
                     planned_all='.' in planned, attempts=attempts,
                     current=diffs.touched_of(reader, posixpath.join(base, 'changes.diff')) or [],
                     drift=_lines(reader, base + '/drift.txt') if base else [],
                     conflicts=_lines(reader, base + '/conflict.files') if base else [])
        if kind == 'solo':
            entry['existing'] = diffs.touched_of(reader, 'existing.diff') or []
        entries.append(entry)
    files, touched = {}, {}

    def add(path):
        if path not in files:
            if len(files) >= _MAX_FILES:
                return None
            files[path] = dict(path=path, dir=posixpath.dirname(path), planned_by=[], touched_by=[], drift_by=[],
                               conflict_by=[])
        return files[path]
    for entry in entries:
        touched[entry['id']] = set(entry['current']).union(*(item['files'] for item in entry['attempts']))
        for field, paths in (('touched_by', sorted(touched[entry['id']])), ('drift_by', entry['drift']),
                             ('conflict_by', entry['conflicts'])):
            for name in paths:
                item = add(name)
                truncated = truncated or item is None
                if item is not None and entry['id'] not in item[field]:
                    item[field].append(entry['id'])
    dirs = {}
    for entry in entries:
        for scope, given in zip(entry['planned'], raw[entry['id']]):
            if scope == '.':
                continue
            # A planned entry is a dir when it says so or holds a file the run changed.
            if given.endswith('/') or (scope not in files and any(_covers(scope, name) for name in files)):
                planners = dirs.setdefault(scope + '/', [])
                if entry['id'] not in planners:
                    planners.append(entry['id'])
            elif add(scope) is None:
                truncated = True
    for item in files.values():
        item['planned_by'] = [entry['id'] for entry in entries
                              if any(scope != '.' and _covers(scope, item['path']) for scope in entry['planned'])]
    overlaps = []
    for i, left in enumerate(entries):
        for right in entries[i + 1:]:
            planned = _overlap(left['planned'], right['planned'])
            shared = sorted(touched[left['id']] & touched[right['id']])
            if planned or shared:
                if len(overlaps) >= _MAX_OVERLAPS:
                    truncated = True
                    break
                overlaps.append(dict(a=left['id'], b=right['id'], planned=planned, touched=shared,
                                     same_wave=left['wave'] is not None and left['wave'] == right['wave']))
    return dict(run=run_key(reader.path), files=[files[name] for name in sorted(files)],
                planned_dirs=[dict(path=name, planned_by=dirs[name]) for name in sorted(dirs)], tasks=entries,
                overlaps=overlaps, deferrals=deferrals, truncated=truncated, warnings=reader.warnings)


# --- Fractal: the child tasks inside a task ---------------------------------------------------

def _fractal(path, task_id, read, *args):
    reader = _Read(path)
    kind = _kind(reader.path)
    if kind == 'solo' and task_id == 'solo':
        target = str(reader.path)
    elif kind == 'plan' and any(task['id'] == task_id for task in _tasks(reader)):
        # forge hands Fractal the task dir as --run-dir, resolved: its realpath names the Fractal task.
        target = os.path.realpath(reader.path / 'tasks' / task_id)
    else:
        return None
    return read(reader, run_key(reader.path), task_id, target, *args)


def fractal_detail(path, task_id, source='plan'):
    return _fractal(path, task_id, fractal.task)


def fractal_node(path, task_id, node, source='plan'):
    return _fractal(path, task_id, fractal.node, node)


def fractal_node_diff(path, task_id, node, source='plan'):
    return _fractal(path, task_id, fractal.node_diff, node)


# --- verification logs by path ---------------------------------------------------------------

LANE_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
LOG_TAIL = 65536               # the tail a log endpoint serves
LOG_SCAN_MAX = 64 << 20        # what is read to number lines or reach a line range
LOG_LINES = 400                # lines in one range
LOG_LINE_MAX = 4000            # characters kept of one line in a range


def _log_name(lane, k, retry=False):
    if retry:
        return 'verification.retry.log'
    if lane is None:
        return 'verification.log'
    return f'verification.lane.{lane}.log' if k is None else f'verification.lane.{lane}.run-{k}.log'


def _open_log(path, name):
    """The open log and its stat, or None: a missing file, a symlink, anything not regular."""
    try:
        fd = os.open(os.path.join(os.path.realpath(path), name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if stat.S_ISREG(info.st_mode):
            return os.fdopen(fd, 'rb'), info
    except OSError:
        pass
    os.close(fd)
    return None


def _newlines(stream, end):
    """How many newlines the first `end` bytes hold, or None past LOG_SCAN_MAX."""
    if end > LOG_SCAN_MAX:
        return None
    stream.seek(0)
    count = 0
    while end > 0:
        chunk = stream.read(min(1 << 20, end))
        if not chunk:
            break
        count += chunk.count(b'\n')
        end -= len(chunk)
    return count


def _log_tail(stream, info, name):
    offset = max(0, info.st_size - LOG_TAIL)
    stream.seek(offset)
    raw = stream.read(LOG_TAIL)
    if offset:
        # Start on a line: drop the line the cut went through, or at least a split character.
        cut = raw.find(b'\n')
        skip = cut + 1 if cut >= 0 else 0
        if cut < 0:
            while skip < min(3, len(raw)) and raw[skip] & 0xC0 == 0x80:
                skip += 1
        raw, offset = raw[skip:], offset + skip
    before = _newlines(stream, offset)
    return dict(name=name, size=info.st_size, mtime=float(info.st_mtime), tail=raw.decode('utf-8', 'replace'),
                tail_first_line=None if before is None else before + 1, truncated=offset > 0)


def _log_range(stream, info, name, first):
    """LOG_LINES lines from line `first`: (lines, whether more follow or a line or the read was cut)."""
    lines, number, read, cut = [], 1, 0, False
    while read < LOG_SCAN_MAX:
        part = stream.readline(LOG_LINE_MAX * 4)
        if not part:
            return dict(name=name, size=info.st_size, first_line=first, lines=lines, truncated=cut)
        read += len(part)
        line, long = part, False
        while not part.endswith(b'\n') and len(part) == LOG_LINE_MAX * 4 and read < LOG_SCAN_MAX:
            part, long = stream.readline(LOG_LINE_MAX * 4), True
            read += len(part)
        if number >= first:
            if len(lines) == LOG_LINES:
                return dict(name=name, size=info.st_size, first_line=first, lines=lines, truncated=True)
            text = line.rstrip(b'\n').rstrip(b'\r').decode('utf-8', 'replace')
            if long or len(text) > LOG_LINE_MAX:
                text, cut = text[:LOG_LINE_MAX].rstrip('\ufffd') + '…', True
            lines.append(text)
        number += 1
    return dict(name=name, size=info.st_size, first_line=first, lines=lines, truncated=True)


def verification_log(path, lane=None, k=None, first=None, retry=False, source='plan'):
    """The run's own verification log (with `retry`, its rerun's), a lane's or one run of a lane's:
    its last 64 KB from a line start, with that line's number, or LOG_LINES lines from line `first`.
    None (a 404) for a solo run, a name no lane can have, and a log that is missing, a symlink or
    not a regular file."""
    if _kind(path) != 'plan' or (lane is not None and not LANE_NAME.fullmatch(lane)):
        return None
    if (k is not None and (lane is None or k < 1)) or (first is not None and first < 1) or (retry and lane is not None):
        return None
    name = _log_name(lane, k, retry)
    opened = _open_log(path, name)
    if opened is None:
        return None
    stream, info = opened
    with stream:
        return _log_tail(stream, info, name) if first is None else _log_range(stream, info, name, first)


class Catalog:
    def __init__(self, plans=(), roots=(), registry=True, registry_dir=None):
        self.options = dict(plans=tuple(plans), roots=tuple(roots), registry=registry, registry_dir=registry_dir)

    def runs(self):
        runs = []
        for path, source in discover(**self.options):
            result = summarize(path, source)
            if _kind(path):
                runs.append(result)
        return sorted(runs, key=lambda item: item['updated'] if item['updated'] is not None else float('-inf'), reverse=True)

    def _find(self, key, read, *args):
        for path, source in discover(**self.options):
            if run_key(path) == key:
                result = read(path, *args, source=source)
                return result if _kind(path) else None
        return None

    def locate(self, key):
        """(path, kind) of the run with this key, kind 'plan' or 'solo'; None for an unknown key.

        The only way a write finds a run dir: the client names a key, never a path."""
        for path, _source in discover(**self.options):
            if run_key(path) == key:
                kind = _kind(path)
                return (str(path), kind) if kind else None
        return None

    def get(self, key):
        return self._find(key, detail)

    def task(self, key, task_id):
        return self._find(key, task_detail, task_id)

    def execution(self, key, name):
        return self._find(key, execution_detail, name)

    def final_review(self, key, name):
        return self._find(key, final_review_detail, name)

    def diff(self, key, task_id, which='current'):
        return self._find(key, task_diff, task_id, which)

    def execution_diff(self, key, name):
        return self._find(key, execution_diff, name)

    def transcript(self, key, name):
        return self._find(key, execution_transcript, name)

    def execution_checks(self, key, name):
        return self._find(key, execution_checks, name)

    def task_checks(self, key, task_id):
        return self._find(key, task_checks, task_id)

    def environment(self, key):
        return self._find(key, run_environment)

    def final_review_diff(self, key, name):
        return self._find(key, final_review_diff, name)

    def files(self, key):
        return self._find(key, files_detail)

    def findings(self, key):
        return self._find(key, run_findings)

    def fractal(self, key, task_id):
        return self._find(key, fractal_detail, task_id)

    def fractal_node(self, key, task_id, node):
        return self._find(key, fractal_node, task_id, node)

    def fractal_node_diff(self, key, task_id, node):
        return self._find(key, fractal_node_diff, task_id, node)

    def verification_log(self, key, lane=None, k=None, first=None, retry=False):
        return self._find(key, verification_log, lane, k, first, retry)

    def models(self):
        from forge_ui import models  # it reads runs through this module
        return models.collect(discover(**self.options))
