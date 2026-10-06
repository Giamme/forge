"""Offline contracts for the Jev backtest runner, scoring and threshold sweep.

Runs with no network access and no API key -- every test patches forge_jev.backtest.ask
(or forge_jev.backtest.commits) directly rather than hitting client.ask's real dispatch
path, and scrubs FORGE_JEV*/TYPESAFE_API_KEY env vars the same way tests/test_jev.py does.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / 'scripts'))
import forge_jev  # noqa: E402
from forge_jev import backtest  # noqa: E402

ENV_KEYS = ('FORGE_JEV', 'FORGE_JEV_ACT', 'FORGE_JEV_SHADOW', 'FORGE_JEV_ROUTING',
           'FORGE_JEV_TESTS', 'FORGE_JEV_GATES', 'FORGE_JEV_MEMORY', 'FORGE_JEV_FIXTURES',
           'FORGE_JEV_FIXTURES_STRICT', 'FORGE_JEV_RECORD', 'TYPESAFE_API_KEY',
           'XDG_CONFIG_HOME')

SENTINEL = 'ZZZ_LABEL_LEAK_SENTINEL_ZZZ'


def _record(sha, *, n_candidates=3, n_labeled=1, prefix='tests/mod', subject='fix thing',
           body='', source_files=None):
    candidates = sorted(f'{prefix}_{i}.py' for i in range(n_candidates))
    test_files = candidates[:n_labeled]
    return dict(
        repo='/fake/repo', sha=sha, parent=sha + '~1', subject=subject, body=body,
        source_files=source_files if source_files is not None else ['src/thing.py'],
        test_files=test_files,
        all_changed=(source_files or ['src/thing.py']) + test_files,
        candidates=candidates,
        stats=dict(n_source=1, n_test=len(test_files), n_candidates=n_candidates, n_test_created=0),
    )


def _fake_commits_factory(records):
    def fake(repo, *, limit=None):
        for i, r in enumerate(records):
            if limit is not None and i >= limit:
                return
            yield r
    return fake


class JevBacktestTestCase(unittest.TestCase):
    """Hermetic env + private config home, mirroring tests/test_jev.py's JevTestCase."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.environ['XDG_CONFIG_HOME'] = str(self.root / 'config')
        self.out_dir = self.root / 'out'
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# 1. Dry run makes zero API calls ----------------------------------------------------

class DryRunTests(JevBacktestTestCase):
    def test_execute_false_makes_no_api_call_and_returns_estimate(self):
        records = [_record('a', n_candidates=250), _record('b', n_candidates=10)]
        with patch('forge_jev.backtest.commits', _fake_commits_factory(records)), \
             patch('forge_jev.backtest.ask', side_effect=AssertionError('must not call ask')):
            summary = backtest.run('/fake/repo', execute=False, out_dir=self.out_dir)
        self.assertFalse(summary['execute'])
        self.assertEqual(summary['n_records'], 2)
        # 250 candidates -> 3 chunks of <=100, plus 10 -> 1 chunk = 4 requests estimated.
        self.assertEqual(summary['estimated_requests'], 4)
        self.assertNotIn('results', summary)
        self.assertFalse((self.out_dir / 'requests.jsonl').exists())


# 2. Chunking --------------------------------------------------------------------------

class ChunkingTests(JevBacktestTestCase):
    def test_index_and_chunk_round_trip(self):
        candidates = [f'tests/t_{i}.py' for i in range(250)]
        pairs = backtest._index_candidates(candidates)
        chunks = list(backtest._chunk(pairs))
        self.assertEqual(len(chunks), 3)
        self.assertEqual([len(c) for c in chunks], [100, 100, 50])
        # every candidate appears in exactly one chunk, and qN maps back to its path
        seen = {}
        for chunk in chunks:
            for qid, path in chunk:
                self.assertNotIn(qid, seen)
                seen[qid] = path
        self.assertEqual(len(seen), 250)
        for i, path in enumerate(candidates):
            self.assertEqual(seen[f'q{i}'], path)

    def test_run_issues_three_requests_for_250_candidates(self):
        record = _record('deadbeef', n_candidates=250)
        calls = []

        def fake_ask(state, questions, *, site, run_dir, config):
            calls.append(sorted(questions))
            answers = {qid: dict(type='noul', noul=0.9) for qid in questions}
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=site)

        with patch('forge_jev.backtest.commits', _fake_commits_factory([record])), \
             patch('forge_jev.backtest.ask', side_effect=fake_ask):
            summary = backtest.run('/fake/repo', execute=True, out_dir=self.out_dir)

        self.assertEqual(len(calls), 3)
        self.assertEqual(summary['requests_used'], 3)
        result = summary['results'][0]
        self.assertFalse(result['incomplete'])
        self.assertEqual(len(result['probabilities']), 250)
        ledger_lines = (self.out_dir / 'requests.jsonl').read_text().splitlines()
        self.assertEqual(len(ledger_lines), 3)


