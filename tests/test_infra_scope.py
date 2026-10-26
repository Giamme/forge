"""Infrastructure pauses scoped by class (#30): a rate limit pauses one model, a quota or
auth stop one harness, anything else everything. The worker stands in for forge-parallel.sh
as in test_schedule.py; an INFRA outcome names its class, harness and model, the way
infra_stop copies them from dispatch. Specs are real registry aliases: sol and astra run on
codex, sonnet and opus on claude."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
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
    if [ -f "$plan/hold/$name" ]; then
      for _ in $(seq 1 1500); do [ -f "$plan/gate" ] && break; sleep 0.02; done
    fi
    outcome="$(head -1 "$plan/outcomes/$name")"
    tail -n +2 "$plan/outcomes/$name" > "$plan/outcomes/$name.rest"; mv "$plan/outcomes/$name.rest" "$plan/outcomes/$name"
    case "$outcome" in
      PASS) echo PASS > "$t/status"; rm -f "$t/resume" ;;
      INFRA*)
        read -r kind cls harness model <<< "$outcome"
        echo INFRA > "$t/status"
        printf 'class=%s\nharness=%s\nmodel=%s\n' "$cls" "$harness" "$model" > "$t/infra.txt"
        if [ "$kind" = INFRAQA ]; then echo stage=qa >> "$t/infra.txt"; echo qa > "$t/resume"
        else echo stage=dwarf >> "$t/infra.txt"; rm -f "$t/resume"; fi ;;
    esac
    echo "$name end ${outcome%% *}" >> "$plan/events.log"
    ;;
  _merge)
    if [ "$(cat "$t/status" 2>/dev/null)" = PASS ]; then touch "$t/merged"; echo "$name merged" >> "$plan/events.log"; fi
    ;;
  _qa_only)
    [ "$(cat "$t/resume" 2>/dev/null)" = qa ]
    ;;
esac
'''

SOL_LIMIT = 'INFRA rate_limit codex gpt-6.1-sol'


class InfraScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.plan = self.root / 'plan'; self.plan.mkdir()
        (self.plan / 'outcomes').mkdir(); (self.plan / 'hold').mkdir()
        (self.plan / 'wt_root').write_text(str(self.root))
        self.worker = self.root / 'worker.sh'; self.worker.write_text(WORKER)
        self.env = dict(os.environ, FORGE_INFRA_BACKOFF='0.05')
        self.rows = []

    def task(self, name, outcomes, dwarf, qa='astra', hold=False):
        self.rows.append(f'{name}\t-\tlow\tf_{name}\t{dwarf}\t{qa}\t{name.upper()}')
        (self.plan / 'outcomes' / name).write_text(''.join(o + '\n' for o in outcomes))
        root = self.plan / 'tasks' / name; root.mkdir(parents=True)
        (root / 'base_ref').write_text('base\n')
        if hold:
            (self.plan / 'hold' / name).write_text('')

    def command(self, *extra, capacity=1):
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')
        return [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker), str(self.plan),
                str(capacity), *map(str, extra)]

    def run_scheduler(self, *extra, capacity=1):
        return subprocess.run(self.command(*extra, capacity=capacity), env=self.env, text=True, timeout=60,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def status(self, name):
        root = self.plan / 'tasks' / name
        if (root / 'merged').exists():
            return 'MERGED'
        f = root / 'status'
        return f.read_text().strip() if f.exists() else 'PENDING'

    def calls(self, name):
        f = self.plan / 'tasks' / name / 'calls'
        return int(f.read_text()) if f.exists() else 0

    def test_a_rate_limited_model_waits_alone_while_tasks_on_another_model_finish(self):
        self.task('a', [SOL_LIMIT] * 3, dwarf='sol')
        self.task('b', ['PASS'], dwarf='astra')
        self.task('c', ['PASS'], dwarf='astra')
        r = self.run_scheduler('--infra-retries', 2)
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual((self.status('b'), self.status('c')), ('MERGED', 'MERGED'))
        # Its own two waits, then it stays INFRA for the next run.
        self.assertEqual((self.status('a'), self.calls('a')), ('INFRA', 3))
        self.assertIn('on codex/gpt-6.1-sol for a', r.stderr)
        self.assertIn('other tasks continue', r.stderr)
        self.assertFalse((self.plan / 'tasks/b/infra.txt').exists())

    def test_a_quota_stop_holds_every_task_on_that_harness_and_no_other(self):
        self.task('a', ['INFRA quota claude sonnet'], dwarf='sonnet')
        self.task('b', ['PASS'], dwarf='opus')            # another model, same harness
        self.task('c', ['PASS'], dwarf='sol')
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual(self.status('c'), 'MERGED')
        self.assertEqual((self.status('a'), self.status('b'), self.calls('b')), ('INFRA', 'PENDING', 0))
        self.assertFalse((self.plan / 'tasks/b/status').exists())   # held, never BLOCKED

    def test_a_qa_only_resume_is_held_by_its_qa_pair_alone(self):
        self.task('a', [SOL_LIMIT], dwarf='sol')
        # q's next entry only re-runs its review on claude: the paused sol does not hold it.
        self.task('q', ['PASS'], dwarf='sol', qa='sonnet')
        (self.plan / 'tasks/q/status').write_text('INFRA\n')
        (self.plan / 'tasks/q/resume').write_text('qa\n')
        self.task('r', ['PASS'], dwarf='sol', qa='sonnet')   # a full entry: held by its implementer
        self.task('s', ['PASS'], dwarf='astra', qa='sol')    # its review would hit the limit too
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual(self.status('q'), 'MERGED')
        self.assertEqual((self.calls('r'), self.calls('s')), (0, 0))

    def test_any_other_class_still_pauses_everything_until_the_drain(self):
        self.task('a', ['INFRA network codex gpt-6.1-sol'], dwarf='sol')
        self.task('b', ['PASS'], dwarf='astra')
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 8, r.stderr)
        self.assertEqual((self.status('b'), self.calls('b')), ('PENDING', 0))

    def test_infra_paused_and_show_describe_the_pause_while_live_and_are_gone_after(self):
        self.task('a', [SOL_LIMIT], dwarf='sol')
        self.task('b', ['PASS'], dwarf='astra', hold=True)
        scheduler = subprocess.Popen(self.command(capacity=2), env=self.env, text=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: scheduler.poll() is None and scheduler.kill())
        paused = self.plan / 'infra.paused'
        deadline = time.monotonic() + 20
        while not paused.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        scopes = json.loads(paused.read_text())['scopes']
        self.assertEqual(scopes, [{'scope': 'model:codex/gpt-6.1-sol', 'class': 'rate_limit', 'harness': 'codex',
                                   'model': 'gpt-6.1-sol', 'tasks': ['a'], 'cycle': 0, 'until': None,
                                   'exhausted': True}])
        shown = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-control.py'), 'show', str(self.plan), '--json'],
                               stdout=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(json.loads(shown.stdout)['run']['infra_paused'], scopes)
        text = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-control.py'), 'show', str(self.plan)],
                              stdout=subprocess.PIPE, text=True, timeout=30).stdout
        self.assertIn('paused model:codex/gpt-6.1-sol (rate_limit)  tasks a  waits used up', text)
        (self.plan / 'gate').write_text('')
        _, err = scheduler.communicate(timeout=30)
        self.assertEqual(scheduler.returncode, 8, err)
        self.assertEqual(self.status('b'), 'MERGED')
        self.assertFalse(paused.exists())
        shown = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-control.py'), 'show', str(self.plan), '--json'],
                               stdout=subprocess.PIPE, text=True, timeout=30)
        self.assertIsNone(json.loads(shown.stdout)['run']['infra_paused'])


if __name__ == '__main__':
    unittest.main()
