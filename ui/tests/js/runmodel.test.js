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

test('a done final review the branch has moved past is OUTDATED, and it keeps its verdict', () => {
  const fr = { state: 'done', verdict: 'FAIL', outdated: true, tip: '3c4d5e6ffff', merged_since: ['page', 'reader'] };
  const F = RM.finishState(run({ final_review: fr, final_reviews: [fr] })).final;
  assert.equal(F.status, 'OUTDATED');
  assert.equal(F.verdict, 'FAIL');
  assert.equal(F.tip, '3c4d5e6ffff');
  assert.deepEqual(F.merged_since, ['page', 'reader']);
  assert.equal(RM.finalStatus(fr), 'OUTDATED');
  const current = { state: 'done', verdict: 'FAIL', outdated: false, tip: 'abc', merged_since: [] };
  assert.equal(RM.finishState(run({ final_review: current })).final.status, 'FAIL');
  const running = { state: 'running', verdict: null, outdated: true, tip: null, merged_since: [] };
  const live = RM.finishState(run({ final_review: running })).final;
  assert.equal(live.status, 'RUNNING');
  assert.equal(live.verdict, null);
  assert.equal(live.tip, null);
  assert.deepEqual(live.merged_since, []);
  assert.equal(RM.reviewSinceLine({ verdict: 'FAIL', tip: '3c4d5e6ffff', merged_since: ['a', 'b', 'c', 'd', 'e', 'f', 'g'] }),
    'FAIL at 3c4d5e6; 7 tasks merged since: a, b, c, d, e +2 more');
  assert.equal(RM.reviewSinceLine({ verdict: 'FAIL', tip: 'abc', merged_since: ['page'] }), 'FAIL at abc; 1 task merged since: page');
  assert.equal(RM.reviewSinceLine(fr), 'FAIL at 3c4d5e6; 2 tasks merged since: page, reader');
});

test('replay shows OUTDATED only from the merge that made the review stale, and the present matches finishState', () => {
  const at = T0 + 1000, ended = at + 100, mergeB = at + 400;
  const present = mergeB + 50;
  const fr = { state: 'done', verdict: 'FAIL', outdated: true, at, started: at, ended, tip: 'abcdef123456', merged_since: ['b'] };
  const r = run({ final_review: fr, final_reviews: [fr], integration: { merged: [{ id: 'a', at: at - 20 }, { id: 'b', at: mergeB }] } });
  assert.equal(RM.stateAt(r, ended - 1).final, 'RUNNING');
  assert.equal(RM.stateAt(r, ended).final, 'FAIL');
  assert.equal(RM.stateAt(r, mergeB - 1).final, 'FAIL');
  assert.equal(RM.stateAt(r, mergeB).final, 'OUTDATED');
  assert.equal(RM.stateAt(r, present).final, 'OUTDATED');
  assert.equal(RM.stateAt(r, present).final, RM.finishState(r).final.status);
  const range = RM.timeRange(r, null);
  assert.ok(range.end >= mergeB, 'scrubbing can reach the merge that stales the review');
  assert.equal(RM.stateAt(r, range.end).final, RM.finishState(r).final.status);

  // The only later merge fell inside the review's starting second, so merged_since is empty.
  const frEmpty = { state: 'done', verdict: 'FAIL', outdated: true, at, started: at, ended: at + 0.2, tip: 'abc1234zz', merged_since: [] };
  const rEmpty = run({ final_review: frEmpty, final_reviews: [frEmpty], integration: { merged: [{ id: 'a', at: at + 0.5 }] } });
  assert.equal(RM.stateAt(rEmpty, at + 0.4).final, 'FAIL');
  assert.equal(RM.stateAt(rEmpty, at + 0.5).final, 'OUTDATED');
  assert.equal(RM.stateAt(rEmpty, at + 0.5).final, RM.finishState(rEmpty).final.status);
  const emptyRange = RM.timeRange(rEmpty, null);
  assert.ok(emptyRange.end >= at + 0.5);
  assert.equal(RM.stateAt(rEmpty, emptyRange.end).final, RM.finishState(rEmpty).final.status);

  // No merge time at all: OUTDATED at the review's end. A merge before `at` does not count.
  const frEnd = { state: 'done', verdict: 'FAIL', outdated: true, at, started: at, ended: at + 30, tip: 'abc', merged_since: [] };
  const rEnd = run({ final_review: frEnd, final_reviews: [frEnd], integration: { merged: [{ id: 'old', at: at - 5 }] } });
  assert.equal(RM.stateAt(rEnd, at + 29).final, 'RUNNING');
  assert.equal(RM.stateAt(rEnd, at + 30).final, 'OUTDATED');
  assert.equal(RM.stateAt(rEnd, at + 30).final, RM.finishState(rEnd).final.status);

  for (const outdated of [false, null]) {
    const frCurrent = { state: 'done', verdict: 'FAIL', outdated, at, started: at, ended, tip: 'abc', merged_since: [] };
    const rCurrent = run({ final_review: frCurrent, final_reviews: [frCurrent], integration: { merged: [{ id: 'b', at: mergeB }] } });
    assert.equal(RM.stateAt(rCurrent, mergeB - 1).final, 'FAIL');
    assert.equal(RM.stateAt(rCurrent, present).final, 'FAIL');
    assert.equal(RM.stateAt(rCurrent, present).final, RM.finishState(rCurrent).final.status);
    const currentRange = RM.timeRange(rCurrent, null);
    assert.ok(currentRange.end >= mergeB);
    assert.equal(RM.stateAt(rCurrent, currentRange.end).final, 'FAIL');
  }
});

