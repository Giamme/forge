"""Wave-scoped proposals: `forge brief propose|proposals|apply|reject` and parked answers (#136).

The brief-change dispatch goes through the fake CLI harness with canned replies, borrowed the
way test_brief does. Every member prompt in the fixture names TASK_a, so the fake tells all
dispatches apart by call number only (`*.qa.1`, `*.qa.2`).
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import sys
import unittest

from test_brief import BriefCase, question, says

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_brief  # noqa: E402
import forge_brief.answers as answers  # noqa: E402
import forge_brief.proposals as proposals  # noqa: E402
import forge_groups  # noqa: E402
import forge_side  # noqa: E402

HEADER = '# id\treview\tqa\tmembers\ttitle\tgoal\n'
NEW_A = 'Implement the change with sqlite. TASK_a\n'
NEW_B = 'Implement the b change with sqlite. TASK_b\n'
APPROACH_A = 'Use the sqlite store.\n'
VOLATILE = ('proposals', 'side', 'control.lock', '.brief.lock', 'brief.groups.noted', 'dataset.uid')


def change(task, prompt=None, approach=None, files=None, **extra):
    return dict(task=task, prompt=prompt, approach=approach, files=files, **extra)


def canned(*changes, summary='Use sqlite everywhere'):
    return json.dumps({'summary': summary, 'changes': list(changes)})


class ProposalCase(BriefCase):
    """Tasks a, b (group g1, review=group), c, d (group g2) and e (no group). q1 of a is the parked wave question."""

    review = 'group'

    def setUp(self):
        super().setUp()
        (self.plan / 'brief_scope').write_text('wave\n')
        rows = [('a', 'a.txt'), ('b', 'b.txt'), ('c', 'c.txt'), ('d', 'd.txt'), ('e', 'e.txt')]
        (self.plan / 'tasks.tsv').write_text(''.join(
            '%s\t-\tlow\t%s\tsol\topus\tTitle of %s\n' % (task, files, task) for task, files in rows))
        for task, _ in rows:
            tdir = self.plan / 'tasks' / task
            tdir.mkdir(parents=True, exist_ok=True)
            (tdir / 'prompt.md').write_text('Implement the change. TASK_%s\n' % task)
            (tdir / 'approach.md').write_text('Old approach of %s.\n' % task)
        (self.plan / 'groups.tsv').write_text(
            HEADER + 'g1\t%s\t-\ta,b\tStore\tPick the store\n' % self.review + 'g2\ttask\t-\tc,d\tOther\t-\n')
        forge_groups.freeze(self.plan)
        (self.plan / 'waves.tsv').write_text(''.join('1\t%s\n' % task for task, _ in rows))
        parked = {'questions': [dict(question('Which store?', 'wave'), id='q1', group='g1'),
                                dict(question('Plan wide?', 'plan'), id='q2', group=None)],
                  'model': 'sol', 'generated_at': '2026-11-13T00:00:00Z'}
        (self.plan / 'tasks' / 'a' / 'brief.parked.json').write_text(json.dumps(parked) + '\n')
        self.answer('a', 'q1', 'sqlite')

    def answer(self, task, qid, text):
        return answers.answer(self.plan, task, qid, text=text)

    def snapshot(self):
        """Every file of the plan but the ones a proposal run is allowed to leave: path -> bytes."""
        found = {}
        for path in sorted(self.plan.rglob('*')):
            relative = path.relative_to(self.plan)
            if path.is_file() and relative.parts[0] not in VOLATILE and path.name not in VOLATILE:
                found[str(relative)] = path.read_bytes()
        return found

    # apply marks affected tasks and regenerates their questions. Those files, and the
    # brief.history archive (its directory name is a timestamp), are asserted in
    # test_brief_affected. The lists here are the plan files the diff itself writes.
    _REGEN_NAMES = {'brief.affected', 'brief.json', 'brief.answers.json', 'brief.answers.prev.json',
                    'brief.parked.json', 'brief.late.json'}

    def changed(self, before):
        after = self.snapshot()
        names = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
        return [name for name in names if Path(name).name not in self._REGEN_NAMES
                and 'brief.history' not in Path(name).parts]

    def propose(self, *changes, **kwargs):
        self.arm({'*.qa.*': says(canned(*changes, **kwargs))})
        return proposals.propose(self.plan, 'a', 'q1')

    def start(self, task, status='RUNNING'):
        (self.plan / 'tasks' / task / 'status').write_text(status + '\n')

    def state(self, pid='p1'):
        return json.loads((self.plan / 'proposals' / pid / 'proposal.json').read_text())

    def digest(self, pid='p1'):
        return (self.plan / 'proposals' / pid / 'hash').read_text().strip()

    def brief_diffs(self):
        return [body for _, body in self.events('brief_diff')]


class ProposeAndApplyTests(ProposalCase):
    def test_a_proposal_touching_two_unstarted_members_is_a_diff_and_apply_writes_exactly_it(self):
        record = self.propose(change('a', prompt=NEW_A, approach=APPROACH_A),
                              change('b', prompt=NEW_B, files='b.txt,b2.txt'))
        self.assertEqual((record['pid'], record['state'], record['reason']), ('p1', 'proposed', None))
        self.assertEqual((record['scope'], record['group'], record['task'], record['qid']), ('wave', 'g1', 'a', 'q1'))
        self.assertEqual(record['answer'], 'sqlite')
        self.assertEqual(self.calls(), ['qa'])
        diff = (self.plan / 'proposals' / 'p1' / 'diff.patch').read_text()
        for path in ('tasks/a/prompt.md', 'tasks/a/approach.md', 'tasks/b/prompt.md', 'tasks.tsv'):
            self.assertIn('+++ b/' + path, diff)
        self.assertNotIn('tasks/b/approach.md', diff)
        self.assertEqual(len(self.digest()), 64)
        before = self.snapshot()

        result = proposals.apply(self.plan, 'p1', self.digest())

        self.assertEqual(result['state'], 'approved')
        # deferrals.txt is waves.tsv's companion: `_waves` writes both
        self.assertEqual(self.changed(before), ['deferrals.txt', 'groups.frozen', 'tasks.tsv',
                                                'tasks/a/approach.md', 'tasks/a/prompt.md', 'tasks/b/prompt.md'])
        self.assertEqual((self.plan / 'tasks' / 'a' / 'prompt.md').read_text(), NEW_A)
        self.assertEqual((self.plan / 'tasks' / 'a' / 'approach.md').read_text(), APPROACH_A)
        rows = {item['id']: item for item in forge_brief.read_tasks(self.plan)}
        self.assertEqual((rows['a']['files'], rows['b']['files']), ('a.txt', 'b.txt,b2.txt'))
        self.assertEqual(rows['b']['title'], 'Title of b')
        self.assertIsNone(forge_groups.check_frozen(self.plan))
        state = self.state()
        self.assertEqual(state['state'], 'approved')
        self.assertTrue(state['applied'])
        self.assertEqual(state['applied_by'], 'cli')

    def test_the_cli_shows_the_diff_and_the_exact_apply_command_and_applies_it(self):
        self.arm({'*.qa.*': says(canned(change('a', prompt=NEW_A)))})
        shown = self.cli('propose', self.plan, 'a', 'q1')
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn('+++ b/tasks/a/prompt.md', shown.stdout)
        command = 'forge brief apply %s p1 --approve %s' % (self.plan, self.digest())
        self.assertIn(command, shown.stdout)
        listed = self.cli('proposals', self.plan)
        self.assertEqual(listed.returncode, 0)
        for text in ('p1', 'proposed', 'scope wave', 'group g1', 'Use sqlite everywhere', command):
            self.assertIn(text, listed.stdout)
        as_json = json.loads(self.cli('proposals', self.plan, '--json').stdout)
        self.assertEqual([(item['pid'], item['state'], item['hash']) for item in as_json],
                         [('p1', 'proposed', self.digest())])
        applied = self.cli('apply', self.plan, 'p1', '--approve', self.digest())
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual((self.plan / 'tasks' / 'a' / 'prompt.md').read_text(), NEW_A)

    def test_a_files_change_recomputes_the_waves_and_refreezes_the_groups(self):
        # A review=task group may overlap: b now shares a file with a, so it moves to wave 2.
        (self.plan / 'groups.tsv').write_text(HEADER + 'g1\ttask\t-\ta,b\tStore\tPick the store\n')
        forge_groups.freeze(self.plan)
        stale = (self.plan / 'groups.frozen').read_text()
        self.propose(change('b', files='a.txt'))
        result = proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual(result['files'], ['tasks.tsv', 'groups.frozen'])
        self.assertNotEqual((self.plan / 'groups.frozen').read_text(), stale)
        self.assertIsNone(forge_groups.check_frozen(self.plan))
        waves = dict(line.split('\t')[::-1] for line in (self.plan / 'waves.tsv').read_text().split('\n') if line)
        self.assertEqual((waves['a'], waves['b'], waves['c']), ('1', '2', '1'))

    def test_a_prompt_only_change_leaves_tasks_tsv_the_freeze_and_the_waves_alone(self):
        self.propose(change('a', prompt=NEW_A))
        before = self.snapshot()
        proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual(self.changed(before), ['tasks/a/prompt.md'])

    def test_the_prompt_gives_the_question_the_group_and_each_member_capped_at_6_kb(self):
        (self.plan / 'tasks' / 'b' / 'prompt.md').write_text('BIG ' * 3000 + 'TASK_b\n')
        self.start('b')
        self.propose(change('a', prompt=NEW_A))
        prompt = (self.plan / 'side' / 'brief-change' / 'a' / '1' / 'prompt.txt').read_text()
        for text in ('Which store?', 'it changes the files', 'x | y', 'sqlite', 'Store', 'Pick the store',
                     'Title of a', 'Title of b', 'Old approach of a.', 'Old approach of b.',
                     str(self.repo), 'b (you may not change them)'):
            self.assertIn(text, prompt)
        self.assertNotIn('Title of c', prompt)
        self.assertIn('(capped)', prompt)
        self.assertIn('truncated to 6 KB', prompt)
        self.assertLess(len(prompt), 20000)

    def test_a_bad_reply_is_retried_once_and_a_second_one_writes_nothing(self):
        self.arm({'*.qa.1': says('sorry, no idea'), '*.qa.2': says(canned(change('a', prompt=NEW_A)))})
        record = proposals.propose(self.plan, 'a', 'q1')
        self.assertEqual((self.calls(), record['state']), (['qa', 'qa'], 'proposed'))
        retry = (self.plan / 'side' / 'brief-change' / 'a' / '2' / 'prompt.txt').read_text()
        self.assertIn('not usable', retry)

        self.arm({'*.qa.*': says('{"summary": 3}')})
        os.environ['CALLS'] = str(self.root / 'calls2')
        self.env['CALLS'] = os.environ['CALLS']
        with self.assertRaises(proposals.Refused):
            proposals.propose(self.plan, 'a', 'q1')
        self.assertEqual(sorted(path.name for path in (self.plan / 'proposals').iterdir() if path.is_dir()), ['p1'])

    def test_proposal_numbers_count_up_and_each_has_its_own_hash(self):
        self.propose(change('a', prompt=NEW_A))
        self.propose(change('a', prompt='Another prompt. TASK_a\n'))
        self.assertEqual([item['pid'] for item in proposals.listing(self.plan)], ['p1', 'p2'])
        self.assertNotEqual(self.digest('p1'), self.digest('p2'))

    def test_a_plan_question_needs_brief_scope_plan_and_a_missing_answer_is_a_usage_error(self):
        # plan questions are proposed as structural plan diffs (test_brief_plan_diff); under brief scope
        # wave they are not answerable, so there is nothing to propose
        with self.assertRaises(proposals.Refused) as caught:
            proposals.propose(self.plan, 'a', 'q2')
        self.assertIn('--brief-scope plan', caught.exception.reason)
        (self.plan / 'tasks' / 'a' / 'brief.parked.answers.json').unlink()
        with self.assertRaises(answers.Usage):
            proposals.propose(self.plan, 'a', 'q1')
        with self.assertRaises(answers.NoSuchQuestion):
            proposals.propose(self.plan, 'a', 'q9')
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.plan / 'proposals').exists())


class ConfinementTests(ProposalCase):
    def assertRefused(self, record, fragment):
        self.assertEqual(record['state'], 'refused')
        self.assertIn(fragment, record['reason'])

    def refuse_and_stay_put(self, fragment, *changes):
        before = self.snapshot()
        record = self.propose(*changes)
        self.assertRefused(record, fragment)
        self.assertEqual(self.changed(before), [])
        with self.assertRaises(proposals.Refused):
            proposals.apply(self.plan, record['pid'], self.digest(record['pid']))
        self.assertEqual(self.changed(before), [])
        shown = self.cli('apply', self.plan, record['pid'], '--approve', self.digest(record['pid']))
        self.assertEqual(shown.returncode, 3)
        self.assertEqual(self.changed(before), [])
        return record

    def test_a_change_to_a_task_outside_the_group_refuses_the_whole_proposal(self):
        record = self.refuse_and_stay_put('task e is not a member of group g1',
                                          change('a', prompt=NEW_A), change('e', prompt='Sneaky. TASK_e\n'))
        diff = (self.plan / 'proposals' / record['pid'] / 'diff.patch').read_text()
        self.assertIn('tasks/a/prompt.md', diff)   # the diff is still shown for information
        self.assertIn('tasks/e/prompt.md', diff)
        # a member of another group is outside this one too
        self.refuse_and_stay_put('task c is not a member of group g1', change('c', prompt='No. TASK_c\n'))

    def test_a_started_member_refuses_the_whole_proposal_whatever_started_it(self):
        for started in (lambda: self.start('b'), lambda: self.start('b', 'PASS'),
                        lambda: (self.plan / 'tasks' / 'b' / 'attempt').write_text('1\n'),
                        lambda: (self.plan / 'tasks' / 'b' / 'attempts.tsv').write_text('1\tsol\t0\n')):
            started()
            record = self.refuse_and_stay_put('task b has started', change('a', prompt=NEW_A), change('b', prompt=NEW_B))
            self.assertIn('tasks/b/prompt.md', (self.plan / 'proposals' / record['pid'] / 'diff.patch').read_text())
            for name in ('status', 'attempt', 'attempts.tsv'):
                (self.plan / 'tasks' / 'b' / name).unlink(missing_ok=True)

    def test_a_pending_status_file_is_not_started(self):
        self.start('b', 'PENDING')
        self.assertEqual(self.propose(change('b', prompt=NEW_B))['state'], 'proposed')

    def test_an_unknown_field_refuses_the_whole_proposal(self):
        self.refuse_and_stay_put('other than prompt, approach and files: deps',
                                 change('a', prompt=NEW_A), change('b', deps='a'))
        self.refuse_and_stay_put('title', change('a', title='New title'))

    def test_an_empty_proposal_and_unusable_values_are_refused(self):
        self.refuse_and_stay_put('changes nothing')
        self.refuse_and_stay_put('changes nothing', change('a', prompt='Implement the change. TASK_a\n'))
        self.refuse_and_stay_put('is empty', change('a', prompt='  \n'))
        self.refuse_and_stay_put('one non-empty line', change('a', files='x\ty'))
        self.refuse_and_stay_put('changed twice', change('a', prompt=NEW_A), change('a', approach=APPROACH_A))

    def test_every_outcome_emits_a_brief_diff_event(self):
        self.propose(change('a', prompt=NEW_A))
        self.propose(change('e', prompt='No. TASK_e\n'))
        proposals.reject(self.plan, 'p1', actor='ada', reason='not now')
        self.propose(change('b', prompt=NEW_B))
        proposals.apply(self.plan, 'p3', self.digest('p3'))
        events = [{key: value for key, value in item.items() if key != 'task'} for item in self.brief_diffs()]
        self.assertEqual(
            [(item['pid'], item['state'], item['tasks']) for item in events],
            [('p1', 'proposed', ['a']), ('p2', 'refused', ['e']), ('p1', 'rejected', ['a']),
             ('p3', 'proposed', ['b']), ('p3', 'approved', ['b'])])
        self.assertTrue(all(item['scope'] == 'wave' and item['group'] == 'g1' for item in events))


class ApplyRefusalTests(ProposalCase):
    def setUp(self):
        super().setUp()
        self.propose(change('a', prompt=NEW_A), change('b', prompt=NEW_B, files='b.txt,b2.txt'))
        self.before = self.snapshot()

    def refuses(self, fragment, digest=None, pid='p1'):
        with self.assertRaises(proposals.Refused) as caught:
            proposals.apply(self.plan, pid, digest or self.digest(pid))
        self.assertIn(fragment, caught.exception.reason)
        self.assertEqual(self.changed(self.before), [])
        self.assertEqual(self.state(pid)['state'], 'proposed')

    def test_a_wrong_hash_refuses(self):
        self.refuses('hash does not match', digest='0' * 64)
        self.refuses('hash does not match', digest=self.digest()[:-1] + ('0' if self.digest()[-1] != '0' else '1'))
        result = self.cli('apply', self.plan, 'p1', '--approve', 'abc')
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.cli('apply', self.plan, 'p1').returncode, 2)   # --approve is required

    def test_a_target_that_started_since_refuses(self):
        self.start('b')
        self.before = self.snapshot()
        self.refuses('task b has started')

    def test_a_target_edited_since_refuses(self):
        (self.plan / 'tasks' / 'a' / 'prompt.md').write_text('Someone edited this. TASK_a\n')
        self.before = self.snapshot()
        self.refuses('changed since proposal')

    def test_a_files_row_edited_since_refuses_but_another_row_does_not(self):
        text = (self.plan / 'tasks.tsv').read_text()
        (self.plan / 'tasks.tsv').write_text(text.replace('e.txt', 'e2.txt'))
        self.before = self.snapshot()
        proposals.apply(self.plan, 'p1', self.digest())   # the row of e is not a target
        text = (self.plan / 'tasks.tsv').read_text()
        self.assertIn('e2.txt', text)
        self.assertIn('b.txt,b2.txt', text)

    def test_the_row_of_a_target_edited_since_refuses(self):
        text = (self.plan / 'tasks.tsv').read_text()
        (self.plan / 'tasks.tsv').write_text(text.replace('b.txt', 'bb.txt'))
        self.before = self.snapshot()
        self.refuses('changed since proposal')

    def test_a_proposal_that_is_not_proposed_refuses(self):
        proposals.apply(self.plan, 'p1', self.digest())
        after = self.snapshot()
        with self.assertRaises(proposals.Refused) as caught:
            proposals.apply(self.plan, 'p1', self.digest())
        self.assertIn('approved', caught.exception.reason)
        self.assertEqual(self.snapshot(), after)
        proposals.reject  # a rejected one cannot be applied either
        # Apply regenerates a's questions, and --force archives the parked wave question with
        # them. Put that question back so p2 can be proposed from the same q1.
        archived = sorted((self.plan / 'tasks' / 'a' / 'brief.history').glob('*/brief.parked.json'))
        (self.plan / 'tasks' / 'a' / 'brief.parked.json').write_bytes(archived[-1].read_bytes())
        self.propose(change('a', prompt='Again. TASK_a\n'))
        proposals.reject(self.plan, 'p2')
        before = self.snapshot()
        with self.assertRaises(proposals.Refused):
            proposals.apply(self.plan, 'p2', self.digest('p2'))
        self.assertEqual(self.snapshot(), before)

    def test_an_unknown_proposal_is_a_usage_error_listing_the_valid_ones(self):
        for pid in ('p9', '../p1', 'p1/..', ''):
            with self.assertRaises(proposals.NoSuchProposal):
                proposals.apply(self.plan, pid, 'x')
        result = self.cli('apply', self.plan, 'p9', '--approve', 'x')
        self.assertEqual(result.returncode, 2)
        self.assertIn('p1', result.stderr)

    def test_a_files_change_is_refused_while_a_run_is_live_but_a_prompt_change_is_not(self):
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid()}))
        self.before = self.snapshot()
        self.refuses('a run is live')
        self.propose(change('a', prompt='Live edit. TASK_a\n'))
        self.before = self.snapshot()
        proposals.apply(self.plan, 'p2', self.digest('p2'))
        self.assertEqual(self.changed(self.before), ['tasks/a/prompt.md'])


class ValidationTests(ProposalCase):
    def test_group_validation_failing_after_a_files_change_refuses_and_writes_nothing(self):
        # g1 is a review group: two members declaring overlapping files is an error.
        self.propose(change('a', prompt=NEW_A), change('b', files='a.txt'))
        before = self.snapshot()
        with self.assertRaises(proposals.Refused) as caught:
            proposals.apply(self.plan, 'p1', self.digest())
        self.assertIn('overlapping files', caught.exception.reason)
        self.assertEqual(self.changed(before), [])
        self.assertEqual(self.state()['state'], 'proposed')
        self.assertIsNone(forge_groups.check_frozen(self.plan))

    def test_a_write_that_fails_halfway_restores_what_it_wrote(self):
        self.propose(change('a', prompt=NEW_A), change('b', files='b.txt,b2.txt'))
        before = self.snapshot()
        original = forge_groups.freeze

        def broken(plan):
            raise OSError('disk full')
        forge_groups.freeze = broken
        self.addCleanup(setattr, forge_groups, 'freeze', original)
        with self.assertRaises(OSError):
            proposals.apply(self.plan, 'p1', self.digest())
        self.assertEqual(self.changed(before), [])


class RejectTests(ProposalCase):
    def test_reject_changes_nothing_but_the_state(self):
        self.propose(change('a', prompt=NEW_A))
        before = self.snapshot()
        record = proposals.reject(self.plan, 'p1', actor='ada', reason='  not   now ')
        self.assertEqual(record['state'], 'rejected')
        self.assertEqual(self.changed(before), [])
        state = self.state()
        self.assertEqual((state['state'], state['rejected_by'], state['reject_reason']), ('rejected', 'ada', 'not now'))
        with self.assertRaises(proposals.Refused):
            proposals.reject(self.plan, 'p1')
        result = self.cli('reject', self.plan, 'p9')
        self.assertEqual(result.returncode, 2)

    def test_the_cli_rejects_with_a_reason(self):
        self.propose(change('a', prompt=NEW_A))
        result = self.cli('reject', self.plan, 'p1', '--reason', 'no')
        self.assertEqual((result.returncode, result.stdout), (0, 'p1 rejected\n'))
        self.assertEqual(self.state()['reject_reason'], 'no')
        listed = self.cli('proposals', self.plan)
        self.assertIn('rejected', listed.stdout)
        self.assertNotIn('apply:', listed.stdout)


class BriefOffTests(ProposalCase):
    def test_brief_off_is_exit_3_and_dispatches_nothing(self):
        for setup in (lambda: self.env.update(FORGE_BRIEF='off'), lambda: (self.plan / 'brief').write_text('off\n')):
            setup()
            self.arm({'*.qa.*': says(canned(change('a', prompt=NEW_A)))})
            result = self.cli('propose', self.plan, 'a', 'q1')
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertIn('forge brief', result.stderr)
            self.assertEqual(self.calls(), [])
            self.assertFalse((self.plan / 'proposals').exists())
            self.env.pop('FORGE_BRIEF', None)
            (self.plan / 'brief').write_text('advisory\n')

    def test_brief_change_follows_the_brief_settings(self):
        self.assertEqual(forge_side.allowed('brief-change', self.plan), (True, None))
        (self.plan / 'brief_model').unlink()
        self.assertEqual(forge_side.allowed('brief-change', self.plan), (False, 'no-model'))
        (self.plan / 'brief_model').write_text('sol\n')
        os.environ['FORGE_BRIEF'] = 'off'
        self.assertEqual(forge_side.allowed('brief-change', self.plan), (False, 'env'))
        del os.environ['FORGE_BRIEF']
        for number in range(1, 16):   # max(10, 3 x 5 tasks)
            (self.plan / 'side' / 'brief-change' / 'z' / str(number)).mkdir(parents=True)
        self.assertEqual(forge_side.allowed('brief-change', self.plan), (False, 'cap'))
        self.assertEqual(forge_side.allowed('brief', self.plan), (True, None))   # each kind has its own cap
        with self.assertRaises(ValueError):
            forge_side.allowed('nope', self.plan)

    def test_a_never_send_repository_gets_no_brief_change_dispatch(self):
        original = forge_side.privacy_floor
        forge_side.privacy_floor = lambda repo: True
        self.addCleanup(setattr, forge_side, 'privacy_floor', original)
        self.assertEqual(forge_side.allowed('brief-change', self.plan), (False, 'never-send'))

    def test_a_plan_that_is_not_one_is_exit_3(self):
        result = self.cli('propose', self.root / 'no-plan', 'a', 'q1')
        self.assertEqual(result.returncode, 3)
        self.assertEqual(self.cli('proposals', self.root / 'no-plan').returncode, 3)


class ParkedAnswerTests(ProposalCase):
    def parked_answers(self, task='a'):
        return json.loads((self.plan / 'tasks' / task / 'brief.parked.answers.json').read_text())

    def test_a_wave_answer_is_recorded_beside_the_parked_file_and_prints_the_propose_command(self):
        result = self.cli('answer', self.plan, 'a', 'q1', 'use postgres', '--actor', 'ada')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('forge brief propose %s a q1' % self.plan, result.stdout)
        entry = self.parked_answers()['q1']
        self.assertEqual((entry['text'], entry['actor']), ('use postgres', 'ada'))
        self.assertIsInstance(entry['t'], float)
        self.assertFalse((self.plan / 'tasks' / 'a' / 'brief.answers.json').exists())
        self.assertFalse((self.plan / 'tasks' / 'a' / 'brief.late.json').exists())

    def test_an_answer_never_changes_a_prompt_or_the_plan(self):
        before = self.snapshot()
        self.answer('a', 'q1', 'something else')
        self.assertEqual(self.changed(before), ['control.log', 'tasks/a/brief.parked.answers.json'])

    def test_a_plan_question_needs_brief_scope_plan(self):
        with self.assertRaises(answers.NoSuchQuestion):
            self.answer('a', 'q2', 'yes')   # brief scope is wave
        (self.plan / 'brief_scope').write_text('plan\n')
        result = self.answer('a', 'q2', 'yes')
        self.assertTrue(result['parked'])
        self.assertEqual(self.parked_answers()['q2']['text'], 'yes')

    def test_with_brief_scope_task_a_parked_question_is_still_unknown(self):
        (self.plan / 'brief_scope').write_text('task\n')
        with self.assertRaises(answers.NoSuchQuestion) as caught:
            self.answer('a', 'q1', 'sqlite')
        self.assertEqual(caught.exception.valid, [])

    def test_an_answer_to_a_task_question_is_as_it_was(self):
        brief = {'questions': [dict(question('Port?'), id='q3')], 'model': 'sol', 'generated_at': 't'}
        (self.plan / 'tasks' / 'b' / 'brief.json').write_text(json.dumps(brief) + '\n')
        result = self.answer('b', 'q3', '443')
        self.assertEqual((result['parked'], result['late'], result['message']), (False, False, 'b/q3 answered\n'))
        self.assertEqual(json.loads((self.plan / 'tasks' / 'b' / 'brief.answers.json').read_text())['q3']['text'], '443')
        self.assertFalse((self.plan / 'tasks' / 'b' / 'brief.parked.answers.json').exists())

    def test_a_parked_answer_is_still_recorded_once_the_task_started(self):
        self.start('a', 'PASS')
        result = self.answer('a', 'q1', 'late but about the siblings')
        self.assertEqual((result['parked'], result['late']), (True, False))

    def test_an_answer_older_than_its_question_is_not_proposed(self):
        parked = json.loads((self.plan / 'tasks' / 'a' / 'brief.parked.json').read_text())
        parked['generated_at'] = '2099-01-01T00:00:00Z'
        (self.plan / 'tasks' / 'a' / 'brief.parked.json').write_text(json.dumps(parked) + '\n')
        with self.assertRaises(proposals.Refused) as caught:
            proposals.propose(self.plan, 'a', 'q1')
        self.assertIn('answer it again', caught.exception.reason)


if __name__ == '__main__':
    unittest.main()
