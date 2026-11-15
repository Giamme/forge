"""`forge handoff note` and `forge handoff claim`: session notes and the driver record (#146).

Plan dirs are temporary. FORGE_DATASET is off so a take-over's audit does not open a dataset.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-handoff.py'
CLAIM_KEYS = {'driver', 'took_over', 'previous'}
DRIVER_KEYS = {'harness', 'session', 'since', 'last_seen'}
LOG_KEYS = {'at', 'actor', 'action', 'task', 'key', 'value', 'previous'}

_HANDOFF = None


def handoff_module():
    """The script, loaded once so a test can patch DRIVER_STALE_S in process."""
    global _HANDOFF
    if _HANDOFF is None:
        spec = importlib.util.spec_from_file_location('forge_handoff_under_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _HANDOFF = module
    return _HANDOFF


class HandoffDriverTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.env = dict(os.environ, FORGE_DATASET='off')

    def make(self, name='plan'):
        plan = self.root / name
        plan.mkdir()
        (plan / 'tasks.tsv').write_text(
            '# id\tdeps\tfiles\tdifficulty\tdwarf\tqa\ttitle\n'
            'a\t-\ta.txt\tmedium\t-\t-\tA note\n')
        return plan

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            capture_output=True, text=True, env=self.env, cwd=str(self.root), timeout=60)

    def claim(self, plan, *args):
        return self.run_cli('claim', plan, *args)

    def put_driver(self, plan, harness, session, since, last_seen):
        record = {'harness': harness, 'session': session, 'since': since, 'last_seen': last_seen}
        text = json.dumps(record) + '\n'
        (plan / 'handoff.driver.json').write_text(text)
        return json.loads(text)

    def driver(self, plan):
        return json.loads((plan / 'handoff.driver.json').read_text())

    def log_rows(self, plan):
        path = plan / 'control.log'
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def frozen(self, path):
        if not path.exists():
            return None
        info = path.stat()
        return (info.st_mtime_ns, path.read_bytes())

    def assert_claim_json(self, data, took_over, previous_harness):
        self.assertEqual(set(data), CLAIM_KEYS)
        self.assertEqual(set(data['driver']), DRIVER_KEYS)
        self.assertIs(data['took_over'], took_over)
        if previous_harness is None:
            self.assertIsNone(data['previous'])
        else:
            self.assertEqual(set(data['previous']), DRIVER_KEYS)
            self.assertEqual(data['previous']['harness'], previous_harness)

    def assert_takeover(self, row, value, previous):
        self.assertEqual(set(row), LOG_KEYS)
        self.assertEqual(row['actor'], 'cli')
        self.assertEqual(row['action'], 'take-over')
        self.assertIsNone(row['task'])
        self.assertEqual(row['key'], 'driver')
        self.assertEqual(row['value'], value)
        self.assertEqual(row['previous'], previous)
        self.assertIsInstance(row['at'], float)

    # ------------------------------------------------------------------------------------ note

    def test_a_note_is_appended_and_show_prints_newest_first(self):
        plan = self.make()
        first = self.run_cli('note', plan, 'user   accepted\ttask\nX')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, 'noted\n')
        second = self.run_cli('note', plan, 'hand fix', '--actor', 'ada', '--json')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, '{"noted": true}\n')
        # A long run of whitespace is collapsed before the length check, so this is a short note.
        third = self.run_cli('note', plan, 'a' + (' ' * 5000) + 'b')
        self.assertEqual(third.returncode, 0, third.stderr)

        rows = [json.loads(line) for line in (plan / 'handoff.notes.jsonl').read_text().splitlines()]
        self.assertEqual([row['text'] for row in rows], ['user accepted task X', 'hand fix', 'a b'])
        self.assertEqual([row['actor'] for row in rows], ['cli', 'ada', 'cli'])
        self.assertIsInstance(rows[0]['at'], float)
        self.assertGreaterEqual(rows[1]['at'], rows[0]['at'])
        self.assertGreaterEqual(rows[2]['at'], rows[1]['at'])

        shown = json.loads(self.run_cli(plan, '--json').stdout)
        self.assertEqual([(note['actor'], note['text']) for note in shown['notes']],
                         [('cli', 'a b'), ('ada', 'hand fix'), ('cli', 'user accepted task X')])
        text = self.run_cli('show', plan).stdout
        self.assertLess(text.index('a b'), text.index('hand fix'))
        self.assertLess(text.index('hand fix'), text.index('user accepted task X'))
        self.assertFalse((plan / 'control.log').exists())

    def test_an_empty_or_overlong_note_is_refused(self):
        plan = self.make()
        missing = plan / 'handoff.notes.jsonl'
        for text in ('', '   \n\t', ' ' * 5000):
            result = self.run_cli('note', plan, text)
            self.assertEqual(result.returncode, 2, repr(text) + result.stderr)
            self.assertFalse(missing.exists())
            self.assertFalse((plan / 'control.log').exists())

        edge = '\u00e9' * 2000
        ok = self.run_cli('note', plan, '  ' + edge + '\n')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(missing.read_text())['text'], edge)
        before = missing.read_bytes()
        for text in ('', ' \n ', '\u00e9' * 2001, 'x' * 2001):
            result = self.run_cli('note', plan, text)
            self.assertEqual(result.returncode, 2, repr(text[:20]) + result.stderr)
            self.assertEqual(missing.read_bytes(), before)
        self.assertFalse((plan / 'control.log').exists())

    # ------------------------------------------------------------------------------------ claim

    def test_the_first_claim_writes_the_driver(self):
        plan = self.make()
        started = time.time()
        result = self.claim(plan, '--harness', 'claude-code', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assert_claim_json(data, False, None)
        self.assertEqual(data['driver']['harness'], 'claude-code')
        self.assertIsNone(data['driver']['session'])
        self.assertEqual(data['driver']['since'], data['driver']['last_seen'])
        self.assertGreaterEqual(data['driver']['since'], started)
        self.assertEqual(self.driver(plan), data['driver'])
        shown = json.loads(self.run_cli(plan, '--json').stdout)
        self.assertEqual(shown['driver'], data['driver'])
        self.assertFalse((plan / 'control.log').exists())
        # --take-over with nobody to replace is still a first claim: no audit line.
        other = self.make('other')
        again = self.claim(other, '--harness', 'codex', '--session', 's', '--take-over', '--json')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertFalse(json.loads(again.stdout)['took_over'])
        self.assertFalse((other / 'control.log').exists())

    def test_a_reclaim_keeps_since_and_refreshes_last_seen(self):
        plan = self.make()
        old = self.put_driver(plan, 'claude-code', 's1', 1000, 2000)
        result = self.claim(plan, '--harness', 'claude-code', '--session', 's1', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assert_claim_json(data, False, 'claude-code')
        self.assertEqual(data['previous']['since'], 1000)
        self.assertEqual(data['previous']['last_seen'], 2000)
        self.assertEqual(data['previous']['session'], 's1')
        self.assertEqual(data['driver']['since'], old['since'])
        self.assertGreater(data['driver']['last_seen'], old['last_seen'])
        self.assertEqual(self.driver(plan), data['driver'])
        self.assertFalse((plan / 'control.log').exists())

    def test_a_different_driver_is_refused_and_nothing_changes(self):
        plan = self.make()
        now = time.time()
        current = self.put_driver(plan, 'claude-code', 's1', now - 10, now)
        log = plan / 'control.log'
        log.write_bytes(b'{"kept":true}\n')
        driver_before = self.frozen(plan / 'handoff.driver.json')
        log_before = self.frozen(log)
        for harness, session in (('codex', 's1'), ('claude-code', 's2')):
            result = self.claim(plan, '--harness', harness, '--session', session, '--json')
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertIn('claude-code/s1', result.stderr)
            self.assertIn(str(current['last_seen']), result.stderr)
            self.assertEqual(self.frozen(plan / 'handoff.driver.json'), driver_before)
            self.assertEqual(self.frozen(log), log_before)

    def test_take_over_appends_one_control_log_line(self):
        plan = self.make()
        seen = time.time()
        old = self.put_driver(plan, 'claude-code', 's1', 1000, seen)
        prior = '{"at": 1, "actor": "ui", "action": "set", "task": null, "key": "timeout", "value": "9", "previous": null}\n'
        (plan / 'control.log').write_text(prior)
        result = self.claim(plan, '--harness', 'codex', '--session', 's2', '--take-over', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assert_claim_json(data, True, 'claude-code')
        self.assertEqual(data['previous']['session'], 's1')
        self.assertEqual(data['previous']['since'], 1000)
        self.assertEqual(data['previous']['last_seen'], old['last_seen'])
        self.assertEqual((data['driver']['harness'], data['driver']['session']), ('codex', 's2'))
        self.assertEqual(data['driver']['since'], data['driver']['last_seen'])
        self.assertNotEqual(data['driver']['since'], 1000)
        self.assertEqual(self.driver(plan), data['driver'])
        text = (plan / 'control.log').read_text()
        self.assertTrue(text.startswith(prior))
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(rows), 2)
        self.assert_takeover(rows[1], 'codex/s2', 'claude-code/s1')

    def test_a_stale_driver_is_replaced_without_take_over(self):
        held = self.make('held')
        age = time.time() - 11 * 3600
        self.put_driver(held, 'claude-code', 's1', age, age)
        before = (held / 'handoff.driver.json').read_bytes()
        refused = self.claim(held, '--harness', 'codex', '--session', 's2', '--json')
        self.assertEqual(refused.returncode, 3, refused.stderr)
        self.assertEqual((held / 'handoff.driver.json').read_bytes(), before)
        self.assertFalse((held / 'control.log').exists())

        plan = self.make('stale')
        seen = time.time() - 13 * 3600
        old = self.put_driver(plan, 'claude-code', 's1', seen - 50, seen)
        started = time.time()
        result = self.claim(plan, '--harness', 'codex', '--session', 's2', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assert_claim_json(data, True, 'claude-code')
        self.assertEqual(data['previous']['last_seen'], old['last_seen'])
        self.assertEqual(data['driver']['since'], data['driver']['last_seen'])
        self.assertGreaterEqual(data['driver']['since'], started)
        self.assertEqual(self.driver(plan)['harness'], 'codex')
        rows = self.log_rows(plan)
        self.assertEqual(len(rows), 1)
        self.assert_takeover(rows[0], 'codex/s2', 'claude-code/s1')

    def test_the_stale_window_is_driver_stale_s(self):
        module = handoff_module()
        old_window = module.DRIVER_STALE_S
        previous = os.environ.get('FORGE_DATASET')
        os.environ['FORGE_DATASET'] = 'off'
        try:
            module.DRIVER_STALE_S = 60
            fresh = self.make('within')
            seen = time.time() - 120
            self.put_driver(fresh, 'claude-code', 's1', seen - 5, seen)
            code, out, err = self.inproc(module, ['claim', str(fresh), '--harness', 'codex', '--session', 's2', '--json'])
            self.assertEqual(code, 0, err)
            self.assertTrue(json.loads(out)['took_over'])
            self.assert_takeover(self.log_rows(fresh)[0], 'codex/s2', 'claude-code/s1')

            module.DRIVER_STALE_S = 10_000
            blocked = self.make('blocked')
            seen = time.time() - 120
            self.put_driver(blocked, 'claude-code', 's1', seen - 5, seen)
            before = (blocked / 'handoff.driver.json').read_bytes()
            code, out, err = self.inproc(module, ['claim', str(blocked), '--harness', 'codex', '--session', 's2', '--json'])
            self.assertEqual(code, 3, err)
            self.assertEqual(out, '')
            self.assertIn('claude-code/s1', err)
            self.assertEqual((blocked / 'handoff.driver.json').read_bytes(), before)
            self.assertFalse((blocked / 'control.log').exists())
        finally:
            module.DRIVER_STALE_S = old_window
            if previous is None:
                os.environ.pop('FORGE_DATASET', None)
            else:
                os.environ['FORGE_DATASET'] = previous

    def test_a_malformed_driver_is_treated_as_absent(self):
        now = time.time()
        samples = [
            '{broken',
            '[]\n',
            'null\n',
            json.dumps({'harness': 'codex', 'session': 's', 'since': True, 'last_seen': True}) + '\n',
            json.dumps({'harness': 'NOPE', 'session': 's', 'since': now, 'last_seen': now}) + '\n',
            '{"harness": "codex"}\n',
            json.dumps({'harness': 'codex', 'session': 'a b', 'since': now, 'last_seen': now}) + '\n',
        ]
        plan = self.make()
        for sample in samples:
            (plan / 'handoff.driver.json').write_text(sample)
            result = self.claim(plan, '--harness', 'claude-code', '--session', 'new', '--json')
            self.assertEqual(result.returncode, 0, sample + result.stderr)
            data = json.loads(result.stdout)
            self.assert_claim_json(data, False, None)
            self.assertEqual((data['driver']['harness'], data['driver']['session']), ('claude-code', 'new'))
            self.assertEqual(self.driver(plan), data['driver'])
            self.assertFalse((plan / 'control.log').exists())

    def test_harness_and_session_are_checked(self):
        plan = self.make()
        missing = self.claim(plan)
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertFalse((plan / 'handoff.driver.json').exists())

        ok = self.claim(plan, '--harness', 'a' * 40, '--session', 's' * 200, '--json')
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(self.driver(plan)['harness'], 'a' * 40)
        self.assertEqual(self.driver(plan)['session'], 's' * 200)
        good = (plan / 'handoff.driver.json').read_bytes()
        cases = (
            ('--harness', 'A', '--session', 's'),
            ('--harness', 'a' * 41, '--session', 's'),
            ('--harness', 'codex/x', '--session', 's'),
            ('--harness', 'a.b_c-9', '--session', 'has space'),
            ('--harness', 'a.b_c-9', '--session', 's' * 201),
            ('--harness', 'a.b_c-9', '--session', ''),
        )
        for args in cases:
            result = self.claim(plan, *args)
            self.assertEqual(result.returncode, 2, args + (result.stderr,))
            self.assertEqual((plan / 'handoff.driver.json').read_bytes(), good)
        self.assertFalse((plan / 'control.log').exists())
        # The same shape, one change back to a legal harness, is accepted on a free run.
        free = self.make('free')
        legal = self.claim(free, '--harness', 'a.b_c-9', '--session', 'ok', '--json')
        self.assertEqual(legal.returncode, 0, legal.stderr)
        self.assertEqual(self.driver(free)['harness'], 'a.b_c-9')

    def test_a_path_that_is_not_a_run_is_refused(self):
        empty = self.root / 'empty'
        empty.mkdir()
        for args in (('note', empty, 'hello'), ('claim', empty, '--harness', 'codex')):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(list(empty.iterdir()), [])

    def inproc(self, module, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = module.main(args)
        return code, out.getvalue(), err.getvalue()


if __name__ == '__main__':
    unittest.main()
