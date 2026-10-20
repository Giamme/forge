'use strict';
// node --test: the files heatmap's pure model (matrix, describe, normPrefs).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const FM = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'files.js'));

const task = (id, wave, over = {}) => Object.assign({ id, wave, status: 'MERGED', planned: [], planned_all: false, attempts: [], current: [], drift: [], conflicts: [] }, over);
const tries = (...lists) => lists.map((files, i) => ({ attempt: i + 1, execution: `qa-${i}`, files }));
function files(tasks, extra = {}) {
  // the endpoint's files[]: every path a task touched, drifted into, conflicted on or planned as a file
  const by = new Map();
  const add = p => { if (!by.has(p)) by.set(p, { path: p, dir: p.includes('/') ? p.slice(0, p.lastIndexOf('/')) : '', planned_by: [], touched_by: [], drift_by: [], conflict_by: [] }); return by.get(p); };
  for (const t of tasks) {
    for (const p of new Set([...t.current, ...t.attempts.flatMap(a => a.files)])) add(p).touched_by.push(t.id);
    for (const p of t.drift) add(p).drift_by.push(t.id);
    for (const p of t.conflicts) add(p).conflict_by.push(t.id);
  }
  return Object.assign({ run: 'abc123abc123', files: [...by.values()].sort((a, b) => a.path < b.path ? -1 : 1), planned_dirs: [], tasks, overlaps: [], deferrals: [], truncated: false, warnings: [] }, extra);
}
const row = (M, p) => M.rows.find(r => r.path === p);
const paths = M => M.rows.map(r => `${'  '.repeat(r.depth)}${r.kind === 'dir' ? r.name + '/' : r.name}`);

test('files group under nested directories; root files sit at depth 0, dirs before files', () => {
  const d = files([task('a', 1, { current: ['README.md', 'ui/app.js', 'ui/lib/x.js', 'ui/lib/y.js', 'docs/api.md'] })]);
  const M = FM.matrix(d, null, {});
  assert.deepEqual(paths(M), ['docs/', '  api.md', 'ui/', '  lib/', '    x.js', '    y.js', '  app.js', 'README.md']);
  const ui = row(M, 'ui'), lib = row(M, 'ui/lib'), x = row(M, 'ui/lib/x.js');
  assert.equal(ui.kind, 'dir'); assert.equal(ui.files, 3); assert.equal(ui.dir, ''); assert.equal(ui.collapsed, false);
  assert.equal(lib.depth, 1); assert.equal(lib.dir, 'ui'); assert.equal(lib.files, 2);
  assert.equal(x.kind, 'file'); assert.equal(x.dir, 'ui/lib'); assert.equal(x.name, 'x.js');
  assert.equal(row(M, 'README.md').depth, 0);
  assert.deepEqual(M.dirs.sort(), ['docs', 'ui', 'ui/lib']);
  assert.equal(M.counts.files, 5); assert.equal(M.counts.shown, 5); assert.equal(M.counts.dirs, 3);
});

test('a chain of directories holding one directory each is one row', () => {
  const M = FM.matrix(files([task('a', 1, { current: ['src/main/java/A.java', 'src/main/java/B.java', 'top.txt'] })]), null, {});
  assert.deepEqual(paths(M), ['src/main/java/', '  A.java', '  B.java', 'top.txt']);
  assert.equal(row(M, 'src/main/java').name, 'src/main/java');
  assert.equal(row(M, 'src/main/java/A.java').dir, 'src/main/java');
});

