"""Plan shape and chain collapse: waves, the sixteen benchmark plans, and the combined task."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECK = ROOT / 'scripts' / 'forge-plan-check.py'
FIXTURES = ROOT / 'tests' / 'fixtures' / 'plan_shapes'
TAB = '\t'

spec = importlib.util.spec_from_file_location('forge_plan_check', CHECK)
plan_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan_check)

# shape, tasks, waves, widest. The shapes are the ones tests/fixtures/plan_shapes/README.md lists.
RECORDED = {
    'easy-ansible-decomp': ('one', 1, 1, 1),
    'easy-ansible-orch': ('chain', 2, 2, 1),
    'easy-element-decomp': ('one', 1, 1, 1),
    'easy-element-orch': ('one', 1, 1, 1),
    'easy-flipt-decomp': ('branches', 2, 1, 2),
    'easy-flipt-orch': ('branches', 3, 1, 3),
    'easy-openlibrary-decomp': ('chain', 2, 2, 1),
    'easy-openlibrary-orch': ('chain', 3, 3, 1),
    'easy-vuls-decomp': ('branches', 2, 1, 2),
    'easy-vuls-orch': ('branches', 2, 1, 2),
    'hard-ecr-decomp': ('branches', 3, 2, 2),
    'hard-ecr-lite': ('chain', 2, 2, 1),
    'hard-metrics-decomp': ('branches', 3, 2, 2),
    'hard-metrics-lite': ('branches', 3, 2, 2),
    'hard-seekbar-decomp': ('chain', 2, 2, 1),
    'hard-seekbar-lite': ('chain', 2, 2, 1),
}

TITLE_A = (
    'Parse flags and preserve every legacy switch the old CLI accepted, '
    'including the undocumented ones')
PROMPT_A = """# Build the parser

## Behavior

Keep the old flags.

```python
## still code
```

## Acceptance criteria

- parser accepts `--json`
- parser rejects a bare `-`

## After

Not criteria.
"""
PROMPT_B = 'Update the callers.\n'
PROMPT_C = """## Acceptance criteria

- callers compile
"""
APPROACH_A = 'Walk the argv once.\n'
APPROACH_C = '\nCall the parser from main.\n'
# A closing `#` run, a three-space indent, a four-space code line, and two fences whose
# inner ``` / ## Acceptance criteria must stay in the body. Part b's criteria heading is indented.
PROMPT_FENCED = """Intro line.

  ## Behavior ##

Do the thing.

   ## Note

Noted.

~~~~markdown
```
## Acceptance criteria
- example only
```
~~~~

````markdown
```
## Acceptance criteria
- also an example
```
````

    ## Acceptance criteria

    - indented code, not a heading

## Acceptance criteria ##

- real check
"""
PROMPT_INDENTED = """Setup.

  ## Acceptance criteria

- indented check
"""
EXPECTED_MARKDOWN = (
    'This task combines the 2 parts of a plan that could only run one after another. '
    'Do them in order, in this one task; each part\'s requirements and acceptance criteria still hold.\n'
    '\n'
    '## Part 1: Parse (a)\n'
    '\n'
    'Intro line.\n'
    '\n'
    '  ### Behavior ##\n'
    '\n'
    'Do the thing.\n'
    '\n'
    '   ### Note\n'
    '\n'
    'Noted.\n'
    '\n'
    '~~~~markdown\n'
    '```\n'
    '## Acceptance criteria\n'
    '- example only\n'
    '```\n'
    '~~~~\n'
    '\n'
    '````markdown\n'
    '```\n'
    '## Acceptance criteria\n'
    '- also an example\n'
    '```\n'
    '````\n'
    '\n'
    '    ## Acceptance criteria\n'
    '\n'
    '    - indented code, not a heading\n'
    '\n'
    '## Part 2: Render (b)\n'
    '\n'
    'Setup.\n'
    '\n'
    '## Acceptance criteria\n'
    '\n'
    '### Part 1: Parse\n'
    '\n'
    '- real check\n'
    '\n'
    '### Part 2: Render\n'
    '\n'
    '- indented check\n')
HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n# note: keep me\n\n'
EXPECTED_TITLE = ('3 parts: ' + TITLE_A)[:100]
EXPECTED_ROW = {
    'id': 'combined',
    'deps': '-',
    'difficulty': 'high',
    'files': 'z.py,a.py,m.py',
    'dwarf': '-',
    'qa': 'opus:high',
    'title': EXPECTED_TITLE,
}
EXPECTED_PROMPT = (
    'This task combines the 3 parts of a plan that could only run one after another. '
    'Do them in order, in this one task; each part\'s requirements and acceptance criteria still hold.\n'
    '\n'
    '## Part 1: ' + TITLE_A + ' (a)\n'
    '\n'
    '## Build the parser\n'
    '\n'
    '### Behavior\n'
    '\n'
    'Keep the old flags.\n'
    '\n'
    '```python\n'
    '## still code\n'
    '```\n'
    '\n'
    '### After\n'
    '\n'
    'Not criteria.\n'
    '\n'
    '## Part 2: Update callers (b)\n'
    '\n'
    'Update the callers.\n'
    '\n'
    '## Part 3: Wire the command (c)\n'
    '\n'
    '## Acceptance criteria\n'
    '\n'
    '### Part 1: ' + TITLE_A + '\n'
    '\n'
    '- parser accepts `--json`\n'
    '- parser rejects a bare `-`\n'
    '\n'
    '### Part 2: Update callers\n'
    '\n'
    '- (no criteria were written for this part)\n'
    '\n'
    '### Part 3: Wire the command\n'
    '\n'
    '- callers compile\n')
EXPECTED_APPROACH = (
    '### Part 1\n'
    '\n'
    'Walk the argv once.\n'
    '\n'
    '### Part 3\n'
    '\n'
    'Call the parser from main.\n')


def _combined_prompt(title, task_id, body, criteria):
    """One combined prompt: intro, one part body, and that part's criteria."""
    intro = (
        'This task combines the 1 parts of a plan that could only run one after another. '
        'Do them in order, in this one task; each part\'s requirements and acceptance criteria still hold.')
    return (
        '%s\n\n## Part 1: %s (%s)\n\n%s\n\n## Acceptance criteria\n\n### Part 1: %s\n\n%s\n'
        % (intro, title, task_id, body, title, criteria))


