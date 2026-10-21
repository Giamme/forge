'use strict';
// node --test ui/tests/js: the pure run model behind the 3D stage, the 2D views and the inspector.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const RM = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'runmodel.js'));

const T0 = 1760600000;
const ex = (id, role, started, dur, extra = {}) => ({ id, role, started, ended: dur === null ? null : started + dur, duration_s: dur, state: dur === null ? 'running' : 'done', exit_code: dur === null ? null : 0, phases: { model: dur ? dur * 0.8 : 0 }, attempt: 1, ...extra });
function run(overrides = {}) {
  const tasks = [
    { id: 'reader', deps: [], files: ['ui/reader.py'], difficulty: 'high', wave: 1, status: 'MERGED', merged_at: T0 + 700, attempt: 2,
      attempts: [{ at: T0 + 300, attempt: 1, status: 'FAIL' }, { at: T0 + 690, attempt: 2, status: 'PASS' }],
      escalations: [{ at: T0 + 310, attempt: 1, from: 'luna', to: 'sol' }],
      executions: [ex('task-1', 'task', T0, 300, { attempt: 1 }), ex('dwarf-1', 'dwarf', T0 + 5, 200, { parent: 'task-1', attempt: 1 }), ex('qa-1', 'qa', T0 + 210, 80, { parent: 'task-1', attempt: 1, verdict: 'FAIL' }),
        ex('task-2', 'task', T0 + 320, 370, { attempt: 2 }), ex('dwarf-2', 'dwarf', T0 + 325, 250, { parent: 'task-2', attempt: 2 }), ex('qa-2', 'qa', T0 + 580, 100, { parent: 'task-2', attempt: 2, verdict: 'PASS' })],
      shadow: [] },
    { id: 'page', deps: [], files: ['ui/static/app.js'], difficulty: 'low', wave: 1, status: 'MERGED', merged_at: T0 + 260, attempt: 1,
      attempts: [{ at: T0 + 250, attempt: 1, status: 'PASS' }], escalations: [],
      executions: [ex('task-3', 'task', T0 + 10, 240), ex('dwarf-3', 'dwarf', T0 + 12, 150, { parent: 'task-3' }), ex('qa-3', 'qa', T0 + 170, 70, { parent: 'task-3', verdict: 'PASS' })],
      shadow: [{ attempt: 1, k: 0, spec: 'opus', verdict: 'FAIL', agreement: 'disagree', status: 'done' }] },
    { id: 'graph', deps: ['page'], files: ['ui/static/graph.js'], difficulty: 'medium', wave: 2, status: 'RUNNING', attempt: 1, attempts: [], escalations: [],
      executions: [ex('task-4', 'task', T0 + 270, null), ex('dwarf-4', 'dwarf', T0 + 272, 100, { parent: 'task-4' }), ex('qa-4', 'qa', T0 + 380, null, { parent: 'task-4', phase: 'model' })], shadow: [] },
    { id: 'server', deps: ['graph', 'reader'], files: ['ui/server.py'], difficulty: 'medium', wave: 3, status: 'PENDING', attempts: [], escalations: [], executions: [], shadow: [] },
    { id: 'docs', deps: ['reader'], files: ['ui/README.md'], difficulty: 'low', wave: null, status: 'PENDING', attempts: [], escalations: [], executions: [], shadow: [] },
  ];
  return { key: 'abc123def456', kind: 'plan', run_id: 'r1', tasks, waves: [['reader', 'page'], ['graph'], ['server']],
    settings: { max_parallel: 2 }, timeline: { started: T0, verifications: [] }, final_reviews: [], final_review: null, verification: null, ...overrides };
}

test('plate keeps archivision layout: districts per layer, blocks inside, plate at least 900 wide', () => {
  const P = RM.planModel(run());
  assert.ok(P.px >= 900);
  assert.deepEqual(P.districts.map(d => d.layer), ['w1', 'w2', 'w3', 'finish']);
  for (const m of P.modules) assert.equal(P.place[m.id].length, 5, m.id);
  const [ax, ay, aw] = P.place['t:reader'], [bx, by] = P.place['t:page'];
  assert.ok(bx >= ax + aw, 'blocks in one district do not overlap');
  assert.equal(by, ay, 'a district is one row');
});

