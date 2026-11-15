'use strict';
// node --test: forget on the Memories tab, compact on the Disk screen, and answering a waiting task.
// No browser. The page modules render against a fake document; post and controlsEnabled are the seams.
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
global.ForgeRunModel = require(path.join(root, 'runmodel.js'));

const state = {
  controls: false,
  gets: [],
  posts: [],
  getReply: () => ({ ok: false, status: 404, error: 'missing' }),
  postReply: () => ({ ok: false, status: 500, error: 'missing' }),
};
const U = require(path.join(root, 'uikit.js'));
const ui = U.create({ document });
ui.controlsEnabled = async () => state.controls;
ui.get = async (p) => { state.gets.push(p); return state.getReply(p); };
ui.post = async (p, body) => { state.posts.push({ path: p, body }); return state.postReply(p, body); };
global.ForgeUI = ui;
global.ForgeTable = require(path.join(root, 'datatable.js'));
const Projects = require(path.join(root, 'projectsview.js'));
const Storage = require(path.join(root, 'storageview.js'));
const Panel = require(path.join(root, 'panel.js'));
const Controls = require(path.join(root, 'controls.js'));

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
const QUEUED = 'Queued: the running scheduler resumes the task';
const PREPARED = 'Prepared: the next run dispatches the task';
const ACTIVE = 'this run is active: compact it after it ends';
const LEDE_OFF = 'What forge takes on disk in every repository it has runs for, and the command that takes each part back. Review snapshots and log copies can go without losing a run: every run stays viewable here and in the dataset. Commands are only copied: this page runs nothing.';

const learnings = [
  { category: 'trap', key: 'alpha', text: 'Alpha landmine', count: 2, last: 'today', states: { '/repo': 'in_memory' }, rows: [{ run_id: 'r', task: 't', role: 'dwarf', model: 'm', from: ['/repo'] }] },
  { category: 'verify', key: 'beta', text: 'Beta check', count: 1, last: 'today', states: { '/repo': 'in_memory' }, rows: [] },
  { category: '', key: 'skipped', text: 'no category', rows: [] },
];
const memories = {
  notes: [], runs: 1, memory_off_runs: 0, differs: false, learnings,
  sources: [{ path: '/repo', root: true, max_lines: 40, memory: [{ title: 'Traps', roles: ['dwarf'], lines: [{ text: 'keep' }] }] }],
};
function projectGet(p) {
  if (p === '/api/projects/proj1') return { ok: true, status: 200, data: { name: 'App', root: '/repo', exists: true, runs: [], checkouts: [] } };
  if (p.startsWith('/api/projects/proj1/memories')) return { ok: true, status: 200, data: memories };
  return { ok: false, status: 404, error: 'missing', detail: p };
}
const forgetPreview = {
  ok: true, status: 200,
  data: { ok: true, dry_run: true, checkouts: [{ path: '/repo', result: {
    ok: true, dry_run: true,
    keys: [{ key: 'trap:alpha', rows: 2, found: true }, { key: 'verify:beta', rows: 0, found: false }],
    memory_lines_removed: ['- <b>drop</b> this fact'],
  } }] },
};
const forgetDone = {
  ok: true, status: 200,
  data: { ok: true, dry_run: false, checkouts: [{ path: '/repo', result: {
    ok: true, dry_run: false,
    keys: [{ key: 'trap:alpha', rows: 2, found: true }, { key: 'verify:beta', rows: 0, found: false }],
    memory_lines_removed: ['- <b>drop</b> this fact'],
  } }] },
};

