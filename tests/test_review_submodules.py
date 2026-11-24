"""#255: a review snapshot has the submodules' files, so a reviewer can run tests that import them.

The snapshot is a fresh repository, so every gitlink came out an empty directory. Through the real
runner with the fake CLI: the reviewer reads the submodule's file at review time, is told it has it,
and the review still holds (the files are invisible to git, so no fingerprint changes)."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import re
import subprocess
import unittest

import test_forge as fixtures

ARTIFACT = fixtures.ROOT / 'scripts' / 'forge-artifact.sh'
FILLED = "Submodules filled in your checkout with the attempt's files: vendor/lib."


class ReviewSubmodulesTests(unittest.TestCase):
    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.env['FORGE_KEEP_SNAPSHOTS'] = '1'     # these tests look inside the snapshot after its review (#139)

    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario

    def add_submodule(self):
        # What `git submodule add` leaves, without its shell script (it needs commands the tests'
        # PATH does not have): a repository at vendor/lib, its gitlink, and .gitmodules.
        lib = self.repo / 'vendor' / 'lib'
        lib.mkdir(parents=True)
        run = lambda *args: subprocess.check_output(['git', *args], cwd=lib, env=self.env)  # noqa: E731
        run('init', '-q')
        (lib / 'marker').write_text('from the submodule\n')
        run('add', '.'); run('commit', '-qm', 'lib')
        (self.repo / '.gitmodules').write_text('[submodule "vendor/lib"]\n\tpath = vendor/lib\n'
                                               '\turl = https://invalid.invalid/lib\n')
        self.git('-c', 'advice.addEmbeddedRepo=false', 'add', '.gitmodules', 'vendor/lib')
        self.git('commit', '-qm', 'add the submodule')
        self.assertTrue(self.git('ls-files', '-s', 'vendor/lib').startswith(b'160000'))

    def plan_a(self):
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        t = p / 'tasks/a'; t.mkdir(parents=True)
        (t / 'prompt.md').write_text('Implement change. TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def test_a_task_reviewer_has_the_submodules_files_and_the_review_holds(self):
        self.add_submodule()
        self.env['READS'] = str(self.root / 'reads')
        self.scenario({'a.qa.*': {'verdict': 'PASS', 'reads': ['vendor/lib/marker']}})
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists(), r.stdout)
        self.assertEqual((self.root / 'reads').read_text(), 'from the submodule\n')
        snapshot = sorted((p / 'tasks/a').glob('review-*'))[-1]
        self.assertEqual(subprocess.check_output(['git', 'status', '--porcelain'], cwd=snapshot, env=self.env,
                                                 text=True), '')
        self.assertNotIn('vendor/lib', (p / 'tasks/a/changes.diff').read_text())
        self.assertIn(FILLED, (p / 'tasks/a/review.deps').read_text())
        self.assertIn(FILLED, (p / 'tasks/a/qa.input').read_text())

    def test_a_snapshot_without_submodules_adds_nothing_to_the_note(self):
        snapshot = self.root / 'snap'
        snapshot.mkdir()
        subprocess.check_call(['git', 'init', '-q'], cwd=snapshot, env=self.env)
        note = self.root / 'note'
        note.write_text('Dependencies in your checkout: none.\n')
        subprocess.check_call(['bash', '-c', 'source "$1"; forge_review_submodules "$2" "$3" "$4"', 'x',
                               str(ARTIFACT), str(self.repo), str(snapshot), str(note)], env=self.env)
        self.assertEqual(note.read_text(), 'Dependencies in your checkout: none.\n')

    def test_every_snapshot_with_a_dependency_note_fills_submodules_too(self):
        # Task, final, adopt and solo reviews: each forge_review_deps call is followed by this one.
        for name in ('forge-parallel.sh', 'forge-lib-review.sh', 'forge-lib-adopt.sh', 'forge-solo.sh'):
            text = (fixtures.ROOT / 'scripts' / name).read_text()
            calls = re.findall(r'^\s*forge_review_deps (.*)$', text, re.M)
            self.assertTrue(calls, name)
            for args in calls:
                with self.subTest(name=name, args=args):
                    self.assertRegex(text, r'forge_review_deps %s\n\s*forge_review_submodules %s\n'
                                     % (re.escape(args), re.escape(args)))


if __name__ == '__main__':
    unittest.main()
