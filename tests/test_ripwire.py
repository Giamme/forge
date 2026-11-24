import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import test_forge as fixtures
ROOT = fixtures.ROOT


def fake_api(root, tag):
    """A file:// stand-in for the GitHub releases API: FORGE_RIPWIRE_API points at it."""
    api = Path(root) / 'api'
    latest = api / 'repos/redhat-et/ripwire/releases/latest'
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps({'tag_name': tag}))
    return api.as_uri()

spec = importlib.util.spec_from_file_location('ripwire', ROOT / 'scripts/forge-ripwire.py')
rw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rw)
FAKE_RW = '''#!/usr/bin/env python3
import json, os, pathlib, sys
if '--version' in sys.argv:
 print('ripwire 0.7.0'); sys.exit()
with open(os.environ['RW_CALLS'], 'a') as f:
 f.write(json.dumps({'argv':sys.argv, 'cwd':os.getcwd()})+'\\n')
print('<context>条件 fresh evidence</context>')
'''


class RipwireIntegrationTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    solo = fixtures.ForgeTests.solo
    plan = fixtures.ForgeTests.plan

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        binary = self.bin / 'ripwire'
        binary.write_text(FAKE_RW); binary.chmod(0o755)
        self.env.pop('FORGE_RIPWIRE', None)
        self.env['RW_CALLS'] = str(self.root / 'rw-calls')

    def calls(self):
        return [json.loads(x) for x in (self.root / 'rw-calls').read_text().splitlines()]

    def test_solo_requirements_and_review_baseline(self):
        result = self.solo()
        self.assertEqual(result.returncode, 0, result.stdout)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        self.assertIn('--for=Implement change. REQUIREMENT_SENTINEL\n', calls[0]['argv'])
        self.assertTrue(any(x.startswith('--pr-context=') for x in calls[1]['argv']))
        self.assertNotEqual(calls[0]['argv'][1], calls[1]['argv'][1])
        for call in calls:
            self.assertIn('--no-cache', call['argv'])
            self.assertFalse(call['cwd'].startswith(call['argv'][1] + '/'))
        records = list((self.root / 'solo').glob('attempts/*/ripwire.json'))
        self.assertEqual(len(records), 2)
        self.assertTrue(all(json.loads(p.read_text())['status'] == 'delivered' for p in records))

    def test_parallel_retry_and_durable_optout(self):
        plan = self.plan()
        self.env['VERDICT'] = 'FAIL'
        self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(len(self.calls()), 2)
        result = self.run_script('forge-parallel.sh', 'retry', plan, 'a', '--no-ripwire')
        self.assertEqual(len(self.calls()), 2, result.stdout)
        self.assertTrue((plan / 'no_ripwire').exists())
        self.run_script('forge-parallel.sh', 'retry', plan, 'a')
        self.assertEqual(len(self.calls()), 2)

    def test_optout_plan_solo_and_environment(self):
        self.assertEqual(self.solo('--no-ripwire').returncode, 0)
        plan = self.plan()
        self.run_script('forge-parallel.sh', 'plan', plan, '--no-ripwire')
        self.run_script('forge-parallel.sh', 'run', plan)
        self.assertFalse((self.root / 'rw-calls').exists())

    def test_direct_planner_preserves_bytes_and_does_not_accumulate(self):
        run = self.root / 'dispatch'; run.mkdir()
        original = ('条件\r\n' + 'x' * 3000 + '\n\n').encode()
        source = run / 'input'; source.write_bytes(original)
        args = ['planner', 'opus', '--repo', self.repo, '--run-dir', run]
        result = self.run_script('forge-dispatch.sh', *args, '--prompt-file', source)
        self.assertEqual(result.returncode, 0, result.stdout)
        delivered = (run / 'planner.prompt').read_bytes()
        self.assertTrue(delivered.startswith(original))
        self.assertEqual(len(self.calls()[0]['argv'][-1][len('--for='):]), 2048)
        result = self.run_script('forge-dispatch.sh', *args, '--prompt-file', run / 'planner.prompt')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((run / 'planner.prompt').read_bytes(), delivered)
        self.run_script('forge-dispatch.sh', *args, '--prompt-file', source, '--dry-run')
        self.assertEqual(len(self.calls()), 2)

    def test_native_review_skips(self):
        result = self.solo('--qa', 'sol', '--native-review')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.calls()), 1)
        record = json.loads(next((self.root / 'solo').glob('attempts/qa-*/ripwire.json')).read_text())
        self.assertIn('native review', record['reason'])

    def test_all_harnesses_and_fifo_stdin(self):
        import threading
        for harness in ['codex', 'claude', 'openclaude', 'opencode', 'antigravity']:
            name = 'agy' if harness == 'antigravity' else harness
            binary = self.bin / name
            binary.write_text(fixtures.FAKE); binary.chmod(0o755)
            run = self.root / harness; run.mkdir()
            fifo = run / 'fifo'; os.mkfifo(fifo)
            original = 'Unicode 条件\nComplete requirements\n'
            writer = threading.Thread(target=lambda: fifo.write_text(original))
            writer.start()
            result = self.run_script('forge-dispatch.sh', 'planner', 'model:high:' + harness,
                                     '--repo', self.repo, '--run-dir', run, '--prompt-file', fifo)
            writer.join(timeout=2)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertTrue((run / 'planner.prompt').read_text().startswith(original))
        self.assertEqual(len(self.calls()), 5)
        run = self.root / 'stdin'; run.mkdir()
        result = subprocess.run(['/bin/bash', str(ROOT / 'scripts/forge-dispatch.sh'),
            'planner', 'opus', '--repo', str(self.repo), '--run-dir', str(run),
            '--prompt-file', '-'], input='from stdin 条件\n'.encode(),
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((run / 'planner.prompt').read_bytes().startswith('from stdin 条件\n'.encode()))


class RipwireBoundsTests(unittest.TestCase):
    def test_errors_empty_incompatible_and_missing_preserve_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); prompt = root / 'prompt'; binary = root / 'ripwire'
            args = argparse.Namespace(repo=tmp, role='dwarf', prompt=str(prompt), query='',
                                      baseline='', artifacts=tmp, native='0')
            for body, reason in [('echo ripwire 0.4.0', 'incompatible'),
                                 ('echo ripwire', 'incompatible'),
                                 ('if [ "$1" = --version ]; then echo 0.6.0; else exit 6; fi', 'exit 6'),
                                 ('if [ "$1" = --version ]; then echo 0.7.0; fi', 'empty')]:
                prompt.write_bytes(b'original\r\n')
                binary.write_text('#!/bin/sh\n' + body); binary.chmod(0o755)
                with patch.object(rw, 'resolve', return_value=str(binary)), patch.dict(os.environ, {'FORGE_RIPWIRE': ''}):
                    rw.prepare(args)
                self.assertEqual(prompt.read_bytes(), b'original\r\n')
                self.assertIn(reason, json.loads((root / 'ripwire.json').read_text())['reason'])
            with patch.object(rw, 'resolve', return_value=None), patch.dict(os.environ, {'FORGE_RIPWIRE': ''}):
                rw.prepare(args)
            self.assertIn('missing', json.loads((root / 'ripwire.json').read_text())['reason'])

    def test_a_child_that_already_exited_is_not_an_error(self):
        # macOS answers EPERM, not ESRCH, to killpg on a group whose only member is an unreaped zombie
        def eperm(*_):
            raise PermissionError(1, 'Operation not permitted')
        with tempfile.TemporaryDirectory() as tmp, patch.object(rw.os, 'killpg', eperm):
            with self.assertRaisesRegex(RuntimeError, 'excessive output'):
                rw.bounded([sys.executable, '-c', "print('x'*20000)"], tmp, time.monotonic() + 5)
            self.assertEqual(rw.bounded([sys.executable, '-c', "print('ok')"], tmp, time.monotonic() + 5), 'ok\n')

    def test_output_and_timeout_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            for code, reason in [("print('x'*20000)", 'excessive output'),
                                 ('import time; time.sleep(20)', 'timeout')]:
                started = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, reason):
                    rw.bounded([sys.executable, '-c', code], tmp, started + .3)
                self.assertLess(time.monotonic() - started, 2)

    def test_timeout_kills_descendants(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / 'survived'
            code = ("import os,time,pathlib; child=os.fork(); "
                    "time.sleep(.7 if child == 0 else 20); "
                    f"pathlib.Path({str(marker)!r}).write_text('survived')")
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                rw.bounded([sys.executable, '-c', code], tmp, time.monotonic() + .2)
            time.sleep(.8)
            self.assertFalse(marker.exists())


class RipwireInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        self.home = self.root / 'home'; self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), PATH=str(self.bin) + ':/usr/bin:/bin',
                        DOWNLOADS=str(self.root / 'downloads'), FIXTURE=str(ROOT / 'tests/fixtures/ripwire-install-v0.4.0.sh'))
        self.env.pop('FORGE_RIPWIRE', None)
        self.env.pop('FORGE_RIPWIRE_VERSION', None)
        self.env['FORGE_RIPWIRE_API'] = fake_api(self.root, 'v0.6.10')
        curl = self.bin / 'curl'
        curl.write_text('''#!/bin/bash
printf 'download\\n' >> "$DOWNLOADS"
for ((i=1; i<=$#; i++)); do
 if [ "${!i}" = -o ]; then j=$((i+1)); dest="${!j}"; fi
done
if [ "${BAD_INSTALLER:-}" = 1 ]; then echo invalid > "$dest"; exit; fi
if [ -n "${dest:-}" ]; then cp "$FIXTURE" "$dest"; else echo 'mock metadata download failed' >&2; exit 22; fi
'''); curl.chmod(0o755)
        self.command = ['/bin/bash', str(ROOT / 'scripts/forge-install-ripwire.sh')]

    def run_install(self, *flags):
        return subprocess.run(self.command + list(flags), env=self.env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, text=True)

    def test_headless_dry_run_optout_and_existing_never_download(self):
        for flags in [(), ('--dry-run',), ('--yes', '--dry-run')]:
            result = self.run_install(*flags)
            self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.root / 'downloads').exists())
        self.env['FORGE_RIPWIRE'] = 'off'
        self.assertEqual(self.run_install('--yes').returncode, 0)
        self.env.pop('FORGE_RIPWIRE')
        dest = self.home / '.local/bin/ripwire'; dest.parent.mkdir(parents=True)
        dest.write_text('preserved')
        self.assertEqual(self.run_install('--yes').returncode, 0)
        self.assertEqual(dest.read_text(), 'preserved')
        self.assertFalse((self.root / 'downloads').exists())

    def test_a_file_that_is_not_the_release_installer_is_never_run_and_upstream_failure_propagates(self):
        self.env['BAD_INSTALLER'] = '1'
        result = self.run_install('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not the Ripwire release installer', result.stdout)
        self.env.pop('BAD_INSTALLER')
        result = self.run_install('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('could not fetch release metadata', result.stdout)

    def test_terminal_consent(self):
        import pty
        import select
        for answer, downloads in [(b'n\n', False), (b'\x04', False), (b'\n', True), (b'y\n', True)]:
            (self.root / 'downloads').unlink(missing_ok=True)
            pid, fd = pty.fork()
            if pid == 0:
                os.execve('/bin/bash', self.command, self.env)
            output = b''
            try:
                deadline = time.monotonic() + 5
                while b'[Y/n]' not in output and time.monotonic() < deadline:
                    if select.select([fd], [], [], .1)[0]:
                        output += os.read(fd, 4096)
                self.assertIn(b'[Y/n]', output)
                self.assertFalse((self.root / 'downloads').exists())
                os.write(fd, answer)
                while time.monotonic() < deadline:
                    done, status = os.waitpid(pid, os.WNOHANG)
                    if done:
                        break
                    if select.select([fd], [], [], .1)[0]:
                        try: output += os.read(fd, 4096)
                        except OSError: pass
                else:
                    self.fail('installer did not finish')
                self.assertEqual((self.root / 'downloads').exists(), downloads)
                self.assertEqual(output.count(b'Ripwire is missing.'), 1)
            finally:
                os.close(fd)
                try: os.kill(pid, 9); os.waitpid(pid, 0)
                except ProcessLookupError: pass

    def test_verified_release_install_and_no_activation(self):
        import hashlib
        import platform
        import tarfile
        archive_root = self.root / 'release'
        os_name = 'macos' if platform.system() == 'Darwin' else 'linux'
        arch = 'arm64' if platform.machine() in ('arm64', 'aarch64') else 'x64'
        name = f'ripwire-0.6.10-{os_name}-{arch}'
        bundle = archive_root / name; bundle.mkdir(parents=True)
        binary = bundle / 'ripwire'; binary.write_text('#!/bin/sh\necho ripwire 0.6.10\n'); binary.chmod(0o755)
        skills = bundle / 'skills'; skills.mkdir()
        (skills / 'install.sh').write_text('#!/bin/sh\ntouch "$HOME/ACTIVATED"\n')
        (self.home / '.claude').mkdir()
        archive = self.root / (name + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(bundle, arcname=name)
        self.env['ARCHIVE'] = str(archive)
        self.env['CHECKSUM'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.bin / 'curl').write_text('''#!/usr/bin/python3
import json, os, pathlib, shutil, sys
a=sys.argv[1:]
with open(os.environ['DOWNLOADS'], 'a') as f: f.write('download\\n')
archive=pathlib.Path(os.environ['ARCHIVE'])
if '-o' not in a:
 print(json.dumps({'tag_name':'v0.6.10', 'assets':[{'browser_download_url':'https://example.test/'+archive.name}]}, indent=2))
else:
 dest=pathlib.Path(a[a.index('-o')+1])
 if dest.name == 'install.sh': shutil.copyfile(os.environ['FIXTURE'], dest)
 elif dest.name.endswith('.sha256'): dest.write_text(os.environ['CHECKSUM']+'  '+archive.name+'\\n')
 else: shutil.copyfile(archive, dest)
'''.replace('#!/usr/bin/python3', '#!' + sys.executable))
        self.env['CHECKSUM'] = '0' * 64
        failed = self.run_install('--yes')
        self.assertNotEqual(failed.returncode, 0, failed.stdout)
        self.assertFalse((self.home / '.local/bin/ripwire').exists())
        self.env['CHECKSUM'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        result = self.run_install('--yes')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((self.home / '.local/bin/ripwire').exists())
        self.assertTrue((self.home / '.local/share/ripwire/skills/install.sh').exists())
        self.assertFalse((self.home / 'ACTIVATED').exists())


class RipwireUpgradeTests(RipwireInstallTests):
    """--upgrade replaces only the Ripwire forge installed (#170)."""

    def release(self, version):
        import hashlib
        import platform
        import tarfile
        os_name = 'macos' if platform.system() == 'Darwin' else 'linux'
        arch = 'arm64' if platform.machine() in ('arm64', 'aarch64') else 'x64'
        name = f'ripwire-{version}-{os_name}-{arch}'
        bundle = self.root / 'release' / name; bundle.mkdir(parents=True)
        binary = bundle / 'ripwire'; binary.write_text(f'#!/bin/sh\necho ripwire {version}\n'); binary.chmod(0o755)
        (bundle / 'skills').mkdir()
        archive = self.root / (name + '.tar.gz')
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(bundle, arcname=name)
        self.env.update(ARCHIVE=str(archive), CHECKSUM=hashlib.sha256(archive.read_bytes()).hexdigest(), TAG='v' + version)
        (self.bin / 'curl').write_text('''#!PYTHON
import json, os, pathlib, shutil, sys
a=sys.argv[1:]
with open(os.environ['DOWNLOADS'], 'a') as f: f.write(' '.join(a) + '\\n')
archive=pathlib.Path(os.environ['ARCHIVE'])
if '-o' not in a:
 print(json.dumps({'tag_name':os.environ['TAG'], 'assets':[{'browser_download_url':'https://example.test/'+archive.name}]}, indent=2))
else:
 dest=pathlib.Path(a[a.index('-o')+1])
 if dest.name == 'install.sh': shutil.copyfile(os.environ['FIXTURE'], dest)
 elif dest.name.endswith('.sha256'): dest.write_text(os.environ['CHECKSUM']+'  '+archive.name+'\\n')
 else: shutil.copyfile(archive, dest)
'''.replace('PYTHON', sys.executable)); (self.bin / 'curl').chmod(0o755)

    def installed(self, version):
        dest = self.home / '.local/bin/ripwire'
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f'#!/bin/sh\necho ripwire {version}\n'); dest.chmod(0o755)
        return dest

    def test_upgrade_replaces_the_ripwire_forge_installed_with_the_latest_release(self):
        dest = self.installed('0.4.0')
        self.env['PATH'] = str(dest.parent) + ':' + self.env['PATH']
        self.release('0.6.10')
        result = self.run_install('--upgrade', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('0.6.10', subprocess.run([str(dest), '--version'], text=True, stdout=subprocess.PIPE).stdout)
        # The installer was fetched at the resolved tag, not a pinned one.
        self.assertIn('/v0.6.10/scripts/install.sh', (self.root / 'downloads').read_text())

    def test_without_upgrade_an_existing_ripwire_is_kept(self):
        dest = self.installed('0.4.0')
        self.release('0.6.10')
        self.assertEqual(self.run_install('--yes').returncode, 0)
        self.assertIn('0.4.0', dest.read_text())
        self.assertFalse((self.root / 'downloads').exists())

    def test_a_ripwire_installed_elsewhere_is_reported_and_never_replaced(self):
        foreign = self.bin / 'ripwire'
        foreign.write_text('#!/bin/sh\necho ripwire 0.4.0\n'); foreign.chmod(0o755)
        self.release('0.6.10')
        result = self.run_install('--upgrade', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('was not installed by forge', result.stdout)
        self.assertIn('0.4.0', foreign.read_text())
        self.assertFalse((self.home / '.local/bin/ripwire').exists())
        self.assertFalse((self.root / 'downloads').exists())

    def test_an_up_to_date_ripwire_downloads_nothing(self):
        self.installed('0.6.10')
        self.release('0.6.10')
        result = self.run_install('--upgrade', '--yes')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('nothing to update', result.stdout)
        self.assertFalse((self.root / 'downloads').exists())

    def test_a_pinned_version_is_installed_and_a_bad_one_refused(self):
        self.release('0.6.2')
        result = self.run_install('--yes', '--version', '0.6.2')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('0.6.2', (self.home / '.local/bin/ripwire').read_text())
        self.assertEqual(self.run_install('--yes', '--version', 'latest-ish').returncode, 2)


class RipwireReleaseCheckTests(unittest.TestCase):
    """The latest release, the version gate, the daily cache and the end-of-run notice (#170)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'home'
        env = {'HOME': str(self.home), 'XDG_STATE_HOME': str(self.root / 'state'),
               'FORGE_RIPWIRE_API': fake_api(self.root, 'v0.7.0'), 'FORGE_RIPWIRE': '',
               'FORGE_UPDATE_CHECK': 'on', 'CI': ''}
        patcher = patch.dict(os.environ, env)
        patcher.start(); self.addCleanup(patcher.stop)

    def binary(self, version, where=None):
        path = where or (self.home / '.local/bin/ripwire')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\necho ripwire {version}\n'); path.chmod(0o755)
        return path

    def test_the_gate_accepts_0_6_and_later(self):
        for text, ok in [('ripwire 0.6.0', True), ('ripwire 0.7.0', True), ('ripwire 0.10.1', True),
                         ('ripwire 0.5.9', False), ('ripwire 0.4.0', False), ('ripwire', False), ('', False)]:
            self.assertEqual(rw.supported(text), ok, text)

    def test_the_latest_tag_comes_from_the_releases_api_and_stops_at_0_6_1_on_an_intel_mac(self):
        self.assertEqual(rw.latest_tag(), 'v0.7.0')
        with patch.object(rw.platform, 'system', return_value='Darwin'), \
                patch.object(rw.platform, 'machine', return_value='x86_64'):
            self.assertEqual(rw.latest_tag(), 'v0.6.1')
        with patch.object(rw.platform, 'system', return_value='Darwin'), \
                patch.object(rw.platform, 'machine', return_value='arm64'):
            self.assertEqual(rw.latest_tag(), 'v0.7.0')
        os.environ['FORGE_RIPWIRE_API'] = (self.root / 'nowhere').as_uri()
        with self.assertRaises(RuntimeError):
            rw.latest_tag()

    def test_the_check_is_cached_for_a_day_and_offline_keeps_the_last_answer(self):
        self.assertEqual(rw.cached_latest()[0], 'v0.7.0')
        fake_api(self.root, 'v0.8.0')
        self.assertEqual(rw.cached_latest()[0], 'v0.7.0')          # within the day: not asked again
        self.assertEqual(rw.cached_latest(force=True)[0], 'v0.8.0')
        os.environ['FORGE_RIPWIRE_API'] = (self.root / 'nowhere').as_uri()
        latest, state = rw.cached_latest(force=True)
        self.assertEqual(latest, 'v0.8.0')
        self.assertFalse(state['reached'])

    def notice(self, answer=None):
        printed = []
        with patch.object(rw, '_ask', return_value=answer) as ask, \
                patch.object(rw.subprocess, 'run', wraps=rw.subprocess.run) as run, \
                patch('builtins.print', lambda *a, **k: printed.append(' '.join(map(str, a)))):
            rw.notice('/nonexistent/installer.sh')
        installs = [c for c in run.call_args_list if c.args and c.args[0][0] == '/bin/bash']
        return printed, ask.called, installs

    def test_without_a_terminal_the_notice_prints_the_command_once_a_day(self):
        self.binary('0.4.0')
        with patch.object(rw, 'resolve', return_value=str((self.home / '.local/bin/ripwire').resolve())):
            printed, asked, installs = self.notice(answer=None)
            self.assertEqual(printed, ['forge: Ripwire 0.7.0 is available (you have 0.4.0): forge ripwire update'])
            self.assertEqual(installs, [])
            self.assertEqual(self.notice(answer=None)[0], [])          # told today already

    def test_on_a_terminal_a_yes_runs_the_upgrade_and_a_no_changes_nothing(self):
        self.binary('0.4.0')
        with patch.object(rw, 'resolve', return_value=str((self.home / '.local/bin/ripwire').resolve())):
            printed, asked, installs = self.notice(answer=False)
            self.assertTrue(asked)
            self.assertEqual((printed, installs), ([], []))
            rw.state_file().unlink()
            printed, asked, installs = self.notice(answer=True)
            self.assertEqual(installs[0].args[0], ['/bin/bash', '/nonexistent/installer.sh', '--upgrade', '--yes',
                                                   '--version', 'v0.7.0'])

    def test_the_notice_is_silent_when_off_in_ci_up_to_date_offline_or_missing(self):
        managed = str((self.home / '.local/bin/ripwire'))
        cases = [('off', dict(FORGE_RIPWIRE='off'), '0.4.0'), ('no update check', dict(FORGE_UPDATE_CHECK='off'), '0.4.0'),
                 ('ci', dict(CI='true'), '0.4.0'), ('up to date', {}, '0.7.0'),
                 ('offline', dict(FORGE_RIPWIRE_API=(self.root / 'nowhere').as_uri()), '0.4.0')]
        for label, env, version in cases:
            with self.subTest(label), patch.dict(os.environ, env):
                rw.state_file().unlink(missing_ok=True)
                self.binary(version)
                with patch.object(rw, 'resolve', return_value=str(Path(managed).resolve())):
                    printed, asked, installs = self.notice(answer=True)
                self.assertEqual((printed, asked, installs), ([], False, []))
        with patch.object(rw, 'resolve', return_value=None):
            self.assertEqual(self.notice(answer=True)[:2], ([], False))

    def test_a_ripwire_installed_outside_forge_is_reported_never_updated(self):
        foreign = self.binary('0.4.0', where=self.root / 'opt/ripwire')
        with patch.object(rw, 'resolve', return_value=str(foreign)):
            printed, asked, installs = self.notice(answer=True)
        self.assertFalse(asked)
        self.assertEqual(installs, [])
        self.assertIn('installed outside forge', printed[0])
