'use strict';
// node --test: the Worktrees screen's pure logic (worktreesview.js), and its controls over a fake document.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

// A small document, enough for ForgeUI and ForgeTable. Set up before worktreesview loads so its root is global.
class Element {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.nodeName = this.tagName;
    this.className = '';
    this.children = [];
    this.parent = null;
    this._text = '';
    this._on = {};
    this.attrs = {};
    this.title = '';
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.indeterminate = false;
    this.value = '';
    this.id = '';
    this.isConnected = true;
  }
  get textContent() {
    if (this.nodeName === '#TEXT') return this._text;
    let out = this._text || '';
    for (const child of this.children) out += child.textContent == null ? '' : child.textContent;
    return out;
  }
  set textContent(value) {
    this._text = value == null ? '' : String(value);
    for (const child of this.children) child.parent = null;
    this.children = [];
  }
  get classList() {
    const node = this;
    const parts = () => String(node.className || '').split(/\s+/).filter(Boolean);
    return {
      contains: c => parts().includes(c),
      add(...cs) { const s = new Set(parts()); for (const c of cs) s.add(c); node.className = [...s].join(' '); },
      remove(...cs) { const s = new Set(parts()); for (const c of cs) s.delete(c); node.className = [...s].join(' '); },
      toggle(c, force) {
        const on = force === undefined ? !parts().includes(c) : !!force;
        if (on) this.add(c); else this.remove(c);
        return on;
      },
    };
  }
  get dataset() {
    if (!this._dataset) {
      const node = this;
      this._dataset = new Proxy({}, {
        get(_t, key) {
          if (typeof key !== 'string') return undefined;
          const value = node.getAttribute('data-' + key);
          return value == null ? undefined : value;
        },
        set(_t, key, value) { node.setAttribute('data-' + key, value); return true; },
      });
    }
    return this._dataset;
  }
  set type(value) { this._type = String(value); this.setAttribute('type', this._type); }
  get type() { return this._type || ''; }
  append(...kids) {
    for (const kid of kids) {
      if (kid == null) continue;
      if (kid.parent && kid.parent !== this) kid.parent.children = kid.parent.children.filter(c => c !== kid);
      kid.parent = this;
      this.children.push(kid);
    }
  }
  replaceChildren(...kids) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this._text = '';
    this.append(...kids);
  }
  insertBefore(node, ref) {
    if (node.parent && node.parent !== this) node.parent.children = node.parent.children.filter(c => c !== node);
    node.parent = this;
    const at = ref ? this.children.indexOf(ref) : -1;
    if (at < 0) this.children.push(node); else this.children.splice(at, 0, node);
    return node;
  }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this);
    this.parent = null;
    this.isConnected = false;
  }
  // ForgeTable redraws by swapping its root. Without this, a header click throws and the select-all bug cannot show.
  replaceWith(...nodes) {
    const parent = this.parent;
    const prepared = [];
    for (const n of nodes) {
      if (n == null) continue;
      if (n.parent) n.parent.children = n.parent.children.filter(c => c !== n);
      if (parent) { n.parent = parent; n.isConnected = parent.isConnected !== false; }
      else { n.parent = null; n.isConnected = false; }
      prepared.push(n);
    }
    if (!parent) { this.isConnected = false; return; }
    const at = parent.children.indexOf(this);
    this.parent = null;
    this.isConnected = false;
    if (at < 0) parent.children.push(...prepared);
    else parent.children.splice(at, 1, ...prepared);
  }
  contains(node) { return node === this || this.children.some(c => c.contains && c.contains(node)); }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
  addEventListener(type, fn) { (this._on[type] || (this._on[type] = [])).push(fn); }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
}
function descendants(node) {
  const out = [];
  for (const child of node.children || []) {
    if (!child.tagName || child.nodeName === '#TEXT') continue;
    out.push(child);
    out.push(...descendants(child));
  }
  return out;
}
function matches(node, simple) {
  if (!node || node.nodeName === '#TEXT' || !simple) return false;
  let rest = simple, tag = '';
  if (/^[a-zA-Z]/.test(rest)) {
    const found = rest.match(/^[a-zA-Z][\w-]*/);
    tag = found[0].toUpperCase();
    rest = rest.slice(found[0].length);
  }
  if (tag && node.tagName !== tag) return false;
  while (rest.startsWith('.')) {
    const found = rest.match(/^\.([a-zA-Z0-9_-]+)/);
    if (!found || !node.classList.contains(found[1])) return false;
    rest = rest.slice(found[0].length);
  }
  while (rest.startsWith('[')) {
    const found = rest.match(/^\[([^\]]+)\]/);
    if (!found) return false;
    const body = found[1], eq = body.indexOf('=');
    const name = eq < 0 ? body : body.slice(0, eq);
    let want = eq < 0 ? null : body.slice(eq + 1);
    if (want != null && ((want.startsWith('"') && want.endsWith('"')) || (want.startsWith("'") && want.endsWith("'")))) want = want.slice(1, -1);
    const got = node.getAttribute(name);
    if (want == null) { if (got == null) return false; }
    else if (got !== want && !(name === 'type' && node.type === want)) return false;
    rest = rest.slice(found[0].length);
  }
  return rest === '';
}
function splitSelector(selector) {
  const parts = [];
  let cur = '', quote = '';
  for (const ch of String(selector).trim()) {
    if (quote) { cur += ch; if (ch === quote) quote = ''; continue; }
    if (ch === '"' || ch === "'") { quote = ch; cur += ch; continue; }
    if (/\s/.test(ch)) { if (cur) { parts.push(cur); cur = ''; } continue; }
    cur += ch;
  }
  if (cur) parts.push(cur);
  return parts;
}
function queryAll(node, selector) {
  const parts = splitSelector(selector);
  function search(scope, index) {
    const out = [];
    for (const candidate of descendants(scope)) {
      if (!matches(candidate, parts[index])) continue;
      if (index === parts.length - 1) out.push(candidate);
      else out.push(...search(candidate, index + 1));
    }
    return out;
  }
  return parts.length ? search(node, 0) : [];
}
Element.prototype.querySelectorAll = function (selector) { return queryAll(this, selector); };
Element.prototype.querySelector = function (selector) { return queryAll(this, selector)[0] || null; };
const document = {
  activeElement: null,
  visibilityState: 'visible',
  createElement(tag) { const node = new Element(tag); node.ownerDocument = document; return node; },
  createTextNode(text) { const node = new Element('#text'); node.textContent = text == null ? '' : String(text); node.ownerDocument = document; return node; },
  querySelector() { return null; },
  querySelectorAll() { return []; },
};
global.Node = Element;
global.document = document;
global.window = global;
global.self = global;
const W = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'worktreesview.js'));
const U = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'uikit.js'));
const Table = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'datatable.js'));

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

