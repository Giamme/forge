"""Where a repo's run dirs live, since forge keeps them outside the repo.

`forge dataset import --registry` reads the registry to find runs to import.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
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


class UnregisterTests(unittest.TestCase):
    """`forge runs rm` takes a run dir back out of the registry, without costing any other entry."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.tmp = Path(self.dir.name).resolve()
        self.config = self.tmp / 'config'
        patcher = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runs = self.config / 'forge' / 'runs'
        self.repos = []
        for name in ('one', 'two'):
            repo = self.tmp / name
            repo.mkdir()
            self.repos.append(repo)

    def entries(self, repo):
        path = forge_registry.runs_registry(repo)
        return [x for x in path.read_text().splitlines() if x and not x.startswith('#')] if path.is_file() else []

    def test_a_run_dir_is_removed_from_every_registry_file_that_lists_it_and_nothing_else_changes(self):
        shared, only_one, other = (self.tmp / n for n in ('shared', 'only-one', 'other'))
        forge_registry.register_run_dir(self.repos[0], shared)
        forge_registry.register_run_dir(self.repos[0], only_one)
        forge_registry.register_run_dir(self.repos[1], shared)
        forge_registry.register_run_dir(self.repos[1], other)
        untouched = forge_registry.runs_registry(self.repos[1])
        mine = forge_registry.runs_registry(self.repos[0])
        # lines a human or an older forge may have left: padding, a comment, a path that is not UTF-8
        extra = b'\n# a note\n  ' + str(shared).encode() + b'  \n' + b'/odd/\xff\xfe/dir\n' + str(only_one).encode()
        mine.write_bytes(mine.read_bytes() + extra)   # no final newline
        os.chmod(mine, 0o640)
        untouched_before = (untouched.read_bytes(), untouched.stat().st_ino)
        changed = forge_registry.unregister_run_dir(only_one)
        self.assertEqual(changed, [mine])
        self.assertEqual(mine.read_bytes().count(str(only_one).encode()), 0)
        self.assertEqual((untouched.read_bytes(), untouched.stat().st_ino), untouched_before, 'a file that does not list it is left alone')
        self.assertEqual(stat_mode(mine), 0o640)
        changed = forge_registry.unregister_run_dir(shared)
        self.assertEqual(sorted(changed), sorted([mine, untouched]))
        self.assertEqual(self.entries(self.repos[1]), [str(other)])
        text = mine.read_bytes()
        self.assertIn(b'# forge run dirs for ' + str(self.repos[0]).encode(), text)
        self.assertIn(b'# a note\n', text)
        self.assertIn(b'/odd/\xff\xfe/dir\n', text, 'the bytes of other lines are kept as they were')
        self.assertNotIn(str(shared).encode(), text)

    def test_a_missing_run_dir_and_a_missing_registry_are_nothing_to_do(self):
        self.assertEqual(forge_registry.unregister_run_dir(self.tmp / 'gone'), [])
        self.assertFalse(self.config.exists(), 'unregistering creates nothing')
        forge_registry.register_run_dir(self.repos[0], self.tmp / 'a')
        self.assertEqual(forge_registry.unregister_run_dir(self.tmp / 'gone'), [])
        self.assertEqual(self.entries(self.repos[0]), [str(self.tmp / 'a')])

    def test_a_path_is_matched_as_written_and_as_resolved(self):
        real = self.tmp / 'real'
        real.mkdir()
        link = self.tmp / 'via-link'
        os.symlink(real, link)
        forge_registry.register_run_dir(self.repos[0], link)   # recorded resolved
        self.assertEqual(self.entries(self.repos[0]), [str(real)])
        self.assertEqual(len(forge_registry.unregister_run_dir(link)), 1)
        self.assertEqual(self.entries(self.repos[0]), [])

    def test_the_lock_is_a_sibling_file_that_is_never_replaced(self):
        # Locking the registry file itself would lock nothing once unregister renames a new one
        # over it; the lock lives in a file of its own.
        forge_registry.register_run_dir(self.repos[0], self.tmp / 'a')
        forge_registry.register_run_dir(self.repos[0], self.tmp / 'b')
        lock, registry = self.runs / '.lock', forge_registry.runs_registry(self.repos[0])
        before = (lock.stat().st_ino, registry.stat().st_ino)
        forge_registry.unregister_run_dir(self.tmp / 'a')
        self.assertEqual(lock.stat().st_ino, before[0])
        self.assertNotEqual(registry.stat().st_ino, before[1], 'the registry file itself was replaced atomically')
        self.assertEqual([p.name for p in self.runs.iterdir() if p.name.endswith('.tmp')], [], 'no temporary file is left')
        self.assertNotIn('.lock', [p.name for p in self.runs.glob('*.txt')])

    def hold(self, seconds=None):
        """Hold the registry lock from this process; for `seconds`, or until the returned callable is called."""
        self.runs.mkdir(parents=True, exist_ok=True)
        handle = open(self.runs / '.lock', 'a')
        fcntl.flock(handle, fcntl.LOCK_EX)
        release = lambda: (fcntl.flock(handle, fcntl.LOCK_UN), handle.close())  # noqa: E731
        if seconds is None:
            return release
        threading.Timer(seconds, release).start()
        return release

    def test_register_gives_up_waiting_after_half_a_second_and_still_records_the_run(self):
        release = self.hold()
        started = time.monotonic()
        forge_registry.register_run_dir(self.repos[0], self.tmp / 'a')
        waited = time.monotonic() - started
        release()
        self.assertTrue(0.4 <= waited < 3, waited)
        self.assertEqual(self.entries(self.repos[0]), [str(self.tmp / 'a')], 'a duplicate is acceptable, a lost run is not')

    def test_register_waits_out_a_lock_that_is_held_briefly(self):
        self.hold(0.15)
        started = time.monotonic()
        forge_registry.register_run_dir(self.repos[0], self.tmp / 'a')
        self.assertGreaterEqual(time.monotonic() - started, 0.1)
        self.assertEqual(self.entries(self.repos[0]), [str(self.tmp / 'a')])

    def test_register_never_blocks_or_raises_when_the_lock_file_is_unusable(self):
        self.runs.mkdir(parents=True)
        os.mkfifo(self.runs / '.lock')   # opening it for reading with no writer would block
        failure = []

        def call():
            try:
                forge_registry.register_run_dir(self.repos[0], self.tmp / 'a')
            except BaseException as exc:  # noqa: BLE001
                failure.append(exc)

        worker = threading.Thread(target=call, daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), 'register_run_dir blocked')
        self.assertEqual(failure, [])
        self.assertEqual(self.entries(self.repos[0]), [str(self.tmp / 'a')])

    def test_unregister_reports_a_lock_that_stays_busy_and_changes_nothing(self):
        forge_registry.register_run_dir(self.repos[0], self.tmp / 'a')
        registry = forge_registry.runs_registry(self.repos[0])
        before = registry.read_bytes()
        release = self.hold()
        self.addCleanup(release)
        with patch.object(forge_registry, 'UNREGISTER_LOCK_WAIT_S', 0.2):
            with self.assertRaises(BlockingIOError):
                forge_registry.unregister_run_dir(self.tmp / 'a')
        self.assertEqual(registry.read_bytes(), before)

    def test_unregister_reports_a_registry_it_cannot_rewrite(self):
        forge_registry.register_run_dir(self.repos[0], self.tmp / 'a')
        if os.geteuid() == 0:
            self.skipTest('permissions do not stop root')
        os.chmod(self.runs, 0o500)
        self.addCleanup(os.chmod, self.runs, 0o700)
        with self.assertRaises(OSError):
            forge_registry.unregister_run_dir(self.tmp / 'a')
        self.assertEqual(self.entries(self.repos[0]), [str(self.tmp / 'a')])

    def test_a_registry_file_that_is_not_a_regular_file_is_skipped_without_blocking(self):
        forge_registry.register_run_dir(self.repos[0], self.tmp / 'a')
        os.mkfifo(self.runs / 'fifo.txt')
        os.symlink(forge_registry.runs_registry(self.repos[0]), self.runs / 'link.txt')
        result = []
        worker = threading.Thread(target=lambda: result.append(forge_registry.unregister_run_dir(self.tmp / 'a')), daemon=True)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive(), 'unregister_run_dir blocked')
        self.assertEqual(result, [[forge_registry.runs_registry(self.repos[0])]])

    def test_concurrent_register_and_unregister_in_separate_processes_lose_no_other_entries(self):
        worker = (
            'import sys\n'
            'from pathlib import Path\n'
            'sys.path.insert(0, %r)\n'
            'import forge_registry\n'
            'mode, repo, start, count, prefix = sys.argv[1:6]\n'
            'for i in range(int(start), int(start) + int(count)):\n'
            '    if mode == "register":\n'
            '        forge_registry.register_run_dir(Path(repo), Path(prefix + str(i)))\n'
            '    else:\n'
            '        forge_registry.unregister_run_dir(prefix + str(i))\n'
        ) % str(ROOT / 'scripts')
        repo = self.repos[0]
        victims, keepers, registrars, each = 90, 30, 4, 60
        for i in range(victims):
            forge_registry.register_run_dir(repo, self.tmp / ('victim-%d' % i))
        for i in range(keepers):
            forge_registry.register_run_dir(repo, self.tmp / ('keeper-%d' % i))
        env = dict(os.environ, XDG_CONFIG_HOME=str(self.config))
        jobs = []
        for n in range(registrars):
            jobs.append(['register', str(repo), str(n * each), str(each), str(self.tmp / ('new-%d-' % n))])
        for half in range(3):
            jobs.append(['unregister', str(repo), str(half * victims // 3), str(victims // 3), str(self.tmp / 'victim-')])
        children = [subprocess.Popen([sys.executable, '-I', '-c', worker, *job], env=env, stderr=subprocess.PIPE) for job in jobs]
        for child in children:
            _, err = child.communicate(timeout=120)
            self.assertEqual(child.returncode, 0, err)
        found = self.entries(repo)
        expected = {str(self.tmp / ('keeper-%d' % i)) for i in range(keepers)}
        expected |= {str(self.tmp / ('new-%d-%d' % (n, i))) for n in range(registrars) for i in range(n * each, n * each + each)}
        self.assertEqual(sorted(expected - set(found)), [], 'entries were lost')
        self.assertEqual([x for x in found if '/victim-' in x], [], 'a victim survived')
        self.assertEqual(len(found), len(set(found)), 'no duplicates when every register got the lock')


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


if __name__ == '__main__':
    unittest.main()
