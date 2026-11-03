"""A spec measurably slower than a lower effort of the same model (#91).

The note is advisory: doctor --spec prints it on stderr only, the plan table prints it
once per distinct spec, and neither the spec nor any exit code changes.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402
from forge_dataset.efforts import slow_note  # noqa: E402

DISPATCH = ROOT / 'scripts' / 'forge-dispatch.sh'
CHECK = ROOT / 'scripts' / 'forge-plan-check.py'
BUILTIN = ('haiku:max: no measured gain over haiku:xhigh (first try 7/7 vs 4/4) at about 4x the time '
           '(7.8 vs 1.9 min; #86, 2026-10-28). The spec is unchanged.')
DATASET = ('haiku:max: no measured gain over haiku:xhigh (first try 5/5 vs 5/5) at about 8x the time '
           '(8.0 vs 1.0 min; forge dataset, 10 runs). The spec is unchanged.')
HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / 'data'
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self._registry = os.environ.pop('FORGE_REGISTRY', None)
        self.addCleanup(self._restore_registry)
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.home), 'FORGE_DATASET': 'on'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _restore_registry(self):
        if self._registry is not None:
            os.environ['FORGE_REGISTRY'] = self._registry


class BuiltinNoteTests(Case):
    def test_the_built_in_row_notes_haiku_max_on_the_claude_model(self):
        for spec in ('haiku:max', 'haiku:max:claude', 'haiku:max:openclaude'):
            with self.subTest(spec=spec):
                self.assertEqual(slow_note(spec), BUILTIN)
        # Reading an absent store must not create one, and must not drop the table.
        self.assertFalse((self.home / 'dataset' / 'dataset.sqlite').exists())

    def test_specs_without_a_measured_slowdown_are_silent(self):
        for spec in ('haiku:xhigh', 'sonnet:high', 'haiku', 'not-a-model:max', 'haiku:max:opencode'):
            with self.subTest(spec=spec):
                self.assertIsNone(slow_note(spec))

    def test_a_store_or_registry_error_returns_none(self):
        with patch('forge_dataset.efforts.registry_models', side_effect=OSError('down')):
            self.assertIsNone(slow_note('haiku:max'))
        with patch('forge_dataset.efforts._runs', side_effect=OSError('locked')):
            self.assertIsNone(slow_note('haiku:max'))

    def test_the_cli_prints_the_note_or_nothing(self):
        noted = self.cli('haiku:max')
        self.assertEqual((noted.returncode, noted.stdout, noted.stderr), (0, BUILTIN + '\n', ''))
        silent = self.cli('sonnet:high')
        self.assertEqual((silent.returncode, silent.stdout), (0, ''))

    def cli(self, spec):
        return subprocess.run([sys.executable, '-m', 'forge_dataset.efforts', spec],
                              cwd=ROOT / 'scripts', env=dict(os.environ), capture_output=True,
                              text=True, timeout=30)


class DatasetNoteTests(Case):
    def tasks(self, n, outcomes, durations):
        return [(list(outcomes), list(durations)) for _ in range(n)]

    def seed(self, groups):
        """groups: (effort, [(outcomes, durations per attempt), ...]). One run per effort."""
        events, n = [], 0
        for index, (effort, tasks) in enumerate(groups):
            run, rows, task_events = 'run-%s' % effort, {}, []
            for outcomes, durations in tasks:
                n += 1
                tid = 't%d' % n
                rows[tid] = dict(difficulty='low', dwarf='haiku:' + effort)
                for i, (status, seconds) in enumerate(zip(outcomes, durations), 1):
                    ts = 1000 + n * 10 + i
                    task_events.append(dict(rid='a%s.%d' % (tid, i), kind='attempt', run=run, mode='parallel',
                                            task=tid, attempt=i, ts=ts, data=dict(status=status)))
                    task_events.append(dict(rid='d%s.%d' % (tid, i), kind='dispatch', run=run, mode='parallel',
                                            task=tid, attempt=i, ts=ts,
                                            data=dict(role='dwarf', model='haiku', effort=effort, duration_s=seconds)))
            events.append(dict(rid='p-%s' % run, kind='plan', run=run, mode='parallel', ts=1000 + index,
                               data=dict(tasks=rows)))
            events.extend(task_events)
        self.assertEqual(store.record(events, repo=self.repo), len(events))

    def test_enough_data_that_meets_the_rule_names_the_dataset_counts(self):
        # xhigh takes two attempts of 60s: the median is runtime_s/attempts (1.0 min), not the sum.
        self.seed([
            ('max', self.tasks(5, ['PASS'], [480])),
            ('xhigh', self.tasks(5, ['PASS', 'PASS'], [60, 60])),
        ])
        for spec in ('haiku:max', 'haiku:max:claude', 'haiku:max:openclaude'):
            with self.subTest(spec=spec):
                self.assertEqual(slow_note(spec), DATASET)
        self.assertIsNone(slow_note('haiku:max:opencode'))

    def test_enough_data_that_is_faster_or_better_overrides_the_table(self):
        self.seed([
            ('max', self.tasks(5, ['PASS'], [60])),
            ('xhigh', self.tasks(5, ['PASS'], [120])),
        ])
        self.assertIsNone(slow_note('haiku:max'))

        fresh = self.root / 'better'
        os.environ['FORGE_DATA_HOME'] = str(fresh)
        self.seed([
            ('max', self.tasks(5, ['PASS'], [480])),
            ('xhigh', self.tasks(2, ['PASS'], [60]) + self.tasks(3, ['FAIL'], [60])),
        ])
        self.assertIsNone(slow_note('haiku:max'))

    def test_four_records_on_one_side_falls_back_to_the_table(self):
        self.seed([
            ('max', self.tasks(5, ['PASS'], [480])),
            ('xhigh', self.tasks(4, ['PASS'], [60])),
        ])
        self.assertEqual(slow_note('haiku:max'), BUILTIN)

    def test_dataset_off_keeps_the_table_even_when_the_rows_would_override_it(self):
        self.seed([
            ('max', self.tasks(5, ['PASS'], [60])),
            ('xhigh', self.tasks(5, ['PASS'], [120])),
        ])
        os.environ['FORGE_DATASET'] = 'off'
        self.assertEqual(slow_note('haiku:max'), BUILTIN)


class DoctorNoteTests(Case):
    def setUp(self):
        super().setUp()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        cli = self.bin / 'claude'
        cli.write_text('#!/bin/sh\necho --help -p --model --add-dir --output-format --verbose '
                       '--effort --permission-mode --allowedTools --disallowed-tools '
                       '--dangerously-skip-permissions\n')
        cli.chmod(0o755)

    def doctor(self, spec):
        run = self.root / ('doctor-' + spec.replace(':', '_'))
        run.mkdir()
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                   FORGE_RIPWIRE='off', FORGE_JEV='off', FORGE_MEMORY='off')
        result = subprocess.run(
            ['bash', str(DISPATCH), 'doctor', '--spec', spec, '--run-dir', str(run)],
            cwd=self.repo, env=env, text=True, capture_output=True, timeout=60)
        return result, run

    def test_doctor_prints_the_note_on_stderr_only(self):
        noted, run = self.doctor('haiku:max')
        plain, _ = self.doctor('haiku:xhigh')
        self.assertEqual(noted.returncode, 0, noted.stderr + noted.stdout)
        self.assertEqual(noted.returncode, plain.returncode)
        self.assertEqual(noted.stderr, 'forge: note: ' + BUILTIN + '\n')
        self.assertNotIn('forge: note:', plain.stderr)
        self.assertIn('offline preflight passed', noted.stdout)
        self.assertNotIn('no measured gain', noted.stdout)
        self.assertNotIn('forge: note:', noted.stdout)
        resolved = (run / 'dwarf.resolved').read_text()
        self.assertIn('effort=max\n', resolved)
        self.assertIn('model=haiku\n', resolved)
        self.assertNotIn('no measured gain', resolved)
        self.assertNotIn('note', resolved)

    def test_a_dispatch_does_not_print_the_note(self):
        run = self.root / 'dispatch-run'
        run.mkdir()
        prompt = self.root / 'prompt.md'
        prompt.write_text('do the thing\n')
        env = dict(os.environ, FORGE_RIPWIRE='off')
        result = subprocess.run(
            ['bash', str(DISPATCH), 'dwarf', 'haiku:max', '--dry-run', '--repo', str(self.repo),
             '--run-dir', str(run), '--prompt-file', str(prompt)],
            cwd=self.repo, env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn('forge: note:', result.stderr)
        self.assertNotIn('no measured gain', result.stdout)
        resolved = (run / 'dwarf.resolved').read_text()
        self.assertIn('effort=max\n', resolved)
        self.assertNotIn('no measured gain', resolved)


class PlanNoteTests(Case):
    def row(self, task, dwarf, qa='opus'):
        return '\t'.join([task, '-', 'low', 'a.py', dwarf, qa, 'Title of ' + task])

    def gates(self, text):
        plan = self.root / 'plan'
        plan.mkdir()
        path = plan / 'tasks.tsv'
        path.write_text(text)
        before = path.read_bytes()
        done = subprocess.run([sys.executable, str(CHECK), 'gates', str(plan)],
                              capture_output=True, text=True, env=dict(os.environ), timeout=60)
        self.assertEqual(path.read_bytes(), before)
        return done

    def test_gates_prints_the_note_once_for_two_slow_tasks(self):
        text = HEADER + self.row('a', 'haiku:max', 'haiku:max') + '\n' + self.row('b', 'haiku:max') + '\n'
        done = self.gates(text)
        self.assertEqual((done.returncode, done.stdout), (0, 'note: ' + BUILTIN + '\n'), done.stderr)
        self.assertEqual(done.stdout.count('note:'), 1)
        self.assertFalse((self.home / 'dataset' / 'dataset.sqlite').exists())

    def test_a_plan_without_a_slow_spec_prints_nothing(self):
        done = self.gates(HEADER + self.row('a', 'sonnet:high') + '\n')
        self.assertEqual((done.returncode, done.stdout), (0, ''), done.stderr)


if __name__ == '__main__':
    unittest.main()
