#!/usr/bin/env python3
"""Pre-review tests: run only the tests an attempt affects before paying for its review (#85).

    forge-pre-review.py run --repo <review snapshot> --base <commit> --reviewed <commit>
                        --command '<test command with {files} and/or {changed}>'
                        --out <prereview.json> --report <prereview.md>
                        [--map <.forge/test-map>] [--deps-from <dir>] [--max-files N]
                        [--timeout S] [--slots-dir <dir> --slots N] [--log <file>]
    forge-pre-review.py select --repo <dir> --base <commit> --reviewed <commit> [--map <file>]
                        [--max-files N]

Opt-in (`plan --pre-review-tests`). Never the whole suite. The selection, in order:

  1. the test files the change adds or edits;
  2. for each changed source file, the tests from the first of these that answers:
     `.forge/test-map` (one rule per line: a source glob, then test paths or globs; `-` for
     "no tests"), the naming convention (foo.py -> test_foo.py, foo_test.py, foo.test.js, ...),
     then a reverse lookup (test files that name the changed module);
  3. with `{changed}` in the command, the changed paths themselves, for a runner that finds
     the related tests on its own (`jest --findRelatedTests {changed}`, `go test`, ...).

A change to shared test infrastructure (a conftest, a test helper, a lockfile, a build file),
more than --max-files selected files, or a run past --timeout makes the check `skipped` (too
broad): the end-of-run verification is where those belong.

The outcome, and the exit code the caller acts on:

  passed        (0) every selected test passed
  flaky         (0) the run failed, but every failing file passed when rerun alone
  pre-existing  (0) every test that fails here fails on the task base too
  failed        (1) a test fails on this attempt and not on the task base (or is new): the
                    attempt goes back to the implementer without a review; --report holds
                    the failing tests and the end of their output, labelled as forge's
  skipped/error (0) nothing to run, too broad, no command, or the check itself broke

stdout is the reviewer's evidence section (empty when there is none); --report is written
only for `failed`; --log gets one `<epoch> <TAB> <status> <TAB> <seconds>` line per check, which the
run summary totals. The review snapshot is only read: every run happens in a throwaway export
made through a private index, so no fingerprint changes. Exit 2 is a usage error.
"""
from __future__ import annotations

import argparse
import fnmatch
import importlib.util
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

_SPEC = importlib.util.spec_from_file_location('forge_fail_first', Path(__file__).resolve().parent / 'forge-fail-first.py')
failfirst = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(failfirst)

MAX_FILES = 20
TIMEOUT_S = 120.0
SLOTS = 2
TAIL = 3000
READ_LIMIT = 1 << 20               # a test file larger than this is not searched by the reverse lookup

# A test file by name: what a runner picks up as a test, as opposed to a helper or fixture
# that lives beside the tests.
TEST_NAME = re.compile(r'(^|/)test_[^/]*$|(^|/)[^/]*_test\.[^/]+$|(^|/)[^/]*\.(test|spec)\.[^/]+$'
                       r'|(^|/)[^/]*_spec\.[^/]+$|(^|/)__tests__/')
# Shared infrastructure: a change here can break any test, so no selection is honest.
INFRA_NAMES = {
    'conftest.py', 'pytest.ini', 'tox.ini', 'setup.cfg', 'setup.py', 'pyproject.toml', 'noxfile.py',
    'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'npm-shrinkwrap.json', 'bun.lockb',
    'tsconfig.json', 'babel.config.js', 'Cargo.toml', 'Cargo.lock', 'go.mod', 'go.sum', 'Gemfile',
    'Gemfile.lock', 'poetry.lock', 'uv.lock', 'Pipfile', 'Pipfile.lock', 'requirements.txt', 'Makefile',
    'CMakeLists.txt', 'build.gradle', 'build.gradle.kts', 'settings.gradle', 'pom.xml', 'composer.json',
    'composer.lock', 'Dockerfile',
}
INFRA_PATTERNS = ('jest.config.*', 'vitest.config.*', 'karma.conf.*', 'webpack.config.*', 'vite.config.*',
                  'requirements*.txt', '.babelrc*')


def is_test(path: str) -> bool:
    return bool(failfirst.TEST_PATH.search(path)) and bool(TEST_NAME.search(path)) and path.endswith(failfirst.CODE_SUFFIXES)


def is_infra(path: str) -> bool:
    """Shared test infrastructure: a build or lock file, or code under a test directory that is not a test."""
    name = path.rsplit('/', 1)[-1]
    if name in INFRA_NAMES or any(fnmatch.fnmatch(name, pattern) for pattern in INFRA_PATTERNS):
        return True
    return bool(failfirst.TEST_PATH.search(path)) and path.endswith(failfirst.CODE_SUFFIXES) and not is_test(path)


