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
