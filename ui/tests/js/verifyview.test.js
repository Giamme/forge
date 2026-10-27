'use strict';
// node --test: the verification view's pure logic (verifyview.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const V = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'verifyview.js'));

const T = 1760601300;
const lane = (name, state, extra = {}) => ({ name, state, command: 'make ' + name, group: null, first: false, retries: null,
  started: T, ended: state === 'running' ? null : T + 30, duration_s: 30, idle_s: null, runs: [{ k: 1, kind: 'initial' }],
  step: null, last_line: null, last_output_at: null, tests: null, ...extra });
const failing = [
  { lane: 'e2e', name: 'pay.card', message: 'timed out', excerpt: 'tb', known: true, log_line: 12 },
  { lane: 'unit', name: 'calc.zero', message: 'boom', excerpt: '', known: false, log_line: null },
  { lane: 'e2e', name: 'pay.refund', message: '500', excerpt: 'x', known: false, log_line: 40 }];

test('running versus finished, and the status a running verification reports', () => {
  const running = V.model({ state: 'running', status: 'RUNNING', mode: 'single', live: { started: T, step: null, last_line: 'ok', idle_s: 3 } }, T + 75);
  assert.equal(running.running, true);
  assert.equal(running.failed, false);
  assert.deepEqual(running.live, { elapsed: '1m15s', step: null, stepElapsed: null, last_line: 'ok', idle: null });
  const failed = V.model({ state: 'done', status: 'FAIL', mode: 'lanes', lanes: [lane('unit', 'fail')], failing: [] }, T + 99);
  assert.deepEqual([failed.running, failed.failed, failed.passed, failed.live], [false, true, false, null]);
  const passed = V.model({ state: 'done', status: 'PASS', mode: 'lanes', lanes: [lane('unit', 'flaky'), lane('lint', 'known')] }, T);
  assert.deepEqual([passed.passed, passed.flaky, passed.known], [true, ['unit'], ['lint']]);
  // a lane that failed fails the view even before verification.status is written
  assert.equal(V.model({ state: 'done', status: null, mode: 'lanes', lanes: [lane('unit', 'fail')] }, T).failed, true);
  assert.equal(V.model(null), null);
});

test('silence is flagged past five minutes, not at them', () => {
  assert.equal(V.idleWarning(300), null);
  assert.equal(V.idleWarning(301), 'no output for 5 min');
  assert.equal(V.idleWarning(3600), 'no output for 60 min');
  assert.equal(V.idleWarning(null), null);
  const m = V.model({ state: 'running', mode: 'lanes', live: { started: T, idle_s: 400 },
    lanes: [lane('e2e', 'running', { idle_s: 400 }), lane('unit', 'pass', { idle_s: 900 })] }, T + 500);
  assert.equal(m.live.idle, 'no output for 6 min');
  assert.deepEqual(m.lanes.map(l => l.idle), ['no output for 6 min', null]);     // a finished lane is not idle
});

test('failing tests group by lane in order, with new and known', () => {
  const groups = V.groupFailing(failing);
  assert.deepEqual(groups.map(g => [g.lane, g.fresh, g.known]), [['e2e', 1, 1], ['unit', 1, 0]]);
  assert.deepEqual(groups[0].entries.map(e => [e.name, e.badge, e.log_line]), [['pay.card', 'known', 12], ['pay.refund', 'new', 40]]);
  assert.deepEqual(V.groupFailing([null, 'x', { lane: 'a', name: 'n', log_line: 0 }]).map(g => g.entries[0].log_line), [null]);
});

