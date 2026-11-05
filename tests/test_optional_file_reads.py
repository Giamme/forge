"""Absent optional task files must not print shell "No such file" noise on retry.

`decided.left` and `qa.gate` are optional: absence means no decision governs / no gate
decision yet. Opening them as stdin before stderr is redirected leaks a diagnostic even
when the retry succeeds (see #128). These unit tests source forge-lib-task.sh the same
way test_qa_gate_once does and check stderr as well as the return value.
"""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / 'scripts' / 'forge-lib-task.sh'
PATH = '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin'


class OptionalFileReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tdir = Path(self.tmp.name) / 'tasks' / 'a'
        self.tdir.mkdir(parents=True)
        (self.tdir / 'qa.last').write_text('Review body\n', encoding='utf-8')

    def sh(self, body):
        script = (
            'note() { :; }; '
            'carry_implementer_session() { :; }; '
            'source "$1"; ' + body
        )
        return subprocess.run(
            ['/bin/bash', '-c', script, 'x', str(LIB)],
            capture_output=True, text=True, encoding='utf-8',
            env={'PATH': PATH, 'SKILL_DIR': str(ROOT)},
        )

    def test_absent_decided_left_is_silent_and_empty(self):
        result = self.sh("decided_left '%s'; printf 'done\\n'" % self.tdir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'done\n')
        self.assertNotIn('No such file', result.stderr)
        self.assertNotIn('decided.left', result.stderr)

    def test_present_decided_left_still_reads_the_number(self):
        (self.tdir / 'decided.left').write_text('3\n', encoding='utf-8')
        result = self.sh("decided_left '%s'" % self.tdir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '3')
        self.assertEqual(result.stderr, '')

    def test_carry_findings_without_qa_gate_is_silent(self):
        result = self.sh("carry_findings '%s'" % self.tdir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.tdir / 'retry_findings.md').read_text(encoding='utf-8'), 'Review body\n')
        self.assertNotIn('No such file', result.stderr)
        self.assertNotIn('qa.gate', result.stderr)

    def test_carry_findings_with_forge_fail_gate_still_prefixes_the_note(self):
        (self.tdir / 'qa.gate').write_text('P2\tfail\n', encoding='utf-8')
        result = self.sh("carry_findings '%s'" % self.tdir)
        self.assertEqual(result.returncode, 0, result.stderr)
        carried = (self.tdir / 'retry_findings.md').read_text(encoding='utf-8')
        self.assertTrue(carried.startswith("forge: the reviewer's verdict was PASS"), carried)
        self.assertIn('Review body', carried)
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
