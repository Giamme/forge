"""`forge dataset history` (#126): a run's review findings and learnings, answered from the store after its run
dir is gone, for forge-ui's Projects screen. The real scripts with the fake agent CLI, as in test_dataset."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

import test_dataset
import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
P1 = '- [P1][CONFIRMED] a.txt:1 — realistic input breaks (x)'


class HistoryTests(test_dataset.StoreCase):
    setUp_fixture = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    setUp = test_dataset.EndToEndTests.setUp
    plan_of = test_dataset.EndToEndTests.plan_of

    def history(self):
        done = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-dataset.py'), 'history', '--json'], env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_a_deleted_runs_findings_and_learnings_are_still_answered(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': P1},
                       'a.dwarf.1': {'message': 'implemented\nFORGE_LEARNING: trap | a.txt is generated [a.txt]'},
                       'a.dwarf.2': {'content': 'second'}})
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', '1').returncode, 0)
        uid = (p / 'dataset.uid').read_text().strip()
        shutil.rmtree(p)
        got = self.history()
        self.assertTrue(got['enabled'])
        [run] = [r for r in got['runs'] if r['run'] == uid]
        self.assertEqual((run['run_id'], run['repo_path'], len(run['base'] or '')), ('plan', str(self.repo.resolve()), 40))
        [review] = [f for f in got['findings'] if f['run'] == uid]
        self.assertEqual((review['task'], review['attempt'], review['role'], review['verdict']), ('a', 1, 'qa', 'FAIL'))
        self.assertEqual(review['findings'], [dict(head=P1, severity=1, status='CONFIRMED', line=review['findings'][0]['line'])])
        self.assertIn(dict(run=uid, run_id='plan', task='a', role='dwarf', category='trap', text='a.txt is generated [a.txt]'),
                      [{k: l[k] for k in ('run', 'run_id', 'task', 'role', 'category', 'text')} for l in got['learnings']])

    def test_with_the_dataset_off_it_is_empty_and_says_so(self):
        self.env['FORGE_DATASET'] = 'off'
        self.assertEqual(self.history(), dict(enabled=False, runs=[], findings=[], learnings=[], ledger=[]))


if __name__ == '__main__':
    unittest.main()
