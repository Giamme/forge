"""forge 0.23 removed Fractal (#154): what is left is a clear error and the reading of old runs.

Every `--fractal*` flag and `forge fractal` exit 2 and name the release. A plan or solo run
that was made with Fractal is still listed and read, and `run`, `retry` and a solo re-run
refuse it without dispatching anything. Nothing here needs the removed runtime.

Fixtures are borrowed from test_forge.py (the fake agent CLI), not inherited.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import subprocess
import sys
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_legacy  # noqa: E402

RELEASE = forge_legacy.REMOVED_IN
REFUSED = 'made with Fractal, which forge %s removed' % RELEASE


class RemovedCase(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    plan = fixtures.ForgeTests.plan
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        fixtures.ForgeTests.setUp(self)

    def old(self, directory, enabled=True):
        (directory / forge_legacy.OLD_SELECTION).write_text(json.dumps(dict(enabled=enabled, run='run-' + '0' * 20)))
        return directory


class RemovedFlagTests(RemovedCase):
    def refused(self, result, flag):
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('%s was removed with Fractal in forge %s' % (flag, RELEASE), result.stdout)
        self.assertEqual(self.calls(), [])

    def test_every_fractal_flag_of_run_plan_and_retry_exits_2_naming_the_release(self):
        p = self.plan()
        for command in (('plan', p), ('run', p), ('retry', p, 'a')):
            for flag in ('--fractal', '--no-fractal', '--fractal-auto-decompose', '--fractal-planner'):
                with self.subTest(command=command[0], flag=flag):
                    self.refused(self.run_script('forge-parallel.sh', *command, flag), flag)
        self.assertFalse((p / forge_legacy.OLD_SELECTION).exists())
        self.assertFalse((p / 'tasks' / 'a' / 'status').exists())

    def test_the_solo_runner_refuses_them_the_same_way(self):
        run = self.root / 'solo'
        run.mkdir()
        (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        for flag in ('--fractal', '--no-fractal', '--fractal-max-depth'):
            with self.subTest(flag):
                self.refused(self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol', flag), flag)
        for flag in ('--dwarf-low', '--retry'):   # options a solo run only had under Fractal
            with self.subTest(flag):
                result = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol', flag, 'x')
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn('removed with Fractal in forge %s' % RELEASE, result.stdout)
        self.assertEqual(self.calls(), [])

    def test_forge_fractal_is_a_removed_command(self):
        result = subprocess.run([str(ROOT / 'forge'), 'fractal', 'status'], env=self.env, cwd=self.repo, text=True,
                                capture_output=True)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn('forge fractal was removed with Fractal in forge %s' % RELEASE, result.stderr)
        self.assertNotIn('fractal', subprocess.run([str(ROOT / 'forge'), '--help'], env=self.env, cwd=self.repo, text=True,
                                                   capture_output=True).stdout.lower())

    def test_the_help_of_the_runners_names_no_fractal_option(self):
        for script in ('forge-parallel.sh', 'forge-solo.sh'):
            with self.subTest(script):
                shown = self.run_script(script, '--help')
                self.assertEqual(shown.returncode, 0, shown.stdout)
                self.assertNotIn('fractal', shown.stdout.lower())


class OldRunTests(RemovedCase):
    def test_run_and_retry_refuse_a_plan_made_with_fractal(self):
        p = self.old(self.plan())
        (p / 'tasks' / 'a' / 'status').write_text('FAIL\n')
        (p / 'tasks' / 'a' / 'attempt').write_text('1\n')
        def files():   # the run takes its lock before it looks at the plan: those two files are not the plan's
            return sorted(str(f.relative_to(p)) for f in p.rglob('*') if not f.name.startswith('schedule.'))
        before = files()
        for command in (('run', p), ('run', p, '--retry-failed', '1'), ('retry', p, 'a'), ('retry', p, 'a', '--dwarf', 'opus')):
            with self.subTest(command=command[0]):
                result = self.run_script('forge-parallel.sh', *command)
                self.assertEqual(result.returncode, 3, result.stdout)
                self.assertIn(REFUSED, result.stdout)
                self.assertIn('Start a new run for the work that is left', result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(files(), before)
        self.assertEqual((p / 'tasks' / 'a' / 'status').read_text(), 'FAIL\n')

    def test_a_plan_that_declined_fractal_runs_as_any_plan(self):
        p = self.old(self.plan(), enabled=False)
        result = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn('Fractal', result.stdout)

    def test_a_solo_run_made_with_fractal_is_not_run_again(self):
        run = self.root / 'solo'
        run.mkdir()
        (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        self.old(run)
        result = self.run_script('forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol')
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertIn(REFUSED, result.stdout)
        self.assertEqual(self.calls(), [])


class LegacyModuleTests(unittest.TestCase):
    def test_the_selection_file_decides_and_an_unreadable_one_counts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            self.assertFalse(forge_legacy.was_fractal(run))
            for text, expected in (('{"enabled": true}', True), ('{"enabled": false}', False), ('{}', True),
                                   ('', False), ('{not json', True), ('[]', True)):
                (run / forge_legacy.OLD_SELECTION).write_text(text)
                self.assertIs(forge_legacy.was_fractal(run), expected, text)

    def test_an_old_plans_pools_file_is_still_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp)
            self.assertEqual(forge_legacy.pools_path(plan), plan / 'dwarf-routing.json')
            (plan / 'fractal-routing.json').write_text('{}')
            self.assertEqual(forge_legacy.pools_path(plan), plan / 'fractal-routing.json')
            (plan / 'dwarf-routing.json').write_text('{}')
            self.assertEqual(forge_legacy.pools_path(plan), plan / 'dwarf-routing.json')

    def test_no_fractal_code_is_left_outside_the_old_record_readers(self):
        # The names that may still say it: the removal note and what reads an old run.
        allowed = {'forge', 'scripts/forge_legacy.py', 'scripts/forge-lib-task.sh', 'scripts/forge-parallel.sh',
                   'scripts/forge-solo.sh', 'scripts/forge-lib-plans.sh', 'scripts/forge-lib-decompose.sh', 'scripts/forge-compare.py',
                   'scripts/forge-control.py', 'scripts/forge-handoff.py', 'scripts/forge-du.py', 'scripts/forge-runs.py',
                   'scripts/forge_storage.py', 'scripts/forge_decide/profiles.py', 'scripts/forge_dataset/cli.py',
                   'scripts/forge_dataset/harvest.py', 'scripts/forge_dataset/importer.py',
                   'scripts/forge_dataset/routing.py', 'scripts/forge_dataset/tiers.py', 'ui/forge_ui/reader.py',
                   'ui/forge_ui/static/panel.js'}
        found = set()
        for top in ('scripts', 'ui/forge_ui'):
            for path in (ROOT / top).rglob('*'):
                if path.is_file() and path.suffix in ('.py', '.sh', '.js', '.css', '.html', '') and '__pycache__' not in path.parts:
                    try:
                        text = path.read_text(encoding='utf-8')
                    except (UnicodeDecodeError, OSError):
                        continue
                    if 'fractal' in text.lower():
                        found.add(str(path.relative_to(ROOT)))
        if 'fractal' in (ROOT / 'forge').read_text().lower():
            found.add('forge')
        self.assertEqual(found - allowed, set())
        self.assertFalse((ROOT / 'scripts' / 'forge_fractal').exists())
        self.assertFalse(list((ROOT / 'scripts').glob('forge-fractal*')))


if __name__ == '__main__':
    unittest.main()
