'use strict';
// node --test: memories/add and memories/edit bodies, the copyable command, and the Memories tab flow.
// No browser. The page renders against the same fake document actions.test.js uses.
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
  createTextNode(text) {
    const node = new Node('#text');
    node.textContent = text == null ? '' : String(text);
    return node;
  },
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

const state = {
  controls: false,
  gets: [],
  posts: [],
  getReply: () => ({ ok: false, status: 404, error: 'missing' }),
  postReply: () => ({ ok: true, status: 200, data: { ok: true, checkouts: [] } }),
};
const U = require(path.join(root, 'uikit.js'));
const ui = U.create({ document });
ui.controlsEnabled = async () => state.controls;
ui.get = async (p) => { state.gets.push(p); return state.getReply(p); };
ui.post = async (p, body) => { state.posts.push({ path: p, body }); return state.postReply(p, body); };
global.ForgeUI = ui;
global.ForgeTable = require(path.join(root, 'datatable.js'));
const P = require(path.join(root, 'projectsview.js'));

function matchesSel(node, selector) {
  const parts = String(selector).match(/\[[^\]]+\]/g);
  if (!parts || parts.join('') !== selector) return false;
  return parts.every(part => {
    const body = part.slice(1, -1);
    const eq = body.indexOf('=');
    const name = (eq === -1 ? body : body.slice(0, eq)).replace(/^data-/, '');
    const value = node.dataset && node.dataset[name];
    if (eq === -1) return value != null && value !== '';
    return String(value) === body.slice(eq + 1).replace(/^["']|["']$/g, '');
  });
}
function walk(node) {
  const out = [];
  for (const child of node.children || []) { out.push(child); out.push(...walk(child)); }
  return out;
}
function buttons(node) { return walk(node).filter(n => n.tagName === 'BUTTON'); }
function button(node, text) { return buttons(node).find(n => n.textContent === text); }
function labelled(node, label) { return walk(node).find(n => n.getAttribute && n.getAttribute('aria-label') === label); }
function field(node, name) { return walk(node).find(n => n.dataset && n.dataset.field === name); }
function confirmText(node) {
  const span = walk(node).find(n => String(n.className || '').split(/\s+/).includes('ui-confirm-text'));
  return span ? span.textContent : '';
}
function click(node) {
  assert.ok(node, 'no such control');
  if (node.disabled) return false;
  if (node.type === 'checkbox') node.checked = !node.checked;
  const event = { preventDefault() {}, stopPropagation() {}, target: node };
  for (const fn of node.listeners.click || []) fn(event);
  for (const fn of node.listeners.change || []) fn(event);
  return true;
}
function setValue(node, value) {
  node.value = value;
  for (const fn of node.listeners.input || []) fn({});
}
async function until(pred) {
  for (let i = 0; i < 40; i++) {
    if (pred()) return;
    await new Promise(r => setTimeout(r, 0));
  }
  assert.fail('timed out');
}
function mount() {
  const node = document.createElement('div');
  node._root = true;
  return node;
}
function learningBox(node, text) {
  return walk(node).find(n => String(n.className || '').split(/\s+/).includes('prj-learning') && n.textContent.includes(text));
}
function codes(node) { return walk(node).filter(n => n.tagName === 'CODE').map(n => n.textContent); }

const learnings = [
  { category: 'trap', key: 'alpha', text: 'Alpha landmine', count: 2, last: 'today', manual: true,
    states: { '/repo': 'in_memory' }, rows: [{ run_id: 'r', task: 't', role: 'person', model: 'm', from: ['/repo'] }] },
  { category: 'verify', key: 'beta', text: 'Beta check', count: 1, last: 'today',
    states: { '/repo': 'in_memory' }, rows: [{ run_id: 'r', task: 't', role: 'dwarf', model: 'm', from: ['/repo'] }] },
  { category: 'finding', key: 'by hand', text: 'typed once', count: 1, last: 'today',
    states: { '/repo': 'in_memory' }, rows: [{ run_id: 'manual-1', task: '', role: 'person', model: '', from: [] }] },
];
const memories = {
  notes: [], runs: 1, memory_off_runs: 0, differs: false, learnings,
  sources: [{ path: '/repo', root: true, max_lines: 40, memory: [{ title: 'Traps', roles: ['dwarf'], lines: [{ text: 'keep' }] }] }],
};
const emptyMemories = {
  notes: [], runs: 0, memory_off_runs: 0, differs: false, learnings: [],
  sources: [{ path: '/repo', root: true, max_lines: 40, memory: null }],
};
const added = {
  ok: true, status: 200,
  data: { ok: true, checkouts: [{ path: '/repo', result: { ok: true, key: 'verify:keep the lock' } }] },
};
const edited = {
  ok: true, status: 200,
  data: { ok: true, checkouts: [{ path: '/repo', result: { ok: true, key: 'trap:alpha' } }] },
};

function projectGet(data) {
  return (p) => {
    if (p === '/api/projects/proj1') return { ok: true, status: 200, data: { name: 'App', root: '/repo', exists: true, runs: [], checkouts: [] } };
    if (p.startsWith('/api/projects/proj1/memories')) return { ok: true, status: 200, data };
    return { ok: false, status: 404, error: 'missing', detail: p };
  };
}
async function openMemories(data, controls) {
  state.controls = controls;
  state.posts = [];
  state.gets = [];
  state.getReply = projectGet(data);
  const h = mount();
  const view = P.render(h, { id: 'proj1', tab: 'memories', runHref: k => '#/runs/' + k });
  await until(() => h.textContent.includes(data.learnings.length ? 'Alpha landmine' : 'No memories yet'));
  for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
  return { h, view };
}

test('memoryAddBody and memoryEditBody accept one good learning and refuse one bad field', () => {
  assert.deepEqual(P.memoryAddBody({ category: 'verify', text: 'keep' }), { body: { category: 'verify', text: 'keep' } });
  assert.deepEqual(P.memoryAddBody({ category: 'trap', text: 'keep', paths: ' ui/a.py, ui/b.py, ui/a.py ', path: '/repo' }),
    { body: { category: 'trap', text: 'keep', paths: ['ui/a.py', 'ui/b.py'], path: '/repo' } });
  assert.equal(P.memoryAddBody({ category: 'finding', text: 'a'.repeat(300) }).body.text.length, 300);
  const addBad = [
    [{ category: 'note', text: 'keep' }, /category/],
    [{ category: 'verify', text: '' }, /1 to 300/],
    [{ category: 'verify', text: 'a'.repeat(301) }, /1 to 300/],
    [{ category: 'verify', text: 'a\nb' }, /1 to 300/],
    [{ category: 'verify', text: 'keep', paths: '../x' }, /\.\./],
    [{ category: 'verify', text: 'keep', paths: '/abs' }, /relative/],
    [{ category: 'verify', text: 'keep', paths: ['a,b'] }, /comma/],
    [{ category: 'verify', text: 'keep', paths: Array.from({ length: 21 }, (_, i) => 'p' + i + '.py') }, /20/],
  ];
  for (const [input, pattern] of addBad) assert.match(P.memoryAddBody(input).error, pattern, JSON.stringify(input));

  assert.deepEqual(P.memoryEditBody('trap:alpha', 'keep'), { body: { key: 'trap:alpha', text: 'keep' } });
  assert.deepEqual(P.memoryEditBody('trap:a b', 'a'.repeat(300)), { body: { key: 'trap:a b', text: 'a'.repeat(300) } });
  const editBad = [
    ['nokey', 'keep', /category:key/],
    ['trap:', 'keep', /category:key/],
    [':x', 'keep', /category:key/],
    ['-trap:x', 'keep', /category:key/],
    ['trap:x\n', 'keep', /category:key/],
    ['trap:alpha', '', /1 to 300/],
    ['trap:alpha', 'a'.repeat(301), /1 to 300/],
    ['trap:alpha', 'a\nb', /1 to 300/],
  ];
  for (const [key, text, pattern] of editBad) assert.match(P.memoryEditBody(key, text).error, pattern, key);
});

test('memoryCommand quotes a text that contains quotes and $', () => {
  assert.equal(P.memoryCommand('edit', '/repo', { key: 'trap:alpha', text: 'it\'s $HOME "now"' }),
    `forge memory edit /repo trap:alpha --text 'it'\\''s $HOME "now"'`);
  assert.equal(P.memoryCommand('edit', '/repo', { key: 'trap:a b', text: 'keep' }),
    `forge memory edit /repo 'trap:a b' --text keep`);
  assert.equal(P.memoryCommand('add', '/repo', { category: 'verify', text: 'keep' }),
    'forge memory add /repo --category verify --text keep');
  assert.equal(P.memoryCommand('add', '/repo', { category: 'trap', text: 'keep', paths: ['a b.py', 'ui/a.py'] }),
    `forge memory add /repo --category trap --text keep --path 'a b.py' --path ui/a.py`);
  assert.equal(P.memoryCommand('add', '/my repo', {}),
    `forge memory add '/my repo' --category verify --text TEXT`);
});

test('Add confirms, then posts once', async () => {
  state.postReply = () => added;
  const { h } = await openMemories(memories, true);
  await until(() => button(h, 'Forget selected (0)'));
  const text = field(h, 'add-text');
  setValue(text, 'keep the lock');
  assert.equal(field(h, 'add-text-count').textContent, '13/300');
  field(h, 'add-paths').value = 'ui/a.py, ui/b.py';
  click(button(h, 'Add learning'));
  await until(() => confirmText(h).includes('Add this learning to /repo?'));
  const asked = confirmText(h);
  assert.ok(asked.includes('verify: keep the lock'));
  assert.ok(asked.includes('paths: ui/a.py, ui/b.py'));
  assert.equal(state.posts.length, 0);
  assert.equal(button(h, 'Add learning').disabled, true);
  assert.equal(button(h, 'Forget selected (0)').disabled, true);

  click(button(h, 'Confirm'));
  await until(() => state.posts.length === 1 && h.textContent.includes('/repo: added verify:keep the lock'));
  assert.deepEqual(state.posts, [{
    path: '/api/projects/proj1/memories/add',
    body: { category: 'verify', text: 'keep the lock', paths: ['ui/a.py', 'ui/b.py'], path: '/repo' },
  }]);
  assert.ok(state.gets.includes('/api/projects/proj1/memories?refresh=1'));
  assert.equal(button(h, 'Add learning').disabled, false);
});

test('an empty or over-long add posts nothing', async () => {
  state.postReply = () => added;
  const { h } = await openMemories(memories, true);
  await until(() => button(h, 'Add learning'));
  click(button(h, 'Add learning'));
  await until(() => h.textContent.includes('1 to 300'));
  assert.equal(confirmText(h), '');
  assert.equal(state.posts.length, 0);

  const text = field(h, 'add-text');
  setValue(text, 'x'.repeat(301));
  assert.equal(field(h, 'add-text-count').textContent, '301/300');
  assert.ok(String(field(h, 'add-text-count').className).split(/\s+/).includes('warn'));
  click(button(h, 'Add learning'));
  for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
  assert.equal(confirmText(h), '');
  assert.equal(state.posts.length, 0);
});

test('Edit confirms the old and new wording, then posts once', async () => {
  state.postReply = () => edited;
  const { h } = await openMemories(memories, true);
  await until(() => labelled(h, 'Edit trap:alpha'));
  click(labelled(h, 'Edit trap:alpha'));
  const text = field(h, 'edit-text');
  assert.equal(text.value, 'Alpha landmine');
  assert.equal(field(h, 'edit-text-count').textContent, '14/300');
  setValue(text, 'Alpha landmine, checked');
  click(button(h, 'Save'));
  await until(() => confirmText(h).includes('Alpha landmine → Alpha landmine, checked'));
  assert.ok(confirmText(h).includes('Change trap:alpha in /repo?'));
  assert.equal(state.posts.length, 0);
  click(button(h, 'Confirm'));
  await until(() => state.posts.length === 1 && h.textContent.includes('/repo: edited trap:alpha'));
  assert.deepEqual(state.posts, [{
    path: '/api/projects/proj1/memories/edit',
    body: { key: 'trap:alpha', text: 'Alpha landmine, checked', path: '/repo' },
  }]);
  assert.equal(codes(h).some(c => c.startsWith('forge memory')), false);
});

test('a redraw while the confirm is open leaves Edit, Add and Forget usable', async () => {
  state.postReply = () => added;
  const { h } = await openMemories(memories, true);
  await until(() => button(h, 'Forget selected (0)'));
  click(labelled(h, 'Select trap:alpha'));
  await until(() => button(h, 'Forget selected (1)'));
  setValue(field(h, 'add-text'), 'keep the lock');
  click(button(h, 'Add learning'));
  await until(() => confirmText(h).includes('Add this learning to /repo?'));
  assert.equal(state.posts.length, 0);
  assert.equal(button(h, 'Add learning').disabled, true);
  assert.equal(button(h, 'Forget selected (1)').disabled, true);
  assert.equal(labelled(h, 'Edit trap:alpha').disabled, true);
  assert.equal(click(button(h, 'Forget selected (1)')), false);
  assert.equal(click(labelled(h, 'Edit trap:alpha')), false);

  click(button(h, 'Read again'));
  await until(() => confirmText(h) === '' && button(h, 'Forget selected (1)') && !button(h, 'Forget selected (1)').disabled);
  for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0));
  assert.equal(state.posts.length, 0);
  assert.equal(button(h, 'Add learning').disabled, false);
  assert.equal(labelled(h, 'Edit trap:alpha').disabled, false);
  assert.equal(labelled(h, 'Select trap:alpha').disabled, false);

  click(labelled(h, 'Edit trap:alpha'));
  assert.equal(field(h, 'edit-text').value, 'Alpha landmine');
  click(button(h, 'Cancel'));
  assert.equal(field(h, 'edit-text'), undefined);

  setValue(field(h, 'add-text'), 'keep the lock');
  click(button(h, 'Add learning'));
  await until(() => confirmText(h).includes('verify: keep the lock'));
  assert.equal(state.posts.length, 0);
  click(button(h, 'Cancel'));
  await until(() => confirmText(h) === '');
  assert.equal(button(h, 'Add learning').disabled, false);
  assert.equal(button(h, 'Forget selected (1)').disabled, false);
});

