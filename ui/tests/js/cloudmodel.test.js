'use strict';
// node --test: the run cloud's pure model (projects, filters, layouts, layout files).
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const CM = require(path.join(__dirname, '..', '..', 'forge_ui', 'static', 'cloudmodel.js'));

const NOW = 1760700000;
const hex = n => n.toString(16).padStart(12, '0');
function runs(n = 40) {
  const repos = ['/w/payments', '/w/catalog', '/w/forge', '/x/forge'];
  const models = [['sol:xhigh', 'opus'], ['luna', 'opus'], ['astra', 'sol:high']];
  return Array.from({ length: n }, (_, i) => {
    const tasks = 1 + (i * 7) % 14, m = models[i % 3];
    const diffs = ['low', 'medium', 'high', ''];
    return { key: hex(i + 1), repo: repos[i % repos.length], goal: `goal ${i}`, run_id: `r${i}`, kind: i % 9 === 0 ? 'solo' : 'plan',
      created: NOW - (n - i) * 3600, updated: NOW - (n - i) * 3600 + 600, task_count: tasks,
      counts: i % 5 === 0 ? { RUNNING: 1, MERGED: tasks - 1 } : i % 7 === 0 ? { FAIL: 1, MERGED: tasks - 1 } : { MERGED: tasks },
      task_statuses: Array.from({ length: tasks }, (_, k) => ({ id: `t${k}`, status: 'MERGED', difficulty: diffs[(i + k) % 4], title: `task ${k}` })),
      models: m, dwarf_models: [m[0]], qa_models: [m[1]], final_review: i % 4 === 0 ? 'FAIL' : null, verification_status: 'PASS', decompose_level: i % 2 ? 'medium' : null };
  });
}
const facts = rs => rs.map(r => CM.runFacts(r, NOW));

test('projects group by repo, oldest first, and disambiguate equal folder names', () => {
  const P = CM.projects(runs());
  assert.equal(P.length, 4);
  const names = P.map(p => p.name).sort();
  assert.ok(names.includes('forge (w)') && names.includes('forge (x)'));
  assert.ok(P[0].first <= P[1].first);
});

test('facts: task buckets, difficulty mix, roles, stale running runs', () => {
  const [f] = facts([{ ...runs(1)[0], counts: { RUNNING: 1 }, updated: NOW - 3 * 3600 }]);
  assert.equal(f.state, 'stale');
  assert.deepEqual([1, 10, 200].map(n => CM.runFacts({ key: hex(9), task_count: n }, NOW).bucket), [1, 3, 9], 'the square root of the tasks, at most 9');
  assert.ok(f.dwarf.has('sol') && f.qa.has('opus') && !f.dwarf.has('opus'));
  assert.equal(Object.values(f.diff).reduce((a, b) => a + b, 0), f.n);
});

test('every filter dimension selects exactly what it names', () => {
  const F = facts(runs());
  const pick = f => F.filter(x => CM.match(x, f, NOW));
  // runs(): odd runs have 8 tasks and even ones 1; every 4th is in /w/payments (the oldest project) with a failing
  // final review; every 9th is solo; i % 3 === 2 has no opus; 7, 14, 21 and 28 failed
  const exactly = (f, n, ok) => { const got = pick(f); assert.equal(got.length, n, JSON.stringify(f)); assert.ok(got.every(ok), JSON.stringify(f)); };
  const proj = CM.projects(F.map(x => x.run))[0].id;
  exactly({ projects: [proj] }, 10, x => x.project === proj);
  exactly({ states: ['failed'] }, 4, x => x.state === 'failed');
  exactly({ kinds: ['solo'] }, 5, x => x.kind === 'solo');
  exactly({ tasks: { min: 8 } }, 20, x => x.n >= 8);
  exactly({ tasks: { max: 7 } }, 20, x => x.n <= 7);
  exactly({ models: { aliases: ['opus'], role: 'qa' } }, 27, x => x.qa.has('opus'));
  exactly({ models: { aliases: ['opus'], role: 'dwarf' } }, 0, () => true);
  exactly({ difficulty: { levels: ['high'], mode: 'dominant' } }, 10, x => x.dominant === 'high');
  exactly({ review: 'FAIL' }, 10, x => x.review === 'FAIL');
  exactly({ review: 'none' }, 30, x => !x.review);
  exactly({ decompose: ['none'] }, 20, x => !x.decompose);
  exactly({ text: 'goal 3' }, 11, x => /^goal 3/.test(x.run.goal));
  exactly({ text: 'task 2' }, 20, x => x.n === 8);                         // task titles are searchable
  assert.equal(pick({}).length, F.length);
  assert.equal(CM.activeCount({ projects: [proj], tasks: { min: 2 } }), 2);
});

