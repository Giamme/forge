"""Brief mode: the question writer, brief.json, parked questions, `forge brief write|show`.

Dispatch goes through the fake CLI harness, borrowed the way test_side does. The fake
tells tasks apart by a TASK_<x> marker in the prompt, so the plans here have no
dependencies unless a test is about the dependency text itself.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest

import test_forge

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_brief  # noqa: E402
import forge_dataset  # noqa: E402

CLI = str(ROOT / 'scripts' / 'forge-brief.py')
NONE = '{"questions": []}'


def reply(*questions):
    return json.dumps({'questions': list(questions)})


def question(text='Which one?', scope='task', must_answer=False, **extra):
    base = {'text': text, 'why': 'it changes the files', 'options': ['x', 'y'], 'default': 'x',
            'scope': scope, 'must_answer': must_answer}
    base.update(extra)
    return base


def says(text):
    """A scenario entry: the side dispatch's reply is exactly `text`."""
    return {'findings': text, 'verdict': 'none'}


class BriefCase(unittest.TestCase):
    git = test_forge.ForgeTests.git
    scenario = test_forge.ForgeTests.scenario
    calls = test_forge.ForgeTests.calls

    def setUp(self):
        test_forge.ForgeTests.setUp(self)
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        for name in ('FORGE_RETRO', 'FORGE_BRIEF', 'FORGE_SUMMARIES', 'FORGE_REGISTRY'):
            os.environ.pop(name, None)
            self.env.pop(name, None)
        self.plan = self.root / 'plan'
        self.plan.mkdir()
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        (self.plan / 'brief').write_text('advisory\n')
        (self.plan / 'brief_model').write_text('sol\n')
        rows = ''.join('%s\t-\tlow\t%s.txt\tsol\topus\tTitle of %s\n' % (i, i, i) for i in 'abc')
        (self.plan / 'tasks.tsv').write_text(rows)
        for i in 'abc':
            tdir = self.plan / 'tasks' / i
            tdir.mkdir(parents=True)
            (tdir / 'prompt.md').write_text('Implement the change. TASK_%s\n' % i)
        self.cache = self.root / 'capability-cache'
        self.cache.mkdir()
        data_home = self.root / 'data-home'
        data_home.mkdir()
        os.environ['FORGE_DATA_HOME'] = str(data_home)
        self.env['FORGE_DATA_HOME'] = str(data_home)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def arm(self, table):
        self.scenario(table)
        os.environ.update(self.env)
        os.environ['FORGE_CAPABILITY_CACHE'] = str(self.cache)

    def all_say(self, text=NONE):
        self.arm({'*.qa.*': says(text)})

    def brief(self, task):
        return json.loads((self.plan / 'tasks' / task / 'brief.json').read_text())

    def parked(self, task):
        return json.loads((self.plan / 'tasks' / task / 'brief.parked.json').read_text())

    def has_brief(self, task):
        return (self.plan / 'tasks' / task / 'brief.json').exists()

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

    def cli(self, *args):
        return subprocess.run([sys.executable, CLI, *map(str, args)], capture_output=True, text=True, env=self.env)


