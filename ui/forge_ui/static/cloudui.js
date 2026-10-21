/* forge-ui run cloud controls: the filter panel, the layout and shape switches, the position tools, the
   legend and the run card, laid over the full-screen cloud (cloud3d.js); and, when 3D is off, the same
   filtered runs as a list. All API values enter the DOM as text.
   window.ForgeCloudUI = { create(root, ctx) }, where ctx = { prefs, save(prefs), openRun(key), route({sel, focus}),
   load3d(), viewToggle() }. */
window.ForgeCloudUI = (() => {
  'use strict';
  const CM = window.ForgeCloudModel, RM = window.ForgeRunModel;
  const STATES = [['live', 'running', 'running'], ['failed', 'has failures', 'fail'], ['done', 'all done', 'merged'], ['pending', 'not started', 'pending'], ['stale', 'stalled', 'interrupted']];
  const STATE = Object.fromEntries(STATES.map(([k, label, st]) => [k, { label, st }]));
  const LIST_CAP = 300;
  const list = v => Array.isArray(v) ? v : [];
  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  function button(text, cls, onClick, title) {
    const b = el('button', text, cls); b.type = 'button';
    if (title) b.title = title;
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }
  const when = t => Number.isFinite(Number(t)) && t ? new Date(Number(t) * 1000).toLocaleString() : '—';

  function create(root, ctx) {
    const prefs = {
      filter: CM.normFilter(ctx.prefs && ctx.prefs.filter),
      layout: CM.LAYOUTS.includes(ctx.prefs && ctx.prefs.layout) ? ctx.prefs.layout : 'helix',
      shape: ctx.prefs && ctx.prefs.shape === 'orbs' ? 'orbs' : 'blocks',
      panel: !(ctx.prefs && ctx.prefs.panel === false),
    };
    const ui = el('div', undefined, 'cloud-ui');
    const bar = el('div', undefined, 'cloud-bar'); bar.setAttribute('role', 'toolbar'); bar.setAttribute('aria-label', 'Cloud');
    const panel = el('form', undefined, 'cloud-panel'); panel.id = 'cloud-panel'; panel.setAttribute('aria-label', 'Filter the runs in the cloud');
    panel.addEventListener('submit', e => e.preventDefault());
    const card = el('aside', undefined, 'cloud-card'); card.setAttribute('aria-label', 'Selected run'); card.hidden = true;
    const legend = el('div', undefined, 'cloud-legend');
    const toast = el('div', undefined, 'cloud-toast'); toast.setAttribute('role', 'status'); toast.hidden = true;
    const listHost = el('div', undefined, 'cloud-list'); listHost.hidden = true;
    ui.append(bar, panel, card, legend, toast);
    root.append(ui, listHost);
    const file = el('input'); file.type = 'file'; file.accept = 'application/json,.json'; file.hidden = true; ui.append(file);

    const S = { runs: [], facts: [], byKey: new Map(), sel: null, focus: null, mode: '2d', stage: null, shown: false, optSig: '', runsRef: null, toastTimer: 0 };
    const cloud = () => S.mode === '3d' && S.stage ? S.stage.cloud : null;
    const save = () => ctx.save && ctx.save({ filter: prefs.filter, layout: prefs.layout, shape: prefs.shape, panel: prefs.panel });
    const nowSec = () => Date.now() / 1000;

    /* ---------- the bar: counts, layout, shape, positions, view ---------- */
    const count = el('span', '', 'cloud-count'); count.setAttribute('aria-live', 'polite');
    const filtersBtn = button('Filters', 'cloud-filters-btn', () => { prefs.panel = !prefs.panel; save(); syncBar(); });
    filtersBtn.setAttribute('aria-controls', 'cloud-panel');
    const layoutSeg = el('div', undefined, 'seg cloud-layouts'); layoutSeg.setAttribute('role', 'group'); layoutSeg.setAttribute('aria-label', 'Layout');
    for (const name of CM.LAYOUTS) {
      const b = button(CM.LAYOUT_INFO[name].name, '', () => setLayout(name), CM.LAYOUT_INFO[name].blurb + ' (L cycles)');
      b.dataset.layout = name; layoutSeg.append(b);
    }
    const shapeSeg = el('div', undefined, 'seg cloud-shapes'); shapeSeg.setAttribute('role', 'group'); shapeSeg.setAttribute('aria-label', 'Run shape');
    for (const [shape, label, title] of [['blocks', 'Blocks', 'Iso blocks with a lamp per task (B toggles)'], ['orbs', 'Orbs', 'Spheres ringed by their tasks (B toggles)']]) {
      const b = button(label, '', () => setShape(shape), title); b.dataset.shape = shape; shapeSeg.append(b);
    }
    const resetView = button('Reset view', '', () => { const c = cloud(); if (c) c.resetView(); }, 'Frame every shown run (0)');
    const posMenu = el('details', undefined, 'cloud-menu');
    const posSum = el('summary', 'Positions'); posMenu.append(posSum);
    const posBody = el('div', undefined, 'cloud-menu-body');
    const posNote = el('p', '', 'muted');
    const resetPos = button('Reset moved runs', '', () => {
      const c = cloud(); if (!c) return;
      const name = CM.LAYOUT_INFO[prefs.layout].name, undo = c.resetPositions('layout');
      posMenu.open = false; syncBar(); refreshCard();
      say(`Moved runs went back to their places in ${name}.`, 'Undo', () => { undo(); syncBar(); refreshCard(); });
    }, 'Put every run you moved in this layout back where the layout places it');
    const exportPos = button('Export positions…', '', () => {
      const c = cloud(); if (!c) return;
      const blob = new Blob([JSON.stringify(c.exportPositions(), null, 2)], { type: 'application/json' });
      const a = el('a'); a.href = URL.createObjectURL(blob); a.download = 'forge-ui-cloud-layout.json';
      document.body.append(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 2000);
      posMenu.open = false;
    }, 'Save where you moved runs, in every layout, as a JSON file');
    const importPos = button('Import positions…', '', () => file.click(), 'Load positions from a file exported here');
    posBody.append(posNote, resetPos, exportPos, importPos); posMenu.append(posBody);
    file.addEventListener('change', async () => {
      const f = file.files && file.files[0]; file.value = ''; posMenu.open = false;
      if (!f) return;
      if (f.size > 5 * 1024 * 1024) { say('That file is larger than 5 MB: not a cloud layout.'); return; }
      let obj = null;
      try { obj = JSON.parse(await f.text()); } catch (_) { say('That file is not JSON.'); return; }
      const c = cloud(); if (!c) return;
      const res = c.importPositions(obj);
      if (!res.ok) { say(`Not imported: ${res.errors[0]}`); return; }
      say(`Imported ${res.count} position${res.count === 1 ? '' : 's'}${res.unknown ? `, ${res.unknown} for runs not listed here` : ''}${res.errors.length ? `; ${res.errors.length} skipped` : ''}.`);
      syncBar(); refreshCard();
    });
    const full = button('Full screen', '', () => {
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      else if (root.requestFullscreen) root.requestFullscreen().catch(() => say('Full screen is not available here.'));
    });
    document.addEventListener('fullscreenchange', () => { full.textContent = document.fullscreenElement === root ? 'Exit full screen' : 'Full screen'; });
    const title = el('h1', 'Run cloud', 'module-title');
    const left = el('div', undefined, 'cloud-bar-left'); left.append(title, count);
    const tools = el('div', undefined, 'cloud-bar-tools'); tools.append(filtersBtn, layoutSeg, shapeSeg, resetView, posMenu, full);
    if (ctx.viewToggle) tools.append(ctx.viewToggle());
    bar.append(left, tools);

    function syncBar() {
      const active = CM.activeCount(prefs.filter);
      filtersBtn.textContent = active ? `Filters (${active})` : 'Filters';
      filtersBtn.setAttribute('aria-expanded', String(prefs.panel));
      panel.hidden = !prefs.panel;
      for (const b of layoutSeg.children) b.setAttribute('aria-pressed', String(b.dataset.layout === prefs.layout));
      for (const b of shapeSeg.children) b.setAttribute('aria-pressed', String(b.dataset.shape === prefs.shape));
      const three = S.mode === '3d';
      for (const n of [layoutSeg, shapeSeg, resetView, posMenu, full]) n.hidden = !three;
      const c = cloud(), n = c ? c.pinnedCount() : 0;
      posSum.textContent = n ? `Positions (${n} moved)` : 'Positions';
      posNote.textContent = n ? `${n} run${n === 1 ? '' : 's'} moved by you in ${CM.LAYOUT_INFO[prefs.layout].name}. Positions are kept in this browser, per layout.` : `Drag a run to move it. Positions are kept in this browser, per layout.`;
      resetPos.disabled = !n;
      insets();
      const visible = c ? c.counts().visible : S.facts.filter(f => CM.match(f, prefs.filter, nowSec())).length;
      count.textContent = `${visible} of ${S.facts.length} run${S.facts.length === 1 ? '' : 's'}`;
    }
    function setLayout(name) {
      if (!CM.LAYOUTS.includes(name)) return;
      prefs.layout = name; save();
      const c = cloud(); if (c) c.setLayout(name);
      syncBar(); legendText(); refreshCard();
    }
    function setShape(shape) {
      prefs.shape = shape === 'orbs' ? 'orbs' : 'blocks'; save();
      const c = cloud(); if (c) c.setShape(prefs.shape);
      syncBar();
    }

    // what the overlays cover, so the cloud frames into the space left over
    function measure() {
      const R = root.getBoundingClientRect(); if (!R.width) return null;
      const I = { l: 0, r: 0, t: Math.max(0, bar.getBoundingClientRect().bottom - R.top) + 8, b: 0 };
      if (!panel.hidden) { const p = panel.getBoundingClientRect(); if (p.width < R.width * .5) I.l = p.right - R.left + 8; }
      if (!card.hidden) { const k = card.getBoundingClientRect(); if (k.width < R.width * .5) I.r = R.right - k.left + 8; }
      const g = legend.getBoundingClientRect(); if (g.height && g.height < R.height * .3) I.b = R.bottom - g.top + 4;
      return I;
    }
    const insets = () => { const c = cloud(); if (c) c.setInsets(measure()); };

    /* ---------- the filter panel ---------- */
    let frameTimer = 0;
    function apply({ rebuild = false } = {}) {
      save();
      const c = cloud();
      if (c) {
        const before = c.counts().visible;
        c.setFilter(prefs.filter);
        // the camera follows a filter once it settles (not on every key typed)
        clearTimeout(frameTimer);
        if (c.counts().visible !== before) frameTimer = setTimeout(() => { insets(); c.resetView(); }, 650);
      }
      if (rebuild) buildPanel(true);
      syncBar();
      if (S.mode !== '3d') renderList();
      refreshCard();
    }
    function chips(label, entries, get, set) {
      const box = el('fieldset', undefined, 'cloud-field');
      box.append(el('legend', label));
      const row = el('div', undefined, 'cloud-chips');
      for (const [value, text, cnt, st] of entries) {
        const b = button(cnt === undefined ? text : `${text} ${cnt}`, 'cloud-chip' + (st ? ' st-' + st : ''));
        b.dataset.key = label + ':' + value;
        b.setAttribute('aria-pressed', String(get().includes(value)));
        b.addEventListener('click', () => {
          const cur = get(), on = !cur.includes(value);
          set(on ? cur.concat(value) : cur.filter(v => v !== value));
          b.setAttribute('aria-pressed', String(on)); apply();
        });
        row.append(b);
      }
      box.append(row); return box;
    }
    function select(label, key, entries, get, set) {
      const l = el('label', undefined, 'cloud-select'); l.append(el('span', label));
      const s = el('select'); s.dataset.key = key;
      for (const [v, t] of entries) { const o = el('option', t); o.value = String(v); if (String(get()) === String(v)) o.selected = true; s.append(o); }
      s.addEventListener('change', () => { set(s.value); apply(); });
      l.append(s); return l;
    }
    function checklist(label, items, { get, set, onlyLabel = 'only', searchable = false }) {
      const box = el('fieldset', undefined, 'cloud-field');
      const head = el('legend', label); box.append(head);
      const tools = el('div', undefined, 'cloud-check-tools');
      const all = button('All', 'linkish', () => { set([]); apply({ rebuild: true }); });
      tools.append(all);
      let q = null;
      if (searchable && items.length > 8) {
        q = el('input'); q.type = 'search'; q.placeholder = `Find ${label.toLowerCase()}…`; q.dataset.key = label + ':find'; q.setAttribute('aria-label', `Find ${label.toLowerCase()}`);
        tools.append(q);
      }
      box.append(tools);
      const ul = el('ul', undefined, 'cloud-checks');
      const ids = items.map(i => i.id);
      for (const it of items) {
        const li = el('li'); li.dataset.name = it.name.toLowerCase();
        const lab = el('label');
        const c = el('input'); c.type = 'checkbox'; c.dataset.key = label + ':' + it.id;
        c.checked = !get().length || get().includes(it.id);
        c.addEventListener('change', () => {
          let cur = get().length ? get().slice() : ids.slice();
          cur = c.checked ? [...new Set(cur.concat(it.id))] : cur.filter(x => x !== it.id);
          if (!cur.length) { c.checked = true; say(`One of the ${label.toLowerCase()} stays chosen: "All" shows every one.`); return; }
          set(cur.length === ids.length ? [] : cur);
          apply({ rebuild: !cur.length || cur.length === ids.length });
        });
        lab.append(c, el('span', it.name, 'cloud-check-name'), el('span', String(it.count), 'muted'));
        lab.title = it.title || it.name;
        const only = button(onlyLabel, 'linkish cloud-only', () => { set([it.id]); apply({ rebuild: true }); }, `Show ${it.name} only`);
        li.append(lab, only); ul.append(li);
      }
      if (q) q.addEventListener('input', () => { const t = q.value.trim().toLowerCase(); for (const li of ul.children) li.hidden = !!t && !li.dataset.name.includes(t); });
      box.append(ul); return box;
    }
    function buildPanel(keep) {
      const active = keep && panel.contains(document.activeElement) ? document.activeElement.dataset.key : null, top = panel.scrollTop;
      const f = prefs.filter, O = CM.options(S.facts), P = CM.projects(S.runs);
      panel.replaceChildren();
      const head = el('div', undefined, 'cloud-panel-head');
      head.append(el('h2', 'Filters', 'plate'), button('Reset', 'linkish', () => { prefs.filter = CM.emptyFilter(); apply({ rebuild: true }); }, 'Show every run'));
      panel.append(head);
      const text = el('input'); text.type = 'search'; text.value = f.text; text.placeholder = 'goal, repo, run or task id, title, model…'; text.dataset.key = 'text';
      text.setAttribute('aria-label', 'Search the runs');
      text.addEventListener('input', () => { f.text = text.value; apply(); });
      panel.append(text);
      panel.append(checklist('Projects', P.map(p => ({ id: p.id, name: p.name, count: p.count, title: p.repo || p.name })), { get: () => f.projects, set: v => { f.projects = v; }, searchable: true }));
      panel.append(chips('State', STATES.filter(([k]) => O.states[k]).map(([k, label, st]) => [k, label, O.states[k], st]), () => f.states, v => { f.states = v; }));
      if (Object.keys(O.kinds).length > 1) panel.append(chips('Kind', Object.entries(O.kinds).map(([k, n]) => [k, k, n]), () => f.kinds, v => { f.kinds = v; }));
      const dBox = chips('Difficulty', ['low', 'medium', 'high', 'none'].filter(k => O.difficulty[k]).map(k => [k, k === 'none' ? 'not set' : k, O.difficulty[k]]), () => f.difficulty.levels, v => { f.difficulty.levels = v; });
      dBox.append(select('Match', 'difficulty-mode', [['contains', 'has a task of'], ['dominant', 'mostly']], () => f.difficulty.mode, v => { f.difficulty.mode = v === 'dominant' ? 'dominant' : 'contains'; }));
      panel.append(dBox);
      const tBox = el('fieldset', undefined, 'cloud-field'); tBox.append(el('legend', 'Tasks per run'));
      const range = el('div', undefined, 'cloud-range');
      for (const [k, label] of [['min', 'at least'], ['max', 'at most']]) {
        const l = el('label'); l.append(el('span', label));
        const i = el('input'); i.type = 'number'; i.min = '1'; i.max = String(Math.max(1, O.maxTasks)); i.step = '1'; i.dataset.key = 'tasks-' + k; i.inputMode = 'numeric';
        i.value = f.tasks[k] == null ? '' : String(f.tasks[k]); i.placeholder = k === 'min' ? '1' : String(O.maxTasks);
        i.addEventListener('input', () => { const v = i.value === '' ? null : Math.max(1, Math.floor(Number(i.value))); f.tasks[k] = Number.isFinite(v) ? v : null; apply(); });
        l.append(i); range.append(l);
      }
      tBox.append(range); panel.append(tBox);
      if (O.aliases.length) {
        const mBox = checklist('Models', O.aliases.map(a => ({ id: a.alias, name: a.alias, count: a.count })), { get: () => f.models.aliases, set: v => { f.models.aliases = v; }, searchable: true });
        mBox.append(select('As', 'models-role', [['any', 'any role'], ['dwarf', 'implementer (dwarf)'], ['qa', 'reviewer (qa)']], () => f.models.role, v => { f.models.role = ['dwarf', 'qa'].includes(v) ? v : 'any'; }));
        panel.append(mBox);
      }
      const more = el('fieldset', undefined, 'cloud-field cloud-more'); more.append(el('legend', 'More'));
      more.append(select('Active', 'active', [[0, 'any time'], [1, 'in the last day'], [7, 'in the last week'], [30, 'in the last month'], [90, 'in the last 3 months']], () => f.activeDays, v => { f.activeDays = Number(v) || 0; }));
      more.append(select('Final review', 'review', [['', 'any'], ['PASS', 'passed'], ['FAIL', 'failed'], ['none', 'no verdict']], () => f.review, v => { f.review = v; }));
      more.append(select('Verification', 'verification', [['', 'any'], ['PASS', 'passed'], ['FAIL', 'failed'], ['none', 'not run']], () => f.verification, v => { f.verification = v; }));
      const dec = Object.keys(O.decompose);
      if (dec.length > 1) more.append(chips('Decompose', dec.map(k => [k, k === 'none' ? 'off' : k, O.decompose[k]]), () => f.decompose, v => { f.decompose = v; }));
      panel.append(more);
      if (active) { const n = [...panel.querySelectorAll('[data-key]')].find(x => x.dataset.key === active); if (n) n.focus({ preventScroll: true }); }
      panel.scrollTop = top;
    }

    /* ---------- the legend ---------- */
    const legendHow = el('p', '', 'cloud-legend-how');
    function legendText() {
      legendHow.textContent = `${CM.LAYOUT_INFO[prefs.layout].blurb} Size is the number of tasks. Drag a run to move it (Shift: depth, Alt: height); double-click opens it. Keys: arrows orbit, + − zoom, F frame, P projects, [ ] runs, L layout, B shape, 0 reset.`;
    }
    {
      const row = el('ul', undefined, 'cloud-legend-states');
      for (const [, label, st] of STATES) { const li = el('li'); li.append(el('i', undefined, 'led st-' + st), document.createTextNode(label)); row.append(li); }
      legend.append(row, legendHow); legendText();
    }

    /* ---------- the run card ---------- */
    function refreshCard() {
      const key = S.sel, f = key && S.byKey.get(key);
      card.hidden = !f;
      if (!f) { card.replaceChildren(); insets(); return; }
      const r = f.run, c = cloud(), proj = CM.projects(S.runs).find(p => p.id === f.project);
      const shown = CM.match(f, prefs.filter, nowSec());
      card.replaceChildren();
      const head = el('div', undefined, 'cloud-card-head');
      head.append(el('span', STATE[f.state] ? STATE[f.state].label : f.state, 'chip st-' + (STATE[f.state] || { st: 'unknown' }).st),
        el('span', `${proj ? proj.name : RM.basename(r.repo)} · ${r.kind || 'plan'}`, 'muted'));
      const shut = button('', 'cloud-card-close', () => ctx.route({ sel: null, focus: S.focus }), 'Close (Esc)');
      shut.setAttribute('aria-label', 'Close (Esc)'); if (window.ForgeIcons) shut.append(window.ForgeIcons.icon('close'));
      head.append(shut);
      card.append(head, el('h2', String(r.goal || 'Untitled run').split('\n')[0]), el('p', r.run_id || '', 'mono muted'));
      const leds = el('span', undefined, 'leds');
      for (const t of list(r.task_statuses).slice(0, 120)) { const l = el('i', undefined, 'led st-' + String(t.status || 'pending').toLowerCase()); l.title = `${t.id}: ${t.status}${t.title ? ' · ' + t.title : ''}`; leds.append(l); }
      card.append(leds);
      const dl = el('dl', undefined, 'fields');
      const mix = ['high', 'medium', 'low', 'none'].filter(k => f.diff[k]).map(k => `${f.diff[k]} ${k === 'none' ? 'unset' : k}`).join(' · ');
      const plates = specs => { if (!specs.length || !window.ForgeIcons) return specs.join(', ') || '—'; const s = el('span', undefined, 'plates'); for (const x of specs) s.append(window.ForgeIcons.modelPlate(x)); return s; };
      for (const [k, v, mono] of [['Tasks', `${f.n}${mix ? ' · ' + mix : ''}`], ['Implementers', plates([...f.dwarf])], ['Reviewers', plates([...f.qa])],
        ['Started', when(r.created)], ['Updated', when(r.updated)], ['Final review', r.final_review || '—'], ['Verification', r.verification_status || '—'], ['Decompose', r.decompose_level || 'off']]) {
        const dd = el('dd', undefined, mono ? 'mono' : ''); dd.append(v instanceof Node ? v : document.createTextNode(String(v))); dl.append(el('dt', k), dd);
      }
      card.append(dl);
      if (!shown) card.append(el('p', 'Hidden by the current filters.', 'warn'));
      const acts = el('div', undefined, 'cloud-card-actions');
      const open = el('a', 'Open run', 'button primary'); open.href = '#/runs/' + encodeURIComponent(key);
      acts.append(open);
      if (c && shown) acts.append(button('Focus', '', () => c.focusRun(key), 'Bring the camera to this run (F)'));
      if (c && c.isPinned(key)) acts.append(button('Reset position', '', () => { const undo = c.resetPositions('run', key); refreshCard(); syncBar(); say('The run went back to its place.', 'Undo', () => { undo(); refreshCard(); syncBar(); }); }, 'Put this run back where the layout places it'));
      if (!(prefs.filter.projects.length === 1 && prefs.filter.projects[0] === f.project)) acts.append(button('Only this project', '', () => { prefs.filter.projects = [f.project]; apply({ rebuild: true }); }));
      card.append(acts);
      if (c && c.isPinned(key)) card.append(el('p', `Moved by you in ${CM.LAYOUT_INFO[prefs.layout].name}.`, 'muted'));
      insets();
    }

    /* ---------- messages ---------- */
    function say(text, action, fn) {
      clearTimeout(S.toastTimer);
      toast.replaceChildren(el('span', text));
      if (action) toast.append(button(action, 'linkish', () => { toast.hidden = true; fn(); }));
      toast.hidden = false;
      S.toastTimer = setTimeout(() => { toast.hidden = true; }, action ? 9000 : 5000);
    }

    /* ---------- without 3D: the same filtered runs as a list ---------- */
    function renderList() {
      listHost.replaceChildren();
      const head = el('div', undefined, 'cloud-list-head');
      if (S.mode === 'failed') head.append(el('p', S.note || '3D view unavailable.', 'warn'));
      else if (S.mode === 'loading') head.append(el('p', 'Loading the 3D view…', 'muted'));
      else {
        head.append(el('p', 'The cloud is a 3D view. These are the runs it would show.', 'muted'));
        head.append(button('Load 3D', 'primary', () => ctx.load3d && ctx.load3d(), 'Load three.js from cdn.jsdelivr.net and show the cloud'));
      }
      listHost.append(head);
      const shown = S.facts.filter(f => CM.match(f, prefs.filter, nowSec())).sort((a, b) => b.t - a.t);
      const P = CM.projects(S.runs);
      let n = 0;
      for (const p of P) {
        const mine = shown.filter(f => f.project === p.id); if (!mine.length) continue;
        const g = el('section', undefined, 'map-repo'); g.append(el('h3', `${p.name} · ${mine.length}`));
        const grid = el('div', undefined, 'map-grid');
        for (const f of mine) {
          if (n++ >= LIST_CAP) break;
          const r = f.run, a = el('a', undefined, 'map-card st-' + (STATE[f.state] || { st: 'unknown' }).st);
          a.href = '#/runs/' + encodeURIComponent(r.key);
          a.append(el('strong', String(r.goal || 'Untitled run').split('\n')[0]), el('span', `${r.run_id || ''} · ${f.n} task${f.n === 1 ? '' : 's'}${r.kind === 'solo' ? ' · solo' : ''}`, 'mono muted'));
          const leds = el('span', undefined, 'leds');
          for (const t of list(r.task_statuses).slice(0, 60)) leds.append(el('i', undefined, 'led st-' + String(t.status || 'pending').toLowerCase()));
          a.append(leds); grid.append(a);
        }
        g.append(grid); listHost.append(g);
      }
      if (n > LIST_CAP) listHost.append(el('p', `${n - LIST_CAP} more: narrow the filters to see them.`, 'muted'));
      if (!shown.length) listHost.append(el('p', S.facts.length ? 'No runs match these filters.' : 'No runs found.', 'empty'));
    }

    /* ---------- render: data from the app, the stage when there is one ---------- */
    function render({ runs, sel = null, focus = null, mode = '2d', stage = null, note = '' }) {
      const newRuns = runs !== S.runsRef;
      if (newRuns) {
        S.runsRef = runs; S.runs = list(runs);
        S.facts = S.runs.map(r => CM.runFacts(r, nowSec())); S.byKey = new Map(S.facts.map(f => [f.key, f]));
      }
      const modeChanged = mode !== S.mode || stage !== S.stage;
      S.mode = mode; S.stage = stage; S.note = note;
      root.classList.toggle('cloud-3d', mode === '3d');
      listHost.hidden = mode === '3d';
      const optSig = JSON.stringify([CM.projects(S.runs).map(p => [p.id, p.count]), CM.options(S.facts)]);
      if (optSig !== S.optSig) { S.optSig = optSig; buildPanel(true); }
      const selChanged = sel !== S.sel, focusChanged = focus !== S.focus;
      S.sel = sel; S.focus = focus;
      if (mode === '3d' && stage) {
        const c = stage.cloud;
        if (!c || !S.shown || modeChanged || stage.view !== 'cloud') {
          syncBar(); refreshCard();
          stage.showCloud(S.runs, { filter: prefs.filter, layout: prefs.layout, shape: prefs.shape, sel, focus, inset: measure() });
          if (!S.shown && sel) stage.cloud.focusRun(sel);
          S.shown = true;
        } else {
          if (newRuns) c.update(S.runs);
          if (focusChanged) c.focusProject(focus);
          if (selChanged) c.select(sel);
        }
      }
      if (mode !== '3d') renderList();
      syncBar(); refreshCard();
    }

    // keys and clicks on the stage come back through the app (cloud callbacks)
    return {
      render, setLayout, setShape, refreshCard: () => { syncBar(); refreshCard(); },
      setFilter(f) { prefs.filter = CM.normFilter(f); apply({ rebuild: true }); },
      get prefs() { return prefs; },
    };
  }
  return { create };
})();
