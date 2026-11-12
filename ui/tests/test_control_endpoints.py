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
GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


def git(cwd, *args):
    done = subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV)
    return done.stdout.strip()


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


class WorktreesTests(EndpointCase):
    def test_reads_the_history_through_the_cli_once_per_read_and_caches_the_rest(self):
        self.behave(stdout=json.dumps({'enabled': False, 'since': None, 'events': [], 'attempts': [], 'integrated': [], 'repos': []}))
        status, body = self.get('/api/worktrees')
        self.assertEqual(status, 200)
        self.assertEqual(self.last_argv(), ['dataset', 'worktrees', '--json'])
        self.assertEqual(body['dataset'], {'enabled': False, 'since': None})
        self.assertIn('totals', body)
        self.get('/api/worktrees')
        self.assertEqual(len(self.calls()), 1)              # cached: no second CLI call, no second git read
        self.get('/api/worktrees?refresh=1')
        self.assertEqual(len(self.calls()), 2)
        for query in ('?refresh=yes', '?repo=/etc', '?refresh=1&refresh=1'):
            self.assertEqual(self.get('/api/worktrees' + query)[0], 400, query)
        self.assertEqual(len(self.calls()), 2)

    def test_a_failed_history_read_still_lists_what_git_knows(self):
        self.behave(code=4, stderr='boom\n')
        status, body = self.get('/api/worktrees')
        self.assertEqual(status, 200)
        self.assertIsNone(body['dataset'])
        self.assertTrue(any('could not be read' in n for n in body['notes']))


class ReadOnlyWorktreesTests(WorktreesTests):
    read_only = True


class ProjectsTests(EndpointCase):
    def pid(self):
        from forge_ui import projects
        return projects.project_id(self.rrepo)

    def test_the_list_and_a_project_read_run_dirs_only(self):
        status, body = self.get('/api/projects')
        self.assertEqual(status, 200)
        self.assertIn((self.pid(), self.rrepo, 1), [(p['id'], p['root'], p['runs']) for p in body['projects']])
        status, body = self.get('/api/projects/' + self.pid())
        self.assertEqual((status, body['root'], [r['key'] for r in body['runs']]), (200, self.rrepo, [self.key]))
        self.assertEqual(self.get('/api/projects/00000000')[0], 404)
        self.assertEqual(self.get('/api/projects?x=1')[0], 400)
        self.assertEqual(self.calls(), [])

    def test_findings_ask_the_dataset_and_forge_issues_offline_then_answer_from_the_cache(self):
        status, body = self.get(f'/api/projects/{self.pid()}/findings')
        self.assertEqual(status, 200)
        self.assertEqual(sorted(c['argv'] for c in self.calls()), [
            ['dataset', 'history', '--json'],
            ['issues', self.rplan, '--json', '--offline', '--all-runs', '--include-plausible', '--include-dismissed']])
        self.get(f'/api/projects/{self.pid()}/findings')
        self.assertEqual(len(self.calls()), 2)
        self.get(f'/api/projects/{self.pid()}/findings?refresh=1')
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual(self.get(f'/api/projects/{self.pid()}/findings?refresh=2')[0], 400)
        self.assertEqual(self.get('/api/projects/00000000/findings')[0], 404)

    def test_memories_ask_forge_memory_for_each_checkout_that_exists(self):
        status, body = self.get(f'/api/projects/{self.pid()}/memories')
        self.assertEqual(status, 200)
        self.assertEqual(sorted(c['argv'] for c in self.calls()), [
            ['dataset', 'history', '--json'], ['memory', 'learnings', self.rrepo, '--json']])
        self.assertEqual([s['path'] for s in body['sources']], [self.rrepo])


