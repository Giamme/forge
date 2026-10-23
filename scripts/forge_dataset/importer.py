"""`forge dataset import`: capture runs that predate live capture, from dirs or archives.

It uses the same readers as live capture, so an execution imported here and later seen
live (or the reverse) collapses to one event by rid. What it cannot do is know which task
files an old execution wrote, because the begin() snapshot did not exist yet. So:

  - the newest task attempt gets the task dir's current files, flagged `legacy`;
  - earlier attempts get only what is immutable: their own dispatch dirs, the diff QA was
    shown (the last ```diff block of the QA prompt), and the status attempts.tsv logged.

No historical outcome is inferred from a file that a later attempt could have rewritten.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from . import (BUSY_TIMEOUT_S, DatasetError, config_runs_dir, data_home, dataset_dir, enabled,
               location_refused, put_blob, put_file, record, record_strict, repo_id, sha256, watch_blob_failures)
from . import harvest as h
from . import capture

SKIP_DIRS = {'.git', 'node_modules', 'capabilities', '.forge-worktrees'}
_DIFF_BLOCK = re.compile(r'```diff\n(.*?)\n```', re.S)


def _temp_roots() -> tuple:
    roots = {os.path.realpath(tempfile.gettempdir()), '/tmp', '/private/tmp', '/var/folders', '/private/var/folders'}
    return tuple(r.rstrip('/') + '/' for r in roots)


def is_temp(path: str | None) -> bool:
    if not path:
        return False
    real = os.path.realpath(path) + '/'
    # Agent scratchpads live under /tmp too, but a plan dir there still belongs to a real
    # repo; only a REPO in a temp dir marks a test run.
    return real.startswith(_temp_roots())


def walk_attempts(top: Path, onerror=None) -> list[Path]:
    found = []
    for directory, subdirs, _ in os.walk(top, onerror=onerror):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith('review-')]
        if os.path.basename(directory) == 'attempts':
            found.extend(Path(directory) / d for d in subdirs if '-' in d)
            subdirs[:] = []
    return sorted(found)


def walk_plans(top: Path, onerror=None) -> list[Path]:
    found = []
    for directory, subdirs, files in os.walk(top, onerror=onerror):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith('review-') and d != 'attempts']
        if 'tasks.tsv' in files:
            found.append(Path(directory))
    return sorted(found)


def walk_fractal_tasks(top: Path, onerror=None) -> list[Path]:
    found = []
    for directory, subdirs, files in os.walk(top, onerror=onerror):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith('review-')
                      and d not in ('attempts', 'steps', 'runtime')]
        if 'request.json' in files and 'nodes' in subdirs and Path(directory).parent.name == 'tasks':
            found.append(Path(directory))
    return sorted(found)


class Mapper:
    """Maps a path inside an extracted archive back to where the run originally lived.

    A run's legacy id hashes its original path, so importing an archive and importing
    the original dir must agree on it. An archive also brings the legacy ids it computed
    from the whole dir (`legacy`, by original path), since it leaves out files they hash.
    """

    def __init__(self):
        self.pairs = []
        self.legacy: dict[str, str] = {}

    def add(self, extracted_parent: Path, original_parent: Path) -> None:
        self.pairs.append((str(extracted_parent.resolve()), str(original_parent)))

    def __call__(self, path: Path | str) -> Path:
        text = str(Path(path).resolve())
        for extracted, original in self.pairs:
            if text == extracted or text.startswith(extracted + '/'):
                return Path(original + text[len(extracted):])
        return Path(text)


def _uid(run_dir: Path, mapper: Mapper, cache: dict) -> str:
    key = str(run_dir)
    if key not in cache:
        stored = h.first_line(run_dir / 'dataset.uid') or _live_uid(run_dir, mapper(run_dir))
        cache[key] = stored or _legacy_uid(run_dir, mapper)
    return cache[key]


def _legacy_uid(run_dir: Path, mapper: Mapper) -> str:
    original = mapper(run_dir)
    return mapper.legacy.get(str(original)) or h.legacy_uid_for(run_dir, original)


def _legacy_uids(run: Path) -> dict:
    """The legacy id an import of `run` gives each run dir in it that has no dataset.uid,
    keyed by its path relative to `run`.

    legacy_uid_for hashes the earliest attempt start anywhere below a run dir, `review-*`
    snapshots included, and an archive leaves those out: so the archive records the ids.
    """
    dirs = {run} | set(walk_plans(run)) | {t.parent.parent for t in walk_fractal_tasks(run)}
    dirs |= {h.locate(a.parent.parent, a.name.split('-', 1)[0])['run_dir'] for a in walk_attempts(run)}
    return {d.relative_to(run).as_posix(): h.legacy_uid_for(d, d) for d in sorted(dirs)
            if (d == run or run in d.parents) and not (d / 'dataset.uid').exists()}


def _live_uid(run_dir: Path, original: Path) -> str | None:
    """The id live capture gave this run, when it is still running where it was archived.

    An archive taken mid-run has no dataset.uid, while the same dir on disk got one from
    the first live begin(). The dir is the same run only if it still holds an execution
    from the archive: /tmp plan paths get reused by later, unrelated runs.
    """
    if original == run_dir.resolve() or not (original / 'dataset.uid').exists():
        return None
    for attempt in walk_attempts(run_dir)[:5]:
        if (original / attempt.relative_to(run_dir)).is_dir():
            return h.first_line(original / 'dataset.uid')
    return None


def _metrics(attempt: Path) -> dict:
    return h.read_json(attempt / 'metrics.json') or {}


def _window(attempt: Path) -> tuple[float, float]:
    metrics = _metrics(attempt)
    start = metrics.get('wall_started') or 0
    total = (metrics.get('elapsed_s') or {}).get('total') or 0
    return start, start + total


def legacy_task_events(tdir: Path, task_attempts: list[Path], prov_base: dict) -> list[dict]:
    """Task attempts of one task dir, oldest first; see the module docstring for what each gets."""
    events = []
    rows = []
    for line in (h.text(tdir / 'attempts.tsv') or '').splitlines():
        cells = line.split('\t')
        if len(cells) >= 3 and cells[0].isdigit():
            rows.append(dict(epoch=int(cells[0]), attempt=h.as_int(cells[1]), status=cells[2],
                             infra=cells[3] if len(cells) > 3 else ''))
    dispatches = []
    for path in sorted((tdir / 'attempts').glob('*-*')):
        role = path.name.split('-', 1)[0]
        if role in h.DISPATCH_ROLES:
            dispatches.append((_window(path)[0], role, path))
    ordered = sorted(task_attempts, key=lambda p: _window(p)[0])
    for index, attempt in enumerate(ordered):
        start, end = _window(attempt)
        # The task dir's files belong to the newest attempt only if it finished; an attempt
        # still running when the dir was copied is no owner of the files around it.
        finished = _metrics(attempt).get('exit_code') is not None and h.first_line(tdir / 'status') != 'RUNNING'
        latest = index == len(ordered) - 1 and finished
        # A dispatch belongs to the task attempt that started last before it. The next
        # attempt's start bounds the window even when this one was killed and never
        # recorded its own end.
        until = _window(ordered[index + 1])[0] if index + 1 < len(ordered) else float('inf')
        prov = dict(prov_base, files={} if latest else None, wall_ended=end or None)
        event = h.task_event(attempt, tdir, _metrics(attempt).get('exit_code'), prov, src='import')
        children = [p.name for (t, _, p) in dispatches if start <= t < until]
        row = next((r for r in rows if r['epoch'] >= int(start)), None)
        if row:
            rows.remove(row)
        data = event['data']
        data.update(legacy=True, children=children, logged=row)
        qa = [p for (_, role, p) in sorted(dispatches) if role == 'qa' and p.name in children]
        if qa:
            data['blobs'].pop('forge_note', None)
        if _metrics(attempt).get('exit_code') is None:
            continue   # unfinished: see import_tree
        if not latest:
            verdict = h.verdict_of(h.text(qa[-1] / 'qa.last')) if qa else None
            data['status'], data['status_source'] = ((row['status'], 'attempts.tsv') if row else
                                                     (verdict, 'qa_verdict') if verdict else (None, None))
            event['attempt'], data['attempt_source'] = ((row['attempt'], 'attempts.tsv') if row else
                                                        (index + 1, 'order'))
            blocks = _DIFF_BLOCK.findall(h.text(qa[-1] / 'qa.prompt') or '') if qa else []
            if blocks:
                diff = blocks[-1]
                data.update(h.diff_summary(diff), diff_source='qa_prompt')
                data['blobs']['diff'] = put_blob(diff, 'diff')
        else:
            event['attempt'] = h.as_int(h.first_line(tdir / 'attempt'))
            data['attempt_source'] = 'attempt_file'
        events.append(event)
    return events


def import_tree(top: Path, mapper: Mapper, args, counts: dict) -> list[tuple[list, str | None]]:
    """Every event under `top`, grouped per repo: [(events, repo)]."""
    batches = []
    uids = {}
    reported: set = set()

    def unreadable(error):
        # os.walk skips a directory it cannot list; import_run must not read that as empty.
        if getattr(args, 'report_unreadable', False) and error.filename not in reported:
            reported.add(error.filename)
            counts.setdefault('failed', []).append('%s: cannot read: %s' % (error.filename, error.strerror or error))

    onerror = unreadable
    if getattr(args, 'report_unreadable', False):
        # The readers below (h.read_json, h.text, h.first_line) turn a file they cannot open into
        # "absent": a finished attempt whose metrics.json is unreadable would pass as unfinished.
        # So every file the walkers can reach must open before any of it is trusted.
        for directory, subdirs, files in os.walk(top, onerror=onerror):
            subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith('review-')]
            for name in files:
                full = os.path.join(directory, name)
                try:
                    if not stat.S_ISREG(os.lstat(full).st_mode):   # links and fifos are never run data
                        continue
                    with open(full, 'rb'):
                        pass
                except OSError as error:
                    unreadable(error)
    plans = {p for p in walk_plans(top, onerror)}
    by_task: dict = {}
    for attempt in walk_attempts(top, onerror):
        role = attempt.name.split('-', 1)[0]
        root = attempt.parent.parent
        ctx = h.locate(root, role)
        repo = ctx.get('repo')
        if is_temp(repo) and not args.include_temp:
            counts['skipped_temp'] = counts.get('skipped_temp', 0) + 1
            continue
        uid = _uid(ctx['run_dir'], mapper, uids)
        stored = h.read_json(attempt / 'dataset.json')
        prov = stored or dict(run=uid, run_id=ctx['run_id'], mode=ctx['mode'], scope=ctx['scope'],
                              task=ctx['task'], repo=repo, link=ctx['link'], files=None)
        prov['root'] = str(mapper(root))
        if role == 'task' and stored is None:
            by_task.setdefault(str(root), (root, prov, repo, []))[3].append(attempt)
            continue
        metrics = _metrics(attempt)
        # Unfinished: still running, or killed. Recording it now would win the rid over the
        # real outcome that live capture (or a later import) records when it ends.
        if metrics.get('exit_code') is None:
            counts['skipped_unfinished'] = counts.get('skipped_unfinished', 0) + 1
            continue
        harness = 'pipeline' if role in h.PIPELINE_ROLES else (metrics.get('harness') or '')
        prov.setdefault('wall_ended', _window(attempt)[1] or None)
        if harness != 'pipeline' and prov.get('scope') == 'task' and stored is None:
            prov['attempt_now'] = None
        try:
            events = capture.events_for(attempt, root, role, harness, metrics.get('exit_code'), prov, 'import')
        except Exception as error:
            counts.setdefault('failed', []).append('%s: %s' % (attempt, error))
            continue
        for event in events:
            if isinstance(event.get('data'), dict) and event['data'].get('root'):
                event['data']['root'] = str(mapper(event['data']['root']))
        # A live begin() recorded which code the execution started with; keep it.
        capture.stamp(events, (stored or {}).get('provenance'), check_change=False)
        batches.append((events, repo))
    for root, prov, repo, attempts in by_task.values():
        try:
            batches.append((legacy_task_events(root, attempts, dict(prov, mode='parallel', task=root.name)), repo))
        except Exception as error:
            counts.setdefault('failed', []).append('%s: %s' % (root, error))
    for task_dir in walk_fractal_tasks(top, onerror):
        try:
            ctx = h.fractal_context(task_dir, create=False)
            if ctx.get('run') and not (task_dir.parent.parent / 'dataset.uid').exists():
                ctx['run'] = _legacy_uid(task_dir.parent.parent, mapper)
            if is_temp(ctx['repo']) and not args.include_temp:
                continue
            events = h.fractal_task_events(task_dir, ctx, src='import')
            for path in sorted(task_dir.glob('nodes/*/node.json')):
                node = h.read_json(path) or {}
                for step in sorted((path.parent / 'steps').glob('*')):
                    if step.is_dir():
                        events.append(h.fractal_step_event(task_dir, node, step, ctx, src='import'))
            batches.append((events, ctx['repo']))
        except Exception as error:
            counts.setdefault('failed', []).append('%s: %s' % (task_dir, error))
    for plan in sorted(plans):
        repo = h.first_line(plan / 'repo')
        if is_temp(repo) and not args.include_temp:
            continue
        uid = _uid(plan, mapper, uids)
        prov = dict(run=uid, run_id=h.first_line(plan / 'run_id') or plan.name)
        bases = [h.first_line(t / 'base_ref') for t in sorted((plan / 'tasks').glob('*')) if (t / 'base_ref').exists()]
        events = [h.plan_event(plan, prov, src='import', head=bases[0] if bases else None)]
        events += intervention_events(plan, prov, counts, repo)
        # A plan that has a dataset.uid was captured live, merges and final reviews included,
        # so synthesizing them again would only add a second, differently keyed copy.
        live = (plan / 'dataset.uid').exists()
        for tdir in sorted((plan / 'tasks').glob('*')) if not live else []:
            if not tdir.is_dir():
                continue
            status = h.first_line(tdir / 'status')
            if (tdir / 'merged').exists() or status in ('CONFLICT', 'INVALIDATED'):
                outcome = 'MERGED' if (tdir / 'merged').exists() else status
                events.append(dict(rid='event:merge:legacy:%s:%s' % (uid, tdir.name), kind='event', src='import',
                                   ts=(tdir / ('merged' if outcome == 'MERGED' else 'status')).stat().st_mtime,
                                   run=uid, run_id=prov['run_id'], mode='parallel', task=tdir.name,
                                   data=dict(name='merge', status=outcome, legacy=True,
                                             reviewed_commit=h.first_line(tdir / 'reviewed.commit'))))
        for review in (sorted(plan.glob('final-review-*')) + sorted(plan.glob('integrate-*/final-review'))) if not live else []:
            if not review.is_dir():
                continue
            events.append(dict(rid='event:final_review:legacy:%s:%s' % (uid, sha256(str(mapper(review)).encode())[:16]),
                               kind='event', src='import', ts=review.stat().st_mtime, run=uid,
                               run_id=prov['run_id'], mode='parallel',
                               data=dict(name='final_review', legacy=True, dir=str(mapper(review)),
                                         verdict=h.first_line(review / 'verdict'), meta=h.kv(review / 'review.meta') or None,
                                         blobs=h.blobs(diff=put_file(review / 'combined.diff', 'diff')))))
        batches.append((events, repo))
    return batches


def _regular_text(path: Path) -> str | None:
    """A run file's text, None if it is absent; a link is never run data (forge's own writers
    refuse them too). A file that exists but cannot be read raises OSError: h.text would
    report it as absent."""
    if path.is_symlink():
        return None
    try:
        return path.read_bytes().decode('utf-8', 'replace')
    except (FileNotFoundError, NotADirectoryError):
        return None


def _stored_interventions(uid: str | None, repo: str | None) -> dict:
    """What the store already holds for a run's escalations and control changes, by rid.

    Released forge captured these live under a time-based rid, which cannot be recomputed.
    A row like that is the same fact the run dir still lists, so the importer must not add
    it a second time under the deterministic rid. {rid: (name, task, fields)}; empty when
    the store cannot be read (a strict write then fails on its own).

    Read only, and only a store that already exists: connect() would create the store,
    which a dry run must not do, nor an import whose store location_refused() rules out.
    """
    from . import DRY
    if not uid or not enabled() or DRY or location_refused(repo):
        return {}
    path = dataset_dir() / 'dataset.sqlite'
    if not path.is_file():
        return {}
    found = {}
    try:
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=BUSY_TIMEOUT_S)
        try:
            for name in h.INTERVENTIONS:
                for rid, task, data in db.execute(
                        "SELECT rid, task, data FROM events WHERE kind = 'event' AND run = ? AND rid LIKE ?",
                        (uid, 'event:%s:%%' % name)):
                    values = json.loads(data)
                    if isinstance(values, dict) and values.get('name') == name:
                        found[rid] = (name, task, _normalized(name, values))
        finally:
            db.close()
    except Exception:
        return {}
    return found


def _normalized(name: str, values: dict) -> dict:
    data = h.intervention_data(name, values)
    if name == 'escalation' and not data['source']:
        data['source'] = 'policy'   # rows from before the source column
    return data


def intervention_events(plan: Path, prov: dict, counts: dict, repo: str | None = None) -> list[dict]:
    """The escalations and control changes a plan dir recorded, as the events live capture emits.

    Same rid as capture.point (harvest.intervention_rid), so a change seen live and then
    imported, or imported twice, is one row. A line that cannot be read is a failure, not
    a skip: this is the only record of what a human did once the run dir is gone.
    """
    uid, run_id = prov['run'], prov['run_id']
    events = []
    stored = _stored_interventions(uid, repo)

    def add(name, values, task, ts):
        data = dict(h.intervention_data(name, values), name=name, blobs={})
        rid = h.intervention_rid(name, uid, task, values)
        fields = _normalized(name, values)
        if rid not in stored and any(known == (name, task, fields) for known in stored.values()):
            return   # already captured live under the old time-based rid
        events.append(dict(rid=rid, kind='event', src='import', ts=ts,
                           run=uid, run_id=run_id, mode='parallel', task=task, data=data))

    def read(path):
        try:
            return _regular_text(path) or ''
        except OSError as error:
            counts.setdefault('failed', []).append('%s: cannot read: %s' % (path, error.strerror or error))
            return ''

    for tdir in sorted((plan / 'tasks').glob('*')):
        path = tdir / 'escalations.tsv'
        for number, line in enumerate(read(path).splitlines(), 1):
            if not line.strip():
                continue
            cells = line.split('\t')
            if len(cells) not in (4, 5) or not cells[0].isdigit():
                counts.setdefault('failed', []).append('%s:%d: not an escalations.tsv line' % (path, number))
                continue
            add('escalation', dict(after_attempt=cells[1], **{'from': cells[2]}, to=cells[3],
                                   source=cells[4] if len(cells) == 5 and cells[4] else 'policy'),
                tdir.name, int(cells[0]))
    path = plan / 'control.log'
    for number, line in enumerate(read(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            ts = float(entry['at'])
        except (ValueError, KeyError, TypeError):
            entry = None
        if not isinstance(entry, dict):
            counts.setdefault('failed', []).append('%s:%d: not a control.log entry' % (path, number))
            continue
        add('control', {field: entry.get(field) for field in h.INTERVENTIONS['control']}, entry.get('task') or None, ts)
    return events


def ledger_events(ledger: Path, repo: str) -> list[dict]:
    events = []
    key = repo_id(repo)
    columns = ('ts', 'run_id', 'task', 'role', 'model', 'verdict', 'category', 'key', 'text', 'duration_s', 'injectable')
    for line in (h.text(ledger) or '').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        cells = line.split('\t')
        row = {name: (cells[i] if i < len(cells) else None) for i, name in enumerate(columns)}
        try:
            ts = time.mktime(time.strptime(row['ts'], '%Y-%m-%dT%H:%M:%SZ')) - time.timezone
        except (TypeError, ValueError):
            ts = 0
        events.append(dict(rid='ledger:%s:%s' % (key, sha256(line.encode())[:24]), kind='ledger', src='import',
                           ts=ts, run_id=row['run_id'], task=None if row['task'] in (None, '-', '') else row['task'],
                           data=row))
    return events


def _manifest(directory: Path) -> dict:
    out = {}
    for line in (h.text(directory / 'MANIFEST.tsv') or '').splitlines():
        cells = line.split('\t')
        if len(cells) >= 2 and not line.startswith('#'):
            out[cells[0]] = cells[1]
    return out


def _member(member, path):
    # Links are never run data (Fractal's archive carries a venv full of absolute ones),
    # and skipping them means nothing extracted can point outside the temp dir.
    if member.issym() or member.islnk():
        return None
    return tarfile.data_filter(member, path)


def _extract(archive: Path, into: Path) -> None:
    with tarfile.open(archive, 'r:gz') as bundle:
        if hasattr(tarfile, 'data_filter'):
            bundle.extractall(into, filter=_member)
        else:
            members = []
            for member in bundle.getmembers():
                target = (into / member.name).resolve()
                if member.issym() or member.islnk():
                    continue
                if not str(target).startswith(str(into.resolve()) + os.sep):
                    raise ValueError('unsafe path in archive: ' + member.name)
                members.append(member)
            bundle.extractall(into, members=members)


ARCHIVE_META = 'FORGE_ARCHIVE.json'
ARCHIVE_EXCLUDED = 'FORGE_ARCHIVE_EXCLUDED.txt'
ARCHIVE_FORMAT = 1
ARCHIVE_MAX_FILE = 50 << 20


def _archive_mapper(work: Path, source: Path | None) -> Mapper:
    """Where an extracted archive's run dir lived: its own FORGE_ARCHIVE.json, else a MANIFEST's source.

    The archive's word wins, and it brings the run's dataset.uid along for the case where
    the run dir it was taken from no longer has one to read.
    """
    mapper = Mapper()
    meta_path = work / ARCHIVE_META
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text())
        original = meta.get('original_path') if isinstance(meta, dict) else None
        if not isinstance(meta, dict) or meta.get('format') != ARCHIVE_FORMAT or not isinstance(original, str) \
                or not os.path.isabs(original):
            raise ValueError('%s is not a format-%d forge archive description' % (ARCHIVE_META, ARCHIVE_FORMAT))
        run = work / Path(original).name
        if not run.is_dir():
            dirs = [d for d in work.iterdir() if d.is_dir()]
            if len(dirs) != 1:
                raise ValueError('the archive holds no run dir named %s' % Path(original).name)
            run = dirs[0]
        uid = meta.get('dataset_uid')
        if isinstance(uid, str) and uid.strip() and not (run / 'dataset.uid').exists():
            (run / 'dataset.uid').write_text(uid.strip() + '\n')
        mapper.add(run, Path(original))
        # Absent from archives written before it existed: those recompute from what they hold.
        legacy = meta.get('legacy_uids')
        for rel, value in (legacy.items() if isinstance(legacy, dict) else ()):
            if isinstance(value, str) and value.strip() and not os.path.isabs(rel) and '..' not in Path(rel).parts:
                mapper.legacy[str(Path(original) / rel)] = value.strip()
    elif source is not None:
        mapper.add(work, source.parent)
    return mapper


def import_run(path, strict: bool = True, include_temp: bool = True) -> dict:
    """Import one run dir or `.tar.gz` and say exactly what happened: {events, new, failed, ...}.

    Strict (the default) stores through record_strict and raises DatasetError if any event
    could not be stored or any part of the run could not be read; the error's `result` holds
    the same dict. A store failure stops further writes (it is reported in `failed`), while an
    unreadable part of the run is reported and the rest is still written. Unfinished executions are left
    for live capture and counted as `skipped_unfinished`: that is not a failure. Unlike the
    CLI, test runs (a repo in a temp dir) are kept by default: live capture keeps them too.
    """
    path = Path(path).expanduser()
    args = SimpleNamespace(include_temp=include_temp, report_unreadable=True)
    counts: dict = {'events': 0, 'new': 0}
    writer = record_strict if strict else record

    def write(batches, blob_failures):
        for events, repo in batches:
            if events:
                counts['events'] += len(events)
                if strict and blob_failures:
                    # put_file turns a failed blob write into "no content"; the first writer of a
                    # rid wins, so storing this batch would make the loss permanent.
                    counts.setdefault('failed', []).extend(blob_failures)
                    return
                try:
                    counts['new'] += writer(events, repo=repo, where='import')
                except DatasetError as error:
                    counts.setdefault('failed', []).append('store: %s' % error)
                    return
        if strict and blob_failures and not counts.get('failed'):
            counts.setdefault('failed', []).extend(blob_failures)

    with watch_blob_failures() as blob_failures:
        if path.is_dir():
            try:
                write(import_tree(path, Mapper(), args, counts), blob_failures)
            except Exception as error:
                counts.setdefault('failed', []).append('%s: %s' % (path, error))
        elif path.is_file() and path.name.endswith(('.tar.gz', '.tgz')):
            work = Path(tempfile.mkdtemp(prefix='forge-dataset-import-'))
            try:
                _extract(path, work)
                write(import_tree(work, _archive_mapper(work, None), args, counts), blob_failures)
            except Exception as error:
                counts.setdefault('failed', []).append('%s: %s' % (path, error))
            finally:
                shutil.rmtree(work, ignore_errors=True)
        else:
            counts.setdefault('failed', []).append('%s: not a run dir or .tar.gz archive' % path)
    result = dict(counts, failed=list(counts.get('failed', [])))
    if strict and result['failed']:
        error = DatasetError('import of %s failed: %s' % (path, '; '.join(result['failed'][:5])))
        error.result = result
        raise error
    return result


def archive_run(run_dir, dest) -> dict:
    """Write run_dir as a `.tar.gz` that `forge dataset import` maps back to the same run.

    Layout: FORGE_ARCHIVE.json (the original realpath, run id, dataset uid, the legacy ids of
    run dirs without one), then
    FORGE_ARCHIVE_EXCLUDED.txt, then the run dir under its basename. Left out are what the
    importer never reads (its skip rules, `review-*` snapshots), symlinks, and files over 50 MB;
    each is listed. Written to a temp name beside `dest`, fsynced, then renamed.
    """
    run = Path(run_dir).expanduser().resolve()
    dest = Path(dest).expanduser()
    if not run.is_dir():
        raise DatasetError('%s is not a run dir' % run)
    real_dest = Path(os.path.realpath(dest))
    if real_dest == run or run in real_dest.parents:
        raise DatasetError('refusing to write the archive %s inside the run dir %s' % (dest, run))
    excluded: list[str] = []
    entries: list[tuple[Path, str]] = [(run, run.name)]

    def unreadable(error):
        raise DatasetError('cannot read %s: %s' % (error.filename, error)) from error

    for directory, subdirs, files in os.walk(run, onerror=unreadable):
        here = Path(directory)
        rel = here.relative_to(run)
        keep = []
        for name in sorted(subdirs):
            if (here / name).is_symlink():
                excluded.append((rel / name).as_posix())
            elif name in SKIP_DIRS or name.startswith('review-'):
                excluded.append((rel / name).as_posix() + '/')
            else:
                keep.append(name)
                entries.append((here / name, (Path(run.name) / rel / name).as_posix()))
        subdirs[:] = keep
        for name in sorted(files):
            full = here / name
            try:
                info = full.lstat()
            except OSError as error:
                raise DatasetError('cannot read %s: %s' % (full, error)) from error
            if not stat.S_ISREG(info.st_mode) or info.st_size > ARCHIVE_MAX_FILE:
                excluded.append((rel / name).as_posix())
            else:
                entries.append((full, (Path(run.name) / rel / name).as_posix()))
    meta = dict(format=ARCHIVE_FORMAT, original_path=str(run), run_id=h.first_line(run / 'run_id') or run.name,
                dataset_uid=h.first_line(run / 'dataset.uid') or None, legacy_uids=_legacy_uids(run),
                created=int(time.time()))

    def anonymous(info):
        info.uid = info.gid = 0
        info.uname = info.gname = ''
        return info

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=str(dest.parent), prefix='.%s.' % dest.name, suffix='.tmp')
    except OSError as error:
        raise DatasetError('cannot write the archive %s: %s' % (dest, error)) from error
    try:
        with os.fdopen(fd, 'wb') as handle:
            with tarfile.open(fileobj=handle, mode='w:gz', dereference=True) as bundle:   # no link members: _extract drops them
                for name, body in ((ARCHIVE_META, json.dumps(meta, sort_keys=True, indent=1) + '\n'),
                                   (ARCHIVE_EXCLUDED, ''.join(line + '\n' for line in excluded))):
                    info = tarfile.TarInfo(name)
                    data = body.encode()
                    info.size, info.mtime, info.mode = len(data), int(time.time()), 0o644
                    bundle.addfile(info, io.BytesIO(data))
                for full, arcname in entries:
                    bundle.add(str(full), arcname=arcname, recursive=False, filter=anonymous)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, dest)
        try:
            directory_fd = os.open(str(dest.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    except (OSError, tarfile.TarError) as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise DatasetError('cannot write the archive %s: %s' % (dest, error)) from error
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return dict(path=str(dest), bytes=dest.stat().st_size, excluded=excluded)


def main(args) -> int:
    from . import harvest  # noqa: F401  (keeps the import graph explicit)
    import forge_dataset as store
    store.DRY = bool(args.dry_run)
    counts = {'events': 0, 'new': 0}
    by_kind: dict = {}
    targets = [Path(p).expanduser() for p in args.paths]
    if args.registry:
        for registry in sorted(config_runs_dir().glob('*.txt')):
            for line in (h.text(registry) or '').splitlines():
                if line.strip() and not line.startswith('#') and Path(line.strip()).is_dir():
                    targets.append(Path(line.strip()))
    if args.fractal:
        state = Path(os.environ.get('XDG_STATE_HOME') or str(Path.home() / '.local' / 'state')).expanduser()
        targets.append(state / 'forge' / 'fractal' / 'runs')
    ledgers = [(Path(r).expanduser() / '.forge' / 'ledger.tsv', str(Path(r).expanduser().resolve())) for r in args.ledger]
    archives = []
    for target in list(targets):
        if target.is_dir() and (target / 'MANIFEST.tsv').exists():
            targets.remove(target)
            manifest = _manifest(target)
            archives += [(target / name, Path(source)) for name, source in manifest.items() if (target / name).exists()]
            repos = target / 'ledgers' / 'REPOS.tsv'
            for line in (h.text(repos) or '').splitlines():
                cells = line.split('\t')
                if len(cells) == 2 and (target / 'ledgers' / (cells[0] + '.ledger.tsv')).exists():
                    ledgers.append((target / 'ledgers' / (cells[0] + '.ledger.tsv'), cells[1]))
        elif target.is_file() and target.name.endswith(('.tar.gz', '.tgz')):
            targets.remove(target)
            archives.append((target, None))

    def write(batches):
        for events, repo in batches:
            if not events:
                continue
            counts['events'] += len(events)
            for event in events:
                by_kind[event['kind']] = by_kind.get(event['kind'], 0) + 1
            if not args.dry_run:
                counts['new'] += record(events, repo=repo, where='import')

    for target in targets:
        if not target.exists():
            print('forge dataset: skipping %s (missing)' % target)
            continue
        write(import_tree(target, Mapper(), args, counts))
    for archive, source in archives:
        work = Path(tempfile.mkdtemp(prefix='forge-dataset-import-'))
        try:
            _extract(archive, work)
            write(import_tree(work, _archive_mapper(work, source), args, counts))
        except Exception as error:
            counts.setdefault('failed', []).append('%s: %s' % (archive, error))
        finally:
            shutil.rmtree(work, ignore_errors=True)
    for ledger, repo in ledgers:
        if ledger.exists() and (args.include_temp or not is_temp(repo)):
            write([(ledger_events(ledger, repo), repo)])
    verb = 'would import' if args.dry_run else 'imported'
    print('forge dataset: %s %d event(s)%s from %d dir(s), %d archive(s), %d ledger(s)'
          % (verb, counts['events'], '' if args.dry_run else ' (%d new)' % counts['new'],
             len(targets), len(archives), len(ledgers)))
    if by_kind:
        print('  ' + ', '.join('%s %d' % kv for kv in sorted(by_kind.items())))
    if counts.get('skipped_unfinished'):
        print('  skipped %d unfinished execution(s): still running or killed; live capture records them when they end'
              % counts['skipped_unfinished'])
    if counts.get('skipped_temp'):
        print('  skipped %d execution(s) whose repo is a temp dir (test runs; --include-temp to keep them)'
              % counts['skipped_temp'])
    for failure in counts.get('failed', [])[:20]:
        print('  failed: ' + failure)
    return 1 if counts.get('failed') else 0
