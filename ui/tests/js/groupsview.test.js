'use strict';
// node --test: the groups block and the group page (groupsview.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

function installDocument() {
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
  function Element(tag) {
    this.tagName = String(tag).toUpperCase();
    this.className = '';
    this.children = [];
    this.href = '';
    this.hidden = false;
    this.id = '';
    this._text = '';
  }
  Object.defineProperty(Element.prototype, 'textContent', {
    get() { return this._text + this.children.map(child => child.textContent).join(''); },
    set(value) { this._text = value == null ? '' : String(value); this.children = []; },
  });
  Object.defineProperty(Element.prototype, 'childElementCount', { get() { return this.children.length; } });
  Element.prototype.append = function (...nodes) {
    for (const node of nodes) if (node) this.children.push(node);
  };
  Element.prototype.replaceChildren = function (...nodes) {
    this.children = [];
    this._text = '';
    this.append(...nodes);
  };
  Element.prototype.querySelectorAll = function (selector) { return queryAll(this, selector); };
  Element.prototype.querySelector = function (selector) { return queryAll(this, selector)[0] || null; };
  globalThis.document = {
    createElement(tag) { return new Element(tag); },
  };
}

installDocument();
const G = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'groupsview.js'));
const RM = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'runmodel.js'));
globalThis.ForgeRunModel = RM;

const hrefs = {
  groupHref: id => '#/runs/demo/group/' + id,
  taskHref: id => '#/runs/demo/tasks/' + id,
};

function run(groups, tasks) {
  return { tasks: tasks || [], groups };
}

test('summary is empty when the run has no groups', () => {
  assert.equal(G.summary(null), null);
  assert.equal(G.summary({}), null);
  assert.equal(G.summary({ groups: null }), null);
  assert.equal(G.summary({ groups: [] }), null);
  assert.equal(G.summary({ tasks: [{ id: 'page', status: 'MERGED' }] }), null);
});

test('a per-task group and a review group each get a row, and a review group links to its page', () => {
  const block = G.summary(run([
    { id: 'loose', review: 'task', qa: null, members: ['page'], title: null, goal: null },
    { id: 'ship', review: 'group', qa: 'opus', members: ['reader', 'server'], title: 'Ship the reader', goal: 'Land it together',
      status: 'NEEDS_DECISION', round: 2, implicated: ['server'] },
  ], [
    { id: 'page', status: 'MERGED' },
    { id: 'reader', status: 'MERGED' },
    { id: 'server', status: 'IMPLEMENTED' },
  ]), hrefs);
  const rows = block.querySelectorAll('article.group-row');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector('.group-title').textContent, 'loose');
  assert.equal(rows[0].querySelector('.group-scope').textContent, 'per-task review');
  assert.equal(rows[0].querySelector('.group-goal').textContent, '(no goal)');
  assert.equal(rows[0].querySelector('a.group-title'), null);
  assert.equal(rows[0].querySelector('.group-state'), null);
  assert.equal(rows[0].querySelector('a.st-merged').textContent, 'page');
  assert.equal(rows[0].querySelector('a.st-merged').href, '#/runs/demo/tasks/page');

  assert.equal(rows[1].querySelector('a.group-title').textContent, 'Ship the reader');
  assert.equal(rows[1].querySelector('a.group-title').href, '#/runs/demo/group/ship');
  assert.equal(rows[1].querySelector('.group-scope').textContent, 'one review');
  assert.equal(rows[1].querySelector('.group-goal').textContent, 'Land it together');
  assert.equal(rows[1].querySelector('.group-state .chip').textContent, 'NEEDS_DECISION');
  assert.match(rows[1].querySelector('.group-round').textContent, /round 2/);
  assert.equal(rows[1].querySelector('.group-implicated').textContent, 'implicated server');
  assert.equal(rows[1].querySelector('a.st-implemented').textContent, 'server');
  assert.equal(rows[1].querySelector('a.st-implemented').href, '#/runs/demo/tasks/server');
  assert.match(rows[1].textContent, /opus/);
});

test('a group with an empty goal says (no goal), and a missing title shows the id', () => {
  const block = G.summary(run([
    { id: 'bare', review: 'group', qa: 'opus', members: [], title: '', goal: '', status: null, round: null, implicated: null },
  ]), hrefs);
  const row = block.querySelector('article.group-row');
  assert.equal(row.querySelector('.group-title').textContent, 'bare');
  assert.equal(row.querySelector('.group-goal').textContent, '(no goal)');
  assert.equal(row.querySelector('.group-state').textContent.includes('no status'), true);
  assert.match(row.textContent, /round —/);
  assert.match(row.textContent, /implicated none/);
  assert.equal(row.querySelector('.muted').textContent, 'no members');
});