class ReadOnlyProjectsTests(ProjectsTests):
    read_only = True


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

    def removed_worktree(self):
        """Makes the plan run's checkout a worktree of a main checkout and removes it: the run's base commit is in
        the main checkout, which is returned. The solo run's repo is the caller's to set."""
        main, wt = Path(os.path.realpath(self.base / 'main')), self.base / 'wt'
        main.mkdir()
        git(main, 'init', '-q', '-b', 'main')
        (main / 'f').write_text('x')
        git(main, 'add', 'f')
        git(main, 'commit', '-q', '-m', 'init')
        sha = git(main, 'rev-parse', 'HEAD')
        git(main, 'worktree', 'add', '-q', '-b', 'w', str(wt))
        (self.plan / 'repo').write_text(f'{wt}\n')
        (self.plan / 'integration.base').write_text(f'{sha}\n')
        git(main, 'worktree', 'remove', '--force', str(wt))
        return main

    def test_a_removed_worktree_reads_and_writes_the_main_checkout(self):
        main = self.removed_worktree()
        # The solo run is in the main checkout, which makes it a known root for the removed worktree's run.
        (self.solo / 'repo').write_text(f'{main}\n')
        self.assertEqual(self.get(f'/api/config?run={self.key}')[0], 200)
        self.assertEqual(self.last_argv(), ['config', 'show', '--repo', str(main), '--json'])
        self.assertEqual(self.cfg(key='repo.verify', value='make test')[0], 200)
        self.assertEqual(self.last_argv(),
                         ['config', 'set', 'repo.verify', 'make test', '--repo', str(main), '--json'])

    def test_a_removed_worktree_with_no_known_root_is_still_refused(self):
        self.removed_worktree()
        (self.solo / 'repo').write_text('/no/such/checkout\n')
        self.assertEqual(self.get(f'/api/config?run={self.key}')[0], 409)
        self.assertEqual(self.cfg(key='repo.verify', value='x')[0], 409)
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
                 (('POST', '/api/config/profiles', {'run': k, 'action': 'nope'}), ('profiles', k)),
                 (('GET', '/api/config/profiles', None, f'run={k}&repo=/x'), ('profiles', k)),
                 (('GET', f'/api/runs/{k}/issues', None, 'scope=x'), ('issues', k))]
        for args, expected in cases:
            self.assertEqual(self.named(*args), expected, args)
        self.assertEqual(self.calls(), [])


class SummariesSwitchTests(EndpointCase):
    """The summaries switch is a `control set` like any key: the run's, and each task's on, off or inherit."""

    def test_argv_for_the_run_and_for_a_task(self):
        plan = self.rplan
        cases = [({'action': 'set', 'key': 'summaries', 'value': 'on'},
                  ['control', 'set', plan, 'summaries', 'on', '--actor', 'ui', '--json']),
                 ({'action': 'set', 'key': 'summaries', 'value': 'off'},
                  ['control', 'set', plan, 'summaries', 'off', '--actor', 'ui', '--json']),
                 ({'action': 'set', 'key': 'summaries', 'value': 'inherit', 'task': 't1'},
                  ['control', 'set', plan, 'summaries', 'inherit', '--task', 't1', '--actor', 'ui', '--json']),
                 ({'action': 'set', 'key': 'summaries', 'value': 'off', 'task': 't1'},
                  ['control', 'set', plan, 'summaries', 'off', '--task', 't1', '--actor', 'ui', '--json'])]
        for body, argv in cases:
            status, answer = self.post(body)
            self.assertEqual((status, answer['ok'], self.last_argv()), (200, True, argv), body)

    def test_a_value_the_cli_refuses_comes_back_as_invalid(self):
        # The route checks the shape only; the value is the CLI's to refuse (exit 2 -> 400 invalid).
        self.behave(code=2, stderr='forge control: summaries: needs on or off\n')
        for value in ('maybe', 'inherit'):   # inherit is a task's value only
            status, answer = self.post({'action': 'set', 'key': 'summaries', 'value': value})
            self.assertEqual((status, answer['error'], answer['detail']),
                             (400, 'invalid', 'forge control: summaries: needs on or off'), value)
        self.assertEqual([call['argv'] for call in self.calls()],
                         [['control', 'set', self.rplan, 'summaries', 'maybe', '--actor', 'ui', '--json'],
                          ['control', 'set', self.rplan, 'summaries', 'inherit', '--actor', 'ui', '--json']])

    def test_a_value_with_a_leading_dash_never_reaches_the_cli(self):
        self.assertEqual(self.post({'action': 'set', 'key': 'summaries', 'value': '-on'})[0], 400)
        self.assertEqual(self.post({'action': 'set', 'key': 'summaries', 'value': 'on', 'task': '--task'})[0], 400)
        self.assertEqual(self.calls(), [])


class SummariesReadOnlyTests(EndpointCase):
    read_only = True

    def test_the_switch_is_refused_for_the_run_and_for_a_task(self):
        for body in ({'action': 'set', 'key': 'summaries', 'value': 'on'},
                     {'action': 'set', 'key': 'summaries', 'value': 'inherit', 'task': 't1'}):
            self.assertEqual(self.post(body), (405, {'error': 'method not allowed'}), body)
        self.assertEqual(self.calls(), [])


