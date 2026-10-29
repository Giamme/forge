"""End-to-end behaviour of the QA severity gate, known-issue reporting, the shared prompt
contract and the verify-coverage warning, through forge-parallel.sh with the fake CLI.

Unit coverage of the gate/contract/coverage modules lives in test_qa.py and
test_verify_coverage.py; this file proves they are wired in, at the default P2 threshold and at a set one."""
import json
from pathlib import Path
import unittest

import test_forge as fixtures

P3 = '- [P3][CONFIRMED] a.txt:1 — cosmetic problem (any input)'
P2 = '- [P2][CONFIRMED] a.txt:1 — edge case breaks (empty input)'
P1 = '- [P1][CONFIRMED] a.txt:1 — realistic input breaks (x)'
UNLABELLED = '- [CONFIRMED] a.txt:1 — breaks, nobody said how badly'


class PolicyTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, rows):
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

    def one(self, finding, *flags, verdict='FAIL'):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.*': {'verdict': verdict, 'findings': finding}})
        r = self.run_script('forge-parallel.sh', 'run', p, *flags)
        return p, r

    # --- the gate ---------------------------------------------------------------
    def test_tolerated_findings_are_accepted_as_known_issues(self):
        p, r = self.one(P3, '--qa-threshold', 'P2')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertIn('cosmetic problem', (p / 'tasks/a/known_issues.md').read_text())
        gate = (p / 'tasks/a/qa.gate').read_text().split('\t')
        self.assertEqual(gate[:2], ['P2', 'accept'])
        self.assertIn('known issues accepted', r.stdout)
        self.assertIn('tasks/a/known_issues.md', r.stdout)

    def test_without_a_threshold_a_fail_made_only_of_p3_findings_is_accepted(self):
        p, r = self.one(P3)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertIn('cosmetic problem', (p / 'tasks/a/known_issues.md').read_text())
        self.assertTrue((p / 'tasks/a/qa.gate').read_text().startswith('P2\taccept'))
        self.assertFalse((p / 'qa_threshold').exists())

    def test_a_pass_listing_a_confirmed_p2_is_made_fail_and_retried(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'PASS', 'findings': P2}, 'a.dwarf.2': {'content': 'fixed'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', '1')
        self.assertIn("forge overrode the reviewer's verdict", r.stdout)
        self.assertIn('attempt 1: PASS -> FAIL at the P2 threshold (worst P2)', r.stdout)
        self.assertIn('a: the reviewer said PASS but listed a CONFIRMED finding at or above the P2 threshold '
                      '(worst P2) — forge made it FAIL', (p / 'tasks/a/task.out').read_text())
        first, second = [line.split('\t')[2] for line in self.text(p, 'tasks/a/attempts.tsv').splitlines()]
        self.assertEqual((first, second), ('FAIL', 'PASS'), r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())

    def test_a_pass_listing_a_confirmed_p2_is_not_merged(self):
        p, r = self.one(P2, verdict='PASS')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')
        self.assertFalse((p / 'tasks/a/merged').exists())
        self.assertTrue((p / 'tasks/a/qa.gate').read_text().startswith('P2\tfail'))
        self.assertIn('attempt 1: PASS -> FAIL at the P2 threshold (worst P2)', r.stdout)
        self.assertNotIn('known issues accepted', r.stdout)

    def test_a_pass_listing_only_a_confirmed_p3_merges_with_the_finding_recorded(self):
        p, r = self.one(P3, verdict='PASS')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertIn('cosmetic problem', (p / 'tasks/a/known_issues.md').read_text())
        self.assertIn('known issues accepted', r.stdout)

    def test_threshold_p3_fails_a_pass_with_a_confirmed_p3(self):
        p, r = self.one(P3, '--qa-threshold', 'P3', verdict='PASS')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')
        self.assertTrue((p / 'tasks/a/qa.gate').read_text().startswith('P3\tfail'))
        self.scenario({'a.qa.*': {'verdict': 'PASS', 'findings': P3}, 'a.dwarf.2': {'content': 'v2'}})
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')     # a retry runs in the foreground
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn('a: the reviewer said PASS but listed a CONFIRMED finding at or above the P3 threshold '
                      '(worst P3) — forge made it FAIL', r.stdout)
        self.assertIn('attempt 2: PASS -> FAIL', r.stdout)
        self.assertNotIn('attempt 1:', r.stdout)                      # the earlier run's flip is not this one's

    def test_a_failed_task_is_not_listed_as_accepting_known_issues(self):
        # P1 blocks at P2; the P3 beside it is still written to known_issues.md, but nothing was accepted.
        p, r = self.one(P3 + '\n' + P1, verdict='PASS')
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL', r.stdout)
        self.assertTrue((p / 'tasks/a/known_issues.md').exists())
        self.assertNotIn('known issues accepted', r.stdout)

    def test_a_finding_at_the_threshold_still_blocks(self):
        p, r = self.one(P2, '--qa-threshold', 'P2')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')
        self.assertTrue((p / 'tasks/a/qa.gate').read_text().startswith('P2\tkeep'))

    def test_one_blocking_finding_among_tolerated_ones_blocks(self):
        p, r = self.one(P3 + '\n' + P1, '--qa-threshold', 'P2')
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL', r.stdout)

    def test_an_unlabelled_confirmed_finding_is_never_tolerated(self):
        p, r = self.one(UNLABELLED, '--qa-threshold', 'P0')
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL', r.stdout)

    def test_a_fail_with_no_confirmed_findings_stays_a_fail(self):
        p, r = self.one('- [P3][PLAUSIBLE] a.txt:1 — might be wrong', '--qa-threshold', 'P2')
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL', r.stdout)

    def test_a_per_task_threshold_beats_the_plan_default(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')])
        (p / 'tasks/a/qa_threshold').write_text('P1\n')     # tooling task: only P0/P1 block
        self.scenario({'a.qa.*': {'verdict': 'FAIL', 'findings': P2},
                       'b.qa.*': {'verdict': 'FAIL', 'findings': P2}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--qa-threshold', 'P3')
        self.assertTrue((p / 'tasks/a/merged').exists(), r.stdout)
        self.assertEqual(self.text(p, 'tasks/b/status'), 'FAIL')

    def test_the_threshold_is_remembered_for_retry(self):
        p, r = self.one(P3, '--qa-threshold', 'P3')
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')
        self.scenario({'a.qa.*': {'verdict': 'FAIL', 'findings': P3}, 'a.dwarf.*': {'content': 'v2'}})
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--qa-threshold', 'P2')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertEqual(self.text(p, 'qa_threshold'), 'P2')

    def test_none_is_the_same_as_p3(self):
        p, r = self.one(P3, '--qa-threshold', 'none', verdict='PASS')
        self.assertEqual(self.text(p, 'qa_threshold'), 'none')
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL', r.stdout)
        self.assertTrue((p / 'tasks/a/qa.gate').read_text().startswith('P3\tfail'))

    def test_default_removes_the_threshold(self):
        p, r = self.one(P3, '--qa-threshold', 'P1')
        self.assertTrue((p / 'qa_threshold').exists())
        self.run_script('forge-parallel.sh', 'run', p, '--qa-threshold', 'default')
        self.assertFalse((p / 'qa_threshold').exists())

    def test_plan_takes_the_threshold_and_the_table_shows_it(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.assertFalse((p / 'qa_threshold').exists())
        r = self.run_script('forge-parallel.sh', 'plan', p, '--qa-threshold', 'P1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(p, 'qa_threshold'), 'P1')
        self.assertIn('QA threshold: P1 (set)', r.stdout)

    def test_the_table_says_which_effort_a_reviewer_with_none_reviews_at(self):
        p = self.plan_of([('a', '-', 'a.txt')])                           # qa `opus`: no effort named
        r = self.run_script('forge-parallel.sh', 'plan', p)
        self.assertIn('QA effort: high where a reviewer spec names none', r.stdout)
        (p / 'tasks.tsv').write_text((p / 'tasks.tsv').read_text().replace('\topus\t', '\topus:xhigh\t'))
        r = self.run_script('forge-parallel.sh', 'plan', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('QA effort:', r.stdout)

    def test_a_bad_threshold_is_refused(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'plan', p, '--qa-threshold', 'high')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('--qa-threshold needs P0, P1, P2, P3, none or default', r.stdout)

    # --- the shared prompt contract -------------------------------------------------
    def test_qa_prompt_asks_for_severity_labels_and_keeps_the_legacy_wording(self):
        p, r = self.one(P3, '--qa-threshold', 'P2')
        qa = (p / 'tasks/a/qa.input').read_text()
        self.assertIn('The implementer was asked to do the task above.', qa)
        self.assertIn('Mark each CONFIRMED if you traced', qa)
        self.assertIn('[P1][CONFIRMED]', qa)
        self.assertIn('Blocking threshold: P2', qa)
        self.assertIn('FORGE_VERDICT: PASS', qa)
        self.assertIn('Place any learning notes before the final FORGE_VERDICT', qa)

    def test_without_a_threshold_the_prompt_carries_the_default_clause(self):
        p, r = self.one(P3)
        qa = (p / 'tasks/a/qa.input').read_text()
        self.assertIn('Blocking threshold: P2', qa)
        self.assertIn('[P1][CONFIRMED]', qa)

    def test_the_dwarf_is_told_not_to_commit_or_leave_processes_running(self):
        p, r = self.one(P3)
        dwarf = (p / 'tasks/a/dwarf.input').read_text()
        self.assertIn('Implementer rules', dwarf)
        self.assertIn('git commit', dwarf)
        self.assertIn('background processes', dwarf)

    def test_solo_uses_the_same_contract(self):
        run = self.root / 'solo'; run.mkdir()
        (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        r = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol')
        self.assertEqual(r.returncode, 0, r.stdout)
        qa = (run / 'qa.input').read_text()
        self.assertIn('[P1][CONFIRMED]', qa)
        self.assertIn('say so plainly rather than inventing something to report', qa)
        self.assertIn('Implementer rules', (run / 'dwarf.input').read_text())

    # --- verification coverage ---------------------------------------------------------
    def with_package_json(self, scripts):
        (self.repo / 'package.json').write_text(json.dumps({'name': 'x', 'scripts': scripts}))
        self.git('add', '.'); self.git('commit', '-qm', 'package.json')

    def test_a_suite_the_verify_command_skips_is_named_before_any_model_runs(self):
        self.with_package_json({'test': 'node --test', 'test:contract': 'node scripts/check-contract.js'})
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'echo npm test')
        self.assertIn("suite 'test:contract'", r.stdout)
        self.assertIn('verification coverage', r.stdout)            # again in the summary
        self.assertIn("test:contract", (p / 'verification.coverage.txt').read_text())
        self.assertEqual(self.text(p, 'verification.status'), 'PASS')   # advisory: never a failure

    def test_the_warning_arrives_before_the_first_dispatch(self):
        self.with_package_json({'test': 'node --test', 'test:contract': 'node scripts/check-contract.js'})
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'echo npm test')
        self.assertLess(r.stdout.index("suite 'test:contract'"), r.stdout.index('dispatched a'))

    def test_a_fully_covered_gate_says_nothing(self):
        self.with_package_json({'test': 'node --test'})
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'echo npm test')
        self.assertNotIn('verify coverage', r.stdout)
        self.assertFalse((p / 'verification.coverage.txt').exists())

    def test_no_verify_command_lists_what_the_project_defines(self):
        self.with_package_json({'test': 'node --test', 'lint': 'eslint .'})
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertIn('no verify command configured', r.stdout)
        self.assertEqual(self.text(p, 'verification.status'), 'UNVERIFIED')

    def test_coverage_never_blocks_integration(self):
        self.with_package_json({'test': 'node --test', 'test:contract': 'x'})
        p = self.plan_of([('a', '-', 'a.txt')])
        self.run_script('forge-parallel.sh', 'run', p, '--verify', 'echo npm test')
        self.git('checkout', '-q', '-b', 'work')
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("suite 'test:contract'", r.stdout)


if __name__ == '__main__':
    unittest.main()
