"""Recursive decomposition in forge-ui's reader (#153): a split task's subtasks are a plan of their own
under it, `<plan>/tasks/<id>/sub`. The run JSON carries the tree on the task, the sub-plan is a run
that is addressed by its key and never listed, and a plan with no decomposition record reads as before.

The plan dirs are made with forge's own module (scripts/forge_decompose.py), so what the reader is
given is what forge writes.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from forge_ui import worktrees
from forge_ui.reader import Catalog, detail, run_key, sub_plans, summarize

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decompose  # noqa: E402


def child(tid, files, deps=(), **more):
    return dict({'id': tid, 'deps': list(deps), 'difficulty': 'low', 'files': files, 'title': 'Part ' + tid,
                 'prompt': 'Do part %s.' % tid}, **more)


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.repo = self.base / 'repo'
        self.repo.mkdir()

    def plan(self, name='plan'):
        """d (owns src/ and docs/) and c, which depends on d. No decomposition record yet."""
        plan = self.base / name
        (plan / 'tasks').mkdir(parents=True)
        (plan / 'tasks.tsv').write_text('d\t-\thigh\tsrc/,docs/\tsol\topus\tThe whole of D\n'
                                        'c\td\tlow\tc.txt\tsol\topus\tC\n')
        for task in 'dc':
            (plan / 'tasks' / task).mkdir()
            (plan / 'tasks' / task / 'prompt.md').write_text('Do %s.\n' % task)
        (plan / 'run_id').write_text(name + '\n')
        (plan / 'repo').write_text(str(self.repo) + '\n')
        (plan / 'goal.txt').write_text('Build D and C\n')
        return plan

    def split(self, plan, nested=True):
        """d is split into a (split again into one and two) and b."""
        first = child('a', ['src/a/'])
        if nested:
            first['split'] = {'reason': 'two steps', 'tasks': [child('one', ['src/a/one.py']),
                                                              child('two', ['src/a/two.py'], deps=['one'])]}
        sub = forge_decompose.split(plan, 'd', {'reason': 'two halves', 'tasks': [first, child('b', ['docs/'])]},
                                    by='planner')
        forge_decompose.settle(plan, by='planner')
        return sub

    def status(self, plan, task, value):
        (plan / 'tasks' / task).mkdir(parents=True, exist_ok=True)
        (plan / 'tasks' / task / 'status').write_text(value + '\n')


class SummaryTests(Case):
    def test_a_plan_with_no_record_gains_no_key(self):
        plan = self.plan()
        summary = summarize(plan)
        self.assertNotIn('decomposition', summary)
        self.assertNotIn('parent', summary)
        data = detail(plan)
        self.assertNotIn('later', data)
        self.assertTrue(all('decomposition' not in task for task in data['tasks']))
        self.assertEqual(list(sub_plans(plan)), [])

    def test_a_plan_with_a_record_and_no_split_has_its_limits(self):
        plan = self.plan()
        forge_decompose.set_limits(plan, depth=3)
        forge_decompose.settle(plan)
        self.assertEqual(summarize(plan)['decomposition'],
                         {'limits': {'depth': 3, 'children': 6, 'nodes': 24, 'frozen': False}, 'split': 0, 'subtasks': 0,
                          'statuses': []})
        task = detail(plan)['tasks'][0]['decomposition']
        self.assertEqual((task['decision'], task['by'], task['sub'], task['nodes']), ('atomic', 'default', None, []))

    def test_a_split_run_counts_its_subtasks_and_lists_their_statuses(self):
        plan = self.plan()
        sub = self.split(plan)
        forge_decompose.freeze(plan)
        self.status(sub, 'b', 'FAIL')
        self.status(sub / 'tasks/a/sub', 'one', 'RUNNING')
        tree = summarize(plan)['decomposition']
        self.assertEqual((tree['split'], tree['subtasks'], tree['limits']['frozen']), (1, 4, True))
        self.assertEqual(tree['statuses'], [{'id': 'd/a', 'status': 'PENDING'}, {'id': 'd/a/one', 'status': 'RUNNING'},
                                            {'id': 'd/a/two', 'status': 'PENDING'}, {'id': 'd/b', 'status': 'FAIL'}])

    def test_work_in_a_sub_plan_moves_the_runs_updated_time(self):
        plan = self.plan()
        sub = self.split(plan, nested=False)
        before = summarize(plan)['updated']
        self.status(sub, 'a', 'RUNNING')
        later = before + 500
        os.utime(sub / 'tasks/a/status', (later, later))
        self.assertEqual(summarize(plan)['updated'], later)
        self.assertEqual(detail(plan)['updated'], later)

    def test_a_sub_plan_names_the_task_it_belongs_to_and_takes_the_runs_limits(self):
        plan = self.plan()
        forge_decompose.set_limits(plan, depth=4)
        sub = self.split(plan)
        summary = summarize(sub)
        self.assertEqual(summary['parent'], {'key': run_key(plan), 'task': 'd', 'run_id': 'plan', 'path': str(plan)})
        self.assertEqual(summary['run_id'], 'plan--d')
        self.assertEqual(summary['decomposition']['limits']['depth'], 4)
        inner = summarize(sub / 'tasks/a/sub')
        self.assertEqual((inner['parent']['key'], inner['parent']['task']), (run_key(sub), 'a'))
        self.assertEqual(inner['decomposition']['limits']['depth'], 4)


class DetailTests(Case):
    def test_the_split_task_carries_every_node_below_it_in_tree_order(self):
        plan = self.plan()
        sub = self.split(plan)
        self.status(sub, 'b', 'PASS')
        (sub / 'tasks/b/merged').write_text('')
        (sub / 'tasks/b/changes.diff').write_text('diff --git a/docs/x b/docs/x\n')
        data = detail(plan)
        d, c = data['tasks']
        tree = d['decomposition']
        self.assertEqual((tree['decision'], tree['by'], tree['reason']), ('split', 'planner', 'two halves'))
        self.assertEqual(tree['sub'], {'key': run_key(sub), 'run_id': 'plan--d', 'path': str(sub)})
        self.assertEqual((tree['merged'], tree['waiting'], tree['start'], tree['kept']), (False, None, 'keep', None))
        self.assertEqual([(n['id'], n['parent'], n['level'], n['task'], n['status'], n['decision']) for n in tree['nodes']],
                         [('d/a', 'd', 1, 'a', 'PENDING', 'split'), ('d/a/one', 'd/a', 2, 'one', 'PENDING', 'atomic'),
                          ('d/a/two', 'd/a', 2, 'two', 'PENDING', 'atomic'), ('d/b', 'd', 1, 'b', 'MERGED', 'atomic')])
        a, one, _two, b = tree['nodes']
        self.assertEqual((a['run'], one['run']), (run_key(sub), run_key(sub / 'tasks/a/sub')))
        self.assertEqual((a['reason'], a['by'], a['title'], a['model'], a['difficulty']),
                         ('two steps', 'planner', 'Part a', 'sol', 'low'))
        self.assertEqual((b['has_diff'], a['has_diff'], b['tokens'], b['attempts']), (True, False, None, 0))
        self.assertEqual(c['decomposition']['decision'], 'atomic')
        self.assertEqual(c['decomposition']['nodes'], [])

    def test_merged_waiting_and_a_fresh_start_are_read_from_the_task(self):
        plan = self.plan()
        sub = forge_decompose.split(plan, 'd', {'tasks': [child('a', ['src/']), child('b', ['docs/'])]},
                                    reason='stuck', by='user', start='fresh')
        (plan / 'tasks/d/sub.waiting').write_text('a FAIL\nb NEEDS_DECISION\n')
        (plan / 'tasks/d/sub.kept').write_text('abc123\n')
        tree = detail(plan)['tasks'][0]['decomposition']
        self.assertEqual((tree['merged'], tree['waiting'], tree['start'], tree['kept'], tree['by']),
                         (False, ['a FAIL', 'b NEEDS_DECISION'], 'fresh', 'abc123', 'user'))
        (plan / 'tasks/d/sub.merged').write_text('abc\n')
        self.assertTrue(detail(plan)['tasks'][0]['decomposition']['merged'])
        self.assertTrue(sub.is_dir())

    def test_a_node_reports_the_tokens_its_executions_spent(self):
        plan = self.plan()
        sub = self.split(plan, nested=False)
        import run_fixture as F
        F.execution(sub / 'tasks/a', 'dwarf-' + F.uid(1), 100, 160, tokens=(1000, 200, 50))
        F.execution(sub / 'tasks/a', 'qa-' + F.uid(2), 170, 200, tokens=(500, 0, 25))
        node = detail(plan)['tasks'][0]['decomposition']['nodes'][0]
        self.assertEqual(node['tokens'], {'input': 1500, 'cached': 200, 'output': 75})

    def test_a_bounded_task_and_one_kept_whole_keep_their_reason(self):
        plan = self.plan()
        forge_decompose.set_limits(plan, depth=0)
        with self.assertRaises(forge_decompose.Bounded):
            forge_decompose.split(plan, 'd', {'reason': 'two halves', 'tasks': [child('a', ['src/']), child('b', ['docs/'])]})
        with self.assertRaises(forge_decompose.Atomic):
            forge_decompose.split(plan, 'c', {'reason': 'one file', 'tasks': [child('a', ['c.txt'])]}, by='planner')
        d, c = detail(plan)['tasks']
        self.assertEqual((d['decomposition']['decision'], d['decomposition']['sub']), ('bounded', None))
        self.assertIn('depth', d['decomposition']['reason'])
        self.assertEqual((c['decomposition']['decision'], c['decomposition']['reason'], c['decomposition']['by']),
                         ('atomic', 'one file', 'planner'))

    def test_a_damaged_record_reads_as_what_is_on_disk(self):
        plan = self.plan()
        self.split(plan, nested=False)
        (plan / 'decomposition.json').write_text('{not json')
        data = detail(plan)
        tree = data['tasks'][0]['decomposition']
        self.assertEqual((tree['decision'], tree['by'], len(tree['nodes'])), ('split', 'default', 2))
        self.assertTrue(any('decomposition.json' in warning for warning in data['warnings']))
        (plan / 'decomposition.json').write_text(json.dumps({'nodes': {'d': {'decision': 'nonsense', 'by': 7, 'reason': 3}},
                                                             'limits': {'depth': 'x'}}))
        tree = detail(plan)['tasks'][0]['decomposition']
        self.assertEqual((tree['decision'], tree['by'], tree['reason']), ('split', 'default', None))
        self.assertEqual(summarize(plan)['decomposition']['limits'], {'depth': 2, 'children': 6, 'nodes': 24, 'frozen': False})

    def test_later_stages_and_the_place_in_a_chain(self):
        plan = self.plan()
        (plan / 'later.json').write_text(json.dumps({'later': [{'title': 'Second', 'goal': 'More.'}, {'title': ' '}, 'bad']}))
        self.assertEqual(detail(plan)['later'], {'stages': [{'title': 'Second', 'goal': 'More.'}], 'stage': None, 'first': None})
        nxt = self.plan('plan-2')
        (nxt / 'chain.json').write_text(json.dumps({'first': str(plan), 'stage': 2, 'previous': str(plan), 'remaining': []}))
        self.assertEqual(detail(nxt)['later'], {'stages': [], 'stage': 2, 'first': {'key': run_key(plan), 'path': str(plan)}})


class CatalogTests(Case):
    def test_a_sub_plan_is_never_listed_and_is_found_by_its_key(self):
        plan = self.plan()
        sub = self.split(plan)
        inner = sub / 'tasks/a/sub'
        catalog = Catalog(plans=[plan], registry=False)
        self.assertEqual([run['key'] for run in catalog.runs()], [run_key(plan)])
        self.assertEqual(catalog.keys(), {run_key(plan)})
        for path in (sub, inner):
            with self.subTest(path.relative_to(plan)):
                got = catalog.get(run_key(path))
                self.assertEqual((got['path'], got['source'], got['run_id']),
                                 (str(path), 'sub', (path / 'run_id').read_text().strip()))
                self.assertEqual(got['repo_root'], str(self.repo))
                self.assertEqual(catalog.locate(run_key(path)), (str(path), 'plan'))
                self.assertIsNotNone(catalog.identity(run_key(path)))
        task = catalog.task(run_key(sub), 'b')
        self.assertEqual(task['id'], 'b')
        self.assertIsNone(catalog.get('0' * 12))
        self.assertIsNone(catalog.locate('0' * 12))

    def test_a_listed_run_wins_over_a_sub_plan_with_the_same_key(self):
        plan = self.plan()
        sub = self.split(plan, nested=False)
        catalog = Catalog(plans=[plan, sub], registry=False)
        self.assertEqual(catalog.get(run_key(sub))['source'], 'plan')

    def test_the_worktrees_screen_knows_the_runs_of_sub_plans(self):
        plan = self.plan()
        sub = self.split(plan)
        self.status(sub, 'b', 'RUNNING')
        runs = worktrees._runs(Catalog(plans=[plan], registry=False))
        self.assertEqual([(run['run_id'], run['key']) for run in runs],
                         [('plan', run_key(plan)), ('plan--d', run_key(sub)), ('plan--d--a', run_key(sub / 'tasks/a/sub'))])
        self.assertEqual(runs[1]['tasks'], {'a': 'PENDING', 'b': 'RUNNING'})
        self.assertTrue(runs[1]['live'])


if __name__ == '__main__':
    unittest.main()
