"""scripts/forge-planner.py: the prompt, the strict reply parse, the pre-plan gate, one-task."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'forge-planner.py'
REQUEST = 'Add a `limits` section to the config.\n\n- It has a max_items key.\n- Defaults apply.\n'
CRITERIA = '\n\n## Acceptance criteria\n\n- the check passes'


def task(tid='parse-config', deps=(), difficulty='medium', files=('src/config.py', 'tests/test_config.py'),
         title='Parse the new config section', approach='Add parse_section().', prompt=None):
    return {'id': tid, 'deps': list(deps), 'difficulty': difficulty, 'files': list(files),
            'title': title, 'approach': approach, 'prompt': prompt or f'Do {tid}.{CRITERIA}'}


def reply(tasks, text_before='Here is the plan.\n'):
    return f'{text_before}```forge-plan\n{json.dumps({"tasks": tasks})}\n```\nDone.\n'


class PlannerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.plan = self.dir / 'plan'
        self.plan.mkdir()
        self.goal = self.dir / 'goal.md'
        self.goal.write_text(REQUEST)

    def run_planner(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True,
                              text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})

    def files(self):
        return sorted(str(p.relative_to(self.plan)) for p in self.plan.rglob('*') if p.is_file())


class ParseTests(PlannerCase):
    def parse(self, text, request=True):
        if request:
            (self.plan / 'request.md').write_text(REQUEST)
        path = self.dir / 'reply.txt'
        path.write_text(text)
        return self.run_planner('parse', '--plan', self.plan, '--reply', path), path

    def test_one_task(self):
        r, _ = self.parse(reply([task()]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.plan / 'tasks.tsv').read_text(),
                         '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
                         'parse-config\t-\tmedium\tsrc/config.py,tests/test_config.py\t-\t-\tParse the new config section\n')
        self.assertEqual((self.plan / 'tasks/parse-config/prompt.md').read_text(),
                         f'Do parse-config.{CRITERIA}\n\n## The whole request\n\n{REQUEST}')
        self.assertEqual((self.plan / 'tasks/parse-config/approach.md').read_text(), 'Add parse_section().')
        self.assertEqual(self.files(), ['request.md', 'tasks.tsv', 'tasks/parse-config/approach.md',
                                        'tasks/parse-config/prompt.md'])

    def test_chain_and_empty_approach(self):
        r, _ = self.parse(reply([task('a', files=['a.py'], approach=''),
                                 task('b', ['a'], 'high', ['b.py']),
                                 task('c', ['a', 'b'], 'low', ['c.py'])]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = (self.plan / 'tasks.tsv').read_text().splitlines()[1:]
        self.assertEqual(rows, ['a\t-\tmedium\ta.py\t-\t-\tParse the new config section',
                                'b\ta\thigh\tb.py\t-\t-\tParse the new config section',
                                'c\ta,b\tlow\tc.py\t-\t-\tParse the new config section'])
        self.assertFalse((self.plan / 'tasks/a/approach.md').exists())
        self.assertTrue((self.plan / 'tasks/b/approach.md').exists())

    def test_two_branches(self):
        r, _ = self.parse(reply([task('left', files=['l.py']), task('right', files=['r.py']),
                                 task('join', ['left', 'right'], 'low', ['out.html'])]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('join\tleft,right\tlow\tout.html\t-\t-\t', (self.plan / 'tasks.tsv').read_text())
        for tid in ('left', 'right', 'join'):
            self.assertTrue((self.plan / 'tasks' / tid / 'prompt.md').read_text().endswith(REQUEST))

    def test_success_clears_a_stale_refusal(self):
        (self.plan / 'planner.refused').write_text('old\n')
        r, _ = self.parse(reply([task()]))
        self.assertEqual(r.returncode, 0)
        self.assertFalse((self.plan / 'planner.refused').exists())

    def test_heading_with_crlf_and_u2028(self):
        crlf = 'Change app.\r\n\r\n## Acceptance criteria\r\n- Tests pass.'
        text = reply([task('a', files=['a.py'], prompt=crlf), task('b', files=['b.py'], prompt='x\u2028y' + CRITERIA)])
        r, _ = self.parse(text)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.plan / 'tasks/a/prompt.md').read_bytes().startswith(crlf.encode()))
        self.assertIn('x\u2028y', (self.plan / 'tasks/b/prompt.md').read_text())

    def test_crlf_reply_and_request_are_kept(self):
        request = 'Do this.\r\n\r\n\r\n'
        (self.plan / 'request.md').write_bytes(request.encode())
        path = self.dir / 'reply.txt'
        path.write_bytes(reply([task()]).replace('\n', '\r\n').encode())
        r = self.run_planner('parse', '--plan', self.plan, '--reply', path)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.plan / 'tasks/parse-config/prompt.md').read_bytes().endswith(request.encode()))

    def test_fenced_heading_is_not_a_heading(self):
        fenced = 'Update this example:\n```md\n## Acceptance criteria\n```\n'
        self.assert_refused(self.broken(prompt=fenced), 'no "## Acceptance criteria"')
        both = fenced + CRITERIA
        r, _ = self.parse(self.broken(prompt=both))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_nul_and_surrogate_are_refused_before_any_write(self):
        good = task('a', files=['a.py'])
        self.assert_refused(reply([good, task('b', files=['src/app\x00.py'])]), 'control character')
        (self.plan / 'planner.refused').unlink()
        surrogate = reply([good, task('b', files=['b.py'], prompt='x\\ud800' + CRITERIA)]).replace('\\\\ud800', '\\ud800')
        self.assertIn('\\ud800', surrogate)
        self.assert_refused(surrogate, 'lone surrogate')

    def test_empty_approach_removes_a_stale_one(self):
        stale = self.plan / 'tasks/parse-config/approach.md'
        stale.parent.mkdir(parents=True)
        stale.write_text('old instructions')
        r, _ = self.parse(reply([task(approach='')]))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(stale.exists())

    def assert_refused(self, text, reason):
        r, path = self.parse(text)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn(reason, r.stdout)
        self.assertIn(str(path), r.stdout)
        refused = (self.plan / 'planner.refused').read_text()
        self.assertIn(reason, refused)
        self.assertIn(str(path), refused)
        self.assertEqual(self.files(), ['planner.refused', 'request.md'])

    def broken(self, **changes):
        t = task()
        t.update(changes)
        return reply([t])

    def test_refusals(self):
        block = reply([task()])
        cases = {
            'two blocks': (block + block, '2 forge-plan blocks'),
            'no block': ('just prose, no plan', 'no forge-plan block'),
            'unclosed': ('```forge-plan\n{"tasks": []}', 'never closed'),
            'not json': ('```forge-plan\nnope\n```', 'not valid JSON'),
            'extra top key': ('```forge-plan\n{"tasks": [], "x": 1}\n```', 'unexpected x'),
            'no tasks': (reply([]), '1 to 20'),
            '21 tasks': (reply([task(f't{i}', files=[f'f{i}']) for i in range(21)]), '1 to 20 objects (21)'),
            'extra key': (self.broken(owner='me'), 'unexpected owner'),
            'missing key': (self.broken(approach=None).replace('"approach": null, ', ''), 'missing approach'),
            'bad id': (self.broken(id='Bad_Id'), 'does not match'),
            'duplicate id': (reply([task('a', files=['a']), task('a', files=['b'])]), 'duplicate id'),
            'unknown dep': (reply([task('a', ['ghost'])]), "unknown dep 'ghost'"),
            'cycle': (reply([task('a', ['b'], files=['a']), task('b', ['c'], files=['b']),
                             task('c', ['a'], files=['c'])]), 'a -> b -> c -> a'),
            'self cycle': (reply([task('a', ['a'])]), 'a -> a'),
            'difficulty': (self.broken(difficulty='hard'), "difficulty 'hard'"),
            'dotdot': (self.broken(files=['../x']), '.. component'),
            'absolute': (self.broken(files=['/etc/passwd']), 'absolute path'),
            'comma in file': (self.broken(files=['a,b']), 'tab, comma, newline or control'),
            'tab in file': (self.broken(files=['a\tb']), 'tab, comma, newline or control'),
            'no files': (self.broken(files=[]), 'non-empty list'),
            'multiline title': (self.broken(title='a\nb'), 'one non-empty line'),
            'tab title': (self.broken(title='a\tb'), 'one non-empty line'),
            'approach type': (self.broken(approach=['x']), 'approach is not a string'),
            'empty prompt': (self.broken(prompt='  '), 'prompt is empty'),
            'no criteria': (self.broken(prompt='Do it.'), 'no "## Acceptance criteria"'),
            'criteria in prose': (self.broken(prompt='see ## Acceptance criteria below'), 'no "## Acceptance'),
        }
        for name, (text, reason) in cases.items():
            with self.subTest(name):
                for p in self.plan.iterdir():
                    p.unlink()
                self.assert_refused(text, reason)

    def test_missing_request_is_a_usage_error(self):
        r, _ = self.parse(reply([task()]), request=False)
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.files(), [])

    def test_existing_tasks_tsv_is_never_overwritten(self):
        (self.plan / 'tasks.tsv').write_text('mine\n')
        r, _ = self.parse(reply([task()]))
        self.assertEqual(r.returncode, 2)
        self.assertEqual((self.plan / 'tasks.tsv').read_text(), 'mine\n')
        self.assertFalse((self.plan / 'tasks').exists())


class GateTests(PlannerCase):
    def gate(self, text):
        self.goal.write_text(text)
        r = self.run_planner('gate', '--plan', self.plan, '--goal-file', self.goal)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip(), json.loads((self.plan / 'plan-gate.json').read_text())

    def test_chars_edge(self):
        text = 'Fix the bug in src/app.py now. ' * 100
        self.assertGreater(len(text), 1500)
        decision, record = self.gate(text[:1500])
        self.assertEqual((decision, record['signals']['chars']), ('one-task', 1500))
        decision, record = self.gate(text[:1501])
        self.assertEqual((decision, record['signals']['chars']), ('plan', 1501))

    def test_trailing_newline_counts(self):
        decision, record = self.gate('x' * 1500 + '\n')
        self.assertEqual((decision, record['signals']['chars']), ('plan', 1501))

    def test_requirements_edge(self):
        decision, record = self.gate(''.join(f'- Requirement number {i} holds\n' for i in range(6)))
        self.assertEqual((decision, record['signals']['requirements']), ('one-task', 6))
        decision, record = self.gate(''.join(f'- Requirement number {i} holds\n' for i in range(7)))
        self.assertEqual((decision, record['signals']['requirements']), ('plan', 7))

    def test_record(self):
        _, record = self.gate('Change src/app.py and tests/test_app.py so the cache expires. Done.\n')
        self.assertEqual(record['decision'], 'one-task')
        self.assertEqual(record['thresholds'], {'chars': 1500, 'requirements': 6})
        self.assertEqual(record['rule'], 'size')
        self.assertEqual(sorted(record['signals']), ['chars', 'paths', 'requirements'])
        self.assertEqual(record['signals']['paths'], 2)
        self.assertEqual(self.files(), ['plan-gate.json'])

    def test_refuses_an_existing_plan(self):
        (self.plan / 'tasks.tsv').write_text('mine\n')
        r = self.run_planner('gate', '--plan', self.plan, '--goal-file', self.goal)
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.plan / 'plan-gate.json').exists())


class OneTaskTests(PlannerCase):
    def test_files(self):
        self.goal.write_text('## Add a limits section\n\nIt has max_items.\n')
        r = self.run_planner('one-task', '--plan', self.plan, '--goal-file', self.goal)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.plan / 'tasks.tsv').read_text(),
                         '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
                         'task\t-\tmedium\t.\t-\t-\tAdd a limits section\n')
        self.assertEqual((self.plan / 'tasks/task/prompt.md').read_text(),
                         '## Add a limits section\n\nIt has max_items.\n\n## Acceptance criteria\n\n'
                         '- Every requirement of the request above is met, and the tests for the files you changed pass.\n')
        self.assertEqual(self.files(), ['tasks.tsv', 'tasks/task/prompt.md'])

    def test_request_is_kept_verbatim(self):
        request = 'Do this.\r\n\n\n'
        self.goal.write_bytes(request.encode())
        r = self.run_planner('one-task', '--plan', self.plan, '--goal-file', self.goal)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.plan / 'tasks/task/prompt.md').read_bytes().startswith(request.encode()))

    def test_difficulty_and_title_cut(self):
        self.goal.write_text('\n\n# ' + 'x\ty' * 60 + '\nbody\n')
        r = self.run_planner('one-task', '--plan', self.plan, '--goal-file', self.goal, '--difficulty', 'high')
        self.assertEqual(r.returncode, 0, r.stderr)
        row = (self.plan / 'tasks.tsv').read_text().splitlines()[1].split('\t')
        self.assertEqual(len(row), 7)
        self.assertEqual(row[2], 'high')
        self.assertEqual(len(row[6]), 80)
        self.assertNotIn('#', row[6])

    def test_refuses_an_existing_plan(self):
        (self.plan / 'tasks.tsv').write_text('mine\n')
        r = self.run_planner('one-task', '--plan', self.plan, '--goal-file', self.goal)
        self.assertEqual(r.returncode, 2)
        self.assertEqual((self.plan / 'tasks.tsv').read_text(), 'mine\n')
        self.assertFalse((self.plan / 'tasks').exists())


class PromptTests(PlannerCase):
    def prompt(self, *extra):
        r = self.run_planner('prompt', '--plan', self.plan, '--goal-file', self.goal, *extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(self.plan / 'planner.prompt'))
        return (self.plan / 'planner.prompt').read_text()

    def test_contents_and_order(self):
        text = self.prompt('--level', 'high')
        self.assertIn(REQUEST.rstrip('\n'), text)
        self.assertIn('read-only', text)
        self.assertIn('typically 7-12', text)
        self.assertNotIn('typically 4-6', text)
        for needle in ('One task is a valid answer', 'A pure chain is worse than one task',
                       'Prefer branches that can run in parallel', 'Never two tasks that edit the same file',
                       'single-file deliverable', 'src/physics.js', 'final low-difficulty task',
                       '## Acceptance criteria', 'Never ask for the whole test suite',
                       '7000 characters of prompt and 9 files', '```forge-plan', '"tasks"'):
            self.assertIn(needle, text)
        self.assertLess(text.index('read-only'), text.index(REQUEST.rstrip('\n')))
        self.assertLess(text.index(REQUEST.rstrip('\n')), text.index('A pure chain'))
        self.assertLess(text.index('A pure chain'), text.index('# Reply format'))

    def test_request_is_kept_verbatim(self):
        request = 'Do this.\r\n\n\n'
        self.goal.write_bytes(request.encode())
        self.prompt()
        self.assertIn(request, (self.plan / 'planner.prompt').read_bytes().decode())

    def test_each_level_and_none(self):
        for level, words in (('low', '2-3 coarse'), ('medium', 'typically 4-6')):
            self.assertIn(words, self.prompt('--level', level))
            (self.plan / 'planner.prompt').unlink()
        self.assertNotIn('Decomposition level', self.prompt())

    def test_example_is_a_valid_reply(self):
        text = self.prompt()
        start = text.index('```forge-plan\n{"tasks"')
        (self.plan / 'request.md').write_text(REQUEST)
        reply_file = self.dir / 'example.txt'
        reply_file.write_text(text[start:text.index('```\n', start + 5) + 4])
        r = self.run_planner('parse', '--plan', self.plan, '--reply', reply_file)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_refuses_an_existing_plan(self):
        (self.plan / 'tasks.tsv').write_text('mine\n')
        r = self.run_planner('prompt', '--plan', self.plan, '--goal-file', self.goal)
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.plan / 'planner.prompt').exists())


if __name__ == '__main__':
    unittest.main()
