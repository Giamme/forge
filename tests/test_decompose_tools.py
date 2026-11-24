"""The tools around a run and a split task (#153): `forge runs` and `forge runs tree`, `forge runs rm`,
`forge watch`, `forge handoff` and the dataset each understand that a split task's subtasks are a plan
of their own under it, `<plan>/tasks/<id>/sub`.

The plan dirs are made with forge's own module (scripts/forge_decompose.py). Nothing here dispatches a
model: the runs of a split task are in test_decompose_run.py.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import test_handoff_resume as resume_fixtures
import test_runs_rm as rm_fixtures

RUN_ID = rm_fixtures.RUN_ID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'ui'))
import forge_decompose  # noqa: E402
from forge_dataset import harvest  # noqa: E402
from forge_ui import reader  # noqa: E402

RUNS = ROOT / 'scripts' / 'forge-runs.py'
HANDOFF = ROOT / 'scripts' / 'forge-handoff.py'


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_') + '_under_test', ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def child(tid, files, deps=(), **more):
    return dict({'id': tid, 'deps': list(deps), 'difficulty': 'low', 'files': files, 'title': 'Part ' + tid,
                 'prompt': 'Do part %s.' % tid}, **more)


def make_plan(base, repo, name='plan'):
    """d (owns src/ and docs/) and c, which depends on d."""
    plan = Path(base) / name
    (plan / 'tasks').mkdir(parents=True)
    (plan / 'tasks.tsv').write_text('d\t-\thigh\tsrc/,docs/\tsol\topus\tThe whole of D\nc\td\tlow\tc.txt\tsol\topus\tC\n')
    for task in 'dc':
        (plan / 'tasks' / task).mkdir()
        (plan / 'tasks' / task / 'prompt.md').write_text('Do %s.\n' % task)
    for file, text in (('run_id', name), ('repo', str(repo)), ('goal.txt', 'Build D and C')):
        (plan / file).write_text(text + '\n')
    return plan


def split(plan, nested=True, **how):
    """d is split into a (split again into one and two) and b. Returns d's sub-plan."""
    first = child('a', ['src/a/'])
    if nested:
        first['split'] = {'reason': 'two steps', 'tasks': [child('one', ['src/a/one.py']),
                                                          child('two', ['src/a/two.py'], deps=['one'])]}
    sub = forge_decompose.split(plan, 'd', {'reason': 'two halves', 'tasks': [first, child('b', ['docs/'])]},
                                **dict(dict(by='planner'), **how))
    forge_decompose.settle(plan, by='planner')
    return sub


def status(plan, task, value):
    (Path(plan) / 'tasks' / task).mkdir(parents=True, exist_ok=True)
    (Path(plan) / 'tasks' / task / 'status').write_text(value + '\n')


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.plan = make_plan(self.base, self.repo)
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(self.base / 'config'))

    def run_tool(self, script, *args):
        return subprocess.run([sys.executable, str(script), *map(str, args)], capture_output=True, text=True,
                              env=self.env, cwd=self.base, timeout=120)


class RunsTests(Case):
    def test_the_runs_line_counts_the_subtasks_of_split_tasks(self):
        runs = load('forge-runs')
        whole = runs.line(reader.summarize(self.plan), time.time())
        self.assertNotIn('subtasks', whole)
        split(self.plan)
        shown = runs.line(reader.summarize(self.plan), time.time())
        self.assertIn('4 subtasks in 1 split task ', shown)

    def test_runs_tree_prints_every_level_with_its_decision_and_the_limits(self):
        sub = split(self.plan)
        status(sub, 'b', 'FAIL')
        done = self.run_tool(RUNS, 'tree', self.plan)
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = done.stdout.splitlines()
        self.assertEqual(lines[0], 'decomposition limits: depth 2, at most 6 subtasks per split, 24 subtasks in the whole run')
        self.assertRegex(done.stdout, r'\nd +PENDING +The whole of D \[split into 2\]\n  split: two halves\n')
        self.assertRegex(done.stdout, r'\n  a +PENDING +Part a \[split into 2\]\n')
        self.assertRegex(done.stdout, r'\n    one +PENDING +Part one\n')
        self.assertRegex(done.stdout, r'\n  b +FAIL +Part b\n')
        self.assertRegex(done.stdout, r'\nc +PENDING +C\n')
        self.assertIn('subtasks of d: %s' % sub, done.stdout)
        self.assertIn('subtasks of d/a: %s' % (sub / 'tasks/a/sub'), done.stdout)

    def test_runs_tree_as_json_and_on_what_is_not_a_plan(self):
        split(self.plan, nested=False)
        forge_decompose.freeze(self.plan)
        data = json.loads(self.run_tool(RUNS, 'tree', self.plan, '--json').stdout)
        self.assertEqual(data['limits'], {'depth': 2, 'children': 6, 'nodes': 24, 'frozen': True})
        self.assertEqual([(n['id'], n['level'], n['decision'], n['by']) for n in data['nodes']],
                         [('d', 0, 'split', 'planner'), ('d/a', 1, 'atomic', 'default'), ('d/b', 1, 'atomic', 'default'),
                          ('c', 0, 'atomic', 'default')])
        done = self.run_tool(RUNS, 'tree', self.repo)
        self.assertEqual(done.returncode, 2)
        self.assertIn('not a plan dir', done.stderr)


