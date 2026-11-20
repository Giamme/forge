"""Tests for cross-platform shell helpers, Windows entrypoints/shims, and MSYS path preservation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
ENV_LIB = SCRIPTS / 'forge-lib-env.sh'

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import forge_posix  # noqa: E402
import forge_proc  # noqa: E402
import forge_registry  # noqa: E402


def _find_bash() -> str | None:
    if os.name != 'nt':
        return shutil.which('bash')
    return forge_proc.bash_executable()


class WindowsEnvTests(unittest.TestCase):
    """forge-lib-env.sh, MSYS path normalization, and Windows installer helpers."""

    def setUp(self) -> None:
        self.bash = _find_bash()
        if not self.bash:
            self.skipTest('Git Bash not available')
        self.dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.dir.name)

    def tearDown(self) -> None:
        self.dir.cleanup()

    def _to_bash_path(self, path: Path) -> str:
        text = str(path)
        if os.name != 'nt':
            return text
        if len(text) >= 2 and text[1] == ':':
            drive = text[0].lower()
            rest = text[2:].replace('\\', '/')
            return f'/{drive}{rest}'
        return text.replace('\\', '/')

    @unittest.skipUnless(os.name == 'nt', 'Windows python3 shim creation test')
    def test_replaces_windowsapps_python3_stub_with_per_user_shim(self) -> None:
        stub_dir = self.tmp / 'WindowsApps'
        real_dir = self.tmp / 'realbin'
        localappdata = self.tmp / 'localappdata'
        stub_dir.mkdir()
        real_dir.mkdir()
        localappdata.mkdir()

        stub = stub_dir / 'python3'
        stub.write_text('#!/usr/bin/env bash\necho "store stub" >&2\nexit 49\n', encoding='utf-8')
        os.chmod(stub, 0o755)

        real_py = real_dir / 'python'
        real_py.write_text(f'#!/usr/bin/env bash\nexec "{self._to_bash_path(Path(sys.executable))}" "$@"\n', encoding='utf-8')
        os.chmod(real_py, 0o755)

        env_lib = self._to_bash_path(ENV_LIB)
        stub_b = self._to_bash_path(stub_dir)
        real_b = self._to_bash_path(real_dir)
        sys_b = '/usr/bin:/bin'

        env = dict(os.environ, LOCALAPPDATA=str(localappdata), PATH=f'{stub_dir}{os.pathsep}{real_dir}')
        cmd = (
            f'export PATH="{stub_b}:{real_b}:{sys_b}"; '
            f'source "{env_lib}"; '
            'forge_ensure_python3; '
            'python3 -c "import sys; print(sys.version_info[0])"'
        )
        done = subprocess.run([self.bash, '-c', cmd], capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), '3')
        self.assertTrue((localappdata / 'forge' / 'bin' / 'python3').is_file())
        self.assertTrue((localappdata / 'forge' / 'bin' / 'python3.cmd').is_file())

    def test_posix_ensure_python3_runs_zero_external_commands(self) -> None:
        env_lib = self._to_bash_path(ENV_LIB)
        done = subprocess.run(
            [self.bash, '-c', f'OSTYPE=linux-gnu; PATH=/nonexistent; source "{env_lib}"; forge_ensure_python3'],
            capture_output=True, text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_forge_is_windows_follows_ostype_and_stands_aside_in_msys2_mode(self) -> None:
        env_lib = self._to_bash_path(ENV_LIB)
        cases = (('linux-gnu', '', 1), ('darwin23', '', 1), ('msys', '', 0), ('msys', 'native', 0),
                 ('cygwin', '', 0), ('msys', 'msys2', 1), ('cygwin', 'msys2', 1))
        for ostype, mode, expected in cases:
            with self.subTest(ostype=ostype, mode=mode):
                done = subprocess.run(
                    [self.bash, '-c', f'OSTYPE={ostype}; FORGE_WIN_MODE={mode}; source "{env_lib}"; forge_is_windows'],
                    capture_output=True, text=True,
                )
                self.assertEqual(done.returncode, expected, done.stderr)

    def test_install_links_claude_codex_and_antigravity_skills_idempotently(self) -> None:
        home = self.tmp / 'home'
        claude_skills = home / '.claude' / 'skills'
        codex_skills = home / '.codex' / 'skills'
        agy_skills = home / '.gemini' / 'config' / 'skills'
        for p in (claude_skills, codex_skills, agy_skills):
            p.mkdir(parents=True)

        env = dict(os.environ, HOME=self._to_bash_path(home), FORGE_RIPWIRE='off')
        if os.name == 'nt':
            env['USERPROFILE'] = str(home)
        else:
            env['PATH'] = '/usr/local/bin:/usr/bin:/bin'
        installer = self._to_bash_path(ROOT / 'scripts' / 'forge-install.sh')

        first = subprocess.run([self.bash, '-c', f'"{installer}" < /dev/null'], capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue((claude_skills / 'forge' / 'SKILL.md').is_file(), first.stdout)
        self.assertTrue((codex_skills / 'forge' / 'SKILL.md').is_file(), first.stdout)
        agy_forge = agy_skills / 'forge'
        if os.name == 'nt':
            # Windows: a real copy in Antigravity's skills directory, which it reads without a plugin.
            self.assertTrue((agy_forge / 'SKILL.md').is_file(), first.stdout)
            self.assertFalse(agy_forge.is_symlink())
            if hasattr(agy_forge, 'is_junction'):
                self.assertFalse(agy_forge.is_junction())
        else:
            # macOS/Linux: Antigravity loads skills only from plugins, and agy is not on this PATH.
            self.assertFalse(os.path.lexists(agy_forge), first.stdout)
            self.assertIn('antigravity: skipped (agy not on PATH)', first.stdout)

        second = subprocess.run([self.bash, '-c', f'"{installer}" < /dev/null'], capture_output=True, text=True, env=env)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('already linked', second.stdout)
        self.assertEqual((agy_forge / 'SKILL.md').is_file(), os.name == 'nt', second.stdout)

    def test_install_junction_under_temp_is_idempotent_and_copy_preserves_checkout(self) -> None:
        checkout = self.tmp / 'checkout'
        (checkout / 'scripts').mkdir(parents=True)
        for name in ('forge-install.sh', 'forge-lib-env.sh'):
            shutil.copy2(SCRIPTS / name, checkout / 'scripts' / name)
        (checkout / 'SKILL.md').write_text('# Forge\n', encoding='utf-8')
        sentinel = checkout / 'SENTINEL.txt'
        sentinel.write_text('keep checkout intact\n', encoding='utf-8')

        home = self.tmp / 'home_tmp'
        claude_skills = home / '.claude' / 'skills'
        agy_skills = home / '.gemini' / 'config' / 'skills'
        claude_skills.mkdir(parents=True)
        agy_skills.mkdir(parents=True)

        installer = self._to_bash_path(checkout / 'scripts' / 'forge-install.sh')
        env = dict(os.environ, HOME=self._to_bash_path(home), FORGE_RIPWIRE='off')
        if os.name == 'nt':
            env['USERPROFILE'] = str(home)
            # Pre-seed Antigravity with a junction pointing at the checkout, and Claude with a junction
            # created via /tmp/<name> (mirroring Git Bash /tmp mount aliases resolved by readlink).
            checkout_sub = '/tmp/' + Path(self.dir.name).name + '/checkout'
            seed = subprocess.run(
                [self.bash, '-c',
                 f'source "{self._to_bash_path(checkout / "scripts" / "forge-lib-env.sh")}"; '
                 f'forge_link_dir "{installer.rsplit("/scripts/", 1)[0]}" "{self._to_bash_path(agy_skills / "forge")}"; '
                 f'if [ -d "{checkout_sub}" ]; then forge_link_dir "{checkout_sub}" "{self._to_bash_path(claude_skills / "forge")}"; fi'],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(seed.returncode, 0, seed.stdout + seed.stderr)
        else:
            env['PATH'] = '/usr/local/bin:/usr/bin:/bin'
            (claude_skills / 'forge').symlink_to(os.path.relpath(checkout, claude_skills))

        first = subprocess.run([self.bash, '-c', f'"{installer}" < /dev/null'], capture_output=True, text=True, env=env)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(sentinel.is_file(), 'checkout sentinel must survive copy_skill_dir removing prior junction')
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep checkout intact\n')
        self.assertIn('claude    : already linked', first.stdout)
        if os.name == 'nt':
            self.assertTrue((agy_skills / 'forge' / '.forge-installed').is_file(), first.stdout)
            self.assertFalse((agy_skills / 'forge').is_symlink())

        second = subprocess.run([self.bash, '-c', f'"{installer}" < /dev/null'], capture_output=True, text=True, env=env)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('claude    : already linked', second.stdout)
        self.assertNotIn('linked (junction)', second.stdout)

    def test_doctor_win_fallbacks_and_msys_pathconv_preservation(self) -> None:
        dispatch = self._to_bash_path(SCRIPTS / 'forge-dispatch.sh')
        repo = self.tmp / 'repo'
        repo.mkdir()
        run_dir = self.tmp / 'run'
        run_dir.mkdir()
        prompt = run_dir / 'prompt.md'
        # Leading slash and --flag=/path must stay literal under Windows MSYS Git Bash.
        prompt.write_text('/slash-command --flag=/literal/path\n', encoding='utf-8')
        fake_bin = self.tmp / 'bin'
        fake_bin.mkdir()
        captured = run_dir / 'captured.txt'
        help_line = '--model --print --print-timeout --effort --add-dir --mode --dangerously-skip-permissions'

        if os.name == 'nt':
            local = self.tmp / 'local'
            oc_exe = local / 'Programs' / '@opencode-aidesktop' / 'resources' / 'opencode-cli.exe'
            oc_exe.parent.mkdir(parents=True)
            oc_exe.write_bytes(b'')
            appdata = self.tmp / 'roaming'
            oc_cmd = appdata / 'npm' / 'openclaude.cmd'
            oc_cmd.parent.mkdir(parents=True)
            oc_cmd.write_text('@echo off\r\nexit /b 0\r\n', encoding='utf-8')

            sys32 = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
            git_root = Path(self.bash).resolve().parent.parent
            clean_path = ';'.join([
                os.path.dirname(sys.executable),
                os.path.dirname(self.bash),
                str(git_root / 'usr' / 'bin'),
                str(git_root / 'cmd'),
                sys32,
            ])
            doc_env = dict(
                os.environ,
                LOCALAPPDATA=str(local),
                APPDATA=str(appdata),
                PATH=clean_path,
                FORGE_DATASET='off',
                FORGE_RIPWIRE='off',
                FORGE_JEV='off',
                FORGE_RUN_REGISTRY='off',
            )
            doc = subprocess.run([self.bash, dispatch, 'doctor'], capture_output=True, text=True, env=doc_env)
            self.assertIn('opencode-cli.exe', doc.stdout + doc.stderr)
            self.assertIn('openclaude.cmd', doc.stdout + doc.stderr)

            agy_stub = fake_bin / 'agy.cmd'
            agy_stub.write_text(
                '@echo off\r\n'
                'if "%~1"=="--help" (\r\n'
                f'  echo {help_line}\r\n'
                '  exit /b 0\r\n'
                ')\r\n'
                f'"{sys.executable}" -c "import json, pathlib, sys; '
                f'pathlib.Path(r\'{captured}\').write_text(json.dumps(sys.argv[1:]))" %*\r\n'
                'echo AGY_OK\r\n',
                encoding='utf-8',
            )
            env = dict(doc_env, PATH=f'{fake_bin};{doc_env["PATH"]}')
        else:
            agy_stub = fake_bin / 'agy'
            agy_stub.write_text(
                '#!/usr/bin/env bash\n'
                'if [ "${1:-}" = --help ]; then\n'
                f'  echo "{help_line}"\n'
                '  exit 0\n'
                'fi\n'
                f'"{sys.executable}" -c "import json, pathlib, sys; '
                f'pathlib.Path(\'{captured}\').write_text(json.dumps(sys.argv[1:]))" "$@"\n'
                'echo AGY_OK\n',
                encoding='utf-8',
            )
            agy_stub.chmod(0o755)
            env = dict(
                os.environ,
                PATH=f'{fake_bin}:{os.environ["PATH"]}',
                FORGE_DATASET='off',
                FORGE_RIPWIRE='off',
                FORGE_JEV='off',
                FORGE_RUN_REGISTRY='off',
            )

        done = subprocess.run(
            [self.bash, dispatch, 'dwarf', 'gemini-3-flash:low:antigravity',
             '--repo', self._to_bash_path(repo),
             '--run-dir', self._to_bash_path(run_dir),
             '--prompt-file', self._to_bash_path(prompt)],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        argv = json.loads(captured.read_text(encoding='utf-8'))
        print_args = [a for a in argv if a.startswith('--print=')]
        self.assertTrue(print_args, argv)
        self.assertIn('/slash-command', print_args[0])
        self.assertIn('--flag=/literal/path', print_args[0])
        if os.name == 'nt':
            add_dirs = [argv[i + 1] for i, a in enumerate(argv) if a == '--add-dir' and i + 1 < len(argv)]
            self.assertEqual(len(add_dirs), 2, argv)
            for d in add_dirs:
                self.assertRegex(d, r'^[A-Za-z]:[/\\]', argv)

    def test_host_path_and_registry_msys_repo(self) -> None:
        if os.name == 'nt':
            self.assertEqual(forge_posix.host_path('/c/Users/dev/repo'), 'C:/Users/dev/repo')
            run_dir = self.tmp / 'run_reg'
            run_dir.mkdir()
            (run_dir / 'repo').write_text('/c/Users/dev/repo\n', encoding='utf-8')
            self.assertEqual(forge_registry._repo_of(run_dir), Path('C:/Users/dev/repo'))
        else:
            self.assertEqual(forge_posix.host_path('/tmp/repo'), '/tmp/repo')


if __name__ == '__main__':
    unittest.main()
