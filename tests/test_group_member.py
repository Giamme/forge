"""A member of a review=group group stops at IMPLEMENTED instead of its own review (#140).

Driven like test_pre_review and test_attempt_cap: forge-parallel.sh `plan` and `run` on a fixture repo with
the fake agent CLI of test_forge (fixtures borrowed, not inherited). The group's own review is a later task, so
these plans end with the members IMPLEMENTED; what is asserted is the member's pipeline and its accounting."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_forge as fixtures
from test_pre_review import EACH

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / 'scripts' / 'forge-route.py'
LIB = ROOT / 'scripts' / 'forge-lib-task.sh'
HEADER = '# id\treview\tqa\tmembers\ttitle\tgoal\n'


def group_file(review):
    return HEADER + 'g1\t%s\t-\ta,b\tTwo\tDo both\n' % review


class MemberCase(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, review=None, rows=None, name='plan', *flags):
        """a and b (b after a when `rows` says so) and c, alone; review: the groups.tsv of {a, b} or None."""
        rows = rows or ['a\t-\tlow\ta.txt\tsol\topus\tA', 'b\t-\tlow\tb.txt\tsol\topus\tB', 'c\t-\tlow\tc.txt\tsol\topus\tC']
        p = self.root / name
        p.mkdir()
        (p / 'tasks.tsv').write_text('\n'.join(rows) + '\n')
        for row in rows:
            task = row.split('\t')[0]
            (p / 'tasks' / task).mkdir(parents=True)
            (p / 'tasks' / task / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + task)
        if review:
            (p / 'groups.tsv').write_text(group_file(review))
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def read(self, plan, task, name):
        return (plan / 'tasks' / task / name).read_text().strip()

    def head(self, plan, task):
        return self.git('rev-parse', 'forge/%s/%s' % (plan.name, task)).decode().strip()


class MemberTests(MemberCase):
    def test_a_member_ends_implemented_with_its_commit_and_fingerprint_and_nothing_else(self):
        p = self.plan_of('group')
        r = self.run_script('forge-parallel.sh', 'run', p)
        for task in ('a', 'b'):
            tdir = p / 'tasks' / task
            self.assertEqual(self.read(p, task, 'status'), 'IMPLEMENTED', r.stdout)
            self.assertEqual(self.read(p, task, 'reviewed.commit'), self.head(p, task))
            self.assertTrue(self.read(p, task, 'source.fingerprint'))
            self.assertFalse((tdir / 'merged').exists())
            # no review: no QA dispatch, input, output or snapshot, no shadow
            for name in ('qa.input', 'qa.out', 'qa.last', 'review.fingerprint', 'review.deps'):
                self.assertFalse((tdir / name).exists(), (task, name))
            self.assertEqual(list(tdir.glob('review-*')), [])
            # no attempt row, escalation or decision: nothing judged this attempt
            self.assertFalse((tdir / 'attempts.tsv').exists())
            self.assertEqual([x.name for x in tdir.iterdir() if x.name.startswith(('escalation', 'decision', 'split'))], [])
            self.assertEqual(self.read(p, task, 'attempt'), '1')
        self.assertEqual(sorted(self.calls()), ['dwarf', 'dwarf', 'dwarf', 'qa'])       # c's review is the only one
        self.assertFalse((p / 'shadow').exists())
        self.assertTrue((p / 'tasks' / 'c' / 'merged').exists())
        self.assertTrue((p / 'tasks' / 'c' / 'attempts.tsv').exists())

    def test_the_fingerprint_is_the_one_a_review_records(self):
        # c is reviewed and a is a member of the same shape: both come from the worktree's own fingerprint.
        p = self.plan_of('group')
        self.run_script('forge-parallel.sh', 'run', p)
        wt = Path((p / 'wt_root').read_text().strip()) / 'a'
        script = 'source "%s/scripts/forge-artifact.sh"; forge_fingerprint "%s"' % (ROOT, wt)
        again = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=self.env)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout.strip(), self.read(p, 'a', 'source.fingerprint'))

    def test_the_table_the_summary_and_the_results_name_the_status(self):
        p = self.plan_of('group')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertRegex(r.stdout, r'\n  a +IMPLEMENTED +forge/plan/a\n')
        self.assertIn("implemented, waiting for their group's review:\n  a: group g1\n  b: group g1\n", r.stdout)
        self.assertNotIn('retry a failed task', r.stdout)            # not a failure to retry
        self.assertIn('a\tIMPLEMENTED\tforge/plan/a', (p / 'results.tsv').read_text())

    def test_a_qa_only_resume_marker_does_not_apply_to_a_member(self):
        p = self.plan_of('group')
        self.run_script('forge-parallel.sh', 'run', p)
        self.scenario({'a.dwarf.*': {'files': {'a2.txt': 'second try\n'}}})
        (p / 'tasks' / 'a' / 'resume').write_text('qa\n')
        (p / 'tasks' / 'a' / 'status').write_text('INFRA\n')
        r = subprocess.run(['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), '_task', str(p), 'a'],
                           cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.read(p, 'a', 'status'), 'IMPLEMENTED')
        self.assertFalse((p / 'tasks' / 'a' / 'resume').exists())
        self.assertEqual(self.read(p, 'a', 'attempt'), '2')           # a full attempt ran, not a re-review


class UnchangedPathTests(MemberCase):
    """A task outside a review=group group is reviewed byte for byte as without groups.tsv."""

    def qa_inputs(self, review):
        # b waits on a, so the run's status as the capsule shows it is the same in both plans
        rows = ['a\t-\tlow\ta.txt\tsol\topus\tA', 'b\ta\tlow\tb.txt\tsol\topus\tB']
        p = self.plan_of(review, rows)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'qa'], r.stdout)
        out = {}
        for task in ('a', 'b'):
            text = (p / 'tasks' / task / 'qa.input').read_text()
            out[task] = text.replace(str(self.root), '<root>')
            self.assertEqual(self.read(p, task, 'status'), 'PASS', r.stdout)
            self.assertTrue((p / 'tasks' / task / 'attempts.tsv').exists())
        return out

    def test_a_review_task_group_member_and_an_ungrouped_task_get_the_same_qa_input(self):
        plain = self.qa_inputs(None)
        fixtures.ForgeTests.setUp(self)            # a fresh repo and calls log for the second plan
        grouped = self.qa_inputs('task')
        self.assertEqual(grouped, plain)


class PreReviewTests(MemberCase):
    def plan_with_tests(self):
        (self.repo / '.forge').mkdir(exist_ok=True)
        (self.repo / '.forge/verify-subset').write_text(EACH + '\n')
        return self.plan_of('group', None, 'plan', '--pre-review-tests')

    def test_a_red_member_is_sent_back_unreviewed_not_implemented(self):
        p = self.plan_with_tests()
        self.scenario({'a.dwarf.1': {'files': {'tests/test_a.py': "raise SystemExit('a.txt is not done yet')\n"}}})
        self.run_script('forge-parallel.sh', 'run', p)
        tdir = p / 'tasks' / 'a'
        self.assertEqual(self.read(p, 'a', 'status'), 'FAIL')
        self.assertTrue((tdir / 'prereview.failed').exists())
        self.assertIn('a.txt is not done yet', (tdir / 'prereview.md').read_text())
        self.assertEqual(list(tdir.glob('review-*')), [])
        self.assertEqual(self.calls().count('qa'), 1)                        # c's review, none for a or b
        self.assertEqual(self.read(p, 'b', 'status'), 'IMPLEMENTED')           # the other member is unaffected

    def test_a_green_member_runs_its_tests_then_stops_with_no_snapshot_left(self):
        p = self.plan_with_tests()
        self.scenario({'a.dwarf.1': {'files': {'tests/test_a.py': 'pass\n'}}})
        self.run_script('forge-parallel.sh', 'run', p)
        tdir = p / 'tasks' / 'a'
        self.assertEqual(self.read(p, 'a', 'status'), 'IMPLEMENTED')
        self.assertIn('"passed"', (tdir / 'prereview.json').read_text().replace(' ', ''))
        self.assertEqual(list(tdir.glob('review-*')), [])


class RefusalTests(MemberCase):
    def test_retry_and_accept_refuse_an_implemented_task_with_exit_3(self):
        p = self.plan_of('group')
        self.run_script('forge-parallel.sh', 'run', p)
        before = self.read(p, 'a', 'status')
        retry = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(retry.returncode, 3, retry.stdout)
        self.assertIn("a is implemented and waits for group g1's review", retry.stdout)
        accept = self.run_script('forge-parallel.sh', 'accept', p, 'a', '--reason', 'looks fine', '--approved')
        self.assertEqual(accept.returncode, 3, accept.stdout)
        self.assertIn("a is implemented and waits for group g1's review", accept.stdout)
        self.assertEqual((before, self.read(p, 'a', 'status')), ('IMPLEMENTED', 'IMPLEMENTED'))
        self.assertEqual(self.read(p, 'a', 'attempt'), '1')                   # the retry changed nothing

    def test_the_scheduler_does_not_retry_it(self):
        p = self.plan_of('group')
        self.run_script('forge-parallel.sh', 'run', p)
        self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 3)
        self.assertEqual(self.calls().count('dwarf'), 3)                      # a and b ran once each, and c


class AccountingTests(unittest.TestCase):
    """record_judged_attempt and post_attempt, sourced from forge-lib-task.sh with stubs for what it borrows."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.skill = self.root / 'skill'
        (self.skill / 'scripts').mkdir(parents=True)
        self.log = self.root / 'calls.log'
        for name in ('forge-route.py', 'forge-ledger.py'):
            (self.skill / 'scripts' / name).write_text(
                'import sys\nopen(%r, "a").write("%s " + " ".join(sys.argv[1:]) + "\\n")\n'
                'if sys.argv[1] == "attempt-cap": print("1 -")\nif sys.argv[1] == "signal": sys.exit(1)\n'
                % (str(self.log), name))
        self.plan = self.root / 'plan'
        (self.plan / 'tasks' / 'a').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (self.plan / 'tasks' / 'a' / 'attempt').write_text('3\n')

    def bash(self, body):
        script = ('note() { :; }; task_status() { cat "$1/tasks/$2/status" 2>/dev/null || echo PENDING; }; '
                  'field() { awk -F"\\t" -v id="$2" \'$1 == id { print $5 }\' "$1"; }; '
                  'qa_only_next_flag() { echo no; }; source "%s"; %s' % (LIB, body))
        env = {'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin', 'SKILL_DIR': str(self.skill)}
        r = subprocess.run(['/bin/bash', '-c', script], capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def rows(self):
        path = self.plan / 'tasks' / 'a' / 'attempts.tsv'
        return [line.split('\t') for line in path.read_text().splitlines()] if path.exists() else []

    def test_record_judged_attempt_writes_a_row_in_the_format_of_record_attempt(self):
        (self.plan / 'tasks' / 'a' / 'status').write_text('FAIL\n')
        self.bash('record_attempt "%s" a; record_judged_attempt "%s" a FAIL; record_judged_attempt "%s" a PASS' % ((self.plan,) * 3))
        recorded, judged, passed = self.rows()
        self.assertEqual(recorded[1:], judged[1:])                # attempt, status, class, implementer
        self.assertEqual(judged[1:], ['3', 'FAIL', '', 'sol'])
        self.assertEqual(passed[1:], ['3', 'PASS', '', 'sol'])
        self.assertTrue(judged[0].isdigit())

    def test_record_attempt_writes_no_row_for_an_implemented_task(self):
        (self.plan / 'tasks' / 'a' / 'status').write_text('IMPLEMENTED\n')
        self.bash('record_attempt "%s" a' % self.plan)
        self.assertEqual(self.rows(), [])

    def test_post_attempt_does_nothing_for_an_implemented_task(self):
        (self.plan / 'tasks' / 'a' / 'status').write_text('IMPLEMENTED\n')
        self.bash('post_attempt "%s" a' % self.plan)
        self.assertFalse(self.log.exists())                       # no escalation check, no ledger, no decision

    def test_post_attempt_runs_the_check_and_the_ledger_for_a_judged_one(self):
        (self.plan / 'tasks' / 'a' / 'status').write_text('FAIL\n')
        self.bash('post_attempt "%s" a' % self.plan)
        calls = self.log.read_text()
        self.assertIn('forge-route.py escalation --plan', calls)
        self.assertIn('--check', calls)
        self.assertIn('forge-ledger.py append', calls)


class AttemptCountTests(unittest.TestCase):
    """The attempt cap counts attempts.tsv rows, never tasks/<id>/attempt, so an attempt that is only IMPLEMENTED
    (no row yet) costs nothing and one that is judged later costs exactly one."""

    def test_the_cap_ignores_the_attempt_counter(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp)
            tdir = plan / 'tasks' / 'a'
            tdir.mkdir(parents=True)
            (tdir / 'attempt').write_text('5\n')
            (tdir / 'attempts.tsv').write_text('1700000000\t4\tFAIL\t\tsol\n')
            r = subprocess.run([sys.executable, str(ROUTE), 'attempt-cap', '--plan', str(plan), '--task', 'a'],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.split(), ['1', '8'], r.stderr)
            (tdir / 'attempts.tsv').unlink()
            r = subprocess.run([sys.executable, str(ROUTE), 'attempt-cap', '--plan', str(plan), '--task', 'a'],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.stdout.split(), ['0', '8'], r.stderr)


if __name__ == '__main__':
    unittest.main()
