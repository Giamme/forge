"""A spec measurably slower than a lower effort of the same model (#91).

The note is advisory: doctor --spec prints it on stderr only, plan prints it under the
table once per distinct spec, and neither the spec nor any exit code changes.
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
import test_forge as fixtures  # noqa: E402

DISPATCH = ROOT / 'scripts' / 'forge-dispatch.sh'
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

    def test_a_nearer_effort_that_fails_the_rule_does_not_hide_a_farther_one(self):
        # xhigh is 1.3× (the rule needs 2×) at the same pass rate. high is 8×.
        # Enough data on the nearer pair must not swallow the farther one.
        self.seed([
            ('max', self.tasks(5, ['PASS'], [480])),
            ('xhigh', self.tasks(5, ['PASS'], [360])),
            ('high', self.tasks(5, ['PASS'], [60])),
        ])
        self.assertEqual(
            slow_note('haiku:max'),
            'haiku:max: no measured gain over haiku:high (first try 5/5 vs 5/5) '
            'at about 8x the time (8.0 vs 1.0 min; forge dataset, 10 runs). '
            'The spec is unchanged.')

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

    def doctor(self, spec, registry=None, name=None, data_home=None, home=None):
        run = self.root / ('doctor-' + (name or spec.replace(':', '_')))
        run.mkdir()
        env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ.get('PATH', ''),
                   FORGE_RIPWIRE='off', FORGE_JEV='off', FORGE_MEMORY='off')
        if registry is not None:
            env['FORGE_REGISTRY'] = registry
        if data_home is not None:
            env['FORGE_DATA_HOME'] = data_home
        if home is not None:
            env['HOME'] = home
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

    def test_a_relative_registry_still_prints_the_note(self):
        # The note is computed after a cd into scripts/. A relative FORGE_REGISTRY is
        # the caller's path; resolving it from scripts/ drops the note while the
        # absolute path of the same file still prints it.
        registry = self.repo / 'regs' / 'mine.tsv'
        registry.parent.mkdir()
        registry.write_text((ROOT / 'registry.tsv').read_text())
        relative, run = self.doctor('haiku:max', registry='regs/mine.tsv', name='relative')
        absolute, _ = self.doctor('haiku:max', registry=str(registry), name='absolute')
        self.assertEqual(absolute.returncode, 0, absolute.stderr + absolute.stdout)
        self.assertEqual(relative.returncode, 0, relative.stderr + relative.stdout)
        self.assertEqual(absolute.stderr, 'forge: note: ' + BUILTIN + '\n')
        self.assertEqual(relative.stderr, absolute.stderr)
        self.assertEqual(relative.returncode, absolute.returncode)
        self.assertNotIn('no measured gain', relative.stdout)
        self.assertNotIn('forge: note:', relative.stdout)
        resolved = (run / 'dwarf.resolved').read_text()
        self.assertIn('effort=max\n', resolved)
        self.assertNotIn('no measured gain', resolved)

    def _measured_override(self, home):
        """Five passing tasks a side: max at 60s is faster than xhigh at 120s, so no note."""
        os.environ['FORGE_DATA_HOME'] = str(home)
        self.seed([
            ('max', self.tasks(5, ['PASS'], [60])),
            ('xhigh', self.tasks(5, ['PASS'], [120])),
        ])

    def _assert_dataset_overrides_the_builtin_warning(self, absolute, relative, runs):
        self.assertEqual(absolute.returncode, 0, absolute.stderr + absolute.stdout)
        self.assertEqual(relative.returncode, 0, relative.stderr + relative.stdout)
        self.assertNotIn('no measured gain', absolute.stderr)
        self.assertNotIn('forge: note:', absolute.stderr)
        self.assertEqual(relative.stderr, absolute.stderr)
        self.assertEqual(relative.returncode, absolute.returncode)
        self.assertNotIn('no measured gain', relative.stdout)
        self.assertNotIn('forge: note:', relative.stdout)
        for run in runs:
            resolved = (run / 'dwarf.resolved').read_text()
            self.assertIn('effort=max\n', resolved)
            self.assertNotIn('no measured gain', resolved)
            self.assertNotIn('note', resolved)

    def test_a_relative_data_home_reads_the_callers_dataset(self):
        # The note is computed after a cd into scripts/. A relative FORGE_DATA_HOME is
        # the caller's path; resolving it from scripts/ misses this store and the
        # built-in 4× warning comes back, while the absolute path of the same directory
        # stays silent.
        self._measured_override(self.home)
        absolute, abs_run = self.doctor('haiku:max', data_home=str(self.home), name='data-absolute')
        relative, rel_run = self.doctor('haiku:max', data_home='../data', name='data-relative')
        self._assert_dataset_overrides_the_builtin_warning(absolute, relative, (abs_run, rel_run))

    def test_a_home_relative_data_home_still_reads_the_callers_dataset(self):
        # ~/… is expanded by Python, not by prefixing $PWD. Prefixing would look for a
        # directory named "~" and miss the store, bringing the built-in warning back.
        home = self.root / 'home'
        self._measured_override(home / 'forge-data')
        absolute, abs_run = self.doctor(
            'haiku:max', data_home=str(home / 'forge-data'), name='tilde-absolute')
        relative, rel_run = self.doctor(
            'haiku:max', data_home='~/forge-data', home=str(home), name='tilde-relative')
        self._assert_dataset_overrides_the_builtin_warning(absolute, relative, (abs_run, rel_run))

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
    """The notes under plan's table, with the fake agent CLI from test_forge.py."""

    def setUp(self):
        super().setUp()
        fixtures.ForgeTests.setUp(self)
        self.env.pop('FORGE_REGISTRY', None)

    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script

    def row(self, task, dwarf, qa='opus'):
        return '\t'.join([task, '-', 'low', task + '.txt', dwarf, qa, 'Title of ' + task])

    def plan(self, *rows):
        plan = self.root / 'plan'
        plan.mkdir()
        (plan / 'tasks.tsv').write_text(HEADER + ''.join(row + '\n' for row in rows))
        for row in rows:
            task = plan / 'tasks' / row.split('\t')[0]
            task.mkdir(parents=True)
            (task / 'prompt.md').write_text('Implement change.\n')
        done = self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory', '--explore', '0')
        self.assertEqual(done.returncode, 0, done.stdout)
        return done.stdout

    def test_plan_prints_the_note_once_for_two_slow_tasks(self):
        out = self.plan(self.row('a', 'haiku:max', 'haiku:max'), self.row('b', 'haiku:max'))
        self.assertEqual(out.count('note: ' + BUILTIN + '\n'), 1, out)

    def test_a_plan_without_a_slow_spec_prints_no_note(self):
        out = self.plan(self.row('a', 'sonnet:high'))
        self.assertNotIn('no measured gain', out)


if __name__ == '__main__':
    unittest.main()
