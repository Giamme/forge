"""Decisions and open-question defaults in the implementer and reviewer prompts (#135).

The fixture is a plan directory: `<plan>/brief` is the level, and the task dir beside
prompt.md holds brief.json and brief.answers.json. Output with brief off is compared to
the same fixture before those files exist.
"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'

PORT = {
    'id': 'q1', 'text': 'Which port should the server bind?', 'why': 'the host is shared',
    'options': ['8080', 'OPTION_SENTINEL'], 'default': '8080', 'scope': 'task', 'must_answer': False,
}
STORE = {
    'id': 'q2', 'text': 'Which store should hold the sessions?', 'why': 'it changes the schema',
    'options': ['sqlite', 'OPTION_SENTINEL'], 'default': 'sqlite', 'scope': 'task', 'must_answer': True,
}
ANSWERED = {'q1': {'text': '443', 'actor': 'ada', 't': '2026-11-11T00:00:00Z'}}
HEADINGS = (
    '## Ownership and baseline\n',
    '## Overall goal\n',
    '## Complete implementation requirements\n',
    '## Intended approach\n',
    '## Decisions the user made\n',
    '## Open questions: follow the stated default\n',
    '## Your previous attempt was reviewed and rejected\n',
    '## Project memory (context from earlier runs)\n',
)


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PromptTests(unittest.TestCase):
    prompt = load('forge_prompt_brief', 'forge-prompt.py')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.plan = self.root / 'plan'
        self.task = self.plan / 'tasks' / 'build'
        self.task.mkdir(parents=True)
        self.req = self.task / 'prompt.md'
        self.approach = self.task / 'approach.md'
        self.retry = self.task / 'retry_findings.md'
        self.req.write_text('REQ\n')
        self.approach.write_text('APPROACH\n')
        self.retry.write_text('FOUND\n')
        self.clean = {role: self.render(role) for role in ('dwarf', 'qa')}
        for text in self.clean.values():
            self.assertNotIn('Decisions the user made', text)
            self.assertNotIn('Open questions', text)
            self.assertIn('REQ\n', text)
            self.assertIn('APPROACH\n', text)
            self.assertIn('FOUND\n', text)

    def render(self, role='dwarf', **extra):
        return self.prompt.context(
            str(self.req), approach=str(self.approach), retry=str(self.retry), role=role, **extra)

    def write_brief(self, questions, answers=None, late=None, level=None):
        level_path = self.plan / 'brief'
        if level is None:
            level_path.unlink(missing_ok=True)
        else:
            level_path.write_text(level + '\n')
        (self.task / 'brief.json').write_text(json.dumps({
            'questions': questions, 'model': 'stub', 'generated_at': '2026-11-11T00:00:00Z'}))
        for name, body in (('brief.answers.json', answers), ('brief.late.json', late)):
            path = self.task / name
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(body))

    def assert_unchanged(self):
        for role, clean in self.clean.items():
            self.assertEqual(self.render(role), clean, role)

    def between(self, text, start, end):
        return text.split(start, 1)[1].split(end, 1)[0]

    def test_no_plan_brief_file_is_byte_identical(self):
        self.write_brief([PORT, STORE], answers=ANSWERED)
        self.assert_unchanged()

    def test_a_plan_brief_without_brief_json_is_byte_identical(self):
        (self.plan / 'brief').write_text('advisory\n')
        self.assert_unchanged()

    def test_empty_questions_are_byte_identical(self):
        self.write_brief([], answers={'q1': {'text': 'GHOST', 'actor': 'ada', 't': '1'}}, level='advisory')
        self.assert_unchanged()

    def test_a_brief_json_that_does_not_parse_is_byte_identical(self):
        (self.plan / 'brief').write_text('advisory\n')
        (self.task / 'brief.json').write_text('{not json')
        self.assert_unchanged()

    def test_advisory_and_gated_put_decisions_then_defaults_between_approach_and_the_retry(self):
        capsule, goal, memory = self.root / 'capsule.md', self.root / 'goal.md', self.root / 'memory.md'
        capsule.write_text('CAPSULE\n')
        goal.write_text('GOAL\n')
        memory.write_text('## Old\n- remembered fact\n')
        self.write_brief([PORT, STORE], answers=ANSWERED, level='advisory')
        for level in ('advisory', 'gated'):
            (self.plan / 'brief').write_text(level + '\n')
            out = self.render(capsule=str(capsule), goal=str(goal), memories=(str(memory),))
            positions = [out.index(heading) for heading in HEADINGS]
            self.assertEqual(positions, sorted(positions), level)
            decisions = self.between(out, '## Decisions the user made\n',
                                     '## Open questions: follow the stated default\n')
            opens = self.between(out, '## Open questions: follow the stated default\n',
                                 '## Your previous attempt was reviewed and rejected\n')
            self.assertIn('override anything in the requirements or approach they contradict.', decisions)
            self.assertNotIn('not a deviation', out)
            self.assertLess(decisions.index('Which port should the server bind?'), decisions.index('443'))
            self.assertNotIn('Which store should hold the sessions?', decisions)
            self.assertNotIn('8080', out)
            self.assertNotIn('OPTION_SENTINEL', out)
            self.assertIn('say in the final message which defaults were used.', opens)
            self.assertLess(opens.index('Which store should hold the sessions?'), opens.index('sqlite'))
            self.assertNotIn('443', opens)
            self.assertNotIn('Which port should the server bind?', opens)

    def test_a_first_attempt_with_no_retry_still_carries_the_brief(self):
        self.write_brief([PORT, STORE], answers=ANSWERED, level='advisory')
        for role in ('dwarf', 'qa'):
            out = self.prompt.context(str(self.req), approach=str(self.approach), role=role)
            self.assertIn('443', out)
            self.assertIn('sqlite', out)
            self.assertLess(out.index('## Intended approach\n'), out.index('## Decisions the user made\n'))
            self.assertLess(out.index('## Decisions the user made\n'),
                            out.index('## Open questions: follow the stated default\n'))
            self.assertNotIn('previous attempt', out)
            self.assertNotIn('previous review', out)

    def test_strict_carries_decisions_and_no_open_questions(self):
        self.write_brief([PORT, STORE], answers=ANSWERED, level='strict')
        out = self.render()
        self.assertIn('443', out)
        self.assertIn('Which port should the server bind?', out)
        self.assertIn('override anything in the requirements or approach they contradict.', out)
        self.assertNotIn('Open questions', out)
        self.assertNotIn('Which store should hold the sessions?', out)
        self.assertNotIn('sqlite', out)
        self.assertLess(out.index('## Intended approach\n'), out.index('## Decisions the user made\n'))
        self.assertLess(out.index('## Decisions the user made\n'),
                        out.index('## Your previous attempt was reviewed and rejected\n'))

    def test_the_reviewer_is_told_a_decision_is_not_a_deviation(self):
        self.write_brief([PORT, STORE], answers=ANSWERED, level='advisory')
        out = self.render('qa')
        self.assertIn(
            "These are the user's own decisions, so following them is not a deviation and must "
            'not be reported as a finding.', out)
        self.assertIn('override anything in the requirements or approach they contradict.', out)
        self.assertIn('443', out)
        self.assertIn('These are the defaults the implementer was told to follow.', out)
        self.assertIn('sqlite', out)
        self.assertLess(out.index('## Intended approach\n'), out.index('## Decisions the user made\n'))
        self.assertLess(out.index('## Decisions the user made\n'),
                        out.index('## Open questions: follow the stated default\n'))
        self.assertLess(out.index('## Open questions: follow the stated default\n'),
                        out.index('## Findings from the previous review round\n'))

    def test_late_answers_are_ignored(self):
        self.write_brief(
            [PORT, STORE], answers=ANSWERED, level='advisory',
            late={'q1': {'text': 'LATE_OVERRIDE', 'actor': 'ada', 't': '2'},
                  'q2': {'text': 'LATE_SENTINEL', 'actor': 'ada', 't': '2'}})
        out = self.render()
        self.assertIn('443', out)
        self.assertIn('sqlite', out)
        self.assertNotIn('LATE_OVERRIDE', out)
        self.assertNotIn('LATE_SENTINEL', out)
        opens = out.split('## Open questions: follow the stated default\n', 1)[1]
        self.assertIn('Which store should hold the sessions?', opens)

    def test_a_broken_answers_file_keeps_the_questions_on_their_defaults(self):
        self.write_brief([PORT, STORE], level='advisory')
        (self.task / 'brief.answers.json').write_text('{not json')
        out = self.render()
        self.assertIn('8080', out)
        self.assertIn('sqlite', out)
        self.assertNotIn('## Decisions the user made', out)
        self.assertIn('## Open questions: follow the stated default\n', out)

    def test_an_answer_of_4kb_is_kept_and_one_byte_over_is_cut(self):
        cap = 4 * 1024
        exact = 'E' * cap
        over = 'O' * cap + 'X'
        self.write_brief([
            {'id': 'q1', 'text': 'Keep the whole answer', 'default': 'no'},
            {'id': 'q2', 'text': 'Cut the long answer', 'default': 'no'},
        ], answers={
            'q1': {'text': exact, 'actor': 'ada', 't': '1'},
            'q2': {'text': over, 'actor': 'ada', 't': '1'},
        }, level='strict')
        out = self.render()
        self.assertIn(exact, out)
        self.assertNotIn('E' * (cap + 1), out)
        self.assertIn('O' * cap, out)
        self.assertNotIn('O' * (cap + 1), out)
        self.assertNotIn('X', out)
        self.assertIn('[... the answer continues; cut here ...]', out)

    def test_a_brief_chunk_over_16kb_is_cut(self):
        cap = 16 * 1024
        questions, answers = [], {}
        for i in range(6):
            label = 'FIRST_QUESTION ' if i == 0 else 'LAST_QUESTION ' if i == 5 else ''
            questions.append({'id': 'q%d' % i, 'text': label + ('q' * 6000), 'default': 'd'})
            answers['q%d' % i] = {'text': 'yes', 'actor': 'ada', 't': '1'}
        self.write_brief(questions, answers=answers, level='strict')
        out = self.render()
        self.assertIn('FIRST_QUESTION', out)
        self.assertNotIn('LAST_QUESTION', out)
        self.assertIn('[... the brief continues; cut here ...]', out)
        self.assertGreater(len(out.encode()), cap)
        self.assertLess(len(out.encode()), cap + 2048)
