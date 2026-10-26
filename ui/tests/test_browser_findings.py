"""The Open findings view in a real browser (Chromium through Python Playwright, skipped without it).

The issues endpoints are answered by the test (`page.route`), so no `gh` and no CLI run: the drafts GET is held until
the test lets it go, which is the seconds `gh` takes on the real server. Fixtures come from test_browser.
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from forge_ui import reader
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule

ROWS = [
    {'fp': 'a' * 12, 'severity': 'P2', 'origin': 'review', 'plan': 'p', 'task': None, 'file': 'a.py', 'summary': 'row alpha', 'filed': None, 'dismissed': None},
    {'fp': 'b' * 12, 'severity': 'P0', 'origin': 'review', 'plan': 'p', 'task': None, 'file': 'b.py', 'summary': 'row bravo', 'filed': None, 'dismissed': None},
    {'fp': 'c' * 12, 'severity': 'P2', 'origin': 'review', 'plan': 'p', 'task': None, 'file': 'c.py', 'summary': 'row charlie', 'filed': None, 'dismissed': {'at': 't', 'reason': ''}},
    {'fp': 'd' * 12, 'severity': 'P2', 'origin': 'review', 'plan': 'p', 'task': None, 'file': 'd.py', 'summary': 'row delta', 'filed': None, 'dismissed': {'at': 't', 'reason': ''}},
]
NOTE = f"--only {'a' * 12}: no such open finding"


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Findings(unittest.TestCase):
    """A plan and a server per test; the findings view with the issues endpoints stubbed."""
    open, assertClean = tb.Page.open, tb.Page.assertClean

    def setUp(self):
        self.temp = temp = tempfile.TemporaryDirectory(prefix='forge-ui-findings-')       # _serve cleans it up on its skip paths
        self.addCleanup(temp.cleanup)
        self.plan = F.make_plan(Path(temp.name) / 'p')
        self.key = reader.run_key(self.plan)
        tb._serve(self, reader.Catalog(plans=[self.plan], registry=False))
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))
        self.drafts, self.posts, self.gets, self.released = [], [], [], []

    def view(self, findings=None):
        """The findings page, its issues endpoints stubbed: listings answer at once, the drafts GET (`only=`) is held."""
        page = self.open(cdn=False, view='2d')
        rows = ROWS if findings is None else findings

        def issues(route):
            req = route.request
            if req.method == 'POST':
                self.posts.append(json.loads(req.post_data))
                self.post_route = route
            elif 'only=' in req.url:
                self.gets.append(req.url)
                self.drafts.append(route)
            else:
                route.fulfill(status=200, content_type='application/json', body=json.dumps({'findings': rows, 'notes': [], 'drafts': []}))
        self.addCleanup(lambda: [r.fulfill(status=200, body='{}') for r in self.drafts if r not in self.released])
        page.route(re.compile(r'/api/runs/[^/]+/issues(\?.*)?$'), issues)
        page.goto(f'{self.url}#/runs/{self.key}/findings')
        page.wait_for_selector('.fnd-actions:not([hidden])')
        page.wait_for_selector('.fnd-table input[aria-label^="Select finding"]')
        return page

    def tick(self, page, summary):
        page.check(f'input[aria-label^="Select finding: {summary}"]')

    def release(self, route, body):
        self.released.append(route)
        route.fulfill(status=200, content_type='application/json', body=json.dumps(body))

    def draft(self, **extra):
        return {'title': '[P2] alpha', 'severity': 'P2', 'origin': 'review', 'fingerprints': ['a' * 12], 'body': 'the body', 'same_title': None, **extra}

    def test_the_preview_files_with_the_scope_and_the_toggle_of_its_drafts(self):
        page = self.view()
        page.click('.fnd-scope button:has-text("All runs")')
        page.wait_for_selector('.fnd-table input[aria-label^="Select finding"]')
        self.tick(page, 'row alpha')
        page.click('.fnd-actions button:has-text("File 1 issue")')
        page.wait_for_function('() => document.querySelector(".fnd-lamp").textContent.includes("preparing")')
        self.assertEqual(len(self.drafts), 1)
        self.assertIn('scope=repo', self.gets[0])
        page.click('.fnd-scope button:has-text("This run")')                    # while the drafts are made
        page.check('.fnd-toggle:has-text("Include plausible") input')
        self.release(self.drafts[0], {'drafts': [self.draft()], 'notes': []})
        page.click('.fnd-dialog button:has-text("File on GitHub")')
        while not self.posts:
            page.wait_for_timeout(20)
        self.assertEqual(self.posts[0]['scope'], 'repo')
        self.assertEqual(self.posts[0]['include_plausible'], False)
        self.assertEqual(self.posts[0]['fingerprints'], ['a' * 12])
        self.release(self.post_route, {'created': [], 'notes': [NOTE]})
        page.wait_for_selector('.fnd-results .fnd-result-note')
        self.assertIn('0 created', page.text_content('.fnd-results'))
        self.assertIn(NOTE, page.text_content('.fnd-results'))
        self.assertClean(page)

    def test_a_hidden_selection_is_not_counted_and_not_acted_on(self):
        page = self.view()
        undismiss = page.locator('.fnd-actions button:has-text("Undismiss")')
        page.check('.fnd-toggle:has-text("Show dismissed") input')
        page.wait_for_selector('input[aria-label^="Select finding: row charlie"]')
        self.tick(page, 'row charlie'); self.tick(page, 'row delta')
        self.assertEqual(undismiss.text_content(), 'Undismiss 2')
        self.assertTrue(undismiss.is_enabled())
        page.uncheck('.fnd-toggle:has-text("Show dismissed") input')
        page.wait_for_function('() => !document.querySelector(\'input[aria-label^="Select finding: row charlie"]\')')
        self.assertEqual(undismiss.text_content(), 'Undismiss')
        self.assertTrue(undismiss.is_disabled())
        self.assertNotIn('dismissed selected', page.text_content('.fnd-note'))

        self.tick(page, 'row alpha'); self.tick(page, 'row bravo')                 # a severity filter hides alpha
        self.assertTrue(page.locator('.fnd-actions button:has-text("File 2 issues")').is_enabled())
        page.select_option('.fnd-field:has-text("Severity") select', 'P0')
        file_key = page.locator('.fnd-actions button:has-text("File 1 issue")')
        self.assertTrue(file_key.is_enabled())
        file_key.click()
        while not self.gets:
            page.wait_for_timeout(20)
        self.assertTrue(self.gets[0].endswith('only=' + 'b' * 12), self.gets[0])
        page.select_option('.fnd-field:has-text("Severity") select', 'P3')           # nothing selected is shown
        self.assertEqual(page.locator('.fnd-actions button:has-text("File")').text_content(), 'File issues')
        self.assertTrue(page.locator('.fnd-actions button:has-text("File")').is_disabled())
        self.assertTrue(page.locator('.fnd-actions button:text-is("Dismiss")').is_disabled())
        self.assertClean(page)

    def test_select_shown_open_counts_only_the_rows_on_screen_toward_its_cap(self):
        """A full selection hidden by a filter must not stop "Select shown open" from ticking the rows still shown.

        25 is MAX_FPS. The first click fills it with P2 rows. Severity P0 hides those, so the second click has to
        tick P0 rows (and stop at 25 of them). File then counts and requests those, not the hidden P2 rows.
        """
        cap = 25

        def row(i, severity):
            base = 0 if severity == 'P2' else 0x100
            return {'fp': f'{base + i:012x}', 'severity': severity, 'origin': 'review', 'plan': 'p', 'task': None,
                    'file': 'a.py', 'summary': f'{severity} row {i}', 'filed': None, 'dismissed': None}

        page = self.view([row(i, 'P2') for i in range(cap)] + [row(i, 'P0') for i in range(cap + 1)])
        page.wait_for_function('() => document.querySelector(".fnd-lamp").textContent.includes("GitHub status checked")')
        page.click('.fnd-actions button:has-text("Select shown open")')
        self.assertEqual(page.locator('input[aria-label^="Select finding: P2 row"]:checked').count(), cap)
        self.assertEqual(page.locator('input[aria-label^="Select finding: P0 row"]:checked').count(), 0)

        page.select_option('.fnd-field:has-text("Severity") select', 'P0')
        page.wait_for_function('() => !document.querySelector(\'input[aria-label^="Select finding: P2 row"]\')')
        self.assertTrue(page.locator('.fnd-actions button:text-is("File issues")').is_disabled())
        page.click('.fnd-actions button:has-text("Select shown open")')
        self.assertEqual(page.locator('input[aria-label^="Select finding: P0 row"]:checked').count(), cap,
                         'hidden P2 rows filled the cap, so no P0 row was ticked')
        self.assertFalse(page.locator('input[aria-label="Select finding: P0 row 25"]').is_checked())
        file_key = page.locator('.fnd-actions button:has-text("File 25 issues")')
        self.assertEqual(file_key.text_content(), 'File 25 issues')
        self.assertTrue(file_key.is_enabled())
        self.assertTrue(page.locator('.fnd-actions button:text-is("Dismiss")').is_enabled())
        file_key.click()
        while not self.gets:
            page.wait_for_timeout(20)
        only = parse_qs(urlsplit(self.gets[0]).query)['only'][0].split(',')
        self.assertEqual(only, [f'{0x100 + i:012x}' for i in range(cap)])
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
