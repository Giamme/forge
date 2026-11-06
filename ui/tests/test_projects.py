"""The Projects screen (#126): grouping by repository, every finding of every review with its status and repeats,
the dataset for runs that are gone, the memories merged from each checkout, and the /api/projects routes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from forge_ui import projects as P, reader
import run_fixture as F

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
REPEAT = '- [P2][CONFIRMED] ui/again.py:3 — the same bug, reported in both rounds.\n\nFORGE_VERDICT: FAIL\n'
TAG = '- [P1][CONFIRMED] ui/x.py:1 — <img src=x onerror=alert(1)> is shown as text.\n\nFORGE_VERDICT: FAIL\n'
LOOSE = '- **P2** [CONFIRMED] ui/loose.py:4: written in another form.\n\nFORGE_VERDICT: FAIL\n'


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV).stdout.strip()


def plan_dir(base, name, repo, run_id=None, base_commit=None):
    p = Path(base) / name
    (p / 'tasks').mkdir(parents=True)
    (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
    (p / 'run_id').write_text((run_id or name) + '\n')
    (p / 'repo').write_text(str(repo) + '\n')
    if base_commit:
        (p / 'integration.base').write_text(base_commit + '\n')
    return p


class IdTests(unittest.TestCase):
    def test_the_id_is_the_run_clouds(self):
        # cloudmodel.js hashId, computed in node for the same strings (projectsview.test.js checks the JS side)
        for root, expected in (('/Users/ayoub/dev/wizaird/forge', 'f94eba50'), ('/tmp/rèpo ✓', 'eaa95eb2'),
                               ('/example/fixture-repo', 'e4d6488d')):
            self.assertEqual(P.project_id(root), expected, root)


class GroupingTests(unittest.TestCase):
    def test_runs_from_the_root_a_linked_worktree_and_a_removed_one_are_one_project(self):
        tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, tmp, True)
        root = tmp / 'app'
        root.mkdir()
        git(root, 'init', '-q', '-b', 'main')
        (root / 'f').write_text('1\n'); git(root, 'add', 'f'); git(root, 'commit', '-qm', 'base')
        head = git(root, 'rev-parse', 'HEAD')
        git(root, 'worktree', 'add', '-q', '-b', 'side', str(tmp / 'side'))
        runs = tmp / 'runs'
        plans = [plan_dir(runs, 'from-root', root), plan_dir(runs, 'from-linked', tmp / 'side'),
                 plan_dir(runs, 'from-gone', tmp / 'gone-worktree', base_commit=head)]
        catalog = reader.Catalog(plans=plans, registry=False)
        found = P.Screen().listing(catalog)['projects']
        self.assertEqual([(p['root'], p['runs']) for p in found], [(str(root), 3)])
        self.assertEqual(found[0]['id'], P.project_id(str(root)))
        detail = P.Screen().detail(catalog, found[0]['id'])
        self.assertEqual([c['path'] for c in detail['checkouts']], [str(root), str(tmp / 'gone-worktree'), str(tmp / 'side')])
        self.assertEqual([c['exists'] for c in detail['checkouts']], [True, False, True])


class FindingsCase(unittest.TestCase):
    """The rich plan fixture, plus a task whose two rounds report the same finding."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan = F.make_plan(Path(tmp.name) / 'p')
        tsv = self.plan / 'tasks.tsv'
        tsv.write_text(tsv.read_text() + 'again\t-\tlow\tui/again.py\tluna\topus\tFix it again\n'
                       'html\t-\tlow\tui/x.py\tluna\topus\tShow text\n'
                       'loose\t-\tlow\tui/loose.py\tluna\topus\tLoose form\n')
        for task, last in (('again', REPEAT), ('html', TAG), ('loose', LOOSE)):
            tdir = self.plan / 'tasks' / task
            for n in (1, 2) if task == 'again' else (1,):
                parent = 'task-%032x' % (n + {'again': 100, 'html': 200, 'loose': 500}[task])
                F.execution(tdir, parent, 100.0 * n, 100.0 * n + 50, exit_code=1, attempt_start=n - 1, trigger='initial')
                F.execution(tdir, 'qa-%032x' % (n + {'again': 300, 'html': 400, 'loose': 600}[task]), 100.0 * n + 10, 100.0 * n + 40,
                            parent=parent, spec=('opus', None, 'claude'), last=last)
            (tdir / 'status').write_text('FAIL\n')
        self.catalog = reader.Catalog(plans=[self.plan], registry=False)
        self.key = reader.run_key(self.plan)
        self.pid = P.project_id('/example/fixture-repo')

    def findings(self, history=None, issues=None):
        return P.Screen().findings(self.catalog, self.pid, lambda: history, lambda path: issues)

    def by_summary(self, rows):
        return {r['summary'].split(' — ', 1)[-1][:30]: r for r in rows}


