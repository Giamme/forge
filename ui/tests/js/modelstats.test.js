'use strict';
// node --test: the models view's project choices and project filter (modelstats.js) group by repo_root.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const MS = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'modelstats.js'));

const NOW = 1760700000, DAY = 86400;
// a task row as /api/models gives it: repo is the checkout the run started in, repo_root the project (null on an older server)
const task = (run, id, repo, repoRoot, alias = 'sol') => ({ run, task: id, kind: 'plan', repo, repo_root: repoRoot, difficulty: 'medium',
  created: NOW - DAY, explored: false, attempts: [{ n: 1, alias, spec: alias, source: 'execution', outcome: 'pass', reason: null, gate: false,
  model_s: null, duration_s: null, tokens: null, timed_out: false }], escalations: [] });

test('projects: the checkouts of one root are one project; a row without repo_root is grouped by repo', () => {
  const data = { tasks: [
    task('r1', 'a', '/dev/forge', '/dev/forge'),
    task('r2', 'b', '/dev/forge-c2', '/dev/forge'),
    task('r3', 'c', '/dev/forge-c3', '/dev/forge'),
    task('r4', 'd', '/old/tool', null),
  ], reviews: [{ run: 'r6', task: null, kind: 'final', alias: 'opus', spec: 'opus', verdict: 'PASS' }] };
  const runs = [{ key: 'r6', repo: '/x/new-wt', repo_root: '/x/new' }];
  assert.deepEqual(MS.options(data, runs).projects.map(p => [p.repo, p.name, p.tasks]),
    [['/dev/forge', 'forge', 3], ['/x/new', 'new', 0], ['/old/tool', 'tool', 1]], 'a run with reviews but no task rows still names its project');
});

test('projects: two roots with one folder name stay two projects, named by their parent folder', () => {
  const data = { tasks: [task('r1', 'a', '/work/forge', '/work/forge'), task('r2', 'b', '/home/forge', '/home/forge')] };
  assert.deepEqual(MS.options(data, []).projects.map(p => p.name), ['forge (home)', 'forge (work)']);
});

test('the project filter names a root and matches every checkout of it; a saved checkout path matches the rows that started there', () => {
  const data = { tasks: [
    task('r1', 'a', '/dev/forge', '/dev/forge', 'sol'),
    task('r2', 'b', '/dev/forge-c2', '/dev/forge', 'luna'),
    task('r3', 'c', '/old/tool', null, 'opus'),
  ], reviews: [{ run: 'r2', task: null, kind: 'final', alias: 'opus', spec: 'opus', verdict: 'PASS' }] };
  const L = project => MS.leaderboard(data, { project }, NOW, []);
  assert.equal(MS.leaderboard(data, {}, NOW, []).totals.tasks, 3);
  assert.equal(L('/dev/forge').totals.tasks, 2, 'the root: both checkouts');
  assert.equal(L('/dev/forge').totals.reviews, 1, 'a final review follows its run\'s root');
  assert.equal(L('/dev/forge-c2').totals.tasks, 1, 'a saved checkout path still matches the rows that started there');
  assert.equal(L('/old/tool').totals.tasks, 1, 'a row without repo_root matches by repo');
  assert.equal(L('/gone').totals.tasks, 0);
});

test('a final review whose run summary is not loaded yet still follows its own repo_root into the project filter', () => {
  // /api/models can load before /api/runs: the review row is then the only place its repository is named
  const data = { tasks: [], reviews: [{ run: 'r', task: null, kind: 'final', repo_root: '/dev/forge', alias: 'opus', spec: 'opus', verdict: 'PASS' }] };
  assert.deepEqual(MS.options(data, []).projects.map(p => p.repo), ['/dev/forge']);
  assert.equal(MS.leaderboard(data, { project: '/dev/forge' }, NOW, []).totals.reviews, 1, 'the project it offers keeps the review');
  assert.equal(MS.leaderboard(data, { project: '/gone' }, NOW, []).totals.reviews, 0);
});
