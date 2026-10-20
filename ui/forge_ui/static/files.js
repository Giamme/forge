/* forge-ui files heatmap: which tasks planned, touched (attempt by attempt), drifted into or conflicted on which
   files of a run, as a matrix of files (grouped under their directories) by tasks (grouped by wave), with the
   overlaps, drift, conflicts and deferrals listed under it. Reads GET /api/runs/<key>/files (api.md, Files).
   window.ForgeFiles = { matrix(data, run, opts), describe(row, column, cell), normPrefs(prefs), render(host, data, ctx) };
   under node:test the same object, where only render needs a DOM. SVG drawn with classes only (style.css colours
   it: the page CSP refuses style attributes). A solo run has one column, `solo`; the files its repo had already
   changed when it started (existing.diff) are `existing` cells, drawn like planned ones but dashed, and the
   planned switch shows or hides both. */
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeFiles = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SORTS = ['path', 'heat', 'tasks'];
  const KINDS = ['planned', 'touched', 'drift', 'conflict'];
  const ROW_CAP = 600, COLLAPSED_CAP = 1000;
  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const clean = p => typeof p === 'string' ? p.replace(/\/+$/, '') : '';
  const paths = v => list(v).map(clean).filter(Boolean);
  const dirOf = p => p.slice(0, Math.max(0, p.lastIndexOf('/')));
  const byName = (a, b) => a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const blank = () => ({ planned: false, via: null, existing: false, touched: 0, attempts: [], current: false, drift: false, conflict: false, overlap: false, with: [], n: 0 });

  function normPrefs(p) {
    const o = p && typeof p === 'object' ? p : {}, s = o.show && typeof o.show === 'object' ? o.show : {};
    const show = {};
    for (const k of KINDS) show[k] = s[k] !== false;
    return { sort: SORTS.includes(o.sort) ? o.sort : 'path', show,
      collapsed: [...new Set(list(o.collapsed).filter(x => typeof x === 'string' && x))].slice(-COLLAPSED_CAP) };
  }

  // Columns: the run's tasks (titles, tasks.tsv order) and the files endpoint's, by wave (no wave last).
  function columnsOf(data, run) {
    const cols = new Map();
    const put = (t, i, fromRun) => {
      if (!t || typeof t !== 'object' || t.id == null) return;
      const id = String(t.id);
      if (!cols.has(id)) cols.set(id, { id, wave: null, status: null, title: '', planned_all: false, order: (fromRun ? 0 : 1e6) + i });
      const c = cols.get(id);
      if (c.wave === null) c.wave = num(t.wave);
      if (!c.status && t.status) c.status = String(t.status);
      if (!c.title && fromRun && t.title) c.title = String(t.title);
      if (!fromRun) c.planned_all = c.planned_all || !!t.planned_all || list(t.planned).includes('.');
    };
    list(run && run.tasks).forEach((t, i) => put(t, i, true));
    list(data && data.tasks).forEach((t, i) => put(t, i, false));
    return [...cols.values()].sort((a, b) => (a.wave ?? Infinity) - (b.wave ?? Infinity) || a.order - b.order)
      .map(c => ({ id: c.id, wave: c.wave, status: c.status || 'PENDING', title: c.title, planned_all: c.planned_all }));
  }

  // Two tasks of one wave that both planned or touched a path overlap (forge-schedule keeps those apart).
  function markOverlaps(cells, waveOf) {
    const byWave = new Map();
    for (const [id, c] of cells) {
      const w = waveOf.get(id);
      if (w == null || !(c.planned || c.touched || c.drift || c.conflict)) continue;
      if (!byWave.has(w)) byWave.set(w, []);
      byWave.get(w).push(id);
    }
    for (const ids of byWave.values()) {
      if (ids.length < 2) continue;
      for (const id of ids) { const c = cells.get(id); c.overlap = true; for (const x of ids) if (x !== id && !c.with.includes(x)) c.with.push(x); }
    }
  }

  function mask(c, show) {
    const m = { planned: show.planned && c.planned, via: null, existing: show.planned && c.existing,
      touched: show.touched ? c.touched : 0, attempts: show.touched ? c.attempts : [], current: show.touched && c.current,
      drift: show.drift && c.drift, conflict: show.conflict && c.conflict, overlap: c.overlap, with: c.with };
    if (m.planned) m.via = c.via;
    return m.planned || m.existing || m.touched || m.drift || m.conflict ? m : null;
  }

  /* The matrix of files × tasks. opts: {filter, sort, collapsed, show} (see normPrefs). Cells are sparse: a row's
     `cells` holds only the tasks with something to show there. `show` switches a kind of mark off: cells lose it
     and files left with nothing are dropped. waves[].from/to are column indices, `to` exclusive (like slice). */
  function matrix(data, run, opts) {
    const o = opts && typeof opts === 'object' ? opts : {};
    const sort = SORTS.includes(o.sort) ? o.sort : 'path';
    const show = {};
    for (const k of KINDS) show[k] = !(o.show && o.show[k] === false);
    const needle = typeof o.filter === 'string' ? o.filter.trim().toLowerCase() : '';
    const collapsed = new Set(o.collapsed instanceof Set ? o.collapsed : list(o.collapsed));
    const columns = columnsOf(data, run), waveOf = new Map(columns.map(c => [c.id, c.wave]));
    const waves = [];
    columns.forEach((c, i) => {
      const w = waves[waves.length - 1];
      if (w && w.wave === c.wave) w.to = i + 1; else waves.push({ wave: c.wave, from: i, to: i + 1 });
    });

    const T = new Map(), scopes = new Map(), alls = [], where = new Map(), near = new Map();
    const plan = (scope, id) => {
      scope = clean(scope);
      if (!scope || scope === '.') return;
      if (!scopes.has(scope)) scopes.set(scope, new Set());
      scopes.get(scope).add(id);
    };
    const addPath = p => { if (!where.has(p)) where.set(p, new Set()); return where.get(p); };
    for (const t of list(data && data.tasks)) {
      if (!t || typeof t !== 'object' || t.id == null || T.has(String(t.id))) continue;
      const f = { id: String(t.id), tries: new Map(), current: new Set(paths(t.current)), drift: new Set(paths(t.drift)),
        conflict: new Set(paths(t.conflicts)), existing: new Set(paths(t.existing)) };
      for (const s of paths(t.planned)) plan(s, f.id);
      if (t.planned_all || list(t.planned).includes('.')) alls.push(f.id);
      for (const a of list(t.attempts)) {
        for (const p of new Set(paths(a && a.files))) {
          if (!f.tries.has(p)) f.tries.set(p, []);
          f.tries.get(p).push(num(a.attempt));
        }
      }
      T.set(f.id, f);
    }
    for (const f of list(data && data.files)) {
      const p = clean(f && f.path);
      if (!p) continue;
      const by = addPath(p);
      for (const id of list(f.planned_by)) by.add(String(id));
    }
    for (const f of T.values()) {
      for (const set of [f.tries.keys(), f.current, f.drift, f.conflict, f.existing]) {
        for (const p of set) {
          addPath(p);
          if (!near.has(p)) near.set(p, new Set());
          near.get(p).add(f.id);
        }
      }
    }
    const plannedDirs = [];
    for (const d of list(data && data.planned_dirs)) {
      const p = clean(d && d.path);
      if (!p || p === '.') continue;
      for (const id of list(d.planned_by)) plan(p, String(id));
      plannedDirs.push(p);
    }
    // the tasks whose plan covers a path (the path itself, a dir above it, or `.`), with the entry that does
    const plannersOf = p => {
      const out = new Map();
      for (let a = p; a; a = dirOf(a)) for (const id of scopes.get(a) || []) if (!out.has(id)) out.set(id, a);
      for (const id of alls) if (!out.has(id)) out.set(id, '.');
      return out;
    };

    const counts = { files: where.size, shown: 0, hidden: 0, filtered: 0, dirs: 0, tasks: columns.length,
      planned: 0, existing: 0, touched: 0, drift: 0, conflict: 0, overlap: 0 };
    const nodes = new Map(), top = { path: '', name: '', dirs: [], leaves: [] };
    nodes.set('', top);
    const dirAt = p => {
      if (nodes.has(p)) return nodes.get(p);
      const parent = dirAt(dirOf(p)), node = { path: p, name: p.slice(p.lastIndexOf('/') + 1) || p, dirs: [], leaves: [] };
      parent.dirs.push(node); nodes.set(p, node);
      return node;
    };
    let max = 0;
    for (const [p, apiBy] of where) {
      const planners = plannersOf(p);
      for (const id of apiBy) if (!planners.has(id)) planners.set(id, null);
      const raw = new Map();
      for (const id of new Set([...planners.keys(), ...(near.get(p) || [])])) {
        if (!waveOf.has(id)) continue;
        const t = T.get(id), tries = t && t.tries.get(p) || [], current = !!t && t.current.has(p);
        raw.set(id, { planned: planners.has(id), via: planners.get(id) ?? null, existing: !!t && t.existing.has(p),
          touched: tries.length || (current ? 1 : 0), attempts: tries.slice().sort((x, y) => (x ?? Infinity) - (y ?? Infinity)),
          current, drift: !!t && t.drift.has(p), conflict: !!t && t.conflict.has(p), overlap: false, with: [] });
      }
      markOverlaps(raw, waveOf);
      const cells = Object.create(null);
      let heat = 0, ntasks = 0;
      for (const [id, c] of raw) {
        const m = mask(c, show);
        if (m) { cells[id] = m; heat += m.touched; ntasks++; }
      }
      if (!ntasks) { counts.hidden++; continue; }
      if (needle && !p.toLowerCase().includes(needle)) { counts.filtered++; continue; }
      counts.shown++;
      for (const id in cells) {
        const c = cells[id];
        max = Math.max(max, c.touched);
        for (const k of ['planned', 'existing', 'drift', 'conflict', 'overlap']) if (c[k]) counts[k]++;
        if (c.touched) counts.touched++;
      }
      dirAt(dirOf(p)).leaves.push({ kind: 'file', path: p, name: p.slice(p.lastIndexOf('/') + 1) || p, cells, heat, ntasks });
    }
    if (show.planned) for (const p of plannedDirs) if (!needle || p.toLowerCase().includes(needle)) dirAt(p);

    // dirs aggregate what is under them (max heat, any flag) plus their own plan; empty ones drop out
    (function agg(node) {
      const cells = new Map(), kids = [];
      const into = id => cells.get(id) || cells.set(id, blank()).get(id);
      const merge = (a, c, n) => {
        a.planned = a.planned || c.planned; a.existing = a.existing || c.existing; a.touched = Math.max(a.touched, c.touched);
        a.current = a.current || c.current; a.drift = a.drift || c.drift; a.conflict = a.conflict || c.conflict; a.n += n;
        if (c.overlap) { a.overlap = true; for (const x of c.with) if (!a.with.includes(x)) a.with.push(x); }
      };
      node.files = 0; node.heat = 0; node.ntasks = 0;
      for (const d of node.dirs) {
        agg(d);
        if (!d.files && !d.cells.size) continue;
        kids.push(d); node.files += d.files; node.heat = Math.max(node.heat, d.heat); node.ntasks = Math.max(node.ntasks, d.ntasks);
        for (const [id, c] of d.cells) merge(into(id), c, c.n);
      }
      for (const f of node.leaves) {
        kids.push(f); node.files++; node.heat = Math.max(node.heat, f.heat); node.ntasks = Math.max(node.ntasks, f.ntasks);
        for (const id in f.cells) merge(into(id), f.cells[id], 1);
      }
      if (node.path && show.planned) {
        const own = new Map();
        for (const [id, via] of plannersOf(node.path)) {
          if (!waveOf.has(id)) continue;
          const c = into(id);
          c.planned = true; c.via = via; own.set(id, c);
        }
        markOverlaps(own, waveOf);
      }
      node.cells = cells; node.kids = kids;
    })(top);

    const rows = [], dirs = [];
    const keyOf = sort === 'heat' ? r => r.heat : sort === 'tasks' ? r => r.ntasks : () => 0;
    const order = (a, b) => keyOf(b) - keyOf(a) || (a.kind === b.kind ? 0 : a.kind === 'file' ? 1 : -1) || byName(a, b);
    const dict = m => { const o = Object.create(null); for (const [id, c] of m) o[id] = c; return o; };
    (function walk(node, depth, open) {
      for (const k of node.kids.slice().sort(order)) {
        if (k.kind === 'file') {
          if (open) rows.push({ kind: 'file', path: k.path, name: k.name, depth, dir: node.path, collapsed: false, files: 1, cells: k.cells, heat: k.heat, ntasks: k.ntasks });
          continue;
        }
        // a chain of dirs that hold nothing but one dir each is one row (src/main/java)
        let d = k, name = k.name;
        while (d.kids.length === 1 && d.kids[0].kind !== 'file') { d = d.kids[0]; name += '/' + d.name; }
        const shut = collapsed.has(d.path);
        dirs.push(d.path);
        if (open) rows.push({ kind: 'dir', path: d.path, name, depth, dir: node.path, collapsed: shut, files: d.files, cells: dict(d.cells), heat: d.heat, ntasks: d.ntasks });
        walk(d, depth + 1, open && !shut);
      }
    })(top, 0, true);
    counts.dirs = dirs.length;
    return { columns, waves, rows, dirs, max, counts };
  }

  // The words for one cell, e.g. "reader touched ui/reader.py in attempts 1, 2 and its current diff · planned".
  function describe(row, col, cell) {
    const id = col && col.id != null ? String(col.id) : '?', c = cell || blank(), bits = [];
    const planned = () => c.via === '.' ? 'planned (the whole repo)' : c.via && c.via !== row.path ? `planned as ${c.via}/` : 'planned';
    let head;
    if (row.kind === 'dir') {
      head = `${id}: ${c.n || 0} of ${plural(row.files || 0, 'file')} under ${row.path}/`;
      if (c.touched) bits.push(`touched in up to ${plural(c.touched, 'attempt')}`);
      if (c.planned) bits.push(c.via ? planned() : 'planned files under it');
      if (c.existing) bits.push('files already changed before the run');
    } else if (c.touched) {
      const tries = list(c.attempts);
      head = `${id} touched ${row.path} ` + (tries.length ? `in attempt${tries.length > 1 ? 's' : ''} ${tries.map(n => n ?? '?').join(', ')}${c.current ? ' and its current diff' : ''}`
        : 'in its current diff (not reviewed yet)');
      if (c.planned) bits.push(planned());
    } else if (c.planned) {
      head = `${id} planned ${row.path}`;
      if (c.via && c.via !== row.path) bits.push(c.via === '.' ? 'the whole repo (.)' : `as ${c.via}/`);
      bits.push('not touched by its diffs');
    } else if (c.existing) {
      head = `${id}: ${row.path} already had uncommitted changes when the run started (existing.diff)`;
    } else head = `${id} · ${row.path}`;
    if (c.existing && row.kind !== 'dir' && (c.touched || c.planned)) bits.push('already changed before the run (existing.diff)');
    if (c.drift) bits.push('drift: changed without being declared');
    if (c.conflict) bits.push('conflicted on merge');
    if (c.overlap) bits.push(`same wave${col && col.wave != null ? ' (' + col.wave + ')' : ''} as ${list(c.with).join(', ') || 'another task'}`);
    return [head, ...bits].join(' · ');
  }

  /* ---------- the view (page only) ---------- */
  const NS = 'http://www.w3.org/2000/svg';
  const CELL = 20, ROW = 20, BOX = 14, IND = 14, CH = 7.25, HCH = 6.65, TILT = 55, LABEL_MAX = 380;
  const HELP = {
    planned: 'Outlined: the task planned this file (its files column names it, a directory above it, or . for the whole repo)',
    touched: 'Filled: the task\'s diff changed this file; darker means more of its reviewed attempts did',
    drift: 'Hatched: the task\'s diff changed this file without declaring it (drift.txt)',
    conflict: 'Red border: this file conflicted when the task was merged (conflict.files)',
  };
  const views = new Map();   // run key -> {filter, pop}: what a re-render keeps for the session
  const hosts = new WeakMap(); // host -> the view drawn in it: {sig, prefs, root, setCtx, handle}
  let uid = 0, openPop = null, watching = false, memo = null, handled = null;

  function el(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined && text !== null) n.textContent = String(text);
    if (cls) n.className = cls;
    return n;
  }
  function svg(name, attrs, parent) {
    const e = document.createElementNS(NS, name);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (parent) parent.append(e);
    return e;
  }
  const stc = s => 'st-' + String(s || 'pending').toLowerCase().replace(/[^a-z0-9_-]/g, '');
  const selTask = sel => !sel ? null : sel.kind === 'task' ? sel.id ?? null : sel.kind === 'execution' ? sel.task ?? null : null;
  const fmt = n => Number(n).toLocaleString('en');
  const clip = (s, n) => s.length <= n ? s : s.slice(0, Math.ceil((n - 1) / 2)) + '…' + s.slice(s.length - Math.floor((n - 1) / 2));
  const shown = (e, on) => e.setAttribute('visibility', on ? 'visible' : 'hidden');

  // The prefs this view works from: ctx.prefs, unless it is what we were handed last time or what we saved since
  // (then ours): a page that hands back stale prefs does not undo the viewer's changes.
  const json = v => { try { return JSON.stringify(v ?? null); } catch (_) { return ''; } };
  function prefsOf(ctx) {
    const seen = json(ctx.prefs);
    if (!memo || (seen !== memo.seen && seen !== memo.saved)) memo = { seen, saved: null, prefs: normPrefs(ctx.prefs) };
    return memo.prefs;
  }
  // What a render draws from, but the selection: the same again keeps the DOM (a live refresh, a new selection).
  const sigOf = (data, run) => json(data) + json(list(run && run.tasks).map(t => t && [t.id, t.status, t.title, t.wave]));

  // One listener for the page: a click outside the cells and the diff link closes the link.
  function watch() {
    if (watching) return;
    watching = true;
    document.addEventListener('click', e => {
      const t = e.target;
      if (openPop && e !== handled && !(t && t.closest && t.closest('.fm-pop'))) { const close = openPop; openPop = null; close(); }
    });
  }

  function mark(g, bx, by, c, max, ids) {
    if (c.touched) svg('rect', { class: 'fm-touch', x: bx + 2, y: by + 2, width: BOX - 4, height: BOX - 4, rx: 1.5, 'fill-opacity': (0.3 + 0.7 * Math.min(1, c.touched / (max || 1))).toFixed(2) }, g);
    if (c.drift) svg('rect', { class: 'fm-drift', x: bx, y: by, width: BOX, height: BOX, rx: 2, fill: `url(#${ids.hatch})` }, g);
    if (c.planned || c.existing) svg('rect', { class: c.planned ? 'fm-plan' : 'fm-exist', fill: 'transparent', x: bx + 0.5, y: by + 0.5, width: BOX - 1, height: BOX - 1, rx: 2 }, g);
    if (c.conflict) svg('rect', { class: 'fm-conflict', fill: 'none', x: bx - 1.5, y: by - 1.5, width: BOX + 3, height: BOX + 3, rx: 3 }, g);
    if (c.overlap) svg('circle', { class: 'fm-overlap', cx: bx + BOX - 0.5, cy: by + 0.5, r: 2.8 }, g);
  }
  function swatch(c, ids) {
    const s = svg('svg', { class: 'fm-swatch', width: 18, height: 18, viewBox: '0 0 18 18', 'aria-hidden': 'true', focusable: 'false' });
    mark(s, 2, 2, Object.assign(blank(), c), c.max || 1, ids);
    return s;
  }

  /* render(host, data, ctx): ctx = {run, sel, onSelectTask(id), diffHref(taskId, path), prefs, savePrefs(prefs)}.
     Returns {select(sel)}, which moves the highlighted column without rebuilding. */
  function render(host, data, ctx) {
    ctx = ctx || {};
    watch();
    const run = ctx.run && typeof ctx.run === 'object' ? ctx.run : null;
    const sig = sigOf(data, run), was = hosts.get(host);
    if (was && was.sig === sig && was.prefs === prefsOf(ctx) && was.root.parentNode === host) {
      was.setCtx(ctx); was.handle.select(ctx.sel);
      return was.handle;
    }
    const key = String(data && data.run || run && run.key || '');
    const V = views.get(key) || { filter: '', pop: null };
    views.delete(key); views.set(key, V);
    if (views.size > 40) views.delete(views.keys().next().value);
    const prefs = prefsOf(ctx);
    const save = () => { const copy = JSON.parse(JSON.stringify(prefs)); memo.saved = json(copy); if (ctx.savePrefs) ctx.savePrefs(copy); };
    // what the last render of this host had: its scroll position and focus
    const before = host.querySelector('.fm-scroll'), active = document.activeElement;
    const keep = { scroll: before ? [before.scrollLeft, before.scrollTop] : null, focus: null, caret: null };
    if (active && host.contains(active)) {
      keep.focus = active.getAttribute('data-fm-focus');
      if (keep.focus === 'filter') keep.caret = [active.selectionStart, active.selectionEnd];
    }
    let sel = selTask(ctx.sel), live = null;
    host.replaceChildren();
    const root = el('div', undefined, 'fm');
    host.append(root);
    const handle = { select(s) { sel = selTask(s); if (live) live.setSel(sel); } };
    hosts.set(host, { sig, prefs, root, handle, setCtx: c => { ctx = c; } });
    if (!data || typeof data !== 'object') {
      root.append(el('p', 'No file information for this run yet.', 'fm-empty'));
      return handle;
    }
    const id = ++uid, ids = { hatch: `fm-hatch-${id}`, zebra: `fm-zebra-${id}` };
    const defs = svg('svg', { class: 'fm-defs', width: 0, height: 0, 'aria-hidden': 'true', focusable: 'false' });
    const d = svg('defs', {}, defs);
    const hatch = svg('pattern', { id: ids.hatch, patternUnits: 'userSpaceOnUse', width: 4, height: 4, patternTransform: 'rotate(45)' }, d);
    svg('rect', { class: 'fm-hatch-bg', width: 4, height: 4, fill: 'none' }, hatch);
    svg('line', { class: 'fm-hatch-line', x1: 1, y1: 0, x2: 1, y2: 4 }, hatch);
    const zebra = svg('pattern', { id: ids.zebra, patternUnits: 'userSpaceOnUse', width: 8, height: ROW * 2 }, d);
    svg('rect', { class: 'fm-zebra', x: 0, y: ROW, width: 8, height: ROW }, zebra);
    root.append(defs);

    /* ---------- the toolbar: filter, sort, switches (which are the legend), counts ---------- */
    const bar = el('div', undefined, 'filters fm-bar');
    const fl = el('label', undefined, 'filter');
    const input = el('input', undefined, 'fm-filter');
    input.type = 'search'; input.placeholder = 'Filter paths…'; input.autocomplete = 'off'; input.spellcheck = false; input.value = V.filter;
    input.setAttribute('data-fm-focus', 'filter');
    fl.append(el('span', 'Path'), input);
    const sl = el('label', undefined, 'filter');
    const sortSel = el('select', undefined, 'fm-sort');
    for (const [v, t] of [['path', 'path'], ['heat', 'most touched'], ['tasks', 'most tasks']]) { const o = el('option', t); o.value = v; sortSel.append(o); }
    sortSel.value = prefs.sort; sortSel.setAttribute('data-fm-focus', 'sort');
    sl.append(el('span', 'Sort'), sortSel);
    bar.append(fl, sl);
    const toggles = el('div', undefined, 'fm-toggles');
    toggles.setAttribute('role', 'group'); toggles.setAttribute('aria-label', 'Show');
    const solo = list(data.tasks).length === 1 && data.tasks[0] && Array.isArray(data.tasks[0].existing);
    for (const k of KINDS) {
      const lab = el('label', undefined, 'filter check fm-toggle'), cb = el('input'), existing = solo && k === 'planned';
      cb.type = 'checkbox'; cb.checked = prefs.show[k]; cb.setAttribute('data-fm-focus', 'show-' + k);
      cb.addEventListener('change', () => { prefs.show[k] = cb.checked; save(); draw(); });
      lab.title = existing ? 'Dashed: the repo already had uncommitted changes to this file when the run started (existing.diff)' : HELP[k];
      lab.append(cb, swatch({ planned: k === 'planned' && !existing, existing, touched: k === 'touched' ? 1 : 0, drift: k === 'drift', conflict: k === 'conflict' }, ids), el('span', existing ? 'existing' : k));
      toggles.append(lab);
    }
    const tools = el('div', undefined, 'fm-tools');
    const btn = (text, focus, onClick) => { const b = el('button', text); b.type = 'button'; b.setAttribute('data-fm-focus', focus); b.addEventListener('click', onClick); return b; };
    tools.append(btn('Collapse all', 'collapse', () => {
      const M = live && live.M;
      if (!M) return;
      prefs.collapsed = [...new Set(prefs.collapsed.concat(M.dirs))].slice(-COLLAPSED_CAP); save(); draw();
    }), btn('Expand all', 'expand', () => {
      const M = live && live.M, mine = new Set(M ? M.dirs : []);
      prefs.collapsed = prefs.collapsed.filter(p => !mine.has(p)); save(); draw();
    }));
    bar.append(toggles, tools);
    const legend = el('div', undefined, 'fm-legend');
    const key1 = el('span', undefined, 'fm-key');
    key1.append(swatch({ touched: 1, overlap: true }, ids), el('span', 'another task of the same wave planned or touched it too'));
    const key2 = el('span', undefined, 'fm-key');
    const ramp = svg('svg', { class: 'fm-ramp', width: 58, height: 18, viewBox: '0 0 58 18', 'aria-hidden': 'true', focusable: 'false' });
    for (let i = 0; i < 4; i++) mark(ramp, 2 + i * 14, 2, { touched: i + 1 }, 4, ids);
    const rampMax = el('span', 'more attempts');
    key2.append(el('span', 'touched in 1 attempt'), ramp, rampMax);
    const key3 = el('span', undefined, 'fm-key');
    const stMark = svg('svg', { class: 'fm-swatch', width: 12, height: 18, viewBox: '0 0 12 18', 'aria-hidden': 'true', focusable: 'false' });
    svg('rect', { class: 'fm-st st-running', x: 2, y: 5, width: 8, height: 8, rx: 2 }, stMark);
    key3.append(stMark, el('span', 'the task\'s status'));
    legend.append(key1, key2, key3);
    if (list(data.tasks).some(t => t && list(t.existing).length)) {
      const key4 = el('span', undefined, 'fm-key');
      key4.append(swatch({ existing: true }, ids), el('span', 'already changed in the repo when the run started (existing.diff)'));
      legend.append(key4);
    }
    const count = el('p', '', 'fm-count');
    count.setAttribute('aria-live', 'polite');
    root.append(bar, legend, count);
    if (data.truncated) root.append(el('p', 'The server cut this run short: it lists at most 2,000 files, 300 tasks and 5,000 overlaps.', 'warn fm-note'));
    const body = el('div', undefined, 'fm-body');
    root.append(body, lists());

    let timer = 0;
    input.addEventListener('input', () => { V.filter = input.value; clearTimeout(timer); timer = setTimeout(draw, 120); });
    input.addEventListener('search', () => { clearTimeout(timer); V.filter = input.value; draw(); });
    sortSel.addEventListener('change', () => { prefs.sort = sortSel.value; save(); draw(); });
    if (keep.focus) {
      const t = [...bar.querySelectorAll('[data-fm-focus]')].find(e => e.getAttribute('data-fm-focus') === keep.focus);
      if (t) { t.focus({ preventScroll: true }); if (keep.caret) try { t.setSelectionRange(keep.caret[0], keep.caret[1]); } catch (_) { /* not a text input */ } }
    }
    draw(keep.focus, keep.scroll);
    return handle;

    /* ---------- the matrix: four panes so the header and the paths stay put while it scrolls ---------- */
    function draw(want, scrollTo) {
      const old = body.querySelector('.fm-scroll');
      if (!scrollTo && old) scrollTo = [old.scrollLeft, old.scrollTop];
      if (want === undefined && document.activeElement && body.contains(document.activeElement)) want = document.activeElement.getAttribute('data-fm-focus');
      const M = matrix(data, run, { filter: V.filter, sort: prefs.sort, collapsed: prefs.collapsed, show: prefs.show });
      live = { M, setSel() {} };
      rampMax.textContent = M.max > 1 ? `… ${M.max} attempts` : '… more attempts';
      body.replaceChildren();
      const c0 = M.counts;
      count.textContent = `${fmt(c0.files)} ${c0.files === 1 ? 'file' : 'files'} × ${fmt(c0.tasks)} ${c0.tasks === 1 ? 'task' : 'tasks'}` +
        (c0.shown !== c0.files ? ` · ${fmt(c0.shown)} shown` : '') + (c0.dirs ? ` · ${fmt(c0.dirs)} ${c0.dirs === 1 ? 'directory' : 'directories'}` : '');
      if (!M.columns.length) { body.append(el('p', 'This run has no tasks yet.', 'fm-empty')); return; }
      if (!M.rows.length) {
        body.append(el('p', !c0.files ? 'No files yet: no task planned a file and no diff touched one.' :
          c0.filtered && V.filter.trim() ? `No file path contains “${V.filter.trim()}”.` : 'Nothing to show with these switches: turn a kind of mark back on.', 'fm-empty'));
        return;
      }
      const cols = M.columns, rows = M.rows.slice(0, ROW_CAP), nc = cols.length, nr = rows.length;
      const colIx = new Map(cols.map((c, i) => [c.id, i])), rowIx = new Map(rows.map((r, i) => [r.path, i]));
      const longest = Math.min(24, cols.reduce((m, c) => Math.max(m, c.id.length), 3)) * HCH;
      const rad = TILT * Math.PI / 180, WAVE_H = 24, HH = WAVE_H + Math.ceil(longest * Math.sin(rad)) + 28;
      const CW = Math.max(nc * CELL + Math.ceil(longest * Math.cos(rad)) + 12, 200), BH = nr * ROW + 4;
      // the paths stay put while the cells scroll, so on a narrow screen they get at most 45% of it
      const avail = body.clientWidth, labelMax = avail ? Math.max(160, Math.min(LABEL_MAX, Math.floor(avail * 0.45))) : LABEL_MAX;
      const counts = rows.map(r => r.kind === 'dir' ? String(r.files) : '');
      const names = rows.map((r, i) => {
        const room = labelMax - 30 - r.depth * IND - (counts[i].length + 1) * CH;
        return clip(r.kind === 'dir' ? r.name + '/' : r.name, Math.max(8, Math.floor(room / CH)));
      });
      const LW = Math.min(labelMax, Math.max(Math.min(200, labelMax), Math.ceil(rows.reduce((m, r, i) => Math.max(m, 30 + r.depth * IND + (names[i].length + counts[i].length + 1) * CH), 0))));

      const scroll = el('div', undefined, 'fm-scroll'), sheet = el('div', undefined, 'fm-sheet');
      const headRow = el('div', undefined, 'fm-headrow'), bodyRow = el('div', undefined, 'fm-bodyrow');
      const corner = svg('svg', { class: 'fm-corner', width: LW, height: HH, 'aria-hidden': 'true', focusable: 'false' });
      const head = svg('svg', { class: 'fm-head', width: CW, height: HH, role: 'group', 'aria-label': 'Tasks by wave: select one' });
      const labels = svg('svg', { class: 'fm-labels', width: LW, height: BH, role: 'group', 'aria-label': 'Files and directories' });
      const grid = svg('svg', { class: 'fm-grid', width: CW, height: BH, role: 'group', 'aria-label': 'Cells: arrow keys move between them, Enter selects the task and offers its diff' });
      headRow.append(corner, head); bodyRow.append(labels, grid); sheet.append(headRow, bodyRow); scroll.append(sheet); body.append(scroll);

      svg('text', { class: 'fm-axis', x: 12, y: HH - 9 }, corner).textContent = 'files ↓';
      svg('text', { class: 'fm-axis', x: LW - 10, y: HH - 9, 'text-anchor': 'end' }, corner).textContent = 'tasks →';
      // the header: wave bands and labels, then one button per task
      M.waves.forEach((w, i) => {
        const x = w.from * CELL, wd = (w.to - w.from) * CELL, full = w.wave == null ? 'no wave' : `wave ${w.wave}`;
        const g = svg('g', { class: 'fm-wave' }, head);
        if (i % 2) svg('rect', { class: 'fm-waveband', x, y: 0, width: wd, height: HH }, g);
        svg('text', { x: x + 3, y: 14 }, g).textContent = wd >= full.length * 6.02 + 3 ? full : w.wave == null ? '–' : `w${w.wave}`;
        svg('line', { x1: x + 2, x2: x + wd - 2, y1: 19.5, y2: 19.5 }, g);
        svg('title', {}, g).textContent = `${full}: ${cols.slice(w.from, w.to).map(c => c.id).join(', ')}`;
      });
      const selH = svg('rect', { class: 'fm-colsel', x: 0, y: WAVE_H, width: CELL, height: HH - WAVE_H, visibility: 'hidden' }, head);
      const hovH = svg('rect', { class: 'fm-hover', x: 0, y: WAVE_H, width: CELL, height: HH - WAVE_H, visibility: 'hidden' }, head);
      const heads = cols.map((c, i) => {
        const cx = i * CELL + CELL / 2, ty = HH - 19;
        const g = svg('g', { class: `fm-colhead ${stc(c.status)}`, tabindex: 0, role: 'button', 'data-c': i, 'data-fm-focus': 'col:' + c.id,
          'aria-label': `${c.id}, ${String(c.status).toLowerCase()}${c.wave == null ? '' : ', wave ' + c.wave}: select this task` }, head);
        svg('title', {}, g).textContent = `${c.id}${c.title ? ' — ' + c.title : ''} · ${c.status}${c.wave == null ? '' : ' · wave ' + c.wave}${c.planned_all ? ' · planned the whole repo (.)' : ''}`;
        svg('rect', { class: 'fm-hit', fill: 'transparent', x: i * CELL + 1, y: WAVE_H + 1, width: CELL - 2, height: HH - WAVE_H - 2, rx: 3 }, g);
        svg('text', { x: cx + 3, y: ty, transform: `rotate(-${TILT} ${cx + 3} ${ty})` }, g).textContent = clip(c.id, 24);
        svg('rect', { class: 'fm-st', x: cx - 4, y: HH - 13, width: 8, height: 8, rx: 2 }, g);
        return g;
      });

      // the paths
      svg('rect', { width: LW, height: BH, fill: `url(#${ids.zebra})` }, labels);
      svg('rect', { width: CW, height: BH, fill: `url(#${ids.zebra})` }, grid);
      rows.forEach((r, i) => {
        if (r.kind !== 'dir') return;
        svg('rect', { class: 'fm-dirband', x: 0, y: i * ROW, width: LW, height: ROW }, labels);
        svg('rect', { class: 'fm-dirband', x: 0, y: i * ROW, width: CW, height: ROW }, grid);
      });
      const hovL = svg('rect', { class: 'fm-hover', x: 0, y: 0, width: LW, height: ROW, visibility: 'hidden' }, labels);
      const dirEls = [];
      rows.forEach((r, i) => {
        const x = 10 + r.depth * IND, y = i * ROW, dir = r.kind === 'dir';
        if (!dir) {
          const g = svg('g', { class: 'fm-file' }, labels);
          svg('title', {}, g).textContent = r.path;
          svg('text', { class: 'fm-label', x: x + 14, y: y + 14 }, g).textContent = names[i];
          return;
        }
        const g = svg('g', { class: 'fm-dirrow', tabindex: dirEls.length ? -1 : 0, role: 'button', 'aria-expanded': String(!r.collapsed), 'data-r': i, 'data-fm-focus': 'dir:' + r.path }, labels);
        dirEls.push(g);
        svg('title', {}, g).textContent = `${r.path}/ · ${plural(r.files, 'file')}${r.collapsed ? ' (collapsed: click to open)' : ''}`;
        svg('rect', { class: 'fm-hit', fill: 'transparent', x: 0, y, width: LW, height: ROW }, g);
        svg('path', { class: 'fm-tri', d: r.collapsed ? `M${x + 2} ${y + 6}l6 4l-6 4z` : `M${x} ${y + 7}l8 0l-4 6z` }, g);
        svg('text', { class: 'fm-label', x: x + 14, y: y + 14 }, g).textContent = names[i];
        svg('text', { class: 'fm-num', x: x + 14 + (names[i].length + 1) * CH, y: y + 14 }, g).textContent = counts[i];
      });

      // the cells
      for (let i = 1; i < M.waves.length; i++) {
        const x = M.waves[i].from * CELL;
        svg('line', { class: 'fm-wavesep', x1: x, x2: x, y1: WAVE_H, y2: HH }, head);
        svg('line', { class: 'fm-wavesep', x1: x, x2: x, y1: 0, y2: BH }, grid);
      }
      const selC = svg('rect', { class: 'fm-colsel', x: 0, y: 0, width: CELL, height: BH, visibility: 'hidden' }, grid);
      const hovC = svg('rect', { class: 'fm-hover', x: 0, y: 0, width: CELL, height: BH, visibility: 'hidden' }, grid);
      const hovR = svg('rect', { class: 'fm-hover', x: 0, y: 0, width: CW, height: ROW, visibility: 'hidden' }, grid);
      const at = new Map(), lanes = [];
      rows.forEach((r, ri) => {
        const lane = [];
        for (const tid in r.cells) { const ci = colIx.get(tid); if (ci !== undefined) lane.push(ci); }
        lane.sort((a, b) => a - b);
        for (const ci of lane) {
          const g = svg('g', { class: 'fm-cell' }, grid);
          svg('title', {}, g).textContent = describe(r, cols[ci], r.cells[cols[ci].id]);
          mark(g, ci * CELL + (CELL - BOX) / 2, ri * ROW + (ROW - BOX) / 2, r.cells[cols[ci].id], M.max, ids);
          at.set(ri * nc + ci, g);
        }
        lanes.push(lane);
      });
      if (M.rows.length > ROW_CAP) body.append(el('p', `Showing the first ${fmt(ROW_CAP)} of ${fmt(M.rows.length)} rows: filter by path or collapse directories to see the rest.`, 'warn fm-note'));

      /* ---------- selection, hover, the diff link and the keyboard ---------- */
      const setSel = tid => {
        const c = tid == null ? undefined : colIx.get(tid);
        for (const e of [selH, selC]) { if (c !== undefined) e.setAttribute('x', c * CELL); shown(e, c !== undefined); }
        heads.forEach((g, i) => { g.classList.toggle('fm-sel', i === c); if (i === c) g.setAttribute('aria-current', 'true'); else g.removeAttribute('aria-current'); });
      };
      setSel(sel);
      live = { M, setSel };
      const hover = (r, c) => {
        for (const e of [hovL, hovR]) { if (r != null) e.setAttribute('y', r * ROW); shown(e, r != null); }
        for (const e of [hovH, hovC]) { if (c != null) e.setAttribute('x', c * CELL); shown(e, c != null); }
      };
      const spot = (pane, e) => {
        const b = pane.getBoundingClientRect(), r = Math.floor((e.clientY - b.top) / ROW), c = Math.floor((e.clientX - b.left) / CELL);
        return [r >= 0 && r < nr ? r : null, c >= 0 && c < nc ? c : null];
      };
      grid.addEventListener('pointermove', e => hover(...spot(grid, e)));
      labels.addEventListener('pointermove', e => hover(spot(labels, e)[0], null));
      head.addEventListener('pointermove', e => {
        const g = e.target.closest && e.target.closest('.fm-colhead');
        hover(null, g ? +g.getAttribute('data-c') : spot(head, e)[1]);
      });
      for (const p of [grid, labels, head]) p.addEventListener('pointerleave', () => hover(null, null));

      let pop = null;
      const closePop = () => { V.pop = null; if (pop) pop.remove(); pop = null; };
      const drawPop = () => {
        if (pop) pop.remove();
        pop = null;
        const r = V.pop ? rowIx.get(V.pop.path) : undefined, c = V.pop ? colIx.get(V.pop.task) : undefined;
        const href = r !== undefined && c !== undefined && ctx.diffHref ? ctx.diffHref(V.pop.task, V.pop.path) : null;
        if (!href) return;
        const text = 'Diff for this file', w = Math.ceil(text.length * 6.6) + 16, h = 20;
        let x = (c + 1) * CELL + 3;
        if (x + w > CW - 2) x = Math.max(2, c * CELL - w - 3);
        const y = Math.max(0, Math.min(r * ROW, BH - h - 1));
        pop = svg('a', { class: 'fm-pop', href, 'aria-label': `Diff of ${V.pop.task} at ${V.pop.path}` }, grid);
        svg('rect', { x, y, width: w, height: h, rx: 4 }, pop);
        svg('text', { x: x + 8, y: y + 14 }, pop).textContent = text;
        openPop = closePop;
      };
      drawPop();

      // one cell at a time is in the tab order (roving tabindex); a single ring marks it while it has keyboard focus
      let cur = null, curAt = null;
      const ring = svg('rect', { class: 'fm-ring', fill: 'none', x: 0, y: 0, width: CELL - 2, height: ROW - 2, rx: 3, visibility: 'hidden' }, grid);
      const rove = (r, c, focus) => {
        const g = at.get(r * nc + c);
        if (!g) return;
        if (cur && cur !== g) for (const a of ['tabindex', 'role', 'data-fm-focus']) cur.removeAttribute(a);
        cur = g; curAt = [r, c];
        g.setAttribute('tabindex', '0'); g.setAttribute('role', 'button');
        g.setAttribute('data-fm-focus', `cell:${cols[c].id}\n${rows[r].path}`);
        if (focus) g.focus();
      };
      const first = colIx.get(sel), fr = first === undefined ? -1 : lanes.findIndex(l => l.includes(first));
      if (fr >= 0) rove(fr, first);
      else { const r0 = lanes.findIndex(l => l.length); if (r0 >= 0) rove(r0, lanes[r0][0]); }
      const activate = (r, c) => {
        const row = rows[r], col = cols[c], cell = row && col && row.cells[col.id];
        if (!cell) return;
        rove(r, c);
        V.pop = row.kind === 'file' && (cell.touched || cell.drift || cell.conflict) ? { task: col.id, path: row.path } : null;
        drawPop();
        if (ctx.onSelectTask) ctx.onSelectTask(col.id);
      };
      const near = (lane, c) => lane.reduce((b, x) => b === null || Math.abs(x - c) < Math.abs(b - c) ? x : b, null);
      const step = (r, c, key) => {
        const lane = lanes[r];
        if (key === 'ArrowRight') { const x = lane.find(v => v > c); return x === undefined ? null : [r, x]; }
        if (key === 'ArrowLeft') { const x = lane.filter(v => v < c).pop(); return x === undefined ? null : [r, x]; }
        if (key === 'Home') return [r, lane[0]];
        if (key === 'End') return [r, lane[lane.length - 1]];
        const dr = key === 'ArrowDown' ? 1 : key === 'ArrowUp' ? -1 : 0;
        if (!dr) return null;
        for (let i = r + dr; i >= 0 && i < nr; i += dr) if (lanes[i].includes(c)) return [i, c];
        for (let i = r + dr; i >= 0 && i < nr; i += dr) if (lanes[i].length) return [i, near(lanes[i], c)];
        return null;
      };
      // a click focuses the cell it hit (so the arrow keys go on from there), never the pane itself
      grid.addEventListener('mousedown', e => { if (!(e.target.closest && e.target.closest('.fm-pop'))) e.preventDefault(); });
      grid.addEventListener('click', e => {
        if (e.target.closest && e.target.closest('.fm-pop')) return;
        const [r, c] = spot(grid, e);
        if (r === null || c === null || !at.has(r * nc + c)) return;
        handled = e;
        activate(r, c);
        if (cur && cur.isConnected) cur.focus({ preventScroll: true });
      });
      grid.addEventListener('keydown', e => {
        if (!cur || e.target !== cur) { if (e.key === 'Escape') closePop(); return; }
        const [r, c] = curAt;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(r, c); return; }
        if (e.key === 'Escape') { closePop(); return; }
        const next = step(r, c, e.key);
        if (next) { e.preventDefault(); rove(next[0], next[1], true); hover(next[0], next[1]); }
      });
      // focus listeners sit on the HTML row: Chromium makes an SVG element with one focusable itself
      bodyRow.addEventListener('focusin', e => {
        if (e.target !== cur || !curAt) return;
        ring.setAttribute('x', curAt[1] * CELL + 1); ring.setAttribute('y', curAt[0] * ROW + 1);
        shown(ring, cur.matches(':focus-visible'));
        grid.append(ring);
      });
      bodyRow.addEventListener('focusout', e => { if (grid.contains(e.target)) { hover(null, null); shown(ring, false); } });
      const pick = g => { if (g && ctx.onSelectTask) ctx.onSelectTask(cols[+g.getAttribute('data-c')].id); };
      head.addEventListener('click', e => pick(e.target.closest && e.target.closest('.fm-colhead')));
      head.addEventListener('keydown', e => {
        if (e.key !== 'Enter' && e.key !== ' ') return;
        e.preventDefault(); pick(e.target.closest && e.target.closest('.fm-colhead'));
      });
      const toggle = path => {
        const set = new Set(prefs.collapsed);
        if (set.has(path)) set.delete(path); else set.add(path);
        prefs.collapsed = [...set].slice(-COLLAPSED_CAP); save(); draw('dir:' + path);
      };
      const dirOfEvent = e => { const g = e.target.closest && e.target.closest('.fm-dirrow'); return g ? rows[+g.getAttribute('data-r')] : null; };
      labels.addEventListener('click', e => { const r = dirOfEvent(e); if (r) toggle(r.path); });
      // the directories are one stop in the tab order too: up and down move between them
      let dirCur = dirEls[0] || null;
      const roveDir = (g, focus) => {
        if (dirCur && dirCur !== g) dirCur.setAttribute('tabindex', '-1');
        dirCur = g; g.setAttribute('tabindex', '0');
        if (focus) g.focus();
      };
      bodyRow.addEventListener('focusin', e => { const g = labels.contains(e.target) && e.target.closest('.fm-dirrow'); if (g) roveDir(g); });
      labels.addEventListener('keydown', e => {
        const r = dirOfEvent(e);
        if (!r) return;
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          const next = dirEls[dirEls.indexOf(e.target.closest('.fm-dirrow')) + (e.key === 'ArrowDown' ? 1 : -1)];
          if (next) { e.preventDefault(); roveDir(next, true); }
          return;
        }
        if (e.key === 'Enter' || e.key === ' ' || (e.key === 'ArrowLeft' && !r.collapsed) || (e.key === 'ArrowRight' && r.collapsed)) { e.preventDefault(); toggle(r.path); }
      });

      if (scrollTo) { scroll.scrollLeft = scrollTo[0]; scroll.scrollTop = scrollTo[1]; }
      if (typeof want === 'string') {
        let target = null;
        if (want.startsWith('col:')) target = heads[colIx.get(want.slice(4))] || null;
        else if (want.startsWith('dir:')) { const r = rowIx.get(want.slice(4)); target = r === undefined ? null : labels.querySelector(`[data-r="${r}"]`); if (target) roveDir(target); }
        else if (want.startsWith('cell:')) {
          const nl = want.indexOf('\n'), r = rowIx.get(want.slice(nl + 1)), c = colIx.get(want.slice(5, nl));
          if (r !== undefined && c !== undefined && at.has(r * nc + c)) { rove(r, c); target = cur; }
        }
        if (target) target.focus({ preventScroll: true });
      }
    }

    /* ---------- under the matrix: overlaps, drift, conflicts, deferrals, as text ---------- */
    function lists() {
      const box = el('div', undefined, 'fm-lists');
      const titles = new Map(list(run && run.tasks).filter(t => t && t.id != null).map(t => [String(t.id), t.title || '']));
      const waveOf = new Map(list(data.tasks).filter(t => t && t.id != null).map(t => [String(t.id), num(t.wave)]));
      let n = 0;
      const task = id => {
        const b = el('button', id, 'linkish fm-task');
        b.type = 'button'; b.title = titles.get(String(id)) || `Select ${id}`; b.setAttribute('data-fm-focus', 'list:' + n++);
        b.addEventListener('click', () => { if (ctx.onSelectTask) ctx.onSelectTask(String(id)); });
        return b;
      };
      const some = (ps, k = 6) => ps.slice(0, k).join(', ') + (ps.length > k ? ` +${ps.length - k} more` : '');
      const items = (rows, ul = el('ul')) => {
        for (const parts of rows.slice(0, 200)) { const li = el('li'); li.append(...parts); ul.append(li); }
        if (rows.length > 200) ul.append(el('li', `… and ${fmt(rows.length - 200)} more`, 'muted'));
        return ul;
      };
      // rows past `fold` (when there are more than a few) go in a closed <details> titled `more`
      const section = (title, help, rows, fold = rows.length, more = '') => {
        const s = el('section', undefined, 'fm-list'), h = el('h3', rows.length ? `${title} (${fmt(rows.length)})` : title);
        h.title = help;
        s.append(h);
        if (!rows.length) s.append(el('p', 'None.', 'muted'));
        else if (rows.length - fold <= 4) s.append(items(rows));
        else {
          if (fold) s.append(items(rows.slice(0, fold)));
          const det = el('details', undefined, 'fm-more');
          det.append(el('summary', `${fmt(rows.length - fold)} ${more}`), items(rows.slice(fold)));
          s.append(det);
        }
        box.append(s);
      };
      if (!solo) {
        const ov = list(data.overlaps).filter(o => o && o.a != null && o.b != null);
        ov.sort((x, y) => (y.same_wave ? 1 : 0) - (x.same_wave ? 1 : 0));
        const same = ov.filter(o => o.same_wave).length;
        section('Overlaps', 'Pairs of tasks whose planned files overlap (equal, a directory above the other, or .) or that changed a file in common. In one wave, forge should have kept them apart.',
          ov.map(o => {
            const wa = waveOf.get(String(o.a)), wb = waveOf.get(String(o.b));
            const bits = [o.planned ? 'plans overlap' : '', list(o.touched).length ? `both touched ${some(list(o.touched).map(String), 2)}` : ''].filter(Boolean);
            return [task(o.a), ' ↔ ', task(o.b), ' · ', o.same_wave ? el('span', `same wave${wa != null ? ' ' + wa : ''}`, 'warn') :
              el('span', wa != null && wb != null ? `waves ${wa} and ${wb}` : 'different waves', 'muted'), bits.length ? ' · ' + bits.join(' · ') : ''];
          }), same, 'in different waves');
        const per = (field, title, help) => section(title, help, list(data.tasks).filter(t => t && t.id != null && paths(t[field]).length)
          .map(t => [task(t.id), ': ' + some(paths(t[field]))]), 8, 'more tasks');
        per('drift', 'Drift', 'Files a task\'s diff changed without declaring them (tasks/<id>/drift.txt).');
        per('conflicts', 'Merge conflicts', 'Files that conflicted when the task was merged (tasks/<id>/conflict.files).');
        section('Deferrals', 'Tasks the wave planner moved to a later wave (deferrals.txt).', list(data.deferrals).filter(x => typeof x === 'string' && x).map(x => [x]), 8, 'more');
      }
      const warns = list(data.warnings).filter(x => typeof x === 'string' && x);
      if (warns.length) section('Warnings', 'Files or lines the reader skipped as malformed.', warns.map(x => [x]), 3, 'more');
      return box;
    }
  }

  return { SORTS, KINDS, ROW_CAP, normPrefs, matrix, describe, render };
});
