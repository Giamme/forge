'use strict';
// node --test: the Findings tab's pure action logic (findingsactions.js): bodies and their errors, the copyable command,
// what a selection allows, tags, and which rows File selected takes.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const A = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'findingsactions.js'));

const FP = 'aaaaaaaaaaaa', FP2 = 'bbbbbbbbbbbb';
const row = (fp, extra = {}) => ({ record_fp: fp, fp: 'i' + fp, run: 'run1', status: 'open', fileable: true, tags: ['backend'], record: null, ...extra });

test('fix needs a commit or a reason, and a commit is lowercase hex', () => {
  assert.deepEqual(A.bodyFor('fix', [FP], { commit: ' ABC1234 ' }), { body: { fps: [FP], commit: 'abc1234' } });
  assert.deepEqual(A.bodyFor('fix', [FP], { reason: 'done by hand' }), { body: { fps: [FP], reason: 'done by hand' } });
  assert.deepEqual(A.bodyFor('fix', [FP, FP2], { commit: 'abc1234', reason: 'x' }).body, { fps: [FP, FP2], commit: 'abc1234', reason: 'x' });
  assert.match(A.bodyFor('fix', [FP], {}).error, /commit or a reason/);
  assert.match(A.bodyFor('fix', [FP], { commit: 'xyz' }).error, /hexadecimal/);
  assert.match(A.bodyFor('fix', [FP], { reason: 'r'.repeat(501) }).error, /at most 500/);
});

test('dismiss needs a reason', () => {
  assert.deepEqual(A.bodyFor('dismiss', [FP, FP2], { reason: ' not a bug ' }), { body: { fps: [FP, FP2], reason: 'not a bug' } });
  assert.match(A.bodyFor('dismiss', [FP], { reason: '   ' }).error, /reason is required/);
  assert.match(A.bodyFor('dismiss', [FP], { reason: 'a\u0000b' }).error, /control/);
  assert.match(A.bodyFor('dismiss', [FP], { reason: 'r'.repeat(501) }).error, /at most 500/);
});

test('reopen and to-file carry only what was given', () => {
  assert.deepEqual(A.bodyFor('reopen', [FP], { reason: 'ignored' }), { body: { fps: [FP] } });
  assert.deepEqual(A.bodyFor('to-file', [FP], {}), { body: { fps: [FP] } });
  assert.deepEqual(A.bodyFor('to-file', [FP], { title: ' Fix it ' }), { body: { fps: [FP], title: 'Fix it' } });
  assert.match(A.bodyFor('to-file', [FP], { title: 't'.repeat(201) }).error, /at most 200/);
});

test('dup takes one finding and another one\'s fingerprint', () => {
  assert.deepEqual(A.bodyFor('dup', [FP], { of: FP2 }), { body: { fps: [FP], of: FP2 } });
  assert.match(A.bodyFor('dup', [FP], { of: 'nope' }).error, /pick the finding/);
  assert.match(A.bodyFor('dup', [FP], { of: FP }).error, /itself/);
  assert.match(A.bodyFor('dup', [FP, FP2], { of: 'cccccccccccc' }).error, /one finding/);
});

test('tag parses a list and says why a tag is wrong', () => {
  assert.deepEqual(A.parseTags('a, b  c,a'), { tags: ['a', 'b', 'c'], error: null });
  assert.match(A.parseTags('').error, /at least one/);
  assert.match(A.parseTags('Bad').error, /"Bad" is not a tag/);
  assert.match(A.parseTags('1x').error, /not a tag/);
  assert.match(A.parseTags('a'.repeat(25)).error, /not a tag/);
  assert.equal(A.parseTags('a'.repeat(24)).error, null);
  assert.match(A.parseTags('a b c d e f g h i').error, /at most 8/);
  assert.deepEqual(A.bodyFor('tag', [FP], { tags: 'ui, flaky' }), { body: { fps: [FP], tags: ['ui', 'flaky'], remove: false } });
  assert.deepEqual(A.bodyFor('tag', [FP], { tags: ['ui'], remove: true }).body, { fps: [FP], tags: ['ui'], remove: true });
  assert.match(A.bodyFor('tag', [FP], { tags: 'Nope' }).error, /not a tag/);
});

test('note takes one finding, and empty text clears it', () => {
  assert.deepEqual(A.bodyFor('note', [FP], { text: ' look here ' }), { body: { fps: [FP], text: 'look here' } });
  assert.deepEqual(A.bodyFor('note', [FP], { text: '' }), { body: { fps: [FP], text: '' } });
  assert.match(A.bodyFor('note', [FP], { text: 'n'.repeat(2001) }).error, /at most 2000/);
  assert.match(A.bodyFor('note', [FP, FP2], { text: 'x' }).error, /one finding/);
});

