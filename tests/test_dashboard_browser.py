"""Development-only browser regression suite; no provider calls or runtime dependency."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from dashboard_fixture import advance, make_run
from forge_fractal.inspection import capture, html, make_server
from forge_fractal import inspection


@unittest.skipUnless(sync_playwright, 'install Python Playwright to run browser checks')
class DashboardBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-dashboard-')
        cls.state = Path(cls.temp.name).resolve() / 'state'
        cls.run_dir = make_run(cls.state)
        cls.environment = patch.dict(os.environ, {'XDG_STATE_HOME': str(cls.state)})
        cls.environment.start()
        try:
            cls.server, cls.url = make_server('run-browser')
        except PermissionError:
            cls.environment.stop(); cls.temp.cleanup()
            raise unittest.SkipTest('loopback sockets are blocked in this sandbox')
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel='chrome')
        except Exception:
            cls.playwright.stop(); cls.server.shutdown(); cls.server.server_close()
            cls.environment.stop(); cls.temp.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.playwright.stop()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        cls.environment.stop(); cls.temp.cleanup()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 900})
        self.page = self.context.new_page()
        self.addCleanup(self.context.close)
        self.page.goto(self.url + '?run=run-browser')
        self.page.get_by_role('heading', name='run-browser', level=1).wait_for()

    def test_refresh_keeps_scroll_focus_selection_and_expansion(self):
        self.page.set_viewport_size({'width': 1440, 'height': 550})
        self.page.locator('.tree-row[data-key="node:task-alpha:implementation"] .tree-select').click()
        self.page.locator('#search').fill('implementation')
        self.page.get_by_role('button', name='Logs', exact=True).click()
        self.page.locator('#log-file').wait_for()
        self.page.locator('#log-output').wait_for()
        self.page.evaluate("document.querySelector('#detail').scrollTop=180; document.querySelector('#log-output').scrollTop=900; document.querySelector('#log-file').focus()")
        before = self.page.evaluate("({pane:document.querySelector('#detail').scrollTop, log:document.querySelector('#log-output').scrollTop})")
        self.assertGreater(before['pane'], 0)
        self.assertGreater(before['log'], 0)
        for _ in range(2):
            advance(self.run_dir)
            self.page.wait_for_timeout(2300)
        after = self.page.evaluate("({pane:document.querySelector('#detail').scrollTop, log:document.querySelector('#log-output').scrollTop, focus:document.activeElement.id, selected:document.querySelector('.tree-row.selected')?.dataset.key, expanded:document.querySelector('.tree-row[data-key=\"node:task-alpha:implementation\"] .tree-toggle').getAttribute('aria-expanded')})")
        self.assertEqual(after['pane'], before['pane'])
        self.assertEqual(after['log'], before['log'])
        self.assertEqual(after['focus'], 'log-file')
        self.assertEqual(after['selected'], 'node:task-alpha:implementation')
        self.assertEqual(after['expanded'], 'true')
        self.assertEqual(self.page.locator('#search').input_value(), 'implementation')
        self.page.get_by_role('button', name='Jump to latest').click()
        self.assertEqual(self.page.locator('#follow-logs').get_attribute('aria-pressed'), 'true')
        self.page.evaluate("document.querySelector('#log-output').scrollTop=0")
        self.page.wait_for_function("document.querySelector('#follow-logs').getAttribute('aria-pressed') === 'false'")
        self.page.get_by_role('button', name='Overview', exact=True).click()
        self.page.get_by_role('button', name='Pause live updates').click()
        frozen = self.page.locator('[data-bind=node-iteration]').inner_text()
        advance(self.run_dir); self.page.wait_for_timeout(2300)
        self.assertEqual(self.page.locator('[data-bind=node-iteration]').inner_text(), frozen)
        self.page.get_by_role('button', name='Resume live updates').click()
        self.page.wait_for_function("document.querySelector('[data-bind=node-iteration]').textContent !== " + json.dumps(frozen))

    def test_all_runs_deep_links_back_and_scoped_artifacts(self):
        seen = []
        self.page.on('request', lambda request: seen.append(request.url))
        self.page.get_by_role('link', name='All runs').click()
        self.page.get_by_role('heading', name='Managed runs').wait_for()
        self.page.get_by_role('link', name='run-browser').click()
        self.page.locator('.tree-row[data-key="node:task-alpha:implementation"]').wait_for()
        self.page.locator('#search').fill('accessible status')
        self.assertTrue(self.page.locator('.tree-row[data-key="node:task-alpha:implementation"]').is_visible())
        self.assertTrue(self.page.locator('.tree-row[data-key="node:task-alpha:ui"]').is_visible())
        self.page.locator('#search').fill('')
        self.page.locator('.tree-row[data-key="node:task-alpha:ui"] .tree-select').click()
        self.page.get_by_role('button', name='Changes', exact=True).click()
        self.page.get_by_text('+ui-new').wait_for()
        self.assertTrue(any('node=ui' in url and 'artifact=changes' in url for url in seen))
        self.assertFalse(any('node=api' in url and 'artifact=changes' in url for url in seen))
        self.page.go_back()
        self.page.get_by_role('heading', name='ui').wait_for()
        self.page.go_back()
        self.page.get_by_role('heading', name='run-browser', level=1).wait_for()
        self.page.goto(self.url + '?run=run-browser&task=task-alpha&node=ui&view=decomposition')
        self.page.get_by_role('heading', name='ui').wait_for()
        self.page.get_by_text('No decomposition decision').wait_for()

    def test_late_artifact_response_cannot_replace_new_selection(self):
        original = inspection.scoped
        entered, release = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            if kwargs.get('node_id') == 'ui' and kwargs.get('artifact') == 'changes':
                entered.set()
                release.wait(5)
            return original(*args, **kwargs)
        with patch.object(inspection, 'scoped', side_effect=delayed):
            self.page.locator('.tree-row[data-key="node:task-alpha:ui"] .tree-select').click()
            self.page.get_by_role('button', name='Changes', exact=True).click()
            self.assertTrue(entered.wait(3))
            self.page.locator('.tree-row[data-key="node:task-alpha:api"] .tree-select').click()
            self.page.get_by_role('button', name='Changes', exact=True).click()
            self.page.get_by_text('+api-new').wait_for()
            release.set()
            self.page.wait_for_timeout(250)
            self.assertFalse(self.page.get_by_text('+ui-new').count())

    def test_independent_task_history_pages(self):
        self.page.locator('.tree-row[data-key="task:task-alpha"] .tree-select').click()
        self.page.get_by_role('button', name='History', exact=True).click()
        self.page.get_by_role('button', name='Next').click()
        self.page.get_by_text('Rows 101–200').wait_for()
        self.page.locator('.tree-row[data-key="task:task-beta"] .tree-select').click()
        self.page.get_by_role('button', name='History', exact=True).click()
        self.page.get_by_text('Rows 1–100').wait_for()
        self.page.locator('.tree-row[data-key="task:task-alpha"] .tree-select').click()
        self.page.get_by_role('button', name='History', exact=True).click()
        self.page.get_by_text('Rows 101–200').wait_for()

    def test_failure_recovery_and_mobile_navigation(self):
        fail = {'once': True}
        def route_data(route):
            if fail['once'] and 'scope=progress' in route.request.url:
                fail['once'] = False; route.abort()
            else:
                route.continue_()
        self.page.route('**/data?**', route_data)
        self.page.wait_for_timeout(2300)
        self.page.get_by_text('Stale · reconnecting').wait_for()
        self.page.get_by_role('button', name='Retry').click()
        self.page.get_by_text('Live · updated', exact=False).wait_for()
        self.page.set_viewport_size({'width': 390, 'height': 760})
        self.page.locator('.tree-row[data-key="node:task-alpha:ui"] .tree-select').click()
        self.assertTrue(self.page.get_by_role('button', name='Back to navigator').is_visible())
        self.page.get_by_role('button', name='Back to navigator').click()
        self.assertTrue(self.page.locator('#search').is_visible())

    def test_empty_state_keyboard_focus_and_reduced_motion(self):
        self.page.goto(self.url + '?run=run-empty')
        self.page.get_by_text('No task execution has started.').wait_for()
        self.page.locator('#search').focus()
        self.assertEqual(self.page.evaluate("getComputedStyle(document.querySelector('#search')).outlineStyle"), 'solid')
        self.page.emulate_media(reduced_motion='reduce')
        self.assertTrue(self.page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))
        self.assertEqual(self.page.evaluate("getComputedStyle(document.querySelector('#pause-updates')).transitionDuration"), '0s')

    def test_offline_complete_history_and_hostile_artifacts(self):
        report = Path(self.temp.name) / 'report.html'
        report.write_text(html(capture(self.run_dir, logs=True, portable=True)))
        self.context.route('http://**/*', lambda route: route.abort())
        self.page.goto(report.as_uri())
        self.page.locator('.tree-row[data-key="task:task-alpha"] .tree-select').click()
        self.page.get_by_role('button', name='History', exact=True).click()
        self.page.get_by_role('button', name='Next').click()
        self.page.get_by_role('button', name='Next').click()
        self.page.get_by_text('Entry 249', exact=True).wait_for()
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))
        self.page.locator('.tree-row[data-key="node:task-alpha:implementation"] .tree-select').click()
        self.page.get_by_role('button', name='Logs', exact=True).click()
        self.page.get_by_text('log line 1199', exact=False).wait_for()
        self.assertFalse(self.page.evaluate('Boolean(window.injected)'))


if __name__ == '__main__':
    unittest.main()
