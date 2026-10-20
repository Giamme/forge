'use strict';
// node --test: quick-jump ranking and the status changes that notify.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const RM = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'runmodel.js'));

const NOW = 1760700000;
const run = (key, o = {}) => ({ key, run_id: 'r-' + key, repo: '/w/' + (o.repo || 'forge'), goal: o.goal || 'Goal ' + key, kind: o.kind || 'plan', updated: o.updated ?? NOW - 60,
  task_count: (o.tasks || []).length, counts: o.counts || { MERGED: 1 }, final_review: o.final_review ?? null, models: o.models || ['sol:high'],
  task_statuses: (o.tasks || []).map(([id, status, title]) => ({ id, status, title, difficulty: 'medium' })) });

test('runs and tasks are indexed with links; solo runs only as runs', () => {
  const idx = RM.searchIndex([run('aaaaaaaaaaaa', { tasks: [['reader', 'MERGED', 'Speed up the reader']] }), run('bbbbbbbbbbbb', { kind: 'solo', tasks: [['solo', 'MERGED', 'x']] })]);
  assert.deepEqual(idx.map(e => e.kind), ['run', 'task', 'run']);
  assert.equal(idx[1].href, '#/runs/aaaaaaaaaaaa/tasks/reader');
  assert.equal(idx[0].href, '#/runs/aaaaaaaaaaaa');
});

test('ranking: exact ids first, then prefixes and word starts, then loose matches; every token must match', () => {
  const idx = RM.searchIndex([
    run('aaaaaaaaaaaa', { goal: 'Make the reader faster', tasks: [['reader', 'MERGED', 'Speed up the reader'], ['page', 'FAIL', 'Render the page']] }),
    run('bbbbbbbbbbbb', { goal: 'Bread recipes', tasks: [['bread', 'MERGED', 'Knead']] }),
  ]);
  const top = RM.searchRank(idx, 'reader', { now: NOW });
  assert.equal(top[0].id, 'task:aaaaaaaaaaaa:reader', 'the exact task id wins');
  assert.ok(top.some(e => e.id === 'run:aaaaaaaaaaaa'));
  assert.ok(!top.some(e => e.id === 'task:bbbbbbbbbbbb:bread'));
  assert.ok(!top.some(e => e.id === 'task:aaaaaaaaaaaa:page'), 'a task is not found through its run\'s goal');
  assert.equal(RM.searchRank(idx, 'r-aaaa page', { now: NOW })[0].id, 'task:aaaaaaaaaaaa:page', 'but through its run id with its own words');
  assert.deepEqual(top.find(e => e.id === 'run:aaaaaaaaaaaa').mark, [9, 15]);
  assert.deepEqual(top[0].mark, [0, 6], 'the label is marked where the text matched');
  assert.ok(RM.searchRank(idx, 'rdr', { now: NOW }).length > 0, 'a subsequence still finds it');
  assert.deepEqual(RM.searchRank(idx, 'reader bread', { now: NOW }).map(e => e.id), []);
  assert.equal(RM.searchRank(idx, 'r-bbbb', { now: NOW })[0].id, 'run:bbbbbbbbbbbb', 'run ids are searchable');
});

test('an empty query lists recent picks first, then live and recent runs', () => {
  const idx = RM.searchIndex([run('aaaaaaaaaaaa', { updated: NOW - 86400 * 30 }), run('bbbbbbbbbbbb', { counts: { RUNNING: 1 } }), run('cccccccccccc')]);
  const top = RM.searchRank(idx, '', { now: NOW, recent: { 'run:aaaaaaaaaaaa': NOW - 10 } });
  assert.deepEqual(top.slice(0, 3).map(e => e.key), ['aaaaaaaaaaaa', 'bbbbbbbbbbbb', 'cccccccccccc']);
});

test('status events: finished and failed runs, failing tasks, review verdicts; the first poll and stale runs are quiet', () => {
  const a0 = run('aaaaaaaaaaaa', { counts: { RUNNING: 1, MERGED: 1 }, tasks: [['t1', 'RUNNING'], ['t2', 'MERGED']] });
  const a1 = run('aaaaaaaaaaaa', { counts: { FAIL: 1, MERGED: 1 }, tasks: [['t1', 'FAIL'], ['t2', 'MERGED']], final_review: 'FAIL' });
  const b0 = run('bbbbbbbbbbbb', { counts: { RUNNING: 1 }, tasks: [['t1', 'RUNNING']] });
  const b1 = run('bbbbbbbbbbbb', { counts: { MERGED: 1 }, tasks: [['t1', 'MERGED']] });
  const c0 = run('cccccccccccc', { counts: { RUNNING: 1 }, tasks: [['t1', 'RUNNING']], updated: NOW - 9000 });
  const c1 = run('cccccccccccc', { counts: { FAIL: 1 }, tasks: [['t1', 'FAIL']], updated: NOW - 9000 });
  const d1 = run('dddddddddddd', { counts: { FAIL: 1 }, tasks: [['t1', 'FAIL']] });
  assert.deepEqual(RM.statusEvents(null, [a1, b1], { now: NOW }), []);
  const ev = RM.statusEvents([a0, b0, c0], [a1, b1, c1, d1], { now: NOW });
  assert.deepEqual(ev.map(e => e.kind).sort(), ['review', 'run-done', 'run-failed', 'task-failed']);
  const tf = ev.find(e => e.kind === 'task-failed');
  assert.equal(tf.href, '#/runs/aaaaaaaaaaaa/tasks/t1'); assert.equal(tf.status, 'FAIL');
  assert.ok(ev.every(e => e.id && e.key !== 'cccccccccccc' && e.key !== 'dddddddddddd'));
  const infra = run('aaaaaaaaaaaa', { counts: { RUNNING: 1 }, tasks: [['t1', 'INFRA']] });
  assert.deepEqual(RM.statusEvents([a0], [infra], { now: NOW }), [], 'INFRA retries are not failures');
});

test('a Fractal tree: depth across, leaves in order, parents centred, cycles and orphans survive', () => {
  const T = RM.fractalTree([{ id: 'root' }, { id: 'a', parent: 'root' }, { id: 'b', parent: 'root' }, { id: 'b1', parent: 'b' }, { id: 'b2', parent: 'b' },
    { id: 'x', parent: 'y' }, { id: 'y', parent: 'x' }, { id: 'lost', parent: 'nowhere' }]);
  assert.deepEqual([T.pos.a.row, T.pos.b1.row, T.pos.b2.row], [0, 1, 2]);
  assert.equal(T.pos.b.row, 1.5); assert.equal(T.pos.root.row, .75);
  assert.equal(T.pos.b1.depth, 2); assert.equal(T.cols, 3);
  assert.deepEqual(T.edges.filter(([p]) => p === 'root').map(e => e[1]), ['a', 'b']);
  for (const id of ['x', 'y', 'lost']) assert.ok(T.pos[id], id + ' is placed');
  assert.deepEqual(RM.fractalTree(null), { pos: {}, edges: [], cols: 0, rows: 0 });
});
