"""The Projects screen (#126) in a real browser (Chromium through Python Playwright, skipped without it).

The list and a project come from the server over the rich plan fixture; the Findings and Memories endpoints are
answered by the test (`page.route`), so no CLI runs. Tabs, filters, links to a run and a diff, text that looks like
HTML staying text, and a Read again that keeps the filters and the scroll position. Fixtures come from test_browser.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from forge_ui import projects as P, reader
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule
PID = P.project_id('/example/fixture-repo')


def finding(n, **extra):
    row = dict(source='run', run=None, run_id='20261016-090000-fx01', task='reader', final_review=None, attempt=1, kind='primary',
               reviewer='opus', verdict='FAIL', at=1760605000.0 + n, id='qa-x:%d' % n, severity='P2', confidence='CONFIRMED',
               location='ui/f%d.py:1' % n, summary='- [P2][CONFIRMED] ui/f%d.py:1 — finding number %d' % (n, n),
               text='finding number %d' % n, fp='%012x' % n, superseded=False, href=None, count=1, occurrences=[],
               status='open', fileable=False, origin=None)
    row.update(extra)
    return row


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Projects(unittest.TestCase):
    open, assertClean = tb.Page.open, tb.Page.assertClean

    def setUp(self):
        self.temp = temp = tempfile.TemporaryDirectory(prefix='forge-ui-projects-')
        self.addCleanup(temp.cleanup)
        self.plan = F.make_plan(Path(temp.name) / 'p')
        self.key = reader.run_key(self.plan)
        tb._serve(self, reader.Catalog(plans=[self.plan], registry=False))
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))
        href = '#/runs/%s/tasks/reader/diff/finding/qa-x:0' % self.key
        self.findings = dict(id=PID, notes=[], runs=1, dataset_runs=1, computed=1.0, available=True, rows=[
            finding(0, run=self.key, href=href, severity='P0', summary='- [P0][CONFIRMED] ui/x.py:1 — <b>bold</b> <img src=x onerror="window.__xss=1">'),
            finding(1, status='superseded'), finding(2, kind='shadow', reviewer='sol:xhigh'),
            finding(3, source='dataset', run_id='gone-run')] + [finding(n) for n in range(4, 60)])
        self.memories = dict(id=PID, notes=[], differs=False, runs=1, memory_off_runs=0, computed=1.0, sources=[
            dict(path='/example/fixture-repo', root=True, branch=None, ok=True, ledger=True, rebuilt_differs=False, max_lines=40,
                 memory=[dict(category='trap', title='Known traps', roles=['planner', 'dwarf', 'qa'], lines=[dict(text='<script>x</script> stays text', count=None)])])],
            learnings=[dict(category='trap', key='k', text='<script>x</script> stays text', count=1, states={'/example/fixture-repo': 'in_memory'},
                            rows=[dict(ts='2026-11-01T00:00:00Z', run_id='r1', task='t', role='dwarf', model='m', text='t', **{'from': ['/example/fixture-repo']})],
                            last='2026-11-01T00:00:00Z', dataset_only=False),
                       dict(category='finding', key='w', text='a finding seen once', count=1, states={'/example/fixture-repo': 'waiting'},
                            rows=[], last='2026-10-01T00:00:00Z', dataset_only=False)])
        self.asked = []

    def page(self, start):
        page = self.open(cdn=False, view='2d')

        def stub(route):
            url = route.request.url
            self.asked.append(url)
            body = self.findings if '/findings' in url else self.memories
            try:
                route.fulfill(status=200, content_type='application/json', body=json.dumps(body))
            except Exception:
                pass
        page.route(re.compile(r'/api/projects/[0-9a-f]{8}/(findings|memories)(\?.*)?$'), stub)
        page.goto(self.url + start)
        return page

    def test_the_list_links_to_a_project_whose_runs_open_the_run(self):
        page = self.page('#/projects')
        page.wait_for_selector('.prj-body table')
        page.click(f'a[href="#/projects/{PID}"]')
        page.wait_for_selector('.prj-table table')
        self.assertEqual(page.text_content('.prj-head h1'), 'fixture-repo')
        page.click('.prj-table a[href="#/runs/%s"]' % self.key)
        page.wait_for_function('() => location.hash.startsWith("#/runs/")')
        self.assertClean(page)

    def test_a_runs_header_and_its_map_district_link_to_its_project(self):
        page = self.page(f'#/runs/{self.key}')
        page.wait_for_selector('#run-head .project-link')
        self.assertEqual(page.get_attribute('#run-head .project-link', 'href'), f'#/projects/{PID}')
        page.goto(self.url + '#/')
        page.wait_for_selector('.map-repo h3 .project-link')
        self.assertEqual(page.get_attribute('.map-repo h3 .project-link', 'href'), f'#/projects/{PID}')
        self.assertClean(page)

    def test_findings_filter_link_to_their_diff_and_keep_html_as_text(self):
        page = self.page(f'#/projects/{PID}/findings')
        page.wait_for_selector('.prj-findings')
        count = lambda: page.inner_text('.prj-count')
        self.assertTrue(count().startswith('58 of 60'), count())                 # open, no shadow
        page.check('input[data-filter="shadow"]')
        self.assertTrue(count().startswith('59 of 60'), count())
        page.select_option('select[data-filter="status"]', 'superseded')
        self.assertTrue(count().startswith('1 of 60'))
        page.select_option('select[data-filter="status"]', 'open')
        page.select_option('select[data-filter="severity"]', 'P0')
        self.assertTrue(count().startswith('1 of 60'))
        self.assertIn('<b>bold</b>', page.inner_text('.prj-findings tbody'))       # shown, not parsed
        self.assertEqual(page.locator('.prj-findings tbody img, .prj-findings tbody b').count(), 0)
        self.assertIsNone(page.evaluate('window.__xss'))
        page.click('.prj-findings a:has-text("diff")')
        page.wait_for_function('() => location.hash.includes("/diff/finding/")')
        self.assertClean(page)

    def test_read_again_keeps_the_filters_and_the_scroll_position(self):
        page = self.page(f'#/projects/{PID}/findings')
        page.wait_for_selector('.prj-findings')
        page.select_option('select[data-filter="reviewer"]', 'opus')
        page.wait_for_function('() => document.querySelector(".prj-count").textContent.startsWith("58 of")')
        # whichever element scrolls the page in this shell
        scroller = """(() => { const m = document.getElementById('main'); return m.scrollHeight > m.clientHeight + 10 ? m : document.scrollingElement; })()"""
        page.evaluate(f'{scroller}.scrollTop = 600')
        before = page.evaluate(f'{scroller}.scrollTop')
        self.assertGreater(before, 0)
        # clicked from script: Playwright's click would first scroll the button, at the top, into view
        page.evaluate("[...document.querySelectorAll('.prj-filters button')].find(b => b.textContent === 'Read again').click()")
        page.wait_for_timeout(300)
        self.assertTrue(self.asked[-1].endswith('/findings?refresh=1'))
        self.assertEqual(page.input_value('select[data-filter="reviewer"]'), 'opus')
        self.assertTrue(page.inner_text('.prj-count').startswith('58 of'))
        self.assertEqual(page.evaluate(f'{scroller}.scrollTop'), before)
        self.assertClean(page)

    def test_memories_show_what_models_are_given_and_every_learning_with_its_state(self):
        page = self.page(f'#/projects/{PID}/memories')
        page.wait_for_selector('.prj-learnings table')
        self.assertIn('<script>x</script> stays text', page.inner_text('.prj-given'))
        self.assertIn('planner, dwarf, qa', page.inner_text('.prj-given'))
        self.assertIn('waiting for a second run', page.inner_text('.prj-learnings'))
        page.select_option('select[data-filter="m-state"]', 'waiting')
        self.assertTrue(page.inner_text('.prj-learnings .prj-count').startswith('1 of 2'))
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
