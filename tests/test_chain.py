"""`plan --after`: a plan that starts from another plan's integration branch (#143).

Fixtures are borrowed from test_forge.py (the fake agent CLI), not inherited: subclassing
ForgeTests would run all of its tests again under this class. The fake edits a.txt, b.txt, c.txt or
d.txt, by the TASK_<x> marker in the prompt, so each plan below uses its own task ids.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import re
import subprocess
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
P1 = '- [P1][CONFIRMED] a.txt:1 — realistic input breaks (x)'
MOVED = 'moved since this plan was made'


class ChainCase(unittest.TestCase):
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

    def make(self, name, ids, *flags, repo=None):
        """A plan dir with one independent low task per id, planned with `flags`. Returns (plan, result)."""
        p = self.root / name
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join('%s\t-\tlow\t%s.txt\tsol\topus\t%s\n' % (i, i, i.upper()) for i in ids))
        for i in ids:
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        return p, self.forge('plan', p, '--repo', repo or self.repo, '--no-memory', *flags)

    def finished(self, name, ids, *flags):
        p, r = self.make(name, ids, *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.forge('run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def text(self, plan, name):
        return (plan / name).read_text().strip()

    def tip(self, plan):
        return self.git('rev-parse', 'forge/%s-integration' % plan.name).decode().strip()

    def listing(self, plan):
        # dataset.uid is the dataset layer's, written for any command that names a plan
        return sorted(str(x.relative_to(plan)) for x in plan.rglob('*') if x.name != 'dataset.uid')

    def branches(self):
        return self.git('branch', '--list', 'forge/*').decode()

    def refused(self, plan, *args):
        """plan --after refused with exit 2 and nothing written: no file in the plan, no branch."""
        before, branches = self.listing(plan), self.branches()
        r = self.forge('plan', plan, '--repo', self.repo, '--no-memory', *args)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertFalse((plan / 'after').exists())
        self.assertFalse((plan / 'integration.base').exists())
        self.assertEqual(self.listing(plan), before)
        self.assertEqual(self.branches(), branches)
        return r.stdout

    def raw_plan(self, name, ids):
        """A plan dir that has not been planned: what plan --after is refused on, or applied to."""
        p = self.root / name
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join('%s\t-\tlow\t%s.txt\tsol\topus\t%s\n' % (i, i, i.upper()) for i in ids))
        for i in ids:
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        return p

    def hand_commit_and_adopt(self, plan):
        """A commit on the plan's integration branch, adopted: accepted.integration moves with it."""
        wt = Path(self.text(plan, 'wt_root')) / '_integration'
        (wt / 'fix.txt').write_text('a fix\n')
        for cmd in (('add', 'fix.txt'), ('commit', '-qm', 'a fix by hand')):
            subprocess.check_call(['git', *cmd], cwd=wt, env=self.env)
        r = self.forge('adopt', plan, '--head', '--reason', 'moved on purpose')
        self.assertEqual(r.returncode, 0, r.stdout)


