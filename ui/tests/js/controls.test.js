'use strict';
// node --test: the pure half of controls.js: model specs, effective values, what can change, the request each
// action becomes (ui/docs/api.md, "Controls (write endpoints)"), and whether a rebuilt inspector keeps an edit.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const C = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'controls.js'));

// `forge control show --json` of a live plan (scripts/forge-control.py `show`)
const show = (over = {}) => ({
  plan: '/w/forge-runs/p', run_id: '20261016-153012-ab12', live: true, fractal: false,
  run: { qa_threshold: 'P2', timeout: 2700, escalate_after: 3, verify_retries: 1, retry_budget: 4, infra_budget: 2 },
  tasks: {
    reader: { status: 'MERGED', dwarf: 'sol:xhigh', qa: 'opus', qa_threshold: null, retry_budget: null, retries_used: 1, pin: null, escalation_stop: false, escalation_next: null },
    page: { status: 'RUNNING', dwarf: 'luna', qa: 'opus', qa_threshold: 'P0', retry_budget: 6, retries_used: 2, pin: 'sol:high:codex', escalation_stop: false, escalation_next: 'sol' },
    graph: { status: 'FAIL', dwarf: 'luna', qa: 'opus', qa_threshold: 'none', retry_budget: null, retries_used: 3, pin: null, escalation_stop: true, escalation_next: null },
    docs: { status: 'PENDING', dwarf: null, qa: null, qa_threshold: null, retry_budget: null, retries_used: 0, pin: null, escalation_stop: false, escalation_next: null },
  },
  log: [], ...over,
});
const idle = () => show({ live: false, run: { qa_threshold: null, timeout: null, escalate_after: 3, verify_retries: 1, retry_budget: null, infra_budget: null } });

test('a pin spec is alias[:effort[:harness]]', () => {
  assert.deepEqual(C.specOf({ alias: 'sol', effort: 'high', harness: 'codex' }), { ok: true, spec: 'sol:high:codex' });
  assert.deepEqual(C.specOf({ alias: 'sol', effort: 'xhigh' }), { ok: true, spec: 'sol:xhigh' });
  assert.deepEqual(C.specOf({ alias: 'sol' }), { ok: true, spec: 'sol' });
  assert.deepEqual(C.specOf({ alias: 'sol', effort: 'default', harness: '' }), { ok: true, spec: 'sol' });
  assert.deepEqual(C.specOf({ alias: 'gpt-6.1', effort: 'default', harness: 'opencode' }), { ok: true, spec: 'gpt-6.1::opencode' }, 'an empty effort keeps the harness in its place');
  assert.deepEqual(C.specOf({ alias: '  sol ', effort: ' low ', harness: ' codex ' }), { ok: true, spec: 'sol:low:codex' }, 'the ends are trimmed');
});

test('a pin spec needs an alias, an effort from the list, and parts without spaces or colons', () => {
  assert.equal(C.specOf({ alias: '' }).ok, false);
  assert.equal(C.specOf({ alias: '   ', effort: 'high' }).ok, false);
  assert.equal(C.specOf({}).ok, false);
  assert.equal(C.specOf({ alias: 'sol', effort: 'ultra' }).ok, false);
  for (const effort of C.EFFORTS) assert.equal(C.specOf({ alias: 'sol', effort }).ok, true, effort);
  assert.deepEqual(C.EFFORTS, ['default', 'low', 'medium', 'high', 'xhigh', 'max']);
  assert.equal(C.specOf({ alias: 'so l' }).ok, false);
  assert.equal(C.specOf({ alias: 'sol:high' }).ok, false, 'the effort has its own field');
  assert.equal(C.specOf({ alias: 'sol', harness: 'co dex' }).ok, false);
  assert.equal(C.specOf({ alias: 'sol', harness: 'codex:x' }).ok, false);
  assert.equal(C.specOf({ alias: 'sol\tx' }).ok, false);
  assert.equal(C.specOf({ alias: 'sol\u0001' }).ok, false);
  assert.equal(C.specOf({ alias: '-sol' }).ok, false, 'the server refuses a leading dash');
  assert.equal(C.specOf({ alias: 'a'.repeat(300) }).ok, false);
  assert.match(C.specOf({ alias: '' }).error, /alias/);
});

