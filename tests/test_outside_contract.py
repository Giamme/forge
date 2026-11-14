"""#149: `{outside=path}` on a finding, and `FORGE_BLOCKED` from an implementer.

A blocking finding whose only fix is a file the task does not own used to be retried until
the budget ran out. The reviewer names that file; the implementer reports it instead of
editing it. outside-stop reads both. This file is the contract those callers share.
"""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-contract.py'


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


contract = module('forge-contract')

FACTS = dict(reach='main', origin='new', evidence='ran')
HEAD = '- [P1][CONFIRMED] a.py:1 — breaks'


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                          capture_output=True, text=True, encoding='utf-8')


class TailTests(unittest.TestCase):
    PHRASES = (
        'outside the files this task declared',
        '{outside=<repo-relative path>}',
        'several paths comma-separated',
        'Write {outside=…} in its own braces, immediately before the facts brace, like the tasks tag',
        "Use it only when the fix cannot be made inside the task's files",
        '{outside=a.py,b.py} {reach=main origin=new evidence=ran}',
    )

    def test_task_solo_and_group_teach_the_outside_brace(self):
        for kind in ('task', 'solo', 'group'):
            tail = contract.qa_text('tail', kind)
            flat = ' '.join(tail.split())
            for phrase in self.PHRASES:
                self.assertIn(phrase, flat, (kind, phrase))
            # The example is a contract bullet with the placeholder path, so it is not a finding.
            self.assertEqual(contract.parse_findings(tail), [])
            self.assertNotIn('FORGE_BLOCKED', tail, kind)
            long = [line for line in tail.splitlines()[:-2] if len(line) > 95]
            self.assertEqual(long, [], kind)

    def test_final_sweep_and_adopt_are_unchanged(self):
        for kind in ('final', 'sweep', 'adopt'):
            tail = contract.qa_text('tail', kind)
            self.assertNotIn('{outside=', tail, kind)
            self.assertNotIn('files this task declared', tail, kind)
            self.assertNotIn('FORGE_BLOCKED', tail, kind)

    def test_the_outside_paragraph_follows_the_severity_rules(self):
        # Group attribution stays its own paragraph and still follows this one.
        task = contract.qa_text('tail', 'task')
        group = contract.qa_text('tail', 'group')
        self.assertLess(task.index('P3  minor'), task.index('{outside=<repo-relative path>}'))
        self.assertLess(task.index('{outside=a.py,b.py}'), task.index('Blocking threshold:'))
        self.assertLess(group.index('{outside=a.py,b.py}'), group.index('{tasks=a,b}'))

    def test_dwarf_teaches_forge_blocked(self):
        for text in (contract.dwarf_text(), contract.dwarf_text(pre_review=True),
                     contract.dwarf_text(verify_after_merge=True)):
            flat = ' '.join(text.split())
            self.assertIn("in a file outside your task's files, do not edit that file", flat)
            self.assertIn('one line per path', flat)
            self.assertIn('FORGE_BLOCKED: <path>: <why>', text)
            self.assertNotIn('{outside=', text)


