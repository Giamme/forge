import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'ui' / 'tests'))
import forge_registry  # noqa: E402
import many_fixture  # noqa: E402


class RunsTests(unittest.TestCase):
    """`forge runs`: a repo's registered runs, newest first, read-only."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.env = dict(os.environ, XDG_CONFIG_HOME=str(base / 'config'))
        self.repos = {}
        for name in ('a', 'b'):
            repo = base / name; (repo / 'src').mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            self.repos[name] = repo
        # six runs, each newer than the last: even ones registered for repo a, odd ones for b
        self.runs = sorted(many_fixture.make_many(base, n=6, repos=2, live=0).iterdir())
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.env['XDG_CONFIG_HOME']}):
            for i, run in enumerate(self.runs):
                forge_registry.register_run_dir(self.repos['ab'[i % 2]], run)

    def forge(self, *args, cwd):
        result = subprocess.run([str(ROOT / 'forge'), 'runs', *args], cwd=cwd, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_lists_this_repos_runs_newest_first_with_their_dirs(self):
        out = self.forge(cwd=self.repos['a'] / 'src')      # anywhere inside the repo
        dirs = [line.strip() for line in out.splitlines() if line.startswith('    ')]
        self.assertEqual(dirs, [str(self.runs[4]), str(self.runs[2]), str(self.runs[0])])
        self.assertTrue(out.startswith(f"forge runs for {self.repos['a']}: 3 of 3"), out)
        limited = self.forge('--limit', '2', cwd=self.repos['a'])
        self.assertEqual([line.strip() for line in limited.splitlines() if line.startswith('    ')], dirs[:2])

    def test_json_names_every_run_of_a_repo_or_of_all_of_them(self):
        b = json.loads(self.forge('--repo', str(self.repos['b']), '--json', cwd=self.repos['a']))
        self.assertEqual([r['path'] for r in b], [str(self.runs[5]), str(self.runs[3]), str(self.runs[1])])
        self.assertTrue(all(r['run_id'] and r['kind'] in ('plan', 'solo') and isinstance(r['counts'], dict) for r in b), b)
        every = json.loads(self.forge('--all', '--limit', '0', '--json', cwd=self.repos['a']))
        self.assertEqual([r['path'] for r in every], [str(p) for p in reversed(self.runs)])

    def test_a_repo_with_no_runs_says_so(self):
        empty = Path(self.tmp.name) / 'c'; empty.mkdir()
        out = self.forge(cwd=empty)
        self.assertIn('no runs registered for', out)
        self.assertEqual(json.loads(self.forge('--json', cwd=empty)), [])


if __name__ == '__main__':
    unittest.main()
