'use strict';
// node --test: the settings view's pure logic (settings.js). No browser.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const S = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'settings.js'));

const run = (key, repo, updated, extra = {}) => ({ key, run_id: key, repo, updated, goal: 'goal of ' + key, ...extra });

test('config request bodies: a set carries value, an unset carries unset:true, never both', () => {
  assert.deepEqual(S.configBody('r1', 'repo.verify', 'make test'), { run: 'r1', key: 'repo.verify', value: 'make test' });
  assert.deepEqual(S.configBody('r1', 'repo.verify', 'make test', false), { run: 'r1', key: 'repo.verify', value: 'make test' });
  assert.deepEqual(S.configBody('r1', 'repo.setup', null, true), { run: 'r1', key: 'repo.setup', unset: true });
  assert.deepEqual(S.configBody('r1', 'repo.setup', 'ignored', true), { run: 'r1', key: 'repo.setup', unset: true });
  assert.deepEqual(S.configBody('r1', 'jev.enabled', true), { run: 'r1', key: 'jev.enabled', value: 'true' });
  assert.deepEqual(S.configBody('r1', 'jev.enabled', false), { run: 'r1', key: 'jev.enabled', value: 'false' });
  assert.equal(S.repoKey('verify-subset'), 'repo.verify-subset');
  assert.deepEqual(S.FILES.map(S.repoKey), ['repo.verify', 'repo.setup', 'repo.verify-subset']);
});

test('the confirm names the exact file, whatever trailing slashes the repo has', () => {
  assert.equal(S.targetPath('/home/a/proj', 'verify'), '/home/a/proj/.forge/verify');
  assert.equal(S.targetPath('/home/a/proj/', 'verify-subset'), '/home/a/proj/.forge/verify-subset');
});

test('verify-subset must contain {files}', () => {
  assert.equal(S.subsetOk('pytest {files}'), true);
  assert.equal(S.subsetOk('pytest'), false);
  assert.equal(S.subsetOk('pytest {file}'), false);
  assert.equal(S.subsetOk(null), false);
  assert.match(S.valueProblem('verify-subset', 'pytest -q'), /\{files\}/);
  assert.equal(S.valueProblem('verify-subset', 'pytest -q {files}'), null);
  assert.equal(S.valueProblem('verify', 'pytest -q'), null);
});

test('value checks follow the server: not empty, 4096 at most, no control characters, no leading dash', () => {
  assert.match(S.valueProblem('verify', ''), /Empty/);
  assert.match(S.valueProblem('verify', ' \n\t '), /Empty/);
  assert.match(S.valueProblem('verify', undefined), /Empty/);
  assert.equal(S.valueProblem('verify', 'a'.repeat(4096)), null);
  assert.match(S.valueProblem('verify', 'a'.repeat(4097)), /Too long/);
  assert.equal(S.valueProblem('setup', 'a\n\tb'), null);
  assert.match(S.valueProblem('setup', 'a\u0000b'), /Control/);
  assert.match(S.valueProblem('setup', 'a\u001bb'), /Control/);
  assert.match(S.valueProblem('setup', 'a\u007fb'), /Control/);
  assert.match(S.valueProblem('setup', '-rf'), /cannot start/);
  // the server counts code points, so emoji count once; lone surrogates it refuses
  assert.equal(S.valueProblem('verify', '\u{1F600}'.repeat(4096)), null);
  assert.match(S.valueProblem('verify', '\u{1F600}'.repeat(4097)), /Too long/);
  assert.match(S.valueProblem('verify', 'a\ud83dz'), /surrogate/);
  assert.match(S.valueProblem('verify', 'a\ude00z'), /surrogate/);
});

test('picker default: the current run when listed, else the most recent run that names a repo', () => {
  const runs = [run('a', '/r/one', 10), run('b', '/r/two', 30), run('c', '/r/one', 20), run('d', null, 99), { key: 'e', updated: 98 }];
  assert.equal(S.pickDefault(runs, 'a'), 'a');
  assert.equal(S.pickDefault(runs, null), 'b');
  assert.equal(S.pickDefault(runs, 'gone'), 'b');
  assert.equal(S.pickDefault(runs, 'd'), 'b');
  assert.equal(S.pickDefault([], 'a'), null);
  assert.equal(S.pickDefault(null), null);
  assert.equal(S.pickDefault([run('x', '/r/x', undefined)], null), 'x');
});

