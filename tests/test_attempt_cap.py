"""The attempt cap (#69): a task's attempts across runs, its cap (`plan --max-attempts`, 8 by default), and the
person who raises it. Attempts used are the attempts.tsv attempts that were not infrastructure stops; the cap is
max_attempts plus every retry=N a person granted. A task at its cap is stopped for a decision (`cap`), and no
budget, pin or plain retry dispatches it.

CapCountTests call `forge-route.py attempt-cap` on a plan dir written by hand. The run cases drive forge-parallel.sh
with the fake agent CLI from test_forge.py, as test_needs_decision.py does."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / 'scripts' / 'forge-route.py'
CONTROL = ROOT / 'scripts' / 'forge-control.py'
SAME = '- [P1][CONFIRMED] a.txt:1 — broken'


def attempt_row(number, status):
    return '1700000000\t%d\t%s\t\tsol\n' % (number, status)


class CapCountTests(unittest.TestCase):
    """The count and the cap, from the files of one plan dir (no run)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan = Path(tmp.name)
        (self.plan / 'tasks' / 'a').mkdir(parents=True)

    def attempts(self, *rows):
        (self.plan / 'tasks' / 'a' / 'attempts.tsv').write_text(''.join(rows))

    def cap(self):
        result = subprocess.run([sys.executable, str(ROUTE), 'attempt-cap', '--plan', str(self.plan), '--task', 'a'],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_a_plan_without_the_file_caps_at_eight(self):
        self.attempts(attempt_row(1, 'FAIL'), attempt_row(2, 'FAIL'), attempt_row(3, 'FAIL'))
        self.assertEqual(self.cap(), ['3', '8'])

    def test_zero_means_no_cap_and_grants_do_not_make_one(self):
        (self.plan / 'max_attempts').write_text('0\n')
        self.attempts(attempt_row(1, 'FAIL'), attempt_row(2, 'FAIL'), attempt_row(3, 'FAIL'))
        (self.plan / 'tasks' / 'a' / 'decisions.tsv').write_text('1700000001\t3\tretry=2\tmore\n')
        self.assertEqual(self.cap(), ['3', '-'])

    def test_the_file_sets_the_cap_and_an_unreadable_one_is_the_default(self):
        self.attempts(attempt_row(1, 'FAIL'), attempt_row(2, 'FAIL'), attempt_row(3, 'FAIL'))
        (self.plan / 'max_attempts').write_text('2\n')
        self.assertEqual(self.cap(), ['3', '2'])
        (self.plan / 'max_attempts').write_text('lots\n')
        self.assertEqual(self.cap(), ['3', '8'])

    def test_infrastructure_stops_spend_no_attempt(self):
        self.attempts(attempt_row(1, 'FAIL'), attempt_row(2, 'INFRA'), attempt_row(2, 'FAIL'), attempt_row(3, 'INFRA'))
        self.assertEqual(self.cap(), ['2', '8'])

    def test_a_qa_only_resume_is_not_a_second_attempt(self):
        # A review with no verdict, then the same attempt's review again: one attempt, two rows.
        self.attempts(attempt_row(1, 'UNKNOWN'), attempt_row(1, 'FAIL'))
        self.assertEqual(self.cap(), ['1', '8'])

    def test_each_retry_grant_adds_its_n_and_other_choices_add_nothing(self):
        self.attempts(attempt_row(1, 'FAIL'), attempt_row(2, 'FAIL'))
        (self.plan / 'tasks' / 'a' / 'decisions.tsv').write_text(
            '1700000001\t2\tretry=2\tnew findings are cheap\n'
            '1700000002\t4\tthreshold=P1\tone more\n'
            '1700000003\t4\tretry=3\tand three\n')
        self.assertEqual(self.cap(), ['2', '13'])


class CapCase(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, *flags):
        """One task, a, that fails every review with the same finding (no convergence, no split)."""
        p = self.root / 'plan'
        p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks' / 'a').mkdir(parents=True)
        (p / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        (p / 'escalate_after').write_text('0\n')   # the tier policy stays out of it: only the cap can stop the task
        return p

    def failing(self):
        table = {'a.qa.%d' % n: {'verdict': 'FAIL', 'findings': SAME} for n in range(1, 9)}
        table.update({'a.dwarf.%d' % n: {'content': 'try %d' % n} for n in range(1, 12)})
        self.scenario(table)

    def read(self, plan, name):
        return (plan / 'tasks' / 'a' / name).read_text().strip()

    def first_line(self, plan):
        return (plan / 'tasks' / 'a' / 'decision.md').read_text().splitlines()[0]

    def decisions(self, plan):
        path = plan / 'tasks' / 'a' / 'decisions.tsv'
        return [line.split('\t') for line in path.read_text().splitlines()] if path.exists() else []

    def dwarf_count(self):
        return self.calls().count('dwarf')

    def retry(self, plan, *args):
        return self.run_script('forge-parallel.sh', 'retry', plan, 'a', *args)

    def control(self, *args):
        env = dict(self.env, FORGE_DATASET='off')
        return subprocess.run([sys.executable, str(CONTROL), *map(str, args)], capture_output=True, text=True,
                              env=env, timeout=60)


class CapRunTests(CapCase):
    def test_the_cap_holds_across_runs(self):
        p = self.plan_of('--max-attempts', 2)
        self.failing()
        self.run_script('forge-parallel.sh', 'run', p)                          # attempt 1
        self.assertEqual((self.read(p, 'status'), self.dwarf_count()), ('FAIL', 1))
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)   # attempt 2, then the cap
        self.assertEqual(self.read(p, 'status'), 'NEEDS_DECISION', r.stdout)
        self.assertEqual(self.first_line(p), 'reasons: cap')
        self.assertIn('Attempts used so far: 2 (attempts 2 of 2)', (p / 'tasks/a/decision.md').read_text())
        self.assertIn('waiting for your decision:\n  a: cap — ', r.stdout)
        self.assertIn('      attempts 2/2\n', r.stdout)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)   # not a third attempt
        self.assertEqual((self.read(p, 'status'), self.dwarf_count()), ('NEEDS_DECISION', 2), r.stdout)

    def test_a_budget_cannot_lift_a_task_at_its_cap(self):
        p = self.plan_of()
        self.failing()
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.dwarf_count(), 1)
        (p / 'max_attempts').write_text('1\n')                                  # attempt 1 has spent the cap
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual(self.dwarf_count(), 1, r.stdout)
        self.assertEqual(self.read(p, 'status'), 'NEEDS_DECISION')
        self.assertEqual(self.first_line(p), 'reasons: cap')

    def test_a_plain_retry_at_the_cap_is_refused_with_the_decision_commands(self):
        p = self.plan_of()
        self.failing()
        self.run_script('forge-parallel.sh', 'run', p)
        (p / 'max_attempts').write_text('1\n')
        r = self.retry(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('at its attempt cap (attempts 1 of 1)', r.stdout)
        self.assertIn('--decide retry=N --reason', r.stdout)
        self.assertEqual((self.read(p, 'status'), self.dwarf_count(), self.decisions(p)), ('NEEDS_DECISION', 1, []))

    def test_retry_n_at_the_cap_allows_exactly_n_more_attempts(self):
        p = self.plan_of('--max-attempts', 2)
        self.failing()
        self.run_script('forge-parallel.sh', 'run', p)
        self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual((self.read(p, 'status'), self.dwarf_count()), ('NEEDS_DECISION', 2))
        r = self.retry(p, '--decide', 'retry=2', '--reason', 'the findings are cheap', '--approved')
        self.assertEqual(self.dwarf_count(), 4, r.stdout)                       # two more, then the new cap
        self.assertEqual(self.decisions(p)[0][2:], ['retry=2', 'the findings are cheap'])
        self.assertEqual((self.read(p, 'status'), self.first_line(p)), ('NEEDS_DECISION', 'reasons: cap'))
        self.assertIn('Attempts used so far: 4 (attempts 4 of 4)', (p / 'tasks/a/decision.md').read_text())

    def test_a_decision_that_would_not_grant_its_n_is_refused(self):
        p = self.plan_of()
        self.failing()
        self.run_script('forge-parallel.sh', 'run', p)
        self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)     # attempt 2, FAIL, under the cap of 8
        self.assertEqual((self.read(p, 'status'), self.dwarf_count()), ('FAIL', 2))
        (p / 'max_attempts').write_text('1\n')                                  # two attempts spent against a cap of one
        self.assertEqual(self.retry(p).returncode, 2)                           # a plain retry stops it for a decision
        r = self.retry(p, '--decide', 'retry=1', '--reason', 'one more', '--approved')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('retry=2 grants 1', r.stdout)                             # with retry=2 there is one attempt
        self.assertEqual((self.dwarf_count(), self.decisions(p)), (2, []))

    def test_zero_is_no_cap_at_run_level(self):
        p = self.plan_of('--max-attempts', 0)
        self.failing()
        self.run_script('forge-parallel.sh', 'run', p)
        self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual((self.read(p, 'status'), self.dwarf_count()), ('FAIL', 2))
        self.assertFalse((p / 'tasks/a/decision.md').exists())


class CapControlTests(CapCase):
    def test_raising_the_cap_needs_confirm(self):
        p = self.plan_of()
        r = self.control('set', p, 'max-attempts', 4)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("a person's decision", r.stderr)
        self.assertFalse((p / 'max_attempts').exists())
        r = self.control('set', p, 'max-attempts', 4, '--confirm')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((p / 'max_attempts').read_text(), '4\n')

    def test_confirm_is_for_max_attempts_only(self):
        p = self.plan_of()
        self.assertEqual(self.control('set', p, 'timeout', 5, '--confirm').returncode, 2)

    def test_show_prints_attempts_used_over_cap(self):
        p = self.plan_of()
        self.failing()
        self.run_script('forge-parallel.sh', 'run', p)
        shown = self.control('show', p)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertRegex(shown.stdout, r'\n  task\s.*\battempts\b')
        self.assertRegex(shown.stdout, r'\n  a\s+FAIL\s.*\s1/8\s')
        (p / 'max_attempts').write_text('0\n')
        self.assertRegex(self.control('show', p).stdout, r'\n  a\s+FAIL\s.*\s1/-\s')


if __name__ == '__main__':
    unittest.main()
