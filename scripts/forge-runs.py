#!/usr/bin/env python3
"""forge runs [--repo DIR | --all] [--limit N] [--json]: this repo's recent runs, in the terminal.

Every solo run, `plan` and `run` registers its run dir (forge_registry.py); this lists them newest
first, one line each (run id, kind, age, task states, verification and final review, goal) with the
run dir under it, so you can see what happened without a browser and get the dir to resume, retry
or open with `forge ui open`. The repo is the Git top level of the current directory unless
`--repo` names one; `--all` lists the runs of every repo. It reads the same run summaries as
forge-ui and never writes anything.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / 'scripts'))
sys.path.insert(0, str(SKILL / 'ui'))

import forge_registry  # noqa: E402
from forge_ui import reader  # noqa: E402

FIELDS = ('key', 'run_id', 'kind', 'goal', 'repo', 'path', 'created', 'updated', 'task_count', 'counts',
          'verification_status', 'final_review')


def repo_of(start: Path) -> Path:
    """The Git top level of `start`, else `start` itself: the key runs are registered under."""
    try:
        out = subprocess.run(['git', '-C', str(start), 'rev-parse', '--show-toplevel'], capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    return start.resolve()


def registered(path: Path) -> list[str]:
    """The run dirs one registry file lists (absolute paths, comments skipped)."""
    try:
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if line.strip() and not line.startswith('#') and os.path.isabs(line.strip())]


def runs(repo: Path | None) -> list[dict]:
    if repo is None:
        found = reader.discover(registry=True, registry_dir=forge_registry.registry_dir())
    else:
        found = reader.discover(plans=registered(forge_registry.runs_registry(repo)), registry=False)
    result = []
    for path, source in found:
        try:
            summary = reader.summarize(path, source)
        except Exception:  # one unreadable run dir must not hide the others
            continue
        result.append({k: summary.get(k) for k in FIELDS})
    return sorted(result, key=lambda r: r['updated'] if r['updated'] is not None else float('-inf'), reverse=True)


def age(epoch, now) -> str:
    if epoch is None:
        return '?'
    s = max(0, now - epoch)
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if s >= size:
            return f'{int(s // size)}{unit} ago'
    return 'just now'


def line(run: dict, now: float, with_repo: bool = False) -> str:
    counts = ' · '.join(f'{status.lower()} {n}' for status, n in (run['counts'] or {}).items()) or 'no tasks'
    checks = ''.join(f'  {label} {run[key]}' for label, key in (('verify', 'verification_status'), ('review', 'final_review'))
                     if run.get(key))
    goal = str(run['goal'] or 'Untitled run').splitlines()[0]
    if len(goal) > 72:
        goal = goal[:71] + '…'
    repo = f"  {Path(run['repo']).name or run['repo']}" if with_repo and run.get('repo') else ''
    return f"{run['run_id'] or run['key']}  {run['kind']}{repo}  {age(run['updated'], now)}  {counts}{checks}  {goal}\n    {run['path']}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='forge runs', description="This repo's recent forge runs, newest first. Read-only.")
    which = parser.add_mutually_exclusive_group()
    which.add_argument('--repo', type=Path, metavar='DIR', help='the repo whose runs to list (default: the Git top level of the current directory)')
    which.add_argument('--all', action='store_true', help='list the runs of every repo')
    parser.add_argument('--limit', type=int, default=10, metavar='N', help='at most N runs (default 10; 0 lists them all)')
    parser.add_argument('--json', action='store_true', help='print the runs as a JSON array, for scripts and agents')
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit must be 0 or more')
    if args.repo is not None and not args.repo.is_dir():
        parser.error(f'--repo: no such directory: {args.repo}')
    repo = None if args.all else repo_of(args.repo if args.repo is not None else Path.cwd())
    found = runs(repo)
    shown = found[:args.limit] if args.limit else found
    if args.json:
        json.dump(shown, sys.stdout, indent=2)
        sys.stdout.write('\n')
        return 0
    where = 'every repo' if repo is None else str(repo)
    if not found:
        print(f'forge runs: no runs registered for {where}')
        print('Runs register when they start (/forge "<goal>" --dwarf <model>); FORGE_RUN_REGISTRY=off turns that off.')
        return 0
    print(f'forge runs for {where}: {len(shown)} of {len(found)}, newest first')
    now = time.time()
    for run in shown:
        print(line(run, now, with_repo=repo is None))
    return 0


if __name__ == '__main__':
    sys.exit(main())
