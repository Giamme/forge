"""The run dataset: every run's inputs and outcomes, kept after the run dir is gone.

End-to-end cases drive the real scripts with the fake agent CLI from test_forge.py, then
delete the run dir and read only the store. That is the contract that matters: a run
dir is temporary, and the dataset must answer without it. Fixtures are borrowed, not
inherited, so ForgeTests' own tests do not run twice.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402
from forge_dataset import capture, checks  # noqa: E402

QUOTA = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}


class StoreCase(unittest.TestCase):
    """Each test gets its own store, so counts are exact."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'data'
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('FORGE_DATASET', None)

    def events(self, **where) -> list:
        path = self.home / 'dataset' / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute('SELECT rid, kind, run, mode, task, attempt, parent, src, data FROM events ORDER BY ts, rid').fetchall()
        finally:
            db.close()
        out = []
        for rid, kind, run, mode, task, attempt, parent, src, data in rows:
            event = dict(rid=rid, kind=kind, run=run, mode=mode, task=task, attempt=attempt, parent=parent, src=src,
                         data=json.loads(data))
            if all(event.get(k) == v for k, v in where.items()):
                out.append(event)
        return out

    def errors(self) -> str:
        path = self.home / 'dataset' / 'errors.log'
        return path.read_text() if path.exists() else ''


