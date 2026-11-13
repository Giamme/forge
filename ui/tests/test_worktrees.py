"""The Worktrees screen's rows (#125): each status against a real repository, the commands it offers, the
dataset's history, and /api/worktrees (caching, refresh, read-only, the CLI it asks)."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forge_ui import reader, worktrees

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


class Fixture(unittest.TestCase):
    """A repository whose path holds a space, one plan run `r1` with tasks done, bad, dirty, locked and gone, and
    the worktrees forge would have left in `<parent>/.forge-worktrees/r1/`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # forge's location list lives in the config home: never the developer's
        config = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.tmp / 'config')})
        config.start(); self.addCleanup(config.stop)
        self.base = Path(os.path.realpath(self.tmp)) / 'my repos'
        self.base.mkdir()
        self.repo = self.base / 'app'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'f.txt').write_text('one\n')
        git(self.repo, 'add', 'f.txt'); git(self.repo, 'commit', '-qm', 'base')
        self.root = self.base / '.forge-worktrees' / 'r1'
        self.root.mkdir(parents=True)
        git(self.repo, 'branch', 'forge/r1-integration')
        self.add('_integration', 'forge/r1-integration', new=False)
        for task in ('done', 'bad', 'dirty', 'locked', 'gone'):
            self.add(task, 'forge/r1/' + task)
        # done is merged into the user's branch; bad has a commit nobody merged
        self.commit('done', 'd.txt'); git(self.repo, 'merge', '-q', '--ff-only', 'forge/r1/done')
        self.commit('bad', 'b.txt')
        (self.root / 'dirty' / 'scratch.txt').write_text('uncommitted\n')
        git(self.repo, 'worktree', 'lock', '--reason', 'on a usb disk', str(self.root / 'locked'))
        shutil.rmtree(self.root / 'gone')
        # in the run's folder but on a branch that is not the run's, and one directly in .forge-worktrees
        self.add('stranger', 'feature/x')
        git(self.repo, 'worktree', 'add', '-q', '-b', 'mine', str(self.base / '.forge-worktrees' / 'mine'))
        # a folder git does not list
        (self.root / 'orphan').mkdir()
        (self.root / 'orphan' / 'x.txt').write_text('left\n')
        self.plan = self.base / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text(''.join(f'{t}\t-\tlow\t{t}.txt\tsol\topus\t{t.upper()}\n'
                                                     for t in ('done', 'bad', 'dirty', 'locked', 'gone', 'later')))
        (self.plan / 'run_id').write_text('r1\n')
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        (self.plan / 'accepted.integration').write_text(git(self.repo, 'rev-parse', 'forge/r1-integration') + '\n')
        for task, status in (('done', 'MERGED'), ('bad', 'FAIL'), ('dirty', 'FAIL'), ('locked', 'FAIL'), ('gone', 'FAIL'), ('later', 'PENDING')):
            (self.plan / 'tasks' / task).mkdir()
            if status == 'MERGED':
                (self.plan / 'tasks' / task / 'merged').write_text('')
            else:
                (self.plan / 'tasks' / task / 'status').write_text(status + '\n')
        self.catalog = reader.Catalog(plans=[self.plan], registry=False)

    def add(self, name, branch, new=True):
        args = ['worktree', 'add', '-q'] + (['-b', branch, str(self.root / name)] if new else [str(self.root / name), branch])
        git(self.repo, *args)

    def commit(self, task, name):
        wt = self.root / task
        (wt / name).write_text(task + '\n')
        git(wt, 'add', name); git(wt, 'commit', '-qm', 'work on ' + task)

    def rows(self, history=None):
        found = worktrees.compute(self.catalog, history)
        return {Path(r['path']).name: r for r in found['rows']}, found


