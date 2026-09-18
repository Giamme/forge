"""Offline tests for the git-history corpus extractor. No network, no API key, real temp git repos."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from forge_jev import corpus  # noqa: E402


def env():
    return dict(GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@test',
                GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@test')


class Repo:
    """A throwaway git repo built with real subprocess calls, branch-name agnostic."""

    def __init__(self, path: Path):
        self.path = path
        subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(path)],
                        check=True, env=env())

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self, subject: str, body: str | None = None, *, add: bool = True) -> str:
        if add:
            subprocess.run(['git', 'add', '-A'], cwd=self.path, check=True, env=env())
        args = ['git', 'commit', '-q', '-m', subject]
        if body:
            args += ['-m', body]
        subprocess.run(args, cwd=self.path, check=True, env=env())
        return self.rev('HEAD')

    def rev(self, ref: str) -> str:
        return subprocess.run(['git', 'rev-parse', ref], cwd=self.path, check=True, env=env(),
                              stdout=subprocess.PIPE).stdout.decode().strip()

    def branch(self, name: str, *, at: str = 'HEAD') -> None:
        subprocess.run(['git', 'branch', name, at], cwd=self.path, check=True, env=env())

    def checkout(self, ref: str) -> None:
        subprocess.run(['git', 'checkout', '-q', ref], cwd=self.path, check=True, env=env())

    def merge(self, ref: str, subject: str) -> str:
        subprocess.run(['git', 'merge', '--no-ff', '-q', '-m', subject, ref], cwd=self.path,
                        check=True, env=env())
        return self.rev('HEAD')


class ClassifyTests(unittest.TestCase):
    def test_is_test_python_and_typescript_conventions(self):
        for path in ['tests/test_a.py', 'test/a.py', 'spec/a.py', '__tests__/a.ts',
                     'e2e/flow.ts', 'a_test.go', 'src/a.test.ts', 'src/a.spec.ts',
                     'nested/dir/test_foo.py']:
            self.assertTrue(corpus.is_test(path), path)

    def test_is_test_false_for_plain_source(self):
        for path in ['src/a.py', 'src/testify.py', 'lib/latest.ts', 'src/contest.py']:
            self.assertFalse(corpus.is_test(path), path)

    def test_classify_splits_and_drops_vendor(self):
        paths = ['src/a.py', 'tests/test_a.py', 'node_modules/pkg/index.js',
                  'vendor/lib.go', 'dist/bundle.js', 'yarn.lock', 'package-lock.json',
                  'poetry.lock', '__pycache__/a.pyc', '.next/x.js', 'coverage/lcov.info',
                  'a.min.js', '__snapshots__/a.snap', 'src/b.test.ts']
        source, test = corpus.classify(paths)
        self.assertEqual(source, ['src/a.py'])
        self.assertEqual(test, ['tests/test_a.py', 'src/b.test.ts'])


class CommitsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repo(Path(self.tmp.name) / 'repo')

    def test_non_git_directory_yields_nothing(self):
        not_a_repo = Path(self.tmp.name) / 'plain'
        not_a_repo.mkdir()
        self.assertEqual(list(corpus.commits(not_a_repo)), [])

    def test_empty_repo_yields_nothing(self):
        self.assertEqual(list(corpus.commits(self.repo.path)), [])

    def test_root_commit_not_yielded(self):
        self.repo.write('src/a.py', 'a=1\n')
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.commit('root: add a and its test')
        self.assertEqual(list(corpus.commits(self.repo.path)), [])

    def test_source_only_commit_not_yielded(self):
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.commit('seed test')
        self.repo.write('src/a.py', 'a=1\n')
        self.repo.commit('source only change')
        self.assertEqual(list(corpus.commits(self.repo.path)), [])

    def test_test_only_commit_not_yielded(self):
        self.repo.write('src/a.py', 'a=1\n')
        self.repo.commit('seed source')
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.commit('test only change')
        self.assertEqual(list(corpus.commits(self.repo.path)), [])

    def test_over_max_files_not_yielded(self):
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.commit('seed')
        for i in range(5):
            self.repo.write(f'src/f{i}.py', f'x = {i}\n')
        self.repo.write('tests/test_a.py', 'def test_a(): assert True\n')
        self.repo.commit('big change')
        self.assertEqual(list(corpus.commits(self.repo.path, max_files=3)), [])
        self.assertEqual(len(list(corpus.commits(self.repo.path, max_files=60))), 1)

    def test_merge_commit_not_yielded(self):
        # Both pre-existing test files live in the seed commit so neither branch's edit counts
        # as "newly created" against its parent -- that's a separate rule, exercised elsewhere.
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.write('tests/test_c.py', 'def test_c(): pass\n')
        self.repo.commit('seed')
        base = self.repo.rev('HEAD')
        self.repo.branch('feature', at=base)
        self.repo.checkout('feature')
        self.repo.write('src/a.py', 'a=1\n')
        self.repo.write('tests/test_a.py', 'def test_a(): assert 1\n')
        self.repo.commit('feature change')
        self.repo.checkout('main')
        self.repo.write('src/b.py', 'b=1\n')
        self.repo.write('tests/test_c.py', 'def test_c(): assert 1\n')
        self.repo.commit('main change')
        self.repo.merge('feature', 'merge feature into main')
        records = list(corpus.commits(self.repo.path))
        subjects = [r['subject'] for r in records]
        self.assertNotIn('merge feature into main', subjects)
        # the two non-merge commits behind it should still be picked up
        self.assertIn('feature change', subjects)
        self.assertIn('main change', subjects)

    def test_parent_tree_rule_drops_newly_created_tests(self):
        # commit1: adds tests/test_a.py (plus a source seed so it's not a bare root-only test dir)
        self.repo.write('src/a.py', 'a=1\n')
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.commit('commit1: add a and test_a')
        # commit2: edits src/a.py, edits the pre-existing test_a.py, AND adds a brand-new
        # tests/test_b.py. test_b.py cannot be in the candidate universe (parent tree predates
        # it) and must be dropped; test_a.py survives so the commit still yields a record.
        self.repo.write('src/a.py', 'a=2\n')
        self.repo.write('tests/test_a.py', 'def test_a(): assert True\n')
        self.repo.write('tests/test_b.py', 'def test_b(): pass\n')
        self.repo.commit('commit2: edit a and test_a, add test_b')

        records = list(corpus.commits(self.repo.path))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['subject'], 'commit2: edit a and test_a, add test_b')
        self.assertIn('tests/test_a.py', record['candidates'])
        self.assertNotIn('tests/test_b.py', record['candidates'])
        self.assertIn('tests/test_a.py', record['test_files'])
        self.assertNotIn('tests/test_b.py', record['test_files'])
        self.assertEqual(record['stats']['n_test_created'], 1)

    def test_commit_with_only_newly_created_tests_is_skipped(self):
        self.repo.write('src/a.py', 'a=1\n')
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.commit('commit1: seed')
        # commit2 touches source but its only test file is brand new -> test_files empties -> skip
        self.repo.write('src/b.py', 'b=1\n')
        self.repo.write('tests/test_b.py', 'def test_b(): pass\n')
        self.repo.commit('commit2: only new test')
        self.assertEqual(list(corpus.commits(self.repo.path)), [])

    def test_limit_caps_and_newest_first(self):
        self.repo.write('src/a.py', 'a=1\n')
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.commit('seed')
        subjects = []
        for i in range(4):
            self.repo.write('src/a.py', f'a={i}\n')
            self.repo.write('tests/test_a.py', f'def test_a(): assert {i}\n')
            subject = f'change {i}'
            self.repo.commit(subject)
            subjects.append(subject)

        all_records = list(corpus.commits(self.repo.path))
        self.assertEqual(len(all_records), 4)
        self.assertEqual([r['subject'] for r in all_records], list(reversed(subjects)))

        limited = list(corpus.commits(self.repo.path, limit=2))
        self.assertEqual(len(limited), 2)
        self.assertEqual([r['subject'] for r in limited], list(reversed(subjects))[:2])

    def test_record_schema_and_stats_agreement(self):
        self.repo.write('src/a.py', 'a=1\n')
        self.repo.write('tests/test_a.py', 'def test_a(): pass\n')
        self.repo.commit('seed')
        self.repo.write('src/a.py', 'a=2\n')
        self.repo.write('src/b.py', 'b=1\n')
        self.repo.write('tests/test_a.py', 'def test_a(): assert True\n')
        sha = self.repo.commit('edit a, add b, edit test_a')
        parent = self.repo.rev('HEAD~1')

        records = list(corpus.commits(self.repo.path))
        self.assertEqual(len(records), 1)
        record = records[0]

        for key in ('repo', 'sha', 'parent', 'subject', 'body', 'source_files', 'test_files',
                    'all_changed', 'candidates', 'stats'):
            self.assertIn(key, record)
        self.assertEqual(record['sha'], sha)
        self.assertEqual(record['parent'], parent)
        self.assertEqual(sorted(record['source_files']), ['src/a.py', 'src/b.py'])
        self.assertEqual(record['test_files'], ['tests/test_a.py'])
        self.assertEqual(sorted(record['all_changed']),
                         sorted(['src/a.py', 'src/b.py', 'tests/test_a.py']))
        self.assertEqual(record['candidates'], sorted(record['candidates']))
        stats = record['stats']
        self.assertEqual(stats['n_source'], len(record['source_files']))
        self.assertEqual(stats['n_test'], len(record['test_files']))
        self.assertEqual(stats['n_candidates'], len(record['candidates']))
        self.assertIn('n_test_created', stats)


class StatsTests(unittest.TestCase):
    def test_stats_aggregates(self):
        records = [
            dict(stats=dict(n_source=1, n_test=1, n_candidates=10, n_test_created=0)),
            dict(stats=dict(n_source=2, n_test=1, n_candidates=20, n_test_created=1)),
        ]
        result = corpus.stats(records)
        self.assertEqual(result['n_commits'], 2)
        self.assertEqual(result['total_source_files'], 3)
        self.assertEqual(result['total_test_files'], 2)
        self.assertEqual(result['total_candidates'], 30)
        self.assertEqual(result['total_test_created'], 1)
        self.assertAlmostEqual(result['avg_candidates'], 15.0)
        self.assertAlmostEqual(result['avg_source_per_commit'], 1.5)
        self.assertAlmostEqual(result['avg_test_per_commit'], 1.0)

    def test_stats_empty(self):
        result = corpus.stats([])
        self.assertEqual(result['n_commits'], 0)
        self.assertEqual(result['avg_candidates'], 0.0)


if __name__ == '__main__':
    unittest.main()
