'use strict';
// node --test: the model leaderboard's pure aggregation (modelstats.js) over /api/models facts.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const MS = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'modelstats.js'));

const NOW = 1760700000, DAY = 86400;
const att = (n, alias, outcome, o = {}) => ({ n, alias, spec: o.spec || alias, source: 'execution', outcome, reason: o.reason ?? null, gate: !!o.gate,
  model_s: o.model_s ?? null, duration_s: o.duration_s ?? null, tokens: o.tokens ?? null, timed_out: !!o.timed_out });
const task = (run, id, attempts, o = {}) => ({ run, task: id, kind: o.kind || 'plan', repo: o.repo || '/w/forge', difficulty: o.difficulty === undefined ? 'medium' : o.difficulty,
  created: o.created ?? NOW - DAY, explored: !!o.explored, propensity: null, final_status: 'MERGED', accepted: !!o.accepted, attempts, escalations: o.escalations || [] });
const review = (run, id, kind, alias, o = {}) => ({ run, task: id, n: kind === 'final' ? null : 1, kind, alias, spec: alias, verdict: 'verdict' in o ? o.verdict : 'PASS',
  findings: o.findings === undefined ? { P0: 0, P1: 0, P2: 0, P3: 0, unlabelled: 0, plausible: 0, confirmed: 0, worst: null } : o.findings,
  duration_s: o.duration_s ?? null, tokens: o.tokens ?? null, agreement: o.agreement ?? null, primary_alias: o.primary_alias ?? null });
const row = (rows, alias) => rows.find(r => r.alias === alias);

test('wilson matches scripts/forge_dataset/routing.py', () => {
  // expected values printed by routing.py's wilson(k, n[, z])
  const cases = [
    [0, 1, 1.96, 0.0, 0.7934567085261071], [1, 1, 1.96, 0.20654329147389294, 1.0], [0, 10, 1.96, 0.0, 0.2775401687666166],
    [3, 7, 1.96, 0.15821692226262685, 0.7495457695909742], [5, 10, 1.96, 0.23658959361548731, 0.7634104063845126],
    [10, 10, 1.96, 0.7224598312333834, 1.0], [1, 2, 1.96, 0.09452865480086614, 0.9054713451991339],
    [17, 40, 1.96, 0.28509168524815875, 0.5780519956394001], [99, 100, 1.96, 0.9455124752390653, 0.9982326134344526],
    [0, 10, 2.576, 2.7755575615628914e-17, 0.3988858710287997], [3, 7, 2.576, 0.11636412362722026, 0.8102993827254801],
  ];
  for (const [k, n, z, lo, hi] of cases) {
    const w = MS.wilson(k, n, z);
    assert.ok(Math.abs(w.lo - lo) < 1e-12 && Math.abs(w.hi - hi) < 1e-12, `${k}/${n} z=${z}: ${w.lo} ${w.hi}`);
    assert.equal(w.p, k / n);
  }
  assert.deepEqual(MS.wilson(0, 0), { p: null, lo: null, hi: null });
  assert.deepEqual(MS.wilson(3, 7), MS.wilson(3, 7, 1.96));
});

test('median follows statistics.median and ignores nulls', () => {
  assert.equal(MS.median([3, null, 1, 2]), 2);
  assert.equal(MS.median([4, 1, 3, 2]), 2.5);
  assert.equal(MS.median([null, undefined]), null);
  assert.equal(MS.median(undefined), null);
});

