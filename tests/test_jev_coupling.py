"""Plan-time forward-dependency check: warns on earlier-wave tasks written against later ones, never edits the plan."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from forge_jev import DEFAULT_THRESHOLDS, RETIRED_THRESHOLDS, SITE_CAPABILITY, cli, coupling, questions  # noqa: E402
from test_decide_labels import LabelsCase, T0, noul as labelled  # noqa: E402


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


ROOT = Path(__file__).resolve().parent.parent
TASKS = [
    'readme\t-\tlow\tREADME.md\topus\topus\tDocument the CLI',
    'lib\t-\tlow\tlib.py\topus\topus\tThe library',
    'cli\t-\tlow\tcli.py\topus\topus\tThe CLI',
]


class ForwardActingTests(unittest.TestCase):
    """The forward gate `on` makes the earlier task wait for the later one (#39)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.plan = Path(self.dir.name)
        self.write(TASKS, '1\treadme\n1\tlib\n2\tcli\n')

    def write(self, rows, waves):
        (self.plan / 'tasks.tsv').write_text('\n'.join(rows) + '\n')
        (self.plan / 'waves.tsv').write_text(waves)

    def tasks_bytes(self):
        return (self.plan / 'tasks.tsv').read_bytes()

    def deps(self):
        return {line.split('\t')[0]: line.split('\t')[1]
                for line in self.tasks_bytes().decode().splitlines()}

    def run_gate(self, mode, flagged=('readme', 'cli')):
        """cmd_coupling with `mode`; the pair `flagged` is the only one the model fires on."""
        def fake_ask(state, questions, **kwargs):
            hot = [qid for qid, q in questions.items()
                   if '%s (' % flagged[0] in str(q) and '%s (' % flagged[1] in str(q)]
            return dict(answers={qid: dict(type='noul', noul=0.9 if qid in hot else 0.05)
                                 for qid in questions})

        args = argparse.Namespace(plan=str(self.plan), repo=str(self.plan), json=False)
        out = io.StringIO()
        with patch('forge_jev.cli.load_config', return_value={}), \
                patch('forge_jev.cli.site_ready', return_value=True), \
                patch('forge_jev.cli.gate_mode', return_value=mode), \
                patch('forge_jev.coupling.ask', side_effect=fake_ask), \
                contextlib.redirect_stdout(out):
            rc = cli.cmd_coupling(args)
        return rc, out.getvalue()

    def recorded(self):
        return json.loads((self.plan / 'plan-gates.json').read_text())

    def test_on_adds_the_dependency_prints_added_and_records_it(self):
        rc, out = self.run_gate('on')
        self.assertEqual(rc, 0)
        self.assertEqual(self.deps(), dict(readme='cli', lib='-', cli='-'))
        [row] = [line.split('\t') for line in out.splitlines()]
        self.assertEqual(row[:2] + row[3:], ['readme', 'cli', 'added'])
        self.assertEqual(self.recorded()['forward']['added'],
                         [dict(earlier='readme', later='cli', p=0.9)])

    def test_a_dependency_that_would_form_a_cycle_is_skipped_and_printed_as_cycle(self):
        # cli waits for lib, lib waits for readme: readme waiting for cli would close the loop.
        rows = [TASKS[0], TASKS[1].replace('\t-\t', '\treadme\t', 1), TASKS[2].replace('\t-\t', '\tlib\t', 1)]
        self.write(rows, '1\treadme\n2\tlib\n3\tcli\n')
        before = self.tasks_bytes()
        rc, out = self.run_gate('on')
        self.assertEqual(self.tasks_bytes(), before)
        [row] = [line.split('\t') for line in out.splitlines()]
        self.assertEqual(row[:2] + row[3:], ['readme', 'cli', 'cycle'])
        forward = self.recorded()['forward']
        self.assertEqual(forward['skipped_cycle'], [dict(earlier='readme', later='cli', p=0.9)])
        self.assertNotIn('added', forward)

    def test_shadow_records_would_add_prints_nothing_and_leaves_tasks_alone(self):
        before = self.tasks_bytes()
        rc, out = self.run_gate('shadow')
        self.assertEqual((rc, out), (3, ''))
        self.assertEqual(self.tasks_bytes(), before)
        self.assertEqual(self.recorded()['forward'], dict(would_add=[dict(earlier='readme', later='cli', p=0.9)]))
        self.assertTrue((self.plan / 'jev-coupling.json').exists())

    def test_off_leaves_tasks_and_the_record_alone(self):
        before = self.tasks_bytes()
        self.run_gate('off')
        self.assertEqual(self.tasks_bytes(), before)
        self.assertFalse((self.plan / 'plan-gates.json').exists())

    def test_the_added_entry_survives_a_later_run_while_the_dependency_stands(self):
        self.run_gate('on')
        # Nothing is warned about now (the dep is declared); the record must stay.
        self.run_gate('on', flagged=('lib', 'cli'))
        self.assertEqual(self.recorded()['forward']['added'][0]['earlier'], 'readme')

    def test_the_waves_put_the_earlier_task_after_the_later_one(self):
        self.run_gate('on')
        (self.plan / 'waves.tsv').unlink()
        subprocess.run(['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), '_waves', str(self.plan)], check=True)
        waves = {line.split('\t')[1]: int(line.split('\t')[0])
                 for line in (self.plan / 'waves.tsv').read_text().splitlines()}
        self.assertGreater(waves['readme'], waves['cli'])


class ForwardLabelTests(LabelsCase):
    """The label does not credit the gate for the dependency it wrote itself (#39)."""

    def body(self, pairs):
        describe = lambda t: '%s (%s) files: %s' % (t, t.upper(), t + '.py')
        return dict(state=dict(tasks=[]), model='jev-latest',
                    questions={'f%d' % i: questions.forward_dependency(describe(a), describe(b))
                               for i, (a, b) in enumerate(pairs)})

    def plan(self, ts, deps, added=()):
        gates = dict(forward=dict(added=[dict(earlier=a, later=b, p=0.9) for a, b in added])) if added else None
        extra = dict(plan_gates=gates) if gates else {}
        self.add('plan', run='run1', ts=ts, tasks={t: dict(title=t, deps=d) for t, d in deps.items()}, **extra)

    def merge_all(self, *tasks):
        for task in tasks:
            self.add('event', run='run1', ts=T0 + 100, task=task, name='merge', status='MERGED')

    def ask_pair(self, a, b, p=0.8):
        self.ask('jev-forward', self.body([(a, b)]), dict(f0=labelled(p)), run='run1', ts=T0)

    def test_a_gate_added_dependency_that_stays_is_unlabelled_even_when_both_merged(self):
        self.plan(T0 - 10, dict(readme='', cli=''))
        self.ask_pair('readme', 'cli')
        self.plan(T0 + 10, dict(readme='cli', cli=''), added=[('readme', 'cli')])
        self.plan(T0 + 20, dict(readme='cli', cli=''))
        self.merge_all('readme', 'cli')
        self.assertEqual(self.collect('jev-forward'), [])

    def test_a_gate_added_dependency_the_user_removed_is_false(self):
        self.plan(T0 - 10, dict(readme='', cli=''))
        self.ask_pair('readme', 'cli')
        self.plan(T0 + 10, dict(readme='cli', cli=''), added=[('readme', 'cli')])
        self.plan(T0 + 20, dict(readme='', cli=''))
        [row] = self.collect('jev-forward')
        self.assertEqual(row['labels'], dict(f0=False))

    def test_a_dependency_a_person_added_is_still_true(self):
        self.plan(T0 - 10, dict(readme='', cli=''))
        self.ask_pair('readme', 'cli')
        self.plan(T0 + 10, dict(readme='cli', cli=''))
        [row] = self.collect('jev-forward')
        self.assertEqual(row['labels'], dict(f0=True))

    def test_a_pair_the_gate_never_flagged_merged_without_the_dependency_is_still_false(self):
        self.plan(T0 - 10, dict(readme='', cli='', api='', ui=''))
        self.ask_pair('api', 'ui', 0.3)
        self.plan(T0 + 10, dict(readme='cli', cli='', api='', ui=''), added=[('readme', 'cli')])
        self.merge_all('api', 'ui')
        [row] = self.collect('jev-forward')
        self.assertEqual(row['labels'], dict(f0=False))


if __name__ == '__main__':
    unittest.main()
