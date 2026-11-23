"""`forge control retry --answer` (#147): the answer to a NEEDS_INPUT task's question, on a live run.

With a scheduler live the CLI queues one request carrying the answer; the scheduler writes
tasks/<id>/answer.md and requeues the task through the preparation `forge-parallel.sh retry --answer`
uses, so the next implementer prompt holds the question and the answer. With none live, the CLI runs
`forge-parallel.sh retry --answer-file --no-dispatch` itself. The scheduler cases use the stand-in
worker of test_control_requests.py; the last class drives a real run with the fake agent CLI."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import test_control_requests as fixtures
import test_forge as forge_fixtures
import test_needs_input as needs_input

ROOT = fixtures.ROOT
QUESTION = needs_input.QUESTION
SECRET = 'the answer is port 80'


def load_control():
    spec = importlib.util.spec_from_file_location('forge_control_under_test', ROOT / 'scripts/forge-control.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Base(fixtures.Base):
    def waiting(self, name='x'):
        self.task(name, status='NEEDS_INPUT')
        (self.plan / 'tasks' / name / 'needs_input.md').write_text(QUESTION)

    def answer_files(self):
        return sorted(p.name for p in self.plan.rglob('answer.md'))


class CliTests(Base):
    def test_an_answer_is_queued_and_logged_without_its_text(self):
        self.waiting()
        self.live()
        result = self.control('retry', self.plan, 'x', '--answer', f'  {SECRET}\n', '--actor', 'ui', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        [name] = self.requests()
        self.assertEqual(json.loads(result.stdout), {'queued': True, 'request': name})
        body = json.loads((self.plan / 'control.requests' / name).read_text())
        self.assertEqual((body['verb'], body['task'], body['dwarf'], body['actor'], body['answer']),
                         ('retry', 'x', None, 'ui', SECRET))
        [entry] = self.log()
        self.assertEqual((entry['action'], entry['task'], entry['value'], entry['answer_chars'], entry['request']),
                         ('retry', 'x', 'answer', len(SECRET), name))
        self.assertNotIn(SECRET, (self.plan / 'control.log').read_text())
        self.assertEqual(self.answer_files(), [])           # the scheduler writes it, not the CLI

    def test_the_answer_file_reads_stdin_and_a_file(self):
        self.waiting()
        self.live()
        piped = subprocess.run([sys.executable, str(fixtures.CONTROL), 'retry', str(self.plan), 'x', '--answer-file', '-'],
                               input='from stdin\nsecond line\n', capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(piped.returncode, 0, piped.stderr)
        reply = self.root / 'reply.txt'
        reply.write_text('from a file\n')
        self.assertEqual(self.control('retry', self.plan, 'x', '--answer-file', reply).returncode, 0)
        bodies = [json.loads((self.plan / 'control.requests' / n).read_text())['answer'] for n in self.requests()]
        self.assertEqual(sorted(bodies), ['from a file', 'from stdin\nsecond line'])

    def test_usage_errors_exit_2_and_queue_nothing(self):
        self.waiting()
        self.live()
        reply = self.root / 'empty.txt'
        reply.write_text('\n')
        nul = self.root / 'nul.txt'
        nul.write_bytes(b'a\0b')
        cases = [
            ('empty', ('--answer', '')),
            ('blank', ('--answer', ' \n\t ')),
            ('empty file', ('--answer-file', reply)),
            ('missing file', ('--answer-file', self.root / 'nope.txt')),
            ('too long', ('--answer', 'x' * 8001)),
            ('nul in a file', ('--answer-file', nul)),
            ('with a dwarf', ('--answer', 'ok', '--dwarf', 'sol:high:codex')),
            ('with a decision', ('--answer', 'ok', '--decide', 'retry=2', '--reason', 'r', '--approved')),
            ('both forms', ('--answer', 'ok', '--answer-file', reply)),
        ]
        for what, args in cases:
            with self.subTest(what):
                result = self.control('retry', self.plan, 'x', *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.log(), [])
        self.assertEqual(self.control('retry', self.plan, 'x', '--answer', 'x' * 8000).returncode, 0)   # the limit itself

    def test_a_file_past_the_read_is_too_long_not_empty_or_invalid(self):
        # The read keeps 8000 characters of UTF-8 plus a little whitespace. Past that, the refusal is the
        # character limit: a prefix of only spaces is not an empty answer, and a character split by the
        # cut is not bad UTF-8. A file that ends on the cap is still judged whole.
        self.waiting()
        self.live()
        cap = 8000 * 4 + 1024
        limited = 'limited to 8000 characters'

        def refuse(path):
            result = self.control('retry', self.plan, 'x', '--answer-file', path)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn(limited, result.stderr)
            self.assertNotIn('empty', result.stderr)
            self.assertNotIn('UTF-8', result.stderr)

        spaces = self.root / 'spaces.txt'
        spaces.write_bytes(b' ' * 40000 + b'yes')
        split = self.root / 'split.txt'
        split.write_bytes(b' ' * (cap - 1) + 'é'.encode() + b'yes')
        blank = self.root / 'blank.txt'
        blank.write_bytes(b' ' * cap)
        whole = self.root / 'whole.txt'
        whole.write_bytes(b' ' * (cap - 3) + b'yes')
        refuse(spaces)
        refuse(split)
        piped = subprocess.run([sys.executable, str(fixtures.CONTROL), 'retry', str(self.plan), 'x', '--answer-file', '-'],
                               input=' ' * 40000 + 'yes', capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(piped.returncode, 2, piped.stderr)
        self.assertIn(limited, piped.stderr)
        self.assertNotIn('empty', piped.stderr)
        result = self.control('retry', self.plan, 'x', '--answer-file', blank)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('the answer is empty', result.stderr)
        self.assertNotIn(limited, result.stderr)
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.control('retry', self.plan, 'x', '--answer-file', whole).returncode, 0)
        [name] = self.requests()
        self.assertEqual(json.loads((self.plan / 'control.requests' / name).read_text())['answer'], 'yes')

    def test_reason_and_approved_with_an_answer_are_refused(self):
        self.waiting()
        self.live()
        cases = [
            ('reason', ('--answer', 'ok', '--reason', 'r')),
            ('approved', ('--answer', 'ok', '--approved')),
            ('both', ('--answer', 'ok', '--reason', 'r', '--approved')),
        ]
        for what, args in cases:
            with self.subTest(what):
                result = self.control('retry', self.plan, 'x', *args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('not combined with --reason or --approved', result.stderr)
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.log(), [])

    def test_an_answer_that_would_not_fit_in_a_request_is_refused(self):
        self.waiting()
        self.live()
        astral = '\U0001F600' * 8000          # escaped as 12 bytes each in the request's JSON
        result = self.control('retry', self.plan, 'x', '--answer', astral)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('does not fit', result.stderr)
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.log(), [])

    def test_a_task_that_is_not_waiting_is_refused_naming_its_status(self):
        self.task('f', status='FAIL')
        self.task('m', status='PASS')
        (self.plan / 'tasks/m/merged').write_text('')
        self.live()
        for name, status in (('f', 'FAIL'), ('m', 'MERGED')):
            with self.subTest(name):
                result = self.control('retry', self.plan, name, '--answer', 'use UTC')
                self.assertEqual(result.returncode, 3, result.stderr)
                self.assertIn(status, result.stderr)
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.log(), [])
        self.assertEqual(self.answer_files(), [])

    def test_without_an_answer_a_waiting_task_is_queued_as_before_and_the_scheduler_refuses_it(self):
        self.waiting()
        self.live()
        self.assertEqual(self.control('retry', self.plan, 'x').returncode, 0)
        [name] = self.requests()
        self.assertNotIn('answer', json.loads((self.plan / 'control.requests' / name).read_text()))

    def test_no_live_scheduler_without_an_answer_is_refused_as_before(self):
        self.waiting()
        result = self.control('retry', self.plan, 'x')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('retry needs a live run', result.stderr)


FAKE_PARALLEL = r'''#!/bin/bash
echo "$@" > "$FAKE_CALL"
file=""; while [ $# -gt 0 ]; do [ "$1" = --answer-file ] && file="$2"; shift; done
cat "$file" > "$FAKE_ANSWER"; stat -f %Lp "$file" > "$FAKE_MODE" 2>/dev/null || stat -c %a "$file" > "$FAKE_MODE"
[ -z "$FAKE_SAY" ] || echo "fake forge-parallel says hello"
exit "${FAKE_EXIT:-0}"
'''


class NoLiveSchedulerTests(Base):
    """With no run live the CLI prepares the task itself: forge-parallel.sh retry --answer-file --no-dispatch."""

    def setUp(self):
        super().setUp()
        skill = self.root / 'skill'
        (skill / 'scripts').mkdir(parents=True)
        fake = skill / 'scripts/forge-parallel.sh'
        fake.write_text(FAKE_PARALLEL)
        self.skill = skill
        self.call, self.copy, self.mode = (self.root / n for n in ('call', 'answer', 'mode'))
        self.env.update(FAKE_CALL=str(self.call), FAKE_ANSWER=str(self.copy), FAKE_MODE=str(self.mode))
        self.waiting()

    def run_cli(self, *args, exit_code=0, say=''):
        module = load_control()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(module, 'SKILL', str(self.skill)), mock.patch.dict(os.environ, self.env | {'FAKE_EXIT': str(exit_code), 'FAKE_SAY': say}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = module.main(['retry', str(self.plan), 'x', *map(str, args)])
        return code, out.getvalue(), err.getvalue()

    def test_the_answer_goes_to_forge_parallel_retry_in_a_private_temp_file(self):
        code, out, err = self.run_cli('--answer', SECRET)
        self.assertEqual(code, 0, err)
        words = self.call.read_text().split()
        self.assertEqual(words[:3], ['retry', str(self.plan.resolve()), 'x'])
        self.assertEqual(words[3], '--answer-file')
        self.assertEqual(words[5:], ['--no-dispatch'])
        self.assertEqual(self.copy.read_text(), SECRET + '\n')
        self.assertEqual(self.mode.read_text().strip(), '600')
        self.assertFalse(Path(words[4]).exists())                 # removed after
        self.assertEqual(sorted(p.name for p in self.plan.glob('.*')), [])
        self.assertIn('prepared with your answer', out)
        self.assertEqual(self.requests(), [])

    def test_json_prints_prepared_and_keeps_the_commands_words_off_stdout(self):
        code, out, err = self.run_cli('--answer', SECRET, '--json', say='1')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {'queued': False, 'prepared': True})
        self.assertIn('fake forge-parallel says hello', err)

    def test_its_exit_code_is_passed_through(self):
        code, out, err = self.run_cli('--answer', SECRET, '--json', exit_code=2)
        self.assertEqual(code, 2)
        self.assertEqual(out, '')
        self.assertEqual(sorted(p.name for p in self.plan.glob('.*')), [])

    def test_stdin_works_here_too(self):
        with mock.patch('sys.stdin', io.TextIOWrapper(io.BytesIO(b'from stdin\n'))):
            code, out, err = self.run_cli('--answer-file', '-')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.copy.read_text(), 'from stdin\n')

    def test_a_task_that_is_not_waiting_is_refused_before_anything_runs(self):
        (self.plan / 'tasks/x/status').write_text('FAIL\n')
        code, out, err = self.run_cli('--answer', SECRET)
        self.assertEqual(code, 3)
        self.assertIn('FAIL', err)
        self.assertFalse(self.call.exists())


class SchedulerRequestTests(Base):
    def test_read_request_drops_an_answer_that_is_not_a_good_string(self):
        spec = importlib.util.spec_from_file_location('forge_schedule_under_test', ROOT / 'scripts/forge-schedule.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        (self.plan / 'control.requests').mkdir()

        def read(**fields):
            request = dict(verb='retry', task='x', dwarf=None, at=1.0, actor='cli', **fields)
            (self.plan / 'control.requests/1-1.json').write_text(json.dumps(request))
            return module.read_request(self.plan, '1-1.json')

        self.assertEqual(read(answer='use UTC')['answer'], 'use UTC')
        self.assertIsNotNone(read())
        for bad in (5, ['a'], {'a': 1}, True, '', 'x' * 8001, 'a\0b'):
            with self.subTest(repr(bad)[:20]):
                self.assertIsNone(read(answer=bad))
        self.assertIsNotNone(read(answer='x' * 8000))

    def test_the_scheduler_writes_the_answer_and_requeues_the_task(self):
        self.waiting()
        self.task('a')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/release"\n')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'tasks/a/calls').exists(), 'a to start')
        self.assertEqual(self.control('retry', self.plan, 'x', '--answer', SECRET).returncode, 0)
        self.wait_for(lambda: self.outcomes(), 'the outcome line')
        (self.plan / 'release').write_text('')
        proc.communicate(timeout=60)
        [line] = self.outcomes()
        self.assertEqual((line['task'], line['outcome'], line['value']), ('x', 'applied', 'answer'))
        self.assertEqual(self.status('x'), 'MERGED')
        self.assertIn('x prepared', ' '.join(self.events()))
        self.assertNotIn(SECRET, (self.plan / 'control.log').read_text())

    def test_the_answer_is_on_disk_before_the_preparation_and_newer_than_the_question(self):
        self.waiting()
        self.task('a')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/release"\n')
        # the stand-in preparation records what it finds, as the real one reads it
        worker = self.worker.read_text().replace(
            '    echo "$name prepared ${*:4}" >> "$plan/events.log"\n',
            '    echo "$name prepared ${*:4}" >> "$plan/events.log"\n'
            '    cp "$t/answer.md" "$plan/seen.answer" 2>/dev/null\n'
            '    python3 -c \'import os,sys; print(os.stat(sys.argv[1]).st_mtime_ns > os.stat(sys.argv[2]).st_mtime_ns)\''
            ' "$t/answer.md" "$t/needs_input.md" > "$plan/seen.newer"\n')
        self.worker.write_text(worker)
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'tasks/a/calls').exists(), 'a to start')
        self.control('retry', self.plan, 'x', '--answer', 'use UTC\nnot local')
        self.wait_for(lambda: self.outcomes(), 'the outcome line')
        (self.plan / 'release').write_text('')
        proc.communicate(timeout=60)
        self.assertEqual((self.plan / 'seen.answer').read_text(), 'use UTC\nnot local\n')
        self.assertEqual((self.plan / 'seen.newer').read_text().strip(), 'True')
        self.assertEqual(sorted(p.name for p in (self.plan / 'tasks/x').glob('.*')), [])

    def test_a_request_the_task_cannot_take_is_refused_and_writes_no_answer(self):
        self.task('f', status='FAIL')
        self.waiting()
        self.task('a')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/release"\n')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'tasks/a/calls').exists(), 'a to start')
        requests = self.plan / 'control.requests'
        requests.mkdir(exist_ok=True)
        for i, fields in enumerate((dict(task='f', answer='use UTC'),                      # not waiting
                                    dict(task='x', answer='use UTC', dwarf='sol:high:codex'),
                                    dict(task='x'))):                                      # waiting, no answer
            body = dict(verb='retry', dwarf=None, at=time.time(), actor='cli') | fields
            (requests / f'.{i}.tmp').write_text(json.dumps(body))
            os.replace(requests / f'.{i}.tmp', requests / f'{time.time_ns()}-{i}.json')
            self.wait_for(lambda: len(self.outcomes()) > i, 'outcome %d' % i)
        (self.plan / 'release').write_text('')
        proc.communicate(timeout=60)
        lines = self.outcomes()
        self.assertEqual([(e['task'], e['outcome']) for e in lines], [('f', 'refused'), ('x', 'refused'), ('x', 'refused')])
        self.assertIn('f is FAIL, not waiting for an answer', lines[0]['reason'])
        self.assertIn('not combined', lines[1]['reason'])
        self.assertIn('x is waiting for an answer: forge-parallel.sh retry', lines[2]['reason'])
        self.assertIn('forge control retry', lines[2]['reason'])
        self.assertEqual(self.answer_files(), [])
        self.assertEqual(self.status('x'), 'NEEDS_INPUT')
        self.assertEqual(self.status('f'), 'FAIL')


class EndToEndTests(unittest.TestCase):
    """A real run, the real preparation: the answer reaches the next implementer prompt."""
    git = forge_fixtures.ForgeTests.git
    run_script = forge_fixtures.ForgeTests.run_script
    scenario = forge_fixtures.ForgeTests.scenario
    plan_of = needs_input.NeedsInputCase.plan_of
    ask = needs_input.NeedsInputCase.ask

    def setUp(self):
        forge_fixtures.ForgeTests.setUp(self)

    def test_a_live_waiting_task_resumes_from_forge_control_retry_answer(self):
        p = self.plan_of()
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': needs_input.FINDING}, 'a.dwarf.2': {'content': 'fixed'}})
        first = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(first.returncode, 5, first.stdout)
        self.ask(p)
        # a second run: a waits for the answer (b waits on a), c keeps the scheduler live meanwhile
        with (p / 'tasks.tsv').open('a') as handle:
            handle.write('c\t-\tlow\tc.txt\tsol\topus\tC\n')
        (p / 'tasks/c').mkdir()
        (p / 'tasks/c/prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_c')
        self.scenario({'a.dwarf.2': {'content': 'fixed'}, 'c.dwarf.1': {'sleep': 8}})
        out = {}
        runner = threading.Thread(target=lambda: out.update(r=self.run_script('forge-parallel.sh', 'run', p)))
        runner.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not (p / 'scheduler.live').exists():
            time.sleep(0.05)
        time.sleep(0.5)
        result = subprocess.run([sys.executable, str(fixtures.CONTROL), 'retry', str(p), 'a', '--answer',
                                 'use UTC, always', '--actor', 'ui', '--json'],
                                capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['queued'], True)
        runner.join(180)
        self.assertEqual(out['r'].returncode, 0, out['r'].stdout)
        dwarf = (p / 'tasks/a/dwarf.input').read_text()
        self.assertIn('The user answered your question', dwarf)
        self.assertIn('use UTC, always', dwarf)
        self.assertIn('> Should the report use UTC or local time?', dwarf)
        self.assertEqual((p / 'tasks/a/retry_findings.source').read_text().strip(), 'answer')
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertTrue((p / 'tasks/b/merged').exists())
        self.assertFalse((p / 'tasks/a/needs_input.md').exists())
        self.assertFalse((p / 'tasks/a/answer.md').exists())
        self.assertIn('forge: control: retry a applied', out['r'].stdout)
        log = (p / 'control.log').read_text()
        self.assertNotIn('use UTC', log)
        entries = [json.loads(line) for line in log.splitlines()]
        self.assertEqual([(e['actor'], e['action'], e['value']) for e in entries],
                         [('ui', 'retry', 'answer'), ('scheduler', 'retry', 'answer')])
        self.assertEqual(entries[0]['answer_chars'], len('use UTC, always'))


if __name__ == '__main__':
    unittest.main()
