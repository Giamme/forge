"""Queued steering (#134): `forge control steer` stores a message, the task's next implementer attempt gets it
(implementer and reviewer prompts), the dataset marks the attempt, and analyses leave steered attempts out."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / 'scripts' / 'forge-control.py'
sys.path.insert(0, str(ROOT / 'scripts'))

TASKS = 'a\t-\tlow\ta.txt\tsol\topus\tFirst\nb\ta\tlow\tb.txt\tsol\topus\tSecond\n'


class SteerControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.plan = Path(tmp.name).resolve() / 'plan'
        (self.plan / 'tasks' / 'a').mkdir(parents=True)
        (self.plan / 'tasks' / 'b').mkdir()
        (self.plan / 'tasks.tsv').write_text(TASKS)
        self.env = dict(os.environ, FORGE_DATASET='off')

    def control(self, *args, stdin=None):
        return subprocess.run([sys.executable, str(CONTROL), *map(str, args)], capture_output=True, text=True,
                              input=stdin, env=self.env, timeout=60)

    def ok(self, *args, stdin=None):
        r = self.control(*args, stdin=stdin)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def entries(self, name='steer.jsonl', task='a'):
        path = self.plan / 'tasks' / task / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def log(self):
        path = self.plan / 'control.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_a_message_waits_for_the_next_attempt_and_the_log_keeps_no_text(self):
        r = self.ok('steer', self.plan, 'a', '  Use the existing parser.  ')
        self.assertIn('queued a message for task a', r.stdout)
        self.assertIn('starts nothing', r.stdout)
        out = json.loads(self.ok('steer', self.plan, 'a', '--file', '-', '--actor', 'ui', '--json',
                                 stdin='Keep the API.\n').stdout)
        self.assertEqual((out['queued'], out['waiting'], out['live'], out['chars']), (True, 2, False, 13))
        texts = [(e['actor'], e['text'], e['chars']) for e in self.entries()]
        self.assertEqual(texts, [('cli', 'Use the existing parser.', 24), ('ui', 'Keep the API.', 13)])
        log = self.log()
        self.assertEqual([(e['action'], e['actor'], e['task'], e['value'], e['steer_chars'], e['waiting']) for e in log],
                         [('steer', 'cli', 'a', 'queued', 24, 1), ('steer', 'ui', 'a', 'queued', 13, 2)])
        self.assertNotIn('parser', (self.plan / 'control.log').read_text())
        self.assertEqual(log[0]['sha256'], self.entries()[0]['sha256'])

    def test_bad_messages_and_tasks_are_refused_with_nothing_written(self):
        for args, code in ((('a', '   '), 2), (('a', 'x' * 8001), 2), (('a',), 2), (('a', 'x', '--file', '-'), 2),
                           (('zz', 'x'), 2), (('../a', 'x'), 2)):
            self.assertEqual(self.control('steer', self.plan, *args, stdin='y').returncode, code, args)
        self.assertEqual(self.control('steer', self.plan, 'a', '--file', '-', stdin='a\0b').returncode, 2)
        (self.plan / 'tasks' / 'b' / 'merged').write_text('')
        r = self.control('steer', self.plan, 'b', 'too late')
        self.assertEqual(r.returncode, 3)
        self.assertIn('MERGED', r.stderr)
        self.assertEqual((self.entries(), self.entries(task='b'), self.log()), ([], [], []))

    def test_the_queue_of_one_task_is_capped(self):
        for n in range(20):
            self.ok('steer', self.plan, 'a', f'message {n}')
        r = self.control('steer', self.plan, 'a', 'one more')
        self.assertEqual(r.returncode, 3)
        self.assertIn('20 messages waiting', r.stderr)

    def test_an_attempt_takes_the_waiting_messages_and_a_redispatch_keeps_them(self):
        self.assertEqual(self.ok('_steer-take', self.plan, 'a', '1').stdout.strip(), '0')   # nothing waiting
        self.assertEqual(self.log(), [])
        self.ok('steer', self.plan, 'a', 'first')
        self.ok('steer', self.plan, 'a', 'second')
        self.assertEqual(self.ok('_steer-take', self.plan, 'a', '2').stdout.strip(), '2')
        self.assertFalse((self.plan / 'tasks/a/steer.jsonl').exists())
        self.assertEqual([e['text'] for e in self.entries('steered.2.jsonl')], ['first', 'second'])
        self.ok('steer', self.plan, 'a', 'third')
        self.ok('_steer-take', self.plan, 'a', '2')                                          # the same attempt again
        self.assertEqual([e['text'] for e in self.entries('steered.2.jsonl')], ['first', 'second', 'third'])
        delivered = [e for e in self.log() if e['action'] == 'steer-delivered']
        self.assertEqual([(e['actor'], e['value'], e['attempt']) for e in delivered], [('scheduler', '2', 2),
                                                                                       ('scheduler', '1', 2)])
        self.assertEqual(self.control('_steer-take', self.plan, 'a', 'x').returncode, 2)


class SteerDatasetTests(unittest.TestCase):
    def test_the_attempt_row_lists_the_messages_it_was_given(self):
        from forge_dataset import harvest
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            self.assertEqual(harvest.steered_of(tdir, 1), ([], None))
            (tdir / 'steered.2.jsonl').write_text(
                json.dumps(dict(at=5.0, actor='ui', text='Use X.', sha256='ab', chars=6)) + '\nnot json\n'
                + json.dumps(dict(at=6.0, actor='cli', text='Keep Y.', sha256='cd', chars=7)) + '\n')
            found, text = harvest.steered_of(tdir, 2)
            self.assertEqual(found, [dict(actor='ui', at=5.0, sha256='ab', chars=6, mode='queued'),
                                     dict(actor='cli', at=6.0, sha256='cd', chars=7, mode='queued')])
            self.assertEqual(text, 'Use X.\n\n---\n\nKeep Y.')
            self.assertEqual(harvest.steered_of(tdir, None), ([], None))

    def test_runs_leave_out_a_task_from_its_first_steered_attempt_unless_asked(self):
        from forge_dataset import export
        def ev(kind, task, attempt, **d):
            return dict(kind=kind, src='live', repo='r', run='run1', run_id='i', mode='parallel', task=task,
                        attempt=attempt, ts=attempt, d=d, prov={})

        class FakeStore:
            events = [ev('attempt', 'a', 1, status='FAIL'), ev('attempt', 'a', 2, status='FAIL', steered=[{'actor': 'ui'}]),
                      ev('attempt', 'a', 3, status='PASS', reviewed_commit='c3'),
                      ev('attempt', 'b', 1, status='PASS', reviewed_commit='c1'),
                      ev('dispatch', 'a', 1, role='dwarf'), ev('dispatch', 'a', 2, role='dwarf'),
                      ev('dispatch', 'a', 3, role='qa'), ev('dispatch', 'b', 1, role='dwarf')]
            repo_paths = {}

            def of(self, kind):
                return [e for e in self.events if e['kind'] == kind]

        runs = export.Runs(FakeStore())
        self.assertEqual(runs.steered, {('run1', 'a'): 2})
        self.assertEqual([e['attempt'] for e in runs.attempts[('run1', 'a')]], [1])
        self.assertEqual([e['attempt'] for e in runs.dispatches[('run1', 'a', 'dwarf')]], [1])
        self.assertEqual(runs.dispatches[('run1', 'a', 'qa')], [])
        self.assertNotIn(('run1', 'a', 3), runs.reviewed)
        self.assertIn(('run1', 'b', 1), runs.reviewed)
        kept = export.Runs(FakeStore(), include_steered=True)
        self.assertEqual([e['attempt'] for e in kept.attempts[('run1', 'a')]], [1, 2, 3])
        self.assertEqual(kept.steered, {('run1', 'a'): 2})
        self.assertIn(('run1', 'a', 3), kept.reviewed)


class SteerRunTests(unittest.TestCase):
    """The fake CLI: a message queued for a task reaches the prompts of its next implementer attempt only."""

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        (self.bin / 'diff').symlink_to(shutil.which('diff'))

    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario

    def plan_of(self):
        p = self.root / 'plan'
        (p / 'tasks' / 'a').mkdir(parents=True)
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. TASK_a\n')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def steer(self, p, text):
        r = subprocess.run([sys.executable, str(CONTROL), 'steer', str(p), 'a', text], capture_output=True,
                           text=True, env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)

    def read(self, p, name):
        return (p / 'tasks' / 'a' / name).read_text()

    def test_a_message_queued_before_the_run_reaches_both_prompts_of_attempt_one(self):
        p = self.plan_of()
        self.steer(p, 'STEER_ONE: keep the old function name.')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.read(p, 'status').strip(), 'PASS', r.stdout)
        self.assertIn('attempt 1 gets 1 message(s) from the user', r.stdout)
        dwarf = self.read(p, 'dwarf.input')
        self.assertIn('## Messages from the user for this attempt', dwarf)
        self.assertIn('STEER_ONE: keep the old function name.', dwarf)
        qa = self.read(p, 'qa.input')
        self.assertIn('## Messages the user sent the implementer for this attempt', qa)
        self.assertIn('STEER_ONE', qa)
        self.assertFalse((p / 'tasks/a/steer.jsonl').exists())
        self.assertTrue((p / 'tasks/a/steered.1.jsonl').exists())
        actions = [json.loads(line)['action'] for line in (p / 'control.log').read_text().splitlines()]
        self.assertEqual(actions, ['steer', 'steer-delivered'])

    def test_a_message_queued_after_a_failure_reaches_the_retry_only(self):
        p = self.plan_of()
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 wrong'},
                       'a.dwarf.2': {'content': 'second try'}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.read(p, 'status').strip(), 'FAIL')
        self.assertNotIn('Messages from the user', self.read(p, 'dwarf.input'))
        self.steer(p, 'STEER_TWO: the test fixture is wrong, not the code.')
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(self.read(p, 'status').strip(), 'PASS', r.stdout)
        dwarf = self.read(p, 'dwarf.input')
        self.assertIn('STEER_TWO', dwarf)
        self.assertIn('a.txt:1 wrong', dwarf)                      # beside the review's findings
        self.assertFalse((p / 'tasks/a/steered.1.jsonl').exists())
        self.assertTrue((p / 'tasks/a/steered.2.jsonl').exists())


if __name__ == '__main__':
    unittest.main()