class WatchTests(Case):
    def test_a_subtask_is_followed_by_its_path_and_its_failure_is_an_event(self):
        watch = load('forge-watch')
        sub = split(self.plan)
        status(self.plan, 'd', 'RUNNING')
        status(sub, 'b', 'RUNNING')
        before = reader.summarize(self.plan)
        self.assertEqual(watch.statuses(before), {'d': 'RUNNING', 'c': 'PENDING', 'd/a': 'PENDING', 'd/a/one': 'PENDING',
                                                  'd/a/two': 'PENDING', 'd/b': 'RUNNING'})
        status(sub, 'b', 'FAIL')
        after = reader.summarize(self.plan)
        events = watch.events(before, after, time.time())
        self.assertEqual([(e['kind'], e['task'], e['status']) for e in events], [('task-failed', 'd/b', 'FAIL')])
        self.assertEqual(watch.describe(events[0]), 'run plan: task d/b FAIL')

    def test_a_plan_with_no_split_is_followed_as_before(self):
        watch = load('forge-watch')
        self.assertEqual(watch.statuses(reader.summarize(self.plan)), {'d': 'PENDING', 'c': 'PENDING'})


class HandoffTests(Case):
    def show(self, *args):
        done = self.run_tool(HANDOFF, 'show', self.plan, *args)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_a_plan_with_no_split_has_no_subtasks(self):
        data = json.loads(self.show('--json'))
        self.assertEqual(data['subtasks'], [])
        self.assertNotIn('subtasks, in', self.show())

    def test_the_report_lists_the_subtasks_under_their_task_with_their_plan_dir(self):
        sub = split(self.plan)
        status(sub, 'b', 'FAIL')
        data = json.loads(self.show('--json'))
        self.assertEqual([task['id'] for task in data['tasks']], ['d', 'c'])
        self.assertEqual([(row['id'], row['parent'], row['plan'], row['status'], row['split']) for row in data['subtasks']],
                         [('d/a', 'd', str(sub), 'PENDING', 'open'),
                          ('d/a/one', 'd/a', str(sub / 'tasks/a/sub'), 'PENDING', None),
                          ('d/a/two', 'd/a', str(sub / 'tasks/a/sub'), 'PENDING', None),
                          ('d/b', 'd', str(sub), 'FAIL', None)])
        text = self.show()
        self.assertIn('    subtasks, in %s:' % sub, text)
        self.assertRegex(text, r'\n    b +FAIL +sol\n')
        self.assertIn('      subtasks, in %s:' % (sub / 'tasks/a/sub'), text)

    def test_a_subtask_that_waits_for_a_person_is_answered_in_its_own_plan(self):
        sub = split(self.plan, nested=False)
        status(sub, 'a', 'NEEDS_INPUT')
        (sub / 'tasks/a/needs_input.md').write_text('Which port?\n')
        status(sub, 'b', 'NEEDS_DECISION')
        (sub / 'tasks/b/decision.md').write_text('reasons: converging, split\n\nb stopped.\n')
        data = json.loads(self.show('--json'))
        self.assertEqual([(item['path'], item['task'], item['kind'], item['plan']) for item in data['needs_you']],
                         [('d/a', 'a', 'NEEDS_INPUT', str(sub)), ('d/b', 'b', 'NEEDS_DECISION', str(sub))])
        commands = [step['command'] for step in data['next']]
        self.assertTrue(any(c.endswith('retry %s a --answer "…"' % sub) for c in commands), commands)
        self.assertTrue(any('retry %s b --decide split --subtasks FILE' % sub in c for c in commands), commands)
        text = self.show()
        self.assertIn('  d/a: NEEDS_INPUT, the question:', text)
        self.assertIn('  d/b: NEEDS_DECISION  reasons: converging, split', text)

    def test_a_task_that_stopped_on_split_is_offered_the_split(self):
        status(self.plan, 'd', 'NEEDS_DECISION')
        (self.plan / 'tasks/d/decision.md').write_text('reasons: split\n')
        status(self.plan, 'c', 'NEEDS_DECISION')
        (self.plan / 'tasks/c/decision.md').write_text('reasons: converging\n')
        steps = json.loads(self.show('--json'))['next']
        offered = [step for step in steps if '--decide split' in step['command']]
        self.assertEqual(len(offered), 1, steps)
        self.assertIn('retry %s d --decide split --subtasks FILE --reason "…" --approved' % self.plan, offered[0]['command'])
        self.assertIn("only on the user's word", offered[0]['why'])

    def test_a_subtask_left_running_by_a_dead_run_is_stale(self):
        sub = split(self.plan, nested=False)
        status(self.plan, 'd', 'RUNNING')
        status(sub, 'a', 'RUNNING')
        stale = json.loads(self.show('--json'))['stale']
        self.assertEqual([item['task'] for item in stale], ['d', 'd/a'])


