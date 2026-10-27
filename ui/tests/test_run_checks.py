"""Where each task of a run is (RunDetail's task.now and task.waiting, run.scheduler), the checks its agents ran
(/tasks/<id>/checks, /executions/<name>/checks) and the environment problems they met (/environment)."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from forge_ui import reader, server, transcripts
import run_fixture as F

BRIEF = '''Read forge run dirs into plain dicts.

## Acceptance criteria

- `python3 -m unittest tests.test_reader` passes.
- `npm run lint` is clean.
'''


def jsonl(*values):
    return ''.join(json.dumps(v) + '\n' for v in values)


def codex_command(ident, command, output, code):
    return jsonl({'type': 'item.started', 'item': {'id': ident, 'type': 'command_execution', 'command': command, 'status': 'in_progress'}},
                 {'type': 'item.completed', 'item': {'id': ident, 'type': 'command_execution', 'command': command,
                                                     'aggregated_output': output, 'exit_code': code,
                                                     'status': 'completed' if code == 0 else 'failed'}})


class Base(unittest.TestCase):
    def setUp(self):
        transcripts._CACHE.clear()
        transcripts._LIVE.clear()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)


class Now(Base):
    """A running task says which agent is at work, the call it is making and since when, and when its log last moved."""

    def setUp(self):
        super().setUp()
        self.plan = F.make_plan(self.base)
        self.log = self.plan / 'tasks/graph/qa.log'
        stream = (F.TRANSCRIPTS / 'claude-stream.jsonl').read_text().splitlines(keepends=True)
        self.log.write_text(''.join(stream[:3]))                 # Bash called, no result yet
        os.utime(self.log, (F.T0 + 2000, F.T0 + 2000))

    def graph(self):
        return next(t for t in reader.detail(self.plan)['tasks'] if t['id'] == 'graph')

    def test_a_call_on_the_logs_last_line_started_when_the_log_last_moved(self):
        self.assertEqual(self.graph()['now'], {
            'role': 'qa', 'execution': F.GRAPH_QA, 'attempt': 1, 'phase': 'model', 'phase_started': F.T0 + 1715.0,
            'started': F.T0 + 1710.0, 'last_output_at': F.T0 + 2000,
            'call': {'kind': 'command', 'command': 'python3 -m unittest tests.test_reader', 'tool': 'Bash',
                     'started': F.T0 + 2000, 'check': 'test'}})

    def test_the_clock_says_when_the_call_started(self):
        lines = self.log.read_text().splitlines(keepends=True)
        start = len(''.join(lines).encode())
        (self.plan / 'tasks/graph/qa.clock').write_text(f'{F.T0 + 1711} 0\n{F.T0 + 1830} {start}\n')
        self.assertEqual(self.graph()['now']['call']['started'], F.T0 + 1830)

    def test_between_calls_there_is_no_call_and_a_task_not_running_has_no_now(self):
        stream = (F.TRANSCRIPTS / 'claude-stream.jsonl').read_text().splitlines(keepends=True)
        self.log.write_text(''.join(stream[:4]))                 # the Bash call was answered
        self.assertIsNone(self.graph()['now']['call'])
        self.assertIsNone(next(t for t in reader.detail(self.plan)['tasks'] if t['id'] == 'reader')['now'])


class Waiting(Base):
    """Why a task that is not running is not: the run dir's state, as the scheduler reads it."""

    def plan(self, rows, live=True, **files):
        plan = self.base / 'plan'
        F.write(plan / 'run_id', 'wait\n')
        F.write(plan / 'tasks.tsv', ''.join('\t'.join([ident, deps, 'low', paths, 'luna', 'opus', ident]) + '\n'
                                            for ident, deps, paths, _ in rows))
        for ident, _, _, status in rows:
            F.write(plan / f'tasks/{ident}/status', status + '\n')
        if live:
            F.write(plan / 'scheduler.live', {'pid': os.getpid()})       # this process: alive
        for name, value in files.items():
            F.write(plan / name.replace('__', '/').replace('_dot_', '.'), value)
        return {t['id']: t['waiting'] for t in reader.detail(plan)['tasks']}, reader.detail(plan)['scheduler']

    ROWS = [('t1', '-', 'ui/app.js', 'RUNNING'),
            ('t2', 't1', 'ui/a.css', 'PENDING'),                         # waits on t1
            ('t3', '-', 'ui', 'PENDING'),                                # shares files with t1
            ('t4', '-', 'docs/x.md', 'PENDING'),                         # nothing holds it
            ('t5', '-', 'docs/y.md', 'FAIL'),
            ('t6', 't5', 'docs/z.md', 'PENDING')]                        # waits on a task that stopped

    def test_each_reason_a_live_run_holds_a_task(self):
        waiting, scheduler = self.plan(self.ROWS)
        self.assertEqual(waiting, {'t1': None, 't2': {'kind': 'deps', 'on': ['t1'], 'stopped': []},
                                   't3': {'kind': 'overlap', 'on': ['t1']}, 't4': {'kind': 'next'}, 't5': None,
                                   't6': {'kind': 'deps', 'on': ['t5'], 'stopped': ['t5']}})
        self.assertEqual(scheduler, {'live': True, 'drained': False, 'stopping': None, 'pauses': [], 'max_parallel': None})
        self.assertEqual(self.plan(self.ROWS, max_parallel='1\n')[0]['t4'], {'kind': 'slot', 'running': 1, 'max_parallel': 1})
        self.assertEqual(self.plan(self.ROWS, drain='')[0]['t4'], {'kind': 'drained'})
        self.assertEqual(self.plan(self.ROWS, stop='graceful\n')[0]['t4'], {'kind': 'stopping', 'mode': 'graceful'})

    def test_an_infrastructure_pause_holds_its_tasks_with_its_countdown(self):
        pause = {'scope': 'model:codex/gpt-6', 'class': 'rate_limit', 'harness': 'codex', 'model': 'gpt-6', 'tasks': ['t4'],
                 'cycle': 2, 'until': F.T0 + 90, 'exhausted': False}
        waiting, scheduler = self.plan(self.ROWS, infra_dot_paused={'scopes': [pause]})
        self.assertEqual(waiting['t4'], dict(pause, kind='infra'))
        self.assertEqual(scheduler['pauses'], [pause])

    def test_a_task_the_retry_policy_stopped_says_so_even_with_no_scheduler(self):
        files = {'tasks__t5__escalation_dot_stop': '3\tsol:xhigh\n', 'tasks__t5__split_dot_suggested': 'Split the parser from the writer.\n'}
        waiting, scheduler = self.plan(self.ROWS, live=False, **files)
        self.assertEqual(waiting['t5'], {'kind': 'escalation', 'attempts': 3, 'model': 'sol:xhigh', 'split': 'Split the parser from the writer.'})
        self.assertEqual([k for k, v in waiting.items() if v], ['t5'])          # nothing else waits on a scheduler that is gone
        self.assertFalse(scheduler['live'])

    def test_a_dead_scheduler_is_not_live(self):
        process_gone = 2 ** 22 + 12345                                    # above any pid this machine hands out
        _, scheduler = self.plan(self.ROWS, live=False, scheduler_dot_live={'pid': process_gone})
        self.assertFalse(scheduler['live'])


