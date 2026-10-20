"""Where a repo's run dirs live, since forge keeps them outside the repo.

`forge dataset import --registry` reads the registry to find runs to import.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_registry  # noqa: E402


class RunRegistryTests(unittest.TestCase):
    """Where a repo's plan dirs live, since forge keeps them outside the repo.

    `forge dataset import --registry` reads it to find runs to import.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.home = Path(self.dir.name) / 'config'
        self.repo = Path(self.dir.name) / 'repo'
        self.repo.mkdir()
        self._old = os.environ.get('XDG_CONFIG_HOME')
        os.environ['XDG_CONFIG_HOME'] = str(self.home)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._old is None:
            os.environ.pop('XDG_CONFIG_HOME', None)
        else:
            os.environ['XDG_CONFIG_HOME'] = self._old

    def test_the_registry_is_never_written_inside_the_repo(self):
        # The first version wrote <repo>/.forge/jev-runs.txt, which left the user's tree
        # dirty and made `integrate` refuse: "working tree is dirty -- commit or stash
        # before integrating". Forge's contract is that it does not touch that tree.
        forge_registry.register_run_dir(self.repo, Path(self.dir.name) / 'plan-a')
        written = [p for p in self.repo.rglob('*') if p.is_file()]
        self.assertEqual(written, [], 'forge must not dirty the user working tree')
        self.assertTrue(forge_registry.runs_registry(self.repo).is_file())

    def test_a_run_dir_is_recorded_once(self):
        plan = Path(self.dir.name) / 'plan-a'
        for _ in range(3):
            forge_registry.register_run_dir(self.repo, plan)
        lines = [x for x in forge_registry.runs_registry(self.repo).read_text().splitlines()
                 if x and not x.startswith('#')]
        self.assertEqual(lines, [str(plan.resolve())])

    def test_two_repos_do_not_share_a_registry(self):
        other = Path(self.dir.name) / 'other'
        other.mkdir()
        self.assertNotEqual(forge_registry.runs_registry(self.repo), forge_registry.runs_registry(other))

    def test_registering_never_raises_or_blocks_whatever_the_paths_are(self):
        # Callers rely on it to note a run and move on: `forge ui open` calls it before any
        # of its own timeouts, so a hang here hangs the command.
        tmp = Path(self.dir.name)
        os.symlink(tmp / 'loop-b', tmp / 'loop-a')
        os.symlink(tmp / 'loop-a', tmp / 'loop-b')

        def registry_is_a_fifo():
            repo = tmp / 'fifo-repo'
            repo.mkdir()
            registry = forge_registry.runs_registry(repo)
            registry.parent.mkdir(parents=True, exist_ok=True)
            os.mkfifo(registry)  # no reader: opening it for append would block forever
            return repo, tmp / 'plan-a'

        for label, paths in (('registry file is a FIFO', registry_is_a_fifo),
                             ('repo path with a NUL byte', lambda: (Path('/tmp/a\x00b'), tmp / 'plan-a')),
                             ('run dir is a symlink loop', lambda: (self.repo, tmp / 'loop-a'))):
            with self.subTest(label):
                repo, run_dir = paths()
                failure = []

                def call():
                    try:
                        forge_registry.register_run_dir(repo, run_dir)
                    except BaseException as exc:  # any escape breaks "never raises"
                        failure.append(exc)

                worker = threading.Thread(target=call, daemon=True)
                worker.start()
                worker.join(5)
                self.assertFalse(worker.is_alive(), 'register_run_dir blocked')
                self.assertEqual(failure, [])

    def test_an_unwritable_registry_never_raises(self):
        # Recording where a run lives must not be able to fail the run.
        # Patch the module the function looks up: forge_jev.cli only re-exports the name.
        with patch('forge_registry.runs_registry', side_effect=OSError('nope')):
            forge_registry.register_run_dir(self.repo, Path('/tmp/plan'))


