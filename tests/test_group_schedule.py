"""forge-parallel.sh run drives group review (#140): the scheduler starts a group's review once every member is
IMPLEMENTED, merges a PASS, sends the implicated members back on a FAIL, and resumes what a stop left.

Driven like test_group_member and test_group_review: `plan` and `run` on a fixture repo with the fake agent CLI of
test_forge. The group's QA reply is scripted per call through the fake's SCENARIO table: the prompt of a group's
review names TASK_a first, so its calls are `a.qa.<n>`; a task outside the group answers under its own name."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest

from test_group_review import GroupCase, P1_B

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
HEADER = '# id\treview\tqa\tmembers\ttitle\tgoal\n'
GROUP = 'g1\tgroup\t-\ta,b,c\tThree\tDo all three\n'
A, B, C = ('%s\t-\tlow\t%s.txt\tsol\topus\t%s' % (t, t, t.upper()) for t in 'abc')
D = 'd\ta\tlow\td.txt\tsol\topus\tD'                # after a member, outside the group
E = 'e\t-\tlow\tchange.txt\tsol\topus\tE'           # unrelated: the fake names its files `change`
P1_NOWHERE = '- [P1][CONFIRMED] elsewhere/x.py:4 — breaks something {reach=main origin=new evidence=traced}'


class ScheduleCase(GroupCase):
    def setUp(self):
        super().setUp()
        self.events_file = self.root / 'events'
        self.env['EVENTS'] = str(self.events_file)

    def plan_of(self, rows, groups=GROUP, name='plan', flags=()):
        p = self.root / name
        p.mkdir()
        (p / 'tasks.tsv').write_text('\n'.join(rows) + '\n')
        for row in rows:
            task = row.split('\t')[0]
            (p / 'tasks' / task).mkdir(parents=True)
            (p / 'tasks' / task / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + task)
        if groups:
            (p / 'groups.tsv').write_text(HEADER + groups)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def trail(self):
        """(file, role, time) of every dispatch the fake harness saw, in order."""
        if not self.events_file.exists():
            return []
        return [(f, role, float(t)) for f, role, t in (line.split() for line in self.events_file.read_text().splitlines())]

    def dispatches(self, file, role):
        return [t for f, r, t in self.trail() if (f, r) == (file, role)]

    def run_plan(self, p, *flags):
        return self.run_script('forge-parallel.sh', 'run', p, *flags)

    def states(self, p, tasks='abc'):
        return {t: self.read(p, t, 'status') for t in tasks}

    def mtime(self, path):
        return path.stat().st_mtime


class ReviewTests(ScheduleCase):
    def test_one_review_merges_the_group_before_its_dependent_starts_and_an_unrelated_task_runs_meanwhile(self):
        p = self.plan_of([A, B, C, D, E])
        # e's implementer outlasts the members', so it runs while the group is reviewed; the review outlasts e.
        self.scenario({'change.dwarf.1': {'sleep': 3}, 'a.qa.1': {'sleep': 6}})
        r = self.run_plan(p, '--max-parallel', '4')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.states(p, 'abcde'), dict.fromkeys('abcde', 'PASS'), r.stdout)
        for task in 'abcde':
            self.assertTrue((p / 'tasks' / task / 'merged').exists(), task)
        self.assertEqual(len(self.dispatches('a.txt', 'qa')), 1, r.stdout)       # exactly one group QA dispatch
        self.assertEqual([self.dispatches(t + '.txt', 'qa') for t in 'bc'], [[], []])   # and none of a member's own
        self.assertIn('forge: reviewing group g1 (a, b, c)', r.stdout)
        self.assertEqual(self.status(p), 'PASS')
        group_merged = self.mtime(p / 'groups' / 'g1' / 'merged')
        review = self.dispatches('a.txt', 'qa')[0]
        self.assertGreaterEqual(review, max(self.dispatches(t + '.txt', 'dwarf')[0] for t in 'abc'))
        # the dependent starts only after the group merged
        self.assertGreaterEqual(self.dispatches('d.txt', 'dwarf')[0], group_merged)
        # the unrelated task started before the review, merged while it ran, and did not wait for the group
        e_merged = self.mtime(p / 'tasks' / 'e' / 'merged')
        self.assertLess(self.dispatches('change.txt', 'dwarf')[0], review)
        self.assertTrue(review < e_merged < group_merged, (review, e_merged, group_merged))
        # every member's merge is the group's: one merge of the group, not one per member
        subjects = subprocess.check_output(['git', '-C', str(Path((p / 'wt_root').read_text().strip()) / '_integration'),
                                            'log', '--format=%s'], text=True).splitlines()
        self.assertEqual([s for s in subjects if s.startswith('forge: merge group')], ['forge: merge group g1 (a,b,c)'])

    def test_the_group_review_starts_without_waiting_for_a_wave(self):
        # e is slow and unrelated; the group is reviewed while it runs, not after it
        p = self.plan_of([A, B, C, E])
        self.scenario({'change.dwarf.1': {'sleep': 4}})
        self.assertEqual(self.run_plan(p, '--max-parallel', '4').returncode, 0)
        e_done = self.mtime(p / 'tasks' / 'e' / 'merged')
        self.assertLess(self.mtime(p / 'groups' / 'g1' / 'merged'), e_done)

    def test_a_selective_retry_runs_only_the_implicated_member_and_reviews_again(self):
        p = self.plan_of([A, B, C])
        self.scenario({'a.qa.1': {'findings': P1_B, 'verdict': 'FAIL'}, 'b.dwarf.2': {'content': 'fixed'}})
        r = self.run_plan(p, '--retry-failed', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.states(p), dict.fromkeys('abc', 'PASS'), r.stdout)
        self.assertEqual({t: self.read(p, t, 'attempt') for t in 'abc'}, {'a': '1', 'b': '2', 'c': '1'})
        self.assertEqual({t: len(self.dispatches(t + '.txt', 'dwarf')) for t in 'abc'}, {'a': 1, 'b': 2, 'c': 1})
        self.assertEqual(len(self.dispatches('a.txt', 'qa')), 2)                 # two group QA dispatches in total
        self.assertEqual(self.gread(p, 'round').strip(), '2')
        self.assertIn('forge: retrying b (automatic retry 1/1)', r.stdout)
        self.assertNotIn('retrying a', r.stdout)
        self.assertEqual((p / 'tasks' / 'b' / 'retries.used').read_text().strip(), '1')
        self.assertFalse((p / 'tasks' / 'a' / 'retries.used').exists())
        self.assertTrue((p / 'groups' / 'g1' / 'merged').exists())

    def test_a_fail_without_a_retry_budget_leaves_the_implicated_member_failed_and_the_others_waiting(self):
        p = self.plan_of([A, B, C])
        self.scenario({'a.qa.1': {'findings': P1_B, 'verdict': 'FAIL'}})
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.states(p), {'a': 'IMPLEMENTED', 'b': 'FAIL', 'c': 'IMPLEMENTED'}, r.stdout)
        self.assertEqual(len(self.dispatches('a.txt', 'qa')), 1)
        self.assertIn('waiting for group g1 (FAIL)', r.stdout)
        # the next run, with a budget, retries b alone and reviews the group again
        self.scenario({'b.dwarf.2': {'content': 'fixed'}})                         # call counts survive the run
        r = self.run_plan(p, '--retry-failed', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual({t: self.read(p, t, 'attempt') for t in 'abc'}, {'a': '1', 'b': '2', 'c': '1'})
        self.assertTrue((p / 'groups' / 'g1' / 'merged').exists())

    def test_unknown_is_reviewed_again_within_the_groups_budget_and_spends_no_member_attempt(self):
        p = self.plan_of([A, B, C])
        self.scenario({'a.qa.1': {'findings': 'I looked at it.', 'verdict': 'none'}})
        r = self.run_plan(p, '--retry-failed', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(len(self.dispatches('a.txt', 'qa')), 2)
        self.assertEqual({t: len(self.dispatches(t + '.txt', 'dwarf')) for t in 'abc'}, dict.fromkeys('abc', 1))
        self.assertEqual({t: self.read(p, t, 'attempt') for t in 'abc'}, dict.fromkeys('abc', '1'))
        self.assertEqual(self.gread(p, 'retries.used').strip(), '1')
        self.assertEqual(self.status(p), 'PASS')
        self.assertTrue((p / 'groups' / 'g1' / 'merged').exists())

    def test_unknown_is_left_when_the_budget_is_spent(self):
        p = self.plan_of([A, B, C])
        self.scenario({'a.qa.*': {'findings': 'I looked at it.', 'verdict': 'none'}})
        r = self.run_plan(p, '--retry-failed', '1')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(len(self.dispatches('a.txt', 'qa')), 2)                 # the first review and the one re-review
        self.assertEqual(self.status(p), 'UNKNOWN')
        self.assertEqual(self.states(p), dict.fromkeys('abc', 'IMPLEMENTED'))
        self.assertIn('waiting for group g1 (UNKNOWN)', r.stdout)

    def test_a_decision_leaves_the_members_implemented_and_names_the_group(self):
        p = self.plan_of([A, B, C, D])
        self.scenario({'a.qa.1': {'findings': P1_B + '\n' + P1_NOWHERE, 'verdict': 'FAIL'}})
        r = self.run_plan(p, '--retry-failed', '2')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.status(p), 'NEEDS_DECISION')
        self.assertEqual(self.states(p), dict.fromkeys('abc', 'IMPLEMENTED'), r.stdout)    # not BLOCKED, not retried
        self.assertEqual(len(self.dispatches('a.txt', 'qa')), 1)
        self.assertEqual(len(self.dispatches('a.txt', 'dwarf')), 1)
        for task in 'abc':
            self.assertIn('forge: %s is implemented, waiting for group g1 (NEEDS_DECISION)' % task, r.stdout)
        self.assertIn('group g1: NEEDS_DECISION', r.stdout)                      # _group_settle's line, once per reaping
        self.assertEqual(self.read(p, 'd', 'status'), 'BLOCKED')                 # its dependency never merged
        self.assertEqual(self.dispatches('d.txt', 'dwarf'), [])


class ResumeTests(ScheduleCase):
    def start(self, p, *flags):
        return subprocess.Popen(['bash', str(SCRIPTS / 'forge-parallel.sh'), 'run', str(p), *map(str, flags)],
                                cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def wait_for(self, what, limit=60):
        end = time.monotonic() + limit
        while time.monotonic() < end:
            if what():
                return
            time.sleep(0.05)
        self.fail('timed out waiting for the harness')

    def test_a_run_killed_during_the_review_reviews_again_without_implementing_any_member(self):
        p = self.plan_of([A, B, C])
        self.scenario({'a.qa.1': {'sleep': 60}})
        run = self.start(p)
        try:
            self.wait_for(lambda: bool(self.dispatches('a.txt', 'qa')))
            os.kill(run.pid, signal.SIGTERM)
            out = run.communicate(timeout=90)[0]
        finally:
            if run.poll() is None:
                run.kill()
                run.communicate()
        self.assertEqual(self.status(p), 'INTERRUPTED', out)
        self.assertEqual(self.states(p), dict.fromkeys('abc', 'IMPLEMENTED'), out)
        self.assertFalse((p / 'groups' / 'g1' / 'merged').exists())
        self.scenario({})
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual({t: len(self.dispatches(t + '.txt', 'dwarf')) for t in 'abc'}, dict.fromkeys('abc', 1))
        self.assertEqual(len(self.dispatches('a.txt', 'qa')), 2)
        self.assertEqual(self.status(p), 'PASS')
        self.assertEqual(self.states(p), dict.fromkeys('abc', 'PASS'))
        self.assertTrue((p / 'groups' / 'g1' / 'merged').exists())

    def test_a_saved_pass_is_merged_at_start_with_no_new_dispatch(self):
        p = self.plan_of([A, B, C, D])
        self.scenario({'a.qa.1': {'findings': 'No verdict.', 'verdict': 'none'}})
        r = self.run_plan(p)                                                     # the review ends UNKNOWN and nothing merges
        self.assertEqual(self.status(p), 'UNKNOWN', r.stdout)
        self.assertEqual(self.states(p), dict.fromkeys('abc', 'IMPLEMENTED'))
        # the round is a PASS that was never merged (a scheduler that died between the two)
        (p / 'groups' / 'g1' / 'qa.last').write_text('Looks right.\n\nFORGE_VERDICT: PASS\n')
        (p / 'groups' / 'g1' / 'status').write_text('PASS\n')
        before = len(self.trail())
        r = self.run_plan(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'groups' / 'g1' / 'merged').exists())
        self.assertEqual(self.states(p, 'abcd'), dict.fromkeys('abcd', 'PASS'))
        self.assertEqual([(f, role) for f, role, _ in self.trail()[before:]], [('d.txt', 'dwarf'), ('d.txt', 'qa')])

    def test_a_stale_pass_is_not_merged(self):
        p = self.plan_of([A, B, C])
        self.scenario({'a.qa.1': {'findings': 'No verdict.', 'verdict': 'none'}})
        self.run_plan(p)
        (p / 'groups' / 'g1' / 'status').write_text('PASS\n')
        # a member's branch moved on after the review: the guards of _merge_group refuse
        wt = Path((p / 'wt_root').read_text().strip()) / 'b'
        (wt / 'b.txt').write_text('changed after the review\n')
        self.scenario({})
        r = self.run_plan(p)
        self.assertFalse((p / 'groups' / 'g1' / 'merged').exists(), r.stdout)
        self.assertNotEqual(self.read(p, 'b', 'status'), 'MERGED')
        self.assertEqual([t for t in 'abc' if (p / 'tasks' / t / 'merged').exists()], [])


class UnchangedTests(ScheduleCase):
    EXPECTED = [('a.txt', 'dwarf'), ('a.txt', 'qa'), ('b.txt', 'dwarf'), ('b.txt', 'qa'),
                ('c.txt', 'dwarf'), ('c.txt', 'qa'), ('d.txt', 'dwarf'), ('d.txt', 'qa')]

    def sequence(self, groups):
        p = self.plan_of([A, B, C, D], groups=groups)
        r = self.run_plan(p, '--max-parallel', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('reviewing group', r.stdout)
        return [(f, role) for f, role, _ in self.trail()]

    def test_a_plan_without_groups_dispatches_as_before(self):
        self.assertEqual(self.sequence(None), self.EXPECTED)

    def test_a_plan_whose_groups_are_all_reviewed_task_by_task_dispatches_as_before(self):
        self.assertEqual(self.sequence('g1\ttask\t-\ta,b,c\tThree\tDo all three\n'), self.EXPECTED)


if __name__ == '__main__':
    unittest.main()
