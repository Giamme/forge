"""repo.identity: the repository a plan or solo run was created in (#99).

A run started from a linked worktree names the main checkout as its root, so the grouping
survives the worktree's removal. Uses the fake agent CLI of test_forge.py; its fixtures are
borrowed rather than inherited, so its tests do not run a second time here."""
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_forge as fixtures

ROOT = fixtures.ROOT


def real(path):
    return os.path.realpath(path)


class RepoIdentityTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script

    def linked(self, branch='fix/linked'):
        wt = self.root / 'wt'
        self.git('worktree', 'add', '-q', '-b', branch, str(wt))
        return wt

    def plan_in(self, checkout, name='plan'):
        p = self.root / name; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        t = p / 'tasks' / 'a'; t.mkdir(parents=True)
        (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', checkout, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def solo_in(self, checkout, run, *args):
        run.mkdir(exist_ok=True); (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        return self.run_script('forge-solo.sh', run, '--repo', checkout, '--dwarf', 'sol', *args)

    def identity(self, run_dir):
        return json.loads((run_dir / 'repo.identity').read_text())

    def write_identity(self, checkout, dir):
        """forge_repo_identity called directly, the way the runners call it."""
        script = ROOT / 'scripts' / 'forge-ui-options.sh'
        return subprocess.run(['bash', '-c', 'source "$1" && forge_repo_identity "$2" "$3"; echo "rc=$?"', 'bash',
                               str(script), str(checkout), str(dir)],
                              cwd=self.repo, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_a_plan_from_the_main_checkout_is_its_own_root(self):
        p = self.plan_in(self.repo)
        branch = self.git('symbolic-ref', '--short', 'HEAD').decode().strip()
        self.assertEqual(self.identity(p), {'root': real(self.repo), 'worktree': None, 'branch': branch})

    def test_a_plan_from_a_linked_worktree_names_the_main_checkout_as_root(self):
        wt = self.linked()
        p = self.plan_in(wt)
        self.assertEqual(self.identity(p), {'root': real(self.repo), 'worktree': real(wt), 'branch': 'fix/linked'})
        self.assertEqual((p / 'repo').read_text().strip(), str(wt))   # `repo` keeps meaning the checkout

    def test_a_detached_head_has_no_branch(self):
        wt = self.linked()
        self.git('-C', str(wt), 'checkout', '-q', '--detach')
        p = self.plan_in(wt)
        self.assertEqual(self.identity(p), {'root': real(self.repo), 'worktree': real(wt), 'branch': None})

    def test_a_solo_run_from_the_linked_worktree_records_the_same_identity_as_a_plan(self):
        wt = self.linked()
        p = self.plan_in(wt)
        run = self.root / 'solo'
        r = self.solo_in(wt, run)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.identity(run), self.identity(p))

    def test_a_dry_run_writes_no_identity(self):
        run = self.root / 'solo'
        r = self.solo_in(self.repo, run, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((run / 'repo.identity').exists())

    def test_a_plan_keeps_its_identity_and_gets_one_only_when_it_has_none(self):
        p = self.plan_in(self.repo)
        first = self.identity(p)
        self.git('checkout', '-q', '-b', 'moved')           # the checkout moves on
        r = self.run_script('forge-parallel.sh', 'plan', p, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.identity(p), first)

        (p / 'repo.identity').unlink()                      # a plan made before this change
        r = self.run_script('forge-parallel.sh', 'plan', p, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.identity(p), {'root': real(self.repo), 'worktree': None, 'branch': 'moved'})

    def test_the_writer_is_best_effort_and_returns_zero(self):
        plain = self.root / 'plain'; plain.mkdir()
        missing = self.root / 'missing'                     # never created: the write cannot happen
        for checkout, dir in ((plain, plain), (self.repo, missing)):
            r = self.write_identity(checkout, dir)
            self.assertEqual(r.stdout.strip(), 'rc=0')
            self.assertFalse((dir / 'repo.identity').exists())
        self.assertEqual(list(plain.iterdir()), [])

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root ignores directory permissions')
    def test_the_writer_into_a_read_only_dir_writes_nothing_and_returns_zero(self):
        ro = self.root / 'ro'; ro.mkdir(); ro.chmod(0o500)
        self.addCleanup(ro.chmod, 0o700)
        r = self.write_identity(self.repo, ro)
        self.assertEqual(r.stdout.strip(), 'rc=0')
        self.assertEqual(list(ro.iterdir()), [])
