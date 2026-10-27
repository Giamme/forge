#!/usr/bin/env python3
"""Run forge's offline test suites in parallel: every test class of tests/ and ui/tests/ in a
process of its own, several at a time, the slowest first.

    python3 tests/run_tests.py [-j N] [--slowest K] [PATTERN ...]

-j defaults to FORGE_TEST_JOBS, else the CPU count capped at 8; 1 runs the classes one after
another. A PATTERN keeps the classes whose id (`suite:module.Class`) contains it. One line is
printed per class as it ends; then the output of every class that failed, the K slowest tests
(default 15) and a summary. Exit 1 when a test failed or a class could not run.

Most of forge's tests are end to end: they start real forge-parallel.sh runs with fake model
CLIs, so a test spends its time waiting on processes, not on a CPU. Each test already works in
temporary directories of its own (dataset_isolation.py gives every process its own dataset and
config homes), which is what lets classes run side by side. The time each class took is kept
in tests/.durations.json (not tracked) so the next run starts the slowest ones first.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
# (name, directory the tests are discovered and run from, extra import path)
SUITES = (('forge', ROOT / 'tests', None), ('ui', ROOT / 'ui' / 'tests', ROOT / 'ui'))
DURATIONS = ROOT / 'tests' / '.durations.json'
CLASS_TIMEOUT = 30 * 60      # a class still running after this is stopped and counted as failed
SPLIT_OVER = 90              # a class that took longer last time runs test by test: it would bound the wall time

# Lists every test id of the suite run from the current directory, one per line. An import
# error shows up as a test of unittest.loader._FailedTest named after the module.
DISCOVER = '''
import sys, unittest
sys.path[0] = '.'
def walk(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from walk(item)
        else:
            yield item.id()
for test_id in walk(unittest.defaultTestLoader.discover('.', pattern='test_*.py')):
    print(test_id)
'''


def env_for(extra: Path | None) -> dict:
    env = dict(os.environ)
    if extra is not None:
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(extra), env.get('PYTHONPATH')]))
    return env


def discover(name: str, where: Path, extra: Path | None) -> dict[str, list[str]]:
    """{module.Class: [test ids]} of one suite, in discovery order. A module that failed to
    import is a class of its own with no test ids: running it shows the import error."""
    done = subprocess.run([sys.executable, '-c', DISCOVER], cwd=where, env=env_for(extra),
                          capture_output=True, text=True)
    if done.returncode != 0:
        sys.exit(f'run_tests: cannot discover the {name} tests:\n{done.stderr}')
    classes: dict[str, list[str]] = {}
    for test_id in done.stdout.split():
        if test_id.startswith('unittest.loader._FailedTest.'):
            classes.setdefault(test_id.rsplit('.', 1)[1], [])     # the module that failed to import
        else:
            classes.setdefault(test_id.rsplit('.', 1)[0], []).append(test_id)
    return classes


def units(suite: str, classes: dict[str, list[str]], known: dict) -> list[tuple[str, str]]:
    """What each process runs: a class, or each of its tests when the class ran over SPLIT_OVER
    seconds last time (a process per test costs an interpreter start, a fraction of a second)."""
    out = []
    for cls, tests in classes.items():
        split = len(tests) > 1 and known.get(f'{suite}:{cls}', 0) > SPLIT_OVER
        out.extend((suite, unit) for unit in (tests if split else [cls]))
    return out


def cost(suite: str, unit: str, known: dict) -> float:
    """The last time a unit took; a test of a split class gets an equal share of the class's."""
    key = f'{suite}:{unit}'
    if key in known:
        return known[key]
    parent = f"{suite}:{unit.rsplit('.', 1)[0]}"
    return known.get(parent, 60.0) / 10 if parent in known else 60.0


# The child: runs one module.Class (or a module) and writes its counts and per-test durations
# as JSON to the file named by argv[3].
def child(unit: str, report: str) -> int:
    import unittest
    sys.path[0] = os.getcwd()          # the suite's directory, not this script's

    class Timed(unittest.TextTestResult):
        def startTest(self, test):
            self._t0 = time.monotonic()
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            durations[test.id()] = time.monotonic() - self._t0

    durations: dict[str, float] = {}
    suite = unittest.defaultTestLoader.loadTestsFromName(unit)
    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=1, resultclass=Timed).run(suite)
    with open(report, 'w') as handle:
        json.dump({'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
                   'skipped': len(result.skipped), 'unexpected': len(result.unexpectedSuccesses),
                   'durations': durations}, handle)
    return 0 if result.wasSuccessful() else 1


