"""A prompt edited while its attempt runs (#150): the attempt is STALE, not reviewed, and spends nothing.

Run cases drive forge-parallel.sh with the fake agent CLI from test_forge.py, as test_outside_stop.py does. The
implementer's scenario key `files` writes any path, so an absolute path to the plan's prompt.md edits it
mid-attempt. The unit cases call forge-route.py directly."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / 'scripts' / 'forge-route.py'


def load_route():
    spec = importlib.util.spec_from_file_location('forge_route_drift', ROUTE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PromptDriftCase(unittest.TestCase):
    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        # The fake PATH lists only the tools the older scripts use; stale.diff is made by diff(1).
        (self.bin / 'diff').symlink_to(shutil.which('diff'))

    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self):
        p = self.root / 'plan'
        p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks' / 'a').mkdir(parents=True)
        (p / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. TASK_a\nOLD_LINE\n')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def edit_during_dwarf(self, p, name, text):
        # The scheduler re-dispatches a STALE task (test_prompt_drift_schedule.py); that second attempt writes
        # something new, or forge fails it as a byte-identical retry.
        return {'a.dwarf.1': {'files': {str(p / 'tasks' / 'a' / name): text}},
                'a.dwarf.2': {'content': 'written for the new text'}}

    def stale_diff(self, p):
        """stale.diff as the stale attempt wrote it: the re-dispatched attempt removes the file after its own check,
        and the diff stays in the findings it was given."""
        return self.read(p, 'retry_findings.md')

    def read(self, p, name):
        return (p / 'tasks' / 'a' / name).read_text()

    def control_log(self, p):
        return [json.loads(line) for line in (p / 'control.log').read_text().splitlines()]

    def assert_stale_and_free(self, p, r):
        # The stale attempt came first and was not reviewed: both dispatches are implementers, QA comes after.
        self.assertEqual(self.calls(), ['dwarf', 'dwarf', 'qa'], r.stdout)
        rows = [line.split('\t') for line in self.read(p, 'attempts.tsv').splitlines()]
        self.assertEqual([row[1:3] for row in rows], [['1', 'STALE'], ['1', 'PASS']])   # the attempt number is reused
        used = subprocess.run([sys.executable, str(ROUTE), 'attempt-cap', '--plan', str(p), '--task', 'a'],
                              capture_output=True, text=True, timeout=60).stdout.split()
        self.assertEqual(used[0], '1')                                  # the stale attempt spent nothing
        for name in ('escalation.next', 'escalation.stop', 'decision.md'):
            self.assertFalse((p / 'tasks' / 'a' / name).exists(), name)

    def test_a_prompt_edited_mid_attempt_is_stale_and_unreviewed(self):
        p = self.plan_of()
        self.scenario(self.edit_during_dwarf(p, 'prompt.md', 'Implement change. TASK_a\nNEW_LINE\n'))
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assert_stale_and_free(p, r)
        diff = self.stale_diff(p)
        self.assertIn('--- prompt.md (attempt 1)', diff)
        self.assertIn('+++ prompt.md (now)', diff)
        self.assertIn('-OLD_LINE', diff)
        self.assertIn('+NEW_LINE', diff)
        last = self.control_log(p)[-1]
        self.assertEqual((last['action'], last['actor'], last['task'], last['value']),
                         ('prompt-changed', 'scheduler', 'a', 1))
        self.assertEqual(last['files'], ['prompt.md'])
        note = self.read(p, 'task.out')
        self.assertEqual(note.count('forge: a: prompt.md changed during attempt 1 — not reviewed; the implementer gets the new text (no attempt spent)'), 1, note)
        self.assertTrue((p / 'tasks/a/reviewed.commit').exists())      # the committed work stays

    def test_an_approach_edited_mid_attempt_is_stale_and_named(self):
        p = self.plan_of()
        (p / 'tasks/a/approach.md').write_text('use the old way\n')
        self.scenario(self.edit_during_dwarf(p, 'approach.md', 'use the new way\n'))
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assert_stale_and_free(p, r)
        self.assertIn('approach.md changed during attempt 1', self.read(p, 'task.out'))
        self.assertNotIn('prompt.md changed', self.read(p, 'task.out'))
        self.assertIn('+use the new way', self.stale_diff(p))
        self.assertEqual(self.control_log(p)[-1]['files'], ['approach.md'])

    def test_an_approach_created_mid_attempt_counts(self):
        p = self.plan_of()
        self.scenario(self.edit_during_dwarf(p, 'approach.md', 'a new approach\n'))
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assert_stale_and_free(p, r)
        # The stale attempt had an empty approach; the re-dispatch was given, and recorded, the new one.
        self.assertIn('+a new approach', self.stale_diff(p))
        self.assertEqual((p / 'tasks/a/approach.dispatched').read_text(), 'a new approach\n')

    def test_an_unedited_attempt_is_reviewed_as_before(self):
        p = self.plan_of()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.read(p, 'status').strip(), 'PASS', r.stdout)
        self.assertIn('qa', self.calls())
        self.assertEqual(self.read(p, 'prompt.dispatched'), self.read(p, 'prompt.md'))
        self.assertFalse((p / 'tasks/a/stale.diff').exists())
        self.assertFalse((p / 'control.log').exists())

    def resume_state(self):
        """Attempt 1 implemented, its review gave no verdict: the next entry re-reviews it (QA-only)."""
        p = self.plan_of()
        self.scenario({'a.qa.1': {'findings': 'No verdict.', 'verdict': 'none'}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.read(p, 'status').strip(), 'UNKNOWN')
        self.assertEqual(self.calls().count('dwarf'), 1)
        self.scenario({})
        return p

    def test_a_qa_only_resume_of_an_attempt_without_the_copies_is_reviewed(self):
        p = self.resume_state()
        (p / 'tasks/a/prompt.dispatched').unlink()                    # an attempt started before this change
        (p / 'tasks/a/approach.dispatched').unlink()
        (p / 'tasks/a/prompt.md').write_text('Implement change. TASK_a\nEDITED\n')
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(self.read(p, 'status').strip(), 'PASS', r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 1)              # QA only
        self.assertFalse((p / 'tasks/a/stale.diff').exists())

    def test_a_qa_only_resume_checks_the_copies_of_the_attempt_it_reviews(self):
        p = self.resume_state()
        (p / 'tasks/a/prompt.md').write_text('Implement change. TASK_a\nEDITED\n')
        self.scenario({'a.dwarf.2': {'content': 'written for the new text'}})   # the re-dispatch must change something
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        # The resumed review found the edit and did not review: the implementer ran again, and its attempt was reviewed.
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'qa'], r.stdout)
        self.assertEqual(self.read(p, 'status').strip(), 'PASS', r.stdout)
        self.assertIn('+EDITED', self.read(p, 'retry_findings.md'))
        self.assertFalse((p / 'tasks/a/resume').exists())


class StaleCountingTests(unittest.TestCase):
    route = load_route()

    def test_a_stale_attempt_is_neutral_in_the_failed_streak(self):
        rows = [(1, 'FAIL', 'sol'), (2, 'STALE', 'sol'), (3, 'FAIL', 'sol')]
        self.assertEqual(self.route.failed_streak(rows, 'sol', {}), 2)

    def test_attempts_spent_skips_stale_and_infra(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            (tdir / 'attempts.tsv').write_text(''.join(
                '1700000000\t%d\t%s\t\tsol\n' % (i + 1, s) for i, s in enumerate(['FAIL', 'STALE', 'INFRA', 'FAIL'])))
            self.assertEqual(self.route.attempts_spent(tdir), 2)


if __name__ == '__main__':
    unittest.main()