test('removable rows are failed, finished, and unknown leftovers; every other status says why', () => {
  for (const status of ['failed', 'finished']) {
    assert.equal(W.removable(row('/p', { status })), true, status);
    assert.equal(W.blockReason(row('/p', { status })), null, status);
  }
  assert.equal(W.removable(row('/p', { status: 'unknown', leftover: true })), true);
  assert.equal(W.blockReason(row('/p', { status: 'unknown', leftover: true })), null);
  const blocked = {
    in_use: 'its run is active',
    dirty: 'uncommitted changes',
    locked: 'locked by git',
    needed: 'its run still needs it',
    missing: 'already gone (prune)',
    foreign: "not forge's",
    removed: 'already removed',
  };
  for (const [status, why] of Object.entries(blocked)) {
    assert.equal(W.removable(row('/p', { status, leftover: true })), false, status);
    assert.equal(W.blockReason(row('/p', { status })), why, status);
  }
  assert.equal(W.removable(row('/p', { status: 'unknown' })), false);
  assert.equal(W.removable(row('/p', { status: 'unknown', leftover: false })), false);
  assert.equal(W.blockReason(row('/p', { status: 'unknown' })), 'not a leftover');
  assert.equal(W.removable(null), false);
  assert.equal(W.blockReason(null), null);
  assert.equal(W.blockReason(row('/p', { status: 'mystery' })), 'mystery');
});

