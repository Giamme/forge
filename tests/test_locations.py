"""forge's location list (scripts/forge_locations.py): every run dir and worktree forge created, written whatever
the dataset says, and read by `forge du`, `forge runs compact` and forge-ui as the index of where to look."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_locations  # noqa: E402
import forge_storage  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.tmp / 'config')})
        self.env.start(); self.addCleanup(self.env.stop)
        os.environ.pop('FORGE_RUN_REGISTRY', None)
        forge_locations._IDENTITY.clear()
        self.repo = self.tmp / 'work' / 'app'
        self.repo.mkdir(parents=True)
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'base')

    def run_dir(self, name='p1', checkout=None):
        run = self.tmp / 'runs' / name
        run.mkdir(parents=True)
        (run / 'repo').write_text(str(checkout or self.repo) + '\n')
        (run / 'run_id').write_text(name + '\n')
        (run / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        return run

    def worktree(self, run_id, name):
        path = self.tmp / 'work' / '.forge-worktrees' / run_id / name
        git(self.repo, 'worktree', 'add', '-q', '-b', 'forge/%s/%s' % (run_id, name), str(path))
        return path


class RecordTests(Case):
    def test_adds_and_gones_fold_by_path_and_a_bad_line_is_skipped(self):
        wt = self.tmp / 'work' / '.forge-worktrees' / 'p1' / 'a'
        self.assertTrue(forge_locations.add_worktree(wt, str(self.repo), run_id='p1', branch='forge/p1/a', wt_kind='task', task='a'))
        with open(forge_locations.file_path(), 'a') as stream:
            stream.write('{"half a line\n')
        forge_locations.gone('worktree', wt)
        [entry] = forge_locations.entries('worktree', present=False)
        self.assertIsNotNone(entry['gone'])
        self.assertEqual(forge_locations.entries('worktree'), [])
        forge_locations.add_worktree(wt, str(self.repo), run_id='p1', branch='forge/p1/a', wt_kind='task', task='a')
        [entry] = forge_locations.entries('worktree')
        self.assertIsNone(entry['gone'])
        self.assertEqual((entry['git'], entry['root'], entry['task']), (str(self.repo / '.git'), str(self.repo), 'a'))

    def test_a_checkout_through_a_symlink_names_the_real_repository(self):
        link = self.tmp / 'link'
        link.symlink_to(self.repo)
        run = self.run_dir(checkout=link)
        forge_locations.add_run(run)
        [entry] = forge_locations.entries('run')
        self.assertEqual((entry['checkout'], entry['root'], entry['git']), (str(self.repo), str(self.repo), str(self.repo / '.git')))

    def test_a_linked_checkout_names_its_main_working_tree(self):
        other = self.tmp / 'work' / 'second'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'side', str(other))
        forge_locations.add_run(self.run_dir(checkout=other))
        [entry] = forge_locations.entries('run')
        self.assertEqual((entry['checkout'], entry['root']), (str(other), str(self.repo)))

    def test_off_with_the_run_registry_and_never_raises(self):
        with patch.dict(os.environ, {'FORGE_RUN_REGISTRY': 'off'}):
            self.assertFalse(forge_locations.add_run(self.run_dir()))
        self.assertFalse(forge_locations.file_path().exists())
        (self.tmp / 'config').write_text('a file where the config home should be')
        self.assertFalse(forge_locations.gone('run', self.tmp / 'x'))

    def test_a_held_lock_delays_a_write_but_never_stops_it(self):
        forge_locations.config_dir().mkdir(parents=True)
        with open(forge_locations.config_dir() / 'locations.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            started = time.monotonic()
            self.assertTrue(forge_locations.add_run(self.run_dir()))
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(forge_locations.entries('run')), 1)


class WriterTests(Case):
    def test_registering_a_run_records_it_and_unregistering_marks_it_gone(self):
        run = self.run_dir()
        subprocess.run([sys.executable, '-I', str(ROOT / 'scripts' / 'forge_registry.py'), 'register', str(run)], check=True,
                       env=dict(os.environ))
        [entry] = forge_locations.entries('run')
        self.assertEqual((entry['path'], entry['root']), (str(run), str(self.repo)))
        import forge_registry
        forge_registry.unregister_run_dir(run)
        self.assertEqual(forge_locations.entries('run'), [])

    def test_the_cli_never_fails_the_caller(self):
        for argv in ([], ['worktree', 'created'], ['run', '/nope'], ['worktree', 'created', '/r', '/c', '/p', '-', 'task']):
            done = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts' / 'forge_locations.py'), *argv], capture_output=True,
                                  env=dict(os.environ))
            self.assertEqual((done.returncode, done.stderr), (0, b''), argv)


class ImportTests(Case):
    def test_the_registry_and_the_worktrees_on_disk_go_in_once(self):
        run = self.run_dir()
        import forge_registry
        forge_registry.register_run_dir(self.repo, run)
        forge_locations.file_path().unlink()            # as before the list existed: only the registry knew
        task = self.worktree('p1', 'a')
        self.worktree('p1', 'not-a-task')               # forge's folder, not one of the run's tasks: not imported
        result = forge_locations.ensure_imported()
        self.assertEqual(result, {'runs': 1, 'worktrees': 1})
        [wt] = forge_locations.entries('worktree')
        self.assertEqual((wt['path'], wt['wt_kind'], wt['task']), (str(task), 'task', 'a'))
        self.assertIsNone(forge_locations.ensure_imported())
        forge_locations.ensure_imported(force=True)
        self.assertEqual(len(forge_locations.entries()), 2)    # nothing twice


class ReaderTests(Case):
    def test_storage_finds_a_run_and_a_worktree_folder_only_the_list_knows(self):
        run = self.run_dir()
        forge_locations.add_run(run)
        wt = self.worktree('p1', 'a')
        forge_locations.add_worktree(wt, str(self.repo), run_id='p1', branch='forge/p1/a', wt_kind='task', task='a')
        shutil.rmtree(run)                               # the run dir is gone; its worktree folder is not
        other = self.run_dir('p2')
        forge_locations.add_run(other)
        self.assertEqual(forge_storage.run_dirs(self.repo), [(str(other), str(self.repo))])
        report = forge_storage.report(self.repo)
        self.assertEqual([w['path'] for w in report['worktrees']], [str(wt.parent)])
        self.assertEqual(forge_storage.run_dirs(self.tmp / 'elsewhere'), [])


class RunTests(unittest.TestCase):
    """Through the real runner: a task's worktree is recorded when it is made and marked gone when the merged
    task's worktree is removed, with the dataset off as well."""
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario

    def test_a_task_worktree_is_recorded_and_marked_gone_after_its_merge(self):
        config = self.root / 'config'
        self.env.update(XDG_CONFIG_HOME=str(config), FORGE_DATASET='off')
        self.env.pop('FORGE_RUN_REGISTRY', None)
        self.scenario({})
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks/a/prompt.md').write_text('Implement change. TASK_a')
        self.assertEqual(self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory').returncode, 0)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config)}):
            found = forge_locations.load()
        runs = [e for (k, _), e in found.items() if k == 'run']
        self.assertEqual([e['path'] for e in runs], [os.path.realpath(p)])
        task = next(e for (k, _), e in found.items() if k == 'worktree' and e.get('task') == 'a')
        self.assertEqual((task['wt_kind'], task['branch'], task['root']), ('task', 'forge/plan/a', os.path.realpath(self.repo)))
        self.assertIsNotNone(task['gone'])               # merged: its worktree was removed
        integration = next(e for (k, _), e in found.items() if k == 'worktree' and e.get('wt_kind') == 'integration')
        self.assertIsNone(integration['gone'])


if __name__ == '__main__':
    unittest.main()
