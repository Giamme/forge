import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forge_ui import repos


GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


def git(cwd, *args):
    done = subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV)
    return done.stdout.strip()


class ReposTest(unittest.TestCase):
    def setUp(self):
        # realpath: /tmp is a symlink on macOS and git reports physical paths.
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='forge-repos-')))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        repos._RESOLVED.clear()
        repos._FALLTHROUGH.clear()
        self.addCleanup(repos._RESOLVED.clear)
        self.addCleanup(repos._FALLTHROUGH.clear)
        self.n = 0

    def make_repo(self, name):
        path = self.tmp / name
        path.mkdir()
        git(path, 'init', '-q', '-b', 'main')
        (path / 'f').write_text(name)
        git(path, 'add', 'f')
        git(path, 'commit', '-q', '-m', 'init')
        return path

    def make_run(self, repo, kind='plan', base=None, identity=None):
        self.n += 1
        run = self.tmp / 'runs' / f'run{self.n}'
        run.mkdir(parents=True)
        (run / 'repo').write_text(f'{repo}\n')
        if base is not None:
            if kind == 'plan':
                (run / 'integration.base').write_text(base + '\n')
            else:
                (run / 'dwarf.head.before').write_text(base + '\n')
        if identity is not None:
            (run / 'repo.identity').write_text(identity if isinstance(identity, str) else json.dumps(identity))
        return str(run)

    def resolve(self, *runs):
        return repos.resolve([(path, repo, kind) for path, repo, kind in runs])

    def git_calls(self):
        calls = []
        real = repos._run_git

        def counting(cwd, *args):
            calls.append((cwd, args))
            return real(cwd, *args)
        return calls, patch.object(repos, '_run_git', counting)

    def test_main_and_worktrees_share_one_root(self):
        main = self.make_repo('proj')
        wt_a, wt_b = self.tmp / 'wt-a', self.tmp / 'wt-b'
        git(main, 'worktree', 'add', '-q', '-b', 'a', str(wt_a))
        git(main, 'worktree', 'add', '-q', '-b', 'b', str(wt_b))
        runs = [(self.make_run(p), str(p), 'plan') for p in (main, wt_a, wt_b)]
        out = repos.resolve(runs)
        self.assertEqual(out[runs[0][0]], dict(root=str(main), worktree=None, branch=None, source='live'))
        self.assertEqual(out[runs[1][0]], dict(root=str(main), worktree=str(wt_a), branch=None, source='live'))
        self.assertEqual(out[runs[2][0]], dict(root=str(main), worktree=str(wt_b), branch=None, source='live'))

    def test_subdirectory_of_a_checkout_names_its_checkout(self):
        main = self.make_repo('proj')
        wt = self.tmp / 'wt'
        git(main, 'worktree', 'add', '-q', '-b', 'a', str(wt))
        (main / 'sub').mkdir()
        (wt / 'sub').mkdir()
        a, b = self.make_run(main / 'sub'), self.make_run(wt / 'sub')
        out = repos.resolve([(a, str(main / 'sub'), 'plan'), (b, str(wt / 'sub'), 'plan')])
        self.assertEqual((out[a]['root'], out[a]['worktree']), (str(main), None))
        self.assertEqual((out[b]['root'], out[b]['worktree']), (str(main), str(wt)))

    def test_removed_worktree_resolves_by_base_commit_in_one_known_root(self):
        main = self.make_repo('proj')
        wt = self.tmp / 'wt'
        git(main, 'worktree', 'add', '-q', '-b', 'a', str(wt))
        (wt / 'g').write_text('g')
        git(wt, 'add', 'g')
        git(wt, 'commit', '-q', '-m', 'on wt')
        base = git(main, 'rev-parse', 'HEAD')
        live = self.make_run(main)
        for kind in ('plan', 'solo'):
            with self.subTest(kind=kind):
                run = self.make_run(wt, kind=kind, base=base)
                shutil.rmtree(wt, ignore_errors=True)
                git(main, 'worktree', 'prune')
                out = repos.resolve([(live, str(main), 'plan'), (run, str(wt), kind)])
                self.assertEqual(out[run], dict(root=str(main), worktree=str(wt), branch=None,
                                                source='base-commit'))
                git(main, 'worktree', 'add', '-q', '-B', 'a', str(wt))

    def test_plan_base_falls_back_to_the_first_task_base_ref(self):
        main = self.make_repo('proj')
        base = git(main, 'rev-parse', 'HEAD')
        gone = self.tmp / 'gone'
        run = self.make_run(gone)
        for name, text in (('t2', 'not a sha'), ('t1', base)):
            (Path(run) / 'tasks' / name).mkdir(parents=True)
            (Path(run) / 'tasks' / name / 'base_ref').write_text(text + '\n')
        live = self.make_run(main)
        out = repos.resolve([(live, str(main), 'plan'), (run, str(gone), 'plan')])
        self.assertEqual(out[run]['source'], 'base-commit')
        self.assertEqual(out[run]['root'], str(main))

    def test_commit_in_no_known_root_keeps_its_own_path(self):
        main = self.make_repo('proj')
        other = self.make_repo('other')
        base = git(other, 'rev-parse', 'HEAD')
        gone = self.tmp / 'gone'
        live, run = self.make_run(main), self.make_run(gone, base=base)
        out = repos.resolve([(live, str(main), 'plan'), (run, str(gone), 'plan')])
        self.assertEqual(out[run], dict(root=str(gone), worktree=None, branch=None, source='path'))

    def test_commit_in_two_known_roots_keeps_its_own_path(self):
        first = self.make_repo('proj')
        second = self.tmp / 'clone'
        git(self.tmp, 'clone', '-q', str(first), str(second))
        base = git(first, 'rev-parse', 'HEAD')
        gone = self.tmp / 'gone'
        runs = [(self.make_run(first), str(first), 'plan'), (self.make_run(second), str(second), 'plan'),
                (self.make_run(gone, base=base), str(gone), 'plan')]
        out = repos.resolve(runs)
        self.assertEqual(out[runs[0][0]]['root'], str(first))
        self.assertEqual(out[runs[1][0]]['root'], str(second))
        self.assertEqual(out[runs[2][0]]['source'], 'path')
        self.assertEqual(out[runs[2][0]]['root'], str(gone))

    def test_same_folder_name_stays_two_roots(self):
        (self.tmp / 'x').mkdir()
        (self.tmp / 'y').mkdir()
        one, two = self.make_repo('x/app'), self.make_repo('y/app')
        a, b = self.make_run(one), self.make_run(two)
        out = repos.resolve([(a, str(one), 'plan'), (b, str(two), 'plan')])
        self.assertEqual(sorted((out[a]['root'], out[b]['root'])), sorted((str(one), str(two))))

    def test_identity_wins_over_a_live_repo_and_needs_no_existing_root(self):
        main = self.make_repo('proj')
        gone = '/nonexistent/forge-repos/root'
        run = self.make_run(main, identity={'root': gone, 'worktree': '/w', 'branch': 'feat'})
        calls, patched = self.git_calls()
        with patched:
            out = repos.resolve([(run, str(main), 'plan')])
        self.assertEqual(out[run], dict(root=gone, worktree='/w', branch='feat', source='identity'))
        self.assertEqual(calls, [])

    def test_identity_takes_worktree_and_branch_only_when_strings(self):
        run = self.make_run('/gone', identity={'root': '/r', 'worktree': None, 'branch': 7})
        self.assertEqual(repos.resolve([(run, '/gone', 'plan')])[run],
                         dict(root='/r', worktree=None, branch=None, source='identity'))

    def test_identity_is_read_on_every_call(self):
        main = self.make_repo('proj')
        run = self.make_run(main)
        self.assertEqual(repos.resolve([(run, str(main), 'plan')])[run]['source'], 'live')
        (Path(run) / 'repo.identity').write_text(json.dumps({'root': '/r'}))
        self.assertEqual(repos.resolve([(run, str(main), 'plan')])[run]['source'], 'identity')

    def test_malformed_identity_falls_through(self):
        main = self.make_repo('proj')
        rows = {
            'not json': '{nope',
            'empty': '',
            'array': '[1]',
            'string': '"/abs"',
            'no root': '{"worktree": "/w"}',
            'root not a string': '{"root": 3}',
            'root relative': '{"root": "rel/path"}',
            'root empty': '{"root": ""}',
            'oversized': json.dumps({'root': '/r', 'pad': 'x' * 70000}),
        }
        for name, text in rows.items():
            with self.subTest(name):
                run = self.make_run(main, identity=text)
                self.assertEqual(repos.resolve([(run, str(main), 'plan')])[run],
                                 dict(root=str(main), worktree=None, branch=None, source='live'))

    def test_identity_that_is_not_a_regular_file_is_ignored(self):
        main = self.make_repo('proj')
        run = self.make_run(main)
        (Path(run) / 'repo.identity').mkdir()
        self.assertEqual(repos.resolve([(run, str(main), 'plan')])[run]['source'], 'live')

    def test_a_base_commit_that_is_not_hex_never_reaches_git(self):
        main = self.make_repo('proj')
        out_file = self.tmp / 'x'
        gone = self.tmp / 'gone'
        for kind in ('plan', 'solo'):
            for base in (f'--output={out_file}', 'HEAD', 'abc123', 'g' * 40, '0' * 41, '0' * 63):
                with self.subTest(kind=kind, base=base):
                    run = self.make_run(gone, kind=kind, base=base)
                    live = self.make_run(main)
                    calls, patched = self.git_calls()
                    with patched:
                        out = repos.resolve([(live, str(main), 'plan'), (run, str(gone), kind)])
                    self.assertFalse(out_file.exists())
                    self.assertEqual(out[run]['source'], 'path')
                    self.assertEqual(calls, [(str(main), ('worktree', 'list', '--porcelain', '-z'))])

    def test_a_missing_repo_text_resolves_to_no_root(self):
        run = self.make_run('/x')
        self.assertEqual(repos.resolve([(run, None, 'plan')])[run],
                         dict(root=None, worktree=None, branch=None, source='path'))

    def test_a_non_git_directory_keeps_its_path(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        run = self.make_run(plain)
        self.assertEqual(repos.resolve([(run, str(plain), 'plan')])[run],
                         dict(root=str(plain), worktree=None, branch=None, source='path'))

    def test_second_resolve_makes_no_git_call_and_one_batch_asks_each_question_once(self):
        main = self.make_repo('proj')
        wt = self.tmp / 'wt'
        git(main, 'worktree', 'add', '-q', '-b', 'a', str(wt))
        base = git(main, 'rev-parse', 'HEAD')
        gone = self.tmp / 'gone'
        runs = [(self.make_run(main), str(main), 'plan'), (self.make_run(main), str(main), 'solo'),
                (self.make_run(wt), str(wt), 'plan'),
                (self.make_run(gone, base=base), str(gone), 'plan'),
                (self.make_run(gone, base=base), str(gone), 'plan')]
        calls, patched = self.git_calls()
        with patched:
            first = repos.resolve(runs)
            self.assertEqual(len(calls), len(set(calls)))
            self.assertEqual(sorted(args[0] for _, args in calls), ['cat-file', 'worktree', 'worktree'])
            calls.clear()
            second = repos.resolve(runs)
        self.assertEqual(calls, [])
        self.assertEqual(first, second)
        self.assertEqual(first[runs[3][0]]['source'], 'base-commit')

    def test_second_resolve_of_a_non_git_directory_makes_no_git_call(self):
        plain = self.tmp / 'plain'
        plain.mkdir()
        runs = [(self.make_run(plain), str(plain), 'plan')]
        first = repos.resolve(runs)
        calls, patched = self.git_calls()
        with patched:
            second = repos.resolve(runs)
        self.assertEqual(calls, [])
        self.assertEqual(first, second)

    def test_a_path_with_a_newline_is_not_cut_at_the_newline(self):
        main = self.make_repo('proj\nsuffix')
        wt = self.tmp / 'wt'
        git(main, 'worktree', 'add', '-q', '-b', 'a', str(wt))
        run = self.make_run(wt)
        self.assertEqual(repos.resolve([(run, str(wt), 'plan')])[run],
                         dict(root=str(main), worktree=str(wt), branch=None, source='live'))

    def test_a_git_without_z_falls_back_to_lines_and_distrusts_a_split_path(self):
        main = self.make_repo('proj\nsuffix')
        wt = self.tmp / 'wt'
        git(main, 'worktree', 'add', '-q', '-b', 'a', str(wt))
        plain = self.make_repo('plain')
        real = repos._run_git

        def old_git(cwd, *args):
            if args[-1] == '-z':
                return subprocess.CompletedProcess(args, 129, b'', b'')
            return real(cwd, *args)
        with patch.object(repos, '_run_git', old_git):
            out = repos.resolve([(self.make_run(plain), str(plain), 'plan'),
                                 (self.make_run(wt), str(wt), 'plan')])
        self.assertEqual([i['root'] for i in out.values()], [str(plain), str(wt)])
        self.assertEqual([i['source'] for i in out.values()], ['live', 'path'])

    def test_cached_runs_survive_the_worktree_being_removed(self):
        main = self.make_repo('proj')
        wt = self.tmp / 'wt'
        git(main, 'worktree', 'add', '-q', '-b', 'a', str(wt))
        runs = [(self.make_run(wt), str(wt), 'plan')]
        before = repos.resolve(runs)
        shutil.rmtree(wt)
        self.assertEqual(repos.resolve(runs), before)

    def test_cache_returns_copies(self):
        main = self.make_repo('proj')
        runs = [(self.make_run(main), str(main), 'plan')]
        repos.resolve(runs)[runs[0][0]]['root'] = 'tampered'
        self.assertEqual(repos.resolve(runs)[runs[0][0]]['root'], str(main))

    def test_fall_through_run_is_resolved_again_when_a_new_root_appears(self):
        main = self.make_repo('proj')
        other = self.make_repo('other')
        base = git(other, 'rev-parse', 'HEAD')
        gone = self.tmp / 'gone'
        stray = (self.make_run(gone, base=base), str(gone), 'plan')
        first = repos.resolve([(self.make_run(main), str(main), 'plan'), stray])
        self.assertEqual(first[stray[0]]['source'], 'path')
        calls, patched = self.git_calls()
        with patched:
            again = repos.resolve([(self.make_run(main), str(main), 'plan'), stray])
            self.assertEqual(again[stray[0]]['source'], 'path')
            self.assertEqual([args[0] for _, args in calls if args[0] == 'cat-file'], [])
            calls.clear()
            third = repos.resolve([(self.make_run(main), str(main), 'plan'),
                                   (self.make_run(other), str(other), 'plan'), stray])
        self.assertEqual(third[stray[0]], dict(root=str(other), worktree=str(gone), branch=None,
                                               source='base-commit'))
        self.assertIn('cat-file', [args[0] for _, args in calls])

    def test_git_failure_or_timeout_never_raises(self):
        main = self.make_repo('proj')
        run = self.make_run(main)
        for failure in (subprocess.TimeoutExpired('git', 1), OSError('no git')):
            with self.subTest(failure=type(failure).__name__):
                repos._RESOLVED.clear()
                repos._FALLTHROUGH.clear()
                with patch.object(repos.subprocess, 'run', side_effect=failure):
                    out = repos.resolve([(run, str(main), 'plan')])
                self.assertEqual(out[run], dict(root=str(main), worktree=None, branch=None, source='path'))
                # A failed git call is not remembered: the next call finds the repository.
                self.assertEqual(repos.resolve([(run, str(main), 'plan')])[run]['source'], 'live')

    def test_git_is_called_without_a_shell_with_closed_stdin_and_a_timeout(self):
        main = self.make_repo('proj')
        run = self.make_run(main)
        real = subprocess.run
        seen = []

        def spy(args, **kwargs):
            seen.append((args, kwargs))
            return real(args, **kwargs)
        with patch.object(repos.subprocess, 'run', spy):
            repos.resolve([(run, str(main), 'plan')])
        (args, kwargs), = seen
        self.assertEqual(args, ['git', '-C', str(main), 'worktree', 'list', '--porcelain', '-z'])
        self.assertIs(kwargs['stdin'], subprocess.DEVNULL)
        self.assertFalse(kwargs.get('shell'))
        self.assertTrue(0 < kwargs['timeout'] <= 10)

    def test_resolving_writes_nothing(self):
        main = self.make_repo('proj')
        run = self.make_run(main)

        def tree(root):
            return sorted((str(p), p.stat().st_mtime_ns) for p in Path(root).rglob('*') if p.is_file())
        before = (tree(run), tree(main / '.git'))
        repos.resolve([(run, str(main), 'plan')])
        self.assertEqual((tree(run), tree(main / '.git')), before)

    def test_result_is_keyed_by_the_given_path_object(self):
        main = self.make_repo('proj')
        run = Path(self.make_run(main))
        self.assertEqual(repos.resolve([(run, str(main), 'plan')])[run]['root'], str(main))


if __name__ == '__main__':
    unittest.main()