class BriefAnswerTests(EndpointCase):
    """POST /api/runs/<key>/tasks/<id>/brief/<qid> runs `forge brief answer` with the answer on stdin."""

    def path(self, qid='q1', task='reader', key=None):
        return f'/api/runs/{key or self.key}/tasks/{task}/brief/{qid}'

    def test_the_answer_is_passed_on_stdin_with_actor_ui(self):
        # A late answer prints a notice, not a JSON object, and exits 0. That is a success.
        notice = 'too late for task reader: the answer does not change this task\n'
        self.behave(stdout=notice)
        answer = 'use the reader'
        status, body = self.post({'answer': answer}, self.path())
        self.assertEqual((status, body), (200, {'exit': 0, 'output': notice}))
        self.assertEqual(self.last_argv(), ['brief', 'answer', self.rplan, 'reader', 'q1',
                                            '--file', '-', '--actor', 'ui'])
        self.assertEqual(self.calls()[-1]['stdin'], answer)
        self.assertEqual(self.calls()[-1]['leader'], True)

    def test_a_bad_qid_an_empty_answer_and_one_too_long_are_refused(self):
        self.behave(stdout='reader/q12 answered\n')
        answer = 'y' * 4096
        status, body = self.post({'answer': answer}, self.path('q12'))
        self.assertEqual((status, body['exit'], body['output']), (200, 0, 'reader/q12 answered\n'), body)
        self.assertEqual(self.calls()[-1]['stdin'], answer)
        self.assertEqual(self.last_argv()[-5:], ['q12', '--file', '-', '--actor', 'ui'])
        before = len(self.calls())
        refused = [('q', {'answer': 'yes'}), ('q1a', {'answer': 'yes'}), ('1', {'answer': 'yes'}),
                   ('Q1', {'answer': 'yes'}), ('q1', {'answer': ''}), ('q1', {}),
                   ('q1', {'answer': 'z' * 4097}), ('q1', {'answer': 'a\x00b'}),
                   ('q1', {'answer': 3}), ('q1', {'answer': 'yes', 'actor': 'ui'})]
        for qid, obj in refused:
            self.assertEqual(self.post(obj, self.path(qid))[0], 400, (qid, obj))
        self.assertEqual(self.post({'answer': 'yes'}, self.path(task='solo', key=self.solo_key))[0], 404)
        self.assertEqual(self.post({'answer': 'yes'}, self.path(key='000000000000'))[0], 404)
        self.assertEqual(len(self.calls()), before)

    def test_a_cli_error_keeps_the_output_and_the_exit_code(self):
        self.behave(code=2, stderr='forge brief: no such question: q9\n', stdout='')
        status, body = self.post({'answer': 'yes'}, self.path('q9'))
        self.assertEqual(status, 400)
        self.assertEqual(body['error'], 'invalid')
        self.assertEqual(body['exit'], 2)
        self.assertEqual(body['output'], '')
        self.assertIn('no such question', body['detail'])

    def test_a_second_write_to_the_run_is_busy(self):
        self.assertTrue(self.server.run_locks.acquire(self.key))
        try:
            self.assertEqual(self.post({'answer': 'yes'}, self.path()), (409, {'error': 'busy'}))
        finally:
            self.server.run_locks.release(self.key)
        self.assertEqual(self.calls(), [])


class BriefAnswerReadOnlyTests(EndpointCase):
    read_only = True

    def test_an_answer_is_refused(self):
        status, body = self.post({'answer': 'yes'}, f'/api/runs/{self.key}/tasks/reader/brief/q1')
        self.assertEqual((status, body), (405, {'error': 'method not allowed'}))
        self.assertEqual(self.calls(), [])


