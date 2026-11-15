"""`plan --retry-per-model N` as a plan option, and the scheduler honouring it (#141).

The route itself (N failures per rung, `per-model`, `worst-case`) is covered in
test_retry_per_model.py. This file is the flag, the table, the option lists, and a run.
Fixtures are borrowed rather than inherited: subclassing ForgeTests would run all of its
tests again under this class.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / 'scripts' / 'forge-route.py'
FINDING = '- [P1][CONFIRMED] a.txt:1 — still broken'
# luna < sonnet < opus, one distinct model per tier (registry aliases the fake CLIs resolve).
TIERS = ('--dwarf-low', 'luna', '--dwarf-medium', 'sonnet', '--dwarf-high', 'opus',
         '--explore', '0', '--converge-window', '99')
RETRY_LINE = ('retry per model: %s (a task may fail %s times on each model the retry policy '
              'gives it, then it moves a rung up)')


class RetryPerModelRunTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

    def write_tasks(self, plan, rows):
        plan.mkdir(parents=True)
        (plan / 'tasks.tsv').write_text(''.join('%s\t%s\t%s\t%s\t%s\t%s\t%s\n' % row for row in rows))
        for row in rows:
            task = plan / 'tasks' / row[0]
            task.mkdir(parents=True)
            (task / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + row[0])

    def plan_cmd(self, plan, *flags):
        return self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory',
                                *TIERS, *flags)

    def planned(self, rows=None, name='plan', flags=()):
        """One low task on luna unless `rows` says otherwise. Returns (plan, plan's stdout)."""
        rows = rows or [('a', '-', 'low', 'a.txt', 'luna', 'opus', 'A')]
        plan = self.root / name
        self.write_tasks(plan, rows)
        result = self.plan_cmd(plan, *flags)
        self.assertEqual(result.returncode, 0, result.stdout)
        return plan, result

    def failing(self, task='a', calls=6):
        """Every review FAILs with the same finding, and each dwarf attempt writes new bytes.

        Identical findings cannot trip the converging signal (nothing in the window is new), and a
        changed diff is what lets the next retry past the byte-identical refusal. `calls` is how
        many distinct dwarf bodies to prepare; the fake numbers them per task."""
        finding = FINDING if task == 'a' else FINDING.replace('a.txt', task + '.txt')
        table = {'%s.qa.*' % task: {'verdict': 'FAIL', 'findings': finding}}
        table.update({'%s.dwarf.%d' % (task, n): {'content': 'try %d' % n} for n in range(1, calls + 1)})
        self.scenario(table)

    def spent(self, plan, task='a'):
        text = (plan / 'tasks' / task / 'attempts.tsv').read_text().splitlines()
        return [(cells[1], cells[2], cells[4]) for cells in (line.split('\t') for line in text)]

    def test_the_flag_writes_the_file_and_the_table_shows_the_worst_case(self):
        plan, result = self.planned(flags=('--retry-per-model', '2'))
        self.assertEqual((plan / 'retry_per_model').read_text(), '2\n')
        # One low task, three rungs, N=2, no shadow: 6 implementer dispatches and 6 reviews.
        # 6 attempts is under the default cap of 8, so the cap sentence stays absent.
        self.assertIn(RETRY_LINE % ('2', '2'), result.stdout)
        self.assertIn('worst case with retry-per-model 2: 12 dispatches', result.stdout)
        self.assertNotIn('stops these tasks before the top of their ladder', result.stdout)

    def test_a_value_outside_1_to_99_exits_2_and_writes_nothing(self):
        for bad in ('0', 'abc'):
            with self.subTest(bad=bad):
                plan = self.root / ('bad-' + bad)
                self.write_tasks(plan, [('a', '-', 'low', 'a.txt', 'luna', 'opus', 'A')])
                result = self.plan_cmd(plan, '--retry-per-model', bad)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('--retry-per-model needs an integer from 1 to 99', result.stdout)
                self.assertFalse((plan / 'retry_per_model').exists())

    def test_no_retry_per_model_removes_the_file_and_a_replan_keeps_it(self):
        plan, first = self.planned(flags=('--retry-per-model', '2'))
        self.assertIn('retry per model: 2 ', first.stdout)
        again = self.plan_cmd(plan)
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual((plan / 'retry_per_model').read_text(), '2\n')
        self.assertIn('worst case with retry-per-model 2: 12 dispatches', again.stdout)
        removed = self.plan_cmd(plan, '--no-retry-per-model')
        self.assertEqual(removed.returncode, 0, removed.stdout)
        self.assertFalse((plan / 'retry_per_model').exists())
        self.assertNotIn('retry per model:', removed.stdout)
        self.assertNotIn('worst case with retry-per-model', removed.stdout)

    def test_the_table_says_nothing_without_the_file(self):
        _plan, result = self.planned()
        self.assertNotIn('retry per model:', result.stdout)
        self.assertNotIn('worst case with retry-per-model', result.stdout)
        self.assertNotIn('stops these tasks before the top of their ladder', result.stdout)

    def test_the_file_is_in_both_option_lists(self):
        # The name in these two lists is what split and `forge runs rerun` copy. Combine has its
        # own loop; the tests below run that path.
        libs = (ROOT / 'scripts' / 'forge-lib-plans.sh').read_text()
        plans = re.search(r'^PLANS_OPTION_FILES="([^"]*)"', libs, re.M).group(1).split()
        self.assertIn('retry_per_model', plans)
        compare = (ROOT / 'scripts' / 'forge-compare.py').read_text()
        names = eval(re.search(r'OPTION_FILES = (\(.*?\))\n', compare, re.S).group(1))
        self.assertIn('retry_per_model', names)

    def test_split_carries_the_file(self):
        plan, _ = self.planned(rows=[('a', '-', 'low', 'a.txt', 'luna', 'opus', 'A'),
                                      ('b', '-', 'low', 'b.txt', 'luna', 'opus', 'B')],
                                flags=('--retry-per-model', '2'))
        self.failing('b', calls=1)
        result = self.run_script('forge-parallel.sh', 'run', plan, '--retry-failed', '0')
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertTrue((plan / 'tasks/a/merged').exists())
        self.assertEqual((plan / 'tasks/b/status').read_text().strip(), 'FAIL')
        new = self.root / 'second'
        result = self.run_script('forge-parallel.sh', 'split', plan, new, '--tasks', 'b')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((new / 'retry_per_model').read_text(), '2\n')
        self.assertEqual((plan / 'retry_per_model').read_text(), '2\n')
        self.assertTrue((plan / 'tasks/a').is_dir())
        self.assertFalse((plan / 'tasks/b').exists())

    def test_combine_carries_the_file_when_every_source_agrees(self):
        plan1, _ = self.planned(rows=[('a', '-', 'low', 'a.txt', 'luna', 'opus', 'A')],
                                 name='plan1', flags=('--retry-per-model', '2'))
        plan2, _ = self.planned(rows=[('b', '-', 'low', 'b.txt', 'luna', 'opus', 'B')],
                                 name='plan2', flags=('--retry-per-model', '2'))
        for source in (plan1, plan2):
            result = self.run_script('forge-parallel.sh', 'run', source)
            self.assertEqual(result.returncode, 0, result.stdout)
        new = self.root / 'combined'
        result = self.run_script('forge-parallel.sh', 'combine', new, plan1, plan2)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((new / 'retry_per_model').read_text(), '2\n')

    def test_rerun_carries_the_file(self):
        plan, _ = self.planned(flags=('--retry-per-model', '2'))
        result = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(result.returncode, 0, result.stdout)
        result = subprocess.run([str(ROOT / 'forge'), 'runs', 'rerun', str(plan), '--dwarf', 'opus'],
                                cwd=self.root, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0, result.stdout)
        # rerun copies the file and then plans again without the flag; a re-plan must keep it.
        self.assertEqual((plan.with_name('plan-opus') / 'retry_per_model').read_text(), '2\n')

    def test_a_failing_task_gets_two_attempts_on_each_rung_then_stops(self):
        plan, _ = self.planned(flags=('--retry-per-model', '2'))
        self.failing()
        result = self.run_script('forge-parallel.sh', 'run', plan, '--retry-failed', '1')
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertEqual(self.calls().count('dwarf'), 6)
        self.assertEqual(self.spent(plan), [
            ('1', 'FAIL', 'luna'), ('2', 'FAIL', 'luna'),
            ('3', 'FAIL', 'sonnet'), ('4', 'FAIL', 'sonnet'),
            ('5', 'FAIL', 'opus'), ('6', 'FAIL', 'opus')])
        self.assertIn('escalating from luna to sonnet', result.stdout)
        self.assertIn('escalating from sonnet to opus', result.stdout)
        # The count in the line is the failures on that rung before the retry was prepared,
        # and the rung is the one it was failing on, not the one the retry moves to.
        for line in ('automatic retry 1, 1 of 2 failed on luna',
                     'automatic retry 2, 2 of 2 failed on luna',
                     'automatic retry 3, 1 of 2 failed on sonnet',
                     'automatic retry 4, 2 of 2 failed on sonnet',
                     'automatic retry 5, 1 of 2 failed on opus'):
            self.assertIn(line, result.stdout)
        self.assertNotIn('automatic retry 6', result.stdout)
        self.assertNotIn('replaces --escalate-after', result.stdout)

    def test_retry_failed_zero_makes_one_attempt(self):
        plan, _ = self.planned(flags=('--retry-per-model', '2'))
        self.failing(calls=2)
        result = self.run_script('forge-parallel.sh', 'run', plan, '--retry-failed', '0')
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertEqual(self.calls().count('dwarf'), 1)
        self.assertEqual(self.spent(plan), [('1', 'FAIL', 'luna')])
        self.assertNotIn('automatic retry', result.stdout)

    def test_a_decision_retries_once_when_per_model_says_no(self):
        # On the top rung, two failures make per-model say no. A bare decided.left cannot get an
        # attempt out: prepare_retry still asks the route, and with nothing held on this model the
        # route exits 3 (no stronger rung) and the scheduler drops the retry. A decisions.tsv row
        # of retry=N dwarf=<current> holds the route off that stop; decided.left is what limits
        # the scheduler to one attempt. The first run only records a real failure and a worktree.
        plan, _ = self.planned(rows=[('a', '-', 'high', 'a.txt', 'opus', 'opus', 'A')],
                                flags=('--retry-per-model', '2'))
        self.scenario({'a.qa.*': {'verdict': 'FAIL', 'findings': FINDING},
                       'a.dwarf.1': {'content': 'one'}, 'a.dwarf.2': {'content': 'two'}})
        result = self.run_script('forge-parallel.sh', 'run', plan, '--retry-failed', '0')
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertEqual(self.calls().count('dwarf'), 1)
        self.assertEqual((plan / 'tasks/a/status').read_text().strip(), 'FAIL')
        self.assertFalse((plan / 'tasks/a/escalation.stop').exists())
        log = plan / 'tasks/a/attempts.tsv'
        cells = log.read_text().splitlines()[0].split('\t')
        self.assertEqual((cells[1], cells[2], cells[4]), ('1', 'FAIL', 'opus'))
        cells[1] = '2'
        log.write_text(log.read_text() + '\t'.join(cells) + '\n')
        (plan / 'tasks/a/attempt').write_text('2\n')
        (plan / 'tasks/a/decisions.tsv').write_text('1700000000\t0\tretry=5 dwarf=opus\tagain\n')
        (plan / 'tasks/a/decided.left').write_text('1\n')
        asked = subprocess.run([sys.executable, str(ROUTE), 'per-model', '--plan', str(plan), '--task', 'a'],
                               capture_output=True, text=True, env=self.env)
        self.assertEqual(asked.returncode, 0, asked.stderr)
        self.assertIs(json.loads(asked.stdout)['retry'], False)
        result = self.run_script('forge-parallel.sh', 'run', plan, '--retry-failed', '0')
        self.assertEqual(self.calls().count('dwarf'), 2, result.stdout)
        self.assertIn('decided retry', result.stdout)
        self.assertNotIn('automatic retry', result.stdout)
        self.assertEqual((plan / 'tasks/a/decided.left').read_text().strip(), '0')

    def test_the_table_names_a_task_the_attempt_cap_cuts_short(self):
        # N times the three rungs is 6, and the cap is 3, so the task cannot reach the top.
        _plan, result = self.planned(flags=('--retry-per-model', '2', '--max-attempts', '3'))
        self.assertIn('worst case with retry-per-model 2: 12 dispatches', result.stdout)
        self.assertIn('max-attempts 3 stops these tasks before the top of their ladder: a '
                      '(plan --max-attempts N raises it)', result.stdout)

    def test_run_notes_once_that_retry_per_model_replaces_escalate_after(self):
        plan, _ = self.planned(flags=('--retry-per-model', '2'))
        result = self.run_script('forge-parallel.sh', 'run', plan, '--escalate-after', '2')
        self.assertEqual(result.returncode, 0, result.stdout)
        phrase = 'retry-per-model 2 replaces --escalate-after for this plan'
        self.assertEqual(result.stdout.count(phrase), 1)


if __name__ == '__main__':
    unittest.main()
