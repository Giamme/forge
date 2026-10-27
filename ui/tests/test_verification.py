"""RunDetail.verification while a verification runs and once it ran in lanes (#33): its state,
mode, live step and output, each lane with its runs and tests, and every failing test."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from forge_ui import export, reader
from forge_ui.reader import detail
import run_fixture as F


T0 = F.T0


class VerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.plan = F.make_plan(Path(temp.name))

    def verification(self):
        data = detail(self.plan)
        return data['verification'], data['warnings']

    def assertIdle(self, value, since):
        self.assertAlmostEqual(value, time.time() - since, delta=5)

    def test_a_finished_lanes_verification(self):
        F.lanes_verification(self.plan)
        v, warnings = self.verification()
        self.assertEqual(warnings, [])
        self.assertEqual((v['state'], v['mode'], v['status'], v['live']), ('done', 'lanes', 'FAIL', None))
        self.assertEqual([(lane['name'], lane['state'], lane['exit'], len(lane['runs'])) for lane in v['lanes']],
                         [('unit', 'flaky', 0, 2), ('e2e', 'fail', 1, 2), ('lint', 'pass', 0, 1)])
        unit, e2e, lint = v['lanes']
        self.assertEqual((unit['duration_s'], unit['idle_s'], e2e['group']), (50.0, None, 'browser'))
        self.assertEqual([run['log'] for run in unit['runs']],
                         ['verification.lane.unit.run-1.log', 'verification.lane.unit.run-2.log'])
        self.assertEqual({k: e2e['tests'][k] for k in ('pass', 'fail', 'skip', 'error')},
                         {'pass': 30, 'fail': 2, 'skip': 1, 'error': 0})
        self.assertIsNone(lint['tests'])
        self.assertEqual([(f['lane'], f['name'], f['known'], f['log_line']) for f in v['failing']],
                         [('e2e', 'checkout.Pay.test_card', True, 12), ('e2e', 'checkout.Pay.test_refund', False, None)])
        self.assertEqual(v['failing'][1]['message'], 'expected 200, got 500')
        self.assertFalse(v['mutated'])
        self.assertEqual(v['runs'][0], {'n': 1, 'kind': 'initial', 'exit': 1, 'tree_changed': False, 'duration_s': 30.0,
                                        'log': 'verification.lane.e2e.run-1.log', 'lane': 'e2e', 'started': T0 + 1300})
        self.assertEqual(len(v['runs']), 5)
        with open(self.plan / 'verification.runs.tsv', 'a', encoding='utf-8') as stream:
            stream.write('6\tinitial\t0\t0\t1\tlog\tunit\n')        # 7 columns: neither form
        v, warnings = self.verification()
        self.assertEqual(len(v['runs']), 5)
        self.assertEqual(warnings, ['verification.runs.tsv line 6: invalid verification run'])
        doc = json.loads((self.plan / 'verification.lanes.json').read_text())
        F.write(self.plan / 'verification.lanes.json', dict(doc, mutated=True), T0 + 1400)
        self.assertTrue(self.verification()[0]['mutated'])           # a lane changed the checkout

    def test_a_running_single_command_shows_its_open_step_and_last_line(self):
        F.single_running(self.plan)
        self.assertEqual((self.plan / 'verification.status').read_text(), 'PASS\n')    # the last verification's
        v, _ = self.verification()
        self.assertEqual((v['state'], v['status'], v['mode'], v['lanes'], v['failing']),
                         ('running', 'RUNNING', 'single', None, []))
        live = v['live']
        # build ended, unit opened and ended inside test: test is the open one
        self.assertEqual(live['step'], {'name': 'test', 'started': None})
        self.assertEqual((live['started'], live['last_line'], live['last_output_at']),
                         (T0 + 1300, 'test_reader ok', T0 + 1340))
        self.assertIdle(live['idle_s'], T0 + 1340)

    def test_a_rerun_is_read_from_the_newer_retry_log(self):
        F.single_running(self.plan, retry=True)
        live = self.verification()[0]['live']
        self.assertEqual((live['step'], live['last_line'], live['last_output_at']),
                         ({'name': 'rerun', 'started': None}, 'rerunning the failed lane', T0 + 1350))

    def test_running_lanes_speak_through_the_lane_that_printed_last(self):
        F.lanes_verification(self.plan, running=True)
        v, warnings = self.verification()
        self.assertEqual(warnings, [])
        self.assertEqual((v['state'], v['status'], v['mode']), ('running', 'RUNNING', 'lanes'))
        self.assertEqual((v['live']['step'], v['live']['last_line'], v['live']['last_output_at']),
                         ({'name': 'browser', 'started': T0 + 1340}, 'checkout: 12 of 33', T0 + 1355))
        self.assertIdle(v['live']['idle_s'], T0 + 1355)
        unit, e2e, lint = v['lanes']
        self.assertIdle(e2e['idle_s'], T0 + 1355)
        self.assertIdle(e2e['duration_s'], T0 + 1300)
        self.assertIdle(lint['idle_s'], T0 + 1330)
        self.assertEqual((unit['state'], unit['duration_s'], unit['idle_s']), ('pass', 20.0, None))

    def test_a_lanes_file_from_an_earlier_verification_is_not_read(self):
        F.lanes_verification(self.plan)
        F.execution(self.plan, 'verification-' + F.uid(5), 1500.0, phase='verification', parent=F.PARALLEL)
        v, _ = self.verification()
        self.assertEqual((v['state'], v['mode'], v['lanes'], v['failing']), ('running', 'single', None, []))

    def test_an_unreadable_lanes_file_is_warned_and_never_fails_the_request(self):
        F.lanes_verification(self.plan, running=True)
        at = T0 + 1360
        cases = {'not JSON': ('{"lanes": [', 'not a JSON object'),
                 'no lanes list': ('{"lanes": {}}', 'no lanes list'),
                 'over 4 MB': ('{"lanes": [], "pad": "' + 'x' * (4 << 20) + '"}', 'larger than 4 MB, not read')}
        for case, (text, warning) in cases.items():
            with self.subTest(case):
                F.write(self.plan / 'verification.lanes.json', text, at)
                v, warnings = self.verification()
                self.assertEqual((v['mode'], v['lanes'], v['failing'], v['state']), ('lanes', None, [], 'running'))
                self.assertEqual(v['live']['step'], None)
                self.assertEqual(warnings, [f'verification.lanes.json: {warning}'])


class VerificationExportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='forge-ui-export-')
        self.addCleanup(temp.cleanup)
        self.plan = F.lanes_verification(F.make_plan(Path(temp.name)))
        self.catalog = reader.Catalog(plans=[self.plan], registry=False)
        self.key = reader.run_key(self.plan)

    def verification(self, **kw):
        _, body = export.build(self.catalog, self.key, **kw)
        html = body.decode('utf-8')
        start = html.index('<script type="application/json" id="forge-snapshot">')
        data = json.loads(html[html.index('>', start) + 1:html.index('</script>', start)])
        return data['api'][f'/api/runs/{self.key}']['verification']

    def test_texts_off_nulls_the_new_texts_and_lane_commands_lose_local_paths(self):
        v = self.verification()
        e2e = v['lanes'][1]
        self.assertEqual((e2e['last_line'], v['failing'][1]['message'], v['failing'][1]['excerpt']), (None, None, None))
        self.assertEqual(v['failing'][1]['name'], 'checkout.Pay.test_refund')
        self.assertEqual(e2e['command'], 'plan-rich/bin/e2e --out plan-rich/out')
        v = self.verification(texts=True, paths=True)
        self.assertEqual((v['lanes'][1]['last_line'], v['failing'][1]['message']), ('2 failed, 30 passed', 'expected 200, got 500'))
        self.assertEqual(v['lanes'][1]['command'], f'{self.plan}/bin/e2e --out {self.plan}/out')


if __name__ == '__main__':
    unittest.main()
