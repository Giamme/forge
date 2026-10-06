"""Scheduler behaviour that needs no model and no git: in-run retries, infrastructure
pauses and resume. The worker is a small bash script standing in for forge-parallel.sh's
`_task`, `_merge` and `_prepare_retry` entry points; each task's outcomes are queued in a
file and consumed one per `_task` call, so every scenario is deterministic.

Ordering is asserted from an event log, never from wall-clock timing."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def task(self, name, outcomes, deps='-', files=None, hold=False):
        self.rows.append(f'{name}\t{deps}\tlow\t{files or "f_" + name}\tsol\topus\t{name.upper()}')
        (self.plan / 'outcomes' / name).write_text(''.join(o + '\n' for o in outcomes))
        root = self.plan / 'tasks' / name; root.mkdir(parents=True)
        (root / 'base_ref').write_text('base\n')   # the scheduler would otherwise ask git
        if hold: (self.plan / 'hold' / name).write_text('')

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


if __name__ == '__main__':
    unittest.main()
