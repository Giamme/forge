/* forge-ui: read-only API viewer. All API values enter the DOM as text.
   Views: the map of every run (#/), the run cloud (#/cloud, full screen: cloudui.js over cloud3d.js), and a
   run (#/runs/<key>) with its stage (3D, or the 2D graph and chart), the inspector beside it and the task table under it. The stage is one element that moves between views,
   so the 3D scene (and its WebGL context) survives navigation and refreshes. */
window.ForgeUI = (() => {
  'use strict';
  const RM = window.ForgeRunModel;
  // an exported page (forge_ui/export.py): one run and the API responses it needs, no server behind it
  const SNAP = (() => { const n = document.getElementById('forge-snapshot'); if (!n) return null; try { const v = JSON.parse(n.textContent); return v && v.api ? v : null; } catch (_) { return null; } })();
  const store = {
    get(k, d) { try { const v = localStorage.getItem('forge-ui-' + k); return v === null ? d : JSON.parse(v); } catch (_) { return d; } },
    set(k, v) { try { localStorage.setItem('forge-ui-' + k, JSON.stringify(v)); } catch (_) { /* private mode: not remembered */ } },
  };
  const state = { runs: [], listText: null, cache: new Map(), key: null, sel: null, paused: false, errors: new Map(), pending: new Map(),
    mapFilter: Object.assign({ text: '', state: '', repo: '', kind: '', model: '', ageDays: 0, verdict: '' }, store.get('map-filter', {})),
    showAll: false, mode: store.get('mode', 'plan'), dim: store.get('view', '3d'),
    overlay: Object.assign({ height: 'none', color: 'status', critical: false, arcs: true, ghosts: true }, store.get('overlay', {})),
    replay: null, playing: null, speed: store.get('speed', 60), stage: null, stageFailed: null, stageLoading: false, timer: null,
    lastStatuses: new Map(), announced: 0, rendered: null, cloud: null, cloudUI: null, cloudPrefs: store.get('cloud', {}),
    models: false, modelsData: null, modelsText: null, modelsError: null, modelsPrefs: store.get('models', {}),
    files: new Map(), filesPrefs: store.get('files', {}) };
  state.stageMode = state.mode === 'exec' ? 'exec' : 'plan';   // the stage's own mode, kept while Files is shown
  if (state.mode === 'files' && !window.ForgeFiles) state.mode = state.stageMode;
  const $ = id => document.getElementById(id);
  const display = value => value === null || value === undefined || value === '' ? '—' :
    typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value);
  const list = value => Array.isArray(value) ? value : [];
  const wall = () => Date.now() / 1000;
  const now = () => { const run = state.key && state.cache.get(state.key)?.data; return run ? RM.liveNow(run, Date.now() / 1000) : Date.now() / 1000; };
  function el(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = display(text);
    if (className) node.className = className;
    return node;
  }
  /* ---------- routes ---------- */
  function hashFor(key, sel) {
    if (key == null) return '#/';
    const base = '#/runs/' + encodeURIComponent(key);
    if (!sel) return base;
    const e = encodeURIComponent;
    if (sel.kind === 'task') return `${base}/tasks/${e(sel.id)}`;
    if (sel.kind === 'execution') return sel.task ? `${base}/tasks/${e(sel.task)}/executions/${e(sel.exec)}` : `${base}/executions/${e(sel.exec)}`;
    if (sel.kind === 'final') return `${base}/final-review${sel.name ? '/' + e(sel.name) : ''}`;
    if (sel.kind === 'verification') return `${base}/verification`;
    if (sel.kind === 'integration') return `${base}/integration`;
    if (sel.kind === 'fractal') return `${base}/tasks/${e(sel.task)}/fractal/${e(sel.node)}`;
    return base;
  }
  // a diff: {kind: 'task', task, source, file} | {kind: 'final', name, file} | {kind: 'fractal', task, node, file}
  function diffHash(key, spec) {
    const e = encodeURIComponent, base = '#/runs/' + e(key), file = spec.file ? '/file/' + e(spec.file) : '';
    if (spec.kind === 'final') return `${base}/final-review/${e(spec.name)}/diff${file}`;
    if (spec.kind === 'fractal') return `${base}/tasks/${e(spec.task)}/fractal/${e(spec.node)}/diff${file}`;
    return `${base}/tasks/${e(spec.task)}/diff${spec.source && spec.source !== 'current' ? '/' + e(spec.source) : ''}${file}`;
  }
  function cloudHash(sel, focus) {
    if (sel) return '#/cloud/runs/' + encodeURIComponent(sel);
    return focus ? '#/cloud/projects/' + encodeURIComponent(focus) : '#/cloud';
  }
  function parseHash(hash) {
    const c = hash.match(/^#\/cloud(?:\/(runs|projects|models)\/([^/]+))?\/?$/);
    if (c) return { key: null, sel: null, cloud: { sel: c[1] === 'runs' ? decodeURIComponent(c[2]) : null, focus: c[1] === 'projects' ? decodeURIComponent(c[2]) : null, model: c[1] === 'models' ? decodeURIComponent(c[2]) : null } };
    if (/^#\/models\/?$/.test(hash)) return { key: null, sel: null, models: true };
    const m = hash.match(/^#\/runs\/([^/]+)(\/.*)?$/);
    if (!m) return { key: null, sel: null };
    const d = decodeURIComponent, rest = m[2] || '';
    let sel = null, r;
    if ((r = rest.match(/^\/tasks\/([^/]+)\/diff(?:\/([^/]+))?(?:\/file\/(.+?))?\/?$/))) return { key: d(m[1]), sel: { kind: 'task', id: d(r[1]) }, diff: { kind: 'task', task: d(r[1]), source: r[2] ? d(r[2]) : 'current', file: r[3] ? d(r[3]) : null } };
    if ((r = rest.match(/^\/tasks\/([^/]+)\/fractal\/([^/]+)\/diff(?:\/file\/(.+?))?\/?$/))) return { key: d(m[1]), sel: { kind: 'task', id: d(r[1]) }, diff: { kind: 'fractal', task: d(r[1]), node: d(r[2]), file: r[3] ? d(r[3]) : null } };
    if ((r = rest.match(/^\/final-review\/([^/]+)\/diff(?:\/file\/(.+?))?\/?$/))) return { key: d(m[1]), sel: { kind: 'final', name: d(r[1]) }, diff: { kind: 'final', name: d(r[1]), file: r[2] ? d(r[2]) : null } };
    if ((r = rest.match(/^\/tasks\/([^/]+)\/executions\/([^/]+)\/?$/))) sel = { kind: 'execution', task: d(r[1]), id: d(r[1]), exec: d(r[2]) };
    if (sel && sel.kind === 'execution' && sel.task) {
      const run = state.cache.get(d(m[1]))?.data, t = run && list(run.tasks).find(x => x.id === sel.task);
      if (t && list(t.shadow).some(x => x.execution === sel.exec)) sel.shadow = true;
    }
    else if ((r = rest.match(/^\/tasks\/([^/]+)\/?$/))) sel = { kind: 'task', id: d(r[1]) };
    else if ((r = rest.match(/^\/executions\/([^/]+)\/?$/))) sel = { kind: 'execution', task: null, exec: d(r[1]) };
    else if ((r = rest.match(/^\/final-review(?:\/([^/]+))?\/?$/))) sel = { kind: 'final', name: r[1] ? d(r[1]) : null };
    else if (/^\/verification\/?$/.test(rest)) sel = { kind: 'verification' };
    else if (/^\/integration\/?$/.test(rest)) sel = { kind: 'integration' };
    else if ((r = rest.match(/^\/tasks\/([^/]+)\/fractal\/([^/]+)\/?$/))) sel = { kind: 'fractal', task: d(r[1]), node: d(r[2]) };
    return { key: d(m[1]), sel };
  }
  const select = sel => { location.hash = hashFor(state.key, sel); };
  function link(text, key, sel) { const a = el('a', text); a.href = hashFor(key, sel); return a; }

  const statuses = new Set(['merged', 'pass', 'running', 'pending', 'fail', 'error', 'infra',
    'timeout', 'unknown', 'invalidated', 'blocked', 'interrupted', 'nochanges', 'conflict']);
  function chip(value) {
    const status = String(value || 'unknown').toLowerCase();
    return el('span', value || '—', 'chip st-' + (statuses.has(status) ? status : 'unknown'));
  }
  function time(value) {
    if (value == null || !Number.isFinite(Number(value))) return '—';
    const date = new Date(Number(value) * 1000);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString();
  }
  function relative(value) {
    if (value == null || !Number.isFinite(Number(value))) return 'updated —';
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(value)));
    if (seconds < 60) return 'updated just now';
    if (seconds < 3600) return `updated ${Math.floor(seconds / 60)}m ago`;
    if (seconds < 86400) return `updated ${Math.floor(seconds / 3600)}h ago`;
    return `updated ${Math.floor(seconds / 86400)}d ago`;
  }
  function section(parent, title, id) {
    const node = el('section');
    if (id) node.id = id;
    if (title) node.append(el('h2', title));
    parent.append(node);
    return node;
  }
  function fields(parent, entries) {
    const dl = el('dl', undefined, 'fields');
    for (const [label, value, mono] of entries) {
      dl.append(el('dt', label));
      const dd = el('dd', undefined, mono ? 'mono' : '');
      dd.append(value instanceof Node ? value : document.createTextNode(display(value)));
      dl.append(dd);
    }
    parent.append(dl);
  }
  function details(parent, title, value, key) {
    const node = el('details');
    node.dataset.keep = key;
    const pre = el('pre', value);
    pre.dataset.scroll = key + '-text';
    node.append(el('summary', title), pre);
    parent.append(node);
    return node;
  }
  function table(parent, headers, rows, key) {
    const wrap = el('div', undefined, 'table-scroll');
    wrap.dataset.scroll = key;
    wrap.tabIndex = 0;
    wrap.setAttribute('role', 'region');
    wrap.setAttribute('aria-label', key);
    const node = el('table');
    const head = el('tr');
    for (const label of headers) { const th = el('th', label); th.scope = 'col'; head.append(th); }
    const thead = el('thead');
    thead.append(head);
    const body = el('tbody');
    for (const cells of rows) {
      const tr = el('tr');
      for (const [index, value] of cells.entries()) {
        const mono = ['ID', 'Dwarf', 'QA', 'Branch'].includes(headers[index]);
        const td = el('td', undefined, mono ? 'mono' : '');
        td.append(value instanceof Node ? value : document.createTextNode(display(value)));
        tr.append(td);
      }
      body.append(tr);
    }
    node.append(thead, body);
    wrap.append(node);
    parent.append(wrap);
    return body;
  }
  // Restore disclosures, internal scroll and keyboard focus after changed data arrives.
  function preserve(parent, build, { restore = true } = {}) {
    const open = new Map(Array.from(parent.querySelectorAll('details[data-keep]'), n => [n.dataset.keep, n.open]));
    const scroll = new Map(Array.from(parent.querySelectorAll('[data-scroll]'), n => [n.dataset.scroll, [n.scrollLeft, n.scrollTop]]));
    const active = parent.contains(document.activeElement) ? document.activeElement : null;
    const focusHref = active?.getAttribute('href');
    const own = [parent.scrollLeft, parent.scrollTop];
    parent.replaceChildren();
    build();
    if (!restore) return;
    for (const node of parent.querySelectorAll('details[data-keep]')) if (open.has(node.dataset.keep)) node.open = open.get(node.dataset.keep);
    for (const node of parent.querySelectorAll('[data-scroll]')) {
      const pos = scroll.get(node.dataset.scroll);
      if (pos) { node.scrollLeft = pos[0]; node.scrollTop = pos[1]; }
    }
    parent.scrollLeft = own[0]; parent.scrollTop = own[1];
    if (focusHref) Array.from(parent.querySelectorAll('a')).find(n => n.getAttribute('href') === focusHref)?.focus({ preventScroll: true });
  }
  function scoped(parent, scope) {
    for (const node of parent.querySelectorAll('[data-keep], [data-scroll]')) {
      if (node.dataset.keep && !node.dataset.keep.startsWith(scope)) node.dataset.keep = scope + node.dataset.keep;
      if (node.dataset.scroll && !node.dataset.scroll.startsWith(scope)) node.dataset.scroll = scope + node.dataset.scroll;
    }
  }

  /* ---------- errors and the run list ---------- */
  function currentResource(path) {
    return path === '/api/runs' || (state.models && path === '/api/models') || (state.key != null && path === '/api/runs/' + encodeURIComponent(state.key));
  }
  function renderErrors() {
    for (const path of state.errors.keys()) if (!currentResource(path)) state.errors.delete(path);
    const banner = $('errors');
    banner.textContent = Array.from(state.errors.values()).join(' · ');
    banner.hidden = !state.errors.size;
  }
  function filteredRuns() {
    return state.runs.filter(run => RM.matchRun(run, state.mapFilter, wall())).sort((a, b) => (b.updated || 0) - (a.updated || 0));
  }
  function renderList() {
    const runs = filteredRuns();
    $('run-count').textContent = `${runs.length} / ${state.runs.length}`;
    if ($('filter').value !== state.mapFilter.text) $('filter').value = state.mapFilter.text;
    preserve($('run-list'), () => {
      if (!runs.length) {
        const empty = el('div', undefined, 'empty');
        empty.append(el('p', state.runs.length ? 'No runs match this filter.' : 'No runs found.'));
        if (!state.runs.length) {
          empty.append(el('p', 'Sources: explicit --plan directories, --root directories (up to two levels deep), and the forge run registry at $XDG_CONFIG_HOME/forge/runs/ (default ~/.config/forge/runs/). Every forge run registers itself there.'));
          empty.append(el('p', 'Point the server at your runs:'), el('pre', 'python3 -m forge_ui --root <dir>'));
        }
        $('run-list').append(empty);
      }
      for (const run of runs) {
        const item = link(undefined, run.key);
        item.className = 'run-item';
        item.dataset.run = run.key;
        const goal = el('strong', String(run.goal || 'Untitled run').split('\n')[0], 'run-goal');
        goal.title = run.goal || 'Untitled run';
        const solo = run.kind === 'solo';
        item.append(goal, el('span', RM.basename(run.repo), 'repo'), el('span', run.run_id, 'mono'));
        if (solo) item.append(el('span', 'solo', 'tag'));
        const updated = el('span', relative(run.updated), 'muted updated');
        if (run.updated != null) updated.dataset.epoch = run.updated;
        updated.title = time(run.updated);
        item.append(updated);
        const counts = el('div', undefined, 'chips');
        for (const [status, count] of Object.entries(run.counts || {})) {
          const badge = chip(status);
          badge.append(document.createTextNode(' ' + display(count)));
          counts.append(badge);
        }
        item.append(counts);
        if (!solo) {
          const verification = el('span', undefined, 'verification');
          verification.append(document.createTextNode('Verification '), chip(run.verification_status));
          if (run.final_review) verification.append(document.createTextNode(' Review '), chip(run.final_review));
          item.append(verification);
        }
        $('run-list').append(item);
      }
    });
    markSelection();
  }
  function markSelection() {
    for (const node of $('run-list').querySelectorAll('[data-run]')) {
      const selected = node.dataset.run === state.key;
      node.classList.toggle('selected', selected);
      if (selected) node.setAttribute('aria-current', 'page'); else node.removeAttribute('aria-current');
    }
  }

  /* ---------- the stage: one element, 3D when it can be, else the 2D views ---------- */
  const stageHost = el('div', undefined, 'stage');
  stageHost.id = 'stage-host';
  const stageNote = el('p', undefined, 'muted stage-note');
  const use3d = () => state.dim === '3d' && !state.stageFailed;
  function ensureStage() {
    if (!use3d() || state.stage || state.stageLoading || !window.ForgeMap3D) return;
    state.stageLoading = true;
    window.ForgeMap3D.load().then(mod => mod.createStage(stageHost, {
      onSelect: sel => select(sel),
      onOpenRun: key => { location.hash = hashFor(key, null); },
      onBack: () => { location.hash = '#/'; },
      onMode: mode => setMode(mode),
      cloud: {
        onSelect: key => { location.hash = cloudHash(key, state.cloud && state.cloud.focus); },
        onFocus: pid => { location.hash = cloudHash(null, pid); },
        onOpenRun: key => { location.hash = hashFor(key, null); },
        onMoved: () => state.cloudUI && state.cloudUI.refreshCard(),
        onLayout: name => state.cloudUI && state.cloudUI.setLayout(name),
        onShape: shape => state.cloudUI && state.cloudUI.setShape(shape),
      },
    })).then(stage => {
      state.stage = stage; state.stageLoading = false;
      stage.setOverlay(state.overlay);
      stage.setReplay(state.replay);
      renderMain();
    }, err => {
      state.stageLoading = false;
      if (window.ForgeMap3D.state === 'ready') window.ForgeMap3D.reason = 'stage';
      state.stageFailed = window.ForgeMap3D.reason || 'load';
      if (err && window.console) console.warn('forge-ui: 3D view unavailable', err);
      renderMain();
    });
  }
  function placeStage(slot) {
    if (!slot) return;
    const show = use3d();
    if (stageHost.parentNode !== slot) slot.append(stageHost);
    stageHost.hidden = !show || !state.stage;
    slot.classList.toggle('stage-on', show && !!state.stage);
    slot.classList.toggle('stage-loading', show && !state.stage);
    stageNote.textContent = state.stageFailed && state.dim === '3d' ? window.ForgeMap3D.message() : show && !state.stage ? 'Loading the 3D view…' : '';
    if (stageNote.parentNode !== slot.parentNode) slot.after(stageNote);
  }

  /* ---------- the map of every run ---------- */
  function filterBar(parent) {
    const f = state.mapFilter, opts = RM.filterOptions(state.runs);
    const bar = el('form', undefined, 'filters');
    bar.setAttribute('aria-label', 'Filter the runs on the map');
    bar.addEventListener('submit', e => e.preventDefault());
    const add = (label, control) => { const l = el('label', undefined, 'filter'); l.append(el('span', label), control); bar.append(l); return control; };
    const text = el('input'); text.type = 'search'; text.value = f.text; text.placeholder = 'goal, repo, id, model…'; text.dataset.f = 'text';
    add('Search', text);
    const sel = (key, entries) => {
      const s = el('select'); s.dataset.f = key;
      for (const [v, label] of entries) { const o = el('option', label); o.value = v; if (String(f[key]) === String(v)) o.selected = true; s.append(o); }
      return s;
    };
    add('State', sel('state', [['', 'any'], ['live', 'running now'], ['failed', 'has failures'], ['done', 'all done'], ['pending', 'not started']]));
    add('Repo', sel('repo', [['', 'any'], ...opts.repos.map(r => [r.path, r.name])]));
    add('Kind', sel('kind', [['', 'any'], ['plan', 'plan'], ['solo', 'solo']]));
    add('Model', sel('model', [['', 'any'], ...opts.models.map(m => [m, m])]));
    add('Age', sel('ageDays', [[0, 'any'], [1, 'last day'], [7, 'last week'], [30, 'last month']]));
    add('Review', sel('verdict', [['', 'any'], ['PASS', 'passed'], ['FAIL', 'failed'], ['none', 'no verdict']]));
    const all = el('input'); all.type = 'checkbox'; all.checked = state.showAll; all.dataset.f = 'showAll';
    const allL = el('label', undefined, 'filter check'); allL.append(all, document.createTextNode(' older runs too')); bar.append(allL);
    const reset = el('button', 'Reset'); reset.type = 'button'; reset.addEventListener('click', () => { Object.assign(state.mapFilter, { text: '', state: '', repo: '', kind: '', model: '', ageDays: 0, verdict: '' }); state.showAll = false; filterChanged(true); });
    bar.append(reset);
    bar.addEventListener('input', e => {
      const k = e.target.dataset.f; if (!k) return;
      if (k === 'showAll') state.showAll = e.target.checked;
      else state.mapFilter[k] = k === 'ageDays' ? Number(e.target.value) : e.target.value;
      filterChanged(false);
    });
    parent.append(bar);
  }
  function filterChanged(rebuildBar) {
    store.set('map-filter', state.mapFilter);
    renderList();
    if (!state.key && !state.cloud) renderMap({ rebuildBar });
  }
  function mapCards(parent) {
    const A = RM.atlasModel(state.runs, state.mapFilter, { now: wall(), showAll: state.showAll });
    const wrap = el('div', undefined, 'map-cards');
    for (const layer of A.layers) {
      const g = el('section', undefined, 'map-repo');
      g.append(el('h3', layer.name));
      const grid = el('div', undefined, 'map-grid');
      for (const m of A.modules.filter(x => x.layer === layer.id)) {
        const r = m.run, card = link(undefined, r.key);
        card.className = `map-card st-${({ live: 'running', failed: 'fail', done: 'merged', pending: 'pending' })[m.state]}${m.match ? '' : ' dim'}`;
        card.append(el('strong', String(r.goal || 'Untitled run').split('\n')[0]), el('span', `${r.run_id || ''}${r.kind === 'solo' ? ' · solo' : ''}`, 'mono muted'));
        const leds = el('span', undefined, 'leds');
        for (const t of m.tasks.slice(0, 60)) { const l = el('i', undefined, 'led st-' + String(t.status || 'pending').toLowerCase()); l.title = `${t.id}: ${t.status}`; leds.append(l); }
        card.append(leds, el('span', relative(r.updated), 'muted'));
        grid.append(card);
      }
      g.append(grid); wrap.append(g);
    }
    parent.append(wrap);
    return A;
  }
  function renderMap({ rebuildBar = true } = {}) {
    const main = $('main');
    if (main.dataset.view !== 'map') {
      main.dataset.view = 'map'; delete main.dataset.run;
      main.replaceChildren();
      const v = section(main, undefined, 'map-view'); v.className = 'map-view';
      const head = el('div', undefined, 'map-head');
      const toCloud = el('a', 'Open the run cloud', 'top-link'); toCloud.href = '#/cloud';
      head.append(el('h1', 'All runs'), el('span', '', 'muted'), toCloud, viewToggle());
      toCloud.classList.add('push');
      v.append(head);
      const bar = el('div'); bar.id = 'map-filters'; v.append(bar);
      const slot = el('div', undefined, 'stage-slot map'); slot.id = 'stage-slot'; v.append(slot);
      const cards = el('div'); cards.id = 'map-cards'; v.append(cards);
      rebuildBar = true;
    }
    if (rebuildBar) preserve($('map-filters'), () => filterBar($('map-filters')));
    syncToggles();
    ensureStage();
    placeStage($('stage-slot'));
    let info;
    if (use3d() && state.stage) {
      info = state.stage.showAtlas(state.runs, state.mapFilter, { showAll: state.showAll });
      $('map-cards').replaceChildren();
    } else {
      preserve($('map-cards'), () => { const A = mapCards($('map-cards')); info = { hidden: A.hidden, matches: A.matches, shown: A.modules.length, total: A.total }; });
    }
    const counter = $('map-view').querySelector('.map-head .muted');
    if (info) counter.textContent = `${info.matches} of ${info.shown} shown runs match${info.hidden ? ` · ${info.hidden} older hidden` : ''}`;
  }

  /* ---------- the run cloud: every run in 3D, full screen ---------- */
  function renderCloud() {
    const main = $('main');
    if (main.dataset.view !== 'cloud' || !state.cloudUI) {
      main.dataset.view = 'cloud'; delete main.dataset.run;
      main.replaceChildren();
      const v = el('section', undefined, 'cloud-view'); v.id = 'cloud-view'; v.setAttribute('aria-label', 'Run cloud');
      const slot = el('div', undefined, 'stage-slot cloud'); slot.id = 'stage-slot'; v.append(slot);
      main.append(v);
      state.cloudUI = window.ForgeCloudUI.create(v, {
        prefs: state.cloudPrefs,
        save: p => { state.cloudPrefs = p; store.set('cloud', p); },
        route: ({ sel, focus }) => { location.hash = cloudHash(sel, focus); },
        load3d: () => { state.dim = '3d'; store.set('view', '3d'); state.stageFailed = null; renderMain(); },
        viewToggle,
      });
    }
    ensureStage();
    placeStage($('stage-slot'));
    stageNote.textContent = '';
    const mode = use3d() ? (state.stage ? '3d' : 'loading') : state.stageFailed && state.dim === '3d' ? 'failed' : '2d';
    state.cloudUI.render({ runs: state.runs, sel: state.cloud.sel, focus: state.cloud.focus, mode, stage: mode === '3d' ? state.stage : null,
      note: mode === 'failed' ? window.ForgeMap3D.message() : '' });
    syncToggles();
  }

  /* ---------- the model leaderboard (#/models) ---------- */
  function renderModels() {
    const main = $('main');
    if (main.dataset.view !== 'models') {
      main.dataset.view = 'models'; delete main.dataset.run;
      main.replaceChildren();
      const v = el('section', undefined, 'models-view'); v.id = 'models-view'; main.append(v);
    }
    const host = $('models-view');
    if (!state.modelsData) {
      host.replaceChildren(el('h1', 'Models'), el('p', state.modelsError ? `Could not load the model results (${state.modelsError}).` : 'Reading every run\'s attempts and reviews…', state.modelsError ? 'warn' : 'muted'));
      return;
    }
    window.ForgeLeaderboard.render(host, state.modelsData, {
      runs: state.runs, prefs: state.modelsPrefs, now: () => Date.now() / 1000,
      savePrefs: p => { state.modelsPrefs = p; store.set('models', p); },
      modelHref: alias => '#/cloud/models/' + encodeURIComponent(alias),
    });
  }
  function loadModels() {
    return request('/api/models', result => {
      state.modelsError = null;
      if (result.text === state.modelsText) return;
      state.modelsText = result.text; state.modelsData = result.data;
      if (state.models) renderModels();
    }, 'Could not refresh the model results', error => { state.modelsError = error.message; if (state.models) renderModels(); });
  }

  /* ---------- a run ---------- */
  function viewToggle() {
    const g = el('div', undefined, 'seg'); g.setAttribute('role', 'group'); g.setAttribute('aria-label', 'Rendering');
    for (const [v, label] of [['3d', '3D'], ['2d', '2D']]) {
      const b = el('button', label); b.type = 'button'; b.dataset.dim = v;
      b.addEventListener('click', () => { state.dim = v; store.set('view', v); if (v === '3d') state.stageFailed = null; renderMain(); });
      g.append(b);
    }
    return g;
  }
  function syncToggles() {
    for (const b of document.querySelectorAll('[data-dim]')) b.setAttribute('aria-pressed', String(b.dataset.dim === state.dim));
    for (const b of document.querySelectorAll('[data-mode]')) b.setAttribute('aria-pressed', String(b.dataset.mode === state.mode));
  }
  function setMode(mode) {
    if (mode === state.mode) return;
    state.mode = mode; store.set('mode', mode);
    if (mode !== 'files') { state.stageMode = mode; if (state.stage) state.stage.setMode(mode); }
    syncToggles();
    const run = state.cache.get(state.key)?.data; if (run) renderRunBody(run);
  }
  function toolbar(parent) {
    const bar = el('div', undefined, 'run-toolbar'); bar.setAttribute('role', 'toolbar'); bar.setAttribute('aria-label', 'Run view');
    const modes = el('div', undefined, 'seg'); modes.setAttribute('role', 'group'); modes.setAttribute('aria-label', 'Plan or execution');
    for (const [m, label, title] of [['plan', 'Planned waves', 'Tasks in the waves the plan put them in'], ['exec', 'Execution', 'Attempts on wall-clock time, in the lanes they really ran in (e)'],
      ['files', 'Files', 'Which tasks planned, changed, drifted into or conflicted on which files']].filter(([m]) => m !== 'files' || window.ForgeFiles)) {
      const b = el('button', label); b.type = 'button'; b.dataset.mode = m; b.title = title; b.addEventListener('click', () => setMode(m)); modes.append(b);
    }
    bar.append(modes, viewToggle());
    const ov = el('div', undefined, 'overlays');
    const sel = (label, key, entries) => {
      const l = el('label', undefined, 'filter'); l.append(el('span', label));
      const s = el('select'); s.dataset.ov = key;
      for (const [v, t] of entries) { const o = el('option', t); o.value = v; if (state.overlay[key] === v) o.selected = true; s.append(o); }
      l.append(s); ov.append(l);
    };
    sel('Height', 'height', [['none', 'difficulty'], ['duration', 'duration'], ['tokens', 'tokens'], ['attempts', 'attempts']]);
    sel('Colour', 'color', [['status', 'status'], ['model', 'model'], ['duration', 'duration'], ['tokens', 'tokens']]);
    for (const [key, label, title] of [['critical', 'Critical path', 'The chain of dependencies that bounded the wall time'], ['arcs', 'Review marks', 'Shadow disagreements and Clef rings'], ['ghosts', 'Projected', 'Where the tasks that have not started would run, by forge-schedule\'s rules']]) {
      const l = el('label', undefined, 'filter check'); l.title = title;
      const c = el('input'); c.type = 'checkbox'; c.checked = !!state.overlay[key]; c.dataset.ov = key;
      l.append(c, document.createTextNode(' ' + label)); ov.append(l);
    }
    ov.addEventListener('input', e => {
      const k = e.target.dataset.ov; if (!k) return;
      state.overlay[k] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
      store.set('overlay', state.overlay);
      if (state.stage) state.stage.setOverlay({ [k]: state.overlay[k] });
      const run = state.cache.get(state.key)?.data; if (run) renderRunBody(run);
    });
    bar.append(ov);
    const rp = el('div', undefined, 'replay'); rp.id = 'replay';
    const play = el('button', '▶ Replay'); play.type = 'button'; play.dataset.act = 'play';
    const range = el('input'); range.type = 'range'; range.min = '0'; range.max = '1000'; range.value = '1000'; range.dataset.act = 'scrub'; range.setAttribute('aria-label', 'Replay time');
    const speed = el('select'); speed.dataset.act = 'speed'; speed.setAttribute('aria-label', 'Replay speed');
    for (const v of [10, 60, 600]) { const o = el('option', `×${v}`); o.value = String(v); if (state.speed === v) o.selected = true; speed.append(o); }
    const live = el('button', 'Live'); live.type = 'button'; live.dataset.act = 'live';
    const at = el('span', '', 'muted mono'); at.dataset.act = 'at';
    rp.append(play, range, speed, live, at);
    rp.addEventListener('click', e => { const a = e.target.dataset.act; if (a === 'play') togglePlay(); if (a === 'live') setReplay(null); });
    rp.addEventListener('input', e => {
      const a = e.target.dataset.act, run = state.cache.get(state.key)?.data; if (!run) return;
      if (a === 'scrub') { stopPlay(); const r = RM.timeRange(run, now()); if (r) setReplay(r.start + (r.end - r.start) * Number(e.target.value) / 1000); }
      if (a === 'speed') { state.speed = Number(e.target.value); store.set('speed', state.speed); }
    });
    bar.append(rp);
    parent.append(bar);
  }
  function syncReplay(run) {
    const rp = $('replay'); if (!rp) return;
    const r = RM.timeRange(run, now());
    rp.hidden = !r;
    if (!r) return;
    const range = rp.querySelector('[data-act=scrub]'), at = rp.querySelector('[data-act=at]');
    range.value = String(state.replay === null ? 1000 : Math.round((state.replay - r.start) / Math.max(1, r.end - r.start) * 1000));
    at.textContent = state.replay === null ? 'live' : `+${RM.fmtDur(state.replay - r.start)} · ${new Date(state.replay * 1000).toLocaleTimeString()}`;
    rp.querySelector('[data-act=play]').textContent = state.playing ? '❚❚ Pause' : '▶ Replay';
    rp.querySelector('[data-act=live]').disabled = state.replay === null;
  }
  function setReplay(t) {
    state.replay = t === null ? null : t;
    if (t === null) stopPlay();
    if (state.stage) state.stage.setReplay(state.replay);
    const run = state.cache.get(state.key)?.data; if (run) { renderRunBody(run); }
  }
  function stopPlay() { if (state.playing) { clearInterval(state.playing); state.playing = null; } }
  function togglePlay() {
    const run = state.cache.get(state.key)?.data; if (!run) return;
    if (state.playing) { stopPlay(); syncReplay(run); return; }
    const r = RM.timeRange(run, now()); if (!r) return;
    if (state.replay === null || state.replay >= r.end) state.replay = r.start;
    state.playing = setInterval(() => {
      const run2 = state.cache.get(state.key)?.data, r2 = run2 && RM.timeRange(run2, now());
      if (!r2) { stopPlay(); return; }
      const t = Math.min(r2.end, state.replay + state.speed * .2);
      setReplay(t);
      if (t >= r2.end) { stopPlay(); syncReplay(run2); }
    }, 200);
    syncReplay(run);
  }
  // A copy of the run whose statuses are what they were at time t (for the 2D views during a replay).
  function atReplay(run) {
    if (state.replay === null) return run;
    const s = RM.stateAt(run, state.replay);
    return { ...run, tasks: list(run.tasks).map(t => ({ ...t, status: s.tasks[t.id] ? s.tasks[t.id].status : t.status })) };
  }
  function renderHeader(run) {
    const head = $('run-head');
    preserve(head, () => {
      const solo = run.kind === 'solo';
      head.append(el('h2', String(run.goal || 'Untitled run').split('\n')[0]));
      const meta = el('p', undefined, 'run-meta');
      meta.append(el('span', RM.basename(run.repo), 'repo'), el('span', run.run_id, 'mono'), el('span', solo ? 'solo run' : `${run.task_count} tasks · ${list(run.waves).length} waves`),
        el('span', relative(run.updated), 'muted'));
      const counts = el('span', undefined, 'chips');
      for (const [s, n] of Object.entries(run.counts || {})) { const c = chip(s); c.append(document.createTextNode(' ' + n)); counts.append(c); }
      meta.append(counts);
      if (!SNAP) {
        const ex = el('button', 'Export…', 'export-btn'); ex.type = 'button'; ex.title = 'Download this run as one HTML file to share';
        ex.addEventListener('click', () => exportDialog(run)); meta.append(ex);
      }
      head.append(meta);
      const more = el('details', undefined, 'run-more'); more.dataset.keep = 'run-more';
      more.append(el('summary', 'Run details'));
      fields(more, [['Repo', run.repo, true], ['Run ID', run.run_id, true], ['Path', run.path, true], ['Source', run.source],
        ['Decompose', run.decompose_level], ['Updated', time(run.updated)], ['Explore', run.settings?.explore], ['Shadow rate', run.settings?.shadow_rate],
        ['Shadow reviewers', list(run.settings?.shadow_qa).join(', '), true], ['QA threshold', run.settings?.qa_threshold], ['Max parallel', run.settings?.max_parallel],
        ['Final reviewer', run.settings?.final_qa, true], ['Timeout (seconds)', run.settings?.timeout], ['Verification', chip(run.verification?.status || run.verification_status)],
        ['Command', run.verification?.command, true]]);
      details(more, 'Request', run.request, 'run-request');
      if (!solo) details(more, 'Waves', list(run.waves).map((ids, i) => `${i + 1}: ${list(ids).join(', ')}`).join('\n') || '—', 'waves');
      if (run.verification?.log_tail != null) details(more, 'Verification output', run.verification.log_tail, 'verification-output');
      head.append(more);
      if (list(run.warnings).length) details(head, `Reader warnings (${run.warnings.length})`, run.warnings.join('\n'), 'warnings');
      if (list(run.truncated).length) details(head, 'Truncated fields', run.truncated.join('\n'), 'truncated');
      scoped(head, run.key + ':');
    });
  }
  function renderTable(run) {
    const host = $('run-tasks');
    preserve(host, () => {
      if (run.kind === 'solo') return;
      host.append(el('h2', `Tasks (${list(run.tasks).length})`));
      if (!list(run.tasks).length) { host.append(el('p', 'This run has no tasks yet.', 'empty')); return; }
      const sev = f => f && f.worst ? `${f.worst} ×${f[f.worst]}` : f && f.plausible ? `~${f.plausible}` : '—';
      const rows = list(run.tasks).map(task => {
        const picks = list(task.route).filter(r => r.explored);
        const exploration = task.explored ? '* ' + (picks.length ? picks.map(r => `${display(r.chosen)} p=${display(r.propensity)}`).join('; ') : 'p=—') : '—';
        const deps = el('span', undefined, 'mono');
        list(task.deps).forEach((id, i) => { if (i) deps.append(document.createTextNode(', ')); deps.append(link(id, run.key, { kind: 'task', id })); });
        if (!deps.hasChildNodes()) deps.textContent = '—';
        return [link(task.id, run.key, { kind: 'task', id: task.id }), task.title, task.difficulty, task.wave, chip(task.status),
          task.dwarf, task.qa, task.attempt, chip(task.verdict), sev(task.findings), RM.fmtDur(RM.taskTimes(task, null, now()).busy || null), exploration, deps, task.branch];
      });
      const body = table(host, ['ID', 'Title', 'Difficulty', 'Wave', 'Status', 'Dwarf', 'QA', 'Attempt', 'Verdict', 'Findings', 'Busy', '* Explored / propensity', 'Deps', 'Branch'], rows, 'Tasks');
      Array.from(body.children).forEach((row, i) => {
        const id = run.tasks[i].id;
        row.classList.toggle('selected', !!state.sel && (state.sel.id === id || state.sel.task === id));
        row.addEventListener('click', event => {
          if (!event.target.closest('a') && !window.getSelection()?.toString()) select({ kind: 'task', id });
        });
      });
      scoped(host, run.key + ':');
    });
  }
  function renderInspector(run) {
    const host = $('inspector'); if (!host || !window.ForgePanel) return;
    const sel = state.sel || (run.kind === 'solo' ? { kind: 'task', id: list(run.tasks)[0]?.id } : null);
    preserve(host, () => {
      window.ForgePanel.render(host, {
        run, sel, now: now(),
        fetchJSON: path => fetchJSON(path).then(r => r.data),
        rerender: () => { const r = state.cache.get(state.key)?.data; if (r) renderInspector(r); },
        hash: s => hashFor(run.key, s), onSelect: s => select(s), diffHash: spec => diffHash(run.key, spec),
        onFractal: (id, data) => { if (state.stage && state.stage.setFractal) state.stage.setFractal(id, data); },
      });
      scoped(host, run.key + ':');
    });
  }
  function render2d(run) {
    const host = $('graph2d'); if (!host) return;
    const show2d = !use3d() || !state.stage;
    host.hidden = !show2d;
    if (!show2d) { host.replaceChildren(); return; }
    preserve(host, () => {
      const r = atReplay(run);
      try {
        if (state.stageMode === 'exec') window.ForgeGantt.render(host, run, { now: now(), onSelect: select, sel: state.sel, replay: state.replay, critical: state.overlay.critical ? RM.criticalPath(run, { now: now() }) : null, ghosts: state.overlay.ghosts });
        else window.ForgeGraph.render(host, r, { selected: state.sel && (state.sel.task || state.sel.id), onSelect: id => select({ kind: 'task', id }) });
      } catch (_) { host.replaceChildren(el('p', 'Graph unavailable. Select tasks in the table below.', 'muted')); }
    });
  }
  function renderStrip(run) {
    const host = $('strip'); if (!host) return;
    const show = state.mode === 'exec' && run.kind !== 'solo';
    host.hidden = !show;
    if (show) window.ForgeGantt.strip(host, run, { now: now(), replay: state.replay }); else host.replaceChildren();
  }
  // Files: which task touched which file, in the stage's place (the inspector stays beside it)
  function renderFiles(run) {
    const host = $('files-view'), view = $('run-view'); if (!host || !view) return;
    const open = state.mode === 'files' && !!window.ForgeFiles;
    view.classList.toggle('files-open', open); host.hidden = !open;
    if (!open) { host.filesSig = ''; return; }
    const c = state.files.get(run.key);
    if (!c || c.updated !== run.updated) loadFiles(run);
    if (!c || !c.data) {
      host.filesSig = '';
      host.replaceChildren(el('p', c && c.error ? `Could not load the files (${c.error}).` : 'Reading which files each task touched…', c && c.error ? 'warn' : 'muted'));
      return;
    }
    const sig = JSON.stringify([c.at, state.sel]);
    if (host.filesSig === sig) return;                     // a poll that changed nothing here redraws nothing
    host.filesSig = sig;
    window.ForgeFiles.render(host, c.data, { run, sel: state.sel, onSelectTask: id => select({ kind: 'task', id }),
      diffHref: (task, path) => diffHash(run.key, { kind: 'task', task, source: 'current', file: path }),
      prefs: state.filesPrefs, savePrefs: p => { state.filesPrefs = p; store.set('files', p); } });
  }
  function loadFiles(run) {
    const key = run.key, updated = run.updated, c = state.files.get(key) || {};
    if (c.pending === updated) return;
    c.pending = updated; state.files.set(key, c);
    fetchJSON(`/api/runs/${encodeURIComponent(key)}/files`).then(r => { c.data = r.data; c.error = null; c.at = Date.now(); }, e => { c.error = e.message; })
      .finally(() => { c.updated = updated; c.pending = null; const cur = state.cache.get(state.key)?.data; if (cur && cur.key === key) renderFiles(cur); });
  }
  // the diff viewer takes the stage's place while it is open
  function renderDiff(run) {
    const host = $('diff-view'), view = $('run-view'); if (!host || !view) return;
    const open = !!state.diff && !!window.ForgeDiff;
    view.classList.toggle('diff-open', open); host.hidden = !open;
    if (!open) { host.replaceChildren(); host.diffSig = ''; return; }
    window.ForgeDiff.render(host, { run, spec: state.diff, fetchJSON: path => fetchJSON(path).then(r => r.data),
      rerender: () => { const r = state.cache.get(state.key)?.data; if (r) renderDiff(r); },
      href: spec => diffHash(run.key, spec), closeHref: hashFor(run.key, state.sel) });
  }
  function renderRunBody(run) {
    renderDiff(run);
    syncToggles();
    syncReplay(run);
    placeStage($('stage-slot'));
    if (use3d() && state.stage) state.stage.showRun(run, { mode: state.stageMode, sel: state.sel });
    render2d(run);
    renderStrip(run);
    renderFiles(run);
    renderInspector(run);
  }
  function renderRun(run) {
    const main = $('main');
    if (main.dataset.view !== 'run' || main.dataset.run !== run.key) {
      stopPlay(); state.replay = null;
      if (state.stage) state.stage.setReplay(null);
      main.dataset.view = 'run'; main.dataset.run = run.key;
      main.replaceChildren();
      const head = el('section', undefined, 'run-header'); head.id = 'run-head'; main.append(head);
      const view = el('section', undefined, 'run-view'); view.id = 'run-view'; main.append(view);
      toolbar(view);
      const row = el('div', undefined, 'stage-row');
      const col = el('div', undefined, 'stage-col');
      const slot = el('div', undefined, 'stage-slot'); slot.id = 'stage-slot';
      const g2 = el('div', undefined, 'graph'); g2.id = 'graph2d'; g2.dataset.scroll = 'graph';
      const files = el('div', undefined, 'files-view'); files.id = 'files-view'; files.hidden = true;
      col.append(slot, g2, files);
      const insp = el('aside', undefined, 'inspector'); insp.id = 'inspector'; insp.setAttribute('aria-label', 'Inspector'); insp.tabIndex = -1;
      row.append(col, insp); view.append(row);
      const strip = el('div', undefined, 'strip-wrap'); strip.id = 'strip'; view.append(strip);
      const diff = el('section', undefined, 'diff-view'); diff.id = 'diff-view'; diff.hidden = true; view.append(diff);
      const tasks = el('section', undefined, 'run-tasks'); tasks.id = 'run-tasks'; main.append(tasks);
      window.scrollTo(0, 0);
    }
    ensureStage();
    renderHeader(run);
    renderRunBody(run);
    renderTable(run);
    announce(run);
  }
  function renderMain() {
    document.body.classList.toggle('cloud-mode', !!state.cloud);
    if (state.cloud) { renderCloud(); return; }
    if (state.models) { renderModels(); return; }
    if (!state.key) { renderMap(); return; }
    const cached = state.cache.get(state.key);
    if (cached) renderRun(cached.data);
  }
  // A polite summary of what changed since the last poll, for screen readers.
  function announce(run) {
    const prev = state.lastStatuses.get(run.key), cur = new Map(list(run.tasks).map(t => [t.id, t.status]));
    state.lastStatuses.set(run.key, cur);
    if (!prev || Date.now() - state.announced < 10000) return;
    const changes = [...cur].filter(([id, s]) => prev.has(id) && prev.get(id) !== s).map(([id, s]) => `${id} ${prev.get(id).toLowerCase()} → ${s.toLowerCase()}`);
    if (!changes.length) return;
    state.announced = Date.now();
    $('live').textContent = changes.slice(0, 5).join('; ');
  }

  /* ---------- data ---------- */
  async function fetchJSON(path) {
    if (SNAP) {
      const data = SNAP.api[path];
      if (data === undefined) { const error = new Error('not in this export'); error.status = 404; error.snapshot = true; throw error; }
      return { text: JSON.stringify(data), data };
    }
    const response = await fetch(path, { cache: 'no-store' });
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      throw error;
    }
    const text = await response.text();
    return { text, data: JSON.parse(text) };
  }
  // Coalesce polling and navigation requests for the same resource.
  function request(path, consume, label, onError) {
    if (state.pending.has(path)) return state.pending.get(path);
    const pending = fetchJSON(path).then(result => {
      consume(result);
      state.errors.delete(path);
    }).catch(error => {
      if (currentResource(path)) {
        state.errors.set(path, `${label}: ${error.message}. Any last good data is kept. Refresh retries while enabled and this tab is visible.`);
        onError?.(error);
      }
    }).finally(() => { state.pending.delete(path); renderErrors(); });
    state.pending.set(path, pending);
    return pending;
  }
  function loadList() {
    return request('/api/runs', result => {
      if (result.text === state.listText) return;
      state.runs = list(result.data.runs);
      state.listText = result.text;
      if (window.ForgeNotify) window.ForgeNotify.observe(state.runs);
      renderList();
      if (state.cloud) renderCloud();
      else if (!state.key) renderMap({ rebuildBar: $('map-filters') ? !$('map-filters').contains(document.activeElement) : true });
    }, 'Could not refresh runs');
  }
  function loadRun(key) {
    return request('/api/runs/' + encodeURIComponent(key), result => {
      const changed = state.cache.get(key)?.text !== result.text;
      state.cache.set(key, result);
      if (state.key === key && changed) renderRun(result.data);
    }, 'Could not refresh run', error => {
      if (state.key !== key || state.cache.has(key)) return;
      if (error.status === 404) state.errors.delete('/api/runs/' + encodeURIComponent(key));
      $('main').dataset.view = 'waiting';
      $('main').replaceChildren(el('p', error.status === 404 ?
        'Waiting for this run to appear… checking again while this tab is visible. If its directory was removed, pick another run.' :
        'Could not load this run. Select another run, or leave refresh enabled to retry.', 'empty'));
    });
  }
  function navigate() {
    let parsed = { key: null, sel: null };
    try { parsed = parseHash(location.hash); } catch (_) { /* invalid escapes fall back to the map */ }
    if (SNAP && parsed.key !== SNAP.meta.key) {               // an export shows its one run
      history.replaceState(null, '', '#/runs/' + encodeURIComponent(SNAP.meta.key));
      parsed = parseHash(location.hash);
    }
    const keyChanged = parsed.key !== state.key;
    state.key = parsed.key;
    state.sel = parsed.sel;
    const diffWas = JSON.stringify(state.diff || null);
    state.diff = parsed.diff || null;
    state.cloud = parsed.cloud || null;
    state.models = !!parsed.models;
    if (state.cloud && state.cloud.model) {                  // a model's runs: the cloud, filtered to it
      cloudFilter({ models: { aliases: [state.cloud.model], role: 'any' } });
      history.replaceState(null, '', '#/cloud'); state.cloud.model = null;
    }
    document.body.classList.toggle('cloud-mode', !!state.cloud);
    for (const a of document.querySelectorAll('.topbar .top-link')) {
      const here = a.getAttribute('href') === (state.cloud ? '#/cloud' : state.models ? '#/models' : !state.key ? '#/' : null);
      if (here) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    }
    renderErrors();
    markSelection();
    if (state.cloud) { renderCloud(); return; }
    if (state.models) { renderModels(); loadModels(); return; }
    if (!state.key) { renderMap(); return; }
    const cached = state.cache.get(state.key);
    if (cached) {
      if (keyChanged) renderRun(cached.data);
      else { renderRunBody(cached.data); renderTable(cached.data); }
    } else { $('main').dataset.view = 'loading'; $('main').replaceChildren(el('p', 'Loading run…', 'empty')); }
    loadRun(state.key);
    // a selection made on the stage keeps the keyboard there (← → walk the tasks); one made elsewhere moves it to the inspector
    if (state.diff) {
      const opened = diffWas === 'null' || JSON.stringify({ ...JSON.parse(diffWas), file: null }) !== JSON.stringify({ ...state.diff, file: null });
      if (opened && $('diff-view')) { $('run-view').scrollIntoView({ block: 'start' }); $('diff-view').focus({ preventScroll: true }); }
    } else if (state.sel && !keyChanged && !stageHost.contains(document.activeElement)) $('inspector')?.focus({ preventScroll: true });
  }
  function busy() {
    if (state.key) {
      const run = state.cache.get(state.key)?.data;
      return !!run && (list(run.tasks).some(t => t.status === 'RUNNING') || (run.final_review && run.final_review.state === 'running'));
    }
    return state.runs.some(r => r.counts && r.counts.RUNNING);
  }
  const background = () => document.visibilityState !== 'visible' && !!window.ForgeNotify && window.ForgeNotify.background();
  function refresh() {
    if (state.paused || SNAP) return;
    if (document.visibilityState !== 'visible') { if (background()) loadList(); return; }   // only to notify
    loadList();
    if (state.key) loadRun(state.key);
    if (state.models) loadModels();
    for (const node of document.querySelectorAll('[data-epoch]')) node.textContent = relative(node.dataset.epoch);
  }
  // Poll faster while something runs, slower otherwise; never with refresh off, and a hidden tab only to notify.
  function schedule() {
    clearTimeout(state.timer);
    if (SNAP) return;                                       // an export never changes
    state.timer = setTimeout(() => { refresh(); schedule(); }, background() ? 30000 : busy() ? 2000 : 5000);
  }
  // the quick jump's commands: what the page can do from anywhere
  function commands() {
    const run = state.key && state.cache.get(state.key)?.data;
    const out = [
      { id: 'cmd:cloud', label: 'Open the run cloud', sub: 'every run in 3D, grouped by project', ids: ['cloud'], href: '#/cloud' },
      { id: 'cmd:map', label: 'Open the map of all runs', sub: 'the front page', ids: ['map', 'home'], href: '#/' },
      { id: 'cmd:models', label: 'Open the model leaderboard', sub: 'pass rates, retries, time and tokens per model', ids: ['models', 'leaderboard'], href: '#/models' },
      { id: 'cmd:notify', label: 'Notifications', sub: 'what finished or failed, and browser notifications', ids: ['alerts', 'bell'], action: () => window.ForgeNotify && window.ForgeNotify.open() },
      { id: 'cmd:dim', label: state.dim === '3d' ? 'Switch to 2D' : 'Switch to 3D', sub: state.dim === '3d' ? 'graphs and charts, no WebGL' : 'loads three.js from cdn.jsdelivr.net', ids: ['2d', '3d'],
        action: () => { state.dim = state.dim === '3d' ? '2d' : '3d'; store.set('view', state.dim); if (state.dim === '3d') state.stageFailed = null; renderMain(); } },
      { id: 'cmd:refresh', label: state.paused ? 'Resume live refresh' : 'Pause live refresh', sub: 'polling of the runs', ids: ['refresh', 'pause'],
        action: () => { $('refresh').checked = state.paused; state.paused = !state.paused; refresh(); } },
    ];
    if (run && !SNAP) out.push({ id: 'cmd:export', label: 'Export this run as one HTML file…', sub: 'to share; opens without forge-ui', ids: ['export', 'share'], action: () => exportDialog(run) });
    if (SNAP) return out.filter(c => c.id === 'cmd:dim').concat(run ? [{ id: 'cmd:mode', label: state.mode === 'plan' ? 'Show the execution' : 'Show the planned waves', sub: '', ids: ['exec', 'plan'], action: () => setMode(state.mode === 'plan' ? 'exec' : 'plan') }] : []);
    if (run && window.ForgeFiles) out.push({ id: 'cmd:files', label: 'Show which tasks touched which files', sub: 'the Files view of this run', ids: ['files', 'heatmap'], action: () => setMode('files') });
    if (run) out.push({ id: 'cmd:mode', label: state.mode === 'plan' ? 'Show the execution' : 'Show the planned waves', sub: run.goal ? String(run.goal).split('\n')[0] : '', ids: ['exec', 'plan'], action: () => setMode(state.mode === 'plan' ? 'exec' : 'plan') });
    return out;
  }
  function cloudFilter(filter) {
    const f = window.ForgeCloudModel.normFilter(filter);
    state.cloudPrefs = { ...state.cloudPrefs, filter: f }; store.set('cloud', state.cloudPrefs);
    if (state.cloud && state.cloudUI) state.cloudUI.setFilter(f);
    if (!state.cloud) location.hash = '#/cloud';
  }
  // what an export holds: chosen each time, private texts and paths off by default
  let exportDlg = null;
  function exportDialog(run) {
    if (!exportDlg) {
      exportDlg = el('dialog', undefined, 'export-dialog'); exportDlg.setAttribute('aria-label', 'Export this run');
      document.body.append(exportDlg);
      exportDlg.addEventListener('click', e => { if (e.target === exportDlg) exportDlg.close(); });
    }
    const d = exportDlg; d.replaceChildren();
    d.append(el('h2', 'Export this run as one HTML file'),
      el('p', 'The file opens in any browser without forge-ui: the run in 2D, and in 3D when it can reach cdn.jsdelivr.net. It holds the goal, task titles, statuses, models, timings, and each finding\'s headline.', 'muted'));
    const opt = (label, help, checked) => { const l = el('label', undefined, 'export-opt'), c = el('input'); c.type = 'checkbox'; c.checked = checked; const t = el('span'); t.append(el('strong', label), el('span', help, 'muted')); l.append(c, t); d.append(l); return c; };
    const texts = opt('Include prompts, diffs and reports', 'Prompts, reviewer reports, logs, finding details and diffs can contain code, secrets and private notes.', false);
    const hide = opt('Hide local paths', 'Paths on this machine are reduced to their last folder or file name.', true);
    const note = el('p', '', 'warn export-note');
    const go = el('a', 'Download', 'button primary'); go.setAttribute('download', '');
    const sync = () => {
      go.href = `/api/runs/${encodeURIComponent(run.key)}/export?texts=${texts.checked ? 1 : 0}&paths=${hide.checked ? 0 : 1}`;
      note.textContent = texts.checked ? 'Share this file only with people who may read this repository\'s code.' : '';
    };
    texts.addEventListener('change', sync); hide.addEventListener('change', sync); sync();
    const cancel = el('button', 'Cancel'); cancel.type = 'button'; cancel.addEventListener('click', () => d.close());
    go.addEventListener('click', () => setTimeout(() => d.close(), 300));
    const row = el('div', undefined, 'export-actions'); row.append(cancel, go);
    d.append(note, row);
    d.showModal(); go.focus();
  }
  function snapshotBanner() {
    const m = SNAP.meta, b = el('div', undefined, 'snapshot-banner'); b.setAttribute('role', 'note');
    const when = Number.isFinite(m.generated) ? new Date(m.generated * 1000).toLocaleString() : 'an unknown date';
    b.append(el('strong', 'Exported run'), document.createTextNode(` · from forge-ui ${m.app || ''} on ${when} · ${m.texts ? 'prompts, diffs and reports included' : 'prompts, diffs and reports left out'} · ${m.paths ? 'local paths shown' : 'local paths hidden'}${(m.omitted || []).length ? ` · ${m.omitted.length} large item${m.omitted.length === 1 ? '' : 's'} left out` : ''}`));
    document.querySelector('.topbar').after(b);
  }
  function init() {
    if (SNAP) { document.body.classList.add('snapshot'); snapshotBanner(); $('refresh').checked = false; }
    if (window.ForgeSearch) window.ForgeSearch.init({ runs: () => state.runs, commands, go: href => { location.hash = href; }, cloudFilter });
    if (window.ForgeNotify) window.ForgeNotify.init({ go: href => { location.hash = href; } });
    $('filter').addEventListener('input', event => { state.mapFilter.text = event.target.value; filterChanged(true); });
    $('refresh').addEventListener('change', event => { state.paused = !event.target.checked; refresh(); });
    window.addEventListener('hashchange', navigate);
    document.addEventListener('visibilitychange', refresh);
    window.addEventListener('resize', () => { const run = !state.cloud && state.cache.get(state.key)?.data; if (run && (!use3d() || !state.stage || state.stageMode === 'exec')) { render2d(run); renderStrip(run); } });
    loadList();
    navigate();
    schedule();
  }
  init();
  return { refresh, state };
})();
