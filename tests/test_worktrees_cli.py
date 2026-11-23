"""forge worktrees rm (#147): removal by forge-ui's rules, against temp repositories with real worktrees.

The statuses are forge-ui's (`worktrees.compute`); these tests only check what the command does with each.
The run registry and the location list live under an isolated XDG_CONFIG_HOME.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE = str(ROOT / 'forge')
SCRIPT = ROOT / 'scripts' / 'forge-worktrees.py'

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


class Fixture(unittest.TestCase):
    """A repository, a plan run `r1` registered in the run registry, and the worktrees forge would leave in
    `<parent>/.forge-worktrees/r1/`: done (merged, finished), bad (an unmerged commit, failed), dirty, locked."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config = self.tmp / 'config'
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.config), FORGE_DATA_HOME=str(self.tmp / 'data'),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                        GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        self.base = self.tmp / 'repos'
        self.base.mkdir()
        self.repo = self.base / 'app'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'f.txt').write_text('one\n')
        git(self.repo, 'add', 'f.txt'); git(self.repo, 'commit', '-qm', 'base')
        self.root = self.base / '.forge-worktrees' / 'r1'
        self.root.mkdir(parents=True)
        for task in ('done', 'bad', 'dirty', 'locked'):
            git(self.repo, 'worktree', 'add', '-q', '-b', 'forge/r1/' + task, str(self.root / task))
        self.commit('done', 'd.txt'); git(self.repo, 'merge', '-q', '--ff-only', 'forge/r1/done')
        self.commit('bad', 'b.txt')
        (self.root / 'dirty' / 'scratch.txt').write_text('uncommitted\n')
        git(self.repo, 'worktree', 'lock', '--reason', 'on a usb disk', str(self.root / 'locked'))
        (self.root / 'orphan').mkdir()
        (self.root / 'orphan' / 'x.txt').write_text('left\n')
        self.plan = self.base / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text(''.join(f'{t}\t-\tlow\t{t}.txt\tsol\topus\t{t.upper()}\n'
                                                     for t in ('done', 'bad', 'dirty', 'locked')))
        (self.plan / 'run_id').write_text('r1\n')
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        for task, status in (('done', 'MERGED'), ('bad', 'FAIL'), ('dirty', 'FAIL'), ('locked', 'FAIL')):
            (self.plan / 'tasks' / task).mkdir()
            if status == 'MERGED':
                (self.plan / 'tasks' / task / 'merged').write_text('')
            else:
                (self.plan / 'tasks' / task / 'status').write_text(status + '\n')
        self.register()

    def register(self):
        registry = self.config / 'forge' / 'runs'
        done = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge_registry.py'), 'register', str(self.plan)],
                              capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(registry.exists() or any(self.config.rglob('*')), 'nothing registered')

    def commit(self, task, name):
        wt = self.root / task
        (wt / name).write_text(task + '\n')
        git(wt, 'add', name); git(wt, 'commit', '-qm', 'work on ' + task)

    def rm(self, *args, env=None):
        done = subprocess.run([FORGE, 'worktrees', 'rm', *map(str, args)], capture_output=True, text=True,
                              env=env or self.env, stdin=subprocess.DEVNULL, timeout=120)
        return done

    def rm_json(self, *args, env=None):
        done = self.rm(*args, '--json', env=env)
        return done.returncode, json.loads(done.stdout), done

    def branches(self):
        return git(self.repo, 'branch', '--format=%(refname:short)').split()


def by_path(report):
    return {Path(r['path']).name: r for r in report['rows']}


