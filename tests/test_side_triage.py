"""Triage dispatches live under forge's state home, outside any plan.

`triage_home` and `allocate_triage` key that home by a 16-hex repository identity.
`triage_allowed` and `run_triage` are the entry points. `allowed` and `run` refuse the
kind. Dispatch itself goes through the fake CLI harness, the way test_side's RunTests does.
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
from test_side import _alive, _group_pids, _kill_recorded

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_dataset  # noqa: E402
import forge_side  # noqa: E402
from forge_decide import save_user  # noqa: E402

KEY = '0123456789abcdef'
OTHER = 'fedcba9876543210'


class TriageHomeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.state = self.base / 'state'
        self.state.mkdir()
        patcher = mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_home_is_the_state_dir_for_that_key(self):
        got = forge_side.triage_home(KEY)
        self.assertEqual(got, self.state / 'forge' / 'triage' / KEY)
        # Naming the home does not create it. Allocation does.
        self.assertFalse(got.exists())

    def test_an_empty_or_relative_state_home_counts_as_unset(self):
        home = self.base / 'home'
        home.mkdir()
        expected = home / '.local' / 'state' / 'forge' / 'triage' / KEY
        for value in ('', 'relative'):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {'HOME': str(home), 'XDG_STATE_HOME': value}):
                    self.assertEqual(forge_side.triage_home(KEY), expected)

    def test_a_bad_key_is_refused(self):
        for key in ('', 'abc', '0123456789abcde', '0123456789abcdef0', '0123456789ABCDEF',
                    '0123456789abcdeg', '../etc/passwd', '0123456789abcde/', None):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    forge_side.triage_home(key)
                with self.assertRaises(ValueError):
                    forge_side.allocate_triage(key)
        self.assertFalse((self.state / 'forge').exists())

    def test_numbering_skips_a_non_numeric_sibling_and_starts_each_key_at_one(self):
        # 5 is the highest n. `note` is a symlink, so following it would write outside the home.
        target = self.base / 'note-target'
        target.mkdir()
        home = forge_side.triage_home(KEY)
        (home / '5').mkdir(parents=True)
        (home / 'note').symlink_to(target)
        (home / '1').mkdir()
        first = forge_side.allocate_triage(KEY)
        self.assertEqual(first, home / '6')
        self.assertTrue(first.is_dir())
        self.assertFalse(first.is_symlink())
        second = forge_side.allocate_triage(KEY)
        self.assertEqual(second.name, '7')
        self.assertEqual(list(target.iterdir()), [])
        other = forge_side.allocate_triage(OTHER)
        self.assertEqual(other, self.state / 'forge' / 'triage' / OTHER / '1')
        self.assertTrue((home / '.lock').is_file())

    def test_the_cap_is_two_hundred_dispatch_dirs_and_a_note_does_not_count(self):
        # 199 real dirs whose names sit far above 200, plus a non-numeric sibling.
        # The next dir is still allowed, and its name is one past the highest n.
        home = forge_side.triage_home(KEY)
        home.mkdir(parents=True)
        for number in range(1000, 1199):
            (home / str(number)).mkdir()
        (home / 'note').mkdir()
        got = forge_side.allocate_triage(KEY)
        self.assertEqual(got.name, '1199')
        with self.assertRaises(forge_side.TriageCapped):
            forge_side.allocate_triage(KEY)
        self.assertFalse((home / '1200').exists())
        self.assertTrue((home / 'note').is_dir())

    def test_a_symlinked_home_is_refused(self):
        target = self.base / 'elsewhere'
        target.mkdir()
        home = forge_side.triage_home(KEY)
        home.parent.mkdir(parents=True)
        home.symlink_to(target)
        with self.assertRaises(ValueError) as caught:
            forge_side.allocate_triage(KEY)
        self.assertIn('symlink', str(caught.exception))
        self.assertEqual(list(target.iterdir()), [])

    def test_a_symlinked_dispatch_dir_is_refused(self):
        target = self.base / 'elsewhere'
        target.mkdir()
        home = forge_side.triage_home(KEY)
        home.mkdir(parents=True)
        (home / '2').mkdir()
        (home / '9').symlink_to(target)
        before = {child.name for child in home.iterdir()}
        with self.assertRaises(ValueError) as caught:
            forge_side.allocate_triage(KEY)
        self.assertIn('symlink', str(caught.exception))
        # The lock may be taken before the link is noticed. No new dispatch dir is.
        self.assertEqual({child.name for child in home.iterdir()} - {'.lock'}, before)
        self.assertEqual(list(target.iterdir()), [])


class TriageAllowedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self._saved = os.environ.get('FORGE_TRIAGE')
        os.environ.pop('FORGE_TRIAGE', None)
        self.addCleanup(self._restore_triage)

    def _restore_triage(self):
        if self._saved is None:
            os.environ.pop('FORGE_TRIAGE', None)
        else:
            os.environ['FORGE_TRIAGE'] = self._saved

    def test_the_reasons_are_env_then_never_send_then_no_model(self):
        # One mutation at a time, from a repo that may run, so each reason is the one
        # that changed and the earlier reason wins while it still applies.
        self.assertEqual(forge_side.triage_allowed(self.repo, 'sol'), (True, None))
        os.environ['FORGE_TRIAGE'] = ' OFF '
        forge = self.repo / '.forge'
        forge.mkdir()
        (forge / 'decide.json').write_text('{"privacy": "never-send"}\n')
        self.assertEqual(forge_side.triage_allowed(self.repo, ''), (False, 'env'))
        os.environ.pop('FORGE_TRIAGE')
        self.assertEqual(forge_side.triage_allowed(self.repo, ''), (False, 'never-send'))
        self.assertEqual(forge_side.triage_allowed(self.repo, '   '), (False, 'never-send'))
        (forge / 'decide.json').write_text('{}\n')
        for spec in ('', '   ', None):
            with self.subTest(spec=spec):
                self.assertEqual(forge_side.triage_allowed(self.repo, spec), (False, 'no-model'))
        self.assertEqual(forge_side.triage_allowed(self.repo, 'sol'), (True, None))

    def test_a_named_spec_ignores_brief_settings_and_the_user_file(self):
        config = self.base / 'config'
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(config), 'FORGE_BRIEF': 'off',
                                          'FORGE_SUMMARIES': 'off'}):
            save_user({'version': 1, 'features': {
                'retro': 'off',
                'brief': {'level': 'off', 'scope': 'task', 'model': ''},
                'summaries': {'mode': 'off', 'model': ''},
            }})
            self.assertEqual(forge_side.triage_allowed(self.repo, 'sol'), (True, None))


class TriageRunTests(unittest.TestCase):
    """Dispatch goes through the fake CLI harness, borrowed the way test_side does.

    Not a ForgeTests subclass: that class's own tests would run again.
    """

    git = test_forge.ForgeTests.git
    scenario = test_forge.ForgeTests.scenario
    calls = test_forge.ForgeTests.calls

    def setUp(self):
        test_forge.ForgeTests.setUp(self)
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        for name in ('FORGE_RETRO', 'FORGE_BRIEF', 'FORGE_SUMMARIES', 'FORGE_REGISTRY', 'FORGE_TRIAGE'):
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
        self.state = self.root / 'state'
        self.state.mkdir()
        os.environ['XDG_STATE_HOME'] = str(self.state)
        self.env['XDG_STATE_HOME'] = str(self.state)

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

    def test_run_and_allowed_refuse_triage_before_dispatch(self):
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        for call in (lambda: forge_side.allowed('triage', self.plan),
                     lambda: forge_side.run('triage', self.plan, 'a', 'prompt', 'sol')):
            with self.assertRaises(ValueError) as caught:
                call()
            self.assertEqual(str(caught.exception), 'use the triage functions')
        self.assertFalse((self.plan / 'side').exists())
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.side_events(), [])

    def test_a_refused_triage_does_not_dispatch(self):
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        dest = forge_side.allocate_triage(KEY)
        os.environ['FORGE_TRIAGE'] = 'off'
        refused = forge_side.run_triage(dest, self.repo, 'Read.\n', 'sol', timeout=12)
        self.assertEqual((refused['ok'], refused['reason'], refused['dir'], refused['last']),
                         (False, 'env', None, None))
        os.environ.pop('FORGE_TRIAGE')
        forge = self.repo / '.forge'
        forge.mkdir()
        (forge / 'decide.json').write_text('{"privacy": "never-send"}\n')
        refused = forge_side.run_triage(dest, self.repo, 'Read.\n', 'sol', timeout=12)
        self.assertEqual((refused['ok'], refused['reason'], refused['dir']), (False, 'never-send', None))
        (forge / 'decide.json').unlink()
        refused = forge_side.run_triage(dest, self.repo, 'Read.\n', '   ', timeout=12)
        self.assertEqual((refused['ok'], refused['reason'], refused['dir']), (False, 'no-model', None))
        self.assertFalse((dest / 'prompt.txt').exists())
        self.assertEqual(self.calls(), [])
        events = self.side_events()
        self.assertEqual([(body['kind'], body['task'], task, body['ok'], body['reason'])
                          for kind, task, body in events],
                         [('triage', None, None, False, 'env'),
                          ('triage', None, None, False, 'never-send'),
                          ('triage', None, None, False, 'no-model')])
        self.assertTrue(all(kind == 'event' for kind, _, _ in events))

    def test_a_successful_triage_records_the_dispatch(self):
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        dest = forge_side.allocate_triage(KEY)
        (dest / 'findings.json').write_text('[]\n')
        result = forge_side.run_triage(dest, self.repo, 'Read the findings.\n', 'sol', timeout=12)
        self.assertTrue(result['ok'], result)
        self.assertIsNone(result['reason'])
        self.assertEqual(result['last'], 'SIDE_OK\n')
        self.assertEqual(result['dir'], str(dest))
        self.assertEqual((dest / 'prompt.txt').read_text(), 'Read the findings.\n')
        self.assertEqual((dest / 'side.last').read_text(), 'SIDE_OK\n')
        self.assertEqual((dest / 'findings.json').read_text(), '[]\n')
        resolved = (dest / 'side.resolved').read_text()
        self.assertIn('role=side\n', resolved)
        self.assertIn('timeout=12\n', resolved)
        self.assertFalse((self.repo / 'change.txt').exists())
        self.assertFalse((self.plan / 'side').exists())
        self.assertEqual(self.calls(), ['qa'])
        events = self.side_events()
        self.assertEqual(len(events), 1, events)
        kind, task, body = events[0]
        self.assertEqual(kind, 'event')
        self.assertIsNone(task)
        self.assertEqual((body['kind'], body['task'], body['ok'], body['reason'], body['spec']),
                         ('triage', None, True, None, 'sol'))

    def test_a_bad_timeout_falls_back_to_an_hour(self):
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        dest = forge_side.allocate_triage(KEY)
        result = forge_side.run_triage(dest, self.repo, 'Read.\n', 'sol', timeout='nope')
        self.assertTrue(result['ok'], result)
        self.assertIn('timeout=3600\n', (dest / 'side.resolved').read_text())

    def test_a_missing_dispatch_script_does_not_write_the_prompt(self):
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        dest = forge_side.allocate_triage(KEY)
        with mock.patch.object(forge_side, 'DISPATCH', self.root / 'missing-dispatch.sh'):
            result = forge_side.run_triage(dest, self.repo, 'Read.\n', 'sol')
        self.assertEqual(result, {'ok': False, 'last': None, 'dir': None, 'reason': 'missing'})
        self.assertFalse((dest / 'prompt.txt').exists())
        self.assertEqual(self.calls(), [])
        events = self.side_events()
        self.assertEqual(len(events), 1, events)
        self.assertEqual((events[0][2]['kind'], events[0][2]['task'], events[0][2]['reason']),
                         ('triage', None, 'missing'))

    def test_a_hung_preflight_does_not_leave_its_processes(self):
        # Help runs before dispatch's own watchdog. The wrapper timeout has to stop the
        # CLI and the runtime helper that spawned it, not only the shell. The grace is
        # how long that watchdog is given to finish; this preflight never reaches it.
        pids = self.root / 'hung-pids'
        self.addCleanup(_kill_recorded, pids)
        sleeper = (
            '#!/usr/bin/env python3\n'
            'import os, sys, time, pathlib\n'
            'if "--help" in sys.argv:\n'
            '    pathlib.Path(os.environ["HUNG_PIDS"]).write_text(\n'
            '        "%s\\n%s\\n%s\\n" % (os.getpgid(0), os.getpid(), os.getppid()))\n'
            '    time.sleep(120)\n'
            'sys.exit(0)\n')
        for name in ('codex', 'claude'):
            cli = self.bin / name
            cli.write_text(sleeper)
            cli.chmod(0o755)
        self.arm({'*.qa.*': {'findings': 'SIDE_OK', 'verdict': 'none'}})
        os.environ['HUNG_PIDS'] = str(pids)
        dest = forge_side.allocate_triage(KEY)
        with mock.patch.object(forge_side, '_DISPATCH_GRACE_S', 0):
            result = forge_side.run_triage(dest, self.repo, 'Slow.\n', 'sol', timeout=5)
        self.assertFalse(result['ok'])
        self.assertEqual(result['reason'], 'timeout')
        self.assertEqual(result['dir'], str(dest))
        self.assertTrue(pids.is_file(), 'the help preflight never started')
        pgid, cli_pid, helper_pid = (int(line) for line in pids.read_text().split())
        self.assertFalse(_alive(cli_pid), cli_pid)
        self.assertFalse(_alive(helper_pid), helper_pid)
        if pgid != os.getpgrp():
            self.assertEqual(_group_pids(pgid), [])
        events = self.side_events()
        self.assertEqual(len(events), 1, events)
        self.assertEqual((events[0][2]['kind'], events[0][2]['ok'], events[0][2]['reason']),
                         ('triage', False, 'timeout'))


if __name__ == '__main__':
    unittest.main()