def read_map(path) -> list:
    """[(source glob, [test globs])] from a .forge/test-map; `-` alone means "no tests"."""
    rules = []
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace') if path else ''
    except OSError:
        return rules
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            rules.append((parts[0], [p for p in parts[1:] if p != '-']))
    return rules


def _stems(path: str) -> list:
    """The names a test would use for a source file: its stem, with - and _ both ways; a
    package's __init__ goes by its directory."""
    name = path.rsplit('/', 1)[-1]
    stem = name.split('.', 1)[0]
    if stem in ('__init__', 'index', 'mod', 'lib') and '/' in path:
        stem = path.rsplit('/', 2)[-2]
    out = [stem]
    for variant in (stem.replace('-', '_'), stem.replace('_', '-')):
        if variant not in out:
            out.append(variant)
    return [s for s in out if len(s) >= 3]


def by_convention(path: str, tests: list) -> list:
    names = set()
    for stem in _stems(path):
        names |= {'test_' + stem, stem + '_test', stem + '.test', stem + '.spec', stem + '_spec'}
    found = []
    for test in tests:
        base = test.rsplit('/', 1)[-1]
        for suffix in failfirst.CODE_SUFFIXES:
            if base.endswith(suffix) and base[:-len(suffix)] in names:
                found.append(test)
                break
    return found


def by_reference(path: str, tests: list, root: Path) -> list:
    """Test files that name the changed module (an import, a require, a path in a string)."""
    patterns = [re.compile(r'(?<![\w-])' + re.escape(stem) + r'(?![\w-])') for stem in _stems(path)]
    found = []
    for test in tests:
        file = root / test
        try:
            if file.stat().st_size > READ_LIMIT:
                continue
            text = file.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        if any(p.search(text) for p in patterns):
            found.append(test)
    return found


def tree_files(repo, commit) -> list:
    out = failfirst._git(repo, 'ls-tree', '-r', '-z', '--name-only', commit)
    return [p for p in out.decode('utf-8', 'surrogateescape').split('\0') if p]


def select(repo, base, reviewed, root: Path, map_path=None, max_files=MAX_FILES) -> dict:
    """What to run, and why; `too_broad` set when the check must not run at all."""
    changes = [(s, p) for s, p in failfirst.changed_paths(repo, base, reviewed) if not p.startswith('.forge/')]
    files = tree_files(repo, reviewed)
    tests = [p for p in files if is_test(p)]
    selected, why = [], {}

    def add(path, reason):
        if path not in why:
            selected.append(path)
            why[path] = reason

    result = dict(changed=[p for s, p in changes if s != 'D'], selected=selected, why=why, too_broad=None,
                  unmatched=[])
    infra = [p for _s, p in changes if is_infra(p)]
    if infra:
        result['too_broad'] = 'the change touches shared test infrastructure: ' + ', '.join(infra[:5])
        return result
    for status, path in changes:
        if status != 'D' and is_test(path):
            add(path, 'changed')
    rules = read_map(map_path)
    present = set(files)
    for status, path in changes:
        if is_test(path) or failfirst.TEST_PATH.search(path) or not path.endswith(failfirst.CODE_SUFFIXES):
            continue                       # tests are already in; docs, data and fixtures name no tests
        matched = [globs for source, globs in rules if fnmatch.fnmatch(path, source)]
        if matched:
            for globs in matched:
                for pattern in globs:
                    for test in sorted(p for p in present if fnmatch.fnmatch(p, pattern)):
                        add(test, 'map: ' + path)
            continue
        found = by_convention(path, tests) or by_reference(path, tests, root)
        how = 'convention' if found and found == by_convention(path, tests) else 'reference'
        for test in found:
            add(test, how + ': ' + path)
        if not found:
            result['unmatched'].append(path)
    if len(selected) > max_files:
        result['too_broad'] = '%d test files selected, more than the cap of %d' % (len(selected), max_files)
    return result


# --- running -------------------------------------------------------------------------
def _fill(command: str, files: list, changed: list) -> str:
    return (command.replace('{files}', ' '.join(shlex.quote(f) for f in files))
                   .replace('{changed}', ' '.join(shlex.quote(c) for c in changed)))


