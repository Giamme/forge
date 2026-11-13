'use strict';
// node --test: which open brief question comes first (briefview.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const B = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'briefview.js'));

const now = 1000000;

function row(id, must, wave) {
  return { task: { id, wave }, question: { id: 'q1', must_answer: must } };
}

test('flagged questions come first, then the earliest projected start, and a task with no projection last', () => {
  // Input order, and a wave number that would win if the sort used waves. It must not.
  const items = [
    row('page', false, 1),
    row('server', false, 2),
    row('docs', true, 4),
    row('api', true, 9),
    row('graph', false, 0),
    row('reader', false, 3),
  ];
  const projections = {
    page: { start: now + 5000 },
    server: { start: now + 2000 },
    docs: { start: now + 9000 },
    api: { start: now + 3000 },
    reader: { start: null, blocked: true },
  };
  assert.deepEqual(B.orderQuestions(items, projections).map(item => item.task.id),
    ['api', 'docs', 'server', 'page', 'graph', 'reader']);
});

test('a flagged question with no projection still comes before one that starts sooner', () => {
  const items = [
    { task: { id: 'soon', wave: 1 }, question: { must_answer: false } },
    { task: { id: 'flagged', wave: 9 }, question: { must_answer: true } },
  ];
  assert.deepEqual(B.orderQuestions(items, { soon: { start: 10 } }).map(item => item.task.id), ['flagged', 'soon']);
});

test('a projected start of zero is a start, and a blocked start is not', () => {
  const items = [
    { task: { id: 'late' }, question: { must_answer: false } },
    { task: { id: 'zero' }, question: { must_answer: false } },
    { task: { id: 'blocked' }, question: { must_answer: false } },
  ];
  const projections = { late: { start: 50 }, zero: { start: 0 }, blocked: { start: null, blocked: true } };
  assert.deepEqual(B.orderQuestions(items, projections).map(item => item.task.id), ['zero', 'late', 'blocked']);
});

test('a question says when its task starts, from the projection and the deps still to merge', () => {
  const cases = [
    [{ deps: ['A', 'B'], start: now + 40 * 60, now }, 'starts after A and B merge, about 40 min'],
    [{ deps: ['A', 'B', 'C'], start: now + 40 * 60, now }, 'starts after A, B and C merge, about 40 min'],
    [{ deps: ['graph'], start: now + 90, now }, 'starts after graph merges, about 2 min'],
    [{ deps: ['A'], start: now + 60, now }, 'starts after A merges, about 1 min'],
    [{ deps: [], start: now + 40 * 60, now }, 'starts in about 40 min'],
    [{ deps: [], start: now + 60 * 60, now }, 'starts in about 1 h'],
    [{ deps: [], start: now + 90 * 60, now }, 'starts in about 1 h 30 min'],
    [{ deps: ['A', 'B'], start: now + 10, now }, 'starts after A and B merge, about now'],
    [{ deps: [], start: now + 10, now }, 'starts about now'],
    [{ deps: [], start: now + 44, now }, 'starts about now'],
    [{ deps: [], start: now - 500, now }, 'starts about now'],
    [{ deps: ['A', 'B'], start: now + 40 * 60 }, 'starts after A and B merge, about now'],
    [{ deps: ['A'], start: null, now }, null],
    [{ start: null }, null],
    // started tasks: the projection is in the past, so it is not "starts about now"
    [{ deps: [], start: now - 500, now, status: 'RUNNING' }, 'running now'],
    [{ deps: ['A'], start: now - 500, now, status: 'FAIL' }, 'already started'],
    [{ deps: [], start: now + 10, now, status: 'PENDING' }, 'starts about now'],
    [{ deps: ['A'], start: now + 60, now, status: 'BLOCKED' }, 'starts after A merges, about 1 min'],
  ];
  for (const [spec, line] of cases) assert.equal(B.startLine(spec), line, JSON.stringify(spec));
});

