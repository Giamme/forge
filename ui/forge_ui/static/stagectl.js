/* forge-ui stage controls: the bank of keys over the 3D stage and the keyboard behind it. Fit (F), zoom (+ −),
   projection (V: perspective, true isometric, top-down), reset (0), full screen, the legend of what the stage
   draws now (L), the route (I) and the sheet of every key (?). scene3d.js says what each key does and what the
   legend holds (ctl); this module only draws the bank, the legend and the sheet, with createElement and
   textContent like the rest of the page. The keys work wherever focus is, while the stage is on screen and
   nothing is being typed. */
export const PROJECTIONS = [['persp', 'Perspective', 'persp'], ['iso', 'Isometric', 'iso'], ['top', 'Top-down', 'top']];
// the sheet: [group, [[keys, what]]]
const SHEET = [
  ['Modules', [['1', 'Runs'], ['2', 'Inspector'], ['3', 'Tasks']]],
  ['Stage', [['F', 'Fit what is shown'], ['+ −', 'Zoom in, out'], ['V', 'Perspective, isometric, top-down'], ['0', 'Reset the view'],
    ['L', 'Legend'], ['I', 'Route: keep a chosen task’s route lit'], ['Shift F', 'Full screen'], ['?', 'These keys']]],
  ['With the stage focused', [['← →', 'Previous, next task (on the map: run)'], ['↑ ↓', 'On the map: previous, next repository'], ['Enter', 'On the map: open the run'],
    ['E', 'Plan or execution'], ['Esc', 'Clear the selection, then back to the map']]],
  ['In a diff', [['n p', 'Next, previous finding'], ['j k', 'Next, previous file']]],
  ['In a conversation', [['/', 'Search what it said and ran'], ['E', 'Next error']]],
];

const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
const icon = name => window.ForgeIcons ? window.ForgeIcons.icon(name) : el('span');
const typing = t => t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));

