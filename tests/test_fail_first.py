"""Fail-first evidence (--fail-first): do the tests a change adds fail without the change?

The script is exercised at its own command line on a hand-built repository, and end to end
through forge-solo.sh and `plan`/`run` with the fake agent CLI from test_forge.py (fixtures
borrowed, not inherited, so ForgeTests does not run twice)."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-fail-first.py'
# A test file is a script that exits non-zero when its assertion fails.
CHECK = "import sys; sys.path.insert(0, '.'); import app\n"


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'; self.repo.mkdir()
        self.env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
                        GIT_COMMITTER_EMAIL='t@t')
        self.git('init', '-q')
        self.write({'app.py': 'VALUE = 1\n', 'README.md': 'old\n'})
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

    def run_script(self, reviewed, command='python3 {files}'):
        out = Path(self.tmp.name) / 'failfirst.json'
        result = subprocess.run(['python3', str(SCRIPT), 'run', '--repo', str(self.repo), '--base', self.base,
                                 '--reviewed', reviewed, '--command', command, '--out', str(out)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(out.read_text()), result.stdout

    def test_each_new_test_is_judged_against_the_code_without_the_change(self):
        self.write({
            'app.py': 'VALUE = 2\n', 'README.md': 'new\n',
            'tests/test_proven.py': CHECK + 'assert app.VALUE == 2\n',
            'tests/test_weak.py': CHECK + 'assert app.VALUE in (1, 2)\n',
            # its new fixture travels with it: test-path files are part of "without the change"
            'tests/data/expected.txt': 'x\n',
            'tests/test_reads_fixture.py': CHECK + "assert open('tests/data/expected.txt').read() == 'x\\n'\n",
            'tests/test_broken.py': CHECK + 'assert app.VALUE == 3\n',
        })
        reviewed = self.commit('change')
        index = hashlib.sha256((self.repo / '.git/index').read_bytes()).hexdigest()
        result, prompt = self.run_script(reviewed)
        self.assertEqual(result['status'], 'ran')
        self.assertEqual({t['path']: t['result'] for t in result['tests']}, {
            'tests/test_proven.py': 'proven',
            'tests/test_weak.py': 'passes_without_change',
            'tests/test_reads_fixture.py': 'passes_without_change',
            'tests/test_broken.py': 'inconclusive',
        })
        self.assertEqual(result['counts'], {'proven': 1, 'passes_without_change': 2, 'inconclusive': 1})
        self.assertIn('[P2][CONFIRMED]', prompt)
        self.assertIn('tests/test_weak.py: PASSES WITHOUT THE CHANGE', prompt)
        # The snapshot is only read: same index, same HEAD, a clean tree.
        self.assertEqual(hashlib.sha256((self.repo / '.git/index').read_bytes()).hexdigest(), index)
        self.assertEqual((self.git('rev-parse', 'HEAD'), self.git('status', '--porcelain')), (reviewed, ''))

    def test_nothing_to_prove_says_nothing_to_the_reviewer(self):
        self.write({'app.py': 'VALUE = 2\n', 'tests/test_proven.py': CHECK + 'assert app.VALUE == 2\n'})
        with_tests = self.commit('with tests')
        self.git('reset', '-q', '--hard', self.base)
        self.write({'app.py': 'VALUE = 2\n'})
        code_only = self.commit('code only')
        for name, reviewed, command, reason in (
                ('no per-file command', with_tests, 'python3 -m unittest', '.forge/verify-subset'),
                ('no test files', code_only, 'python3 {files}', 'no test files')):
            with self.subTest(name):
                result, prompt = self.run_script(reviewed, command)
                self.assertEqual((result['status'], result['tests'], prompt), ('skipped', [], ''))
                self.assertIn(reason, result['reason'])
        result, prompt = self.run_script(with_tests)  # positive control
        self.assertEqual([t['result'] for t in result['tests']], ['proven'])
        self.assertNotIn('[P2]', prompt)


class RunnerTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    solo = fixtures.ForgeTests.solo

    def prepare(self, task='change'):
        (self.repo / '.forge').mkdir(exist_ok=True)
        (self.repo / '.forge/verify-subset').write_text('python3 {files}\n')
        name = task + '.txt'
        self.scenario({f'{task}.dwarf.*': {'files': {
            'tests/test_change.py': f"import sys\nsys.exit(0 if open('{name}').read() == 'implemented\\n' else 1)\n",
            'tests/test_weak.py': 'pass\n'}}})

    def events(self):
        path = Path(os.environ['FORGE_DATA_HOME']) / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = [json.loads(d) for (d,) in db.execute("SELECT data FROM events WHERE kind = 'event'")]
        finally:
            db.close()
        return [row for row in rows if row.get('name') == 'fail_first']

    def check(self, where, qa_input):
        evidence = json.loads((where / 'failfirst.json').read_text())
        self.assertEqual({t['path']: t['result'] for t in evidence['tests']},
                         {'tests/test_change.py': 'proven', 'tests/test_weak.py': 'passes_without_change'})
        self.assertIn('tests/test_weak.py: PASSES WITHOUT THE CHANGE', qa_input)

    def test_solo_gives_the_reviewer_the_evidence_and_keeps_the_verdict(self):
        self.prepare()
        before = len(self.events())
        result = self.solo('--fail-first')
        self.assertEqual(result.returncode, 0, result.stdout)
        run = self.root / 'solo'
        self.check(run, (run / 'qa.input').read_text())
        self.assertEqual((run / 'verdict').read_text().strip(), 'PASS')  # advice only
        self.assertEqual(len(self.events()), before + 1)
        self.git('clean', '-fdq', '-e', '.forge')  # a fresh change for the second run
        result = self.solo()  # off by default: no run, no stale evidence from the last one
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((run / 'failfirst.json').exists())
        self.assertNotIn('Fail-first evidence', (run / 'qa.input').read_text())

    def test_a_plan_with_fail_first_gives_every_task_review_the_evidence(self):
        self.prepare('a')
        p = self.root / 'plan'; (p / 'tasks/a').mkdir(parents=True)
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (p / 'tasks/a/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        result = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', '--fail-first')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('fail-first:', result.stdout)
        result = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.check(p / 'tasks/a', (p / 'tasks/a/qa.input').read_text())
        self.assertEqual((p / 'tasks/a/status').read_text().strip(), 'PASS')  # advice only


if __name__ == '__main__':
    unittest.main()