test('a redraw while the add is in flight keeps the result line and the controls', async () => {
  let land;
  const inFlight = new Promise(r => { land = r; });
  state.postReply = () => inFlight;
  const { h } = await openMemories(memories, true);
  await until(() => button(h, 'Forget selected (0)'));
  click(labelled(h, 'Select trap:alpha'));
  setValue(field(h, 'add-text'), 'keep the lock');
  click(button(h, 'Add learning'));
  await until(() => confirmText(h).includes('verify: keep the lock'));
  click(button(h, 'Confirm'));
  await until(() => state.posts.length === 1);
  click(button(h, 'Read again'));
  await until(() => confirmText(h) === '' && button(h, 'Add learning') && !button(h, 'Add learning').disabled);
  assert.equal(button(h, 'Forget selected (1)').disabled, false);
  assert.equal(labelled(h, 'Edit trap:alpha').disabled, false);
  state.gets = [];
  land(added);
  await until(() => h.textContent.includes('/repo: added verify:keep the lock'));
  assert.ok(state.gets.includes('/api/projects/proj1/memories?refresh=1'));
  assert.equal(button(h, 'Add learning').disabled, false);
  assert.equal(button(h, 'Forget selected (1)').disabled, false);
  assert.equal(state.posts.length, 1);
});