class StorePrimitiveTests(StoreCase):
    def test_a_cut_blob_keeps_both_ends_and_names_the_original(self):
        # A QA prompt ends with the diff and a log ends with the failure: a head-only cut
        # would keep exactly the part nobody needs.
        head, tail = store.CAPS['log']
        data = b'H' * head + b'M' * 50000 + b'T' * tail
        ref = store.put_blob(data, 'log')
        kept = store.read_blob(ref['sha'])
        self.assertTrue(kept.startswith(b'H' * head) and kept.endswith(b'T' * tail))
        self.assertNotIn(b'M' * 100, kept)
        self.assertEqual(ref['original_sha'], store.sha256(data))
        self.assertEqual(ref['n'], len(data))
        # The same original under another cap is a different stored blob, never a collision.
        self.assertNotEqual(store.put_blob(data, 'prompt')['sha'], ref['sha'])

    def test_concurrent_writers_lose_and_duplicate_nothing(self):
        # Parallel tasks capture at the same moment, from processes and from Fractal's
        # threads; every distinct event lands once and a repeated rid is ignored.
        script = (
            'import sys; sys.path.insert(0, %r)\n'
            'import forge_dataset as s\n'
            'w = int(sys.argv[1])\n'
            'for i in range(10):\n'
            '    s.record([dict(rid="e-%%d-%%d" %% (w, i), kind="t"), dict(rid="shared-%%d" %% i, kind="t")])\n'
        ) % str(ROOT / 'scripts')
        procs = [subprocess.Popen([sys.executable, '-c', script, str(w)], env=dict(os.environ)) for w in range(12)]
        threads = [threading.Thread(target=lambda w=w: [store.record([dict(rid='t-%d-%d' % (w, i), kind='t')])
                                                       for i in range(10)]) for w in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([p.wait() for p in procs], [0] * 12)
        rids = [e['rid'] for e in self.events()]
        self.assertEqual(len(rids), len(set(rids)))
        self.assertEqual(len(rids), 12 * 10 + 10 + 8 * 10)
        self.assertEqual(self.errors(), '')

    def test_a_store_inside_the_repo_is_never_written(self):
        # integrate refuses a dirty tree; a store there would block every integration.
        repo = Path(self.tmp.name) / 'repo'
        (repo / '.git').mkdir(parents=True)
        os.environ['FORGE_DATA_HOME'] = str(repo / 'data')
        run = Path(self.tmp.name) / 'run'
        run.mkdir()
        (run / 'prompt.md').write_text('x')
        (run / 'repo').write_text(str(repo) + '\n')
        capture.point('merge', str(run), values=dict(status='MERGED'), texts=dict(note='y'))
        self.assertFalse((repo / 'data').exists())

    def test_a_locked_store_costs_a_bounded_wait_and_never_raises(self):
        store.record([dict(rid='warm', kind='t')])
        db = sqlite3.connect(str(self.home / 'dataset' / 'dataset.sqlite'), timeout=0)
        db.execute('BEGIN EXCLUSIVE')
        try:
            started = time.time()
            self.assertEqual(store.record([dict(rid='blocked', kind='t')]), 0)
            self.assertLess(time.time() - started, 5)
        finally:
            db.rollback()
            db.close()
        self.assertIn('locked', self.errors())

    def provenance_of(self, rid):
        db = sqlite3.connect(str(self.home / 'dataset' / 'dataset.sqlite'))
        try:
            return db.execute('SELECT forge_version, registry_sha, code_sha, forge_rev FROM events WHERE rid = ?',
                              (rid,)).fetchone()
        finally:
            db.close()

    def test_events_name_the_code_that_produced_them_and_imports_name_none(self):
        # The release number outlives weeks of dev edits; code_sha tells two regimes apart.
        # An imported run was produced by whatever forge ran it, never by the importer.
        store.record([dict(rid='live', kind='t'), dict(rid='old', kind='t', src='import')])
        prov = store.provenance()
        self.assertEqual(self.provenance_of('live'),
                         (prov['forge_version'], prov['registry_sha'], prov['code_sha'], prov['forge_rev']))
        self.assertRegex(prov['code_sha'], '^[0-9a-f]{16}$')
        self.assertEqual(self.provenance_of('old'), (None, None, None, None))

    def test_a_review_is_graded_only_when_every_sure_finding_carries_a_severity(self):
        from forge_dataset import harvest
        sure = lambda severity, status='CONFIRMED': dict(status=status, severity=severity)
        cases = [('PASS', [], True, 'clean'), ('PASS', [sure(None, 'PLAUSIBLE')], True, 'graded'),
                 ('FAIL', [sure(1), sure(2)], True, 'graded'), ('FAIL', [sure(1), sure(None)], True, 'ungraded'),
                 ('FAIL', [sure(2, 'UNSTATED')], True, 'ungraded'), ('FAIL', [], True, 'ungraded'),
                 ('FAIL', [sure(None, 'PLAUSIBLE')], True, 'ungraded'), ('UNKNOWN', [], True, 'unparseable'),
                 ('PASS', [], False, 'unparseable')]
        self.assertEqual([harvest.review_completeness(v, f, ok) for v, f, ok, _ in cases], [c[-1] for c in cases])

    SCHEMA_1 = ('CREATE TABLE events (rid TEXT PRIMARY KEY, kind TEXT NOT NULL, ts REAL NOT NULL, src TEXT NOT NULL, '
                'repo TEXT, run TEXT, run_id TEXT, mode TEXT, task TEXT, attempt INTEGER, parent TEXT, inv TEXT, '
                'forge_version TEXT, registry_sha TEXT, data TEXT NOT NULL);'
                "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT); INSERT INTO meta VALUES ('schema', '1');")

    def old_store(self, extra=''):
        directory = self.home / 'dataset'
        directory.mkdir(parents=True)
        db = sqlite3.connect(str(directory / 'dataset.sqlite'))
        db.executescript(self.SCHEMA_1 + extra)
        db.execute("INSERT INTO events (rid, kind, ts, src, data) VALUES ('before', 't', 1, 'live', '{}')")
        db.commit()
        db.close()

    def schema(self):
        db = sqlite3.connect(str(self.home / 'dataset' / 'dataset.sqlite'))
        try:
            columns = [row[1] for row in db.execute('PRAGMA table_info(events)')]
            return db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0], columns
        finally:
            db.close()

    def test_a_schema_1_store_migrates_once_under_concurrent_first_writers(self):
        # Every capture after an upgrade races to migrate; one wins, none fails or is lost.
        self.old_store()
        script = ('import sys; sys.path.insert(0, %r)\nimport forge_dataset as s\n'
                  's.record([dict(rid="w-" + sys.argv[1], kind="t")])\n') % str(ROOT / 'scripts')
        procs = [subprocess.Popen([sys.executable, '-c', script, str(w)], env=dict(os.environ)) for w in range(12)]
        self.assertEqual([p.wait() for p in procs], [0] * 12)
        version, columns = self.schema()
        self.assertEqual((version, columns[-2:]), ('2', ['code_sha', 'forge_rev']))
        self.assertEqual(sorted(e['rid'] for e in self.events()), sorted(['before'] + ['w-%d' % w for w in range(12)]))
        self.assertEqual(self.errors(), '')

    def test_a_half_finished_migration_is_completed_by_the_next_connect(self):
        self.old_store(extra='ALTER TABLE events ADD COLUMN code_sha TEXT;')
        store.record([dict(rid='after', kind='t')])
        version, columns = self.schema()
        self.assertEqual((version, columns[-2:]), ('2', ['code_sha', 'forge_rev']))
        self.assertIsNotNone(self.provenance_of('after')[2])


