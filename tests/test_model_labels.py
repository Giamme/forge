"""forge_models.py: the model id each spec runs, shown wherever a person picks or confirms one."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_models  # noqa: E402

ROWS = [['sol', 'codex', 'gpt-6.2-sol', '<=ultra'],
        ['sol', 'opencode', 'github-copilot/gpt-6.2-sol', '-'],
        ['opus', 'claude', 'opus', '<=max'],
        ['opus-4.6', 'antigravity', 'claude-opus-4-6-thinking', 'none']]


class ModelLabelTests(unittest.TestCase):
    def test_a_spec_resolves_the_way_dispatch_does(self):
        self.assertEqual(forge_models.resolve('sol', ROWS)['model'], 'gpt-6.2-sol')
        self.assertEqual(forge_models.resolve('sol:high:opencode', ROWS)['model'], 'github-copilot/gpt-6.2-sol')
        self.assertEqual(forge_models.resolve('sol:high', ROWS)['effort'], 'high')
        self.assertFalse(forge_models.resolve('x-1::opencode', ROWS)['known'])
        for spec in ('', 'script', 'UNASSIGNED', 'sol,opus', 'unknown'):
            self.assertIsNone(forge_models.resolve(spec, ROWS), spec)

    def test_the_label_names_the_model_and_says_when_the_cli_picks_the_version(self):
        self.assertEqual(forge_models.describe('sol:xhigh', ROWS), 'gpt-6.2-sol on codex')
        self.assertEqual(forge_models.describe('opus', ROWS), "opus on claude (the CLI's current Opus)")
        self.assertEqual(forge_models.describe('x-1::opencode', ROWS), 'x-1 on opencode (literal id, not in the registry)')
        self.assertEqual(forge_models.describe('nope', ROWS), 'unresolved')

    def test_the_legend_lists_each_distinct_spec_of_a_plan_once_pools_included(self):
        with tempfile.TemporaryDirectory() as d:
            tasks = Path(d) / 'tasks.tsv'
            tasks.write_text('# id deps difficulty files dwarf qa title\n'
                             'a\t-\tlow\tx\tsol:high\topus\tA\n'
                             'b\t-\thigh\ty\tsol:high,opus-4.6::antigravity\topus\tB\n'
                             'c\t-\tlow\tz\tscript\topus\tC\n')
            self.assertEqual(forge_models.plan_specs(tasks), ['sol:high', 'opus', 'opus-4.6::antigravity'])
            lines = forge_models.legend(forge_models.plan_specs(tasks), ROWS)
            self.assertEqual(lines[0], '  sol:high               gpt-6.2-sol on codex')
            self.assertEqual(len(lines), 3)

    def test_the_cli_reads_the_real_registry(self):
        env = dict(os.environ)
        env.pop('FORGE_REGISTRY', None)
        out = subprocess.run([sys.executable, str(ROOT / 'scripts/forge_models.py'), 'describe', 'astra'],
                             env=env, text=True, stdout=subprocess.PIPE, check=True).stdout
        self.assertEqual(out, 'astra = gpt-6.2-astra on codex\n')


if __name__ == '__main__':
    unittest.main()
