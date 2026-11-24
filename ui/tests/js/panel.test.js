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

test('the Handoff section names the driver and lists the notes, newest first; a run without a handoff has none', () => {
  const sections = host => host.querySelectorAll('section.insp-section').filter(s => s.children[0] && s.children[0].textContent === 'Handoff');
  const now = Date.now() / 1000;
  const host = render(planRun({ handoff: {
    driver: { harness: 'codex', session: null, since: now - 600, last_seen: now - 60, stale: false },
    notes: [{ at: now - 30, actor: 'cli', text: 'took over from claude-code' }, { at: now - 900, actor: null, text: 'user accepted task a' }],
    notes_total: 3 } }));
  const [section] = sections(host);
  assert.ok(section, 'a Handoff section');
  assert.match(section.textContent, /Driver: codex · since 10m00s · seen 1m00s ago/);
  assert.deepEqual(section.querySelectorAll('ul.handoff-notes li').map(li => li.children[1].textContent),
    ['took over from claude-code', 'user accepted task a']);
  assert.match(section.textContent, /1 older notes in handoff\.notes\.jsonl/);
  const none = render(planRun({ handoff: { driver: null, notes: [], notes_total: 0 } }));
  assert.match(sections(none)[0].textContent, /Driver: none recorded/);
  assert.equal(sections(render(planRun({ handoff: null }))).length, 0);
});

// ---- #153: the limits and stages on a run, and the tree of subtasks on a task.

function subtask(id, extra) {
  const parts = id.split('/');
  return Object.assign({
    id, parent: parts.length > 2 ? parts.slice(0, -1).join('/') : null, run: 'k' + parts.length, run_id: 'plan-b--d',
    task: parts[parts.length - 1], level: parts.length - 1, title: 'Part ' + id, status: 'PENDING', difficulty: 'low',
    model: 'sol', qa: 'opus', decision: 'atomic', reason: null, by: 'default', attempts: 0, tokens: null, has_diff: false,
  }, extra);
}

function splitTask(extra) {
  return Object.assign({
    id: 'd', title: 'The whole of D', status: 'RUNNING', deps: [], files: ['src/'], difficulty: 'high', attempts: [],
    executions: [], escalations: [], reviews: [],
    decomposition: {
      decision: 'split', reason: 'two halves that can be reviewed apart', by: 'user', at: 1,
      sub: { key: 'subkey123456', run_id: 'plan-b--d', path: '/tmp/plans/b/tasks/d/sub' },
      merged: false, waiting: ['b'], start: 'fresh', kept: '0123456789abcdef0123',
      nodes: [
        subtask('d/a', { status: 'MERGED', decision: 'split', reason: 'two steps', by: 'planner', attempts: 2,
          tokens: { input: 1200, cached: 900, output: 300 }, has_diff: true }),
        subtask('d/a/one', { status: 'MERGED', tokens: { input: 400, cached: 0, output: 100 } }),
        subtask('d/b', { status: 'FAIL', decision: 'bounded', reason: 'the run already has 24 subtasks' }),
      ],
    },
  }, extra);
}

function renderTask(task, extra) {
  // The section alone: the rest of a task's page needs a browser.
  const host = document.createElement('div'), seen = [];
  P.decompositionSection(host, Object.assign({
    run: planRun({ tasks: [task] }), now: 0, hash: () => '#/here', onSelect() {},
    nodeHash: n => `#/runs/${n.run}/tasks/${n.task}`, onSubtasks: (id, nodes) => seen.push([id, nodes && nodes.length]),
  }, extra), task);
  return { host, seen };
}

function sectionOf(host, title) {
  return host.querySelectorAll('section.insp-section').find(s => s.children[0] && s.children[0].textContent === title) || null;
}

test('a run shows the limits a split works within, what was split, and the stages of a larger goal', () => {
  const run = render(planRun({
    decomposition: { limits: { depth: 2, children: 6, nodes: 24, frozen: true }, split: 1, subtasks: 3, statuses: {} },
    later: { stages: [{ title: 'The API' }, { title: 'The UI' }], stage: 2, first: 'aaaaaaaaaaaa' },
  }));
  const fields = fieldMap(run);
  assert.equal(fields.get('Decomposition').textContent,
    'depth 2 · up to 6 subtasks per split · 24 subtasks in the run (frozen) · 3 subtasks in 1 split task');
  assert.equal(fields.get('Stage').textContent, '2 of a larger goal');
  assert.equal(fields.get('Later stages').textContent, 'The API · The UI');
  const off = fieldMap(render(planRun({ decomposition: { limits: { depth: 0, children: 6, nodes: 24, frozen: false }, split: 0, subtasks: 0 } })));
  assert.equal(off.get('Decomposition').textContent, 'splitting is off for this run');
  // A plan with no record reads as it did before these rows existed.
  const plain = labels(render(planRun({})));
  for (const label of ['Decomposition', 'Stage', 'Later stages', 'Subtasks of']) assert.equal(plain.includes(label), false, label);
});

