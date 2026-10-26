'use strict';
// node --test: the open-findings view's pure logic (findingsview.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const F = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'findingsview.js'));

const row = (fp, extra = {}) => ({ fp, severity: 'P2', confidence: 'high', origin: 'review', plan: 'p', task: null, file: 'a.py', summary: 's' + fp, filed: null, dismissed: null, ...extra });
const fps = n => Array.from({ length: n }, (_, i) => String(i).padStart(12, '0'));

test('status: open, filed while OPEN, fixed when CLOSED, dismissed', () => {
  assert.equal(F.statusOf(row('a')), 'open');
  assert.equal(F.statusOf(row('a', { filed: { url: 'u', state: 'OPEN' } })), 'filed');
  assert.equal(F.statusOf(row('a', { filed: { url: 'u', state: 'CLOSED' } })), 'fixed');
  assert.equal(F.statusOf(row('a', { filed: { url: 'u' } })), 'filed');
  assert.equal(F.statusOf(row('a', { dismissed: { at: 't', reason: 'dup' } })), 'dismissed');
  assert.equal(F.statusOf(row('a', { dismissed: { at: 't' }, filed: { url: 'u', state: 'OPEN' } })), 'dismissed');
});

test('filters: open by default, then status, severity and origin', () => {
  const rows = [row('a'), row('b', { severity: 'P0' }), row('c', { origin: 'plausible' }),
    row('d', { filed: { url: 'u', state: 'OPEN' } }), row('e', { dismissed: { at: 't' } })];
  assert.deepEqual(F.filterRows(rows).map(r => r.fp), ['a', 'b', 'c']);
  assert.deepEqual(F.filterRows(rows, { status: 'all' }).map(r => r.fp), ['a', 'b', 'c', 'd', 'e']);
  assert.deepEqual(F.filterRows(rows, { status: 'dismissed' }).map(r => r.fp), ['e']);
  assert.deepEqual(F.filterRows(rows, { dismissed: true }).map(r => r.fp), ['a', 'b', 'c', 'e']);
  assert.deepEqual(F.filterRows(rows, { status: 'all', dismissed: false }).map(r => r.fp), ['a', 'b', 'c', 'd']);
  assert.deepEqual(F.filterRows(rows, { severity: 'P0' }).map(r => r.fp), ['b']);
  assert.deepEqual(F.filterRows(rows, { origin: 'plausible' }).map(r => r.fp), ['c']);
  assert.deepEqual(F.filterRows(null), []);
});

test('merging online data fills filed by fingerprint and keeps offline-only rows', () => {
  const off = [row('a'), row('b'), row('c')];
  const on = [row('a', { filed: { url: 'https://x/1', state: 'OPEN' } }), row('b', { filed: null }), row('z')];
  const out = F.mergeFiled(off, on);
  assert.deepEqual(out.map(r => r.fp), ['a', 'b', 'c', 'z']);
  assert.equal(F.statusOf(out[0]), 'filed');
  assert.equal(out[0].summary, 'sa');
  assert.equal(F.statusOf(out[1]), 'open');
  assert.equal(out[2], off[2]);
  assert.equal(off[0].filed, null);
});

test('selection rules are per action', () => {
  const open = fps(26).map(f => row(f));
  const dis = row('dis', { dismissed: { at: 't' } }), filed = row('fil', { filed: { url: 'u', state: 'OPEN' } });
  assert.equal(F.selectable('file', open[0]), true);
  assert.equal(F.selectable('dismiss', open[0]), true);
  assert.equal(F.selectable('undismiss', open[0]), false);
  assert.equal(F.selectable('undismiss', dis), true);
  assert.equal(F.selectable('file', dis), false);
  assert.equal(F.selectable('dismiss', filed), false);
  assert.equal(F.selectable('bogus', open[0]), false);
  const rows = [...open, dis, filed];
  const pick = n => new Set(open.slice(0, n).map(r => r.fp));
  assert.equal(F.selection('file', rows, pick(25)).enabled, true);
  assert.equal(F.selection('file', rows, pick(25)).count, 25);
  const over = F.selection('file', rows, pick(26));
  assert.equal(over.enabled, false);
  assert.match(over.note, /at most 25/);
  assert.equal(F.selection('file', rows, new Set()).enabled, false);
  const mixed = new Set(['dis', 'fil', open[0].fp]);
  assert.deepEqual(F.selection('file', rows, mixed).fps, [open[0].fp]);
  assert.deepEqual(F.selection('undismiss', rows, mixed).fps, ['dis']);
  assert.equal(F.selection('undismiss', rows, new Set([open[0].fp])).enabled, false);
});

