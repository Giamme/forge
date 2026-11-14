"""groups.tsv: a frozen set of tasks with a title and goal.

Two opt-in features share it: group review (one QA review covers several tasks) and
brief mode part 2 (a wave-scoped answer acts on a group). This module is only the data
layer: parse, validate, derive from waves.tsv, resolve the QA spec, freeze.

<plan>/groups.tsv has six tab-separated columns, `#` lines and empty lines ignored:

    id  review  qa  members  title  goal

`review` is task or group, `qa` a spec or `-`, `members` a comma list of task ids in
order, `title` and `goal` one line each (`-` when absent). Stdlib only.
"""
from __future__ import annotations

import hashlib
import os
import posixpath
import re
import tempfile
from collections import namedtuple
from pathlib import Path

Group = namedtuple('Group', 'id review qa members title goal')

HEADER = '# id\treview\tqa\tmembers\ttitle\tgoal'
FROZEN_REASON = "groups.tsv or a member's deps/files changed since plan; run plan again"
ID_RE = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
SWEEP_ID_RE = re.compile(r'sweep-[0-9]+')
REVIEWS = ('task', 'group')
RANK = {'high': 3, 'medium': 2, 'low': 1}


class GroupError(Exception):
    """A malformed groups.tsv or a broken group rule; the message is one line."""


# --- tasks.tsv ----------------------------------------------------------------

def _tasks(plan) -> dict:
    """tasks.tsv rows by id, in file order: id deps difficulty files dwarf qa title."""
    path = Path(plan) / 'tasks.tsv'
    try:
        text = path.read_text()
    except OSError as error:
        raise GroupError('tasks.tsv could not be read: %s' % error.strerror)
    rows = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith('#'):
            continue
        row = line.split('\t')
        if len(row) != 7:
            raise GroupError('tasks.tsv line %d: needs seven columns, has %d' % (number, len(row)))
        rows[row[0]] = row
    return rows


def _list(value: str) -> list:
    return [item for item in value.split(',') if item and item != '-']


def overlap(a: str, b: str) -> bool:
    """forge-schedule.py's overlap, ported exactly: a declared directory covers the
    paths beneath it, and `.` covers everything. `-` declares nothing."""
    def paths(value: str) -> list:
        return [posixpath.normpath(p) for p in value.split(',') if p and p != '-']

    return any(
        x == '.' or y == '.' or x == y or x.startswith(y + '/') or y.startswith(x + '/')
        for x in paths(a) for y in paths(b)
    )


# --- groups.tsv ---------------------------------------------------------------

def load(plan) -> list:
    path = Path(plan) / 'groups.tsv'
    if not path.is_file():
        return []
    groups = []
    seen = set()
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip() or line.startswith('#'):
            continue
        row = line.split('\t')
        where = 'groups.tsv line %d' % number
        if len(row) != 6:
            raise GroupError('%s: needs six columns, has %d' % (where, len(row)))
        gid, review, qa, members, title, goal = row
        if not ID_RE.fullmatch(gid):
            raise GroupError('%s: bad group id %r (use [a-z0-9][a-z0-9-]{0,39})' % (where, gid))
        if gid in seen:
            raise GroupError('%s: duplicate group id %s' % (where, gid))
        seen.add(gid)
        if review not in REVIEWS:
            raise GroupError('%s: review must be task or group, not %r' % (where, review))
        if not qa.strip():
            raise GroupError('%s: qa is empty (use - for none)' % where)
        names = [m.strip() for m in members.split(',')]
        if members.strip() in ('', '-') or any(not m for m in names):
            raise GroupError('%s: members is empty or has an empty entry' % where)
        groups.append(Group(gid, review, qa, tuple(names), title or '-', goal or '-'))
    return groups


def _is_sweep(plan, task: str) -> bool:
    return bool(SWEEP_ID_RE.fullmatch(task)) or (Path(plan) / 'tasks' / task / 'sweep.json').is_file()


def _reaches(tasks: dict, start: str, targets: set):
    """A target task reachable from start through deps (any task, transitively), or None.
    Returns (target, path) where path lists the tasks passed through."""
    stack = [(dep, []) for dep in reversed(_list(tasks[start][1]))]
    seen = {start}
    while stack:
        task, via = stack.pop()
        if task in seen:
            continue
        seen.add(task)
        if task in targets:
            return task, via
        if task in tasks:
            stack.extend((dep, via + [task]) for dep in reversed(_list(tasks[task][1])))
    return None


def validate(plan, groups, max_tasks=6) -> None:
    tasks = _tasks(plan)
    owner = {}
    ids = set()
    for g in groups:
        if g.id in ids:
            raise GroupError('group %s: duplicate group id' % g.id)
        ids.add(g.id)
        for member in g.members:
            if member not in tasks:
                raise GroupError('group %s: member %s is not in tasks.tsv' % (g.id, member))
            if member in owner:
                raise GroupError('group %s: task %s is in two groups (%s and %s)'
                                 % (g.id, member, owner[member], g.id))
            owner[member] = g.id
        if g.id in tasks:
            raise GroupError('group %s: the id is also a task id' % g.id)
        if g.review != 'group':
            continue
        if len(g.members) < 2:
            raise GroupError('group %s: a review group needs at least 2 members' % g.id)
        if len(g.members) > max_tasks:
            raise GroupError('group %s: %d members, at most %d allowed' % (g.id, len(g.members), max_tasks))
        for member in g.members:
            others = set(g.members) - {member}
            hit = _reaches(tasks, member, others)
            if hit:
                other, via = hit
                how = 'through ' + ', '.join(via) if via else 'directly'
                raise GroupError('group %s: member %s depends on member %s (%s)' % (g.id, member, other, how))
        for i, a in enumerate(g.members):
            for b in g.members[i + 1:]:
                if overlap(tasks[a][3], tasks[b][3]):
                    raise GroupError('group %s: members %s and %s declare overlapping files' % (g.id, a, b))
        for member in g.members:
            if _is_sweep(plan, member):
                raise GroupError('group %s: member %s is a sweep task' % (g.id, member))


