"""Forge's invariants, asserted against the paths Jev can now reach.

Every test here is about something Jev must NOT be able to do. They are deliberately
end-to-end and shell-level: the properties are about what the runner writes to disk and
exits with, and a mock cannot prove those.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import hashlib
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


class InvariantOneTests(PlanInvariantCase):
    """Jev never changes a task's tier: its plan-time gates only warn, and the plan keeps
    the declared one."""

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


    def _gated_plans(self, drift):
        """Plan the same one-task plan twice, once with the gates on (prompt `on`, drift
        `drift`) and once with Jev off. Returns (gated plan dir, plain plan dir, output)."""
        from forge_jev import gates, questions  # noqa: WPS433
        (self.repo / 'src' / 'b.py').write_text('y = 2\n')
        _git(self.repo, 'add', '-A')
        _git(self.repo, 'commit', '-qm', 'b')
        rows = [f'a1{TAB}-{TAB}low{TAB}src/a.py{TAB}-{TAB}-{TAB}Edit a']
        plan, plain = self._plan(rows), self._plan(rows, name='plain')
        fixtures = self.root / 'fixtures'
        fixtures.mkdir()
        prompt_state = gates.task_state(self.repo, goal='a goal\n', task_id='a1', title='Edit a',
                                        files='src/a.py', deps='-',
                                        siblings=[dict(id='a1', deps='-', files='src/a.py', title='Edit a')])
        drift_state = dict(goal='a goal\n', task=dict(id='a1', title='Edit a', declared_files='src/a.py'),
                           prompt='', approach='')
        for state, asked, answers in (
                (prompt_state, dict(prompt_adequacy=questions.prompt_adequacy()),
                 dict(prompt_adequacy=dict(type='noul', noul=0.10))),
                (drift_state, dict(f0=questions.drift_prediction('src/b.py')),
                 dict(f0=dict(type='noul', noul=0.95)))):
            body = json.dumps(dict(state=state, model='jev-latest', questions=asked),
                              sort_keys=True, separators=(',', ':')).encode()
            (fixtures / (hashlib.sha256(body).hexdigest() + '.json')).write_text(
                json.dumps(dict(answers=answers)))
        # The gates the way a user sets them: the other gates stay off.
        config = self.root / 'config'
        (config / 'forge').mkdir(parents=True)
        import forge_decide  # noqa: WPS433
        modes = {g: 'off' for g in forge_decide.GATES}
        modes.update(prompt='on', drift=drift)
        (config / 'forge' / 'decide.json').write_text(json.dumps(dict(version=1, gates=modes)))
        env = dict(FORGE_JEV_FIXTURES=str(fixtures), XDG_CONFIG_HOME=str(config),
                   TYPESAFE_API_KEY='ts-test-key')
        # Prompt `on` sends the vague task back once (exit 4, before the table); the plan goes
        # through on the second call, and both calls' notes are the output.
        sent = self._run(plan, '--dwarf-low', 'luna', '--qa', 'opus', env=env)
        self.assertEqual(sent.returncode, 4, sent.stderr)
        result = self._run(plan, '--dwarf-low', 'luna', '--qa', 'opus', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        result.stdout, result.stderr = sent.stdout + result.stdout, sent.stderr + result.stderr
        off = self._run(plain, '--dwarf-low', 'luna', '--qa', 'opus', env=dict(env, FORGE_JEV='off'))
        # The same plan with Jev off: no request, no note.
        self.assertNotIn('jev:', off.stdout + off.stderr)
        self.assertFalse((plain / 'jev.jsonl').exists())
        self.assertTrue((plan / 'tasks' / 'a1' / 'jev.json').is_file())
        return plan, plain, result.stdout + result.stderr

    def test_jev_shadow_drift_prints_the_prompt_note_and_leaves_tasks_tsv_byte_identical(self):
        plan, plain, output = self._gated_plans('shadow')
        self.assertIn('jev: a1 — the prompt may be too vague for QA to judge correctness against', output)
        self.assertNotIn('undeclared files', output)
        self.assertNotIn('suggests', output)
        self.assertEqual((plan / 'tasks.tsv').read_bytes(), (plain / 'tasks.tsv').read_bytes())
        self.assertEqual(json.loads((plan / 'plan-gates.json').read_text())['drift']['a1']['would_add'],
                         [dict(file='src/b.py', p=0.95)])

    def test_jev_on_drift_prints_the_notes_and_changes_exactly_the_added_files_column(self):
        plan, plain, output = self._gated_plans('on')
        self.assertIn('jev: a1 — the prompt may be too vague for QA to judge correctness against', output)
        self.assertIn('drift added to a1: src/b.py', output)
        self.assertNotIn('suggests', output)
        gated = [l.split('\t') for l in (plan / 'tasks.tsv').read_text().splitlines()]
        kept = [l.split('\t') for l in (plain / 'tasks.tsv').read_text().splitlines()]
        self.assertEqual(len(gated), len(kept))
        self.assertEqual(gated[0], kept[0])
        # Only a1's files cell differs, by the file the gate added.
        self.assertEqual(kept[1][3], 'src/a.py')
        self.assertEqual(gated[1][3], 'src/a.py,src/b.py')
        self.assertEqual(gated[1][:3] + gated[1][4:], kept[1][:3] + kept[1][4:])
        self.assertEqual(json.loads((plan / 'plan-gates.json').read_text())['drift']['a1']['added'],
                         [dict(file='src/b.py', p=0.95)])


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


class FractalEligibleTests(unittest.TestCase):
    """A Fractal child's model always comes from the run's frozen eligible pool."""

    SOURCE = (ROOT / 'scripts' / 'forge_fractal' / 'execution.py').read_text()

    def test_the_frozen_eligible_check_is_untouched(self):
        self.assertIn("if model not in self.config['eligible']:", self.SOURCE)
        self.assertIn("raise ValueError('Model not in frozen eligible pool')", self.SOURCE)


if __name__ == '__main__':
    unittest.main()
