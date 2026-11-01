"""--infra-fallback (#30, #81): a dwarf's lateral stop (capacity, a usage limit, ...) moves the task to
the next healthy model of its own tier pool, recorded like an escalation with source `fallback`. The stopped model is
still paused and counted, so a pool that is all at capacity ends the run after a bounded
number of dispatches. Runs drive the real forge-parallel.sh with the fake CLIs of
test_forge.py; sol and astra both run on codex, opus on claude."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
        return route.fallback_pick(current, {'medium': pool}, self.registry, route.parse_excluded(excluded), 'medium')

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
        pick = lambda current, excluded, tier='medium': route.fallback_pick(  # noqa: E731
            current, pools, self.registry, route.parse_excluded(excluded), tier)
        self.assertEqual(pick('sol', ''), 'astra')
        self.assertIsNone(pick('sol', 'codex/gpt-6-astra'))     # sonnet and opus are free, but not peers
        self.assertIsNone(pick('sol', 'codex/*'))
        self.assertIsNone(pick('opus', '', 'high'))              # a pool of one has no peer
        self.assertIsNone(pick('sonnet', '', 'low'))

    def test_a_spec_in_no_pool_has_no_fallback(self):
        self.assertIsNone(self.pick('opus', ['sol', 'astra']))

    def test_the_same_model_at_another_effort_is_no_fallback(self):
        self.assertEqual(self.pick('sol:high', ['sol:high', 'sol:xhigh', 'astra']), 'astra')
        self.assertIsNone(self.pick('sol:high', ['sol:high', 'sol:xhigh']))


class RunningTierTests(unittest.TestCase):
    """`forge-route.py fallback`, which reads the tier a task runs in from its records."""

    def fallback(self, dwarf, difficulty, pools, escalations=(), route_record=None, exclude='', replanned=None,
                 unrecorded=()):
        """`escalations` are recorded as forge does, with `pools`; `unrecorded` rows as a plan made before
        running.tier left them. `replanned` are the pools from then on."""
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp)
            (plan / 'tasks/a').mkdir(parents=True)
            (plan / 'tasks.tsv').write_text(f'a\t-\t{difficulty}\tf\t{dwarf}\topus\tA\n')
            (plan / 'fractal-routing.json').write_text(json.dumps(pools))
            if route_record:
                (plan / 'tasks/a/route.jsonl').write_text(route_record + '\n')
            if unrecorded:
                (plan / 'tasks/a/escalations.tsv').write_text(
                    ''.join('1\t1\t' + '\t'.join(row) + '\n' for row in unrecorded))
            for row in escalations:
                subprocess.run([sys.executable, str(ROOT / 'scripts/forge-route.py'), 'record', '--plan', tmp,
                                '--task', 'a', '--line', '1\t1\t' + '\t'.join(row)], check=True)
            if replanned:
                (plan / 'fractal-routing.json').write_text(json.dumps(replanned))
            done = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-route.py'), 'fallback', '--plan', tmp,
                                   '--task', 'a', '--exclude', exclude], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0 if done.stdout else 1, done.stderr)
            return done.stdout.strip() or None

    def test_the_command_prints_the_pick_or_exits_1(self):
        pools = {'medium': ['sol', 'astra']}
        self.assertEqual(self.fallback('sol', 'medium', pools, exclude='codex/gpt-6.1-sol'), 'astra')
        self.assertIsNone(self.fallback('sol', 'medium', pools, exclude='codex/gpt-6-astra'))

    def test_a_model_in_two_pools_stays_in_the_tier_of_its_task(self):
        pools = {'medium': ['sol'], 'high': ['sol', 'sonnet']}
        self.assertIsNone(self.fallback('sol', 'medium', pools))      # sonnet is a high-tier model
        self.assertEqual(self.fallback('sol', 'high', pools), 'sonnet')
        pools['medium'] = ['sol', 'astra']
        self.assertEqual(self.fallback('sol', 'medium', pools), 'astra')

    def test_a_model_named_by_hand_outside_its_tier_does_not_move(self):
        pools = {'medium': ['sol', 'astra'], 'high': ['sonnet', 'opus']}
        self.assertIsNone(self.fallback('sonnet', 'medium', pools))

    def test_an_escalated_task_stays_in_the_target_tier_of_its_escalation(self):
        # A low task escalated to the medium pool's sol: sol is also in the high pool, but the task runs in medium.
        pools = {'low': ['astra'], 'medium': ['sol'], 'high': ['sol', 'sonnet']}
        rows = [('astra', 'sol', 'policy')]
        self.assertIsNone(self.fallback('sol', 'low', pools, rows))
        pools['medium'] = ['sol', 'opus']
        self.assertEqual(self.fallback('sol', 'low', pools, rows), 'opus')

    def test_the_policy_tier_is_the_one_its_target_was_picked_from(self):
        # astra's highest pool is medium, so the policy picked sol from high, though medium lists sol too.
        pools = {'low': ['astra'], 'medium': ['astra', 'sol'], 'high': ['sol', 'sonnet']}
        self.assertEqual(self.fallback('sol', 'low', pools, [('astra', 'sol', 'policy')]), 'sonnet')

    def test_a_model_picked_by_a_person_runs_in_the_nearest_pool_listing_it(self):
        pools = {'low': ['astra'], 'medium': ['sol', 'opus'], 'high': ['sol', 'sonnet']}
        self.assertEqual(self.fallback('sol', 'low', pools, [('astra', 'sol', 'pin')]), 'opus')
        self.assertEqual(self.fallback('sol', 'low', pools, [('astra', 'sol', 'retry')]), 'opus')
        self.assertEqual(self.fallback('sol', 'high', pools, [('sonnet', 'sol', 'pin')]), 'sonnet')

    def test_a_fallback_or_an_effort_step_stays_in_its_tier(self):
        pools = {'low': ['astra', 'luna'], 'medium': ['sol', 'opus'], 'high': ['luna', 'sonnet']}
        self.assertEqual(self.fallback('luna', 'low', pools, [('astra', 'luna', 'fallback')]), 'astra')
        self.assertEqual(self.fallback('astra:high', 'low', pools, [('astra', 'astra:high', 'policy')]), 'luna')

    def test_a_re_plan_does_not_change_the_recorded_tier_of_an_escalation(self):
        # Escalated into medium; the re-plan moves sol to the high pool only. It still runs in medium.
        rows = [('luna', 'sol', 'policy')]
        pools = {'low': ['luna'], 'medium': ['sol'], 'high': ['opus']}
        replanned = {'low': ['luna'], 'medium': ['astra'], 'high': ['sol', 'sonnet']}
        self.assertIsNone(self.fallback('sol', 'low', pools, rows, replanned=replanned))
        replanned['medium'] = ['sol', 'astra']
        self.assertEqual(self.fallback('sol', 'low', pools, rows, replanned=replanned), 'astra')

    def test_a_fallback_after_an_escalation_keeps_the_recorded_tier(self):
        pools = {'low': ['luna'], 'medium': ['sol', 'astra'], 'high': ['astra', 'sonnet']}
        rows = [('luna', 'sol', 'policy'), ('sol', 'astra', 'fallback')]
        self.assertEqual(self.fallback('astra', 'low', pools, rows), 'sol')

    def test_a_record_for_another_spec_no_longer_counts(self):
        # The table was edited after the escalation: the task runs its difficulty's tier again.
        pools = {'low': ['luna', 'haiku'], 'medium': ['sol', 'astra']}
        self.assertEqual(self.fallback('luna', 'low', pools, [('haiku', 'sol', 'policy')]), 'haiku')

    def test_an_escalation_with_no_record_reads_as_the_difficulty(self):
        # A plan made before running.tier: never a tier inferred from the pools, so sol has no move from low.
        pools = {'low': ['luna'], 'medium': ['sol', 'astra'], 'high': ['sol', 'sonnet']}
        self.assertIsNone(self.fallback('sol', 'low', pools, unrecorded=[('luna', 'sol', 'policy')]))

    def test_a_task_exploring_a_lower_tier_stays_there(self):
        # A medium task exploring low-tier sol: sol is also in the high pool.
        pools = {'low': ['sol'], 'medium': ['astra'], 'high': ['sol', 'sonnet']}
        record = json.dumps({'chosen': 'sol', 'chosen_tier': 'low', 'explored': True, 'tier': 'medium'})
        self.assertIsNone(self.fallback('sol', 'medium', pools, route_record=record))
        pools['low'] = ['sol', 'luna']
        self.assertEqual(self.fallback('sol', 'medium', pools, route_record=record), 'luna')

    def test_a_task_exploring_a_stronger_tier_runs_in_that_tier(self):
        pools = {'low': ['astra'], 'medium': ['sol', 'opus'], 'high': ['sol', 'sonnet']}
        record = json.dumps({'chosen': 'sol', 'chosen_tier': 'medium', 'explored': True, 'tier': 'low'})
        self.assertEqual(self.fallback('sol', 'low', pools, route_record=record), 'opus')

    def test_an_exploration_then_an_escalation_counts_from_the_explored_tier(self):
        pools = {'low': ['astra', 'luna'], 'medium': ['sol', 'opus'], 'high': ['sonnet']}
        record = json.dumps({'chosen': 'astra', 'chosen_tier': 'low', 'explored': True, 'tier': 'medium'})
        self.assertEqual(self.fallback('luna', 'medium', pools, [('astra', 'luna', 'pin')], record), 'astra')

    def test_an_exploration_the_user_replaced_at_the_gate_no_longer_counts(self):
        pools = {'low': ['sol', 'luna'], 'medium': ['astra', 'sonnet']}
        record = json.dumps({'chosen': 'sol', 'chosen_tier': 'low', 'explored': True, 'tier': 'medium'})
        self.assertEqual(self.fallback('sonnet', 'medium', pools, route_record=record), 'astra')

    def test_a_plan_without_a_usable_record_uses_the_difficulty(self):
        # Made before chosen_tier was recorded: the explored model is not in the difficulty's pool, so no move.
        pools = {'low': ['sol'], 'medium': ['astra'], 'high': ['sol', 'sonnet']}
        old = json.dumps({'chosen': 'sol', 'explored': True, 'tier': 'medium'})
        self.assertIsNone(self.fallback('sol', 'medium', pools, route_record=old))
        self.assertIsNone(self.fallback('sol', 'medium', pools, route_record='not json'))

    def test_exploration_records_the_tier_of_its_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp)
            (plan / 'tasks.tsv').write_text('a\t-\tmedium\tf\tastra\topus\tA\n')
            pools = plan / 'fractal-routing.json'
            pools.write_text(json.dumps({'low': ['sol'], 'medium': ['astra'], 'high': ['sol', 'sonnet']}))
            for seed in range(30):
                subprocess.run([sys.executable, str(ROOT / 'scripts/forge-route.py'), 'explore', '--plan', tmp,
                                '--task', 'a', '--tier', 'medium', '--epsilon', '1', '--default', 'astra',
                                '--pools', str(pools)], capture_output=True, text=True, check=True,
                               env=dict(os.environ, FORGE_ROUTE_EXPLORE_SEED=str(seed)))
            decisions = [json.loads(line) for line in (plan / 'tasks/a/route.jsonl').read_text().splitlines()]
            self.assertEqual({(d['chosen'], d['chosen_tier']) for d in decisions},
                             {('astra', 'medium'), ('sol', 'low')})


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

    def test_the_tier_is_recorded_when_the_task_moves(self):
        p = self.plan('sol,astra')
        self.scenario({'a.dwarf.1': CAPACITY})
        self.assertEqual(self.run_plan(p, '--infra-retries', '0').returncode, 0)
        self.assertEqual((p / 'tasks/a/running.tier').read_text(), 'medium\tastra\n')

    def test_a_model_the_high_pool_also_lists_does_not_move_a_medium_task_up(self):
        p = self.plan('sol', 'opus', '--dwarf-high', 'sol,sonnet')
        self.scenario({'a.dwarf.1': USAGE_LIMIT})
        r = self.run_plan(p, '--infra-retries', '1', '--infra-backoff', '1')
        self.assertEqual((self.dwarf_column(p), self.escalations(p)), ('sol', []))
        self.assertNotIn('falling back', r.stdout)


if __name__ == '__main__':
    unittest.main()