test('effective values: inherit, an override and none for the threshold', () => {
  const c = show();
  assert.deepEqual(C.effective(c, 'docs').threshold, { mode: 'inherit', effective: 'P2', from: 'run' });
  assert.deepEqual(C.effective(c, 'page').threshold, { mode: 'P0', effective: 'P0', from: 'task' });
  assert.deepEqual(C.effective(c, 'graph').threshold, { mode: 'none', effective: 'none', from: 'task' }, 'none is an override: no threshold for this task');
  assert.deepEqual(C.effective(idle(), 'docs').threshold, { mode: 'inherit', effective: 'none', from: 'run' }, 'an unset run threshold is none');
  assert.equal(C.effective(c).qa, 'P2');
  assert.equal(C.effective(idle()).qa, 'none');
});

test('effective values: the retry budget, what a task used and the run values', () => {
  const c = show();
  assert.deepEqual(C.effective(c, 'page').retry, { mode: 'set', own: 6, effective: 6, used: 2, from: 'task' });
  assert.deepEqual(C.effective(c, 'graph').retry, { mode: 'inherit', own: null, effective: 4, used: 3, from: 'run' });
  assert.deepEqual(C.effective(idle(), 'graph').retry, { mode: 'inherit', own: null, effective: null, used: 3, from: null }, 'the run budget is only known while live');
  const run = C.effective(c);
  assert.deepEqual([run.timeout, run.timeoutMin, run.escalateAfter, run.verifyRetries, run.retryBudget, run.infraBudget, run.live], [2700, '45', 3, 1, 4, 2, true]);
  assert.equal(C.effective(idle()).timeoutMin, null);
  assert.equal(C.minutesOf(450), '7.5');
  const t = C.effective(c, 'page');
  assert.deepEqual([t.pin, t.next, t.stop, t.status], ['sol:high:codex', 'sol', false, 'RUNNING']);
  assert.equal(C.effective(c, 'graph').stop, true);
  assert.equal(C.effective(c, 'nope').known, false);
});

test('the budgets change only while a run is live', () => {
  for (const key of ['retry-budget', 'infra-retries']) {
    assert.deepEqual(C.canEdit(show(), key), { enabled: true, hidden: false, why: null }, key);
    assert.deepEqual(C.canEdit(idle(), key), { enabled: false, hidden: false, why: 'only while a run is in progress' }, key);
  }
  assert.equal(C.canEdit(idle(), 'retry-budget', 'graph').enabled, false, 'a task budget too');
  assert.equal(C.canEdit(idle(), 'retry-budget', 'graph').why, 'only while a run is in progress');
  for (const key of ['qa-threshold', 'timeout', 'escalate-after', 'verify-retries']) assert.equal(C.canEdit(idle(), key).enabled, true, key);
  assert.equal(C.canEdit(idle(), 'qa-threshold', 'graph').enabled, true, 'a task threshold does not need a live run');
});

test('a merged task has nothing to change and a Fractal plan hides the pin', () => {
  assert.equal(C.canEdit(show(), 'qa-threshold', 'reader').enabled, false);
  assert.match(C.canEdit(show(), 'qa-threshold', 'reader').why, /merged/);
  assert.equal(C.canEdit(show(), 'pin', 'reader').enabled, false);
  assert.equal(C.canEdit(show(), 'pin', 'page').enabled, true);
  assert.deepEqual(C.canEdit(show({ fractal: true }), 'pin', 'page'), { enabled: false, hidden: true, why: 'Fractal plans never apply pins' });
  assert.equal(C.canEdit(show({ fractal: true }), 'qa-threshold', 'page').enabled, true, 'only pins are off under Fractal');
});

test('each action becomes the body ui/docs/api.md shows', () => {
  const body = change => { const p = C.prepare(change); assert.equal(p.ok, true, JSON.stringify(p)); return p.body; };
  assert.deepEqual(body({ kind: 'set', key: 'qa-threshold', input: 'P2' }), { action: 'set', key: 'qa-threshold', value: 'P2' });
  assert.deepEqual(body({ kind: 'set', key: 'qa-threshold', input: 'none' }), { action: 'set', key: 'qa-threshold', value: 'none' });
  assert.deepEqual(body({ kind: 'set', key: 'retry-budget', input: 'inherit', task: 'reader' }), { action: 'set', key: 'retry-budget', value: 'inherit', task: 'reader' });
  assert.deepEqual(body({ kind: 'set', key: 'qa-threshold', input: 'inherit', task: 'graph' }), { action: 'set', key: 'qa-threshold', value: 'inherit', task: 'graph' });
  assert.deepEqual(body({ kind: 'set', key: 'retry-budget', input: '5', task: 'graph' }), { action: 'set', key: 'retry-budget', value: '5', task: 'graph' });
  assert.deepEqual(body({ kind: 'set', key: 'timeout', input: '45' }), { action: 'set', key: 'timeout', value: '2700' }, 'minutes in the UI, seconds to the CLI');
  assert.deepEqual(body({ kind: 'set', key: 'timeout', input: '7.5' }), { action: 'set', key: 'timeout', value: '450' });
  assert.deepEqual(body({ kind: 'set', key: 'timeout', input: '0' }), { action: 'set', key: 'timeout', value: '0' });
  assert.deepEqual(body({ kind: 'set', key: 'escalate-after', input: ' 2 ' }), { action: 'set', key: 'escalate-after', value: '2' });
  assert.deepEqual(body({ kind: 'set', key: 'verify-retries', input: '0' }), { action: 'set', key: 'verify-retries', value: '0' });
  assert.deepEqual(body({ kind: 'set', key: 'infra-retries', input: '3' }), { action: 'set', key: 'infra-retries', value: '3' });
  assert.deepEqual(body({ kind: 'pin', task: 'graph', alias: 'sol', effort: 'high', harness: 'codex' }), { action: 'pin', task: 'graph', spec: 'sol:high:codex' });
  assert.deepEqual(body({ kind: 'unpin', task: 'graph' }), { action: 'unpin', task: 'graph' });
  assert.deepEqual(C.bodyFor('set', { key: 'retry-budget', value: 3, task: 'x' }), { action: 'set', key: 'retry-budget', value: '3', task: 'x' }, 'value is always a string');
});

