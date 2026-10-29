"""Submodule files in forge's worktrees: exported before setup, invisible to every capture.

An end-to-end run with the fake CLIs of test_forge.py, in a repo with an initialized local
submodule whose URL points nowhere (a fetch would fail).
"""
import os
from pathlib import Path
import subprocess
import unittest

import test_forge

NOWHERE = 'https://invalid.invalid/x'


class SubmoduleRunTests(unittest.TestCase):
 plan = test_forge.ForgeTests.plan
 scenario = test_forge.ForgeTests.scenario
 run_script = test_forge.ForgeTests.run_script

 def setUp(self):
  test_forge.ForgeTests.setUp(self)
  self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
  self.env.pop('GIT_DIR', None)

 def git(self, *args, cwd=None):
  return subprocess.check_output(['git', *args], cwd=cwd or self.repo, env=self.env, text=True)

 def add_submodule(self):
  src = self.root / 'subsrc'; src.mkdir()
  self.git('init', '-q', cwd=src); (src / 'lib.txt').write_text('lib\n')
  self.git('add', '.', cwd=src); self.git('commit', '-qm', 'lib', cwd=src)
  self.git('-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(src), 'vendor/x')
  self.git('config', '-f', '.gitmodules', 'submodule.vendor/x.url', NOWHERE)
  self.git('config', 'submodule.vendor/x.url', NOWHERE)
  self.git('add', '.'); self.git('commit', '-qm', 'submodule')

 def gitlink_without_files(self):
  # A gitlink whose commit exists nowhere offline and whose directory holds nothing.
  self.git('update-index', '--add', '--cacheinfo', '160000,' + '1' * 40 + ',vendor/gone')
  self.git('commit', '-qm', 'gitlink')

 def worktrees(self, p):
  return (p / 'wt_root').read_text().strip()

 def test_task_worktree_gets_files_before_setup_and_captures_ignore_them(self):
  self.add_submodule()
  self.scenario({'a.dwarf.*': {'files': {'vendor/x/lib.txt': 'edited\n', 'normal.txt': 'n\n'}}})
  seen = self.root / 'seen.txt'
  p = self.plan()
  r = self.run_script('forge-parallel.sh', 'run', p, '--setup', f'cp vendor/x/lib.txt {seen}',
                      '--verify', 'test -f vendor/x/lib.txt')
  self.assertEqual(r.returncode, 0, r.stdout)
  self.assertEqual(seen.read_text(), 'lib\n')
  line = (p / 'tasks/a/submodules.txt').read_text().split('\t')
  self.assertEqual(line[:2], ['exported', 'vendor/x'])
  self.assertTrue((p / 'tasks/_integration/submodules.txt').exists())
  diff = (p / 'tasks/a/changes.diff').read_text()
  self.assertIn('normal.txt', diff); self.assertNotIn('vendor/x/lib.txt', diff); self.assertNotIn('edited', diff)
  self.assertEqual((p / 'tasks/a/merged').exists(), True)
  files = self.git('show', '--name-only', '--format=', 'forge/' + (p / 'run_id').read_text().strip() + '/a').split()
  self.assertIn('normal.txt', files); self.assertNotIn('vendor/x/lib.txt', files)
  self.assertFalse((Path(self.worktrees(p)) / 'a').exists())
  capsule = (p / 'tasks/a/baseline.capsule').read_text()
  self.assertIn('- `vendor/x` is a submodule: its files are here to read and build with. '
                'Changes there are not part of your change and are never committed or reviewed.', capsule)

 def test_integrate_populates_candidate_and_removes_it(self):
  self.add_submodule()
  p = self.plan()
  r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'test -f vendor/x/lib.txt')
  self.assertEqual(r.returncode, 0, r.stdout)
  r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
  self.assertEqual(r.returncode, 0, r.stdout)
  out = next(p.glob('integrate-*'))
  self.assertEqual((out / 'submodules.txt').read_text().split('\t')[0], 'exported')
  self.assertFalse(list(Path(self.worktrees(p)).glob('_candidate-*')))
  self.assertTrue((self.repo / 'b.txt').exists())

 def test_missing_submodule_is_noted_and_in_the_capsule(self):
  self.gitlink_without_files()
  p = self.plan()
  r = self.run_script('forge-parallel.sh', 'run', p)
  self.assertEqual(r.returncode, 0, r.stdout)
  self.assertEqual((p / 'tasks/a/submodules.txt').read_text().split('\t')[0], 'missing')
  self.assertIn('forge: _integration: submodule vendor/gone is not available in this worktree', r.stdout)
  self.assertIn('forge: a: submodule vendor/gone is not available in this worktree', (p / 'tasks/a/task.out').read_text())
  self.assertIn('- Submodule `vendor/gone` is not available in this worktree (its directory is empty).',
                (p / 'tasks/a/baseline.capsule').read_text())

 def test_no_submodules_changes_nothing(self):
  p = self.plan()
  r = self.run_script('forge-parallel.sh', 'run', p)
  self.assertEqual(r.returncode, 0, r.stdout)
  self.assertEqual(list(p.rglob('submodules.*')), [])
  self.assertNotIn('submodule', r.stdout)
  capsule = (p / 'tasks/a/baseline.capsule').read_text()
  self.assertNotIn('submodule', capsule)
  self.assertTrue(capsule.rstrip().endswith("- Implementation must respect other tasks' ownership; report necessary scope changes."))


if __name__ == '__main__':
 unittest.main()
