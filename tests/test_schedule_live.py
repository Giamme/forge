"""The scheduler's live control files: retry and infra budgets a human can change while a
run is in progress (through `forge control`, which this file stands in for by writing the
same files), and the scheduler.live file that says they are being read.

The worker is a small bash script standing in for forge-parallel.sh's `_task`, `_merge` and
`_prepare_retry`, like in test_schedule.py. A test scripts a task's run by dropping a hook
at hooks/<task>.<call number>: it is sourced inside the worker on that call, so it can write
control files, wait for other tasks, or record what it saw. Ordering is asserted from
events.log and from files, not from wall-clock timing; the few sleeps below only make the
path under test the likely one and never decide a pass."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
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
waitfor() { for _ in $(seq 1 500); do [ -e "$1" ] && return 0; sleep 0.02; done; return 1; }
put() { printf '%s' "$2" > "$1.tmp" && mv "$1.tmp" "$1"; }   # atomic, like forge control
case "$cmd" in
  _task)
    n=$(( $(cat "$t/calls" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$t/calls"
    echo "$name start $n" >> "$plan/events.log"
    for f in scheduler.live retry_budget infra_budget; do cp "$plan/$f" "$t/seen.$f.$n" 2>/dev/null; done
    [ -f "$plan/hooks/$name.$n" ] && . "$plan/hooks/$name.$n"
    outcome="$(head -1 "$plan/outcomes/$name")"
    tail -n +2 "$plan/outcomes/$name" > "$plan/outcomes/$name.rest"; mv "$plan/outcomes/$name.rest" "$plan/outcomes/$name"
    case "$outcome" in
      PASS|FAIL|UNKNOWN) echo "$outcome" > "$t/status" ;;
      INFRA) echo INFRA > "$t/status"; printf 'class=quota\nstage=dwarf\n' > "$t/infra.txt" ;;
      INFRA_LONG) echo INFRA > "$t/status"; printf 'class=quota\nretry_after=600\nstage=dwarf\n' > "$t/infra.txt" ;;
    esac
    echo "$name end $outcome" >> "$plan/events.log"
    ;;
  _merge)
    if [ "$(cat "$t/status" 2>/dev/null)" = PASS ]; then touch "$t/merged"; echo "$name merged" >> "$plan/events.log"; fi
    ;;
  _prepare_retry)
    echo "$name prepared" >> "$plan/events.log"
    [ -e "$plan/prepare_fails/$name" ] && exit 1
    rm -f "$t/status" "$t/noretry"
    ;;
esac
'''

# What a hook does once a task has ended FAIL and the scheduler had time to look at it:
# the whole point of the re-check is a task nothing else will look at again.
AFTER_X_ENDED = '''waitfor "$plan/tasks/x/status"; sleep 0.3
'''


class Result:
    def __init__(self, proc, out, err):
        self.pid, self.returncode, self.stdout, self.stderr = proc.pid, proc.returncode, out, err


class LiveBudgetTests(unittest.TestCase):
    def setUp(self):
        self.reset()

    def reset(self):
        """A fresh plan dir, so one test can try several scenarios."""
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name); self.plan = self.root / 'plan'; self.plan.mkdir()
        for sub in ('outcomes', 'hooks', 'prepare_fails'): (self.plan / sub).mkdir()
        (self.plan / 'wt_root').write_text(str(self.root))
        self.worker = self.root / 'worker.sh'; self.worker.write_text(WORKER)
        self.env = dict(os.environ, FORGE_INFRA_BACKOFF='0.05')
        self.rows = []

    def task(self, name, outcomes, deps='-'):
        self.rows.append(f'{name}\t{deps}\tlow\tf_{name}\tsol\topus\t{name.upper()}')
        (self.plan / 'outcomes' / name).write_text(''.join(o + '\n' for o in outcomes))
        root = self.plan / 'tasks' / name; root.mkdir(parents=True)
        (root / 'base_ref').write_text('base\n')   # the scheduler would otherwise ask git

    def hook(self, name, call, body):
        (self.plan / 'hooks' / f'{name}.{call}').write_text(body)

    def start(self, *extra, capacity=3):
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')
        command = [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker),
                   str(self.plan), str(capacity), *map(str, extra)]
        proc = subprocess.Popen(command, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def finish(self, proc, timeout=60):
        out, err = proc.communicate(timeout=timeout)
        return Result(proc, out, err)

    def run_scheduler(self, *extra, capacity=3):
        return self.finish(self.start(*extra, capacity=capacity))

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

    def read(self, *parts):
        f = self.plan.joinpath(*parts)
        return f.read_text() if f.exists() else None

    def wait_for(self, condition, what, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition(): return
            time.sleep(0.02)
        self.fail('timed out waiting for ' + what)

    def live_files(self):
        return [n for n in ('scheduler.live', 'retry_budget', 'infra_budget') if (self.plan / n).exists()]

    # --- lifecycle ------------------------------------------------------------------
    def test_the_scheduler_publishes_that_it_is_live_and_withdraws_it(self):
        self.task('a', ['PASS'])
        r = self.run_scheduler('--retry-failed', 2, '--infra-retries', 3)
        self.assertEqual(r.returncode, 0, r.stderr)
        live = json.loads(self.read('tasks/a/seen.scheduler.live.1'))
        self.assertEqual(live['pid'], r.pid)
        self.assertLess(abs(time.time() - live['started']), 60)
        self.assertEqual(self.read('tasks/a/seen.retry_budget.1').strip(), '2')
        self.assertEqual(self.read('tasks/a/seen.infra_budget.1').strip(), '3')
        self.assertEqual(self.live_files(), [])

    def test_without_flags_the_budgets_are_published_as_zero(self):
        self.task('a', ['PASS'])
        self.assertEqual(self.run_scheduler().returncode, 0)
        self.assertEqual((self.read('tasks/a/seen.retry_budget.1').strip(),
                          self.read('tasks/a/seen.infra_budget.1').strip()), ('0', '0'))

    def test_an_interrupted_run_withdraws_everything_too(self):
        self.task('a', ['PASS'])
        self.hook('a', 1, 'put "$plan/tasks/a/retry_budget" 4\nwaitfor "$plan/never"\n')
        proc = self.start('--retry-failed', 1)
        self.wait_for(lambda: 'a start 1' in self.events() and (self.plan / 'tasks/a/retry_budget').exists(),
                      'a to be running with a per-task budget')
        self.assertEqual(sorted(self.live_files()), ['infra_budget', 'retry_budget', 'scheduler.live'])
        proc.send_signal(signal.SIGTERM)
        r = self.finish(proc)
        self.assertEqual(r.returncode, 130, r.stderr)
        self.assertEqual(self.live_files(), [])
        self.assertFalse((self.plan / 'tasks/a/retry_budget').exists())

    def test_a_stale_per_task_budget_from_an_earlier_invocation_is_removed_at_start(self):
        self.task('x', ['PASS']); self.task('y', ['PASS'])
        (self.plan / 'tasks/x/status').write_text('FAIL\n')
        (self.plan / 'tasks/x/retry_budget').write_text('5')
        (self.plan / 'tasks/x/retries.used').write_text('3')
        self.hook('y', 1, '[ -e "$plan/tasks/x/retry_budget" ] && : > "$plan/stale_budget_seen"\n')
        r = self.run_scheduler()                          # no --retry-failed: x is left for a human
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual((self.status('x'), self.calls('x')), ('FAIL', 0))
        self.assertFalse((self.plan / 'stale_budget_seen').exists())
        self.assertFalse((self.plan / 'tasks/x/retries.used').exists())

    def test_a_stale_budget_of_an_id_missing_from_tasks_tsv_is_removed_too(self):
        self.task('a', ['PASS'])
        (self.plan / 'tasks/ghost').mkdir()
        (self.plan / 'tasks/ghost/retry_budget').write_text('9')
        self.assertEqual(self.run_scheduler().returncode, 0)
        self.assertFalse((self.plan / 'tasks/ghost/retry_budget').exists())

    # --- the retry budget ------------------------------------------------------------
    def test_a_budget_raised_after_a_task_ended_gives_it_exactly_one_retry(self):
        self.task('x', ['FAIL', 'FAIL', 'PASS']); self.task('y', ['PASS'])
        self.hook('y', 1, AFTER_X_ENDED + '''put "$plan/tasks/x/retry_budget" 1
waitfor "$plan/tasks/x/retries.used"
# long enough for several re-checks: x ends FAIL again, and its budget of 1 is spent
for _ in $(seq 1 500); do [ "$(grep -c '^x end' "$plan/events.log")" -ge 2 ] && break; sleep 0.02; done
sleep 1.3
''')
        r = self.run_scheduler('--retry-failed', 0)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual((self.status('x'), self.calls('x'), self.status('y')), ('FAIL', 2, 'MERGED'))
        self.assertEqual(self.events().count('x prepared'), 1)
        self.assertEqual(self.read('tasks/x/retries.used').strip(), '1')
        self.assertIn('automatic retry 1/1', r.stderr)
        self.assertFalse((self.plan / 'tasks/x/retry_budget').exists())   # gone with the run

    def test_a_budget_of_zero_for_one_task_leaves_the_others_retrying(self):
        self.task('x', ['FAIL', 'PASS']); self.task('w', ['FAIL', 'PASS'])
        self.hook('x', 1, 'put "$plan/tasks/x/retry_budget" 0\n')
        r = self.run_scheduler('--retry-failed', 3)
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual((self.status('x'), self.calls('x')), ('FAIL', 1))
        self.assertEqual((self.status('w'), self.calls('w')), ('MERGED', 2))
        self.assertIsNone(self.read('tasks/x/retries.used'))
        self.assertEqual(self.read('tasks/w/retries.used').strip(), '1')

    def test_raising_the_run_wide_budget_applies_to_every_task_and_shows_in_the_usage_line(self):
        self.task('x', ['FAIL', 'FAIL', 'PASS'])
        self.hook('x', 1, 'put "$plan/retry_budget" 3\n')
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual((r.returncode, self.status('x'), self.calls('x')), (0, 'MERGED', 3), r.stderr)
        self.assertIn('automatic retry 1/3', r.stderr)
        self.assertIn('automatic retry 2/3', r.stderr)

    def test_an_invalid_per_task_budget_falls_back_to_the_plan_file(self):
        # The plan file is raised to 2 while the flag says 1, so only a fallback to the
        # file (not to the flag, and not to 0) lets x reach its third and passing run.
        for bad in ('abc', '-1', '', ' \n', '1.5', '1 2', '+1', '1' * 70):
            with self.subTest(value=bad):
                self.reset()
                self.task('x', ['FAIL', 'FAIL', 'PASS'])
                self.hook('x', 1, 'put "$plan/retry_budget" 2\nprintf %s $' + repr(bad) + ' > "$t/retry_budget"\n')
                r = self.run_scheduler('--retry-failed', 1)
                self.assertEqual((r.returncode, self.status('x'), self.calls('x')), (0, 'MERGED', 3), r.stderr)

    def test_an_invalid_plan_budget_falls_back_to_the_flag(self):
        self.task('x', ['FAIL', 'PASS'])
        self.hook('x', 1, 'put "$plan/retry_budget" abc\n')
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual((r.returncode, self.status('x'), self.calls('x')), (0, 'MERGED', 2), r.stderr)

    def test_a_retry_that_cannot_be_prepared_is_not_repeated_until_the_budget_changes(self):
        self.task('x', ['FAIL', 'PASS']); self.task('y', ['PASS'])
        (self.plan / 'prepare_fails' / 'x').write_text('')
        self.hook('y', 1, AFTER_X_ENDED + '''put "$plan/tasks/x/retry_budget" 1
sleep 2.3
grep -c '^x prepared' "$plan/events.log" > "$plan/prepared_before_raise"
rm "$plan/prepare_fails/x"
put "$plan/tasks/x/retry_budget" 2
waitfor "$plan/tasks/x/retries.used"
''')
        r = self.run_scheduler()
        self.assertEqual(self.read('prepared_before_raise').strip(), '1')   # not once per second
        self.assertEqual((r.returncode, self.status('x'), self.calls('x')), (0, 'MERGED', 2), r.stderr)

    # --- what blocks a retry -----------------------------------------------------------
    def retried_after(self, setup):
        self.task('x', ['FAIL', 'PASS'])
        self.hook('x', 1, setup)
        r = self.run_scheduler('--retry-failed', 2)
        return r, self.calls('x')

    def test_escalation_stop_blocks_an_automatic_retry(self):
        r, calls = self.retried_after(': > "$t/escalation.stop"\n')
        self.assertEqual((r.returncode, calls, self.status('x')), (5, 1, 'FAIL'), r.stderr)

    def test_a_pin_lets_a_stopped_task_be_retried_automatically(self):
        r, calls = self.retried_after(': > "$t/escalation.stop"\necho sol:high > "$t/escalation.pin"\n')
        self.assertEqual((r.returncode, calls, self.status('x')), (0, 2, 'MERGED'), r.stderr)

    def test_noretry_blocks_even_with_a_pin(self):
        r, calls = self.retried_after(': > "$t/noretry"\n: > "$t/escalation.stop"\necho sol:high > "$t/escalation.pin"\n')
        self.assertEqual((r.returncode, calls, self.status('x')), (5, 1, 'FAIL'), r.stderr)

    # --- the infra budget --------------------------------------------------------------
    def test_lowering_the_infra_budget_to_zero_stops_further_waits(self):
        self.task('a', ['INFRA', 'INFRA', 'PASS'])
        self.hook('a', 2, 'put "$plan/infra_budget" 0\n')
        r = self.run_scheduler('--infra-retries', 5)
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual((self.status('a'), self.calls('a')), ('INFRA', 2))
        self.assertEqual(r.stderr.count('waiting'), 1)

    def test_raising_the_infra_budget_lets_a_run_wait_that_would_have_stopped(self):
        self.task('a', ['INFRA', 'PASS'])
        self.hook('a', 1, 'put "$plan/infra_budget" 1\n')
        r = self.run_scheduler()                                    # --infra-retries 0
        self.assertEqual((r.returncode, self.status('a'), self.calls('a')), (0, 'MERGED', 2), r.stderr)

    # --- never failing a run over the control files -------------------------------------
    def test_a_held_control_lock_does_not_fail_the_run(self):
        holder = subprocess.Popen(
            [sys.executable, '-c',
             'import fcntl, os, sys, time\n'
             'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n'
             'fcntl.flock(fd, fcntl.LOCK_EX)\n'
             'print("held", flush=True)\n'
             'time.sleep(10)\n', str(self.plan / 'control.lock')],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait); self.addCleanup(holder.kill); self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        self.task('a', ['PASS'])
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual((r.returncode, self.status('a')), (0, 'MERGED'), r.stderr)
        self.assertIn('control.lock', r.stderr)
        self.assertEqual(self.read('tasks/a/seen.retry_budget.1').strip(), '1')   # written anyway
        self.assertEqual(self.live_files(), [])

    # --- path safety -------------------------------------------------------------------
    def test_a_symlinked_budget_file_is_neither_read_nor_removed(self):
        victim = self.root / 'victim'; victim.write_text('5')
        self.task('x', ['PASS'])
        (self.plan / 'tasks/x/status').write_text('FAIL\n')
        (self.plan / 'tasks/x/retry_budget').symlink_to(victim)
        r = self.run_scheduler()
        self.assertEqual((r.returncode, self.calls('x')), (5, 0), r.stderr)    # the 5 was not believed
        self.assertTrue((self.plan / 'tasks/x/retry_budget').is_symlink())
        self.assertEqual(victim.read_text(), '5')

    def test_the_usage_counter_is_not_written_through_a_symlink(self):
        victim = self.root / 'victim'; victim.write_text('keep')
        self.task('x', ['FAIL', 'PASS'])
        (self.plan / 'tasks/x/retries.used').symlink_to(victim)
        r = self.run_scheduler('--retry-failed', 1)
        self.assertEqual((r.returncode, self.status('x')), (0, 'MERGED'), r.stderr)   # the run goes on
        self.assertEqual(victim.read_text(), 'keep')
        self.assertIn('retries.used', r.stderr)

    def test_a_symlinked_task_dir_is_not_a_place_to_write_or_read_budgets(self):
        elsewhere = self.root / 'elsewhere'; elsewhere.mkdir()
        (elsewhere / 'retry_budget').write_text('5')
        (elsewhere / 'status').write_text('FAIL\n')
        self.task('y', ['PASS'])
        self.rows.append('x\t-\tlow\tf_x\tsol\topus\tX')
        (self.plan / 'tasks/x').symlink_to(elsewhere)
        (self.plan / 'outcomes/x').write_text('PASS\n')
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), ['retry_budget', 'status'])
        self.assertEqual((elsewhere / 'retry_budget').read_text(), '5')


    # --- drain, stop and a live max-parallel (forge control writes these files) -----------
    def test_a_drain_holds_new_dispatches_and_the_scheduler_waits_until_resume(self):
        self.task('a', ['PASS'])
        self.task('b', ['PASS'])
        self.hook('a', 1, 'put "$plan/drain" ""\n')
        proc = self.start(capacity=1)
        self.wait_for(lambda: self.status('a') == 'MERGED', 'a to merge')
        time.sleep(0.5)
        self.assertIsNone(proc.poll(), 'a drained scheduler must wait, not exit')
        self.assertEqual((self.calls('b'), self.status('b')), (0, 'PENDING'))
        (self.plan / 'drain').unlink()
        r = self.finish(proc)
        self.assertEqual((r.returncode, self.status('b')), (0, 'MERGED'), r.stderr)
        self.assertEqual(r.stderr.count('drained — 1 task(s) waiting'), 1, r.stderr)

    def test_a_graceful_stop_finishes_the_running_and_leaves_the_rest_pending(self):
        self.task('a', ['PASS'])
        self.task('x', ['FAIL', 'PASS'])
        self.task('b', ['PASS'])
        self.task('c', ['PASS'], deps='a')
        self.hook('a', 1, 'put "$plan/stop" graceful\n')
        self.hook('x', 1, 'waitfor "$plan/stop"\n')     # x fails after the stop: it is not retried
        r = self.run_scheduler('--retry-failed', 1, capacity=2)
        self.assertEqual(r.returncode, 130, r.stderr)
        self.assertEqual((self.status('a'), self.status('x'), self.calls('x')), ('MERGED', 'FAIL', 1))
        self.assertNotIn('x prepared', self.events())
        self.assertEqual((self.status('b'), self.status('c')), ('PENDING', 'PENDING'))
        self.assertFalse((self.plan / 'tasks/b/status').exists())
        self.assertIn('stopped by forge control stop; 2 task(s) not started', r.stderr)
        self.assertEqual([n for n in ('drain', 'stop') if (self.plan / n).exists()], [])

    def test_stop_now_interrupts_the_running_task(self):
        self.task('a', ['PASS'])
        self.task('b', ['PASS'])
        self.hook('a', 1, 'put "$plan/stop" now\nsleep 30\n')
        self.env['FORGE_INTERRUPT_GRACE'] = '1'
        r = self.run_scheduler(capacity=1)
        self.assertEqual(r.returncode, 130, r.stderr)
        self.assertEqual((self.status('a'), self.calls('b')), ('INTERRUPTED', 0))
        self.assertFalse((self.plan / 'stop').exists())

    def test_a_stop_ends_a_long_infrastructure_wait(self):
        for mode in ('graceful', 'now'):
            with self.subTest(mode):
                self.reset()
                self.task('a', ['INFRA_LONG', 'PASS'])
                proc = self.start('--infra-retries', 1)
                paused = self.plan / 'infra.paused'
                self.wait_for(lambda: paused.exists() and json.loads(paused.read_text())['scopes'][0]['until'],
                              'the 600 s wait to start')
                (self.plan / 'stop').write_text(mode)
                started = time.monotonic()
                r = self.finish(proc, timeout=20)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual((r.returncode, self.status('a'), self.calls('a')), (130, 'INFRA', 1), r.stderr)

    def test_a_drained_idle_scheduler_exits_promptly_on_stop(self):
        self.task('a', ['PASS'])
        self.task('b', ['PASS'])
        self.hook('a', 1, 'put "$plan/drain" ""\n')
        proc = self.start(capacity=1)
        self.wait_for(lambda: self.status('a') == 'MERGED', 'a to merge')
        time.sleep(0.3)
        self.assertIsNone(proc.poll())
        (self.plan / 'stop').write_text('graceful')
        started = time.monotonic()
        r = self.finish(proc, timeout=20)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual((r.returncode, self.status('b')), (130, 'PENDING'), r.stderr)

    def test_raising_max_parallel_mid_run_lets_the_next_tasks_run_together(self):
        for name in 'abc':
            self.task(name, ['PASS'])
        self.hook('a', 1, 'put "$plan/max_parallel" 3\nwaitfor "$plan/tasks/b/calls"\n')
        r = self.run_scheduler(capacity=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        events = self.events()
        first_end = next(i for i, e in enumerate(events) if ' end ' in e)
        self.assertGreaterEqual(sum(' start ' in e for e in events[:first_end]), 2, events)

    def test_an_invalid_max_parallel_falls_back_to_the_argument(self):
        for name in 'ab':
            self.task(name, ['PASS'])
        (self.plan / 'max_parallel').write_text('65')
        self.hook('a', 1, 'sleep 0.5\n')
        r = self.run_scheduler(capacity=1)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.events()[:2], ['a start 1', 'a end PASS'])


if __name__ == '__main__':
    unittest.main()
