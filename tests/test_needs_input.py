"""NEEDS_INPUT (#44): a task stopped for the user's answer.

The stop itself is built here; the dwarf-message gate that will cause it comes later, so a test
puts a task in the state by writing its `status` and `needs_input.md`, as the gate will. Run
cases drive forge-parallel.sh with the fake agent CLI from test_forge.py; the scheduler cases use
the stand-in worker from test_control_requests.py. Fixtures are borrowed, not inherited."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import os
from pathlib import Path
import unittest

import test_control_requests as control_fixtures
import test_forge as fixtures

FINDING = '- [P1][CONFIRMED] a.txt:1 — broken'
QUESTION = 'The implementer asked:\nShould the report use UTC or local time?\n'


class NeedsInputCase(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, *flags):
        """a, and b which depends on a, each with a prompt."""
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\nb\ta\tlow\tb.txt\tsol\topus\tB\n')
        for i in 'ab':
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def failed_plan(self, *flags):
        """Attempt 1 of `a` fails its review with FINDING; `b` is blocked behind it."""
        p = self.plan_of(*flags)
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': FINDING}, 'a.dwarf.2': {'content': 'fixed'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.read(p, 'status'), 'FAIL')
        return p

    def ask(self, p, question=QUESTION):
        """What the gate will do: the question on disk and the status."""
        (p / 'tasks/a/needs_input.md').write_text(question)
        (p / 'tasks/a/status').write_text('NEEDS_INPUT\n')

    def waiting_plan(self, *flags):
        """Attempt 1 failed review; attempt 2 carried its findings, then ended asking a question.
        The old qa.last is still on disk."""
        p = self.failed_plan(*flags)
        r = self.run_script('forge-parallel.sh', '_prepare_retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn(FINDING, self.read(p, 'retry_findings.md'))
        (p / 'tasks/a/attempt').write_text('2\n')
        self.ask(p)
        return p

    def read(self, plan, name, task='a'):
        return (plan / 'tasks' / task / name).read_text().strip()

    def snapshot(self, p):
        return {n: self.read(p, n) if (p / 'tasks/a' / n).exists() else None
                for n in ('status', 'needs_input.md', 'attempt')}

    def dwarf_count(self):
        return self.calls().count('dwarf')


class SummaryTests(NeedsInputCase):
    def test_the_summary_waits_for_the_answer_and_does_not_call_the_task_failed(self):
        p = self.waiting_plan()
        before = self.dwarf_count()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.dwarf_count(), before)                    # a later run never dispatches it
        self.assertEqual(self.read(p, 'status'), 'NEEDS_INPUT')
        self.assertEqual(self.read(p, 'status', 'b'), 'BLOCKED')        # and its dependent never starts
        self.assertIn('waiting for your answer:\n'
                      '  a: The implementer asked:\n'
                      f'    answer with: forge-parallel.sh retry {p} a --answer "<text>"\n', r.stdout)
        self.assertNotIn('retry a failed task', r.stdout)

    def test_the_question_line_is_the_first_non_empty_one_cut_at_120_characters(self):
        p = self.failed_plan()
        self.ask(p, '\n\n' + 'q' * 200 + '\nsecond line\n')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertIn('  a: ' + 'q' * 120 + '\n', r.stdout)
        self.assertNotIn('q' * 121, r.stdout)
        self.assertNotIn('second line', r.stdout)

    def test_another_failed_task_keeps_the_retry_hint(self):
        p = self.failed_plan()
        (p / 'tasks/b/status').write_text('FAIL\n')
        self.ask(p)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertIn('waiting for your answer:', r.stdout)
        self.assertIn('retry a failed task with its reviewer', r.stdout)


class AnswerTests(NeedsInputCase):
    def assert_answered(self, p, answer, before_attempt):
        dwarf = (p / 'tasks/a/dwarf.input').read_text()
        self.assertIn('The user answered your question', dwarf)
        self.assertIn(answer, dwarf)
        self.assertIn('> Should the report use UTC or local time?', dwarf)
        self.assertFalse((p / 'tasks/a/needs_input.md').exists())
        self.assertFalse((p / 'tasks/a/answer.md').exists())
        self.assertEqual(int(self.read(p, 'attempt')), before_attempt + 1)

    def test_an_answer_resumes_the_task_with_the_question_and_the_answer_in_the_prompt(self):
        p = self.waiting_plan()
        attempt = int(self.read(p, 'attempt'))
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--answer', 'use UTC')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assert_answered(p, 'use UTC', attempt)
        self.assertEqual(self.read(p, 'retry_findings.source'), 'answer')
        self.assertTrue((p / 'tasks/a/merged').exists())

    def test_an_answer_file_does_the_same(self):
        p = self.waiting_plan()
        attempt = int(self.read(p, 'attempt'))
        answer = self.root / 'reply.txt'
        answer.write_text('use UTC, always\nand say so\n')
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--answer-file', answer)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assert_answered(p, 'use UTC, always\nand say so', attempt)

    def test_an_answer_after_a_reviewed_fail_keeps_the_old_findings_beside_it(self):
        p = self.waiting_plan()
        self.assertIn(FINDING, (p / 'tasks/a/qa.last').read_text())    # the review that came before the question
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--answer', 'use UTC')
        self.assertEqual(r.returncode, 0, r.stdout)
        dwarf = (p / 'tasks/a/dwarf.input').read_text()
        self.assertIn(FINDING, dwarf)
        self.assertIn('use UTC', dwarf)
        self.assertLess(dwarf.index(FINDING), dwarf.index('use UTC'))
        self.assertEqual(self.read(p, 'retry_findings.source'), 'answer')
        self.assertIn(FINDING, self.read(p, 'retry_findings.md'))

    def test_the_findings_commit_stays_as_it_was(self):
        p = self.waiting_plan()
        commit = self.read(p, 'retry_findings.commit')
        (p / 'tasks/a/answer.md').write_text('use UTC\n')
        r = self.run_script('forge-parallel.sh', '_prepare_retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.read(p, 'retry_findings.commit'), commit)
        self.assertEqual(self.read(p, 'retry_findings.source'), 'answer')
        self.assertEqual([f.name for f in (p / 'tasks/a').glob('retry_findings.next*')], [])
        self.assertFalse((p / 'tasks/a/status').exists())              # the scheduler's entry clears it

    def test_a_refused_preparation_keeps_the_question_the_answer_and_the_findings(self):
        p = self.pooled_failed_plan()
        self.ask(p)
        findings = self.read(p, 'retry_findings.md')
        self.assertIn('already in the highest tier', self.read(p, 'split.suggested'))
        dwarfs = self.dwarf_count()
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--answer', 'use UTC')
        self.assertEqual((r.returncode, self.dwarf_count()), (2, dwarfs), r.stdout)
        self.assertEqual(self.read(p, 'status'), 'NEEDS_INPUT')
        self.assertEqual(self.read(p, 'needs_input.md'), QUESTION.strip())
        self.assertEqual(self.read(p, 'answer.md'), 'use UTC')
        self.assertEqual(self.read(p, 'retry_findings.md'), findings)
        self.assertEqual(self.read(p, 'retry_findings.source'), 'review')
        self.assertFalse((p / 'tasks/a/retry_findings.next').exists())
        # The same answer goes through once the human names a model.
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--answer', 'use UTC', '--dwarf', 'sol')
        self.assertEqual((r.returncode, self.dwarf_count()), (0, dwarfs + 1), r.stdout)
        self.assertIn('use UTC', (p / 'tasks/a/dwarf.input').read_text())
        self.assertFalse((p / 'tasks/a/answer.md').exists())

    def pooled_failed_plan(self):
        """Three failed attempts on the only model there is: the retry policy stops the task."""
        p = self.plan_of('--dwarf-low', 'sol')
        table = {'a.qa.%d' % n: {'verdict': 'FAIL', 'findings': FINDING} for n in (1, 2, 3)}
        table.update({'a.dwarf.%d' % n: {'content': 'try %d' % n} for n in (1, 2, 3, 4, 5)})
        self.scenario(table)
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 5)
        self.assertEqual(r.returncode, 5, r.stdout)
        return p


class RefusalTests(NeedsInputCase):
    def assert_nothing_changed(self, p, before, dwarfs):
        self.assertEqual(self.snapshot(p), before)
        self.assertEqual(self.dwarf_count(), dwarfs)

    def test_retry_without_an_answer_exits_2_and_changes_nothing(self):
        p = self.waiting_plan()
        before, dwarfs = self.snapshot(p), self.dwarf_count()
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn(f'a is waiting for an answer: forge-parallel.sh retry {p} a --answer', r.stdout)
        self.assert_nothing_changed(p, before, dwarfs)
        self.assertFalse((p / 'tasks/a/answer.md').exists())

    def test_an_answer_on_a_task_that_is_not_waiting_exits_2(self):
        p = self.failed_plan()
        before, dwarfs = self.snapshot(p), self.dwarf_count()
        for flag, value in (('--answer', 'use UTC'), ('--answer-file', self.root / 'none')):
            with self.subTest(flag):
                r = self.run_script('forge-parallel.sh', 'retry', p, 'a', flag, value)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertFalse((p / 'tasks/a/answer.md').exists())
                self.assert_nothing_changed(p, before, dwarfs)

    def test_a_rejected_answer_leaves_the_other_options_unapplied_too(self):
        p = self.failed_plan()
        before, dwarfs = self.snapshot(p), self.dwarf_count()
        (p / 'setup_cmd').write_text('original setup')
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a', '--answer', 'use UTC', '--setup', 'replacement setup')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual((p / 'setup_cmd').read_text(), 'original setup')
        self.assertFalse((p / 'tasks/a/answer.md').exists())
        self.assert_nothing_changed(p, before, dwarfs)

    def test_an_empty_answer_exits_2_and_changes_nothing(self):
        p = self.waiting_plan()
        before, dwarfs = self.snapshot(p), self.dwarf_count()
        empty = self.root / 'empty.txt'; empty.write_text('')
        for args in (('--answer', ''), ('--answer-file', empty), ('--answer-file', self.root / 'missing.txt')):
            with self.subTest(args):
                r = self.run_script('forge-parallel.sh', 'retry', p, 'a', *args)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertFalse((p / 'tasks/a/answer.md').exists())
                self.assert_nothing_changed(p, before, dwarfs)

    def test_the_preparation_itself_wants_a_fresh_answer(self):
        p = self.waiting_plan()
        before, dwarfs = self.snapshot(p), self.dwarf_count()
        findings = self.read(p, 'retry_findings.md')
        answer = p / 'tasks/a/answer.md'
        for what in ('missing', 'empty', 'older than the question', 'as old as the question'):
            with self.subTest(what):
                if what == 'empty':
                    answer.write_text('')
                if what in ('older than the question', 'as old as the question'):
                    answer.write_text('use UTC\n')
                    question = (p / 'tasks/a/needs_input.md').stat()
                    if what == 'as old as the question':
                        os.utime(answer, ns=(question.st_atime_ns, question.st_mtime_ns))
                    else:
                        stamp = question.st_mtime - 60
                        os.utime(answer, (stamp, stamp))
                r = self.run_script('forge-parallel.sh', '_prepare_retry', p, 'a')
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn('a is waiting for an answer', r.stdout)
                self.assert_nothing_changed(p, before, dwarfs)
                self.assertEqual(self.read(p, 'retry_findings.md'), findings)
                self.assertFalse((p / 'tasks/a/retry_findings.next').exists())
        self.assertEqual(self.read(p, 'needs_input.md'), QUESTION.strip())


class SchedulerTests(control_fixtures.Base):
    def test_the_scheduler_never_retries_a_question_and_blocks_what_waits_on_it(self):
        self.task('a', status='NEEDS_INPUT')
        (self.plan / 'tasks/a/needs_input.md').write_text(QUESTION)
        self.task('b', deps='a')
        self.task('c')
        proc = self.start('--retry-failed', 3)
        _, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 5, err)
        self.assertEqual([self.status(n) for n in 'abc'], ['NEEDS_INPUT', 'BLOCKED', 'MERGED'])
        self.assertEqual([e for e in self.events() if e.startswith(('a ', 'b '))], [])
        # A second run of the same plan leaves it where it is.
        proc = self.start('--retry-failed', 3)
        _, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 5, err)
        self.assertEqual([self.status(n) for n in 'ab'], ['NEEDS_INPUT', 'BLOCKED'])
        self.assertEqual([e for e in self.events() if e.startswith(('a ', 'b '))], [])
        self.assertEqual((self.plan / 'tasks/a/needs_input.md').read_text(), QUESTION)

    def test_control_retry_is_refused_with_the_answer_hint(self):
        self.task('x', status='NEEDS_INPUT')
        (self.plan / 'tasks/x/needs_input.md').write_text(QUESTION)
        self.task('a')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/release"\n')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'tasks/a/calls').exists(), 'a to start')
        self.control('retry', self.plan, 'x')
        self.wait_for(lambda: self.outcomes(), 'the outcome line')
        (self.plan / 'release').write_text('')
        proc.communicate(timeout=60)
        [line] = self.outcomes()
        self.assertEqual((line['task'], line['outcome']), ('x', 'refused'))
        self.assertTrue(line['reason'].startswith(f'x is waiting for an answer: forge-parallel.sh retry {self.plan} x --answer'),
                        line['reason'])
        self.assertEqual(self.status('x'), 'NEEDS_INPUT')
        self.assertEqual((self.plan / 'tasks/x/needs_input.md').read_text(), QUESTION)
        self.assertNotIn('x prepared', ' '.join(self.events()))


if __name__ == '__main__':
    unittest.main()