test('the replay range reaches a merge that lands after the last execution', () => {
  // Review ends at 1100, the last execution at 1399, the merge that stales the review at 1400.
  // The slider used to stop at 1399, still FAIL, while the current view is OUTDATED.
  const reviewEnd = 1100, execEnded = 1399, mergeAt = 1400;
  const fr = { state: 'done', verdict: 'FAIL', outdated: true, at: 1000, started: 1000, ended: reviewEnd, tip: 'abcdef1', merged_since: ['b'] };
  const task = { id: 'b', status: 'MERGED', merged_at: mergeAt, deps: [], executions: [
    { id: 'task-b', role: 'task', started: 1200, ended: execEnded, state: 'done', attempt: 1 }] };
  const r = { tasks: [task], final_review: fr, final_reviews: [fr], integration: { merged: [{ id: 'b', at: mergeAt }] } };
  const range = RM.timeRange(r, null);
  assert.ok(range && range.end >= mergeAt);
  assert.equal(RM.stateAt(r, execEnded).final, 'FAIL');
  assert.equal(RM.stateAt(r, range.end).final, 'OUTDATED');
  assert.equal(RM.stateAt(r, range.end).final, RM.finishState(r).final.status);
  // Either record of the marker is enough. outdatedMoment reads integration.merged.
  const markerOnly = { ...r, tasks: [{ ...task, merged_at: null }] };
  const markerRange = RM.timeRange(markerOnly, null);
  assert.ok(markerRange.end >= mergeAt);
  assert.equal(RM.stateAt(markerOnly, execEnded).final, 'FAIL');
  assert.equal(RM.stateAt(markerOnly, markerRange.end).final, 'OUTDATED');
  const taskOnly = { ...r, integration: null };
  assert.ok(RM.timeRange(taskOnly, null).end >= mergeAt);
});

test('a merged task a human accepted displays as ACCEPTED; any other status stays itself', () => {
  assert.equal(RM.taskStatus({ status: 'MERGED', accepted: true }), 'ACCEPTED');
  assert.equal(RM.taskStatus({ status: 'MERGED', accepted: false }), 'MERGED');
  assert.equal(RM.taskStatus({ status: 'MERGED' }), 'MERGED');
  assert.equal(RM.taskStatus({ status: 'FAIL', accepted: true }), 'FAIL');
  assert.equal(RM.taskStatus({ status: 'RUNNING', accepted: true }), 'RUNNING');
  assert.equal(RM.taskStatus({ status: 'PENDING' }), 'PENDING');
});

