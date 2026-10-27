"""An interrupted `run` stops without verifying, leaves no process holding the plan lock, and exits 130.

Uses the same fake agent CLI as test_forge.py, driven per call through SCENARIO. The runner is
always the pid `Popen` returned: that is the pid a user or a script would signal."""
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest

import test_forge as fixtures

PGREP = '/usr/bin/pgrep'
SCRIPTS = fixtures.ROOT / 'scripts'


class InterruptTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    plan = fixtures.ForgeTests.plan

    def start(self, plan, *args):
        """`run` as a user starts it: a process of its own, in a session of its own."""
        out = (self.root / 'run.out').open('w')
        self.addCleanup(out.close)
        run = subprocess.Popen(['bash', str(SCRIPTS / 'forge-parallel.sh'), 'run', str(plan), *map(str, args)],
                               cwd=self.repo, env=self.env, stdout=out, stderr=subprocess.STDOUT,
                               start_new_session=True)
        self.addCleanup(self.kill_group, run.pid)
        return run

    @staticmethod
    def kill_group(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def wait_for(self, condition, seconds=30, what='the condition'):
        deadline = time.time() + seconds
        while not condition():
            self.assertLess(time.time(), deadline, 'timed out waiting for ' + what)
            time.sleep(0.1)

    def pgrep(self, pattern):
        found = subprocess.run([PGREP, '-f', pattern], capture_output=True, text=True).stdout.split()
        return [int(pid) for pid in found if int(pid) != os.getpid()]

    def assertLockFree(self, plan):
        with open(plan / 'schedule.lock', 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.fail('the plan lock is still held')

    def finish(self, run, seconds=20):
        started = time.time()
        try:
            rc = run.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            self.fail('the runner is still alive %ds after the interrupt' % seconds)
        self.assertLess(time.time() - started, seconds)
        return rc

    def text(self, plan, name):
        return (plan / name).read_text().strip()

    def sleeping_dwarf(self):
        """A plan whose first dwarf call sleeps, with a verify command that must never run."""
        self.scenario({'a.dwarf.1': {'sleep': 60}})
        marker = self.root / 'verify-ran'
        return self.plan(), marker, f'touch {marker}'

    def assertInterruptedWithoutVerifying(self, run, p, marker):
        self.assertEqual(self.finish(run), 130, (self.root / 'run.out').read_text())
        self.assertFalse(marker.exists(), 'the verify command ran')
        self.assertEqual(self.text(p, 'tasks/a/status'), 'INTERRUPTED')
        self.assertIn('INTERRUPTED', (p / 'results.tsv').read_text())
        self.assertFalse((p / 'verification.status').exists())
        self.assertFalse((p / 'verification.runs.tsv').exists())
        self.assertLockFree(p)
        out = (self.root / 'run.out').read_text()
        self.assertIn(f'forge-parallel.sh retry {p} a', out)
        self.assertIn('results (integration branch', out)

    def test_sigterm_to_the_runner_while_a_dwarf_sleeps_skips_verification(self):
        p, marker, verify = self.sleeping_dwarf()
        run = self.start(p, '--verify', verify)
        self.wait_for(lambda: 'dwarf' in self.calls(), what='the dwarf to start')
        os.kill(run.pid, signal.SIGTERM)
        self.assertInterruptedWithoutVerifying(run, p, marker)

    def test_sigint_to_the_scheduler_skips_verification(self):
        p, marker, verify = self.sleeping_dwarf()
        run = self.start(p, '--verify', verify)
        self.wait_for(lambda: 'dwarf' in self.calls(), what='the dwarf to start')
        scheduler = self.pgrep(f'forge-schedule.py {SCRIPTS}/forge-parallel.sh {p} ')
        self.assertEqual(len(scheduler), 1, scheduler)
        os.kill(scheduler[0], signal.SIGINT)
        self.assertInterruptedWithoutVerifying(run, p, marker)

    def test_sigterm_to_the_runner_during_verification_stops_the_whole_command(self):
        self.scenario({})
        p = self.plan()
        started = self.root / 'verify-started'
        unique = 'interrupt-test-%d' % os.getpid()
        # The command's own children must go with it, whichever of them is the foreground one.
        run = self.start(p, '--verify', f'touch {started}; sleep 600.{os.getpid()} & sleep 601.{os.getpid()}; # {unique}')
        self.wait_for(started.exists, what='the verify command to start')
        self.wait_for(lambda: self.pgrep(r'sleep 601\.%d' % os.getpid()), what='the verify command to sleep')
        os.kill(run.pid, signal.SIGTERM)
        self.assertEqual(self.finish(run), 130, (self.root / 'run.out').read_text())
        self.assertEqual(self.text(p, 'verification.status'), 'INTERRUPTED')
        self.assertEqual(self.text(p, 'verification.exit'), '130')
        runs = (p / 'verification.runs.tsv').read_text() if (p / 'verification.runs.tsv').exists() else ''
        self.assertNotIn('rerun', runs)
        self.assertFalse((p / 'verification.retry.log').exists())
        self.assertEqual(self.pgrep(unique), [])
        self.assertEqual(self.pgrep(r'sleep 60[01]\.%d' % os.getpid()), [])
        self.assertLockFree(p)

    def test_a_verify_command_that_ignores_term_is_killed_after_the_grace(self):
        self.scenario({})
        p = self.plan()
        started = self.root / 'verify-started'
        unique = 'stubborn-%d' % os.getpid()
        run = self.start(p, '--verify', f"trap '' TERM; touch {started}; while :; do sleep 1; done # {unique}")
        self.wait_for(started.exists, what='the verify command to start')
        stopped = time.time()
        os.kill(run.pid, signal.SIGTERM)
        self.assertEqual(self.finish(run), 130, (self.root / 'run.out').read_text())
        self.assertGreaterEqual(time.time() - stopped, 4, 'killed before the grace was over')
        self.assertEqual(self.pgrep(unique), [])
        self.assertEqual(self.text(p, 'verification.status'), 'INTERRUPTED')
        self.assertLockFree(p)

    def test_a_failing_verify_is_still_rerun_when_nothing_interrupts(self):
        self.scenario({})
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'exit 9', '--verify-retries', 1)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'verification.status'), 'FAIL')
        rows = [line.split('\t')[1] for line in (p / 'verification.runs.tsv').read_text().splitlines()]
        self.assertEqual(rows, ['initial', 'rerun'])
        self.assertTrue((p / 'verification.retry.log').exists())


if __name__ == '__main__':
    unittest.main()
