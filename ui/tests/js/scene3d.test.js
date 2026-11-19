'use strict';
// node --test: scene3d part stagger window stays inside [0, 1] regardless of attempt count (#195).
const test = require('node:test');
const assert = require('node:assert/strict');
const { register } = require('node:module');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const RM = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'runmodel.js'));

const THREE_STUB_LOADER = `
export async function resolve(spec, ctx, next) {
  if (spec === 'three' || spec.startsWith('three/')) {
    return { shortCircuit: true, url: 'three-stub:' + spec };
  }
  return next(spec, ctx);
}
export async function load(url, ctx, next) {
  if (url.startsWith('three-stub:')) {
    return {
      shortCircuit: true,
      format: 'module',
      source: 'export default {}; export class OrbitControls {} export class EffectComposer {} export class RenderPass {} export class OutputPass {} export class ShaderPass {} export class LineMaterial {} export class LineSegments2 {} export class LineSegmentsGeometry {} export function mergeVertices() {}',
    };
  }
  return next(url, ctx);
}
`;

let scene3d;
async function loadScene3d() {
  if (scene3d) return scene3d;
  register('data:text/javascript,' + encodeURIComponent(THREE_STUB_LOADER));
  globalThis.window = { ForgeRunModel: RM };
  scene3d = await import(pathToFileURL(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'scene3d.js')).href);
  return scene3d;
}

test('short tasks keep the 0.05 stagger step up to 8 parts', async () => {
  const { partWindow, partRise } = await loadScene3d();
  assert.deepEqual(partWindow(0, 1).map(v => +v.toFixed(4)), [0.34, 0.64]);
  assert.deepEqual(partWindow(1, 1).map(v => +v.toFixed(4)), [0.39, 0.69]);
  assert.deepEqual(partWindow(7, 7).map(v => +v.toFixed(4)), [0.69, 0.99]);
  for (let idx = 0; idx <= 7; idx++) {
    assert.equal(partRise(1, idx, 7), 1, `part ${idx} reaches full height at p=1`);
  }
});

test('tasks with 10 attempts (2 or 3 parts each) reach visibility and scale 1 for every part', async () => {
  const { partWindow, partRise } = await loadScene3d();
  const executions = [];
  for (let i = 1; i <= 10; i++) {
    const start = 1760600000 + i * 100;
    executions.push(
      { id: `d${i}`, role: 'dwarf', attempt: i, started: start, ended: start + 40, exit: 0 },
      { id: `q${i}`, role: 'qa', attempt: i, started: start + 50, ended: start + 80, verdict: i === 10 ? 'PASS' : 'FINDINGS' },
    );
  }
  const tp = RM.taskParts({ id: 't1', status: 'MERGED', executions });
  const R = Math.max(1, ...tp.zones.map(z => z.parts.length));
  const indices = [];
  tp.zones.forEach((z, ci) => z.parts.forEach((_pid, ri) => indices.push(ri + ci * R)));
  const maxIdx = Math.max(...indices);
  assert.equal(maxIdx, 19);

  for (const idx of indices) {
    const [a, b] = partWindow(idx, maxIdx);
    assert.ok(a >= 0.34 && b <= 1 + 1e-12, `window [${a}, ${b}] for idx ${idx} stays within [0.34, 1]`);
    const q = partRise(1, idx, maxIdx);
    assert.equal(q, 1, `idx ${idx} settles to scale 1`);
  }

  // With shadow reviews (3 parts per attempt => maxIdx = 29) and even up to 60 indices
  for (const cap of [29, 45, 60]) {
    for (let idx = 0; idx <= cap; idx++) {
      assert.equal(partRise(1, idx, cap), 1, `cap=${cap} idx=${idx} settles to 1`);
    }
  }
});
