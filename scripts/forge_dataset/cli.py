"""`forge dataset ...`: inspect the run dataset, import old runs, and the shell's capture hooks.

User-facing: status, dump, import, export, outcomes, routing, reviewers. Internal (called by forge's shell scripts, fail-open,
silent): event, invocation, plan.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import connect, data_home, dataset_dir, enabled, read_blob


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file()) if path.is_dir() else 0


def _human(n: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0
    return str(n)


def _continued_sessions(db) -> dict:
    """How many dispatches asked to continue a reviewer session.

    Counted from the dispatch events themselves. A shadow review is not required, and a
    continue that failed still counts: the file was written because continuing was requested.
    """
    continued = failed = 0
    for (raw,) in db.execute(
            "SELECT data FROM events WHERE kind = 'dispatch' AND data LIKE ?",
            ('%"session_continue": "%',)):
        value = json.loads(raw).get('session_continue')
        if value == 'continued':
            continued += 1
        elif value == 'failed':
            failed += 1
    return dict(continued_reviews=continued, failed_continues=failed)


def status(args) -> int:
    from . import checks
    directory = dataset_dir()
    summary = dict(path=str(directory), enabled=enabled(), exists=(directory / 'dataset.sqlite').exists())
    if summary['exists']:
        db = connect()
        try:
            summary['events'] = dict(db.execute('SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY kind'))
            summary['by_source'] = dict(db.execute('SELECT src, COUNT(*) FROM events GROUP BY src'))
            summary['repos'] = [dict(repo=r, path=p, origin=o) for r, p, o in
                                db.execute('SELECT repo, path, origin FROM repos ORDER BY path')]
            summary['runs'] = db.execute('SELECT COUNT(DISTINCT run) FROM events WHERE run IS NOT NULL').fetchone()[0]
            last = db.execute('SELECT MAX(ts) FROM events').fetchone()[0]
            summary['last_event'] = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last)) if last else None
            summary['checks'] = checks.run(db, verbose=args.verbose)
            summary.update(_continued_sessions(db))
            if summary['events'].get('dispatch'):
                from . import export
                if db.execute("SELECT 1 FROM events WHERE kind = 'dispatch' AND data LIKE '%qa_shadow%' LIMIT 1").fetchone():
                    summary['shadow'] = export.agreement(export.Runs(export.Store(db)))
        finally:
            db.close()
        summary['size'] = dict(database=_size(directory / 'dataset.sqlite') + _size(directory / 'dataset.sqlite-wal'),
                               blobs=_size(directory / 'blobs'))
    errors = directory / 'errors.log'
    summary['errors'] = len(errors.read_text(errors='replace').splitlines()) if errors.exists() else 0
    legacy = data_home() / 'legacy'
    summary['legacy_archives'] = len(list(legacy.glob('*.tar.gz'))) if legacy.is_dir() else 0
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    print('forge dataset: %s (%s)' % (summary['path'], 'capturing' if summary['enabled'] else 'off: FORGE_DATASET=off'))
    if not summary['exists']:
        print('  nothing captured yet')
    else:
        print('  size:   database %s, blobs %s' % (_human(summary['size']['database']), _human(summary['size']['blobs'])))
        print('  events: %d in %d run(s) across %d repo(s); last %s'
              % (sum(summary['events'].values()), summary['runs'], len(summary['repos']), summary['last_event']))
        print('          ' + ', '.join('%s %d' % kv for kv in summary['events'].items()))
        if summary['by_source'].get('import'):
            print('          %d imported from old run dirs' % summary['by_source']['import'])
        for line in checks.report(summary['checks']):
            print('  ' + line)
        shadow = summary.get('shadow')
        if shadow:
            rate = ' (%.0f%% agree)' % (100.0 * shadow['agree'] / shadow['pairs']) if shadow['pairs'] else ''
            print('  shadow: %d second review(s), %d usable pair(s), %d agree%s'
                  % (shadow['shadow_reviews'], shadow['pairs'], shadow['agree'], rate))
            for pair, n in shadow['by_pair'].items():
                print('          %s: %d of %d agree' % (pair, n['agree'], n['pairs']))
        if summary.get('continued_reviews') or summary.get('failed_continues'):
            print('  continued reviews: %d' % summary['continued_reviews'])
            print('  failed continues: %d' % summary['failed_continues'])
    if summary['legacy_archives']:
        print('  legacy: %d archived run dir(s) kept in %s (`forge dataset import %s` re-reads them;'
              ' events already stored are skipped)' % (summary['legacy_archives'], legacy, legacy))
    if summary['errors']:
        print('  errors: %d capture failure(s) logged in %s' % (summary['errors'], errors))
    return 0


def dump(args) -> int:
    if not (dataset_dir() / 'dataset.sqlite').exists():
        return 0
    db = connect()
    try:
        where, params = [], []
        for column in ('kind', 'run', 'repo', 'task', 'src'):
            value = getattr(args, column)
            if value:
                where.append('%s = ?' % column)
                params.append(value)
        if args.since:
            where.append('ts >= ?')
            params.append(args.since)
        query = 'SELECT * FROM events%s ORDER BY ts, rid' % (' WHERE ' + ' AND '.join(where) if where else '')
        cursor = db.execute(query, params)
        names = [d[0] for d in cursor.description]
        for row in cursor:
            event = dict(zip(names, row))
            event['data'] = json.loads(event['data'])
            print(json.dumps(event, sort_keys=True))
    finally:
        db.close()
    return 0


def blob(args) -> int:
    sys.stdout.buffer.write(read_blob(args.sha))
    return 0


def _pairs(items: list | None) -> dict:
    out = {}
    for item in items or []:
        key, _, value = item.partition('=')
        if key:
            out[key] = value
    return out


def event(args) -> int:
    from .capture import point
    point(args.name, args.dir, args.task, _pairs(args.set), _pairs(args.file), repo=args.repo)
    return 0


def invocation(args) -> int:
    from .capture import invocation as record_invocation
    record_invocation(args.command, args.dir, args.rc, args.started, args.argv, args.pid)
    return 0


def plan(args) -> int:
    from .capture import plan_snapshot
    plan_snapshot(args.dir)
    return 0


def run_import(args) -> int:
    from . import importer
    return importer.main(args)


def run_export(args) -> int:
    from . import export
    return export.main(args)


def run_outcomes(args) -> int:
    from . import outcomes
    return outcomes.main(args)


def run_routing(args) -> int:
    from . import routing
    return routing.main(args)


def run_reviewers(args) -> int:
    from . import reviewers
    return reviewers.main(args)


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog='forge dataset', description='The run dataset: every run\'s inputs and outcomes.')
    sub = top.add_subparsers(dest='command', required=True)
    p = sub.add_parser('status', help='where the dataset is, what it holds, and what looks incomplete')
    p.add_argument('--json', action='store_true')
    p.add_argument('--verbose', '-v', action='store_true', help='list every incomplete item, not just counts')
    p.set_defaults(func=status)
    p = sub.add_parser('dump', help='events as JSON lines')
    for column in ('kind', 'run', 'repo', 'task', 'src'):
        p.add_argument('--' + column)
    p.add_argument('--since', type=float, help='unix time')
    p.set_defaults(func=dump)
    p = sub.add_parser('blob', help='print a stored blob by its sha')
    p.add_argument('sha')
    p.set_defaults(func=blob)
    p = sub.add_parser('import', help='capture run dirs, archives and ledgers that predate live capture')
    p.add_argument('paths', nargs='*', help='run/plan dirs, dirs to scan, or .tar.gz archives')
    p.add_argument('--registry', action='store_true', help='every run dir listed in the Jev runs registry')
    p.add_argument('--fractal', action='store_true', help='the Fractal state dir')
    p.add_argument('--ledger', action='append', default=[], metavar='REPO', help="a repo's .forge/ledger.tsv")
    p.add_argument('--include-temp', action='store_true', help='also import runs whose repo is a temp dir (test runs)')
    p.add_argument('--dry-run', action='store_true')
    p.set_defaults(func=run_import)
    p = sub.add_parser('export', help='labeled decision cases (System One JSONL) derived from outcomes')
    p.add_argument('--out', help='output dir (default: <data>/exports/forge-decisions)')
    p.add_argument('--slice', dest='slices', action='append', choices=('routing', 'retry', 'review', 'drift', 'citation', 'memory'),
                   help='only this slice; repeatable (default: all)')
    p.add_argument('--limit', type=int, help='at most this many cases per slice, spread across runs')
    p.add_argument('--seed', type=int, default=7)
    p.set_defaults(func=run_export)
    p = sub.add_parser('outcomes', help='what happened to integrated work afterwards (read-only git)')
    p.add_argument('--horizon-days', type=int, default=14, help='how long after integrate to look (default 14)')
    p.add_argument('--repo', help='only this repo')
    p.add_argument('--include-temp', action='store_true', help='also repos under temp dirs (test runs)')
    p.add_argument('--dry-run', action='store_true', help='report what would be recorded, write nothing')
    p.set_defaults(func=run_outcomes)
    p = sub.add_parser('routing', help='pass rate and runtime per tier and model, and what the --explore draws say')
    p.add_argument('--repo', help='only this repo')
    p.add_argument('--since', type=float, metavar='DAYS', help='only tasks first attempted in the last DAYS days')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=run_routing)
    p = sub.add_parser('reviewers', help='how often work each reviewer passed was later reverted or rewritten (advisory)')
    p.add_argument('--horizon-days', type=int, default=14, help='the outcomes recorded at this horizon (default 14)')
    p.add_argument('--repo', help='only this repo')
    p.add_argument('--json', action='store_true')
    p.set_defaults(func=run_reviewers)
    p = sub.add_parser('event', help=argparse.SUPPRESS)
    p.add_argument('name')
    p.add_argument('--dir')
    p.add_argument('--task')
    p.add_argument('--repo')
    p.add_argument('--set', action='append')
    p.add_argument('--file', action='append')
    p.set_defaults(func=event)
    p = sub.add_parser('invocation', help=argparse.SUPPRESS)
    p.add_argument('command')
    p.add_argument('--dir')
    p.add_argument('--rc')
    p.add_argument('--started')
    p.add_argument('--pid')
    p.add_argument('argv', nargs=argparse.REMAINDER)
    p.set_defaults(func=invocation)
    p = sub.add_parser('plan', help=argparse.SUPPRESS)
    p.add_argument('dir')
    p.set_defaults(func=plan)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if getattr(args, 'argv', None) and args.argv[:1] == ['--']:
        args.argv = args.argv[1:]
    return args.func(args)