test('questions sit under their group, then unknown groups, then Plan, then loose', () => {
  const groups = [
    { id: 'ship', title: 'Ship the reader', goal: 'Land it together' },
    { id: 'quiet', title: 'Quiet', goal: 'unused' },
    { id: 'docs', title: '', goal: '' },
  ];
  const task = { id: 'reader', group: 'ship' };
  const item = (id, question) => ({ task, question: { id, ...question } });
  const sections = B.groupQuestions([
    item('q1', { scope: 'wave', group: 'ship' }),
    item('q2', { scope: 'wave', group: '' }),
    item('q3', { scope: 'plan', group: 'ship' }),
    item('q4', { group: 'docs' }),
    item('q5', { scope: 'wave', group: 'other' }),
    item('q6', {}),
    item('q7', { scope: 'plan' }),
    item('q8', { scope: 'wave', group: 'ship' }),
    item('q9', { scope: 'wave', group: null }),
  ], groups);
  assert.deepEqual(sections.map(section => ({
    kind: section.kind, id: section.id, title: section.title, goal: section.goal,
    ids: section.items.map(row => row.question.id),
  })), [
    { kind: 'group', id: 'ship', title: 'Ship the reader', goal: 'Land it together', ids: ['q1', 'q6', 'q8'] },
    { kind: 'group', id: 'docs', title: 'docs', goal: '', ids: ['q4'] },
    { kind: 'group', id: 'other', title: 'other', goal: '', ids: ['q5'] },
    { kind: 'plan', id: null, title: 'Plan', goal: '', ids: ['q3', 'q7'] },
    { kind: 'loose', id: null, title: '', goal: '', ids: ['q2', 'q9'] },
  ]);
});

test('a propose button is only for an answered wave or plan question', () => {
  const cases = [
    [{ scope: 'wave', answer: 'yes' }, true],
    [{ scope: 'plan', answer: 'later' }, true],
    [{ scope: 'wave', answer: '' }, false],
    [{ scope: 'plan' }, false],
    [{ answer: 'local' }, false],
    [{ scope: 'task', answer: 'yes' }, false],
    [null, false],
  ];
  for (const [question, allowed] of cases) assert.equal(B.canPropose(question), allowed, JSON.stringify(question));
});

