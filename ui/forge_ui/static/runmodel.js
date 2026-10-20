/* forge-ui run model: pure data for the 3D stage, the 2D views and the inspector. No DOM, no I/O.
   A plain script in the page (window.ForgeRunModel) and a CommonJS module under node:test.
   plate(), cells() and blockSize() are archivision's lib/layout.js (MIT, © 2026 Giamme), with the
   size function passed in so run blocks and task blocks can size themselves. */
(function (root, factory) {
  'use strict';
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeRunModel = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const DONE = new Set(['MERGED', 'PASS', 'NOCHANGES']);
  const BAD = new Set(['FAIL', 'ERROR', 'CONFLICT', 'INFRA', 'TIMEOUT', 'INVALIDATED', 'INTERRUPTED', 'UNKNOWN', 'BLOCKED']);
  const WEIGHT = { low: 1, medium: 2, high: 3 };
  const weight = t => WEIGHT[String(t && t.difficulty || '').toLowerCase()] || 2;
  const alias = spec => spec ? String(spec).split(':')[0] : null;
  const basename = p => p ? String(p).split(/[\\/]/).filter(Boolean).pop() || String(p) : '—';
  function fmtDur(s) {
    s = num(s); if (s === null) return '—';
    s = Math.max(0, Math.round(s));
    if (s < 60) return s + 's';
    if (s < 3600) return Math.floor(s / 60) + 'm' + String(s % 60).padStart(2, '0') + 's';
    return Math.floor(s / 3600) + 'h' + String(Math.floor(s % 3600 / 60)).padStart(2, '0') + 'm';
  }

  /* ---------- archivision's plate: one district per layer, wrapped into rows ---------- */
  const GAPX = 18, PADX = 22, TOPPAD = 44, BOTPAD = 16, ROWGAP = 18, DGAP = 18;
  const isInt = v => Number.isInteger(v) && v >= 0;
  function cells(m) {
    const parts = m.parts || [];
    const taken = new Set();
    const own = parts.length && parts.every(p => isInt(p.col) && isInt(p.row)) && parts.every(p => {
      for (let c = p.col; c < p.col + (p.cspan || 1); c++) for (let r = p.row; r < p.row + (p.rspan || 1); r++) {
        if (taken.has(`${c},${r}`)) return false; taken.add(`${c},${r}`);
      }
      return true;
    });
    if (own) return 'own';
    for (const p of parts) { delete p.cspan; delete p.rspan; }
    const zones = (m.zones || []).filter(z => z.parts && z.parts.length);
    if (zones.length) {
      const at = {}; zones.forEach((z, c) => z.parts.forEach((id, r) => { at[id] = [c, r]; }));
      const extra = zones.length;
      parts.filter(p => !at[p.id]).forEach((p, r) => { at[p.id] = [extra, r]; });
      for (const p of parts) [p.col, p.row] = at[p.id];
      return 'zones';
    }
    const C = Math.max(1, Math.ceil(Math.sqrt(parts.length)));
    parts.forEach((p, i) => { p.col = i % C; p.row = Math.floor(i / C); });
    return 'grid';
  }
  function grid(m) {
    const ps = m.parts || [];
    return [Math.max(1, ...ps.map(p => (p.col || 0) + (p.cspan || 1))), Math.max(1, ...ps.map(p => (p.row || 0) + (p.rspan || 1)))];
  }
  function blockSize(m) {
    const [C, R] = grid(m);
    return [clamp(24 + C * 70 + (C - 1) * 20, 190, 460), clamp(40 + R * 38 + (R - 1) * 26, 160, 300), 50 + Math.min(14, (m.parts || []).length * 1.6)];
  }
  // modules: [{ id, layer, … }] in display order; layers: [{ id }] in display order.
  function plate({ modules, layers }, size = blockSize) {
    const used = layers.filter(l => modules.some(m => m.layer === l.id));
    if (!modules.length) return { px: 900, py: 400, place: {}, districts: [], rows: 0 };
    const AREA = Math.sqrt(modules.reduce((t, m) => { const [w, d] = size(m); return t + (w + GAPX) * (d + TOPPAD + BOTPAD + ROWGAP); }, 0));
    const PXT = Math.max(2 * PADX + Math.max(...modules.map(m => size(m)[0])),
      Math.round((AREA * 1.12 >= 1600 ? AREA * 1.12 : Math.max(900, Math.min(1600, AREA * 1.35))) / 10) * 10);
    const dists = [];
    for (const l of used) {
      let chunk = [], cw = 2 * PADX - GAPX;
      for (const m of modules.filter(x => x.layer === l.id)) {
        const [w] = size(m);
        if (chunk.length && cw + GAPX + w > PXT) { dists.push({ layer: l.id, mods: chunk }); chunk = []; cw = 2 * PADX - GAPX; }
        chunk.push(m); cw += GAPX + w;
      }
      if (chunk.length) dists.push({ layer: l.id, mods: chunk });
    }
    const rows = []; let row = [], rx = 0;
    for (const d of dists) {
      d.w = 2 * PADX + d.mods.reduce((a, m) => a + size(m)[0], 0) + GAPX * (d.mods.length - 1);
      d.d = TOPPAD + Math.max(...d.mods.map(m => size(m)[1])) + BOTPAD;
      if (row.length && rx + DGAP + d.w > PXT) { rows.push(row); row = []; rx = 0; }
      row.push(d); rx += (row.length > 1 ? DGAP : 0) + d.w;
    }
    if (row.length) rows.push(row);
    const PX = Math.max(PXT, ...rows.map(r => r.reduce((a, d) => a + d.w, 0) + DGAP * (r.length - 1)));
    const place = {}, districts = [];
    let y = 0;
    rows.forEach(r => {
      const w0 = r.reduce((a, d) => a + d.w, 0) + DGAP * (r.length - 1), extra = PX - w0, dmax = Math.max(...r.map(d => d.d));
      let x = 0;
      r.forEach((d, i) => {
        const w = d.w + (i === r.length - 1 ? extra : 0);
        districts.push({ layer: d.layer, x, y, w, d: dmax });
        let bx = x + PADX;
        for (const m of d.mods) { const [bw, bd, bh] = size(m); place[m.id] = [bx, y + TOPPAD, bw, bd, Math.round(bh)]; bx += bw + GAPX; }
        x += w + DGAP;
      });
      y += dmax + ROWGAP;
    });
    return { px: PX, py: y - ROWGAP, place, districts, rows: rows.length };
  }

  /* ---------- statuses ---------- */
  function runState(run) {
    const counts = run && run.counts || {};
    if (counts.RUNNING) return 'live';
    if (Object.keys(counts).some(s => BAD.has(s) && counts[s] > 0)) return 'failed';
    const total = Object.values(counts).reduce((a, n) => a + (n || 0), 0);
    if (total && Object.keys(counts).every(s => DONE.has(s) || !counts[s])) return 'done';
    return 'pending';
  }
  // What one dwarf/qa/task execution ended as, in the task status vocabulary.
  function execStatus(e) {
    if (!e) return 'PENDING';
    if (e.state === 'running') return 'RUNNING';
    if (e.state === 'orphaned') return 'INTERRUPTED';
    if (e.timed_out) return 'TIMEOUT';
    if (e.infra_class) return 'INFRA';
    if (e.role === 'qa' && (e.verdict === 'PASS' || e.verdict === 'FAIL')) return e.verdict;
    if (e.exit_code === 0) return e.role === 'qa' ? 'UNKNOWN' : 'PASS';
    return 'ERROR';
  }
  // The clock a run is drawn at: now, unless the run has not changed for longer than any dispatch may take
  // (twice its timeout), in which case what still says RUNNING died with its process and stops at that point.
  function liveNow(run, now) {
    const t = num(now); if (t === null) return null;
    const upd = num(run && run.updated); if (upd === null) return t;
    const timeout = num(run && run.settings && run.settings.timeout) || 3600;
    return Math.min(t, upd + 2 * Math.max(timeout, 600));
  }
  const execEnd = (e, now) => num(e.ended) ?? (e.state === 'running' && num(now) !== null ? Math.max(num(e.started) || now, now) : num(e.started));

  /* ---------- filters and the atlas (every run on one plate) ---------- */
  function matchRun(run, f, now) {
    f = f || {};
    const text = String(f.text || '').trim().toLowerCase();
    if (text && ![run.goal, run.repo, run.run_id, run.path, ...list(run.models)].some(v => String(v || '').toLowerCase().includes(text))) return false;
    if (f.state && runState(run) !== f.state) return false;
    if (f.repo && run.repo !== f.repo) return false;
    if (f.kind && run.kind !== f.kind) return false;
    if (f.model && !list(run.models).some(m => alias(m) === f.model)) return false;
    if (f.ageDays && num(now) !== null && !(num(run.updated) !== null && run.updated >= now - f.ageDays * 86400)) return false;
    if (f.verdict) {
      const v = run.final_review || run.verification_status || null;
      if (f.verdict === 'none' ? v : v !== f.verdict) return false;
    }
    return true;
  }
  function filterOptions(runs) {
    const repos = new Map(), models = new Set();
    for (const r of list(runs)) {
      if (r.repo && !repos.has(r.repo)) repos.set(r.repo, basename(r.repo));
      for (const m of list(r.models)) if (alias(m)) models.add(alias(m));
    }
    return { repos: [...repos].map(([path, name]) => ({ path, name })), models: [...models].sort() };
  }
  const atlasSize = m => {
    const n = Math.max(1, m.tasks.length);
    return [clamp(170 + Math.min(24, n) * 9, 190, 400), 150, 36 + Math.min(26, n * 1.4)];
  };
  // The newest runs first; by default the last `limit` runs within `days` (never fewer than `floor` when there are any).
  function atlasModel(runs, f, { now = null, showAll = false, limit = 60, days = 14, floor = 12 } = {}) {
    const sorted = list(runs).slice().sort((a, b) => (b.updated || 0) - (a.updated || 0));
    let shown = sorted;
    if (!showAll) {
      const recent = num(now) === null ? sorted : sorted.filter(r => (r.updated || 0) >= now - days * 86400);
      shown = (recent.length >= Math.min(floor, sorted.length) ? recent : sorted.slice(0, floor)).slice(0, limit);
    }
    const layers = [], seen = new Set();
    for (const r of shown) {
      const id = 'repo:' + (r.repo || '?');
      if (!seen.has(id)) { seen.add(id); layers.push({ id, name: basename(r.repo), repo: r.repo || null }); }
    }
    const modules = shown.map(r => ({ id: r.key, layer: 'repo:' + (r.repo || '?'), run: r, state: runState(r),
      tasks: list(r.task_statuses).length ? list(r.task_statuses) : Object.entries(r.counts || {}).flatMap(([s, n]) => Array.from({ length: Math.min(n, 60) }, (_, i) => ({ id: s + i, status: s }))),
      match: matchRun(r, f, now) }));
    const p = plate({ modules, layers }, atlasSize);
    return { layers, modules, ...p, hidden: sorted.length - shown.length, total: sorted.length, matches: modules.filter(m => m.match).length };
  }

  /* ---------- the plan: waves as districts, then the finish line ---------- */
  // Column of every task: its wave, else one right of its deepest dependency (graph.js's rule).
  function columns(tasks) {
    const byId = new Map(list(tasks).map(t => [t.id, t])), col = new Map(), floating = [];
    for (const t of list(tasks)) {
      const w = Math.floor(Number(t.wave));
      if (t.wave !== null && t.wave !== undefined && Number.isFinite(w) && w >= 1) col.set(t.id, w); else { col.set(t.id, 1); floating.push(t); }
    }
    for (let pass = 0; pass < floating.length + 1; pass++) {
      let changed = false;
      for (const t of floating) {
        let deepest = 0;
        for (const d of list(t.deps)) if (d !== t.id && byId.has(d)) deepest = Math.max(deepest, col.get(d));
        const want = deepest ? deepest + 1 : 1;
        if (want !== col.get(t.id)) { col.set(t.id, want); changed = true; }
      }
      if (!changed) break;
    }
    return { col, inferred: new Set(floating.map(t => t.id)) };
  }
  const TASK_H = { low: 42, medium: 54, high: 66 };
  const planSize = m => m.kind === 'task' ? [230, 168, TASK_H[String(m.task.difficulty || '').toLowerCase()] || 50] : [210, 150, 46];
  // Integration is running only while a task is; a finished run with a task that never merged is blocked there.
  function integrationStatus(statuses) {
    if (!statuses.length) return 'PENDING';
    if (statuses.some(s => s === 'RUNNING')) return 'RUNNING';
    if (statuses.every(s => s === 'MERGED')) return 'MERGED';
    if (statuses.some(s => s === 'PENDING' || s === 'PASS')) return 'PENDING';
    return 'BLOCKED';
  }
  function finishState(run) {
    const fr = run.final_review, v = run.verification, ig = run.integration;
    const tasks = list(run.tasks), merged = tasks.filter(t => t.status === 'MERGED').length;
    return {
      integration: { status: integrationStatus(tasks.map(t => t.status)),
        merged, total: tasks.length, base: ig && ig.base || null, accepted: ig && ig.accepted || null },
      verification: { status: v && v.status ? String(v.status).toUpperCase() : 'PENDING', command: v && v.command || null, runs: list(v && v.runs).length },
      final: { status: !fr ? (run.settings && run.settings.final_qa ? 'PENDING' : 'NONE') : fr.state === 'running' ? 'RUNNING' : fr.state === 'infra' ? 'INFRA' : fr.verdict || 'UNKNOWN',
        requested: !!(fr || run.settings && run.settings.final_qa), spec: fr && fr.spec || run.settings && run.settings.final_qa || null, findings: fr && fr.findings || null,
        count: list(run.final_reviews).length },
    };
  }
  function planModel(run) {
    const tasks = list(run && run.tasks), solo = run && run.kind === 'solo';
    const { col, inferred } = columns(tasks);
    const used = [...new Set(tasks.map(t => col.get(t.id)))].sort((a, b) => a - b);
    const layers = used.map(c => ({ id: 'w' + c, name: solo ? 'SOLO RUN' : 'WAVE ' + c + (tasks.some(t => col.get(t.id) === c && inferred.has(t.id)) && !list(run.waves).length ? ' · inferred' : ''), wave: c }));
    const modules = tasks.map(t => ({ id: 't:' + t.id, kind: 'task', layer: 'w' + col.get(t.id), task: t }));
    const flows = [];
    const ids = new Set(tasks.map(t => t.id));
    for (const t of tasks) for (const d of new Set(list(t.deps))) if (ids.has(d) && d !== t.id) flows.push({ from: 't:' + d, to: 't:' + t.id, kind: 'dep' });
    if (!solo && tasks.length) {
      layers.push({ id: 'finish', name: 'FINISH', finish: true });
      for (const [id, name] of [['f:integration', 'Integration'], ['f:verification', 'Verification'], ['f:final', 'Final review']]) modules.push({ id, kind: 'finish', layer: 'finish', name });
      const needed = new Set(tasks.flatMap(t => list(t.deps)));
      for (const t of tasks) if (!needed.has(t.id)) flows.push({ from: 't:' + t.id, to: 'f:integration', kind: 'finish' });
      flows.push({ from: 'f:integration', to: 'f:verification', kind: 'finish' }, { from: 'f:verification', to: 'f:final', kind: 'finish' });
    }
    const p = plate({ modules, layers }, planSize);
    return { layers, modules, flows, ...p, finish: solo ? null : finishState(run) };
  }

  /* ---------- inside a task: one zone per attempt, its dwarf, its review and the shadow reviews ---------- */
  // shadow.status is written only when a shadow review ends (done, timeout, error, …): none means it still runs
  const shadowStatus = s => s.verdict || (s.status == null ? 'RUNNING' : s.status === 'done' ? 'UNKNOWN' : s.status === 'timeout' || s.status === 'overtime' ? 'TIMEOUT' : 'ERROR');
  function taskParts(task) {
    const execs = list(task && task.executions);
    const byAttempt = new Map();
    const put = (a, p) => { const k = a == null ? 'other' : String(a); if (!byAttempt.has(k)) byAttempt.set(k, []); byAttempt.get(k).push(p); };
    for (const e of execs) {
      if (e.role !== 'dwarf' && e.role !== 'qa') continue;
      put(e.attempt, { id: e.id, kind: e.role, exec: e, status: execStatus(e), label: e.role === 'dwarf' ? 'DWARF' : 'QA', spec: e.model && e.model.spec || null, attempt: e.attempt });
    }
    for (const s of list(task && task.shadow)) {
      put(s.attempt, { id: 'shadow:' + s.attempt + '-' + s.k, kind: 'shadow', shadow: s, exec: s.execution ? { id: s.execution } : null,
        status: shadowStatus(s), label: 'SHADOW', spec: s.spec || null, attempt: s.attempt });
    }
    if (!byAttempt.size) {
      put(1, { id: 'plan:dwarf', kind: 'dwarf', planned: true, status: 'PENDING', label: 'DWARF', spec: task && task.dwarf || null, attempt: 1 });
      put(1, { id: 'plan:qa', kind: 'qa', planned: true, status: 'PENDING', label: 'QA', spec: task && task.qa || null, attempt: 1 });
    }
    const keys = [...byAttempt.keys()].sort((a, b) => a === 'other' ? 1 : b === 'other' ? -1 : Number(a) - Number(b));
    const order = { dwarf: 0, qa: 1, shadow: 2 };
    const zones = [], parts = [], links = [];
    for (const k of keys) {
      const ps = byAttempt.get(k).sort((a, b) => order[a.kind] - order[b.kind] || (num(a.exec && a.exec.started) || 0) - (num(b.exec && b.exec.started) || 0));
      zones.push({ name: k === 'other' ? 'OTHER' : 'ATTEMPT ' + k, attempt: k === 'other' ? null : Number(k), parts: ps.map(p => p.id) });
      parts.push(...ps);
      const dw = ps.filter(p => p.kind === 'dwarf').pop(), qa = ps.filter(p => p.kind === 'qa').pop();
      if (dw && qa) links.push({ from: dw.id, to: qa.id, kind: 'flow' });
      for (const sh of ps.filter(p => p.kind === 'shadow')) if (qa) links.push({ from: qa.id, to: sh.id, kind: 'shadow', agree: sh.shadow.agreement || 'unknown' });
    }
    // Between attempts: the review that failed attempt n hands over to the next dwarf, labelled with the
    // models when that retry was escalated. Escalations are matched by time: their attempt column counts differently.
    const byId = new Map(parts.map(p => [p.id, p]));
    const handover = i => {
      const prev = zones[i - 1].parts.map(id => byId.get(id)).filter(p => p.kind !== 'shadow'), next = zones[i].parts.map(id => byId.get(id));
      return [prev[prev.length - 1], next.find(p => p.kind === 'dwarf') || next[0]];
    };
    const escalated = new Map();
    for (const esc of list(task && task.escalations)) {
      const at = num(esc.at); if (at === null) continue;
      const z = zones.findIndex(z => z.attempt !== null && z.parts.some(id => { const e = byId.get(id).exec; return e && num(e.started) !== null && e.started >= at; }));
      if (z > 0 && !escalated.has(z)) escalated.set(z, esc);
    }
    for (let i = 1; i < zones.length; i++) {
      const [a, b] = handover(i), esc = escalated.get(i);
      if (a && b) links.push(esc ? { from: a.id, to: b.id, kind: 'escalation', label: `${esc.from || '?'} → ${esc.to || '?'}` } : { from: a.id, to: b.id, kind: 'retry' });
    }
    return { zones, parts, links };
  }

  /* ---------- timings ---------- */
  function taskTimes(task, run, now) {
    const execs = list(task && task.executions).filter(e => e.role === 'task' || e.role === 'solo');
    const all = list(task && task.executions);
    const starts = (execs.length ? execs : all).map(e => num(e.started)).filter(v => v !== null);
    const ends = (execs.length ? execs : all).map(e => execEnd(e, now)).filter(v => v !== null);
    const first = starts.length ? Math.min(...starts) : null, last = ends.length ? Math.max(...ends) : null;
    const model = all.filter(e => e.role === 'dwarf' || e.role === 'qa').reduce((a, e) => a + (num(e.phases && e.phases.model) || 0), 0);
    const busy = execs.reduce((a, e) => { const s = num(e.started), x = execEnd(e, now); return a + (s !== null && x !== null ? x - s : 0); }, 0);
    let ready = null;
    if (run) {
      const byId = new Map(list(run.tasks).map(t => [t.id, t]));
      const deps = list(task.deps).map(d => byId.get(d)).filter(Boolean);
      const at = deps.map(d => num(d.merged_at)).filter(v => v !== null);
      ready = deps.length ? (at.length === deps.length ? Math.max(...at) : null) : num(run.timeline && run.timeline.started);
    }
    const tokens = all.reduce((a, e) => { const t = e.tokens || {}; return { input: a.input + (num(t.input) || 0), output: a.output + (num(t.output) || 0), cached: a.cached + (num(t.cached) || 0) }; }, { input: 0, output: 0, cached: 0 });
    return { first, last, busy, model, overhead: Math.max(0, busy - model), wait: first !== null && ready !== null ? Math.max(0, first - ready) : null, ready, tokens };
  }
  function metricValue(task, metric, run, now) {
    if (!task) return null;
    if (metric === 'duration') { const t = taskTimes(task, null, now); return t.busy || null; }
    if (metric === 'tokens') { const t = taskTimes(task, null, now).tokens; return t.input + t.output || null; }
    if (metric === 'attempts') return num(task.attempt) || list(task.executions).filter(e => e.role === 'task').length || null;
    if (metric === 'model') return alias(task.dwarf);
    return null;
  }

  /* ---------- projected schedule: forge-schedule.py's rules, run forward from now ---------- */
  function normPath(p) {
    const out = [];
    for (const s of String(p).split('/')) { if (!s || s === '.') continue; if (s === '..' && out.length && out[out.length - 1] !== '..') out.pop(); else out.push(s); }
    return (String(p).startsWith('/') ? '/' : '') + out.join('/') || '.';
  }
  function overlap(a, b) {
    const A = list(a).filter(p => p && p !== '-').map(normPath), B = list(b).filter(p => p && p !== '-').map(normPath);
    return A.some(x => B.some(y => x === '.' || y === '.' || x === y || x.startsWith(y + '/') || y.startsWith(x + '/')));
  }
  function estimates(run) {
    const tasks = list(run && run.tasks), samples = [];
    for (const t of tasks) for (const e of list(t.executions)) if (e.role === 'task' && num(e.duration_s) !== null) samples.push(e.duration_s / weight(t));
    samples.sort((a, b) => a - b);
    const unit = samples.length ? samples[Math.floor(samples.length / 2)] : 600;
    return t => Math.max(30, unit * weight(t));
  }
  function projectSchedule(run, { now = null, maxParallel = null } = {}) {
    const tasks = list(run && run.tasks), est = estimates(run), cap = Math.max(1, num(maxParallel) || num(run && run.settings && run.settings.max_parallel) || 3);
    const byId = new Map(tasks.map(t => [t.id, t]));
    const lastEnd = Math.max(...tasks.flatMap(t => list(t.executions).map(e => execEnd(e, now)).filter(v => v !== null)), num(run && run.timeline && run.timeline.started) || 0);
    let clock = num(now) !== null ? now : lastEnd || 0;
    const merged = new Map(), active = new Map(), out = {};
    for (const t of tasks) {
      if (t.status === 'MERGED') merged.set(t.id, num(t.merged_at) || clock);
      else if (t.status === 'PASS') merged.set(t.id, clock);   // the scheduler merges a saved PASS before anything starts
      else if (t.status === 'RUNNING') {
        const tl = taskTimes(t, null, clock);
        const end = Math.max(clock + 30, (tl.first !== null ? tl.first : clock) + est(t));
        active.set(t.id, end); out[t.id] = { start: tl.first !== null ? tl.first : clock, end, running: true };
      }
    }
    const pending = tasks.filter(t => !merged.has(t.id) && !active.has(t.id) && ['PENDING', 'BLOCKED', 'INFRA'].includes(t.status || 'PENDING')).map(t => t.id);
    let guard = tasks.length * 4 + 8;
    while ((pending.length || active.size) && guard-- > 0) {
      for (const id of pending.slice()) {
        if (active.size >= cap) break;
        const t = byId.get(id);
        if (list(t.deps).some(d => byId.has(d) && !merged.has(d))) continue;
        if ([...active.keys()].some(o => overlap(t.files, byId.get(o).files))) continue;
        const end = clock + est(t);
        active.set(id, end); out[id] = { start: clock, end, ghost: true };
        pending.splice(pending.indexOf(id), 1);
      }
      if (!active.size) break;
      const [id, end] = [...active].sort((a, b) => a[1] - b[1])[0];
      clock = Math.max(clock, end); active.delete(id); merged.set(id, end);
    }
    for (const id of pending) out[id] = { start: null, end: null, blocked: true };
    return out;
  }

  /* ---------- execution: attempts placed on wall-clock time, in lanes ---------- */
  const LANE_D = 118, BLOCK_D = 90, EXEC_PAD = 40, RULER = 60;
  function execItems(run, now) {
    const items = [];
    for (const t of list(run && run.tasks)) {
      const execs = list(t.executions);
      let tops = execs.filter(e => e.role === 'task');
      if (!tops.length) tops = execs.filter(e => e.role === 'solo');
      if (!tops.length && execs.length) {             // no task-level record: span its dwarf and qa calls
        const s = Math.min(...execs.map(e => num(e.started)).filter(v => v !== null));
        if (Number.isFinite(s)) tops = [{ id: 'span:' + t.id, role: 'task', started: s, ended: Math.max(...execs.map(e => execEnd(e, now) || s)), state: execs.some(e => e.state === 'running') ? 'running' : 'done', attempt: t.attempt, synthetic: true }];
      }
      const rows = list(t.attempts);
      for (const e of tops) {
        const start = num(e.started); if (start === null) continue;
        const kids = execs.filter(k => k !== e && (k.parent === e.id || e.synthetic || (tops.length === 1 && !k.parent && (k.role === 'dwarf' || k.role === 'qa'))));
        const row = rows.filter(r => r.attempt === e.attempt).pop();
        const status = e.state === 'running' ? 'RUNNING' : row ? row.status : e.state === 'orphaned' ? 'INTERRUPTED' : t.status === 'MERGED' && e === tops[tops.length - 1] ? 'MERGED' : execStatus(e);
        const end = execEnd(e, now);
        const running = kids.find(k => k.state === 'running');
        items.push({ id: e.id, task: t.id, attempt: e.attempt, start, end: end !== null ? Math.max(end, start) : start, running: e.state === 'running',
          status: e === tops[tops.length - 1] && t.status === 'MERGED' ? 'MERGED' : status, role: running ? running.role : null, phase: running ? running.phase : null,
          segs: kids.filter(k => k.role === 'dwarf' || k.role === 'qa').map(k => ({ id: k.id, role: k.role, start: num(k.started), end: execEnd(k, now), status: execStatus(k) })).filter(s => s.start !== null) });
      }
    }
    return items;
  }
  // scale: {t0, t1} of a previous model, kept while every block still fits inside it, so a live run does not
  // change the time axis (and so every block) on each poll
  function execModel(run, { now = null, ghosts = true, maxParallel = null, scale = null } = {}) {
    const items = execItems(run, now);
    const cap = Math.max(1, num(maxParallel) || num(run && run.settings && run.settings.max_parallel) || 3);
    const live = items.some(i => i.running) || list(run && run.tasks).some(t => t.status === 'RUNNING');
    const proj = ghosts && run && run.kind !== 'solo' ? projectSchedule(run, { now: now !== null ? now : undefined, maxParallel: cap }) : {};
    for (const [id, p] of Object.entries(proj)) if (p.ghost && p.start !== null) items.push({ id: 'ghost:' + id, task: id, ghost: true, start: p.start, end: p.end, status: 'PENDING', segs: [] });
    const extra = [];
    for (const v of list(run && run.timeline && run.timeline.verifications)) if (num(v.started) !== null) extra.push({ id: v.id, kind: 'verification', start: v.started, end: execEnd(v, now) || v.started, status: v.state === 'running' ? 'RUNNING' : v.exit_code === 0 ? 'PASS' : v.exit_code == null ? 'UNKNOWN' : 'FAIL', segs: [] });
    for (const r of list(run && run.final_reviews)) {
      const s = num(r.started) ?? num(r.at); if (s === null) continue;
      const e = num(r.ended) ?? (r.state === 'running' && num(now) !== null ? now : s + 1);
      extra.push({ id: 'review:' + r.name, kind: 'review', review: r.name, start: s, end: Math.max(s, e), status: r.state === 'running' ? 'RUNNING' : r.verdict || 'UNKNOWN', findings: r.findings || null, segs: [] });
    }
    const all = items.concat(extra);
    if (!all.length) return { items: [], extra: [], lanes: 0, cap, overLimit: false, t0: null, t1: null, k: 1, px: 900, py: 300, ruler: [], live, nowX: null };
    let t0 = Math.min(...all.map(i => i.start));
    let t1 = Math.max(...all.map(i => i.end), num(now) !== null && live ? now : -Infinity);
    if (scale && num(scale.t0) === t0 && num(scale.t1) !== null && t1 <= scale.t1) t1 = scale.t1;
    else if (live) {
      // headroom, rounded to a whole step, so growing blocks do not rescale every poll
      const raw = Math.max(60, (t1 - t0) * 1.25 + 120), unit = [60, 300, 600, 1800, 3600, 7200, 21600].find(u => raw / u <= 12) || 21600;
      t1 = t0 + Math.ceil(raw / unit) * unit;
    }
    const span = Math.max(60, t1 - t0);
    const W = clamp(260 * Math.sqrt(Math.max(1, items.length)), 900, 2600), k = W / span;
    // greedy interval partitioning: the real attempts first (in start order), then the projected ones
    const lanesEnd = [];
    const place = it => {
      let lane = lanesEnd.findIndex(e => e <= it.start + 1);
      if (lane < 0) { lane = lanesEnd.length; lanesEnd.push(it.end); } else lanesEnd[lane] = it.end;
      it.lane = lane;
    };
    items.filter(i => !i.ghost).sort((a, b) => a.start - b.start || a.end - b.end).forEach(place);
    items.filter(i => i.ghost).sort((a, b) => a.start - b.start).forEach(place);
    const lanes = Math.max(1, lanesEnd.length);
    const x = t => EXEC_PAD + (t - t0) * k;
    for (const it of items) { it.x = x(it.start); it.w = Math.max(22, (it.end - it.start) * k); it.y = RULER + it.lane * LANE_D + (LANE_D - BLOCK_D) / 2; it.d = BLOCK_D; }
    const fy = RULER + lanes * LANE_D + 24;
    const finishLanes = [['verification', 'VERIFY'], ['review', 'FINAL REVIEW']].filter(([k]) => extra.some(e => e.kind === k));
    finishLanes.forEach(([kind], i) => extra.filter(e => e.kind === kind).forEach(e => { e.lane = 'f' + i; e.x = x(e.start); e.w = Math.max(22, (e.end - e.start) * k); e.y = fy + i * LANE_D + (LANE_D - BLOCK_D) / 2; e.d = BLOCK_D; }));
    const steps = [60, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400];
    const step = steps.find(s => span / s <= 10) || 86400;
    const ruler = [];
    for (let t = Math.ceil(t0 / step) * step; t <= t0 + span; t += step) ruler.push({ t, x: x(t), label: fmtDur(t - t0) });
    return { items, extra, lanes, finishLanes: finishLanes.map(([k, name], i) => ({ kind: k, name, y: fy + i * LANE_D })), cap, overLimit: lanes > cap, t0, t1: t0 + span, k,
      px: EXEC_PAD * 2 + W, py: fy + finishLanes.length * LANE_D + 10, laneD: LANE_D, ruler, live, nowX: num(now) !== null && live ? x(now) : null, x };
  }

  /* ---------- the critical path and how busy the slots were ---------- */
  function criticalPath(run, { now = null } = {}) {
    const tasks = list(run && run.tasks), byId = new Map(tasks.map(t => [t.id, t]));
    const T = new Map(tasks.map(t => [t.id, taskTimes(t, null, now)]));
    const end = t => num(t.merged_at) ?? T.get(t.id).last;
    const timed = tasks.filter(t => end(t) !== null);
    if (timed.length) {
      let cur = timed.reduce((a, b) => end(b) > end(a) ? b : a);
      const ids = [cur.id], waits = [], seen = new Set(ids);
      for (;;) {
        const deps = list(cur.deps).map(d => byId.get(d)).filter(d => d && !seen.has(d.id) && end(d) !== null);
        if (!deps.length) break;
        const d = deps.reduce((a, b) => end(b) > end(a) ? b : a);
        const first = T.get(cur.id).first;
        if (first !== null && first - end(d) > 30) waits.push({ from: d.id, to: cur.id, seconds: first - end(d) });
        ids.unshift(d.id); seen.add(d.id); cur = d;
      }
      return { mode: 'exec', ids, waits };
    }
    // nothing ran yet: the heaviest chain by difficulty
    const best = new Map(), visiting = new Set();
    const walk = t => {
      if (best.has(t.id)) return best.get(t.id);
      if (visiting.has(t.id)) return { w: 0, path: [] };
      visiting.add(t.id);
      let top = { w: 0, path: [] };
      for (const d of list(t.deps)) { const dt = byId.get(d); if (dt && dt !== t) { const r = walk(dt); if (r.w > top.w) top = r; } }
      visiting.delete(t.id);
      const r = { w: top.w + weight(t), path: top.path.concat(t.id) }; best.set(t.id, r); return r;
    };
    let top = { w: 0, path: [] };
    for (const t of tasks) { const r = walk(t); if (r.w > top.w) top = r; }
    return { mode: 'plan', ids: top.path, waits: [] };
  }
  function concurrency(run, { now = null } = {}) {
    const ev = [];
    for (const t of list(run && run.tasks)) for (const e of list(t.executions)) {
      if (!['task', 'dwarf', 'qa', 'solo'].includes(e.role)) continue;
      const s = num(e.started), x = execEnd(e, now); if (s === null || x === null) continue;
      const key = e.role === 'solo' ? 'task' : e.role;
      ev.push([s, key, 1], [Math.max(s, x), key, -1]);
    }
    ev.sort((a, b) => a[0] - b[0] || a[2] - b[2]);
    const cur = { task: 0, dwarf: 0, qa: 0 }, series = [];
    let peak = 0;
    for (const [t, k, d] of ev) {
      cur[k] += d;
      if (series.length && series[series.length - 1].t === t) Object.assign(series[series.length - 1], cur);
      else series.push({ t, ...cur });
      peak = Math.max(peak, cur.task, cur.dwarf + cur.qa);
    }
    return { series, peak, cap: num(run && run.settings && run.settings.max_parallel) || 3 };
  }

  /* ---------- replay: what every node looked like at time t ---------- */
  function stateAt(run, t) {
    const tasks = {};
    for (const task of list(run && run.tasks)) {
      const execs = list(task.executions);
      let status = 'PENDING', role = null;
      if (num(task.merged_at) !== null && task.merged_at <= t) status = 'MERGED';
      else {
        const cover = execs.filter(e => num(e.started) !== null && e.started <= t && (execEnd(e, t) === null || execEnd(e, t) > t || (e.state === 'running' && num(e.ended) === null)));
        const top = cover.find(e => e.role === 'task' || e.role === 'solo') || cover[0];
        if (top) { status = 'RUNNING'; const k = cover.find(e => e.role === 'dwarf' || e.role === 'qa'); role = k ? k.role : null; }
        else {
          const rows = list(task.attempts).filter(r => num(r.at) !== null && r.at <= t);
          if (rows.length) status = rows[rows.length - 1].status;
          else if (execs.some(e => num(e.started) !== null && e.started <= t)) status = 'RUNNING';
        }
      }
      tasks[task.id] = { status, role };
    }
    const fr = list(run && run.final_reviews).filter(r => (num(r.started) ?? num(r.at) ?? Infinity) <= t);
    const last = fr.sort((a, b) => (a.at || 0) - (b.at || 0)).pop();
    const final = !last ? 'NONE' : num(last.ended) !== null && last.ended <= t ? (last.verdict || 'UNKNOWN') : 'RUNNING';
    const vs = list(run && run.timeline && run.timeline.verifications).filter(v => num(v.started) !== null && v.started <= t);
    const lv = vs.pop();
    const verification = !lv ? 'PENDING' : num(lv.ended) !== null && lv.ended <= t ? (lv.exit_code === 0 ? 'PASS' : 'FAIL') : 'RUNNING';
    return { t, tasks, final, verification, integration: integrationStatus(Object.values(tasks).map(s => s.status)) };
  }
  function timeRange(run, now) {
    const s = [], e = [];
    for (const t of list(run && run.tasks)) for (const x of list(t.executions)) { if (num(x.started) !== null) s.push(x.started); const end = execEnd(x, now); if (end !== null) e.push(end); }
    for (const r of list(run && run.final_reviews)) { const v = num(r.started) ?? num(r.at); if (v !== null) s.push(v); if (num(r.ended) !== null) e.push(r.ended); }
    for (const v of list(run && run.timeline && run.timeline.verifications)) { if (num(v.started) !== null) s.push(v.started); if (num(v.ended) !== null) e.push(v.ended); }
    if (!s.length) return null;
    return { start: Math.min(...s), end: Math.max(...e, ...s) };
  }
  // Events worth flashing during a replay: attempts ending, escalations, merges, reviews.
  function events(run) {
    const out = [];
    for (const t of list(run && run.tasks)) {
      for (const a of list(t.attempts)) if (num(a.at) !== null) out.push({ t: a.at, kind: 'attempt', task: t.id, status: a.status, attempt: a.attempt });
      for (const e of list(t.escalations)) if (num(e.at) !== null) out.push({ t: e.at, kind: 'escalation', task: t.id, from: e.from, to: e.to });
      if (num(t.merged_at) !== null) out.push({ t: t.merged_at, kind: 'merge', task: t.id });
    }
    for (const r of list(run && run.final_reviews)) if (num(r.ended) !== null) out.push({ t: r.ended, kind: 'review', name: r.name, verdict: r.verdict });
    return out.sort((a, b) => a.t - b.t);
  }

  // Changes when what is drawn changes shape (not when a status or a time moves).
  function structureKey(run) {
    return JSON.stringify([run && run.key, run && run.kind,
      list(run && run.tasks).map(t => [t.id, list(t.deps), t.wave, t.difficulty]),
      !!(run && run.final_review), list(run && run.final_reviews).length]);
  }
  function innerKey(task) {
    return JSON.stringify([list(task && task.executions).filter(e => e.role === 'dwarf' || e.role === 'qa').map(e => e.id), list(task && task.shadow).map(s => [s.attempt, s.k]), list(task && task.escalations).length]);
  }

  /* ---------- quick jump: an index of runs and tasks, ranked against what was typed ---------- */
  function searchIndex(runs) {
    const out = [];
    for (const r of list(runs)) {
      const goal = String(r.goal || 'Untitled run').split('\n')[0], n = num(r.task_count) || list(r.task_statuses).length;
      out.push({ id: 'run:' + r.key, kind: 'run', key: r.key, label: goal, sub: `${basename(r.repo)} · ${r.run_id || r.key} · ${r.kind === 'solo' ? 'solo' : n + ' task' + (n === 1 ? '' : 's')}`,
        ids: [r.run_id, r.key].filter(Boolean), words: [basename(r.repo), ...list(r.models)], t: num(r.updated) || 0, state: runState(r), href: '#/runs/' + encodeURIComponent(r.key) });
      if (r.kind === 'solo') continue;
      for (const t of list(r.task_statuses)) {
        if (!t || !t.id) continue;
        out.push({ id: `task:${r.key}:${t.id}`, kind: 'task', key: r.key, task: t.id, label: t.title ? `${t.id} · ${t.title}` : t.id, sub: `${goal} · ${String(t.status || 'PENDING').toLowerCase()}`,
          ids: [t.id], words: [r.run_id, basename(r.repo)].filter(Boolean), t: num(r.updated) || 0, status: t.status, subSearch: false,   // not found through its run's goal
          href: `#/runs/${encodeURIComponent(r.key)}/tasks/${encodeURIComponent(t.id)}` });
      }
    }
    return out;
  }
  // how well one query token matches a text: exact, prefix, word start, substring, then a loose subsequence
  function tokenScore(text, q, loose = true) {
    const s = String(text || '').toLowerCase(); if (!s || !q) return null;
    if (s === q) return { score: 100, at: 0, len: q.length };
    const i = s.indexOf(q);
    if (i === 0) return { score: 80, at: 0, len: q.length };
    if (i > 0) return { score: /[^a-z0-9]/.test(s[i - 1]) ? 65 : 50, at: i, len: q.length };
    if (!loose || q.length < 2) return null;
    let j = 0, run = 0, best = 0, starts = 0, first = -1, last = -1;
    for (let k = 0; k < s.length && j < q.length; k++) {
      if (s[k] !== q[j]) { run = 0; continue; }
      if (first < 0) first = k;
      run++; best = Math.max(best, run); if (k === 0 || /[^a-z0-9]/.test(s[k - 1])) starts++;
      j++; last = k;
    }
    if (j < q.length || last - first + 1 > q.length * 3) return null;     // all of it, close together
    return { score: Math.max(5, 18 + best * 3 + starts * 4 - (s.length - q.length) / 8), at: -1, len: 0 };
  }
  // entries from searchIndex() and any others ({id, kind, label, sub, ids, words, t}); recent: {id: epoch} picked before
  function searchRank(entries, query, { now = null, recent = {}, limit = 40 } = {}) {
    const q = String(query || '').trim().toLowerCase(), tokens = q.split(/\s+/).filter(Boolean);
    const t0 = num(now) ?? Math.max(0, ...list(entries).map(e => num(e.t) || 0));
    const fresh = e => num(e.t) ? 12 * Math.exp(-Math.max(0, t0 - e.t) / (7 * 86400)) : 0;
    const scored = [];
    for (const e of list(entries)) {
      let total = 0, mark = null;
      if (!tokens.length) total = (recent[e.id] ? 200 + (recent[e.id] / 1e10) : 0) + (e.state === 'live' ? 30 : 0) + fresh(e) + (e.kind === 'run' ? 5 : e.kind === 'command' ? 1 : -50);
      else {
        let ok = true;
        for (const tok of tokens) {
          let best = null;
          const fields = [[e.label, 1, true], ...list(e.ids).map(v => [v, 1.15, true]), ...(e.subSearch === false ? [] : [[e.sub, .55, false]]), ...list(e.words).map(v => [v, .6, false])];
          for (const [text, w, loose] of fields) {
            const m = tokenScore(text, tok, loose); if (!m) continue;
            const sc = m.score * w;
            if (!best || sc > best.sc) best = { sc };
          }
          if (!best) { ok = false; break; }
          total += best.sc;
          const at = String(e.label || '').toLowerCase().indexOf(tok);
          if (at >= 0 && !mark) mark = [at, at + tok.length];
        }
        if (!ok) continue;
        total += fresh(e) + (recent[e.id] ? 15 : 0) + (e.state === 'live' ? 4 : 0);
      }
      if (total > 0 || !tokens.length) scored.push({ ...e, score: total, mark });
    }
    scored.sort((a, b) => b.score - a.score || (b.t || 0) - (a.t || 0) || String(a.label).localeCompare(String(b.label)));
    return scored.slice(0, limit);
  }

  /* ---------- notifications: what changed between two polls of the run list ---------- */
  const FAILING = new Set(['FAIL', 'ERROR', 'CONFLICT', 'TIMEOUT', 'INVALIDATED', 'INTERRUPTED', 'BLOCKED']);
  // prev, next: RunSummary lists (prev null on the first poll, which is only a baseline). A run that has not
  // changed for two hours (stale: its process died) never fires.
  function statusEvents(prev, next, { now = null, staleAfter = 7200 } = {}) {
    if (!Array.isArray(prev)) return [];
    const before = new Map(prev.map(r => [r.key, r])), out = [], t = num(now);
    for (const r of list(next)) {
      const p = before.get(r.key); if (!p) continue;
      if (t !== null && num(r.updated) !== null && t - r.updated > staleAfter) continue;
      const goal = String(r.goal || 'Untitled run').split('\n')[0], base = { key: r.key, run_id: r.run_id || '', goal, repo: basename(r.repo), t: num(r.updated) };
      const was = runState(p), is = runState(r);
      if (was === 'live' && (is === 'done' || is === 'failed')) out.push({ ...base, kind: is === 'done' ? 'run-done' : 'run-failed', id: `run|${r.key}|${is}`, href: '#/runs/' + encodeURIComponent(r.key) });
      const old = new Map(list(p.task_statuses).map(x => [x.id, x.status]));
      for (const x of list(r.task_statuses)) {
        if (!FAILING.has(x.status) || old.get(x.id) === x.status || !old.has(x.id)) continue;
        out.push({ ...base, kind: 'task-failed', task: x.id, status: x.status, title: x.title || '', id: `task|${r.key}|${x.id}|${x.status}`, href: `#/runs/${encodeURIComponent(r.key)}/tasks/${encodeURIComponent(x.id)}` });
      }
      if ((r.final_review === 'PASS' || r.final_review === 'FAIL') && p.final_review !== r.final_review) out.push({ ...base, kind: 'review', verdict: r.final_review, id: `review|${r.key}|${r.final_review}`, href: `#/runs/${encodeURIComponent(r.key)}/final-review` });
    }
    return out;
  }

  /* ---------- Fractal: a task's child tasks as a tidy tree (depth across, siblings down) ---------- */
  // nodes: TaskFractal.nodes (tree order, parent ids); returns {pos: {id: {col, row, depth}}, edges: [[parent, child]], cols, rows}
  function fractalTree(nodes) {
    const ns = list(nodes).filter(n => n && n.id), ids = new Set(ns.map(n => n.id)), kids = new Map();
    const roots = [];
    for (const n of ns) {
      const p = n.parent && ids.has(n.parent) && n.parent !== n.id ? n.parent : null;
      if (p) { if (!kids.has(p)) kids.set(p, []); kids.get(p).push(n.id); } else roots.push(n.id);
    }
    const pos = {}, edges = [], seen = new Set();
    let next = 0, cols = 0;
    const place = (id, depth) => {
      if (seen.has(id)) return null; seen.add(id);                         // a cycle in bad data stops here
      const ch = (kids.get(id) || []).map(k => place(k, depth + 1)).filter(r => r !== null);
      for (const k of kids.get(id) || []) if (pos[k]) edges.push([id, k]);
      const row = ch.length ? (ch[0] + ch[ch.length - 1]) / 2 : next++;
      pos[id] = { col: depth, row, depth }; cols = Math.max(cols, depth + 1);
      return row;
    };
    for (const r of roots) place(r, 0);
    for (const n of ns) if (!seen.has(n.id)) place(n.id, 0);               // what a cycle left out
    return { pos, edges, cols, rows: next };
  }

  return { plate, cells, blockSize, liveNow, integrationStatus, shadowStatus, runState, execStatus, matchRun, filterOptions, atlasModel, columns, planModel, finishState, taskParts,
    taskTimes, metricValue, overlap, projectSchedule, execModel, criticalPath, concurrency, stateAt, timeRange, events, structureKey, innerKey,
    fmtDur, alias, basename, weight, searchIndex, searchRank, statusEvents, fractalTree };
});
