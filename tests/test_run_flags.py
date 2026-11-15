"""`run` records the flags it is using in <plan>/run.flags.json (#146).

Fixtures are borrowed from test_forge.py (the fake agent CLI), not inherited: subclassing
ForgeTests would run all of its tests again under this class.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import re
import time
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
# The shell form --verify "echo 'a\"b'", plus a trailing newline a command substitution would drop.
VERIFY = "echo 'a\"b'\n"
FLAGS = ['--max-parallel', '2', '--retry-failed', '2', '--infra-retries', '4',
         '--sweep', 'P3', '--sweep-max', '2', '--timeout', '600', '--verify', VERIFY]
KEYS = {'retry_failed', 'infra_retries', 'sweep', 'sweep_max', 'sweep_plausible', 'sweep_ahead',
        'max_parallel', 'timeout', 'verify', 'fractal', 'argv', 'at'}


class RunFlagsTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    plan = fixtures.ForgeTests.plan

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        # A developer's default profile would change the retry budget a plain run records.
        self.cfg = self.root / 'cfg'
        for name in [k for k in self.env if k.startswith(('FORGE_DECIDE', 'FORGE_JEV_'))]:
            self.env.pop(name, None)
        self.env['XDG_CONFIG_HOME'] = str(self.cfg)
        self.env['XDG_STATE_HOME'] = str(self.root / 'state')

    def flags(self, plan):
        return json.loads((plan / 'run.flags.json').read_text())

    def started(self, plan, *args):
        result = self.run_script('forge-parallel.sh', 'run', plan, *args)
        self.assertNotIn('could not write', result.stdout)
        self.assertNotIn('Traceback', result.stdout)
        return result

    def assert_number(self, value):
        self.assertIsInstance(value, int)
        self.assertNotIsInstance(value, bool)

    def decide(self, profiles):
        path = self.cfg / 'forge' / 'decide.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(version=1, profiles=profiles)))

    def planned(self, *flags):
        """One low task, planned with `flags`. The profile test needs pools the fixture plan does not set."""
        p = self.root / 'profiled'
        p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\t-\t-\tA\n')
        (p / 'tasks' / 'a').mkdir(parents=True)
        (p / 'tasks' / 'a' / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        result = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(result.returncode, 0, result.stdout)
        return p

    def test_run_records_the_flags_it_is_using(self):
        p = self.plan()
        before = time.time()
        result = self.started(p, *FLAGS)
        self.assertEqual(result.returncode, 0, result.stdout)
        data = self.flags(p)
        self.assertEqual(set(data), KEYS)
        self.assertEqual(data['retry_failed'], 2)
        self.assertEqual(data['infra_retries'], 4)
        self.assertEqual(data['sweep'], 'P3')
        self.assertEqual(data['sweep_max'], 2)
        self.assertIs(data['sweep_plausible'], False)
        self.assertIs(data['sweep_ahead'], False)
        self.assertEqual(data['max_parallel'], 2)
        self.assertEqual(data['timeout'], 600)
        self.assertEqual(data['verify'], VERIFY)
        self.assertIs(data['fractal'], False)
        self.assertEqual(data['argv'], FLAGS)
        for key in ('retry_failed', 'infra_retries', 'sweep_max', 'max_parallel', 'timeout'):
            self.assert_number(data[key])
        self.assertIsInstance(data['at'], float)
        self.assertGreater(data['at'], before)
        self.assertLess(data['at'], time.time() + 5)
        self.assertFalse(list(p.glob('.run.flags.json.*')))
        self.assertEqual((p / 'verify_cmd').read_text(), VERIFY)

    def test_a_plain_run_records_the_scheduler_defaults(self):
        # run passes --retry-failed / --infra-retries only when the value is > 0, and the
        # scheduler's own default for both is 0 (forge-schedule.py). sweep_max stays null
        # unless --sweep-max was given, even though the scheduler's own default is 5.
        p = self.plan()
        result = self.started(p)
        self.assertEqual(result.returncode, 0, result.stdout)
        data = self.flags(p)
        self.assertEqual(data['retry_failed'], 0)
        self.assertEqual(data['infra_retries'], 0)
        self.assertIsNone(data['sweep'])
        self.assertIsNone(data['sweep_max'])
        self.assertIs(data['sweep_plausible'], False)
        self.assertIs(data['sweep_ahead'], False)
        self.assertEqual(data['max_parallel'], 3)
        self.assertIsNone(data['timeout'])
        self.assertIsNone(data['verify'])
        self.assertIs(data['fractal'], False)
        self.assertEqual(data['argv'], [])
        self.assert_number(data['retry_failed'])
        self.assert_number(data['infra_retries'])
        self.assert_number(data['max_parallel'])

    def test_sweep_without_a_max_records_null_and_the_switches_that_were_given(self):
        p = self.plan()
        result = self.started(p, '--sweep', 'P3', '--sweep-plausible', '--sweep-ahead')
        self.assertEqual(result.returncode, 0, result.stdout)
        data = self.flags(p)
        self.assertEqual(data['sweep'], 'P3')
        self.assertIsNone(data['sweep_max'])
        self.assertIs(data['sweep_plausible'], True)
        self.assertIs(data['sweep_ahead'], True)

    def test_a_profile_retry_budget_is_recorded_when_no_flag_is_given(self):
        small = {'routing': {'dwarf': {'any': ['sol:medium']}, 'qa': {'any': ['opus:medium']}},
                 'policy': {'explore': 0}}
        self.decide({'P': {**small, 'policy': {'explore': 0, 'retry_failed': 2, 'infra_retries': 3}}})
        p = self.planned('--profile', 'P')
        result = self.started(p)
        self.assertEqual(result.returncode, 0, result.stdout)
        data = self.flags(p)
        self.assertEqual(data['retry_failed'], 2)
        self.assertEqual(data['infra_retries'], 3)
        self.assertEqual(data['argv'], [])

    def test_a_dry_run_writes_no_flags_file(self):
        p = self.plan()
        result = self.run_script('forge-parallel.sh', 'run', p, '--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('dry run', result.stdout)
        self.assertFalse((p / 'run.flags.json').exists())
        self.assertNotIn('could not write', result.stdout)

    def test_a_second_run_rewrites_the_file_and_a_dry_run_leaves_it(self):
        p = self.plan()
        self.assertEqual(self.started(p).returncode, 0)
        first = self.flags(p)
        self.assertEqual(self.started(p, '--max-parallel', '4').returncode, 0)
        second = self.flags(p)
        self.assertGreater(second['at'], first['at'])
        self.assertEqual(second['max_parallel'], 4)
        self.assertEqual(second['argv'], ['--max-parallel', '4'])
        raw = (p / 'run.flags.json').read_bytes()
        result = self.run_script('forge-parallel.sh', 'run', p, '--dry-run', '--max-parallel', '9')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((p / 'run.flags.json').read_bytes(), raw)

    def test_an_unwritable_flags_file_is_one_note_and_the_run_completes(self):
        p = self.plan()
        (p / 'run.flags.json').mkdir()
        result = self.run_script('forge-parallel.sh', 'run', p, '--max-parallel', '2')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count('could not write'), 1)
        self.assertIn('forge: could not write', result.stdout)
        self.assertIn('run.flags.json', result.stdout)
        self.assertNotIn('Traceback', result.stdout)
        self.assertTrue((p / 'run.flags.json').is_dir())
        self.assertFalse(list(p.glob('.run.flags.json.*')))
        self.assertTrue((p / 'tasks' / 'a' / 'merged').exists())

    def test_retry_does_not_write_the_file(self):
        self.env['FAIL_A'] = '1'
        p = self.plan()
        result = self.started(p)
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertEqual(self.flags(p)['retry_failed'], 0)
        before = (p / 'run.flags.json').read_bytes()
        # A retry whose diff matches the previous attempt is refused before a review; a new
        # change gets through do_retry, which still must not rewrite the run's flags.
        self.env['CONTENT'] = 'a different change'
        result = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--retry-failed', '4', '--infra-retries', '3')
        self.assertEqual(result.returncode, 5, result.stdout)
        self.assertEqual((p / 'run.flags.json').read_bytes(), before)
        self.assertNotIn('could not write', result.stdout)

    def test_the_file_is_not_in_either_option_list(self):
        libs = (ROOT / 'scripts' / 'forge-lib-plans.sh').read_text()
        plans = re.search(r'^PLANS_OPTION_FILES="([^"]*)"', libs, re.M).group(1).split()
        self.assertNotIn('run.flags.json', plans)
        compare = (ROOT / 'scripts' / 'forge-compare.py').read_text()
        names = eval(re.search(r'OPTION_FILES = (\(.*?\))\n', compare, re.S).group(1))
        self.assertNotIn('run.flags.json', names)