test('the verdict column names what an acceptance was over and the graph draws no failure mark', () => {
  const over = (accepted_over, verdict) => RM.taskVerdict({ status: 'MERGED', accepted: true, accepted_over, verdict });
  assert.deepEqual(over('FAIL', 'FAIL'), { text: 'FAIL · accepted', status: 'accepted', mark: null });
  assert.deepEqual(over('UNKNOWN', 'FAIL'), { text: 'UNKNOWN · accepted', status: 'accepted', mark: null });
  assert.deepEqual(over(null, 'FAIL'), { text: 'accepted', status: 'accepted', mark: null });
  // a review that was not accepted still fails, and the 2D graph still draws its cross or tick
  assert.deepEqual(RM.taskVerdict({ status: 'MERGED', accepted: false, verdict: 'FAIL' }), { text: 'FAIL', status: 'fail', mark: 'FAIL' });
  assert.deepEqual(RM.taskVerdict({ status: 'MERGED', verdict: 'PASS' }), { text: 'PASS', status: 'pass', mark: 'PASS' });
  assert.deepEqual(RM.taskVerdict({ status: 'RUNNING', verdict: null }), { text: null, status: 'unknown', mark: null });
});

test('only the accepted attempt is ACCEPTED; earlier failures and the reviewer verdict stay', () => {
  const r = run();
  const reader = r.tasks[0];
  reader.accepted = true;
  reader.accepted_attempt = 2;
  reader.accepted_over = 'FAIL';
  reader.verdict = 'FAIL';
  reader.attempts[1].status = 'FAIL';
  reader.executions.find(e => e.id === 'qa-2').verdict = 'FAIL';
  const items = RM.execModel(r, { now: T0 + 800 }).items.filter(i => i.task === 'reader');
  assert.equal(items.find(i => i.attempt === 1).status, 'FAIL');
  assert.equal(items.find(i => i.attempt === 2).status, 'ACCEPTED');
  assert.equal(items.find(i => i.attempt === 2).segs.find(s => s.role === 'qa').status, 'FAIL');
  assert.equal(items.find(i => i.attempt === 2).segs.find(s => s.role === 'dwarf').status, 'PASS');
  const parts = RM.taskParts(reader);
  assert.equal(parts.parts.find(p => p.id === 'qa-1').status, 'FAIL');
  assert.equal(parts.parts.find(p => p.id === 'qa-2').status, 'FAIL');
  // no attempt number on the acceptance: the history is not relabelled
  const plain = run();
  plain.tasks[0].accepted = true;
  plain.tasks[0].accepted_attempt = null;
  assert.equal(RM.execModel(plain, { now: T0 + 800 }).items.find(i => i.id === 'task-2').status, 'MERGED');
  assert.equal(RM.attemptStatus({ status: 'FAIL', accepted: true, accepted_attempt: 1 }, 1, 'FAIL'), 'FAIL');
  // acceptance does not change the run's counts: reader is still one of the two MERGED tasks
  const before = RM.finishState(run());
  assert.equal(RM.finishState(r).integration.merged, before.integration.merged);
  assert.equal(RM.finishState(r).integration.status, before.integration.status);
});

test('an outdated review matches the OUTDATED filter, not its verdict', () => {
  const stale = { final_review: 'FAIL', final_review_outdated: true, verification_status: 'PASS' };
  assert.equal(RM.runReviewStatus(stale), 'OUTDATED');
  assert.equal(RM.matchRun(stale, { verdict: 'OUTDATED' }, T0), true);
  assert.equal(RM.matchRun(stale, { verdict: 'FAIL' }, T0), false);
  assert.equal(RM.matchRun(stale, { verdict: 'none' }, T0), false);
  const fresh = { final_review: 'FAIL', final_review_outdated: false };
  assert.equal(RM.runReviewStatus(fresh), 'FAIL');
  assert.equal(RM.matchRun(fresh, { verdict: 'FAIL' }, T0), true);
  assert.equal(RM.matchRun(fresh, { verdict: 'OUTDATED' }, T0), false);
  const verifyOnly = { final_review: null, final_review_outdated: null, verification_status: 'PASS' };
  assert.equal(RM.matchRun(verifyOnly, { verdict: 'PASS' }, T0), true);
  assert.equal(RM.matchRun(verifyOnly, { verdict: 'OUTDATED' }, T0), false);
});