test('plan model: waves as districts, inferred column for a task without a wave, a finish line after them', () => {
  const P = RM.planModel(run());
  const layerOf = id => P.modules.find(m => m.id === id).layer;
  assert.equal(layerOf('t:reader'), 'w1');
  assert.equal(layerOf('t:server'), 'w3');
  assert.equal(layerOf('t:docs'), 'w2', 'docs follows reader, its deepest dependency');
  assert.deepEqual(P.modules.filter(m => m.kind === 'finish').map(m => m.id), ['f:integration', 'f:verification', 'f:final']);
  assert.ok(P.flows.some(f => f.from === 't:graph' && f.to === 't:server'));
  assert.ok(P.flows.some(f => f.from === 't:server' && f.to === 'f:integration'), 'sinks feed integration');
  assert.ok(!P.flows.some(f => f.from === 't:reader' && f.to === 'f:integration'), 'a task others depend on is not a sink');
  assert.equal(P.finish.final.status, 'NONE');
  assert.equal(P.finish.integration.merged, 2);
});

test('solo runs have no finish line', () => {
  const r = run({ kind: 'solo', tasks: [{ ...run().tasks[1], id: 'solo' }] });
  const P = RM.planModel(r);
  assert.equal(P.finish, null);
  assert.ok(!P.modules.some(m => m.kind === 'finish'));
});

test('task parts: one zone per attempt, dwarf → qa, an escalation labelled between attempts, shadow links', () => {
  const tp = RM.taskParts(run().tasks[0]);
  assert.deepEqual(tp.zones.map(z => z.name), ['ATTEMPT 1', 'ATTEMPT 2']);
  assert.ok(tp.links.some(l => l.kind === 'flow' && l.from === 'dwarf-1' && l.to === 'qa-1'));
  const esc = tp.links.find(l => l.kind === 'escalation');
  assert.ok(esc, 'escalation link');
  assert.equal(esc.label, 'luna → sol');
  assert.equal(esc.from, 'qa-1'); assert.equal(esc.to, 'dwarf-2');
  const page = RM.taskParts(run().tasks[1]);
  assert.ok(page.links.some(l => l.kind === 'shadow' && l.agree === 'disagree'));
  const pending = RM.taskParts(run().tasks[3]);
  assert.ok(pending.parts.every(p => p.planned));
});

test('projected schedule respects dependencies, capacity and file overlap', () => {
  const r = run();
  const proj = RM.projectSchedule(r, { now: T0 + 400, maxParallel: 2 });
  assert.ok(proj.graph.running, 'a running task keeps its slot');
  assert.ok(proj.docs.ghost && proj.docs.start >= T0 + 400, 'docs can start now: reader merged');
  assert.ok(proj.server.start >= proj.graph.end - 1e-6, 'server waits for graph');
  const one = RM.projectSchedule(r, { now: T0 + 400, maxParallel: 1 });
  assert.ok(one.docs.start >= one.graph.end - 1e-6, 'one slot: docs waits for the running graph');
  const busyAt = t => Object.values(one).filter(p => p.start !== null && p.start <= t && p.end > t).length;
  for (const p of Object.values(one)) if (p.start !== null) assert.ok(busyAt(p.start + .5) <= 1);
  // overlapping files never run together
  const r2 = run(); r2.tasks[4].files = ['ui/static/graph.js'];
  const p2 = RM.projectSchedule(r2, { now: T0 + 400, maxParallel: 3 });
  assert.ok(p2.docs.start >= p2.graph.end - 1e-6, 'docs waits for graph: same file');
  assert.equal(RM.overlap(['a/b'], ['a']), true);
  assert.equal(RM.overlap(['a/b'], ['a/bc']), false);
  assert.equal(RM.overlap(['.'], ['x']), true);
});

test('execution model: attempts on time in lanes, a running attempt grows to now, ghosts after', () => {
  const X = RM.execModel(run(), { now: T0 + 400 });
  const real = X.items.filter(i => !i.ghost);
  assert.equal(real.length, 4, 'two reader attempts, page, graph');
  const lanes = new Set(real.map(i => i.lane));
  assert.ok(lanes.size <= 2, 'never more lanes than overlap needs');
  const g = real.find(i => i.task === 'graph');
  assert.equal(g.status, 'RUNNING'); assert.equal(g.end, T0 + 400); assert.equal(g.role, 'qa');
  assert.ok(X.items.some(i => i.ghost && i.task === 'server'));
  const reader2 = real.find(i => i.id === 'task-2');
  assert.equal(reader2.status, 'MERGED');
  assert.deepEqual(reader2.segs.map(s => s.role), ['dwarf', 'qa']);
  for (const a of real) for (const b of real) if (a !== b && a.lane === b.lane) assert.ok(a.end <= b.start + 1 || b.end <= a.start + 1, 'no overlap within a lane');
});

