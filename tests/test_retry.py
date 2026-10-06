"""End-to-end behaviour of the decomposed runner's retry, resume and verify-rerun paths.

Uses the same fake agent CLI as test_forge.py, driven per call through SCENARIO. The
fixtures are borrowed rather than inherited: subclassing ForgeTests would run all of its
tests again under this class."""
import json
from pathlib import Path
import unittest

import test_forge as fixtures


class RetryTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, rows):
        """rows: (id, deps, files). One prompt per task carrying TASK_<id> for the fake."""
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text(''.join(f'{i}\t{d}\tlow\t{f}\tsol\topus\t{i.upper()}\n' for i, d, f in rows))
        for i, _d, _f in rows:
            t = p / 'tasks' / i; t.mkdir(parents=True)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def text(self, plan, name):
        return (plan / name).read_text().strip()

    # --- in-run retry -------------------------------------------------------------
    def test_retry_failed_merges_after_a_failing_review_without_a_second_command(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'}})    # a real fix changes the diff
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 3)   # a twice, b once
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '2')
        self.assertTrue((p / 'tasks/a/merged').exists() and (p / 'tasks/b/merged').exists())
        self.assertIn('previous attempt was reviewed', (p / 'tasks/a/dwarf.input').read_text())
        self.assertIn('[P1][CONFIRMED]', (p / 'tasks/a/dwarf.input').read_text())

    def test_without_the_flag_the_failure_still_waits_for_a_human(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')
        self.assertEqual(self.text(p, 'tasks/b/status'), 'BLOCKED')
        self.assertEqual(self.calls().count('dwarf'), 1)

    def test_a_retry_that_changes_nothing_is_not_retried_again(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        # the second dwarf run writes nothing: its diff is byte-identical to the first
        self.scenario({'a.qa.*': {'verdict': 'FAIL'}, 'a.dwarf.2': {'no_edit': True}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)
        self.assertTrue((p / 'tasks/a/noretry').exists())

    def test_the_retry_hint_names_the_failed_task_and_the_flag(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.*': {'verdict': 'FAIL'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertIn('--retry-failed', r.stdout)
        self.assertIn('retry', r.stdout)

    # --- QA-only resume -------------------------------------------------------------
    def test_a_reviewer_with_no_verdict_is_asked_again_without_rerunning_the_dwarf(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'none', 'findings': 'looks fine to me, but no verdict line'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'UNKNOWN', r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'qa'])      # QA only: the code did not change
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '1')       # and no attempt was spent
        self.assertTrue((p / 'tasks/a/merged').exists())

    def test_run_with_retry_failed_also_asks_an_unverdicted_review_again(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'none', 'findings': 'no verdict'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'qa'])

    def test_a_stale_resume_marker_falls_back_to_the_full_task(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'none', 'findings': 'no verdict line here'},
                       'a.dwarf.2': {'content': 'second attempt'}})
        self.run_script('forge-parallel.sh', 'run', p)
        wt = Path(self.text(p, 'wt_root')) / 'a'
        (wt / 'a.txt').write_text('edited after review\n')      # the worktree no longer matches
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)
        self.assertEqual(self.text(p, 'tasks/a/attempt'), '2')

    # --- attempt log -----------------------------------------------------------------
    def test_every_entry_is_logged_for_audit(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL'}, 'a.dwarf.2': {'content': 'fixed'}})
        self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        rows = [line.split('\t') for line in (p / 'tasks/a/attempts.tsv').read_text().splitlines()]
        self.assertEqual([(r[1], r[2]) for r in rows], [('1', 'FAIL'), ('2', 'PASS')])

    # --- verification reruns ----------------------------------------------------------
    def flaky_command(self):
        marker = self.root / 'flake-marker'    # outside every worktree, so the tree stays unchanged
        return f'if [ -e {marker} ]; then exit 0; else touch {marker}; echo first-run-failed; exit 1; fi'

    def test_a_flaky_verify_command_passes_on_rerun_and_says_so(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', self.flaky_command())
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(p, 'verification.status'), 'PASS')
        self.assertTrue((p / 'verification.flaky').exists())
        self.assertIn('first-run-failed', (p / 'verification.log').read_text())
        self.assertEqual(self.text(p, 'verification.exit'), '0')
        self.assertIn('FLAKY', r.stdout)

    def test_verify_retries_zero_keeps_the_strict_single_run(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', self.flaky_command(), '--verify-retries', 0)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'verification.status'), 'FAIL')
        self.assertFalse((p / 'verification.flaky').exists())

    def test_a_consistently_failing_verify_still_fails_after_the_rerun(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'echo nope; exit 9')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'verification.exit'), '9')
        self.assertFalse((p / 'verification.flaky').exists())
        self.assertEqual(self.text(p, 'verification.status'), 'FAIL')

    def test_a_command_that_changes_the_tree_is_never_retried(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'echo x >> mutated.txt; exit 1')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertFalse((p / 'verification.retry.log').exists())

    # --- integrate -------------------------------------------------------------------
    def test_integrate_names_the_tasks_that_are_not_part_of_it(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')])
        self.scenario({'b.qa.*': {'verdict': 'FAIL'}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.git('checkout', '-q', '-b', 'work')
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('NOT part of this integration', r.stdout)
        self.assertIn('b(FAIL)', r.stdout)

    # --- shared flag parser -------------------------------------------------------------
    def test_bad_values_are_usage_errors(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        for flag, value in [('--retry-failed', 'x'), ('--infra-retries', '-1'), ('--timeout', 'soon'),
                            ('--qa-threshold', 'P9'), ('--verify-retries', '')]:
            r = self.run_script('forge-parallel.sh', 'run', p, flag, value)
            self.assertEqual(r.returncode, 2, (flag, r.stdout))

    def test_new_flags_are_not_accepted_where_they_make_no_sense(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved', '--retry-failed', 1)
        self.assertEqual(r.returncode, 2, r.stdout)

    def test_timeout_flag_is_persisted_and_reaches_dispatch(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.env['SCENARIO_UNUSED'] = '1'
        r = self.run_script('forge-parallel.sh', 'run', p, '--timeout', 123)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(p, 'timeout'), '123')


if __name__ == '__main__':
    unittest.main()
