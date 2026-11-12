'use strict';
// node --test: the task header and the attempt trail (panel.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

global.window = {
  ForgeRunModel: require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'runmodel.js')),
};
const P = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'panel.js'));

const REASONS = {
  first: 'first attempt',
  review: 'after review findings',
  prereview: 'after failing tests before review',
  bounce: 'after unaddressed findings',
  message: 'after an unfinished message',
  answer: 'after your answer',
  escalation: 'escalated to a stronger model',
  manual: 'manual retry',
  unknown: 'reason not recorded',
};

test('a trail line names the attempt, the model, the verdict, the findings and why it ran', () => {
  const base = { attempt: 2, dwarf: 'grok:xhigh', verdict: 'FAIL', findings: { confirmed: 3, worst: 'P1' }, worst: 'P1' };
  for (const [reason, words] of Object.entries(REASONS)) {
    assert.equal(P.trailLine({ ...base, reason }), `#2 grok:xhigh · FAIL · 3 findings (worst P1) · ${words}`, reason);
  }
  assert.equal(P.reasonText('sideways'), 'reason not recorded');
  assert.equal(P.trailLine({ ...base, reason: 'nope' }), '#2 grok:xhigh · FAIL · 3 findings (worst P1) · reason not recorded');
  assert.equal(P.trailLine({ attempt: 1, dwarf: 'luna', verdict: 'FAIL', findings: { confirmed: 1 }, worst: 'P1', reason: 'first' }),
    '#1 luna · FAIL · 1 finding (worst P1) · first attempt');
  assert.equal(P.trailLine({ attempt: 2, dwarf: 'sol:xhigh', verdict: 'PASS', findings: { confirmed: 0 }, worst: null, reason: 'escalation' }),
    '#2 sol:xhigh · PASS · 0 findings · escalated to a stronger model');
  assert.equal(P.trailLine({ attempt: 3, dwarf: null, verdict: null, findings: null, worst: null, reason: 'unknown' }),
    '#3 — · — · no findings · reason not recorded');
});

test('a link with a url opens, and a link without one is text', () => {
  assert.deepEqual(P.linkModel({ kind: 'issue', number: 123, url: 'https://github.com/acme/widgets/issues/123' }),
    { label: '#123', href: 'https://github.com/acme/widgets/issues/123' });
  assert.deepEqual(P.linkModel({ kind: 'pr', number: 45, url: 'https://github.com/acme/widgets/pull/45' }),
    { label: 'PR #45', href: 'https://github.com/acme/widgets/pull/45' });
  assert.deepEqual(P.linkModel({ kind: 'issue', number: 133, url: null }), { label: '#133', href: null });
  assert.deepEqual(P.linkModel({ kind: 'url', number: null, url: 'https://github.com/acme/widgets' }),
    { label: 'https://github.com/acme/widgets', href: 'https://github.com/acme/widgets' });
  assert.deepEqual(P.linkModel({ kind: 'issue', number: 7, url: 'javascript:alert(1)' }), { label: '#7', href: null });
});

test('prompt, approach and acceptance criteria are disabled when their text is missing', () => {
  const ready = P.headerActions({ criteria: 'Ships.' }, { prompt: 'Do the thing.', approach: 'Carefully.' });
  assert.deepEqual(ready.map(a => [a.label, a.disabled]), [
    ['Prompt', false], ['Approach', false], ['Acceptance criteria', false]]);
  const missing = P.headerActions({ criteria: null }, { prompt: null, approach: null });
  assert.deepEqual(missing.map(a => [a.key, a.disabled]), [
    ['prompt', true], ['approach', true], ['criteria', true]]);
  const partial = P.headerActions({ criteria: '' }, { prompt: 'x' });
  assert.deepEqual(partial.map(a => [a.key, a.disabled]), [
    ['prompt', false], ['approach', true], ['criteria', true]]);
  assert.equal(P.headerActions({ title: 'Read' }, {}).find(a => a.key === 'criteria').disabled, true);
});
