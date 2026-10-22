"""Dataset preservation: strict recording, strict import, self-describing run archives.

`forge runs rm` deletes a run dir only after every event of it is in the store and an
archive exists that a later import maps back to the same run. These tests prove the three
pieces that promise it, each against a store and config home of its own.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import forge_dataset as store  # noqa: E402
from forge_dataset import capture, importer  # noqa: E402


class PreserveCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.use_store('data')
        os.environ.pop('FORGE_DATASET', None)

    def use_store(self, name: str) -> Path:
        home = self.root / name
        patcher = patch.dict(os.environ, {'FORGE_DATA_HOME': str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return home

    def rows(self, columns='rid, kind, run, task, src, data') -> list:
        path = store.dataset_dir() / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            return db.execute('SELECT %s FROM events ORDER BY rid' % columns).fetchall()
        finally:
            db.close()

    def make_run(self, name='old', uid=None, unfinished=False) -> Path:
        """A finished two-attempt plan, the way the legacy import tests build one."""
        plan = self.root / name
        tdir = plan / 'tasks' / 'a'
        tdir.mkdir(parents=True)
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (plan / 'repo').write_text('/nonexistent/repo\n')
        (plan / 'run_id').write_text(name + '\n')
        (tdir / 'status').write_text('PASS\n')
        (tdir / 'attempt').write_text('2\n')
        (tdir / 'changes.diff').write_text('diff --git a/a.txt b/a.txt\n+second\n')
        if uid:
            (plan / 'dataset.uid').write_text(uid + '\n')

        def attempt(role, at, exit_code=0, **files):
            d = tdir / 'attempts' / ('%s-%032x' % (role, int(at * 1000)))
            d.mkdir(parents=True)
            (d / 'metrics.json').write_text(json.dumps(dict(role=role, wall_started=at, exit_code=exit_code,
                                                            harness='codex' if role != 'task' else 'pipeline',
                                                            elapsed_s=dict(total=5))))
            for key, body in files.items():
                (d / key.replace('_', '.')).write_text(body)

        attempt('task', 100.0)
        attempt('dwarf', 101.0, dwarf_resolved='alias=sol\nharness=codex\nmodel=m\n')
        attempt('qa', 102.0, qa_resolved='alias=opus\n', qa_last='FORGE_VERDICT: FAIL',
                qa_prompt='review this\n```diff\ndiff --git a/a.txt b/a.txt\n+first\n```\n')
        attempt('task', 200.0)
        attempt('dwarf', 201.0, dwarf_resolved='alias=sol\n')
        attempt('qa', 202.0, qa_resolved='alias=opus\n', qa_last='FORGE_VERDICT: PASS')
        if unfinished:
            attempt('dwarf', 300.0, exit_code=None, dwarf_resolved='alias=sol\n')
        return plan


class StrictRecordingTests(PreserveCase):
    def test_an_unwritable_store_raises_for_record_strict_and_not_for_record(self):
        blocker = self.root / 'blocked'
        blocker.write_text('a file where the data home should be')
        os.environ['FORGE_DATA_HOME'] = str(blocker)
        self.assertEqual(store.record([dict(rid='x', kind='t')]), 0)
        with self.assertRaises(store.DatasetError):
            store.record_strict([dict(rid='x', kind='t')])

    def test_a_locked_database_raises_after_the_busy_timeout(self):
        self.assertEqual(store.record_strict([dict(rid='warm', kind='t')]), 1)
        holder = sqlite3.connect(str(store.dataset_dir() / 'dataset.sqlite'), timeout=0)
        holder.execute('BEGIN EXCLUSIVE')
        try:
            with patch.object(store, 'BUSY_TIMEOUT_S', 0.2):
                started = time.time()
                with self.assertRaises(store.DatasetError) as caught:
                    store.record_strict([dict(rid='blocked', kind='t')])
                self.assertGreaterEqual(time.time() - started, 0.2)
                self.assertIn('locked', str(caught.exception))
                self.assertEqual(store.record([dict(rid='blocked', kind='t')]), 0)
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual([r[0] for r in self.rows()], ['warm'])

    def test_a_refused_location_and_a_disabled_store_raise(self):
        repo = self.root / 'repo'
        (repo / '.git').mkdir(parents=True)
        os.environ['FORGE_DATA_HOME'] = str(repo / 'data')
        self.assertEqual(store.record([dict(rid='x', kind='t')], repo=repo), 0)
        with self.assertRaises(store.DatasetError):
            store.record_strict([dict(rid='x', kind='t')], repo=repo)
        self.assertFalse((repo / 'data').exists())
        self.use_store('elsewhere')
        with patch.dict(os.environ, {'FORGE_DATASET': 'off'}):
            with self.assertRaises(store.DatasetError):
                store.record_strict([dict(rid='x', kind='t')])

    def test_record_strict_counts_new_rows_and_nothing_for_duplicates(self):
        events = [dict(rid='a', kind='t'), dict(rid='b', kind='t')]
        self.assertEqual(store.record_strict(events), 2)
        self.assertEqual(store.record_strict(events + [dict(rid='c', kind='t')]), 1)
        self.assertEqual(store.record_strict([]), 0)


class ImportRunTests(PreserveCase):
    def test_a_clean_run_returns_counts_and_a_repeat_adds_nothing(self):
        run = self.make_run()
        first = importer.import_run(run)
        self.assertEqual(first['failed'], [])
        self.assertGreater(first['events'], 0)
        self.assertEqual(first['new'], first['events'])
        self.assertEqual(importer.import_run(run)['new'], 0)

    def test_an_unfinished_execution_is_reported_and_is_not_a_failure(self):
        result = importer.import_run(self.make_run(unfinished=True))
        self.assertEqual((result['failed'], result['skipped_unfinished']), ([], 1))

    def test_a_failed_item_raises_in_strict_mode_and_is_listed_otherwise(self):
        run = self.make_run()
        with patch.object(capture, 'events_for', side_effect=ValueError('unreadable')):
            result = importer.import_run(run, strict=False)
            self.assertEqual(len(result['failed']), 4)
            self.assertIn('unreadable', result['failed'][0])
            self.use_store('second')
            with self.assertRaises(store.DatasetError) as caught:
                importer.import_run(run)
        self.assertEqual(len(caught.exception.result['failed']), 4)
        # What could be read was still written, so a retry only has the rest to do.
        self.assertGreater(caught.exception.result['new'], 0)

    def test_a_failed_write_raises_in_strict_mode(self):
        run = self.make_run()
        blocker = self.root / 'blocked'
        blocker.write_text('x')
        os.environ['FORGE_DATA_HOME'] = str(blocker)
        with self.assertRaises(store.DatasetError) as caught:
            importer.import_run(run)
        self.assertTrue(any(f.startswith('store:') for f in caught.exception.result['failed']))
        # Lenient mode keeps the old contract: nothing stored, nothing raised.
        self.assertEqual(importer.import_run(run, strict=False)['new'], 0)

    def test_a_missing_path_and_a_garbled_archive_are_failures(self):
        with self.assertRaises(store.DatasetError):
            importer.import_run(self.root / 'nowhere')
        bad = self.root / 'bad.tar.gz'
        bad.write_bytes(b'not a tarball')
        with self.assertRaises(store.DatasetError):
            importer.import_run(bad)


class ArchiveTests(PreserveCase):
    def names(self, archive):
        with tarfile.open(archive) as bundle:
            return bundle.getnames()

    def test_the_layout_carries_identity_and_lists_what_it_left_out(self):
        run = self.make_run(uid='abc123')
        (run / 'tasks' / 'a' / 'review-1-1').mkdir()
        (run / 'tasks' / 'a' / 'review-1-1' / 'qa.last').write_text('x')
        (run / 'node_modules').mkdir()
        (run / 'node_modules' / 'dep.js').write_text('x')
        with open(run / 'huge.bin', 'wb') as handle:
            handle.truncate(51 << 20)
        (run / 'link').symlink_to(run / 'run_id')
        dest = self.root / 'out' / 'old.tar.gz'
        result = importer.archive_run(run, dest)
        self.assertEqual(result['path'], str(dest))
        self.assertEqual(result['bytes'], dest.stat().st_size)
        self.assertEqual(sorted(result['excluded']),
                         ['huge.bin', 'link', 'node_modules/', 'tasks/a/review-1-1/'])
        names = self.names(dest)
        self.assertEqual({n.split('/')[0] for n in names}, {'FORGE_ARCHIVE.json', 'FORGE_ARCHIVE_EXCLUDED.txt', 'old'})
        self.assertIn('old/tasks.tsv', names)
        self.assertFalse([n for n in names if 'review-1-1' in n or 'huge' in n or 'node_modules' in n or 'link' in n])
        with tarfile.open(dest) as bundle:
            meta = json.load(bundle.extractfile('FORGE_ARCHIVE.json'))
            listed = bundle.extractfile('FORGE_ARCHIVE_EXCLUDED.txt').read().decode().split()
        self.assertEqual(sorted(listed), sorted(result['excluded']))
        self.assertEqual((meta['format'], meta['original_path'], meta['run_id'], meta['dataset_uid']),
                         (1, os.path.realpath(run), 'old', 'abc123'))
        self.assertAlmostEqual(meta['created'], time.time(), delta=60)
        self.assertEqual([p.name for p in dest.parent.iterdir()], ['old.tar.gz'])

    def test_a_run_without_a_uid_records_null(self):
        dest = self.root / 'old.tar.gz'
        importer.archive_run(self.make_run(), dest)
        with tarfile.open(dest) as bundle:
            self.assertIsNone(json.load(bundle.extractfile('FORGE_ARCHIVE.json'))['dataset_uid'])

    def test_a_destination_inside_the_run_is_refused(self):
        run = self.make_run()
        for dest in (run / 'x.tar.gz', run / 'tasks' / 'x.tar.gz', run):
            with self.assertRaises(store.DatasetError):
                importer.archive_run(run, dest)
        self.assertFalse((run / 'x.tar.gz').exists())

    @unittest.skipIf(os.geteuid() == 0, 'root reads every directory')
    def test_an_unreadable_subdirectory_fails_the_archive_instead_of_being_dropped(self):
        run = self.make_run()
        hidden = run / 'tasks' / 'hidden'
        hidden.mkdir(parents=True)
        (hidden / 'attempts.txt').write_text('x')
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o755)
        dest = self.root / 'out' / 'a.tar.gz'
        with self.assertRaises(store.DatasetError):
            importer.archive_run(run, dest)
        self.assertFalse(dest.exists())

    def test_a_failed_write_leaves_no_partial_archive(self):
        run = self.make_run()
        dest = self.root / 'out' / 'old.tar.gz'
        with patch.object(tarfile.TarFile, 'add', side_effect=OSError('disk full')):
            with self.assertRaises(store.DatasetError):
                importer.archive_run(run, dest)
        self.assertEqual(list(dest.parent.iterdir()), [])

    def identity_roundtrip(self, uid):
        run = self.make_run(uid=uid)
        original = os.path.realpath(run)
        self.assertEqual(importer.import_run(run)['failed'], [])
        before = self.rows('rid, run, data')
        self.assertTrue(before)
        archive = self.root / 'kept' / 'old.tar.gz'
        importer.archive_run(run, archive)
        shutil.rmtree(run)
        result = importer.import_run(archive)
        self.assertEqual((result['events'], result['new']), (len(before), 0))
        self.assertEqual(self.rows('rid, run, data'), before)
        runs = {r[1] for r in before}
        self.assertEqual(len(runs), 1)
        if uid:
            self.assertEqual(runs, {uid})
        else:
            self.assertTrue(next(iter(runs)).startswith('legacy-'))
        self.assertTrue(any(original in r[2] for r in before))
        self.assertFalse(any('forge-dataset-import-' in r[2] for r in before))
        # A store that never saw the dir gets the same identities from the archive alone.
        self.use_store('fresh')
        importer.import_run(archive)
        self.assertEqual(self.rows('rid, run, data'), before)

    def test_an_archive_imports_as_the_run_it_was_taken_from(self):
        self.identity_roundtrip('uid-1234')

    def test_an_archive_of_a_run_without_a_uid_imports_as_the_same_legacy_run(self):
        self.identity_roundtrip(None)

    def test_the_cli_import_maps_a_direct_archive_too(self):
        run = self.make_run()
        importer.import_run(run)
        before = self.rows('rid')
        archive = self.root / 'old.tar.gz'
        importer.archive_run(run, archive)
        shutil.rmtree(run)
        args = type('A', (), dict(paths=[str(archive)], registry=False, fractal=False, ledger=[],
                                  include_temp=True, dry_run=False))()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(importer.main(args), 0)
        self.assertIn('(0 new)', out.getvalue())
        self.assertEqual(self.rows('rid'), before)


class InterventionTests(PreserveCase):
    ESCALATION = ('1700000000\t1\tsol\tluna\tpin\n'
                  '1700000100\t2\tluna\tastra\n')

    def run_with_interventions(self, control_at):
        run = self.make_run(uid='uid-live')
        (run / 'tasks' / 'a' / 'escalations.tsv').write_text(self.ESCALATION)
        entry = dict(at=control_at, actor='ui', action='set', task=None, key='qa-threshold', value=3, previous=None)
        entry2 = dict(at=control_at + 1, actor='cli', action='pin', task='a', key='escalation', value='sol:high', previous=2.5)
        (run / 'control.log').write_text(json.dumps(entry) + '\n' + json.dumps(entry2) + '\n')
        return run

    def named(self, name):
        return [json.loads(r[0]) for r in self.rows('data') if json.loads(r[0]).get('name') == name]

    def test_an_escalation_emitted_live_and_then_imported_is_one_row(self):
        run = self.run_with_interventions(time.time())
        capture.point('escalation', str(run), 'a', {'from': 'sol', 'to': 'luna', 'after_attempt': '1', 'source': 'pin'})
        [live] = self.named('escalation')
        self.assertEqual(live['source'], 'pin')
        self.assertTrue(self.rows('rid')[0][0].startswith('event:escalation:'))
        importer.import_run(run)
        importer.import_run(run)
        escalations = sorted((d['after_attempt'], d['from'], d['to'], d['source']) for d in self.named('escalation'))
        # The pin line merged with the live event; the 4-column legacy line is a policy escalation.
        self.assertEqual(escalations, [('1', 'sol', 'luna', 'pin'), ('2', 'luna', 'astra', 'policy')])
        rows = [r for r in self.rows() if r[1] == 'event' and json.loads(r[5]).get('name') == 'escalation']
        self.assertEqual(sorted((r[3], r[4]) for r in rows), [('a', 'import'), ('a', 'live')])

    def test_a_control_line_is_one_row_after_live_capture_and_repeated_imports(self):
        at = time.time()
        run = self.run_with_interventions(at)
        capture.point('control', str(run), None, {'at': str(at), 'actor': 'ui', 'action': 'set', 'key': 'qa-threshold',
                                                  'value': '3', 'previous': ''})
        for _ in range(3):
            importer.import_run(run)
        control = self.named('control')
        self.assertEqual(sorted((d['action'], d['value']) for d in control), [('pin', 'sol:high'), ('set', '3')])
        rows = {r[3]: r for r in self.rows() if json.loads(r[5]).get('name') == 'control'}
        self.assertEqual(set(rows), {None, 'a'})
        self.assertEqual(rows['a'][4], 'import')
        self.assertEqual(rows[None][4], 'live')

    def test_a_line_that_cannot_be_read_fails_the_import(self):
        run = self.make_run()
        (run / 'tasks' / 'a' / 'escalations.tsv').write_text('1700000000\tsol\n')
        (run / 'control.log').write_text('{"at": 1.0}\nnot json\n')
        with self.assertRaises(store.DatasetError) as caught:
            importer.import_run(run)
        failed = caught.exception.result['failed']
        self.assertEqual(len(failed), 2)   # the 2-cell tsv line and the unparseable log line
        self.assertTrue(any('escalations.tsv:1' in f for f in failed))
        self.assertTrue(any('control.log:2' in f for f in failed))

    @unittest.skipIf(os.geteuid() == 0, 'root reads every file')
    def test_an_unreadable_intervention_file_fails_the_import_instead_of_reading_as_empty(self):
        for relative in ('tasks/a/escalations.tsv', 'control.log'):
            with self.subTest(relative):
                run = self.make_run(name='r-' + relative.replace('/', '-'))
                target = run / relative
                target.write_text('1700000000\t1\tsol\tluna\tpin\n' if relative.endswith('tsv')
                                  else json.dumps(dict(at=1.0, actor='ui', action='set', task=None, key='k')) + '\n')
                target.chmod(0)
                self.addCleanup(target.chmod, 0o644)
                with self.assertRaises(store.DatasetError) as caught:
                    importer.import_run(run)
                self.assertTrue(any(relative in f for f in caught.exception.result['failed']))

    @unittest.skipIf(os.geteuid() == 0, 'root reads every directory')
    def test_an_unreadable_subdirectory_fails_a_strict_import_instead_of_reading_as_empty(self):
        run = self.make_run()
        hidden = run / 'tasks' / 'a' / 'attempts'
        hidden.chmod(0)
        self.addCleanup(hidden.chmod, 0o755)
        with self.assertRaises(store.DatasetError) as caught:
            importer.import_run(run)
        self.assertTrue(any(str(hidden) in f for f in caught.exception.result['failed']))
        self.assertEqual(importer.import_run(run, strict=False)['failed'], caught.exception.result['failed'])

    @unittest.skipIf(os.geteuid() == 0, 'root reads every file')
    def test_an_unreadable_run_file_fails_a_strict_import_instead_of_reading_as_absent(self):
        run = self.make_run()
        qa = sorted((run / 'tasks' / 'a' / 'attempts').glob('qa-*'))[-1]
        files = [qa / 'metrics.json', run / 'tasks' / 'a' / 'status', run / 'run_id']
        for target in files:
            self.assertTrue(target.exists(), target)
        for target in files:
            with self.subTest(target.name):
                original = target.stat().st_mode
                target.chmod(0)
                self.addCleanup(target.chmod, original)
                with self.assertRaises(store.DatasetError) as caught:
                    importer.import_run(run)
                self.assertTrue(any(str(target) in f for f in caught.exception.result['failed']))
                target.chmod(original)
        self.assertEqual(importer.import_run(run)['failed'], [])

    def test_other_point_events_keep_a_rid_per_occurrence(self):
        run = self.make_run()
        for _ in range(2):
            capture.point('merge', str(run), 'a', {'status': 'MERGED'})
        self.assertEqual(len(self.rows('rid')), 2)


if __name__ == '__main__':
    unittest.main()
