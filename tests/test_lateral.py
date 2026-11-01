"""A move to another model of the same tier is lateral, not an escalation (#81). An infrastructure stop
on a dwarf moves the task sideways: the record is tasks/<id>/lateral.tsv and a `lateral` dataset event,
never an escalations.tsv row. The retry policy's streak counts across the move, so two failures on A, a
budget stop that moves to B and one failure on B reach --escalate-after 3 as three failures on one model
do. An explicit `retry --dwarf B` is no move, and the streak restarts there.

The end-to-end runs drive the real forge-parallel.sh with the fake CLIs of test_forge.py, as
test_infra_fallback does. The streak rules are a table over failed_streak, and the policy is run on
task dirs written as the scheduler leaves them (attempts.tsv, lateral.tsv, escalations.tsv)."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_forge as fixtures
import test_dataset as dataset_fixtures

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forge_route', ROOT / 'scripts/forge-route.py')
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)

BUDGET = {'rc': 1, 'stdout': 'Insufficient balance.'}
FAIL = {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — still broken'}


class StreakTests(unittest.TestCase):
    """failed_streak over (attempt, status, spec) rows as attempts.tsv gives them, and (attempt, from, to)
    moves as lateral.tsv gives them."""

    def test_the_table(self):
        fail = 'FAIL'
        cases = [
            ('a lateral move keeps the count',
             [(1, fail, 'sol'), (2, fail, 'sol'), (3, fail, 'astra')], 'astra', [(2, 'sol', 'astra')], False, 3),
            ('a change with no move on record restarts it',
             [(1, fail, 'sol'), (2, fail, 'sol'), (3, fail, 'astra')], 'astra', [], False, 1),
            ('an escalation restarts it, beside a move of other models',
             [(1, fail, 'sol'), (2, fail, 'sol'), (3, fail, 'opus')], 'opus', [(2, 'sol', 'astra')], False, 1),
            ('a move made before the earlier failure does not carry',
             [(1, fail, 'sol'), (2, fail, 'sol'), (3, fail, 'astra')], 'astra', [(1, 'sol', 'astra')], False, 1),
            ('a move made after the later attempt began does not carry',
             [(1, fail, 'sol'), (2, fail, 'sol'), (3, fail, 'astra')], 'astra', [(3, 'sol', 'astra')], False, 1),
            ('an explicit move back does not inherit an older move',
             [(1, fail, 'sol'), (2, fail, 'sol'), (3, fail, 'astra'), (4, fail, 'sol'), (5, fail, 'astra')],
             'astra', [(2, 'sol', 'astra')], False, 1),
            ('a pass ends it',
             [(1, 'PASS', 'sol'), (2, fail, 'sol'), (3, fail, 'astra')], 'astra', [(2, 'sol', 'astra')], False, 2),
            ('infra stops judge nothing',
             [(1, fail, 'sol'), (2, fail, 'sol'), (2, 'INFRA', 'sol'), (3, fail, 'astra'), (3, 'INFRA', 'astra')],
             'astra', [(2, 'sol', 'astra')], False, 3),
            ('no failure on the new model yet carries nothing',
             [(1, fail, 'sol'), (2, fail, 'sol')], 'astra', [(2, 'sol', 'astra')], False, 0),
            ('a chain of moves carries across each hop',
             [(1, fail, 'sol'), (2, fail, 'sol'), (3, fail, 'astra'), (4, fail, 'opus')],
             'opus', [(2, 'sol', 'astra'), (3, 'astra', 'opus')], False, 4),
            ('by effort, a bump restarts it',
             [(1, fail, 'sol:high'), (2, fail, 'sol:xhigh')], 'sol:xhigh', [], True, 1),
            ('without by effort, a bump is the same model',
             [(1, fail, 'sol:high'), (2, fail, 'sol:xhigh')], 'sol:xhigh', [], False, 2),
            ('by effort, a move at the same effort keeps it',
             [(1, fail, 'sol:high'), (2, fail, 'astra:high')], 'astra:high', [(1, 'sol:high', 'astra:high')], True, 2),
        ]
        registry = route.registry_models()
        for name, rows, current, laterals, by_effort, expected in cases:
            with self.subTest(name):
                self.assertEqual(route.failed_streak(rows, current, registry, by_effort=by_effort, laterals=laterals),
                                 expected)

    def test_lateral_rows_reads_the_dwarf_moves_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            (tdir / 'lateral.tsv').write_text('1700000001\t2\tdwarf\tsol\tastra\tcapacity\n'
                                             '1700000002\t3\tqa\topus\tsonnet\tcapacity\n'
                                             'short\n')
            self.assertEqual(route.lateral_rows(tdir), [(2, 'sol', 'astra')])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(route.lateral_rows(Path(tmp)), [])


class PolicyTests(unittest.TestCase):
    """`forge-route.py escalation` on a task dir as the scheduler leaves it, with escalate_after 3 and a
    high pool to escalate into."""

    def decide(self, attempts, laterals=(), escalations=''):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp)
            tdir = plan / 'tasks' / 'a'
            tdir.mkdir(parents=True)
            (plan / 'tasks.tsv').write_text('a\t-\tmedium\tf\tastra\topus\tA\n')
            (plan / 'fractal-routing.json').write_text(json.dumps({'medium': ['sol', 'astra'], 'high': ['sonnet']}))
            (plan / 'escalate_after').write_text('3\n')
            (tdir / 'attempts.tsv').write_text(''.join('1700000000\t%d\t%s\t\t%s\n' % row for row in attempts))
            if laterals:
                (tdir / 'lateral.tsv').write_text(
                    ''.join('1700000000\t%d\tdwarf\t%s\t%s\tbudget\n' % row for row in laterals))
            if escalations:
                (tdir / 'escalations.tsv').write_text(escalations)
            done = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-route.py'), 'escalation',
                                   '--plan', tmp, '--task', 'a'], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()

    # Two failures on sol, a budget stop (attempt 2 refunded, so it is not counted), one failure on astra.
    ATTEMPTS = [(1, 'FAIL', 'sol'), (2, 'FAIL', 'sol'), (2, 'INFRA', 'sol'), (3, 'FAIL', 'astra')]

    def test_a_move_between_failures_escalates_as_three_failures_do(self):
        self.assertEqual(self.decide(self.ATTEMPTS, [(2, 'sol', 'astra')]), 'sonnet')

    def test_the_same_failures_without_the_move_restart_on_the_new_model(self):
        self.assertEqual(self.decide(self.ATTEMPTS), '')

    def test_an_explicit_retry_is_no_move(self):
        self.assertEqual(self.decide(self.ATTEMPTS, escalations='2\tsol\tastra\tretry\n'), '')

    def test_an_old_fallback_row_is_no_move_either(self):
        # Runs from before lateral moves: the move was an escalations.tsv `fallback` row, and it restarts the count as before.
        self.assertEqual(self.decide(self.ATTEMPTS, escalations='2\tsol\tastra\tfallback\n'), '')


class LateralRecordTests(unittest.TestCase):
    """`forge-route.py lateral`: the record, and the tier a move keeps."""

    def lateral(self, plan, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/forge-route.py'), 'lateral', '--plan', str(plan),
                               '--task', 'a', *args], capture_output=True, text=True)

    def test_a_move_keeps_the_tier_the_task_ran_in(self):
        # Escalated to high, though its difficulty is low: the move stays in high.
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp)
            (plan / 'tasks/a').mkdir(parents=True)
            (plan / 'tasks.tsv').write_text('a\t-\tlow\tf\tsol\topus\tA\n')
            (plan / 'tasks/a/running.tier').write_text('high\tsol\n')
            done = self.lateral(plan, '--line', '1\t1\tdwarf\tsol\tastra\tcapacity')
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual((plan / 'tasks/a/lateral.tsv').read_text(), '1\t1\tdwarf\tsol\tastra\tcapacity\n')
            self.assertEqual((plan / 'tasks/a/running.tier').read_text(), 'high\tastra\n')
            self.assertFalse((plan / 'tasks/a/escalations.tsv').exists())

    def test_a_symlinked_record_is_refused_and_nothing_is_written_outside(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / 'plan'
            (plan / 'tasks/a').mkdir(parents=True)
            (plan / 'tasks.tsv').write_text('a\t-\tmedium\tf\tsol\topus\tA\n')
            outside = Path(tmp) / 'outside.tsv'
            outside.write_text('')
            (plan / 'tasks/a/lateral.tsv').symlink_to(outside)
            self.assertEqual(self.lateral(plan, '--probe').returncode, 1)
            self.assertEqual(self.lateral(plan, '--line', '1\t1\tdwarf\tsol\tastra\tcapacity').returncode, 1)
            self.assertEqual(outside.read_text(), '')


class RunTests(unittest.TestCase):
    """End to end, with the fake CLIs: sol and astra run on codex, the high pool's sonnet on claude."""

    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    events = dataset_fixtures.StoreCase.events

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.env['FORGE_INFRA_BACKOFF'] = '0.05'
        self.home = self.root / 'data'
        self.env['FORGE_DATA_HOME'] = str(self.home)
        self.env.pop('FORGE_DATASET', None)

    def plan(self, pool='sol,astra', qa='opus', *extra):
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text(f'a\t-\tmedium\ta.txt\tsol\t{qa}\tA\n')
        (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks/a/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--explore', '0',
                            '--dwarf-medium', pool, *extra)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def run_plan(self, p, *extra):
        return self.run_script('forge-parallel.sh', 'run', p, *extra)

    def dwarf_column(self, p):
        return [line.split('\t')[4] for line in (p / 'tasks.tsv').read_text().splitlines() if line.startswith('a\t')][0]

    def rows(self, p, name):
        f = p / 'tasks/a' / name
        return [line.split('\t') for line in f.read_text().splitlines()] if f.exists() else []

    def escalations(self, p):
        """attempt, from, to, source of each escalations.tsv row."""
        return [row[1:] for row in self.rows(p, 'escalations.tsv')]

    def laterals(self, p):
        """attempt, role, from, to, cause of each lateral.tsv row."""
        return [row[1:] for row in self.rows(p, 'lateral.tsv')]

    def spent_on(self, p):
        """(attempt, status, spec) of each attempt spent; a stop that refunded its attempt is left out."""
        return [(r[1], r[2], r[4]) for r in self.rows(p, 'attempts.tsv') if r[2] != 'INFRA']

    def lateral_events(self):
        return [e for e in self.events() if e['rid'].startswith('event:lateral:')]

    # A budget is the whole codex harness: a peer must run on another harness to be a move. sonnet is claude's.
    def test_a_budget_stop_moves_sideways_and_is_recorded_as_a_lateral_move(self):
        p = self.plan('sol,sonnet')
        self.scenario({'a.dwarf.1': BUDGET})
        r = self.run_plan(p, '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertEqual(self.dwarf_column(p), 'sonnet')
        self.assertEqual(self.laterals(p), [['0', 'dwarf', 'sol', 'sonnet', 'budget']])
        self.assertEqual(self.escalations(p), [])
        self.assertIn('sol is out of budget; moving sideways to sonnet (same tier)', r.stdout)
        self.assertEqual([(e['task'], e['data']['role'], e['data']['from'], e['data']['to'], e['data']['cause'],
                           e['data']['attempt']) for e in self.lateral_events()],
                         [('a', 'dwarf', 'sol', 'sonnet', 'budget', '0')])
        self.assertEqual([e for e in self.events() if e['rid'].startswith('event:escalation:')], [])

    def test_two_failures_a_move_and_one_failure_on_the_new_model_escalate(self):
        p = self.plan('sol,sonnet', 'opus', '--dwarf-high', 'opus')
        self.scenario({'a.qa.1': FAIL, 'a.qa.2': FAIL, 'a.qa.3': FAIL,
                       'a.dwarf.1': {'content': 'try 1'}, 'a.dwarf.2': {'content': 'try 2'},
                       'a.dwarf.3': BUDGET,
                       'a.dwarf.4': {'content': 'try 4'}, 'a.dwarf.5': {'content': 'try 5'}})
        r = self.run_plan(p, '--retry-failed', '3', '--escalate-after', '3')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.spent_on(p), [('1', 'FAIL', 'sol'), ('2', 'FAIL', 'sol'), ('3', 'FAIL', 'sonnet'),
                                            ('4', 'PASS', 'opus')])
        self.assertEqual(self.laterals(p), [['2', 'dwarf', 'sol', 'sonnet', 'budget']])
        self.assertEqual(self.escalations(p), [['3', 'sonnet', 'opus', 'policy']])
        self.assertIn('escalating from sonnet to opus', r.stdout)

    def test_an_explicit_retry_to_the_new_model_restarts_the_count(self):
        p = self.plan('sol,sonnet', 'opus', '--dwarf-high', 'opus')
        self.scenario({'a.qa.1': FAIL, 'a.qa.2': FAIL, 'a.qa.3': FAIL,
                       'a.dwarf.1': {'content': 'try 1'}, 'a.dwarf.2': {'content': 'try 2'},
                       'a.dwarf.3': {'content': 'try 3'}})
        self.run_plan(p, '--retry-failed', '1')
        self.assertEqual(self.spent_on(p), [('1', 'FAIL', 'sol'), ('2', 'FAIL', 'sol')])
        self.run_script('forge-parallel.sh', 'retry', p, 'a', '--dwarf', 'sonnet')    # a FAIL verdict exits 5
        self.assertEqual(self.spent_on(p), [('1', 'FAIL', 'sol'), ('2', 'FAIL', 'sol'), ('3', 'FAIL', 'sonnet')])
        self.assertEqual(self.escalations(p), [['2', 'sol', 'sonnet', 'retry']])
        self.assertEqual(self.laterals(p), [])
        self.assertEqual(self.calls().count('dwarf'), 3)     # no fourth attempt: one failure on sonnet is not three

    def test_a_move_that_cannot_be_recorded_moves_nothing(self):
        p = self.plan('sol,sonnet')
        outside = self.root / 'outside.tsv'
        outside.write_text('')
        (p / 'tasks/a/lateral.tsv').symlink_to(outside)
        self.scenario({'a.dwarf.1': BUDGET})
        r = self.run_plan(p, '--infra-retries', '1')
        self.assertEqual(self.dwarf_column(p), 'sol')
        self.assertEqual(outside.read_text(), '')
        self.assertIn('could not record the lateral move', r.stdout)
        self.assertEqual(self.lateral_events(), [])


if __name__ == '__main__':
    unittest.main()
