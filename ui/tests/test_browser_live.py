"""#115: a live refresh never cancels what the user is doing, and #113: the version beside the logo.

Over the live fixture, with every poll's answer changed, as a busy run's are: a press, a selection, a focused
<select> and an opened disclosure survive it, a held region shows the latest data when the use ends, and an idle
region still redraws, so none of this can pass just because nothing was redrawn."""
import os
from pathlib import Path
import re
import tempfile
import time
import unittest

from forge_ui import reader
import run_fixture as F
import test_browser as tb

setUpModule, tearDownModule = tb.setUpModule, tb.tearDownModule
MARK = 'POLLMARK-'


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class LiveRefresh(unittest.TestCase):
    open, assertClean = tb.Page.open, tb.Page.assertClean

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-live-')
        cls.plan = F.make_live(Path(cls.temp.name), time.time(), os.getpid())
        cls.key = reader.run_key(cls.plan)
        tb._serve(cls, reader.Catalog(plans=[cls.plan], registry=False))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def live_page(self, hash_):
        """The page at `hash_`, with each answer to a run or the models changed, so every poll redraws."""
        page = self.open(cdn=False, view='2d')
        self.polls = 0                  # answers to what this page shows: not the run list beside a run
        self.closing = False
        self.move_repos = False         # each poll also moves the runs to a new repository (new filter options)
        run_path = '/api/runs/' + self.key
        shown = run_path if '/runs/' in hash_ else '/api/models' if 'models' in hash_ else '/api/runs'

        def change(route):
            try:
                answer(route)
            except Exception:                                 # playwright is an optional import here
                if not (self.closing or page.is_closed()):
                    raise

        def answer(route):
            response = route.fetch()
            body = response.json()
            if route.request.url.endswith(shown):
                self.polls += 1
            if route.request.url.endswith(run_path):
                body['goal'] = f'{MARK}{self.polls}'
                for task in body.get('tasks') or []:
                    task['title'] = f'{MARK}{self.polls}'
            else:
                body['__poll'] = self.polls
                if route.request.url.endswith('/api/runs'):
                    for run in body['runs']:
                        run['goal'] = f'{MARK}{self.polls}'
                        if self.move_repos:
                            run['repo'] = run['repo_root'] = f'/r/{self.polls}'
            route.fulfill(response=response, json=body)

        page.route(re.compile(r'/api/(runs(/[^/?]+)?|models)$'), change)
        page.goto(f'{self.url}{hash_}')
        return page

    def done(self, page):
        self.closing = True                                   # an answer still on its way has nowhere to go
        page.close()
        self.closing = False

    def next_poll(self, page):
        """Wait for a poll that brought new data, and for the page to have dealt with it."""
        before = self.polls
        deadline = time.time() + 15
        while self.polls <= before and time.time() < deadline:
            page.wait_for_timeout(100)
        self.assertGreater(self.polls, before, 'no poll arrived')
        page.wait_for_timeout(300)

    def identity(self, page, selector):
        """Tag the node, so a later `same` says whether a poll replaced it."""
        page.eval_on_selector(selector, 'n => { n.__mine = true; }')

    def same(self, page, selector):
        return page.eval_on_selector(selector, 'n => !!n.__mine')

    def press_across_a_poll(self, page, selector):
        # In the page: the inspector also redraws as its lazy reads land, so a held locator can go stale in between.
        x, y = page.eval_on_selector(selector, """n => { n.scrollIntoView({ block: 'nearest' }); const b = n.getBoundingClientRect();
            return [b.x + b.width / 2, b.y + b.height / 2]; }""")
        page.mouse.move(x, y)
        page.mouse.down()
        self.next_poll(page)
        page.mouse.up()

    def test_a_click_pressed_before_a_poll_and_released_after_it_still_fires(self):
        for name, start, selector in (
                ('inspector close', '#/runs/{key}/tasks/api', '#inspector .insp-close'),
                ('task table row', '#/runs/{key}', '.run-tasks tbody tr:nth-child(2) td:first-child'),
                ('column button', '#/runs/{key}', '.dt-tools button'),
                ('run header link', '#/runs/{key}', '#run-head a[href]'),
                ('map card', '#/', '#map-cards .map-card'),
                ('cloud card', '#/cloud', '.cloud-list .map-card')):
            with self.subTest(name):
                page = self.live_page(start.format(key=self.key))
                try:
                    page.wait_for_selector(selector)
                    page.evaluate("() => { window.__clicks = 0; document.addEventListener('click', () => { window.__clicks++; }, true); }")
                    self.press_across_a_poll(page, selector)
                    page.wait_for_timeout(100)
                    self.assertEqual(page.evaluate('window.__clicks'), 1, 'the release did not click')
                    self.assertClean(page)
                finally:
                    self.done(page)                                   # a page left polling would count in the next row's polls

    def test_a_selection_survives_a_poll_and_then_the_latest_data_shows(self):
        for name, start, selector in (('inspector', f'#/runs/{self.key}/tasks/api', '#inspector .insp-head'),
                                      ('run header', f'#/runs/{self.key}', '#run-head .run-head-body > *')):
            with self.subTest(name):
                page = self.live_page(start)
                try:
                    page.wait_for_selector(selector)
                    self.next_poll(page)
                    page.evaluate("""s => { const r = document.createRange(); r.selectNodeContents(document.querySelector(s));
                        const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }""", selector)
                    text = page.evaluate('getSelection().toString()')
                    self.assertTrue(text.strip())
                    self.identity(page, selector)
                    self.next_poll(page)
                    self.assertEqual(page.evaluate('getSelection().toString()'), text)
                    self.assertTrue(self.same(page, selector))
                    page.evaluate('getSelection().removeAllRanges()')
                    page.wait_for_timeout(200)
                    self.assertFalse(self.same(page, selector))           # the held redraw ran
                    self.assertClean(page)
                finally:
                    self.done(page)

    def test_a_focused_select_keeps_its_element_on_every_page(self):
        for name, start, selector in (('models', '#/models', '#models-view select'),
                                      ('cloud', '#/cloud', '.cloud-select select[data-key="difficulty-mode"]')):
            with self.subTest(name):
                page = self.live_page(start)
                try:
                    self.move_repos = name == 'cloud'                     # new options are what rebuild its filters
                    page.wait_for_selector(selector)
                    self.next_poll(page)
                    self.identity(page, selector)
                    page.focus(selector)
                    self.next_poll(page)
                    self.next_poll(page)
                    self.assertTrue(self.same(page, selector))
                    self.assertEqual(page.evaluate('document.activeElement.tagName'), 'SELECT')
                    page.evaluate('document.activeElement.blur()')
                    page.wait_for_timeout(200)
                    self.assertFalse(self.same(page, selector))           # and it does rebuild, once the use ends
                    self.assertClean(page)
                finally:
                    self.done(page)

    def test_changing_the_focused_select_still_draws_at_once(self):
        page = self.live_page('#/models')
        page.wait_for_selector('#models-view select')
        selector = '#models-view select'
        values = page.eval_on_selector(selector, 's => [...s.options].map(o => o.value)')
        self.assertGreater(len(values), 1)
        page.focus(selector)
        self.identity(page, selector)
        page.select_option(selector, values[-1])
        page.wait_for_timeout(200)
        self.assertFalse(self.same(page, selector))                           # what the user asked for did not wait
        self.assertClean(page)

    def test_a_disclosure_opened_by_hand_stays_open_across_a_poll(self):
        for name, start, selector, redrawn in (
                ('task prompt', f'#/runs/{self.key}/tasks/api', '#inspector details[data-keep$="task-api-prompt"]', '#inspector .insp-head'),
                ('run request', f'#/runs/{self.key}', 'details[data-keep$="run-request"]', '#run-head .run-head-body > *'),
                ('models tier', '#/models', '#models-view details.lb-diff', '#models-view details.lb-diff')):
            with self.subTest(name):
                page = self.live_page(start)
                try:
                    page.wait_for_selector(selector, state='attached')
                    self.next_poll(page)
                    page.eval_on_selector(selector, 'd => { d.open = false; }')
                    page.wait_for_timeout(100)
                    page.eval_on_selector(selector, 'd => { d.open = true; }')
                    page.wait_for_timeout(100)
                    self.identity(page, redrawn)
                    self.next_poll(page)
                    self.assertFalse(self.same(page, redrawn))            # it was rebuilt, and it is still open
                    self.assertTrue(page.eval_on_selector(selector, 'd => d.open'))
                    self.assertClean(page)
                finally:
                    self.done(page)

    def test_leaving_a_diff_with_a_held_redraw_does_not_draw_it_over_what_followed(self):
        page = self.live_page(f'#/runs/{self.key}/tasks/api')
        page.wait_for_selector('#inspector .insp-head')
        page.evaluate(f"location.hash = '#/runs/{self.key}/tasks/api/diff'")
        host = '#diff-view'
        page.wait_for_function("h => document.querySelector(h)?.textContent.trim()", arg=host)
        self.next_poll(page)
        page.evaluate("""s => { const r = document.createRange(); r.selectNodeContents(document.querySelector(s));
            const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }""", host)
        self.next_poll(page)                                                  # the diff's redraw is now held
        page.evaluate('history.back()')
        page.wait_for_function(f"() => location.hash === '#/runs/{self.key}/tasks/api'")
        page.evaluate('getSelection().removeAllRanges()')
        self.next_poll(page)
        self.assertEqual(page.eval_on_selector(host, 'n => n.textContent.trim()'), '')
        self.assertClean(page)

    def test_a_held_draw_that_throws_does_not_strand_the_ones_behind_it(self):
        page = self.live_page('#/')
        page.wait_for_selector('#map-cards .map-card')
        drawn = page.evaluate("""() => {
            const [a, b] = ['a', 'b'].map(text => { const n = document.createElement('div'); n.textContent = text; document.body.append(n); return n; });
            const range = document.createRange(); range.setStart(a.firstChild, 0); range.setEnd(b.firstChild, 1);
            const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
            window.__drew = [];
            window.ForgeUI.guard(a, () => { window.__drew.push('a'); throw new Error('a draw failed'); });
            window.ForgeUI.guard(b, () => { window.__drew.push('b'); });
            return window.__drew.length; }""")
        self.assertEqual(drawn, 0)
        page.evaluate('getSelection().removeAllRanges()')
        page.wait_for_function('() => window.__drew.length === 2')
        page.wait_for_timeout(50)
        self.assertEqual(self.errors, ['a draw failed'])                      # still reported

    def test_a_run_list_redraw_that_was_held_marks_the_open_run(self):
        page = self.live_page(f'#/runs/{self.key}')
        page.wait_for_selector('#run-list a[aria-current]')
        self.next_poll(page)
        page.evaluate("""() => { const r = document.createRange(); r.selectNodeContents(document.querySelector('#run-list'));
            const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }""")
        self.identity(page, '#run-list a[aria-current]')
        self.next_poll(page)
        page.evaluate('getSelection().removeAllRanges()')
        page.wait_for_function("() => !document.querySelector('#run-list a[aria-current]')?.__mine")
        page.wait_for_timeout(50)
        self.assertEqual(page.locator('#run-list a[aria-current]').count(), 1)
        self.assertClean(page)

    def test_an_idle_region_redraws_on_every_poll(self):
        page = self.live_page(f'#/runs/{self.key}/tasks/api')
        page.wait_for_selector('#inspector .insp-head')
        self.next_poll(page)
        for selector in ('#inspector .insp-head', '#run-head .run-head-body > *', '.run-tasks tbody tr'):
            self.identity(page, selector)
        self.next_poll(page)
        for selector in ('#inspector .insp-head', '#run-head .run-head-body > *', '.run-tasks tbody tr'):
            self.assertFalse(self.same(page, selector), selector)
        self.assertIn(MARK, page.text_content('.run-tasks tbody'))
        self.assertClean(page)

    def test_a_long_hold_says_newer_data_is_waiting_and_offers_to_show_it(self):
        page = self.live_page(f'#/runs/{self.key}/tasks/api')
        selector = '#inspector .insp-head'
        page.wait_for_selector(selector)
        self.next_poll(page)
        page.evaluate("""s => { const r = document.createRange(); r.selectNodeContents(document.querySelector(s));
            const sel = getSelection(); sel.removeAllRanges(); sel.addRange(r); }""", selector)
        self.identity(page, selector)
        self.next_poll(page)
        self.assertFalse(page.is_visible('#live-held'))                       # a short hold says nothing
        page.wait_for_selector('#live-held:not([hidden])', timeout=12000)
        page.click('#live-held button')
        page.wait_for_timeout(200)
        self.assertFalse(self.same(page, selector))
        self.assertFalse(page.is_visible('#live-held'))
        self.assertClean(page)


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class VersionLabel(unittest.TestCase):
    open, assertClean = tb.Page.open, tb.Page.assertClean

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-version-')
        plan = F.make_plan(Path(cls.temp.name))
        tb._serve(cls, reader.Catalog(plans=[plan], registry=False))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def test_the_header_shows_forges_version_with_the_build_in_its_title(self):
        page = self.open(cdn=False, view='2d')
        page.goto(self.url + '#/')
        page.wait_for_selector('#brand-version:not([hidden])')
        version = (Path(__file__).resolve().parents[2] / 'VERSION').read_text().strip()
        self.assertEqual(page.text_content('#brand-version'), 'v' + version)
        self.assertIn('build', page.get_attribute('#brand-version', 'title'))
        self.assertClean(page)

    def test_an_unknown_version_shows_no_label_and_the_page_works(self):
        page = self.open(cdn=False, view='2d')
        page.route('**/api/health', lambda route: route.fulfill(json={'app': 'forge-ui', 'version': None, 'build': 'x', 'pid': 1, 'controls': False}))
        page.goto(self.url + '#/')
        page.wait_for_selector('.topbar .top-link')
        page.wait_for_timeout(300)
        self.assertFalse(page.is_visible('#brand-version'))
        self.assertClean(page)

    def test_the_header_does_not_wrap_more_at_phone_width_for_it(self):
        context = self.browser.new_context(viewport={'width': 390, 'height': 800})
        self.addCleanup(context.close)
        page = self.open(cdn=False, view='2d', context=context)
        page.goto(self.url + '#/')
        page.wait_for_selector('.topbar .brand')
        page.wait_for_timeout(300)
        self.assertFalse(page.is_visible('#brand-version'))
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)


if __name__ == '__main__':
    unittest.main()
