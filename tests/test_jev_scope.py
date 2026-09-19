"""`forge jev scope`: read run logs back, report distributions and the repeat noise floor.

Every threshold in forge_jev was at some point fitted to numbers the model was not
producing. This command is how that gets noticed afterwards, so it must work with no key,
no network and a mix of good, failed and malformed log lines.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))

from forge_jev import scope  # noqa: E402


def _row(site, key, answers, ok=True, reason=None):
    return json.dumps(dict(at=0, site=site, key=key, ok=ok, reason=reason, latency_s=0.1,
                           model='jev-latest', usage=None, questions=list(answers or {}),
                           answers=answers, acting=False, shadow=False))


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.plan = Path(self.dir.name) / 'plan'
        (self.plan / 'tasks' / 'a').mkdir(parents=True)
        # Plan-level log: two byte-identical routing requests (same key) that came back
        # slightly different, plus one distinct request.
        (self.plan / 'jev.jsonl').write_text('\n'.join([
            _row('jev-routing', 'k1', {'design_judgment': {'type': 'score', 'score': 1.0, 'confidence': 0.80},
                                       'verifiable': {'type': 'noul', 'noul': 0.40}}),
            _row('jev-routing', 'k1', {'design_judgment': {'type': 'score', 'score': 1.2, 'confidence': 0.87},
                                       'verifiable': {'type': 'noul', 'noul': 0.45}}),
            _row('jev-routing', 'k2', {'design_judgment': {'type': 'score', 'score': 0.0, 'confidence': 0.95},
                                       'verifiable': {'type': 'noul', 'noul': 0.90}}),
            'not json at all',
            _row('jev-drift', 'k3', None, ok=False, reason='http-429: slow down'),
            _row('jev-drift', 'k4', None, ok=False, reason='no-key'),
        ]) + '\n')
        # Task-level log, found by walking the plan directory.
        (self.plan / 'tasks' / 'a' / 'jev.jsonl').write_text(
            _row('jev-review', 'k5', {'correctness': {'type': 'noul', 'noul': 0.21}}) + '\n')

    def test_finds_every_log_under_a_plan_and_accepts_a_file(self):
        self.assertEqual(len(scope.logs_under([self.plan])), 2)
        self.assertEqual(len(scope.logs_under([self.plan / 'jev.jsonl'])), 1)
        self.assertEqual(scope.logs_under([Path(self.dir.name) / 'missing']), [])

    def test_distribution_per_site_and_question(self):
        result = scope.report([self.plan])
        self.assertEqual(result['rows'], 6)   # the unparseable line is not a row
        by = {(r['site'], r['question']): r for r in result['rubrics']}
        judgment = by[('jev-routing', 'design_judgment')]
        # Choice/score answers contribute their confidence, noul answers their probability.
        self.assertEqual((judgment['n'], judgment['min'], judgment['max']), (3, 0.80, 0.95))
        self.assertEqual(judgment['median'], 0.87)
        self.assertEqual(by[('jev-review', 'correctness')]['n'], 1)

    def test_noise_floor_is_the_largest_spread_across_identical_requests(self):
        result = scope.report([self.plan])
        by = {(r['site'], r['question']): r for r in result['rubrics']}
        # k1 repeated: confidence 0.80 vs 0.87, noul 0.40 vs 0.45. k2 and k5 never repeated.
        self.assertEqual(by[('jev-routing', 'design_judgment')]['repeat_spread'], 0.07)
        self.assertEqual(by[('jev-routing', 'design_judgment')]['repeated_requests'], 1)
        self.assertEqual(by[('jev-routing', 'verifiable')]['repeat_spread'], 0.05)
        self.assertIsNone(by[('jev-review', 'correctness')]['repeat_spread'])
        self.assertEqual(result['noise_floor'], 0.07)

    def test_skips_are_counted_by_site_and_reason_family(self):
        result = scope.report([self.plan])
        # 'http-429: slow down' collapses to its family so a thousand distinct messages
        # still read as one line.
        self.assertEqual(result['skips'], {'jev-drift': {'http-429': 1, 'no-key': 1}})

    def test_no_repeats_means_an_unknown_floor_not_zero(self):
        (self.plan / 'jev.jsonl').unlink()
        result = scope.report([self.plan])
        self.assertIsNone(result['noise_floor'])
        self.assertIn('unknown', scope.render(result))

    def test_render_mentions_the_floor_and_every_rubric(self):
        text = scope.render(scope.report([self.plan]))
        self.assertIn('noise floor: 0.070', text)
        self.assertIn('design_judgment', text)
        self.assertIn('jev-drift: http-429 x1, no-key x1', text)


class CliContractTests(unittest.TestCase):
    """Works with no key configured and no network; exit 2 when there is nothing to read."""

    def _run(self, *paths, json_out=False):
        env = dict(os.environ, XDG_CONFIG_HOME=str(Path(self.dir.name) / 'config'))
        env.pop('TYPESAFE_API_KEY', None)
        args = [sys.executable, str(ROOT / 'scripts' / 'forge-jev.py'), 'scope', *map(str, paths)]
        if json_out:
            args.append('--json')
        return subprocess.run(args, capture_output=True, text=True, env=env)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.run_dir = Path(self.dir.name) / 'run'
        self.run_dir.mkdir()
        (self.run_dir / 'jev.jsonl').write_text(
            _row('jev-verify', 'k', {'pick': {'type': 'choice', 'choice': 'pytest', 'confidence': 0.73}}) + '\n')

    def test_reports_without_a_key(self):
        result = self._run(self.run_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('jev-verify', result.stdout)
        self.assertIn('1 request(s)', result.stdout)

    def test_json_output_is_parseable(self):
        result = self._run(self.run_dir, json_out=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['rubrics'][0]['question'], 'pick')

    def test_nothing_to_read_is_a_usage_error(self):
        result = self._run(Path(self.dir.name) / 'nowhere')
        self.assertEqual(result.returncode, 2)
        self.assertIn('no jev.jsonl', result.stderr)


if __name__ == '__main__':
    unittest.main()
