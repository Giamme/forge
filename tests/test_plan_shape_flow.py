"""plan --collapse-chains, the sequential-plan note, and the shape on the plan event.

Drives forge-parallel.sh with the fake CLIs from test_forge. Shape and collapse themselves
are covered in test_plan_shape.py; this is the wiring into `plan` and the dataset.
"""
import json
import re
import sys
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

from forge_dataset import harvest  # noqa: E402
from forge_dataset.forecast import plan_tasks  # noqa: E402

HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
COLLAPSED = ('collapsed a sequential plan of %d tasks into one '
             '(tasks.precollapse.tsv keeps the original)')
SEQUENTIAL = ('this plan runs sequentially: %d tasks, at most 1 in parallel; '
              'each pays its own implementation and review. One task would run once: '
              're-plan with --collapse-chains')
# The id this fixture had before shape, collapse and plan-gate joined the plan hash.
BARE_RID = 'plan:fixed-run:26062ad6ab65e2f6'


def task_row(task_id, deps, difficulty, files, title):
    return '%s\t%s\t%s\t%s\t-\t-\t%s\n' % (task_id, deps, difficulty, files, title)


def data_rows(path):
    return [line.split('\t') for line in Path(path).read_text().splitlines()
            if line.strip() and not line.startswith('#')]


def shape_of(plan):
    return json.loads((plan / 'plan-shape.json').read_text())


class PlanShapeFlowTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    calls = fixtures.ForgeTests.calls

    def write_plan(self, name, rows):
        plan = self.root / name
        plan.mkdir()
        (plan / 'tasks.tsv').write_text(HEADER + ''.join(rows))
        for row in rows:
            task_id = row.split('\t', 1)[0]
            task = plan / 'tasks' / task_id
            task.mkdir(parents=True)
            (task / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + task_id + '\n')
        return plan

    def plan(self, plan, *flags):
        return self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory',
                               '--dwarf', 'sol', '--qa', 'opus', '--explore', '0', *flags)

    def assert_shape_matches_waves(self, plan):
        info = shape_of(plan)
        waves = {}
        for line in (plan / 'waves.tsv').read_text().splitlines():
            number, task_id = line.split('\t', 1)
            waves.setdefault(int(number), []).append(task_id)
        self.assertEqual(info['tasks'], sum(len(ids) for ids in waves.values()))
        self.assertEqual(info['waves'], len(waves))
        self.assertEqual(info['widest'], max(len(ids) for ids in waves.values()))
        if info['tasks'] == 1:
            self.assertEqual(info['shape'], 'one')
        elif info['widest'] == 1:
            self.assertEqual(info['shape'], 'chain')
        else:
            self.assertEqual(info['shape'], 'branches')

    def test_collapse_chains_merges_a_chain_before_routing_and_runs_one_dwarf(self):
        plan = self.write_plan('chain', [
            task_row('a', '-', 'low', 'a.txt', 'Add A'),
            task_row('b', 'a', 'medium', 'b.txt', 'Add B'),
            task_row('c', 'b', 'low', 'c.txt', 'Add C'),
        ])
        planned = self.plan(plan, '--collapse-chains')
        self.assertEqual(planned.returncode, 0, planned.stdout)
        self.assertIn(COLLAPSED % 3, planned.stdout)
        self.assertNotIn('sequentially', planned.stdout)
        self.assertFalse((plan / '.collapse.err').exists())

        rows = data_rows(plan / 'tasks.tsv')
        declared = data_rows(plan / 'tasks.declared.tsv')
        self.assertEqual([row[0] for row in rows], ['combined'])
        self.assertEqual([row[0] for row in declared], ['combined'])
        # Declared is the combined row before routing fills the `-` columns.
        self.assertEqual(declared[0][2], 'medium')
        self.assertEqual(declared[0][3], 'a.txt,b.txt,c.txt')
        self.assertEqual(declared[0][4:6], ['-', '-'])
        self.assertEqual(rows[0][2], 'medium')
        self.assertEqual(rows[0][4:6], ['sol', 'opus'])
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.precollapse.tsv')], ['a', 'b', 'c'])
        self.assertEqual(shape_of(plan)['shape'], 'one')
        self.assert_shape_matches_waves(plan)
        forecast, merged = plan_tasks(plan)
        self.assertEqual(forecast, [{'task': 'combined', 'tier': 'medium', 'spec': 'sol'}])
        self.assertEqual(merged, [])

        before = self.calls().count('dwarf')
        ran = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(self.calls().count('dwarf') - before, 1, ran.stdout)

    def test_collapse_chains_with_the_chain_in_a_review_group_plans_and_runs(self):
        # #257: groups.tsv still named the parts, so validate refused the collapsed plan.
        plan = self.write_plan('grouped', [
            task_row('a', '-', 'medium', 'a.txt', 'Package'),
            task_row('b', 'a', 'high', 'b.txt', 'Wire it'),
        ])
        (plan / 'groups.tsv').write_text('# id\treview\tqa\tmembers\ttitle\tgoal\n'
                                         'auth\ttask\t-\ta,b\tAuth\tboth parts\n')
        planned = self.plan(plan, '--collapse-chains')
        self.assertEqual(planned.returncode, 0, planned.stdout)
        self.assertIn(COLLAPSED % 2, planned.stdout)
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['combined'])
        self.assertFalse((plan / 'groups.tsv').exists())
        self.assertIn('a,b', (plan / 'groups.precollapse.tsv').read_text())
        before = self.calls().count('dwarf')
        ran = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(self.calls().count('dwarf') - before, 1, ran.stdout)

    def test_a_chain_without_the_flag_keeps_its_tasks_and_says_so(self):
        plan = self.write_plan('chain', [
            task_row('a', '-', 'low', 'a.txt', 'Add A'),
            task_row('b', 'a', 'medium', 'b.txt', 'Add B'),
            task_row('c', 'b', 'low', 'c.txt', 'Add C'),
        ])
        planned = self.plan(plan)
        self.assertEqual(planned.returncode, 0, planned.stdout)
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['a', 'b', 'c'])
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.declared.tsv')], ['a', 'b', 'c'])
        self.assertFalse((plan / 'collapsed.json').exists())
        self.assertFalse((plan / 'tasks.precollapse.tsv').exists())
        self.assertIn(SEQUENTIAL % 3, planned.stdout)
        self.assertNotIn('collapsed a sequential plan', planned.stdout)
        self.assertEqual(shape_of(plan), {'shape': 'chain', 'tasks': 3, 'waves': 3, 'widest': 1})
        self.assert_shape_matches_waves(plan)

    def test_a_branches_plan_is_unchanged_except_for_plan_shape(self):
        rows = [task_row('a', '-', 'low', 'a.txt', 'Add A'), task_row('b', '-', 'low', 'b.txt', 'Add B')]
        plain = self.write_plan('branch-plain', rows)
        flagged = self.write_plan('branch-flag', rows)
        without = self.plan(plain)
        with_flag = self.plan(flagged, '--collapse-chains')
        self.assertEqual(without.returncode, 0, without.stdout)
        self.assertEqual(with_flag.returncode, 0, with_flag.stdout)
        for name in ('tasks.tsv', 'tasks.declared.tsv', 'waves.tsv'):
            self.assertEqual((plain / name).read_bytes(), (flagged / name).read_bytes(), name)
        self.assertEqual(without.stdout.replace(str(plain), 'PLAN').replace(plain.name, 'PLAN'),
                         with_flag.stdout.replace(str(flagged), 'PLAN').replace(flagged.name, 'PLAN'))
        for planned, plan in ((without, plain), (with_flag, flagged)):
            self.assertNotIn('sequentially', planned.stdout)
            self.assertNotIn('collapsed a sequential plan', planned.stdout)
            self.assertFalse((plan / 'collapsed.json').exists())
            self.assertFalse((plan / 'tasks.precollapse.tsv').exists())
            self.assertEqual(shape_of(plan), {'shape': 'branches', 'tasks': 2, 'waves': 1, 'widest': 2})
            self.assert_shape_matches_waves(plan)
        self.assertEqual((plain / 'plan-shape.json').read_bytes(), (flagged / 'plan-shape.json').read_bytes())

    def test_a_one_task_plan_has_no_note_and_is_not_collapsed(self):
        for name, flags in (('one', ()), ('one-flag', ('--collapse-chains',))):
            plan = self.write_plan(name, [task_row('a', '-', 'low', 'a.txt', 'Add A')])
            planned = self.plan(plan, *flags)
            self.assertEqual(planned.returncode, 0, planned.stdout)
            self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['a'])
            self.assertNotIn('sequentially', planned.stdout)
            self.assertNotIn('collapsed a sequential plan', planned.stdout)
            self.assertFalse((plan / 'collapsed.json').exists())
            self.assertEqual(shape_of(plan)['shape'], 'one')

    def test_replanning_a_collapsed_plan_with_the_flag_stays_one_task(self):
        plan = self.write_plan('chain', [
            task_row('a', '-', 'low', 'a.txt', 'Add A'),
            task_row('b', 'a', 'low', 'b.txt', 'Add B'),
            task_row('c', 'b', 'low', 'c.txt', 'Add C'),
        ])
        first = self.plan(plan, '--collapse-chains')
        self.assertEqual(first.returncode, 0, first.stdout)
        kept = (plan / 'tasks.precollapse.tsv').read_bytes()
        again = self.plan(plan, '--collapse-chains')
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual([row[0] for row in data_rows(plan / 'tasks.tsv')], ['combined'])
        self.assertEqual((plan / 'tasks.precollapse.tsv').read_bytes(), kept)
        self.assertNotIn(COLLAPSED % 3, again.stdout)
        self.assertNotIn('already exists', again.stdout)
        self.assertNotIn('could not collapse', again.stdout)
        self.assertEqual(shape_of(plan)['shape'], 'one')

    def test_harvest_records_the_shape_and_an_older_plan_omits_it(self):
        plan = self.write_plan('chain', [
            task_row('a', '-', 'low', 'a.txt', 'Add A'),
            task_row('b', 'a', 'low', 'b.txt', 'Add B'),
            task_row('c', 'b', 'low', 'c.txt', 'Add C'),
        ])
        planned = self.plan(plan, '--collapse-chains')
        self.assertEqual(planned.returncode, 0, planned.stdout)
        event = harvest.plan_event(plan, dict(run='fixed-run'), src='import', inventory=False)
        info = shape_of(plan)
        self.assertEqual(event['data']['shape'], info['shape'])
        self.assertEqual(event['data']['widest'], info['widest'])
        # The count is `wave_count`; `waves` stays the task→wave map, as on every plan.
        self.assertEqual(event['data']['wave_count'], info['waves'])
        self.assertEqual(event['data']['waves'], {'combined': 1})
        self.assertIs(event['data']['collapsed'], True)
        self.assertEqual(event['data']['collapsed_from'], ['a', 'b', 'c'])
        self.assertNotIn('plan_gate', event['data'])

        (plan / 'plan-gate.json').write_text(json.dumps({'decision': 'plan', 'rule': 'size'}) + '\n')
        gated = harvest.plan_event(plan, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual(gated['data']['plan_gate'], {'decision': 'plan', 'rule': 'size'})
        self.assertNotEqual(gated['rid'], event['rid'])

        (plan / 'plan-shape.json').unlink()
        (plan / 'collapsed.json').unlink()
        (plan / 'plan-gate.json').unlink()
        stripped = harvest.plan_event(plan, dict(run='fixed-run'), src='import', inventory=False)
        for key in ('shape', 'widest', 'wave_count', 'collapsed', 'collapsed_from', 'plan_gate'):
            self.assertNotIn(key, stripped['data'])
        self.assertEqual(stripped['data']['waves'], {'combined': 1})
        self.assertNotEqual(stripped['rid'], event['rid'])

        # A plan built without those files, the same fixture test_plan_gates hashes.
        old = self.root / 'old'
        (old / 'tasks' / 'a').mkdir(parents=True)
        (old / 'goal.txt').write_text('keep the plan id\n')
        (old / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA title\n')
        (old / 'run_id').write_text('plan-rid\n')
        (old / 'tasks' / 'a' / 'prompt.md').write_text('do the thing\n')
        (old / 'tasks' / 'a' / 'approach.md').write_text('carefully\n')
        bare = harvest.plan_event(old, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual(bare['rid'], BARE_RID)
        for key in ('shape', 'widest', 'wave_count', 'collapsed', 'collapsed_from', 'plan_gate'):
            self.assertNotIn(key, bare['data'])
        self.assertEqual(bare['data']['waves'], {})

        # A three-task chain whose shape record says "waves": 3: the count goes to wave_count, and
        # waves.tsv's task→wave map stays `waves`, so the field has one type across the store.
        chain = self.root / 'shaped-chain'
        (chain / 'tasks').mkdir(parents=True)
        (chain / 'tasks.tsv').write_text(
            'a\t-\tlow\ta.txt\tsol\topus\tA\n'
            'b\ta\tlow\tb.txt\tsol\topus\tB\n'
            'c\tb\tlow\tc.txt\tsol\topus\tC\n')
        (chain / 'waves.tsv').write_text('1\ta\n2\tb\n3\tc\n')
        (chain / 'plan-shape.json').write_text(json.dumps(
            {'shape': 'chain', 'tasks': 3, 'waves': 3, 'widest': 1}) + '\n')
        shaped = harvest.plan_event(chain, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual(shaped['data']['shape'], 'chain')
        self.assertEqual(shaped['data']['widest'], 1)
        self.assertEqual(shaped['data']['wave_count'], 3)
        self.assertEqual(shaped['data']['waves'], {'a': 1, 'b': 2, 'c': 3})
        (chain / 'plan-shape.json').unlink()
        unshaped = harvest.plan_event(chain, dict(run='fixed-run'), src='import', inventory=False)
        self.assertEqual(unshaped['data']['waves'], {'a': 1, 'b': 2, 'c': 3})
        self.assertNotIn('shape', unshaped['data'])
        self.assertNotIn('widest', unshaped['data'])

    def test_split_does_not_carry_the_shape_or_the_collapse_record(self):
        # PLANS_OPTION_FILES is what split copies onto the new plan. Combine does not use
        # it. A split's table is a subset, and the next plan recomputes the shape, so a
        # copied plan-shape.json or collapsed.json would describe the plan before the split.
        text = (ROOT / 'scripts' / 'forge-lib-plans.sh').read_text()
        names = re.search(r'^PLANS_OPTION_FILES="([^"]*)"', text, re.M).group(1).split()
        self.assertNotIn('plan-shape.json', names)
        self.assertNotIn('collapsed.json', names)

    def test_long_task_ids_align_approval_status_and_results_tables(self):
        long_id = 'a-task-with-a-long-id'
        plan = self.write_plan('wide-ids', [
            task_row('a', '-', 'low', 'a.txt', 'Short task'),
            task_row(long_id, '-', 'low', 'b.txt', 'Long task'),
        ])
        (plan / 'tasks' / 'a' / 'approach.md').write_text('Step one.\n')
        planned = self.plan(plan, '--no-profile')
        self.assertEqual(planned.returncode, 0, planned.stdout)
        lines = planned.stdout.splitlines()
        hdr = next(line for line in lines if re.match(r'^  id\s+diff\s+dwarf\s+qa\s+files$', line))
        row_short = next(line for line in lines if line.startswith('  a '))
        row_long = next(line for line in lines if line.startswith('  ' + long_id + ' '))
        app_line = next(line for line in lines if '| Step one.' in line)
        for col in ('diff', 'dwarf', 'qa', 'files'):
            pos = hdr.index(col)
            self.assertNotEqual(row_short[pos], ' ', row_short)
            self.assertNotEqual(row_long[pos], ' ', row_long)
            self.assertEqual(row_short[pos - 1], ' ', row_short)
            self.assertEqual(row_long[pos - 1], ' ', row_long)
        self.assertEqual(app_line.index('|'), hdr.index('diff'))

        ran = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(ran.returncode, 0, ran.stdout)
        prompt_text = (plan / 'tasks' / 'a' / 'dwarf.prompt').read_text()
        st_lines = prompt_text.splitlines()
        st_hdr = next(line for line in st_lines if re.match(r'^id\s+diff\s+status\s+files$', line))
        st_table = st_lines[st_lines.index(st_hdr) + 1:]
        st_short = next(line for line in st_table if line.startswith('a '))
        st_long = next(line for line in st_table if line.startswith(long_id + ' '))
        for col in ('diff', 'status', 'files'):
            pos = st_hdr.index(col)
            self.assertEqual(st_short[pos - 1], ' ', st_short)
            self.assertEqual(st_long[pos - 1], ' ', st_long)
            self.assertNotEqual(st_short[pos], ' ', st_short)
            self.assertNotEqual(st_long[pos], ' ', st_long)

        res_lines = ran.stdout.splitlines()
        res_hdr = next(line for line in res_lines if re.match(r'^  id\s+status\s+branch$', line))
        res_table = res_lines[res_lines.index(res_hdr) + 1:]
        res_short = next(line for line in res_table if line.startswith('  a '))
        res_long = next(line for line in res_table if line.startswith('  ' + long_id + ' '))
        for col in ('status', 'branch'):
            pos = res_hdr.index(col)
            self.assertEqual(res_short[pos - 1], ' ', res_short)
            self.assertEqual(res_long[pos - 1], ' ', res_long)
            self.assertNotEqual(res_short[pos], ' ', res_short)
            self.assertNotEqual(res_long[pos], ' ', res_long)


if __name__ == '__main__':
    unittest.main()