test('edit takes a severity, a text, or both; an empty text clears', () => {
  assert.deepEqual(A.bodyFor('edit', [FP], { severity: 'P1' }), { body: { fps: [FP], severity: 'P1' } });
  assert.deepEqual(A.bodyFor('edit', [FP], { severity: '', text: 'new' }), { body: { fps: [FP], text: 'new' } });
  assert.deepEqual(A.bodyFor('edit', [FP], { severity: 'P0', text: '' }), { body: { fps: [FP], severity: 'P0', text: '' } });
  assert.match(A.bodyFor('edit', [FP], {}).error, /severity or a text/);
  assert.match(A.bodyFor('edit', [FP], { severity: 'P4' }).error, /P0, P1, P2 or P3/);
  assert.match(A.bodyFor('edit', [FP], { text: 'x'.repeat(4001) }).error, /at most 4000/);
});

test('a body needs 1 to 200 fingerprints and a known op', () => {
  assert.match(A.bodyFor('dismiss', [], { reason: 'x' }).error, /select a finding/);
  assert.match(A.bodyFor('dismiss', Array.from({ length: 201 }, (_, i) => 'f' + i), { reason: 'x' }).error, /at most 200/);
  assert.equal(A.bodyFor('dismiss', Array.from({ length: 200 }, (_, i) => 'f' + i), { reason: 'x' }).body.fps.length, 200);
  assert.deepEqual(A.bodyFor('reopen', [FP, FP, FP2], {}).body.fps, [FP, FP2]);
  assert.match(A.bodyFor('nuke', [FP], {}).error, /unknown/);
  assert.equal(A.pathOf('ab12cd34', 'to-file'), '/api/projects/ab12cd34/findings/to-file');
});

test('commandFor quotes the reason, title and note so the shell reads them back as one word', () => {
  const reason = 'it\'s "fine" $HOME `id` $(x)\nnext';
  assert.equal(A.commandFor('dismiss', '/r/app', [FP, FP2], { reason }),
    `forge findings dismiss /r/app ${FP} ${FP2} --reason 'it'\\''s "fine" $HOME \`id\` $(x)\nnext'`);
  assert.equal(A.commandFor('dismiss', '/r/my app', [FP], { reason: 'x' }), `forge findings dismiss '/r/my app' ${FP} --reason x`);
  assert.equal(A.shellQuote('plain-word_1.2'), 'plain-word_1.2');
  assert.equal(A.shellQuote(''), "''");
  assert.equal(A.shellQuote('a b'), "'a b'");
  assert.equal(A.shellQuote('-x'), "'-x'");
  assert.equal(A.commandFor('dismiss', '/r', [FP], { reason: '-rf' }), `forge findings dismiss /r ${FP} --reason='-rf'`);
  assert.equal(A.commandFor('to-file', '/r', [FP], { title: 'Fix $PATH' }), `forge findings to-file /r ${FP} --title 'Fix $PATH'`);
  assert.equal(A.commandFor('note', '/r', [FP], { text: '-dash' }), `forge findings note /r ${FP} -- '-dash'`);
  assert.equal(A.commandFor('note', '/r', [FP], { text: 'a\'b' }), `forge findings note /r ${FP} 'a'\\''b'`);
});

test('commandFor names every op and keeps a placeholder for what was not typed', () => {
  assert.equal(A.commandFor('fix', '/r', [FP], { commit: 'abc1234' }), `forge findings fix /r ${FP} --commit abc1234`);
  assert.equal(A.commandFor('fix', '/r', [FP], { reason: 'by hand' }), `forge findings fix /r ${FP} --reason 'by hand'`);
  assert.equal(A.commandFor('fix', '/r', [FP], {}), `forge findings fix /r ${FP} --commit SHA`);
  assert.equal(A.commandFor('dismiss', '/r', [FP], {}), `forge findings dismiss /r ${FP} --reason TEXT`);
  assert.equal(A.commandFor('reopen', '/r', [FP, FP2], {}), `forge findings reopen /r ${FP} ${FP2}`);
  assert.equal(A.commandFor('to-file', '/r', [FP], {}), `forge findings to-file /r ${FP}`);
  assert.equal(A.commandFor('dup', '/r', [FP], { of: FP2 }), `forge findings dup /r ${FP} --of ${FP2}`);
  assert.equal(A.commandFor('dup', '/r', [FP], {}), `forge findings dup /r ${FP} --of FP`);
  assert.equal(A.commandFor('tag', '/r', [FP], { tags: 'ui, flaky' }), `forge findings tag /r ${FP} ui flaky --add`);
  assert.equal(A.commandFor('tag', '/r', [FP], { tags: ['ui'], remove: true }), `forge findings tag /r ${FP} ui --remove`);
  assert.equal(A.commandFor('tag', '/r', [FP], {}), `forge findings tag /r ${FP} TAG --add`);
  assert.equal(A.commandFor('note', '/r', [FP], {}), `forge findings note /r ${FP} TEXT`);
  assert.equal(A.commandFor('edit', '/r', [FP], {}), `forge findings edit /r ${FP} --severity P2`);
  assert.equal(A.commandFor('edit', '/r', [FP], { severity: 'P0' }), `forge findings edit /r ${FP} --severity P0`);
});

