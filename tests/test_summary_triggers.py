"""The scheduler starts `forge-summarize.py <plan> --task <id>` when a task changes (#133 B).

The worker is a small bash script standing in for forge-parallel.sh's `_task`, `_merge` and
`_prepare_retry`; each phase of a task lasts long enough for the scheduler's loop to see it, so
the count of triggers is deterministic. The summarize script is a stub that records each call
(FORGE_SUMMARIZE); the switch itself is the real forge_side.allowed()."""
import dataset_isolation  # noqa: F401  keeps the run dataset and forge's config out of the developer's home
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]

WORKER = r'''#!/bin/bash
cmd="$1"; plan="$2"; name="$3"; t="$plan/tasks/$name"
mkdir -p "$t"
case "$cmd" in
  _task)
    n=$(( $(cat "$t/calls" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$t/calls"
    echo RUNNING > "$t/status"
    [ -e "$plan/fast/$name" ] || sleep 0.4
    echo "reply $n" > "$t/dwarf.last"         # the dwarf is done; the status is still RUNNING
    [ -e "$plan/fast/$name" ] || sleep 0.4
    echo "review $n" > "$t/qa.last"          # the verdict, while the attempt is still open
    [ -e "$plan/fast/$name" ] || sleep 0.4
    outcome="$(head -1 "$plan/outcomes/$name")"
    tail -n +2 "$plan/outcomes/$name" > "$plan/outcomes/$name.rest"; mv "$plan/outcomes/$name.rest" "$plan/outcomes/$name"
    echo "$n" >> "$t/attempts.tsv"           # the attempt ends
    echo "$outcome" > "$t/status"
    ;;
  _merge)
    if [ "$(cat "$t/status" 2>/dev/null)" = PASS ]; then touch "$t/merged"; fi
    ;;
  _prepare_retry)
    rm -f "$t/status" "$t/noretry"
    ;;
esac
'''

# Records "<task> <status then>" per call.
SUMMARIZE = textwrap.dedent('''\
    import os, sys
    plan, task = sys.argv[1], sys.argv[3]
    root = os.path.join(plan, 'tasks', task)
    state = 'MERGED' if os.path.exists(os.path.join(root, 'merged')) else (
        open(os.path.join(root, 'status')).read().strip() if os.path.exists(os.path.join(root, 'status')) else 'PENDING')
    with open(os.path.join(plan, 'summarize.calls'), 'a') as out:
        out.write(task + ' ' + state + '\\n')
    if os.environ.get('SUMMARIZE_FAIL'):
        sys.exit(7)
    ''')


class SummaryTriggerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.plan = self.root / 'plan'; self.plan.mkdir()
        (self.plan / 'outcomes').mkdir()
        (self.plan / 'wt_root').write_text(str(self.root))
        self.worker = self.root / 'worker.sh'; self.worker.write_text(WORKER)
        self.stub = self.root / 'summarize.py'; self.stub.write_text(SUMMARIZE)
        self.env = dict(os.environ, FORGE_SUMMARIZE=str(self.stub))
        self.env.pop('FORGE_SUMMARIES', None)
        self.rows = []

    def task(self, name, outcomes):
        self.rows.append(f'{name}\t-\tlow\tf_{name}\tsol\topus\t{name.upper()}')
        (self.plan / 'outcomes' / name).write_text(''.join(o + '\n' for o in outcomes))
        root = self.plan / 'tasks' / name; root.mkdir(parents=True)
        (root / 'base_ref').write_text('base\n')

    def on(self):
        (self.plan / 'summaries').write_text('on\n')
        (self.plan / 'summaries_model').write_text('sol:high:codex\n')

    def run_scheduler(self, *extra, env=None):
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')
        command = [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker),
                   str(self.plan), '3', *map(str, extra)]
        return subprocess.run(command, env=env or self.env, text=True, timeout=60,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def calls(self, task=None):
        path = self.plan / 'summarize.calls'
        lines = path.read_text().splitlines() if path.exists() else []
        return [line for line in lines if task is None or line.split()[0] == task]

    def settle(self):
        """The triggers are detached: let the last one record itself."""
        import time
        end = time.monotonic() + 5
        previous = None
        while time.monotonic() < end:
            now = self.calls()
            if now == previous:
                return
            previous = now
            time.sleep(0.4)

    def test_off_starts_nothing_and_leaves_no_file(self):
        self.task('a', ['FAIL', 'PASS'])
        result = self.run_scheduler('--retry-failed', 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settle()
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.plan / 'summaries.out').exists())
        self.assertFalse((self.plan / 'side').exists())

    def test_the_environment_switch_beats_the_plan_file(self):
        self.on(); self.task('a', ['PASS'])
        result = self.run_scheduler(env=dict(self.env, FORGE_SUMMARIES='off'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settle()
        self.assertEqual(self.calls(), [])

    def test_one_trigger_per_change_through_running_verdict_fail_retry_and_merged(self):
        self.on(); self.task('a', ['FAIL', 'PASS'])
        result = self.run_scheduler('--retry-failed', 1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settle()
        # attempt 1: RUNNING, the dwarf's reply, its review; it ends FAIL and is requeued in the same pass (one
        # change: the retry); attempt 2: RUNNING, reply, review; MERGED when the pass merges it. Eight changes, eight
        # triggers: the hundreds of loops between them start nothing. (What the stub saw of the status
        # is only the moment it ran, so only the count is asserted.)
        self.assertEqual(len(self.calls('a')), 8, self.calls('a'))

    def test_a_task_override_off_silences_that_task_only(self):
        self.on(); self.task('a', ['PASS']); self.task('b', ['PASS'])
        (self.plan / 'tasks/b/summaries').write_text('off\n')
        result = self.run_scheduler()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settle()
        self.assertEqual(self.calls('b'), [])
        self.assertEqual(len(self.calls('a')), 4, self.calls('a'))   # RUNNING, the reply, its review, MERGED

    def test_a_task_override_on_summarizes_that_task_under_an_off_plan(self):
        (self.plan / 'summaries').write_text('off\n')
        (self.plan / 'summaries_model').write_text('sol:high:codex\n')
        self.task('a', ['PASS']); self.task('b', ['PASS'])
        (self.plan / 'tasks/a/summaries').write_text('on\n')
        self.assertEqual(self.run_scheduler().returncode, 0)
        self.settle()
        self.assertEqual(self.calls('b'), [])
        self.assertTrue(self.calls('a'))

    def test_a_held_admission_lock_never_holds_the_scheduler_back(self):
        import fcntl
        self.on(); self.task('a', ['PASS'])
        (self.plan / 'side' / 'summary').mkdir(parents=True)
        with open(self.plan / 'side' / 'summary' / '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)   # another allocator holds it for the whole run
            result = self.run_scheduler()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.plan / 'tasks/a/merged').exists())

    def test_an_admission_check_still_blocked_at_the_end_is_handed_to_a_detached_summarize(self):
        import fcntl
        self.on(); self.task('a', ['PASS'])
        (self.plan / 'side' / 'summary').mkdir(parents=True)
        with open(self.plan / 'side' / 'summary' / '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_scheduler()
        # The scheduler's own check threads die with it; the final transition (MERGED) must not be lost.
        self.assertEqual(result.returncode, 0, result.stderr)
        self.settle()
        self.assertTrue(self.calls('a'), 'the blocked admission check was dropped at shutdown')
        self.assertTrue((self.plan / 'summaries.out').exists())

    def test_a_task_added_during_the_run_gets_its_summaries_even_when_it_finishes_fast(self):
        import json, time
        self.on(); self.task('a', ['PASS'])
        (self.plan / 'fast').mkdir(); (self.plan / 'fast' / 'n').write_text('')
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')
        command = [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker), str(self.plan), '3']
        proc = subprocess.Popen(command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        time.sleep(0.5)
        self.task('n', ['PASS'])
        # Atomically: the live scheduler reads tasks.tsv on its own clock, and a half-written file
        # (write_text truncates first) would refuse the add below as "the row is not in tasks.tsv".
        staged = self.plan / 'tasks.tsv.new'
        staged.write_text('\n'.join(self.rows) + '\n'); os.replace(staged, self.plan / 'tasks.tsv')
        requests = self.plan / 'control.requests'; requests.mkdir(exist_ok=True)
        (requests / 'r1').write_text(json.dumps({'verb': 'add', 'task': 'n'}))
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertTrue((self.plan / 'tasks/n/merged').exists())
        self.settle()
        self.assertTrue(self.calls('n'), 'the added task was never summarised')

    def test_a_failing_summarize_changes_neither_the_task_nor_the_exit_code(self):
        self.on(); self.task('a', ['FAIL', 'PASS'])
        result = self.run_scheduler('--retry-failed', 1, env=dict(self.env, SUMMARIZE_FAIL='1'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.plan / 'tasks/a/merged').exists())
        self.settle()
        self.assertTrue(self.calls('a'))

    def test_a_missing_summarize_script_never_stops_the_run(self):
        self.on(); self.task('a', ['PASS'])
        result = self.run_scheduler(env=dict(self.env, FORGE_SUMMARIZE=str(self.root / 'nowhere.py')))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.plan / 'tasks/a/merged').exists())


if __name__ == '__main__':
    unittest.main()