class ResumeTests(unittest.TestCase):
    """`forge handoff resume` one level down: a subtask a dead run left RUNNING is saved, reset and
    prepared in its own plan. Real git worktrees; forge-parallel.sh is the fake of test_handoff_resume.py."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(os.path.realpath(temp.name))
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        resume_fixtures.git(self.repo, 'init', '-q', '-b', 'main')
        (self.repo / 'a.txt').write_text('one\n')
        resume_fixtures.git(self.repo, 'add', '.')
        resume_fixtures.git(self.repo, 'commit', '-q', '-m', 'init')
        self.plan = self.root / 'plan'
        (self.plan / 'tasks/a').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('a\t-\tmedium\ta.txt,x.txt\tsol\topus\tA\n')
        (self.plan / 'tasks/a/prompt.md').write_text('Do a.\n')
        (self.plan / 'run_id').write_text('r1\n')
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        (self.plan / 'wt_root').write_text(str(self.root / 'wt') + '\n')
        status(self.plan, 'a', 'FAIL')
        self.sub = forge_decompose.split(self.plan, 'a', {'tasks': [child('x', ['a.txt']), child('y', ['x.txt'])]},
                                         reason='two halves', by='user')
        (self.sub / 'wt_root').write_text(str(self.root / 'wt-sub') + '\n')
        (self.sub / 'repo').write_text(str(self.repo) + '\n')
        status(self.plan, 'a', 'RUNNING')
        self.parent_wt = self.worktree(self.root / 'wt/a', 'forge/r1/a')
        self.wt = self.worktree(self.root / 'wt-sub/x', 'forge/r1--a/x')
        (self.wt / 'a.txt').write_text('half done\n')
        status(self.sub, 'x', 'RUNNING')
        status(self.sub, 'y', 'INTERRUPTED')
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': 2 ** 22 + 12345, 'started': 1}))
        fake = self.root / 'fake-parallel.sh'
        fake.write_text(resume_fixtures.FAKE)
        self.log = self.root / 'fake.log'
        self.env = dict(os.environ, FORGE_DATASET='off', FORGE_HANDOFF_PARALLEL=str(fake), FAKE_LOG=str(self.log),
                        FORGE_HANDOFF_NO_START='1')

    def worktree(self, path, branch):
        path.parent.mkdir(parents=True, exist_ok=True)
        resume_fixtures.git(self.repo, 'worktree', 'add', '-q', '-b', branch, str(path))
        return path

    def resume(self):
        done = subprocess.run([sys.executable, str(HANDOFF), 'resume', str(self.plan), '--json'], capture_output=True,
                              text=True, env=self.env, cwd=str(self.root), timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.said = done.stderr
        return json.loads(done.stdout)

    def state(self, task):
        return (self.sub / 'tasks' / task / 'status').read_text().strip()

    def test_a_stale_subtask_is_saved_reset_and_prepared_in_its_own_plan(self):
        data = self.resume()
        self.assertEqual([(row['task'], row.get('plan'), row['reset']) for row in data['recovered']],
                         [('a', None, True), ('x', str(self.sub), True)])
        diff = Path(data['recovered'][1]['diff'])
        self.assertEqual(diff.parent, self.sub / 'tasks/x')
        self.assertIn('+half done', diff.read_text())
        self.assertEqual((self.wt / 'a.txt').read_text(), 'one\n')
        self.assertEqual((self.state('x'), self.state('y')), ('INTERRUPTED', 'INTERRUPTED'))
        self.assertEqual(self.log.read_text().splitlines(),
                         ['retry %s a --no-dispatch' % self.plan, 'retry %s x --no-dispatch' % self.sub,
                          'retry %s y --no-dispatch' % self.sub])
        self.assertEqual([(row['task'], row.get('plan')) for row in data['prepared']],
                         [('a', None), ('x', str(self.sub)), ('y', str(self.sub))])

    def test_subtasks_whose_plan_another_command_holds_are_left_alone_and_said(self):
        holder = subprocess.Popen([sys.executable, '-c', resume_fixtures.HOLD_LOCK, str(self.sub / 'schedule.lock')],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            data = self.resume()
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)
            holder.stdout.close()
        self.assertEqual([row['task'] for row in data['recovered']], ['a'])
        self.assertEqual((self.wt / 'a.txt').read_text(), 'half done\n')
        self.assertEqual(self.state('x'), 'RUNNING')
        self.assertIn('warning: a: its subtasks (%s) are locked by ' % self.sub, self.said)

    def test_subtasks_already_on_the_tasks_branch_are_not_looked_at(self):
        (self.plan / 'tasks/a/sub.merged').write_text('abc\n')
        data = self.resume()
        self.assertEqual([row['task'] for row in data['recovered']], ['a'])
        self.assertEqual(self.state('x'), 'RUNNING')
        self.assertEqual(self.log.read_text().splitlines(), ['retry %s a --no-dispatch' % self.plan])


class DatasetTests(Case):
    def test_a_plan_with_no_record_has_none_of_it(self):
        self.assertEqual(harvest.decomposition_of(self.plan), (None, None, None))
        data = harvest.plan_event(self.plan, dict(run='u1', run_id='plan'), src='import')['data']
        for key in ('parent', 'decomposition', 'chain'):
            self.assertNotIn(key, data)

    def test_the_plan_record_carries_each_tasks_decision_and_a_split_is_a_new_revision(self):
        forge_decompose.settle(self.plan)
        prov = dict(run='u1', run_id='plan')
        before = harvest.plan_event(self.plan, prov, src='import')
        self.assertEqual(before['data']['decomposition']['nodes']['d']['decision'], 'atomic')
        self.assertEqual(harvest.plan_event(self.plan, prov, src='import')['rid'], before['rid'])
        split(self.plan, nested=False)
        after = harvest.plan_event(self.plan, prov, src='import')
        self.assertNotEqual(after['rid'], before['rid'])
        tree = after['data']['decomposition']
        self.assertEqual(tree['limits'], {'depth': 2, 'children': 6, 'nodes': 24})
        self.assertEqual(tree['nodes']['d'], {'decision': 'split', 'reason': 'two halves', 'by': 'planner', 'children': 2,
                                              'limit': None})

    def test_a_sub_plans_record_names_the_run_and_task_above_it_and_its_level(self):
        (self.plan / 'dataset.uid').write_text('rootuid\n')
        sub = split(self.plan, by='user', start='fresh')
        (sub / 'dataset.uid').write_text('subuid\n')
        inner = sub / 'tasks/a/sub'
        data = harvest.plan_event(sub, dict(run='subuid', run_id='plan--d'), src='import')['data']
        self.assertEqual(data['parent'], {'run': 'rootuid', 'run_id': 'plan', 'task': 'd', 'level': 1, 'root': 'rootuid',
                                          'start': 'fresh', 'by': 'user', 'reason': 'two halves'})
        self.assertEqual(data['orchestrator'], 'split')
        below = harvest.plan_event(inner, dict(run='x', run_id='plan--d--a'), src='import')['data']
        self.assertEqual((below['parent']['run'], below['parent']['task'], below['parent']['level'], below['parent']['root']),
                         ('subuid', 'a', 2, 'rootuid'))
        # A dispatch in a subtask's dir belongs to the sub-plan, as any task's does to its plan.
        where = harvest.locate(sub / 'tasks' / 'b')
        self.assertEqual((where['plan'], where['task'], where['run_id']), (sub, 'b', 'plan--d'))

    def test_a_bounded_node_and_a_chained_plan_are_recorded(self):
        forge_decompose.set_limits(self.plan, children=2)
        with self.assertRaises(forge_decompose.Bounded):
            forge_decompose.split(self.plan, 'd', {'tasks': [child('a', ['src/a']), child('b', ['src/b']), child('x', ['docs/'])]})
        (self.plan / 'later.json').write_text(json.dumps({'later': [{'title': 'Second', 'goal': 'More.'}]}))
        data = harvest.plan_event(self.plan, dict(run='u1', run_id='plan'), src='import')['data']
        self.assertEqual((data['decomposition']['nodes']['d']['decision'], data['decomposition']['nodes']['d']['limit']),
                         ('bounded', 'children'))
        self.assertEqual(data['chain'], {'stage': None, 'first': None, 'previous': None, 'later': ['Second']})


class RemoveTests(rm_fixtures.RmCase):
    """`forge runs rm` on a run with a split task: the sub-plan's branches, worktrees and kept ref are the run's."""

    def split_run(self):
        """Task a of the run is split into x and y. x has a worktree and a branch, y a branch, and the
        sub-plan its integration branch; a kept ref holds a's earlier work."""
        (self.plan / 'tasks/a/status').write_text('FAIL\n')
        sub = forge_decompose.split(self.plan, 'a', {'tasks': [child('x', ['a.txt']), child('y', ['a.txt'], deps=['x'])]},
                                    reason='two steps', by='user', start='fresh')
        sub_id = RUN_ID + '--a'
        self.assertEqual((sub / 'run_id').read_text().strip(), sub_id)
        root = self.wt_root.parent / sub_id
        root.mkdir(parents=True)
        self.git('branch', 'forge/%s/x' % sub_id)
        self.git('worktree', 'add', '-q', str(root / 'x'), 'forge/%s/x' % sub_id)
        self.git('branch', 'forge/%s/y' % sub_id)
        self.git('branch', 'forge/%s-integration' % sub_id)
        self.git('update-ref', 'refs/forge/kept/%s/a' % RUN_ID, 'HEAD')
        self.git('update-ref', 'refs/forge/kept/%s/zzz' % RUN_ID, 'HEAD')      # no task of the run: not its ref
        self.git('branch', 'forge/%s--other/x' % RUN_ID)                        # another run's branch
        return sub, root

    def test_a_dry_run_lists_what_the_sub_plan_left_in_git(self):
        sub, root = self.split_run()
        done, result = self.rmj(self.plan, '--dry-run', '--worktrees', '--branches')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted(b['name'] for b in result['branches']),
                         sorted(['forge/myrun--a-integration', 'forge/myrun--a/x', 'forge/myrun--a/y',
                                 'refs/forge/kept/myrun/a']))
        self.assertEqual([(wt['path'], wt['kind'], wt['branch']) for wt in result['worktrees']],
                         [(str(root / 'x'), 'run', 'forge/myrun--a/x')])
        self.assertTrue(sub.is_dir())

    def test_the_delete_takes_the_sub_plans_worktrees_branches_and_kept_ref(self):
        sub, root = self.split_run()
        done, result = self.rmj(self.plan, '--yes', '--no-archive', '--worktrees', '--branches')
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertFalse(self.plan.exists())
        self.assertFalse(root.exists())
        self.assertEqual(sorted(self.branches()), ['forge/myrun--other/x', 'main'])
        refs = self.git('for-each-ref', '--format=%(refname)', 'refs/forge/').split()
        self.assertEqual(refs, ['refs/forge/kept/myrun/zzz'])
        kept = [b for b in result['branches'] if b['name'] == 'refs/forge/kept/myrun/a'][0]
        self.assertTrue(kept['deleted'])
        self.assertIn('git update-ref refs/forge/kept/myrun/a', kept['note'])

    def test_a_sub_plan_is_not_deleted_by_itself(self):
        sub, _root = self.split_run()
        done = self.rm(sub, '--yes', '--no-archive')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn('deleted with the run it belongs to', done.stderr + done.stdout)
        self.assertTrue(sub.is_dir())


if __name__ == '__main__':
    unittest.main()
