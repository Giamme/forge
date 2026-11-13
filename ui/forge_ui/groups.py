"""Groups of a plan: `<plan>/groups.tsv` and, for a group that is reviewed as one, `<plan>/groups/<gid>/`.

Read through the run reader (`text`, `rows`, `object`, `exists`). A missing file is an empty result.
A malformed one is a warning on `reader.warnings` and whatever rows could still be read: nothing here raises.

`groups.tsv` is `id review qa members title goal`, tab-separated, `#` comments and blank lines ignored.
`-` in a cell is `None`. `members` is a list of task ids. A `review=task` group is that identity only.
A `review=group` group also has the review state in its directory: `status`, `round`, `merged`,
`rounds.tsv` (`round epoch status implicated reviewer`), and the first line of `decision.md`.
`group()` adds the review itself: `attribution.json`, `members.tsv`, `qa.last`, `known_issues.md`,
`candidate.base` and `candidate.commit`, and the QA dispatch `executions.scan` reads from
`groups/<gid>/attempts/` — the same builder a task's QA dispatch uses.
"""
from __future__ import annotations

import math
import re

from forge_ui import executions


# The same shape forge_groups accepts, and the shape the group route matches.
GID = re.compile(r'[a-z0-9][a-z0-9-]{0,39}')
# Head and tail kept of a review text, as TaskDetail's texts and a final review's qa.last do
# (reader.excerpt: two bounded reads, the cut marked in reader.truncated).
_TEXT_HEAD = 20000
_TEXT_TAIL = 8000
_REVIEWS = ('task', 'group')


