/* forge-ui shell: Immersive (modules float over a full-bleed stage) or Workbench (modules docked, resized by
   splitters), which modules are open, and their sizes. Loaded at the top of <body> without defer, so the page's first
   paint already has the user's layout; the controls are wired once the document is parsed.
   State on <body>: data-shell="immersive|workbench", data-runs / data-insp / data-tasks="open|closed", and the sizes as
   --runs-w, --insp-w, --tasks-h. Keys 1, 2, 3 toggle the runs, inspector and tasks modules (digits need no Shift on US
   or Italian keyboards). On a first visit the inspector and the tasks start folded, so a run opens on its plate; choosing
   something opens the inspector (app.js), and once a module is opened or folded that choice is kept.
   Remembered in localStorage under forge-ui-shell.
   window.ForgeShell = { state, toggle(panel, open?), setShell(name), head(panel, title, count?), splitter(panel), onChange(fn) } */
window.ForgeShell = (() => {
  'use strict';
  const KEY = 'forge-ui-shell', body = document.body;
  const PANELS = { runs: { label: 'Runs', key: '1', icon: 'panel-left' }, insp: { label: 'Inspector', key: '2', icon: 'panel-right' }, tasks: { label: 'Tasks', key: '3', icon: 'panel-bottom' } };
  const SIZE = { runs: ['--runs-w', 220, 560, 300], insp: ['--insp-w', 300, 760, 380], tasks: ['--tasks-h', 120, 900, 208] };
  const read = () => { try { const v = JSON.parse(localStorage.getItem(KEY)); return v && typeof v === 'object' ? v : {}; } catch (_) { return {}; } };
  const saved = read();
  const narrow = window.matchMedia && window.matchMedia('(max-width: 760px)').matches;
  const state = {
    shell: saved.shell === 'workbench' ? 'workbench' : 'immersive',
    runs: typeof saved.runs === 'boolean' ? saved.runs : !narrow, insp: saved.insp === true, tasks: saved.tasks === true,
    size: { runs: num(saved.size && saved.size.runs, 'runs'), insp: num(saved.size && saved.size.insp, 'insp'), tasks: num(saved.size && saved.size.tasks, 'tasks') },
  };
  function num(v, panel) { const [, lo, hi, d] = SIZE[panel]; return typeof v === 'number' && Number.isFinite(v) ? Math.max(lo, Math.min(hi, Math.round(v))) : d; }
  const listeners = new Set();
  const icon = name => window.ForgeIcons ? window.ForgeIcons.icon(name) : document.createTextNode('');
  function save() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (_) { /* private mode: not remembered */ } }
  function apply() {
    body.dataset.shell = state.shell;
    for (const p of Object.keys(PANELS)) body.dataset[p] = state[p] ? 'open' : 'closed';
    for (const [p, [prop]] of Object.entries(SIZE)) body.style.setProperty(prop, state.size[p] + 'px');
    for (const b of document.querySelectorAll('[data-panel-toggle]')) {
      const p = b.dataset.panelToggle; b.setAttribute('aria-pressed', String(!!state[p]));
      b.title = `${state[p] ? 'Hide' : 'Show'} the ${PANELS[p].label.toLowerCase()} (${PANELS[p].key})`;
    }
    for (const b of document.querySelectorAll('[data-shell-pick]')) b.setAttribute('aria-pressed', String(b.dataset.shellPick === state.shell));
    for (const b of document.querySelectorAll('[data-theme-pick]')) b.setAttribute('aria-pressed', String(window.ForgeTheme && b.dataset.themePick === window.ForgeTheme.get()));
    for (const s of document.querySelectorAll('.splitter[data-panel]')) s.setAttribute('aria-valuenow', String(state.size[s.dataset.panel]));
  }
  function changed() { apply(); save(); for (const f of listeners) try { f(state); } catch (_) { /* a listener's own problem */ } }
  function toggle(panel, open) { if (!PANELS[panel]) return; state[panel] = open === undefined ? !state[panel] : !!open; changed(); }
  function setShell(name) { state.shell = name === 'workbench' ? 'workbench' : 'immersive'; changed(); }
  apply();

  function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
  function bank(label, cls) { const g = el('div', 'seg ' + cls); g.setAttribute('role', 'group'); g.setAttribute('aria-label', label); return g; }
  // a module's head: its plate, an optional count, room for tools, and the key that folds it away
  function head(panel, title, count) {
    const h = el('div', 'module-head');
    const plate = el('h2', 'module-title', title); h.append(plate);
    if (count !== undefined) h.append(el('span', 'module-count', count));
    const tools = el('span', 'module-tools');
    tools.append(foldButton(panel));
    h.append(tools);
    return h;
  }
  function foldButton(panel) {
    const b = el('button', 'icon-btn fold-btn'); b.type = 'button'; b.dataset.fold = panel;
    const sync = () => { b.replaceChildren(icon(state[panel] ? (panel === 'tasks' ? 'chevron-down' : 'close') : panel === 'tasks' ? 'chevron-up' : PANELS[panel].icon));
      b.setAttribute('aria-label', `${state[panel] ? 'Fold away' : 'Open'} the ${PANELS[panel].label.toLowerCase()} (${PANELS[panel].key})`); b.title = b.getAttribute('aria-label'); };
    sync(); listeners.add(() => { if (b.isConnected) sync(); });
    b.addEventListener('click', () => toggle(panel));
    return b;
  }
  // a splitter on a module's inner edge: drag it, or focus it and use the arrow keys
  function splitter(panel) {
    const [prop, lo, hi] = SIZE[panel], rows = panel === 'tasks';
    const s = el('div', 'splitter ' + (rows ? 'h' : 'v'));
    s.dataset.panel = panel; s.tabIndex = 0;
    s.setAttribute('role', 'separator'); s.setAttribute('aria-orientation', rows ? 'horizontal' : 'vertical');
    s.setAttribute('aria-label', `Resize the ${PANELS[panel].label.toLowerCase()}`);
    s.setAttribute('aria-valuemin', String(lo)); s.setAttribute('aria-valuemax', String(hi)); s.setAttribute('aria-valuenow', String(state.size[panel]));
    const set = v => { state.size[panel] = num(v, panel); body.style.setProperty(prop, state.size[panel] + 'px'); s.setAttribute('aria-valuenow', String(state.size[panel])); };
    let start = null;
    s.addEventListener('pointerdown', e => {
      if (e.button !== 0) return;
      e.preventDefault(); s.setPointerCapture(e.pointerId); s.classList.add('dragging'); body.classList.add('resizing'); body.classList.toggle('rows', rows);
      start = { x: e.clientX, y: e.clientY, v: state.size[panel] };
    });
    s.addEventListener('pointermove', e => {
      if (!start) return;
      const d = rows ? start.y - e.clientY : panel === 'insp' ? start.x - e.clientX : e.clientX - start.x;
      set(start.v + d);
    });
    const end = () => { if (!start) return; start = null; s.classList.remove('dragging'); body.classList.remove('resizing', 'rows'); changed(); };
    s.addEventListener('pointerup', end); s.addEventListener('pointercancel', end);
    s.addEventListener('keydown', e => {
      const grow = rows ? 'ArrowUp' : panel === 'insp' ? 'ArrowLeft' : 'ArrowRight', shrink = rows ? 'ArrowDown' : panel === 'insp' ? 'ArrowRight' : 'ArrowLeft';
      const step = e.shiftKey ? 64 : 16;
      if (e.key === grow) set(state.size[panel] + step); else if (e.key === shrink) set(state.size[panel] - step);
      else if (e.key === 'Home') set(lo); else if (e.key === 'End') set(hi); else return;
      e.preventDefault(); changed();
    });
    return s;
  }

  function wire() {
    const tools = document.querySelector('.desk-tools');
    if (tools) {
      const panels = bank('Modules', 'panel-bank');
      for (const [p, d] of Object.entries(PANELS)) {
        const b = el('button'); b.type = 'button'; b.dataset.panelToggle = p; b.dataset.key = d.key;
        b.append(icon(d.icon), el('span', 'panel-label', d.label)); panels.append(b);   // the label, or on a narrow desk the panel's picture
        b.addEventListener('click', () => toggle(p));
      }
      const shells = bank('Layout', 'icons shell-switch');
      for (const [name, label] of [['immersive', 'Immersive: modules float over the stage'], ['workbench', 'Workbench: modules docked beside the stage']]) {
        const b = el('button'); b.type = 'button'; b.dataset.shellPick = name; b.setAttribute('aria-label', label); b.title = label;
        b.append(icon(name)); b.addEventListener('click', () => setShell(name)); shells.append(b);
      }
      const themes = bank('Theme', 'icons theme-switch');
      for (const [name, label] of [['system', 'Theme: follow the system'], ['light', 'Theme: light'], ['dark', 'Theme: dark']]) {
        const b = el('button'); b.type = 'button'; b.dataset.themePick = name; b.setAttribute('aria-label', label); b.title = label;
        b.append(icon(name === 'light' ? 'sun' : name === 'dark' ? 'moon' : 'system'));
        b.addEventListener('click', () => { if (window.ForgeTheme) window.ForgeTheme.set(name); apply(); }); themes.append(b);
      }
      tools.prepend(panels, shells, themes);
    }
    const bell = document.getElementById('bell'); if (bell && !bell.querySelector('.icon')) bell.prepend(icon('bell'));
    const search = document.querySelector('.top-search-label'); if (search && !search.querySelector('.icon')) search.prepend(icon('search'));
    const side = document.querySelector('.sidebar');
    if (side) {
      const t = side.querySelector('.module-tools'); if (t) t.append(foldButton('runs'));
      side.append(splitter('runs'));
    }
    document.addEventListener('keydown', e => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.repeat) return;
      const t = e.target, typing = t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName));
      if (typing || document.querySelector('dialog[open]')) return;
      const p = Object.keys(PANELS).find(k => PANELS[k].key === e.key);
      if (p) { e.preventDefault(); toggle(p); }
    });
    apply();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire, { once: true }); else wire();

  return { state, toggle, setShell, head, splitter, onChange: f => listeners.add(f) };
})();