test('the findings table marks ambiguous and blocking findings', () => {
  const table = G.findingsTable({
    findings: [
      { n: 1, severity: 'P1', status: 'CONFIRMED', file: 'ui/reader.py', line: 42, tasks: ['reader', 'page'], blocking: true, ambiguous: false },
      { n: 2, severity: 'P3', status: 'OPEN', file: 'ui/page.html', line: null, tasks: [], blocking: false, ambiguous: false },
      { n: 3, severity: null, status: null, file: null, line: null, tasks: ['docs'], blocking: false, ambiguous: true },
    ],
    ambiguous: [1],
  });
  const rows = table.querySelectorAll('tr.group-ambiguous');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector('td.group-blocking').textContent, 'yes');
  assert.equal(rows[0].querySelector('td.mono').textContent, 'ui/reader.py:42');
  assert.match(rows[0].textContent, /reader, page/);
  assert.match(rows[0].textContent, /yes/);
  const plain = table.querySelectorAll('tbody tr')[1];
  const cells = plain.querySelectorAll('td');
  assert.equal(plain.className, '');
  assert.equal(plain.querySelector('td.group-blocking'), null);
  assert.equal(cells[2].textContent, 'ui/page.html');
  assert.equal(cells[4].textContent, 'no');
  assert.equal(cells[5].textContent, 'no');
  assert.match(rows[1].textContent, /docs/);
  assert.equal(G.findingsTable(null).textContent, 'No findings.');
  assert.equal(G.findingsTable({ findings: [] }).textContent, 'No findings.');
});

test('NEEDS_DECISION shows the retry command, quoted when the plan path has a space', () => {
  assert.equal(G.decisionCommand({ id: 'ship', members: ['reader', 'server'] }, '/plans/my plan'),
    "forge group retry '/plans/my plan' ship --tasks reader,server");
  assert.equal(G.decisionCommand({ id: 'ship', members: [] }, '/plans/demo'),
    'forge group retry /plans/demo ship --tasks …');
  assert.equal(G.decisionCommand({ id: 'ship', members: null }, null),
    'forge group retry <plan> ship --tasks …');
  const page = G.pageView({
    id: 'ship', review: 'group', qa: 'opus', members: ['reader', 'server'], title: 'Ship the reader', goal: 'Land it together',
    status: 'NEEDS_DECISION', round: 1, implicated: ['server'], decision: 'reasons: ambiguous',
    attribution: { findings: [{ n: 1, severity: 'P1', status: 'CONFIRMED', file: 'ui/reader.py', line: 42, tasks: ['reader'], blocking: true, ambiguous: true }], ambiguous: [1] },
    rounds: [{ round: 1, epoch: 1760601000, status: 'NEEDS_DECISION', implicated: ['server'], reviewer: 'opus' }],
    members_by_round: [{ round: 1, members: [{ task: 'reader', commit: 'abc123', attempt: 1 }] }],
    known_issues: 'tolerated gap', qa_last: 'Reviewed the group.\n',
    execution: { id: 'qa-1', role: 'qa' },
  }, {
    plan: '/plans/my plan', run: { tasks: [{ id: 'server', status: 'IMPLEMENTED' }] }, back: '#/runs/demo',
    taskHref: hrefs.taskHref, transcriptHref: execution => '#/runs/demo/executions/' + execution.id + '/conversation',
  });
  assert.equal(page.querySelector('.group-cmd').textContent, "forge group retry '/plans/my plan' ship --tasks reader,server");
  assert.equal(page.querySelector('.group-decision p').textContent, 'reasons: ambiguous');
  assert.equal(page.querySelector('tr.group-ambiguous td.group-blocking').textContent, 'yes');
  assert.match(page.querySelector('.group-known').textContent, /tolerated gap/);
  assert.match(page.querySelector('.group-review').textContent, /Reviewed the group/);
  assert.equal(page.querySelector('a.diff-link').href, '#/runs/demo/executions/qa-1/conversation');
  assert.match(page.querySelector('.group-rounds').textContent, /Round 1/);
  assert.match(page.querySelector('.group-rounds').textContent, /abc123/);
  assert.equal(page.querySelector('.group-back').href, '#/runs/demo');
  const passing = G.pageView({ id: 'ship', review: 'group', status: 'PASS', members: [], rounds: [], qa_last: null, known_issues: null, execution: null }, {});
  assert.equal(passing.querySelector('.group-decision'), null);
  assert.equal(passing.querySelector('.group-known').textContent.includes('None.'), true);
  assert.equal(passing.querySelector('.group-review').textContent.includes('No review yet.'), true);
  assert.equal(passing.querySelector('.group-transcript').textContent.includes('No review transcript.'), true);
  assert.equal(passing.querySelector('.group-rounds').textContent.includes('No rounds.'), true);
});

test('a task chip names its group and links to the group page', () => {
  const chip = G.taskChip('ship', '#/runs/demo/group/ship');
  assert.equal(chip.textContent, 'group ship');
  assert.equal(chip.className, 'group-chip');
  assert.equal(chip.href, '#/runs/demo/group/ship');
  assert.equal(G.taskChip(null, '#/'), null);
  assert.equal(G.taskChip('', '#/'), null);
});

test('IMPLEMENTED is labelled awaiting group review', () => {
  assert.equal(RM.statusText({ status: 'IMPLEMENTED' }), 'awaiting group review');
  assert.equal(RM.statusLabel('IMPLEMENTED'), 'awaiting group review');
  assert.equal(RM.statusText({ status: 'FAIL' }), 'FAIL');
  assert.equal(RM.statusLabel('MERGED'), 'MERGED');
  assert.equal(RM.isWaiting('IMPLEMENTED'), false);
});
