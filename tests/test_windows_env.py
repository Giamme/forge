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
        self.assertTrue((agy_skills / 'forge' / 'SKILL.md').is_file(), first.stdout)
        if os.name == 'nt':
            agy_forge = agy_skills / 'forge'
            self.assertFalse(agy_forge.is_symlink())
            if hasattr(agy_forge, 'is_junction'):
                self.assertFalse(agy_forge.is_junction())

        second = subprocess.run([self.bash, '-c', f'"{installer}" < /dev/null'], capture_output=True, text=True, env=env)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('already linked', second.stdout)
        self.assertTrue((agy_skills / 'forge' / 'SKILL.md').is_file(), second.stdout)

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
