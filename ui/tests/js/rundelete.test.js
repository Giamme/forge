'use strict';
// node --test: the delete-run dialog's pure logic (rundelete.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const D = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'rundelete.js'));

const step = (name, ok = true, detail = '') => ({ step: name, ok, detail });
const wt = (p, extra = {}) => ({ path: p, branch: 'forge/r/t', kind: 'run', dirty: false, removed: false, note: '', ...extra });
const br = (name, status = 'merged', extra = {}) => ({ name, sha: 'abc', status, checked_out: false, deleted: false, note: '', ...extra });
const dry = (extra = {}) => ({ run_dir: '/r', run_id: 'r1', dry_run: true, ok: true, steps: [step('preserve')], archive: '/a.tar.gz', bytes_freed: 0,
  branches: [], worktrees: [], size_bytes: 2048, archive_bytes: 0, archive_estimate_bytes: 512, ...extra });

test('request body: every option combination, archive on unless off', () => {
  for (const dryRun of [true, false]) for (const archive of [true, false]) for (const worktrees of [true, false]) for (const branches of [true, false]) {
    assert.deepEqual(D.deleteBody({ dryRun, archive, worktrees, branches }), { dry_run: dryRun, archive, worktrees, branches });
  }
  assert.deepEqual(D.deleteBody({}), { dry_run: false, archive: true, worktrees: false, branches: false });
  assert.deepEqual(D.deleteBody(), { dry_run: false, archive: true, worktrees: false, branches: false });
  const s = D.initial('r1');
  assert.deepEqual(D.bodyOf(s, true), { dry_run: true, archive: true, worktrees: false, branches: false });
  assert.deepEqual(D.bodyOf(D.withOption(D.withOption(s, 'archive', false), 'branches', true), false), { dry_run: false, archive: false, worktrees: false, branches: true });
});

test('defaults: archive on, worktrees and branches off, nothing typed', () => {
  const s = D.initial('r1');
  assert.deepEqual([s.archive, s.worktrees, s.branches, s.typed], [true, false, false, '']);
});

test('confirm gate: the exact run id, no trimming, no case folding', () => {
  assert.equal(D.confirmOk('r1', 'r1'), true);
  for (const typed of ['', 'r', 'r1 ', ' r1', 'R1', 'r11', 'r1\n']) assert.equal(D.confirmOk(typed, 'r1'), false, JSON.stringify(typed));
  assert.equal(D.confirmOk('', ''), false);
  assert.equal(D.confirmOk(undefined, 'r1'), false);
  assert.equal(D.confirmOk('r1', undefined), false);
});

test('Delete needs the id AND a dry run, and not a running request', () => {
  const typed = D.withTyped(D.initial('r1'), 'r1');
  assert.equal(D.canDelete(typed, { result: dry() }, false), true);
  assert.equal(D.canDelete(typed, null, false), false);
  assert.equal(D.canDelete(typed, { result: dry() }, true), false);
  assert.equal(D.canDelete(D.withTyped(typed, 'r'), { result: dry() }, false), false);
});

test('changing an option clears the typed id; setting the same value does not', () => {
  const typed = D.withTyped(D.initial('r1'), 'r1');
  for (const [name, value] of [['archive', false], ['worktrees', true], ['branches', true]]) {
    const next = D.withOption(typed, name, value);
    assert.equal(next.typed, '', name);
    assert.equal(next[name], value);
    assert.equal(D.confirmOk(next.typed, next.runId), false);
  }
  assert.equal(D.withOption(typed, 'archive', true).typed, 'r1');
  assert.equal(D.withOption(typed, 'nonsense', true), typed);
});

