'use strict';
// node --test: the Projects screen's pure logic (projectsview.js), and the project id it shares with the cloud.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const P = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'projectsview.js'));
global.self = global; global.window = global;
require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'runmodel.js'));
const C = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'cloudmodel.js'));

test('the project id is the cloud\'s, and the server computes the same (test_projects.IdTests)', () => {
  assert.equal(C.hashId('/Users/ayoub/dev/wizaird/forge'), 'f94eba50');
  assert.equal(C.hashId('/tmp/rèpo ✓'), 'eaa95eb2');
  assert.equal(P.routeOf('f94eba50'), '#/projects/f94eba50');
  assert.equal(P.routeOf('f94eba50', 'memories'), '#/projects/f94eba50/memories');
});

const f = (summary, extra = {}) => ({ summary, severity: 'P2', status: 'open', kind: 'primary', run_id: 'r1', reviewer: 'opus', location: 'a.py:1', at: 1000, ...extra });
const rows = [
  f('one'), f('two', { severity: 'P0' }), f('three', { status: 'superseded' }), f('four', { kind: 'shadow' }),
  f('five', { run_id: 'r2', reviewer: 'sol', location: 'lib/b.py:9', at: 10 }), f('six', { status: 'dismissed' }),
];
const names = list => list.map(r => r.summary);

test('findings: open and no shadow reviews by default; each filter narrows', () => {
  assert.deepEqual(names(P.filterFindings(rows, {}, 2000)), ['one', 'two', 'five']);
  assert.deepEqual(names(P.filterFindings(rows, { status: 'all' }, 2000)), ['one', 'two', 'three', 'five', 'six']);
  assert.deepEqual(names(P.filterFindings(rows, { shadow: true }, 2000)), ['one', 'two', 'four', 'five']);
  assert.deepEqual(names(P.filterFindings(rows, { status: 'superseded' }, 2000)), ['three']);
  assert.deepEqual(names(P.filterFindings(rows, { severity: 'P0' }, 2000)), ['two']);
  assert.deepEqual(names(P.filterFindings(rows, { run: 'r2' }, 2000)), ['five']);
  assert.deepEqual(names(P.filterFindings(rows, { reviewer: 'sol' }, 2000)), ['five']);
  assert.deepEqual(names(P.filterFindings(rows, { path: 'LIB/' }, 2000)), ['five']);
  assert.deepEqual(names(P.filterFindings(rows, { since: 1 }, 1000 + 86400 - 1)), ['one', 'two']);   // five is older than a day
});

test('a summary drops what its columns already show', () => {
  assert.equal(P.cleanSummary('- [P1][CONFIRMED] ui/a.py:14 — the bug', 'ui/a.py:14'), 'the bug');
  assert.equal(P.cleanSummary('- **[P2][CONFIRMED] `x/y.py:1-3` against z', 'x/y.py:1-3'), 'against z');
  assert.equal(P.cleanSummary('- S2: NOT FIXED — a.js:2 still', 'a.js:2'), 'S2: NOT FIXED — a.js:2 still');
  assert.equal(P.cleanSummary('- [P3] only tags', null), 'only tags');
});

test('a learning\'s state is the picked checkout\'s, else another checkout\'s, else gone', () => {
  const l = { states: { '/root': 'in_memory', '/side': 'capped' } };
  assert.deepEqual(P.stateFor(l, '/side'), { state: 'capped', from: null });
  assert.deepEqual(P.stateFor({ states: { '/side': 'waiting' } }, '/root'), { state: 'waiting', from: '/side' });
  assert.deepEqual(P.stateFor({ states: {} }, '/root'), { state: 'gone', from: 'dataset' });
  const all = [{ category: 'trap', text: 'Alpha', states: { '/root': 'in_memory' } }, { category: 'verify', text: 'beta', states: { '/root': 'stale' } }];
  assert.deepEqual(P.filterLearnings(all, { state: 'stale' }, '/root').map(x => x.text), ['beta']);
  assert.deepEqual(P.filterLearnings(all, { text: 'alp' }, '/root').map(x => x.text), ['Alpha']);
  assert.deepEqual(P.filterLearnings(all, { category: 'trap' }, '/root').map(x => x.text), ['Alpha']);
});

test('the Memories tab says why it is empty', () => {
  assert.match(P.memoryEmpty({ runs: 2, memory_off_runs: 2, learnings: [], sources: [] }), /memory turned off/);
  assert.match(P.memoryEmpty({ runs: 2, memory_off_runs: 0, learnings: [], sources: [{ memory: null }] }), /No memories yet/);
  assert.equal(P.memoryEmpty({ runs: 2, memory_off_runs: 0, learnings: [{}], sources: [] }), null);
});
