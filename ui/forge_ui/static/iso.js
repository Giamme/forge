/* forge-ui 3D engine: archivision's iso-glow renderer, cut down to what forge's run maps draw.

   Ported from archivision (assets/shell-3d.html and assets/figure.js; MIT, © 2026 Giamme). Its drawing
   style comes from iso-glow, under this licence:

   MIT License
   Copyright (c) 2026 April Zhu (iso-glow: lighting, palettes, gallery shell, examples)
   Copyright (c) 2026 Tolga Cohce (iso-figure, on which iso-glow is based)
   Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
   associated documentation files (the "Software"), to deal in the Software without restriction, including
   without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
   copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the
   following conditions: The above copyright notice and this permission notice shall be included in all
   copies or substantial portions of the Software.
   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT
   LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO
   EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
   IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
   USE OR OTHER DEALINGS IN THE SOFTWARE.

   three.js 0.160.0 (MIT, © 2010-2023 three.js authors) comes from cdn.jsdelivr.net through index.html's
   import map. Differences from archivision: the canvas lives in a container, not the whole window; nothing
   writes inline styles or <style> elements (forge-ui's CSP forbids both, style.css holds every rule and
   colour); paint() lights an element toward its own colour (a task status), not only the one accent; and
   frames are drawn on demand, not every display refresh. */
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { ShaderPass } from 'three/addons/postprocessing/ShaderPass.js';
import { LineMaterial } from 'three/addons/lines/LineMaterial.js';
import { LineSegments2 } from 'three/addons/lines/LineSegments2.js';
import { LineSegmentsGeometry } from 'three/addons/lines/LineSegmentsGeometry.js';
import { mergeVertices } from 'three/addons/utils/BufferGeometryUtils.js';

export { THREE };

const TS = 0.01;                                         // one plan unit is a centimetre; three.js works in metres
const W = (x, y, z) => new THREE.Vector3(x * TS, z * TS, y * TS);   // plan x right, y toward the viewer, z up
const MONO3 = 'ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const NOISE = `float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }`;
const CW = .61;                                          // monospace advance, in em
// same tokens as the iso figures' light and dark themes
const THEMES = {
  dark: { bg: 0x121314, body: 0x1b1c1f, line: 0x4f5357, major: 0x393c3f, edge: 0x27292c, sky: 0xdde4ff, ground: 0x080808, hemi: .7, amb: 0, sun: .7, fill: .2, fadeMin: .45 },
  light: { bg: 0xe9eaea, body: 0xf1f1f0, line: 0x5d6266, major: 0x8a8f93, edge: 0xb9bcbf, sky: 0xffffff, ground: 0xe6e6e6, hemi: .9, amb: 1.6, sun: .35, fill: .15, fadeMin: .8 },
};
const PAL = {
  light: { line: '#8e9397', detail: '#c3c6c8', recess: '#dcdddd', ink: '#6a6f73', inkHi: '#16181a', glassOff: '#e4e5e5', glassOn: '#f6ece2', bg: '#f6f6f5' },
  dark: { line: '#4f5357', detail: '#2b2e30', recess: '#0f1011', ink: '#868b8f', inkHi: '#e6e8e9', glassOff: '#141516', glassOn: '#22170f', bg: '#121314' },
};

