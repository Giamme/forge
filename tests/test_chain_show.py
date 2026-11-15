"""Chained plans show up where runs are listed (#143).

A plan's `after` file names the plan it starts from. The reader reports that predecessor on the
summary and the detail, and the runs that name this one, without summarizing every neighbour.
`forge runs` prints the predecessor on the follower's line.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'ui'))

import forge_registry  # noqa: E402
from forge_ui.reader import Catalog, detail, run_key, summarize  # noqa: E402

_spec = importlib.util.spec_from_file_location('forge_runs_show', ROOT / 'scripts' / 'forge-runs.py')
runs_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runs_cli)

TASKS = 't\t-\tlow\tt.txt\tsol\topus\tOne task\n'


class ChainShowTests(unittest.TestCase):
    """Registered plan dirs, the way `forge runs` finds them: an isolated XDG config home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.config = str(self.base / 'config')
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.config})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.a = self.plan('a', 'plan-a')
        self.b = self.plan('b', 'plan-b')
        # The file stores a symlink to A, not A's real path: matching is on the resolved path.
        link = self.base / 'via-a'
        link.symlink_to(self.a, target_is_directory=True)
        (self.b / 'after').write_text(str(link) + '\n')
        self.plain = self.plan('plain', 'plan-plain')
        for plan in (self.a, self.b, self.plain):
            forge_registry.register_run_dir(self.repo, plan)
        self.predecessor = {
            'key': run_key(self.a),
            'run_id': 'plan-a',
            'path': os.path.realpath(self.a),
        }
        self.follower = {'key': run_key(self.b), 'run_id': 'plan-b'}

    def plan(self, name, run_id):
        path = self.base / name
        path.mkdir()
        (path / 'tasks.tsv').write_text(TASKS)
        (path / 'run_id').write_text(run_id + '\n')
        (path / 'repo').write_text(str(self.repo) + '\n')
        (path / 'goal.txt').write_text('Ship ' + name + '\n')
        return path

    def test_the_summary_and_the_catalog_name_the_predecessor_and_its_follower(self):
        self.assertEqual(summarize(self.b)['after'], self.predecessor)
        self.assertEqual(detail(self.b)['after'], self.predecessor)
        self.assertIsNone(summarize(self.a)['after'])
        self.assertIsNone(detail(self.a)['after'])
        self.assertIsNone(summarize(self.plain)['after'])

        listed = {item['run_id']: item for item in Catalog().runs()}
        self.assertEqual(listed['plan-a']['followed_by'], [self.follower])
        self.assertEqual(listed['plan-b']['after'], self.predecessor)
        self.assertEqual(listed['plan-b']['followed_by'], [])
        self.assertIsNone(listed['plan-plain']['after'])
        self.assertEqual(listed['plan-plain']['followed_by'], [])
        self.assertEqual(Catalog().get(run_key(self.a))['followed_by'], [self.follower])
        self.assertEqual(Catalog().get(run_key(self.b))['after'], self.predecessor)

    def test_a_deleted_predecessor_keeps_its_key_and_a_null_run_id(self):
        gone = self.plan('gone', 'plan-gone')
        key, real = run_key(gone), os.path.realpath(gone)
        shutil.rmtree(gone)
        follower = self.plan('later', 'plan-later')
        (follower / 'after').write_text(real + '\n')
        want = {'key': key, 'run_id': None, 'path': real}
        self.assertEqual(summarize(follower)['after'], want)
        self.assertEqual(detail(follower)['after'], want)
        # No run id left to print: the line falls back to the predecessor's key.
        self.assertIn('  after ' + key, runs_cli.line(summarize(follower), time.time()).splitlines()[0])

    def test_forge_runs_names_the_predecessor_only_on_the_follower(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = runs_cli.main(['--repo', str(self.repo), '--limit', '0', '--json'])
        self.assertEqual(code, 0)
        listed = {item['run_id']: item for item in json.loads(buf.getvalue())}
        self.assertEqual(listed['plan-b']['after'], self.predecessor)
        self.assertIsNone(listed['plan-a']['after'])
        self.assertIsNone(listed['plan-plain']['after'])
        now = time.time()
        self.assertIn('  after plan-a', runs_cli.line(listed['plan-b'], now).splitlines()[0])
        self.assertNotIn(' after ', runs_cli.line(listed['plan-a'], now).splitlines()[0])
        self.assertNotIn(' after ', runs_cli.line(listed['plan-plain'], now).splitlines()[0])


if __name__ == '__main__':
    unittest.main()
