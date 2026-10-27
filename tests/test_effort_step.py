"""The optional effort step before the escalation stop: --escalate-effort.

Off unless a plan has escalate_effort containing `on`. On, a task that has used up
escalate_after failures at the top tier is retried at the next effort of the same
alias and harness. The streak counts per effort, so that bump starts again.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import test_forge as fixtures
import test_retry as retry_fixtures

ROOT = Path(__file__).resolve().parents[1]
DISPATCH = ROOT / 'scripts' / 'forge-dispatch.sh'
ROUTE = ROOT / 'scripts' / 'forge-route.py'
CONTROL = ROOT / 'scripts' / 'forge-control.py'

# astra's codex row is <=ultra, and astra sits in the high pool below: the top tier.
TOP = {'low': ['sol'], 'high': ['astra']}
OLD_STOP = (
    'a failed 3 attempts on astra:medium, and it is already in the highest tier of this '
    "plan's pools (high). Another attempt on the same model rarely passes, "
    'so split the task into smaller ones, or retry it with an explicit --dwarf <spec>.\n')
TOP_STOP = (
    'a failed 3 attempts on astra:ultra, and it is already in the highest tier of this '
    "plan's pools (high). The highest effort this pairing accepts was tried as well. "
    'Another attempt on the same model rarely passes, '
    'so split the task into smaller ones, or retry it with an explicit --dwarf <spec>.\n')


def clean_env():
    env = dict(os.environ)
    env.pop('FORGE_REGISTRY', None)   # the assertions are about this repo's registry.tsv
    return env


class EffortsTests(unittest.TestCase):
    """forge-dispatch.sh efforts, against the real registry. It must not dispatch."""

    def efforts(self, spec, cwd=None):
        return subprocess.run(['bash', str(DISPATCH), 'efforts', '--spec', spec],
                              capture_output=True, text=True, timeout=30, env=clean_env(), cwd=cwd)

    def test_a_ceiling_lists_every_effort_up_to_it(self):
        cwd = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cwd, True)
        result = self.efforts('astra', cwd=cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'low\nmedium\nhigh\nxhigh\nmax\nultra\n')
        self.assertEqual(list(cwd.iterdir()), [])                 # the listing writes nothing
        # The effort and harness in the spec pick the row; they do not filter the list.
        self.assertEqual(self.efforts('astra:high:codex').stdout, result.stdout)

    def test_a_list_prints_exactly_those_efforts_weakest_first(self):
        # gemini-pro on antigravity accepts low and high, and not the medium between them.
        result = self.efforts('gemini-pro')
        self.assertEqual((result.returncode, result.stdout), (0, 'low\nhigh\n'), result.stderr)

    def test_none_and_a_missing_ladder_print_nothing(self):
        for spec in ('opus:medium:antigravity', 'kimi'):   # none, and opencode's `-` with no ladder
            with self.subTest(spec=spec):
                result = self.efforts(spec)
                self.assertEqual((result.returncode, result.stdout), (0, ''), result.stderr)

    def test_an_unknown_alias_exits_2(self):
        for spec in ('not-a-model', 'not-a-model:high:codex'):
            with self.subTest(spec=spec):
                result = self.efforts(spec)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')


class RouteTests(unittest.TestCase):
    """forge-route.py escalation on a hand-built plan, in the style of CheckTests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = Path(os.path.realpath(self.tmp.name)) / 'plan'
        self.tdir = self.plan / 'tasks' / 'a'
        self.tdir.mkdir(parents=True)

    def rows(self, spec, n, start=1):
        return ''.join('17000000%02d\t%d\tFAIL\t\t%s\n' % (i, i, spec) for i in range(start, start + n))

    def task(self, dwarf, pools, attempts, effort=False):
        (self.plan / 'tasks.tsv').write_text('a\t-\thigh\ta.txt\t%s\topus\tA\n' % dwarf)
        (self.plan / 'fractal-routing.json').write_text(json.dumps(pools))
        (self.tdir / 'attempts.tsv').write_text(attempts)
        path = self.plan / 'escalate_effort'
        if effort:
            path.write_text('on\n')
        elif path.exists():
            path.unlink()

    def route(self, *extra):
        return subprocess.run([sys.executable, str(ROUTE), 'escalation', '--plan', str(self.plan),
                               '--task', 'a', *extra],
                              capture_output=True, text=True, timeout=30, env=clean_env())

    def test_three_failures_at_the_top_tier_step_to_the_next_effort(self):
        self.task('astra:medium', TOP, self.rows('astra:medium', 3), effort=True)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'astra:high\n'), result.stderr)
        self.assertEqual((self.tdir / 'escalation.next').read_text(), 'astra:high\n')
        self.assertFalse((self.tdir / 'escalation.stop').exists())
        self.assertFalse((self.tdir / 'split.suggested').exists())

    def test_three_more_failures_at_the_new_effort_step_again(self):
        attempts = self.rows('astra:medium', 3) + self.rows('astra:high', 3, start=4)
        self.task('astra:high', TOP, attempts, effort=True)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'astra:xhigh\n'), result.stderr)
        self.assertEqual((self.tdir / 'escalation.next').read_text(), 'astra:xhigh\n')

    def test_the_streak_restarts_at_the_new_effort(self):
        # Two failures at high are not yet escalate_after, even after three at medium.
        attempts = self.rows('astra:medium', 3) + self.rows('astra:high', 2, start=4)
        self.task('astra:high', TOP, attempts, effort=True)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, ''), result.stderr)
        self.assertFalse((self.tdir / 'escalation.next').exists())
        self.assertFalse((self.tdir / 'escalation.stop').exists())

    def test_the_top_effort_stops_and_says_so(self):
        self.task('astra:ultra', TOP, self.rows('astra:ultra', 3), effort=True)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (3, ''), result.stderr)
        self.assertEqual((self.tdir / 'escalation.stop').read_text(), '3\tastra:ultra\n')
        self.assertEqual((self.tdir / 'split.suggested').read_text(), TOP_STOP)
        self.assertFalse((self.tdir / 'escalation.next').exists())

    def test_an_explicit_harness_is_kept(self):
        self.task('astra:medium:codex', TOP, self.rows('astra:medium:codex', 3), effort=True)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'astra:high:codex\n'), result.stderr)

    def test_a_gap_steps_to_the_next_accepted_effort_above_the_current_one(self):
        # gemini-pro accepts low and high. A bare spec runs at medium, which is not in the list.
        self.task('gemini-pro', {'high': ['gemini-pro']}, self.rows('gemini-pro', 3), effort=True)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'gemini-pro:high\n'), result.stderr)

    def test_a_stronger_model_still_wins_over_the_effort_step(self):
        self.task('sol', {'low': ['sol'], 'high': ['astra']}, self.rows('sol', 3), effort=True)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'astra\n'), result.stderr)

    def test_step_off_stops_exactly_as_before(self):
        self.task('astra:medium', TOP, self.rows('astra:medium', 3), effort=False)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (3, ''), result.stderr)
        self.assertEqual((self.tdir / 'escalation.stop').read_text(), '3\tastra:medium\n')
        self.assertEqual((self.tdir / 'split.suggested').read_text(), OLD_STOP)
        self.assertFalse((self.tdir / 'escalation.next').exists())

    def test_step_off_counts_every_effort_of_the_model_as_one_streak(self):
        attempts = self.rows('astra:medium', 2) + self.rows('astra:high', 1, start=3)
        self.task('astra:high', TOP, attempts, effort=False)
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (3, ''), result.stderr)
        self.assertEqual((self.tdir / 'escalation.stop').read_text(), '3\tastra:high\n')
        self.assertNotIn('highest effort', (self.tdir / 'split.suggested').read_text())


class ControlEffortTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan = Path(tmp.name).resolve() / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('a\t-\thigh\tsrc\tsol\topus\tA\n')
        self.env = dict(os.environ, FORGE_DATASET='off')

    def run_control(self, *args, env=None):
        return subprocess.run([sys.executable, str(CONTROL), *map(str, args)],
                              capture_output=True, text=True, env=env or self.env, timeout=60)

    def ok(self, *args, env=None):
        result = self.run_control(*args, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def log(self):
        path = self.plan / 'control.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_set_on_and_off_writes_the_file_shows_it_and_audits_it(self):
        self.assertEqual(json.loads(self.ok('show', self.plan, '--json').stdout)['run']['escalate_effort'], 'off')
        turned_on = self.ok('set', self.plan, 'escalate-effort', 'on')
        self.assertIn('at the next retry decision', turned_on.stdout)
        self.assertEqual((self.plan / 'escalate_effort').read_text(), 'on\n')
        shown = json.loads(self.ok('show', self.plan, '--json').stdout)
        self.assertEqual(shown['run']['escalate_effort'], 'on')
        self.assertIn('nothing changed', self.ok('set', self.plan, 'escalate-effort', 'on').stdout)
        turned_off = self.ok('set', self.plan, 'escalate-effort', 'off')
        self.assertIn('at the next retry decision', turned_off.stdout)
        self.assertFalse((self.plan / 'escalate_effort').exists())
        shown = json.loads(self.ok('show', self.plan, '--json').stdout)
        self.assertEqual(shown['run']['escalate_effort'], 'off')
        self.assertIn('escalate-effort off', self.ok('show', self.plan).stdout)
        self.assertEqual([(e['action'], e['task'], e['key'], e['value'], e['previous']) for e in self.log()],
                         [('set', None, 'escalate-effort', 'on', None),
                          ('set', None, 'escalate-effort', 'off', 'on')])

    def test_a_bad_value_or_a_task_scope_is_refused_and_not_logged(self):
        self.assertEqual(self.run_control('set', self.plan, 'escalate-effort', 'maybe').returncode, 2)
        self.assertEqual(self.run_control('set', self.plan, 'escalate-effort', 'on', '--task', 'a').returncode, 2)
        self.assertFalse((self.plan / 'escalate_effort').exists())
        self.assertEqual(self.log(), [])

    def test_the_change_is_a_dataset_control_event(self):
        # A private store: the suite shares one FORGE_DATA_HOME, and test_control dumps every
        # control event in it and expects only the keys that test itself set.
        env = {k: v for k, v in self.env.items() if k != 'FORGE_DATASET'}
        env['FORGE_DATA_HOME'] = str(self.plan.parent / 'dataset-home')
        self.ok('set', self.plan, 'escalate-effort', 'on', env=env)
        dump = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-dataset.py'), 'dump', '--kind', 'event'],
                              capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(dump.returncode, 0, dump.stderr)
        events = [json.loads(line)['data'] for line in dump.stdout.splitlines()]
        events = [e for e in events if e.get('name') == 'control' and e.get('key') == 'escalate-effort']
        self.assertEqual(len(events), 1, events)
        entry = self.log()[0]
        for field in ('at', 'actor', 'action', 'key', 'value', 'previous'):
            self.assertEqual(events[0][field], '' if entry[field] is None else str(entry[field]), field)


class EffortEndToEndTests(unittest.TestCase):
    """The step through a real run, the way test_retry drives the fake agent."""

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.env.pop('FORGE_REGISTRY', None)

    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    plan_of = retry_fixtures.RetryTests.plan_of
    pooled_plan = retry_fixtures.RetryTests.pooled_plan
    three_failures = retry_fixtures.RetryTests.three_failures
    spent_on = retry_fixtures.RetryTests.spent_on

    def test_a_top_tier_failure_is_retried_at_the_next_effort(self):
        p = self.pooled_plan('--dwarf-low', 'sol')          # sol is the only pool, so the top tier
        self.three_failures()
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 3, '--escalate-effort')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.spent_on(p), [('1', 'FAIL', 'sol'), ('2', 'FAIL', 'sol'), ('3', 'FAIL', 'sol'),
                                            ('4', 'PASS', 'sol:high')])
        self.assertEqual((p / 'tasks/a/escalations.tsv').read_text().split('\t')[1:],
                         ['3', 'sol', 'sol:high', 'policy\n'])
        self.assertIn('effort=high', (p / 'tasks/a/dwarf.resolved').read_text())
        self.assertIn('escalating from sol to sol:high', r.stdout)
        self.assertEqual((p / 'escalate_effort').read_text(), 'on\n')

    def test_run_and_retry_persist_the_switch_and_integrate_rejects_it(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--escalate-effort', '--timeout', '7')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual((p / 'escalate_effort').read_text(), 'on\n')
        self.assertEqual((p / 'timeout').read_text(), '7')             # the switch did not swallow the next flag
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--no-escalate-effort')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((p / 'escalate_effort').exists())
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved', '--escalate-effort')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse((p / 'escalate_effort').exists())


if __name__ == '__main__':
    unittest.main()
