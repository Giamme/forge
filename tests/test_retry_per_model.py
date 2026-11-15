"""`plan --retry-per-model N` in forge-route.py (#141): N failures on each rung of the retry policy.

The policy core only: the setting, the escalation trigger, the rung ladder, `per-model` (may the task
retry) and `worst-case` (the most dispatches the unmerged tasks can spend). Plan dirs are written by hand,
as the scheduler leaves them: tasks.tsv, fractal-routing.json pools, tasks/<id>/attempts.tsv.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / 'scripts' / 'forge-route.py'
spec = importlib.util.spec_from_file_location('forge_route', ROUTE)
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)

# luna < sonnet < opus, one model per tier. opus accepts every effort up to max on claude.
POOLS = {'low': ['luna'], 'medium': ['sonnet'], 'high': ['opus']}


def clean_env():
    env = dict(os.environ)
    env.pop('FORGE_REGISTRY', None)      # the assertions are about this repo's registry.tsv
    return env


class PlanCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan = Path(os.path.realpath(tmp.name)) / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'fractal-routing.json').write_text(json.dumps(POOLS))
        saved = os.environ.pop('FORGE_REGISTRY', None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, 'FORGE_REGISTRY', saved)

    def tasks(self, *rows):
        """rows: (id, dwarf). tasks.tsv columns: id, deps, difficulty, files, dwarf, qa, title."""
        (self.plan / 'tasks.tsv').write_text(
            ''.join('%s\t-\thigh\t%s.txt\t%s\topus\tT\n' % (i, i, dwarf) for i, dwarf in rows))
        for i, _dwarf in rows:
            (self.plan / 'tasks' / i).mkdir(exist_ok=True)

    def attempts(self, task, *rows):
        """rows: (attempt, status, spec)."""
        (self.plan / 'tasks' / task / 'attempts.tsv').write_text(
            ''.join('17000000%02d\t%d\t%s\t\t%s\n' % (n, n, status, spec) for n, status, spec in rows))

    def setting(self, name, text):
        (self.plan / name).write_text(text)

    def route(self, *args):
        return subprocess.run([sys.executable, str(ROUTE), *args], capture_output=True, text=True,
                              timeout=60, env=clean_env())


class SettingTests(PlanCase):
    def test_a_number_from_1_to_99_is_the_setting(self):
        for text, want in (('3\n', 3), ('1', 1), ('99\n', 99), ('0\n', None), ('100\n', None),
                           ('x\n', None), ('', None), ('-2\n', None), ('2.5\n', None)):
            with self.subTest(text=text):
                self.setting('retry_per_model', text)
                self.assertEqual(route.per_model_setting(self.plan), want)

    def test_a_missing_file_is_no_setting(self):
        self.assertIsNone(route.per_model_setting(self.plan))


class EscalationTriggerTests(PlanCase):
    def check(self):
        return self.route('escalation', '--plan', str(self.plan), '--task', 'a', '--check')

    def test_n_failures_escalate_before_escalate_after(self):
        self.tasks(('a', 'luna'))
        self.setting('escalate_after', '3\n')
        self.setting('retry_per_model', '2\n')
        self.attempts('a', (1, 'FAIL', 'luna'), (2, 'FAIL', 'luna'))
        done = self.check()
        self.assertEqual((done.returncode, done.stdout), (0, 'sonnet\n'), done.stderr)

    def test_retry_per_model_escalates_even_with_escalate_after_off(self):
        # It replaces escalate_after for its plan: a 0 there must not throw the move away.
        self.tasks(('a', 'luna'))
        self.setting('escalate_after', '0\n')
        self.setting('retry_per_model', '2\n')
        self.attempts('a', (1, 'FAIL', 'luna'), (2, 'FAIL', 'luna'))
        done = self.check()
        self.assertEqual((done.returncode, done.stdout), (0, 'sonnet\n'), done.stderr)
        self.assertTrue((self.plan / 'tasks' / 'a' / 'escalation.next').exists())

    def test_one_failure_below_n_does_not_escalate(self):
        self.tasks(('a', 'luna'))
        self.setting('retry_per_model', '2\n')
        self.attempts('a', (1, 'FAIL', 'luna'))
        done = self.check()
        self.assertEqual((done.returncode, done.stdout), (0, ''), done.stderr)

    def test_without_the_file_escalate_after_decides_as_before(self):
        self.tasks(('a', 'luna'))
        self.setting('escalate_after', '3\n')
        self.attempts('a', (1, 'FAIL', 'luna'), (2, 'FAIL', 'luna'))
        done = self.check()
        self.assertEqual((done.returncode, done.stdout), (0, ''), done.stderr)

    def test_the_count_starts_again_on_the_new_rung(self):
        self.tasks(('a', 'sonnet'))             # the scheduler moved the dwarf after the second failure
        self.setting('retry_per_model', '2\n')
        self.attempts('a', (1, 'FAIL', 'luna'), (2, 'FAIL', 'luna'), (3, 'FAIL', 'sonnet'))
        done = self.check()
        self.assertEqual((done.returncode, done.stdout), (0, ''), done.stderr)
        self.attempts('a', (1, 'FAIL', 'luna'), (2, 'FAIL', 'luna'), (3, 'FAIL', 'sonnet'), (4, 'FAIL', 'sonnet'))
        done = self.check()
        self.assertEqual((done.returncode, done.stdout), (0, 'opus\n'), done.stderr)


class LadderTests(PlanCase):
    def test_the_tiers_above_in_order(self):
        self.assertEqual(route.rungs_above('luna', self.plan), ['sonnet', 'opus'])
        self.assertEqual(route.rungs_above('sonnet', self.plan), ['opus'])
        self.assertEqual(route.rungs_above('opus', self.plan), [])

    def test_the_effort_step_adds_the_top_models_higher_efforts(self):
        self.setting('escalate_effort', 'on\n')
        self.assertEqual(route.rungs_above('luna', self.plan),
                         ['sonnet', 'opus', 'opus:high', 'opus:xhigh', 'opus:max'])
        self.assertEqual(route.rungs_above('opus:xhigh', self.plan), ['opus:max'])
        self.assertEqual(route.rungs_above('opus:max', self.plan), [])

    def test_a_spec_in_no_pool_has_no_rungs(self):
        self.setting('escalate_effort', 'on\n')
        self.assertEqual(route.rungs_above('gemini', self.plan), [])
        self.assertEqual(route.rungs_above('script', self.plan), [])
        self.assertEqual(route.rungs_above('luna', self.plan.parent / 'nowhere'), [])

    def test_a_ladder_is_capped(self):
        # A registry row with a long ladder of efforts still ends at MAX_RUNGS.
        pools = {'low': ['luna'], 'high': ['opus']}
        (self.plan / 'fractal-routing.json').write_text(json.dumps(pools))
        self.setting('escalate_effort', 'on\n')
        self.assertLessEqual(len(route.rungs_above('luna', self.plan)), route.MAX_RUNGS)


class PerModelTests(PlanCase):
    def decision(self, spec='luna', n=2):
        self.tasks(('a', spec))
        self.setting('retry_per_model', '%d\n' % n)

    def test_n_failures_on_a_rung_with_higher_tiers_left_may_retry(self):
        self.decision('luna')
        self.attempts('a', (1, 'FAIL', 'luna'), (2, 'FAIL', 'luna'))
        self.assertEqual(route.per_model(self.plan, 'a'), {
            'retry': True, 'rung': 'luna', 'failed_on_rung': 2, 'rungs_left': 2, 'per_model': 2})

    def test_n_failures_on_the_top_rung_stop(self):
        self.decision('opus')
        self.attempts('a', (1, 'FAIL', 'opus'), (2, 'FAIL', 'opus'))
        got = route.per_model(self.plan, 'a')
        self.assertEqual((got['retry'], got['failed_on_rung'], got['rungs_left']), (False, 2, 0))

    def test_effort_steps_leave_rungs_at_the_top_tier(self):
        self.decision('opus')
        self.setting('escalate_effort', 'on\n')
        self.attempts('a', (1, 'FAIL', 'opus'), (2, 'FAIL', 'opus'))
        got = route.per_model(self.plan, 'a')
        self.assertEqual((got['retry'], got['rungs_left']), (True, 3))

    def test_below_n_may_retry_on_the_top_rung(self):
        self.decision('opus', n=3)
        self.attempts('a', (1, 'FAIL', 'opus'), (2, 'FAIL', 'opus'))
        got = route.per_model(self.plan, 'a')
        self.assertEqual((got['retry'], got['failed_on_rung']), (True, 2))

    def test_infra_and_stale_rows_between_failures_do_not_count(self):
        self.decision('opus', n=3)
        self.attempts('a', (1, 'FAIL', 'opus'), (2, 'INFRA', 'opus'), (3, 'STALE', 'opus'),
                      (4, 'FAIL', 'opus'))
        got = route.per_model(self.plan, 'a')
        self.assertEqual((got['failed_on_rung'], got['retry']), (2, True))

    def test_attempts_before_a_decision_are_not_counted(self):
        self.decision('opus')
        self.attempts('a', (1, 'FAIL', 'opus'), (2, 'FAIL', 'opus'))
        (self.plan / 'tasks' / 'a' / 'decisions.tsv').write_text('1700000099\t2\tretry=1 dwarf=opus\tagain\n')
        got = route.per_model(self.plan, 'a')
        self.assertEqual((got['failed_on_rung'], got['retry']), (0, True))
        self.attempts('a', (1, 'FAIL', 'opus'), (2, 'FAIL', 'opus'), (3, 'FAIL', 'opus'), (4, 'FAIL', 'opus'))
        got = route.per_model(self.plan, 'a')
        self.assertEqual((got['failed_on_rung'], got['retry']), (2, False))

    def test_a_task_off_the_ladder_has_no_rungs(self):
        for dwarf in ('-', 'UNASSIGNED', 'script'):
            with self.subTest(dwarf=dwarf):
                self.decision(dwarf)
                got = route.per_model(self.plan, 'a')
                self.assertEqual((got['retry'], got['rungs_left'], got['failed_on_rung']), (True, 0, 0))

    def test_an_unknown_task_may_not_retry(self):
        self.decision('luna')
        got = route.per_model(self.plan, 'nope')
        self.assertEqual((got['retry'], got['per_model']), (False, 2))


class WorstCaseTests(PlanCase):
    def test_two_tasks_with_shadow(self):
        self.tasks(('a', 'luna'), ('b', 'opus'), ('c', 'luna'))
        (self.plan / 'tasks' / 'c' / 'merged').write_text('x\n')
        self.setting('retry_per_model', '3\n')
        self.setting('shadow_qa', 'sol\n')
        self.setting('shadow_rate', '0.5\n')
        self.setting('max_attempts', '0\n')        # the default cap of 8 would list task a
        got = route.worst_case(self.plan)
        self.assertEqual(got, {
            'per_model': 3,
            'tasks': [{'id': 'a', 'rungs': 3, 'dwarf': 9, 'qa': 9, 'shadow': 5},
                      {'id': 'b', 'rungs': 1, 'dwarf': 3, 'qa': 3, 'shadow': 2}],
            'total': 31, 'capped': []})

    def test_no_shadow_file_means_no_shadow_reviews(self):
        self.tasks(('a', 'luna'))
        self.setting('retry_per_model', '2\n')
        got = route.worst_case(self.plan)
        self.assertEqual((got['tasks'][0]['shadow'], got['total']), (0, 12))

    def test_the_default_shadow_rate_is_a_tenth(self):
        self.tasks(('a', 'opus'))
        self.setting('retry_per_model', '5\n')
        self.setting('shadow_qa', 'sol\n')
        self.assertEqual(route.worst_case(self.plan)['tasks'][0]['shadow'], 1)    # ceil(5 * 0.1)

    def test_a_shadow_rate_past_one_counts_one_shadow_per_review(self):
        self.tasks(('a', 'opus'))
        self.setting('retry_per_model', '3\n')
        self.setting('shadow_qa', 'sol\n')
        self.setting('shadow_rate', '5\n')
        self.assertEqual(route.worst_case(self.plan)['tasks'][0]['shadow'], 3)    # not ceil(3 * 5)

    def test_a_task_past_max_attempts_is_capped(self):
        self.tasks(('a', 'luna'), ('b', 'opus'))
        self.setting('retry_per_model', '3\n')
        self.setting('max_attempts', '4\n')
        self.assertEqual(route.worst_case(self.plan)['capped'], ['a'])
        self.setting('max_attempts', '0\n')
        self.assertEqual(route.worst_case(self.plan)['capped'], [])
        self.setting('max_attempts', '9\n')
        self.assertEqual(route.worst_case(self.plan)['capped'], [])

    def test_effort_steps_add_rungs(self):
        self.tasks(('a', 'luna'))
        self.setting('retry_per_model', '1\n')
        self.setting('escalate_effort', 'on\n')
        self.assertEqual(route.worst_case(self.plan)['tasks'][0]['rungs'], 6)

    def test_a_script_task_dispatches_no_implementer(self):
        self.tasks(('a', 'script'))
        self.setting('retry_per_model', '2\n')
        got = route.worst_case(self.plan)
        self.assertEqual(got['tasks'], [{'id': 'a', 'rungs': 1, 'dwarf': 0, 'qa': 2, 'shadow': 0}])

    def test_a_plan_without_the_setting_still_shows_a_ladder(self):
        self.tasks(('a', 'luna'))
        got = route.worst_case(self.plan)
        self.assertEqual((got['per_model'], got['tasks'][0]['rungs'], got['tasks'][0]['dwarf']), (None, 3, 3))


class CliTests(PlanCase):
    def test_per_model_exits_2_without_the_file(self):
        self.tasks(('a', 'luna'))
        done = self.route('per-model', '--plan', str(self.plan), '--task', 'a')
        self.assertEqual(done.returncode, 2, done.stderr)

    def test_per_model_prints_the_json(self):
        self.tasks(('a', 'luna'))
        self.setting('retry_per_model', '2\n')
        self.attempts('a', (1, 'FAIL', 'luna'))
        done = self.route('per-model', '--plan', str(self.plan), '--task', 'a')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout), {
            'retry': True, 'rung': 'luna', 'failed_on_rung': 1, 'rungs_left': 2, 'per_model': 2})

    def test_an_unknown_task_is_retry_false(self):
        self.tasks(('a', 'luna'))
        self.setting('retry_per_model', '2\n')
        done = self.route('per-model', '--plan', str(self.plan), '--task', 'zzz')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIs(json.loads(done.stdout)['retry'], False)

    def test_worst_case_prints_the_json(self):
        self.tasks(('a', 'luna'))
        self.setting('retry_per_model', '2\n')
        done = self.route('worst-case', '--plan', str(self.plan))
        self.assertEqual(done.returncode, 0, done.stderr)
        got = json.loads(done.stdout)
        self.assertEqual(sorted(got), ['capped', 'per_model', 'tasks', 'total'])
        self.assertEqual(sorted(got['tasks'][0]), ['dwarf', 'id', 'qa', 'rungs', 'shadow'])

    def test_worst_case_without_the_file_exits_0(self):
        self.tasks(('a', 'luna'))
        done = self.route('worst-case', '--plan', str(self.plan))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(json.loads(done.stdout)['per_model'])


if __name__ == '__main__':
    unittest.main()