test('selectable keeps the filtered view and drops rows that cannot be removed', () => {
  const all = [
    row('/a', { status: 'finished', repo: '/r/app', run: 'k1' }),
    row('/b', { status: 'failed', repo: '/r/app', run: 'k1' }),
    row('/c', { status: 'dirty', repo: '/r/app', run: 'k1' }),
    row('/d', { status: 'finished', repo: '/r/app', run: 'k2' }),
    row('/e', { status: 'unknown', leftover: true, repo: '/s/lib', run: 'k1' }),
    row('/f', { status: 'unknown', leftover: false, repo: '/s/lib', run: 'k1' }),
    row('/g', { status: 'in_use', repo: '/s/lib', run: 'k1' }),
    row('/h', { status: 'removed', repo: '/r/app', run: 'k1' }),
  ];
  assert.deepEqual(W.selectable(all, { repo: '/r/app' }), ['/a', '/b', '/d']);
  assert.deepEqual(W.selectable(all, { run: 'k2' }), ['/d']);
  assert.deepEqual(W.selectable(all, { repo: '/s/lib', run: 'k1' }), ['/e']);
  assert.deepEqual(W.selectable(all, { repo: '/r/app', status: 'disk' }), ['/a', '/b', '/d']);
  assert.deepEqual(W.selectable(all, { status: 'failed' }), ['/b']);
  assert.deepEqual(W.selectable([], { repo: '/r/app' }), []);
  assert.deepEqual(W.selectable(null, {}), []);
});

test('summaryText counts a dry run and names each skip with its reason', () => {
  const text = W.summaryText({ ok: true, dry_run: true, rows: [
    { path: '/wt/a', status: 'would-remove', reason: null },
    { path: '/wt/b', status: 'would-remove', reason: '' },
    { path: '/wt/<b>c</b>', status: 'skipped', reason: 'its run is active' },
    { path: '/wt/d', status: 'skipped', reason: '' },
  ] });
  assert.match(text, /^2 worktrees would be removed\./);
  assert.match(text, /Skipped: \/wt\/<b>c<\/b> \(its run is active\); \/wt\/d\./);
  assert.equal(W.summaryText({ rows: [{ path: '/wt/a', status: 'would-remove' }] }), '1 worktree would be removed.');
  assert.equal(W.summaryText({ rows: [] }), '0 worktrees would be removed.');
  assert.equal(W.summaryText(null), '0 worktrees would be removed.');
  assert.match(W.summaryText({ rows: [
    { path: '/wt/a', status: 'would-remove' },
    { path: '/wt/b', status: 'failed', reason: 'permission denied' },
  ] }), /1 worktree would be removed\. Failed: \/wt\/b \(permission denied\)\./);
});

test('resultLines keeps one line per path for a mixed removal', () => {
  assert.deepEqual(W.resultLines({ ok: false, dry_run: false, rows: [
    { path: '/wt/a', action: 'remove', status: 'removed', reason: null },
    { path: '/wt/b', action: 'none', status: 'skipped', reason: 'locked by git' },
    { path: '/wt/c', action: 'remove', status: 'failed', reason: 'permission denied' },
  ] }), [
    { path: '/wt/a', status: 'removed', reason: '' },
    { path: '/wt/b', status: 'skipped', reason: 'locked by git' },
    { path: '/wt/c', status: 'failed', reason: 'permission denied' },
  ]);
  assert.deepEqual(W.resultLines(null), []);
  assert.deepEqual(W.resultLines({ rows: [null, { status: 'removed' }] }), [{ path: '', status: 'removed', reason: '' }]);
});