class WriteTests(BriefCase):
    def test_nothing_to_ask_writes_empty_questions_for_every_task(self):
        self.all_say()
        results = forge_brief.write(self.plan)
        self.assertEqual([r['status'] for r in results], ['written'] * 3)
        self.assertEqual(self.calls(), ['qa'] * 3)
        for task in 'abc':
            data = self.brief(task)
            self.assertEqual(data['questions'], [])
            self.assertEqual(data['model'], 'sol')
            self.assertTrue(data['generated_at'])
            self.assertNotIn('error', data)
            self.assertFalse((self.plan / 'tasks' / task / 'brief.parked.json').exists())
        self.assertEqual(self.events('brief_question'), [])

    def test_task_scope_questions_are_written_with_ids(self):
        self.arm({'a.qa.*': says(reply(question('First?'), question('Second?'))),
                  '*.qa.*': says(NONE)})
        forge_brief.write(self.plan, task='a')
        self.assertEqual(self.calls(), ['qa'])
        data = self.brief('a')
        self.assertEqual([q['id'] for q in data['questions']], ['q1', 'q2'])
        self.assertEqual([q['text'] for q in data['questions']], ['First?', 'Second?'])
        self.assertEqual(data['questions'][0], {
            'id': 'q1', 'text': 'First?', 'why': 'it changes the files', 'options': ['x', 'y'],
            'default': 'x', 'scope': 'task', 'must_answer': False})
        self.assertFalse(self.has_brief('b'))
        self.assertFalse((self.plan / 'tasks' / 'a' / 'brief.parked.json').exists())

    def test_wider_scopes_are_parked_whatever_brief_scope_says(self):
        (self.plan / 'brief_scope').write_text('plan\n')
        self.arm({'a.qa.*': says(reply(question('Here', 'task'), question('Wave', 'wave'),
                                       question('Plan', 'plan'))),
                  '*.qa.*': says(NONE)})
        forge_brief.write(self.plan, task='a')
        self.assertEqual([(q['id'], q['scope']) for q in self.brief('a')['questions']], [('q1', 'task')])
        parked = self.parked('a')
        self.assertEqual([(q['id'], q['scope'], q['text']) for q in parked['questions']],
                         [('q2', 'wave', 'Wave'), ('q3', 'plan', 'Plan')])
        self.assertEqual(parked['model'], 'sol')
        self.assertTrue(parked['generated_at'])

    def test_more_than_three_questions_keep_three(self):
        self.arm({'*.qa.*': says(reply(*[question('Q%d?' % n) for n in range(1, 5)]))})
        forge_brief.write(self.plan, task='a')
        self.assertEqual([q['text'] for q in self.brief('a')['questions']], ['Q1?', 'Q2?', 'Q3?'])

    def test_must_answer_is_kept_only_under_gated(self):
        self.arm({'*.qa.*': says(reply(question('Hard?', must_answer=True)))})
        forge_brief.write(self.plan, task='a')
        self.assertFalse(self.brief('a')['questions'][0]['must_answer'])
        (self.plan / 'brief').write_text('strict\n')
        forge_brief.write(self.plan, task='a', force=True)
        self.assertFalse(self.brief('a')['questions'][0]['must_answer'])
        (self.plan / 'brief').write_text('gated\n')
        forge_brief.write(self.plan, task='a', force=True)
        self.assertTrue(self.brief('a')['questions'][0]['must_answer'])

    def test_a_fenced_reply_is_accepted(self):
        self.arm({'*.qa.*': says('```json\n' + reply(question('Fenced?')) + '\n```')})
        forge_brief.write(self.plan, task='a')
        self.assertEqual(self.brief('a')['questions'][0]['text'], 'Fenced?')
        self.assertEqual(self.calls(), ['qa'])

    def test_a_reply_that_is_not_json_is_retried_once_then_recorded_as_an_error(self):
        self.arm({'a.qa.*': says('I have no questions, sorry'), '*.qa.*': says(NONE)})
        results = forge_brief.write(self.plan, task='a')
        self.assertEqual(self.calls(), ['qa', 'qa'])
        data = self.brief('a')
        self.assertEqual(data['questions'], [])
        self.assertEqual(data['error'], 'reply is not valid JSON')
        self.assertEqual((results[0]['status'], results[0]['reason']), ('error', 'reply is not valid JSON'))
        retry = (self.plan / 'side' / 'brief' / 'a' / '2' / 'prompt.txt').read_text()
        first = (self.plan / 'side' / 'brief' / 'a' / '1' / 'prompt.txt').read_text()
        self.assertTrue(retry.startswith(first))
        self.assertIn('not usable', retry)
        self.assertNotIn('not usable', first)

    def test_a_wrong_shape_is_retried_and_a_good_second_reply_is_used(self):
        self.arm({'a.qa.1': says('{"questions": "none"}'),
                  'a.qa.2': says(reply(question('Second try?'))),
                  '*.qa.*': says(NONE)})
        forge_brief.write(self.plan, task='a')
        self.assertEqual(self.calls(), ['qa', 'qa'])
        data = self.brief('a')
        self.assertNotIn('error', data)
        self.assertEqual(data['questions'][0]['text'], 'Second try?')

    def test_each_wrong_shape_is_refused(self):
        for bad in ('{"questions": [{"why": "no text"}]}', '{"questions": [1]}', '[]',
                    reply(question(scope='galaxy')), reply(question(must_answer='yes')),
                    reply(question(options='x'))):
            questions, error = forge_brief.parse_reply(bad)
            self.assertIsNone(questions, bad)
            self.assertTrue(error, bad)
        # Every key of the format is required: a question with no default leaves a guess.
        for key in ('text', 'why', 'options', 'default', 'scope', 'must_answer'):
            partial = question()
            del partial[key]
            questions, error = forge_brief.parse_reply(reply(partial))
            self.assertIsNone(questions, key)
            self.assertIn(key, error)
        for key in ('why', 'default'):
            questions, error = forge_brief.parse_reply(reply(question(**{key: ' '})))
            self.assertIsNone(questions, key)
        questions, error = forge_brief.parse_reply(reply(question(options=[])))
        self.assertEqual((error, questions[0]['options']), (None, []))

    def test_an_incomplete_question_is_retried_like_any_bad_reply(self):
        self.arm({'a.qa.1': says('{"questions": [{"text": "Which database?"}]}'),
                  'a.qa.2': says(reply(question('Which database?'))),
                  '*.qa.*': says(NONE)})
        forge_brief.write(self.plan, task='a')
        self.assertEqual(self.calls(), ['qa', 'qa'])
        data = self.brief('a')
        self.assertNotIn('error', data)
        self.assertEqual(data['questions'][0]['default'], 'x')

    def test_a_retry_the_cap_refuses_still_records_the_error(self):
        for number in range(1, 10):
            (self.plan / 'side' / 'brief' / 'z' / str(number)).mkdir(parents=True)
        self.arm({'*.qa.*': says('not json')})
        results = forge_brief.write(self.plan, task='a')
        self.assertEqual(self.calls(), ['qa'])   # the first reply used the last slot
        data = self.brief('a')
        self.assertEqual(data['questions'], [])
        self.assertIn('reply is not valid JSON', data['error'])
        self.assertIn('cap', data['error'])
        self.assertEqual(results[0]['status'], 'error')

    def test_a_failed_dispatch_still_leaves_a_brief_json(self):
        self.arm({'*.qa.*': {'rc': 1, 'findings': 'boom', 'verdict': 'none'}})
        results = forge_brief.write(self.plan, task='a')
        self.assertEqual(self.calls(), ['qa'])   # a dispatch failure is not a bad reply: no retry
        self.assertEqual(self.brief('a')['questions'], [])
        self.assertEqual(self.brief('a')['error'], 'dispatch failed: error')
        self.assertEqual(results[0]['status'], 'error')

    def test_question_events(self):
        self.arm({'a.qa.*': says(reply(question('Here', must_answer=True), question('Wave', 'wave'))),
                  '*.qa.*': says(NONE)})
        (self.plan / 'brief').write_text('gated\n')
        forge_brief.write(self.plan, task='a')
        found = sorted((body['qid'], body['scope'], body['must_answer'], body['parked'], body['task'])
                       for _, body in self.events('brief_question'))
        self.assertEqual(found, [('q1', 'task', True, False, 'a'), ('q2', 'wave', False, True, 'a')])

    def test_the_prompt_carries_the_task_its_neighbours_and_capped_dependencies(self):
        (self.plan / 'tasks.tsv').write_text(
            'a\t-\tlow\ta.txt\tsol\topus\tTitle of a\n'
            'b\ta\tlow\tb.txt\tsol\topus\tTitle of b\n')
        (self.plan / 'tasks' / 'a' / 'prompt.md').write_text('DEP_HEAD ' + 'x' * 9000 + ' DEP_TAIL TASK_a\n')
        (self.plan / 'tasks' / 'b' / 'approach.md').write_text('Use the approach APPROACH_SENTINEL.\n')
        self.all_say()
        forge_brief.write(self.plan, task='b')
        prompt = (self.plan / 'side' / 'brief' / 'b' / '1' / 'prompt.txt').read_text()
        self.assertIn('TASK_b', prompt)
        self.assertIn('APPROACH_SENTINEL', prompt)
        self.assertIn('b\ta\tlow\tb.txt\tsol\topus\tTitle of b', prompt)
        self.assertIn('- a: Title of a', prompt)
        self.assertIn('- b: Title of b', prompt)
        self.assertIn('DEP_HEAD', prompt)
        self.assertNotIn('DEP_TAIL', prompt)
        self.assertIn('(capped)', prompt)
        self.assertIn(str(self.repo), prompt)
        self.assertIn('must not write', prompt)
        self.assertIn('At most 3 questions', prompt)
        self.assertLess(len(prompt), 12000)

    def test_the_task_documents_are_never_truncated(self):
        (self.plan / 'tasks' / 'a' / 'prompt.md').write_text('x' * 40000 + ' PROMPT_TAIL TASK_a\n')
        (self.plan / 'tasks' / 'a' / 'approach.md').write_text('y' * 40000 + ' APPROACH_TAIL\n')
        self.all_say()
        forge_brief.write(self.plan, task='a')
        prompt = (self.plan / 'side' / 'brief' / 'a' / '1' / 'prompt.txt').read_text()
        self.assertIn('PROMPT_TAIL', prompt)
        self.assertIn('APPROACH_TAIL', prompt)
        self.assertNotIn('(capped)', prompt)


