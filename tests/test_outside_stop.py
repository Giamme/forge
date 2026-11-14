"""A task whose every blocking finding needs a file outside it stops as NEEDS_DECISION (`plan`, #149).

Run cases drive forge-parallel.sh with the fake agent CLI from test_forge.py, as test_needs_decision.py
does. The plan has two tasks: a declares a.txt, b declares b.txt and depends on a."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
from pathlib import Path
import json
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]


def finding(line, tag='', severity='P1', status='CONFIRMED'):
    return '- [%s][%s] %s — needs a change elsewhere%s' % (severity, status, line, (' ' + tag) if tag else '')


class OutsideStopCase(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self):
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\nb\ta\tlow\tb.txt\tsol\topus\tB\n')
        for i in 'ab':
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def run_b(self, findings, verdict='FAIL'):
        """b fails its first review with these findings; every later review passes."""
        p = self.plan_of()
        table = {'b.qa.1': {'verdict': verdict, 'findings': findings}}
        self.scenario(table)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 3)
        return p, r

    def read(self, p, name, task='b'):
        return (p / 'tasks' / task / name).read_text()

    def dwarf_calls(self):
        return self.calls().count('dwarf')

    def test_stop_after_the_first_attempt(self):
        p, _ = self.run_b(finding('a.txt:1', '{outside=a.txt}'))
        self.assertEqual(self.read(p, 'status').strip(), 'NEEDS_DECISION')
        self.assertEqual(self.dwarf_calls(), 2)  # a once, b once: no retry
        decision = self.read(p, 'decision.md')
        self.assertTrue(decision.startswith('reasons: plan'), decision)
        self.assertIn('a.txt — declared by a (MERGED)', decision)
        self.assertIn("add the path to this task's files", decision)
        self.assertIn('make this task depend on the owner', decision)
        self.assertIn('merge the two rows into one task', decision)
        self.assertIn('--decide retry=N', decision)  # the existing choices stay
        outside = json.loads(self.read(p, 'outside.json'))
        self.assertEqual(outside, [{'path': 'a.txt', 'owner': 'a', 'owners': ['a'], 'owner_status': 'MERGED'}])
        self.assertFalse((p / 'tasks/b/escalation.next').exists())
        self.assertFalse((p / 'tasks/b/escalation.stop').exists())

    def test_a_path_no_task_declares_stops(self):
        p, _ = self.run_b(finding('docs/x.md:1', '{outside=docs/x.md}'))
        self.assertEqual(self.read(p, 'status').strip(), 'NEEDS_DECISION')
        self.assertIn('docs/x.md — declared by no task', self.read(p, 'decision.md'))
        self.assertIsNone(json.loads(self.read(p, 'outside.json'))[0]['owner'])

    def test_a_partly_tagged_fail_retries(self):
        p, _ = self.run_b(finding('a.txt:1', '{outside=a.txt}') + '\n' + finding('b.txt:2'))
        self.assertNotEqual(self.read(p, 'status').strip(), 'NEEDS_DECISION')
        self.assertGreater(self.dwarf_calls(), 2)
        self.assertFalse((p / 'tasks/b/outside.json').exists())

    def test_a_tag_on_the_tasks_own_file_does_not_stop(self):
        p, _ = self.run_b(finding('b.txt:1', '{outside=b.txt}'))
        self.assertNotEqual(self.read(p, 'status').strip(), 'NEEDS_DECISION')
        self.assertGreater(self.dwarf_calls(), 2)
        self.assertFalse((p / 'tasks/b/outside.json').exists())

    def test_plausible_or_tolerated_findings_do_not_stop(self):
        for n, text in enumerate([finding('a.txt:1', '{outside=a.txt}', status='PLAUSIBLE'),
                                  finding('a.txt:1', '{outside=a.txt}', severity='P3')]):
            with self.subTest(n=n):
                self.setUp()
                p, _ = self.run_b(text)
                self.assertNotEqual(self.read(p, 'status').strip(), 'NEEDS_DECISION')
                self.assertFalse((p / 'tasks/b/outside.json').exists())

    def test_the_implementers_claim_reaches_the_review(self):
        p = self.plan_of()
        self.scenario({'b.dwarf.1': {'message': 'done\nFORGE_BLOCKED: a.txt: the helper lives there'},
                       'b.qa.1': {'verdict': 'PASS'}})
        self.run_script('forge-parallel.sh', 'run', p)
        text = self.read(p, 'qa.input')
        self.assertIn('The implementer says these fixes need files outside the task:', text)
        self.assertIn('  - a.txt: the helper lives there', text)
        self.assertIn('Confirm a real one with {outside=<path>}', text)

    def test_no_claim_no_section(self):
        p = self.plan_of()
        self.scenario({'b.qa.1': {'verdict': 'PASS'}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertNotIn('The implementer says these fixes need files outside the task', self.read(p, 'qa.input'))


if __name__ == '__main__':
    unittest.main()