function installDocument() {
  function descendants(node) {
    const out = [];
    for (const child of node.children || []) {
      out.push(child);
      out.push(...descendants(child));
    }
    return out;
  }
  function matches(node, part) {
    const found = String(part).match(/^([a-zA-Z0-9]*)((?:\.[a-zA-Z0-9_-]+)*)$/);
    if (!found || (!found[1] && !found[2])) return false;
    if (found[1] && node.tagName !== found[1].toUpperCase()) return false;
    if (found[2]) {
      const classes = String(node.className || '').split(/\s+/);
      for (const name of found[2].slice(1).split('.')) {
        if (!classes.includes(name)) return false;
      }
    }
    return true;
  }
  function queryCompound(root, selector) {
    const tokens = [];
    const combinators = [];
    const re = /\s*>\s*|\s+/g;
    let last = 0;
    for (let found = re.exec(selector); found; found = re.exec(selector)) {
      tokens.push(selector.slice(last, found.index).trim());
      combinators.push(found[0].includes('>') ? '>' : ' ');
      last = found.index + found[0].length;
    }
    tokens.push(selector.slice(last).trim());
    let index = 0;
    let current = [root];
    if (tokens[0] !== ':scope') current = descendants(root).filter(node => matches(node, tokens[0]));
    index = 1;
    for (; index < tokens.length; index++) {
      const token = tokens[index];
      const comb = combinators[index - 1];
      const next = [];
      for (const base of current) {
        const pool = comb === '>' ? base.children : descendants(base);
        for (const candidate of pool) if (matches(candidate, token)) next.push(candidate);
      }
      current = next;
    }
    return current;
  }
  function queryAll(node, selector) {
    const out = [];
    const seen = new Set();
    for (const part of String(selector).split(',')) {
      const trimmed = part.trim();
      if (!trimmed) continue;
      for (const hit of queryCompound(node, trimmed)) {
        if (seen.has(hit)) continue;
        seen.add(hit);
        out.push(hit);
      }
    }
    return out;
  }
  function Element(tag) {
    this.tagName = String(tag).toUpperCase();
    this.className = '';
    this.children = [];
    this.parentElement = null;
    this.isConnected = true;
    this.dataset = {};
    this._attrs = {};
    this._on = {};
    this._text = '';
    this.value = '';
    this.disabled = false;
    this.type = '';
    this.title = '';
    this.placeholder = '';
    this.href = '';
    this.id = '';
  }
  Object.defineProperty(Element.prototype, 'textContent', {
    get() { return this._text + this.children.map(child => child.textContent).join(''); },
    set(value) {
      for (const child of this.children) child.parentElement = null;
      this._text = value == null ? '' : String(value);
      this.children = [];
    },
  });
  Object.defineProperty(Element.prototype, 'childElementCount', { get() { return this.children.length; } });
  Object.defineProperty(Element.prototype, 'innerHTML', {
    get() { return undefined; },
    set() { throw new Error('innerHTML is not allowed'); },
  });
  Element.prototype.append = function (...nodes) {
    for (const node of nodes) {
      if (!node) continue;
      if (node.parentElement) node.remove();
      node.parentElement = this;
      node.isConnected = true;
      this.children.push(node);
    }
  };
  Element.prototype.replaceChildren = function (...nodes) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this._text = '';
    this.append(...nodes);
  };
  Element.prototype.remove = function () {
    const parent = this.parentElement;
    if (parent) parent.children = parent.children.filter(child => child !== this);
    this.parentElement = null;
    this.isConnected = false;
  };
  Element.prototype.addEventListener = function (type, fn) {
    (this._on[type] || (this._on[type] = [])).push(fn);
  };
  Element.prototype.click = function () {
    const fns = this._on.click || [];
    return fns[0] && fns[0].call(this);
  };
  Element.prototype.setAttribute = function (name, value) { this._attrs[name] = String(value); };
  Element.prototype.getAttribute = function (name) {
    return Object.prototype.hasOwnProperty.call(this._attrs, name) ? this._attrs[name] : null;
  };
  Element.prototype.removeAttribute = function (name) { delete this._attrs[name]; };
  Element.prototype.querySelectorAll = function (selector) { return queryAll(this, selector); };
  Element.prototype.querySelector = function (selector) { return queryAll(this, selector)[0] || null; };
  globalThis.document = { createElement(tag) { return new Element(tag); } };
}

installDocument();

function arm(post) {
  const posts = [];
  globalThis.ForgeUI = {
    controlsEnabled: () => Promise.resolve(true),
    post(path, body) {
      posts.push({ path, body });
      return post ? post(path, body) : Promise.resolve({ ok: false, status: 400, data: {} });
    },
    outcome() {},
  };
  return posts;
}

function card(root, qid) {
  return root.querySelectorAll('li.brief-q').find(node => node.dataset.qid === qid);
}

test('an affected task is badged, and one that already started says so', () => {
  assert.equal(B.affectedLabel({ affected: { pid: 'p3', flag_only: false } }), 'affected by p3');
  assert.equal(B.affectedLabel({ affected: { pid: 'p3', reason: 'started', flag_only: true } }), 'flagged: already started');
  assert.equal(B.affectedLabel({ affected: { pid: '', flag_only: false } }), null);
  assert.equal(B.affectedLabel({}), null);
  assert.equal(B.affectedLabel({ affected: null }), null);
  const badge = B.affectedBadge({ affected: { pid: 'p3', reason: 'dependent', flag_only: false } });
  assert.equal(badge.textContent, 'affected by p3');
  assert.equal(badge.title, 'dependent');
  assert.ok(String(badge.className).split(/\s+/).includes('brief-affected'));
  const flagged = B.affectedBadge({ affected: { pid: 'p1', reason: 'went', flag_only: true } });
  assert.equal(flagged.textContent, 'flagged: already started');
  assert.equal(flagged.title, 'went');
  assert.equal(B.affectedBadge({}), null);
  assert.equal(B.affectedBadge({ affected: { flag_only: false } }), null);
});

