"""QA on a retry checks the previous round's findings first and sees the diff between attempts.

The end-to-end tests drive forge-parallel.sh with the fake agent CLI of test_forge, like
test_retry; the rest source forge-lib-task.sh as test_escalation_pin does, and call
forge-prompt.py directly."""
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import test_forge as fixtures
import test_escalation_pin as pins
import test_retry as retry_fixtures

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / 'scripts' / 'forge-prompt.py'
FIRST = 'FIRSTLINE_UNCHANGED'
FENCED = '- [P1][CONFIRMED] a.txt:1 — broken, as in\n```diff\n-old\n+new\n```\nREPORT_SENTINEL'


def prompt(*args):
    return subprocess.run([sys.executable, str(PROMPT), *map(str, args)], capture_output=True, text=True, check=True).stdout


def last_diff_fence(text):
    """What the dataset and forge-ui take as the reviewed diff of a QA prompt."""
    return text[[m.start() for m in re.finditer(r'^```diff$', text, re.M)][-1]:]


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, text):
        path = self.dir / name; path.write_text(text); return path

    def base(self):
        return ['--requirements', self.write('req', 'REQ\n'), '--retry', self.write('retry', FENCED + '\n')]

    def test_the_dwarf_role_is_the_default_and_ignores_the_round_inputs(self):
        extra = ['--retry-diff', self.write('d', '+x\n'), '--retry-range', 'aaaaaaa..bbbbbbb',
                 '--retry-stat', self.write('s', 'stat\n'), '--reply', self.write('r', 'reply\n')]
        plain = prompt(*self.base())
        self.assertEqual(prompt(*self.base(), '--role', 'dwarf', *extra), plain)
        self.assertIn('## Your previous attempt was reviewed and rejected\n' + FENCED + '\n', plain)

    def test_the_qa_role_frames_the_findings_for_a_reviewer(self):
        out = prompt(*self.base(), '--role', 'qa')
        self.assertIn('## Findings from the previous review round\n', out)
        self.assertNotIn('Your previous attempt was reviewed and rejected', out)
        self.assertIn('still present', out)
        self.assertIn('never as a `- [Px][CONFIRMED]`', out)
        self.assertIn("threshold", out)

    def test_carried_diff_fences_become_text_fences(self):
        out = prompt(*self.base(), '--role', 'qa', '--reply', self.write('r', 'I fixed it:\n```diff\n+z\n```\n'))
        self.assertNotIn('```diff', out)
        self.assertIn('```text\n-old\n+new\n```', out)
        self.assertIn('```text\n+z\n```', out)

    def test_the_diff_since_is_inlined_in_a_patch_fence(self):
        out = prompt(*self.base(), '--role', 'qa', '--retry-diff', self.write('d', '+CHANGED\n'),
                     '--retry-range', 'aaaaaaa..bbbbbbb', '--retry-stat', self.write('s', ' a.txt | 1 +\n'))
        self.assertIn('### Changes since that review (aaaaaaa..bbbbbbb)\n```patch\n+CHANGED\n```', out)
        self.assertNotIn('a.txt | 1', out)

    def test_a_large_diff_since_is_summarised_by_its_stat(self):
        out = prompt(*self.base(), '--role', 'qa', '--retry-diff', self.write('d', '+' + 'x' * 120000 + '\n'),
                     '--retry-range', 'aaaaaaa..bbbbbbb', '--retry-stat', self.write('s', ' big.txt | 9 +\n'))
        self.assertIn('too large to inline', out)
        self.assertIn(' big.txt | 9 +', out)
        self.assertNotIn('xxxxxxxxxx', out)

    def test_the_reply_keeps_its_head_and_marks_the_cut(self):
        out = prompt(*self.base(), '--role', 'qa', '--reply', self.write('r', 'HEAD' + 'y' * 20000 + 'TAIL'))
        self.assertIn("### The implementer's reply (its own account; verify every claim against the code)\nHEAD", out)
        self.assertIn('cut here', out)
        self.assertNotIn('TAIL', out)

    def test_a_first_attempt_has_no_round_sections(self):
        out = prompt('--requirements', self.write('req', 'REQ\n'), '--role', 'qa', '--reply', self.write('r', 'reply\n'),
                     '--retry-diff', self.write('d', '+x\n'))
        self.assertNotIn('previous review round', out)
        self.assertNotIn('implementer', out)
        self.assertNotIn('Changes since', out)


