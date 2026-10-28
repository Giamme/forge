"""The Settings view (#/settings) in a real browser (Chromium through Python Playwright, skipped without it).

GET /api/config and the run list are answered by page.route, so no forge CLI runs; the page clock is fast-forwarded
past the view's 20 s read interval instead of waiting it out. Development-only: nothing here runs in forge itself.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

from forge_ui import reader
import run_fixture as F
import test_browser as tb       # a module import: `from test_browser import Page` would run that class here too

READ_EVERY_MS = 21000           # past settings.js's POLL_MS


def setUpModule():
    tb.setUpModule()


def tearDownModule():
    tb.tearDownModule()


_GATES = ('drift', 'forward', 'prompt', 'memory-curate', 'memory-slice', 'memory-stale', 'verify-discover')


def decide(floor=False):
    gates = {name: {'mode': 'off', 'source': 'default', 'provider': None, 'model': None,
                    'skip': None, 'ready': False, 'site': 'jev-' + name} for name in _GATES}
    return {
        'user_path': '/cfg/forge/decide.json', 'repo_path': None, 'migrated': False, 'legacy': False,
        'profile': None, 'gates': gates, 'providers': {},
        'budget': {'caps': {'run_tokens': 1000, 'run_usd': None, 'daily_tokens': 40000, 'daily_usd': 2},
                   'spend': {'date': '2026-10-28', 'today': {'tokens': 12, 'usd': 0.25}, 'run': None}},
        'privacy': {'floor': floor, 'path': '/r/one/.forge/decide.json' if floor else None},
        'shadowed': [], 'errors': [],
    }


def config(repo, verify, jev=False, floor=False):
    file = lambda content: {'exists': content is not None, 'content': content}
    return {
        'registry': {'path': '/cfg/registry.tsv', 'rows': [], 'reason': 'Read-only here.'},
        'jev': {'enabled': jev, 'path': '/cfg/forge.json', 'exists': True},
        'decide': decide(floor),
        'repo': {'path': repo, 'files': {'verify': file(verify), 'setup': file(None), 'verify-subset': file(None)}},
        'dataset': {'path': '/data', 'enabled': False}, 'env': {},
    }


@unittest.skipUnless(tb.sync_playwright, 'install Python Playwright to run browser checks')
class Settings(unittest.TestCase):
    """Two runs of two repositories; the test decides what /api/config and /api/runs answer."""
    open, assertClean = tb.Page.open, tb.Page.assertClean

    def setUp(self):
        self.temp = temp = tempfile.TemporaryDirectory(prefix='forge-ui-settings-')       # _serve cleans it up on its skip paths
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.one, self.two = F.make_plan(base / 'one'), F.make_solo(base / 'two')
        self.a, self.b = reader.run_key(self.one), reader.run_key(self.two)
        tb._serve(self, reader.Catalog(plans=[self.one, self.two], registry=False))
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close(), self.thread.join(timeout=2)))
        self.repos = {self.a: '/r/one', self.b: '/r/two'}
        self.configs = {self.a: config('/r/one', 'make test\n'), self.b: config('/r/two', 'pytest -q\n')}
        self.fail = None                 # (status, body) to answer GET /api/config with, or None
        self.goal = None                 # a goal that replaces run a's in the run list

    def page(self):
        page = self.open(cdn=False, view='2d')
        page.clock.install()

        def runs(route):
            data = route.fetch().json()
            for r in data['runs']:
                r['repo'] = self.repos.get(r['key'], r.get('repo'))
                r['updated'] = 2000 if r['key'] == self.a else 1000           # run a is the default
                if r['key'] == self.a and self.goal:
                    r['goal'] = self.goal
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data))

        def cfg(route):
            if route.request.method != 'GET':
                return route.continue_()
            if self.fail:
                status, body = self.fail
                return route.fulfill(status=status, content_type='application/json', body=json.dumps(body))
            key = parse_qs(urlparse(route.request.url).query)['run'][0]
            route.fulfill(status=200, content_type='application/json', body=json.dumps(self.configs[key]))

        page.route('**/api/runs', runs)
        page.route('**/api/config?**', cfg)
        page.goto(self.url + '#/settings')
        page.wait_for_selector('#set-f-verify')
        self.assertEqual(page.input_value('#set-run'), self.a)
        return page

    def next_read(self, page):
        page.clock.fast_forward(READ_EVERY_MS)

    def relisted(self, page, goal):
        """Change run a's goal and wait until the page has taken the new run list in (and handed it to the view)."""
        self.goal = goal
        page.clock.fast_forward(6000)                                    # past the page's own refresh interval
        page.wait_for_function("g => (window.ForgeUI.state.listText || '').includes(g)", arg=goal, timeout=20000)

    def option(self, page, key):
        return page.text_content(f'#set-run option[value="{key}"]')

    def test_a_switch_confirm_survives_a_run_list_change_and_its_answer_is_applied(self):
        page = self.page()
        page.fill('#set-f-verify', 'make test -j4')
        # no: the run and the edit stay, and the picker then shows the new list
        page.select_option('#set-run', self.b)
        page.wait_for_selector('.set-picker .ui-confirm')
        self.relisted(page, 'a goal renamed while asking')
        self.assertEqual(page.locator('.set-picker .ui-confirm').count(), 1, 'the open confirm stays')
        page.click('.set-picker .ui-confirm button:text-is("Cancel")')
        self.assertEqual(page.input_value('#set-run'), self.a)
        self.assertEqual(page.input_value('#set-f-verify'), 'make test -j4')
        self.assertIn('a goal renamed while asking', self.option(page, self.a))
        # yes: the switch happens and the edit is dropped
        page.select_option('#set-run', self.b)
        page.wait_for_selector('.set-picker .ui-confirm')
        self.relisted(page, 'and renamed again')
        page.click('.set-picker .ui-confirm button:text-is("Confirm")')
        page.wait_for_function("() => document.querySelector('#set-f-verify')?.value === 'pytest -q\\n'")
        self.assertEqual(page.input_value('#set-run'), self.b)
        self.assertIn('and renamed again', self.option(page, self.a))
        self.assertClean(page)

    def test_typing_on_text_a_held_read_replaced_warns_that_the_file_changed(self):
        page = self.page()
        page.select_option('#set-gate-drift-mode', 'shadow')             # the mode confirm holds the body
        page.wait_for_selector('#set-decide .ui-confirm')
        self.configs[self.a] = config('/r/one', 'make all\n')
        with page.expect_response('**/api/config?**'):
            self.next_read(page)
        self.assertEqual(page.input_value('#set-f-verify'), 'make test\n', 'the held body still shows the old text')
        page.click('#set-f-verify'); page.keyboard.press('End'); page.keyboard.type('# mine')
        note = page.locator('#set-f-verify').locator('xpath=..').locator('.set-file-note', has_text='changed on disk')
        self.assertTrue(note.is_visible(), 'the save would overwrite make all: the warning shows')
        page.click('#set-decide .ui-confirm button:text-is("Cancel")')
        self.assertEqual(page.input_value('#set-f-verify'), 'make test\n# mine')
        self.assertTrue(note.is_visible(), 'and stays after the redraw')
        self.assertClean(page)

    def test_changing_drift_records_the_same_mode_the_cli_would(self):
        page = self.page()
        posted = {}

        def capture(route):
            if route.request.method != 'POST':
                return route.fallback()
            posted['body'] = route.request.post_data_json
            gate = self.configs[self.a]['decide']['gates']['drift']
            gate['mode'] = posted['body']['value']
            gate['source'] = 'user'
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'ok': True, 'key': posted['body']['key'],
                                           'previous': 'off', 'value': posted['body']['value']}))

        page.route('**/api/config', capture)
        page.select_option('#set-gate-drift-mode', 'shadow')
        page.wait_for_selector('#set-decide .ui-confirm')
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.split('?', 1)[0].endswith('/api/config')):
            page.click('#set-decide .ui-confirm button:text-is("Confirm")')
        page.wait_for_function("() => document.querySelector('#set-gate-drift-source')?.textContent === 'user'")
        self.assertEqual(page.input_value('#set-gate-drift-mode'), 'shadow')
        self.assertEqual(posted['body'], {'run': self.a, 'key': 'decide.gate.drift', 'value': 'shadow'})
        self.assertClean(page)

    def test_under_a_privacy_floor_a_gate_still_writes_the_user_level(self):
        # The user file sets drift on and the floor keeps it off here. The select shows the mode in effect, and choosing
        # that same mode must still write the user level, which is where the control writes.
        self.configs[self.a] = config('/r/one', 'make test\n', floor=True)
        self.configs[self.a]['decide']['gates']['drift'].update(mode='off', source='user', skip='privacy')
        page = self.page()
        # Nothing shows the user level's mode here, so the select holds no mode: choosing the one in effect is a change.
        self.assertNotIn(page.input_value('#set-gate-drift-mode'), ('off', 'shadow', 'on'))
        posted = {}

        def capture(route):
            if route.request.method != 'POST':
                return route.fallback()
            posted['body'] = route.request.post_data_json
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'ok': True, 'key': posted['body']['key'],
                                           'previous': 'on', 'value': posted['body']['value']}))

        page.route('**/api/config', capture)
        page.select_option('#set-gate-drift-mode', 'off')
        page.wait_for_selector('#set-decide .ui-confirm')
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.split('?', 1)[0].endswith('/api/config')):
            page.click('#set-decide .ui-confirm button:text-is("Confirm")')
        self.assertEqual(posted['body'], {'run': self.a, 'key': 'decide.gate.drift', 'value': 'off'})
        self.assertClean(page)

    def test_a_privacy_floor_says_nothing_is_sent_and_the_gates_stay_editable(self):
        self.configs[self.a] = config('/r/one', 'make test\n', floor=True)
        page = self.page()
        self.assertEqual(page.text_content('#set-decide-privacy'),
                         'Nothing is sent. This repository is under a privacy floor.')
        self.assertEqual(page.locator('.set-gate-skip').all_text_contents(), ['skip: privacy'] * 7)
        self.assertTrue(page.is_enabled('#set-gate-drift-mode'))
        self.assertIn('forge decide provider add …', page.text_content('#set-decide'))
        budget = page.text_content('#set-decide-budget')
        self.assertIn('run cap 1000 tokens', budget)
        self.assertIn('today (2026-10-28): 12 tokens of 40000', budget)
        self.assertClean(page)

    def test_a_failed_read_after_a_good_one_keeps_the_page_and_the_unsaved_text(self):
        page = self.page()
        page.fill('#set-f-verify', 'make test -j4')
        self.fail = (504, {'error': 'timeout', 'detail': 'forge config show timed out'})
        with page.expect_response('**/api/config?**'):
            self.next_read(page)
        page.wait_for_selector('.set-stale:not([hidden])')
        self.assertIn('forge config show timed out', page.text_content('.set-stale'))
        self.assertEqual(page.input_value('#set-f-verify'), 'make test -j4')
        self.assertTrue(page.is_enabled('#set-f-verify + .set-problem + .set-keys button:text-is("Save")'))
        self.fail = None
        with page.expect_response('**/api/config?**'):
            self.next_read(page)
        page.wait_for_selector('.set-stale', state='hidden')
        self.assertEqual(page.input_value('#set-f-verify'), 'make test -j4')
        self.assertClean(page)

    def test_a_failed_first_read_shows_the_error_page(self):
        self.fail = (504, {'error': 'timeout', 'detail': 'forge config show timed out'})
        page = self.open(cdn=False, view='2d')
        page.route('**/api/config?**', lambda route: route.fulfill(status=504, content_type='application/json', body=json.dumps(self.fail[1])))
        page.goto(self.url + '#/settings')
        page.wait_for_selector('.set-body .warn')
        self.assertEqual(page.text_content('.set-body .warn'), 'Could not read the configuration: forge config show timed out')
        self.assertEqual(page.locator('#set-f-verify').count(), 0)
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
