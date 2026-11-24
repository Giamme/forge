"""`forge dataset reviewers`: how the work each reviewer PASSed held up, over a hand-built store."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402


class ReviewerReportTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name) / 'data'
        self.repo = Path(tmp.name) / 'repo'
        self.repo.mkdir()
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events, self.n = [], 0

    def landed(self, reviews, added=10, kept=10, reverted=False, horizon=14, status='ok'):
        """One attempt's reviewed commit, the reviews of that attempt, and what outcomes saw later."""
        self.n += 1
        run, task, commit = 'run%d' % self.n, 't', 'c%04d' % self.n
        self.events.append(dict(rid='a%d' % self.n, kind='attempt', run=run, mode='parallel', task=task, attempt=1,
                                ts=1000 + self.n, data=dict(status='MERGED', reviewed_commit=commit)))
        for i, (role, alias, verdict) in enumerate(reviews):
            self.events.append(dict(rid='d%d.%d' % (self.n, i), kind='dispatch', run=run, mode='parallel', task=task,
                                    attempt=1, ts=1000 + self.n + i,
                                    data=dict(role=role, alias=alias, model=alias + '-model', effort='high', verdict=verdict)))
        self.events.append(dict(rid='o%d' % self.n, kind='outcome', run=run, mode='parallel', task=task, ts=9000 + self.n,
                                data=dict(name='survival', status=status, reviewed=commit, added=added, surviving=kept,
                                          reverted_by=['r%d' % self.n] if reverted else [], horizon_days=horizon)))

    def forge(self, *args):
        r = subprocess.run([str(ROOT / 'forge'), 'dataset', 'reviewers', *args], capture_output=True, text=True,
                           env=dict(os.environ))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_each_reviewer_gets_the_share_of_its_passed_work_that_did_not_hold(self):
        PASS = lambda alias: ('qa', alias, 'PASS')
        for _ in range(6):
            self.landed([PASS('opus')])
        self.landed([PASS('opus')], kept=8)                       # 80% kept is the line: it held
        self.landed([PASS('opus'), PASS('opus')])                  # a QA-only resume passed it twice: one outcome
        self.landed([PASS('opus')], reverted=True)                 # every line kept, then reverted
        self.landed([PASS('opus')], added=100, kept=79)            # rewritten
        self.landed([PASS('sol'), ('qa_shadow', 'opus', 'PASS')])  # a shadow's PASS decided nothing
        self.landed([PASS('sol')], kept=2)
        self.landed([('qa', 'opus', 'FAIL')], reverted=True)       # the gate or a human let it through
        self.landed([])                                            # no review recorded
        self.landed([PASS('opus')], reverted=True, horizon=30)     # another horizon
        self.landed([PASS('opus')], status='lost')
        self.assertEqual(store.record(self.events, repo=self.repo), len(self.events))

        report = json.loads(self.forge('--json'))
        rows = {r['reviewer']: r for r in report['reviewers']}
        opus, sol = rows['opus'], rows['sol']
        self.assertEqual((opus['landed'], opus['reverted'], opus['rewritten'], opus['did_not_hold'], opus['enough']),
                         (10, 1, 1, 2, True))
        self.assertEqual(opus['specs'], ['opus-model:high'])
        self.assertAlmostEqual(opus['rate'], 0.2)
        # Wilson 95% for 2 of 10
        self.assertAlmostEqual(opus['ci'][0], 0.0567, places=4)
        self.assertAlmostEqual(opus['ci'][1], 0.5098, places=4)
        # Two outcomes are too few for a rate: the row says so instead of showing 50%.
        self.assertEqual((sol['landed'], sol['rewritten'], sol['enough'], sol['rate'], sol['ci'], sol['note']),
                         (2, 1, False, None, None, 'not enough data (2/10)'))
        self.assertEqual(set(rows), {'opus', 'sol'})
        self.assertEqual(report['unattributed'], dict(not_passed=1, no_review=1))
        self.assertEqual(report['skipped'], dict(lost=1, no_additions=0))
        self.assertEqual(report['other_horizons'], [30])

        at30 = json.loads(self.forge('--json', '--horizon-days', '30'))
        self.assertEqual([(r['reviewer'], r['landed'], r['reverted']) for r in at30['reviewers']], [('opus', 1, 1)])
        self.assertEqual(len(json.loads(self.forge('--json', '--repo', str(self.repo)))['reviewers']), 2)
        self.assertEqual(json.loads(self.forge('--json', '--repo', str(self.repo.parent)))['reviewers'], [])

        text = self.forge()
        [sol_line] = [line for line in text.splitlines() if line.split()[:1] == ['sol']]
        self.assertTrue(sol_line.endswith('not enough data (2/10)'), sol_line)
        self.assertNotIn('%', sol_line)
        [opus_line] = [line for line in text.splitlines() if line.split()[:1] == ['opus']]
        self.assertTrue(opus_line.endswith('20% [6%-51%] (2/10)'), opus_line)

    def test_an_alias_that_moved_to_a_newer_model_keeps_its_old_dispatches_apart(self):
        # terra was gpt-5.6-terra and is gpt-6.1-terra now (registry.tsv). A dispatch recorded as
        # terra on the old model is counted under terra-5.6, the alias that resolves to it today;
        # a model no alias resolves to any more is counted under its model id.
        from forge_dataset.names import dispatch_name
        for model, n in (('gpt-5.6-terra', 3), ('gpt-6.1-terra', 2), ('gpt-5.1-terra', 1)):
            for _ in range(n):
                self.landed([('qa', 'terra', 'PASS')])
                self.events[-2]['data'].update(model=model, harness='codex')
        self.assertEqual(store.record(self.events, repo=self.repo), len(self.events))
        rows = {r['reviewer']: r['landed'] for r in json.loads(self.forge('--json'))['reviewers']}
        self.assertEqual(rows, {'terra-5.6': 3, 'terra': 3})
        # No harness recorded: compared with the alias's default row.
        self.assertEqual(dispatch_name(dict(alias='terra', model='gpt-5.6-terra')), 'terra-5.6')
        self.assertEqual(dispatch_name(dict(alias='terra', model='gpt-6.1-terra')), 'terra')
        # sol and astra moved to 6.2 the same way (#180): the old models are sol-6.1 and astra-6.
        self.assertEqual(dispatch_name(dict(alias='sol', model='gpt-6.1-sol', harness='codex')), 'sol-6.1')
        self.assertEqual(dispatch_name(dict(alias='sol', model='github-copilot/gpt-6.1-sol', harness='opencode')), 'sol-6.1')
        self.assertEqual(dispatch_name(dict(alias='astra', model='gpt-6-astra', harness='openclaude')), 'astra-6')
        self.assertEqual(dispatch_name(dict(alias='sol', model='gpt-6.2-sol', harness='codex')), 'sol')
        # An alias forge does not know, or a dispatch with no model, keeps its name.
        self.assertEqual(dispatch_name(dict(alias='mine', model='x', harness='codex')), 'mine')
        self.assertEqual(dispatch_name(dict(alias='terra')), 'terra')
        self.assertEqual(dispatch_name(dict(model='gpt-x')), 'gpt-x')

    def test_no_store_is_an_empty_report_and_creates_none(self):
        report = json.loads(self.forge('--json'))
        self.assertEqual((report['reviewers'], report['horizon_days'], report['min_landed']), ([], 14, 10))
        self.assertFalse(self.home.exists())


if __name__ == '__main__':
    unittest.main()
