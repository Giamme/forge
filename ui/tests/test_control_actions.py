"""The routes that remove worktrees, forget memories, compact the store and answer a task (#147)."""
import json
import os
import threading
import time
import unittest

from forge_ui import control, projects as P, storage, worktrees
from test_control_api import ControlApiCase

ROWS = {'ok': True, 'dry_run': False, 'rows': [{'path': '/wt/a', 'action': 'remove', 'status': 'removed', 'reason': ''}]}


class FakeWorktrees(worktrees.Screen):
    """A Worktrees screen that lists fixed paths and counts how often it was asked to read again."""

    def __init__(self, paths):
        super().__init__()
        self.cached, self.fresh, self.asked = list(paths), list(paths), []

    def answer(self, catalog, history, refresh=False, now=None):
        self.asked.append(refresh)
        return {'rows': [{'path': p} for p in (self.fresh if refresh else self.cached)]}


class FakeProjects(P.Screen):
    def __init__(self, found):
        super().__init__()
        self.found = found

    def projects(self, catalog):
        return self.found


class ActionCase(ControlApiCase):
    def setUp(self):
        super().setUp()
        self.behave(stdout='{"ok": true}')

    def last_argv(self):
        return self.calls()[-1]['argv']

    def refused_everywhere(self, path, body):
        """The same request without the token, from a foreign origin and read-only never reaches the CLI."""
        self.assertEqual(self.post(body, path, **{'X-Forge-UI-Token': None})[0], 403)
        self.assertEqual(self.post(body, path, **{'X-Forge-UI-Token': 'x' * 43})[0], 403)
        self.assertEqual(self.post(body, path, Origin='http://evil.example')[0], 403)
        self.assertEqual(self.post(body, path, **{'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.calls(), [])
        ro = self.start(read_only=True)
        self.assertEqual(self.post(body, path, server=ro)[0], 405)
        self.assertEqual(self.calls(), [])


class WorktreesRemoveTests(ActionCase):
    PATH = '/api/worktrees/remove'

    def setUp(self):
        super().setUp()
        self.screen = self.server.worktree_screen = FakeWorktrees(['/wt/a', '/wt/b'])
        self.behave(stdout=json.dumps(ROWS))

    def test_argv_and_answer(self):
        self.assertEqual(self.post({'paths': ['/wt/a', '/wt/b', '/wt/a']}, self.PATH), (200, ROWS))
        self.assertEqual(self.last_argv(), ['worktrees', 'rm', '/wt/a', '/wt/b', '--json', '--actor', 'ui'])
        self.post({'paths': ['/wt/a'], 'branches': True, 'dry_run': True}, self.PATH)
        self.assertEqual(self.last_argv(), ['worktrees', 'rm', '/wt/a', '--branches', '--dry-run', '--json', '--actor', 'ui'])

    def test_a_path_missing_from_the_cached_rows_is_looked_up_once_more(self):
        self.screen.fresh.append('/wt/new')
        self.assertEqual(self.post({'paths': ['/wt/a']}, self.PATH)[0], 200)
        self.assertEqual(self.screen.asked, [False])
        self.assertEqual(self.post({'paths': ['/wt/new']}, self.PATH)[0], 200)
        self.assertEqual(self.screen.asked, [False, False, True])
        self.assertEqual(self.post({'paths': ['/wt/zzz']}, self.PATH)[0], 400)
        self.assertEqual(self.screen.asked[3:], [False, True])
        self.assertEqual(len(self.calls()), 2)

    def test_bad_bodies(self):
        bad = [{}, {'paths': []}, {'paths': 'a'}, {'paths': ['/wt/a'] * 0}, {'paths': ['wt/a']}, {'paths': ['/wt/a\n']},
               {'paths': [3]}, {'paths': ['/wt/not-listed']}, {'paths': ['/wt/a'], 'extra': 1},
               {'paths': ['/wt/a'], 'branches': 'yes'}, {'paths': ['/wt/a'], 'dry_run': 1},
               {'paths': [f'/wt/{n}' for n in range(201)]}]
        for body in bad:
            self.assertEqual(self.post(body, self.PATH)[0], 400, body)
        self.assertEqual(self.calls(), [])

    def test_refused_without_authentication_and_read_only(self):
        self.refused_everywhere(self.PATH, {'paths': ['/wt/a']})

    def test_a_failed_row_is_a_200_with_the_rows(self):
        failed = dict(ROWS, ok=False, rows=[{'path': '/wt/a', 'action': 'remove', 'status': 'failed', 'reason': 'dirty'}])
        self.behave(code=4, stdout=json.dumps(failed), stderr='a row failed\n')
        status, body = self.post({'paths': ['/wt/a']}, self.PATH)
        self.assertEqual((status, body['ok'], body['rows']), (200, False, failed['rows']))

    def test_other_failures_keep_their_status(self):
        self.behave(code=2, stderr='bad\n')
        self.assertEqual(self.post({'paths': ['/wt/a']}, self.PATH)[0], 400)
        self.behave(code=4, stdout='', stderr='boom\n')
        self.assertEqual(self.post({'paths': ['/wt/a']}, self.PATH)[0], 500)

    def test_caches_are_dropped_after_a_real_removal_only(self):
        storage.screen(self.server).result = {'computed': 123.0}
        self.screen.result = {'computed': time.time()}
        self.post({'paths': ['/wt/a'], 'dry_run': True}, self.PATH)
        self.assertIsNotNone(self.screen.result)
        self.assertEqual(storage.screen(self.server).result['computed'], 123.0)
        self.post({'paths': ['/wt/a']}, self.PATH)
        self.assertIsNone(self.screen.result)
        self.assertEqual(storage.screen(self.server).result['computed'], 0)

    def test_a_second_removal_is_busy(self):
        lock = control._server_lock(self.server, 'worktrees_lock')
        self.assertTrue(lock.acquire())
        try:
            self.assertEqual(self.post({'paths': ['/wt/a']}, self.PATH), (409, {'error': 'busy'}))
        finally:
            lock.release()
        self.assertEqual(self.calls(), [])


class MemoriesForgetTests(ActionCase):
    PID = '0123abcd'

    def setUp(self):
        super().setUp()
        self.one, self.two = self.base / 'one', self.base / 'two'
        self.one.mkdir()
        self.two.mkdir()
        runs = [{'repo_worktree': str(self.one)}, {'repo_worktree': str(self.two)}, {'repo_worktree': str(self.base / 'gone')}]
        project = {'root': str(self.one), 'runs': runs}
        self.screen = self.server.project_screen = FakeProjects({self.PID: project})
        self.path = f'/api/projects/{self.PID}/memories/forget'
        self.behave(stdout='{"ok": true, "keys": []}')

    def test_every_existing_checkout_is_asked(self):
        status, body = self.post({'keys': ['trap:a b', 'verify:c'], 'dry_run': True}, self.path)
        self.assertEqual(status, 200)
        self.assertEqual((body['ok'], body['dry_run']), (True, True))
        self.assertEqual([c['path'] for c in body['checkouts']], [str(self.one), str(self.two)])
        self.assertEqual(body['checkouts'][0]['result'], {'ok': True, 'keys': []})
        argvs = [c['argv'] for c in self.calls()]
        self.assertEqual(argvs, [['memory', 'forget', str(p), 'trap:a b', 'verify:c', '--dry-run', '--json', '--actor', 'ui']
                                 for p in (self.one, self.two)])

    def test_path_names_one_checkout(self):
        status, body = self.post({'keys': ['trap:x'], 'path': str(self.two)}, self.path)
        self.assertEqual((status, [c['path'] for c in body['checkouts']]), (200, [str(self.two)]))
        self.assertEqual(self.last_argv(), ['memory', 'forget', str(self.two), 'trap:x', '--json', '--actor', 'ui'])
        self.assertEqual(len(self.calls()), 1)
        for other in (str(self.base / 'gone'), str(self.base), '', 3):
            self.assertEqual(self.post({'keys': ['trap:x'], 'path': other}, self.path)[0], 400, other)
        self.assertEqual(len(self.calls()), 1)

    def test_bad_bodies_and_unknown_project(self):
        bad = [{}, {'keys': []}, {'keys': 'trap:x'}, {'keys': ['nokey']}, {'keys': ['trap:']}, {'keys': [':x']},
               {'keys': ['-trap:x']}, {'keys': ['trap:x\n']}, {'keys': [1]}, {'keys': ['trap:x'], 'dry_run': 'no'},
               {'keys': ['trap:x'], 'more': 1}, {'keys': [f'trap:{n}' for n in range(201)]}]
        for body in bad:
            self.assertEqual(self.post(body, self.path)[0], 400, body)
        self.assertEqual(self.post({'keys': ['trap:x']}, '/api/projects/ffffffff/memories/forget')[0], 404)
        self.assertEqual(self.calls(), [])

    def test_refused_without_authentication_and_read_only(self):
        self.refused_everywhere(self.path, {'keys': ['trap:x']})

    def test_one_failing_checkout_is_listed_and_all_failing_is_an_error(self):
        # the fake fails every call: both fail, the first failure is the answer
        self.behave(code=4, stdout='{"ok": false}', stderr='boom\n')
        self.assertEqual(self.post({'keys': ['trap:x']}, self.path)[0], 500)

    def test_the_memories_answers_are_dropped_after_a_real_forget(self):
        self.screen.cli[('memory', str(self.one), (1, 2))] = (time.time(), {'memory': 'x'})
        self.screen.cli['history'] = (time.time(), {})
        self.post({'keys': ['trap:x'], 'dry_run': True}, self.path)
        self.assertEqual(len(self.screen.cli), 2)
        self.post({'keys': ['trap:x']}, self.path)
        self.assertEqual(list(self.screen.cli), ['history'])


class CompactTests(ActionCase):
    PATH = '/api/storage/compact'

    def setUp(self):
        super().setUp()
        self.out = {'ok': True, 'dry_run': False, 'bytes_freed': 5, 'runs': []}
        self.behave(stdout=json.dumps(self.out))

    def test_argv(self):
        self.assertEqual(self.post({'all': True}, self.PATH), (200, self.out))
        self.assertEqual(self.last_argv(), ['runs', 'compact', '--all', '--logs', '--json'])
        self.post({'all': True, 'dry_run': True}, self.PATH)
        self.assertEqual(self.last_argv(), ['runs', 'compact', '--all', '--logs', '--json', '--dry-run'])
        self.post({'run': self.key}, self.PATH)
        self.assertEqual(self.last_argv(), ['runs', 'compact', str(self.server.catalog.locate(self.key)[0]),
                                            '--logs', '--json'])
        self.post({'run': self.key, 'dry_run': True}, self.PATH)
        self.assertEqual(self.last_argv()[-1], '--dry-run')

    def test_bad_bodies_and_unknown_run(self):
        bad = [{}, {'run': self.key, 'all': True}, {'all': False}, {'all': 1}, {'run': 'xyz'}, {'run': 3},
               {'run': self.key.upper()}, {'all': True, 'dry_run': 'x'}, {'all': True, 'x': 1}]
        for body in bad:
            self.assertEqual(self.post(body, self.PATH)[0], 400, body)
        self.assertEqual(self.post({'run': '0' * 12}, self.PATH)[0], 404)
        self.assertEqual(self.calls(), [])

    def test_refused_without_authentication_and_read_only(self):
        self.refused_everywhere(self.PATH, {'all': True})

    def test_an_active_run_is_a_409_with_the_message(self):
        self.behave(code=3, stdout='', stderr='forge runs compact: the run is active\n')
        status, body = self.post({'run': self.key}, self.PATH)
        self.assertEqual((status, body['error']), (409, 'refused'))
        self.assertIn('active', body['detail'])

    def test_the_storage_answer_is_stale_after_a_real_compact_only(self):
        storage.screen(self.server).result = {'computed': 99.0}
        self.post({'all': True, 'dry_run': True}, self.PATH)
        self.assertEqual(storage.screen(self.server).result['computed'], 99.0)
        self.post({'all': True}, self.PATH)
        self.assertEqual(storage.screen(self.server).result['computed'], 0)

    def test_a_concurrent_compact_is_busy(self):
        self.behave(stdout=json.dumps(self.out), sleep=1.5)
        first = []
        worker = threading.Thread(target=lambda: first.append(self.post({'all': True}, self.PATH)))
        worker.start()
        self.wait_for_call()
        self.assertEqual(self.post({'all': True}, self.PATH), (409, {'error': 'busy'}))
        worker.join()
        self.assertEqual(first[0][0], 200)
        self.assertEqual(len(self.calls()), 1)


class AnswerTests(ActionCase):
    def path(self, task='reader', key=None):
        return f'/api/runs/{key or self.key}/tasks/{task}/answer'

    def test_the_text_goes_to_stdin_whether_or_not_a_scheduler_lives(self):
        self.behave(stdout='{"queued": false, "prepared": true}')
        self.assertEqual(self.post({'text': '  use the new table \n'}, self.path()),
                         (200, {'live': False, 'result': {'queued': False, 'prepared': True}}))
        call = self.calls()[-1]
        self.assertEqual(call['argv'], ['control', 'retry', str(self.server.catalog.locate(self.key)[0]), 'reader',
                                        '--answer-file', '-', '--actor', 'ui', '--json'])
        self.assertEqual(call['stdin'], 'use the new table')
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid()}))
        self.behave(stdout='{"queued": true, "request": "r1"}')
        self.assertEqual(self.post({'text': 'again'}, self.path()),
                         (200, {'live': True, 'result': {'queued': True, 'request': 'r1'}}))
        self.assertEqual(self.calls()[-1]['argv'][:2], ['control', 'retry'])
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': 2 ** 22 + 12345678}))
        self.assertFalse(self.post({'text': 'x'}, self.path())[1]['live'])

    def test_bad_bodies_and_runs(self):
        bad = [{}, {'text': ''}, {'text': '   \n'}, {'text': 'x' * 8001}, {'text': 'a\x00b'}, {'text': 3},
               {'text': 'x', 'more': 1}, {'text': '\ud800'}]
        for body in bad:
            self.assertEqual(self.post(body, self.path())[0], 400, body)
        self.assertEqual(self.post({'text': 'x' * 8000}, self.path())[0], 200)
        self.assertEqual(self.post({'text': 'x'}, self.path(key=self.solo_key))[0], 404)
        self.assertEqual(self.post({'text': 'x'}, self.path(key='0' * 12))[0], 404)
        self.assertEqual(len(self.calls()), 1)

    def test_not_waiting_is_a_409(self):
        self.behave(code=3, stderr='forge control: task reader is not NEEDS_INPUT\n')
        status, body = self.post({'text': 'x'}, self.path())
        self.assertEqual((status, body['error']), (409, 'refused'))
        self.assertIn('NEEDS_INPUT', body['detail'])

    def test_refused_without_authentication_and_read_only(self):
        self.refused_everywhere(self.path(), {'text': 'x'})

    def test_a_second_write_to_the_run_is_busy(self):
        self.assertTrue(self.server.run_locks.acquire(self.key))
        try:
            self.assertEqual(self.post({'text': 'x'}, self.path()), (409, {'error': 'busy'}))
        finally:
            self.server.run_locks.release(self.key)
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
