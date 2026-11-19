"""Automated cross-platform guardrails for Windows, macOS, and Linux."""
from __future__ import annotations

import ast
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import forge_posix  # noqa: E402

# Adapter modules that intentionally encapsulate platform-specific OS/POSIX primitives.
ADAPTER_MODULES = {'forge_flock.py', 'forge_posix.py', 'forge_proc.py'}

# Functions allowed to keep a POSIX-only fallback behind `if forge_proc.IS_WINDOWS:`.
EXEC_ALLOWLIST = {
    ('scripts/forge-guard.py', 'session'),
}


def _production_py_files() -> list[Path]:
    files: list[Path] = []
    for base in (SCRIPTS, ROOT / 'ui' / 'forge_ui'):
        for path in sorted(base.rglob('*.py')):
            if path.name in ADAPTER_MODULES:
                continue
            files.append(path)
    return files


def _enclosing_func(tree: ast.AST, target: ast.AST) -> str | None:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    cur: ast.AST | None = target
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
        cur = parents.get(id(cur))
    return None


class CrossPlatformGuardrailTests(unittest.TestCase):
    def test_no_crlf_in_adapter_and_modified_python_sources(self):
        candidates: list[Path] = [
            *sorted(SCRIPTS.rglob('*.py')),
            *sorted((ROOT / 'ui' / 'forge_ui').rglob('*.py')),
        ]
        crlf_files = [
            str(p.relative_to(ROOT)).replace('\\', '/')
            for p in candidates
            if p.is_file() and b'\r\n' in p.read_bytes()
        ]
        self.assertEqual(crlf_files, [], f'CRLF line endings found: {crlf_files}')

    def test_no_unguarded_posix_primitives_in_production_python(self):
        violations: list[str] = []
        forbidden_os = {'uname', 'execv', 'execve', 'execvp', 'execvpe',
                        'O_NOFOLLOW', 'O_NONBLOCK', 'O_CLOEXEC', 'O_DIRECTORY'}
        forbidden_sig = {'SIGKILL', 'SIGQUIT', 'SIGUSR1', 'SIGUSR2'}

        for path in _production_py_files():
            rel = str(path.relative_to(ROOT)).replace('\\', '/')
            source = path.read_text(encoding='utf-8')
            tree = ast.parse(source, filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == 'fcntl':
                            violations.append(f'{rel}:{node.lineno}: direct import fcntl (use forge_flock)')
                elif isinstance(node, ast.ImportFrom):
                    if node.module == 'fcntl':
                        violations.append(f'{rel}:{node.lineno}: direct from fcntl import (use forge_flock)')
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id == 'os' and node.attr in forbidden_os:
                        fn = _enclosing_func(tree, node)
                        if node.attr.startswith('execv') and (rel, fn) in EXEC_ALLOWLIST:
                            continue
                        violations.append(f'{rel}:{node.lineno}: direct os.{node.attr}')
                    elif node.value.id == 'signal' and node.attr in forbidden_sig:
                        violations.append(f'{rel}:{node.lineno}: direct signal.{node.attr} (use forge_proc)')

        self.assertEqual(violations, [], '\n'.join(violations))

    def test_open_nofollow_enforces_symlink_and_reparse_protection(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target_dir = root / 'real_dir'
            target_dir.mkdir()
            if os.name == 'nt':
                junction = root / 'dir_junction'
                mk = subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction), str(target_dir)],
                                    capture_output=True, text=True)
                self.assertEqual(mk.returncode, 0, mk.stdout + mk.stderr)
                with self.assertRaises(OSError) as ctx:
                    forge_posix.open_dir(junction, nofollow=True)
                self.assertEqual(ctx.exception.errno, errno.ELOOP)
            target = root / 'target.txt'
            target.write_text('secret\n', encoding='utf-8')
            link = root / 'link.txt'
            try:
                link.symlink_to(target)
            except OSError:
                return
            with self.assertRaises(OSError) as ctx:
                # Even without O_NOFOLLOW in caller flags, open_nofollow enforces it.
                forge_posix.open_nofollow(link, os.O_RDONLY)
            self.assertEqual(ctx.exception.errno, errno.ELOOP)


if __name__ == '__main__':
    unittest.main()