test('request URLs', () => {
  assert.equal(F.issuesUrl('abc', { scope: 'run', offline: true }), '/api/runs/abc/issues?scope=run&offline=1&plausible=0');
  assert.equal(F.issuesUrl('abc', { scope: 'repo', offline: false, plausible: true, dismissed: true }),
    '/api/runs/abc/issues?scope=repo&offline=0&plausible=1&dismissed=1');
  assert.equal(F.issuesUrl('abc', { scope: 'run', offline: false, only: ['0123456789ab', 'ba9876543210'] }),
    '/api/runs/abc/issues?scope=run&offline=0&plausible=0&only=0123456789ab,ba9876543210');
  assert.equal(F.issuesUrl('a/b', { scope: 'x' }), '/api/runs/a%2Fb/issues?scope=run&offline=0&plausible=0');
  assert.equal(F.issuesPath('k'), '/api/runs/k/issues');
  assert.equal(F.dismissPath('k'), '/api/runs/k/issues/dismiss');
});

test('request bodies', () => {
  assert.deepEqual(F.fileBody('repo', ['a'], true, ['bug']), { scope: 'repo', fingerprints: ['a'], include_plausible: true, labels: ['bug'] });
  assert.deepEqual(F.fileBody('weird', ['a']), { scope: 'run', fingerprints: ['a'], include_plausible: false, labels: [] });
  assert.deepEqual(F.dismissBody(['a'], '  dup  ', false), { fingerprints: ['a'], reason: 'dup' });
  assert.deepEqual(F.dismissBody(['a'], '   ', false), { fingerprints: ['a'] });
  assert.deepEqual(F.dismissBody(['a'], 'ignored', true), { fingerprints: ['a'], undo: true });
  assert.equal(F.dismissBody(['a'], 'x'.repeat(600), false).reason.length, 500);
});

test('labels: at most 5, 1 to 50 characters, no leading dash, no control characters', () => {
  assert.deepEqual(F.parseLabels(' bug , forge ,, '), { labels: ['bug', 'forge'], error: null });
  assert.deepEqual(F.parseLabels(''), { labels: [], error: null });
  assert.ok(F.parseLabels('a,b,c,d,e,f').error);
  assert.ok(F.parseLabels('x'.repeat(51)).error);
  assert.ok(F.parseLabels('-bad').error);
  assert.ok(F.parseLabels('a\u0007b').error);
});

test('origin filter offers accepted, and an outdated review carries a hollow lamp', () => {
  assert.ok(F.ORIGINS.includes('accepted'));
  const rows = [row('a', { origin: 'accepted' }), row('b', { origin: 'tolerated' }), row('c', { origin: 'review' })];
  assert.deepEqual(F.filterRows(rows, { origin: 'accepted' }).map(r => r.fp), ['a']);
  assert.equal(F.reviewMarker(row('c')), null);
  assert.equal(F.reviewMarker(row('c', { review: null })), null);
  assert.equal(F.reviewMarker(row('c', { review: { name: 'final-review-1-1', outdated: false, merged_since: ['page'] } })), null);
  assert.equal(F.reviewMarker(row('c', { review: { outdated: null, merged_since: [] } })), null);

  const one = F.reviewMarker(row('c', { origin: 'review', review: { name: 'final-review-1-1', outdated: true, merged_since: ['page'] } }));
  assert.equal(one.lamp, 'st-outdated');
  assert.equal(one.label, 'outdated review');
  assert.equal(one.sentence, 'The review is outdated: 1 task merged into the integration branch after it (page), so check whether they fixed it first.');
  assert.equal(one.title, one.sentence);

  const two = F.reviewMarker(row('c', { review: { outdated: true, merged_since: ['page', 'reader'] } }));
  assert.equal(two.sentence, 'The review is outdated: 2 tasks merged into the integration branch after it (page, reader), so check whether they fixed it first.');
  assert.match(two.title, /page/);
  assert.match(two.title, /reader/);

  const none = F.reviewMarker(row('c', { review: { outdated: true, merged_since: [] } }));
  assert.equal(none.sentence, 'The review is outdated: 0 tasks merged into the integration branch after it, so check whether they fixed it first.');

  const many = F.reviewMarker(row('c', { review: { outdated: true, merged_since: ['a', 'b', 'c', 'd', 'e', 'f'] } }));
  assert.equal(many.sentence, 'The review is outdated: 6 tasks merged into the integration branch after it (a, b, c, d, e, +1 more), so check whether they fixed it first.');
  assert.equal(many.title, many.sentence);
});