function tagSet(root) {
  const names = new Set();
  (function walk(node) {
    if (node.tagName && node.nodeName !== '#TEXT') names.add(node.tagName);
    for (const child of node.children || []) walk(child);
  })(root);
  return names;
}
function fire(node, type) {
  for (const fn of (node._on && node._on[type]) || []) fn({ preventDefault() {}, stopPropagation() {}, target: node });
}
function buttons(root, text) {
  return root.querySelectorAll('button').filter(b => b.textContent === text);
}
async function settle() {
  for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r));
}
function guardTimers() {
  const orig = global.setTimeout, origClear = global.clearTimeout, ids = new Set();
  global.setTimeout = (fn, ms, ...args) => {
    const id = orig(fn, ms, ...args);
    ids.add(id);
    if (id && typeof id.unref === 'function') id.unref();
    return id;
  };
  global.clearTimeout = id => { ids.delete(id); return origClear(id); };
  return () => {
    for (const id of [...ids]) origClear(id);
    global.setTimeout = orig;
    global.clearTimeout = origClear;
  };
}
const wt = (p, extra = {}) => ({
  path: p, repo: '/r/app', run: 'k1', run_id: 'run-1', status: 'finished', kind: 'task', branch: 'forge/<i>x</i>', merged: false, size: 10,
  commands: [{ label: 'remove the worktree', command: 'git worktree remove ' + p }], ...extra,
});
function screenData(rows) {
  return { rows, runs: [], notes: [], computed: 1700000000, sizes_pending: 0, totals: { by_status: {} } };
}
async function renderScreen({ controls, rows, onPost }) {
  const calls = [];
  const ui = U.create({ document });
  ui.controlsEnabled = () => Promise.resolve(!!controls);
  ui.get = async (p) => {
    calls.push({ method: 'GET', path: p });
    return { ok: true, status: 200, data: screenData(rows), error: null, detail: null, result: null };
  };
  ui.post = async (p, body) => {
    calls.push({ method: 'POST', path: p, body: JSON.parse(JSON.stringify(body)) });
    return onPost ? onPost(body) : { ok: false, status: 500, error: 'failed', detail: 'no handler', data: null, result: null };
  };
  global.ForgeUI = ui;
  global.ForgeTable = Table;
  const host = document.createElement('section');
  W.render(host, {});
  await settle();
  host._calls = calls;
  return host;
}
const posts = host => host._calls.filter(c => c.method === 'POST');

test('read-only renders no removal controls and keeps every copy button', async () => {
  const stop = guardTimers();
  try {
    const rows = [wt('/r/.forge-worktrees/fin'), wt('/r/.forge-worktrees/dirt', { status: 'dirty' })];
    const host = await renderScreen({ controls: false, rows, onPost: () => { throw new Error('posted'); } });
    assert.equal(posts(host).length, 0);
    assert.equal(host.querySelector('tbody input'), null);
    assert.equal(host.querySelector('[aria-label="Select all removable worktrees"]'), null);
    assert.equal(buttons(host, 'Remove').length, 0);
    assert.equal(host.textContent.includes('Remove selected'), false);
    assert.equal(host.textContent.includes('Delete merged branches too'), false);
    assert.equal(host.textContent.includes('this page runs nothing'), true);
    assert.equal(buttons(host, 'Copy').length, 2);
    assert.equal(host.textContent.includes('git worktree remove /r/.forge-worktrees/fin'), true);
    assert.match(host.textContent, /forge\/<i>x<\/i>/);
    assert.equal(tagSet(host).has('I'), true, 'the status lamp is an element');
  } finally { stop(); }
});