test('read-only memories show commands and the person chip, and no write buttons', async () => {
  const { h } = await openMemories(memories, false);
  await until(() => labelled(h, 'Commands for trap:alpha'));
  for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
  const editAlpha = `forge memory edit /repo trap:alpha --text 'Alpha landmine'`;
  const addAlpha = `forge memory add /repo --category trap --text 'Alpha landmine'`;
  const editHand = `forge memory edit /repo 'finding:by hand' --text 'typed once'`;
  // #175: no command line sits in a Learning cell; a row's commands open on request, one row at a time
  assert.deepEqual(codes(learningBox(h, 'Alpha landmine')), []);
  assert.equal(codes(h).some(c => c.startsWith('forge memory edit')), false);
  click(labelled(h, 'Commands for trap:alpha'));
  assert.ok(codes(h).includes(editAlpha));
  assert.ok(codes(h).includes(addAlpha));
  assert.ok(labelled(h, 'Copy: ' + editAlpha));
  click(labelled(h, 'Commands for finding:by hand'));
  assert.ok(codes(h).includes(editHand));
  assert.equal(codes(h).includes(editAlpha), false, 'one row at a time');
  click(labelled(h, 'Close commands'));
  assert.equal(codes(h).some(c => c.startsWith('forge memory')), false);
  assert.equal(buttons(h).some(b => b.textContent === 'Edit' || b.textContent === 'Add learning' || b.textContent === 'Save' || b.textContent.startsWith('Forget selected')), false);
  assert.equal(labelled(h, 'Select trap:alpha'), undefined);
  assert.equal(state.posts.length, 0);

  assert.ok(learningBox(h, 'Alpha landmine').textContent.includes('added by a person'));
  assert.equal(learningBox(h, 'Beta check').textContent.includes('added by a person'), false);
  assert.ok(learningBox(h, 'typed once').textContent.includes('added by a person'));
});

