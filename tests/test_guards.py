"""forge-guard.py: the process-group session wrapper, the reaper, and the promise scan."""
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'scripts' / 'forge-guard.py'

spec = importlib.util.spec_from_file_location('forge_guard', GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_until(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def run(*args, **kwargs):
    return subprocess.run([sys.executable, str(GUARD), *map(str, args)], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=30, **kwargs)


# (message, the pattern names that must be reported). Positives are phrased the way an
# implementer's last message actually promises later work.
PROMISES = [
    ("The full suite is running in the background; I'll check the result shortly.", {'background', 'ill_do'}),
    ('Tests are still running.', {'still_running'}),
    ('The migration is still in progress.', {'still_running'}),
    ('It is still running and I will report back when it ends.', {'still_running', 'report_back'}),
    ("I'll report back with the numbers.", {'report_back', 'ill_do'}),
    ("Once it finishes I'll verify the output.", {'once_it_finishes', 'ill_do'}),
    ('Once the build completes, the numbers will be in build.log.', {'once_it_finishes'}),
    ("I'll run the integration tests next.", {'ill_do'}),
    ("Next I'll measure the bundle size.", {'next_ill'}),
    ('Next, I will wire the handler.', {'next_ill'}),
    ('I will continue with the remaining endpoints.', {'will_continue'}),
    ('The import will finish later tonight.', {'will_continue'}),
    ('To be continued.', {'to_be_continued'}),
    ("I'm waiting for the server to come up.", {'waiting_for'}),
    ('I am still waiting on the build.', {'waiting_for'}),
    ('Implemented the feature. Remaining checks:', {'trailing_colon'}),
    ('Done with the parser.\n\nStill to do:\n', {'trailing_colon'}),
    ('I will verify the migration once the DB is up.', {'ill_do'}),
    ("I’ll follow up on the failing case.", {'ill_do'}),
]
# Past-tense reports and descriptions of code, which look similar and must stay quiet.
CLEAN = [
    'I ran the suite in the background and it finished: 120 passed.',
    'It ran in the background and passed.',
    'The dev server was started in the background and exited cleanly once the checks completed.',
    'The job is no longer running; the process completed in 4s.',
    'The change adds a retry to the background job scheduler.',
    'The function will continue to work for existing callers.',
    'This change continues to honour the old flag.',
    'I changed the waiting for lock logic in queue.py.',
    'Implemented the handler and verified with npm test: 42 passed.',
    'Summary:\n- fixed the bug\n- added a test\nAll 12 tests passed.',
    'I checked the output and it matches.',
    'The next step for the team is to deploy.',
    'Remaining work is out of scope.',
    'Result: PASS',
    '',
    '   \n\n',
]


class PromiseScanTests(unittest.TestCase):
    def test_positives(self):
        for message, expected in PROMISES:
            with self.subTest(message):
                found = {name for name, _ in guard.scan_promises(message)}
                self.assertTrue(expected <= found, (expected, found))

    def test_negatives(self):
        for message in CLEAN:
            with self.subTest(message):
                self.assertEqual(guard.scan_promises(message), [])

    def test_excerpt_is_the_offending_sentence(self):
        text = 'All tests pass. The suite is still running in CI. Nothing else.'
        [(name, excerpt)] = guard.scan_promises(text)
        self.assertEqual((name, excerpt), ('still_running', 'The suite is still running in CI'))

    def test_cli_prints_pattern_tab_excerpt_and_nothing_when_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            message = Path(tmp) / 'dwarf.last'
            message.write_text("The server keeps running in the background.\n")
            result = run('promises', message)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ['background\tThe server keeps running in the background'])
            message.write_text('Done. All 3 tests pass.\n')
            self.assertEqual(run('promises', message).stdout, '')
            self.assertEqual(run('promises', Path(tmp) / 'absent').returncode, 0)

    def test_only_the_tail_is_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            message = Path(tmp) / 'dwarf.last'
            message.write_text("I'll check the logs next.\n" + 'filler line that says nothing much.\n' * 400 + 'Final answer: all done.\n')
            self.assertEqual(run('promises', message).stdout, '')
            self.assertEqual(run('promises', message, '--tail-bytes', 100000).stdout.split('\t')[0], 'ill_do')
            # A promise inside the window is found, and a cut that lands mid-sentence is not reported.
            message.write_text('x' * 5000 + ' and then the tail ends with I will report back.')
            self.assertEqual(run('promises', message).stdout.split('\t')[0], 'report_back')
            message.write_text('word ' * 2000 + "I'll")
            self.assertEqual(run('promises', message, '--tail-bytes', '50').stdout, '')

    def test_bad_usage(self):
        self.assertEqual(run().returncode, 2)
        self.assertEqual(run('promises').returncode, 2)
        self.assertEqual(run('promises', 'f', '--tail-bytes', 'many').returncode, 2)
        self.assertEqual(run('nonsense').returncode, 2)


class SessionTests(unittest.TestCase):
    def test_ping(self):
        self.assertEqual(run('ping').stdout, 'ok\n')

    def test_same_pid_new_session_and_group(self):
        probe = 'import os; print(os.getpid(), os.getpgrp(), os.getsid(0))'
        process = subprocess.Popen([sys.executable, str(GUARD), 'session', '--', sys.executable, '-c', probe],
                                   stdout=subprocess.PIPE, text=True)
        out, _ = process.communicate(timeout=30)
        pid, pgid, sid = map(int, out.split())
        self.assertEqual(pid, process.pid)                 # exec, not spawn: $! stays the harness pid
        self.assertEqual((pgid, sid), (pid, pid))          # so the pid doubles as the group id

    def test_arguments_and_stdin_reach_the_command_untouched(self):
        script = 'import sys; print(sys.argv[1:], sys.stdin.read())'
        result = subprocess.run([sys.executable, str(GUARD), 'session', '--', sys.executable, '-c', script, '--odd', 'a b', '-'],
                                input='from stdin', stdout=subprocess.PIPE, text=True, timeout=30)
        self.assertEqual(result.stdout, "['--odd', 'a b', '-'] from stdin\n")

    def test_exit_status_passes_through_and_missing_command_is_127(self):
        self.assertEqual(run('session', '--', sys.executable, '-c', 'import sys; sys.exit(7)').returncode, 7)
        missing = run('session', '--', 'definitely-not-a-command-forge')
        self.assertEqual(missing.returncode, 127)
        self.assertEqual(run('session', '--').returncode, 2)

    def test_works_when_already_a_group_leader(self):
        # setsid() fails for a group leader; the helper must carry on instead of dying.
        probe = 'import os; print(os.getpid() == os.getpgrp())'
        result = subprocess.run([sys.executable, str(GUARD), 'session', '--', sys.executable, '-c', probe],
                                stdout=subprocess.PIPE, text=True, timeout=30, start_new_session=True)
        self.assertEqual(result.stdout.strip(), 'True')


class ReapTests(unittest.TestCase):
    def leave_orphans(self, count=2):
        """Start a session whose command backgrounds `count` sleeps and exits: classic orphans."""
        script = ' '.join(['sleep 120 >/dev/null 2>&1 </dev/null &'] * count) + ' echo $!'
        leader = subprocess.Popen([sys.executable, str(GUARD), 'session', '--', '/bin/sh', '-c', script],
                                  stdout=subprocess.PIPE, text=True)
        out, _ = leader.communicate(timeout=30)
        last = int(out.split()[-1])
        self.addCleanup(lambda: alive(last) and os.kill(last, signal.SIGKILL))
        self.assertTrue(wait_until(lambda: alive(last), 5))
        return leader.pid, last

    def test_orphans_are_listed_then_killed(self):
        pgid, last = self.leave_orphans(2)
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / 'dwarf.orphans'
            result = run('reap', pgid, listing)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), '2')
            lines = listing.read_text().splitlines()
            self.assertEqual(len(lines), 2, lines)
            for line in lines:
                pid, _, command = line.partition('\t')
                self.assertTrue(pid.isdigit() and 'sleep 120' in command, line)
            self.assertIn(str(last), [line.split('\t')[0] for line in lines])
        self.assertTrue(wait_until(lambda: not alive(last), 5), 'orphan survived the reaper')
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)

    def test_a_term_ignoring_orphan_is_killed_after_the_grace_period(self):
        script = "trap '' TERM; exec sleep 120"
        leader = subprocess.Popen([sys.executable, str(GUARD), 'session', '--', '/bin/sh', '-c',
                                   "/bin/sh -c \"%s\" >/dev/null 2>&1 </dev/null & echo $!" % script], stdout=subprocess.PIPE, text=True)
        out, _ = leader.communicate(timeout=30)
        pid = int(out.split()[-1])
        self.addCleanup(lambda: alive(pid) and os.kill(pid, signal.SIGKILL))
        time.sleep(0.3)      # let the shell install its trap before TERM arrives
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run('reap', leader.pid, Path(tmp) / 'o').stdout.strip(), '1')
        self.assertTrue(wait_until(lambda: not alive(pid), 5))

    def test_clean_group_prints_zero_and_writes_nothing(self):
        leader = subprocess.Popen([sys.executable, str(GUARD), 'session', '--', sys.executable, '-c', 'pass'])
        leader.wait(timeout=30)
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / 'dwarf.orphans'
            result = run('reap', leader.pid, listing)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, '0'))
            self.assertFalse(listing.exists())

    def test_never_signals_its_own_or_its_callers_group(self):
        # The reaper's own pgid is `$$` of the shell that exec'd it: asking it to reap that
        # group must be refused, or the helper would kill itself and its caller.
        with tempfile.TemporaryDirectory() as tmp:
            listing = Path(tmp) / 'o'
            result = subprocess.run(['/bin/sh', '-c', 'exec "$0" "$1" reap $$ "$2"', sys.executable, str(GUARD), str(listing)],
                                    stdout=subprocess.PIPE, text=True, timeout=30, start_new_session=True)
            self.assertEqual((result.returncode, result.stdout.strip()), (0, '0'))
            self.assertFalse(listing.exists())
        for pgid in ('0', '1', '-5'):
            self.assertEqual(run('reap', pgid, '/dev/null').stdout.strip(), '0', pgid)

    def test_bad_usage(self):
        self.assertEqual(run('reap').returncode, 2)
        self.assertEqual(run('reap', 'notanumber', '/dev/null').returncode, 2)


if __name__ == '__main__':
    unittest.main()
