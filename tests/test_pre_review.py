"""Pre-review tests (#85): forge runs only the tests an attempt affects before paying for its review.

The script is exercised at its own command line on a hand-built repository, and end to end
through `plan --pre-review-tests` / `run` with the fake agent CLI from test_forge.py (fixtures
borrowed, not inherited, so ForgeTests does not run twice)."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-pre-review.py'
# Each selected test file is a script that exits non-zero when its assertion fails.
EACH = 'for f in {files}; do python3 "$f" || exit 1; done'
CHECK = "import sys; sys.path.insert(0, '.'); import app\n"


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                        GIT_COMMITTER_EMAIL='t@t')
        self.git('init', '-q')
        self.write({'app.py': 'VALUE = 1\n', 'README.md': 'old\n',
                    'lib/parser.py': 'def parse(x):\n    return x\n',
                    'tests/test_app.py': CHECK + 'assert app.VALUE in (1, 2)\n',
                    'tests/test_parsing.py': 'from lib import parser\n',
                    'tests/test_other.py': 'pass\n'})
        self.base = self.commit('base')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.repo), *args], env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def write(self, files):
        for name, text in files.items():
            path = self.repo / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(text)

    def commit(self, message):
        self.git('add', '-A'); self.git('commit', '-qm', message)
        return self.git('rev-parse', 'HEAD')

    def select(self, reviewed, *args):
        result = subprocess.run(['python3', str(SCRIPT), 'select', '--repo', str(self.repo), '--base', self.base,
                                 '--reviewed', reviewed, *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def run_check(self, reviewed, command=EACH, *args, base=None):
        out, report = self.root / 'prereview.json', self.root / 'prereview.md'
        report.unlink(missing_ok=True)
        result = subprocess.run(['python3', str(SCRIPT), 'run', '--repo', str(self.repo), '--base', base or self.base,
                                 '--reviewed', reviewed, '--command', command, '--out', str(out),
                                 '--report', str(report), *args], env=self.env, capture_output=True, text=True)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return result.returncode, json.loads(out.read_text()), result.stdout, report

    # --- selection ---------------------------------------------------------------------
    def test_selection_takes_changed_tests_then_convention_then_reference(self):
        self.write({'app.py': 'VALUE = 2\n', 'lib/parser.py': 'def parse(x):\n    return x.strip()\n',
                    'tests/test_new.py': 'pass\n', 'README.md': 'new\n'})
        chosen = self.select(self.commit('change'))
        self.assertEqual(chosen['why'], {'tests/test_new.py': 'changed',
                                         'tests/test_app.py': 'convention: app.py',
                                         'tests/test_parsing.py': 'reference: lib/parser.py'})
        self.assertIsNone(chosen['too_broad'])

    def test_a_test_map_rule_answers_for_its_sources_before_any_guess(self):
        self.write({'app.py': 'VALUE = 2\n', 'lib/parser.py': 'def parse(x):\n    return x.strip()\n'})
        reviewed = self.commit('change')
        test_map = self.root / 'test-map'
        test_map.write_text('# source glob   tests\nlib/*.py   tests/test_other.py\napp.py -\n')
        chosen = self.select(reviewed, '--map', str(test_map))
        self.assertEqual(chosen['why'], {'tests/test_other.py': 'map: lib/parser.py'})   # app.py maps to no tests

    def test_shared_infrastructure_or_too_many_files_is_too_broad(self):
        for name in ('conftest.py', 'package.json', 'tests/helpers.py'):
            with self.subTest(name=name):
                self.git('checkout', '-q', self.base)
                self.write({name: '# changed\n', 'app.py': 'VALUE = 2\n'})
                chosen = self.select(self.commit('infra ' + name))
                self.assertIn('shared test infrastructure: ' + name, chosen['too_broad'])
        self.git('checkout', '-q', self.base)
        self.write({'app.py': 'VALUE = 2\n', 'tests/test_new.py': 'pass\n'})
        chosen = self.select(self.commit('two tests'), '--max-files', '1')
        self.assertEqual(chosen['too_broad'], '2 test files selected, more than the cap of 1')

    # --- outcomes ----------------------------------------------------------------------
    def test_a_green_check_is_evidence_and_the_snapshot_is_only_read(self):
        self.write({'app.py': 'VALUE = 2\n'})
        reviewed = self.commit('change')
        index = hashlib.sha256((self.repo / '.git/index').read_bytes()).hexdigest()
        rc, result, evidence, report = self.run_check(reviewed)
        self.assertEqual((rc, result['status'], result['selected']), (0, 'passed', ['tests/test_app.py']))
        self.assertIn('All passed: tests/test_app.py', evidence)
        self.assertFalse(report.exists())
        self.assertEqual(hashlib.sha256((self.repo / '.git/index').read_bytes()).hexdigest(), index)
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('status', '--porcelain')), (reviewed, ''))

    def test_a_test_the_attempt_breaks_fails_it_with_the_output_for_the_implementer(self):
        self.write({'app.py': 'VALUE = 2\n', 'tests/test_new.py': CHECK + "assert app.VALUE == 3, 'VALUE is ' + str(app.VALUE)\n"})
        rc, result, evidence, report = self.run_check(self.commit('change'))
        self.assertEqual((rc, result['status'], result['failing']), (1, 'failed', ['tests/test_new.py']))
        self.assertEqual(evidence, '')
        text = report.read_text()
        self.assertIn('observed by forge, not by a reviewer', text)
        self.assertIn('  - tests/test_new.py', text)
        self.assertIn('VALUE is 2', text)                   # the end of the failing run's output

    def test_a_test_that_fails_on_the_task_base_too_is_not_blamed(self):
        self.write({'tests/test_app.py': CHECK + 'assert app.VALUE == 5\n'})
        base = self.commit('a red test before the change')
        self.write({'app.py': 'VALUE = 2\n'})
        rc, result, evidence, _report = self.run_check(self.commit('change'), base=base)
        self.assertEqual((rc, result['status'], result['preexisting']), (0, 'pre-existing', ['tests/test_app.py']))
        self.assertIn('fail on the task base too', evidence)

    def test_a_failure_that_passes_when_rerun_alone_is_flaky(self):
        self.write({'app.py': 'VALUE = 2\n'})
        flag = self.root / 'failed-once'
        command = f'if [ ! -e {flag} ]; then : > {flag}; exit 1; fi; ' + EACH
        rc, result, evidence, _report = self.run_check(self.commit('change'), command)
        self.assertEqual((rc, result['status']), (0, 'flaky'))
        self.assertIn('flaky', evidence)

    def test_a_check_that_cannot_run_or_runs_too_long_never_blocks(self):
        self.write({'app.py': 'VALUE = 2\n'})
        reviewed = self.commit('change')
        for command, args, reason in (('', (), 'no test command'),
                                      ('python3 -m unittest', (), 'no test command'),
                                      ('sleep 5; ' + EACH, ('--timeout', '1'), 'too broad: the selected tests took longer')):
            with self.subTest(command=command):
                rc, result, evidence, _report = self.run_check(reviewed, command, *args)
                self.assertEqual((rc, result['status'], evidence), (0, 'skipped', ''))
                self.assertTrue(result['reason'].startswith(reason), result['reason'])

    def test_a_related_tests_runner_gets_the_changed_paths(self):
        self.write({'app.py': 'VALUE = 2\n', 'lib/parser.py': 'def parse(x):\n    return x.strip()\n'})
        seen = self.root / 'changed.txt'
        rc, result, _evidence, _report = self.run_check(self.commit('change'), f'printf "%s\\n" {{changed}} > {seen}')
        self.assertEqual((rc, result['status']), (0, 'passed'))
        self.assertEqual(seen.read_text().split(), ['app.py', 'lib/parser.py'])


class RunnerTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan(self, *flags):
        (self.repo / '.forge').mkdir(exist_ok=True)
        (self.repo / '.forge/verify-subset').write_text(EACH + '\n')
        p = self.root / 'plan'; (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        result = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(result.returncode, 0, result.stdout)
        return p, result.stdout

    def test_a_red_attempt_goes_back_to_the_implementer_without_a_review(self):
        p, table = self.plan('--pre-review-tests')
        self.assertIn('pre-review tests: on', table)
        self.scenario({'a.dwarf.1': {'files': {'tests/test_a.py': "raise SystemExit('a.txt is not done yet')\n"}},
                       'a.dwarf.2': {'content': 'fixed', 'files': {'tests/test_a.py': 'pass\n'}}})
        result = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'dwarf', 'qa'])          # no reviewer for the red attempt
        task = p / 'tasks/a'
        self.assertTrue((task / 'merged').exists())
        retry = (task / 'dwarf.input').read_text()
        self.assertIn('Pre-review tests (observed by forge, not by a reviewer)', retry)
        self.assertIn('a.txt is not done yet', retry)
        self.assertIn('forge runs the tests this change affects before the review', retry)
        self.assertIn('All passed: tests/test_a.py', (task / 'qa.input').read_text())
        self.assertEqual(json.loads((task / 'prereview.json').read_text())['status'], 'passed')
        self.assertIn('pre-review tests: 2 check(s), 1 attempt(s) sent back without a review', result.stdout)

    def test_plan_sets_how_many_checks_run_at_once(self):
        p, _table = self.plan('--pre-review-tests', '--pre-review-parallel', '3')
        self.assertEqual((p / 'pre_review_parallel').read_text().strip(), '3')
        for bad in ('0', '65', 'two'):
            result = self.run_script('forge-parallel.sh', 'plan', p, '--pre-review-parallel', bad)
            self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual((p / 'pre_review_parallel').read_text().strip(), '3')

    def test_without_the_flag_nothing_runs_before_the_review(self):
        p, table = self.plan()
        self.assertNotIn('pre-review tests', table)
        self.scenario({'a.dwarf.1': {'files': {'tests/test_a.py': "raise SystemExit('red')\n"}}})
        self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        self.assertFalse((p / 'tasks/a/prereview.json').exists())
        self.assertNotIn('forge runs the tests this change affects', (p / 'tasks/a/dwarf.input').read_text())


if __name__ == '__main__':
    unittest.main()
