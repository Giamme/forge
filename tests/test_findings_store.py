"""The finding record store, default tags and the status rule (scripts/forge_findings).

State and config homes are isolated per test; the repositories are throwaway git repos.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_findings  # noqa: E402
import forge_identity  # noqa: E402
import forge_registry  # noqa: E402
from forge_findings import status, store, tags  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
FP = 'aaaaaaaaaaaa'
FP2 = 'bbbbbbbbbbbb'
FP3 = 'cccccccccccc'
SHA = 'abc1234'


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def load_issues():
    spec = importlib.util.spec_from_file_location('forge_issues_under_test', ROOT / 'scripts' / 'forge-issues.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.home / 'state'),
                                      'XDG_CONFIG_HOME': str(self.home / 'config')})
        env.start()
        self.addCleanup(env.stop)
        self.repo = self.home / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init', '-q', '-b', 'main')
        git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'init')
        self.ident = forge_identity.identity(str(self.repo))
        self.findings = self.home / 'state' / 'forge' / 'findings'
        self.record = self.findings / f"{self.ident['key']}.record.json"
        self.log = self.findings / f"{self.ident['key']}.record.log.jsonl"

    def stored(self):
        return json.loads(self.record.read_text())

    def log_lines(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def files(self):
        # the lock file is created by any locked attempt, written or not
        return sorted(p.name for p in self.findings.iterdir() if not p.name.endswith('.lock')) \
            if self.findings.exists() else []


class StatusFieldTests(StoreCase):
    def test_file_format_and_a_dismissal(self):
        changed = store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='noise')
        self.assertEqual([c[0] for c in changed], [FP])
        data = self.stored()
        self.assertEqual(data['version'], 1)
        entry = data['findings'][FP]
        self.assertEqual((entry['status'], entry['reason'], entry['actor']), ('dismissed', 'noise', 'cli'))
        self.assertIsInstance(entry['at'], float)
        self.assertEqual(store.get(self.ident, FP), entry)
        self.assertEqual(store.load(self.ident), {FP: entry})

    def test_each_status_with_its_required_field(self):
        cases = [('to-file', {}), ('filed', {'issue': 'https://github.com/o/r/issues/1'}),
                 ('fixed', {'commit': SHA}), ('dismissed', {'reason': 'noise'}),
                 ('duplicate', {'duplicate_of': FP2}), ('open', {})]
        for status_name, fields in cases:
            with self.subTest(status_name):
                store.set_status(self.ident, [FP], status_name, 'ui', **fields)
                entry = store.get(self.ident, FP)
                self.assertEqual(entry['status'], status_name)
                for key, value in fields.items():
                    self.assertEqual(entry[key], value)

    def test_fixed_takes_a_reason_instead_of_a_commit(self):
        store.set_status(self.ident, [FP], 'fixed', 'cli', reason='fixed by hand')
        self.assertEqual(store.get(self.ident, FP)['reason'], 'fixed by hand')

    def test_refusals_write_nothing(self):
        before = self.files()
        refusals = [
            ('filed without an issue', dict(status='filed')),
            ('fixed without commit or reason', dict(status='fixed')),
            ('dismissed without a reason', dict(status='dismissed')),
            ('duplicate without duplicate_of', dict(status='duplicate')),
            ('duplicate of itself', dict(status='duplicate', duplicate_of=FP)),
            ('unknown status', dict(status='done')),
            ('bad commit', dict(status='fixed', commit='XYZ')),
            ('short commit', dict(status='fixed', commit='abc12')),
            ('long reason', dict(status='dismissed', reason='x' * 501)),
            ('long note', dict(note='x' * 2001)),
            ('bad tag', dict(add_tags=['Bad'])),
            ('digit-first tag', dict(add_tags=['1up'])),
            ('long tag', dict(add_tags=['a' * 25])),
            ('nine tags', dict(add_tags=[f't{i}' for i in range(9)])),
            ('bad severity', dict(edit={'severity': 'P4'})),
            ('long edit text', dict(edit={'text': 'x' * 4001})),
            ('long draft title', dict(draft={'title': 'x' * 201, 'body': 'b'})),
            ('long draft body', dict(draft={'title': 't', 'body': 'x' * 20001})),
            ('unknown change', dict(color='red')),
        ]
        for name, changes in refusals:
            with self.subTest(name), self.assertRaises(store.Invalid):
                store.update(self.ident, [FP], 'cli', **changes)
        for name, fps in (('short fingerprint', ['abc']), ('uppercase', ['A' * 12]), ('a bare string', FP)):
            with self.subTest(name), self.assertRaises(store.Invalid):
                store.update(self.ident, fps, 'cli', note='x')
        with self.assertRaises(store.Invalid):
            store.update(self.ident, [FP], 'robot', note='x')
        with self.assertRaises(store.Invalid):
            store.mark_duplicate(self.ident, FP, FP, 'cli')
        self.assertEqual(self.files(), before)

    def test_a_refused_member_of_a_batch_writes_nothing_for_the_others(self):
        store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='x')
        before = self.record.read_text()
        with self.assertRaises(store.Invalid):
            store.set_status(self.ident, [FP2, FP3], 'duplicate', 'cli', duplicate_of=FP3)
        self.assertEqual(self.record.read_text(), before)

    def test_every_actor_is_accepted(self):
        for actor in store.ACTORS:
            store.update(self.ident, [FP], actor, note=actor)
            self.assertEqual(store.get(self.ident, FP)['actor'], actor)

    def test_a_new_status_starts_its_fields_afresh(self):
        store.set_status(self.ident, [FP], 'filed', 'cli', issue='https://x/1')
        store.set_status(self.ident, [FP], 'open', 'cli')
        entry = store.get(self.ident, FP)
        self.assertEqual(entry['status'], 'open')
        self.assertNotIn('issue', entry)

    def test_mark_duplicate(self):
        store.mark_duplicate(self.ident, FP, FP2, 'triage')
        entry = store.get(self.ident, FP)
        self.assertEqual((entry['status'], entry['duplicate_of'], entry['actor']), ('duplicate', FP2, 'triage'))


class UpdateTests(StoreCase):
    def test_a_batch_is_one_write_and_one_log_line_per_finding(self):
        with patch.object(store, '_write_record', wraps=store._write_record) as write:
            changed = store.set_status(self.ident, [FP, FP2, FP3, FP], 'dismissed', 'ui', reason='bulk')
        self.assertEqual(write.call_count, 1)
        self.assertEqual([c[0] for c in changed], [FP, FP2, FP3])
        lines = self.log_lines()
        self.assertEqual([line['fp'] for line in lines], [FP, FP2, FP3])
        self.assertEqual(set(self.stored()['findings']), {FP, FP2, FP3})

    def test_an_unchanged_entry_is_not_written_or_logged(self):
        store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='same')
        first = self.record.read_text()
        with patch.object(store, '_write_record') as write:
            self.assertEqual(store.set_status(self.ident, [FP], 'dismissed', 'ui', reason='same'), [])
        write.assert_not_called()
        self.assertEqual(self.record.read_text(), first)
        self.assertEqual(len(self.log_lines()), 1)

    def test_a_change_that_adds_nothing_to_an_unknown_finding_creates_no_entry(self):
        self.assertEqual(store.remove_tags(self.ident, [FP], ['absent'], 'cli'), [])
        self.assertEqual(store.set_note(self.ident, FP, '', 'cli'), [])
        self.assertEqual(store.set_edit(self.ident, FP, 'ui', text=''), [])
        self.assertFalse(self.record.exists())
        self.assertEqual(self.log_lines(), [])
        # so a swept finding is not turned into a reopened one
        self.assertEqual(status.effective(store.get(self.ident, FP), {'swept': {'at': 1}}, None), 'swept')

    def test_reopening_an_unknown_finding_is_still_recorded(self):
        store.set_status(self.ident, [FP], 'open', 'cli')
        self.assertEqual(self.stored()['findings'][FP]['status'], 'open')

    def test_tags_that_are_not_a_list_are_refused(self):
        for bad in (5, None.__class__, 'ui', {'a': 1}):
            with self.assertRaises(store.Invalid):
                store.update(self.ident, [FP], 'cli', add_tags=bad)
            with self.assertRaises(store.Invalid):
                store.update(self.ident, [FP], 'cli', remove_tags=bad)
        self.assertFalse(self.record.exists())

    def test_only_the_changed_findings_of_a_batch_are_logged(self):
        store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        changed = store.set_status(self.ident, [FP, FP2], 'dismissed', 'cli', reason='r')
        self.assertEqual([c[0] for c in changed], [FP2])
        self.assertEqual([line['fp'] for line in self.log_lines()], [FP, FP2])

    def test_log_line_shape(self):
        store.set_status(self.ident, [FP], 'dismissed', 'sweep', reason='first')
        store.set_status(self.ident, [FP], 'dismissed', 'ui', reason='second')
        first, second = self.log_lines()
        self.assertEqual(set(first), {'at', 'actor', 'fp', 'op', 'before', 'after'})
        self.assertEqual((first['actor'], first['fp'], first['op'], first['before']), ('sweep', FP, 'status', None))
        self.assertEqual(first['after']['status'], 'dismissed')
        self.assertEqual(second['before']['reason'], 'first')
        self.assertEqual(second['after']['reason'], 'second')
        self.assertEqual(second['actor'], 'ui')

    def test_each_change_emits_one_finding_record_event(self):
        with patch('forge_dataset.capture.point') as point:
            store.set_status(self.ident, [FP, FP2], 'dismissed', 'ui', reason='r')
            store.set_status(self.ident, [FP], 'dismissed', 'ui', reason='r')
        self.assertEqual(point.call_count, 2)
        names = {call.args[0] for call in point.call_args_list}
        self.assertEqual(names, {'finding_record'})
        values = [call.args[3] for call in point.call_args_list]
        self.assertEqual(values[0], {'fp': FP, 'op': 'status', 'status': 'dismissed', 'actor': 'ui'})
        self.assertEqual(values[1]['fp'], FP2)

    def test_a_failing_dataset_does_not_fail_the_change(self):
        with patch('forge_dataset.capture.point', side_effect=RuntimeError('boom')):
            store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        self.assertEqual(store.get(self.ident, FP)['status'], 'dismissed')

    def test_tags_add_remove_and_dedupe(self):
        store.add_tags(self.ident, [FP, FP2], ['ui', 'perf', 'ui'], 'ui')
        self.assertEqual(store.get(self.ident, FP)['tags'], ['ui', 'perf'])
        self.assertEqual(store.get(self.ident, FP2)['status'], 'open')
        store.add_tags(self.ident, [FP], ['ui', 'docs'], 'ui')
        self.assertEqual(store.get(self.ident, FP)['tags'], ['ui', 'perf', 'docs'])
        store.remove_tags(self.ident, [FP], ['ui', 'absent'], 'cli')
        self.assertEqual(store.get(self.ident, FP)['tags'], ['perf', 'docs'])
        store.remove_tags(self.ident, [FP], ['perf', 'docs'], 'cli')
        self.assertNotIn('tags', store.get(self.ident, FP))
        self.assertEqual(self.log_lines()[0]['op'], 'tags')

    def test_eight_tags_are_the_limit(self):
        store.add_tags(self.ident, [FP], [f't{i}' for i in range(8)], 'cli')
        with self.assertRaises(store.Invalid):
            store.add_tags(self.ident, [FP], ['extra'], 'cli')
        self.assertEqual(len(store.get(self.ident, FP)['tags']), 8)

    def test_note_sets_and_clears(self):
        store.set_note(self.ident, FP, 'look at this', 'ui')
        self.assertEqual(store.get(self.ident, FP)['note'], 'look at this')
        store.set_note(self.ident, FP, '', 'ui')
        self.assertNotIn('note', store.get(self.ident, FP))

    def test_edit_merges_and_clears_fields(self):
        store.set_edit(self.ident, FP, 'ui', severity='P2')
        store.set_edit(self.ident, FP, 'ui', text='new words')
        self.assertEqual(store.get(self.ident, FP)['edit'], {'severity': 'P2', 'text': 'new words'})
        store.set_edit(self.ident, FP, 'ui', severity='P1')
        self.assertEqual(store.get(self.ident, FP)['edit'], {'severity': 'P1', 'text': 'new words'})
        store.set_edit(self.ident, FP, 'ui', text='')
        self.assertEqual(store.get(self.ident, FP)['edit'], {'severity': 'P1'})
        self.assertEqual(store.set_edit(self.ident, FP, 'ui'), [])
        with self.assertRaises(store.Invalid):
            store.set_edit(self.ident, FP, 'ui', severity='P9')
        self.assertEqual(store.get(self.ident, FP)['edit'], {'severity': 'P1'})

    def test_draft_is_stored_and_cleared(self):
        store.update(self.ident, [FP], 'ui', draft={'title': 'T', 'body': 'B'})
        self.assertEqual(store.get(self.ident, FP)['draft'], {'title': 'T', 'body': 'B'})
        store.update(self.ident, [FP], 'ui', draft=None)
        self.assertNotIn('draft', store.get(self.ident, FP))

    def test_tags_note_edit_and_draft_survive_a_status_change(self):
        store.add_tags(self.ident, [FP], ['ui'], 'ui')
        store.set_note(self.ident, FP, 'n', 'ui')
        store.set_status(self.ident, [FP], 'dismissed', 'ui', reason='r')
        entry = store.get(self.ident, FP)
        self.assertEqual((entry['tags'], entry['note']), (['ui'], 'n'))


class LockTests(StoreCase):
    def test_locked_true_inside_lock_does_not_wait_on_itself(self):
        with store.lock(self.ident):
            changed = store.set_status(self.ident, [FP], 'dismissed', 'cli', locked=True, reason='r')
        self.assertEqual(len(changed), 1)

    def test_without_locked_a_held_lock_is_busy(self):
        with patch.object(store, 'LOCK_WAIT', 0.2), store.lock(self.ident):
            with self.assertRaises(store.Busy):
                store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        self.assertFalse(self.record.exists())

    def test_another_process_holding_the_lock_is_busy(self):
        store.set_status(self.ident, [FP2], 'dismissed', 'cli', reason='r')
        code = ('import fcntl, os, sys, time\n'
                'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n'
                'fcntl.flock(fd, fcntl.LOCK_EX)\n'
                'print("held", flush=True)\n'
                'sys.stdin.readline()\n')
        lock_path = self.findings / f"{self.ident['key']}.lock"
        proc = subprocess.Popen([sys.executable, '-c', code, str(lock_path)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'held')
            with patch.object(store, 'LOCK_WAIT', 0.3):
                with self.assertRaises(store.Busy):
                    with store.lock(self.ident):
                        pass
                with self.assertRaises(store.Busy):
                    store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        finally:
            proc.stdin.write('\n')
            proc.stdin.close()
        proc.wait()
        with store.lock(self.ident):
            pass
        self.assertNotIn(FP, self.stored()['findings'])

    def test_a_symlinked_lock_is_refused(self):
        self.findings.mkdir(parents=True)
        target = self.home / 'elsewhere'
        target.write_text('')
        os.symlink(target, self.findings / f"{self.ident['key']}.lock")
        with self.assertRaises(store.Corrupt):
            with store.lock(self.ident):
                pass


class FileRuleTests(StoreCase):
    def test_a_symlinked_record_is_refused_for_reading_and_writing(self):
        self.findings.mkdir(parents=True)
        target = self.home / 'elsewhere.json'
        target.write_text(json.dumps({'version': 1, 'findings': {}}))
        os.symlink(target, self.record)
        with self.assertRaises(store.Corrupt):
            store.load(self.ident)
        with self.assertRaises(store.Corrupt):
            store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        self.assertEqual(json.loads(target.read_text()), {'version': 1, 'findings': {}})

    def test_a_symlinked_findings_directory_is_refused(self):
        real = self.home / 'real'
        real.mkdir()
        (self.home / 'state' / 'forge').mkdir(parents=True)
        os.symlink(real, self.findings)
        with self.assertRaises(store.Corrupt):
            store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        self.assertEqual(list(real.iterdir()), [])

    def test_an_unreadable_record_is_corrupt(self):
        self.findings.mkdir(parents=True)
        for text in ('not json', '[]', '{"version": 2, "findings": {}}',
                     json.dumps({'version': 1, 'findings': {'nope': {'status': 'open', 'at': 1}}}),
                     json.dumps({'version': 1, 'findings': {FP: {'status': 'weird', 'at': 1}}}),
                     json.dumps({'version': 1, 'findings': {FP: {'status': 'open'}}})):
            with self.subTest(text):
                self.record.write_text(text)
                with self.assertRaises(store.Corrupt):
                    store.load(self.ident)
                with self.assertRaises(store.Corrupt):
                    store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
                self.assertEqual(self.record.read_text(), text)

    def test_the_write_is_atomic_and_leaves_no_temp_files(self):
        store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        store.set_status(self.ident, [FP2], 'dismissed', 'cli', reason='r')
        names = self.files()
        self.assertEqual([n for n in names if n.endswith('.tmp')], [])
        self.assertEqual(oct(self.record.stat().st_mode & 0o777)[-3:] in ('600', '644', '664'), True)

    def test_a_failed_replace_leaves_the_old_record_and_no_temp_file(self):
        store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        before = self.record.read_text()
        with patch('os.replace', side_effect=OSError(13, 'denied')):
            with self.assertRaises(store.Corrupt):
                store.set_status(self.ident, [FP2], 'dismissed', 'cli', reason='r')
        self.assertEqual(self.record.read_text(), before)
        self.assertEqual([n for n in self.files() if n.endswith('.tmp')], [])

    def test_empty_or_relative_xdg_state_home_counts_as_unset(self):
        fake_home = self.home / 'home'
        fake_home.mkdir()
        for value in ('', 'relative/state'):
            with self.subTest(value), patch.dict(os.environ, {'XDG_STATE_HOME': value, 'HOME': str(fake_home)}):
                self.assertEqual(store.findings_dir(), fake_home / '.local' / 'state' / 'forge' / 'findings')
        self.assertFalse((Path.cwd() / 'relative').exists())

    def test_the_lock_is_the_one_forge_issues_takes(self):
        issues = load_issues()
        with patch.dict(os.environ, {'XDG_STATE_HOME': str(self.home / 'state')}):
            self.assertEqual(issues.findings_dir(), self.findings)
        store.set_status(self.ident, [FP], 'dismissed', 'cli', reason='r')
        # forge-issues' repo_lock(key) opens <findings>/<key>.lock, the file lock() holds
        with patch.object(issues, 'LOCK_WAIT', 0.2), store.lock(self.ident):
            with self.assertRaises(issues.Busy):
                with issues.repo_lock(self.ident['key'], 'test'):
                    pass


class MigrationTests(StoreCase):
    def write_old(self, checkout, data):
        self.findings.mkdir(parents=True, exist_ok=True)
        path = self.findings / f"{forge_registry.repo_key(checkout)}.json"
        path.write_text(json.dumps(data, indent=2))
        return path

    def test_old_dismissals_merge_in_for_the_main_checkout(self):
        path = self.write_old(self.repo, {FP: {'at': 1700000000, 'reason': 'old reason'}})
        before = path.read_text()
        entry = store.get(self.ident, FP)
        self.assertEqual(entry, {'status': 'dismissed', 'at': 1700000000, 'actor': 'issues', 'reason': 'old reason'})
        self.assertEqual(path.read_text(), before)
        self.assertFalse(self.record.exists())

    def test_a_linked_worktrees_dismissals_merge_in(self):
        wt = self.home / 'wt'
        git(self.repo, 'worktree', 'add', '-q', '-b', 'feature', str(wt))
        self.addCleanup(lambda: subprocess.run(['git', '-C', str(self.repo), 'worktree', 'remove', '--force', str(wt)],
                                               capture_output=True, env=GIT_ENV))
        wt_ident = forge_identity.identity(str(wt))
        self.assertEqual(wt_ident['key'], self.ident['key'])
        main_file = self.write_old(self.repo, {FP: {'at': 1, 'reason': 'main'}})
        wt_file = self.write_old(str(wt), {FP2: {'at': 2, 'reason': 'worktree'}})
        self.assertNotEqual(main_file, wt_file)
        before = (main_file.read_text(), wt_file.read_text())
        # the worktree has to be a checkout forge has seen: identity() of it records nothing, so register it
        with patch.object(forge_identity, 'checkouts', return_value=[str(self.repo), str(wt)]):
            found = store.load(wt_ident)
        self.assertEqual({fp: e['reason'] for fp, e in found.items()}, {FP: 'main', FP2: 'worktree'})
        self.assertEqual((main_file.read_text(), wt_file.read_text()), before)

    def test_a_removed_worktrees_file_still_merges_through_its_recorded_path(self):
        gone = self.home / 'gone-worktree'
        self.write_old(str(gone), {FP: {'at': 5, 'reason': 'from a gone worktree'}})
        with patch.object(forge_identity, 'checkouts', return_value=[str(self.repo), str(gone)]):
            self.assertEqual(store.get(self.ident, FP)['reason'], 'from a gone worktree')

    def test_the_record_wins_over_an_old_dismissal(self):
        path = self.write_old(self.repo, {FP: {'at': 1, 'reason': 'old'}, FP2: {'at': 2, 'reason': 'old2'}})
        store.set_status(self.ident, [FP], 'open', 'ui')
        found = store.load(self.ident)
        self.assertEqual(found[FP]['status'], 'open')
        self.assertEqual(found[FP2]['status'], 'dismissed')
        self.assertEqual(json.loads(path.read_text())[FP], {'at': 1, 'reason': 'old'})

    def test_a_change_to_a_migrated_finding_keeps_the_old_file_untouched(self):
        path = self.write_old(self.repo, {FP: {'at': 1, 'reason': 'old'}})
        before = path.read_text()
        changed = store.add_tags(self.ident, [FP], ['ui'], 'ui')
        self.assertEqual(changed[0][1]['status'], 'dismissed')
        entry = store.get(self.ident, FP)
        self.assertEqual((entry['status'], entry['tags'], entry['reason']), ('dismissed', ['ui'], 'old'))
        self.assertEqual(path.read_text(), before)
        self.assertEqual(list(self.stored()['findings']), [FP])

    def test_unreadable_old_files_are_skipped(self):
        self.findings.mkdir(parents=True)
        (self.findings / f"{forge_registry.repo_key(self.repo)}.json").write_text('{broken')
        self.assertEqual(store.load(self.ident), {})
        self.write_old(self.repo, {FP: {'at': 1}, FP2: {'at': 2, 'reason': 'ok'}, 'bad': {'at': 1, 'reason': 'x'}})
        self.assertEqual(list(store.load(self.ident)), [FP2])

    def test_a_symlinked_old_file_is_skipped(self):
        self.findings.mkdir(parents=True)
        target = self.home / 'real.json'
        target.write_text(json.dumps({FP: {'at': 1, 'reason': 'x'}}))
        os.symlink(target, self.findings / f"{forge_registry.repo_key(self.repo)}.json")
        self.assertEqual(store.load(self.ident), {})


class DefaultTagTests(unittest.TestCase):
    def test_default_tags(self):
        cases = {
            'tests/test_x.py': 'tests', 'ui/tests/js/a.test.js': 'tests', 'src/test/Foo.java': 'tests',
            'pkg/test_thing.py': 'tests', 'pkg/thing_test.go': 'tests', 'web/app.test.ts': 'tests',
            'docs/guide.html': 'docs', 'README.md': 'docs', 'pkg/NOTES.md': 'docs', 'site/pages/x.css': 'docs',
            '.github/workflows/ci.yml': 'infra', 'ci/run.sh': 'infra', 'Dockerfile': 'infra',
            'docker-compose.yml': 'infra', 'config.yaml': 'infra', 'install.sh': 'infra',
            'scripts/install-deps.sh': 'infra', 'scripts/forge-install.sh': 'infra',
            'ui/forge_ui/control.py': 'ui', 'web/main.py': 'ui', 'frontend/x.rb': 'ui', 'static/x.txt': 'ui',
            'src/app.js': 'ui', 'src/a.ts': 'ui', 'src/a.tsx': 'ui', 'src/a.css': 'ui', 'src/a.html': 'ui',
            'scripts/forge-issues.py': 'backend', 'main.go': 'backend', 'deploy/config.yml': 'backend',
            'lib/contest.py': 'backend', 'latest/x.py': 'backend',
        }
        for path, expected in cases.items():
            with self.subTest(path):
                self.assertEqual(tags.default_tags(path), [expected])

    def test_no_path_gives_no_tags(self):
        for path in (None, '', '   ', 12):
            self.assertEqual(tags.default_tags(path), [])

    def test_first_match_wins(self):
        self.assertEqual(tags.default_tags('docs/tests/x.js'), ['tests'])
        self.assertEqual(tags.default_tags('docs/app.js'), ['docs'])
        self.assertEqual(tags.default_tags('ui/guide.md'), ['docs'])
        self.assertEqual(tags.default_tags('.github/page.html'), ['infra'])

    def test_tags_for_prefers_the_stored_tags(self):
        self.assertEqual(tags.tags_for({'tags': ['perf']}, 'ui/a.js'), ['perf'])
        self.assertEqual(tags.tags_for({'status': 'open'}, 'ui/a.js'), ['ui'])
        self.assertEqual(tags.tags_for(None, 'ui/a.js'), ['ui'])
        self.assertEqual(tags.tags_for({'tags': []}, 'main.go'), ['backend'])
        self.assertEqual(tags.tags_for(None, None), [])

    def test_default_tags_are_never_stored(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'XDG_STATE_HOME': tmp}):
            ident = {'key': '0123456789abcdef', 'root': tmp}
            store.set_note(ident, FP, 'x', 'cli')
            self.assertNotIn('tags', store.get(ident, FP))


class EffectiveTests(unittest.TestCase):
    def test_record_statuses_win_over_everything(self):
        issues = {'filed': {'url': 'u', 'state': 'CLOSED'}, 'swept': {'task': 't'}, 'dismissed': {'at': 1}}
        row = {'kind': 'shadow', 'confidence': 'UNSTATED', 'superseded': True}
        for name in ('duplicate', 'dismissed', 'fixed', 'filed', 'to-file'):
            with self.subTest(name):
                self.assertEqual(status.effective({'status': name}, issues, row), name)

    def test_the_issues_entry(self):
        eff = status.effective
        self.assertEqual(eff(None, {'filed': {'url': 'u', 'state': 'CLOSED'}}, None), 'fixed')
        self.assertEqual(eff(None, {'filed': {'url': 'u', 'state': 'closed'}}, None), 'fixed')
        self.assertEqual(eff(None, {'filed': {'url': 'u', 'state': 'OPEN'}}, None), 'filed')
        self.assertEqual(eff(None, {'filed': {'url': 'u', 'state': 'CLOSED'}, 'swept': {'task': 't'}}, None), 'fixed')
        self.assertEqual(eff(None, {'filed': None, 'swept': {'task': 't'}, 'dismissed': {'at': 1}}, None), 'swept')
        self.assertEqual(eff(None, {'filed': None, 'swept': None, 'dismissed': {'at': 1}}, None), 'dismissed')
        self.assertEqual(eff(None, {'filed': None, 'swept': None, 'dismissed': None}, None), 'open')

    def test_the_review_row(self):
        eff = status.effective
        self.assertEqual(eff(None, None, {'kind': 'shadow', 'confidence': 'UNSTATED', 'superseded': True}), 'shadow')
        self.assertEqual(eff(None, None, {'kind': 'review', 'confidence': 'UNSTATED', 'superseded': True}), 'unlabelled')
        self.assertEqual(eff(None, None, {'kind': 'review', 'confidence': 'HIGH', 'superseded': True}), 'superseded')
        self.assertEqual(eff(None, None, {'kind': 'review', 'confidence': 'HIGH', 'superseded': False}), 'open')
        self.assertEqual(eff(None, {'swept': {'task': 't'}}, {'kind': 'shadow'}), 'swept')

    def test_nothing_known_is_open(self):
        self.assertEqual(status.effective(None, None, None), 'open')
        self.assertEqual(status.effective({}, {}, {}), 'open')

    def test_reopened_skips_the_record_and_the_old_store_but_not_a_filed_issue(self):
        eff = status.effective
        reopened = {'status': 'open'}
        self.assertEqual(eff(reopened, {'dismissed': {'at': 1}}, None), 'open')
        self.assertEqual(eff(reopened, {'swept': {'task': 't'}}, None), 'open')
        self.assertEqual(eff(reopened, {'filed': {'url': 'u', 'state': 'OPEN'}}, None), 'filed')
        self.assertEqual(eff(reopened, {'filed': {'url': 'u', 'state': 'CLOSED'}}, None), 'fixed')
        self.assertEqual(eff(reopened, None, {'kind': 'shadow'}), 'shadow')
        self.assertEqual(eff(reopened, None, {'confidence': 'UNSTATED'}), 'unlabelled')
        self.assertEqual(eff(reopened, None, {'superseded': True}), 'superseded')
        self.assertEqual(eff(reopened, {'dismissed': {'at': 1}}, {'superseded': True}), 'superseded')

    def test_only_open_and_to_file_count_as_open(self):
        self.assertEqual(status.OPEN, ('open', 'to-file'))


class FingerprintTests(unittest.TestCase):
    def test_equals_forge_issues(self):
        issues = load_issues()
        cases = [('Owner/Repo', 'a.py', 'Plain text'),
                 ('owner/repo', 'a.py', 'MiXeD   Case\tand\n\nwhitespace   runs'),
                 ('owner/repo', 'dir/b.js', 'x' * 400),
                 ('owner/repo', 'dir/b.js', 'y' * 159 + ' ' + 'z' * 50),
                 ('', 'a.py', 'empty slug'),
                 ('o/r', '', 'no file'),
                 ('o/r', 'a.py', 'ünïcode — text ✓'),
                 ('o/r', 'a.py', '')]
        for slug, file, text in cases:
            with self.subTest(slug=slug, file=file, text=text[:20]):
                self.assertEqual(forge_findings.fingerprint(slug, file, text),
                                 issues.fingerprint(slug, {'file': file, 'text': text}))
        self.assertRegex(forge_findings.fingerprint('o/r', 'a.py', 't'), r'^[0-9a-f]{12}$')

    def test_the_slug_is_case_insensitive_and_the_file_is_not(self):
        self.assertEqual(forge_findings.fingerprint('O/R', 'a.py', 't'), forge_findings.fingerprint('o/r', 'a.py', 't'))
        self.assertNotEqual(forge_findings.fingerprint('o/r', 'A.py', 't'), forge_findings.fingerprint('o/r', 'a.py', 't'))


if __name__ == '__main__':
    unittest.main()
