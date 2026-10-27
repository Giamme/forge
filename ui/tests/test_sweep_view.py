"""The finding sweep (`run --sweep`) in forge-ui: a fix task's findings with what its review said of each, the
findings it took from their task, and the run's account (RunDetail.sweep); read, and in a real browser
(Chromium through Python Playwright, skipped without it)."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from forge_ui import reader
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule


def finding(ident, severity, line, text):
    return dict(id=ident, key='k' + ident, severity=severity, confidence='CONFIRMED', origin='tolerated', task='reader',
                file='ui/reader.py', line=line, path='ui/reader.py', location='ui/reader.py:%d' % line, text=text,
                raw='[%s][CONFIRMED] ui/reader.py:%d — %s' % (severity, line, text))


def add_sweep(plan: Path, merged=True):
    """A sweep that gave one fix task two of reader's findings and left one of page's for forge issues."""
    F.write(plan / 'tasks.tsv', (plan / 'tasks.tsv').read_text()
            + 'sweep-1\t-\tlow\tui/reader.py\tluna\topus\tFix 2 review findings in ui/reader.py\n')
    root = plan / 'tasks/sweep-1'
    F.write(root / 'sweep.json', dict(file='ui/reader.py', findings=[
        finding('S1', 'P2', 12, 'Status is read twice.\nOnce per poll.'), finding('S2', 'P3', 40, 'A stale comment.')]))
    F.write(root / 'status', 'PASS\n')
    if merged:
        F.write(root / 'merged', '')
    F.write(root / 'sweep.outcome.json', dict(at=F.T0 + 900, findings=[
        dict(id='S1', key='kS1', task='reader', status='fixed', note=''),
        dict(id='S2', key='kS2', task='reader', status='not-fixed', note='the comment still says v1')]))
    F.write(plan / 'sweep/plan.json', dict(version=1, at=F.T0 + 800, severity='P3', plausible=False, max=5,
                                           integration='b' * 40, estimate=dict(dispatches=2), tasks=[],
                                           dropped=[dict(task='page', severity='P3', confidence='CONFIRMED',
                                                         origin='tolerated', file='ui/page.html', line=3,
                                                         text='Old markup.', key='k3', reason='changed',
                                                         detail=dict(merged_since=['graph']))]))


class Reading(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.plan = F.make_plan(Path(temp.name))

    def tasks(self):
        data = reader.detail(self.plan)
        return data, {task['id']: task for task in data['tasks']}

    def test_a_fix_task_its_source_task_and_the_run_say_what_the_sweep_did(self):
        add_sweep(self.plan)
        data, tasks = self.tasks()
        self.assertEqual(tasks['sweep-1']['sweep'], dict(file='ui/reader.py', findings=[
            dict(id='S1', severity='P2', confidence='CONFIRMED', task='reader', location='ui/reader.py:12',
                 summary='Status is read twice.', status='fixed', note=None),
            dict(id='S2', severity='P3', confidence='CONFIRMED', task='reader', location='ui/reader.py:40',
                 summary='A stale comment.', status='not-fixed', note='the comment still says v1')]))
        self.assertEqual([(f['id'], f['by'], f['status']) for f in tasks['reader']['swept']],
                         [('S1', 'sweep-1', 'fixed'), ('S2', 'sweep-1', 'not-fixed')])
        self.assertEqual((tasks['page']['sweep'], tasks['page']['swept']), (None, []))
        self.assertEqual(data['sweep'], dict(at=F.T0 + 800, severity='P3', plausible=False, tasks=['sweep-1'], findings=2,
                                             fixed=1, dropped=[dict(task='page', severity='P3', location='ui/page.html:3',
                                                                    summary='Old markup.', reason='changed',
                                                                    merged_since=['graph'])]))

    def test_until_its_fix_task_merges_a_finding_has_no_outcome(self):
        add_sweep(self.plan, merged=False)
        data, tasks = self.tasks()
        self.assertEqual([f['status'] for f in tasks['sweep-1']['sweep']['findings']], [None, None])
        self.assertEqual(data['sweep']['fixed'], 0)

    def test_a_run_never_swept_has_no_sweep(self):
        data, tasks = self.tasks()
        self.assertIsNone(data['sweep'])
        self.assertTrue(all(task['sweep'] is None and task['swept'] == [] for task in tasks.values()))


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Page(unittest.TestCase):
    open, assertClean = tb.Page.open, tb.Page.assertClean

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-sweep-')
        cls.plan = F.make_plan(Path(cls.temp.name))
        add_sweep(cls.plan)
        cls.key = reader.run_key(cls.plan)
        tb._serve(cls, reader.Catalog(plans=[cls.plan], registry=False))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def test_the_inspector_shows_each_swept_finding_where_it_came_from_and_the_runs_account(self):
        page = self.open(cdn=False, view='2d')
        page.goto(f'{self.url}#/runs/{self.key}/tasks/sweep-1')
        page.wait_for_selector('#inspector .sweep-item')
        items = page.locator('#inspector .sweep-item')
        self.assertEqual(items.count(), 2)
        self.assertEqual(items.nth(0).locator('.lamp').get_attribute('class'), 'lamp st-pass')
        self.assertEqual(items.nth(1).locator('.lamp').get_attribute('class'), 'lamp st-fail')
        self.assertIn('S2 · not fixed · from reader — the comment still says v1', items.nth(1).text_content())
        items.nth(0).locator('.sweep-meta a').click()                       # to the task the finding came from
        page.wait_for_selector('#inspector h2:text-matches("^reader ·")')
        self.assertIn('Taken by the finding sweep', page.text_content('#inspector'))
        self.assertIn('S1 · fixed · by sweep-1', page.locator('#inspector .sweep-item').first.text_content())
        page.goto(f'{self.url}#/runs/{self.key}')
        page.wait_for_selector('#inspector dt:text-is("Finding sweep")')
        self.assertIn('1 fix task · 1 of 2 findings fixed · 2 left for forge issues (P3 and worse)', page.text_content('#inspector'))
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
