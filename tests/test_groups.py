"""groups.tsv: parse, validate, derive from waves, resolve qa, freeze, and the CLI.

Plans are fixture dirs in a temp dir; nothing needs git or a model."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_groups as fg  # noqa: E402

CLI = str(ROOT / 'scripts' / 'forge-groups.py')

# id deps difficulty files dwarf qa title
TASKS = [
    ('a', '-', 'low', 'src/a.py', 'dw-a', 'qa-a', 'A'),
    ('b', '-', 'medium', 'src/b.py', 'dw-b', 'qa-b', 'B'),
    ('c', '-', 'high', 'docs', 'dw-c', 'qa-c', 'C'),
    ('d', 'a', 'low', 'lib/d.py', 'dw-d', 'qa-d', 'D'),
    ('e', 'd', 'low', 'lib/e.py', 'dw-e', 'qa-e', 'E'),
    ('f', '-', 'medium', 'docs/x.md', 'dw-f', 'qa-f', 'F'),
]


def make_plan(tasks=TASKS, groups=None, waves=None):
    plan = Path(tempfile.mkdtemp(prefix='groups-test-'))
    (plan / 'tasks.tsv').write_text(''.join('\t'.join(t) + '\n' for t in tasks))
    if groups is not None:
        (plan / 'groups.tsv').write_text(groups)
    if waves is not None:
        (plan / 'waves.tsv').write_text(''.join('%s\t%s\n' % w for w in waves))
    return plan


def row(*cols):
    return '\t'.join(cols) + '\n'


def problem(plan, text, **kw):
    (plan / 'groups.tsv').write_text(text)
    with_error = None
    try:
        fg.validate(plan, fg.load(plan), **kw)
    except fg.GroupError as error:
        with_error = str(error)
    return with_error


class LoadTests(unittest.TestCase):
    def test_no_file_is_empty(self):
        self.assertEqual(fg.load(make_plan()), [])

    def test_good_rows_skip_comments_and_blanks(self):
        plan = make_plan(groups='# id\treview\tqa\tmembers\ttitle\tgoal\n\n'
                         + row('g1', 'group', '-', 'a,b', 'Output formats', 'JSON and TSV')
                         + row('g2', 'task', 'x:high', 'd', '-', '-'))
        self.assertEqual(fg.load(plan), [
            fg.Group('g1', 'group', '-', ('a', 'b'), 'Output formats', 'JSON and TSV'),
            fg.Group('g2', 'task', 'x:high', ('d',), '-', '-')])

    def test_malformed_rows_name_the_line(self):
        cases = {
            'columns': (row('g1', 'group', '-', 'a,b', 'T'), 'six columns'),
            'id': (row('G1', 'group', '-', 'a,b', '-', '-'), 'bad group id'),
            'long id': (row('a' * 41, 'group', '-', 'a,b', '-', '-'), 'bad group id'),
            'review': (row('g1', 'wave', '-', 'a,b', '-', '-'), 'review'),
            'members': (row('g1', 'group', '-', '', '-', '-'), 'members'),
            'hole': (row('g1', 'group', '-', 'a,,b', '-', '-'), 'members'),
            'dup': (row('g1', 'group', '-', 'a,b', '-', '-') + row('g1', 'group', '-', 'd,e', '-', '-'),
                    'duplicate group id'),
        }
        for name, (text, word) in cases.items():
            with self.subTest(name):
                plan = make_plan(groups='# header\n\n' + text)
                with self.assertRaises(fg.GroupError) as caught:
                    fg.load(plan)
                self.assertIn('line 3', str(caught.exception)) if name != 'dup' else None
                self.assertIn(word, str(caught.exception))
        plan = make_plan(groups=row('g1', 'group', '-', 'a,b', '-', '-') + row('g1', 'group', '-', 'd,e', '-', '-'))
        with self.assertRaisesRegex(fg.GroupError, 'line 2'):
            fg.load(plan)


class ValidateTests(unittest.TestCase):
    def test_valid_passes(self):
        plan = make_plan(groups=row('g1', 'group', '-', 'a,b,f', '-', '-') + row('g2', 'task', '-', 'd,e', '-', '-'))
        fg.validate(plan, fg.load(plan))
        # review=task groups skip the dependency rule, as above (e depends on d)

    def test_unknown_member(self):
        self.assertIn('nope', problem(make_plan(), row('g1', 'task', '-', 'a,nope', '-', '-')))

    def test_task_in_two_groups(self):
        text = row('g1', 'task', '-', 'a,b', '-', '-') + row('g2', 'task', '-', 'b,d', '-', '-')
        message = problem(make_plan(), text)
        self.assertIn('g2', message)
        self.assertIn('two groups', message)

    def test_task_twice_in_one_group(self):
        self.assertIn('two groups', problem(make_plan(), row('g1', 'task', '-', 'a,a', '-', '-')))

    def test_id_clash_with_task(self):
        self.assertIn('task id', problem(make_plan(), row('a', 'task', '-', 'b,d', '-', '-')))
        self.assertIsNone(problem(make_plan(), row('zz', 'task', '-', 'b,d', '-', '-')))

    def test_group_size_bounds(self):
        self.assertIn('at least 2', problem(make_plan(), row('g1', 'group', '-', 'a', '-', '-')))
        self.assertIsNone(problem(make_plan(), row('g1', 'task', '-', 'a', '-', '-')))
        members = ','.join('t%d' % i for i in range(7))
        tasks = [('t%d' % i, '-', 'low', 'f%d' % i, '-', '-', '-') for i in range(7)]
        message = problem(make_plan(tasks), row('g1', 'group', '-', members, '-', '-'))
        self.assertIn('at most 6', message)
        self.assertIsNone(problem(make_plan(tasks), row('g1', 'group', '-', members, '-', '-'), max_tasks=7))
        self.assertIsNone(problem(make_plan(tasks), row('g1', 'task', '-', members, '-', '-')))

    def test_direct_dependency(self):
        message = problem(make_plan(), row('g1', 'group', '-', 'a,d', '-', '-'))
        self.assertIn('g1', message)
        self.assertIn('d depends on member a', message)

    def test_transitive_dependency_through_a_non_member(self):
        tasks = TASKS[:3] + [('m', 'a', 'low', 'm.py', '-', '-', '-'), ('n', 'm', 'low', 'n.py', '-', '-', '-')]
        message = problem(make_plan(tasks), row('g1', 'group', '-', 'a,n', '-', '-'))
        self.assertIn('n depends on member a', message)
        self.assertIn('through m', message)
        self.assertIsNone(problem(make_plan(tasks), row('g1', 'group', '-', 'b,n', '-', '-')))

    def test_dependency_cycle_outside_does_not_hang(self):
        tasks = [('p', 'q', 'low', 'p', '-', '-', '-'), ('q', 'p', 'low', 'q', '-', '-', '-'),
                 ('r', '-', 'low', 'r', '-', '-', '-')]
        self.assertIsNone(problem(make_plan(tasks), row('g1', 'group', '-', 'p,r', '-', '-')))

    def test_overlapping_files(self):
        tasks = [('a', '-', 'low', 'src/a.py', '-', '-', '-'), ('b', '-', 'low', 'src/a.py', '-', '-', '-')]
        self.assertIn('overlapping files', problem(make_plan(tasks), row('g1', 'group', '-', 'a,b', '-', '-')))

    def test_directory_covers_file(self):
        # c declares docs, f declares docs/x.md beneath it
        message = problem(make_plan(), row('g1', 'group', '-', 'c,f', '-', '-'))
        self.assertIn('overlapping files', message)
        self.assertIn('c and f', message)
        # a sibling with a shared prefix is not beneath it
        tasks = [('a', '-', 'low', 'docs', '-', '-', '-'), ('b', '-', 'low', 'docs2/x.md', '-', '-', '-')]
        self.assertIsNone(problem(make_plan(tasks), row('g1', 'group', '-', 'a,b', '-', '-')))
        # review=task does not check
        self.assertIsNone(problem(make_plan(), row('g1', 'task', '-', 'c,f', '-', '-')))

    def test_overlap_matches_the_scheduler(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('forge_schedule', ROOT / 'scripts' / 'forge-schedule.py')
        schedule = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(schedule)
        for a, b in [('docs', 'docs/x.md'), ('docs/', 'docs'), ('.', 'x'), ('-', 'x'), ('a,b', 'c,b/d'),
                     ('docs', 'docs2'), ('./a', 'a/b'), ('a', 'b')]:
            self.assertEqual(fg.overlap(a, b), schedule.overlap(a, b), (a, b))

    def test_sweep_members(self):
        tasks = [('sweep-1', '-', 'low', 'x', '-', '-', '-'), ('b', '-', 'low', 'y', '-', '-', '-'),
                 ('fixer', '-', 'low', 'z', '-', '-', '-')]
        plan = make_plan(tasks)
        self.assertIn('sweep', problem(plan, row('g1', 'group', '-', 'sweep-1,b', '-', '-')))
        (plan / 'tasks' / 'fixer').mkdir(parents=True)
        (plan / 'tasks' / 'fixer' / 'sweep.json').write_text('{}')
        self.assertIn('fixer is a sweep task', problem(plan, row('g1', 'group', '-', 'fixer,b', '-', '-')))
        self.assertIsNone(problem(plan, row('g1', 'task', '-', 'fixer,b', '-', '-')))
        self.assertIsNone(problem(make_plan(tasks[1:2] + [('c', '-', 'low', 'w', '-', '-', '-')]),
                                  row('g1', 'group', '-', 'b,c', '-', '-')))


class DeriveTests(unittest.TestCase):
    WAVES = [(1, 'a'), (1, 'b'), (1, 'c'), (2, 'd'), (3, 'e'), (3, 'f')]

    def test_one_group_per_wave_of_two_or_more(self):
        plan = make_plan(waves=self.WAVES)
        groups = fg.derive(plan)
        self.assertEqual([(g.id, g.review, g.qa, g.members, g.title, g.goal) for g in groups],
                         [('w1', 'task', '-', ('a', 'b', 'c'), '-', '-'),
                          ('w3', 'task', '-', ('e', 'f'), '-', '-')])

    def test_split_at_max_tasks_in_tasks_order(self):
        tasks = [('t%d' % i, '-', 'low', 'f%d' % i, '-', '-', '-') for i in range(7)]
        plan = make_plan(tasks, waves=[(3, 't%d' % i) for i in reversed(range(7))])
        groups = fg.derive(plan, max_tasks=3)
        self.assertEqual([(g.id, g.members) for g in groups],
                         [('w3a', ('t0', 't1', 't2')), ('w3b', ('t3', 't4', 't5'))])  # w3c has one task: dropped
        groups = fg.derive(plan)
        self.assertEqual([(g.id, len(g.members)) for g in groups], [('w3a', 6)])

    def test_high_left_out_under_group_review(self):
        groups = fg.derive(make_plan(waves=self.WAVES), review='group')
        self.assertEqual([(g.id, g.review, g.members) for g in groups],
                         [('w1', 'group', ('a', 'b')), ('w3', 'group', ('e', 'f'))])

    def test_group_of_one_dropped(self):
        waves = [(1, 'a'), (1, 'c'), (2, 'b')]
        self.assertEqual(fg.derive(make_plan(waves=waves), review='group'), [])
        self.assertEqual([g.members for g in fg.derive(make_plan(waves=waves))], [('a', 'c')])

    def test_derived_groups_validate(self):
        plan = make_plan(waves=self.WAVES)
        fg.validate(plan, fg.derive(plan, review='group'))

    def test_write_then_load_round_trips(self):
        plan = make_plan(waves=self.WAVES)
        groups = fg.derive(plan, review='group')
        fg.write(plan, groups)
        text = (plan / 'groups.tsv').read_text()
        self.assertTrue(text.startswith('# id\treview\tqa\tmembers\ttitle\tgoal\n'))
        self.assertEqual(fg.load(plan), groups)
        self.assertEqual([p.name for p in plan.iterdir() if p.name.startswith('.groups')], [])
        fg.write(plan, [fg.Group('x', 'task', ' ', ('a', 'b'), 'two\tcol\nline', '')])
        self.assertEqual(fg.load(plan), [fg.Group('x', 'task', '-', ('a', 'b'), 'two col line', '-')])


class ResolveTests(unittest.TestCase):
    def test_highest_difficulty_wins_ties_go_first(self):
        plan = make_plan()
        groups = [fg.Group('g1', 'group', '-', ('a', 'b'), '-', '-'),
                  fg.Group('g2', 'group', '-', ('d', 'a'), '-', '-'),
                  fg.Group('g3', 'group', '-', ('f', 'b', 'c'), '-', '-'),
                  fg.Group('g4', 'group', 'mine', ('a', 'b'), '-', '-'),
                  fg.Group('g5', 'task', '-', ('b', 'c'), '-', '-')]
        self.assertEqual([g.qa for g in fg.resolve_qa(plan, groups)],
                         ['qa-b', 'qa-d', 'qa-c', 'mine', '-'])
        tied = [fg.Group('g', 'group', '-', ('f', 'b'), '-', '-')]
        self.assertEqual(fg.resolve_qa(plan, tied)[0].qa, 'qa-f')


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.plan = make_plan(groups=row('g1', 'group', '-', 'a,b', 'T', 'G'))
        self.groups = fg.load(self.plan)

    def rewrite(self, task_id, column, value):
        lines = []
        for line in (self.plan / 'tasks.tsv').read_text().splitlines():
            cols = line.split('\t')
            if cols[0] == task_id:
                cols[column] = value
            lines.append('\t'.join(cols))
        (self.plan / 'tasks.tsv').write_text('\n'.join(lines) + '\n')

    def test_text_shape(self):
        self.assertEqual(fg.freeze_text(self.plan, self.groups),
                         'g1\tgroup\t-\ta,b\tT\tG\na\t-\tsrc/a.py\nb\t-\tsrc/b.py\n')

    def test_stable_and_sensitive(self):
        before = fg.fingerprint(self.plan, self.groups)
        self.assertEqual(before, fg.fingerprint(self.plan, fg.load(self.plan)))
        self.assertEqual(len(before), 64)
        self.rewrite('a', 1, 'b')
        self.assertNotEqual(before, fg.fingerprint(self.plan, self.groups))
        self.rewrite('a', 1, '-')
        self.assertEqual(before, fg.fingerprint(self.plan, self.groups))
        self.rewrite('b', 3, 'src')
        self.assertNotEqual(before, fg.fingerprint(self.plan, self.groups))
        self.rewrite('b', 3, 'src/b.py')
        for column in range(6):
            other = list(self.groups[0])
            other[column] = ('task' if column == 1 else ('b', 'a') if column == 3 else 'changed')
            self.assertNotEqual(before, fg.fingerprint(self.plan, [fg.Group(*other)]), column)

    def test_dwarf_and_other_columns_do_not_count(self):
        before = fg.fingerprint(self.plan, self.groups)
        self.rewrite('a', 4, 'other-dwarf')
        self.rewrite('b', 5, 'other-qa')
        self.rewrite('b', 6, 'other title')
        self.assertEqual(before, fg.fingerprint(self.plan, self.groups))

    def test_check_frozen_states(self):
        self.assertIsNone(fg.check_frozen(make_plan()))            # no groups, no frozen file
        self.assertEqual(fg.check_frozen(self.plan), fg.FROZEN_REASON)  # groups, never frozen
        fg.freeze(self.plan)
        self.assertEqual((self.plan / 'groups.frozen').read_text(), fg.fingerprint(self.plan, self.groups) + '\n')
        self.assertIsNone(fg.check_frozen(self.plan))              # matches
        self.rewrite('a', 3, 'src/changed.py')
        self.assertEqual(fg.check_frozen(self.plan),
                         "groups.tsv or a member's deps/files changed since plan; run plan again")
        self.rewrite('a', 3, 'src/a.py')
        (self.plan / 'groups.tsv').write_text(row('g1', 'group', '-', 'a,b', 'T2', 'G'))
        self.assertIsNotNone(fg.check_frozen(self.plan))
        (self.plan / 'groups.tsv').write_text('')
        self.assertIsNotNone(fg.check_frozen(self.plan))           # frozen file but no groups
        fg.freeze(self.plan)
        self.assertFalse((self.plan / 'groups.frozen').exists())
        self.assertIsNone(fg.check_frozen(self.plan))

    def test_lookups(self):
        (self.plan / 'groups.tsv').write_text(row('g1', 'group', '-', 'a,b', '-', '-') + row('g2', 'task', '-', 'd,e', '-', '-'))
        self.assertEqual(fg.group_of(self.plan, 'b').id, 'g1')
        self.assertEqual(fg.group_of(self.plan, 'e').id, 'g2')
        self.assertIsNone(fg.group_of(self.plan, 'f'))
        self.assertEqual(fg.members_in_review_groups(self.plan), {'a', 'b'})


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        done = subprocess.run([sys.executable, '-I', CLI, *args], capture_output=True, text=True, timeout=60)
        return done.returncode, done.stdout, done.stderr

    def test_derive_resolve_freeze_check_of_show(self):
        plan = make_plan(waves=DeriveTests.WAVES)
        self.assertEqual(self.run_cli('derive', str(plan), '--review', 'group'), (0, '', ''))
        self.assertEqual([g.id for g in fg.load(plan)], ['w1', 'w3'])
        self.assertEqual(self.run_cli('validate', str(plan)), (0, '', ''))
        self.assertEqual(self.run_cli('resolve', str(plan)), (0, '', ''))
        self.assertEqual([g.qa for g in fg.load(plan)], ['qa-b', 'qa-f'])
        code, _, err = self.run_cli('check', str(plan))
        self.assertEqual(code, 2)
        self.assertIn('run plan again', err)
        self.assertEqual(self.run_cli('freeze', str(plan)), (0, '', ''))
        self.assertEqual(self.run_cli('check', str(plan)), (0, '', ''))
        self.assertEqual(self.run_cli('of', str(plan), 'e'), (0, 'w3\tgroup\tqa-f\n', ''))
        self.assertEqual(self.run_cli('of', str(plan), 'c'), (0, '', ''))
        code, out, _ = self.run_cli('show', str(plan))
        self.assertEqual((code, out.splitlines()[0]), (0, 'w1\tgroup\tqa-b\ta,b\t-\t-'))
        code, out, _ = self.run_cli('show', str(plan), '--json')
        self.assertEqual(json.loads(out)[1], dict(id='w3', review='group', qa='qa-f', members=['e', 'f'],
                                                  title='-', goal='-'))

    def test_resolve_without_groups_tsv_leaves_the_plan_alone(self):
        plan = make_plan()
        self.assertFalse((plan / 'groups.tsv').exists())
        self.assertEqual(self.run_cli('resolve', str(plan)), (0, '', ''))
        self.assertFalse((plan / 'groups.tsv').exists())

    def test_invalid_input_exits_2_with_one_line(self):
        plan = make_plan(groups=row('g1', 'group', '-', 'a,d', '-', '-'))
        for args in (['validate', str(plan)], ['validate', str(plan), '--max-tasks', 1],
                     ['show', str(plan.parent / 'missing')], ['of', str(plan)], ['bogus', str(plan)],
                     ['derive', str(plan), '--review', 'group'], ['derive', str(plan), '--review', 'x']):
            with self.subTest(args=args):
                code, out, err = self.run_cli(*map(str, args))
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertTrue(err.strip())
        code, _, err = self.run_cli('validate', str(plan))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertIn('g1', err)
        (plan / 'groups.tsv').write_text('g1\tgroup\n')
        code, _, err = self.run_cli('show', str(plan))
        self.assertEqual(code, 2)
        self.assertIn('line 1', err)

    def test_max_tasks_option(self):
        tasks = [('t%d' % i, '-', 'low', 'f%d' % i, '-', '-', '-') for i in range(4)]
        plan = make_plan(tasks, groups=row('g1', 'group', '-', 't0,t1,t2,t3', '-', '-'))
        self.assertEqual(self.run_cli('validate', str(plan))[0], 0)
        self.assertEqual(self.run_cli('validate', str(plan), '--max-tasks', '3')[0], 2)


if __name__ == '__main__':
    unittest.main()
