"""`forge config`: one view of every config source, and safe edits for the per-repo ones."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE = ROOT / 'forge'


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.cfg = self.base / 'cfg'
        self.repo = self.base / 'R'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.registry = self.base / 'registry.tsv'
        self.registry.write_text('# c\nsol\tcodex\tgpt-x\thigh\t900\nastra\tclaude\topus\tlow,high\t\n')
        self.env = dict(os.environ, HOME=str(self.base), XDG_CONFIG_HOME=str(self.cfg),
                        FORGE_REGISTRY=str(self.registry), FORGE_TEST_TOKEN='hunter2')

    def run_cfg(self, *args, stdin=None):
        return subprocess.run([str(FORGE), 'config', *args], capture_output=True, text=True,
                              env=self.env, input=stdin, cwd=str(self.base))

    def test_show_json(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge/verify').write_text('make test\n')
        (self.cfg / 'forge').mkdir(parents=True)
        (self.cfg / 'forge/cloudflare.env').write_text('CF_SECRET_VALUE=zzz\n')
        out = self.run_cfg('show', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        data = json.loads(out.stdout)
        self.assertEqual(data['registry']['rows'][0],
                         dict(alias='sol', harness='codex', model='gpt-x', effort='high', timeout='900'))
        self.assertTrue(data['registry']['overridden'])
        self.assertFalse(data['registry']['editable'])
        self.assertIn('enabled', data['jev'])
        self.assertEqual(data['repo']['files']['verify'], dict(exists=True, content='make test\n'))
        self.assertFalse(data['repo']['files']['setup']['exists'])
        self.assertEqual(data['env']['FORGE_TEST_TOKEN'], '<set>')
        self.assertNotIn('hunter2', out.stdout)
        self.assertNotIn('zzz', out.stdout)

    def test_oversize_content_is_withheld(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge/setup').write_text('x' * 70000)
        data = json.loads(self.run_cfg('show', '--repo', str(self.repo), '--json').stdout)
        entry = data['repo']['files']['setup']
        self.assertTrue(entry['exists'])
        self.assertIsNone(entry['content'])
        self.assertIn('note', entry)

    def test_set_and_unset_verify(self):
        out = self.run_cfg('set', 'repo.verify', 'bash tests/check.sh', '--repo', str(self.repo), '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads(out.stdout)['ok'])
        self.assertEqual((self.repo / '.forge/verify').read_text().strip(), 'bash tests/check.sh')
        out = self.run_cfg('unset', 'repo.verify', '--repo', str(self.repo), '--json')
        self.assertEqual(json.loads(out.stdout)['previous'].strip(), 'bash tests/check.sh')
        self.assertFalse((self.repo / '.forge/verify').exists())

    def test_set_reads_stdin(self):
        self.run_cfg('set', 'repo.setup', '-', '--repo', str(self.repo), stdin='npm ci\n')
        self.assertEqual((self.repo / '.forge/setup').read_text(), 'npm ci\n')

    def test_verify_subset_needs_files_placeholder(self):
        bad = self.run_cfg('set', 'repo.verify-subset', 'pytest', '--repo', str(self.repo))
        self.assertEqual(bad.returncode, 2)
        self.assertFalse((self.repo / '.forge/verify-subset').exists())
        ok = self.run_cfg('set', 'repo.verify-subset', 'pytest {files}', '--repo', str(self.repo))
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_unknown_key_lists_valid_keys(self):
        out = self.run_cfg('set', 'repo.nope', 'x', '--repo', str(self.repo))
        self.assertEqual(out.returncode, 2)
        self.assertIn('repo.verify-subset', out.stderr)

    def test_refusals(self):
        plain = self.base / 'plain'; plain.mkdir()
        self.assertEqual(self.run_cfg('set', 'repo.verify', 'x', '--repo', str(plain)).returncode, 2)
        outside = self.base / 'outside'; outside.mkdir()
        # symlinked .forge dir
        (self.repo / '.forge').symlink_to(outside)
        for cmd in (('set', 'repo.verify', 'x'), ('unset', 'repo.verify')):
            self.assertEqual(self.run_cfg(*cmd, '--repo', str(self.repo)).returncode, 2)
        self.assertEqual(list(outside.iterdir()), [])
        (self.repo / '.forge').unlink()
        # symlinked target
        (self.repo / '.forge').mkdir()
        victim = outside / 'victim'; victim.write_text('keep')
        (self.repo / '.forge/verify').symlink_to(victim)
        for cmd in (('set', 'repo.verify', 'x'), ('unset', 'repo.verify')):
            self.assertEqual(self.run_cfg(*cmd, '--repo', str(self.repo)).returncode, 2)
        self.assertEqual(victim.read_text(), 'keep')
        self.assertTrue((self.repo / '.forge/verify').is_symlink())
        (self.repo / '.forge/verify').unlink()
        # FIFO target
        os.mkfifo(self.repo / '.forge/verify')
        for cmd in (('set', 'repo.verify', 'x'), ('unset', 'repo.verify')):
            self.assertEqual(self.run_cfg(*cmd, '--repo', str(self.repo)).returncode, 2)

    def test_jev_enabled(self):
        out = self.run_cfg('set', 'jev.enabled', 'true', '--json')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads((self.cfg / 'forge/jev.json').read_text())['enabled'])
        out = self.run_cfg('set', 'jev.enabled', 'false', '--json')
        self.assertTrue(json.loads(out.stdout)['previous'])
        self.assertFalse(json.loads((self.cfg / 'forge/jev.json').read_text())['enabled'])
        self.assertEqual(self.run_cfg('set', 'jev.enabled', 'maybe').returncode, 2)

    def test_launcher(self):
        self.assertEqual(self.run_cfg('--help').returncode, 0)
        out = subprocess.run([str(FORGE), '--help'], capture_output=True, text=True)
        self.assertIn('config', out.stdout)


if __name__ == '__main__':
    unittest.main()
