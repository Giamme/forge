/* forge website: the run plate and the run cloud, drawn live with three.js in forge-ui's manner
   (tasks as blocks on a plate in their waves, hairline edges, lamps that mean one thing each,
   findings standing over their block). Demo data only; nothing here reads a real run. */
import * as THREE from 'three';

const reduceMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ---------- colours come from style.css, through a probe, so the 3D and the page never drift ---------- */
const probe = document.createElement('i');
probe.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;visibility:hidden';
document.body.append(probe);
function token(name) {
  probe.style.color = `var(${name})`;
  return new THREE.Color(getComputedStyle(probe).color);
}
function palette() {
  return {
    body: token('--stage-body'), line: token('--stage-line'), edge: token('--stage-edge'), bg: token('--stage-bg'),
    ink: token('--ink'), route: token('--route'), off: token('--lamp-off'),
    green: new THREE.Color('#5bd57d'), amber: new THREE.Color('#f4c443'), red: new THREE.Color('#ff5f53'),
    p1: token('--sev-p1'), dark: document.documentElement.dataset.theme === 'dark' ||
      (!document.documentElement.dataset.theme && window.matchMedia('(prefers-color-scheme: dark)').matches),
  };
}
function onThemeChange(fn) {
  new MutationObserver(fn).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', fn);
}

/* ---------- shared drawing helpers ---------- */
function roundedBlock(w, d, h, r) {
  const s = new THREE.Shape();
  const x = -w / 2, y = -d / 2;
  s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.quadraticCurveTo(x + w, y, x + w, y + r);
  s.lineTo(x + w, y + d - r); s.quadraticCurveTo(x + w, y + d, x + w - r, y + d);
  s.lineTo(x + r, y + d); s.quadraticCurveTo(x, y + d, x, y + d - r);
  s.lineTo(x, y + r); s.quadraticCurveTo(x, y, x + r, y);
  const g = new THREE.ExtrudeGeometry(s, { depth: h, bevelEnabled: false, curveSegments: 6 });
  g.rotateX(-Math.PI / 2);
  return g;
}
let glowTex = null;
function glowTexture() {
  if (glowTex) return glowTex;
  const c = document.createElement('canvas'); c.width = c.height = 128;
  const g = c.getContext('2d');
  const grd = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grd.addColorStop(0, 'rgba(255,255,255,1)'); grd.addColorStop(0.25, 'rgba(255,255,255,.55)');
  grd.addColorStop(0.6, 'rgba(255,255,255,.12)'); grd.addColorStop(1, 'rgba(255,255,255,0)');
  g.fillStyle = grd; g.fillRect(0, 0, 128, 128);
  glowTex = new THREE.CanvasTexture(c);
  return glowTex;
}
function glowSprite(color, size) {
  const m = new THREE.SpriteMaterial({ map: glowTexture(), color, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, opacity: 0 });
  const s = new THREE.Sprite(m); s.scale.setScalar(size); return s;
}
function labelTexture(lines, w, h, pal) {
  const scale = 4;
  const c = document.createElement('canvas'); c.width = w * scale; c.height = h * scale;
  const g = c.getContext('2d');
  g.scale(scale, scale);
  g.fillStyle = `#${pal.ink.getHexString()}`;
  g.textBaseline = 'alphabetic';
  g.font = '700 21px "D-DIN Condensed", "Arial Narrow", sans-serif';
  g.fillText(lines[0].toUpperCase(), 8, 24);
  g.globalAlpha = 0.72;
  g.font = '500 10.5px ui-monospace, Menlo, monospace';
  if (lines[1]) g.fillText(lines[1], 8, 40);
  const t = new THREE.CanvasTexture(c); t.anisotropy = 4; t.colorSpace = THREE.SRGBColorSpace;
  return t;
}
const ease = t => 1 - Math.pow(1 - Math.min(Math.max(t, 0), 1), 3);
const backOut = t => { t = Math.min(Math.max(t, 0), 1); const c = 1.7; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); };

