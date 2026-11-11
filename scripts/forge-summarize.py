#!/usr/bin/env python3
"""Generated task summaries. One side dispatch per task, and only when its facts changed.

  forge summarize PLAN [--task ID] [--force] [--json]

Facts are a compact JSON object, in this order: id, title, status, the attempt trail
(attempt, dwarf spec, verdict, findings count, worst severity, retry reason), the latest
review's finding titles and severities, known-issue titles, dependencies, and the tasks
that wait on this one. The prompt gets that JSON capped at 8 KB. It never includes the
repository, the diff or the task prompt.

inputs_hash is the sha256 of the canonical facts (sorted keys, no whitespace). An existing
summary.json with the same hash is left in place, unless --force is given.

Exit codes: 0 when the plan was read, including when every task was skipped; 2 usage;
3 when PLAN is not a directory.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time

import fcntl

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_side  # noqa: E402

# The facts the model sees. 8 KB, the same cut forge uses for a reply it carries forward.
FACTS_CAP = 8 * 1024
# A reply whose text is longer than this is rejected. The prompt asks for 3 to 5 lines.
TEXT_LINES = 8

_CONTRACT = None
_BULLET = re.compile(r'^(?:[-*+]|[0-9]{1,3}[.)])\s+')
_TAG = re.compile(r'\b(?:P[0-3]|CONFIRMED|PLAUSIBLE|UNSTATED)\b')
_LOCATION = re.compile(r'[\w./\\-]+\.\w+:\d+(?:[-:]\d+)?')
_MARKUP = re.compile(r'[*_`\[\]]+')

_INSTRUCTIONS = (
    'Reply with JSON only, no other text:\n'
    '{"text": "<3 to 5 short lines: what the task is for, what each attempt did, '
    'why it was retried, where it stands>", "sentence": "<one sentence>"}\n'
    '\n'
    'Use only the facts given. Invent nothing. Do not restate the live "Now" facts.\n'
    '\n'
    'Facts:\n'
)


def _contract():
    """forge-contract.py, loaded by path: the file name is not importable."""
    global _CONTRACT
    if _CONTRACT is None:
        path = Path(__file__).resolve().parent / 'forge-contract.py'
        spec = importlib.util.spec_from_file_location('forge_contract_summary', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CONTRACT = module
    return _CONTRACT


def _safe(task) -> bool:
    """A single path component. `..` or a slash would leave the task directory."""
    return (isinstance(task, str) and bool(task) and task not in ('.', '..')
            and '/' not in task and '\\' not in task and '\0' not in task)


def _text(path: Path):
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _lines(path: Path) -> list:
    text = _text(path)
    if not text:
        return []
    return [line for line in text.splitlines() if line and not line.startswith('#')]


def _atomic(path: Path, text: str) -> None:
    """Replace `path` with `text`. A crash leaves the previous file in place."""
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


def _tasks(plan: Path) -> tuple:
    """`(order, rows)` from tasks.tsv. A repeated id keeps its first row."""
    order, rows = [], {}
    for line in _lines(plan / 'tasks.tsv'):
        cells = line.split('\t')
        ident = cells[0].strip()
        if not ident or ident in rows:
            continue
        rows[ident] = cells
        order.append(ident)
    return order, rows


def _cell(cells: list, index: int) -> str:
    return cells[index].strip() if index < len(cells) else ''


def _deps(cell: str) -> list:
    if not cell or cell == '-':
        return []
    found = []
    for item in cell.split(','):
        item = item.strip()
        if item and item != '-' and item not in found:
            found.append(item)
    return found


def _title_of(finding) -> str:
    """The finding's first line, without the marker, the severity tag or the location."""
    first = finding.text.split('\n', 1)[0]
    first = _contract().split_facts(first)[0]
    first = _BULLET.sub('', first.strip())
    first = re.sub(r'^#{1,6}\s+', '', first)
    first = _TAG.sub('', first)
    first = _LOCATION.sub('', first)
    first = _MARKUP.sub('', first)
    first = first.strip(' \t—–-:`*|')
    first = ' '.join(first.split())
    if first:
        return first
    return ' '.join(finding.text.split())[:120]


def _severity(finding):
    if type(finding.severity) is int and 0 <= finding.severity <= 3:
        return 'P%d' % finding.severity
    return None


def _parsed(text: str) -> list:
    if not text:
        return []
    return list(_contract().parse_findings(text))


def _worst(findings: list):
    ranks = [finding.severity for finding in findings if type(finding.severity) is int]
    if not ranks:
        return None
    return 'P%d' % min(ranks)


