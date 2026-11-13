#!/usr/bin/env python3
"""forge runs compact [RUN_DIR ... | --repo DIR | --all] [--older-than DAYS] [--logs] [--dry-run] [--json]

Takes back the space a run dir holds for nothing, and keeps every run on disk and viewable (#139).
Per run, with the run's own lock held from the first step to the last (`schedule.lock` of a plan,
`pipeline.lock` of a solo run, as `forge runs rm` takes it), so a run started meanwhile fails to
start instead of racing the compaction:

  check       the same identity and safety rules as `forge runs rm`; an active run is skipped
  preserve    a strict dataset import of the run, as `forge runs rm` starts with: if any event
              cannot be stored, nothing in that run is touched
  snapshots   remove every review snapshot (forge_storage.py has the rule); nothing reads one once
              its review has ended, and the importer and the archive never did
  logs        --logs: gzip each dwarf's and reviewer's log copy in its execution record
              (attempts/<role>-<id>/<role>.log, 64 KB or more) into <log>.gz, same mode and times;
              forge-ui reads either. The logs in the task dirs, which running agents write and retries
              read, are never touched.
  record      the dataset event run_compacted (fail-open)

Never touched: an active run, a worktree, the logs in task dirs, and anything forge does not create.
A dependency install or a `.git` found inside a run dir is reported with its size (`strays`), not
removed. Without RUN_DIR, `--repo DIR` takes every registered run of that repository and `--all` every
registered run; there an active run, or one changed less than an hour ago, is skipped, not an error.

`--auto` is what forge itself starts in the background when a run starts (storage.compact auto, the
default): `--logs` over the repository's runs, never the run named by `--exclude`, silent, and
snapshots are kept when FORGE_KEEP_SNAPSHOTS=1. It also refreshes the repository's footprint, which
`--start-check` compares with storage.warn-gb (20 GB by default) and warns about at run start.

--json prints one object on stdout, on failure too:
  {ok, dry_run, bytes_freed, runs: [{run_dir, run_id, repo, status, steps: [{step, ok, detail}],
   size_bytes, bytes_freed, snapshots: {count, bytes, removed}, logs: {count, bytes, compressed},
   strays: [{path, bytes, kind}]}]}
status: compacted, nothing (nothing to take back), would-compact (dry run), active, recent, refused, failed.
Exit 0 ok, 2 refused input, 3 the one run named is active, 4 something failed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))

import forge_storage  # noqa: E402

OK, REFUSED, BUSY, FAILED = 0, 2, 3, 4
RECENT_S = 3600


def _rm():
    """forge-runs-rm.py, for its identity, lock and safety rules: one set for both commands."""
    module = sys.modules.get('forge_runs_rm')
    if module is None:
        spec = importlib.util.spec_from_file_location('forge_runs_rm', Path(__file__).resolve().with_name('forge-runs-rm.py'))
        module = importlib.util.module_from_spec(spec)
        sys.modules['forge_runs_rm'] = module
        spec.loader.exec_module(module)
    return module


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def registered(repo: Path | None) -> list[str]:
    """The run dirs the registry lists, for one repository or (None) every one; existing dirs only, no repeats."""
    return [path for path, _ in forge_storage.run_dirs(repo)]


def repo_of(start: Path) -> Path:
    try:
        out = subprocess.run(['git', '-C', str(start), 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return start.resolve()


def last_change(path: str) -> float:
    """The newest mtime among the run dir's top-level entries and its task dirs (lock files aside)."""
    newest = 0.0
    for directory in (path, os.path.join(path, 'tasks')):
        try:
            with os.scandir(directory) as listing:
                for entry in listing:
                    if entry.name.endswith('.lock'):
                        continue          # opened by every command that checks it: the lock, not its file, says a run is live
                    try:
                        newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return newest


