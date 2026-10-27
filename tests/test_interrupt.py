"""An interrupted `run` stops without verifying, leaves no process holding the plan lock, and exits 130.

Uses the same fake agent CLI as test_forge.py, driven per call through SCENARIO. The runner is
always the pid `Popen` returned: that is the pid a user or a script would signal."""
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
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

    def wait_for(self, condition, seconds=120, what='the condition'):   # a ceiling: a busy machine is slow to get there
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

    def slow_python(self, when, seconds=3):
        """Hold one python3 step of the run for `seconds`, so a signal lands inside it on purpose.

        `when` is a Python expression over `argv` (that process's sys.argv), evaluated in a
        sitecustomize on PYTHONPATH. The first process it matches creates <root>/slow and sleeps;
        later matches do not. Returns that marker's path."""
        site = self.root / 'site'
        site.mkdir(exist_ok=True)
        marker = self.root / 'slow'
        (site / 'sitecustomize.py').write_text(
            'import os, sys, time\n'
            'argv = sys.argv\n'
            f'if {when}:\n'
            '    try:\n'
            f'        os.close(os.open({str(marker)!r}, os.O_CREAT | os.O_EXCL | os.O_WRONLY))\n'
            f'        time.sleep({seconds})\n'
            '    except FileExistsError:\n'
            '        pass\n')
        self.env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(site), self.env.get('PYTHONPATH')]))
        return marker

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
        self.assertIn('interrupted; verification was skipped.', out)
        self.assertNotIn("retry a failed task with its reviewer's findings", out)   # its own hint only

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
        out = (self.root / 'run.out').read_text()
        self.assertIn('interrupted during verification, which was stopped and not rerun.', out)
        self.assertNotIn('continue each one with', out)            # every task merged: none to continue

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

    def test_a_stop_before_the_guard_made_its_session_runs_nothing(self):
        # The guard is held before its setsid: there is no process group to signal yet.
        unique = 'early-stop-%d' % os.getpid()
        held = self.slow_python(f"argv[0].endswith('forge-guard.py') and any({unique!r} in a for a in argv)")
        self.scenario({})
        p = self.plan()
        ran = self.root / 'verify-ran'
        run = self.start(p, '--verify', f'touch {ran}; sleep 30 # {unique}')
        self.wait_for(held.exists, what='the guard to start')
        os.kill(run.pid, signal.SIGTERM)
        self.assertEqual(self.finish(run), 130, (self.root / 'run.out').read_text())
        time.sleep(3.5)                                    # past the hold: a guard left alive would start it now
        self.assertFalse(ran.exists(), 'the verify command ran after the stop')
        self.assertEqual(self.pgrep(unique), [])
        self.assertEqual(self.text(p, 'verification.status'), 'INTERRUPTED')
        self.assertLockFree(p)

    def test_a_signal_while_verification_is_being_entered_still_skips_it(self):
        # run records its verification phase (a python3 call) just before it verifies; bash runs
        # the handler only once that call returns.
        held = self.slow_python("argv[0].endswith('forge-runtime.py') and argv[1:2] == ['phase'] and argv[-1] == 'verification'"
                                " and '/attempts/parallel-' in argv[2]")     # the run's own record, not a task's
        self.scenario({})
        p = self.plan()
        ran = self.root / 'verify-ran'
        run = self.start(p, '--verify', f'touch {ran}')
        self.wait_for(held.exists, what='the verification phase to be recorded')
        os.kill(run.pid, signal.SIGTERM)
        self.assertEqual(self.finish(run), 130, (self.root / 'run.out').read_text())
        self.assertFalse(ran.exists(), 'the verify command ran')
        self.assertFalse((p / 'verification.status').exists())
        self.assertFalse((p / 'verification.runs.tsv').exists())
        self.assertIn('interrupted; verification was skipped.', (self.root / 'run.out').read_text())
        self.assertLockFree(p)

    def test_a_stop_before_the_command_records_no_run_of_it(self):
        # Held while the verification records its own start, before the command is launched.
        held = self.slow_python("argv[0].endswith('forge-runtime.py') and argv[1:2] == ['begin'] and argv[-1] == 'verification'")
        self.scenario({})
        p = self.plan()
        ran = self.root / 'verify-ran'
        run = self.start(p, '--verify', f'touch {ran}')
        self.wait_for(held.exists, what='the verification to start')
        os.kill(run.pid, signal.SIGTERM)
        self.assertEqual(self.finish(run), 130, (self.root / 'run.out').read_text())
        self.assertFalse(ran.exists(), 'the verify command ran')
        self.assertEqual(self.text(p, 'verification.status'), 'INTERRUPTED')
        self.assertFalse((p / 'verification.runs.tsv').exists(), 'a run of a command that never started')
        self.assertLockFree(p)

    def interrupted_over_a_stale_status(self, stale):
        """The run's output after a SIGTERM during the dwarf, over a verification.status an earlier run left."""
        p, marker, verify = self.sleeping_dwarf()
        (p / 'verification.status').write_text(stale + '\n')
        run = self.start(p, '--verify', verify)
        self.wait_for(lambda: 'dwarf' in self.calls(), what='the dwarf to start')
        os.kill(run.pid, signal.SIGTERM)
        self.assertEqual(self.finish(run), 130)
        out = (self.root / 'run.out').read_text()
        self.assertIn('interrupted; verification was skipped.', out)
        return out

    def test_a_stale_failed_verification_is_not_reported_as_this_runs(self):
        self.assertNotIn('combined verification failed', self.interrupted_over_a_stale_status('FAIL'))

    def test_a_stale_interrupted_verification_is_not_reported_as_this_runs(self):
        self.assertNotIn('interrupted during verification', self.interrupted_over_a_stale_status('INTERRUPTED'))

    def test_the_exit_code_is_kept_when_the_tree_cannot_be_read_after_the_command(self):
        self.scenario({})
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'mv .git .git.gone; exit 3')
        self.assertEqual(self.text(p, 'verification.exit'), '3', r.stdout)

    def test_a_graceful_control_stop_ends_the_run_without_verifying(self):
        # a's dwarf is busy when the stop comes: it finishes and merges; b never starts.
        self.scenario({'a.dwarf.1': {'sleep': 3}})
        p = self.plan()
        ran = self.root / 'verify-ran'
        run = self.start(p, '--verify', f'touch {ran}')
        self.wait_for(lambda: 'dwarf' in self.calls(), what='the dwarf to start')
        stop = subprocess.run([sys.executable, str(SCRIPTS / 'forge-control.py'), 'stop', str(p)],
                              capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertEqual(self.finish(run, 60), 130, (self.root / 'run.out').read_text())
        self.assertFalse(ran.exists(), 'the verify command ran')
        results = (p / 'results.tsv').read_text()
        self.assertIn('a\tMERGED', results)
        self.assertIn('b\tPENDING', results)
        out = (self.root / 'run.out').read_text()
        self.assertIn('stopped by forge control stop; 1 task(s) not started', out)
        self.assertIn('verification was skipped', out)
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