test('picker grouping: by repo, newest repo first, newest run first inside, runs without a repo left out', () => {
  const runs = [run('a', '/r/one', 10), run('b', '/r/two', 30), run('c', '/r/one', 20), run('d', '', 99), run('e', '/r/three', 5)];
  const groups = S.groupRuns(runs);
  assert.deepEqual(groups.map(g => g.repo), ['/r/two', '/r/one', '/r/three']);
  assert.deepEqual(groups.map(g => g.name), ['two', 'one', 'three']);
  assert.deepEqual(groups[1].runs.map(r => r.key), ['c', 'a']);
  assert.deepEqual(S.groupRuns(null), []);
  assert.equal(S.optionLabel(run('r1', '/r', 1, { goal: 'first line\nsecond' })), 'r1 · first line');
  assert.ok(S.optionLabel(run('r1', '/r', 1, { goal: 'x'.repeat(100) })).length < 70);
});

test('dirty editor: untouched or equal-to-disk text follows the disk', () => {
  const e = S.editorOf('make\n');
  assert.equal(S.isDirty(e), false);
  assert.equal(S.shown(e), 'make\n');
  assert.equal(S.isDirty(S.edited(e, 'make')), false);              // a trailing newline is not an edit
  const same = S.dirtyState(S.edited(e, 'make'), 'make test\n');
  assert.equal(same.dirty, false);
  assert.equal(S.shown(same.editor), 'make test\n');
  assert.deepEqual(S.dirtyState(undefined, 'a'), { editor: { base: 'a', draft: null }, dirty: false, moved: false });
  assert.equal(S.dirtyState(S.editorOf(null), null).editor.base, '');
});

test('dirty editor: unsaved text survives a refresh, and a change on disk is flagged', () => {
  const typed = S.edited(S.editorOf('make\n'), 'make -j4\n# mine');
  let r = S.dirtyState(typed, 'make\n');                           // polled, unchanged
  assert.equal(r.dirty, true);
  assert.equal(r.moved, false);
  assert.equal(S.shown(r.editor), 'make -j4\n# mine');
  r = S.dirtyState(r.editor, 'make\n');                             // polled again
  assert.equal(S.shown(r.editor), 'make -j4\n# mine');
  r = S.dirtyState(r.editor, 'make all\n');                         // the file changed under the edit
  assert.equal(r.dirty, true);
  assert.equal(r.moved, true);
  assert.equal(S.shown(r.editor), 'make -j4\n# mine');
  assert.equal(r.editor.base, 'make all\n');
  r = S.dirtyState(S.edited(S.editorOf('make\n'), ''), 'make\n');   // cleared: dirty, so Remove is the way, not Save
  assert.equal(r.dirty, true);
  assert.equal(S.shown(r.editor), '');
  r = S.dirtyState(S.edited(S.editorOf('make\n'), 'make'), 'make\n');   // typed back to the disk text: clean again
  assert.equal(r.dirty, false);
  assert.equal(S.shown(r.editor), 'make\n');
});

test('typing on text a read moved on since the textarea was drawn counts as moved', () => {
  // drawn from 'make\n'; a held body was not redrawn when the disk became 'make all\n'
  const polled = S.dirtyState(S.editorOf('make\n'), 'make all\n').editor;
  let r = S.typed(polled, 'make\n# mine', 'make\n');
  assert.equal(r.dirty, true);
  assert.equal(r.moved, true);
  assert.deepEqual(r.editor, { base: 'make all\n', draft: 'make\n# mine' });
  r = S.typed(polled, 'make all\n# mine', 'make all');               // drawn from the current text: not moved
  assert.deepEqual([r.dirty, r.moved], [true, false]);
  r = S.typed(polled, 'make all', 'make\n');                         // typed to the disk text: clean, nothing to warn of
  assert.deepEqual([r.dirty, r.moved], [false, false]);
});

test('a failed read keeps a good page with a note, and replaces the page only before any good read', () => {
  assert.deepEqual(S.readFailure({ ok: false, status: 504, detail: 'forge config show timed out' }, true), { keep: true, message: 'forge config show timed out' });
  assert.deepEqual(S.readFailure({ ok: false, status: 500, error: 'boom' }, false), { keep: false, message: 'boom' });
  assert.deepEqual(S.readFailure({ ok: false, status: 0, error: 'network' }, true), { keep: true, message: 'could not reach forge-ui' });
  assert.deepEqual(S.readFailure({ ok: false, status: 0 }, false), { keep: false, message: 'could not reach forge-ui' });
});
