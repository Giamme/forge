"""plan --reviewer-session: a later review round continues the reviewer's session, or goes fresh.

The unit tests source forge-lib-task.sh like test_escalation_pin; the end-to-end ones drive
forge-parallel.sh with the fake agent CLI of test_forge and read the argv the backend was
given from qa.cmd (claude's `--resume <id>`, codex's `exec resume <id>`)."""
import os
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

import test_forge as fixtures
import test_escalation_pin as pins

ROOT = Path(__file__).resolve().parent.parent
SESSION = fixtures.CLAUDE_SESSION
FAIL = {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'}
# Each retry's dwarf writes something new, or the diff is identical and nothing is reviewed again.
DWARVES = {'a.dwarf.%d' % n: {'content': 'attempt %d' % n} for n in range(2, 6)}


def continues(cmd_file):
    argv = shlex.split(Path(cmd_file).read_text())
    return '--resume' in argv or ('exec' in argv and 'resume' in argv)


def rows(path):
    return [line.split('\t') for line in Path(path).read_text().splitlines()]


class DecideTests(unittest.TestCase):
    """review_session_decide and what prepare_retry keeps beside the findings. The fixtures are
    borrowed from test_escalation_pin, not inherited, so its tests stay its own."""
    setUp = pins.PinTests.setUp
    stub_skill = pins.PinTests.stub_skill
    fail_attempts = pins.PinTests.fail_attempts
    sh = pins.PinTests.sh
    retry = pins.PinTests.retry

    def decide(self, session=SESSION, saved=('claude', 'opus', 'high'), now=('claude', 'opus', 'high')):
        for name, text in (('s', None if session is None else session + '\n'),
                           ('r', None if saved is None else ''.join('%s=%s\n' % kv for kv in zip(('harness', 'model', 'effort'), saved))),
                           ('c', None if now is None else ''.join('%s=%s\n' % kv for kv in zip(('harness', 'model', 'effort'), now)))):
            if text is not None:
                (self.root / name).write_text(text)
            elif (self.root / name).exists():
                (self.root / name).unlink()
        r = self.sh("review_session_decide '%s/s' '%s/r' '%s/c'" % ((self.root,) * 3))
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.rstrip('\n').split('\t')

    def test_the_same_spec_continues(self):
        self.assertEqual(self.decide(), ['continued', 'same-spec', SESSION])
        self.assertEqual(self.decide(saved=('codex', 'm', 'high'), now=('codex', 'm', 'high'))[:2], ['continued', 'same-spec'])
        self.assertEqual(self.decide(saved=('openclaude', 'm', 'x'), now=('openclaude', 'm', 'x'))[:2], ['continued', 'same-spec'])

    def test_a_changed_harness_model_or_effort_is_fresh(self):
        for now in (('openclaude', 'opus', 'high'), ('claude', 'sonnet', 'high'), ('claude', 'opus', 'low')):
            self.assertEqual(self.decide(now=now), ['fresh', 'spec-changed', ''])
        self.assertEqual(self.decide(saved=None), ['fresh', 'spec-changed', ''])

    def test_an_unsupported_harness_is_fresh(self):
        same = ('opencode', 'm', 'x')
        self.assertEqual(self.decide(saved=same, now=same), ['fresh', 'harness-unsupported', ''])
        self.assertEqual(self.decide(now=None), ['fresh', 'harness-unsupported', ''])    # doctor failed

    def test_a_missing_or_malformed_session_is_fresh_and_never_an_error(self):
        self.assertEqual(self.decide(session=None), ['fresh', 'no-session', ''])
        self.assertEqual(self.decide(session=''), ['fresh', 'no-session', ''])
        for bad in ('short-1', 'has space-12345', 'semi;colon-12345', '../etc/passwd-1', 'a' * 129):
            self.assertEqual(self.decide(session=bad), ['fresh', 'invalid-session', ''], bad)
        for good in ('a' * 8, 'a' * 128):
            self.assertEqual(self.decide(session=good)[0], 'continued')

    def test_a_real_review_replaces_the_session_and_what_it_ran_as(self):
        (self.tdir / 'retry_findings.session').write_text('old-session-0000\n')
        (self.tdir / 'retry_findings.resolved').write_text('harness=old\n')
        (self.tdir / 'qa.session').write_text(SESSION + '\n')
        (self.tdir / 'qa.resolved').write_text('harness=claude\nmodel=opus\n')
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual((self.tdir / 'retry_findings.session').read_text(), SESSION + '\n')
        self.assertEqual((self.tdir / 'retry_findings.resolved').read_text(), 'harness=claude\nmodel=opus\n')

    def test_an_older_session_never_sits_beside_newer_findings(self):
        (self.tdir / 'retry_findings.session').write_text('old-session-0000\n')
        (self.tdir / 'retry_findings.resolved').write_text('harness=old\n')
        self.assertEqual(self.retry().returncode, 0)     # a real review that left no session
        self.assertIn('FORGE_VERDICT: FAIL', (self.tdir / 'retry_findings.md').read_text())
        self.assertEqual(sorted(p.name for p in self.tdir.glob('retry_findings.*')), ['retry_findings.md'])

    def test_emptied_findings_lose_their_session(self):
        for n in ('session', 'resolved'):
            (self.tdir / ('retry_findings.' + n)).write_text('x\n')
        (self.tdir / 'qa.last').write_text('The dwarf produced no changes at all.\n')
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual((self.tdir / 'retry_findings.md').read_text(), '')
        self.assertEqual(sorted(p.name for p in self.tdir.glob('retry_findings.*')), ['retry_findings.md'])

    def test_kept_findings_keep_their_session(self):
        for status, qa_last in (('UNKNOWN', 'no verdict line\n'), ('INFRA', ''),
                                ('FAIL', 'The retry changed nothing: identical.\n')):
            (self.tdir / 'retry_findings.md').write_text('earlier report\n')
            (self.tdir / 'retry_findings.session').write_text(SESSION + '\n')
            (self.tdir / 'retry_findings.resolved').write_text('harness=claude\n')
            (self.tdir / 'reviewed.commit').write_text('0' * 40 + '\n')
            (self.tdir / 'status').write_text(status + '\n')
            (self.tdir / 'qa.last').write_text(qa_last)
            (self.tdir / 'qa.session').write_text('other-session-9999\n')    # not this review's
            self.assertEqual(self.retry().returncode, 0)
            self.assertEqual((self.tdir / 'retry_findings.session').read_text(), SESSION + '\n', status)
            self.assertEqual((self.tdir / 'retry_findings.resolved').read_text(), 'harness=claude\n', status)

    def test_an_infrastructure_resume_leaves_the_session_alone(self):
        (self.tdir / 'retry_findings.md').write_text('earlier report\n')
        (self.tdir / 'retry_findings.session').write_text(SESSION + '\n')
        (self.tdir / 'retry_findings.resolved').write_text('harness=claude\n')
        r = self.sh("infra_stop '%s' a dwarf 2; prepare_resume '%s' a" % (self.tdir, self.plan))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tdir / 'retry_findings.session').read_text(), SESSION + '\n')
        self.assertEqual((self.tdir / 'retry_findings.resolved').read_text(), 'harness=claude\n')


class RoundTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, *flags, ids='a'):
        p = self.root / 'plan'; p.mkdir()
        files = {'a': 'a.txt', 'b': 'b.txt'}
        (p / 'tasks.tsv').write_text(''.join('%s\t-\tlow\t%s\tsol\topus\t%s\n' % (i, files[i], i.upper()) for i in ids))
        for i in ids:
            t = p / 'tasks' / i; t.mkdir(parents=True)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.plan_output = r.stdout
        return p

    def first_round_fails(self, *flags, later=None):
        """Round 1 is reviewed FAIL; `later` is the scenario for the calls after it. The fake counts
        calls per scenario file, so the keys are the task's overall call numbers."""
        self.scenario(dict(DWARVES, **{'a.qa.1': FAIL}, **(later or {})))
        p = self.plan_of(*flags)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        return p

    def retry(self, p):
        return self.run_script('forge-parallel.sh', 'retry', p, 'a')

    def log(self, p):
        return rows(p / 'tasks/a/review_sessions.tsv')

    # --- the option --------------------------------------------------------------------------
    def test_the_option_is_a_plan_file_and_one_summary_line(self):
        p = self.plan_of('--reviewer-session')
        self.assertTrue((p / 'reviewer_session').exists())
        self.assertEqual(sum('reviewer-session' in line for line in self.plan_output.splitlines()), 1)
        p2 = self.root / 'plan'
        self.assertEqual(self.run_script('forge-parallel.sh', 'plan', p2, '--repo', self.repo, '--no-memory',
                                         '--no-reviewer-session').returncode, 0)
        self.assertFalse((p / 'reviewer_session').exists())

    def test_off_by_default_nothing_continues_and_nothing_is_logged(self):
        p = self.first_round_fails()
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertEqual(self.calls().count('qa'), 2)
        self.assertFalse(continues(p / 'tasks/a/qa.cmd'))
        self.assertFalse((p / 'tasks/a/review_sessions.tsv').exists())
        self.assertNotIn('reviewer-session', self.plan_output)

    # --- per-task QA -------------------------------------------------------------------------
    def test_a_retry_with_the_same_qa_spec_continues_the_session(self):
        p = self.first_round_fails('--reviewer-session')
        self.assertFalse(continues(p / 'tasks/a/qa.cmd'))
        self.assertEqual(self.retry(p).returncode, 0)
        argv = shlex.split((p / 'tasks/a/qa.cmd').read_text())
        self.assertEqual(argv[argv.index('--resume') + 1], SESSION)
        self.assertEqual(self.log(p), [['1', '1', 'fresh', 'first-round', ''],
                                       ['2', '2', 'continued', 'same-spec', SESSION]])

    def test_a_changed_qa_spec_goes_fresh(self):
        p = self.first_round_fails('--reviewer-session')
        tsv = p / 'tasks.tsv'; tsv.write_text(tsv.read_text().replace('\topus\t', '\tsonnet\t'))
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertFalse(continues(p / 'tasks/a/qa.cmd'))
        self.assertEqual(self.log(p)[-1], ['2', '2', 'fresh', 'spec-changed', ''])

    def test_a_registry_that_resolves_the_alias_differently_goes_fresh(self):
        p = self.first_round_fails('--reviewer-session')
        registry = self.root / 'registry.tsv'
        registry.write_text((ROOT / 'registry.tsv').read_text().replace('opus\tclaude\topus\t', 'opus\tclaude\topus-next\t'))
        self.env['FORGE_REGISTRY'] = str(registry)
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertFalse(continues(p / 'tasks/a/qa.cmd'))
        self.assertEqual(self.log(p)[-1], ['2', '2', 'fresh', 'spec-changed', ''])

    def test_a_pinned_implementer_still_continues_the_reviewers_session(self):
        p = self.first_round_fails('--reviewer-session')
        (p / 'tasks/a/escalation.pin').write_text('sol:high\n')
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertIn('sol:high', (p / 'tasks.tsv').read_text())
        self.assertTrue(continues(p / 'tasks/a/qa.cmd'))
        self.assertEqual(self.log(p)[-1], ['2', '2', 'continued', 'same-spec', SESSION])

    def test_a_malformed_saved_id_goes_fresh(self):
        p = self.first_round_fails('--reviewer-session')
        (p / 'tasks/a/qa.session').write_text('not a valid id!\n')
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertFalse(continues(p / 'tasks/a/qa.cmd'))
        self.assertEqual(self.log(p)[-1], ['2', '2', 'fresh', 'invalid-session', ''])

    def test_a_review_that_left_no_session_leaves_no_older_one(self):
        p = self.first_round_fails('--reviewer-session', later={'a.qa.2': FAIL})
        self.assertEqual(self.retry(p).returncode, 5)               # round 2, continued, FAIL
        self.assertTrue((p / 'tasks/a/retry_findings.session').exists())
        (p / 'tasks/a/qa.session').unlink()
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertFalse(continues(p / 'tasks/a/qa.cmd'))
        self.assertEqual([r[0:4] for r in self.log(p)],
                         [['1', '1', 'fresh', 'first-round'], ['2', '2', 'continued', 'same-spec'],
                          ['3', '3', 'fresh', 'no-session']])

    def test_a_failed_continue_is_followed_by_a_fresh_dispatch_that_decides(self):
        # qa.2 is round 2's continued dispatch (it fails); qa.3, the fresh one, passes.
        p = self.first_round_fails('--reviewer-session', later={'a.qa.2': {'continue_fails': True}})
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertEqual((p / 'tasks/a/status').read_text().strip(), 'PASS')
        self.assertEqual(self.calls().count('qa'), 3)            # one in round 1, two in round 2
        self.assertFalse(continues(p / 'tasks/a/qa.cmd'))        # the one that counted
        self.assertEqual(self.log(p)[1:], [['2', '2', 'fresh', 'continue-failed', SESSION]])
        self.assertTrue((p / 'tasks/a/qa.continue.out').exists())

    def test_a_harness_that_cannot_continue_is_unsupported_and_goes_fresh(self):
        p = self.first_round_fails('--reviewer-session')
        real = self.bin / 'claude.real'
        shutil.copy(self.bin / 'claude', real)
        (self.bin / 'claude').unlink()
        (self.bin / 'claude').write_text(
            '#!/usr/bin/env python3\nimport os, subprocess, sys\n'
            "real = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'claude.real')\n"
            "if '--help' in sys.argv:\n"
            "    r = subprocess.run([real] + sys.argv[1:], capture_output=True, text=True)\n"
            "    sys.stdout.write(r.stdout.replace('--resume ', '')); sys.exit(r.returncode)\n"
            'os.execv(real, [real] + sys.argv[1:])\n')
        (self.bin / 'claude').chmod(0o755)
        self.env['FORGE_CAPABILITY_CACHE'] = str(self.root / 'fresh-capabilities')
        self.assertEqual(self.retry(p).returncode, 0)
        self.assertEqual((p / 'tasks/a/status').read_text().strip(), 'PASS')
        self.assertFalse(continues(p / 'tasks/a/qa.cmd'))
        self.assertEqual(self.log(p)[1], ['2', '2', 'fresh', 'continue-unsupported', SESSION])
        self.assertEqual(len(self.log(p)), 2)

    def test_shadow_reviewers_never_continue(self):
        self.scenario(dict(DWARVES, **{'a.qa.1': FAIL}))
        p = self.plan_of('--reviewer-session', '--shadow-qa', 'opus,sol', '--shadow-rate', '1')
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        self.assertEqual(self.retry(p).returncode, 0)
        shadows = sorted(p.glob('shadow/a/*/qa.cmd'))
        self.assertEqual(len(shadows), 2)
        self.assertTrue(continues(p / 'tasks/a/qa.cmd'))
        for cmd in shadows:
            self.assertFalse(continues(cmd), cmd)

    # --- final reviews ------------------------------------------------------------------------
    def merged_plan(self, *flags):
        self.scenario({})
        p = self.plan_of(*flags, ids='ab')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def review(self, p, *args):
        before = set(p.glob('final-review-*'))
        r = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'opus', *args)
        self.assertEqual(r.returncode, 0, r.stdout)
        [new] = set(p.glob('final-review-*')) - before
        meta = dict(line.split('=', 1) for line in (new / 'review.meta').read_text().splitlines())
        return new, meta

    def test_a_second_final_review_continues_the_first_ones_session(self):
        p = self.merged_plan('--reviewer-session')
        first, meta = self.review(p)
        self.assertEqual((meta['session'], meta['session_reason']), ('fresh', 'first-round'))
        self.assertFalse(continues(first / 'qa.cmd'))
        second, meta = self.review(p)
        self.assertEqual((meta['session'], meta['session_reason']), ('continued', 'same-spec'))
        argv = shlex.split((second / 'qa.cmd').read_text())
        self.assertEqual(argv[argv.index('--resume') + 1], SESSION)

    def test_a_final_review_with_another_spec_is_fresh(self):
        p = self.merged_plan('--reviewer-session')
        self.review(p)
        r = self.run_script('forge-parallel.sh', 'review', p, '--qa', 'sonnet')
        self.assertEqual(r.returncode, 0, r.stdout)
        newest = max(p.glob('final-review-*'), key=lambda d: d.stat().st_mtime_ns)
        text = (newest / 'review.meta').read_text()
        self.assertIn('session=fresh\nsession_reason=spec-changed\n', text)
        self.assertFalse(continues(newest / 'qa.cmd'))

    def test_final_reviews_of_a_plan_without_the_option_record_nothing(self):
        p = self.merged_plan()
        self.review(p)
        second, meta = self.review(p)
        self.assertNotIn('session', meta)
        self.assertFalse(continues(second / 'qa.cmd'))


if __name__ == '__main__':
    unittest.main()
