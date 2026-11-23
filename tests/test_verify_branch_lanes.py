"""#151: verification runs the lanes committed on the branch it verifies, not the repository root's copy,
and `forge-parallel.sh verify <plan>` re-runs a plan's verification without a `run`.

The helper that picks the source is checked directly; the rest goes through the real runner with
test_forge.py's fake CLIs."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
from pathlib import Path
import subprocess
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / 'scripts' / 'forge-lib-verify.sh'


class BranchLanesTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def lanes(self, **lanes):
        """Committed repository lanes in .forge/verify.d/, name -> command."""
        d = self.repo / '.forge' / 'verify.d'
        d.mkdir(parents=True, exist_ok=True)
        for name, command in lanes.items():
            (d / name).write_text(command + '\n')
        self.git('add', '.forge/verify.d')
        self.git('commit', '-qm', 'verification lanes')
        return d

    def one_task_plan(self):
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks/a/prompt.md').write_text('Implement change. TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def integration(self, p):
        return Path((p / 'wt_root').read_text().strip()) / '_integration'

    def in_wt(self, wt, *args):
        return subprocess.check_output(['git', *args], cwd=wt, env=self.env, text=True).strip()

    def source(self, plan, repo, checkout=''):
        out = subprocess.run(['bash', '-c', 'source "$0"; verify_source_for "$1" "$2" "$3"', str(LIB),
                              str(plan), str(repo), str(checkout)], capture_output=True, text=True, check=True).stdout
        return out.rstrip('\n').split('\t') if out else None

    def test_the_checkouts_committed_lanes_come_before_the_roots(self):
        self.lanes(unit='echo committed')
        wt = self.root / 'wt'
        self.git('worktree', 'add', '-q', '--detach', str(wt))
        plan = self.root / 'p'; plan.mkdir()
        self.assertEqual(self.source(plan, self.repo, wt), ['lanes:checkout', str(wt / '.forge' / 'verify.d')])
        # Without a checkout, or with the root itself as the checkout: the root's, as before.
        self.assertEqual(self.source(plan, self.repo)[0], 'lanes:repo')
        self.assertEqual(self.source(plan, self.repo, self.repo)[0], 'lanes:repo')
        # The plan's own lanes and command still come first.
        (plan / 'verify_cmd').write_text('true')
        self.assertEqual(self.source(plan, self.repo, wt)[0], 'command:plan')

    def test_untracked_lanes_in_a_checkout_do_not_count(self):
        (self.repo / '.forge' / 'verify.d').mkdir(parents=True)
        (self.repo / '.forge' / 'verify.d' / 'unit').write_text('echo root\n')
        wt = self.root / 'wt'
        self.git('worktree', 'add', '-q', '--detach', str(wt))
        (wt / '.forge' / 'verify.d').mkdir(parents=True)
        (wt / '.forge' / 'verify.d' / 'unit').write_text('echo stray\n')        # never committed
        plan = self.root / 'p'; plan.mkdir()
        self.assertEqual(self.source(plan, self.repo, wt), ['lanes:repo', str(self.repo / '.forge' / 'verify.d')])

    def test_a_committed_verify_command_in_the_checkout_counts_too(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'verify').write_text('echo committed\n')
        self.git('add', '.forge/verify'); self.git('commit', '-qm', 'verify')
        wt = self.root / 'wt'
        self.git('worktree', 'add', '-q', '--detach', str(wt))
        plan = self.root / 'p'; plan.mkdir()
        self.assertEqual(self.source(plan, self.repo, wt)[0], 'command:checkout')

    def test_an_uncommitted_edit_of_the_roots_lane_is_not_what_runs(self):
        # The issue's shape, reversed: the root's copy is stale (here: broken), the branch's is right.
        self.scenario({})
        lanes = self.lanes(unit='test -f a.txt', lint='echo clean')
        p = self.one_task_plan()
        (lanes / 'unit').write_text('echo stale-root-lane; exit 3\n')
        (lanes / 'extra').write_text('exit 9\n')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'verification.status').read_text().strip(), 'PASS')
        self.assertEqual(sorted((p / 'verification.command').read_text().splitlines()),
                         ['lint: echo clean', 'unit: test -f a.txt'])
        self.assertIn('verification runs the lanes committed on the branch it verifies', r.stdout)
        self.assertIn('changed on the branch: unit', r.stdout)
        self.assertIn('only in the repository root (not run): extra', r.stdout)

    def test_an_adopted_range_that_renames_a_suite_is_verified_with_its_own_lane(self):
        self.scenario({})
        self.lanes(chat='test -f suite-old.txt')
        (self.repo / 'suite-old.txt').write_text('old\n')
        self.git('add', 'suite-old.txt'); self.git('commit', '-qm', 'the old suite')
        p = self.one_task_plan()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        wt = self.integration(p)
        # One range renames the suite and updates the lane that names it, as in the issue.
        self.in_wt(wt, 'mv', 'suite-old.txt', 'suite-new.txt')
        (wt / '.forge' / 'verify.d' / 'chat').write_text('test -f suite-new.txt\n')
        self.in_wt(wt, 'add', '-A', '.'); self.in_wt(wt, 'commit', '-qm', 'rename the suite')
        r = self.run_script('forge-parallel.sh', 'adopt', p, '--head', '--reason', 'the suite rename')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'verification.status').read_text().strip(), 'PASS', r.stdout)
        self.assertEqual((p / 'verification.command').read_text().strip(), 'chat: test -f suite-new.txt')
        self.assertIn('changed on the branch: chat', r.stdout)

    def test_verify_reruns_the_plans_verification_without_dispatching(self):
        self.scenario({})
        marker = self.root / 'ready'
        self.lanes(unit=f'test -f {marker}')
        p = self.one_task_plan()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual((p / 'verification.status').read_text().strip(), 'FAIL')
        before = list(self.calls())
        marker.write_text('')
        r = self.run_script('forge-parallel.sh', 'verify', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'verification.status').read_text().strip(), 'PASS')
        self.assertIn('verification of ', r.stdout)
        self.assertEqual(self.calls(), before)                         # no model was asked
        marker.unlink()
        r = self.run_script('forge-parallel.sh', 'verify', p, '--verify-retries', '0')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual((p / 'verification.status').read_text().strip(), 'FAIL')
        self.assertEqual((p / 'verify_retries').read_text(), '0')

    def test_verify_refuses_a_plan_without_a_run_or_with_a_moved_branch(self):
        self.scenario({})
        self.lanes(unit='true')
        p = self.one_task_plan()
        r = self.run_script('forge-parallel.sh', 'verify', p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('has never been run', r.stdout)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        wt = self.integration(p)
        (wt / 'late.txt').write_text('late\n')
        self.in_wt(wt, 'add', 'late.txt'); self.in_wt(wt, 'commit', '-qm', 'late')
        r = self.run_script('forge-parallel.sh', 'verify', p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('adopt', r.stdout)
        r = self.run_script('forge-parallel.sh', 'verify', p, '--bogus')
        self.assertEqual(r.returncode, 2, r.stdout)


if __name__ == '__main__':
    unittest.main()
