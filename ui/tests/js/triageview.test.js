'use strict';
// node --test: the triage panel (triageview.js): the pure helpers, start, polling, the proposal cards, approve and
// reject, and the read-only commands. No browser. The panel renders against the fake document the other page tests use;
// get, post and controlsEnabled are the seams, and setTimeout is a registry the test fires by hand.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('path');

const root = path.join(__dirname, '..', '..', 'forge_ui', 'static');

class Node {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeName = this.tagName;
    this.className = '';
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this.attrs = {};
    this.dataset = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = '';
    this.type = '';
    this.title = '';
    this.id = '';
    this.href = '';
    this.tabIndex = 0;
    this._text = '';
    this._root = false;
  }
  get classList() {
    const node = this;
    const tokens = () => String(node.className || '').split(/\s+/).filter(Boolean);
    return {
      contains: c => tokens().includes(c),
      add(...cs) { const set = new Set(tokens()); for (const c of cs) set.add(c); node.className = [...set].join(' '); },
      remove(...cs) { const drop = new Set(cs); node.className = tokens().filter(c => !drop.has(c)).join(' '); },
      toggle(c, force) {
        const on = force === undefined ? !tokens().includes(c) : !!force;
        if (on) this.add(c); else this.remove(c);
        return on;
      },
    };
  }
  get childNodes() { return this.children; }
  get childElementCount() { return this.children.filter(c => c.nodeName && c.nodeName[0] !== '#').length; }
  hasChildNodes() { return this.children.length > 0; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(value) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this._text = value == null ? '' : String(value);
  }
  get isConnected() {
    let n = this;
    while (n) { if (n._root) return true; n = n.parentNode; }
    return false;
  }
  append(...nodes) {
    for (const node of nodes) {
      if (!node || typeof node !== 'object') continue;
      if (node.parentNode) node.remove();
      node.parentNode = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this._text = '';
    this.append(...nodes);
  }
  remove() {
    const parent = this.parentNode;
    if (parent) parent.children = parent.children.filter(c => c !== this);
    this.parentNode = null;
  }
  contains(node) { return node === this || this.children.some(c => c.contains && c.contains(node)); }
  replaceWith(...nodes) {
    const parent = this.parentNode;
    if (!parent) return;
    const at = parent.children.indexOf(this);
    this.remove();
    const prepared = [];
    for (const node of nodes) {
      if (!node) continue;
      if (node.parentNode) node.remove();
      node.parentNode = parent;
      prepared.push(node);
    }
    parent.children.splice(at, 0, ...prepared);
  }
  querySelectorAll(selector) {
    const out = [];
    for (const child of this.children || []) {
      if (matchesSel(child, selector)) out.push(child);
      if (child.querySelectorAll) out.push(...child.querySelectorAll(selector));
    }
    return out;
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (matchesSel(node, selector)) return node;
      node = node.parentNode;
    }
    return null;
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  focus() { document.activeElement = this; }
  click() {
    const event = { preventDefault() {}, stopPropagation() {}, target: this };
    for (const fn of this.listeners.click || []) fn(event);
  }
  getBoundingClientRect() { return { left: 0, top: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  scrollIntoView() {}
}
global.Node = Node;
const document = {
  activeElement: null,
  createElement(tag) { return new Node(tag); },
  createTextNode(text) { const n = new Node('#text'); n.textContent = text == null ? '' : String(text); return n; },
  querySelector() { return null; },
  getElementById() { return null; },
  addEventListener() {},
};
document.body = new Node('body');
document.body._root = true;
global.document = document;
Object.defineProperty(global, 'navigator', { value: { clipboard: { async writeText() {} } }, configurable: true });
global.self = global;
global.window = global;

// setTimeout(fn, 0) is real (the outcome line paints on it); every other timer is held until the test fires it.
const realSet = global.setTimeout, realClear = global.clearTimeout;
let held = [], nextId = 1;
global.setTimeout = (fn, ms) => {
  if (!ms) return realSet(fn, 0);
  const t = { id: nextId++, fn, ms, live: true };
  held.push(t);
  return t;
};
global.clearTimeout = t => { if (t && typeof t === 'object') t.live = false; else realClear(t); };
const timers = ms => held.filter(t => t.live && (ms === undefined || t.ms === ms));
async function fire(ms) {
  const t = timers(ms)[0];
  assert.ok(t, 'no timer of ' + ms + ' ms is waiting');
  t.live = false;
  await t.fn();
  await settle();
}

const state = { controls: true, gets: [], posts: [], getReply: () => ({ ok: false, status: 404, error: 'missing' }), postReply: () => ({ ok: true, status: 200, data: {} }) };
const U = require(path.join(root, 'uikit.js'));
const ui = U.create({ document });
ui.controlsEnabled = async () => state.controls;
ui.get = async p => { state.gets.push(p); return state.getReply(p); };
ui.post = async (p, body) => { state.posts.push({ path: p, body }); return state.postReply(p, body); };
global.ForgeUI = ui;
const T = require(path.join(root, 'triageview.js'));

function matchesSel(node, selector) {
  const parts = String(selector).match(/\[[^\]]+\]/g);
  if (!parts || parts.join('') !== selector) return false;
  return parts.every(part => {
    const body = part.slice(1, -1), eq = body.indexOf('=');
    const name = (eq === -1 ? body : body.slice(0, eq)).replace(/^data-/, '');
    const value = node.dataset && node.dataset[name];
    if (eq === -1) return value != null && value !== '';
    return String(value) === body.slice(eq + 1).replace(/^["']|["']$/g, '');
  });
}
function walk(node) { const out = []; for (const c of node.children || []) { out.push(c, ...walk(c)); } return out; }
const buttons = node => walk(node).filter(n => n.tagName === 'BUTTON');
const button = (node, text) => buttons(node).find(n => n.textContent === text);
const field = (node, name) => walk(node).find(n => n.dataset && n.dataset.field === name);
const hasClass = (n, c) => String(n.className || '').split(/\s+/).includes(c);
const byClass = (node, c) => walk(node).filter(n => hasClass(n, c));
const codes = node => walk(node).filter(n => n.tagName === 'CODE').map(n => n.textContent);
const confirmText = node => { const s = byClass(node, 'ui-confirm-text')[0]; return s ? s.textContent : ''; };
function click(node) {
  assert.ok(node, 'no such control');
  if (node.disabled) return false;
  for (const fn of node.listeners.click || []) fn({ preventDefault() {}, stopPropagation() {}, target: node });
  return true;
}
function setValue(node, value) { node.value = value; for (const fn of node.listeners.input || []) fn({}); }
async function settle() { for (let i = 0; i < 8; i++) await new Promise(r => realSet(r, 0)); }
async function until(pred) {
  for (let i = 0; i < 60; i++) { if (pred()) return; await new Promise(r => realSet(r, 0)); }
  assert.fail('timed out');
}
function mount() { const n = document.createElement('div'); n._root = true; return n; }
function toggleOpen(h) {
  const d = walk(h).find(n => n.tagName === 'DETAILS' && hasClass(n, 'triage'));
  d.open = true;
  for (const fn of d.listeners.toggle || []) fn({});
}

const FP = n => String(n).repeat(12).slice(0, 12);
const RUNNING = { triage: 3, state: 'running', spec: 'sol:high', started: new Date(Date.now() - 65000).toISOString(), findings: 4 };
const DONE = { triage: 3, state: 'done', spec: 'sol:high', started: '2026-11-16T10:00:00Z', finished: '2026-11-16T10:02:05Z', findings: 4, proposals: 5 };
const proposals = () => ({ triage: 3, proposals: [
  { id: 'p1', kind: 'duplicate', hash: 'h1', state: 'pending', fp: FP(1), of: FP(2), why: 'same lock bug' },
  { id: 'p2', kind: 'fixed', hash: 'h2', state: 'pending', fp: FP(3), evidence: 'the guard is there now', commit: 'abc1234' },
  { id: 'p3', kind: 'tag', hash: 'h3', state: 'pending', fps: [FP(1), FP(3), FP(4)], tags: ['ui', 'lock'] },
  { id: 'p4', kind: 'issue', hash: 'h4', state: 'pending', fps: [FP(2)], title: 'Fix the lock', body: 'Long body text' },
  { id: 'p5', kind: 'plan', hash: 'h5', state: 'pending', fps: [FP(4)], title: 'Rework locks', request: 'Do it in two steps',
    plan: { tasks: [{ id: 'a', difficulty: 'low', title: 'first' }, { id: 'b', difficulty: 'high', title: 'second' }] } },
] });
const ROWS = [{ record_fp: FP(1), summary: '- [P2] a.py:1 — the lock leaks' }, { record_fp: FP(2), summary: 'second finding' }];

function reply(st, props) {
  state.getReply = p => {
    if (p.startsWith('/api/specs/check')) return { ok: true, status: 200, data: { ok: true, resolved: { harness: 'codex', model: 'gpt-5', effort: 'high' } } };
    if (p.startsWith('/api/projects/proj1/triage')) return { ok: true, status: 200, data: { state: st, proposals: props || null } };
    return { ok: false, status: 404, error: 'missing', detail: p };
  };
}
function reset(controls = true) {
  held = []; state.controls = controls; state.gets = []; state.posts = [];
  state.postReply = () => ({ ok: true, status: 200, data: {} });
}
function open(store, extra) {
  const h = mount(), calls = { reload: 0, shown: [] };
  const view = T.render(h, Object.assign({
    id: 'proj1', root: '/my repo', writes: state.controls, store, rows: () => ROWS, openCount: () => 12,
    reload: () => { calls.reload += 1; }, show: fp => calls.shown.push(fp),
  }, extra || {}));
  return { h, view, calls };
}

test('the pure helpers: one line per kind, the commands, the states', () => {
  const [dup, fixed, tag, issue, plan] = proposals().proposals;
  assert.equal(T.proposalSummary(dup), `duplicate: ${FP(1)} repeats ${FP(2)}`);
  assert.equal(T.proposalSummary(fixed), `fixed: ${FP(3)} by abc1234`);
  assert.equal(T.proposalSummary(tag), 'tag 3 findings: ui, lock');
  assert.equal(T.proposalSummary(issue), 'issue for 1 finding: Fix the lock');
  assert.equal(T.proposalSummary(plan), 'plan for 1 finding: Rework locks (2 tasks)');
  assert.equal(T.cliFor('triage', '/repo', null, { spec: 'sol:high' }), 'forge findings triage /repo --spec sol:high --detach');
  assert.equal(T.cliFor('triage', '/my repo', null, {}), "forge findings triage '/my repo' --spec SPEC --detach");
  assert.equal(T.cliFor('proposals', '/repo'), 'forge findings proposals /repo');
  assert.equal(T.cliFor('apply', '/repo', 3, { id: 'p2', hash: 'h2' }), 'forge findings apply /repo 3 p2 --approve h2');
  assert.equal(T.cliFor('reject', '/repo', 3, { id: 'p2' }), 'forge findings reject /repo 3 p2');
  assert.equal(T.isActive({ state: 'running' }), true);
  for (const s of [{ state: 'done' }, { state: 'failed' }, null, undefined]) assert.equal(T.isActive(s), false);
  assert.equal(T.pollDelay({ state: 'running' }), 5000);
  assert.equal(T.pollDelay({ state: 'done' }), null);
  assert.equal(T.pollDelay(null), null);
  assert.equal(T.elapsed('2026-11-16T10:00:00Z', '2026-11-16T10:01:05Z'), '1m 05s');
  assert.equal(T.parseMax(''), null);
  assert.equal(T.parseMax('50'), 50);
  for (const bad of ['0', '2001', '1.5', 'x']) assert.ok(Number.isNaN(T.parseMax(bad)), bad);
});

test('start: the spec is checked 400 ms after typing, a bad spec blocks Start, a good one confirms then posts the body', async () => {
  reset(); reply(null, null);
  const store = { open: true };
  const { h } = open(store);
  await until(() => field(h, 'spec'));
  const start = button(h, 'Start triage');
  assert.equal(start.disabled, true, 'nothing checked yet');
  state.getReply = p => p.startsWith('/api/specs/check')
    ? { ok: true, status: 200, data: { ok: false, error: 'no such model' } } : { ok: true, status: 200, data: { state: null, proposals: null } };
  setValue(field(h, 'spec'), 'nope');
  assert.equal(timers(400).length, 1);
  assert.equal(state.gets.filter(p => p.startsWith('/api/specs/check')).length, 0, 'not before the pause');
  await fire(400);
  assert.ok(state.gets.includes('/api/specs/check?spec=nope'));
  assert.match(h.textContent, /no such model/);
  assert.equal(start.disabled, true);

  reply(null, null);
  setValue(field(h, 'spec'), 'sol:high');
  setValue(field(h, 'spec'), 'sol:high ');
  assert.equal(timers(400).length, 1, 'typing again restarts the one check');
  await fire(400);
  assert.ok(state.gets.includes('/api/specs/check?spec=sol%3Ahigh'));
  assert.match(h.textContent, /codex · gpt-5/);
  assert.equal(start.disabled, false);
  setValue(field(h, 'max'), '5');
  click(start);
  await until(() => confirmText(h));
  assert.equal(confirmText(h), "Start a read-only triage of 5 open findings with sol:high? It uses that model's quota.");
  assert.equal(state.posts.length, 0, 'nothing before Confirm');
  state.postReply = () => ({ ok: true, status: 200, data: { triage: 3, state: 'running' } });
  reply(RUNNING, null);
  click(button(h, 'Confirm'));
  await until(() => state.posts.length === 1);
  assert.deepEqual(state.posts, [{ path: '/api/projects/proj1/triage', body: { spec: 'sol:high', max_findings: 5 } }]);
  await until(() => /running for/.test(h.textContent));
  assert.equal(start.disabled, true, 'a running triage blocks a second start');
  assert.equal(timers(5000).length, 1);
});

test('Cancel posts nothing, a 409 shows its detail, an out-of-range max blocks Start', async () => {
  reset(); reply(null, null);
  const { h } = open({ open: true });
  await until(() => field(h, 'spec'));
  setValue(field(h, 'spec'), 'sol');
  await fire(400);
  click(button(h, 'Start triage'));
  await until(() => confirmText(h));
  click(button(h, 'Cancel'));
  await settle();
  assert.equal(state.posts.length, 0);
  state.postReply = () => ({ ok: false, status: 409, error: 'refused', detail: 'a triage is already running', data: null });
  click(button(h, 'Start triage'));
  await until(() => confirmText(h));
  click(button(h, 'Confirm'));
  await until(() => /already running/.test(h.textContent));
  setValue(field(h, 'max'), '5000');
  assert.equal(button(h, 'Start triage').disabled, true);
  assert.match(h.textContent, /1 to 2000/);
});

test('polling: every 5 s while running, one timer, gone when it is done', async () => {
  reset(); reply(RUNNING, null);
  const store = { open: true };
  const { h } = open(store);
  await until(() => /running for/.test(h.textContent));
  assert.match(h.textContent, /sol:high/);
  assert.match(h.textContent, /4 findings/);
  assert.equal(timers().length, 1);
  assert.equal(timers()[0].ms, 5000);
  const before = state.gets.length;
  await fire(5000);
  assert.equal(state.gets.length, before + 1);
  assert.equal(timers().length, 1, 'still running: one new timer, not two');
  reply(DONE, proposals());
  await fire(5000);
  assert.equal(timers().length, 0, 'done: polling ends');
  assert.equal(byClass(h, 'triage-card').length, 5);
  assert.match(h.textContent, /took 2m 05s/);
});

test('a failed triage shows its reason and does not poll', async () => {
  reset(); reply({ state: 'failed', spec: 'sol', reason: 'the model timed out', findings: 2 }, null);
  const { h } = open({ open: true });
  await until(() => /the model timed out/.test(h.textContent));
  assert.equal(timers().length, 0);
});

test('a redraw stops the old panel; leaving the page stops the poll', async () => {
  reset(); reply(RUNNING, null);
  const store = { open: true };
  const a = open(store);
  await until(() => timers().length === 1);
  a.view.stop();
  assert.equal(timers().length, 0, 'stop clears the poll');
  const b = open(store);
  await until(() => /running for/.test(b.h.textContent));
  assert.equal(timers().length, 1);
  const c = open(store);
  b.view.stop();
  await until(() => timers().length === 1);
  const gets = state.gets.length;
  c.h._root = false;                                       // the page left
  await fire(5000);
  assert.equal(state.gets.length, gets, 'a panel no longer on the page does not read');
  assert.equal(timers().length, 0);
});

test('a tick answered after stop() does not draw or schedule', async () => {
  reset(); reply(RUNNING, null);
  const { h, view } = open({ open: true });
  await until(() => timers().length === 1);
  let release;
  state.getReply = () => new Promise(r => { release = () => r({ ok: true, status: 200, data: { state: RUNNING, proposals: null } }); });
  const pending = fire(5000);
  await until(() => release);
  view.stop();
  release();
  await pending;
  assert.equal(timers().length, 0);
});

test('a collapsed panel with nothing known reads nothing until it opens', async () => {
  reset(); reply(null, null);
  const { h } = open({});
  await settle();
  assert.equal(state.gets.length, 0);
  toggleOpen(h);
  await until(() => state.gets.includes('/api/projects/proj1/triage'));
  await until(() => /No triage has run/.test(h.textContent));
});

test('a done triage: cards grouped by kind, with findings, why, hash and state', async () => {
  reset(); reply(DONE, proposals());
  const { h, calls } = open({ open: true });
  await until(() => byClass(h, 'triage-card').length === 5);
  assert.deepEqual(byClass(h, 'triage-group').map(g => g.dataset.kind), ['duplicate', 'fixed', 'tag', 'issue', 'plan']);
  const card = id => byClass(h, 'triage-card').find(c => c.dataset.pid === id);
  const dup = card('p1');
  assert.match(dup.textContent, /same lock bug/);
  assert.ok(codes(dup).includes('h1'));
  assert.match(dup.textContent, /pending/);
  assert.match(card('p2').textContent, /the guard is there now/);
  assert.match(card('p2').textContent, /abc1234/);
  assert.match(card('p3').textContent, /ui/);
  const issue = card('p4'), body = walk(issue).find(n => n.tagName === 'DETAILS');
  assert.ok(!body.open, 'the issue body is closed');
  assert.match(body.textContent, /Long body text/);
  assert.match(issue.textContent, /Fix the lock/);
  const plan = card('p5');
  assert.match(plan.textContent, /Rework locks/);
  assert.match(plan.textContent, /Do it in two steps/);
  const taskRows = byClass(plan, 'triage-tasks')[0].children.map(li => li.textContent);
  assert.match(taskRows[0], /a.*low.*first/);
  assert.match(taskRows[1], /b.*high.*second/);
  // a finding the table knows is a link to its row; one it does not is plain text
  const link = buttons(card('p3')).find(b => b.textContent === FP(1));
  assert.ok(link);
  click(link);
  assert.deepEqual(calls.shown, [FP(1)]);
  assert.equal(buttons(card('p3')).some(b => b.textContent === FP(3)), false);
});

test('Approve confirms what changes, then sends exactly the card\'s hash, then reads again and reloads', async () => {
  reset(); reply(DONE, proposals());
  const { h, calls } = open({ open: true });
  await until(() => byClass(h, 'triage-card').length === 5);
  const card = id => byClass(h, 'triage-card').find(c => c.dataset.pid === id);
  click(button(card('p3'), 'Approve'));
  await until(() => confirmText(card('p3')));
  assert.equal(confirmText(card('p3')), 'Add ui, lock to 3 findings?');
  assert.equal(state.posts.length, 0);
  const after = proposals(); after.proposals[2].state = 'applied';
  reply(DONE, after);
  click(button(card('p3'), 'Confirm'));
  await until(() => state.posts.length === 1 && calls.reload === 1);
  assert.deepEqual(state.posts, [{ path: '/api/projects/proj1/triage/3/p3/apply', body: { hash: 'h3' } }]);
  assert.ok(state.gets.includes('/api/projects/proj1/triage?n=3'));
  assert.equal(button(card('p3'), 'Approve'), undefined, 'applied: no more buttons');
  assert.match(card('p3').textContent, /applied/);
});

test('each kind confirms with what it changes', () => {
  const [dup, fixed, tag, issue, plan] = proposals().proposals;
  assert.equal(T.confirmFor(dup), `Mark 1 finding ${FP(1)} as a duplicate of ${FP(2)}?`);
  assert.equal(T.confirmFor(fixed), `Mark 1 finding ${FP(3)} fixed by abc1234?`);
  assert.match(T.confirmFor(issue), /Mark 1 finding to file as "Fix the lock"/);
  assert.equal(T.confirmFor(plan), 'Create the plan dir and print its plan command (nothing runs)?');
});

test('a 409 on approve shows its detail and leaves the card pending', async () => {
  reset(); reply(DONE, proposals());
  const { h, calls } = open({ open: true });
  await until(() => byClass(h, 'triage-card').length === 5);
  const card = byClass(h, 'triage-card').find(c => c.dataset.pid === 'p1');
  state.postReply = () => ({ ok: false, status: 409, error: 'refused', detail: 'p1: the hash does not match the proposal as it is now; read it again', data: null });
  click(button(card, 'Approve'));
  await until(() => confirmText(card));
  click(button(card, 'Confirm'));
  await until(() => /hash does not match/.test(card.textContent));
  assert.equal(calls.reload, 0);
  assert.equal(button(card, 'Approve').disabled, false, 'it can be tried again');
});

test('Reject sends the reason, or an empty body', async () => {
  reset(); reply(DONE, proposals());
  const { h, calls } = open({ open: true });
  await until(() => byClass(h, 'triage-card').length === 5);
  const card = id => byClass(h, 'triage-card').find(c => c.dataset.pid === id);
  field(card('p1'), 'reason').value = 'not a duplicate';
  click(button(card('p1'), 'Reject'));
  await until(() => calls.reload === 1);
  assert.deepEqual(state.posts[0], { path: '/api/projects/proj1/triage/3/p1/reject', body: { reason: 'not a duplicate' } });
  click(button(card('p2'), 'Reject'));
  await until(() => calls.reload === 2);
  assert.deepEqual(state.posts[1], { path: '/api/projects/proj1/triage/3/p2/reject', body: {} });
});

test('a plan approval sends the plan dir and shows the dir and the next command, also after a redraw', async () => {
  reset(); reply(DONE, proposals());
  const store = { open: true };
  const { h } = open(store);
  await until(() => byClass(h, 'triage-card').length === 5);
  const card = byClass(h, 'triage-card').find(c => c.dataset.pid === 'p5');
  field(card, 'plan-dir').value = 'plans/locks';
  click(button(card, 'Approve'));
  await until(() => confirmText(card));
  assert.match(confirmText(card), /Create the plan dir and print its plan command \(nothing runs\)/);
  const after = proposals(); after.proposals[4].state = 'applied'; after.proposals[4].plan_dir = '/repo/plans/locks';
  reply(DONE, after);
  state.postReply = () => ({ ok: true, status: 200, data: { proposal: {}, changed: [], plan_dir: '/repo/plans/locks', next: 'forge-parallel.sh plan /repo/plans/locks --repo /repo' } });
  click(button(card, 'Confirm'));
  await until(() => state.posts.length === 1);
  assert.deepEqual(state.posts[0], { path: '/api/projects/proj1/triage/3/p5/apply', body: { hash: 'h5', plan_dir: 'plans/locks' } });
  await until(() => codes(h).includes('forge-parallel.sh plan /repo/plans/locks --repo /repo'));
  assert.match(h.textContent, /Plan dir: \/repo\/plans\/locks/);
  const again = open(store);                                // the Findings tab redrew
  await until(() => codes(again.h).includes('forge-parallel.sh plan /repo/plans/locks --repo /repo'));
});

test('read-only: the commands to copy, the state and the proposals, no action buttons', async () => {
  reset(false); reply(DONE, proposals());
  const { h } = open({ open: true });
  await until(() => byClass(h, 'triage-card').length === 5);
  assert.equal(walk(h).some(n => n.dataset && (n.dataset.op === 'start' || n.dataset.op === 'approve' || n.dataset.op === 'reject')), false);
  assert.equal(button(h, 'Start triage'), undefined);
  assert.equal(button(h, 'Approve'), undefined);
  assert.equal(button(h, 'Reject'), undefined);
  assert.equal(field(h, 'spec'), undefined);
  const shown = codes(h);
  assert.ok(shown.includes("forge findings triage '/my repo' --spec SPEC --detach"));
  assert.ok(shown.includes("forge findings proposals '/my repo'"));
  assert.ok(shown.includes("forge findings apply '/my repo' 3 p1 --approve h1"));
  assert.ok(shown.includes("forge findings reject '/my repo' 3 p5"));
  assert.ok(button(h, 'Copy'));
  assert.match(h.textContent, /done/);
});

test('with no writes given the panel asks controlsEnabled', async () => {
  reset(false); reply(null, null);
  const { h } = open({ open: true }, { writes: undefined });
  await until(() => codes(h).length > 0);
  assert.equal(field(h, 'spec'), undefined);
  reset(true); reply(null, null);
  const b = open({ open: true }, { writes: undefined });
  await until(() => field(b.h, 'spec'));
});

test('the Findings tab mounts the panel above the table, collapsed, and leaving the tab stops its poll', async () => {
  global.ForgeTable = require(path.join(root, 'datatable.js'));
  const P = require(path.join(root, 'projectsview.js'));
  reset(true);
  const findings = { notes: [], issues_online: true, issues_read_at: 1, rows: [
    { record_fp: FP(1), summary: 'one', severity: 'P2', status: 'open', kind: 'primary', run_id: 'r1', reviewer: 'opus', location: 'a.py:1', at: 1000, tags: [] },
    { record_fp: FP(2), summary: 'two', severity: 'P1', status: 'open', kind: 'primary', run_id: 'r1', reviewer: 'opus', location: 'a.py:2', at: 1000, tags: [] },
  ] };
  state.getReply = p => {
    if (p === '/api/projects/proj1') return { ok: true, status: 200, data: { name: 'App', root: '/repo', exists: true, runs: [], checkouts: [] } };
    if (p.startsWith('/api/projects/proj1/findings')) return { ok: true, status: 200, data: findings };
    if (p.startsWith('/api/projects/proj1/triage')) return { ok: true, status: 200, data: { state: RUNNING, proposals: null } };
    return { ok: false, status: 404, error: 'missing' };
  };
  const h = mount();
  const view = P.render(h, { id: 'proj1', tab: 'findings', runHref: k => '#/runs/' + k });
  await until(() => byClass(h, 'triage').length === 1 && h.textContent.includes('two'));
  const panel = byClass(h, 'triage')[0];
  assert.ok(!panel.open, 'collapsed by default');
  assert.match(panel.textContent, /Triage with an agent/);
  const kids = byClass(h, 'prj-body')[0].children;
  assert.ok(hasClass(kids[0], 'triage-host'), 'above the filters and the table');
  assert.ok(button(h, 'Mark fixed'), 'the existing actions are still there');
  assert.equal(state.gets.filter(p => p.startsWith('/api/projects/proj1/triage')).length, 0, 'collapsed: nothing read');
  toggleOpen(h);
  await until(() => timers(5000).length === 1);
  view.go('proj1', 'memories');                              // leaving the tab ends the poll
  assert.equal(timers().length, 0);
});