class FindingsTests(FindingsCase):
    def test_every_review_of_every_attempt_appears_with_where_it_came_from(self):
        rows = self.by_summary(self.findings()['rows'])
        first = rows['a missing status file raises i']
        self.assertEqual((first['task'], first['attempt'], first['reviewer'], first['kind'], first['run']), ('reader', 1, 'opus', 'primary', self.key))
        self.assertTrue(first['href'].startswith('#/runs/%s/tasks/reader/diff/finding/' % self.key))
        shadow = rows['the refresh timer is never cle']
        # the primary review of the same attempt passed: that is not a later review, so the shadow's finding stays open
        self.assertEqual((shadow['kind'], shadow['reviewer'], shadow['status']), ('shadow', 'sol:xhigh', 'open'))
        final = rows['the task endpoint accepts an e']
        self.assertEqual((final['task'], final['final_review'], final['kind']), (None, F.REVIEW_OLD, 'final'))
        self.assertTrue(final['href'].startswith('#/runs/%s/final-review/%s/diff/finding/' % (self.key, F.REVIEW_OLD)))
        self.assertIn('the documented field name does', rows)        # an unlabelled finding too

    def test_a_finding_a_later_pass_got_past_is_superseded_never_fixed(self):
        rows = self.by_summary(self.findings()['rows'])
        self.assertEqual(rows['a missing status file raises i']['status'], 'superseded')
        self.assertEqual(rows['a huge wave number may allocat']['status'], 'superseded')
        self.assertEqual(rows['the warning could name the fil']['status'], 'open')   # in the passing review itself
        self.assertEqual(rows['the empty-state text is not lo']['status'], 'open')       # tolerated
        self.assertNotIn('fixed', {r['status'] for r in self.findings()['rows']})

    def test_a_finding_repeated_across_rounds_is_one_row_with_a_count(self):
        rows = [r for r in self.findings()['rows'] if r['task'] == 'again']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['count'], [o['attempt'] for o in rows[0]['occurrences']]), (2, [1, 2]))
        self.assertEqual(rows[0]['attempt'], 2)                                      # the newest leads

    def entry(self, file, text, task, plan_dir=None, **extra):
        return dict(dict(file=file, text=text, task=task, plan_dir=str(plan_dir or self.plan), origin='tolerated',
                         filed=None, dismissed={'at': 't', 'reason': 'by design'}), **extra)

    def test_dismissed_and_filed_come_from_forge_issues(self):
        listing = dict(findings=[self.entry('ui/page.html', 'the empty-state text is not localised.', 'page')])
        rows = self.by_summary(self.findings(issues=listing)['rows'])
        self.assertEqual(rows['the empty-state text is not lo']['status'], 'dismissed')
        self.assertEqual(rows['the empty-state text is not lo']['origin'], 'tolerated')

    def test_an_entry_of_another_run_or_task_is_not_this_findings_state(self):
        text = 'the empty-state text is not localised.'
        for name, entry in (('another run', self.entry('ui/page.html', text, 'page', plan_dir='/elsewhere/other-run')),
                            ('another task', self.entry('ui/page.html', text, 'again'))):
            rows = self.by_summary(self.findings(issues=dict(findings=[entry]))['rows'])
            self.assertEqual(rows['the empty-state text is not lo']['status'], 'open', name)

    def test_a_finding_written_in_another_form_finds_its_entry(self):
        listing = dict(findings=[self.entry('ui/loose.py', 'written in another form.', 'loose')])
        [row] = [r for r in self.findings(issues=listing)['rows'] if r['task'] == 'loose']
        self.assertEqual(row['status'], 'dismissed')

    def test_a_finding_both_reviewers_reported_is_led_by_the_primary_one(self):
        base = dict(source='run', run='k', run_id='r', task='t', final_review=None, attempt=1, reviewer='opus',
                    href=None, fp='abc', superseded=False)
        [row] = P.collapse([dict(base, kind='primary', at=1.0), dict(base, kind='shadow', reviewer='sol', at=2.0)])
        self.assertEqual((row['kind'], row['reviewer'], row['count']), ('primary', 'opus', 2))

    def test_a_run_whose_dir_is_gone_comes_from_the_dataset_labelled(self):
        history = dict(enabled=True, learnings=[], ledger=[], runs=[
            dict(run='gone-uid', run_id='old-run', mode='parallel', repo_path='/example/fixture-repo', base=None, first=1.0, last=2.0)],
            findings=[dict(run='gone-uid', run_id='old-run', task='t', attempt=1, role='qa', scope='task', reviewer='opus',
                           verdict='FAIL', ts=1.5, findings=[dict(head='- [P1][CONFIRMED] lib/old.py:9 — an old bug.',
                                                                  severity=1, status='CONFIRMED', line=1)])])
        found = self.findings(history=history)
        [row] = [r for r in found['rows'] if r['run_id'] == 'old-run']
        self.assertEqual((row['source'], row['href'], row['run'], row['severity'], row['location']),
                         ('dataset', None, None, 'P1', 'lib/old.py:9'))
        self.assertEqual(found['dataset_runs'], 1)

    def test_without_the_dataset_or_forge_issues_the_answer_says_what_is_missing(self):
        notes = self.findings(history=None, issues=None)['notes']
        self.assertTrue(any('dataset could not be read' in n for n in notes))
        self.assertTrue(any('forge issues could not be read' in n for n in notes))
        notes = self.findings(history=dict(enabled=False, runs=[], findings=[], learnings=[], ledger=[]))['notes']
        self.assertTrue(any('FORGE_DATASET=off' in n for n in notes))


