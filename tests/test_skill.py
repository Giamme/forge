import json
from pathlib import Path
import re
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
