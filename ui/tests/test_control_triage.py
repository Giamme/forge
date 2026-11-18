"""Triage routes and GET /api/specs/check: forge findings, and doctor for a spec (#161)."""
import contextlib
import io
import json
import os
import threading
import time
import urllib.parse
from unittest.mock import patch

from forge_ui import control
from test_control_actions import ActionCase, FakeProjects

FP = 'a1b2c3d4e5f6'
STATE = {'triage': 1, 'state': 'done', 'spec': 'sol:high:codex', 'findings': 3, 'proposals': 1}
PROPOSALS = {'triage': 1, 'spec': 'sol:high:codex', 'at': 't',
             'proposals': [{'id': 'p1', 'hash': FP, 'state': 'pending', 'kind': 'duplicate'}]}
DOCTOR_OK = '\n'.join([
    'role=side',
    'alias=sol',
    'harness=codex',
    'model=gpt-5',
    'effort=high',
    'yolo=off',
    'offline preflight passed; authentication, quota and live model availability unverified',
]) + '\n'
RESOLVED = {'role': 'side', 'alias': 'sol', 'harness': 'codex', 'model': 'gpt-5', 'effort': 'high', 'yolo': 'off'}


class TriageRouteTests(ActionCase):
    PID = '0123abcd'

    def setUp(self):
        super().setUp()
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.other = self.base / 'other'
        self.other.mkdir()
        # The root exists, and a checkout exists too. The identity root is what the CLI must be given.
        self.screen = self.server.project_screen = FakeProjects({
            self.PID: {'id': self.PID, 'root': str(self.root), 'name': 'repo',
                       'runs': [{'repo_worktree': str(self.other)}]},
        })
        self.path = f'/api/projects/{self.PID}/triage'
        self.apply = f'/api/projects/{self.PID}/triage/1/p1/apply'
        self.reject = f'/api/projects/{self.PID}/triage/1/p2/reject'

    def get(self, path, server=None):
        status, _headers, data = self.send('GET', path, server=server)
        return status, json.loads(data)

    def decision(self, kind, n='1', pid='p1'):
        return f'/api/projects/{self.PID}/triage/{n}/{pid}/{kind}'

    def two_cli(self, path, first_stdout, second):
        """One GET that runs two CLI calls. The first sleeps, so `second` is in place before it starts."""
        box = {}
        before = len(self.calls())
        self.behave(stdout=first_stdout, sleep=2)

        def run():
            try:
                box['result'] = self.get(path)
            except Exception as exc:  # the thread would otherwise swallow it
                box['error'] = exc

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 10
        while len(self.calls()) <= before and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.calls()), before + 1, 'the first CLI call did not start on its own')
        self.behave(**second)
        worker.join(timeout=15)
        self.assertFalse(worker.is_alive(), 'the request did not finish')
        if box.get('error') is not None:
            raise box['error']
        self.behave(stdout='{"ok": true}')
        return box['result'], [c['argv'] for c in self.calls()[before:]]

    def test_start_argv_uses_the_root_and_returns_at_once(self):
        root = str(self.root)
        started = {'triage': 3, 'state': 'running'}
        self.behave(stdout=json.dumps(started))
        self.assertEqual(self.post({'spec': 'sol:high:codex'}, self.path), (200, started))
        self.assertEqual(self.last_argv(),
                         ['findings', 'triage', root, '--spec', 'sol:high:codex', '--detach', '--actor', 'ui', '--json'])
        self.assertNotIn(str(self.other), self.last_argv())

        specials = 'Az9._:/@+-'
        self.post({'spec': specials, 'max_findings': 1}, self.path)
        self.assertEqual(self.last_argv(),
                         ['findings', 'triage', root, '--spec', specials, '--max-findings', '1',
                          '--detach', '--actor', 'ui', '--json'])
        self.post({'spec': 'a' * 120, 'max_findings': 2000}, self.path)
        self.assertEqual(self.last_argv()[3:7], ['--spec', 'a' * 120, '--max-findings', '2000'])
        # A leading '-' is the equals form, so the CLI does not read the spec as a flag.
        self.post({'spec': '-sol'}, self.path)
        self.assertEqual(self.last_argv()[3], '--spec=-sol')
        self.assertEqual(self.last_argv()[4:], ['--detach', '--actor', 'ui', '--json'])

    def test_start_rejects_a_bad_body_before_the_cli(self):
        bad = [
            {},
            {'spec': ''},
            {'spec': 'a' * 121},
            {'spec': 'a b'},
            {'spec': 'a*b'},
            {'spec': 'a=b'},
            {'spec': 'a\nb'},
            {'spec': 'café'},
            {'spec': 1},
            {'spec': None},
            {'max_findings': 10},
            {'spec': 'sol', 'max_findings': 0},
            {'spec': 'sol', 'max_findings': 2001},
            {'spec': 'sol', 'max_findings': -1},
            {'spec': 'sol', 'max_findings': True},
            {'spec': 'sol', 'max_findings': 1.5},
            {'spec': 'sol', 'max_findings': '4'},
            {'spec': 'sol', 'timeout': 30},
        ]
        for body in bad:
            self.assertEqual(self.post(body, self.path)[0], 400, body)
        self.assertEqual(self.calls(), [])

    def test_a_refused_start_is_409_with_the_cli_message(self):
        self.behave(code=3, stderr='forge findings: a triage is running: 2\n', stdout='')
        status, answer = self.post({'spec': 'sol:high:codex'}, self.path)
        self.assertEqual((status, answer['error']), (409, 'refused'))
        self.assertIn('a triage is running: 2', answer['detail'])
        self.assertIn('--detach', self.last_argv())

    def test_status_is_not_cached_and_passes_n(self):
        state = {'triage': 2, 'state': 'running', 'spec': 'sol:high:codex'}
        self.behave(stdout=json.dumps(state))
        root = str(self.root)
        for _ in range(2):
            self.assertEqual(self.get(self.path + '?n=2'), (200, {'state': state, 'proposals': state}))
        self.assertEqual([c['argv'] for c in self.calls()], [
            ['findings', 'triage-status', root, '2', '--json'],
            ['findings', 'proposals', root, '2', '--json'],
        ] * 2)
        self.assertNotIn(str(self.other), self.calls()[0]['argv'])

    def test_a_bad_status_query_never_calls_the_cli(self):
        for query in ('?n=', '?n=2a', '?n=-1', '?n=1.2', '?n=1&n=2', '?refresh=1', '?n=2&refresh=1', '?n'):
            self.assertEqual(self.get(self.path + query)[0], 400, query)
        self.assertEqual(self.calls(), [])

    def test_no_triage_yet_is_an_empty_200_and_another_refusal_stays_409(self):
        root = str(self.root)
        self.behave(code=3, stderr='forge findings: no triage has run for this repository\n', stdout='')
        self.assertEqual(self.get(self.path), (200, {'state': None, 'proposals': None}))
        self.assertEqual(self.last_argv(), ['findings', 'triage-status', root, '--json'])

        self.behave(code=3, stderr='forge findings: no triage 4\n', stdout='')
        self.assertEqual(self.get(self.path + '?n=4'), (200, {'state': None, 'proposals': None}))
        self.assertEqual(self.last_argv(), ['findings', 'triage-status', root, '4', '--json'])

        before = len(self.calls())
        self.behave(code=3, stderr='forge findings: triage 4 has no state\n', stdout='')
        status, body = self.get(self.path + '?n=4')
        self.assertEqual((status, body['error']), (409, 'refused'))
        self.assertIn('has no state', body['detail'])
        self.assertEqual(len(self.calls()) - before, 1)  # proposals is not asked when status itself was refused

    def test_status_and_proposals_are_the_two_cli_results(self):
        root = str(self.root)
        path = self.path + '?n=0'
        got, argvs = self.two_cli(path, json.dumps(STATE), {'stdout': json.dumps(PROPOSALS)})
        self.assertEqual(got, (200, {'state': STATE, 'proposals': PROPOSALS}))
        self.assertEqual(argvs, [
            ['findings', 'triage-status', root, '0', '--json'],
            ['findings', 'proposals', root, '0', '--json'],
        ])

        got, argvs = self.two_cli(self.path + '?n=3', json.dumps(STATE), {
            'code': 3, 'stderr': 'forge findings: triage 3 has no proposals (running)\n', 'stdout': ''})
        self.assertEqual(got, (200, {'state': STATE, 'proposals': None}))
        self.assertEqual(argvs, [
            ['findings', 'triage-status', root, '3', '--json'],
            ['findings', 'proposals', root, '3', '--json'],
        ])

    def test_apply_and_reject_argv(self):
        root = str(self.root)
        plan = str(self.base / 'plan-out')
        decided = {'proposal': {'id': 'p1'}, 'changed': [FP]}
        self.behave(stdout=json.dumps(decided))
        self.assertEqual(self.post({'hash': FP}, self.apply), (200, decided))
        self.assertEqual(self.last_argv(),
                         ['findings', 'apply', root, '1', 'p1', '--approve', FP, '--actor', 'ui', '--json'])

        self.post({'hash': FP, 'plan_dir': plan}, self.decision('apply', '0', 'p0'))
        self.assertEqual(self.last_argv(),
                         ['findings', 'apply', root, '0', 'p0', '--approve', FP, '--plan-dir', plan,
                          '--actor', 'ui', '--json'])
        self.post({'hash': FP}, self.decision('apply', '12', 'p9999'))
        self.assertEqual(self.last_argv()[2:6], [root, '12', 'p9999', '--approve'])

        self.post({}, self.reject)
        self.assertEqual(self.last_argv(),
                         ['findings', 'reject', root, '1', 'p2', '--actor', 'ui', '--json'])
        self.post({'reason': 'n' * 500}, self.decision('reject', '12', 'p9999'))
        self.assertEqual(self.last_argv(),
                         ['findings', 'reject', root, '12', 'p9999', '--reason', 'n' * 500, '--actor', 'ui', '--json'])
        self.post({'reason': '-not this'}, self.reject)
        self.assertIn('--reason=-not this', self.last_argv())
        self.assertNotIn(str(self.other), self.last_argv())

    def test_apply_and_reject_reject_bad_input_before_the_cli(self):
        bad_apply = [
            {},
            {'hash': ''},
            {'hash': 'A1B2C3D4E5F6'},
            {'hash': 'a' * 11},
            {'hash': 'a' * 13},
            {'hash': 'g1b2c3d4e5f6'},
            {'hash': FP, 'plan_dir': 'rel/plan'},
            {'hash': FP, 'plan_dir': '../out'},
            {'hash': FP, 'plan_dir': ''},
            {'hash': FP, 'plan_dir': None},
            {'hash': FP, 'extra': 1},
        ]
        for body in bad_apply:
            self.assertEqual(self.post(body, self.apply)[0], 400, body)
        for path in (self.decision('apply', 'x', 'p1'), self.decision('apply', '1', 'p12345'),
                     self.decision('apply', '1', 'P1'), self.decision('apply', '-1', 'p1')):
            self.assertEqual(self.post({'hash': FP}, path)[0], 400, path)
        for path in (self.decision('reject', '1', 'p'), self.decision('reject', '1', 'px'),
                     self.decision('reject', '1a', 'p1')):
            self.assertEqual(self.post({'reason': 'no'}, path)[0], 400, path)
        for body in ({'reason': 'r' * 501}, {'reason': 1}, {'reason': 'no', 'extra': 1}):
            self.assertEqual(self.post(body, self.reject)[0], 400, body)
        self.assertEqual(self.calls(), [])

    def test_a_refused_decision_is_409_and_a_held_lock_is_busy(self):
        root = str(self.root)
        self.screen.cli[('issues', root, True)] = (time.time(), {'findings': []})
        self.screen.runs['/stale'] = (1.0, [])
        self.behave(code=3, stderr='forge findings: wrong hash\n', stdout='')
        status, answer = self.post({'hash': FP}, self.apply)
        self.assertEqual((status, answer['error']), (409, 'refused'))
        self.assertIn('wrong hash', answer['detail'])
        self.behave(code=3, stderr='forge findings: not pending\n', stdout='')
        status, answer = self.post({'reason': 'no'}, self.reject)
        self.assertEqual((status, answer['error']), (409, 'refused'))
        self.assertIn('not pending', answer['detail'])
        self.assertIn(('issues', root, True), self.screen.cli)
        self.assertEqual(self.screen.runs, {'/stale': (1.0, [])})

        self.behave(stdout='{"ok": true}')
        lock = control._server_lock(self.server, 'findings_lock')
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertEqual(self.post({'hash': FP}, self.apply), (409, {'error': 'busy'}))
            self.assertEqual(self.post({}, self.reject), (409, {'error': 'busy'}))
            # Start does not take the findings lock, and it does not drop the cache.
            self.assertEqual(self.post({'spec': 'sol'}, self.path)[0], 200)
        finally:
            lock.release()
        self.assertEqual([c['argv'][1] for c in self.calls()], ['apply', 'reject', 'triage'])
        self.assertIn(('issues', root, True), self.screen.cli)
        self.assertEqual(self.screen.runs, {'/stale': (1.0, [])})

    def test_a_decision_drops_the_findings_cache(self):
        root = str(self.root)
        now = time.time()
        self.screen.cli[('issues', root, True)] = (now, {'findings': []})
        self.screen.cli[('issues', root, False)] = (now, {'findings': []})
        self.screen.cli[('memory', root, ())] = (now, {'memory': 'x'})
        self.screen.runs['/stale'] = (1.0, [])
        self.assertEqual(self.post({'hash': FP}, self.apply)[0], 200)
        self.assertEqual(set(self.screen.cli), {('memory', root, ())})
        self.assertEqual(self.screen.runs, {})

        self.screen.cli[('issues', root, True)] = (now, {'findings': []})
        self.screen.runs['/stale'] = (1.0, [])
        self.assertEqual(self.post({'reason': 'no'}, self.reject)[0], 200)
        self.assertEqual(set(self.screen.cli), {('memory', root, ())})
        self.assertEqual(self.screen.runs, {})

    def test_a_missing_root_falls_back_to_a_checkout_and_none_is_404(self):
        checkout = self.base / 'checkout'
        checkout.mkdir()
        gone = str(self.base / 'gone')
        self.screen.found[self.PID] = {
            'root': gone,
            'runs': [{'repo_worktree': str(self.base / 'missing')}, {'repo_worktree': str(checkout)}],
        }
        root = str(checkout)
        self.post({'spec': 'sol'}, self.path)
        self.assertEqual(self.last_argv()[2], root)
        self.get(self.path + '?n=1')
        self.assertEqual([c['argv'] for c in self.calls()[-2:]], [
            ['findings', 'triage-status', root, '1', '--json'],
            ['findings', 'proposals', root, '1', '--json'],
        ])
        self.post({'hash': FP}, self.apply)
        self.assertEqual(self.last_argv()[2], root)
        self.post({}, self.reject)
        self.assertEqual(self.last_argv()[2], root)
        self.assertNotIn(gone, [part for c in self.calls() for part in c['argv']])

        before = len(self.calls())
        self.screen.found[self.PID] = {'root': gone, 'runs': [{'repo_worktree': str(self.base / 'missing')}]}
        for status, body in (self.post({'spec': 'sol'}, self.path), self.get(self.path),
                             self.post({'hash': FP}, self.apply), self.post({}, self.reject)):
            self.assertEqual((status, body), (404, {'error': 'no checkout'}))
        unknown = 'ffffffff'
        self.assertEqual(self.post({'spec': 'sol'}, f'/api/projects/{unknown}/triage'), (404, {'error': 'not found'}))
        self.assertEqual(self.get(f'/api/projects/{unknown}/triage'), (404, {'error': 'not found'}))
        self.assertEqual(self.post({'hash': FP}, f'/api/projects/{unknown}/triage/1/p1/apply'),
                         (404, {'error': 'not found'}))
        self.assertEqual(len(self.calls()), before)

    def test_read_only_serves_gets_and_refuses_posts(self):
        self.behave(code=3, stderr='forge findings: no triage has run for this repository\n', stdout='')
        ro = self.start(read_only=True)
        ro.project_screen = self.screen
        self.assertEqual(self.get(self.path, server=ro), (200, {'state': None, 'proposals': None}))
        self.assertEqual(self.last_argv(), ['findings', 'triage-status', str(self.root), '--json'])
        before = len(self.calls())
        self.assertEqual(self.post({'spec': 'sol:high:codex'}, self.path, server=ro)[0], 405)
        self.assertEqual(self.post({'hash': FP}, self.apply, server=ro)[0], 405)
        self.assertEqual(self.post({'reason': 'no'}, self.reject, server=ro)[0], 405)
        self.assertEqual(len(self.calls()), before)

    def test_spec_check_parses_doctor_and_a_refusal(self):
        seen = []

        def fake_run(argv, timeout, stdin=None):
            seen.append((argv, timeout, stdin))
            spec = argv[argv.index('--spec') + 1]
            if spec == 'spawn:fail':
                return control.CliResult(error='cannot run the forge CLI: No such file')
            if spec == 'nope:high:missing':
                return control.CliResult(code=3, stdout='role=side\n', stderr='forge: harness is not installed\n')
            # A key=value line on stderr is a note, not part of the resolved block.
            return control.CliResult(code=0, stdout=DOCTOR_OK, stderr='secret=from-stderr\n')

        with patch.object(control, 'run_cli', fake_run):
            status, body = self.get('/api/specs/check?spec=sol:high:codex')
            self.assertEqual((status, body), (200, {'ok': True, 'resolved': RESOLVED}))
            argv, timeout, stdin = seen[-1]
            self.assertEqual(argv[0], 'bash')
            self.assertEqual(argv[2:], ['doctor', '--spec', 'sol:high:codex', '--role', 'side'])
            self.assertTrue(os.path.isfile(argv[1]))
            self.assertTrue(argv[1].endswith(os.path.join('scripts', 'forge-dispatch.sh')))
            self.assertEqual((timeout, stdin), (control.TIMEOUT_S, None))

            self.assertEqual(self.get('/api/specs/check?spec=nope:high:missing'),
                             (200, {'ok': False, 'error': 'forge: harness is not installed'}))
            self.assertEqual(self.get('/api/specs/check?spec=spawn:fail')[0], 502)

            for spec in ('Az9._:/@+-', 'a' * 120, 'a+b', '-sol'):
                quoted = urllib.parse.quote(spec, safe='')
                status, body = self.get('/api/specs/check?spec=' + quoted)
                self.assertEqual(status, 200, spec)
                self.assertTrue(body['ok'], spec)
                self.assertEqual(seen[-1][0][seen[-1][0].index('--spec') + 1], spec)
            # A raw '+' in the query is a plus in the spec, not a space.
            self.assertEqual(self.get('/api/specs/check?spec=a+b')[0], 200)
            self.assertEqual(seen[-1][0][seen[-1][0].index('--spec') + 1], 'a+b')

            before = len(seen)
            for spec in ('', 'a' * 121, 'a b', 'a*b', 'a=b', 'a\nb', 'café', 'a%b'):
                status, _body = self.get('/api/specs/check?spec=' + urllib.parse.quote(spec, safe=''))
                self.assertEqual(status, 400, spec)
            self.assertEqual(self.get('/api/specs/check')[0], 400)
            self.assertEqual(self.get('/api/specs/check?spec=sol&x=1')[0], 400)
            self.assertEqual(len(seen), before)

            ro = self.start(read_only=True)
            self.assertEqual(self.get('/api/specs/check?spec=sol', server=ro),
                             (200, {'ok': True, 'resolved': RESOLVED}))
        self.assertEqual(self.calls(), [])

    def test_refused_without_authentication_and_read_only(self):
        self.refused_everywhere(self.path, {'spec': 'sol'})
        self.refused_everywhere(self.apply, {'hash': FP})
        self.refused_everywhere(self.reject, {'reason': 'no'})

    def test_each_post_logs_one_control_line(self):
        server = self.start(quiet=False)
        server.project_screen = self.screen
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.assertEqual(self.post({'spec': 'sol'}, self.path, server=server)[0], 200)
            self.assertEqual(self.post({'hash': FP}, self.apply, server=server)[0], 200)
            self.assertEqual(self.post({'reason': 'no'}, self.reject, server=server)[0], 200)
        lines = [line for line in buf.getvalue().splitlines() if line.startswith('forge-ui: control ')]
        self.assertEqual(lines, [
            f'forge-ui: control triage {self.PID} -> 200',
            f'forge-ui: control triage apply {self.PID} -> 200',
            f'forge-ui: control triage reject {self.PID} -> 200',
        ])


if __name__ == '__main__':
    import unittest
    unittest.main()
