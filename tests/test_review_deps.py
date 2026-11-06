"""#117: a reviewer gets the attempt's dependencies, is told what it has, and says whether its tests ran.

Through the real runners with the fake CLI: the link must hold the attempt's copy, void no review,
stay out of the reviewed change, and leave a repo with no dependency directories as it was."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import os
from pathlib import Path
import subprocess
import unittest

import test_forge as fixtures

SETUP = 'mkdir -p node_modules/dep && echo attempt > node_modules/dep/marker'
COULD_NOT_RUN = 'FORGE_TESTS: COULD NOT RUN — the browser suite needs a display'


class ReviewDepsTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    solo = fixtures.ForgeTests.solo

    def ignore_deps(self):
        (self.repo / '.gitignore').write_text('node_modules/\n')    # the usual pattern, which a link does not match
        self.git('add', '.'); self.git('commit', '-qm', 'ignore')

    def plan_a(self, *plan_args):
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        t = p / 'tasks/a'; t.mkdir(parents=True)
        (t / 'prompt.md').write_text('Implement change. TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *plan_args)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def snapshot(self, parent):
        found = sorted(Path(parent).glob('review-*'))
        self.assertTrue(found, 'no review snapshot in %s' % parent)
        return found[-1]

    def deps_note(self, prompt_file):
        """What the prompt itself tells the reviewer about its dependencies."""
        lines = [l for l in Path(prompt_file).read_text().splitlines() if l.startswith('Dependenc')]
        self.assertTrue(lines, 'the prompt says nothing about dependencies')
        return '\n'.join(lines)

    def link_in(self, make_source, make_snapshot=lambda snapshot: None):
        """forge_review_deps on two plain directories: (snapshot, the note)."""
        source, snapshot = self.root / 'src', self.root / 'snap'
        source.mkdir(); snapshot.mkdir(); (snapshot / '.git').mkdir()
        make_source(source); make_snapshot(snapshot)
        subprocess.check_call(['bash', '-c', 'source "$1"; forge_review_deps "$2" "$3" "$4"', 'x',
                               str(fixtures.ROOT / 'scripts/forge-artifact.sh'), str(source), str(snapshot),
                               str(self.root / 'note')], env=self.env)
        return snapshot, (self.root / 'note').read_text()

    def status(self, snapshot):
        return subprocess.check_output(['git', 'status', '--porcelain'], cwd=snapshot, env=self.env, text=True)

    def test_a_task_reviewer_has_the_attempts_dependencies_and_the_review_holds(self):
        self.ignore_deps()
        self.env['READS'] = str(self.root / 'reads')
        self.scenario({'a.qa.*': {'verdict': 'PASS', 'findings': COULD_NOT_RUN, 'reads': ['node_modules/dep/marker']}})
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p, '--setup', SETUP)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists(), r.stdout)
        snapshot = self.snapshot(p / 'tasks/a')
        self.assertEqual((self.root / 'reads').read_text(), 'attempt\n')   # the attempt's copy, at review time
        self.assertEqual(self.status(snapshot), '')                  # not part of the reviewed change
        self.assertNotIn('node_modules', (p / 'tasks/a/changes.diff').read_text())
        self.assertIn('node_modules', self.deps_note(p / 'tasks/a/qa.input'))
        self.assertIn('the browser suite needs a display', r.stdout)  # visible after the run

    def test_without_dependencies_nothing_is_linked_and_the_prompt_says_so(self):
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists(), r.stdout)
        self.assertFalse(os.path.lexists(self.snapshot(p / 'tasks/a') / 'node_modules'))
        note = self.deps_note(p / 'tasks/a/qa.input')
        self.assertIn('none', note)
        self.assertNotIn('node_modules', note)

    def test_a_committed_vendor_directory_stays_the_committed_one(self):
        (self.repo / 'vendor').mkdir(); (self.repo / 'vendor/lib.txt').write_text('committed\n')
        self.git('add', '.'); self.git('commit', '-qm', 'vendor')
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        snapshot = self.snapshot(p / 'tasks/a')
        self.assertFalse((snapshot / 'vendor').is_symlink())
        self.assertFalse(os.path.lexists(snapshot / 'vendor/vendor'))
        self.assertEqual((snapshot / 'vendor/lib.txt').read_text(), 'committed\n')
        note = self.deps_note(p / 'tasks/a/qa.input')
        self.assertIn('vendor', note)
        self.assertNotIn('none', note)                                # it would tell the reviewer it has nothing to run with

    def test_a_workspace_packages_dependencies_are_linked_too(self):
        self.ignore_deps()
        (self.repo / 'packages/web').mkdir(parents=True); (self.repo / 'packages/web/index.js').write_text('x\n')
        self.git('add', '.'); self.git('commit', '-qm', 'package')
        self.env['READS'] = str(self.root / 'reads')
        self.scenario({'a.qa.*': {'reads': ['packages/web/node_modules/dep/marker']}})
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p, '--setup',
                            'mkdir -p packages/web/node_modules/dep && echo nested > packages/web/node_modules/dep/marker')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.root / 'reads').read_text(), 'nested\n')
        self.assertEqual(self.status(self.snapshot(p / 'tasks/a')), '')
        self.assertIn('packages/web/node_modules', self.deps_note(p / 'tasks/a/qa.input'))

    def test_what_gets_linked(self):
        def source(root):
            (root / 'node_modules/inner/node_modules').mkdir(parents=True)     # inside another: the outer link covers it
            (root / 'a/b/c/d/node_modules').mkdir(parents=True)                # five levels: found
            (root / 'a/b/c/d/e/node_modules').mkdir(parents=True)              # six: not looked for
            (root / 'gone/node_modules').mkdir(parents=True)                   # its parent is not in the snapshot
        snapshot, note = self.link_in(source, lambda root: (root / 'a/b/c/d/e').mkdir(parents=True))
        self.assertTrue((snapshot / 'node_modules').is_symlink())
        self.assertTrue((snapshot / 'a/b/c/d/node_modules').is_symlink())
        self.assertFalse(os.path.lexists(snapshot / 'a/b/c/d/e/node_modules'))
        self.assertFalse(os.path.lexists(snapshot / 'gone'))
        self.assertEqual((snapshot / '.git/info/exclude').read_text().split(), ['/a/b/c/d/node_modules', '/node_modules'])
        self.assertIn('a/b/c/d/node_modules, node_modules', note)
        self.assertIn('complete', note)                                        # never claims it is

    def test_every_dependency_dir_fail_first_links_is_linked_for_the_reviewer_too(self):
        # One list in two languages: a name added to one and not the other gives the reviewer and the
        # fail-first check different checkouts. Mix's deps/_build and CocoaPods' Pods are among them (#127).
        spec = importlib.util.spec_from_file_location('ff', fixtures.ROOT / 'scripts/forge-fail-first.py')
        ff = importlib.util.module_from_spec(spec); spec.loader.exec_module(ff)
        self.assertTrue({'node_modules', 'deps', '_build', 'Pods'} <= set(ff.DEP_DIRS))
        snapshot, note = self.link_in(lambda root: [(root / name).mkdir() for name in ff.DEP_DIRS])
        self.assertEqual(sorted(p.name for p in snapshot.iterdir() if p.is_symlink()), sorted(ff.DEP_DIRS))
        for name in ff.DEP_DIRS:
            self.assertIn(name, note)

    def test_a_write_through_the_link_voids_no_review(self):
        self.ignore_deps()
        self.scenario({'a.qa.*': {'writes': {'node_modules/dep/cache': 'written by a test run'}}})
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p, '--setup', SETUP)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists(), r.stdout)
        snapshot = self.snapshot(p / 'tasks/a')
        self.assertTrue((snapshot / 'node_modules').is_symlink())     # so the write above went through it
        self.assertEqual(self.status(snapshot), '')

    def test_a_shadow_reviewer_has_what_the_primarys_prompt_says(self):
        self.ignore_deps()
        self.env['READS'] = str(self.root / 'reads')
        self.scenario({'a.shadow.*': {'reads': ['node_modules/dep/marker']}})
        p = self.plan_a('--shadow-qa', 'opus,sol', '--shadow-rate', '1')
        r = self.run_script('forge-parallel.sh', 'run', p, '--setup', SETUP)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.root / 'reads').read_text(), 'attempt\n')
        self.assertEqual(self.status(next(p.glob('shadow/a/*/review-snapshot'))), '')

    def test_a_final_reviewer_has_the_integration_worktrees_dependencies(self):
        self.ignore_deps()
        self.scenario({'a.qa.2': {'verdict': 'PASS', 'findings': COULD_NOT_RUN}})
        p = self.plan_a()
        r = self.run_script('forge-parallel.sh', 'run', p, '--setup', SETUP)
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.run_script('forge-parallel.sh', 'review', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('the browser suite needs a display', r.stdout)
        out = sorted(p.glob('final-review-*'))[-1]
        snapshot = self.snapshot(out)
        self.assertEqual((snapshot / 'node_modules/dep/marker').read_text(), 'attempt\n')
        self.assertEqual(self.status(snapshot), '')
        self.assertEqual((out / 'verdict').read_text().strip(), 'PASS')
        self.assertIn('node_modules', self.deps_note(out / 'qa.input'))

    def test_a_solo_reviewer_has_the_repos_dependencies(self):
        self.ignore_deps()
        (self.repo / 'node_modules/dep').mkdir(parents=True)
        (self.repo / 'node_modules/dep/marker').write_text('repo\n')
        self.scenario({'*.qa.*': {'verdict': 'PASS', 'findings': COULD_NOT_RUN}})
        r = self.solo('--qa', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('the browser suite needs a display', r.stdout)
        self.assertIn('node_modules', self.deps_note(self.root / 'solo/qa.input'))
        snapshot = self.snapshot(self.root / 'solo')
        self.assertEqual((snapshot / 'node_modules/dep/marker').read_text(), 'repo\n')
        self.assertEqual(self.status(snapshot), '')

    def test_the_prompt_says_when_forge_ran_no_tests_of_its_own(self):
        p = self.plan_a()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        self.assertIn('ran no tests of its own', (p / 'tasks/a/qa.input').read_text())

    def test_retry_reports_a_review_whose_tests_could_not_run(self):
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'},
                       'a.qa.2': {'verdict': 'PASS', 'findings': COULD_NOT_RUN}})
        p = self.plan_a()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('the browser suite needs a display', r.stdout)
        self.assertNotIn('decided.left', r.stdout)                    # no decision governs it, so no noise about one


if __name__ == '__main__':
    unittest.main()