class RuntimeHookTests(StoreCase):
    def runtime(self, *args, env=None):
        return subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge-runtime.py'), *map(str, args)],
                              capture_output=True, text=True, env=dict(os.environ, **(env or {})))

    def test_the_kill_switch_records_nothing(self):
        root = Path(self.tmp.name) / 'run'
        root.mkdir()
        began = self.runtime('begin', root, 'dwarf', env=dict(FORGE_DATASET='off'))
        self.runtime('finish', began.stdout.strip(), root, 'dwarf', 'codex', 0, env=dict(FORGE_DATASET='off'))
        self.assertFalse((self.home / 'dataset').exists())
        self.assertFalse((root / 'dataset.uid').exists())

    def test_a_broken_store_leaves_begin_and_finish_exactly_as_they_were(self):
        # begin's stdout IS the attempt path the shell captures; a stray print from capture
        # would corrupt every dispatch.
        root = Path(self.tmp.name) / 'run'
        root.mkdir()
        blocker = Path(self.tmp.name) / 'not-a-dir'
        blocker.write_text('x')
        env = dict(FORGE_DATA_HOME=str(blocker))
        began = self.runtime('begin', root, 'dwarf', env=env)
        self.assertEqual(began.returncode, 0, began.stderr)
        attempt = Path(began.stdout.strip())
        self.assertTrue((attempt / 'metrics.json').exists(), began.stdout)
        self.assertEqual(began.stderr, '')
        finished = self.runtime('finish', attempt, root, 'dwarf', 'codex', 0, env=env)
        self.assertEqual((finished.returncode, finished.stderr), (0, ''))

    def test_an_execution_keeps_the_code_it_started_with_and_a_mid_run_edit_marks_it(self):
        # The skill dir is the live dev checkout: an edit between begin and finish must not
        # credit the outcome to code the execution never ran.
        root = Path(self.tmp.name) / 'run'
        root.mkdir()
        registry = Path(self.tmp.name) / 'registry.tsv'
        registry.write_text((ROOT / 'registry.tsv').read_text())
        env = dict(FORGE_REGISTRY=str(registry))
        began = self.runtime('begin', root, 'dwarf', env=env)
        attempt = Path(began.stdout.strip())
        started = json.loads((attempt / 'dataset.json').read_text())['provenance']
        registry.write_text(registry.read_text() + '# edited mid-run\n')
        self.runtime('finish', attempt, root, 'dwarf', 'codex', 0, env=env)
        [dispatch] = self.events(kind='dispatch')
        self.assertTrue(dispatch['data']['code_changed'])
        db = sqlite3.connect(str(self.home / 'dataset' / 'dataset.sqlite'))
        row = db.execute('SELECT registry_sha, code_sha FROM events WHERE kind = ?', ('dispatch',)).fetchone()
        db.close()
        self.assertEqual(row, (started['registry_sha'], started['code_sha']))