def _blank(value):
    """A stripped cell, or None for an empty one and for `-`."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ('', '-') else text


def _id_list(value):
    """A comma list of ids, or None for an empty cell and for `-`."""
    text = _blank(value)
    if text is None:
        return None
    items = [part.strip() for part in text.split(',') if part.strip()]
    return items or None


def _int(reader, label, value, what):
    text = (value or '').strip()
    if text in ('', '-'):
        return None
    try:
        return int(text)
    except (ValueError, OverflowError):
        reader.warn(label, f'{what} is not an integer')
        return None


def _rounds(reader, name):
    """rounds.tsv rows that parse. A bad row is warned and skipped."""
    result = []
    for label, row in reader.rows(name, 5):
        if len(row) != 5:
            reader.warn(label, 'expected 5 columns')
        try:
            number, epoch = int(row[0].strip()), float(row[1].strip())
            if not math.isfinite(epoch):
                raise ValueError()
        except (ValueError, OverflowError):
            reader.warn(label, 'invalid round or epoch')
            continue
        result.append(dict(round=number, epoch=epoch, status=_blank(row[2]),
                           implicated=_id_list(row[3]), reviewer=_blank(row[4])))
    return result


def _state(reader, gid):
    """The review state a review=group group carries. Missing files are null, not an error."""
    prefix = f'groups/{gid}'
    rounds = _rounds(reader, prefix + '/rounds.tsv')
    last = rounds[-1] if rounds else None
    return dict(status=reader.word(prefix + '/status'), round=reader.number(prefix + '/round', int),
                merged=reader.exists(prefix + '/merged'),
                implicated=None if last is None else last['implicated'],
                decision=reader.line(prefix + '/decision.md'), rounds=rounds)


def groups(reader) -> list:
    """One dict per groups.tsv row, in file order. `[]` when the plan has no groups.tsv."""
    if not reader.exists('groups.tsv'):
        return []
    found, seen = [], set()
    for label, row in reader.rows('groups.tsv', 6):
        if len(row) != 6:
            reader.warn(label, 'expected 6 columns')
        gid = row[0].strip()
        if not GID.fullmatch(gid):
            reader.warn(label, 'invalid group id')
            continue
        if gid in seen:
            reader.warn(label, 'duplicate group id')
            continue
        seen.add(gid)
        review = row[1].strip()
        if review not in _REVIEWS:
            reader.warn(label, 'review must be task or group')
        item = dict(id=gid, review=review or None, qa=_blank(row[2]), members=_id_list(row[3]),
                    title=_blank(row[4]), goal=_blank('\t'.join(row[5:])))
        if review == 'group':
            item.update(_state(reader, gid))
        found.append(item)
    return found


def _members_by_round(reader, name):
    """members.tsv grouped by round, in file order: [{round, members: [{task, commit, attempt}]}]."""
    rows = []
    for label, row in reader.rows(name, 4):
        raw = (row[0] or '').strip()
        number = _int(reader, label, row[0], 'round')
        if number is None:
            # `_int` already warned a cell that is not an integer. A blank one has not.
            if raw in ('', '-'):
                reader.warn(label, 'invalid round')
            continue
        task = row[1].strip()
        if not task or task == '-':
            reader.warn(label, 'missing task')
            continue
        rows.append(dict(round=number, task=task, commit=_blank(row[2]),
                         attempt=_int(reader, label, row[3], 'attempt')))
    grouped, index = [], {}
    for row in rows:
        bucket = index.get(row['round'])
        if bucket is None:
            bucket = index[row['round']] = dict(round=row['round'], members=[])
            grouped.append(bucket)
        bucket['members'].append(dict(task=row['task'], commit=row['commit'], attempt=row['attempt']))
    return grouped


def _flag(reader, name, index, item, key):
    value = item.get(key)
    if type(value) is bool:
        return value
    if value is not None:
        reader.warn(name, f'finding {index} {key} is not a boolean')
    return False


def _finding(reader, name, index, item):
    number = item.get('n')
    if type(number) is not int:
        reader.warn(name, f'finding {index} has no n')
        number = index
    line = item.get('line')
    if type(line) is not int:
        if line is not None:
            reader.warn(name, f'finding {index} line is not an integer')
        line = None
    tasks = item.get('tasks')
    if not (isinstance(tasks, list) and all(isinstance(part, str) for part in tasks)):
        reader.warn(name, f'finding {index} tasks is not a list of ids')
        tasks = []

    def text(key):
        value = item.get(key)
        return value if isinstance(value, str) and value else None

    return dict(n=number, severity=text('severity'), status=text('status'), file=text('file'), line=line,
                tasks=tasks, blocking=_flag(reader, name, index, item, 'blocking'),
                ambiguous=_flag(reader, name, index, item, 'ambiguous'))


def _strings(reader, name, obj, key):
    value = obj.get(key)
    if value is None:
        return []
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return value
    reader.warn(name, f'{key} is not a list of ids')
    return []


def _numbers(reader, name, obj, key):
    value = obj.get(key)
    if value is None:
        return []
    if isinstance(value, list) and all(type(part) is int for part in value):
        return value
    reader.warn(name, f'{key} is not a list of numbers')
    return []


def _attribution(reader, name):
    """attribution.json, or None when it is missing or not an attribution (warned)."""
    obj = reader.object(name)
    if obj is None:
        return None
    findings = obj.get('findings')
    if not isinstance(findings, list):
        reader.warn(name, 'not an attribution')
        return None
    parsed = []
    for index, item in enumerate(findings, 1):
        if not isinstance(item, dict):
            reader.warn(name, f'finding {index} is not an object')
            continue
        parsed.append(_finding(reader, name, index, item))
    number = obj.get('round')
    if type(number) is not int:
        if number is not None:
            reader.warn(name, 'round is not an integer')
        number = None
    result = dict(round=number, findings=parsed, implicated=_strings(reader, name, obj, 'implicated'),
                  ambiguous=_numbers(reader, name, obj, 'ambiguous'))
    blocking = obj.get('blocking')
    if type(blocking) is bool:
        result['blocking'] = blocking
    elif blocking is not None:
        reader.warn(name, 'blocking is not a boolean')
    return result


def _review_execution(reader, gid):
    """The group's QA dispatch, built the same way as a task's: the newest qa execution in its attempts/."""
    prefix = f'groups/{gid}'
    # REVIEWING is the only status on which the dispatch is still in flight. Any other, including a
    # missing status file, leaves an execution with no exit code orphaned rather than running.
    execs = executions.scan(reader, prefix + '/attempts', reader.word(prefix + '/status') == 'REVIEWING', ('qa',))
    return execs[-1] if execs else None


def group(reader, gid) -> dict | None:
    """One group, with its review, or None when `gid` is not a group of this plan.

    A gid that cannot name a group dir (`GID`) is None without a read. The dict is the `groups()`
    row plus `attribution`, `members_by_round`, `qa_last`, `known_issues`, `candidate` and
    `execution`. `qa_last` and `known_issues` go through `reader.excerpt`, the bounded reader the
    task and review inspectors use. `warnings` and `truncated` are that read's.
    """
    if not isinstance(gid, str) or not GID.fullmatch(gid):
        return None
    found = next((dict(item) for item in groups(reader) if item['id'] == gid), None)
    if found is None:
        return None
    prefix = f'groups/{gid}'
    found['attribution'] = _attribution(reader, prefix + '/attribution.json')
    found['members_by_round'] = _members_by_round(reader, prefix + '/members.tsv')
    found['qa_last'] = reader.excerpt(prefix + '/qa.last', _TEXT_HEAD, _TEXT_TAIL, 'qa_last')
    found['known_issues'] = reader.excerpt(prefix + '/known_issues.md', _TEXT_HEAD, _TEXT_TAIL, 'known_issues')
    found['candidate'] = dict(base=_blank(reader.line(prefix + '/candidate.base')),
                              commit=_blank(reader.line(prefix + '/candidate.commit')))
    found['execution'] = _review_execution(reader, gid)
    found['warnings'] = reader.warnings
    found['truncated'] = reader.truncated
    return found