test('planned: the file itself, a directory above it, or `.` for the whole repo', () => {
  const t = [task('f', 1, { planned: ['ui/a.py'] }), task('d', 2, { planned: ['ui'] }), task('all', 3, { planned: ['.'], planned_all: true }),
    task('w', 4, { current: ['ui/a.py', 'ui/b.py', 'docs/x.md'] })];
  const d = files(t);
  d.files.find(f => f.path === 'ui/a.py').planned_by = ['f', 'd'];
  d.files.find(f => f.path === 'ui/b.py').planned_by = ['d'];
  const M = FM.matrix(d, null, {});
  const a = row(M, 'ui/a.py').cells, b = row(M, 'ui/b.py').cells, x = row(M, 'docs/x.md').cells;
  assert.equal(a.f.planned, true); assert.equal(a.f.via, 'ui/a.py');
  assert.equal(a.d.planned, true); assert.equal(a.d.via, 'ui');
  assert.equal(b.f, undefined, 'f planned a.py only');
  assert.equal(b.d.via, 'ui');
  for (const c of [a.all, b.all, x.all]) { assert.equal(c.planned, true); assert.equal(c.via, '.'); }
  assert.equal(x.d, undefined);
  assert.equal(M.columns.find(c => c.id === 'all').planned_all, true);
  // dirs: planned when they are covered themselves, or a file under them is
  const ui = row(M, 'ui').cells;
  assert.equal(ui.d.planned, true); assert.equal(ui.d.via, 'ui');
  assert.equal(ui.f.planned, true); assert.equal(ui.f.via, null);
  assert.equal(row(M, 'docs').cells.all.via, '.');
  assert.match(FM.describe(row(M, 'ui/b.py'), M.columns.find(c => c.id === 'w'), b.w), /^w touched ui\/b\.py in its current diff/);
  assert.match(FM.describe(row(M, 'ui/b.py'), M.columns.find(c => c.id === 'd'), b.d), /^d planned ui\/b\.py · as ui\/ · not touched/);
  assert.match(FM.describe(row(M, 'docs/x.md'), M.columns.find(c => c.id === 'all'), x.all), /the whole repo/);
});

test('a planned directory with nothing under it yet is a row of its own', () => {
  const d = files([task('p', 1, { planned: ['assets'] }), task('q', 2, { current: ['ui/x.js'] })], { planned_dirs: [{ path: 'assets/', planned_by: ['p'] }] });
  const M = FM.matrix(d, null, {});
  const r = row(M, 'assets');
  assert.equal(r.kind, 'dir'); assert.equal(r.files, 0); assert.equal(r.cells.p.planned, true); assert.equal(r.cells.p.via, 'assets');
  assert.equal(FM.matrix(d, null, { show: { planned: false } }).rows.some(x => x.path === 'assets'), false);
});

test('touched: one per attempt whose diff had the file, or one for the current diff alone', () => {
  const t = [task('r', 1, { attempts: tries(['a.py'], ['a.py', 'b.py'], ['a.py']), current: ['a.py', 'c.py'] })];
  const M = FM.matrix(files(t), null, {});
  const a = row(M, 'a.py').cells.r, b = row(M, 'b.py').cells.r, c = row(M, 'c.py').cells.r;
  assert.equal(a.touched, 3); assert.deepEqual(a.attempts, [1, 2, 3]); assert.equal(a.current, true);
  assert.equal(b.touched, 1); assert.deepEqual(b.attempts, [2]); assert.equal(b.current, false);
  assert.equal(c.touched, 1); assert.deepEqual(c.attempts, []); assert.equal(c.current, true);
  assert.equal(M.max, 3);
  assert.equal(M.counts.touched, 3);
  assert.equal(FM.describe(row(M, 'a.py'), M.columns[0], a), 'r touched a.py in attempts 1, 2, 3 and its current diff');
  assert.equal(FM.describe(row(M, 'b.py'), M.columns[0], b), 'r touched b.py in attempt 2');
});

test('the brief\'s example title', () => {
  const t = [task('reader', 1, { planned: ['ui/reader.py'], attempts: tries(['ui/reader.py'], ['ui/reader.py']), current: ['ui/reader.py'] })];
  const d = files(t);
  d.files[0].planned_by = ['reader'];
  const M = FM.matrix(d, null, {}), r = row(M, 'ui/reader.py');
  assert.equal(FM.describe(r, M.columns[0], r.cells.reader), 'reader touched ui/reader.py in attempts 1, 2 and its current diff · planned');
  assert.match(FM.describe(row(M, 'ui'), M.columns[0], row(M, 'ui').cells.reader), /^reader: 1 of 1 file under ui\/ · touched in up to 2 attempts · planned files under it$/);
});

test('drift and conflict flags, and directories aggregate them (max heat, any flag)', () => {
  const t = [task('a', 1, { current: ['ui/x.js', 'ui/y.js'], attempts: tries(['ui/x.js'], ['ui/x.js']), drift: ['ui/y.js'] }),
    task('b', 2, { conflicts: ['ui/z.js'] })];
  const M = FM.matrix(files(t), null, {});
  assert.equal(row(M, 'ui/y.js').cells.a.drift, true);
  assert.equal(row(M, 'ui/x.js').cells.a.drift, false);
  assert.equal(row(M, 'ui/z.js').cells.b.conflict, true);
  assert.equal(row(M, 'ui/z.js').cells.b.touched, 0);
  const ui = row(M, 'ui').cells;
  assert.equal(ui.a.touched, 2); assert.equal(ui.a.drift, true); assert.equal(ui.a.conflict, false); assert.equal(ui.a.n, 2);
  assert.equal(ui.b.conflict, true); assert.equal(ui.b.n, 1);
  assert.equal(M.counts.drift, 1); assert.equal(M.counts.conflict, 1);
  assert.match(FM.describe(row(M, 'ui/y.js'), M.columns[0], row(M, 'ui/y.js').cells.a), /drift/);
  assert.match(FM.describe(row(M, 'ui/z.js'), M.columns[1], row(M, 'ui/z.js').cells.b), /conflicted on merge/);
});