def task(task_id, deps='-', files='-', difficulty='low', dwarf='-', qa='-', title=None):
    return dict(id=task_id, deps=deps, difficulty=difficulty, files=files, dwarf=dwarf, qa=qa,
                title=title if title is not None else 'Title of ' + task_id)


def chain_rows():
    """Table order c, a, b. Wave order is a, then b, then c."""
    return [
        task('c', deps='b', difficulty='medium', files='-', dwarf='sonnet:medium', qa='opus:high',
             title='Wire the command'),
        task('a', difficulty='low', files='z.py,a.py', dwarf='sonnet:medium', qa='opus:high', title=TITLE_A),
        task('b', deps='a', difficulty='high', files='a.py,m.py', dwarf='haiku:xhigh', qa='opus:high',
             title='Update callers'),
    ]


def render(rows):
    return HEADER + ''.join(TAB.join(row[name] for name in plan_check.TASK_COLS) + '\n' for row in rows)


def fingerprint(plan):
    found = {}
    for path in plan.rglob('*'):
        if path.is_file():
            found[path.relative_to(plan).as_posix()] = (path.stat().st_mtime_ns, path.read_bytes())
    return found


class ShapeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def check(self, *args):
        return subprocess.run([sys.executable, str(CHECK), *args], capture_output=True, text=True)

    def plan_with(self, text):
        plan = self.root / 'plan'
        plan.mkdir(exist_ok=True)
        (plan / 'tasks.tsv').write_text(text)
        return plan

    def test_the_sixteen_benchmark_plans_have_their_recorded_shapes(self):
        self.assertEqual({path.stem for path in FIXTURES.glob('*.tsv')}, set(RECORDED))
        for name, (kind, tasks, wave_count, widest) in RECORDED.items():
            with self.subTest(name=name):
                plan = self.root / name
                plan.mkdir()
                (plan / 'tasks.tsv').write_bytes((FIXTURES / (name + '.tsv')).read_bytes())
                done = self.check('shape', str(plan))
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout),
                                 {'shape': kind, 'tasks': tasks, 'waves': wave_count, 'widest': widest})
                self.assertFalse((plan / 'plan-shape.json').exists())

    def test_write_saves_the_printed_json(self):
        plan = self.root / 'saved'
        plan.mkdir()
        (plan / 'tasks.tsv').write_bytes((FIXTURES / 'easy-ansible-orch.tsv').read_bytes())
        before = (plan / 'tasks.tsv').read_bytes()
        done = self.check('shape', str(plan), '--write')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((plan / 'plan-shape.json').read_text(), done.stdout)
        self.assertEqual(json.loads(done.stdout), {'shape': 'chain', 'tasks': 2, 'waves': 2, 'widest': 1})
        self.assertEqual((plan / 'tasks.tsv').read_bytes(), before)

    def test_waves_follow_deps_and_file_overlap(self):
        # (name, rows as (id, deps, files), waves)
        cases = (
            ('diamond', [('a', '-', 'a.py'), ('b', 'a', 'b.py'), ('c', 'a', 'c.py'), ('d', 'b,c', 'd.py')],
             [['a'], ['b', 'c'], ['d']]),
            ('same file, no deps', [('a', '-', 'a.py'), ('b', '-', 'a.py')],
             [['a'], ['b']]),
            ('directory holds a file', [('a', '-', 'src'), ('b', '-', 'src/main.py')],
             [['a'], ['b']]),
            ('trailing slash is a directory', [('a', '-', 'src/'), ('b', '-', 'src/main.py')],
             [['a'], ['b']]),
            ('a leading ./ is the same path', [('a', '-', './src/a.py'), ('b', '-', 'src/a.py')],
             [['a'], ['b']]),
            ('src does not contain src2', [('a', '-', 'src'), ('b', '-', 'src2/main.py')],
             [['a', 'b']]),
            ('dot takes the wave and blocks the next file', [('a', '-', '.'), ('b', '-', 'lib/b.py')],
             [['a'], ['b']]),
            ('a later non-overlapping file stays with the earlier wave',
             [('a', '-', 'lib/a.py'), ('b', '-', '.'), ('c', '-', 'lib/c.py')],
             [['a', 'c'], ['b']]),
            ('dash shares a wave with a file', [('a', '-', '-'), ('b', '-', 'lib/b.py')],
             [['a', 'b']]),
            ('dash overlaps nothing, so it joins a wave that already holds dot',
             [('a', '-', '.'), ('b', '-', '-')],
             [['a', 'b']]),
            ('dot overlaps a dash token already placed in the wave',
             [('a', '-', '-'), ('b', '-', '.')],
             [['a'], ['b']]),
        )
        for name, raw, expected in cases:
            with self.subTest(name=name):
                rows = [task(task_id, deps=deps, files=files) for task_id, deps, files in raw]
                self.assertEqual(plan_check.waves(rows), expected)
                info = plan_check.shape(rows)
                widest = max(len(group) for group in expected)
                kind = 'one' if len(raw) == 1 else ('chain' if widest == 1 else 'branches')
                self.assertEqual(info, {'shape': kind, 'tasks': len(raw), 'waves': len(expected), 'widest': widest})

    def test_a_cycle_or_an_unknown_dep_is_an_error(self):
        cycle = render([task('a', deps='b', files='a.py'), task('b', deps='a', files='b.py')])
        unknown = render([task('a', deps='missing', files='a.py')])
        for text, needle in ((cycle, 'cycle'), (unknown, 'unknown dep')):
            with self.subTest(needle=needle):
                plan = self.plan_with(text)
                before = fingerprint(plan)
                for args in (('shape', str(plan), '--write'), ('collapse', str(plan))):
                    done = self.check(*args)
                    self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
                    self.assertIn(needle, done.stderr)
                    self.assertEqual(done.stdout, '')
                    self.assertEqual(fingerprint(plan), before)
        with self.assertRaises(plan_check.PlanShapeError):
            plan_check.waves([task('a', deps='a')])
        with self.assertRaises(plan_check.PlanShapeError):
            plan_check.shape([task('a', deps='no-such-task')])


class CollapseTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def check(self, *args):
        return subprocess.run([sys.executable, str(CHECK), *args], capture_output=True, text=True)

    def write_plan(self, rows, prompts=None, approaches=None, extra=None):
        plan = self.root / 'plan'
        if plan.exists():
            raise AssertionError('one plan per test')
        plan.mkdir()
        text = render(rows)
        (plan / 'tasks.tsv').write_text(text)
        (plan / 'goal.txt').write_text('keep the goal\n')
        for row in rows:
            root = plan / 'tasks' / row['id']
            root.mkdir(parents=True)
            (root / 'prompt.md').write_text((prompts or {}).get(row['id'], 'do ' + row['id'] + '\n'))
            approach = (approaches or {}).get(row['id'])
            if approach is not None:
                (root / 'approach.md').write_text(approach)
            (root / 'notes.txt').write_text('notes for ' + row['id'] + '\n')
        for name, data in (extra or {}).items():
            path = plan / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return plan, text.encode()

    def test_collapse_rows_merges_the_chain_without_a_plan_dir(self):
        self.assertGreater(len('3 parts: ' + TITLE_A), 100)
        self.assertEqual(len(EXPECTED_TITLE), 100)
        row, prompt, approach = plan_check.collapse_rows(
            chain_rows(),
            {'a': PROMPT_A, 'b': PROMPT_B, 'c': PROMPT_C},
            {'a': APPROACH_A, 'c': APPROACH_C})
        self.assertEqual(row, EXPECTED_ROW)
        self.assertEqual(prompt, EXPECTED_PROMPT)
        self.assertEqual([line for line in prompt.splitlines() if line.lower().startswith('## acceptance criteria')],
                         ['## Acceptance criteria'])
        self.assertIn('## still code', prompt)
        self.assertNotIn('\n### still code', prompt)
        self.assertEqual(approach, EXPECTED_APPROACH)
        # No part had an approach worth writing.
        self.assertIsNone(plan_check.collapse_rows(chain_rows(), {'a': PROMPT_A, 'b': '', 'c': PROMPT_C}, {})[2])
        self.assertIsNone(plan_check.collapse_rows(chain_rows(), {}, {'a': '   \n', 'b': ''})[2])

    def test_a_chain_becomes_one_combined_task(self):
        rows = chain_rows()
        plan, original = self.write_plan(
            rows,
            {'a': PROMPT_A, 'b': PROMPT_B, 'c': PROMPT_C},
            {'a': APPROACH_A, 'c': APPROACH_C})
        done = self.check('collapse', str(plan))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            done.stdout,
            'collapsed a sequential plan of 3 tasks into one (tasks.precollapse.tsv keeps the original)\n')
        self.assertEqual((plan / 'tasks.precollapse.tsv').read_bytes(), original)
        self.assertEqual((plan / 'tasks.tsv').read_bytes(),
                         HEADER.encode() + TAB.join(EXPECTED_ROW[name] for name in plan_check.TASK_COLS).encode() + b'\n')
        self.assertEqual((plan / 'tasks' / 'combined' / 'prompt.md').read_text(), EXPECTED_PROMPT)
        self.assertEqual((plan / 'tasks' / 'combined' / 'approach.md').read_text(), EXPECTED_APPROACH)
        record = json.loads((plan / 'collapsed.json').read_text())
        self.assertEqual(record, {
            'from': ['a', 'b', 'c'],
            'into': 'combined',
            'shape': {'shape': 'chain', 'tasks': 3, 'waves': 3, 'widest': 1},
        })
        for task_id in ('a', 'b', 'c'):
            self.assertFalse((plan / 'tasks' / task_id).exists())
            kept = plan / 'tasks.precollapse' / task_id
            self.assertEqual((kept / 'notes.txt').read_text(), 'notes for ' + task_id + '\n')
        self.assertEqual((plan / 'tasks.precollapse' / 'a' / 'prompt.md').read_text(), PROMPT_A)
        self.assertFalse((plan / 'tasks.precollapse' / 'b' / 'approach.md').exists())
        self.assertEqual((plan / 'goal.txt').read_text(), 'keep the goal\n')
        leftovers = [path.name for path in plan.rglob('*') if path.name.startswith('.')]
        self.assertEqual(leftovers, [])

    def test_a_chain_with_no_approaches_writes_no_approach_file(self):
        plan, _original = self.write_plan(
            [task('a', files='a.py'), task('b', deps='a', files='b.py')])
        done = self.check('collapse', str(plan))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((plan / 'tasks' / 'combined' / 'prompt.md').is_file())
        self.assertFalse((plan / 'tasks' / 'combined' / 'approach.md').exists())

    def test_a_combined_task_takes_the_highest_risk_only_when_every_part_was_rated(self):
        # #122: an unrated part could be the riskiest, so a partly rated chain records no risk.
        cases = [
            ('every part rated', {'a': 'low\n', 'b': 'high\n', 'c': 'medium\n'}, 'high\n'),
            ('one part unrated', {'a': 'low\n', 'c': 'high\n'}, None),
            ('one part unreadable as a risk', {'a': 'low\n', 'b': 'severe\n', 'c': 'low\n'}, None),
        ]
        for name, risks, expected in cases:
            with self.subTest(name):
                shutil.rmtree(self.root / 'plan', ignore_errors=True)
                plan, _ = self.write_plan(chain_rows(), extra={'tasks/%s/risk' % tid: text.encode()
                                                               for tid, text in risks.items()})
                done = self.check('collapse', str(plan))
                self.assertEqual(done.returncode, 0, done.stderr)
                risk = plan / 'tasks' / 'combined' / 'risk'
                self.assertEqual(risk.read_text() if risk.exists() else None, expected)

    def test_a_branches_plan_and_a_single_task_are_left_untouched(self):
        for kind, rows in (
            ('branches', [task('a', files='a.py'), task('b', files='b.py')]),
            ('one', [task('only', files='only.py')]),
        ):
            with self.subTest(kind=kind):
                # A fresh directory: write_plan refuses a second plan in one test.
                self.root = self.root / kind
                self.root.mkdir()
                plan, _original = self.write_plan(rows)
                before = fingerprint(plan)
                done = self.check('collapse', str(plan))
                self.assertEqual((done.returncode, done.stdout), (0, 'not collapsed: %s\n' % kind))
                self.assertEqual(done.stderr, '')
                self.assertEqual(fingerprint(plan), before)

    def test_a_collapsed_plan_is_refused_the_second_time(self):
        plan, _original = self.write_plan(
            [task('a', files='a.py', dwarf='sol'), task('b', deps='a', files='b.py', dwarf='sol')])
        first = self.check('collapse', str(plan))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(plan_check.parse_task_rows((plan / 'tasks.tsv').read_text())[0]['dwarf'], 'sol')
        before = fingerprint(plan)
        second = self.check('collapse', str(plan))
        self.assertEqual(second.returncode, 2, second.stdout + second.stderr)
        self.assertEqual(second.stdout, '')
        self.assertIn('tasks.precollapse.tsv', second.stderr)
        self.assertEqual(fingerprint(plan), before)

    def test_markdown_headings_and_fences_land_in_the_combined_prompt(self):
        rows = [
            task('a', files='a.py', title='Parse'),
            task('b', deps='a', files='b.py', title='Render'),
        ]
        _row, prompt, approach = plan_check.collapse_rows(
            rows, {'a': PROMPT_FENCED, 'b': PROMPT_INDENTED}, {})
        self.assertEqual(prompt, EXPECTED_MARKDOWN)
        self.assertIsNone(approach)
        self.assertNotIn('no criteria were written', prompt)
        # The fenced examples stay put; only the combined section is a real criteria heading
        # outside a fence. A four-space line is code, so it is not demoted and not extracted.
        self.assertEqual(prompt.count('\n## Acceptance criteria\n'), 3)
        self.assertIn('\n    ## Acceptance criteria\n', prompt)
        self.assertNotIn('### Acceptance criteria', prompt)

    def test_a_tab_is_a_heading_separator_and_may_follow_a_closing_fence(self):
        # CommonMark: `##\tTitle` is a heading, and ```<tab> closes a fence. A space-only
        # scan leaves the criteria heading in the body and reports that none were written.
        rows = [task('a', files='a.py', title='Parse')]
        cases = (
            ('tab heading',
             '##\tBehavior\n\nKeep it.\n\n##\tAcceptance criteria\n- real check\n',
             '###\tBehavior\n\nKeep it.'),
            ('tab after fence',
             '```python\nprint(1)\n```\t\n\n## Acceptance criteria\n- real check\n',
             '```python\nprint(1)\n```\t'),
        )
        for name, raw, body in cases:
            with self.subTest(name=name):
                _row, prompt, approach = plan_check.collapse_rows(rows, {'a': raw}, {})
                self.assertIsNone(approach)
                self.assertEqual(prompt, _combined_prompt('Parse', 'a', body, '- real check'))
                self.assertNotIn('no criteria were written', prompt)
                self.assertEqual(prompt.count('\n## Acceptance criteria\n'), 1)

    def test_an_unreadable_prompt_refuses_and_keeps_the_plan(self):
        plan, _original = self.write_plan(
            [task('a', files='a.py'), task('b', deps='a', files='b.py')],
            {'a': 'requirements for a\n\n## Acceptance criteria\n\n- a works\n',
             'b': 'requirements for b\n'})
        target = plan / 'tasks' / 'a' / 'prompt.md'
        before = fingerprint(plan)
        target.chmod(0)
        try:
            done = self.check('collapse', str(plan))
        finally:
            for path in (target, plan / 'tasks.precollapse' / 'a' / 'prompt.md'):
                if path.exists():
                    path.chmod(0o644)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(done.stdout, '')
        self.assertIn('cannot read', done.stderr)
        self.assertIn('prompt.md', done.stderr)
        self.assertEqual(fingerprint(plan), before)

    def test_a_failed_collapse_leaves_the_plan_as_it_was(self):
        plan, _original = self.write_plan(
            [task('a', files='a.py'), task('b', deps='a', files='b.py')])
        # The part-directory destination cannot be created, so the rename never commits.
        (plan / 'tasks.precollapse').write_text('not a directory\n')
        before = fingerprint(plan)
        done = self.check('collapse', str(plan))
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(done.stdout, '')
        self.assertEqual(fingerprint(plan), before)


if __name__ == '__main__':
    unittest.main()
