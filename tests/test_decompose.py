"""Recursive decomposition (#153): the node model, the limits, and a task's split into a sub-plan.

Pure Python on scripts/forge_decompose.py: no git, no dispatch. The runs of a split task are in
test_decompose_run.py.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decompose as fd  # noqa: E402

HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'


def child(cid, files, deps=(), difficulty='low', **extra):
    return dict(dict(id=cid, deps=list(deps), difficulty=difficulty, files=list(files), title='Do ' + cid,
                     approach='', prompt='Do %s.\n\n## Acceptance criteria\n\n- it works\n' % cid), **extra)


class PlanCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = Path(self.tmp.name) / 'forge-big'
        self.make(self.plan, [('a', '-', 'high', 'src,docs/a.md', 'sol', 'opus', 'Build a'),
                              ('b', 'a', 'low', 'lib/b.py', 'luna', 'opus', 'Build b')])
        (self.plan / 'repo').write_text('/nowhere\n')
        (self.plan / 'dwarf-routing.json').write_text(json.dumps({'low': ['luna', 'terra'], 'high': ['sol']}))
        (self.plan / 'qa-routing.json').write_text(json.dumps({'any': ['opus']}))

    def make(self, plan, rows):
        (plan / 'tasks').mkdir(parents=True)
        (plan / 'tasks.tsv').write_text(HEADER + ''.join('\t'.join(r) + '\n' for r in rows))
        for row in rows:
            (plan / 'tasks' / row[0]).mkdir()
            (plan / 'tasks' / row[0] / 'prompt.md').write_text('Prompt of %s. PARENT_SENTINEL\n' % row[0])

    def kids(self, n=2):
        return [child('c%d' % i, ['src/c%d.py' % i]) for i in range(1, n + 1)]


class NodeModelTests(PlanCase):
    def test_a_split_records_the_node_and_writes_an_ordinary_plan(self):
        sub = fd.split(self.plan, 'a', dict(tasks=[child('one', ['src/one.py'], difficulty='low'),
                                                   child('two', ['src/two.py', 'docs/a.md'], deps=['one'], difficulty='high'),
                                                   child('three', ['src/sub'], difficulty='low')]),
                       reason='three separable parts', by='user')
        self.assertEqual(sub, self.plan / 'tasks' / 'a' / 'sub')
        rows = fd.read_rows(sub)
        self.assertEqual([r['id'] for r in rows], ['one', 'two', 'three'])
        # Routed with the plan's difficulty pools, each pool in turn; the reviewer from the plain pool.
        self.assertEqual([r['dwarf'] for r in rows], ['luna', 'sol', 'terra'])
        self.assertEqual({r['qa'] for r in rows}, {'opus'})
        self.assertEqual(rows[1]['deps'], ['one'])
        self.assertEqual((sub / 'run_id').read_text(), 'big--a\n')
        self.assertEqual((sub / 'repo').read_text(), '/nowhere\n')
        prompt = (sub / 'tasks' / 'one' / 'prompt.md').read_text()
        self.assertTrue(prompt.startswith('Do one.'))
        self.assertIn('PARENT_SENTINEL', prompt)
        node = fd.load(self.plan)['nodes']['a']
        self.assertEqual((node['decision'], node['by'], node['reason']), ('split', 'user', 'three separable parts'))
        self.assertEqual((node['id'], node['parent'], node['goal'], node['difficulty']), ('a', None, 'Build a', 'high'))
        self.assertEqual(node['files'], ['src', 'docs/a.md'])
        self.assertEqual(node['limits_used']['depth'], 0)
        self.assertEqual(node['limits_used']['children'], 3)
        self.assertEqual(node['subtasks'], ['one', 'two', 'three'])
        # Its subtasks are nodes of their own plan, each atomic until somebody decides otherwise.
        kids = fd.load(sub)['nodes']
        self.assertEqual({k: (v['decision'], v['by'], v['parent'], v['id']) for k, v in kids.items()},
                         {c: ('atomic', 'default', 'a', 'a/' + c) for c in ('one', 'two', 'three')})
        self.assertEqual(kids['two']['deps'], ['one'])

    def test_the_tree_lists_every_level_in_order(self):
        fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='r')
        sub = fd.sub_dir(self.plan, 'a')
        fd.split(sub, 'c1', dict(tasks=[child('x', ['src/c1.py']), child('y', ['src/c1.py'], deps=['x'])]), reason='deeper')
        nodes = fd.tree(self.plan)
        self.assertEqual([(n['id'], n['level'], n['parent'], n['decision'], n['children']) for n in nodes],
                         [('a', 0, None, 'split', 2), ('a/c1', 1, 'a', 'split', 2), ('a/c1/x', 2, 'a/c1', 'atomic', 0),
                          ('a/c1/y', 2, 'a/c1', 'atomic', 0), ('a/c2', 1, 'a', 'atomic', 0), ('b', 0, None, 'atomic', 0)])
        self.assertEqual(nodes[2]['run'], 'big--a--c1')
        self.assertEqual(fd.root_of(fd.sub_dir(sub, 'c1')), self.plan.resolve())
        self.assertEqual(fd.count_subnodes(self.plan), 4)
        self.assertEqual(fd.tree(sub)[0]['id'], 'a/c1')

    def test_a_decision_somebody_made_is_not_replaced_by_a_default(self):
        fd.record(self.plan, 'b', 'atomic', 'one coherent change', 'planner')
        fd.record(self.plan, 'b', 'atomic', 'no split was proposed', 'default')
        self.assertEqual(fd.load(self.plan)['nodes']['b']['reason'], 'one coherent change')
        self.assertEqual(fd.settle(self.plan), [])
        nodes = fd.load(self.plan)['nodes']
        self.assertEqual((nodes['b']['by'], nodes['b']['reason']), ('planner', 'one coherent change'))
        self.assertEqual((nodes['a']['decision'], nodes['a']['by']), ('atomic', 'default'))

    def test_a_planners_reason_to_keep_a_task_whole_is_recorded(self):
        (self.plan / 'tasks' / 'b' / fd.REASON).write_text('one file, one function\n')
        fd.settle(self.plan)
        node = fd.load(self.plan)['nodes']['b']
        self.assertEqual((node['decision'], node['by'], node['reason']), ('atomic', 'planner', 'one file, one function'))

    def test_a_proposal_of_one_subtask_keeps_the_node_whole_with_its_reason(self):
        with self.assertRaises(fd.Atomic):
            fd.split(self.plan, 'a', dict(tasks=self.kids(1), reason='it is one piece'), by='planner')
        node = fd.load(self.plan)['nodes']['a']
        self.assertEqual((node['decision'], node['by'], node['reason']), ('atomic', 'planner', 'it is one piece'))
        self.assertFalse(fd.is_split(self.plan, 'a'))


class LimitTests(PlanCase):
    def test_defaults_then_set_then_frozen_by_the_first_run(self):
        self.assertEqual(fd.limits(self.plan), dict(depth=2, children=6, nodes=24))
        self.assertEqual(fd.set_limits(self.plan, depth=1, nodes=5), dict(depth=1, children=6, nodes=5))
        fd.freeze(self.plan)
        self.assertTrue(fd.is_frozen(self.plan))
        with self.assertRaises(fd.Invalid) as refused:
            fd.set_limits(self.plan, depth=3)
        self.assertIn('frozen', str(refused.exception))
        self.assertEqual(fd.set_limits(self.plan, depth=1), dict(depth=1, children=6, nodes=5))   # the same value is no change
        self.assertEqual(fd.limits(self.plan)['depth'], 1)

    def test_a_value_out_of_range_is_refused(self):
        for values in (dict(depth=7), dict(children=1), dict(nodes=-1), dict(children=21)):
            with self.subTest(values), self.assertRaises(fd.Invalid):
                fd.set_limits(self.plan, **values)
        self.assertFalse((self.plan / fd.FILE).exists())

    def test_at_the_depth_limit_the_node_is_bounded_and_stays_one_task(self):
        fd.set_limits(self.plan, depth=1)
        fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='r')
        sub = fd.sub_dir(self.plan, 'a')
        with self.assertRaises(fd.Bounded) as limit:
            fd.split(sub, 'c1', dict(tasks=[child('x', ['src/c1.py']), child('y', ['src/c1.py'], deps=['x'])]), reason='more')
        self.assertEqual(limit.exception.limit, 'depth')
        node = fd.load(sub)['nodes']['c1']
        self.assertEqual((node['decision'], node['limits_used']['limit'], node['limits_used']['depth']), ('bounded', 'depth', 1))
        self.assertIn('more', node['reason'])
        self.assertFalse(fd.is_split(sub, 'c1'))
        fd.settle(self.plan)                         # a later default never turns bounded into atomic
        self.assertEqual(fd.load(sub)['nodes']['c1']['decision'], 'bounded')

    def test_the_children_and_the_tree_limits(self):
        fd.set_limits(self.plan, children=2, nodes=3)
        with self.assertRaises(fd.Bounded) as limit:
            fd.split(self.plan, 'a', dict(tasks=self.kids(3)), reason='r')
        self.assertEqual(limit.exception.limit, 'children')
        fd.split(self.plan, 'a', dict(tasks=self.kids(2)), reason='r')
        with self.assertRaises(fd.Bounded) as limit:
            fd.split(self.plan, 'b', dict(tasks=[child('p', ['lib/b.py']), child('q', ['lib/b.py'], deps=['p'])]), reason='r')
        self.assertEqual(limit.exception.limit, 'nodes')
        self.assertEqual(fd.load(self.plan)['nodes']['b']['decision'], 'bounded')

    def test_depth_zero_turns_splitting_off(self):
        fd.set_limits(self.plan, depth=0)
        with self.assertRaises(fd.Bounded):
            fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='r')
        self.assertIn('no task is split', fd.describe(fd.limits(self.plan)))

    def test_a_sub_plan_has_no_limits_of_its_own(self):
        fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='r')
        sub = fd.sub_dir(self.plan, 'a')
        self.assertEqual(fd.limits(sub), fd.limits(self.plan))
        with self.assertRaises(fd.Invalid):
            fd.set_limits(sub, depth=4)


class ValidationTests(PlanCase):
    def refused(self, kids, text, task='a'):
        with self.assertRaises(fd.Invalid) as error:
            fd.split(self.plan, task, dict(tasks=kids), reason='r')
        self.assertIn(text, str(error.exception))
        self.assertFalse(fd.is_split(self.plan, task))
        self.assertNotIn(task, fd.load(self.plan)['nodes'])     # a wrong proposal records nothing
        self.assertEqual([p.name for p in (self.plan / 'tasks' / task).iterdir()], ['prompt.md'])

    def test_a_subtask_owns_paths_inside_its_parents(self):
        self.refused([child('x', ['src/x.py']), child('y', ['lib/y.py'])], 'outside the files its parent declares')
        self.refused([child('x', ['src/x.py']), child('y', ['docs'])], 'outside')          # the parent has docs/a.md only
        self.refused([child('x', ['src/x.py']), child('y', ['srcs/y.py'])], 'outside')     # a prefix is not a directory
        self.refused([child('x', ['src/x.py']), child('y', ['src/../lib/y.py'])], '..')
        self.refused([child('x', ['src/x.py']), child('y', ['/etc/passwd'])], 'absolute')
        self.assertTrue(fd.inside('anything/at/all', ['.']))
        self.assertTrue(fd.inside('./src/a.py', ['src/']))
        self.assertTrue(fd.inside('docs/a.md', ['src', 'docs/a.md']))

    def test_deps_name_siblings_and_form_no_cycle(self):
        self.refused([child('x', ['src/x.py'], deps=['b']), child('y', ['src/y.py'])], 'unknown dep')
        self.refused([child('x', ['src/x.py'], deps=['y']), child('y', ['src/y.py'], deps=['x'])], 'cycle')
        self.refused([child('x', ['src/x.py']), child('x', ['src/y.py'])], 'duplicate id')
        self.refused([child('X', ['src/x.py']), child('y', ['src/y.py'])], 'does not match')
        self.refused([child('x', []), child('y', ['src/y.py'])], 'files')
        self.refused([child('x', ['src/x.py'], difficulty='huge'), child('y', ['src/y.py'])], 'difficulty')
        self.refused('nope', 'not a list')

    def test_only_a_task_that_can_still_change_is_split(self):
        (self.plan / 'tasks' / 'a' / 'merged').write_text('')
        with self.assertRaises(fd.Invalid) as error:
            fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='r')
        self.assertIn('MERGED', str(error.exception))
        (self.plan / 'tasks' / 'a' / 'merged').unlink()
        (self.plan / 'tasks' / 'a' / 'status').write_text('RUNNING\n')
        with self.assertRaises(fd.Invalid):
            fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='r')
        with self.assertRaises(fd.Invalid) as error:
            fd.split(self.plan, 'nope', dict(tasks=self.kids()), reason='r')
        self.assertIn('no task', str(error.exception))

    def test_a_script_task_is_not_split(self):
        text = (self.plan / 'tasks.tsv').read_text().replace('\tluna\t', '\tscript\t')
        (self.plan / 'tasks.tsv').write_text(text)
        with self.assertRaises(fd.Invalid) as error:
            fd.split(self.plan, 'b', dict(tasks=[child('p', ['lib/b.py']), child('q', ['lib/b.py'], deps=['p'])]), reason='r')
        self.assertIn('script task', str(error.exception))


class ResumeTests(PlanCase):
    def test_a_second_split_never_duplicates_the_children(self):
        sub = fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='first')
        (sub / 'tasks' / 'c1' / 'status').write_text('FAIL\n')
        before = sorted(str(p.relative_to(sub)) for p in sub.rglob('*'))
        with self.assertRaises(fd.Invalid) as error:
            fd.split(self.plan, 'a', dict(tasks=self.kids(3)), reason='second')
        self.assertIn('already split', str(error.exception))
        self.assertEqual(sorted(str(p.relative_to(sub)) for p in sub.rglob('*')), before)
        self.assertEqual(fd.load(self.plan)['nodes']['a']['reason'], 'first')
        self.assertEqual(fd.waiting(self.plan, 'a'), ['c1 FAIL', 'c2 PENDING'])

    def test_open_until_the_sub_plan_has_merged_into_the_task(self):
        self.assertFalse(fd.is_open(self.plan, 'a'))
        fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='r')
        self.assertTrue(fd.is_open(self.plan, 'a'))
        (self.plan / 'tasks' / 'a' / fd.MERGED).write_text('abc\n')
        self.assertFalse(fd.is_open(self.plan, 'a'))
        self.assertTrue(fd.is_split(self.plan, 'a'))

    def test_a_failed_write_leaves_no_half_sub_plan(self):
        (self.plan / 'repo').unlink()
        with self.assertRaises(fd.Invalid):
            fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='r')
        self.assertEqual([p.name for p in (self.plan / 'tasks' / 'a').iterdir()], ['prompt.md'])

    def test_a_stored_proposal_is_applied_once_through_every_level(self):
        nested = child('c1', ['src/c1.py'], split=dict(reason='two halves', tasks=[
            child('x', ['src/c1.py']), child('y', ['src/c1.py'], deps=['x'])]))
        (self.plan / 'tasks' / 'a' / fd.PROPOSAL).write_text(json.dumps(dict(reason='parts', tasks=[nested, child('c2', ['src/c2.py'])])))
        lines = fd.settle(self.plan, by='planner')
        self.assertEqual(lines, ['a: split into 2 subtasks'])
        self.assertFalse((self.plan / 'tasks' / 'a' / fd.PROPOSAL).exists())
        self.assertEqual([n['id'] for n in fd.tree(self.plan)], ['a', 'a/c1', 'a/c1/x', 'a/c1/y', 'a/c2', 'b'])
        self.assertEqual(fd.load(fd.sub_dir(self.plan, 'a'))['nodes']['c1']['reason'], 'two halves')
        self.assertEqual(fd.settle(self.plan, by='planner'), [])


class SlotTests(unittest.TestCase):
    def test_one_bound_for_every_scheduler_of_the_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = fd.Slots(tmp), fd.Slots(tmp)     # two schedulers of one tree
            a, b = first.take(2), second.take(2)
            self.assertTrue(a and b)
            self.assertIsNone(first.take(2))
            self.assertIsNone(second.take(2))
            c = second.take(3)                                # a raised bound opens a slot at once
            self.assertTrue(c)
            fd.Slots.give(a)
            again = second.take(2)
            self.assertTrue(again)
            self.assertIsNone(first.take(2))
            for held in (b, c, again):
                fd.Slots.give(held)

    def test_slots_that_cannot_be_used_bound_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / fd.SLOTS
            blocker.write_text('not a directory')
            slots = fd.Slots(tmp)
            self.assertIs(slots.take(1), False)
            self.assertIs(slots.take(1), False)


class CommandLineTests(PlanCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge_decompose.py'), *map(str, args)],
                              text=True, capture_output=True, env=dict(os.environ))

    def test_exit_codes_say_what_happened(self):
        proposal = self.plan / 'proposal.json'
        proposal.write_text(json.dumps(dict(tasks=self.kids(1), reason='one piece')))
        self.assertEqual(self.run_cli('split', self.plan, 'a', '--proposal', proposal, '--by', 'planner').returncode, 5)
        proposal.write_text(json.dumps(dict(tasks=[child('x', ['elsewhere/x.py']), child('y', ['src/y.py'])])))
        refused = self.run_cli('split', self.plan, 'a', '--proposal', proposal, '--reason', 'r')
        self.assertEqual(refused.returncode, 3)
        self.assertIn('outside the files its parent declares', refused.stderr)
        proposal.write_text(json.dumps(dict(tasks=self.kids(7))))
        bounded = self.run_cli('split', self.plan, 'a', '--proposal', proposal, '--reason', 'r')
        self.assertEqual(bounded.returncode, 4)
        self.assertIn('limit is 6 per split', bounded.stderr)
        proposal.write_text(json.dumps(dict(tasks=self.kids(2))))
        self.assertEqual(self.run_cli('split', self.plan, 'a', '--proposal', proposal, '--check').returncode, 0)
        self.assertEqual(self.run_cli('state', self.plan, 'a').stdout, 'none\n')
        made = self.run_cli('split', self.plan, 'a', '--proposal', proposal, '--reason', 'r')
        self.assertEqual((made.returncode, made.stdout.strip()), (0, str(self.plan / 'tasks' / 'a' / 'sub')))
        self.assertEqual(self.run_cli('state', self.plan, 'a').stdout, 'open\n')
        self.assertEqual(self.run_cli('root', self.plan / 'tasks' / 'a' / 'sub').stdout.strip(), str(self.plan.resolve()))

    def test_limits_are_printed_as_the_table_shows_them(self):
        shown = self.run_cli('limits', self.plan, '--depth', '1', '--children', '4')
        self.assertEqual(shown.stdout, 'decomposition limits: depth 1, at most 4 subtasks per split, 24 subtasks in the whole run\n')
        self.assertEqual(self.run_cli('freeze', self.plan).returncode, 0)
        refused = self.run_cli('limits', self.plan, '--depth', '2')
        self.assertEqual(refused.returncode, 3)
        self.assertIn('frozen', refused.stderr)
        self.assertIn('(frozen)', self.run_cli('limits', self.plan).stdout)
        as_json = json.loads(self.run_cli('limits', self.plan, '--json').stdout)
        self.assertEqual(as_json, dict(depth=1, children=4, nodes=24, frozen=True, used=0))

    def test_the_tree_as_text_and_as_json(self):
        fd.split(self.plan, 'a', dict(tasks=self.kids()), reason='two parts')
        text = self.run_cli('tree', self.plan).stdout
        self.assertIn('[split into 2]', text)
        self.assertIn('split: two parts', text)
        self.assertIn('  c1', text)
        nodes = json.loads(self.run_cli('tree', self.plan, '--json').stdout)
        self.assertEqual([n['id'] for n in nodes], ['a', 'a/c1', 'a/c2', 'b'])


if __name__ == '__main__':
    unittest.main()
