/* forge-ui execution chart (2D): every attempt on wall-clock time in the lane it ran in, with its dwarf and
   review inside, projected attempts for what has not started, and the concurrency strip under the stage.
   Defines window.ForgeGantt.{render, strip}. SVG drawn with classes only (style.css colours it). */
window.ForgeGantt = (() => {
  'use strict';
  const RM = window.ForgeRunModel;
  const NS = 'http://www.w3.org/2000/svg';
  const list = v => Array.isArray(v) ? v : [];
  function svg(name, attrs, parent) {
    const e = document.createElementNS(NS, name);
    for (const k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (parent) parent.append(e);
    return e;
  }
  const stc = s => 'st-' + String(s || 'pending').toLowerCase().replace(/[^a-z0-9_-]/g, '');
  const LANE = 34, LABEL = 70, TOP = 26;

  function render(container, run, { now = null, onSelect = null, sel = null, replay = null, critical = null, ghosts = true } = {}) {
    container.replaceChildren();
    const X = RM.execModel(run, { now, ghosts });
    const all = X.items.concat(X.extra);
    if (!all.length) { const p = document.createElement('p'); p.className = 'fg-empty'; p.textContent = 'Nothing has run yet: no attempt timings are recorded for this run.'; container.append(p); return; }
    const width = Math.max(640, container.clientWidth || 900), plot = width - LABEL - 12;
    const xs = t => LABEL + (t - X.t0) / (X.t1 - X.t0) * plot;
    const laneOf = it => typeof it.lane === 'number' ? it.lane : X.lanes + (it.kind === 'verification' ? 0 : X.finishLanes.length > 1 ? 1 : 0);
    const rows = X.lanes + X.finishLanes.length;
    const height = TOP + rows * LANE + 8;
    const root = svg('svg', { class: 'gantt', viewBox: `0 0 ${width} ${height}`, width, height, role: 'group', 'aria-label': 'Attempts on wall-clock time' });
    for (const r of X.ruler) {
      svg('line', { class: 'gt-tick', x1: xs(r.t), x2: xs(r.t), y1: TOP - 6, y2: height - 4 }, root);
      svg('text', { class: 'gt-tick-label', x: xs(r.t) + 3, y: TOP - 10 }, root).textContent = '+' + r.label;
    }
    for (let i = 0; i < rows; i++) {
      const over = i < X.lanes && i >= X.cap, y = TOP + i * LANE;
      svg('rect', { class: 'gt-lane' + (over ? ' gt-over' : ''), x: LABEL, y: y + 2, width: plot, height: LANE - 4, rx: 4 }, root);
      const name = i < X.lanes ? `lane ${i + 1}${over ? ' !' : ''}` : X.finishLanes[i - X.lanes].name.toLowerCase();
      const t = svg('text', { class: 'gt-lane-label' + (over ? ' gt-over' : ''), x: 6, y: y + LANE / 2 + 4 }, root); t.textContent = name;
      if (over) svg('title', {}, t).textContent = `More attempts overlapped than --max-parallel ${X.cap} allows: separate run invocations, or a resumed run.`;
    }
    const critSet = new Set(critical && critical.ids || []);
    for (const it of all) {
      const y = TOP + laneOf(it) * LANE + 5, x = xs(it.start), w = Math.max(3, xs(it.end) - x);
      const future = replay !== null && it.start > replay;
      const g = svg('g', { class: `gt-item ${stc(it.status)}${it.ghost ? ' gt-ghost' : ''}${future ? ' gt-future' : ''}${critSet.has(it.task) && !it.ghost ? ' gt-crit' : ''}${sel && (sel.exec === it.id || (sel.kind === 'task' && sel.id === it.task)) ? ' gt-sel' : ''}`, tabindex: 0, role: 'button' }, root);
      svg('rect', { class: 'gt-box', x, y, width: w, height: LANE - 10, rx: 3 }, g);
      for (const s of it.segs) {
        const sx = xs(s.start), sw = Math.max(1.5, xs(s.end ?? it.end) - sx);
        svg('rect', { class: `gt-seg gt-${s.role} ${stc(s.status)}`, x: sx, y: y + LANE - 17, width: sw, height: 5, rx: 1.5 }, g);
      }
      const label = it.kind === 'verification' ? 'verify' : it.kind === 'review' ? 'review' : `${it.task}${it.attempt ? ' A' + it.attempt : ''}`;
      if (w > label.length * 6.4 + 6) svg('text', { class: 'gt-label', x: x + 4, y: y + 11 }, g).textContent = label;
      svg('title', {}, g).textContent = `${label} · ${it.status} · ${RM.fmtDur(it.end - it.start)}${it.ghost ? ' (projected)' : ''}`;
      const pick = () => {
        if (!onSelect) return;
        if (it.kind === 'review') onSelect({ kind: 'final', name: it.review });
        else if (it.kind === 'verification') onSelect({ kind: 'execution', task: null, exec: it.id });
        else if (it.ghost || String(it.id).startsWith('span:')) onSelect({ kind: 'task', id: it.task });
        else onSelect({ kind: 'execution', task: it.task, id: it.task, exec: it.id });
      };
      g.addEventListener('click', pick);
      g.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } });
    }
    const cursorT = replay !== null ? replay : X.live && now !== null ? now : null;
    if (cursorT !== null && cursorT >= X.t0 && cursorT <= X.t1) svg('line', { class: 'gt-now', x1: xs(cursorT), x2: xs(cursorT), y1: TOP - 8, y2: height - 2 }, root);
    container.append(root);
  }

  // How many attempts, dwarves and reviewers were busy at each moment, against the concurrency limit.
  function strip(container, run, { now = null, replay = null } = {}) {
    container.replaceChildren();
    const c = RM.concurrency(run, { now }), range = RM.timeRange(run, now);
    if (!c.series.length || !range) return;
    const width = Math.max(640, container.clientWidth || 900), H = 62, plot = width - LABEL - 12;
    const t0 = range.start, t1 = Math.max(range.end, now !== null && list(run.tasks).some(t => t.status === 'RUNNING') ? now : range.end, t0 + 1);
    const xs = t => LABEL + (t - t0) / (t1 - t0) * plot;
    const top = Math.max(c.cap, c.peak, 1), ys = v => H - 8 - v / top * (H - 18);
    const root = svg('svg', { class: 'strip', viewBox: `0 0 ${width} ${H}`, width, height: H, role: 'img', 'aria-label': `Concurrency: peak ${c.peak} attempts at once, limit ${c.cap}` });
    const step = key => {
      let d = `M${xs(c.series[0].t)} ${ys(0)}`, prev = 0;
      for (const p of c.series) { d += ` L${xs(p.t)} ${ys(prev)} L${xs(p.t)} ${ys(p[key])}`; prev = p[key]; }
      return d + ` L${xs(t1)} ${ys(prev)} L${xs(t1)} ${ys(0)} Z`;
    };
    svg('path', { class: 'cc-area cc-tasks', d: step('task') }, root);
    const models = c.series.map(p => ({ t: p.t, m: p.dwarf + p.qa }));
    let d = `M${xs(models[0].t)} ${ys(0)}`, prev = 0;
    for (const p of models) { d += ` L${xs(p.t)} ${ys(prev)} L${xs(p.t)} ${ys(p.m)}`; prev = p.m; }
    svg('path', { class: 'cc-line cc-models', d }, root);
    svg('line', { class: 'cc-cap', x1: LABEL, x2: width - 12, y1: ys(c.cap), y2: ys(c.cap) }, root);
    svg('text', { class: 'gt-lane-label', x: 6, y: 16 }, root).textContent = 'busy';
    svg('text', { class: 'gt-lane-label', x: 6, y: 30 }, root).textContent = `peak ${c.peak}`;
    svg('text', { class: 'gt-lane-label cc-cap-label', x: 6, y: 44 }, root).textContent = `limit ${c.cap}`;
    const cursorT = replay !== null ? replay : null;
    if (cursorT !== null) svg('line', { class: 'gt-now', x1: xs(cursorT), x2: xs(cursorT), y1: 2, y2: H - 2 }, root);
    container.append(root);
  }
  return { render, strip };
})();
