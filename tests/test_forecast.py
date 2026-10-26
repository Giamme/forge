"""`forge dataset forecast` and the advisory block `forge-parallel.sh run` prints before dispatching."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402

REGISTRY = 'sol\tcodex\tsol-model\nopus\tclaude\topus-model\n'


class History:
    """Hand-built past tasks: a plan, its attempts and their dwarf and QA dispatches."""

    def __init__(self):
        self.events, self.n = [], 0

    def task(self, tier, alias, model, effort, outcomes, tokens=None, seconds=None, run=None):
        """One past task; outcomes is its attempts' statuses, each attempt a dwarf and a QA dispatch.

        tokens and seconds are the dwarf dispatch's (input, output) and duration; QA adds
        (200, 0) tokens and 30s per attempt, so a task's total is easy to work out by hand.
        """
        self.n += 1
        run = run or 'past%d' % self.n
        tid = 't%d' % self.n
        self.events.append(dict(rid='p%d' % self.n, kind='plan', run=run, mode='parallel', ts=1000 + self.n,
                                data=dict(tasks={tid: dict(difficulty=tier, dwarf=alias)})))
        for n, status in enumerate(outcomes, 1):
            ts = 1000 + self.n + n
            self.events.append(dict(rid='a%d.%d' % (self.n, n), kind='attempt', run=run, mode='parallel', task=tid,
                                    attempt=n, ts=ts, data=dict(status=status)))
            dwarf = dict(role='dwarf', alias=alias, model=model, effort=effort)
            qa = dict(role='qa', alias='opus', model='opus-model', effort='high', verdict=status)
            if tokens:
                # cached_input is part of input, so it must not be added a second time
                dwarf['tokens'] = dict(input=tokens[0], cached_input=999, output=tokens[1])
                qa['tokens'] = dict(input=200, output=0)
            if seconds:
                dwarf['duration_s'], qa['duration_s'] = seconds, 30
            for role, data in (('d', dwarf), ('q', qa)):
                self.events.append(dict(rid='%s%d.%d' % (role, self.n, n), kind='dispatch', run=run, mode='parallel',
                                        task=tid, attempt=n, ts=ts, data=data))


def plan_dir(root: Path, rows, uid='current', merged=()):
    """A plan dir as `plan` leaves it: tasks.tsv, waves.tsv and the run's dataset uid."""
    p = root / 'plan'
    p.mkdir()
    (p / 'tasks.tsv').write_text(''.join('%s\t-\t%s\ta.txt\t%s\topus\t%s\n' % (i, tier, spec, i.upper())
                                         for i, tier, spec, _w in rows))
    (p / 'waves.tsv').write_text(''.join('%s\t%s\n' % (w, i) for i, _t, _s, w in rows))
    (p / 'dataset.uid').write_text(uid + '\n')
    for i in merged:
        (p / 'tasks' / i).mkdir(parents=True)
        (p / 'tasks' / i / 'merged').write_text('x\n')
    return p


class ForecastReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'data'
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.root / 'registry.tsv').write_text(REGISTRY)
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.home),
                                          'FORGE_REGISTRY': str(self.root / 'registry.tsv')})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('FORGE_FORECAST', 'FORGE_FORECAST_BUDGET_S', 'FORGE_DATASET'):
            os.environ.pop(name, None)

    def forge(self, *args, env=None):
        r = subprocess.run([str(ROOT / 'forge'), 'dataset', 'forecast', *map(str, args)], capture_output=True,
                           text=True, env=dict(os.environ, **(env or {})))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def seed(self):
        h = History()
        for i in range(1, 7):
            # four pass first; two fail first and pass on attempt 2, paying for both attempts
            h.task('low', 'sol', 'sol-model', 'high', ['PASS'] if i <= 4 else ['FAIL', 'PASS'],
                   tokens=(1000 * i, 100), seconds=60 * i)
        for i in range(5):
            h.task('high', 'opus', 'opus-model', 'max', ['PASS'] if i == 0 else ['FAIL'])
        for i in range(2):
            h.task('medium', 'sol', 'sol-model', 'high', ['PASS'])
        # this run's own earlier tasks are not history for its forecast
        h.task('low', 'sol', 'sol-model', 'high', ['PASS'], tokens=(1, 1), seconds=1, run='current')
        self.assertEqual(store.record(h.events, repo=self.repo), len(h.events))
        return plan_dir(self.root, [('a', 'low', 'sol:high', 1), ('b', 'high', 'opus', 1),
                                    ('c', 'medium', 'sol', 2), ('d', 'low', 'sol:medium', 2), ('e', 'low', 'sol', 2)],
                        merged=['e'])

    def test_each_task_gets_what_its_tier_and_model_cost_before(self):
        p = self.seed()
        fc = json.loads(self.forge(p, '--json'))
        rows = {r['task']: r for r in fc['tasks']}
        a, b, c, d = rows['a'], rows['b'], rows['c'], rows['d']
        self.assertEqual(set(rows), {'a', 'b', 'c', 'd'})
        self.assertEqual(fc['merged'], ['e'])
        self.assertEqual((a['model'], a['effort'], a['history'], a['enough'], a['flag']),
                         ('sol-model', 'high', 6, True, False))
        # tokens per task: 1300 2300 3300 4300, then two attempts each: 10600 12600
        self.assertEqual(a['tokens'], dict(n=6, median=3800, p90=12600))
        # seconds per task: 90 150 210 270, then 660 780
        self.assertEqual(a['time_s'], dict(n=6, median=240, p90=780))
        self.assertEqual((a['first_pass']['k'], a['first_pass']['n']), (4, 6))
        self.assertAlmostEqual(a['first_pass']['rate'], 4 / 6)
        # Wilson 95% for 4 of 6
        self.assertAlmostEqual(a['first_pass']['ci'][0], 0.3000, places=4)
        self.assertAlmostEqual(a['first_pass']['ci'][1], 0.9032, places=4)
        # a spec without an effort matches every effort of its model; 1 of 5 is flagged
        self.assertEqual((b['model'], b['effort'], b['enough'], b['flag']), ('opus-model', None, True, True))
        self.assertAlmostEqual(b['first_pass']['rate'], 0.2)
        self.assertEqual(b['tokens'], dict(n=0, median=None, p90=None))
        self.assertEqual((c['enough'], c['flag'], c['first_pass']['rate'], c['note']),
                         (False, False, None, 'not enough history (2/5)'))
        # an effort the history never ran is not the same arm
        self.assertEqual((d['history'], d['note']), (0, 'not enough history (0/5)'))
        total = fc['total']
        self.assertEqual((total['tasks'], total['forecast'], total['flagged']), (4, 2, 1))
        self.assertEqual(total['tokens'], dict(tasks=1, median=3800, p90=12600))
        self.assertEqual(total['time_s'], dict(tasks=1, median=240, p90=780, by_waves=240))
        self.assertAlmostEqual(total['first_pass_expected'], 4 / 6 + 0.2)

        text = self.forge(p)
        [a_line] = [line for line in text.splitlines() if line.split()[:1] == ['a']]
        self.assertIn('first try 67% [30%-90%] (4/6)', a_line)
        self.assertIn('tokens 4k (p90 13k)', a_line)
        self.assertIn('time 4m00s (p90 13m00s)', a_line)
        self.assertNotIn('retry', a_line)
        [b_line] = [line for line in text.splitlines() if line.split()[:1] == ['b']]
        self.assertTrue(b_line.endswith('<- likely to need a retry'), b_line)
        [c_line] = [line for line in text.splitlines() if line.split()[:1] == ['c']]
        self.assertTrue(c_line.endswith('not enough history (2/5)'), c_line)
        self.assertNotIn('%', c_line)
        self.assertIn('expected first-try passes: 0.9 of 2 forecast task(s), 1 flagged under 50%', text)

        self.assertEqual(json.loads(self.forge(p, '--json', '--repo', self.repo))['tasks'][0]['history'], 6)
        self.assertIsNone(json.loads(self.forge(p, '--json', '--repo', self.root)))   # no history from that repo

    def test_no_store_prints_nothing_to_forecast_and_creates_none(self):
        p = plan_dir(self.root, [('a', 'low', 'sol', 1)])
        self.assertIn('no task history', self.forge(p))
        self.assertIsNone(json.loads(self.forge(p, '--json')))
        self.assertEqual(self.forge(p, '--run'), '')
        self.assertFalse(self.home.exists())
        self.assertFalse((p / 'forecast.json').exists())

    def forecasts(self):
        db = sqlite3.connect(str(self.home / 'dataset' / 'dataset.sqlite'))
        try:
            return [(rid, json.loads(data)) for rid, data in
                    db.execute("SELECT rid, data FROM events WHERE kind = 'forecast'")]
        finally:
            db.close()

    def test_the_run_block_is_recorded_once_so_it_can_be_checked_later(self):
        p = self.seed()
        out = self.forge(p, '--run')
        self.assertIn('first try 67% [30%-90%] (4/6)', out)
        saved = json.loads((p / 'forecast.json').read_text())
        self.assertEqual([r['task'] for r in saved['tasks']], ['a', 'b', 'c', 'd'])
        [(rid, data)] = self.forecasts()
        self.assertTrue(rid.startswith('forecast:current:'), rid)
        self.assertEqual(data['tasks'], saved['tasks'])
        self.assertEqual(self.forge(p, '--run'), out)      # a resumed run with the same forecast adds no event
        self.assertEqual(len(self.forecasts()), 1)

    def test_the_run_block_puts_tasks_without_enough_history_on_one_line(self):
        p = self.seed()
        out = self.forge(p, '--run').splitlines()
        rows = [line.split()[0] for line in out if line.split()[:1] in (['a'], ['b'], ['c'], ['d'])]
        self.assertEqual(rows, ['a', 'b'])             # c and d have 2 and 0 past tasks: no row each
        [thin] = [line for line in out if 'without enough history' in line]
        self.assertTrue(thin.startswith('  2 task(s)') and thin.endswith(': c, d'), thin)
        self.assertIn('not enough history (2/5)', self.forge(p))   # the command itself still shows every task

        (self.root / 'thin').mkdir()
        q = plan_dir(self.root / 'thin', [('c', 'medium', 'sol', 1), ('d', 'low', 'sol:medium', 1)])
        [line] = self.forge(q, '--run').splitlines()   # a plan with no history to show is one line, not a table
        self.assertIn('2 task(s)', line)
        self.assertIn('forge dataset forecast %s' % q.resolve(), line)
        self.assertTrue((q / 'forecast.json').exists())

    def test_the_run_block_can_be_turned_off_and_never_waits_past_its_budget(self):
        p = self.seed()
        self.assertEqual(self.forge(p, '--run', env=dict(FORGE_FORECAST='off')), '')
        late = self.forge(p, '--run', env=dict(FORGE_FORECAST_BUDGET_S='0.000001'))
        self.assertEqual(len(late.splitlines()), 1, late)
        self.assertIn('skipped', late)
        self.assertFalse((p / 'forecast.json').exists())
        self.assertIn('first try', self.forge(p, '--run', env=dict(FORGE_DATASET='off')))
        self.assertEqual(self.forecasts(), [])


