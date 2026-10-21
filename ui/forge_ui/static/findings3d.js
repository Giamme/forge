/* forge-ui finding beacons: every review finding as a small mark over the stage. Its shape is its severity
   (P0 spike, P1 diamond, P2 cone, P3 pin, no severity a ring), drawn like the blocks: a hairline in its --sev-*
   colour over a translucent fill of the same, on a short post, its foot on the block it is about; a confirmed
   finding is full size and fuller, a plausible one smaller and paler. Seen from straight above (the top-down
   projection) the marks lie flat, so each keeps its silhouette. scene3d.js says where each one stands (an anchor
   that follows its block as it lifts, opens or travels); this module draws them, one instanced mesh per shape
   with the engine's hairline outline, and picks them. Marks stand still, so an idle stage draws no frames; one
   that arrives (a new finding on a poll, one a replay reaches, one moving onto an opened task) drops in. */
export const SHAPES = ['P0', 'P1', 'P2', 'P3', 'none'];
const MAX = 512;                                           // marks per shape
const SIZE = 18;                                           // plan units per unit of the shapes below: a mark stands ~40 tall
const MIN_PX = 28;                                         // a mark stands at least this tall on screen while the plate is near
const FAR_PPU = .5;                                        // screen px per plan unit below which that floor shrinks with the plate, to half
const TALL = 2.2;                                          // a mark's height in shape units (post and head)

