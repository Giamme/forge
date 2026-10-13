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

import json
import os
import re
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

from . import config_runs_dir, data_home, put_blob, put_file, record, repo_id, sha256
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


def walk_attempts(top: Path) -> list[Path]:
    found = []
    for directory, subdirs, _ in os.walk(top):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith('review-')]
        if os.path.basename(directory) == 'attempts':
            found.extend(Path(directory) / d for d in subdirs if '-' in d)
            subdirs[:] = []
    return sorted(found)


def walk_plans(top: Path) -> list[Path]:
    found = []
    for directory, subdirs, files in os.walk(top):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith('review-') and d != 'attempts']
        if 'tasks.tsv' in files:
            found.append(Path(directory))
    return sorted(found)


def walk_fractal_tasks(top: Path) -> list[Path]:
    found = []
    for directory, subdirs, files in os.walk(top):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith('review-')
                      and d not in ('attempts', 'steps', 'runtime')]
        if 'request.json' in files and 'nodes' in subdirs and Path(directory).parent.name == 'tasks':
            found.append(Path(directory))
    return sorted(found)


class Mapper:
    """Maps a path inside an extracted archive back to where the run originally lived.

    A run's legacy id hashes its original path, so importing an archive and importing
    the original dir must agree on it.
    """

    def __init__(self):
        self.pairs = []

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
        cache[key] = stored or h.legacy_uid_for(run_dir, mapper(run_dir))
    return cache[key]


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
    plans = {p for p in walk_plans(top)}
    by_task: dict = {}
    for attempt in walk_attempts(top):
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
        batches.append((events, repo))
    for root, prov, repo, attempts in by_task.values():
        try:
            batches.append((legacy_task_events(root, attempts, dict(prov, mode='parallel', task=root.name)), repo))
        except Exception as error:
            counts.setdefault('failed', []).append('%s: %s' % (root, error))
    for task_dir in walk_fractal_tasks(top):
        try:
            ctx = h.fractal_context(task_dir, create=False)
            if ctx.get('run') and not (task_dir.parent.parent / 'dataset.uid').exists():
                ctx['run'] = h.legacy_uid_for(task_dir.parent.parent, mapper(task_dir.parent.parent))
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
        state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home() / '.local' / 'state'))).expanduser()
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
            mapper = Mapper()
            if source is not None:
                mapper.add(work, source.parent)
            write(import_tree(work, mapper, args, counts))
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
