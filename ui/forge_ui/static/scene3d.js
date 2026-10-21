/* forge-ui stage: forge runs drawn with iso.js.
   - atlas: every run on one plate, one district per repo, one block per run with a lamp per task;
   - plan: one run's waves as districts, a block per task, and the finish line (integration, verification,
     final review); opening a task shows its attempts, each with its dwarf, its review and any shadow reviews;
   - exec: the same run on wall-clock time, one block per attempt in the lane it really ran in;
   - cloud: every run floating in 3D, grouped by project (cloud3d.js), on the same engine and WebGL context.
   The scene logic (blocks, roofs, lamps, opening, cables, beads) is archivision's assets/figure.js (MIT,
   © 2026 Giamme; drawing style iso-glow, MIT © 2026 April Zhu and Tolga Cohce: see the notice in iso.js).
   All text from the API is drawn on canvases or set with textContent, never parsed as HTML. */
import { createEngine } from './iso.js';
import { createCloud } from './cloud3d.js';
import { createBeacons } from './findings3d.js';
import { createControls } from './stagectl.js';

const RM = window.ForgeRunModel;
const list = v => Array.isArray(v) ? v : [];
const num = v => typeof v === 'number' && Number.isFinite(v) ? v : null;
const clamp = v => Math.max(0, Math.min(1, v));
const segT = (v, a, b) => clamp((v - a) / (b - a));
const ez = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const back = t => t <= 0 ? 0 : t >= 1 ? 1 : 1 + 2.2 * Math.pow(t - 1, 3) + 1.2 * Math.pow(t - 1, 2);
const MODEL_PALETTE = ['#4e9af1', '#f08c3a', '#6cc070', '#c46fd8', '#e0c341', '#4fc2c0', '#e06a8c', '#9a8cff', '#a3b84a', '#d98f6b'];
const KIND_H = { dwarf: 24, qa: 13, shadow: 12 };