/* ---------- text: wrap and fit monospace print into a box (archivision's figure.js) ---------- */
const words = s => String(s).replace(/ ·/g, ' ·').split(' ');
export function wrap(s, n) {
  const o = []; let c = '';
  const add = (w, sep) => { if (!c) c = w; else if ((c + sep + w).length <= n) c += sep + w; else { o.push(c); c = w; } };
  for (const w of words(s)) {
    if (w.length <= n) add(w, ' ');
    else w.split(/(?<=[_(\/.-])|(?<=[a-z0-9])(?=[A-Z])/).forEach((q, i) => {
      if (q.length <= n) add(q, i ? '' : ' ');
      else for (let k = 0; k < q.length; k += n) add(q.slice(k, k + n), k || i ? '' : ' ');   // a token longer than a line is cut
    });
  }
  if (c) o.push(c); return o;
}
export function fit(s, width, hmax, fmax, lines, fmin = 3.5, step = .5) {
  s = String(s == null ? '' : s); if (!s) return null;
  for (let f = fmax; f >= fmin; f -= step) {
    const n = Math.floor(width / (CW * f)); if (n < 1) continue;
    const L = wrap(s, n);
    if (L.length <= lines && L.every(l => l.length <= n) && L.length * f * 1.18 <= hmax) return { f, L };
  }
  // still too long at the smallest size: keep the first lines and end with an ellipsis
  const f = fmin, n = Math.max(1, Math.floor(width / (CW * f))), L = wrap(s, n).slice(0, Math.max(1, Math.min(lines, Math.floor(hmax / (f * 1.18)))));
  const last = L.length - 1; if (L[last].length >= n) L[last] = L[last].slice(0, n - 1) + '…'; else L[last] += '…';
  return { f, L };
}
export const ellipsis = (s, n) => { s = String(s == null ? '' : s); return s.length <= n ? s : s.slice(0, Math.max(1, n - 1)) + '…'; };

export async function createEngine(host, { reduceMotion = false } = {}) {
  const box = () => { const r = host.getBoundingClientRect(); return [Math.max(1, Math.round(r.width)), Math.max(1, Math.round(r.height))]; };
  let [VW, VH] = box();
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setSize(VW, VH);
  const canvas = renderer.domElement;
  canvas.className = 'stage-canvas';
  canvas.tabIndex = 0;
  host.append(canvas);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(THEMES.dark.bg);
  const persp = new THREE.PerspectiveCamera(28, VW / VH, 0.05, 900);
  const ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.05, 900);
  let orthoH = 10, camera = persp;
  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true; controls.maxPolarAngle = Math.PI * .495; controls.minDistance = .3; controls.maxDistance = 400;
  controls.enableZoom = false;                            // the page scrolls past the stage until the stage is engaged
  const hemi = new THREE.HemisphereLight(0xdde4ff, 0x080808, .7); scene.add(hemi);
  const ambient = new THREE.AmbientLight(0xffffff, 0); scene.add(ambient);
  const sun = new THREE.DirectionalLight(0xffffff, .7); sun.position.set(6, 14, 10); scene.add(sun);
  const fill = new THREE.DirectionalLight(0x8899bb, .2); fill.position.set(-10, 4, -8); scene.add(fill);
  // screen-space soft gradients and dither, so glows and pools stay smooth instead of banded
  const target = new THREE.WebGLRenderTarget(VW, VH, { samples: 4, type: THREE.HalfFloatType });
  const composer = new EffectComposer(renderer, target);
  const renderPass = new RenderPass(scene, camera); composer.addPass(renderPass);
  composer.addPass(new OutputPass());
  composer.addPass(new ShaderPass({
    uniforms: { tDiffuse: { value: null } },
    vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: `uniform sampler2D tDiffuse; varying vec2 vUv; ${NOISE}
      void main() { vec4 c = texture2D(tDiffuse, vUv); c.rgb += (hash(gl_FragCoord.xy) - 0.5) / 255.0; gl_FragColor = c; }`,
  }));
  composer.setSize(VW, VH);

  /* ---------- materials ---------- */
  const accent = new THREE.Color('#ff9d3c');
  const devRes = () => renderer.getDrawingBufferSize(new THREE.Vector2());
  const bodyMat = new THREE.MeshStandardMaterial({ color: 0x1b1c1f, roughness: .9, metalness: .05, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
  const LINE_COL = new THREE.Color(0x4f5357), MAJOR_COL = new THREE.Color(0x393c3f), EDGE_COL = new THREE.Color(0x27292c), BG_COL = new THREE.Color(0x121314);
  const lineOpts = { linewidth: 1, resolution: devRes(), polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -6 };
  const lineMat = new LineMaterial({ color: EDGE_COL, ...lineOpts });
  const majorMat = new LineMaterial({ color: MAJOR_COL, ...lineOpts });
  const accentLineMat = new LineMaterial({ color: accent.clone(), linewidth: 1, resolution: devRes(), polygonOffset: true, polygonOffsetFactor: -3, polygonOffsetUnits: -8 });
  const outlineMat = new THREE.ShaderMaterial({
    uniforms: { color: { value: LINE_COL.clone() }, thickness: { value: 1 }, res: { value: devRes() } },
    vertexShader: `uniform float thickness; uniform vec2 res;
      void main() {
        vec4 p = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
        vec4 n = projectionMatrix * vec4(normalize(normalMatrix * normal), 0.0);
        vec2 d = length(n.xy) > 1e-5 ? normalize(n.xy) : vec2(0.0);
        p.xy += d * thickness * 2.0 / res * p.w;
        gl_Position = p;
      }`,
    fragmentShader: `uniform vec3 color; void main() { gl_FragColor = vec4(color, 1.0); }`,
    side: THREE.BackSide, polygonOffset: true, polygonOffsetFactor: 6, polygonOffsetUnits: 24,
  });
  const recessMat = new THREE.MeshStandardMaterial({ color: 0x0f1011, roughness: .95, side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -1 });
  let theme = 'dark';
  // lamp halos: one screen-facing point per lamp, shaded per pixel
  const glowMats = new Set();
  function glowPoints(positions, size, color = accent) {
    const g = new THREE.BufferGeometry().setFromPoints(positions);
    const m = new THREE.ShaderMaterial({
      uniforms: { color: { value: new THREE.Color(color) }, intensity: { value: 0 }, size: { value: size }, scale: { value: 1 }, light: { value: theme === 'light' ? 1 : 0 }, ortho: { value: camera === ortho ? 1 : 0 } },
      transparent: true, depthWrite: false, blending: theme === 'light' ? THREE.NormalBlending : THREE.AdditiveBlending, toneMapped: false,
      vertexShader: `uniform float size, scale, ortho;
        void main() {
          vec4 mv = modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = ortho > 0.5 ? size * scale : size * scale / -mv.z;
          mv.xyz -= normalize(mv.xyz) * size * 0.5;
          gl_Position = projectionMatrix * mv;
        }`,
      fragmentShader: `uniform vec3 color; uniform float intensity, light; ${NOISE}
        void main() {
          float d = length(gl_PointCoord - 0.5) * 2.0; if (d > 1.0) discard;
          float halo = exp(-d * d * 9.0) * (1.0 - d);
          float core = 1.0 - smoothstep(0.05, 0.12, d);
          float a = (halo * 0.7 + core * 0.9) * intensity + (hash(gl_FragCoord.xy) - 0.5) / 255.0;
          vec3 c = mix(color, vec3(1.0), core * 0.6 * (1.0 - light));
          gl_FragColor = light > 0.5 ? vec4(c, clamp(a, 0.0, 1.0)) : vec4(c * max(a, 0.0), 1.0);
        }`,
    });
    glowMats.add(m);
    const pts = new THREE.Points(g, m);
    pts.raycast = () => {};
    return pts;
  }
  // soft radial pool (light on the plate), computed per pixel and dithered; dark theme only when additive
  const softGlows = new Set();
  function softMat(color, additive) {
    const m = new THREE.ShaderMaterial({
      uniforms: { color: { value: new THREE.Color(color) }, opacity: { value: 0 } },
      transparent: true, depthWrite: false, blending: additive ? THREE.AdditiveBlending : THREE.NormalBlending, toneMapped: false,
      vertexShader: `varying vec2 vUv; void main() { vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
      fragmentShader: `uniform vec3 color; uniform float opacity; varying vec2 vUv; ${NOISE}
        void main() {
          float d = length(vUv - 0.5) * 2.0;
          float a = smoothstep(1.0, 0.0, d); a *= a;
          a = a * opacity + (hash(gl_FragCoord.xy) - 0.5) / 255.0;
          gl_FragColor = vec4(color, max(a, 0.0));
        }`,
    });
    if (additive) { softGlows.add(m); m.visible = theme !== 'light'; }
    return m;
  }

  /* ---------- lines and solids ---------- */
  const fat = (arr, mat = lineMat) => new LineSegments2(new LineSegmentsGeometry().setPositions(arr), mat);
  const loopL = (pts, mat = majorMat) => { const out = []; for (let i = 0; i < pts.length; i++) { const a = pts[i], b = pts[(i + 1) % pts.length]; out.push(a.x, a.y, a.z, b.x, b.y, b.z); } return fat(out, mat); };
  const noRay = o => { o.raycast = () => {}; return o; };
  function addOutline(m) { const o = noRay(new THREE.Mesh(m.geometry, outlineMat)); o.userData.outline = true; m.add(o); }
  // Crease lines that stay continuous: a sharp edge starts a line, followed through softer straight-on neighbours.
  function creaseInfo(geo) {
    if (geo.userData.crease) return geo.userData.crease;
    const src = new THREE.BufferGeometry().setAttribute('position', geo.attributes.position);
    if (geo.index) src.setIndex(geo.index);
    const g = mergeVertices(src, 1e-4);
    const p = g.attributes.position, ix = g.index.array, nV = p.count;
    const P = i => new THREE.Vector3().fromBufferAttribute(p, i);
    const faceN = [], edgeFaces = new Map();
    for (let f = 0; f < ix.length / 3; f++) {
      const a = ix[3 * f], b = ix[3 * f + 1], c = ix[3 * f + 2];
      const n = new THREE.Vector3().crossVectors(P(b).sub(P(a)), P(c).sub(P(a)));
      faceN.push(n.lengthSq() > 1e-14 ? n.normalize() : null);
      for (const [u, v] of [[a, b], [b, c], [c, a]]) {
        if (u === v) continue;
        const k = Math.min(u, v) * nV + Math.max(u, v);
        (edgeFaces.get(k) || edgeFaces.set(k, []).get(k)).push(f);
      }
    }
    const edges = [];
    for (const [k, fs] of edgeFaces) {
      const a = Math.floor(k / nV), b = k % nV;
      const ns = fs.map(f => faceN[f]).filter(Boolean);
      let ang = 180;
      if (ns.length >= 2) { ang = 0; for (let i = 1; i < ns.length; i++) ang = Math.max(ang, THREE.MathUtils.radToDeg(ns[0].angleTo(ns[i]))); }
      else if (ns.length === 0) continue;
      const dir = P(b).sub(P(a)), len = dir.length();
      if (len > 0) edges.push({ a, b, ang, dir: dir.divideScalar(len), len });
    }
    const byVert = new Map();
    edges.forEach((e, i) => { for (const v of [e.a, e.b]) (byVert.get(v) || byVert.set(v, []).get(v)).push(i); });
    g.dispose(); src.dispose();
    return (geo.userData.crease = { p, edges, byVert });
  }
  const GROW = .55, STRAIGHT = .9;
  function creaseLines(geo, thr) {
    const { p, edges, byVert } = creaseInfo(geo), low = thr * GROW;
    const on = new Uint8Array(edges.length), queue = [];
    edges.forEach((e, i) => { if (e.ang >= thr) { on[i] = 1; queue.push(i); } });
    while (queue.length) {
      const i = queue.pop(), e = edges[i];
      for (const [v, out] of [[e.a, -1], [e.b, 1]]) {
        for (const j of byVert.get(v)) {
          if (on[j]) continue; const f = edges[j]; if (f.ang < low) continue;
          const d = f.dir.dot(e.dir) * (f.a === v ? 1 : -1) * out;
          if (d > STRAIGHT) { on[j] = 1; queue.push(j); }
        }
      }
    }
    const parent = new Map(), find = x => { while (parent.get(x) !== x) { parent.set(x, parent.get(parent.get(x))); x = parent.get(x); } return x; };
    edges.forEach((e, i) => { if (!on[i]) return; for (const v of [e.a, e.b]) if (!parent.has(v)) parent.set(v, v); parent.set(find(e.a), find(e.b)); });
    const total = new Map(); edges.forEach((e, i) => { if (on[i]) { const r = find(e.a); total.set(r, (total.get(r) || 0) + e.len); } });
    const out = [];
    edges.forEach((e, i) => { if (on[i] && total.get(find(e.a)) >= .06) out.push(p.getX(e.a), p.getY(e.a), p.getZ(e.a), p.getX(e.b), p.getY(e.b), p.getZ(e.b)); });
    return out;
  }
  function roundRect(sh, x, y, w, h, r) {
    sh.moveTo(x + r, y); sh.lineTo(x + w - r, y); sh.quadraticCurveTo(x + w, y, x + w, y + r);
    sh.lineTo(x + w, y + h - r); sh.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    sh.lineTo(x + r, y + h); sh.quadraticCurveTo(x, y + h, x, y + h - r);
    sh.lineTo(x, y + r); sh.quadraticCurveTo(x, y, x + r, y);
  }
  // iso box: rounded in plan, sharp top and bottom rims, with its silhouette and crease hairlines
  function rbox(x, y, z, w, d, h, r, mat, parent) {
    r = Math.max(.01, Math.min(r, w / 2 - .01, d / 2 - .01));
    const sh = new THREE.Shape(); roundRect(sh, x, -(y + d), w, d, r);
    const g = new THREE.ExtrudeGeometry(sh, { depth: h, bevelEnabled: false, curveSegments: 10 });
    g.rotateX(-Math.PI / 2); g.translate(0, z, 0); g.scale(TS, TS, TS);
    const m = new THREE.Mesh(g, mat || bodyMat);
    addOutline(m);
    m.add(noRay(fat(creaseLines(g, 30), lineMat)));
    const mj = noRay(fat(creaseLines(g, 60), majorMat)); mj.renderOrder = 1; m.add(mj);
    parent.add(m); return m;
  }
  // flat recess panel lying on a top face
  function inset(x, y, z, w, d, r, parent) {
    const sh = new THREE.Shape(); roundRect(sh, x, -(y + d), w, d, Math.min(r, w / 2, d / 2));
    const g = new THREE.ShapeGeometry(sh, 12); g.rotateX(-Math.PI / 2); g.translate(0, z + .05, 0); g.scale(TS, TS, TS);
    const m = noRay(new THREE.Mesh(g, recessMat)); parent.add(m);
    parent.add(noRay(loopL(sh.getPoints(12).map(p => W(p.x, -p.y, z + .06)), lineMat))); return m;
  }
  const seg = (arr, parent, mat = lineMat) => { const l = noRay(fat(arr.flatMap(([a, b]) => [...W(...a).toArray(), ...W(...b).toArray()]), mat)); parent.add(l); return l; };
  const polyLine = (pts, mat, parent) => { const out = []; for (let i = 0; i < pts.length - 1; i++) out.push(...W(...pts[i]).toArray(), ...W(...pts[i + 1]).toArray()); const l = noRay(fat(out, mat)); parent.add(l); return l; };
  const cumLen = pts => pts.reduce((a, p, i) => (a.push(i ? a[i - 1] + Math.hypot(...p.map((v, j) => v - pts[i - 1][j])) : 0), a), []);
  const alongPts = (pts, cum, t) => { const d = t * cum[cum.length - 1]; let i = 1; while (i < cum.length - 1 && cum[i] < d) i++;
    const f = (d - cum[i - 1]) / (cum[i] - cum[i - 1] || 1); return pts[i - 1].map((v, j) => v + (pts[i][j] - v) * f); };
  const moveGlow = (gp, p, k) => { gp.geometry.attributes.position.setXYZ(0, ...W(...p).toArray()); gp.geometry.attributes.position.needsUpdate = true; gp.material.uniforms.intensity.value = k; };

  /* ---------- per-element materials, so anything can dim, fade or light up on its own ---------- */
  const OUT_FS = 'uniform vec3 color; uniform float opacity; void main() { gl_FragColor = vec4(color, opacity); }';
  const INKHI = new THREE.Color(), DETAIL = new THREE.Color(), GLASSON = new THREE.Color(), TMPC = new THREE.Color();
  let PALC = null;
  const lineMats = new Set();
  function palette() {
    const C = PAL[theme], light = theme === 'light', hi = TMPC.copy(accent).lerp(new THREE.Color(light ? 0x000000 : 0xffffff), light ? .3 : .42);
    PALC = { ...C, acc: '#' + accent.getHexString(), accHi: '#' + hi.getHexString(), light };
    INKHI.set(C.inkHi); DETAIL.set(light ? '#c3c6c8' : '#34373a'); GLASSON.set(C.glassOn);
  }
  palette();
  const setOp = (m, o) => { m.opacity = o; const t = o < .999; if (m.transparent !== t) { m.transparent = t; m.needsUpdate = true; } };
  function lmat(o = {}) {
    const m = new LineMaterial({ color: 0x777777, linewidth: 1, resolution: devRes(), polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -6, ...o });
    m.uniforms.resolution = lineMat.uniforms.resolution;   // follows the resize
    lineMats.add(m);
    return m;
  }
  function mats() {
    const out = new THREE.ShaderMaterial({ uniforms: { color: { value: new THREE.Color() }, thickness: outlineMat.uniforms.thickness, res: outlineMat.uniforms.res, opacity: { value: 1 } },
      vertexShader: outlineMat.vertexShader, fragmentShader: OUT_FS, side: THREE.BackSide, polygonOffset: true, polygonOffsetFactor: 6, polygonOffsetUnits: 24 });
    return { body: bodyMat.clone(), out, line: lmat(), major: lmat(), labs: [] };
  }
  // vis: 1 normal → 0 gone into the background; hot: 0..1 toward col (an element's status, or the accent);
  // hov: 0..1 brighter; op: lines and print; opB: the solid; tint: 0..1 of col into the body
  function paint(M, vis, hot, hov, op, opB, col = accent, tint = 0) {
    const mix = (dst, base) => { dst.copy(base); if (hov > .01) dst.lerp(INKHI, hov * .5); if (hot > .01) dst.lerp(col, hot); if (vis < .999) dst.lerp(BG_COL, 1 - vis); };
    mix(M.line.color, lineMat.color); mix(M.major.color, majorMat.color); mix(M.out.uniforms.color.value, outlineMat.uniforms.color.value);
    M.line.linewidth = M.major.linewidth = lineMat.linewidth * (1 + .5 * hot);
    M.body.color.copy(bodyMat.color); if (tint > .001) M.body.color.lerp(col, tint); if (vis < .999) M.body.color.lerp(BG_COL, (1 - vis) * .6);
    setOp(M.body, opB); setOp(M.line, op); setOp(M.major, op);
    M.out.uniforms.opacity.value = op; if (M.out.transparent !== op < .999) M.out.transparent = op < .999;
    const lo = op * (.1 + .9 * vis); for (const m of M.labs) m.opacity = lo;
  }
  const lineCol = (m, base, hot, vis, col = accent) => { m.color.copy(base); if (hot > .01) m.color.lerp(col, hot); if (vis < .999) m.color.lerp(BG_COL, 1 - vis); };
  function solid(x, y, z, w, d, h, r, M, parent, ud) {
    r = Math.max(.01, Math.min(r, w / 2 - .01, d / 2 - .01));
    const sh = new THREE.Shape(); roundRect(sh, x, -(y + d), w, d, r);
    const g = new THREE.ExtrudeGeometry(sh, { depth: h, bevelEnabled: false, curveSegments: r > 3 ? 10 : 4 });
    g.rotateX(-Math.PI / 2); g.translate(0, z, 0); g.scale(TS, TS, TS);
    const m = new THREE.Mesh(g, M.body); m.userData = ud || {};
    m.add(noRay(new THREE.Mesh(g, M.out)));
    for (const [thr, mat] of [[30, M.line], [60, M.major]]) { const a = creaseLines(g, thr); if (a.length) { const l = noRay(fat(a, mat)); if (mat === M.major) l.renderOrder = 1; m.add(l); } }
    parent.add(m); return m;
  }
  const loopAt = (x, y, w, d, r, z, mat, parent, n = 8) => { const sh = new THREE.Shape(); roundRect(sh, x, -(y + d), w, d, Math.max(.01, Math.min(r, w / 2 - .01, d / 2 - .01)));
    const l = noRay(loopL(sh.getPoints(n).map(p => W(p.x, -p.y, z)), mat)); parent.add(l); return l; };
  // a flat rounded plate on a roof (a tint, a status lamp): basic material, drawn over the roof
  function roofPlate(x, y, z, w, d, r, mat, parent) {
    const sh = new THREE.Shape(); roundRect(sh, x, -(y + d), w, d, Math.max(.01, Math.min(r, w / 2 - .01, d / 2 - .01)));
    const g = new THREE.ShapeGeometry(sh, 8); g.rotateX(-Math.PI / 2); g.translate(0, z, 0); g.scale(TS, TS, TS);
    const m = noRay(new THREE.Mesh(g, mat)); m.renderOrder = 2; parent.add(m); return m;
  }
  // an arrowhead at the end of a cable
  function cone(pts, size, mat, parent) {
    const n = pts.length - 1, e = W(...pts[n]), dir = e.clone().sub(W(...pts[Math.max(0, n - 3)])).normalize();
    const c = noRay(new THREE.Mesh(new THREE.ConeGeometry(size * .7 * TS, size * 1.9 * TS, 14), mat));
    c.position.copy(e).addScaledVector(dir, -size * .95 * TS); c.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir); parent.add(c); return c;
  }

  /* ---------- print: canvases lying on faces, drawn in plan units ---------- */
  const TXT = new Set();
  const font = (x, f, s, wt = 400) => { x.font = `${wt} ${(f * s).toFixed(2)}px ${MONO3}`; };
  function tplane(w, h, P, draw, parent, M) {
    const s = Math.max(.6, Math.min(P, 1024 / w, 1024 / h)), cw = Math.max(2, Math.ceil(w * s)), ch = Math.max(2, Math.ceil(h * s));
    const c = document.createElement('canvas'); c.width = cw; c.height = ch;
    const tex = new THREE.CanvasTexture(c); tex.colorSpace = THREE.SRGBColorSpace; tex.anisotropy = 8;
    const mat = new THREE.MeshBasicMaterial({ map: tex, transparent: true, depthWrite: false, toneMapped: false, polygonOffset: true, polygonOffsetFactor: -4, polygonOffsetUnits: -12 });
    const mesh = noRay(new THREE.Mesh(new THREE.PlaneGeometry(w * TS, h * TS), mat)); mesh.renderOrder = 3;
    const t = { c, x: c.getContext('2d'), tex, s, w, h, mesh, mat, gone: false };
    t.redraw = () => { if (c.width !== cw || c.height !== ch) { c.width = cw; c.height = ch; } const x = t.x; x.setTransform(1, 0, 0, 1, 0, 0); x.clearRect(0, 0, cw, ch); draw(x, s, PALC, t); t.gone = false; tex.needsUpdate = true; };
    tex.onUpdate = () => { c.width = c.height = 1; };      // once on the GPU the canvas is not needed until the next redraw
    mesh.onBeforeRender = () => { if (t.gone) t.redraw(); };
    mesh.userData.tp = t;
    t.redraw(); TXT.add(t); parent.add(mesh); if (M) M.labs.push(mat); return t;
  }
  const release = g => g.traverse(o => { const t = o.userData && o.userData.tp; if (t && !t.gone) { t.tex.dispose(); t.c.width = t.c.height = 1; t.gone = true; } });
  const roofAt = (t, x, y, z) => { t.mesh.rotation.x = -Math.PI / 2; t.mesh.position.copy(W(x + t.w / 2, y + t.h / 2, z + .14)); return t; };
  const frontAt = (t, x, y, ztop) => { t.mesh.position.copy(W(x + t.w / 2, y + .14, ztop - t.h / 2)); return t; };
  function lines(x, s, F, X, Y, color, maxW, wt) {
    font(x, F.f, s, wt); x.fillStyle = color; x.textAlign = 'left'; x.textBaseline = 'alphabetic';
    F.L.forEach((ln, i) => x.fillText(ln, X * s, (Y + F.f * (.82 + i * 1.18)) * s, maxW * s));
  }
  const rr = (x, s, X, Y, w, h, r) => { x.beginPath(); x.roundRect(X * s, Y * s, Math.max(0, w) * s, Math.max(0, h) * s, Math.max(0, Math.min(r, w / 2, h / 2)) * s); };
  // device pixels per plan unit once a box of this size is framed (how sharp its print is drawn)
  const pxIn = (w, d) => Math.max(2, Math.min(16, 2 * Math.min(1700, VW) * .6 / Math.max(w, d * 1.6)));

  /* ---------- cables: arcs from roof edge to roof edge (archivision's ends/curve/arc) ---------- */
  const ends = (a, b, ha, hb) => {
    const g = [b.x - a.x - a.w, a.x - b.x - b.w, b.y - a.y - a.d, a.y - b.y - b.d], k = g.indexOf(Math.max(...g));
    const ov = (a0, a1, b0, b1) => { const lo = Math.max(a0, b0), hi = Math.min(a1, b1); return lo < hi ? (lo + hi) / 2 : null; };
    if (k < 2) { const yc = ov(a.y, a.y + a.d, b.y, b.y + b.d), ya = yc ?? a.y + a.d / 2, yb = yc ?? b.y + b.d / 2;
      return k === 0 ? [[a.x + a.w, ya, ha], [b.x, yb, hb]] : [[a.x, ya, ha], [b.x + b.w, yb, hb]]; }
    const xc = ov(a.x, a.x + a.w, b.x, b.x + b.w), xa = xc ?? a.x + a.w / 2, xb = xc ?? b.x + b.w / 2;
    return k === 2 ? [[xa, a.y + a.d, ha], [xb, b.y, hb]] : [[xa, a.y, ha], [xb, b.y + b.d, hb]];
  };
  const curve = (A, B, lift0, k, n = 24) => {
    const dist = Math.hypot(B[0] - A[0], B[1] - A[1]), c = [(A[0] + B[0]) / 2, (A[1] + B[1]) / 2, Math.max(A[2], B[2]) + lift0 + dist * k], pts = [];
    for (let i = 0; i <= n; i++) { const t = i / n, u = 1 - t; pts.push([0, 1, 2].map(j => u * u * A[j] + 2 * u * t * c[j] + t * t * B[j])); }
    return pts;
  };
  const arc = (a, b, rise = 40, k = .26, cap = 260) => {
    const dist = Math.hypot(b[0] - a[0], b[1] - a[1]), t0 = Math.max(a[2], b[2]) + rise + Math.min(cap, dist * k);
    const c = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, t0], pts = [];
    for (let i = 0; i <= 40; i++) { const t = i / 40, u = 1 - t; pts.push([0, 1, 2].map(j => u * u * a[j] + 2 * u * t * c[j] + t * t * b[j])); }
    return pts;
  };

  /* ---------- colours from style.css: status colours (--st on .st-<status>) and the stage tokens ---------- */
  const probe = document.createElement('span');
  probe.className = 'stage-probe'; probe.setAttribute('aria-hidden', 'true'); host.append(probe);
  const COLORS = new Map();
  function cssColor(className, prop) {
    const key = className + '|' + prop;
    if (!COLORS.has(key)) {
      probe.className = 'stage-probe ' + className;
      const v = getComputedStyle(probe).getPropertyValue(prop).trim();
      let c = null; try { if (v) c = new THREE.Color(v); } catch (_) { c = null; }
      COLORS.set(key, c);
    }
    return COLORS.get(key);
  }
  const statusColor = s => cssColor('st-' + String(s || 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, ''), '--st') || new THREE.Color('#888888');
  const statusHex = s => '#' + statusColor(s).getHexString();

  /* ---------- theme: follows the page (prefers-color-scheme), colours from style.css ---------- */
  const themeHooks = new Set();
  let appliedKey = '';
  function setTheme(t) {
    theme = t; const T = THEMES[t], light = t === 'light';
    COLORS.clear();
    const bg = cssColor('', '--stage-bg'), acc = cssColor('', '--stage-accent');
    BG_COL.set(bg || T.bg); scene.background.copy(BG_COL);
    if (acc) accent.copy(acc);
    bodyMat.color.set(T.body);
    LINE_COL.set(T.line); MAJOR_COL.set(T.major); EDGE_COL.set(T.edge); appliedKey = '';
    recessMat.color.set(light ? 0xdcdddd : 0x0f1011);
    accentLineMat.color.copy(accent);
    hemi.color.set(T.sky); hemi.groundColor.set(T.ground); hemi.intensity = T.hemi; ambient.intensity = T.amb; sun.intensity = T.sun; fill.intensity = T.fill;
    for (const m of glowMats) { m.blending = light ? THREE.NormalBlending : THREE.AdditiveBlending; m.uniforms.light.value = light ? 1 : 0; m.needsUpdate = true; m.visible = !light; }
    for (const m of softGlows) m.visible = !light;
    palette();
    for (const f of themeHooks) f(t);
    for (const t of TXT) if (!t.gone) t.redraw();
    invalidate();
  }
  const media = matchMedia('(prefers-color-scheme: light)');
  const onScheme = () => setTheme(media.matches ? 'light' : 'dark');
  media.addEventListener('change', onScheme);

  function applyWeight() {
    const dist = camera.position.distanceTo(controls.target);
    const ppm = camera === ortho ? VH * ortho.zoom / orthoH : VH / (dist * fovK());
    const k = THREE.MathUtils.clamp((ppm / 40 - .35) / .65, 0, 1);
    const fm = THEMES[theme].fadeMin, fade = fm + (1 - fm) * k, dpr = renderer.getPixelRatio();
    const key = fade.toFixed(2) + '|' + dpr + '|' + theme + '|' + (camera === ortho); if (key === appliedKey) return; appliedKey = key;
    lineMat.linewidth = majorMat.linewidth = dpr; accentLineMat.linewidth = 1.6 * dpr;
    lineMat.color.copy(BG_COL).lerp(EDGE_COL, fade);
    majorMat.color.copy(BG_COL).lerp(MAJOR_COL, fade);
    outlineMat.uniforms.thickness.value = .5 * dpr;
    outlineMat.uniforms.color.value.copy(BG_COL).lerp(LINE_COL, fade);
    for (const f of weightHooks) f();
  }
  const weightHooks = new Set();

  /* ---------- camera: frame a box, keeping the user's angle ---------- */
  const fovK = () => 2 * Math.tan(THREE.MathUtils.degToRad(persp.fov / 2));
  function orthoFrustum() { const a = VW / VH; ortho.top = orthoH / 2; ortho.bottom = -orthoH / 2; ortho.left = -orthoH * a / 2; ortho.right = orthoH * a / 2; ortho.updateProjectionMatrix(); }
  function setOrtho(on) {
    if (on === (camera === ortho)) return;
    if (on) {
      orthoH = camera.position.distanceTo(controls.target) * fovK();
      ortho.position.copy(camera.position); ortho.quaternion.copy(camera.quaternion); ortho.zoom = 1; orthoFrustum();
      camera = ortho;
    } else {
      const d = orthoH / fovK() / ortho.zoom;
      persp.position.copy(controls.target).add(ortho.position.clone().sub(controls.target).setLength(d));
      persp.quaternion.copy(ortho.quaternion); camera = persp;
    }
    controls.object = camera; renderPass.camera = camera; controls.update(); appliedKey = '';
    for (const m of glowMats) m.uniforms.ortho.value = on ? 1 : 0;
    invalidate();
  }
  const DIR_ISO = new THREE.Vector3(1, 1, 1).normalize(), UP = new THREE.Vector3(0, 1, 0);
  let CAMT = null, NOW = performance.now();
  const ez = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  const clamp01 = v => Math.max(0, Math.min(1, v));
  function fitFor(bx, dir) {
    const c = W((bx[0] + bx[3]) / 2, (bx[1] + bx[4]) / 2, (bx[2] + bx[5]) / 2);
    const f = dir.clone().negate(), r = new THREE.Vector3().crossVectors(f, UP).normalize(), u = new THREE.Vector3().crossVectors(r, f);
    const tv = Math.tan(THREE.MathUtils.degToRad(persp.fov / 2)), th = tv * VW / VH;
    let D = .5;
    for (const X of [bx[0], bx[3]]) for (const Y of [bx[1], bx[4]]) for (const Z of [bx[2], bx[5]]) {
      const p = W(X, Y, Z).sub(c), px = p.dot(r), py = p.dot(u), pz = p.dot(f);
      D = Math.max(D, Math.abs(px) / (th * .96) - pz, Math.abs(py) / (tv * .92) - pz);
    }
    return { c, D };
  }
  function frameBox(bx, dur = 900, minEl = 26, dirOverride = null) {
    let dir = dirOverride ? dirOverride.clone().normalize() : camera.position.clone().sub(controls.target);
    dir = dir.lengthSq() > 1e-8 ? dir.normalize() : DIR_ISO.clone();
    const el = Math.asin(THREE.MathUtils.clamp(dir.y, -1, 1)), lo = THREE.MathUtils.degToRad(minEl);
    if (el < lo) { const hz = Math.hypot(dir.x, dir.z) || 1; dir.set(dir.x / hz * Math.cos(lo), Math.sin(lo), dir.z / hz * Math.cos(lo)); }
    const { c, D } = fitFor(bx, dir);
    const to = { t: c, off: dir.clone().multiplyScalar(D), zoom: camera === ortho ? orthoH / (D * fovK()) : null };
    if (reduceMotion || dur <= 1) {
      controls.target.copy(to.t); camera.position.copy(to.t).add(to.off);
      if (to.zoom) { ortho.zoom = to.zoom; ortho.updateProjectionMatrix(); }
      CAMT = null; invalidate(); return;
    }
    CAMT = { t0: performance.now(), dur, fromT: controls.target.clone(), fromO: camera.position.clone().sub(controls.target), fromZ: camera === ortho ? ortho.zoom : null, to };
    controls.enableDamping = false; invalidate();
  }
  function camStep(now) {
    if (!CAMT) return false;
    const k = ez(clamp01((now - CAMT.t0) / CAMT.dur));
    controls.target.lerpVectors(CAMT.fromT, CAMT.to.t, k);
    camera.position.copy(controls.target).add(CAMT.fromO.clone().lerp(CAMT.to.off, k));
    if (camera === ortho && CAMT.to.zoom && CAMT.fromZ) { ortho.zoom = THREE.MathUtils.lerp(CAMT.fromZ, CAMT.to.zoom, k); ortho.updateProjectionMatrix(); }
    if (k >= 1) { controls.enableDamping = true; CAMT = null; }
    return true;
  }
  const cancelCam = () => { if (CAMT) { controls.enableDamping = true; CAMT = null; } };

  /* ---------- the loop: frames only while something moves ---------- */
  const tickers = new Set();
  let raf = 0, last = 0, wakeUntil = 0, visible = true, onScreen = true, disposed = false;
  const tmpV = new THREE.Vector2();
  function invalidate(ms = 0) {
    if (disposed) return;
    if (ms) wakeUntil = Math.max(wakeUntil, performance.now() + ms);
    if (!raf && visible && onScreen) raf = requestAnimationFrame(frame);
  }
  function frame(now) {
    raf = 0; if (disposed) return;
    // capped, not fixed: on a slow GPU an opening still finishes on time, in fewer frames
    const dt = last ? Math.min(.25, (now - last) / 1000) : 1 / 60; last = now; NOW = now;
    let more = camStep(now);
    for (const f of tickers) if (f(now, dt)) more = true;
    if (controls.update()) more = true;
    applyWeight();
    const bufH = renderer.getDrawingBufferSize(tmpV).y, pxScale = camera === ortho ? bufH * ortho.zoom / orthoH : bufH / fovK();
    for (const m of glowMats) m.uniforms.scale.value = pxScale;
    composer.render();
    if ((more || now < wakeUntil) && visible && onScreen) raf = requestAnimationFrame(frame); else last = 0;
  }
  controls.addEventListener('change', () => invalidate());
  const onVisibility = () => { visible = document.visibilityState === 'visible'; if (visible) invalidate(); };
  document.addEventListener('visibilitychange', onVisibility);
  const io = typeof IntersectionObserver === 'function' ? new IntersectionObserver(es => { onScreen = es.some(e => e.isIntersecting); if (onScreen) invalidate(); }) : null;
  if (io) io.observe(host);
  function resize() {
    const [w, h] = box(); if (w === VW && h === VH) return;
    VW = w; VH = h;
    persp.aspect = VW / VH; persp.updateProjectionMatrix(); orthoFrustum();
    renderer.setSize(VW, VH); composer.setSize(VW, VH);
    const res = devRes();
    outlineMat.uniforms.res.value.copy(res); lineMat.resolution.copy(res); majorMat.resolution.copy(res); accentLineMat.resolution.copy(res);
    appliedKey = ''; invalidate();
  }
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(resize) : null;
  if (ro) ro.observe(host);

  /* ---------- pointer: picking, click (not drag) and hover ---------- */
  const RAY = new THREE.Raycaster(), NDC = new THREE.Vector2();
  function pick(cx, cy, objects, ok) {
    const r = canvas.getBoundingClientRect();
    NDC.set((cx - r.left) / r.width * 2 - 1, -((cy - r.top) / r.height) * 2 + 1); RAY.setFromCamera(NDC, camera);
    for (const h of RAY.intersectObjects(objects, false)) if (!ok || ok(h.object)) return h.object;
    return null;
  }
  // screen position (px inside the canvas) of a plan point
  const VP = new THREE.Vector3();
  function project(x, y, z) {
    camera.updateMatrixWorld(); VP.copy(W(x, y, z)).project(camera);
    return { x: (VP.x + 1) / 2 * VW, y: (1 - VP.y) / 2 * VH, on: VP.z < 1 && VP.z > -1 && Math.abs(VP.x) < 1.04 && Math.abs(VP.y) < 1.04 };
  }
  const handlers = { click: null, hover: null, key: null };
  let down = null, hoverAt = null;
  const engage = on => { controls.enableZoom = on; canvas.classList.toggle('engaged', on); };
  canvas.addEventListener('pointerdown', e => { down = [e.clientX, e.clientY]; cancelCam(); engage(true); canvas.focus({ preventScroll: true }); });
  canvas.addEventListener('pointerup', e => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) { down = null; return; }
    down = null; if (handlers.click) handlers.click(e.clientX, e.clientY, e);
  });
  canvas.addEventListener('pointermove', e => { hoverAt = [e.clientX, e.clientY]; if (!down && handlers.hover) { handlers.hover(e.clientX, e.clientY); } });
  canvas.addEventListener('pointerleave', () => { hoverAt = null; if (handlers.hover) handlers.hover(null, null); });
  canvas.addEventListener('blur', () => engage(false));
  canvas.addEventListener('wheel', () => cancelCam(), { passive: true });
  canvas.addEventListener('keydown', e => { if (handlers.key && handlers.key(e)) { e.preventDefault(); invalidate(); } });

  function disposeGroup(g) {
    g.traverse(o => {
      if (o.geometry) o.geometry.dispose();
      const t = o.userData && o.userData.tp; if (t) { t.tex.dispose(); TXT.delete(t); }
      for (const m of [].concat(o.material || [])) {
        if (m === bodyMat || m === lineMat || m === majorMat || m === outlineMat || m === recessMat || m === accentLineMat) continue;
        glowMats.delete(m); softGlows.delete(m); lineMats.delete(m);
        if (m.map) m.map.dispose(); m.dispose();
      }
    });
    if (g.parent) g.parent.remove(g);
  }
  function dispose() {
    disposed = true; if (raf) cancelAnimationFrame(raf); raf = 0;
    media.removeEventListener('change', onScheme); document.removeEventListener('visibilitychange', onVisibility);
    if (io) io.disconnect(); if (ro) ro.disconnect();
    controls.dispose(); disposeGroup(scene); target.dispose(); composer.dispose && composer.dispose(); renderer.dispose();
    canvas.remove(); probe.remove();
  }

  onScheme();
  controls.target.set(0, 0, 0); camera.position.copy(DIR_ISO).multiplyScalar(20);

  return {
    THREE, W, TS, CW, MONO3, scene, renderer, canvas, controls, host,
    get camera() { return camera; }, get theme() { return theme; }, get PALC() { return PALC; }, get size() { return [VW, VH]; }, get hover() { return hoverAt; },
    accent, BG_COL, INKHI, DETAIL, GLASSON, bodyMat, lineMat, majorMat, outlineMat, recessMat, accentLineMat,
    glowPoints, softMat, fat, loopL, noRay, rbox, inset, seg, polyLine, cumLen, alongPts, moveGlow, roundRect,
    mats, lmat, paint, lineCol, setOp, solid, loopAt, roofPlate, cone,
    tplane, release, roofAt, frontAt, lines, rr, font, fit, wrap, ellipsis, pxIn,
    ends, curve, arc, statusColor, statusHex, cssColor,
    frameBox, fitFor, cancelCam, setOrtho, isOrtho: () => camera === ortho, invalidate, tickers, themeHooks, weightHooks,
    pick, project, handlers, disposeGroup, dispose, reduceMotion, now: () => NOW,
  };
}
