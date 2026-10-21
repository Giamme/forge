import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SkillChecks(unittest.TestCase):
    def test_reference_links_resolve(self):
        for path in [ROOT/'SKILL.md', *ROOT.glob('references/*.md')]:
            for link in re.findall(r'\]\(([^)]+)\)', path.read_text()):
                if '://' not in link and not link.startswith('#'):
                    self.assertTrue((path.parent/link.split('#')[0]).exists(), (path, link))

    def test_evaluation_cases_are_actionable(self):
        cases = json.loads((ROOT/'tests/skill-cases.json').read_text())
        self.assertTrue(any(case['dispatch'] for case in cases))
        self.assertTrue(any(not case['dispatch'] for case in cases))
        for case in cases:
            self.assertTrue(case['request'])
            self.assertTrue(case['expected'])
            self.assertIsInstance(case['dispatch'], bool)


class LauncherTests(unittest.TestCase):
    """`./forge`, the repository-local launcher: asking for help is not a usage error."""

    def setUp(self):
        # `forge install --help` reaches the installer: if it ever installed, it would relink
        # the developer's real harnesses, so every call runs in a throwaway HOME.
        home = tempfile.TemporaryDirectory(); self.addCleanup(home.cleanup)
        self.env = dict(os.environ, HOME=home.name)

    def forge(self, *args):
        return subprocess.run([str(ROOT / 'forge'), *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, env=self.env)

    def test_help_succeeds_on_stdout_and_mistakes_fail_on_stderr(self):
        for args, code in ((['--help'], 0), (['-h'], 0), (['help'], 0), ([], 2), (['frobnicate'], 2)):
            with self.subTest(args):
                result = self.forge(*args)
                self.assertEqual(result.returncode, code, result.stderr)
                shown, quiet = (result.stdout, result.stderr) if code == 0 else (result.stderr, result.stdout)
                self.assertIn('usage: forge', shown)
                self.assertEqual(quiet, '')
        self.assertIn('frobnicate', self.forge('frobnicate').stderr, 'the error names what was typed')

    def test_every_command_the_help_lists_is_routed(self):
        listed = re.findall(r'^  ([a-z]+) ', self.forge('--help').stdout, re.M)
        self.assertTrue(listed)
        for name in listed:
            with self.subTest(name):
                result = self.forge(name, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'usage: forge {name}', result.stdout)