// ctl: { view() → 'run'|'atlas'|'cloud'|null, fit(), reset(), zoom(k), projection, setProjection(name),
//        legend() → [{title, note?, rows: [{sw, label}]}], toggleRoute() }
export function createControls(host, ctl) {
  const bank = el('div', 'stage-ctl'); bank.setAttribute('role', 'toolbar'); bank.setAttribute('aria-label', 'Stage view'); bank.setAttribute('aria-orientation', 'vertical');
  const key = (name, label, keys, fn, ico = name) => {
    const b = el('button', 'icon-btn'); b.type = 'button'; b.dataset.ctl = name;
    b.setAttribute('aria-label', label); b.title = `${label} (${keys})`; b.setAttribute('aria-keyshortcuts', keys);
    b.append(icon(ico)); b.addEventListener('click', fn); return b;
  };
  // momentary keys (fit, zoom, reset) have no lamp; a latching one lights while it is in (the projection bank, legend, full screen)
  const latch = b => { b.classList.add('latch'); b.setAttribute('aria-pressed', 'false'); return b; };
  const projs = el('div', 'stage-ctl-group'); projs.setAttribute('role', 'group'); projs.setAttribute('aria-label', 'Projection (V)');
  for (const [name, label, ico] of PROJECTIONS) projs.append(latch(key('proj-' + name, label, 'V', () => { ctl.setProjection(name); sync(); }, ico)));
  const full = latch(key('fullscreen', 'Full screen', 'Shift+F', () => fullscreen(), 'fullscreen'));
  const leg = latch(key('legend', 'Legend', 'L', () => legend(), 'legend'));
  leg.setAttribute('aria-expanded', 'false');
  bank.append(
    key('fit', 'Fit what is shown', 'F', () => ctl.fit()),
    key('zoom-in', 'Zoom in', '+', () => ctl.zoom(1.35)),
    key('zoom-out', 'Zoom out', '-', () => ctl.zoom(1 / 1.35)),
    key('reset', 'Reset the view', '0', () => { ctl.reset(); sync(); }),
    el('span', 'stage-ctl-rule'), projs, el('span', 'stage-ctl-rule'),
    leg, full,
    key('keys', 'Keys', '?', () => sheet(), 'keyboard'),
  );
  if (!document.fullscreenEnabled) full.hidden = true;
  host.append(bank);

  // the legend: what the stage draws now, rebuilt each time it opens and when the view changes under it
  const panel = el('section', 'stage-legend'); panel.id = 'stage-legend'; panel.hidden = true; panel.setAttribute('aria-label', 'Legend');
  leg.setAttribute('aria-controls', panel.id);
  host.append(panel);
  function fillLegend() {
    const head = el('div', 'stage-legend-head'); head.append(el('h3', 'plate', 'Legend'));
    const x = el('button', 'icon-btn'); x.type = 'button'; x.setAttribute('aria-label', 'Close the legend'); x.append(icon('close')); x.addEventListener('click', () => legend(false));
    head.append(x);
    const body = [head];
    for (const sec of ctl.legend()) {
      const s = el('div', 'lg-sec'); s.append(el('h4', 'plate lg-title', sec.title));
      for (const r of sec.rows) { const row = el('div', 'lg-row'); row.append(swatch(r.sw), el('span', 'lg-label', r.label)); s.append(row); }
      if (sec.note) s.append(el('p', 'lg-note', sec.note));
      body.push(s);
    }
    panel.replaceChildren(...body);
  }
  function swatch(sw = {}) {
    const s = el('span', 'lg-sw');
    if (sw.lamp) { s.classList.add('lg-lamp'); s.append(el('i', 'lamp ' + sw.lamp)); }
    else if (sw.route) { s.classList.add('lg-lamp'); s.append(el('i', 'lamp lamp-route')); }
    else if (sw.sev) { s.classList.add('lg-sev', 'sev-' + sw.sev); if (sw.pale) s.classList.add('pale'); }
    else if (sw.cable) s.classList.add('lg-cable', 'cable-' + sw.cable);
    else if (sw.bead) s.classList.add('lg-bead');
    else if (sw.ring) s.classList.add('lg-ring');
    else if (sw.color) { s.classList.add('lg-color'); s.style.setProperty('--sw', sw.color); }
    else if (sw.scale) { s.classList.add('lg-scale'); s.append(el('span', 'lg-min', sw.scale[0]), el('span', 'lg-bar'), el('span', 'lg-max', sw.scale[1])); }
    else if (sw.height) { s.classList.add('lg-height'); s.append(el('i'), el('i'), el('i')); }
    return s;
  }
  function legend(open = panel.hidden) {
    if (open) fillLegend();
    panel.hidden = !open; leg.setAttribute('aria-expanded', String(open)); leg.setAttribute('aria-pressed', String(open));
  }

  // the projection in use is the bank's lit key
  function sync() {
    for (const b of projs.children) b.setAttribute('aria-pressed', String(b.dataset.ctl === 'proj-' + ctl.projection));
    full.setAttribute('aria-pressed', String(document.fullscreenElement === host));
    const v = ctl.view(); bank.hidden = v !== 'run' && v !== 'atlas';
    if (bank.hidden && !panel.hidden) legend(false);
    else if (!panel.hidden) fillLegend();
  }
  function cycle() {
    const i = PROJECTIONS.findIndex(x => x[0] === ctl.projection);
    ctl.setProjection(PROJECTIONS[(i + 1) % PROJECTIONS.length][0]); sync();
  }
  function fullscreen() {
    if (document.fullscreenElement === host) document.exitFullscreen();
    else if (host.requestFullscreen) host.requestFullscreen().catch(() => {});
  }
  const onFull = () => sync();                             // the engine refits a framing the user has not moved
  document.addEventListener('fullscreenchange', onFull);

  // the sheet of every key, a dialog made the first time it opens
  let dlg = null;
  function sheet() {
    if (!dlg) {
      dlg = el('dialog', 'keys-sheet'); dlg.setAttribute('aria-label', 'Keys');
      const head = el('div', 'keys-head'); head.append(el('span', 'plate', 'Keys'));
      const x = el('button', 'icon-btn'); x.type = 'button'; x.setAttribute('aria-label', 'Close'); x.append(icon('close')); x.addEventListener('click', () => dlg.close());
      head.append(x); dlg.append(head);
      const grid = el('div', 'keys-grid');
      for (const [title, rows] of SHEET) {
        const g = el('section', 'keys-group'); g.append(el('h4', 'plate lg-title', title));
        const dl = el('dl');
        for (const [k, what] of rows) { const dt = el('dt'); for (const part of k.split(' ')) dt.append(el('kbd', null, part)); dl.append(dt, el('dd', null, what)); }
        g.append(dl); grid.append(g);
      }
      dlg.append(grid);
      dlg.addEventListener('click', e => { if (e.target === dlg) dlg.close(); });   // a click on the backdrop
      document.body.append(dlg);                            // a modal dialog is drawn over a full-screen stage too
    }
    if (!dlg.open) dlg.showModal();
  }

  // the keys: anywhere on the page while the stage is on screen and nothing is being typed
  const shown = () => host.isConnected && !host.hidden && host.getClientRects().length > 0 && !bank.hidden;
  function onKey(e) {
    if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return;
    if (document.querySelector('dialog[open]') && !(dlg && dlg.open)) return;
    if (!shown()) return;
    const k = e.key;
    if (dlg && dlg.open) return;
    if (k === '?') { e.preventDefault(); sheet(); return; }
    if (e.repeat && !['+', '=', '-', '_'].includes(k)) return;
    if (k === 'F' && e.shiftKey) { e.preventDefault(); fullscreen(); return; }
    const act = { f: () => ctl.fit(), '+': () => ctl.zoom(1.35), '=': () => ctl.zoom(1.35), '-': () => ctl.zoom(1 / 1.35), _: () => ctl.zoom(1 / 1.35),
      v: () => cycle(), 0: () => { ctl.reset(); sync(); }, l: () => legend(), i: () => { ctl.toggleRoute(); if (!panel.hidden) fillLegend(); } }[k.length === 1 ? k.toLowerCase() : k];
    if (!act) return;
    e.preventDefault(); act();
  }
  document.addEventListener('keydown', onKey);
  sync();
  return {
    sync, bank, legend: panel,
    get legendOpen() { return !panel.hidden; },
    dispose() { document.removeEventListener('keydown', onKey); document.removeEventListener('fullscreenchange', onFull); bank.remove(); panel.remove(); if (dlg) dlg.remove(); },
  };
}
