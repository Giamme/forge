"""The issues, dismiss, delete and config write routes: argv, refusals, failures, no caching, the real CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from forge_ui import control, reader, server as ui_server
from test_control_api import ControlApiCase, FIXTURES, ROOT

FP = 'a1b2c3d4e5f6'
FP2 = '0123456789ab'


class EndpointCase(ControlApiCase):
    def setUp(self):
        super().setUp()
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.rplan = str(self.plan.resolve())
        self.rrepo = str(self.repo)
        (self.plan / 'repo').write_text(self.rrepo + '\n')
        self.behave(stdout='{"ok": true}')

    def get(self, path):
        status, _headers, data = self.send('GET', path)
        return status, json.loads(data)

    def last_argv(self):
        return self.calls()[-1]['argv']

    def issues(self, tail=''):
        return f'/api/runs/{self.key}/issues{tail}'


class IssuesListTests(EndpointCase):
    def test_argv_for_each_query(self):
        d = self.rplan
        cases = [('', ['issues', d, '--json']),
                 ('?scope=repo', ['issues', d, '--json', '--all-runs']),
                 ('?offline=1&plausible=1&dismissed=1',
                  ['issues', d, '--json', '--offline', '--include-plausible', '--include-dismissed']),
                 (f'?only={FP},{FP2}', ['issues', d, '--json', '--only', f'{FP},{FP2}'])]
        for query, argv in cases:
            status, body = self.get(self.issues(query))
            self.assertEqual((status, body), (200, {'ok': True}), query)
            self.assertEqual(self.last_argv(), argv, query)

    def test_bad_queries_never_reach_the_cli(self):
        many = ','.join(f'{n:012x}' for n in range(26))
        for query in ('?scope=all', '?offline=yes', f'?only={FP}x', '?only=', f'?only={many}', '?run=1',
                      '?scope=run&scope=repo'):
            self.assertEqual(self.get(self.issues(query))[0], 400, query)
        self.assertEqual(self.calls(), [])

    def test_unknown_run_and_busy_cli(self):
        self.assertEqual(self.get('/api/runs/000000000000/issues')[0], 404)
        self.behave(code=3, stderr='forge issues: busy\n', stdout='{"busy": true}')
        status, body = self.get(self.issues())
        self.assertEqual((status, body['error'], body['result']), (409, 'refused', {'busy': True}))

    def test_repeated_listings_are_each_their_own_call(self):
        for query in ('', f'?only={FP}', '?plausible=1'):
            self.get(self.issues(query))
        argvs = [call['argv'] for call in self.calls()]
        d = self.rplan
        self.assertEqual(argvs, [['issues', d, '--json'], ['issues', d, '--json', '--only', FP],
                                 ['issues', d, '--json', '--include-plausible']])


class IssuesWriteTests(EndpointCase):
    def test_file_argv(self):
        status, body = self.post({'scope': 'repo', 'fingerprints': [FP, FP2], 'include_plausible': True,
                                  'labels': ['bug', 'forge']}, self.issues())
        self.assertEqual((status, body), (200, {'ok': True}))
        self.assertEqual(self.last_argv(), ['issues', self.rplan, '--only', f'{FP},{FP2}', '--max-issues', '2',
                                            '--apply', '--json', '--all-runs', '--include-plausible',
                                            '--label', 'bug', '--label', 'forge'])
        self.post({'fingerprints': [FP]}, self.issues())
        self.assertEqual(self.last_argv(), ['issues', self.rplan, '--only', FP, '--max-issues', '1',
                                            '--apply', '--json'])

    def test_file_refusals(self):
        many = [f'{n:012x}' for n in range(26)]
        bad = [{'fingerprints': ['xyz']}, {'fingerprints': many}, {'fingerprints': []}, {},
               {'fingerprints': [FP], 'labels': ['a\nb']}, {'fingerprints': [FP], 'labels': ['x' * 51]},
               {'fingerprints': [FP], 'labels': ['a'] * 6}, {'fingerprints': [FP], 'labels': [3]},
               {'fingerprints': [FP], 'scope': 'all'}, {'fingerprints': [FP], 'include_plausible': 'yes'},
               {'fingerprints': [FP], 'repo': '/tmp'}]
        for body in bad:
            self.assertEqual(self.post(body, self.issues())[0], 400, body)
        self.assertEqual(self.calls(), [])

    def test_dismiss_and_undo_argv(self):
        path = self.issues('/dismiss')
        self.post({'fingerprints': [FP, FP2], 'reason': 'not a bug'}, path)
        self.assertEqual(self.last_argv(), ['issues', self.rplan, '--json', '--dismiss', f'{FP},{FP2}',
                                            '--reason', 'not a bug'])
        self.post({'fingerprints': [FP], 'undo': True}, path)
        self.assertEqual(self.last_argv(), ['issues', self.rplan, '--json', '--undismiss', FP])

    def test_dismiss_refusals(self):
        path = self.issues('/dismiss')
        for body in ({'fingerprints': ['nope']}, {'fingerprints': [FP], 'reason': 'x' * 501},
                     {'fingerprints': [FP], 'reason': 5}, {'fingerprints': [FP], 'undo': 1},
                     {'fingerprints': [FP], 'extra': 1}, {'fingerprints': [FP], 'reason': 'a', 'undo': True}):
            self.assertEqual(self.post(body, path)[0], 400, body)
        self.assertEqual(self.calls(), [])


class DeleteTests(EndpointCase):
    PATH = property(lambda self: f'/api/runs/{self.key}/delete')

    def test_argv(self):
        d = self.rplan
        cases = [({'dry_run': True, 'archive': True, 'worktrees': False, 'branches': False},
                  ['runs', 'rm', d, '--json', '--dry-run']),
                 ({'dry_run': False, 'archive': False, 'worktrees': True, 'branches': True},
                  ['runs', 'rm', d, '--json', '--yes', '--no-archive', '--worktrees', '--branches']),
                 ({}, ['runs', 'rm', d, '--json', '--yes'])]
        for body, argv in cases:
            self.assertEqual(self.post(body, self.PATH)[0], 200, body)
            self.assertEqual(self.last_argv(), argv, body)

    def test_refused_bodies(self):
        for body in ({'dry_run': 'no'}, {'archive': None}, {'path': '/'}):
            self.assertEqual(self.post(body, self.PATH)[0], 400, body)
        self.assertEqual(self.calls(), [])

    def test_failure_carries_the_steps(self):
        self.behave(code=4, stderr='forge runs rm: not completed\n', stdout=json.dumps({'steps': [{'name': 'archive', 'ok': True}]}))
        status, body = self.post({'dry_run': False}, self.PATH)
        self.assertEqual(status, 500)
        self.assertEqual(body['error'], 'failed')
        self.assertEqual(body['result']['steps'], [{'name': 'archive', 'ok': True}])
        self.behave(code=3, stdout='{"steps": []}')
        self.assertEqual(self.post({}, self.PATH)[0], 409)
        self.behave(code=2, stdout='{"steps": []}')
        self.assertEqual(self.post({}, self.PATH)[0], 400)

    def test_a_deleted_run_is_gone(self):
        # the fake CLI removes the run dir, as a real deletion would
        self.fake.write_text(self.fake.read_text().replace("time.sleep(behavior", f"__import__('shutil').rmtree({self.rplan!r}); time.sleep(behavior", 1))
        self.assertEqual(self.post({'dry_run': False}, self.PATH)[0], 200)
        self.assertEqual(self.post({'dry_run': False}, self.PATH)[0], 404)
        self.assertEqual(len(self.calls()), 1)

    def test_dir_lock_refuses_a_second_delete(self):
        locks = control._server_lock(self.server, 'dir_locks')
        self.assertTrue(locks.acquire(os.path.realpath(self.plan)))
        self.assertEqual(self.post({}, self.PATH), (409, {'error': 'busy'}))
        self.assertEqual(self.calls(), [])


class ConfigTests(EndpointCase):
    def cfg(self, **body):
        return self.post({'run': self.key, **body}, '/api/config')

    def test_show_and_set_argv(self):
        self.assertEqual(self.get(f'/api/config?run={self.key}')[0], 200)
        self.assertEqual(self.last_argv(), ['config', 'show', '--repo', self.rrepo, '--json'])
        self.cfg(key='repo.verify', value='make test')
        self.assertEqual(self.last_argv(), ['config', 'set', 'repo.verify', 'make test', '--repo', self.rrepo, '--json'])
        self.cfg(key='repo.setup', unset=True)
        self.assertEqual(self.last_argv(), ['config', 'unset', 'repo.setup', '--repo', self.rrepo, '--json'])
        self.cfg(key='jev.enabled', value='true')
        self.assertEqual(self.last_argv()[:4], ['config', 'set', 'jev.enabled', 'true'])
        self.cfg(key='repo.verify-subset', value='pytest {files}')
        self.assertEqual(self.last_argv()[3], 'pytest {files}')

    def test_refusals(self):
        bad = [dict(key='repo.other', value='x'), dict(key='repo.verify'), dict(key='repo.verify', value=''),
               dict(key='repo.verify', value='x', unset=True), dict(key='repo.verify', unset=False),
               dict(key='jev.enabled', value='maybe'), dict(key='jev.enabled', unset=True),
               dict(key='repo.verify-subset', value='pytest'), dict(key='repo.verify', value='-rf'),
               dict(key='repo.verify', value='a\x00b'), dict(key='repo.verify', value=3),
               dict(key='repo.verify', value='x', repo='/etc'), dict(key='repo.verify', value='x', path='/etc')]
        for body in bad:
            self.assertEqual(self.cfg(**body)[0], 400, body)
        self.assertEqual(self.post({'key': 'repo.verify', 'value': 'x'}, '/api/config')[0], 400)
        self.assertEqual(self.post({'run': '../x', 'key': 'repo.verify', 'value': 'x'}, '/api/config')[0], 400)
        self.assertEqual(self.get('/api/config?run=zz&repo=/etc')[0], 400)
        self.assertEqual(self.get('/api/config?repo=/etc')[0], 400)
        self.assertEqual(self.calls(), [])

    def test_unknown_run_or_repo(self):
        self.assertEqual(self.cfg(run='000000000000', key='repo.verify', value='x')[0], 404)
        (self.plan / 'repo').write_text('/no/such/dir\n')
        self.assertEqual(self.cfg(key='repo.verify', value='x')[0], 409)
        self.assertEqual(self.get(f'/api/config?run={self.key}')[0], 409)
        self.assertEqual(self.calls(), [])

    def test_config_lock_serializes(self):
        lock = control._server_lock(self.server, 'config_lock')
        lock.acquire()
        try:
            self.assertEqual(self.cfg(key='repo.verify', value='x'), (409, {'error': 'busy'}))
        finally:
            lock.release()
        self.assertEqual(self.calls(), [])


class RealCliTest(unittest.TestCase):
    def test_delete_dry_run_through_the_api(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            plan = base / 'plan'
            shutil.copytree(FIXTURES / 'plan-full', plan)
            home = base / 'home'
            home.mkdir()
            env = {'HOME': str(home), 'XDG_CONFIG_HOME': str(base / 'config'), 'XDG_STATE_HOME': str(base / 'state'),
                   'XDG_DATA_HOME': str(base / 'data'), 'FORGE_DATA_HOME': str(base / 'data')}
            with patch.dict(os.environ, env):
                catalog = reader.Catalog(plans=[plan], registry=False)
                ctx_server = type('S', (), {'catalog': catalog, 'run_locks': control.RunLocks()})()
                key = reader.run_key(plan)
                status, body, route, ran = control.dispatch(ctx_server, ROOT / 'forge', 'POST',
                                                            f'/api/runs/{key}/delete', {'dry_run': True})
            self.assertEqual(route, 'delete')
            self.assertTrue(plan.is_dir(), 'a dry run changes nothing')
            self.assertEqual(status, 200, body)
            self.assertTrue(body['dry_run'] and body['ok'])
            self.assertEqual([step['step'] for step in body['steps']][-1], 'unregister')


if __name__ == '__main__':
    unittest.main()
