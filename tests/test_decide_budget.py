"""Offline contracts for forge_decide's spend caps (GitHub #36).

The state home is the private XDG_STATE_HOME from dataset_isolation, so each test clears
the spend dir it shares; `now` is always explicit so day boundaries are deterministic.
"""
import dataset_isolation  # noqa: F401  keeps forge's state out of the developer's home
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decide  # noqa: E402
from forge_decide import budget  # noqa: E402


def at(day, hour=12, minute=0):
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


NOON = at(27)
PRICED = {'price_usd_per_mtok': 2}


class BudgetTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        shutil.rmtree(forge_decide.state_dir() / 'spend', ignore_errors=True)
        self.addCleanup(shutil.rmtree, forge_decide.state_dir() / 'spend', True)

    def run_dir(self, name='a'):
        path = self.root / name
        path.mkdir(exist_ok=True)
        return path

    def charge(self, tokens, run, provider=None, now=NOON):
        budget.charge(provider or {}, {'total_tokens': tokens}, run_dir=run, now=now)

    def check(self, run, now=NOON, **caps):
        return budget.check(caps, run_dir=run, now=now)


class UsageAndCostTests(BudgetTestCase):
    def test_usage_tokens_table(self):
        for usage, want in (
            ({'input_tokens': 10, 'output_tokens': 5}, 15),
            ({'prompt_tokens': 7, 'completion_tokens': 3}, 10),
            ({'total_tokens': 42}, 42),
            (None, 0),
            ({'total_tokens': 'lots'}, 0),
            ({'input_tokens': 'x', 'output_tokens': 5}, 5),
            ('nope', 0),
        ):
            with self.subTest(usage=usage):
                self.assertEqual(budget.usage_tokens(usage), want)

    def test_cost(self):
        usage = {'total_tokens': 500_000}
        self.assertAlmostEqual(budget.cost(PRICED, usage), 1.0)
        self.assertEqual(budget.cost({**PRICED, 'local': True}, usage), 0.0)
        self.assertEqual(budget.cost({}, usage), 0.0)


class CapTests(BudgetTestCase):
    def test_run_cap(self):
        run = self.run_dir()
        self.charge(600, run)
        self.charge(300, run)
        self.assertIsNone(self.check(run, run_tokens=1000))
        self.charge(200, run)
        self.assertEqual(self.check(run, run_tokens=1000), 'budget')
        self.assertIsNone(self.check(self.run_dir('b'), run_tokens=1000))

    def test_run_dir_from_env(self):
        run = self.run_dir()
        os.environ[budget.RUN_DIR_ENV] = str(run)
        self.addCleanup(os.environ.pop, budget.RUN_DIR_ENV, None)
        budget.charge({}, {'total_tokens': 1000}, now=NOON)
        self.assertEqual(budget.check({'run_tokens': 1000}, now=NOON), 'budget')
        self.assertEqual(json.loads((run / budget.RUN_FILE).read_text())['tokens'], 1000)

    def test_daily_cap_spans_runs(self):
        a, b, c = self.run_dir('a'), self.run_dir('b'), self.run_dir('c')
        self.charge(700, a)
        self.assertIsNone(self.check(a, daily_tokens=1000))
        self.charge(400, b)
        for run in (a, b, c):
            self.assertEqual(self.check(run, daily_tokens=1000), 'budget')

    def test_day_rollover(self):
        run = self.run_dir()
        late = at(27, 23, 59)
        self.charge(1000, run, now=late)
        self.assertEqual(self.check(run, now=late, daily_tokens=1000), 'budget')
        yesterday = forge_decide.state_dir() / 'spend' / '2026-10-27.json'
        before = yesterday.read_text()
        self.assertIsNone(self.check(self.run_dir('b'), now=at(28, 0, 1), daily_tokens=1000))
        self.assertEqual(yesterday.read_text(), before)

    def test_usd_caps(self):
        run = self.run_dir()
        budget.charge(PRICED, {'total_tokens': 400_000}, run_dir=run, now=NOON)
        self.assertIsNone(self.check(run, run_usd=1.0, daily_usd=1.0))
        budget.charge(PRICED, {'total_tokens': 100_000}, run_dir=run, now=NOON)
        self.assertEqual(self.check(run, run_usd=1.0), 'budget')
        self.assertEqual(self.check(self.run_dir('b'), daily_usd=1.0), 'budget')
        self.assertIsNone(self.check(self.run_dir('b'), run_usd=1.0))

    def test_local_provider_spends_no_usd(self):
        run = self.run_dir()
        budget.charge({**PRICED, 'local': True}, {'total_tokens': 5_000_000}, run_dir=run, now=NOON)
        self.assertIsNone(self.check(run, run_usd=0.01, daily_usd=0.01))
        self.assertEqual(self.check(run, run_tokens=1000), 'budget')

    def test_none_cap_never_blocks(self):
        run = self.run_dir()
        self.charge(10**9, run)
        self.assertIsNone(self.check(run))
        self.assertIsNone(self.check(run, run_tokens=None, run_usd=None,
                                     daily_tokens=None, daily_usd=None))


class FailOpenTests(BudgetTestCase):
    def test_corrupt_counter_counts_as_zero_and_is_rewritten(self):
        run = self.run_dir()
        counter = run / budget.RUN_FILE
        for junk in ('{not json', '[]', '{"tokens": "x"}', ''):
            with self.subTest(junk=junk):
                counter.write_text(junk)
                self.assertIsNone(self.check(run, run_tokens=1))
                self.charge(5, run)
                self.assertEqual(json.loads(counter.read_text())['tokens'], 5)
                shutil.rmtree(forge_decide.state_dir() / 'spend', ignore_errors=True)

    def test_unwritable_run_dir_skips_without_raising(self):
        missing_parent = self.root / 'file'
        missing_parent.write_text('not a dir')
        self.charge(100, missing_parent / 'run')
        self.assertIsNone(self.check(missing_parent / 'run', run_tokens=1))

    def test_check_never_raises(self):
        self.assertIsNone(budget.check(None, run_dir=self.root / 'none', now='bad'))
        self.assertIsNone(budget.check({'run_tokens': 'x'}, run_dir=self.run_dir(), now=NOON))


class ConcurrencyTests(BudgetTestCase):
    def test_threads_lose_no_charge(self):
        run = self.run_dir()

        def work():
            for _ in range(25):
                self.charge(100, run)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = budget.spend(run_dir=run, now=NOON)
        self.assertEqual(got['run']['tokens'], 20_000)
        self.assertEqual(got['today']['tokens'], 20_000)


class SpendTests(BudgetTestCase):
    def test_spend(self):
        run = self.run_dir()
        budget.charge(PRICED, {'total_tokens': 500_000}, run_dir=run, now=NOON)
        got = budget.spend(run_dir=run, now=NOON)
        self.assertEqual(got['date'], '2026-10-27')
        self.assertEqual(got['run']['tokens'], 500_000)
        self.assertAlmostEqual(got['today']['usd'], 1.0)
        self.assertIsNone(budget.spend(now=NOON)['run'])
        self.assertEqual(budget.spend(now=at(28))['today'], {'tokens': 0, 'usd': 0.0})


if __name__ == '__main__':
    unittest.main()