# 3. max_requests cap halts cleanly ------------------------------------------------------

class BudgetCapTests(JevBacktestTestCase):
    def test_cap_halts_and_marks_remaining_incomplete(self):
        records = [_record(f'r{i}', n_candidates=150) for i in range(3)]  # 2 requests each = 6 total

        def fake_ask(state, questions, *, site, run_dir, config):
            answers = {qid: dict(type='noul', noul=0.5) for qid in questions}
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=site)

        with patch('forge_jev.backtest.commits', _fake_commits_factory(records)), \
             patch('forge_jev.backtest.ask', side_effect=fake_ask):
            summary = backtest.run('/fake/repo', execute=True, out_dir=self.out_dir, max_requests=3)

        self.assertEqual(summary['requests_used'], 3)
        self.assertLessEqual(summary['requests_used'], 3)
        incomplete = [r for r in summary['results'] if r['incomplete']]
        self.assertGreaterEqual(len(incomplete), 1)
        self.assertEqual(summary['n_incomplete'], len(incomplete))


# 4. ask() -> None marks the record incomplete and excludes it from metrics -----------

class FailureHandlingTests(JevBacktestTestCase):
    def test_all_failed_run_reports_zero_scored_records(self):
        records = [_record('a'), _record('b')]
        with patch('forge_jev.backtest.commits', _fake_commits_factory(records)), \
             patch('forge_jev.backtest.ask', return_value=None):
            summary = backtest.run('/fake/repo', execute=True, out_dir=self.out_dir)

        self.assertTrue(all(r['incomplete'] for r in summary['results']))
        self.assertEqual(summary['n_incomplete'], 2)
        for row in summary['sweep']:
            self.assertEqual(row['n_records'], 0)
            self.assertIsNone(row['micro_recall'])  # not 1.0 -- no manufactured perfect precision
            self.assertIsNone(row['macro_recall'])


# 5. sweep() arithmetic: micro vs macro ------------------------------------------------

class SweepArithmeticTests(unittest.TestCase):
    def test_micro_and_macro_recall_diverge_on_unequal_record_sizes(self):
        # Small record: 2 labeled tests, both selected at threshold 0.5 -> recall 1.0.
        small = dict(sha='s', n_candidates=4, n_labeled=2, labels=['t0', 't1'], incomplete=False,
                    probabilities={'t0': 0.9, 't1': 0.9, 't2': 0.1, 't3': 0.1})
        # Big record: 20 labeled tests, only 2 selected -> recall 0.1.
        big_labels = [f'b{i}' for i in range(20)]
        big_probs = {label: (0.9 if i < 2 else 0.1) for i, label in enumerate(big_labels)}
        big = dict(sha='b', n_candidates=20, n_labeled=20, labels=big_labels, incomplete=False,
                  probabilities=big_probs)

        rows = backtest.sweep([small, big], thresholds=[0.5])
        row = rows[0]
        # micro: (2 + 2) selected-labeled / (2 + 20) labeled = 4/22
        self.assertAlmostEqual(row['micro_recall'], 4 / 22)
        # macro: mean(1.0, 0.1) = 0.55
        self.assertAlmostEqual(row['macro_recall'], 0.55)
        self.assertNotAlmostEqual(row['micro_recall'], row['macro_recall'])
        # reduction: selected = 2 + 2 = 4 of 24 candidates -> 1 - 4/24
        self.assertAlmostEqual(row['reduction'], 1 - 4 / 24)


