"""`forge issues` by repository identity: every run of the repository, a slug when the checkout is gone, the record.

State and config are isolated under a temp XDG_STATE_HOME / XDG_CONFIG_HOME; `gh` is a fake on PATH, as in test_issues.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/forge-issues.py'
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_identity  # noqa: E402
import forge_locations  # noqa: E402
import forge_registry  # noqa: E402
from forge_findings import store  # noqa: E402

GIT_ENV = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t',
               GIT_COMMITTER_EMAIL='t@t', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')

FAKE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ['FAKE_GH_STATE']
    state = json.load(open(path))
    args = sys.argv[1:]
    state['calls'].append(args)
    def val(flag):
        return args[args.index(flag) + 1] if flag in args else None
    if args[:2] == ['issue', 'list']:
        out = json.dumps(state['issues'])
    elif args[:2] == ['issue', 'create']:
        n = len(state['issues']) + 1
        url = 'https://github.com/%s/issues/%d' % (val('--repo'), n)
        state['issues'].append({'number': n, 'url': url, 'state': 'OPEN', 'title': val('--title'),
                                'body': open(val('--body-file')).read()})
        out = url
    elif args[:2] == ['label', 'list']:
        out = '[]'
    elif args[0] == 'api':
        out = 'abc123'
    else:
        sys.exit('fake gh: unexpected ' + ' '.join(args))
    json.dump(state, open(path, 'w'))
    print(out)
    ''')


def git(cwd, *args):
    return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def load_issues():
    spec = importlib.util.spec_from_file_location('forge_issues_identity_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdentityCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_home, self.config = self.tmp / 'state', self.tmp / 'config'
        bin_dir = self.tmp / 'bin'
        bin_dir.mkdir()
        (bin_dir / 'gh').write_text(FAKE_GH)
        (bin_dir / 'gh').chmod(0o755)
        self.gh_state = self.tmp / 'gh.json'
        self.gh_state.write_text(json.dumps({'issues': [], 'calls': []}))
        env = patch.dict(os.environ, {'XDG_STATE_HOME': str(self.state_home), 'XDG_CONFIG_HOME': str(self.config),
                                      'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'FAKE_GH_STATE': str(self.gh_state)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('FORGE_RUN_REGISTRY', None)
        forge_identity._identity_cache.clear()
        forge_identity._checkouts_cache.clear()
        forge_locations._IDENTITY.clear()
        self.repo = self.make_repo('work/app', origin='git@github.com:o/r.git')
        self.ident = forge_identity.identity(self.repo)
        self.count = 0

    def make_repo(self, name, origin=None):
        path = self.tmp / name
        path.mkdir(parents=True)
        git(path, 'init', '-q', '-b', 'main')
        git(path, 'commit', '-q', '--allow-empty', '-m', 'base')
        if origin:
            git(path, 'remote', 'add', 'origin', origin)
        return path

    def worktree(self, name):
        path = self.tmp / 'work' / name
        git(self.repo, 'worktree', 'add', '-q', '-b', name, str(path))
        return path

    def plan(self, name, checkout, *texts, identity_root=None):
        """A plan dir of `checkout` with one merged task per text, registered like a started run."""
        run = self.tmp / 'runs' / name
        (run / 'tasks').mkdir(parents=True)
        (run / 'repo').write_text(str(checkout) + '\n')
        if identity_root:
            (run / 'repo.identity').write_text(json.dumps({'root': str(identity_root), 'worktree': str(checkout),
                                                           'branch': name}) + '\n')
        for i, text in enumerate(texts):
            task = run / 'tasks' / f't{i}'
            task.mkdir()
            (task / 'status').write_text('MERGED\n')
            (task / 'merged').write_text('abc123\n')
            (task / 'qa_threshold').write_text('P1\n')
            (task / 'known_issues.md').write_text(f'## Known issues\n\n- [P2][CONFIRMED] src/{name}.py:{i + 1} — {text}\n')
        forge_registry.register_run_dir(checkout, run)
        return run

    def cli(self, *args, code=0):
        res = subprocess.run([sys.executable, str(SCRIPT), *args, '--json'], capture_output=True, text=True,
                             env=os.environ.copy(), timeout=60)
        self.assertEqual(res.returncode, code, res.stderr)
        return json.loads(res.stdout) if code == 0 else res.stderr

    def listed(self, *args, run=None):
        return self.cli(str(run or self.repo), '--offline', '--all-runs', '--include-plausible', *args)

    def by_text(self, out):
        return {f['text'].split('.')[0]: f for f in out['findings']}

    def record_file(self):
        return self.state_home / 'forge' / 'findings' / f"{self.ident['key']}.record.json"

    def record(self):
        return json.loads(self.record_file().read_text())['findings']

    def gh_calls(self):
        return json.loads(self.gh_state.read_text())['calls']


class EveryRunTests(IdentityCase):
    def test_all_runs_lists_runs_of_linked_and_removed_worktrees(self):
        wt = self.worktree('linked')
        gone = self.worktree('gone')
        self.plan('main-run', self.repo, 'Main finding.')
        self.plan('linked-run', wt, 'Linked finding.')
        removed = self.plan('removed-run', gone, 'Removed finding.', identity_root=self.repo)
        git(self.repo, 'worktree', 'remove', '--force', str(gone))
        self.assertFalse(gone.exists())

        out = self.listed()
        found = self.by_text(out)
        self.assertEqual(sorted(found), ['Linked finding', 'Main finding', 'Removed finding'])
        self.assertEqual(out['repo'], 'o/r')
        self.assertEqual(out['warnings'], [])
        # a worktree's own plan names the same repository: the same fingerprints, the same record
        from_linked = self.listed(run=wt)
        self.assertEqual({f['fp'] for f in from_linked['findings']}, {f['fp'] for f in out['findings']})
        # the removed worktree's plan: slug from the root's origin, a warning, no failure
        alone = self.cli(str(removed), '--offline')
        self.assertEqual(alone['repo'], 'o/r')
        self.assertEqual([f['text'].split('.')[0] for f in alone['findings']], ['Removed finding'])
        self.assertEqual(alone['findings'][0]['fp'], found['Removed finding']['fp'])
        self.assertEqual(len(alone['warnings']), 1)
        self.assertIn('no longer exists', alone['warnings'][0])
        self.assertIn(str(self.repo), alone['warnings'][0])
        every = self.cli(str(removed), '--offline', '--all-runs')
        self.assertEqual(sorted(self.by_text(every)), ['Linked finding', 'Main finding', 'Removed finding'])
        self.assertIn('no longer exists', every['warnings'][0])

    def test_a_directory_without_tasks_is_skipped_with_a_note(self):
        run = self.plan('empty', self.repo)
        shutil.rmtree(run / 'tasks')
        out = self.listed()
        self.assertTrue(any('no tasks/ directory, skipped' in n for n in out['notes']), out['notes'])

    def test_a_repository_with_no_origin_lists_with_empty_slug_fingerprints(self):
        bare = self.make_repo('work/bare')
        run = self.plan('bare-run', bare, 'Lonely finding.')
        out = self.cli(str(run), '--offline')
        self.assertEqual(out['repo'], '')
        self.assertTrue(any('no GitHub origin remote' in w for w in out['warnings']), out['warnings'])
        mod = load_issues()
        f = out['findings'][0]
        self.assertEqual(f['fp'], mod.fingerprint('', {'file': "src/bare-run.py", 'text': f['text']}))
        self.assertEqual(len(out['drafts']), 1)
        # online listing needs no GitHub either, and asks it nothing
        online = self.cli(str(run))
        self.assertEqual(online['repo'], '')
        self.assertEqual(self.gh_calls(), [])
        # filing still needs a slug, and --repo gives one
        err = self.cli(str(run), '--apply', code=2)
        self.assertIn('has no GitHub origin remote; pass --repo owner/repo', err)
        named = self.cli(str(run), '--offline', '--repo', 'Acme/Thing')
        self.assertEqual(named['repo'], 'Acme/Thing')
        self.assertNotEqual(named['findings'][0]['fp'], f['fp'])


class RecordTests(IdentityCase):
    def test_dismiss_and_undismiss_write_the_record_not_the_old_store(self):
        wt = self.worktree('linked')
        run = self.plan('linked-run', wt, 'Linked finding.')
        fp = self.cli(str(run), '--offline')['findings'][0]['fp']
        out = self.cli(str(run), '--dismiss', fp, '--reason', 'not a bug')
        self.assertEqual(out, {'ok': True, 'dismissed': [fp], 'undismissed': [], 'warnings': []})
        record = self.record()
        self.assertEqual((record[fp]['status'], record[fp]['reason'], record[fp]['actor']), ('dismissed', 'not a bug', 'issues'))
        findings = self.state_home / 'forge' / 'findings'
        self.assertEqual(sorted(p.name for p in findings.glob('*.json')), [self.record_file().name])
        listed = self.listed()
        entry = listed['findings'][0]
        self.assertEqual(entry['status'], 'dismissed')
        self.assertEqual(entry['dismissed'], {'at': int(record[fp]['at']), 'reason': 'not a bug'})
        self.assertEqual(entry['record']['status'], 'dismissed')
        self.assertEqual(listed['drafts'], [])
        self.assertEqual(listed['summary']['dismissed'], 1)
        self.assertEqual(len(self.listed('--include-dismissed')['drafts']), 1)
        # dismissing again without a reason keeps the earlier one
        self.cli(str(run), '--dismiss', fp)
        self.assertEqual(self.record()[fp]['reason'], 'not a bug')
        self.cli(str(run), '--undismiss', fp)
        self.assertEqual(self.record()[fp]['status'], 'open')
        again = self.listed()
        self.assertIsNone(again['findings'][0]['dismissed'])
        self.assertEqual(again['findings'][0]['status'], 'open')
        self.assertEqual(len(again['drafts']), 1)

    def test_a_task_dir_or_file_in_a_plan_resolves_the_plans_repository(self):
        wt = self.worktree('linked')
        run = self.plan('linked-run', wt, 'Linked finding.')
        task = run / 'tasks' / 't0'
        fp = self.cli(str(run), '--offline')['findings'][0]['fp']
        for form in (task, task / 'known_issues.md'):
            with self.subTest(form=form.name):
                self.cli(str(form), '--dismiss', fp, '--reason', 'x')
                findings = self.state_home / 'forge' / 'findings'
                self.assertEqual(sorted(p.name for p in findings.glob('*.json')), [self.record_file().name])
                self.assertEqual(self.record()[fp]['status'], 'dismissed')
                self.assertEqual(self.listed()['findings'][0]['status'], 'dismissed')
                self.assertEqual(self.cli(str(form), '--offline')['findings'][0]['status'], 'dismissed')
                self.cli(str(form), '--undismiss', fp)
                self.assertEqual(self.record()[fp]['status'], 'open')

    def test_an_old_store_dismissal_of_a_worktree_still_shows_and_is_never_written(self):
        wt = self.worktree('linked')
        self.plan('linked-run', wt, 'Linked finding.')
        fp = self.listed()['findings'][0]['fp']
        findings = self.state_home / 'forge' / 'findings'
        findings.mkdir(parents=True)
        old = findings / f'{forge_registry.repo_key(wt)}.json'
        self.assertNotEqual(old.name, f"{self.ident['key']}.json")
        old.write_text(json.dumps({fp: {'at': 1700000000, 'reason': 'old reason'}}))
        before = old.read_text()
        entry = self.listed()['findings'][0]
        self.assertEqual(entry['dismissed'], {'at': 1700000000, 'reason': 'old reason'})
        self.assertEqual(entry['status'], 'dismissed')
        self.assertEqual(entry['record'], {'status': 'dismissed', 'at': 1700000000, 'actor': 'issues', 'reason': 'old reason'})
        self.assertFalse(self.record_file().exists())
        self.assertEqual(self.listed()['drafts'], [])
        # undismissing reopens it in the record; the old file is left as it was
        self.cli(str(self.repo), '--all-runs', '--undismiss', fp)
        self.assertEqual(old.read_text(), before)
        self.assertEqual(self.listed()['findings'][0]['status'], 'open')
        self.assertEqual(len(self.listed()['drafts']), 1)
        # load_dismissed still answers for forge-sweep, from the record
        mod = load_issues()
        self.assertEqual(mod.load_dismissed(self.ident['key']), {})
        self.cli(str(self.repo), '--all-runs', '--dismiss', fp, '--reason', 'again')
        self.assertEqual(list(mod.load_dismissed(self.ident['key'])), [fp])

    def test_status_and_drafts_follow_the_record(self):
        self.plan('run', self.repo, 'Open one.', 'Fixed one.', 'Duplicate one.', 'Filed one.', 'To file one.')
        found = self.by_text(self.listed())
        fp = {name: f['fp'] for name, f in found.items()}
        self.assertEqual({f['status'] for f in found.values()}, {'open'})
        self.assertTrue(all(f['record'] is None for f in found.values()))
        store.set_status(self.ident, [fp['Fixed one']], 'fixed', 'cli', reason='done')
        store.set_status(self.ident, [fp['Duplicate one']], 'duplicate', 'cli', duplicate_of=fp['Open one'])
        store.set_status(self.ident, [fp['Filed one']], 'filed', 'cli', issue='https://github.com/o/r/issues/7')
        store.update(self.ident, [fp['To file one']], 'cli', status='to-file',
                     draft={'title': 'Hand written title', 'body': 'Also check the importer.'})
        out = self.listed()
        found = self.by_text(out)
        self.assertEqual({name: f['status'] for name, f in found.items()},
                         {'Open one': 'open', 'Fixed one': 'fixed', 'Duplicate one': 'duplicate', 'Filed one': 'filed',
                          'To file one': 'to-file'})
        self.assertEqual(found['Fixed one']['record']['reason'], 'done')
        self.assertIsNone(found['Fixed one']['dismissed'])
        self.assertEqual(out['summary']['fixed'], 1)
        self.assertEqual(out['summary']['duplicate'], 1)
        self.assertEqual(out['summary']['to_file'], 1)
        drafted = {x for d in out['drafts'] for x in d['fingerprints']}
        self.assertEqual(drafted, {fp['Open one'], fp['To file one']})
        # nothing about a fixed, duplicate or filed finding is drafted, even with --include-dismissed
        self.assertEqual({x for d in self.listed('--include-dismissed')['drafts'] for x in d['fingerprints']}, drafted)
        # the title a person wrote wins, and their body follows the generated one under Notes
        titled = next(d for d in out['drafts'] if d['fingerprints'] == [fp['To file one']])
        self.assertEqual(titled['title'], 'Hand written title')
        online = self.cli(str(self.repo), '--all-runs', '--include-plausible')
        body = next(d for d in online['drafts'] if d['fingerprints'] == [fp['To file one']])['body']
        self.assertIn('## Notes\nAlso check the importer.', body)
        self.assertLess(body.index('## Context'), body.index('## Notes'))
        plain = next(d for d in online['drafts'] if d['fingerprints'] == [fp['Open one']])
        self.assertNotIn('## Notes', plain['body'])
        self.assertTrue(plain['title'].startswith('[P2] '))

    def test_to_file_keeps_the_findings_the_record_marks_to_file(self):
        self.plan('run', self.repo, 'Open one.', 'Marked one.', 'Marked other.')
        fp = {name: f['fp'] for name, f in self.by_text(self.listed()).items()}
        store.set_status(self.ident, [fp['Marked one'], fp['Marked other']], 'to-file', 'cli')
        chosen = self.listed('--to-file')
        self.assertEqual({f['fp'] for f in chosen['findings']}, {fp['Marked one'], fp['Marked other']})
        self.assertEqual(chosen['summary']['findings'], 2)
        both = self.listed('--to-file', '--only', fp['Marked one'], '--only', fp['Open one'])
        self.assertEqual([f['fp'] for f in both['findings']], [fp['Marked one']])
        self.assertEqual([d['fingerprints'] for d in both['drafts']], [[fp['Marked one']]])
        self.assertIn('--to-file', subprocess.run([sys.executable, str(SCRIPT), '--help'], capture_output=True,
                                                  text=True, env=os.environ.copy()).stdout)

    def test_apply_records_each_created_issue_as_filed(self):
        wt = self.worktree('linked')
        self.plan('linked-run', wt, 'First one.', 'Second one.')
        out = self.cli(str(self.repo), '--all-runs', '--include-plausible', '--apply')
        urls = [c['url'] for c in out['created'] if 'url' in c]
        self.assertEqual(len(urls), 2)
        record = self.record()
        fps = {f['fp'] for f in out['findings']}
        self.assertEqual(set(record), fps)
        self.assertEqual({e['issue'] for e in record.values()}, set(urls))
        self.assertTrue(all(e['status'] == 'filed' and e['actor'] == 'issues' for e in record.values()))
        # gh forgets the issues: the record alone keeps the findings from being filed twice
        self.gh_state.write_text(json.dumps({'issues': [], 'calls': []}))
        again = self.cli(str(self.repo), '--all-runs', '--include-plausible', '--apply')
        self.assertEqual(again['created'], [])
        self.assertEqual({f['status'] for f in again['findings']}, {'filed'})
        self.assertEqual([c for c in self.gh_calls() if c[:2] == ['issue', 'create']], [])


class InProcessTests(IdentityCase):
    def test_listing_returns_the_json_object_of_a_listing(self):
        wt = self.worktree('linked')
        self.plan('main-run', self.repo, 'Main finding.')
        run = self.plan('linked-run', wt, 'Linked finding.')
        fp = self.by_text(self.listed())['Linked finding']['fp']
        store.set_status(self.ident, [fp], 'dismissed', 'cli', reason='r')
        mod = load_issues()
        expected = self.cli(str(self.repo), '--offline', '--all-runs', '--include-plausible', '--include-dismissed')
        got = mod.listing(self.repo)
        self.assertEqual(json.loads(json.dumps(got)), expected)
        self.assertEqual(sorted(got), ['already_filed', 'drafts', 'findings', 'notes', 'online', 'plan', 'repo', 'summary', 'warnings'])
        self.assertEqual(len(got['drafts']), 2)
        # the same flags as a call: no plausible, no dismissed, one run
        narrow = mod.listing(run, include_plausible=False, include_dismissed=False, all_runs=False)
        self.assertEqual(json.loads(json.dumps(narrow)),
                         self.cli(str(run), '--offline'))
        self.assertEqual(narrow['drafts'], [])
        # offline unless asked: a call that is not offline reaches gh
        mod.listing(self.repo, online=True)
        self.assertTrue(any(c[:2] == ['issue', 'list'] for c in self.gh_calls()))

    def test_listing_raises_the_fail_main_would_print(self):
        mod = load_issues()
        with self.assertRaises(mod.Fail) as caught:
            mod.listing(self.tmp / 'nowhere')
        self.assertIn('nowhere', str(caught.exception))
        bare = self.make_repo('work/bare')
        run = self.plan('bare-run', bare, 'Lonely finding.')
        listed = mod.listing(run, all_runs=False)
        self.assertEqual(listed['repo'], '')
        self.assertEqual(len(listed['findings']), 1)


if __name__ == '__main__':
    unittest.main()