export function createBeacons(E, { reduce = false } = {}) {
  const { THREE, W, TS } = E;
  const POST = .3;                                         // the post's height; every head sits on it
  const post = () => new THREE.CylinderGeometry(.035, .035, POST, 6).translate(0, POST / 2, 0);
  const geo = {
    P0: mergeTwo(THREE, new THREE.ConeGeometry(.42, 1.6, 4).translate(0, POST + .8, 0), post()),
    P1: mergeTwo(THREE, new THREE.OctahedronGeometry(.62).scale(1, 1.3, 1).translate(0, POST + .8, 0), post()),
    P2: mergeTwo(THREE, new THREE.ConeGeometry(.62, 1.2, 16).rotateX(Math.PI).translate(0, POST + .6, 0), post()),
    P3: mergeTwo(THREE, new THREE.SphereGeometry(.5, 18, 12).translate(0, POST + .48, 0), post()),
    none: mergeTwo(THREE, new THREE.TorusGeometry(.5, .12, 8, 24).translate(0, POST + .56, 0), post()),
  };
  const group = new THREE.Group(); group.name = 'beacons';
  const meshes = {}, outlines = {};
  for (const k of SHAPES) {
    const m = new THREE.InstancedMesh(geo[k], new THREE.MeshBasicMaterial({ toneMapped: false, transparent: true, depthWrite: false }), MAX);
    m.count = 0; m.frustumCulled = false; m.userData = { beacons: k }; m.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    m.setColorAt(0, new THREE.Color());                    // allocates the per-instance colours
    const o = new THREE.InstancedMesh(geo[k], E.outlineInstanced(), MAX);
    o.material.side = THREE.BackSide; o.count = 0; o.frustumCulled = false; o.instanceMatrix = m.instanceMatrix; o.raycast = () => {};
    group.add(m, o); meshes[k] = m; outlines[k] = o;
  }
  let unit = SIZE;                                         // plan units per shape unit this frame (marks grow when far away)
  let items = [], byShape = Object.fromEntries(SHAPES.map(k => [k, []])), seen = null, visible = true, hot = null, flat = false;
  const FLAT = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2);   // the head toward the top of the screen
  const M4 = new THREE.Matrix4(), Q = new THREE.Quaternion(), S = new THREE.Vector3(), P = new THREE.Vector3(), C = new THREE.Color();
  const ez = t => 1 - Math.pow(1 - t, 3);

  // items: [{key, sev: 'P0'..'P3'|null, plausible, anchor() → [x, y, z, scale?] in plan units | null (hidden), data}]
  function set(next) {
    const now = performance.now(), first = seen === null;
    seen = seen || new Set();
    items = next.map(it => ({ ...it, shape: SHAPES.includes(it.sev) ? it.sev : 'none', born: first || seen.has(it.key) || reduce ? -Infinity : now,
      phase: (hash(it.key) % 628) / 100 }));
    for (const it of items) seen.add(it.key);
    byShape = Object.fromEntries(SHAPES.map(k => [k, []]));
    for (const it of items) if (byShape[it.shape].length < MAX) byShape[it.shape].push(it);
    paint();
  }
  // the fill is the severity's colour seen through: fuller when confirmed, paler when plausible; the hairline is the colour itself
  function paint() {
    const bg = E.BG_COL, light = E.theme === 'light';
    for (const k of SHAPES) {
      const m = meshes[k], list = byShape[k];
      list.forEach((it, i) => {
        C.copy(E.sevColor(it.sev || 'none')).lerp(bg, it.plausible ? .7 : .45);
        if (hot === it.key) C.lerp(E.INKHI, .3);
        m.setColorAt(i, C);
      });
      if (m.instanceColor) m.instanceColor.needsUpdate = true;
      m.material.opacity = light ? .82 : .75;
      outlines[k].material.uniforms.color.value.copy(E.sevColor(k));
    }
  }
  // every frame: each mark where its anchor is; one that just arrived falls in. Returns whether anything moves.
  function tick(now) {
    if (!visible) { for (const k of SHAPES) meshes[k].count = outlines[k].count = 0; return false; }
    let moving = false;
    const ppu = Math.max(1e-6, E.pxPerUnit()), least = MIN_PX * Math.min(1, Math.max(.5, ppu / FAR_PPU)) / (ppu * TALL);
    unit = Math.max(SIZE, least);
    for (const k of SHAPES) {
      const m = meshes[k], list = byShape[k];
      let n = 0;
      for (const it of list) {
        const a = it.anchor();
        if (!a) { it.hidden = true; continue; }
        if (it.hidden && !reduce) it.born = now;               // shown again: it arrives
        it.hidden = false;
        const age = (now - it.born) / 650, drop = age < 1 ? 1 - ez(Math.max(0, age)) : 0;
        if (drop > 0) moving = true;
        const sc = unit * (a[3] || 1) * (it.plausible ? .8 : 1) * (hot === it.key ? 1.2 : 1);
        const lie = flat ? .7 * sc : 0;                        // lying flat, it floats its own thickness over the roof
        P.copy(W(a[0], a[1], a[2] + drop * 60 + lie));
        it.head = flat ? [a[0], a[1] - 1.15 * sc, a[2] + lie] : [a[0], a[1], a[2] + drop * 60 + 1.15 * sc];   // where its head is drawn, in plan units
        Q.setFromAxisAngle(UP, it.phase); if (flat) Q.premultiply(FLAT);
        S.setScalar(sc * TS);
        M4.compose(P, Q, S); m.setMatrixAt(n, M4); it.slot = n; n++;
      }
      m.count = outlines[k].count = n; m.instanceMatrix.needsUpdate = true;
      m.boundingSphere = null;                             // three.js keeps the first one it computes: picking needs today's
    }
    return moving;
  }
  const UP = new THREE.Vector3(0, 1, 0);
  // the item under the pointer, or null
  function pick(cx, cy) {
    if (!visible) return null;
    const h = E.pickHit(cx, cy, SHAPES.map(k => meshes[k]).filter(m => m.count));
    if (!h || h.instanceId == null) return null;
    const k = h.object.userData.beacons;
    return byShape[k].find(it => it.slot === h.instanceId && it.anchor()) || null;
  }
  function setHot(key) { if (key !== hot) { hot = key; paint(); } }
  E.themeHooks.add(() => paint());
  return {
    group, set, tick, pick, setHot, repaint: paint,
    get count() { return items.length; },
    get unit() { return unit; },                          // what scene3d spaces marks by
    get items() { return items; },
    setVisible(v) { visible = !!v; group.visible = visible; },
    setFlat(v) { flat = !!v; },                           // the top-down projection
    get flat() { return flat; },
    dispose() { for (const k of SHAPES) { meshes[k].dispose(); outlines[k].dispose(); geo[k].dispose(); } },
  };
}

function hash(s) { let h = 7; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h; }
// two geometries as one (positions and normals), so the pin is one instance; one without an index (the
// octahedron) draws its vertices in order
function mergeTwo(THREE, a, b) {
  const out = new THREE.BufferGeometry(), na = a.attributes.position.count, nb = b.attributes.position.count;
  const cat = (x, y, n) => { const r = new Float32Array(x.length + y.length); r.set(x); r.set(y, x.length); return new THREE.BufferAttribute(r, n); };
  out.setAttribute('position', cat(a.attributes.position.array, b.attributes.position.array, 3));
  out.setAttribute('normal', cat(a.attributes.normal.array, b.attributes.normal.array, 3));
  const order = (g, n) => g.index ? Array.from(g.index.array) : Array.from({ length: n }, (_, i) => i);
  const ia = order(a, na), ib = order(b, nb).map(i => i + na);
  out.setIndex(ia.concat(ib));
  a.dispose(); b.dispose();
  return out;
}