test('a task question stays a direct list, and an unanswered parked plan question can be answered', () => {
  arm();
  const host = document.createElement('div');
  const section = B.taskSection(host, { run: { key: 'demo', groups: [] } }, {
    id: 'reader',
    brief: {
      questions: [{ id: 'q1', state: 'open', text: 'Which reader?' }],
      parked: [{ id: 'q4', text: 'For the <b>whole</b> plan?', scope: 'plan', state: 'open' }],
    },
  });
  assert.equal(section.querySelectorAll(':scope > .brief-questions').length, 1);
  const parked = section.querySelector('.brief-parked');
  assert.equal(parked.querySelector('summary').textContent, 'Parked (wider scope)');
  assert.equal(parked.querySelector('h4').textContent, 'Plan');
  assert.equal(parked.querySelector('.brief-goal'), null);
  assert.equal(parked.querySelectorAll('.brief-box').length, 1);
  assert.equal(parked.querySelectorAll('.brief-propose').length, 0);
  assert.equal(parked.textContent.includes('For the <b>whole</b> plan?'), true);
  assert.equal(parked.querySelector('b'), null);
  assert.equal(section.querySelectorAll(':scope > .brief-questions .brief-box').length, 1);
});

test('Propose a change is only on an answered wave or plan question and posts that question', async () => {
  let refreshed = 0;
  const posts = arm(() => Promise.resolve({ ok: true, status: 200, data: { exit: 0, output: 'p7  proposed  scope wave\n' } }));
  const host = document.createElement('div');
  const run = {
    key: 'demo',
    groups: [{ id: 'ship', title: 'Ship the reader', goal: 'Land it together' }],
  };
  const section = B.taskSection(host, { run, refresh: () => { refreshed += 1; } }, {
    id: 'reader',
    group: 'ship',
    brief: {
      questions: [
        { id: 'q1', state: 'answered', text: 'wave?', scope: 'wave', group: 'ship', answer: 'yes' },
        { id: 'q2', state: 'answered', text: 'plan?', scope: 'plan', answer: 'later' },
        { id: 'q3', state: 'answered', text: 'task?', answer: 'local' },
        { id: 'q4', state: 'open', text: 'still open', scope: 'wave', group: 'ship' },
        { id: 'q5', state: 'answered', text: 'blank', scope: 'wave', group: 'ship', answer: '' },
      ],
    },
  });
  assert.equal(section.querySelector('.brief-goal').textContent, 'Land it together');
  assert.equal(section.textContent.includes('(no goal)'), false);
  for (const qid of ['q1', 'q2']) {
    const button = card(section, qid).querySelector('button.brief-propose');
    assert.equal(button.textContent, 'Propose a change');
  }
  for (const qid of ['q3', 'q4', 'q5']) assert.equal(card(section, qid).querySelector('.brief-propose'), null);
  await card(section, 'q1').querySelector('button.brief-propose').click();
  assert.deepEqual(posts, [{ path: '/api/runs/demo/proposals', body: { task: 'reader', qid: 'q1' } }]);
  assert.equal(refreshed, 1);
});

test('open questions with no group stay a flat list, flagged first', () => {
  arm();
  const host = document.createElement('div');
  B.renderList(host, {
    run: {
      key: 'flat',
      tasks: [
        { id: 'page', status: 'PENDING', brief: { questions: [{ id: 'q1', state: 'open', text: 'later', must_answer: false }] } },
        { id: 'docs', status: 'PENDING', brief: { questions: [{ id: 'q2', state: 'open', text: 'first', must_answer: true }] } },
      ],
    },
    projections: {},
    now,
    href: id => '#/tasks/' + id,
  });
  assert.equal(host.querySelector('h4'), null);
  assert.deepEqual(host.querySelectorAll('a.brief-open').map(link => link.querySelector('.brief-open-task').textContent), ['docs', 'page']);
  assert.equal(host.dataset.proposals, undefined);
});