class EndToEndTests(StoreCase):
    setUp_fixture = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    solo = fixtures.ForgeTests.solo

    def setUp(self):
        StoreCase.setUp(self)
        self.setUp_fixture()
        self.env['FORGE_DATA_HOME'] = str(self.home)

    def plan_of(self, rows):
        p = self.root / 'plan'
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join(f'{i}\t{d}\tlow\t{f}\tsol\topus\t{i.upper()}\n' for i, d, f in rows))
        for i, _d, _f in rows:
            t = p / 'tasks' / i
            t.mkdir(parents=True)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory',
                            '--decompose-level', 'low')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def attempts(self, task):
        return sorted(self.events(kind='attempt', task=task), key=lambda e: e['attempt'] or 0)

    def test_a_deleted_parallel_run_is_fully_answerable_from_the_store(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--verify', 'test -f a.txt && test -f b.txt')
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        shutil.rmtree(p)

        plan = self.events(kind='plan')[-1]['data']
        self.assertEqual(plan['decompose_level'], 'low')
        self.assertEqual(plan['tasks']['a']['declared_difficulty'], 'low')
        self.assertEqual(store.read_blob(plan['tasks']['a']['prompt']['sha']).decode(),
                         'Implement change. REQUIREMENT_SENTINEL TASK_a')
        for task in 'ab':
            [attempt] = self.attempts(task)
            self.assertEqual((attempt['data']['status'], attempt['attempt']), ('PASS', 1))
            self.assertIn('+implemented', store.read_blob(attempt['data']['blobs']['diff']['sha']).decode())
            roles = {e['data']['role']: e['data'] for e in self.events(kind='dispatch', parent=attempt['rid'])}
            self.assertEqual((roles['dwarf']['model'], roles['dwarf']['harness']), ('gpt-6.1-sol', 'codex'))
            self.assertEqual(roles['qa']['verdict'], 'PASS')
            self.assertEqual(roles['dwarf']['tokens']['output'], 3)
        merges = {e['task']: e['data']['status'] for e in self.events(kind='event') if e['data']['name'] == 'merge'}
        self.assertEqual(merges, {'a': 'MERGED', 'b': 'MERGED'})
        scopes = {e['data']['scope']: e['data']['status'] for e in self.events(kind='verification')}
        self.assertEqual(scopes, {'run': 'PASS', 'integrate': 'PASS'})
        self.assertTrue(self.events(kind='verify_run'))
        names = {e['data']['name'] for e in self.events(kind='event')}
        self.assertIn('integrate_advanced', names)
        commands = sorted(e['data']['command'] for e in self.events(kind='invocation'))
        self.assertEqual(commands, ['integrate', 'plan', 'run'])
        self.assertEqual(len({e['run'] for e in self.events() if e['run']}), 1)
        self.assertEqual(checks.run(store.connect())['pass_without_merge']['count'], 0)
        self.assertEqual(self.errors(), '')

    def test_each_retry_keeps_its_own_diff_and_a_failed_one_inherits_none(self):
        # attempt 1: reviewed and failed; attempt 2: a real fix that passes; a later human
        # retry whose dwarf crashes must not be credited with attempt 2's diff and verdict.
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'}, 'a.dwarf.3': {'rc': 1, 'stderr': 'Traceback: boom'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        first, second = self.attempts('a')
        self.assertEqual((first['data']['status'], second['data']['status']), ('FAIL', 'PASS'))
        self.assertEqual(second['data']['trigger'], 'auto')
        diffs = [store.read_blob(a['data']['blobs']['diff']['sha']).decode() for a in (first, second)]
        self.assertIn('+implemented', diffs[0])
        self.assertIn('+fixed', diffs[1])
        qa = self.events(kind='dispatch', parent=first['rid'])
        self.assertEqual([f['severity'] for d in qa if d['data']['role'] == 'qa' for f in d['data']['findings']], [1])

        (p / 'tasks' / 'a' / 'merged').unlink()
        (p / 'tasks' / 'a' / 'status').write_text('FAIL\n')
        self.run_script('forge-parallel.sh', 'retry', p, 'a')
        crashed = self.attempts('a')[-1]
        self.assertEqual((crashed['attempt'], crashed['data']['status'], crashed['data']['trigger']), (3, 'ERROR', 'human'))
        self.assertNotIn('diff', crashed['data']['blobs'])
        self.assertEqual(crashed['data']['changed_files'], [])

    def test_an_infrastructure_stop_is_recorded_as_a_refund_not_an_attempt(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.dwarf.1': QUOTA})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        [stopped] = self.attempts('a')
        self.assertEqual((stopped['data']['status'], stopped['attempt'], stopped['data']['refunded']), ('INFRA', 1, True))

    def test_a_solo_run_is_one_attempt_with_both_dispatches(self):
        r = self.solo()
        self.assertEqual(r.returncode, 0, r.stdout)
        [attempt] = self.events(kind='attempt', mode='solo')
        self.assertEqual(attempt['data']['verdict'], 'PASS')
        self.assertEqual(attempt['data']['changed_files'], ['change.txt'])
        roles = sorted(e['data']['role'] for e in self.events(kind='dispatch', parent=attempt['rid']))
        self.assertEqual(roles, ['dwarf', 'qa'])

    def test_importing_a_live_captured_run_adds_nothing(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        before = len(self.events())
        r = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'forge-dataset.py'), 'import', str(p),
                            '--include-temp'], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.events()), before, r.stdout)

    def test_an_export_labels_each_decision_by_what_happened_next(self):
        from forge_dataset import export
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')])
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        shutil.rmtree(p)

        cases = export.build(store.connect())
        routing = {c['task']: c for c in cases if c['slice'] == 'routing'}
        self.assertEqual({t: c['expected'] for t, c in routing.items()},
                         {'a': {'first_pass': False, 'tier': 'low'}, 'b': {'first_pass': True, 'tier': 'low'}})
        state = routing['a']['state']
        self.assertIn('REQUIREMENT_SENTINEL TASK_a', state['prompt'])
        self.assertEqual([s['id'] for s in state['plan_tasks']], ['b'])
        self.assertEqual(state['dwarf']['model'], 'gpt-6.1-sol')
        reviews = sorted((c['task'], c['expected']['passes'], '+fixed' in c['state']['diff'])
                         for c in cases if c['slice'] == 'review')
        self.assertEqual(reviews, [('a', False, False), ('a', True, True), ('b', True, False)])
        self.assertEqual({c['state']['reviewer']['model'] for c in cases if c['slice'] == 'review'}, {'opus'})
        severities = sorted((c['task'], c['expected']['passes'], c['expected'].get('severity'))
                            for c in cases if c['slice'] == 'review')
        self.assertEqual(severities, [('a', False, 'P1'), ('a', True, 'none'), ('b', True, 'none')])
        self.assertEqual(state['size'], dict(prompt_chars=len(state['prompt']), declared_files=1, plan_tasks=2,
                                             deps=0, decompose_level='low'))
        self.assertEqual((routing['a']['meta']['attempts_observed'], routing['a']['meta']['raw_verdict'],
                          routing['a']['meta']['first_raw_pass_attempt'], routing['a']['meta']['stop_reason']),
                         (2, 'FAIL', 2, 'passed'))
        [retry] = [c for c in cases if c['slice'] == 'retry']
        self.assertEqual((retry['task'], retry['state']['attempt'], retry['expected']), ('a', 2, {'retry_pass': True}))
        self.assertIn('a.txt:1 — broken', retry['state']['previous_review'])
        out = self.home / 'export'
        export.write(cases, out, seed=7, limit=None)
        self.assertEqual((out.stat().st_mode & 0o777, (out / 'dataset.jsonl').stat().st_mode & 0o777), (0o700, 0o600))

    def test_an_attempt_that_says_nothing_about_the_implementer_is_never_labeled(self):
        # A later success must not stand in for the first attempt, and a reviewer's or a
        # harness's failure is no evidence about the task: only an implementer that ran out
        # of time failed it. a times out, b's harness crashes, c's reviewer gives no verdict.
        from forge_dataset import export
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt'), ('c', '-', 'c.txt')])
        (p / 'timeout').write_text('2\n')
        self.scenario({'a.dwarf.1': {'sleep': 6}, 'b.dwarf.1': {'rc': 1},
                       'c.qa.1': {'verdict': 'none', 'findings': 'Ran out of budget before deciding.'}})
        self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual([self.text_of(p, t) for t in 'abc'], ['TIMEOUT', 'ERROR', 'PASS'])
        for task in 'ab':
            r = self.run_script('forge-parallel.sh', 'retry', p, task)
            self.assertEqual(self.text_of(p, task), 'PASS', r.stdout)
        shutil.rmtree(p)

        stats = {}
        cases = export.build(store.connect(), slices=('routing',), stats=stats)
        routing = {c['task']: c for c in cases}
        self.assertEqual({t: c['expected']['first_pass'] for t, c in routing.items()}, {'a': False, 'c': True})
        self.assertEqual(routing['c']['meta']['attempts_observed'], 1)
        self.assertEqual(stats['censored'], {'routing:dwarf_error': 1})

    def text_of(self, plan, task):
        return (plan / 'tasks' / task / 'status').read_text().strip()


