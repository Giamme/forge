"""`forge issues`: leftover review findings become GitHub issues through plain `gh` (a fake one here)."""
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
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/forge-issues.py'

FAKE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    path = os.environ['FAKE_GH_STATE']
    state = json.load(open(path))
    args = sys.argv[1:]
    state['calls'].append(args)
    def val(flag):
        return args[args.index(flag) + 1] if flag in args else None
    def save():
        json.dump(state, open(path, 'w'))
    if args[:2] == ['issue', 'list']:
        print(json.dumps(state['issues'])); save()
    elif args[:2] == ['issue', 'create']:
        n = len(state['issues']) + 1
        url = 'https://github.com/%s/issues/%d' % (val('--repo'), n)
        labels = [args[i + 1] for i, a in enumerate(args) if a == '--label']
        state['issues'].append({'number': n, 'url': url, 'state': 'OPEN', 'title': val('--title'),
                                'body': open(val('--body-file')).read(), 'labels': labels})
        save(); print(url)
    elif args[:2] == ['label', 'list']:
        print(json.dumps([{'name': x} for x in state['labels']])); save()
    elif args[0] == 'api':
        save(); sys.exit(0 if args[1].endswith('/' + state['published']) else 1)
    else:
        save(); sys.exit('fake gh: unexpected ' + ' '.join(args))
    ''')

# A `gh` for two processes at once: every call is appended to a log, and `issue create` is slow (so a second
# filer that skipped the lock would look up the issues before the first one lands) and adds to a shared state.
RACE_GH = textwrap.dedent('''\
    #!/usr/bin/env python3
    import fcntl, json, os, sys, time
    state, log = os.environ['RACE_GH_STATE'], os.environ['RACE_GH_LOG']
    args = sys.argv[1:]
    with open(log, 'a') as fh:
        fh.write(' '.join(args[:2]) + '\\n')
    def issues():
        with open(state) as fh:
            return json.load(fh)
    if args[:2] == ['issue', 'list']:
        print(json.dumps(issues()))
    elif args[:2] == ['issue', 'create']:
        time.sleep(1)
        with open(state + '.lock', 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            found = issues()
            url = 'https://github.com/o/r/issues/%d' % (len(found) + 1)
            with open(args[args.index('--body-file') + 1]) as body:
                found.append({'number': len(found) + 1, 'url': url, 'state': 'OPEN',
                              'title': args[args.index('--title') + 1], 'body': body.read()})
            with open(state + '.tmp', 'w') as fh:
                json.dump(found, fh)
            os.replace(state + '.tmp', state)
        print(url)
    elif args[:2] == ['label', 'list']:
        print('[]')
    else:
        sys.exit(1)
    ''')

# Holds a repository lock until its stdin closes: the "another process is filing" of the busy-lock test.
LOCK_HOLDER = textwrap.dedent('''\
    import fcntl, os, sys
    fcntl.flock(os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600), fcntl.LOCK_EX)
    print('locked', flush=True)
    sys.stdin.read()
    ''')


def repo_key(path):
    spec = importlib.util.spec_from_file_location('forge_registry_under_test', ROOT / 'scripts/forge_registry.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.repo_key(path)


class ForgeIssuesTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        bin_dir = self.dir / 'bin'; bin_dir.mkdir()
        (bin_dir / 'gh').write_text(FAKE_GH); (bin_dir / 'gh').chmod(0o755)
        self.state = self.dir / 'gh.json'
        self.write_state(issues=[], labels=['bug'])
        self.repo = self.dir / 'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'add', 'origin', 'git@github.com:o/r.git'], check=True)
        self.state_home = self.dir / 'state'
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', FAKE_GH_STATE=str(self.state),
                        XDG_STATE_HOME=str(self.state_home))
        self.plan = self.dir / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        (self.plan / 'tasks.tsv').write_text('# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
                                             'import\t-\thigh\tsrc\tsol:high:codex\tastra:xhigh:codex\tImport\n'
                                             'ui\t-\tlow\tsrc\tsol:high:codex\tastra:xhigh:codex\tUI\n')
        (self.plan / 'results.tsv').write_text('import\tMERGED\tforge/p/import\nui\tFAIL\tforge/p/ui\n')
        self.task('import', 'MERGED', known=[
            '- [P2][CONFIRMED] src/import.ts:645 — Company creation runs before the receipt replay. A retry '
            'creates an extra company.',
            '- [P3][CONFIRMED] [src/import.ts:775](/abs/src/import.ts:775) — **Persisting `null`** loses the list.'],
            qa=['- [P3][PLAUSIBLE] src/lead.ts:12 — Lead names may keep trailing spaces.'])
        self.task('ui', 'FAIL', known=[], qa=['- [P1][CONFIRMED] src/ui.tsx:3 — Button does nothing.'])

    def write_state(self, **kw):
        state = {'issues': [], 'labels': [], 'calls': [], 'published': 'abc123', **kw}
        self.state.write_text(json.dumps(state))

    def gh_state(self):
        return json.loads(self.state.read_text())

    def task(self, name, status, known, qa):
        d = self.plan / 'tasks' / name
        d.mkdir()
        (d / 'status').write_text(status + '\n')
        (d / 'merged').write_text('abc123\n')
        (d / 'qa_threshold').write_text('P1\n')
        if known:
            (d / 'known_issues.md').write_text('## Known issues\n\n' + '\n\n'.join(known) + '\n')
        (d / 'qa.last').write_text('VERDICT: PASS\n\n' + '\n'.join(known + qa) + '\n')

    def forge_issues(self, *args, code=0):
        res = subprocess.run([sys.executable, str(SCRIPT), *args, '--json'], capture_output=True, text=True,
                             env=self.env, timeout=60)
        self.assertEqual(res.returncode, code, res.stderr)
        return json.loads(res.stdout) if code == 0 else res.stderr

    def created(self):
        return [c for c in self.gh_state()['calls'] if c[:2] == ['issue', 'create']]

    def test_drafts_only_without_apply(self):
        out = self.forge_issues(str(self.plan))
        self.assertEqual(out['repo'], 'o/r')
        self.assertEqual(out['summary']['tolerated'], 2)
        self.assertIn('ui: status FAIL, its findings still block it', out['notes'])
        self.assertEqual([d['title'] for d in out['drafts']],
                         ['[P2] import.ts: Company creation runs before the receipt replay',
                          '[P3] import.ts: Persisting null loses the list'])
        self.assertEqual(self.created(), [])

    def test_apply_files_each_finding_once(self):
        out = self.forge_issues(str(self.plan), '--include-plausible', '--apply', '--lang', 'it',
                                '--label', 'bug', '--label', 'forge')
        self.assertEqual(len([c for c in out['created'] if 'url' in c]), 3)
        self.assertIn({'note': 'labels not in o/r, left off: forge'}, out['created'])
        issues = {i['title']: i for i in self.gh_state()['issues']}
        body = issues['[P2] import.ts: Company creation runs before the receipt replay']['body']
        self.assertIn('sotto la soglia bloccante P1', body)
        self.assertIn('review di `astra:xhigh:codex`', body)
        self.assertIn('https://github.com/o/r/blob/abc123/src/import.ts#L645', body)
        self.assertRegex(body, r'<!-- forge:finding fp=[0-9a-f]{12} source=forge:plan/import -->')
        self.assertIn('[P3] Da verificare: lead.ts: Lead names may keep trailing spaces', issues)
        self.assertEqual({tuple(i['labels']) for i in issues.values()}, {('bug',)})
        again = self.forge_issues(str(self.plan), '--include-plausible', '--apply')
        self.assertEqual(again['summary']['already_filed'], 3)
        self.assertEqual(again['created'], [])
        self.assertEqual(len(self.gh_state()['issues']), 3)

    def test_findings_filed_by_gh_proman_are_not_filed_again(self):
        fp = self.forge_issues(str(self.plan), '--min-severity', 'P2')['drafts'][0]['fingerprints'][0]
        self.write_state(issues=[{'number': 9, 'url': 'https://github.com/o/r/issues/9', 'state': 'CLOSED',
                                  'title': 'import.ts: Company creation…',
                                  'body': f'<!-- gh-proman:finding fp={fp} source=forge:plan/import -->'}])
        out = self.forge_issues(str(self.plan), '--min-severity', 'P2', '--apply')
        self.assertEqual(out['already_filed'][0]['issue'], 'https://github.com/o/r/issues/9')
        self.assertEqual(self.created(), [])

    def test_fingerprint_matches_gh_proman(self):
        # gh-proman's finding_fp gives the same value (its tests assert it too): keep them in step.
        res = subprocess.run([sys.executable, '-c', 'import importlib.util as u, sys; '
                              's = u.spec_from_file_location("fi", sys.argv[1]); m = u.module_from_spec(s); '
                              's.loader.exec_module(m); print(m.fingerprint("Owner/Repo", {"file": "src/a.ts", '
                              '"text": "Null  deref when the  list is empty."}))', str(SCRIPT)],
                             capture_output=True, text=True, check=True)
        self.assertEqual(res.stdout.strip(), 'd19f17f6ce07')

    def test_open_issue_with_same_title_is_skipped_and_grouping(self):
        self.write_state(issues=[{'number': 4, 'url': 'https://github.com/o/r/issues/4', 'state': 'OPEN',
                                  'title': '[P1] import.ts: Company creation runs before the receipt replay',
                                  'body': 'by hand'}])
        out = self.forge_issues(str(self.plan), '--min-severity', 'P2', '--apply')
        self.assertEqual(out['created'][0]['skipped'], 'an open issue has the same title')
        out = self.forge_issues(str(self.plan), '--include-plausible', '--group', 'run')
        self.assertEqual([d['title'] for d in out['drafts']],
                         ['[P2] Non-blocking forge findings: plan',
                          '[P3] To verify: lead.ts: Lead names may keep trailing spaces'])

    def test_unpublished_commit_and_whole_run_review(self):
        self.write_state(published='other')
        review = self.plan / 'final-review'; review.mkdir()
        (review / 'qa.resolved').write_text('role=qa\nalias=astra\nharness=codex\neffort=xhigh\n')
        (review / 'reviewed.commit').write_text('def456\n')
        (review / 'qa.last').write_text('- [P1][CONFIRMED] src/a.ts:1 — Broken thing.\n')
        out = self.forge_issues(str(review), '--apply')
        self.assertEqual(out['summary']['review'], 1)
        body = self.gh_state()['issues'][0]['body']
        self.assertIn('whole-run review', body)
        self.assertIn('`src/a.ts:1` (commit not pushed to GitHub yet)', body)

    def test_no_github_remote_is_a_clear_error(self):
        subprocess.run(['git', '-C', str(self.repo), 'remote', 'remove', 'origin'], check=True)
        err = self.forge_issues(str(self.plan), code=2)
        self.assertIn('has no GitHub origin remote; pass --repo owner/repo', err)

    def offline_env(self):
        """PATH with a `gh` that records every call and fails."""
        bin_dir = self.dir / 'nogh'; bin_dir.mkdir(exist_ok=True)
        (bin_dir / 'gh').write_text(f'#!/bin/sh\necho "$@" >> {self.dir}/gh-called\nexit 1\n')
        (bin_dir / 'gh').chmod(0o755)
        return dict(self.env, PATH=f'{bin_dir}:{os.environ["PATH"]}')

    def run_issues(self, *args, env=None, code=0, cwd=None):
        res = subprocess.run([sys.executable, str(SCRIPT), *args, '--json'], capture_output=True, text=True,
                             env=env or self.env, timeout=60, cwd=cwd)
        self.assertEqual(res.returncode, code, res.stderr)
        return json.loads(res.stdout) if code == 0 else res.stderr

    def review_dir(self, plan, name, lines):
        d = plan / name; d.mkdir()
        (d / 'qa.input').write_text('x'); (d / 'reviewed.commit').write_text('def456\n')
        (d / 'qa.last').write_text('\n'.join(lines) + '\n')
        return d

    def test_offline_lists_each_finding_once_without_gh(self):
        env = self.offline_env()
        out = self.run_issues(str(self.plan), '--offline', '--include-plausible', env=env)
        self.assertFalse((self.dir / 'gh-called').exists())
        self.assertEqual(len(out['findings']), 3)
        self.assertEqual([f['severity'] for f in out['findings']], ['P2', 'P3', 'P3'])
        first = out['findings'][0]
        self.assertEqual(sorted(first), sorted(['fp', 'severity', 'confidence', 'origin', 'plan', 'plan_dir', 'task',
                                               'file', 'summary', 'text', 'reviewer', 'commit', 'review', 'swept',
                                               'filed', 'dismissed']))
        self.assertEqual((first['origin'], first['plan'], first['task'], first['filed'], first['dismissed']),
                         ('tolerated', 'plan', 'import', None, None))
        self.assertIsNone(first['review'])
        # Fingerprints hash the repo, the file and the text only. These are the values the formula produced
        # before origin and `review` existed, so a filed issue is still recognised.
        self.assertEqual(first['fp'], 'cf5d9440c970')
        persisting = next(f for f in out['findings'] if f['text'] == 'Persisting null loses the list.')
        self.assertEqual(persisting['fp'], '3f6046d5ed8e')
        self.assertTrue(all(d['same_title'] is None for d in out['drafts']))
        # the commit is in no remote-tracking ref: unpublished wording, still no gh
        self.assertIn('(commit not pushed to GitHub yet)', out['drafts'][0]['body'])

    def test_offline_apply_is_a_usage_error(self):
        self.assertIn('--offline', self.run_issues(str(self.plan), '--offline', '--apply', code=2))

    def test_only_selects_and_reports_unknown(self):
        fps = [f['fp'] for f in self.run_issues(str(self.plan))['findings']]
        out = self.run_issues(str(self.plan), '--only', f'{fps[1]},{"0" * 12}')
        self.assertEqual([d['fingerprints'] for d in out['drafts']], [[fps[1]]])
        self.assertEqual([f['fp'] for f in out['findings']], [fps[1]])
        self.assertTrue(any('0' * 12 in n for n in out['notes']))
        self.run_issues(str(self.plan), '--only', 'xyz', code=2)

    def test_whole_run_review_is_current_only(self):
        old = self.review_dir(self.plan, 'final-review-0-1', ['- [P1][CONFIRMED] src/old.ts:1 — Superseded thing.'])
        cur = self.review_dir(self.plan, 'final-review-1-1', ['- [P1][CONFIRMED] src/a.ts:1 — Current thing.'])
        os.utime(old / 'qa.last', (1, 1))
        os.symlink('final-review-1-1', self.plan / 'final-review')
        out = self.run_issues(str(self.plan), '--offline')
        reviews = [f for f in out['findings'] if f['origin'] == 'review']
        self.assertEqual([(f['file'], f['task']) for f in reviews], [('src/a.ts', None)])
        self.assertEqual(len(out['findings']), 3)
        # a newer integrate-*/final-review wins over the published link
        integ = self.plan / 'integrate-9-9'; integ.mkdir()
        self.review_dir(integ, 'final-review', ['- [P0][CONFIRMED] src/b.ts:2 — Integrate thing.'])
        os.utime(cur / 'qa.last', (1000, 1000))
        out = self.run_issues(str(self.plan), '--offline')
        self.assertEqual([f['file'] for f in out['findings'] if f['origin'] == 'review'], ['src/b.ts'])

    def test_published_review_without_qa_last_is_not_replaced_by_older_integrate(self):
        integ = self.plan / 'integrate-1-1'; integ.mkdir()
        old = self.review_dir(integ, 'final-review', ['- [P1][CONFIRMED] src/old.ts:1 — Old integrate thing.'])
        os.utime(old / 'qa.last', (1, 1))
        fresh = self.plan / 'final-review-2-2'; fresh.mkdir()
        (fresh / 'qa.input').write_text('x')
        os.symlink('final-review-2-2', self.plan / 'final-review')
        out = self.run_issues(str(self.plan), '--offline')
        self.assertEqual([f for f in out['findings'] if f['origin'] == 'review'], [])

    def test_known_issues_file_input_reports_the_plan_dir(self):
        via_dir = self.run_issues(str(self.plan / 'tasks' / 'import'), '--offline', '--repo', 'o/r')
        via_file = self.run_issues(str(self.plan / 'tasks' / 'import' / 'known_issues.md'), '--offline', '--repo', 'o/r')
        pick = lambda o: [(f['plan'], f['plan_dir']) for f in o['findings']]
        self.assertEqual(pick(via_file), pick(via_dir))
        self.assertEqual(pick(via_file)[0][0], 'plan')

    def elsewhere(self, origin='git@github.com:other/b.git'):
        """A cwd unrelated to the run: another repository with its own GitHub origin, or a plain dir."""
        d = self.dir / ('elsewhere-repo' if origin else 'elsewhere')
        d.mkdir()
        if origin:
            subprocess.run(['git', 'init', '-q', str(d)], check=True)
            subprocess.run(['git', '-C', str(d), 'remote', 'add', 'origin', origin], check=True)
        return d

    def test_every_input_form_resolves_the_runs_repository_from_any_cwd(self):
        cwd = self.elsewhere()
        task = self.plan / 'tasks' / 'import'
        listed = lambda: {f['fp']: f['dismissed'] for f in self.run_issues(str(self.plan), '--offline', cwd=cwd)['findings']}
        fp = sorted(listed())[0]
        self.run_issues(str(task / 'known_issues.md'), '--dismiss', fp, cwd=cwd)
        self.assertIsNotNone(listed()[fp])
        self.assertEqual([p.name for p in self.findings_dir().glob('*.json')], [f'{repo_key(self.repo)}.json'])
        for form in (task / 'qa.last', task):
            self.run_issues(str(form), '--undismiss', fp, cwd=cwd)
            self.assertIsNone(listed()[fp])
            self.run_issues(str(form), '--dismiss', fp, cwd=cwd)
            self.assertIsNotNone(listed()[fp])
        self.assertEqual([p.name for p in self.findings_dir().glob('*.json')], [f'{repo_key(self.repo)}.json'])
        # listing through a file names the run's repository, not the cwd's
        out = self.run_issues(str(task / 'known_issues.md'), '--offline', cwd=cwd)
        self.assertEqual((out['repo'], out['warnings']), ('o/r', []))
        self.assertIsNotNone({f['fp']: f['dismissed'] for f in out['findings']}[fp])

    def solo_run(self):
        solo = self.dir / 'solo-run'; solo.mkdir()
        (solo / 'repo').write_text(str(self.repo) + '\n')
        (solo / 'status').write_text('PASS\n')
        (solo / 'merged').write_text('abc123\n')
        (solo / 'qa.last').write_text('VERDICT: PASS\n\n- [P3][PLAUSIBLE] src/solo.ts:4 — Solo edge case.\n')
        return solo

    def test_a_solo_run_lists_and_dismisses_in_its_own_repository_from_any_cwd(self):
        solo, cwd = self.solo_run(), self.elsewhere(origin=None)  # forge-ui calls from its state dir
        env = self.offline_env()
        out = self.run_issues(str(solo), '--offline', '--include-plausible', env=env, cwd=cwd)
        self.assertFalse((self.dir / 'gh-called').exists())
        self.assertEqual((out['repo'], out['warnings']), ('o/r', []))
        self.assertEqual([(f['plan'], f['file']) for f in out['findings']], [('solo-run', 'src/solo.ts')])
        fp = out['findings'][0]['fp']
        self.run_issues(str(solo / 'qa.last'), '--dismiss', fp, env=env, cwd=cwd)
        again = self.run_issues(str(solo), '--offline', '--include-plausible', env=env, cwd=cwd)
        self.assertIsNotNone(again['findings'][0]['dismissed'])
        self.assertEqual(list(json.loads(self.store_path().read_text())), [fp])

    def test_a_run_recording_no_repository_falls_back_to_the_cwd_and_says_so(self):
        solo = self.solo_run()
        (solo / 'repo').unlink()
        out = self.run_issues(str(solo), '--offline', '--include-plausible', cwd=self.repo)
        self.assertEqual(out['repo'], 'o/r')
        self.assertEqual(len(out['warnings']), 1)
        self.assertIn('records no repository; using the current directory', out['warnings'][0])
        text = out['warnings'][0]
        # The listing keeps the two-space indent; a dismissal does not. Both say `warning:`.
        listed = subprocess.run([sys.executable, str(SCRIPT), str(solo), '--offline', '--include-plausible'],
                                capture_output=True, text=True, env=self.env, timeout=60, cwd=self.repo)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([line for line in listed.stdout.splitlines() if line.startswith('  warning')],
                         [f'  warning: {text}'])
        fp = out['findings'][0]['fp']
        dismissed = self.run_issues(str(solo), '--dismiss', fp, cwd=self.repo)
        self.assertEqual(dismissed['warnings'], out['warnings'])
        shown = subprocess.run([sys.executable, str(SCRIPT), str(solo), '--dismiss', fp],
                               capture_output=True, text=True, env=self.env, timeout=60, cwd=self.repo)
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertEqual([line for line in shown.stdout.splitlines() if line.startswith('warning')],
                         [f'warning: {text}'])
        self.assertEqual(list(json.loads(self.store_path().read_text())), [fp])

    def test_threshold_review_with_known_issues_is_collected(self):
        line = '- [P2][CONFIRMED] src/a.ts:1 — X.'
        cur = self.review_dir(self.plan, 'final-review-1-1', [line])
        (cur / 'known_issues.md').write_text(line + '\n'); (cur / 'verdict').write_text('ACCEPTED\n')
        os.symlink('final-review-1-1', self.plan / 'final-review')
        out = self.run_issues(str(self.plan), '--offline')
        reviews = [f for f in out['findings'] if f['origin'] == 'review']
        self.assertEqual([(f['file'], f['task']) for f in reviews], [('src/a.ts', None)])
        # the same review dir given directly reports the same task and source marker
        direct = self.run_issues(str(self.plan / 'final-review'), '--offline', '--repo', 'o/r')
        self.assertEqual([f['task'] for f in direct['findings']], [None])
        self.assertIn('source=forge:plan/final-review', direct['drafts'][0]['body'])

    def test_integrate_review_given_directly_reports_the_plan(self):
        integ = self.plan / 'integrate-5-5'; integ.mkdir()
        self.review_dir(integ, 'final-review', ['- [P2][CONFIRMED] src/a.ts:1 — X.'])
        os.symlink('integrate-5-5/final-review', self.plan / 'final-review')
        direct = self.run_issues(str(self.plan / 'final-review'), '--offline', '--repo', 'o/r')
        via = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r')
        pick = lambda o: [(f['plan'], f['plan_dir'], f['task']) for f in o['findings'] if f['origin'] == 'review']
        self.assertEqual(pick(direct), pick(via))
        self.assertEqual(pick(direct)[0][0], 'plan')

    def test_review_qa_last_file_input_reports_the_plan(self):
        self.review_dir(self.plan, 'final-review-1-1', ['- [P1][CONFIRMED] src/a.ts:1 — Review thing.'])
        os.symlink('final-review-1-1', self.plan / 'final-review')
        integ = self.plan / 'integrate-5-5'; integ.mkdir()
        self.review_dir(integ, 'final-review', ['- [P1][CONFIRMED] src/b.ts:1 — Integrate thing.'])
        pick = lambda o: [(f['plan'], f['plan_dir'], f['task'], f['origin']) for f in o['findings']]
        for rel in ('final-review/qa.last', 'final-review-1-1/qa.last', 'integrate-5-5/final-review/qa.last'):
            out = self.run_issues(str(self.plan / rel), '--offline', '--repo', 'o/r')
            self.assertEqual(pick(out), [('plan', str(self.plan.resolve()), None, 'review')], rel)

    def test_review_known_issues_file_input_matches_directory(self):
        line = '- [P2][CONFIRMED] src/a.ts:1 — Review thing.'
        cur = self.review_dir(self.plan, 'final-review-1-1', [line])
        (cur / 'known_issues.md').write_text(line + '\n')
        os.symlink('final-review-1-1', self.plan / 'final-review')
        pick = lambda o: [(f['plan'], f['task'], f['origin']) for f in o['findings'] if f['file'] == 'src/a.ts']
        want = pick(self.run_issues(str(self.plan), '--offline', '--repo', 'o/r'))
        self.assertEqual(want, [('plan', None, 'review')])
        out = self.run_issues(str(self.plan / 'final-review' / 'known_issues.md'), '--offline', '--repo', 'o/r')
        self.assertEqual(pick(out), want)
        self.assertIn('source=forge:plan/final-review', out['drafts'][0]['body'])

    def test_unfinished_task_keeps_its_name(self):
        pend = self.plan / 'tasks' / 'pending'; pend.mkdir()
        (pend / 'qa.last').write_text('- [P1][CONFIRMED] src/b.ts:1 — Pending thing.\n')
        for arg in (str(self.plan), str(pend)):
            out = self.run_issues(arg, '--offline', '--repo', 'o/r')
            got = [f['task'] for f in out['findings'] if f['file'] == 'src/b.ts']
            self.assertEqual(got, ['pending'], arg)

    def test_all_runs_dedupes_to_the_newest_plan(self):
        xdg = self.dir / 'xdg'
        env = dict(self.env, XDG_CONFIG_HOME=str(xdg))
        newer = self.dir / 'plan2'
        (newer / 'tasks' / 'import').mkdir(parents=True)
        (newer / 'repo').write_text(str(self.repo) + '\n')
        d = newer / 'tasks' / 'import'
        (d / 'status').write_text('MERGED\n'); (d / 'merged').write_text('abc123\n')
        (d / 'known_issues.md').write_text('- [P2][CONFIRMED] src/import.ts:645 — Company creation runs before '
                                           'the receipt replay. A retry creates an extra company.\n')
        os.utime(self.plan, (1000, 1000))
        sys.path.insert(0, str(ROOT / 'scripts'))
        reg = xdg / 'forge/runs'; reg.mkdir(parents=True)
        key = subprocess.run([sys.executable, '-c', 'import sys; sys.path.insert(0, sys.argv[1]); '
                              'from forge_registry import repo_key; print(repo_key(sys.argv[2]))',
                              str(ROOT / 'scripts'), str(self.repo)], capture_output=True, text=True,
                             check=True).stdout.strip()
        (reg / f'{key}.txt').write_text(f'# runs\n{self.plan}\n{newer}\n{self.dir}/gone\n')
        out = self.run_issues(str(self.repo), '--all-runs', '--offline', env=env)
        shared = [f for f in out['findings'] if f['file'] == 'src/import.ts' and f['severity'] == 'P2']
        self.assertEqual([f['plan'] for f in shared], ['plan2'])
        self.assertEqual(len(out['findings']), 2)
        self.assertTrue(any('gone' in n and 'missing' in n for n in out['notes']))
        draft = next(d for d in out['drafts'] if d['severity'] == 'P2')
        self.assertIn('forge `plan2`', draft['body'])
        self.assertIn('source=forge:plan2/import', draft['body'])
        # a plan dir naming the repo works too
        again = self.run_issues(str(self.plan), '--all-runs', '--offline', env=env)
        self.assertEqual(len(again['findings']), 2)


    # ------------------------------------------------------------------------ dismissals and the repository lock

    def findings_dir(self):
        return self.state_home / 'forge' / 'findings'

    def store_path(self):
        return self.findings_dir() / f'{repo_key(self.repo)}.json'

    def lock_path(self):
        return self.findings_dir() / f'{repo_key(self.repo)}.lock'

    def fps(self):
        return [f['fp'] for f in self.run_issues(str(self.plan), '--offline')['findings']]

    def draft_fps(self, out):
        return [fp for d in out['drafts'] for fp in d['fingerprints']]

    def hold_lock(self):
        """A separate process holding the repository lock, released by closing its stdin."""
        self.findings_dir().mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen([sys.executable, '-c', LOCK_HOLDER, str(self.lock_path())],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.release_lock, holder)
        self.assertEqual(holder.stdout.readline().strip(), 'locked')
        return holder

    def release_lock(self, holder):
        holder.stdin.close(); holder.wait(); holder.stdout.close()

    def test_dismiss_persists_and_the_finding_leaves_the_drafts(self):
        env = self.offline_env()  # a dismissal needs no network: this gh records any call and fails
        fp = self.fps()[0]
        out = self.run_issues(str(self.plan), '--dismiss', fp, '--reason', 'x', env=env)
        self.assertEqual(out, {'ok': True, 'dismissed': [fp], 'undismissed': [], 'warnings': []})
        self.assertFalse((self.dir / 'gh-called').exists())
        stored = json.loads(self.store_path().read_text())
        self.assertEqual(list(stored), [fp])
        self.assertEqual(stored[fp]['reason'], 'x')
        self.assertIsInstance(stored[fp]['at'], int)
        listed = self.run_issues(str(self.plan), '--offline')
        self.assertNotIn(fp, self.draft_fps(listed))
        marks = {f['fp']: f['dismissed'] for f in listed['findings']}
        self.assertEqual(marks.pop(fp), {'at': stored[fp]['at'], 'reason': 'x'})
        self.assertEqual(list(marks.values()), [None])
        self.assertEqual(listed['summary']['dismissed'], 1)
        # --include-dismissed drafts it again, and it stays marked
        everything = self.run_issues(str(self.plan), '--offline', '--include-dismissed')
        self.assertIn(fp, self.draft_fps(everything))
        self.assertEqual({f['fp']: f['dismissed'] is not None for f in everything['findings']}[fp], True)
        # filing leaves it out too
        applied = self.run_issues(str(self.plan), '--apply')
        self.assertEqual(len([c for c in applied['created'] if 'url' in c]), 1)
        self.assertEqual(len(self.created()), 1)
        # --undismiss restores it
        out = self.run_issues(str(self.plan), '--undismiss', fp)
        self.assertEqual(out, {'ok': True, 'dismissed': [], 'undismissed': [fp], 'warnings': []})
        self.assertEqual(json.loads(self.store_path().read_text()), {})
        restored = self.run_issues(str(self.plan), '--offline')
        self.assertIn(fp, self.draft_fps(restored))
        self.assertEqual([f['dismissed'] for f in restored['findings']], [None, None])

    def test_dismiss_takes_repeated_comma_separated_and_unknown_fingerprints(self):
        a, b = self.fps()
        ghost = '0' * 12
        out = self.run_issues(str(self.plan), '--dismiss', f'{a},{ghost}', '--dismiss', b, '--dismiss', a)
        self.assertEqual(out['dismissed'], [a, ghost, b])
        self.assertEqual(sorted(json.loads(self.store_path().read_text())), sorted([a, b, ghost]))
        out = self.run_issues(str(self.plan), '--undismiss', ghost, '--undismiss', a)
        self.assertEqual(out, {'ok': True, 'dismissed': [], 'undismissed': [ghost, a], 'warnings': []})
        self.assertEqual(list(json.loads(self.store_path().read_text())), [b])
        # dismissing again without a reason keeps the earlier one; with a reason it replaces it
        self.run_issues(str(self.plan), '--dismiss', b, '--reason', 'first')
        self.run_issues(str(self.plan), '--dismiss', b)
        self.assertEqual(json.loads(self.store_path().read_text())[b]['reason'], 'first')
        self.run_issues(str(self.plan), '--dismiss', b, '--reason', 'x' * 500)
        self.assertEqual(json.loads(self.store_path().read_text())[b]['reason'], 'x' * 500)

    def test_dismiss_rejects_bad_input_before_touching_the_store(self):
        a = self.fps()[0]
        for bad in (['--dismiss', 'xyz'], ['--undismiss', 'A' * 12], ['--dismiss', f'{a},'],
                    ['--dismiss', a, '--reason', 'x' * 501], ['--undismiss', a, '--reason', 'x'],
                    ['--reason', 'x'], ['--dismiss', a, '--undismiss', a], ['--dismiss', a, '--apply']):
            self.run_issues(str(self.plan), *bad, code=2)
        self.assertFalse(self.findings_dir().exists())

    def test_the_store_belongs_to_the_repository_the_run_resolves_to(self):
        fp = self.fps()[0]
        # --repo changes the slug the issues go to, not the store the marks live in
        self.run_issues(str(self.plan), '--dismiss', fp, '--repo', 'other/name')
        # --all-runs: the repository itself, or a plan dir naming it
        self.run_issues(str(self.repo), '--all-runs', '--dismiss', fp)
        self.run_issues(str(self.plan), '--all-runs', '--dismiss', fp)
        self.assertEqual([p.name for p in self.findings_dir().glob('*.json')], [f'{repo_key(self.repo)}.json'])
        self.assertEqual(list(json.loads(self.store_path().read_text())), [fp])

    def test_an_empty_xdg_state_home_means_the_default(self):
        home = self.dir / 'home'; home.mkdir()
        env = dict(self.env, XDG_STATE_HOME='', HOME=str(home))
        fp = self.fps()[0]
        self.run_issues(str(self.plan), '--dismiss', fp, env=env)
        self.assertTrue((home / '.local/state/forge/findings' / f'{repo_key(self.repo)}.json').is_file())

    def test_a_symlinked_or_special_store_is_refused(self):
        fp = self.fps()[0]
        self.findings_dir().mkdir(parents=True)
        target = self.dir / 'elsewhere.json'; target.write_text('{}')
        store = self.store_path()
        store.symlink_to(target)
        for argv in (['--dismiss', fp], ['--undismiss', fp], ['--offline']):
            self.assertIn('regular file', self.run_issues(str(self.plan), *argv, code=2))
        self.assertEqual(target.read_text(), '{}')
        store.unlink(); store.mkdir()
        self.assertIn('regular file', self.run_issues(str(self.plan), '--dismiss', fp, code=2))
        store.rmdir()
        store.write_text('[]')
        self.assertIn('not a dismissal store', self.run_issues(str(self.plan), '--dismiss', fp, code=2))
        self.assertEqual(store.read_text(), '[]')
        # a symlinked lock file is refused too, before anything is looked up
        store.unlink(); self.lock_path().unlink()
        self.lock_path().symlink_to(target)
        self.assertIn('lock', self.run_issues(str(self.plan), '--apply', code=2))
        self.assertIn('lock', self.run_issues(str(self.plan), '--dismiss', fp, code=2))
        self.assertEqual(self.gh_state()['calls'], [])
        self.assertEqual(target.read_text(), '{}')
        # a symlinked directory is refused for the store and for the lock around a filing
        shutil.rmtree(self.findings_dir())
        real = self.dir / 'real'; real.mkdir()
        self.findings_dir().symlink_to(real)
        self.assertIn('not a symlink', self.run_issues(str(self.plan), '--dismiss', fp, code=2))
        self.assertIn('not a symlink', self.run_issues(str(self.plan), '--apply', code=2))
        self.assertEqual(list(real.iterdir()), [])
        self.assertEqual(self.created(), [])

    def test_a_busy_repository_lock_stops_apply_and_dismiss_with_exit_3(self):
        fp = self.fps()[0]
        holder = self.hold_lock()
        started = time.monotonic()
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), str(self.plan), *argv, '--json'],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)
                 for argv in (['--apply'], ['--dismiss', fp])]
        results = [p.communicate(timeout=60) for p in procs]
        elapsed = time.monotonic() - started
        for proc, (out, err) in zip(procs, results):
            self.assertEqual(proc.returncode, 3, err)
            self.assertEqual(out, '')
            self.assertEqual(err.strip(), 'forge issues: busy: another filing or dismissal for o/r is in progress')
        self.assertTrue(9 <= elapsed < 20, f'a busy lock is retried for about 10 s, took {elapsed:.1f}')
        # not even the lookup ran, and the dismissal was not stored
        self.assertEqual(self.gh_state()['calls'], [])
        self.assertFalse(self.store_path().exists())
        # listing takes no lock
        self.assertEqual(self.forge_issues(str(self.plan))['summary']['drafts'], 2)
        self.release_lock(holder)
        self.run_issues(str(self.plan), '--dismiss', fp)
        self.assertEqual(len([c for c in self.run_issues(str(self.plan), '--apply')['created'] if 'url' in c]), 1)

    def test_concurrent_apply_files_the_finding_once(self):
        bin_dir = self.dir / 'racebin'; bin_dir.mkdir()
        (bin_dir / 'gh').write_text(RACE_GH); (bin_dir / 'gh').chmod(0o755)
        state, log = self.dir / 'race.json', self.dir / 'race.log'
        state.write_text('[]')
        env = dict(self.env, PATH=f'{bin_dir}:{os.environ["PATH"]}', RACE_GH_STATE=str(state), RACE_GH_LOG=str(log))
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), str(self.plan), '--min-severity', 'P2', '--apply',
                                   '--json'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for _ in range(2)]
        results = [p.communicate(timeout=60) for p in procs]
        for proc, (_, err) in zip(procs, results):
            self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(log.read_text().split('\n').count('issue create'), 1)
        self.assertEqual(len(json.loads(state.read_text())), 1)
        outs = [json.loads(out) for out, _ in results]
        # one process created it; the other's lookup, made after the first one's create, found it
        self.assertEqual(sorted((o['summary']['already_filed'], len([c for c in o['created'] if 'url' in c]))
                                for o in outs), [(0, 1), (1, 0)])

    def issues_mod(self):
        spec = importlib.util.spec_from_file_location('forge_issues_under_test', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def set_merged(self, task, when):
        os.utime(self.plan / 'tasks' / task / 'merged', (when, when))

    def test_review_outdated_follows_the_final_review_rule(self):
        # Same cases as forge-ui's FinalReviewSummary: a known integration commit wins, a review with no
        # integration commit is outdated when its tip moved, an older integrate review only when a task
        # merged after it, and a merge inside the review's starting second does not count.
        mod = self.issues_mod()
        at = 1_700_000_000
        sha, other = 'a' * 40, 'b' * 40
        sha256 = 'c' * 64
        review = self.review_dir(self.plan, f'final-review-{at}-1', ['- [P1][CONFIRMED] src/a.ts:1 — Broken thing.'])
        (review / 'verdict').write_text('FAIL\n')
        (review / 'review.meta').write_text(f'tip={sha}\n')
        (self.plan / 'accepted.integration').write_text(sha + '\n')
        self.set_merged('import', at + 0.5)  # inside the starting second
        self.set_merged('ui', at)
        self.assertEqual(mod.review_outdated(self.plan, review), (False, []))
        self.set_merged('import', at + 5)
        # tip still matches the accepted integration: a later merge does not outdate it, and merged_since stays empty
        self.assertEqual(mod.review_outdated(self.plan, review), (False, []))
        (review / 'review.meta').write_text(f'tip={other}\n')
        self.assertEqual(mod.review_outdated(self.plan, review), (True, ['import']))
        self.set_merged('import', at + 0.5)
        self.assertEqual(mod.review_outdated(self.plan, review), (True, []))  # tip moved, but that merge does not count
        self.set_merged('import', at + 5)
        (review / 'review.meta').write_text(f'tip={other}\nintegration={sha}\n')
        self.assertEqual(mod.review_outdated(self.plan, review), (False, []))
        (review / 'review.meta').write_text(f'tip={sha}\nintegration={other}\n')
        self.assertEqual(mod.review_outdated(self.plan, review), (True, ['import']))
        (review / 'review.meta').write_text(f'tip={sha}\nintegration=not-a-commit\n')
        self.assertEqual(mod.review_outdated(self.plan, review), (False, []))  # falls through to the matching tip
        (review / 'verdict').write_text('')
        self.assertEqual(mod.review_outdated(self.plan, review), (None, []))
        (review / 'verdict').write_text('FAIL\n')
        (self.plan / 'accepted.integration').unlink()
        self.assertEqual(mod.review_outdated(self.plan, review), (None, []))

        (self.plan / 'accepted.integration').write_text(sha + '\n')
        integ = self.plan / f'integrate-{at}-2'
        integ.mkdir()
        ireview = self.review_dir(integ, 'final-review', ['- [P0][CONFIRMED] src/b.ts:2 — Integrate thing.'])
        (ireview / 'verdict').write_text('FAIL\n')
        (ireview / 'review.meta').write_text(f'tip={other}\n')  # the candidate tip, not the integration tip
        self.set_merged('import', at + 0.5)
        self.assertEqual(mod.review_outdated(self.plan, ireview), (False, []))
        self.assertEqual(mod.review_stamp(self.plan, ireview)['name'], f'integrate-{at}-2/final-review')
        self.set_merged('import', at + 1)  # the first instant that counts
        self.assertEqual(mod.review_outdated(self.plan, ireview), (True, ['import']))
        (ireview / 'review.meta').write_text(f'tip={other}\nintegration={sha256}\n')
        (self.plan / 'accepted.integration').write_text(sha256 + '\n')
        self.assertEqual(mod.review_outdated(self.plan, ireview), (False, []))
        self.assertEqual(mod.outdated_line('en', {'outdated': True, 'merged_since': ['a', 'b', 'c', 'd', 'e', 'f']}),
                         'The review is outdated: 6 tasks merged into the integration branch after it '
                         '(a, b, c, d, e, +1 more), so check whether they fixed it first.')
        self.assertEqual(mod.outdated_line('it', {'outdated': True, 'merged_since': ['import']}),
                         'La review è obsoleta: 1 task è stato integrato nel branch di integrazione dopo di essa '
                         '(import), quindi verifica prima se l\'hanno già corretta.')
        self.assertEqual(mod.outdated_line('en', {'outdated': False, 'merged_since': ['import']}), '')
        self.assertEqual(mod.fingerprint('o/r', {'file': 'src/a.ts', 'text': 'Broken thing.'}), 'aa3c78624139')

    def test_outdated_review_is_the_same_on_every_input_path(self):
        at = 1_700_000_000
        sha, other = 'a' * 40, 'b' * 40
        name = f'final-review-{at}-9'
        line = '- [P1][CONFIRMED] src/a.ts:1 — Broken thing.'
        review = self.review_dir(self.plan, name, [line])
        (review / 'verdict').write_text('FAIL\n')
        (review / 'review.meta').write_text(f'tip={other}\n')
        (self.plan / 'accepted.integration').write_text(sha + '\n')
        self.set_merged('import', at + 5)
        self.set_merged('ui', at)  # inside the starting second: present, and not "since"
        os.symlink(name, self.plan / 'final-review')
        sentence = ('The review is outdated: 1 task merged into the integration branch after it (import), '
                    'so check whether they fixed it first.')
        context = ('Forge whole-run review finding left open: check whether it was already fixed before working on it.')
        stamp = {'name': name, 'outdated': True, 'merged_since': ['import']}

        def review_row(out):
            rows = [f for f in out['findings'] if f['origin'] == 'review']
            self.assertEqual(len(rows), 1, out['findings'])
            return rows[0]

        paths = [str(self.plan), str(self.plan / 'final-review'), str(self.plan / 'final-review' / 'qa.last'),
                 str(self.plan / name), str(self.plan / name / 'qa.last')]
        outs = [self.run_issues(path, '--offline', '--repo', 'o/r') for path in paths]
        first = review_row(outs[0])
        self.assertEqual(first['review'], stamp)
        self.assertEqual(first['fp'], 'aa3c78624139')
        body = next(d['body'] for d in outs[0]['drafts'] if d['origin'] == 'review')
        self.assertIn(context + '\n' + sentence, body)
        self.assertIn('import', body)
        for path, out in zip(paths, outs):
            row = review_row(out)
            self.assertEqual(row['review'], stamp, path)
            self.assertEqual(row['fp'], first['fp'], path)
            self.assertEqual(next(d['body'] for d in out['drafts'] if d['origin'] == 'review'), body, path)
        self.assertEqual(outs[0]['summary']['review'], 1)
        res = subprocess.run([sys.executable, str(SCRIPT), str(self.plan), '--offline', '--repo', 'o/r'],
                             capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        marked = [row for row in res.stdout.splitlines() if 'Broken thing' in row]
        self.assertTrue(marked and all('(outdated review)' in row for row in marked), res.stdout)
        self.assertTrue(all('(outdated review)' not in row for row in res.stdout.splitlines() if 'Company creation' in row))

        # A current review: integration= matches, so a later merge does not change the issue text.
        (review / 'review.meta').write_text(f'tip={other}\nintegration={sha}\n')
        current = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r')
        row = review_row(current)
        self.assertEqual(row['review'], {'name': name, 'outdated': False, 'merged_since': []})
        self.assertEqual(row['fp'], 'aa3c78624139')
        current_body = next(d['body'] for d in current['drafts'] if d['origin'] == 'review')
        self.assertIn(context + '\n\nOrigin:', current_body)
        self.assertNotIn('The review is outdated', current_body)
        self.assertNotIn('merged into the integration branch', current_body)
        res = subprocess.run([sys.executable, str(SCRIPT), str(self.plan), '--offline', '--repo', 'o/r'],
                             capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn('(outdated review)', res.stdout)

        # Italian sentence, and an integrate review reaches the same stamp from the plan, the dir and qa.last.
        (review / 'review.meta').write_text(f'tip={other}\n')
        italian = self.run_issues(str(self.plan / name), '--offline', '--repo', 'o/r', '--lang', 'it')
        ibody = italian['drafts'][0]['body']
        self.assertIn('1 task è stato integrato nel branch di integrazione dopo di essa (import)', ibody)
        self.assertIn('quindi verifica prima se l\'hanno già corretta', ibody)

    def test_integrate_review_outdated_agrees_across_inputs(self):
        at = 1_700_000_000
        sha, other = 'a' * 40, 'b' * 40
        integ = self.plan / f'integrate-{at}-3'
        integ.mkdir()
        ireview = self.review_dir(integ, 'final-review', ['- [P0][CONFIRMED] src/b.ts:2 — Integrate thing.'])
        (ireview / 'verdict').write_text('FAIL\n')
        (ireview / 'review.meta').write_text(f'tip={other}\n')
        (self.plan / 'accepted.integration').write_text(sha + '\n')
        self.set_merged('import', at + 2)
        self.set_merged('ui', at)
        os.symlink(f'integrate-{at}-3/final-review', self.plan / 'final-review')
        stamp = {'name': f'integrate-{at}-3/final-review', 'outdated': True, 'merged_since': ['import']}
        paths = [str(self.plan), str(self.plan / 'final-review'), str(ireview / 'qa.last')]
        bodies, fps = [], []
        for path in paths:
            out = self.run_issues(path, '--offline', '--repo', 'o/r')
            row = next(f for f in out['findings'] if f['origin'] == 'review')
            self.assertEqual(row['review'], stamp, path)
            bodies.append(next(d['body'] for d in out['drafts'] if d['origin'] == 'review'))
            fps.append(row['fp'])
        self.assertEqual(len(set(bodies)), 1)
        self.assertEqual(len(set(fps)), 1)
        self.assertIn('import', bodies[0])
        self.assertIn('The review is outdated', bodies[0])

    def test_accepted_findings_are_not_described_as_tolerated(self):
        reason = 'edge case, tracked in the backlog'
        tolerated = ('- [P2][CONFIRMED] src/import.ts:645 — Company creation runs before the receipt replay. '
                     'A retry creates an extra company.')
        accepted_p0 = '- [P0][CONFIRMED] src/a.ts:10 — First accepted bug stays open.'
        accepted_p1 = '- [P1][CONFIRMED] src/b.ts:4 — Second accepted bug stays open.'
        text = ('## Known issues (QA findings below blocking threshold P1)\n\n' + tolerated + '\n\n'
                "## Accepted by a human despite the reviewer's FAIL verdict\n\n"
                f'Reason: {reason}\n\n' + accepted_p0 + '\n' + accepted_p1 + '\n')
        task = self.plan / 'tasks' / 'import'
        (task / 'known_issues.md').write_text(text)
        (task / 'accepted.by').write_text(f'status=FAIL\nreason={reason}\nby=ada\nat=2026-01-01T00:00:00+0000\n')
        sentence = ("A person accepted the work over the reviewer's FAIL verdict, so it was merged without fixing it. "
                    f'Reason: {reason}')
        paths = [str(self.plan), str(task), str(task / 'known_issues.md')]
        listed = []
        for path in paths:
            out = self.run_issues(path, '--offline', '--repo', 'o/r')
            by_origin = {}
            for finding in out['findings']:
                by_origin.setdefault(finding['origin'], []).append(finding)
            self.assertEqual(sorted(f['file'] for f in by_origin['tolerated']), ['src/import.ts'], path)
            self.assertEqual(sorted(f['file'] for f in by_origin['accepted']), ['src/a.ts', 'src/b.ts'], path)
            self.assertTrue(all(f['review'] is None for f in out['findings']), path)
            accepted = {f['file']: f for f in by_origin['accepted']}
            self.assertEqual(accepted['src/a.ts']['fp'], 'febf4ed2d38f', path)
            self.assertEqual(accepted['src/b.ts']['fp'], '94c4784087cc', path)
            self.assertEqual(by_origin['tolerated'][0]['fp'], 'cf5d9440c970', path)
            for draft in out['drafts']:
                if draft['origin'] == 'accepted':
                    self.assertIn(sentence, draft['body'], path)
                    self.assertNotIn('below the blocking threshold', draft['body'], path)
                    self.assertNotIn('soglia bloccante', draft['body'], path)
                if draft['origin'] == 'tolerated':
                    self.assertIn('below the blocking threshold P1', draft['body'], path)
            listed.append(out)
        self.assertEqual([f['fp'] for f in listed[0]['findings'] if f['origin'] == 'accepted'],
                         [f['fp'] for f in listed[1]['findings'] if f['origin'] == 'accepted'])
        self.assertEqual(listed[0]['summary']['accepted'], 2)
        self.assertEqual(listed[0]['summary']['tolerated'], 1)

        grouped = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r', '--group', 'task')
        titles = [d['title'] for d in grouped['drafts']]
        self.assertIn('[P0] Accepted forge findings: import', titles)
        self.assertNotIn('Non-blocking forge findings: import', titles)
        grouped_body = next(d['body'] for d in grouped['drafts'] if d['origin'] == 'accepted')
        self.assertIn(sentence, grouped_body)
        self.assertNotIn('below the blocking threshold', grouped_body)
        italian = self.run_issues(str(task), '--offline', '--repo', 'o/r', '--lang', 'it', '--group', 'task')
        self.assertIn('[P0] Segnalazioni forge accettate: import', [d['title'] for d in italian['drafts']])
        ibody = next(d['body'] for d in italian['drafts'] if d['origin'] == 'accepted')
        self.assertIn("il verdetto FAIL del revisore", ibody)
        self.assertIn(f'Motivo: {reason}', ibody)
        self.assertNotIn('soglia bloccante', ibody)

        long_reason = 'R' * 600
        (task / 'accepted.by').write_text(f'status=FAIL\nreason={long_reason}\n')
        cut = self.run_issues(str(task), '--offline', '--repo', 'o/r')
        cut_body = next(d['body'] for d in cut['drafts'] if 'First accepted' in d['body'])
        self.assertIn('Reason: ' + 'R' * 500, cut_body)
        self.assertNotIn('R' * 501, cut_body)
        self.assertEqual(next(f['fp'] for f in cut['findings'] if f['file'] == 'src/a.ts'), 'febf4ed2d38f')

        (task / 'accepted.by').write_text('User: accepted the tradeoff.\n')
        free = self.run_issues(str(task / 'known_issues.md'), '--offline', '--repo', 'o/r')
        free_body = next(d['body'] for d in free['drafts'] if d['origin'] == 'accepted')
        self.assertIn("reviewer's FAIL verdict", free_body)
        self.assertIn('Reason: User: accepted the tradeoff.', free_body)
        self.assertNotIn('below the blocking threshold', free_body)

    def tolerated_task(self, name, known, threshold=None):
        task = self.plan / 'tasks' / name
        task.mkdir()
        (task / 'status').write_text('PASS\n')
        (task / 'merged').write_text('abc123\n')
        if threshold:
            (task / 'qa_threshold').write_text(threshold + '\n')
        (task / 'known_issues.md').write_text(known)
        return task

    def test_every_tolerated_form_is_filed_and_canonical_keeps_its_fingerprint(self):
        canonical = '- [P2][CONFIRMED] src/import.ts:645 — Company creation runs before the receipt replay. A retry creates an extra company.'
        self.tolerated_task('forms', '## Known issues\n\n' + canonical + '\n\n* (P3) CONFIRMED a.py:7 — x\n')
        out = self.run_issues(str(self.plan / 'tasks' / 'forms'), '--offline', '--repo', 'o/r')
        by_file = {f['file']: f for f in out['findings']}
        self.assertEqual(sorted(by_file), ['a.py', 'src/import.ts'])
        self.assertEqual((by_file['a.py']['severity'], by_file['a.py']['text']), ('P3', 'x'))
        self.assertEqual(by_file['src/import.ts']['fp'], 'cf5d9440c970')

    def test_nested_plausible_bullet_does_not_replace_its_confirmed_parent(self):
        self.tolerated_task('nested', '## Known issues\n\n* (P3) CONFIRMED a.py:7 — Parent defect.\n'
                            '  - [P3][PLAUSIBLE] b.py:9 — Related suspicion.\n')
        out = self.run_issues(str(self.plan / 'tasks' / 'nested'), '--offline', '--repo', 'o/r')
        self.assertEqual([(f['file'], f['text']) for f in out['findings']], [('a.py', 'Parent defect.')])

    def test_threshold_is_the_effective_one(self):
        known = '## Known issues\n\n- [P3][CONFIRMED] a.py:1 — x\n'
        self.tolerated_task('plain', known)
        self.tolerated_task('none', known, 'none')
        self.tolerated_task('own', known, 'P0')
        out = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r', '--group', 'task')
        bodies = {d['title']: d['body'] for d in out['drafts']}
        joined = '\n'.join(bodies.values())
        self.assertNotIn('threshold ?', joined)
        for name, expect in (('plain', 'P2'), ('none', 'P3'), ('own', 'P0')):
            found = self.run_issues(str(self.plan / 'tasks' / name), '--offline', '--repo', 'o/r')
            self.assertIn(f'below the blocking threshold {expect}', found['drafts'][0]['body'], name)

    def test_flip_heading_splits_tolerated_and_accepted(self):
        task = self.tolerated_task('flip',
            '## Known issues\n\n- [P3][CONFIRMED] src/t.ts:1 — Tolerated one.\n\n'
            "## Accepted by a human despite forge's FAIL (the reviewer said PASS, but listed findings at or above P2)\n\n"
            '- [P1][CONFIRMED] src/a.ts:2 — Accepted one.\n')
        (task / 'accepted.by').write_text('status=FAIL\nreason=ok\nby=ada\n')
        out = self.run_issues(str(task), '--offline', '--repo', 'o/r')
        self.assertEqual({f['file']: f['origin'] for f in out['findings']},
                         {'src/t.ts': 'tolerated', 'src/a.ts': 'accepted'})

    def test_solo_run_dir_lists_known_issues(self):
        solo = self.dir / 'solo'
        solo.mkdir()
        (solo / 'known_issues.md').write_text('## Known issues\n\n* (P3) CONFIRMED a.py:7 — x\n')
        out = self.run_issues(str(solo), '--offline', '--repo', 'o/r')
        self.assertEqual([(f['file'], f['origin']) for f in out['findings']], [('a.py', 'tolerated')])
        self.assertIn('below the blocking threshold P2', out['drafts'][0]['body'])

    def accept_task(self, name, status, reason, findings):
        task = self.plan / 'tasks' / name
        task.mkdir()
        (task / 'status').write_text('PASS\n')
        (task / 'merged').write_text('abc123\n')
        (task / 'qa_threshold').write_text('P1\n')
        (task / 'known_issues.md').write_text(
            f"## Accepted by a human despite the reviewer's {status} verdict\n\n" + '\n'.join(findings) + '\n')
        (task / 'accepted.by').write_text(f'status={status}\nreason={reason}\nby=ada\n')
        return task

    def test_grouped_run_keeps_every_acceptance(self):
        # alpha and beta share one --group run issue. The body used to keep only the first
        # finding's verdict and reason; beta's acceptance has to be in the same issue.
        reason_a, reason_b = 'reason-alpha', 'reason-beta'
        self.accept_task('alpha', 'FAIL', reason_a, [
            '- [P2][CONFIRMED] src/alpha.ts:1 — Alpha stays open.',
            '- [P2][CONFIRMED] src/alpha2.ts:2 — Alpha second stays open.'])
        self.accept_task('beta', 'UNKNOWN', reason_b, [
            '- [P2][CONFIRMED] src/beta.ts:3 — Beta stays open.'])
        grouped = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r', '--group', 'run')
        body = next(d['body'] for d in grouped['drafts'] if d['origin'] == 'accepted')
        self.assertIn('[P2] Accepted forge findings: plan', [d['title'] for d in grouped['drafts']])
        fail = ("A person accepted the work over the reviewer's FAIL verdict, so it was merged without fixing it. "
                f'Reason: {reason_a}')
        unknown = ("A person accepted the work over the reviewer's UNKNOWN verdict, so it was merged without fixing it. "
                   f'Reason: {reason_b}')
        self.assertIn(fail, body)
        self.assertIn(unknown, body)
        self.assertEqual(body.count(fail), 1)  # alpha's two findings share one acceptance
        self.assertNotIn('below the blocking threshold', body)
        self.assertLess(body.index('Forge review findings left open'), body.index(fail))
        self.assertLess(body.index('Forge review findings left open'), body.index(unknown))
        italian = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r', '--group', 'run', '--lang', 'it')
        ibody = next(d['body'] for d in italian['drafts'] if d['origin'] == 'accepted')
        self.assertIn('il verdetto FAIL del revisore', ibody)
        self.assertIn(f'Motivo: {reason_a}', ibody)
        self.assertIn('il verdetto UNKNOWN del revisore', ibody)
        self.assertIn(f'Motivo: {reason_b}', ibody)
        self.assertNotIn('soglia bloccante', ibody)

    def test_grouped_plausible_keeps_an_outdated_review_warning(self):
        # A task's P2 PLAUSIBLE sorts ahead of the outdated review's P3 PLAUSIBLE. --group run
        # puts them in one issue; the warning and the merged ids still have to be in it.
        at = 1_700_000_000
        sha, other = 'a' * 40, 'b' * 40
        task = self.plan / 'tasks' / 'import'
        (task / 'qa.last').write_text((task / 'qa.last').read_text()
                                      + '- [P2][PLAUSIBLE] src/lead.ts:2 — Lead names may drop a character.\n')
        name = f'final-review-{at}-4'
        review = self.review_dir(self.plan, name, ['- [P3][PLAUSIBLE] src/rev.ts:3 — Review maybe.'])
        (review / 'verdict').write_text('FAIL\n')
        (review / 'review.meta').write_text(f'tip={other}\n')
        (self.plan / 'accepted.integration').write_text(sha + '\n')
        self.set_merged('import', at + 5)
        self.set_merged('ui', at)
        os.symlink(name, self.plan / 'final-review')
        sentence = ('The review is outdated: 1 task merged into the integration branch after it (import), '
                    'so check whether they fixed it first.')
        out = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r', '--include-plausible', '--group', 'run')
        drafts = [d for d in out['drafts'] if d['origin'] == 'plausible']
        self.assertEqual(len(drafts), 1, [d['title'] for d in out['drafts']])
        body = drafts[0]['body']
        self.assertIn('Lead names may drop a character', body)
        self.assertIn('Review maybe', body)
        self.assertIn(sentence, body)
        self.assertLess(body.index('Forge review findings left open'), body.index(sentence))
        italian = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r', '--include-plausible',
                                  '--group', 'run', '--lang', 'it')
        ibody = next(d['body'] for d in italian['drafts'] if d['origin'] == 'plausible')
        self.assertIn('1 task è stato integrato nel branch di integrazione dopo di essa (import)', ibody)

        # A current review adds nothing: the same group, without the warning.
        (review / 'review.meta').write_text(f'tip={other}\nintegration={sha}\n')
        current = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r', '--include-plausible', '--group', 'run')
        current_body = next(d['body'] for d in current['drafts'] if d['origin'] == 'plausible')
        self.assertNotIn('The review is outdated', current_body)
        self.assertNotIn('merged into the integration branch', current_body)

    def test_listed_underscore_task_outdates_an_older_integrate_review(self):
        # The UI counts a tasks.tsv id that starts with `_`. tasks/_integration is forge's own
        # directory and is not a task unless a row lists it, so its marker must not count.
        at = 1_700_000_000
        sha = 'a' * 40
        listed = (self.plan / 'tasks.tsv').read_text()
        (self.plan / 'tasks.tsv').write_text(
            listed + '_fix\t-\tlow\tsrc\tsol:high:codex\tastra:xhigh:codex\tFix\n')
        fix = self.plan / 'tasks' / '_fix'
        fix.mkdir()
        (fix / 'merged').write_text('abc123\n')
        own = self.plan / 'tasks' / '_integration'
        own.mkdir()
        (own / 'merged').write_text('abc123\n')
        os.utime(own / 'merged', (at + 50, at + 50))
        self.set_merged('import', at)       # inside the review's starting second
        self.set_merged('ui', at + 0.4)
        self.set_merged('_fix', at + 0.2)   # same second: not yet a later merge
        integ = self.plan / f'integrate-{at}-1'
        integ.mkdir()
        ireview = self.review_dir(integ, 'final-review', ['- [P1][CONFIRMED] src/a.ts:1 — Broken thing.'])
        (ireview / 'verdict').write_text('FAIL\n')
        (ireview / 'review.meta').write_text(f'tip={sha}\n')  # no integration= commit
        (self.plan / 'accepted.integration').write_text(sha + '\n')
        mod = self.issues_mod()
        self.assertEqual(mod.review_outdated(self.plan, ireview), (False, []))
        self.set_merged('_fix', at + 2)
        self.assertEqual(mod.review_outdated(self.plan, ireview), (True, ['_fix']))
        os.symlink(f'integrate-{at}-1/final-review', self.plan / 'final-review')
        out = self.run_issues(str(self.plan), '--offline', '--repo', 'o/r')
        row = next(f for f in out['findings'] if f['origin'] == 'review')
        self.assertEqual(row['review'], {'name': f'integrate-{at}-1/final-review',
                                         'outdated': True, 'merged_since': ['_fix']})
        body = next(d['body'] for d in out['drafts'] if d['origin'] == 'review')
        self.assertIn('(_fix)', body)
        self.assertNotIn('_integration', body)


if __name__ == '__main__':
    unittest.main()
