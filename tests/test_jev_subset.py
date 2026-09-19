"""Early test feedback in a throwaway export.

The property that made this possible at all: it must never touch the task worktree.
Running tests there has no safe window -- artifacts before the diff capture reach QA as
if the dwarf wrote them, and artifacts after trip the fingerprint re-checks in do_task
and merge_task, which write INVALIDATED and block the merge.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

from forge_jev import subset  # noqa: E402


def _git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class RepoCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name) / 'repo'
        (self.repo / 'tests').mkdir(parents=True)
        (self.repo / 'src').mkdir()
        (self.repo / 'src' / 'cart.py').write_text('def total(i, d=0):\n    return max(0, sum(i) - d)\n')
        (self.repo / 'tests' / 'test_cart.py').write_text(
            'import unittest\n'
            'import sys; sys.path.insert(0, ".")\n'
            'from src.cart import total\n'
            'class T(unittest.TestCase):\n'
            '    def test_clamp(self):\n'
            '        self.assertGreaterEqual(total([5], 20), 0)\n')
        (self.repo / 'tests' / 'README.md').write_text('not a test\n')
        (self.repo / 'tests' / 'fixture.json').write_text('{}\n')
        _git(self.repo, 'init', '-q', '.')
        _git(self.repo, 'config', 'user.email', 't@t')
        _git(self.repo, 'config', 'user.name', 't')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-qm', 'base')


class CandidateTests(RepoCase):
    def test_only_test_code_is_a_candidate(self):
        # Matching "anything under tests/" pulled in README.md and JSON fixtures, each
        # costing a question and a slot under the cap for a file no runner executes.
        found = subset.candidate_tests(self.repo)
        self.assertEqual(found, ['tests/test_cart.py'])

    def test_a_non_repository_yields_nothing(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(subset.candidate_tests(plain), [])

    def test_candidates_are_capped_and_sorted(self):
        for i in range(subset.CANDIDATE_CAP + 10):
            (self.repo / 'tests' / f'test_{i:04d}.py').write_text('')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-qm', 'many')
        found = subset.candidate_tests(self.repo)
        self.assertEqual(len(found), subset.CANDIDATE_CAP)
        self.assertEqual(found, sorted(found))


class ExportTests(RepoCase):
    def test_export_never_touches_the_source_worktree(self):
        """The whole reason per-task subsetting was deferred, asserted directly."""
        before_tree = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD^{tree}'],
                                     capture_output=True, text=True).stdout
        before_files = sorted(p.name for p in self.repo.rglob('*') if '.git' not in p.parts)
        with tempfile.TemporaryDirectory() as destination:
            self.assertTrue(subset.export(self.repo, 'HEAD', destination))
            self.assertTrue((Path(destination) / 'tests' / 'test_cart.py').is_file())
            subprocess.run('python3 -m unittest tests/test_cart.py', shell=True,
                           cwd=destination, capture_output=True)
        after_tree = subprocess.run(['git', '-C', str(self.repo), 'rev-parse', 'HEAD^{tree}'],
                                    capture_output=True, text=True).stdout
        after_files = sorted(p.name for p in self.repo.rglob('*') if '.git' not in p.parts)
        self.assertEqual(before_tree, after_tree)
        self.assertEqual(before_files, after_files)
        self.assertEqual(list(self.repo.rglob('__pycache__')), [])

    def test_a_bad_commit_fails_without_raising(self):
        with tempfile.TemporaryDirectory() as destination:
            self.assertFalse(subset.export(self.repo, 'nonexistent', destination))

    def test_dependency_directories_are_linked_not_copied(self):
        (self.repo / 'node_modules').mkdir()
        (self.repo / 'node_modules' / 'marker').write_text('x')
        with tempfile.TemporaryDirectory() as destination:
            subset.export(self.repo, 'HEAD', destination)
            linked = Path(destination) / 'node_modules'
            self.assertTrue(linked.is_symlink())
            self.assertTrue((linked / 'marker').is_file())


class SelectTests(RepoCase):
    def test_selection_is_ordered_by_probability(self):
        candidates = ['tests/a.py', 'tests/b.py', 'tests/c.py']
        answers = {'t0': dict(type='noul', noul=0.2),
                   't1': dict(type='noul', noul=0.9),
                   't2': dict(type='noul', noul=0.5)}
        with patch('forge_jev.subset.ask', return_value=dict(answers=answers)):
            scored = subset.select(self.repo, task='t', changed=[], candidates=candidates)
        self.assertEqual([name for _p, name in scored],
                         ['tests/b.py', 'tests/c.py', 'tests/a.py'])

    def test_no_candidates_asks_nothing(self):
        with patch('forge_jev.subset.ask') as mocked:
            self.assertIsNone(subset.select(self.repo, task='t', changed=[], candidates=[]))
        mocked.assert_not_called()

    def test_a_failed_request_is_none(self):
        with patch('forge_jev.subset.ask', return_value=None):
            self.assertIsNone(subset.select(self.repo, task='t', changed=[],
                                            candidates=['tests/a.py']))


class CliContractTests(RepoCase):
    """Exit 0 means only one thing: the subset failed here AND passed on the base."""

    def _run(self, command, base=None, env=None):
        # A private config home: without it the exit code depended on whether the
        # developer's own ~/.config/forge/jev.json happened to be enabled.
        environment = dict(os.environ, XDG_CONFIG_HOME=str(Path(self.dir.name) / 'config'))
        environment.pop('TYPESAFE_API_KEY', None)
        environment.update(env or {})
        args = [sys.executable, str(ROOT / 'scripts' / 'forge-jev.py'), 'test-subset',
                '--repo', str(self.repo), '--commit', 'HEAD',
                '--task', str(self.repo / 'tests' / 'README.md'), '--command', command]
        if base:
            args += ['--base', base]
        return subprocess.run(args, capture_output=True, text=True, env=environment)

    def test_a_command_without_the_placeholder_is_a_usage_error(self):
        # Appending paths blindly is what produced a command that failed for reasons
        # unrelated to the diff; there is no general way to add a file list.
        result = self._run('python3 -m unittest discover -s tests')
        self.assertEqual(result.returncode, 2)

    def test_jev_disabled_does_nothing(self):
        result = self._run('python3 -m unittest {files}', env=dict(FORGE_JEV='off'))
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, '')


class BashWiringTests(unittest.TestCase):
    SOURCE = (ROOT / 'scripts' / 'forge-parallel.sh').read_text()

    def test_the_subset_uses_its_own_opt_in_template(self):
        # Never .forge/verify: that command has no placeholder and no obligation to
        # accept a file list.
        self.assertIn('.forge/verify-subset', self.SOURCE)

    def test_a_subset_result_reaches_the_qa_prompt(self):
        start = self.SOURCE.index('subset.jev.txt')
        self.assertIn('qa.input', self.SOURCE[start:start + 6000])

    def test_the_subset_never_writes_a_verification_status(self):
        start = self.SOURCE.index('test-subset --repo')
        block = self.SOURCE[start:start + 800]
        self.assertNotIn('verification.status', block)


if __name__ == '__main__':
    unittest.main()
