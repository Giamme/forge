"""Automatic decomposition contracts and real-Fractal/fake-provider execution."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

from test_forge import FAKE, ROOT
import test_fractal as fixtures
from forge_fractal import read_json, write_json
from forge_fractal.cli import parser
from forge_fractal.decomposition import DecisionStage, capacity, parse_decision, reserved_nodes
from forge_fractal.execution import Tree, Halt
from forge_fractal.pipeline import retry
from forge_fractal.selection import decomposition_settings, select


def child(name, paths=None):
    return dict(id=name, goal='Implement and test ' + name, difficulty='low', paths=paths or [name + '.txt'], deps=[])


class DecisionContracts(unittest.TestCase):
    setUp = fixtures.FractalContracts.setUp

    def args(self, *extra):
        run = self.root / 'run'
        run.mkdir(exist_ok=True)
        return parser().parse_args(['select', '--run-dir', str(run), '--repo', str(self.root),
                                   '--mode', 'solo', '--dwarf', 'luna:high', *extra])

    def test_planner_precedence_and_role_resolution(self):
        args = self.args('--fractal-auto-decompose')
        with patch('forge_fractal.selection.resolve', side_effect=lambda spec, role, **kw: dict(spec=spec, role=role)):
            first = decomposition_settings(args, {'luna:high', 'terra:high'})
            self.assertEqual(first['resolved'], [dict(spec=s, role='planner') for s in ('luna:high', 'terra:high')])
            (Path(args.run_dir) / 'planner').write_text('sol:xhigh\n')
            self.assertEqual(decomposition_settings(args, {'luna:high'})['planner'], 'sol:xhigh')
            args.fractal_planner = 'opus:high'
            configured = decomposition_settings(args, {'luna:high'})
            self.assertEqual(configured['planner'], 'opus:high')
            self.assertEqual(configured['resolved'][0]['role'], 'planner')

    def test_conflicts_frozen_config_and_legacy(self):
        with self.assertRaisesRegex(ValueError, 'mutually exclusive'):
            select(self.args('--choice', 'off', '--fractal-auto-decompose'))
        with self.assertRaisesRegex(ValueError, 'requires'):
            select(self.args('--choice', 'off', '--fractal-planner', 'sol'))
        with self.assertRaisesRegex(ValueError, 'frozen'):
            decomposition_settings(self.args('--fractal-auto-decompose'), {'sol'}, {})
        saved = dict(decomposition=dict(enabled=True, planner='sol', resolved=[]))
        self.assertEqual(decomposition_settings(self.args(), {'luna'}, saved), saved['decomposition'])
        with self.assertRaisesRegex(ValueError, 'frozen'):
            decomposition_settings(self.args('--fractal-planner', 'opus'), {'sol'}, saved)

    def test_dry_selection_does_not_activate_runtime_or_write(self):
        args = self.args('--fractal-auto-decompose', '--fractal-planner', 'sol', '--dry-run')
        with contextlib.redirect_stderr(io.StringIO()), patch('forge_fractal.selection.resolve', return_value={}), \
             patch('forge_fractal.selection.ensure_runtime', side_effect=AssertionError('runtime called')):
            self.assertEqual(select(args), 'dry-fractal')
        self.assertFalse((Path(args.run_dir) / 'fractal-selection.json').exists())
        self.assertFalse((self.root / 'state').exists())

    def test_decision_schema_rejects_shortcuts(self):
        atomic = dict(decision='atomic', reason='One tightly coupled change')
        self.assertEqual(parse_decision('FORGE_FRACTAL: ' + json.dumps(atomic)), atomic)
        invalid = [dict(done=True), dict(decision='atomic', reason=' '),
                   dict(atomic, children=[]), dict(decision='split', reason='split', children=[child('a')]),
                   dict(decision='split', reason='split', children=[dict(child('a'), paths='a'), child('b')])]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_decision('FORGE_FRACTAL: ' + json.dumps(value))
        for text in ('', 'FORGE_FRACTAL: {}\nmore text', 'FORGE_FRACTAL: {}\nFORGE_FRACTAL: {}'):
            with self.assertRaises(ValueError):
                parse_decision(text)

    def tree(self):
        run = self.root / 'managed'; task = run / 'tasks/task'
        write_json(run / 'run.json', dict(limits=dict(depth=2, children=3, nodes=5, iterations=6, deadline=60),
                   routing={'low': ['luna:high']}, eligible=['luna:high'], decomposition={'enabled': True}))
        write_json(task / 'request.json', dict(prompt='full task', planner='sol:xhigh'))
        write_json(task / 'state.json', dict(elapsed=0))
        tree = Tree(run, task)
        root = dict(id='implementation', parent=None, paths=['.'], ancestors=[], goal='full task',
                    model='luna:high', status='pending', iteration=0)
        tree.update(root)
        return tree, root

    def test_admission_is_reserved_and_recovers_without_duplicate_planning(self):
        tree, root = self.tree(); stage = DecisionStage(tree)
        stage.accept(root, dict(attempts=1, planner='sol'), dict(decision='split', reason='separable',
                                                             children=[child('a'), child('b')]))
        self.assertEqual(len(reserved_nodes(tree.nodes())), 3)
        created = []

        def create(parent, **request):
            created.append(request['name'])
            node = dict(request, id=request['name'], parent=parent['id'], status='pending', ancestors=['implementation'])
            tree.update(node)
            if len(created) == 1:
                raise RuntimeError('crash after node.json')

        with patch.object(tree, 'create_node', side_effect=create):
            with self.assertRaisesRegex(RuntimeError, 'crash'):
                stage.recover_admission(root)
            stage.recover_admission(root)
            stage.recover_admission(root)
        self.assertEqual(created, ['a', 'b'])
        self.assertEqual(len(tree.nodes()), 3)

    def test_concurrent_acceptance_reserves_capacity(self):
        tree, root = self.tree()
        parents = []
        for name in ('a', 'b'):
            node = dict(root, id=name, parent='implementation', ancestors=['implementation'])
            tree.update(node); parents.append(node)
        barrier = threading.Barrier(2)

        def accept(node):
            barrier.wait()
            DecisionStage(tree).accept(node, dict(attempts=1), dict(decision='split', reason='split',
                                       children=[child(node['id'] + '1'), child(node['id'] + '2')]))

        threads = [threading.Thread(target=accept, args=(node,)) for node in parents]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(n['decomposition']['decision'] for n in parents), ['bounded', 'split'])
        self.assertEqual(len(reserved_nodes(tree.nodes())), 5)

    def test_scope_dependencies_and_case_collisions_rejected(self):
        tree, root = self.tree(); root['paths'] = ['src']
        invalid = [[child('a'), child('b')],
                   [child('a', ['src']), dict(child('b', ['src']), deps=['missing'])],
                   [dict(child('a', ['src']), deps=['b']), dict(child('b', ['src']), deps=['a'])],
                   [child('same', ['src']), child('SAME', ['src'])]]
        for requests in invalid:
            with self.subTest(requests=requests), self.assertRaises(ValueError):
                tree.validate_children(root, requests)
        self.assertEqual(len(tree.nodes()), 1)

    def test_retry_replans_changed_goal_and_preserves_reservations(self):
        tree, root = self.tree()
        request = read_json(tree.task / 'request.json')
        request['repo'] = str(self.root); write_json(tree.task / 'request.json', request)
        stage = DecisionStage(tree)
        stage.accept(root, dict(attempts=1), dict(decision='split', reason='separable',
                                               children=[child('a'), child('b')]))
        with patch('forge_fractal.pipeline.artifact', return_value='fingerprint'):
            retry(tree.run, tree.task, 'new reviewed requirements', 'luna:high')
        changed = tree.nodes()['implementation']
        self.assertNotIn('decomposition', changed)
        self.assertEqual(changed['decomposition_history'][0]['decision'], 'split')
        self.assertEqual(len(reserved_nodes(tree.nodes())), 3)
        self.assertTrue(changed['awaiting_children'])

    def test_unchanged_accepted_decision_is_reused(self):
        tree, root = self.tree()
        DecisionStage(tree).accept(root, dict(attempts=1), dict(decision='atomic', reason='atomic'))
        with patch('forge_fractal.decomposition.DecisionStage.invoke', side_effect=AssertionError('replanned')):
            DecisionStage(tree).ensure(root, self.root)
        self.assertEqual(root['decomposition']['attempts'], 1)


AUTO_FAKE = FAKE.replace("qa = '--disallowed-tools'", "planning = 'FORGE_DECOMPOSITION_DECISION' in p\nqa = '--disallowed-tools'")
AUTO_FAKE = AUTO_FAKE.replace("('qa' if qa else 'dwarf')+'\\n'", "('planner' if planning else ('qa' if qa else 'dwarf'))+'\\n'")
AUTO_FAKE = AUTO_FAKE.replace("if not qa:\n", "if not qa:\n if 'Owned paths: ' in p:\n  import json; owned = json.JSONDecoder().raw_decode(p.split('Owned paths: ')[-1])[0]; name = 'change.txt' if owned == ['.'] else owned[0]\n", 1)
AUTO_FAKE = AUTO_FAKE.replace("result = ('FORGE_VERDICT: '+verdict) if qa else 'implemented'", '''
import json
result = ('FORGE_VERDICT: '+verdict) if qa else 'implemented\\nFORGE_FRACTAL: {"done":true}'
if planning:
 context = json.JSONDecoder().raw_decode(p.split('Node planning context:\\n')[-1])[0]
 with open(os.environ['CALLS']+'.planning', 'a') as f: f.write(json.dumps({'context':context,'argv':a})+'\\n')
 if os.environ.get('PLAN_SLEEP'): time.sleep(4)
 if os.environ.get('PLAN_MUTATE'): pathlib.Path('base.txt').write_text('planner edit')
 if os.environ.get('PLAN_FAIL'): sys.exit(9)
 mode = os.environ.get('PLAN_MODE', 'nested')
 response = {'decision':'atomic','reason':'One atomic edit remains'}
 def c(name,paths): return {'id':name,'goal':'Implement and test '+name,'difficulty':'low','paths':paths,'deps':[]}
 if mode == 'nested' and context['node'] == 'implementation':
  response = {'decision':'split','reason':'Separable modules','children':[c('alpha',['a.txt','b.txt']),c('beta',['c.txt'])]}
 elif mode == 'nested' and context['node'] == 'alpha':
  response = {'decision':'split','reason':'Two independent files','children':[c('alpha1',['a.txt']),c('alpha2',['b.txt'])]}
 elif os.environ.get('PLAN_DEEP') and context['node'] == 'alpha1':
  response = {'decision':'split','reason':'Ordered stages','children':[c('deep1',['a.txt']),dict(c('deep2',['a.txt']),deps=['deep1'])]}
 result = 'FORGE_FRACTAL: '+json.dumps(response)
 if mode == 'invalid' or (mode == 'repair' and not context['feedback']): result = 'FORGE_FRACTAL: {"done":true}'
elif not qa and os.environ.get('WORK_CHILDREN'):
 result = 'FORGE_FRACTAL: {"children":[{}]}'
''')


@unittest.skipUnless(os.environ.get('FORGE_TEST_FRACTAL_RUNTIME'), 'requires pinned Fractal runtime')
class AutoIntegration(unittest.TestCase):
    git = fixtures.FractalIntegration.git
    solo = fixtures.FractalIntegration.solo
    managed = fixtures.FractalIntegration.managed
    cli = fixtures.FractalIntegration.cli
    wait_for = fixtures.FractalIntegration.wait_for
    start_solo = fixtures.FractalIntegration.start_solo
    parallel_plan = fixtures.FractalIntegration.parallel_plan

    def setUp(self):
        fixtures.FractalIntegration.setUp(self)
        for binary in self.bin.iterdir(): binary.write_text(AUTO_FAKE)

    def nodes(self):
        return [read_json(path) for path in self.managed().glob('tasks/*/nodes/*/node.json')]

    def test_nested_planning_and_separate_models(self):
        result = self.solo('--fractal-auto-decompose', '--fractal-planner', 'sol:xhigh',
                           '--dwarf-low', 'luna:high', '--fractal-concurrency', '1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        nodes = {n['id']: n for n in self.nodes()}
        self.assertEqual(len(nodes), 5)
        self.assertEqual(nodes['alpha']['model'], 'luna:high')
        self.assertEqual(nodes['alpha']['resolved_planner'], 'gpt-5.6-sol:xhigh:codex')
        self.assertEqual(nodes['alpha1']['decomposition']['decision'], 'bounded')
        self.assertIn('.alpha.alpha1', nodes['alpha1']['branch'])
        self.assertTrue(all(n['iteration'] == 1 for n in nodes.values()))
        self.assertTrue(all((self.repo / name).exists() for name in ('a.txt', 'b.txt', 'c.txt')))
        calls = (self.root / 'calls').read_text().splitlines()
        self.assertEqual(calls[0], 'planner'); self.assertEqual(calls.count('qa'), 1)
        self.assertEqual(calls.count('planner'), 3)
        self.assertEqual(self.solo('--dry-run').returncode, 0)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), calls)
        report = self.root / 'report.html'
        captured = self.cli('report', '--html', str(report))
        self.assertEqual(captured.returncode, 0, captured.stderr)
        self.assertIn('planner.last', report.read_text())

    def test_invalid_decisions_repair_once_and_then_halt(self):
        self.env['PLAN_MODE'] = 'invalid'
        result = self.solo('--fractal-auto-decompose')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['planner', 'planner'])
        self.assertEqual(self.nodes()[0]['status'], 'planning_exhausted')
        self.env['PLAN_MODE'] = 'repair'
        result = self.solo('--retry')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.nodes()[0]['decomposition']['attempts'], 2)

    def test_planner_mutation_fails_without_import(self):
        self.env['PLAN_MUTATE'] = '1'; self.env['PLAN_MODE'] = 'atomic'
        result = self.solo('--fractal-auto-decompose', '--yolo-dwarf')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.nodes()[0]['status'], 'planning_mutation')
        self.assertEqual((self.repo / 'base.txt').read_text(), 'base\n')
        calls = json.loads((self.root / 'calls.planning').read_text().splitlines()[0])
        self.assertIn('read-only', calls['argv'])
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', calls['argv'])

    def test_failure_and_unplanned_children_never_fall_through(self):
        self.env['PLAN_FAIL'] = '1'
        result = self.solo('--fractal-auto-decompose')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.nodes()[0]['status'], 'planning_failed')
        self.env.pop('PLAN_FAIL'); self.env['PLAN_MODE'] = 'atomic'; self.env['WORK_CHILDREN'] = '1'
        result = self.solo('--retry')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.nodes()[0]['status'], 'unplanned_children')
        self.assertEqual(len(self.nodes()), 1)

    def test_depth_zero_avoids_planner_and_frozen_upgrade(self):
        result = self.solo('--fractal-auto-decompose', '--fractal-depth', '0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / 'calls').read_text().splitlines(), ['dwarf', 'qa'])
        self.assertEqual(self.nodes()[0]['decomposition']['decision'], 'bounded')
        result = self.solo('--fractal-depth', '3')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('frozen', result.stderr)

    def test_depth_three_and_recovery_after_upstream_init(self):
        self.env['PLAN_DEEP'] = '1'
        result = self.solo('--fractal-auto-decompose', '--fractal-depth', '3')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        nodes = {n['id']: n for n in self.nodes()}
        self.assertEqual(nodes['deep1']['ancestors'], ['implementation', 'alpha', 'alpha1'])
        self.assertEqual(nodes['deep2']['decomposition']['decision'], 'bounded')
        task = next(self.managed().glob('tasks/*'))
        # Crash after Fractal initialized a child, before Forge saved its node.json.
        code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from forge_fractal.execution import Tree
tree = Tree(Path(sys.argv[2]), Path(sys.argv[3])); tree.initialize()
root = tree.nodes()['implementation']; original = tree.update
def crash(node, **values):
    if node['id'] == 'recovered': raise RuntimeError('crash after upstream init')
    original(node, **values)
tree.update = crash
try: tree.create_node('recovered', root, 'test recovery', 'low', ['.'], [], 'sol')
except RuntimeError as error: assert str(error) == 'crash after upstream init', error
else: raise AssertionError('no simulated crash')
tree.update = original
tree.create_node('recovered', root, 'test recovery', 'low', ['.'], [], 'sol')
assert tree.nodes()['recovered']['parent'] == 'implementation'
'''
        runtime = Path(os.environ['FORGE_TEST_FRACTAL_RUNTIME']) / 'bin/python'
        result = subprocess.run([str(runtime), '-c', code, str(ROOT / 'scripts'), str(self.managed()), str(task)],
                                env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pause_resume_and_stop_during_planning(self):
        self.env['PLAN_SLEEP'] = '1'; self.env['PLAN_MODE'] = 'atomic'
        process = self.start_solo('--fractal-auto-decompose')
        self.wait_for(lambda: (self.root / 'calls.planning').exists())
        self.assertEqual(self.cli('pause').returncode, 0)
        self.wait_for(lambda: self.nodes()[0]['status'] == 'paused')
        count = (self.root / 'calls').read_text().splitlines().count('planner')
        time.sleep(.3)
        self.assertEqual((self.root / 'calls').read_text().splitlines().count('planner'), count)
        self.assertEqual(self.cli('resume').returncode, 0)
        self.wait_for(lambda: (self.root / 'calls').read_text().splitlines().count('planner') > count)
        self.assertEqual(self.cli('stop').returncode, 0)
        process.wait(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertNotIn('dwarf', (self.root / 'calls').read_text().splitlines())
        self.env.pop('PLAN_SLEEP')
        self.assertEqual(self.cli('resume').returncode, 0)
        self.wait_for(lambda: (self.root / 'solo/verdict').exists(), 30)
        self.assertEqual((self.root / 'solo/verdict').read_text().strip(), 'PASS')

    def test_planning_respects_invocation_timeout(self):
        self.env['PLAN_SLEEP'] = '1'
        result = self.solo('--fractal-auto-decompose', '--timeout', '1')
        self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
        self.assertEqual(self.nodes()[0]['status'], 'timeout')
        self.assertNotIn('dwarf', (self.root / 'calls').read_text().splitlines())

    def test_parallel_uses_saved_planner_and_keeps_qa(self):
        self.env['PLAN_MODE'] = 'atomic'
        plan, script = self.parallel_plan(dwarf='luna:high', planner='sol:xhigh')
        result = subprocess.run([*script, 'run', str(plan), '--fractal-auto-decompose', '--fractal-concurrency', '1'],
                                env=self.env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(all(n['resolved_planner'] == 'gpt-5.6-sol:xhigh:codex' for n in self.nodes()))
        self.assertEqual((self.root / 'calls').read_text().splitlines().count('qa'), 2)
        self.assertIn('MERGED', (plan / 'results.tsv').read_text())
