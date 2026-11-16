"""The state the Findings tab shows (#160): the issue's state read online (offline when GitHub cannot be asked), the
finding record, the shadow / unlabelled / superseded rules of forge_findings.status, the honest open count."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from forge_ui import projects as P, reader
import run_fixture as F
from test_projects import FindingsCase, git

UNSTATED = '- [P2] ui/unst.py:5 — a finding that states no confidence.\n\nFORGE_VERDICT: FAIL\n'


class StateCase(FindingsCase):
    """FindingsCase on a real repository, so the project has an identity and a record file of its own."""

    def setUp(self):
        super().setUp()
        tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.root = tmp / 'app'
        self.root.mkdir()
        git(self.root, 'init', '-q', '-b', 'main')
        (self.root / 'f').write_text('1\n')
        git(self.root, 'add', 'f')
        git(self.root, 'commit', '-qm', 'base')
        (self.plan / 'repo').write_text(str(self.root) + '\n')
        tsv = self.plan / 'tasks.tsv'
        tsv.write_text(tsv.read_text() + 'unst\t-\tlow\tui/unst.py\tluna\topus\tUnstated\n')
        tdir = self.plan / 'tasks' / 'unst'
        F.execution(tdir, 'task-%032x' % 700, 100.0, 150.0, exit_code=1, attempt_start=0, trigger='initial')
        F.execution(tdir, 'qa-%032x' % 701, 110.0, 140.0, parent='task-%032x' % 700, spec=('opus', None, 'claude'),
                    last=UNSTATED)
        (tdir / 'status').write_text('FAIL\n')
        self.catalog = reader.Catalog(plans=[self.plan], registry=False)
        self.pid = P.project_id(str(self.root))
        self.state = tmp / 'state'
        patch = mock.patch.dict(os.environ, {'XDG_STATE_HOME': str(self.state)})
        patch.start()
        self.addCleanup(patch.stop)
        self.ident = P.record_modules().identity.identity(str(self.root))

    def write_record(self, entries):
        folder = self.state / 'forge' / 'findings'
        folder.mkdir(parents=True, exist_ok=True)
        data = {fp: dict(entry, at=1.0, actor='ui') for fp, entry in entries.items()}
        (folder / ('%s.record.json' % self.ident['key'])).write_text(json.dumps(dict(version=1, findings=data)))

    def fp(self, text, slug=''):
        """The record's key of a finding line, computed apart from the screen."""
        return P.issues_module().fingerprint(slug, P.parsed_finding(text))

    def rows(self, **kw):
        found = self.screen_findings(**kw)
        return {r['location']: r for r in found['rows']}, found

    def entry(self, file, text, task, **extra):
        return dict(dict(file=file, text=text, task=task, plan_dir=str(self.plan), origin='tolerated', filed=None,
                         dismissed=None), **extra)

    def screen_findings(self, history=None, issues=None, now=None, screen=None):
        return (screen or P.Screen()).findings(self.catalog, self.pid, lambda: history, issues or (lambda path, online=True: None),
                                               now=now)

    AGAIN = '- [P2][CONFIRMED] ui/again.py:3 — the same bug, reported in both rounds.'
    LOOSE = '- **P2** [CONFIRMED] ui/loose.py:4: written in another form.'

    def listed(self, entry, repo='acme/app'):
        return dict(repo=repo, findings=[entry])

    def again_entry(self, **extra):
        e = self.entry('ui/again.py', 'the same bug, reported in both rounds.', 'again', **extra)
        e.setdefault('fp', self.fp(self.AGAIN, 'acme/app'))
        return e


