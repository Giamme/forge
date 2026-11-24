"""Project memory and the user's checkout.

#156: forge's own untracked memory files (.forge/memory.md, .forge/ledger.tsv) do not make the
user's tree dirty for integrate, nor for the warning `plan` prints about uncommitted work.
Runs use test_forge.py's fake CLIs.

#159: a linked worktree whose .forge/ is not tracked keeps its memory in the main checkout, so
`git worktree remove` cannot take its learnings with it."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
MEMORY = str(ROOT / 'scripts' / 'forge-memory.sh')
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_memory_home  # noqa: E402


class IntegrateMemoryTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    plan = fixtures.ForgeTests.plan

    def forge_memory(self):
        d = self.repo / '.forge'
        d.mkdir(exist_ok=True)
        (d / 'memory.md').write_text('# project memory\n\n## Traps\n- a fact\n')
        (d / 'ledger.tsv').write_text('# ts\trun_id\ttask\trole\n')

    def test_forges_own_memory_files_do_not_block_integrate(self):
        self.scenario({})
        p = self.plan()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        self.forge_memory()
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('working tree is dirty', r.stdout)
        self.assertTrue((self.repo / 'a.txt').exists())
        self.assertTrue((self.repo / '.forge' / 'memory.md').exists())     # left as it was

    def test_a_change_of_the_users_own_still_blocks_and_is_named(self):
        self.scenario({})
        p = self.plan()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        self.forge_memory()
        (self.repo / 'notes.txt').write_text('mine\n')
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('working tree is dirty', r.stdout)
        self.assertIn('?? notes.txt', r.stdout)
        self.assertNotIn('?? .forge', r.stdout)

    def test_plan_does_not_warn_about_forges_memory_alone(self):
        self.forge_memory()
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks/a/prompt.md').write_text('Implement change. TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('has uncommitted changes', r.stdout)


class LinkedWorktreeMemoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.main = self.base / 'main'
        self.main.mkdir()
        self.env = dict(os.environ, FORGE_DATASET='off', FORGE_DATA_HOME=str(self.base / 'data'), FORGE_MEMORY='on',
                        XDG_CONFIG_HOME=str(self.base / 'cfg'), GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        for key in ('FORGE_MEMORY_DEEP_PRUNE', 'FORGE_MEMORY_PER_CHECKOUT'):
            self.env.pop(key, None)
        self.git(self.main, 'init', '-q')
        (self.main / 'a.py').write_text('x = 1\n')
        (self.main / '.gitignore').write_text('.forge/\n')
        self.git(self.main, 'add', '.')
        self.git(self.main, 'commit', '-qm', 'init')
        self.wt = self.base / 'wt'
        self.git(self.main, 'worktree', 'add', '-q', '-b', 'side', str(self.wt))

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                              env=self.env).stdout

    def memory(self, *args, env=None):
        return subprocess.run(['bash', MEMORY, *args], capture_output=True, text=True, env=env or self.env)

    def learn(self, repo, text, env=None):
        r = self.memory('add', str(repo), '--category', 'trap', '--text', text, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_learnings_of_a_linked_worktree_survive_its_removal(self):
        self.learn(self.wt, 'the generated client is rebuilt by make')
        self.assertFalse((self.wt / '.forge' / 'ledger.tsv').exists())
        self.assertIn('the generated client is rebuilt by make', (self.main / '.forge' / 'ledger.tsv').read_text())
        self.git(self.main, 'worktree', 'remove', str(self.wt))
        shown = self.memory('show', str(self.main))
        self.assertIn('the generated client is rebuilt by make', shown.stdout)

    def test_every_reader_agrees_on_the_main_checkout(self):
        self.learn(self.main, 'one fact from the main checkout')
        self.assertEqual(self.memory('where', str(self.wt)).stdout.strip(), str(self.main))
        self.assertEqual(self.memory('where', str(self.main)).stdout.strip(), str(self.main))
        self.assertIn('one fact from the main checkout', self.memory('show', str(self.wt)).stdout)
        self.assertIn('one fact from the main checkout', self.memory('inject', str(self.wt), 'dwarf').stdout)
        self.assertIn('one fact from the main checkout', self.memory('learnings', str(self.wt), '--json').stdout)
        self.assertEqual(forge_memory_home.memory_root(self.wt), self.main)
        self.assertEqual(forge_memory_home.memory_root(self.main), self.main)

    def test_a_tracked_forge_folder_stays_with_its_checkout(self):
        (self.wt / '.forge').mkdir()
        (self.wt / '.forge' / 'ledger.tsv').write_text(
            '# ts\trun_id\ttask\trole\tmodel\tverdict\tcategory\tkey\ttext\tduration_s\tinjectable\n')
        self.git(self.wt, 'add', '-f', '.forge/ledger.tsv')
        self.git(self.wt, 'commit', '-qm', 'track memory')
        self.learn(self.wt, 'a fact for this branch only')
        self.assertIn('a fact for this branch only', (self.wt / '.forge' / 'ledger.tsv').read_text())
        self.assertFalse((self.main / '.forge').exists())
        self.assertEqual(forge_memory_home.memory_root(self.wt), self.wt)

    def test_per_checkout_can_be_asked_for(self):
        env = dict(self.env, FORGE_MEMORY_PER_CHECKOUT='1')
        self.learn(self.wt, 'kept in the worktree', env=env)
        self.assertIn('kept in the worktree', (self.wt / '.forge' / 'ledger.tsv').read_text())
        self.assertFalse((self.main / '.forge').exists())

    def test_a_worktree_ledger_from_before_is_moved_once(self):
        self.learn(self.main, 'a main fact')
        (self.wt / '.forge').mkdir()
        main_rows = (self.main / '.forge' / 'ledger.tsv').read_text().splitlines()
        stranded = '2026-10-01T00:00:01Z\tr9\tt\tdwarf\tm\tPASS\ttrap\told worktree fact\told worktree fact\t1\t1'
        (self.wt / '.forge' / 'ledger.tsv').write_text('\n'.join([main_rows[0], main_rows[1], stranded]) + '\n')
        (self.wt / '.forge' / 'memory.md').write_text('stale\n')
        r = self.memory('show', str(self.wt))
        self.assertIn('1 learning row(s) of the linked worktree', r.stderr)
        self.assertIn('old worktree fact', r.stdout)
        self.assertIn('a main fact', r.stdout)
        rows = (self.main / '.forge' / 'ledger.tsv').read_text().splitlines()
        self.assertEqual(rows.count(main_rows[1]), 1)                  # a row already there is not added again
        self.assertIn(stranded, rows)
        self.assertTrue((self.wt / '.forge' / 'ledger.tsv.merged').exists())
        self.assertTrue((self.wt / '.forge' / 'memory.md.merged').exists())
        again = self.memory('show', str(self.wt))
        self.assertNotIn('linked worktree', again.stderr)

    def test_a_path_outside_git_is_its_own_home(self):
        plain = self.base / 'plain'
        plain.mkdir()
        self.assertEqual(self.memory('where', str(plain)).stdout.strip(), str(plain))
        self.assertEqual(forge_memory_home.memory_root(plain), plain)


if __name__ == '__main__':
    unittest.main()
