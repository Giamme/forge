"""Routing composite: the part that decides a tier without asking the model to."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from forge_jev import routing  # noqa: E402
from forge_jev.questions import (blast_radius, design_judgment, spec_clarity,  # noqa: E402
                                 state_subtlety, test_coverage)


def dims(design=0.0, state=0.0, blast=0.0, spec=0.0, coverage=1.0, confidence=0.9):
    return dict(design_judgment=(design, confidence), state_subtlety=(state, confidence),
                blast_radius=(blast, confidence), spec_clarity=(spec, confidence),
                test_coverage=(coverage, confidence))


class WeightTests(unittest.TestCase):
    def test_weights_sum_to_one(self):
        # combine() renormalizes, so a drifting sum would silently rescale every
        # composite rather than fail loudly.
        self.assertAlmostEqual(sum(abs(w) for w in routing.WEIGHTS.values()), 1.0)

    def test_every_weighted_dimension_has_a_question(self):
        self.assertEqual(set(routing.WEIGHTS), set(routing.QUESTIONS))

    def test_design_judgment_carries_the_most_weight(self):
        heaviest = max(routing.WEIGHTS, key=lambda k: abs(routing.WEIGHTS[k]))
        self.assertEqual(heaviest, 'design_judgment')


class CompositeTests(unittest.TestCase):
    def test_mechanical_well_covered_task_is_low(self):
        self.assertEqual(routing.combine(dims())['tier'], 'low')

    def test_maximally_hard_task_is_high(self):
        result = routing.combine(dims(1.0, 1.0, 1.0, 1.0, coverage=0.0))
        self.assertEqual(result['tier'], 'high')
        self.assertIsNone(result['escalated'])

    def test_ten_line_concurrency_fix_outranks_a_large_rename(self):
        # decompose.md's own example, and the reason state_subtlety is weighted second.
        concurrency = routing.combine(dims(design=0.66, state=1.0, blast=0.66, spec=0.5,
                                           coverage=0.0))['composite']
        rename = routing.combine(dims(design=0.0, state=0.0, blast=0.33, spec=0.0,
                                      coverage=1.0))['composite']
        self.assertGreater(concurrency, rename)

    def test_missing_coverage_raises_the_composite(self):
        covered = routing.combine(dims(coverage=1.0))['composite']
        uncovered = routing.combine(dims(coverage=0.0))['composite']
        self.assertGreater(uncovered, covered)

    def test_a_dimension_may_be_missing_without_losing_the_judgment(self):
        partial = dims()
        partial['state_subtlety'] = (None, None)
        result = routing.combine(partial)
        self.assertIsNotNone(result)
        self.assertEqual(result['missing'], ['state_subtlety'])
        self.assertNotIn('state_subtlety', result['dimensions'])

    def test_no_usable_dimension_returns_none(self):
        self.assertIsNone(routing.combine({k: (None, None) for k in routing.WEIGHTS}))
        self.assertIsNone(routing.combine({}))

    def test_confidence_is_weighted_not_vetoed_by_one_dimension(self):
        mixed = dims()
        mixed['blast_radius'] = (0.5, 0.0)
        result = routing.combine(mixed)
        # Measured on a real task: blast_radius came back 0.0 while design_judgment
        # (0.35 weight) was 0.94. A minimum would report 0.0 for a mostly-settled
        # composite, so the low-weight unknown is weighted, not given a veto.
        self.assertGreater(result['confidence'], 0.5)
        # ...but the dimension worth looking at is still named.
        self.assertEqual(result['weakest'], 'blast_radius')

    def test_an_uncertain_dominant_dimension_still_lowers_confidence(self):
        mixed = dims()
        mixed['design_judgment'] = (0.5, 0.1)
        self.assertLess(routing.combine(mixed)['confidence'],
                        routing.combine(dims())['confidence'])


class EscalationTests(unittest.TestCase):
    def test_boundary_escalates_one_tier_and_says_why(self):
        result = routing.combine(dims(0.3, 0.3, 0.3, 0.3, coverage=0.7))
        self.assertEqual(result['escalated'], 'boundary')
        self.assertEqual(routing._escalate(result['base_tier']), result['tier'])

    def test_low_confidence_escalates(self):
        result = routing.combine(dims(0.0, 0.0, 0.0, 0.0, coverage=1.0, confidence=0.2))
        self.assertEqual(result['confidence'], 0.2)
        self.assertEqual(result['escalated'], 'low-confidence')
        self.assertEqual(result['tier'], 'medium')

    def test_high_never_escalates_past_high(self):
        result = routing.combine(dims(0.62, 0.62, 0.62, 0.62, coverage=0.38))
        self.assertEqual(result['tier'], 'high')

    def test_confident_mid_tier_task_is_not_escalated(self):
        result = routing.combine(dims(0.5, 0.5, 0.5, 0.5, coverage=0.5))
        self.assertIsNone(result['escalated'])
        self.assertEqual(result['tier'], 'medium')


class ActsTests(unittest.TestCase):
    CONFIG = dict(thresholds=dict(routing_act=0.85))

    def test_high_confidence_unescalated_judgment_acts(self):
        judgment = dict(tier='high', confidence=0.9, escalated=None)
        self.assertTrue(routing.acts(judgment, config=self.CONFIG))

    def test_below_threshold_does_not_act(self):
        judgment = dict(tier='high', confidence=0.84, escalated=None)
        self.assertFalse(routing.acts(judgment, config=self.CONFIG))

    def test_an_escalated_judgment_never_acts_however_confident(self):
        # The escalation exists BECAUSE the composite was not trustworthy there, so
        # acting on it would write the least reliable judgments straight into the plan.
        judgment = dict(tier='high', confidence=0.99, escalated='boundary')
        self.assertFalse(routing.acts(judgment, config=self.CONFIG))

    def test_missing_confidence_does_not_act(self):
        self.assertFalse(routing.acts(dict(tier='high', confidence=None, escalated=None),
                                      config=self.CONFIG))
        self.assertFalse(routing.acts(None, config=self.CONFIG))


class NormalizationTests(unittest.TestCase):
    def test_levels_map_onto_zero_to_one(self):
        result = dict(answers=dict(q=dict(type='score', score=3, confidence=0.8)))
        self.assertEqual(routing._normalized(result, 'q', 4), (1.0, 0.8))
        result = dict(answers=dict(q=dict(type='score', score=0, confidence=0.8)))
        self.assertEqual(routing._normalized(result, 'q', 4), (0.0, 0.8))

    def test_rubrics_with_different_level_counts_are_comparable(self):
        # A 3-level rubric at its top must contribute the same 1.0 as a 4-level one.
        three = dict(answers=dict(q=dict(type='score', score=2, confidence=0.8)))
        four = dict(answers=dict(q=dict(type='score', score=3, confidence=0.8)))
        self.assertEqual(routing._normalized(three, 'q', 3)[0],
                         routing._normalized(four, 'q', 4)[0])

    def test_absent_answer_is_none_not_zero(self):
        # Scoring 0.0 for an unanswered dimension would quietly rate a task easier.
        self.assertEqual(routing._normalized(dict(answers={}), 'q', 4), (None, None))


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)

    def test_excerpt_marks_a_file_the_task_will_create(self):
        excerpt = routing._excerpt(self.repo, 'does/not/exist.py')
        self.assertEqual(excerpt['does/not/exist.py'], '(does not exist yet)')

    def test_excerpt_is_bounded(self):
        (self.repo / 'big.py').write_text('x' * 10000)
        excerpt = routing._excerpt(self.repo, 'big.py')
        self.assertEqual(len(excerpt['big.py']), routing.FILE_EXCERPT_CHARS)

    def test_excerpt_caps_the_number_of_files(self):
        names = []
        for i in range(routing.MAX_EXCERPT_FILES + 4):
            (self.repo / f'f{i}.py').write_text('x')
            names.append(f'f{i}.py')
        self.assertEqual(len(routing._excerpt(self.repo, ' '.join(names))),
                         routing.MAX_EXCERPT_FILES)

    def test_memory_is_empty_when_absent(self):
        self.assertEqual(routing._memory(self.repo), '')

    def test_score_task_never_raises_when_ask_fails(self):
        with patch('forge_jev.routing.ask', return_value=None):
            self.assertIsNone(routing.score_task(self.repo, goal='g', task_id='t',
                                                 title='t', files='a.py'))

    def test_declared_difficulty_is_not_sent_to_the_model(self):
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['state'] = state
            return None

        with patch('forge_jev.routing.ask', side_effect=fake_ask):
            routing.score_task(self.repo, goal='g', task_id='t', title='t', files='a.py',
                               declared_difficulty='high')
        # An independent second opinion, not an agreement rate.
        self.assertNotIn('high', json.dumps(captured['state']))


    def test_the_task_prompt_reaches_the_model(self):
        # It did not, until a real plan was scored. Every rubric still answered about
        # the one-line title, plausibly enough that no unit test noticed -- and
        # prompt_adequacy, the gate whose whole job is to catch an under-specified
        # task, was structurally guaranteed to be shown one. A 460-word spec with a
        # declared output shape and named edge cases scored 0.23 and tripped it.
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['state'] = state
            return None

        needle = 'THE-REQUIREMENTS-THE-REVIEWER-READS'
        with patch('forge_jev.routing.ask', side_effect=fake_ask):
            routing.score_task(self.repo, goal='g', task_id='t', title='t', files='a.py',
                               prompt='Do the thing. ' + needle)
        self.assertIn(needle, json.dumps(captured['state']))


    def test_boundary_rounding_never_jumps_a_tier(self):
        # Measured on a real plan: a composite of 0.373 sat 0.043 above LOW_MAX, scored
        # medium, and was escalated to high -- promoted a whole tier past a line it was
        # merely near. Rounding up to a boundary is a tie-break; jumping the next one
        # is not, and it sends the expensive model at a task nothing said was hard.
        order = {'low': 0, 'medium': 1, 'high': 2}
        step = 0.001
        value = 0.0
        while value <= 1.0:
            base = routing.tier_for(value)
            final = routing._escalate(base) if routing._near_boundary(value) else base
            with self.subTest(composite=round(value, 3)):
                self.assertLessEqual(order[final] - order[base], 1)
            value += step

    def test_a_composite_at_or_above_a_cut_is_not_rounded_up_to_it(self):
        # At exactly the cut, tier_for already returns the higher tier.
        for cut in (routing.LOW_MAX, routing.MEDIUM_MAX):
            with self.subTest(cut=cut):
                self.assertFalse(routing._near_boundary(cut))
                self.assertFalse(routing._near_boundary(cut + 0.001))
                self.assertTrue(routing._near_boundary(cut - 0.001))


class CalibrationGateTests(unittest.TestCase):
    """--jev-act alone must never be enough. This is the gate that makes that true."""

    CONFIG = dict(thresholds=dict(routing_act=0.85))
    CONFIDENT = dict(tier='low', confidence=0.95, escalated=None)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)

    def _write(self, payload):
        (self.repo / '.forge').mkdir(parents=True, exist_ok=True)
        (self.repo / '.forge' / routing.CALIBRATION_FILE).write_text(json.dumps(payload))

    def test_uncalibrated_repo_never_acts(self):
        self.assertIsNone(routing.calibration(self.repo))
        self.assertFalse(routing.may_act(self.repo, self.CONFIDENT, config=self.CONFIG))
        # ...even though the judgment itself clears the threshold.
        self.assertTrue(routing.acts(self.CONFIDENT, config=self.CONFIG))

    def test_calibrated_tier_acts(self):
        self._write(dict(calibrated=True, tiers=dict(low=dict(n=40, threshold=0.7))))
        self.assertTrue(routing.may_act(self.repo, self.CONFIDENT, config=self.CONFIG))

    def test_calibration_is_per_tier(self):
        # A repo with 40 low samples and 3 high ones is calibrated for one, guessing
        # about the other.
        self._write(dict(calibrated=True, tiers=dict(low=dict(n=40, threshold=0.7))))
        high = dict(tier='high', confidence=0.95, escalated=None)
        self.assertFalse(routing.may_act(self.repo, high, config=self.CONFIG))

    def test_corrupt_calibration_is_not_calibration(self):
        for payload in ('not json at all', '[1,2,3]', 'null', '{"tiers": {"low": {}}}'):
            (self.repo / '.forge').mkdir(parents=True, exist_ok=True)
            (self.repo / '.forge' / routing.CALIBRATION_FILE).write_text(payload)
            self.assertFalse(routing.may_act(self.repo, self.CONFIDENT, config=self.CONFIG),
                             payload)

    def test_calibrated_false_is_honoured(self):
        self._write(dict(calibrated=False, tiers=dict(low=dict(n=40, threshold=0.7))))
        self.assertFalse(routing.may_act(self.repo, self.CONFIDENT, config=self.CONFIG))

    def test_write_refuses_when_no_tier_met_the_bar(self):
        from forge_jev import calibrate
        summary = dict(min_sample=30, tiers=dict(
            low=dict(n=4, pass_rate=1.0, shortfall=26, meets_floor=False)))
        written, reason = calibrate.write_calibration(self.repo, summary)
        self.assertFalse(written)
        self.assertIn('no tier', reason)
        self.assertFalse((self.repo / '.forge' / routing.CALIBRATION_FILE).exists())

    def test_write_stores_only_the_tiers_that_earned_it(self):
        from forge_jev import calibrate
        # meets_floor, not a swept threshold, is what earns a tier the right to act.
        summary = dict(min_sample=30, n_samples=43, tiers=dict(
            low=dict(n=40, n_confident=38, pass_rate=0.9, shortfall=None,
                     meets_floor=True, confident_lower_bound=0.87, act_at=0.85),
            high=dict(n=3, pass_rate=0.5, shortfall=27, meets_floor=False)))
        written, _reason = calibrate.write_calibration(self.repo, summary)
        self.assertTrue(written)
        stored = routing.calibration(self.repo)
        self.assertEqual(sorted(stored['tiers']), ['low'])
        self.assertTrue(routing.may_act(self.repo, self.CONFIDENT, config=self.CONFIG))
        self.assertFalse(routing.may_act(self.repo, dict(tier='high', confidence=0.95,
                                                         escalated=None), config=self.CONFIG))


if __name__ == '__main__':
    unittest.main()
