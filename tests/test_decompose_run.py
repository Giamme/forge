"""Recursive decomposition end to end (#153): a split task runs its subtasks as a plan of its own.

Drives forge-parallel.sh with the fake agent CLI from test_forge.py. The fake edits a.txt, b.txt,
c.txt or d.txt by the first TASK_<x> marker it finds in the prompt, looking for `a` first. A
subtask's prompt carries its parent's, so the parent here is always task `d` (it owns a.txt, b.txt
and d.txt) and its subtasks are `a` and `b`: each prompt then resolves to its own file. A planner
asked about `d` reads d's prompt, so it answers on the `d.qa.<n>` counter, with d's reviews.

The node model, the limits and the validation of a split are in test_decompose.py; the planner's
side is in test_planner.py. Fixtures are borrowed, not inherited.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import sys
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_decompose  # noqa: E402

SAME = '- [P1][CONFIRMED] d.txt:1 — broken'
CRITERIA = '\n\n## Acceptance criteria\n\n- the file exists'
REASON = 'three attempts failed the same way; two smaller pieces'


def child(tid, deps=(), files=None, **more):
    return dict({'id': tid, 'deps': list(deps), 'difficulty': 'low', 'files': files or ['%s.txt' % tid],
                 'title': 'Part %s' % tid, 'approach': '', 'prompt': 'Do one part. TASK_%s%s' % (tid, CRITERIA)}, **more)


def block(*tasks, reason='two independent halves'):
    return 'The split.\n```forge-plan\n%s\n```\n' % json.dumps({'reason': reason, 'tasks': list(tasks)})


class SplitCase(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def forge(self, *args):
        r = self.run_script('forge-parallel.sh', *args)
        for broken in ('syntax error', 'unbound variable', 'command not found', 'Traceback'):
            self.assertNotIn(broken, r.stdout)
        return r

    def plan_dir(self, name='plan', second=True):
        """d owns a.txt, b.txt and d.txt; c depends on d."""
        p = self.root / name
        p.mkdir()
        rows = 'd\t-\tlow\ta.txt,b.txt,d.txt\tsol\topus\tThe whole of D\n'
        if second:
            rows += 'c\td\tlow\tc.txt\tsol\topus\tC\n'
        (p / 'tasks.tsv').write_text(rows)
        for i in ('d', 'c') if second else ('d',):
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        return p

    def planned(self, *flags, second=True):
        p = self.plan_dir(second=second)
        r = self.forge('plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def stopped(self, *flags, fails=3, more=None, run_flags=(), second=True):
        """d fails `fails` reviews on the only model its tier has: it stops for a decision, on `split`."""
        p = self.planned('--dwarf-low', 'sol', *flags, second=second)
        (p / 'converge_window').write_text('9\n')
        table = {'d.qa.%d' % n: {'verdict': 'FAIL', 'findings': SAME} for n in range(1, fails + 1)}
        table.update({'d.dwarf.%d' % n: {'content': 'try %d' % n} for n in range(1, 9)})
        table.update(more or {})
        self.scenario(table)
        r = self.forge('run', p, '--retry-failed', 5, *run_flags)
        return p, r

    def subtasks(self, *tasks, name='subtasks.md'):
        path = self.root / name
        path.write_text(block(*(tasks or (child('a'), child('b')))))
        return path

    def status(self, p, task):
        return forge_decompose.status_of(p, task)

    def children(self, sub):
        return sorted(x.name for x in (sub / 'tasks').iterdir() if (x / 'prompt.md').exists())

    def split(self, p, *args, decide='split', subtasks=None):
        return self.forge('retry', p, 'd', '--decide', decide, '--subtasks', subtasks or self.subtasks(),
                          '--reason', REASON, '--approved', *args)

    def branch_files(self, branch):
        return sorted(self.git('ls-tree', '-r', '--name-only', branch).decode().split())

    def run_id(self, p):
        return (p / 'run_id').read_text().strip()

    def node(self, p, task):
        return forge_decompose.node_of(p, task)


class SplitOfAStoppedTaskTests(SplitCase):
    def test_one_approved_choice_splits_it_and_the_parent_is_reviewed_on_the_whole_diff(self):
        p, r = self.stopped()
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.status(p, 'd'), 'NEEDS_DECISION')
        decision = (p / 'tasks/d/decision.md').read_text()
        self.assertEqual(decision.splitlines()[0], 'reasons: split')
        self.assertIn('--decide split', decision)
        self.assertIn('decomposition limits: depth 2', decision)
        table = (p / 'tasks.tsv').read_bytes()
        before = self.calls()

        r = self.split(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'tasks.tsv').read_bytes(), table)      # the plan's table was not edited
        sub = p / 'tasks/d/sub'
        self.assertEqual([row['id'] for row in forge_decompose.read_rows(sub)], ['a', 'b'])
        self.assertEqual((sub / 'run_id').read_text().strip(), self.run_id(p) + '--d')
        # Each subtask was implemented and reviewed once, and the parent was only reviewed.
        self.assertEqual(self.calls()[len(before):].count('dwarf'), 2)
        self.assertEqual(self.calls()[len(before):].count('qa'), 3)
        self.assertEqual(self.status(sub, 'a'), 'MERGED')
        self.assertEqual(self.status(sub, 'b'), 'MERGED')
        self.assertEqual(self.status(p, 'd'), 'MERGED')
        # The parent's review read the cumulative diff: its own earlier work and both subtasks'.
        review = (p / 'tasks/d/qa.input').read_text()
        for name in ('a.txt', 'b.txt', 'd.txt'):
            self.assertIn('+++ b/' + name, review)
        self.assertIn('REQUIREMENT_SENTINEL TASK_d', review)
        self.assertEqual(self.branch_files('forge/%s-integration' % self.run_id(p)), ['a.txt', 'b.txt', 'base.txt', 'd.txt'])
        # The decision is recorded as every decision is, and on the node.
        row = (p / 'tasks/d/decisions.tsv').read_text().splitlines()[-1].split('\t')
        self.assertEqual(row[2:], ['retry=1 split', REASON])
        node = self.node(p, 'd')
        self.assertEqual((node['decision'], node['by'], node['reason']), ('split', 'user', REASON))
        self.assertEqual(self.node(sub, 'a')['decision'], 'atomic')
        self.assertTrue((p / 'tasks/d/sub.merged').exists())
        self.assertFalse((p / 'tasks/d/decision.md').exists())

    def test_a_subtask_is_told_the_task_it_is_part_of_and_what_the_last_review_found(self):
        p, _ = self.stopped()
        self.assertEqual(self.split(p).returncode, 0)
        prompt = (p / 'tasks/d/sub/tasks/a/prompt.md').read_text()
        self.assertIn('Do one part. TASK_a', prompt)
        self.assertIn('## The task this is a part of', prompt)
        self.assertIn('REQUIREMENT_SENTINEL TASK_d', prompt)
        self.assertIn('## The work so far, and its last review', prompt)
        self.assertIn('d.txt:1', prompt)

    def test_the_rest_of_the_plan_runs_once_the_split_task_has_merged(self):
        p, _ = self.stopped()
        self.assertEqual(self.status(p, 'c'), 'BLOCKED')
        self.assertEqual(self.split(p).returncode, 0)
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p, 'c'), 'MERGED')
        self.assertEqual(self.branch_files('forge/%s-integration' % self.run_id(p)),
                         ['a.txt', 'b.txt', 'base.txt', 'c.txt', 'd.txt'])

    def test_the_split_needs_a_reason_the_approval_and_the_subtasks(self):
        p, _ = self.stopped()
        before = self.calls()
        for args in (('--decide', 'split', '--subtasks', self.subtasks(), '--approved'),
                     ('--decide', 'split', '--subtasks', self.subtasks(), '--reason', REASON),
                     ('--decide', 'split', '--reason', REASON, '--approved'),
                     ('--decide', 'split', '--subtasks', self.root / 'missing.md', '--reason', REASON, '--approved'),
                     ('--decide', 'split', '--subtasks', self.subtasks(), '--dwarf', 'opus', '--reason', REASON, '--approved')):
            with self.subTest(args=args[1:4]):
                r = self.forge('retry', p, 'd', *args)
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertEqual(self.status(p, 'd'), 'NEEDS_DECISION')
                self.assertFalse((p / 'tasks/d/sub').exists())
        self.assertEqual(self.calls(), before)

    def test_a_wrong_split_is_refused_and_changes_nothing(self):
        p, _ = self.stopped()
        before = self.calls()
        outside = self.subtasks(child('a'), child('b', files=['elsewhere.txt']), name='outside.md')
        r = self.split(p, subtasks=outside)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn("outside its parent's files", r.stdout)
        cycle = self.subtasks(child('a', deps=['b']), child('b', deps=['a']), name='cycle.md')
        r = self.split(p, subtasks=cycle)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('cycle', r.stdout)
        self.assertEqual(self.status(p, 'd'), 'NEEDS_DECISION')
        self.assertFalse((p / 'tasks/d/sub').exists())
        self.assertNotEqual(self.node(p, 'd').get('decision'), 'split')
        self.assertEqual(self.calls(), before)

    def test_an_answer_of_one_task_keeps_it_whole_and_is_recorded_with_its_reason(self):
        p, _ = self.stopped()
        one = self.root / 'one.md'
        one.write_text(block(child('a', files=['a.txt', 'b.txt', 'd.txt']), reason='it is one change to one function'))
        r = self.split(p, subtasks=one)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('kept whole', r.stdout)
        self.assertEqual(self.status(p, 'd'), 'NEEDS_DECISION')
        node = self.node(p, 'd')
        self.assertEqual(node['decision'], 'atomic')
        self.assertFalse((p / 'tasks/d/sub').exists())

    def test_fresh_starts_from_the_base_and_keeps_the_earlier_work_on_a_ref(self):
        p, _ = self.stopped()
        tip = self.git('rev-parse', 'forge/%s/d' % self.run_id(p)).decode().strip()
        r = self.split(p, decide='split=fresh')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.git('rev-parse', 'refs/forge/kept/%s/d' % self.run_id(p)).decode().strip(), tip)
        self.assertEqual((p / 'tasks/d/sub.kept').read_text().strip(), tip)
        # The parent's own attempt is not part of the new work: only the subtasks' files are.
        self.assertEqual(self.branch_files('forge/%s-integration' % self.run_id(p)), ['a.txt', 'b.txt', 'base.txt'])
        review = (p / 'tasks/d/qa.input').read_text()
        self.assertNotIn('+++ b/d.txt', review)
        self.assertEqual((p / 'tasks/d/decisions.tsv').read_text().splitlines()[-1].split('\t')[2], 'retry=1 split=fresh')
        self.assertNotIn('The work so far', (p / 'tasks/d/sub/tasks/a/prompt.md').read_text())

    def test_a_task_that_only_failed_can_be_split_too(self):
        p = self.planned()
        self.scenario({'d.qa.1': {'verdict': 'FAIL', 'findings': SAME}})
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.status(p, 'd'), 'FAIL')
        r = self.split(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p, 'd'), 'MERGED')

    def test_a_merged_task_is_not_split(self):
        p = self.planned()
        self.assertEqual(self.forge('run', p).returncode, 0)
        r = self.split(p)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertFalse((p / 'tasks/d/sub').exists())


class FailingChildTests(SplitCase):
    def failing(self):
        """The split is made, and subtask a's review fails: d waits for it."""
        p, _ = self.stopped(more={'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — wrong'},
                                  'a.dwarf.2': {'content': 'fixed'}})
        attempt = (p / 'tasks/d/attempt').read_text().strip()
        r = self.split(p)
        return p, r, attempt

    def test_the_parent_waits_and_spends_no_attempt(self):
        p, r, attempt = self.failing()
        self.assertEqual(r.returncode, 5, r.stdout)
        sub = p / 'tasks/d/sub'
        self.assertEqual(self.status(sub, 'a'), 'FAIL')
        self.assertEqual(self.status(sub, 'b'), 'MERGED')
        self.assertEqual(self.status(p, 'd'), 'BLOCKED')
        self.assertEqual((p / 'tasks/d/attempt').read_text().strip(), attempt)
        self.assertIn('a', (p / 'tasks/d/sub.waiting').read_text())
        self.assertFalse((p / 'tasks/d/sub.merged').exists())
        self.assertIn('waiting for its subtasks', r.stdout)
        # Nothing of the parent was reviewed: its last review is still the one before the split.
        self.assertEqual(self.calls().count('qa'), 3 + 2)

    def test_a_run_says_what_it_waits_for_and_the_retried_child_lets_it_finish(self):
        p, _, _ = self.failing()
        sub = p / 'tasks/d/sub'
        rows = (sub / 'tasks.tsv').read_bytes()
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn('waiting for their subtasks:', r.stdout)
        self.assertEqual(self.status(p, 'd'), 'BLOCKED')
        before = self.calls()
        r = self.forge('retry', sub, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(sub, 'a'), 'MERGED')
        self.assertEqual(self.status(p, 'd'), 'BLOCKED')    # the parent is its plan's to finish
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p, 'd'), 'MERGED')
        self.assertEqual(self.status(p, 'c'), 'MERGED')
        # Resume made no child twice: the same table, and b was never dispatched again.
        self.assertEqual((sub / 'tasks.tsv').read_bytes(), rows)
        self.assertEqual(self.children(sub), ['a', 'b'])
        after = self.calls()[len(before):]
        self.assertEqual(after.count('dwarf'), 2)          # a again, then c
        self.assertEqual(after.count('qa'), 3)             # a, the whole of d, c

    def test_a_run_with_a_retry_budget_retries_the_child_itself(self):
        p, _ = self.stopped(more={'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — wrong'},
                                  'a.dwarf.2': {'content': 'fixed'}})
        r = self.split(p, '--no-dispatch')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertFalse((p / 'tasks/d/sub/wt_root').exists())
        r = self.forge('run', p, '--retry-failed', 2)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p / 'tasks/d/sub', 'a'), 'MERGED')
        self.assertEqual((p / 'tasks/d/sub/tasks/a/attempt').read_text().strip(), '2')
        self.assertEqual(self.status(p, 'd'), 'MERGED')

    def test_a_second_split_of_the_same_task_is_refused(self):
        p, _, _ = self.failing()
        r = self.split(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('already split', r.stdout)
        self.assertEqual(self.children(p / 'tasks/d/sub'), ['a', 'b'])


class RetryOfTheParentTests(SplitCase):
    def test_a_parent_whose_whole_review_fails_is_retried_as_any_task(self):
        # Review 4 of d is the one of the whole task, after its subtasks merged.
        p, _ = self.stopped(more={'d.qa.4': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — the halves disagree'}})
        r = self.split(p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.status(p, 'd'), 'FAIL')
        self.assertTrue((p / 'tasks/d/sub.merged').exists())
        before = self.calls()
        r = self.forge('retry', p, 'd')
        self.assertEqual(r.returncode, 0, r.stdout)
        # An ordinary attempt: d's own implementer on d's branch, then its review. No subtask ran again.
        self.assertEqual(self.calls()[len(before):], ['dwarf', 'qa'])
        self.assertEqual(self.status(p, 'd'), 'MERGED')
        self.assertIn('the halves disagree', (p / 'tasks/d/dwarf.prompt').read_text())
        self.assertEqual(self.children(p / 'tasks/d/sub'), ['a', 'b'])
        review = (p / 'tasks/d/qa.input').read_text()
        for name in ('a.txt', 'b.txt', 'd.txt'):
            self.assertIn('+++ b/' + name, review)


class AutoSplitTests(SplitCase):
    def test_the_flag_needs_a_planner_to_ask(self):
        p = self.planned()
        r = self.forge('run', p, '--auto-split')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('--split-planner', r.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_task_that_stops_on_split_is_split_by_the_planner_without_waiting(self):
        # d's fourth read-only call is the planner's, its fifth the review of the whole task.
        p, r = self.stopped(more={'d.qa.4': {'findings': block(child('a'), child('b')), 'verdict': 'none'}},
                            run_flags=('--auto-split', '--split-planner', 'sol'))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(p, 'd'), 'MERGED')
        self.assertEqual(self.status(p, 'c'), 'MERGED')
        node = self.node(p, 'd')
        self.assertEqual((node['decision'], node['by']), ('split', 'auto'))
        self.assertIn('auto-split', node['reason'])
        self.assertEqual((p / 'tasks/d/decisions.tsv').read_text().splitlines()[-1].split('\t')[2], 'retry=1 split')
        self.assertIn('TASK_d', (p / 'tasks/d/split/planner.prompt').read_text())

    def test_a_planner_that_keeps_it_whole_leaves_the_task_waiting_and_is_asked_once(self):
        one = block(child('a', files=['a.txt', 'b.txt', 'd.txt']), reason='one change')
        p, r = self.stopped(more={'d.qa.4': {'findings': one, 'verdict': 'none'}},
                            run_flags=('--auto-split', '--split-planner', 'sol'))
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.status(p, 'd'), 'NEEDS_DECISION')
        self.assertEqual(self.node(p, 'd')['decision'], 'atomic')
        self.assertEqual(self.node(p, 'd')['reason'], 'one change')
        before = self.calls()
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.calls(), before)       # auto-split is still on, and nobody is asked twice

    def test_without_the_flag_the_task_waits_and_no_planner_is_asked(self):
        p, r = self.stopped()
        self.assertEqual(self.status(p, 'd'), 'NEEDS_DECISION')
        self.assertEqual(self.calls().count('qa'), 3)
        self.assertFalse((p / 'tasks/d/split').exists())


class LimitTests(SplitCase):
    def test_the_table_shows_the_limits_and_the_first_run_freezes_them(self):
        p = self.plan_dir()
        r = self.forge('plan', p, '--repo', self.repo, '--no-memory', '--split-depth', 1, '--split-children', 2)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('decomposition limits: depth 1, at most 2 subtasks per split, 24 subtasks in the whole run', r.stdout)
        self.assertIn('waits for your choice', r.stdout)
        r = self.forge('plan', p, '--repo', self.repo, '--no-memory', '--split-children', 3)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('at most 3 subtasks per split', r.stdout)
        self.assertEqual(self.forge('run', p).returncode, 0)
        self.assertIn('(frozen)', self.forge('plan', p, '--repo', self.repo, '--no-memory').stdout)
        r = self.forge('plan', p, '--repo', self.repo, '--no-memory', '--split-depth', 3)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('frozen', r.stdout)
        self.assertEqual(forge_decompose.limits(p)['depth'], 1)

    def test_a_limit_out_of_range_is_refused_before_anything_is_written(self):
        p = self.plan_dir()
        r = self.forge('plan', p, '--repo', self.repo, '--no-memory', '--split-depth', 9)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse((p / 'waves.tsv').exists())

    def test_a_split_past_a_limit_is_recorded_as_bounded_and_the_task_stays(self):
        p, _ = self.stopped('--split-children', 2)
        three = self.subtasks(child('a'), child('b'), child('x', files=['d.txt']), name='three.md')
        before = self.calls()
        r = self.split(p, subtasks=three)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('the limit is 2 per split', r.stdout)
        node = self.node(p, 'd')
        self.assertEqual((node['decision'], node['limits_used']['limit']), ('bounded', 'children'))
        self.assertEqual(self.status(p, 'd'), 'NEEDS_DECISION')
        self.assertFalse((p / 'tasks/d/sub').exists())
        self.assertEqual(self.calls(), before)
        # Within the limit, the same task still splits.
        self.assertEqual(self.split(p).returncode, 0)

    def test_depth_zero_turns_splitting_off(self):
        p, _ = self.stopped('--split-depth', 0)
        r = self.split(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(self.node(p, 'd')['decision'], 'bounded')


class PlannedSplitTests(SplitCase):
    def proposed(self, *flags, nested=False):
        """A plan whose planner proposed d's subtasks: what forge-planner.py parse leaves for `plan`."""
        p = self.plan_dir()
        first = child('a')
        if nested:
            # Both inner prompts carry TASK_a (their parent's), so the scenario says what each writes.
            first = child('a', files=['a.txt', 'd.txt'], split={'reason': 'two steps', 'tasks': [
                child('one', files=['a.txt']), child('two', deps=['one'], files=['d.txt'])]})
            self.scenario({'a.dwarf.1': {'content': 'one'}, 'a.dwarf.2': {'no_edit': True, 'files': {'d.txt': 'two\n'}}})
        (p / 'tasks/d/split.proposal.json').write_text(json.dumps({'reason': 'two halves', 'tasks': [first, child('b')]}))
        (p / 'tasks/c/split.reason').write_text('one file, one change\n')
        (p / 'planner.prompt').write_text('the planner was asked\n')
        r = self.forge('plan', p, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p, r

    def test_plan_makes_the_sub_plan_and_shows_the_tree_before_approval(self):
        p, r = self.proposed()
        self.assertIn('subtasks (a split task runs its subtasks as a plan of its own', r.stdout)
        self.assertIn('two halves', r.stdout)
        self.assertEqual(self.calls(), [])
        node = self.node(p, 'd')
        self.assertEqual((node['decision'], node['by'], node['reason']), ('split', 'planner', 'two halves'))
        kept = self.node(p, 'c')
        self.assertEqual((kept['decision'], kept['by'], kept['reason']), ('atomic', 'planner', 'one file, one change'))
        self.assertTrue((p / 'tasks/d/sub/waves.tsv').exists())
        again = self.forge('plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual(self.children(p / 'tasks/d/sub'), ['a', 'b'])

    def test_the_run_dispatches_the_subtasks_and_reviews_the_parent_on_all_of_it(self):
        p, _ = self.proposed()
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        # a, b and c were implemented; d never had an implementer of its own. Four reviews: a, b, d, c.
        self.assertEqual(self.calls().count('dwarf'), 3)
        self.assertEqual(self.calls().count('qa'), 4)
        self.assertFalse((p / 'tasks/d/dwarf.prompt').exists())
        review = (p / 'tasks/d/qa.input').read_text()
        self.assertIn('+++ b/a.txt', review)
        self.assertIn('+++ b/b.txt', review)
        self.assertEqual(self.branch_files('forge/%s-integration' % self.run_id(p)), ['a.txt', 'b.txt', 'base.txt', 'c.txt'])
        self.assertEqual((p / 'tasks/d/attempt').read_text().strip(), '1')

    def test_a_subtask_split_again_runs_two_levels_down(self):
        p, r = self.proposed(nested=True)
        inner = p / 'tasks/d/sub/tasks/a/sub'
        self.assertEqual([row['id'] for row in forge_decompose.read_rows(inner)], ['one', 'two'])
        self.assertEqual((inner / 'run_id').read_text().strip(), self.run_id(p) + '--d--a')
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.status(inner, 'two'), 'MERGED')
        self.assertEqual(self.status(p / 'tasks/d/sub', 'a'), 'MERGED')
        self.assertEqual(self.status(p, 'd'), 'MERGED')
        self.assertEqual(self.branch_files('forge/%s-integration' % self.run_id(p)),
                         ['a.txt', 'b.txt', 'base.txt', 'c.txt', 'd.txt'])
        tree = forge_decompose.tree(p)
        self.assertEqual(sorted((n['level'], n['task']) for n in tree),
                         [(0, 'c'), (0, 'd'), (1, 'a'), (1, 'b'), (2, 'one'), (2, 'two')])

    def test_a_proposal_past_the_depth_limit_runs_the_task_whole(self):
        p, r = self.proposed('--split-depth', 0)
        self.assertEqual(self.node(p, 'd')['decision'], 'bounded')
        self.assertFalse((p / 'tasks/d/sub').exists())
        self.assertIn('bounded', r.stdout)
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/d/dwarf.prompt').exists())

    def test_one_bound_covers_the_whole_tree(self):
        # Two independent subtasks under --max-parallel 1 never work at the same time.
        p, _ = self.proposed()
        self.env['EVENTS'] = str(self.root / 'events')
        self.scenario({'a.dwarf.*': {'sleep': 1}, 'b.dwarf.*': {'sleep': 1}})
        r = self.forge('run', p, '--max-parallel', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        events = [line.split() for line in (self.root / 'events').read_text().splitlines()]
        order = [(name, role) for name, role, _ in events]
        first = order[0][0]
        self.assertEqual(order[:2], [(first, 'dwarf'), (first, 'qa')])
        self.assertLessEqual(len(list((p / 'tree.slots').glob('*'))), 1)

    def test_integrate_refuses_a_sub_plan(self):
        p, _ = self.proposed()
        self.assertEqual(self.forge('run', p).returncode, 0)
        r = self.forge('integrate', p / 'tasks/d/sub', '--approved')
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn('sub-plan', r.stdout)


class ChainedGoalTests(SplitCase):
    def test_next_plans_the_following_stage_and_integrate_on_the_last_brings_the_chain(self):
        p = self.planned(second=False)
        (p / 'planner').write_text('sol\n')
        (p / 'later.json').write_text(json.dumps({'later': [{'title': 'The second stage', 'goal': 'Add c on top of d.'}]}))
        r = self.forge('next', p)
        self.assertEqual(r.returncode, 3, r.stdout)        # the stage before it has not merged
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('this plan is one stage of a larger goal', r.stdout)
        self.assertIn('forge-parallel.sh next', r.stdout)
        stage = {'id': 'c', 'deps': [], 'difficulty': 'low', 'files': ['c.txt'], 'title': 'Add c', 'approach': '',
                 'prompt': 'Implement change. TASK_c' + CRITERIA}
        self.scenario({'change.qa.*': {'findings': 'Plan.\n```forge-plan\n%s\n```\n' % json.dumps({'tasks': [stage]}),
                                       'verdict': 'none'}})
        r = self.forge('next', p, '--approved')
        self.assertEqual(r.returncode, 2, r.stdout)
        r = self.forge('next', p, '--dwarf', 'sol', '--qa', 'opus', '--explore', '0', '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        new = Path(str(p) + '-2')
        self.assertTrue((new / 'tasks.tsv').exists(), r.stdout)
        chain = json.loads((new / 'chain.json').read_text())
        self.assertEqual((chain['stage'], Path(chain['previous']).name), (2, p.name))
        self.assertIn('Add c on top of d.', (new / 'request.md').read_text())
        self.assertEqual((new / 'after').read_text().strip(), str(p))
        r = self.forge('run', new)
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.forge('integrate', new, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(sorted(x.name for x in self.repo.iterdir() if x.suffix == '.txt'), ['base.txt', 'c.txt', 'd.txt'])


if __name__ == '__main__':
    unittest.main()
