"""`forge control` and a split task (#153): `retry --decide split --subtasks FILE` on a live run and
with no run going, and `show` with the tree of nodes.

The scheduler cases use the stand-in worker from test_control_requests.py; the cases with no run
live drive the real scripts with the fake agent CLI, as test_decompose_run.py does. Fixtures are
borrowed, not inherited.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import subprocess
import sys
from pathlib import Path

import test_control_requests as control_fixtures
import test_decompose_run as split_fixtures

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / 'scripts/forge-control.py'
sys.path.insert(0, str(ROOT / 'scripts'))

child = split_fixtures.child
block = split_fixtures.block


class LiveSplitTests(control_fixtures.Base):
    """A scheduler is live (or stands in): the request names the stored, checked subtasks."""

    def failed(self, name='x'):
        self.task(name, status='FAIL')
        self.rows[-1] = f'{name}\t-\tlow\ta.txt,b.txt\tsol\topus\tThe whole of X'
        self.write_tasks()

    def subtasks(self, *tasks, name='subtasks.md'):
        path = self.root / name
        path.write_text(block(*(tasks or (child('a'), child('b')))))
        return path

    def split(self, *args, task='x', subtasks=None, decide='split'):
        return self.control('retry', self.plan, task, '--decide', decide, '--subtasks', subtasks or self.subtasks(),
                            '--reason', 'two halves', '--approved', *args)

    def stored(self, task='x'):
        return sorted(p.name for p in (self.plan / 'tasks' / task).glob('split.*'))

    def test_the_request_carries_the_decision_and_names_the_stored_subtasks(self):
        self.failed()
        self.live()
        result = self.split()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        [name] = self.requests()
        request = json.loads((self.plan / 'control.requests' / name).read_text())
        self.assertEqual((request['decide'], request['reason'], request['approved']), ('split', 'two halves', True))
        self.assertEqual(self.stored(), [request['subtasks']])
        proposal = json.loads((self.plan / 'tasks/x' / request['subtasks']).read_text())
        self.assertEqual([t['id'] for t in proposal['tasks']], ['a', 'b'])
        # Nothing of the task changed yet: the scheduler makes the split.
        self.assertEqual(self.status('x'), 'FAIL')
        self.assertFalse((self.plan / 'tasks/x/sub').exists())

    def test_what_control_refuses_before_it_queues_anything(self):
        self.failed()
        self.task('m', status='PASS')
        (self.plan / 'tasks/m/merged').write_text('')
        self.task('r', status='RUNNING')
        self.live()
        (self.plan / 'decomposition.json').write_text(json.dumps({'version': 1, 'nodes': {}, 'limits': {
            'depth': 2, 'children': 2, 'nodes': 24}}))
        cases = (
            ('no subtasks', ('x', '--decide', 'split', '--reason', 'r', '--approved'), 2, '--subtasks FILE'),
            ('no reason', ('x', '--decide', 'split', '--subtasks', self.subtasks(), '--approved'), 2, '--reason'),
            ('no approval', ('x', '--decide', 'split', '--subtasks', self.subtasks(), '--reason', 'r'), 2, '--approved'),
            ('a model', ('x', '--decide', 'split', '--subtasks', self.subtasks(), '--dwarf', 'sol', '--reason', 'r',
                         '--approved'), 2, 'does not take --dwarf'),
            ('subtasks without a split', ('x', '--subtasks', self.subtasks()), 2, 'goes with --decide split'),
            ('a missing file', ('x', '--decide', 'split', '--subtasks', self.root / 'none.md', '--reason', 'r',
                                '--approved'), 2, 'not a readable file'),
            ('a merged task', ('m', '--decide', 'split', '--subtasks', self.subtasks(), '--reason', 'r', '--approved'),
             3, 'MERGED'),
            ('a running task', ('r', '--decide', 'split', '--subtasks', self.subtasks(), '--reason', 'r', '--approved'),
             3, 'RUNNING'),
            ('a file outside the task', ('x', '--decide', 'split', '--subtasks',
                                         self.subtasks(child('a'), child('b', files=['other.txt']), name='out.md'),
                                         '--reason', 'r', '--approved'), 3, "outside its parent's files"),
            ('one task', ('x', '--decide', 'split', '--subtasks',
                          self.subtasks(child('a', files=['a.txt', 'b.txt']), name='one.md'), '--reason', 'r', '--approved'),
             3, 'at least two'),
            ('past a limit', ('x', '--decide', 'split', '--subtasks',
                              self.subtasks(child('a'), child('b'), child('c', files=['b.txt']), name='three.md'),
                              '--reason', 'r', '--approved'), 3, 'the limit is 2 per split'),
        )
        for what, args, code, said in cases:
            with self.subTest(what):
                result = self.control('retry', self.plan, *args)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertIn(said, result.stderr)
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.stored(), [])
        self.assertEqual(self.status('x'), 'FAIL')

    def run_beside(self, during):
        """A live scheduler with one other task held open while `during` runs."""
        self.task('held')
        (self.plan / 'hooks/held.1').write_text('waitfor "$plan/release"\n')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'tasks/held/calls').exists(), 'the held task to start')
        during()
        (self.plan / 'release').write_text('')
        proc.communicate(timeout=60)

    def test_the_scheduler_prepares_the_split_from_the_stored_subtasks(self):
        self.failed()
        seen = {}

        def during():
            result = self.split(decide='split=fresh')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.wait_for(lambda: self.outcomes(), 'the outcome line')
            seen['stored'] = self.stored()

        self.run_beside(during)
        [line] = self.outcomes()
        self.assertEqual((line['task'], line['outcome']), ('x', 'applied'), line)
        self.assertIn('x prepared --decide split=fresh --reason two halves --approved', self.events())
        # The stand-in preparation leaves the proposal where the real one reads it; the request's copy is gone.
        self.assertEqual(seen['stored'], ['split.proposal.json'])
        self.assertEqual(self.requests(), [])

    def test_a_refused_split_leaves_no_subtasks_behind(self):
        self.failed()
        (self.plan / 'tasks/x/escalation.stop').write_text('3\tsol\n')      # the stand-in preparation exits 3 on it

        def during():
            self.assertEqual(self.split().returncode, 0)
            self.wait_for(lambda: self.outcomes(), 'the outcome line')

        self.run_beside(during)
        [line] = self.outcomes()
        self.assertEqual((line['task'], line['outcome']), ('x', 'refused'))
        self.assertIn('was not split', line['reason'])
        self.assertEqual(self.stored(), ['split.suggested'])
        self.assertEqual(self.status('x'), 'FAIL')

    def test_a_request_written_by_hand_without_subtasks_is_refused(self):
        self.failed()

        def during():
            (self.plan / 'control.requests').mkdir(exist_ok=True)
            for n, extra in enumerate((dict(), dict(subtasks='../../tasks.tsv'), dict(subtasks='split.request.1-1.json'))):
                (self.plan / 'control.requests' / ('%d-1.json' % (n + 1))).write_text(json.dumps(dict(
                    verb='retry', task='x', dwarf=None, at=0, actor='cli', decide='split', reason='r', approved=True, **extra)))
            self.wait_for(lambda: len(self.outcomes()) == 3, 'three outcome lines')

        self.run_beside(during)
        self.assertEqual([line['outcome'] for line in self.outcomes()], ['refused'] * 3)
        self.assertTrue((self.plan / 'tasks.tsv').exists())
        self.assertNotIn('x prepared', ' '.join(self.events()))


class NoRunTests(split_fixtures.SplitCase):
    def control(self, *args):
        return subprocess.run([sys.executable, str(CONTROL), *map(str, args)], capture_output=True, text=True,
                              env=self.env, cwd=self.repo, timeout=300)

    def test_with_no_run_going_the_split_is_made_now_and_the_next_run_runs_it(self):
        p, _ = self.stopped()
        before = self.calls()
        result = self.control('retry', p, 'd', '--decide', 'split', '--subtasks', self.subtasks(), '--reason',
                              split_fixtures.REASON, '--approved', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {'queued': False, 'prepared': True, 'split': True})
        self.assertEqual(self.calls(), before)
        self.assertEqual(self.children(p / 'tasks/d/sub'), ['a', 'b'])
        self.assertTrue((p / 'tasks/d/retry.prepared').exists())
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p, 'd'), 'MERGED')
        self.assertEqual(self.status(p, 'c'), 'MERGED')

    def test_show_has_the_limits_and_every_node_of_the_tree(self):
        p, _ = self.stopped()
        self.assertEqual(self.split(p, '--no-dispatch').returncode, 0)
        shown = json.loads(self.control('show', p, '--json').stdout)
        tree = shown['decomposition']
        self.assertEqual(tree['limits'], {'depth': 2, 'children': 6, 'nodes': 24, 'frozen': True})
        self.assertEqual([(n['id'], n['level'], n['decision']) for n in tree['nodes']],
                         [('d', 0, 'split'), ('d/a', 1, 'atomic'), ('d/b', 1, 'atomic'), ('c', 0, 'atomic')])
        self.assertEqual(tree['nodes'][1]['plan'], str(p / 'tasks/d/sub'))
        text = self.control('show', p).stdout
        self.assertIn('decomposition limits: depth 2', text)
        self.assertIn('[split into 2]', text)
        self.assertIn(str(p / 'tasks/d/sub'), text)
        # A subtask is a task of its own plan: its controls are that plan's.
        sub = json.loads(self.control('show', p / 'tasks/d/sub', '--json').stdout)
        self.assertEqual(sorted(sub['tasks']), ['a', 'b'])
        result = self.control('pin', p / 'tasks/d/sub', 'a', 'opus')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_plan_with_no_split_shows_only_its_limits(self):
        p = self.planned()
        text = self.control('show', p).stdout
        self.assertIn('decomposition limits: depth 2', text)
        self.assertNotIn('subtasks (', text)


if __name__ == '__main__':
    import unittest
    unittest.main()