class OnlineStateTests(StateCase):
    def test_a_filed_issue_that_is_closed_is_fixed_and_one_that_is_open_is_filed(self):
        for state, status in (('CLOSED', 'fixed'), ('OPEN', 'filed')):
            entry = self.again_entry(filed=dict(number=7, state=state))
            rows, found = self.rows(issues=lambda path, online=True: self.listed(entry), now=500.0)
            self.assertEqual(rows['ui/again.py:3']['status'], status, state)
            self.assertEqual((found['issues_online'], found['issues_read_at']), (True, 500.0))
            self.assertFalse([n for n in found['notes'] if 'GitHub' in n or 'forge issues' in n])

    def test_the_listing_is_asked_of_the_root_once_and_online_first(self):
        calls = []

        def issues(path, online=True):
            calls.append((path, online))
            return dict(repo='acme/app', findings=[])
        self.screen_findings(issues=issues)
        self.assertEqual(calls, [(str(self.root), True)])

    def test_a_failing_online_call_falls_back_to_the_offline_listing_with_a_note(self):
        entry = self.again_entry(filed=dict(number=7, state='OPEN'))
        calls = []

        def issues(path, online=True):
            calls.append(online)
            return None if online else self.listed(entry)
        rows, found = self.rows(issues=issues, now=900.0)
        self.assertEqual(calls, [True, False])
        self.assertEqual((found['issues_online'], found['issues_read_at']), (False, 900.0))
        self.assertIn('issue states could not be read from GitHub: filed findings may show as open', found['notes'])
        self.assertEqual(rows['ui/again.py:3']['status'], 'filed')

    def test_the_runs_are_asked_only_when_the_root_gave_nothing(self):
        calls = []

        def issues(path, online=True):
            calls.append((path, online))
            return dict(repo='acme/app', findings=[]) if path != str(self.root) else None
        found = self.screen_findings(issues=issues)
        self.assertEqual(calls[0], (str(self.root), True))
        self.assertIn((str(self.root), False), calls)
        self.assertTrue(any(path == os.path.realpath(self.plan) for path, _ in calls))
        self.assertFalse(found['issues_online'])        # the root's online call failed: the runs are asked offline
        self.assertIn('issue states could not be read from GitHub: filed findings may show as open', found['notes'])

    def test_nothing_read_at_all_keeps_the_old_note(self):
        found = self.screen_findings()
        self.assertFalse(found['issues_online'])
        self.assertIsNone(found['issues_read_at'])
        self.assertTrue(any('forge issues could not be read' in n for n in found['notes']))


class RecordStateTests(StateCase):
    def test_a_record_that_says_fixed_or_duplicate_wins_over_the_review(self):
        self.write_record({self.fp(self.AGAIN): dict(status='fixed'), self.fp(self.LOOSE): dict(status='duplicate')})
        rows, found = self.rows()
        self.assertEqual(rows['ui/again.py:3']['status'], 'fixed')
        self.assertEqual(rows['ui/loose.py:4']['status'], 'duplicate')
        self.assertEqual(rows['ui/again.py:3']['record']['status'], 'fixed')
        self.assertEqual(found['notes'].count('the finding record could not be read'), 0)

    def test_a_row_with_no_issues_entry_gets_its_record_by_the_fingerprint_it_computes(self):
        slug = 'acme/app'
        self.write_record({self.fp(self.AGAIN, slug): dict(status='dismissed', reason='by design')})
        rows, _ = self.rows(issues=lambda path, online=True: dict(repo=slug, findings=[]))
        row = rows['ui/again.py:3']
        self.assertEqual((row['record_fp'], row['status']), (self.fp(self.AGAIN, slug), 'dismissed'))
        self.assertEqual(row['record']['reason'], 'by design')
        # the same line without the listing's slug is another finding: the record has nothing under that key
        rows, _ = self.rows()
        self.assertEqual(rows['ui/again.py:3']['status'], 'open')
        self.assertEqual(rows['ui/again.py:3']['record_fp'], self.fp(self.AGAIN))

    def test_the_entrys_own_fp_is_the_records_key(self):
        entry = self.again_entry(fp='0123456789ab')
        self.write_record({'0123456789ab': dict(status='fixed')})
        rows, _ = self.rows(issues=lambda path, online=True: self.listed(entry))
        self.assertEqual(rows['ui/again.py:3']['record_fp'], '0123456789ab')
        self.assertEqual(rows['ui/again.py:3']['status'], 'fixed')

    def test_a_reopened_finding_and_a_to_file_one_are_open_and_to_file_is_fileable(self):
        slug = 'acme/app'
        self.write_record({self.fp(self.AGAIN, slug): dict(status='to-file', draft=dict(title='T', body='B')),
                           self.fp(self.LOOSE, slug): dict(status='open')})
        entry = self.again_entry()
        rows, _ = self.rows(issues=lambda path, online=True: self.listed(entry))
        self.assertEqual(rows['ui/again.py:3']['status'], 'to-file')
        self.assertEqual(rows['ui/again.py:3']['draft'], dict(title='T', body='B'))
        self.assertTrue(rows['ui/again.py:3']['fileable'])
        self.assertEqual(rows['ui/loose.py:4']['status'], 'open')
        self.assertIsNone(rows['ui/loose.py:4']['draft'])

    def test_tags_are_the_stored_ones_else_the_defaults_of_the_file(self):
        self.write_record({self.fp(self.LOOSE): dict(status='open', tags=['triage'])})
        rows, _ = self.rows()
        self.assertEqual(rows['ui/loose.py:4']['tags'], ['triage'])
        self.assertEqual(rows['ui/x.py:1']['tags'], ['ui'])   # ui/x.py
        self.assertEqual(rows['docs/api.md:7']['tags'], ['docs'])


