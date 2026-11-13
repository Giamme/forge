"""Affected tasks: an approved proposal marks them and regenerates only their questions (#136).

A wave apply that changes one task regenerates that task and every unstarted task that depends
on it. A merged or running dependent is flagged and left alone. A plan apply that adds a task
marks the new task. Answers survive regeneration when the question text does; the rest are kept
in brief.answers.prev.json. `--no-regenerate` writes the markers and does not dispatch.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
from pathlib import Path
import re
import sys
import unittest

from test_brief import question, reply, says
from test_brief_plan_diff import PlanCase, new
from test_brief_proposals import ProposalCase, change, canned

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_brief.proposals as proposals  # noqa: E402

STAMP = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')
NEW_A = 'New prompt for a. TASK_a\n'


def marker(plan, task):
    return json.loads((plan / 'tasks' / task / 'brief.affected').read_text())


def brief_runs(plan):
    """`(task, n)` for each brief dispatch, not the brief-change dispatch that proposed."""
    root = plan / 'side' / 'brief'
    found = []
    if not root.is_dir():
        return found
    for task_dir in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith('.')):
        runs = sorted((path.name for path in task_dir.iterdir() if path.is_dir() and path.name.isdigit()), key=int)
        found.extend((task_dir.name, run) for run in runs)
    return found


class CarryTests(unittest.TestCase):
    def test_an_answer_follows_the_question_text_and_the_rest_is_dropped(self):
        old = [{'id': 'q1', 'text': 'Drop me?'}, {'id': 'q2', 'text': 'Keep me?'}]
        answers = {'q1': {'text': 'gone', 'actor': 'ada', 't': 1},
                   'q2': {'text': 'kept', 'actor': 'ada', 't': 2}}
        new = [{'id': 'q1', 'text': 'Keep me?'}, {'id': 'q2', 'text': 'New?'}]
        kept, dropped = proposals._carry_answers(old, answers, new)
        self.assertEqual(kept, {'q1': answers['q2']})
        self.assertEqual(dropped, {'q1': answers['q1']})

    def test_the_same_text_twice_pairs_in_order_and_a_blank_text_never_matches(self):
        old = [{'id': 'q1', 'text': 'Same?'}, {'id': 'q2', 'text': 'Same?'}, {'id': 'q3', 'text': ''}]
        answers = {'q1': {'text': 'first'}, 'q2': {'text': 'second'}, 'q3': {'text': 'blank'}}
        new = [{'id': 'q1', 'text': 'Same?'}, {'id': 'q2', 'text': 'Same?'}, {'id': 'q3', 'text': ''}]
        kept, dropped = proposals._carry_answers(old, answers, new)
        self.assertEqual([kept[qid]['text'] for qid in ('q1', 'q2')], ['first', 'second'])
        self.assertEqual(dropped, {'q3': {'text': 'blank'}})


class WaveFixture(ProposalCase):
    """a changed, b depends on a, m merged and r running also depend on a, u does not."""

    def setUp(self):
        super().setUp()
        rows = [('a', '-'), ('b', 'a'), ('m', 'a'), ('r', 'a'), ('u', '-'), ('c', '-'), ('d', '-'), ('e', '-')]
        (self.plan / 'tasks.tsv').write_text(''.join(
            '%s\t%s\tlow\t%s.txt\tsol\topus\tTitle of %s\n' % (task, deps, task, task) for task, deps in rows))
        for task, _ in rows:
            tdir = self.plan / 'tasks' / task
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / 'prompt.md').write_text('Implement the change. TASK_%s\n' % task)
            (tdir / 'approach.md').write_text('Old approach of %s.\n' % task)
        (self.plan / 'tasks' / 'm' / 'status').write_text('PASS\n')
        (self.plan / 'tasks' / 'm' / 'merged').write_text('1\n')
        (self.plan / 'tasks' / 'r' / 'status').write_text('RUNNING\n')
        self.bytes = {}
        self.bytes['a'] = self.put_brief('a', [dict(question('Drop me?'), id='q1'),
                                               dict(question('Keep me?'), id='q2')],
                                         {'q1': {'text': 'gone', 'actor': 'ada', 't': 1},
                                          'q2': {'text': 'kept', 'actor': 'ada', 't': 2}})
        self.bytes['b'] = self.put_brief('b', [dict(question('Old b?'), id='q1')])
        self.bytes['m'] = self.put_brief('m', [dict(question('Merged?'), id='q1')])
        self.bytes['r'] = self.put_brief('r', [dict(question('Running?'), id='q1')])
        self.bytes['u'] = self.put_brief('u', [dict(question('Unrelated?'), id='q1')])

    def put_brief(self, task, questions, answers=None):
        tdir = self.plan / 'tasks' / task
        (tdir / 'brief.json').write_text(json.dumps(
            {'questions': questions, 'model': 'sol', 'generated_at': '2026-11-13T00:00:00Z'}) + '\n')
        if answers is not None:
            (tdir / 'brief.answers.json').write_text(json.dumps(answers) + '\n')
        return (tdir / 'brief.json').read_bytes()

    def propose_a(self, prompt=NEW_A):
        self.arm({'*.qa.*': says(canned(change('a', prompt=prompt)))})
        record = proposals.propose(self.plan, 'a', 'q1')
        self.assertEqual(record['state'], 'proposed', record.get('reason'))
        return record

    def arm_questions(self, *texts):
        self.arm({'*.qa.*': says(reply(*(question(text) for text in texts)))})


class WaveAffectedTests(WaveFixture):
    def test_the_changed_task_and_its_unstarted_dependent_are_regenerated_and_the_started_are_flagged(self):
        self.propose_a()
        self.arm_questions('Keep me?')
        result = proposals.apply(self.plan, 'p1', self.digest())

        self.assertEqual(result['affected'], ['a', 'b'])
        self.assertEqual(result['flagged'], ['m', 'r'])
        self.assertIn('regenerating questions for 2 tasks: a, b', result['notes'])
        self.assertIn('flagged (already started): m, r', result['notes'])
        self.assertEqual(brief_runs(self.plan), [('a', '1'), ('b', '1')])
        self.assertEqual((self.plan / 'tasks' / 'a' / 'prompt.md').read_text(), NEW_A)

        changed = marker(self.plan, 'a')
        downstream = marker(self.plan, 'b')
        self.assertEqual((changed['pid'], changed['reason']), ('p1', 'changed'))
        self.assertNotIn('flag_only', changed)
        self.assertEqual((downstream['pid'], downstream['reason']), ('p1', 'dependent'))
        self.assertNotIn('flag_only', downstream)
        self.assertEqual(changed['t'], downstream['t'])
        self.assertRegex(changed['t'], STAMP)

        for task in ('m', 'r'):
            flagged = marker(self.plan, task)
            self.assertEqual((flagged['pid'], flagged['reason'], flagged['flag_only']), ('p1', 'dependent', True))
            self.assertEqual((self.plan / 'tasks' / task / 'brief.json').read_bytes(), self.bytes[task])
            self.assertFalse((self.plan / 'side' / 'brief' / task).exists())
        self.assertEqual((self.plan / 'tasks' / 'u' / 'brief.json').read_bytes(), self.bytes['u'])
        self.assertFalse((self.plan / 'tasks' / 'u' / 'brief.affected').exists())

        fresh = self.brief('a')
        self.assertEqual([q['text'] for q in fresh['questions']], ['Keep me?'])
        carried = json.loads((self.plan / 'tasks' / 'a' / 'brief.answers.json').read_text())
        self.assertEqual(carried, {'q1': {'text': 'kept', 'actor': 'ada', 't': 2}})
        prev = json.loads((self.plan / 'tasks' / 'a' / 'brief.answers.prev.json').read_text())
        self.assertEqual(prev, {'q1': {'text': 'gone', 'actor': 'ada', 't': 1}})
        archived = list((self.plan / 'tasks' / 'a' / 'brief.history').glob('*/brief.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text())['questions'][0]['text'], 'Drop me?')
        self.assertEqual([q['text'] for q in self.brief('b')['questions']], ['Keep me?'])

        text = self.cli('show', self.plan).stdout
        for task in ('a', 'b', 'm', 'r'):
            self.assertIn('%s  affected by p1' % task, text)
        self.assertIn('m  affected by p1  flagged (already started)', text)
        self.assertIn('r  affected by p1  flagged (already started)', text)
        self.assertNotIn('u  affected', text)
        found = json.loads(self.cli('show', self.plan, '--json').stdout)
        self.assertEqual([(row['task'], row['reason'], row['flag_only']) for row in found['affected']], [
            ('a', 'changed', False), ('b', 'dependent', False),
            ('m', 'dependent', True), ('r', 'dependent', True)])
        only = json.loads(self.cli('show', self.plan, '--task', 'm', '--json').stdout)
        self.assertEqual([row['task'] for row in only['affected']], ['m'])
        self.assertTrue(only['affected'][0]['flag_only'])

        approved = [body for body in self.brief_diffs() if body.get('state') == 'approved']
        self.assertEqual([(body['affected'], body['flagged']) for body in approved], [(['a', 'b'], ['m', 'r'])])
        proposed = [body for body in self.brief_diffs() if body.get('state') == 'proposed']
        self.assertTrue(proposed)
        self.assertNotIn('affected', proposed[0])
        self.assertNotIn('flagged', proposed[0])

    def test_the_cli_prints_the_regenerating_line(self):
        self.propose_a()
        self.arm_questions('Keep me?')
        shown = self.cli('apply', self.plan, 'p1', '--approve', self.digest())
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('regenerating questions for 2 tasks: a, b', shown.stdout)
        self.assertIn('flagged (already started): m, r', shown.stdout)
        self.assertEqual(brief_runs(self.plan), [('a', '1'), ('b', '1')])

    def test_a_dependent_of_a_dependent_is_regenerated_too(self):
        tdir = self.plan / 'tasks' / 'k'
        tdir.mkdir()
        (tdir / 'prompt.md').write_text('Implement the change. TASK_k\n')
        (tdir / 'approach.md').write_text('Old approach of k.\n')
        (self.plan / 'tasks.tsv').write_text(
            (self.plan / 'tasks.tsv').read_text() + 'k\tb\tlow\tk.txt\tsol\topus\tTitle of k\n')
        self.put_brief('k', [dict(question('K?'), id='q1')])
        self.propose_a()
        self.arm_questions('Keep me?')
        result = proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual(result['affected'], ['a', 'b', 'k'])
        self.assertEqual(result['flagged'], ['m', 'r'])
        self.assertEqual(brief_runs(self.plan), [('a', '1'), ('b', '1'), ('k', '1')])
        self.assertEqual(marker(self.plan, 'k')['reason'], 'dependent')

    def test_no_regenerate_marks_without_a_dispatch(self):
        self.propose_a()
        before = self.calls()
        result = proposals.apply(self.plan, 'p1', self.digest(), regenerate=False)
        self.assertEqual(result['affected'], ['a', 'b'])
        self.assertEqual(result['flagged'], ['m', 'r'])
        self.assertIn('marked affected, not regenerating: a, b', result['notes'])
        self.assertNotIn('regenerating questions', '\n'.join(result['notes']))
        self.assertEqual(self.calls(), before)
        self.assertEqual(brief_runs(self.plan), [])
        self.assertEqual((self.plan / 'tasks' / 'a' / 'brief.json').read_bytes(), self.bytes['a'])
        self.assertEqual((self.plan / 'tasks' / 'b' / 'brief.json').read_bytes(), self.bytes['b'])
        self.assertEqual((self.plan / 'tasks' / 'u' / 'brief.json').read_bytes(), self.bytes['u'])
        self.assertEqual(marker(self.plan, 'a')['reason'], 'changed')
        self.assertTrue(marker(self.plan, 'm')['flag_only'])
        self.assertFalse((self.plan / 'tasks' / 'a' / 'brief.answers.prev.json').exists())
        approved = [body for body in self.brief_diffs() if body.get('state') == 'approved']
        self.assertEqual(approved[-1]['affected'], ['a', 'b'])
        self.assertEqual(approved[-1]['flagged'], ['m', 'r'])

    def test_the_cli_no_regenerate_dispatches_nothing(self):
        self.propose_a()
        before = self.calls()
        shown = self.cli('apply', self.plan, 'p1', '--approve', self.digest(), '--no-regenerate')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('marked affected, not regenerating: a, b', shown.stdout)
        self.assertNotIn('regenerating questions', shown.stdout)
        self.assertIn('flagged (already started): m, r', shown.stdout)
        self.assertEqual(self.calls(), before)
        self.assertEqual(brief_runs(self.plan), [])
        self.assertTrue((self.plan / 'tasks' / 'b' / 'brief.affected').is_file())
        self.assertEqual((self.plan / 'tasks' / 'b' / 'brief.json').read_bytes(), self.bytes['b'])

    def test_the_next_approved_proposal_replaces_the_marker(self):
        self.propose_a()
        proposals.apply(self.plan, 'p1', self.digest(), regenerate=False)
        self.assertEqual(marker(self.plan, 'b')['pid'], 'p1')
        self.propose_a('Newer prompt for a. TASK_a\n')
        proposals.apply(self.plan, 'p2', self.digest('p2'), regenerate=False)
        self.assertEqual(marker(self.plan, 'a')['pid'], 'p2')
        self.assertEqual(marker(self.plan, 'b')['pid'], 'p2')
        self.assertEqual(marker(self.plan, 'm')['pid'], 'p2')
        self.assertTrue(marker(self.plan, 'm')['flag_only'])
        self.assertFalse((self.plan / 'tasks' / 'u' / 'brief.affected').exists())
        self.assertEqual(self.state('p1')['state'], 'approved')
        self.assertTrue((self.plan / 'proposals' / 'p1' / 'proposal.json').is_file())
        text = self.cli('show', self.plan).stdout
        self.assertIn('a  affected by p2', text)
        self.assertNotIn('affected by p1', text)

    def test_a_refused_apply_marks_nothing_and_dispatches_nothing(self):
        self.propose_a()
        before = {task: (self.plan / 'tasks' / task / 'brief.json').read_bytes() for task in 'abmru'}
        with self.assertRaises(proposals.Refused):
            proposals.apply(self.plan, 'p1', '0' * 64)
        self.assertEqual(brief_runs(self.plan), [])
        for task, data in before.items():
            self.assertEqual((self.plan / 'tasks' / task / 'brief.json').read_bytes(), data)
            self.assertFalse((self.plan / 'tasks' / task / 'brief.affected').exists())


class PlanAffectedTests(PlanCase):
    def test_an_added_task_is_affected_and_regenerated(self):
        untouched = self.put_brief_bytes('c', [dict(question('Leave c?'), id='q1')])
        self.plan_propose(add=[new('f', ['c'])])
        self.arm({'*.qa.*': says(reply(question('Added?')))})
        result = proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual(result['affected'], ['f'])
        self.assertEqual(result['flagged'], [])
        self.assertIn('regenerating questions for 1 tasks: f', result['notes'])
        self.assertEqual(marker(self.plan, 'f')['reason'], 'added')
        self.assertNotIn('flag_only', marker(self.plan, 'f'))
        self.assertEqual([q['text'] for q in self.brief('f')['questions']], ['Added?'])
        self.assertEqual(brief_runs(self.plan), [('f', '1')])
        self.assertEqual((self.plan / 'tasks' / 'c' / 'brief.json').read_bytes(), untouched)
        self.assertFalse((self.plan / 'tasks' / 'c' / 'brief.affected').exists())
        approved = [body for body in self.brief_diffs() if body.get('state') == 'approved']
        self.assertEqual(approved[-1]['affected'], ['f'])
        self.assertEqual(approved[-1]['flagged'], [])

    def test_removing_a_task_marks_the_rewritten_dependent_and_what_depends_on_it(self):
        untouched = self.put_brief_bytes('a', [dict(question('Leave a?'), id='q1')])
        self.put_brief_bytes('d', [dict(question('Old d?'), id='q1')])
        self.put_brief_bytes('e', [dict(question('Old e?'), id='q1')])
        self.plan_propose(remove=['c'], deps=[{'task': 'd', 'deps': []}])
        self.arm({'*.qa.*': says(reply(question('After?')))})
        result = proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual(result['affected'], ['d', 'e'])
        self.assertEqual(result['flagged'], [])
        self.assertEqual(marker(self.plan, 'd')['reason'], 'changed')
        self.assertEqual(marker(self.plan, 'e')['reason'], 'dependent')
        self.assertEqual(brief_runs(self.plan), [('d', '1'), ('e', '1')])
        self.assertEqual([q['text'] for q in self.brief('e')['questions']], ['After?'])
        self.assertFalse((self.plan / 'tasks' / 'c').exists())
        removed = self.plan / 'tasks' / '.removed' / 'c-p1'
        self.assertTrue(removed.is_dir())
        self.assertFalse((removed / 'brief.affected').exists())
        self.assertEqual((self.plan / 'tasks' / 'a' / 'brief.json').read_bytes(), untouched)

    def put_brief_bytes(self, task, questions):
        tdir = self.plan / 'tasks' / task
        (tdir / 'brief.json').write_text(json.dumps(
            {'questions': questions, 'model': 'sol', 'generated_at': '2026-11-13T00:00:00Z'}) + '\n')
        return (tdir / 'brief.json').read_bytes()


if __name__ == '__main__':
    unittest.main()
