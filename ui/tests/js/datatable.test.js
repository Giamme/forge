'use strict';
// node --test: the data tables' pure parts (datatable.js): saved preferences against changing columns, and sorting.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const T = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'datatable.js'));

const cols = ids => ids.map(id => ({ id, label: id.toUpperCase(), value: r => r[id] }));

test('saved preferences survive columns being added and removed', () => {
  const cases = [
    ['no preferences: the default order, nothing hidden, no sort', ['a', 'b', 'c'], {}, { order: ['a', 'b', 'c'], hidden: [], sort: null }],
    ['a saved order is kept', ['a', 'b', 'c'], { order: ['c', 'a', 'b'] }, { order: ['c', 'a', 'b'], hidden: [], sort: null }],
    ['a column gone since is dropped', ['a', 'c'], { order: ['c', 'b', 'a'], hidden: ['b'], sort: { id: 'b', dir: 'asc' } },
      { order: ['c', 'a'], hidden: [], sort: null }],
    ['a new column lands after its default predecessor', ['a', 'b', 'n', 'c'], { order: ['c', 'b', 'a'] }, { order: ['c', 'b', 'n', 'a'], hidden: [], sort: null }],
    ['a new first column goes first', ['n', 'a', 'b'], { order: ['b', 'a'] }, { order: ['n', 'b', 'a'], hidden: [], sort: null }],
    ['duplicates in a saved order count once', ['a', 'b'], { order: ['b', 'b', 'a'] }, { order: ['b', 'a'], hidden: [], sort: null }],
    ['the first column never hides', ['a', 'b'], { hidden: ['a', 'b'] }, { order: ['a', 'b'], hidden: ['b'], sort: null }],
    ['a sort keeps its direction', ['a', 'b'], { sort: { id: 'b', dir: 'desc' } }, { order: ['a', 'b'], hidden: [], sort: { id: 'b', dir: 'desc' } }],
    ['a sort with no direction is dropped', ['a', 'b'], { sort: { id: 'b', dir: 'up' } }, { order: ['a', 'b'], hidden: [], sort: null }],
    ['garbage reads as no preferences', ['a', 'b'], 'nonsense', { order: ['a', 'b'], hidden: [], sort: null }],
  ];
  for (const [what, ids, prefs, want] of cases) assert.deepEqual(T.normPrefs(prefs, cols(ids)), want, what);
  const fixed = [{ id: 'a' }, { id: 'b', hideable: false }, { id: 's', sortable: false }];
  assert.deepEqual(T.normPrefs({ hidden: ['b', 's'], sort: { id: 's', dir: 'asc' } }, fixed), { order: ['a', 'b', 's'], hidden: ['s'], sort: null },
    'a column that cannot hide stays shown, and one that cannot sort is never the sort');
  const quiet = [{ id: 'a' }, { id: 'b' }, { id: 'q', hidden: true }];
  assert.deepEqual(T.normPrefs({}, quiet).hidden, ['q'], 'a column that starts hidden is hidden with no preferences');
  assert.deepEqual(T.normPrefs({ order: ['a', 'b', 'q'], hidden: [] }, quiet).hidden, [], 'once the user has shown it, it stays shown');
  assert.deepEqual(T.normPrefs({ order: ['b', 'a'] }, quiet), { order: ['b', 'q', 'a'], hidden: ['q'], sort: null }, 'added since the order was saved: it starts hidden');
});

test('sorting: numbers by value, text naturally, empty values always last, ties in their original order', () => {
  const col = { id: 'v', value: r => r.v };
  const rows = [{ k: 1, v: 'task-10' }, { k: 2, v: null }, { k: 3, v: 'task-2' }, { k: 4, v: 7 }, { k: 5, v: '' }, { k: 6, v: 'Task-2' }, { k: 7, v: -1 }, { k: 8, v: NaN }];
  const keys = dir => T.sortRows(rows, col, dir).map(r => r.k);
  assert.deepEqual(keys('asc'), [7, 4, 3, 6, 1, 2, 5, 8]);
  assert.deepEqual(keys('desc'), [1, 3, 6, 4, 7, 2, 5, 8]);
  assert.deepEqual(keys(null), [1, 2, 3, 4, 5, 6, 7, 8], 'no sort keeps the given order');
  assert.deepEqual(T.sortRows([], col, 'asc'), []);
});

test('moving a column', () => {
  assert.deepEqual(T.move(['a', 'b', 'c', 'd'], 'a', 2), ['b', 'c', 'a', 'd']);
  assert.deepEqual(T.move(['a', 'b', 'c'], 'c', 0), ['c', 'a', 'b']);
  assert.deepEqual(T.move(['a', 'b', 'c'], 'b', 99), ['a', 'c', 'b'], 'past the end clamps');
  assert.deepEqual(T.move(['a', 'b'], 'x', 0), ['a', 'b'], 'an unknown column changes nothing');
});

test('a header click cycles ascending, descending, off', () => {
  let s = null;
  const seen = [];
  for (let i = 0; i < 3; i++) { s = T.nextSort(s, 'b'); seen.push(s && s.dir); }
  assert.deepEqual(seen, ['asc', 'desc', null]);
  assert.deepEqual(T.nextSort({ id: 'a', dir: 'desc' }, 'b'), { id: 'b', dir: 'asc' }, 'another column starts ascending');
});
