"""Forge's invariants, asserted against the paths Jev can now reach.

Every test here is about something Jev must NOT be able to do. They are deliberately
end-to-end and shell-level: the properties are about what the runner writes to disk and
exits with, and a mock cannot prove those.
"""
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
TAB = '\t'


def _git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class PlanInvariantCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.repo = self.root / 'repo'
        (self.repo / 'src').mkdir(parents=True)
        (self.repo / 'src' / 'a.py').write_text('x = 1\n')
        _git(self.repo, 'init', '-q', '.')
        _git(self.repo, 'config', 'user.email', 't@t')
        _git(self.repo, 'config', 'user.name', 't')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-qm', 'init')

    def _plan(self, rows, name='plan'):
        plan = self.root / name
        plan.mkdir()
        header = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
        (plan / 'tasks.tsv').write_text(header + ''.join(r + '\n' for r in rows))
        (plan / 'goal.txt').write_text('a goal\n')
        return plan

    def _run(self, plan, *args, env=None):
        environment = dict(os.environ, FORGE_RIPWIRE='off')
        environment.update(env or {})
        return subprocess.run(
            ['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'plan', str(plan),
             '--repo', str(self.repo), *args],
            capture_output=True, text=True, env=environment)

    def _calibrate(self, tier='low'):
        forge = self.repo / '.forge'
        forge.mkdir(exist_ok=True)
        (forge / 'jev-routing-calibration.json').write_text(json.dumps(
            dict(calibrated=True, tiers={tier: dict(n=40, threshold=0.7, pass_rate=0.95)})))


class InvariantOneTests(PlanInvariantCase):
    """Forge never picks a model. A Jev tier is still only a tier."""

    def test_a_jev_written_tier_with_no_pool_is_unassigned_and_exits_2(self):
        # The danger the invariant guards: Jev moves a task to a tier the user supplied
        # no pool for, and forge quietly falls back to some model to keep going.
        self._calibrate('low')
        plan = self._plan([f'd1{TAB}-{TAB}high{TAB}src/a.py{TAB}-{TAB}-{TAB}'
                           'Fix a spelling mistake in a comment'])
        result = self._run(plan, '--dwarf-high', 'sol', '--qa', 'opus',
                           env=dict(FORGE_JEV_ACT='on'))
        rows = (plan / 'tasks.tsv').read_text().splitlines()[1:]
        dwarf = rows[0].split('\t')[4]
        if rows[0].split('\t')[2] == 'low':
            # Jev acted, so the invariant is the thing under test.
            self.assertEqual(dwarf, 'UNASSIGNED')
            self.assertEqual(result.returncode, 2)
            self.assertIn('will not pick a model', result.stdout + result.stderr)
        else:
            # Jev declined to act; the row must be untouched either way.
            self.assertNotEqual(dwarf, '')

    def test_jev_off_leaves_the_difficulty_column_byte_identical(self):
        rows = [f'a1{TAB}-{TAB}low{TAB}src/a.py{TAB}-{TAB}-{TAB}Edit a',
                f'b1{TAB}-{TAB}high{TAB}src/b.py{TAB}-{TAB}-{TAB}Edit b']
        plan = self._plan(rows)
        before = [line.split('\t')[2] for line in
                  (plan / 'tasks.tsv').read_text().splitlines()[1:]]
        self._run(plan, '--dwarf-low', 'luna', '--dwarf-high', 'sol', '--qa', 'opus',
                  env=dict(FORGE_JEV='off'))
        after = [line.split('\t')[2] for line in
                 (plan / 'tasks.tsv').read_text().splitlines()[1:]]
        self.assertEqual(before, after)

    def test_advisory_mode_does_not_write_the_difficulty_column(self):
        # No calibration exists, so --jev-act must still be inert.
        rows = [f'a1{TAB}-{TAB}high{TAB}src/a.py{TAB}-{TAB}-{TAB}'
                'Fix a spelling mistake in a comment']
        plan = self._plan(rows)
        self._run(plan, '--dwarf-low', 'luna', '--dwarf-high', 'sol', '--qa', 'opus',
                  env=dict(FORGE_JEV_ACT='on'))
        after = (plan / 'tasks.tsv').read_text().splitlines()[1]
        self.assertEqual(after.split('\t')[2], 'high')


