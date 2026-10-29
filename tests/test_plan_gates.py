"""The plan-gates record: tasks.tsv edits that cannot cycle, the send-back history and file,
the block under the table, and the dataset field."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
TAB = '\t'
CHECK = ROOT / 'scripts' / 'forge-plan-check.py'

from forge_jev import plan_gates  # noqa: E402


def row(task, deps='-', files='-', title=None):
    return TAB.join([task, deps, 'low', files, '-', '-', title or 'Title of ' + task])


class PlanCase(unittest.TestCase):
    HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan = Path(tmp.name) / 'plan'
        self.plan.mkdir()

    def write_tasks(self, *rows, tail=''):
        (self.plan / 'tasks.tsv').write_text(self.HEADER + '# a comment\n' + ''.join(r + '\n' for r in rows) + tail)

    def tasks(self):
        return (self.plan / 'tasks.tsv').read_text()

    def column(self, task, col):
        for line in self.tasks().splitlines():
            if line.split(TAB)[0] == task:
                return line.split(TAB)[col]

    def check(self, *args):
        return subprocess.run([sys.executable, str(CHECK), *args], capture_output=True, text=True)


class AddFilesTests(PlanCase):
    def test_a_dash_becomes_the_first_name_and_other_lines_stay_byte_identical(self):
        self.write_tasks(row('a', files='x.py'), row('b'), row('c', deps='a', files='z.py'), tail='\n')
        before = self.tasks().split('\n')
        self.assertEqual(plan_gates.add_files(self.plan, 'b', ['src/new.py', 'src/other.py']),
                         ['src/new.py', 'src/other.py'])
        after = self.tasks().split('\n')
        self.assertEqual(self.column('b', 3), 'src/new.py,src/other.py')
        self.assertEqual([l for l in after if not l.startswith('b\t')], [l for l in before if not l.startswith('b\t')])
        self.assertEqual(after[after.index(next(l for l in after if l.startswith('b\t')))].split(TAB)[:3],
                         ['b', '-', 'low'])

    def test_appends_in_order_and_never_removes(self):
        self.write_tasks(row('a', files='x.py,y.py'))
        self.assertEqual(plan_gates.add_files(self.plan, 'a', ['b.py', 'a.py', 'y.py', 'b.py']), ['b.py', 'a.py'])
        self.assertEqual(self.column('a', 3), 'x.py,y.py,b.py,a.py')

    def test_a_name_under_a_declared_directory_is_a_no_op(self):
        for declared in ('src', 'src/'):
            self.write_tasks(row('a', files=declared + ',lib.py'))
            before = self.tasks()
            self.assertEqual(plan_gates.add_files(self.plan, 'a', ['src/deep/mod.py', 'lib.py', './src/x.py']), [])
            self.assertEqual(self.tasks(), before)

    def test_an_unknown_task_changes_nothing(self):
        self.write_tasks(row('a'))
        before = self.tasks()
        self.assertEqual(plan_gates.add_files(self.plan, 'zz', ['x.py']), [])
        self.assertEqual(self.tasks(), before)


class AddDepTests(PlanCase):
    def setUp(self):
        super().setUp()
        self.write_tasks(row('a', deps='b'), row('b', deps='c'), row('c'))

    def test_a_cycle_is_refused_and_a_transitive_dep_accepted(self):
        before = self.tasks()
        self.assertFalse(plan_gates.add_dep(self.plan, 'c', 'a'))
        self.assertFalse(plan_gates.add_dep(self.plan, 'c', 'b'))
        self.assertFalse(plan_gates.add_dep(self.plan, 'a', 'a'))
        self.assertEqual(self.tasks(), before)
        self.assertTrue(plan_gates.add_dep(self.plan, 'a', 'c'))
        self.assertEqual(self.column('a', 1), 'b,c')

    def test_an_unknown_id_is_refused(self):
        before = self.tasks()
        self.assertFalse(plan_gates.add_dep(self.plan, 'a', 'nope'))
        self.assertFalse(plan_gates.add_dep(self.plan, 'nope', 'a'))
        self.assertEqual(self.tasks(), before)

    def test_an_existing_dep_is_true_with_no_change(self):
        before = self.tasks()
        self.assertTrue(plan_gates.add_dep(self.plan, 'a', 'b'))
        self.assertEqual(self.tasks(), before)

    def test_a_dash_becomes_the_dep(self):
        self.write_tasks(row('p'), row('q'))
        self.assertTrue(plan_gates.add_dep(self.plan, 'p', 'q'))
        self.assertEqual(self.column('p', 1), 'q')
        self.assertEqual(self.column('q', 1), '-')


class WavesTests(unittest.TestCase):
    def test_an_added_dep_moves_the_task_to_a_later_wave_like_a_declared_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'repo'
            repo.mkdir()
            for args in (('init', '-q', '.'), ('config', 'user.email', 't@t'), ('config', 'user.name', 't')):
                subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
            (repo / 'f.txt').write_text('x\n')
            subprocess.run(['git', '-C', str(repo), 'add', '-A'], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(repo), 'commit', '-qm', 'init'], check=True, capture_output=True)
            plan = root / 'plan'
            plan.mkdir()
            (plan / 'goal.txt').write_text('a goal\n')
            (plan / 'tasks.tsv').write_text(PlanCase.HEADER + row('readme', files='README.md') + '\n'
                                            + row('cli', files='cli.py') + '\n')

            def run_plan():
                done = subprocess.run(
                    ['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'plan', str(plan), '--repo', str(repo),
                     '--dwarf-low', 'luna', '--qa', 'opus'],
                    capture_output=True, text=True,
                    env=dict(os.environ, FORGE_RIPWIRE='off', FORGE_JEV='off', FORGE_DECIDE='off'))
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                return dict(reversed(l.split(TAB)) for l in (plan / 'waves.tsv').read_text().splitlines())

            self.assertEqual(run_plan(), dict(readme='1', cli='1'))
            self.assertTrue(plan_gates.add_dep(plan, 'readme', 'cli'))
            self.assertEqual(run_plan(), dict(readme='2', cli='1'))


class SendbackTests(PlanCase):
    def record(self):
        return {
            'prompt': {'t1': {'p': 0.31, 'shadow': False}},
            'coverage': {'requirements': ['the CLI prints a summary line'],
                         'uncovered': [{'text': 'the CLI prints a summary line', 'sha': 'abc', 'p': 0.9,
                                        'shadow': False}]},
        }

    def test_mark_pending_is_once_ever_even_after_sendback(self):
        self.write_tasks(row('t1', title='Write the parser'))
        data = self.record()
        self.assertTrue(plan_gates.mark_pending(data, 'prompt', 't1'))
        plan_gates.save(self.plan, data)
        self.assertTrue(plan_gates.sendback(self.plan))
        data = plan_gates.load(self.plan)
        self.assertEqual(data['pending']['prompt'], [])
        self.assertFalse(plan_gates.mark_pending(data, 'prompt', 't1'))
        plan_gates.save(self.plan, data)
        self.assertFalse(plan_gates.sendback(self.plan))
        self.assertFalse((self.plan / 'plan.sendback.md').exists())

    def test_a_coverage_sha_goes_back_once_even_when_its_entry_is_removed_and_re_added(self):
        self.write_tasks(row('t1'))
        data = self.record()
        self.assertTrue(plan_gates.mark_pending(data, 'coverage', 'abc'))
        plan_gates.save(self.plan, data)
        plan_gates.sendback(self.plan)
        data = plan_gates.load(self.plan)
        entry = data['coverage']['uncovered']
        data['coverage']['uncovered'] = []
        self.assertTrue(plan_gates.mark_pending(data, 'coverage', 'other-not-listed'))
        data['coverage']['uncovered'] = entry
        self.assertFalse(plan_gates.mark_pending(data, 'coverage', 'abc'))
        self.assertEqual(data['sent_back']['coverage'], ['abc', 'other-not-listed'])

    def test_sendback_writes_both_sections_and_keeps_the_history(self):
        self.write_tasks(row('t1', title='Write the parser'))
        data = self.record()
        plan_gates.mark_pending(data, 'prompt', 't1')
        plan_gates.mark_pending(data, 'coverage', 'abc')
        plan_gates.save(self.plan, data)
        self.assertTrue(plan_gates.sendback(self.plan))
        text = (self.plan / 'plan.sendback.md').read_text()
        self.assertEqual(text, "\n".join([
            "# Sent back by forge's plan gates",
            "",
            "Rewrite what is listed here, then run `plan` again. Forge sends each task and requirement back once:",
            "anything still flagged next time is listed under the table instead.",
            "",
            "## Task prompts a reviewer could not judge (prompt gate, below 0.55)",
            '- t1 — "Write the parser" (0.31): tasks/t1/prompt.md',
            "",
            "## Requirements no task covers (coverage gate)",
            '- "the CLI prints a summary line" (0.90): add it to a task\'s ## Acceptance criteria, or add a task',
            ""]))
        saved = plan_gates.load(self.plan)
        self.assertEqual(saved['pending'], {'prompt': [], 'coverage': []})
        self.assertEqual(saved['sent_back'], {'prompt': ['t1'], 'coverage': ['abc']})
        self.assertFalse(plan_gates.sendback(self.plan))
        self.assertFalse((self.plan / 'plan.sendback.md').exists())

    def test_a_section_with_no_items_is_left_out(self):
        self.write_tasks(row('t1'))
        data = self.record()
        plan_gates.mark_pending(data, 'coverage', 'abc')
        plan_gates.save(self.plan, data)
        plan_gates.sendback(self.plan)
        text = (self.plan / 'plan.sendback.md').read_text()
        self.assertNotIn('Task prompts', text)
        self.assertIn('Requirements no task covers', text)

    def test_nothing_pending_creates_no_record(self):
        self.write_tasks(row('t1'))
        self.assertFalse(plan_gates.sendback(self.plan))
        self.assertFalse((self.plan / 'plan-gates.json').exists())

    def test_the_cli_exits_4_once_then_0(self):
        self.write_tasks(row('t1', title='Write the parser'))
        data = self.record()
        plan_gates.mark_pending(data, 'prompt', 't1')
        plan_gates.save(self.plan, data)
        first = self.check('sendback', str(self.plan))
        self.assertEqual(first.returncode, 4, first.stderr)
        self.assertEqual(first.stdout, (self.plan / 'plan.sendback.md').read_text())
        second = self.check('sendback', str(self.plan))
        self.assertEqual((second.returncode, second.stdout), (0, ''))


class MarksTests(PlanCase):
    RECORD = {
        'drift': {'t2': {'added': [{'file': 'tests/test_route.py', 'p': 0.84}]},
                  't3': {'would_add': [{'file': 'ui/static/app.js', 'p': 0.71}]}},
        'forward': {'added': [{'earlier': 'readme', 'later': 'cli', 'p': 0.88}],
                    'skipped_cycle': [{'earlier': 'c', 'later': 'd', 'p': 0.7}]},
        'prompt': {'t4': {'p': 0.31, 'shadow': True},
                   't1': {'p': 0.31, 'shadow': False, 'after': 0.42}},
        'coverage': {'uncovered': [{'text': 'the CLI prints a summary line', 'sha': 'abc', 'p': 0.9, 'shadow': False},
                                   {'text': 'never sent back', 'sha': 'def', 'p': 0.8, 'shadow': False}]},
        'sent_back': {'prompt': ['t1'], 'coverage': ['abc']},
        'pending': {'prompt': [], 'coverage': []},
    }
    EXPECTED = [
        'decision-model gates:',
        '  drift added to t2: tests/test_route.py (0.84)',
        '  drift (shadow) would add to t3: ui/static/app.js (0.71)',
        '  forward added a dependency: readme now waits for cli (0.88)',
        '  forward skipped a dependency that would form a cycle: c on d (0.70)',
        '  prompt (shadow) t4: 0.31, below 0.55',
        '  prompt still below 0.55 after its rewrite: t1 (0.42)',
        '  requirements no task covers: "the CLI prints a summary line" (0.90)',
    ]

    def test_the_record_becomes_the_designs_lines(self):
        plan_gates.save(self.plan, self.RECORD)
        self.assertEqual(plan_gates.marks(self.plan), self.EXPECTED)

    def test_a_prompt_that_passed_its_rewrite_is_not_listed(self):
        data = json.loads(json.dumps(self.RECORD))
        data['prompt']['t1']['after'] = 0.8
        plan_gates.save(self.plan, data)
        self.assertNotIn('  prompt still below 0.55 after its rewrite: t1 (0.42)', plan_gates.marks(self.plan))

    def test_empty_missing_or_unreadable_records_give_nothing(self):
        self.assertEqual(plan_gates.marks(self.plan), [])
        (self.plan / 'plan-gates.json').write_text('{}')
        self.assertEqual(plan_gates.marks(self.plan), [])
        (self.plan / 'plan-gates.json').write_text('{"sent_back": {"prompt": ["t1"]}}')
        self.assertEqual(plan_gates.marks(self.plan), [])
        (self.plan / 'plan-gates.json').write_text('not json')
        self.assertEqual((plan_gates.load(self.plan), plan_gates.marks(self.plan)), ({}, []))

    def test_the_cli_prints_them(self):
        plan_gates.save(self.plan, self.RECORD)
        done = self.check('gates', str(self.plan))
        self.assertEqual((done.returncode, done.stdout.splitlines()), (0, self.EXPECTED))
        empty = Path(self.plan) / 'empty'
        empty.mkdir()
        done = self.check('gates', str(empty))
        self.assertEqual((done.returncode, done.stdout), (0, ''))


class PlanEventTests(PlanCase):
    def event(self):
        from forge_dataset import harvest
        (self.plan / 'tasks' / 'a').mkdir(parents=True, exist_ok=True)
        (self.plan / 'goal.txt').write_text('keep the plan id\n')
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA title\n')
        (self.plan / 'run_id').write_text('plan-rid\n')
        (self.plan / 'tasks' / 'a' / 'prompt.md').write_text('do the thing\n')
        (self.plan / 'tasks' / 'a' / 'approach.md').write_text('carefully\n')
        return harvest.plan_event(self.plan, dict(run='fixed-run'), src='import', inventory=False)

    def test_the_record_joins_the_event_and_the_id_only_when_the_file_exists(self):
        bare = self.event()
        # The id of this fixture in test_dataset: a plan without the file keeps it.
        self.assertEqual(bare['rid'], 'plan:fixed-run:26062ad6ab65e2f6')
        self.assertNotIn('plan_gates', bare['data'])
        (self.plan / 'plan-gates.json').write_text(json.dumps({'sent_back': {'prompt': ['a'], 'coverage': []}}))
        first = self.event()
        self.assertEqual(first['data']['plan_gates'], {'sent_back': {'prompt': ['a'], 'coverage': []}})
        self.assertNotEqual(first['rid'], bare['rid'])

    def test_records_that_differ_after_their_first_line_have_different_ids(self):
        (self.plan / 'plan-gates.json').write_text('{\n"sent_back": {"prompt": ["a"]}\n}\n')
        one = self.event()
        (self.plan / 'plan-gates.json').write_text('{\n"sent_back": {"prompt": ["b"]}\n}\n')
        two = self.event()
        (self.plan / 'plan-gates.json').write_text('{"sent_back":\n{"prompt": ["b"]}}\n')
        same = self.event()
        self.assertNotEqual(one['rid'], two['rid'])
        self.assertEqual(two['rid'], same['rid'])


if __name__ == '__main__':
    unittest.main()
