/* forge-ui cloud model: pure data for the run cloud (no DOM, no I/O). Groups runs by project, filters
   them, and places them in one of five layouts. A plain script in the page (window.ForgeCloudModel) and a
   CommonJS module under node:test. Coordinates are plan units (centimetres; x right, y toward the viewer,
   z up), the same as iso.js's W(), and a position is the centre of a run's footprint at its base. */
(function (root, factory) {
  'use strict';
  const RM = typeof module === 'object' && module.exports ? require('./runmodel.js') : root.ForgeRunModel;
  const api = factory(RM);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ForgeCloudModel = api;
})(typeof self !== 'undefined' ? self : this, function (RM) {
  'use strict';

  const list = v => Array.isArray(v) ? v : [];
  const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const GOLDEN = Math.PI * (3 - Math.sqrt(5));
  const LAYOUTS = ['helix', 'sphere', 'galaxy', 'timeline', 'constellation'];
  const LAYOUT_INFO = {
    helix: { name: 'Helix', elevation: 18, azimuth: 45, blurb: 'One spiral per project: oldest runs at the bottom, newest at the top.' },
    sphere: { name: 'Sphere', elevation: 24, azimuth: 45, blurb: 'Runs spread evenly over a shell per project, oldest at the bottom pole.' },
    galaxy: { name: 'Galaxy', elevation: 55, azimuth: 45, blurb: 'A flat spiral disc per project, newest at the centre.' },
    timeline: { name: 'Timeline', elevation: 52, azimuth: 90, blurb: 'Left to right is when each run started; one lane per project.' },
    constellation: { name: 'Constellation', elevation: 24, azimuth: 45, blurb: 'Runs pulled together by project and by the models they share; lines join each project\'s nearest runs.' },
  };
  const DIFFS = ['low', 'medium', 'high'];

  // FNV-1a: a short stable id for a project (its repo path)
  function hashId(s) {
    let h = 0x811c9dc5;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; }
    return h.toString(16).padStart(8, '0');
  }
  const unit = s => hashId(String(s)).split('').reduce((a, c) => (a * 16 + parseInt(c, 16)) % 1e9, 7) / 1e9;   // [0, 1)
  const startOf = r => num(r.created) ?? num(r.updated) ?? 0;

  /* ---------- projects and per-run facts ---------- */
  function projects(runs) {
    const by = new Map();
    for (const r of list(runs)) {
      const repo = r.repo || '?', id = hashId(repo);
      if (!by.has(id)) by.set(id, { id, repo: r.repo || null, name: RM.basename(r.repo), keys: [], first: Infinity, last: -Infinity, count: 0 });
      const p = by.get(id), t = startOf(r);
      p.keys.push(r.key); p.count++; p.first = Math.min(p.first, t); p.last = Math.max(p.last, num(r.updated) ?? t);
    }
    const out = [...by.values()].sort((a, b) => a.first - b.first || a.id.localeCompare(b.id));
    // two repos with the same folder name: add the parent folder
    const names = new Map(); for (const p of out) names.set(p.name, (names.get(p.name) || 0) + 1);
    for (const p of out) if (names.get(p.name) > 1 && p.repo) { const parts = String(p.repo).split(/[\\/]/).filter(Boolean); p.name = `${p.name} (${parts[parts.length - 2] || '/'})`; }
    return out;
  }
  const bucket = n => clamp(Math.round(Math.sqrt(Math.max(1, n))), 1, 9);
  const blockSize = b => ({ s: 70 + 24 * b, h: 40 + 6 * b });
  const orbRadius = b => 26 + 9 * b;
  function runFacts(run, now) {
    const tasks = list(run.task_statuses);
    const diff = { low: 0, medium: 0, high: 0, none: 0 };
    for (const t of tasks) { const d = String(t.difficulty || '').toLowerCase(); diff[DIFFS.includes(d) ? d : 'none']++; }
    const dominant = [...DIFFS, 'none'].reduce((a, k) => diff[k] > diff[a] ? k : a, 'none');
    let state = RM.runState(run);
    if (state === 'live' && num(now) !== null && num(run.updated) !== null && now - run.updated > 7200) state = 'stale';
    const n = Math.max(1, num(run.task_count) || tasks.length || 1);
    const aliases = specs => new Set(list(specs).map(RM.alias).filter(Boolean));
    return { key: run.key, run, project: hashId(run.repo || '?'), n, bucket: bucket(n), diff, dominant, state, t: startOf(run),
      dwarf: aliases(run.dwarf_models || run.models), qa: aliases(run.qa_models || []), all: aliases(run.models),
      review: RM.runReviewStatus(run) || null, verification: run.verification_status || null, decompose: run.decompose_level || null, kind: run.kind };
  }

  /* ---------- filters: the cloud shows exactly the runs that match ---------- */
  function emptyFilter() {
    return { text: '', projects: [], states: [], kinds: [], difficulty: { levels: [], mode: 'contains' }, tasks: { min: null, max: null },
      models: { aliases: [], role: 'any' }, activeDays: 0, review: '', verification: '', decompose: [] };
  }
  function normFilter(f) {
    const e = emptyFilter(); f = f || {};
    return { ...e, ...f, difficulty: { ...e.difficulty, ...(f.difficulty || {}) }, tasks: { ...e.tasks, ...(f.tasks || {}) }, models: { ...e.models, ...(f.models || {}) } };
  }
  function activeCount(f) {
    f = normFilter(f);
    return [f.text.trim(), f.projects.length, f.states.length, f.kinds.length, f.difficulty.levels.length, num(f.tasks.min) !== null || num(f.tasks.max) !== null,
      f.models.aliases.length, f.activeDays, f.review, f.verification, f.decompose.length].filter(Boolean).length;
  }
  function verdictMatch(want, have) { return !want || (want === 'none' ? !have : have === want); }
  function match(fact, f, now) {
    f = normFilter(f);
    const r = fact.run;
    const text = f.text.trim().toLowerCase();
    if (text) {
      const hay = [r.goal, r.repo, r.run_id, r.path, ...list(r.models), ...list(r.task_statuses).flatMap(t => [t.id, t.title])];
      if (!hay.some(v => String(v || '').toLowerCase().includes(text))) return false;
    }
    if (f.projects.length && !f.projects.includes(fact.project)) return false;
    if (f.states.length && !f.states.includes(fact.state)) return false;
    if (f.kinds.length && !f.kinds.includes(fact.kind)) return false;
    if (f.difficulty.levels.length) {
      const ok = f.difficulty.mode === 'dominant' ? f.difficulty.levels.includes(fact.dominant) : f.difficulty.levels.some(l => fact.diff[l] > 0);
      if (!ok) return false;
    }
    if (num(f.tasks.min) !== null && fact.n < f.tasks.min) return false;
    if (num(f.tasks.max) !== null && fact.n > f.tasks.max) return false;
    if (f.models.aliases.length) {
      const pool = f.models.role === 'dwarf' ? fact.dwarf : f.models.role === 'qa' ? fact.qa : fact.all;
      if (!f.models.aliases.some(a => pool.has(a))) return false;
    }
    if (f.activeDays && num(now) !== null && !(num(r.updated) !== null && r.updated >= now - f.activeDays * 86400)) return false;
    if (!verdictMatch(f.review, fact.review)) return false;
    if (!verdictMatch(f.verification, fact.verification)) return false;
    if (f.decompose.length && !f.decompose.includes(fact.decompose || 'none')) return false;
    return true;
  }
  function options(facts) {
    const states = {}, kinds = {}, diff = { low: 0, medium: 0, high: 0, none: 0 }, aliases = new Map(), decompose = {};
    let maxTasks = 1;
    for (const f of facts) {
      states[f.state] = (states[f.state] || 0) + 1; kinds[f.kind] = (kinds[f.kind] || 0) + 1;
      for (const k of Object.keys(diff)) if (f.diff[k]) diff[k]++;
      for (const a of f.all) aliases.set(a, (aliases.get(a) || 0) + 1);
      const d = f.decompose || 'none'; decompose[d] = (decompose[d] || 0) + 1;
      maxTasks = Math.max(maxTasks, f.n);
    }
    return { states, kinds, difficulty: diff, aliases: [...aliases].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([alias, count]) => ({ alias, count })), decompose, maxTasks };
  }

  /* ---------- cluster anchors: computed from every run, so a filter never moves a project ---------- */
  function clusterRadius(layout, m, smax) {
    if (layout === 'sphere') return Math.max(smax, Math.sqrt(m * (1.3 * smax) ** 2 / (4 * Math.PI))) + smax;
    if (layout === 'galaxy') return 1.05 * smax * Math.sqrt(m + 1) + smax;
    const k = clamp(Math.round(4 + 2 * Math.sqrt(m)), 6, 24);
    return Math.max(.9 * smax, k * 1.35 * smax / (2 * Math.PI)) + smax;
  }
  function anchors(layout, projs, countOf, smax) {
    const placed = [], out = new Map(), gap = 140, step = 220;
    for (const p of projs) {
      const R = Math.ceil(clusterRadius(layout, countOf(p), smax) / 50) * 50;   // quantised: a few more runs do not move it
      for (let j = 0; j < 100000; j++) {
        const r = step * Math.sqrt(j), th = j * GOLDEN, x = r * Math.cos(th), y = r * Math.sin(th);
        if (placed.every(q => Math.hypot(q.x - x, q.y - y) >= q.R + R + gap)) { placed.push({ x, y, R }); out.set(p.id, { x, y, R }); break; }
      }
    }
    return out;
  }

  /* ---------- the layouts ---------- */
  function sizes(facts) {
    let smax = 0, hmax = 0;
    for (const f of facts) { const b = blockSize(f.bucket); smax = Math.max(smax, b.s); hmax = Math.max(hmax, b.h); }
    return { smax: smax || blockSize(1).s, hmax: hmax || blockSize(1).h };
  }
  function helix(group, c, smax, hmax, pid) {
    const m = group.length, k = clamp(Math.round(4 + 2 * Math.sqrt(m)), 6, 24);
    const a = 1.35 * smax, R = Math.max(.9 * smax, k * a / (2 * Math.PI)), th0 = unit(pid) * Math.PI * 2;
    const pos = {};
    group.forEach((f, i) => { const th = th0 + i * 2 * Math.PI / k; pos[f.key] = [c.x + R * Math.cos(th), c.y + R * Math.sin(th), i * (hmax + 40) / k]; });
    return { pos, ring: R, top: m ? (m - 1) * (hmax + 40) / k + hmax : 0 };
  }
  function sphere(group, c, smax) {
    const m = group.length, R = Math.max(smax, Math.sqrt(m * (1.3 * smax) ** 2 / (4 * Math.PI))), pos = {};
    group.forEach((f, i) => {
      const yy = -1 + (2 * i + 1) / Math.max(1, m), rr = Math.sqrt(Math.max(0, 1 - yy * yy)), ph = i * GOLDEN;
      pos[f.key] = [c.x + R * rr * Math.cos(ph), c.y + R * rr * Math.sin(ph), R * (1 + yy)];
    });
    return { pos, ring: R, top: 2 * R + smax };
  }
  function galaxy(group, c, smax) {
    const m = group.length, cc = 1.05 * smax, pos = {};          // blocks are axis-aligned squares: wide enough apart on any diagonal
    group.slice().reverse().forEach((f, i) => {                // newest first, at the centre
      const r = cc * Math.sqrt(i + .5), th = i * GOLDEN + unit(f.key) * .2;
      pos[f.key] = [c.x + r * Math.cos(th), c.y + r * Math.sin(th), (unit(f.key + '*') - .5) * 30 + 15];
    });
    return { pos, ring: cc * Math.sqrt(m + .5) + smax / 2, top: smax };
  }
  function niceAxis(t0, t1, L) {
    const span = Math.max(1, t1 - t0), units = [[3600, 'hour'], [6 * 3600, '6h'], [86400, 'day'], [7 * 86400, 'week'], [30 * 86400, 'month'], [365 * 86400, 'year']];
    const [step] = units.find(([u]) => span / u <= 12) || units[units.length - 1];
    const ticks = [];
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) ticks.push({ t, x: (t - t0) / span * L });
    return { t0, t1, step, length: L, ticks };
  }
  function timeline(facts, projs, smax, hmax) {
    const N = facts.length, L = clamp(400 * Math.sqrt(Math.max(1, N)), 2000, 30000);
    const t0 = Math.min(...facts.map(f => f.t)), t1 = Math.max(...facts.map(f => f.t), t0 + 1);
    const pos = {}, clusters = [], laneD = 1.5 * smax;
    let lane = 0;
    for (const p of projs) {
      const group = facts.filter(f => f.project === p.id).sort((a, b) => a.t - b.t);
      if (!group.length) continue;
      const levels = [];
      for (const f of group) {
        const s = blockSize(f.bucket).s, x = (f.t - t0) / (t1 - t0) * L;
        let lv = levels.findIndex(end => end <= x - s / 2 - 10);
        if (lv < 0) { lv = levels.length; levels.push(0); }
        levels[lv] = x + s / 2;
        pos[f.key] = [x, lane * laneD, lv * (hmax + 20)];
      }
      clusters.push({ pid: p.id, center: [L / 2, lane * laneD], radius: L / 2 + smax, lane: [-smax, lane * laneD - laneD / 2 + 8, L + 2 * smax, laneD - 16], z0: 0, z1: levels.length * (hmax + 20), shape: 'lane' });
      lane++;
    }
    return { pos, clusters, axis: niceAxis(t0, t1, L) };
  }
  // a seeded PRNG, so the same runs always settle the same way
  function mulberry32(a) { return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
  function jaccard(a, b) { if (!a.size || !b.size) return 0; let n = 0; for (const x of a) if (b.has(x)) n++; return n / (a.size + b.size - n); }
  // 3D force layout: springs between runs of one project and runs that share models, repulsion within a cutoff
  function forceLayout(facts, { iterations = 300, seed = 7 } = {}) {
    const N = facts.length, rnd = mulberry32(seed), s = sizes(facts).smax, R0 = 100 * Math.sqrt(Math.max(1, N));
    const P = facts.map(() => [(rnd() - .5) * 2 * R0, (rnd() - .5) * 2 * R0, rnd() * R0]);
    const links = [];
    const byNode = facts.map(() => []);
    for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
      const same = facts[i].project === facts[j].project, J = jaccard(facts[i].all, facts[j].all);
      if (same) byNode[i].push([j, 1, 3.2 * s]), byNode[j].push([i, 1, 3.2 * s]);
      else if (J >= .34) byNode[i].push([j, .3 * J, 6 * s]), byNode[j].push([i, .3 * J, 6 * s]);
    }
    const seen = new Set();
    byNode.forEach((ls, i) => ls.sort((a, b) => b[1] - a[1]).slice(0, 12).forEach(([j, w, rest]) => {
      const k = i < j ? i + ',' + j : j + ',' + i; if (!seen.has(k)) { seen.add(k); links.push([Math.min(i, j), Math.max(i, j), w, rest]); }
    }));
    const cell = 8 * s;
    for (let it = 0; it < iterations; it++) {
      const cool = 1 - it / iterations, F = P.map(() => [0, 0, 0]);
      for (const [i, j, w, rest] of links) {
        const d = [P[j][0] - P[i][0], P[j][1] - P[i][1], P[j][2] - P[i][2]], L = Math.hypot(...d) || 1, f = w * (L - rest) / L * .1;
        for (let k = 0; k < 3; k++) { F[i][k] += d[k] * f; F[j][k] -= d[k] * f; }
      }
      const grid = new Map(), keyOf = p => p.map(v => Math.floor(v / cell)).join(',');
      P.forEach((p, i) => { const k = keyOf(p); if (!grid.has(k)) grid.set(k, []); grid.get(k).push(i); });
      for (let i = 0; i < N; i++) {
        const [cx, cy, cz] = P[i].map(v => Math.floor(v / cell));
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
          for (const j of grid.get(`${cx + dx},${cy + dy},${cz + dz}`) || []) {
            if (j <= i) continue;
            const d = [P[i][0] - P[j][0], P[i][1] - P[j][1], P[i][2] - P[j][2]];
            let L = Math.hypot(...d); if (L > cell) continue;
            if (L < 1e-3) { d[0] = rnd() - .5; d[1] = rnd() - .5; L = Math.hypot(...d) || 1; }
            const f = (2.6 * s) * (2.6 * s) / (L * L) * .5;
            for (let k = 0; k < 3; k++) { F[i][k] += d[k] / L * f; F[j][k] -= d[k] / L * f; }
          }
        }
      }
      for (let i = 0; i < N; i++) {
        for (let k = 0; k < 3; k++) {
          F[i][k] -= P[i][k] * .002;                        // weak gravity toward the middle
          const step = clamp(F[i][k], -s, s) * cool;
          P[i][k] += step;
        }
      }
    }
    const zmin = Math.min(0, ...P.map(p => p[2]));
    const pos = {};
    facts.forEach((f, i) => { pos[f.key] = [P[i][0], P[i][1], P[i][2] - zmin]; });
    return pos;
  }

  // push apart any two runs whose blocks intersect, along the axis where they overlap least; pinned runs stay put
  function resolve(pos, facts, fixed, iterations = 60) {
    const items = facts.filter(f => pos[f.key]).map(f => ({ p: pos[f.key], b: blockSize(f.bucket), fixed: !!fixed[f.key] }));
    const cell = sizes(facts).smax + 8, key = (x, y, z) => x + ',' + y + ',' + z;
    for (let it = 0; it < iterations; it++) {
      const grid = new Map();
      items.forEach((a, i) => { const k = key(...a.p.map(v => Math.floor(v / cell))); if (!grid.has(k)) grid.set(k, []); grid.get(k).push(i); });
      let moved = false;
      items.forEach((a, i) => {
        const [cx, cy, cz] = a.p.map(v => Math.floor(v / cell));
        for (let dx = -1; dx <= 1; dx++) for (let dy = -1; dy <= 1; dy++) for (let dz = -1; dz <= 1; dz++) {
          for (const j of grid.get(key(cx + dx, cy + dy, cz + dz)) || []) {
            if (j <= i) continue;
            const b = items[j]; if (a.fixed && b.fixed) continue;
            const ox = (a.b.s + b.b.s) / 2 + 4 - Math.abs(a.p[0] - b.p[0]), oy = (a.b.s + b.b.s) / 2 + 4 - Math.abs(a.p[1] - b.p[1]);
            const oz = Math.min(a.p[2] + a.b.h, b.p[2] + b.b.h) - Math.max(a.p[2], b.p[2]) + 4;
            if (ox <= 0 || oy <= 0 || oz <= 0) continue;
            const m = Math.min(ox, oy, oz), ax = m === ox ? 0 : m === oy ? 1 : 2, dir = Math.sign(a.p[ax] - b.p[ax]) || (i % 2 ? 1 : -1);
            const wa = a.fixed ? 0 : b.fixed ? 1 : .5;
            a.p[ax] += dir * m * wa; b.p[ax] -= dir * m * (1 - wa); moved = true;
          }
        }
      });
      if (!moved) break;
    }
  }
  // each project's runs joined by their minimum spanning tree (the constellation's lines): pairs of keys
  function constellationLinks(facts, pos) {
    const out = [], by = new Map();
    for (const f of list(facts)) if (pos[f.key]) { if (!by.has(f.project)) by.set(f.project, []); by.get(f.project).push(f.key); }
    for (const keys of by.values()) {
      if (keys.length < 2) continue;
      const n = keys.length, inTree = new Uint8Array(n), best = new Array(n).fill(Infinity), from = new Array(n).fill(-1);
      const d = (i, j) => { const a = pos[keys[i]], b = pos[keys[j]]; return Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]); };
      best[0] = 0;
      for (let k = 0; k < n; k++) {
        let u = -1; for (let i = 0; i < n; i++) if (!inTree[i] && (u < 0 || best[i] < best[u])) u = i;
        inTree[u] = 1; if (from[u] >= 0) out.push([keys[from[u]], keys[u]]);
        for (let v = 0; v < n; v++) if (!inTree[v]) { const w = d(u, v); if (w < best[v]) { best[v] = w; from[v] = u; } }
      }
    }
    return out;
  }

  // facts: the runs to place (filtered); all: every run (for cluster anchors); pinned: {key: [x,y,z]} of dragged runs
  function layout(name, facts, { all = null, pinned = {}, seed = 7 } = {}) {
    name = LAYOUTS.includes(name) ? name : 'helix';
    facts = list(facts); const everyone = list(all || facts);
    const projs = projects(everyone.map(f => f.run));
    const { smax, hmax } = sizes(everyone);
    let pos = {}, clusters = [], axis = null;
    if (name === 'timeline') ({ pos, clusters, axis } = facts.length ? timeline(facts, projs, smax, hmax) : { pos: {}, clusters: [], axis: null });
    else if (name === 'constellation') pos = forceLayout(facts, { seed });       // pins are applied after: a drag never moves the others
    else {
      const total = new Map(projs.map(p => [p.id, everyone.filter(f => f.project === p.id).length]));
      const A = anchors(name, projs, p => total.get(p.id), smax);
      for (const p of projs) {
        const group = facts.filter(f => f.project === p.id).sort((a, b) => a.t - b.t || a.key.localeCompare(b.key));
        if (!group.length) continue;
        const c = A.get(p.id);
        const r = name === 'sphere' ? sphere(group, c, smax) : name === 'galaxy' ? galaxy(group, c, smax) : helix(group, c, smax, hmax, p.id);
        Object.assign(pos, r.pos);
        clusters.push({ pid: p.id, center: [c.x, c.y], radius: r.ring + smax / 2, z0: 0, z1: r.top, shape: name === 'sphere' ? 'shell' : 'ring' });
      }
    }
    if (name === 'constellation') {
      for (const p of projs) {
        const ps = facts.filter(f => f.project === p.id).map(f => pos[f.key]); if (!ps.length) continue;
        const cx = ps.reduce((a, q) => a + q[0], 0) / ps.length, cy = ps.reduce((a, q) => a + q[1], 0) / ps.length;
        const R = Math.max(smax, ...ps.map(q => Math.hypot(q[0] - cx, q[1] - cy))) + smax / 2;
        clusters.push({ pid: p.id, center: [cx, cy], radius: R, z0: 0, z1: Math.max(...ps.map(q => q[2])) + hmax, shape: 'cloud' });
      }
    }
    const fixed = {};
    for (const [k, v] of Object.entries(pinned || {})) if (pos[k] && Array.isArray(v) && v.length >= 3 && v.slice(0, 3).every(x => num(x) !== null)) { pos[k] = v.slice(0, 3); fixed[k] = true; }
    if (name === 'constellation' || name === 'sphere') resolve(pos, facts, fixed);
    const xs = [], ys = [], zs = [];
    for (const k of Object.keys(pos)) { const [x, y, z] = pos[k]; xs.push(x - smax, x + smax); ys.push(y - smax, y + smax); zs.push(z, z + hmax); }
    for (const c of clusters) { xs.push(c.center[0] - c.radius, c.center[0] + c.radius); ys.push(c.center[1] - c.radius, c.center[1] + c.radius); }
    const bounds = xs.length ? [Math.min(...xs), Math.min(...ys), Math.min(0, ...zs), Math.max(...xs), Math.max(...ys), Math.max(...zs, hmax)] : [-500, -500, 0, 500, 500, 100];
    return { name, pos, clusters, bounds, axis, smax, hmax, projects: projs };
  }
  // what changes the layout (not a status or a colour)
  function layoutKey(facts) { return JSON.stringify(list(facts).map(f => [f.key, f.bucket, f.project, Math.round(f.t)])); }

  /* ---------- labels: keep the biggest that do not overlap on screen ---------- */
  function labelPick(cands, { max = 32 } = {}) {
    const out = [], rects = [];
    for (const c of list(cands).slice().sort((a, b) => b.pri - a.pri)) {
      if (out.length >= max) break;
      const r = [c.x - c.w / 2, c.y - c.h, c.x + c.w / 2, c.y];
      if (rects.some(q => r[0] < q[2] && r[2] > q[0] && r[1] < q[3] && r[3] > q[1])) continue;
      rects.push(r); out.push(c.key);
    }
    return out;
  }

  /* ---------- the dragged positions, as a file to export and import ---------- */
  const KEY = /^[0-9a-f]{12}$/;
  function serializeLayout(store, labels, now) {
    return { format: 'forge-ui-cloud-layout', version: 1, exported_at: num(now) ?? null, layouts: store && store.layouts || {}, labels: labels || {} };
  }
  function parseLayoutFile(obj) {
    const errors = [], layouts = {};
    let count = 0;
    if (!obj || typeof obj !== 'object' || obj.format !== 'forge-ui-cloud-layout' || obj.version !== 1) return { ok: false, errors: ['not a forge-ui cloud layout file (version 1)'], layouts, count };
    for (const [name, entries] of Object.entries(obj.layouts || {})) {
      if (!LAYOUTS.includes(name)) { errors.push(`unknown layout ${name}`); continue; }
      if (!entries || typeof entries !== 'object') continue;
      layouts[name] = {};
      for (const [k, v] of Object.entries(entries)) {
        if (count >= 10000) { errors.push('more than 10000 positions: the rest were skipped'); break; }
        if (!KEY.test(k) || !Array.isArray(v) || v.length < 3 || !v.slice(0, 3).every(x => num(x) !== null && Math.abs(x) <= 1e6)) { errors.push(`bad entry ${String(k).slice(0, 20)}`); continue; }
        layouts[name][k] = v.slice(0, 4).map(Number); count++;
      }
    }
    return { ok: true, errors, layouts, count };
  }

  return { LAYOUTS, LAYOUT_INFO, hashId, projects, runFacts, blockSize, orbRadius, emptyFilter, normFilter, activeCount, match, options,
    layout, layoutKey, resolve, constellationLinks, niceAxis, labelPick, serializeLayout, parseLayoutFile };
});