const disk = {
  computed: 1700000000, complete: true,
  totals: { runs: 3000, worktrees: 0, reclaimable: 1536 },
  runs: [
    { key: '0123456789ab', run_id: 'run-a', path: '/runs/a', repo: '/r/app', total: 2000, snapshots: 100, logs: 50, gz: 0, other: 0, fractal: 0, strays: 0, reclaimable: 1536, updated: 1700000000,
      commands: [{ label: 'compact', command: 'forge runs compact /runs/a --logs' }, { label: 'remove', command: 'forge runs rm /runs/a --dry-run' }] },
    { key: null, run_id: 'run-b', path: '/runs/b', repo: '/r/app', total: 1000, snapshots: 0, logs: 10, gz: 0, other: 0, fractal: 0, strays: 0, reclaimable: 100, updated: 1700000000,
      commands: [{ label: 'compact', command: 'forge runs compact /runs/b --logs' }] },
  ],
  commands: [{ label: 'all', command: 'forge runs compact --all --logs' }, { label: 'du', command: 'forge du --all' }],
  worktrees: [], strays: [],
};
function storageGet(p) {
  if (p.startsWith('/api/storage')) return { ok: true, status: 200, data: { state: 'ready', data: disk } };
  return { ok: false, status: 404, error: 'missing', detail: p };
}
function compactAnswer(dry, bytes, n) {
  return { ok: true, status: 200, data: { ok: true, dry_run: dry, bytes_freed: bytes, runs: Array.from({ length: n }, () => ({ id: 'a' })) } };
}
function commandBox(node, command) {
  const code = walk(node).find(n => n.tagName === 'CODE' && n.textContent === command);
  return code ? code.parentNode : null;
}
function commandButtons(node, command) {
  const box = commandBox(node, command);
  assert.ok(box, command);
  return buttons(box).map(b => b.textContent);
}

function waitingTask() {
  return {
    id: 'reader', status: 'NEEDS_INPUT', title: 'Read the table', needs_input: 'Which table?',
    header: { title: 'Read the table', criteria: 'It answers.' },
    trail: [], executions: [], attempts: [], deps: [], files: [], route: [], escalations: [], lateral: [], wave: 1,
  };
}
function panelCtx(run) {
  Panel.cache.clear();
  return {
    run, sel: { kind: 'task', id: 'reader' }, now: 0,
    hash: () => '#', onSelect() {}, rerender() {},
    fetchJSON: () => new Promise(() => {}),
  };
}

test('forgetKeys keeps the selected learnings, in learning order', () => {
  const rows = learnings.concat([{ category: 'finding', key: 'gamma', text: 'later' }]);
  assert.deepEqual(Projects.forgetKeys(rows, new Set(['verify:beta', 'trap:alpha'])), ['trap:alpha', 'verify:beta']);
  assert.deepEqual(Projects.forgetKeys(rows, ['finding:gamma', 'trap:missing']), ['finding:gamma']);
  assert.deepEqual(Projects.forgetKeys(rows, [rows[0]]), ['trap:alpha']);
  assert.deepEqual(Projects.forgetKeys(null, ['trap:alpha']), []);
  assert.deepEqual(Projects.forgetKeys(rows, ['skipped', ':skipped']), []);
});

test('compactBody names one run or every run', () => {
  assert.deepEqual(Storage.compactBody('0123456789ab'), { run: '0123456789ab' });
  assert.deepEqual(Storage.compactBody({ key: '0123456789ab' }), { run: '0123456789ab' });
  assert.deepEqual(Storage.compactBody({ run: 'abcdefabcdef' }), { run: 'abcdefabcdef' });
  assert.deepEqual(Storage.compactBody({ all: true }), { all: true });
  assert.deepEqual(Storage.compactBody('all'), { all: true });
});

test('compactSummary says what a dry run would free', () => {
  assert.equal(Storage.compactSummary({ dry_run: true, bytes_freed: 1536, runs: [{}, {}] }), 'Would free 1.5 KB across 2 runs');
  assert.equal(Storage.compactSummary({ dry_run: true, bytes_freed: 0, runs: [{}] }), 'Would free 0 B across 1 run');
  assert.equal(Storage.compactSummary({ dry_run: false, bytes_freed: 2048, runs: [] }), 'Freed 2.0 KB across 0 runs');
});

test('answerOutcome reads live, and an error the way every other write does', () => {
  assert.equal(Panel.answerOutcome({ ok: true, status: 200, data: { live: true, result: {} } }), QUEUED);
  assert.equal(Panel.answerOutcome({ ok: true, status: 200, data: { live: false, result: {} } }), PREPARED);
  assert.equal(Panel.answerOutcome({ ok: false, status: 409, error: 'busy', detail: 'locked' }), 'busy, try again');
  assert.equal(Panel.answerOutcome({ ok: false, status: 409, error: 'refused', detail: 'task reader is not NEEDS_INPUT', data: { live: true } }), 'task reader is not NEEDS_INPUT');
});

