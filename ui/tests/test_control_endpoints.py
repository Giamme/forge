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
               {'fingerprints': [FP], 'labels': ['a\nb']}, {'fingerprints': [FP], 'labels': ['x' * 51]}, {'fingerprints': [FP], 'labels': ['\ud800']},
               {'fingerprints': [FP], 'labels': ['-wip']}, {'fingerprints': [FP], 'labels': ['   ']},
               {'fingerprints': [FP], 'labels': ['a'] * 6}, {'fingerprints': [FP], 'labels': [3]},
               {'fingerprints': [FP], 'scope': 'all'}, {'fingerprints': [FP], 'include_plausible': 'yes'},
               {'fingerprints': [FP], 'repo': '/tmp'}]
        for body in bad:
            self.assertEqual(self.post(body, self.issues())[0], 400, body)
        # One rule each, so the detail names the rule that body actually breaks.
        named = [(['-wip'], '-'), (['   '], 'blank'), (['x' * 51], '50'), (['a\nb'], 'control')]
        for labels, word in named:
            status, body = self.post({'fingerprints': [FP], 'labels': labels}, self.issues())
            self.assertEqual(status, 400, labels)
            self.assertIn(word, body['detail'], labels)
        self.assertEqual(self.calls(), [])

    def test_dismiss_and_undo_argv(self):
        path = self.issues('/dismiss')
        self.post({'fingerprints': [FP, FP2], 'reason': 'not a bug'}, path)
        self.assertEqual(self.last_argv(), ['issues', self.rplan, '--json', '--dismiss', f'{FP},{FP2}',
                                            '--reason=not a bug'])
        self.post({'fingerprints': [FP], 'reason': '-dup'}, path)
        self.assertEqual(self.last_argv()[-1], '--reason=-dup')
        self.post({'fingerprints': [FP], 'reason': 'dup\tof #3'}, path)
        self.assertEqual(self.last_argv()[-1], '--reason=dup\tof #3')
        self.post({'fingerprints': [FP], 'undo': True}, path)
        self.assertEqual(self.last_argv(), ['issues', self.rplan, '--json', '--undismiss', FP])

    def test_dismiss_refusals(self):
        path = self.issues('/dismiss')
        for body in ({'fingerprints': ['nope']}, {'fingerprints': [FP], 'reason': 'x' * 501},
                     {'fingerprints': [FP], 'reason': 5}, {'fingerprints': [FP], 'undo': 1},
                     {'fingerprints': [FP], 'reason': '\ud800'}, {'fingerprints': [FP], 'reason': 'a\x00b'},
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


class ReviewerOutcomesTests(EndpointCase):
    def test_reads_the_dataset_report_through_the_cli_and_takes_no_parameters(self):
        self.behave(stdout='{"reviewers": []}')
        self.assertEqual(self.get('/api/models/reviewers'), (200, {'reviewers': []}))
        self.assertEqual(self.last_argv(), ['dataset', 'reviewers', '--json'])
        self.assertEqual(self.get('/api/models/reviewers?repo=/etc')[0], 400)
        self.assertEqual(len(self.calls()), 1)


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
        self.cfg(key='decide.gate.drift', value='shadow')
        self.assertEqual(self.last_argv(),
                         ['config', 'set', 'decide.gate.drift', 'shadow', '--repo', self.rrepo, '--json'])
        self.cfg(key='decide.gate.forward.provider', value='box')
        self.assertEqual(self.last_argv(),
                         ['config', 'set', 'decide.gate.forward.provider', 'box', '--repo', self.rrepo, '--json'])
        self.cfg(key='decide.gate.prompt.model', value='m' * 256)
        self.assertEqual(self.last_argv(),
                         ['config', 'set', 'decide.gate.prompt.model', 'm' * 256, '--repo', self.rrepo, '--json'])
        self.cfg(key='repo.verify', value='x' * 300)
        self.assertEqual(self.last_argv()[3], 'x' * 300)
        self.cfg(key='repo.verify-subset', value='pytest {files}')
        self.assertEqual(self.last_argv()[3], 'pytest {files}')

    def test_decide_names_match_forge_decide(self):
        scripts = str(ROOT / 'scripts')
        sys.path.insert(0, scripts)
        try:
            import forge_decide
        finally:
            sys.path.remove(scripts)
        self.assertEqual(control.DECIDE_GATES, forge_decide.GATES)
        self.assertEqual(control.DECIDE_MODES, forge_decide.MODES)

    def test_config_without_the_token_is_refused(self):
        status, _body = self.post({'run': self.key, 'key': 'decide.gate.drift', 'value': 'on'},
                                  '/api/config', **{'X-Forge-UI-Token': None})
        self.assertEqual(status, 403)
        self.assertEqual(self.calls(), [])

    def test_refusals(self):
        bad = [dict(key='repo.other', value='x'), dict(key='repo.verify'), dict(key='repo.verify', value=''),
               dict(key='repo.verify', value='x', unset=True), dict(key='repo.verify', unset=False),
               dict(key='jev.enabled', value='true'), dict(key='jev.enabled', unset=True),
               dict(key='decide.gate.routing', value='on'), dict(key='decide.gate.drift', value='maybe'),
               dict(key='decide.gate.drift', value='off\n'), dict(key='decide.gate.drift', unset=True),
               dict(key='decide.gate.drift.provider', unset=True),
               dict(key='decide.gate.drift.provider', value='-box'),
               dict(key='decide.gate.drift.provider', value='a\nb'),
               dict(key='decide.gate.drift.model', value='m' * 257),
               dict(key='decide.gate.drift.model', value=' ok'),
               dict(key='decide.gate.drift.model', value='a\ud800b'),
               dict(key='decide.gate.drift.provider.extra', value='box'),
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


class RouteNameTests(EndpointCase):
    """A refused request is logged under its own route and run, as api.md promises."""

    def named(self, method, path, body, query=''):
        return control.dispatch(self.server, self.fake, method, path, body, query=query)[2:]

    def test_refused_requests_keep_their_route_and_key(self):
        k = self.key
        cases = [(('POST', f'/api/runs/{k}/issues', {}), ('issues', k)),
                 (('POST', f'/api/runs/{k}/issues/dismiss', {}), ('dismiss', k)),
                 (('POST', f'/api/runs/{k}/issues/dismiss', {'undo': True}), ('undismiss', k)),
                 (('POST', f'/api/runs/{k}/delete', {'dry_run': 1}), ('delete', k)),
                 (('POST', '/api/config', {'run': k, 'key': 'nope'}), ('config', k)),
                 (('GET', '/api/config', None, f'run={k}&repo=/x'), ('config', k)),
                 (('GET', f'/api/runs/{k}/issues', None, 'scope=x'), ('issues', k))]
        for args, expected in cases:
            self.assertEqual(self.named(*args), expected, args)
        self.assertEqual(self.calls(), [])


class RealCliTest(ControlApiCase):
    """The real `forge runs rm --dry-run --json`, through the HTTP server, on a temp plan copy."""

    def setUp(self):
        super().setUp()
        home = self.base / 'home'
        home.mkdir()
        env = {'HOME': str(home), 'XDG_CONFIG_HOME': str(self.base / 'config'), 'XDG_STATE_HOME': str(self.base / 'state'),
               'XDG_DATA_HOME': str(self.base / 'data'), 'FORGE_DATA_HOME': str(self.base / 'data'),
               'FORGE_DATASET': 'off'}
        for patcher in (patch.dict(os.environ, env), patch.object(ui_server, 'forge_cli', ROOT / 'forge')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_delete_dry_run_through_the_api(self):
        status, body = self.post({'dry_run': True}, f'/api/runs/{self.key}/delete')
        self.assertEqual(status, 200, body)
        self.assertTrue(self.plan.is_dir(), 'a dry run changes nothing')
        self.assertTrue(body['dry_run'] and body['ok'])
        self.assertEqual([step['step'] for step in body['steps']][-1], 'unregister')
        self.assertEqual(self.post({'dry_run': True}, f'/api/runs/{self.key}/delete', **{'X-Forge-UI-Token': 'wrong'})[0], 403)


if __name__ == '__main__':
    unittest.main()