class PlanAfterTests(ChainCase):
    def test_records_the_chain_creates_the_branch_and_shows_it(self):
        a = self.finished('first', 'a')
        base = self.text(a, 'accepted.integration')
        self.assertEqual(self.tip(a), base)
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(b, 'after'), str(a))
        self.assertEqual(self.text(b, 'integration.base'), base)
        self.assertEqual(self.tip(b), base)
        self.assertIn('after: first (%s)' % base[:12], r.stdout)
        # a plan made from the user's HEAD has no such line, and no warning about HEAD either
        _, plain = self.make('plain', 'c')
        self.assertNotIn('after:', plain.stdout)

    def test_run_builds_on_the_first_plans_work(self):
        a = self.finished('first', 'a')
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        r = self.forge('run', b)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((b / 'tasks/b/merged').exists())
        self.assertEqual(self.text(b, 'tasks/b/base_ref'), self.text(a, 'accepted.integration'))
        shown = self.git('ls-tree', '--name-only', 'forge/second-integration').decode().split()
        self.assertIn('a.txt', shown)
        self.assertIn('b.txt', shown)
        # the first plan's branch is where it was, and the user's checkout has neither file yet
        self.assertEqual(self.tip(a), self.text(a, 'accepted.integration'))
        self.assertFalse((self.repo / 'a.txt').exists())

    def test_uncommitted_changes_warning_does_not_apply_to_a_chained_plan(self):
        a = self.finished('first', 'a')
        (self.repo / 'base.txt').write_text('edited\n')
        _, plain = self.make('plain', 'c')
        self.assertIn('uncommitted changes', plain.stdout)
        _, chained = self.make('second', 'b', '--after', str(a))
        self.assertEqual(chained.returncode, 0, chained.stdout)
        self.assertNotIn('uncommitted changes', chained.stdout)

    def test_refuses_a_plan_with_a_task_that_is_not_merged(self):
        self.scenario({'a.qa.*': {'verdict': 'FAIL', 'findings': P1}})
        p, r = self.make('first', 'ab')
        self.assertEqual(self.forge('run', p).returncode, 5)
        b = self.raw_plan('second', 'c')
        out = self.refused(b, '--after', str(p))
        self.assertIn('not merged', out)
        self.assertIn('a (FAIL)', out)
        self.assertNotIn('b (', out)       # b merged

    def test_refuses_another_repository(self):
        other = self.root / 'other'
        other.mkdir()
        for cmd in (('init', '-q'), ('commit', '-q', '--allow-empty', '-m', 'initial')):
            subprocess.check_call(['git', *cmd], cwd=other, env=self.env)
        a, r = self.make('first', 'a', repo=other)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.forge('run', a).returncode, 0)
        out = self.refused(self.raw_plan('second', 'b'), '--after', str(a))
        self.assertIn('another repository', out)

    def test_refuses_what_is_not_a_plan_dir(self):
        b = self.raw_plan('second', 'b')
        self.assertIn('not a directory', self.refused(b, '--after', str(self.root / 'missing')))
        empty = self.root / 'empty'
        empty.mkdir()
        self.assertIn('not a plan directory', self.refused(b, '--after', str(empty)))
        self.assertIn('--after needs a plan directory', self.refused(b, '--after'))

    def test_refuses_a_plan_after_itself(self):
        b = self.raw_plan('second', 'b')
        self.assertIn('after itself', self.refused(b, '--after', str(b)))

    def test_refuses_an_integration_branch_that_moved_past_what_the_plan_accepted(self):
        a = self.finished('first', 'a')
        wt = Path(self.text(a, 'wt_root')) / '_integration'
        (wt / 'fix.txt').write_text('a fix\n')
        for cmd in (('add', 'fix.txt'), ('commit', '-qm', 'a fix by hand')):
            subprocess.check_call(['git', *cmd], cwd=wt, env=self.env)
        out = self.refused(self.raw_plan('second', 'b'), '--after', str(a))
        self.assertIn('adopt', out)
        self.assertIn('forge/first-integration', out)

    def test_refuses_a_missing_integration_branch(self):
        a = self.finished('first', 'a')
        wt = Path(self.text(a, 'wt_root')) / '_integration'
        self.git('worktree', 'remove', '--force', str(wt))
        self.git('branch', '-D', 'forge/first-integration')
        self.assertIn('is missing', self.refused(self.raw_plan('second', 'b'), '--after', str(a)))

    def test_refuses_a_plan_whose_scheduler_is_live(self):
        a = self.finished('first', 'a')
        (a / 'scheduler.live').write_text(json.dumps({'pid': os.getpid(), 'started': '2026-11-15T10:00:00Z'}))
        out = self.refused(self.raw_plan('second', 'b'), '--after', str(a))
        self.assertIn('is running', out)
        self.assertIn(str(os.getpid()), out)
        # a pid that is gone is not a live scheduler
        (a / 'scheduler.live').write_text(json.dumps({'pid': 2 ** 22 + 12345, 'started': '2026-11-15T10:00:00Z'}))
        b, r = self.make('third', 'c', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_refuses_a_branch_of_its_own_at_another_commit(self):
        a = self.finished('first', 'a')
        self.git('branch', 'forge/second-integration', 'HEAD')
        out = self.refused(self.raw_plan('second', 'b'), '--after', str(a))
        self.assertIn('forge/second-integration already exists', out)

    def test_accepts_its_own_branch_when_it_is_at_the_chain_base(self):
        a = self.finished('first', 'a')
        self.git('branch', 'forge/second-integration', self.text(a, 'accepted.integration'))
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(b, 'after'), str(a))