function noOverlap(L, F) {
  for (const c of L.clusters) {
    const ks = F.filter(f => f.project === c.pid).map(f => f.key);
    for (let i = 0; i < ks.length; i++) for (let j = i + 1; j < ks.length; j++) {
      const a = L.pos[ks[i]], b = L.pos[ks[j]], fa = F.find(f => f.key === ks[i]), fb = F.find(f => f.key === ks[j]);
      const sa = CM.blockSize(fa.bucket), sb = CM.blockSize(fb.bucket);
      const apartXY = Math.max(Math.abs(a[0] - b[0]), Math.abs(a[1] - b[1])) >= (sa.s + sb.s) / 2 - 1;
      const apartZ = Math.abs(a[2] - b[2]) >= Math.min(sa.h, sb.h) - 1;
      assert.ok(apartXY || apartZ, `${L.name}: ${ks[i]} and ${ks[j]} collide`);
    }
  }
}
test('each layout places every run, deterministically, without collisions inside a cluster', () => {
  const F = facts(runs(60));
  for (const name of CM.LAYOUTS) {
    const a = CM.layout(name, F), b = CM.layout(name, F);
    assert.equal(Object.keys(a.pos).length, F.length, name);
    assert.ok(Object.values(a.pos).every(p => p.every(Number.isFinite)), name);
    assert.deepEqual(a.pos, b.pos, name + ' is deterministic');
    assert.ok(a.bounds.every(Number.isFinite));
    noOverlap(a, F);
  }
  // the runs of each project started together: the timeline stacks them
  const burst = facts(runs(40).map((r, i) => ({ ...r, created: NOW + (i % 4) * 3600 + i })));
  noOverlap(CM.layout('timeline', burst), burst);
  const T = CM.layout('timeline', F);
  assert.ok(T.axis.ticks.length > 0 && T.axis.ticks.length <= 13);
});

test('a filter never moves a project, and adding a newer project leaves older ones in place', () => {
  const all = facts(runs(40));
  const some = all.filter((_, i) => i % 3 === 0);
  const A = CM.layout('helix', all), B = CM.layout('helix', some, { all });
  for (const c of B.clusters) assert.deepEqual(c.center, A.clusters.find(x => x.pid === c.pid).center);
  const extra = facts([{ ...runs(1)[0], key: 'ffffffffffff', repo: '/w/new', created: NOW + 1000, updated: NOW + 1000 }]);
  const C = CM.layout('helix', all.concat(extra));
  for (const c of A.clusters) assert.deepEqual(C.clusters.find(x => x.pid === c.pid).center, c.center);
});

test('dragged runs keep their place in every layout, constellation included', () => {
  const F = facts(runs(20)), k = F[3].key;
  for (const name of CM.LAYOUTS) {
    const at = CM.layout(name, F).pos[F[5].key].slice();                     // dropped onto another run
    assert.deepEqual(CM.layout(name, F, { pinned: { [k]: at } }).pos[k], at, name);
  }
});

test('labels: the most important that do not overlap', () => {
  const keys = CM.labelPick([{ key: 'a', x: 100, y: 100, w: 80, h: 20, pri: 3 }, { key: 'b', x: 110, y: 105, w: 80, h: 20, pri: 2 }, { key: 'c', x: 400, y: 100, w: 80, h: 20, pri: 1 }]);
  assert.deepEqual(keys, ['a', 'c']);
});

test('layout files: round trip, and anything odd is refused', () => {
  const store = { layouts: { helix: { [hex(1)]: [1, 2, 3, 99] } } };
  const file = JSON.parse(JSON.stringify(CM.serializeLayout(store, { [hex(1)]: 'r1' }, NOW)));
  const back = CM.parseLayoutFile(file);
  assert.equal(back.ok, true); assert.equal(back.count, 1); assert.deepEqual(back.layouts.helix[hex(1)], [1, 2, 3, 99]);
  const bad = CM.parseLayoutFile({ format: 'forge-ui-cloud-layout', version: 1, layouts: { helix: { 'nope': [1, 2, 3], [hex(2)]: [NaN, 1, 1], [hex(3)]: [1e9, 0, 0] }, warp: {} } });
  assert.equal(bad.count, 0); assert.equal(bad.errors.length, 4); assert.deepEqual(Object.keys(bad.layouts), ['helix']);
  assert.equal(CM.parseLayoutFile({ format: 'other' }).ok, false);
  assert.equal(CM.parseLayoutFile(null).ok, false);
});

test('empty and odd inputs never throw', () => {
  for (const name of CM.LAYOUTS) CM.layout(name, []);
  CM.options([]); CM.projects([]); CM.runFacts({ key: hex(9) }, NOW);
  assert.equal(CM.match(CM.runFacts({ key: hex(9) }, NOW), null, NOW), true);
});

test('constellation: a drag moves only the dragged run (and what it lands on), and lines span each project', () => {
  const F = facts(runs(30)), k = F[4].key;
  const A = CM.layout('constellation', F), far = [99999, 99999, 0];
  const B = CM.layout('constellation', F, { pinned: { [k]: far } });
  assert.deepEqual(B.pos[k], far);
  for (const f of F) if (f.key !== k) assert.deepEqual(B.pos[f.key], A.pos[f.key], 'unpinned runs stay where they were');
  const links = CM.constellationLinks(F, A.pos);
  const per = new Map(); for (const f of F) per.set(f.project, (per.get(f.project) || 0) + 1);
  assert.equal(links.length, [...per.values()].reduce((a, n) => a + n - 1, 0));
  for (const [a, b] of links) assert.equal(F.find(f => f.key === a).project, F.find(f => f.key === b).project);
  assert.ok(CM.LAYOUTS.every(n => CM.LAYOUT_INFO[n].azimuth !== undefined));
});