test.describe('forget, compact and answer on the page', { concurrency: 1 }, () => {
  test.beforeEach(() => {
    state.controls = false;
    state.gets = [];
    state.posts = [];
    state.getReply = () => ({ ok: false, status: 404, error: 'missing' });
    state.postReply = () => ({ ok: false, status: 500, error: 'missing' });
    document.activeElement = null;
  });

  test('forgetting previews, confirms, then forgets, and a cancel stops there', async () => {
    state.controls = true;
    state.getReply = projectGet;
    const replies = [forgetPreview, forgetPreview, forgetDone];
    state.postReply = () => replies.shift();
    const h = mount();
    Projects.render(h, { id: 'proj1', tab: 'memories', runHref: k => '#/runs/' + k });
    await until(() => button(h, 'Forget selected (0)'));
    assert.equal(click(button(h, 'Forget selected (0)')), false);
    assert.equal(state.posts.length, 0);

    const search = walk(h).find(n => n.dataset && n.dataset.filter === 'm-text');
    search.value = 'Beta';
    for (const fn of search.listeners.input) fn({});
    click(labelled(h, 'Select all shown learnings'));
    click(button(h, 'Forget selected (1)'));
    await until(() => confirmText(h).includes('verify:beta — Beta check'));
    assert.equal(confirmText(h).includes('trap:alpha'), false);
    assert.deepEqual(state.posts.map(p => p.body.keys), [['verify:beta']]);
    click(button(h, 'Cancel'));
    for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
    state.posts = [];
    search.value = '';
    for (const fn of search.listeners.input) fn({});

    click(labelled(h, 'Select all shown learnings'));
    await until(() => button(h, 'Forget selected (2)'));
    click(button(h, 'Forget selected (2)'));
    await until(() => confirmText(h).includes('trap:alpha — Alpha landmine'));
    const asked = confirmText(h);
    assert.match(asked, /verify:beta — Beta check/);
    assert.match(asked, /memory\.md lines that go:/);
    assert.match(asked, /- <b>drop<\/b> this fact/);
    assert.equal(walk(h).some(n => n.tagName === 'B' || n.tagName === 'IMG'), false);
    assert.deepEqual(state.posts.map(p => p.body), [
      { keys: ['trap:alpha', 'verify:beta'], dry_run: true, path: '/repo' },
    ]);
    click(button(h, 'Confirm'));
    await until(() => h.textContent.includes('trap:alpha: 2 rows removed') && h.textContent.includes('verify:beta: not found'));
    assert.deepEqual(state.posts.map(p => [p.path, p.body]), [
      ['/api/projects/proj1/memories/forget', { keys: ['trap:alpha', 'verify:beta'], dry_run: true, path: '/repo' }],
      ['/api/projects/proj1/memories/forget', { keys: ['trap:alpha', 'verify:beta'], dry_run: false, path: '/repo' }],
    ]);
    assert.ok(state.gets.includes('/api/projects/proj1/memories?refresh=1'));

    state.posts = [];
    state.gets = [];
    replies.push(forgetPreview);
    click(labelled(h, 'Select trap:alpha'));
    click(button(h, 'Forget selected (1)'));
    await until(() => confirmText(h).includes('trap:alpha — Alpha landmine'));
    click(button(h, 'Cancel'));
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(confirmText(h), '');
    assert.deepEqual(state.posts.map(p => p.body.dry_run), [true]);
    assert.equal(state.gets.some(p => p.includes('refresh=1')), false);
  });

  test('read-only memories keep the table and add no forget control', async () => {
    state.getReply = projectGet;
    const h = mount();
    Projects.render(h, { id: 'proj1', tab: 'memories', runHref: k => '#/runs/' + k });
    await until(() => h.textContent.includes('Alpha landmine'));
    for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(labelled(h, 'Select all shown learnings'), undefined);
    assert.equal(labelled(h, 'Select trap:alpha'), undefined);
    assert.equal(buttons(h).some(b => b.textContent.startsWith('Forget selected')), false);
    assert.equal(state.posts.length, 0);
  });

  test('compacting previews, confirms, then compacts, and a cancel or a 409 does not', async () => {
    state.controls = true;
    state.getReply = storageGet;
    const replies = [compactAnswer(true, 1536, 2), compactAnswer(false, 2048, 1)];
    state.postReply = () => replies.shift();
    const h = mount();
    Storage.render(h, { runHref: k => '#/runs/' + k });
    await until(() => {
      const box = commandBox(h, 'forge runs compact /runs/a --logs');
      return box && buttons(box).some(b => b.textContent === 'Compact');
    });
    assert.deepEqual(commandButtons(h, 'forge runs compact /runs/a --logs'), ['Copy', 'Compact']);
    assert.deepEqual(commandButtons(h, 'forge runs rm /runs/a --dry-run'), ['Copy']);
    assert.deepEqual(commandButtons(h, 'forge runs compact /runs/b --logs'), ['Copy']);
    assert.deepEqual(commandButtons(h, 'forge runs compact --all --logs'), ['Copy', 'Compact all']);
    assert.deepEqual(commandButtons(h, 'forge du --all'), ['Copy']);
    const lede = walk(h).find(n => String(n.className || '').split(/\s+/).includes('wt-lede'));
    assert.equal(lede.textContent.includes('this page runs nothing'), false);

    click(button(h, 'Compact'));
    await until(() => confirmText(h) === 'Would free 1.5 KB across 2 runs');
    assert.deepEqual(state.posts.map(p => p.body), [{ run: '0123456789ab', dry_run: true }]);
    click(button(h, 'Confirm'));
    await until(() => h.textContent.includes('Freed 2.0 KB across 1 run'));
    assert.deepEqual(state.posts.map(p => [p.path, p.body]), [
      ['/api/storage/compact', { run: '0123456789ab', dry_run: true }],
      ['/api/storage/compact', { run: '0123456789ab', dry_run: false }],
    ]);
    assert.ok(state.gets.includes('/api/storage?refresh=1'));

    state.posts = [];
    state.gets = [];
    replies.push(compactAnswer(true, 1536, 2));
    click(button(h, 'Compact all'));
    await until(() => confirmText(h) === 'Would free 1.5 KB across 2 runs');
    click(button(h, 'Cancel'));
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(confirmText(h), '');
    assert.deepEqual(state.posts.map(p => p.body), [{ all: true, dry_run: true }]);
    assert.equal(state.gets.some(p => p.includes('refresh=1')), false);

    state.posts = [];
    state.postReply = () => ({ ok: false, status: 409, error: 'refused', detail: 'run is active' });
    click(button(h, 'Compact'));
    await until(() => h.textContent.includes(ACTIVE));
    assert.equal(button(h, 'Confirm'), undefined);
    assert.equal(state.posts.length, 1);
    assert.equal(state.posts[0].body.dry_run, true);

    state.posts = [];
    state.postReply = () => ({ ok: false, status: 409, error: 'busy', detail: 'locked' });
    click(button(h, 'Compact all'));
    await until(() => h.textContent.includes('busy, try again'));
    assert.equal(h.textContent.includes(ACTIVE), false);
    assert.equal(button(h, 'Confirm'), undefined);
    assert.equal(state.posts.length, 1);
  });

  test('read-only disk keeps every copy and the original lede', async () => {
    state.getReply = storageGet;
    const h = mount();
    Storage.render(h, { runHref: k => '#/runs/' + k });
    await until(() => walk(h).some(n => n.tagName === 'CODE' && n.textContent === 'forge du --all'));
    for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(button(h, 'Compact'), undefined);
    assert.equal(button(h, 'Compact all'), undefined);
    for (const command of ['forge runs compact /runs/a --logs', 'forge runs rm /runs/a --dry-run', 'forge runs compact /runs/b --logs', 'forge runs compact --all --logs', 'forge du --all'])
      assert.deepEqual(commandButtons(h, command), ['Copy']);
    const lede = walk(h).find(n => String(n.className || '').split(/\s+/).includes('wt-lede'));
    assert.equal(lede.textContent, LEDE_OFF);
    assert.equal(state.posts.length, 0);
  });

  test('a plan can send an answer, an empty one is refused, and read-only keeps the copy', async () => {
    const task = waitingTask();
    const run = { kind: 'plan', key: 'bbbbbbbbbbbb', path: '/tmp/plans/b', tasks: [task], settings: {}, updated: 1 };
    state.controls = true;
    const h = mount();
    Panel.render(h, panelCtx(run));
    await until(() => button(h, 'Send answer') && button(h, 'Copy command'));
    const field = walk(h).find(n => n.tagName === 'TEXTAREA');
    assert.equal(field.getAttribute('aria-label'), 'Answer');
    field.value = '  \n';
    click(button(h, 'Send answer'));
    await until(() => h.textContent.includes('the answer is empty'));
    assert.equal(state.posts.length, 0);

    state.postReply = () => ({ ok: true, status: 200, data: { live: true, result: {} } });
    field.value = 'use the new table';
    click(button(h, 'Send answer'));
    await until(() => h.textContent.includes(QUEUED));
    assert.deepEqual(state.posts, [{ path: '/api/runs/bbbbbbbbbbbb/tasks/reader/answer', body: { text: 'use the new table' } }]);

    state.postReply = () => ({ ok: true, status: 200, data: { live: false, result: {} } });
    field.value = 'later';
    click(button(h, 'Send answer'));
    await until(() => h.textContent.includes(PREPARED));
    assert.equal(h.textContent.includes(QUEUED), false);
    assert.deepEqual(state.posts.at(-1), { path: '/api/runs/bbbbbbbbbbbb/tasks/reader/answer', body: { text: 'later' } });

    state.posts = [];
    state.postReply = () => ({ ok: false, status: 409, error: 'busy', detail: 'locked' });
    field.value = 'again';
    click(button(h, 'Send answer'));
    await until(() => h.textContent.includes('busy, try again'));
    assert.equal(state.posts.length, 1);
    assert.equal(h.textContent.includes(QUEUED), false);

    state.controls = false;
    state.posts = [];
    const quiet = mount();
    Panel.render(quiet, panelCtx(run));
    await until(() => button(quiet, 'Copy command'));
    for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(walk(quiet).some(n => n.tagName === 'TEXTAREA'), false);
    assert.equal(button(quiet, 'Send answer'), undefined);
    assert.equal(state.posts.length, 0);

    state.controls = true;
    const soloHost = mount();
    const solo = waitingTask();
    Panel.render(soloHost, panelCtx({ kind: 'solo', key: 'aaaaaaaaaaaa', path: '/tmp/solo', tasks: [solo], settings: {}, updated: 1 }));
    await until(() => soloHost.textContent.includes('Which table?'));
    for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(walk(soloHost).some(n => n.tagName === 'TEXTAREA'), false);
    assert.equal(button(soloHost, 'Send answer'), undefined);
    assert.equal(button(soloHost, 'Copy command'), undefined);
  });

  test('a redraw while forget is confirming cancels it and leaves forget usable', async () => {
    state.controls = true;
    state.getReply = projectGet;
    state.postReply = () => forgetPreview;
    const h = mount();
    const view = Projects.render(h, { id: 'proj1', tab: 'memories', runHref: k => '#/runs/' + k });
    await until(() => button(h, 'Forget selected (0)'));
    click(labelled(h, 'Select trap:alpha'));
    click(button(h, 'Forget selected (1)'));
    await until(() => confirmText(h).includes('trap:alpha — Alpha landmine'));
    assert.equal(button(h, 'Forget selected (1)').disabled, true);
    assert.equal(labelled(h, 'Select all shown learnings').disabled, true);
    assert.equal(state.posts.length, 1);

    click(button(h, 'Read again'));
    await until(() => confirmText(h) === '' && button(h, 'Forget selected (1)'));
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0));
    assert.deepEqual(state.posts.map(p => p.body.dry_run), [true]);
    assert.equal(button(h, 'Forget selected (1)').disabled, false);
    assert.equal(labelled(h, 'Select trap:alpha').disabled, false);
    assert.equal(labelled(h, 'Select all shown learnings').disabled, false);

    view.go('proj1', 'runs');
    state.posts = [];
    view.go('proj1', 'memories');
    await until(() => button(h, 'Forget selected (1)'));
    click(button(h, 'Forget selected (1)'));
    await until(() => confirmText(h).includes('trap:alpha — Alpha landmine'));
    view.go('proj1', 'findings');
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(state.posts.length, 1);
    assert.equal(state.posts[0].body.dry_run, true);
    view.go('proj1', 'memories');
    await until(() => button(h, 'Forget selected (1)'));
    assert.equal(button(h, 'Forget selected (1)').disabled, false);
    assert.equal(labelled(h, 'Select trap:alpha').disabled, false);
  });

  test('a redraw while the confirmed forget is in flight keeps its result line', async () => {
    state.controls = true;
    state.getReply = projectGet;
    let land;
    const inFlight = new Promise(r => { land = r; });
    const replies = [forgetPreview, inFlight];
    state.postReply = () => replies.shift();
    const h = mount();
    Projects.render(h, { id: 'proj1', tab: 'memories', runHref: k => '#/runs/' + k });
    await until(() => button(h, 'Forget selected (0)'));
    click(labelled(h, 'Select trap:alpha'));
    click(button(h, 'Forget selected (1)'));
    await until(() => confirmText(h).includes('trap:alpha — Alpha landmine'));
    click(button(h, 'Confirm'));
    await until(() => state.posts.length === 2);
    click(button(h, 'Read again'));
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0));
    state.gets = [];
    land(forgetDone);
    await until(() => h.textContent.includes('trap:alpha: 2 rows removed'));
    assert.ok(state.gets.includes('/api/projects/proj1/memories?refresh=1'));
    assert.equal(button(h, 'Forget selected (0)').disabled, true);    // the forgotten key is no longer picked
  });

  test('changing checkout while forget is confirming does not stick the controls', async () => {
    state.controls = true;
    state.getReply = p => {
      if (p.startsWith('/api/projects/proj1/memories')) return { ok: true, status: 200, data: {
        ...memories,
        sources: [memories.sources[0], { path: '/other', root: false, branch: 'side', max_lines: 40, memory: [] }],
      } };
      return projectGet(p);
    };
    state.postReply = () => forgetPreview;
    const h = mount();
    Projects.render(h, { id: 'proj1', tab: 'memories', runHref: k => '#/runs/' + k });
    await until(() => walk(h).find(n => n.dataset && n.dataset.filter === 'source'));
    click(labelled(h, 'Select trap:alpha'));
    click(button(h, 'Forget selected (1)'));
    await until(() => confirmText(h).includes('trap:alpha — Alpha landmine'));
    const sel = walk(h).find(n => n.dataset && n.dataset.filter === 'source');
    sel.value = '/other';
    for (const fn of sel.listeners.change) fn({});
    assert.equal(confirmText(h), '');
    assert.equal(labelled(h, 'Select trap:alpha').disabled, false);
    assert.equal(labelled(h, 'Select all shown learnings').disabled, false);
    for (let i = 0; i < 8; i++) await new Promise(r => setTimeout(r, 0));
    assert.deepEqual(state.posts.map(p => p.body.dry_run), [true]);

    state.posts = [];
    click(labelled(h, 'Select trap:alpha'));
    click(button(h, 'Forget selected (1)'));
    await until(() => confirmText(h).includes('trap:alpha — Alpha landmine'));
    assert.deepEqual(state.posts.map(p => p.body), [{ keys: ['trap:alpha'], dry_run: true, path: '/other' }]);
    click(button(h, 'Cancel'));
    for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(state.posts.length, 1);
    assert.equal(button(h, 'Forget selected (1)').disabled, false);
  });

  test('a typed answer survives the inspector being drawn again', async () => {
    state.controls = true;
    const h = mount();
    const run = { kind: 'plan', key: 'bbbbbbbbbbbb', path: '/tmp/plans/b', tasks: [waitingTask()], settings: {}, updated: 1 };
    Panel.render(h, panelCtx(run));
    const field = walk(h).find(n => n.tagName === 'TEXTAREA');
    assert.ok(field, 'the answer box is in the tree before controlsEnabled resolves');
    assert.equal(field.parentNode.dataset.hold, 'answer:bbbbbbbbbbbb:reader');
    field.value = '   ';
    for (const fn of field.listeners.input) fn({});
    for (const fn of field.listeners.blur) fn({});
    assert.equal(field.parentNode.dataset.editing, undefined);
    field.value = 'keep this answer';
    field.focus();
    for (const fn of field.listeners.focus) fn({});
    for (const fn of field.listeners.input) fn({});
    assert.equal(field.parentNode.dataset.editing, '1');
    const held = Controls.hold(h);
    assert.ok(held, 'the draft is held across the rebuild');
    Panel.render(h, panelCtx(run));
    const blank = walk(h).find(n => n.tagName === 'TEXTAREA');
    assert.notEqual(blank, field);
    assert.equal(blank.value, '');
    held.restore();
    const again = walk(h).find(n => n.tagName === 'TEXTAREA');
    assert.equal(again, field);
    assert.equal(again.value, 'keep this answer');
    assert.equal(document.activeElement, field);
    for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0));
    assert.equal(walk(h).find(n => n.tagName === 'TEXTAREA').value, 'keep this answer');
  });
});
