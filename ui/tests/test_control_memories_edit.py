"""POST /api/projects/<id>/memories/add and /memories/edit (#161).

Text is one argv element. A bad body is refused before forge is called.
"""
import time

from forge_ui import control
from test_control_actions import ActionCase, FakeProjects


class MemoriesEditTests(ActionCase):
    PID = '0123abcd'

    def setUp(self):
        super().setUp()
        self.one, self.two = self.base / 'one', self.base / 'two'
        self.one.mkdir()
        self.two.mkdir()
        runs = [{'repo_worktree': str(self.one)}, {'repo_worktree': str(self.two)},
                {'repo_worktree': str(self.base / 'gone')}]
        self.screen = self.server.project_screen = FakeProjects({self.PID: {'root': str(self.one), 'runs': runs}})
        self.add_path = f'/api/projects/{self.PID}/memories/add'
        self.edit_path = f'/api/projects/{self.PID}/memories/edit'
        self.behave(stdout='{"ok": true, "key": "verify:kept"}')

    def test_add_and_edit_argv(self):
        tricky = 'it\'s $HOME "now"'
        status, body = self.post({'category': 'verify', 'text': tricky, 'path': str(self.one)}, self.add_path)
        self.assertEqual(status, 200)
        self.assertEqual(body['checkouts'][0]['result'], {'ok': True, 'key': 'verify:kept'})
        self.assertEqual(self.last_argv(),
                         ['memory', 'add', str(self.one), '--category', 'verify', '--text', tricky,
                          '--actor', 'ui', '--json'])

        dashed = '-n keep'
        self.post({'category': 'finding', 'text': dashed, 'paths': [], 'path': str(self.one)}, self.add_path)
        self.assertEqual(self.last_argv(),
                         ['memory', 'add', str(self.one), '--category', 'finding', '--text', dashed,
                          '--actor', 'ui', '--json'])

        self.post({'category': 'trap', 'text': 'keep the lock',
                   'paths': ['ui/a.py', 'ui/b.py', 'ui/a.py'], 'path': str(self.two)}, self.add_path)
        self.assertEqual(self.last_argv(),
                         ['memory', 'add', str(self.two), '--category', 'trap', '--text', 'keep the lock',
                          '--path', 'ui/a.py', '--path', 'ui/b.py', '--actor', 'ui', '--json'])

        paths = [f'p{i}.py' for i in range(20)]
        self.post({'category': 'verify', 'text': 'a' * 300, 'paths': paths, 'path': str(self.one)}, self.add_path)
        argv = self.last_argv()
        self.assertEqual(argv[argv.index('--text') + 1], 'a' * 300)
        self.assertEqual([argv[i + 1] for i, word in enumerate(argv) if word == '--path'], paths)

        self.post({'key': 'trap:a b', 'text': tricky, 'path': str(self.one)}, self.edit_path)
        self.assertEqual(self.last_argv(),
                         ['memory', 'edit', str(self.one), 'trap:a b', '--text', tricky, '--actor', 'ui', '--json'])

    def test_every_checkout_or_only_the_named_path(self):
        status, body = self.post({'category': 'verify', 'text': 'everywhere'}, self.add_path)
        self.assertEqual((status, body['ok']), (200, True))
        self.assertEqual([c['path'] for c in body['checkouts']], [str(self.one), str(self.two)])
        self.assertEqual([c['argv'] for c in self.calls()], [
            ['memory', 'add', str(p), '--category', 'verify', '--text', 'everywhere', '--actor', 'ui', '--json']
            for p in (self.one, self.two)])

        status, body = self.post({'key': 'trap:x', 'text': 'only two', 'path': str(self.two)}, self.edit_path)
        self.assertEqual((status, [c['path'] for c in body['checkouts']]), (200, [str(self.two)]))
        self.assertEqual(len(self.calls()), 3)
        for other in (str(self.base / 'gone'), str(self.base), '', 3):
            self.assertEqual(self.post({'key': 'trap:x', 'text': 'no', 'path': other}, self.edit_path)[0], 400, other)
            self.assertEqual(self.post({'category': 'verify', 'text': 'no', 'path': other}, self.add_path)[0], 400, other)
        self.assertEqual(len(self.calls()), 3)

    def test_bad_bodies_are_400_before_any_call(self):
        long_path = 'p' * 301
        add_bad = [
            {},
            {'text': 'keep'},
            {'category': 'note', 'text': 'keep'},
            {'category': 'VERIFY', 'text': 'keep'},
            {'category': '', 'text': 'keep'},
            {'category': 1, 'text': 'keep'},
            {'category': 'verify', 'text': ''},
            {'category': 'verify', 'text': '   '},
            {'category': 'verify', 'text': 'a' * 301},
            {'category': 'verify', 'text': 'a\nb'},
            {'category': 'verify', 'text': 'a\tb'},
            {'category': 'verify', 'text': 3},
            {'category': 'verify', 'text': 'keep', 'paths': 'ui/a.py'},
            {'category': 'verify', 'text': 'keep', 'paths': ['../x']},
            {'category': 'verify', 'text': 'keep', 'paths': ['/abs']},
            {'category': 'verify', 'text': 'keep', 'paths': ['a/../b']},
            {'category': 'verify', 'text': 'keep', 'paths': [long_path]},
            {'category': 'verify', 'text': 'keep', 'paths': ['a,b']},
            {'category': 'verify', 'text': 'keep', 'paths': ['app/[id]/page']},
            {'category': 'verify', 'text': 'keep', 'paths': [f'p{i}.py' for i in range(21)]},
            {'category': 'verify', 'text': 'keep', 'paths': ['']},
            {'category': 'verify', 'text': 'keep', 'extra': 1},
        ]
        edit_bad = [
            {},
            {'text': 'keep'},
            {'key': 'nokey', 'text': 'keep'},
            {'key': 'trap:', 'text': 'keep'},
            {'key': ':x', 'text': 'keep'},
            {'key': '-trap:x', 'text': 'keep'},
            {'key': 'trap:x\n', 'text': 'keep'},
            {'key': '', 'text': 'keep'},
            {'key': 1, 'text': 'keep'},
            {'key': 'trap:x', 'text': ''},
            {'key': 'trap:x', 'text': '   '},
            {'key': 'trap:x', 'text': 'a' * 301},
            {'key': 'trap:x', 'text': 'a\nb'},
            {'key': 'trap:x', 'text': 'keep', 'paths': ['ui/a.py']},
            {'key': 'trap:x', 'text': 'keep', 'extra': 1},
        ]
        cached = ('memory', str(self.one), (1, 2))
        self.screen.cli[cached] = (time.time(), {'memory': 'x'})
        self.screen.cli['history'] = (time.time(), {})
        for body in add_bad:
            self.assertEqual(self.post(body, self.add_path)[0], 400, body)
        for body in edit_bad:
            self.assertEqual(self.post(body, self.edit_path)[0], 400, body)
        self.assertEqual(self.calls(), [])
        self.assertEqual(set(self.screen.cli), {cached, 'history'})

    def test_unknown_project_and_no_checkout(self):
        self.assertEqual(self.post({'category': 'verify', 'text': 'keep'},
                                   '/api/projects/ffffffff/memories/add')[0], 404)
        self.assertEqual(self.post({'key': 'trap:x', 'text': 'keep'},
                                   '/api/projects/ffffffff/memories/edit')[0], 404)
        self.screen.found[self.PID] = {'root': str(self.base / 'gone'),
                                       'runs': [{'repo_worktree': str(self.base / 'gone')}]}
        for path, body in ((self.add_path, {'category': 'verify', 'text': 'keep'}),
                           (self.edit_path, {'key': 'trap:x', 'text': 'keep'})):
            self.assertEqual(self.post(body, path),
                             (409, {'error': 'refused', 'detail': 'no checkout of this project exists'}))
        self.assertEqual(self.calls(), [])

    def test_a_busy_lock_does_not_call_or_drop_the_cache(self):
        cached = ('memory', str(self.one), (1, 2))
        self.screen.cli[cached] = (time.time(), {'memory': 'x'})
        self.screen.cli['history'] = (time.time(), {})
        lock = control._server_lock(self.server, 'memory_lock')
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertEqual(self.post({'category': 'verify', 'text': 'keep'}, self.add_path), (409, {'error': 'busy'}))
            self.assertEqual(self.post({'key': 'trap:x', 'text': 'keep'}, self.edit_path), (409, {'error': 'busy'}))
        finally:
            lock.release()
        self.assertEqual(self.calls(), [])
        self.assertEqual(set(self.screen.cli), {cached, 'history'})

    def test_a_success_drops_only_the_memory_answers(self):
        self.screen.cli[('memory', str(self.one), (1, 2))] = (time.time(), {'memory': 'x'})
        self.screen.cli[('issues', str(self.one))] = (time.time(), {'issues': 1})
        self.screen.cli['history'] = (time.time(), {})
        self.assertEqual(self.post({'category': 'verify', 'text': 'keep', 'path': str(self.one)}, self.add_path)[0], 200)
        self.assertEqual(set(self.screen.cli), {'history', ('issues', str(self.one))})
        self.screen.cli[('memory', str(self.two), 'k')] = (time.time(), {})
        self.assertEqual(self.post({'key': 'trap:x', 'text': 'edited', 'path': str(self.two)}, self.edit_path)[0], 200)
        self.assertEqual(set(self.screen.cli), {'history', ('issues', str(self.one))})

    def test_every_checkout_failing_returns_the_first_error(self):
        self.behave(code=4, stdout='{"ok": false}', stderr='boom\n')
        self.assertEqual(self.post({'category': 'verify', 'text': 'nope'}, self.add_path)[0], 500)
        self.assertEqual(len(self.calls()), 2)

    def test_refused_without_authentication_and_read_only(self):
        self.refused_everywhere(self.add_path, {'category': 'verify', 'text': 'keep'})
        self.refused_everywhere(self.edit_path, {'key': 'trap:x', 'text': 'keep'})