test('overlap: another task of the same wave planned or touched the file', () => {
  const t = [task('a', 1, { current: ['x.py'] }), task('b', 1, { planned: ['x.py'] }), task('c', 2, { current: ['x.py'] }),
    task('d', 1, { current: ['y.py'] }), task('e', null, { current: ['y.py'] }), task('f', null, { current: ['y.py'] })];
  const d = files(t);
  d.files.find(f => f.path === 'x.py').planned_by = ['b'];
  const M = FM.matrix(d, null, {});
  const x = row(M, 'x.py').cells, y = row(M, 'y.py').cells;
  assert.equal(x.a.overlap, true); assert.deepEqual(x.a.with, ['b']);
  assert.equal(x.b.overlap, true); assert.deepEqual(x.b.with, ['a']);
  assert.equal(x.c.overlap, false, 'wave 2 is alone');
  assert.equal(y.d.overlap, false); assert.equal(y.e.overlap, false, 'no wave is no overlap');
  assert.match(FM.describe(row(M, 'x.py'), M.columns.find(c => c.id === 'a'), x.a), /same wave \(1\) as b/);
  assert.equal(M.counts.overlap, 2);
  // an overlap still shows when the other task's mark is switched off
  const N = FM.matrix(d, null, { show: { planned: false } });
  assert.equal(row(N, 'x.py').cells.b, undefined);
  assert.equal(row(N, 'x.py').cells.a.overlap, true);
  // two tasks that both planned a directory overlap on it, even with nothing under it
  const e = files([task('p', 1, { planned: ['lib'] }), task('q', 1, { planned: ['lib'] })], { planned_dirs: [{ path: 'lib/', planned_by: ['p', 'q'] }] });
  const lib = row(FM.matrix(e, null, {}), 'lib').cells;
  assert.equal(lib.p.overlap, true); assert.equal(lib.q.overlap, true);
});

test('filter: a case-insensitive substring of the path, keeping the dirs above what matches', () => {
  const d = files([task('a', 1, { current: ['ui/Reader.py', 'ui/app.js', 'tests/test_reader.py', 'docs/x.md'] })]);
  const M = FM.matrix(d, null, { filter: '  READER ' });
  assert.deepEqual(paths(M), ['tests/', '  test_reader.py', 'ui/', '  Reader.py']);
  assert.equal(M.counts.files, 4); assert.equal(M.counts.shown, 2); assert.equal(M.counts.filtered, 2);
  assert.equal(row(M, 'ui').files, 1);
  assert.equal(FM.matrix(d, null, { filter: 'nothing-like-it' }).rows.length, 0);
});

test('sort: by path, by most touched, by most tasks (ties by name, dirs first)', () => {
  const t = [task('a', 1, { attempts: tries(['b.py', 'c.py'], ['c.py'], ['c.py']), current: ['a.py', 'b.py', 'c.py'] }),
    task('b', 1, { current: ['a.py'] }), task('c', 2, { current: ['a.py', 'lib/z.py'], attempts: tries(['lib/z.py'], ['lib/z.py'], ['lib/z.py'], ['lib/z.py']) })];
  const d = files(t);
  assert.deepEqual(paths(FM.matrix(d, null, { sort: 'path' })), ['lib/', '  z.py', 'a.py', 'b.py', 'c.py']);
  // heat: z.py 4 attempts, a.py 1+1+1, c.py 3, b.py 1
  assert.deepEqual(paths(FM.matrix(d, null, { sort: 'heat' })), ['lib/', '  z.py', 'a.py', 'c.py', 'b.py']);
  // tasks: a.py 3 tasks, then the rest 1 each, dirs before files
  assert.deepEqual(paths(FM.matrix(d, null, { sort: 'tasks' })), ['a.py', 'lib/', '  z.py', 'b.py', 'c.py']);
  assert.deepEqual(paths(FM.matrix(d, null, { sort: 'bogus' })), paths(FM.matrix(d, null, {})));
});

