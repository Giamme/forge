"""NEEDS_DECISION (#69): a task whose reviews do not converge, or that the retry policy cannot move
to a stronger model, stops and waits for a person, and only a person's explicit decision resumes it.

Run cases drive forge-parallel.sh with the fake agent CLI from test_forge.py (see test_needs_input.py
for the shape); the scheduler cases use the stand-in worker from test_control_requests.py. Fixtures
are borrowed, not inherited. `converge_window` is set to 2 (or 3) to keep the scenarios short."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

import test_control_requests as control_fixtures
import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
# Reviews that fail with a new finding each time, all P1: the ledger sees a moving target.
MOVING = ['- [P1][CONFIRMED] alpha.py:1 — frobnicator leaks sockets',
          '- [P1][CONFIRMED] beta.py:2 — widget tolerance computed wrongly',
          '- [P1][CONFIRMED] gamma.py:3 — scheduler ignores cancellation',
          '- [P1][CONFIRMED] delta.py:4 — cache invalidation misses entries']
SAME = '- [P1][CONFIRMED] a.txt:1 — broken'


class DecisionCase(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, *flags, window=None):
        """a, and b which depends on a, each with a prompt."""
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\nb\ta\tlow\tb.txt\tsol\topus\tB\n')
        for i in 'ab':
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        if window is not None:
            (p / 'converge_window').write_text('%d\n' % window)
        return p

    def moving_table(self, reviews=4, verdict='FAIL'):
        table = {'a.qa.%d' % n: {'verdict': verdict, 'findings': MOVING[(n - 1) % len(MOVING)]}
                 for n in range(1, reviews + 1)}
        table.update({'a.dwarf.%d' % n: {'content': 'try %d' % n} for n in range(1, 12)})
        return table

    def converging_plan(self, window=2):
        """Two reviews in a row fail with new findings: the signal stops `a` after its second attempt."""
        p = self.plan_of(window=window)
        self.scenario(self.moving_table())
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.read(p, 'status'), 'NEEDS_DECISION')
        return p, r

    def split_plan(self):
        """Three failed attempts on the only model there is: the retry policy stops the task."""
        p = self.plan_of('--dwarf-low', 'sol', window=9)
        table = {'a.qa.%d' % n: {'verdict': 'FAIL', 'findings': SAME} for n in range(1, 8)}
        table.update({'a.dwarf.%d' % n: {'content': 'try %d' % n} for n in range(1, 12)})
        self.scenario(table)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual(r.returncode, 5, r.stdout)
        return p, r

    def read(self, plan, name, task='a'):
        return (plan / 'tasks' / task / name).read_text().strip()

    def decisions_of(self, p, task):
        return [row.split('\t') for row in (p / 'tasks' / task / 'decisions.tsv').read_text().splitlines()]

    def decisions(self, p):
        path = p / 'tasks/a/decisions.tsv'
        return [line.split('\t') for line in path.read_text().splitlines()] if path.exists() else []

    def dwarf_count(self):
        return self.calls().count('dwarf')

    def retry(self, p, *args):
        return self.run_script('forge-parallel.sh', 'retry', p, 'a', *args)


class StopTests(DecisionCase):
    def test_the_window_is_a_plan_flag_and_a_control_setting(self):
        # The final review of run 1: both were documented, neither existed; only a hand-edited file worked.
        p = self.plan_of('--converge-window', 2)
        self.assertEqual((p / 'converge_window').read_text(), '2\n')
        self.scenario(self.moving_table())
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual((self.read(p, 'status'), self.dwarf_count()), ('NEEDS_DECISION', 2))
        done = subprocess.run(['python3', str(ROOT / 'scripts' / 'forge-control.py'), 'set', str(p), 'converge-window', '5'],
                              capture_output=True, text=True, env=dict(self.env, FORGE_DATASET='off'), timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((p / 'converge_window').read_text(), '5\n')

    def test_the_signal_stops_the_task_and_the_run_says_so(self):
        p, r = self.converging_plan()
        self.assertEqual(self.dwarf_count(), 2)                      # not a third attempt
        text = (p / 'tasks/a/decision.md').read_text()
        self.assertEqual(text.splitlines()[0], 'reasons: converging')
        self.assertIn('Signal: 2 failed reviews in a row', text)
        self.assertRegex(text, r'attempt\s+verdict\s+confirmed\s+new\s+worst')
        self.assertRegex(text, r'\n  1\s+FAIL\s+1\s+1\s+P1\n  2\s+FAIL\s+1\s+1\s+P1\n')
        self.assertIn('Attempts used so far: 2', text)
        for command in (f'forge-parallel.sh retry {p} a --decide retry=N --reason "..." --approved',
                        f'forge-parallel.sh retry {p} a --decide threshold=P1 --reason "..." --approved',
                        f'forge-parallel.sh retry {p} a --dwarf <spec> --reason "..." --approved',
                        f'forge-parallel.sh accept {p} a --reason "..." --approved'):
            self.assertIn(command, text)
        # The split is an edit of tasks.tsv and a later run: drain/add need a live scheduler, gone by now.
        for shown in (text, r.stdout):
            self.assertIn(f'forge-parallel.sh run {p}', shown)
            self.assertNotIn('forge control drain', shown)
        # attempts.tsv keeps FAIL for the attempt; the status is the stop.
        attempts = [line.split('\t') for line in (p / 'tasks/a/attempts.tsv').read_text().splitlines()]
        self.assertEqual([(row[1], row[2]) for row in attempts], [('1', 'FAIL'), ('2', 'FAIL')])
        self.assertEqual(self.read(p, 'status', 'b'), 'BLOCKED')
        self.assertIn('waiting for your decision:\n'
                      f'  a: converging — {p}/tasks/a/decision.md\n', r.stdout)
        self.assertIn('--decide retry=N | --decide threshold=Pn | --dwarf <spec>', r.stdout)
        self.assertNotIn('retry a failed task', r.stdout)

    def test_the_policy_stop_is_a_decision_now(self):
        p, r = self.split_plan()
        self.assertEqual(self.read(p, 'status'), 'NEEDS_DECISION')
        self.assertEqual(self.read(p, 'decision.md').splitlines()[0], 'reasons: split')
        self.assertIn('already in the highest tier', (p / 'tasks/a/split.suggested').read_text())
        self.assertIn('Retry policy:', (p / 'tasks/a/decision.md').read_text())
        rows = [line.split('\t')[2] for line in (p / 'tasks/a/attempts.tsv').read_text().splitlines()]
        self.assertEqual(rows, ['FAIL'] * 3)
        self.assertIn('waiting for your decision:', r.stdout)

    def test_both_reasons_are_listed_in_order(self):
        p = self.plan_of('--dwarf-low', 'sol', window=3)
        self.scenario(self.moving_table())
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.read(p, 'decision.md').splitlines()[0], 'reasons: converging, split')

    def test_a_task_whose_reviews_improve_runs_on_to_its_retry_budget(self):
        p = self.plan_of('--qa-threshold', 'P3', window=2)         # a P3 finding blocks too, so each review fails
        (p / 'escalate_after').write_text('0')                      # the tier policy is off: only the signal could stop it
        table = {'a.qa.1': {'verdict': 'FAIL', 'findings': MOVING[0]},
                 'a.qa.2': {'verdict': 'FAIL', 'findings': MOVING[1].replace('[P1]', '[P2]')},
                 'a.qa.3': {'verdict': 'FAIL', 'findings': MOVING[2].replace('[P1]', '[P3]')}}
        table.update({'a.dwarf.%d' % n: {'content': 'try %d' % n} for n in range(1, 6)})
        self.scenario(table)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 2)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.dwarf_count(), 3)                      # the first attempt and both retries
        self.assertEqual(self.read(p, 'status'), 'FAIL')
        self.assertFalse((p / 'tasks/a/decision.md').exists())


class NothingSilentTests(DecisionCase):
    def test_a_rerun_does_not_dispatch_it_and_its_dependent_ends_blocked(self):
        p, _ = self.converging_plan()
        before = self.dwarf_count()
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 3)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.dwarf_count(), before)
        self.assertEqual(self.read(p, 'status'), 'NEEDS_DECISION')
        self.assertEqual(self.read(p, 'status', 'b'), 'BLOCKED')
        self.assertIn('waiting for your decision:', r.stdout)

    def test_a_plain_retry_is_refused_and_changes_nothing(self):
        p, _ = self.converging_plan()
        before = self.dwarf_count()
        r = self.retry(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('waiting for your decision', r.stdout)
        self.assertIn('reasons: converging', r.stdout)               # the first lines of decision.md
        self.assertEqual((self.read(p, 'status'), self.dwarf_count(), self.decisions(p)), ('NEEDS_DECISION', before, []))

    def test_prepare_retry_alone_does_not_resume_it(self):
        p, _ = self.converging_plan()
        r = self.run_script('forge-parallel.sh', '_prepare_retry', p, 'a')
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.read(p, 'status'), 'NEEDS_DECISION')


class DecisionTests(DecisionCase):
    def test_a_decision_needs_a_reason_and_approved(self):
        p, _ = self.converging_plan()
        before = self.dwarf_count()
        for what, args in (('no approval', ('--decide', 'retry=2', '--reason', 'try again')),
                           ('no reason', ('--decide', 'retry=2', '--approved')),
                           ('an empty reason', ('--decide', 'retry=2', '--reason', ' ', '--approved')),
                           ('dwarf without approval', ('--dwarf', 'sol:high', '--reason', 'try it')),
                           ('too many', ('--decide', 'retry=21', '--reason', 'r', '--approved')),
                           ('none', ('--decide', 'retry=0', '--reason', 'r', '--approved')),
                           ('not a choice', ('--decide', 'more', '--reason', 'r', '--approved')),
                           ('a level that is not one', ('--decide', 'threshold=P4', '--reason', 'r', '--approved'))):
            with self.subTest(what):
                r = self.retry(p, *args)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertEqual((self.read(p, 'status'), self.dwarf_count(), self.decisions(p)),
                                 ('NEEDS_DECISION', before, []))

    def test_a_decision_on_a_failed_task_is_refused(self):
        p = self.plan_of(window=2)
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': SAME}, 'a.dwarf.2': {'content': 'fixed'}})
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        self.assertEqual(self.read(p, 'status'), 'FAIL')
        before = self.dwarf_count()
        r = self.retry(p, '--decide', 'retry=2', '--reason', 'try again', '--approved')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('is FAIL, not waiting for a decision', r.stdout)
        self.assertEqual((self.read(p, 'status'), self.dwarf_count(), self.decisions(p)), ('FAIL', before, []))

    def test_retry_n_is_n_attempts_and_restarts_the_count(self):
        p, _ = self.converging_plan()
        before = self.dwarf_count()
        r = self.retry(p, '--decide', 'retry=2', '--reason', 'new findings are cheap ones', '--approved')
        # Two attempts follow the decision, whatever the run's retry budget was (the run had 5).
        self.assertEqual(self.dwarf_count(), before + 2, r.stdout)
        [row] = self.decisions(p)
        self.assertEqual(row[1:], ['2', 'retry=2', 'new findings are cheap ones'])
        # The signal counts only the reviews after the decision: the first of them was not a window, the
        # second completed one.
        self.assertEqual(self.read(p, 'status'), 'NEEDS_DECISION', r.stdout)
        text = (p / 'tasks/a/decision.md').read_text()
        self.assertIn('Attempts used so far: 4', text)
        self.assertRegex(text, r'\n  3\s+FAIL\s+1\s+1\s+P1\n  4\s+FAIL')   # a new full window failed
        self.assertNotRegex(text, r'\n  [12]\s+FAIL')

    def test_retry_one_is_one_attempt_and_the_signal_waits_for_a_window(self):
        p, _ = self.converging_plan()
        before = self.dwarf_count()
        r = self.retry(p, '--decide', 'retry=1', '--reason', 'one more', '--approved')
        self.assertEqual(self.dwarf_count(), before + 1, r.stdout)
        # The third review fails with a new finding as well, but one review is not a window.
        self.assertEqual(self.read(p, 'status'), 'FAIL', r.stdout)
        r = self.retry(p)                                              # a FAIL task retries as it always did
        self.assertEqual(self.read(p, 'status'), 'NEEDS_DECISION', r.stdout)

    def test_a_decision_on_a_split_stop_gets_its_attempts(self):
        p, _ = self.split_plan()
        self.assertTrue((p / 'tasks/a/escalation.stop').exists())
        before = self.dwarf_count()
        r = self.retry(p, '--decide', 'retry=3', '--reason', 'the findings are shrinking', '--approved')
        self.assertEqual(self.dwarf_count(), before + 3, r.stdout)     # all three, not stopped at once by the old streak
        self.assertEqual([row[1:3] for row in self.decisions(p)], [['3', 'retry=3']])

    def test_retry_n_stops_early_when_an_attempt_passes(self):
        p = self.plan_of(window=9)
        table = {'a.qa.%d' % n: {'verdict': 'FAIL', 'findings': SAME} for n in range(1, 3)}
        table.update({'a.qa.3': {'verdict': 'PASS'}, 'a.qa.4': {'verdict': 'PASS'}})
        table.update({'a.dwarf.%d' % n: {'content': 'try %d' % n} for n in range(1, 12)})
        self.scenario(table)
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 0).returncode, 5)
        (p / 'tasks/a/status').write_text('NEEDS_DECISION\n')
        (p / 'tasks/a/decision.md').write_text('reasons: converging\n')
        before = self.dwarf_count()
        r = self.retry(p, '--decide', 'retry=5', '--reason', 'go on', '--approved')
        self.assertEqual(self.dwarf_count(), before + 2, r.stdout)     # the second of the five passed: no third
        self.assertTrue((p / 'tasks/a/merged').exists(), r.stdout)

    def test_a_quota_stop_keeps_the_decided_attempts_for_the_next_run(self):
        p, _ = self.converging_plan()
        (p / 'converge_window').write_text('9\n')                      # only the allowance can stop it now
        table = {'a.qa.%d' % n: {'verdict': 'FAIL', 'findings': MOVING[(n - 1) % len(MOVING)]} for n in range(1, 9)}
        table.update({'a.dwarf.%d' % n: {'content': 'try %d' % n} for n in range(1, 12)})
        table['a.dwarf.3'] = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}
        self.scenario(table)
        r = self.retry(p, '--decide', 'retry=3', '--reason', 'go on', '--approved')
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertEqual(self.read(p, 'status'), 'INFRA')
        self.assertEqual(self.read(p, 'decided.left'), '2')            # three attempts, none spent yet
        before = self.dwarf_count()
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 0)
        # The resumed attempt and the two that follow it, though the run's budget is 0.
        self.assertEqual(self.dwarf_count(), before + 3, r.stdout)
        self.assertEqual(self.read(p, 'attempt'), '5', r.stdout)

    def test_a_plain_retry_ends_what_a_decision_allowed(self):
        p, _ = self.converging_plan()
        self.retry(p, '--decide', 'retry=1', '--reason', 'one more', '--approved')
        self.assertEqual(self.read(p, 'decided.left'), '0')
        self.retry(p)
        self.assertFalse((p / 'tasks/a/decided.left').exists())

    def test_a_threshold_decision_writes_the_task_threshold_and_retries(self):
        p, _ = self.converging_plan()
        before = self.dwarf_count()
        r = self.retry(p, '--decide', 'threshold=P0', '--reason', 'P1s can wait', '--approved')
        self.assertEqual(self.dwarf_count(), before + 1, r.stdout)
        self.assertEqual(self.read(p, 'qa_threshold'), 'P0')
        self.assertEqual(self.decisions(p)[0][2], 'threshold=P0')
        self.assertTrue((p / 'tasks/a/merged').exists(), r.stdout)    # a P1 finding no longer blocks: the retry passes

    def test_a_named_model_is_a_decision_too(self):
        p, _ = self.converging_plan()
        before = self.dwarf_count()
        r = self.retry(p, '--dwarf', 'opus', '--reason', 'a stronger model', '--approved')
        self.assertEqual(self.dwarf_count(), before + 1, r.stdout)
        self.assertEqual(self.decisions(p)[0][2:], ['dwarf=opus', 'a stronger model'])
        self.assertIn('opus', (p / 'tasks.tsv').read_text())

    def test_a_named_model_with_a_count_is_one_decision(self):
        p, _ = self.converging_plan()
        before = self.dwarf_count()
        r = self.retry(p, '--decide', 'retry=2', '--dwarf', 'opus', '--reason', 'this model, two more', '--approved')
        self.assertEqual(self.dwarf_count(), before + 2, r.stdout)
        self.assertEqual(self.decisions(p)[0][2:], ['retry=2 dwarf=opus', 'this model, two more'])
        row = next(line for line in (p / 'tasks.tsv').read_text().splitlines() if line.startswith('a\t'))
        self.assertEqual(row.split('\t')[4], 'opus')
        # Both attempts ran on it: the policy may not move a task a person just put on a model.
        rows = [row.split('\t') for row in (p / 'tasks/a/attempts.tsv').read_text().splitlines()]
        self.assertEqual([row[-1] for row in rows[-2:]], ['opus', 'opus'], rows)

    def stopped_pair(self):
        """a and b stopped for a decision side by side, and c, blocked behind a."""
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\nb\t-\tlow\tb.txt\tsol\topus\tB\n'
                                     'c\ta\tlow\tc.txt\tsol\topus\tC\n')
        for i in 'abc':
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        (p / 'converge_window').write_text('2\n')
        table = {}
        for task in 'ab':
            table.update({'%s.qa.%d' % (task, n): {'verdict': 'FAIL', 'findings': MOVING[n - 1]} for n in (1, 2)})
            table.update({'%s.dwarf.%d' % (task, n): {'content': 'try %d' % n} for n in range(1, 9)})
        table['a.dwarf.3'] = table['b.dwarf.3'] = {'content': 'decided', 'sleep': 1}
        self.scenario(table)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5, '--max-parallel', 3)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual([self.read(p, 'status', t) for t in 'abc'], ['NEEDS_DECISION', 'NEEDS_DECISION', 'BLOCKED'], r.stdout)
        self.assertIn('--no-dispatch', r.stdout)                       # the summary says how to decide several
        return p

    def test_two_decisions_run_side_by_side_under_one_run(self):
        p = self.stopped_pair()
        for task, why in (('a', 'first reason'), ('b', 'second reason')):
            before = self.dwarf_count()
            r = self.run_script('forge-parallel.sh', 'retry', p, task, '--decide', 'retry=1', '--reason', why,
                                '--approved', '--no-dispatch')
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertEqual(self.dwarf_count(), before)               # recorded, and nothing started
            self.assertEqual(self.decisions_of(p, task)[0][2:], ['retry=1', why])
        self.env['EVENTS'] = str(self.root / 'events')
        r = self.run_script('forge-parallel.sh', 'run', p, '--max-parallel', 3)
        self.assertEqual(r.returncode, 0, r.stdout)
        for task in 'abc':
            self.assertTrue((p / 'tasks' / task / 'merged').exists(), r.stdout)
            self.assertFalse((p / 'tasks' / task / 'retry.prepared').exists())
        events = [line.split()[:2] for line in (self.root / 'events').read_text().splitlines()]
        self.assertEqual(sorted(events[:2]), [['a.txt', 'dwarf'], ['b.txt', 'dwarf']])   # neither waited for the other
        self.assertLess(events.index(['a.txt', 'qa']), events.index(['c.txt', 'dwarf']))  # c waited for its dependency only
        self.assertEqual(self.read(p, 'attempt', 'a'), '3')            # one more attempt, the one decided

    def test_no_dispatch_keeps_every_gate(self):
        p = self.stopped_pair()
        before = (p / 'tasks/a/status').read_text()
        for name, flags, code in (('no decision', [], 2), ('no reason', ['--decide', 'retry=1', '--approved'], 2),
                                  ('no approved', ['--decide', 'retry=1', '--reason', 'x'], 2)):
            with self.subTest(name):
                r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--no-dispatch', *flags)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                self.assertEqual((p / 'tasks/a/status').read_text(), before)
                self.assertFalse((p / 'tasks/a/retry.prepared').exists())
                self.assertFalse((p / 'tasks/a/decisions.tsv').exists())

    def test_a_retry_that_dispatches_clears_a_prepared_marker(self):
        p = self.stopped_pair()
        self.assertEqual(self.run_script('forge-parallel.sh', 'retry', p, 'a', '--decide', 'retry=1', '--reason', 'x',
                                         '--approved', '--no-dispatch').returncode, 0)
        self.assertEqual(self.read(p, 'attempt'), '2')
        self.assertTrue((p / 'tasks/a/retry.prepared').exists())
        self.assertEqual(self.run_script('forge-parallel.sh', 'retry', p, 'a').returncode, 0)
        self.assertFalse((p / 'tasks/a/retry.prepared').exists())

    def test_a_refused_preparation_puts_the_decision_back(self):
        p, _ = self.converging_plan()
        (p / 'tasks/a/escalation.stop').write_text('3\tsol\n')
        (p / 'tasks/a/split.suggested').write_text('split it\n')
        (p / 'tasks/a/escalation.pin').write_text('sol:high\n')
        (p / 'tasks/a/escalations.tsv').mkdir()                        # the pin's record cannot be written
        before = self.dwarf_count()
        r = self.retry(p, '--decide', 'threshold=P0', '--reason', 'try', '--approved')
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.read(p, 'status'), self.dwarf_count(), self.decisions(p)), ('NEEDS_DECISION', before, []))
        self.assertFalse((p / 'tasks/a/qa_threshold').exists())
        self.assertTrue((p / 'tasks/a/escalation.stop').exists())
        self.assertTrue((p / 'tasks/a/split.suggested').exists())

    def test_accept_takes_a_task_waiting_for_a_decision(self):
        p, _ = self.converging_plan()
        r = self.run_script('forge-parallel.sh', 'accept', p, 'a', '--reason', 'good enough', '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertIn('Accepted by a human despite the reviewer\'s FAIL verdict', (p / 'tasks/a/known_issues.md').read_text())


class SchedulerTests(control_fixtures.Base):
    """The stand-in worker, a task already waiting for a decision."""

    def waiting(self, name='x', stop=True):
        self.task(name, status='NEEDS_DECISION')
        (self.plan / 'tasks' / name / 'decision.md').write_text(
            'reasons: converging\n\nx stopped for your decision.\n')
        if stop:                                                       # the stand-in preparation stops on it
            (self.plan / 'tasks' / name / 'escalation.stop').write_text('3\tsol\n')

    def run_beside(self, during, budget=3):
        """A live scheduler with one other task held open while `during` runs; returns its stderr."""
        self.task('a')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/release"\n')
        proc = self.start('--retry-failed', budget)
        self.wait_for(lambda: (self.plan / 'tasks/a/calls').exists(), 'a to start')
        during()
        (self.plan / 'release').write_text('')
        _, err = proc.communicate(timeout=60)
        return err

    def test_a_budget_and_a_pin_do_not_resume_it(self):
        self.waiting()

        def during():
            self.assertEqual(self.control('set', self.plan, 'retry-budget', '9', '--task', 'x').returncode, 0)
            self.assertEqual(self.control('set', self.plan, 'retry-budget', '9').returncode, 0)
            self.assertEqual(self.control('pin', self.plan, 'x', 'sol:xhigh:codex').returncode, 0)
            time.sleep(2.5)                                            # past the scheduler's look at ended tasks

        self.run_beside(during)
        self.assertEqual(self.status('x'), 'NEEDS_DECISION')
        self.assertNotIn('x prepared', ' '.join(self.events()))
        self.assertFalse(any(e.startswith('x start') for e in self.events()))

    def test_control_retry_without_a_decision_is_refused(self):
        self.waiting()

        def during():
            result = self.control('retry', self.plan, 'x')
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertIn('waiting for your decision', result.stderr)
            self.assertIn('reasons: converging', result.stderr)
            self.assertEqual(self.requests(), [])
            # A request that was written by hand is refused by the scheduler all the same.
            name = '1-1.json'
            (self.plan / 'control.requests').mkdir(exist_ok=True)
            (self.plan / 'control.requests' / name).write_text(json.dumps(dict(verb='retry', task='x', dwarf=None, at=0, actor='cli')))
            self.wait_for(lambda: self.outcomes(), 'the outcome line')

        self.run_beside(during)
        [line] = self.outcomes()
        self.assertEqual((line['task'], line['outcome']), ('x', 'refused'))
        self.assertIn('x is waiting for your decision', line['reason'])
        self.assertIn('reasons: converging', line['reason'])
        self.assertEqual(self.status('x'), 'NEEDS_DECISION')
        self.assertNotIn('x prepared', ' '.join(self.events()))

    def test_control_refusals_of_a_decision(self):
        self.waiting()
        self.task('f', status='FAIL')
        self.live()
        for what, args in (('no reason', ('x', '--decide', 'retry=2', '--approved')),
                           ('no approval', ('x', '--decide', 'retry=2', '--reason', 'r')),
                           ('dwarf without approval', ('x', '--dwarf', 'sol:xhigh:codex', '--reason', 'r'))):
            with self.subTest(what):
                result = self.control('retry', self.plan, *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        result = self.control('retry', self.plan, 'f', '--decide', 'retry=2', '--reason', 'r', '--approved')
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('not waiting for a decision', result.stderr)
        self.assertEqual(self.requests(), [])

    def test_control_retry_with_a_decision_passes_it_to_the_preparation(self):
        self.waiting(stop=False)

        def during():
            result = self.control('retry', self.plan, 'x', '--decide', 'retry=2', '--reason', 'new findings are cheap',
                                  '--approved', '--json')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.wait_for(lambda: self.outcomes(), 'the outcome line')

        self.run_beside(during)
        [line] = self.outcomes()
        self.assertEqual((line['task'], line['outcome']), ('x', 'applied'))
        self.assertIn('x prepared --decide retry=2 --reason new findings are cheap --approved', self.events())
        self.assertIn('x start 1 trigger=human', self.events())

    def starts(self, name):
        return [e for e in self.events() if e.startswith(name + ' start')]

    def decide_live(self, choice, budget):
        self.task('x', outcomes=['FAIL'] * 6, status='NEEDS_DECISION')
        (self.plan / 'tasks/x/decision.md').write_text('reasons: converging\n')

        def during():
            result = self.control('retry', self.plan, 'x', '--decide', choice, '--reason', 'go on', '--approved')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.wait_for(lambda: self.outcomes(), 'the outcome line')

        self.run_beside(during, budget)

    def test_a_decision_of_one_attempt_is_not_extended_by_the_retry_budget(self):
        self.decide_live('retry=1', 3)
        self.assertEqual(len(self.starts('x')), 1, self.events())
        self.assertEqual(self.status('x'), 'FAIL')

    def test_a_decision_of_n_attempts_is_n_attempts_whatever_the_retry_budget(self):
        self.decide_live('retry=3', 0)
        self.assertEqual(len(self.starts('x')), 3, self.events())
        self.assertEqual(self.status('x'), 'FAIL')

    def test_control_retry_with_a_dwarf_needs_the_same_flags(self):
        self.waiting(stop=False)

        def during():
            result = self.control('retry', self.plan, 'x', '--dwarf', 'sol:xhigh:codex', '--reason', 'stronger',
                                  '--approved')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.wait_for(lambda: self.outcomes(), 'the outcome line')

        self.run_beside(during)
        self.assertIn('x prepared --dwarf sol:xhigh:codex --reason stronger --approved', self.events())

    def test_control_retry_can_name_a_model_and_a_count(self):
        self.waiting(stop=False)

        def during():
            result = self.control('retry', self.plan, 'x', '--decide', 'retry=2', '--dwarf', 'sol:xhigh:codex',
                                  '--reason', 'this model, two more', '--approved')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.wait_for(lambda: self.outcomes(), 'the outcome line')

        self.run_beside(during)
        self.assertIn('x prepared --dwarf sol:xhigh:codex --decide retry=2 --reason this model, two more --approved', self.events())


class RealPreparationTests(DecisionCase):
    def test_the_scheduler_entry_records_the_decision(self):
        p, _ = self.converging_plan()
        r = self.run_script('forge-parallel.sh', '_prepare_retry', p, 'a', '--decide', 'retry=2', '--reason', 'again',
                            '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.decisions(p)[0][2:], ['retry=2', 'again'])
        self.assertFalse((p / 'tasks/a/status').exists())              # the scheduler's entry clears it
        r = self.run_script('forge-parallel.sh', '_prepare_retry', p, 'a', '--decide', 'retry=2')
        self.assertEqual(r.returncode, 2, r.stdout)                    # no longer waiting for a decision


if __name__ == '__main__':
    unittest.main()