test('warnings: archive off, UNMERGED branches by name, dirty worktrees', () => {
  const r = dry({ branches: [br('forge/r/a'), br('forge/r/b', 'UNMERGED'), br('forge/r/c', 'UNMERGED')], worktrees: [wt('/w/1'), wt('/w/2', { dirty: true })] });
  const off = D.warningsOf(r, D.initial('r1'));
  assert.deepEqual(off.map(w => w.id), ['unmerged', 'dirty']);
  assert.deepEqual(off.map(w => w.tone), ['note', 'note']);
  assert.deepEqual(off[0].names, ['forge/r/b', 'forge/r/c']);
  assert.match(off[0].text, /forge\/r\/b, forge\/r\/c/);
  assert.deepEqual(off[1].names, ['/w/2']);
  const armed = D.warningsOf(r, { archive: false, worktrees: true, branches: true });
  assert.deepEqual(armed.map(w => [w.id, w.tone]), [['no-archive', 'warn'], ['unmerged', 'warn'], ['dirty', 'warn']]);
  assert.match(armed[0].text, /lost for good/);
  assert.match(armed[2].text, /run directory is kept/);
});

test('warnings: nothing for merged branches, clean, foreign or checked-out ones', () => {
  const r = dry({ branches: [br('forge/r/a'), br('forge/r/b', 'UNMERGED', { checked_out: true })], worktrees: [wt('/w/1'), wt('/w/2', { kind: 'foreign', dirty: true })] });
  assert.deepEqual(D.warningsOf(r, { archive: true, worktrees: true, branches: true }), []);
  assert.deepEqual(D.warningsOf(null, D.initial('r1')), []);
});

test('plates: foreign is never touched, dirty is not removed, UNMERGED stands out', () => {
  assert.deepEqual(D.worktreePlate(wt('/w')), { id: 'run', label: 'run', note: '' });
  assert.equal(D.worktreePlate(wt('/w', { kind: 'foreign', dirty: true })).id, 'foreign');
  assert.match(D.worktreePlate(wt('/w', { kind: 'foreign' })).note, /never touched/);
  assert.equal(D.worktreePlate(wt('/w', { dirty: true })).id, 'dirty');
  assert.match(D.worktreePlate(wt('/w', { dirty: true })).note, /will not be removed/);
  assert.equal(D.branchPlate(br('a')).label, 'merged');
  assert.equal(D.branchPlate(br('a', 'UNMERGED')).label, 'UNMERGED');
});

test('outcome: a 200 is a full success', () => {
  const result = dry({ dry_run: false, steps: [step('preserve'), step('remove')] });
  const o = D.outcomeOf({ ok: true, status: 200, data: result, result: null });
  assert.equal(o.kind, 'success');
  assert.deepEqual(o.steps.map(s => s.name), ['preserve', 'remove']);
  assert.deepEqual(o.failed, []);
});

test('outcome: a 500 with a result is a partial failure, with its failed steps and the CLI words', () => {
  const result = dry({ dry_run: false, ok: false, steps: [step('preserve'), step('worktrees', false, 'dirty'), step('unregister', false, 'registry needs repair: rerun')] });
  const o = D.outcomeOf({ ok: false, status: 500, error: 'failed', detail: 'forge runs rm: not completed (exit 4)', data: { result }, result });
  assert.equal(o.kind, 'partial');
  assert.deepEqual(o.failed.map(s => s.name), ['worktrees', 'unregister']);
  assert.equal(o.steps.length, 3);
  assert.equal(o.guidance, 'registry needs repair: rerun');
  assert.equal(o.steps[2].detail, 'registry needs repair: rerun');
});

test('outcome: 409 is plain, other failures are errors for ForgeUI.outcome', () => {
  assert.deepEqual(D.outcomeOf({ ok: false, status: 409, error: 'refused', detail: 'the run is active' }), { kind: 'busy', text: 'the run is active' });
  assert.equal(D.outcomeOf({ ok: false, status: 409, error: 'refused', detail: null }).text, 'the run is active');
  assert.equal(D.outcomeOf({ ok: false, status: 409, error: 'refused', detail: 'the forge CLI exited 3' }).text, 'the run is active');
  assert.match(D.outcomeOf({ ok: false, status: 409, error: 'busy' }).text, /try again/);
  for (const res of [{ ok: false, status: 500, error: 'failed', detail: 'boom', result: null }, { ok: false, status: 504, error: 'timeout' },
    { ok: false, status: 0, error: 'network' }, { ok: false, status: 400, error: 'invalid', detail: 'bad' }]) {
    assert.equal(D.outcomeOf(res).kind, 'error', JSON.stringify(res));
  }
  assert.equal(D.outcomeOf(null).kind, 'error');
});

