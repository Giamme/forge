"""The scheduler re-dispatches a STALE attempt with the new prompt, and no attempt is spent (#150).

Run cases drive forge-parallel.sh with the fake agent CLI from test_forge.py, as test_prompt_drift.py does: the
implementer's scenario `files` writes the plan's prompt.md by absolute path on call 1 only, which makes that
attempt STALE. The preparation cases call `_prepare_retry <plan> <id> --prompt` on a hand-made fixture."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import shutil
import sys
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
NEW = 'Implement change. TASK_a\nNEW_LINE\n'
CONSOLE = 'forge: a: re-dispatching with the changed prompt (no attempt spent)'
STALE_DIFF = '--- prompt.md (attempt 2)\n+++ prompt.md (now)\n@@ -1 +1 @@\n-OLD_LINE\n+NEW_LINE\n'


def load_schedule():
    spec = importlib.util.spec_from_file_location('forge_schedule_drift', SCRIPTS / 'forge-schedule.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['forge_schedule_drift'] = module
    spec.loader.exec_module(module)
    return module


class PromptDriftScheduleCase(unittest.TestCase):
    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        (self.bin / 'diff').symlink_to(shutil.which('diff'))

    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, *flags):
        p = self.root / 'plan'
        p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks' / 'a').mkdir(parents=True)
        (p / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. TASK_a\nOLD_LINE\n')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def read(self, p, name):
        return (p / 'tasks' / 'a' / name).read_text()

    def attempts(self, p):
        return [tuple(line.split('\t')[1:3]) for line in self.read(p, 'attempts.tsv').splitlines()]

    def edit_on_first_dwarf_call(self, p):
        # The second attempt writes something new: a retry whose diff is byte-identical to the last is failed unreviewed.
        self.scenario({'a.dwarf.1': {'files': {str(p / 'tasks' / 'a' / 'prompt.md'): NEW}},
                       'a.dwarf.2': {'content': 'updated for the new prompt'}})

    def stale_fixture(self, p, attempt=2, extra=None):
        """What an attempt left behind when its prompt changed: STALE, the diff, and the committed work's review."""
        t = p / 'tasks' / 'a'
        t.joinpath('status').write_text('STALE\n')
        t.joinpath('attempt').write_text('%d\n' % attempt)
        t.joinpath('stale.diff').write_text(STALE_DIFF)
        t.joinpath('prompt.md').write_text(NEW)
        for name, text in (extra or {}).items():
            t.joinpath(name).write_text(text)
        return t

    def prepare(self, p):
        return self.run_script('forge-parallel.sh', '_prepare_retry', p, 'a', '--prompt')

    # --- the scheduler ---------------------------------------------------------------
    def test_a_stale_attempt_is_dispatched_again_with_the_new_prompt_and_spends_no_retry(self):
        p = self.plan_of()
        self.edit_on_first_dwarf_call(p)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.read(p, 'status').strip(), 'PASS', r.stdout)
        self.assertIn('a              MERGED', r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)
        self.assertEqual(self.read(p, 'attempt').strip(), '1')                     # the stale attempt's number is reused
        self.assertEqual(self.attempts(p), [('1', 'STALE'), ('1', 'PASS')])
        dwarf = self.read(p, 'dwarf.input')
        self.assertIn('NEW_LINE', dwarf)
        self.assertIn('The requirements changed while you worked', dwarf)
        self.assertIn('-OLD_LINE', dwarf)
        self.assertIn('+NEW_LINE', dwarf)
        retries = p / 'tasks' / 'a' / 'retries.used'
        self.assertTrue(not retries.exists() or retries.read_text().strip() == '0')
        self.assertFalse((p / 'tasks' / 'a' / 'decided.left').exists())
        self.assertEqual(r.stdout.count(CONSOLE), 1, r.stdout)
        self.assertNotIn('automatic retry', r.stdout)

    def test_the_next_attempt_is_recorded_as_a_prompt_retry(self):
        p = self.plan_of()
        self.edit_on_first_dwarf_call(p)
        self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        rows = [line.split('\t') for line in self.read(p, 'retry_sources.tsv').splitlines()]
        self.assertEqual([row[:2] for row in rows], [['1', 'prompt']])

    def test_a_stale_task_is_re_dispatched_once_without_any_retry_budget(self):
        p = self.plan_of()
        self.edit_on_first_dwarf_call(p)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 0)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.read(p, 'status').strip(), 'PASS', r.stdout)
        self.assertIn('a              MERGED', r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)
        self.assertIn(CONSOLE, r.stdout)

    def escalation_run(self, escalate_after):
        p = self.plan_of('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        t = p / 'tasks' / 'a'
        t.joinpath('attempt').write_text('2\n')
        t.joinpath('attempts.tsv').write_text('1700000000\t1\tFAIL\t\tsol\n1700000001\t2\tFAIL\t\tsol\n')
        self.edit_on_first_dwarf_call(p)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1, '--escalate-after', escalate_after)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.attempts(p), [('1', 'FAIL'), ('2', 'FAIL'), ('3', 'STALE'), ('3', 'PASS')], r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)
        for name in ('escalations.tsv', 'escalation.stop', 'escalation.next', 'split.suggested', 'decision.md'):
            self.assertFalse((p / 'tasks' / 'a' / name).exists(), name)
        self.assertNotIn('escalating', r.stdout)
        rows = [line.split('\t') for line in (p / 'tasks.tsv').read_text().splitlines() if not line.startswith('#')]
        self.assertEqual(rows[0][4], 'sol')

    def test_the_stale_re_dispatch_does_not_escalate_the_model(self):
        self.escalation_run(3)

    def test_the_stale_re_dispatch_does_not_escalate_even_when_the_policy_is_due(self):
        self.escalation_run(2)   # two failures are enough here: the prompt path asks the policy nothing

    def test_a_stale_task_left_by_an_interrupted_run_is_re_dispatched_at_the_next_run(self):
        p = self.plan_of()
        self.stale_fixture(p, attempt=1)
        t = p / 'tasks' / 'a'
        t.joinpath('attempts.tsv').write_text('1700000000\t1\tSTALE\t\tsol\n')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.read(p, 'status').strip(), 'PASS', r.stdout)
        self.assertIn('a              MERGED', r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 1)
        self.assertIn('+NEW_LINE', self.read(p, 'dwarf.input'))
        self.assertEqual(self.attempts(p), [('1', 'STALE'), ('1', 'PASS')])
        self.assertIn(CONSOLE, r.stdout)

    # --- the preparation -----------------------------------------------------------------
    def test_the_preparation_writes_the_findings_and_gives_the_attempt_back(self):
        p = self.plan_of()
        t = self.stale_fixture(p, extra={'retry_findings.commit': 'abc\n', 'resume': 'qa\n', 'noretry': '',
                                         'retry_findings.session': 's\n', 'old': ''})
        r = self.prepare(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.read(p, 'attempt').strip(), '1')
        findings = self.read(p, 'retry_findings.md')
        self.assertIn('The requirements changed while you worked', findings)
        self.assertIn('The full new requirements are above', findings)
        self.assertIn('keep what still holds, and explain anything you change', findings)
        self.assertTrue(findings.rstrip().endswith('```'))
        self.assertIn('```diff\n' + STALE_DIFF + '```', findings)
        self.assertEqual(self.read(p, 'retry_findings.source').strip(), 'prompt')
        for name in ('retry_findings.commit', 'resume', 'status', 'noretry', 'retry_findings.session'):
            self.assertFalse((t / name).exists(), name)
        # The gates that spend or judge an attempt did not run.
        for name in ('escalations.tsv', 'escalation.stop', 'escalation.next', 'decision.md', 'decisions.tsv'):
            self.assertFalse((t / name).exists(), name)

    def test_the_attempt_number_never_goes_below_zero(self):
        p = self.plan_of()
        self.stale_fixture(p, attempt=0)
        self.assertEqual(self.prepare(p).returncode, 0)
        self.assertEqual(self.read(p, 'attempt').strip(), '0')

    def test_the_implementer_session_is_carried_only_for_a_plan_that_keeps_it(self):
        reviewed = {'reviewed.dwarf.session': 'sess-1\n', 'reviewed.dwarf.resolved': 'claude:sol\n'}
        p = self.plan_of()
        (p / 'implementer_session').write_text('on\n')
        t = self.stale_fixture(p, extra=reviewed)
        self.assertEqual(self.prepare(p).returncode, 0)
        self.assertEqual((t / 'retry_findings.dwarf.session').read_text(), 'sess-1\n')
        self.assertEqual((t / 'retry_findings.dwarf.resolved').read_text(), 'claude:sol\n')
        (p / 'implementer_session').unlink()
        self.stale_fixture(p, extra=reviewed)
        self.assertEqual(self.prepare(p).returncode, 0)
        self.assertFalse((t / 'retry_findings.dwarf.session').exists())
        self.assertFalse((t / 'retry_findings.dwarf.resolved').exists())

    def test_only_a_stale_task_is_prepared(self):
        p = self.plan_of()
        t = p / 'tasks' / 'a'
        t.joinpath('status').write_text('FAIL\n')
        t.joinpath('attempt').write_text('2\n')
        t.joinpath('retry_findings.md').write_text('older findings\n')
        before = {f.name: f.read_bytes() for f in t.iterdir() if f.is_file()}
        r = self.prepare(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('not STALE', r.stdout)
        self.assertEqual({f.name: f.read_bytes() for f in t.iterdir() if f.is_file()}, before)

    # --- what `forge control retry` may retry ------------------------------------------------
    def test_control_retry_accepts_a_stale_task_but_the_budget_never_does(self):
        schedule = load_schedule()
        self.assertIn('STALE', schedule.RETRYABLE)
        self.assertNotIn('STALE', schedule.AUTO_RETRY)


if __name__ == '__main__':
    unittest.main()
