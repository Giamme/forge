"""Offline contracts for the Jev calibrate command: joining shadow-mode routing
judgments to ledger outcomes, and -- most importantly -- refusing to emit a threshold
below --min-sample.

Pure file-format parsing with no network and no API key involved, so unlike
test_jev.py/test_jev_backtest.py these tests do not need to scrub FORGE_JEV* env vars
or point XDG_CONFIG_HOME anywhere; calibrate.py never touches config or the client.
"""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / 'scripts'))
from forge_jev import calibrate, cli  # noqa: E402


def _write_tsv(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['\t'.join(str(x) for x in row) for row in rows]
    path.write_text('\n'.join(lines) + ('\n' if lines else ''))


def _write_jsonl(path: Path, entries) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) if not isinstance(e, str) else e for e in entries]
    path.write_text('\n'.join(lines) + ('\n' if lines else ''))


def _score_line(ok=True, at=1.0) -> dict:
    return dict(at=at, site='jev-score-plan', ok=ok, answers={'q0': dict(type='noul', noul=0.5)},
               shadow=True, acting=False)


def _routing_row(run_id, task, tier, confidence, composite=0.5):
    return (run_id, task, tier, confidence, composite)


def _ledger_qa_row(run_id, task, verdict, ts='1'):
    return (ts, run_id, task, 'qa', 'model', verdict, 'cat', 'key', 'text', '-')


def _ledger_dwarf_row(run_id, task, duration_s, ts='0'):
    return (ts, run_id, task, 'dwarf', 'model', '-', 'cat', 'key', 'text', duration_s)


class CalibrateTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / 'repo'
        self.repo.mkdir()
        self.run_dir = self.repo / '.forge' / 'runs' / 'r1'
        self.run_dir.mkdir(parents=True)

    def _seed_run(self, routing_rows, run_dir=None, score_ok=True):
        run_dir = run_dir or self.run_dir
        _write_jsonl(run_dir / 'jev.jsonl', [_score_line(ok=score_ok)])
        _write_tsv(run_dir / 'jev-routing.tsv', routing_rows)

    def _seed_ledger(self, rows):
        _write_tsv(self.repo / '.forge' / 'ledger.tsv', rows)


# 1. The refusal below min_sample is the headline behaviour ---------------------------

class RefusalTests(CalibrateTestCase):
    def test_tier_below_min_sample_emits_no_threshold(self):
        rows = [_routing_row('r1', f't{i}', 'low', 0.6) for i in range(4)]
        self._seed_run(rows)
        self._seed_ledger([_ledger_qa_row('r1', f't{i}', 'PASS') for i in range(4)])

        summary = calibrate.run(self.repo, min_sample=30)
        tier = summary['tiers']['low']
        self.assertEqual(tier['n'], 4)
        self.assertIsNone(tier['threshold'])
        self.assertEqual(tier['shortfall'], 26)
        self.assertFalse(summary['any_meets_bar'])

        text = calibrate.report(summary)
        self.assertIn('low: 4 of 30 samples -- no threshold emitted (need 26 more)', text)

    def test_exit_code_3_when_nothing_can_be_said(self):
        rows = [_routing_row('r1', f't{i}', 'low', 0.6) for i in range(4)]
        self._seed_run(rows)
        self._seed_ledger([_ledger_qa_row('r1', f't{i}', 'PASS') for i in range(4)])

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(['calibrate', '--repo', str(self.repo)])
        self.assertEqual(rc, 3)


# 2. A tier that meets the bar gets stats and a real (non-invented) threshold ---------

class MeetsBarTests(CalibrateTestCase):
    def test_tier_meeting_bar_emits_stats_and_swept_threshold(self):
        rows = []
        ledger = []
        # 2 samples below the sweep floor (0.30 confidence), passing -- excluded from
        # every subset in the sweep, but they still drag the tier's overall pass rate up.
        for i in range(2):
            task = f'below{i}'
            rows.append(_routing_row('r1', task, 'high', 0.30))
            ledger.append(_ledger_qa_row('r1', task, 'PASS'))
        # 4 samples at exactly the sweep floor (0.50), failing.
        for i in range(4):
            task = f'floor{i}'
            rows.append(_routing_row('r1', task, 'high', 0.50))
            ledger.append(_ledger_qa_row('r1', task, 'FAIL'))
        # 6 samples at 0.90, passing.
        for i in range(6):
            task = f'high{i}'
            rows.append(_routing_row('r1', task, 'high', 0.90))
            ledger.append(_ledger_qa_row('r1', task, 'PASS'))
            ledger.append(_ledger_dwarf_row('r1', task, str(10.0 + i)))

        self._seed_run(rows)
        self._seed_ledger(ledger)

        summary = calibrate.run(self.repo, min_sample=10)
        tier = summary['tiers']['high']
        self.assertEqual(tier['n'], 12)
        self.assertAlmostEqual(tier['pass_rate'], 8 / 12)
        # The six passing 0.90 samples score 1.0 observed, but a Wilson 95% lower bound
        # on 6/6 is only 0.61 -- under the tier's own 0.667 -- so no threshold is
        # suggested. Before that guard this returned 0.55 on the strength of six points,
        # which is precisely the overfit the sweep is prone to.
        self.assertIsNone(tier['threshold'])
        self.assertIsNone(tier['shortfall'])
        self.assertTrue(summary['any_meets_bar'])

        text = calibrate.report(summary)
        self.assertIn('high: n=12', text)
        self.assertIn('below the floor', text)
        self.assertIn('95% lower bound=0.610', text)


