"""Memory curation. The asymmetry that shapes every default: memory.md is injected into
every dispatch of every future run, and the ledger is append-only and never injected."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from forge_jev import DEFAULT_THRESHOLDS, memory  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _result(durable=0.9, category='trap', category_confidence=0.95,
            same=None, same_confidence=0.9):
    answers = dict(durable=dict(type='noul', noul=durable),
                   category=dict(type='choice', choice=category,
                                 confidence=category_confidence))
    if same is not None:
        answers['same'] = dict(type='choice', choice=same, confidence=same_confidence)
    return dict(answers=answers)


class CurateTests(unittest.TestCase):
    def _curate(self, *, recorded='trap', text='a fact', existing=None, **kwargs):
        """`recorded` is what the model emitted; kwargs describe what Jev answered."""
        with patch('forge_jev.memory.ask', return_value=_result(**kwargs)):
            return memory.curate(category=recorded, text=text, existing=existing)

    def test_a_durable_fact_is_injectable(self):
        self.assertTrue(self._curate(durable=0.9)['injectable'])

    def test_a_one_off_observation_is_not_injectable(self):
        # Measured: "I renamed cand to candidates in verify.py today" scored 0.1.
        self.assertFalse(self._curate(durable=0.1)['injectable'])

    def test_a_confident_recategorisation_is_applied(self):
        # Measured live: a fact recorded as `trap` that describes how the suite is run
        # came back `verify` at confidence 1.0.
        result = self._curate(recorded='trap', category='verify', category_confidence=1.0)
        self.assertEqual(result['category'], 'verify')

    def test_an_unconfident_recategorisation_is_not_applied(self):
        result = self._curate(recorded='trap', category='verify', category_confidence=0.4)
        self.assertEqual(result['category'], 'trap')

    def test_none_never_becomes_a_category(self):
        # 'none' means the model could not place it, which is a reason to keep what the
        # model that did the work said -- not to invent a category or drop the fact.
        result = self._curate(recorded='trap', category='none', category_confidence=1.0)
        self.assertEqual(result['category'], 'trap')

    def test_a_confident_paraphrase_merges(self):
        result = self._curate(existing=['run the suite with check.sh'],
                              same='run the suite with check.sh', same_confidence=0.95)
        self.assertEqual(result['matched'], 'run the suite with check.sh')

    def test_an_unconfident_paraphrase_does_not_merge(self):
        # Merging two facts that only look alike loses one permanently; a duplicate is
        # merely untidy. The bar sits on the side of keeping both.
        result = self._curate(existing=['run the suite with check.sh'],
                              same='run the suite with check.sh', same_confidence=0.5)
        self.assertIsNone(result['matched'])

    def test_no_match_never_merges(self):
        result = self._curate(existing=['something else'], same='none', same_confidence=1.0)
        self.assertIsNone(result['matched'])

    def test_the_worst_measured_false_merge_does_not_merge(self):
        # Six repeats per case against a memory shaped the way cmd_memory_curate shapes
        # one. The worst wrong merge -- two different facts about `config/prod.yaml`,
        # that it is ciphertext and that it must not hold plaintext secrets -- topped
        # out at 0.72, and it merged on every one of the six, so only the threshold
        # stops it.
        result = self._curate(existing=['the checked-in prod.yaml is ciphertext'],
                              same='the checked-in prod.yaml is ciphertext',
                              same_confidence=0.72)
        self.assertIsNone(result['matched'])

    def test_the_weakest_measured_true_merge_still_merges(self):
        # 0.91, from the same paraphrase against a three-line memory. An earlier pass
        # measured this case at 0.99 because it fed entries as "verify | text"; real
        # entries carry no category prefix, and the number moved with the state.
        result = self._curate(existing=['`pytest -q` runs the suite in about 40s'],
                              same='`pytest -q` runs the suite in about 40s',
                              same_confidence=0.91)
        self.assertEqual(result['matched'], '`pytest -q` runs the suite in about 40s')

    def test_the_strongest_measured_false_correction_is_refused(self):
        # A fact that genuinely spans two categories: `make test` regenerating fixtures
        # is both how the project is verified and a trap about the dirty tree it leaves.
        # Five repeats, 0.60-0.72, and it proposed the change every time.
        result = self._curate(recorded='verify', category='trap', category_confidence=0.72)
        self.assertEqual(result['category'], 'verify')

    def test_the_weakest_measured_true_correction_is_applied(self):
        result = self._curate(recorded='verify', category='trap', category_confidence=0.92)
        self.assertEqual(result['category'], 'trap')

    def test_merging_sits_higher_in_the_gap_than_recategorising(self):
        # Both rubrics separate at the same place -- correct 0.91+, wrong 0.72 or less --
        # so the two numbers differ by cost, not by scale. Losing a fact is worse than
        # filing it under the wrong heading, so merging takes the top of the gap.
        merge = DEFAULT_THRESHOLDS['memory_merge']
        recat = DEFAULT_THRESHOLDS['memory_recategorize']
        self.assertGreater(merge, recat)
        for name, value in (('memory_merge', merge), ('memory_recategorize', recat)):
            with self.subTest(name):
                self.assertGreater(value, 0.72, 'would act on a measured wrong judgment')
                self.assertLess(value, 0.91, 'would refuse a measured correct judgment')

    def test_a_merged_line_is_injectable_even_when_it_reads_as_a_one_off(self):
        # Without this, dedup is defeated by the quality gate in the case it exists for:
        # rebuild skips a non-injectable row before counting it, a finding needs two
        # distinct runs to be promoted, and a restatement measures 0.44-0.83 on
        # durability where the same fact stated fresh measures 0.82-0.83.
        result = self._curate(existing=['`pytest -q` runs the suite in about 40s'],
                              same='`pytest -q` runs the suite in about 40s',
                              same_confidence=0.95, durable=0.44)
        self.assertEqual(result['matched'], '`pytest -q` runs the suite in about 40s')
        self.assertTrue(result['injectable'])

    def test_a_line_that_did_not_merge_is_still_gated(self):
        # The exemption is the merge, not the presence of existing entries.
        result = self._curate(existing=['`pytest -q` runs the suite in about 40s'],
                              same='none', same_confidence=1.0, durable=0.44)
        self.assertIsNone(result['matched'])
        self.assertFalse(result['injectable'])

    def test_a_merge_refused_by_the_threshold_does_not_grant_the_exemption(self):
        result = self._curate(existing=['`pytest -q` runs the suite in about 40s'],
                              same='`pytest -q` runs the suite in about 40s',
                              same_confidence=0.72, durable=0.44)
        self.assertIsNone(result['matched'])
        self.assertFalse(result['injectable'])

    def test_dedup_is_not_asked_when_there_is_nothing_to_match(self):
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['q'] = questions
            return _result()

        with patch('forge_jev.memory.ask', side_effect=fake_ask):
            memory.curate(category='trap', text='a fact', existing=[])
        self.assertNotIn('same', captured['q'])

    def test_curate_logs_where_it_is_told_to(self):
        # Every memory call was invisible until a real run: no jev.jsonl line, no
        # latency, no tokens. 51 logged calls sat beside an unknown number of unlogged
        # ones, which is the accounting a run log exists to prevent.
        seen = {}

        def fake_ask(state, questions, **kwargs):
            seen['run_dir'] = kwargs.get('run_dir')
            return _result(durable=0.9, category='trap', category_confidence=1.0)

        with patch('forge_jev.memory.ask', side_effect=fake_ask):
            memory.curate(category='trap', text='a fact', run_dir='/tmp/run-42')
        self.assertEqual(seen['run_dir'], '/tmp/run-42')

    def test_a_failed_request_yields_no_opinion(self):
        with patch('forge_jev.memory.ask', return_value=None):
            self.assertIsNone(memory.curate(category='trap', text='a fact'))

    def test_empty_text_asks_nothing(self):
        with patch('forge_jev.memory.ask') as mocked:
            self.assertIsNone(memory.curate(category='trap', text='   '))
        mocked.assert_not_called()


class StalenessTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.file = Path(self.dir.name) / 'times.py'
        self.file.write_text('def now(tz): return datetime.now(ZoneInfo(tz))')

    def _still(self, probability):
        with patch('forge_jev.memory.ask',
                   return_value=dict(answers=dict(still=dict(type='noul', noul=probability)))):
            return memory.still_true(text='a fact', anchor_path=self.file)

    def test_a_confidently_false_fact_is_stale(self):
        self.assertTrue(self._still(0.04)['stale'])

    def test_an_uncertain_fact_is_kept(self):
        # Measured: facts that had genuinely gone false scored 0.03-0.10 and ones still
        # true scored 0.47-0.96. gate_warn's 0.60 sits inside the true range.
        self.assertFalse(self._still(0.47)['stale'])
        self.assertFalse(self._still(0.55)['stale'])

    def test_the_drop_threshold_is_well_below_gate_warn(self):
        self.assertLess(DEFAULT_THRESHOLDS['stale_drop'], DEFAULT_THRESHOLDS['gate_warn'])
        self.assertLess(0.10, DEFAULT_THRESHOLDS['stale_drop'])
        self.assertGreater(0.47, DEFAULT_THRESHOLDS['stale_drop'])

    def test_a_missing_file_is_not_a_reason_to_drop(self):
        with patch('forge_jev.memory.ask') as mocked:
            self.assertIsNone(memory.still_true(text='a fact', anchor_path='/nope/x.py'))
        mocked.assert_not_called()

    def test_a_failed_request_keeps_the_entry(self):
        with patch('forge_jev.memory.ask', return_value=None):
            self.assertIsNone(memory.still_true(text='a fact', anchor_path=self.file))


class SliceTests(unittest.TestCase):
    LINES = [f'- fact {i}' for i in range(12)]

    def test_a_short_list_is_left_alone_without_a_request(self):
        with patch('forge_jev.memory.ask') as mocked:
            self.assertIsNone(memory.relevant_lines(task='t', lines=self.LINES[:5]))
        mocked.assert_not_called()

    def test_it_keeps_the_highest_scoring_lines_in_original_order(self):
        answers = {f'l{i}': dict(type='noul', noul=i / 12.0) for i in range(12)}
        with patch('forge_jev.memory.ask', return_value=dict(answers=answers)):
            kept = memory.relevant_lines(task='t', lines=self.LINES, keep=4)
        # Highest four are 8..11, and they come back in the order they appeared, because
        # the section headings above them are what give them meaning.
        self.assertEqual(kept, ['- fact 8', '- fact 9', '- fact 10', '- fact 11'])

    def test_an_unscored_line_is_kept_not_discarded(self):
        # Absence of a judgment is not evidence against a fact.
        answers = {f'l{i}': dict(type='noul', noul=0.01) for i in range(12)}
        del answers['l0']
        with patch('forge_jev.memory.ask', return_value=dict(answers=answers)):
            kept = memory.relevant_lines(task='t', lines=self.LINES, keep=1)
        self.assertEqual(kept, ['- fact 0'])

    def test_a_failed_request_injects_everything(self):
        with patch('forge_jev.memory.ask', return_value=None):
            self.assertIsNone(memory.relevant_lines(task='t', lines=self.LINES))

    def test_a_keep_larger_than_the_list_asks_nothing(self):
        # There is nothing to narrow, and asking would cost a request to learn that.
        with patch('forge_jev.memory.ask') as mocked:
            self.assertIsNone(memory.relevant_lines(task='t', lines=self.LINES, keep=99))
        mocked.assert_not_called()

    def test_it_never_returns_more_lines_than_keep(self):
        answers = {f'l{i}': dict(type='noul', noul=0.9) for i in range(12)}
        with patch('forge_jev.memory.ask', return_value=dict(answers=answers)):
            kept = memory.relevant_lines(task='t', lines=self.LINES, keep=3)
        self.assertEqual(len(kept), 3)


class LedgerContractTests(unittest.TestCase):
    """The injectable flag gates injection only. Nothing recorded is ever lost."""

    def test_rebuild_treats_a_pre_existing_ten_column_row_as_injectable(self):
        source = (REPO_ROOT / 'scripts' / 'forge-memory.sh').read_text()
        self.assertIn('NF >= 11 && $11 == "0" { next }', source)

    def test_the_record_path_writes_the_row_whatever_jev_says(self):
        source = (REPO_ROOT / 'scripts' / 'forge-memory.sh').read_text()
        start = source.index('inject=1')
        block = source[start:source.index('n=$((n+1))', start)]
        # The one thing that must not appear: a way to skip writing the ledger row.
        self.assertNotIn('continue', block)

    def test_deep_staleness_runs_only_on_an_explicit_prune(self):
        # rebuild runs after EVERY dispatch. A request per entry per dispatch would be
        # the most expensive thing in a run, so the semantic check is opt-in.
        source = (REPO_ROOT / 'scripts' / 'forge-memory.sh').read_text()
        start = source.index('memory-stale')
        guard = source[max(0, start - 400):start]
        self.assertIn('FORGE_MEMORY_DEEP_PRUNE', guard)
        self.assertIn('FORGE_MEMORY_DEEP_PRUNE=1 rebuild', source)


class NoOpTests(unittest.TestCase):
    def test_inject_without_a_task_makes_no_subprocess_call(self):
        repo = tempfile.mkdtemp()
        forge_dir = Path(repo) / '.forge'
        forge_dir.mkdir()
        (forge_dir / 'memory.md').write_text(
            '# forge project memory\n\n## Known traps\n'
            + '\n'.join(f'- fact {i} [x]' for i in range(12)) + '\n')
        import os
        env = dict(os.environ, FORGE_JEV='off')
        result = subprocess.run(
            ['bash', str(REPO_ROOT / 'scripts' / 'forge-memory.sh'), 'inject', repo, 'dwarf'],
            capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count('\n- fact'), 12)


if __name__ == '__main__':
    unittest.main()
