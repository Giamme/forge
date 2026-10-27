import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from forge_ui import executions, findings, reader
from forge_ui.reader import (detail, execution_detail, execution_transcript, final_review_detail, run_findings,
                             summarize, task_detail)
import run_fixture as F


T0 = F.T0
ZERO = dict(P0=0, P1=0, P2=0, P3=0, unlabelled=0, plausible=0, confirmed=0, worst=None)


def counts(**values):
    return dict(ZERO, **values)


class FixtureCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.plan = F.make_plan(self.base)
        self.solo = F.make_solo(self.base)

    def task(self, data, ident):
        return next(task for task in data['tasks'] if task['id'] == ident)

    def execs(self, data, ident):
        return {item['id']: item for item in self.task(data, ident)['executions']}

    def write(self, path, value, at=T0 + 6000):
        F.write(path, value, at)

    def all_outputs(self, run):
        data = detail(run)
        outputs = [data, summarize(run)]
        names = [item['id'] for task in data['tasks'] for item in task['executions']]
        names += [item['execution'] for task in data['tasks'] for item in task['shadow'] if item['execution']]
        names += [item['id'] for item in data['timeline']['runs'] + data['timeline']['verifications']]
        names += [item['execution'] for item in data['final_reviews'] if item['execution']]
        for task in data['tasks']:
            outputs.append(task_detail(run, task['id']))
        for name in names:
            outputs.append(execution_detail(run, name))
        for review in data['final_reviews']:
            outputs.append(final_review_detail(run, review['name']))
        return outputs


