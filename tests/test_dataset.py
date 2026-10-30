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
from unittest import mock
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
        # WAL, as connect() has made every store since the first one: a rollback-journal
        # store would race twelve first writers to switch it, a race no real store has.
        db.execute('PRAGMA journal_mode=WAL')
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

    def test_first_writers_to_a_new_store_from_threads_lose_nothing(self):
        # A plan's first-pass predictions are threads of one process, and on a new machine
        # they can be the store's very first writers: switching the new file to WAL must not
        # drop the event of whichever thread loses that race (it lost 5 rounds in 40 before).
        for r in range(20):
            home = self.home / ('r%d' % r)
            with patch.dict(os.environ, {'FORGE_DATA_HOME': str(home)}):
                barrier = threading.Barrier(8)

                def write(i, r=r, barrier=barrier):
                    barrier.wait()
                    store.record([dict(rid='r%d-%d' % (r, i), kind='t')])
                threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            db = sqlite3.connect(str(home / 'dataset' / 'dataset.sqlite'))
            try:
                self.assertEqual(db.execute('SELECT count(*) FROM events').fetchone()[0], 8, r)
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            finally:
                db.close()
            self.assertFalse((home / 'dataset' / 'errors.log').exists(), r)

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
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        StoreCase.setUp(self)
        self.setUp_fixture()
        self.env['FORGE_DATA_HOME'] = str(self.home)

    def plan_of(self, rows, *flags):
        p = self.root / 'plan'
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join(f'{i}\t{d}\tlow\t{f}\tsol\topus\t{i.upper()}\n' for i, d, f in rows))
        for i, _d, _f in rows:
            t = p / 'tasks' / i
            t.mkdir(parents=True)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory',
                            '--decompose-level', 'low', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.plan_output = r.stdout
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

    def test_a_flipped_review_is_recorded_as_the_gate_and_kept_as_the_raw_verdict(self):
        from forge_dataset import export
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'verdict': 'PASS', 'findings': '- [P2][CONFIRMED] a.txt:1 — wrong'},
                       'a.dwarf.2': {'content': 'fixed'}})
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        first = self.attempts('a')[0]
        self.assertEqual(first['data']['gate'].split('\t')[1], 'fail')
        cases = export.build(store.connect())
        course = {c['task']: c for c in cases if c['slice'] == 'routing'}['a']['meta']
        self.assertEqual((course['gate_fail'], course['gate_pass'], course['raw_verdict']), (True, False, 'PASS'))
        rows = [r for r in export._qa_rows(export.Runs(export.Store(store.connect()))) if r['attempt'] == first['attempt']]
        self.assertEqual([(r['gate'], r['verdict']) for r in rows], [('fail', 'PASS')])

    def test_an_earlier_qa_execution_does_not_inherit_the_gate_of_the_one_the_attempt_ended_on(self):
        # One attempt, two QA executions. The first prints a clean PASS and then the dispatch
        # stops on quota, so forge never gates it. QA-only resume keeps the attempt number;
        # the second PASS lists a CONFIRMED P2 and the gate turns the attempt into FAIL.
        # The gate belongs to that second review. Built through the fake harness: qa.1 is the
        # infrastructure stop, retry re-runs QA only.
        from forge_dataset import export
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario({'a.qa.1': {'rc': 1, 'is_error': True, 'verdict': 'PASS',
                                  'findings': "You've hit your usage limit. Try again in 2 hours."},
                       'a.qa.2': {'verdict': 'PASS', 'findings': '- [P2][CONFIRMED] a.txt:1 — wrong'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'qa'], r.stdout)
        self.assertEqual((p / 'tasks' / 'a' / 'attempt').read_text().strip(), '1')
        self.assertEqual((p / 'tasks' / 'a' / 'status').read_text().strip(), 'FAIL')
        stopped, gated = self.attempts('a')
        self.assertEqual((stopped['attempt'], stopped['data']['status'], stopped['data']['infra']['stage']),
                         (1, 'INFRA', 'qa'))
        self.assertEqual((gated['attempt'], gated['data']['status'], gated['data']['gate'].split('\t')[1]),
                         (1, 'FAIL', 'fail'))
        qa = [e for e in self.events(kind='dispatch', task='a') if e['data']['role'] == 'qa']
        self.assertEqual([(e['attempt'], e['data']['verdict'], (e['data'].get('infra') or {}).get('class'),
                           e['data'].get('session_continue')) for e in qa],
                         [(1, 'PASS', 'quota', None), (1, 'PASS', None, None)])

        runs = export.Runs(export.Store(store.connect()))
        rows = [row for row in export._qa_rows(runs) if row['task'] == 'a']
        self.assertEqual([(row['gate'], row['verdict'], row['worst']) for row in rows],
                         [(None, 'PASS', 'none'), ('fail', 'PASS', 'P2')])
        cases = [c for c in export.review_cases(runs) if c['task'] == 'a']
        self.assertEqual([c['meta']['gate'] for c in cases], [row['gate'] for row in rows])
        # Same diff and the same PASS, but the severities disagree (none vs P2). Exporting
        # one case per gate must not grade them separately.
        self.assertEqual(cases[0]['state'], cases[1]['state'])
        self.assertEqual([c['expected'] for c in cases], [{'passes': True}, {'passes': True}])
        course = next(c['meta'] for c in export.routing_cases(runs) if c['task'] == 'a')
        self.assertEqual((course['gate_fail'], course['gate_pass'], course['raw_verdict']), (True, False, 'PASS'))

    def test_separate_runs_that_met_the_same_diff_stay_one_review_case_whatever_their_gates(self):
        # Two runs of the same task produce the same diff, each with one QA execution. The first
        # reviewer's PASS lists a CONFIRMED P2 (gate fail), the second is clean (gate keep). Only
        # the executions of one attempt are split by gate; these export as one case, as before.
        from forge_dataset import export
        self.scenario({'a.qa.1': {'verdict': 'PASS', 'findings': '- [P2][CONFIRMED] a.txt:1 — wrong'}})
        p = self.plan_of([('a', '-', 'a.txt')])
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        shutil.rmtree(p)
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)

        runs = export.Runs(export.Store(store.connect()))
        rows = [row for row in export._qa_rows(runs) if row['task'] == 'a']
        self.assertEqual(len({row['run'] for row in rows}), 2)
        self.assertEqual(len({row['diff'] for row in rows}), 1)
        self.assertEqual(sorted(row['gate'] for row in rows), ['fail', 'keep'])
        [case] = [c for c in export.review_cases(runs) if c['task'] == 'a']
        self.assertEqual(case['key'], export._sha(runs.prompt(case['run'], 'a')[:export.REVIEW_CHARS], rows[0]['diff']))

    def test_a_continued_session_the_attempt_ended_on_leaves_no_row_carrying_its_gate(self):
        # The last verdict-bearing QA dispatch continued a reviewer session, so _qa_rows skips
        # it. The earlier fresh PASS of the same attempt must not inherit the attempt's gate.
        # The fake harness's QA-only re-run is a fresh dispatch, so this shape is imported the
        # way the harvester reads a run dir: two qa dispatches, one attempt, gate `P2\tfail\t...`.
        plan = Path(self.tmp.name) / 'continued-gate'
        plan.mkdir()
        (plan / 'dataset.uid').write_text('uid-continued-gate\n')
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (plan / 'repo').write_text('/nonexistent/repo\n')
        (plan / 'run_id').write_text('continued-gate\n')
        (plan / 'goal.txt').write_text('goal\n')
        tdir = plan / 'tasks' / 'a'
        tdir.mkdir(parents=True)
        (tdir / 'status').write_text('FAIL\n')
        (tdir / 'attempt').write_text('1\n')
        (tdir / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a\n')
        (tdir / 'qa.gate').write_text('P2\tfail\tP2\t1\t0\t0\n')
        (tdir / 'reviewed.commit').write_text('commit-continued-gate\n')
        diff = 'diff --git a/a.txt b/a.txt\n+a'
        (tdir / 'changes.diff').write_text(diff + '\n')
        task_attempt = tdir / 'attempts' / 'task-continued-gate'
        task_attempt.mkdir(parents=True)
        (task_attempt / 'metrics.json').write_text(json.dumps(
            dict(role='task', wall_started=1.0, exit_code=0, harness='pipeline', elapsed_s=dict(total=1))))

        def qa(name, mode, started):
            attempt = tdir / 'attempts' / name
            attempt.mkdir()
            (attempt / 'metrics.json').write_text(json.dumps(
                dict(role='qa', wall_started=started, exit_code=0, harness='codex', elapsed_s=dict(total=1))))
            (attempt / 'qa.resolved').write_text('alias=opus\nharness=codex\nmodel=opus\n')
            (attempt / 'qa.last').write_text('FORGE_VERDICT: PASS\n')
            (attempt / 'qa.prompt').write_text('review this\n```diff\n%s\n```\n' % diff)
            if mode is not None:
                (attempt / 'qa.continue').write_text('requested=prev\nmode=%s\nreason=round\n' % mode)

        qa('qa-fresh', None, 2.0)
        qa('qa-continued', 'continued', 3.0)
        from forge_dataset import importer
        args = type('A', (), dict(paths=[str(plan)], registry=False, fractal=False, ledger=[],
                                  include_temp=True, dry_run=False))()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(args), 0)

        from forge_dataset import export
        runs = export.Runs(export.Store(store.connect()))
        qa_events = runs.dispatches[('uid-continued-gate', 'a', 'qa')]
        self.assertEqual([(e['attempt'], e['d']['wall_started'], e['d']['session_continue'], e['d']['verdict'])
                          for e in qa_events],
                         [(1, 2.0, None, 'PASS'), (1, 3.0, 'continued', 'PASS')])
        self.assertEqual(self.attempts('a')[-1]['data']['gate'].split('\t')[1], 'fail')
        rows = [row for row in export._qa_rows(runs) if row['task'] == 'a']
        self.assertEqual([(row['verdict'], row['gate']) for row in rows], [('PASS', None)])
        cases = [c for c in export.review_cases(runs) if c['task'] == 'a']
        self.assertEqual([c['meta']['gate'] for c in cases], [None])

    def test_a_shadow_pair_belongs_only_to_the_qa_execution_the_attempt_ended_on(self):
        # One attempt, two reviews of the same diff, and a shadow that finished. The first
        # PASS is the infrastructure stop forge never gated; the second PASS lists a
        # CONFIRMED P2 and the gate flips the attempt to FAIL. shadow_pairs binds the
        # shadow to that second dispatch. Built from store events in the shape the
        # harvester writes: the fake harness's QA-only re-run does not also leave one
        # completed shadow beside both dispatches.
        plan = Path(self.tmp.name) / 'shadow-gate'
        plan.mkdir()
        (plan / 'dataset.uid').write_text('uid-shadow-gate\n')
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (plan / 'repo').write_text('/nonexistent/repo\n')
        (plan / 'run_id').write_text('shadow-gate\n')
        (plan / 'goal.txt').write_text('goal\n')
        tdir = plan / 'tasks' / 'a'
        tdir.mkdir(parents=True)
        (tdir / 'status').write_text('FAIL\n')
        (tdir / 'attempt').write_text('1\n')
        (tdir / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_a\n')
        (tdir / 'qa.gate').write_text('P2\tfail\tP2\t1\t0\t0\n')
        (tdir / 'reviewed.commit').write_text('commit-shadow-gate\n')
        diff = 'diff --git a/a.txt b/a.txt\n+a'
        (tdir / 'changes.diff').write_text(diff + '\n')
        task_attempt = tdir / 'attempts' / 'task-shadow-gate'
        task_attempt.mkdir(parents=True)
        (task_attempt / 'metrics.json').write_text(json.dumps(
            dict(role='task', wall_started=1.0, exit_code=0, harness='pipeline', elapsed_s=dict(total=1))))

        def qa(name, body, started):
            attempt = tdir / 'attempts' / name
            attempt.mkdir()
            (attempt / 'metrics.json').write_text(json.dumps(
                dict(role='qa', wall_started=started, exit_code=0, harness='codex', elapsed_s=dict(total=1))))
            (attempt / 'qa.resolved').write_text('alias=opus\nharness=codex\nmodel=opus\n')
            (attempt / 'qa.last').write_text(body)
            (attempt / 'qa.prompt').write_text('review this\n```diff\n%s\n```\n' % diff)

        qa('qa-stopped', 'FORGE_VERDICT: PASS\n', 2.0)
        qa('qa-gated', '- [P2][CONFIRMED] a.txt:1 — wrong\nFORGE_VERDICT: PASS\n', 3.0)
        shadow = plan / 'shadow' / 'a' / '1-1' / 'attempts' / 'qa-shadow-a'
        shadow.mkdir(parents=True)
        (shadow / 'metrics.json').write_text(json.dumps(
            dict(role='qa', wall_started=4.0, exit_code=0, harness='codex', elapsed_s=dict(total=1))))
        (shadow / 'qa.resolved').write_text('alias=sol\nharness=codex\nmodel=shadow-model\n')
        (shadow / 'qa.last').write_text('FORGE_VERDICT: FAIL\n')
        (shadow / 'qa.prompt').write_text('review this\n```diff\n%s\n```\n' % diff)
        (shadow / 'dataset.json').write_text(json.dumps(dict(
            run='uid-shadow-gate', run_id='shadow-gate', mode='parallel', scope='task', task='a',
            shadow=dict(attempt=1, reviewed='commit-shadow-gate', primary='opus'))))
        from forge_dataset import importer
        args = type('A', (), dict(paths=[str(plan)], registry=False, fractal=False, ledger=[],
                                  include_temp=True, dry_run=False))()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(args), 0)
        from forge_dataset.capture import point
        point('shadow_review', str(plan / 'shadow' / 'a' / '1-1'), 'a',
              dict(status='done', attempt=1, reviewed='commit-shadow-gate'))

        from forge_dataset import export
        runs = export.Runs(export.Store(store.connect()))
        pairs = export.shadow_pairs(runs)
        self.assertEqual(set(pairs), {('uid-shadow-gate', 'a', 1)})
        primary, shadow_event = pairs[('uid-shadow-gate', 'a', 1)]
        self.assertEqual(primary['d']['wall_started'], 3.0)
        self.assertEqual(shadow_event['d']['verdict'], 'FAIL')
        rows = [row for row in export._qa_rows(runs) if row['task'] == 'a']
        self.assertEqual([(row['gate'], row['verdict'], row['worst']) for row in rows],
                         [(None, 'PASS', 'none'), ('fail', 'PASS', 'P2')])
        cases = [c for c in export.review_cases(runs) if c['task'] == 'a']
        self.assertEqual([c['meta']['gate'] for c in cases], [None, 'fail'])
        self.assertNotIn('shadow', cases[0]['meta'])
        self.assertEqual(cases[1]['meta']['shadow'],
                         dict(verdict='FAIL', reviewer='shadow-model', worst='none'))
        self.assertIs(cases[1]['meta']['consensus'], False)
        self.assertEqual([c['expected'] for c in cases], [{'passes': True}, {'passes': True}])

    def test_a_cohort_takes_the_shadow_pair_whose_primary_it_contains(self):
        # Run A's earlier execution reviewed diff X while its ending dispatch reviewed Y;
        # run B's ending dispatch reviewed X. A's pair sorts first but its primary is not
        # in the cohort of X, so B's pair must still be found.
        from forge_dataset import export

        def row(run, dispatch):
            return dict(run=run, task='t', attempt=1, repo='r', verdict='PASS', gate='pass',
                        diff='X', review='', effort=None, model='m', reviewer={}, completeness='clean',
                        worst='none', dispatch=dispatch)

        a_early, a_end, b_end = object(), object(), object()
        shadow_a = dict(d=dict(verdict='FAIL', model='s'))
        shadow_b = dict(d=dict(verdict='PASS', model='s'))
        primary_b = dict(d=dict(verdict='PASS'))
        rows = [row('A', a_early), row('B', b_end)]
        rows[1]['dispatch'] = primary_b
        pairs = {('A', 't', 1): (a_end, shadow_a), ('B', 't', 1): (primary_b, shadow_b)}

        class FakeRuns:
            reviewed = {}
            survival = {}

            def prompt(self, run, task):
                return 'task'

        with mock.patch.object(export, '_qa_rows', return_value=iter(rows)), \
                mock.patch.object(export, 'shadow_pairs', return_value=pairs):
            cases = list(export.review_cases(FakeRuns()))
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]['meta']['shadow']['verdict'], 'PASS')
        self.assertIs(cases[0]['meta']['consensus'], True)

    def test_a_solo_run_records_its_gate_and_the_tolerated_findings(self):
        self.scenario({'*.qa.*': {'verdict': 'PASS', 'findings': '- [P3][CONFIRMED] change.txt:1 — nit'}})
        r = self.solo()
        self.assertEqual(r.returncode, 0, r.stdout)
        [attempt] = self.events(kind='attempt', mode='solo')
        self.assertEqual(attempt['data']['gate'].split('\t')[1], 'keep')
        self.assertIn('nit', store.read_blob(attempt['data']['blobs']['known_issues']['sha']).decode())

    def test_a_plan_records_its_threshold_and_whether_it_was_the_default(self):
        self.plan_of([('a', '-', 'a.txt')])
        self.assertEqual({k: self.events(kind='plan')[-1]['data']['options'][k]
                          for k in ('qa_threshold', 'qa_threshold_source')},
                         dict(qa_threshold='P2', qa_threshold_source='default'))
        (self.root / 'plan').rename(self.root / 'plan0')
        self.plan_of([('a', '-', 'a.txt')], '--qa-threshold', 'P1')
        options = self.events(kind='plan')[-1]['data']['options']
        self.assertEqual((options['qa_threshold'], options['qa_threshold_source']), ('P1', 'set'))

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

    def test_the_export_skips_rows_recorded_by_the_removed_clef_sites(self):
        # Stores written before #35 hold clef-first-pass and clef-retry-pass `jev` events.
        # They are neither an error nor a baseline, and the cases keep their ids.
        from forge_dataset import export
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'}})
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(list(p.rglob('clef.*')), [])
        uid = (p / 'dataset.uid').read_text().strip()
        shutil.rmtree(p)

        before = export.build(store.connect(), slices=('routing', 'retry'))
        answers = lambda qid: {qid: dict(type='noul', noul=0.42)}  # noqa: E731
        store.record([
            dict(rid='jev:old-first-a', kind='jev', ts=1.0, run=uid, task='a',
                 data=dict(site='clef-first-pass', ok=True, model='clef', intended_attempt=1,
                           answers=answers('first_pass'), assignment=dict(dwarf='sol', qa='opus'))),
            dict(rid='jev:old-retry-a', kind='jev', ts=2.0, run=uid, task='a',
                 data=dict(site='clef-retry-pass', ok=True, model='clef', attempt=2,
                           answers=answers('retry_pass')))])
        self.assertEqual(len([e for e in self.events(kind='jev') if e['data']['site'].startswith('clef-')]), 2)
        after = export.build(store.connect(), slices=('routing', 'retry'))
        self.assertEqual([c['id'] for c in after], [c['id'] for c in before])
        self.assertEqual({c['slice'] for c in after}, {'routing', 'retry'})
        for case in after:
            self.assertEqual([k for k in case['baseline'] if k.endswith('_clef')], [], case['id'])
        [retry_case] = [c for c in after if c['slice'] == 'retry']
        self.assertEqual(retry_case['baseline'], {})
        self.assertEqual(self.errors(), '')

    def test_a_shadow_review_is_recorded_beside_the_run_and_changes_nothing(self):
        # A second reviewer of another model family, sampled (here every attempt), measures
        # how often two reviewers agree. Nothing in the run reads it: a shadow FAIL on a
        # diff the primary passed leaves the task PASS and merged.
        from forge_dataset import export
        self.scenario({'a.shadow.*': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — wrong'}})
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')], '--shadow-qa', 'opus,sol', '--shadow-rate', '1')
        self.assertIn('shadow review: opus,sol on a sampled 1 of reviewed attempts (about 2 extra reviews', self.plan_output)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual({t: self.text_of(p, t) for t in 'ab'}, {'a': 'PASS', 'b': 'PASS'})
        self.assertTrue(all((p / 'tasks' / t / 'merged').exists() for t in 'ab'))
        self.assertEqual(sorted(self.calls()), ['dwarf', 'dwarf', 'qa', 'qa', 'shadow', 'shadow'])
        self.assertEqual({t: (p / 'shadow' / t / '1-1' / 'shadow.status').read_text().strip() for t in 'ab'},
                         {'a': 'done', 'b': 'done'})
        self.assertEqual(json.loads((p / 'shadow' / 'a' / '1-1' / 'shadow.json').read_text())['spec'], 'sol')
        # Two reviews of one diff, each in its own files: the shadow's FAIL never touched the primary's PASS.
        verdict = lambda path: path.read_text().strip().splitlines()[-1]
        self.assertEqual((verdict(p / 'tasks' / 'a' / 'qa.last'), verdict(p / 'shadow' / 'a' / '1-1' / 'qa.last')),
                         ('FORGE_VERDICT: PASS', 'FORGE_VERDICT: FAIL'))
        shutil.rmtree(p)

        shadows = {e['task']: e for e in self.events(kind='dispatch') if e['data']['role'] == 'qa_shadow'}
        self.assertEqual({t: (e['attempt'], e['data']['verdict'], e['data']['model']) for t, e in shadows.items()},
                         {'a': (1, 'FAIL', 'gpt-6.1-sol'), 'b': (1, 'PASS', 'gpt-6.1-sol')})
        [attempt_a] = self.attempts('a')
        self.assertEqual(shadows['a']['data']['shadow']['reviewed'], attempt_a['data']['reviewed_commit'])
        ended = {e['task']: e['data']['status'] for e in self.events(kind='event') if e['data']['name'] == 'shadow_review'}
        self.assertEqual(ended, {'a': 'done', 'b': 'done'})
        reviews = {c['task']: c for c in export.build(store.connect(), slices=('review',))}
        self.assertEqual({t: (c['expected']['passes'], c['meta']['consensus']) for t, c in reviews.items()},
                         {'a': (True, False), 'b': (True, True)})
        runs = export.Runs(export.Store(store.connect()))
        self.assertEqual({k: export.agreement(runs)[k] for k in ('shadow_reviews', 'pairs', 'agree')},
                         dict(shadow_reviews=2, pairs=2, agree=1))
        self.assertEqual(checks.run(store.connect())['qa_without_attempt']['items'], [])
        self.assertEqual(self.errors(), '')

    def test_a_shadow_review_never_holds_its_task_for_long_or_outlives_it(self):
        # The shadow may take as long again as the primary did (here a 2 s minimum), then it
        # is stopped and the task goes on. A spec that cannot run here is dropped, not fatal.
        self.env['FORGE_SHADOW_GRACE_S'] = '2'
        self.scenario({'a.shadow.*': {'sleep': 60}})
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')], '--shadow-qa', 'nosuchalias,sol',
                         '--shadow-rate', '1')
        started = time.time()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertLess(time.time() - started, 40, r.stdout)
        self.assertIn('shadow review spec nosuchalias is not usable here', r.stdout)
        self.assertEqual((p / 'shadow' / 'a' / '1-1' / 'shadow.status').read_text().strip(), 'overtime')
        self.assertEqual((p / 'shadow' / 'b' / '1-1' / 'shadow.status').read_text().strip(), 'done')
        self.assertEqual(self.text_of(p, 'a'), 'PASS')
        live = subprocess.run(['pgrep', '-f', str(p / 'shadow')], capture_output=True, text=True)
        self.assertEqual(live.stdout.strip(), '')

    def test_a_shadow_review_dies_with_the_task_that_started_it(self):
        # An interrupt never reaches a background job (a manual retry's Ctrl-C), and nothing
        # cleans up after a SIGKILL, so the shadow watches the task shell that started it.
        self.scenario({'a.shadow.*': {'sleep': 60}, 'a.qa.*': {'sleep': 60}})
        p = self.plan_of([('a', '-', 'a.txt')], '--shadow-qa', 'sol', '--shadow-rate', '1')
        run = subprocess.Popen(['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'run', str(p)], cwd=self.repo,
                               env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: run.poll() is None and run.kill())
        shadow = p / 'shadow' / 'a' / '1-1'
        self.wait_for(lambda: (shadow / 'shadow.out').exists(), 30)
        launcher = subprocess.run(['pgrep', '-f', 'forge-shadow.py run --plan %s' % p], capture_output=True,
                                  text=True).stdout.split()
        self.assertEqual(len(launcher), 1, launcher)
        task_shell = int(subprocess.run(['ps', '-o', 'ppid=', '-p', launcher[0]], capture_output=True,
                                        text=True).stdout.strip())
        os.kill(task_shell, 9)
        self.wait_for(lambda: (shadow / 'shadow.status').exists(), 30)
        self.assertEqual((shadow / 'shadow.status').read_text().strip(), 'orphaned')
        self.wait_for(lambda: not subprocess.run(['pgrep', '-f', str(shadow)], capture_output=True).stdout, 15)

    def wait_for(self, condition, seconds):
        deadline = time.time() + seconds
        while not condition():
            self.assertLess(time.time(), deadline, 'timed out waiting')
            time.sleep(0.2)

    def plan_rows(self, name, rows, *flags):
        p = self.root / name
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join('%s\t-\tlow\t%s.txt\t%s\topus\t%s\n' % (i, i, dw, i.upper()) for i, dw in rows))
        for i, _ in rows:
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        return p, self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)

    def test_exploratory_routing_records_each_decision_with_its_odds(self):
        # Rows left to forge sometimes get their neighbouring tier's model from the pools given
        # to this plan, never a model nobody named, and each decision keeps the probability it
        # had. A seed makes the draws repeatable: with it, a and b are explored, c and d keep
        # the pool's turn.
        from forge_dataset import export, routing
        rows = [('a', '-'), ('b', '-'), ('c', '-'), ('d', '-'), ('e', 'sol:high')]
        pools = ('--dwarf-low', 'sol,luna', '--dwarf-high', 'astra')
        column = lambda p: {line.split('\t')[0]: line.split('\t')[4] for line in (p / 'tasks.tsv').read_text().splitlines()
                            if line and not line.startswith('#')}
        today, _ = self.plan_rows('today', rows, *pools)
        off, r = self.plan_rows('off', rows, *pools, '--explore', '0')
        self.assertEqual(((off / 'tasks.tsv').read_text(), list(off.rglob('route.*'))), ((today / 'tasks.tsv').read_text(), []))
        self.assertEqual(self.plan_rows('bad', rows, *pools, '--explore', '0.7')[1].returncode, 2)

        self.env['FORGE_ROUTE_EXPLORE_SEED'] = '3'
        p, r = self.plan_rows('plan', rows, *pools, '--explore', '0.5')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(column(p), {'a': 'astra', 'b': 'astra', 'c': 'sol', 'd': 'luna', 'e': 'sol:high'})
        self.assertEqual(column(today), {'a': 'sol', 'b': 'luna', 'c': 'sol', 'd': 'luna', 'e': 'sol:high'})
        decisions = {t: [json.loads(l) for l in (p / 'tasks' / t / 'route.jsonl').read_text().splitlines()]
                     for t in 'abcd'}
        self.assertFalse((p / 'tasks' / 'e' / 'route.jsonl').exists())
        for t, [d] in decisions.items():
            # Exactly two candidates: the pool's turn and the next tier up (low has no cheaper one).
            self.assertEqual((d['candidates'], d['default'], d['chosen']),
                             ([column(today)[t], 'astra'], column(today)[t], column(p)[t]))
            self.assertAlmostEqual(d['propensity'], 0.5 * (d['chosen'] == d['default']) + 0.5 / 2, places=5)
        self.assertEqual({t for t, [d] in decisions.items() if d['explored']}, {'a', 'b'})
        self.assertIn('astra*', r.stdout)
        self.assertIn('picked by exploratory routing (--explore 0.5)', r.stdout)
        # A re-plan sees resolved rows as the user's: no new draw, nothing re-labelled.
        self.assertEqual(self.run_script('forge-parallel.sh', 'plan', p, '--no-memory', *pools, '--explore', '0.5').returncode, 0)
        self.assertEqual([len((p / 'tasks' / t / 'route.jsonl').read_text().splitlines()) for t in 'abcd'], [1] * 4)
        # An approval-gate edit after the draw: b now runs sol, so its odds describe a choice that never ran.
        (p / 'tasks.tsv').write_text((p / 'tasks.tsv').read_text().replace('b\t-\tlow\tb.txt\tastra', 'b\t-\tlow\tb.txt\tsol'))
        self.assertEqual(self.run_script('forge-parallel.sh', 'plan', p, '--no-memory', *pools).returncode, 0)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        shutil.rmtree(p)

        stats = {}
        cases = {c['task']: c for c in export.build(store.connect(), slices=('routing',), stats=stats)}
        self.assertEqual({t: (c['meta']['route'] or {}).get('followed') for t, c in cases.items()},
                         {'a': True, 'b': False, 'c': True, 'd': True, 'e': None})
        self.assertNotIn('route', cases['a']['state'])
        cohort = {a['task']: (a['explored'], a['followed'], a['first_pass']) for a in stats['assignments']}
        self.assertEqual(cohort, {'a': (True, True, True), 'b': (True, False, True), 'c': (False, True, True),
                                  'd': (False, True, True)})
        # The routing table compares within each fixed pair, every task in the arm it was assigned:
        # b stays in astra's arm although the gate gave it sol, and says so.
        pairs = {(r['default'], r['alternative']): r for r in routing.randomized(
            routing.tasks(export.Runs(export.Store(store.connect()))))}
        self.assertEqual(set(pairs), {('sol', 'astra'), ('luna', 'astra')})
        arms = {pair: {arm: (a['assigned'], a['not_followed'], a['first_pass']) for arm, a in r['arms'].items()}
                for pair, r in pairs.items()}
        self.assertEqual(arms, {('sol', 'astra'): {'default': (1, 0, 1.0), 'alternative': (1, 0, 1.0)},
                                ('luna', 'astra'): {'default': (1, 0, 1.0), 'alternative': (1, 1, 1.0)}})
        self.assertTrue(all(r['verdict'].startswith('not enough randomized data (1/30') for r in pairs.values()))

    def test_exploration_is_on_by_default_and_an_explicit_zero_is_kept(self):
        self.env.pop('FORGE_ROUTE_EXPLORE')       # the suite turns it off; a user's shell does not
        rows = [('a', '-'), ('b', '-')]
        pools = ('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        on, r = self.plan_rows('on', rows, *pools)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((on / 'explore').read_text().strip(), '0.2')
        for t in 'ab':
            [d] = [json.loads(l) for l in (on / 'tasks' / t / 'route.jsonl').read_text().splitlines()]
            self.assertEqual((d['epsilon'], d['candidates']), (0.2, ['sol', 'astra']))
        self.assertIn('exploration on (--explore 0.2)', r.stdout)
        # The odds are printed with a dot whatever the user's number locale says.
        self.env['LC_ALL'] = 'it_IT.UTF-8'
        _, r = self.plan_rows('on-it', rows, *pools)
        self.env.pop('LC_ALL')
        self.assertIn('probability 0.1,', r.stdout)
        off, r = self.plan_rows('off', rows, *pools, '--explore', '0')
        self.assertNotIn('exploration on', r.stdout)
        (off / 'tasks.tsv').write_text((off / 'tasks.tsv').read_text().replace('\tsol\t', '\t-\t'))
        r = self.run_script('forge-parallel.sh', 'plan', off, '--no-memory', *pools)   # a re-plan, no flag
        self.assertEqual(((off / 'explore').read_text().strip(), list(off.rglob('route.*'))), ('0', []), r.stdout)
        # A row whose tier has no neighbour in the pools is not explored at all.
        alone, r = self.plan_rows('alone', rows, '--dwarf-low', 'sol')
        self.assertEqual(list(alone.rglob('route.*')), [])
        self.assertNotIn('exploration on', r.stdout)   # nothing could be drawn, so nothing to announce
        # Nor when every row names its dwarf: exploration only touches rows left to forge.
        named, r = self.plan_rows('named', [('a', 'sol'), ('b', 'astra')], *pools)
        self.assertEqual((r.returncode, list(named.rglob('route.*'))), (0, []), r.stdout)
        self.assertNotIn('exploration on', r.stdout)
        # Effort counts: pools that differ only in effort still explore, which is how the
        # routing table measures whether an effort was needed. The same spec twice does not.
        effort, r = self.plan_rows('effort', rows, '--dwarf-low', 'sol', '--dwarf-high', 'sol:xhigh')
        [d] = [json.loads(l) for l in (effort / 'tasks' / 'a' / 'route.jsonl').read_text().splitlines()]
        self.assertEqual(d['candidates'], ['sol', 'sol:xhigh'])
        same, r = self.plan_rows('same', rows, '--dwarf-low', 'sol', '--dwarf-high', 'sol')
        self.assertEqual(list(same.rglob('route.*')), [])

    def test_a_qa_list_round_robins_within_its_tier(self):
        # The QA flags share the dwarf's grammar. A list was written into the column verbatim
        # and reached dispatch as one spec ('opus,sol' is not a known alias): no review ran.
        p = self.root / 'qapool'
        rows = [('a', 'low'), ('b', 'low'), ('c', 'low'), ('d', 'high')]
        (p / 'tasks').mkdir(parents=True)
        (p / 'tasks.tsv').write_text(''.join('%s\t-\t%s\t%s.txt\tsol\t-\t%s\n' % (i, tier, i, i.upper()) for i, tier in rows))
        for i, _ in rows:
            (p / 'tasks' / i).mkdir()
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        plan = lambda: self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory',
                                       '--qa', 'opus,sol:xhigh', '--qa-high', 'astra')
        r = plan()
        self.assertEqual(r.returncode, 0, r.stdout)
        qa = {l.split('\t')[0]: l.split('\t')[5] for l in (p / 'tasks.tsv').read_text().splitlines()
              if l and not l.startswith('#')}
        self.assertEqual(qa, {'a': 'opus', 'b': 'sol:xhigh', 'c': 'opus', 'd': 'astra'})
        # A list in an explicit column is refused by plan, rather than by the first review.
        (p / 'tasks.tsv').write_text((p / 'tasks.tsv').read_text().replace('\tastra\t', '\topus,astra\t'))
        r = plan()
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("the qa column names one spec, not a list ('opus,astra')", r.stdout)

    def test_an_escalation_is_recorded_at_the_moment_the_model_changes(self):
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'}})
        p = self.plan_of([('a', '-', 'a.txt')])
        self.run_script('forge-parallel.sh', 'run', p)
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--dwarf', 'luna')
        self.assertEqual(r.returncode, 0, r.stdout)
        [e] = [e for e in self.events(kind='event') if e['data']['name'] == 'escalation']
        self.assertEqual((e['task'], e['data']['from'], e['data']['to'], e['data']['after_attempt']),
                         ('a', 'sol', 'luna', '1'))

    def commit_at(self, when, message, files=None):
        env = dict(self.env, GIT_AUTHOR_DATE='@%d +0000' % when, GIT_COMMITTER_DATE='@%d +0000' % when)
        for name, content in (files or {}).items():
            if content is None:
                subprocess.run(['git', 'rm', '-q', name], cwd=self.repo, env=env, check=True)
            else:
                (self.repo / name).write_text(content)
                subprocess.run(['git', 'add', name], cwd=self.repo, env=env, check=True)
        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', message], cwd=self.repo, env=env, check=True)
        return self.git('rev-parse', 'HEAD').decode().strip()

    def test_integrated_work_is_followed_into_the_history_that_comes_after(self):
        # The one outcome no reviewer decides: whether the lines a task added are still there.
        # Later commits get frozen dates around a one-day horizon after the integration.
        from forge_dataset import export, outcomes
        self.scenario({'a.dwarf.*': {'content': 'one\ntwo\nthree\nfour'}, 'b.dwarf.*': {'content': 'kept'}})
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')])
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        shutil.rmtree(p)
        db = store.connect()
        [(at, integration)] = [(ts, json.loads(d)) for ts, d in db.execute("SELECT ts, data FROM events WHERE kind='event'")
                               if json.loads(d)['name'] == 'integrate_advanced']
        db.close()
        [attempt_a] = self.attempts('a')
        rewrite = self.commit_at(at + 3600, 'reword one line', {'a.txt': 'one\nTWO\nthree\nfour\n'})
        revert = self.commit_at(at + 7200, 'Revert "a"\n\nThis reverts commit %s.' % attempt_a['data']['reviewed_commit'])
        self.commit_at(at + 3 * 86400, 'after the horizon', {'b.txt': None})
        index = (self.repo / '.git' / 'index').stat().st_mtime_ns

        found, counts = outcomes.harvest(horizon_days=1, include_temp=True, now=at + 10 * 86400)
        by_task = {e['task']: e['data'] for _, e in found}
        self.assertEqual({t: (d['status'], d['added'], d['surviving'], d['horizon_rev']) for t, d in by_task.items()},
                         {'a': ('ok', 4, 3, revert), 'b': ('ok', 1, 1, revert)})
        self.assertEqual((by_task['a']['reverted_by'], by_task['b']['reverted_by']), ([revert], []))
        self.assertEqual((self.repo / '.git' / 'index').stat().st_mtime_ns, index)
        self.assertNotEqual(rewrite, revert)

        # A horizon the history has not reached yet is not an outcome: nothing is written.
        self.assertEqual(outcomes.harvest(horizon_days=30, include_temp=True, now=at + 86400)[1]['pending'], 2)
        args = type('A', (), dict(horizon_days=1, repo=None, include_temp=True, dry_run=True))()
        before = len(self.events())
        with contextlib.redirect_stdout(io.StringIO()), patch.object(outcomes.time, 'time', lambda: at + 10 * 86400):
            outcomes.main(args)
            self.assertEqual(len(self.events()), before)
            args.dry_run = False
            outcomes.main(args)
            self.assertEqual(len(self.events(kind='outcome')), 2)
            outcomes.main(args)
        self.assertEqual(len(self.events(kind='outcome')), 2)

        reviews = {c['task']: c for c in export.build(store.connect(), slices=('review',)) if 'survives' in c['expected']}
        self.assertEqual({t: c['expected']['survives'] for t, c in reviews.items()}, {'a': False, 'b': True})

        # A branch reset past the integration can no longer say what survived: lost, not failed.
        self.git('reset', '-q', '--hard', integration['parent'])
        found, _ = outcomes.harvest(horizon_days=2, include_temp=True, now=at + 10 * 86400)
        self.assertEqual({e['data']['status'] for _, e in found}, {'lost'})
        self.assertEqual(self.errors(), '')


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

    def test_fractal_import_reads_the_default_state_dir_when_xdg_state_home_is_empty(self):
        # XDG says an empty XDG_STATE_HOME means unset; forge stores Fractal runs under
        # ~/.local/state then, so --fractal must not look in the working directory.
        home, cwd = Path(self.tmp.name) / 'home', Path(self.tmp.name) / 'cwd'
        cwd.mkdir()
        task = home / '.local' / 'state' / 'forge' / 'fractal' / 'runs' / 'run-x' / 'tasks' / 'task-1'
        (task / 'nodes' / 'root').mkdir(parents=True)
        (task / 'request.json').write_text(json.dumps(dict(repo='/r', output='/o')))
        (task / 'nodes' / 'root' / 'node.json').write_text(json.dumps(dict(id='root', iteration=0)))
        from forge_dataset import importer
        args = type('A', (), dict(paths=[], registry=False, fractal=True, ledger=[], include_temp=True,
                                  dry_run=False))()
        previous = os.getcwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, previous)
        with patch.dict(os.environ, {'XDG_STATE_HOME': '', 'HOME': str(home)}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(args), 0)
        self.assertEqual([e['task'] for e in self.events(kind='fractal_node')], ['task-1/root'])


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


