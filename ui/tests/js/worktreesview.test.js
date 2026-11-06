'use strict';
// node --test: the Worktrees screen's pure logic (worktreesview.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const W = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'worktreesview.js'));

const row = (path, extra = {}) => ({ path, repo: '/r/app', run: 'k1', status: 'finished', kind: 'task', size: null, ...extra });
const rows = [
  row('/r/.forge-worktrees/r1/a', { size: 100 }),
  row('/r/.forge-worktrees/r1/b', { status: 'failed', size: 50 }),
  row('/r/.forge-worktrees/r1/_integration', { status: 'needed', kind: 'integration' }),
  row('/s/.forge-worktrees/r2/c', { repo: '/s/lib', run: 'k2', status: 'removed', size: 999 }),
  row('/s/.forge-worktrees/r2/gone', { repo: '/s/lib', run: 'k2', status: 'missing', size: 7 }),
  row('/s/.forge-worktrees/mine', { repo: '/s/lib', run: null, status: 'foreign', kind: null, size: 1 }),
];
const paths = list => list.map(r => r.path.split('/').pop());

test('filters: repository, run, status, on disk and kind each narrow the rows, and combine', () => {
  assert.deepEqual(paths(W.filterRows(rows, {})), ['a', 'b', '_integration', 'c', 'gone', 'mine']);
  assert.deepEqual(paths(W.filterRows(rows, { repo: '/s/lib' })), ['c', 'gone', 'mine']);
  assert.deepEqual(paths(W.filterRows(rows, { run: 'k1' })), ['a', 'b', '_integration']);
  assert.deepEqual(paths(W.filterRows(rows, { status: 'failed' })), ['b']);
  assert.deepEqual(paths(W.filterRows(rows, { status: 'disk' })), ['a', 'b', '_integration', 'gone', 'mine']);
  assert.deepEqual(paths(W.filterRows(rows, { kind: 'integration' })), ['_integration']);
  assert.deepEqual(paths(W.filterRows(rows, { kind: 'none' })), ['mine']);
  assert.deepEqual(paths(W.filterRows(rows, { repo: '/s/lib', status: 'disk' })), ['gone', 'mine']);
});

test('totals: disk use counts only folders on disk whose size is known, and says how many are still measured', () => {
  assert.deepEqual(W.totals(rows), { count: 6, onDisk: 4, bytes: 151, pending: 1 });
  assert.deepEqual(W.totals([]), { count: 0, onDisk: 0, bytes: 0, pending: 0 });
});

test('sizes read in binary units, and an unknown size as a dash', () => {
  assert.equal(W.humanBytes(0), '0 B');
  assert.equal(W.humanBytes(1536), '1.5 KB');
  assert.equal(W.humanBytes(8.5 * 1024 ** 3), '8.5 GB');
  assert.equal(W.humanBytes(300 * 1024 ** 2), '300 MB');
  assert.equal(W.humanBytes(null), '—');
});

test('a path is shown from .forge-worktrees on, and any other path whole', () => {
  assert.equal(W.shortPath('/Users/me/dev/.forge-worktrees/r1/a b'), '.forge-worktrees/r1/a b');
  assert.equal(W.shortPath('/elsewhere/x'), '/elsewhere/x');
  assert.equal(W.repoName('/Users/me/dev/app/'), 'app');
});