class SummariesRealCliTests(ControlApiCase):
    """The real `forge control set summaries` through the API. `on` is not called here: it starts forge summarize."""

    def setUp(self):
        super().setUp()
        for patcher in (patch.dict(os.environ, {'FORGE_DATASET': 'off'}),
                        patch.object(ui_server, 'forge_cli', ROOT / 'forge')):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_off_and_inherit_are_written_and_removed_and_bad_values_refused(self):
        status, answer = self.post({'action': 'set', 'key': 'summaries', 'value': 'off'})
        self.assertEqual((status, answer['ok']), (200, True), answer)
        self.assertEqual((self.plan / 'summaries').read_text(), 'off\n')
        status, answer = self.post({'action': 'set', 'key': 'summaries', 'value': 'off', 'task': 'reader'})
        self.assertEqual((status, answer['ok']), (200, True), answer)
        self.assertEqual((self.plan / 'tasks/reader/summaries').read_text(), 'off\n')
        status, answer = self.post({'action': 'set', 'key': 'summaries', 'value': 'inherit', 'task': 'reader'})
        self.assertEqual((status, answer['ok']), (200, True), answer)
        self.assertFalse((self.plan / 'tasks/reader/summaries').exists())
        for body in ({'action': 'set', 'key': 'summaries', 'value': 'maybe'},
                     {'action': 'set', 'key': 'summaries', 'value': 'inherit'},
                     {'action': 'set', 'key': 'summaries', 'value': 'maybe', 'task': 'reader'}):
            status, answer = self.post(body)
            self.assertEqual((status, answer['error']), (400, 'invalid'), body)
            self.assertIn('summaries', answer['detail'], body)
        self.assertEqual((self.plan / 'summaries').read_text(), 'off\n')  # a refused change writes nothing


