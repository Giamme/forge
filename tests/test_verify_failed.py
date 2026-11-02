"""forge-verify-failed.py (#112): the test files a failed verification names, and the verify-subset
command that reruns only those. The end-to-end reruns are in test_retry.py."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-verify-failed.py'
TRACKED = ('tests/test_alpha.py', 'ui/tests/test_beta.py', 'pkg/sub/test_gamma.py', 'ui/tests/js/panel.test.js',
           'tests/helper.py')


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        for name in TRACKED:
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / name).write_text('#\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)

    def command(self, log, subset='run {files}'):
        (Path(self.tmp.name) / 'log').write_text(log)
        (Path(self.tmp.name) / 'subset').write_text(subset)
        return subprocess.run([sys.executable, str(SCRIPT), 'command', '--checkout', str(self.repo),
                               '--log', str(Path(self.tmp.name) / 'log'),
                               '--subset', str(Path(self.tmp.name) / 'subset')],
                              capture_output=True, text=True, timeout=60)

    def test_each_runner_names_its_failing_files(self):
        cases = [
            ('unittest FAIL', 'FAIL: test_x (test_alpha.Case.test_x)\n', 'run tests/test_alpha.py'),
            ('unittest ERROR in setUpClass', 'ERROR: setUpClass (test_beta.Case)\n', 'run ui/tests/test_beta.py'),
            ('a dotted module', 'FAIL: test_x (pkg.sub.test_gamma.Case.test_x)\n', 'run pkg/sub/test_gamma.py'),
            ('an import failure', 'ERROR: test_alpha (unittest.loader._FailedTest.test_alpha)\n', 'run tests/test_alpha.py'),
            ('pytest', 'FAILED tests/test_alpha.py::test_x - assert 1 == 2\n', 'run tests/test_alpha.py'),
            ('node spec (node 24)', '✖ failing tests:\n\ntest at ui/tests/js/panel.test.js:1:63\n✖ adds (0.7ms)\n',
             'run ui/tests/js/panel.test.js'),
            ('node TAP, an absolute location', "not ok 1 - adds\n  ---\n  location: '{repo}/ui/tests/js/panel.test.js:1:63'\n",
             'run ui/tests/js/panel.test.js'),
            ('two failures in one file, then another file',
             'FAIL: a (test_alpha.C.a)\nFAIL: b (test_alpha.C.b)\nFAIL: c (test_beta.C.c)\n',
             'run tests/test_alpha.py ui/tests/test_beta.py'),
        ]
        for name, log, expected in cases:
            with self.subTest(name):
                r = self.command(log.replace('{repo}', str(self.repo.resolve())))
                self.assertEqual((r.returncode, r.stdout.strip()), (0, expected))

    def test_nothing_to_rerun_alone_exits_1(self):
        cases = [
            ('no failure line', 'Ran 3 tests\nOK\n', 'run {files}'),
            ('a file git does not track', 'FAILED tests/test_missing.py::test_x\n', 'run {files}'),
            ('a subset command without a placeholder', 'FAIL: test_x (test_alpha.Case.test_x)\n', 'run all'),
        ]
        for name, log, subset in cases:
            with self.subTest(name):
                r = self.command(log, subset)
                self.assertEqual((r.returncode, r.stdout), (1, ''))

    def test_changed_is_filled_too_and_paths_are_quoted(self):
        (self.repo / 'tests' / 'test_a$b.py').write_text('#\n')
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        r = self.command('FAILED tests/test_a$b.py::t\n', 'related {changed}')
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "related 'tests/test_a$b.py'"))


if __name__ == '__main__':
    unittest.main()
