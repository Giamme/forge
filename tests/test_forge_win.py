"""scripts/forge-win.sh: Windows MSYS2 mode is opt-in, and the file does nothing anywhere else.

Every entry script sources forge-win.sh first. Off Windows, and on Windows in native mode (the
default), it must return at once and start no process. These tests source it under a simulated
OSTYPE, so they run on Linux, macOS and Git Bash alike.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WIN = ROOT / 'scripts' / 'forge-win.sh'
BASH = shutil.which('bash') or '/bin/bash'
# A real MSYS2 (pacman, and no git-bash.exe at the root) takes the MSYS2 branch whatever the mode,
# so the Git Bash cases cannot be simulated there.
IN_MSYS2 = os.path.exists('/usr/bin/pacman') and not os.path.exists('/git-bash.exe')
REPORT = ('OSTYPE="$1"; . "$2"; echo "rc=$?"; echo "agent_path=${FORGE_WIN_AGENT_PATH-unset}"; '
          'if declare -F pkill >/dev/null; then echo pkill=function; else echo pkill=command; fi; '
          'echo "mode=${FORGE_WIN_MODE-unset}"')


def source(ostype, mode=None, **extra):
    """Source forge-win.sh in a fresh bash that believes it runs on `ostype`."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('FORGE_WIN_') and k != 'FORGE_MSYS2_ROOT'}
    if mode is not None:
        env['FORGE_WIN_MODE'] = mode
    env.update(extra)
    return subprocess.run([BASH, '-c', REPORT, 'forge-win-test', ostype, str(WIN)],
                          capture_output=True, text=True, env=env, timeout=60)


class ForgeWinTests(unittest.TestCase):
    def assert_untouched(self, result, mode):
        self.assertEqual((result.returncode, result.stderr), (0, ''))
        self.assertEqual(result.stdout.split(),
                         ['rc=0', 'agent_path=unset', 'pkill=command', 'mode=%s' % (mode or 'unset')])

    def test_off_windows_it_does_nothing_whatever_the_mode(self):
        for ostype in ('linux-gnu', 'darwin23'):
            for mode in (None, 'msys2', 'not-a-mode'):
                with self.subTest(ostype=ostype, mode=mode):
                    self.assert_untouched(source(ostype, mode), mode)

    @unittest.skipIf(IN_MSYS2, 'inside MSYS2 forge always runs in MSYS2 mode')
    def test_native_is_the_default_in_git_bash_and_starts_no_process(self):
        for mode in (None, 'native'):
            with self.subTest(mode=mode):
                # No command can be found: the native path may only use builtins and file tests.
                self.assert_untouched(source('msys', mode, PATH=os.path.join(ROOT, 'no-such-dir')), mode)

    @unittest.skipUnless(IN_MSYS2, 'needs a real MSYS2')
    def test_an_msys2_shell_runs_msys2_mode_and_says_so(self):
        # Helpers that adapt forge to native Windows read FORGE_WIN_MODE to tell MSYS2 mode apart.
        for mode in (None, 'native', 'msys2'):
            with self.subTest(mode=mode):
                result = source('msys', mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split()[2:], ['pkill=function', 'mode=msys2'])

    @unittest.skipUnless(IN_MSYS2, 'needs a real MSYS2')
    def test_msys2_tools_come_first_and_agents_keep_the_shell_path(self):
        # A UCRT64 or MINGW64 shell puts its own bin first, where a MinGW python3 is a native one.
        script = ('. "$1"; command -v python3; echo "$PATH"; echo "$FORGE_WIN_AGENT_PATH"; '
                  'echo "$FORGE_WIN_MSYSTEM"; echo "$MSYS"; . "$1"; echo "$PATH"; echo "$MSYS"')
        with tempfile.TemporaryDirectory() as mingw:
            fake = os.path.join(mingw, 'python3')
            with open(fake, 'w') as f:
                f.write('#!/bin/sh\necho mingw\n')
            os.chmod(fake, 0o755)
            shell_path = '%s:/usr/bin' % mingw
            for msys, expected in ((None, 'winsymlinks:native'),
                                   ('error_start:x', 'error_start:x winsymlinks:native'),
                                   ('winsymlinks:nativestrict', 'winsymlinks:nativestrict')):
                with self.subTest(msys=msys):
                    env = {k: v for k, v in os.environ.items() if not k.startswith('FORGE_WIN_') and k != 'MSYS'}
                    env.update(PATH=shell_path, MSYSTEM='UCRT64')
                    if msys is not None:
                        env['MSYS'] = msys
                    result = subprocess.run(['/usr/bin/bash', '-c', script, 'forge-win-test', str(WIN)],
                                            capture_output=True, text=True, env=env, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    # Sourced again by a child script, nothing changes.
                    self.assertEqual(result.stdout.splitlines(), [
                        '/usr/bin/python3', '/usr/bin:' + shell_path, shell_path, 'UCRT64', expected,
                        '/usr/bin:' + shell_path, expected])

    def test_an_unknown_mode_is_refused(self):
        result = source('msys', 'MSYS2')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("FORGE_WIN_MODE is 'MSYS2'; use native (the default) or msys2", result.stderr)

    @unittest.skipIf(IN_MSYS2, 'inside MSYS2 there is nothing to hand over to')
    def test_msys2_mode_without_msys2_says_where_it_looked(self):
        with tempfile.TemporaryDirectory() as empty:
            result = source('msys', 'msys2', FORGE_MSYS2_ROOT=empty)
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertIn('MSYS2 was not found at %s (set FORGE_MSYS2_ROOT)' % empty.rstrip('/\\'), result.stderr)

    def test_it_keeps_to_bash_3_2(self):
        # It is sourced by macOS's system Bash too (CONTRIBUTING.md): no Bash 4 syntax outside comments.
        code = [line for line in WIN.read_text(encoding='utf-8').splitlines() if not line.lstrip().startswith('#')]
        for construct in ('mapfile', 'readarray', 'declare -A', ',,}', '^^}', '|&'):
            with self.subTest(construct=construct):
                self.assertFalse([line for line in code if construct in line])


if __name__ == '__main__':
    unittest.main()