test('a draft the server would refuse never leaves the page', () => {
  const refused = change => { const p = C.prepare(change); assert.equal(p.ok, false, JSON.stringify(change)); assert.ok(p.error); };
  refused({ kind: 'set', key: 'qa-threshold', input: 'P4' });
  refused({ kind: 'set', key: 'qa-threshold', input: 'inherit' });                  // inherit is for a task
  refused({ kind: 'set', key: 'retry-budget', input: 'inherit' });
  refused({ kind: 'set', key: 'retry-budget', input: '-1', task: 'a' });
  refused({ kind: 'set', key: 'retry-budget', input: '1.5', task: 'a' });
  refused({ kind: 'set', key: 'retry-budget', input: '1234567890', task: 'a' });    // more than 9 digits
  refused({ kind: 'set', key: 'retry-budget', input: '', task: 'a' });
  refused({ kind: 'set', key: 'timeout', input: 'soon' });
  refused({ kind: 'set', key: 'timeout', input: '-5' });
  refused({ kind: 'set', key: 'timeout', input: '45', task: 'a' });                 // the plan's timeout, not a task's
  refused({ kind: 'set', key: 'escalate-after', input: '3', task: 'a' });
  refused({ kind: 'set', key: 'colour', input: '3' });
  refused({ kind: 'pin', task: 'a', alias: '' });
  refused({ kind: 'pin', alias: 'sol' });
  refused({ kind: 'unpin' });
  refused({ kind: 'delete', task: 'a' });
});

test('the confirm names what changes and when it takes effect', () => {
  assert.equal(C.prepare({ kind: 'set', key: 'qa-threshold', input: 'P2', task: 'x' }).confirm, 'Set QA threshold for task `x` to P2? Takes effect at its next attempt');
  assert.equal(C.prepare({ kind: 'set', key: 'qa-threshold', input: 'P1' }).confirm, "Set QA threshold to P1? Takes effect at each task's next attempt");
  assert.equal(C.prepare({ kind: 'set', key: 'qa-threshold', input: 'none', task: 'x' }).confirm, 'Set QA threshold for task `x` to none (no threshold for this task)? Takes effect at its next attempt');
  assert.equal(C.prepare({ kind: 'set', key: 'timeout', input: '45' }).confirm, "Set Timeout to 45 min? Takes effect at each task's next attempt");
  assert.equal(C.prepare({ kind: 'set', key: 'retry-budget', input: '2', task: 'x' }).confirm, 'Set Retry budget for task `x` to 2? Takes effect at the next retry decision');
  assert.equal(C.prepare({ kind: 'set', key: 'verify-retries', input: '2' }).confirm, 'Set Verify retries to 2? Takes effect at the next verification');
  assert.equal(C.prepare({ kind: 'pin', task: 'x', alias: 'sol', effort: 'high' }).confirm, "Pin task `x`'s next retry to `sol:high`? Takes effect at the next full retry, not a QA-only resume");
  assert.match(C.prepare({ kind: 'unpin', task: 'x' }).confirm, /^Remove the pin on task `x`\?/);
});

