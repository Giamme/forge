import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from forge_ui import fractal, reader
from forge_ui.server import make_server
import run_fixture as F


class FractalCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(os.path.realpath(temp.name))
        self.state = self.base / 'state'
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.state), 'XDG_CONFIG_HOME': str(self.base / 'config')})
        env.start()
        self.addCleanup(env.stop)
        self.plan, self.solo = F.make_plan(self.base), F.make_solo(self.base)
        self.task = F.make_fractal(self.state, self.plan, 'reader')
        # control/locked is unreadable; the temp dir's cleanup needs it back.
        self.addCleanup(os.chmod, self.task / 'control/locked', 0o700)

    def detail(self, task='reader', run=None):
        return reader.fractal_detail(run or self.plan, task)


class FractalTests(FractalCase):
    def test_run_detail_and_task(self):
        run_id = fractal.run_id(self.plan)
        self.assertEqual(reader.detail(self.plan)['fractal'], dict(enabled=True, run=run_id, available=True, reason=None))
        self.assertIsNone(reader.detail(self.solo)['fractal'])
        data = self.detail()
        self.assertEqual({key: data[key] for key in ('run', 'task', 'fractal_run', 'fractal_task', 'available', 'reason',
                                                     'warnings', 'truncated')},
                         dict(run=reader.run_key(self.plan), task='reader', fractal_run=run_id,
                              fractal_task=self.task.name, available=True, reason=None, warnings=[], truncated=[]))
        self.assertEqual(data['state'], dict(status='active', acceptance='pending', elapsed=120.5, started_at=F.T0 + 20,
                                             ended_at=None, exit_code=None, error=None, qa_status=None))
        self.assertIsNone(data['outcome'])
        self.assertEqual([(node['id'], node['parent'], node['depth'], node['status'], node['ui_status'], node['has_diff'],
                           node['reserved']) for node in data['nodes']], [
            ('implementation', None, 0, 'waiting', 'RUNNING', False, False),
            ('api', 'implementation', 1, 'active', 'RUNNING', False, False),
            ('docs', 'implementation', 1, 'reserved', 'PENDING', False, True),
            ('ui', 'implementation', 1, 'completed', 'MERGED', True, False)])
        root = data['nodes'][0]
        self.assertEqual(root['decomposition'], dict(phase='accepted', decision='split',
                                                     reason='The labels and the API are independent.',
                                                     children=['ui', 'api', 'docs']))
        self.assertEqual((root['cost'], root['tokens']['output_tokens'], root['resolved_model']),
                         (0.42, 300, 'gpt-sol:xhigh:codex'))
        self.assertEqual(sorted(root), sorted(['id', 'parent', 'depth', 'goal', 'difficulty', 'paths', 'deps', 'model',
                                               'resolved_model', 'status', 'ui_status', 'phase', 'iteration',
                                               'started_at', 'ended_at', 'cost', 'unknown_cost_steps', 'tokens',
                                               'decomposition', 'error', 'has_diff', 'reserved']))
        self.assertEqual(data['nodes'][2]['deps'], ['ui'])
        self.assertEqual([(event['kind'], event['node']) for event in data['events']][:2],
                         [('node_created', 'implementation'), ('decomposition_decision', 'implementation')])
        # No absolute path stored in the state root reaches the page.
        self.assertNotIn(str(self.state), json.dumps(data))
        json.dumps(data, allow_nan=False)
        F.write(self.task / 'outcome.json', dict(acceptance='PASS', verification='passed', task_status='PASS',
                                                 pipeline_exit_code=0, fingerprint='x'))
        self.assertEqual(self.detail()['outcome'], dict(acceptance='PASS', verification='passed', task_status='PASS'))

    def test_nodes_and_their_diffs(self):
        node = reader.fractal_node(self.plan, 'reader', 'implementation')
        self.assertEqual((node['id'], node['depth'], node['run'], node['fractal_task']),
                         ('implementation', 0, reader.run_key(self.plan), self.task.name))
        self.assertTrue(node['last'].startswith('…') and len(node['last']) == 20001)
        self.assertEqual(node['truncated'], ['last'])
        self.assertEqual(node['planning_tokens'], dict(input_tokens=500, cached_input_tokens=0, output_tokens=50))
        self.assertEqual([item['name'] for item in node['decomposition']['admission']], ['ui', 'api', 'docs'])
        self.assertEqual(node['decomposition_history'][0]['phase'], 'rejected')
        self.assertNotIn(str(self.state), json.dumps(node))
        for absent in ('control', 'workspace', 'source', 'candidate', 'branch', 'step', 'planning_step', 'fingerprint'):
            self.assertNotIn(absent, node)
        reserved = reader.fractal_node(self.plan, 'reader', 'docs')
        self.assertEqual((reserved['reserved'], reserved['last'], reserved['goal']), (True, None, 'Document the labels'))
        diff = reader.fractal_node_diff(self.plan, 'reader', 'ui')
        self.assertEqual(diff['source'], dict(kind='fractal', id='ui', which='candidate', execution=None, attempt=None,
                                              file=f'{self.task.name}/nodes/ui/candidate.diff'))
        self.assertEqual((diff['available'], [item['new'] for item in diff['files']], diff['findings'], diff['bytes']),
                         (True, ['ui/labels.py'], [], len(F.FRACTAL_DIFF)))
        missing = reader.fractal_node_diff(self.plan, 'reader', 'api')
        self.assertEqual((missing['available'], missing['reason'], missing['files']), (False, 'no candidate diff yet', []))
        for name in ('nope', '../ui', '', '-x', 'control'):
            self.assertIsNone(reader.fractal_node(self.plan, 'reader', name), name)
            self.assertIsNone(reader.fractal_node_diff(self.plan, 'reader', name), name)

    def test_never_opens_what_it_must_not(self):
        opened = []
        original_open, original_scandir = os.open, os.scandir

        def record_open(path, *args, **kwargs):
            opened.append(os.fsdecode(path) if not isinstance(path, int) else path)
            return original_open(path, *args, **kwargs)

        def record_scandir(path='.'):
            opened.append(('scandir', path if isinstance(path, int) else os.fsdecode(path)))
            return original_scandir(path)
        with patch('os.open', record_open), patch('os.scandir', record_scandir):
            data = self.detail()
            for name in ('implementation', 'ui', 'api', 'docs'):
                reader.fractal_node(self.plan, 'reader', name)
                reader.fractal_node_diff(self.plan, 'reader', name)
        names = {item for item in opened if isinstance(item, str)}
        self.assertFalse(names & {'control', 'locked', 'product', 'steps', 'planning', 'ledger.db', 'initialization.json'},
                         names)
        self.assertEqual(data['warnings'], [])

    def test_symlinks_are_refused(self):
        outside = self.base / 'elsewhere'
        nodes = self.task / 'nodes'
        moved = self.base / 'nodes-moved'
        nodes.rename(moved)
        nodes.symlink_to(moved)
        data = self.detail()
        self.assertEqual((data['available'], data['nodes']), (True, []))
        self.assertEqual(data['warnings'], [f'fractal {self.task.name}/nodes: a symlink or not a directory'])
        nodes.unlink()
        moved.rename(nodes)
        node = nodes / 'api/node.json'
        F.write(outside / 'node.json', json.loads(node.read_text()))
        node.unlink()
        node.symlink_to(outside / 'node.json')
        data = self.detail()
        self.assertNotIn('api', [item['id'] for item in data['nodes'] if not item['reserved']])
        self.assertEqual(data['warnings'], [f'fractal {self.task.name}/nodes/api/node.json: a symlink'])
        run_json = self.task.parents[1] / 'run.json'
        F.write(outside / 'run.json', json.loads(run_json.read_text()))
        run_json.unlink()
        run_json.symlink_to(outside / 'run.json')
        data = self.detail()
        self.assertEqual((data['available'], data['reason'], data['nodes']), (False, 'invalid run.json', []))
        self.assertEqual(reader.detail(self.plan)['fractal']['reason'], 'invalid run.json')

    def test_unusable_runs(self):
        run_json = self.task.parents[1] / 'run.json'
        config = json.loads(run_json.read_text())
        F.write(run_json, dict(config, runner='/somewhere/else'))
        data = self.detail()
        self.assertEqual((data['available'], data['reason'], data['state'], data['nodes']),
                         (False, 'runner mismatch', None, []))
        F.write(run_json, config)
        selection = self.plan / 'fractal-selection.json'
        F.write(selection, dict(enabled=True, run='run-../../etc'))
        self.assertEqual(reader.detail(self.plan)['fractal'], dict(enabled=True, run=None, available=False,
                                                                  reason='invalid run id'))
        self.assertIn('fractal-selection.json: invalid run id', reader.detail(self.plan)['warnings'])
        F.write(selection, dict(enabled=False))
        self.assertEqual(reader.detail(self.plan)['fractal'], dict(enabled=False, run=None, available=False,
                                                                  reason='Fractal is off for this run'))
        self.assertEqual(self.detail()['reason'], 'Fractal is off for this run')
        F.write(selection, '{"enabled": true,')
        self.assertEqual(reader.detail(self.plan)['fractal']['reason'], 'invalid fractal-selection.json')
        F.write(selection, ' ' * (70 * 1024) + '{}')
        self.assertEqual(reader.detail(self.plan)['fractal']['reason'], 'invalid fractal-selection.json')
        selection.unlink()
        self.assertIsNone(reader.detail(self.plan)['fractal'])
        self.assertEqual(self.detail()['reason'], 'Fractal is not selected for this run')
        F.write(selection, dict(enabled=True, run='run-' + '0' * 20))
        self.assertEqual(reader.detail(self.plan)['fractal']['reason'], 'managed run not found')
        # A task Fractal has not reached yet, and one whose request names another dir.
        F.write(selection, dict(enabled=True, run=fractal.run_id(self.plan)))
        self.assertEqual(self.detail('page')['reason'], 'no Fractal task for this task yet')
        request = json.loads((self.task / 'request.json').read_text())
        F.write(self.task / 'request.json', dict(request, output='/elsewhere'))
        self.assertEqual(self.detail()['reason'], 'request does not match this task')
        self.assertIsNone(self.detail('nope'))
        self.assertIsNone(reader.fractal_node(self.plan, 'reader', 'ui'))

    def test_malformed_and_oversized_files(self):
        nodes = self.task / 'nodes'
        (nodes / 'api/node.json').write_text('{"id": "api", ', encoding='utf-8')
        (nodes / 'ui/node.json').write_bytes(b'{"id": "ui", "goal": "' + b'x' * (4 * 1024 * 1024) + b'"}')
        (self.task / 'state.json').write_text('[1, 2]', encoding='utf-8')
        with (self.task / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write('not json\n{"at": NaN, "kind": "x"}\n')
        data = self.detail()
        self.assertEqual(data['state'], None)
        self.assertEqual([item['id'] for item in data['nodes'] if not item['reserved']], ['implementation'])
        self.assertEqual(sorted(data['warnings']), sorted([
            f'fractal {self.task.name}/nodes/api/node.json: not a JSON object',
            f'fractal {self.task.name}/nodes/ui/node.json: larger than 4096 KiB',
            f'fractal {self.task.name}/state.json: not a JSON object',
            f'fractal {self.task.name}/events.jsonl: 2 lines are not JSON objects']))
        # ui and api were admitted, so they are still listed, as reserved.
        self.assertEqual([item['id'] for item in data['nodes'] if item['reserved']], ['api', 'docs', 'ui'])
        F.write(nodes / 'implementation/node.json', dict(id='implementation', status=7, parent='x/y', paths='ui',
                                                         tokens=[1], cost='free',
                                                         decomposition=dict(admission=[dict(name='../x'), 'y'])))
        root = self.detail()['nodes'][0]
        self.assertEqual((root['status'], root['ui_status'], root['parent'], root['paths'], root['tokens'], root['cost'],
                          root['decomposition']['children']), (None, 'UNKNOWN', None, [], None, None, []))
        F.write(nodes / 'implementation/node.json', dict(id='implementation', status='planning_failed', error='e' * 500))
        root = self.detail()['nodes'][0]
        self.assertEqual((root['ui_status'], len(root['error'])), ('FAIL', 241))

    def test_many_events_keep_the_newest(self):
        with (self.task / 'events.jsonl').open('w', encoding='utf-8') as stream:
            for n in range(3000):
                stream.write(json.dumps(dict(at=F.T0 + n, kind='step_finished', node='ui', iteration=n)) + '\n')
        events = self.detail()['events']
        self.assertEqual((len(events), events[0]['iteration'], events[-1]['iteration']), (50, 2950, 2999))

    def test_state_root(self):
        with patch.dict(os.environ, {'XDG_STATE_HOME': '', 'HOME': str(self.base / 'home')}):
            # forge would resolve an empty XDG_STATE_HOME to its working dir; it counts as unset here.
            self.assertEqual(fractal.state_root(), str(self.base / 'home/.local/state/forge/fractal'))
            data = self.detail()
            self.assertEqual((data['available'], data['reason']), (False, 'managed run not found'))
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.state)}):
            self.assertEqual(fractal.state_root(), str(self.state / 'forge/fractal'))

    def test_solo_run(self):
        task = F.make_fractal(self.state, self.solo, 'solo')
        self.assertEqual(task.name, fractal.task_id(str(self.solo)))
        self.assertTrue(reader.detail(self.solo)['fractal']['available'])
        data = reader.fractal_detail(self.solo, 'solo')
        self.assertEqual((data['available'], len(data['nodes'])), (True, 4))
        self.assertIsNone(reader.fractal_detail(self.solo, 'reader'))
        os.chmod(task / 'control/locked', 0o700)


