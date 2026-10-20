#!/usr/bin/env python3
"""Fail-first evidence: do the tests a change adds or edits fail without the change?

    forge-fail-first.py run --repo <review snapshot> --base <commit> --reviewed <commit>
                        --command '<test command with {files}>' --out <failfirst.json>
                        [--deps-from <dir>] [--timeout S]

Opt-in (`--fail-first` on forge-solo.sh and `plan`) and advisory. A test that passes on the
code it is meant to protect, before that code changed, proves nothing about the change. For
every test file the diff adds or edits, this runs the project's own per-file test command
(`.forge/verify-subset`, which must contain `{files}`) twice, each in a throwaway export:

  - with the change: the reviewed tree;
  - without it: the base tree plus the change's test-path files only (test files, fixtures
    and helpers under test directories), so the new tests meet the old code.

Each file ends up `proven` (passes with, fails without), `passes_without_change` (passes both
ways: it does not prove the change), `inconclusive` (fails even with the change: a broken test
or a missing dependency, no evidence either way) or `error` (could not export or timed out).

The result goes to the JSON file, and the reviewer's prompt section goes to stdout; the QA
prompt asks the reviewer to report every `passes_without_change` file as a P2 finding. Forge
never changes a status or verdict on its own from this. The review snapshot is only read:
exports use a private index, so no fingerprint can change. Always exits 0 except on usage
errors (2).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

# The same two conditions as forge_jev/subset.py: a test path, and for the files that are run,
# a source suffix (README.md and JSON fixtures under tests/ are carried over, never run).
TEST_PATH = re.compile(r'(^|/)(tests?|spec|specs|__tests__)/'
                       r'|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$')
CODE_SUFFIXES = ('.py', '.go', '.js', '.jsx', '.ts', '.tsx', '.rb', '.rs', '.java',
                 '.kt', '.php', '.sh', '.mjs', '.cjs')
DEP_DIRS = ('node_modules', '.venv', 'venv', 'vendor')
MAX_FILES = 20
TAIL = 2000


def _git(repo, *args, env=None, data=None):
    return subprocess.run(['git', '-C', str(repo), *args], input=data, capture_output=True,
                          env=env, timeout=120, check=True).stdout


def changed_paths(repo, base, reviewed):
    """[(status, path)] of the change, renames split into a delete and an add."""
    out = _git(repo, 'diff', '--name-status', '-z', '--no-renames', base, reviewed, '--', '.', ':(exclude).forge')
    parts = out.decode('utf-8', 'surrogateescape').split('\0')
    return [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]


def export(repo, tree, dest, overlay_from=None, overlay=(), deps_from=None):
    """Check `tree` out into `dest` through a private index; `overlay` paths come from `overlay_from`."""
    handle, index = tempfile.mkstemp(prefix='forge-failfirst-index.')
    os.close(handle); os.unlink(index)
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        _git(repo, 'read-tree', tree, env=env)
        for path in overlay:
            entry = _git(repo, 'ls-tree', '-z', overlay_from, '--', path).decode('utf-8', 'surrogateescape')
            if entry:
                _git(repo, 'update-index', '-z', '--index-info', env=env, data=entry.encode('utf-8', 'surrogateescape'))
            else:
                _git(repo, 'update-index', '--force-remove', '--', path, env=env)
        _git(repo, 'checkout-index', '--all', '--force', '--prefix=' + str(dest) + '/', env=env)
    finally:
        if os.path.exists(index):
            os.unlink(index)
    for name in DEP_DIRS:  # dependencies are not in the tree; link them so the export can run
        source = Path(deps_from) / name if deps_from else None
        if source and source.is_dir() and not (Path(dest) / name).exists():
            try:
                os.symlink(source, Path(dest) / name)
            except OSError:
                pass


def run_one(command, cwd, timeout):
    started = time.time()
    try:
        done = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout, errors='replace')
    except subprocess.TimeoutExpired:
        return dict(rc=None, seconds=round(time.time() - started, 3), tail='timed out')
    except OSError as error:
        return dict(rc=None, seconds=round(time.time() - started, 3), tail=str(error))
    tail = ((done.stdout or '') + (done.stderr or ''))[-TAIL:]
    return dict(rc=done.returncode, seconds=round(time.time() - started, 3), tail=tail)


def classify(with_change, without):
    if with_change['rc'] is None or without['rc'] is None:
        return 'error'
    if with_change['rc'] != 0:
        return 'inconclusive'
    return 'proven' if without['rc'] != 0 else 'passes_without_change'


def evaluate(repo, base, reviewed, command, timeout, deps_from=None):
    result = dict(version=1, status='ran', command=command, base=base, reviewed=reviewed, tests=[],
                  skipped=[], started=time.time())
    if '{files}' not in (command or ''):
        result.update(status='skipped', reason='no per-file test command: put one with {files} in .forge/verify-subset')
        return result
    changes = changed_paths(repo, base, reviewed)
    support = [path for _status, path in changes if TEST_PATH.search(path)]
    tests = [path for status, path in changes
             if status != 'D' and TEST_PATH.search(path) and path.endswith(CODE_SUFFIXES)]
    if not tests:
        result.update(status='skipped', reason='the change adds or edits no test files')
        return result
    result['skipped'] = tests[MAX_FILES:]
    work = Path(tempfile.mkdtemp(prefix='forge-failfirst-'))
    try:
        with_dir, without_dir = work / 'with', work / 'without'
        export(repo, reviewed, with_dir, deps_from=deps_from)
        export(repo, base, without_dir, overlay_from=reviewed, overlay=support, deps_from=deps_from)
        for path in tests[:MAX_FILES]:
            line = command.replace('{files}', shlex.quote(path))
            with_change = run_one(line, with_dir, timeout)
            without = run_one(line, without_dir, timeout)
            result['tests'].append(dict(path=path, result=classify(with_change, without),
                                        with_change=with_change, without_change=without))
    except (OSError, subprocess.SubprocessError) as error:
        result.update(status='error', reason=f'could not export the change: {error}'[:500])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    result['ended'] = time.time()
    return result


def prompt_section(result):
    """What the reviewer reads; empty when there is nothing to say."""
    if result.get('status') != 'ran' or not result.get('tests'):
        return ''
    words = dict(proven='fails without the change and passes with it',
                 passes_without_change='PASSES WITHOUT THE CHANGE: it does not prove the change',
                 inconclusive='fails even with the change (a broken test or the environment): no evidence either way',
                 error='could not be run (timeout or error): no evidence either way')
    lines = ['Fail-first evidence (observed by forge, not claimed by the implementer): each test file this',
             'change adds or edits was run with the change and against the code WITHOUT its non-test changes.',
             'Command: ' + result['command']]
    lines += [f'  - {item["path"]}: {words[item["result"]]}' for item in result['tests']]
    if result.get('skipped'):
        lines.append(f'  ({len(result["skipped"])} more test files were not run)')
    if any(item['result'] == 'passes_without_change' for item in result['tests']):
        lines += ['Report each test file that passes without the change as a [P2][CONFIRMED] finding at that file:',
                  'a test that cannot fail does not protect this change. If the test is not meant to catch it',
                  '(a refactor kept green, a pure rename), say why instead.']
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(prog='forge-fail-first.py')
    sub = parser.add_subparsers(dest='cmd', required=True)
    run = sub.add_parser('run')
    run.add_argument('--repo', required=True)
    run.add_argument('--base', required=True)
    run.add_argument('--reviewed', required=True)
    run.add_argument('--command', default='')
    run.add_argument('--out', required=True)
    run.add_argument('--deps-from')
    run.add_argument('--timeout', type=float, default=float(os.environ.get('FORGE_FAIL_FIRST_TIMEOUT') or 600))
    args = parser.parse_args(argv)
    try:
        result = evaluate(args.repo, args.base, args.reviewed, args.command.strip(), max(1.0, args.timeout),
                          deps_from=args.deps_from)
    except Exception as error:  # advisory: never break the run
        result = dict(version=1, status='error', reason=str(error)[:500], command=args.command, tests=[])
    counts = {}
    for item in result.get('tests', []):
        counts[item['result']] = counts.get(item['result'], 0) + 1
    result['counts'] = counts
    try:
        Path(args.out).write_text(json.dumps(result, indent=1) + '\n', encoding='utf-8')
    except OSError:
        pass
    sys.stdout.write(prompt_section(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
