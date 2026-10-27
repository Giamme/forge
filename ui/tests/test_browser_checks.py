"""Where each task stands and the checks its agents ran (checksview.js) in a real browser (Chromium through Python
Playwright, skipped without it), on a live run the real server reads, and in an export."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest

from forge_ui import export, reader
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Checks(unittest.TestCase):
    open, assertClean = tb.Page.open, tb.Page.assertClean

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-checks-')
        cls.plan = F.make_live(Path(cls.temp.name), time.time(), os.getpid())       # this process stands in for the scheduler
        cls.key = reader.run_key(cls.plan)
        tb._serve(cls, reader.Catalog(plans=[cls.plan], registry=False))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def task(self, ident, wait):
        page = self.open(cdn=False, view='2d')
        page.goto(f'{self.url}#/runs/{self.key}/tasks/{ident}')
        page.wait_for_selector(wait)
        return page

    def test_the_table_says_what_each_task_runs_or_waits_on(self):
        page = self.open(cdn=False, view='2d')
        page.goto(f'{self.url}#/runs/{self.key}')
        page.wait_for_selector('.run-tasks .now-cell')
        now = {row.locator('td').first.text_content(): row.locator('.now-cell').text_content()
               for row in page.locator('.run-tasks tbody tr').all()}
        self.assertRegex(now['api'], r'^npm test\s*\dm\d\ds$')                 # the dwarf's call, running for minutes
        self.assertRegex(now['ui'], r'^bash tests/check\.sh\s*\d+m\d\ds\s*quiet \d+ min$')   # nine minutes and more
        self.assertEqual(now['docs'], 'waits on api')
        self.assertEqual(now['cli'], 'shares files with api')
        self.assertRegex(now['perf'], r'^paused: model luna-model on codex · rate limitresumes in 2\dm\d\ds$')
        self.assertEqual(now['db'], 'stopped by the retry policy after 3 attempts on luna')
        banner = page.text_content('#run-head .env-banner')
        self.assertIn('Docker ran out of network address pools in 2 tasks: schema, api', banner)
        self.assertClean(page)

    def test_each_attempt_lists_its_checks_with_exits_and_durations_and_the_criteria_it_skipped(self):
        page = self.task('api', '#inspector .checks')
        self.assertIn('runs npm test', page.text_content('#inspector .now-live'))
        first = page.locator('#inspector .attempt').first
        rows = [(r.locator('.tag').text_content(), r.locator('.check-cmd').text_content(), r.locator('.check-meta').text_content())
                for r in first.locator('.check').all()]
        self.assertEqual(rows, [('lint', 'npm run lint', 'exit 114s1 error · 1 warning'),
                                ('tests', 'npm test', 'exit 12m12s41 passed · 3 failed'),
                                ('tests', 'npm test', 'exit 12m05s41 passed · 3 failed')])   # the dwarf's two, then the reviewer's
        self.assertIn('Docker ran out of network address pools', first.text_content())
        self.assertEqual(first.locator('.checks-missing').text_content(), 'not run: npx tsc --noEmit')
        self.assertClean(page)

    def test_a_check_opens_the_conversation_at_its_command(self):
        page = self.task('api', '#inspector .checks')
        page.locator('#inspector .attempt').first.locator('.check-cmd', has_text='npm test').first.click()
        page.wait_for_function("() => document.activeElement && document.activeElement.id === 'ev-2'")
        self.assertIn('npm test', page.text_content('#ev-2'))
        self.assertClean(page)

    def test_a_quiet_reviewer_is_flagged_and_a_waiting_task_says_why(self):
        page = self.task('ui', '#inspector .now-live')
        self.assertTrue(page.is_visible('#inspector .now-live .now-quiet'))
        self.assertRegex(page.text_content('#inspector .now-live .now-quiet'), r'^quiet \d+ min$')
        page.goto(f'{self.url}#/runs/{self.key}/tasks/db')
        page.wait_for_selector('#inspector .now-split')
        self.assertIn('Split the migration from the backfill', page.locator('#inspector .now-split pre').text_content())
        self.assertClean(page)

    def test_an_export_with_its_texts_keeps_the_checks(self):
        name, body = export.build(reader.Catalog(plans=[self.plan], registry=False), self.key, texts=True)
        path = Path(self.temp.name) / name
        path.write_bytes(body)
        page = self.open(cdn=False, view='2d')
        page.goto(path.as_uri() + f'#/runs/{self.key}/tasks/api')
        page.wait_for_selector('#inspector .checks')
        self.assertEqual(page.locator('#inspector .attempt').first.locator('.check').count(), 3)
        self.assertIn('Docker ran out of network address pools', page.text_content('#run-head .env-banner'))
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