class CarryTests(unittest.TestCase):
    """The commit a carried report judged, and the diff between attempts, in the library.
    The fixtures are borrowed from test_escalation_pin, not inherited, so its tests stay its own."""
    stub_skill = pins.PinTests.stub_skill
    fail_attempts = pins.PinTests.fail_attempts
    sh = pins.PinTests.sh
    retry = pins.PinTests.retry

    def setUp(self):
        pins.PinTests.setUp(self)
        self.wt = self.root / 'wt'; self.wt.mkdir()
        self.run_git('init', '-q')
        self.commits = [self.commit('one'), self.commit('two')]

    def run_git(self, *args):
        return subprocess.run(['git', '-C', str(self.wt), '-c', 'user.name=t', '-c', 'user.email=t@t', *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, text):
        (self.wt / 'a.txt').write_text(text + '\n')
        self.run_git('add', '-A'); self.run_git('commit', '-qm', text)
        return self.run_git('rev-parse', 'HEAD')

    def since(self, name):
        path = self.tdir / ('changes.since.' + name)
        return path.read_text() if path.exists() else None

    def delta(self, reviewed=None):
        r = self.sh("attempt_delta '%s' '%s' '%s'" % (self.tdir, self.wt, reviewed or self.commits[1]))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_a_carried_report_records_the_commit_it_judged(self):
        (self.tdir / 'reviewed.commit').write_text(self.commits[0] + '\n')
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual((self.tdir / 'retry_findings.commit').read_text(), self.commits[0] + '\n')
        self.assertIn('FORGE_VERDICT: FAIL', (self.tdir / 'retry_findings.md').read_text())

    def test_repeating_the_preparation_changes_nothing(self):
        (self.tdir / 'reviewed.commit').write_text(self.commits[0] + '\n')
        self.retry()
        before = {n: (self.tdir / n).read_bytes() for n in ('retry_findings.md', 'retry_findings.commit')}
        self.retry()
        self.assertEqual(before, {n: (self.tdir / n).read_bytes() for n in before})

    def test_emptied_findings_lose_their_commit(self):
        (self.tdir / 'retry_findings.commit').write_text(self.commits[0] + '\n')
        (self.tdir / 'status').write_text('FAIL\n')
        (self.tdir / 'qa.last').write_text('The dwarf produced no changes at all.\n')
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual((self.tdir / 'retry_findings.md').read_text(), '')
        self.assertFalse((self.tdir / 'retry_findings.commit').exists())

    def kept(self, status, qa_last):
        (self.tdir / 'retry_findings.md').write_text('earlier report\n')
        (self.tdir / 'retry_findings.commit').write_text(self.commits[0] + '\n')
        (self.tdir / 'reviewed.commit').write_text(self.commits[1] + '\n')
        (self.tdir / 'status').write_text(status + '\n')
        (self.tdir / 'qa.last').write_text(qa_last)
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual((self.tdir / 'retry_findings.md').read_text(), 'earlier report\n')
        self.assertEqual((self.tdir / 'retry_findings.commit').read_text(), self.commits[0] + '\n')

    def test_findings_that_are_kept_keep_their_commit(self):
        self.kept('UNKNOWN', 'no verdict line here\n')
        self.kept('INFRA', '')
        self.kept('FAIL', 'The retry changed nothing: the diff is byte-identical.\n')

    def test_an_infrastructure_resume_leaves_both_alone(self):
        (self.tdir / 'retry_findings.md').write_text('earlier report\n')
        (self.tdir / 'retry_findings.commit').write_text(self.commits[0] + '\n')
        r = self.sh("infra_stop '%s' a dwarf 2; prepare_resume '%s' a" % (self.tdir, self.plan))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.tdir / 'retry_findings.commit').read_text(), self.commits[0] + '\n')
        self.assertEqual((self.tdir / 'retry_findings.md').read_text(), 'earlier report\n')

    def carried(self, commit=None):
        (self.tdir / 'retry_findings.md').write_text('earlier report\n')
        (self.tdir / 'retry_findings.commit').write_text((commit or self.commits[0]) + '\n')

    def test_the_delta_between_attempts_is_written_as_three_files(self):
        self.carried()
        self.delta()
        self.assertEqual(self.since('range'), '%s %s\n' % tuple(self.commits))
        self.assertIn('-one\n+two', self.since('diff'))
        self.assertIn('a.txt | 2', self.since('stat'))

    def test_no_valid_range_leaves_no_files(self):
        self.carried()
        self.delta()
        (self.tdir / 'retry_findings.commit').unlink()
        self.delta()
        self.assertEqual([self.since(n) for n in ('diff', 'range', 'stat')], [None] * 3)
        for commit in ('0' * 40, 'not-a-commit'):
            self.carried(commit); self.delta()
            self.assertEqual([self.since(n) for n in ('diff', 'range', 'stat')], [None] * 3)
        (self.tdir / 'retry_findings.md').write_text('')
        self.carried(self.commits[0]); (self.tdir / 'retry_findings.md').write_text('')
        self.delta()
        self.assertIsNone(self.since('diff'))
        self.carried(); self.delta(self.commits[0])         # nothing changed
        self.assertIsNone(self.since('diff'))

    def test_a_commit_that_is_no_ancestor_of_the_reviewed_one_is_refused(self):
        self.run_git('checkout', '-q', '--orphan', 'other')
        other = self.commit('unrelated')
        self.carried(other)
        self.delta(self.commits[1])
        self.assertIsNone(self.since('range'))


class ContinuityTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    plan_of = retry_fixtures.RetryTests.plan_of

    def read(self, plan, name):
        return (plan / 'tasks' / 'a' / name).read_text()

    def retried(self, table, retries=1):
        """One task whose attempt 1 writes a.txt and first.txt; attempt 2 rewrites only a.txt."""
        p = self.plan_of([('a', '-', 'a.txt')])
        self.scenario(dict({'a.dwarf.1': {'content': 'attempt one', 'files': {'first.txt': FIRST + '\n'}},
                            'a.dwarf.2': {'content': 'attempt two', 'message': 'REPLY_SENTINEL ```diff'},
                            'a.qa.1': {'verdict': 'FAIL', 'findings': FENCED}}, **table))
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', retries)
        return p, r

    def test_the_reviewer_of_a_retry_gets_the_previous_round_and_the_diff_between_attempts(self):
        p, r = self.retried({})
        self.assertEqual(r.returncode, 0, r.stdout)
        qa, dwarf = self.read(p, 'qa.input'), self.read(p, 'dwarf.input')
        self.assertIn('## Findings from the previous review round\n', qa)
        self.assertIn('REPORT_SENTINEL', qa)
        self.assertIn('labelled', qa)
        self.assertNotIn('Your previous attempt was reviewed and rejected', qa)
        self.assertIn('## Your previous attempt was reviewed and rejected', dwarf)
        self.assertNotIn('Findings from the previous review round', dwarf)
        self.assertIn('### Changes since that review (', qa)
        since = qa.split('```patch\n')[1].split('\n```')[0]
        self.assertIn('+attempt two', since)
        self.assertNotIn(FIRST, since)
        self.assertEqual(self.read(p, 'changes.since.diff').rstrip('\n'), since)
        old, new = self.read(p, 'changes.since.range').split()
        self.assertEqual((old, new), (self.git('rev-parse', 'forge/%s/a~1' % self.run_id(p)).decode().strip(), new))
        self.assertIn('a.txt', self.read(p, 'changes.since.stat'))
        self.assertIn("### The implementer's reply", qa)
        self.assertIn('REPLY_SENTINEL', qa)

    def run_id(self, p):
        return (p / 'run_id').read_text().strip() if (p / 'run_id').exists() else p.name

    def test_the_task_diff_stays_the_last_diff_fence(self):
        p, r = self.retried({})
        self.assertEqual(r.returncode, 0, r.stdout)
        qa = self.read(p, 'qa.input')
        self.assertEqual(len(re.findall(r'^```diff$', qa, re.M)), 1)
        tail = last_diff_fence(qa)
        self.assertIn(FIRST, tail); self.assertIn('+attempt two', tail)
        self.assertIn('```text\n-old\n+new\n```', qa)
        self.assertIn('REPLY_SENTINEL ```text', qa)
        self.assertLess(qa.index('Findings from the previous review round'), qa.index('```diff'))

    def test_a_first_attempt_has_neither_the_files_nor_the_sections(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        qa = self.read(p, 'qa.input')
        for text in ('previous review round', 'Changes since', "implementer's reply"):
            self.assertNotIn(text, qa)
        for name in ('diff', 'range', 'stat'):
            self.assertFalse((p / 'tasks/a' / ('changes.since.' + name)).exists())
        self.assertFalse((p / 'tasks/a/retry_findings.commit').exists())

    def test_a_qa_only_rerun_keeps_the_previous_round_and_that_attempts_reply(self):
        # attempt 2's review gives no verdict, so the third review re-asks QA on the same commit
        p, r = self.retried({'a.qa.2': {'verdict': 'none', 'findings': 'I looked, but reached no conclusion'}}, retries=2)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)
        self.assertEqual(self.calls().count('qa'), 3)
        qa = self.read(p, 'qa.input')
        self.assertIn('## Findings from the previous review round', qa)
        self.assertIn('REPORT_SENTINEL', qa)
        self.assertIn('REPLY_SENTINEL', qa)
        self.assertIn('+attempt two', qa.split('```patch\n')[1].split('\n```')[0])
        self.assertEqual(len(re.findall(r'^```diff$', qa, re.M)), 1)

    def test_a_retry_that_only_rejects_the_findings_still_stops_before_qa(self):
        p, r = self.retried({'a.dwarf.2': {'no_edit': True, 'message': 'the findings are wrong'}})
        self.assertEqual(self.calls().count('qa'), 1)
        self.assertIn('byte-identical', self.read(p, 'qa.last'))


if __name__ == '__main__':
    unittest.main()
