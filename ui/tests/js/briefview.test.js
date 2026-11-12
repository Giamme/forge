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
