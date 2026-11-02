import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CLASSES = ('capacity', 'rate_limit', 'usage_limit', 'budget', 'auth', 'network', 'provider_down', 'empty', 'cancelled')


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


class CauseTableTests(unittest.TestCase):
    def setUp(self):
        self.infra = module('forge_infra')

    def test_every_cause_has_an_entry_with_valid_values(self):
        self.assertEqual(set(self.infra.CAUSES), set(CLASSES))
        for name, cause in self.infra.CAUSES.items():
            with self.subTest(cause=name):
                self.assertEqual(set(cause), {'scope', 'move', 'without_peer', 'hint'})
                self.assertIn(cause['scope'], ('model', 'harness', 'all'))
                self.assertIn(cause['move'], ('lateral', 'wait', 'retry'))
                self.assertIn(cause['without_peer'], ('wait', 'ask', 'wait_if_reset'))
                self.assertTrue(cause['hint'] is None or isinstance(cause['hint'], str))

    def test_the_table_says_what_each_cause_means(self):
        table = {
            'capacity': ('model', 'lateral', 'wait'),
            'rate_limit': ('model', 'wait', 'wait'),
            'usage_limit': ('harness', 'lateral', 'wait_if_reset'),
            'budget': ('harness', 'lateral', 'ask'),
            'auth': ('harness', 'lateral', 'ask'),
            'network': ('all', 'wait', 'wait'),
            'provider_down': ('harness', 'lateral', 'wait'),
            'empty': ('model', 'retry', 'wait'),
            'cancelled': ('model', 'retry', 'ask'),
        }
        for name, (scope, move, without_peer) in table.items():
            with self.subTest(cause=name):
                cause = self.infra.CAUSES[name]
                self.assertEqual((cause['scope'], cause['move'], cause['without_peer']), (scope, move, without_peer))

    def test_hints_name_the_harness_and_leave_no_placeholder(self):
        self.assertEqual(self.infra.hint('budget', 'claude'), 'add credits, or check the billing of claude')
        self.assertEqual(self.infra.hint('auth', 'codex'), 'log in to codex again')
        self.assertEqual(self.infra.hint('cancelled', 'grok'), "check grok's approval and permission settings")
        self.assertIsNone(self.infra.hint('capacity', 'codex'))
        for name in CLASSES:
            with self.subTest(cause=name):
                text = self.infra.hint(name, 'codex')
                self.assertTrue(text is None or '{' not in text, text)

    def test_legacy_quota_is_usage_limit(self):
        self.assertEqual(self.infra.cause_of('quota'), 'usage_limit')
        self.assertEqual(self.infra.entry('quota'), self.infra.entry('usage_limit'))
        self.assertEqual(self.infra.cause_of('usage_limit'), 'usage_limit')

    def test_unknown_and_empty_names(self):
        self.assertEqual(self.infra.cause_of('gremlins'), 'gremlins')
        self.assertEqual(self.infra.cause_of(None), '')
        self.assertEqual(self.infra.cause_of(''), '')
        # An unknown cause pauses everything and waits, like network.
        for name in ('gremlins', None, ''):
            with self.subTest(cls=name):
                self.assertEqual(self.infra.entry(name), self.infra.entry('network'))
                self.assertEqual(self.infra.entry(name)['scope'], 'all')
                self.assertEqual(self.infra.entry(name)['move'], 'wait')
                self.assertIsNone(self.infra.hint(name, 'codex'))

    def test_needs_you(self):
        needs_you = self.infra.needs_you
        self.assertTrue(needs_you('budget', False, False, False))         # money is gone
        self.assertFalse(needs_you('budget', True, False, False))         # a peer takes the work
        self.assertFalse(needs_you('usage_limit', False, True, True))     # the reset is inside the cap
        self.assertTrue(needs_you('usage_limit', False, False, False))    # no reset known
        self.assertTrue(needs_you('usage_limit', False, True, False))     # the reset is past the cap
        self.assertFalse(needs_you('capacity', False, False, False))      # capacity waits for itself
        self.assertTrue(needs_you('auth', False, False, False))
        self.assertTrue(needs_you('cancelled', False, False, False))
        self.assertFalse(needs_you('network', False, False, False))
        self.assertFalse(needs_you('quota', False, True, True))           # legacy name, usage_limit