# 6. records_with_full_recall -----------------------------------------------------------

class FullRecallTests(unittest.TestCase):
    def test_full_recall_fraction_requires_every_record_perfect(self):
        perfect = dict(sha='p', n_candidates=2, n_labeled=1, labels=['t0'], incomplete=False,
                       probabilities={'t0': 0.9, 't1': 0.1})
        partial = dict(sha='q', n_candidates=2, n_labeled=2, labels=['t0', 't1'], incomplete=False,
                       probabilities={'t0': 0.9, 't1': 0.1})

        only_perfect = backtest.sweep([perfect], thresholds=[0.5])
        self.assertEqual(only_perfect[0]['records_with_full_recall'], 1.0)

        mixed = backtest.sweep([perfect, partial], thresholds=[0.5])
        self.assertEqual(mixed[0]['records_with_full_recall'], 0.5)


# 7. Label never enters the request ------------------------------------------------------

class LabelLeakTests(JevBacktestTestCase):
    def test_test_files_sentinel_never_sent_to_jev(self):
        # test_files is a subset of candidates by contract, so a sentinel placed there would
        # legitimately appear in a question's path text too (that candidate must be asked
        # about). The property under test is narrower and more important: the *state* sent
        # with every request -- built only from subject/body/source_files -- must never carry
        # test_files/all_changed, and no per-question payload may mark "this one is the label".
        record = _record('leak-check', n_candidates=5)
        record['test_files'] = [record['candidates'][0]]
        record['subject'] = 'fix thing not ' + SENTINEL  # sentinel would leak via subject too if mishandled
        sentinel_label = SENTINEL + '_LABEL_PATH.py'
        record['test_files'] = [sentinel_label]
        record['candidates'] = sorted(record['candidates'] + [sentinel_label])
        record['all_changed'] = record['source_files'] + record['test_files']

        sent = []

        def fake_ask(state, questions, *, site, run_dir, config):
            sent.append((dict(state), dict(questions)))
            answers = {qid: dict(type='noul', noul=0.5) for qid in questions}
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=site)

        with patch('forge_jev.backtest.commits', _fake_commits_factory([record])), \
             patch('forge_jev.backtest.ask', side_effect=fake_ask):
            backtest.run('/fake/repo', execute=True, out_dir=self.out_dir)

        self.assertTrue(sent)
        for state, questions in sent:
            # state never carries test_files/all_changed at all, under any key.
            self.assertEqual(set(state), {'task', 'changed_files'})
            self.assertNotIn(sentinel_label, json.dumps(state))
            # each question is a plain Noul dict for its candidate -- no extra "is_label"-style
            # field distinguishes the ground-truth path from any other candidate.
            for q in questions.values():
                self.assertEqual(set(q) <= {'type', 'instructions', 'criteria'}, True)


# 8. report() ----------------------------------------------------------------------------

class ReportTests(JevBacktestTestCase):
    def test_report_names_a_threshold_when_recall_is_high(self):
        record = _record('a', n_candidates=2, n_labeled=1)

        def fake_ask(state, questions, *, site, run_dir, config):
            answers = {qid: dict(type='noul', noul=0.99) for qid in questions}
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=site)

        with patch('forge_jev.backtest.commits', _fake_commits_factory([record])), \
             patch('forge_jev.backtest.ask', side_effect=fake_ask):
            summary = backtest.run('/fake/repo', execute=True, out_dir=self.out_dir)

        text = backtest.report(summary)
        self.assertIsInstance(text, str)
        self.assertIn('Highest threshold holding micro recall >= 0.95', text)

    def test_report_says_plainly_when_no_threshold_reaches_target(self):
        record = _record('a', n_candidates=2, n_labeled=2)

        def fake_ask(state, questions, *, site, run_dir, config):
            # Both labeled tests always score low -> recall never reaches 0.95.
            answers = {qid: dict(type='noul', noul=0.01) for qid in questions}
            return dict(answers=answers, usage=None, model='jev-latest', latency_s=0.0, site=site)

        with patch('forge_jev.backtest.commits', _fake_commits_factory([record])), \
             patch('forge_jev.backtest.ask', side_effect=fake_ask):
            summary = backtest.run('/fake/repo', execute=True, out_dir=self.out_dir)

        text = backtest.report(summary)
        self.assertIn('No threshold in this sweep holds micro recall >= 0.95.', text)

    def test_dry_run_report_mentions_no_api_calls(self):
        with patch('forge_jev.backtest.commits', _fake_commits_factory([_record('a')])):
            summary = backtest.run('/fake/repo', execute=False, out_dir=self.out_dir)
        text = backtest.report(summary)
        self.assertIn('DRY RUN', text)
        self.assertIn('no API calls', text)