test('collapse: a collapsed dir keeps its aggregate and hides what is under it', () => {
  const d = files([task('a', 1, { current: ['ui/lib/x.js', 'ui/app.js', 'b.txt'], drift: ['ui/lib/x.js'] })]);
  const M = FM.matrix(d, null, { collapsed: ['ui/lib'] });
  assert.deepEqual(paths(M), ['ui/', '  lib/', '  app.js', 'b.txt']);
  assert.equal(row(M, 'ui/lib').collapsed, true);
  assert.equal(row(M, 'ui/lib').cells.a.drift, true);
  assert.equal(row(M, 'ui/lib').files, 1);
  const N = FM.matrix(d, null, { collapsed: ['ui'] });
  assert.deepEqual(paths(N), ['ui/', 'b.txt']);
  assert.deepEqual(N.dirs.sort(), ['ui', 'ui/lib'], 'dirs lists collapsed ones too');
  assert.equal(row(N, 'ui').cells.a.drift, true);
});

test('show switches a kind of mark off: cells lose it and rows left with nothing drop', () => {
  const t = [task('a', 1, { planned: ['p.py'], current: ['t.py'], drift: ['d.py'], conflicts: ['c.py'] })];
  const d = files(t);
  d.files.push({ path: 'p.py', dir: '', planned_by: ['a'], touched_by: [], drift_by: [], conflict_by: [] });
  const all = FM.matrix(d, null, {});
  assert.deepEqual(all.rows.map(r => r.path), ['c.py', 'd.py', 'p.py', 't.py']);
  const only = kind => FM.matrix(d, null, { show: { planned: kind === 'planned', touched: kind === 'touched', drift: kind === 'drift', conflict: kind === 'conflict' } }).rows.map(r => r.path);
  assert.deepEqual(only('planned'), ['p.py']);
  assert.deepEqual(only('touched'), ['t.py']);
  assert.deepEqual(only('drift'), ['d.py']);
  assert.deepEqual(only('conflict'), ['c.py']);
  const none = FM.matrix(d, null, { show: { planned: false, touched: false, drift: false, conflict: false } });
  assert.equal(none.rows.length, 0); assert.equal(none.counts.hidden, 4);
  const noTouch = FM.matrix(files([task('a', 1, { current: ['x'], drift: ['x'] })]), null, { show: { touched: false } });
  assert.equal(row(noTouch, 'x').cells.a.touched, 0); assert.equal(row(noTouch, 'x').cells.a.drift, true);
});

test('columns: by wave, then the run\'s task order, with its titles and statuses', () => {
  const run = { tasks: [{ id: 'z', title: 'Zed', wave: 2, status: 'RUNNING' }, { id: 'y', title: 'Why', wave: 1, status: 'MERGED' }, { id: 'w', title: 'Double-u', wave: null }] };
  const d = files([task('y', 1, { status: 'PASS' }), task('z', 2), task('x', 1), task('w', null)]);
  const M = FM.matrix(d, run, {});
  assert.deepEqual(M.columns.map(c => c.id), ['y', 'x', 'z', 'w']);
  assert.deepEqual(M.columns[0], { id: 'y', wave: 1, status: 'MERGED', title: 'Why', planned_all: false });
  assert.equal(M.columns[1].title, '');
  assert.equal(M.columns[3].status, 'MERGED', 'from the files endpoint when the run has none');
  assert.deepEqual(M.waves, [{ wave: 1, from: 0, to: 2 }, { wave: 2, from: 2, to: 3 }, { wave: null, from: 3, to: 4 }]);
  // without a run: the endpoint's own order
  assert.deepEqual(FM.matrix(d, undefined, {}).columns.map(c => c.id), ['y', 'x', 'z', 'w']);
  // a run with no files data still has its columns
  assert.deepEqual(FM.matrix(null, run, {}).columns.map(c => c.id), ['y', 'z', 'w']);
});