test('the sentence after a change comes from the CLI answer', () => {
  assert.equal(C.savedText({ ok: true, changed: { key: 'qa-threshold', value: 'P1', previous: 'P2' }, applies: "at the task's next attempt" }), "Saved. Applies at the task's next attempt.");
  assert.equal(C.savedText({ ok: true, changed: { key: 'qa-threshold', value: 'P2', previous: 'P2' }, applies: 'x' }), 'Nothing changed: it was already so.');
  assert.equal(C.savedText({ ok: true, changed: { key: 'pin', value: null, previous: null } }), 'Nothing changed: it was already so.');
  assert.equal(C.savedText({ ok: true }), 'Saved.');
  assert.equal(C.savedText(null), 'Saved.');
});

test('an edit in progress is kept for the same run and task, and reset by moving to another', () => {
  const task = C.scopeOf('0123456789ab', 'graph');
  assert.equal(C.keepEditing({ scope: task, editing: true }, { scope: C.scopeOf('0123456789ab', 'graph') }), true, 'a poll rebuilds the same section');
  assert.equal(C.keepEditing({ scope: task, editing: true }, { scope: C.scopeOf('0123456789ab', 'page') }), false, 'another task');
  assert.equal(C.keepEditing({ scope: task, editing: true }, { scope: C.scopeOf('0123456789ab') }), false, 'back to the run');
  assert.equal(C.keepEditing({ scope: task, editing: true }, { scope: C.scopeOf('ba9876543210', 'graph') }), false, 'another run, the same task id');
  assert.equal(C.keepEditing({ scope: C.scopeOf('0123456789ab'), editing: true }, { scope: C.scopeOf('0123456789ab') }), true);
  assert.equal(C.keepEditing({ scope: task, editing: false }, { scope: task }), false, 'nothing being edited: the fresh section wins');
  assert.equal(C.keepEditing(null, { scope: task }), false);
  assert.equal(C.keepEditing({ scope: task, editing: true }, null), false);
  assert.notEqual(C.scopeOf('k', 'a'), C.scopeOf('k'));
  assert.notEqual(C.scopeOf('k', 'run'), C.scopeOf('k'), 'a task named run is not the run');
});

test('a section is being edited while it has focus, an unsaved value, an open confirm or a request out', () => {
  assert.equal(C.isEditing({ focus: true }), true);
  assert.equal(C.isEditing({ dirty: true }), true);
  assert.equal(C.isEditing({ confirm: true }), true);
  assert.equal(C.isEditing({ busy: true }), true);
  assert.equal(C.isEditing({ focus: false, dirty: false, confirm: false, busy: false }), false);
  assert.equal(C.isEditing(null), false);
});

test('the change log reads who, what, previous to new, and when', () => {
  assert.deepEqual(C.logLine({ at: 1760620000.5, actor: 'ui', action: 'set', task: 'graph', key: 'qa-threshold', value: 'P1', previous: 'P2' }),
    { who: 'ui', what: 'QA threshold · graph', from: 'P2', to: 'P1', at: 1760620000.5 });
  assert.deepEqual(C.logLine({ at: 1, actor: 'cli', action: 'set', task: null, key: 'timeout', value: '2700', previous: '1800' }),
    { who: 'cli', what: 'Timeout', from: '30 min', to: '45 min', at: 1 });
  assert.deepEqual(C.logLine({ at: 2, actor: 'ui', action: 'set', task: null, key: 'retry-budget', value: '3', previous: null }).from, 'unset');
  assert.deepEqual(C.logLine({ at: 3, actor: 'ui', action: 'unpin', task: 'graph', key: 'pin', value: null, previous: 'sol:high' }),
    { who: 'ui', what: 'Pin · graph', from: 'sol:high', to: 'no pin', at: 3 });
  assert.deepEqual(C.logLine({ at: 4, actor: 'ui', action: 'pin', task: 'graph', key: 'pin', value: 'sol', previous: null }).from, 'no pin');
  assert.deepEqual(C.logLine(null), { who: '?', what: '?', from: 'unset', to: 'unset', at: null });
  assert.equal(C.agoText(100, 130), 'just now');
  assert.equal(C.agoText(100, 400), '5m ago');
  assert.equal(C.agoText(0, 7300), '2h ago');
  assert.equal(C.agoText(0, 200000), '2d ago');
  assert.equal(C.agoText(null, 5), '—');
});

test('the pin suggestions are the implementer aliases seen in runs', () => {
  const models = { tasks: [{ attempts: [{ alias: 'sol' }, { alias: 'luna' }, { alias: null }] }, { attempts: [{ alias: 'sol' }] }, { attempts: [] }, null], reviews: [{ alias: 'opus' }] };
  assert.deepEqual(C.aliasesOf(models), ['luna', 'sol']);
  assert.deepEqual(C.aliasesOf(null), []);
  assert.deepEqual(C.aliasesOf({ tasks: 'x' }), []);
});
