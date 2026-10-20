"""The page in a real browser (Chromium through Python Playwright, skipped without it).

Two ways in: with the CDN blocked, which is what an offline machine sees (the 2D views and the inspector
must still work, with no CSP violation), and with three.js loaded, which needs the CDN or a local copy of
the three@0.160.0 npm package named by FORGE_UI_TEST_THREE (its `package/` dir). Without either, the 3D
tests are skipped. Development-only: nothing here runs in forge itself.
"""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

from forge_ui import reader
from forge_ui.server import make_server
import run_fixture as F

THREE_LOCAL = os.environ.get('FORGE_UI_TEST_THREE')
CDN = 'https://cdn.jsdelivr.net/npm/three@0.160.0/'
ARGS = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']


@unittest.skipUnless(sync_playwright, 'install Python Playwright to run browser checks')
class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-browser-')
        base = Path(cls.temp.name)
        cls.plan, cls.solo = F.make_plan(base / 'p'), F.make_solo(base / 's')
        cls.key = reader.run_key(cls.plan)
        try:
            cls.server = make_server(reader.Catalog(plans=[cls.plan, cls.solo], registry=False), port=0, quiet=True)
        except PermissionError:
            cls.temp.cleanup()
            raise unittest.SkipTest('loopback sockets are blocked in this sandbox')
        cls.url = f'http://127.0.0.1:{cls.server.server_address[1]}/'
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(args=ARGS)
        except Exception as exc:  # no browser installed for this Playwright
            cls.playwright.stop(); cls.server.shutdown(); cls.server.server_close(); cls.temp.cleanup()
            raise unittest.SkipTest(f'Chromium is not available: {exc}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.playwright.stop()
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        cls.temp.cleanup()

    def open(self, cdn, view='3d', mode='plan'):
        context = self.browser.new_context(viewport={'width': 1440, 'height': 960})
        self.addCleanup(context.close)
        page = context.new_page()
        self.errors = []
        page.on('pageerror', lambda e: self.errors.append(str(e)))
        page.add_init_script(f"""
          window.__csp = [];
          document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
          try {{ localStorage.setItem('forge-ui-view', '"{view}"'); localStorage.setItem('forge-ui-mode', '"{mode}"'); }} catch (_) {{}}
        """)
        if not cdn:
            page.route(CDN + '**', lambda route: route.abort())
        elif THREE_LOCAL:
            def serve(route):
                rel = route.request.url[len(CDN):].split('?')[0]
                path = Path(THREE_LOCAL) / rel
                if path.is_file():
                    route.fulfill(status=200, content_type='text/javascript', body=path.read_bytes())
                else:
                    route.fulfill(status=404, body='')
            page.route(CDN + '**', serve)
        return page

    def three_or_skip(self, page):
        page.wait_for_function("() => window.ForgeMap3D && ['ready', 'unavailable'].includes(window.ForgeMap3D.state)", timeout=30000)
        if page.evaluate('window.ForgeMap3D.state') != 'ready':
            self.skipTest('three.js could not load: no CDN access and FORGE_UI_TEST_THREE is not set')
        page.wait_for_function("() => window.ForgeUI.state.stage && window.ForgeUI.state.stage.view", timeout=30000)

    def assertClean(self, page):
        self.assertEqual(page.evaluate('window.__csp'), [])
        self.assertEqual(self.errors, [])

    def test_without_the_cdn_the_2d_views_and_the_inspector_still_work(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/runs/' + self.key)
        page.wait_for_function("() => window.ForgeMap3D && window.ForgeMap3D.state === 'unavailable'", timeout=30000)
        page.wait_for_selector('#graph2d .fg-node')
        self.assertIn('3D view unavailable', page.text_content('.stage-note'))
        self.assertEqual(page.locator('#graph2d .fg-node').count(), 5)
        page.click('#graph2d .fg-node[data-id="reader"]')
        page.wait_for_selector('#inspector .attempt')
        text = page.text_content('#inspector')
        for expected in ('reader', 'Models', 'Timings', 'Attempts', 'Review findings', 'P1'):
            self.assertIn(expected, text)
        # the findings and the prompts come from the lazy endpoint
        page.wait_for_selector('#inspector .finding')
        page.click('[data-mode=exec]')
        page.wait_for_selector('#graph2d .gantt .gt-item')
        page.wait_for_selector('#strip .strip')
        self.assertTrue(page.evaluate("getComputedStyle(document.querySelector('#graph2d .gt-box')).fill") != 'none')
        page.goto(self.url + '#/runs/' + self.key + '/final-review')
        page.wait_for_function("() => document.querySelector('#inspector') && document.querySelector('#inspector').textContent.includes('Final review')")
        page.goto(self.url + '#/')
        page.wait_for_selector('.map-card')
        self.assertEqual(page.locator('.map-card').count(), 2)
        page.select_option('select[data-f=kind]', 'solo')
        page.wait_for_selector('.map-card.dim')
        self.assertEqual(page.locator('.map-card.dim').count(), 1)
        self.assertClean(page)

    def test_the_2d_graph_draws_status_colours_under_the_csp(self):
        page = self.open(cdn=False, view='2d')
        page.goto(self.url + '#/runs/' + self.key)
        page.wait_for_selector('#graph2d .fg-node')
        fills = page.evaluate("[...document.querySelectorAll('#graph2d .fg-bar')].map(e => getComputedStyle(e).fill)")
        self.assertTrue(fills and all(f not in ('none', 'rgb(0, 0, 0)') for f in fills), fills)
        self.assertGreater(len(set(fills)), 1, 'different statuses, different colours')
        self.assertClean(page)

    def test_the_3d_stage_shows_the_run_and_opens_a_task(self):
        page = self.open(cdn=True)
        page.goto(self.url + '#/runs/' + self.key)
        self.three_or_skip(page)
        page.wait_for_function("() => window.ForgeUI.state.stage.view === 'run'")
        nodes = dict(page.evaluate("window.ForgeUI.state.stage.debug().nodes.map(n => [n.id, n.status])"))
        self.assertEqual(nodes['t:reader'], 'MERGED')
        self.assertEqual(nodes['t:graph'], 'RUNNING')
        self.assertIn('f:final', nodes)
        page.evaluate("window.ForgeUI.state.stage.debug().settle()")
        page.wait_for_timeout(600)
        pos = page.evaluate("window.ForgeUI.state.stage.debug().screenPos('t:reader')")
        self.assertTrue(pos and pos['on'])
        page.mouse.click(pos['x'], pos['y'])
        page.wait_for_function("() => location.hash.endsWith('/tasks/reader')")
        page.wait_for_function("() => window.ForgeUI.state.stage.debug().prog.some(([id, p, t]) => id === 't:reader' && t === 1)")
        page.click('[data-mode=exec]')
        page.wait_for_function("() => window.ForgeUI.state.stage.mode === 'exec'")
        self.assertTrue(page.evaluate("window.ForgeUI.state.stage.debug().nodes.some(n => n.id.startsWith('x:task-'))"))
        self.assertClean(page)

    def test_a_status_change_on_disk_recolours_the_node_without_a_rebuild(self):
        page = self.open(cdn=True)
        page.goto(self.url + '#/runs/' + self.key)
        self.three_or_skip(page)
        page.wait_for_function("() => window.ForgeUI.state.stage.view === 'run'")
        before = page.evaluate("window.ForgeUI.state.stage.debug().rebuilds")
        task = self.plan / 'tasks' / 'graph'
        status = (task / 'status').read_text()
        self.addCleanup(lambda: ((task / 'merged').unlink(missing_ok=True), (task / 'status').write_text(status)))
        (task / 'status').write_text('PASS\n'); (task / 'merged').write_text('')
        page.wait_for_function("() => window.ForgeUI.state.stage.debug().nodes.find(n => n.id === 't:graph').status === 'MERGED'", timeout=15000)
        self.assertEqual(page.evaluate("window.ForgeUI.state.stage.debug().rebuilds"), before)
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
