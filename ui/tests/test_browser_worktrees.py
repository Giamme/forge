"""The Worktrees screen (#125) in a real browser (Chromium through Python Playwright, skipped without it).

/api/worktrees is answered by the test (`page.route`), so no git runs: filters, the copy buttons, the link from a
run's page, and a refresh that keeps a selected command in place. Fixtures come from test_browser.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from forge_ui import reader
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule


def row(name, status, **extra):
    path = f'/home/me/my repos/.forge-worktrees/r1/{name}'
    out = dict(path=path, repo='/home/me/my repos/app', git_dir='/home/me/my repos/app', run=None, run_id='r1', run_path=None,
               task=name, branch=f'forge/r1/{name}', kind='task', status=status, merged=False, head=None, commit=None,
               created=None, removed=None, derived=False, locked=None, size=2048, note='', commands=[])
    out.update(extra)
    return out


def answer(rows, runs=()):
    by = {s: 0 for s in ('in_use', 'dirty', 'locked', 'needed', 'failed', 'finished', 'missing', 'foreign', 'unknown', 'removed')}
    for r in rows:
        by[r['status']] += 1
    return dict(rows=rows, runs=list(runs), repos=['/home/me/my repos/app'], notes=[], truncated=False,
                dataset=dict(enabled=True, since=None), computed=1790000000.0, sizes_pending=0,
                totals=dict(count=len(rows), on_disk=len(rows), bytes=0, by_status=by))


REMOVE = "git -C '/home/me/my repos/app' worktree remove '/home/me/my repos/.forge-worktrees/r1/done'"
ROWS = [
    row('done', 'finished', merged=True, commands=[dict(label='remove the worktree', command=REMOVE)]),
    row('bad', 'failed', commands=[dict(label='remove the worktree', command="git -C '/home/me/my repos/app' worktree remove '/home/me/my repos/.forge-worktrees/r1/bad'")],
        note='the branch stays'),
    row('busy', 'in_use', note='its run r1 is active'),
    row('old', 'removed', size=None, derived=True),
]


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Worktrees(unittest.TestCase):
    open, assertClean = tb.Page.open, tb.Page.assertClean

    def setUp(self):
        self.temp = temp = tempfile.TemporaryDirectory(prefix='forge-ui-worktrees-')
        self.addCleanup(temp.cleanup)
        self.plan = F.make_plan(Path(temp.name) / 'p')
        self.key = reader.run_key(self.plan)
        tb._serve(self, reader.Catalog(plans=[self.plan], registry=False))
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))
        self.body, self.asked = answer(ROWS), []

    def page(self, start='#/worktrees'):
        context = self.browser.new_context(viewport={'width': 1440, 'height': 960})
        self.addCleanup(context.close)
        context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=self.url.rstrip('/'))
        page = self.open(cdn=False, view='2d', context=context)

        def worktrees(route):
            self.asked.append(route.request.url)
            try:
                route.fulfill(status=200, content_type='application/json', body=json.dumps(self.body))
            except Exception:
                pass   # the page went away first
        page.route(re.compile(r'/api/worktrees(\?.*)?$'), worktrees)
        page.goto(self.url + start)
        return page

    def shown(self, page):
        return page.eval_on_selector_all('.wt-grid tbody tr', 'rows => rows.map(r => r.className.replace(/.*wt-row-/, ""))')

    def test_filters_narrow_the_rows_and_a_command_is_copied_whole(self):
        page = self.page()
        page.wait_for_selector('.wt-grid')
        self.assertEqual(self.shown(page), ['finished', 'failed', 'in_use', 'removed'])
        page.select_option('select[data-filter="status"]', 'failed')
        self.assertEqual(self.shown(page), ['failed'])
        page.select_option('select[data-filter="status"]', 'disk')
        self.assertEqual(self.shown(page), ['finished', 'failed', 'in_use'])
        self.assertEqual(page.locator('.wt-row-in_use .wt-copy').count(), 0)     # an active run's worktree offers nothing
        page.click('.wt-row-finished .wt-copy')
        page.wait_for_function('() => document.querySelector(".wt-row-finished .wt-copy").textContent === "Copied"')
        self.assertEqual(page.evaluate('navigator.clipboard.readText()'), REMOVE)
        self.assertNotIn('--force', page.inner_text('.wt-table'))
        self.assertEqual(self.asked, [self.url + 'api/worktrees'])               # one read, never on the page's poll
        self.assertClean(page)

    def test_a_runs_page_links_to_its_worktrees_and_the_screen_keeps_to_that_run(self):
        self.body = answer([dict(ROWS[0], run=self.key), dict(ROWS[1], run='000000000000')])
        page = self.page(f'#/runs/{self.key}')
        page.click('.worktrees-link')
        page.wait_for_selector('.wt-grid')
        self.assertEqual(page.evaluate('location.hash'), f'#/worktrees/runs/{self.key}')
        self.assertEqual(self.shown(page), ['finished'])
        self.assertTrue(page.is_visible('.wt-run-note'))
        self.assertClean(page)

    def test_a_refresh_waits_while_a_command_is_selected_then_shows_the_new_rows(self):
        page = self.page()
        page.wait_for_selector('.wt-grid')
        page.evaluate("""() => { const r = document.createRange(); r.selectNodeContents(document.querySelector('.wt-row-finished .wt-cmd code'));
            const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }""")
        self.assertEqual(page.evaluate('getSelection().toString()'), REMOVE)
        self.body = answer(ROWS + [row('new', 'failed')])
        page.evaluate('window.ForgeUI.state.worktreesView.reload()')
        page.wait_for_timeout(300)
        self.assertEqual(self.asked[1:], [self.url + 'api/worktrees?refresh=1'])
        self.assertEqual(page.evaluate('getSelection().toString()'), REMOVE)      # held: the selection is intact
        self.assertEqual(len(self.shown(page)), 4)
        page.evaluate('getSelection().removeAllRanges()')
        page.wait_for_function('() => document.querySelectorAll(".wt-grid tbody tr").length === 5')
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