class Slot:
    """One of N machine-wide run slots under a directory, so parallel tasks do not run N suites
    at once. A slot whose holder is gone is taken over."""

    def __init__(self, directory, count):
        self.directory, self.count, self.path = Path(directory) if directory else None, max(1, count), None

    def __enter__(self):
        if self.directory is None:
            return self
        self.directory.mkdir(parents=True, exist_ok=True)
        while True:
            for n in range(self.count):
                path = self.directory / ('slot-%d' % n)
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                except FileExistsError:
                    try:
                        os.kill(int(path.read_text().strip() or 0), 0)
                    except (ProcessLookupError, ValueError):
                        path.unlink(missing_ok=True)          # its holder is gone
                    except (OSError, PermissionError):
                        pass
                    continue
                with os.fdopen(fd, 'w') as handle:
                    handle.write(str(os.getpid()))
                self.path = path
                return self
            time.sleep(0.5)

    def __exit__(self, *exc):
        if self.path:
            self.path.unlink(missing_ok=True)


def evaluate(repo, base, reviewed, command, map_path=None, deps_from=None, max_files=MAX_FILES,
             timeout=TIMEOUT_S, slots_dir=None, slots=SLOTS) -> dict:
    result = dict(version=1, status='skipped', reason=None, command=command, selected=[], why={},
                  changed=[], runs=[], failing=[], preexisting=[], flaky=[], started=time.time())
    if '{files}' not in command and '{changed}' not in command:
        result['reason'] = 'no test command: put one with {files} or {changed} in .forge/verify-subset'
        return result
    work = Path(tempfile.mkdtemp(prefix='forge-prereview-'))
    try:
        here = work / 'reviewed'
        failfirst.export(repo, reviewed, here, deps_from=deps_from)
        chosen = select(repo, base, reviewed, here, map_path, max_files)
        result.update(selected=chosen['selected'], why=chosen['why'], changed=chosen['changed'],
                      unmatched=chosen['unmatched'])
        if chosen['too_broad']:
            result['reason'] = 'too broad: ' + chosen['too_broad']
            return result
        uses_files = '{files}' in command
        if uses_files and not chosen['selected']:
            result['reason'] = 'no test covers the change'
            return result
        if not uses_files and not chosen['changed']:
            result['reason'] = 'the change has no paths to pass to {changed}'
            return result
        if not uses_files and len(chosen['changed']) > max_files:
            result['reason'] = 'too broad: %d changed paths, more than the cap of %d' % (len(chosen['changed']), max_files)
            return result
        deadline = time.monotonic() + timeout
        with Slot(slots_dir, slots):
            outcome = _run(result, command, chosen, here, work, repo, base, deps_from, deadline)
        result.update(outcome)
    except (OSError, subprocess.SubprocessError) as error:
        result.update(status='error', reason=('the check could not run: %s' % error)[:500])
    finally:
        shutil.rmtree(work, ignore_errors=True)
        result['ended'] = time.time()
    return result


def _run(result, command, chosen, here, work, repo, base, deps_from, deadline) -> dict:
    def left():
        return deadline - time.monotonic()

    def run(kind, line, cwd, path=None):
        if left() <= 0:
            return None
        done = failfirst.run_one(line, cwd, left())
        done.update(kind=kind, path=path)
        result['runs'].append(done)
        return done

    uses_files = '{files}' in command
    files, changed = chosen['selected'], chosen['changed']
    first = run('all', _fill(command, files, changed), here)
    if first is None or first['rc'] is None:
        if first is not None and first['tail'] != 'timed out':
            return dict(status='error', reason='the test command could not run: ' + first['tail'][-200:])
        return dict(status='skipped', reason='too broad: the selected tests took longer than %ds' % int(TIMEOUT_S if first is None else first['seconds']))
    if first['rc'] == 0:
        return dict(status='passed')
    # Which files fail on their own: the rest failed only together, or only that once (flaky).
    if uses_files:
        failing = []
        for path in files:
            alone = run('alone', _fill(command, [path], changed), here, path)
            if alone is None:
                return dict(status='skipped', reason='too broad: no time left to rerun the failing tests')
            if alone['rc'] != 0:
                failing.append(path)
    else:
        again = run('again', _fill(command, files, changed), here)
        if again is None:
            return dict(status='skipped', reason='too broad: no time left to rerun the failing tests')
        failing = ['{changed}'] if again['rc'] != 0 else []
    if not failing:
        return dict(status='flaky', flaky=list(files) if uses_files else ['{changed}'])
    # A test that fails on the task base too was red before this attempt: not its fault.
    at_base = set(tree_files(repo, base))
    old = work / 'base'
    failfirst.export(repo, base, old, deps_from=deps_from)
    blamed, preexisting = [], []
    for path in failing:
        if path == '{changed}':
            present = [c for c in changed if c in at_base]
            done = run('base', _fill(command, files, present), old) if present else None
        else:
            done = run('base', _fill(command, [path], changed), old, path) if path in at_base else None
        (preexisting if done is not None and done['rc'] not in (0, None) else blamed).append(path)
    if blamed:
        return dict(status='failed', failing=blamed, preexisting=preexisting)
    return dict(status='pre-existing', preexisting=preexisting)


