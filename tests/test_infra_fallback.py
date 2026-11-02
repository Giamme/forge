"""--infra-fallback (#30, #81): a dwarf's lateral stop (capacity, a usage limit, ...) moves the task to
the next healthy model of its own tier pool, recorded in lateral.tsv and not as an escalation. The stopped model is
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
BUDGET = {'rc': 1, 'stdout': json.dumps({'type': 'result', 'is_error': True, 'result': 'Credit balance is too low'})}
CODEX_BUDGET = {'rc': 1, 'stdout': '{"type":"error","message":"You exceeded your current quota, please check your plan '
                                  'and billing details."}\n{"type":"turn.failed","error":{"message":"You exceeded your '
                                  'current quota."}}'}
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
                 unrecorded=(), qa='kimi'):
        """`escalations` are recorded as forge does, with `pools`; `unrecorded` rows as a plan made before
        running.tier left them. `replanned` are the pools from then on. The reviewer, `qa`, is in no pool
        unless a test puts it there: a move never lands on it."""
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp)
            (plan / 'tasks/a').mkdir(parents=True)
            (plan / 'tasks.tsv').write_text(f'a\t-\t{difficulty}\tf\t{dwarf}\t{qa}\tA\n')
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

    def test_the_tasks_reviewer_is_never_its_implementers_peer(self):
        # The final review of run 1: sol stopped and the move landed on astra, the task's own reviewer.
        pools = {'medium': ['sol', 'astra', 'sonnet']}
        self.assertEqual(self.fallback('sol', 'medium', pools, qa='astra'), 'sonnet')
        self.assertEqual(self.fallback('sol', 'medium', pools, qa='astra:xhigh'), 'sonnet')   # any effort of it
        self.assertIsNone(self.fallback('sol', 'medium', {'medium': ['sol', 'astra']}, qa='astra'))
        self.assertEqual(self.fallback('sol', 'medium', {'medium': ['sol', 'astra']}, qa='-'), 'astra')
        # The reviewer's model through another harness is the same model (round 2 of the final review).
        self.assertEqual(self.fallback('opus', 'low', {'low': ['opus', 'sol:high:opencode', 'astra']}, qa='sol'), 'astra')

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


class QaPickTests(unittest.TestCase):
    def setUp(self):
        self.registry = route.registry_models()

    def pick(self, current, implementer, pool, excluded=''):
        return route.qa_fallback_pick(current, implementer, {'medium': pool}, self.registry,
                                      route.parse_excluded(excluded), 'medium')

    def test_the_implementers_model_on_another_harness_is_still_the_implementer(self):
        # sol is gpt-6.1-sol on codex and github-copilot/gpt-6.1-sol on opencode: one model, two ids
        self.assertIsNone(self.pick('opus', 'sol', ['opus', 'sol:high:opencode']))
        self.assertIsNone(self.pick('opus', 'sol:high:opencode', ['opus', 'sol']))
        self.assertEqual(self.pick('opus', 'sol', ['opus', 'sol:high:opencode', 'astra']), 'astra')

    def test_a_literal_id_of_the_implementers_model_is_the_implementer(self):
        self.assertIsNone(self.pick('opus', 'gpt-6.1-sol', ['opus', 'sol:high:opencode']))

    def test_another_model_listed_under_the_implementers_alias_is_a_peer(self):
        # the bundled gemini alias is Flash on antigravity and Pro on opencode: two models, one alias
        pro = 'github-copilot/gemini-3.1-pro-preview::opencode'
        self.assertEqual(self.pick('opus', 'gemini-flash', ['opus', pro]), pro)
        self.assertIsNone(self.pick('opus', 'gemini-pro', ['opus', 'gemini-pro:high']))

    def test_a_registry_from_the_environment_resolves_custom_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = Path(tmp) / 'registry.tsv'
            registry.write_text('review-a\tcodex\tmodel-a\t<=max\nreview-b\tclaude\tmodel-b\t<=max\n')
            plan = Path(tmp) / 'plan'
            plan.mkdir()
            (plan / 'tasks.tsv').write_text('a\t-\tmedium\ta.txt\tsol\treview-a\tA\n')
            (plan / 'qa-routing.json').write_text(json.dumps({'medium': ['review-a', 'review-b']}))
            env = dict(os.environ, FORGE_REGISTRY=str(registry))
            r = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-route.py'), 'qa-fallback', '--plan', str(plan),
                                '--task', 'a', '--exclude', 'codex/model-a'], env=env, capture_output=True, text=True)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, 'review-b'), r.stderr)


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

    def laterals(self, p):
        """The lateral.tsv rows as role, from, to, cause."""
        f = p / 'tasks/a/lateral.tsv'
        return [line.split('\t')[2:] for line in f.read_text().splitlines()] if f.exists() else []

    def two_tasks(self, qa_b='opus', *extra):
        """a on sol and b on astra (both codex), one at a time, so b is still pending when a stops."""
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text(f'a\t-\tmedium\ta.txt\tsol\topus\tA\nb\t-\tmedium\tb.txt\tastra\t{qa_b}\tB\n')
        for name in 'ab':
            (p / 'tasks' / name).mkdir(parents=True)
            (p / 'tasks' / name / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + name)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--explore', '0',
                            '--dwarf-medium', 'sol,astra,sonnet', *extra)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.scenario({'a.dwarf.1': CODEX_BUDGET})
        return p

    def test_a_pending_task_on_a_paused_harness_moves_sideways_before_its_dispatch(self):
        # The final review of run 1: a's budget stop paused codex and a moved to sonnet, but b, pending on codex's
        # astra, was never dispatched and the run exited 8 with nothing saying why.
        p = self.two_tasks()
        r = self.run_plan(p, '--max-parallel', '1', '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        rows = {line.split('\t')[0]: line.split('\t')[4] for line in (p / 'tasks.tsv').read_text().splitlines()
                if not line.startswith('#')}
        self.assertEqual(rows, {'a': 'sonnet', 'b': 'sonnet'})
        moved = (p / 'tasks/b/lateral.tsv').read_text().split('\t')[2:]
        self.assertEqual([c.strip() for c in moved], ['dwarf', 'astra', 'sonnet', 'budget'])
        self.assertIn('MERGED', (p / 'results.tsv').read_text().split('b\t')[1].split('\n')[0])

    def test_a_held_task_moves_its_implementer_then_its_reviewer(self):
        # Round 2 of the final review: one try per set of pauses moved b's implementer, and its reviewer (also on
        # codex) never moved although the reviewer pool had a peer, so b stayed held and the run exited 8.
        p = self.two_tasks('terra', '--qa-medium', 'terra,opus')
        r = self.run_plan(p, '--max-parallel', '1', '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        row = [line.split('\t') for line in (p / 'tasks.tsv').read_text().splitlines() if line.startswith('b\t')][0]
        self.assertEqual((row[4], row[5]), ('sonnet', 'opus'))
        roles = [line.split('\t')[2] for line in (p / 'tasks/b/lateral.tsv').read_text().splitlines()]
        self.assertEqual(roles, ['dwarf', 'qa'])

    def test_a_task_held_without_a_peer_is_named_in_the_summary_with_who_must_act(self):
        # b's reviewer is on codex too, and the plan has no reviewer pools: b stays held, and the summary says so.
        p = self.two_tasks(qa_b='terra')
        r = self.run_plan(p, '--max-parallel', '1', '--infra-retries', '1')
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertIn('held back by an infrastructure pause', r.stdout)
        self.assertRegex(r.stdout, r'\n  b\s+held back: budget on codex — needs you: add credits')
        self.assertIn("fix what is marked 'needs you'", r.stdout)
        self.assertFalse((p / 'infra.paused').exists())
        self.assertEqual(json.loads((p / 'infra.last').read_text())['scopes'][0]['held'], ['b'])
        # A retry is a new invocation: the pauses that run ended with are not its own (round 2 of the review).
        r = self.run_script('forge-parallel.sh', 'retry', p, 'b')
        self.assertFalse((p / 'infra.last').exists(), r.stdout)
        self.assertNotIn('held back', r.stdout)

    def test_a_capacity_stop_moves_to_the_next_model_of_the_pool_at_once(self):
        p = self.plan()
        self.scenario({'a.dwarf.1': CAPACITY})
        # A long backoff: waiting for sol's pause would show as a 30 s run.
        r = self.run_plan(p, '--infra-fallback', '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertEqual(self.dwarf_column(p), 'astra')
        self.assertEqual(self.escalations(p), [])
        self.assertEqual(self.laterals(p), [['dwarf', 'sol', 'astra', 'capacity']])
        self.assertIn('sol is at capacity; moving sideways to astra (same tier)', r.stdout)
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
        self.assertEqual(self.escalations(p), [])
        self.assertEqual(self.laterals(p), [['dwarf', 'sol', 'astra', 'capacity'], ['dwarf', 'astra', 'sol', 'capacity'],
                                            ['dwarf', 'sol', 'astra', 'capacity']])
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

    # --- the QA stage: a review stopped on infrastructure moves to a peer reviewer ----------------------
    def qa_routing(self, p):
        return json.loads((p / 'qa-routing.json').read_text())

    def qa_column(self, p):
        return [line.split('\t')[5] for line in (p / 'tasks.tsv').read_text().splitlines() if line.startswith('a\t')][0]

    def test_plan_keeps_the_reviewer_pools(self):
        p = self.plan('sol', 'opus', '--qa-low', 'a', '--qa-medium', 'b,c', '--qa-high', 'd')
        self.assertEqual(self.qa_routing(p), {'low': ['a'], 'medium': ['b', 'c'], 'high': ['d']})
        (self.root / 'plan').rename(self.root / 'plan.1')
        p = self.plan('sol', 'opus', '--qa', 'x')
        self.assertEqual(self.qa_routing(p), {'any': ['x']})
        self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--explore', '0',
                        '--dwarf-medium', 'sol', '--qa-high', 'y')       # a re-plan updates it, as the dwarf pools are
        self.assertEqual(self.qa_routing(p), {'any': ['x'], 'high': ['y']})

    def test_a_qa_stage_budget_stop_moves_to_a_peer_reviewer_and_keeps_the_attempt(self):
        p = self.plan('sol', 'opus', '--qa-medium', 'opus,astra')
        self.scenario({'a.qa.1': BUDGET})
        r = self.run_plan(p, '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertEqual((self.qa_column(p), self.dwarf_column(p), self.escalations(p), self.laterals(p)),
                         ('astra', 'sol', [], [['qa', 'opus', 'astra', 'budget']]))
        self.assertEqual((p / 'tasks/a/lateral.tsv').read_text().split('\t')[1], '1')       # the attempt it stopped on
        self.assertEqual((p / 'tasks/a/attempt').read_text().strip(), '1')
        self.assertEqual((self.calls().count('dwarf'), self.calls().count('qa')), (1, 2))
        self.assertIn('reviewer opus is out of budget; moving sideways to reviewer astra (same tier)', r.stdout)

    def test_a_qa_stage_move_never_picks_the_implementers_model(self):
        p = self.plan('sol', 'opus', '--qa-medium', 'opus,sol')        # the only other reviewer wrote the work
        self.scenario({'a.qa.1': BUDGET})
        r = self.run_plan(p, '--infra-retries', '1')
        self.assertEqual((self.qa_column(p), self.laterals(p)), ('opus', []))
        self.assertEqual(self.calls().count('dwarf'), 1)
        self.assertIn('nothing will be retried until a person acts', r.stdout)       # a budget with no peer asks

    def test_a_qa_stage_capacity_stop_without_a_peer_waits_for_its_reviewer(self):
        p = self.plan(qa='astra')
        self.scenario({'a.qa.1': CAPACITY})
        r = self.run_plan(p, '--infra-fallback', '--infra-retries', '1')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.dwarf_column(p), self.qa_column(p), self.escalations(p), self.laterals(p)),
                         ('sol', 'astra', [], []))
        self.assertIn('is over; resuming a', r.stdout)

    def test_a_plan_without_reviewer_pools_has_no_qa_move(self):
        p = self.plan('sol', 'opus', '--qa-medium', 'opus,astra')
        (p / 'qa-routing.json').unlink()                # a plan made before the pools were kept
        self.scenario({'a.qa.1': BUDGET})
        r = self.run_plan(p, '--infra-retries', '1')
        self.assertEqual((self.qa_column(p), self.laterals(p)), ('opus', []))
        self.assertIn('nothing will be retried until a person acts', r.stdout)

    def test_a_reviewer_no_pool_lists_has_no_peer(self):
        p = self.plan('sol', 'opus', '--qa-medium', 'sonnet,astra')
        self.scenario({'a.qa.1': BUDGET})
        self.run_plan(p, '--infra-retries', '1')
        self.assertEqual((self.qa_column(p), self.laterals(p)), ('opus', []))

    def test_the_any_pool_is_the_second_choice(self):
        p = self.plan('sol', 'opus', '--qa', 'opus,astra', '--qa-high', 'sonnet')    # a medium task: no medium pool
        self.scenario({'a.qa.1': BUDGET})
        self.run_plan(p, '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual((self.qa_column(p), self.laterals(p)), ('astra', [['qa', 'opus', 'astra', 'budget']]))

    def test_a_moved_review_starts_a_fresh_session(self):
        fail = {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'}
        p = self.plan('sol', 'opus', '--qa-medium', 'opus,astra', '--reviewer-session')
        self.scenario({'a.qa.1': fail, 'a.dwarf.2': {'content': 'attempt 2'}, 'a.qa.2': BUDGET})
        r = self.run_plan(p, '--retry-failed', '1', '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.laterals(p), [['qa', 'opus', 'astra', 'budget']])
        sessions = [line.split('\t') for line in (p / 'tasks/a/review_sessions.tsv').read_text().splitlines()]
        # round 2 continued opus's session until opus stopped; the review that moved to astra starts fresh
        self.assertEqual([row[2:4] for row in sessions],
                         [['fresh', 'first-round'], ['continued', 'same-spec'], ['fresh', 'spec-changed']])
        self.assertNotIn('resume', (p / 'tasks/a/qa.cmd').read_text())

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
        # The final review of run 1: a re-plan turned lateral moves back on; it keeps them off, as it
        # keeps the routing files.
        self.assertEqual((p / 'infra_fallback').read_text(), 'off\n')
        self.run_plan(p, '--infra-fallback')
        self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory',
                                          '--explore', '0', '--dwarf-medium', 'sol,astra')
        self.assertEqual((p / 'infra_fallback').read_text(), 'on\n')

    def test_a_new_plan_moves_a_capacity_stop_without_any_flag(self):
        p = self.plan()
        self.scenario({'a.dwarf.1': CAPACITY})
        r = self.run_plan(p, '--infra-retries', '1', '--infra-backoff', '30')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((self.dwarf_column(p), self.escalations(p), self.laterals(p)),
                         ('astra', [], [['dwarf', 'sol', 'astra', 'capacity']]))

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
        self.assertEqual((self.dwarf_column(p), self.escalations(p), self.laterals(p)),
                         ('sonnet', [], [['dwarf', 'sol', 'sonnet', 'usage_limit']]))
        self.assertIn('sol stopped (usage_limit); moving sideways to sonnet (same tier)', r.stdout)
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
        self.assertNotIn('moving sideways', r.stdout)


if __name__ == '__main__':
    unittest.main()