class RefusalTests(BriefCase):
    def refused(self, reason):
        self.all_say()
        with self.assertRaises(forge_brief.BriefOff) as caught:
            forge_brief.write(self.plan)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.plan / 'side').exists())
        for task in 'abc':
            self.assertFalse(self.has_brief(task))
        result = self.cli('write', self.plan)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(self.calls(), [])
        return result.stderr

    def test_brief_off_dispatches_nothing(self):
        (self.plan / 'brief').unlink()
        self.assertIn('off for this plan', self.refused('off'))

    def test_env_off_dispatches_nothing(self):
        os.environ['FORGE_BRIEF'] = 'off'
        self.env['FORGE_BRIEF'] = 'off'
        self.assertIn('FORGE_BRIEF=off', self.refused('env'))

    def test_never_send_dispatches_nothing(self):
        (self.repo / '.forge').mkdir()
        (self.repo / '.forge' / 'decide.json').write_text('{"privacy": "never-send"}\n')
        self.assertIn('never-send', self.refused('never-send'))

    def test_no_model_dispatches_nothing(self):
        (self.plan / 'brief_model').unlink()
        self.assertIn('no brief model', self.refused('no-model'))

    def test_a_task_the_cap_refuses_is_skipped_with_its_reason(self):
        # Ten dispatches already made is the whole cap for three tasks' worth of floor.
        for number in range(1, 11):
            (self.plan / 'side' / 'brief' / 'z' / str(number)).mkdir(parents=True)
        self.all_say()
        results = forge_brief.write(self.plan)
        self.assertEqual([(r['task'], r['status'], r['reason']) for r in results],
                         [(i, 'skipped', 'cap') for i in 'abc'])
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.has_brief('a'))


