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

    def test_a_flipped_pass_is_retried_with_the_forge_note_above_the_report_and_a_plain_fail_has_none(self):
        finding = '- [P2][CONFIRMED] a.txt:1 — edge case breaks (empty input)'
        note = ("forge: the reviewer's verdict was PASS, but this report lists CONFIRMED findings at or above "
                "the `P2` blocking threshold, so forge failed the attempt. Fix those findings.")
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'PASS', 'findings': finding}, 'a.dwarf.2': {'content': 'fixed'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        carried = (p / 'tasks/a/retry_findings.md').read_text()
        self.assertTrue(carried.startswith(note + '\n\n'), carried)
        self.assertIn(finding, carried)
        self.assertIn(note, (p / 'tasks/a/dwarf.input').read_text())

    def test_a_plain_fails_retry_findings_carry_no_forge_note(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('forge: the reviewer', (p / 'tasks/a/retry_findings.md').read_text())
        self.assertNotIn('forge: the reviewer', (p / 'tasks/a/dwarf.input').read_text())

    def test_a_later_attempt_that_never_reached_the_gate_does_not_inherit_the_flip_note(self):
        # Attempt 1 is a flipped PASS; attempt 2's own FAIL is invalidated before the gate runs.
        # The retry after it carries attempt 2's report, which no forge decision touched.
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'PASS', 'findings': '- [P2][CONFIRMED] a.txt:1 — edge case breaks'},
                       'a.dwarf.2': {'content': 'fixed'},
                       'a.qa.2': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — still broken'},
                       'a.dwarf.3': {'content': 'fixed again'}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.env['MUTATE'] = '1'
        self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(self.text(p, 'tasks/a/status'), 'INVALIDATED')
        del self.env['MUTATE']
        self.run_script('forge-parallel.sh', 'retry', p, 'a')
        carried = (p / 'tasks/a/retry_findings.md').read_text()
        self.assertIn('still broken', carried)
        self.assertNotIn('forge: the reviewer', carried)

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

    # --- escalation policy -----------------------------------------------------------
    def pooled_plan(self, *pools):
        """One low task on sol, planned with the given tier pools (the escalation ladder)."""
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks' / 'a').mkdir(parents=True)
        (p / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *pools)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def three_failures(self, then=None):
        # Each attempt writes something new, so no retry is the byte-identical kind.
        table = {'a.qa.%d' % n: {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — still broken'}
                 for n in (1, 2, 3)}
        table.update({'a.dwarf.%d' % n: {'content': 'try %d' % n} for n in (1, 2, 3, 4, 5)})
        table.update(then or {})
        self.scenario(table)

    def spent_on(self, p):
        return [(r[1], r[2], r[4]) for r in (line.split('\t') for line in
                                             (p / 'tasks/a/attempts.tsv').read_text().splitlines())]

    def test_the_fourth_attempt_after_three_failures_goes_a_tier_up(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        self.three_failures()
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 3)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.spent_on(p), [('1', 'FAIL', 'sol'), ('2', 'FAIL', 'sol'), ('3', 'FAIL', 'sol'),
                                            ('4', 'PASS', 'astra')])
        self.assertIn('model=gpt-6-astra', (p / 'tasks/a/dwarf.resolved').read_text())
        self.assertEqual((p / 'tasks/a/escalations.tsv').read_text().split('\t')[1:], ['3', 'sol', 'astra', 'policy\n'])
        self.assertIn('escalating from sol to astra', r.stdout)

    def test_with_no_stronger_model_the_task_stops_with_a_split_suggestion(self):
        p = self.pooled_plan('--dwarf-low', 'sol')
        self.three_failures()
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 3)          # budget left, but no fourth sol attempt
        self.assertIn('already in the highest tier', (p / 'tasks/a/split.suggested').read_text())
        self.assertIn('split suggested', r.stdout)
        # A plain retry is refused the same way; naming a model is the human's call.
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual((r.returncode, self.calls().count('dwarf')), (2, 3), r.stdout)
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--dwarf', 'sol')
        self.assertEqual((r.returncode, self.calls().count('dwarf')), (0, 4), r.stdout)
        self.assertFalse((p / 'tasks/a/escalations.tsv').exists())   # the same model is no escalation

    def test_a_spent_budget_still_reports_the_escalation_due(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        self.three_failures()
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 2)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn('its next retry goes to astra', r.stdout)

    def test_escalate_after_zero_keeps_retrying_the_same_model(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        self.three_failures()
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 3, '--escalate-after', 0)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual([spec for _, _, spec in self.spent_on(p)], ['sol'] * 4)
        self.assertFalse((p / 'tasks/a/escalations.tsv').exists())

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
        self.assertFalse((p / 'verification.retry.log').exists())
        self.assertNotIn('jev_retry', (p / 'verification.runs.tsv').read_text())
        self.assertFalse((p / 'verification.jev.json').exists())

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
                            ('--qa-threshold', 'P9'), ('--verify-retries', ''), ('--escalate-after', 'x')]:
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