test('results and links', () => {
  const lines = F.resultLines([{ title: 'T', url: 'https://g/1' }, { title: 'U', skipped: 'same title', issue: 'https://g/2' }, { note: 'labels left off' }]);
  assert.deepEqual(lines.map(l => l.kind), ['created', 'skipped', 'note']);
  assert.equal(lines[1].url, 'https://g/2');
  assert.equal(F.safeUrl('https://github.com/o/r/issues/1'), 'https://github.com/o/r/issues/1');
  assert.equal(F.safeUrl('javascript:alert(1)'), null);
  assert.equal(F.safeUrl('http://x'), null);
  assert.equal(F.safeUrl(null), null);
});

test('a POST that created nothing shows the CLI notes, one that created something does not', () => {
  const note = '--only 0123456789ab: no such open finding';
  assert.deepEqual(F.emptyNotes([], [note]), [note]);
  assert.deepEqual(F.emptyNotes(undefined, [note, '', 7]), [note, '7']);
  assert.deepEqual(F.emptyNotes([{ skipped: 'same title', title: 'T' }], [note]), [note]);
  assert.deepEqual(F.emptyNotes([{ title: 'T', url: 'https://g/1' }], [note]), []);
  assert.deepEqual(F.emptyNotes([], null), []);
});

test('an action takes the selected rows the filters leave shown', () => {
  const rows = [row('a'), row('b', { severity: 'P0' }), row('d', { dismissed: { at: 't' } }), row('e', { dismissed: { at: 't' } })];
  const selected = new Set(['a', 'b', 'd', 'e']);
  const shown = f => F.filterRows(rows, f);
  assert.deepEqual(F.selection('undismiss', shown({ dismissed: true }), selected).fps, ['d', 'e']);
  assert.equal(F.selection('undismiss', shown({ dismissed: false }), selected).enabled, false);
  assert.equal(F.selection('undismiss', shown({ dismissed: false }), selected).count, 0);
  assert.deepEqual(F.selection('file', shown({ severity: 'P0' }), selected).fps, ['b']);
  assert.deepEqual(F.selection('dismiss', shown({ severity: 'P3' }), selected).fps, []);
});

test('select shown open caps the shown open rows, not a selection the filter hides', () => {
  const hidden = new Set(fps(F.MAX_FPS));
  const p0 = [row('p0p0p0p0p0p0', { severity: 'P0' }), row('p0p0p0p0p0p1', { severity: 'P0' })];
  assert.deepEqual(F.selectShownOpen(p0, hidden), ['p0p0p0p0p0p0', 'p0p0p0p0p0p1']);
  assert.equal(hidden.size, F.MAX_FPS);

  const open = fps(F.MAX_FPS + 1).map(fp => row(fp));
  assert.deepEqual(F.selectShownOpen(open, new Set()), open.slice(0, F.MAX_FPS).map(r => r.fp));
  const partial = new Set(open.slice(0, 10).map(r => r.fp));
  assert.deepEqual(F.selectShownOpen(open, partial), open.slice(10, F.MAX_FPS).map(r => r.fp));
  assert.equal(partial.size, 10);
  assert.deepEqual(F.selectShownOpen(open, new Set(open.slice(0, F.MAX_FPS).map(r => r.fp))), []);

  // A shown dismissed row is not open, so it is neither added nor counted: File would not take it.
  const dismissed = fps(F.MAX_FPS).map(fp => row(fp, { dismissed: { at: 't' } }));
  const one = row('oooooooooooo');
  assert.deepEqual(F.selectShownOpen(dismissed.concat([one]), new Set(dismissed.map(r => r.fp))), ['oooooooooooo']);
  assert.deepEqual(F.selectShownOpen([row('dddddddddddd', { dismissed: { at: 't' } }), one], new Set()), ['oooooooooooo']);
});
