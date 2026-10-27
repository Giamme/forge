"""The verification inspector (verifyview.js) in a real browser (Chromium through Python Playwright, skipped
without it), on the run fixture's lanes verifications served by the real server, and in an export."""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from forge_ui import export, reader
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Verification(unittest.TestCase):
    """A plan and a server per test, with a lanes verification running or finished."""
    open, assertClean = tb.Page.open, tb.Page.assertClean

    def serve(self, running):
        self.temp = temp = tempfile.TemporaryDirectory(prefix='forge-ui-verify-')       # _serve cleans it up on its skip paths
        self.addCleanup(temp.cleanup)
        self.plan = F.lanes_verification(F.make_plan(Path(temp.name) / 'p'), running=running)
        # e2e's log names its failing test on line 12, where the reader's failing entry points
        F.write(self.plan / 'verification.lane.e2e.log',
                ''.join(f'e2e line {n}\n' if n != 12 else 'FAIL checkout.Pay.test_card\n' for n in range(1, 41)))
        self.key = reader.run_key(self.plan)
        tb._serve(self, reader.Catalog(plans=[self.plan], registry=False))
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))

    def inspector(self, url=None):
        page = self.open(cdn=False, view='2d')
        page.goto(url or f'{self.url}#/runs/{self.key}/verification')
        page.wait_for_selector('#inspector .vfy-lanes')
        return page

    def test_a_running_lanes_verification_shows_its_step_last_line_and_elapsed_time(self):
        self.serve(running=True)
        page = self.inspector()
        live = page.text_content('#inspector .vfy-live')
        self.assertRegex(live, r'running for \d+h\d\dm')                 # since T0+1300, long ago
        self.assertIn('step browser', live)
        self.assertEqual(page.text_content('#inspector .vfy-live .vfy-last'), 'checkout: 12 of 33')
        self.assertRegex(page.text_content('#inspector .vfy-live .vfy-idle'), r'^no output for \d+ min$')
        self.assertEqual(page.locator('#inspector .vfy-table tbody tr').count(), 3)
        self.assertIn('Live output: e2e', page.text_content('#inspector .vfy-tail h3'))   # the lane that printed last
        page.wait_for_selector('#inspector .vfy-tail .vfy-log')
        self.assertIn('FAIL checkout.Pay.test_card', page.text_content('#inspector .vfy-tail .vfy-log'))
        page.click('#inspector .vfy-lane button:has-text("lint")')        # follow another lane
        page.wait_for_function("() => document.querySelector('#inspector .vfy-tail h3').textContent.includes('lint')")
        page.wait_for_function("() => document.querySelector('#inspector .vfy-tail .vfy-log')?.textContent.includes('lint output')")
        self.assertClean(page)

    def test_a_failed_verification_lists_its_failing_tests_first(self):
        self.serve(running=False)
        page = self.inspector()
        first = page.evaluate("document.querySelector('#inspector section').className")
        self.assertIn('vfy-failing', first)
        tests = page.locator('#inspector .vfy-test')
        self.assertEqual(tests.count(), 2)
        self.assertEqual([tests.nth(i).locator('.vfy-badge').text_content() for i in range(2)], ['known', 'new'])
        self.assertIn('expected 200, got 500', tests.nth(1).text_content())
        self.assertFalse(page.locator('#inspector .vfy-test details').first.evaluate('d => d.open'))   # excerpts start closed
        self.assertIn('rerun 1', page.text_content('#inspector .vfy-table'))
        self.assertClean(page)

    def test_open_in_log_shows_the_line_highlighted(self):
        self.serve(running=False)
        page = self.inspector()
        page.click('#inspector .vfy-test button:has-text("open in log (line 12)")')
        page.wait_for_selector('#inspector .vfy-opened .vfy-hl')
        self.assertEqual(page.text_content('#inspector .vfy-opened .vfy-hl .vfy-ln'), '12')
        self.assertEqual(page.text_content('#inspector .vfy-opened .vfy-hl .vfy-lt'), 'FAIL checkout.Pay.test_card')
        self.assertEqual(page.text_content('#inspector .vfy-opened .vfy-line .vfy-ln'), '2')      # ten lines above it
        self.assertClean(page)

    def test_an_export_shows_the_failing_tests_and_falls_back_to_the_tail(self):
        self.serve(running=False)
        name, body = export.build(reader.Catalog(plans=[self.plan], registry=False), self.key, texts=True)
        path = Path(self.temp.name) / name
        path.write_bytes(body)
        page = self.inspector(path.as_uri() + f'#/runs/{self.key}/verification')
        self.assertEqual(page.locator('#inspector .vfy-test').count(), 2)
        page.click('#inspector .vfy-test button:has-text("open in log (line 12)")')
        page.wait_for_selector('#inspector .vfy-opened .vfy-hl')            # no line ranges in an export: the tail
        self.assertEqual(page.text_content('#inspector .vfy-opened .vfy-hl .vfy-lt'), 'FAIL checkout.Pay.test_card')
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
