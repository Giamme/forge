"""Brief questions in a real browser (Chromium through Python Playwright, skipped without it).

Answering posts to the brief answer route and the answer shows as text. A late answer shows the CLI notice.
Read-only has the questions and no answer box. Parked questions stay collapsed, with no box. The run's list
opens the task, flagged questions first. The route is answered by the test (`page.route`), so no `forge brief`
runs. Fixtures come from test_browser.
"""
from __future__ import annotations

import json
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

ROUTE = re.compile(r'/api/runs/[^/]+/tasks/([^/]+)/brief/(q[0-9]+)$')
ANSWER = 'use <b>8080</b>'
LATE = 'use <b>later</b>'


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Brief(unittest.TestCase):
    """graph is running and unflagged, server is pending and flagged, page is merged with a parked question,
    docs failed with a stale one. server sorts first: it is flagged, and graph would win on start alone."""
    open, assertClean = tb.Page.open, tb.Page.assertClean

    def setUp(self):
        self.temp = temp = tempfile.TemporaryDirectory(prefix='forge-ui-brief-')
        self.addCleanup(temp.cleanup)
        self.plan = F.make_plan(Path(temp.name) / 'p')
        self.key = reader.run_key(self.plan)
        F.write(self.plan / 'brief', 'gated\n')
        F.write(self.plan / 'tasks/graph/brief.json', {'questions': [
            {'id': 'q1', 'text': 'Draw <b>which</b>?', 'why': 'the picture', 'options': ['bars', 'nodes'], 'default': 'nodes'}]})
        F.write(self.plan / 'tasks/server/brief.json', {'questions': [
            {'id': 'q1', 'text': 'Which <b>port</b>?', 'why': 'the proxy', 'options': ['8080', '9090'],
             'default': '8080', 'must_answer': True}]})
        F.write(self.plan / 'tasks/page/brief.json', {'questions': [
            {'id': 'q1', 'text': 'Layout?'}]})
        F.write(self.plan / 'tasks/page/brief.parked.json', {'questions': [
            {'id': 'q2', 'text': 'For the <b>whole</b> plan?', 'scope': 'plan', 'why': 'wider than this task'}]})
        F.write(self.plan / 'tasks/docs/brief.json', {'questions': [
            {'id': 'q1', 'text': 'Which page?', 'possibly_stale': True}]})
        tb._serve(self, reader.Catalog(plans=[self.plan], registry=False))
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))
        self.posts = []
        self.late = False

    def answer_route(self, route):
        """The answer route, answered by the test. A POST is recorded; a GET goes on to the server."""
        request = route.request
        if request.method != 'POST':
            return route.continue_()
        match = ROUTE.search(request.url)
        body = json.loads(request.post_data or '{}')
        task, qid = match.group(1), match.group(2)
        self.posts.append({'task': task, 'qid': qid, 'body': body, 'token': request.headers.get('x-forge-ui-token')})
        text = body.get('answer', '')
        if self.late:
            notice = ('too late for task %s: the answer does not change this task\n'
                      'answer: %s\n'
                      'forge control add /plans/demo %s-%s\n' % (task, text, task, qid))
            F.write(self.plan / 'tasks' / task / 'brief.late.json', {qid: {'text': text, 'actor': 'ui', 't': time.time()}})
            output = notice
        else:
            F.write(self.plan / 'tasks' / task / 'brief.answers.json', {qid: {'text': text, 'actor': 'ui', 't': time.time()}})
            output = '%s/%s answered\n' % (task, qid)
        route.fulfill(status=200, content_type='application/json', body=json.dumps({'exit': 0, 'output': output}))

    def test_answering_posts_the_answer_and_shows_it_as_text(self):
        page = self.open(cdn=False, view='2d')
        page.route(ROUTE, self.answer_route)
        page.goto(self.url + '#/runs/' + self.key + '/tasks/server')
        page.wait_for_function("() => document.querySelector('#inspector .brief-section')?.dataset.controls === 'on'")
        section = page.text_content('#inspector .brief-section')
        self.assertIn('Which <b>port</b>?', section)
        self.assertIn('the proxy', section)
        self.assertIn('8080 · 9090', section)
        self.assertIn('if unanswered: 8080', section)
        self.assertIn('must answer', section)
        titles = page.evaluate("""() => [...document.querySelectorAll('#inspector .insp-section h3')].map(h => h.textContent)""")
        self.assertGreater(titles.index('Questions'), titles.index('Summary'))
        page.fill('#inspector .brief-box textarea', ANSWER)
        page.click('#inspector .brief-send')
        page.wait_for_function(
            "() => document.querySelector('#inspector .brief-got')?.textContent === 'Answer: use <b>8080</b>'", timeout=20000)
        token = page.evaluate("document.querySelector('meta[name=forge-ui-token]').content")
        self.assertTrue(token)
        self.assertEqual(self.posts, [{'task': 'server', 'qid': 'q1', 'body': {'answer': ANSWER}, 'token': token}])
        self.assertEqual(page.locator('#inspector .brief-section b, #inspector .brief-section i').count(), 0)
        self.assertEqual(page.locator('#inspector .brief-notice').count(), 0)
        self.assertClean(page)

    def test_a_late_answer_shows_the_notice(self):
        self.late = True
        page = self.open(cdn=False, view='2d')
        page.route(ROUTE, self.answer_route)
        page.goto(self.url + '#/runs/' + self.key + '/tasks/page')
        page.wait_for_function("() => document.querySelector('#inspector .brief-section')?.dataset.controls === 'on'")
        page.fill('#inspector .brief-section > .brief-questions .brief-box textarea', LATE)
        page.click('#inspector .brief-section > .brief-questions .brief-send')
        page.wait_for_function("""() => {
          const note = document.querySelector('#inspector .brief-notice');
          const got = document.querySelector('#inspector .brief-got');
          return note && got && note.textContent.includes('forge control add') && got.textContent === 'Answer: use <b>later</b>';
        }""", timeout=20000)
        self.assertIn(LATE, page.text_content('#inspector .brief-notice'))
        self.assertIn('too late for task page', page.text_content('#inspector .brief-notice'))
        self.assertEqual(page.locator('#inspector .brief-section b, #inspector .brief-section i').count(), 0)
        self.assertEqual(self.posts[0]['task'], 'page')
        self.assertEqual(self.posts[0]['body'], {'answer': LATE})
        self.assertClean(page)

    def test_read_only_shows_the_questions_without_the_box(self):
        ro = make_server(reader.Catalog(plans=[self.plan], registry=False), port=0, quiet=True, read_only=True)
        thread = threading.Thread(target=ro.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (ro.shutdown(), ro.server_close(), thread.join(timeout=2)))
        base = f'http://127.0.0.1:{ro.server_address[1]}/'
        page = self.open(cdn=False, view='2d')
        page.goto(base + '#/runs/' + self.key + '/tasks/server')
        page.wait_for_function("""() => {
          const section = document.querySelector('#inspector .brief-section');
          return section && section.dataset.controls === 'off' && !section.querySelector('.brief-box');
        }""")
        self.assertIn('Which <b>port</b>?', page.text_content('#inspector .brief-section'))
        self.assertIn('must answer', page.text_content('#inspector .brief-section'))
        self.assertEqual(page.locator('#inspector .brief-section textarea, #inspector .brief-section button').count(), 0)
        self.assertEqual(page.locator('#inspector .brief-section b').count(), 0)
        self.assertClean(page)

    def test_parked_questions_stay_collapsed_and_have_no_box(self):
        page = self.open(cdn=False, view='2d')
        page.goto(self.url + '#/runs/' + self.key + '/tasks/page')
        page.wait_for_selector('#inspector .brief-parked')
        page.wait_for_function("() => document.querySelector('#inspector .brief-section')?.dataset.controls === 'on'")
        self.assertFalse(page.evaluate("document.querySelector('#inspector .brief-parked').open"))
        self.assertEqual(page.text_content('#inspector .brief-parked > summary'), 'Parked (wider scope)')
        self.assertIn('For the <b>whole</b> plan?', page.text_content('#inspector .brief-parked'))
        self.assertEqual(page.locator('#inspector .brief-parked .brief-box, #inspector .brief-parked textarea').count(), 0)
        self.assertEqual(page.locator('#inspector .brief-parked b').count(), 0)
        self.assertEqual(page.locator('#inspector .brief-section > .brief-questions .brief-box').count(), 1)
        self.assertIn('Layout?', page.text_content('#inspector .brief-section > .brief-questions'))
        self.assertClean(page)

    def test_the_run_list_opens_the_flagged_task(self):
        page = self.open(cdn=False, view='2d')
        page.goto(self.url + '#/runs/' + self.key)
        page.wait_for_selector('#run-questions a.brief-open')
        self.assertEqual(page.locator('#run-questions a.brief-open').count(), 4)
        self.assertNotIn('whole', page.text_content('#run-questions'))
        first = page.locator('#run-questions a.brief-open').first
        self.assertIn('/tasks/server', first.get_attribute('href'))
        self.assertIn('starts after graph merges', first.text_content())
        self.assertIn('must answer', first.text_content())
        self.assertIn('possibly stale', page.locator('#run-questions a.brief-open', has_text='Which page?').text_content())
        self.assertEqual(page.locator('#run-questions b, #run-questions i').count(), 0)
        first.click()
        page.wait_for_selector('#inspector .insp-head h2')
        self.assertIn('server', page.text_content('#inspector .insp-head h2'))
        self.assertIn('Which <b>port</b>?', page.text_content('#inspector .brief-section'))
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
