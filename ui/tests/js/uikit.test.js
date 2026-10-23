'use strict';
// node --test: the shared write helpers (uikit.js) over fake fetch and document objects. No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const U = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'uikit.js'));

// ---- a fake document: just enough tree for the token meta, the outcome line and the confirm strip
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.parent = null; this.listeners = {}; this.attrs = {}; this.className = ''; this.textContent = ''; this.isConnected = true; this.focused = 0; }
  get classList() { return { contains: c => this.className.split(/\s+/).includes(c) }; }
  append(...kids) { for (const k of kids) { k.parent = this; this.children.push(k); } }
  replaceChildren(...kids) { this.children = []; this.append(...kids); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  contains(n) { return n === this || this.children.some(c => c.contains(n)); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  fire(type, event = {}) { for (const fn of this.listeners[type] || []) fn({ preventDefault() {}, stopPropagation() { event.stopped = true; }, ...event }); }
  focus() { this.ownerDocument.activeElement = this; this.focused++; }
}
function fakeDocument({ token } = {}) {
  const doc = { activeElement: null, createElement: tag => { const n = new Node(tag); n.ownerDocument = doc; return n; },
    querySelector: sel => sel === 'meta[name="forge-ui-token"]' && token !== undefined ? { getAttribute: () => token } : null };
  return doc;
}
const answer = (status, data) => ({ status, json: async () => { if (data === undefined) throw new Error('no body'); return data; } });
function env({ token, replies = [], document = fakeDocument({ token }) } = {}) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    const next = replies.length > 1 ? replies.shift() : replies[0];
    if (next instanceof Error) throw next;
    return next;
  };
  return { calls, env: { document, fetch } };
}

test('a POST carries the token, a JSON content type and the body, and nothing else does', () => {
  const init = U.requestInit('POST', { action: 'unpin', task: 'graph' }, 'tok-123');
  assert.equal(init.method, 'POST');
  assert.deepEqual(init.headers, { 'X-Forge-UI-Token': 'tok-123', 'Content-Type': 'application/json' });
  assert.equal(init.body, '{"action":"unpin","task":"graph"}');
  assert.equal(init.cache, 'no-store');
  const read = U.requestInit('GET');
  assert.deepEqual(read.headers, {});
  assert.equal(read.body, undefined);
});

test('post sends the page token to the path once, and get sends no token', async () => {
  const { calls, env: e } = env({ token: 'tok-abc', replies: [answer(200, { ok: true })] });
  const ui = U.create(e);
  assert.equal(ui.token(), 'tok-abc');
  await ui.post('/api/runs/0123456789ab/controls', { action: 'set', key: 'qa-threshold', value: 'P2' });
  await ui.get('/api/models');
  assert.equal(calls.length, 2);
  assert.equal(calls[0].url, '/api/runs/0123456789ab/controls');
  assert.equal(calls[0].init.headers['X-Forge-UI-Token'], 'tok-abc');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0].init.body), { action: 'set', key: 'qa-threshold', value: 'P2' });
  assert.equal(calls[1].init.method, 'GET');
  assert.equal('X-Forge-UI-Token' in calls[1].init.headers, false);
});

test('post never retries: a timeout, a refusal or a dead network is one request', async () => {
  for (const reply of [answer(504, { error: 'timeout', detail: 'x' }), answer(409, { error: 'busy' }), new Error('network down')]) {
    const { calls, env: e } = env({ token: 't', replies: [reply] });
    const res = await U.create(e).post('/api/runs/0123456789ab/controls', { action: 'unpin', task: 'a' });
    assert.equal(calls.length, 1);
    assert.equal(res.ok, false);
  }
});

test('post without a token sends nothing', async () => {
  const { calls, env: e } = env({ replies: [answer(200, { ok: true })] });
  const res = await U.create(e).post('/api/runs/0123456789ab/controls', {});
  assert.equal(calls.length, 0);
  assert.equal(res.ok, false);
  assert.equal(res.status, 0);
  assert.match(res.detail, /read-only/);
});