test('the question list shows a group title and goal, then unknown groups, then loose questions', () => {
  arm();
  const host = document.createElement('div');
  B.renderList(host, {
    run: {
      key: 'grouped',
      groups: [
        { id: 'ship', title: 'Ship the reader', goal: 'Land it together' },
        { id: 'quiet', title: 'Quiet', goal: 'unused' },
      ],
      tasks: [
        { id: 'page', group: 'ship', status: 'PENDING', brief: { questions: [{ id: 'q1', state: 'open', text: 'alpha' }] } },
        { id: 'misc', group: 'zz', status: 'PENDING', brief: { questions: [{ id: 'q2', state: 'open', text: 'beta' }] } },
        { id: 'loose', status: 'PENDING', brief: { questions: [{ id: 'q3', state: 'open', text: 'gamma' }] } },
      ],
    },
    projections: {},
    now,
  });
  assert.deepEqual(host.querySelectorAll('h4').map(node => node.textContent), ['Ship the reader', 'zz']);
  assert.equal(host.querySelector('.brief-goal').textContent, 'Land it together');
  assert.equal(host.textContent.includes('(no goal)'), false);
  assert.equal(host.textContent.includes('unused'), false);
  assert.deepEqual(host.querySelectorAll('a.brief-open .brief-open-task').map(node => node.textContent), ['page', 'misc', 'loose']);
});

test('a proposal shows its state, and only a proposed one can be approved with the shown hash or rejected', async () => {
  const hash = 'ab'.repeat(32);
  const seen = [];
  globalThis.ForgeDiff = {
    renderPatch(box, text, opts) {
      seen.push({ text, truncated: opts && opts.truncated });
      const pre = document.createElement('pre');
      pre.className = 'diff-stub';
      pre.textContent = text;
      box.append(pre);
    },
  };
  const posts = arm(() => Promise.resolve({ ok: false, status: 409, data: { output: 'no\n' } }));
  const host = document.createElement('div');
  let refreshed = 0;
  B.renderList(host, {
    run: {
      key: 'demo',
      tasks: [],
      proposals: [
        { pid: 'p1', state: 'proposed', scope: 'wave', group: 'ship', summary: 'split the reader', hash, diff: '--- a/a.py\n', truncated: false },
        { pid: 'p2', state: 'refused', scope: 'plan', group: '', summary: 'nope', reason: 'does not fit', hash: 'cd', diff: 'x', truncated: true },
        { pid: 'p4', state: 'approved', scope: 'wave', group: null, summary: 'done', hash: 'ee', diff: '' },
        { pid: 'p5', state: 'rejected', scope: 'plan', summary: 'no', reason: 'later', diff: 'y' },
      ],
    },
    refresh: () => { refreshed += 1; },
  });
  assert.equal(host.dataset.proposals, '1');
  const cards = host.querySelectorAll('article.brief-proposal');
  assert.equal(cards.length, 4);
  assert.equal(cards[0].querySelector('.brief-state').textContent, 'proposed');
  assert.equal(cards[0].querySelector('.brief-proposal-group').textContent, 'ship');
  assert.equal(cards[0].querySelector('code.brief-hash').textContent, hash);
  assert.equal(cards[0].querySelector('.brief-approve').textContent, 'Approve');
  assert.equal(cards[0].querySelector('.brief-reject').textContent, 'Reject');
  assert.equal(cards[1].querySelector('.brief-reason').textContent, 'does not fit');
  assert.equal(cards[1].querySelector('.brief-proposal-group').textContent, '—');
  assert.equal(cards[1].querySelector('button'), null);
  assert.equal(cards[2].querySelector('.brief-state').textContent, 'approved');
  assert.equal(cards[2].querySelector('button'), null);
  assert.equal(cards[2].querySelector('.brief-proposal-group').textContent, '—');
  assert.equal(cards[3].querySelector('.brief-state').textContent, 'rejected');
  assert.equal(cards[3].querySelector('button'), null);
  assert.equal(cards[3].querySelector('.brief-reason'), null);
  assert.deepEqual(seen, [
    { text: '--- a/a.py\n', truncated: false },
    { text: 'x', truncated: true },
    { text: '', truncated: false },
    { text: 'y', truncated: false },
  ]);
  await cards[0].querySelector('.brief-approve').click();
  cards[0].querySelector('input').value = '  not now  ';
  await cards[0].querySelector('.brief-reject').click();
  const bare = document.createElement('div');
  B.renderList(bare, {
    run: { key: 'demo2', tasks: [], proposals: [{ pid: 'p9', state: 'proposed', scope: 'wave', summary: 'again', hash: 'abc', diff: '' }] },
  });
  await bare.querySelector('.brief-reject').click();
  assert.deepEqual(posts, [
    { path: '/api/runs/demo/proposals/p1/apply', body: { hash } },
    { path: '/api/runs/demo/proposals/p1/reject', body: { reason: 'not now' } },
    { path: '/api/runs/demo2/proposals/p9/reject', body: {} },
  ]);
  assert.equal(refreshed, 0);
});