test('controls put a checkbox and Remove only on removable rows, and select the filtered view', async () => {
  const stop = guardTimers();
  try {
    const rows = [
      wt('/r/.forge-worktrees/fin'),
      wt('/r/.forge-worktrees/bad', { status: 'failed' }),
      wt('/r/.forge-worktrees/dirt', { status: 'dirty', commands: [{ label: 'status', command: 'git -C /r status' }] }),
      wt('/s/.forge-worktrees/lib', { repo: '/s/lib', run: 'k2', status: 'finished' }),
      wt('/r/.forge-worktrees/<b>left</b>', { status: 'unknown', leftover: true }),
      wt('/r/.forge-worktrees/gone', { status: 'unknown', leftover: false }),
    ];
    let posted = 0;
    const host = await renderScreen({
      controls: true, rows,
      onPost: () => { posted += 1; return { ok: false, status: 400, error: 'invalid', detail: 'stop', data: null }; },
    });
    assert.equal(posted, 0);
    assert.equal(host.textContent.includes('runs nothing'), false);
    assert.match(host.textContent, /forge worktrees rm/);
    assert.equal(buttons(host, 'Copy').length, rows.length);
    assert.match(host.textContent, /\/r\/\.forge-worktrees\/<b>left<\/b>/);
    assert.equal(tagSet(host).has('B'), false);
    assert.equal(tagSet(host).has('IMG'), false);
    const removablePaths = ['/r/.forge-worktrees/fin', '/r/.forge-worktrees/bad', '/s/.forge-worktrees/lib', '/r/.forge-worktrees/<b>left</b>'];
    for (const p of removablePaths) {
      const box = host.querySelector(`[aria-label="Select ${p}"]`);
      assert.equal(box.disabled, false, p);
      assert.equal(box.parent.querySelector('button').textContent, 'Remove');
    }
    for (const [p, why] of [['/r/.forge-worktrees/dirt', 'uncommitted changes'], ['/r/.forge-worktrees/gone', 'not a leftover']]) {
      const box = host.querySelector(`[aria-label="${why}"]`);
      assert.equal(box.disabled, true, p);
      assert.equal(box.title, why);
      assert.match(box.parent.textContent, new RegExp(why));
      assert.equal(box.parent.querySelector('button'), null);
    }
    assert.equal(buttons(host, 'Remove').length, removablePaths.length);
    const bulk = buttons(host, 'Remove selected (0)')[0];
    assert.equal(bulk.disabled, true);
    const all = host.querySelector('[aria-label="Select all removable worktrees"]');
    all.checked = true; fire(all, 'change');
    assert.equal(bulk.textContent, 'Remove selected (4)');
    assert.equal(bulk.disabled, false);
    const dirt = host.querySelector('[aria-label="uncommitted changes"]');
    assert.equal(dirt.checked, false);
    const repo = host.querySelector('[data-filter="repo"]');
    repo.value = '/s/lib'; fire(repo, 'change');
    assert.equal(bulk.textContent, 'Remove selected (1)');
    const shown = host.querySelectorAll('tbody input').filter(b => !b.disabled);
    assert.deepEqual(shown.map(b => b._path), ['/s/.forge-worktrees/lib']);
    assert.equal(shown[0].checked, true);
    assert.equal(buttons(host, 'Copy').length, 1);
  } finally { stop(); }
});

function selectAllBox(host) {
  const th = host.querySelector('th[data-col="select"]');
  const box = th && th.querySelector('[aria-label="Select all removable worktrees"]');
  assert.ok(box, 'select-all stays in the Select header');
  return box;
}
function clickHeader(host, col) {
  const btn = host.querySelector(`th[data-col="${col}"] button`);
  assert.ok(btn, col + ' header');
  fire(btn, 'click');
}