class ExecutionTests(FixtureCase):
    def test_one_execution_in_full(self):
        execs = self.execs(detail(self.plan), 'reader')
        self.assertEqual(execs[F.READER_DWARF2], {
            'id': F.READER_DWARF2, 'role': 'dwarf', 'parent': F.READER_TASK2, 'attempt': 2, 'shadow': False,
            'state': 'done', 'started': T0 + 630, 'ended': T0 + 930, 'duration_s': 300.0, 'phase': 'complete',
            'phase_started': T0 + 930,
            'phases': {'preparation': 1.25, 'preflight': 0.5, 'model': 297.0, 'snapshot': 0.75, 'verification': 0.0,
                       'finalization': 0.5},
            'exit_code': 0, 'infra_class': None, 'timed_out': False,
            'model': {'spec': 'sol:xhigh', 'alias': 'sol', 'harness': 'codex', 'model': 'sol-model', 'effort': 'xhigh',
                      'clamped': None},
            'tokens': {'input': 150000, 'cached': 100000, 'output': 9000}, 'prompt_bytes': 4096,
            'verdict': None, 'findings': None})
        self.assertEqual(list(execs), [F.READER_TASK1, F.READER_DWARF1, F.READER_QA1,
                                       F.READER_TASK2, F.READER_DWARF2, F.READER_QA2])
        task = execs[F.READER_TASK1]
        self.assertIsNone(task['model'])
        self.assertIsNone(task['tokens'])
        self.assertEqual(task['exit_code'], 1)
        # `<harness default>` is no effort: the spec is the bare alias.
        self.assertEqual(execs[F.READER_QA1]['model']['spec'], 'opus')
        self.assertIsNone(execs[F.READER_QA1]['model']['effort'])

    def test_nesting_by_dataset_parent_and_by_time(self):
        data = detail(self.plan)
        reader_execs = self.execs(data, 'reader')
        for child, parent in ((F.READER_DWARF1, F.READER_TASK1), (F.READER_QA1, F.READER_TASK1),
                              (F.READER_DWARF2, F.READER_TASK2), (F.READER_QA2, F.READER_TASK2)):
            self.assertEqual(reader_execs[child]['parent'], parent, child)
        self.assertIsNone(reader_execs[F.READER_TASK1]['parent'])  # a task execution is a root
        docs = self.execs(data, 'docs')
        self.assertFalse((self.plan / 'tasks/docs/attempts' / F.DOCS_DWARF / 'dataset.json').exists())
        self.assertEqual(docs[F.DOCS_DWARF]['parent'], F.DOCS_TASK)
        self.assertEqual(docs[F.DOCS_QA]['parent'], F.DOCS_TASK)
        self.assertEqual(docs[F.DOCS_QA]['attempt'], 1)
        # A parent the dataset names that does not exist falls back to time as well.
        self.write(self.plan / 'tasks/reader/attempts' / F.READER_DWARF2 / 'dataset.json',
                   dict(parent='task-' + F.uid(999)))
        self.assertEqual(self.execs(detail(self.plan), 'reader')[F.READER_DWARF2]['parent'], F.READER_TASK2)
        # Started outside every task execution: no parent, no attempt.
        F.execution(self.plan / 'tasks/docs', 'dwarf-' + F.uid(45), 1600.0, 1700.0, dataset=False)
        stray = self.execs(detail(self.plan), 'docs')['dwarf-' + F.uid(45)]
        self.assertEqual((stray['parent'], stray['attempt']), (None, None))

    def test_attempts_match_rows_and_survive_escalation(self):
        data = detail(self.plan)
        task = self.task(data, 'reader')
        self.assertEqual(task['escalations'], [{'at': T0 + 615, 'attempt': 1, 'from': 'luna', 'to': 'sol:xhigh', 'source': 'policy'}])
        self.assertEqual([item['attempt'] for item in task['executions']], [1, 1, 1, 2, 2, 2])
        self.assertEqual(self.execs(data, 'reader')[F.READER_DWARF1]['model']['spec'], 'luna')
        # Rows win over dataset.json: here the dataset disagrees and is ignored.
        self.write(self.plan / 'tasks/reader/attempts' / F.READER_TASK2 / 'dataset.json',
                   dict(parent=F.PARALLEL, attempt_start=7))
        self.assertEqual([item['attempt'] for item in self.task(detail(self.plan), 'reader')['executions']],
                         [1, 1, 1, 2, 2, 2])
        # Without attempts.tsv, dataset.json numbers them: attempt_start + 1, or attempt_start on a resume.
        (self.plan / 'tasks/reader/attempts.tsv').unlink()
        self.write(self.plan / 'tasks/reader/attempts' / F.READER_TASK2 / 'dataset.json',
                   dict(parent=F.PARALLEL, attempt_start=1, resume='qa'))
        self.assertEqual([item['attempt'] for item in self.task(detail(self.plan), 'reader')['executions']],
                         [1, 1, 1, 1, 1, 1])
        # A row too far from the end (over 120 s) is not that execution's.
        self.write(self.plan / 'tasks/docs/attempts.tsv', f'{int(T0 + 1500 + 121)}\t5\tFAIL\t\tluna\n')
        self.assertEqual(self.execs(detail(self.plan), 'docs')[F.DOCS_TASK]['attempt'], None)
        self.write(self.plan / 'tasks/docs/attempts.tsv', f'{int(T0 + 1500 + 120)}\t5\tFAIL\t\tluna\n')
        self.assertEqual(self.execs(detail(self.plan), 'docs')[F.DOCS_QA]['attempt'], 5)

    def test_assign_attempts_rules(self):
        def make(ident, ended, state='done', dataset=None):
            item = executions.Execution(id=ident, role='task', parent=None, attempt=None, started=0.0,
                                        ended=ended, state=state)
            item.dataset, item.rel = dataset, ''
            return item
        a, b, c = make('task-a', 100.0), make('task-b', 300.0), make('task-c', None, 'running')
        rows = [dict(at=101.0, attempt=1), dict(at=102.0, attempt=9), dict(at=299.0, attempt=2)]
        executions.assign_attempts([a, b, c], rows, current=3)
        self.assertEqual((a['attempt'], b['attempt'], c['attempt']), (1, 2, 3))
        d = make('task-d', None, 'orphaned', dict(attempt_start=4, resume=None))
        e = make('task-e', None, 'orphaned')
        executions.assign_attempts([d, e], [], current=3)
        self.assertEqual((d['attempt'], e['attempt']), (5, None))

    def test_running_done_and_orphaned(self):
        data = detail(self.plan)
        graph = self.execs(data, 'graph')
        self.assertEqual([graph[name]['state'] for name in (F.GRAPH_TASK, F.GRAPH_DWARF, F.GRAPH_QA)],
                         ['running', 'done', 'running'])
        qa = graph[F.GRAPH_QA]
        self.assertEqual((qa['phase'], qa['exit_code'], qa['ended'], qa['duration_s']), ('model', None, None, None))
        self.assertEqual(qa['phase_started'], T0 + 1715)
        # No copy yet: the live qa.resolved of the task dir names the model.
        self.assertEqual(qa['model']['spec'], 'opus:high')
        self.assertIsNone(qa['verdict'])
        self.assertEqual(qa['attempt'], 1)
        # An execution that never finished, in a task that is no longer running, is orphaned.
        F.execution(self.plan / 'tasks/docs', 'dwarf-' + F.uid(44), 1100.0, dataset=False)
        # In a running task only the newest execution of a role can be in flight.
        F.execution(self.plan / 'tasks/graph', 'task-' + F.uid(33), 1250.0, phase='dispatch')
        data = detail(self.plan)
        self.assertEqual(self.execs(data, 'docs')['dwarf-' + F.uid(44)]['state'], 'orphaned')
        self.assertEqual(self.execs(data, 'docs')['dwarf-' + F.uid(44)]['parent'], F.DOCS_TASK)
        graph = self.execs(data, 'graph')
        self.assertEqual(graph['task-' + F.uid(33)]['state'], 'orphaned')
        self.assertEqual(graph[F.GRAPH_TASK]['state'], 'running')
        # The task stops: everything still open in it is orphaned.
        self.write(self.plan / 'tasks/graph/status', 'INTERRUPTED\n')
        states = {item['state'] for item in self.task(detail(self.plan), 'graph')['executions']}
        self.assertEqual(states, {'orphaned', 'done'})
        # A child cannot outlive its parent task execution.
        self.write(self.plan / 'tasks/graph/status', 'RUNNING\n')
        metrics = json.loads((self.plan / 'tasks/graph/attempts' / F.GRAPH_TASK / 'metrics.json').read_text())
        metrics.update(exit_code=0, elapsed_s=dict(metrics['elapsed_s'], total=1000.0))
        self.write(self.plan / 'tasks/graph/attempts' / F.GRAPH_TASK / 'metrics.json', metrics)
        self.assertEqual(self.execs(detail(self.plan), 'graph')[F.GRAPH_QA]['state'], 'orphaned')

    def test_timed_out_infra_and_tokens(self):
        attempt = self.plan / 'tasks/reader/attempts' / F.READER_QA1
        self.write(attempt / 'qa.timedout', '')
        metrics = json.loads((attempt / 'metrics.json').read_text())
        metrics.update(infra_class='quota', input_tokens=None, cached_input_tokens=None, output_tokens=None)
        self.write(attempt / 'metrics.json', metrics)
        qa = self.execs(detail(self.plan), 'reader')[F.READER_QA1]
        self.assertTrue(qa['timed_out'])
        self.assertEqual(qa['infra_class'], 'quota')
        self.assertIsNone(qa['tokens'])

    def test_findings_per_qa_execution_and_task(self):
        data = detail(self.plan)
        reader_execs = self.execs(data, 'reader')
        self.assertEqual((reader_execs[F.READER_QA1]['verdict'], reader_execs[F.READER_QA1]['findings']),
                         ('FAIL', counts(P1=1, confirmed=1, plausible=1, worst='P1')))
        self.assertEqual((reader_execs[F.READER_QA2]['verdict'], reader_execs[F.READER_QA2]['findings']),
                         ('PASS', counts(plausible=1)))
        self.assertIsNone(reader_execs[F.READER_DWARF1]['findings'])
        self.assertEqual(self.task(data, 'reader')['findings'], counts(plausible=1))
        page = self.task(data, 'page')
        self.assertEqual(page['findings'], counts(P3=1, confirmed=1, worst='P3'))
        self.assertEqual(page['gate'], {'threshold': 'P2', 'decision': 'keep', 'worst': 'P3', 'n_confirmed': 1,
                                        'n_unlabelled': 0, 'n_below': 1})
        self.assertTrue(page['known_issues'])
        self.assertFalse(self.task(data, 'reader')['known_issues'])
        docs = self.task(data, 'docs')
        self.assertEqual(docs['findings'], counts(confirmed=1, unlabelled=1))
        self.assertEqual(self.execs(data, 'docs')[F.DOCS_QA]['findings'], counts(confirmed=1, unlabelled=1))
        self.assertIsNone(self.task(data, 'server')['findings'])
        self.assertIsNone(self.task(data, 'graph')['findings'])
        self.write(self.plan / 'tasks/page/qa.gate', 'P2\tkeep\n')
        data = detail(self.plan)
        self.assertIsNone(self.task(data, 'page')['gate'])
        self.assertTrue(any('tasks/page/qa.gate' in warning for warning in data['warnings']))

    def test_shadow_disagreement(self):
        data = detail(self.plan)
        self.assertEqual(self.task(data, 'page')['shadow'], [{
            'attempt': 1, 'k': 0, 'spec': 'sol:xhigh', 'primary': 'opus', 'started': T0 + 612.0, 'status': 'done',
            'verdict': 'FAIL', 'reviewed': 'c' * 40, 'pick': 'other_family', 'execution': F.PAGE_SHADOW_QA,
            'findings': counts(P1=1, confirmed=1, worst='P1'), 'primary_verdict': 'PASS', 'agreement': 'disagree',
            'ended': T0 + 1013.0, 'duration_s': 400.0}])
        # Shadow executions are not the task's own executions.
        self.assertNotIn(F.PAGE_SHADOW_QA, self.execs(data, 'page'))
        self.write(self.plan / 'shadow/page/1-0/qa.last', 'FORGE_VERDICT: PASS\n')
        self.write(self.plan / 'shadow/page/1-0/shadow.json', dict(attempt='1'))
        entry = self.task(detail(self.plan), 'page')['shadow'][0]
        self.assertEqual((entry['agreement'], entry['started']), ('agree', T0 + 613.0))  # started from the execution
        # Still running: no shadow.status yet.
        (self.plan / 'shadow/page/1-0/shadow.status').unlink()
        (self.plan / 'shadow/page/1-0/qa.last').unlink()
        F.execution(self.plan / 'shadow/page/1-0', 'qa-' + F.uid(24), 1100.0, shadow=True)
        entry = self.task(detail(self.plan), 'page')['shadow'][0]
        self.assertEqual((entry['execution'], entry['agreement'], entry['ended']), ('qa-' + F.uid(24), 'unknown', None))
        self.assertEqual(execution_detail(self.plan, 'qa-' + F.uid(24))['state'], 'running')
        self.assertEqual(execution_detail(self.plan, F.PAGE_SHADOW_QA)['state'], 'done')

    def test_final_reviews_newest_published_and_running(self):
        data = detail(self.plan)
        reviews = data['final_reviews']
        self.assertEqual([item['name'] for item in reviews], [F.REVIEW_NEW, F.REVIEW_OLD, F.INTEGRATE])
        self.assertEqual([item['state'] for item in reviews], ['running', 'done', 'done'])
        self.assertEqual([item['published'] for item in reviews], [False, True, False])
        self.assertEqual([item['source'] for item in reviews], ['review', 'review', 'integrate'])
        self.assertEqual(data['final_review'], reviews[0])
        self.assertEqual(summarize(self.plan)['final_review'], 'FAIL')
        self.assertEqual(reviews[1], {
            'name': F.REVIEW_OLD, 'source': 'review', 'at': 1760602000.0, 'published': True, 'verdict': 'FAIL',
            'state': 'done', 'spec': 'opus', 'threshold': 'P2', 'base': 'a' * 40, 'tip': 'b' * 40,
            'integration': None, 'outdated': False, 'merged_since': [],
            'gate': {'threshold': 'P2', 'decision': 'keep', 'worst': 'P1', 'n_confirmed': 2, 'n_unlabelled': 0,
                     'n_below': 0},
            'findings': counts(P1=1, P2=1, confirmed=2, worst='P1'), 'execution': F.REVIEW_OLD_QA,
            'started': T0 + 2010, 'ended': T0 + 2400, 'duration_s': 390.0})
        self.assertEqual((reviews[0]['threshold'], reviews[0]['execution'], reviews[0]['ended']),
                         (None, F.REVIEW_NEW_QA, None))
        # No verdict yet, so the newest review is not outdated either way. The integrate review's tip is the
        # candidate, not the integration tip, and nothing merged after it started.
        self.assertIsNone(reviews[0]['outdated'])
        self.assertEqual(reviews[0]['merged_since'], [])
        self.assertIsNone(reviews[0]['integration'])
        self.assertIs(reviews[2]['outdated'], False)
        self.assertEqual(reviews[2]['merged_since'], [])
        self.assertNotEqual(reviews[2]['tip'], 'b' * 40)
        running = execution_detail(self.plan, F.REVIEW_NEW_QA)
        self.assertEqual((running['state'], running['model']['spec'], running['owner']),
                         ('running', 'opus', {'kind': 'final_review', 'id': F.REVIEW_NEW}))
        self.assertEqual(running['prompt'], 'Review the combined result again.\n')
        # The newest review without its prompt was never started; a verdict makes it done.
        (self.plan / F.REVIEW_NEW / 'qa.input').unlink()
        self.assertEqual(detail(self.plan)['final_reviews'][0]['state'], 'orphaned')
        self.write(self.plan / F.REVIEW_NEW / 'qa.infra', 'class=quota\n')
        self.assertEqual(detail(self.plan)['final_reviews'][0]['state'], 'infra')
        self.write(self.plan / F.REVIEW_NEW / 'verdict', 'UNKNOWN\n')
        data = detail(self.plan)
        self.assertEqual((data['final_review']['state'], data['final_review']['verdict']), ('done', 'UNKNOWN'))
        self.assertEqual(execution_detail(self.plan, F.REVIEW_NEW_QA)['state'], 'orphaned')
        self.assertIsNone(data['timeline']['ended'])  # the parallel run and graph still run
        # A link that leaves the plan publishes nothing.
        outside = self.base / 'elsewhere'
        outside.mkdir()
        (self.plan / 'final-review').unlink()
        (self.plan / 'final-review').symlink_to(outside)
        self.assertEqual([item['published'] for item in detail(self.plan)['final_reviews']], [False, False, False])
        (self.plan / 'final-review').unlink()
        (self.plan / 'final-review').symlink_to(F.INTEGRATE + '/final-review')
        self.assertEqual([item['published'] for item in detail(self.plan)['final_reviews']], [False, False, True])
        # Only the newest review can be in flight: an older one with a prompt and no verdict never finished.
        self.write(self.plan / 'final-review-1760602500-1' / 'qa.input', 'Review the combined result.\n')
        older = detail(self.plan)['final_reviews'][1]
        self.assertEqual((older['name'], older['state']), ('final-review-1760602500-1', 'orphaned'))

    def test_final_reviews_are_capped_at_twenty(self):
        for n in range(25):
            self.write(self.plan / f'final-review-{1760590000 + n}-1' / 'verdict', 'PASS\n')
        reviews = detail(self.plan)['final_reviews']
        self.assertEqual(len(reviews), 20)
        self.assertEqual(reviews[0]['name'], F.REVIEW_NEW)
        self.assertIsNotNone(final_review_detail(self.plan, 'final-review-1760590000-1'))
        self.assertIsNone(final_review_detail(self.plan, 'final-review-1-1'))

    def test_settings_integration_verification_and_deferrals(self):
        data = detail(self.plan)
        self.assertEqual(data['settings'], {'explore': 0.25, 'shadow_rate': 0.5, 'shadow_qa': ['sol:xhigh'],
                                            'timeout': 3600, 'max_parallel': 2, 'qa_threshold': 'P2',
                                            'escalate_after': 1, 'verify_retries': 1, 'final_qa': 'opus'})
        self.assertEqual(data['integration'], {
            'base': 'a' * 40, 'accepted': 'b' * 40,
            'merged': [{'id': 'page', 'at': F.MERGED['page']}, {'id': 'reader', 'at': F.MERGED['reader']}],
            'integrations': [{'name': F.INTEGRATE, 'at': 1760601800.0, 'verification_status': 'PASS',
                              'final_review': F.INTEGRATE + '/final-review', 'ended': T0 + 1990}]})
        self.assertEqual(self.task(data, 'reader')['merged_at'], F.MERGED['reader'])
        self.assertIsNone(self.task(data, 'docs')['merged_at'])
        self.assertEqual(data['verification']['commit'], 'b' * 40)
        self.assertEqual(data['verification']['runs'], [
            {'n': 1, 'kind': 'initial', 'exit': 1, 'tree_changed': False, 'duration_s': 30.0,
             'log': 'verification.run-1.log', 'lane': None, 'started': None},
            {'n': 2, 'kind': 'rerun', 'exit': 0, 'tree_changed': False, 'duration_s': 28.5,
             'log': 'verification.run-2.log', 'lane': None, 'started': None}])
        self.assertEqual(data['deferrals'], ['wave 1: docs deferred (file overlap with a task already in this wave)'])
        self.assertTrue(data['findings_available'])
        self.assertEqual(data['warnings'], [])
        with open(self.plan / 'verification.runs.tsv', 'a', encoding='utf-8') as stream:
            stream.write('x\tinitial\t0\t0\t1\tlog\n3\trerun\t0\t2\t1\tlog\n4\tjev_retry\t\t1\t2\tverification.run-4.log\n'
                         '5\trerun\tNaN\t0\t1\tlog\n6\trerun\t0\t0\tinf\tlog\n')
        os.utime(self.plan / 'verification.runs.tsv', (T0, T0))
        self.write(self.plan / 'max_parallel', 'two\n')
        data = detail(self.plan)
        self.assertEqual(data['verification']['runs'][2], {'n': 4, 'kind': 'jev_retry', 'exit': None,
                                                           'tree_changed': True, 'duration_s': 2.0,
                                                           'log': 'verification.run-4.log', 'lane': None,
                                                           'started': None})
        self.assertEqual(len(data['verification']['runs']), 3)
        self.assertEqual(sum('verification.runs.tsv line' in warning for warning in data['warnings']), 4)
        self.assertIsNone(data['settings']['max_parallel'])
        self.assertTrue(any(warning.startswith('max_parallel:') for warning in data['warnings']))

    def test_timeline(self):
        data = detail(self.plan)
        timeline = data['timeline']
        self.assertEqual((timeline['started'], timeline['ended']), (T0, None))
        self.assertEqual([item['id'] for item in timeline['runs']], [F.PARALLEL])
        self.assertEqual([item['id'] for item in timeline['verifications']], [F.VERIFICATION])
        self.assertEqual((timeline['runs'][0]['state'], timeline['verifications'][0]['state']), ('running', 'done'))
        # An older parallel run that never finished is orphaned; the newest is the one in flight.
        F.execution(self.plan, 'parallel-' + F.uid(3), -100.0, phase='dispatch')
        runs = detail(self.plan)['timeline']['runs']
        self.assertEqual([(item['id'], item['state']) for item in runs],
                         [('parallel-' + F.uid(3), 'orphaned'), (F.PARALLEL, 'running')])
        solo = detail(self.solo)['timeline']
        self.assertEqual(solo, {'started': T0, 'ended': T0 + 690, 'runs': [], 'verifications': []})

    def test_summary_additions(self):
        summary = summarize(self.plan)
        self.assertEqual([{k: t[k] for k in ('id', 'status')} for t in summary['task_statuses']], [{'id': 'reader', 'status': 'MERGED'},
                                                    {'id': 'page', 'status': 'MERGED'},
                                                    {'id': 'graph', 'status': 'RUNNING'},
                                                    {'id': 'server', 'status': 'PENDING'},
                                                    {'id': 'docs', 'status': 'FAIL'}])
        self.assertEqual(summary['models'], ['luna', 'luna:low', 'opus', 'opus:high', 'sol:xhigh'])
        self.assertEqual(summary['final_review'], 'FAIL')
        self.assertIs(summary['final_review_outdated'], False)
        self.assertEqual({task['id']: task['accepted'] for task in summary['task_statuses']},
                         {'reader': True, 'page': False, 'graph': False, 'server': False, 'docs': False})
        self.assertNotIn('timeline', summary)
        solo = summarize(self.solo)
        self.assertEqual((solo['task_statuses'], solo['models'], solo['final_review']),
                         ([{'id': 'solo', 'status': 'PASS', 'difficulty': None, 'title': 'A rich solo run',
                            'accepted': False}], ['opus', 'sol:high'], None))
        self.assertIsNone(solo['final_review_outdated'])

    def _review(self, data, name):
        return next(item for item in data['final_reviews'] if item['name'] == name)

    def test_published_fail_review_outdated_after_later_merges(self):
        # The run list must judge the published dir only, and must not open the other reviews' attempts.
        opened = []
        original = Path.open

        def wrapped(path, *args, **kwargs):
            opened.append(str(path))
            return original(path, *args, **kwargs)

        with patch.object(Path, 'open', wrapped):
            self.assertIs(summarize(self.plan)['final_review_outdated'], False)
        opened_text = '\n'.join(opened)
        self.assertIn('accepted.integration', opened_text)
        self.assertIn('final-review/review.meta', opened_text)
        self.assertIn('final-review/verdict', opened_text)
        self.assertNotIn('/attempts/', opened_text)
        self.assertNotIn(F.REVIEW_NEW, opened_text)
        self.assertNotIn(F.INTEGRATE + '/', opened_text)

        # Drop the in-flight newer review so RunDetail's final_review is the published FAIL.
        shutil.rmtree(self.plan / F.REVIEW_NEW)
        data = detail(self.plan)
        self.assertEqual(data['final_review']['name'], F.REVIEW_OLD)
        self.assertIs(data['final_review']['outdated'], False)
        self.assertEqual(data['final_review']['merged_since'], [])
        self.assertIs(summarize(self.plan)['final_review_outdated'], False)

        at = data['final_review']['at']
        # A merge moves the integration tip. page lands before reader.
        F.set_merged_at(self.plan, 'page', at + 2)
        F.set_merged_at(self.plan, 'reader', at + 5)
        self.write(self.plan / 'accepted.integration', 'c' * 40 + '\n')
        data = detail(self.plan)
        self.assertIs(data['final_review']['outdated'], True)
        self.assertEqual(data['final_review']['merged_since'], ['page', 'reader'])
        self.assertIs(summarize(self.plan)['final_review_outdated'], True)
        self.assertEqual(final_review_detail(self.plan, F.REVIEW_OLD)['merged_since'], ['page', 'reader'])
        found = next(item for item in run_findings(self.plan)['final_reviews'] if item['name'] == F.REVIEW_OLD)
        self.assertIs(found['outdated'], True)
        self.assertEqual(found['merged_since'], ['page', 'reader'])

        # The commit has moved, but a merge inside the review's starting second is not "since".
        F.set_merged_at(self.plan, 'page', at + 0.5)
        F.set_merged_at(self.plan, 'reader', at + 0.25)
        data = detail(self.plan)
        self.assertIs(data['final_review']['outdated'], True)
        self.assertEqual(data['final_review']['merged_since'], [])
        self.assertIs(summarize(self.plan)['final_review_outdated'], True)

    def test_malformed_integration_is_null_and_does_not_decide_freshness(self):
        # tip of the published review already equals accepted.integration. A non-commit
        # integration= must not count as known, or the review looks outdated for the wrong reason.
        accepted = (self.plan / 'accepted.integration').read_text().strip()
        old = self.plan / F.REVIEW_OLD / 'review.meta'
        old.write_text(old.read_text() + 'integration=not-a-commit\n')
        # The integrate review's tip is the candidate, not the integration tip. The same bad
        # value must fall through to the merge-time rule, which finds nothing merged after it.
        integrate = self.plan / F.INTEGRATE / 'final-review' / 'review.meta'
        integrate.write_text(integrate.read_text() + 'integration=not-a-commit\n')

        data = detail(self.plan)
        review = self._review(data, F.REVIEW_OLD)
        self.assertEqual(review['tip'], accepted)
        self.assertIsNone(review['integration'])
        self.assertIs(review['outdated'], False)
        self.assertEqual(review['merged_since'], [])
        self.assertIn(f'{F.REVIEW_OLD}/review.meta: integration is not a commit', data['warnings'])
        self.assertIn('final-review/review.meta: integration is not a commit', data['warnings'])
        integrate_review = self._review(data, F.INTEGRATE)
        self.assertIsNone(integrate_review['integration'])
        self.assertNotEqual(integrate_review['tip'], accepted)
        self.assertIs(integrate_review['outdated'], False)
        self.assertEqual(integrate_review['merged_since'], [])
        self.assertIn(f'{F.INTEGRATE}/final-review/review.meta: integration is not a commit', data['warnings'])

        self.assertIs(summarize(self.plan)['final_review_outdated'], False)
        found = final_review_detail(self.plan, F.REVIEW_OLD)
        self.assertIsNone(found['integration'])
        self.assertIs(found['outdated'], False)
        self.assertIn(f'{F.REVIEW_OLD}/review.meta: integration is not a commit', found['warnings'])

    def test_summary_updated_includes_accepted_integration(self):
        # Previously read files and the run dir stay at 200. Rewriting accepted.integration
        # must move `updated`, because the summary reads it for final_review_outdated.
        for path in [self.plan, *self.plan.rglob('*')]:
            if not path.is_symlink():
                os.utime(path, (200, 200))
        os.utime(self.plan / 'accepted.integration', (300, 300))
        self.assertEqual(summarize(self.plan)['updated'], 300.0)

    def test_matching_integration_commit_is_current_even_when_the_candidate_tip_differs(self):
        meta = self.plan / F.INTEGRATE / 'final-review' / 'review.meta'
        accepted = (self.plan / 'accepted.integration').read_text().strip()
        meta.write_text(meta.read_text() + f'integration={accepted}\n')
        at = self._review(detail(self.plan), F.INTEGRATE)['at']
        F.set_merged_at(self.plan, 'reader', at + 5)
        review = self._review(detail(self.plan), F.INTEGRATE)
        self.assertEqual(review['integration'], accepted)
        self.assertNotEqual(review['tip'], accepted)
        self.assertIs(review['outdated'], False)
        self.assertEqual(review['merged_since'], [])
        self.assertIs(final_review_detail(self.plan, F.INTEGRATE)['outdated'], False)
        # A sha256 object name is a commit as well. It still wins over the later merge and the
        # candidate tip; a length check that only allowed 40 hex digits would call this outdated.
        sha256 = 'c' * 64
        meta.write_text(meta.read_text().replace(f'integration={accepted}\n', f'integration={sha256}\n'))
        self.write(self.plan / 'accepted.integration', sha256 + '\n')
        review = self._review(detail(self.plan), F.INTEGRATE)
        self.assertEqual((review['integration'], review['outdated'], review['merged_since']), (sha256, False, []))
        self.assertEqual(detail(self.plan)['warnings'], [])

    def test_same_second_merge_does_not_outdate_an_older_integrate_review(self):
        at = self._review(detail(self.plan), F.INTEGRATE)['at']
        metrics = self.plan / F.INTEGRATE / 'final-review' / 'attempts' / F.INTEGRATE_QA / 'metrics.json'
        payload = json.loads(metrics.read_text())
        payload['wall_started'] = at + 0.2  # inside the review's first second, before the merge below
        metrics.write_text(json.dumps(payload))
        F.set_merged_at(self.plan, 'reader', at + 0.5)
        link = self.plan / 'final-review'
        link.unlink()
        link.symlink_to(F.INTEGRATE + '/final-review')

        data = detail(self.plan)
        review = self._review(data, F.INTEGRATE)
        self.assertEqual(review['started'], at + 0.2)
        self.assertGreater(at + 0.5, review['started'])
        self.assertIsNone(review['integration'])
        self.assertIs(review['outdated'], False)
        self.assertEqual(review['merged_since'], [])
        # The newest review is still in flight. The summary follows the published link.
        self.assertEqual(data['final_review']['name'], F.REVIEW_NEW)
        self.assertIsNone(data['final_review']['outdated'])
        self.assertIs(summarize(self.plan)['final_review_outdated'], False)
        self.assertIs(final_review_detail(self.plan, F.INTEGRATE)['outdated'], False)

        F.set_merged_at(self.plan, 'reader', at + 1)
        review = self._review(detail(self.plan), F.INTEGRATE)
        self.assertIs(review['outdated'], True)
        self.assertEqual(review['merged_since'], ['reader'])
        self.assertIs(summarize(self.plan)['final_review_outdated'], True)
        found = next(item for item in run_findings(self.plan)['final_reviews'] if item['name'] == F.INTEGRATE)
        self.assertIs(found['outdated'], True)
        self.assertEqual(found['merged_since'], ['reader'])

    def test_accepted_tasks_keep_their_status(self):
        before = detail(self.plan)
        counts = before['counts']
        reader_task = self.task(before, 'reader')
        self.assertEqual((reader_task['status'], reader_task['accepted'], reader_task['accepted_attempt'],
                          reader_task['accepted_over']), ('MERGED', True, None, None))
        for ident in ('page', 'graph', 'server', 'docs'):
            task = self.task(before, ident)
            self.assertEqual((task['accepted'], task['accepted_attempt'], task['accepted_over']),
                             (False, None, None), ident)
        solo = detail(self.solo)
        self.assertEqual((solo['tasks'][0]['accepted'], solo['tasks'][0]['accepted_attempt'],
                          solo['tasks'][0]['accepted_over']), (False, None, None))
        self.assertFalse(summarize(self.solo)['task_statuses'][0]['accepted'])

        self.write(self.plan / 'tasks/page/accepted.by',
                   'task=page\nstatus=FAIL\nreason=tradeoff\nby=human\nat=2026-01-01T00:00:00+0000\n'
                   'epoch=1\nattempt=2\nreviewed_commit=abc\n')
        self.write(self.plan / 'tasks/docs/accepted.by', 'task=docs\nstatus=UNKNOWN\nattempt=1\n')
        data = detail(self.plan)
        self.assertEqual(data['counts'], counts)
        page, docs = self.task(data, 'page'), self.task(data, 'docs')
        self.assertEqual((page['status'], page['accepted'], page['accepted_attempt'], page['accepted_over']),
                         ('MERGED', True, 2, 'FAIL'))
        self.assertEqual((docs['status'], docs['accepted'], docs['accepted_attempt'], docs['accepted_over']),
                         ('FAIL', True, 1, 'UNKNOWN'))
        self.assertEqual(data['warnings'], [])
        statuses = {task['id']: task['accepted'] for task in summarize(self.plan)['task_statuses']}
        self.assertEqual(statuses, {'reader': True, 'page': True, 'graph': False, 'server': False, 'docs': True})

        self.write(self.plan / 'tasks/graph/accepted.by', 'task=graph\nstatus=FAIL\nattempt=nope\n')
        data = detail(self.plan)
        graph = self.task(data, 'graph')
        self.assertEqual((graph['status'], graph['accepted'], graph['accepted_attempt'], graph['accepted_over']),
                         ('RUNNING', True, None, 'FAIL'))
        self.assertEqual(data['counts'], counts)
        self.assertTrue(any(warning == 'tasks/graph/accepted.by: attempt is not an integer' for warning in data['warnings']))

    def test_solo_executions(self):
        data = detail(self.solo)
        task = data['tasks'][0]
        self.assertEqual([(item['id'], item['parent'], item['attempt'], item['state']) for item in task['executions']],
                         [(F.SOLO_RUN, None, 1, 'done'), (F.SOLO_DWARF, F.SOLO_RUN, 1, 'done'),
                          (F.SOLO_QA, F.SOLO_RUN, 1, 'done')])
        self.assertEqual(task['findings'], counts(plausible=1))
        for field, value in (('integration', None), ('final_review', None), ('final_reviews', []), ('deferrals', [])):
            self.assertEqual(data[field], value, field)
        self.assertEqual(set(data['settings'].values()), {None})
        result = execution_detail(self.solo, F.SOLO_QA)
        self.assertEqual(result['owner'], {'kind': 'task', 'id': 'solo'})
        self.assertEqual(result['findings'][0]['location'], 'src/solo.py:3')
        self.assertIsNone(execution_detail(self.solo, F.READER_QA1))
        self.assertIsNone(task_detail(self.solo, 'reader'))
        self.assertEqual([review['execution'] for review in task_detail(self.solo, 'solo')['reviews']], [F.SOLO_QA])
        # A running solo attempt is the one after the last attempts.tsv row.
        self.write(self.solo / 'status', 'RUNNING\n')
        F.execution(self.solo, 'solo-' + F.uid(63), 800.0, phase='dispatch')
        self.assertEqual(execution_detail(self.solo, 'solo-' + F.uid(63))['attempt'], 2)