test('a live execution model keeps its time axis while every block still fits', () => {
  const X1 = RM.execModel(run(), { now: T0 + 400 });
  const X2 = RM.execModel(run(), { now: T0 + 600, scale: { t0: X1.t0, t1: X1.t1 } });     // unscaled, this poll's axis would be longer
  assert.equal(X2.t1, X1.t1); assert.equal(X2.k, X1.k);
  const X3 = RM.execModel(run(), { now: X1.t1 + 10, scale: { t0: X1.t0, t1: X1.t1 } });
  assert.ok(X3.t1 > X1.t1, 'a block past the axis rescales it');
});

// verification lanes and runs on the finish rows (#33)
const VEXEC = (started, dur, extra = {}) => ({ id: 'verification-1', role: 'verification', started, ended: dur === null ? null : started + dur,
  state: dur === null ? 'running' : 'done', exit_code: dur === null ? null : 1, ...extra });
const vlane = (name, state, runs, extra = {}) => ({ name, state, group: null, first: false, runs: runs.map(([kind, s, e, exit], i) =>
  ({ k: i + 1, kind, started: s === null ? null : T0 + s, ended: e === null ? null : T0 + e, exit })), ...extra });
function lanesRun(state, lanes) {
  return run({ timeline: { started: T0, verifications: [VEXEC(T0 + 800, state === 'running' ? null : 200)] },
    verification: { status: 'PASS', state, mode: 'lanes', runs: [], lanes } });
}

test('lanes mode: a finish row per lane, an item per run at its time, a running lane grows to now', () => {
  const r = lanesRun('running', [
    vlane('unit', 'flaky', [['initial', 810, 840, 1], ['rerun', 850, 870, 0]], { group: 'db', first: true }),
    vlane('e2e', 'running', [['initial', 810, null, null]])]);
  const X = RM.execModel(r, { now: T0 + 900 });
  assert.deepEqual(X.finishLanes.map(f => f.label), ['verify · unit', 'verify · e2e']);
  const v = X.extra.filter(e => e.kind === 'verification');
  assert.deepEqual(v.map(e => [e.id, e.lane, e.start - T0, e.end - T0, e.status, !!e.rerun]),
    [['vlane:unit:1', 'f0', 810, 840, 'FAIL', false], ['vlane:unit:2', 'f0', 850, 870, 'FLAKY', true],
     ['vlane:e2e:1', 'f1', 810, 900, 'RUNNING', false]]);
  assert.equal(v[0].title, 'verify · unit · run 1 · group db · runs first');
  assert.equal(v[1].title, 'verify · unit · rerun 1 · group db · runs first');
  assert.ok(!X.extra.some(e => e.id === 'verification-1'), 'the lanes stand for their execution');
});

test('single mode: runs back to back from the execution start, approximate; no runs is one box', () => {
  const runs = [{ n: 1, kind: 'initial', exit: 1, duration_s: 60, tree_changed: false }, { n: 2, kind: 'rerun', exit: 0, duration_s: 45, tree_changed: false }];
  const r = run({ timeline: { started: T0, verifications: [VEXEC(T0 + 800, 120, { exit_code: 0 })] },
    verification: { status: 'PASS', state: 'done', mode: 'single', runs, lanes: null } });
  const X = RM.execModel(r, { now: T0 + 1000 });
  assert.deepEqual(X.finishLanes.map(f => f.name), ['VERIFY']);
  assert.deepEqual(X.extra.map(e => [e.start - T0, e.end - T0, e.status, e.approx, !!e.rerun]), [[800, 860, 'FAIL', true, false], [860, 905, 'PASS', true, true]]);
  assert.match(X.extra[1].title, /approximate placement/);
  const box = RM.execModel(run({ timeline: { started: T0, verifications: [VEXEC(T0 + 800, 120, { exit_code: 0 })] },
    verification: { status: 'PASS', state: 'done', mode: 'single', runs: [], lanes: null } }), { now: T0 + 1000 }).extra;
  assert.deepEqual(box.map(e => [e.id, e.start - T0, e.end - T0, e.status, !!e.approx]), [['verification-1', 800, 920, 'PASS', false]]);
});

