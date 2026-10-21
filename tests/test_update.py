import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import test_forge as fixtures

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

    def npm_install(self):
        """Lay forge out as `npm install -g` does: a package dir, and a relative link in bin/."""
        package = self.root / 'npm/lib/node_modules/@giamme/forge'
        shutil.copytree(self.seed, package, ignore=shutil.ignore_patterns('.git'))
        shutil.copy2(ROOT / 'forge', package / 'forge')
        forge = self.root / 'npm/bin/forge'; forge.parent.mkdir()
        forge.symlink_to('../lib/node_modules/@giamme/forge/forge')
        return package, forge

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

    def test_forge_install_through_the_npm_bin_link_links_the_package(self):
        package, forge = self.npm_install()
        result = subprocess.run([str(forge), 'install'], env=dict(self.env, FORGE_RIPWIRE='off'), cwd=self.root,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        for harness in ['claude', 'openclaude', 'codex']:
            self.assertEqual((self.home / ('.' + harness) / 'skills/forge').resolve(), package)

    def test_update_of_an_npm_install_names_the_npm_command(self):
        package, _ = self.npm_install()
        result = self.update(source=package)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('npm install -g @giamme/forge@latest', result.stdout)
        self.assertFalse((self.home / 'agy.calls').exists())

class VersionTests(unittest.TestCase):
    """`forge update` / `forge version` from the launcher, and the once-a-day notice the runners print."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'; self.home.mkdir()
        self.state = self.root / 'state'
        self.env = dict(os.environ, HOME=str(self.home), XDG_STATE_HOME=str(self.state),
                        GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@test',
                        GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@test')
        self.env.pop('FORGE_UPDATE_CHECK', None); self.env.pop('CI', None)
        self.seed = self.root / 'seed'; (self.seed / 'scripts').mkdir(parents=True)
        shutil.copyfile(ROOT / 'forge', self.seed / 'forge'); (self.seed / 'forge').chmod(0o755)
        for name in ['forge-update.sh', 'forge-install.sh', 'forge-install-ripwire.sh', 'forge-version.py']:
            shutil.copyfile(ROOT / 'scripts' / name, self.seed / 'scripts' / name)
        (self.seed / 'SKILL.md').write_text('skill\n')
        (self.seed / 'VERSION').write_text('0.7\n')
        self.git(self.seed, 'init', '-q'); self.git(self.seed, 'add', '.'); self.git(self.seed, 'commit', '-qm', 'initial')
        self.git(self.seed, 'tag', 'v0.7')
        self.source = self.root / 'checkout'
        self.git(self.root, 'clone', '-q', str(self.seed), str(self.source))

    def git(self, cwd, *args):
        return subprocess.check_output(['git', '-C', str(cwd), *args], env=self.env, stderr=subprocess.PIPE).decode().strip()

    def forge(self, *args, **env):
        return subprocess.run([str(self.source / 'forge'), *args], env=dict(self.env, **env), cwd=self.root,
                              capture_output=True, text=True)

    def notice(self, **env):
        return subprocess.run(['python3', str(self.source / 'scripts/forge-version.py'), 'notice'],
                              env=dict(self.env, **env), capture_output=True, text=True)

    def test_update_and_version_are_routed_from_the_launcher(self):
        self.git(self.seed, 'tag', 'v0.8')
        result = self.forge('update', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('would fetch origin', result.stdout)
        self.assertFalse((self.source / '.git/FETCH_HEAD').exists())
        result = self.forge('version')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], 'forge 0.7')
        self.assertIn(self.git(self.source, 'rev-parse', '--short', 'HEAD'), lines[1])
        self.assertIn('origin/', lines[2])
        self.assertIn('not checked yet', lines[3])  # plain `version` never reaches the network
        result = self.forge('version', '--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('forge 0.8 is available (you have 0.7): forge update', result.stdout)

    def test_the_notice_names_the_newest_release_at_most_once_a_day(self):
        self.git(self.seed, 'tag', 'v0.9'); self.git(self.seed, 'tag', 'v0.10')  # 0.10 is newer, not lexically
        first = self.notice()
        self.assertEqual((first.returncode, first.stdout), (0, 'forge 0.10 is available (you have 0.7): forge update\n'))
        self.assertEqual(self.notice().stdout, '')  # same day: no second check, no second line
        cache = self.state / 'forge/update-check.json'
        data = json.loads(cache.read_text()); data['checked'] -= 25 * 3600; cache.write_text(json.dumps(data))
        self.assertEqual(self.notice().stdout, first.stdout)  # a day later it tells again

    def test_the_notice_stays_quiet_and_never_fails(self):
        self.git(self.seed, 'tag', 'v0.8')
        cache = self.state / 'forge/update-check.json'
        cases = [
            ('switched off', dict(FORGE_UPDATE_CHECK='off'), False),
            ('in CI', dict(CI='true'), False),
            ('offline', {}, True),
        ]
        for name, env, checked in cases:
            with self.subTest(name):
                if name == 'offline':
                    self.git(self.source, 'remote', 'set-url', 'origin', str(self.root / 'absent'))
                result = self.notice(**env)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
                self.assertEqual(cache.exists(), checked)  # off and CI do not even ask the remote
        self.git(self.source, 'remote', 'set-url', 'origin', str(self.seed))
        cache.unlink()
        self.git(self.seed, 'tag', '-d', 'v0.8')
        self.assertEqual(self.notice().stdout, '')  # positive control: reachable, and already the newest


class RunnerNoticeTests(unittest.TestCase):
    """A finished solo run and `plan` print the notice and keep their exit code."""
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git

    def test_runs_end_with_the_notice(self):
        seed = self.root / 'forge-seed'
        seed.mkdir()
        shutil.copytree(ROOT / 'scripts', seed / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        for name in ('forge', 'SKILL.md', 'registry.tsv'):
            shutil.copy2(ROOT / name, seed / name)
        (seed / 'VERSION').write_text('0.7\n')
        for args in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'forge'], ['tag', 'v9.9']):
            subprocess.run(['git', '-C', str(seed), *args], env=self.env, check=True, capture_output=True)
        forge = self.root / 'forge-checkout'
        subprocess.run(['git', 'clone', '-q', str(seed), str(forge)], env=self.env, check=True, capture_output=True)
        line = 'forge 9.9 is available (you have 0.7): forge update'
        run = self.root / 'solo'; run.mkdir(); (run / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL\n')
        plan = self.root / 'plan'; (plan / 'tasks/a').mkdir(parents=True)
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (plan / 'tasks/a/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a')
        for name, args in (('solo', ['forge-solo.sh', run, '--repo', self.repo, '--dwarf', 'sol']),
                           ('plan', ['forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory'])):
            with self.subTest(name):
                state = self.root / ('state-' + name)
                env = dict(self.env, XDG_STATE_HOME=str(state), FORGE_UPDATE_CHECK='on')
                result = subprocess.run(['bash', str(forge / 'scripts' / args[0]), *map(str, args[1:])], cwd=self.repo,
                                        env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(result.stderr.splitlines()[-1], line)
                self.assertNotIn(line, result.stdout)