export async function createStage(host, cb = {}) {
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const E = await createEngine(host, { reduceMotion: reduce });
  const { THREE, W, TS } = E;
  const TMP = new THREE.Color(), TMP2 = new THREE.Color();
  const tip = document.createElement('div');
  tip.className = 'stage-tip'; tip.hidden = true; tip.setAttribute('role', 'tooltip'); host.append(tip);

  const st = { view: null, mode: 'plan', run: null, runs: [], filter: {}, atlasOpts: {}, sel: null, hover: null, replay: null,
    overlay: { height: 'none', color: 'status', critical: false, arcs: true, ghosts: true },
    // what the stage draws, each switchable (the toolbar's Layers): see applyLayers
    // route: a chosen task's lineage stays lit, what it waits on and what waits on it (I)
    layers: { cables: true, beads: true, lamps: true, roof: true, labels: true, beacons: true, finish: true, districts: true, halos: true, route: true },
    findings: null,
    structure: '', execSig: '', atlasSig: '', rebuilds: 0, fade: null, plan: null, exec: null, atlas: null, crit: null, now: Date.now() / 1000 };
  let world = null;                                         // the group of the current view
  let EL = new Map(), PICK = [], FLOWS = [], PROG = new Map(), DISTRICTS = [], EXEC_BY = null;
  const nowSec = () => st.run ? RM.liveNow(st.run, Date.now() / 1000) : Date.now() / 1000;

  /* ---------- elements: a block, a part or a lane item, each with its own materials ---------- */
  function newEl(id, kind, g, n, data) {
    const el = { id, kind, g, n, data, status: 'PENDING', col: E.statusColor('pending'), root: new THREE.Group(), cover: new THREE.Group(),
      M: E.mats(), cur: { vis: 1, sel: 0, hov: 0, lit: 0, op: 1, opB: 1, tint: 0, halo: 0, hk: 1, route: 0 }, tgt: { vis: 1, sel: 0, hov: 0, lit: 0, op: 1, opB: 1, tint: 0, halo: 0, hk: 1, route: 0 },
      texts: [], sig: '', pulse: false, offset: new THREE.Vector3(), fadeK: 1 };
    el.root.add(el.cover); g.add(el.root); EL.set(id, el);
    return el;
  }
  function body(el, { r = 9, z = 0, ud = {} } = {}) {
    const { x, y, w, d, h } = el.n;
    const m = E.solid(x, y, z, w, d, h, r, el.M, el.cover, { el: el.id, cover: true, ...ud }); PICK.push(m); el.body = m;
    el.tintMat = new THREE.MeshBasicMaterial({ color: el.col.clone(), toneMapped: false, transparent: true, opacity: 0, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -4 });
    E.roofPlate(x + 1, y + 1, z + h + .08, w - 2, d - 2, r - 1, el.tintMat, el.cover);
    el.haloMat = E.softMat(0xffffff, true);
    const hm = E.noRay(new THREE.Mesh(new THREE.PlaneGeometry((w + 80) * TS, (d + 80) * TS), el.haloMat));
    hm.rotation.x = -Math.PI / 2; hm.position.copy(W(x + w / 2, y + d / 2, z + .5)); el.root.add(hm); el.haloMesh = hm;
    return m;
  }
  // a row of status lamps on a roof: one geometry, a colour per lamp
  function leds(el, n, x0, y0, z, { step = 10, s = 3, perRow = 20 } = {}) {
    if (!n) return;
    const pos = [], col = [];
    for (let i = 0; i < n; i++) {
      const lx = x0 + (i % perRow) * step, ly = y0 - Math.floor(i / perRow) * step;
      for (const [a, b] of [[-s, -s], [-s, s], [s, s], [-s, -s], [s, s], [s, -s]]) { pos.push(...W(lx + a, ly + b, z).toArray()); col.push(.5, .5, .5); }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    geo.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
    const mat = new THREE.MeshBasicMaterial({ vertexColors: true, toneMapped: false, transparent: true, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -12 });
    const mesh = E.noRay(new THREE.Mesh(geo, mat)); el.cover.add(mesh); el.M.labs.push(mat);
    el.leds = { geo, n, mesh };
  }
  function setLeds(el, statuses) {
    if (!el.leds) return;
    const a = el.leds.geo.attributes.color;
    for (let i = 0; i < el.leds.n; i++) {
      const s = statuses[i];
      const c = !s ? E.DETAIL : s.isColor ? s : E.statusColor(s);   // a state, or a colour of its own (a severity)
      for (let k = 0; k < 6; k++) a.setXYZ(i * 6 + k, c.r, c.g, c.b);
    }
    a.needsUpdate = true;
  }
  function setStatus(el, status, col) {
    el.status = status || 'PENDING';
    el.col = col || E.statusColor(el.status);
    el.pulse = el.status === 'RUNNING';
  }
  const PLAIN = new Set(['PENDING', 'NONE', 'UNKNOWN']);
  function baseTargets(el) {
    const T = el.tgt, s = el.status, light = E.theme === 'light';
    T.lit = PLAIN.has(s) ? 0 : .32;
    T.tint = PLAIN.has(s) ? 0 : light ? .34 : .1;          // a pale panel shows a lit section by its colour: no glow to carry it
    T.vis = 1; T.op = 1; T.opB = 1;
    if (s === 'PENDING' || s === 'NONE') { T.opB = .55; T.op = .8; }
    // parts are small: their status has to read from the colour of the whole piece
    if (el.kind === 'part' && !PLAIN.has(s)) { T.tint = light ? .5 : .32; T.lit = .55; }
  }
  // a label plane on the roof or the front, redrawn only when what it shows changes
  function label(el, w, h, P, draw, where, at) {
    const t = E.tplane(w, h, P, (x, s, C) => draw(x, s, C, el), where === 'root' ? el.root : el.cover, el.M);
    if (at.front) E.frontAt(t, at.x, at.y, at.z); else E.roofAt(t, at.x, at.y, at.z);
    el.texts.push(t); return t;
  }
  const redraw = el => { for (const t of el.texts) if (!t.gone) t.redraw(); };
  function chip(x, s, C, X, Y, text, colHex, { fill = true, f = 6.5 } = {}) {
    E.font(x, f, s, 600);
    const w = text.length * f * E.CW + 6;
    E.rr(x, s, X, Y, w, f * 1.6, f * .5);
    if (fill) { x.fillStyle = colHex; x.globalAlpha = C.light ? .16 : .22; x.fill(); x.globalAlpha = 1; }
    x.strokeStyle = colHex; x.lineWidth = Math.max(1, .35 * s); x.stroke();
    x.fillStyle = colHex; x.textBaseline = 'middle'; x.fillText(text, (X + 3) * s, (Y + f * .82) * s); x.textBaseline = 'alphabetic';
    return w;
  }
  function display(el, text, { w, x, y, z }) {
    return el.disp = label(el, w, 16, 4, (cx, s, C) => {
      cx.fillStyle = C.glassOff; cx.strokeStyle = el.cur.sel > .5 ? C.acc : C.detail; cx.lineWidth = Math.max(1, .5 * s);
      E.rr(cx, s, .5, .5, w - 1, 15, 3); cx.fill(); cx.stroke();
      cx.fillStyle = E.statusHex(el.status); cx.beginPath(); cx.arc(7 * s, 8 * s, 2.6 * s, 0, 7); cx.fill();
      const f = E.fit(text(el), w - 18, 12, 8.5, 1, 4.5);
      if (f) { E.font(cx, f.f, s); cx.fillStyle = C.ink; cx.textBaseline = 'middle'; cx.fillText(f.L[0], 13 * s, 8.3 * s, (w - 18) * s); cx.textBaseline = 'alphabetic'; }
    }, 'root', { front: true, x, y, z });
  }
  function halo(el) { el.haloMat.uniforms.color.value.copy(el.col); }

  /* ---------- the plate and its districts ---------- */
  function plateFor(g, px, py, districts, names) {
    const plateG = new THREE.Group(); g.add(plateG);
    const p = E.rbox(-34, -34, -16, px + 68, py + 68, 16, 26, E.bodyMat, plateG); p.userData = { plate: true }; PICK.push(p);
    E.loopAt(-22, -22, px + 44, py + 44, 18, .06, E.lineMat, plateG, 12);
    for (const dd of districts) {
      E.inset(dd.x, dd.y, 0, dd.w, dd.d, 16, plateG);
      const name = String(names(dd) || '').toUpperCase(); if (!name) continue;
      const f = Math.max(9, Math.min(26, (dd.w - 60) / (name.length * (E.CW + .14))));
      const tw = Math.min(dd.w - 30, name.length * f * (E.CW + .14) + 6);
      const t = E.tplane(tw, f * 1.4, 4, (x, s, C) => {
        E.font(x, f, s); x.letterSpacing = `${(f * .14 * s).toFixed(1)}px`; x.fillStyle = dd.warn ? E.statusHex('fail') : C.ink;
        x.fillText(name, 0, f * 1.05 * s, tw * s); x.letterSpacing = '0px';
      }, plateG);
      E.roofAt(t, dd.x + 18, dd.y + 12, 0); DISTRICTS.push(t);
    }
    return plateG;
  }

  /* ---------- cables between blocks ---------- */
  const flowMatFaint = () => E.lmat({ polygonOffsetFactor: -3, polygonOffsetUnits: -8 });
  function cable(g, a, b, { kind = 'dep', planes = null } = {}) {
    const A = [a.x + a.w / 2, a.y + a.d / 2, a.h + 2], B = [b.x + b.w / 2, b.y + b.d / 2, b.h + 2];
    const [ea, eb] = E.ends(a, b, a.h + 2, b.h + 2);
    const pts = E.arc(Math.hypot(A[0] - B[0], A[1] - B[1]) < 400 ? ea : A, Math.hypot(A[0] - B[0], A[1] - B[1]) < 400 ? eb : B, 14, .18, 120);
    const mat = flowMatFaint(), line = E.polyLine(pts, mat, g);
    const am = new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false, transparent: true });
    const cn = E.cone(pts, 5, am, g);
    const f = { kind, line, mat, cone: cn, am, pts, cum: E.cumLen(pts), cur: 0, hot: 0, crit: 0, vis: 1, planes, from: null, to: null };
    FLOWS.push(f); return f;
  }

  /* ---------- atlas: every run ---------- */
  function runInfo(r) {
    const c = r.counts || {}, total = r.task_count || Object.values(c).reduce((a, n) => a + n, 0);
    const merged = (c.MERGED || 0) + (c.PASS || 0), running = c.RUNNING || 0;
    const bad = Object.entries(c).filter(([s]) => ['FAIL', 'ERROR', 'CONFLICT', 'INFRA', 'TIMEOUT', 'INVALIDATED', 'INTERRUPTED', 'BLOCKED'].includes(s)).reduce((a, [, n]) => a + n, 0);
    const parts = [r.kind === 'solo' ? (Object.keys(c)[0] || 'solo') : `${merged}/${total} done`];
    if (running) parts.push(`${running} running`);
    if (bad) parts.push(`${bad} failed`);
    if (r.final_review) parts.push(`review ${r.final_review}`);
    else if (r.verification_status) parts.push(`verify ${r.verification_status}`);
    return parts.join(' · ');
  }
  const STATE_STATUS = { live: 'RUNNING', failed: 'FAIL', done: 'MERGED', pending: 'PENDING' };
  function buildAtlas() {
    const A = RM.atlasModel(st.runs, st.filter, { now: nowSec(), ...st.atlasOpts });
    st.atlas = A;
    const g = new THREE.Group(); world.add(g);
    const names = new Map(A.layers.map(l => [l.id, l.name]));
    plateFor(g, A.px, A.py, A.districts, d => names.get(d.layer));
    for (const m of A.modules) {
      const p = A.place[m.id]; if (!p) continue;
      const [x, y, w, d, h] = p;
      const el = newEl('r:' + m.id, 'run', g, { x, y, w, d, h }, { run: m.run, module: m });
      setStatus(el, STATE_STATUS[m.state]);
      body(el);
      label(el, w - 24, 48, 4, (cx, s, C, e) => {
        const r = e.data.run, sel = e.cur.sel > .5;
        const nm = E.fit(String(r.goal || 'Untitled run').split('\n')[0], w - 30, 32, 17, 2, 6);
        if (nm) E.lines(cx, s, nm, 2, 2, sel ? C.accHi : C.inkHi, w - 30, 500);
        E.font(cx, 7, s); cx.fillStyle = C.ink;
        cx.fillText(E.ellipsis(`${RM.basename(r.repo)} · ${r.run_id || ''}${r.kind === 'solo' ? ' · solo' : ''}`, Math.floor((w - 30) / (7 * E.CW))), 2 * s, 44 * s);
      }, 'cover', { x: x + 12, y: y + 8, z: h });
      leds(el, Math.min(60, m.tasks.length), x + 17, y + d - 14, h + .16, { step: 8, s: 2.4, perRow: Math.max(4, Math.floor((w - 30) / 8)) });
      setLeds(el, m.tasks.map(t => t.status));
      display(el, e => runInfo(e.data.run), { w: w - 24, x: x + 12, y: y + d, z: h - 8 });
      halo(el);
    }
    st.atlasBox = [-34, -34, -16, A.px + 34, A.py + 34, 80];
    E.frameBox(st.atlasBox, st.atlasFramed ? 700 : 1400, 30);
    st.atlasFramed = true;
  }
  function updateAtlas() {
    const A = RM.atlasModel(st.runs, st.filter, { now: nowSec(), ...st.atlasOpts });
    st.atlas = A;
    for (const m of A.modules) {
      const el = EL.get('r:' + m.id); if (!el) continue;
      el.data.run = m.run; el.data.module = m;
      setStatus(el, STATE_STATUS[m.state]); halo(el);
      setLeds(el, m.tasks.map(t => t.status));
      const sig = JSON.stringify([m.run.goal, m.run.counts, m.run.final_review, m.run.verification_status]);
      if (sig !== el.sig) { el.sig = sig; redraw(el); }
    }
    retarget();
  }

  /* ---------- plan: waves, tasks, the finish line ---------- */
  function taskStatus(t) {
    if (st.replay !== null && st.replayState) { const s = st.replayState.tasks[t.id]; return s ? s.status : 'PENDING'; }
    return t.status || 'PENDING';
  }
  function taskLine(el) {
    const t = el.data.task, s = el.status;
    const bits = [s];
    if (num(t.attempt) && t.attempt > 1) bits.push('attempt ' + t.attempt);
    if (s === 'RUNNING') {
      const live = list(t.executions).filter(e => e.state === 'running' && (e.role === 'dwarf' || e.role === 'qa')).pop();
      if (live) bits.push(`${live.role === 'qa' ? 'QA' : 'dwarf'} · ${live.phase || ''} ${RM.fmtDur(nowSec() - (num(live.started) || nowSec()))}`.replace(/\s+/g, ' '));
    } else {
      const tt = RM.taskTimes(t, null, nowSec());
      if (tt.busy) bits.push(RM.fmtDur(tt.busy));
    }
    return bits.join(' · ');
  }
  function buildTask(m, p, g) {
    const [x, y, w, d, h] = p, t = m.task;
    const el = newEl('t:' + t.id, 'task', g, { x, y, w, d, h }, { task: t });
    setStatus(el, taskStatus(t));
    body(el);
    el.roof = label(el, w - 24, d - 26, 4, (cx, s, C, e) => {
      const tk = e.data.task, sel = e.cur.sel > .5;
      E.font(cx, 21, s, 600); cx.fillStyle = sel ? C.accHi : C.inkHi; cx.fillText(E.ellipsis(tk.id, Math.floor((w - 30) / (21 * E.CW))), 2 * s, 19 * s);
      const tl = E.fit(tk.title || '', w - 30, 24, 10, 2, 5);
      if (tl) E.lines(cx, s, tl, 2, 25, C.ink, w - 30, 400);
      E.font(cx, 9, s, 500); cx.fillStyle = C.ink;
      cx.fillText(E.ellipsis(`${tk.dwarf || '—'}${tk.explored ? ' *' : ''} → ${tk.qa || '—'}`, Math.floor((w - 30) / (9 * E.CW))), 2 * s, 57 * s);
      let bx = 2;
      const f = tk.findings;
      if (f && (f.confirmed || f.plausible)) {
        for (const sev of ['P0', 'P1', 'P2', 'P3']) if (f[sev]) bx += chip(cx, s, C, bx, 64, `${sev}×${f[sev]}`, E.sevHex(sev)) + 3;
        if (f.unlabelled) bx += chip(cx, s, C, bx, 64, `?×${f.unlabelled}`, E.statusHex('unknown')) + 3;
      }
      if (tk.gate && tk.gate.decision === 'accept') bx += chip(cx, s, C, bx, 64, 'gate ok', E.statusHex('pass')) + 3;
      if (st.overlay.arcs && list(tk.shadow).some(x => x.agreement === 'disagree')) bx += chip(cx, s, C, bx, 64, 'shadow ≠', E.statusHex('conflict')) + 3;
      if (tk.accepted_by) chip(cx, s, C, bx, 64, 'accepted', E.statusHex('nochanges'));
    }, 'cover', { x: x + 12, y: y + 8, z: h });
    const tries = Math.max(1, list(t.attempts).length);
    leds(el, Math.min(20, tries), x + 17, y + d - 13, h + .16, { step: 9, s: 2.8 });
    el.ledCount = tries;
    display(el, taskLine, { w: w - 24, x: x + 12, y: y + d, z: h - 8 });
    // the Clef ring: how likely the first try was to pass, in the colour of what happened
    const p0 = t.clef && t.clef.first_pass && num(t.clef.first_pass.p);
    if (p0 !== null) {
      const cx0 = x + w - 22, cy0 = y + 22, rIn = 8, rOut = 11.5;
      const bgM = new THREE.MeshBasicMaterial({ color: E.DETAIL.clone(), toneMapped: false, transparent: true, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -12 });
      const fgM = new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false, transparent: true, polygonOffset: true, polygonOffsetFactor: -5, polygonOffsetUnits: -14 });
      for (const [m, len] of [[bgM, Math.PI * 2], [fgM, Math.max(.05, Math.min(1, p0)) * Math.PI * 2]]) {
        const ring = E.noRay(new THREE.Mesh(new THREE.RingGeometry(rIn * TS, rOut * TS, 40, 1, Math.PI / 2, len), m));
        ring.rotation.x = -Math.PI / 2; ring.position.copy(W(cx0, cy0, h + .2)); el.cover.add(ring); el.M.labs.push(m);
      }
      el.clef = { fgM, p: p0 };
    }
    halo(el);
    return el;
  }
  function finishInfo(id) {
    const F = st.plan && st.plan.finish, run = st.run; if (!F) return {};
    if (id === 'f:integration') return { title: 'INTEGRATION', sub: `${F.integration.merged}/${F.integration.total} merged`, status: F.integration.status, line: F.integration.accepted ? 'at ' + String(F.integration.accepted).slice(0, 10) : 'into forge/' + (run.run_id || '…') + '-integration' };
    if (id === 'f:verification') return { title: 'VERIFICATION', sub: F.verification.command || 'no command recorded', status: F.verification.status, line: F.verification.runs ? `${F.verification.runs} run${F.verification.runs > 1 ? 's' : ''}` : 'not run yet' };
    const f = F.final.findings;
    return { title: 'FINAL REVIEW', sub: F.final.requested ? (F.final.spec || 'reviewer not chosen') : 'not requested', status: F.final.status, findings: f,
      line: F.final.status === 'NONE' ? 'opt-in: forge review <plan>' : F.final.count > 1 ? `${F.final.count} reviews` : F.final.status };
  }
  function buildFinish(m, p, g) {
    const [x, y, w, d, h] = p;
    const el = newEl(m.id, 'finish', g, { x, y, w, d, h }, { name: m.name });
    const info = finishInfo(m.id);
    setStatus(el, finishStatus(m.id, info));
    body(el, { r: 7 });
    el.roof = label(el, w - 24, d - 28, 4, (cx, s, C, e) => {
      const fi = finishInfo(e.id), sel = e.cur.sel > .5;
      if (fi.status === 'NONE') { cx.setLineDash([2.4 * s, 1.8 * s]); cx.strokeStyle = C.line; cx.lineWidth = Math.max(1, .3 * s); E.rr(cx, s, .5, .5, w - 25, d - 29, 5); cx.stroke(); cx.setLineDash([]); }
      E.font(cx, 15, s, 600); cx.fillStyle = sel ? C.accHi : C.inkHi; cx.fillText(fi.title || '', 4 * s, 17 * s);
      const sub = E.fit(fi.sub || '', w - 34, 22, 9.5, 2, 5); if (sub) E.lines(cx, s, sub, 4, 23, C.ink, w - 34, 400);
      if (fi.findings) { let bx = 4; for (const sev of ['P0', 'P1', 'P2', 'P3']) if (fi.findings[sev]) bx += chip(cx, s, C, bx, 46, `${sev}×${fi.findings[sev]}`, E.sevHex(sev)) + 3;
        if (fi.findings.plausible) chip(cx, s, C, bx, 46, `~${fi.findings.plausible}`, E.statusHex('pending'), { fill: false }); }
    }, 'cover', { x: x + 12, y: y + 8, z: h });
    if (m.id === 'f:final') { leds(el, 4, x + 18, y + d - 13, h + .16, { step: 11, s: 3 }); }
    else if (m.id === 'f:integration') leds(el, Math.min(40, list(st.run.tasks).length), x + 17, y + d - 13, h + .16, { step: 8, s: 2.4, perRow: 22 });
    else leds(el, Math.max(1, Math.min(12, list(st.run.verification && st.run.verification.runs).length)), x + 17, y + d - 13, h + .16, { step: 9, s: 2.8 });
    finishLeds(el);
    display(el, e => finishInfo(e.id).line || '', { w: w - 24, x: x + 12, y: y + d, z: h - 8 });
    halo(el);
    return el;
  }
  function finishStatus(id, info) {
    if (st.replay !== null && st.replayState) return id === 'f:final' ? (st.run.final_review || st.run.settings && st.run.settings.final_qa ? st.replayState.final : 'NONE') : id === 'f:verification' ? st.replayState.verification : st.replayState.integration;
    return info.status || 'PENDING';
  }
  function finishLeds(el) {
    if (el.id === 'f:final') { const f = finishInfo(el.id).findings || {}; setLeds(el, ['P0', 'P1', 'P2', 'P3'].map(s => f[s] ? E.sevColor(s) : null)); }
    else if (el.id === 'f:integration') setLeds(el, list(st.run.tasks).map(t => taskStatus(t) === 'MERGED' ? 'MERGED' : null));
    else setLeds(el, list(st.run.verification && st.run.verification.runs).map(r => r.exit === 0 ? 'PASS' : 'FAIL'));
  }
  function buildPlan(g) {
    const P = RM.planModel(st.run); st.plan = P;
    const names = new Map(P.layers.map(l => [l.id, l.name]));
    P.plateG = plateFor(g, P.px, P.py, P.districts, d => names.get(d.layer));
    for (const m of P.modules) {
      const p = P.place[m.id]; if (!p) continue;
      if (m.kind === 'task') buildTask(m, p, g); else buildFinish(m, p, g);
    }
    for (const f of P.flows) {
      const a = EL.get(f.from), b = EL.get(f.to); if (!a || !b) continue;
      const c = cable(g, a.n, b.n, { kind: f.kind }); c.from = f.from; c.to = f.to; c.g = 'plan';
    }
    for (const el of EL.values()) if (el.g === g && el.kind === 'task') PROG.set(el.id, { p: 0, t: 0 });
  }

  /* ---------- inside a task: attempts, dwarves, reviews ---------- */
  function buildInner(el) {
    const task = el.data.task, tp = RM.taskParts(task), { x, y, w, d } = el.n;
    const G = el.inner = new THREE.Group(); G.visible = false; el.root.add(G);
    el.innerKey = RM.innerKey(task); el.kids = [];
    el.floorMat = E.recessMat.clone();
    { const sh = new THREE.Shape(); E.roundRect(sh, x, -(y + d), w, d, 9);
      const fg = new THREE.ShapeGeometry(sh, 8); fg.rotateX(-Math.PI / 2); fg.translate(0, .2, 0); fg.scale(TS, TS, TS);
      const floor = new THREE.Mesh(fg, el.floorMat); floor.userData = { el: el.id, floor: true }; G.add(floor); PICK.push(floor); }
    el.innerLine = E.lmat(); E.loopAt(x, y, w, d, 9, .26, el.innerLine, G);
    const C = Math.max(1, tp.zones.length), R = Math.max(1, ...tp.zones.map(z => z.parts.length));
    const padX = 10, padT = 22, padB = 10, gx = 8, gy = 7;
    const cw = (w - 2 * padX - (C - 1) * gx) / C, ch = Math.min(46, (d - padT - padB - (R - 1) * gy) / R);
    el.zoneEls = [];
    tp.zones.forEach((z, ci) => {
      const zx = x + padX + ci * (cw + gx), zy = y + padT;
      const zm = E.lmat({ dashed: true, dashSize: 4 * TS, gapSize: 3 * TS });
      E.loopAt(zx - 3, zy - 13, cw + 6, R * ch + (R - 1) * gy + 16, 5, .32, zm, G, 6).computeLineDistances();
      const nm = z.name, f = Math.min(6.5, cw / (nm.length * (E.CW + .12) + 2));
      const t = E.tplane(Math.min(cw, nm.length * f * (E.CW + .12) + 4), f * 1.5, E.pxIn(w, d), (cx, s, Cc) => {
        E.font(cx, f, s); cx.letterSpacing = `${(f * .12 * s).toFixed(2)}px`; cx.fillStyle = Cc.ink; cx.fillText(nm, 0, f * 1.1 * s); cx.letterSpacing = '0px';
      }, G);
      E.roofAt(t, zx, zy - 11, .3);
      el.zoneEls.push({ mat: zm, lab: t });
      z.parts.forEach((pid, ri) => {
        const part = tp.parts.find(p => p.id === pid);
        const ph = KIND_H[part.kind] || 14;
        const n = { x: zx, y: zy + ri * (ch + gy), w: cw, d: ch, h: ph };
        const k = newEl('p:' + task.id + ':' + part.id, 'part', G, n, { part, task: task.id, parent: el.id, idx: ri + ci * R });
        el.root.remove(k.root); G.add(k.root);
        setStatus(k, part.planned ? 'PENDING' : part.status);
        const r = part.kind === 'qa' ? Math.min(cw, ch) * .3 : Math.min(4, Math.min(cw, ch) * .07);
        body(k, { r });
        k.roof = label(k, cw, ch, E.pxIn(w, d), (cx, s, Cc, e) => {
          const pt = e.data.part, sel = e.cur.sel > .5, pad = Math.max(2, Math.min(cw, ch) * .08);
          if (pt.kind === 'shadow' || pt.planned) { cx.setLineDash([2 * s, 1.5 * s]); cx.strokeStyle = sel ? Cc.acc : Cc.line; cx.lineWidth = Math.max(1, .3 * s); E.rr(cx, s, 1.2, 1.2, cw - 2.4, ch - 2.4, 2); cx.stroke(); cx.setLineDash([]); }
          const f1 = Math.min(7, ch * .2, cw / (pt.label.length * E.CW + 3));
          E.font(cx, f1, s, 600); cx.fillStyle = sel ? Cc.accHi : Cc.inkHi; cx.fillText(pt.label, pad * s, (pad + f1) * s);
          const sp = E.fit(pt.spec || '—', cw - 2 * pad, ch - f1 * 1.6 - pad * 2, 6, 2, 2.5);
          if (sp) E.lines(cx, s, sp, pad, pad + f1 * 1.35, Cc.ink, cw - 2 * pad, 400);
          const fs = pt.exec && pt.exec.findings || pt.shadow && pt.shadow.findings;
          if (fs && fs.worst) chip(cx, s, Cc, pad, ch - pad - 6 * 1.6, `${fs.worst}×${fs[fs.worst]}`, E.sevHex(fs.worst), { f: Math.min(5, ch * .14) });
        }, 'cover', { x: zx, y: n.y, z: ph });
        el.kids.push(k);
      });
    });
    el.innerFlows = [];
    for (const l of tp.links) {
      const a = EL.get('p:' + task.id + ':' + l.from), b = EL.get('p:' + task.id + ':' + l.to); if (!a || !b) continue;
      const [A, B] = E.ends(a.n, b.n, a.n.h, b.n.h);
      const pts = E.curve(A, B, l.kind === 'flow' ? 3 : 6, .14);
      const mat = l.kind === 'escalation' ? E.lmat({ polygonOffsetFactor: -3, polygonOffsetUnits: -8 }) : E.lmat({ polygonOffsetFactor: -3, polygonOffsetUnits: -8, ...(l.kind === 'retry' || l.kind === 'shadow' ? { dashed: true, dashSize: 2.5 * TS, gapSize: 2 * TS } : {}) });
      const line = E.polyLine(pts, mat, G); if (l.kind === 'retry' || l.kind === 'shadow') line.computeLineDistances();
      const am = new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false, transparent: true });
      const cn = E.cone(pts, 1.6, am, G);
      const col = l.kind === 'escalation' ? E.accent : l.kind === 'shadow' ? E.statusColor(l.agree === 'disagree' ? 'conflict' : l.agree === 'agree' ? 'pass' : 'unknown') : null;
      const f = { kind: l.kind, line, mat, cone: cn, am, pts, cum: E.cumLen(pts), col, a: a.id, b: b.id };
      if (l.label) {
        const nm = l.label, fz = 4.2, tw = nm.length * fz * E.CW + 4;
        const mid = pts[Math.floor(pts.length / 2)];
        f.lab = E.tplane(tw, fz * 1.6, E.pxIn(w, d), (cx, s, Cc) => { E.font(cx, fz, s, 600); cx.fillStyle = Cc.accHi; cx.fillText(nm, 2 * s, fz * 1.15 * s); }, G);
        E.roofAt(f.lab, mid[0] - tw / 2, mid[1] - fz, mid[2] + 1);
      }
      el.innerFlows.push(f);
    }
    syncFractal(el);
    applyLayers();
  }
  /* ---------- Fractal: an opened task's child tasks, a small tree floating over it ---------- */
  const FRACTAL = new Map();                                // task id → TaskFractal
  const FR_H = { low: 12, medium: 18, high: 26 };
  function syncFractal(el) {
    if (!el || !el.inner || el.kind !== 'task') return;
    const tid = el.data.task.id, data = FRACTAL.get(tid), nodes = data && data.available ? list(data.nodes) : [];
    const sig = JSON.stringify([E.theme, nodes.map(n => [n.id, n.parent, n.ui_status, n.difficulty, n.model, n.reserved ? 1 : 0])]);
    if (sig === el.fractalSig) return;
    el.fractalSig = sig;
    if (el.fractalG) { PICK = PICK.filter(o => !(o.userData.fractal && o.userData.fractal.task === tid)); E.disposeGroup(el.fractalG); el.fractalG = null; }
    if (!nodes.length) return;
    const T = RM.fractalTree(nodes), byId = new Map(nodes.map(n => [n.id, n])), { x, y, w, d, h } = el.n;
    const S = 34, DX = 62, DY = 44, tw = S + (T.cols - 1) * DX, td = S + Math.max(0, T.rows - 1) * DY;
    const x0 = x + w / 2 - tw / 2, y0 = y + d / 2 - td / 2, z0 = h + 70;
    const G = el.fractalG = new THREE.Group(); el.inner.add(G);
    const at = id => { const p = T.pos[id], n = byId.get(id); return { x: x0 + p.col * DX, y: y0 + p.row * DY, w: S, d: S, h: FR_H[String(n.difficulty || '').toLowerCase()] || 16 }; };
    const cable = E.lmat({ polygonOffsetFactor: -3, polygonOffsetUnits: -8 }); cable.color.copy(E.majorMat.color).lerp(E.accent, .45);
    for (const [pa, ch] of T.edges) { const A = at(pa), B = at(ch), [P, Q] = E.ends(A, B, z0 + A.h / 2, z0 + B.h / 2); E.polyLine(E.curve(P, Q, 3, .1), cable, G); }
    // a stem from the task's floor to each root
    const stem = E.lmat({ dashed: true, dashSize: 4 * TS, gapSize: 3 * TS }); stem.color.copy(cable.color);
    for (const n of nodes) if (T.pos[n.id] && T.pos[n.id].depth === 0) { const A = at(n.id); E.polyLine([[x + w / 2, y + d / 2, 1], [A.x + S / 2, A.y + S / 2, z0]], stem, G).computeLineDistances(); }
    for (const n of nodes) {
      const b = at(n.id), M = E.mats(), col = E.statusColor(n.ui_status || 'unknown');
      const m = E.solid(b.x, b.y, z0, b.w, b.d, b.h, 6, M, G, { fractal: { task: tid, node: n.id } });
      E.paint(M, n.reserved ? .6 : 1, .6, 0, n.reserved ? .55 : 1, n.reserved ? .45 : 1, col, .3);
      PICK.push(m);
      const lab = E.tplane(S - 4, 13, E.pxIn(w, d), (cx, sc, C) => {
        E.font(cx, 4.6, sc, 600); cx.fillStyle = C.inkHi; cx.fillText(E.ellipsis(n.id, 12), 1 * sc, 5.4 * sc);
        E.font(cx, 3.6, sc); cx.fillStyle = C.ink; cx.fillText(E.ellipsis(n.reserved ? 'reserved' : n.model || n.status || '', 14), 1 * sc, 11 * sc);
      }, G, M);
      E.roofAt(lab, b.x + 2, b.y + 2, z0 + b.h);
    }
  }
  E.themeHooks.add(() => { for (const el of EL.values()) if (el.fractalG) { el.fractalSig = ''; syncFractal(el); } });
  function closeInner(el) {
    if (!el.inner) return;
    if (el.kind === 'task') { const tid = el.data.task.id; PICK = PICK.filter(o => !(o.userData.fractal && o.userData.fractal.task === tid)); el.fractalG = null; el.fractalSig = ''; }
    for (const k of el.kids || []) { EL.delete(k.id); PICK = PICK.filter(o => o.userData.el !== k.id); }
    PICK = PICK.filter(o => !(o.userData.floor && o.userData.el === el.id));
    E.disposeGroup(el.inner); el.inner = null; el.kids = []; el.innerFlows = [];
  }

  /* ---------- exec: attempts on wall-clock time ---------- */
  function execSig(X) { return JSON.stringify([X.items.map(i => [i.id, i.lane, i.ghost ? 1 : 0, i.segs.length]), X.extra.map(e => e.id), X.lanes, X.t0, X.t1]); }
  function buildExec(g) {
    const X = RM.execModel(st.run, { now: nowSec(), ghosts: st.overlay.ghosts }); st.exec = X;
    st.execSig = execSig(X);
    const dists = [];
    for (let i = 0; i < X.lanes; i++) dists.push({ layer: 'lane' + i, x: 6, y: 60 + i * X.laneD + 4, w: X.px - 12, d: X.laneD - 8, warn: i >= X.cap, name: `LANE ${i + 1}${i >= X.cap ? ' · over --max-parallel ' + X.cap : ''}` });
    for (const fl of X.finishLanes || []) dists.push({ layer: fl.kind, x: 6, y: fl.y + 4, w: X.px - 12, d: X.laneD - 8, name: fl.name });
    X.plateG = plateFor(g, X.px, X.py, dists, d => d.name);
    // the time ruler along the back edge
    const tk = [];
    for (const r of X.ruler) tk.push([[r.x, 30, .2], [r.x, 50, .2]]);
    if (tk.length) E.seg(tk, X.plateG, E.majorMat);
    for (const r of X.ruler) {
      const t = E.tplane(40, 10, 4, (cx, s, C) => { E.font(cx, 7, s); cx.fillStyle = C.ink; cx.fillText('+' + r.label, 1 * s, 8 * s); }, X.plateG);
      E.roofAt(t, r.x + 2, 30, .1);
    }
    if (!X.items.length && !X.extra.length) {
      const t = E.tplane(400, 20, 3, (cx, s, C) => { E.font(cx, 12, s); cx.fillStyle = C.ink; cx.fillText('Nothing has run yet: no attempt timings recorded.', 4 * s, 14 * s); }, X.plateG);
      E.roofAt(t, 60, 80, .1);
    }
    for (const it of X.items.concat(X.extra)) buildAttempt(it, g);
    // now: a pane of light across the lanes, moving with the clock (or the replay)
    const nowM = new THREE.MeshBasicMaterial({ color: E.accent.clone(), toneMapped: false, transparent: true, opacity: .16, depthWrite: false, side: THREE.DoubleSide });
    const pane = E.noRay(new THREE.Mesh(new THREE.PlaneGeometry(1, 1), nowM));
    pane.rotation.y = Math.PI / 2; pane.scale.set((X.py - 40) * TS, 70 * TS, 1);
    pane.position.copy(W(0, (X.py + 40) / 2, 35)); g.add(pane);
    const lineM = E.accentLineMat;
    const nl = E.polyLine([[0, 40, .4], [0, X.py, .4]], lineM, g);
    X.now = { pane, nl, mat: nowM };
    placeNow();
  }
  function placeNow() {
    const X = st.exec; if (!X || !X.now) return;
    const t = st.replay !== null ? st.replay : nowSec();
    const show = X.t0 !== null && (st.replay !== null || X.live) && t >= X.t0 - 1 && t <= X.t1 + 1;
    X.now.pane.visible = X.now.nl.visible = show;
    if (!show) return;
    const xx = X.x(t);
    X.now.pane.position.x = xx * TS; X.now.nl.position.x = xx * TS;
  }
  function attemptLine(el) {
    const it = el.data.item;
    if (it.ghost) return `projected · ~${RM.fmtDur(it.end - it.start)}`;
    if (it.kind === 'review') return `${el.status} · ${RM.fmtDur(it.end - it.start)}`;
    const bits = [el.status, RM.fmtDur(it.end - it.start)];
    if (it.running && it.role) bits.splice(1, 0, it.role === 'qa' ? 'QA' : 'dwarf');
    return bits.join(' · ');
  }
  function buildAttempt(it, g) {
    const h = it.ghost ? 26 : it.kind ? 30 : 36;
    const n = { x: it.x, y: it.y, w: it.w, d: it.d, h };
    const id = 'x:' + it.id;
    const el = newEl(id, it.ghost ? 'ghost' : it.kind || 'attempt', g, n, { item: it });
    setStatus(el, it.status);
    body(el, { r: Math.min(7, it.w / 4) });
    const lw = Math.max(10, it.w - 10);
    el.roof = label(el, lw, it.d - 14, 4, (cx, s, C, e) => {
      const item = e.data.item, sel = e.cur.sel > .5;
      const title = item.kind === 'verification' ? 'verification' : item.kind === 'review' ? 'final review' : item.task + (item.attempt ? ' · A' + item.attempt : '');
      const f = Math.min(11, Math.max(4, (lw - 4) / Math.max(4, title.length * E.CW)));
      E.font(cx, f, s, 600); cx.fillStyle = sel ? C.accHi : item.ghost ? C.ink : C.inkHi; cx.fillText(E.ellipsis(title, Math.floor((lw - 2) / (f * E.CW))), 1 * s, (f + 1) * s);
      // segments: when the dwarf and the review ran inside this attempt
      const t0 = item.start, span = Math.max(1, item.end - item.start), by = it.d - 34;
      for (const sg of item.segs) {
        const sx = (sg.start - t0) / span * (lw - 2), sw = Math.max(1.5, ((sg.end ?? item.end) - sg.start) / span * (lw - 2));
        cx.fillStyle = E.statusHex(sg.status); cx.globalAlpha = sg.role === 'qa' ? .95 : .6;
        E.rr(cx, s, 1 + sx, by, Math.min(sw, lw - 2 - sx), 8, 2); cx.fill(); cx.globalAlpha = 1;
        if (sw > 26) { E.font(cx, 5.5, s, 600); cx.fillStyle = C.bg; cx.fillText(sg.role === 'qa' ? 'QA' : 'DWARF', (3 + sx) * s, (by + 6) * s); }
      }
      if (item.findings && item.findings.worst) chip(cx, s, C, 1, by - 12, `${item.findings.worst}×${item.findings[item.findings.worst]}`, E.sevHex(item.findings.worst), { f: 5 });
      if (item.ghost) { cx.setLineDash([2 * s, 1.6 * s]); cx.strokeStyle = C.line; cx.lineWidth = Math.max(1, .3 * s); E.rr(cx, s, .5, .5, lw - 1, it.d - 15, 3); cx.stroke(); cx.setLineDash([]); }
    }, 'cover', { x: it.x + 5, y: it.y + 6, z: h });
    if (it.w > 70) display(el, attemptLine, { w: Math.min(it.w - 10, 150), x: it.x + 5, y: it.y + it.d, z: h - 6 });
    halo(el);
    return el;
  }

  /* ---------- finding beacons: each review finding as a mark over the block it is about ---------- */
  const BEACONS = createBeacons(E, { reduce });
  E.scene.add(BEACONS.group);
  const SEV_RANK = { P0: 0, P1: 1, P2: 2, P3: 3 };
  const byRank = (a, b) => (SEV_RANK[a.f.severity] ?? 4) - (SEV_RANK[b.f.severity] ?? 4) || (a.f.status === 'CONFIRMED' ? 0 : 1) - (b.f.status === 'CONFIRMED' ? 0 : 1);
  const planOff = el => [el.offset.x / TS, el.offset.z / TS];
  // the i-th of n marks, in a grid of up to five a row centred on (cx, cy)
  function grid(i, n, cx, cy, step) {
    const cols = Math.min(n, 5), rows = Math.ceil(n / cols), c = i % cols, r = Math.floor(i / cols);
    return [cx + (c - (cols - 1) / 2) * step, cy + (r - (rows - 1) / 2) * step];
  }
  // the execution view's block holding an execution (an attempt holds its dwarf's and its review's)
  function execEl(id) {
    if (!EXEC_BY) {
      EXEC_BY = new Map();
      for (const el of EL.values()) { const it = el.data && el.data.item; if (!it || it.ghost) continue;
        EXEC_BY.set(it.id, el); for (const g of list(it.segs)) if (g.id) EXEC_BY.set(g.id, el);
        if (it.kind === 'review' && it.review) EXEC_BY.set('final:' + it.review, el); }
    }
    return EXEC_BY.get(id) || null;
  }
  // a mark's foot on a block's roof: [x, y, z, scale]
  function over(el, i, n, step, scale) {
    // lying flat (top-down) the marks keep to the roof's right edge, off its text
    const cx = BEACONS.flat ? el.n.x + el.n.w - Math.min(el.n.w / 2, step * .7) : el.n.x + el.n.w / 2;
    const [ox, oy] = planOff(el), [x, y] = grid(i, n, cx + ox, el.n.y + el.n.d / 2 + oy, step);
    return [x, y, el.n.h, scale];
  }
  // where a task's finding stands: over the roof (the latest attempt's, while the task is shut), on the review
  // that found it (while it is open), on its attempt (execution); null hides it
  function anchorTask(tid, r, ri, rn, j, jn, shown) {
    if (!st.layers.beacons || st.fade || !shown(r)) return null;
    const u = BEACONS.unit * 1.15;                         // marks a little more than their width apart
    if (st.mode === 'exec') { const el = execEl(r.execution); return el && el.cur.vis > .5 ? over(el, j, jn, u * .7, .7) : null; }
    const el = EL.get('t:' + tid); if (!el || el.cur.vis < .5) return null;
    const pr = PROG.get(el.id), p = pr ? pr.p : 0;
    if (p > .5) {
      const k = (el.kids || []).find(k => k.data.part.exec && k.data.part.exec.id === r.execution);
      if (!k || (k.q || 0) < .6) return null;
      const [ox, oy] = planOff(el), [x, y] = grid(j, jn, k.n.x + k.n.w / 2 + ox, k.n.y + k.n.d / 2 + oy, u * .5);
      return [x, y, k.n.h * (k.q || 1), .5];
    }
    if (ri < 0 || p > .15) return null;
    const a = over(el, ri, rn, u, 1); a[2] = el.n.h * el.cur.hk + el.cover.position.y / TS;
    return a;
  }
  function anchorFinal(name, i, n, shown, review) {
    if (!st.layers.beacons || !st.layers.finish || st.fade || !shown(review)) return null;
    const el = st.mode === 'exec' ? execEl('final:' + name) : EL.get('f:final');
    const s = st.mode === 'exec' ? .7 : .9;
    return el && el.cur.vis > .5 ? over(el, i, n, BEACONS.unit * 1.15 * s, s) : null;
  }
  function syncBeacons() {
    const F = st.findings, run = st.run;
    if (st.view !== 'run' || !run || !F || F.run !== run.key) { BEACONS.set([]); return; }
    // in a replay a finding shows once its review has finished
    const ends = new Map();
    for (const t of list(run.tasks)) for (const e of list(t.executions)) ends.set(e.id, num(e.ended));
    for (const fr of list(run.final_reviews)) if (fr.execution) ends.set(fr.execution, num(fr.ended));
    const shown = r => st.replay === null || (r.execution && ends.get(r.execution) != null && ends.get(r.execution) <= st.replay);
    const items = [], solo = run.kind === 'solo';
    for (const tf of list(F.tasks)) {
      const tid = solo ? (list(run.tasks)[0] || {}).id : tf.id; if (!tid) continue;
      const latest = Math.max(...tf.reviews.map(r => r.attempt ?? 0));
      const roof = tf.reviews.filter(r => (r.attempt ?? 0) === latest).flatMap(r => r.findings.map(f => ({ f, r }))).sort(byRank);
      for (const r of tf.reviews) {
        const mine = r.findings.map(f => ({ f, r })).sort(byRank);
        mine.forEach(({ f }, j) => {
          const ri = roof.findIndex(x => x.f.id === f.id && x.r === r);
          items.push({ key: `${run.key}:${tid}:${f.id}`, sev: f.severity, plausible: f.status !== 'CONFIRMED', data: { task: tid, review: r, finding: f },
            anchor: () => anchorTask(tid, r, ri, roof.length, j, mine.length, shown) });
        });
      }
    }
    const fr = list(F.final_reviews)[0];                     // the newest final review's
    if (fr) {
      const review = { kind: 'final', execution: fr.execution, spec: fr.spec, verdict: fr.verdict };
      fr.findings.map(f => ({ f })).sort(byRank).forEach(({ f }, i, arr) => items.push({ key: `${run.key}:final:${fr.name}:${f.id}`, sev: f.severity,
        plausible: f.status !== 'CONFIRMED', data: { final: fr.name, review, finding: f }, anchor: () => anchorFinal(fr.name, i, arr.length, shown, review) }));
    }
    BEACONS.set(items);
  }
  // the layers the toolbar switches; a frame never turns these back on
  function applyLayers() {
    const L = st.layers, finishOff = id => !L.finish && String(id || '').startsWith('f:');
    for (const f of FLOWS) f.line.visible = f.cone.visible = L.cables && !finishOff(f.from) && !finishOff(f.to);
    for (const el of EL.values()) {
      if (el.leds) el.leds.mesh.visible = L.lamps;
      if (el.roof) el.roof.mesh.visible = L.roof && !el.roofTiny;
      if (el.disp) el.disp.mesh.visible = L.labels;
      if (el.haloMesh) el.haloMesh.visible = L.halos;
      if (el.kind === 'finish') el.root.visible = L.finish;
      if (el.kind === 'review' || el.kind === 'verification') el.root.visible = L.finish;
    }
    for (const t of DISTRICTS) t.mesh.visible = L.districts;
    BEACONS.setVisible(L.beacons && st.view === 'run');
    E.invalidate(100);
  }

  /* ---------- name tags: a roof too small to read gets its name floating over it, facing the screen ---------- */
  const CM = window.ForgeCloudModel;
  const TAGS = new Map();                                   // element id → screen sprite
  const ID_FONT = 21, TAG_PX = 20, TINY = 3.5;             // the roof id's size in plan units; px: a tag, a roof too small to draw
  const READ = 7 / Math.sqrt(Math.min(2, window.devicePixelRatio || 1));   // px at which a roof's id still reads (sharper on a dense screen)
  let tagAt = 0, tagKey = '';
  const SV = new THREE.Vector3();
  function screenOf(v) { SV.copy(v).project(E.camera); const [VW, VH] = E.size; return { x: (SV.x + 1) / 2 * VW, y: (1 - SV.y) / 2 * VH, on: SV.z < 1 && Math.abs(SV.x) < 1.02 && Math.abs(SV.y) < 1.02 }; }
  function tagText(el) {
    if (el.kind === 'task') return el.data.task.id;
    if (el.kind === 'finish') return el.data.name || '';
    if (el.kind === 'run') return E.ellipsis(String(el.data.run.goal || el.data.run.run_id || '').split('\n')[0], 34);
    const it = el.data.item; return it ? it.task || it.review || it.kind || '' : '';
  }
  // the elements a tag can name in the view shown: plan blocks, the attempts on the time axis, the map's runs
  function taggable(el) {
    if (st.view === 'atlas') return el.kind === 'run';
    if (el.kind === 'part' || el.kind === 'ghost' || !el.root.parent) return false;
    return st.mode === 'plan' ? el.g === st.planG : el.g === st.execG;
  }
  function roofTop(el) { el.root.updateWorldMatrix(true, false); return el.root.localToWorld(W(el.n.x + el.n.w / 2, el.n.y + el.n.d / 2, el.n.h * (el.cur.hk || 1) + el.cover.position.y / TS)); }
  function tagFor(el) {
    let t = TAGS.get(el.id); if (t) return t;
    const id = el.id, text = tagText(el), w = Math.ceil(text.length * 11 * E.CW) + 26;
    t = E.sprite(w, 22, 3, (x, s, C) => {
      const cur = EL.get(id) || el, col = E.statusHex(cur.status);
      E.rr(x, s, 1, 1, w - 2, 20, 6); x.globalAlpha = .92; x.fillStyle = C.bg; x.fill(); x.globalAlpha = 1;
      x.strokeStyle = sameSel(cur) ? C.acc : C.line; x.lineWidth = Math.max(1, s); x.stroke();
      x.fillStyle = col; x.beginPath(); x.arc(10 * s, 11 * s, 3.2 * s, 0, 7); x.fill();
      E.font(x, 11, s, 600); x.fillStyle = C.inkHi; x.fillText(text, 18 * s, 15 * s);
    }, { screen: true, px: TAG_PX });
    t.mesh.center.set(.5, -.2); t.mat.depthTest = false; t.mesh.renderOrder = 6;
    world.add(t.mesh); TAGS.set(id, t); return t;
  }
  const sameSel = el => st.sel && selIds().has(el.id);
  function updateTags() {
    const on = st.layers.roof && (st.view === 'run' || st.view === 'atlas'), cands = [], sel = selIds();
    E.camera.updateMatrixWorld();                           // the frame step runs before the render that would update it
    for (const el of EL.values()) {
      if (!on || !taggable(el)) continue;
      const top = roofTop(el), p = screenOf(top);
      const b = screenOf(top.clone().add(W(ID_FONT, 0, 0))), px = Math.hypot(b.x - p.x, b.y - p.y);
      const tiny = px < TINY && st.view === 'run';
      if (el.roof && el.roofTiny !== tiny) { el.roofTiny = tiny; el.roof.mesh.visible = st.layers.roof && !tiny; }
      if (!p.on || px >= READ || el.tgt.vis < .5) continue;
      const w = (Math.ceil(tagText(el).length * 11 * E.CW) + 26) * TAG_PX / 22;
      cands.push({ key: el.id, x: p.x, y: p.y - 4, w: w + 8, h: TAG_PX + 6, pri: (sel.has(el.id) ? 1e9 : 0) + (el.id === st.hover ? 1e8 : 0) + (el.status === 'RUNNING' ? 1e6 : 0) + px });
    }
    // a finding's mark is never covered: its head goes in first, as a place no tag may take
    if (st.view === 'run' && st.layers.beacons) {
      const sz = BEACONS.unit * 2.2 * E.pxPerUnit();
      BEACONS.items.forEach((it, i) => { if (!it.head || it.hidden) return; const h = screenOf(W(...it.head)); if (h.on) cands.push({ key: 'beacon:' + i, x: h.x, y: h.y + sz * .45, w: sz * .8, h: sz, pri: 1e12 }); });
    }
    const keep = new Set(CM ? CM.labelPick(cands, { max: (st.view === 'atlas' ? 10 : 40) + cands.filter(c => c.pri === 1e12).length }).filter(k => !k.startsWith('beacon:')) : []);
    for (const [id, t] of TAGS) if (!keep.has(id)) t.mesh.visible = false;
    for (const id of keep) {
      const el = EL.get(id), t = tagFor(el), look = el.status + '|' + sameSel(el);
      if (t.look !== look) { t.look = look; t.redraw(); }
      t.mesh.visible = true; t.mesh.position.copy(roofTop(el));
    }
  }
  // each frame: the tags follow the camera, at most every 120 ms, and once more when it settles
  function tagsTick(now) {
    const c = E.camera, key = [c.position.x, c.position.y, c.position.z, c.zoom || 1, ...E.size].map(v => v.toFixed(4)).join() + st.view + st.mode + st.hover + JSON.stringify(st.sel) + st.layers.roof + st.rebuilds;
    if (key === tagKey) return false;
    if (now - tagAt < 120) return true;
    tagAt = now; tagKey = key; updateTags(); return false;
  }

  /* ---------- building a view ---------- */
  function clearWorld() {
    if (world) E.disposeGroup(world);
    world = new THREE.Group(); E.scene.add(world);
    EL = new Map(); PICK = []; FLOWS = []; PROG = new Map(); DISTRICTS = []; EXEC_BY = null; TAGS.clear(); tagKey = '';
    st.plan = st.exec = st.atlas = null; st.fade = null;
    BEACONS.set([]);
    hideTip();
  }
  function buildRun() {
    clearWorld(); st.rebuilds++;
    const pg = new THREE.Group(), xg = new THREE.Group(); world.add(pg, xg);
    st.planG = pg; st.execG = xg;
    buildPlan(pg); buildExec(xg);
    pg.visible = st.mode === 'plan'; xg.visible = st.mode === 'exec';
    computeCritical();
    syncBeacons(); applyLayers();
    retarget();
  }
  function rebuildExec() {
    for (const [id, el] of [...EL]) if (el.g === st.execG) EL.delete(id);
    PICK = PICK.filter(o => { let p = o; while (p) { if (p === st.execG) return false; p = p.parent; } return true; });
    E.disposeGroup(st.execG); st.execG = new THREE.Group(); world.add(st.execG);
    buildExec(st.execG); st.execG.visible = st.mode === 'exec' || !!st.fade; st.rebuilds++; st.execDirty = false;
    EXEC_BY = null; applyLayers();
  }
  function boxOf(which) {
    const P = which === 'exec' ? st.exec : st.plan;
    if (!P) return [-34, -34, -16, 900, 500, 60];
    return [-34, -34, -16, P.px + 34, P.py + 34, 36];
  }

  /* ---------- selection, opening, statuses → targets ---------- */
  function selIds() {
    const s = st.sel, out = new Set(); if (!s) return out;
    if (st.view === 'atlas') { if (s.kind === 'run') out.add('r:' + s.key); return out; }
    const tid = s.kind === 'task' ? s.id : s.kind === 'execution' || s.kind === 'fractal' ? s.task : null;
    if (tid) {
      out.add('t:' + tid);
      if (s.kind === 'execution') for (const el of EL.values()) if (el.kind === 'part' && el.data.task === tid && el.data.part.exec && el.data.part.exec.id === s.exec) out.add(el.id);
    }
    if (s.kind === 'task' || s.kind === 'execution' || s.kind === 'fractal') {
      for (const el of EL.values()) {
        const it = el.data && el.data.item; if (!it) continue;
        if (s.kind !== 'execution' ? it.task === tid : it.id === s.exec || list(it.segs).some(g => g.id === s.exec)) out.add(el.id);
      }
    }
    if (s.kind === 'final') { out.add('f:final'); for (const el of EL.values()) if (el.data && el.data.item && el.data.item.kind === 'review' && (!s.name || el.data.item.review === s.name)) out.add(el.id); }
    if (s.kind === 'verification') { out.add('f:verification'); for (const el of EL.values()) if (el.data && el.data.item && el.data.item.kind === 'verification') out.add(el.id); }
    if (s.kind === 'integration') out.add('f:integration');
    return out;
  }
  function metricColor(el) {
    const o = st.overlay, t = el.data && el.data.task; if (!t || o.color === 'status') return null;
    if (o.color === 'model') return new THREE.Color(modelHex(RM.alias(t.dwarf) || '?'));
    const v = RM.metricValue(t, o.color, st.run, nowSec()), mx = st.metricMax && st.metricMax[o.color];
    if (v === null || !mx) return E.DETAIL.clone();
    return new THREE.Color().copy(E.statusColor('pending')).lerp(E.accent, Math.sqrt(v / mx));
  }
  function retarget() {
    const sel = selIds(), tsel = st.sel && (st.sel.kind === 'task' ? st.sel.id : st.sel.kind === 'execution' || st.sel.kind === 'fractal' ? st.sel.task : null), open = tsel && st.mode === 'plan' && st.view === 'run' ? 't:' + tsel : null;
    const crit = st.overlay.critical && st.crit ? new Set(st.crit.ids.map(id => 't:' + id)) : new Set();
    // the route: what the chosen task waits on and what waits on it, lit like a route set on a signal box desk
    const route = new Set();
    if (tsel && st.layers.route && st.view === 'run' && st.run) { const L = RM.lineage(st.run, tsel); for (const id of [...L.ancestors, ...L.descendants]) route.add('t:' + id); }
    st.routeSet = route;
    if (st.view === 'run' && st.overlay.height !== 'none') {
      st.metricMax = st.metricMax || {};
      st.metricMax[st.overlay.height] = Math.max(0, ...list(st.run.tasks).map(t => RM.metricValue(t, st.overlay.height, st.run, nowSec()) || 0));
    }
    if (st.view === 'run' && st.overlay.color !== 'status' && st.overlay.color !== 'model') {
      st.metricMax = st.metricMax || {};
      st.metricMax[st.overlay.color] = Math.max(0, ...list(st.run.tasks).map(t => RM.metricValue(t, st.overlay.color, st.run, nowSec()) || 0));
    }
    for (const el of EL.values()) {
      baseTargets(el);
      const T = el.tgt;
      T.sel = sel.has(el.id) ? 1 : 0;
      T.route = route.has(el.id) && st.mode === 'plan' ? 1 : 0;
      T.hov = st.hover === el.id ? 1 : 0;
      T.halo = el.pulse ? .22 : sel.has(el.id) ? .16 : 0;
      if (el.kind === 'run' && el.data.module && !el.data.module.match) { T.vis = .14; T.op = .45; T.halo = 0; }
      if (el.kind === 'ghost') { T.opB = .08; T.op = .7; T.lit = 0; T.tint = 0; }
      if (crit.has(el.id)) { T.lit = Math.max(T.lit, .75); T.halo = Math.max(T.halo, .12); }
      if (el.flash && el.flash > performance.now()) { T.lit = Math.max(T.lit, .9); T.halo = Math.max(T.halo, .3); }   // an event, without motion
      if (st.overlay.critical && st.crit && el.kind === 'task' && !crit.has(el.id)) T.vis = Math.min(T.vis, .55);
      el.metricCol = metricColor(el);
      if (el.metricCol) { T.tint = E.theme === 'light' ? .5 : .45; T.lit = .5; }
      if (el.kind === 'task' && st.overlay.height !== 'none') {
        const v = RM.metricValue(el.data.task, st.overlay.height, st.run, nowSec()), mx = st.metricMax[st.overlay.height];
        T.hk = v === null || !mx ? .35 : .4 + 1.4 * Math.sqrt(v / mx);
      } else T.hk = 1;
      if (st.replay !== null && el.data && el.data.item && !el.data.item.ghost) {
        const it = el.data.item;
        if (it.start > st.replay) { T.vis = .22; T.op = .35; T.opB = .25; }
      }
      if (el.kind === 'task') { const pr = PROG.get(el.id); if (pr) pr.t = el.id === open ? 1 : 0; }
      // an open task has the stage: the other blocks sink and fade, as archivision does when a block opens
      // with its route set, the rest of the plan sinks and fades (gentler on the pale panel); the route is lit full
      if (open && route.size && el.id !== open && (el.kind === 'task' || el.kind === 'finish') && !sel.has(el.id)) {
        if (route.has(el.id)) { T.lit = 1; T.halo = Math.max(T.halo, .14); }
        else { T.vis = Math.min(T.vis, E.theme === 'light' ? .5 : .3); T.hk *= .4; T.halo = 0; }
      }
      // on the time axis: the route's attempts stay, the rest dim
      if (tsel && st.layers.route && st.mode === 'exec' && el.data && el.data.item && el.data.item.task && el.data.item.task !== tsel && !route.has('t:' + el.data.item.task)) T.vis = Math.min(T.vis, .35);
    }
    for (const f of FLOWS) {
      f.hot = (st.hover && (f.from === st.hover || f.to === st.hover)) || sel.has(f.from) || sel.has(f.to) ? 1 : 0;
      const onRoute = id => route.has(id) || sel.has(id);
      f.route = route.size && onRoute(f.from) && onRoute(f.to) ? 1 : 0;
      f.crit = st.overlay.critical && st.crit && crit.has(f.from) && crit.has(f.to) && st.crit.ids.indexOf(f.from.slice(2)) === st.crit.ids.indexOf(f.to.slice(2)) - 1 ? 1 : 0;
      const tgtEl = EL.get(f.to);
      f.live = tgtEl && tgtEl.status === 'RUNNING' && f.kind === 'dep';
    }
    E.invalidate(200);
    syncCtl();
  }
  function computeCritical() { st.crit = st.view === 'run' && st.run && st.run.kind !== 'solo' ? RM.criticalPath(st.run, { now: nowSec() }) : null; }

  /* ---------- every frame: ease toward the targets, open, light, move beads ---------- */
  const BEADS = [];
  const beadMat = new THREE.MeshBasicMaterial({ color: E.accent.clone(), toneMapped: false });
  const beadG = new THREE.Group(); E.scene.add(beadG);
  for (let i = 0; i < 24; i++) {
    const m = E.noRay(new THREE.Mesh(new THREE.SphereGeometry(TS, 12, 8), beadMat)); m.visible = false; beadG.add(m);
    const gl = E.glowPoints([new THREE.Vector3()], .3); gl.visible = false; beadG.add(gl); BEADS.push({ m, gl });
  }
  E.themeHooks.add(() => { beadMat.color.copy(E.accent); for (const el of EL.values()) { el.col = E.statusColor(el.status); halo(el); } retarget(); });
  /* ---------- events: what just happened, played once on the plate. A merge sends a pulse to integration, a failed
     attempt a red ring across the plate, an escalation a hop with the two models, a final review a ring in its verdict's
     colour. They play for what a live poll brings and for what a replay passes; with reduced motion the block lights
     instead, and a replay says each event in the live region. ---------- */
  const fxG = new THREE.Group(); fxG.name = 'events'; E.scene.add(fxG);
  const FX = [], RINGS = [], PULSES = [];
  const UNIT_CIRCLE = Array.from({ length: 65 }, (_, i) => [Math.cos(i / 64 * Math.PI * 2), Math.sin(i / 64 * Math.PI * 2), 0]);
  for (let i = 0; i < 6; i++) {
    const g = new THREE.Group(), mat = E.lmat({ color: 0xffffff, transparent: true, linewidth: E.accentLineMat.linewidth, depthTest: false });
    E.polyLine(UNIT_CIRCLE, mat, g); g.visible = false; g.renderOrder = 5; fxG.add(g); RINGS.push({ g, mat, busy: false });
  }
  for (let i = 0; i < 4; i++) {
    const mat = new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false, transparent: true, depthTest: false });
    const m = E.noRay(new THREE.Mesh(new THREE.SphereGeometry(TS, 12, 8), mat)); m.visible = false; m.renderOrder = 5;
    const gl = E.glowPoints([new THREE.Vector3()], .5); gl.visible = false; fxG.add(m, gl); PULSES.push({ m, mat, gl, busy: false });
  }
  const FAILED = new Set(['FAIL', 'ERROR', 'CONFLICT', 'INFRA', 'TIMEOUT', 'BLOCKED']);
  const evKey = ev => [ev.kind, ev.task || ev.name, ev.attempt ?? '', ev.t].join('|');
  let evSeen = new Set(), evRun = null, evAt = null, sayAt = 0;
  // the block an event happened to, in the view shown: the task's block, or on the time axis its attempt then
  function evEl(ev) {
    if (st.mode === 'plan') return EL.get(ev.kind === 'review' ? 'f:final' : 't:' + ev.task);
    let best = null;
    for (const el of EL.values()) {
      const it = el.data && el.data.item; if (!it || it.ghost || el.g !== st.execG) continue;
      const mine = ev.kind === 'review' ? it.kind === 'review' && it.review === ev.name : it.task === ev.task && !it.kind;
      if (mine && it.start <= ev.t + 1 && (!best || it.start > best.data.item.start)) best = el;
    }
    return best;
  }
  const centre = el => { const p = roofTop(el); return [p.x / TS, p.z / TS, p.y / TS]; };   // world back to plan units
  function ring(el, color, from, to, dur) {
    const r = RINGS.find(x => !x.busy); if (!r) return;
    const [x, y, z] = centre(el); r.busy = true; r.g.position.copy(W(x, y, z + 1)); r.mat.color.copy(color); r.g.visible = true;
    FX.push({ t0: performance.now(), dur, step: k => { const e = 1 - Math.pow(1 - k, 3); r.g.scale.setScalar(from + (to - from) * e); E.setOp(r.mat, (1 - k) * .95); },
      end: () => { r.g.visible = false; r.busy = false; } });
  }
  function pulse(a, b, color, dur, then) {
    const p = PULSES.find(x => !x.busy); if (!p) { if (then) then(); return; }
    const A = centre(a), B = centre(b), pts = E.arc(A, B, 24, .16, 140), cum = E.cumLen(pts);
    p.busy = true; p.mat.color.copy(color); p.gl.material.uniforms.color.value.copy(color); p.m.visible = p.gl.visible = true; p.m.scale.setScalar(4.5);
    FX.push({ t0: performance.now(), dur, step: k => { const q = E.alongPts(pts, cum, ez(k)); p.m.position.copy(W(...q)); E.moveGlow(p.gl, q, 1); },
      end: () => { p.m.visible = p.gl.visible = false; p.busy = false; if (then) then(); } });
  }
  function hop(el, text) {
    const [x, y, z] = centre(el), w = Math.ceil(text.length * 11 * E.CW) + 16;
    const t = E.sprite(w, 22, 3, (c, s, C) => {
      E.rr(c, s, 1, 1, w - 2, 20, 6); c.globalAlpha = .92; c.fillStyle = C.bg; c.fill(); c.globalAlpha = 1;
      c.strokeStyle = C.acc; c.lineWidth = Math.max(1, s); c.stroke();
      E.font(c, 11, s, 600); c.fillStyle = C.accHi; c.fillText(text, 8 * s, 15 * s);
    }, { screen: true, px: 20 });
    t.mesh.center.set(.5, 0); t.mat.depthTest = false; t.mesh.renderOrder = 7; fxG.add(t.mesh);
    const r = el.n.w / 2;
    const pts = E.arc([x - r * .8, y, z], [x + r * .8, y, z], 8, .3, 60), mat = E.lmat({ color: E.accent.clone(), transparent: true, linewidth: E.accentLineMat.linewidth, depthTest: false });
    const line = E.polyLine(pts, mat, fxG);
    FX.push({ t0: performance.now(), dur: 2200, step: k => { t.mesh.position.copy(W(x, y, z + 30 + 50 * ez(k))); const o = k < .7 ? 1 : 1 - (k - .7) / .3; t.mat.opacity = o; E.setOp(mat, o); },
      end: () => { E.disposeGroup(t.mesh); fxG.remove(t.mesh); E.disposeGroup(line); fxG.remove(line); } });
  }
  function flash(el) { el.flash = performance.now() + 2600; retarget(); setTimeout(() => retarget(), 2700); }
  function say(ev) {
    if (ev.kind === 'merge') return `${ev.task} merged`;
    if (ev.kind === 'attempt') return `${ev.task} attempt ${ev.attempt ?? ''} ${String(ev.status || '').toLowerCase()}`.replace(/\s+/g, ' ');
    if (ev.kind === 'escalation') return `${ev.task} escalated ${RM.alias(ev.from) || ev.from} → ${RM.alias(ev.to) || ev.to}`;
    return `final review ${ev.name || ''}: ${String(ev.verdict || '').toLowerCase()}`;
  }
  function playEvent(ev) {
    const el = evEl(ev); if (!el || !el.root.parent) return false;
    if (ev.kind === 'attempt' && !FAILED.has(ev.status)) return false;
    if (reduce) { flash(el); return true; }
    const r0 = Math.max(el.n.w, el.n.d) * .55;
    if (ev.kind === 'merge') {
      const integ = st.mode === 'plan' && st.layers.finish ? EL.get('f:integration') : null, green = E.statusColor('merged');
      if (integ) pulse(el, integ, green, 900, () => ring(integ, green, Math.max(integ.n.w, integ.n.d) * .5, Math.max(integ.n.w, integ.n.d) * .5 + 60, 900));
      else ring(el, green, r0, r0 + 60, 900);
    } else if (ev.kind === 'attempt') ring(el, E.statusColor('fail'), r0, r0 + 220, 1400);
    else if (ev.kind === 'escalation') hop(el, `${RM.alias(ev.from) || ev.from || '?'} → ${RM.alias(ev.to) || ev.to || '?'}`);
    else if (ev.kind === 'review') ring(el, E.statusColor(['PASS', 'ACCEPT', 'APPROVE'].includes(String(ev.verdict || '').toUpperCase()) ? 'pass' : 'fail'), r0, r0 + 120, 1200);
    E.invalidate(100);
    return true;
  }
  // events not seen yet (a poll) or passed since the last replay time (a replay playing forward): at most six at once
  function syncEvents({ replayFrom = null } = {}) {
    if (st.view !== 'run' || !st.run) return;
    const all = RM.events(st.run);
    if (evRun !== st.run.key) { evRun = st.run.key; evSeen = new Set(all.map(evKey)); evAt = st.replay; return; }
    let fresh;
    if (st.replay !== null) {
      fresh = replayFrom !== null && st.replay > replayFrom ? all.filter(ev => ev.t > replayFrom && ev.t <= st.replay) : [];
      evAt = st.replay;
    } else fresh = all.filter(ev => !evSeen.has(evKey(ev)));
    for (const ev of all) evSeen.add(evKey(ev));
    const shown = fresh.slice(-6).filter(playEvent);
    if (st.replay !== null && shown.length && live && performance.now() - sayAt > 1500) { sayAt = performance.now(); live.textContent = shown.slice(-3).map(say).join('; '); }
  }

  let lastPulse = 0, lastText = 0;
  function frameStep(now, dt) {
    const kk = reduce ? 1 : Math.min(1, dt * 7);
    let moving = false;
    // the plan ⇄ exec switch: blocks travel toward where they ran, while one view fades into the other
    let fk = null;
    if (st.fade) {
      fk = ez(clamp((now - st.fade.t0) / st.fade.dur)); moving = true;
      if (fk >= 1) { st.planG.visible = st.mode === 'plan'; st.execG.visible = st.mode === 'exec'; for (const el of EL.values()) el.offset.set(0, 0, 0); st.fade = null; retarget(); fk = null; }
    }
    for (const [id, pr] of PROG) {
      if (pr.p === pr.t) continue;
      pr.p = pr.t > pr.p ? Math.min(pr.t, pr.p + dt * 1000 / 1300) : Math.max(pr.t, pr.p - dt * 1000 / 600);
      if (reduce) pr.p = pr.t;
      const el = EL.get(id); if (el && pr.t > 0 && !el.inner) buildInner(el);
      moving = true;
    }
    const pulse = .5 + .5 * Math.sin(now / 420);
    let pulsing = false;
    for (const el of EL.values()) {
      const c = el.cur, T = el.tgt;
      for (const k of ['vis', 'sel', 'hov', 'lit', 'op', 'opB', 'tint', 'halo', 'hk', 'route']) {
        const d = T[k] - c[k]; if (Math.abs(d) > .002) { c[k] += d * kk; moving = true; } else c[k] = T[k];
      }
      // which view the element belongs to, during a switch
      let gf = 1;
      if (fk !== null) {
        const inPlan = el.g === st.planG || (el.kind === 'part');
        gf = st.mode === 'exec' ? (inPlan ? 1 - fk : fk) : (inPlan ? fk : 1 - fk);
        const off = st.fade.off.get(el.id);
        if (off) { const k = inPlan ? (st.mode === 'exec' ? fk : 1 - fk) : (st.mode === 'exec' ? 1 - fk : fk); el.offset.copy(off).multiplyScalar(k); }
      }
      el.root.position.copy(el.offset);
      let lift = 0, ob = 1, ol = 1;
      const pr = PROG.get(el.id);
      if (pr) { const p = pr.p; lift = ez(segT(p, .04, .46)); ob = 1 - segT(p, .06, .3); ol = 1 - segT(p, .14, .46); }
      if (el.kind === 'part') { const par = EL.get(el.data.parent), pp = par && PROG.get(par.id); const p = pp ? pp.p : 0; const [a, b] = [.34 + el.data.idx * .05, .64 + el.data.idx * .05]; const q = back(segT(p, a, b)); el.q = q; el.root.visible = q > .001; el.cover.scale.y = Math.max(.001, q); }
      el.cover.visible = ol > .003;
      el.cover.position.y = (el.kind === 'task' ? 120 : 0) * lift * TS;
      if (el.kind === 'task' || el.kind === 'finish') el.cover.scale.y = Math.max(.05, c.hk);
      const pulseK = el.pulse && st.replay === null ? pulse : el.pulse ? .6 : 0;
      if (el.pulse) pulsing = true;
      const col = el.metricCol || el.col;
      TMP.copy(col).lerp(E.accent, c.sel);
      if (c.route > .01) TMP.lerp(E.INKHI, .55 * c.route * (1 - c.sel));   // a set route: its blocks' hairlines take the route lamp
      E.paint(el.M, c.vis, Math.min(1, Math.max(c.lit + c.sel * .9, c.route * .85)), c.hov, c.op * ol * gf, c.opB * ob * gf, TMP, 0);
      if (c.tint > .001) el.M.body.color.lerp(TMP2.copy(col), c.tint * c.vis);
      if (el.tintMat) { el.tintMat.color.copy(col); el.tintMat.opacity = (c.tint * (E.theme === 'light' ? 1.45 : 1.1) + .08 * c.sel) * ob * gf * c.vis; }
      if (el.haloMat) el.haloMat.uniforms.opacity.value = (c.halo + (el.pulse ? .12 * pulseK : 0)) * gf * c.vis;
      if (el.clef) el.clef.fgM.color.copy(E.statusColor(['MERGED', 'PASS'].includes(el.status) ? 'pass' : ['FAIL', 'ERROR', 'CONFLICT'].includes(el.status) ? 'fail' : 'pending'));
      if (el.inner) {
        const p = pr ? pr.p : 0, vis = p > .002;
        if (!vis && el.inner.visible) { E.release(el.inner); }
        el.inner.visible = vis;
        if (vis) {
          const fo = segT(p, 0, .3) * gf;
          E.setOp(el.floorMat, fo); E.setOp(el.innerLine, fo); E.lineCol(el.innerLine, E.lineMat.color, 0, 1);
          for (const z of el.zoneEls) { E.lineCol(z.mat, E.majorMat.color, 0, 1); z.mat.color.lerp(E.INKHI, .18); E.setOp(z.mat, fo); z.lab.mat.opacity = fo; }
          for (const f of el.innerFlows) {
            const qa = EL.get(f.a), qb = EL.get(f.b), o = segT(Math.min(qa ? qa.q || 0 : 0, qb ? qb.q || 0 : 0), .7, 1) * gf;
            f.line.visible = f.cone.visible = o > .01;
            const hot = st.sel && (sel(f.a) || sel(f.b)) ? 1 : 0;
            E.lineCol(f.mat, E.outlineMat.uniforms.color.value, f.col ? 1 : hot, 1, f.col || E.accent); f.mat.linewidth = E.lineMat.linewidth * (f.col ? 1.6 : 1.15 + .5 * hot); E.setOp(f.mat, o);
            f.am.color.copy(f.mat.color); f.am.opacity = o; if (f.lab) f.lab.mat.opacity = o;
          }
        }
      }
    }
    for (const f of FLOWS) {
      const a = EL.get(f.from), b = EL.get(f.to);
      let gf = 1;
      if (fk !== null) gf = st.mode === 'exec' ? 1 - fk : fk;
      const vis = Math.min(a ? a.cur.vis : 1, b ? b.cur.vis : 1);
      const want = Math.max(f.hot, f.crit, f.route || 0);
      f.cur += (want - f.cur) * kk; if (Math.abs(want - f.cur) > .002) moving = true;
      // a set route is drawn in the route lamp's white; the critical path and hover keep the accent
      E.lineCol(f.mat, E.outlineMat.uniforms.color.value, f.crit ? 1 : f.cur * .9, vis, f.route && !f.crit ? E.INKHI : E.accent);
      if (!f.crit && !f.cur) f.mat.color.lerp(E.INKHI, .12);
      f.mat.linewidth = E.lineMat.linewidth * (1 + .9 * Math.max(f.cur, f.crit));
      E.setOp(f.mat, (.55 + .45 * Math.max(f.cur, f.crit)) * gf);
      f.am.color.copy(f.mat.color); f.am.opacity = f.mat.opacity;
    }
    // beads: into running tasks, along the selected task's cables, inside an open running task
    const want = [];
    const tB = now / 1600;
    for (const f of FLOWS) if ((f.live || f.hot || f.crit || f.route) && (st.mode === 'plan' || st.fade) && st.planG && st.planG.visible) want.push({ pts: f.pts, cum: f.cum, t: (tB + (f.pts[0][0] % 97) / 97) % 1, r: 3.2, size: .32, k: .8 });
    for (const el of EL.values()) if (el.inner && el.inner.visible) for (const f of el.innerFlows) {
      const b = EL.get(f.b); if (b && b.status === 'RUNNING' || f.kind === 'flow' && st.sel && sel(f.a)) want.push({ pts: f.pts, cum: f.cum, t: (tB * 1.4) % 1, r: 1.1, size: .12, k: .8 });
    }
    BEADS.forEach((bd, i) => {
      const w = want[i]; bd.m.visible = bd.gl.visible = !!w && !reduce && st.layers.beads; if (!bd.m.visible) return;
      const p = E.alongPts(w.pts, w.cum, w.t); bd.m.position.copy(W(...p)); bd.m.scale.setScalar(w.r);
      bd.gl.material.uniforms.size.value = w.size; E.moveGlow(bd.gl, p, w.k);
    });
    // elapsed times on running blocks tick once a second
    if (now - lastText > 1000) { lastText = now; for (const el of EL.values()) if (el.pulse && !el.roofOnly) for (const t of el.texts) if (t !== el.roof && !t.gone) t.redraw(); }
    if (st.view === 'run' && st.exec && st.exec.live && st.mode === 'exec') { placeNow(); }
    if (BEACONS.tick(now)) moving = true;
    if (tagsTick(now)) moving = true;
    for (let i = FX.length - 1; i >= 0; i--) {
      const f = FX[i], k = Math.min(1, (now - f.t0) / f.dur);
      if (k >= 1) { FX.splice(i, 1); f.end(); } else f.step(k);
      moving = true;
    }
    if (!moving && (pulsing || want.length) && !reduce) {
      // only lamps and beads move: about 30 frames a second is plenty
      if (now - lastPulse > 30) { lastPulse = now; setTimeout(() => E.invalidate(), 33); }
      return false;
    }
    return moving;
  }
  const sel = id => selIds().has(id);
  E.tickers.add(frameStep);
  E.controls.addEventListener('start', hideTip);

  /* ---------- pointer and keys ---------- */
  const elOf = o => o && o.userData && o.userData.el ? EL.get(o.userData.el) : null;
  const pickable = o => {
    for (let x = o; x; x = x.parent) if (!x.visible) return false;
    const el = elOf(o);
    if (el && o.userData.cover) { const pr = PROG.get(el.id); if (pr && pr.p > .22) return false; if (el.kind === 'part' && (el.q || 0) < .5) return false; }
    return true;
  };
  function info(el) {
    if (!el) return null;
    if (el.kind === 'run') { const r = el.data.run; return { meta: `${RM.basename(r.repo)} · ${r.kind}${el.data.module.match ? '' : ' · filtered out'}`, title: String(r.goal || 'Untitled run').split('\n')[0], body: runInfo(r), foot: 'click to open this run' }; }
    if (el.kind === 'task') return taskCard(el);
    if (el.kind === 'finish') { const fi = finishInfo(el.id); return { meta: 'finish', title: fi.title, body: `${fi.status} · ${fi.sub}`, foot: fi.line }; }
    if (el.kind === 'part') { const p = el.data.part; return { meta: `${p.label.toLowerCase()} · attempt ${p.attempt ?? '—'}`, title: p.spec || '—', body: p.planned ? 'planned' : p.status, foot: p.exec && p.exec.id ? 'click for prompt, model and timings' : '' }; }
    const it = el.data.item; if (!it) return null;
    if (it.kind === 'verification') return { meta: 'verification', title: 'Verification', body: attemptLine(el) };
    if (it.kind === 'review') return { meta: 'final review', title: it.review, body: attemptLine(el) };
    return { meta: it.ghost ? 'projected by forge-schedule rules' : `attempt ${it.attempt ?? '—'} · lane ${it.lane + 1}`, title: it.task, body: attemptLine(el), foot: new Date(it.start * 1000).toLocaleTimeString() + ' → ' + new Date(it.end * 1000).toLocaleTimeString() };
  }
  // what a finding says after its "- [P1][CONFIRMED] path:line —" head
  function said(f) {
    const t = String(f.summary || '').replace(/^\s*-\s*\[P\d\]\s*(\[[A-Z]+\]\s*)?/, ''), i = t.indexOf(' — ');
    return i >= 0 && i < 200 ? t.slice(i + 3) : t;
  }
  function findingInfo(d) {
    const f = d.finding, r = d.review || {};
    const who = r.kind === 'final' ? `final review ${d.final}` : `${r.kind || 'review'} review${r.attempt != null ? ', attempt ' + r.attempt : ''} of ${d.task}`;
    return { meta: `${f.severity || 'no severity'} · ${String(f.status || '').toLowerCase()} · ${who}`, title: f.location || 'no location named', body: said(f), foot: 'click to open it in the diff' };
  }
  // a task's card: what it is, how it went, who did it, its worst finding and its place in the plan
  function taskCard(el) {
    const t = el.data.task, now = nowSec(), tt = RM.taskTimes(t, st.run, now), tok = RM.metricValue(t, 'tokens', st.run, now);
    const tries = list(t.attempts).length;
    const how = [tries ? `${tries} attempt${tries > 1 ? 's' : ''}` : null, tt.busy ? 'busy ' + RM.fmtDur(tt.busy) : null,
      tok ? `${tok >= 1e6 ? (tok / 1e6).toFixed(1) + 'M' : tok >= 1e3 ? Math.round(tok / 1e3) + 'k' : tok} tokens` : null].filter(Boolean).join(' · ');
    const body = [how, `${t.dwarf || '—'}${t.explored ? ' *' : ''} → ${t.qa || '—'}`];
    const tf = st.findings && list(st.findings.tasks).find(x => x.id === t.id);
    let note = null;
    if (tf) {
      const latest = Math.max(...tf.reviews.map(r => r.attempt ?? 0));
      const fs = tf.reviews.filter(r => (r.attempt ?? 0) === latest).flatMap(r => r.findings.map(f => ({ f }))).sort(byRank);
      if (fs.length) note = { sev: fs[0].f.severity, text: `${fs[0].f.location ? fs[0].f.location + ' — ' : ''}${said(fs[0].f)}${fs.length > 1 ? ` (+${fs.length - 1} more)` : ''}` };
    }
    const L = st.run ? RM.lineage(st.run, t.id) : { ancestors: new Set(), descendants: new Set() };
    const foot = [list(t.deps).length ? 'after ' + t.deps.join(', ') : 'no dependencies', L.descendants.size ? `${L.descendants.size} task${L.descendants.size > 1 ? 's' : ''} wait on it` : null].filter(Boolean).join(' · ');
    return { meta: `task · wave ${t.wave ?? '—'} · ${t.difficulty || '—'}`, title: `${t.id} · ${t.title || ''}`, status: el.status, body, note, foot };
  }
  function showTip(el, cx, cy, inf = info(el)) {
    if (!inf) return hideTip();
    tip.replaceChildren();
    for (const [cls, text] of [['meta', inf.meta], ['title', inf.title], ...[].concat(inf.body || []).map(b => ['body', b]), ['note', inf.note], ['foot', inf.foot]]) {
      if (!text) continue; const d = document.createElement('div'); d.className = 'stage-tip-' + cls;
      // the status leads the first line as the inspector shows it: a chip with its lamp
      if (cls === 'body' && inf.status && !tip.querySelector('.chip')) { const c = document.createElement('span'); c.className = 'chip st-' + String(inf.status).toLowerCase(); c.textContent = inf.status; d.append(c, ' '); }
      if (typeof text === 'string') d.append(text);
      else { const c = document.createElement('span'); c.className = 'sev-chip sev-' + (['P0', 'P1', 'P2', 'P3'].includes(text.sev) ? text.sev : 'none'); c.textContent = text.sev || '?'; d.append(c, text.text); }
      tip.append(d);
    }
    tip.hidden = false;
    // inside the part of the stage the modules and the stage's keys leave free
    const r = host.getBoundingClientRect(), tw = tip.offsetWidth || 240, th = tip.offsetHeight || 80, I = E.overlayInset();
    const bank = CTL && !CTL.bank.hidden ? CTL.bank.getBoundingClientRect() : null;
    const L = I.l + 4, R = r.width - I.r - (bank && bank.width && bank.left - r.left > r.width / 2 ? r.right - bank.left + 4 : 8), T = I.t + 4, B = r.height - I.b - 8;
    let x = cx - r.left + 16, y = cy - r.top + 16;
    if (x + tw > R) x = cx - r.left - tw - 16;
    if (y + th > B) y = cy - r.top - th - 16;
    x = Math.max(L, Math.min(x, R - tw)); y = Math.max(T, Math.min(y, B - th));
    tip.style.transform = `translate(${Math.max(4, x).toFixed(0)}px, ${Math.max(4, y).toFixed(0)}px)`;
  }
  function hideTip() { tip.hidden = true; }
  let hoverRaf = 0, hoverAt = null;
  E.handlers.hover = (cx, cy) => {
    hoverAt = cx === null ? null : [cx, cy];
    if (hoverRaf) return;
    hoverRaf = requestAnimationFrame(() => {
      hoverRaf = 0;
      // a finding's mark floats in front of its block: it is picked first
      const b = hoverAt && st.view === 'run' ? BEACONS.pick(hoverAt[0], hoverAt[1]) : null;
      BEACONS.setHot(b ? b.key : null);
      if (b) {
        E.canvas.classList.add('pointing');
        if (st.hover !== null) { st.hover = null; retarget(); }
        showTip(null, hoverAt[0], hoverAt[1], findingInfo(b.data)); E.invalidate(); return;
      }
      const o = hoverAt ? E.pick(hoverAt[0], hoverAt[1], PICK, pickable) : null;
      const el = elOf(o), id = el && !(o.userData.floor) ? el.id : null;
      E.canvas.classList.toggle('pointing', !!id);
      if (id !== st.hover) { st.hover = id; retarget(); }
      if (el && !o.userData.floor && hoverAt) showTip(el, hoverAt[0], hoverAt[1]); else hideTip();
    });
  };
  function selFor(el) {
    if (!el) return null;
    if (el.kind === 'task') return { kind: 'task', id: el.data.task.id };
    if (el.kind === 'part') { const p = el.data.part; return p.exec && p.exec.id && !p.planned ? { kind: 'execution', task: el.data.task, id: el.data.task, exec: p.exec.id, shadow: p.kind === 'shadow' } : { kind: 'task', id: el.data.task }; }
    if (el.kind === 'finish') return { kind: el.id === 'f:final' ? 'final' : el.id === 'f:verification' ? 'verification' : 'integration' };
    const it = el.data.item; if (!it) return null;
    if (it.kind === 'review') return { kind: 'final', name: it.review };
    if (it.kind === 'verification') return { kind: 'execution', task: null, exec: it.id };
    if (it.ghost || String(it.id).startsWith('span:')) return { kind: 'task', id: it.task };
    return { kind: 'execution', task: it.task, id: it.task, exec: it.id };
  }
  E.handlers.click = (cx, cy) => {
    hideTip();
    const b = st.view === 'run' ? BEACONS.pick(cx, cy) : null;
    if (b) { cb.onFinding && cb.onFinding(b.data); return; }
    const o = E.pick(cx, cy, PICK, pickable), el = elOf(o);
    if (o && o.userData.fractal) { cb.onSelect && cb.onSelect({ kind: 'fractal', task: o.userData.fractal.task, node: o.userData.fractal.node }); return; }
    if (st.view === 'atlas') {
      if (el && el.kind === 'run') {
        st.sel = { kind: 'run', key: el.data.run.key }; retarget();
        const { x, y, w, d, h } = el.n;
        E.frameBox([x - 20, y - 20, 0, x + w + 20, y + d + 20, h * 2], 600, 40);
        setTimeout(() => cb.onOpenRun && cb.onOpenRun(el.data.run.key), reduce ? 0 : 520);
      }
      return;
    }
    if (!el || o.userData.plate) { cb.onSelect && cb.onSelect(null); return; }
    if (o.userData.floor) { cb.onSelect && cb.onSelect({ kind: 'task', id: el.data.task.id }); return; }
    cb.onSelect && cb.onSelect(selFor(el));
  };
  function order() { return st.plan ? st.plan.modules.filter(m => m.kind === 'task').map(m => m.task.id) : []; }
  // the map by keyboard: ← → walk the runs, ↑ ↓ jump a repository, Enter opens; the run's card shows where it is
  const live = document.getElementById('live');
  function walkAtlas(e) {
    // in reading order: repository by repository as the plate lays them out, then row by row
    const A = st.atlas, li = new Map((A ? A.layers : []).map((l, j) => [l.id, j]));
    const mods = A ? A.modules.filter(m => A.place[m.id] && m.match !== false)
      .sort((a, b) => (li.get(a.layer) ?? 0) - (li.get(b.layer) ?? 0) || A.place[a.id][1] - A.place[b.id][1] || A.place[a.id][0] - A.place[b.id][0]) : [];
    if (!mods.length) return false;
    const cur = st.sel && st.sel.kind === 'run' ? mods.findIndex(m => m.run.key === st.sel.key) : -1;
    if (e.key === 'Enter') { if (cur < 0) return false; cb.onOpenRun && cb.onOpenRun(mods[cur].run.key); return true; }
    let i;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { const d = e.key === 'ArrowRight' ? 1 : -1; i = cur < 0 ? (d > 0 ? 0 : mods.length - 1) : (cur + d + mods.length) % mods.length; }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      const d = e.key === 'ArrowDown' ? 1 : -1, layer = cur < 0 ? null : mods[cur].layer;
      const starts = mods.map((m, j) => j).filter(j => j === 0 || mods[j].layer !== mods[j - 1].layer);
      const at = layer === null ? -1 : starts.findIndex(j => mods[j].layer === layer);
      i = starts[at < 0 ? (d > 0 ? 0 : starts.length - 1) : (at + d + starts.length) % starts.length];
    } else return false;
    const m = mods[i]; st.sel = { kind: 'run', key: m.run.key }; retarget();
    const el = EL.get('r:' + m.id);
    if (el) {
      const r = E.canvas.getBoundingClientRect(), p = E.project(el.n.x + el.n.w / 2, el.n.y + el.n.d / 2, el.n.h);
      if (!p.on) E.frameBox([el.n.x - 60, el.n.y - 60, 0, el.n.x + el.n.w + 60, el.n.y + el.n.d + 60, el.n.h * 2], 500, 30);
      else showTip(el, r.left + p.x, r.top + p.y);
    }
    if (live) live.textContent = `${String(m.run.goal || m.run.run_id || m.run.key).split('\n')[0]}: ${m.state || ''}, ${RM.basename(m.run.repo || '')}, ${i + 1} of ${mods.length}`;
    return true;
  }
  E.handlers.key = e => {
    if (e.key === 'Escape') { if (st.view === 'run' && st.sel) { cb.onSelect && cb.onSelect(null); return true; } if (st.view === 'run' && cb.onBack) { cb.onBack(); return true; } if (st.view === 'atlas' && st.sel) { st.sel = null; hideTip(); retarget(); return true; } return false; }
    if (st.view === 'atlas') return walkAtlas(e);
    if (st.view !== 'run') return false;
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const ids = order(); if (!ids.length) return true;
      const cur = st.sel && (st.sel.kind === 'task' ? st.sel.id : st.sel.task), d = e.key === 'ArrowRight' ? 1 : -1;
      const i = ids.indexOf(cur), next = i < 0 ? (d > 0 ? 0 : ids.length - 1) : (i + d + ids.length) % ids.length;
      cb.onSelect && cb.onSelect({ kind: 'task', id: ids[next] }); return true;
    }
    if (e.key === 'e' || e.key === 'E') { cb.onMode && cb.onMode(st.mode === 'plan' ? 'exec' : 'plan'); return true; }
    return false;
  };

  // the cloud has its own pointer and keys: while it is shown, the engine's handlers go to it
  let cloud = null;
  for (const k of Object.keys(E.handlers)) {
    const mine = E.handlers[k];
    E.handlers[k] = (...a) => {
      if (st.view === 'cloud') return cloud && cloud.handlers[k] ? cloud.handlers[k](...a) : k === 'grab' ? null : false;
      return mine ? mine(...a) : k === 'grab' ? null : false;
    };
  }
  const leaveCloud = () => { if (cloud) cloud.detach(); };

  /* ---------- public API ---------- */
  function frameSelection(dur = 850, dir = null) {
    if (st.view !== 'run') return;
    const ids = [...selIds()].map(id => EL.get(id)).filter(el => el && el.root.parent && (st.mode === 'plan' ? el.g === st.planG || el.kind === 'part' : el.g === st.execG));
    if (!ids.length) { E.frameBox(boxOf(st.mode), dur, 26, dir); return; }
    // a chosen task's route is seen end to end: what it waits on and what waits on it come into the frame
    if (st.mode === 'plan' && st.routeSet) for (const id of st.routeSet) { const el = EL.get(id); if (el && el.root.parent) ids.push(el); }
    hideTip();
    const xs = ids.flatMap(el => [el.n.x, el.n.x + el.n.w]), ys = ids.flatMap(el => [el.n.y, el.n.y + el.n.d]);
    const hs = Math.max(...ids.map(el => el.n.h));
    // keep the neighbours in view: the selection fills about half the stage, not all of it
    const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
    const pad = Math.max(st.mode === 'exec' ? 80 : 40, span * .3);
    const opening = st.mode === 'plan' && ids.some(el => el.kind === 'task');
    const tree = opening && ids.some(el => FRACTAL.has(el.data && el.data.task && el.data.task.id)) ? hs + 130 : 0;   // a Fractal tree floats above
    E.frameBox([Math.min(...xs) - pad, Math.min(...ys) - pad, 0, Math.max(...xs) + pad, Math.max(...ys) + pad, Math.max(tree, hs * (opening ? 2 : 1.4))], dur, opening ? 52 : 40, dir);
  }

  /* ---------- the view controls (stagectl.js): fit, zoom, projection, reset, the legend ---------- */
  const ISO_DIR = new THREE.Vector3(1, 1, 1), TOP_DIR = new THREE.Vector3(0, 1, .002);
  let proj = 'persp';
  try { proj = localStorage.getItem('forge-ui-projection') || 'persp'; } catch { /* storage refused: perspective */ }
  if (!['persp', 'iso', 'top'].includes(proj)) proj = 'persp';
  if (proj !== 'persp') { E.setOrtho(true); if (proj === 'top') E.camera.position.copy(TOP_DIR).normalize().multiplyScalar(20); }
  BEACONS.setFlat(proj === 'top');
  function frameShown(dur = 650, dir = null) {
    if (st.view === 'run' && st.run) { if (st.sel) frameSelection(dur, dir); else E.frameBox(boxOf(st.mode), dur, 26, dir); }
    else if (st.view === 'atlas' && st.atlasBox) E.frameBox(st.atlasBox, dur, 30, dir);
  }
  function setProjection(p, dur = 650, dir) {
    const was = proj; proj = ['persp', 'iso', 'top'].includes(p) ? p : 'persp';
    try { localStorage.setItem('forge-ui-projection', proj); } catch { /* not remembered */ }
    E.setOrtho(proj !== 'persp'); BEACONS.setFlat(proj === 'top'); syncBeacons();
    // isometric is the true 35° view; leaving top-down goes back to it
    frameShown(dur, dir !== undefined ? dir : proj === 'top' ? TOP_DIR : proj === 'iso' || was === 'top' ? ISO_DIR : null);
  }
  const fmtMetric = (m, v) => v === null || v === undefined ? '—' : m === 'duration' ? RM.fmtDur(v) : m === 'tokens' ? (v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? Math.round(v / 1e3) + 'k' : String(v)) : String(Math.round(v));
  const modelHex = a => MODEL_PALETTE[[...a].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7) % MODEL_PALETTE.length];
  // what the legend says: only what the stage draws now
  function legendModel() {
    const L = st.layers, o = st.overlay, out = [];
    const lamps = { title: 'Lamps', rows: [
      { sw: { lamp: 'st-merged' }, label: 'merged · passed · accepted' }, { sw: { lamp: 'st-running' }, label: 'running · interrupted' },
      { sw: { lamp: 'st-fail' }, label: 'failed · conflict · infra · timeout' }, { sw: { lamp: 'st-pending' }, label: 'pending, not started' },
      { sw: { route: true }, label: st.view === 'atlas' ? 'chosen run' : 'chosen task' + (L.route ? ' and its route' : '') }] };
    if (st.view === 'atlas') {
      out.push({ title: 'Map', rows: [], note: 'Each block is a run, each district a repository. The lamps on a block are its tasks; the block stands taller the more tasks it has.' }, lamps);
      return out;
    }
    if (st.view !== 'run') return out;
    out.push(lamps);
    if (st.mode === 'exec') out.push({ title: 'Execution', rows: [], note: 'One block per attempt on wall-clock time, in the lane it ran in: the dwarf, then its review.' });
    const cables = [];
    if (L.cables) cables.push({ sw: { cable: 'dep' }, label: 'a task waits on the one this comes from' });
    if (L.cables && L.beads) cables.push({ sw: { bead: true }, label: 'work flowing into a running task' });
    if (L.cables && L.route) cables.push({ sw: { cable: 'route' }, label: 'route of the chosen task' });
    if (L.cables && o.critical) cables.push({ sw: { cable: 'critical' }, label: 'critical path: it bounded the wall time' });
    cables.push({ sw: { cable: 'retry' }, label: 'inside a task: retry, shadow review' }, { sw: { cable: 'escalation' }, label: 'inside a task: escalation to another model' });
    out.push({ title: 'Cables', rows: cables });
    const fs = L.beacons && st.findings ? BEACONS.items : [];
    if (fs.length) {
      const has = new Set(fs.map(it => it.shape));
      const rows = [['P0', 'P0 · spike'], ['P1', 'P1 · diamond'], ['P2', 'P2 · cone'], ['P3', 'P3 · pin'], ['none', 'no severity · ring']].filter(([k]) => has.has(k)).map(([k, label]) => ({ sw: { sev: k }, label }));
      if (fs.some(it => it.plausible)) rows.push({ sw: { sev: 'P2', pale: true }, label: 'plausible: smaller, paler' });
      out.push({ title: 'Findings', rows, note: 'A click opens the diff at the finding.' });
    }
    if (o.arcs && list(st.run.tasks).some(t => t.clef && t.clef.first_pass)) out.push({ title: 'Review marks', rows: [{ sw: { ring: true }, label: 'Clef ring: the chance the first try passes' }] });
    if (o.height !== 'none') {
      const mx = st.metricMax && st.metricMax[o.height];
      out.push({ title: 'Height', rows: [{ sw: { height: true }, label: `${o.height}: up to ${fmtMetric(o.height, mx)}` }] });
    } else out.push({ title: 'Height', rows: [{ sw: { height: true }, label: 'difficulty' }] });
    if (o.color === 'model') {
      const models = [...new Set(list(st.run.tasks).map(t => RM.alias(t.dwarf) || '?'))].sort();
      out.push({ title: 'Colour: model', rows: models.map(a => ({ sw: { color: modelHex(a) }, label: a })) });
    } else if (o.color !== 'status') {
      const mx = st.metricMax && st.metricMax[o.color];
      out.push({ title: `Colour: ${o.color}`, rows: [{ sw: { scale: [fmtMetric(o.color, 0), fmtMetric(o.color, mx)] }, label: '' }] });
    }
    return out;
  }
  let CTL = null, ctlQ = 0;
  const syncCtl = () => { if (CTL && !ctlQ) ctlQ = requestAnimationFrame(() => { ctlQ = 0; CTL.sync(); }); };
  const api = {
    showAtlas(runs, filter, opts = {}) {
      st.runs = list(runs); st.filter = filter || {}; st.atlasOpts = { showAll: !!opts.showAll };
      const A = RM.atlasModel(st.runs, st.filter, { now: nowSec(), ...st.atlasOpts });
      const sig = JSON.stringify(A.modules.map(m => [m.id, m.tasks.length, m.layer]));
      if (st.view !== 'atlas' || sig !== st.atlasSig) {
        leaveCloud(); st.view = 'atlas'; st.run = null; st.sel = null; clearWorld(); st.atlasSig = sig; st.rebuilds++; buildAtlas();
      } else updateAtlas();
      E.canvas.setAttribute('aria-label', `Map of ${A.modules.length} runs, ${A.matches} matching the filter${A.hidden ? `, ${A.hidden} older runs hidden` : ''}`);
      retarget();
      return { hidden: A.hidden, total: A.total, matches: A.matches, shown: A.modules.length };
    },
    showRun(run, { mode, sel } = {}) {
      if (mode) st.mode = mode;
      const key = RM.structureKey(run), first = st.view !== 'run' || !st.run || st.run.key !== run.key;
      st.run = run; st.replayState = st.replay !== null ? RM.stateAt(run, st.replay) : null;
      const selChanged = sel !== undefined && JSON.stringify(sel || null) !== JSON.stringify(st.sel || null);
      if (sel !== undefined) st.sel = sel;
      if (selChanged && !first && key === st.structure) { api.update(run); retarget(); frameSelection(); return; }
      if (first || key !== st.structure) {
        leaveCloud(); st.view = 'run'; st.structure = key; buildRun();
        if (first) {
          const b = boxOf(st.mode);
          if (!reduce) { E.frameBox(b, 0, 26); }
          if (st.sel) frameSelection(first ? 1100 : 850); else E.frameBox(b, first ? 1000 : 700, 26);
        }
      } else api.update(run);
      E.canvas.setAttribute('aria-label', `Run ${run.run_id || ''}: ${list(run.tasks).length} tasks, ${st.mode === 'plan' ? 'planned waves' : 'execution timeline'}`);
      retarget();
      syncEvents();
    },
    update(run) {
      if (st.view !== 'run' || !run || !st.run || run.key !== st.run.key) return;
      if (RM.structureKey(run) !== st.structure) { api.showRun(run); return; }
      st.run = run; st.replayState = st.replay !== null ? RM.stateAt(run, st.replay) : null;
      st.plan.finish = RM.finishState(run);
      const byId = new Map(list(run.tasks).map(t => [t.id, t]));
      for (const el of EL.values()) {
        if (el.kind === 'task') {
          const t = byId.get(el.data.task.id); if (!t) continue;
          el.data.task = t; setStatus(el, taskStatus(t)); halo(el);
          const rows = list(t.attempts).map(a => a.status);
          if (st.replay === null && t.status === 'RUNNING') rows.push('RUNNING');
          if (rows.length > el.ledCount) { /* more tries than lamps: the newest show */ }
          setLeds(el, rows.slice(-el.ledCount));
          const sig = JSON.stringify([t.status, t.findings, t.gate, list(t.shadow).map(s => s.agreement), t.dwarf, t.accepted_by, el.status]);
          if (sig !== el.sig) { el.sig = sig; redraw(el); }
          if (el.inner && RM.innerKey(t) !== el.innerKey) { closeInner(el); buildInner(el); st.rebuilds++; }
          if (el.kids) for (const k of el.kids) { const np = RM.taskParts(t).parts.find(p => p.id === k.data.part.id); if (np) { k.data.part = np; setStatus(k, np.planned ? 'PENDING' : np.status); redraw(k); } }
        } else if (el.kind === 'finish') {
          setStatus(el, finishStatus(el.id, finishInfo(el.id))); halo(el); finishLeds(el);
          const sig = JSON.stringify([finishInfo(el.id), el.status]); if (sig !== el.sig) { el.sig = sig; redraw(el); }
        }
      }
      const X = RM.execModel(run, { now: nowSec(), ghosts: st.overlay.ghosts, scale: st.exec && { t0: st.exec.t0, t1: st.exec.t1 } });
      // the hidden view is rebuilt when it is next shown, not on every poll that changes its shape
      if (execSig(X) !== st.execSig) { if (st.mode === 'exec' || st.fade) rebuildExec(); else st.execDirty = true; }
      else {
        st.exec.items = X.items; st.exec.extra = X.extra; st.exec.live = X.live;
        const byItem = new Map(X.items.concat(X.extra).map(i => [i.id, i]));
        for (const el of EL.values()) if (el.data && el.data.item) {
          const it = byItem.get(el.data.item.id); if (!it) continue;
          const grew = Math.abs((it.end - it.start) - (el.data.item.end - el.data.item.start)) * X.k > 4;
          el.data.item = it; setStatus(el, st.replay !== null ? replayItemStatus(it) : it.status); halo(el);
          const sig = JSON.stringify([it.status, it.segs.map(s => [s.status, Math.round(s.end || 0)]), it.role]);
          if (grew) { rebuildItem(el, it); } else if (sig !== el.sig) { el.sig = sig; redraw(el); }
        }
      }
      computeCritical();
      retarget();
      syncEvents();
    },
    setMode(mode) {
      if (mode === st.mode || st.view !== 'run') { st.mode = mode; return; }
      const from = st.mode; st.mode = mode;
      if (mode === 'exec' && st.execDirty) { st.execDirty = false; rebuildExec(); }
      // where each task travels: its plan block ↔ its latest attempt on the time axis
      const off = new Map();
      for (const el of EL.values()) {
        if (el.kind !== 'task') continue;
        const its = [...EL.values()].filter(x => x.data && x.data.item && x.data.item.task === el.data.task.id);
        const last = its.sort((a, b) => a.data.item.start - b.data.item.start).pop(); if (!last) continue;
        const dx = (last.n.x + last.n.w / 2) - (el.n.x + el.n.w / 2), dy = (last.n.y + last.n.d / 2) - (el.n.y + el.n.d / 2);
        off.set(el.id, W(dx, dy, 0));
        for (const x of its) off.set(x.id, W(-dx, -dy, 0));
      }
      st.planG.visible = st.execG.visible = true;
      for (const el of EL.values()) if (el.kind === 'task') { const pr = PROG.get(el.id); if (pr) pr.t = 0; }
      st.fade = reduce ? null : { t0: performance.now(), dur: 900, from, off };
      if (reduce) { st.planG.visible = mode === 'plan'; st.execG.visible = mode === 'exec'; }
      st.plan.plateG.visible = mode === 'plan'; st.exec.plateG.visible = mode === 'exec';
      retarget();
      if (st.sel) frameSelection(900); else E.frameBox(boxOf(mode), 900, 26);
      E.invalidate(950);
    },
    // RunFindings of the run shown (GET /api/runs/<key>/findings): a mark per finding
    setFindings(data) { if ((data || null) === st.findings) return; st.findings = data || null; syncBeacons(); E.invalidate(200); },
    // {cables, beads, lamps, roof, labels, beacons, finish, districts, halos}: what the stage draws
    setLayers(L) { Object.assign(st.layers, L || {}); applyLayers(); if ('beacons' in (L || {}) || 'finish' in (L || {})) syncBeacons(); retarget(); },
    get layers() { return { ...st.layers }; },
    setOverlay(o) { Object.assign(st.overlay, o || {}); if (st.view === 'run') { if ('ghosts' in (o || {})) rebuildExec(); computeCritical(); for (const el of EL.values()) redraw(el); } retarget(); },
    setReplay(t) {
      const from = st.replay;
      st.replay = t === null || t === undefined ? null : Number(t);
      if (st.view !== 'run') return;
      E.invalidate(100);                                    // marks a replay reaches drop in
      st.replayState = st.replay !== null ? RM.stateAt(st.run, st.replay) : null;
      for (const el of EL.values()) {
        if (el.kind === 'task') setStatus(el, taskStatus(el.data.task));
        else if (el.kind === 'finish') setStatus(el, finishStatus(el.id, finishInfo(el.id)));
        else if (el.data && el.data.item) setStatus(el, st.replay !== null ? replayItemStatus(el.data.item) : el.data.item.status);
        else continue;
        halo(el); if (el.kind === 'finish') finishLeds(el);
        const sig = 'replay:' + el.status; if (sig !== el.sig) { el.sig = sig; redraw(el); }
      }
      placeNow(); retarget();
      // playing forward: what the replay just passed happens again; a jump across the run plays nothing
      const R = st.run && RM.timeRange(st.run, nowSec());
      if (st.replay === null) evAt = null;
      else syncEvents({ replayFrom: from !== null && R && st.replay - from <= .1 * Math.max(1, R.end - R.start) ? from : null });
    },
    select(s, { frame = true } = {}) {
      const same = JSON.stringify(s || null) === JSON.stringify(st.sel || null);
      st.sel = s || null; retarget();
      if (frame && !same && st.view === 'run') frameSelection();
    },
    // runs: every RunSummary; opts: {filter, layout, shape, sel, focus} (see cloud3d.js)
    showCloud(runs, opts = {}) {
      if (st.view !== 'cloud') { clearWorld(); st.view = 'cloud'; st.run = null; st.sel = null; st.atlasSig = ''; st.structure = ''; }
      if (!cloud) cloud = createCloud(E, host, cb.cloud || {});
      const c = cloud.show(list(runs), opts); syncCtl();
      E.canvas.setAttribute('aria-label', `Cloud of runs: ${c.visible} of ${c.total} runs in ${c.projects} projects, ${c.layout} layout`);
      return c;
    },
    get cloud() { return cloud; },
    // a task's Fractal tree (TaskFractal, or null), drawn over the task while it is open
    setFractal(taskId, data) {
      if (data) FRACTAL.set(taskId, data); else FRACTAL.delete(taskId);
      const el = EL.get('t:' + taskId); if (el && el.inner) { syncFractal(el); E.invalidate(300); }
    },
    focus() { E.canvas.focus({ preventScroll: true }); },
    // frame again what is shown, in the part of the stage the modules leave free (after a module opens, folds or resizes)
    reframe(dur = 500) {
      if (st.view === 'run' && st.run) { if (st.sel) frameSelection(dur); else E.frameBox(boxOf(st.mode), dur, 26); }
      else if (st.view === 'atlas' && st.atlasBox) E.frameBox(st.atlasBox, dur, 30);
      else if (st.view === 'cloud' && cloud && cloud.frameAll) cloud.frameAll();
    },
    get mode() { return st.mode; },
    get view() { return st.view; },
    dispose() { if (CTL) CTL.dispose(); E.dispose(); tip.remove(); },
    debug() {
      return {
        view: st.view, mode: st.mode, rebuilds: st.rebuilds, sel: st.sel, prog: [...PROG].map(([id, p]) => [id, p.p, p.t]), fade: !!st.fade,
        nodes: [...EL.values()].map(el => ({ id: el.id, kind: el.kind, status: el.status, sel: el.tgt.sel, vis: el.tgt.vis })),
        screenPos(id) { const el = EL.get(id); if (!el) return null; const r = E.canvas.getBoundingClientRect(); const p = E.project(el.n.x + el.n.w / 2, el.n.y + el.n.d / 2, el.n.h); return { x: r.left + p.x, y: r.top + p.y, on: p.on }; },
        settle() { for (const el of EL.values()) Object.assign(el.cur, el.tgt); for (const pr of PROG.values()) pr.p = pr.t; E.invalidate(); },
        beaconScale: () => ({ unit: BEACONS.unit, ppu: E.pxPerUnit() }),
        projection: () => ({ proj, ortho: E.isOrtho() }),
        pickBeacon: (x, y) => { const b = BEACONS.pick(x, y); return b ? b.key : null; },
        // the finding marks drawn now, each with where it is on the screen
        beacons() {
          const r = E.canvas.getBoundingClientRect();
          return BEACONS.items.map(it => { if (!it.anchor() || it.hidden || !it.head) return { key: it.key, id: it.data.finding.id, shown: false };
            const p = E.project(...it.head); return { key: it.key, id: it.data.finding.id, sev: it.sev, shown: true, x: r.left + p.x, y: r.top + p.y, on: p.on }; });
        },
      };
    },
  };
  function replayItemStatus(it) {
    const t = st.replay; if (it.ghost) return 'PENDING';
    if (t < it.start) return 'PENDING';
    if (t < it.end && (it.running || it.end > t)) return 'RUNNING';
    return it.status;
  }
  function rebuildItem(el, it) {
    const g = el.g;
    EL.delete(el.id); PICK = PICK.filter(o => o.userData.el !== el.id);
    E.disposeGroup(el.root);
    const X = st.exec, k = X.k;
    it.x = X.x(it.start); it.w = Math.max(22, (it.end - it.start) * k);
    const nel = buildAttempt(it, g);
    Object.assign(nel.cur, el.cur);
  }
  CTL = createControls(host, {
    view: () => st.view, fit: () => frameShown(650), zoom: k => E.zoomBy(k),
    get projection() { return proj; }, setProjection,
    reset: () => setProjection('persp', 700, ISO_DIR),
    legend: legendModel,
    toggleRoute: () => { api.setLayers({ route: !st.layers.route }); if (cb.onLayers) cb.onLayers({ ...st.layers }); },
  });
  return api;
}