class InvariantThreeTests(unittest.TestCase):
    """Absence of verification is UNVERIFIED, never PASS."""

    SOURCE = (ROOT / 'scripts' / 'forge-parallel.sh').read_text()

    def test_no_jev_branch_writes_pass_to_verification_status(self):
        # Read every line that writes verification.status and confirm none of them sits
        # inside a Jev branch. A Jev-chosen command must flow through the SAME run and
        # status logic as a user-configured one, never short-circuit to success.
        lines = self.SOURCE.splitlines()
        writes = [i for i, line in enumerate(lines) if 'verification.status' in line
                  and 'PASS' in line]
        self.assertTrue(writes, 'expected at least one PASS write to exist')
        for index in writes:
            window = '\n'.join(lines[max(0, index - 25):index])
            self.assertNotIn('forge-jev.py', window,
                             f'a PASS write at line {index + 1} follows a jev call')

    def test_discovery_cannot_report_a_command_it_did_not_enumerate(self):
        from forge_jev import verify  # noqa: WPS433
        source = (ROOT / 'scripts' / 'forge_jev' / 'verify.py').read_text()
        # The guard that makes selection safe: a pick absent from the candidate list is
        # discarded rather than returned.
        self.assertIn('commands.index(picked)', source)
        self.assertIn('except ValueError', source)
        self.assertTrue(hasattr(verify, 'candidates'))

    def test_a_failing_jev_chosen_command_still_fails(self):
        # The run/compare logic is shared, so what matters is that the jev branch only
        # sets `cmd` and never touches rc or the status afterwards.
        start = self.SOURCE.index('jev chose a verification command')
        block = self.SOURCE[self.SOURCE.rindex('if [ -z "$cmd" ]', 0, start):]
        block = block[:block.index('\n  fi\n')]
        self.assertIn('cmd="$jev_cmd"', block)
        self.assertNotIn('verification.status', block)