def derive(plan, review='task', max_tasks=6) -> list:
    """One group per computed wave with at least two tasks (w1, w2, ...). A wave larger
    than max_tasks splits into w3a, w3b, ... in tasks.tsv order. With review='group',
    high-difficulty tasks stay out and keep their own review."""
    if review not in REVIEWS:
        raise GroupError('review must be task or group, not %r' % review)
    if max_tasks < 2:
        raise GroupError('max tasks must be at least 2')
    tasks = _tasks(plan)
    path = Path(plan) / 'waves.tsv'
    try:
        text = path.read_text()
    except OSError as error:
        raise GroupError('waves.tsv could not be read: %s' % error.strerror)
    waves = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith('#'):
            continue
        row = line.split('\t')
        if len(row) != 2 or not row[0].isdigit() or row[1] not in tasks:
            raise GroupError('waves.tsv line %d: needs wave<TAB>task with a task from tasks.tsv' % number)
        waves.setdefault(int(row[0]), set()).add(row[1])
    groups = []
    for wave in sorted(waves):
        members = [t for t in tasks if t in waves[wave]]
        if review == 'group':
            members = [t for t in members if tasks[t][2] != 'high']
        chunks = [members[i:i + max_tasks] for i in range(0, len(members), max_tasks)]
        for index, chunk in enumerate(chunks):
            if len(chunk) < 2:
                continue
            suffix = chr(ord('a') + index) if len(chunks) > 1 else ''
            groups.append(Group('w%d%s' % (wave, suffix), review, '-', tuple(chunk), '-', '-'))
    return groups


def _cell(value: str) -> str:
    value = ' '.join(str(value).split())
    return value or '-'


def write(plan, groups) -> None:
    lines = [HEADER] + ['\t'.join([g.id, g.review, _cell(g.qa), ','.join(g.members),
                                   _cell(g.title), _cell(g.goal)]) for g in groups]
    _atomic(Path(plan) / 'groups.tsv', '\n'.join(lines) + '\n')


def _atomic(path: Path, text: str) -> None:
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    try:
        # mkstemp makes the file 0600, and os.replace keeps that: give it the mode a plain open would.
        mask = os.umask(0)
        os.umask(mask)
        os.fchmod(fd, 0o666 & ~mask)
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def resolve_qa(plan, groups) -> list:
    """For review=group with qa `-`: the qa of the member with the highest difficulty
    (high > medium > low, ties to the first listed), as tasks.tsv has it after plan."""
    tasks = _tasks(plan)
    out = []
    for g in groups:
        if g.review == 'group' and g.qa == '-':
            best = None
            for member in g.members:
                if member in tasks and (best is None or RANK.get(tasks[member][2], 0) > RANK.get(tasks[best][2], 0)):
                    best = member
            if best is not None:
                g = g._replace(qa=tasks[best][5] or '-')
        out.append(g)
    return out


# --- freeze -------------------------------------------------------------------

def freeze_text(plan, groups) -> str:
    """One line per group with all six columns, each followed by one line per member:
    member<TAB>deps<TAB>files. A member's dwarf column is not part of it."""
    tasks = _tasks(plan)
    lines = []
    for g in groups:
        lines.append('\t'.join([g.id, g.review, g.qa, ','.join(g.members), g.title, g.goal]))
        for member in g.members:
            row = tasks.get(member)
            lines.append('\t'.join([member, row[1], row[3]]) if row else '%s\t<missing>\t<missing>' % member)
    return '\n'.join(lines) + '\n' if lines else ''


def fingerprint(plan, groups) -> str:
    return hashlib.sha256(freeze_text(plan, groups).encode()).hexdigest()


def freeze(plan) -> None:
    plan = Path(plan)
    groups = load(plan)
    frozen = plan / 'groups.frozen'
    if not groups:
        try:
            frozen.unlink()
        except FileNotFoundError:
            pass
        return
    _atomic(frozen, fingerprint(plan, groups) + '\n')


def check_frozen(plan):
    """None when groups.tsv and its members still match groups.frozen (or neither
    exists); otherwise the reason."""
    plan = Path(plan)
    frozen = plan / 'groups.frozen'
    try:
        groups = load(plan)
        if not groups:
            return FROZEN_REASON if frozen.exists() else None
        if not frozen.is_file():
            return FROZEN_REASON
        return None if frozen.read_text().strip() == fingerprint(plan, groups) else FROZEN_REASON
    except (GroupError, OSError):
        return FROZEN_REASON


# --- lookups ------------------------------------------------------------------

def group_of(plan, task):
    for g in load(plan):
        if task in g.members:
            return g
    return None


def members_in_review_groups(plan) -> set:
    return {m for g in load(plan) if g.review == 'group' for m in g.members}
