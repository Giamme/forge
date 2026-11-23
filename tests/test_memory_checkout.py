"""Project memory and the user's checkout.

#156: forge's own untracked memory files (.forge/memory.md, .forge/ledger.tsv) do not make the
user's tree dirty for integrate, nor for the warning `plan` prints about uncommitted work.
Runs use test_forge.py's fake CLIs."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import unittest

import test_forge as fixtures


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


if __name__ == '__main__':
    unittest.main()