class Outcome:
    def __init__(self, run_dir, quiet):
        self.data = dict(run_dir=run_dir, run_id=None, repo=None, status=None, steps=[], size_bytes=None, bytes_freed=0,
                         snapshots=dict(count=0, bytes=0, removed=0), logs=dict(count=0, bytes=0, compressed=0), strays=[])
        self.quiet = quiet
        self.ok = True

    def step(self, name, ok, detail):
        self.data['steps'].append(dict(step=name, ok=bool(ok), detail=detail))
        if not ok:
            self.ok = False


def event(run, name, **values):
    stamp = json.dumps([run.uid, name, run.path, time.time_ns()])
    return dict(rid='event:%s:%s' % (name, hashlib.sha256(stamp.encode()).hexdigest()[:24]), kind='event',
                ts=time.time(), run=run.uid, run_id=run.run_id, mode=run.mode, task=None,
                parent=os.environ.get('FORGE_DATASET_PARENT') or None,
                inv=os.environ.get('FORGE_DATASET_INVOCATION') or None,
                data=dict(values, name=name, blobs={}))


def compact_one(run_dir, args, batch) -> tuple[Outcome, int]:
    """One run dir. Returns its outcome and the exit code it would give on its own."""
    rm = _rm()
    out = Outcome(run_dir, args.json)
    try:
        target = rm.locate(run_dir)
        if target.info is None:
            raise rm.Refused('no such directory')
        run = rm.identify(target)
    except rm.Refused as error:
        out.data['status'] = 'refused'
        out.step('check', False, str(error))
        return out, REFUSED
    out.data.update(run_dir=run.path, run_id=run.run_id, repo=run.repo)
    if batch and time.time() - last_change(run.path) < RECENT_S:
        out.data['status'] = 'recent'
        out.step('check', True, 'skipped: changed less than an hour ago')
        return out, OK
    if args.older_than is not None and time.time() - last_change(run.path) < args.older_than * 86400:
        out.data['status'] = 'recent'
        out.step('check', True, 'skipped: changed less than %g day(s) ago' % args.older_than)
        return out, OK
    fd = None
    try:
        if not args.dry_run:
            try:
                fd = rm.take_lock(run)
            except rm.Busy as error:
                out.data['status'] = 'active'
                out.step('lock', not batch, 'skipped: ' + str(error))
                return out, BUSY
            rm.pause_for_test()
        pid = rm.scheduler_pid(run)
        if pid:
            out.data['status'] = 'active'
            out.step('lock', not batch, 'skipped: scheduler.live names a live process (pid %d)' % pid)
            return out, BUSY
        found = forge_storage.scan(run.path)
        keep_snapshots = args.auto and os.environ.get('FORGE_KEEP_SNAPSHOTS', '0') == '1'
        snaps = [] if keep_snapshots else found['snapshots']['items']
        logs = found['logs']['compressible'] if args.logs else []
        out.data.update(size_bytes=found['total'], strays=found['strays'])
        out.data['snapshots'].update(count=len(snaps), bytes=sum(s['bytes'] for s in snaps))
        out.data['logs'].update(count=len(logs), bytes=sum(entry['bytes'] for entry in logs))
        out.step('check', True, '%s dir, run id %s, %s: %d snapshot(s) (%s)%s%s' % (
            run.kind, run.run_id, forge_storage.size_text(found['total']), len(snaps),
            forge_storage.size_text(out.data['snapshots']['bytes']),
            ', %d log(s) to gzip (%s)' % (len(logs), forge_storage.size_text(out.data['logs']['bytes'])) if args.logs else '',
            '; FORGE_KEEP_SNAPSHOTS=1 keeps the snapshots' if keep_snapshots else ''))
        for stray in found['strays']:
            out.step('stray', True, 'not forge\'s, left in place: %s (%s)' % (stray['path'], forge_storage.size_text(stray['bytes'])))
        if not snaps and not logs and not found['parts']:
            out.data['status'] = 'nothing'
            return out, OK
        if args.dry_run:
            out.data['status'] = 'would-compact'
            out.step('preserve', True, 'would import the run into the dataset, strictly' if rm.enabled() else
                     'skipped: FORGE_DATASET=off')
            if snaps:
                out.step('snapshots', True, 'would remove %d snapshot(s), %s' % (len(snaps), forge_storage.size_text(out.data['snapshots']['bytes'])))
            if logs:
                out.step('logs', True, 'would gzip %d log(s), %s now, about %s after' % (
                    len(logs), forge_storage.size_text(out.data['logs']['bytes']),
                    forge_storage.size_text(out.data['logs']['bytes'] * forge_storage.GZIP_RATIO)))
            out.data['bytes_freed'] = out.data['snapshots']['bytes'] + int(out.data['logs']['bytes'] * (1 - forge_storage.GZIP_RATIO))
            return out, OK
        try:
            run.uid = rm.harvest.run_uid(Path(run.path), create=False)
        except Exception:   # noqa: BLE001 -- an identity we cannot read only costs the event its run column
            run.uid = None
        if rm.enabled():
            try:
                result = rm.importer.import_run(run.path, strict=True)
            except (rm.DatasetError, OSError) as error:
                out.data['status'] = 'failed'
                out.step('preserve', False, 'the dataset could not take the run, nothing was changed: %s' % error)
                return out, FAILED
            out.step('preserve', True, 'imported %d event(s), %d new' % (result['events'], result['new']))
        else:
            out.step('preserve', True, 'skipped: FORGE_DATASET=off (snapshots and log encodings hold nothing the dataset keeps)')
        rm.check_unchanged(run, 'during the compaction')
        freed, problems = 0, []
        for snap in snaps:
            why = forge_storage.remove_snapshot(snap['path'], run.path)
            if why:
                problems.append('%s: %s' % (snap['path'], why))
            else:
                freed += snap['bytes']
                out.data['snapshots']['removed'] += 1
        if snaps:
            out.step('snapshots', not problems, 'removed %d of %d snapshot(s), %s%s' % (
                out.data['snapshots']['removed'], len(snaps), forge_storage.size_text(freed),
                '; ' + '; '.join(problems[:3]) if problems else ''))
        log_problems, log_freed = [], 0
        for entry in logs:
            gained, why = forge_storage.compress_log(entry['path'])
            if why:
                log_problems.append('%s: %s' % (entry['path'], why))
            else:
                log_freed += gained
                out.data['logs']['compressed'] += 1
        for part in found['parts']:
            # a gzip an interrupted compaction left beside an execution record's log: ours by name and place
            if forge_storage.ATTEMPT_DIR.fullmatch(os.path.basename(os.path.dirname(part))):
                try:
                    os.unlink(part)
                except OSError:
                    pass
        if logs:
            out.step('logs', not log_problems, 'gzipped %d of %d log(s), %s taken back%s' % (
                out.data['logs']['compressed'], len(logs), forge_storage.size_text(log_freed),
                '; ' + '; '.join(log_problems[:3]) if log_problems else ''))
        out.data['bytes_freed'] = freed + log_freed
        if rm.enabled():
            rm.record([event(run, 'run_compacted', path=run.path, bytes_freed=freed + log_freed,
                             snapshots_removed=out.data['snapshots']['removed'], snapshot_bytes=freed,
                             logs_compressed=out.data['logs']['compressed'], log_bytes_freed=log_freed,
                             auto=bool(args.auto))], repo=run.repo, where='event:run_compacted')
        out.data['status'] = 'compacted' if out.ok else 'failed'
        return out, OK if out.ok else FAILED
    except (rm.Busy, rm.Failed) as error:
        out.data['status'] = 'failed'
        out.step('check', False, str(error))
        return out, FAILED
    finally:
        if fd is not None:
            os.close(fd)


