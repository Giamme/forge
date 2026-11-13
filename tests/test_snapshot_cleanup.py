"""#139: a review snapshot is removed once its review ends, on every outcome, and what forge keeps of
the review stays beside it.

Through the real runners with the fake CLI. A snapshot is a full export of the reviewed tree plus a git
store, so each one left behind costs the size of the repository's tree. FORGE_KEEP_SNAPSHOTS=1 keeps
them (the snapshot-content tests in test_review_deps, test_review and test_forge use it)."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
from pathlib import Path
import unittest

import test_adopt
import test_forge as fixtures

FAIL = {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'}


class SnapshotCleanupTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    solo = fixtures.ForgeTests.solo

    def plan_a(self, *plan_args):
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks/a/prompt.md').write_text('Implement change. TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *plan_args)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def snapshots(self, parent, pattern='review-*'):
        return [d for d in Path(parent).glob(pattern) if d.is_dir()]

    def test_a_task_review_and_its_shadow_leave_their_records_and_no_snapshot(self):
        self.scenario({})
        p = self.plan_a('--shadow-qa', 'opus,sol', '--shadow-rate', '1')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        task = p / 'tasks/a'
        self.assertTrue((task / 'merged').exists(), r.stdout)
        self.assertEqual(self.snapshots(task), [])
        for kept in ('review.base', 'review.deps', 'review.fingerprint', 'qa.input', 'qa.last'):
            self.assertTrue((task / kept).is_file(), kept)
        [shadow] = list(p.glob('shadow/a/*'))
        self.assertEqual((shadow / 'shadow.status').read_text().strip(), 'done')
        self.assertFalse((shadow / 'review-snapshot').exists())
        self.assertTrue((shadow / 'review.base').is_file())

    def test_a_failed_task_review_leaves_no_snapshot(self):
        self.scenario({'a.qa.*': FAIL})
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'tasks/a/status').read_text().strip(), 'FAIL')
        self.assertEqual(self.snapshots(p / 'tasks/a'), [])

    def test_the_next_review_removes_a_snapshot_an_interrupted_review_left(self):
        self.scenario({})
        p = self.plan_a()
        left = p / 'tasks/a/review-1-4242'
        (left / '.git').mkdir(parents=True); (left / 'a.txt').write_text('an old export\n')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(left.exists())
        self.assertEqual(self.snapshots(p / 'tasks/a'), [])

    def test_keep_snapshots_keeps_the_task_snapshot(self):
        self.scenario({})
        self.env['FORGE_KEEP_SNAPSHOTS'] = '1'
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        [snapshot] = self.snapshots(p / 'tasks/a')
        self.assertTrue((snapshot / 'a.txt').is_file())

    def test_a_final_review_leaves_no_snapshot(self):
        self.scenario({})
        p = self.plan_a()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        r = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        out = sorted(p.glob('final-review-*'))[-1]
        self.assertEqual((out / 'verdict').read_text().strip(), 'PASS')
        self.assertTrue((out / 'review.base').is_file())
        self.assertEqual(self.snapshots(out), [])

    def test_the_next_final_review_removes_a_snapshot_an_interrupted_one_left(self):
        self.scenario({})
        p = self.plan_a()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        left = p / 'final-review-1-4242' / 'review-4242'
        (left / '.git').mkdir(parents=True); (left / 'a.txt').write_text('an old export\n')
        not_a_snapshot = p / 'final-review-1-4242' / 'review-notes'
        not_a_snapshot.mkdir()
        r = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse(left.exists())
        self.assertTrue(not_a_snapshot.is_dir())

    def test_a_solo_review_leaves_no_snapshot(self):
        self.scenario({})
        r = self.solo('--qa', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        run = self.root / 'solo'
        self.assertTrue((run / 'review.base').is_file())
        self.assertEqual(self.snapshots(run), [])


class AdoptSnapshotTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    merged_plan = test_adopt.AdoptTests.merged_plan
    in_wt = test_adopt.AdoptTests.in_wt
    commit_on_integration = test_adopt.AdoptTests.commit_on_integration
    adopt = test_adopt.AdoptTests.adopt
    review_dir = test_adopt.AdoptTests.review_dir

    def adopted(self, table):
        p = self.merged_plan(table)
        self.commit_on_integration()
        return p, self.adopt(p, '--head', '--reason', 'a fix by hand')

    def test_an_accepted_adopt_leaves_no_snapshot(self):
        p, r = self.adopted({})
        self.assertEqual(r.returncode, 0, r.stdout)
        out = self.review_dir(p)
        self.assertEqual((out / 'verdict').read_text().strip(), 'PASS')
        self.assertTrue((out / 'review.base').is_file())
        self.assertFalse((out / 'review').exists())

    def test_a_refused_adopt_leaves_no_snapshot(self):
        p, r = self.adopted({test_adopt.REVIEW: test_adopt.BLOCKING})
        self.assertNotEqual(r.returncode, 0, r.stdout)
        out = self.review_dir(p)
        self.assertEqual((out / 'verdict').read_text().strip(), 'FAIL')
        self.assertFalse((out / 'review').exists())


if __name__ == '__main__':
    unittest.main()