class ProfileTests(EndpointCase):
    BODY = {'policy': {'timeout': 15}, 'note': 'kept'}

    def profiles(self, **body):
        return self.post({'run': self.key, **body}, '/api/config/profiles')

    def listed(self):
        """A list payload that is also a show payload, so one fake answer serves both calls."""
        body = {'policy': {'timeout': 15}}
        return {'default': {'name': 'mine', 'level': 'user'},
                'profiles': [{'name': 'mine', 'level': 'user', 'shadowed': ['repo'], 'default': True},
                             {'name': 'balanced', 'level': 'builtin', 'shadowed': [], 'default': False}],
                'body': body, 'name': 'mine', 'level': 'user', 'shadowed': ['repo']}

    def test_list_argv_attaches_each_body(self):
        payload = self.listed()
        self.behave(stdout=json.dumps(payload))
        status, body = self.get(f'/api/config/profiles?run={self.key}')
        self.assertEqual(status, 200, body)
        self.assertEqual([row['body'] for row in body['profiles']], [payload['body'], payload['body']])
        self.assertEqual(body['profiles'][0]['shadowed'], ['repo'])
        self.assertIs(body['profiles'][0]['default'], True)
        repo = self.rrepo
        self.assertEqual([call['argv'] for call in self.calls()], [
            ['config', 'profile', 'list', '--json', '--repo', repo],
            ['config', 'profile', 'show', 'mine', '--json', '--repo', repo],
            ['config', 'profile', 'show', 'balanced', '--json', '--repo', repo]])
        self.assertTrue(all(call['stdin'] == '' and call['leader'] for call in self.calls()))

    def test_a_bad_list_never_asks_show(self):
        self.behave(code=2, stderr='forge config: bad\n', stdout='{"ok": false, "error": "bad"}')
        status, body = self.get(f'/api/config/profiles?run={self.key}')
        self.assertEqual((status, body['error']), (400, 'invalid'))
        self.behave(stdout=json.dumps({'profiles': [{'name': '../x', 'level': 'user'}]}))
        self.assertEqual(self.get(f'/api/config/profiles?run={self.key}')[0], 502)
        self.assertEqual(len(self.calls()), 2)

    def test_every_action_at_both_levels(self):
        name, body = 'work', self.BODY
        stdin = json.dumps(body, allow_nan=False)
        repo = self.rrepo
        cases = [
            ('user-create', dict(action='create', name=name, level='user', body=body),
             ['config', 'profile', 'create', name, '--file', '-', '--json'], stdin),
            ('repo-create', dict(action='create', name=name, level='repo', body=body),
             ['config', 'profile', 'create', name, '--file', '-', '--repo', repo, '--json'], stdin),
            ('user-from', dict(action='create', name=name, level='user', **{'from': 'balanced'}),
             ['config', 'profile', 'create', name, '--from', 'balanced', '--json'], ''),
            ('repo-from', dict(action='create', name=name, level='repo', **{'from': 'balanced'}),
             ['config', 'profile', 'create', name, '--from', 'balanced', '--repo', repo, '--json'], ''),
            ('user-edit', dict(action='edit', name=name, level='user', body=body),
             ['config', 'profile', 'edit', name, '--file', '-', '--json'], stdin),
            ('repo-edit', dict(action='edit', name=name, level='repo', body=body),
             ['config', 'profile', 'edit', name, '--file', '-', '--repo', repo, '--json'], stdin),
            ('user-set', dict(action='set', name=name, level='user', key='policy.retry_failed', value='4'),
             ['config', 'profile', 'set', name, 'policy.retry_failed', '4', '--json'], ''),
            ('repo-set', dict(action='set', name=name, level='repo', key='routing.dwarf.low', value='haiku:xhigh'),
             ['config', 'profile', 'set', name, 'routing.dwarf.low', 'haiku:xhigh', '--repo', repo, '--json'], ''),
            ('user-unset', dict(action='unset', name=name, level='user', key='policy.timeout'),
             ['config', 'profile', 'unset', name, 'policy.timeout', '--json'], ''),
            ('repo-unset', dict(action='unset', name=name, level='repo', key='policy.timeout'),
             ['config', 'profile', 'unset', name, 'policy.timeout', '--repo', repo, '--json'], ''),
            ('user-rm', dict(action='rm', name=name, level='user'),
             ['config', 'profile', 'rm', name, '--json'], ''),
            ('repo-rm', dict(action='rm', name=name, level='repo'),
             ['config', 'profile', 'rm', name, '--repo', repo, '--json'], ''),
            ('user-use', dict(action='use', name=name, level='user'),
             ['config', 'profile', 'use', name, '--json'], ''),
            ('repo-use', dict(action='use', name=name, level='repo'),
             ['config', 'profile', 'use', name, '--repo', repo, '--json'], ''),
            ('user-none', dict(action='use', level='user', none=True),
             ['config', 'profile', 'use', '--none', '--json'], ''),
            ('repo-none', dict(action='use', level='repo', none=True),
             ['config', 'profile', 'use', '--none', '--repo', repo, '--json'], ''),
            ('long-name', dict(action='rm', name='a' * 64, level='user'),
             ['config', 'profile', 'rm', 'a' * 64, '--json'], ''),
            ('empty-value', dict(action='set', name=name, level='user', key='policy.fast', value=''),
             ['config', 'profile', 'set', name, 'policy.fast', '', '--json'], ''),
        ]
        for label, fields, argv, wanted in cases:
            self.assertEqual(self.profiles(**fields)[0], 200, label)
            call = self.calls()[-1]
            self.assertEqual(call['argv'], argv, label)
            self.assertEqual(call['stdin'], wanted, label)
            self.assertTrue(call['leader'], label)

    def test_refusals_never_reach_the_cli(self):
        name = 'work'
        bad = [dict(action='drop', name=name, level='user'),
               dict(action='create', name=name, level='builtin', body=self.BODY),
               dict(action='create', name=name, level='user'),
               dict(action='create', name=name, level='user', **{'from': 'balanced'}, body=self.BODY),
               dict(action='create', name=' has space', level='user', body=self.BODY),
               dict(action='create', name='-lead', level='user', body=self.BODY),
               dict(action='create', name='a' * 65, level='user', body=self.BODY),
               dict(action='create', name='', level='user', body=self.BODY),
               dict(action='create', name='ok/no', level='user', body=self.BODY),
               dict(action='create', name=name, level='user', **{'from': '-no'}),
               dict(action='edit', name=name, level='user'),
               dict(action='edit', name=name, level='user', body=['nope']),
               dict(action='edit', name=name, level='user', body='{}'),
               dict(action='set', name=name, level='user', key='gates.drift', value='on'),
               dict(action='set', name=name, level='user', key='routing.', value='x'),
               dict(action='set', name=name, level='user', key='Policy.timeout', value='1'),
               dict(action='set', name=name, level='user', key='policy.timeout', value='a\nb'),
               dict(action='set', name=name, level='user', key='policy.timeout', value='a\tb'),
               dict(action='set', name=name, level='user', key='policy.timeout', value='a\x00b'),
               dict(action='set', name=name, level='user', key='policy.timeout', value='a\x7fb'),
               dict(action='set', name=name, level='user', key='policy.timeout', value='\ud800'),
               dict(action='set', name=name, level='user', key='policy.timeout', value='-1'),
               dict(action='set', name=name, level='user', key='policy.timeout', value=15),
               dict(action='unset', name=name, level='user'),
               dict(action='rm', name=name, level='user', key='policy.timeout'),
               dict(action='use', level='user'),
               dict(action='use', name=name, level='user', none=True),
               dict(action='use', level='user', none=False),
               dict(action='use', level='user', none='yes'),
               dict(action='rm', name=name, level='user', repo='/etc'),
               dict(action='create', name=name, level='user', body=self.BODY, path='/etc')]
        for fields in bad:
            self.assertEqual(self.profiles(**fields)[0], 400, fields)
        self.assertEqual(self.post(['nope'], '/api/config/profiles')[0], 400)
        self.assertEqual(self.post({'action': 'rm', 'name': name, 'level': 'user'}, '/api/config/profiles')[0], 400)
        self.assertEqual(self.profiles(run='../x', action='rm', name=name, level='user')[0], 400)
        self.assertEqual(self.get('/api/config/profiles?run=zz')[0], 400)
        self.assertEqual(self.get(f'/api/config/profiles?run={self.key}&repo=/etc')[0], 400)
        self.assertEqual(self.calls(), [])
        status, obj, route, key = control.dispatch(
            self.server, self.fake, 'POST', '/api/config/profiles', ['nope'])
        self.assertEqual((status, route, key, self.calls()), (400, 'profiles', '', []))

    def test_an_oversized_body_is_refused_before_the_cli(self):
        overhead = len(json.dumps({'note': ''}, allow_nan=False).encode())
        fits = control.MAX_PROFILE_BODY - overhead
        self.assertEqual(len(json.dumps({'note': 'x' * fits}, allow_nan=False).encode()), control.MAX_PROFILE_BODY)

        def ask(n):
            return control.dispatch(self.server, self.fake, 'POST', '/api/config/profiles', {
                'run': self.key, 'action': 'create', 'name': 'wide', 'level': 'user',
                'body': {'note': 'x' * n}})

        status, body, route, key = ask(fits + 1)
        self.assertEqual((status, route, key), (400, 'profiles', self.key))
        self.assertIn('64', body['detail'])
        self.assertEqual(self.calls(), [])
        status, body = ask(fits)[:2]
        self.assertEqual(status, 200, body)
        self.assertEqual(self.calls()[-1]['stdin'], json.dumps({'note': 'x' * fits}, allow_nan=False))

    def test_a_deeply_nested_body_is_a_400_not_a_500(self):
        deep = 0
        for _ in range(980):
            deep = [deep]
        status, body, route, key = control.dispatch(self.server, self.fake, 'POST', '/api/config/profiles', {
            'run': self.key, 'action': 'create', 'name': 'deep', 'level': 'user', 'body': {'note': deep}})
        self.assertEqual((status, route), (400, 'profiles'), body)
        self.assertEqual(self.calls(), [])

    def test_without_the_token_is_refused(self):
        status, _body = self.post({'run': self.key, 'action': 'rm', 'name': 'work', 'level': 'user'},
                                  '/api/config/profiles', **{'X-Forge-UI-Token': None})
        self.assertEqual(status, 403)
        self.assertEqual(self.calls(), [])

    def test_unknown_run_or_repo(self):
        self.assertEqual(self.profiles(run='000000000000', action='rm', name='work', level='user')[0], 404)
        self.assertEqual(self.get('/api/config/profiles?run=000000000000')[0], 404)
        (self.plan / 'repo').write_text('/no/such/dir\n')
        self.assertEqual(self.profiles(action='rm', name='work', level='user')[0], 409)
        self.assertEqual(self.profiles(action='rm', name='work', level='repo')[0], 409)
        self.assertEqual(self.get(f'/api/config/profiles?run={self.key}')[0], 409)
        self.assertEqual(self.calls(), [])

    def test_a_removed_worktree_uses_the_main_checkout(self):
        main = ConfigTests.removed_worktree(self)
        (self.solo / 'repo').write_text(f'{main}\n')
        self.behave(stdout=json.dumps({'default': {'name': None, 'level': None}, 'profiles': []}))
        self.assertEqual(self.get(f'/api/config/profiles?run={self.key}')[0], 200)
        self.assertEqual(self.last_argv(), ['config', 'profile', 'list', '--json', '--repo', str(main)])
        self.assertEqual(self.profiles(action='rm', name='work', level='repo')[0], 200)
        self.assertEqual(self.last_argv(), ['config', 'profile', 'rm', 'work', '--repo', str(main), '--json'])
        self.assertEqual(self.profiles(action='rm', name='work', level='user')[0], 200)
        self.assertEqual(self.last_argv(), ['config', 'profile', 'rm', 'work', '--json'])

    def test_config_lock_serializes_writes_and_not_lists(self):
        lock = control._server_lock(self.server, 'config_lock')
        lock.acquire()
        try:
            self.assertEqual(self.profiles(action='rm', name='work', level='user'), (409, {'error': 'busy'}))
            self.assertEqual(self.calls(), [])
            self.behave(stdout=json.dumps({'default': {'name': None, 'level': None}, 'profiles': []}))
            self.assertEqual(self.get(f'/api/config/profiles?run={self.key}')[0], 200)
            self.assertEqual(len(self.calls()), 1)
        finally:
            lock.release()

    def test_cli_exit_codes(self):
        fields = dict(action='rm', name='work', level='user')
        self.behave(code=3, stderr='forge config: no\n', stdout='{"ok": false, "error": "no"}')
        status, body = self.profiles(**fields)
        self.assertEqual((status, body['error'], body['result']['ok']), (409, 'refused', False))
        self.behave(code=2, stderr='forge config: bad\n', stdout='{"ok": false, "error": "bad"}')
        self.assertEqual(self.profiles(**fields)[0], 400)
        self.behave(code=1, stderr='forge config: boom\n', stdout='{"ok": false}')
        self.assertEqual(self.profiles(**fields)[0], 502)