class FractalEndpointTests(FractalCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(reader.Catalog(plans=[self.plan, self.solo], registry=False), port=0, quiet=True)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join()
        self.addCleanup(stop)
        self.key = reader.run_key(self.plan)

    def request(self, path, method='GET', headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        try:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def test_routes(self):
        base = f'/api/runs/{self.key}/tasks/reader/fractal'
        for path, expected in ((base, self.detail()),
                               (base + '/nodes/ui', reader.fractal_node(self.plan, 'reader', 'ui')),
                               (base + '/nodes/ui/diff', reader.fractal_node_diff(self.plan, 'reader', 'ui'))):
            status, body = self.request(path)
            self.assertEqual((status, json.loads(body)), (200, expected), path)
            self.assertEqual(self.request(path, 'HEAD'), (200, b''), path)
            self.assertEqual(self.request(path, headers={'Host': 'evil.example'})[0], 403, path)
        status, body = self.request(f'/api/runs/{self.key}/tasks/page/fractal')
        self.assertEqual((status, json.loads(body)['available']), (200, False))
        for path in (f'/api/runs/{self.key}/tasks/nope/fractal', f'/api/runs/{self.key}/tasks/%2e%2e/fractal',
                     base + '/nodes/nope', base + '/nodes/..', base + '/nodes/a%2Fb', base + '/nodes/ui/x',
                     base + '/nodes', base + '/nodes/', f'/api/runs/{"0" * 12}/tasks/reader/fractal',
                     f'/api/runs/{self.key}/tasks/page/fractal/nodes/ui'):
            status, body = self.request(path)
            self.assertEqual((status, json.loads(body)), (404, {'error': 'not found'}), path)


if __name__ == '__main__':
    unittest.main()