class ImportTests(StoreCase):
    def test_an_old_run_gets_one_attempt_per_execution_with_its_own_diff(self):
        # A run dir from before live capture: no dataset.json, no attempts.tsv. Earlier
        # attempts' diffs come from what QA was shown, never from the task's current files.
        plan = Path(self.tmp.name) / 'old'
        tdir = plan / 'tasks' / 'a'
        tdir.mkdir(parents=True)
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (plan / 'repo').write_text('/nonexistent/repo\n')
        (plan / 'run_id').write_text('old\n')
        (tdir / 'status').write_text('PASS\n')
        (tdir / 'attempt').write_text('2\n')
        (tdir / 'changes.diff').write_text('diff --git a/a.txt b/a.txt\n+second\n')

        def attempt(role, at, **files):
            d = tdir / 'attempts' / ('%s-%032x' % (role, int(at * 1000)))
            d.mkdir(parents=True)
            (d / 'metrics.json').write_text(json.dumps(dict(role=role, wall_started=at, exit_code=0,
                                                            harness='codex' if role != 'task' else 'pipeline',
                                                            elapsed_s=dict(total=5))))
            for name, body in files.items():
                (d / name.replace('_', '.')).write_text(body)

        attempt('task', 100.0)
        attempt('dwarf', 101.0, dwarf_resolved='alias=sol\nharness=codex\nmodel=m\n')
        attempt('qa', 102.0, qa_resolved='alias=opus\n', qa_last='FORGE_VERDICT: FAIL',
                qa_prompt='review this\n```diff\ndiff --git a/a.txt b/a.txt\n+first\n```\n')
        attempt('task', 200.0)
        attempt('dwarf', 201.0, dwarf_resolved='alias=sol\n')
        attempt('qa', 202.0, qa_resolved='alias=opus\n', qa_last='FORGE_VERDICT: PASS')

        from forge_dataset import importer
        args = type('A', (), dict(paths=[str(plan)], registry=False, fractal=False, ledger=[], include_temp=True,
                                  dry_run=False))()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(args), 0)
        attempts = sorted(self.events(kind='attempt'), key=lambda e: e['attempt'])
        self.assertEqual([(a['attempt'], a['data']['status']) for a in attempts], [(1, 'FAIL'), (2, 'PASS')])
        self.assertIn('+first', store.read_blob(attempts[0]['data']['blobs']['diff']['sha']).decode())
        self.assertIn('+second', store.read_blob(attempts[1]['data']['blobs']['diff']['sha']).decode())
        self.assertEqual([len(a['data']['children']) for a in attempts], [2, 2])
        self.assertTrue(all(e['src'] == 'import' for e in self.events()))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(args), 0)
        self.assertEqual(len(self.events(kind='attempt')), 2)

    def test_an_execution_still_running_is_left_for_live_capture(self):
        # The first write of a rid wins: importing an execution before it ends would
        # permanently shadow the outcome live capture records when it does.
        run = Path(self.tmp.name) / 'run'
        attempt = run / 'attempts' / ('dwarf-%032x' % 7)
        attempt.mkdir(parents=True)
        (run / 'prompt.md').write_text('x')
        (attempt / 'metrics.json').write_text(json.dumps(dict(role='dwarf', wall_started=1.0, exit_code=None)))
        from forge_dataset import importer
        args = type('A', (), dict(paths=[str(run)], registry=False, fractal=False, ledger=[], include_temp=True,
                                  dry_run=False))()
        with contextlib.redirect_stdout(io.StringIO()):
            importer.main(args)
        self.assertEqual(self.events(kind='dispatch'), [])