class OutsidePathsTests(unittest.TestCase):
    def test_paths(self):
        cases = (
            ('one path', '{outside=src/a.py}', ['src/a.py']),
            ('two, comma and space', '{outside=src/a.py, src/b.py}', ['src/a.py', 'src/b.py']),
            ('two, comma only', '{outside=src/a.py,src/b.py}', ['src/a.py', 'src/b.py']),
            ('space separated', '{outside=src/a.py src/b.py}', ['src/a.py', 'src/b.py']),
            ('backticked path', '{outside=`src/a.py`}', ['src/a.py']),
            ('backticked pair', '{outside=`src/a.py`, `src/b.py`}', ['src/a.py', 'src/b.py']),
            ('quoted', '{outside="src/a.py", \'src/b.py\'}', ['src/a.py', 'src/b.py']),
            ('duplicates keep the first', '{outside=src/a.py, src/b.py, src/a.py}', ['src/a.py', 'src/b.py']),
            ('order kept', '{outside=src/b.py, src/a.py}', ['src/b.py', 'src/a.py']),
            ('no brace', 'no brace here {tasks=a}', []),
            ('empty brace', '{outside=}', []),
            ('empty brace, spaces', '{outside= }', []),
            ('empty brace, comma', '{outside=,}', []),
            ('unclosed brace', '{outside=src/a.py', []),
        )
        for name, text, expected in cases:
            with self.subTest(name):
                self.assertEqual(contract.outside_paths(text), expected)

    def test_the_brace_before_the_facts_and_after_them(self):
        before = HEAD + ' {outside=src/a.py} {reach=main origin=new evidence=ran}'
        after = HEAD + ' {reach=main origin=new evidence=ran} {outside=src/b.py}'
        self.assertEqual(contract.outside_paths(before), ['src/a.py'])
        self.assertEqual(contract.outside_paths(after), ['src/b.py'])

    def test_with_a_tasks_tag_the_outside_brace_is_still_the_paths(self):
        # tasks ids are not paths, and the facts words are not either. The last outside brace wins.
        lines = (
            HEAD + ' {tasks=a,b} {outside=src/a.py} {reach=main origin=new evidence=ran}',
            HEAD + ' {outside=src/a.py, src/b.py} {tasks=a} {reach=main origin=new evidence=ran}',
            HEAD + ' {reach=main origin=new evidence=ran} {tasks=a,b} {outside=src/c.py}',
            HEAD + ' {outside=old.py} {reach=main origin=new evidence=ran} {outside=src/a.py, old.py}',
        )
        self.assertEqual(contract.outside_paths(lines[0]), ['src/a.py'])
        self.assertEqual(contract.outside_paths(lines[1]), ['src/a.py', 'src/b.py'])
        self.assertEqual(contract.outside_paths(lines[2]), ['src/c.py'])
        self.assertEqual(contract.outside_paths(lines[3]), ['src/a.py', 'old.py'])

    def test_a_brace_on_a_later_line_of_the_block_counts(self):
        text = '- [P1][CONFIRMED] a.py:1 — breaks\n  {outside=src/a.py, src/b.py}\n'
        self.assertEqual(contract.outside_paths(text), ['src/a.py', 'src/b.py'])


class FactsSurviveTests(unittest.TestCase):
    def test_outside_after_the_facts_is_cut_and_the_facts_remain(self):
        line = HEAD + ' {reach=main origin=new evidence=ran} {outside=src/a.py}'
        self.assertEqual(contract.split_facts(line), (HEAD, FACTS))
        self.assertEqual(contract.facts(line), FACTS)

    def test_outside_before_the_facts_stays_on_the_line(self):
        line = HEAD + ' {outside=src/a.py} {reach=main origin=new evidence=ran}'
        self.assertEqual(contract.split_facts(line), (HEAD + ' {outside=src/a.py}', FACTS))
        self.assertEqual(contract.facts(line), FACTS)

    def test_outside_and_tasks_in_either_order_after_the_facts(self):
        for tail in (' {outside=src/a.py} {tasks=a}', ' {tasks=a,b} {outside=src/a.py}'):
            line = HEAD + ' {reach=main origin=new evidence=ran}' + tail
            with self.subTest(tail=tail):
                self.assertEqual(contract.split_facts(line), (HEAD, FACTS))
                self.assertEqual(contract.facts(line), FACTS)

    def test_both_braces_before_the_facts_stay(self):
        kept = HEAD + ' {tasks=a} {outside=src/a.py}'
        line = kept + ' {reach=main origin=new evidence=ran}'
        self.assertEqual(contract.split_facts(line), (kept, FACTS))

    def test_a_wrapped_bullet_keeps_facts_when_outside_follows_them(self):
        text = ('- [P1][CONFIRMED] a.py:1 — breaks when the list\n  is empty\n'
                '  {reach=main origin=new evidence=ran} {outside=src/a.py}\n')
        self.assertEqual(contract.facts(text), FACTS)
        self.assertEqual(contract.outside_paths(text), ['src/a.py'])

    def test_an_outside_brace_alone_is_not_facts(self):
        line = HEAD + ' {outside=src/a.py}'
        self.assertEqual(contract.split_facts(line), (line, None))
        self.assertIsNone(contract.facts(line))

    def test_markup_around_an_outside_tail_is_cut_like_a_tasks_tail(self):
        line = HEAD + ' {reach=main origin=new evidence=ran} `{outside=src/a.py}`.'
        self.assertEqual(contract.split_facts(line), (HEAD, FACTS))