class StatusTests(Fixture):
    def test_each_worktree_gets_the_status_of_its_state(self):
        rows, _ = self.rows()
        got = {name: rows[name]['status'] for name in rows}
        self.assertEqual(got, {
            'done': 'finished', 'bad': 'failed', 'dirty': 'dirty', 'locked': 'locked', 'gone': 'missing',
            '_integration': 'needed', 'stranger': 'foreign', 'mine': 'foreign', 'orphan': 'unknown'})
        self.assertEqual((rows['done']['merged'], rows['bad']['merged']), (True, False))
        self.assertEqual(rows['locked']['locked'], 'on a usb disk')
        self.assertEqual(rows['bad']['commit']['subject'], 'work on bad')
        self.assertEqual((rows['done']['kind'], rows['_integration']['kind'], rows['stranger']['kind']), ('task', 'integration', None))

    def test_a_locked_worktree_whose_folder_is_gone_stays_locked(self):
        shutil.rmtree(self.root / 'locked')                     # the usb disk is unplugged
        rows, _ = self.rows()
        self.assertEqual(rows['locked']['status'], 'locked')
        self.assertEqual(rows['locked']['commands'], [])        # prune would do nothing for it

    def test_an_active_run_holds_every_worktree_it_owns(self):
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid()}))
        rows, _ = self.rows()
        for name in ('done', 'bad', '_integration'):
            self.assertEqual(rows[name]['status'], 'in_use', name)
            self.assertEqual(rows[name]['commands'], [], name)
        self.assertEqual(rows['stranger']['status'], 'foreign')

    def test_an_integrated_run_frees_its_integration_worktree(self):
        history = dict(enabled=True, since=None, events=[], attempts=[], repos=[],
                       integrated=[dict(repo_path=str(self.repo), run_id='r1')])
        rows, _ = self.rows(history)
        self.assertEqual(rows['_integration']['status'], 'finished')

    def test_a_run_dir_that_is_gone_is_judged_from_the_dataset_and_git(self):
        shutil.rmtree(self.plan)
        history = dict(enabled=True, since=None, events=[], integrated=[], repos=[dict(repo='k', path=str(self.repo))],
                       attempts=[dict(repo_path=str(self.repo), run_id='r1', task=t) for t in ('done', 'bad')])
        rows, _ = self.rows(history)
        self.assertEqual((rows['done']['status'], rows['bad']['status']), ('finished', 'failed'))
        # dirty had no attempt on record, but its folder and branch carry forge's names for the run's task dirty
        self.assertEqual((rows['dirty']['kind'], rows['dirty']['status']), ('task', 'dirty'))
        self.assertEqual(rows['stranger']['status'], 'foreign')


class CommandTests(Fixture):
    def test_each_status_offers_its_command_quoted_and_never_forced(self):
        rows, found = self.rows()
        q = worktrees.quote
        repo = q(self.repo)
        self.assertEqual([c['command'] for c in rows['done']['commands']], [
            f'git -C {repo} worktree remove {q(self.root / "done")}', f'git -C {repo} branch -d forge/r1/done'])
        self.assertEqual([c['command'] for c in rows['bad']['commands']], [f'git -C {repo} worktree remove {q(self.root / "bad")}'])
        self.assertIn('branch stays', rows['bad']['note'])
        self.assertEqual([c['command'] for c in rows['dirty']['commands']], [f'git -C {q(self.root / "dirty")} status'])
        self.assertEqual([c['command'] for c in rows['gone']['commands']], [f'git -C {repo} worktree prune'])
        for name in ('locked', '_integration', 'stranger', 'mine'):
            self.assertEqual(rows[name]['commands'], [], name)
        self.assertIn("'", repo)                          # the space in the path is quoted
        everything = json.dumps(found)
        self.assertNotIn('--force', everything)
        self.assertNotIn(' -f ', everything)
        self.assertEqual([g['command'] for g in found['runs']], [f'forge runs rm {q(self.plan)} --worktrees --branches --dry-run'])

    def test_running_a_shown_command_changes_the_row_at_the_next_read(self):
        rows, _ = self.rows()
        for row in (rows['done'], rows['bad'], rows['gone']):
            for c in row['commands']:
                subprocess.run(c['command'], shell=True, check=True, capture_output=True, env=GIT_ENV)
        after, _ = self.rows()
        # pruned or removed: git forgets them, and the run dir still says they were there
        self.assertEqual({after[n]['status'] for n in ('done', 'bad', 'gone')}, {'removed'})
        self.assertTrue(all(after[n]['derived'] for n in ('done', 'bad', 'gone')))
        self.assertEqual(after['dirty']['status'], 'dirty')
        self.assertNotIn('forge/r1/done', git(self.repo, 'branch', '--list', 'forge/*'))
        self.assertIn('forge/r1/bad', git(self.repo, 'branch', '--list', 'forge/*'))