test('the select-all checkbox survives sort, a column move and reset', async () => {
  const stop = guardTimers();
  try {
    const rows = [
      wt('/r/.forge-worktrees/fin'),
      wt('/r/.forge-worktrees/bad', { status: 'failed' }),
      wt('/r/.forge-worktrees/dirt', { status: 'dirty' }),
    ];
    const host = await renderScreen({ controls: true, rows, onPost: () => { throw new Error('posted'); } });
    const fin = host.querySelector('[aria-label="Select /r/.forge-worktrees/fin"]');
    fin.checked = true; fire(fin, 'change');
    assert.equal(selectAllBox(host).indeterminate, true);
    clickHeader(host, 'status');
    clickHeader(host, 'status');
    assert.equal(selectAllBox(host).indeterminate, true, 'a second sort still redraws through the replacement root');
    assert.equal(host.querySelector('[aria-label="Select /r/.forge-worktrees/fin"]').checked, true);
    fire(host.querySelector('[aria-label="Move Status left"]'), 'click');
    assert.equal(selectAllBox(host).indeterminate, true);
    fire(buttons(host, 'Reset')[0], 'click');
    const all = selectAllBox(host);
    assert.equal(all.indeterminate, true);
    all.checked = true; fire(all, 'change');
    assert.equal(buttons(host, 'Remove selected (2)')[0].disabled, false);
    assert.equal(host.querySelector('[aria-label="uncommitted changes"]').checked, false);
    assert.equal(posts(host).length, 0);
  } finally { stop(); }
});

test('a dry run that would remove nothing does not confirm or send the real call', async () => {
  const stop = guardTimers();
  try {
    const host = await renderScreen({
      controls: true, rows: [wt('/r/.forge-worktrees/fin')],
      onPost: () => ({ ok: true, status: 200, data: { ok: true, dry_run: true, rows: [
        { path: '/r/.forge-worktrees/fin', status: 'skipped', reason: 'uncommitted changes' },
      ] }, error: null, detail: null, result: null }),
    });
    pressRemove(host, '/r/.forge-worktrees/fin');
    await settle();
    assert.equal(posts(host).length, 1);
    assert.equal(posts(host)[0].body.dry_run, true);
    assert.equal(host.querySelector('.ui-confirm'), null);
    assert.equal(host.textContent.includes('Remove 0 worktrees?'), false);
    assert.match(host.textContent, /0 worktrees would be removed/);
    assert.match(host.textContent, /Skipped: \/r\/\.forge-worktrees\/fin \(uncommitted changes\)/);
    assert.equal(host._calls.some(c => c.path === '/api/worktrees?refresh=1'), false);
    assert.equal(buttons(host, 'Remove')[0].disabled, false);
    await settle();
    assert.equal(posts(host).length, 1);
  } finally { stop(); }
});