class ReviewRuleTests(StateCase):
    def test_a_shadow_review_row_is_shadow_and_an_unstated_one_is_unlabelled(self):
        rows, _ = self.rows()
        self.assertEqual(rows['ui/app.js:30']['status'], 'shadow')
        self.assertEqual(rows['ui/unst.py:5']['status'], 'unlabelled')
        self.assertEqual(rows['ui/reader.py:42']['status'], 'superseded')
        self.assertEqual(rows['ui/again.py:3']['status'], 'open')

    def test_every_row_carries_the_record_fields(self):
        rows, _ = self.rows()
        for row in rows.values():
            self.assertTrue({'record_fp', 'record', 'tags', 'draft', 'status', 'fileable'} <= set(row))
        self.assertIsNone(rows['docs/api.md:7']['record_fp'])          # no severity: forge issues cannot parse it

    def test_without_forge_findings_the_old_status_stays_and_the_answer_says_so(self):
        with mock.patch.dict(P._STATE, {'record': None}):
            rows, found = self.rows()
        self.assertEqual(rows['ui/app.js:30']['status'], 'open')
        self.assertIn('the finding record could not be read', found['notes'])
        self.assertEqual(rows['ui/again.py:3']['tags'], [])

    def test_a_corrupt_record_is_the_same_note(self):
        folder = self.state / 'forge' / 'findings'
        folder.mkdir(parents=True)
        (folder / ('%s.record.json' % self.ident['key'])).write_text('{nope')
        rows, found = self.rows()
        self.assertIn('the finding record could not be read', found['notes'])
        self.assertEqual(rows['ui/app.js:30']['status'], 'open')


class OpenCountTests(StateCase):
    def count(self):
        found = P.Screen().listing(self.catalog)['projects']
        self.assertEqual([p['id'] for p in found], [self.pid])
        return found[0]['open_findings']

    def test_shadow_unlabelled_and_superseded_rows_are_not_counted(self):
        rows, _ = self.rows()
        worked_on = [r for r in rows.values() if r['status'] in ('open', 'to-file')]
        self.assertLess(len(worked_on), len(rows))
        self.assertEqual(self.count(), len(worked_on))
        self.assertGreater(len(rows) - len(worked_on), 2)

    def test_a_row_the_record_closed_is_not_counted(self):
        before = self.count()
        self.write_record({self.fp(self.AGAIN): dict(status='fixed'), self.fp(self.LOOSE): dict(status='dismissed')})
        self.assertEqual(self.count(), before - 2)

    def test_the_count_without_the_record_is_the_old_one(self):
        with mock.patch.dict(P._STATE, {'record': None}):
            rows, _ = self.rows()
            self.assertEqual(self.count(), sum(1 for r in rows.values() if not r['superseded']))


if __name__ == '__main__':
    unittest.main()
