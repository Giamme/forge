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
import many_fixture as MF
import run_fixture as F

THREE_LOCAL = os.environ.get('FORGE_UI_TEST_THREE')
CDN = 'https://cdn.jsdelivr.net/npm/three@0.160.0/'
ARGS = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']
PW = {}                         # one Playwright and one browser for the module: the sync API starts only once per thread


def setUpModule():
    if sync_playwright is None:
        return
    PW['playwright'] = sync_playwright().start()
    try:
        PW['browser'] = PW['playwright'].chromium.launch(args=ARGS)
    except Exception as exc:  # no browser installed for this Playwright
        PW['error'] = str(exc)


def tearDownModule():
    if 'browser' in PW:
        PW['browser'].close()
    if 'playwright' in PW:
        PW['playwright'].stop()
    PW.clear()


def _serve(cls, catalog):
    try:
        cls.server = make_server(catalog, port=0, quiet=True)
    except PermissionError:
        cls.temp.cleanup()
        raise unittest.SkipTest('loopback sockets are blocked in this sandbox')
    if 'browser' not in PW:
        cls.server.server_close(); cls.temp.cleanup()
        raise unittest.SkipTest(f"Chromium is not available: {PW.get('error')}")
    cls.browser = PW['browser']
    cls.url = f'http://127.0.0.1:{cls.server.server_address[1]}/'
    cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
    cls.thread.start()


@unittest.skipUnless(sync_playwright, 'install Python Playwright to run browser checks')
class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-browser-')
        base = Path(cls.temp.name)
        cls.plan, cls.solo = F.make_plan(base / 'p'), F.make_solo(base / 's')
        cls.key = reader.run_key(cls.plan)
        _serve(cls, reader.Catalog(plans=[cls.plan, cls.solo], registry=False))

    @classmethod
    def tearDownClass(cls):
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


@unittest.skipUnless(sync_playwright, 'install Python Playwright to run browser checks')
class Cloud(unittest.TestCase):
    """The run cloud (#/cloud) over many runs in several repos."""
    open, three_or_skip, assertClean = Page.open, Page.three_or_skip, Page.assertClean

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-cloud-')
        base = Path(cls.temp.name)
        cls.runs = sorted(MF.make_many(base, 40, repos=3, live=1).iterdir())
        cls.solos = sum(1 for p in cls.runs if (p / 'prompt.md').is_file())
        _serve(cls, reader.Catalog(plans=cls.runs, registry=False))

    tearDownClass = classmethod(Page.tearDownClass.__func__)

    def cloud(self, page, hash='#/cloud'):
        page.goto(self.url + hash)
        self.three_or_skip(page)
        page.wait_for_function("() => window.ForgeUI.state.stage.view === 'cloud' && window.ForgeUI.state.stage.cloud.debug().visible.length > 0", timeout=30000)
        page.evaluate("window.ForgeUI.state.stage.cloud.debug().settle()")
        page.wait_for_timeout(500)

    def visible(self, page):
        return page.evaluate("window.ForgeUI.state.stage.cloud.debug().visible.length")

    def test_without_3d_the_cloud_lists_the_filtered_runs(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/cloud')
        page.wait_for_selector('.cloud-list .map-card')
        self.assertIn('cloud-mode', page.evaluate('document.body.className'))
        self.assertEqual(page.locator('.cloud-list .map-card').count(), len(self.runs))
        page.click('.cloud-chip:has-text("solo")')
        page.wait_for_function(f"() => document.querySelectorAll('.cloud-list .map-card').length === {self.solos}")
        self.assertIn(f'{self.solos} of {len(self.runs)} runs', page.text_content('.cloud-count'))
        self.assertClean(page)

    def test_the_cloud_filters_selects_and_opens_runs(self):
        page = self.open(cdn=True)
        self.cloud(page)
        self.assertEqual(self.visible(page), len(self.runs))
        page.click('.cloud-chip:has-text("solo")')
        page.wait_for_function(f"() => window.ForgeUI.state.stage.cloud.debug().visible.length === {self.solos}")
        page.click('.cloud-panel-head button')
        page.wait_for_function(f"() => window.ForgeUI.state.stage.cloud.debug().visible.length === {len(self.runs)}")
        page.wait_for_timeout(1700)                             # the camera re-frames once a filter settles
        page.evaluate("window.ForgeUI.state.stage.cloud.debug().settle()")
        page.wait_for_timeout(300)
        spot = page.evaluate("window.ForgeUI.state.stage.cloud.debug().clickable(340, 120, 1080, 780)")
        self.assertTrue(spot, 'no run is clickable in the middle of the stage')
        page.mouse.click(spot['x'], spot['y'])
        page.wait_for_function(f"() => location.hash === '#/cloud/runs/{spot['key']}'")
        page.wait_for_selector('.cloud-card h2')
        self.assertIn('Open run', page.text_content('.cloud-card'))
        page.mouse.dblclick(spot['x'], spot['y'])
        page.wait_for_function(f"() => location.hash === '#/runs/{spot['key']}' && window.ForgeUI.state.stage.view === 'run'", timeout=15000)
        page.go_back()
        page.wait_for_function("() => window.ForgeUI.state.stage.view === 'cloud'")
        page.click('[data-shape=orbs]')
        page.wait_for_function("() => window.ForgeUI.state.stage.cloud.debug().shape === 'orbs'")
        self.assertEqual(self.visible(page), len(self.runs))
        for name in ('sphere', 'galaxy', 'timeline', 'constellation'):
            page.click(f'[data-layout={name}]')
            page.wait_for_function(f"() => window.ForgeUI.state.stage.cloud.debug().layout === '{name}'")
        self.assertClean(page)

    def test_a_dragged_run_stays_where_it_was_dropped(self):
        page = self.open(cdn=True)
        self.cloud(page)
        spot = page.evaluate("window.ForgeUI.state.stage.cloud.debug().clickable(400, 200, 1000, 700)")
        self.assertTrue(spot)
        page.mouse.move(spot['x'], spot['y']); page.mouse.down()
        for i in range(1, 11):
            page.mouse.move(spot['x'] + 12 * i, spot['y'] - 5 * i)
        page.mouse.up()
        dropped = page.evaluate(f"window.ForgeUI.state.stage.cloud.debug().pos('{spot['key']}')")
        moved = page.evaluate(f"window.ForgeUI.state.stage.cloud.debug().screenPos('{spot['key']}')")
        self.assertLess(abs(moved['x'] - spot['x'] - 120) + abs(moved['y'] - spot['y'] + 50), 4)
        self.assertEqual(page.evaluate("window.ForgeUI.state.stage.cloud.debug().pinned"), [spot['key']])
        page.reload()
        self.cloud(page)
        again = page.evaluate(f"window.ForgeUI.state.stage.cloud.debug().pos('{spot['key']}')")
        for a, b in zip(again, dropped):
            self.assertAlmostEqual(a, b, delta=.1)
        # reset puts it back, and undo brings the drop back
        page.click('.cloud-menu summary'); page.click('text=Reset moved runs')
        page.wait_for_function("() => window.ForgeUI.state.stage.cloud.debug().pinned.length === 0")
        page.click('.cloud-toast button')
        page.wait_for_function("() => window.ForgeUI.state.stage.cloud.debug().pinned.length === 1")
        self.assertClean(page)


if __name__ == '__main__':
    unittest.main()