class SelectionTests(BriefCase):
    def test_an_existing_brief_json_is_not_redone_without_force(self):
        (self.plan / 'tasks' / 'a' / 'brief.json').write_text('{"questions": [], "model": "kept"}\n')
        self.all_say(reply(question('Fresh?')))
        results = forge_brief.write(self.plan)
        self.assertEqual(self.calls(), ['qa', 'qa'])
        self.assertEqual(self.brief('a')['model'], 'kept')
        self.assertEqual([(r['task'], r['status'], r['reason']) for r in results if r['task'] == 'a'],
                         [('a', 'skipped', 'exists')])
        forge_brief.write(self.plan, task='a')
        self.assertEqual(self.calls(), ['qa', 'qa'])
        self.assertEqual(self.brief('a')['model'], 'kept')
        forge_brief.write(self.plan, task='a', force=True)
        self.assertEqual(self.calls(), ['qa'] * 3)
        self.assertEqual(self.brief('a')['questions'][0]['text'], 'Fresh?')

    def test_force_removes_a_stale_parked_file(self):
        self.arm({'*.qa.*': says(reply(question('Wide', 'plan')))})
        forge_brief.write(self.plan, task='a')
        self.assertTrue((self.plan / 'tasks' / 'a' / 'brief.parked.json').exists())
        self.arm({'*.qa.*': says(NONE)})
        forge_brief.write(self.plan, task='a', force=True)
        self.assertFalse((self.plan / 'tasks' / 'a' / 'brief.parked.json').exists())

    def test_a_forced_rewrite_sets_old_answers_aside(self):
        self.arm({'*.qa.*': says(reply(question('Which database?')))})
        forge_brief.write(self.plan, task='a')
        tdir = self.plan / 'tasks' / 'a'
        (tdir / 'brief.answers.json').write_text(json.dumps({'q1': {'text': 'PostgreSQL', 'actor': 'cli', 't': 1}}))
        (tdir / 'brief.late.json').write_text(json.dumps({'q1': {'text': 'late', 'actor': 'cli', 't': 2}}))
        self.arm({'*.qa.*': says(reply(question('Which login protocol?')))})
        forge_brief.write(self.plan, task='a', force=True)
        self.assertEqual(self.brief('a')['questions'][0]['text'], 'Which login protocol?')
        self.assertFalse((tdir / 'brief.answers.json').exists())
        self.assertFalse((tdir / 'brief.late.json').exists())
        found = forge_brief.collect(self.plan, task='a')
        self.assertEqual([(r['id'], r['state']) for r in found['questions']], [('q1', 'open')])
        kept = list((tdir / 'brief.history').glob('*/brief.answers.json'))
        self.assertEqual(len(kept), 1)
        self.assertEqual(json.loads(kept[0].read_text())['q1']['text'], 'PostgreSQL')
        old = json.loads((kept[0].parent / 'brief.json').read_text())
        self.assertEqual(old['questions'][0]['text'], 'Which database?')

    def test_two_writers_on_one_task_dispatch_once_and_do_not_overwrite(self):
        import threading
        import time
        self.all_say(reply(question('Once?')))
        real = forge_brief._ask
        entered = []

        def slow(*args, **kwargs):
            entered.append(1)
            time.sleep(0.6)
            return real(*args, **kwargs)
        forge_brief._ask = slow
        self.addCleanup(setattr, forge_brief, '_ask', real)
        outcomes = []
        threads = [threading.Thread(target=lambda: outcomes.append(forge_brief.write(self.plan, task='a')))
                   for _ in range(2)]
        for thread in threads:
            thread.start()
            time.sleep(0.2)
        for thread in threads:
            thread.join()
        self.assertEqual(len(entered), 1)
        self.assertEqual(self.calls(), ['qa'])
        self.assertEqual(sorted(out[0]['status'] for out in outcomes), ['skipped', 'written'])
        self.assertEqual(self.brief('a')['questions'][0]['text'], 'Once?')

    def test_parallel_workers_still_dispatch_in_wave_order(self):
        import time
        (self.plan / 'waves.tsv').write_text('1\ta\n2\tb\n2\tc\n')
        self.all_say()
        real_build, real_run = forge_brief.build_prompt, forge_brief.forge_side.run
        order = []

        def slow_build(plan, task, tasks, repo):
            if task['id'] == 'a':
                time.sleep(0.8)
            return real_build(plan, task, tasks, repo)

        def spy(kind, plan, task, *args, **kwargs):
            order.append(task)
            return real_run(kind, plan, task, *args, **kwargs)
        forge_brief.build_prompt = slow_build
        forge_brief.forge_side.run = spy
        self.addCleanup(setattr, forge_brief, 'build_prompt', real_build)
        self.addCleanup(setattr, forge_brief.forge_side, 'run', real_run)
        forge_brief.write(self.plan, jobs=3)
        self.assertEqual(order, ['a', 'b', 'c'])

    def test_tasks_go_in_wave_order_then_file_order(self):
        (self.plan / 'waves.tsv').write_text('1\tc\n2\ta\n2\tb\n')
        events = self.root / 'events'
        self.env['EVENTS'] = str(events)
        self.all_say()
        results = forge_brief.write(self.plan, jobs=1)
        self.assertEqual([r['task'] for r in results], ['c', 'a', 'b'])
        self.assertEqual([line.split()[0] for line in events.read_text().splitlines()],
                         ['c.txt', 'a.txt', 'b.txt'])

    def test_a_task_missing_from_waves_goes_last_in_file_order(self):
        (self.plan / 'waves.tsv').write_text('1\tb\n')
        events = self.root / 'events'
        self.env['EVENTS'] = str(events)
        self.all_say()
        forge_brief.write(self.plan, jobs=1)
        self.assertEqual([line.split()[0] for line in events.read_text().splitlines()],
                         ['b.txt', 'a.txt', 'c.txt'])

    def test_unknown_task_is_refused_before_any_dispatch(self):
        self.all_say()
        with self.assertRaises(forge_brief.UnknownTask):
            forge_brief.write(self.plan, task='nope')
        self.assertEqual(self.calls(), [])
        result = self.cli('write', self.plan, '--task', 'nope')
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_write_through_the_cli_prints_one_line_per_task(self):
        self.arm({'a.qa.*': says(reply(question('Q?'), question('W?', 'wave'))), '*.qa.*': says(NONE)})
        result = self.cli('write', self.plan, '--jobs', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ['a: 1 question(s), 1 parked', 'b: 0 question(s)', 'c: 0 question(s)'])
        again = self.cli('write', self.plan)
        self.assertEqual(again.stdout.splitlines(), ['%s: skipped (exists)' % i for i in 'abc'])
        self.assertEqual(self.cli('write', self.plan, '--jobs', '0').returncode, 2)


