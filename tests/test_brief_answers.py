"""Brief answers, late answers and possibly-stale questions (#135).

The contract is the CLI: `forge brief answer` and `forge brief stale`. Dispatch and the
question writer are not involved; the plans here already have a brief.json.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_dataset  # noqa: E402

CLI = str(ROOT / 'scripts' / 'forge-brief.py')
CONTROL = str(ROOT / 'scripts' / 'forge-control.py')
ANSWER_CAP = 4 * 1024


def question(qid, **extra):
    base = {'id': qid, 'text': 'Question %s?' % qid, 'why': 'it changes the files',
            'options': ['x', 'y'], 'default': 'x', 'scope': 'task', 'must_answer': False}
    base.update(extra)
    return base


def diff_of(*files):
    parts = []
    for name in files:
        parts.append(
            'diff --git a/%s b/%s\n'
            'index 1111111..2222222 100644\n'
            '--- a/%s\n'
            '+++ b/%s\n'
            '@@ -1 +1 @@\n'
            '-old\n'
            '+new\n' % (name, name, name, name))
    return ''.join(parts)


class AnswerTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self.addCleanup(self._restore)
        self.root = Path(tempfile.mkdtemp(prefix='brief-answer-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.plan = self.root / 'plan'
        self.plan.mkdir()
        (self.plan / 'repo').write_text(str(self.root / 'repo') + '\n')
        (self.plan / 'brief').write_text('advisory\n')
        (self.plan / 'tasks.tsv').write_text('')
        data_home = self.root / 'data-home'
        data_home.mkdir()
        os.environ['FORGE_DATA_HOME'] = str(data_home)
        os.environ['FORGE_DATASET'] = 'on'
        self.env = dict(os.environ)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def cli(self, *args, input=None):
        return subprocess.run([sys.executable, CLI, *map(str, args)],
                              capture_output=True, text=True, env=self.env, input=input)

    def task(self, tid, deps='-', files=None, questions=('q1',), status=None, attempts=None, merged=False):
        files = 'src/%s.py' % tid if files is None else files
        with (self.plan / 'tasks.tsv').open('a', encoding='utf-8') as handle:
            handle.write('%s\t%s\tlow\t%s\tsol\topus\tTitle of %s\n' % (tid, deps, files, tid))
        tdir = self.plan / 'tasks' / tid
        tdir.mkdir(parents=True)
        (tdir / 'prompt.md').write_text('Do %s.\n' % tid)
        if questions is not None:
            body = {'questions': [question(qid) for qid in questions], 'model': 'sol', 'generated_at': 't'}
            (tdir / 'brief.json').write_text(json.dumps(body) + '\n')
        if status is not None:
            (tdir / 'status').write_text(status if str(status).endswith('\n') else status + '\n')
        if attempts is not None:
            (tdir / 'attempts.tsv').write_text(attempts)
        if merged:
            (tdir / 'merged').write_text('')
        return tdir

    def files(self, tid):
        tdir = self.plan / 'tasks' / tid
        return {path.name: path.read_bytes() for path in tdir.iterdir()
                if path.is_file() and path.name != '.brief.lock'}

    def answers(self, tid, name='brief.answers.json'):
        return json.loads((self.plan / 'tasks' / tid / name).read_text())

    def events(self, name):
        path = forge_dataset.dataset_dir() / 'dataset.sqlite'
        if not path.exists():
            return []
        db = sqlite3.connect(str(path))
        try:
            rows = db.execute('SELECT task, data FROM events ORDER BY ts, rid').fetchall()
        finally:
            db.close()
        return [(task, json.loads(data)) for task, data in rows if json.loads(data).get('name') == name]

    def log(self):
        path = self.plan / 'control.log'
        self.assertTrue(path.is_file(), 'control.log was not written')
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def control_task(self, stdout):
        lines = [line for line in stdout.splitlines() if line.startswith('forge control add ')]
        self.assertEqual(lines, [lines[0]] if lines else [], stdout)
        self.assertEqual(len(lines), 1, stdout)
        parts = __import__('shlex').split(lines[0])
        self.assertEqual(parts[:3], ['forge', 'control', 'add'], parts)
        self.assertEqual(len(parts), 5, parts)
        self.assertEqual(parts[3], str(self.plan))
        return parts[4]

    def test_an_answer_before_the_task_starts_is_stored_logged_and_replaced(self):
        self.task('a', questions=('q1', 'q2'))
        self.task('b', status='PENDING', questions=('q1',))
        self.task('c', attempts='\n', questions=('q1',))
        before = self.files('a')
        tasks_tsv = (self.plan / 'tasks.tsv').read_bytes()

        other = self.cli('answer', self.plan, 'a', 'q2', 'other')
        self.assertEqual(other.returncode, 0, other.stderr)
        first = self.cli('answer', self.plan, 'a', 'q1', 'first')
        self.assertEqual(first.returncode, 0, first.stderr)
        started = time.time()
        second = self.cli('answer', self.plan, 'a', 'q1', 'second', '--actor', 'ui')
        ended = time.time()
        self.assertEqual(second.returncode, 0, second.stderr)
        for result in (other, first, second):
            self.assertNotIn('forge control add', result.stdout)
            self.assertNotIn('too late', result.stdout)

        data = self.answers('a')
        self.assertEqual(set(data), {'q1', 'q2'})
        self.assertEqual(data['q2'], {'text': 'other', 'actor': 'cli', 't': data['q2']['t']})
        self.assertEqual(data['q2']['actor'], 'cli')
        self.assertEqual(data['q1']['text'], 'second')
        self.assertEqual(data['q1']['actor'], 'ui')
        self.assertIsInstance(data['q1']['t'], (int, float))
        self.assertGreaterEqual(data['q1']['t'], started)
        self.assertLessEqual(data['q1']['t'], ended + 1)
        self.assertNotIn('first', (self.plan / 'tasks' / 'a' / 'brief.answers.json').read_text())
        self.assertFalse((self.plan / 'tasks' / 'a' / 'brief.late.json').exists())

        # PENDING, and an attempts file with no row, are still before the start.
        pending = self.cli('answer', self.plan, 'b', 'q1', 'pending')
        self.assertEqual(pending.returncode, 0, pending.stderr)
        self.assertEqual(self.answers('b')['q1']['text'], 'pending')
        self.assertFalse((self.plan / 'tasks' / 'b' / 'brief.late.json').exists())
        blank = self.cli('answer', self.plan, 'c', 'q1', 'blank')
        self.assertEqual(blank.returncode, 0, blank.stderr)
        self.assertEqual(self.answers('c')['q1']['text'], 'blank')
        self.assertFalse((self.plan / 'tasks' / 'c' / 'brief.late.json').exists())

        after = self.files('a')
        self.assertEqual(set(after) - set(before), {'brief.answers.json'})
        for name, body in before.items():
            self.assertEqual(after[name], body, name)
        self.assertEqual((self.plan / 'tasks.tsv').read_bytes(), tasks_tsv)

        q1 = [line for line in self.log() if line.get('task') == 'a' and line.get('key') == 'q1']
        self.assertEqual([(line['actor'], line['value'], line['previous'], line['late']) for line in q1],
                         [('cli', 'first', None, False), ('ui', 'second', 'first', False)])
        for line in q1:
            self.assertEqual(line['action'], 'answer')
            self.assertIsInstance(line['at'], (int, float))
        recorded = [(body['actor'], body['late']) for task, body in self.events('brief_answer')
                    if task == 'a' and body.get('qid') == 'q1']
        self.assertEqual(sorted(recorded), [('cli', False), ('ui', False)])

    def test_an_unknown_task_or_question_exits_2_and_writes_nothing(self):
        self.task('a', questions=('q1', 'q2'))
        parked = {'questions': [question('q9', scope='plan')], 'model': 'sol', 'generated_at': 't'}
        (self.plan / 'tasks' / 'a' / 'brief.parked.json').write_text(json.dumps(parked) + '\n')
        before = self.files('a')
        tasks_tsv = (self.plan / 'tasks.tsv').read_bytes()

        missing = self.cli('answer', self.root / 'no-plan', 'a', 'q1', 'hi')
        self.assertEqual((missing.returncode, missing.stdout), (3, ''))
        self.assertIn('no-plan', missing.stderr)

        unknown = self.cli('answer', self.plan, 'nope', 'q1', 'hi')
        self.assertEqual((unknown.returncode, unknown.stdout), (2, ''))
        valid_tasks = unknown.stderr.split('valid tasks:', 1)[-1]
        self.assertIn('a', valid_tasks)
        self.assertNotIn('nope', valid_tasks)
        self.assertFalse((self.plan / 'tasks' / 'nope').exists())

        bad = self.cli('answer', self.plan, 'a', 'q9', 'hi')
        self.assertEqual((bad.returncode, bad.stdout), (2, ''))
        valid_questions = bad.stderr.split('valid questions', 1)[-1]
        self.assertIn('q1', valid_questions)
        self.assertIn('q2', valid_questions)
        self.assertNotIn('q9', valid_questions)

        self.assertEqual(self.files('a'), before)
        self.assertEqual((self.plan / 'tasks.tsv').read_bytes(), tasks_tsv)
        self.assertFalse((self.plan / 'control.log').exists())
        self.assertFalse((self.plan / 'tasks' / 'a' / '.brief.lock').exists())
        self.assertEqual(self.events('brief_answer'), [])

    def test_a_late_answer_is_recorded_apart_and_offers_a_follow_up(self):
        # RUNNING, a finished status, the merge marker (status may still say PASS),
        # an attempts.tsv row on a task that is otherwise PENDING, and BLOCKED.
        cases = (
            ('run', {'status': 'RUNNING'}),
            ('done', {'status': 'MERGED'}),
            ('marked', {'status': 'PASS', 'merged': True}),
            ('tried', {'status': 'PENDING', 'attempts': '1700000000\t1\tFAIL\t\tsol\n'}),
            ('blocked', {'status': 'BLOCKED'}),
        )
        sentinel = b'{"q2": {"text": "keep", "actor": "cli", "t": 1}}\n'
        for tid, kwargs in cases:
            questions = ('q1', 'q2') if tid == 'done' else ('q1',)
            tdir = self.task(tid, questions=questions, **kwargs)
            (tdir / 'brief.answers.json').write_bytes(sentinel)
        known = {line.split('\t', 1)[0] for line in (self.plan / 'tasks.tsv').read_text().splitlines()}
        tasks_tsv = (self.plan / 'tasks.tsv').read_bytes()

        for tid, _ in cases:
            before = self.files(tid)
            result = self.cli('answer', self.plan, tid, 'q1', 'after')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('too late', result.stdout)
            self.assertIn(tid, result.stdout)
            follow = self.control_task(result.stdout)
            self.assertNotIn(follow, known)
            self.assertIn(tid, follow)
            self.assertIn('q1', follow)

            after = self.files(tid)
            self.assertEqual(set(after) - set(before), {'brief.late.json'}, tid)
            for name, body in before.items():
                self.assertEqual(after[name], body, '%s %s' % (tid, name))
            self.assertEqual((tdir_answers := self.plan / 'tasks' / tid / 'brief.answers.json').read_bytes(),
                             sentinel)
            late = self.answers(tid, 'brief.late.json')
            self.assertEqual(set(late), {'q1'})
            self.assertEqual(late['q1']['text'], 'after')
            self.assertEqual(late['q1']['actor'], 'cli')
            self.assertIsInstance(late['q1']['t'], (int, float))
            logged = [line for line in self.log() if line.get('task') == tid]
            self.assertEqual([(line['action'], line['key'], line['actor'], line['value'], line['late'])
                              for line in logged], [('answer', 'q1', 'cli', 'after', True)])
            recorded = [(body['qid'], body['actor'], body['late'])
                        for task, body in self.events('brief_answer') if task == tid]
            self.assertEqual(recorded, [('q1', 'cli', True)])
        self.assertEqual((self.plan / 'tasks.tsv').read_bytes(), tasks_tsv)

    def test_a_late_answer_names_the_row_and_prompt_control_add_needs(self):
        # `forge control add` queues a row that is already in tasks.tsv; the line carries
        # only the plan and the task id, so the prompt is what holds the answer.
        self.task('run', files='src/routes', status='RUNNING', questions=('q1',))
        result = self.cli('answer', self.plan, 'run', 'q1', 'use the routes module')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('too late', result.stdout)
        follow = self.control_task(result.stdout)
        self.assertEqual(follow, 'run-q1')
        prompt = 'tasks/%s/prompt.md' % follow
        self.assertIn('tasks.tsv', result.stdout)
        self.assertIn(prompt, result.stdout)
        self.assertIn('question: Question q1?', result.stdout)
        self.assertIn('answer: use the routes module', result.stdout)
        add = next(line for line in result.stdout.splitlines() if line.startswith('forge control add '))
        self.assertNotIn('use the routes module', add)
        row = next(line for line in result.stdout.splitlines() if line.startswith(follow + '\t'))
        self.assertEqual(row.split('\t'),
                         [follow, 'run', 'low', 'src/routes', 'sol', 'opus', 'Late answer to run q1'])

        def control_add(env):
            argv = shlex.split(add)
            return subprocess.run([sys.executable, CONTROL, *argv[2:]],
                                  capture_output=True, text=True, env=env)

        refused = control_add(self.env)
        self.assertEqual(refused.returncode, 2, refused.stderr)
        self.assertIn("task 'run-q1' is not in tasks.tsv", refused.stderr)

        with (self.plan / 'tasks.tsv').open('a', encoding='utf-8') as handle:
            handle.write(row + '\n')
        prompt_path = self.plan / 'tasks' / follow / 'prompt.md'
        prompt_path.parent.mkdir(parents=True)
        prompt_path.write_text('Question q1?\nuse the routes module\n')
        env = dict(self.env)
        env['FORGE_CONTROL_DOCTOR'] = 'true'
        queued = control_add(env)
        self.assertEqual(queued.returncode, 3, queued.stderr)
        self.assertNotIn('not in tasks.tsv', queued.stderr)
        self.assertIn('live', queued.stderr)

    def test_an_undeclared_merge_flags_only_open_questions_of_dependents_that_have_not_started(self):
        # `merged` is the marker a real merge writes; status can still say PASS.
        # `word` is the same fact recorded only as a MERGED status line.
        up = self.task('up', files='src/up.py', questions=('q1',), status='PASS', merged=True)
        (up / 'changes.diff').write_text(diff_of('src/up.py', 'notes/extra.md'))
        down = self.task('down', deps='up', files='src/down.py', questions=('q1', 'q2', 'q3'))
        (down / 'brief.answers.json').write_text(json.dumps({'q2': {'text': 'y', 'actor': 'cli', 't': 1}}) + '\n')
        (down / 'brief.parked.json').write_text(json.dumps({'questions': [question('p1', scope='wave')]}) + '\n')
        self.task('run', deps='up', status='RUNNING', questions=('q1',))
        self.task('far', deps='down', questions=('q1',))
        self.task('side', questions=('q1',))
        # BLOCKED is past starting, so an answer for it would be late and stale leaves it.
        self.task('blocked', deps='up', status='BLOCKED', questions=('q1',))
        word = self.task('word', files='src/word.py', questions=('q1',), status='MERGED')
        (word / 'changes.diff').write_text(diff_of('src/word.py', 'notes/other.md'))
        self.task('word-down', deps='word', questions=('q1',))

        untouched = ('up', 'run', 'far', 'side', 'blocked')
        before = {tid: self.files(tid) for tid in untouched}
        parked = (down / 'brief.parked.json').read_bytes()
        answered = (down / 'brief.answers.json').read_bytes()

        bad = self.cli('stale', self.plan, 'nope')
        self.assertEqual((bad.returncode, bad.stdout), (2, ''))
        self.assertIn('down', bad.stderr.split('valid tasks:', 1)[-1])
        missing = self.cli('stale', self.root / 'no-plan', 'up')
        self.assertEqual(missing.returncode, 3, missing.stderr)
        self.assertEqual(missing.stdout, '')

        result = self.cli('stale', self.plan, 'up')
        self.assertEqual(result.returncode, 0, result.stderr)
        word_result = self.cli('stale', self.plan, 'word')
        self.assertEqual(word_result.returncode, 0, word_result.stderr)
        self.assertEqual(result.stdout, 'down/q1\ndown/q3\n')
        self.assertEqual(word_result.stdout, 'word-down/q1\n')

        data = json.loads((down / 'brief.json').read_text())
        self.assertEqual(data['model'], 'sol')
        self.assertEqual(data['generated_at'], 't')
        by_id = {item['id']: item for item in data['questions']}
        self.assertIs(by_id['q1']['possibly_stale'], True)
        self.assertIs(by_id['q3']['possibly_stale'], True)
        self.assertNotIn('possibly_stale', by_id['q2'])
        self.assertEqual(by_id['q1']['text'], 'Question q1?')
        self.assertEqual((down / 'brief.answers.json').read_bytes(), answered)
        self.assertEqual((down / 'brief.parked.json').read_bytes(), parked)
        flagged = json.loads((self.plan / 'tasks' / 'word-down' / 'brief.json').read_text())
        self.assertIs(flagged['questions'][0]['possibly_stale'], True)
        for tid in untouched:
            self.assertEqual(self.files(tid), before[tid], tid)

        again = self.cli('stale', self.plan, 'up')
        self.assertEqual((again.returncode, again.stdout), (0, 'down/q1\ndown/q3\n'))
        repeated = json.loads((down / 'brief.json').read_text())
        self.assertIs(repeated['questions'][0]['possibly_stale'], True)

    def test_a_merge_within_declared_files_flags_nothing(self):
        up = self.task('up', files='src/up.py,src/note.py', questions=('q1',), status='PASS', merged=True)
        (up / 'changes.diff').write_text(diff_of('src/up.py', 'src/note.py'))
        down = self.task('down', deps='up', questions=('q1',))
        before = self.files('down')
        # Not merged yet: a diff with an undeclared file still flags nothing.
        waiting = self.task('waiting', files='src/waiting.py', status='RUNNING', questions=('q1',))
        (waiting / 'changes.diff').write_text(diff_of('src/waiting.py', 'notes/extra.md'))
        held = self.task('held', deps='waiting', questions=('q1',))
        held_before = self.files('held')

        result = self.cli('stale', self.plan, 'up')
        self.assertEqual((result.returncode, result.stdout), (0, ''))
        self.assertEqual(self.files('down'), before)
        self.assertNotIn('possibly_stale', (down / 'brief.json').read_text())
        skipped = self.cli('stale', self.plan, 'waiting')
        self.assertEqual((skipped.returncode, skipped.stdout), (0, ''))
        self.assertEqual(self.files('held'), held_before)

    def test_a_declared_directory_covers_paths_under_it(self):
        # check_drift: `src/routes` covers `src/routes/api.py`, and a trailing slash is the
        # same directory. A name that only shares the prefix string is still outside.
        up = self.task('up', files='src/routes', questions=('q1',), status='PASS', merged=True)
        (up / 'changes.diff').write_text(diff_of('src/routes/api.py'))
        down = self.task('down', deps='up', questions=('q1',))
        slash = self.task('slash', files='src/routes/', questions=('q1',), status='MERGED')
        (slash / 'changes.diff').write_text(diff_of('src/routes/api.py', 'src/routes/nested/x.py'))
        slash_down = self.task('slash-down', deps='slash', questions=('q1',))
        wide = self.task('wide', files='src/routes', questions=('q1',), status='MERGED')
        (wide / 'changes.diff').write_text(diff_of('src/routes/api.py', 'src/routes-extra.py'))
        wide_down = self.task('wide-down', deps='wide', questions=('q1',))

        inside = self.cli('stale', self.plan, 'up')
        self.assertEqual((inside.returncode, inside.stdout), (0, ''))
        self.assertNotIn('possibly_stale', (down / 'brief.json').read_text())
        slashed = self.cli('stale', self.plan, 'slash')
        self.assertEqual((slashed.returncode, slashed.stdout), (0, ''))
        self.assertNotIn('possibly_stale', (slash_down / 'brief.json').read_text())
        outside = self.cli('stale', self.plan, 'wide')
        self.assertEqual((outside.returncode, outside.stdout), (0, 'wide-down/q1\n'))
        flagged = json.loads((wide_down / 'brief.json').read_text())
        self.assertIs(flagged['questions'][0]['possibly_stale'], True)

    def test_file_input_reads_stdin_strips_controls_and_caps_at_4kb(self):
        self.task('a', questions=('q1', 'q2'))
        answers = self.plan / 'tasks' / 'a' / 'brief.answers.json'
        controls = self.root / 'controls.txt'
        controls.write_text('\x00\n\t')
        empty = self.cli('answer', self.plan, 'a', 'q1', '--file', str(controls))
        self.assertEqual(empty.returncode, 2, empty.stderr)
        self.assertEqual(empty.stdout, '')
        self.assertFalse(answers.exists())
        self.assertFalse((self.plan / 'control.log').exists())

        missing = self.cli('answer', self.plan, 'a', 'q1', '--file', str(self.root / 'missing.txt'))
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertFalse(answers.exists())

        payload = 'A\x00B\n' + ('C' * 5000)
        capped = self.cli('answer', self.plan, 'a', 'q1', '--file', '-', input=payload)
        self.assertEqual(capped.returncode, 0, capped.stderr)
        stored = self.answers('a')['q1']['text']
        self.assertEqual(stored, 'AB' + ('C' * (ANSWER_CAP - 2)))
        self.assertEqual(len(stored.encode('utf-8')), ANSWER_CAP)
        self.assertNotIn('\x00', stored)
        self.assertNotIn('\n', stored)

        path = self.root / 'answer.txt'
        path.write_text('use\x07 postgres\n')
        filed = self.cli('answer', self.plan, 'a', 'q2', '--file', str(path))
        self.assertEqual(filed.returncode, 0, filed.stderr)
        data = self.answers('a')
        self.assertEqual(data['q2']['text'], 'use postgres')
        self.assertEqual(data['q1']['text'], stored)
        self.assertFalse((self.plan / 'tasks' / 'a' / 'brief.late.json').exists())


    def test_with_the_diff_gone_only_this_runs_attempt_events_name_the_changed_files(self):
        # The brief package builds the question writer's prompt, so its one read of the store is
        # scoped to this run's own attempt file lists; another run's, or a plan with no run_id, give nothing.
        import forge_brief.answers as answers
        self.task('a')
        db = forge_dataset.connect()
        repo = forge_dataset.repo_id(self.root / 'repo')
        for rid, run_id, files in (('r1', 'this-run', ['src/mine.py']), ('r2', 'other-run', ['src/theirs.py'])):
            db.execute('INSERT INTO events (rid, kind, ts, src, repo, run_id, task, data) VALUES (?,?,?,?,?,?,?,?)',
                       (rid, 'attempt', 1.0, 'live', repo, run_id, 'a', json.dumps({'changed_files': files})))
        db.commit(); db.close()
        self.assertEqual(answers.changed_files(self.plan, 'a'), set())
        (self.plan / 'run_id').write_text('this-run\n')
        self.assertEqual(answers.changed_files(self.plan, 'a'), {'src/mine.py'})


if __name__ == '__main__':
    unittest.main()
