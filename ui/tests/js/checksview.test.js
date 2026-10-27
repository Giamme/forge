'use strict';
// node --test: where a task stands and the checks its agents ran (checksview.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const C = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'checksview.js'));

const T = 1760601300;

test('a running task says who is at work and the first line of the call it is making', () => {
  const task = { now: { role: 'qa', execution: 'qa-1', attempt: 2, phase: 'model', phase_started: T, last_output_at: T + 40,
    call: { kind: 'command', command: '\ncd /repo && python3 -m unittest tests.test_reader\necho done', tool: 'Bash', started: T + 30, check: 'test' } } };
  assert.deepEqual(C.nowModel(task), { who: 'reviewer', role: 'qa', execution: 'qa-1', attempt: 2, phase: 'model', phaseSince: T,
    call: { text: 'cd /repo && python3 -m unittest tests.test_reader', command: task.now.call.command, tool: 'Bash', check: 'test', since: T + 30 },
    quietSince: T + 40 });
  assert.equal(C.nowModel({ now: { role: 'dwarf', call: { kind: 'tool', tool: 'WebSearch' } } }).call.text, 'WebSearch');   // no command: the tool
  assert.equal(C.nowModel({ now: { role: 'dwarf', call: null } }).call, null);
  assert.equal(C.nowModel({ now: null }), null);
});

test('a quiet log is flagged past five minutes, not at them', () => {
  assert.equal(C.quietText(T, T + 300), null);
  assert.equal(C.quietText(T, T + 301), 'quiet 5 min');
  assert.equal(C.quietText(T, T + 3600), 'quiet 60 min');
  assert.equal(C.quietText(null, T), null);
  assert.equal(C.untilText(T + 130, T), 'resumes in 2m10s');
  assert.equal(C.untilText(T - 1, T), 'resuming');
  assert.equal(C.untilText(null, T), null);
});

test('each reason a task waits reads as a sentence with the tasks it names', () => {
  const cases = [
    [{ kind: 'deps', on: ['t1', 't5'], stopped: ['t5'] }, 'waits on', [{ id: 't1', stopped: false }, { id: 't5', stopped: true }]],
    [{ kind: 'overlap', on: ['t1'] }, 'shares files with', [{ id: 't1', stopped: false }]],
    [{ kind: 'slot', running: 2, max_parallel: 2 }, 'no free slot: 2 of 2 running', []],
    [{ kind: 'drained' }, 'drained: nothing new starts until the run resumes', []],
    [{ kind: 'stopping', mode: 'now' }, 'the run is stopping now', []],
    [{ kind: 'next' }, "starts at the scheduler's next turn", []],
    [{ kind: 'infra', scope: 'model:codex/gpt-6', class: 'rate_limit', harness: 'codex', model: 'gpt-6', until: T + 90 }, 'paused: model gpt-6 on codex · rate limit', []],
    [{ kind: 'infra', scope: 'all', class: 'network' }, 'paused: every model · network', []],
    [{ kind: 'escalation', attempts: 3, model: 'sol:xhigh', split: 'Split it.' }, 'stopped by the retry policy after 3 attempts on sol:xhigh', []],
  ];
  for (const [w, text, tasks] of cases) {
    const m = C.waitingModel(w);
    assert.equal(m.text, text, w.kind);
    assert.deepEqual(m.tasks, tasks, w.kind);
  }
  assert.equal(C.waitingModel(cases[6][0]).until, T + 90);
  const used = C.waitingModel({ kind: 'infra', scope: 'all', until: T + 90, exhausted: true });
  assert.deepEqual([used.until, used.exhausted], [null, true]);                                 // no countdown once its waits are spent
  assert.equal(C.waitingModel(cases[8][0]).split, 'Split it.');
  assert.equal(C.waitingModel({ kind: 'something new' }), null);
  assert.equal(C.waitingModel(null), null);
});

test('the Now column sorts running first, then what starts soonest, and what is held last', () => {
  const tasks = [{ waiting: { kind: 'escalation' } }, { waiting: { kind: 'deps' } }, { now: {} }, { waiting: { kind: 'next' } }, {}];
  assert.deepEqual(tasks.map(C.nowRank), [6, 3, 0, 1, null]);
});

test('a check reads as a lamp, its kind, its exit, its time and its counts', () => {
  const ran = { index: 4, command: 'python3 -m unittest\n-v', kind: 'test', exit: 1, failed: true, running: false, started: T, ended: T + 42.5,
    duration_s: 42.5, counts: { passed: 8, failed: 1, skipped: 0 }, tail: 'FAILED (failures=1)' };
  assert.deepEqual(C.checkRow(ran, T + 100), { index: 4, state: 'fail', kind: 'tests', command: 'python3 -m unittest', full: 'python3 -m unittest\n-v',
    exit: 'exit 1', time: '43s', since: null, counts: '8 passed · 1 failed', tail: 'FAILED (failures=1)' });
  const running = C.checkRow({ index: 6, command: 'bash tests/check.sh', kind: 'test', running: true, started: T, counts: null }, T + 75);
  assert.deepEqual([running.state, running.time, running.since, running.exit], ['running', '1m15s', T, null]);
  assert.equal(C.checkRow({ kind: 'lint', exit: 0, failed: false }, T).state, 'pass');
  assert.equal(C.checkRow({ kind: 'lint', exit: null, failed: false }, T).state, 'unknown');          // no exit code was logged
  assert.equal(C.checkRow({ kind: 'typecheck', exit: 2, failed: false }, T).state, 'fail');
  assert.equal(C.countsText({ errors: 3, warnings: 1 }), '3 errors · 1 warning');
  assert.equal(C.countsText({ errors: 0 }), '0 errors');
  assert.equal(C.countsText({ passed: 5, failed: 0, skipped: 2 }), '5 passed · 2 skipped');
  assert.equal(C.countsText(null), null);
});

test('an attempt names the acceptance-criteria checks it did not run as written', () => {
  const cov = C.coverageModel([{ command: 'npm test', status: 'ran' }, { command: 'npm run lint', status: 'missing' },
    { command: 'python3 -m unittest tests.test_x', status: 'similar' }]);
  assert.deepEqual(cov, { total: 3, ran: 1, similar: ['python3 -m unittest tests.test_x'], missing: ['npm run lint'],
    text: '1 of 3 acceptance-criteria checks ran as written' });
  assert.equal(C.coverageModel([]), null);
});

test('the banner names only the problems several tasks met', () => {
  const env = { problems: [{ problem: 'docker-pools', tasks: ['t1', 't2'] }, { problem: 'port', tasks: ['t3'] }] };
  assert.deepEqual(C.bannerProblems(env).map(p => p.problem), ['docker-pools']);
  assert.deepEqual(C.bannerProblems(null), []);
});

test('the scheduler fields count each reason tasks wait, and only while a scheduler runs', () => {
  const run = { scheduler: { live: true, drained: false, stopping: null, max_parallel: 3, pauses: [] },
    tasks: [{ status: 'RUNNING' }, { status: 'PENDING', waiting: { kind: 'deps' } }, { status: 'PENDING', waiting: { kind: 'deps' } },
      { status: 'INFRA', waiting: { kind: 'infra' } }] };
  assert.deepEqual(C.schedulerModel(run), [['Scheduler', 'dispatching'], ['Running', '1 of 3 slots'], ['Waiting', '2 on dependencies · 1 paused']]);
  assert.equal(C.schedulerModel({ ...run, scheduler: { ...run.scheduler, drained: true } })[0][1], 'drained: nothing new starts');
  assert.equal(C.schedulerModel({ ...run, scheduler: { live: false } }), null);
});