# --- what people read ------------------------------------------------------------------
def _tail(result) -> str:
    for run in reversed(result['runs']):
        if run['kind'] in ('alone', 'again', 'all') and run['rc'] not in (0, None):
            return run['tail'][-TAIL:]
    return ''


def report(result) -> str:
    """The retry findings of a `failed` check: what the implementer gets instead of a review."""
    lines = ['Pre-review tests (observed by forge, not by a reviewer): forge ran the tests this attempt',
             'affects before dispatching a review, and they failed, so no reviewer was asked.',
             'Command: ' + result['command'],
             'Failing on this attempt (they pass on the task base, or are new):']
    lines += ['  - ' + path for path in result['failing']]
    if result['preexisting']:
        lines += ['Failing on the task base too (not caused by this attempt):']
        lines += ['  - ' + path for path in result['preexisting']]
    lines += ['', 'End of the output:', '```', _tail(result).rstrip(), '```', '',
              'Fix these failures; the review runs once they pass.']
    return '\n'.join(lines) + '\n'


def evidence(result) -> str:
    """What the reviewer reads; empty when nothing ran."""
    status = result['status']
    if status not in ('passed', 'flaky', 'pre-existing'):
        return ''
    ran = result['selected'] or ['(the runner\'s related tests for the changed paths)']
    lines = ['Pre-review tests (observed by forge, not claimed by the implementer): before this review,',
             'forge ran the tests this change affects on the reviewed tree.',
             'Command: ' + result['command']]
    if status == 'passed':
        lines.append('All passed: ' + ', '.join(ran))
    elif status == 'flaky':
        lines.append('The run failed once, then every test passed when rerun alone (flaky): ' + ', '.join(ran))
    else:
        lines.append('Some fail, but they fail on the task base too, so this change did not cause them:')
        lines += ['  - ' + path for path in result['preexisting']]
    lines.append('Run further tests only where these do not cover the change.')
    return '\n'.join(lines) + '\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='forge-pre-review.py')
    sub = parser.add_subparsers(dest='cmd', required=True)
    for name in ('run', 'select'):
        p = sub.add_parser(name)
        p.add_argument('--repo', required=True)
        p.add_argument('--base', required=True)
        p.add_argument('--reviewed', required=True)
        p.add_argument('--map')
        p.add_argument('--max-files', type=int, default=int(os.environ.get('FORGE_PRE_REVIEW_MAX_FILES') or MAX_FILES))
        if name == 'run':
            p.add_argument('--command', default='')
            p.add_argument('--out', required=True)
            p.add_argument('--report', required=True)
            p.add_argument('--deps-from')
            p.add_argument('--timeout', type=float, default=float(os.environ.get('FORGE_PRE_REVIEW_TIMEOUT') or TIMEOUT_S))
            p.add_argument('--slots-dir')
            p.add_argument('--slots', type=int, default=SLOTS)
            p.add_argument('--log')
    args = parser.parse_args(argv)
    if args.cmd == 'select':
        work = Path(tempfile.mkdtemp(prefix='forge-prereview-'))
        try:
            failfirst.export(args.repo, args.reviewed, work / 'reviewed')
            chosen = select(args.repo, args.base, args.reviewed, work / 'reviewed', args.map, max(1, args.max_files))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        print(json.dumps(chosen, indent=1))
        return 0
    try:
        result = evaluate(args.repo, args.base, args.reviewed, args.command.strip(), args.map, args.deps_from,
                          max(1, args.max_files), max(1.0, args.timeout), args.slots_dir, args.slots)
    except Exception as error:  # the gate never blocks on its own failure
        result = dict(version=1, status='error', reason=str(error)[:500], command=args.command, selected=[],
                      runs=[], failing=[], preexisting=[], flaky=[])
    result['seconds'] = round(sum(r.get('seconds') or 0 for r in result.get('runs', [])), 3)
    try:
        Path(args.out).write_text(json.dumps(result, indent=1) + '\n', encoding='utf-8')
        if result['status'] == 'failed':
            Path(args.report).write_text(report(result), encoding='utf-8')
    except OSError:
        pass
    if args.log:
        try:
            with open(args.log, 'a', encoding='utf-8') as log:
                log.write('%d\t%s\t%.3f\n' % (time.time(), result['status'], result['seconds']))
        except OSError:
            pass
    sys.stdout.write(evidence(result))
    return 1 if result['status'] == 'failed' else 0


if __name__ == '__main__':
    sys.exit(main())
