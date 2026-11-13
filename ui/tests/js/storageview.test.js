'use strict';
// node --test: the Disk screen's pure logic (storageview.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const S = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'storageview.js'));

test('summary adds run dirs, worktrees and the data home, and says what can go', () => {
  const data = { runs: [{}, {}], worktrees: [{}], data_home: { bytes: 10 },
    totals: { runs: 2048, worktrees: 1024, data_home: 10, reclaimable: 1500 } };
  const s = S.summary(data);
  assert.equal(s.total, 3082);
  assert.equal(s.reclaimable, 1500);
  assert.equal(s.text, '2.0 KB in 2 run dirs · 1.0 KB in 1 worktree folders · 10 B in the data home');
  assert.equal(S.summary({ runs: [], worktrees: [], totals: {} }).text, '0 B in 0 run dirs · 0 B in 0 worktree folders');
});

test('the data home lists its non-empty parts, largest first, with what each is for', () => {
  const parts = S.homeParts({ parts: { dataset: 5, blobs: 50, exports: 20, legacy: 0, other: 1 } });
  assert.deepEqual(parts.map(p => p.key), ['blobs', 'exports', 'dataset', 'other']);
  assert.match(parts[2].label, /kept/);
  assert.deepEqual(S.homeParts(null), []);
});

test('sizes read like du', () => {
  assert.equal(S.humanBytes(0), '0 B');
  assert.equal(S.humanBytes(1536), '1.5 KB');
  assert.equal(S.humanBytes(22 * 2 ** 30), '22 GB');
  assert.equal(S.humanBytes(-1), '—');
});

const DATA = {
  runs: [
    { repo: '/r/app', snapshots: 600, logs: 50, gz: 10, other: 30, fractal: 10, strays: 0 },
    { repo: '/r/app', snapshots: 100, logs: 0, gz: 0, other: 0, fractal: 0, strays: 200 },
    { repo: '/r/lib', snapshots: 0, logs: 5, gz: 0, other: 5, fractal: 0, strays: 0 },
    { repo: null, snapshots: 1, logs: 0, gz: 0, other: 0, fractal: 0, strays: 0 },
  ],
  worktrees: [{ repo: '/r/lib', bytes: 2000 }],
  data_home: { bytes: 89 },
  totals: { worktrees: 2000 },
};

test('the breakdown keeps every part in its fixed order and adds up to the whole', () => {
  const parts = S.breakdown(DATA);
  assert.deepEqual(parts.map(p => p.key), ['snapshots', 'logs', 'other', 'strays', 'worktrees', 'home']);
  assert.deepEqual(parts.map(p => p.bytes), [701, 65, 45, 200, 2000, 89]);
  assert.equal(Math.round(parts.reduce((n, p) => n + p.share, 0) * 1000), 1000);
  assert.deepEqual(parts.map(p => p.color), ['var(--disk-1)', 'var(--disk-2)', 'var(--disk-3)', 'var(--disk-4)', 'var(--disk-5)', 'var(--disk-6)']);
  assert.ok(S.breakdown({}).every(p => p.bytes === 0 && p.share === 0));
});

test('repositories add their run dirs and worktrees, largest first, and a run with no repository is named so', () => {
  const repos = S.byRepo(DATA);
  assert.deepEqual(repos.map(g => [g.name, g.total]), [['lib', 2010], ['app', 1000], ['unknown repository', 1]]);
  assert.deepEqual(repos[1].parts, { snapshots: 700, logs: 60, other: 40, strays: 200, worktrees: 0 });
  assert.equal(repos[0].parts.worktrees, 2000);
});

test('shares read as whole percents, with the smallest never shown as 0', () => {
  assert.deepEqual([0, 0.004, 0.05, 0.123, 0.999].map(S.pct), ['0%', '<1%', '5%', '12%', '100%']);
});
