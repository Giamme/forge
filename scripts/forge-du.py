#!/usr/bin/env python3
"""forge du [--repo DIR | --all] [--top N] [--json]: what forge takes on disk, and the command that takes each part back (#139).

Reports, for the repository at the current directory (its Git top level) unless `--repo` names one, or for
every repository with `--all`:

  run dirs    each registered run dir: its total, review snapshots, logs (plain and gzipped), other files,
              and what `forge runs compact --logs` would take back
  strays      what forge does not create but found inside a run dir (a dependency install, a .git):
              reported, never removed by forge
  worktrees   each run's worktree folder (<repo>/../.forge-worktrees/<run id>); forge-ui's Worktrees
              screen has the state of each one and the command that removes it safely
  data home   the dataset (kept: it is what forge learns from), its blobs, archives, exports, legacy

Sizes are disk usage, as `du` counts it (forge_storage.py has the rules). It reads only, and measuring a
large tree takes a while: every file is visited. The repository's total is kept for the storage.warn-gb
check at run start.

--json prints {repo, computed, complete, runs, strays, worktrees, data_home, totals, commands}.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))

import forge_storage  # noqa: E402
from forge_storage import size_text  # noqa: E402


def repo_of(start: Path) -> Path:
    try:
        out = subprocess.run(['git', '-C', str(start), 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return start.resolve()


def data_home() -> Path:
    from forge_dataset import data_home
    return data_home()


def commands(result: dict) -> list[dict]:
    """What takes each part back, as text to run: never run here."""
    out = []
    scope = ('--repo ' + shlex.quote(result['repo'])) if result['repo'] else '--all'
    if result['totals']['reclaimable']:
        out.append(dict(label='remove the leftover review snapshots and gzip the log copies (every run stays, and the dataset keeps it)',
                        command='forge runs compact %s --logs' % scope, bytes=result['totals']['reclaimable']))
    if result['worktrees']:
        out.append(dict(label='see each worktree\'s state and the command that removes it safely', command='forge ui open',
                        bytes=result['totals']['worktrees'], note='then open Worktrees'))
    return out


def age(epoch, now) -> str:
    if epoch is None:
        return '?'
    s = max(0, now - epoch)
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if s >= size:
            return '%d%s ago' % (s // size, unit)
    return 'just now'


def print_text(result: dict, top: int) -> None:
    now = time.time()
    t = result['totals']
    where = result['repo'] or 'every repository'
    print('forge du for %s: %s in %d run dir(s), %s in %d worktree folder(s)%s' % (
        where, size_text(t['runs']), len(result['runs']), size_text(t['worktrees']), len(result['worktrees']),
        ', data home %s' % size_text(t['data_home']) if result['data_home'] else ''))
    if t['reclaimable']:
        print('  about %s can be taken back without losing a run: %s in review snapshots, the rest by gzipping logs'
              % (size_text(t['reclaimable']), size_text(t['snapshots'])))
    if not result['complete']:
        print('  (measuring stopped at its time limit: some sizes are lower bounds)')
    if result['runs']:
        shown = result['runs'][:top] if top else result['runs']
        print('\nrun dirs, largest first (%d of %d):' % (len(shown), len(result['runs'])))
        print('  %-28s %9s %10s %9s %9s %12s %12s' % ('run', 'total', 'snapshots', 'logs', 'other', 'not forge\'s', 'reclaimable'))
        for run in shown:
            name = run['run_id'] if len(run['run_id']) <= 28 else run['run_id'][:27] + '…'
            print('  %-28s %9s %10s %9s %9s %12s %12s  %s' % (
                name, size_text(run['total']), size_text(run['snapshots']), size_text(run['logs'] + run['gz']),
                size_text(run['other'] + run['fractal']), size_text(run['strays']) if run['strays'] else '-',
                size_text(run['reclaimable']), age(run['updated'], now)))
            print('    %s' % run['path'])
    if result['strays']:
        print('\nnot forge\'s, found inside run dirs (forge never removes these; delete them by hand if you do not need them):')
        for stray in result['strays']:
            print('  %9s  %s' % (size_text(stray['bytes']), stray['path']))
    if result['worktrees']:
        shown = result['worktrees'][:top] if top else result['worktrees']
        print('\nworktree folders, largest first (%d of %d):' % (len(shown), len(result['worktrees'])))
        for wt in shown:
            print('  %9s  %s' % (size_text(wt['bytes']), wt['path']))
    home = result['data_home']
    if home:
        p = home['parts']
        print('\ndata home %s: %s' % (home['path'], size_text(home['bytes'])))
        print('  dataset %s and blobs %s (kept: what forge learns from); archives %s (forge runs rm); exports %s '
              '(forge dataset export writes them again); legacy %s; other %s'
              % (size_text(p['dataset']), size_text(p['blobs']), size_text(p['archive']), size_text(p['exports']),
                 size_text(p['legacy']), size_text(p['storage'] + p['other'])))
    if result['commands']:
        print('\nto take space back:')
        for c in result['commands']:
            print('  %s%s\n    %s' % (c['command'], ('  (' + c['note'] + ')') if c.get('note') else '', c['label']))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='forge du', description='What forge takes on disk: run dirs, worktrees and its data '
                                     'home, and the command that takes each part back. Reads only.')
    which = parser.add_mutually_exclusive_group()
    which.add_argument('--repo', type=Path, metavar='DIR', help='the repository (default: the Git top level of the current directory)')
    which.add_argument('--all', action='store_true', help='every repository forge has runs for')
    parser.add_argument('--top', type=int, default=10, metavar='N', help='list the N largest run dirs and worktree folders (default 10; 0: all)')
    parser.add_argument('--json', action='store_true', help='print one JSON object, for scripts and agents')
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.top < 0:
        parser.error('--top must be 0 or more')
    if args.repo is not None and not args.repo.is_dir():
        parser.error('--repo: no such directory: %s' % args.repo)
    repo = None if args.all else repo_of(args.repo if args.repo is not None else Path.cwd())
    home = data_home()
    tty = not args.json and sys.stderr.isatty()

    def progress(index, count, _path):
        if tty:
            sys.stderr.write('\rmeasuring run dir %d of %d…' % (index + 1, count))
            sys.stderr.flush()

    result = forge_storage.report(repo, home=home, progress=progress)
    if tty:
        sys.stderr.write('\r\033[K')
    result['commands'] = commands(result)
    forge_storage.save_footprints(home, forge_storage.footprints_of(result))
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write('\n')
        return 0
    if not result['runs'] and not result['worktrees']:
        print('forge du: no registered runs for %s' % (repo or 'any repository'))
        if result['data_home']:
            print('data home %s: %s' % (result['data_home']['path'], size_text(result['data_home']['bytes'])))
        return 0
    print_text(result, args.top)
    return 0


if __name__ == '__main__':
    sys.exit(main())