test('an empty Memories tab can add the first learning, and read-only shows the command', async () => {
  state.postReply = () => added;
  const writing = await openMemories(emptyMemories, true);
  await until(() => button(writing.h, 'Add learning'));
  assert.equal(button(writing.h, 'Forget selected (0)'), undefined);
  setValue(field(writing.h, 'add-text'), 'hello');
  click(button(writing.h, 'Add learning'));
  await until(() => confirmText(writing.h).includes('Add this learning to /repo?'));
  assert.ok(confirmText(writing.h).includes('verify: hello'));
  assert.equal(state.posts.length, 0);
  click(button(writing.h, 'Confirm'));
  await until(() => state.posts.length === 1);
  assert.deepEqual(state.posts[0], {
    path: '/api/projects/proj1/memories/add',
    body: { category: 'verify', text: 'hello', path: '/repo' },
  });

  const quiet = await openMemories(emptyMemories, false);
  const command = 'forge memory add /repo --category verify --text TEXT';
  await until(() => codes(quiet.h).includes(command));
  assert.equal(button(quiet.h, 'Add learning'), undefined);
  assert.equal(buttons(quiet.h).some(b => b.textContent.startsWith('Forget selected')), false);
  assert.ok(labelled(quiet.h, 'Copy: ' + command));
  assert.equal(state.posts.length, 0);
});