class HistoryTests(Fixture):
    def test_events_date_the_rows_and_name_what_was_removed(self):
        gone = str(self.root / 'old-task')
        outside = str(self.root / 'pruned')
        history = dict(enabled=True, since=100.0, integrated=[], repos=[], attempts=[], events=[
            dict(name='worktree_created', ts=100.0, run='u', run_id='r1', task='done', path=str(self.root / 'done'),
                 branch='forge/r1/done', kind='task', repo_path=str(self.repo), by=None),
            dict(name='worktree_created', ts=110.0, run='u', run_id='r1', task='old-task', path=gone,
                 branch='forge/r1/old-task', kind='task', repo_path=str(self.repo), by=None),
            dict(name='worktree_removed', ts=120.0, run='u', run_id='r1', task='old-task', path=gone,
                 branch='forge/r1/old-task', kind='task', repo_path=str(self.repo), by=None),
            dict(name='worktree_created', ts=130.0, run='u', run_id='r1', task='pruned', path=outside,
                 branch='forge/r1/pruned', kind='task', repo_path=str(self.repo), by=None)])
        rows, found = self.rows(history)
        self.assertEqual((rows['done']['status'], rows['done']['created']), ('finished', 100.0))
        self.assertEqual((rows['old-task']['status'], rows['old-task']['created'], rows['old-task']['removed']), ('removed', 110.0, 120.0))
        self.assertEqual((rows['pruned']['status'], rows['pruned']['removed']), ('removed', None))
        self.assertIn('unknown date', rows['pruned']['note'])
        self.assertFalse(rows['old-task']['derived'])
        self.assertEqual(found['dataset'], {'enabled': True, 'since': 100.0})
        # removed rows come after every worktree on disk
        statuses = [r['status'] for r in found['rows']]
        self.assertEqual(statuses, sorted(statuses, key=lambda s: s == 'removed'))

    def test_with_the_dataset_off_the_screen_says_so_and_still_lists_what_git_knows(self):
        rows, found = self.rows(dict(enabled=False, since=None, events=[], attempts=[], integrated=[], repos=[]))
        self.assertEqual(rows['bad']['status'], 'failed')
        self.assertTrue(any('FORGE_DATASET=off' in n for n in found['notes']))


class FoundOnDiskTests(Fixture):
    """A worktree on disk is listed with its real state however forge-ui reaches its repository, and is never
    called removed while its folder is there."""

    def history_only(self, repo_path, extra_repos=()):
        """No run dir any more: what the dataset says, with every path through `repo_path`."""
        shutil.rmtree(self.plan)
        return dict(enabled=True, since=1.0, integrated=[], attempts=[],
                    repos=[dict(repo='k', path=str(p)) for p in (repo_path, *extra_repos)],
                    events=[dict(name='worktree_created', ts=10.0, run='u', run_id='r1', task=t,
                                 path=str(self.root / t), branch='forge/r1/' + t, kind='task', repo_path=str(repo_path), by=None)
                            for t in ('done', 'bad')])

    def test_a_repository_reached_through_a_symlink_is_listed(self):
        link = self.tmp / 'link-to-app'
        link.symlink_to(self.repo)
        rows, found = self.rows(self.history_only(link))
        self.assertEqual((rows['done']['status'], rows['bad']['status']), ('finished', 'failed'))
        self.assertIn(str(self.repo), found['repos'])

    def test_worktrees_whose_recorded_checkout_was_deleted_are_found_through_their_own_git(self):
        gone = self.base / 'old-checkout'          # the checkout the run started in, deleted by hand since
        rows, _ = self.rows(self.history_only(gone))
        self.assertEqual((rows['done']['status'], rows['bad']['status']), ('finished', 'failed'))
        self.assertEqual(rows['dirty']['status'], 'dirty')

    def test_a_repository_git_cannot_list_is_named_and_its_worktrees_are_not_called_removed(self):
        real_git = worktrees._git

        def refusing(cwd, *args, **kwargs):
            if args[:2] == ('worktree', 'list'):
                return 128, ''
            return real_git(cwd, *args, **kwargs)
        with patch.object(worktrees, '_git', refusing):
            rows, found = self.rows(self.history_only(self.repo))
        self.assertNotEqual(rows['done']['status'], 'removed')
        self.assertTrue(any('could not list' in n for n in found['notes']), found['notes'])

    def test_a_task_split_after_its_worktree_was_made_is_still_the_runs(self):
        self.add('split-me', 'forge/r1/split-me')       # not in tasks.tsv: replaced by its parts
        self.add('named-x', 'forge/r1/other-name')      # forge's branch shape, another folder: not shown as forge's
        rows, _ = self.rows()
        self.assertEqual((rows['split-me']['kind'], rows['split-me']['status']), ('task', 'finished'))
        self.assertEqual(rows['named-x']['status'], 'foreign')

    def test_a_leftover_folder_git_no_longer_knows_offers_its_removal(self):
        rows, _ = self.rows()
        [cmd] = rows['orphan']['commands']
        self.assertEqual(cmd['command'], 'rm -rf -- %s' % worktrees.quote(self.root / 'orphan'))
        self.assertIn('look inside', cmd['label'])


