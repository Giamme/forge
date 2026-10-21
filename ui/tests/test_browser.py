"""The page in a real browser (Chromium through Python Playwright, skipped without it).

Two ways in: with the CDN blocked, which is what an offline machine sees (the 2D views and the inspector
must still work, with no CSP violation), and with three.js loaded, which needs the CDN or a local copy of
the three@0.160.0 npm package named by FORGE_UI_TEST_THREE (its `package/` dir). Without either, the 3D
tests are skipped. Development-only: nothing here runs in forge itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
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

    def open(self, cdn, view='3d', mode='plan', init='', context=None, tasks=True):
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
        """ + ("try { if (!localStorage.getItem('forge-ui-shell')) localStorage.setItem('forge-ui-shell', '{\"tasks\":true}'); } catch (_) {}"   # past a first visit: the table open
               if tasks else '') + init)
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

    def steady(self, page, expr):
        """Wait until `expr` (a screen position) reads the same on two frames in a row: the camera has stopped."""
        page.evaluate('window.__steady = null')
        page.wait_for_function(f'() => {{ const now = JSON.stringify({expr}); const same = now === window.__steady; window.__steady = now; return same; }}')

    def frames(self, page, n=2):
        """Wait for n animation frames, so the stage has drawn what was just set."""
        page.evaluate('window.__frames = 0')
        page.wait_for_function(f'() => ++window.__frames > {n}')

    def test_without_the_cdn_the_2d_views_and_the_inspector_still_work(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/runs/' + self.key)
        page.wait_for_function("() => window.ForgeMap3D && window.ForgeMap3D.state === 'unavailable'", timeout=30000)
        page.wait_for_selector('#graph2d .fg-node')
        note = page.text_content('.stage-note')
        self.assertTrue(note.strip(), 'the stage says why there is no 3D view')
        self.assertEqual(note, page.evaluate('window.ForgeMap3D.message()'))
        self.assertEqual(page.locator('#graph2d .fg-node').count(), 5)
        # status colours come from style.css classes: the page CSP refuses style attributes (they drew black)
        fills = page.evaluate("[...document.querySelectorAll('#graph2d .fg-bar')].map(e => getComputedStyle(e).fill)")
        self.assertTrue(fills and all(f not in ('none', 'rgb(0, 0, 0)') for f in fills), fills)
        self.assertGreater(len(set(fills)), 1, 'different statuses, different colours')
        page.click('#graph2d .fg-node[data-id="reader"]')
        page.wait_for_selector('#inspector .attempt')
        self.assertTrue(page.text_content('#inspector .insp-head h2').startswith('reader'))
        # the task's models and timings, beside its attempts and findings (fixture values, not headings)
        values = page.eval_on_selector_all('#inspector .insp-section > dl.fields dd', 'ds => ds.map(d => d.textContent)')
        self.assertLessEqual({'sol:xhigh', 'opus', '20m00s', '18m38s'}, set(values), values)   # dwarf, reviewer; busy, model time
        # the findings and the prompts come from the lazy endpoint
        page.wait_for_selector('#inspector .finding')
        self.assertIn('P1', page.text_content('#inspector .findings'))
        page.click('[data-mode=exec]')
        page.wait_for_selector('#graph2d .gantt .gt-item')
        page.wait_for_selector('#strip .strip')
        self.assertTrue(page.evaluate("getComputedStyle(document.querySelector('#graph2d .gt-box')).fill") != 'none')
        page.goto(self.url + '#/runs/' + self.key + '/final-review')
        page.wait_for_selector('#inspector a.diff-link[href*="/final-review/"]')
        page.goto(self.url + '#/')
        page.wait_for_selector('.map-card')
        self.assertEqual(page.locator('.map-card').count(), 2)
        page.select_option('select[data-f=kind]', 'solo')
        page.wait_for_selector('.map-card.dim')
        self.assertEqual(page.locator('.map-card.dim').count(), 1)
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
        self.steady(page, "window.ForgeUI.state.stage.debug().screenPos('t:reader')")
        pos = page.evaluate("window.ForgeUI.state.stage.debug().screenPos('t:reader')")
        self.assertTrue(pos and pos['on'])
        page.mouse.click(pos['x'], pos['y'])
        page.wait_for_function("() => location.hash.endsWith('/tasks/reader')")
        page.wait_for_function("() => window.ForgeUI.state.stage.debug().prog.some(([id, p, t]) => id === 't:reader' && t === 1)")
        page.click('[data-mode=exec]')
        page.wait_for_function("() => window.ForgeUI.state.stage.mode === 'exec'")
        self.assertTrue(page.evaluate("window.ForgeUI.state.stage.debug().nodes.some(n => n.id.startsWith('x:task-'))"))
        self.assertClean(page)

    def test_orbiting_asks_for_one_frame_at_a_time(self):
        """While the camera moves, the stage asks for its next frame once (#6). Asked twice a frame,
        the copies doubled every frame and a second of orbiting drew hundreds of times."""
        count = """(() => {
          const raf = window.requestAnimationFrame.bind(window), waiting = new Map(); window.__copies = 0;
          window.requestAnimationFrame = cb => {
            const n = (waiting.get(cb) || 0) + 1; waiting.set(cb, n); window.__copies = Math.max(window.__copies, n);
            return raf(t => { const m = waiting.get(cb) - 1; if (m) waiting.set(cb, m); else waiting.delete(cb); cb(t); });
          };
        })();"""
        page = self.open(cdn=True, init=count)
        page.goto(self.url + '#/runs/' + self.key)
        self.three_or_skip(page)
        page.evaluate("window.ForgeUI.state.stage.debug().settle()")
        at = "window.ForgeUI.state.stage.debug().screenPos('t:reader')"
        self.steady(page, at)
        before = page.evaluate(at)
        box = page.locator('#stage-host canvas').bounding_box()
        x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 4
        page.mouse.move(x, y)
        page.mouse.down()
        for i in range(1, 4):
            page.mouse.move(x - 12 * i, y + 2 * i)
            self.frames(page)
        moved, copies = page.evaluate(at) != before, page.evaluate('window.__copies')
        page.mouse.up()
        self.assertTrue(moved, 'the drag orbited the camera')
        self.assertEqual(copies, 1, 'copies of the frame callback waiting at once')
        self.steady(page, at)                                           # and the camera comes to rest
        self.assertClean(page)

    def test_choosing_a_task_opens_the_inspector_and_a_module_key_folds_it_for_good(self):
        page = self.open(cdn=False, tasks=False)                        # a first visit: nothing remembered yet
        page.goto(self.url + '#/runs/' + self.key)
        page.wait_for_selector('#run-tasks table', state='attached')
        self.assertFalse(page.is_visible('#run-tasks table'))           # a run opens on its plate
        self.assertFalse(page.is_visible('#inspector'))
        page.keyboard.press('3')                                        # 3: the tasks
        page.click('#run-tasks td[data-col=id] a:text-is("reader")')
        page.wait_for_selector('#inspector .attempt')                   # what was chosen is read in the inspector
        page.keyboard.press('2')                                        # 2: the inspector
        page.wait_for_function("document.body.dataset.insp === 'closed'")
        self.assertFalse(page.is_visible('#inspector'))
        page.click('[data-shell-pick=workbench]')
        page.evaluate("location.hash = '#/runs/' + %r" % self.key)      # back to the run: nothing chosen to reopen it
        page.reload()
        page.wait_for_selector('#run-tasks table')
        self.assertEqual(page.evaluate('[document.body.dataset.shell, document.body.dataset.insp]'), ['workbench', 'closed'])
        self.assertFalse(page.is_visible('#inspector'))
        self.assertClean(page)

    def test_a_header_click_sorts_the_tasks_and_the_sort_is_remembered(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/runs/' + self.key)
        page.wait_for_selector('#run-tasks table')
        ids = lambda: page.eval_on_selector_all('#run-tasks tbody td[data-col=id]', 'ns => ns.map(n => n.textContent)')
        self.assertEqual(ids(), ['reader', 'page', 'graph', 'server', 'docs'])          # the plan's order
        page.click('#run-tasks th[data-col=id] .th-btn')
        page.click('#run-tasks th[data-col=id] .th-btn')
        self.assertEqual(ids(), ['server', 'reader', 'page', 'graph', 'docs'])
        self.assertEqual(page.get_attribute('#run-tasks th[data-col=id]', 'aria-sort'), 'descending')
        page.reload()
        page.wait_for_selector('#run-tasks table')
        self.assertEqual(ids(), ['server', 'reader', 'page', 'graph', 'docs'])
        self.assertClean(page)

    def test_the_theme_switch_overrides_the_system_and_is_remembered(self):
        page = self.open(cdn=False)                                     # the browser itself asks for light
        page.goto(self.url + '#/')
        page.wait_for_selector('[data-theme-pick=dark]')
        bg = lambda: page.evaluate('getComputedStyle(document.body).backgroundColor')
        light = bg()
        page.click('[data-theme-pick=dark]')
        self.assertEqual(page.get_attribute('html', 'data-theme'), 'dark')
        dark = bg()
        self.assertNotEqual(dark, light)
        page.reload()
        page.wait_for_selector('[data-theme-pick=dark][aria-pressed=true]')
        self.assertEqual(bg(), dark)
        page.click('[data-theme-pick=system]')
        self.assertIsNone(page.get_attribute('html', 'data-theme'))
        self.assertEqual(bg(), light)
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
        # the P1 sits right under new line 42; the finding on line 88, outside the hunks, heads its file
        self.assertEqual(page.evaluate("document.querySelector('.diff-pin .sev-P1').closest('.diff-pin').previousElementSibling.querySelectorAll('td.ln')[1].textContent"), '42')
        self.assertEqual(page.locator('.diff-file[data-path="ui/reader.py"] .diff-file-findings .diff-finding').count(), 1)
        self.assertFalse(page.is_visible('.stage-row'))
        page.click('.diff-close')
        page.wait_for_function("() => location.hash.endsWith('/tasks/reader') && document.getElementById('diff-view').hidden")
        self.assertTrue(page.is_visible('.stage-row'))
        self.assertClean(page)

    def test_an_attempts_conversation_shows_what_its_agent_said_and_ran(self):
        page = self.open(cdn=False)
        page.goto(self.url + f'#/runs/{self.key}/tasks/reader')
        link = f'#inspector .exec-line a[href$="/executions/{F.READER_DWARF1}/conversation"]'
        page.wait_for_selector(link)
        page.click(link)
        page.wait_for_selector('.convo-list .ev--command')
        self.assertFalse(page.is_visible('.stage-row'))
        # codex's log: its failed test run is the one error, and E goes to it
        self.assertEqual(page.locator('.convo-list .ev.is-error').count(), 1)
        page.keyboard.press('e')
        page.wait_for_function("() => document.activeElement.matches('.ev.is-error.ev--command')")
        # an edit links to its file, made relative, in the diff its review read
        self.assertEqual(page.get_attribute('.ev--edit a.ev-file', 'href'),
                         f'#/runs/{self.key}/tasks/reader/diff/{F.READER_DWARF1}/file/ui%2Freader.py')
        # / searches the conversation, not every run
        page.keyboard.press('/')
        page.wait_for_function("() => document.activeElement.matches('.convo-find input')")
        self.assertFalse(page.evaluate("!!document.querySelector('dialog[open]')"))
        page.fill('.convo-find input', 'pending')
        page.wait_for_function("() => [...document.querySelectorAll('.convo-list .ev')].filter(n => !n.hidden).map(n => n.dataset.kind).join() === 'message'")
        page.click('.diff-close')
        page.wait_for_function(f"() => location.hash.endsWith('/executions/{F.READER_DWARF1}') && document.getElementById('diff-view').hidden")
        self.assertClean(page)

    def test_a_running_reviewers_conversation_grows_with_its_log(self):
        log = self.plan / 'tasks/graph/qa.log'
        before = log.read_text()
        self.addCleanup(log.write_text, before)
        page = self.open(cdn=False)
        page.goto(self.url + f'#/runs/{self.key}/tasks/graph/executions/{F.GRAPH_QA}/conversation')
        page.wait_for_selector('.convo-list .ev--command')
        self.assertTrue(page.is_visible('.convo-follow'))
        kinds = "() => [...document.querySelectorAll('.convo-list .ev')].map(n => n.dataset.kind).join()"
        self.assertEqual(page.evaluate(kinds), 'reasoning,command')
        # the reviewer goes on: its log grows, the run dir does not change, the page reads it again
        log.write_text(''.join((F.TRANSCRIPTS / 'claude-stream.jsonl').read_text().splitlines(keepends=True)[:7]))
        page.wait_for_function(f"{kinds} === 'reasoning,command,edit,message'", timeout=10000)
        self.assertClean(page)

    def test_fractal_child_tasks_show_in_the_inspector_with_their_diffs(self):
        page = self.open(cdn=False)
        page.goto(self.url + f'#/runs/{self.key}/tasks/reader')
        page.wait_for_selector('#inspector .fractal-tree li')
        self.assertEqual(page.locator('#inspector .fractal-tree li').count(), 4)
        self.assertEqual(page.eval_on_selector_all('#inspector .fractal-tree .tag', 'ts => ts.map(t => (t.textContent.match(/\\d+/) || [])[0])'), ['3'])   # the one split, into 3
        page.click('#inspector .fractal-tree a:text-is("ui")')
        page.wait_for_function("() => location.hash.endsWith('/tasks/reader/fractal/ui')")
        node_diff = '#inspector .insp-head a.diff-link[href$="/tasks/reader/fractal/ui/diff"]'   # not the task view's link, still there until the hash is handled
        page.wait_for_selector(node_diff)
        self.assertIn('Add accessible status labels', page.text_content('#inspector'))
        page.click(node_diff)
        page.wait_for_selector('.diff-file')
        self.assertIn('candidate.diff', page.text_content('.diff-meta'))
        self.assertClean(page)

    def test_the_model_leaderboard_links_a_model_to_its_runs_in_the_cloud(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/models')
        page.wait_for_selector('.lb table')
        self.assertTrue(page.is_visible('.lb .lb-caveat[role=note]'))   # observational, not causal
        text = page.text_content('.lb')
        for model in ('luna', 'opus'):                                  # an implementer, a reviewer
            self.assertIn(model, text)
        page.click('.lb table a.lb-alias:has(.model-plate[title="luna"])')       # the model's name plate links to its runs
        page.wait_for_function("() => location.hash === '#/cloud'")
        page.wait_for_selector('.cloud-list .map-card')
        self.assertEqual(page.locator('.cloud-list .map-card').count(), 1)    # only the plan used luna
        self.assertClean(page)

    def test_the_export_dialog_leaves_private_texts_out_by_default(self):
        page = self.open(cdn=False)
        page.goto(self.url + '#/runs/' + self.key)
        page.click('.export-btn')
        page.wait_for_selector('dialog.export-dialog[open]')
        link = page.locator('dialog.export-dialog a[download]')
        self.assertTrue(link.get_attribute('href').endswith(f'/api/runs/{self.key}/export?texts=0&paths=0'))
        self.assertEqual(page.text_content('dialog.export-dialog .export-note'), '')
        page.click('dialog.export-dialog .export-opt:has-text("Include prompts") input')
        self.assertIn('texts=1', link.get_attribute('href'))
        self.assertNotEqual(page.text_content('dialog.export-dialog .export-note'), '')   # who may see it, once texts are in
        self.assertClean(page)

    def test_an_exported_run_opens_from_a_file_without_a_server(self):
        name, body = export.build(reader.Catalog(plans=[self.plan], registry=False), self.key)
        path = Path(self.temp.name) / name
        path.write_bytes(body)
        page = self.open(cdn=False)
        page.goto(path.as_uri())
        page.wait_for_function("() => window.ForgeMap3D && window.ForgeMap3D.state === 'unavailable'", timeout=30000)
        page.wait_for_selector('#graph2d .fg-node')
        self.assertIn('left out', page.text_content('.snapshot-banner'))   # README: the file states what it left out
        self.assertEqual(page.evaluate('location.hash'), '#/runs/' + self.key)
        page.click('#graph2d .fg-node[data-id="reader"]')
        page.wait_for_selector('#inspector .finding')
        self.assertIn('P1', page.text_content('#inspector'))
        self.assertClean(page)
        three = self.open(cdn=True)
        three.goto(path.as_uri())
        self.three_or_skip(three)
        three.wait_for_function("() => window.ForgeUI.state.stage.view === 'run'")
        self.assertClean(three)

    def test_the_files_heatmap_selects_tasks_and_opens_a_file_diff(self):
        page = self.open(cdn=False)
        page.goto(self.url + f'#/runs/{self.key}/tasks/reader')
        page.wait_for_selector('#inspector .insp-head')
        page.click('[data-mode=files]')
        page.wait_for_selector('.files-view .fm-defs', state='attached')
        self.assertFalse(page.is_visible('#graph2d'))
        text = page.text_content('.files-view')
        for path in ('ui/', 'graph.js', 'README.md'):
            self.assertIn(path, text)
        # under the matrix: docs drifted into README.md, graph conflicted on ui/graph.js
        self.assertEqual(page.eval_on_selector_all('.files-view .fm-list .fm-task', 'bs => bs.map(b => b.textContent)'), ['docs', 'graph'])
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
          window.Notification = class { constructor(t, o) { window.__notes.push(o.tag); } close() {} };
          Notification.permission = 'granted'; Notification.requestPermission = () => Promise.resolve('granted');
          document.hasFocus = () => false;
          try { localStorage.setItem('forge-ui-notify', JSON.stringify({ kinds: { run: true, task: true, review: true }, browser: true })); } catch (_) {}
        """
        page = self.open(cdn=False, init=stub)
        other = self.open(cdn=False, init=stub, context=page.context)
        for p in (page, other):
            p.goto(self.url + '#/')
            p.wait_for_selector('.map-card')                     # drawn after the first poll, which is only a baseline
        task = self.plan / 'tasks' / 'graph'
        status = (task / 'status').read_text()
        self.addCleanup(lambda: (task / 'status').write_text(status))
        (task / 'status').write_text('FAIL\n')
        tag = f'task|{self.key}|graph|FAIL'
        page.wait_for_function(f"() => JSON.parse(localStorage.getItem('forge-ui-notify-log') || '[]').some(x => x.id === '{tag}')", timeout=20000)
        for p in (page, other):                                 # each tab has polled the failure and decided what to tell
            p.wait_for_function("() => document.querySelectorAll('.map-card .led.st-fail').length === 2", timeout=20000)
            p.evaluate("() => navigator.locks.request('forge-ui-notify', () => {})")
        notes = page.evaluate('window.__notes') + other.evaluate('window.__notes')
        self.assertEqual(notes.count(tag), 1, notes)
        other.wait_for_function("() => document.title.startsWith('(')")
        self.assertTrue(page.evaluate("document.querySelector('link[rel=icon]').href.startsWith('data:image/png')"))
        page.click('#bell')
        page.wait_for_selector(f'.notify-pop .notify-item a[href="#/runs/{self.key}/tasks/graph"]')
        page.wait_for_function("() => !document.title.startsWith('(')")
        other.wait_for_function("() => !document.title.startsWith('(')", timeout=10000)
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

    def beacons(self, page):
        """The finding marks drawn now, once the camera and the marks have stopped, each with where it is on the screen."""
        page.wait_for_function("() => window.ForgeUI.state.stage.debug().beacons().some(b => b.shown)", timeout=15000)
        page.evaluate("window.ForgeUI.state.stage.debug().settle()")
        self.steady(page, "window.ForgeUI.state.stage.debug().beacons()")
        return [b for b in page.evaluate("window.ForgeUI.state.stage.debug().beacons()") if b['shown'] and b['on']]

    def test_a_finding_mark_opens_the_diff_at_its_finding(self):
        page = self.open(cdn=True)
        page.goto(self.url + '#/runs/' + self.key)
        self.three_or_skip(page)
        mark = next(b for b in self.beacons(page) if b['sev'] == 'P1')
        page.mouse.click(mark['x'], mark['y'])
        page.wait_for_function(f"() => decodeURIComponent(location.hash).endsWith('/finding/{mark['id']}')")
        page.wait_for_function(f"() => document.activeElement && document.activeElement.dataset.finding === {json.dumps(mark['id'])}")
        self.assertTrue(page.evaluate("document.activeElement.classList.contains('sev-P1')"))
        self.assertClean(page)

    def test_a_layer_switched_off_and_the_projection_stay_after_a_reload(self):
        context = self.browser.new_context(viewport={'width': 1440, 'height': 960})
        self.addCleanup(context.close)
        page = self.open(cdn=True, context=context)
        page.goto(self.url + '#/runs/' + self.key)
        self.three_or_skip(page)
        mark = self.beacons(page)[0]
        page.click('.view-btn')
        page.click('.layer-key[data-layer=beacons]')
        self.assertEqual(page.get_attribute('.layer-key[data-layer=beacons]', 'aria-pressed'), 'false')
        self.frames(page)
        self.assertIsNone(page.evaluate(f"window.ForgeUI.state.stage.debug().pickBeacon({mark['x']}, {mark['y']})"), 'a hidden mark is not picked')
        page.keyboard.press('Escape')
        page.keyboard.press('v')                                    # the stage's keys work wherever focus is
        page.wait_for_function("() => window.ForgeUI.state.stage.debug().projection().ortho")
        page.reload()
        self.three_or_skip(page)
        page.wait_for_function("() => window.ForgeUI.state.stage.view === 'run'")
        self.assertEqual(page.evaluate("window.ForgeUI.state.stage.debug().projection()"), {'proj': 'iso', 'ortho': True})
        self.assertFalse(page.evaluate("window.ForgeUI.state.stage.layers.beacons"))
        page.click('.view-btn')
        self.assertEqual(page.get_attribute('.layer-key[data-layer=beacons]', 'aria-pressed'), 'false')
        self.assertClean(page)

    def test_the_map_walks_its_runs_by_keyboard_and_enter_opens_one(self):
        page = self.open(cdn=True)
        page.goto(self.url + '#/')
        self.three_or_skip(page)
        page.wait_for_function("() => window.ForgeUI.state.stage.view === 'atlas'")
        page.focus('#stage-host canvas')
        page.keyboard.press('ArrowRight')
        page.wait_for_function("() => window.ForgeUI.state.stage.debug().sel && window.ForgeUI.state.stage.debug().sel.kind === 'run'")
        key = page.evaluate("window.ForgeUI.state.stage.debug().sel.key")
        self.assertNotEqual(page.text_content('#live'), '')        # where the walk is, said aloud
        page.keyboard.press('Enter')
        page.wait_for_function(f"() => location.hash === '#/runs/{key}'")
        self.assertClean(page)


@unittest.skipUnless(sync_playwright, 'install Python Playwright to run browser checks')
class Cloud(unittest.TestCase):
    """The run cloud (#/cloud) over many runs in several repos."""
    open, three_or_skip, assertClean, frames = Page.open, Page.three_or_skip, Page.assertClean, Page.frames

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-cloud-')
        base = Path(cls.temp.name)
        cls.runs = sorted(MF.make_many(base, 40, repos=3, live=1).iterdir())
        cls.solos = sum(1 for p in cls.runs if (p / 'prompt.md').is_file())
        cls.solo = next(reader.run_key(p) for p in cls.runs if (p / 'prompt.md').is_file())
        _serve(cls, reader.Catalog(plans=cls.runs, registry=False))

    tearDownClass = classmethod(Page.tearDownClass.__func__)

    def cloud(self, page, hash='#/cloud'):
        page.goto(self.url + hash)
        self.three_or_skip(page)
        page.wait_for_function("() => window.ForgeUI.state.stage.view === 'cloud' && window.ForgeUI.state.stage.cloud.debug().visible.length > 0", timeout=30000)
        page.evaluate("window.ForgeUI.state.stage.cloud.debug().settle()")
        self.frames(page)                                       # drawn settled: picking reads the drawn matrices

    def filter_to(self, page, selector, n):
        """Click a filter, wait for n runs to show and for them to move to the filtered layout, then settle it.
        The camera's re-frame, 650 ms later, is not waited for."""
        at = f"JSON.stringify(window.ForgeUI.state.stage.cloud.debug().screenPos('{self.solo}'))"   # a run shown either way
        before = page.evaluate(at)
        page.click(selector)
        page.wait_for_function(f"() => window.ForgeUI.state.stage.cloud.debug().visible.length === {n}")
        page.wait_for_function(f"() => {at} !== {json.dumps(before)}")
        page.evaluate("window.ForgeUI.state.stage.cloud.debug().settle()")
        self.frames(page)

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
        self.assertEqual(re.findall(r'\d+', page.text_content('.cloud-count')), [str(self.solos), str(len(self.runs))])
        self.assertClean(page)

    def test_the_cloud_filters_selects_and_opens_runs(self):
        page = self.open(cdn=True)
        self.cloud(page)
        self.assertEqual(self.visible(page), len(self.runs))
        self.filter_to(page, '.cloud-chip:has-text("solo")', self.solos)
        self.filter_to(page, '.cloud-panel-head button', len(self.runs))
        spot = page.evaluate("window.ForgeUI.state.stage.cloud.debug().clickable(340, 120, 1080, 780)")
        self.assertTrue(spot, 'no run is clickable in the middle of the stage')
        page.mouse.click(spot['x'], spot['y'])
        page.wait_for_function(f"() => location.hash === '#/cloud/runs/{spot['key']}'")
        page.wait_for_selector(f'.cloud-card a[href="#/runs/{spot["key"]}"]')
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


@unittest.skipUnless(sync_playwright, 'install Python Playwright to run browser checks')
class FirstRun(unittest.TestCase):
    """With no run at all (a fresh install), the map says how to get one; the first run that appears replaces it."""
    open, assertClean = Page.open, Page.assertClean

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='forge-ui-first-run-')
        cls.root = Path(cls.temp.name) / 'runs'; cls.root.mkdir()
        _serve(cls, reader.Catalog(roots=[cls.root], registry=False))

    tearDownClass = classmethod(Page.tearDownClass.__func__)

    def test_no_runs_shows_how_to_start_one_until_a_run_appears(self):
        page = self.open(cdn=False, view='2d')
        page.goto(self.url)
        page.wait_for_selector('#first-run')
        self.assertEqual(page.locator('#map-filters, #stage-slot').count(), 0)   # no filters, no plate over nothing
        commands = page.locator('#first-run pre').all_text_contents() + page.locator('#run-list pre').all_text_contents()
        self.assertTrue(any(c.startswith('/forge ') for c in commands), commands)
        self.assertTrue(any(c.startswith('forge ui open ') for c in commands), commands)
        self.assertFalse([c for c in commands if 'python3 -m forge_ui' in c], commands)   # works only from inside ui/
        F.make_plan(self.root / 'p')
        page.wait_for_selector('#map-filters', timeout=30000)
        self.assertEqual(page.locator('#first-run').count(), 0)
        self.assertClean(page)