test('first attempts: the lowest attempt number counts, censored attempts count in no rate', () => {
  const data = { tasks: [
    task('r1', 'a', [att(2, 'luna', 'pass'), att(1, 'sol', 'fail')]),                       // given out of order
    task('r1', 'b', [att(1, 'sol', 'pass')]),
    task('r1', 'c', [att(1, 'sol', 'censored', { reason: 'reviewer_error' })]),
    // attempt 1 was refunded (INFRA) and dropped by the server: the attempts as given start at 2
    task('r2', 'd', [att(2, 'luna', 'fail'), att(3, 'sol', 'pass')]),
    task('r2', 'e', []),
  ] };
  const L = MS.leaderboard(data, { minN: 0 }, NOW);
  const sol = row(L.implementers, 'sol'), luna = row(L.implementers, 'luna');
  assert.equal(sol.tasks, 3);
  assert.deepEqual([sol.first.k, sol.first.n, sol.first.censored], [1, 2, 1]);
  assert.deepEqual(sol.first.reasons, { reviewer_error: 1 });
  assert.equal(sol.retries, 1);
  assert.deepEqual(sol.attempts, { pass: 2, fail: 1, censored: 1, total: 4 });
  assert.equal(luna.tasks, 1);
  assert.deepEqual([luna.first.k, luna.first.n], [0, 1]);
  assert.equal(luna.retries, 1);
  assert.equal(sol.first.lo, MS.wilson(1, 2).lo);
  assert.equal(L.totals.tasks, 5);
  assert.equal(L.totals.tasksRun, 4);
  assert.equal(L.totals.attempts, 6);
  assert.deepEqual([L.totals.first.k, L.totals.first.n, L.totals.first.censored], [1, 3, 1]);
});

test('escalations count by alias, timeouts by who ran out, gate and human accepts', () => {
  const data = { tasks: [
    task('r1', 'a', [att(1, 'luna', 'fail', { timed_out: true }), att(2, 'sol', 'pass', { gate: true })],
      { escalations: [{ n: 1, from: 'luna:low', to: 'sol:xhigh' }] }),
    task('r1', 'b', [att(1, 'luna', 'censored', { reason: 'reviewer_timeout' }), att(2, 'luna', 'censored', { reason: 'forge_timeout' }), att(3, 'luna', 'censored', { reason: 'dwarf_timeout' })],
      { escalations: [{ n: 2, from: 'luna', to: 'sol' }, { n: 3, from: 'sol:high', to: 'opus' }] }),
    // accepted by a human although no attempt passed: credited to the last attempt's model
    task('r1', 'c', [att(1, 'sol', 'fail'), att(2, 'luna', 'fail')], { accepted: true }),
    // accepted but an attempt passed: forge's own pass, not a human accept
    task('r1', 'd', [att(1, 'luna', 'fail'), att(2, 'sol', 'pass')], { accepted: true }),
  ] };
  const L = MS.leaderboard(data, {}, NOW);
  const luna = row(L.implementers, 'luna'), sol = row(L.implementers, 'sol');
  assert.equal(luna.escalationsAway, 2);
  assert.equal(sol.escalationsAway, 1);
  assert.equal(luna.timeouts, 4);
  assert.deepEqual(luna.timeoutKinds, { implementer: 2, reviewer: 1, forge: 1 });
  assert.equal(sol.gateAccepts, 1);
  assert.equal(luna.humanAccepts, 1);
  assert.equal(sol.humanAccepts, 0);
});

test('times are medians, tokens are sums with their coverage', () => {
  const tk = (i, c, o) => ({ input: i, cached: c, output: o });
  const data = { tasks: [task('r', 'a', [att(1, 'sol', 'pass', { model_s: 100, duration_s: 110, tokens: tk(1000, 500, 10) })]),
    task('r', 'b', [att(1, 'sol', 'fail', { model_s: 300, duration_s: 320 }), att(2, 'sol', 'pass', { model_s: null, duration_s: 50, tokens: tk(2000, 0, 30) })])] };
  const sol = MS.leaderboard(data, {}, NOW).implementers[0];
  assert.equal(sol.medianModelS, 200);
  assert.equal(sol.medianDurationS, 110);
  assert.deepEqual(sol.tokens, { input: 3000, cached: 500, output: 40, attempts: 2, coverage: 2 / 3 });
});