class MemoryTests(StoreCase):
    def test_curation_and_cap_evictions_are_recorded_with_their_reasons(self):
        repo = Path(self.tmp.name) / 'repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        run = Path(self.tmp.name) / 'run'
        run.mkdir()
        (run / 'prompt.md').write_text('x')
        last = run / 'qa.last'
        last.write_text('FORGE_LEARNING: trap | flaky when run twice [gone/file.py]\n'
                        'FORGE_LEARNING: verify | python3 -m unittest\nFORGE_VERDICT: PASS\n')
        env = dict(os.environ, FORGE_JEV='off', FORGE_MEMORY='on')
        r = subprocess.run(['bash', str(ROOT / 'scripts' / 'forge-memory.sh'), 'record', str(repo), '--last', str(last),
                            '--role', 'qa', '--run-id', 'r1', '--run-dir', str(run), '--verdict', 'PASS'],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        [event] = [e for e in self.events(kind='event') if e['data']['name'] == 'memory_record']
        curation = store.read_blob(event['data']['blobs']['curation']['sha']).decode().splitlines()
        self.assertEqual([line.split('\t')[0] for line in curation], ['trap', 'verify'])
        drops = store.read_blob(event['data']['blobs']['drops']['sha']).decode()
        self.assertIn('anchor_missing\ttrap', drops)


class JevCaptureTests(StoreCase):
    def test_a_live_judgment_keeps_its_full_request_and_a_replay_is_not_recorded(self):
        from forge_jev import client, questions
        run = Path(self.tmp.name) / 'run'
        run.mkdir()
        qs = {'q': questions.Noul('Is this risky?')}
        payload = json.dumps(dict(answers={'q': {'p': 0.8}}, model='jev-test')).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload

        env = {'TYPESAFE_API_KEY': 'k', 'XDG_CONFIG_HOME': self.tmp.name}
        with patch.dict(os.environ, env), patch('urllib.request.urlopen', return_value=Response()):
            os.environ.pop('FORGE_JEV_FIXTURES', None)
            self.assertIsNotNone(client.ask({'diff': 'x'}, qs, site='jev-drift', run_dir=run))
        [event] = self.events(kind='jev')
        line = json.loads((run / 'jev.jsonl').read_text().splitlines()[0])
        self.assertEqual(event['data']['blobs']['request']['original_sha'], line['key'])
        request = json.loads(store.read_blob(event['data']['blobs']['request']['sha']))
        self.assertEqual(request['state'], {'diff': 'x'})
        self.assertEqual(event['data']['answers'], {'q': {'p': 0.8}})

        fixtures_dir = Path(self.tmp.name) / 'fixtures'
        fixtures_dir.mkdir()
        (fixtures_dir / (line['key'] + '.json')).write_text(payload.decode())
        with patch.dict(os.environ, dict(env, FORGE_JEV_FIXTURES=str(fixtures_dir))):
            self.assertIsNotNone(client.ask({'diff': 'x'}, qs, site='jev-drift', run_dir=run))
        self.assertEqual(len(self.events(kind='jev')), 1)


class SubsetReceiptTests(StoreCase):
    """A test selection is only a label when we know how the chosen tests ended, so
    every outcome leaves a receipt, not just the failure that reaches QA."""

    def setUp(self):
        StoreCase.setUp(self)
        self.repo = Path(self.tmp.name) / 'repo'
        (self.repo / 'tests').mkdir(parents=True)
        (self.repo / 'tests' / 'test_x.py').write_text('x = 1\n')
        git = lambda *a: subprocess.run(['git', '-C', str(self.repo), *a], check=True, capture_output=True)
        git('init', '-q')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base', '--allow-empty')
        git('add', '-A')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'tests')
        self.base = subprocess.check_output(['git', '-C', str(self.repo), 'rev-parse', 'HEAD']).decode().strip()
        (self.repo / 'broken').write_text('x')
        git('add', '-A')
        git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'change')
        self.run_dir = Path(self.tmp.name) / 'run'
        self.run_dir.mkdir()

    def outcome(self, command, timeout=60):
        from forge_jev import cli
        env = dict(TYPESAFE_API_KEY='k', FORGE_JEV_TESTS='on', XDG_CONFIG_HOME=self.tmp.name)
        with patch.dict(os.environ, env), patch('forge_jev.subset.select', return_value=[(0.9, 'tests/test_x.py')]), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(['test-subset', '--repo', str(self.repo), '--commit', 'HEAD', '--base', self.base,
                      '--task', str(self.repo / 'tests' / 'test_x.py'), '--command', command,
                      '--run-dir', str(self.run_dir), '--timeout', str(timeout)])
        receipts = [e for e in self.events(kind='event') if e['data']['name'] == 'subset_run']
        self.assertEqual(len(receipts), 1)
        os.remove(self.home / 'dataset' / 'dataset.sqlite')
        return receipts[0]['data']

    def test_every_outcome_leaves_a_receipt(self):
        passed = self.outcome('test -f {files}')
        self.assertEqual((passed['outcome'], passed['chosen'], passed['candidate_exit']),
                         ('pass', ['tests/test_x.py'], 0))
        regression = self.outcome('echo checking; test ! -f broken && test -f {files}')
        self.assertEqual((regression['outcome'], regression['base_exit']), ('regression', 0))
        self.assertIn('candidate_output', regression['blobs'])
        self.assertEqual(self.outcome('false {files}')['outcome'], 'environment')
        self.assertEqual(self.outcome('sleep 5 # {files}', timeout=1)['outcome'], 'timeout')