test('critical path follows the dependency that finished last', () => {
  const cp = RM.criticalPath(run(), { now: T0 + 400 });
  assert.equal(cp.mode, 'exec');
  assert.deepEqual(cp.ids, ['reader']);
  const r = run(); r.tasks[3].status = 'MERGED'; r.tasks[3].merged_at = T0 + 1200;
  r.tasks[2].status = 'MERGED'; r.tasks[2].merged_at = T0 + 880;
  r.tasks[2].executions = r.tasks[2].executions.map(e => ({ ...e, state: 'done', exit_code: 0, ended: T0 + 870 }));
  r.tasks[3].executions = [ex('task-9', 'task', T0 + 900, 250)];
  const cp2 = RM.criticalPath(r, { now: T0 + 1300 });
  assert.deepEqual(cp2.ids, ['page', 'graph', 'server'], 'graph (merged at +880) finished after reader (+700)');
});

test('lineage: every task a task waits on and every task that waits on it, through any number of steps', () => {
  const L = id => { const l = RM.lineage(run(), id); return [[...l.ancestors].sort(), [...l.descendants].sort()]; };
  assert.deepEqual(L('graph'), [['page'], ['server']]);
  assert.deepEqual(L('server'), [['graph', 'page', 'reader'], []], 'through graph to page');
  assert.deepEqual(L('reader'), [[], ['docs', 'server']]);
  assert.deepEqual(L('nope'), [[], []]);
  const cyc = { tasks: [{ id: 'a', deps: ['b'] }, { id: 'b', deps: ['a'] }, { id: 'c', deps: ['gone'] }] };
  assert.deepEqual([...RM.lineage(cyc, 'a').ancestors], ['b'], 'a cycle ends the walk');
  assert.deepEqual([...RM.lineage(cyc, 'c').ancestors], [], 'a dependency on no task is not a task');
});

test('stateAt rebuilds statuses at any moment', () => {
  const r = run();
  let s = RM.stateAt(r, T0 + 100);
  assert.equal(s.tasks.reader.status, 'RUNNING'); assert.equal(s.tasks.reader.role, 'dwarf');
  assert.equal(s.tasks.server.status, 'PENDING');
  s = RM.stateAt(r, T0 + 305);
  assert.equal(s.tasks.reader.status, 'FAIL');
  s = RM.stateAt(r, T0 + 800);
  assert.equal(s.tasks.reader.status, 'MERGED'); assert.equal(s.tasks.page.status, 'MERGED');
  assert.equal(s.tasks.graph.status, 'RUNNING');
  assert.equal(s.final, 'NONE');
});

test('concurrency counts attempts and model calls against the limit', () => {
  const c = RM.concurrency(run(), { now: T0 + 400 });
  assert.equal(c.cap, 2);
  assert.equal(c.peak, 2);
  const calls = RM.concurrency({ tasks: [{ id: 'a', executions: [ex('d1', 'dwarf', T0, 100)] }, { id: 'b', executions: [ex('d2', 'dwarf', T0 + 10, 100)] }] });
  assert.equal(calls.peak, 2, 'model calls count without a task record');
  assert.ok(c.series.every(p => p.task >= 0 && p.dwarf >= 0 && p.qa >= 0));
});

test('filters dim but keep: the atlas holds every shown run, flagged by match', () => {
  const now = T0 + 1000;
  const runs = [
    { key: 'a', repo: '/r/one', goal: 'make reader', updated: now - 10, counts: { RUNNING: 1, MERGED: 2 }, kind: 'plan', models: ['sol:xhigh'], task_statuses: [{ id: 'x', status: 'RUNNING' }] },
    { key: 'b', repo: '/r/two', goal: 'docs', updated: now - 20, counts: { MERGED: 3 }, kind: 'plan', models: ['opus'], task_statuses: [] },
    { key: 'c', repo: '/r/one', goal: 'old', updated: now - 40 * 86400, counts: { FAIL: 1 }, kind: 'solo', models: [] },
  ];
  const A = RM.atlasModel(runs, { state: 'live' }, { now, floor: 1 });
  assert.deepEqual(A.modules.map(m => [m.id, m.match]), [['a', true], ['b', false]]);
  assert.equal(A.hidden, 1, 'the old run is hidden by default');
  assert.equal(RM.atlasModel(runs, {}, { now, showAll: true }).modules.length, 3);
  assert.deepEqual(A.layers.map(l => l.name), ['one', 'two']);
  assert.equal(RM.matchRun(runs[1], { model: 'opus' }, now), true);
  assert.equal(RM.matchRun(runs[1], { text: 'READER' }, now), false);
  assert.equal(RM.runState(runs[0]), 'live'); assert.equal(RM.runState(runs[1]), 'done'); assert.equal(RM.runState(runs[2]), 'failed');
});

