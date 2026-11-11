"""The task summaries in a real browser (Chromium through Python Playwright, skipped without it).

The run's switch above the tasks and its summary column, a task's summary in the inspector (its text, its model and
time, what it came from, out of date, its error) and the task's own switch. The control is answered by the test
(`page.route`): a POST is recorded and taken as saved, so no `forge control` runs. The summary files are written here,
as `forge summarize` leaves them. Fixtures come from test_browser.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

from forge_ui import reader
from forge_ui.server import make_server
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule

CONTROLS = re.compile(r'/api/runs/[^/]+/controls$')
TEXT = 'Reads <b>x</b> run dirs.\nIt keeps the rest as text.'
SENTENCE = 'Reads <b>x</b> run dirs into plain dicts.'


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Summaries(unittest.TestCase):
    """A plan and a server per test: reader has a stale summary, graph a fresh one with the error of a later try."""
    open, assertClean = tb.Page.open, tb.Page.assertClean

    def setUp(self):
        self.temp = temp = tempfile.TemporaryDirectory(prefix='forge-ui-summaries-')       # _serve cleans it up on its skip paths
        self.addCleanup(temp.cleanup)
        self.plan = F.make_plan(Path(temp.name) / 'p')
        self.key = reader.run_key(self.plan)
        now = int(time.time())
        # make_plan pins every file's mtime to the past. reader's status changed after its summary was made: out of date.
        # graph's summary is newer than its files.
        F.write(self.plan / 'tasks/reader/summary.json', {'text': TEXT, 'sentence': SENTENCE, 'model': 'sol:low', 'generated_at': now - 600,
                                                          'sources': [{'kind': 'attempt', 'ref': '2'},
                                                                      {'kind': 'finding', 'ref': '1:Missing status file'}]})
        os.utime(self.plan / 'tasks/reader/status', (now, now))
        F.write(self.plan / 'tasks/graph/summary.json', {'text': 'Draws the graph.', 'sentence': 'Draws the graph.', 'model': 'luna',
                                                         'generated_at': now + 60, 'sources': []})
        F.write(self.plan / 'tasks/graph/summary.error', 'quota exhausted\n')
        tb._serve(self, reader.Catalog(plans=[self.plan], registry=False))
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))
        self.posts, self.ticks = [], 0

    def control(self, route):
        """The write route, answered by the test: a POST is recorded and taken as saved; a GET goes on to the server."""
        if route.request.method != 'POST':
            return route.continue_()
        body = json.loads(route.request.post_data)
        self.posts.append(body)
        route.fulfill(status=200, content_type='application/json',
                      body=json.dumps({'ok': True, 'changed': {'key': 'summaries', 'previous': 'before', 'value': body['value']},
                                       'applies': 'at its next change'}))

    def set_switch(self, value, model=None):
        """The run's switch as `forge control set summaries` leaves it in the plan dir, its mtime moved so the page reads it."""
        self.ticks += 1
        path = self.plan / 'summaries'
        F.write(path, value + '\n')
        stamp = time.time() + self.ticks
        os.utime(path, (stamp, stamp))
        if model:
            F.write(self.plan / 'summaries_model', model + '\n')

    def poll(self):
        """A change the page's next poll reads (tasks.tsv is newer), so it rebuilds the inspector."""
        self.ticks += 1
        stamp = time.time() + self.ticks
        os.utime(self.plan / 'tasks.tsv', (stamp, stamp))

    def mark(self, page):
        page.evaluate("document.querySelector('#inspector > :first-child').dataset.mark = '1'")

    def rebuilt(self, page):
        """Wait until the inspector was built again: the marker put on its first node is gone with that node."""
        page.wait_for_function("() => !document.querySelector('#inspector > :first-child').dataset.mark", timeout=20000)

    def test_the_run_switch_turns_the_summary_column_on_and_off_through_the_control(self):
        page = self.open(cdn=False, view='2d')
        page.route(CONTROLS, self.control)
        page.goto(self.url + '#/runs/' + self.key)
        page.wait_for_selector('#run-summaries .sum-bank')
        self.assertEqual(page.get_attribute('#run-summaries button[data-value=off]', 'aria-pressed'), 'true')
        self.assertEqual(page.locator('th[data-col=summary]').count(), 0, 'off: no column')
        page.click('#run-summaries button[data-value=on]')
        self.assertEqual(page.text_content('#run-summaries .ui-confirm .ui-confirm-text'),
                         "Set Summaries to on? Takes effect at each task's next change")
        page.click('#run-summaries .ui-confirm button:text-is("Confirm")')
        page.wait_for_selector('#run-summaries .ui-outcome.ok')
        self.assertEqual(self.posts, [{'action': 'set', 'key': 'summaries', 'value': 'on'}])
        self.set_switch('on', 'sol:low')                                                      # what the CLI would leave behind
        page.wait_for_selector('th[data-col=summary]', timeout=20000)
        self.assertEqual(page.get_attribute('#run-summaries button[data-value=on]', 'aria-pressed'), 'true')
        self.assertEqual(page.text_content('#run-summaries .sum-model'), 'sol:low')
        cells = page.eval_on_selector_all('td[data-col=summary]', 'cells => cells.map(c => c.textContent)')
        self.assertIn(SENTENCE, cells, 'the sentence is plain text: its <b> is not an element')
        self.assertEqual(page.locator('td[data-col=summary] b').count(), 0)
        page.click('#run-summaries button[data-value=off]')
        page.click('#run-summaries .ui-confirm button:text-is("Confirm")')
        while len(self.posts) < 2:                                                            # the route answers during the wait
            page.wait_for_timeout(20)
        self.assertEqual(self.posts[1], {'action': 'set', 'key': 'summaries', 'value': 'off'})
        self.set_switch('off')
        page.wait_for_function("() => !document.querySelector('th[data-col=summary]')", timeout=20000)
        self.assertEqual(page.text_content('#run-summaries .sum-model'), '')
        self.assertClean(page)

    def test_a_task_summary_shows_its_text_model_time_sources_and_out_of_date(self):
        page = self.open(cdn=False, view='2d')
        page.route(CONTROLS, self.control)
        page.goto(self.url + '#/runs/' + self.key + '/tasks/reader')
        page.wait_for_selector('#inspector .sum-section .sum-text')
        self.assertEqual(page.text_content('#inspector .sum-text'), TEXT, 'the text as written, <b> and all')
        self.assertEqual(page.locator('#inspector .sum-text b').count(), 0)
        self.assertRegex(page.text_content('#inspector .sum-by'), r'^Generated by sol:low, \d+m ago')
        self.assertEqual(page.locator('#inspector .sum-section .chip', has_text='Out of date').count(), 1, 'its status is newer')
        self.assertIn('finding:', page.text_content('#inspector .sum-sources li:has(a:text-is("Missing status file"))'))
        self.assertIn('attempt 1', page.text_content('#inspector .sum-sources'))
        self.assertIn('dwarf', page.eval_on_selector_all('#inspector .sum-sources a', 'as => as.map(a => a.textContent)'))
        page.click('#inspector .sum-sources a:text-is("dwarf")')                             # the attempt's execution, as the trail links it
        page.wait_for_selector('#inspector .insp-head h2:text-is("reader · dwarf")')
        page.goto(self.url + '#/runs/' + self.key + '/tasks/reader')
        page.wait_for_selector('#inspector .sum-sources a:text-is("Missing status file")')
        page.click('#inspector .sum-sources a:text-is("Missing status file")')               # scrolls to the findings: no navigation
        self.assertTrue(page.url.endswith('/tasks/reader'), page.url)
        page.goto(self.url + '#/runs/' + self.key + '/tasks/graph')
        page.wait_for_selector('#inspector .sum-section .sum-text')
        self.assertEqual(page.locator('#inspector .sum-section .chip', has_text='Out of date').count(), 0, 'newer than its files')
        self.assertEqual(page.text_content('#inspector .sum-error'), 'The last try failed: quota exhausted')
        self.assertEqual(page.locator('#inspector .sum-sources').count(), 0)
        self.assertClean(page)

    def test_a_task_switch_sends_the_override_and_a_poll_keeps_the_question(self):
        page = self.open(cdn=False, view='2d')
        page.route(CONTROLS, self.control)
        page.goto(self.url + '#/runs/' + self.key + '/tasks/reader')
        page.wait_for_selector('#inspector .sum-switch .sum-bank')
        self.assertEqual(page.get_attribute('#inspector .sum-bank button[data-value=inherit]', 'aria-pressed'), 'true')
        page.click('#inspector .sum-bank button[data-value=off]')
        self.assertEqual(page.text_content('#inspector .sum-section .ui-confirm .ui-confirm-text'),
                         'Set Summaries for task `reader` to off? Takes effect at its next change')
        self.mark(page); self.poll(); self.rebuilt(page)                                       # the inspector is drawn again
        self.assertTrue(page.is_visible('#inspector .sum-section .ui-confirm'), 'the question is still asked')
        page.click('#inspector .sum-section .ui-confirm button:text-is("Confirm")')
        page.wait_for_selector('#inspector .sum-msgs .ui-outcome.ok')
        self.assertEqual(self.posts, [{'action': 'set', 'key': 'summaries', 'value': 'off', 'task': 'reader'}])
        self.assertClean(page)

    def test_a_poll_keeps_the_keyboard_on_the_task_switch(self):
        page = self.open(cdn=False, view='2d')
        page.route(CONTROLS, self.control)
        page.goto(self.url + '#/runs/' + self.key + '/tasks/reader')
        page.wait_for_selector('#inspector .sum-switch .sum-bank')
        page.click('#inspector .sum-bank button[data-value=off]')
        page.click('#inspector .sum-section .ui-confirm button:text-is("Cancel")')         # the keyboard goes back to the key
        self.assertEqual(page.evaluate('document.activeElement.dataset.value'), 'off')
        self.mark(page); self.poll(); self.rebuilt(page)
        page.wait_for_function("() => document.activeElement && document.activeElement.dataset.value === 'off'")
        self.assertEqual(self.posts, [], 'cancelled: nothing was sent')
        self.assertClean(page)

    def test_a_read_only_server_shows_the_summary_without_switches(self):
        self.set_switch('on', 'sol:low')
        ro = make_server(reader.Catalog(plans=[self.plan], registry=False), port=0, quiet=True, read_only=True)
        thread = threading.Thread(target=ro.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (ro.shutdown(), ro.server_close(), thread.join(timeout=2)))
        base = f'http://127.0.0.1:{ro.server_address[1]}/'
        page = self.open(cdn=False, view='2d')
        page.goto(base + '#/runs/' + self.key + '/tasks/reader')
        page.wait_for_selector('#inspector .sum-section .sum-text')
        page.wait_for_function("() => !document.querySelector('#inspector .sum-switch')")  # the page learns it cannot write
        self.assertEqual(page.text_content('#inspector .sum-text'), TEXT)
        self.assertEqual(page.locator('#inspector .sum-section button, #inspector .sum-section .ui-confirm').count(), 0)
        page.goto(base + '#/runs/' + self.key)
        page.wait_for_selector('th[data-col=summary]')
        self.assertEqual(page.text_content('#run-summaries .sum-state'), 'on')
        self.assertEqual(page.locator('#run-summaries button').count(), 0)
        self.assertEqual(page.text_content('#run-summaries .sum-model'), 'sol:low')
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