class RemoveTests(Fixture):
    def test_a_finished_worktree_is_removed_and_its_branch_stays_without_branches(self):
        code, report, _ = self.rm_json(self.root / 'done')
        self.assertEqual(code, 0)
        self.assertEqual(by_path(report)['done'], dict(path=str(self.root / 'done'), action='remove', status='removed', reason=None))
        self.assertFalse((self.root / 'done').exists())
        self.assertIn('forge/r1/done', self.branches())

    def test_branches_deletes_a_merged_branch_and_never_an_unmerged_one(self):
        code, report, _ = self.rm_json(self.root / 'done', self.root / 'bad', '--branches')
        rows = by_path(report)
        self.assertEqual(code, 0)
        self.assertEqual((rows['done']['action'], rows['done']['status']), ('remove+branch', 'removed'))
        self.assertEqual((rows['bad']['action'], rows['bad']['status']), ('remove', 'removed'))
        self.assertFalse((self.root / 'bad').exists())
        branches = self.branches()
        self.assertNotIn('forge/r1/done', branches)
        self.assertIn('forge/r1/bad', branches)       # its commit is nobody's else

    def test_dirty_locked_and_live_worktrees_are_skipped_with_their_reasons_and_stay(self):
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid()}))
        # with the run live, every one of its worktrees is in use
        code, report, _ = self.rm_json(self.root / 'done', self.root / 'dirty', self.root / 'locked')
        self.assertEqual(code, 0)
        for name in ('done', 'dirty', 'locked'):
            self.assertEqual((by_path(report)[name]['status'], by_path(report)[name]['action']), ('skipped', 'none'), name)
            self.assertIn('active', by_path(report)[name]['reason'])
            self.assertTrue((self.root / name).exists())
        (self.plan / 'scheduler.live').unlink()
        code, report, _ = self.rm_json(self.root / 'dirty', self.root / 'locked')
        rows = by_path(report)
        self.assertEqual(code, 0)
        self.assertIn('uncommitted', rows['dirty']['reason'])
        self.assertIn('locked', rows['locked']['reason'])
        self.assertTrue((self.root / 'dirty' / 'scratch.txt').exists())
        self.assertTrue((self.root / 'locked').exists())

    def test_a_leftover_folder_is_deleted(self):
        code, report, _ = self.rm_json(self.root / 'orphan')
        self.assertEqual(code, 0)
        self.assertEqual(by_path(report)['orphan'], dict(path=str(self.root / 'orphan'), action='delete-folder', status='removed', reason=None))
        self.assertFalse((self.root / 'orphan').exists())

    def test_a_missing_worktree_is_skipped_with_the_prune_hint(self):
        shutil.rmtree(self.root / 'bad')
        code, report, _ = self.rm_json(self.root / 'bad')
        self.assertEqual(code, 0)
        self.assertEqual(by_path(report)['bad']['status'], 'skipped')
        self.assertIn('git worktree prune', by_path(report)['bad']['reason'])

    def test_a_path_that_is_no_row_is_skipped(self):
        stranger = self.tmp / 'elsewhere'
        stranger.mkdir()
        code, report, _ = self.rm_json(stranger)
        self.assertEqual(code, 0)
        self.assertEqual(report['rows'], [dict(path=str(stranger), action='none', status='skipped',
                                               reason='not a worktree forge knows')])
        self.assertTrue(stranger.exists())

    def test_a_path_is_matched_by_its_realpath(self):
        link = self.tmp / 'link'
        os.symlink(self.root, link)
        code, report, _ = self.rm_json(link / 'done')
        self.assertEqual((code, by_path(report)['done']['status']), (0, 'removed'))
        self.assertFalse((self.root / 'done').exists())


class DryRunTests(Fixture):
    def test_a_dry_run_changes_nothing_and_says_would_remove(self):
        before = git(self.repo, 'worktree', 'list', '--porcelain')
        code, report, _ = self.rm_json(self.root / 'done', self.root / 'orphan', self.root / 'dirty', '--branches', '--dry-run')
        rows = by_path(report)
        self.assertEqual(code, 0)
        self.assertTrue(report['dry_run'])
        self.assertEqual((rows['done']['action'], rows['done']['status']), ('remove+branch', 'would-remove'))
        self.assertEqual((rows['orphan']['action'], rows['orphan']['status']), ('delete-folder', 'would-remove'))
        self.assertEqual(rows['dirty']['status'], 'skipped')
        self.assertEqual(git(self.repo, 'worktree', 'list', '--porcelain'), before)
        self.assertIn('forge/r1/done', self.branches())
        self.assertTrue((self.root / 'orphan').exists())
        locations = self.config / 'forge' / 'locations.jsonl'
        self.assertNotIn('"gone"', locations.read_text() if locations.exists() else '')


