"""Tests for cross-platform CLI entrypoints (`bin/forge.js` and `forge.cmd`)."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE_JS = ROOT / 'bin' / 'forge.js'
FORGE_CMD = ROOT / 'forge.cmd'


class EntrypointTests(unittest.TestCase):
    def test_node_bin_forge_js_help_and_exit_code(self) -> None:
        node = shutil.which('node')
        if not node:
            self.skipTest('node not available')
        done = subprocess.run([node, str(FORGE_JS), '--help'], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('usage: forge <command>', done.stdout)
        bad = subprocess.run([node, str(FORGE_JS), '__no_such_command__'], capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 2)

    def test_node_bin_forge_js_filters_windows_bash_stubs(self) -> None:
        node = shutil.which('node')
        if not node:
            self.skipTest('node not available')
        src = FORGE_JS.read_text(encoding='utf-8')
        check = (
            'const s = require("fs").readFileSync(process.argv[1], "utf8").replace(/^#!.*\\r?\\n/, "");'
            'const fn = new Function(s.replace(/const bash = findBash\\(\\);[\\s\\S]*/, "return {isWindowsBashStub};"));'
            'const {isWindowsBashStub} = fn();'
            'if (!isWindowsBashStub("C:\\\\Windows\\\\System32\\\\bash.exe")) process.exit(2);'
            'if (!isWindowsBashStub("C:\\\\Users\\\\x\\\\AppData\\\\Local\\\\Microsoft\\\\WindowsApps\\\\bash.exe")) process.exit(3);'
            'if (isWindowsBashStub("C:\\\\Program Files\\\\Git\\\\bin\\\\bash.exe")) process.exit(4);'
        )
        self.assertIn('isWindowsBashStub', src)
        done = subprocess.run([node, '-e', check, str(FORGE_JS)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    @unittest.skipUnless(os.name == 'nt', 'Windows CMD entrypoint test')
    def test_forge_cmd_help_and_exit_code_on_windows(self) -> None:
        done = subprocess.run(['cmd.exe', '/c', str(FORGE_CMD), '--help'], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('usage: forge <command>', done.stdout)
        bad = subprocess.run(['cmd.exe', '/c', str(FORGE_CMD), '__no_such_command__'], capture_output=True, text=True, timeout=60)
        self.assertEqual(bad.returncode, 2)


if __name__ == '__main__':
    unittest.main()
