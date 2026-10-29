"""The attribution gate (GitHub #45): in `on`, `forge-parallel.sh attribute` probes the model's top
suspect's boundary before its binary search; `shadow` only records the ranking; the label says
whether the suspect is a verified breaking merge.

End-to-end cases reuse test_attribute.py's run of three merged tasks (a, b, c) and its verify script.
The skill directory's forge-jev.py is a wrapper that answers the `culprit` request from STUB_RANK
(a JSON object, id -> p) through a fixture it writes itself, and logs each request body to
STUB_REQUESTS, the way test_message_gate.py does.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

import test_attribute as attribute_fixtures
import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_dataset as dataset  # noqa: E402
from forge_decide import label_attribution, sites  # noqa: E402
from forge_jev import attribution, questions  # noqa: E402
from test_decide_labels import LabelsCase, T0  # noqa: E402

CLASH = attribute_fixtures.CLASH
# Every prefix with a.txt fails: a is the breaking merge.
A_BREAKS = '[ -e a.txt ] && { echo "a breaks it"; exit 1; }\nexit 0\n'
KEY = 'ts-attribution-test-key'

WRAPPER = '''#!/usr/bin/env python3
import hashlib, json, os, sys
from pathlib import Path
sys.path.insert(0, %r)
from forge_jev import client
real = client._canonical


def spy(value):
    if isinstance(value, dict) and 'questions' in value:
        with open(os.environ['STUB_REQUESTS'], 'a') as log:
            log.write(json.dumps(value) + '\\n')
        if os.environ.get('STUB_RANK'):
            probs = json.loads(os.environ['STUB_RANK'])
            top = max(probs, key=probs.get)
            payload = dict(answers=dict(culprit=dict(type='choice', choice=top, confidence=probs[top],
                                                     probabilities=probs)),
                           model=value['model'])
            Path(os.environ['FORGE_JEV_FIXTURES'], hashlib.sha256(real(value)).hexdigest() + '.json').write_text(
                json.dumps(payload))
    return real(value)


client._canonical = spy
from forge_jev.cli import main
raise SystemExit(main())
''' % str(ROOT / 'scripts')


def ranking(**p):
    """A full distribution over a, b, c and none: what is not named shares the rest."""
    rest = max(0.0, 1.0 - sum(p.values()))
    missing = [name for name in ('a', 'b', 'c', 'none') if name not in p]
    return dict(p, **{name: rest / len(missing) for name in missing})


class GateCase(unittest.TestCase):
    setUp_fixture = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    scenario = fixtures.ForgeTests.scenario
    failed_run = attribute_fixtures.AttributeTests.failed_run
    result = attribute_fixtures.AttributeTests.result
    integration_tip = attribute_fixtures.AttributeTests.integration_tip
    status_of = attribute_fixtures.AttributeTests.status_of

    def setUp(self):
        self.setUp_fixture()
        self.skill = self.root / 'skill'
        (self.skill / 'scripts').mkdir(parents=True)
        for entry in ROOT.iterdir():
            if entry.name not in ('scripts', '.git') and not entry.name.startswith('.forge'):
                (self.skill / entry.name).symlink_to(entry)
        for entry in (ROOT / 'scripts').iterdir():
            (self.skill / 'scripts' / entry.name).symlink_to(entry)
        wrapper = self.skill / 'scripts' / 'forge-jev.py'
        wrapper.unlink()
        wrapper.write_text(WRAPPER)
        self.fixtures_dir = self.root / 'fixtures'
        self.fixtures_dir.mkdir()
        self.requests = self.root / 'requests.jsonl'

    def run_script(self, name, *args):
        return subprocess.run(['bash', str(self.skill / 'scripts' / name), *map(str, args)], cwd=self.repo,
                              env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def attribute(self, *args):
        return self.run_script('forge-parallel.sh', 'attribute', self.plan, *args)

    def gate(self, mode, rank=None):
        """The attribution gate's mode, and the distribution the model answers with (None: the
        request has no fixture, so the verb gets no answer)."""
        self.env.pop('FORGE_JEV', None)
        self.env.update(XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state'),
                        TYPESAFE_API_KEY=KEY, FORGE_JEV_FIXTURES=str(self.fixtures_dir),
                        STUB_REQUESTS=str(self.requests))
        decide = subprocess.run(['python3', str(ROOT / 'scripts' / 'forge-decide.py'), 'gate', 'attribution', mode,
                                 *(['--force'] if mode == 'on' else [])], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(decide.returncode, 0, decide.stdout)
        if rank is not None:
            self.env['STUB_RANK'] = json.dumps(rank)

    def asked(self):
        if not self.requests.exists():
            return []
        return [json.loads(line) for line in self.requests.read_text().splitlines()]


class OnTests(GateCase):
    def test_a_right_guess_is_confirmed_after_the_base_and_two_prefixes(self):
        self.failed_run(CLASH)
        self.gate('on', ranking(b=0.8, a=0.1))
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertEqual(found['culprit'], 'b')
        self.assertEqual(found['verification_runs'], 3)
        self.assertEqual(sorted(s['prefix'] for s in found['steps']), [0, 1, 2, 3])
        gate = found['gate']
        self.assertEqual((gate['mode'], gate['tried'], gate['outcome']), ('on', 'b', 'confirmed'))
        self.assertEqual([item['id'] for item in gate['ranking']][0], 'b')
        self.assertEqual(gate['ranking'][0]['p'], 0.8)
        self.assertEqual({item['id'] for item in gate['ranking']}, {'a', 'b', 'c', 'none'})
        self.assertIn("breaks verification (prefix 1 passes, 2 fails; 3 verification runs) "
                      "(the decision model's first guess)", r.stdout)
        self.assertIn('a and b clash', (self.plan / 'tasks/b/verification.md').read_text())

    def test_the_request_carries_the_run_the_log_and_each_merge(self):
        self.failed_run(CLASH)
        self.gate('on', ranking(b=0.8))
        self.attribute()
        [body] = self.asked()
        state = body['state']
        self.assertEqual(state['run'], (self.plan / 'run_id').read_text().strip())
        self.assertIn('a and b clash', state['log'])
        self.assertEqual([(t['id'], t['title'], t['files']) for t in state['tasks']],
                         [('a', 'A', ['a.txt']), ('b', 'B', ['b.txt']), ('c', 'C', ['c.txt'])])
        self.assertIn('b.txt', state['tasks'][1]['diff'])
        self.assertEqual(list(body['questions']), ['culprit'])
        self.assertEqual(list(body['questions']['culprit']['criteria']), ['a', 'b', 'c', 'none'])

    def test_a_wrong_guess_before_the_boundary_continues_from_its_prefix(self):
        self.failed_run(CLASH)
        self.gate('on', ranking(a=0.7, b=0.1))
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertEqual(found['culprit'], 'b')
        self.assertEqual((found['gate']['tried'], found['gate']['outcome']), ('a', 'refuted'))
        self.assertEqual(self.status_of(found['steps'], 1), ['PASS'])
        self.assertEqual(self.status_of(found['steps'], 2), ['FAIL'])
        self.assertNotIn("first guess", r.stdout)

    def test_a_wrong_guess_after_the_boundary_continues_below_it(self):
        self.failed_run(A_BREAKS)
        self.gate('on', ranking(c=0.7, a=0.1))
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertEqual(found['culprit'], 'a')
        self.assertEqual((found['gate']['tried'], found['gate']['outcome']), ('c', 'refuted'))
        self.assertEqual(self.status_of(found['steps'], 2), ['FAIL'])
        self.assertEqual(self.status_of(found['steps'], 1), ['FAIL'])
        self.assertEqual(found['verification_runs'], 3)             # the base, prefix 2, prefix 1
        # the tip was FAIL from the run: it is never verified again
        self.assertEqual([s['source'] for s in found['steps'] if s['prefix'] == 3], ['run'])

    def test_none_on_top_leaves_the_plain_search_to_decide(self):
        self.failed_run(CLASH)
        self.gate('on', ranking(none=0.9))
        self.assertEqual(self.attribute().returncode, 0)
        found = self.result()
        self.assertEqual(found['culprit'], 'b')
        self.assertEqual((found['gate']['mode'], found['gate']['tried'], found['gate']['outcome']),
                         ('on', None, None))
        self.assertEqual(found['gate']['ranking'][0]['id'], 'none')


class OtherModeTests(GateCase):
    def test_shadow_records_the_ranking_and_the_plain_search_decides(self):
        self.failed_run(CLASH)
        plain = self.attribute()
        self.assertEqual(plain.returncode, 0, plain.stdout)
        baseline = self.result()
        self.assertIsNone(baseline['gate'])
        self.gate('shadow', ranking(a=0.7, b=0.1))
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        found = self.result()
        self.assertEqual(len(self.asked()), 1)
        self.assertEqual((found['gate']['mode'], found['gate']['tried'], found['gate']['outcome']),
                         ('shadow', None, None))
        self.assertEqual(found['gate']['ranking'][0], dict(id='a', p=0.7))
        self.assertEqual(found['verification_runs'], baseline['verification_runs'])
        self.assertEqual([(s['prefix'], s['status']) for s in found['steps']],
                         [(s['prefix'], s['status']) for s in baseline['steps']])
        self.assertEqual(found['culprit'], baseline['culprit'])
        self.assertNotIn("first guess", r.stdout)

    def test_off_asks_nothing_and_records_no_gate(self):
        self.failed_run(CLASH)
        self.gate('off', ranking(b=0.8))
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.asked(), [])
        found = self.result()
        self.assertIsNone(found['gate'])
        self.assertEqual(found['culprit'], 'b')

    def test_a_verb_that_exits_3_leaves_the_plain_search_to_decide(self):
        self.failed_run(CLASH)
        self.gate('on')                     # no fixture: the model does not answer, the verb exits 3
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(len(self.asked()), 1)
        found = self.result()
        self.assertEqual(found['gate'], dict(mode='on'))
        self.assertEqual(found['culprit'], 'b')
        self.assertNotIn("first guess", r.stdout)

    def test_a_search_that_stops_before_the_tip_asks_nothing(self):
        self.failed_run(attribute_fixtures.NEVER)          # the base fails: no culprit to rank
        self.gate('on', ranking(b=0.8))
        self.assertEqual(self.attribute().returncode, 0)
        self.assertTrue(self.result()['base_fails'])
        self.assertEqual(self.asked(), [])

    def freeze(self, **selection):
        (self.plan / 'jev-selection.json').write_text(json.dumps(dict(choice=None, act=False, shadow=False,
                                                                        capabilities={}, **selection)))

    def test_a_plan_frozen_with_no_gates_asks_nothing_whatever_the_global_setting(self):
        self.failed_run(CLASH)
        self.gate('on', ranking(b=0.8))
        self.freeze(no_gates=True, gates={})
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.asked(), [])
        found = self.result()
        self.assertIsNone(found['gate'])
        self.assertEqual(found['culprit'], 'b')

    def test_a_gate_frozen_on_acts_although_the_global_setting_is_off(self):
        self.failed_run(CLASH)
        self.gate('off', ranking(b=0.8))
        self.freeze(no_gates=False, gates={'attribution': 'on'})
        r = self.attribute()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(len(self.asked()), 1)
        found = self.result()
        self.assertEqual((found['gate']['mode'], found['gate']['tried'], found['gate']['outcome']),
                         ('on', 'b', 'confirmed'))


class StateTests(GateCase):
    def test_the_merges_are_the_ones_the_search_found(self):
        self.failed_run(CLASH)
        self.assertEqual(self.attribute().returncode, 0)
        found = self.result()
        self.assertEqual(attribution.merges(self.plan, self.repo),
                         [(m['commit'], m['id']) for m in found['merges']])

    def test_a_long_diff_is_cut_with_a_marker(self):
        with mock.patch.object(attribution, '_git', return_value='x' * 5000):
            text = attribution._diff('repo', 'abc')
        self.assertEqual(text, 'x' * 3000 + '\n[cut]')
        with mock.patch.object(attribution, '_git', return_value='x' * 3000):
            self.assertEqual(attribution._diff('repo', 'abc'), 'x' * 3000)

    def test_the_log_is_its_last_150_lines(self):
        path = self.root / 'v.log'
        path.write_text('\n'.join('line %d' % i for i in range(400)))
        tail = attribution._tail(path).splitlines()
        self.assertEqual((len(tail), tail[0], tail[-1]), (150, 'line 250', 'line 399'))

    def test_no_opinion_without_a_log_or_with_fewer_than_two_merges(self):
        self.failed_run(CLASH)
        args = mock.Mock(plan=str(self.plan), repo=str(self.repo), log=str(self.root / 'missing.log'))
        with mock.patch.object(attribution, 'site_ready', return_value=True), \
             mock.patch.object(attribution, 'ask') as ask:
            self.assertEqual(attribution.main(args), 3)
            args.log = str(self.plan / 'verification.log')
            with mock.patch.object(attribution, 'merges', return_value=[('c1', 'a')]):
                self.assertEqual(attribution.main(args), 3)
            ask.assert_not_called()
        with mock.patch.object(attribution, 'site_ready', return_value=False):
            self.assertEqual(attribution.main(args), 3)


class MeasurementTests(unittest.TestCase):
    def body(self, log='tests/test_a.py failed in a.py'):
        return dict(state=dict(run='r', log=log, tasks=[dict(id='a', title='A', files=['a.py', 'x.py'], diff=''),
                                                        dict(id='b', title='B', files=['b.py'], diff='')]),
                    model='jev-latest', questions=dict(culprit=questions.culprit(['a', 'b'])))

    def answers(self, **p):
        return dict(culprit=dict(type='choice', choice=max(p, key=p.get), confidence=max(p.values()), probabilities=p))

    def test_decisions_are_the_options_but_none(self):
        self.assertEqual(attribution.decisions(self.body()), ['culprit:a', 'culprit:b'])
        self.assertEqual(attribution.decisions(None), [])
        self.assertEqual(attribution.decisions(dict(questions={})), [])

    def test_score_is_the_options_probability(self):
        answers = self.answers(a=0.2, b=0.7, none=0.1)
        self.assertEqual(attribution.score(answers, 'culprit:b'), 0.7)
        self.assertIsNone(attribution.score(answers, 'culprit:c'))
        self.assertIsNone(attribution.score({}, 'culprit:a'))

    def test_fired_is_the_top_option_unless_it_is_none(self):
        self.assertEqual(attribution.fired(self.answers(a=0.2, b=0.7, none=0.1)), ['culprit:b'])
        self.assertEqual(attribution.fired(self.answers(a=0.2, b=0.3, none=0.5)), [])
        self.assertEqual(attribution.fired({}), [])
        # a tie keeps the order the options were asked in
        self.assertEqual(attribution.fired(self.answers(b=0.4, a=0.4, none=0.2), None, self.body()), ['culprit:a'])

    def test_a_tie_breaks_by_merge_order_whatever_order_the_request_stores_its_criteria_in(self):
        # the stored request sorts its criteria (a, none, z); the run asked z first
        body = dict(state=dict(run='r', log='', tasks=[dict(id='z', files=[]), dict(id='a', files=[])]),
                    model='jev-latest', questions=dict(culprit=dict(criteria={'a': '', 'none': '', 'z': ''})))
        answers = self.answers(none=0.5, z=0.5, a=0.0)
        self.assertEqual(attribution.fired(answers, None, body), ['culprit:z'])
        self.assertEqual(attribution.ranking(attribution.probabilities({'answers': answers}, 'culprit'),
                                             ['z', 'a', 'none'])[0][0], 'z')

    def test_the_report_resolves_a_tie_from_the_stored_body_and_marks_it_unknown_without_one(self):
        from forge_decide import report
        body = dict(state=dict(run='r', log='', tasks=[dict(id='z', files=[]), dict(id='a', files=[])]),
                    model='jev-latest', questions=dict(culprit=dict(criteria={'a': '', 'none': '', 'z': ''})))
        entry = dict(ok=True, key='k', model='jev-latest', questions=['culprit'],
                     answers=self.answers(none=0.5, z=0.5, a=0.0))

        class Store:
            def __init__(self, found):
                self.found = found

            def body(self, key):
                return self.found

        record = sites.site('jev-attribution')
        self.assertEqual(report._fired(record, entry, {}, Store(body)), ['culprit:z'])
        self.assertIsNone(report._fired(record, entry, {}, Store(None)))
        clear = dict(entry, answers=self.answers(none=0.1, z=0.2, a=0.7))
        self.assertEqual(report._fired(record, clear, {}, Store(None)), ['culprit:a'])

    def test_the_baseline_counts_the_tasks_files_named_in_the_log(self):
        self.assertEqual(attribution.baseline(self.body(), 'culprit:a'), 1.0)
        self.assertEqual(attribution.baseline(self.body('a.py x.py'), 'culprit:a'), 2.0)
        self.assertEqual(attribution.baseline(self.body(), 'culprit:b'), 0.0)
        self.assertEqual(attribution.baseline(self.body(), 'culprit:zzz'), 0.0)
        self.assertEqual(attribution.baseline(None, 'culprit:a'), 0.0)

    def test_the_site_reads_them_through_the_registry(self):
        record = sites.site('jev-attribution')
        self.assertEqual(record.decisions(self.body()), ['culprit:a', 'culprit:b'])
        self.assertEqual(record.score(self.answers(a=0.2, b=0.7), 'culprit:a'), 0.2)


class LabelTests(LabelsCase):
    IDS = ['a', 'b', 'c']

    def body(self):
        return dict(state=dict(run='id-run1', log='boom', tasks=[dict(id=i, title=i, files=[], diff='') for i in self.IDS]),
                    model='jev-latest', questions=dict(culprit=questions.culprit(self.IDS)))

    def request(self, run='run1'):
        self.ask('jev-attribution', self.body(),
                 dict(culprit=dict(type='choice', choice='b', confidence=0.8,
                                   probabilities=dict(a=0.1, b=0.8, c=0.05, none=0.05))), run=run, ts=T0)

    def attribution(self, steps, *, run='run1', base_fails=False, inconclusive=None, ts=T0 + 60):
        doc = dict(culprit=None, base_fails=base_fails, inconclusive=inconclusive,
                   merges=[dict(id=i, commit='c' + i) for i in self.IDS],
                   steps=[dict(prefix=k, commit='x', status=status, seconds=1, source='search')
                          for k, status in steps.items()])
        ref = dataset.put_blob(json.dumps(doc), 'text')
        self.add('event', run=run, ts=ts, name='attribution', blobs=dict(attribution=ref))

    def only(self):
        [row] = self.collect('jev-attribution')
        return row

    def test_a_verified_pass_then_fail_labels_the_merge_true(self):
        self.request()
        self.attribution({0: 'PASS', 1: 'PASS', 2: 'FAIL', 3: 'FAIL'})
        row = self.only()
        self.assertEqual(row['labels'], {'culprit:a': False, 'culprit:b': True, 'culprit:c': False})
        self.assertEqual(row['live'], {'culprit:a': 0.1, 'culprit:b': 0.8, 'culprit:c': 0.05})
        self.assertEqual((row['site'], row['gate'], row['run']), ('jev-attribution', 'attribution', 'run1'))

    def test_two_passing_prefixes_label_false_and_two_failing_ones_too(self):
        self.request()
        self.attribution({0: 'PASS', 1: 'PASS', 2: 'FAIL', 3: 'FAIL'})
        labels = self.only()['labels']
        self.assertIs(labels['culprit:a'], False)      # prefixes 0 and 1 both PASS
        self.assertIs(labels['culprit:c'], False)      # prefixes 2 and 3 both FAIL

    def test_a_merge_with_an_unverified_neighbour_has_no_label(self):
        self.request()
        self.attribution({0: 'PASS', 2: 'FAIL', 3: 'FAIL'})      # prefix 1 was never verified
        self.assertEqual(self.only()['labels'], {'culprit:c': False})

    def test_a_step_that_was_not_a_verdict_is_not_evidence(self):
        self.request()
        self.attribution({0: 'PASS', 1: 'UNVERIFIED', 2: 'FAIL', 3: 'FAIL'})
        self.assertEqual(self.only()['labels'], {'culprit:c': False})

    def test_a_search_with_no_boundary_to_read_gives_no_label(self):
        self.request()
        self.attribution({0: 'FAIL'}, base_fails=True)
        self.assertEqual(self.collect('jev-attribution'), [])

    def test_an_inconclusive_search_gives_no_label(self):
        self.request()
        self.attribution({0: 'PASS', 1: 'PASS', 2: 'FAIL', 3: 'FAIL'}, inconclusive='prefix 2 could not be verified')
        self.assertEqual(self.collect('jev-attribution'), [])

    def test_no_event_or_another_runs_event_gives_no_label(self):
        self.request()
        self.assertEqual(self.collect('jev-attribution'), [])
        self.attribution({0: 'PASS', 1: 'PASS', 2: 'FAIL', 3: 'FAIL'}, run='run2')
        self.assertEqual(self.collect('jev-attribution'), [])

    def test_the_label_reads_verified_prefixes_whichever_order_the_search_took(self):
        self.request()
        # a guess of c probed prefix 2 first and then the search went below it
        self.attribution({0: 'PASS', 2: 'FAIL', 1: 'PASS', 3: 'FAIL'})
        self.assertEqual(self.only()['labels'], {'culprit:a': False, 'culprit:b': True, 'culprit:c': False})


if __name__ == '__main__':
    unittest.main()