test('structure key ignores statuses and times', () => {
  const a = run(), b = run();
  b.tasks[2].status = 'MERGED'; b.tasks[2].executions[0].ended = T0 + 999;
  assert.equal(RM.structureKey(a), RM.structureKey(b));
  b.tasks.push({ id: 'new', deps: [] });
  assert.notEqual(RM.structureKey(a), RM.structureKey(b));
});

test('empty and odd runs never throw', () => {
  for (const r of [{}, { tasks: null }, { tasks: [{ id: 'x' }] }, { kind: 'solo', tasks: [] }]) {
    RM.planModel(r); RM.execModel(r, { now: T0 }); RM.criticalPath(r); RM.concurrency(r); RM.stateAt(r, T0); RM.projectSchedule(r, { now: T0 });
    RM.taskParts((r.tasks || [])[0]); RM.timeRange(r, T0);
  }
  assert.equal(RM.fmtDur(3725), '1h02m'); assert.equal(RM.fmtDur(65), '1m05s'); assert.equal(RM.fmtDur(null), '—');
});

test('integration runs only while a task does; a finished run with an unmerged task is blocked', () => {
  const integration = statuses => RM.finishState({ tasks: statuses.map((status, i) => ({ id: 't' + i, status })) }).integration.status;
  assert.equal(integration(['MERGED', 'MERGED', 'FAIL']), 'BLOCKED');
  assert.equal(integration(['MERGED', 'RUNNING']), 'RUNNING');
  assert.equal(integration(['MERGED', 'MERGED']), 'MERGED');
  assert.equal(integration(['MERGED', 'PENDING']), 'PENDING');
  const r = run(); r.tasks[2].status = 'FAIL'; r.tasks[3].status = 'BLOCKED'; r.tasks[4].status = 'MERGED';
  assert.equal(RM.finishState(r).integration.status, 'BLOCKED');
});

test('a shadow review without shadow.status still runs; a timed-out one is not running', () => {
  const shadow = s => RM.taskParts({ shadow: [{ attempt: 1, k: 0, ...s }] }).parts.find(p => p.kind === 'shadow').status;
  assert.equal(shadow({ status: null }), 'RUNNING');
  assert.equal(shadow({ status: 'timeout' }), 'TIMEOUT');
  assert.equal(shadow({ status: 'error' }), 'ERROR');
  assert.equal(shadow({ status: 'done', verdict: 'PASS' }), 'PASS');
});

test('the projection queues BLOCKED and INFRA tasks and merges a saved PASS first, as forge-schedule does', () => {
  const r = run();
  r.tasks[2].status = 'PASS';                            // graph passed and waits to be merged
  r.tasks[2].executions = r.tasks[2].executions.map(e => ({ ...e, state: 'done', exit_code: 0, ended: T0 + 500 }));
  r.tasks[3].status = 'BLOCKED';                         // server: blocked last time, its deps are done now
  r.tasks[4].status = 'INFRA';
  const p = RM.projectSchedule(r, { now: T0 + 600, maxParallel: 3 });
  assert.ok(p.server && p.server.ghost && p.server.start === T0 + 600, 'server starts now');
  assert.ok(p.docs && p.docs.ghost, 'an INFRA stop is dispatched again');
  assert.equal(p.graph, undefined, 'a PASS is merged, not run again');
});

test('a live execution model keeps its time axis while every block still fits', () => {
  const X1 = RM.execModel(run(), { now: T0 + 400 });
  const X2 = RM.execModel(run(), { now: T0 + 600, scale: { t0: X1.t0, t1: X1.t1 } });     // unscaled, this poll's axis would be longer
  assert.equal(X2.t1, X1.t1); assert.equal(X2.k, X1.k);
  const X3 = RM.execModel(run(), { now: X1.t1 + 10, scale: { t0: X1.t0, t1: X1.t1 } });
  assert.ok(X3.t1 > X1.t1, 'a block past the axis rescales it');
});