class RegisterCommandTests(unittest.TestCase):
    """`forge_registry.py register <run-dir>`: what the runners call on every run.

    It runs inside every plan, run and solo run, so whatever the run dir holds it must
    exit 0, print nothing and return at once.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name)
        self.config = self.tmp / 'config'
        self.repo = self.tmp / 'repo'
        self.repo.mkdir()
        self.run_dir = self.tmp / 'plan-a'
        self.run_dir.mkdir()
        (self.run_dir / 'repo').write_text(str(self.repo) + '\n')
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.config))
        self.env.pop('FORGE_RUN_REGISTRY', None)

    def register(self, run_dir, env=None):
        result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/forge_registry.py'), 'register',
                                 str(run_dir)], env=env or self.env, capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        return result

    def registered(self, repo=None):
        path = self.config / 'forge/runs' / (forge_registry.repo_key(repo or self.repo) + '.txt')
        if not path.is_file():
            return []
        return [x for x in path.read_text().splitlines() if x and not x.startswith('#')]

    def test_a_run_dir_is_registered_under_its_repo_once(self):
        self.register(self.run_dir)
        self.register(self.run_dir)
        self.assertEqual(self.registered(), [str(self.run_dir.resolve())])
        self.assertEqual([p for p in self.repo.rglob('*')], [], 'forge must not dirty the user working tree')

    def test_only_the_first_line_of_the_repo_file_is_read(self):
        (self.run_dir / 'repo').write_text(f'  {self.repo}  \nsomething else\n')
        self.register(self.run_dir)
        self.assertEqual(self.registered(), [str(self.run_dir.resolve())])

    def test_forge_run_registry_off_registers_nothing(self):
        for value in ('off', 'OFF', 'Off'):
            with self.subTest(value):
                self.register(self.run_dir, env=dict(self.env, FORGE_RUN_REGISTRY=value))
                self.assertEqual(self.registered(), [])
                self.assertFalse(self.config.exists())

    def test_a_config_home_that_is_not_absolute_is_never_written_below_the_current_directory(self):
        # A relative XDG_CONFIG_HOME resolves below the cwd, which for a runner is usually the
        # user's repo. An empty one counts as unset (XDG spec): the run lands in ~/.config.
        home = self.tmp / 'home'
        default = home / '.config/forge/runs' / (forge_registry.repo_key(self.repo) + '.txt')
        for value, expected in (('', [str(self.run_dir.resolve())]), ('relative/config', [])):
            with self.subTest(repr(value)):
                result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/forge_registry.py'), 'register',
                                         str(self.run_dir)], env=dict(self.env, XDG_CONFIG_HOME=value, HOME=str(home)),
                                        cwd=self.repo, capture_output=True, text=True, timeout=10,
                                        stdin=subprocess.DEVNULL)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
                self.assertEqual([p for p in self.repo.rglob('*')], [])
                lines = default.read_text().splitlines() if default.is_file() else []
                self.assertEqual([x for x in lines if x and not x.startswith('#')], expected)
                if default.exists():
                    default.unlink()

    def test_never_fails_blocks_or_prints_whatever_the_run_dir_holds(self):
        def missing_run_dir():
            return self.tmp / 'no-such-run'

        def missing_repo_file():
            (self.run_dir / 'repo').unlink()
            return self.run_dir

        def repo_file_is_a_fifo():
            (self.run_dir / 'repo').unlink()
            os.mkfifo(self.run_dir / 'repo')  # no writer: a blocking open would wait forever
            return self.run_dir

        def repo_file_is_a_dir():
            (self.run_dir / 'repo').unlink()
            (self.run_dir / 'repo').mkdir()
            return self.run_dir

        def repo_with_a_nul_byte():
            (self.run_dir / 'repo').write_bytes(str(self.repo).encode() + b'\x00x\n')
            return self.run_dir

        def relative_repo():
            (self.run_dir / 'repo').write_text('repo\n')
            return self.run_dir

        def empty_repo_file():
            (self.run_dir / 'repo').write_text('\n')
            return self.run_dir

        def undecodable_repo_file():
            (self.run_dir / 'repo').write_bytes(b'\xff\xfe/not/utf8\n')
            return self.run_dir

        def registry_is_unwritable():
            self.config.write_text('a file where the config dir should be')
            return self.run_dir

        for setup in (missing_run_dir, missing_repo_file, repo_file_is_a_fifo, repo_file_is_a_dir,
                      repo_with_a_nul_byte, relative_repo, empty_repo_file, undecodable_repo_file,
                      registry_is_unwritable):
            with self.subTest(setup.__name__):
                self.setUp()
                self.register(setup())  # a hang fails on the timeout, any output or exit code on the assert
                runs = self.config / 'forge/runs'
                self.assertEqual(sorted(runs.iterdir()) if runs.is_dir() else [], [], 'nothing is registered, under any repo')

    def test_arguments_it_does_not_understand_are_ignored_quietly(self):
        for args in ([], ['register'], ['register', 'a', 'b'], ['unregister', str(self.run_dir)]):
            with self.subTest(args):
                result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/forge_registry.py'), *args],
                                        env=self.env, capture_output=True, text=True, timeout=10)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        self.assertFalse(self.config.exists())


if __name__ == '__main__':
    unittest.main()
