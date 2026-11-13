"""plan --groups, a groups.tsv in the plan dir, the approval table and the run check (#140, #136).

The parsing, validation and derivation rules are test_groups'; this is the wiring in `forge-parallel.sh`:
what `plan` writes (groups.tsv, groups.frozen, the option files), what the table says, what `run` refuses
and how `split` treats the groups. Driven like test_feature_plan_options: `plan` on a fixture repo with the
fake agent CLI of test_forge (fixtures borrowed, not inherited)."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
from pathlib import Path
import re
import sys
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_groups as fg  # noqa: E402

P1 = '- [P1][CONFIRMED] a.txt:1 — realistic input breaks (x)'
HEADER = '# id\treview\tqa\tmembers\ttitle\tgoal\n'


def row(*cols):
    return '\t'.join(cols) + '\n'


class GroupsPlanTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    scenario = fixtures.ForgeTests.scenario
    run_script = fixtures.ForgeTests.run_script

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        self.count = 0

    def new_plan(self, rows=None, name=None, parent=None):
        """rows: (id, deps, difficulty, files, dwarf, qa); the default is four independent tasks."""
        rows = rows or [('a', '-', 'low', 'a.txt', 'sol', 'sonnet'), ('b', '-', 'medium', 'b.txt', 'sol', 'opus:high'),
                        ('c', '-', 'high', 'c.txt', 'sol', 'opus'), ('d', 'a', 'low', 'd.txt', 'sol', 'sonnet')]
        self.count += 1
        base = parent or self.root
        base.mkdir(parents=True, exist_ok=True)
        p = base / (name or 'plan%d' % self.count)
        p.mkdir()
        (p / 'tasks.tsv').write_text(''.join('%s\t%s\t%s\t%s\t%s\t%s\tT%s\n' % (*r, r[0]) for r in rows))
        for r in rows:
            (p / 'tasks' / r[0]).mkdir(parents=True)
            (p / 'tasks' / r[0] / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + r[0])
        return p

    def plan_cmd(self, p, *flags):
        return self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory', *flags)

    def planned(self, p, *flags):
        r = self.plan_cmd(p, *flags)
        self.assertEqual(r.returncode, 0, r.stdout)
        return r.stdout

    def groups(self, p):
        return [(g.id, g.review, g.qa, g.members) for g in fg.load(p)]

    def frozen_ok(self, p):
        self.assertTrue((p / 'groups.frozen').is_file())
        self.assertIsNone(fg.check_frozen(p))

    # --- plan --groups waves ----------------------------------------------------------------------------------
    def test_waves_writes_the_groups_and_freezes_them(self):
        p = self.new_plan()
        out = self.planned(p, '--groups', 'waves')
        self.assertEqual(self.groups(p), [('w1', 'task', '-', ('a', 'b', 'c'))])   # d waits on a: alone in wave 2
        self.frozen_ok(p)
        self.assertIn('groups:', out)

    def test_group_review_leaves_the_high_tasks_out_and_resolves_the_qa(self):
        p = self.new_plan()
        self.planned(p, '--groups', 'waves', '--group-review')
        # the highest difficulty left in the group is b (medium): its qa spec is the group's
        self.assertEqual(self.groups(p), [('w1', 'group', 'opus:high', ('a', 'b'))])
        self.frozen_ok(p)

    def test_group_max_tasks_splits_a_wave_and_is_kept_as_a_plan_file(self):
        rows = [(t, '-', 'low', t + '.txt', 'sol', 'sonnet') for t in 'abcde']
        p = self.new_plan(rows)
        self.planned(p, '--groups', 'waves', '--group-max-tasks', '2', '--group-max-diff-kb', '64')
        self.assertEqual([g[0] for g in self.groups(p)], ['w1a', 'w1b'])       # a,b / c,d; e alone is no group
        self.assertEqual((p / 'group_max_tasks').read_text(), '2\n')
        self.assertEqual((p / 'group_max_diff_kb').read_text(), '64\n')
        # a re-plan without the flag keeps the limit it was made with: three members are refused
        (p / 'groups.tsv').write_text(HEADER + row('g', 'group', '-', 'a,b,c', '-', '-'))
        r = self.plan_cmd(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('3 members, at most 2 allowed', r.stdout)

    def test_the_option_files_are_written_only_when_given(self):
        p = self.new_plan()
        self.planned(p, '--groups', 'waves')
        self.assertFalse((p / 'group_max_tasks').exists())
        self.assertFalse((p / 'group_max_diff_kb').exists())

    def test_bad_flag_values_are_refused_before_anything_is_written(self):
        for flags in (('--group-max-tasks', '0'), ('--group-max-tasks', 'x'), ('--group-max-tasks',),
                      ('--group-max-diff-kb', '0'), ('--group-max-diff-kb', '-3'), ('--groups',),
                      ('--groups', str(self.root / 'missing.tsv')), ('--group-review',)):
            p = self.new_plan()
            before = sorted(x.name for x in p.iterdir())
            r = self.plan_cmd(p, *flags)
            self.assertEqual(r.returncode, 2, (flags, r.stdout))
            # dataset.uid is written before any flag is read, whatever the flags are
            self.assertEqual(sorted(x.name for x in p.iterdir() if x.name != 'dataset.uid'), before, flags)

    # --- groups.tsv by hand or from a file ------------------------------------------------------------------------
    def test_a_groups_file_in_the_plan_dir_is_used_without_the_flag(self):
        p = self.new_plan()
        (p / 'groups.tsv').write_text(HEADER + row('g1', 'group', '-', 'a,b', 'Two', 'Do both'))
        self.planned(p)
        # qa: the member with the highest difficulty (b, medium) gives it
        self.assertEqual(self.groups(p), [('g1', 'group', 'opus:high', ('a', 'b'))])
        self.frozen_ok(p)

    def test_an_explicit_qa_is_kept(self):
        p = self.new_plan()
        (p / 'groups.tsv').write_text(HEADER + row('g1', 'group', 'sol:xhigh', 'a,c', '-', '-'))
        self.planned(p)
        self.assertEqual(self.groups(p), [('g1', 'group', 'sol:xhigh', ('a', 'c'))])

    def test_groups_file_flag_copies_the_file(self):
        p = self.new_plan()
        src = self.root / 'mine.tsv'
        src.write_text(HEADER + row('x1', 'task', '-', 'a,b', 'Mine', 'Goal'))
        self.planned(p, '--groups', src)
        self.assertEqual(self.groups(p), [('x1', 'task', '-', ('a', 'b'))])
        self.frozen_ok(p)
        self.assertEqual(src.read_text(), HEADER + row('x1', 'task', '-', 'a,b', 'Mine', 'Goal'))   # the source is left alone

    def test_a_replan_freezes_the_groups_again(self):
        p = self.new_plan()
        self.planned(p, '--groups', 'waves')
        first = (p / 'groups.frozen').read_text()
        tasks = (p / 'tasks.tsv').read_text()
        (p / 'tasks.tsv').write_text(tasks.replace('b.txt', 'bb.txt'))
        self.planned(p)
        self.assertNotEqual((p / 'groups.frozen').read_text(), first)
        self.frozen_ok(p)

    # --- invalid groups: exit 2 and the rule ------------------------------------------------------------------------
    def test_an_invalid_group_exits_2_naming_the_rule(self):
        cases = {
            'a member that is not a task': (row('g', 'task', '-', 'a,zz', '-', '-'), 'member zz is not in tasks.tsv'),
            'two groups': (row('g', 'task', '-', 'a,b', '-', '-') + row('h', 'task', '-', 'b,c', '-', '-'), 'is in two groups'),
            'one member': (row('g', 'group', '-', 'a', '-', '-'), 'needs at least 2 members'),
            'too many': (row('g', 'group', '-', 'a,b,c', '-', '-'), 'at most 2 allowed'),
            'a dependency': (row('g', 'group', '-', 'a,d', '-', '-'), 'depends on member'),
            'a malformed row': ('g\tgroup\n', 'needs six columns'),
        }
        for name, (text, reason) in cases.items():
            with self.subTest(name):
                p = self.new_plan()
                (p / 'groups.tsv').write_text(HEADER + text)
                r = self.plan_cmd(p, '--group-max-tasks', '2')
                self.assertEqual(r.returncode, 2, r.stdout)
                self.assertIn(reason, r.stdout)
                self.assertFalse((p / 'groups.frozen').exists())

    def test_overlapping_files_exit_2(self):
        rows = [('a', '-', 'low', 'src', 'sol', 'opus'), ('b', '-', 'low', 'src/b.txt', 'sol', 'opus')]
        p = self.new_plan(rows)
        (p / 'groups.tsv').write_text(HEADER + row('g', 'group', '-', 'a,b', '-', '-'))
        r = self.plan_cmd(p)
        # they are in two waves (the overlap defers b), but a review group must not overlap either
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('overlapping files', r.stdout)

    # --- the approval table ---------------------------------------------------------------------------------------------
    def test_the_table_shows_the_groups_and_the_reviews(self):
        rows = [(t, '-', 'low', t + '.txt', 'sol', 'sonnet') for t in 'abcdefg']
        p = self.new_plan(rows)
        (p / 'groups.tsv').write_text(HEADER + row('g1', 'group', 'opus:medium', 'a,b,c', 'Formats', 'Every command can write JSON and TSV')
                                      + row('g2', 'group', '-', 'd,e', '-', '-') + row('w3', 'task', '-', 'f,g', '-', '-'))
        out = self.planned(p)
        self.assertIn('\ngroups:\n', out)
        lines = out.splitlines()
        block = lines[lines.index('groups:') + 1:][:3]
        self.assertEqual(block, [
            '  g1  review=group  qa=opus:medium  a, b, c  — Every command can write JSON and TSV',
            '  g2  review=group  qa=sonnet       d, e     — (no goal)',
            '  w3  review=task   -               f, g     — (no goal)'])
        # 7 tasks, 2 review groups cover 5 of them: 7 - 5 + 2 reviews
        self.assertIn('reviews: 4 (2 groups replace 5 task reviews)', lines)
        self.assertIn('tasks: 7   waves: 1   estimated dispatches: 11 (7 dwarf + 4 qa)', out)

    def test_a_single_review_group_is_counted(self):
        p = self.new_plan()
        (p / 'groups.tsv').write_text(HEADER + row('g1', 'group', '-', 'a,b', '-', '-'))
        out = self.planned(p)
        self.assertIn('reviews: 3 (1 groups replace 2 task reviews)', out.splitlines())

    def test_review_task_groups_change_no_estimate(self):
        p = self.new_plan()
        out = self.planned(p, '--groups', 'waves')
        self.assertIn('groups:', out)
        self.assertNotIn('reviews:', out)
        self.assertIn('estimated dispatches: 8 (4 dwarf + 4 qa)', out)

    # --- no groups: nothing changes ---------------------------------------------------------------------------------------
    def test_without_groups_the_files_and_the_table_are_as_before(self):
        # Two plans of one fixture in different directories with the same name (the run id is in the table).
        first = self.new_plan(name='same', parent=self.root / 'one')
        second = self.new_plan(name='same', parent=self.root / 'two')
        out_first, out_second = self.planned(first), self.planned(second)
        self.assertEqual(out_first, out_second)
        names = sorted(x.name for x in first.iterdir())
        self.assertEqual(names, sorted(x.name for x in second.iterdir()))
        self.assertEqual([n for n in names if n.startswith('group')], [])
        self.assertNotIn('groups:', out_first)
        self.assertNotIn('reviews:', out_first)
        self.assertIn('tasks: 4   waves: 2   estimated dispatches: 8 (4 dwarf + 4 qa)', out_first)
        for name in ('tasks.tsv', 'waves.tsv'):
            self.assertEqual((first / name).read_text(), (second / name).read_text())
        # a stale groups.frozen with no groups.tsv is cleared, not left to refuse the run
        (first / 'groups.frozen').write_text('0\n')
        self.planned(first)
        self.assertFalse((first / 'groups.frozen').exists())

    def test_run_without_groups_does_not_check(self):
        p = self.new_plan()
        self.planned(p)
        r = self.run_script('forge-parallel.sh', 'run', p, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)

    # --- run ------------------------------------------------------------------------------------------------------------------
    def frozen_plan(self):
        p = self.new_plan()
        (p / 'groups.tsv').write_text(HEADER + row('g1', 'group', '-', 'a,b', 'Two', 'Do both'))
        self.planned(p)
        r = self.run_script('forge-parallel.sh', 'run', p, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def edit_tasks(self, p, task, column, value):
        rows = []
        for line in (p / 'tasks.tsv').read_text().splitlines(keepends=True):
            cols = line.rstrip('\n').split('\t')
            if cols[0] == task:
                cols[column] = value
                line = '\t'.join(cols) + '\n'
            rows.append(line)
        (p / 'tasks.tsv').write_text(''.join(rows))

    def refused(self, p):
        r = self.run_script('forge-parallel.sh', 'run', p, '--dry-run')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('run plan again', r.stdout)
        self.assertNotIn('worktree root', r.stdout)          # nothing was set up, a dry run's lines included

    def test_run_refuses_after_groups_tsv_is_edited(self):
        p = self.frozen_plan()
        (p / 'groups.tsv').write_text((p / 'groups.tsv').read_text().replace('Do both', 'Do something else'))
        self.refused(p)

    def test_run_refuses_after_a_members_files_are_edited(self):
        p = self.frozen_plan()
        self.edit_tasks(p, 'a', 3, 'other.txt')
        self.refused(p)

    def test_run_refuses_after_a_members_deps_are_edited(self):
        p = self.frozen_plan()
        self.edit_tasks(p, 'b', 1, 'c')
        self.refused(p)

    def test_run_refuses_when_the_frozen_file_is_gone(self):
        p = self.frozen_plan()
        (p / 'groups.frozen').unlink()
        self.refused(p)

    def test_run_does_not_refuse_after_only_a_dwarf_changed(self):
        p = self.frozen_plan()
        self.edit_tasks(p, 'a', 4, 'opus')
        r = self.run_script('forge-parallel.sh', 'run', p, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_run_does_not_refuse_for_a_task_outside_the_groups(self):
        p = self.frozen_plan()
        self.edit_tasks(p, 'c', 3, 'other.txt')
        r = self.run_script('forge-parallel.sh', 'run', p, '--dry-run')
        self.assertEqual(r.returncode, 0, r.stdout)

    # --- the option lists ---------------------------------------------------------------------------------------------------------
    def test_the_files_are_in_the_option_lists(self):
        files = {'groups.tsv', 'groups.frozen', 'group_max_tasks', 'group_max_diff_kb'}
        libs = (ROOT / 'scripts' / 'forge-lib-plans.sh').read_text()
        plans = re.search(r'^PLANS_OPTION_FILES="([^"]*)"', libs, re.M).group(1).split()
        self.assertTrue(files <= set(plans), files - set(plans))
        compare = (ROOT / 'scripts' / 'forge-compare.py').read_text()
        names = eval(re.search(r'OPTION_FILES = (\(.*?\))\n', compare, re.S).group(1))
        self.assertTrue(files <= set(names), files - set(names))

    # --- Fractal ----------------------------------------------------------------------------------------------------------------------
    def test_group_review_under_fractal_exits_2(self):
        p = self.new_plan()
        (p / 'fractal-selection.json').write_text(json.dumps({'enabled': True}))
        (p / 'groups.tsv').write_text(HEADER + row('g1', 'group', '-', 'a,b', '-', '-'))
        r = self.plan_cmd(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('group review does not support Fractal yet', r.stdout)
        self.assertFalse((p / 'groups.frozen').exists())

    def test_task_review_groups_are_fine_under_fractal(self):
        p = self.new_plan()
        (p / 'fractal-selection.json').write_text(json.dumps({'enabled': True}))
        self.planned(p, '--groups', 'waves')
        self.frozen_ok(p)

    def test_run_refuses_group_review_under_fractal(self):
        p = self.frozen_plan()
        r = self.run_script('forge-parallel.sh', 'run', p, '--dry-run', '--fractal')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('group review does not support Fractal yet', r.stdout)

    # --- split ---------------------------------------------------------------------------------------------------------------------------
    def split_fixture(self):
        """a and e merged; b, c and d fail review. g1 (b, c) reviews together, g2 (d, e) per task."""
        rows = [(t, '-', 'low', t + '.txt', 'sol', 'opus') for t in 'abcde']
        p = self.new_plan(rows, name='plan')
        (p / 'groups.tsv').write_text(HEADER + row('g1', 'group', '-', 'b,c', '-', '-') + row('g2', 'task', '-', 'd,e', '-', '-'))
        self.planned(p)
        self.scenario({'b.qa.*': {'verdict': 'FAIL', 'findings': P1}, 'c.qa.*': {'verdict': 'FAIL', 'findings': P1},
                       'd.qa.*': {'verdict': 'FAIL', 'findings': P1}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        return p

    def test_split_refuses_to_cut_a_review_group(self):
        p = self.split_fixture()
        before = ((p / 'groups.tsv').read_text(), (p / 'groups.frozen').read_text(), (p / 'tasks.tsv').read_text())
        new = self.root / 'second'
        for tasks in ('b', 'c,d'):
            r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', tasks)
            self.assertEqual(r.returncode, 3, r.stdout)
            self.assertIn('review group g1', r.stdout)
            self.assertIn('--tasks b,c', r.stdout)
            self.assertFalse(new.exists())
        self.assertEqual(((p / 'groups.tsv').read_text(), (p / 'groups.frozen').read_text(), (p / 'tasks.tsv').read_text()), before)

    def test_split_refusal_does_not_offer_a_merged_review_group_member(self):
        # b is already MERGED, so "--tasks b,c" is a command split refuses. The refusal must not suggest it.
        p = self.split_fixture()
        self.assertFalse((p / 'tasks' / 'b' / 'merged').exists())
        (p / 'tasks' / 'b' / 'merged').write_text('')
        before = ((p / 'groups.tsv').read_text(), (p / 'groups.frozen').read_text(), (p / 'tasks.tsv').read_text())
        new = self.root / 'second'
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'c')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('review group g1', r.stdout)
        self.assertIn('b, which is MERGED', r.stdout)
        self.assertNotIn('--tasks', r.stdout)
        self.assertFalse(new.exists())
        # the advice the old message gave is itself refused, before the group check
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'b,c')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn("task 'b' is MERGED", r.stdout)
        self.assertNotIn('review group', r.stdout)
        self.assertFalse(new.exists())
        self.assertEqual(((p / 'groups.tsv').read_text(), (p / 'groups.frozen').read_text(), (p / 'tasks.tsv').read_text()), before)

    def test_split_moves_a_whole_review_group_and_cuts_a_task_group(self):
        p = self.split_fixture()
        new = self.root / 'second'
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'b,c,d')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.groups(new), [('g1', 'group', 'opus', ('b', 'c')), ('g2', 'task', '-', ('d',))])
        self.assertEqual(self.groups(p), [('g2', 'task', '-', ('e',))])
        for plan in (p, new):
            fg.validate(plan, fg.load(plan))
            self.frozen_ok(plan)
        self.assertEqual((new / 'group_max_tasks').exists(), (p / 'group_max_tasks').exists())
        # the run check accepts both plans as they are
        for plan in (p, new):
            self.assertEqual(self.run_script('forge-parallel.sh', 'run', plan, '--dry-run').returncode, 0)

    def test_split_leaves_no_groups_where_no_member_is_left(self):
        p = self.split_fixture()
        new = self.root / 'second'
        r = self.run_script('forge-parallel.sh', 'split', p, new, '--tasks', 'b,c')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.groups(new), [('g1', 'group', 'opus', ('b', 'c'))])
        self.assertEqual(self.groups(p), [('g2', 'task', '-', ('d', 'e'))])
        self.frozen_ok(p)
        self.frozen_ok(new)
        r = self.run_script('forge-parallel.sh', 'split', p, self.root / 'third', '--tasks', 'd')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.groups(p), [('g2', 'task', '-', ('e',))])


if __name__ == '__main__':
    unittest.main()
