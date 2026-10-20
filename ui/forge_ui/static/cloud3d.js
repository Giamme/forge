/* forge-ui run cloud: every run floating in 3D, grouped by project, in one of five layouts (cloudmodel.js),
   drawn as iso-glow blocks or as orbs. Runs can be dragged anywhere; where they are dropped is kept in this
   browser, per layout. Hosted by the stage's engine (iso.js), so the cloud shares its WebGL context with the
   map and the run views. Blocks and labels follow archivision's figure.js (MIT, © 2026 Giamme; drawing style
   iso-glow, MIT © 2026 April Zhu and Tolga Cohce: see the notice in iso.js). All text is drawn on canvases
   or set with textContent. */
const STATE_STATUS = { live: 'RUNNING', failed: 'FAIL', done: 'MERGED', pending: 'PENDING', stale: 'INTERRUPTED' };
const STORE_KEY = 'forge-ui-cloud-layout-v1';
const STORE_CAP = 5000;
const LABEL_W = 236, LABEL_H = 48;                          // run labels, in pixels

export function createCloud(E, host, cb = {}) {
  const { THREE, W, TS } = E;
  const CM = window.ForgeCloudModel, RM = window.ForgeRunModel;
  const reduce = E.reduceMotion;
  const root = new THREE.Group(), frameG = new THREE.Group(), runG = new THREE.Group(), labelG = new THREE.Group();
  root.add(frameG, runG, labelG);
  const tip = document.createElement('div');
  tip.className = 'stage-tip'; tip.hidden = true; tip.setAttribute('role', 'tooltip'); host.append(tip);
  const TMP = new THREE.Color(), TMP2 = new THREE.Color(), V = new THREE.Vector3(), V2 = new THREE.Vector3();

  const st = { runs: [], facts: new Map(), visible: [], filter: CM.emptyFilter(), layout: 'helix', shape: 'blocks', L: null, sel: null,
    hover: null, focus: null, sig: '', framed: null, attached: false, buildMs: 0, labels: [], drag: null };
  const EL = new Map();                                     // key → element (blocks) or orb slot (orbs)
  let PICK = [], LABELPICK = [], orbs = null;
  const empty = g => { for (const ch of [...g.children]) E.disposeGroup(ch); };
  const nowSec = () => Date.now() / 1000;
  const store = loadStore();

  /* ---------- the dragged positions, in this browser ---------- */
  function loadStore() {
    try { const v = JSON.parse(localStorage.getItem(STORE_KEY) || 'null'); if (v && typeof v === 'object' && v.layouts) return v; } catch (_) { /* private mode or junk */ }
    return { layouts: {} };
  }
  function saveStore() {
    for (const name of Object.keys(store.layouts)) {
      const e = Object.entries(store.layouts[name]);
      if (e.length > STORE_CAP) store.layouts[name] = Object.fromEntries(e.sort((a, b) => (b[1][3] || 0) - (a[1][3] || 0)).slice(0, STORE_CAP));
    }
    try { localStorage.setItem(STORE_KEY, JSON.stringify(store)); } catch (_) { /* full or private: positions last this session */ }
  }
  const pinned = () => { const out = {}; for (const [k, v] of Object.entries(store.layouts[st.layout] || {})) out[k] = v.slice(0, 3); return out; };

  /* ---------- colours ---------- */
  const statusOf = f => STATE_STATUS[f.state] || 'PENDING';
  const lampStatuses = f => {
    const ts = (f.run.task_statuses || []).map(t => t.status);
    if (ts.length <= 64) return ts;
    const out = []; for (let i = 0; i < 64; i++) out.push(ts[Math.floor(i * ts.length / 64)]);   // binned by position
    return out;
  };

  /* ---------- blocks: one shared solid per size, per-run materials ---------- */
  function buildBlock(f) {
    const b = CM.blockSize(f.bucket), unit = E.unitSolid('cloud:' + f.bucket, b.s, b.s, b.h, 9);
    const g = new THREE.Group(); runG.add(g);
    const M = E.mats();
    const body = E.solidFrom(unit, M, g, { cloud: f.key }); PICK.push(body);
    // the roof: a tint in the run's colour and a lamp per task, one mesh coloured per vertex (RGBA)
    const st0 = lampStatuses(f), n = st0.length, per = Math.max(1, Math.ceil(Math.sqrt(n))), cell = (b.s - 26) / per, sz = Math.min(5, cell * .32);
    const quad = (cx, cy, hx, hy, z) => [[-hx, -hy], [-hx, hy], [hx, hy], [-hx, -hy], [hx, hy], [hx, -hy]].flatMap(([a, c]) => W(cx + a, cy + c, z).toArray());
    const pos = quad(0, 0, b.s / 2 - 3, b.s / 2 - 3, b.h + .08);
    for (let i = 0; i < n; i++) pos.push(...quad(-b.s / 2 + 13 + cell * (i % per + .5), -b.s / 2 + 13 + cell * (Math.floor(i / per) + .5), sz, sz, b.h + .6));
    const lg = new THREE.BufferGeometry();
    lg.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3)); lg.setAttribute('color', new THREE.Float32BufferAttribute(new Float32Array(pos.length / 3 * 4).fill(.5), 4));
    const lampMat = new THREE.MeshBasicMaterial({ vertexColors: true, toneMapped: false, transparent: true, depthWrite: false, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -12 });
    const lamps = E.noRay(new THREE.Mesh(lg, lampMat)); lamps.renderOrder = 2; g.add(lamps); M.labs.push(lampMat);
    const haloMat = E.softMat(0xffffff, true);
    const halo = E.noRay(new THREE.Mesh(new THREE.PlaneGeometry((b.s + 90) * TS, (b.s + 90) * TS), haloMat)); halo.rotation.x = -Math.PI / 2; halo.position.y = .3 * TS; halo.visible = false; g.add(halo);
    const el = { key: f.key, f, g, M, body, lamps, lampGeo: lg, lampN: n, halo, haloMat, size: b, cur: null, tgt: null, sc: 0, tgtSc: 1, roofSig: '',
      c: { vis: 1, hot: 0, hov: 0 }, t: { vis: 1, hot: 0, hov: 0 }, sig: '' };
    setLamps(el); return el;
  }
  function setLamps(el) {
    const a = el.lampGeo.attributes.color, ss = lampStatuses(el.f);
    for (let i = 0; i < el.lampN; i++) { const c = ss[i] ? E.statusColor(ss[i]) : E.DETAIL; for (let k = 0; k < 6; k++) a.setXYZW(6 + i * 6 + k, c.r, c.g, c.b, 1); }
    a.needsUpdate = true; el.roofSig = '';
  }
  function setRoof(el, col, alpha) {
    const sig = col.getHexString() + alpha.toFixed(3); if (sig === el.roofSig) return;
    const a = el.lampGeo.attributes.color; el.roofSig = sig;
    for (let k = 0; k < 6; k++) a.setXYZW(k, col.r, col.g, col.b, alpha);
    a.needsUpdate = true;
  }
  function paintBlock(el, pulse) {
    const c = el.c, col = E.statusColor(statusOf(el.f));
    TMP.copy(col).lerp(E.accent, c.hot > .5 ? c.hot : 0);
    const lit = statusOf(el.f) === 'PENDING' ? 0 : .3;
    E.paint(el.M, c.vis, Math.min(1, lit + c.hot * .9), c.hov, 1, 1, TMP, 0);
    el.M.body.color.lerp(col, (E.theme === 'light' ? .1 : .12) * c.vis);
    setRoof(el, col, Math.round((E.theme === 'light' ? .14 : .2) * c.vis * 50) / 50);
    el.haloMat.uniforms.color.value.copy(col);
    const glow = (el.f.state === 'live' ? .14 + .12 * pulse : 0) * c.vis + (c.hot > .5 ? .12 : 0);
    el.haloMat.uniforms.opacity.value = glow; el.halo.visible = glow > .002;   // no draw for a halo that is off
  }

  /* ---------- orbs: one instanced sphere, an instanced outline and an instanced ring of task segments ---------- */
  const SEG_W = 64;
  function buildOrbs(facts) {
    const n = Math.max(1, facts.length), sphere = new THREE.SphereGeometry(1, 28, 18);
    const body = new THREE.InstancedMesh(sphere, new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: .55, metalness: .05 }), n);
    const out = new THREE.InstancedMesh(sphere, E.outlineInstanced(), n); E.noRay(out);
    const ringGeo = new THREE.PlaneGeometry(2, 2);
    const seg = new THREE.InstancedBufferAttribute(new Float32Array(n * 2), 2); ringGeo.setAttribute('aSeg', seg);
    const data = new Uint8Array(SEG_W * n * 4), tex = new THREE.DataTexture(data, SEG_W, n, THREE.RGBAFormat);
    tex.colorSpace = THREE.SRGBColorSpace; tex.needsUpdate = true;
    const ringMat = new THREE.ShaderMaterial({
      uniforms: { tSeg: { value: tex }, rows: { value: n }, opacity: { value: 1 } },
      transparent: true, depthWrite: false, toneMapped: false,
      vertexShader: `attribute vec2 aSeg; varying vec2 vUv; varying vec2 vSeg;
        void main() {
          mat4 m = modelViewMatrix;
          #ifdef USE_INSTANCING
            m = m * instanceMatrix;
          #endif
          vec4 c = m * vec4(0.0, 0.0, 0.0, 1.0);
          float s = length(m[0].xyz);
          c.xy += position.xy * s * 1.38;
          gl_Position = projectionMatrix * c; vUv = uv; vSeg = aSeg;
        }`,
      fragmentShader: `uniform sampler2D tSeg; uniform float rows; uniform float opacity; varying vec2 vUv; varying vec2 vSeg;
        void main() {
          vec2 p = vUv * 2.0 - 1.0; float r = length(p);
          if (r < 0.84 || r > 1.0) discard;
          float n = max(vSeg.y, 1.0), a = fract(atan(p.y, p.x) / 6.2831853 + 0.25);
          float idx = floor(a * n), fr = fract(a * n);
          if (n > 1.0 && (fr < 0.07 || fr > 0.93)) discard;
          vec4 col = texture2D(tSeg, vec2((idx + 0.5) / ${SEG_W}.0, (vSeg.x + 0.5) / rows));
          if (col.a < 0.01) discard;
          gl_FragColor = vec4(col.rgb, opacity * col.a);
        }`,
    });
    const ring = E.noRay(new THREE.InstancedMesh(ringGeo, ringMat, n));
    for (const m of [body, out, ring]) { m.frustumCulled = false; m.count = facts.length; runG.add(m); }
    body.userData.orbs = true; PICK.push(body);
    const glow = E.glowPoints(facts.map(() => new THREE.Vector3()), .9); runG.add(glow);
    orbs = { body, out, ring, seg, tex, data, glow, keys: facts.map(f => f.key), index: new Map(facts.map((f, i) => [f.key, i])) };
    facts.forEach((f, i) => {
      body.setColorAt(i, E.bodyMat.color);
      seg.setXY(i, i, Math.min(SEG_W, lampStatuses(f).length));
      const el = { key: f.key, f, orb: i, size: { s: CM.orbRadius(f.bucket) * 2, h: CM.orbRadius(f.bucket) * 2, r: CM.orbRadius(f.bucket) }, cur: null, tgt: null, sc: 0, tgtSc: 1,
        c: { vis: 1, hot: 0, hov: 0 }, t: { vis: 1, hot: 0, hov: 0 }, sig: '' };
      EL.set(f.key, el); setOrbSegments(el);
    });
    seg.needsUpdate = true;
  }
  function setOrbSegments(el) {
    const ss = lampStatuses(el.f), row = el.orb;
    for (let i = 0; i < SEG_W; i++) {
      const o = (row * SEG_W + i) * 4, s = ss[i];
      if (!s) { orbs.data[o + 3] = 0; continue; }
      const c = TMP2.copy(E.statusColor(s)).convertLinearToSRGB();          // the texture is sRGB: bytes are encoded
      orbs.data[o] = c.r * 255; orbs.data[o + 1] = c.g * 255; orbs.data[o + 2] = c.b * 255; orbs.data[o + 3] = 255;
    }
    orbs.tex.needsUpdate = true;
  }
  const MAT = new THREE.Matrix4(), Q = new THREE.Quaternion(), S = new THREE.Vector3();
  function placeOrb(el) {
    const r = el.size.r, s = Math.max(1e-4, r * TS * el.sc);
    V.copy(W(el.cur[0], el.cur[1], el.cur[2] + r)); S.setScalar(s);
    MAT.compose(V, Q, S);
    for (const m of [orbs.body, orbs.out, orbs.ring]) m.setMatrixAt(el.orb, MAT);
    const gp = orbs.glow.geometry.attributes.position;               // a beacon over live runs; others parked out of sight
    if (el.f.state === 'live' && !el.leaving) gp.setXYZ(el.orb, V.x, V.y + s * 1.1, V.z); else gp.setXYZ(el.orb, 0, -1e7, 0);
    gp.needsUpdate = true;
    orbs.dirty = true;
  }
  function paintOrb(el, pulse) {
    const c = el.c, col = E.statusColor(statusOf(el.f));
    TMP2.copy(E.bodyMat.color).lerp(col, E.theme === 'light' ? .45 : .55);
    E.mixColor(TMP, TMP2, c.vis, c.hot * .8, c.hov);
    orbs.body.setColorAt(el.orb, TMP);
    orbs.colorDirty = true;
    if (el.f.state === 'live') orbs.live = true;
  }

  /* ---------- labels: world sprites for the runs that matter right now, screen sprites per project ---------- */
  const LABELS = new Map();                                 // key → sprite record (LRU by insertion)
  function runLabel(el) {
    let t = LABELS.get(el.key);
    if (t) { LABELS.delete(el.key); LABELS.set(el.key, t); return t; }
    const key = el.key, w = LABEL_W, h = LABEL_H;
    t = E.sprite(w, h, 2, (x, s, C) => {
      const f = (EL.get(key) || el).f, r = f.run;
      E.rr(x, s, 1, 1, w - 2, h - 2, 10); x.fillStyle = C.light ? 'rgba(250,250,249,.92)' : 'rgba(22,23,24,.9)'; x.fill();
      x.strokeStyle = E.statusHex(statusOf(f)); x.lineWidth = Math.max(1, 1.2 * s); x.stroke();
      const g = E.fit(String(r.goal || 'Untitled run').split('\n')[0], w - 20, 26, 12.5, 2, 9);
      if (g) E.lines(x, s, g, 10, 5, C.inkHi, w - 20, 600);
      E.font(x, 9.5, s); x.fillStyle = C.ink;
      x.fillText(E.ellipsis(`${r.run_id || ''} · ${f.n} task${f.n > 1 ? 's' : ''} · ${(STATE_STATUS[f.state] || '').toLowerCase()}`, Math.floor((w - 20) / (9.5 * E.CW))), 10 * s, (h - 8) * s);
    }, { screen: true, px: h });
    t.mesh.center.set(.5, -.12); t.mat.depthTest = false;
    labelG.add(t.mesh); LABELS.set(el.key, t);
    while (LABELS.size > 96) {
      const [k, old] = LABELS.entries().next().value;
      if (st.labels.includes(k)) { LABELS.delete(k); LABELS.set(k, old); if ([...LABELS.keys()].every(x => st.labels.includes(x))) break; continue; }
      LABELS.delete(k); E.disposeGroup(old.mesh);
    }
    return t;
  }
  let projectLabels = [];
  function buildFrames() {
    empty(frameG);
    for (const t of projectLabels) E.disposeGroup(t.mesh);
    projectLabels = []; LABELPICK = [];
    const L = st.L; if (!L) return;
    const names = new Map(L.projects.map(p => [p.id, p]));
    // a faint floor grid for the layouts that stand on one
    if (st.layout !== 'constellation') {
      const [x0, y0, , x1, y1] = L.bounds, step = 400, segs = [];
      for (let x = Math.floor(x0 / step) * step; x <= x1; x += step) segs.push([[x, y0, -.5], [x, y1, -.5]]);
      for (let y = Math.floor(y0 / step) * step; y <= y1; y += step) segs.push([[x0, y, -.5], [x1, y, -.5]]);
      if (segs.length < 400) { const gm = E.lmat(); gm.color.copy(E.lineMat.color); gm.opacity = .35; gm.transparent = true; E.seg(segs, frameG, gm); }
    }
    for (const c of L.clusters) {
      const p = names.get(c.pid); if (!p) continue;
      const mat = E.lmat({ dashed: c.shape === 'ring' || c.shape === 'shell', dashSize: 14 * TS, gapSize: 10 * TS });
      if (c.shape === 'lane') E.loopAt(c.lane[0], c.lane[1], c.lane[2], c.lane[3], 18, .4, mat, frameG, 8);
      else if (c.shape === 'cloud') {
        const mine = st.visible.filter(f => f.project === c.pid), hOf = new Map(mine.map(f => [f.key, shapeSize(f).h / 2]));
        const segs = CM.constellationLinks(mine, st.L.pos).map(([a, b]) => { const A = st.L.pos[a], B = st.L.pos[b]; return [[A[0], A[1], A[2] + hOf.get(a)], [B[0], B[1], B[2] + hOf.get(b)]]; });
        if (segs.length) E.seg(segs, frameG, mat);
      } else {
        const pts = Array.from({ length: 96 }, (_, i) => { const a = i / 96 * Math.PI * 2; return W(c.center[0] + c.radius * Math.cos(a), c.center[1] + c.radius * Math.sin(a), .4); });
        const loop = E.noRay(E.loopL(pts, mat)); loop.computeLineDistances(); frameG.add(loop);
      }
      c.mat = mat;
      const count = st.visible.filter(f => f.project === c.pid).length, text = `${p.name.toUpperCase()}  ${count}`;
      const w = text.length * 9 + 26, t = E.sprite(w, 26, 3, (x, s, C) => {
        E.rr(x, s, 1, 1, w - 2, 24, 12); x.fillStyle = C.light ? 'rgba(250,250,249,.92)' : 'rgba(22,23,24,.88)'; x.fill();
        x.strokeStyle = st.focus === c.pid ? C.acc : C.line; x.lineWidth = Math.max(1, s); x.stroke();
        E.font(x, 12, s, 600); x.fillStyle = st.focus === c.pid ? C.accHi : C.inkHi; x.letterSpacing = `${(1.2 * s).toFixed(1)}px`;
        x.fillText(text, 13 * s, 17 * s); x.letterSpacing = '0px';
      }, { screen: true, px: 26 });
      const far = Math.SQRT1_2 * c.radius;
      const top = c.shape === 'lane' ? [c.lane[0] - 40, c.lane[1] + c.lane[3] / 2, 30] : st.layout === 'galaxy' ? [c.center[0] - far, c.center[1] - far, 40] : [c.center[0], c.center[1], (c.z1 || 0) + 120];
      t.mesh.position.copy(W(...top)); t.mesh.userData = { project: c.pid, tp: t }; t.mat.depthTest = false;
      if (c.shape === 'lane') t.mesh.center.set(1, .5);
      labelG.add(t.mesh); projectLabels.push(t); LABELPICK.push(t.mesh);
    }
    if (L.axis) {                                            // the time axis runs along the front of the lanes
      const z = -.5, y = Math.max(...L.clusters.filter(c => c.lane).map(c => c.lane[1] + c.lane[3])) + 40;
      E.seg([[[0, y, z], [L.axis.length, y, z]], ...L.axis.ticks.map(k => [[k.x, y - 20, z], [k.x, y + 20, z]])], frameG, E.majorMat);
      for (const k of L.axis.ticks) {
        const d = new Date(k.t * 1000), text = L.axis.step < 86400 ? d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
        const w = text.length * 8 + 14, t = E.sprite(w, 22, 3, (x, s, C) => { E.font(x, 11, s); x.fillStyle = C.ink; x.fillText(text, 7 * s, 15 * s); }, { screen: true, px: 20 });
        t.mesh.position.copy(W(k.x, y + 70, z)); t.mat.depthTest = false; labelG.add(t.mesh); projectLabels.push(t);
      }
    }
  }

  /* ---------- building and laying out ---------- */
  function clearRuns() {
    for (const t of LABELS.values()) E.disposeGroup(t.mesh);
    LABELS.clear(); st.labels = [];
    empty(runG);
    EL.clear(); PICK = []; orbs = null;
  }
  function rebuild() {
    const t0 = performance.now(), was = new Map([...EL].map(([k, el]) => [k, { cur: el.cur, sc: el.sc }]));
    clearRuns();
    if (st.shape === 'orbs') buildOrbs(st.visible);
    else for (const f of st.visible) EL.set(f.key, buildBlock(f));
    for (const el of EL.values()) {                         // runs already on screen travel from where they were
      const w = was.get(el.key);
      el.tgt = (st.L.pos[el.key] || [0, 0, 0]).slice(); el.cur = w && w.cur && !reduce ? w.cur.slice() : el.tgt.slice();
      el.sc = reduce ? 1 : w ? w.sc : 0; place(el);
    }
    buildFrames(); retarget();
    st.buildMs = performance.now() - t0;
    if (orbs) { orbs.body.computeBoundingSphere(); orbs.body.instanceColor && (orbs.body.instanceColor.needsUpdate = true); }
  }
  function place(el) {
    if (el.orb !== undefined) { placeOrb(el); return; }
    el.g.position.copy(W(...el.cur)); el.g.scale.setScalar(Math.max(1e-4, el.sc));
  }
  function relayout({ frame = false } = {}) {
    const all = [...st.facts.values()];
    st.visible = all.filter(f => CM.match(f, st.filter, nowSec()));
    // a poll that only changes statuses keeps the layout (the constellation's simulation is not free)
    const pin = pinned(), vkey = CM.layoutKey(st.visible), lsig = [st.layout, vkey, all.length === st.visible.length ? '' : CM.layoutKey(all), JSON.stringify(pin)].join('|');
    const moved = lsig !== st.lsig || !st.L;
    if (moved) { st.L = CM.layout(st.layout, st.visible, { all, pinned: pin }); st.lsig = lsig; }
    const sig = st.shape + '|' + vkey;
    const shapeChanged = !st.sig || st.sig.split('|')[0] !== st.shape;
    if (shapeChanged || st.shape === 'orbs' && sig !== st.sig) { st.sig = sig; rebuild(); }
    else {
      st.sig = sig;
      const want = new Set(st.visible.map(f => f.key));
      for (const [k, el] of [...EL]) if (!want.has(k)) { el.tgtSc = 0; el.leaving = true; }
      for (const f of st.visible) {
        let el = EL.get(f.key);
        if (el && el.f.bucket !== f.bucket) { removeBlock(el); el = null; }
        if (!el) { el = buildBlock(f); el.cur = (st.L.pos[f.key] || [0, 0, 0]).slice(); el.sc = reduce ? 1 : 0; EL.set(f.key, el); }
        el.f = f; el.leaving = false; el.tgtSc = 1; el.tgt = (st.L.pos[f.key] || el.cur).slice();
        if (reduce) { el.cur = el.tgt.slice(); el.sc = 1; }
        place(el);
      }
      if (moved) buildFrames();
      retarget();
    }
    if (frame) frameAll(reduce ? 0 : 1100);
    E.invalidate(1300);
  }
  function removeBlock(el) {
    EL.delete(el.key); PICK = PICK.filter(o => o !== el.body);
    const t = LABELS.get(el.key); if (t) { LABELS.delete(el.key); E.disposeGroup(t.mesh); }
    E.disposeGroup(el.g);
  }
  function retarget() {
    for (const el of EL.values()) {
      el.t.vis = st.focus && el.f.project !== st.focus ? .2 : 1;
      el.t.hot = st.sel === el.key || (st.drag && st.drag.key === el.key) ? 1 : 0;
      el.t.hov = st.hover === el.key ? 1 : 0;
    }
    for (const c of st.L ? st.L.clusters : []) if (c.mat) { E.lineCol(c.mat, E.majorMat.color, st.focus === c.pid ? 1 : 0, st.focus && st.focus !== c.pid ? .3 : 1); c.mat.color.lerp(E.INKHI, .2); }
    updateLabels(true);
    E.invalidate(400);
  }
  // camera: where each layout reads best, framing what is shown (or one project)
  function viewDir() {
    const info = CM.LAYOUT_INFO[st.layout] || {}, e = THREE.MathUtils.degToRad(info.elevation ?? 25), a = THREE.MathUtils.degToRad(info.azimuth ?? 45);
    return new THREE.Vector3(Math.cos(e) * Math.cos(a), Math.sin(e), Math.cos(e) * Math.sin(a));
  }
  // lane labels hang left of the timeline's lanes: room for them too
  function frameBox(bx, dur, dir) { E.frameBox(bx, dur, -89, dir || null, st.layout === 'timeline' ? { ...(st.inset || {}), l: ((st.inset || {}).l || 0) + 170 } : st.inset); }
  // the points that must be in view: each run's footprint and top, and each cluster's ring and label
  function framePts(pid) {
    const pts = [];
    for (const f of st.visible) {
      if (pid && f.project !== pid) continue;
      const p = st.L.pos[f.key]; if (!p) continue;
      const b = shapeSize(f), r = b.s / 2;
      pts.push([p[0] - r, p[1], p[2]], [p[0] + r, p[1], p[2] + b.h], [p[0], p[1] - r, p[2]], [p[0], p[1] + r, p[2] + b.h]);
    }
    for (const c of st.L.clusters) {
      if (pid && c.pid !== pid) continue;
      if (c.shape === 'lane') pts.push([c.lane[0], c.lane[1], 0], [c.lane[0] + c.lane[2], c.lane[1] + c.lane[3], 0], [c.lane[0] - 160, c.lane[1] + c.lane[3] / 2, 30]);
      else {
        for (let i = 0; i < 8; i++) { const a = i / 8 * Math.PI * 2; pts.push([c.center[0] + c.radius * Math.cos(a), c.center[1] + c.radius * Math.sin(a), 0]); }
        pts.push([c.center[0], c.center[1], (c.z1 || 0) + 160]);
      }
    }
    return pts;
  }
  const shapeSize = f => st.shape === 'orbs' ? { s: CM.orbRadius(f.bucket) * 2, h: CM.orbRadius(f.bucket) * 2 } : CM.blockSize(f.bucket);
  function frameAll(dur = 900) {
    if (!st.L) return;
    if (st.focus && st.L.clusters.some(x => x.pid === st.focus)) { frameCluster(st.focus, dur); return; }
    const pts = framePts(null);
    if (pts.length) frameBox({ pts }, dur, viewDir()); else { const b = st.L.bounds; frameBox([b[0], b[1], b[2], b[3], b[4], b[5]], dur, viewDir()); }
  }
  function frameCluster(pid, dur = 900) { const pts = framePts(pid); if (pts.length) frameBox({ pts }, dur); }

  /* ---------- every frame: travel, grow, fade, light ---------- */
  let lastPulse = 0;
  function tick(now, dt) {
    if (!st.attached) return false;
    const k = reduce ? 1 : 1 - Math.exp(-6 * dt), kk = reduce ? 1 : Math.min(1, dt * 7);
    let moving = false, live = false;
    const pulse = .5 + .5 * Math.sin(now / 420);
    for (const el of [...EL.values()]) {
      let moved = false;
      if (!st.drag || st.drag.key !== el.key) for (let i = 0; i < 3; i++) { const d = el.tgt[i] - el.cur[i]; if (Math.abs(d) > .05) { el.cur[i] += d * k; moved = true; } else el.cur[i] = el.tgt[i]; }
      const ds = el.tgtSc - el.sc; if (Math.abs(ds) > .002) { el.sc += ds * k; moved = true; } else el.sc = el.tgtSc;
      for (const key of ['vis', 'hot', 'hov']) { const d = el.t[key] - el.c[key]; if (Math.abs(d) > .002) { el.c[key] += d * kk; moving = true; } else el.c[key] = el.t[key]; }
      if (moved) { place(el); moving = true; }
      if (el.leaving && el.sc <= .002) { if (el.orb === undefined) removeBlock(el); else { el.sc = 0; place(el); } continue; }
      if (el.f.state === 'live') live = true;
      if (el.orb === undefined) paintBlock(el, pulse); else paintOrb(el, pulse);
    }
    if (orbs) {
      if (orbs.dirty) { for (const m of [orbs.body, orbs.out, orbs.ring]) m.instanceMatrix.needsUpdate = true; orbs.body.computeBoundingSphere(); orbs.dirty = false; }
      if (orbs.colorDirty && orbs.body.instanceColor) { orbs.body.instanceColor.needsUpdate = true; orbs.colorDirty = false; }
      orbs.out.material.uniforms.color.value.copy(E.outlineMat.uniforms.color.value);
      orbs.glow.material.uniforms.intensity.value = live ? .35 + .35 * pulse : 0;
      orbs.glow.material.uniforms.color.value.copy(E.statusColor('running'));
    }
    if (moving) for (const k2 of st.labels) { const el = EL.get(k2), t = LABELS.get(k2); if (el && t) t.mesh.position.copy(labelAt(el)); }
    if (!moving && live && !reduce) { if (now - lastPulse > 40) { lastPulse = now; setTimeout(() => E.invalidate(), 40); } return false; }
    return moving;
  }

  /* ---------- level of detail: lamps, creases and labels by on-screen size ---------- */
  let lodTimer = 0;
  const labelAt = el => W(el.cur[0], el.cur[1], el.cur[2] + el.size.h * el.sc);
  function pxSize(el) {
    const cam = E.camera, [VW, VH] = E.size;
    V.copy(W(el.cur[0], el.cur[1], el.cur[2] + el.size.h / 2));
    const d = cam.position.distanceTo(V);
    const unit = E.isOrtho() ? VH * cam.zoom / (cam.top - cam.bottom) : VH / (2 * d * Math.tan(THREE.MathUtils.degToRad(cam.fov / 2)));
    return el.size.s * TS * unit * Math.max(.001, el.sc);
  }
  function updateLabels(force) {
    if (!st.attached || !st.L) return;
    const now = performance.now(); if (!force && now - lodTimer < 120) return; lodTimer = now;
    const cands = [];
    for (const el of EL.values()) {
      if (el.leaving) continue;
      const px = pxSize(el);
      if (el.orb === undefined) { el.lamps.visible = px > 18; for (const ch of el.body.children) if (ch.userData.crease) ch.visible = px > 30; }
      const must = el.key === st.sel || el.key === st.hover || (st.drag && st.drag.key === el.key);
      if (!must && (px < 44 || el.c.vis < .5)) continue;
      const p = E.project(el.cur[0], el.cur[1], el.cur[2] + el.size.h); if (!p.on) continue;
      cands.push({ key: el.key, x: p.x, y: p.y - 6, w: LABEL_W + 8, h: LABEL_H + 4, pri: must ? 1e9 : px });
    }
    const keep = new Set(CM.labelPick(cands, { max: 16 }));
    st.labels = [...keep];
    for (const [k, t] of LABELS) t.mesh.visible = keep.has(k);
    for (const k of keep) { const el = EL.get(k), t = runLabel(el); t.mesh.visible = true; t.mesh.position.copy(labelAt(el)); }
  }
  E.controls.addEventListener('change', () => { if (st.attached) updateLabels(false); });
  E.controls.addEventListener('end', () => { if (st.attached) updateLabels(true); });

  /* ---------- pointer: hover, click, double-click, drag ---------- */
  function hitRun(cx, cy) {
    if (!st.attached) return null;
    const lab = LABELPICK.length ? E.pickHit(cx, cy, LABELPICK) : null;
    const h = E.pickHit(cx, cy, PICK, o => o.visible);
    if (lab && (!h || lab.point.distanceTo(E.camera.position) < h.point.distanceTo(E.camera.position))) return { project: lab.object.userData.project };
    if (!h) return null;
    const key = h.object.userData.orbs ? orbs && orbs.keys[h.instanceId] : h.object.userData.cloud;
    const el = key && EL.get(key);
    return el && !el.leaving ? { key, point: h.point } : null;
  }
  function info(el) {
    const f = el.f, r = f.run, counts = Object.entries(r.counts || {}).map(([s, n]) => `${s.toLowerCase()} ${n}`).join(' · ');
    const proj = st.L && st.L.projects.find(p => p.id === f.project);
    return [['meta', `${proj ? proj.name : RM.basename(r.repo)} · ${r.run_id || ''} · ${r.kind}`], ['title', String(r.goal || 'Untitled run').split('\n')[0]],
      ['body', `${f.n} task${f.n > 1 ? 's' : ''} · ${counts}`], ['body', `${[...f.dwarf].join(', ') || '—'} → ${[...f.qa].join(', ') || '—'}`],
      ['foot', `started ${new Date(f.t * 1000).toLocaleString()}${r.final_review ? ' · review ' + r.final_review : ''}${r.verification_status ? ' · verify ' + r.verification_status : ''}`],
      ['foot', (store.layouts[st.layout] || {})[el.key] ? 'moved by you · drag to move, Shift = depth, Alt = height' : 'drag to move · double-click to open']];
  }
  function showTip(el, cx, cy) {
    tip.replaceChildren();
    for (const [cls, text] of info(el)) { const d = document.createElement('div'); d.className = 'stage-tip-' + cls; d.textContent = text; tip.append(d); }
    tip.hidden = false;
    const r = host.getBoundingClientRect(), tw = tip.offsetWidth || 260, th = tip.offsetHeight || 90;
    let x = cx - r.left + 16, y = cy - r.top + 16;
    if (x + tw > r.width - 8) x = cx - r.left - tw - 16;
    if (y + th > r.height - 8) y = cy - r.top - th - 16;
    tip.style.transform = `translate(${Math.max(4, x).toFixed(0)}px, ${Math.max(4, y).toFixed(0)}px)`;
  }
  const hideTip = () => { tip.hidden = true; };
  let hoverRaf = 0, hoverAt = null;
  const handlers = {
    hover(cx, cy) {
      hoverAt = cx === null ? null : [cx, cy];
      if (hoverRaf) return;
      hoverRaf = requestAnimationFrame(() => {
        hoverRaf = 0;
        const h = hoverAt ? hitRun(hoverAt[0], hoverAt[1]) : null, key = h && h.key || null;
        E.canvas.classList.toggle('pointing', !!h);
        if (key !== st.hover) { st.hover = key; retarget(); }
        if (key && hoverAt) showTip(EL.get(key), hoverAt[0], hoverAt[1]); else hideTip();
      });
    },
    click(cx, cy) {
      hideTip();
      const h = hitRun(cx, cy);
      if (h && h.project) { api.focusProject(st.focus === h.project ? null : h.project); cb.onFocus && cb.onFocus(st.focus); return; }
      api.select(h ? h.key : null); cb.onSelect && cb.onSelect(st.sel);
    },
    dbl(cx, cy) { const h = hitRun(cx, cy); if (h && h.key && cb.onOpenRun) cb.onOpenRun(h.key); },
    grab(cx, cy) {
      const h = hitRun(cx, cy); if (!h || !h.key) return null;
      const el = EL.get(h.key), P = W(...el.cur), n = new THREE.Vector3(); E.camera.getWorldDirection(n);
      const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(n, P);
      const H = E.rayAt(cx, cy).ray.intersectPlane(plane, new THREE.Vector3()) || P.clone();
      st.drag = { key: h.key, plane, off: P.clone().sub(H), P: P.clone(), start: el.cur.slice(), lastY: cy };
      hideTip(); return st.drag;
    },
    drag(cx, cy, e, tok) {
      const el = EL.get(tok.key); if (!el) return;
      const n = new THREE.Vector3(); E.camera.getWorldDirection(n);
      const [, VH] = E.size, dist = E.camera.position.distanceTo(tok.P), dy = (cy - tok.lastY) / VH * 2 * dist;
      if (e.shiftKey) tok.P.addScaledVector(n, -dy);          // Shift: toward or away from you
      else if (e.altKey) tok.P.y -= dy * .5;                  // Alt: straight up or down
      else { const H = E.rayAt(cx, cy).ray.intersectPlane(tok.plane, V2); if (H) tok.P.copy(H).add(tok.off); }
      if (e.shiftKey || e.altKey) {                          // a changed modifier carries on from where the run is now
        tok.plane.setFromNormalAndCoplanarPoint(n, tok.P);
        const H = E.rayAt(cx, cy).ray.intersectPlane(tok.plane, V2); if (H) tok.off.copy(tok.P).sub(H);
      }
      tok.lastY = cy;
      el.cur = [tok.P.x / TS, tok.P.z / TS, Math.max(-5000, tok.P.y / TS)]; el.tgt = el.cur.slice(); place(el);
      const t = LABELS.get(el.key); if (t) t.mesh.position.copy(labelAt(el));
    },
    drop(cx, cy, e, tok, how) {
      const el = EL.get(tok.key);
      st.drag = null;
      if (!el) return;
      if (how && how.cancel) { el.cur = tok.start.slice(); el.tgt = tok.start.slice(); place(el); retarget(); return; }
      if (how && how.moved) {
        (store.layouts[st.layout] = store.layouts[st.layout] || {})[el.key] = [...el.cur.map(v => Math.round(v * 10) / 10), Math.round(nowSec())];
        saveStore();
        if (st.L) { st.L.pos[el.key] = el.cur.slice(); if (st.layout === 'constellation') buildFrames(); }
        cb.onMoved && cb.onMoved(el.key);
      }
      retarget();
    },
    key(e) {
      const k = e.key, cam = E.camera, ctr = E.controls.target;
      if (k === 'ArrowLeft' || k === 'ArrowRight' || k === 'ArrowUp' || k === 'ArrowDown') {
        const sph = new THREE.Spherical().setFromVector3(V.copy(cam.position).sub(ctr));
        if (k === 'ArrowLeft') sph.theta -= .09; if (k === 'ArrowRight') sph.theta += .09;
        if (k === 'ArrowUp') sph.phi = Math.max(.03, sph.phi - .07); if (k === 'ArrowDown') sph.phi = Math.min(Math.PI - .03, sph.phi + .07);
        cam.position.copy(ctr).add(V.setFromSpherical(sph)); cam.lookAt(ctr); return true;
      }
      if (k === '+' || k === '=' || k === '-' || k === '_') { const f = k === '+' || k === '=' ? .85 : 1 / .85; cam.position.sub(ctr).multiplyScalar(f).add(ctr); if (E.isOrtho()) { cam.zoom /= f; cam.updateProjectionMatrix(); } return true; }
      if (k === '0' || k === 'Home') { api.resetView(); return true; }
      if (k === 'f' || k === 'F') { if (st.sel) api.focusRun(st.sel); else frameAll(); return true; }
      if (k === 'p' || k === 'P') {
        const ids = st.L ? st.L.clusters.map(c => c.pid) : []; if (!ids.length) return true;
        const i = ids.indexOf(st.focus), next = i < 0 ? ids[0] : i === ids.length - 1 ? null : ids[i + 1];
        api.focusProject(next); cb.onFocus && cb.onFocus(next); return true;
      }
      if (k === '[' || k === ']') {
        const keys = st.visible.slice().sort((a, b) => a.t - b.t).map(f => f.key); if (!keys.length) return true;
        const i = keys.indexOf(st.sel), d = k === ']' ? 1 : -1, next = keys[i < 0 ? (d > 0 ? 0 : keys.length - 1) : (i + d + keys.length) % keys.length];
        api.select(next); api.focusRun(next); cb.onSelect && cb.onSelect(next); return true;
      }
      if (k === 'Enter' && st.sel) { cb.onOpenRun && cb.onOpenRun(st.sel); return true; }
      if (k === 'l' || k === 'L') { const i = CM.LAYOUTS.indexOf(st.layout); cb.onLayout && cb.onLayout(CM.LAYOUTS[(i + 1) % CM.LAYOUTS.length]); return true; }
      if (k === 'b' || k === 'B') { cb.onShape && cb.onShape(st.shape === 'blocks' ? 'orbs' : 'blocks'); return true; }
      if (k === 'Escape') {
        if (st.sel) { api.select(null); cb.onSelect && cb.onSelect(null); return true; }
        if (st.focus) { api.focusProject(null); cb.onFocus && cb.onFocus(null); return true; }
        return false;
      }
      return false;
    },
  };

  /* ---------- public ---------- */
  const api = {
    handlers,
    // the cloud keeps its runs while another view is shown, and comes back to where its camera was
    attach() {
      if (st.attached) return;
      st.attached = true; E.scene.add(root); E.tickers.add(tick); E.setProfile('free');
      if (st.cam) { E.cancelCam(); E.controls.target.copy(st.cam.t); E.camera.position.copy(st.cam.p); }
      E.invalidate(300);
    },
    detach() {
      if (!st.attached) return;
      st.cam = { p: E.camera.position.clone(), t: E.controls.target.clone() };
      st.attached = false; E.scene.remove(root); E.tickers.delete(tick); hideTip(); E.setProfile('plate'); E.canvas.classList.remove('pointing', 'grabbing');
    },
    // runs: RunSummary list; opts: {filter, layout, shape, sel, focus}
    show(runs, opts = {}) {
      api.attach();
      if (opts.layout && CM.LAYOUTS.includes(opts.layout)) st.layout = opts.layout;
      if (opts.shape === 'blocks' || opts.shape === 'orbs') st.shape = opts.shape;
      if (opts.filter) st.filter = CM.normFilter(opts.filter);
      if (opts.sel !== undefined) st.sel = opts.sel;
      if (opts.focus !== undefined) st.focus = opts.focus;
      if (opts.inset !== undefined) st.inset = opts.inset;
      const first = st.framed === null || st.framed !== st.layout;
      api.update(runs, { frame: first });
      st.framed = st.layout;
      return api.counts();
    },
    update(runs, { frame = false } = {}) {
      st.runs = Array.isArray(runs) ? runs : [];
      const now = nowSec(), prev = st.facts;
      st.facts = new Map(st.runs.map(r => [r.key, CM.runFacts(r, now)]));
      relayout({ frame: frame || (!prev.size && st.facts.size > 0) });     // the first runs to arrive are framed
      // recolour lamps and redraw labels whose text moved (a status change alone does not lay out again)
      for (const el of EL.values()) {
        const f = st.facts.get(el.key); if (!f) continue;
        const sig = JSON.stringify([f.state, (f.run.task_statuses || []).map(t => t.status), f.run.goal]);
        if (sig === el.sig) continue;
        const had = el.sig; el.sig = sig; el.f = f;
        if (el.orb === undefined) setLamps(el); else { setOrbSegments(el); place(el); }
        const t = LABELS.get(el.key); if (t && had && prev.size) t.redraw();
      }
      E.invalidate(400);
      return api.counts();
    },
    setFilter(f) { st.filter = CM.normFilter(f); relayout(); return api.counts(); },
    setLayout(name) { if (!CM.LAYOUTS.includes(name) || name === st.layout) return; st.layout = name; relayout({ frame: true }); st.framed = name; },
    setShape(shape) { if (shape !== 'blocks' && shape !== 'orbs' || shape === st.shape) return; st.shape = shape; st.sig = ''; relayout(); },
    select(key) { st.sel = key && EL.has(key) ? key : null; retarget(); },
    focusProject(pid) { st.focus = pid || null; retarget(); buildFrames(); frameAll(reduce ? 0 : 900); },
    focusRun(key) {
      const el = EL.get(key); if (!el) return;
      const s = el.size.s * 2.2;
      frameBox([el.cur[0] - s, el.cur[1] - s, el.cur[2], el.cur[0] + s, el.cur[1] + s, el.cur[2] + el.size.h * 2], reduce ? 0 : 800);
    },
    resetView() { frameAll(reduce ? 0 : 800); },
    // pixels the page's overlays cover on each side ({l, r, t, b}), kept clear when framing
    setInsets(inset) { st.inset = inset || null; },
    pinnedCount() { return Object.keys(store.layouts[st.layout] || {}).length; },
    isPinned(key) { return !!(store.layouts[st.layout] || {})[key]; },
    resetPositions(scope = 'layout', key = null) {
      const before = JSON.parse(JSON.stringify(store.layouts[st.layout] || {}));
      if (scope === 'run' && key) delete (store.layouts[st.layout] || {})[key];
      else store.layouts[st.layout] = {};
      saveStore(); relayout();
      return () => { store.layouts[st.layout] = before; saveStore(); relayout(); };   // undo
    },
    exportPositions() {
      const labels = {}; for (const r of st.runs) labels[r.key] = r.run_id || '';
      return CM.serializeLayout(store, labels, nowSec());
    },
    importPositions(obj) {
      const res = CM.parseLayoutFile(obj); if (!res.ok) return res;
      for (const [name, entries] of Object.entries(res.layouts)) store.layouts[name] = { ...(store.layouts[name] || {}), ...entries };
      saveStore(); relayout();
      const known = new Set(st.runs.map(r => r.key));
      res.unknown = Object.values(res.layouts).reduce((a, e) => a + Object.keys(e).filter(k => !known.has(k)).length, 0);
      return res;
    },
    counts() { return { visible: st.visible.length, total: st.facts.size, projects: st.L ? st.L.projects.length : 0, layout: st.layout, shape: st.shape }; },
    get layout() { return st.layout; },
    get shape() { return st.shape; },
    get focus() { return st.focus; },
    debug() {
      return {
        layout: st.layout, shape: st.shape, visible: st.visible.map(f => f.key), pinned: Object.keys(store.layouts[st.layout] || {}), sel: st.sel, focus: st.focus,
        labels: st.labels.slice(), buildMs: st.buildMs, calls: E.renderer.info.render.calls,
        pos(key) { const el = EL.get(key); return el ? el.cur.slice() : null; },
        screenPos(key) {
          const el = EL.get(key); if (!el) return null;
          const r = E.canvas.getBoundingClientRect(), p = E.project(el.cur[0], el.cur[1], el.cur[2] + el.size.h * .5);
          return { x: r.left + p.x, y: r.top + p.y, on: p.on };
        },
        settle() { for (const el of EL.values()) { el.cur = el.tgt.slice(); el.sc = el.tgtSc; Object.assign(el.c, el.t); place(el); } E.settleCam(); updateLabels(true); E.invalidate(200); },
        // what a pointer at (cx, cy) would pick: a run key, 'project:<id>' or null
        hit(cx, cy) { const h = hitRun(cx, cy); return h ? h.key || 'project:' + h.project : null; },
        // a run that a click at its middle picks, inside the given client rect
        clickable(x0 = 0, y0 = 0, x1 = 1e9, y1 = 1e9) {
          const r = E.canvas.getBoundingClientRect();
          for (const k of st.visible.map(f => f.key)) {
            const el = EL.get(k); if (!el) continue;
            const p = E.project(el.cur[0], el.cur[1], el.cur[2] + el.size.h * .5), x = r.left + p.x, y = r.top + p.y;
            if (p.on && x > x0 && x < x1 && y > y0 && y < y1 && document.elementFromPoint(x, y) === E.canvas && hitRun(x, y)?.key === k) return { key: k, x, y };
          }
          return null;
        },
      };
    },
  };
  E.themeHooks.add(() => { if (st.L) { buildFrames(); for (const el of EL.values()) if (el.orb === undefined) setLamps(el); else setOrbSegments(el); } });
  return api;
}
