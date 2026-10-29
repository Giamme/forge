"""plan --implementer-session: a retry continues the implementer's own session, or goes fresh (#90).

Drives forge-parallel.sh with the fake agent CLI of test_forge and reads the argv the dwarf
was given from dwarf.cmd (claude's `--resume <id>`). The fake publishes CLAUDE_SESSION as
claude's session id, and CODEX_THREAD as codex's."""
import json
import shlex
import sys
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from forge_dataset import harvest  # noqa: E402  the dataset's view of the plan and its dispatches
SESSION = fixtures.CLAUDE_SESSION
FAIL = {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'}
# Each retry's dwarf writes something new, or the diff is identical and nothing is reviewed again.
DWARVES = {'a.dwarf.%d' % n: {'content': 'attempt %d' % n} for n in range(2, 6)}


def resumed(cmd_file):
    argv = shlex.split(Path(cmd_file).read_text())
    return argv[argv.index('--resume') + 1] if '--resume' in argv else None


def rows(path):
    return [line.split('\t') for line in Path(path).read_text().splitlines()]


class ImplementerSessionTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, *flags, dwarf='opus', ids='a', name='plan'):
        p = self.root / name; p.mkdir()
        (p / 'tasks.tsv').write_text(''.join('%s\t-\tlow\t%s.txt\t%s\topus\t%s\n' % (i, i, dwarf, i.upper()) for i in ids))
        for i in ids:
            t = p / 'tasks' / i; t.mkdir(parents=True)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.plan_output = r.stdout
        return p

    def first_attempt_fails(self, *flags, dwarf='opus', ids='a', later=None):
        """Task a's attempt 1 is reviewed FAIL; `later` is the scenario for the calls after it. The
        fake counts calls per scenario file, so the keys are the task's overall call numbers."""
        self.scenario(dict(DWARVES, **{'a.qa.1': FAIL}, **(later or {})))
        p = self.plan_of(*flags, dwarf=dwarf, ids=ids)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        return p

    def retry(self, p, *args):
        return self.run_script('forge-parallel.sh', 'retry', p, 'a', *args)

    def log(self, p):
        return rows(p / 'tasks/a/implementer_sessions.tsv')

    def status(self, p):
        return (p / 'tasks/a/status').read_text().strip()

    # --- the option --------------------------------------------------------------------------
    def test_the_option_is_a_plan_file_and_one_summary_line(self):
        p = self.plan_of('--implementer-session')
        self.assertEqual((p / 'implementer_session').read_text(), 'on\n')
        self.assertEqual(sum('implementer-session' in line for line in self.plan_output.splitlines()), 1)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--no-implementer-session')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((p / 'implementer_session').exists())

    def test_the_dataset_records_the_option_and_the_continued_dispatch(self):
        p = self.first_attempt_fails('--implementer-session')
        self.assertEqual(self.retry(p).returncode, 0)
        event = harvest.plan_event(p, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual(event['data']['options'].get('implementer_session'), 'on')
        # Attempt dirs are named by uuid; their order is the time each dispatch started.
        first, second = sorted((p / 'tasks/a/attempts').glob('dwarf-*'),
                               key=lambda d: json.loads((d / 'metrics.json').read_text())['wall_started'])
        self.assertIsNone(harvest.dispatch_event(first, p / 'tasks/a', 'dwarf', 'claude', 0, {})['data']['session_continue'])
        self.assertEqual(harvest.dispatch_event(second, p / 'tasks/a', 'dwarf', 'claude', 0, {})['data']['session_continue'],
                         'continued')

    def test_off_by_default_nothing_continues_and_nothing_is_logged(self):
        p = self.first_attempt_fails()
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertIsNone(resumed(p / 'tasks/a/dwarf.cmd'))
        self.assertFalse((p / 'tasks/a/implementer_sessions.tsv').exists())

    # --- continuing --------------------------------------------------------------------------
    def test_a_retry_with_the_same_dwarf_spec_continues_its_session(self):
        p = self.first_attempt_fails('--implementer-session')
        tdir = p / 'tasks/a'
        self.assertIsNone(resumed(tdir / 'dwarf.cmd'))
        self.assertEqual((tdir / 'dwarf.prompt').read_text(), (tdir / 'dwarf.input').read_text())
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertEqual(self.status(p), 'PASS')
        self.assertEqual(resumed(tdir / 'dwarf.cmd'), SESSION)
        # The continued session is told what changed, above this attempt's full input, findings included.
        sent, given = (tdir / 'dwarf.prompt').read_text(), (tdir / 'dwarf.input').read_text()
        self.assertTrue(sent.endswith(given) and len(sent) > len(given))
        self.assertIn('a.txt:1 — broken', given)
        self.assertEqual(self.log(p), [['1', '1', 'fresh', 'first-attempt', ''],
                                       ['2', '2', 'continued', 'same-spec', SESSION]])

    def test_an_escalated_retry_starts_fresh(self):
        p = self.first_attempt_fails('--implementer-session')
        self.assertEqual(self.retry(p, '--dwarf', 'sonnet').returncode, 0)
        self.assertIsNone(resumed(p / 'tasks/a/dwarf.cmd'))
        self.assertEqual(self.log(p)[-1], ['2', '2', 'fresh', 'spec-changed', ''])

    def test_a_codex_implementer_starts_fresh(self):
        # codex's `exec resume` cannot be given the implementer's write access.
        p = self.first_attempt_fails('--implementer-session', dwarf='sol')
        self.assertEqual(self.retry(p).returncode, 0)
        argv = shlex.split((p / 'tasks/a/dwarf.cmd').read_text())
        self.assertNotIn('resume', argv)
        self.assertEqual(self.log(p)[-1], ['2', '2', 'fresh', 'harness-unsupported', ''])

    def test_work_that_changed_since_its_review_starts_fresh(self):
        # A timed-out or interrupted implementer leaves edits the earlier session never saw.
        p = self.first_attempt_fails('--implementer-session')
        wt = Path((p / 'wt_root').read_text().strip()) / 'a'
        (wt / 'a.txt').write_text('edited after the review\n')
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertIsNone(resumed(p / 'tasks/a/dwarf.cmd'))
        self.assertEqual(self.log(p)[-1], ['2', '2', 'fresh', 'work-changed', ''])

    def test_a_retry_with_no_reviewed_session_starts_fresh(self):
        p = self.first_attempt_fails('--implementer-session')
        (p / 'tasks/a/reviewed.dwarf.session').unlink()
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertIsNone(resumed(p / 'tasks/a/dwarf.cmd'))
        self.assertEqual(self.log(p)[-1], ['2', '2', 'fresh', 'no-session', ''])

    # --- when continuing fails ---------------------------------------------------------------
    def test_a_continue_that_changed_nothing_is_followed_by_a_fresh_dispatch_that_decides(self):
        # dwarf.2 is attempt 2's continued dispatch (it fails at once); dwarf.3, the fresh one, works.
        p = self.first_attempt_fails('--implementer-session', later={'a.dwarf.2': {'continue_fails': True}})
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertEqual(self.status(p), 'PASS')
        self.assertEqual(self.calls().count('dwarf'), 3)
        self.assertIsNone(resumed(p / 'tasks/a/dwarf.cmd'))      # the one that counted
        self.assertTrue((p / 'tasks/a/dwarf.continue.out').exists())
        self.assertEqual(self.log(p)[1:], [['2', '2', 'fresh', 'continue-failed', SESSION]])

    def test_a_continue_that_failed_after_changing_files_is_not_run_again(self):
        # Re-running it fresh would pay for the attempt twice, on top of work it half did.
        p = self.first_attempt_fails('--implementer-session', later={'a.dwarf.2': {'content': 'half done', 'rc': 1}})
        self.assertNotEqual(self.retry(p).returncode, 0)
        self.assertEqual(self.status(p), 'ERROR')
        self.assertEqual(self.calls().count('dwarf'), 2)
        self.assertEqual(self.log(p)[1:], [['2', '2', 'failed', 'continue-failed', SESSION]])

    # --- derived plans -----------------------------------------------------------------------
    def test_split_carries_the_option_and_the_retry_continues(self):
        # Two tasks so split can move the failed one and leave the merged one behind.
        p = self.first_attempt_fails('--implementer-session', ids='ab')
        new = self.root / 'second'
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((new / 'implementer_session').read_text(), 'on\n')
        self.assertEqual(self.run_script('forge-parallel.sh', 'retry', new, 'a').returncode, 0)
        self.assertEqual(resumed(new / 'tasks/a/dwarf.cmd'), SESSION)


if __name__ == '__main__':
    unittest.main()