class Checks(Base):
    def setUp(self):
        super().setUp()
        self.plan = F.make_plan(self.base)
        F.write(self.plan / 'tasks/reader/prompt.md', BRIEF)
        self.catalog = reader.Catalog(plans=[self.plan], registry=False)
        self.key = reader.run_key(self.plan)

    def get(self, path):
        return server.api_get(self.catalog, f'/api/runs/{self.key}' + path)

    def test_a_tasks_checks_attempt_by_attempt_against_its_acceptance_criteria(self):
        data = self.get('/tasks/reader/checks')
        self.assertEqual(data['criteria'], [{'command': 'python3 -m unittest tests.test_reader', 'kind': 'test'},
                                            {'command': 'npm run lint', 'kind': 'lint'}])
        first, second = data['attempts']
        self.assertEqual([(r['role'], [(c['command'], c['exit'], c['failed']) for c in r['checks']]) for r in first['executions']], [
            ('dwarf', [('python3 -m unittest -q', 1, True)]),
            ('qa', [('python3 -m unittest tests.test_reader', None, True)])])
        self.assertEqual([(c['command'], c['status'], c['by']) for c in first['criteria']], [
            ('python3 -m unittest tests.test_reader', 'ran', {'execution': F.READER_QA1, 'role': 'qa', 'index': 1}),
            ('npm run lint', 'missing', None)])
        self.assertEqual((second['attempt'], [c['status'] for c in second['criteria']]), (2, ['missing', 'missing']))
        self.assertIsNone(self.get('/tasks/nope/checks'))

    def test_an_executions_checks_and_the_roles_that_have_none(self):
        data = self.get(f'/executions/{F.READER_DWARF1}/checks')
        self.assertEqual((data['role'], data['attempt'], data['harness'], data['call']), ('dwarf', 1, 'codex', None))
        self.assertEqual([(c['index'], c['kind']) for c in data['checks']], [(4, 'test')])      # its place in the conversation
        self.assertEqual([c['status'] for c in data['criteria']], ['similar', 'missing'])      # unittest, another module
        self.assertIsNone(self.get(f'/executions/{F.READER_TASK1}/checks'))                     # a task execution runs no agent
        review = self.get(f'/executions/{F.REVIEW_OLD_QA}/checks')
        self.assertEqual((review['owner']['kind'], review['criteria']), ('final_review', []))   # no brief to hold it to


class Environment(Base):
    def test_a_problem_met_in_several_tasks_is_listed_once_with_each_task(self):
        plan = F.make_plan(self.base)
        pools = 'Error response from daemon: all predefined address pools have been fully subnetted\n'
        F.write(plan / f'tasks/reader/attempts/{F.READER_DWARF1}/dwarf.log', codex_command('c1', 'npm run db:up', pools, 1))
        F.write(plan / f'tasks/docs/attempts/{F.DOCS_DWARF}/dwarf.log',
                codex_command('c1', 'docker compose up -d db', pools, 1)
                + codex_command('c2', 'npm test', 'Error: listen EADDRINUSE: address already in use :::5432\n', 1))
        data = server.api_get(reader.Catalog(plans=[plan], registry=False), f'/api/runs/{reader.run_key(plan)}/environment')
        self.assertEqual([(p['problem'], p['tasks'], p['count']) for p in data['problems']],
                         [('docker-pools', ['reader', 'docs'], 2), ('port', ['docs'], 1)])
        self.assertEqual(data['problems'][0]['hits'][0], {
            'task': 'reader', 'attempt': 1, 'role': 'dwarf', 'execution': F.READER_DWARF1, 'index': 0,
            'command': 'npm run db:up', 'line': pools.strip()})


if __name__ == '__main__':
    unittest.main()