class FailureTests(Fixture):
    def test_a_failed_removal_gives_exit_4_and_the_other_rows_are_still_processed(self):
        # git cannot remove a worktree holding an untracked file inside a directory it cannot delete from
        stuck = self.root / 'bad' / 'sub'
        stuck.mkdir()
        (stuck / 'keep.txt').write_text('x\n')
        git(self.root / 'bad', 'add', 'sub/keep.txt'); git(self.root / 'bad', 'commit', '-qm', 'sub')
        os.chmod(stuck, 0o500)
        self.addCleanup(os.chmod, stuck, 0o700)
        if os.access(stuck / 'new', os.W_OK) or os.geteuid() == 0:
            self.skipTest('permissions do not stop this user')
        code, report, _ = self.rm_json(self.root / 'bad', self.root / 'done', self.root / 'orphan')
        rows = by_path(report)
        self.assertEqual(code, 4)
        self.assertFalse(report['ok'])
        self.assertEqual(rows['bad']['status'], 'failed')
        self.assertTrue(rows['bad']['reason'])
        self.assertEqual((rows['done']['status'], rows['orphan']['status']), ('removed', 'removed'))
        self.assertFalse((self.root / 'done').exists())

    def test_a_failed_branch_delete_is_a_failed_row(self):
        # git's own refusal cannot be provoked on a merged branch, so the git call is replaced
        import importlib.util
        spec = importlib.util.spec_from_file_location('forge_worktrees_cli_under_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        paths = [str(ROOT / 'scripts'), str(ROOT / 'ui')]
        sys.path[:0] = paths
        self.addCleanup(lambda: [sys.path.remove(p) for p in paths])
        spec.loader.exec_module(module)
        calls = []

        class Rm:
            forge_locations = type('L', (), {'gone': staticmethod(lambda *a, **k: True)})

            @staticmethod
            def git(repo, *args):
                calls.append(args)
                return (0, '', '') if args[0] == 'worktree' else (1, '', 'error: branch is busy\nmore')

            @staticmethod
            def event(run, name, **values):
                return dict(data=values)

            @staticmethod
            def record(*a, **k):
                return 1
        row = dict(path=str(self.root / 'done'), repo=str(self.repo), git_dir=str(self.repo), branch='forge/r1/done',
                   kind='task', run_id='r1', run_path=None, task='done', merged=True)
        failure = module.act(Rm, row, 'remove+branch', 'cli')
        self.assertIn('branch is busy', failure)
        self.assertEqual([c[:2] for c in calls], [('worktree', 'remove'), ('branch', '-d')])


class RecordTests(Fixture):
    def locations(self):
        text = (self.config / 'forge' / 'locations.jsonl')
        return [json.loads(line) for line in text.read_text().splitlines() if line.strip()] if text.exists() else []

    def test_a_removal_is_recorded_as_gone_with_the_actor(self):
        self.assertEqual(self.rm('--actor', 'ui', self.root / 'done').returncode, 0)
        gone = [e for e in self.locations() if e.get('op') == 'gone' and e.get('kind') == 'worktree']
        self.assertEqual([(e['path'], e['by']) for e in gone], [(str(self.root / 'done'), 'forge worktrees rm (ui)')])

    def test_the_default_actor_is_cli_and_the_event_carries_it(self):
        self.assertEqual(self.rm(self.root / 'orphan').returncode, 0)
        gone = [e for e in self.locations() if e.get('op') == 'gone']
        self.assertEqual([e['by'] for e in gone], ['forge worktrees rm (cli)'])
        done = subprocess.run([FORGE, 'dataset', 'worktrees', '--json'], capture_output=True, text=True, env=self.env)
        report = json.loads(done.stdout)
        removed = [e for e in report['events'] if e['name'] == 'worktree_removed']
        self.assertEqual([(e['path'], e.get('by')) for e in removed], [(str(self.root / 'orphan'), 'forge worktrees rm (cli)')])

    def test_a_skipped_row_records_nothing(self):
        self.assertEqual(self.rm(self.root / 'dirty').returncode, 0)
        self.assertEqual([e for e in self.locations() if e.get('op') == 'gone'], [])


class JsonAndUsageTests(Fixture):
    def test_the_json_shape_is_exact(self):
        code, report, done = self.rm_json(self.root / 'done', self.root / 'dirty', '--dry-run')
        self.assertEqual(set(report), {'ok', 'dry_run', 'rows'})
        for row in report['rows']:
            self.assertEqual(set(row), {'path', 'action', 'status', 'reason'})
            self.assertIn(row['action'], ('remove', 'remove+branch', 'delete-folder', 'none'))
            self.assertIn(row['status'], ('removed', 'skipped', 'failed', 'would-remove'))
        self.assertEqual(len(done.stdout.strip().splitlines()), 1)
        self.assertIs(report['ok'], True)
        self.assertIs(report['dry_run'], True)

    def test_text_output_is_one_line_per_path(self):
        done = self.rm(self.root / 'done', self.root / 'dirty')
        lines = done.stdout.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('removed'))
        self.assertTrue(lines[1].startswith('skipped'))

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.rm().returncode, 2)
        self.assertEqual(self.rm(self.root / 'done', '--nope').returncode, 2)
        self.assertEqual(self.rm(self.root / 'done', '--actor', 'robot').returncode, 2)
        self.assertTrue((self.root / 'done').exists())

    def test_the_launcher(self):
        done = subprocess.run([FORGE, 'worktrees', '--help'], capture_output=True, text=True, env=self.env)
        self.assertEqual(done.returncode, 0)
        self.assertIn('rm', done.stdout)
        bare = subprocess.run([FORGE, 'worktrees'], capture_output=True, text=True, env=self.env)
        self.assertEqual(bare.returncode, 2)
        helped = subprocess.run([FORGE, 'help'], capture_output=True, text=True, env=self.env)
        self.assertIn('  worktrees ', helped.stdout + helped.stderr)


class NoForceTests(unittest.TestCase):
    def test_the_command_never_forces_or_force_deletes(self):
        text = SCRIPT.read_text()
        self.assertNotIn('--force', text)
        self.assertNotIn("'-D'", text)
        self.assertNotIn(' -D ', text)


if __name__ == '__main__':
    unittest.main()