test('rows below min n are flagged small but kept; rank by the interval\'s lower end, then n', () => {
  const tasks = [];
  for (let i = 0; i < 20; i++) tasks.push(task('r', 'a' + i, [att(1, 'sol', i < 15 ? 'pass' : 'fail')]));      // 15/20
  tasks.push(task('r', 'b', [att(1, 'luna', 'pass')]), task('r', 'c', [att(1, 'luna', 'pass')]));              // 2/2: the best rate, a low bound
  for (let i = 0; i < 4; i++) tasks.push(task('r', 'd' + i, [att(1, 'astra', i < 3 ? 'pass' : 'fail')]));       // 3/4: sol's rate, a lower bound
  const L = MS.leaderboard({ tasks }, { minN: 5 }, NOW);
  assert.deepEqual(L.implementers.map(r => r.alias), ['sol', 'luna', 'astra']);
  assert.deepEqual(L.implementers.map(r => r.small), [false, true, true]);
  assert.ok(L.caveats.some(c => c.includes('fewer than 5')));
  assert.equal(MS.leaderboard({ tasks }, { minN: 1 }, NOW).implementers.filter(r => r.small).length, 0);
  // equal lower bounds: more outcomes first
  const tie = MS.leaderboard({ tasks: [task('r', 'x', [att(1, 'b', 'fail')]), task('r', 'y', [att(1, 'a', 'fail')]), task('r', 'z', [att(1, 'a', 'fail')])] }, {}, NOW);
  assert.deepEqual(tie.implementers.map(r => r.alias), ['a', 'b']);
});

test('filters: project, difficulty, kind, period and exploratory picks; reviews follow their task', () => {
  const data = { tasks: [
    task('r1', 'a', [att(1, 'sol', 'pass')], { repo: '/w/forge', difficulty: 'high', explored: true }),
    task('r1', 'b', [att(1, 'luna', 'fail')], { repo: '/w/forge', difficulty: null }),
    task('r2', 'solo', [att(1, 'sol', 'fail')], { repo: '/w/pay', kind: 'solo', difficulty: null, created: NOW - 40 * DAY }),
  ], reviews: [
    review('r1', 'a', 'primary', 'opus'), review('r1', 'b', 'primary', 'opus'), review('r1', null, 'final', 'opus'),
    review('r2', 'solo', 'primary', 'gemma'), review('r3', null, 'final', 'gemma'),
  ] };
  const runs = [{ key: 'r3', repo: '/w/other', kind: 'plan', created: NOW - DAY }];
  const pick = f => { const L = MS.leaderboard(data, f, NOW, runs); return [L.totals.tasks, L.totals.reviews, L.implementers.map(r => r.alias).sort().join(',')]; };
  assert.deepEqual(pick({}), [3, 5, 'luna,sol']);
  assert.deepEqual(pick({ project: '/w/forge' }), [2, 3, 'luna,sol']);
  assert.deepEqual(pick({ project: '/w/other' }), [0, 1, ''], 'a run without task rows is placed by the run summaries');
  assert.deepEqual(pick({ difficulty: 'high' }), [1, 1, 'sol']);
  assert.deepEqual(pick({ difficulty: 'none' }), [2, 2, 'luna,sol'], 'a missing difficulty is "none"; final reviews have none');
  assert.deepEqual(pick({ kind: 'solo' }), [1, 1, 'sol']);
  assert.deepEqual(pick({ kind: 'plan' }), [2, 4, 'luna,sol']);
  assert.deepEqual(pick({ days: 30 }), [2, 4, 'luna,sol']);
  assert.deepEqual(pick({ days: 90 }), [3, 5, 'luna,sol']);
  assert.deepEqual(pick({ exploratoryOnly: true }), [1, 1, 'sol']);
  assert.ok(MS.leaderboard(data, { exploratoryOnly: true }, NOW).caveats.some(c => c.startsWith('Final reviews')));
  const byD = MS.leaderboard(data, {}, NOW).byDifficulty;
  assert.deepEqual(Object.keys(byD), ['low', 'medium', 'high', 'none']);
  assert.deepEqual([byD.low.length, byD.high.map(r => r.alias), byD.none.map(r => r.alias).sort()], [0, ['sol'], ['luna', 'sol']]);
});