# 3. Malformed jev.jsonl lines are skipped, never raise -------------------------------

class MalformedJsonlTests(CalibrateTestCase):
    def test_corrupt_jsonl_lines_are_skipped_and_counted(self):
        _write_jsonl(self.run_dir / 'jev.jsonl',
                    ['not json at all', json.dumps(['also', 'not', 'a', 'dict']), _score_line()])
        _write_tsv(self.run_dir / 'jev-routing.tsv', [_routing_row('r1', 't0', 'low', 0.6)])
        self._seed_ledger([_ledger_qa_row('r1', 't0', 'PASS')])

        summary = calibrate.run(self.repo, min_sample=1)
        self.assertEqual(summary['n_skipped_jsonl_lines'], 2)
        self.assertEqual(summary['n_samples'], 1)  # the one good score line still gates the run dir in

    def test_run_dir_with_no_successful_score_line_is_skipped(self):
        _write_jsonl(self.run_dir / 'jev.jsonl', [_score_line(ok=False)])
        _write_tsv(self.run_dir / 'jev-routing.tsv', [_routing_row('r1', 't0', 'low', 0.6)])
        self._seed_ledger([_ledger_qa_row('r1', 't0', 'PASS')])

        summary = calibrate.run(self.repo, min_sample=1)
        self.assertEqual(summary['n_samples'], 0)
        self.assertEqual(summary['n_run_dirs_used'], 0)


# 4. Malformed / short ledger rows are skipped, never raise ---------------------------

class MalformedLedgerTests(CalibrateTestCase):
    def test_short_and_bad_role_rows_are_skipped_and_counted(self):
        rows = [_routing_row('r1', 't0', 'low', 0.6)]
        self._seed_run(rows)
        good = _ledger_qa_row('r1', 't0', 'PASS')
        short_row = ('1', 'r1', 't0', 'qa')  # only 4 of 10 fields
        bad_role = ('1', 'r1', 't0', 'sysadmin', 'model', 'PASS', 'cat', 'key', 'text', '-')
        _write_tsv(self.repo / '.forge' / 'ledger.tsv', [good])
        # Append the malformed rows as raw lines (they aren't valid 10-tuples to render
        # via _write_tsv's normal path, but the short one legitimately has few columns).
        path = self.repo / '.forge' / 'ledger.tsv'
        with path.open('a') as handle:
            handle.write('\t'.join(short_row) + '\n')
            handle.write('\t'.join(bad_role) + '\n')

        summary = calibrate.run(self.repo, min_sample=1)
        self.assertEqual(summary['n_skipped_ledger_rows'], 2)
        self.assertEqual(summary['n_samples'], 1)
        self.assertTrue(summary['tiers']['low']['pass_rate'] == 1.0)

    def test_non_numeric_duration_does_not_crash_and_is_simply_not_counted(self):
        rows = [_routing_row('r1', 't0', 'low', 0.6)]
        self._seed_run(rows)
        self._seed_ledger([_ledger_qa_row('r1', 't0', 'PASS'),
                           _ledger_dwarf_row('r1', 't0', 'not-a-number')])
        summary = calibrate.run(self.repo, min_sample=1)
        self.assertEqual(summary['n_samples'], 1)
        self.assertIsNone(summary['tiers']['low']['mean_duration_s'])


# 5. Missing files are not an error ----------------------------------------------------

class MissingFilesTests(CalibrateTestCase):
    def test_missing_ledger_tsv_yields_zero_samples_not_a_crash(self):
        rows = [_routing_row('r1', 't0', 'low', 0.6)]
        self._seed_run(rows)
        # no ledger.tsv written at all
        summary = calibrate.run(self.repo, min_sample=1)
        self.assertEqual(summary['n_samples'], 0)
        for tier in calibrate.TIERS:
            self.assertEqual(summary['tiers'][tier]['n'], 0)

    def test_missing_jev_routing_tsv_skips_the_run_dir(self):
        _write_jsonl(self.run_dir / 'jev.jsonl', [_score_line()])
        # no jev-routing.tsv written
        self._seed_ledger([_ledger_qa_row('r1', 't0', 'PASS')])
        summary = calibrate.run(self.repo, min_sample=1)
        self.assertEqual(summary['n_samples'], 0)
        self.assertEqual(summary['n_run_dirs_used'], 0)


# 6. qa verdict wins the collapse; dwarf supplies duration -----------------------------