class ReviewerSessionTests(StoreCase):
    """A review that continued a session is tagged and kept out of reviewer statistics.

    Attempt dirs are built by hand. The session files are what live capture archives
    (`<role>.session`, `<role>.continue`); nothing here drives the session-rounds runner.
    """

    def import_dir(self, path):
        from forge_dataset import importer
        args = type('A', (), dict(paths=[str(path)], registry=False, fractal=False, ledger=[],
                                  include_temp=True, dry_run=False))()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(importer.main(args), 0)

    def status_text(self) -> str:
        from forge_dataset import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(['status']), 0)
        return buf.getvalue()

    def test_harvest_tags_a_continued_session_a_failed_continue_and_a_fresh_dispatch(self):
        from forge_dataset import harvest
        root = Path(self.tmp.name) / 'root'
        root.mkdir()

        def harvest_one(name, *, continue_body=None, session=None, metrics_session=None):
            attempt = Path(self.tmp.name) / name
            attempt.mkdir()
            metrics = dict(exit_code=0, harness='codex')
            if metrics_session is not None:
                metrics['session_id'] = metrics_session
            (attempt / 'metrics.json').write_text(json.dumps(metrics))
            (attempt / 'qa.last').write_text('FORGE_VERDICT: FAIL\n')
            if session is not None:
                (attempt / 'qa.session').write_text(session + '\n')
            if continue_body is not None:
                (attempt / 'qa.continue').write_text(continue_body)
            event = harvest.dispatch_event(attempt, root, 'qa', 'codex', 0,
                                           dict(run='r', task='a', attempt_now=1))
            return event['data']['session_id'], event['data']['session_continue']

        self.assertEqual(harvest_one('continued', session='sess-1',
                                     continue_body='requested=old\nmode=continued\nreason=ok\n'),
                         ('sess-1', 'continued'))
        self.assertEqual(harvest_one('failed', session='sess-2',
                                     continue_body='requested=old\nmode=failed\nreason=exit\n'),
                         ('sess-2', 'failed'))
        self.assertEqual(harvest_one('unknown', continue_body='mode=sideways\n')[1], 'failed')
        self.assertEqual(harvest_one('no-mode', continue_body='requested=old\n')[1], 'failed')
        self.assertEqual(harvest_one('absent'), (None, None))
        self.assertEqual(harvest_one('from-metrics', metrics_session='from-metrics'), ('from-metrics', None))
        # The archived file is the id that was published; metrics only fills a gap.
        self.assertEqual(harvest_one('both', session='from-file', metrics_session='from-metrics')[0], 'from-file')

    def import_shadowed_reviews(self, tasks):
        """Import one plan of shadowed reviews and mark each shadow done.

        `tasks` is (task id, title, reviewed commit, reviews). A review is
        (attempt dir name, verdict, continue mode or None, session id or None,
        exit code, wall_started). Directory names are the import order;
        wall_started is the time.
        """
        plan = Path(self.tmp.name) / 'plan'
        plan.mkdir()
        (plan / 'dataset.uid').write_text('uid-session\n')
        (plan / 'tasks.tsv').write_text(''.join(
            '%s\t-\tlow\ta.txt\tsol\topus\t%s\n' % (tid, title)
            for tid, title, _, _ in tasks))
        (plan / 'repo').write_text('/nonexistent/repo\n')
        (plan / 'run_id').write_text('session-run\n')
        (plan / 'goal.txt').write_text('goal\n')

        def prompt(tid):
            diff = 'diff --git a/%s.txt b/%s.txt\n+%s' % (tid, tid, tid)
            return 'review this\n```diff\n%s\n```\n' % diff

        def qa(tdir, name, verdict, mode, session, exit_code, started):
            attempt = tdir / 'attempts' / name
            attempt.mkdir(parents=True)
            metrics = dict(role='qa', wall_started=started, exit_code=exit_code, harness='codex',
                           elapsed_s=dict(total=1), input_tokens=11, output_tokens=7)
            if session:
                metrics['session_id'] = session
                (attempt / 'qa.session').write_text(session + '\n')
            (attempt / 'metrics.json').write_text(json.dumps(metrics))
            (attempt / 'qa.resolved').write_text('alias=opus\nharness=codex\nmodel=primary\n')
            (attempt / 'qa.last').write_text('FORGE_VERDICT: %s\n' % verdict)
            (attempt / 'qa.prompt').write_text(prompt(tdir.name))
            if mode is not None:
                (attempt / 'qa.continue').write_text('requested=prev\nmode=%s\nreason=round\n' % mode)

        def task(tid, commit, reviews):
            tdir = plan / 'tasks' / tid
            tdir.mkdir(parents=True)
            (tdir / 'status').write_text('PASS\n')
            (tdir / 'attempt').write_text('1\n')
            (tdir / 'reviewed.commit').write_text(commit + '\n')
            (tdir / 'changes.diff').write_text('diff --git a/%s.txt b/%s.txt\n+%s\n' % (tid, tid, tid))
            # The dispatch rid is the attempt directory name, so names are unique across tasks.
            task_attempt = tdir / 'attempts' / ('task-%s' % tid)
            task_attempt.mkdir(parents=True)
            (task_attempt / 'metrics.json').write_text(json.dumps(
                dict(role='task', wall_started=1.0, exit_code=0, harness='pipeline', elapsed_s=dict(total=1))))
            for spec in reviews:
                qa(tdir, *spec)
            shadow = plan / 'shadow' / tid / '1-1' / 'attempts' / ('qa-shadow-%s' % tid)
            shadow.mkdir(parents=True)
            (shadow / 'metrics.json').write_text(json.dumps(
                dict(role='qa', wall_started=9.0, exit_code=0, harness='codex', elapsed_s=dict(total=1))))
            (shadow / 'qa.resolved').write_text('alias=sol\nharness=codex\nmodel=shadow-model\n')
            (shadow / 'qa.last').write_text('FORGE_VERDICT: FAIL\n')
            (shadow / 'qa.prompt').write_text(prompt(tid))
            (shadow / 'dataset.json').write_text(json.dumps(dict(
                run='uid-session', run_id='session-run', mode='parallel', scope='task', task=tid,
                shadow=dict(attempt=1, reviewed=commit, primary='opus'))))

        for tid, _, commit, reviews in tasks:
            task(tid, commit, reviews)
        self.import_dir(plan)
        from forge_dataset.capture import point
        for tid, _, commit, _ in tasks:
            point('shadow_review', str(plan / 'shadow' / tid / '1-1'), tid,
                  dict(status='done', attempt=1, reviewed=commit))

    def test_a_failed_continue_then_a_fresh_fallback_keeps_only_the_fresh_review(self):
        # One round, three shapes, each with a shadow of the same attempt number:
        #   continued — the only primary continued, so it leaves the slices and the pairs
        #   rerun     — a fresh PASS, then a continued QA-only re-run; the last verdict
        #               continued, so the pair is dropped and the fresh PASS stays, alone
        #   fallback  — mode=failed (exit 1, verdict FAIL), then a fresh PASS; the fresh
        #               one is the last verdict and is the pair
        self.import_shadowed_reviews([
            ('continued', 'continued only', 'commit-continued', [
                ('qa-c1', 'FAIL', 'continued', 'sess-c', 0, 2.0)]),
            ('rerun', 'fresh then continued', 'commit-rerun', [
                ('qa-r1', 'PASS', None, 'sess-fresh', 0, 2.0),
                ('qa-r2', 'FAIL', 'continued', 'sess-again', 0, 3.0)]),
            ('fallback', 'failed then fresh', 'commit-fallback', [
                ('qa-f1', 'FAIL', 'failed', 'sess-failed', 1, 2.0),
                ('qa-f2', 'PASS', None, None, 0, 4.0)]),
        ])

        tagged = {(e['task'], e['data']['verdict'], e['data']['session_continue'], e['data']['session_id'])
                  for e in self.events(kind='dispatch') if e['data']['role'] == 'qa'}
        self.assertEqual(tagged, {
            ('continued', 'FAIL', 'continued', 'sess-c'),
            ('rerun', 'PASS', None, 'sess-fresh'),
            ('rerun', 'FAIL', 'continued', 'sess-again'),
            ('fallback', 'FAIL', 'failed', 'sess-failed'),
            ('fallback', 'PASS', None, None),
        })
        self.assertEqual({e['task']: e['data'].get('reviewed_commit') for e in self.events(kind='attempt')},
                         {'continued': 'commit-continued', 'rerun': 'commit-rerun', 'fallback': 'commit-fallback'})

        from forge_dataset import export
        runs = export.Runs(export.Store(store.connect()))
        cases = {c['task']: c for c in export.review_cases(runs)}
        self.assertNotIn('continued', cases)
        self.assertEqual(cases['rerun']['expected']['passes'], True)
        self.assertNotIn('shadow', cases['rerun']['meta'])
        self.assertEqual(cases['fallback']['expected']['passes'], True)
        self.assertEqual(cases['fallback']['meta']['shadow']['verdict'], 'FAIL')
        self.assertIs(cases['fallback']['meta']['consensus'], False)
        self.assertEqual(list(export.citation_cases(runs, 7)), [])

        pairs = export.shadow_pairs(runs)
        self.assertEqual(set(pairs), {('uid-session', 'fallback', 1)})
        primary, shadow_event = pairs[('uid-session', 'fallback', 1)]
        self.assertEqual(primary['d']['verdict'], 'PASS')
        self.assertIsNone(primary['d']['session_continue'])
        self.assertEqual(shadow_event['d']['verdict'], 'FAIL')
        agreement = export.agreement(runs)
        self.assertEqual((agreement['pairs'], agreement['agree']), (1, 0))

    def test_an_earlier_fresh_review_imported_later_is_not_the_one_the_attempt_ended_on(self):
        # qa-ffff starts at 1.0 and qa-aaaa (continued) starts at 2.0. Import walks
        # attempt dirs lexicographically, so the later continued review is stored first.
        self.import_shadowed_reviews([
            ('later-continued', 'continued imported first', 'commit-later-continued', [
                ('qa-ffff', 'PASS', None, 'sess-fresh', 0, 1.0),
                ('qa-aaaa', 'FAIL', 'continued', 'sess-again', 0, 2.0)]),
        ])
        from forge_dataset import export
        runs = export.Runs(export.Store(store.connect()))
        qa = runs.dispatches[('uid-session', 'later-continued', 'qa')]
        self.assertEqual({(e['d']['wall_started'], e['d']['session_continue'], e['d']['verdict'])
                          for e in qa},
                         {(1.0, None, 'PASS'), (2.0, 'continued', 'FAIL')})
        pairs = export.shadow_pairs(runs)
        self.assertEqual([(p['d']['wall_started'], p['d']['session_continue'])
                          for p, _ in pairs.values()], [])
        self.assertEqual([(e['d']['wall_started'], e['d']['session_continue']) for e in qa],
                         [(1.0, None), (2.0, 'continued')])

    def test_an_earlier_failed_continue_imported_later_does_not_drop_the_fresh_fallback(self):
        # qa-ffff is a failed continue at 1.0; qa-aaaa is the fresh fallback at 2.0.
        # Lexicographic import stores the fresh review first and the failed continue last.
        self.import_shadowed_reviews([
            ('later-fresh', 'fresh imported first', 'commit-later-fresh', [
                ('qa-ffff', 'FAIL', 'failed', 'sess-failed', 1, 1.0),
                ('qa-aaaa', 'PASS', None, None, 0, 2.0)]),
        ])
        from forge_dataset import export
        runs = export.Runs(export.Store(store.connect()))
        qa = runs.dispatches[('uid-session', 'later-fresh', 'qa')]
        self.assertEqual({(e['d']['wall_started'], e['d']['session_continue'], e['d']['verdict'])
                          for e in qa},
                         {(1.0, 'failed', 'FAIL'), (2.0, None, 'PASS')})
        pairs = export.shadow_pairs(runs)
        self.assertEqual(set(pairs), {('uid-session', 'later-fresh', 1)})
        primary, shadow_event = pairs[('uid-session', 'later-fresh', 1)]
        self.assertEqual(primary['d']['wall_started'], 2.0)
        self.assertEqual(primary['d']['verdict'], 'PASS')
        self.assertIsNone(primary['d']['session_continue'])
        self.assertEqual(shadow_event['d']['verdict'], 'FAIL')
        self.assertEqual([(e['d']['wall_started'], e['d']['session_continue']) for e in qa],
                         [(1.0, 'failed'), (2.0, None)])

    def test_status_counts_continued_reviews_with_no_shadow_dispatch(self):
        plan = Path(self.tmp.name) / 'counts'
        plan.mkdir()
        (plan / 'dataset.uid').write_text('uid-counts\n')
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (plan / 'repo').write_text('/nonexistent/repo\n')
        (plan / 'run_id').write_text('counts\n')
        tdir = plan / 'tasks' / 'a'
        tdir.mkdir(parents=True)
        (tdir / 'attempt').write_text('1\n')

        def dispatch(name, mode, started, role='qa'):
            attempt = tdir / 'attempts' / name
            attempt.mkdir(parents=True)
            (attempt / 'metrics.json').write_text(json.dumps(
                dict(role=role, wall_started=started, exit_code=0, harness='claude', elapsed_s=dict(total=1))))
            (attempt / (role + '.last')).write_text('FORGE_VERDICT: PASS\n' if role == 'qa' else 'implemented\n')
            (attempt / (role + '.session')).write_text('sess\n')
            if mode is not None:
                (attempt / (role + '.continue')).write_text('requested=prev\nmode=%s\n' % mode)

        dispatch('qa-0001', 'continued', 1.0)
        dispatch('qa-0002', 'failed', 2.0)
        dispatch('qa-0003', 'sideways', 3.0)   # an unknown mode is a request that failed
        dispatch('qa-0004', None, 4.0)         # a fresh review is neither count
        # plan --implementer-session: an implementer's continues are counted apart from reviews.
        dispatch('dwarf-0001', 'continued', 5.0, role='dwarf')
        dispatch('dwarf-0002', 'continued', 6.0, role='dwarf')
        dispatch('dwarf-0003', 'failed', 7.0, role='dwarf')
        self.import_dir(plan)
        text = self.status_text()
        self.assertIn('\n  continued reviews: 1\n', '\n' + text)
        self.assertIn('\n  failed continues: 2\n', '\n' + text)
        self.assertIn('\n  continued implementers: 2\n', '\n' + text)
        self.assertIn('\n  failed implementer continues: 1\n', '\n' + text)
        self.assertNotIn('shadow:', text)

    def test_status_is_quiet_when_every_review_is_fresh(self):
        plan = Path(self.tmp.name) / 'fresh'
        plan.mkdir()
        (plan / 'dataset.uid').write_text('uid-fresh\n')
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (plan / 'repo').write_text('/nonexistent/repo\n')
        (plan / 'run_id').write_text('fresh\n')
        attempt = plan / 'tasks' / 'a' / 'attempts' / 'qa-0001'
        attempt.mkdir(parents=True)
        (attempt / 'metrics.json').write_text(json.dumps(
            dict(role='qa', wall_started=1.0, exit_code=0, harness='codex')))
        (attempt / 'qa.last').write_text('FORGE_VERDICT: PASS\n')
        self.import_dir(plan)
        text = self.status_text()
        self.assertNotIn('continued reviews:', text)
        self.assertNotIn('failed continues:', text)
        self.assertNotIn('shadow:', text)

    def test_reviewer_session_changes_the_plan_id_only_when_the_file_is_present(self):
        plan = Path(self.tmp.name) / 'plan'
        (plan / 'tasks' / 'a').mkdir(parents=True)
        (plan / 'goal.txt').write_text('keep the plan id\n')
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA title\n')
        (plan / 'run_id').write_text('plan-rid\n')
        (plan / 'tasks' / 'a' / 'prompt.md').write_text('do the thing\n')
        (plan / 'tasks' / 'a' / 'approach.md').write_text('carefully\n')
        from forge_dataset import harvest
        bare = harvest.plan_event(plan, dict(run='fixed-run'), src='import', inventory=False)
        # The id of this fixture before reviewer_session joined the plan hash. A plan
        # that has no such file must keep it, so a re-import adds nothing.
        self.assertEqual(bare['rid'], 'plan:fixed-run:26062ad6ab65e2f6')
        self.assertNotIn('reviewer_session', bare['data']['options'])
        (plan / 'reviewer_session').write_text('on\n')
        flagged = harvest.plan_event(plan, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual(flagged['data']['options'].get('reviewer_session'), 'on')
        self.assertNotEqual(flagged['rid'], 'plan:fixed-run:26062ad6ab65e2f6')


class ExportStateIdentityTests(StoreCase):
    """The routing and retry cases this fixture exported before the assemblers left forge_jev.

    The ids were captured from export.build (default seed 7) at that base. The state is what
    siblings, assemble, retry and task_size produced, and the question text is the frozen rubric.
    """

    PROMPT = 'Ship the widget.\n\n## Acceptance criteria\n\n- widget exists\n'
    FINDINGS = '- [P1][CONFIRMED] a.txt:1 — broken\n'

    def test_the_move_leaves_the_routing_and_retry_cases(self):
        from forge_dataset import export
        prompt, findings = self.PROMPT, self.FINDINGS
        goal, memory, approach = 'Make widgets.', 'Widgets live in widgets/.', 'Add a widget module.'
        snapshot = json.dumps({'a.txt': 'print(1)\n'})

        def blob(text, kind='text'):
            return store.put_blob(text, kind)

        events = [
            dict(rid='plan1', kind='plan', src='import', repo='fixture', run='run-1', mode='parallel', ts=1000,
                 data=dict(decompose_level='low', goal=goal,
                           blobs=dict(goal=blob(goal), memory=blob(memory), snapshot=blob(snapshot)),
                           tasks={'a': dict(title='Add widget', files='a.txt, b.txt', deps='sib',
                                            prompt=blob(prompt, 'prompt'), approach=blob(approach),
                                            declared_difficulty='low', difficulty_source='orchestrator',
                                            dwarf='sol:high', qa='opus:high'),
                                  'sib': dict(title='Sibling ' + 'T' * 200, files='s.txt', deps='-')})),
            dict(rid='att1', kind='attempt', src='import', repo='fixture', run='run-1', mode='parallel',
                 task='a', attempt=1, ts=1100,
                 data=dict(status='FAIL', wall_started=1100, dwarf='sol:high', qa='opus:high')),
            dict(rid='d1', kind='dispatch', src='import', repo='fixture', run='run-1', mode='parallel',
                 task='a', attempt=1, ts=1100,
                 data=dict(role='dwarf', model='gpt-6.1-sol', effort='high', harness='codex')),
            dict(rid='q1', kind='dispatch', src='import', repo='fixture', run='run-1', mode='parallel',
                 task='a', attempt=1, ts=1100,
                 data=dict(role='qa', model='opus', effort='high', harness='claude', verdict='FAIL')),
            dict(rid='att2', kind='attempt', src='import', repo='fixture', run='run-1', mode='parallel',
                 task='a', attempt=2, ts=1200,
                 data=dict(status='PASS', wall_started=1200, dwarf='sol:high', qa='opus:high',
                           blobs=dict(retry_findings=blob(findings)))),
            dict(rid='d2', kind='dispatch', src='import', repo='fixture', run='run-1', mode='parallel',
                 task='a', attempt=2, ts=1200,
                 data=dict(role='dwarf', model='gpt-6.1-sol', effort='high', harness='codex')),
            dict(rid='q2', kind='dispatch', src='import', repo='fixture', run='run-1', mode='parallel',
                 task='a', attempt=2, ts=1200,
                 data=dict(role='qa', model='opus', effort='high', harness='claude', verdict='PASS')),
        ]
        self.assertEqual(store.record(events), len(events))
        cases = export.build(store.connect(), slices=('routing', 'retry'))
        self.assertEqual(len(cases), 2)
        by_slice = {c['slice']: c for c in cases}
        routing, retry = by_slice['routing'], by_slice['retry']

        # Captured before the move.
        self.assertEqual(routing['id'], 'routing-f23eac86963c')
        self.assertEqual(list(routing['state']),
                         ['goal', 'task', 'prompt', 'approach', 'plan_tasks', 'file_excerpts', 'memory',
                          'dwarf', 'reviewer', 'size'])
        self.assertEqual(routing['state'], dict(
            goal=goal,
            task=dict(id='a', title='Add widget', files='a.txt, b.txt', deps='sib'),
            prompt=prompt, approach=approach,
            plan_tasks=[dict(id='sib', title='Sibling ' + 'T' * 112, files='s.txt', deps='-')],
            file_excerpts={'a.txt': 'print(1)\n', 'b.txt': '(does not exist yet)'},
            memory=memory,
            dwarf=dict(model='gpt-6.1-sol', effort='high', harness='codex'),
            reviewer=dict(model='opus', effort='high', harness='claude'),
            size=dict(prompt_chars=58, declared_files=2, plan_tasks=2, deps=1, decompose_level='low')))
        self.assertEqual(routing['questions']['first_pass'], dict(
            type='noul',
            instructions=('This Forge task is dispatched to the model in state.dwarf, at the effort shown, in its '
                          'own worktree. An independent model (state.reviewer) then reviews the resulting diff '
                          'against the task. Will that FIRST attempt pass review, with no retry needed?'),
            criteria={
                'true': 'The task is within reach of this model at this effort on the first try: the '
                        'requirements are clear, the change is contained, and nothing in it is likely '
                        'to be missed or subtly wrong.',
                'false': 'The first attempt is likely to fail review: the task has edge cases, '
                         'cross-cutting effects, unclear requirements or subtle state that a first '
                         'attempt by this model at this effort typically gets wrong, so a retry would '
                         'be needed.'}))
        self.assertEqual(routing['expected'], dict(first_pass=False, tier='low'))

        self.assertEqual(retry['id'], 'retry-9aa1fee49f43')
        self.assertEqual(list(retry['state']),
                         list(routing['state']) + ['attempt', 'previous_review', 'previous_review_truncated'])
        self.assertEqual(retry['state'], dict(routing['state'], attempt=2, previous_review=findings,
                                               previous_review_truncated=False))
        self.assertEqual(retry['questions']['retry_pass'], dict(
            type='noul',
            instructions=('An earlier attempt at this Forge task failed review. The task is now dispatched again, to '
                          'the model in state.dwarf at the effort shown, with that review\'s findings '
                          '(state.previous_review) to fix; state.attempt is which try this is, and the model in '
                          'state.reviewer reviews the result. Will this attempt pass review?'),
            criteria={
                'true': 'The findings are specific and fixable, and this model at this effort can address '
                        'them without breaking something else: the retry is likely to pass.',
                'false': 'The findings point at a design problem, an unclear requirement or a recurring '
                         'mistake that another attempt by this model at this effort is likely to repeat: '
                         'the retry is likely to fail again and should be escalated or rethought.'}))
        self.assertEqual(retry['expected'], dict(retry_pass=True))


class IndependenceTests(unittest.TestCase):
    def test_only_capture_entry_points_import_the_dataset(self):
        # Nothing that builds a prompt may read earlier runs' verdicts, or the reviewer
        # stops being independent. Outside its own package, the dataset may only be
        # written to, through capture.
        allowed = {'forge-runtime.py', 'forge-schedule.py', 'forge-dataset.py', 'forge-shadow.py', 'client.py',
                   'cli.py', 'invocation.py', 'pipeline.py', 'predict.py',
                   # forge control and forge runs rm: they write to the store, or only locate it
                   'forge-control.py', 'forge-runs-rm.py'}
        for path in (ROOT / 'scripts').rglob('*'):
            if path.suffix not in ('.py', '.sh') or 'forge_dataset' in path.parts:
                continue
            text = path.read_text(errors='replace')
            if 'forge_dataset' not in text and 'forge-dataset.py' not in text:
                continue
            if path.suffix == '.sh':
                for line in text.splitlines():
                    if 'forge-dataset.py' in line:
                        # The one read: run's advisory forecast, printed to the operator's stderr only.
                        self.assertRegex(line, r'forge-dataset\.py" (event|invocation|forecast "\$1" --run 2>/dev/null)',
                                         str(path))
                continue
            if path.name == 'forge-plan-check.py':
                # task_size does not read the store. The size advisory may import it and nothing else.
                imports = [line.strip() for line in text.splitlines() if 'forge_dataset' in line and 'import' in line]
                self.assertEqual(imports, ['from forge_dataset.states import task_size  # noqa: E402'])
                continue
            if path.parent.name == 'forge_decide' and path.name == 'labels.py':
                # `forge decide report`/`replay` measure gates on the store, read-only; no prompt is built there.
                imports = [line.strip() for line in text.splitlines() if 'forge_dataset' in line and 'import' in line]
                self.assertEqual(imports, ['from forge_dataset import dataset_dir', 'from forge_dataset import export'])
                continue
            self.assertIn(path.name, allowed, path)
            for line in text.splitlines():
                if 'forge_dataset' in line and 'import' in line:
                    self.assertRegex(line, r'from forge_dataset(\.capture)? import (capture|point|jev|fractal_step|'
                                           r'fractal_outcome|mint_run_uid)|from forge_dataset\.cli import main|'
                                           r'from forge_dataset import DatasetError, data_home, dataset_dir, enabled, '
                                           r'harvest, importer, record_strict', str(path))

    def test_forge_config_locates_the_dataset_as_the_store_does(self):
        # forge config mirrors data_home() and enabled() rather than import the store; keep them in step.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp).resolve()
            cases = {'FORGE_DATA_HOME': dict(FORGE_DATA_HOME='~/fdh', XDG_DATA_HOME=str(home / 'xdg')),
                     'XDG_DATA_HOME': dict(XDG_DATA_HOME=str(home / 'xdg')),
                     'neither': {},
                     'relative XDG_DATA_HOME': dict(XDG_DATA_HOME='rel/xdg'),
                     'FORGE_DATASET=Off': dict(FORGE_DATA_HOME=str(home / 'fdh'), FORGE_DATASET=' Off ')}
            for name, extra in cases.items():
                env = {k: v for k, v in os.environ.items()
                       if k not in ('FORGE_DATA_HOME', 'XDG_DATA_HOME', 'FORGE_DATASET')}
                env.update(extra, HOME=str(home), XDG_CONFIG_HOME=str(home / 'cfg'))
                with self.subTest(name), patch.dict(os.environ, env, clear=True):
                    out = subprocess.run([str(ROOT / 'forge'), 'config', 'show', '--json'], capture_output=True,
                                         text=True, env=env, cwd=tmp)
                    self.assertEqual(out.returncode, 0, out.stderr)
                    shown = json.loads(out.stdout)['dataset']
                    self.assertEqual(shown, dict(path=str(store.dataset_dir()), enabled=store.enabled()))
                    self.assertEqual(shown['enabled'], name != 'FORGE_DATASET=Off')
                    if name in ('neither', 'relative XDG_DATA_HOME'):
                        # The XDG spec says a relative path is invalid and ignored; it is not the cwd.
                        self.assertEqual(shown['path'], str(home / '.local/share/forge/dataset'))
