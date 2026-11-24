"""scripts/forge-planner.py and recursive decomposition (#153): `subtasks` and `reason` on a task of
a whole-plan reply, the `later` stages of a goal too large for one plan, and the question asked
about one task of a plan that already exists (`node-prompt`, `node-parse`).

The fixtures are test_planner.py's, borrowed, not inherited.
"""
import json

import test_planner as planner_fixtures

REQUEST = planner_fixtures.REQUEST
CRITERIA = planner_fixtures.CRITERIA
task = planner_fixtures.task
reply = planner_fixtures.reply


def part(tid, files, deps=(), **more):
    return dict(task(tid, deps, 'low', files, title='Part ' + tid), **more)


def whole(**more):
    """One task that owns src/ and docs/."""
    return dict(task('whole', files=('src/', 'docs/')), **more)


class Case(planner_fixtures.PlannerCase):
    def parse(self, text):
        (self.plan / 'request.md').write_text(REQUEST)
        table = self.plan / 'tasks.tsv'
        if table.exists():
            table.unlink()            # a plan is never overwritten: the table is discarded before a new reply is read
        path = self.dir / 'reply.txt'
        path.write_text(text)
        return self.run_planner('parse', '--plan', self.plan, '--reply', path)

    def stored(self, name, tid='whole'):
        path = self.plan / 'tasks' / tid / name
        return path.read_text() if path.exists() else None


class SubtasksTests(Case):
    def test_a_tasks_subtasks_are_stored_as_a_proposal_through_every_level(self):
        inner = [part('one', ['src/a/one.py']), part('two', ['src/a/two.py'], deps=['one'])]
        text = reply([whole(reason='two halves', subtasks=[
            part('a', ['src/a/'], reason='two steps', subtasks=inner), part('b', ['docs/'], deps=['a'], risk='high')])])
        r = self.parse(text)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        # The table has the task only: its subtasks are made by `plan`, within the run's limits.
        self.assertEqual((self.plan / 'tasks.tsv').read_text().splitlines()[1:],
                         ['whole\t-\tmedium\tsrc/,docs/\t-\t-\tParse the new config section'])
        proposal = json.loads(self.stored('split.proposal.json'))
        self.assertEqual(proposal['reason'], 'two halves')
        first, second = proposal['tasks']
        self.assertEqual((first['id'], first['files'], first['deps'], first['risk']), ('a', ['src/a/'], [], None))
        self.assertEqual((second['id'], second['deps'], second['risk']), ('b', ['a'], 'high'))
        self.assertEqual(first['prompt'], 'Do a.' + CRITERIA)
        self.assertEqual((first['split']['reason'], [t['id'] for t in first['split']['tasks']]), ('two steps', ['one', 'two']))
        self.assertNotIn('split', second)
        self.assertIsNone(self.stored('split.reason'))

    def test_a_task_kept_whole_keeps_its_reason_and_a_new_reply_replaces_what_the_last_one_said(self):
        self.assertEqual(self.parse(reply([whole(reason='  one coherent change  ')])).returncode, 0)
        self.assertEqual(self.stored('split.reason'), 'one coherent change\n')
        self.assertIsNone(self.stored('split.proposal.json'))
        split = reply([whole(subtasks=[part('a', ['src/x.py']), part('b', ['docs/x.md'])])])
        self.assertEqual(self.parse(split).returncode, 0)
        self.assertIsNone(self.stored('split.reason'))
        self.assertEqual(json.loads(self.stored('split.proposal.json'))['reason'], '')
        self.assertEqual(self.parse(reply([whole()])).returncode, 0)
        self.assertIsNone(self.stored('split.reason'))
        self.assertIsNone(self.stored('split.proposal.json'))

    def test_a_reply_whose_subtasks_break_a_rule_is_refused_whole(self):
        two = [part('a', ['src/x.py']), part('b', ['docs/x.md'])]
        deep = part('l6', ['src/x.py'])
        for level in range(5, 0, -1):
            deep = part('l%d' % level, ['src/'], subtasks=[deep, part('s%d' % level, ['src/y%d.py' % level])])
        cases = (
            ('one subtask', [part('a', ['src/x.py'])], 'subtasks must be a list of 2 to 20 objects (1)'),
            ('not a list', {'a': 1}, 'subtasks must be a list of 2 to 20 objects;'),
            ('a file outside the task', [part('a', ['src/x.py']), part('b', ['lib/x.py'])],
             "task whole: subtask b: file 'lib/x.py' is outside its parent's files (src/, docs/)"),
            ('a dep on a task of the plan', [part('a', ['src/x.py'], deps=['other']), part('b', ['docs/x.md'])],
             "task whole: among its subtasks: task a: unknown dep 'other'"),
            ('a cycle', [part('a', ['src/x.py'], deps=['b']), part('b', ['docs/x.md'], deps=['a'])],
             'task whole: among its subtasks: dependency cycle: a -> b -> a'),
            ('two subtasks with one id', [part('a', ['src/x.py']), part('a', ['docs/x.md'])], 'duplicate'),
            ('a subtask with no criteria', [part('a', ['src/x.py'], prompt='Do it.'), part('b', ['docs/x.md'])],
             'prompt has no "## Acceptance criteria" heading'),
            ('an unknown key', [dict(part('a', ['src/x.py']), model='opus'), part('b', ['docs/x.md'])], 'model'),
            ('nested too deep', [part('a', ['src/'], subtasks=[deep, part('z', ['src/z.py'])]), part('b', ['docs/x.md'])],
             'subtasks nest more than 6 levels deep'),
        )
        for what, subtasks, said in cases:
            with self.subTest(what):
                self.wipe()
                r = self.parse(reply([whole(subtasks=subtasks), task('other', files=['other.py'])]))
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn(said, r.stdout)
                self.assertEqual(self.files(), ['planner.refused', 'request.md'])
        for what, reason in (('two lines', 'one\ntwo'), ('too long', 'x' * 501), ('not a string', 7)):
            with self.subTest(what):
                self.wipe()
                r = self.parse(reply([whole(reason=reason, subtasks=two)]))
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn('reason is not one line of at most 500 characters', r.stdout)

    def test_the_prompt_says_when_to_split_and_that_it_is_never_required(self):
        r = self.run_planner('prompt', '--plan', self.plan, '--goal-file', self.goal)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = (self.plan / 'planner.prompt').read_text()
        for needle in ('`subtasks`', "inside its parent's `files`", 'sibling subtasks', '`reason`', '`later`',
                       'only when the one before has merged'):
            self.assertIn(needle, text)


