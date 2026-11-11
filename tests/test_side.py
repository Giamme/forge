"""Feature settings and the side-dispatch wrapper.

`settings` and `allowed` are the choice a later brief, summary or retro step reads.
`run` is the dispatch those steps make. `forge config` coverage for the same keys
lives in test_config.py.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import test_forge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_dataset  # noqa: E402
import forge_side  # noqa: E402
from forge_decide import save_user  # noqa: E402

OFF = {
    'retro': False,
    'brief': {'level': 'off', 'scope': 'task', 'model': ''},
    'summaries': {'mode': 'off', 'model': ''},
}


class SideCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.plan = self.base / 'plan'
        self.repo = self.base / 'repo'
        self.plan.mkdir()
        self.repo.mkdir()
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        self.cfg = self.base / 'cfg'
        patcher = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.cfg)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('FORGE_RETRO', 'FORGE_BRIEF', 'FORGE_SUMMARIES', 'FORGE_DECIDE_REPO'):
            os.environ.pop(name, None)

    def user(self, features):
        save_user({'version': 1, 'custom': {'kept': True}, 'features': features})

    def fill(self, kind, count, task='a'):
        root = self.plan / 'side' / kind / task
        root.mkdir(parents=True, exist_ok=True)
        for number in range(1, count + 1):
            (root / str(number)).mkdir()


class SettingsTests(SideCase):
    def test_nothing_configured_is_off(self):
        # An env value other than off does not turn a feature on. There is no "on" switch
        # outside the plan file and the user settings.
        os.environ['FORGE_RETRO'] = 'on'
        os.environ['FORGE_BRIEF'] = 'strict'
        os.environ['FORGE_SUMMARIES'] = 'on'
        self.assertEqual(forge_side.settings(self.plan), OFF)
        self.assertEqual(forge_side.allowed('brief', self.plan), (False, 'off'))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (False, 'off'))

    def test_plan_files_beat_the_user_settings(self):
        self.user({
            'retro': 'on',
            'brief': {'level': 'advisory', 'scope': 'plan', 'model': 'user-brief'},
            'summaries': {'mode': 'on', 'model': 'user-sum'},
        })
        (self.plan / 'retro').write_text('off\n')
        (self.plan / 'brief').write_text(' strict \n')
        (self.plan / 'brief_scope').write_text('wave\n')
        (self.plan / 'brief_model').write_text(' plan-brief \n')
        (self.plan / 'summaries').write_text('off\n')
        (self.plan / 'summaries_model').write_text('plan-sum\n')
        self.assertEqual(forge_side.settings(self.plan), {
            'retro': False,
            'brief': {'level': 'strict', 'scope': 'wave', 'model': 'plan-brief'},
            'summaries': {'mode': 'off', 'model': 'plan-sum'},
        })
        # The user file's other keys are not part of the feature read.
        stored = json.loads((self.cfg / 'forge' / 'decide.json').read_text())
        self.assertEqual(stored['custom'], {'kept': True})

    def test_user_settings_apply_when_the_plan_names_nothing(self):
        self.user({
            'retro': 'on',
            'brief': {'level': 'gated', 'scope': 'plan', 'model': 'sol'},
            'summaries': {'mode': 'on', 'model': 'astra'},
        })
        self.assertEqual(forge_side.settings(self.plan), {
            'retro': True,
            'brief': {'level': 'gated', 'scope': 'plan', 'model': 'sol'},
            'summaries': {'mode': 'on', 'model': 'astra'},
        })

    def test_invalid_plan_tokens_fall_through_but_a_retro_file_can_force_off(self):
        # Brief, scope and summaries ignore a token they do not recognise. Retro's file is
        # the on/off switch itself: anything other than `on` is off, and it beats the user.
        self.user({
            'retro': 'on',
            'brief': {'level': 'advisory', 'scope': 'wave', 'model': 'user-m'},
            'summaries': {'mode': 'on', 'model': 'user-s'},
        })
        (self.plan / 'retro').write_text('ON\n')
        (self.plan / 'brief').write_text('STRICT\n')
        (self.plan / 'brief_scope').write_text('nope\n')
        (self.plan / 'summaries').write_text('yes\n')
        (self.plan / 'brief_model').write_text('   \n')
        self.assertEqual(forge_side.settings(self.plan), {
            'retro': False,
            'brief': {'level': 'advisory', 'scope': 'wave', 'model': 'user-m'},
            'summaries': {'mode': 'on', 'model': 'user-s'},
        })

    def test_env_off_beats_the_plan_and_the_user_and_keeps_scope_and_model(self):
        self.user({
            'retro': 'on',
            'brief': {'level': 'strict', 'scope': 'plan', 'model': 'user-m'},
            'summaries': {'mode': 'on', 'model': 'user-s'},
        })
        (self.plan / 'retro').write_text('on\n')
        (self.plan / 'brief').write_text('strict\n')
        (self.plan / 'brief_scope').write_text('wave\n')
        (self.plan / 'brief_model').write_text('plan-brief\n')
        (self.plan / 'summaries').write_text('on\n')
        (self.plan / 'summaries_model').write_text('plan-sum\n')
        os.environ['FORGE_RETRO'] = ' Off '
        os.environ['FORGE_BRIEF'] = 'OFF'
        os.environ['FORGE_SUMMARIES'] = 'off'
        self.assertEqual(forge_side.settings(self.plan), {
            'retro': False,
            'brief': {'level': 'off', 'scope': 'wave', 'model': 'plan-brief'},
            'summaries': {'mode': 'off', 'model': 'plan-sum'},
        })


class AllowedTests(SideCase):
    """One mutation away from a fixture where both kinds may run."""

    def setUp(self):
        super().setUp()
        (self.plan / 'retro').write_text('on\n')
        (self.plan / 'brief').write_text('advisory\n')
        (self.plan / 'brief_scope').write_text('task\n')
        (self.plan / 'brief_model').write_text('sol\n')
        (self.plan / 'summaries').write_text('on\n')
        (self.plan / 'summaries_model').write_text('sol\n')
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')

    def test_both_kinds_may_run(self):
        self.assertEqual(forge_side.allowed('brief', self.plan), (True, None))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (True, None))
        # Asking does not create the side tree. The cap check must not mkdir just to count.
        self.assertFalse((self.plan / 'side').exists())

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            forge_side.allowed('retro', self.plan)
        self.assertFalse((self.plan / 'side').exists())

    def test_brief_env_off_is_env(self):
        os.environ['FORGE_BRIEF'] = 'off'
        self.assertEqual(forge_side.allowed('brief', self.plan), (False, 'env'))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (True, None))

    def test_summaries_env_off_is_env(self):
        os.environ['FORGE_SUMMARIES'] = 'off'
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (False, 'env'))
        self.assertEqual(forge_side.allowed('brief', self.plan), (True, None))

    def test_brief_off_refuses_only_the_brief(self):
        (self.plan / 'brief').write_text('off\n')
        self.assertEqual(forge_side.allowed('brief', self.plan), (False, 'off'))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (True, None))

    def test_summaries_off_refuses_only_the_summary(self):
        (self.plan / 'summaries').write_text('off\n')
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (False, 'off'))
        self.assertEqual(forge_side.allowed('brief', self.plan), (True, None))

    def test_a_task_summaries_file_overrides_the_plan_both_ways(self):
        task = self.plan / 'tasks' / 'a'
        task.mkdir(parents=True)
        (task / 'summaries').write_text('off\n')
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (False, 'off'))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'b'), (True, None))
        self.assertEqual(forge_side.allowed('brief', self.plan), (True, None))
        (self.plan / 'summaries').write_text('off\n')
        (task / 'summaries').write_text('on\n')
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (True, None))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'b'), (False, 'off'))

    def test_a_task_override_does_not_beat_env_off_or_leave_the_plan(self):
        os.environ['FORGE_SUMMARIES'] = 'off'
        task = self.plan / 'tasks' / 'a'
        task.mkdir(parents=True)
        (task / 'summaries').write_text('on\n')
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (False, 'env'))
        os.environ.pop('FORGE_SUMMARIES')
        # `../sneaky` would resolve to <plan>/sneaky if the task id were joined unsafely.
        (self.plan / 'sneaky').mkdir()
        (self.plan / 'sneaky' / 'summaries').write_text('off\n')
        (task / 'summaries').write_text('maybe\n')
        self.assertEqual(forge_side.allowed('summary', self.plan, '../sneaky'), (True, None))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (True, None))

    def test_never_send_refuses_both_kinds_and_leaves_retro_on(self):
        forge = self.repo / '.forge'
        forge.mkdir()
        (forge / 'decide.json').write_text('{"privacy": "never-send"}\n')
        self.assertEqual(forge_side.allowed('brief', self.plan), (False, 'never-send'))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (False, 'never-send'))
        self.assertTrue(forge_side.settings(self.plan)['retro'])

    def test_brief_without_a_model_is_no_model(self):
        (self.plan / 'brief_model').unlink()
        self.assertEqual(forge_side.allowed('brief', self.plan), (False, 'no-model'))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (True, None))

    def test_summary_without_a_model_is_no_model(self):
        (self.plan / 'summaries_model').unlink()
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (False, 'no-model'))
        self.assertEqual(forge_side.allowed('brief', self.plan), (True, None))

    def test_cap_is_ten_for_one_task_and_counts_every_task_of_that_kind(self):
        # Dirs live under task b. The cap is per kind, so a dispatch for task a is at the cap too.
        # Nine is still under the floor of ten; the tenth refuses. The other kind is untouched.
        self.fill('brief', 9, 'b')
        self.assertEqual(forge_side.allowed('brief', self.plan), (True, None))
        (self.plan / 'side' / 'brief' / 'b' / '10').mkdir()
        self.assertEqual(forge_side.allowed('brief', self.plan), (False, 'cap'))
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (True, None))

    def test_cap_grows_to_three_times_the_tasks_ignoring_comments_and_blanks(self):
        (self.plan / 'tasks.tsv').write_text(
            '# note\n'
            '\n'
            'a\t-\tlow\ta.txt\tsol\topus\tA\n'
            'b\ta\tlow\tb.txt\tsol\topus\tB\n'
            'c\ta\tlow\tc.txt\tsol\topus\tC\n'
            'd\ta\tlow\td.txt\tsol\topus\tD\n')
        self.fill('summary', 11, 'a')
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (True, None))
        (self.plan / 'side' / 'summary' / 'a' / '12').mkdir()
        self.assertEqual(forge_side.allowed('summary', self.plan, 'a'), (False, 'cap'))
        self.assertEqual(forge_side.allowed('brief', self.plan), (True, None))


class CapTests(unittest.TestCase):
    def test_short_text_is_unchanged_and_a_cut_keeps_the_marker(self):
        self.assertEqual(forge_side.cap('short', 10), 'short')
        self.assertEqual(forge_side.cap(None, 5), '')
        cut = forge_side.cap('abcdefghijklmnopqrstuvwxyz', 20)
        self.assertTrue(cut.endswith('… (capped)'))
        self.assertLessEqual(len(cut), 20)
        self.assertNotIn('xyz', cut)
        # Smaller than the marker: the marker is still visible, and it is the whole result.
        self.assertEqual(forge_side.cap('abcdefghijklmnopqrstuvwxyz', 3), '… (capped)')
        self.assertEqual(forge_side.cap('abc', -1), '… (capped)')


class RunTests(unittest.TestCase):
    """Dispatch goes through the fake CLI harness, borrowed the way test_dispatch does.

    Not a ForgeTests subclass: that class's own tests would run again, and they call
    `self.plan()` as a method.
    """

    git = test_forge.ForgeTests.git
    scenario = test_forge.ForgeTests.scenario
    calls = test_forge.ForgeTests.calls

    def setUp(self):
        test_forge.ForgeTests.setUp(self)
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        for name in ('FORGE_RETRO', 'FORGE_BRIEF', 'FORGE_SUMMARIES', 'FORGE_REGISTRY'):
            os.environ.pop(name, None)
            self.env.pop(name, None)
        self.plan = self.root / 'plan'
        self.plan.mkdir()
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        self.cache = self.root / 'capability-cache'
        self.cache.mkdir()
        # One store per test, so an event assertion is not reading an earlier test's rows.
        data_home = self.root / 'data-home'
        data_home.mkdir()
        os.environ['FORGE_DATA_HOME'] = str(data_home)
        self.env['FORGE_DATA_HOME'] = str(data_home)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def arm(self, table):
        self.scenario(table)
        os.environ.update(self.env)
        os.environ['FORGE_CAPABILITY_CACHE'] = str(self.cache)

    def side_events(self):
        path = forge_dataset.dataset_dir() / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute('SELECT kind, task, data FROM events ORDER BY ts, rid').fetchall()
        finally:
            db.close()
        found = []
        for kind, task, data in rows:
            body = json.loads(data)
            if body.get('name') == 'side_dispatch':
                found.append((kind, task, body))
        return found

    def test_run_numbers_per_task_and_records_the_dispatch(self):
        # 5 is the highest n under c. A non-numeric sibling does not take a number.
        # Task a has none, so it starts at 1. The features are off: run does not re-check that.
        pre = self.plan / 'side' / 'brief' / 'c'
        (pre / '5').mkdir(parents=True)
        (pre / 'note').mkdir()
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        first = forge_side.run('brief', self.plan, 'c', 'Read the tree.\n', 'sol', timeout=12)
        self.assertTrue(first['ok'], first)
        self.assertIsNone(first['reason'])
        self.assertEqual(first['last'], 'SIDE_OK\n')
        dest = Path(first['dir'])
        self.assertEqual(dest.name, '6')
        self.assertEqual((dest / 'side.last').read_text(), 'SIDE_OK\n')
        self.assertEqual((dest / 'prompt.txt').read_text(), 'Read the tree.\n')
        resolved = (dest / 'side.resolved').read_text()
        self.assertIn('role=side\n', resolved)
        self.assertIn('effort=low\n', resolved)
        self.assertIn('timeout=12\n', resolved)
        self.assertIn('timeout_source=explicit\n', resolved)
        self.assertFalse((self.repo / 'change.txt').exists())
        self.assertEqual(self.calls(), ['qa'])
        second = forge_side.run('brief', self.plan, 'a', 'Other task.\n', 'sol', timeout=12)
        self.assertTrue(second['ok'], second)
        self.assertEqual(Path(second['dir']).name, '1')
        self.assertEqual(Path(second['dir']).parent.name, 'a')
        third = forge_side.run('brief', self.plan, 'c', 'Again.\n', 'sol', timeout=12)
        self.assertEqual(Path(third['dir']).name, '7')
        events = self.side_events()
        self.assertEqual([(kind, task, body['ok'], body['reason'], body['kind'], body['spec'])
                          for kind, task, body in events],
                         [('event', 'c', True, None, 'brief', 'sol'),
                          ('event', 'a', True, None, 'brief', 'sol'),
                          ('event', 'c', True, None, 'brief', 'sol')])

    def test_a_failed_dispatch_returns_ok_false(self):
        self.arm({'*.qa.*': {'rc': 1, 'findings': 'SIDE_FAIL', 'verdict': 'none'}})
        result = forge_side.run('summary', self.plan, 'a', 'Summarise.\n', 'sol')
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'error')
        self.assertEqual(result['last'], 'SIDE_FAIL\n')
        self.assertEqual(Path(result['dir']).name, '1')

    def test_the_tenth_dispatch_runs_and_the_next_is_cap(self):
        self.fill_summary(9)
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        ran = forge_side.run('summary', self.plan, 'a', 'Summarise.\n', 'sol')
        self.assertTrue(ran['ok'], ran)
        self.assertEqual(Path(ran['dir']).name, '10')
        self.assertEqual(self.calls(), ['qa'])
        blocked = forge_side.run('summary', self.plan, 'a', 'One more.\n', 'sol')
        self.assertEqual(blocked, {'ok': False, 'last': None, 'dir': None, 'reason': 'cap'})
        self.assertFalse((self.plan / 'side' / 'summary' / 'a' / '11').exists())
        self.assertEqual(self.calls(), ['qa'])
        # The other kind has its own count.
        other = forge_side.run('brief', self.plan, 'a', 'A brief.\n', 'sol')
        self.assertTrue(other['ok'], other)
        self.assertEqual(Path(other['dir']).name, '1')
        events = [body for _, _, body in self.side_events()]
        self.assertEqual([(body['kind'], body['ok'], body['reason']) for body in events],
                         [('summary', True, None), ('summary', False, 'cap'), ('brief', True, None)])

    def fill_summary(self, count):
        root = self.plan / 'side' / 'summary' / 'a'
        root.mkdir(parents=True)
        for number in range(1, count + 1):
            (root / str(number)).mkdir()

    def test_a_dispatch_timeout_is_timeout(self):
        self.arm({'*.qa.*': {'sleep': 30}})
        result = forge_side.run('brief', self.plan, 'a', 'Slow.\n', 'sol', timeout=1)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'timeout')
        self.assertTrue((Path(result['dir']) / 'side.timedout').exists())
        events = self.side_events()
        self.assertEqual(len(events), 1, events)
        self.assertEqual((events[0][2]['ok'], events[0][2]['reason'], events[0][2]['kind']),
                         (False, 'timeout', 'brief'))

    def test_unknown_kind_raises_before_dispatch(self):
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        with self.assertRaises(ValueError):
            forge_side.run('retro', self.plan, 'a', 'prompt', 'sol')
        self.assertFalse((self.plan / 'side').exists())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.side_events(), [])

    def test_a_missing_dispatch_script_does_not_create_a_dir(self):
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        with mock.patch.object(forge_side, 'DISPATCH', self.root / 'missing-dispatch.sh'):
            result = forge_side.run('brief', self.plan, 'a', 'Read.\n', 'sol')
        self.assertEqual(result, {'ok': False, 'last': None, 'dir': None, 'reason': 'missing'})
        self.assertFalse((self.plan / 'side').exists())
        self.assertEqual(self.calls(), [])
        events = self.side_events()
        self.assertEqual(len(events), 1, events)
        body = events[0][2]
        self.assertEqual((body['ok'], body['reason'], body['task'], body['spec']),
                         (False, 'missing', 'a', 'sol'))


if __name__ == '__main__':
    unittest.main()