class CollapseRuleTests(CalibrateTestCase):
    def test_qa_verdict_wins_over_absence_and_dwarf_supplies_duration(self):
        rows = [_routing_row('r1', 't0', 'medium', 0.7)]
        self._seed_run(rows)
        self._seed_ledger([
            _ledger_dwarf_row('r1', 't0', '5.0', ts='0'),
            _ledger_dwarf_row('r1', 't0', '9.5', ts='1'),  # max of the dwarf durations
            _ledger_qa_row('r1', 't0', 'PASS', ts='2'),
        ])
        summary = calibrate.run(self.repo, min_sample=1)
        tier = summary['tiers']['medium']
        self.assertEqual(tier['n'], 1)
        self.assertEqual(tier['pass_rate'], 1.0)
        self.assertAlmostEqual(tier['mean_duration_s'], 9.5)

    def test_no_qa_row_collapses_to_unknown_which_is_not_a_pass(self):
        rows = [_routing_row('r1', 't0', 'medium', 0.7)]
        self._seed_run(rows)
        self._seed_ledger([_ledger_dwarf_row('r1', 't0', '5.0')])
        summary = calibrate.run(self.repo, min_sample=1)
        tier = summary['tiers']['medium']
        self.assertEqual(tier['n'], 1)
        self.assertEqual(tier['pass_rate'], 0.0)


# 7. --json shape -----------------------------------------------------------------------

class JsonShapeTests(CalibrateTestCase):
    def test_json_output_is_valid_and_matches_run_summary(self):
        rows = [_routing_row('r1', 't0', 'low', 0.6)]
        self._seed_run(rows)
        self._seed_ledger([_ledger_qa_row('r1', 't0', 'PASS')])

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(['calibrate', '--repo', str(self.repo), '--min-sample', '1', '--json'])
        self.assertEqual(rc, 0)
        data = json.loads(out.getvalue())
        self.assertEqual(data['n_samples'], 1)
        self.assertIn('tiers', data)
        for tier in calibrate.TIERS:
            self.assertIn(tier, data['tiers'])
            self.assertIn('n', data['tiers'][tier])
            self.assertIn('threshold', data['tiers'][tier])
        self.assertTrue(data['any_meets_bar'])


# 8. --repo not a directory is a usage error --------------------------------------------

class UsageErrorTests(unittest.TestCase):
    def test_missing_repo_dir_returns_2(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(['calibrate', '--repo', '/no/such/path/at/all'])
        self.assertEqual(rc, 2)


class AbsoluteFloorTests(CalibrateTestCase):
    """Acting is gated on an absolute pass rate, not on beating the tier's own average.

    The relative version is unanswerable by construction: the confident subset is
    contained in the tier, so when confidence clusters tightly it IS most of the tier and
    cannot out-perform it by a detectable margin.
    """

    def _seed(self, n, passing, confidence=0.90, tier='low'):
        rows, ledger = [], []
        for i in range(n):
            task = f't{i}'
            rows.append(_routing_row('r1', task, tier, confidence))
            ledger.append(_ledger_qa_row('r1', task, 'PASS' if i < passing else 'FAIL'))
        self._seed_run(rows)
        self._seed_ledger(ledger)
        return calibrate.run(self.repo, min_sample=30)

    def test_a_reliable_tier_meets_the_floor(self):
        tier = self._seed(40, 40)['tiers']['low']
        self.assertEqual(tier['n_confident'], 40)
        self.assertTrue(tier['meets_floor'])
        self.assertGreaterEqual(tier['confident_lower_bound'], 0.80)

    def test_a_marginal_tier_is_refused(self):
        # 80% observed is exactly the floor, so the lower bound sits well under it.
        tier = self._seed(40, 32)['tiers']['low']
        self.assertFalse(tier['meets_floor'])
        self.assertLess(tier['confident_lower_bound'], 0.80)

    def test_a_perfect_but_tiny_sample_is_still_refused(self):
        # 100% of 5 is 1.0 observed and 0.57 bounded: the sample-size requirement falls
        # out of the arithmetic rather than being a second magic constant.
        tier = self._seed(5, 5)['tiers']['low']
        self.assertFalse(tier['meets_floor'])
        self.assertEqual(tier['shortfall'], 25)

    def test_low_confidence_samples_do_not_count_toward_the_floor(self):
        tier = self._seed(40, 40, confidence=0.50)['tiers']['low']
        self.assertEqual(tier['n_confident'], 0)
        self.assertFalse(tier['meets_floor'])

    def test_write_follows_the_floor_not_the_swept_threshold(self):
        summary = self._seed(40, 40)
        written, reason = calibrate.write_calibration(self.repo, summary)
        self.assertTrue(written, reason)
        stored = json.loads((self.repo / '.forge'
                             / 'jev-routing-calibration.json').read_text())
        self.assertEqual(sorted(stored['tiers']), ['low'])
        self.assertGreaterEqual(stored['tiers']['low']['lower_bound'], 0.80)

    def test_write_refuses_a_marginal_tier(self):
        written, reason = calibrate.write_calibration(self.repo, self._seed(40, 32))
        self.assertFalse(written)
        self.assertIn('floor', reason)


if __name__ == '__main__':
    unittest.main()