class LaterTests(Case):
    def staged(self, later):
        body = {'tasks': [task()], 'later': later}
        return self.parse('```forge-plan\n%s\n```\n' % json.dumps(body))

    def test_the_stages_after_this_plan_are_kept_in_order_and_dropped_by_a_reply_without_them(self):
        r = self.staged([{'title': ' The API ', 'goal': 'Serve the limits.\n\n- one route'}, {'title': 'The UI', 'goal': 'Show them.'}])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads((self.plan / 'later.json').read_text()),
                         {'later': [{'title': 'The API', 'goal': 'Serve the limits.\n\n- one route'},
                                    {'title': 'The UI', 'goal': 'Show them.'}]})
        self.assertEqual(self.parse(reply([task()])).returncode, 0)
        self.assertFalse((self.plan / 'later.json').exists())

    def test_what_is_not_a_list_of_stages_is_refused(self):
        stage = {'title': 'Next', 'goal': 'More.'}
        cases = (
            ('an empty list', [], 'later must be a list of 1 to 10 objects (0)'),
            ('too many', [stage] * 11, 'later must be a list of 1 to 10 objects (11)'),
            ('no goal', [{'title': 'Next'}], 'later 1'),
            ('an empty goal', [stage, {'title': 'Then', 'goal': '  '}], 'later 2: goal is not a non-empty text'),
            ('a title of two lines', [{'title': 'a\nb', 'goal': 'More.'}], 'later 1: title is not one line'),
            ('one more key', [dict(stage, tasks=[])], 'later 1'),
        )
        for what, later, said in cases:
            with self.subTest(what):
                self.wipe()
                r = self.staged(later)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn(said, r.stdout)
                self.assertEqual(self.files(), ['planner.refused', 'request.md'])


