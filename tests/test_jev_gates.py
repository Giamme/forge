"""Pre-dispatch gates: plan-time warnings that must never block and never nag."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from forge_jev import DEFAULT_THRESHOLDS, gates, routing  # noqa: E402

# The largest spread jevscope measured across byte-identical repeated requests in
# a real run. A threshold with less margin than this is measuring noise.
NOISE = 0.07


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)

    def _commit(self, *names):
        for name in names:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('x')
        subprocess.run(['git', '-C', str(self.repo), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(self.repo), '-c', 'user.email=t@t',
                        '-c', 'user.name=t', 'commit', '-qm', 'c'], check=True)

    def test_declared_files_are_never_candidates(self):
        self._commit('a.py', 'b.py')
        self.assertEqual(gates.candidates(self.repo, 'a.py'), ['b.py'])

    def test_declared_list_may_be_comma_or_space_separated(self):
        self._commit('a.py', 'b.py', 'c.py')
        self.assertEqual(gates.candidates(self.repo, 'a.py,b.py'), ['c.py'])
        self.assertEqual(gates.candidates(self.repo, 'a.py b.py'), ['c.py'])

    def test_noise_directories_and_binaries_are_skipped(self):
        self._commit('src/a.py', 'node_modules/x.js', 'package-lock.json',
                     'logo.png', 'dist/bundle.js', 'go.sum', 'Cargo.lock',
                     'web/yarn.lock')
        # package-lock.json ends in .json, not .lock -- the suffix list alone misses it.
        self.assertEqual(gates.candidates(self.repo, '-'), ['src/a.py'])

    def test_candidates_are_sorted_and_capped_deterministically(self):
        self._commit(*[f'f{i:03d}.py' for i in range(gates.CANDIDATE_CAP + 20)])
        found = gates.candidates(self.repo, '-')
        self.assertEqual(len(found), gates.CANDIDATE_CAP)
        # Sorted BEFORE truncation, so the same plan cannot warn differently twice.
        self.assertEqual(found, sorted(found))
        self.assertEqual(found, gates.candidates(self.repo, '-'))

    def test_a_non_repository_yields_nothing_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as plain:
            self.assertEqual(gates.candidates(plain, '-'), [])

    def test_empty_candidate_pool_short_circuits_without_a_request(self):
        self._commit('a.py')
        with patch('forge_jev.gates.ask') as mocked:
            self.assertEqual(gates.predict_drift(self.repo, goal='g', task_id='t',
                                                 title='t', declared='a.py'), [])
        mocked.assert_not_called()

    def test_a_failed_request_is_none_not_an_empty_list(self):
        # "Jev did not answer" and "Jev predicts no drift" must stay distinguishable.
        self._commit('a.py', 'b.py')
        with patch('forge_jev.gates.ask', return_value=None):
            self.assertIsNone(gates.predict_drift(self.repo, goal='g', task_id='t',
                                                  title='t', declared='a.py'))

    def test_only_files_at_or_above_the_threshold_are_reported_highest_first(self):
        self._commit('a.py', 'b.py', 'c.py', 'd.py')
        pool = gates.candidates(self.repo, 'a.py')
        answers = {f'f{i}': dict(type='noul', noul=value)
                   for i, value in enumerate((0.95, 0.10, 0.70)[:len(pool)])}
        with patch('forge_jev.gates.ask', return_value=dict(answers=answers)):
            found = gates.predict_drift(self.repo, goal='g', task_id='t', title='t',
                                        declared='a.py')
        self.assertEqual([name for name, _p in found], ['b.py', 'd.py'])
        self.assertEqual([p for _n, p in found], [0.95, 0.70])


class ThresholdTests(unittest.TestCase):
    """Each rubric gets its own warn level. Measured, not assumed."""

    def test_the_three_gate_thresholds_are_distinct(self):
        # Reusing gate_warn for all three would warn on roughly half of all good tasks.
        self.assertEqual(DEFAULT_THRESHOLDS['gate_warn'], 0.60)
        self.assertEqual(DEFAULT_THRESHOLDS['prompt_warn'], 0.55)
        self.assertEqual(DEFAULT_THRESHOLDS['verifiable_warn'], 0.38)

    def test_prompt_warn_sits_in_the_measured_gap(self):
        # Re-measured after prompt.md began reaching the model. The earlier numbers
        # (vague 0.04, specific 0.55-0.87) were taken with only a task TITLE in state,
        # so they described data production never produced.
        #
        # 3 runs over 8 prompts in the real state, plus 8 real task prompts:
        # unjudgeable ("make the error messages better", "harden the server -- fix what
        # you find") 0.06-0.27; stating checkable behaviour 0.85-0.96.
        warn = DEFAULT_THRESHOLDS['prompt_warn']
        self.assertGreater(warn, 0.27 + NOISE,
                           'must clear the worst true positive by more than repeat noise')
        self.assertLess(warn, 0.85 - NOISE,
                        'must stay clear of the weakest real prompt by the same')

    def test_verifiable_warn_stays_below_the_overlap(self):
        # This rubric does NOT separate its classes. Tasks that genuinely cannot be
        # checked alone scored 0.34-0.56; ones that can scored 0.43-0.90. They overlap,
        # so no threshold divides them and moving the number cannot fix that -- the
        # rubric needs reworking. 0.38 trades recall for precision: everything at or
        # below it has so far been genuinely unverifiable. This test exists to stop
        # someone raising it into the overlap in the meantime.
        self.assertLess(DEFAULT_THRESHOLDS['verifiable_warn'], 0.43,
                        'above 0.43 it fires on tasks that ARE independently verifiable')


class WarningPlumbingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)

    def _score(self, adequacy, verifiable, *, gates_on=True):
        answers = {name: dict(type='score', score=0, confidence=0.9)
                   for name in routing.WEIGHTS}
        answers['prompt_adequacy'] = dict(type='noul', noul=adequacy)
        answers['independently_verifiable'] = dict(type='noul', noul=verifiable)
        with patch('forge_jev.routing.enabled', return_value=gates_on), \
             patch('forge_jev.routing.ask', return_value=dict(answers=answers)):
            return routing.score_task(self.repo, goal='g', task_id='t', title='t',
                                      files='a.py')

    def test_a_good_task_produces_no_warnings_key_at_all(self):
        self.assertNotIn('warnings', self._score(0.80, 0.70))

    def test_a_vague_prompt_warns(self):
        self.assertEqual(self._score(0.04, 0.70)['warnings'], dict(prompt_adequacy=0.04))

    def test_a_fragment_warns(self):
        self.assertEqual(self._score(0.80, 0.16)['warnings'],
                         dict(independently_verifiable=0.16))

    def test_the_gates_are_not_asked_when_the_capability_is_off(self):
        self.assertNotIn('warnings', self._score(0.04, 0.16, gates_on=False))

    def test_warnings_never_change_the_tier(self):
        # A gate is advice about the task text; it must not move which model runs it.
        self.assertEqual(self._score(0.04, 0.16)['tier'], self._score(0.90, 0.90)['tier'])


if __name__ == '__main__':
    unittest.main()
