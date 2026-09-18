"""Plan-time semantic wave coupling: warns on same-wave pairs, never edits the plan."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from forge_jev import DEFAULT_THRESHOLDS, coupling  # noqa: E402


def task(task_id, title='t', files='-', difficulty='low'):
    return dict(id=task_id, title=title, files=files, difficulty=difficulty)


class PairsTests(unittest.TestCase):
    def test_pairs_only_form_within_a_wave(self):
        tasks = {i: task(i) for i in ('a', 'b', 'c', 'd')}
        waves = {'1': ['a', 'b'], '2': ['c', 'd']}
        found = coupling.pairs(tasks, waves)
        self.assertEqual(sorted(found), [('a', 'b'), ('c', 'd')])
        # Never across waves: 'b' and 'c' are in different waves and must not appear.
        self.assertNotIn(('b', 'c'), found)

    def test_a_single_task_wave_produces_no_pairs(self):
        tasks = {'a': task('a')}
        waves = {'1': ['a']}
        self.assertEqual(coupling.pairs(tasks, waves), [])

    def test_unknown_ids_in_a_wave_are_ignored_rather_than_raising(self):
        tasks = {'a': task('a'), 'b': task('b')}
        waves = {'1': ['a', 'b', 'ghost']}
        self.assertEqual(coupling.pairs(tasks, waves), [('a', 'b')])

    def test_max_pairs_cap_is_deterministic(self):
        ids = [f't{i:03d}' for i in range(15)]  # 15 choose 2 = 105 > MAX_PAIRS
        tasks = {i: task(i) for i in ids}
        waves = {'1': ids}
        first = coupling.pairs(tasks, waves)
        second = coupling.pairs(tasks, waves)
        self.assertEqual(len(first), coupling.MAX_PAIRS)
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(first))


class ParseTasksTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.plan = Path(self.dir.name)

    def _write(self, name, text):
        (self.plan / name).write_text(text)

    def test_malformed_rows_are_skipped_without_raising(self):
        self._write('tasks.tsv', '\n'.join([
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle',
            'a\t-\tlow\ta.py\topus\topus\tGood row',
            'too\tfew\tcolumns',
            '',
            'b\t-\tlow\tb.py\topus\topus\tAnother good row',
        ]))
        parsed = coupling.parse_tasks(self.plan)
        self.assertEqual(set(parsed), {'a', 'b'})
        self.assertEqual(parsed['a']['title'], 'Good row')

    def test_missing_tasks_tsv_yields_nothing(self):
        self.assertEqual(coupling.parse_tasks(self.plan), {})

    def test_missing_waves_tsv_yields_nothing(self):
        self.assertEqual(coupling.parse_waves(self.plan), {})

    def test_waves_tsv_is_wave_to_task_ids(self):
        self._write('waves.tsv', '1\ta\n1\tb\n2\tc\n')
        self.assertEqual(coupling.parse_waves(self.plan), {'1': ['a', 'b'], '2': ['c']})


class CoupledPairsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)

    def test_a_single_task_wave_asks_nothing(self):
        tasks = [task('a')]
        waves = {'1': ['a']}
        with patch('forge_jev.coupling.ask') as mocked:
            self.assertEqual(coupling.coupled_pairs(self.repo, tasks=tasks, waves=waves), [])
        mocked.assert_not_called()

    def test_a_failed_request_is_none_not_an_empty_list(self):
        tasks = [task('a'), task('b')]
        waves = {'1': ['a', 'b']}
        with patch('forge_jev.coupling.ask', return_value=None):
            found = coupling.coupled_pairs(self.repo, tasks=tasks, waves=waves)
        self.assertIsNone(found)

    def test_only_pairs_at_or_above_threshold_are_returned_highest_first(self):
        tasks = [task('a'), task('b'), task('c'), task('d')]
        waves = {'1': ['a', 'b', 'c', 'd']}
        candidate_pairs = coupling.pairs({t['id']: t for t in tasks}, waves)
        # 6 pairs for 4 tasks; give distinct probabilities so ranking is unambiguous.
        values = [0.95, 0.10, 0.61, 0.60, 0.59, 0.80]
        answers = {f'p{i}': dict(type='noul', noul=v) for i, v in enumerate(values[:len(candidate_pairs)])}
        with patch('forge_jev.coupling.ask', return_value=dict(answers=answers)):
            found = coupling.coupled_pairs(self.repo, tasks=tasks, waves=waves)
        # coupling_warn, not gate_warn: this rubric's own measured scale. Everything
        # above it is ranked, then capped -- the ordering is the signal, and a plan that
        # warns about half its pairs teaches people to ignore the warning.
        warn = DEFAULT_THRESHOLDS['coupling_warn']
        probabilities = [p for _a, _b, p in found]
        self.assertEqual(probabilities, sorted(probabilities, reverse=True))
        self.assertTrue(all(p >= warn for p in probabilities))
        above = sorted((v for v in values[:len(candidate_pairs)] if v >= warn), reverse=True)
        self.assertEqual(probabilities, above[:coupling.MAX_WARNINGS])

    def test_pairs_are_never_formed_across_waves(self):
        tasks = [task('a'), task('b'), task('c')]
        waves = {'1': ['a', 'b'], '2': ['c']}
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['questions'] = questions
            return dict(answers={qid: dict(type='noul', noul=0.9) for qid in questions})

        with patch('forge_jev.coupling.ask', side_effect=fake_ask):
            found = coupling.coupled_pairs(self.repo, tasks=tasks, waves=waves)
        self.assertEqual(len(captured['questions']), 1)  # only the (a, b) pair
        pair_ids = {(a, b) for a, b, _p in found}
        self.assertEqual(pair_ids, {('a', 'b')})

    def test_max_pairs_cap_is_respected_in_a_real_request(self):
        ids = [f't{i:03d}' for i in range(15)]
        tasks = [task(i) for i in ids]
        waves = {'1': ids}
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['questions'] = questions
            return dict(answers={qid: dict(type='noul', noul=0.0) for qid in questions})

        with patch('forge_jev.coupling.ask', side_effect=fake_ask):
            coupling.coupled_pairs(self.repo, tasks=tasks, waves=waves)
        self.assertEqual(len(captured['questions']), coupling.MAX_PAIRS)


if __name__ == '__main__':
    unittest.main()


class MeasuredThresholdTests(unittest.TestCase):
    """This gate borrowed gate_warn and therefore never fired, on any plan, ever."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.plan = Path(self.dir.name)

    def _tasks(self, n):
        return [dict(id=f't{i}', title=f'Task {i}', files=f'f{i}.py', difficulty='low',
                     prompt=f'requirements for t{i}') for i in range(n)]

    def _found(self, probabilities):
        tasks = self._tasks(len(probabilities) + 1)
        waves = {'1': [task['id'] for task in tasks]}
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['state'] = state
            answers = {qid: dict(type='noul', noul=p)
                       for qid, p in zip(sorted(questions), probabilities)}
            return dict(answers=answers)

        with patch('forge_jev.coupling.ask', side_effect=fake_ask):
            found = coupling.coupled_pairs(self.plan, tasks=tasks, waves=waves)
        return found, captured.get('state')

    def test_coupling_has_its_own_threshold_far_below_gate_warn(self):
        # Measured over 11 same-wave pairs from a real plan with known outcomes, 5 runs
        # each: coupling probabilities live at 0.09-0.27. gate_warn's 0.60 was not a
        # strict threshold, it was unreachable -- the feature had never fired once.
        warn = DEFAULT_THRESHOLDS['coupling_warn']
        self.assertLess(warn, DEFAULT_THRESHOLDS['gate_warn'])
        self.assertGreater(warn, 0.18, 'must clear the highest measured independent pair')
        self.assertLess(warn, 0.22, 'must stay under the lowest measured coupled pair')

    def test_the_requirements_reach_the_model(self):
        # Without them this rubric had no signal at all: the two pairs that produced a
        # defect that really shipped scored lowest of everything measured.
        _found, state = self._found([0.9])
        self.assertIn('requirements for t0', str(state))

    def test_no_more_than_three_pairs_are_reported(self):
        # Rank is the signal, not the absolute value, and the distribution moves with the
        # plan. At a fixed threshold a real 8-task plan warned about 12 of its 21 pairs.
        found, _state = self._found([0.9] * 10)
        self.assertLessEqual(len(found), coupling.MAX_WARNINGS)

    def test_the_three_reported_are_the_highest_scoring(self):
        found, _state = self._found([0.21, 0.95, 0.22, 0.80, 0.60])
        self.assertEqual([p for _a, _b, p in found], [0.95, 0.8, 0.6])

    def test_a_plan_with_nothing_above_the_threshold_reports_nothing(self):
        found, _state = self._found([0.19, 0.10, 0.05])
        self.assertEqual(found, [])