class NodeTests(Case):
    """One task of a plan that exists: the question to a planner and the strict parse of the answer."""

    def setUp(self):
        super().setUp()
        (self.plan / 'tasks/whole').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('whole\t-\thigh\tsrc/,docs/\tsol\topus\tThe whole of it\n'
                                             'other\twhole\tlow\tother.py\tsol\topus\tOther\n')
        (self.plan / 'tasks/whole/prompt.md').write_text('Build the whole of it.\n')

    def node_prompt(self, *extra):
        r = self.run_planner('node-prompt', '--plan', self.plan, '--task', 'whole', *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(self.plan / 'tasks/whole/split.prompt'))
        return (self.plan / 'tasks/whole/split.prompt').read_text()

    def node_parse(self, text, *extra):
        path = self.dir / 'answer.txt'
        path.write_text(text)
        return self.run_planner('node-parse', '--plan', self.plan, '--task', 'whole', '--reply', path, *extra)

    def answer(self, tasks, reason='two halves', fenced=True):
        body = json.dumps(dict({'tasks': tasks}, **({'reason': reason} if reason is not None else {})))
        return 'My answer.\n```forge-plan\n%s\n```\n' % body if fenced else body

    def test_the_question_carries_the_task_what_stopped_it_and_the_limits(self):
        (self.plan / 'tasks/whole/qa.last').write_text('VERDICT: FAIL\nThe docs half is missing.\n')
        (self.plan / 'tasks/whole/decision.md').write_text('Stopped on split: two concerns in one task.\n')
        text = self.node_prompt()
        for needle in ('read-only', 'Its id is `whole`, its difficulty `high`, its title "The whole of it"',
                       '<<<TASK\nBuild the whole of it.\nTASK>>>', 'Stopped on split: two concerns in one task.',
                       'The docs half is missing.', 'Keeping it whole is a valid answer', 'A split is never required',
                       "owns paths inside this task's files** (src/, docs/)", 'the subtasks build on it',
                       'At most 6 subtasks.', 'a list of 1 to 6 objects (one means: keep it whole)', '```forge-plan'):
            self.assertIn(needle, text)
        self.assertNotIn('set aside', text)

    def test_the_question_follows_the_start_and_the_runs_limit(self):
        (self.plan / 'decomposition.json').write_text(json.dumps({'version': 1, 'nodes': {}, 'limits': {
            'depth': 2, 'children': 3, 'nodes': 24}}))
        text = self.node_prompt('--start', 'fresh')
        self.assertIn('is set aside: the subtasks start from the code as it was', text)
        self.assertNotIn('the subtasks build on it', text)
        self.assertIn('At most 3 subtasks.', text)
        self.assertNotIn('# Why it stopped', text)
        r = self.run_planner('node-prompt', '--plan', self.plan, '--task', 'nope')
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no task 'nope'", r.stderr)

    def test_an_answer_is_stored_as_the_tasks_proposal(self):
        (self.plan / 'tasks/whole/split.refused').write_text('old\n')
        r = self.node_parse(self.answer([part('a', ['src/']), part('b', ['docs/guide.md'], deps=['a'])]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        proposal = json.loads(self.stored('split.proposal.json'))
        self.assertEqual((proposal['reason'], [(t['id'], t['deps']) for t in proposal['tasks']]),
                         ('two halves', [('a', []), ('b', ['a'])]))
        self.assertIsNone(self.stored('split.refused'))

    def test_subtasks_a_person_wrote_are_read_bare_and_can_go_elsewhere(self):
        out = self.dir / 'checked.json'
        r = self.node_parse(self.answer([part('a', ['src/x.py']), part('b', ['docs/'])], reason=None, fenced=False),
                            '--out', out)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((json.loads(out.read_text())['reason'], len(json.loads(out.read_text())['tasks'])), ('', 2))
        self.assertIsNone(self.stored('split.proposal.json'))

    def test_one_task_is_the_answer_that_keeps_it_whole(self):
        r = self.node_parse(self.answer([part('whole', ['src/', 'docs/'])], reason='one coherent change'))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        proposal = json.loads(self.stored('split.proposal.json'))
        self.assertEqual((proposal['reason'], len(proposal['tasks'])), ('one coherent change', 1))

    def test_an_answer_that_breaks_a_rule_is_refused_and_nothing_is_stored(self):
        cases = (
            ('a file outside the task', self.answer([part('a', ['src/']), part('b', ['other.py'])]),
             "file 'other.py' is outside its parent's files (src/, docs/)"),
            ('a dep on another task of the plan', self.answer([part('a', ['src/'], deps=['other']), part('b', ['docs/'])]),
             "unknown dep 'other'"),
            ('no tasks', self.answer([]), 'subtasks must be a list of 1 to 20 objects (0)'),
            ('a reason of two lines', self.answer([part('a', ['src/']), part('b', ['docs/'])], reason='a\nb'),
             'reason is not one line'),
            ('one more key', 'x\n```forge-plan\n%s\n```\n' % json.dumps({'tasks': [part('a', ['src/'])], 'groups': []}), 'groups'),
            ('no block and no JSON', 'I would split it in two.', 'not valid JSON'),
        )
        for what, text, said in cases:
            with self.subTest(what):
                r = self.node_parse(text)
                self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
                self.assertIn('subtasks refused: ', r.stdout)
                self.assertIn(said, r.stdout)
                self.assertIn(said, self.stored('split.refused'))
                self.assertIsNone(self.stored('split.proposal.json'))


if __name__ == '__main__':
    import unittest
    unittest.main()