class BlockedLinesTests(unittest.TestCase):
    def test_plain_bulleted_and_quoted_lines(self):
        message = '\n'.join([
            'Work is done. The form is FORGE_BLOCKED: no.py: not its own line',
            'FORGE_BLOCKED: scripts/a.py: owned by the other task',
            '- FORGE_BLOCKED: lib/b.py: the helper',
            '> FORGE_BLOCKED: src/c.py: see the note',
            '`FORGE_BLOCKED: docs/d.md: backticked line`',
            '* FORGE_BLOCKED: ui/e.js: breaks: the call',
            '**FORGE_BLOCKED: f.py: bold**',
            '  FORGE_BLOCKED: g.py: indented as the prompt shows it',
        ])
        self.assertEqual(contract.blocked_lines(message), [
            ('scripts/a.py', 'owned by the other task'),
            ('lib/b.py', 'the helper'),
            ('src/c.py', 'see the note'),
            ('docs/d.md', 'backticked line'),
            ('ui/e.js', 'breaks: the call'),
            ('f.py', 'bold'),
            ('g.py', 'indented as the prompt shows it'),
        ])

    def test_an_empty_why_and_duplicates(self):
        message = '\n'.join([
            'FORGE_BLOCKED: scripts/a.py:',
            '- FORGE_BLOCKED: lib/b.py:   ',
            'FORGE_BLOCKED: scripts/a.py: later reason is dropped',
            '> FORGE_BLOCKED: `lib/b.py`: also dropped',
            'FORGE_BLOCKED: src/c.py: kept\r\n',
        ])
        self.assertEqual(contract.blocked_lines(message), [
            ('scripts/a.py', ''),
            ('lib/b.py', ''),
            ('src/c.py', 'kept'),
        ])

    def test_a_line_without_the_why_colon_is_not_the_form(self):
        self.assertEqual(contract.blocked_lines('FORGE_BLOCKED: scripts/a.py\n'), [])


