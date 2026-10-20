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

from forge_ui import export, reader
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
        cls.xdg = os.environ.get('XDG_STATE_HOME')
        os.environ['XDG_STATE_HOME'] = str(base / 'state')      # where the reader task's Fractal tree lives
        F.make_fractal(base / 'state', cls.plan, 'reader')
        _serve(cls, reader.Catalog(plans=[cls.plan, cls.solo], registry=False))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)
        if getattr(cls, 'xdg', None) is None:
            os.environ.pop('XDG_STATE_HOME', None)
        else:
            os.environ['XDG_STATE_HOME'] = cls.xdg
        cls.temp.cleanup()

    def open(self, cdn, view='3d', mode='plan', init='', context=None):
        if context is None:
            context = self.browser.new_context(viewport={'width': 1440, 'height': 960})
            self.addCleanup(context.close)
        page = context.new_page()
        self.errors = []
        page.on('pageerror', lambda e: self.errors.append(str(e)))
        page.add_init_script(f"""
          window.__csp = [];
          document.addEventListener('securitypolicyviolation', e => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
          try {{ localStorage.setItem('forge-ui-view', '"{view}"'); localStorage.setItem('forge-ui-mode', '"{mode}"'); }} catch (_) {{}}
        """ + init)
        if not cdn:
            page.route(CDN + '**', lambda route: route.abort())
        elif THREE_LOCAL:
            def serve(route):
                rel = route.request.url[len(CDN):].split('?')[0]
                path = Path(THREE_LOCAL) / rel
                if path.is_file():   # as the CDN answers: readable from any origin, file:// pages included
                    route.fulfill(status=200, content_type='text/javascript', body=path.read_bytes(), headers={'Access-Control-Allow-Origin': '*'})
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

    def test_the_diff_viewer_pins_findings_to_their_lines(self):
        page = self.open(cdn=False)
        page.goto(self.url + f'#/runs/{self.key}/tasks/reader')
        page.wait_for_selector('#inspector .insp-head .diff-link')
        page.click('#inspector .insp-head .diff-link')
        page.wait_for_selector('.diff-file')
        page.click('.diff-source:has-text("attempt 1")')
        page.wait_for_function(f"() => location.hash.endsWith('/diff/{F.READER_QA1}')")
        page.wait_for_selector('.diff-pin .diff-finding.sev-P1')
        self.assertIn('line 42', page.text_content('.diff-pin'))
        page.wait_for_selector('.diff-file-findings:has-text("outside the changed lines")')
        self.assertFalse(page.is_visible('.stage-row'))
        page.click('.diff-close')
        page.wait_for_function("() => location.hash.endsWith('/tasks/reader') && document.getElementById('diff-view').hidden")
        self.assertTrue(page.is_visible('.stage-row'))
        self.assertClean(page)

    def test_fractal_child_tasks_show_in_the_inspector_with_their_diffs(self):
        page = self.open(cdn=False)
        page.goto(self.url + f'#/runs/{self.key}/tasks/reader')
        page.wait_for_selector('#inspector .fractal-tree li')
        self.assertEqual(page.locator('#inspector .fractal-tree li').count(), 4)
        self.assertIn('split into 3', page.text_content('#inspector .fractal-tree'))
        page.click('#inspector .fractal-tree a:text-is("ui")')
        page.wait_for_function("() => location.hash.endsWith('/tasks/reader/fractal/ui')")
        page.wait_for_selector('#inspector .insp-head .diff-link')
        self.assertIn('Add accessible status labels', page.text_content('#inspector'))
        page.click('#inspector .insp-head .diff-link')
        page.wait_for_selector('.diff-file')
        self.assertIn('candidate.diff', page.text_content('.diff-meta'))
        self.assertClean(page)

    def test_the_model_leaderboard_counts_first_attempts_and_links_to_the_cloud(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/models')
        page.wait_for_selector('.lb table')
        text = page.text_content('.lb')
        for expected in ('Observational', 'Implementers', 'Reviewers', 'luna', 'opus'):
            self.assertIn(expected, text)
        page.click('.lb table a:text-is("luna")')
        page.wait_for_function("() => location.hash === '#/cloud'")
        page.wait_for_selector('.cloud-filters-btn')
        self.assertIn('Filters (1)', page.text_content('.cloud-filters-btn'))
        self.assertClean(page)

    def test_the_export_dialog_leaves_private_texts_out_by_default(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/runs/' + self.key)
        page.click('.export-btn')
        page.wait_for_selector('dialog.export-dialog[open]')
        link = page.locator('dialog.export-dialog a[download]')
        self.assertTrue(link.get_attribute('href').endswith(f'/api/runs/{self.key}/export?texts=0&paths=0'))
        page.click('dialog.export-dialog .export-opt:has-text("Include prompts") input')
        self.assertIn('texts=1', link.get_attribute('href'))
        self.assertIn('Share this file only', page.text_content('dialog.export-dialog'))
        with page.expect_download() as dl:
            link.click()
        self.assertRegex(dl.value.suggested_filename, r'^forge-run-.+\.html$')
        self.assertClean(page)

    def test_an_exported_run_opens_from_a_file_without_a_server(self):
        name, body = export.build(reader.Catalog(plans=[self.plan], registry=False), self.key)
        path = Path(self.temp.name) / name
        path.write_bytes(body)
        page = self.open(cdn=False)
        page.goto(path.as_uri())
        page.wait_for_function("() => window.ForgeMap3D && window.ForgeMap3D.state === 'unavailable'", timeout=30000)
        page.wait_for_selector('#graph2d .fg-node')
        self.assertIn('left out', page.text_content('.snapshot-banner'))
        self.assertEqual(page.evaluate('location.hash'), '#/runs/' + self.key)
        page.click('#graph2d .fg-node[data-id="reader"]')
        page.wait_for_selector('#inspector .finding')
        self.assertIn('P1', page.text_content('#inspector'))
        self.assertEqual(page.evaluate('window.__csp'), [])
        three = self.open(cdn=True)
        three.goto(path.as_uri())
        self.three_or_skip(three)
        three.wait_for_function("() => window.ForgeUI.state.stage.view === 'run'")
        self.assertEqual(three.evaluate('window.__csp'), [])

    def test_the_files_heatmap_selects_tasks_and_opens_a_file_diff(self):
        page = self.open(cdn=False)
        page.goto(self.url + f'#/runs/{self.key}/tasks/reader')
        page.wait_for_selector('#inspector .insp-head')
        page.click('[data-mode=files]')
        page.wait_for_selector('.files-view .fm-defs', state='attached')
        self.assertFalse(page.is_visible('#graph2d'))
        text = page.text_content('.files-view')
        for expected in ('ui/', 'graph.js', 'README.md', 'MERGE CONFLICTS', 'DRIFT'):
            self.assertIn(expected.lower(), text.lower())
        page.focus('.files-view [data-fm-focus^="cell:reader"]')
        page.keyboard.press('ArrowDown')
        page.keyboard.press('Enter')
        page.wait_for_selector('.files-view .fm-pop')
        page.click('.files-view .fm-pop')
        page.wait_for_function("() => location.hash.endsWith('/tasks/reader/diff/file/tests%2Ftest_reader.py')")
        page.wait_for_selector('.diff-file[data-path="tests/test_reader.py"]')
        self.assertClean(page)

    def test_quick_jump_finds_a_task_and_opens_it(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/')
        page.wait_for_selector('.map-card')
        page.keyboard.press('Control+k')
        page.wait_for_selector('dialog.jump[open]')
        page.keyboard.type('reader')
        page.wait_for_selector('.jump-item[aria-selected=true]')
        self.assertTrue(page.text_content('.jump-item[aria-selected=true] .jump-label').startswith('reader'))
        page.keyboard.press('Enter')
        page.wait_for_function("() => location.hash.endsWith('/tasks/reader')")
        self.assertFalse(page.is_visible('dialog.jump'))
        page.click('.run-header h2')
        page.keyboard.press('/')
        page.wait_for_selector('dialog.jump[open]')
        self.assertIn('reader', page.text_content('.jump-item[aria-selected=true]'), 'a recent pick comes first')
        page.keyboard.type('cloud')
        page.keyboard.press('Enter')
        page.wait_for_function("() => location.hash === '#/cloud'")
        self.assertClean(page)

    def test_a_failing_task_is_notified_once_across_tabs(self):
        stub = """
          window.__notes = [];
          window.Notification = class { constructor(t, o) { window.__notes.push(t + ' | ' + o.body); } close() {} };
          Notification.permission = 'granted'; Notification.requestPermission = () => Promise.resolve('granted');
          document.hasFocus = () => false;
          try { localStorage.setItem('forge-ui-notify', JSON.stringify({ kinds: { run: true, task: true, review: true }, browser: true })); } catch (_) {}
        """
        page = self.open(cdn=False, init=stub)
        other = self.open(cdn=False, init=stub, context=page.context)
        for p in (page, other):
            p.goto(self.url + '#/')
            p.wait_for_selector('.map-card')
        page.wait_for_timeout(2500)                             # the first poll is only a baseline
        task = self.plan / 'tasks' / 'graph'
        status = (task / 'status').read_text()
        self.addCleanup(lambda: (task / 'status').write_text(status))
        (task / 'status').write_text('FAIL\n')
        page.wait_for_function("() => window.__notes.some(n => n.startsWith('Task graph fail'))", timeout=20000)
        other.wait_for_function("() => document.title.startsWith('(')", timeout=20000)
        page.wait_for_timeout(2500)
        notes = page.evaluate('window.__notes') + other.evaluate('window.__notes')
        self.assertEqual(sum(n.startswith('Task graph fail') for n in notes), 1, notes)
        self.assertTrue(page.evaluate("document.querySelector('link[rel=icon]').href.startsWith('data:image/png')"))
        page.click('#bell')
        page.wait_for_selector('.notify-pop .notify-item')
        self.assertIn('Task graph fail', page.text_content('.notify-pop'))
        page.wait_for_function("() => document.title === 'forge-ui'")
        other.wait_for_function("() => document.title === 'forge-ui'", timeout=10000)
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
