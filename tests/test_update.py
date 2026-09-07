import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'; self.home.mkdir()
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + ':' + os.environ['PATH'],
                        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@test',
                        GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@test')
        for harness in ['claude', 'openclaude', 'codex']:
            (self.home / ('.' + harness) / 'skills').mkdir(parents=True)
        agy = self.bin / 'agy'
        agy.write_text('''#!/bin/bash
[ "${FAIL_AGY:-}" != 1 ] || { echo 'mock plugin failure' >&2; exit 7; }
mkdir -p "$HOME/copied"
cp -R "$3/skills/forge/." "$HOME/copied/"
echo copied >> "$HOME/agy.calls"
'''); agy.chmod(0o755)
        opencode = self.bin / 'opencode'; opencode.write_text('#!/bin/sh\nexit 0\n'); opencode.chmod(0o755)
        self.seed = self.root / 'seed'; self.seed.mkdir()
        self.git(self.seed, 'init', '-q')
        (self.seed / 'scripts').mkdir()
        for name in ['forge-update.sh', 'forge-install.sh', 'forge-install-ripwire.sh']:
            shutil.copyfile(ROOT / 'scripts' / name, self.seed / 'scripts' / name)
        (self.seed / 'SKILL.md').write_text('old skill\n')
        self.git(self.seed, 'add', '.'); self.git(self.seed, 'commit', '-qm', 'initial')
        self.source = self.root / 'checkout with spaces'
        self.git(self.root, 'clone', '-q', str(self.seed), str(self.source))

    def git(self, cwd, *args):
        return subprocess.check_output(['git', '-C', str(cwd), *args], env=self.env, stderr=subprocess.PIPE).decode().strip()

    def update(self, *args, source=None):
        return subprocess.run(['/bin/bash', str((source or self.source) / 'scripts/forge-update.sh'), *args],
                              env=self.env, cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def advance(self):
        (self.seed / 'SKILL.md').write_text('new skill\n')
        self.git(self.seed, 'commit', '-qam', 'update')

    def test_fast_forward_refreshes_links_and_copy(self):
        self.advance()
        result = self.update()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), self.git(self.seed, 'rev-parse', 'HEAD'))
        for harness in ['claude', 'openclaude', 'codex']:
            link = self.home / ('.' + harness) / 'skills/forge'
            self.assertEqual(link.resolve(), self.source)
        self.assertEqual((self.home / 'copied/SKILL.md').read_text(), 'new skill\n')
        self.assertIn('auto-detected', result.stdout)
        self.assertNotIn('Ripwire', result.stdout)
        result = self.update('--local', source=self.home / '.codex/skills/forge')
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_dry_run_does_not_fetch_or_install(self):
        self.advance()
        before = self.git(self.source, 'rev-parse', 'HEAD')
        result = self.update('--dry-run')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), before)
        self.assertFalse((self.source / '.git/FETCH_HEAD').exists())
        self.assertFalse((self.home / 'agy.calls').exists())
        self.assertFalse((self.home / '.codex/skills/forge').exists())

    def test_dirty_checkout_rejected_but_local_refresh_allowed(self):
        (self.source / 'mine').write_text('keep me')
        result = self.update()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local changes', result.stdout)
        self.assertFalse((self.source / '.git/FETCH_HEAD').exists())
        self.assertEqual(self.update('--local').returncode, 0)
        self.assertEqual((self.source / 'mine').read_text(), 'keep me')

    def test_divergence_and_fetch_failure_do_not_install(self):
        self.advance()
        (self.source / 'local').write_text('local work')
        self.git(self.source, 'add', '.'); self.git(self.source, 'commit', '-qm', 'local')
        before = self.git(self.source, 'rev-parse', 'HEAD')
        result = self.update()
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.git(self.source, 'rev-parse', 'HEAD'), before)
        self.assertFalse((self.home / 'agy.calls').exists())
        self.git(self.source, 'remote', 'set-url', 'origin', str(self.root / 'absent'))
        self.assertNotEqual(self.update().returncode, 0)
        self.assertFalse((self.home / 'agy.calls').exists())

    def test_install_failure_is_reported_and_real_directories_preserved(self):
        dest = self.home / '.codex/skills/forge'; dest.mkdir(); (dest / 'mine').write_text('keep')
        self.env['FAIL_AGY'] = '1'
        result = self.update('--local')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('mock plugin failure', result.stdout)
        self.assertIn('retry with --local', result.stdout)
        self.assertEqual((dest / 'mine').read_text(), 'keep')

    def test_detached_missing_upstream_and_copied_source(self):
        self.git(self.source, 'branch', '--unset-upstream')
        self.assertIn('no upstream', self.update().stdout)
        self.git(self.source, 'checkout', '--detach', '-q')
        self.assertIn('detached HEAD', self.update().stdout)
        self.assertEqual(self.update('--local').returncode, 0)
        copied = self.home / 'copied'
        shutil.rmtree(copied / '.git')
        self.assertIn('original Git checkout', self.update(source=copied).stdout)
        result = self.update('--source', str(self.source), '--local', source=copied)
        self.assertEqual(result.returncode, 0, result.stdout)