class LocationListTests(Fixture):
    """forge's location list (scripts/forge_locations.py) is an index of where to look: with no run dir, no dataset and
    the run's checkout deleted, the worktrees on disk are still found, and the ones it saw go keep their dates."""

    def record(self, *lines):
        path = self.tmp / 'config' / 'forge' / 'locations.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(line) + '\n' for line in lines))

    def test_the_list_alone_finds_a_repository_whose_checkout_was_deleted(self):
        shutil.rmtree(self.plan)
        gone_checkout = str(self.base / 'old-checkout')
        common = os.path.realpath(self.repo / '.git')
        self.record(*[dict(v=1, at=10.0, op='add', kind='worktree', path=os.path.realpath(self.root / t), checkout=gone_checkout,
                           git=common, root=str(self.repo), run_id='r1', task=t, branch='forge/r1/' + t, wt_kind='task')
                      for t in ('done', 'bad')])
        rows, _ = self.rows(None)
        self.assertEqual((rows['done']['status'], rows['bad']['status']), ('finished', 'failed'))
        self.assertEqual(rows['done']['created'], 10.0)

    def test_a_worktree_it_saw_go_is_removed_with_both_dates(self):
        path = os.path.realpath(self.root / 'old-task')
        self.record(dict(v=1, at=20.0, op='add', kind='worktree', path=path, checkout=str(self.repo), root=str(self.repo),
                         run_id='r1', task='old-task', branch='forge/r1/old-task', wt_kind='task'),
                    dict(v=1, at=30.0, op='gone', kind='worktree', path=path),
                    'not json at all')
        rows, _ = self.rows(None)
        row = rows['old-task']
        self.assertEqual((row['status'], row['created'], row['removed'], row['derived']), ('removed', 20.0, 30.0, False))
        self.assertEqual(row['task'], 'old-task')


class ScreenTests(Fixture):
    def test_the_answer_is_cached_until_it_is_old_or_a_refresh_is_asked(self):
        screen, calls = worktrees.Screen(), []

        def history():
            calls.append(1)
            return None
        with patch.object(worktrees, 'compute', wraps=worktrees.compute) as compute:
            first = screen.answer(self.catalog, history, now=1000.0)
            screen.answer(self.catalog, history, now=1000.0 + worktrees.CACHE_S - 1)
            self.assertEqual(compute.call_count, 1)
            screen.answer(self.catalog, history, refresh=True, now=1001.0)
            self.assertEqual(compute.call_count, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(first['totals']['by_status']['failed'], 1)
        self.assertTrue(any('could not be read' in n for n in first['notes']))

    def test_sizes_fill_in_from_the_background(self):
        screen = worktrees.Screen()
        screen.answer(self.catalog, lambda: None)
        screen.sizes.thread.join(30)
        answer = screen.answer(self.catalog, lambda: None)
        sized = {Path(r['path']).name: r['size'] for r in answer['rows']}
        self.assertGreater(sized['orphan'], 0)
        self.assertIsNone(sized.get('gone'))
        self.assertEqual(answer['sizes_pending'], 0)


if __name__ == '__main__':
    unittest.main()