class PrecisionAndNotesTests(unittest.TestCase):
    """Precision is the drift headline; the caveats must match the capability."""

    RECORDS = [
        dict(sha='A', n_candidates=4, n_labeled=2, labels=['a1', 'a2'], incomplete=False,
             probabilities={'a1': 0.9, 'a2': 0.4, 'a3': 0.8, 'a4': 0.1}),
        dict(sha='B', n_candidates=2, n_labeled=1, labels=['b1'], incomplete=False,
             probabilities={'b1': 0.6, 'b2': 0.2}),
    ]

    def test_precision_is_hits_over_selected(self):
        row = backtest.sweep(self.RECORDS, thresholds=[0.5])[0]
        # selected: a1,a3,b1 -> 3 selected, 2 of them labeled
        self.assertAlmostEqual(row['precision'], 2 / 3)
        self.assertAlmostEqual(row['micro_recall'], 2 / 3)

    def test_precision_is_none_when_nothing_is_selected(self):
        row = backtest.sweep(self.RECORDS, thresholds=[0.99])[0]
        self.assertIsNone(row['precision'])

    def test_micro_and_macro_recall_differ_on_uneven_records(self):
        row = backtest.sweep(self.RECORDS, thresholds=[0.5])[0]
        self.assertNotAlmostEqual(row['micro_recall'], row['macro_recall'])

    def test_incomplete_records_yield_no_precision(self):
        row = backtest.sweep([dict(sha='X', n_candidates=5, n_labeled=1, labels=['x'],
                                   incomplete=True, probabilities={})], thresholds=[0.5])[0]
        self.assertIsNone(row['precision'])
        self.assertEqual(row['n_records'], 0)
        self.assertEqual(row['n_incomplete'], 1)

    def test_drift_notes_drop_the_test_lower_bound_claim(self):
        # all_changed IS the complete file set for a commit, so the test-selection
        # lower-bound caveat would simply be false for drift.
        drift, tests = backtest.notes('drift'), backtest.notes('tests')
        self.assertNotIn(backtest.LOWER_BOUND_NOTE, drift)
        self.assertIn(backtest.LOWER_BOUND_NOTE, tests)
        self.assertIn(backtest.DRIFT_CAP_NOTE, drift)
        self.assertNotIn(backtest.DRIFT_CAP_NOTE, tests)

    def test_post_hoc_caveat_travels_with_every_capability(self):
        for capability in ('tests', 'drift'):
            self.assertIn(backtest.POST_HOC_NOTE, backtest.notes(capability))

    def test_report_prints_the_capability_caveats(self):
        for capability in ('tests', 'drift'):
            text = backtest.report(dict(capability=capability, repo='/r', execute=False,
                                        n_records=1, avg_candidates=3.0, estimated_requests=1,
                                        max_questions_per_request=100,
                                        notes=backtest.notes(capability)))
            self.assertIn(backtest.POST_HOC_NOTE, text)
            self.assertIn('no API calls made', text)

    def test_report_headlines_precision_for_drift_and_recall_for_tests(self):
        rows = backtest.sweep(self.RECORDS)
        drift = backtest.report(dict(capability='drift', repo='/r', execute=True,
                                     requests_used=1, n_incomplete=0, sweep=rows,
                                     notes=backtest.notes('drift')))
        self.assertIn('Drift headline', drift)
        tests = backtest.report(dict(capability='tests', repo='/r', execute=True,
                                     requests_used=1, n_incomplete=0, sweep=rows,
                                     notes=backtest.notes('tests')))
        self.assertNotIn('Drift headline', tests)
        self.assertIn('0.95', tests)


if __name__ == '__main__':
    unittest.main()