class RunChainTests(ChainCase):
    def test_run_refuses_a_chained_plan_whose_predecessor_moved(self):
        a = self.finished('first', 'a')
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.hand_commit_and_adopt(a)
        base = self.text(b, 'integration.base')
        calls = self.calls()
        r = self.forge('run', b)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('plan first %s: run plan %s --after %s again' % (MOVED, b, a), r.stdout)
        self.assertEqual(self.calls(), calls)        # nothing was dispatched
        self.assertEqual(self.text(b, 'integration.base'), base)
        self.assertEqual(self.tip(b), base)
        # planning again from the moved predecessor is the way out
        again = self.forge('plan', b, '--after', str(a))
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual(self.text(b, 'integration.base'), self.text(a, 'accepted.integration'))
        r = self.forge('run', b)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.git('show', 'forge/second-integration:fix.txt').decode(), 'a fix\n')

    def test_a_plan_split_off_a_chained_plan_is_not_refused(self):
        a = self.finished('first', 'a')
        self.scenario({'c.qa.*': {'verdict': 'FAIL', 'findings': P1.replace('a.txt', 'c.txt')}})
        b, r = self.make('second', 'bc', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.forge('run', b).returncode, 5)
        self.assertEqual(self.text(b, 'tasks/b/status'), 'PASS')
        self.assertTrue((b / 'tasks/b/merged').exists())
        new = self.root / 'third'
        r = self.forge('split', b, new, '--tasks', 'c')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(new, 'after'), str(a))
        self.scenario({})
        r = self.forge('run', new, '--retry-failed', '1')
        self.assertNotIn(MOVED, r.stdout)
        self.assertNotEqual(r.returncode, 3, r.stdout)

    def test_the_check_ends_once_a_task_of_the_plan_has_merged(self):
        a = self.finished('first', 'a')
        self.scenario({'c.qa.*': {'verdict': 'FAIL', 'findings': P1.replace('a.txt', 'c.txt')}})
        b, r = self.make('second', 'bc', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.forge('run', b).returncode, 5)
        self.assertTrue((b / 'tasks/b/merged').exists())
        merged_tip = self.tip(b)
        self.hand_commit_and_adopt(a)
        r = self.forge('run', b)
        self.assertNotIn(MOVED, r.stdout)
        # the branch was reused, never reset to HEAD or to the base
        self.assertEqual(self.tip(b), merged_tip)

    def test_an_existing_integration_branch_is_never_reset(self):
        a = self.finished('first', 'a')
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        # the user's checkout moves on: HEAD is no longer where the chain started
        (self.repo / 'later.txt').write_text('later\n')
        self.git('add', 'later.txt')
        self.git('commit', '-qm', 'later')
        self.assertEqual(self.forge('run', b).returncode, 0)
        shown = self.git('ls-tree', '--name-only', 'forge/second-integration').decode().split()
        self.assertIn('a.txt', shown)
        self.assertNotIn('later.txt', shown)
        self.assertEqual(self.text(b, 'integration.base'), self.text(a, 'accepted.integration'))

    def test_a_deleted_branch_is_made_again_at_the_chain_base_not_at_head(self):
        a = self.finished('first', 'a')
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.git('branch', '-D', 'forge/second-integration')
        self.assertEqual(self.forge('run', b).returncode, 0)
        self.assertIn('a.txt', self.git('ls-tree', '--name-only', 'forge/second-integration').decode().split())
        self.assertEqual(self.text(b, 'tasks/b/base_ref'), self.text(a, 'accepted.integration'))