test('an HTTP answer maps to {ok, status, data, error, detail, result}', () => {
  const ok = U.mapResponse(200, { ok: true, changed: { key: 'timeout' }, applies: "at the task's next attempt", controls: { run: {} } });
  assert.deepEqual([ok.ok, ok.status, ok.error, ok.detail, ok.result], [true, 200, null, null, null]);
  assert.equal(ok.data.applies, "at the task's next attempt");

  const invalid = U.mapResponse(400, { error: 'invalid', detail: 'qa-threshold needs P0, P1, P2, P3 or none' });
  assert.deepEqual([invalid.ok, invalid.status, invalid.error, invalid.detail], [false, 400, 'invalid', 'qa-threshold needs P0, P1, P2, P3 or none']);

  const cli = { ok: false, task: 'graph' };                           // the CLI's own stdout object, passed through
  const refused = U.mapResponse(409, { error: 'refused', detail: 'retry-budget can only change while a run is live', result: cli });
  assert.deepEqual([refused.ok, refused.status, refused.error], [false, 409, 'refused']);
  assert.equal(refused.result, cli);

  const busy = U.mapResponse(409, { error: 'busy' });
  assert.deepEqual([busy.error, busy.detail, busy.result], ['busy', null, null]);

  const slow = U.mapResponse(504, { error: 'timeout', detail: 'the operation may have partly completed; reload to see the current state' });
  assert.deepEqual([slow.ok, slow.status, slow.error], [false, 504, 'timeout']);
});

test('an answer that is not what the server sends is a failure with a reason', () => {
  assert.deepEqual(U.mapResponse(502, null), { ok: false, status: 502, data: null, error: 'http 502', detail: null, result: null });
  const empty = U.mapResponse(200, null);
  assert.equal(empty.ok, false);
  assert.equal(empty.error, 'bad response');
  const down = U.mapResponse(0, null);
  assert.deepEqual([down.ok, down.error, down.detail], [false, 'network', 'could not reach forge-ui']);
});

test('controls are enabled only with a token and a health answer of controls:true, asked once', async () => {
  const none = env({ replies: [answer(200, { controls: true })] });                  // no token: read-only server or an export
  assert.equal(await U.create(none.env).controlsEnabled(), false);
  assert.equal(none.calls.length, 0, 'no token, no request');

  const off = env({ token: 't', replies: [answer(200, { app: 'forge-ui', controls: false })] });
  assert.equal(await U.create(off.env).controlsEnabled(), false);

  const on = env({ token: 't', replies: [answer(200, { app: 'forge-ui', controls: true })] });
  const ui = U.create(on.env);
  assert.equal(await ui.controlsEnabled(), true);
  assert.equal(await ui.controlsEnabled(), true);
  assert.equal(on.calls.length, 1, 'cached for the page load');
  assert.equal(on.calls[0].url, '/api/health');

  const old = env({ token: 't', replies: [answer(200, { app: 'forge-ui' })] });     // a server that predates the write API
  assert.equal(await U.create(old.env).controlsEnabled(), false);
});

test('a health check that got no answer is asked again, not remembered as read-only', async () => {
  const { calls, env: e } = env({ token: 't', replies: [new Error('offline'), answer(200, { controls: true })] });
  const ui = U.create(e);
  assert.equal(await ui.controlsEnabled(), false);
  assert.equal(await ui.controlsEnabled(), true);
  assert.equal(calls.length, 2);
});

test('the result line says what happened', () => {
  assert.deepEqual(U.outcomeText({ ok: true }, 'Saved'), { tone: 'ok', text: 'Saved' });
  assert.deepEqual(U.outcomeText({ ok: false, status: 409, error: 'busy' }), { tone: 'bad', text: 'busy, try again' });
  assert.match(U.outcomeText({ ok: false, status: 504, error: 'timeout' }).text, /partly completed/);
  assert.equal(U.outcomeText({ ok: false, status: 409, error: 'refused', detail: 'task graph is MERGED' }).text, 'task graph is MERGED');
  assert.equal(U.outcomeText({ ok: false, status: 400, error: 'invalid', detail: 'needs a whole number' }).tone, 'bad');
  assert.match(U.outcomeText({ ok: false, status: 403, error: 'forbidden' }).text, /reload/);
  assert.equal(U.outcomeText({ ok: false, status: 502, error: 'bad gateway' }).text, 'bad gateway');
});

