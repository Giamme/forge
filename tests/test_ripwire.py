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

spec = importlib.util.spec_from_file_location('ripwire', ROOT / 'scripts/forge-ripwire.py')
rw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rw)
FAKE_RW = '''#!/usr/bin/env python3
import json, os, pathlib, sys
if '--version' in sys.argv:
 print('ripwire 0.4.0'); sys.exit()
with open(os.environ['RW_CALLS'], 'a') as f:
 f.write(json.dumps({'argv':sys.argv, 'cwd':os.getcwd()})+'\\n')
print('<context>条件 fresh evidence</context>')
'''


class RipwireIntegrationTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
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
            for body, reason in [('echo ripwire 9.0.0', 'incompatible'),
                                 ('if [ "$1" = --version ]; then echo 0.4.0; else exit 6; fi', 'exit 6'),
                                 ('if [ "$1" = --version ]; then echo 0.4.0; fi', 'empty')]:
                prompt.write_bytes(b'original\r\n')
                binary.write_text('#!/bin/sh\n' + body); binary.chmod(0o755)
                with patch.object(rw, 'resolve', return_value=str(binary)), patch.dict(os.environ, {'FORGE_RIPWIRE': ''}):
                    rw.prepare(args)
                self.assertEqual(prompt.read_bytes(), b'original\r\n')
                self.assertIn(reason, json.loads((root / 'ripwire.json').read_text())['reason'])
            with patch.object(rw, 'resolve', return_value=None), patch.dict(os.environ, {'FORGE_RIPWIRE': ''}):
                rw.prepare(args)
            self.assertIn('missing', json.loads((root / 'ripwire.json').read_text())['reason'])

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

    def test_checksum_and_upstream_failure_propagate(self):
        self.env['BAD_INSTALLER'] = '1'
        result = self.run_install('--yes')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stdout)
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
        name = f'ripwire-0.4.0-{os_name}-{arch}'
        bundle = archive_root / name; bundle.mkdir(parents=True)
        binary = bundle / 'ripwire'; binary.write_text('#!/bin/sh\necho ripwire 0.4.0\n'); binary.chmod(0o755)
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
 print(json.dumps({'tag_name':'v0.4.0', 'assets':[{'browser_download_url':'https://example.test/'+archive.name}]}, indent=2))
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