class FractalCaptureTests(StoreCase):
    def test_steps_captured_from_many_threads_are_each_recorded_once(self):
        task = Path(self.tmp.name) / 'runs' / 'run-x' / 'tasks' / 'task-1'
        task.mkdir(parents=True)
        (task / 'request.json').write_text(json.dumps(dict(repo='/r', output='/o')))
        steps = [task / 'nodes' / 'implementation' / 'steps' / ('%032x' % i) for i in range(16)]
        for step in steps:
            step.mkdir(parents=True)
        node = dict(id='implementation', iteration=0, model='sol', status='active')
        threads = [threading.Thread(target=capture.fractal_step, args=(task, node, step),
                                    kwargs=dict(phase='implementation', iteration=0, rc=0, cost=None, resolved='m'))
                   for step in steps]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.events(kind='fractal_step')), 16)
        self.assertEqual(self.errors(), '')


class IndependenceTests(unittest.TestCase):
    def test_only_capture_entry_points_import_the_dataset(self):
        # Nothing that builds a prompt may read earlier runs' verdicts, or the reviewer
        # stops being independent. Outside its own package, the dataset may only be
        # written to, through capture.
        allowed = {'forge-runtime.py', 'forge-schedule.py', 'forge-dataset.py', 'client.py', 'cli.py',
                   'invocation.py', 'pipeline.py'}
        for path in (ROOT / 'scripts').rglob('*'):
            if path.suffix not in ('.py', '.sh') or 'forge_dataset' in path.parts:
                continue
            text = path.read_text(errors='replace')
            if 'forge_dataset' not in text and 'forge-dataset.py' not in text:
                continue
            if path.suffix == '.sh':
                for line in text.splitlines():
                    if 'forge-dataset.py' in line:
                        self.assertRegex(line, r'forge-dataset\.py" (event|invocation)', str(path))
                continue
            self.assertIn(path.name, allowed, path)
            for line in text.splitlines():
                if 'forge_dataset' in line and 'import' in line:
                    self.assertRegex(line, r'from forge_dataset(\.capture)? import (capture|point|jev|fractal_step|'
                                           r'fractal_outcome)|from forge_dataset\.cli import main', str(path))
