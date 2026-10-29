"""Offline contracts for forge_decide.metrics (GitHub #37): hand-computed values only."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from forge_decide import metrics  # noqa: E402


def rows_for(runs, per_run=4, replay=lambda label: 0.9 if label else 0.1,
             baseline=lambda label: 2 if label else 0):
    """`runs` runs of `per_run` decisions, half true, with the given score rules."""
    out = []
    for run in range(runs):
        for i in range(per_run):
            label = i % 2 == 0
            out.append(dict(run=f'r{run:02d}', ts=run, label=label, replay=replay(label),
                            baseline=baseline(label)))
    return out


class AucTestCase(unittest.TestCase):
    def test_hand_computed(self):
        self.assertEqual(metrics.auc([(0.9, 1), (0.8, 0), (0.7, 1), (0.6, 0)]), 0.75)
        self.assertEqual(metrics.auc([(1, True), (0, False)]), 1.0)
        self.assertEqual(metrics.auc([(0, True), (1, False)]), 0.0)

    def test_ties_share_their_average_rank(self):
        self.assertEqual(metrics.auc([(1, 1), (1, 0)]), 0.5)
        # positives 2 and 1, negatives 1 and 0: 2>1, 2>0, 1=1 counts half, 1>0.
        self.assertEqual(metrics.auc([(2, 1), (1, 1), (1, 0), (0, 0)]), 0.875)

    def test_one_class_is_none(self):
        self.assertIsNone(metrics.auc([(0.5, 1), (0.7, 1)]))
        self.assertIsNone(metrics.auc([(0.5, 0)]))
        self.assertIsNone(metrics.auc([]))


class SplitTestCase(unittest.TestCase):
    def test_older_half_calibrates_by_earliest_ts(self):
        rows = [dict(run='c', ts=5), dict(run='a', ts=9), dict(run='a', ts=1),
                dict(run='b', ts=3), dict(run='d', ts=7)]
        calibrate, judged = metrics.split(rows)
        # first ts: a=1, b=3, c=5, d=7 -> the older 4//2 = 2 runs calibrate.
        self.assertEqual({row['run'] for row in calibrate}, {'a', 'b'})
        self.assertEqual({row['run'] for row in judged}, {'c', 'd'})
        self.assertEqual(len(calibrate) + len(judged), len(rows))

    def test_odd_run_count_judges_the_extra_run(self):
        rows = [dict(run=name, ts=i) for i, name in enumerate('abc')]
        calibrate, judged = metrics.split(rows)
        self.assertEqual([row['run'] for row in calibrate], ['a'])
        self.assertEqual([row['run'] for row in judged], ['b', 'c'])

    def test_empty(self):
        self.assertEqual(metrics.split([]), ([], []))


class BootstrapTestCase(unittest.TestCase):
    def test_same_seed_same_interval(self):
        rows = rows_for(10)
        stat = lambda sample: sum(1 for row in sample if row['label']) / len(sample)  # noqa: E731
        self.assertEqual(metrics.paired_bootstrap(rows, stat), metrics.paired_bootstrap(rows, stat))
        self.assertEqual(metrics.paired_bootstrap(rows, stat, n=200, seed=3),
                         metrics.paired_bootstrap(rows, stat, n=200, seed=3))

    def test_resamples_whole_runs_not_rows(self):
        # One run of 1 row and one of 10: two draws can only total 2, 11 or 20 rows.
        rows = [dict(run='a', ts=0)] + [dict(run='b', ts=1) for _ in range(10)]
        seen = set()

        def stat(sample):
            seen.add(len(sample))
            return len(sample)

        low, high = metrics.paired_bootstrap(rows, stat, n=400)
        self.assertEqual(seen, {2, 11, 20})
        self.assertIn(low, seen)
        self.assertIn(high, seen)

    def test_interval_index_has_no_minus_one(self):
        # stat is the resample number: values 0..n-1 sorted, so the picks are index
        # int(n*0.025) and int(n*0.975), not int(n*0.975)-1.
        calls = iter(range(100))
        low, high = metrics.paired_bootstrap([dict(run='a')], lambda sample: next(calls), n=100)
        self.assertEqual((low, high), (2, 97))

    def test_skipped_resamples_and_the_half_rule(self):
        rows = rows_for(4)
        self.assertIsNone(metrics.paired_bootstrap(rows, lambda sample: None, n=100))
        calls = iter(range(10 ** 6))
        # Valid exactly half the time passes (n/2), one fewer does not.
        half = lambda sample: (lambda k: k if k % 2 == 0 else None)(next(calls))  # noqa: E731
        self.assertIsNotNone(metrics.paired_bootstrap(rows, half, n=100))
        calls = iter(range(10 ** 6))
        fewer = lambda sample: (lambda k: k if k % 100 < 49 else None)(next(calls))  # noqa: E731
        self.assertIsNone(metrics.paired_bootstrap(rows, fewer, n=100))

    def test_no_rows(self):
        self.assertIsNone(metrics.paired_bootstrap([], lambda sample: 1.0))


class PrecisionTestCase(unittest.TestCase):
    def test_precision_recall_flagged(self):
        pairs = [(0.9, 1), (0.8, 0), (0.6, 1), (0.2, 1), (0.1, 0)]
        self.assertEqual(metrics.precision_at(pairs, 0.6),
                         dict(precision=2 / 3, recall=2 / 3, flagged=3))

    def test_nothing_flagged_and_no_positives(self):
        self.assertEqual(metrics.precision_at([(0.2, 1), (0.1, 0)], 0.5),
                         dict(precision=None, recall=0.0, flagged=0))
        self.assertEqual(metrics.precision_at([(0.9, 0)], 0.5),
                         dict(precision=0.0, recall=None, flagged=1))


class PromotesTestCase(unittest.TestCase):
    def test_passes_when_all_four_hold(self):
        rows = rows_for(8)
        self.assertEqual(metrics.promotes(rows, floor=0.7, threshold=0.5), (True, 'passes'))
        self.assertEqual(metrics.promotes(rows), (True, 'passes'))

    def test_too_few_decisions(self):
        passes, reason = metrics.promotes(rows_for(10, per_run=4)[:29])
        self.assertFalse(passes)
        self.assertIn('29 labelled decisions', reason)

    def test_unlabelled_rows_do_not_count(self):
        rows = rows_for(10)
        for row in rows[:20]:
            row['label'] = None
        passes, reason = metrics.promotes(rows)
        self.assertFalse(passes)
        self.assertIn('20 labelled decisions', reason)

    def test_too_few_runs(self):
        passes, reason = metrics.promotes(rows_for(7, per_run=6))
        self.assertFalse(passes)
        self.assertIn('7 judged runs', reason)

    def test_worse_than_baseline(self):
        rows = rows_for(8, replay=lambda label: 0.1 if label else 0.9)
        passes, reason = metrics.promotes(rows)
        self.assertFalse(passes)
        self.assertIn('worse than the cheap baseline', reason)

    def test_margin_is_a_strict_bound(self):
        # Replay ties the baseline everywhere: the difference is exactly 0 every resample.
        rows = rows_for(8, replay=lambda label: 1.0)
        rows = [dict(row, baseline=row['replay']) for row in rows]
        self.assertTrue(metrics.promotes(rows, margin=0.02)[0])
        self.assertFalse(metrics.promotes(rows, margin=0.0)[0])

    def test_precision_below_floor(self):
        rows = rows_for(8, replay=lambda label: 0.9 if label else 0.8)
        passes, reason = metrics.promotes(rows, floor=0.7, threshold=0.5)
        self.assertFalse(passes)
        self.assertIn('below the floor 0.7', reason)
        # The same scores pass once the threshold separates the classes.
        self.assertEqual(metrics.promotes(rows, floor=0.7, threshold=0.85), (True, 'passes'))

    def test_floor_with_nothing_flagged(self):
        passes, reason = metrics.promotes(rows_for(8), floor=0.5, threshold=0.99)
        self.assertFalse(passes)
        self.assertIn('flags nothing', reason)

    def test_first_failed_condition_is_named(self):
        rows = rows_for(7, per_run=2, replay=lambda label: 0.1 if label else 0.9)
        self.assertIn('labelled decisions', metrics.promotes(rows)[1])

    def test_replay_may_be_missing_for_a_decision(self):
        rows = rows_for(8)
        for row in rows[::5]:
            row['replay'] = None
        self.assertTrue(metrics.promotes(rows)[0])


if __name__ == '__main__':
    unittest.main()