// The server never has stderr to quote for `runs rm --json`: detail is always "the forge CLI exited N" and the
// CLI's words are only in result.steps.
test('refusals carry the CLI reason from result.steps, not the generic exit text', () => {
  const lock = dry({ ok: false, steps: [step('check'), step('lock', false, 'scheduler.live names a live process (pid 7): the run is active; stop it first')] });
  const busy = { ok: false, status: 409, error: 'refused', detail: 'the forge CLI exited 3', result: lock };
  assert.match(D.outcomeOf(busy).text, /the run is active; stop it first/);
  assert.match(D.previewOf(busy).text, /pid 7/);
  const refused = { ok: false, status: 400, error: 'invalid', detail: 'the forge CLI exited 2', result: dry({ ok: false, steps: [step('check', false, 'git for-each-ref failed')] }) };
  const o = D.outcomeOf(refused);
  assert.equal(o.kind, 'error');
  assert.equal(o.text, 'git for-each-ref failed');
  assert.equal(o.refused, true);
  assert.equal(D.previewOf(refused).text, 'git for-each-ref failed');
  assert.equal(D.reasonOf({ detail: 'the forge CLI exited 4' }), '');
  assert.equal(D.reasonOf({ detail: 'real words' }), 'real words');
});

test('guidance is the failed steps that say what to do, not the generic exit text', () => {
  const result = dry({ ok: false, steps: [step('remove', false, 'permission denied'), step('unregister', false, 'removed, registry needs repair: rerun `forge runs rm /r`')] });
  const o = D.outcomeOf({ ok: false, status: 500, error: 'failed', detail: 'the forge CLI exited 4', result });
  assert.equal(o.guidance, 'removed, registry needs repair: rerun `forge runs rm /r`');
});

test('click outside: only beyond the dialog box, not in its padding', () => {
  const box = { left: 100, right: 400, top: 50, bottom: 350 };
  assert.equal(D.outsideBox(box, 110, 60), false);
  assert.equal(D.outsideBox(box, 99, 60), true);
  assert.equal(D.outsideBox(box, 200, 351), true);
  assert.equal(D.outsideBox(null, 0, 0), false);
});

test('a failed step name may come as name (older shape)', () => {
  assert.deepEqual(D.stepsOf({ steps: [{ name: 'archive', ok: false }, 'junk'] }), [{ name: 'archive', ok: false, detail: '' }]);
});

test('dry-run answers: a preview, a preview that would not complete, busy, error', () => {
  assert.deepEqual(D.previewOf({ ok: true, status: 200, data: dry() }).completes, true);
  const bad = D.previewOf({ ok: false, status: 500, detail: 'not completed', result: dry({ ok: false }) });
  assert.equal(bad.kind, 'preview');
  assert.equal(bad.completes, false);
  assert.equal(bad.note, 'not completed');
  assert.equal(D.previewOf({ ok: true, status: 200, data: dry({ ok: false }) }).completes, false);
  assert.equal(D.previewOf({ ok: false, status: 409, error: 'refused', detail: 'active' }).kind, 'busy');
  assert.equal(D.previewOf({ ok: false, status: 404, error: 'not found' }).kind, 'error');
});

test('sizes and the request path', () => {
  assert.equal(D.fmtBytes(0), '0 B');
  assert.equal(D.fmtBytes(1536), '1.5 KB');
  assert.equal(D.fmtBytes(5 * 1024 * 1024), '5.0 MB');
  assert.equal(D.fmtBytes(null), 'unknown');
  assert.equal(D.fmtBytes(-1), 'unknown');
  assert.equal(D.deletePath('0123456789ab'), '/api/runs/0123456789ab/delete');
});
