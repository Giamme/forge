"""forge-parallel.sh auto: from a goal to a finished run in one blocking command (#87, #88).

Drives the real script with test_forge's fake CLIs. The fake planner is the fake `codex` printing a
canned reply as its final message: the planner is dispatched read-only, so the fake files it with
the reviewers ('qa' in the calls log) under the task name `change`, and the scenario key
`change.qa.<n>` is the planner's n-th reply. Dwarves and reviewers of the tasks have their own
names (TASK_a in a prompt makes the fake work on a.txt), so they never share that counter.
The send-back tests answer the decision-model gates from fixtures, as test_plan_gates_flow does.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

import test_forge as fixtures
import test_plan_gates_flow as gates_flow

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-parallel.sh'
sys.path.insert(0, str(ROOT / 'scripts'))

from forge_dataset import harvest  # noqa: E402

SMALL_GOAL = 'Add a limits section to the config.\n\n- It has a max_items key.\n- Defaults apply.\n'
LARGE_GOAL = SMALL_GOAL + 'Background paragraph. ' * 100 + '\n'
USAGE_LIMIT = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}
CRITERIA = '\n\n## Acceptance criteria\n\n- the file exists'


def task(tid, deps=(), difficulty='low', files=None, prompt=None):
    return {'id': tid, 'deps': list(deps), 'difficulty': difficulty, 'files': files or ['%s.txt' % tid],
            'title': 'Add %s' % tid, 'approach': '', 'prompt': prompt or 'Implement change. TASK_%s%s' % (tid, CRITERIA)}


def reply(*tasks):
    return 'Here is the plan.\n```forge-plan\n%s\n```\n' % json.dumps({'tasks': list(tasks)})


def data_rows(path):
    return [line.split('\t') for line in Path(path).read_text().splitlines() if line.strip() and not line.startswith('#')]


class AutoCase(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def goal(self, text=SMALL_GOAL):
        path = self.root / 'goal.md'
        path.write_text(text)
        return path

    def planner_says(self, *replies):
        # One reply answers every planner call of the test; several answer the first call, the second ...
        keys = ['change.qa.*'] if len(replies) == 1 else ['change.qa.%d' % n for n in range(1, len(replies) + 1)]
        self.scenario({key: {'findings': text, 'verdict': 'none'} for key, text in zip(keys, replies)})

    def auto(self, name='plan', *args, goal=None, planner='sol', plan_flags=True):
        plan = self.root / name
        flags = ['--dwarf', 'sol', '--qa', 'opus', '--explore', '0', '--no-memory'] if plan_flags else []
        planner_flags = ['--planner', planner] if planner else []
        result = self.run_script('forge-parallel.sh', 'auto', plan, '--repo', self.repo,
                                 '--goal-file', goal or self.goal(), *planner_flags, *flags, *args)
        return result, plan

    def planned(self, name, *tasks, args=()):
        self.planner_says(reply(*tasks))
        result, plan = self.auto(name, '--plan', 'always', *args)
        self.assertEqual(result.returncode, 0, result.stdout)
        return result, plan

    def printed_command(self, output):
        lines = output.splitlines()
        return shlex.split(lines[next(i for i, line in enumerate(lines) if 'To start it:' in line) + 1])


class ValidReplyTests(AutoCase):
    def test_one_task_reply_makes_the_plan_and_stops_at_approval(self):
        result, plan = self.planned('one', task('only'))
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['only'])
        self.assertEqual((plan / 'request.md').read_text(), SMALL_GOAL)
        self.assertEqual((plan / 'orchestrator').read_text(), 'auto\n')
        self.assertEqual((plan / 'planner').read_text(), 'sol\n')
        self.assertIn('## The whole request', (plan / 'tasks' / 'only' / 'prompt.md').read_text())
        self.assertTrue((plan / 'waves.tsv').exists())
        # The planner was the only model call: no dwarf, and no review of anything.
        self.assertEqual(self.calls(), ['qa'])
        self.assertEqual(self.printed_command(result.stdout), [str(SCRIPT), 'run', str(plan)])

    def test_two_branches_are_kept_and_the_printed_command_carries_the_run_flags(self):
        self.planner_says(reply(task('a'), task('b')))
        result, plan = self.auto('branches', '--plan', 'always', '--', '--max-parallel', '2', '--verify', 'echo ok now')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['a', 'b'])
        self.assertFalse((plan / 'collapsed.json').exists())
        self.assertEqual(self.calls(), ['qa'])
        self.assertEqual(self.printed_command(result.stdout),
                         [str(SCRIPT), 'run', str(plan), '--max-parallel', '2', '--verify', 'echo ok now'])

    def test_a_chain_ends_as_one_combined_task_unless_no_collapse(self):
        chain = (task('a'), task('b', deps=['a']), task('c', deps=['b']))
        _, collapsed = self.planned('collapsed', *chain)
        self.assertEqual([row[0] for row in data_rows(collapsed / 'tasks.tsv')], ['combined'])
        self.assertEqual([row[0] for row in data_rows(collapsed / 'tasks.precollapse.tsv')], ['a', 'b', 'c'])
        self.assertTrue((collapsed / 'collapsed.json').exists())
        _, kept = self.planned('kept', *chain, args=('--no-collapse',))
        self.assertEqual([row[0] for row in data_rows(kept / 'tasks.tsv')], ['a', 'b', 'c'])
        self.assertFalse((kept / 'collapsed.json').exists())

    def test_approved_runs_the_plan_and_the_run_decides_the_exit_code(self):
        self.planner_says(reply(task('a'), task('b')))
        result, plan = self.auto('approved', '--plan', 'always', '--approved', '--', '--max-parallel', '2')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sorted(self.calls()), ['dwarf', 'dwarf', 'qa', 'qa', 'qa'])
        self.assertNotIn('To start it:', result.stdout)
        self.assertIn('MERGED', result.stdout)

    def test_a_failed_review_makes_auto_exit_with_the_runs_code(self):
        self.env['FAIL_A'] = '1'
        self.planner_says(reply(task('a'), task('b')))
        result, plan = self.auto('failing', '--plan', 'always', '--approved')
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)

    def test_the_profile_names_the_planner_and_its_fast_tier(self):
        config = self.root / 'cfg'
        self.env.update(XDG_CONFIG_HOME=str(config), XDG_STATE_HOME=str(self.root / 'state'))
        (config / 'forge').mkdir(parents=True)
        profile = {'routing': {'planner': 'sol:medium', 'dwarf': {'any': ['sol']}, 'qa': {'any': ['opus']}},
                   'policy': {'explore': 0, 'fast': ['planner']}}
        (config / 'forge' / 'decide.json').write_text(json.dumps({'version': 1, 'profiles': {'mine': profile}}))
        self.planner_says(reply(task('a')))
        result, plan = self.auto('profiled', '--plan', 'always', '--profile', 'mine', planner=None, plan_flags=False)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((plan / 'planner').read_text(), 'sol:medium\n')
        self.assertIn('service_tier=fast', (plan / 'planner.cmd').read_text())
        self.assertEqual((plan / 'fast').read_text(), 'planner\n')
        self.assertEqual(self.calls(), ['qa'])

    def test_an_existing_frozen_profile_names_the_planner_not_the_current_default(self):
        config = self.root / 'cfg'
        self.env.update(XDG_CONFIG_HOME=str(config), XDG_STATE_HOME=str(self.root / 'state'))
        (config / 'forge').mkdir(parents=True)
        body = {'routing': {'dwarf': {'any': ['sol']}, 'qa': {'any': ['opus']}}, 'policy': {'explore': 0}}
        decide = {'version': 1, 'default_profile': 'now',
                  'profiles': {'now': dict(body, routing=dict(body['routing'], planner='opus'))}}
        (config / 'forge' / 'decide.json').write_text(json.dumps(decide))
        plan = self.root / 'frozen'
        plan.mkdir()
        frozen = {'name': 'then', 'level': 'user', 'shadowed': [], 'overridden': [],
                  'body': dict(body, routing=dict(body['routing'], planner='sol:medium'))}
        (plan / 'profile.json').write_text(json.dumps(frozen))
        self.planner_says(reply(task('a')))
        result, plan = self.auto('frozen', '--plan', 'always', planner=None)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('gpt-6.1-sol', (plan / 'planner.cmd').read_text())
        self.assertNotIn('opus', (plan / 'planner.cmd').read_text())
        self.assertEqual(json.loads((plan / 'profile.json').read_text())['name'], 'then')
        # plan records the planner of a kept frozen profile too, so the table and the dataset see it.
        self.assertEqual((plan / 'planner').read_text(), 'sol:medium\n')


class RefusalTests(AutoCase):
    def test_a_malformed_reply_stops_with_exit_4_before_anything_else_is_dispatched(self):
        self.planner_says('I would split this in two, but I forgot the block.')
        result, plan = self.auto('malformed', '--plan', 'always', '--approved')
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn('planner reply refused', result.stdout)
        self.assertIn(str(plan / 'planner.last'), result.stdout)
        self.assertTrue((plan / 'planner.refused').exists())
        self.assertFalse((plan / 'tasks.tsv').exists())
        self.assertEqual(self.calls(), ['qa'])

    def test_a_planner_infrastructure_stop_exits_8(self):
        self.scenario({'change.qa.1': USAGE_LIMIT})
        result, plan = self.auto('infra', '--plan', 'always', '--approved')
        self.assertEqual(result.returncode, 8, result.stdout)
        self.assertIn('Nothing was dispatched after the planner', result.stdout)
        self.assertFalse((plan / 'tasks.tsv').exists())
        self.assertEqual(self.calls(), ['qa'])

    def test_another_planner_failure_exits_4_and_names_its_records(self):
        self.scenario({'change.qa.1': {'rc': 1, 'stderr': 'Traceback: something unrelated broke'}})
        result, plan = self.auto('broken', '--plan', 'always')
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertIn(str(plan / 'planner.last'), result.stdout)
        self.assertIn(str(plan / 'planner.log'), result.stdout)

    def test_no_planner_is_a_usage_error_before_any_model_call(self):
        result, plan = self.auto('nobody', '--plan', 'always', planner=None)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('auto needs --planner SPEC, or a profile with routing.planner', result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse((plan / 'tasks.tsv').exists())

    def test_an_explicitly_empty_planner_or_profile_is_a_usage_error(self):
        for flag in ('--planner', '--profile'):
            with self.subTest(flag=flag):
                result, plan = self.auto('empty' + flag[2:], '--plan', 'never', flag, '', planner=None)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('%s needs' % flag, result.stdout)
                self.assertEqual(self.calls(), [])
                self.assertFalse((plan / 'tasks.tsv').exists())

    def test_an_unusable_planner_spec_stops_in_the_preflight(self):
        result, _ = self.auto('nospec', '--plan', 'always', planner='no-such-model')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_missing_value_or_plan_dir_is_a_usage_error(self):
        goal = self.goal()
        for args in (['--repo'], ['--repo', self.repo, '--goal-file'], ['--repo', self.repo, '--goal-file', goal, '--planner'],
                     ['--repo', self.repo, '--goal-file', goal, '--profile'], ['--repo', self.repo, '--goal-file', goal, '--fast']):
            with self.subTest(args=args[-1]):
                result = self.run_script('forge-parallel.sh', 'auto', self.root / 'usage', *args)
                self.assertEqual(result.returncode, 2, result.stdout)
        result = self.run_script('forge-parallel.sh', 'auto')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_plan_dir_with_tasks_is_refused(self):
        existing = self.root / 'existing'
        (existing / 'tasks').mkdir(parents=True)
        (existing / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        self.planner_says(reply(task('a')))
        result, _ = self.auto('existing', '--plan', 'always')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('auto starts a new plan; use plan and run on an existing one', result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse((existing / 'request.md').exists())


class GateTests(AutoCase):
    def test_a_small_goal_skips_the_planner_with_plan_auto(self):
        result, plan = self.auto('small', planner=None)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), [])
        rows = data_rows(plan / 'tasks.tsv')
        self.assertEqual([(row[0], row[2]) for row in rows], [('task', 'medium')])
        self.assertEqual(json.loads((plan / 'plan-gate.json').read_text())['decision'], 'one-task')
        self.assertEqual((plan / 'request.md').read_text(), SMALL_GOAL)
        self.assertEqual((plan / 'orchestrator').read_text(), 'auto\n')
        self.assertFalse((plan / 'planner').exists())
        self.assertEqual(self.printed_command(result.stdout), [str(SCRIPT), 'run', str(plan)])

    def test_the_one_task_difficulty_is_chosen_by_the_flag(self):
        result, plan = self.auto('hard', '--one-task-difficulty', 'high', planner=None)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([row[2] for row in data_rows(plan / 'tasks.tsv')], ['high'])

    def test_a_large_goal_goes_to_the_planner_with_plan_auto(self):
        self.planner_says(reply(task('a'), task('b')))
        result, plan = self.auto('large', goal=self.goal(LARGE_GOAL))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), ['qa'])
        self.assertEqual(json.loads((plan / 'plan-gate.json').read_text())['decision'], 'plan')
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['a', 'b'])

    def test_plan_always_plans_a_small_goal_and_plan_never_does_not_plan_a_large_one(self):
        self.planner_says(reply(task('a'), task('b')))
        _, always = self.auto('always', '--plan', 'always')
        self.assertEqual(self.calls(), ['qa'])
        self.assertEqual([row[0] for row in data_rows(always / 'tasks.tsv')], ['a', 'b'])
        self.assertFalse((always / 'plan-gate.json').exists())
        result, never = self.auto('never', '--plan', 'never', goal=self.goal(LARGE_GOAL))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), ['qa'])
        self.assertEqual([row[0] for row in data_rows(never / 'tasks.tsv')], ['task'])

    def test_a_goal_file_that_is_the_plans_request_is_left_alone(self):
        plan = self.root / 'inplace'
        plan.mkdir()
        (plan / 'request.md').write_text(SMALL_GOAL)
        result = self.run_script('forge-parallel.sh', 'auto', plan, '--repo', self.repo, '--goal-file', plan / 'request.md',
                                 '--dwarf', 'sol', '--qa', 'opus', '--explore', '0', '--no-memory')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((plan / 'request.md').read_text(), SMALL_GOAL)
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['task'])

    def test_approved_runs_a_one_task_plan(self):
        result, plan = self.auto('small-run', '--approved', planner=None)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(sorted(self.calls()), ['dwarf', 'qa'])


class FastTierTests(AutoCase):
    def test_fast_planner_reaches_the_planner_dispatch(self):
        self.planner_says(reply(task('a')))
        result, plan = self.auto('fast', '--plan', 'always', '--fast', 'planner')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('service_tier=fast', (plan / 'planner.cmd').read_text())
        self.assertEqual((plan / 'fast').read_text(), 'planner\n')

    def test_another_fast_role_does_not_make_the_planner_fast(self):
        self.planner_says(reply(task('a')))
        result, plan = self.auto('slow', '--plan', 'always', '--fast', 'dwarf,qa')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('service_tier', (plan / 'planner.cmd').read_text())

    def test_no_fast_flag_leaves_the_planner_on_the_standard_tier(self):
        self.planner_says(reply(task('a')))
        _, plan = self.auto('standard', '--plan', 'always')
        self.assertNotIn('service_tier', (plan / 'planner.cmd').read_text())
        self.assertFalse((plan / 'fast').exists())


class DatasetTests(AutoCase):
    def test_the_plan_event_records_the_orchestrator_and_the_planner_as_author(self):
        _, plan = self.planned('recorded', task('a'), task('b'))
        event = harvest.plan_event(plan, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual(event['data']['orchestrator'], 'auto')
        self.assertEqual(event['data']['author'], 'planner')
        self.assertEqual(event['data']['planner'], 'sol')
        self.assertTrue(event['data']['planner_dispatches'])

    def test_a_plan_made_by_hand_has_no_orchestrator(self):
        plan = self.root / 'by-hand'
        (plan / 'tasks' / 'a').mkdir(parents=True)
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (plan / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. TASK_a')
        event = harvest.plan_event(plan, dict(run='fixed-run'), src='import', inventory=False)
        self.assertNotIn('orchestrator', event['data'])
        self.assertEqual(event['data']['author'], 'orchestrator')


class SendBackTests(AutoCase):
    """The plan gates send a vague prompt back; auto asks the planner once more."""
    gates = gates_flow.FlowCase.gates
    learn = gates_flow.FlowCase.learn
    answer = staticmethod(gates_flow.FlowCase.answer)

    def setUp(self):
        AutoCase.setUp(self)
        for name in [k for k in self.env if k.startswith(('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE_', 'CLOUDFLARE_'))]:
            self.env.pop(name)
        self.config = self.root / 'config'
        self.fixtures = self.root / 'fixtures'
        self.fixtures.mkdir()
        self.shim = self.root / 'shim'
        self.shim.mkdir()
        (self.shim / 'sitecustomize.py').write_text(gates_flow.SHIM)
        self.env.update(XDG_CONFIG_HOME=str(self.config), XDG_STATE_HOME=str(self.root / 'state'),
                        TYPESAFE_API_KEY='ts-auto-test-key', FORGE_JEV_FIXTURES=str(self.fixtures),
                        FORGE_ROUTE_EXPLORE='0')
        self.gates(prompt='on')

    def plan_cmd(self, plan, *args, env=None):
        return subprocess.run(
            ['bash', str(SCRIPT), 'plan', str(plan), '--repo', str(self.repo), '--no-memory', '--explore', '0',
             '--dwarf', 'sol', '--qa', 'opus', '--planner', 'sol', '--collapse-chains', *args],
            capture_output=True, text=True, env=env or self.env)

    def seeded(self, text):
        """The plan dir auto would hand to `plan` for this reply, to learn the gates' requests from."""
        seed = self.root / ('seed%d' % len(list(self.root.glob('seed*'))))
        seed.mkdir()
        (seed / 'request.md').write_text(SMALL_GOAL)
        (seed / 'planner.last').write_text(text)
        parsed = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge-planner.py'), 'parse', '--plan', str(seed),
                                 '--reply', str(seed / 'planner.last')], capture_output=True, text=True)
        self.assertEqual(parsed.returncode, 0, parsed.stdout + parsed.stderr)
        (seed / 'repo').write_text(str(self.repo) + '\n')
        return seed

    def vague(self, tid):
        return task(tid, prompt='Do the thing for %s.\n\n## Acceptance criteria\n\n- it works' % tid)

    def replies(self, first, second):
        texts = [reply(first), reply(second)]
        for text in texts:
            self.learn(self.seeded(text), self.answer)
        self.planner_says(*texts)

    def test_a_send_back_redispatches_the_planner_once_with_the_sendback_file(self):
        self.replies(self.vague('t1'), task('t1', prompt='Make the thing work for t1, with the details.' + CRITERIA))
        result, plan = self.auto('sentback', '--plan', 'always')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), ['qa', 'qa'])
        self.assertIn("# Sent back by forge's plan gates", result.stdout)
        first, second = (plan / 'planner.first.prompt').read_text(), (plan / 'planner.prompt').read_text()
        self.assertNotIn('Sent back by forge', first)
        self.assertIn("# Sent back by forge's plan gates", second)
        self.assertIn('- t1', second)
        self.assertTrue(second.startswith(first.split('\n')[0]))
        self.assertIn('Make the thing work', (plan / 'tasks' / 't1' / 'prompt.md').read_text())
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['t1'])
        self.assertTrue((plan / 'waves.tsv').exists())
        self.assertIn('Do the thing for t1', (plan / 'planner.first.last').read_text())
        self.assertEqual((plan / 'request.md').read_text(), SMALL_GOAL)

    def test_a_second_send_back_stops_with_exit_4(self):
        self.replies(self.vague('t1'), self.vague('t2'))
        result, plan = self.auto('twice', '--plan', 'always', '--approved')
        self.assertEqual(result.returncode, 4, result.stdout)
        self.assertEqual(self.calls(), ['qa', 'qa'])
        self.assertIn('sent items back a second time', result.stdout)
        self.assertTrue((plan / 'plan.sendback.md').exists())
        self.assertNotIn('dwarf', self.calls())
        # The first plan's files were removed before the second reply was parsed.
        self.assertFalse((plan / 'tasks' / 't1').exists())


if __name__ == '__main__':
    unittest.main()
