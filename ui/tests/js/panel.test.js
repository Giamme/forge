'use strict';
// node --test: the task header and the attempt trail (panel.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

// Enough of a document for ForgePanel.render. Elements are real Nodes so panel.js can
// tell a link from text (`value instanceof Node`).
class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.className = '';
    this.children = [];
    this._text = '';
    this.dataset = {};
    this.href = '';
    this.id = '';
    this.title = '';
  }
}
global.Node = Node;
Object.defineProperty(Node.prototype, 'textContent', {
  get() { return this._text + this.children.map(child => child.textContent).join(''); },
  set(value) { this._text = value == null ? '' : String(value); this.children = []; },
});
Node.prototype.append = function (...nodes) {
  for (const node of nodes) if (node) this.children.push(node);
};
Node.prototype.replaceChildren = function (...nodes) {
  this.children = [];
  this._text = '';
  this.append(...nodes);
};
Node.prototype.addEventListener = function () {};
function matches(node, part) {
  const found = part.match(/^([a-zA-Z0-9]*)(?:\.([a-zA-Z0-9_-]+))?$/);
  if (!found || (!found[1] && !found[2])) return false;
  if (found[1] && node.tagName !== found[1].toUpperCase()) return false;
  if (found[2] && !String(node.className || '').split(/\s+/).includes(found[2])) return false;
  return true;
}
function descendants(node) {
  const out = [];
  for (const child of node.children || []) {
    if (!child.tagName || child.tagName[0] === '#') continue;
    out.push(child);
    out.push(...descendants(child));
  }
  return out;
}
function queryAll(node, selector) {
  const parts = String(selector).trim().split(/\s+/);
  function search(scope, index) {
    const out = [];
    for (const candidate of descendants(scope)) {
      if (!matches(candidate, parts[index])) continue;
      if (index === parts.length - 1) out.push(candidate);
      else out.push(...search(candidate, index + 1));
    }
    return out;
  }
  return search(node, 0);
}
Node.prototype.querySelectorAll = function (selector) { return queryAll(this, selector); };
Node.prototype.querySelector = function (selector) { return queryAll(this, selector)[0] || null; };
global.document = {
  createElement(tag) { return new Node(tag); },
  createTextNode(text) {
    const node = new Node('#text');
    node.textContent = text == null ? '' : String(text);
    return node;
  },
};

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

function planRun(extra) {
  return Object.assign({
    kind: 'plan', key: 'bbbbbbbbbbbb', run_id: 'plan-b', path: '/tmp/plans/b', repo: '/tmp/repo',
    tasks: [], settings: {}, waves: [], deferrals: [],
  }, extra);
}

function render(run) {
  const host = document.createElement('div');
  P.render(host, { run, sel: null, now: 0, hash: () => '#/here', onSelect() {} });
  return host;
}

function fieldMap(host) {
  const out = new Map();
  for (const dl of host.querySelectorAll('dl.fields')) {
    for (let i = 0; i < dl.children.length; i += 2) out.set(dl.children[i].textContent, dl.children[i + 1]);
  }
  return out;
}

function labels(host) {
  return [...fieldMap(host).keys()];
}

test('a chained run links the plan it follows and the plans that follow it', () => {
  const chained = render(planRun({
    after: { key: 'aaaaaaaaaaaa', run_id: 'plan-a', path: '/tmp/plans/a' },
    followed_by: [
      { key: 'cccccccccccc', run_id: 'plan-c' },
      { key: 'dddddddddddd', run_id: null },
    ],
  }));
  const plain = render(planRun({ after: null, followed_by: [] }));
  const fields = fieldMap(chained);
  assert.deepEqual(fields.get('After').querySelectorAll('a').map(a => [a.textContent, a.href]),
    [['plan-a', '#/runs/aaaaaaaaaaaa']]);
  assert.deepEqual(fields.get('Followed by').querySelectorAll('a').map(a => [a.textContent, a.href]), [
    ['plan-c', '#/runs/cccccccccccc'],
    ['dddddddddddd', '#/runs/dddddddddddd'],
  ]);
  // The finish block of a run in no chain is the one it had before these rows existed.
  assert.deepEqual(labels(chained).filter(label => label !== 'After' && label !== 'Followed by'), labels(plain));
  assert.equal(labels(plain).includes('After'), false);
  assert.equal(labels(plain).includes('Followed by'), false);
  assert.ok(labels(plain).includes('Integration'));
});