class ProfileCliTests(EndpointCase):
    """The real `forge config profile`, through the HTTP server, on a temp config home and git repo."""

    def setUp(self):
        super().setUp()
        self.root = os.path.realpath(self.repo)
        git(self.root, 'init', '-q')
        (self.plan / 'repo').write_text(self.root + '\n')
        self.cfg = self.base / 'xdg'
        home = self.base / 'home'
        home.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('FORGE_DECIDE', 'FORGE_JEV')) and key != 'FORGE_REGISTRY'}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(self.cfg),
                   XDG_CACHE_HOME=str(self.base / 'cache'), XDG_DATA_HOME=str(self.base / 'data'),
                   XDG_STATE_HOME=str(self.base / 'state'))
        self.env = env
        for patcher in (patch.dict(os.environ, env, clear=True),
                        patch.object(ui_server, 'forge_cli', ROOT / 'forge')):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.user_file = self.cfg / 'forge' / 'decide.json'
        self.repo_file = Path(self.root) / '.forge' / 'decide.json'

    def profiles(self, **body):
        return self.post({'run': self.key, **body}, '/api/config/profiles')

    def test_list_returns_every_profile_with_its_body(self):
        user = {'policy': {'timeout': 15}, 'note': 'user'}
        repo_body = {'policy': {'timeout': 9}, 'note': 'repo'}
        self.assertEqual(self.profiles(action='create', name='shared', level='repo', body=repo_body)[0], 200)
        self.assertEqual(self.profiles(action='create', name='shared', level='user', body=user)[0], 200)
        self.assertEqual(self.profiles(action='use', name='shared', level='user')[0], 200)
        status, listed = self.get(f'/api/config/profiles?run={self.key}')
        self.assertEqual(status, 200, listed)
        rows = {row['name']: row for row in listed['profiles']}
        self.assertEqual(rows['shared'], {'name': 'shared', 'level': 'user', 'shadowed': ['repo'],
                                           'default': True, 'body': user})
        builtin = json.loads((ROOT / 'scripts/forge_decide/builtin_profiles.json').read_text())['profiles']
        for name in ('balanced', 'lite'):
            self.assertEqual(rows[name]['level'], 'builtin', name)
            self.assertEqual(rows[name]['shadowed'], [], name)
            self.assertIs(rows[name]['default'], False, name)
            self.assertEqual(rows[name]['body'], builtin[name], name)
        self.assertEqual(listed['default'], {'name': 'shared', 'level': 'user'})

    def test_every_action_round_trips_at_both_levels(self):
        created = {'policy': {'timeout': 15}, 'note': 'kept'}
        edited = {'policy': {'timeout': 20}, 'note': 'edited'}
        for level, path in (('user', self.user_file), ('repo', self.repo_file)):
            status, answer = self.profiles(action='create', name='work', level=level, body=created)
            self.assertEqual((status, answer.get('ok'), answer.get('level'), answer.get('body')),
                             (200, True, level, created), answer)
            status, answer = self.profiles(action='edit', name='work', level=level, body=edited)
            self.assertEqual((status, answer.get('body')), (200, edited), answer)
            status, answer = self.profiles(action='set', name='work', level=level,
                                           key='policy.retry_failed', value='4')
            self.assertEqual(status, 200, answer)
            self.assertEqual(answer['body']['policy']['retry_failed'], 4, level)
            self.assertEqual(answer['body']['policy']['timeout'], 20, level)
            self.assertEqual(answer['body']['note'], 'edited', level)
            status, answer = self.profiles(action='unset', name='work', level=level, key='policy.timeout')
            self.assertEqual(status, 200, answer)
            self.assertNotIn('timeout', answer['body']['policy'])
            self.assertEqual(answer['body']['policy']['retry_failed'], 4, level)
            status, answer = self.profiles(action='use', name='work', level=level)
            self.assertEqual(status, 200, answer)
            self.assertEqual(json.loads(path.read_text())['default_profile'], 'work', level)
            status, answer = self.profiles(action='use', level=level, none=True)
            self.assertEqual((status, answer.get('name'), answer.get('body')), (200, None, None), answer)
            self.assertNotIn('default_profile', json.loads(path.read_text()))
            status, answer = self.profiles(action='rm', name='work', level=level)
            self.assertEqual((status, answer.get('ok'), answer.get('body')), (200, True, None), answer)
            self.assertNotIn('profiles', json.loads(path.read_text()), level)

    def test_create_from_a_builtin_copies_its_body(self):
        builtin = json.loads((ROOT / 'scripts/forge_decide/builtin_profiles.json').read_text())['profiles']['balanced']
        for level, path in (('user', self.user_file), ('repo', self.repo_file)):
            status, answer = self.profiles(action='create', name='work', level=level, **{'from': 'balanced'})
            self.assertEqual(status, 200, answer)
            self.assertEqual(answer['body'], builtin, level)
            self.assertEqual(json.loads(path.read_text())['profiles']['work'], builtin, level)

    def test_a_profile_created_through_the_api_matches_the_cli_file(self):
        body = {'policy': {'timeout': 15}, 'note': 'kept'}
        stdin = json.dumps(body, allow_nan=False)
        for level in ('user', 'repo'):
            status, answer = self.profiles(action='create', name='kept', level=level, body=body)
            self.assertEqual(status, 200, answer)
            self.assertEqual(answer['body'], body)
        self.assertEqual(self.user_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.repo_file.stat().st_mode & 0o777, 0o644)
        other_cfg = self.base / 'cli-xdg'
        other_repo = self.base / 'cli-repo'
        other_repo.mkdir()
        git(other_repo, 'init', '-q')
        other_root = os.path.realpath(other_repo)
        env = dict(self.env, XDG_CONFIG_HOME=str(other_cfg), HOME=str(self.base / 'cli-home'))

        def cli(*args):
            done = subprocess.run([str(ROOT / 'forge'), 'config', *args], input=stdin,
                                  capture_output=True, text=True, env=env)
            self.assertEqual(done.returncode, 0, done.stderr or done.stdout)
            return done

        cli('profile', 'create', 'kept', '--file', '-', '--json')
        cli('profile', 'create', 'kept', '--file', '-', '--repo', other_root, '--json')
        self.assertEqual((other_cfg / 'forge' / 'decide.json').read_bytes(), self.user_file.read_bytes())
        self.assertEqual((Path(other_root) / '.forge' / 'decide.json').read_bytes(), self.repo_file.read_bytes())

    def test_a_builtin_edit_is_refused_and_writes_nothing(self):
        # The CLI refuses a built-in with no copy (exit 3), which maps to 409 the way every
        # other refusal does.
        for level in ('user', 'repo'):
            status, body = self.profiles(action='edit', name='balanced', level=level,
                                         body={'policy': {'timeout': 1}})
            self.assertEqual(status, 409, body)
            self.assertEqual(body['error'], 'refused')
            self.assertIn('read-only', body['detail'])
            self.assertIs(body['result']['ok'], False)
            self.assertIn('read-only', body['result']['error'])
        self.assertFalse(self.user_file.exists())
        self.assertFalse(self.repo_file.exists())


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