class ShowTests(BriefCase):
    def put(self, task, name, data):
        (self.plan / 'tasks' / task / name).write_text(json.dumps(data))

    def setUp(self):
        super().setUp()
        (self.plan / 'waves.tsv').write_text('1\ta\n2\tb\n3\tc\n')

        def q(qid, must=False, **extra):
            return dict({'id': qid, 'text': 'Question %s' % qid, 'why': '', 'options': [], 'default': 'd',
                         'scope': 'task', 'must_answer': must}, **extra)
        self.put('a', 'brief.json', {'questions': [q('q1'), q('q2', True)], 'model': 'sol', 'generated_at': 't'})
        self.put('a', 'brief.answers.json', {'q2': {'text': 'Use y', 'actor': 'ui', 't': 5}})
        self.put('a', 'brief.parked.json', {'questions': [q('q3', scope='plan')], 'model': 'sol', 'generated_at': 't'})
        self.put('b', 'brief.json', {'questions': [q('q1', True), q('q2'), q('q3', possibly_stale=True)],
                                     'model': 'sol', 'generated_at': 't'})
        self.put('b', 'brief.late.json', {'q2': {'text': 'Too late', 'actor': 'cli', 't': 9}})
        self.put('c', 'brief.json', {'questions': [], 'error': 'dispatch failed: timeout', 'model': 'sol',
                                     'generated_at': 't'})

    def test_json_lists_every_state_flagged_open_first(self):
        result = self.cli('show', self.plan, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        found = json.loads(result.stdout)
        self.assertEqual([(r['task'], r['id'], r['state'], r['must_answer']) for r in found['questions']], [
            ('b', 'q1', 'open', True),
            ('a', 'q1', 'open', False),
            ('b', 'q3', 'possibly_stale', False),
            ('a', 'q2', 'answered', True),
            ('b', 'q2', 'late', False),
        ])
        by = {(r['task'], r['id']): r for r in found['questions']}
        self.assertEqual(by[('a', 'q2')]['answer']['text'], 'Use y')
        self.assertEqual(by[('b', 'q2')]['answer']['text'], 'Too late')
        self.assertEqual([(r['task'], r['id'], r['scope']) for r in found['parked']], [('a', 'q3', 'plan')])
        self.assertEqual(found['errors'], [{'task': 'c', 'error': 'dispatch failed: timeout'}])

    def test_an_unanswered_flagged_question_that_is_stale_still_comes_first(self):
        data = json.loads((self.plan / 'tasks' / 'b' / 'brief.json').read_text())
        data['questions'][1]['must_answer'] = True
        data['questions'][1]['possibly_stale'] = True
        self.put('b', 'brief.json', data)
        (self.plan / 'tasks' / 'b' / 'brief.late.json').unlink()
        found = json.loads(self.cli('show', self.plan, '--json').stdout)
        self.assertEqual([(r['task'], r['id'], r['state']) for r in found['questions'][:2]],
                         [('b', 'q1', 'open'), ('b', 'q2', 'possibly_stale')])

    def test_task_filter(self):
        found = json.loads(self.cli('show', self.plan, '--task', 'a', '--json').stdout)
        self.assertEqual({r['task'] for r in found['questions']}, {'a'})
        self.assertEqual(self.cli('show', self.plan, '--task', 'nope').returncode, 2)

    def test_text_lists_states_and_the_parked_questions_apart(self):
        text = self.cli('show', self.plan).stdout
        self.assertLess(text.index('b/q1'), text.index('a/q1'))
        self.assertIn('b/q1  open [must answer]', text)
        self.assertIn('a/q2  answered [must answer]', text)
        self.assertIn('answer: Use y (ui)', text)
        self.assertIn('b/q3  possibly_stale', text)
        self.assertIn('b/q2  late', text)
        parked_at = text.index('Parked (wider scope):')
        self.assertGreater(text.index('a/q3'), parked_at)
        self.assertLess(text.index('b/q2'), parked_at)
        self.assertIn('c: the question writer failed: dispatch failed: timeout', text)

    def test_show_needs_a_plan_and_brief_not_off(self):
        missing = self.cli('show', self.root / 'no-plan')
        self.assertEqual((missing.returncode, missing.stdout), (3, ''))
        self.assertIn('no-plan', missing.stderr)
        (self.plan / 'brief').unlink()
        off = self.cli('show', self.plan)
        self.assertEqual(off.returncode, 3, off.stderr)
        self.assertIn('off for this plan', off.stderr)


if __name__ == '__main__':
    unittest.main()
