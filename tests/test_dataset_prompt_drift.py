"""A prompt edit during an attempt is recorded, and left out of routing, tiers and forecasts.

The attempt stops STALE and spends nothing (#150). Its event keeps the text the
implementer was given, and export._attempts drops it, which is the only path those
statistics read attempt events through.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402
from forge_dataset.export import Runs, Store, _attempts  # noqa: E402
from forge_dataset.importer import import_run  # noqa: E402
from forge_dataset.routing import tasks as routing_tasks  # noqa: E402
from forge_dataset.tiers import records as tier_records  # noqa: E402


class PromptDriftDatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'data'
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('FORGE_DATASET', None)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def execution(self, directory, name, started):
        attempt = directory / 'attempts' / name
        attempt.mkdir(parents=True)
        (attempt / 'metrics.json').write_text(json.dumps(dict(
            role='task', wall_started=started, exit_code=0, harness='pipeline',
            elapsed_s=dict(total=1))))
        return attempt

    def plan(self):
        path = Path(self.tmp.name) / 'plan'
        self.write(path / 'dataset.uid', 'drift-uid\n')
        self.write(path / 'repo', '/nonexistent/repo\n')
        self.write(path / 'run_id', 'drift-run\n')
        self.write(path / 'goal.txt', 'goal\n')
        self.write(path / 'tasks.tsv',
                   'stale\t-\tlow\tstale.txt\tluna\topus\tStale\n'
                   'fail\t-\tlow\tfail.txt\tluna\topus\tFail\n'
                   'again\t-\tlow\tagain.txt\tluna\topus\tAgain\n')
        self.write(path / 'retry_per_model', '2\n')
        return path

    def imported(self, path):
        result = import_run(path)
        self.assertEqual(result['failed'], [], result)
        db = store.connect()
        try:
            return Runs(Store(db))
        finally:
            db.close()

    def blob(self, ref) -> str:
        self.assertIsInstance(ref, dict)
        return store.read_blob(ref['sha']).decode()

    def test_a_stale_attempt_keeps_the_dispatched_prompt_and_a_fail_does_not_flag(self):
        plan = self.plan()
        stale = plan / 'tasks' / 'stale'
        self.write(stale / 'status', 'STALE\n')
        self.write(stale / 'attempt', '1\n')
        self.write(stale / 'prompt.md', 'new text')
        self.write(stale / 'prompt.dispatched', 'old text')
        self.write(stale / 'approach.md', 'new approach')
        self.write(stale / 'approach.dispatched', '')
        self.write(stale / 'stale.diff', '--- prompt.md\n+++ prompt.md\n')
        self.execution(stale, 'task-stale', 10)
        fail = plan / 'tasks' / 'fail'
        self.write(fail / 'status', 'FAIL\n')
        self.write(fail / 'attempt', '1\n')
        self.write(fail / 'prompt.md', 'live prompt')
        self.execution(fail, 'task-fail', 20)
        runs = self.imported(plan)

        [stale_event] = runs.attempts[('drift-uid', 'stale')]
        self.assertIs(stale_event['d']['prompt_changed'], True)
        self.assertEqual(self.blob(stale_event['d']['blobs']['prompt']), 'old text')
        self.assertNotIn('approach', stale_event['d']['blobs'])
        self.assertTrue({'prompt.md', 'approach.md', 'prompt.dispatched', 'approach.dispatched', 'stale.diff'}
                        <= set(stale_event['d']['produced']))

        [fail_event] = runs.attempts[('drift-uid', 'fail')]
        self.assertIs(fail_event['d']['prompt_changed'], False)
        self.assertEqual(self.blob(fail_event['d']['blobs']['prompt']), 'live prompt')

        # The only execution was the prompt change, so routing has nothing to score.
        [routed] = [r for r in routing_tasks(runs) if r['task'] == 'stale']
        self.assertEqual((routed['attempts'], routed['censored'], routed['first_pass']), (0, 'not_run', None))

    def test_a_prompt_change_is_left_out_of_attempt_statistics(self):
        # Attempt 1 ran, the prompt changed (STALE, number reused), then failed, then attempt 2 passed.
        plan = self.plan()
        again = plan / 'tasks' / 'again'
        self.write(again / 'status', 'PASS\n')
        self.write(again / 'attempt', '2\n')
        self.write(again / 'prompt.md', 'final prompt')
        self.write(again / 'attempts.tsv',
                   '100\t1\tSTALE\t\tluna\n'
                   '200\t1\tFAIL\t\tluna\n'
                   '300\t2\tPASS\t\tluna\n')
        self.execution(again, 'task-again-stale', 100)
        self.execution(again, 'task-again-fail', 200)
        self.execution(again, 'task-again-pass', 300)
        runs = self.imported(plan)

        raw = runs.attempts[('drift-uid', 'again')]
        self.assertEqual(sorted((e['attempt'], e['d']['status'], e['d']['prompt_changed']) for e in raw),
                         [(1, 'FAIL', False), (1, 'STALE', True), (2, 'PASS', False)])
        got = _attempts(runs, 'drift-uid', 'again', raw)
        self.assertEqual([a['n'] for a in got], [1, 2])
        self.assertEqual([e['d']['status'] for e in got[0]['events']], ['FAIL'])
        self.assertIs(got[0]['outcome'], False)
        self.assertEqual([e['d']['status'] for e in got[1]['events']], ['PASS'])

        [routed] = [r for r in routing_tasks(runs) if r['task'] == 'again']
        self.assertEqual(routed['attempts'], 2)
        self.assertIs(routed['first_pass'], False)
        [tier] = [r for r in tier_records(runs) if r['task'] == 'again']
        self.assertEqual((tier['attempts'], tier['first_pass']), (2, 0))

    def test_an_attempt_stored_before_the_flag_still_counts(self):
        # No prompt_changed key: the row predates the field and is a real attempt.
        runs = SimpleNamespace(dispatches=defaultdict(list))
        got = _attempts(runs, 'r', 't', [dict(attempt=1, d=dict(status='STALE'))])
        self.assertEqual([a['n'] for a in got], [1])
        self.assertIsNone(got[0]['outcome'])

    def test_the_plan_event_records_retry_per_model(self):
        plan = self.plan()
        again = plan / 'tasks' / 'again'
        self.write(again / 'status', 'PASS\n')
        self.write(again / 'attempt', '1\n')
        self.execution(again, 'task-again', 10)
        runs = self.imported(plan)
        [event] = [e for e in runs.store.of('plan') if e['run'] == 'drift-uid']
        self.assertEqual(event['d']['options'].get('retry_per_model'), '2')

        # The value is part of the plan id, so a plan that asked for a different cap is a different plan.
        from forge_dataset.harvest import plan_event
        before = plan_event(plan, dict(run='drift-uid'), src='import', inventory=False)
        self.write(plan / 'retry_per_model', '4\n')
        after = plan_event(plan, dict(run='drift-uid'), src='import', inventory=False)
        self.assertEqual(after['data']['options'].get('retry_per_model'), '4')
        self.assertNotEqual(before['rid'], after['rid'])
