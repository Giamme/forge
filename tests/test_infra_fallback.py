"""--infra-fallback (#30): a dwarf's capacity stop moves the task to the next healthy model of
its own tier pool, recorded like an escalation with source `fallback`. The stopped model is
still paused and counted, so a pool that is all at capacity ends the run after a bounded
number of dispatches. Runs drive the real forge-parallel.sh with the fake CLIs of
test_forge.py; sol and astra both run on codex, opus on claude."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

import test_forge as fixtures
from test_dispatch import CODEX_CAPACITY

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forge_route', ROOT / 'scripts/forge-route.py')
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)

CAPACITY = {'rc': 1, 'stdout': CODEX_CAPACITY}


class PickTests(unittest.TestCase):
    def setUp(self):
        self.registry = route.registry_models()

    def pick(self, current, pool, excluded=''):
        return route.fallback_pick(current, {'medium': pool}, self.registry, route.parse_excluded(excluded))

    def test_the_next_member_in_pool_order_wrapping_around(self):
        pool = ['sol', 'astra', 'sonnet']
        self.assertEqual(self.pick('astra', pool), 'sonnet')
        self.assertEqual(self.pick('sonnet', pool), 'sol')
        self.assertEqual(self.pick('sol:high', pool), 'astra')     # the effort written does not matter

    def test_excluded_pairs_and_harnesses_are_skipped(self):
        pool = ['sol', 'astra', 'sonnet']
        self.assertEqual(self.pick('sol', pool, 'codex/gpt-6-astra'), 'sonnet')
        self.assertEqual(self.pick('sol', pool, 'codex/*'), 'sonnet')
        self.assertIsNone(self.pick('sol', pool, 'codex/gpt-6-astra,claude/sonnet'))

    def test_a_spec_in_no_pool_has_no_fallback(self):
        self.assertIsNone(self.pick('opus', ['sol', 'astra']))

    def test_the_same_model_at_another_effort_is_no_fallback(self):
        self.assertEqual(self.pick('sol:high', ['sol:high', 'sol:xhigh', 'astra']), 'astra')
        self.assertIsNone(self.pick('sol:high', ['sol:high', 'sol:xhigh']))

    def test_the_command_prints_the_pick_or_exits_1(self):
        with __import__('tempfile').TemporaryDirectory() as tmp:
            plan = Path(tmp)
            (plan / 'tasks.tsv').write_text('a\t-\tmedium\tf\tsol\topus\tA\n')
            (plan / 'fractal-routing.json').write_text(json.dumps({'medium': ['sol', 'astra']}))
            command = [sys.executable, str(ROOT / 'scripts/forge-route.py'), 'fallback', '--plan', tmp, '--task', 'a']
            done = subprocess.run(command + ['--exclude', 'codex/gpt-6.1-sol'], capture_output=True, text=True)
            self.assertEqual((done.returncode, done.stdout), (0, 'astra\n'))
            done = subprocess.run(command + ['--exclude', 'codex/gpt-6-astra'], capture_output=True, text=True)
            self.assertEqual((done.returncode, done.stdout), (1, ''))


class RunTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.env['FORGE_INFRA_BACKOFF'] = '0.05'

    def plan(self, pool='sol,astra', qa='opus'):
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text(f'a\t-\tmedium\ta.txt\tsol\t{qa}\tA\n')
        (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks/a/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--explore', '0',
                            '--dwarf-medium', pool)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def run_plan(self, p, *extra):
        return self.run_script('forge-parallel.sh', 'run', p, *extra)

    def dwarf_column(self, p):
        return [line.split('\t')[4] for line in (p / 'tasks.tsv').read_text().splitlines() if line.startswith('a\t')][0]

    def escalations(self, p):
        f = p / 'tasks/a/escalations.tsv'
        return [line.split('\t')[2:] for line in f.read_text().splitlines()] if f.exists() else []

    def test_a_capacity_stop_moves_to_the_next_model_of_the_pool_at_once(self):
        p = self.plan()
        self.scenario({'a.dwarf.1': CAPACITY})
        # A long backoff: waiting for sol's pause would show as a 30 s run.
        r = self.run_plan(p, '--infra-fallback', '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertEqual(self.dwarf_column(p), 'astra')
        self.assertEqual(self.escalations(p), [['sol', 'astra', 'fallback']])
        self.assertIn('sol is at capacity; falling back to astra (same tier)', r.stdout)
        self.assertNotIn('is over; resuming', r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)

    def test_a_pool_all_at_capacity_ends_after_a_bounded_number_of_dispatches(self):
        p = self.plan()
        self.scenario({'a.dwarf.*': CAPACITY})
        r = self.run_plan(p, '--infra-fallback', '--infra-retries', '1')
        self.assertEqual(r.returncode, 8, r.stdout)
        # Each stop pauses its model and counts against its budget of one wait, so each model
        # is tried at most twice: sol, astra, sol again once its short wait is over, then
        # astra, after which both budgets are spent and nothing is left to fall back to.
        self.assertEqual(self.calls().count('dwarf'), 4)
        self.assertEqual(self.escalations(p), [['sol', 'astra', 'fallback'], ['astra', 'sol', 'fallback'],
                                               ['sol', 'astra', 'fallback']])
        self.assertEqual((p / 'tasks/a/status').read_text().strip(), 'INFRA')

    def test_no_candidate_pauses_as_before(self):
        p = self.plan(pool='sol:high,sol:xhigh')        # one model at two efforts
        self.scenario({'a.dwarf.1': CAPACITY})
        r = self.run_plan(p, '--infra-fallback', '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('is over; resuming a', r.stdout)
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))

    def test_a_pin_wins_over_the_fallback(self):
        p = self.plan()
        (p / 'tasks/a/escalation.pin').write_text('sol:xhigh\n')
        self.scenario({'a.dwarf.1': CAPACITY})
        r = self.run_plan(p, '--infra-fallback', '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.escalations(p), [['sol', 'sol:xhigh', 'pin']])

    def test_a_qa_stage_capacity_stop_does_not_fall_back(self):
        p = self.plan(qa='astra')
        self.scenario({'a.qa.1': CAPACITY})
        r = self.run_plan(p, '--infra-fallback', '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))
        self.assertIn('is over; resuming a', r.stdout)

    def test_with_the_option_off_the_task_waits_for_its_model(self):
        p = self.plan()
        self.scenario({'a.dwarf.1': CAPACITY})
        r = self.run_plan(p, '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))
        self.assertIn('is over; resuming a', r.stdout)
        self.assertFalse((p / 'infra_fallback').exists())
        self.run_plan(p, '--infra-fallback')
        self.assertEqual((p / 'infra_fallback').read_text(), 'on\n')
        self.run_plan(p, '--no-infra-fallback')
        self.assertFalse((p / 'infra_fallback').exists())


if __name__ == '__main__':
    unittest.main()