class ReplanTests(ChainCase):
    def test_a_replan_without_after_keeps_it(self):
        a = self.finished('first', 'a')
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        before = (self.text(b, 'after'), self.text(b, 'integration.base'), self.tip(b))
        again = self.forge('plan', b)
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual((self.text(b, 'after'), self.text(b, 'integration.base'), self.tip(b)), before)
        self.assertIn('after: first (%s)' % before[1][:12], again.stdout)

    def test_a_replan_with_the_same_after_is_a_replan_even_after_a_merge(self):
        a = self.finished('first', 'a')
        b = self.finished('second', 'b', '--after', str(a))
        merged_tip = self.tip(b)
        r = self.forge('plan', b, '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.tip(b), merged_tip)
        self.assertEqual(self.text(b, 'after'), str(a))

    def test_a_replan_with_another_after_is_refused_once_a_task_merged(self):
        a = self.finished('first', 'a')
        other = self.finished('elsewhere', 'd')
        b = self.finished('second', 'b', '--after', str(a))
        before = self.listing(b), self.text(b, 'after'), self.text(b, 'integration.base'), self.branches()
        r = self.forge('plan', b, '--after', str(other))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('already follows', r.stdout)
        self.assertEqual((self.listing(b), self.text(b, 'after'), self.text(b, 'integration.base'), self.branches()), before)

    def test_a_replan_with_another_after_before_any_merge_moves_the_untouched_branch(self):
        a = self.finished('first', 'a')
        other = self.finished('elsewhere', 'd')
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        # nothing merged and the branch is still where this plan began: it moves with the plan
        r = self.forge('plan', b, '--after', str(other))
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(b, 'after'), str(other))
        self.assertEqual(self.text(b, 'integration.base'), self.text(other, 'accepted.integration'))
        self.assertEqual(self.tip(b), self.text(other, 'accepted.integration'))
        self.assertIn('after: elsewhere (', r.stdout)

    def test_a_replan_does_not_move_a_branch_that_is_checked_out(self):
        a = self.finished('first', 'a')
        other = self.finished('elsewhere', 'd')
        b, r = self.make('second', 'b', '--after', str(a))
        self.assertEqual(r.returncode, 0, r.stdout)
        wt = self.root / 'wt-second'
        self.git('worktree', 'add', str(wt), 'forge/second-integration')
        r = self.forge('plan', b, '--after', str(other))
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('checked out in a worktree', r.stdout)
        self.assertEqual(self.text(b, 'after'), str(a))
        self.assertEqual(self.tip(b), self.text(a, 'accepted.integration'))


class IntegrateChainTests(ChainCase):
    def test_integrate_names_the_chain_and_brings_both_plans(self):
        a = self.finished('first', 'a')
        b = self.finished('second', 'b', '--after', str(a))
        self.assertFalse((self.repo / 'a.txt').exists())
        r = self.forge('integrate', b, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('this brings plans first → second', r.stdout)
        self.assertEqual((self.repo / 'a.txt').read_text(), 'implemented\n')
        self.assertEqual((self.repo / 'b.txt').read_text(), 'implemented\n')

    def test_a_three_plan_chain_is_named_oldest_first(self):
        a = self.finished('first', 'a')
        b = self.finished('second', 'b', '--after', str(a))
        c = self.finished('third', 'c', '--after', str(b))
        r = self.forge('integrate', c, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('this brings plans first → second → third', r.stdout)
        for name in 'abc':
            self.assertTrue((self.repo / ('%s.txt' % name)).exists())

    def test_a_plan_without_a_predecessor_prints_no_chain(self):
        a = self.finished('first', 'a')
        r = self.forge('integrate', a, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn('this brings', r.stdout)

    def test_a_missing_plan_dir_along_the_way_ends_the_list_with_an_ellipsis(self):
        a = self.finished('first', 'a')
        b = self.finished('second', 'b', '--after', str(a))
        c = self.finished('third', 'c', '--after', str(b))
        (b / 'after').write_text(str(self.root / 'gone') + '\n')
        r = self.forge('integrate', c, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('this brings plans … → second → third', r.stdout)


class OptionListTests(unittest.TestCase):
    def test_after_is_in_both_option_lists(self):
        libs = (ROOT / 'scripts' / 'forge-lib-plans.sh').read_text()
        plans = re.search(r'^PLANS_OPTION_FILES="([^"]*)"', libs, re.M).group(1).split()
        self.assertIn('after', plans)
        compare = (ROOT / 'scripts' / 'forge-compare.py').read_text()
        names = eval(re.search(r'OPTION_FILES = (\(.*?\))\n', compare, re.S).group(1))
        self.assertIn('after', names)


if __name__ == '__main__':
    unittest.main()
