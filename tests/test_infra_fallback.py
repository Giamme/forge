"""--infra-fallback (#30, #81): a dwarf's lateral stop (capacity, a usage limit, ...) moves the task to
the next healthy model of its own tier pool, recorded like an escalation with source `fallback`. The stopped model is
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
USAGE_LIMIT = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}


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

    def test_a_model_of_another_tier_is_never_picked(self):
        pools = {'low': ['sonnet'], 'medium': ['sol', 'astra'], 'high': ['opus']}
        pick = lambda current, excluded: route.fallback_pick(  # noqa: E731
            current, pools, self.registry, route.parse_excluded(excluded))
        self.assertEqual(pick('sol', ''), 'astra')
        self.assertIsNone(pick('sol', 'codex/gpt-6-astra'))     # sonnet and opus are free, but not peers
        self.assertIsNone(pick('sol', 'codex/*'))
        self.assertEqual(pick('opus', ''), None)                 # a pool of one has no peer
        self.assertIsNone(pick('sonnet', ''))

    def test_a_model_in_two_pools_stays_in_the_tier_of_its_task(self):
        pools = {'medium': ['sol'], 'high': ['sol', 'sonnet']}
        pick = lambda tier: route.fallback_pick(  # noqa: E731
            'sol', pools, self.registry, set(), tier)
        self.assertIsNone(pick('medium'))           # sonnet is a high-tier model: not a peer of a medium task
        self.assertEqual(pick('high'), 'sonnet')
        self.assertEqual(pick(''), 'sonnet')        # no tier known: the highest pool that runs the model
        pools['medium'] = ['sol', 'astra']
        self.assertEqual(pick('medium'), 'astra')
        # An escalated task runs a model only a higher pool lists: that pool is its tier.
        self.assertEqual(route.fallback_pick('sonnet', pools, self.registry, set(), 'medium'), 'sol')

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

    def test_the_command_takes_the_tier_from_the_tasks_difficulty(self):
        with __import__('tempfile').TemporaryDirectory() as tmp:
            plan = Path(tmp)
            (plan / 'tasks.tsv').write_text('a\t-\tmedium\tf\tsol\topus\tA\n')
            (plan / 'fractal-routing.json').write_text(json.dumps({'medium': ['sol'], 'high': ['sol', 'sonnet']}))
            command = [sys.executable, str(ROOT / 'scripts/forge-route.py'), 'fallback', '--plan', tmp, '--task', 'a']
            done = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual((done.returncode, done.stdout), (1, ''))


class RunTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.env['FORGE_INFRA_BACKOFF'] = '0.05'

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
        r = self.run_plan(p, '--no-infra-fallback', '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))
        self.assertIn('is over; resuming a', r.stdout)
        self.assertEqual((p / 'infra_fallback').read_text(), 'off\n')
        self.run_plan(p, '--infra-fallback')
        self.assertEqual((p / 'infra_fallback').read_text(), 'on\n')

    def test_a_new_plan_has_the_fallback_on_and_the_flag_turns_it_off(self):
        self.assertEqual((self.plan() / 'infra_fallback').read_text(), 'on\n')
        (self.root / 'plan').rename(self.root / 'plan.1')
        p = self.plan('sol,astra', 'opus', '--no-infra-fallback')
        self.assertEqual((p / 'infra_fallback').read_text(), 'off\n')
        self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory',
                                          '--explore', '0', '--dwarf-medium', 'sol,astra')
        self.assertEqual((p / 'infra_fallback').read_text(), 'on\n')       # a re-plan is a new decision

    def test_a_new_plan_moves_a_capacity_stop_without_any_flag(self):
        p = self.plan()
        self.scenario({'a.dwarf.1': CAPACITY})
        r = self.run_plan(p, '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('astra', [['sol', 'astra', 'fallback']]))

    def test_a_plan_without_the_file_does_not_fall_back(self):
        p = self.plan()
        (p / 'infra_fallback').unlink()          # a plan made before the fallback was on by default
        self.scenario({'a.dwarf.1': CAPACITY})
        r = self.run_plan(p, '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))
        self.assertIn('is over; resuming a', r.stdout)

    def test_control_set_off_stops_the_next_lateral_move(self):
        p = self.plan()
        control = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-control.py'), 'set', str(p),
                                  'infra-fallback', 'off'], capture_output=True, text=True, env=self.env)
        self.assertEqual(control.returncode, 0, control.stderr)
        self.assertIn('applies at the next infrastructure stop', control.stdout)
        self.assertEqual((p / 'infra_fallback').read_text(), 'off\n')
        shown = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-control.py'), 'show', str(p), '--json'],
                               capture_output=True, text=True, env=self.env)
        self.assertEqual(json.loads(shown.stdout)['run']['infra_fallback'], 'off')
        self.scenario({'a.dwarf.1': CAPACITY})
        r = self.run_plan(p, '--infra-retries', '1')
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))
        self.assertIn('is over; resuming a', r.stdout)

    def test_a_usage_limit_moves_to_a_model_of_another_harness_without_waiting(self):
        p = self.plan(pool='sol,astra,sonnet')
        self.scenario({'a.dwarf.1': USAGE_LIMIT})
        r = self.run_plan(p, '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        # astra is a codex model like sol: the whole harness is out, so the pick is sonnet on claude.
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sonnet', [['sol', 'sonnet', 'fallback']]))
        self.assertIn('sol stopped (usage_limit); falling back to sonnet (same tier)', r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)

    def test_a_pool_whose_only_free_model_is_in_another_tier_gives_no_move(self):
        p = self.plan('sol', 'opus', '--dwarf-low', 'astra', '--dwarf-high', 'sonnet')
        self.scenario({'a.dwarf.1': CAPACITY})
        r = self.run_plan(p, '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))
        self.assertIn('is over; resuming a', r.stdout)

    def test_a_model_the_high_pool_also_lists_does_not_move_a_medium_task_up(self):
        p = self.plan('sol', 'opus', '--dwarf-high', 'sol,sonnet')
        self.scenario({'a.dwarf.1': USAGE_LIMIT})
        r = self.run_plan(p, '--infra-retries', '1', '--infra-backoff', '1')
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))
        self.assertNotIn('falling back', r.stdout)


if __name__ == '__main__':
    unittest.main()