function makeRenderer(host) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.setClearColor(0x000000, 0);
  host.append(renderer.domElement);
  return renderer;
}

/* drive a render loop only while the canvas is on screen and the tab is visible */
function loop(host, step) {
  let raf = 0, visible = false;
  const tick = now => { raf = 0; step(now); if (visible && !document.hidden) raf = requestAnimationFrame(tick); };
  const start = () => { if (!raf && visible && !document.hidden) raf = requestAnimationFrame(tick); };
  new IntersectionObserver(es => { visible = es[0].isIntersecting; start(); }, { threshold: 0.01 }).observe(host);
  document.addEventListener('visibilitychange', start);
  return { kick: start };
}

/* ================================================================================================
   The plate: one run, its tasks in waves, ending in integration, verification and final review.
   ================================================================================================ */
export async function createPlate(host, tagsHost, run) {
  try { await document.fonts.load('700 15px "D-DIN Condensed"'); } catch { /* fallback face */ }
  const renderer = makeRenderer(host);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(28, 1, 0.1, 200);
  let pal = palette();

  const hemi = new THREE.HemisphereLight(0xffffff, 0x8a948c, 1.1); scene.add(hemi);
  const sun = new THREE.DirectionalLight(0xffffff, 1.3); sun.position.set(-6, 14, 9); scene.add(sun);
  const amb = new THREE.AmbientLight(0xffffff, 0.35); scene.add(amb);

  const world = new THREE.Group(); scene.add(world);
  const COL = 3.1, ROW = 2.25;
  const pos = n => new THREE.Vector3(n.wave * COL, 0, n.row * ROW);

  // the plate itself, with a recessed well per wave
  const xs = run.tasks.map(t => t.wave), zs = run.tasks.map(t => t.row);
  const minX = Math.min(...xs) * COL - 1.9, maxX = Math.max(...xs) * COL + 1.9;
  const minZ = Math.min(...zs) * ROW - 1.55, maxZ = Math.max(...zs) * ROW + 1.55;
  const plateGeo = roundedBlock(maxX - minX, maxZ - minZ, 0.32, 0.5);
  const plateMat = new THREE.MeshStandardMaterial({ color: pal.body, roughness: 0.95, metalness: 0 });
  const plate = new THREE.Mesh(plateGeo, plateMat);
  plate.position.set((minX + maxX) / 2, -0.32, (minZ + maxZ) / 2);
  world.add(plate);
  const lineMat = new THREE.LineBasicMaterial({ color: pal.line, transparent: true, opacity: 0.9 });
  const plateEdges = new THREE.LineSegments(new THREE.EdgesGeometry(plateGeo, 25), lineMat);
  plateEdges.position.copy(plate.position); world.add(plateEdges);
  const wellMat = new THREE.LineDashedMaterial({ color: pal.line, dashSize: 0.18, gapSize: 0.14, transparent: true, opacity: 0.45 });
  const waves = [...new Set(run.tasks.filter(t => !t.finish).map(t => t.wave))];
  for (const wv of waves) {
    const ws = run.tasks.filter(t => t.wave === wv).map(t => t.row);
    const z0 = Math.min(...ws) * ROW - 1.15, z1 = Math.max(...ws) * ROW + 1.15, x0 = wv * COL - 1.35, x1 = wv * COL + 1.35;
    const g = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(x0, 0.01, z0), new THREE.Vector3(x1, 0.01, z0), new THREE.Vector3(x1, 0.01, z1), new THREE.Vector3(x0, 0.01, z1), new THREE.Vector3(x0, 0.01, z0)]);
    const l = new THREE.Line(g, wellMat); l.computeLineDistances(); world.add(l);
  }

  // blocks
  const EL = new Map(), PICK = [];
  for (const t of run.tasks) {
    const w = t.finish ? 1.9 : 2.3, d = t.finish ? 1.2 : 1.55, h = t.finish ? 0.5 : (t.difficulty === 'high' ? 1.05 : t.difficulty === 'medium' ? 0.8 : 0.6);
    const g = new THREE.Group(); g.position.copy(pos(t)); world.add(g);
    const geo = roundedBlock(w, d, h, 0.16);
    const body = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: pal.body, roughness: 0.85 }));
    body.userData.id = t.id; PICK.push(body);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 25), new THREE.LineBasicMaterial({ color: pal.line, transparent: true }));
    const roofMat = new THREE.MeshBasicMaterial({ color: pal.body, transparent: true, opacity: 0, depthWrite: false, toneMapped: false });
    const roof = new THREE.Mesh(new THREE.PlaneGeometry(w - 0.18, d - 0.18), roofMat);
    roof.rotation.x = -Math.PI / 2; roof.position.y = h + 0.006;
    const lab = new THREE.Mesh(new THREE.PlaneGeometry(w - 0.24, (w - 0.24) * 50 / 150),
      new THREE.MeshBasicMaterial({ map: labelTexture([t.id, t.finish ? '' : t.dwarf], 150, 50, pal), transparent: true, depthWrite: false }));
    lab.rotation.x = -Math.PI / 2; lab.position.set(0, h + 0.012, -d / 2 + (w - 0.24) * 50 / 150 / 2 + 0.08);
    const lamp = new THREE.Mesh(new THREE.SphereGeometry(0.085, 16, 12), new THREE.MeshBasicMaterial({ color: pal.off, toneMapped: false }));
    lamp.position.set(w / 2 - 0.26, h + 0.09, -d / 2 + 0.26);
    const halo = glowSprite(pal.amber, 1.4); halo.position.copy(lamp.position);
    const roofGlow = glowSprite(pal.amber, Math.max(w, d) * 1.9); roofGlow.position.set(0, h + 0.05, 0);
    const inner = new THREE.Group(); inner.add(body, edges, roof, lab, lamp, halo, roofGlow); g.add(inner);
    EL.set(t.id, { t, g, inner, body, edges, roof, lab, lamp, halo, roofGlow, h, w, d, status: 'pending', rise: 1, riseAt: -1, hot: 0 });
  }

  // cables between dependencies, and a bead that travels them while work flows
  const CABLES = [];
  for (const t of run.tasks) for (const dep of t.deps || []) {
    const a = EL.get(dep), b = EL.get(t.id); if (!a || !b) continue;
    let curve;
    if (Math.abs(a.g.position.x - b.g.position.x) < 0.1) {
      // the finish line runs front to back inside one column
      const p0 = a.g.position.clone().add(new THREE.Vector3(0, a.h * 0.5, a.d / 2));
      const p3 = b.g.position.clone().add(new THREE.Vector3(0, b.h * 0.5, -b.d / 2));
      curve = new THREE.CubicBezierCurve3(p0, p0.clone().add(new THREE.Vector3(0, 0.45, 0.25)), p3.clone().add(new THREE.Vector3(0, 0.45, -0.25)), p3);
    } else {
      const p0 = a.g.position.clone().add(new THREE.Vector3(a.w / 2, a.h * 0.55, 0));
      const p3 = b.g.position.clone().add(new THREE.Vector3(-b.w / 2, b.h * 0.55, 0));
      const lift = 0.9 + Math.abs(p3.z - p0.z) * 0.18;
      curve = new THREE.CubicBezierCurve3(p0, p0.clone().add(new THREE.Vector3(0.9, lift, 0)), p3.clone().add(new THREE.Vector3(-0.9, lift, 0)), p3);
    }
    const geo = new THREE.BufferGeometry().setFromPoints(curve.getPoints(40));
    const mat = new THREE.LineBasicMaterial({ color: pal.line, transparent: true, opacity: 0.75 });
    const line = new THREE.Line(geo, mat); world.add(line);
    const bead = new THREE.Mesh(new THREE.SphereGeometry(0.07, 12, 10), new THREE.MeshBasicMaterial({ color: pal.amber, toneMapped: false, transparent: true, opacity: 0 }));
    const beadGlow = glowSprite(pal.amber, 0.9); bead.add(beadGlow); world.add(bead);
    CABLES.push({ from: dep, to: t.id, curve, line, mat, bead, beadGlow, lit: 0, tgt: 0, col: pal.amber.clone(), flow: false, phase: Math.random() });
  }

  // the review finding beacon: a P1 diamond on a post, standing over its block
  const beaconGeo = new THREE.OctahedronGeometry(0.34, 0); beaconGeo.scale(1, 1.55, 1);
  const beacon = new THREE.Group();
  const bMat = new THREE.MeshBasicMaterial({ color: pal.p1, transparent: true, opacity: 0.55, depthWrite: false, toneMapped: false });
  const bMesh = new THREE.Mesh(beaconGeo, bMat);
  const bEdge = new THREE.LineSegments(new THREE.EdgesGeometry(beaconGeo), new THREE.LineBasicMaterial({ color: pal.p1 }));
  const post = new THREE.Mesh(new THREE.CylinderGeometry(0.02, 0.02, 0.9, 6), new THREE.MeshBasicMaterial({ color: pal.p1, transparent: true, opacity: 0.7 }));
  post.position.y = -0.9; const bGlow = glowSprite(pal.p1, 2.2);
  bMesh.add(bEdge); beacon.add(bMesh, post, bGlow); beacon.visible = false; world.add(beacon);
  const BEACON = { on: false, at: 0, target: null, k: 0 };

  // merge pulses: a ring that runs out across the plate when a task merges
  const PULSES = [];
  function pulse(id, color) {
    const e = EL.get(id); if (!e) return;
    const m = new THREE.Mesh(new THREE.RingGeometry(0.9, 1.0, 64), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }));
    m.rotation.x = -Math.PI / 2; m.position.copy(e.g.position).setY(0.03); world.add(m);
    PULSES.push({ m, at: performance.now() });
  }

  /* ---------- status ---------- */
  const statusColor = s => s === 'running' ? pal.amber : s === 'fail' ? pal.red : (s === 'pass' || s === 'merged') ? pal.green : s === 'route' ? pal.route : pal.off;
  function setStatus(id, s) {
    const e = EL.get(id); if (!e) return;
    const was = e.status; e.status = s;
    if (s === 'merged' && was !== 'merged') pulse(id, pal.green);
    if (s === 'fail' && was !== 'fail') pulse(id, pal.red);
    updateTag(e); kick();
  }
  function flow(from, to, on, status = 'running') {
    for (const c of CABLES) if (c.from === from && (to == null || c.to === to)) { c.tgt = on ? 1 : 0; c.flow = on && status === 'running'; c.col.copy(statusColor(status)); }
    kick();
  }
  function showBeacon(id, on) {
    BEACON.on = on; BEACON.at = performance.now(); if (id) BEACON.target = id; beacon.visible = true; kick();
  }
  // the plan landing: each block dips and rises again in turn, lamps still unlit
  function reveal(stagger = 90) {
    const now = performance.now(); let i = 0;
    for (const e of EL.values()) { e.riseAt = now + (i++) * stagger; }
    kick();
  }
  function reset() {
    for (const e of EL.values()) { e.status = 'pending'; e.rise = 1; e.riseAt = -1; updateTag(e); }
    for (const c of CABLES) { c.tgt = 0; c.lit = 0; c.flow = false; }
    BEACON.on = false; BEACON.k = 0; beacon.visible = false;
    kick();
  }

  /* ---------- name tags: model and CLI, floating over running and failed blocks ---------- */
  const TAGS = new Map();
  for (const e of EL.values()) {
    if (e.t.finish) continue;
    const el = document.createElement('div'); el.className = 'tag';
    el.innerHTML = '<span class="lamp"></span><span class="id"></span><span class="model"></span>';
    el.querySelector('.id').textContent = e.t.id; el.querySelector('.model').textContent = e.t.dwarf;
    tagsHost.append(el); TAGS.set(e.t.id, el);
  }
  function updateTag(e) {
    const el = TAGS.get(e.t.id); if (!el) return;
    const l = el.querySelector('.lamp');
    l.className = 'lamp' + (e.status === 'running' ? ' is-amber is-blink' : e.status === 'fail' ? ' is-red' : (e.status === 'merged' || e.status === 'pass') ? ' is-green' : '');
    el.dataset.state = e.status;
  }
  const V = new THREE.Vector3();
  function placeTags(rect) {
    for (const [id, el] of TAGS) {
      const e = EL.get(id);
      V.copy(e.g.position).setY(e.h * e.rise + 0.6).applyMatrix4(world.matrixWorld).project(camera);
      const x = (V.x + 1) / 2 * rect.width, y = (1 - V.y) / 2 * rect.height;
      el.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px) translate(-50%, -100%)`;
      el.style.opacity = '1';
    }
  }

  /* ---------- camera: a slow drift and the pointer, framed toward the right of the stage ---------- */
  const center = new THREE.Vector3((minX + maxX) / 2, 0.4, (minZ + maxZ) / 2);
  const radius = Math.hypot(maxX - minX, maxZ - minZ) / 2;
  let pointer = { x: 0, y: 0 }, pTarget = { x: 0, y: 0 };
  host.addEventListener('pointermove', ev => {
    const r = host.getBoundingClientRect();
    pTarget.x = (ev.clientX - r.left) / r.width * 2 - 1; pTarget.y = (ev.clientY - r.top) / r.height * 2 - 1;
    kick();
  });
  host.addEventListener('pointerleave', () => { pTarget.x = 0; pTarget.y = 0; });
  let W = 1, H = 1, frameOffset = 0;
  function resize() {
    const r = host.getBoundingClientRect(); W = Math.max(1, r.width); H = Math.max(1, r.height);
    renderer.setSize(W, H, false); camera.aspect = W / H;
    // on a wide stage the plate sits in the free area right of the intro and above the session
    const wide = W > 900;
    frameOffset = wide ? W * 0.205 : 0;
    camera.setViewOffset(W, H, -frameOffset, wide ? H * 0.13 : 0, W, H);
    camera.updateProjectionMatrix(); kick();
  }
  new ResizeObserver(resize).observe(host);

  function placeCamera(now) {
    const still = reduceMotion();
    pointer.x += (pTarget.x - pointer.x) * 0.06; pointer.y += (pTarget.y - pointer.y) * 0.06;
    const drift = still ? 0 : Math.sin(now / 9000) * 0.09;
    const az = -0.72 + drift + pointer.x * 0.12, el = 0.62 - pointer.y * 0.06;
    const fit = W > 900 ? 1.06 * Math.max(1, 1.75 / camera.aspect) : 0.78 * Math.max(1, 1.5 / camera.aspect);
    const dist = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * fit;
    camera.position.set(center.x + Math.sin(az) * Math.cos(el) * dist, center.y + Math.sin(el) * dist, center.z + Math.cos(az) * Math.cos(el) * dist);
    camera.lookAt(center);
  }

  /* ---------- hover: a hovered block lifts and lights its edges ---------- */
  const ray = new THREE.Raycaster(), mouse = new THREE.Vector2(); let hover = null;
  host.addEventListener('pointermove', ev => {
    const r = host.getBoundingClientRect();
    mouse.set((ev.clientX - r.left) / r.width * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const hit = ray.intersectObjects(PICK, false)[0];
    hover = hit ? hit.object.userData.id : null;
    host.style.cursor = hover ? 'pointer' : '';
  });

  /* ---------- frame ---------- */
  const tmp = new THREE.Color();
  function step(now) {
    placeCamera(now);
    const still = reduceMotion();
    const blinkOn = still || Math.floor(now / 500) % 2 === 0;
    for (const e of EL.values()) {
      if (e.riseAt >= 0) e.rise = still ? 1 : 0.55 + 0.45 * backOut((now - e.riseAt) / 600);
      e.inner.scale.y = Math.max(0.001, e.rise); e.inner.visible = e.rise > 0.002;
      const col = statusColor(e.status);
      const lit = e.status !== 'pending';
      e.hot += ((hover === e.t.id ? 1 : 0) - e.hot) * 0.2;
      e.inner.position.y = e.hot * 0.18;
      e.roof.material.color.copy(col); e.roof.material.opacity = lit ? (pal.dark ? 0.32 : 0.5) : 0;
      e.lamp.material.color.copy(lit ? col : pal.off);
      const on = e.status === 'running' ? (blinkOn ? 1 : 0.25) : lit ? 1 : 0;
      e.halo.material.color.copy(col); e.halo.material.opacity = on * (pal.dark ? 0.95 : 0.6);
      e.roofGlow.material.color.copy(col); e.roofGlow.material.opacity = lit ? (pal.dark ? 0.22 : 0.12) * (e.status === 'running' ? 0.7 + 0.3 * on : 1) : 0;
      e.edges.material.color.copy(tmp.copy(pal.line).lerp(hover === e.t.id ? pal.route : col, lit ? 0.55 : hover === e.t.id ? 0.8 : 0));
    }
    for (const c of CABLES) {
      c.lit += (c.tgt - c.lit) * 0.08;
      const ends = Math.min(EL.get(c.from).rise, EL.get(c.to).rise);
      c.mat.color.copy(tmp.copy(pal.line).lerp(c.col, c.lit)); c.mat.opacity = (0.55 + 0.45 * c.lit) * Math.min(1, ends);
      if (c.flow && !still) {
        const t = ((now / 1400) + c.phase) % 1; c.bead.position.copy(c.curve.getPoint(t));
        c.bead.material.opacity = Math.sin(t * Math.PI); c.beadGlow.material.opacity = c.bead.material.opacity * 0.9;
        c.bead.material.color.copy(c.col); c.beadGlow.material.color.copy(c.col);
      } else { c.bead.material.opacity = 0; c.beadGlow.material.opacity = 0; }
    }
    if (beacon.visible && BEACON.target) {
      const e = EL.get(BEACON.target);
      const k = still ? (BEACON.on ? 1 : 0) : BEACON.on ? backOut((now - BEACON.at) / 650) : 1 - ease((now - BEACON.at) / 400);
      BEACON.k = k;
      beacon.position.set(e.g.position.x + 0.35, e.h + 1.45 + (1 - Math.min(k, 1)) * 2.2, e.g.position.z);
      bMesh.rotation.y = still ? 0.6 : now / 1600;
      beacon.scale.setScalar(Math.max(0.001, Math.min(k, 1.2)));
      bGlow.material.opacity = (pal.dark ? 0.8 : 0.45) * Math.min(k, 1);
      if (!BEACON.on && k <= 0.01) beacon.visible = false;
    }
    for (let i = PULSES.length - 1; i >= 0; i--) {
      const p = PULSES[i], t = (now - p.at) / 1300;
      if (t >= 1 || still) { world.remove(p.m); p.m.geometry.dispose(); p.m.material.dispose(); PULSES.splice(i, 1); continue; }
      p.m.scale.setScalar(1 + ease(t) * 3.2); p.m.material.opacity = 0.85 * (1 - t);
    }
    renderer.render(scene, camera);
    placeTags({ width: W, height: H });
    return true;
  }
  const L = loop(host, now => step(now));
  const kick = () => L.kick();

  onThemeChange(() => {
    pal = palette();
    plateMat.color.copy(pal.body); lineMat.color.copy(pal.line); wellMat.color.copy(pal.line);
    for (const e of EL.values()) {
      e.body.material.color.copy(pal.body);
      e.lab.material.map.dispose(); e.lab.material.map = labelTexture([e.t.id, e.t.finish ? '' : e.t.dwarf], 150, 50, pal); e.lab.material.needsUpdate = true;
    }
    hemi.intensity = pal.dark ? 0.7 : 1.1; amb.intensity = pal.dark ? 0.15 : 0.35;
    bMat.color.copy(pal.p1); kick();
  });
  hemi.intensity = pal.dark ? 0.7 : 1.1; amb.intensity = pal.dark ? 0.15 : 0.35;

  return { setStatus, flow, beacon: showBeacon, reveal, reset, kick };
}

/* ================================================================================================
   The run cloud: every run as a block, grouped by project, in four layouts.
   ================================================================================================ */
export async function createCloud(host, runs, { onPick } = {}) {
  const renderer = makeRenderer(host);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 400);
  let pal = palette();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x7d877f, 1.1));
  const sun = new THREE.DirectionalLight(0xffffff, 1.2); sun.position.set(-10, 20, 12); scene.add(sun);

  const N = runs.length;
  const geo = roundedBlock(1, 1, 1, 0.12);
  const bodyMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.8 });
  const bodies = new THREE.InstancedMesh(geo, bodyMat, N);
  bodies.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
  const roofGeo = new THREE.PlaneGeometry(0.82, 0.82); roofGeo.rotateX(-Math.PI / 2);
  const roofMat = new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false });
  const roofs = new THREE.InstancedMesh(roofGeo, roofMat, N);
  const group = new THREE.Group(); group.add(bodies, roofs); scene.add(group);

  // glow points for running runs
  const glowGeo = new THREE.BufferGeometry();
  const glowPos = new Float32Array(N * 3); glowGeo.setAttribute('position', new THREE.BufferAttribute(glowPos, 3));
  const glowMat = new THREE.PointsMaterial({ size: 3.2, map: glowTexture(), color: pal.amber, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true, opacity: 0.85 });
  const glow = new THREE.Points(glowGeo, glowMat); group.add(glow);

  const projects = [...new Set(runs.map(r => r.project))];
  const colorOf = r => r.state === 'running' ? pal.amber : r.state === 'failed' ? pal.red : pal.green;

  function layout(name) {
    const out = [];
    runs.forEach((r, i) => {
      const p = projects.indexOf(r.project), k = r.idx, n = r.count;
      let v;
      if (name === 'helix') {
        const a = k * 0.62 + p * 1.1, rad = 3.2;
        v = new THREE.Vector3((p - (projects.length - 1) / 2) * 9 + Math.cos(a) * rad, k * 0.95 - n * 0.45, Math.sin(a) * rad);
      } else if (name === 'sphere') {
        const phi = Math.acos(1 - 2 * (i + 0.5) / N), th = Math.PI * (1 + Math.sqrt(5)) * i;
        v = new THREE.Vector3(Math.cos(th) * Math.sin(phi), Math.cos(phi), Math.sin(th) * Math.sin(phi)).multiplyScalar(13);
      } else if (name === 'galaxy') {
        const arm = p / projects.length * Math.PI * 2, t = k / n, a = arm + t * 3.6, rad = 3 + t * 16;
        v = new THREE.Vector3(Math.cos(a) * rad, Math.sin(i * 1.7) * 0.8, Math.sin(a) * rad);
      } else {
        v = new THREE.Vector3((r.day - 15) * 1.25, 0, (p - (projects.length - 1) / 2) * 4.2 + (k % 2) * 1.2);
      }
      out.push(v);
    });
    return out;
  }
  let from = layout('helix'), to = from, tAt = 0;
  const cur = from.map(v => v.clone());
  function setLayout(name) { from = cur.map(v => v.clone()); to = layout(name); tAt = performance.now(); kick(); }

  const M = new THREE.Matrix4(), Q = new THREE.Quaternion(), S = new THREE.Vector3(), P = new THREE.Vector3(), C = new THREE.Color();
  function writeInstances(now) {
    const k = reduceMotion() ? 1 : ease((now - tAt) / 1400);
    let gi = 0;
    runs.forEach((r, i) => {
      cur[i].lerpVectors(from[i], to[i], k);
      const h = 0.5 + r.tasks * 0.22;
      S.set(1.6, h, 1.6); Q.identity(); P.copy(cur[i]);
      M.compose(P, Q, S); bodies.setMatrixAt(i, M);
      bodies.setColorAt(i, C.copy(pal.body).lerp(colorOf(r), hover === i ? 0.35 : 0.08));
      S.set(1.6, 1, 1.6); P.copy(cur[i]).setY(cur[i].y + h + 0.01); M.compose(P, Q, S); roofs.setMatrixAt(i, M);
      roofs.setColorAt(i, C.copy(colorOf(r)).multiplyScalar(pal.dark ? 0.9 : 0.85));
      if (r.state === 'running') { glowPos[gi++] = P.x; glowPos[gi++] = P.y + 0.3; glowPos[gi++] = P.z; }
    });
    for (let j = gi; j < glowPos.length; j++) glowPos[j] = 9999;
    glowGeo.attributes.position.needsUpdate = true;
    bodies.instanceMatrix.needsUpdate = true; roofs.instanceMatrix.needsUpdate = true;
    if (bodies.instanceColor) bodies.instanceColor.needsUpdate = true;
    if (roofs.instanceColor) roofs.instanceColor.needsUpdate = true;
  }

  let W = 1, H = 1;
  function resize() { const r = host.getBoundingClientRect(); W = Math.max(1, r.width); H = Math.max(1, r.height); renderer.setSize(W, H, false); camera.aspect = W / H; camera.updateProjectionMatrix(); kick(); }
  new ResizeObserver(resize).observe(host);

  let drag = null, yaw = 0.6, pitch = 0.42, yawV = 0, auto = true;
  host.addEventListener('pointerdown', e => { drag = { x: e.clientX, y: e.clientY, yaw, pitch }; auto = false; host.setPointerCapture(e.pointerId); });
  host.addEventListener('pointerup', e => { drag = null; host.releasePointerCapture(e.pointerId); });
  host.addEventListener('pointermove', e => {
    if (drag) { yaw = drag.yaw - (e.clientX - drag.x) * 0.006; pitch = Math.min(1.2, Math.max(0.05, drag.pitch + (e.clientY - drag.y) * 0.004)); kick(); return; }
    const r = host.getBoundingClientRect();
    mouse.set((e.clientX - r.left) / r.width * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const hit = ray.intersectObject(bodies, false)[0];
    const h = hit ? hit.instanceId : null;
    if (h !== hover) { hover = h; host.style.cursor = h != null ? 'pointer' : 'grab'; if (onPick) onPick(h != null ? runs[h] : null); kick(); }
  });
  const ray = new THREE.Raycaster(), mouse = new THREE.Vector2(); let hover = null;

  function step(now) {
    if (auto && !reduceMotion() && !drag) yaw += 0.0016;
    const dist = 50;
    camera.position.set(Math.sin(yaw) * Math.cos(pitch) * dist, Math.sin(pitch) * dist, Math.cos(yaw) * Math.cos(pitch) * dist);
    camera.lookAt(0, 0, 0);
    writeInstances(now);
    renderer.render(scene, camera);
  }
  const L = loop(host, step); const kick = () => L.kick();
  onThemeChange(() => { pal = palette(); glowMat.color.copy(pal.amber); kick(); });
  return { setLayout };
}