def user_settings() -> dict:
    try:
        from forge_decide import load_user
        data, error = load_user()
    except Exception:   # noqa: BLE001 -- a broken user file must not stop a run from starting
        return forge_storage.settings(None)
    features = data.get('features') if not error and isinstance(data, dict) else None
    return forge_storage.settings(features if isinstance(features, dict) else None)


def start_check(args) -> int:
    """At run start: warn when the repository's last measured footprint passes storage.warn-gb, then start
    the background compaction. Prints at most a few lines on stderr; never fails the run."""
    try:
        repo = str(Path(args.repo).resolve())
        policy = user_settings()
        home = _rm().data_home()
        known = forge_storage.load_footprints(home).get(repo) or {}
        size = known.get('bytes')
        if policy['warn_gb'] is not None and isinstance(size, (int, float)) and size > policy['warn_gb'] * forge_storage.GB:
            print('forge: this repository\'s forge files take %s (run dirs %s, worktrees %s), over storage.warn-gb %g.\n'
                  '  See what: forge du --repo %s\n'
                  '  Take back the review snapshots and gzip old logs: forge runs compact --repo %s --logs\n'
                  '  (forge config set storage.warn-gb N|off changes this warning)'
                  % (forge_storage.size_text(size), forge_storage.size_text(known.get('runs')),
                     forge_storage.size_text(known.get('worktrees')), policy['warn_gb'], repo, repo), file=sys.stderr)
        if policy['compact'] == 'auto' or policy['warn_gb'] is not None:
            log = forge_storage.footprint_path(home).with_name('auto.log')
            log.parent.mkdir(parents=True, exist_ok=True)
            argv = [sys.executable, '-I', str(Path(__file__).resolve()), '--auto', '--repo', repo]
            if args.exclude:
                argv += ['--exclude', args.exclude]
            with open(log, 'ab') as sink:
                subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, start_new_session=True,
                                 close_fds=True)
    except Exception:   # noqa: BLE001 -- a run never fails over its storage check
        pass
    return OK