class ParsingTests(unittest.TestCase):
    def test_a_forge_blocked_line_is_not_part_of_any_finding(self):
        text = '\n'.join([
            '- [P1][CONFIRMED] a.py:1 — breaks {outside=lib/x.py} {reach=main origin=new evidence=ran}',
            '  more detail',
            'FORGE_BLOCKED: lib/x.py: the helper is not this task',
            '- FORGE_BLOCKED: lib/y.py: also outside',
            '- [P1][CONFIRMED] b.py:2 — breaks FORGE_BLOCKED: lib/z.py: glued on the bullet',
            '- [P3][CONFIRMED] c.py:3 — nit',
            'FORGE_VERDICT: FAIL',
        ])
        found = contract.parse_findings(text)
        self.assertEqual(len(found), 3)
        self.assertFalse(any('FORGE_BLOCKED' in finding.text for finding in found))
        self.assertNotIn('helper', found[0].text)
        self.assertNotIn('glued', found[1].text)
        self.assertEqual(contract.outside_paths(found[0].text), ['lib/x.py'])
        self.assertEqual(contract.facts(found[0].text), FACTS)
        self.assertEqual([finding.status for finding in found], ['CONFIRMED', 'CONFIRMED', 'CONFIRMED'])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_outside_prints_one_object_per_finding(self):
        reply = '\n'.join([
            'Notes.',
            '- [P1][CONFIRMED] a.py:1 — breaks {tasks=a} {outside=src/a.py, src/b.py} {reach=main origin=new evidence=ran}',
            'FORGE_BLOCKED: src/a.py: not a finding',
            '- [P3][CONFIRMED] b.py:2 — nit {outside=docs/c.md}',
            '- [P1][PLAUSIBLE] c.py:3 — maybe {outside=lib/d.py}',
            '- [P0] e.py:5 — unstated {outside=e.py}',
            '- [CONFIRMED] f.py:6 — no label',
            'FORGE_VERDICT: FAIL',
        ])
        path = self.root / 'qa.last'
        path.write_text(reply, encoding='utf-8')
        result = run('outside', '--qa-last', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.endswith('\n'))
        rows = json.loads(result.stdout)
        expected = [
            {'n': 1, 'severity': 'P1', 'status': 'CONFIRMED', 'blocking': True,
             'outside': ['src/a.py', 'src/b.py']},
            {'n': 2, 'severity': 'P3', 'status': 'CONFIRMED', 'blocking': False,
             'outside': ['docs/c.md']},
            {'n': 3, 'severity': 'P1', 'status': 'PLAUSIBLE', 'blocking': False,
             'outside': ['lib/d.py']},
            {'n': 4, 'severity': 'P0', 'status': 'UNSTATED', 'blocking': True,
             'outside': ['e.py']},
            {'n': 5, 'severity': None, 'status': 'CONFIRMED', 'blocking': True,
             'outside': []},
        ]
        self.assertEqual(rows, expected)
        self.assertEqual([list(row) for row in rows],
                         [['n', 'severity', 'status', 'blocking', 'outside']] * len(rows))
        # Default is P2. P0 blocks only P0; none is P3, so the tolerated P3 starts blocking.
        self.assertEqual(run('outside', '--qa-last', path, '--threshold', 'P2').stdout, result.stdout)
        at_p0 = json.loads(run('outside', '--qa-last', path, '--threshold', 'P0').stdout)
        self.assertEqual([row['blocking'] for row in at_p0], [False, False, False, True, True])
        at_none = json.loads(run('outside', '--qa-last', path, '--threshold', 'none').stdout)
        self.assertEqual([row['blocking'] for row in at_none], [True, True, False, True, True])

    def test_blocked_prints_path_and_why(self):
        message = '\n'.join([
            'FORGE_BLOCKED: scripts/a.py: owned by other',
            '- FORGE_BLOCKED: lib/b.py:',
            'FORGE_BLOCKED: scripts/a.py: duplicate',
        ])
        path = self.root / 'message.txt'
        path.write_text(message, encoding='utf-8')
        result = run('blocked', '--message', path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            {'path': 'scripts/a.py', 'why': 'owned by other'},
            {'path': 'lib/b.py', 'why': ''},
        ])
        self.assertEqual(list(json.loads(result.stdout)[0]), ['path', 'why'])

    def test_an_empty_reply_is_an_empty_list(self):
        path = self.root / 'empty.txt'
        path.write_text('', encoding='utf-8')
        for args in (['outside', '--qa-last', path], ['blocked', '--message', path]):
            result = run(*args)
            self.assertEqual((result.returncode, result.stdout), (0, '[]\n'), args)

    def test_an_unreadable_file_exits_3(self):
        missing = self.root / 'nope'
        for args in (['outside', '--qa-last', missing], ['blocked', '--message', missing],
                     ['outside', '--qa-last', self.root], ['blocked', '--message', self.root]):
            result = run(*args)
            self.assertEqual(result.returncode, 3, args)
            self.assertEqual(result.stdout, '')
            self.assertIn('cannot read', result.stderr)

    def test_missing_arguments_exit_2(self):
        for args in (['outside'], ['blocked'], ['outside', '--threshold', 'P2'], ['blocked', '--qa-last', 'x']):
            result = run(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