test('filter values are normalised; activeCount ignores min n', () => {
  assert.deepEqual(MS.normFilter(null), MS.emptyFilter());
  assert.deepEqual(MS.normFilter({ project: 3, difficulty: 'huge', kind: 'x', days: -4, exploratoryOnly: 'yes', minN: 'a' }), MS.emptyFilter());
  assert.deepEqual(MS.normFilter({ days: 7.9, minN: 10.5, exploratoryOnly: true }), { ...MS.emptyFilter(), days: 7, minN: 10, exploratoryOnly: true });
  assert.equal(MS.activeCount({ minN: 30 }), 0);
  assert.equal(MS.activeCount({ project: '/w', days: 7, exploratoryOnly: true }), 3);
});

test('reviewers: counts by kind, verdicts, findings per review, shadow agreement', () => {
  const F = (p1, p2) => ({ P0: 0, P1: p1, P2: p2, P3: 0, unlabelled: 1, plausible: 0, confirmed: p1 + p2, worst: null });
  const data = { tasks: [task('r', 'a', [att(1, 'sol', 'pass')])], reviews: [
    review('r', 'a', 'primary', 'opus', { verdict: 'FAIL', findings: F(1, 2), duration_s: 100 }),
    review('r', 'a', 'primary', 'opus', { verdict: 'PASS', findings: F(0, 0), duration_s: 300, tokens: { input: 10, cached: 0, output: 2 } }),
    review('r', null, 'final', 'opus', { verdict: null, findings: null, duration_s: 200 }),
    review('r', 'a', 'shadow', 'sol', { spec: 'sol:xhigh', verdict: 'FAIL', agreement: 'disagree', primary_alias: 'opus' }),
    review('r', 'a', 'shadow', 'sol', { verdict: 'PASS', agreement: 'agree' }),
    review('r', 'a', 'shadow', 'sol', { verdict: 'PASS', agreement: 'agree' }),
    review('r', 'a', 'shadow', 'sol', { verdict: null, agreement: 'unknown' }),
    { ...review('r', 'a', 'primary', null), spec: 'gemma:high' },                                             // alias from its spec
    { ...review('r', 'a', 'primary', null), spec: null },                                                       // no model at all
  ] };
  const L = MS.leaderboard(data, {}, NOW);
  assert.deepEqual(L.reviewers.map(r => r.alias), ['sol', 'opus', 'gemma'], 'most reviews first');
  const opus = row(L.reviewers, 'opus'), sol = row(L.reviewers, 'sol');
  assert.deepEqual(opus.reviews, { primary: 2, shadow: 0, final: 1, total: 3 });
  assert.deepEqual(opus.verdicts, { PASS: 1, FAIL: 1, other: 1 });
  assert.equal(opus.findings.n, 2);
  assert.equal(opus.findings.P1, 0.5);
  assert.equal(opus.findings.P2, 1);
  assert.equal(opus.findings.unlabelled, 1);
  assert.equal(opus.findingTotals.P2, 2);
  assert.equal(opus.medianDurationS, 200);
  assert.deepEqual(opus.tokens, { input: 10, cached: 0, output: 2, reviews: 1, coverage: 1 / 3 });
  assert.equal(opus.agreement.n, 0);
  assert.equal(opus.agreement.lo, null);
  assert.deepEqual([sol.agreement.k, sol.agreement.n, sol.agreement.unknown], [2, 3, 1]);
  assert.equal(sol.agreement.lo, MS.wilson(2, 3).lo);
  assert.equal(sol.reviews.shadow, 4);
  assert.equal(L.totals.unattributedReviews, 1);
  assert.ok(L.caveats.some(c => c.startsWith('1 review with no recorded model')));
});