def parser():
    p = Parser(prog='forge runs compact', description='Take back the space run dirs hold for nothing: review snapshots, '
               'and with --logs the dwarf and reviewer log copies (gzipped). Every run stays on disk, viewable, and in the '
               'dataset. See scripts/forge-runs-compact.py for the steps and the safety rules.')
    p.add_argument('run_dirs', nargs='*', metavar='RUN_DIR', help='the plan dirs or solo run dirs to compact')
    which = p.add_mutually_exclusive_group()
    which.add_argument('--repo', metavar='DIR', help='every registered run of this repository')
    which.add_argument('--all', action='store_true', help='every registered run of every repository')
    p.add_argument('--older-than', type=float, metavar='DAYS', help='only runs unchanged for at least DAYS days')
    p.add_argument('--logs', action='store_true', help='also gzip the dwarf and reviewer log copies of 64 KB or more')
    p.add_argument('--dry-run', action='store_true', help='change nothing; report what would be taken back')
    p.add_argument('--json', action='store_true', help='print one JSON object, on failure too')
    p.add_argument('--auto', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--exclude', metavar='RUN_DIR', help=argparse.SUPPRESS)
    p.add_argument('--start-check', action='store_true', help=argparse.SUPPRESS)
    return p


def print_text(results, total, dry_run):
    for out in results:
        data = out.data
        head = '%s  %s' % (data['run_id'] or os.path.basename(data['run_dir'] or ''), data['status'])
        if data['bytes_freed']:
            head += (', about %s to take back' if dry_run else ', %s taken back') % forge_storage.size_text(data['bytes_freed'])
        print(head)
        print('    %s' % data['run_dir'])
        for step in data['steps']:
            print('    %-4s %-9s %s' % ('ok' if step['ok'] else 'FAIL', step['step'], step['detail']),
                  file=sys.stdout if step['ok'] else sys.stderr)
    counted = sum(1 for out in results if out.data['status'] in ('compacted', 'would-compact'))
    if dry_run:
        print('forge runs compact: dry run, nothing was changed; %d run(s) to compact, about %s to take back' % (counted, forge_storage.size_text(total)))
    else:
        print('forge runs compact: %d run(s) compacted, %s taken back' % (counted, forge_storage.size_text(total)))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser().parse_args(argv)
        if args.older_than is not None and args.older_than < 0:
            raise UsageError('--older-than must be 0 or more')
        if args.start_check:
            if not args.repo:
                raise UsageError('--start-check needs --repo')
            return start_check(args)
        if args.auto:
            if not args.repo:
                raise UsageError('--auto needs --repo')
            args.logs, args.json = True, False
        if args.run_dirs and (args.repo or args.all):
            raise UsageError('name run dirs, or use --repo or --all, not both')
        if not args.run_dirs and not args.repo and not args.all:
            raise UsageError('name a run dir, or use --repo DIR (every run of a repository) or --all')
        if args.repo and not os.path.isdir(args.repo):
            raise UsageError('--repo: no such directory: %s' % args.repo)
    except UsageError as error:
        if '--json' in argv:
            json.dump(dict(ok=False, dry_run='--dry-run' in argv, bytes_freed=0, runs=[], error='usage: %s' % error), sys.stdout, indent=2)
            sys.stdout.write('\n')
        else:
            parser().print_usage(sys.stderr)
            print('forge runs compact: %s' % error, file=sys.stderr)
        return REFUSED
    batch = not args.run_dirs
    if batch:
        import forge_locations
        forge_locations.ensure_imported()      # once: what the run registry and the disk already hold
    repo = str(repo_of(Path(args.repo))) if args.repo else None
    dirs = args.run_dirs if args.run_dirs else registered(Path(repo) if repo else None)
    excluded = os.path.realpath(args.exclude) if args.exclude else None
    results, codes = [], []
    policy = user_settings() if args.auto else None
    if policy is not None and policy['compact'] != 'auto':
        dirs = []                       # storage.compact off: only the footprint is measured
    for path in dirs:
        if excluded and os.path.realpath(path) == excluded:
            continue
        try:
            out, code = compact_one(path, args, batch)
        except KeyboardInterrupt:
            out, code = Outcome(path, args.json), 130
            out.data['status'] = 'failed'
            out.step('interrupted', False, 'interrupted; see the steps above for what was done')
            results.append(out)
            codes.append(code)
            break
        except Exception as error:   # noqa: BLE001 -- --json must still print one object
            out, code = Outcome(path, args.json), FAILED
            out.data['status'] = 'failed'
            out.step('internal', False, '%s: %s' % (type(error).__name__, error))
        results.append(out)
        codes.append(code)
    if args.auto and repo and (policy['warn_gb'] is not None or policy['compact'] == 'auto'):
        measured = forge_storage.footprints_of(forge_storage.report(Path(repo), budget_s=600))
        forge_storage.save_footprints(_rm().data_home(), {repo: measured.get(repo) or dict(bytes=0, runs=0, worktrees=0, count=0)})
    total = sum(out.data['bytes_freed'] for out in results)
    if batch:
        codes = [c for c in codes if c != BUSY]
    code = max(codes) if codes else OK
    if args.auto:
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        for out in results:
            if out.data['status'] in ('compacted', 'failed'):
                print('%s %s %s %s' % (stamp, out.data['status'], out.data['run_dir'], forge_storage.size_text(out.data['bytes_freed'])))
        return OK
    if args.json:
        json.dump(dict(ok=code == OK, dry_run=args.dry_run, bytes_freed=total, runs=[out.data for out in results]), sys.stdout, indent=2)
        sys.stdout.write('\n')
    else:
        if not results:
            print('forge runs compact: no registered runs%s' % (' for ' + repo if repo else ''))
        else:
            print_text(results, total, args.dry_run)
    return code


if __name__ == '__main__':
    sys.exit(main())
