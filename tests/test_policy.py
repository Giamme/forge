"""End-to-end behaviour of the QA severity gate, known-issue reporting, the shared prompt
contract and the verify-coverage warning, through forge-parallel.sh with the fake CLI.

Unit coverage of the gate/contract/coverage modules lives in test_qa.py and
test_verify_coverage.py; this file proves they are wired in and change nothing by default."""
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

    def test_without_a_threshold_the_same_review_still_fails(self):
        p, r = self.one(P3)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(p, 'tasks/a/status'), 'FAIL')
        self.assertFalse((p / 'tasks/a/known_issues.md').exists())
        self.assertFalse((p / 'tasks/a/qa.gate').exists())

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

    def test_none_clears_a_threshold(self):
        p, r = self.one(P3, '--qa-threshold', 'P2')
        self.assertTrue((p / 'qa_threshold').exists())
        self.run_script('forge-parallel.sh', 'run', p, '--qa-threshold', 'none')
        self.assertFalse((p / 'qa_threshold').exists())

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

    def test_without_a_threshold_the_prompt_has_no_threshold_clause(self):
        p, r = self.one(P3)
        qa = (p / 'tasks/a/qa.input').read_text()
        self.assertNotIn('Blocking threshold', qa)
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