test('options: projects named by folder (parent added on a clash), difficulties, kinds, dates', () => {
  const data = { tasks: [
    task('r1', 'a', [], { repo: '/w/forge', difficulty: 'low', created: NOW - 5 * DAY, explored: true }),
    task('r2', 'a', [], { repo: '/x/forge', difficulty: null, created: NOW - DAY }),
    task('r3', 'solo', [att(1, 'sol', 'pass')], { repo: '/w/pay', kind: 'solo', difficulty: null, created: NOW - 2 * DAY }),
  ], reviews: [review('r9', null, 'final', 'opus')] };
  const O = MS.options(data, [{ key: 'r9', repo: '/z/catalog' }, { key: 'r8', repo: '/z/unused' }]);
  assert.deepEqual(O.projects.map(p => [p.name, p.repo, p.tasks]), [['catalog', '/z/catalog', 0], ['forge (w)', '/w/forge', 1], ['forge (x)', '/x/forge', 1], ['pay', '/w/pay', 1]]);
  assert.deepEqual(O.difficulties.map(d => [d.value, d.tasks]), [['low', 1], ['none', 2]]);
  assert.deepEqual(O.kinds, [{ value: 'plan', tasks: 2 }, { value: 'solo', tasks: 1 }]);
  assert.deepEqual(O.range, { first: NOW - 5 * DAY, last: NOW - DAY });
  assert.equal(O.explored, 1);
  assert.deepEqual(O.aliases, ['opus', 'sol']);
});

test('the api.md example aggregates as documented', () => {
  const data = { generated: NOW, runs: 1, truncated: true,
    tasks: [{ run: '3f9a0c1d2e4b', task: 'reader', kind: 'plan', repo: '/x/y', difficulty: 'high', created: 1760619000.0, explored: true, propensity: 0.125,
      final_status: 'MERGED', accepted: true,
      attempts: [{ n: 1, alias: 'luna', spec: 'luna', source: 'execution', outcome: 'fail', reason: null, gate: false, model_s: 297.0, duration_s: 300.0,
        tokens: { input: 120000, cached: 90000, output: 8000 }, timed_out: false }],
      escalations: [{ n: 1, from: 'luna', to: 'sol:xhigh' }] }],
    reviews: [{ run: '3f9a0c1d2e4b', task: 'reader', n: 1, kind: 'primary', alias: 'opus', spec: 'opus', verdict: 'FAIL',
      findings: { P0: 0, P1: 1, P2: 2, P3: 0, unlabelled: 0, plausible: 1, confirmed: 3, worst: 'P1' }, duration_s: 270.0, tokens: null, agreement: null, primary_alias: null }],
    warnings: ['3f9a0c1d2e4b: tasks/x/attempts.tsv line 2: invalid attempt'] };
  const L = MS.leaderboard(data, { exploratoryOnly: true }, NOW);
  const luna = L.implementers[0];
  assert.deepEqual([luna.alias, luna.tasks, luna.first.k, luna.first.n, luna.escalationsAway, luna.humanAccepts, luna.medianModelS], ['luna', 1, 0, 1, 1, 1, 297]);
  assert.equal(luna.tokens.coverage, 1);
  assert.deepEqual([L.reviewers[0].alias, L.reviewers[0].findings.P2], ['opus', 2]);
  assert.equal(L.totals.truncated, true);
  assert.ok(L.caveats.some(c => c.startsWith('Only the newest runs')));
  assert.ok(L.caveats.some(c => c.includes('unreadable entry')));
});

test('empty or malformed input never throws', () => {
  for (const data of [undefined, null, {}, [], 'x', { tasks: null, reviews: 3 }, { tasks: [null, 4, 'x', {}, { run: 'r' }], reviews: [null, {}, { run: 'r' }] },
    { tasks: [{ run: 'r', task: 't', attempts: [null, { n: 'x' }, { alias: 'a', outcome: 'weird', tokens: 'many' }], escalations: [null, { from: null }, 5] }] }]) {
    const L = MS.leaderboard(data, undefined, undefined);
    assert.ok(Array.isArray(L.implementers) && Array.isArray(L.reviewers) && Array.isArray(L.caveats));
    assert.deepEqual(Object.keys(L.byDifficulty), ['low', 'medium', 'high', 'none']);
    const O = MS.options(data, 'not runs');
    assert.ok(Array.isArray(O.projects));
  }
  const L = MS.leaderboard({ tasks: [{ run: 'r', task: 't', attempts: [{ alias: 'a', outcome: 'weird' }] }] }, {}, NOW);
  assert.deepEqual(L.implementers[0].first, { k: 0, n: 0, censored: 1, reasons: { unknown: 1 }, p: null, lo: null, hi: null }, 'an unknown outcome is censored');
  assert.equal(MS.leaderboard({}, {}, NOW).totals.tasks, 0);
});