class Runner:
    def __init__(self, jobs: int):
        self.jobs = jobs
        self.lock = threading.Lock()
        self.live: dict[int, subprocess.Popen] = {}
        self.stopping = False

    def run_one(self, suite: str, unit: str) -> dict:
        where, extra = next((w, e) for n, w, e in SUITES if n == suite)
        fd, report = tempfile.mkstemp(prefix='forge-test-report-')
        os.close(fd)
        started = time.monotonic()
        with tempfile.TemporaryFile() as out:
            proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--child', unit, report],
                                    cwd=where, env=env_for(extra), stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
            with self.lock:
                self.live[proc.pid] = proc
            timed_out = False
            try:
                proc.wait(timeout=CLASS_TIMEOUT)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill(proc)
            with self.lock:
                self.live.pop(proc.pid, None)
            out.seek(0)
            output = out.read().decode('utf-8', 'replace')
        try:
            with open(report) as handle:
                counts = json.load(handle)
        except (OSError, ValueError):
            counts = None
        finally:
            os.unlink(report)
        ok = proc.returncode == 0 and counts is not None and not timed_out
        if timed_out:
            output += f'\nrun_tests: stopped after {CLASS_TIMEOUT // 60} minutes\n'
        return {'suite': suite, 'unit': unit, 'ok': ok, 'seconds': time.monotonic() - started,
                'counts': counts, 'output': output}

    @staticmethod
    def kill(proc: subprocess.Popen) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue

    def stop_all(self) -> None:
        self.stopping = True
        with self.lock:
            live = list(self.live.values())
        for proc in live:
            self.kill(proc)


def load_durations() -> dict:
    try:
        data = json.loads(DURATIONS.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_durations(old: dict, results: list[dict]) -> None:
    """Each unit's time, and for a class run test by test the sum of its tests (whether to keep
    splitting it is decided from that)."""
    merged = dict(old)
    merged.update({f"{r['suite']}:{r['unit']}": round(r['seconds'], 2) for r in results})
    classes: dict[str, float] = {}
    for r in results:
        for test, seconds in (r['counts'] or {}).get('durations', {}).items():
            if test != r['unit']:
                continue                      # only a unit that is one test adds to its class
            key = f"{r['suite']}:{test.rsplit('.', 1)[0]}"
            classes[key] = classes.get(key, 0.0) + r['seconds']
    merged.update({k: round(v, 2) for k, v in classes.items()})
    try:
        tmp = DURATIONS.with_name(DURATIONS.name + '.tmp')
        tmp.write_text(json.dumps(merged, indent=1, sort_keys=True) + '\n')
        tmp.replace(DURATIONS)
    except OSError:
        pass


def clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f'{seconds // 60}m{seconds % 60:02d}s' if seconds >= 60 else f'{seconds}s'


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == '--child':
        return child(sys.argv[2], sys.argv[3])
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    default_jobs = os.environ.get('FORGE_TEST_JOBS') or str(min(os.cpu_count() or 1, 8))
    parser.add_argument('-j', '--jobs', type=int, default=int(default_jobs))
    parser.add_argument('--slowest', type=int, default=15)
    parser.add_argument('patterns', nargs='*')
    args = parser.parse_args()
    jobs = max(1, args.jobs)

    known = load_durations()
    classes = [u for name, where, extra in SUITES for u in units(name, discover(name, where, extra), known)]
    if args.patterns:
        classes = [c for c in classes if any(p in f'{c[0]}:{c[1]}' for p in args.patterns)]
    if not classes:
        sys.exit('run_tests: no test class matches')
    # the slowest first, so a long one does not start last; unknown ones count as slow
    classes.sort(key=lambda c: -cost(c[0], c[1], known))
    print(f'running {len(classes)} test classes and tests on {jobs} worker(s)', flush=True)

    runner = Runner(jobs)
    results: list[dict] = []
    started = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=jobs)
    try:
        futures = [pool.submit(runner.run_one, suite, unit) for suite, unit in classes]
        for future in concurrent.futures.as_completed(futures):
            r = future.result()
            results.append(r)
            n = r['counts']['tests'] if r['counts'] else '?'
            print(f"  {'ok  ' if r['ok'] else 'FAIL'} {clock(r['seconds']):>7}  {r['suite']}:{r['unit']} ({n} tests)",
                  flush=True)
    except KeyboardInterrupt:
        runner.stop_all()
        pool.shutdown(wait=False, cancel_futures=True)
        print('\nrun_tests: interrupted', file=sys.stderr)
        return 130
    pool.shutdown()
    wall = time.monotonic() - started
    save_durations(known, results)

    failed = [r for r in results if not r['ok']]
    for r in failed:
        print(f"\n===== {r['suite']}:{r['unit']} =====\n{r['output'].rstrip()}")
    durations = sorted(((t, f"{r['suite']}:{test}") for r in results if r['counts']
                        for test, t in r['counts']['durations'].items()), reverse=True)
    if args.slowest and durations:
        print(f'\nslowest {min(args.slowest, len(durations))} tests:')
        for seconds, test in durations[:args.slowest]:
            print(f'  {seconds:6.1f}s  {test}')
    total = {k: sum(r['counts'][k] for r in results if r['counts']) for k in ('tests', 'failures', 'errors', 'skipped')}
    busy = sum(r['seconds'] for r in results)
    print(f"\n{total['tests']} tests in {len(results)} classes: {clock(wall)} on {jobs} worker(s) "
          f"({clock(busy)} of test time); {total['skipped']} skipped")
    if failed:
        print(f"FAILED: {total['failures']} failures, {total['errors']} errors in {len(failed)} classes: "
              + ', '.join(f"{r['suite']}:{r['unit']}" for r in failed))
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