test('the subtasks of a split task link the task they belong to, in the plan above', () => {
  const sub = render(planRun({ parent: { key: 'bbbbbbbbbbbb', run_id: 'plan-b', task: 'd' } }));
  assert.deepEqual(fieldMap(sub).get('Subtasks of').querySelectorAll('a').map(a => [a.textContent, a.href]),
    [['d · plan-b', '#/runs/bbbbbbbbbbbb/tasks/d']]);
});

test('a split task shows its decision, who made it, why, and every subtask below it', () => {
  const { host, seen } = renderTask(splitTask());
  const s = sectionOf(host, 'Decomposition');
  assert.ok(s, 'the section is there');
  assert.deepEqual(s.querySelectorAll('div.chips span').map(span => span.textContent), ['split', 'decided by user']);
  const text = s.textContent;
  assert.ok(text.includes('two halves that can be reviewed apart'));
  assert.ok(text.includes('3 subtasks · 2 merged · 2.0k tokens'), text);
  assert.ok(text.includes('the task is reviewed on all of their work once every one has merged'));
  assert.ok(text.includes("started over from the task's base (earlier work kept at 0123456789ab)"));
  assert.ok(text.includes('Waiting for: b'));
  assert.deepEqual(s.querySelectorAll('p a').map(a => a.href), ['#/runs/subkey123456']);
  const rows = s.querySelectorAll('ul.node-tree li');
  assert.deepEqual(rows.map(li => li.className), ['ft-node ft-d0', 'ft-node ft-d1', 'ft-node ft-d0']);
  assert.deepEqual(rows.map(li => li.children[1].href), ['#/runs/k2/tasks/a', '#/runs/k3/tasks/one', '#/runs/k2/tasks/b']);
  assert.ok(rows[0].textContent.includes('MERGED · low · 2 attempts · 1.5k tokens'), rows[0].textContent);
  assert.deepEqual(rows[0].querySelectorAll('a.diff-link').map(a => a.href), ['#/runs/k2/tasks/a/diff']);
  assert.deepEqual(rows.map(li => li.querySelectorAll('span.tag').map(t => t.textContent)), [['split'], [], ['kept whole at a limit']]);
  assert.equal(rows[0].title, 'Part d/a\ntwo steps');
  assert.equal(rows[1].querySelectorAll('a.diff-link').length, 0);
  assert.deepEqual(seen, [['d', 3]]);
});

test('a task the planner kept whole says why, and a task nobody decided about says nothing', () => {
  const kept = renderTask(splitTask({ decomposition: { decision: 'atomic', reason: 'one coherent change', by: 'planner', sub: null, nodes: [] } }));
  const s = sectionOf(kept.host, 'Decomposition');
  assert.deepEqual(s.querySelectorAll('div.chips span').map(span => span.textContent), ['kept whole', 'decided by planner']);
  assert.ok(s.textContent.includes('one coherent change'));
  assert.equal(s.querySelectorAll('ul.node-tree').length, 0);
  assert.deepEqual(kept.seen, [['d', null]]);
  const quiet = renderTask(splitTask({ decomposition: { decision: 'atomic', reason: null, by: 'default', sub: null, nodes: [] } }));
  assert.equal(sectionOf(quiet.host, 'Decomposition'), null);
  const none = renderTask(splitTask({ decomposition: undefined }));
  assert.equal(sectionOf(none.host, 'Decomposition'), null);
  const bounded = renderTask(splitTask({ decomposition: { decision: 'bounded', reason: 'the run is 2 levels deep already', by: 'planner', sub: null, nodes: [] } }));
  assert.deepEqual(sectionOf(bounded.host, 'Decomposition').querySelectorAll('div.chips span').map(span => span.textContent),
    ['kept whole at a limit', 'decided by planner']);
});
