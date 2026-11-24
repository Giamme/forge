"""`forge handoff resume`: follow a live run, or restart it safely (#146).

Real git worktrees in temporary repos. forge-parallel.sh is replaced by a fake through
FORGE_HANDOFF_PARALLEL, so no retry or run really starts.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-handoff.py'
RUN = 'r1'
FULL_FLAGS = {
    'retry_failed': 2, 'infra_retries': 3, 'sweep': 'P2', 'sweep_max': 4, 'sweep_plausible': True,
    'sweep_ahead': True, 'max_parallel': 5, 'timeout': 900, 'verify': 'make check', 'fractal': False,
    'argv': [], 'at': 1.0}
FAKE = '''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
if [ "$1" = run ]; then echo "fake run output"; exit "${FAKE_RUN_RC:-0}"; fi
if [ "$1" = retry ] && [ "$3" = "${FAKE_RETRY_FAIL:-}" ]; then echo "retry refused" >&2; exit 2; fi
exit 0
'''
# Holds an exclusive flock on argv[1], says so, and keeps it until its stdin closes.
HOLD_LOCK = (
    'import sys\n'
    'sys.path.insert(0, %r)\n'
    'import forge_flock\n'
    'h = open(sys.argv[1], "a")\n'
    'forge_flock.flock(h, forge_flock.LOCK_EX | forge_flock.LOCK_NB)\n'
    'print("held", flush=True)\n'
    'sys.stdin.read()\n' % str(ROOT / 'scripts'))
# Prints free or busy: whether an exclusive flock on argv[1] can be taken now.
PROBE_LOCK = (
    'import sys\n'
    'sys.path.insert(0, %r)\n'
    'import forge_flock\n'
    'h = open(sys.argv[1], "a")\n'
    'try:\n'
    '    forge_flock.flock(h, forge_flock.LOCK_EX | forge_flock.LOCK_NB)\n'
    '    print("free")\n'
    'except OSError:\n'
    '    print("busy")\n' % str(ROOT / 'scripts'))
_MODULE = None


def module():
    global _MODULE
    if _MODULE is None:
        spec = importlib.util.spec_from_file_location('forge_handoff_resume_under_test', SCRIPT)
        _MODULE = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_MODULE)
    return _MODULE


def git(cwd, *args):
    subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                            GIT_COMMITTER_EMAIL='t@t'))


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'a.txt').write_text('one\n')
        (self.repo / 'b.txt').write_text('two\n')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'init')
        self.wt_root = self.root / 'wt'
        self.wt_root.mkdir()
        self.plan = self.root / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('a\t-\ta.txt\tmedium\t-\t-\tA\nb\t-\tb.txt\tmedium\t-\t-\tB\n')
        (self.plan / 'run_id').write_text(RUN + '\n')
        (self.plan / 'wt_root').write_text(str(self.wt_root) + '\n')
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        self.fake = self.root / 'fake-parallel.sh'
        self.fake.write_text(FAKE)
        self.fake_log = self.root / 'fake.log'
        self.env = dict(os.environ, FORGE_DATASET='off', FORGE_HANDOFF_PARALLEL=str(self.fake),
                        FAKE_LOG=str(self.fake_log), FORGE_HANDOFF_NO_START='1')

    # ---- helpers

    def worktree(self, task, branch=None, at=None):
        path = Path(at) if at else self.wt_root / task
        git(self.repo, 'worktree', 'add', '-q', '-b', branch or f'forge/{RUN}/{task}', str(path))
        return path

    def task_files(self, task, status='RUNNING', attempt=None, rows=()):
        tdir = self.plan / 'tasks' / task
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / 'status').write_text(status + '\n')
        if attempt is not None:
            (tdir / 'attempt').write_text(f'{attempt}\n')
        if rows:
            (tdir / 'attempts.tsv').write_text(''.join(f'1700000000\t{n}\tFAIL\t\t-\n' for n in rows))
        return tdir

    def dirty(self, wt):
        (wt / 'a.txt').write_text('changed\n')
        (wt / 'new.txt').write_text('brand new\n')

    def resume(self, *args, env=None):
        return subprocess.run([sys.executable, str(SCRIPT), 'resume', str(self.plan), *args], capture_output=True,
                              text=True, env=env or self.env, cwd=str(self.root), timeout=120)

    def calls(self):
        return self.fake_log.read_text().splitlines() if self.fake_log.exists() else []

    def status(self, task):
        return (self.plan / 'tasks' / task / 'status').read_text().strip()

    def live(self, pid=None):
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': pid or os.getpid(), 'started': time.time()}))

    # ---- tests

    def test_live_scheduler_changes_nothing_and_starts_nothing(self):
        wt = self.worktree('a')
        self.dirty(wt)
        tdir = self.task_files('a', attempt=1)
        self.live()
        before = {p: p.read_bytes() for p in tdir.iterdir()}
        env = dict(self.env)
        env.pop('FORGE_HANDOFF_NO_START')
        result = self.resume('--harness', 'codex', '--json', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual((data['live'], data['recovered'], data['prepared'], data['started'], data['command']),
                         (True, [], [], None, []))
        self.assertEqual({p: p.read_bytes() for p in tdir.iterdir()}, before)
        self.assertEqual((wt / 'a.txt').read_text(), 'changed\n')
        self.assertTrue((wt / 'new.txt').exists())
        self.assertEqual(self.calls(), [])
        self.assertFalse(list(self.plan.glob('resume-*')))
        text = self.resume('--harness', 'codex').stdout
        self.assertIn('live', text)
        self.assertIn('forge control show', text)

    def test_stale_running_task_is_saved_reset_and_marked(self):
        wt = self.worktree('a')
        self.dirty(wt)
        (wt / 'b.txt').write_text('staged\n')
        git(wt, 'add', 'b.txt')   # a staged change too
        index_before = subprocess.run(['git', '-C', str(wt), 'status', '--porcelain'], capture_output=True, text=True).stdout
        self.task_files('a', attempt=2, rows=[1])
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': 2 ** 22 + 12345, 'started': 1}))
        result = self.resume('--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        [row] = data['recovered']
        self.assertEqual((row['task'], row['reset'], row['attempt_rewound']), ('a', True, True))
        diff = Path(row['diff'])
        self.assertTrue(diff.name.startswith('interrupted-') and diff.name.endswith('.diff'))
        self.assertEqual(diff.parent, self.plan / 'tasks' / 'a')
        text = diff.read_text()
        self.assertIn('+changed', text)
        self.assertIn('new.txt', text)
        self.assertIn('+brand new', text)
        self.assertIn('+staged', text)
        self.assertIn('M a.txt', index_before)
        self.assertEqual(subprocess.run(['git', '-C', str(wt), 'status', '--porcelain'], capture_output=True,
                                        text=True).stdout, '')
        self.assertFalse((wt / 'new.txt').exists())
        self.assertEqual(self.status('a'), 'INTERRUPTED')
        self.assertEqual((self.plan / 'tasks' / 'a' / 'attempt').read_text().strip(), '1')
        self.assertEqual([p['task'] for p in data['prepared']], ['a'])
        self.assertEqual(self.calls(), [f'retry {self.plan} a --no-dispatch'])

    def test_a_plan_lock_held_by_another_command_changes_nothing(self):
        # #157: a forge command that holds schedule.lock (a `run` starting its scheduler, a retry) is
        # working on the plan; resume must not reset its worktrees nor restart it.
        wt = self.worktree('a')
        self.dirty(wt)
        tdir = self.task_files('a', attempt=1)
        before = {p: p.read_bytes() for p in tdir.iterdir()}
        holder = subprocess.Popen([sys.executable, '-c', HOLD_LOCK, str(self.plan / 'schedule.lock')],
                                  stdout=subprocess.PIPE, stdin=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), 'held')
        (self.plan / 'schedule.holder').write_text(json.dumps({'pid': holder.pid, 'command': 'run', 'at': time.time()}))
        env = dict(self.env)
        env.pop('FORGE_HANDOFF_NO_START')
        result = self.resume('--json', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual((data['locked'], data['recovered'], data['prepared'], data['started']), (True, [], [], None))
        self.assertEqual({p: p.read_bytes() for p in tdir.iterdir()}, before)
        self.assertEqual((wt / 'a.txt').read_text(), 'changed\n')
        self.assertTrue((wt / 'new.txt').exists())
        self.assertEqual(self.calls(), [])
        text = self.resume().stdout
        self.assertIn(f'the plan is locked by forge-parallel.sh run (pid {holder.pid})', text)

    def test_the_lock_is_free_again_when_the_task_is_prepared(self):
        # `retry --no-dispatch` takes the plan lock itself: resume must have let it go by then.
        self.worktree('a')
        self.task_files('a', attempt=1)
        probe = self.root / 'probe-parallel.sh'
        probe.write_text(FAKE.replace('exit 0\n', 'python3 -c "$PROBE" "$2/schedule.lock" >> "$FAKE_LOG"\nexit 0\n'))
        probe.chmod(0o755)
        env = dict(self.env, FORGE_HANDOFF_PARALLEL=str(probe), PROBE=PROBE_LOCK)
        data = json.loads(self.resume('--json', env=env).stdout)
        self.assertEqual([r['task'] for r in data['recovered']], ['a'])
        self.assertEqual(self.calls(), [f'retry {self.plan} a --no-dispatch', 'free'])

    def test_clean_worktree_writes_an_empty_diff(self):
        self.worktree('a')
        self.task_files('a', attempt=1)
        data = json.loads(self.resume('--json').stdout)
        self.assertEqual(Path(data['recovered'][0]['diff']).read_bytes(), b'')

    def test_attempt_is_kept_when_attempts_tsv_has_its_row(self):
        self.worktree('a')
        self.task_files('a', attempt=2, rows=[1, 2])
        data = json.loads(self.resume('--json').stdout)
        self.assertFalse(data['recovered'][0]['attempt_rewound'])
        self.assertEqual((self.plan / 'tasks' / 'a' / 'attempt').read_text().strip(), '2')

    def test_attempt_is_rewound_with_no_attempts_file(self):
        self.worktree('a')
        self.task_files('a', attempt=1)
        data = json.loads(self.resume('--json').stdout)
        self.assertTrue(data['recovered'][0]['attempt_rewound'])
        self.assertEqual((self.plan / 'tasks' / 'a' / 'attempt').read_text().strip(), '0')

    def test_an_attempt_that_committed_keeps_its_number(self):
        # Stopped during its review: the retry re-runs only the review, which spends no attempt.
        wt = self.worktree('a')
        (wt / 'a.txt').write_text('done\n')
        git(wt, 'commit', '-qam', 'forge(a) attempt 1: A')
        tdir = self.task_files('a', attempt=1)
        (tdir / 'reviewed.commit').write_text(subprocess.run(['git', '-C', str(wt), 'rev-parse', 'HEAD'],
                                                             capture_output=True, text=True).stdout)
        data = json.loads(self.resume('--json').stdout)
        self.assertFalse(data['recovered'][0]['attempt_rewound'])
        self.assertEqual((tdir / 'attempt').read_text().strip(), '1')
        self.assertEqual((wt / 'a.txt').read_text(), 'done\n')       # the committed work stays

    def test_an_earlier_attempts_commit_does_not_stop_the_rewind(self):
        wt = self.worktree('a')
        git(wt, 'commit', '-q', '--allow-empty', '-m', 'forge(a) attempt 1: A')
        tdir = self.task_files('a', attempt=2, rows=[1])
        (tdir / 'reviewed.commit').write_text(subprocess.run(['git', '-C', str(wt), 'rev-parse', 'HEAD'],
                                                             capture_output=True, text=True).stdout)
        data = json.loads(self.resume('--json').stdout)
        self.assertTrue(data['recovered'][0]['attempt_rewound'])
        self.assertEqual((tdir / 'attempt').read_text().strip(), '1')

    def assert_untouched(self, wt, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['recovered'], [])
        self.assertIn('skipped', result.stderr)
        self.assertEqual((wt / 'a.txt').read_text(), 'changed\n')
        self.assertEqual((wt / 'new.txt').read_text(), 'brand new\n')
        self.assertEqual(self.status('a'), 'RUNNING')
        self.assertEqual((self.plan / 'tasks' / 'a' / 'attempt').read_text().strip(), '1')
        self.assertFalse(list((self.plan / 'tasks' / 'a').glob('interrupted-*')))

    def test_guard_skips_a_path_outside_wt_root(self):
        wt = self.worktree('a', at=self.root / 'elsewhere')
        self.dirty(wt)
        self.task_files('a', attempt=1)
        (self.wt_root / 'a').symlink_to(wt)       # <wt_root>/a leads outside: the same as a symlinked path
        self.assert_untouched(wt, self.resume('--json'))

    def test_guard_skips_a_task_dir_that_is_a_plain_directory_in_another_repo(self):
        other = self.root / 'other'
        other.mkdir()
        git(other, 'init', '-q', '-b', 'main')
        (other / 'a.txt').write_text('x\n')
        git(other, 'add', '.')
        git(other, 'commit', '-q', '-m', 'i')
        git(other, 'checkout', '-q', '-b', f'forge/{RUN}/a')
        (other / 'a.txt').write_text('changed\n')
        (other / 'new.txt').write_text('brand new\n')
        (self.wt_root / 'a').symlink_to(other)
        self.task_files('a', attempt=1)
        self.assert_untouched(other, self.resume('--json'))

    def test_guard_skips_a_symlinked_wt_root_entry_inside_wt_root(self):
        real = self.worktree('a', at=self.wt_root / 'a-real')
        self.dirty(real)
        (self.wt_root / 'a').symlink_to(real)
        self.task_files('a', attempt=1)
        self.assert_untouched(real, self.resume('--json'))

    def test_guard_skips_the_wrong_branch(self):
        wt = self.worktree('a', branch='forge/other/a')
        self.dirty(wt)
        self.task_files('a', attempt=1)
        self.assert_untouched(wt, self.resume('--json'))

    def test_guard_skips_a_main_checkout(self):
        (self.plan / 'wt_root').write_text(str(self.root) + '\n')
        git(self.repo, 'checkout', '-q', '-b', f'forge/{RUN}/repo')
        self.dirty(self.repo)
        (self.plan / 'tasks.tsv').write_text('repo\t-\ta.txt\tmedium\t-\t-\tR\n')
        self.task_files('repo', attempt=1)
        result = self.resume('--json')
        self.assertEqual(json.loads(result.stdout)['recovered'], [])
        self.assertEqual((self.repo / 'a.txt').read_text(), 'changed\n')
        self.assertTrue((self.repo / 'new.txt').exists())

    def test_guard_skips_a_missing_worktree(self):
        self.task_files('a', attempt=1)
        result = self.resume('--json')
        self.assertEqual(json.loads(result.stdout)['recovered'], [])
        self.assertIn('skipped', result.stderr)
        self.assertEqual(self.status('a'), 'RUNNING')

    def test_guarded_task_does_not_stop_the_others(self):
        bad = self.worktree('a', branch='forge/other/a')
        self.dirty(bad)
        good = self.worktree('b')
        (good / 'b.txt').write_text('dirty\n')
        self.task_files('a', attempt=1)
        self.task_files('b', attempt=1)
        data = json.loads(self.resume('--json').stdout)
        self.assertEqual([r['task'] for r in data['recovered']], ['b'])
        self.assertEqual((bad / 'a.txt').read_text(), 'changed\n')
        self.assertEqual((good / 'b.txt').read_text(), 'two\n')

    def recover(self, task='a'):
        m = module()
        warnings = []
        row = m.recover_task(m.control(), str(self.plan), str(self.wt_root), RUN, task, warnings.append)
        return m, row, warnings

    def test_diff_is_on_disk_when_the_reset_runs(self):
        wt = self.worktree('a')
        self.dirty(wt)
        self.task_files('a', attempt=1)
        m = module()
        real, seen = m.git, []

        def spy(path, *args, **kwargs):
            if args[0] in ('reset', 'clean'):
                seen.append((args[0], str(path), [p.read_text() for p in (self.plan / 'tasks' / 'a').glob('interrupted-*.diff')]))
            return real(path, *args, **kwargs)

        m.git = spy
        try:
            _, row, _ = self.recover()
        finally:
            m.git = real
        self.assertEqual([(kind, path) for kind, path, _ in seen], [('reset', str(wt)), ('clean', str(wt))])
        for _, _, diffs in seen:
            self.assertEqual(len(diffs), 1)
            self.assertIn('+brand new', diffs[0])
        self.assertTrue(row['reset'])

    def test_a_diff_that_cannot_be_saved_resets_nothing(self):
        wt = self.worktree('a')
        self.dirty(wt)
        self.task_files('a', attempt=1)
        m = module()
        real = m.save_diff

        def broken(*args):
            raise OSError('disk full')

        m.save_diff = broken
        try:
            _, row, warnings = self.recover()
        finally:
            m.save_diff = real
        self.assertIsNone(row)
        self.assertTrue(any('untouched' in w for w in warnings))
        self.assertEqual((wt / 'a.txt').read_text(), 'changed\n')
        self.assertTrue((wt / 'new.txt').exists())
        self.assertEqual(self.status('a'), 'RUNNING')

    def test_prepare_failure_is_reported_and_the_rest_goes_on(self):
        for task in ('a', 'b'):
            self.worktree(task)
            self.task_files(task, attempt=1)
        env = dict(self.env, FAKE_RETRY_FAIL='a')
        result = self.resume('--json', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual([(p['task'], p['ok']) for p in data['prepared']], [('a', False), ('b', True)])
        self.assertIn('could not be prepared', result.stderr)
        self.assertEqual(len(self.calls()), 2)

    def test_already_interrupted_task_is_prepared_too(self):
        self.task_files('a', status='INTERRUPTED', attempt=1)
        data = json.loads(self.resume('--json').stdout)
        self.assertEqual(data['recovered'], [])
        self.assertEqual([p['task'] for p in data['prepared']], ['a'])

    def test_command_from_a_full_flags_file(self):
        (self.plan / 'run.flags.json').write_text(json.dumps(FULL_FLAGS))
        data = json.loads(self.resume('--json').stdout)
        self.assertIsNone(data['started'])
        self.assertEqual(data['command'], [
            '/bin/bash', str(self.fake), 'run', str(self.plan), '--retry-failed', '2', '--infra-retries', '3',
            '--sweep', 'P2', '--sweep-max', '4', '--sweep-plausible', '--sweep-ahead', '--max-parallel', '5',
            '--timeout', '900', '--verify', 'make check', '--no-fractal'])
        self.assertEqual(self.calls(), [])     # NO_START: not even the run

    def test_run_command_builder(self):
        build = module().run_command
        base = ['/bin/bash', str(self.fake), 'run', 'P']
        os.environ['FORGE_HANDOFF_PARALLEL'] = str(self.fake)
        self.addCleanup(os.environ.pop, 'FORGE_HANDOFF_PARALLEL')
        self.assertEqual(build('P', {}), base)
        self.assertEqual(build('P', {'fractal': True, 'sweep': None, 'sweep_plausible': True}), base + ['--fractal'])
        self.assertEqual(build('P', {'sweep': 'P1'}), base + ['--sweep', 'P1'])
        self.assertEqual(build('P', {'sweep': 'rm -rf', 'max_parallel': 0, 'timeout': 'x'}), base)

    def test_no_flags_file_says_the_run_predates_019(self):
        self.worktree('a')
        self.task_files('a', attempt=1)
        result = self.resume()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('predates 0.19', result.stdout)
        self.assertIn(f'run {self.plan}', result.stdout)
        self.assertEqual(self.status('a'), 'INTERRUPTED')          # steps 1-5 still ran
        self.assertEqual(self.calls(), [f'retry {self.plan} a --no-dispatch'])
        env = dict(self.env)
        env.pop('FORGE_HANDOFF_NO_START')
        self.assertIsNone(json.loads(self.resume('--json', env=env).stdout)['started'])

    def test_refused_claim_exits_3_and_touches_no_worktree(self):
        wt = self.worktree('a')
        self.dirty(wt)
        self.task_files('a', attempt=1)
        now = time.time()
        (self.plan / 'handoff.driver.json').write_text(json.dumps(
            {'harness': 'claude', 'session': 's1', 'since': now - 60, 'last_seen': now - 30}))
        result = self.resume('--harness', 'codex')
        self.assertEqual(result.returncode, 3)
        self.assertIn('claude/s1 holds this run', result.stderr)
        self.assertEqual((wt / 'a.txt').read_text(), 'changed\n')
        self.assertEqual(self.status('a'), 'RUNNING')
        self.assertEqual(self.calls(), [])
        # --take-over claims, then goes on
        again = self.resume('--harness', 'codex', '--take-over')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(json.loads((self.plan / 'handoff.driver.json').read_text())['harness'], 'codex')
        self.assertEqual(self.status('a'), 'INTERRUPTED')

    def test_claim_is_recorded_with_harness_and_session(self):
        self.resume('--harness', 'codex', '--session', 'abc')
        driver = json.loads((self.plan / 'handoff.driver.json').read_text())
        self.assertEqual((driver['harness'], driver['session']), ('codex', 'abc'))

    def test_fractal_plan_points_at_fractal_resume(self):
        (self.plan / 'fractal-selection.json').write_text(json.dumps({'enabled': True}))
        wt = self.worktree('a')
        self.dirty(wt)
        self.task_files('a', attempt=1)
        result = self.resume()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f'forge fractal resume {RUN}', result.stdout)
        self.assertEqual(self.status('a'), 'RUNNING')
        self.assertEqual((wt / 'a.txt').read_text(), 'changed\n')
        self.assertEqual(self.calls(), [])

    def test_detached_run_writes_a_done_file_with_its_exit_code(self):
        (self.plan / 'run.flags.json').write_text(json.dumps(dict(FULL_FLAGS, sweep=None, sweep_max=None)))
        env = dict(self.env, FAKE_RUN_RC='7')
        env.pop('FORGE_HANDOFF_NO_START')
        result = self.resume('--json', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        started = json.loads(result.stdout)['started']
        self.assertEqual(Path(started['log']).parent, self.plan)
        self.assertRegex(Path(started['done']).name, r'^resume-\d+\.done$')
        for _ in range(100):
            if os.path.exists(started['done']):
                break
            time.sleep(0.1)
        self.assertEqual(Path(started['done']).read_text().strip(), '7')
        self.assertIn('fake run output', Path(started['log']).read_text())
        self.assertTrue(any(c.startswith(f'run {self.plan}') for c in self.calls()))

    def leftover_worker(self, task, role='dwarf'):
        """A process a dead scheduler left behind, and the harness record that names it (pid TAB identity)."""
        process = subprocess.Popen(['sleep', '60'], start_new_session=True)
        self.addCleanup(lambda: (process.kill(), process.wait()))
        guard = str(ROOT / 'scripts' / 'forge-guard.py')
        identity = subprocess.run([sys.executable, guard, 'identity', str(process.pid)], capture_output=True,
                                  text=True).stdout.strip()
        record = self.plan / 'tasks' / task / f'{role}.harness'
        record.write_text(f'{process.pid}\t{identity}\n')
        return process, record

    def test_leftover_worker_is_stopped_before_the_reset(self):
        wt = self.worktree('a')
        self.dirty(wt)
        self.task_files('a', attempt=1)
        process, record = self.leftover_worker('a')
        _, qa_record = self.leftover_worker('a', 'qa')
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': 2 ** 22 + 12345, 'started': 1}))
        result = self.resume('--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        for _ in range(50):
            if process.poll() is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(process.poll(), 'the leftover worker is still running')
        self.assertFalse(record.exists())
        self.assertFalse(qa_record.exists())
        self.assertTrue(json.loads(result.stdout)['recovered'][0]['reset'])
        self.assertFalse((wt / 'new.txt').exists())

    def test_a_harness_that_cannot_be_stopped_leaves_the_worktree_alone(self):
        wt = self.worktree('a')
        self.dirty(wt)
        tdir = self.task_files('a', attempt=1)
        (tdir / 'dwarf.harness').mkdir()   # not a regular file: the guard cannot be asked to stop it
        result = self.resume('--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['recovered'], [])
        self.assertIn('harness', result.stderr)
        self.assertEqual(self.status('a'), 'RUNNING')
        self.assertEqual((wt / 'a.txt').read_text(), 'changed\n')
        self.assertTrue((wt / 'new.txt').exists())

    def test_a_solo_run_is_refused_before_it_is_claimed(self):
        solo = self.root / 'solo'
        solo.mkdir()
        (solo / 'prompt.md').write_text('p\n')
        (solo / 'repo').write_text(str(self.repo) + '\n')
        result = subprocess.run([sys.executable, str(SCRIPT), 'resume', str(solo)], capture_output=True, text=True,
                                env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertFalse((solo / 'handoff.driver.json').exists())

    def test_an_unreadable_flags_file_is_not_called_pre_019(self):
        self.worktree('a')
        self.task_files('a', attempt=1)
        (self.plan / 'run.flags.json').write_text('{not json')
        result = self.resume()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('not readable', result.stdout)
        self.assertNotIn('predates', result.stdout)

    def test_usage_errors(self):
        self.assertEqual(self.resume('--harness', 'BAD HARNESS').returncode, 2)
        missing = subprocess.run([sys.executable, str(SCRIPT), 'resume', str(self.root / 'nope')], capture_output=True,
                                 text=True, env=self.env)
        self.assertEqual(missing.returncode, 2)


if __name__ == '__main__':
    unittest.main()