class MemoriesTests(unittest.TestCase):
    def test_learnings_from_each_checkout_are_merged_and_say_where_they_came_from(self):
        tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, tmp, True)
        root, side = tmp / 'app', tmp / 'side'
        root.mkdir(); side.mkdir()
        (root / '.git').mkdir()
        plans = [plan_dir(tmp / 'runs', 'r1', root), plan_dir(tmp / 'runs', 'r2', side)]
        (plans[1] / 'repo.identity').write_text(json.dumps(dict(root=str(root), worktree=str(side), branch='b')))
        catalog = reader.Catalog(plans=plans, registry=False)
        pid = P.project_id(str(root))
        row = lambda run, text: dict(ts='2026-11-0%sT00:00:00Z' % run[-1], run_id=run, task='t', role='dwarf', model='m',
                                     verdict='-', text=text, injectable=True)
        answers = {
            str(root): dict(ledger=True, memory=[dict(category='verify', title='Verify', roles=['planner', 'dwarf'],
                                                      lines=[dict(text='make test', count=None)])],
                            rebuilt_differs=False, max_lines=40,
                            learnings=[dict(category='verify', key='make test', text='make test', count=1, state='in_memory',
                                            first='a', last='2026-11-01T00:00:00Z', rows=[row('r1', 'make test')])]),
            str(side): dict(ledger=True, memory=[], rebuilt_differs=False, max_lines=40,
                            learnings=[dict(category='verify', key='make test', text='make test', count=1, state='in_memory',
                                            first='a', last='2026-11-01T00:00:00Z', rows=[row('r1', 'make test')]),
                                       dict(category='trap', key='x', text='x', count=1, state='capped', first='a',
                                            last='2026-11-02T00:00:00Z', rows=[row('r2', 'x')])])}
        history = dict(enabled=True, findings=[], ledger=[], runs=[
            dict(run='gone', run_id='r0', mode='parallel', repo_path=str(root), base=None, first=1, last=1)],
            learnings=[dict(run='gone', run_id='r0', task='t', role='qa', model='m', ts=1.0, category='trap', text='from a removed worktree [a.py]')])
        got = P.Screen().memories(catalog, pid, lambda path: answers.get(path), lambda: history)
        self.assertEqual([s['path'] for s in got['sources']], [str(root), str(side)])
        self.assertTrue(got['differs'])
        by = {l['text']: l for l in got['learnings']}
        self.assertEqual(by['make test']['states'], {str(root): 'in_memory', str(side): 'in_memory'})
        self.assertEqual([r['from'] for r in by['make test']['rows']], [[str(root), str(side)]])   # one row, both sources
        self.assertEqual(by['x']['states'], {str(side): 'capped'})
        self.assertTrue(by['from a removed worktree [a.py]']['dataset_only'])
        self.assertEqual(by['from a removed worktree [a.py]']['rows'][0]['from'], ['dataset'])


if __name__ == '__main__':
    unittest.main()