test('without the diff viewer the patch is still text', () => {
  delete globalThis.ForgeDiff;
  arm();
  const host = document.createElement('div');
  B.renderList(host, {
    run: {
      key: 'plain',
      tasks: [],
      proposals: [{ pid: 'p1', state: 'refused', scope: 'plan', summary: 'nope', reason: 'because', diff: 'not a hunk\n', truncated: true }],
    },
  });
  const card = host.querySelector('article.brief-proposal');
  assert.equal(card.querySelector('pre.brief-diff').textContent, 'not a hunk\n');
  assert.equal(card.querySelector('p.warn').textContent, 'This diff was cut short.');
  assert.equal(card.querySelector('button'), null);
});

test('approve and reject stay disabled while the call is in flight', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  globalThis.ForgeDiff = { renderPatch() {} };
  globalThis.ForgeUI = {
    controlsEnabled: () => Promise.resolve(true),
    post: () => pending,
    outcome() {},
  };
  const host = document.createElement('div');
  B.renderList(host, {
    run: {
      key: 'pend',
      tasks: [],
      proposals: [{ pid: 'p3', state: 'proposed', scope: 'wave', group: 'ship', summary: 'split it', hash: 'abc', diff: 'x' }],
    },
  });
  const card = host.querySelector('article.brief-proposal');
  const approve = card.querySelector('.brief-approve');
  const done = approve.click();
  assert.equal(approve.disabled, true);
  assert.equal(card.querySelector('.brief-reject').disabled, true);
  assert.equal(card.querySelector('input').disabled, true);
  release({ ok: false, status: 500, data: null });
  await done;
  assert.equal(approve.disabled, false);
  assert.equal(card.querySelector('.brief-reject').disabled, false);
  assert.equal(card.querySelector('input').disabled, false);
});

test('read-only shows the proposal and the question with no buttons', async () => {
  globalThis.ForgeUI = {
    controlsEnabled: () => Promise.resolve(false),
    post() { throw new Error('read-only must not post'); },
    outcome() {},
  };
  const host = document.createElement('div');
  B.renderList(host, {
    run: {
      key: 'ro',
      tasks: [],
      proposals: [{ pid: 'p1', state: 'proposed', scope: 'wave', group: 'ship', summary: 'split the reader', hash: 'abc', diff: 'x' }],
    },
  });
  const inspector = document.createElement('div');
  B.taskSection(inspector, { run: { key: 'ro' } }, {
    id: 'reader',
    brief: { questions: [{ id: 'q1', state: 'answered', text: 'wave?', scope: 'wave', group: 'ship', answer: 'yes' }] },
  });
  await Promise.resolve();
  assert.equal(host.querySelectorAll('button').length, 0);
  assert.equal(host.querySelector('.brief-state').textContent, 'proposed');
  assert.equal(host.querySelector('.brief-summary').textContent, 'split the reader');
  assert.equal(inspector.querySelectorAll('button').length, 0);
  assert.equal(inspector.textContent.includes('wave?'), true);
});