class WaitCapTests(unittest.TestCase):
    def test_the_cap_is_the_schedulers_cap(self):
        self.assertEqual(module('forge_infra').WAIT_CAP_S, 6 * 3600)
        self.assertEqual(module('forge_infra').WAIT_CAP_S, module('forge-schedule').INFRA_WAIT_CAP)


class SchedulerTests(unittest.TestCase):
    def test_each_cause_gets_the_scope_its_table_entry_names(self):
        scheduler = module('forge-schedule')
        table = [
            ('capacity', 'model:codex/sol'),
            ('rate_limit', 'model:codex/sol'),
            ('empty', 'model:codex/sol'),
            ('cancelled', 'model:codex/sol'),
            ('usage_limit', 'harness:codex'),
            ('quota', 'harness:codex'),        # a class an old run recorded
            ('budget', 'harness:codex'),
            ('auth', 'harness:codex'),
            ('provider_down', 'harness:codex'),
            ('network', 'all'),
            ('something_new', 'all'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for cls, scope in table:
                with self.subTest(cls=cls):
                    (root / 'infra.txt').write_text('class=%s\nharness=codex\nmodel=sol\nstage=dwarf\n' % cls)
                    info = scheduler.infra_info(root)
                    self.assertEqual(info['class'], cls)
                    self.assertEqual(scheduler.infra_scope(info)[0], scope)


if __name__ == '__main__':
    unittest.main()


class SummaryRecordTests(unittest.TestCase):
    """`forge_infra.py stop` and `held` on a plan an earlier run left an infra.last in (round 2 of the final
    review of run 1): an old pause must not lend its hint to a new stop, nor name a task that has run since."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plan = Path(self.tmp.name)
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\tf\tsonnet\topus\tA\nb\t-\tlow\tg\tastra\topus\tB\n')
        for name in 'ab':
            (self.plan / 'tasks' / name).mkdir(parents=True)
        (self.plan / 'infra.last').write_text(json.dumps({'scopes': [
            {'scope': 'harness:codex', 'class': 'budget', 'harness': 'codex', 'model': None, 'tasks': ['a'],
             'held': ['b'], 'needs_you': True, 'hint': 'add credits, or check the billing of codex'}]}))

    def infra(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge_infra.py'), *args, str(self.plan), *(['a'] if args == ('stop',) else [])],
                              capture_output=True, text=True, timeout=60)

    def test_a_pause_on_another_harness_is_not_this_stops_pause(self):
        (self.plan / 'tasks/a/infra.txt').write_text('class=capacity\nstage=dwarf\nharness=claude\nmodel=sonnet\n')
        line = self.infra('stop').stdout
        self.assertIn('capacity on claude/sonnet', line)
        self.assertNotIn('needs you', line)
        (self.plan / 'tasks/a/infra.txt').write_text('class=budget\nstage=dwarf\nharness=codex\nmodel=sol\n')
        self.assertIn('needs you: add credits, or check the billing of codex', self.infra('stop').stdout)

    def test_a_held_task_that_has_run_since_is_not_held_back(self):
        r = self.infra('held')
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout, r'^  b\s+held back: budget on codex — needs you')
        for done in ('merged', 'status'):
            with self.subTest(done):
                (self.plan / 'tasks/b' / done).write_text('FAIL\n' if done == 'status' else '')
                r = self.infra('held')
                self.assertEqual((r.returncode, r.stdout), (1, ''))
                (self.plan / 'tasks/b' / done).unlink()
