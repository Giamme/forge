"""The Settings view (#/settings) in a real browser (Chromium through Python Playwright, skipped without it).

GET /api/config and the run list are answered by page.route, so no forge CLI runs; the page clock is fast-forwarded
past the view's 20 s read interval instead of waiting it out. Development-only: nothing here runs in forge itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from forge_ui import reader
import run_fixture as F
import test_browser as tb       # a module import: `from test_browser import Page` would run that class here too

READ_EVERY_MS = 21000           # past settings.js's POLL_MS


def setUpModule():
    tb.setUpModule()


def tearDownModule():
    tb.tearDownModule()


_GATES = ('drift', 'forward', 'prompt', 'memory-curate', 'memory-slice', 'memory-stale', 'verify-discover',
          'coverage', 'retry-addressed', 'dwarf-message', 'attribution', 'findings')


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
        self.failing = None                 # (status, body) to answer GET /api/config with, or None
        self.goal = None                 # a goal that replaces run a's in the run list
        self.where = {}                  # key -> (repo_root, repo_worktree, repo_branch) the run list reports
        self.hide = set()                # run keys the list pretends are gone
        self.profiles = None             # a GET /api/config/profiles body, or None to let the server answer

    def page(self):
        page = self.open(cdn=False, view='2d')
        page.clock.install()

        def runs(route):
            data = route.fetch().json()
            if self.hide:
                data['runs'] = [r for r in data['runs'] if r['key'] not in self.hide]
            for r in data['runs']:
                r['repo'] = self.repos.get(r['key'], r.get('repo'))
                r['updated'] = 2000 if r['key'] == self.a else 1000           # run a is the default
                if r['key'] == self.a and self.goal:
                    r['goal'] = self.goal
                if r['key'] in self.where:
                    r['repo_root'], r['repo_worktree'], r['repo_branch'] = self.where[r['key']]
            route.fulfill(status=200, content_type='application/json', body=json.dumps(data))

        def cfg(route):
            if route.request.method != 'GET':
                return route.continue_()
            if self.failing:
                status, body = self.failing
                return route.fulfill(status=status, content_type='application/json', body=json.dumps(body))
            key = parse_qs(urlparse(route.request.url).query)['run'][0]
            route.fulfill(status=200, content_type='application/json', body=json.dumps(self.configs[key]))

        page.route('**/api/runs', runs)
        page.route('**/api/config?**', cfg)
        if self.profiles is not None:
            payload = self.profiles

            def profiles(route):
                if route.request.method != 'GET':
                    return route.continue_()
                route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))

            page.route('**/api/config/profiles?**', profiles)
        page.goto(self.url + '#/settings')
        page.wait_for_selector('#set-f-verify')
        self.assertEqual(self.shown(page), self.a)
        return page

    def shown(self, page):
        """The run the picker shows."""
        return page.get_attribute('#set-run', 'data-run')

    def choose(self, page, key):
        """Open the picker and click the run's option."""
        page.click('#set-run')
        page.click(f'#set-run-list [data-run="{key}"]')

    def next_read(self, page):
        page.clock.fast_forward(READ_EVERY_MS)

    def relisted(self, page, goal):
        """Change run a's goal and wait until the page has taken the new run list in (and handed it to the view)."""
        self.goal = goal
        page.clock.fast_forward(6000)                                    # past the page's own refresh interval
        page.wait_for_function("g => (window.ForgeUI.state.listText || '').includes(g)", arg=goal, timeout=20000)

    def option(self, page, key):
        return page.text_content(f'#set-run-list [data-run="{key}"]')

    def test_a_switch_confirm_survives_a_run_list_change_and_its_answer_is_applied(self):
        page = self.page()
        page.fill('#set-f-verify', 'make test -j4')
        # no: the run and the edit stay, and the picker then shows the new list
        self.choose(page, self.b)
        page.wait_for_selector('.set-picker .ui-confirm')
        self.relisted(page, 'a goal renamed while asking')
        self.assertEqual(page.locator('.set-picker .ui-confirm').count(), 1, 'the open confirm stays')
        page.click('.set-picker .ui-confirm button:text-is("Cancel")')
        self.assertEqual(self.shown(page), self.a)
        self.assertEqual(page.input_value('#set-f-verify'), 'make test -j4')
        self.assertIn('a goal renamed while asking', self.option(page, self.a))
        # yes: the switch happens and the edit is dropped
        self.choose(page, self.b)
        page.wait_for_selector('.set-picker .ui-confirm')
        self.relisted(page, 'and renamed again')
        page.click('.set-picker .ui-confirm button:text-is("Confirm")')
        page.wait_for_function("() => document.querySelector('#set-f-verify')?.value === 'pytest -q\\n'")
        self.assertEqual(self.shown(page), self.b)
        self.assertIn('and renamed again', self.option(page, self.a))
        self.assertClean(page)

    def test_each_run_shows_its_worktree_as_secondary_text_and_the_picker_works_from_the_keyboard(self):
        self.where = {self.b: ('/r/two', '/r/two-wt', 'fix/x')}
        page = self.page()
        option = f'#set-run-list [role=option][data-run="{self.b}"]'
        self.assertEqual(page.text_content(f'{option} .repo-wt'), 'two-wt (fix/x)')
        self.assertEqual(page.get_attribute(f'{option} .repo-wt', 'title'), '/r/two-wt\nbranch fix/x')
        self.assertNotIn('two-wt', page.text_content(f'{option} .set-run-label'))
        # ArrowDown opens the list on the run shown; ArrowDown and Enter choose the next one
        page.focus('#set-run')
        page.keyboard.press('ArrowDown')
        page.wait_for_function("() => document.activeElement?.id === 'set-run-list'")     # the open list takes the focus
        self.assertEqual(page.get_attribute('#set-run', 'aria-expanded'), 'true')
        self.assertEqual(page.get_attribute('#set-run-list', 'aria-activedescendant'), f'set-run-o-{self.a}')
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        page.wait_for_function("() => document.querySelector('#set-f-verify')?.value === 'pytest -q\\n'")
        self.assertEqual(self.shown(page), self.b)
        self.assertEqual(page.text_content('#set-run .repo-wt'), 'two-wt (fix/x)')
        self.assertEqual(page.evaluate('document.activeElement.id'), 'set-run')
        # Escape closes the list, keeps the run and gives the focus back to the button
        page.keyboard.press('ArrowDown')
        page.wait_for_function("() => document.activeElement?.id === 'set-run-list'")
        page.keyboard.press('Escape')
        page.wait_for_selector('#set-run-pop:not(:popover-open)', state='attached')
        self.assertEqual((self.shown(page), page.get_attribute('#set-run', 'aria-expanded')), (self.b, 'false'))
        self.assertEqual(page.evaluate('document.activeElement.id'), 'set-run')
        self.assertClean(page)

    def test_the_path_line_names_the_repository_the_configuration_was_read_from(self):
        # The run's recorded root has moved away, so the server read the settings from the run's own checkout. The page
        # names the directory the configuration came from, not the root it was told.
        self.where = {self.a: ('/old/main', None, None)}
        page = self.page()
        self.assertEqual(page.text_content('.set-repo-path .mono'), '/r/one')
        self.assertClean(page)

    def test_the_setup_guide_opens_as_a_dialog_and_each_gate_explains_itself(self):
        page = self.page()
        self.assertFalse(page.is_visible('#set-guide'))
        page.click('#set-decide-guide')
        page.wait_for_selector('#set-guide[open]')
        self.assertGreaterEqual(page.locator('#set-guide .set-guide-steps > li').count(), 5)
        self.assertIn('provider add', page.inner_text('#set-guide'))
        page.click('#set-guide .set-guide-close')
        self.assertFalse(page.is_visible('#set-guide'))
        for gate in ('drift', 'memory-stale', 'findings'):
            self.assertTrue(page.inner_text(f'#set-gate-{gate} .set-gate-summary').strip())
        self.assertFalse(page.is_visible('#set-gate-drift-help dl'))
        page.click('#set-gate-drift-help summary')
        self.assertTrue(page.is_visible('#set-gate-drift-help dl'))
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
        self.assertEqual(page.locator('.set-gate-skip').all_text_contents(), ['skip: privacy'] * len(_GATES))   # every gate
        self.assertTrue(page.is_enabled('#set-gate-drift-mode'))
        self.assertIn('forge decide provider add …', page.text_content('#set-decide'))
        budget = page.text_content('#set-decide-budget')
        self.assertIn('run cap 1000 tokens', budget)
        self.assertIn('today (2026-10-28): 12 tokens of 40000', budget)
        self.assertClean(page)

    def test_a_failed_read_after_a_good_one_keeps_the_page_and_the_unsaved_text(self):
        page = self.page()
        page.fill('#set-f-verify', 'make test -j4')
        self.failing = (504, {'error': 'timeout', 'detail': 'forge config show timed out'})
        with page.expect_response('**/api/config?**'):
            self.next_read(page)
        page.wait_for_selector('.set-stale:not([hidden])')
        self.assertIn('forge config show timed out', page.text_content('.set-stale'))
        self.assertEqual(page.input_value('#set-f-verify'), 'make test -j4')
        self.assertTrue(page.is_enabled('#set-f-verify + .set-problem + .set-keys button:text-is("Save")'))
        self.failing = None
        with page.expect_response('**/api/config?**'):
            self.next_read(page)
        page.wait_for_selector('.set-stale', state='hidden')
        self.assertEqual(page.input_value('#set-f-verify'), 'make test -j4')
        self.assertClean(page)

    def test_a_failed_first_read_shows_the_error_page(self):
        self.failing = (504, {'error': 'timeout', 'detail': 'forge config show timed out'})
        page = self.open(cdn=False, view='2d')
        page.route('**/api/config?**', lambda route: route.fulfill(status=504, content_type='application/json', body=json.dumps(self.failing[1])))
        page.goto(self.url + '#/settings')
        page.wait_for_selector('.set-body .warn')
        self.assertEqual(page.text_content('.set-body .warn'), 'Could not read the configuration: forge config show timed out')
        self.assertEqual(page.locator('#set-f-verify').count(), 0)
        self.assertClean(page)

    def test_a_profile_can_be_duplicated_edited_set_as_default_and_deleted(self):
        # The list, the copy, the edit and the default hit the real CLI. The run's repository has to be a git
        # toplevel, and the user file has to land in a temp config home rather than the developer's.
        repo = Path(self.temp.name) / 'repo'
        repo.mkdir()
        root = os.path.realpath(repo)
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        (self.one / 'repo').write_text(root + '\n')
        (self.one / 'repo.identity').write_text(json.dumps({'root': root, 'worktree': root, 'branch': None}))
        cfg = Path(self.temp.name) / 'xdg'
        home = Path(self.temp.name) / 'home'
        home.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('FORGE_DECIDE', 'FORGE_JEV')) and key != 'FORGE_REGISTRY'}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(cfg),
                   XDG_CACHE_HOME=str(Path(self.temp.name) / 'cache'),
                   XDG_DATA_HOME=str(Path(self.temp.name) / 'data'),
                   XDG_STATE_HOME=str(Path(self.temp.name) / 'state'))
        with patch.dict(os.environ, env, clear=True):
            page = self.page()
            page.wait_for_selector('#set-prof-balanced', timeout=60000)
            self.assertIn('builtin', page.text_content('#set-prof-balanced'))
            self.assertIn('builtin', page.text_content('#set-prof-lite'))
            page.click('#set-prof-balanced')
            self.assertIn('read-only', page.text_content('#set-profile-detail'))
            self.assertEqual(page.locator('#set-profile-save').count(), 0)
            self.assertEqual(page.locator('#set-profile-delete').count(), 0)
            self.assertEqual(page.locator('#set-profile-default').count(), 0)
            self.assertIn('haiku:xhigh', page.text_content('#set-profile-detail'))
            self.assertIn('retry_failed', page.text_content('#set-profile-policy'))

            page.click('#set-profile-duplicate')
            page.fill('#set-dup-name', 'mine')
            page.select_option('#set-dup-level', 'user')
            page.click('#set-dup-save')
            page.wait_for_selector('#set-profile-detail .ui-confirm')
            page.click('#set-profile-detail .ui-confirm button:text-is("Confirm")')
            page.wait_for_selector('#set-prof-mine', timeout=120000)
            page.wait_for_function("() => document.querySelector('#set-profile-detail h3')?.textContent === 'mine'")
            self.assertIn('user', page.text_content('#set-prof-mine'))

            body = page.input_value('#set-profile-body')
            self.assertIn('"retry_failed": 2', body)
            page.fill('#set-profile-body', body.replace('"retry_failed": 2', '"retry_failed": 4', 1))
            # A refresh that redraws the page (the dataset line changes) must keep the unsaved JSON.
            self.configs[self.a]['dataset']['enabled'] = True
            with page.expect_response('**/api/config?**'):
                self.next_read(page)
            page.wait_for_function("""() => {
              const on = (document.querySelector('#set-dataset')?.textContent || '').includes('Capture is on');
              const text = document.querySelector('#set-profile-body')?.value || '';
              return on && text.includes('"retry_failed": 4');
            }""")
            page.click('#set-profile-save')
            page.wait_for_selector('#set-profile-detail .ui-confirm')
            page.click('#set-profile-detail .ui-confirm button:text-is("Confirm")')
            page.wait_for_function(
                "() => (document.querySelector('#set-profile-policy')?.textContent || '').includes('4')",
                timeout=120000)

            page.click('#set-profile-default')
            page.wait_for_selector('#set-profile-detail .ui-confirm')
            page.click('#set-profile-detail .ui-confirm button:text-is("Confirm")')
            page.wait_for_function(
                "() => [...document.querySelectorAll('#set-prof-mine .tag')].some(t => t.textContent === 'default')",
                timeout=60000)

            page.click('#set-profile-delete')
            page.wait_for_selector('#set-profile-detail .ui-confirm')
            page.click('#set-profile-detail .ui-confirm button:text-is("Confirm")')
            page.wait_for_function("() => !document.querySelector('#set-prof-mine')", timeout=60000)
            self.assertEqual(page.locator('#set-prof-balanced').count(), 1)
            user_file = cfg / 'forge' / 'decide.json'
            if user_file.is_file():
                self.assertNotIn('mine', json.loads(user_file.read_text()).get('profiles', {}))
            self.assertClean(page)

    def test_clearing_a_default_uses_the_storing_level_and_a_repo_profile_copies_by_its_body(self):
        # `mine` is a user profile that the repo file names as default_profile. Clearing must remove that repo
        # key. `team` exists only in the repo file, so a user-level copy cannot use --from (no --repo is passed);
        # the page sends the body. Both go through the real CLI, in a temp config home.
        repo = Path(self.temp.name) / 'repo'
        repo.mkdir()
        root = os.path.realpath(repo)
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        (self.one / 'repo').write_text(root + '\n')
        (self.one / 'repo.identity').write_text(json.dumps({'root': root, 'worktree': root, 'branch': None}))
        team = {'policy': {'retry_failed': 7}}
        (Path(root) / '.forge').mkdir()
        (Path(root) / '.forge' / 'decide.json').write_text(json.dumps({
            'default_profile': 'mine', 'profiles': {'team': team},
        }) + '\n')
        cfg = Path(self.temp.name) / 'xdg'
        (cfg / 'forge').mkdir(parents=True)
        (cfg / 'forge' / 'decide.json').write_text(json.dumps({
            'profiles': {'mine': {'policy': {'retry_failed': 3}}},
        }) + '\n')
        home = Path(self.temp.name) / 'home'
        home.mkdir()
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('FORGE_DECIDE', 'FORGE_JEV')) and key != 'FORGE_REGISTRY'}
        env.update(HOME=str(home), XDG_CONFIG_HOME=str(cfg),
                   XDG_CACHE_HOME=str(Path(self.temp.name) / 'cache'),
                   XDG_DATA_HOME=str(Path(self.temp.name) / 'data'),
                   XDG_STATE_HOME=str(Path(self.temp.name) / 'state'))

        def posted(page, click):
            with page.expect_request(lambda r: r.method == 'POST' and r.url.endswith('/api/config/profiles')) as caught:
                page.click(click)
            return caught.value.post_data_json

        with patch.dict(os.environ, env, clear=True):
            page = self.page()
            page.wait_for_selector('#set-prof-mine', timeout=60000)
            page.wait_for_selector('#set-prof-team')
            self.assertIn('default', page.text_content('#set-prof-mine'))
            page.click('#set-prof-mine')
            page.click('#set-profile-default')
            page.wait_for_selector('#set-profile-detail .ui-confirm')
            question = page.text_content('#set-profile-detail .ui-confirm-text')
            self.assertIn('repo level', question)
            self.assertNotIn('user level', question)
            body = posted(page, '#set-profile-detail .ui-confirm button:text-is("Confirm")')
            self.assertEqual(body['action'], 'use')
            self.assertEqual(body['level'], 'repo')
            self.assertIs(body['none'], True)
            self.assertNotIn('name', body)
            page.wait_for_function("""() => {
              const tags = [...document.querySelectorAll('#set-prof-mine .tag')].map(t => t.textContent);
              return tags.includes('user') && !tags.includes('default');
            }""", timeout=60000)
            repo_data = json.loads((Path(root) / '.forge' / 'decide.json').read_text())
            self.assertNotIn('default_profile', repo_data)
            self.assertEqual(repo_data['profiles']['team'], team)
            user_path = cfg / 'forge' / 'decide.json'
            user_data = json.loads(user_path.read_text())
            self.assertNotIn('default_profile', user_data)
            self.assertIn('mine', user_data['profiles'])

            page.click('#set-prof-team')
            page.click('#set-profile-duplicate')
            page.fill('#set-dup-name', 'personal')
            page.select_option('#set-dup-level', 'user')
            page.click('#set-dup-save')
            page.wait_for_selector('#set-profile-detail .ui-confirm')
            body = posted(page, '#set-profile-detail .ui-confirm button:text-is("Confirm")')
            self.assertEqual(body['name'], 'personal')
            self.assertEqual(body['level'], 'user')
            self.assertNotIn('from', body)
            self.assertEqual(body['body'], team)
            page.wait_for_selector('#set-prof-personal', timeout=120000)

            page.fill('#set-new-name', 'other')
            page.select_option('#set-new-level', 'user')
            page.select_option('#set-new-source', 'team')
            page.click('#set-new-save')
            page.wait_for_selector('#set-new .ui-confirm')
            body = posted(page, '#set-new .ui-confirm button:text-is("Confirm")')
            self.assertEqual(body['name'], 'other')
            self.assertEqual(body['level'], 'user')
            self.assertNotIn('from', body)
            self.assertEqual(body['body'], team)
            page.wait_for_selector('#set-prof-other', timeout=120000)
            user_data = json.loads(user_path.read_text())
            self.assertEqual(user_data['profiles']['personal'], team)
            self.assertEqual(user_data['profiles']['other'], team)
            self.assertClean(page)

    def test_an_unsaved_profile_survives_the_run_leaving_and_a_switch_asks_first(self):
        # The file editors are clean. Only the profile JSON is dirty, which used to be ignored: an explicit
        # switch discarded it with no confirm, and a poll that dropped the run moved to the other repository.
        self.profiles = {
            'default': {'name': None, 'level': None},
            'profiles': [{
                'name': 'team', 'level': 'repo', 'shadowed': [], 'default': False,
                'body': {'policy': {'retry_failed': 1}},
            }],
        }
        page = self.page()
        page.wait_for_selector('#set-profile-body')
        page.fill('#set-profile-body', page.input_value('#set-profile-body').replace('"retry_failed": 1', '"retry_failed": 9', 1))
        self.choose(page, self.b)
        page.wait_for_selector('.set-picker .ui-confirm')
        self.assertIn('profile team', page.text_content('.set-picker .ui-confirm'))
        page.click('.set-picker .ui-confirm button:text-is("Cancel")')
        self.assertEqual(self.shown(page), self.a)
        self.assertIn('"retry_failed": 9', page.input_value('#set-profile-body'))
        self.hide.add(self.a)
        page.clock.fast_forward(6000)
        page.wait_for_function(
            "() => (document.querySelector('.set-picker')?.textContent || '').includes('unsaved edits are kept')",
            timeout=20000)
        self.assertEqual(self.shown(page), '')
        self.assertIn('"retry_failed": 9', page.input_value('#set-profile-body'))
        self.assertEqual(page.input_value('#set-f-verify'), 'make test\n')
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