class LazyTests(FixtureCase):
    def test_task_detail(self):
        result = task_detail(self.plan, 'reader')
        self.assertEqual(result['run'], reader.run_key(self.plan))
        self.assertEqual(result['id'], 'reader')
        self.assertEqual(result['texts']['accepted_by'], 'task=reader\nstatus=PASS\nby=human\n')
        self.assertEqual(result['texts']['retry_findings'], F.QA_READER_1)
        self.assertIsNone(result['texts']['known_issues'])
        self.assertEqual([(review['kind'], review['execution'], review['attempt'], review['verdict'])
                          for review in result['reviews']],
                         [('primary', F.READER_QA1, 1, 'FAIL'), ('primary', F.READER_QA2, 2, 'PASS')])
        self.assertEqual(result['reviews'][0]['findings'][0], {
            'severity': 'P1', 'status': 'CONFIRMED', 'location': 'ui/reader.py:42', 'line': 3,
            'summary': '- [P1][CONFIRMED] ui/reader.py:42 — a missing status file raises instead of reading as PENDING.',
            'text': '- [P1][CONFIRMED] ui/reader.py:42 — a missing status file raises instead of reading as PENDING.\n'
                    '  The read is not wrapped, so FileNotFoundError escapes to the server.'})
        self.assertEqual((result['truncated'], result['warnings']), ([], []))
        page = task_detail(self.plan, 'page')
        self.assertEqual(page['gate']['decision'], 'keep')
        self.assertTrue(page['texts']['known_issues'].startswith('## Known issues'))
        self.assertEqual([(review['kind'], review['verdict'], review['state']) for review in page['reviews']],
                         [('primary', 'PASS', 'done'), ('shadow', 'FAIL', 'done')])
        graph = task_detail(self.plan, 'graph')
        self.assertEqual([(review['execution'], review['state'], review['findings']) for review in graph['reviews']],
                         [(F.GRAPH_QA, 'running', None)])
        # A reply in the task dir that no execution copied is listed on its own.
        self.write(self.plan / 'tasks/reader/qa.last', '- [P0][CONFIRMED] ui/x.py:1 — data loss.\n\nFORGE_VERDICT: FAIL\n')
        extra = task_detail(self.plan, 'reader')['reviews'][-1]
        self.assertEqual((extra['execution'], extra['attempt'], extra['spec'], extra['verdict'], extra['state']),
                         (None, 2, 'opus', 'FAIL', 'done'))
        self.assertEqual(extra['findings'][0]['severity'], 'P0')
        self.assertEqual(task_detail(self.plan, 'server')['reviews'], [])
        for bad in ('nope', '', '.', '..', 'reader/x', 'solo'):
            self.assertIsNone(task_detail(self.plan, bad), bad)

    def test_excerpts_cut_the_middle_at_character_boundaries(self):
        for char in ('é', '😀', 'a'):
            with self.subTest(char=char):
                value = 'H' + char * 30000 + 'T'
                self.write(self.plan / 'tasks/reader/qa.input', value)
                result = task_detail(self.plan, 'reader')
                self.assertEqual(result['texts']['qa_input'], value[:20000] + '\n…\n' + value[-8000:])
                self.assertEqual(result['truncated'], ['qa_input'])
                self.assertEqual(result['warnings'], [])
        self.write(self.plan / 'tasks/reader/qa.input', 'é' * 28000)
        result = task_detail(self.plan, 'reader')
        self.assertEqual((result['texts']['qa_input'], result['truncated']), ('é' * 28000, []))
        (self.plan / 'tasks/reader/qa.input').write_bytes(b'\xff' + b'x' * 200000)
        result = task_detail(self.plan, 'reader')
        self.assertTrue(result['texts']['qa_input'].startswith('�'))
        self.assertEqual(len(result['warnings']), 1)

    def test_execution_detail(self):
        result = execution_detail(self.plan, F.READER_QA1)
        base = self.execs(detail(self.plan), 'reader')[F.READER_QA1]
        self.assertEqual({key: result[key] for key in base if key != 'findings'},
                         {key: value for key, value in base.items() if key != 'findings'})
        self.assertEqual(result['owner'], {'kind': 'task', 'id': 'reader'})
        self.assertEqual(result['dataset'], {'trigger': None, 'parent': F.READER_TASK1, 'attempt_start': None,
                                             'resume': None})
        self.assertEqual(result['resolved']['alias'], 'opus')
        self.assertEqual(result['prompt'], F.prompt_text('qa', F.READER_QA1, F.DIFF_READER_1))
        self.assertEqual(result['last'], F.QA_READER_1)
        self.assertEqual([item['severity'] for item in result['findings']], ['P1', 'P2'])
        task = execution_detail(self.plan, F.READER_TASK2)
        self.assertEqual((task['dataset']['attempt_start'], task['dataset']['trigger'], task['resolved'],
                          task['prompt'], task['findings']), (1, 'auto', None, None, None))
        running = execution_detail(self.plan, F.GRAPH_QA)
        self.assertEqual((running['prompt'], running['last'], running['findings']),
                         ('Review the graph diff (as delivered).\n', None, None))
        self.assertEqual(running['resolved']['effort'], 'high')
        (self.plan / 'tasks/graph/qa.delivered').unlink()
        self.assertEqual(execution_detail(self.plan, F.GRAPH_QA)['prompt'], 'Review the graph diff.\n')
        self.assertEqual(execution_detail(self.plan, F.PARALLEL)['owner'], {'kind': 'run', 'id': None})
        self.assertEqual(execution_detail(self.plan, F.PAGE_SHADOW_QA)['owner'], {'kind': 'shadow', 'id': 'page/1-0'})
        self.assertEqual(execution_detail(self.plan, F.INTEGRATE_QA)['owner'],
                         {'kind': 'final_review', 'id': F.INTEGRATE})
        self.write(self.plan / 'tasks/reader/attempts' / F.READER_QA1 / 'qa.last', 'x' * 30000 + 'END')
        result = execution_detail(self.plan, F.READER_QA1)
        self.assertEqual(result['last'], '…' + ('x' * 30000 + 'END')[-20000:])
        self.assertEqual(result['truncated'], ['last'])
        for bad in ('qa-' + F.uid(999), 'qa-' + 'f' * 31, 'bogus-' + F.uid(1), '../' + F.READER_QA1, F.SOLO_QA):
            self.assertIsNone(execution_detail(self.plan, bad), bad)

    def test_lazy_reads_never_open_logs_or_commands(self):
        original = Path.open
        opened = []

        def guard(path, *args, **kwargs):
            opened.append(path.name)
            return original(path, *args, **kwargs)
        data = detail(self.plan)
        with patch.object(Path, 'open', guard):
            for task in data['tasks']:
                task_detail(self.plan, task['id'])
                for item in task['executions']:
                    execution_detail(self.plan, item['id'])
            for review in data['final_reviews']:
                final_review_detail(self.plan, review['name'])
        self.assertTrue(opened)
        self.assertEqual([name for name in opened if name.endswith(('.log', '.cmd'))], [])

    def test_execution_transcript(self):
        done = execution_transcript(self.plan, F.READER_DWARF1)
        self.assertEqual({key: done[key] for key in ('role', 'state', 'harness', 'format', 'source', 'owner')},
                         dict(role='dwarf', state='done', harness='codex', format='codex', source='log',
                              owner={'kind': 'task', 'id': 'reader'}))
        # a finished execution reads its own copy, never the owner dir's log (the next attempt's)
        self.write(self.plan / 'tasks/reader/dwarf.log', 'the next attempt\n')
        self.assertEqual(execution_transcript(self.plan, F.READER_DWARF1)['events'], done['events'])
        # a reviewer still at work: the log its dispatch is writing, up to its last whole line
        live = execution_transcript(self.plan, F.GRAPH_QA)
        self.assertEqual((live['state'], [ev['kind'] for ev in live['events']]), ('running', ['reasoning', 'command']))
        for name in (F.READER_TASK1, F.PARALLEL, F.VERIFICATION, 'qa-' + F.uid(999)):
            self.assertIsNone(execution_transcript(self.plan, name), name)

    def test_a_transcript_never_follows_a_log_out_of_the_run(self):
        log = self.plan / 'tasks/reader/attempts' / F.READER_DWARF1 / 'dwarf.log'
        outside = self.base / 'outside.log'
        outside.write_text(log.read_text())
        log.unlink()
        log.symlink_to(outside)
        result = execution_transcript(self.plan, F.READER_DWARF1)
        self.assertEqual((result['source'], result['events']), ('none', []))
        self.assertIn('outside the run dir', ' '.join(result['warnings']))

    def test_final_review_detail(self):
        result = final_review_detail(self.plan, F.REVIEW_OLD)
        summary = detail(self.plan)['final_reviews'][1]
        self.assertEqual({key: result[key] for key in summary if key != 'findings'},
                         {key: value for key, value in summary.items() if key != 'findings'})
        self.assertEqual(result['meta'], {'base': 'a' * 40, 'tip': 'b' * 40, 'source': '/example/worktrees/_integration',
                                          'spec': 'opus', 'threshold': 'P2'})
        self.assertTrue(result['tasks_summary'].startswith('reader\tMERGED'))
        self.assertEqual(result['qa_input'], 'Review the combined result.\n')
        self.assertEqual(result['qa_last'], F.QA_REVIEW_OLD)
        self.assertIsNone(result['known_issues'])
        self.assertEqual([item['location'] for item in result['findings']], ['ui/server.py:55', 'ui/app.js:120'])
        running = final_review_detail(self.plan, F.REVIEW_NEW)
        self.assertEqual((running['state'], running['findings'], running['qa_last']), ('running', None, None))
        self.assertEqual(final_review_detail(self.plan, F.INTEGRATE)['source'], 'integrate')
        for bad in ('final-review-1-1', 'integrate-1-1', 'final-review', '../x', F.REVIEW_OLD + '/x'):
            self.assertIsNone(final_review_detail(self.plan, bad), bad)
        self.assertIsNone(final_review_detail(self.solo, F.REVIEW_OLD))
        shutil.rmtree(self.plan / F.INTEGRATE / 'final-review')
        self.assertIsNone(final_review_detail(self.plan, F.INTEGRATE))
        integration = detail(self.plan)['integration']['integrations'][0]
        self.assertEqual((integration['final_review'], integration['ended']), (None, T0 + 1850))