test('commandsFor offers Reopen only for a row that is not open, and nothing without a record key', () => {
  assert.deepEqual(A.commandsFor('/r', row(FP)).map(c => c.op), ['fix', 'dismiss', 'to-file', 'dup', 'tag', 'note', 'edit']);
  assert.ok(A.commandsFor('/r', row(FP, { status: 'dismissed' })).some(c => c.op === 'reopen'));
  assert.deepEqual(A.commandsFor('/r', row(null)), []);
  assert.ok(A.commandsFor('/r', row(FP)).every(c => c.command.startsWith('forge findings ') && c.command.includes(FP)));
});

test('a selection allows what its rows allow', () => {
  assert.equal(A.selectable(row(FP)), true);
  assert.equal(A.selectable(row(null)), false);
  assert.equal(A.selectable(null), false);
  const none = A.allowed([]);
  assert.ok(Object.entries(none).filter(([k]) => !['count', 'why'].includes(k)).every(([, v]) => v === false));
  const one = A.allowed([row(FP)]);
  assert.deepEqual([one.fix, one.dismiss, one.reopen, one['to-file'], one.dup, one.tag, one.note, one.edit, one.file], [true, true, false, true, true, true, true, true, true]);
  const two = A.allowed([row(FP), row(FP2, { status: 'to-file' })]);
  assert.deepEqual([two.reopen, two.dup, two.note, two.edit, two.fix, two.file], [true, false, false, false, true, true]);
  assert.equal(A.allowed([row(FP, { status: 'dismissed' })]).reopen, true);
  assert.equal(A.allowed([row(FP, { status: 'filed', fileable: false })]).file, false);
  assert.equal(A.allowed([row(null)]).count, 0);
  const many = A.allowed(Array.from({ length: 201 }, (_, i) => row('f' + i)));
  assert.equal(many.fix, false);
  assert.match(many.why, /at most 200/);
});

test('selectedRows keeps the shown, selectable, picked rows once each, in row order', () => {
  const rows = [row(FP), row(FP2), row(null), row(FP)];
  assert.deepEqual(A.selectedRows(rows, new Set([FP2, FP, 'hidden'])).map(r => r.record_fp), [FP, FP2]);
  assert.deepEqual(A.fpsOf(A.selectedRows(rows, [FP2])), [FP2]);
  assert.deepEqual(A.selectedRows(null, new Set([FP])), []);
});

test('tags: the ones present, the filter, and which are only derived', () => {
  const rows = [row(FP, { tags: ['ui', 'flaky'], record: { tags: ['ui', 'flaky'] } }), row(FP2, { tags: ['backend'] }), row('cccccccccccc', { tags: [] })];
  assert.deepEqual(A.tagsIn(rows), ['backend', 'flaky', 'ui']);
  assert.deepEqual(A.filterByTag(rows, 'ui').map(r => r.record_fp), [FP]);
  assert.equal(A.filterByTag(rows, '').length, 3);
  assert.equal(A.derivedTags(rows[0]), false);
  assert.equal(A.derivedTags(rows[1]), true);
});

test('fileableFps takes open fileable rows, at most 25, once each', () => {
  const rows = Array.from({ length: 30 }, (_, i) => row('r' + i, { fp: 'fp' + i }));
  assert.equal(A.fileableFps(rows).length, 25);
  assert.deepEqual(A.fileableFps(rows).slice(0, 2), ['fp0', 'fp1']);
  const mixed = [row('a', { fp: 'f1', fileable: false }), row('b', { fp: 'f2', status: 'filed' }), row('c', { fp: 'f3', status: 'to-file' }), row('d', { fp: 'f3' }), row('e', { fp: null })];
  assert.deepEqual(A.fileableFps(mixed), ['f3']);
  assert.equal(A.fileRun(mixed), 'run1');
  assert.equal(A.fileRun([row('a', { fileable: false })]), null);
  assert.deepEqual(A.fileableFps(null), []);
});

test('the confirm sentence names the count and what changes', () => {
  assert.equal(A.confirmText('dismiss', 2, { reason: 'noise' }), 'Dismiss 2 findings: noise?');
  assert.equal(A.confirmText('reopen', 1, {}), 'Reopen 1 finding?');
  assert.equal(A.confirmText('fix', 3, { commit: 'abc1234' }), 'Mark 3 findings fixed by abc1234?');
  assert.equal(A.confirmText('tag', 2, { tags: ['ui', 'x'], remove: true }), 'Remove ui, x from 2 findings?');
  assert.equal(A.confirmText('note', 1, { text: '' }), 'Clear the note of 1 finding?');
});
