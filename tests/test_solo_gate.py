"""A solo run enforces the QA severity rule itself, the same gate a decomposed task gets (#93):
the default P2 threshold, --qa-threshold, known_issues.md, qa.gate, and --native-review left alone.

Drives forge-solo.sh (and, for parity, forge-parallel.sh) through the fake CLIs of test_forge.py."""
from pathlib import Path
import unittest

import test_forge as fixtures

P3 = '- [P3][CONFIRMED] change.txt:1 — cosmetic problem (any input)'
P2 = '- [P2][CONFIRMED] change.txt:1 — edge case breaks (empty input)'
OVERRIDE = 'the reviewer said PASS, but listed a CONFIRMED finding at or above the %s threshold — forge made it FAIL'


class SoloGateTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    solo = fixtures.ForgeTests.solo

    def review(self, finding, verdict, *flags):
        self.scenario({'*.qa.*': {'verdict': verdict, 'findings': finding}})
        r = self.solo(*flags)
        return self.root / 'solo', r

    def verdict(self, run):
        return (run / 'verdict').read_text().strip()

    def test_default_threshold_turns_a_pass_with_a_confirmed_p2_into_fail(self):
        run, r = self.review(P2, 'PASS')
        self.assertEqual(self.verdict(run), 'FAIL', r.stdout)
        self.assertEqual((run / 'status').read_text().strip(), 'FAIL')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn('verdict: FAIL (' + OVERRIDE % 'P2' + ')', r.stdout)
        fields = (run / 'qa.gate').read_text().rstrip('\n').split('\t')
        self.assertEqual(fields[:2], ['P2', 'fail'])
        self.assertEqual(len(fields), 6)
        self.assertFalse((run / 'qa_threshold').exists())

    def test_a_reviewer_fail_exits_like_a_gated_fail(self):
        run, r = self.review(P2, 'FAIL')
        self.assertEqual(self.verdict(run), 'FAIL', r.stdout)
        self.assertEqual(r.returncode, 5, r.stdout)

    def test_a_pass_with_only_a_confirmed_p3_stays_pass_and_records_it(self):
        run, r = self.review(P3, 'PASS')
        self.assertEqual(self.verdict(run), 'PASS', r.stdout)
        self.assertIn('cosmetic problem', (run / 'known_issues.md').read_text())
        self.assertTrue((run / 'qa.gate').read_text().startswith('P2\t'))
        self.assertIn('verdict: PASS\n', r.stdout)

    def test_a_fail_with_only_a_p3_is_accepted(self):
        run, r = self.review(P3, 'FAIL')
        self.assertEqual(self.verdict(run), 'PASS', r.stdout)
        self.assertEqual((run / 'status').read_text().strip(), 'PASS')
        self.assertTrue((run / 'qa.gate').read_text().startswith('P2\taccept'))
        self.assertIn('verdict: PASS (accepted with known issues: %s)' % (run / 'known_issues.md'), r.stdout)
        self.assertIn('cosmetic problem', (run / 'known_issues.md').read_text())

    def test_the_flag_sets_the_threshold(self):
        for value in ('P3', 'none'):
            with self.subTest(value=value):
                self.git('reset', '-q', '--hard'); self.git('clean', '-qfd')
                run, r = self.review(P3, 'PASS', '--qa-threshold', value)
                self.assertEqual(self.verdict(run), 'FAIL', r.stdout)
                self.assertEqual((run / 'qa_threshold').read_text().strip(), value)
                self.assertTrue((run / 'qa.gate').read_text().startswith('P3\tfail'))
                self.assertIn(OVERRIDE % 'P3', r.stdout)

    def test_the_threshold_reaches_the_review_prompt(self):
        run, _ = self.review(P3, 'PASS', '--qa-threshold', 'P1')
        self.assertIn('Blocking threshold: P1', (run / 'qa.input').read_text())

    def test_a_bad_threshold_is_a_usage_error(self):
        r = self.solo('--qa-threshold', 'P9')
        self.assertEqual(r.returncode, 2, r.stdout)

    def test_native_review_stays_unknown_without_a_gate(self):
        run, r = self.review(P2, 'PASS', '--qa', 'sol', '--native-review')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.verdict(run), 'UNKNOWN', r.stdout)
        self.assertFalse((run / 'qa.gate').exists())

    def test_solo_and_a_decomposed_task_reach_the_same_decision(self):
        cases = [(P2, 'PASS'), (P3, 'PASS'), (P3, 'FAIL'), (P2, 'FAIL')]
        for n, (finding, said) in enumerate(cases):
            with self.subTest(finding=finding[:6], reviewer=said):
                self.git('reset', '-q', '--hard'); self.git('clean', '-qfd')
                run, _ = self.review(finding, said)
                plan = self.root / ('plan%d' % n); plan.mkdir()
                (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
                (plan / 'tasks' / 'a').mkdir(parents=True)
                (plan / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. TASK_a')
                self.assertEqual(self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo,
                                                 '--no-memory').returncode, 0)
                self.scenario({'a.qa.*': {'verdict': said, 'findings': finding.replace('change.txt', 'a.txt')}})
                self.run_script('forge-parallel.sh', 'run', plan)
                solo_gate = (run / 'qa.gate').read_text().rstrip('\n').split('\t')
                task_gate = (plan / 'tasks/a/qa.gate').read_text().rstrip('\n').split('\t')
                self.assertEqual(solo_gate, task_gate)
                task_ok = (plan / 'tasks/a/status').read_text().strip()
                self.assertEqual(self.verdict(run), task_ok)
                for p in self.root.glob('scenario.json*'): p.unlink()
                import shutil; shutil.rmtree(run)


if __name__ == '__main__':
    unittest.main()