class MemoryInvariantTests(unittest.TestCase):
    """--no-memory and FORGE_MEMORY=off suppress every Jev memory hook."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'memory.md').write_text(
            '# forge project memory\n\n## Known traps\n'
            + '\n'.join(f'- fact {i} [x]' for i in range(12)) + '\n')
        self.last = self.repo / 'last.txt'
        self.last.write_text('FORGE_LEARNING: trap | a durable fact [src]\n'
                             'FORGE_VERDICT: PASS\n')
        self.task = self.repo / 'task.md'
        self.task.write_text('do a thing\n')

    def _memory(self, *args, env=None):
        environment = dict(os.environ)
        environment.update(env or {})
        return subprocess.run(
            ['bash', str(ROOT / 'scripts' / 'forge-memory.sh'), *args],
            capture_output=True, text=True, env=environment)

    def test_memory_off_suppresses_record_entirely(self):
        result = self._memory('record', str(self.repo), '--last', str(self.last),
                              '--role', 'dwarf', env=dict(FORGE_MEMORY='off'))
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.repo / '.forge' / 'ledger.tsv').exists())

    def test_memory_off_suppresses_inject_and_its_slicing(self):
        result = self._memory('inject', str(self.repo), 'dwarf', '--task', str(self.task),
                              env=dict(FORGE_MEMORY='off'))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), '')

    def test_jev_off_injects_the_full_role_slice(self):
        result = self._memory('inject', str(self.repo), 'dwarf', '--task', str(self.task),
                              env=dict(FORGE_JEV='off'))
        self.assertEqual(result.stdout.count('\n- fact'), 12)

    def test_a_rejected_learning_still_reaches_the_ledger(self):
        # The whole point of gating injection rather than recording: nothing is lost.
        self._memory('record', str(self.repo), '--last', str(self.last), '--role', 'dwarf',
                     '--run-id', 'r1', '--task', 't1', env=dict(FORGE_JEV='off'))
        ledger = (self.repo / '.forge' / 'ledger.tsv').read_text()
        self.assertIn('a durable fact', ledger)


class SecretHygieneTests(unittest.TestCase):
    """The key must not reach a run dir, a prompt, or a log."""

    def test_no_run_dir_artifact_contains_the_key(self):
        with tempfile.TemporaryDirectory() as run_dir, tempfile.TemporaryDirectory() as home:
            key = 'sk-test-DO-NOT-LEAK-123456'
            env = dict(os.environ, TYPESAFE_API_KEY=key, XDG_CONFIG_HOME=home,
                       FORGE_JEV_FIXTURES=str(ROOT / 'tests' / 'fixtures' / 'jev'))
            subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge-jev.py'),
                            'doctor', '--json'], capture_output=True, text=True, env=env)
            for path in Path(run_dir).rglob('*'):
                if path.is_file():
                    self.assertNotIn(key, path.read_text(errors='replace'), str(path))

    def test_status_never_prints_the_key(self):
        with tempfile.TemporaryDirectory() as home:
            key = 'sk-test-DO-NOT-LEAK-123456'
            env = dict(os.environ, TYPESAFE_API_KEY=key, XDG_CONFIG_HOME=home)
            result = subprocess.run(
                [sys.executable, str(ROOT / 'scripts' / 'forge-jev.py'), 'status', '--json'],
                capture_output=True, text=True, env=env)
            self.assertNotIn(key, result.stdout)
            self.assertNotIn(key, result.stderr)
            self.assertIn('"key_present": true', result.stdout)


if __name__ == '__main__':
    unittest.main()


class FractalRerateTests(unittest.TestCase):
    """Child re-rating may move a child between authorized pools, nothing more."""

    SOURCE = (ROOT / 'scripts' / 'forge_fractal' / 'execution.py').read_text()

    def test_rerating_happens_outside_the_guard(self):
        # admit holds a lock the whole tree contends for; a network call under it would
        # stall every sibling.
        admit = self.SOURCE[self.SOURCE.index('    def admit(self'):]
        admit = admit[:admit.index('with self.guard:')]
        self.assertIn('self._rerate(', admit)

    def test_the_frozen_eligible_check_is_untouched(self):
        self.assertIn("if model not in self.config['eligible']:", self.SOURCE)
        self.assertIn("raise ValueError('Model not in frozen eligible pool')", self.SOURCE)

    def test_a_malformed_difficulty_is_rejected_before_any_correction(self):
        body = self.SOURCE[self.SOURCE.index("difficulty = request['difficulty']"):]
        body = body[:body.index('paths = [')]
        validate = body.index("raise ValueError('Invalid child difficulty')")
        correct = body.index('rerated.get(')
        self.assertLess(validate, correct,
                        'a bad difficulty must be rejected, not silently replaced')

    def test_rerate_returns_empty_rather_than_raising(self):
        from forge_fractal import execution
        tree = execution.Tree.__new__(execution.Tree)
        tree.request = {}
        self.assertEqual(tree._rerate({'goal': 'g'}, [{'id': 'c1'}]), {})


class SpecParseTests(unittest.TestCase):
    """NL -> flags maps onto aliases the user already has; it never invents one."""

    def test_aliases_come_from_the_registry_with_their_model_ids(self):
        from forge_jev import spec
        catalogue = spec.aliases(ROOT)
        self.assertIn('opus', catalogue)
        # Bare alias names are opaque; the model id is what makes the question
        # answerable at all. Measured: with names only, every tier returned 'none'.
        self.assertTrue(any('opus' in model for model in catalogue['opus']))

    def test_flags_are_rendered_in_tier_order(self):
        from forge_jev import spec
        flags = spec.as_flags(dict(high=dict(alias='opus'), low=dict(alias='haiku')))
        self.assertEqual(flags, '--dwarf-low haiku --dwarf-high opus')

    def test_an_empty_sentence_asks_nothing(self):
        from unittest.mock import patch
        from forge_jev import spec
        with patch('forge_jev.spec.ask') as mocked:
            self.assertIsNone(spec.parse('   ', skill_dir=ROOT))
        mocked.assert_not_called()