test('outcome keeps one aria-live line in its host and replaces its words', async () => {
  const { env: e } = env({ token: 't', replies: [answer(200, {})] });
  const ui = U.create(e), host = e.document.createElement('div');
  ui.outcome(host, { ok: true }, 'Saved. Applies at the next retry decision.');
  await new Promise(r => setTimeout(r, 5));                            // a new live region is filled a tick later
  const line = host.children[0];
  assert.equal(host.children.length, 1);
  assert.equal(line.attrs['aria-live'], 'polite');
  assert.equal(line.attrs.role, 'status');
  assert.match(line.className, /ui-outcome ok/);
  assert.equal(line.children[0].className, 'lamp st-pass');
  assert.equal(line.children[1].textContent, 'Saved. Applies at the next retry decision.');
  ui.outcome(host, { ok: false, status: 409, error: 'busy' });
  assert.equal(host.children.length, 1);
  assert.match(line.className, /ui-outcome bad/);
  assert.equal(line.children[0].className, 'lamp st-fail');
  assert.equal(line.children[1].textContent, 'busy, try again');
});

test('confirmInline answers true for Confirm, false for Cancel and for Escape, and leaves nothing behind', async () => {
  const { env: e } = env({ token: 't', replies: [answer(200, {})] });
  const ui = U.create(e), host = e.document.createElement('div'), opener = e.document.createElement('button');
  host.isConnected = opener.isConnected = true;
  const ask = (opts) => {
    e.document.activeElement = opener;
    const pending = ui.confirmInline(host, 'Set QA threshold for task `x` to P2? Takes effect at its next attempt', opts);
    return { pending, strip: host.children[0] };
  };
  const press = (strip, label) => strip.children.find(c => c.textContent === label).fire('click');

  let { pending, strip } = ask();
  assert.equal(strip.children[1].textContent, 'Set QA threshold for task `x` to P2? Takes effect at its next attempt');
  assert.equal(e.document.activeElement.textContent, 'Confirm', 'focus starts on the key that confirms');
  press(strip, 'Confirm');
  assert.equal(await pending, true);
  assert.equal(host.children.length, 0);

  ({ pending, strip } = ask());
  press(strip, 'Cancel');
  assert.equal(await pending, false);

  const escape = { key: 'Escape' };
  ({ pending, strip } = ask());
  strip.fire('keydown', escape);
  assert.equal(await pending, false);
  assert.equal(escape.stopped, true, 'Escape does not reach the page\'s own handlers');
  assert.equal(host.children.length, 0);

  ({ pending, strip } = ask());
  strip.fire('keydown', { key: 'Enter' });                              // other keys do nothing
  press(strip, 'Confirm');
  assert.equal(await pending, true);
});

test('a destructive confirm starts on Cancel, and a second ask cancels the first', async () => {
  const { env: e } = env({ token: 't', replies: [answer(200, {})] });
  const ui = U.create(e), host = e.document.createElement('div');
  const first = ui.confirmInline(host, 'Delete this run?', { danger: true });
  const strip = host.children[0];
  assert.match(strip.className, /ui-confirm danger/);
  assert.equal(e.document.activeElement.textContent, 'Cancel', 'a stray Enter cannot delete');
  assert.equal(strip.children.find(c => c.textContent === 'Confirm').className, 'ui-danger');
  const second = ui.confirmInline(host, 'Delete the other one?');
  assert.equal(await first, false);
  assert.equal(host.children.length, 1);
  host.children[0].children.find(c => c.textContent === 'Confirm').fire('click');
  assert.equal(await second, true);
});
