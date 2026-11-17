"""POST /api/projects/<pid>/findings/<op>: change a finding by calling `forge findings` (#161)."""
import contextlib
import json
import os
import threading
import time
import unittest

from forge_ui import control
from test_control_actions import ActionCase, FakeProjects

FP = 'a1b2c3d4e5f6'
FP2 = '0123456789ab'
COMMIT = 'abc1234'
OPS = ('fix', 'dismiss', 'reopen', 'to-file', 'dup', 'tag', 'note', 'edit')


class FindingsActionTests(ActionCase):
    PID = '0123abcd'

    def setUp(self):
        super().setUp()
        self.root = self.base / 'repo'
        self.root.mkdir()
        self.screen = self.server.project_screen = FakeProjects({
            self.PID: {'id': self.PID, 'root': str(self.root), 'name': 'repo', 'runs': [],
                       'exists': True, 'live': 0, 'last': None, 'path_only': 0},
        })

    def path(self, op, pid=None):
        return f'/api/projects/{pid or self.PID}/findings/{op}'

    def get(self, path):
        status, _headers, data = self.send('GET', path)
        return status, json.loads(data)

    def body(self, op, **over):
        base = {
            'fix': {'fps': [FP], 'commit': COMMIT},
            'dismiss': {'fps': [FP], 'reason': 'noise'},
            'reopen': {'fps': [FP, FP2]},
            'to-file': {'fps': [FP], 'title': 'A draft'},
            'dup': {'fps': [FP], 'of': FP2},
            'tag': {'fps': [FP], 'tags': ['flaky', 'ui']},
            'note': {'fps': [FP], 'text': 'look again'},
            'edit': {'fps': [FP], 'severity': 'P2'},
        }[op]
        base.update(over)
        return base

    @contextlib.contextmanager
    def during_call(self, body, path):
        """Yield the CLI argv while the fake forge is still inside the call, so a temp file still exists."""
        box = {}
        previous = len(self.calls())
        self.behave(stdout='{"ok": true}', sleep=1.5)

        def run():
            try:
                box['result'] = self.post(body, path)
            except Exception as exc:  # the thread would otherwise swallow it
                box['error'] = exc

        worker = threading.Thread(target=run)
        worker.start()
        try:
            deadline = time.monotonic() + 10
            while len(self.calls()) <= previous and time.monotonic() < deadline:
                time.sleep(0.02)
            if len(self.calls()) <= previous:
                worker.join(timeout=5)
                self.fail(box.get('error') or box.get('result') or 'the CLI was never called')
            yield self.calls()[-1]['argv']
        finally:
            worker.join()
            self.behave(stdout='{"ok": true}')
        if box.get('error') is not None:
            raise box['error']
        self.assertEqual(box['result'][0], 200, box['result'])

    def test_a_removed_root_writes_through_a_checkout_still_on_disk_and_none_is_404(self):
        checkout = self.base / 'linked'
        checkout.mkdir()
        gone = str(self.base / 'gone')
        self.screen.found[self.PID] = {
            'id': self.PID, 'root': gone, 'name': 'repo', 'exists': False, 'live': 0, 'last': None, 'path_only': 0,
            'runs': [{'repo_worktree': str(self.base / 'missing')}, {'repo_worktree': str(checkout)}],
        }
        status, _ = self.post(self.body('tag'), self.path('tag'))
        self.assertEqual(status, 200)
        self.assertEqual(self.last_argv()[:3], ['findings', 'tag', str(checkout)])
        self.assertNotIn(gone, self.last_argv())
        before = len(self.calls())
        self.screen.found[self.PID]['runs'] = [{'repo_worktree': str(self.base / 'missing')}]
        self.assertEqual(self.post(self.body('tag'), self.path('tag')), (404, {'error': 'no checkout'}))
        self.assertEqual(len(self.calls()), before)

    def test_each_op_calls_forge_findings_for_the_project_root(self):
        root = str(self.root)
        actor = ['--actor', 'ui', '--json']
        cases = [
            ('fix', {'fps': [FP], 'commit': COMMIT},
             ['findings', 'fix', root, FP, '--commit', COMMIT, *actor]),
            ('fix', {'fps': [FP, FP2], 'reason': 'by hand'},
             ['findings', 'fix', root, FP, FP2, '--reason', 'by hand', *actor]),
            ('fix', {'fps': [FP], 'commit': COMMIT, 'reason': 'landed'},
             ['findings', 'fix', root, FP, '--commit', COMMIT, '--reason', 'landed', *actor]),
            ('dismiss', {'fps': [FP, FP2], 'reason': 'noise'},
             ['findings', 'dismiss', root, FP, FP2, '--reason', 'noise', *actor]),
            ('dismiss', {'fps': [FP], 'reason': '-not a bug'},
             ['findings', 'dismiss', root, FP, '--reason=-not a bug', *actor]),
            ('reopen', {'fps': [FP, FP2]},
             ['findings', 'reopen', root, FP, FP2, *actor]),
            ('to-file', {'fps': [FP, FP2], 'title': 'A draft'},
             ['findings', 'to-file', root, FP, FP2, '--title', 'A draft', *actor]),
            ('to-file', {'fps': [FP], 'title': '-bug'},
             ['findings', 'to-file', root, FP, '--title=-bug', *actor]),
            ('to-file', {'fps': [FP]},
             ['findings', 'to-file', root, FP, *actor]),
            ('dup', {'fps': [FP], 'of': FP2},
             ['findings', 'dup', root, FP, '--of', FP2, *actor]),
            ('tag', {'fps': [FP, FP2], 'tags': ['flaky', 'ui']},
             ['findings', 'tag', root, FP, FP2, '--', 'flaky', 'ui', '--add', *actor]),
            ('tag', {'fps': [FP], 'tags': ['flaky'], 'remove': False},
             ['findings', 'tag', root, FP, '--', 'flaky', '--add', *actor]),
            ('tag', {'fps': [FP], 'tags': ['flaky'], 'remove': True},
             ['findings', 'tag', root, FP, '--', 'flaky', '--remove', *actor]),
            ('note', {'fps': [FP], 'text': 'look again'},
             ['findings', 'note', root, FP, 'look again', *actor]),
            ('note', {'fps': [FP], 'text': ''},
             ['findings', 'note', root, FP, '', *actor]),
            ('note', {'fps': [FP], 'text': '-look'},
             ['findings', 'note', '--actor', 'ui', '--json', root, FP, '--', '-look']),
            ('edit', {'fps': [FP], 'severity': 'P0'},
             ['findings', 'edit', root, FP, '--severity', 'P0', *actor]),
            ('edit', {'fps': [FP], 'severity': 'P3'},
             ['findings', 'edit', root, FP, '--severity', 'P3', *actor]),
        ]
        for op, body, argv in cases:
            status, answer = self.post(body, self.path(op))
            self.assertEqual((status, answer, self.last_argv()), (200, {'ok': True}, argv), body)

    def test_bad_bodies_never_call_the_cli(self):
        bad = [
            ('fix', {'fps': ['xyz'], 'commit': COMMIT}),
            ('fix', {'fps': [FP.upper()], 'commit': COMMIT}),
            ('fix', {'fps': ['a' * 11], 'commit': COMMIT}),
            ('fix', {'fps': ['a' * 13], 'commit': COMMIT}),
            ('fix', {'fps': [1], 'commit': COMMIT}),
            ('fix', {'fps': [], 'commit': COMMIT}),
            ('fix', {'commit': COMMIT}),
            ('fix', {'fps': [f'{n:012x}' for n in range(201)], 'commit': COMMIT}),
            ('fix', {'fps': [FP]}),
            ('fix', {'fps': [FP], 'commit': 'abc123'}),
            ('fix', {'fps': [FP], 'commit': 'a' * 41}),
            ('fix', {'fps': [FP], 'commit': 'ABC1234'}),
            ('fix', {'fps': [FP], 'reason': 'x' * 501}),
            ('fix', {'fps': [FP], 'commit': COMMIT, 'extra': 1}),
            ('dismiss', {'fps': [FP]}),
            ('dismiss', {'fps': [FP], 'reason': 'x' * 501}),
            ('dismiss', {'fps': [FP], 'reason': 1}),
            ('dismiss', {'fps': [FP], 'reason': 'noise', 'extra': 1}),
            ('reopen', {}),
            ('reopen', {'fps': []}),
            ('reopen', {'fps': [FP], 'reason': 'no'}),
            ('to-file', {'fps': [FP], 'title': 't' * 201}),
            ('to-file', {'fps': [FP], 'body': 'b' * 20001}),
            ('to-file', {'fps': [FP], 'title': 't', 'extra': 1}),
            ('dup', {'fps': [FP, FP2], 'of': FP2}),
            ('dup', {'fps': [FP, FP], 'of': FP2}),
            ('dup', {'fps': [FP]}),
            ('dup', {'fps': [FP], 'of': 'not-a-fingerprint'}),
            ('dup', {'fps': [FP], 'of': FP2, 'extra': 1}),
            ('tag', {'fps': [FP], 'tags': ['Bad']}),
            ('tag', {'fps': [FP], 'tags': ['a_b']}),
            ('tag', {'fps': [FP], 'tags': ['1abc']}),
            ('tag', {'fps': [FP], 'tags': ['']}),
            ('tag', {'fps': [FP], 'tags': ['a' * 25]}),
            ('tag', {'fps': [FP], 'tags': [f't{n}' for n in range(9)]}),
            ('tag', {'fps': [FP], 'tags': []}),
            ('tag', {'fps': [FP], 'tags': 'flaky'}),
            ('tag', {'fps': [FP]}),
            ('tag', {'fps': [FP], 'tags': ['ok'], 'remove': 'yes'}),
            ('tag', {'fps': [FP], 'tags': ['ok'], 'extra': 1}),
            ('note', {'fps': [FP, FP2], 'text': 'hi'}),
            ('note', {'fps': [FP]}),
            ('note', {'fps': [FP], 'text': 'n' * 2001}),
            ('note', {'fps': [FP], 'text': 3}),
            ('note', {'fps': [FP], 'text': 'a\x00b'}),
            ('note', {'fps': [FP], 'text': 'hi', 'extra': 1}),
            ('edit', {'fps': [FP, FP2], 'severity': 'P1'}),
            ('edit', {'fps': [], 'severity': 'P1'}),
            ('edit', {'fps': [FP]}),
            ('edit', {'fps': [FP], 'severity': 'P4'}),
            ('edit', {'fps': [FP], 'severity': 'p1'}),
            ('edit', {'fps': [FP], 'text': 'e' * 4001}),
            ('edit', {'fps': [FP], 'severity': 'P1', 'extra': 1}),
        ]
        for op, body in bad:
            self.assertEqual(self.post(body, self.path(op))[0], 400, body)
        self.assertEqual(self.calls(), [])

    def test_limits_at_the_boundary_are_accepted(self):
        root = str(self.root)
        fps = [f'{n:012x}' for n in range(200)]
        self.assertEqual(self.post({'fps': fps, 'commit': 'a' * 40}, self.path('fix'))[0], 200)
        self.assertEqual(self.last_argv(),
                         ['findings', 'fix', root, *fps, '--commit', 'a' * 40, '--actor', 'ui', '--json'])
        reason = 'r' * 500
        self.assertEqual(self.post({'fps': [FP], 'reason': reason}, self.path('dismiss'))[0], 200)
        self.assertEqual(self.last_argv(),
                         ['findings', 'dismiss', root, FP, '--reason', reason, '--actor', 'ui', '--json'])
        tags = ['a', 'a-b', 'b' * 24, 'c0', 'd-1', 'e', 'f', 'g']
        self.assertEqual(self.post({'fps': [FP], 'tags': tags}, self.path('tag'))[0], 200)
        self.assertEqual(self.last_argv(),
                         ['findings', 'tag', root, FP, '--', *tags, '--add', '--actor', 'ui', '--json'])
        note = 'n' * 2000
        self.assertEqual(self.post({'fps': [FP], 'text': note}, self.path('note'))[0], 200)
        self.assertEqual(self.last_argv(),
                         ['findings', 'note', root, FP, note, '--actor', 'ui', '--json'])
        title = 't' * 200
        self.assertEqual(self.post({'fps': [FP], 'title': title}, self.path('to-file'))[0], 200)
        self.assertEqual(self.last_argv(),
                         ['findings', 'to-file', root, FP, '--title', title, '--actor', 'ui', '--json'])
        for name, text, flag in (('to-file', 'b' * 20000, '--body-file'), ('edit', 'e' * 4000, '--text-file')):
            body = {'fps': [FP], 'body': text} if name == 'to-file' else {'fps': [FP], 'text': text}
            self.assertEqual(self.post(body, self.path(name))[0], 200, name)
            argv = self.last_argv()
            self.assertIn(flag, argv, name)
            self.assertNotIn(text, argv, name)
            path = argv[argv.index(flag) + 1]
            self.assertTrue(os.path.isabs(path), path)
            self.assertFalse(os.path.exists(path), path)
            self.assertFalse(os.path.isdir(os.path.dirname(path)), path)

    def test_body_and_edit_text_are_passed_by_file_and_the_file_is_removed(self):
        draft = 'draft line\nsecond — café'
        edited = 'edited\nline — café'
        with self.during_call({'fps': [FP], 'title': 'Hello', 'body': draft}, self.path('to-file')) as argv:
            self.assertEqual(argv[:6], ['findings', 'to-file', str(self.root), FP, '--title', 'Hello'])
            self.assertEqual(argv[6], '--body-file')
            self.assertEqual(argv[8:], ['--actor', 'ui', '--json'])
            self.assertNotIn(draft, argv)
            path = argv[7]
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(handle.read(), draft)
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)
            self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o077, 0)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.dirname(path)))

        with self.during_call({'fps': [FP], 'severity': 'P1', 'text': edited}, self.path('edit')) as argv:
            self.assertEqual(argv[:6], ['findings', 'edit', str(self.root), FP, '--severity', 'P1'])
            self.assertEqual(argv[6], '--text-file')
            self.assertEqual(argv[8:], ['--actor', 'ui', '--json'])
            self.assertNotIn(edited, argv)
            path = argv[7]
            with open(path, encoding='utf-8') as handle:
                self.assertEqual(handle.read(), edited)
            self.assertEqual(os.stat(path).st_mode & 0o077, 0)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(os.path.dirname(path)))

        secret = 'secret draft'
        self.behave(code=3, stderr='forge findings: refused\n', stdout='')
        status, answer = self.post({'fps': [FP], 'body': secret}, self.path('to-file'))
        self.assertEqual((status, answer['error']), (409, 'refused'))
        argv = self.last_argv()
        self.assertNotIn(secret, argv)
        self.assertFalse(os.path.exists(argv[argv.index('--body-file') + 1]))

    def test_unknown_project_and_a_path_that_is_not_an_op(self):
        self.assertEqual(self.post({'fps': [FP], 'commit': COMMIT}, self.path('fix', 'ffffffff')),
                         (404, {'error': 'not found'}))
        self.assertEqual(self.post({'fps': [FP]}, f'/api/projects/{self.PID}/findings')[0], 405)
        self.assertEqual(self.post({'fps': [FP]}, self.path('triage'))[0], 405)
        self.assertEqual(self.calls(), [])

    def test_a_refused_change_is_409_with_the_cli_message_and_keeps_the_cache(self):
        root = str(self.root)
        self.screen.cli[('issues', root, True)] = (time.time(), {'findings': []})
        self.screen.runs['/stale'] = (1.0, [])
        self.behave(code=3, stdout='', stderr='forge findings: already fixed\n')
        status, answer = self.post({'fps': [FP], 'commit': COMMIT}, self.path('fix'))
        self.assertEqual((status, answer['error']), (409, 'refused'))
        self.assertIn('already fixed', answer['detail'])
        self.assertIn(('issues', root, True), self.screen.cli)
        self.assertEqual(self.screen.runs, {'/stale': (1.0, [])})

    def test_a_held_findings_lock_is_busy(self):
        lock = control._server_lock(self.server, 'findings_lock')
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertEqual(self.post({'fps': [FP]}, self.path('reopen')), (409, {'error': 'busy'}))
        finally:
            lock.release()
        self.assertEqual(self.calls(), [])

    def test_a_successful_write_makes_the_next_findings_get_read_again(self):
        root = str(self.root)
        now = time.time()
        self.screen.cli[('issues', root, True)] = (now, {'findings': []})
        self.screen.cli[('issues', root, False)] = (now, {'findings': []})
        self.screen.cli['history'] = (now, {'enabled': False, 'runs': [], 'findings': []})
        self.screen.cli[('memory', root, ())] = (now, {'memory': 'x'})
        self.screen.runs['/stale'] = (1.0, [])
        status, got = self.get(f'/api/projects/{self.PID}/findings')
        self.assertEqual(status, 200, got)
        self.assertEqual(self.calls(), [])  # the seeded issues listing and history answered it
        self.assertEqual(self.post({'fps': [FP], 'reason': 'done'}, self.path('dismiss')), (200, {'ok': True}))
        self.assertEqual(set(self.screen.cli), {('memory', root, ()), 'history'})
        self.assertEqual(self.screen.runs, {})
        status, got = self.get(f'/api/projects/{self.PID}/findings')
        self.assertEqual(status, 200, got)
        self.assertEqual([c['argv'] for c in self.calls()], [
            ['findings', 'dismiss', root, FP, '--reason', 'done', '--actor', 'ui', '--json'],
            ['issues', root, '--json', '--all-runs', '--include-plausible', '--include-dismissed'],
        ])

    def test_refused_without_authentication_and_read_only(self):
        self.refused_everywhere(self.path('reopen'), {'fps': [FP]})
        ro = self.start(read_only=True)
        for op in OPS:
            self.assertEqual(self.post(self.body(op), self.path(op), server=ro)[0], 405, op)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