def _attempt_rows(task_dir: Path) -> list:
    """One dict per attempts.tsv row: epoch, attempt, verdict (the status column), dwarf."""
    rows = []
    for line in _lines(task_dir / 'attempts.tsv'):
        parts = line.split('\t')
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        dwarf = parts[4].strip() if len(parts) > 4 else ''
        rows.append({
            'epoch': int(parts[0]) if parts[0].isdigit() else None,
            'attempt': int(parts[1]),
            'verdict': parts[2].strip() or None,
            'dwarf': dwarf or None,
        })
    return rows


def _reasons(task_dir: Path) -> dict:
    """`{attempt: source}` from retry_sources.tsv. The last row for an attempt wins."""
    found = {}
    for line in _lines(task_dir / 'retry_sources.tsv'):
        parts = line.split('\t')
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        source = parts[1].strip()
        if source:
            found[int(parts[0])] = source
    return found


def _shadow(path: Path) -> bool:
    return _json(path / 'metrics.json').get('shadow') is True


def _number(value):
    """A finite float, or None. A bool is not a timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    if not math.isfinite(value):
        return None
    return value


def _finished(path: Path, started: float):
    """Wall time the review finished, or None when metrics never closed it.

    begin() stores wall_started. finish() adds elapsed_s.total, the monotonic
    seconds until the dispatch ended, so wall_started plus that total is the
    completion. date +%s then truncates it into the attempt log.
    """
    metrics = _json(path / 'metrics.json')
    elapsed = metrics.get('elapsed_s')
    total = _number(elapsed.get('total') if isinstance(elapsed, dict) else None)
    if total is None or total < 0:
        return None
    # A metrics file from begin() has no total. A zero total with no exit is the
    # same unfinished state, not a review that ended on its start instant.
    if total == 0 and metrics.get('exit_code') is None:
        return None
    return started + total


def _log_mtime(task_dir: Path):
    """When attempts.tsv was last appended, or None. Sub-second when the filesystem gives it."""
    try:
        return (task_dir / 'attempts.tsv').stat().st_mtime
    except OSError:
        return None


def _open_attempt(rows: list, task_dir: Path):
    """The attempt a review belongs to when no attempts.tsv row owns it.

    The counter advances before the attempt starts. record_attempt appends the
    row when it ends. Until that append, the counter is this attempt and the
    last row is the previous one. Once the counter has caught up, a review that
    was never archived belongs to the last logged attempt.
    """
    current = _counter(task_dir)
    last = rows[-1]['attempt'] if rows else None
    if current is not None and (last is None or current > last):
        return current
    return last


def _review_end(item: dict) -> float:
    """When this review was over. Without a finish, the start is the only bound."""
    if item['finished'] is not None:
        return item['finished']
    return item['started']


def _after_append(started: float, rows: list, task_dir: Path) -> bool:
    """True when the review started after attempts.tsv was last appended.

    record_attempt runs after the review, so a start later than that write is not
    one of the logged rows. It belongs to the attempt the counter already holds.
    A mtime that is only the truncated epoch cannot say: date +%s drops the
    fraction, and a one-second timestamp does too. This is the whole log, not
    only its last row: an earlier row in the same second was written before the
    final append as well.
    """
    written = _log_mtime(task_dir)
    if written is None or not started > written:
        return False
    if not written.is_integer():
        return True
    last = None
    for row in rows:
        if row['epoch'] is not None:
            last = row['epoch']
    return last is None or written > last


def _append_rejects(epoch: int, started: float, finished, task_dir: Path, ahead: bool) -> bool:
    """The last logged row does not own this review.

    Its second can also hold the start of the next attempt, whose row is still
    missing. The same rules as `_after_append`, for a one-second mtime that the
    coarser check cannot decide: a review that did not finish inside the second
    then belongs to the open attempt.
    """
    written = _log_mtime(task_dir)
    started_before_append = written is not None and started <= written
    if written is not None and written > epoch and not started_before_append:
        return True
    if ahead and finished is None and started > epoch and not started_before_append:
        return True
    return False


def _matching_group(rows: list, mark: float):
    """`[start, end)` of the attempts logged in the first second that contains `mark`.

    A row at truncated epoch E owns a review that finished before the next second
    (`mark < E + 1`). Every later row shares that test until its own epoch is
    larger, so the earliest matching second is the one the review finished in.
    Rows logged in that same second stay in the group; the next second does not.
    """
    start = None
    epoch = None
    for index, row in enumerate(rows):
        row_epoch = row['epoch']
        if row_epoch is None or not mark < row_epoch + 1:
            continue
        start = index
        epoch = row_epoch
        break
    if start is None:
        return None
    end = start + 1
    while end < len(rows) and rows[end]['epoch'] == epoch:
        end += 1
    return start, end


def _slot(mark: float, epoch: int, count: int) -> int:
    """Which of `count` attempts logged at `epoch` a review that ended at `mark` is.

    One row is that row. Several attempts recorded in one second are ordered in
    the file, and each review is recorded just after it finishes, so a finish
    later in the second is a later attempt. The second is split into `count`
    equal parts. A finish before the second began is the first of them.
    """
    if count <= 1:
        return 0
    offset = mark - epoch
    if offset <= 0:
        return 0
    if offset >= 1:
        return count - 1
    slot = int(offset * count)
    if slot >= count:
        return count - 1
    return slot


def _clusters(items: list) -> list:
    """Reviews one attempt could own, in start order.

    The next attempt's review starts after this attempt's review has finished:
    record_attempt sits between them. Overlapping reviews stay one attempt.
    """
    clusters = []
    ends = []
    for item in items:
        end = _review_end(item)
        if not clusters or ends[-1] <= item['started']:
            clusters.append([item])
            ends.append(end)
        else:
            clusters[-1].append(item)
            if end > ends[-1]:
                ends[-1] = end
    return clusters


def _place_clusters(clusters: list, epoch: int, count: int) -> list:
    """Row offset within the shared second for each cluster.

    One cluster per row follows file order: the earlier review is the earlier
    attempt, whichever part of the second it finished in. A single cluster uses
    where it finished. Fewer clusters than rows do too, without a later cluster
    landing before an earlier one. Extras share the last row.
    """
    if len(clusters) == count:
        return list(range(count))
    if len(clusters) > count:
        return list(range(count - 1)) + [count - 1] * (len(clusters) - count + 1)
    chosen = []
    used = set()
    for cluster in clusters:
        slot = _slot(max(item['mark'] for item in cluster), epoch, count)
        if chosen:
            slot = max(slot, chosen[-1])
        while slot in used and slot + 1 < count:
            slot += 1
        if slot in used:
            slot = chosen[-1]
        used.add(slot)
        chosen.append(slot)
    return chosen


def _assign_timed(items: list, rows: list, task_dir: Path) -> dict:
    """`{id(item): attempt}` for reviews with no dataset.json attempt_now.

    Items are in start order. An attempt the log does not own yet is the open
    attempt from the counter.
    """
    opened = _open_attempt(rows, task_dir)
    last = rows[-1]['attempt'] if rows else None
    ahead = opened is not None and opened != last
    assigned = {}
    buckets = {}
    order = []
    for item in items:
        if _after_append(item['started'], rows, task_dir):
            assigned[id(item)] = opened
            continue
        group = _matching_group(rows, item['mark'])
        if group is None:
            assigned[id(item)] = opened
            continue
        if group not in buckets:
            buckets[group] = []
            order.append(group)
        buckets[group].append(item)
    for start, end in order:
        members = buckets[(start, end)]
        epoch = rows[start]['epoch']
        count = end - start
        if count == 1:
            for item in members:
                if end == len(rows) and _append_rejects(
                        epoch, item['started'], item['finished'], task_dir, ahead):
                    assigned[id(item)] = opened
                else:
                    assigned[id(item)] = rows[start]['attempt']
            continue
        # Two completed attempts can share the truncated second. The first row's
        # window contains every review that finished in it, so taking that row
        # would give the later review the earlier attempt's findings.
        clusters = _clusters(members)
        for cluster, offset in zip(clusters, _place_clusters(clusters, epoch, count)):
            index = start + offset
            for item in cluster:
                if index == len(rows) - 1 and _append_rejects(
                        epoch, item['started'], item['finished'], task_dir, ahead):
                    assigned[id(item)] = opened
                else:
                    assigned[id(item)] = rows[index]['attempt']
    return assigned


def _reviews(task_dir: Path, rows: list) -> dict:
    """`{attempt: qa.last text}` from attempts/qa-*/, newest start wins. Shadow reviews are skipped.

    dataset.json's attempt_now wins when a capture wrote one. Production archives
    do not persist it, so the attempt log is the fallback. Its epoch is date +%s
    at the end of the attempt, which drops the fraction: a review that starts at
    E.2 and finishes at E.8 is stored as E. Comparing the raw start with <= E
    skips that row and takes the next attempt. The finish, when metrics closed,
    matches a row when it falls before the next second. Without a finish, the
    start uses that window.

    The live qa.last is the review still in the task dir. When no archive holds the same
    text, it belongs to the open attempt when the counter is ahead of the log, and
    otherwise to the last recorded attempt (a review that was not archived).
    """
    items = []
    root = task_dir / 'attempts'
    if root.is_dir():
        for path in root.iterdir():
            if not path.is_dir() or not path.name.startswith('qa-') or _shadow(path):
                continue
            text = _text(path / 'qa.last')
            if text is None:
                continue
            pinned = _json(path / 'dataset.json').get('attempt_now')
            if type(pinned) is not int or pinned <= 0:
                pinned = None
            started = _number(_json(path / 'metrics.json').get('wall_started'))
            finished = _finished(path, started) if started is not None else None
            if started is None:
                if pinned is None:
                    continue
                try:
                    started = path.stat().st_mtime
                except OSError:
                    started = 0.0
                finished = None
            items.append({
                'started': started,
                'finished': finished,
                'mark': finished if finished is not None else started,
                'name': path.name,
                'text': text,
                'pinned': pinned,
            })
    items.sort(key=lambda item: (item['started'], item['name']))
    assigned = _assign_timed([item for item in items if item['pinned'] is None], rows, task_dir)
    found = {}
    for item in items:
        number = item['pinned'] if item['pinned'] is not None else assigned.get(id(item))
        if number is None:
            continue
        found[number] = item['text']
    live = _text(task_dir / 'qa.last')
    if live is not None and all(text != live for text in found.values()):
        number = _open_attempt(rows, task_dir)
        if number is not None:
            found[number] = live
    return found


def _counter(task_dir: Path):
    text = _text(task_dir / 'attempt')
    if text and text.strip().isdigit():
        return int(text.strip())
    return None


def _latest(task_dir: Path, rows: list, reviews: dict):
    """`(attempt, text)` of the latest review, or `(None, None)` when the task has none."""
    live = _text(task_dir / 'qa.last')
    if live is not None:
        # The same text can sit on an older archive and on the latest attempt. The finding
        # refs name the newest attempt that still holds it.
        matches = [number for number, text in reviews.items() if text == live]
        if matches:
            return max(matches), live
        return _open_attempt(rows, task_dir), live
    if not reviews:
        return None, None
    number = max(reviews)
    return number, reviews[number]


def _finding_items(text) -> list:
    return [{'title': _title_of(finding), 'severity': _severity(finding)} for finding in _parsed(text)]


def _known_titles(task_dir: Path) -> list:
    return [_title_of(finding) for finding in _parsed(_text(task_dir / 'known_issues.md') or '')]


def collect(plan: Path, task: str) -> tuple:
    """`(facts, sources)` for one task. Facts keys are in the order the prompt shows them.

    Sources name every attempt row and each finding of the latest review, as
    `{kind: attempt, ref: n}` and `{kind: finding, ref: attempt:title}`.
    """
    order, rows = _tasks(plan)
    cells = rows.get(task, [])
    task_dir = plan / 'tasks' / task
    attempts = _attempt_rows(task_dir)
    reasons = _reasons(task_dir)
    reviews = _reviews(task_dir, attempts)
    latest_n, latest_text = _latest(task_dir, attempts, reviews)
    trail = []
    for row in attempts:
        review = reviews.get(row['attempt'])
        parsed = _parsed(review) if review is not None else None
        trail.append({
            'attempt': row['attempt'],
            'dwarf': row['dwarf'],
            'verdict': row['verdict'],
            'findings': None if parsed is None else len(parsed),
            'worst': None if parsed is None else _worst(parsed),
            'reason': reasons.get(row['attempt']),
        })
    status = _text(task_dir / 'status')
    waiting = [other for other in order if other != task and task in _deps(_cell(rows[other], 1))]
    body = {
        'id': task,
        'title': _cell(cells, 6) or None,
        'status': status.strip() or None if status is not None else None,
        'attempts': trail,
        'findings': _finding_items(latest_text) if latest_text is not None else [],
        'known_issues': _known_titles(task_dir),
        'dependencies': _deps(_cell(cells, 1)),
        'waiting': waiting,
    }
    ref = '0' if latest_n is None else str(latest_n)
    sources = [{'kind': 'attempt', 'ref': str(row['attempt'])} for row in trail]
    sources += [{'kind': 'finding', 'ref': '%s:%s' % (ref, item['title'])} for item in body['findings']]
    return body, sources


def canonical(body: dict) -> str:
    """Sorted keys, no whitespace. This is the string inputs_hash covers."""
    return json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _hash(body: dict) -> str:
    return hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()


def _stored_hash(path: Path):
    data = _json(path)
    value = data.get('inputs_hash')
    return value if isinstance(value, str) and value else None


def _prompt(body: dict) -> str:
    shown = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
    return _INSTRUCTIONS + forge_side.cap(shown, FACTS_CAP) + '\n'


def _reply(last):
    """`(text, sentence)` or a reason string. A bad reply names why it was refused."""
    raw = (last or '').strip().lstrip('\ufeff')
    if not raw:
        return 'not json'
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        return 'not json'
    if not isinstance(data, dict):
        return 'not json'
    text, sentence = data.get('text'), data.get('sentence')
    if not isinstance(text, str):
        return 'missing text'
    if not isinstance(sentence, str):
        return 'missing sentence'
    if len(text.splitlines()) > TEXT_LINES:
        return 'text over 8 lines'
    return text, sentence


def _result(task: str, status: str, reason) -> dict:
    return {'task': task, 'status': status, 'reason': reason}


def _lock(task_dir: Path):
    """Exclusive non-blocking lock on summary.lock. None when another summariser holds it.

    The caller closes the fd, which releases the lock. Two triggers of one task never
    dispatch together: the second finds the lock taken and returns busy.
    """
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(task_dir / 'summary.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _fail(task_dir: Path, task: str, reason: str) -> dict:
    """Keep the previous summary.json and record why this attempt did not replace it."""
    try:
        _atomic(task_dir / 'summary.error', reason + '\n')
    except OSError:
        pass
    return _result(task, 'skipped', reason)


def _write(task_dir: Path, task: str, text: str, sentence: str, spec: str, digest: str, sources: list) -> dict:
    body = {
        'text': text,
        'sentence': sentence,
        'model': spec,
        'generated_at': int(time.time()),
        'inputs_hash': digest,
        'sources': sources,
    }
    _atomic(task_dir / 'summary.json', json.dumps(body, ensure_ascii=False, indent=2) + '\n')
    try:
        (task_dir / 'summary.error').unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    return _result(task, 'done', None)


def summarize_task(plan: Path, task: str, force: bool) -> dict:
    """Summarise one task. Skips when the switch is off, the lock is held, or nothing changed.

    The switch is checked before the task directory is created, so a refusal leaves the
    plan untouched. It is checked again under the lock: the cap may have filled.
    """
    if not _safe(task):
        return _result(task, 'skipped', 'unsafe')
    ok, reason = forge_side.allowed('summary', plan, task)
    if not ok:
        return _result(task, 'skipped', reason)
    task_dir = plan / 'tasks' / task
    fd = _lock(task_dir)
    if fd is None:
        if (task_dir / 'summary.lock').exists():
            return _result(task, 'skipped', 'busy')
        return _result(task, 'skipped', 'error')
    try:
        ok, reason = forge_side.allowed('summary', plan, task)
        if not ok:
            return _result(task, 'skipped', reason)
        body, sources = collect(plan, task)
        digest = _hash(body)
        if not force and _stored_hash(task_dir / 'summary.json') == digest:
            return _result(task, 'skipped', 'unchanged')
        spec = forge_side.settings(plan)['summaries']['model']
        outcome = forge_side.run('summary', plan, task, _prompt(body), spec)
        if not outcome.get('ok'):
            return _fail(task_dir, task, outcome.get('reason') or 'error')
        parsed = _reply(outcome.get('last'))
        if isinstance(parsed, str):
            return _fail(task_dir, task, parsed)
        text, sentence = parsed
        return _write(task_dir, task, text, sentence, spec, digest, sources)
    finally:
        os.close(fd)


def _report(item: dict, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + '\n')
        return
    if item['reason']:
        print('%s %s %s' % (item['task'], item['status'], item['reason']))
    else:
        print('%s %s' % (item['task'], item['status']))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='forge summarize',
        description='Write a generated summary for each task, again only when its inputs changed.')
    parser.add_argument('plan', help='the plan directory')
    parser.add_argument('--task', help='summarise this task only')
    parser.add_argument('--force', action='store_true', help='dispatch even when the inputs are unchanged')
    parser.add_argument('--json', action='store_true', help='print one JSON object per task: task, status, reason')
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    plan = Path(args.plan)
    if not plan.is_dir():
        sys.stderr.write('forge summarize: not a plan dir: %s\n' % plan)
        return 3
    if args.task is not None and not _safe(args.task):
        sys.stderr.write('forge summarize: bad task id: %s\n' % args.task)
        return 2
    order, _ = _tasks(plan)
    chosen = [args.task] if args.task else list(order)
    for task in chosen:
        if task not in order:
            item = _result(task, 'skipped', 'unknown')
        else:
            item = summarize_task(plan, task, args.force)
        _report(item, args.json)
    return 0


if __name__ == '__main__':
    sys.exit(main())
