"""The README's pictures of forge-ui, made again from the made-up runs of demo_runs.py.

Needs Python Playwright with its Chromium, ImageMagick and the WebP tools (img2webp), and three.js:
from the CDN, or from a local copy of the three@0.160.0 npm package named by FORGE_UI_TEST_THREE
(its package/ dir).
Development-only, like the browser tests: nothing here runs in forge itself.

    python3 ui/tools/readme_media.py [OUT] [--only NAME ...]     default OUT: ui/docs/media
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from playwright.sync_api import sync_playwright  # noqa: E402

import demo_runs  # noqa: E402
from forge_ui import reader, server  # noqa: E402

CDN = 'https://cdn.jsdelivr.net/npm/three@0.160.0/'
THREE = os.environ.get('FORGE_UI_TEST_THREE')
ARGS = ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist']
VIEW = {'width': 1360, 'height': 860}
MOTION_VIEW = {'width': 1680, 'height': 940}    # a bigger stage for the animations
CLOUD_VIEW = {'width': 1280, 'height': 720}     # the cloud is costly to draw in software: fewer pixels, still over ANIM_WIDTH
WIDTH = 1600            # stills are taken at 2x and scaled to this width: sharp on a high-density screen
ANIM_WIDTH = 880
FPS = 10
# The animations run on a clock the script turns: performance.now() and requestAnimationFrame answer to
# window.__clock once it is frozen, so every frame is 1/FPS s after the last however slowly a
# software renderer draws it. Frames the browser had already asked for wait for the next step.
CLOCK = """(() => {
  const realNow = performance.now.bind(performance), realRaf = window.requestAnimationFrame.bind(window);
  const realCancel = window.cancelAnimationFrame.bind(window), queue = new Map(), real = new Map();
  let frozen = null, seq = 0;
  performance.now = () => frozen === null ? realNow() : frozen;
  window.requestAnimationFrame = cb => {
    const id = ++seq;
    if (frozen !== null) { queue.set(id, cb); return id; }
    real.set(id, realRaf(t => { real.delete(id); if (frozen === null) cb(t); else queue.set(id, cb); }));
    return id;
  };
  window.cancelAnimationFrame = id => { queue.delete(id); if (real.has(id)) { realCancel(real.get(id)); real.delete(id); } };
  window.__clock = {
    freeze() { frozen = realNow(); },
    step(ms) { frozen += ms; const due = [...queue.values()]; queue.clear(); for (const cb of due) cb(frozen); },
  };
})();"""


class Studio:
    def __init__(self, out, browser, url, key, catalog):
        self.out, self.browser, self.url, self.key, self.catalog = out, browser, url, key, catalog

    def page(self, motion=False, mode='plan', view=None, shell=None):
        """shell: which modules start open ({'runs', 'insp', 'tasks'}: true or false), Immersive layout."""
        ctx = self.browser.new_context(viewport=view or (MOTION_VIEW if motion else VIEW), device_scale_factor=1 if motion else 2,
                                       color_scheme='dark')
        page = ctx.new_page()
        if motion:
            page.add_init_script(CLOCK)
        page.on('pageerror', lambda e: print('page error:', e, file=sys.stderr))
        modules = {'shell': 'immersive', 'runs': False, 'insp': False, 'tasks': False, **(shell or {})}
        page.add_init_script("try { localStorage.setItem('forge-ui-view', '\"3d\"'); "
                             f"localStorage.setItem('forge-ui-mode', '\"{mode}\"'); "
                             f"localStorage.setItem('forge-ui-shell', '{json.dumps(modules)}'); }} catch (_) {{}}")
        if THREE:
            def serve(route):
                path = Path(THREE) / route.request.url[len(CDN):].split('?')[0]
                if path.is_file():
                    route.fulfill(status=200, content_type='text/javascript', body=path.read_bytes(),
                                  headers={'Access-Control-Allow-Origin': '*'})
                else:
                    route.fulfill(status=404, body='')
            page.route(CDN + '**', serve)
        return ctx, page

    def go(self, page, frag, stage=True):
        page.goto(self.url + frag)
        if stage:
            page.wait_for_function("() => window.ForgeMap3D && window.ForgeMap3D.state === 'ready'", timeout=60000)
            page.wait_for_function('() => window.ForgeUI.state.stage && window.ForgeUI.state.stage.view', timeout=60000)
        self.settle(page)

    @staticmethod
    def settle(page, ms=900):
        """Let tweens and the camera finish, asking for frames all along (headless Chromium can stop drawing a
        page whose only pending frame was asked for mid-frame), then wait for two quiet frames."""
        page.evaluate('ms => new Promise(r => { const t0 = performance.now(); const f = () => performance.now() - t0 > ms ? r() : requestAnimationFrame(f); f(); })', ms)
        page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')

    def still(self, page, name, clip):
        """A 2x screenshot of the clip, scaled to WIDTH and reduced to 256 colours: small, still sharp."""
        raw = self.out / f'.{name}.png'
        page.screenshot(path=str(raw), clip=clip, timeout=120000)
        dest = self.out / f'{name}.png'
        subprocess.run(['convert', str(raw), '-resize', f'{WIDTH}x>', '-strip', '-colors', '256', '+dither',
                        f'PNG8:{dest}'], check=True)
        raw.unlink()
        print(f'{dest.name}: {dest.stat().st_size // 1024} KB')

    def animation(self, frames, name, inset=8, pad=28):
        """PNG frames, 1/FPS s apart, as one looping animated WebP (a quarter of a GIF's size, in full
        colour), trimmed to what any frame shows: the stage's border and empty margins cut."""
        dest = self.out / f'{name}.webp'
        with tempfile.TemporaryDirectory() as tmp:
            for i, data in enumerate(frames):
                (Path(tmp) / f'{i:05d}.png').write_bytes(data)
            first = sorted(Path(tmp).glob('*.png'))
            size = subprocess.run(['identify', '-format', '%w %h', str(first[0])], check=True, capture_output=True, text=True).stdout
            width, height = (int(v) for v in size.split())
            box = subprocess.run(['convert', *map(str, first), '-evaluate-sequence', 'Max', '-shave', f'{inset}x{inset}', '-fuzz', '4%',
                                  '-trim', '-format', '%@', 'info:'], check=True, capture_output=True, text=True).stdout
            w, h, x, y = (int(v) for v in box.replace('x', '+').split('+'))
            x0, y0 = max(x + inset - pad, inset), max(y + inset - pad, inset)
            x1, y1 = min(x + inset + w + pad, width - inset), min(y + inset + h + pad, height - inset)
            cut = Path(tmp) / 'cut'
            cut.mkdir()
            subprocess.run(['mogrify', '-path', str(cut), '-crop', f'{x1 - x0}x{y1 - y0}+{x0}+{y0}', '+repage',
                            '-filter', 'Lanczos', '-resize', f'{ANIM_WIDTH}x>', *map(str, first)], check=True)
            subprocess.run(['img2webp', '-loop', '0', '-lossy', '-q', '75', '-m', '6', '-d', str(1000 // FPS),
                            *map(str, sorted(cut.glob('*.png'))), '-o', str(dest)], check=True, capture_output=True)
        print(f'{dest.name}: {dest.stat().st_size // 1024} KB, {len(frames)} frames')

    @staticmethod
    def box(page, *selectors, pad=0, bottom=None):
        """The union of the elements' boxes, in page pixels."""
        boxes = [page.locator(s).first.bounding_box() for s in selectors]
        x0, y0 = min(b['x'] for b in boxes) - pad, min(b['y'] for b in boxes) - pad
        x1, y1 = max(b['x'] + b['width'] for b in boxes) + pad, max(b['y'] + b['height'] for b in boxes) + pad
        if bottom is not None:
            y1 = min(y1, bottom)
        return dict(x=max(x0, 0), y=max(y0, 0), width=x1 - max(x0, 0), height=y1 - max(y0, 0))

    def zoom(self, page, presses):
        """Bring the camera closer with the stage's own key (+), which works wherever focus is."""
        for _ in range(presses):
            page.keyboard.press('+')
            page.wait_for_timeout(300)
        page.mouse.move(0, 0)
        self.settle(page, 1200)

    @staticmethod
    def window(page):
        """The whole window: the stage with the modules floating over it."""
        return dict(x=0, y=0, **page.viewport_size)

    # -- stills ----------------------------------------------------------------------------------

    def run(self):
        ctx, page = self.page(shell={'runs': True, 'insp': True})
        self.go(page, f'#/runs/{self.key}')
        self.settle(page, 1500)
        self.zoom(page, 1)
        self.still(page, 'run', self.window(page))
        ctx.close()

    def execution(self):
        ctx, page = self.page(mode='exec', shell={'insp': True})
        self.go(page, f'#/runs/{self.key}')
        self.settle(page, 1500)
        self.zoom(page, 2)
        self.still(page, 'execution', self.window(page))
        ctx.close()

    def files(self):
        ctx, page = self.page(mode='files')
        self.go(page, f'#/runs/{self.key}', stage=False)
        page.wait_for_selector('#files-view .fm-cell')
        self.settle(page)
        self.still(page, 'files', self.box(page, '#main', bottom=VIEW['height']))
        ctx.close()

    def diff(self):
        detail = server.api_get(self.catalog, f'/api/runs/{self.key}/tasks/token-model/diff')
        source = next(s['id'] for s in detail['sources'] if s.get('verdict') == 'FAIL')
        ctx, page = self.page()
        # opened at its first finding, as a finding's mark on the stage opens it: the diff scrolls to it
        self.go(page, f'#/runs/{self.key}/tasks/token-model/diff/{source}/finding/{source}%3A0', stage=False)
        page.wait_for_selector('#diff-view .diff-pin')
        self.settle(page, 2000)                                 # past the finding's flash
        page.evaluate('document.activeElement && document.activeElement.blur()')
        self.still(page, 'diff', self.box(page, '#diff-view', pad=8, bottom=VIEW['height'] - 4))
        ctx.close()

    def conversation(self):
        task = next(t for t in server.api_get(self.catalog, f'/api/runs/{self.key}')['tasks'] if t['id'] == 'token-model')
        dwarf = [e['id'] for e in task['executions'] if e['role'] == 'dwarf'][-1]   # the retry: a test fails, then the fix
        ctx, page = self.page()
        self.go(page, f'#/runs/{self.key}/tasks/token-model/executions/{dwarf}/conversation', stage=False)
        page.wait_for_selector('#diff-view .ev--command')
        self.settle(page)
        page.evaluate('document.activeElement && document.activeElement.blur()')
        # down to the passing test run, so no event is cut in half
        end = page.evaluate("() => [...document.querySelectorAll('#diff-view .ev--command')].pop().getBoundingClientRect().bottom")
        self.still(page, 'conversation', self.box(page, '#diff-view', pad=8, bottom=min(end, VIEW['height'] - 4)))
        ctx.close()

    def models(self):
        ctx, page = self.page()
        self.go(page, '#/models', stage=False)
        page.wait_for_selector('#models-view table')
        self.settle(page)
        self.still(page, 'models', self.box(page, '#models-view', pad=8, bottom=VIEW['height'] - 4))
        ctx.close()

    def map(self):
        ctx, page = self.page(shell={'runs': True})
        self.go(page, '#/')
        self.settle(page, 1500)
        page.mouse.move(0, 0)
        self.still(page, 'map', self.window(page))
        ctx.close()

    # -- animations ------------------------------------------------------------------------------

    @staticmethod
    def film(page, crop, seconds, act=None):
        """Turn the frozen clock 1/FPS s at a time, photographing the crop (PNG) after each turn.
        act(i), when given, runs before frame i: a mouse move, a click."""
        frames = []
        for i in range(round(seconds * FPS)):
            if act:
                act(i)
            page.evaluate(f'window.__clock.step({1000 / FPS})')
            frames.append(page.screenshot(clip=crop, timeout=120000))
            if (i + 1) % 10 == 0:
                print(f'  {i + 1} frames', flush=True)
        return frames

    def hero(self):
        """The quick-start run as planned, then as it really ran: retries in red, lanes on wall-clock time."""
        ctx, page = self.page(motion=True)
        self.go(page, f'#/runs/{self.key}')
        self.settle(page, 1500)
        self.zoom(page, 1)
        # the open stage only: below the run's header, above the tasks, left of the stage's keys
        slot, head = self.box(page, '#stage-slot'), page.locator('#run-head').bounding_box()
        tasks, keys = page.locator('.tasks-module').bounding_box(), page.locator('.stage-ctl').bounding_box()
        top, right = head['y'] + head['height'] + 8, keys['x'] - 8
        crop = dict(x=slot['x'], y=top, width=right - slot['x'], height=tasks['y'] - 8 - top)
        page.evaluate('window.__clock.freeze()')
        frames = self.film(page, crop, 1.5)
        page.click('[data-mode="exec"]')
        frames += self.film(page, crop, 3.5)
        page.click('[data-mode="plan"]')
        frames += self.film(page, crop, 3.0)
        ctx.close()
        self.animation(frames, 'hero')

    def cloud(self):
        """Every run at once: orbit the helix, then lay the cloud out as a galaxy and a timeline."""
        ctx, page = self.page(motion=True, view=CLOUD_VIEW)
        self.go(page, '#/cloud')
        page.wait_for_function('() => window.ForgeUI.state.stage.cloud && window.ForgeUI.state.stage.cloud.debug().visible.length > 0')
        page.get_by_role('button', name='Filters', exact=True).click()       # the panel folds away: all stage
        self.settle(page, 1800)
        crop = self.box(page, '#stage-slot')
        x, y = crop['x'] + crop['width'] * .5, crop['y'] + crop['height'] * .1     # empty sky: a drag here orbits
        page.evaluate('window.__clock.freeze()')
        page.mouse.move(x, y)
        page.mouse.down()
        frames = self.film(page, crop, 3.5, lambda i: page.mouse.move(x - i * 7, y + i * .4))
        page.mouse.up()
        frames += self.film(page, crop, 1.0)
        for layout in ('Galaxy', 'Timeline'):
            page.get_by_role('button', name=layout, exact=True).click()
            frames += self.film(page, crop, 2.8)
        ctx.close()
        self.animation(frames, 'cloud')


TAKES = ('hero', 'cloud', 'run', 'execution', 'diff', 'conversation', 'models', 'files', 'map')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('out', nargs='?', type=Path, default=HERE.parent / 'docs' / 'media')
    parser.add_argument('--only', nargs='+', choices=TAKES, default=TAKES, metavar='NAME', help=', '.join(TAKES))
    args = parser.parse_args(argv)
    for tool in ('img2webp', 'convert', 'mogrify', 'identify'):
        if not shutil.which(tool):
            parser.error(f'{tool} is not installed')
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='forge-ui-media-') as tmp:
        runs = demo_runs.build(Path(tmp) / 'runs')
        catalog = reader.Catalog(plans=runs, registry=False)
        srv = server.make_server(catalog, port=0, quiet=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(args=ARGS)
                studio = Studio(args.out, browser, f'http://127.0.0.1:{srv.server_address[1]}/', reader.run_key(runs[0]), catalog)
                for name in args.only:
                    getattr(studio, name)()
                browser.close()
        finally:
            srv.shutdown()
            srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
