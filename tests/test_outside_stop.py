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
        # The plan section and the choices keep the blank line the other decision reasons have.
        self.assertRegex(decision, r'Attempts used so far: .*\n\nFiles outside this task:\n')
        self.assertRegex(decision, r'\n\nChoices \(each needs your reason')
        outside = json.loads(self.read(p, 'outside.json'))
        self.assertEqual(outside, [{'path': 'a.txt', 'owner': 'a', 'owners': ['a'], 'owner_status': 'MERGED'}])
        self.assertFalse((p / 'tasks/b/escalation.next').exists())
        self.assertFalse((p / 'tasks/b/escalation.stop').exists())

    def test_a_decision_that_cannot_be_written_does_not_escalate(self):
        # decision.md is written through .decision.md.tmp. A directory there makes the write fail
        # after the review is in the ledger. Falling through would append that review again and,
        # with --escalate-after 1, leave the escalation marker this stop exists to prevent.
        p = self.plan_of()
        (p / 'tasks' / 'b' / '.decision.md.tmp').mkdir()
        self.scenario({'b.qa.1': {'verdict': 'FAIL', 'findings': finding('a.txt:1', '{outside=a.txt}')}})
        self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 0, '--escalate-after', 1)
        self.assertEqual(self.read(p, 'status').strip(), 'FAIL')
        self.assertFalse((p / 'tasks' / 'b' / 'decision.md').exists())
        self.assertFalse((p / 'tasks' / 'b' / 'escalation.next').exists())
        self.assertFalse((p / 'tasks' / 'b' / 'escalation.stop').exists())
        self.assertEqual(len((p / 'tasks' / 'b' / 'ledger.tsv').read_text().splitlines()), 1)
        self.assertEqual(self.dwarf_calls(), 2)  # a once, b once: the failed stop is not a retry

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


class WaitLineTests(OutsideStopCase):
    """#193: `run` says when a task stops for the user, while the other tasks still run."""

    def test_the_line_is_printed_while_another_task_runs(self):
        import subprocess, time
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\nb\t-\tlow\tb.txt\tsol\topus\tB\n')
        for i in 'ab':
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        # a stops for a decision at its first review; b's implementer is still at work for a while.
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': finding('docs/x.md:1', '{outside=docs/x.md}')},
                       'b.dwarf.1': {'sleep': 25}, 'b.qa.*': {'verdict': 'PASS'}})
        out = self.root / 'run.out'
        with open(out, 'w') as stream:
            proc = subprocess.Popen(['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'run', str(p)], cwd=self.repo,
                                    env=self.env, text=True, stdout=stream, stderr=subprocess.STDOUT)
        try:
            want = 'forge: needs you: a NEEDS_DECISION (plan) -> %s' % (p / 'tasks' / 'a' / 'decision.md')
            deadline = time.time() + 120
            while time.time() < deadline and want not in out.read_text() and proc.poll() is None:
                time.sleep(0.2)
            self.assertIn(want, out.read_text())
            self.assertEqual((p / 'tasks' / 'b' / 'status').read_text().strip(), 'RUNNING')
            self.assertIsNone(proc.poll())                       # the run goes on
        finally:
            proc.wait(timeout=300)
        self.assertEqual(out.read_text().count('forge: needs you: a '), 1)
        self.assertNotIn('forge: needs you: b ', out.read_text())

    def test_each_waiting_status_has_its_line(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('forge_schedule_wait', ROOT / 'scripts' / 'forge-schedule.py')
        schedule = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(schedule)
        t = self.root / 'plan' / 'tasks' / 'x'
        t.mkdir(parents=True)
        (t / 'decision.md').write_text('reasons: converging, attempt-cap\n\nmore\n')
        (t / 'infra.txt').write_text('class=rate_limit\n')
        line = schedule.waiting_line
        self.assertEqual(line(t.parent.parent, 'x', 'NEEDS_DECISION'),
                         'forge: needs you: x NEEDS_DECISION (converging, attempt-cap) -> %s' % (t / 'decision.md'))
        self.assertEqual(line(t.parent.parent, 'x', 'NEEDS_INPUT'),
                         'forge: needs you: x NEEDS_INPUT (a question to answer) -> %s' % (t / 'needs_input.md'))
        self.assertEqual(line(t.parent.parent, 'x', 'INFRA'),
                         'forge: stopped: x INFRA (rate_limit, no attempt spent) -> %s' % (t / 'infra.txt'))
        (t / 'decision.md').unlink()
        self.assertIn('(see decision.md)', line(t.parent.parent, 'x', 'NEEDS_DECISION'))
        for state in ('FAIL', 'PASS', 'RUNNING', 'MERGED'):
            self.assertIsNone(line(t.parent.parent, 'x', state))


if __name__ == '__main__':
    unittest.main()
