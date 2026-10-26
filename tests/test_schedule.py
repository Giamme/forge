"""Scheduler behaviour that needs no model and no git: in-run retries, infrastructure
pauses, resume and interrupts. The worker is a small bash script standing in for
forge-parallel.sh's `_task`, `_merge` and `_prepare_retry` entry points; each task's
outcomes are queued in a file and consumed one per `_task` call, so every scenario is
deterministic.

Ordering is asserted from an event log, never from wall-clock timing."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

WORKER = r'''#!/bin/bash
cmd="$1"; plan="$2"; name="$3"; t="$plan/tasks/$name"
mkdir -p "$t"
case "$cmd" in
  _task)
    n=$(( $(cat "$t/calls" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$t/calls"
    echo "$name start $n" >> "$plan/events.log"
    # a's second run opens the gate that keeps task c alive until the retry has begun.
    if [ "$name" = a ] && [ "$n" = 2 ]; then touch "$plan/gate"; fi
    if [ -f "$plan/hold/$name" ]; then
      for _ in $(seq 1 500); do [ -f "$plan/gate" ] && break; sleep 0.02; done
    fi
    outcome="$(head -1 "$plan/outcomes/$name")"
    tail -n +2 "$plan/outcomes/$name" > "$plan/outcomes/$name.rest"; mv "$plan/outcomes/$name.rest" "$plan/outcomes/$name"
    echo "output of $name attempt $n"
    case "$outcome" in
      PASS|FAIL|UNKNOWN) echo "$outcome" > "$t/status" ;;
      INFRA) echo INFRA > "$t/status"; printf 'class=quota\nstage=dwarf\n' > "$t/infra.txt" ;;
      NOCHANGE) echo FAIL > "$t/status"; : > "$t/noretry" ;;
      TIMEOUT) echo TIMEOUT > "$t/status" ;;
    esac
    echo "$name end $outcome" >> "$plan/events.log"
    ;;
  _merge)
    if [ "$(cat "$t/status" 2>/dev/null)" = PASS ]; then touch "$t/merged"; echo "$name merged" >> "$plan/events.log"; fi
    ;;
  _prepare_retry)
    echo "$name prepared" >> "$plan/events.log"
    rm -f "$t/status" "$t/noretry"
    ;;
esac
'''


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.plan = self.root / 'plan'; self.plan.mkdir()
        (self.plan / 'outcomes').mkdir(); (self.plan / 'hold').mkdir()
        (self.plan / 'wt_root').write_text(str(self.root))
        self.worker = self.root / 'worker.sh'; self.worker.write_text(WORKER)
        self.env = dict(os.environ, FORGE_INFRA_BACKOFF='0.05')
        self.rows = []

    def task(self, name, outcomes, deps='-', files=None, hold=False, hold_merge=False):
        self.rows.append(f'{name}\t{deps}\tlow\t{files or "f_" + name}\tsol\topus\t{name.upper()}')
        (self.plan / 'outcomes' / name).write_text(''.join(o + '\n' for o in outcomes))
        root = self.plan / 'tasks' / name; root.mkdir(parents=True)
        (root / 'base_ref').write_text('base\n')   # the scheduler would otherwise ask git
        if hold: (self.plan / 'hold' / name).write_text('')
        if hold_merge:
            directory = self.plan / 'hold-merge'; directory.mkdir(exist_ok=True)
            (directory / name).write_text('')

    def run_scheduler(self, *extra, capacity=3, env=None):
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')
        command = [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker),
                   str(self.plan), str(capacity), *map(str, extra)]
        return subprocess.run(command, env=env or self.env, text=True, timeout=60,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def status(self, name):
        root = self.plan / 'tasks' / name
        if (root / 'merged').exists(): return 'MERGED'
        f = root / 'status'
        return f.read_text().strip() if f.exists() else 'PENDING'

    def events(self):
        f = self.plan / 'events.log'
        return f.read_text().splitlines() if f.exists() else []

    def calls(self, name):
        f = self.plan / 'tasks' / name / 'calls'
        return int(f.read_text()) if f.exists() else 0

    def raw_status(self, name):
        f = self.plan / 'tasks' / name / 'status'
        return f.read_text().strip() if f.exists() else 'PENDING'

    # --- retries ---------------------------------------------------------------
    def test_without_the_flag_a_failure_is_left_for_a_human(self):
        self.task('a', ['FAIL', 'PASS'])
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual((self.status('a'), self.calls('a')), ('FAIL', 1))
        self.assertNotIn('a prepared', self.events())

    def test_retry_failed_requeues_inside_the_run_and_merges(self):
        self.task('a', ['FAIL', 'PASS'])
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.status('a'), self.calls('a')), ('MERGED', 2))
        self.assertEqual(self.events().count('a prepared'), 1)
        self.assertIn('automatic retry 1/1', r.stderr)

    def test_retry_budget_is_per_task_and_bounded(self):
        self.task('a', ['FAIL', 'FAIL', 'PASS'])
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual((self.status('a'), self.calls('a')), ('FAIL', 2))

    def test_a_bigger_retry_budget_gets_there(self):
        self.task('a', ['FAIL', 'FAIL', 'PASS'])
        r = self.run_scheduler('--retry-failed', 2)
        self.assertEqual((r.returncode, self.status('a'), self.calls('a')), (0, 'MERGED', 3), r.stderr)

    def test_unknown_verdicts_are_retried_too(self):
        self.task('a', ['UNKNOWN', 'PASS'])
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual((r.returncode, self.status('a')), (0, 'MERGED'), r.stderr)

    def test_a_no_progress_marker_stops_the_retry(self):
        self.task('a', ['NOCHANGE', 'PASS'])
        r = self.run_scheduler('--retry-failed', 3)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual((self.status('a'), self.calls('a')), ('FAIL', 1))

    def test_timeouts_and_errors_are_never_retried_automatically(self):
        self.task('a', ['TIMEOUT', 'PASS'])
        r = self.run_scheduler('--retry-failed', 3)
        self.assertEqual((r.returncode, self.status('a'), self.calls('a')), (5, 'TIMEOUT', 1))

    def test_dependents_wait_for_the_retried_task_to_merge(self):
        self.task('a', ['FAIL', 'PASS'])
        self.task('b', ['PASS'], deps='a')
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        events = self.events()
        self.assertLess(events.index('a merged'), events.index('b start 1'))
        self.assertEqual(self.status('b'), 'MERGED')

    def test_other_tasks_keep_running_while_one_is_retried(self):
        # c is held until a's SECOND run begins, so c is provably still running during a's
        # retry: no pass barrier makes it wait for a, and a's retry does not wait for c.
        self.task('a', ['FAIL', 'PASS'])
        self.task('c', ['PASS'], hold=True)
        r = self.run_scheduler('--retry-failed', 1, capacity=2)
        self.assertEqual(r.returncode, 0, r.stderr)
        events = self.events()
        self.assertLess(events.index('a start 2'), events.index('c end PASS'))
        self.assertLess(events.index('a end FAIL'), events.index('a start 2'))

    def test_a_saved_failure_is_retried_on_resume_only_when_asked(self):
        self.task('a', ['PASS'])
        (self.plan / 'tasks/a/status').write_text('FAIL\n')
        r = self.run_scheduler()
        self.assertEqual((r.returncode, self.calls('a')), (5, 0))
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual((r.returncode, self.status('a'), self.calls('a')), (0, 'MERGED', 1), r.stderr)

    def test_full_output_is_not_duplicated_across_retries(self):
        self.task('a', ['FAIL', 'PASS'])
        r = self.run_scheduler('--retry-failed', 1, env=dict(self.env, FORGE_OUTPUT='full'))
        self.assertEqual(r.stdout.count('output of a attempt 1'), 1, r.stdout)
        self.assertEqual(r.stdout.count('output of a attempt 2'), 1, r.stdout)

    # --- infrastructure stops ---------------------------------------------------
    def test_an_infrastructure_stop_pauses_without_failing_or_blocking_anything(self):
        self.task('a', ['INFRA', 'PASS'])
        self.task('b', ['PASS'], deps='a')
        self.task('c', ['PASS'])
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual(self.status('a'), 'INFRA')
        self.assertEqual(self.status('c'), 'MERGED')          # unrelated work still finished
        self.assertEqual(self.status('b'), 'PENDING')         # not BLOCKED: nobody failed
        self.assertFalse((self.plan / 'tasks/b/status').exists())

    def test_nothing_new_is_dispatched_after_an_infrastructure_stop(self):
        self.task('a', ['INFRA', 'PASS'])
        self.task('b', ['PASS'], deps='a')
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 8)
        self.assertEqual(self.calls('b'), 0)

    def test_the_next_run_resumes_an_infrastructure_stop_without_a_flag(self):
        self.task('a', ['INFRA', 'PASS'])
        self.assertEqual(self.run_scheduler().returncode, 8)
        r = self.run_scheduler()
        self.assertEqual((r.returncode, self.status('a'), self.calls('a')), (0, 'MERGED', 2), r.stderr)
        self.assertEqual(self.events().count('a prepared'), 0)   # not a failure: no retry prep

    def test_infra_retries_wait_and_resume_inside_the_run(self):
        self.task('a', ['INFRA', 'PASS'])
        self.task('b', ['PASS'], deps='a')
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.status('a'), self.status('b')), ('MERGED', 'MERGED'))
        self.assertIn('waiting', r.stderr)

    def test_infra_retries_are_bounded(self):
        self.task('a', ['INFRA', 'INFRA', 'PASS'])
        r = self.run_scheduler('--infra-retries', 1)
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual((self.status('a'), self.calls('a')), ('INFRA', 2))

    def test_a_retry_after_hint_sets_the_wait(self):
        self.task('a', ['INFRA', 'PASS'])
        # The worker writes the stop record; this edit makes it advertise a short hint.
        text = WORKER.replace("printf 'class=quota\\nstage=dwarf\\n'", "printf 'class=quota\\nretry_after=0\\nstage=dwarf\\n'")
        self.worker.write_text(text)
        r = self.run_scheduler('--infra-retries', 1, env=dict(self.env, FORGE_INFRA_BACKOFF='999'))
        self.assertEqual((r.returncode, self.status('a')), (0, 'MERGED'), r.stderr)

    def test_infrastructure_stops_do_not_consume_the_failure_retry_budget(self):
        self.task('a', ['INFRA', 'FAIL', 'PASS'])
        r = self.run_scheduler('--retry-failed', 1, '--infra-retries', 1)
        self.assertEqual((r.returncode, self.status('a'), self.calls('a')), (0, 'MERGED', 3), r.stderr)

    def test_failures_are_not_reported_as_infrastructure(self):
        self.task('a', ['FAIL'])
        self.task('b', ['PASS'])
        self.assertEqual(self.run_scheduler().returncode, 5)

    def test_invalid_counts_are_rejected(self):
        self.task('a', ['PASS'])
        self.assertNotEqual(self.run_scheduler('--retry-failed', -1).returncode, 0)

    # --- interrupts -------------------------------------------------------------
    # The worker below blocks inside `_merge` until the test releases it. Tasks listed
    # before the gate are dispatched first, so the reap walks past them while they are
    # still held, then sticks in the gate's merge. Releasing them writes a terminal
    # status before the signal; the scheduler cannot have reaped that status yet.
    def interrupt_worker(self):
        self.worker.write_text(r'''#!/bin/bash
cmd="$1"; plan="$2"; name="$3"; t="$plan/tasks/$name"
waitfor() { for _ in $(seq 1 1500); do [ -e "$1" ] && return 0; sleep 0.02; done; return 1; }
case "$cmd" in
  _task)
    n=$(( $(cat "$t/calls" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$t/calls"
    echo "$name start $n" >> "$plan/events.log"
    if [ -f "$plan/hold/$name" ]; then waitfor "$plan/release/$name" || exit 1; fi
    outcome="$(head -1 "$plan/outcomes/$name")"
    tail -n +2 "$plan/outcomes/$name" > "$plan/outcomes/$name.rest"; mv "$plan/outcomes/$name.rest" "$plan/outcomes/$name"
    case "$outcome" in
      PASS|FAIL|UNKNOWN) echo "$outcome" > "$t/status" ;;
      TIMEOUT) echo TIMEOUT > "$t/status" ;;
      INFRA) echo INFRA > "$t/status"; printf 'class=quota\nstage=dwarf\n' > "$t/infra.txt" ;;
    esac
    echo "$name end $outcome" >> "$plan/events.log"
    ;;
  _merge)
    if [ "$(cat "$t/status" 2>/dev/null)" = PASS ]; then
      mkdir -p "$plan/merging"
      echo "$name merging" >> "$plan/events.log"
      touch "$plan/merging/$name"
      if [ -f "$plan/hold-merge/$name" ]; then waitfor "$plan/release-merge/$name" || exit 1; fi
      touch "$t/merged"
      echo "$name merged" >> "$plan/events.log"
    fi
    ;;
  _prepare_retry)
    echo "$name prepared" >> "$plan/events.log"
    rm -f "$t/status" "$t/noretry"
    ;;
esac
''')

    def start(self, *extra, capacity=3):
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')
        command = [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker),
                   str(self.plan), str(capacity), *map(str, extra)]
        proc = subprocess.Popen(command, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def finish(self, proc, timeout=45):
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)

    def wait_for(self, condition, what, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.02)
        self.fail('timed out waiting for ' + what + '\nevents: ' + repr(self.events()))

    def release(self, name):
        directory = self.plan / 'release'; directory.mkdir(exist_ok=True)
        (directory / name).write_text('')

    def release_merge(self, name):
        directory = self.plan / 'release-merge'; directory.mkdir(exist_ok=True)
        (directory / name).write_text('')

    def wait_until_gate_holds_the_reap(self, proc, *started):
        def gate_is_blocking():
            if proc.poll() is not None:
                self.fail('scheduler exited before the gate merge blocked\nevents: ' + repr(self.events()))
            return (self.plan / 'merging/gate').exists() and all(
                f'{name} start 1' in self.events() for name in started)
        self.wait_for(gate_is_blocking, 'the gate merge to block after earlier tasks started')

    def signal_and_release_gate(self, proc):
        self.assertIsNone(proc.poll(), 'scheduler exited before the signal')
        proc.send_signal(signal.SIGTERM)
        self.release_merge('gate')
        return self.finish(proc)

    def test_an_interrupt_merges_tasks_that_finished_before_the_next_reap(self):
        # a and b write PASS and exit while the scheduler is stuck merging gate, so both
        # have a recorded result and neither has been reaped when the signal arrives.
        self.interrupt_worker()
        self.task('a', ['PASS'], hold=True)
        self.task('b', ['PASS'], hold=True)
        self.task('gate', ['PASS'], hold_merge=True)
        proc = self.start()
        self.wait_until_gate_holds_the_reap(proc, 'a', 'b')
        self.release('a'); self.release('b')
        self.wait_for(lambda: self.raw_status('a') == 'PASS' and self.raw_status('b') == 'PASS'
                      and 'a end PASS' in self.events() and 'b end PASS' in self.events(),
                      'a and b to record PASS')
        self.assertNotIn('a merging', self.events())
        self.assertNotIn('b merging', self.events())
        result = self.signal_and_release_gate(proc)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual((self.status('a'), self.status('b'), self.status('gate')),
                         ('MERGED', 'MERGED', 'MERGED'))
        self.assertEqual((self.raw_status('a'), self.raw_status('b'), self.raw_status('gate')),
                         ('PASS', 'PASS', 'PASS'))
        self.assertNotIn('INTERRUPTED', [self.raw_status(name) for name in ('a', 'b', 'gate')])
        self.assertEqual(self.events().count('a merged'), 1)
        self.assertEqual(self.events().count('b merged'), 1)
        self.assertNotIn('a prepared', self.events())
        self.assertNotIn('b prepared', self.events())

    def test_an_interrupt_keeps_a_finished_failure_and_stops_only_the_live_task(self):
        self.interrupt_worker()
        self.task('live', ['PASS'], hold=True)
        self.task('done', ['FAIL', 'PASS'], hold=True)
        self.task('gate', ['PASS'], hold_merge=True)
        proc = self.start('--retry-failed', 1)
        self.wait_until_gate_holds_the_reap(proc, 'live', 'done')
        self.release('done')
        self.wait_for(lambda: self.raw_status('done') == 'FAIL' and 'done end FAIL' in self.events(),
                      'done to record FAIL')
        self.assertNotIn('done prepared', self.events())
        self.assertNotIn('done end PASS', self.events())
        result = self.signal_and_release_gate(proc)
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertEqual(self.raw_status('done'), 'FAIL')
        self.assertEqual(self.status('done'), 'FAIL')
        self.assertEqual(self.calls('done'), 1)
        self.assertEqual((self.plan / 'outcomes' / 'done').read_text(), 'PASS\n')
        self.assertNotIn('done prepared', self.events())
        self.assertNotIn('done start 2', self.events())
        self.assertEqual(self.raw_status('live'), 'INTERRUPTED')
        self.assertNotIn('live end PASS', self.events())
        self.assertEqual(self.status('gate'), 'MERGED')
        self.assertEqual(self.raw_status('gate'), 'PASS')

    def test_a_task_that_finishes_during_a_merge_is_reaped_on_interrupt(self):
        self.interrupt_worker()
        self.task('late', ['PASS'], hold=True)
        self.task('gate', ['PASS'], hold_merge=True)
        proc = self.start()
        self.wait_until_gate_holds_the_reap(proc, 'late')
        self.release('late')
        self.wait_for(lambda: 'late end PASS' in self.events() and self.raw_status('late') == 'PASS',
                      'late to finish during the gate merge')
        self.assertNotIn('gate merged', self.events())
        self.assertNotIn('late merging', self.events())
        result = self.signal_and_release_gate(proc)
        self.assertEqual(result.returncode, 130, result.stderr)
        events = self.events()
        self.assertLess(events.index('late end PASS'), events.index('gate merged'))
        self.assertIn('late merged', events)
        self.assertEqual((self.status('late'), self.status('gate')), ('MERGED', 'MERGED'))
        self.assertEqual((self.raw_status('late'), self.raw_status('gate')), ('PASS', 'PASS'))
        self.assertNotIn('late prepared', events)

    def test_an_error_during_dispatch_leaves_a_finished_failure_unretried(self):
        # b has no base_ref, so dispatch blocks in `git rev-parse` after a is already
        # running. a exits FAIL during that lookup, still unreaped. The exception
        # then runs `finally`. Requeueing there would delete the FAIL and spend a
        # retry this run can no longer dispatch.
        self.task('a', ['FAIL', 'PASS'])
        self.task('b', ['PASS'])
        (self.plan / 'tasks' / 'b' / 'base_ref').unlink()
        bindir = self.root / 'bin'
        bindir.mkdir()
        git = bindir / 'git'
        git.write_text(r'''#!/bin/bash
echo git-entered >> "$FORGE_TEST_PLAN/events.log"
parent=$PPID
for _ in $(seq 1 1500); do
  [ -e "$FORGE_TEST_PLAN/git-release" ] && exit 1
  kill -0 "$parent" 2>/dev/null || exit 1
  sleep 0.02
done
exit 1
''')
        git.chmod(0o755)
        self.env = dict(self.env, PATH=str(bindir) + os.pathsep + self.env['PATH'],
                        FORGE_TEST_PLAN=str(self.plan))
        proc = self.start('--retry-failed', 1, capacity=2)
        self.wait_for(lambda: 'a end FAIL' in self.events() and 'git-entered' in self.events(),
                      'a to finish while the next task looks up its baseline')
        self.assertEqual(self.raw_status('a'), 'FAIL')
        self.assertNotIn('a prepared', self.events())
        (self.plan / 'git-release').write_text('')
        result = self.finish(proc)
        # The lookup must actually fail, and finally must run: live_stop drops
        # scheduler.live before the reaping that used to spend this retry.
        self.assertNotIn(result.returncode, (0, 130), result.stderr)
        self.assertIn('rev-parse', result.stderr)
        self.assertFalse((self.plan / 'scheduler.live').exists())
        self.assertEqual(self.raw_status('a'), 'FAIL', result.stderr)
        self.assertEqual(self.status('a'), 'FAIL', result.stderr)
        self.assertEqual(self.calls('a'), 1)
        self.assertNotIn('a prepared', self.events())
        self.assertNotIn('a start 2', self.events())
        self.assertFalse((self.plan / 'tasks' / 'a' / 'retries.used').exists())
        self.assertEqual((self.plan / 'outcomes' / 'a').read_text(), 'PASS\n')
        self.assertEqual(self.calls('b'), 0)
        self.assertEqual(self.status('b'), 'PENDING')


if __name__ == '__main__':
    unittest.main()