test('a run with no exit is UNKNOWN once ended and RUNNING while it runs, never FAIL', () => {
  const ended = lanesRun('done', [vlane('unit', 'interrupted', [['initial', 810, 830, null]])]);
  assert.equal(RM.execModel(ended, { now: T0 + 2000 }).extra[0].status, 'UNKNOWN');
  const going = lanesRun('running', [vlane('unit', 'running', [['initial', 810, null, null]])]);
  assert.equal(RM.execModel(going, { now: T0 + 900 }).extra[0].status, 'RUNNING');
  const single = run({ timeline: { started: T0, verifications: [VEXEC(T0 + 800, 100, { exit_code: null })] },
    verification: { status: null, state: 'done', mode: 'single', runs: [{ n: 1, kind: 'initial', exit: null, duration_s: 90 }], lanes: null } });
  assert.equal(RM.execModel(single, { now: T0 + 2000 }).extra[0].status, 'UNKNOWN');
  assert.deepEqual(RM.verifyLeds(single), ['UNKNOWN']);
  assert.equal(RM.stateAt(single, T0 + 950).verification, 'UNKNOWN');
});

test('finishState reads RUNNING while the verification runs, whatever a stale status says', () => {
  const r = lanesRun('running', [vlane('unit', 'running', [['initial', 810, null, null]])]);
  assert.equal(r.verification.status, 'PASS');
  const V = RM.finishState(r).verification;
  assert.deepEqual([V.status, V.mode, V.lanes, V.reruns], ['RUNNING', 'lanes', [{ name: 'unit', state: 'running' }], 0]);
  assert.equal(RM.finishState(lanesRun('done', [vlane('unit', 'pass', [['initial', 810, 830, 0]])])).verification.status, 'PASS');
});

test('replay: a lane is pending before its run, running during it, its own state after', () => {
  const r = lanesRun('done', [vlane('unit', 'flaky', [['initial', 810, 840, 1], ['rerun', 850, 870, 0]]), vlane('e2e', 'fail', [['initial', 815, 860, 2]])]);
  const at = t => RM.stateAt(r, T0 + t).lanes.map(l => l.status);
  assert.deepEqual(at(805), ['PENDING', 'PENDING']);
  assert.deepEqual(at(820), ['RUNNING', 'RUNNING']);
  assert.deepEqual(at(845), ['FAIL', 'RUNNING']);
  assert.deepEqual(at(880), ['FLAKY', 'FAIL']);
  assert.deepEqual(RM.verifyLeds(r, RM.stateAt(r, T0 + 820)), ['RUNNING', 'RUNNING']);
  assert.deepEqual(RM.verifyLeds(r, RM.stateAt(r, T0 + 805)), [null, null]);
  assert.deepEqual(RM.verifyLeds(r), ['FLAKY', 'FAIL']);
  assert.equal(RM.stateAt(run(), T0).lanes, null);
});

test('the finding sweep reads as one line, and each swept finding as a lamp', () => {
  const sweep = { tasks: ['sweep-1', 'sweep-2'], findings: 3, fixed: 2, dropped: [{}, {}] };
  assert.equal(RM.sweepLine(sweep), '2 fix tasks · 2 of 3 findings fixed · 3 left for forge issues');
  assert.equal(RM.sweepLine({ tasks: ['sweep-1'], findings: 1, fixed: 1, dropped: [] }), '1 fix task · 1 of 1 finding fixed · 0 left for forge issues');
  assert.equal(RM.sweepLine({ tasks: [], findings: 0, fixed: 0, dropped: [{}] }), 'nothing to fix; 1 left for forge issues');
  assert.equal(RM.sweepLine(null), null);
  assert.deepEqual(['fixed', 'not-fixed', 'unreported', null].map(s => RM.sweepState(s).lamp), ['pass', 'fail', 'unknown', 'pending']);
});
