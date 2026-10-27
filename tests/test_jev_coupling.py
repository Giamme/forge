"""Plan-time forward-dependency check: warns on earlier-wave tasks written against later ones, never edits the plan."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from forge_jev import DEFAULT_THRESHOLDS, RETIRED_THRESHOLDS, SITE_CAPABILITY, cli, coupling  # noqa: E402


def task(task_id, title='t', files='-', difficulty='low', deps='-'):
    return dict(id=task_id, title=title, files=files, difficulty=difficulty, deps=deps)


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


class ForwardPairTests(unittest.TestCase):
    """The ordering question: an earlier-wave task written against a later one.

    jevscope's `readme` ran in wave 1 documenting the CLI that wave 2 built. It passed
    its own review -- the thing it was wrong about did not exist yet -- and shipped
    describing an interface nobody ever wrote.
    """

    def test_pairs_run_earlier_wave_to_later_only(self):
        tasks = {i: task(i) for i in ('a', 'b', 'c')}
        waves = {'1': ['a', 'b'], '2': ['c']}
        found = coupling.forward_pairs(tasks, waves)
        self.assertEqual(sorted(found), [('a', 'c'), ('b', 'c')])
        # Never the safe direction, and never within a wave.
        self.assertNotIn(('c', 'a'), found)
        self.assertNotIn(('a', 'b'), found)

    def test_a_declared_dependency_is_not_a_forward_dependency(self):
        # If LATER were named in EARLIER's deps, forge would not have placed EARLIER
        # first, so the pair cannot arise. Only the undeclared ordering is a risk.
        tasks = {'doc': task('doc', deps='cli'), 'cli': task('cli')}
        waves = {'1': ['doc'], '2': ['cli']}
        self.assertEqual(coupling.forward_pairs(tasks, waves), [])

    def test_waves_sort_numerically_not_as_strings(self):
        # '10' must follow '9'. Sorted as strings it precedes '2', which would invert
        # the direction of every pair involving a plan with ten or more waves.
        tasks = {i: task(i) for i in ('early', 'late')}
        waves = {'9': ['early'], '10': ['late']}
        self.assertEqual(coupling.forward_pairs(tasks, waves), [('early', 'late')])

    def test_a_single_wave_plan_has_no_forward_pairs(self):
        tasks = {i: task(i) for i in ('a', 'b')}
        self.assertEqual(coupling.forward_pairs(tasks, {'1': ['a', 'b']}), [])

    def test_the_pair_budget_is_capped_and_deterministic(self):
        early = [f'e{i:03d}' for i in range(30)]
        late = [f'l{i:03d}' for i in range(30)]
        tasks = {i: task(i) for i in early + late}
        waves = {'1': early, '2': late}
        found = coupling.forward_pairs(tasks, waves)
        self.assertEqual(len(found), coupling.MAX_FORWARD_PAIRS)
        self.assertEqual(found, sorted(found))
        self.assertEqual(found, coupling.forward_pairs(tasks, waves))


class ForwardDependencyTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.repo = Path(self.dir.name)

    def _ask(self, values):
        tasks = [task('doc'), task('cli')]
        waves = {'1': ['doc'], '2': ['cli']}
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['state'] = state
            captured['site'] = kwargs.get('site')
            return dict(answers={qid: dict(type='noul', noul=v)
                                 for qid, v in zip(sorted(questions), values)})

        with patch('forge_jev.coupling.ask', side_effect=fake_ask):
            found = coupling.forward_dependencies(self.repo, tasks=tasks, waves=waves)
        return found, captured

    def test_a_single_wave_plan_asks_nothing(self):
        with patch('forge_jev.coupling.ask') as mocked:
            self.assertEqual(coupling.forward_dependencies(
                self.repo, tasks=[task('a'), task('b')], waves={'1': ['a', 'b']}), [])
        mocked.assert_not_called()

    def test_a_failed_request_is_none_not_an_empty_list(self):
        with patch('forge_jev.coupling.ask', return_value=None):
            self.assertIsNone(coupling.forward_dependencies(
                self.repo, tasks=[task('a'), task('b')],
                waves={'1': ['a'], '2': ['b']}))

    def test_it_is_logged_under_its_own_site(self):
        # The run log is the only place a threshold can be re-measured, so the site
        # name is part of the contract.
        _found, captured = self._ask([0.9])
        self.assertEqual(captured['site'], 'jev-forward')

    def test_above_the_threshold_warns(self):
        # Measured on the real plan, 5 runs: readme->cli 0.88-0.90, every independent
        # cross-wave pair 0.05-0.15. forward_warn sits in the middle of that gap.
        found, _c = self._ask([0.88])
        self.assertEqual(found, [('doc', 'cli', 0.88)])

    def test_below_the_threshold_says_nothing(self):
        found, _c = self._ask([0.15])
        self.assertEqual(found, [])

    def test_the_requirements_reach_the_model(self):
        tasks = [dict(id='doc', title='d', files='README.md', difficulty='low', deps='-',
                      prompt='DOC-REQUIREMENTS-MARKER'),
                 dict(id='cli', title='c', files='cli.py', difficulty='low', deps='-',
                      prompt='')]
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['state'] = state
            return dict(answers={qid: dict(type='noul', noul=0.9) for qid in questions})

        with patch('forge_jev.coupling.ask', side_effect=fake_ask):
            coupling.forward_dependencies(self.repo, tasks=tasks,
                                          waves={'1': ['doc'], '2': ['cli']})
        self.assertIn('DOC-REQUIREMENTS-MARKER', str(captured['state']))

    def test_no_more_than_three_are_reported_highest_first(self):
        early = [task(f'e{i}') for i in range(5)]
        tasks = early + [task('z')]
        waves = {'1': [t['id'] for t in early], '2': ['z']}

        def fake_ask(state, questions, **kwargs):
            values = [0.99, 0.95, 0.60, 0.91, 0.70]
            return dict(answers={qid: dict(type='noul', noul=v)
                                 for qid, v in zip(sorted(questions), values)})

        with patch('forge_jev.coupling.ask', side_effect=fake_ask):
            found = coupling.forward_dependencies(self.repo, tasks=tasks, waves=waves)
        self.assertEqual([p for _a, _b, p in found], [0.99, 0.95, 0.91])


class ForwardThresholdTests(unittest.TestCase):
    def test_forward_warn_sits_in_the_measured_gap(self):
        # 7 cross-wave pairs of a real plan, 5 runs each. The one genuine forward
        # dependency -- `readme`, which shipped documenting an interface nobody built --
        # scored 0.88-0.90; the other six scored 0.05-0.15.
        warn = DEFAULT_THRESHOLDS['forward_warn']
        self.assertGreater(warn, 0.15 + 0.07, 'must clear the worst true negative by more than noise')
        self.assertLess(warn, 0.88 - 0.07, 'must stay clear of the one known positive by the same')

    def test_coupling_warn_is_retired_not_a_live_threshold(self):
        # The same-wave rubric is gone: a stored coupling_warn is safe to delete, and
        # doctor must say so rather than call it unknown.
        self.assertNotIn('coupling_warn', DEFAULT_THRESHOLDS)
        self.assertIn('coupling_warn', RETIRED_THRESHOLDS)


class CmdCouplingTests(unittest.TestCase):
    """`forge-jev.py coupling` asks only the forward rubric."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.plan = Path(self.dir.name)
        # a and b share wave 1 (the removed same-wave pair); c is wave 2, so a->c and
        # b->c are the only candidates for the forward rubric.
        (self.plan / 'tasks.tsv').write_text('\n'.join([
            'a\t-\tlow\ta.py\topus\topus\tTask a',
            'b\t-\tlow\tb.py\topus\topus\tTask b',
            'c\t-\tlow\tc.py\topus\topus\tTask c',
        ]) + '\n')
        (self.plan / 'waves.tsv').write_text('1\ta\n1\tb\n2\tc\n')

    def _run(self, answer_for, ready=True, as_json=False):
        calls = []

        def fake_ask(state, questions, **kwargs):
            calls.append((kwargs.get('site'), dict(questions)))
            return dict(answers={qid: dict(type='noul', noul=answer_for(qid))
                                 for qid in questions})

        def fake_ready(site, config=None):
            return ready and site == 'jev-forward'

        args = argparse.Namespace(plan=str(self.plan), repo=str(self.plan), json=as_json)
        out = io.StringIO()
        with patch('forge_jev.cli.load_config', return_value={}), \
                patch('forge_jev.cli.site_ready', side_effect=fake_ready), \
                patch('forge_jev.coupling.ask', side_effect=fake_ask), \
                contextlib.redirect_stdout(out):
            rc = cli.cmd_coupling(args)
        return rc, out.getvalue(), calls

    def test_only_the_forward_rubric_is_asked_and_only_forward_rows_come_back(self):
        # f0 is (a, c), f1 is (b, c). The same-wave pair (a, b) must never be asked.
        rc, out, calls = self._run(lambda qid: 0.9 if qid == 'f0' else 0.1)
        self.assertEqual(rc, 0)
        self.assertEqual([site for site, _q in calls], ['jev-forward'])
        self.assertEqual(sorted(calls[0][1]), ['f0', 'f1'])
        self.assertEqual(out.strip().split('\t')[3], 'forward')
        self.assertEqual(out.strip().split('\t')[:2], ['a', 'c'])
        written = json.loads((self.plan / 'jev-coupling.json').read_text())
        self.assertEqual([(r['a'], r['b'], r['kind']) for r in written], [('a', 'c', 'forward')])

    def test_nothing_found_exits_3_and_writes_nothing(self):
        rc, out, _calls = self._run(lambda qid: 0.05)
        self.assertEqual((rc, out), (3, ''))
        self.assertFalse((self.plan / 'jev-coupling.json').exists())

    def test_it_is_gated_on_jev_forward_even_when_other_gates_sites_are_ready(self):
        # jev-drift is also a gates site; only jev-forward readiness may let this run.
        self.assertEqual(SITE_CAPABILITY['jev-forward'], 'gates')
        self.assertNotIn('jev-coupling', SITE_CAPABILITY)
        rc, out, calls = self._run(lambda qid: 0.9, ready=False)
        self.assertEqual((rc, out, calls), (3, '', []))


if __name__ == '__main__':
    unittest.main()