test('lane rows: time, reruns, counts, the step and last line while running, labels', () => {
  const done = V.laneRow(lane('e2e', 'fail', { group: 'browser', first: true, retries: 2, duration_s: 75,
    runs: [{ k: 1, kind: 'initial' }, { k: 2, kind: 'rerun' }], tests: { pass: 30, fail: 1, error: 1, skip: 1 },
    step: { name: 'old' }, last_line: 'old line' }), T + 999);
  assert.deepEqual([done.time, done.runs, done.counts, done.step, done.last_line, done.labels, done.chip],
    ['1m15s', 'rerun 1 of 2', '30 passed · 2 failed · 1 skipped', null, null, ['first', 'group browser'], 'fail']);
  const live = V.laneRow(lane('e2e', 'running', { step: { name: 'browser', started: T }, last_line: 'checkout 12/33', duration_s: 1 }), T + 42);
  assert.deepEqual([live.time, live.runs, live.counts, live.step, live.last_line], ['42s', '', null, 'browser', 'checkout 12/33']);
  assert.equal(V.laneRow(lane('u', 'flaky', { runs: [{ kind: 'initial' }, { kind: 'rerun' }] })).runs, 'rerun 1');
  assert.equal(V.laneRow(lane('u', 'pass', { tests: { pass: 4 } })).counts, '4 passed · 0 failed');
  assert.deepEqual(['flaky', 'known', 'skipped', 'waiting', 'bogus'].map(s => V.laneRow(lane('u', s)).chip),
    ['flaky', 'known', 'none', 'pending', 'unknown']);
});

test('single mode: the live step, and the step a finished command ended in', () => {
  const running = V.model({ state: 'running', mode: 'single', live: { started: T, step: { name: 'test', started: null }, last_line: 'x' } }, T + 5);
  assert.deepEqual([running.live.step, running.live.stepElapsed, running.endStep], ['test', null, null]);
  const tail = '::forge step name=build state=start\n::forge step name=build state=end exit=0\n'
    + '::forge step name=test state=start\n::forge step name=unit state=start\n::forge step name=unit state=end exit=1\nFAILED\n';
  assert.equal(V.model({ state: 'done', status: 'FAIL', mode: 'single', log_tail: tail }, T).endStep, 'test');
  assert.equal(V.model({ state: 'done', status: 'PASS', mode: 'single', log_tail: 'all good\n' }, T).endStep, null);
  assert.equal(V.model({ state: 'done', status: 'FAIL', mode: 'single', log_tail: null }, T).endStep, null);
  assert.equal(V.openStep('\x1b[2m::forge step name=a state=start\x1b[0m\n'), 'a');
});

test('the live tail follows the lane that printed last unless one was chosen', () => {
  const v = { lanes: [lane('unit', 'pass'), lane('e2e', 'running', { last_output_at: T + 9 }), lane('lint', 'running', { last_output_at: T + 20 })] };
  assert.equal(V.tailLane(v, null), 'lint');
  assert.equal(V.tailLane(v, 'unit'), 'unit');
  assert.equal(V.tailLane(v, 'gone'), 'lint');
  assert.equal(V.tailLane({ lanes: [lane('a', 'pass'), lane('b', 'fail')] }, null), 'b');
  assert.equal(V.tailLane({ lanes: null }, null), null);
});

test('log paths and tail numbering', () => {
  assert.equal(V.logPath('abc', null), '/api/runs/abc/verification/log');
  assert.equal(V.logPath('abc', 'e2e', 2), '/api/runs/abc/verification/lanes/e2e/runs/2/log');
  assert.equal(V.linesPath('abc', 'e2e', 40), '/api/runs/abc/verification/lanes/e2e/log/lines/30');
  assert.equal(V.linesPath('abc', 'e2e', 3), '/api/runs/abc/verification/lanes/e2e/log/lines/1');
  assert.deepEqual(V.tailLines({ tail: 'a\nb\n', tail_first_line: 7 }), [{ n: 7, text: 'a' }, { n: 8, text: 'b' }]);
  assert.deepEqual(V.tailLines({ tail: 'a', tail_first_line: null }), [{ n: null, text: 'a' }]);
  assert.deepEqual(V.tailLines({ tail: '' }), []);
});
