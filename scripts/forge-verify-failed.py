#!/usr/bin/env python3
"""The test files a failed verification names, and the command that reruns only those (#112).

  forge-verify-failed.py command --checkout DIR --log LOG --subset FILE
      Prints the repository's `.forge/verify-subset` command with {files} (and {changed}) set to
      the tracked test files LOG names as failing, and exits 0. Exits 1, printing nothing, when the
      subset command has neither placeholder or LOG names no tracked test file.

A failed verification used to be rerun whole to tell a flaky test from a regression: one more
full suite for a failure that is usually deterministic. forge now reruns the failing files first.
If they fail again on their own, the regression is confirmed and the full rerun is skipped; if they
pass, the full command reruns as before, so a pass always comes from the whole command.

Recognised failure lines:
  unittest   `FAIL: test_x (module.Class.test_x)` and `ERROR: ...`, the module mapped to a tracked
             `<module>.py` (a dotted module to `<a>/<b>.py`); an import failure
             (`unittest.loader._FailedTest.<module>`) names its module last
  pytest     `FAILED path/test_x.py::...` and `ERROR path/test_x.py`
  node:test  the spec reporter's `test at path/x.test.js:L:C` and TAP's `location: '/abs/x.test.js:L:C'`
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys

UNITTEST = re.compile(r'^(?:FAIL|ERROR): \S+ \(([\w.]+)\)\s*$')
PYTEST = re.compile(r'^(?:FAILED|ERROR) (\S+?\.py)(?:::|\s|$)')
# node --test names a failing test's file only in its location: `test at <file>:L:C` under the spec
# reporter's failing-tests list, `location: '<file>:L:C'` in a TAP failure's diagnostics.
NODE = re.compile(r"^\s*(?:test at |location: ')(\S+\.(?:m?js|cjs|mts|ts)):\d+:\d+")
FAILED_IMPORT = 'unittest.loader._FailedTest.'


def tracked(checkout: str) -> list:
    try:
        out = subprocess.run(['git', '-C', checkout, 'ls-files', '-z'], capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    return [p for p in out.decode('utf-8', 'replace').split('\0') if p]


def module_files(dotted: str, files: list) -> list:
    """Tracked files for a unittest id: its longest module prefix that names one."""
    if dotted.startswith(FAILED_IMPORT):
        dotted = dotted[len(FAILED_IMPORT):]
    parts = dotted.split('.')
    for end in range(len(parts), 0, -1):
        tail = '/'.join(parts[:end]) + '.py'
        found = [f for f in files if f == tail or f.endswith('/' + tail)]
        if found:
            return found
    return []


def path_file(path: str, checkout: str, files: set) -> list:
    if os.path.isabs(path):      # runners print real paths; the checkout may be named through a symlink
        path = os.path.relpath(os.path.realpath(path), os.path.realpath(checkout))
    path = os.path.normpath(path)
    return [path] if path in files else []


def failing_files(log_text: str, checkout: str) -> list:
    files = tracked(checkout)
    known = set(files)
    found = []
    for line in log_text.splitlines():
        match = UNITTEST.match(line)
        if match:
            names = module_files(match.group(1), files)
        else:
            match = PYTEST.match(line) or NODE.match(line)
            names = path_file(match.group(1), checkout, known) if match else []
        for name in names:
            if name not in found:
                found.append(name)
    return found


def subset_command(template: str, files: list) -> str:
    quoted = ' '.join(shlex.quote(f) for f in files)
    return template.replace('{files}', quoted).replace('{changed}', quoted)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='forge-verify-failed.py')
    sub = ap.add_subparsers(dest='command', required=True)
    p = sub.add_parser('command')
    p.add_argument('--checkout', required=True)
    p.add_argument('--log', required=True)
    p.add_argument('--subset', required=True)
    args = ap.parse_args(argv)
    try:
        template = open(args.subset, encoding='utf-8', errors='replace').read().strip()
        log_text = open(args.log, encoding='utf-8', errors='replace').read()
    except OSError:
        return 1
    if '{files}' not in template and '{changed}' not in template:
        return 1
    files = failing_files(log_text, args.checkout)
    if not files:
        return 1
    print(subset_command(template, files))
    return 0


if __name__ == '__main__':
    sys.exit(main())
