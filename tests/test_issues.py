"""`forge issues`: leftover review findings become GitHub issues through plain `gh` (a fake one here)."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
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
        self.env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}', FAKE_GH_STATE=str(self.state))
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

    def run_issues(self, *args, env=None, code=0):
        res = subprocess.run([sys.executable, str(SCRIPT), *args, '--json'], capture_output=True, text=True,
                             env=env or self.env, timeout=60)
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
                                               'file', 'summary', 'text', 'reviewer', 'commit', 'filed']))
        self.assertEqual((first['origin'], first['plan'], first['task'], first['filed']),
                         ('tolerated', 'plan', 'import', None))
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


if __name__ == '__main__':
    unittest.main()
