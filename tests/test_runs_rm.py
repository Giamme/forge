"""`forge runs rm`: deleting a run keeps its traces, and touches nothing that is not the run's.

Every test builds its own HOME, config home, data home, git repo and plan dir. The command runs as a
subprocess, the way `forge runs rm` does, except the confirmation prompt, which needs a terminal.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_registry  # noqa: E402
from forge_dataset import importer  # noqa: E402

RM = ROOT / 'scripts' / 'forge-runs-rm.py'
RUNS = ROOT / 'scripts' / 'forge-runs.py'
SCHEDULE = ROOT / 'scripts' / 'forge-schedule.py'
FRACTAL = ROOT / 'scripts' / 'forge-fractal.py'
RUN_ID = 'myrun'


def load_module():
    spec = importlib.util.spec_from_file_location('forge_runs_rm_under_test', RM)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RmCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.config = self.root / 'config'
        self.data = self.root / 'data'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.config), FORGE_DATA_HOME=str(self.data),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t',
                        GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        for name in ('FORGE_DATASET', 'FORGE_RUNS_RM_PAUSE_AFTER_LOCK', 'GIT_DIR', 'GIT_WORK_TREE'):
            self.env.pop(name, None)
        self.repo = self.root / 'work' / 'project'
        self.wt_root = self.root / 'work' / '.forge-worktrees' / RUN_ID
        self.repo.mkdir(parents=True)
        self.git('init', '-q', '-b', 'main')
        (self.repo / 'a.txt').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-q', '-m', 'base')
        self.plan = self.make_plan()

    # ---- fixtures

    def git(self, *args, cwd=None, check=True):
        done = subprocess.run(['git', *args], cwd=cwd or self.repo, env=self.env, capture_output=True, text=True)
        if check:
            self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def worktree(self, name, branch, base=None, root=None):
        path = (root or self.wt_root) / name
        self.git('worktree', 'add', '-q', '-b', branch, str(path), *([base] if base else []))
        return path

    def make_plan(self, name=RUN_ID, tasks=('a', 'b'), register=True):
        plan = self.root / 'runs' / name
        (plan / 'tasks').mkdir(parents=True)
        (plan / 'tasks.tsv').write_text(''.join('%s\t-\tlow\t%s.txt\tsol\topus\tTask %s\n' % (t, t, t) for t in tasks))
        (plan / 'repo').write_text(str(self.repo) + '\n')
        (plan / 'run_id').write_text(name + '\n')
        (plan / 'wt_root').write_text(str(self.wt_root) + '\n')
        (plan / 'logs').mkdir()
        (plan / 'logs' / 'plan.log').write_text('planned\n' * 100)
        review = plan / 'review-1-1'
        review.mkdir()
        (review / 'diff.patch').write_bytes(b'x' * (4 << 20))
        for task in tasks:
            tdir = plan / 'tasks' / task
            tdir.mkdir()
            (tdir / 'status').write_text('PASS\n')
            (tdir / 'attempt').write_text('1\n')
            for role, at, extra in (('task', 100.0, {}), ('dwarf', 101.0, {'dwarf.resolved': 'alias=sol\nharness=codex\n'}),
                                    ('qa', 102.0, {'qa.resolved': 'alias=opus\n', 'qa.last': 'FORGE_VERDICT: PASS'})):
                attempt = tdir / 'attempts' / ('%s-%032x' % (role, int(at * 1000)))
                attempt.mkdir(parents=True)
                (attempt / 'metrics.json').write_text(json.dumps(dict(
                    role=role, wall_started=at, exit_code=0, harness='pipeline' if role == 'task' else 'codex',
                    elapsed_s=dict(total=5))))
                for file, body in extra.items():
                    (attempt / file).write_text(body)
        if register:
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config)}):
                forge_registry.register_run_dir(self.repo, plan)
        return plan

    def make_solo(self, name='solo-run'):
        run = self.root / 'runs' / name
        run.mkdir(parents=True)
        (run / 'prompt.md').write_text('do it\n')
        (run / 'repo').write_text(str(self.repo) + '\n')
        (run / 'notes.txt').write_text('notes\n')
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config)}):
            forge_registry.register_run_dir(self.repo, run)
        return run

    def make_worktrees(self):
        """a: merged, clean. b: one commit ahead (UNMERGED), clean. integration: a merged branch with no
        worktree. foreign: another branch, same root."""
        self.wt_root.mkdir(parents=True, exist_ok=True)
        self.git('branch', 'forge/%s/a' % RUN_ID)
        self.git('worktree', 'add', '-q', str(self.wt_root / 'a'), 'forge/%s/a' % RUN_ID)
        ahead = self.worktree('b', 'forge/%s/b' % RUN_ID)
        (ahead / 'b.txt').write_text('ahead\n')
        self.git('add', '.', cwd=ahead)
        self.git('commit', '-q', '-m', 'ahead', cwd=ahead)
        self.git('branch', 'forge/%s-integration' % RUN_ID)
        self.worktree('foreign', 'other/feature')
        return {'a': self.wt_root / 'a', 'b': self.wt_root / 'b', 'foreign': self.wt_root / 'foreign'}

    # ---- running and looking

    def rm(self, *args, env=None, cwd=None, stdin=subprocess.DEVNULL):
        return subprocess.run([sys.executable, str(RUNS), 'rm', *map(str, args)], env=env or self.env,
                              cwd=cwd or self.root, capture_output=True, text=True, stdin=stdin, timeout=120)

    def rmj(self, *args, **kwargs):
        done = self.rm(*args, '--json', **kwargs)
        return done, json.loads(done.stdout)

    def tree(self, path):
        """Every path below `path` with what would change if anything did, the lock files aside."""
        found = {}
        for directory, subdirs, files in os.walk(path):
            for name in subdirs + files:
                full = Path(directory) / name
                if name in ('schedule.lock', 'pipeline.lock'):
                    continue
                info = full.lstat()
                found[str(full.relative_to(path))] = (stat.S_IFMT(info.st_mode), info.st_size if stat.S_ISREG(info.st_mode) else 0)
        return found

    def registered(self):
        found = []
        for path in sorted((self.config / 'forge' / 'runs').glob('*.txt')):
            found += [x for x in path.read_text().splitlines() if x and not x.startswith('#')]
        return found

    def rows(self, where='1'):
        path = self.data / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            return db.execute('SELECT kind, rid, run, run_id, mode, data FROM events WHERE %s ORDER BY rid' % where).fetchall()
        finally:
            db.close()

    def steps(self, result):
        return {s['step']: s for s in result['steps']}

    def worktree_paths(self):
        return [line[len('worktree '):] for line in self.git('worktree', 'list', '--porcelain').splitlines()
                if line.startswith('worktree ')]

    def branches(self):
        return self.git('for-each-ref', '--format=%(refname:short)', 'refs/heads/').split()


class DryRunTests(RmCase):
    def test_a_dry_run_changes_nothing_and_reports_every_step_branch_and_worktree(self):
        self.make_worktrees()
        before = (self.tree(self.plan), self.tree(self.wt_root), self.registered(), self.worktree_paths(), self.branches())
        done, result = self.rmj(self.plan, '--dry-run', '--worktrees', '--branches')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((result['dry_run'], result['ok'], result['run_id'], result['run_dir']),
                         (True, True, RUN_ID, str(self.plan)))
        self.assertEqual([s['step'] for s in result['steps']],
                         ['check', 'lock', 'preserve', 'archive', 'intent', 'worktrees', 'branches', 'remove', 'record', 'unregister'])
        self.assertTrue(all(s['ok'] and isinstance(s['detail'], str) for s in result['steps']), result['steps'])
        self.assertEqual({b['name']: b['status'] for b in result['branches']},
                         {'forge/%s/a' % RUN_ID: 'merged', 'forge/%s/b' % RUN_ID: 'UNMERGED',
                          'forge/%s-integration' % RUN_ID: 'merged'})
        kinds = {Path(w['path']).name: (w['kind'], w['dirty']) for w in result['worktrees']}
        self.assertEqual(kinds, {'a': ('run', False), 'b': ('run', False), 'foreign': ('foreign', None)})
        self.assertTrue(result['size_bytes'] > 4 << 20 and result['archive_estimate_bytes'] < 1 << 20)
        self.assertEqual(Path(result['archive']).parent, self.data / 'archive')
        self.assertEqual(result['bytes_freed'], 0)
        after = (self.tree(self.plan), self.tree(self.wt_root), self.registered(), self.worktree_paths(), self.branches())
        self.assertEqual(before, after)
        self.assertFalse(self.data.exists(), 'a dry run writes no dataset and no archive')
        self.assertFalse((self.plan / 'schedule.lock').exists(), 'and does not even create the lock file')

    def test_a_dry_run_says_what_a_dirty_worktree_costs(self):
        wts = self.make_worktrees()
        (wts['a'] / 'scratch.txt').write_text('uncommitted\n')
        done, result = self.rmj(self.plan, '--dry-run', '--worktrees')
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertFalse(result['ok'])
        steps = self.steps(result)
        self.assertFalse(steps['remove']['ok'])
        self.assertIn('dirty', steps['remove']['detail'])
        self.assertEqual({Path(w['path']).name: w['dirty'] for w in result['worktrees']},
                         {'a': True, 'b': False, 'foreign': None})

    def test_the_text_report_lists_the_same_things(self):
        self.make_worktrees()
        done = self.rm(self.plan, '--dry-run')
        self.assertEqual(done.returncode, 0, done.stderr)
        for expected in ('preserve', 'archive', 'unregister', 'UNMERGED', 'merged', 'foreign', 'dry run, nothing was changed'):
            self.assertIn(expected, done.stdout)


class DeleteTests(RmCase):
    def test_a_real_delete_archives_removes_unregisters_and_leaves_the_foreign_worktree(self):
        self.make_worktrees()
        size = sum(f.stat().st_size for f in self.plan.rglob('*') if f.is_file())
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(result['ok'], result)
        self.assertEqual([s['step'] for s in result['steps']],
                         ['check', 'lock', 'preserve', 'archive', 'intent', 'worktrees', 'branches', 'remove', 'record', 'unregister'])
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.registered(), [])
        self.assertEqual(result['bytes_freed'], size)
        archive = Path(result['archive'])
        self.assertEqual(archive.parent, self.data / 'archive')
        self.assertRegex(archive.name, r'^%s-\d{8}-\d{6}(-\d+)?\.tar\.gz$' % RUN_ID)
        with tarfile.open(archive) as bundle:
            names = bundle.getnames()
        self.assertFalse([n for n in names if 'review-1-1' in n], names)
        self.assertIn('%s/tasks.tsv' % RUN_ID, names)
        self.assertEqual(result['archive_bytes'], archive.stat().st_size)
        self.assertTrue(self.wt_root.joinpath('foreign').is_dir(), 'the foreign worktree is not the run\'s')
        self.assertEqual(self.branches().count('other/feature'), 1)
        # without --worktrees and --branches the run's own are left where they are
        self.assertEqual(sorted(self.branches()), sorted(['main', 'other/feature', 'forge/%s/a' % RUN_ID,
                                                          'forge/%s/b' % RUN_ID, 'forge/%s-integration' % RUN_ID]))

    def test_importing_the_archive_afterwards_adds_nothing(self):
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 0, done.stderr)
        before = self.rows()
        self.assertTrue(len([r for r in before if r[0] not in ('event',)]) > 0, before)
        with patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.data)}):
            imported = importer.import_run(result['archive'])
        self.assertEqual(imported['new'], 0, imported)
        self.assertTrue(imported['events'] > 0)
        self.assertEqual(self.rows(), before)

    def test_both_intent_events_carry_the_identity_read_before_the_removal(self):
        uid = 'a' * 32
        (self.plan / 'dataset.uid').write_text(uid + '\n')
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 0, done.stderr)
        events = {json.loads(r[5])['name']: r for r in self.rows("kind = 'event'")}
        for name in ('run_delete_started', 'run_deleted'):
            self.assertEqual((events[name][2], events[name][3], events[name][4]), (uid, RUN_ID, 'parallel'), name)
        started, deleted = (json.loads(events[n][5]) for n in ('run_delete_started', 'run_deleted'))
        self.assertEqual((started['path'], started['archive']), (str(self.plan), result['archive']))
        self.assertEqual((deleted['path'], deleted['archive'], deleted['bytes_freed'], deleted['archive_bytes']),
                         (str(self.plan), result['archive'], result['bytes_freed'], result['archive_bytes']))

    def test_worktrees_and_branches_go_when_asked_and_the_foreign_ones_stay(self):
        wts = self.make_worktrees()
        done, result = self.rmj(self.plan, '--yes', '--worktrees', '--branches')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.plan.exists())
        self.assertFalse(wts['a'].exists() or wts['b'].exists())
        self.assertTrue(wts['foreign'].is_dir())
        self.assertEqual(sorted(self.branches()), ['main', 'other/feature'])
        self.assertEqual(sorted(p for p in self.worktree_paths() if '.forge-worktrees' in p), [str(wts['foreign'])])
        self.assertTrue(all(b['deleted'] for b in result['branches']), result['branches'])
        unmerged = [b for b in result['branches'] if b['status'] == 'UNMERGED'][0]
        self.assertIn(unmerged['sha'], unmerged['note'], 'what a deleted branch pointed at is reported')

    def test_an_emptied_worktree_root_is_removed_with_the_worktrees(self):
        self.git('branch', 'forge/%s/a' % RUN_ID)
        self.wt_root.mkdir(parents=True)
        self.git('worktree', 'add', '-q', str(self.wt_root / 'a'), 'forge/%s/a' % RUN_ID)
        done, _ = self.rmj(self.plan, '--yes', '--worktrees', '--branches')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.wt_root.exists())

    def test_a_branch_checked_out_in_a_remaining_worktree_is_kept(self):
        wts = self.make_worktrees()
        (wts['b'] / 'scratch.txt').write_text('dirty\n')   # stays, so forge/<id>/b stays checked out
        done, result = self.rmj(self.plan, '--yes', '--worktrees', '--branches')
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertIn('forge/%s/b' % RUN_ID, self.branches())
        kept = [b for b in result['branches'] if b['name'] == 'forge/%s/b' % RUN_ID][0]
        self.assertFalse(kept['deleted'])
        self.assertIn('checked out', kept['note'])

    def test_no_archive_writes_none_but_still_preserves(self):
        done, result = self.rmj(self.plan, '--yes', '--no-archive')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(result['archive'])
        self.assertFalse((self.data / 'archive').exists())
        self.assertTrue(self.rows("kind != 'event'"))
        self.assertIn('--no-archive', self.steps(result)['archive']['detail'])

    def test_dataset_off_deletes_with_a_loud_warning_and_no_store(self):
        done, result = self.rmj(self.plan, '--yes', env=dict(self.env, FORGE_DATASET='off'))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('WARNING', done.stderr)
        self.assertIn('NOT be kept', done.stderr)
        self.assertIn('NOT', self.steps(result)['preserve']['detail'])
        self.assertFalse((self.data / 'dataset').exists())
        self.assertTrue(Path(result['archive']).is_file(), 'the archive is still written')
        self.assertFalse(self.plan.exists())

    def test_a_solo_run_is_deleted_under_its_pipeline_lock(self):
        run = self.make_solo()
        done, result = self.rmj(run, '--yes')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(run.exists())
        self.assertEqual(self.registered(), [str(self.plan)])
        self.assertEqual(self.steps(result)['lock']['detail'], 'holding pipeline.lock')

    def test_it_is_reachable_through_the_forge_command(self):
        done = subprocess.run([str(ROOT / 'forge'), 'runs', 'rm', str(self.plan), '--dry-run', '--json'], env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, cwd=self.root, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)['run_id'], RUN_ID)

    def test_listing_is_unchanged(self):
        done = subprocess.run([sys.executable, str(RUNS), '--repo', str(self.repo), '--json'], env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([r['path'] for r in json.loads(done.stdout)], [str(self.plan)])


class StopsBeforeDeletingTests(RmCase):
    def test_a_dataset_that_cannot_take_the_run_stops_everything(self):
        blocker = self.root / 'blocked'
        blocker.write_text('a file where the data home should be')
        before = self.tree(self.plan)
        done, result = self.rmj(self.plan, '--yes', env=dict(self.env, FORGE_DATA_HOME=str(blocker)))
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertFalse(result['ok'])
        steps = self.steps(result)
        self.assertFalse(steps['preserve']['ok'])
        self.assertIn('nothing was deleted', steps['preserve']['detail'])
        self.assertNotIn('remove', steps)
        self.assertEqual(self.tree(self.plan), before)
        self.assertEqual(self.registered(), [str(self.plan)])

    def test_a_read_only_data_home_stops_everything_too(self):
        if os.geteuid() == 0:
            self.skipTest('permissions do not stop root')
        self.data.mkdir()
        os.chmod(self.data, 0o500)
        self.addCleanup(os.chmod, self.data, 0o700)
        before = self.tree(self.plan)
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertFalse(self.steps(result)['preserve']['ok'])
        self.assertEqual(self.tree(self.plan), before)
        self.assertEqual(self.registered(), [str(self.plan)])

    def test_a_failed_archive_stops_everything(self):
        self.data.mkdir()
        (self.data / 'archive').write_text('a file where the archive dir should be')
        before = self.tree(self.plan)
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 4, done.stderr)
        steps = self.steps(result)
        self.assertTrue(steps['preserve']['ok'])
        self.assertFalse(steps['archive']['ok'])
        self.assertNotIn('intent', steps)
        self.assertEqual(self.tree(self.plan), before)
        self.assertEqual(self.registered(), [str(self.plan)])

    def test_the_destinations_must_not_be_inside_the_run_dir(self):
        before = self.tree(self.plan)
        for label, env in (('data home', dict(self.env, FORGE_DATA_HOME=str(self.plan / 'data'))),
                           ('symlinked data home', None)):
            if env is None:
                os.symlink(self.plan / 'logs', self.root / 'link')
                env = dict(self.env, FORGE_DATA_HOME=str(self.root / 'link'))
            with self.subTest(label):
                done, result = self.rmj(self.plan, '--yes', env=env)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn('inside the run dir', result['steps'][0]['detail'])
                self.assertEqual(self.tree(self.plan), before)
        # the archive destination matters only when an archive is written
        done, _ = self.rmj(self.plan, '--yes', '--no-archive', '--dry-run', env=dict(self.env, FORGE_DATASET='off',
                                                                                    FORGE_DATA_HOME=str(self.plan / 'data')))
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_a_dirty_worktree_is_reported_and_not_removed_and_keeps_the_run_dir(self):
        wts = self.make_worktrees()
        (wts['a'] / 'scratch.txt').write_text('uncommitted\n')
        done, result = self.rmj(self.plan, '--yes', '--worktrees')
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertFalse(result['ok'])
        self.assertTrue((wts['a'] / 'scratch.txt').is_file(), 'the dirty worktree and its change are untouched')
        self.assertFalse(wts['b'].exists(), 'the clean one is removed')
        self.assertTrue(wts['foreign'].is_dir())
        self.assertTrue(self.plan.is_dir(), 'the run dir is kept as well')
        self.assertEqual(self.registered(), [str(self.plan)])
        steps = self.steps(result)
        self.assertFalse(steps['remove']['ok'])
        self.assertIn('dirty', steps['remove']['detail'])
        self.assertIn(str(wts['a']), steps['remove']['detail'])
        dirty = [w for w in result['worktrees'] if Path(w['path']).name == 'a'][0]
        self.assertEqual((dirty['kind'], dirty['dirty'], dirty['removed']), ('run', True, False))
        # committing the change lets a rerun finish the job
        self.git('add', '.', cwd=wts['a'])
        self.git('commit', '-q', '-m', 'keep', cwd=wts['a'])
        done, result = self.rmj(self.plan, '--yes', '--worktrees')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.plan.exists())

    def test_an_untracked_file_alone_makes_a_worktree_dirty(self):
        wts = self.make_worktrees()
        (wts['b'] / 'new.txt').write_text('untracked\n')
        done, result = self.rmj(self.plan, '--dry-run')
        self.assertEqual({Path(w['path']).name: w['dirty'] for w in result['worktrees']},
                         {'a': False, 'b': True, 'foreign': None})

    def test_a_forged_wt_root_file_is_ignored(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        self.git('branch', 'forge/%s/a' % RUN_ID)
        self.wt_root.mkdir(parents=True)
        genuine = self.worktree('b', 'forge/%s/b' % RUN_ID)
        decoy = elsewhere / 'a'
        self.git('worktree', 'add', '-q', str(decoy), 'forge/%s/a' % RUN_ID)   # the run's branch, not the run's place
        (self.plan / 'wt_root').write_text(str(elsewhere) + '\n')
        done, result = self.rmj(self.plan, '--yes', '--worktrees', '--branches')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(decoy.is_dir(), 'a worktree outside <repo>/../.forge-worktrees/<run id>/ is never removed')
        self.assertFalse(genuine.exists())
        by_name = {Path(w['path']).name: w for w in result['worktrees']}
        self.assertEqual((by_name['a']['kind'], by_name['b']['kind']), ('foreign', 'run'))
        self.assertIn('forge/%s/a' % RUN_ID, self.branches(), 'and its branch stays: it is checked out there')


class RefusalTests(RmCase):
    def assertRefused(self, path, *args, code=2, env=None, expect=None):
        before = self.tree(self.plan)
        done, result = self.rmj(path, '--yes', *args, env=env)
        self.assertEqual(done.returncode, code, done.stderr)
        self.assertFalse(result['ok'])
        self.assertIn('steps', result)
        if expect:
            self.assertIn(expect, ' '.join(s['detail'] for s in result['steps']))
        self.assertEqual(self.tree(self.plan), before)
        return result

    def test_the_repo_home_and_a_git_dir_are_refused(self):
        (self.home / 'tasks.tsv').write_text('a\t-\n')
        (self.home / 'repo').write_text(str(self.repo) + '\n')
        self.assertRefused(self.repo)
        self.assertRefused(self.repo / '.git')
        self.assertRefused(self.home, expect='home directory')
        self.assertRefused('/', expect='home directory')
        self.assertTrue((self.repo / '.git').is_dir() and (self.home / 'tasks.tsv').is_file())

    def test_a_dir_containing_the_repo_or_the_home_is_refused(self):
        for container, expect in ((self.repo.parent, 'repo'), (self.root, 'home directory')):
            (container / 'tasks.tsv').write_text('a\t-\n')
            (container / 'repo').write_text(str(self.repo) + '\n')
            with self.subTest(str(container)):
                self.assertRefused(container, expect=expect)

    def test_a_run_dir_with_a_git_dir_at_its_top_is_refused(self):
        (self.plan / '.git').mkdir()
        self.assertRefused(self.plan, expect='.git')
        self.assertTrue(self.plan.is_dir())

    def test_a_symlink_to_a_run_is_refused_and_so_is_a_dir_that_is_not_a_run(self):
        link = self.root / 'link'
        os.symlink(self.plan, link)
        self.assertRefused(link, expect='symbolic link')
        self.assertRefused(str(link) + '/', expect='symbolic link')
        self.assertRefused(self.plan / 'logs', expect='not a forge run dir')
        self.assertRefused(self.plan / 'tasks.tsv', expect='not a directory')

    def test_an_unsafe_run_id_is_refused(self):
        for text in ('../escape', '-rf', 'a b', '.hidden', 'x' * 129, 'a/b'):
            (self.plan / 'run_id').write_text(text + '\n')
            with self.subTest(text):
                self.assertRefused(self.plan, expect='run id')
        (self.plan / 'run_id').unlink()   # no run_id file: the basename is used, and it is safe
        odd = self.root / 'runs' / 'myrun\n'   # `$` would let a trailing newline through
        self.plan.rename(odd)
        self.assertRefused(odd, expect='run id')
        odd.rename(self.plan)
        done, result = self.rmj(self.plan, '--dry-run')
        self.assertEqual((done.returncode, result['run_id']), (0, RUN_ID))

    def test_a_plan_without_a_usable_repo_file_is_refused(self):
        for text in (None, 'relative/repo', ''):
            if text is None:
                (self.plan / 'repo').unlink()
            else:
                (self.plan / 'repo').write_text(text + '\n')
            with self.subTest(text):
                self.assertRefused(self.plan, expect='repo')

    def test_without_a_terminal_nothing_is_deleted_unless_yes_is_given(self):
        before = self.tree(self.plan)
        done = self.rm(self.plan)
        self.assertEqual(done.returncode, 2)
        self.assertIn('pass --yes', done.stderr)
        done, result = self.rmj(self.plan)
        self.assertEqual(done.returncode, 2)
        self.assertIn('pass --yes', result['steps'][0]['detail'])
        self.assertEqual(self.tree(self.plan), before)
        self.assertEqual(self.registered(), [str(self.plan)])

    def test_bad_arguments_still_print_one_json_object(self):
        for args in (['--json', '--bogus', str(self.plan)], ['--json']):
            with self.subTest(args):
                done = self.rm(*args)
                self.assertEqual(done.returncode, 2)
                result = json.loads(done.stdout)
                self.assertFalse(result['ok'])
                self.assertIn('usage', result['steps'][0]['detail'])

    def test_worktree_cleanup_is_refused_when_git_cannot_say_what_there_is(self):
        (self.plan / 'repo').write_text(str(self.root / 'no-such-repo') + '\n')
        self.assertRefused(self.plan, '--worktrees', expect='cannot clean up')
        done, result = self.rmj(self.plan, '--dry-run')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('unknown', self.steps(result)['check']['detail'])


class ExclusionTests(RmCase):
    def hold(self, path):
        child = subprocess.Popen([sys.executable, '-c', 'import fcntl, sys\nf = open(sys.argv[1], "a")\n'
                                  'fcntl.flock(f, fcntl.LOCK_EX)\nprint("held", flush=True)\nsys.stdin.read()', str(path)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (child.stdin.close(), child.wait(10), child.stdout.close()))
        self.assertEqual(child.stdout.readline().strip(), 'held')
        return child

    def test_a_held_schedule_lock_is_busy_and_changes_nothing(self):
        self.hold(self.plan / 'schedule.lock')
        before = self.tree(self.plan)
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertFalse(result['ok'])
        self.assertIn('schedule.lock', result['steps'][-1]['detail'])
        self.assertEqual(self.tree(self.plan), before)
        self.assertEqual(self.registered(), [str(self.plan)])
        self.assertFalse(self.data.exists(), 'not even the dataset is touched')

    def test_a_held_pipeline_lock_is_busy_for_a_solo_run(self):
        run = self.make_solo()
        self.hold(run / 'pipeline.lock')
        done, result = self.rmj(run, '--yes')
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertTrue(run.is_dir())

    def test_a_live_scheduler_is_active_even_when_nothing_holds_the_lock(self):
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid(), 'started': time.time()}))
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn('scheduler.live', result['steps'][-1]['detail'])
        self.assertTrue(self.plan.is_dir())
        done, _ = self.rmj(self.plan, '--dry-run')
        self.assertEqual(done.returncode, 3, 'a dry run predicts it')
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': 2 ** 22 + 12345}))   # a dead one does not count
        done, _ = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 0, done.stderr)

    def paused(self, run, lock_command):
        marker = self.root / 'paused'
        env = dict(self.env, FORGE_RUNS_RM_PAUSE_AFTER_LOCK=str(marker))
        child = subprocess.Popen([sys.executable, str(RUNS), 'rm', str(run), '--yes', '--json'], env=env, cwd=self.root,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.monotonic() + 30
        while not marker.exists():
            if child.poll() is not None:
                self.fail('rm exited before taking the lock: %r' % (child.communicate(),))
            self.assertLess(time.monotonic(), deadline, 'rm never took the lock')
            time.sleep(0.02)
        attempt = subprocess.run(lock_command, capture_output=True, text=True, timeout=30)
        marker.unlink()
        out, err = child.communicate(timeout=60)
        return attempt, child.returncode, json.loads(out)

    def test_while_rm_holds_the_lock_a_plan_cannot_start(self):
        script = self.root / 'noop.sh'
        script.write_text('exit 0\n')
        attempt, code, result = self.paused(self.plan, [sys.executable, str(SCHEDULE), 'lock', str(script), 'run', str(self.plan)])
        self.assertEqual(attempt.returncode, 3, attempt.stderr)
        self.assertIn('forge scheduler', attempt.stderr)
        self.assertEqual(code, 0, result)
        self.assertFalse(self.plan.exists())
        again = subprocess.run([sys.executable, str(SCHEDULE), 'lock', str(script), 'run', str(self.plan)],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(again.returncode, 3, 'and once it is gone it cannot be started either')
        self.assertFalse(self.plan.exists(), 'the failed start did not recreate the dir')

    def test_while_rm_holds_the_lock_a_solo_run_cannot_start(self):
        run = self.make_solo()
        script = self.root / 'noop.sh'
        script.write_text('exit 0\n')
        attempt, code, result = self.paused(run, [sys.executable, str(FRACTAL), '_lock', str(run), str(script)])
        self.assertEqual(attempt.returncode, 3, attempt.stderr)
        self.assertEqual(code, 0, result)
        self.assertFalse(run.exists())


class PartialFailureTests(RmCase):
    def setUp(self):
        super().setUp()
        if os.geteuid() == 0:
            self.skipTest('permissions do not stop root')

    def test_a_failed_removal_keeps_the_dir_identifiable_and_a_rerun_finishes_it(self):
        (self.plan / 'dataset.uid').write_text('b' * 32 + '\n')
        stuck = self.plan / 'tasks' / 'a' / 'stuck'
        stuck.mkdir()
        (stuck / 'file').write_text('x')
        os.chmod(stuck, 0o500)   # a file inside cannot be unlinked
        self.addCleanup(lambda: stuck.exists() and os.chmod(stuck, 0o700))
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 4, done.stderr)
        steps = self.steps(result)
        self.assertTrue(steps['preserve']['ok'] and steps['archive']['ok'] and steps['intent']['ok'])
        self.assertFalse(steps['remove']['ok'])
        self.assertTrue((self.plan / 'tasks.tsv').is_file() and (self.plan / 'repo').is_file())
        self.assertTrue((self.plan / 'dataset.uid').is_file(), 'the run keeps its dataset identity for the rerun')
        self.assertFalse((self.plan / 'logs').exists(), 'what could go did go')
        self.assertEqual(self.registered(), [str(self.plan)], 'still registered: it still exists')
        self.assertEqual(result['bytes_freed'], 0)
        os.chmod(stuck, 0o700)
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(self.plan.exists())
        self.assertEqual(self.registered(), [])

    def test_a_registry_that_cannot_be_rewritten_is_reported_and_a_rerun_repairs_it(self):
        directory = self.config / 'forge' / 'runs'
        os.chmod(directory, 0o500)
        self.addCleanup(os.chmod, directory, 0o700)
        done, result = self.rmj(self.plan, '--yes')
        self.assertEqual(done.returncode, 4, done.stderr)
        self.assertFalse(self.plan.exists(), 'the run itself is gone')
        step = self.steps(result)['unregister']
        self.assertFalse(step['ok'])
        self.assertIn('removed, registry needs repair: rerun `forge runs rm %s`' % self.plan, step['detail'])
        self.assertEqual(self.registered(), [str(self.plan)])
        os.chmod(directory, 0o700)
        done, result = self.rmj(self.plan)   # no --yes: a path that no longer exists only unregisters
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.registered(), [])
        self.assertEqual([s['step'] for s in result['steps']], ['check', 'unregister'])

    def test_a_run_that_is_already_gone_only_unregisters(self):
        ghost = self.root / 'runs' / 'ghost'
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config)}):
            forge_registry.register_run_dir(self.repo, ghost)
        self.assertIn(str(ghost), self.registered())
        before = self.rows()
        done, result = self.rmj(ghost, '--dry-run')
        self.assertEqual(done.returncode, 0)
        self.assertIn(str(ghost), self.registered())
        done, result = self.rmj(ghost)
        self.assertEqual((done.returncode, result['ok']), (0, True), done.stderr)
        self.assertNotIn(str(ghost), self.registered())
        self.assertIn(str(self.plan), self.registered())
        self.assertEqual(self.rows(), before)
        self.assertFalse(self.data.exists())


class ConfirmationTests(RmCase):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    def run_in_process(self, answer, *args):
        module = load_module()
        stderr = io.StringIO()
        with patch.dict(os.environ, self.env), patch('sys.stdin', self.Terminal(answer)), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = module.main([str(self.plan), *args])
        return code, stderr.getvalue()

    def test_on_a_terminal_the_run_id_must_be_typed(self):
        before = self.tree(self.plan)
        code, prompt = self.run_in_process('nope\n')
        self.assertEqual(code, 2)
        self.assertIn('type the run id to delete', prompt)
        self.assertEqual(self.tree(self.plan), before)
        self.assertEqual(self.rows(), [], 'declining stores nothing')
        code, _ = self.run_in_process(RUN_ID + '\n')
        self.assertEqual(code, 0)
        self.assertFalse(self.plan.exists())

    def test_the_prompt_is_not_asked_for_a_dry_run(self):
        code, prompt = self.run_in_process('', '--dry-run')
        self.assertEqual((code, prompt), (0, ''))


if __name__ == '__main__':
    unittest.main()