test('a solo run: one column, and the files its repo already had changed are existing cells', () => {
  const d = files([task('solo', 1, { status: 'PASS', attempts: tries(['src/solo.py']), current: ['src/solo.py'], existing: ['notes.txt', 'src/solo.py'] })]);
  const M = FM.matrix(d, { tasks: [{ id: 'solo', title: 'A solo run', wave: 1, status: 'PASS' }] }, {});
  assert.deepEqual(M.columns.map(c => c.id), ['solo']);
  assert.deepEqual(M.rows.map(r => r.path), ['src', 'src/solo.py', 'notes.txt']);
  const n = row(M, 'notes.txt').cells.solo;
  assert.equal(n.existing, true); assert.equal(n.planned, false); assert.equal(n.touched, 0);
  assert.equal(row(M, 'src/solo.py').cells.solo.existing, true);
  assert.equal(row(M, 'src').cells.solo.existing, true);
  assert.equal(M.counts.existing, 2);
  assert.match(FM.describe(row(M, 'notes.txt'), M.columns[0], n), /already had uncommitted changes/);
  assert.equal(FM.matrix(d, null, { show: { planned: false } }).rows.some(r => r.path === 'notes.txt'), false, 'the planned switch hides existing too');
});

test('empty or malformed input never throws', () => {
  const weird = [undefined, null, 0, 'x', [], {}, { files: 'no', tasks: 7 }, { tasks: [null, 1, 'a', { id: null }, { id: 'ok', attempts: [null, { files: 'nope' }, { attempt: 'x', files: ['a', null, 3] }], planned: [null, '.', 5], current: 'x', drift: [{}] }],
    files: [null, 3, { path: 5 }, { path: '' }, { path: 'b/', planned_by: 'x' }, { path: 'c', planned_by: ['ghost'] }], planned_dirs: [null, { path: '.' }, { path: 'q/', planned_by: [null] }], overlaps: 'x' }];
  for (const d of weird) {
    for (const run of [undefined, null, 'r', { tasks: 'x' }, { tasks: [null, { id: 3, wave: 'two' }] }]) {
      for (const opts of [undefined, null, 'x', { filter: 5, sort: 9, collapsed: 'a', show: 'all' }]) {
        const M = FM.matrix(d, run, opts);
        assert.ok(Array.isArray(M.rows) && Array.isArray(M.columns) && Array.isArray(M.waves));
        assert.equal(typeof M.max, 'number');
      }
    }
  }
  const M = FM.matrix(weird[weird.length - 1], null, {});
  assert.deepEqual(M.columns.map(c => c.id), ['ok']);
  assert.equal(row(M, 'a').cells.ok.touched, 1);
  assert.equal(row(M, 'a').cells.ok.planned, true, 'its `.` planned everything');
  assert.equal(row(M, 'c').cells.ok.planned, true);
  assert.equal(row(M, 'c').cells.ghost, undefined, 'a planner that is not a task has no column');
  assert.equal(FM.describe({ kind: 'file', path: 'x' }, null, null), '? · x');
});

test('task ids that are Object.prototype names are ordinary keys', () => {
  const M = FM.matrix(files([task('__proto__', 1, { current: ['a'] }), task('constructor', 1, { current: ['b'] })]), null, {});
  assert.equal(row(M, 'a').cells.__proto__.touched, 1);
  assert.equal(row(M, 'a').cells.constructor, undefined);
  assert.equal(row(M, 'b').cells.constructor.touched, 1);
});

test('normPrefs: defaults, unknown values and the collapsed cap', () => {
  assert.deepEqual(FM.normPrefs(null), { sort: 'path', show: { planned: true, touched: true, drift: true, conflict: true }, collapsed: [] });
  const p = FM.normPrefs({ sort: 'heat', show: { drift: false, planned: 0 }, collapsed: ['a', 'a', 3, '', 'b'] });
  assert.equal(p.sort, 'heat');
  assert.deepEqual(p.show, { planned: true, touched: true, drift: false, conflict: true });
  assert.deepEqual(p.collapsed, ['a', 'b']);
  assert.equal(FM.normPrefs({ sort: 'nope' }).sort, 'path');
  assert.equal(FM.normPrefs({ collapsed: Array.from({ length: 1500 }, (_, i) => 'd' + i) }).collapsed.length, 1000);
});

test('a big run stays quick', () => {
  const tasks = Array.from({ length: 40 }, (_, i) => task('t' + i, 1 + (i % 6), {
    planned: ['pkg' + (i % 10)], current: Array.from({ length: 20 }, (_, k) => `pkg${(i + k) % 10}/sub${k % 4}/f${(i * 7 + k) % 200}.py`) }));
  const d = files(tasks);
  const t0 = Date.now();
  const M = FM.matrix(d, null, { sort: 'heat' });
  assert.ok(Date.now() - t0 < 1500);
  assert.ok(M.counts.files > 150);
  assert.ok(M.rows.length > M.counts.shown);
});
