"""The infrastructure backoff shape (#30): `--infra-backoff`, `forge control set infra-backoff`
and the scheduler's waits, all through forge-schedule.py's one parser."""
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
import test_infra_scope as scope

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forge_schedule', ROOT / 'scripts/forge-schedule.py')
schedule = importlib.util.module_from_spec(spec)
spec.loader.exec_module(schedule)


class ShapeTests(unittest.TestCase):
    def waits(self, shape):
        parsed = schedule.parse_backoff(shape)
        return [schedule.backoff_wait(parsed, cycle) for cycle in range(6)]

    def test_each_shape_gives_its_waits_and_caps(self):
        cases = [
            ('exp:10', [10, 20, 40, 80, 160, 320]),
            ('exp:600', [600, 1200, 1800, 1800, 1800, 1800]),   # the default cap: max(base, 1800)
            ('exp:60,300', [60, 120, 240, 300, 300, 300]),
            ('linear:10,10', [10, 20, 30, 40, 50, 60]),
            ('linear:5,10,25', [5, 15, 25, 25, 25, 25]),
            ('10,20,30', [10, 20, 30, 30, 30, 30]),
            ('list:0.5', [0.5] * 6),
        ]
        for shape, expected in cases:
            with self.subTest(shape):
                self.assertEqual(self.waits(shape), expected)

    def test_malformed_shapes_are_rejected(self):
        self.assertEqual(schedule.parse_backoff('exp:10,300'), ('exp', [10.0, 300.0]))   # the valid control
        for shape in ('linear:10', 'exp:', 'list:', '-5', '0', 'abc', 'exp:10,x', 'exp:1,2,3', 'linear:1,0',
                      'fib:1,2', ''):
            with self.subTest(shape):
                with self.assertRaises(ValueError):
                    schedule.parse_backoff(shape)


class SchedulerBackoffTests(scope.InfraScopeBase):
    def test_a_model_pause_waits_by_the_plans_shape_and_names_it(self):
        self.task('a', [scope.SOL_LIMIT] * 4, dwarf='sol')
        (self.plan / 'infra_backoff').write_text('linear:0.05,0.05\n')
        r = self.run_scheduler('--infra-retries', 3)
        self.assertEqual(r.returncode, 8, r.stderr)
        for wait, number in (('0.05', 1), ('0.1', 2), ('0.15', 3)):
            self.assertIn('waiting %ss before resuming (backoff linear:0.05,0.05, wait %d of 3)' % (wait, number), r.stderr)
        self.assertEqual(self.calls('a'), 4)

    def test_without_a_shape_the_waits_are_as_before(self):
        self.task('a', [scope.SOL_LIMIT] * 3, dwarf='sol')
        r = self.run_scheduler('--infra-retries', 2)
        self.assertIn('waiting 0.05s before resuming (backoff exp:0.05, wait 1 of 2)', r.stderr)
        self.assertIn('waiting 0.1s before resuming (backoff exp:0.05, wait 2 of 2)', r.stderr)

    def test_an_invalid_file_warns_once_and_uses_the_default(self):
        self.task('a', [scope.SOL_LIMIT] * 3, dwarf='sol')
        (self.plan / 'infra_backoff').write_text('linear:10\n')
        r = self.run_scheduler('--infra-retries', 2)
        self.assertEqual(r.stderr.count('infra_backoff ignored'), 1, r.stderr)
        self.assertIn('waiting 0.1s before resuming (backoff exp:0.05, wait 2 of 2)', r.stderr)


class FlagTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    calls = fixtures.ForgeTests.calls
    run_script = fixtures.ForgeTests.run_script
    plan = fixtures.ForgeTests.plan

    def setUp(self):
        fixtures.ForgeTests.setUp(self)

    def test_a_malformed_shape_stops_run_before_any_dispatch(self):
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p, '--infra-backoff', 'linear:10')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('wrong number of values', r.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse((p / 'infra_backoff').exists())

    def test_a_valid_shape_is_kept_in_the_plan(self):
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p, '--infra-backoff', 'linear:10,10')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'infra_backoff').read_text(), 'linear:10,10\n')


class ControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.plan = Path(tmp.name) / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\tsrc\tsol\tastra\tA\n')
        self.env = {k: v for k, v in os.environ.items() if k != 'FORGE_INFRA_BACKOFF'}
        self.env['FORGE_DATASET'] = 'off'

    def control(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/forge-control.py'), *map(str, args)],
                              capture_output=True, text=True, env=self.env, timeout=60)

    def shown(self):
        return json.loads(self.control('show', self.plan, '--json').stdout)['run']['infra_backoff']

    def test_set_validates_writes_logs_shows_and_drops(self):
        self.assertEqual(self.shown(), 'exp:60')
        result = self.control('set', self.plan, 'infra-backoff', 'linear:10,10')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.plan / 'infra_backoff').read_text(), 'linear:10,10\n')
        self.assertEqual(self.shown(), 'linear:10,10')
        entry = json.loads((self.plan / 'control.log').read_text().splitlines()[-1])
        self.assertEqual((entry['key'], entry['value'], entry['previous']), ('infra-backoff', 'linear:10,10', None))
        refused = self.control('set', self.plan, 'infra-backoff', 'linear:10')
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertEqual((self.plan / 'infra_backoff').read_text(), 'linear:10,10\n')
        self.assertEqual(self.control('set', self.plan, 'infra-backoff', 'none').returncode, 0)
        self.assertFalse((self.plan / 'infra_backoff').exists())
        self.assertEqual(self.shown(), 'exp:60')


if __name__ == '__main__':
    unittest.main()
