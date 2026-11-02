import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_registry  # noqa: E402

NO_DESKTOP = ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS')
HOOK = 'printf "%s|%s|%s|%s|%s\\n" "$FORGE_WATCH_EVENT" "$FORGE_WATCH_RUN_ID" "$FORGE_WATCH_VERDICT" "$FORGE_WATCH_TASK" "$FORGE_WATCH_GOAL" >> "$LOG"'


class WatchTests(unittest.TestCase):
    """`forge watch`: events printed, notified and handed to hooks as runs change. Read-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / 'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.log = self.base / 'events.log'
        self.env = {k: v for k, v in os.environ.items() if k not in NO_DESKTOP}
        self.env.update(XDG_CONFIG_HOME=str(self.base / 'config'), LOG=str(self.log))
        if sys.platform == 'darwin':
            # osascript needs no desktop variable: without this, a test run on a Mac raises real notifications.
            self.env['FORGE_WATCH_NOTIFY'] = 'off'
        self.procs = []
        self.addCleanup(lambda: [p.kill() for p in self.procs if p.poll() is None])

    def make_run(self, name, *statuses, goal='Add retries'):
        run = self.base / name
        run.mkdir()
        rows = ['t%d\t.\tlow\t.\tsol\topus\tTask %d' % (i, i) for i, _ in enumerate(statuses, 1)]
        for file, text in (('run_id', name), ('repo', str(self.repo)), ('goal.txt', goal), ('tasks.tsv', '\n'.join(rows))):
            (run / file).write_text(text + '\n')
        for i, status in enumerate(statuses, 1):
            self.set_status(run, i, status)
        return run

    @staticmethod
    def set_status(run, number, status):
        (run / 'tasks' / ('t%d' % number)).mkdir(parents=True, exist_ok=True)
        path = run / 'tasks' / ('t%d' % number) / 'status'
        path.with_name('status.tmp').write_text(status + '\n')
        os.replace(path.with_name('status.tmp'), path)   # never a half-written file for the watch to read

    def start(self, *args, env=None):
        proc = subprocess.Popen([str(ROOT / 'forge'), 'watch', '--interval', '0.05', '--settle', '0', *args], cwd=self.repo,
                                env=env or self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(proc)
        self.out, self.err, self.lines = [], [], queue.Queue()
        # Read both pipes on threads: a test then waits on the output with a timeout, and a watch that never prints fails it.
        self.readers = [threading.Thread(target=lambda: [self.lines.put(line) for line in proc.stdout], daemon=True),
                        threading.Thread(target=lambda: self.err.extend(proc.stderr), daemon=True)]
        for reader in self.readers:
            reader.start()
        return proc

    def wait_for(self, proc, text):
        """Read stdout until a line has `text`: the watch has reached that point."""
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                line = self.lines.get(timeout=0.2)
            except queue.Empty:
                continue
            self.out.append(line)
            if text in line:
                return
        self.fail('never saw %r; got %r' % (text, self.out))

    def finish(self, proc, expect):
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill(); proc.wait()
            self.fail('the watch did not end; got %r' % self.out)
        for reader in self.readers:
            reader.join(timeout=5)
        while not self.lines.empty():
            self.out.append(self.lines.get())
        err = ''.join(self.err)
        self.assertEqual(proc.returncode, expect, ''.join(self.out) + err)
        return ''.join(self.out), err

    def events(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_a_failed_task_ends_the_run_and_the_hook_gets_each_event(self):
        run = self.make_run('r1', 'RUNNING')
        proc = self.start(str(run), '--exec', HOOK)
        self.wait_for(proc, 'following r1  live  Add retries')
        self.set_status(run, 1, 'FAIL')
        out, _ = self.finish(proc, 1)
        self.assertIn('r1  t1  RUNNING -> FAIL', out)
        self.assertEqual(self.events(), ['task-failed|r1|FAIL|t1|Add retries', 'run-failed|r1|FAIL||Add retries'])

    def test_a_merged_run_reports_its_review_then_its_finish_with_the_verdict(self):
        run = self.make_run('r1', 'RUNNING')
        proc = self.start(str(run), '--exec', HOOK)
        self.wait_for(proc, '  following r1')
        (run / 'final-review.verdict').write_text('PASS\n')
        self.set_status(run, 1, 'MERGED')
        out, _ = self.finish(proc, 0)
        self.assertIn('DONE    run r1 finished: Add retries', out)
        self.assertEqual(self.events(), ['review|r1|PASS||Add retries', 'run-done|r1|PASS||Add retries'])

    def test_a_task_retried_after_failing_is_not_a_failed_run(self):
        run = self.make_run('r1', 'RUNNING')
        proc = self.start(str(run), '--settle', '2', '--exec', HOOK)
        self.wait_for(proc, '  following r1')
        self.set_status(run, 1, 'FAIL')
        self.wait_for(proc, 'FAIL    run r1: task t1 FAIL')
        self.set_status(run, 1, 'RUNNING')                   # the scheduler's automatic retry, within the settle time
        self.wait_for(proc, 'FAIL -> RUNNING')
        self.set_status(run, 1, 'MERGED')
        self.finish(proc, 0)
        self.assertEqual(self.events(), ['task-failed|r1|FAIL|t1|Add retries', 'run-done|r1|PASS||Add retries'])

    def test_a_run_that_was_already_over_is_only_a_baseline(self):
        run = self.make_run('r1', 'MERGED')
        out, _ = self.finish(self.start(str(run), '--exec', HOOK), 0)
        self.assertIn('following r1  done', out)
        self.assertEqual(self.events(), [])

    def test_no_notifier_is_said_once_and_the_events_still_print(self):
        run = self.make_run('r1', 'RUNNING')
        proc = self.start(str(run))
        self.wait_for(proc, '  following r1')
        self.set_status(run, 1, 'MERGED')
        out, err = self.finish(proc, 0)
        self.assertIn('DONE', out)
        self.assertEqual(err.count('no desktop notifications'), 1, err)

    @unittest.skipIf(sys.platform == 'darwin', 'macOS notifies through osascript')
    def test_a_desktop_notification_is_raised_when_a_session_exists_and_not_headless_or_with_no_notify(self):
        bin_dir = self.base / 'bin'; bin_dir.mkdir()
        fake = bin_dir / 'notify-send'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NOTES"\n'); fake.chmod(0o755)
        base_env = dict(self.env, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], NOTES=str(self.base / 'notes'))
        for label, env, extra, notified in (('desktop', dict(base_env, DISPLAY=':0'), (), True),
                                            ('headless', base_env, (), False),
                                            ('opted out', dict(base_env, DISPLAY=':0'), ('--no-notify',), False)):
            with self.subTest(label):
                (self.base / 'notes').unlink(missing_ok=True)
                run = self.make_run('r-' + label.replace(' ', '-'), 'RUNNING')
                proc = self.start(str(run), *extra, env=env)
                self.wait_for(proc, '  following ')
                self.set_status(run, 1, 'MERGED')
                _, err = self.finish(proc, 0)
                if notified:
                    self.assertEqual((self.base / 'notes').read_text().splitlines(),
                                     ['--app-name=forge -- forge: run finished run r-desktop finished: Add retries'])
                else:
                    self.assertFalse((self.base / 'notes').exists())
                    self.assertEqual('no desktop notifications' in err, label == 'headless', err)

    def test_the_repos_on_finish_hook_runs_only_when_it_is_safe(self):
        hook = self.repo / '.forge' / 'on-finish'
        hook.parent.mkdir()
        hook.write_text('#!/bin/sh\n' + HOOK.replace('"$FORGE_WATCH_GOAL"', '"$(pwd)"') + '\n'); hook.chmod(0o755)

        def finished(name, expect_hook):
            run = self.make_run(name, 'RUNNING')
            proc = self.start(str(run))
            self.wait_for(proc, '  following ')
            self.set_status(run, 1, 'MERGED')
            return self.finish(proc, 0)[1]

        with self.subTest('untracked'):
            finished('r1', True)
            self.assertEqual(self.events(), ['run-done|r1|PASS||%s' % self.repo])
        with self.subTest('tracked by git'):
            self.log.unlink()
            subprocess.run(['git', '-C', str(self.repo), 'add', '-f', '.forge/on-finish'], check=True)
            err = finished('r2', False)
            self.assertEqual(self.events(), [])
            self.assertIn('not running', err); self.assertIn('tracked by git', err)
        with self.subTest('writable by others'):
            subprocess.run(['git', '-C', str(self.repo), 'rm', '-q', '--cached', '.forge/on-finish'], check=True)
            hook.chmod(0o777)
            err = finished('r3', False)
            self.assertEqual(self.events(), [])
            self.assertIn('writable by others', err)
        with self.subTest('not executable'):
            hook.chmod(0o644)
            err = finished('r4', False)
            self.assertEqual((self.events(), err.count('not running')), ([], 0))
            self.assertNotIn('did not start', err)   # skipped quietly, never attempted

    def test_a_failing_hook_is_reported_and_does_not_stop_the_watch(self):
        run = self.make_run('r1', 'RUNNING')
        proc = self.start(str(run), '--exec', 'exit 7')
        self.wait_for(proc, '  following ')
        self.set_status(run, 1, 'MERGED')
        out, err = self.finish(proc, 0)
        self.assertIn('DONE', out)
        self.assertIn('--exec exited 7', err)

    def test_a_repo_watch_follows_new_runs_and_stops_when_none_is_running(self):
        first = self.make_run('r1', 'RUNNING')
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.env['XDG_CONFIG_HOME']}):
            forge_registry.register_run_dir(self.repo, first)
            proc = self.start('--repo', str(self.repo), '--until-done', '--exec', HOOK)
            self.wait_for(proc, 'following r1  live')
            second = self.make_run('r2', 'RUNNING', goal='Second')
            forge_registry.register_run_dir(self.repo, second)
            self.wait_for(proc, 'new run r2  live  Second')
            self.set_status(first, 1, 'MERGED')
            self.set_status(second, 1, 'FAIL')
            out, _ = self.finish(proc, 0)
        self.assertIn('nothing is running', out)
        self.assertEqual(sorted(self.events()), ['run-done|r1|PASS||Add retries', 'run-failed|r2|FAIL||Second', 'task-failed|r2|FAIL|t1|Second'])

    def test_a_directory_that_is_not_a_run_is_refused(self):
        proc = self.start(str(self.base))
        _, err = self.finish(proc, 2)
        self.assertIn('is not a forge run', err)


if __name__ == '__main__':
    unittest.main()
