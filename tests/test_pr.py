import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_registry  # noqa: E402

FAKE_GH = '''#!/bin/sh
# records how it was called, copies the --body-file it was given, and answers like gh
printf '%s\\n' "$@" > "$GH_LOG"
pwd > "$GH_LOG.cwd"
while [ $# -gt 0 ]; do [ "$1" = --body-file ] && cp "$2" "$GH_LOG.body"; shift; done
echo https://github.com/o/r/pull/9
exit "${GH_EXIT:-0}"
'''


class PrTests(unittest.TestCase):
    """`forge pr`: a PR description from a finished run. Never calls the real gh or the network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / 'repo'
        self.git('init', '-q', str(self.repo))
        self.bin = self.base / 'bin'; self.bin.mkdir()
        (self.bin / 'gh').write_text(FAKE_GH); (self.bin / 'gh').chmod(0o755)
        self.gh_log = self.base / 'gh-args'
        self.env = dict(os.environ, PATH='%s:%s' % (self.bin, os.environ['PATH']), GH_LOG=str(self.gh_log),
                        XDG_CONFIG_HOME=str(self.base / 'config'))
        self.run = self.make_run('r1')

    def git(self, *args, cwd=None):
        return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *(['-C', str(cwd)] if cwd else []), *args],
                              check=True, capture_output=True, text=True).stdout

    def make_run(self, name, goal=None, age=0, **statuses):
        """A finished plan run with tasks t1, t2, a final review and a verification, written like forge writes them."""
        run = self.base / name
        run.mkdir()
        files = {'run_id': name, 'repo': str(self.repo), 'goal.txt': goal or 'Add retries to %s/api' % self.repo,
                 'request.md': 'SECRET-REQUEST: the customer is Acme\n', 'deferrals.txt': 'SECRET-DEFER: rename later\n',
                 'verification.status': 'PASS', 'integration.base': 'abc1234567890abcdef\n',
                 'tasks.tsv': 't1\t.\tlow\t.\tsol\topus\tAdd retry helper\nt2\tt1\thigh\t.\tsol\topus\tWire it | in\n'}
        for file, text in files.items():
            (run / file).write_text(text if text.endswith('\n') else text + '\n')
        for ident, status in (('t1', statuses.get('t1', 'MERGED')), ('t2', statuses.get('t2', 'FAIL'))):
            (run / 'tasks' / ident).mkdir(parents=True)
            (run / 'tasks' / ident / 'status').write_text(status + '\n')
        review = run / 'final-review-1-1'; review.mkdir()
        (review / 'verdict').write_text('PASS\n'); (review / 'qa.input').write_text('x')
        (review / 'qa.last').write_text('- [P2][CONFIRMED] src/b.ts:2 — Smaller thing.\n- [P1][CONFIRMED] src/a.ts:1 — Broken thing in %s/x.\n'
                                        'SECRET-BODY: the private text of the finding\n' % Path.home())
        for path in [run, *run.rglob('*')]:
            os.utime(path, (1_800_000_000 + age, 1_800_000_000 + age), follow_symlinks=False)
        return run

    def forge_pr(self, *args, code=0, env=None, cwd=None):
        done = subprocess.run([str(ROOT / 'forge'), 'pr', *args], cwd=cwd or self.repo, env=env or self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        return done.stdout, done.stderr

    def test_the_body_describes_the_run_and_leaves_private_text_out(self):
        out, err = self.forge_pr(str(self.run))
        self.assertIn('Add retries to repo/api', out)                      # the goal, its path cut to a name
        self.assertIn('Built by forge run `r1`: 1 of 2 tasks merged.', out)
        self.assertIn('| t1 | Add retry helper | low | MERGED | sol | opus |', out)
        self.assertIn('| t2 | Wire it \\| in | high | FAIL | sol | opus |', out)
        self.assertIn('- `t2` Wire it \\| in: FAIL', out)                  # what did not merge
        self.assertIn('- Verification: PASS', out); self.assertIn('- Final review: PASS', out)
        self.assertLess(out.index('**P1**'), out.index('**P2**'))           # worst first
        self.assertIn('- **P1** `src/a.ts:1` Broken thing in ~/x.', out)    # headline only, home as ~
        self.assertIn('- Integration branch: `forge/r1-integration`', out); self.assertIn('abc123456789', out)
        for private in ('SECRET-REQUEST', 'SECRET-DEFER', 'SECRET-BODY', str(self.repo), str(self.run), str(Path.home())):
            self.assertNotIn(private, out)
        self.assertIn('suggested title: Add retries to repo/api', err)
        self.assertFalse(self.gh_log.exists(), 'printing must not call gh')

    def test_texts_and_paths_are_opt_in(self):
        out, _ = self.forge_pr(str(self.run), '--texts')
        self.assertIn('SECRET-REQUEST: the customer is Acme', out); self.assertIn('- SECRET-DEFER: rename later', out)
        self.assertNotIn('SECRET-BODY', out)                                # a finding's text is never included
        out, _ = self.forge_pr(str(self.run), '--paths')
        self.assertIn('Add retries to %s/api' % self.repo, out); self.assertNotIn('SECRET-REQUEST', out)
        out, _ = self.forge_pr(str(self.run), '--texts')
        (self.run / 'request.md').write_text('```\ncode\n```\n')
        out, _ = self.forge_pr(str(self.run), '--texts')
        self.assertIn('````text\n```\ncode\n```\n````', out)                # a request with fences cannot close ours

    def test_a_final_review_the_branch_has_moved_past_is_marked_outdated(self):
        (self.run / 'accepted.integration').write_text('b' * 40 + '\n')
        (self.run / 'final-review-1-1' / 'review.meta').write_text('tip=%s\n' % ('a' * 40))
        self.assertIn('- Final review: PASS (outdated: the integration branch has moved since)', self.forge_pr(str(self.run))[0])
        (self.run / 'final-review-1-1' / 'review.meta').write_text('tip=%s\n' % ('b' * 40))
        self.assertIn('- Final review: PASS\n', self.forge_pr(str(self.run))[0])

    def test_out_writes_the_body_to_a_file(self):
        out, _ = self.forge_pr(str(self.run), '--out', str(self.base / 'pr.md'))
        self.assertEqual((self.base / 'pr.md').read_text(), out)

    def test_a_run_that_is_still_going_or_is_not_a_plan_is_refused(self):
        going = self.make_run('r2', t2='RUNNING')
        _, err = self.forge_pr(str(going), code=2)
        self.assertIn('still running (running tasks)', err)
        solo = self.base / 'solo'; solo.mkdir()
        (solo / 'prompt.md').write_text('do it\n'); (solo / 'repo').write_text(str(self.repo) + '\n')
        self.assertIn('solo run', self.forge_pr(str(solo), code=2)[1])
        self.assertIn('not a forge run', self.forge_pr(str(self.base), code=2)[1])
        self.assertIn('RUN_DIR cannot be combined', self.forge_pr(str(self.run), '--repo', str(self.repo), code=2)[1])
        self.assertIn('used by --create only', self.forge_pr(str(self.run), '--base', 'main', code=2)[1])
        self.assertFalse(self.gh_log.exists())

    def test_without_a_run_dir_it_takes_the_newest_finished_run_of_the_repo(self):
        older = self.make_run('r0', goal='Older work', age=-100)
        newer = self.make_run('r3', goal='Newest work', age=100)
        going = self.make_run('r4', goal='Still going', age=200, t2='RUNNING')
        _, err = self.forge_pr(code=2)
        self.assertIn('no finished run is registered', err)
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': self.env['XDG_CONFIG_HOME']}):
            for run in (older, newer, going):
                forge_registry.register_run_dir(self.repo, run)
        out, _ = self.forge_pr()
        self.assertIn('Newest work', out); self.assertNotIn('Still going', out)

    def pushed(self, branch='forge/r1-integration'):
        """The integration branch, with a remote and an upstream: what --create requires."""
        self.git('commit', '-q', '--allow-empty', '-m', 'x', cwd=self.repo)
        self.git('branch', branch, cwd=self.repo)
        remote = self.base / 'origin.git'
        self.git('init', '-q', '--bare', str(remote))
        self.git('remote', 'add', 'origin', str(remote), cwd=self.repo)
        self.git('push', '-q', '-u', 'origin', branch, cwd=self.repo)

    def test_create_opens_a_draft_with_gh_for_the_pushed_integration_branch(self):
        self.pushed()
        out, _ = self.forge_pr(str(self.run), '--create', '--base', 'dev', cwd=self.base)   # not run from the repo
        self.assertEqual(out.strip(), 'https://github.com/o/r/pull/9')
        args = self.gh_log.read_text().splitlines()
        self.assertEqual(args[:3], ['pr', 'create', '--draft'])
        self.assertEqual(args[args.index('--head') + 1], 'forge/r1-integration')
        self.assertEqual(args[args.index('--base') + 1], 'dev')
        self.assertEqual(args[args.index('--title') + 1], 'Add retries to repo/api')
        self.assertEqual(Path(str(self.gh_log) + '.cwd').read_text().strip(), str(self.repo))
        printed = self.forge_pr(str(self.run))[0]
        self.assertEqual(Path(str(self.gh_log) + '.body').read_text(), printed)   # the body that was printed
        self.assertFalse(Path(args[args.index('--body-file') + 1]).exists(), 'the temporary body is removed')

    def test_create_refuses_an_unpushed_branch_and_never_pushes(self):
        self.git('commit', '-q', '--allow-empty', '-m', 'x', cwd=self.repo)
        self.git('branch', 'forge/r1-integration', cwd=self.repo)
        _, err = self.forge_pr(str(self.run), '--create', code=2)
        self.assertIn('has not been pushed', err); self.assertIn('push -u origin forge/r1-integration', err)
        self.assertFalse(self.gh_log.exists())
        self.assertEqual(self.git('remote', cwd=self.repo), '')            # still no remote: nothing was added or pushed
        _, err = self.forge_pr(str(self.run), '--create', '--head', 'nonesuch', code=2)
        self.assertIn('no branch nonesuch', err); self.assertFalse(self.gh_log.exists())

    def test_create_reports_a_failing_gh_and_a_missing_one(self):
        self.pushed()
        self.forge_pr(str(self.run), '--create', code=1, env=dict(self.env, GH_EXIT='1'))
        tools = self.base / 'tools'; tools.mkdir()
        for name in ('git', 'python3', 'bash', 'env', 'dirname', 'readlink'):
            found = subprocess.run(['which', name], capture_output=True, text=True).stdout.strip()
            if found:
                (tools / name).symlink_to(found)
        _, err = self.forge_pr(str(self.run), '--create', code=2, env=dict(self.env, PATH=str(tools)))
        self.assertIn('needs the gh CLI', err)


if __name__ == '__main__':
    unittest.main()