class RunForecastTests(unittest.TestCase):
    """Through forge-parallel.sh with the fake CLI: printed before dispatch, and nothing else changes."""
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script

    def plan(self, home):
        self.env['FORGE_DATA_HOME'] = str(home)
        p = self.root / 'plan'
        p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks' / 'a').mkdir(parents=True)
        (p / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def seeded(self):
        home = self.root / 'data'
        h = History()
        for _ in range(5):
            h.task('low', 'sol', fixtures_model(), None, ['FAIL', 'PASS'], tokens=(1000, 0), seconds=60)
        with patch.dict(os.environ, {'FORGE_DATA_HOME': str(home)}):
            self.assertEqual(store.record(h.events, repo=self.repo), len(h.events))
        return self.plan(home)

    def test_run_prints_the_forecast_before_dispatching(self):
        p = self.seeded()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('forge: forecast from 5 past task(s)', r.stdout)
        self.assertIn('(0/5)', r.stdout)
        self.assertLess(r.stdout.index('forge: forecast from'), r.stdout.index('dispatched a'))
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertTrue((p / 'forecast.json').exists())

    def test_no_forecast_hides_it_and_changes_nothing_else(self):
        p = self.seeded()
        r = self.run_script('forge-parallel.sh', 'run', p, '--no-forecast')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('dispatched a', r.stdout)
        self.assertNotIn('forecast', r.stdout)
        self.assertFalse((p / 'forecast.json').exists())


def fixtures_model():
    """The model the fake codex reports for `sol`, which is what live capture records."""
    return 'gpt-6.1-sol'


if __name__ == '__main__':
    unittest.main()