class RobustnessTests(FixtureCase):
    def test_contract_module_unavailable(self):
        with patch('forge_ui.findings._load', return_value=None):
            data = detail(self.plan)
            self.assertFalse(data['findings_available'])
            self.assertFalse(findings.available())
            for task in data['tasks']:
                self.assertIsNone(task['findings'])
                for item in task['executions'] + task['shadow']:
                    self.assertIsNone(item['findings'])
            self.assertEqual(self.execs(data, 'reader')[F.READER_QA1]['verdict'], 'FAIL')
            self.assertEqual(self.task(data, 'page')['shadow'][0]['agreement'], 'disagree')
            self.assertEqual(self.task(data, 'page')['gate']['decision'], 'keep')  # the gate needs no contract
            for review in data['final_reviews']:
                self.assertIsNone(review['findings'])
            self.assertTrue(all(review['findings'] is None for review in task_detail(self.plan, 'reader')['reviews']))
            self.assertIsNone(execution_detail(self.plan, F.READER_QA1)['findings'])
            self.assertIsNone(final_review_detail(self.plan, F.REVIEW_OLD)['findings'])
            self.assertFalse(detail(self.solo)['findings_available'])
            for output in self.all_outputs(self.plan):
                json.dumps(output, allow_nan=False)
        self.assertTrue(detail(self.plan)['findings_available'])

    def test_contract_loader_failure_is_cached(self):
        broken = self.base / 'forge-contract.py'
        broken.write_text('raise RuntimeError("broken")\n')
        for path in (self.base / 'missing.py', broken):
            with self.subTest(path=path.name), patch.object(findings, 'CONTRACT', path), \
                    patch.dict(findings._STATE, clear=True):
                self.assertFalse(findings.available())
                self.assertIsNone(findings.summarize('- [P1][CONFIRMED] a.py:1 — x\n'))
                self.assertIsNone(findings.listing('- [P1][CONFIRMED] a.py:1 — x\n'))
                self.assertEqual(findings._STATE, {'module': None})
        self.assertTrue(findings.available())

    def test_findings_and_gate_parsing(self):
        text = ('- [P0][CONFIRMED] a.py:1 — x\n- [P2][CONFIRMED] b.py:2 — y\n- [PLAUSIBLE] c.py:3 — z\n'
                '- [P1] d.py:4 — no status at all\n\nFORGE_VERDICT: FAIL\n')
        self.assertEqual(findings.summarize(text), counts(P0=1, P2=1, confirmed=2, plausible=1, unlabelled=1,
                                                          worst='P0'))
        self.assertEqual([(item['status'], item['severity'], item['line']) for item in findings.listing(text)],
                         [('CONFIRMED', 'P0', 1), ('CONFIRMED', 'P2', 2), ('PLAUSIBLE', None, 3), ('UNSTATED', 'P1', 4)])
        self.assertEqual(len(findings.listing(text, limit=2)), 2)
        self.assertEqual(findings.listing('- [P1][CONFIRMED] x — ' + 'w' * 300)[0]['summary'],
                         ('- [P1][CONFIRMED] x — ' + 'w' * 300)[:240])
        for empty in (None, ''):
            self.assertIsNone(findings.summarize(empty))
            self.assertIsNone(findings.listing(empty))
        self.assertEqual(findings.summarize('FORGE_VERDICT: PASS\n'), ZERO)
        self.assertEqual(findings.parse_gate('P2\taccept\tP3\t1\t0\t1\n'),
                         {'threshold': 'P2', 'decision': 'accept', 'worst': 'P3', 'n_confirmed': 1,
                          'n_unlabelled': 0, 'n_below': 1})
        self.assertEqual(findings.parse_gate('P1\tkeep\t-\t0\tx\t0'),
                         {'threshold': 'P1', 'decision': 'keep', 'worst': None, 'n_confirmed': 0,
                          'n_unlabelled': None, 'n_below': 0})
        for bad in (None, '', 'P2\tkeep\n', '\n\n'):
            self.assertIsNone(findings.parse_gate(bad))

    def test_malformed_metrics_are_warnings_only(self):
        path = self.plan / 'tasks/docs/attempts' / F.DOCS_DWARF / 'metrics.json'
        cases = {
            'nan': ('{"started": NaN}', ['not a JSON object']),
            'list': ('[1, 2]', ['not a JSON object']),
            'empty': ('', ['not a JSON object']),
            'missing': ('{}', ['missing started', 'missing wall_started', 'missing exit_code']),
            'string elapsed': ('{"started": 1, "wall_started": 2, "phase": "model", "phase_started": 1, '
                               '"elapsed_s": "12", "exit_code": null}', ['elapsed_s is not an object']),
            'wrong types': ('{"started": "1", "wall_started": true, "phase": 3, "phase_started": 1, '
                            '"elapsed_s": {"model": "x", "total": 5}, "exit_code": "0", "input_tokens": 1.5}',
                            ['started is not a number', 'wall_started is not a number', 'phase is not a string',
                             'elapsed_s.model is not a number', 'exit_code is not an integer',
                             'input_tokens is not an integer']),
            'huge': ('{"started": 0, "wall_started": 1e308, "phase": "complete", "phase_started": 1e308, '
                     '"elapsed_s": {"total": 1e308}, "exit_code": 0}', []),
            'huge int': ('{"started": 0, "wall_started": 1' + '0' * 400 + ', "phase": "x", "phase_started": 0, '
                         '"elapsed_s": {}, "exit_code": 0}', ['wall_started is not a number']),
        }
        for name, (value, reasons) in cases.items():
            with self.subTest(name):
                self.write(path, value)
                for _ in range(2):  # the second read is served from the cache and warns the same
                    data = detail(self.plan)
                    item = self.execs(data, 'docs')[F.DOCS_DWARF]
                    json.dumps(data, allow_nan=False)
                    warnings = [warning for warning in data['warnings'] if F.DOCS_DWARF in warning]
                    for reason in reasons:
                        self.assertIn(f'tasks/docs/attempts/{F.DOCS_DWARF}/metrics.json: {reason}', warnings)
                    self.assertEqual(len(warnings), len(reasons) if name != 'missing' else 6)
                self.assertIn(item['state'], ('done', 'orphaned'))
                self.assertIn(item['parent'], (F.DOCS_TASK, None))
                json.dumps(execution_detail(self.plan, F.DOCS_DWARF), allow_nan=False)
        self.assertIsNone(item['ended'])  # 1e308 + 1e308 is not a time
        for value in ('{"parent": 3, "attempt_start": "1", "resume": true}', 'nope'):
            self.write(self.plan / 'tasks/reader/attempts' / F.READER_TASK2 / 'dataset.json', value)
            json.dumps(detail(self.plan), allow_nan=False)
        self.assertEqual(execution_detail(self.plan, F.READER_TASK2)['attempt'], 2)

    def test_dirs_outside_the_run_are_ignored(self):
        outside = F.make_plan(self.base / 'other')
        foreign = outside / 'tasks/reader/attempts' / F.READER_QA1
        stray = 'qa-' + F.uid(777)
        (self.plan / 'tasks/docs/attempts' / stray).symlink_to(foreign, target_is_directory=True)
        (self.plan / 'tasks/server').mkdir()
        (self.plan / 'tasks/server/attempts').symlink_to(outside / 'tasks/reader/attempts', target_is_directory=True)
        data = detail(self.plan)
        self.assertNotIn(stray, self.execs(data, 'docs'))
        self.assertEqual(self.task(data, 'server')['executions'], [])
        self.assertIn(f'tasks/docs/attempts/{stray}: outside the run dir', data['warnings'])
        self.assertIn('tasks/server/attempts: outside the run dir', data['warnings'])
        self.assertIsNone(execution_detail(self.plan, stray))
        self.assertIsNone(execution_detail(self.plan, F.READER_QA1.replace(F.uid(12), F.uid(778))))
        self.assertIsNone(executions.locate(self.plan, stray, ['docs']))
        self.assertEqual(executions.locate(self.plan, F.READER_QA1, ['server', 'reader']),
                         Path('tasks/reader/attempts') / F.READER_QA1)
        # A symlink that stays inside the run is followed.
        (self.plan / 'tasks/docs/attempts' / stray).unlink()
        (self.plan / 'tasks/docs/attempts' / stray).symlink_to(self.plan / 'tasks/docs/attempts' / F.DOCS_QA)
        self.assertIn(stray, self.execs(detail(self.plan), 'docs'))

    def test_cache_parses_once_until_the_signature_changes(self):
        parsed = []
        original = executions._metrics

        def counting(reader_, name, text):
            parsed.append(name)
            return original(reader_, name, text)
        with patch.object(executions, '_metrics', counting):
            first = detail(self.plan)
            count = len(parsed)
            self.assertGreaterEqual(count, 20)
            self.assertEqual(detail(self.plan), first)
            self.assertEqual(len(parsed), count)
            path = self.plan / 'tasks/docs/attempts' / F.DOCS_TASK / 'metrics.json'
            metrics = json.loads(path.read_text())
            metrics['exit_code'] = 2
            self.write(path, metrics)
            self.assertEqual(self.execs(detail(self.plan), 'docs')[F.DOCS_TASK]['exit_code'], 2)
            self.assertEqual(parsed[count:], [f'tasks/docs/attempts/{F.DOCS_TASK}/metrics.json'])
            # A file written moments ago is not trusted to the cache: same size, same tick is possible.
            count = len(parsed)
            path.write_text(path.read_text())
            detail(self.plan)
            detail(self.plan)
            self.assertEqual(len(parsed), count + 2)

    def test_cache_is_bounded(self):
        cache = executions._SigCache(size=3)
        for n in range(5):
            cache.put(n, 'sig', n)
        self.assertEqual([cache.get(n, 'sig') for n in range(5)], [None, None, 2, 3, 4])
        self.assertIsNone(cache.get(4, 'other'))

    def test_all_outputs_are_strict_json_read_only_and_deterministic(self):
        def snapshot(root):
            result = {}
            for path in [root, *root.rglob('*')]:
                info = path.lstat()
                content = path.read_bytes() if path.is_file() and not path.is_symlink() else b''
                result[str(path.relative_to(root))] = (info.st_mtime_ns, hashlib.sha1(content).hexdigest())
            return result
        for run in (self.plan, self.solo):
            before = snapshot(run)
            outputs = self.all_outputs(run)
            for output in outputs:
                self.assertIsNotNone(output)
                json.dumps(output, allow_nan=False)
            self.assertEqual(snapshot(run), before)
            self.assertEqual(self.all_outputs(run), outputs)
            self.assertEqual(detail(run), detail(run))


if __name__ == '__main__':
    unittest.main()
