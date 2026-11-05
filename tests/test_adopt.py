"""#79: `adopt` brings a commit made outside forge into a run's integration branch, reviewed alone.

Through the real runner with the fake CLI: the guard keeps its meaning, a refusal leaves the run as it
was, and what is adopted is recorded as a person's, with no task row and no implementer dispatch."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
from pathlib import Path
import subprocess
import unittest

import test_forge as fixtures

BLOCKING = {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] fix.txt:1 — wrong'}
# a.qa.1 is the task's own review; the scenario is keyed by the file name in the prompt, so an adoption
# review (no TASK_a in its prompt) counts under change.qa.
REVIEW = 'change.qa.1'


class AdoptTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def merged_plan(self, table=None):
        self.scenario(table or {})
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks/a/prompt.md').write_text('Implement change. TASK_a')
        for args in (('plan', p, '--repo', self.repo, '--no-memory'), ('run', p)):
            r = self.run_script('forge-parallel.sh', *args)
            self.assertEqual(r.returncode, 0, r.stdout)
        self.calls_before = list(self.calls())
        self.wt = Path((p / 'wt_root').read_text().strip()) / '_integration'
        return p

    def in_wt(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.wt, env=self.env, text=True).strip()

    def commit_on_integration(self, name='fix.txt'):
        (self.wt / name).write_text('a fix\n')
        self.in_wt('add', name); self.in_wt('commit', '-qm', 'a fix by hand')
        return self.in_wt('rev-parse', 'HEAD')

    def commit_elsewhere(self):
        """A commit on a branch of the source repo, off the integration branch."""
        self.git('checkout', '-q', '-b', 'hand', self.in_wt('rev-parse', 'HEAD'))
        (self.repo / 'fix.txt').write_text('a fix\n')
        self.git('add', 'fix.txt'); self.git('commit', '-qm', 'a fix by hand')
        sha = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('checkout', '-q', '-')
        return sha

    def adopt(self, p, *args):
        return self.run_script('forge-parallel.sh', 'adopt', p, *args)

    def accepted(self, p):
        return (p / 'accepted.integration').read_text().strip()

    def review_dir(self, p):
        return sorted(p.glob('adopt-*'))[-1]

    def test_the_guard_names_adopt_and_head_passes_the_review_that_refused(self):
        p = self.merged_plan()
        sha = self.commit_on_integration()
        r = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'opus')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('adopt', r.stdout)
        r = self.adopt(p, '--head', '--reason', 'the accepted task left a narrow bug')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.accepted(p), sha)
        self.assertEqual(self.calls()[len(self.calls_before):], ['qa'])          # one review, and no implementer
        out = self.review_dir(p)
        prompt = (out / 'qa.input').read_text()
        self.assertIn('the accepted task left a narrow bug', prompt)
        self.assertIn('+a fix', prompt)
        self.assertNotIn('+implemented', prompt)                                 # only that range, not the task's work
        r = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        final = sorted(p.glob('final-review-*'))[-1]
        self.assertIn('+a fix', (final / 'qa.input').read_text())
        self.assertIn('the accepted task left a narrow bug', (final / 'qa.input').read_text())

    def test_a_commit_from_elsewhere_is_cherry_picked_and_keeps_its_author(self):
        p = self.merged_plan()
        sha = self.commit_elsewhere()
        before = self.accepted(p)
        r = self.adopt(p, sha, '--reason', 'r')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotEqual(self.accepted(p), before)
        self.assertEqual(self.in_wt('rev-parse', 'HEAD'), self.accepted(p))
        self.assertEqual((self.wt / 'fix.txt').read_text(), 'a fix\n')
        [row] = [line.split('\t') for line in (p / 'adoptions.tsv').read_text().splitlines()]
        self.assertEqual(row[1:5], [before, self.accepted(p), 'PASS', '0'])
        self.assertEqual(row[6:], [self.review_dir(p).name, 'r'])
        entry = json.loads((p / 'control.log').read_text().splitlines()[-1])
        self.assertEqual((entry['action'], entry['verdict'], entry['reason'], entry['approved']), ('adopt', 'PASS', 'r', False))

    def test_a_failed_review_puts_the_branch_back_and_keeps_the_commits_reachable(self):
        p = self.merged_plan({REVIEW: BLOCKING})
        before = self.accepted(p)
        sha = self.commit_on_integration()
        r = self.adopt(p, '--head', '--reason', 'r')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn('wrong', r.stdout)                                         # the findings are printed
        self.assertEqual(self.accepted(p), before)
        self.assertEqual(self.in_wt('rev-parse', 'HEAD'), before)
        self.assertFalse((p / 'adoptions.tsv').exists())
        self.assertIn(sha, r.stdout)
        self.assertEqual(self.in_wt('rev-parse', 'refs/forge/adopt-refused/' + self.review_dir(p).name), sha)

    def test_a_failed_review_of_a_commit_from_elsewhere_undoes_the_cherry_pick(self):
        p = self.merged_plan({REVIEW: BLOCKING})
        before = self.accepted(p)
        r = self.adopt(p, self.commit_elsewhere(), '--reason', 'r')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.in_wt('rev-parse', 'HEAD'), before)
        self.assertFalse((self.wt / 'fix.txt').exists())

    def test_approved_overrides_a_failed_review_and_says_so(self):
        p = self.merged_plan({REVIEW: BLOCKING})
        sha = self.commit_on_integration()
        r = self.adopt(p, '--head', '--reason', 'r', '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.accepted(p), sha)
        self.assertEqual((p / 'adoptions.tsv').read_text().split('\t')[3:5], ['FAIL', '1'])
        self.assertTrue(json.loads((p / 'control.log').read_text().splitlines()[-1])['approved'])

    def test_a_tolerated_finding_is_adopted_with_its_known_issue(self):
        p = self.merged_plan({REVIEW: {'verdict': 'FAIL', 'findings': '- [P3][CONFIRMED] fix.txt:1 — a nit'}})
        sha = self.commit_on_integration()
        self.assertEqual(self.adopt(p, '--head', '--reason', 'r').returncode, 0)
        self.assertEqual(self.accepted(p), sha)
        self.assertIn('a nit', (self.review_dir(p) / 'known_issues.md').read_text())

    def test_an_infrastructure_stop_adopts_nothing_even_when_approved(self):
        p = self.merged_plan({REVIEW: {'rc': 1, 'is_error': True, 'verdict': 'none', 'findings': "You've hit your usage limit."}})
        before = self.accepted(p)
        self.commit_on_integration()
        r = self.adopt(p, '--head', '--reason', 'r', '--approved')
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertEqual(self.accepted(p), before)
        self.assertEqual(self.in_wt('rev-parse', 'HEAD'), before)

    def test_what_is_refused_before_any_model_call(self):
        p = self.merged_plan()
        sha = self.commit_elsewhere()
        accepted = self.accepted(p)
        for name, args, code in (('no reason', (sha,), 2), ('nothing named', ('--reason', 'r'), 2),
                                 ('both', (sha, '--head', '--reason', 'r'), 2),
                                 ('head with nothing new', ('--head', '--reason', 'r'), 3),
                                 ('not a commit', ('nope', '--reason', 'r'), 3),
                                 ('already there', (accepted, '--reason', 'r'), 3),
                                 ('a spec that cannot resolve', (sha, '--reason', 'r', '--qa', 'nope:nope:nope'), 3)):
            with self.subTest(name):
                r = self.adopt(p, *args)
                self.assertEqual(r.returncode, code, r.stdout)
                self.assertEqual(self.in_wt('rev-parse', 'HEAD'), accepted)
                self.assertEqual(self.calls(), self.calls_before)
        (self.wt / 'dirty.txt').write_text('x\n')
        r = self.adopt(p, sha, '--reason', 'r')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.calls(), self.calls_before)

    def test_a_commit_that_does_not_apply_leaves_nothing_behind(self):
        p = self.merged_plan()
        sha = self.commit_elsewhere()
        (self.wt / 'fix.txt').write_text('another fix\n')                         # the same file, another way
        self.in_wt('add', 'fix.txt'); self.in_wt('commit', '-qm', 'clash')
        (p / 'accepted.integration').write_text(self.in_wt('rev-parse', 'HEAD') + '\n')
        now = self.accepted(p)
        r = self.adopt(p, sha, '--reason', 'r')
        self.assertEqual(r.returncode, 6, r.stdout)
        self.assertEqual(self.in_wt('rev-parse', 'HEAD'), now)
        self.assertEqual(self.in_wt('status', '--porcelain'), '')
        self.assertEqual(self.calls(), self.calls_before)

    def test_a_merge_commit_is_refused(self):
        p = self.merged_plan()
        sha = self.commit_elsewhere()
        self.git('checkout', '-q', '-b', 'side', sha + '~1')
        (self.repo / 'other.txt').write_text('x\n'); self.git('add', '.'); self.git('commit', '-qm', 'side')
        self.git('checkout', '-q', 'hand')
        self.git('merge', '-q', '--no-ff', '-m', 'merge', 'side')
        merge = self.git('rev-parse', 'HEAD').decode().strip()
        self.git('checkout', '-q', '-')
        r = self.adopt(p, merge, '--reason', 'r')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.calls(), self.calls_before)


if __name__ == '__main__':
    unittest.main()