test('a removal dry-runs, confirms, then posts, and never before the confirm', async () => {
  const stop = guardTimers();
  try {
    const target = '/r/.forge-worktrees/<img src=x>';
    const rows = [wt(target), wt('/r/.forge-worktrees/dirt', { status: 'dirty' })];
    const host = await renderScreen({
      controls: true, rows,
      onPost: body => body.dry_run
        ? { ok: true, status: 200, data: { ok: true, dry_run: true, rows: [
          { path: target, status: 'would-remove', reason: null },
          { path: '/r/.forge-worktrees/skip', status: 'skipped', reason: '<b>locked</b>' },
        ] }, error: null, detail: null, result: null }
        : { ok: true, status: 200, data: { ok: false, dry_run: false, rows: [
          { path: target, status: 'removed', reason: null },
          { path: '/r/.forge-worktrees/skip', status: 'skipped', reason: '<b>locked</b>' },
          { path: '/r/.forge-worktrees/nope', status: 'failed', reason: '<script>x</script>' },
        ] }, error: null, detail: null, result: null },
    });
    const branches = host.querySelectorAll('label').find(l => l.textContent.includes('Delete merged branches too')).querySelector('input');
    branches.checked = true; fire(branches, 'change');
    pressRemove(host, target);
    pressRemove(host, target);   // a second click while the first is in flight sends nothing more
    await settle();
    assert.equal(posts(host).length, 1);
    assert.deepEqual(posts(host)[0].body, { paths: [target], branches: true, dry_run: true });
    const strip = host.querySelector('.ui-confirm');
    assert.match(strip.textContent, /Remove 1 worktrees\?/);
    assert.match(host.textContent, /1 worktree would be removed/);
    assert.match(host.textContent, /<b>locked<\/b>/);
    assert.equal(host.querySelector('img'), null);
    assert.equal(host.querySelector('b'), null);
    assert.equal(host.querySelector('script'), null);
    fire(buttons(strip, 'Confirm')[0], 'click');
    await settle();
    assert.equal(posts(host).length, 2);
    assert.deepEqual(posts(host)[1].body, { paths: [target], branches: true, dry_run: false });
    assert.deepEqual(posts(host).map(c => c.path), ['/api/worktrees/remove', '/api/worktrees/remove']);
    const outcome = host.querySelectorAll('div.wt-notes').find(n => n.querySelector('li'));
    const lines = outcome.querySelectorAll('li').map(li => li.textContent);
    assert.deepEqual(lines, [
      target + ' removed',
      '/r/.forge-worktrees/skip skipped (<b>locked</b>)',
      '/r/.forge-worktrees/nope failed (<script>x</script>)',
    ]);
    assert.equal(host.querySelector('img'), null);
    assert.equal(host.querySelector('script'), null);
    assert.equal(host._calls.filter(c => c.method === 'GET').map(c => c.path).at(-1), '/api/worktrees?refresh=1');
    const refreshAt = host._calls.findLastIndex(c => c.method === 'GET' && c.path === '/api/worktrees?refresh=1');
    const realAt = host._calls.findIndex(c => c.method === 'POST' && c.body.dry_run === false);
    assert.ok(refreshAt > realAt);
  } finally { stop(); }
});

function pressRemove(host, path) {
  const box = host.querySelector(`[aria-label="Select ${path}"]`);
  fire(box.parent.querySelector('button'), 'click');
}

test('cancel after the dry run sends no removal', async () => {
  const stop = guardTimers();
  try {
    const host = await renderScreen({
      controls: true, rows: [wt('/r/.forge-worktrees/fin')],
      onPost: () => ({ ok: true, status: 200, data: { ok: true, dry_run: true, rows: [{ path: '/r/.forge-worktrees/fin', status: 'would-remove', reason: null }] }, error: null, detail: null, result: null }),
    });
    pressRemove(host, '/r/.forge-worktrees/fin');
    await settle();
    assert.equal(posts(host).length, 1);
    fire(buttons(host.querySelector('.ui-confirm'), 'Cancel')[0], 'click');
    await settle();
    assert.equal(posts(host).length, 1);
    assert.equal(posts(host)[0].body.dry_run, true);
    assert.equal(host._calls.some(c => c.path === '/api/worktrees?refresh=1'), false);
  } finally { stop(); }
});

test('a 409 says a removal is already running, and any other error uses outcomeText', async () => {
  const stop = guardTimers();
  try {
    const busy = await renderScreen({
      controls: true, rows: [wt('/r/.forge-worktrees/fin')],
      onPost: () => ({ ok: false, status: 409, error: 'busy', detail: null, data: null, result: null }),
    });
    pressRemove(busy, '/r/.forge-worktrees/fin');
    await settle();
    assert.equal(posts(busy).length, 1);
    assert.equal(busy.querySelector('.ui-confirm'), null);
    assert.match(busy.textContent, /busy: another removal is running/);
    const bad = await renderScreen({
      controls: true, rows: [wt('/r/.forge-worktrees/fin')],
      onPost: () => ({ ok: false, status: 400, error: 'invalid', detail: 'bad path', data: null, result: null }),
    });
    pressRemove(bad, '/r/.forge-worktrees/fin');
    await settle();
    assert.equal(bad.querySelector('.ui-confirm'), null);
    assert.match(bad.textContent, /bad path/);
    assert.equal(posts(bad).length, 1);
  } finally { stop(); }
});
