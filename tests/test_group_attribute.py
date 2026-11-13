"""Group review attribution: one QA reply's findings, back to the members that must change.

Nothing here needs git or a model. The contract's parser and gate decide what a finding is
and whether it blocks; these tests cover how that result is handed to a member.
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_groups.attribute as attr  # noqa: E402

_spec = importlib.util.spec_from_file_location('forge_contract', ROOT / 'scripts' / 'forge-contract.py')
contract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contract)

SCRIPT = ROOT / 'scripts' / 'forge_groups' / 'attribute.py'

# Three members, disjoint files. `b`'s path is written with a leading ./ on purpose.
MEMBERS = [
    ('a', ['src/a.py', 'src/a_util.py']),
    ('b', ['./src/b.py']),
    ('c', ['docs/c.md']),
]


def reply(*lines):
    return '\n'.join(lines) + '\n\nFORGE_VERDICT: FAIL\n'


class AttributeTests(unittest.TestCase):
    def test_file_attribution_across_three_members(self):
        # Listed c, b, a: implicated comes back in member order, and the first path:line wins
        # over a later file named in the same bullet. A leading ./ matches either side.
        text = reply(
            '- [P2][CONFIRMED] docs/c.md — missing step',
            '* (P1) CONFIRMED src/b.py:2 — also mentions src/a.py:9',
            '- [P1][CONFIRMED] src/a.py:1 — first file of a',
            '- [P1][CONFIRMED] ./src/a_util.py:12 — second file of a',
        )
        result = attr.attribute(text, MEMBERS, 'P2')
        self.assertEqual([row['tasks'] for row in result['findings']], [['c'], ['b'], ['a'], ['a']])
        self.assertEqual([(row['file'], row['line']) for row in result['findings']], [
            ('docs/c.md', None), ('src/b.py', 2), ('src/a.py', 1), ('src/a_util.py', 12)])
        self.assertEqual(result['implicated'], ['a', 'b', 'c'])
        self.assertEqual(result['ambiguous'], [])
        self.assertTrue(result['blocking'])
        self.assertTrue(all(row['blocking'] and not row['ambiguous'] for row in result['findings']))
        self.assertEqual([row['n'] for row in result['findings']], [1, 2, 3, 4])

    def test_tasks_tag_overrides_the_file_and_implicates_each_named_member(self):
        text = reply(
            '- [P1][CONFIRMED] src/b.py:4 — the split is wrong {tasks= c, a }',
            '- [P0][CONFIRMED] src/a.py:1 — both sides {tasks=b a}',
            '- [P1][CONFIRMED] src/b.py:8 — repeats a member {tasks=b, ghost, b, a}',
        )
        result = attr.attribute(text, MEMBERS, 'P2')
        first, second, third = result['findings']
        self.assertEqual(first['file'], 'src/b.py')
        self.assertEqual(first['tasks'], ['a', 'c'])
        self.assertFalse(first['ambiguous'])
        self.assertEqual(second['tasks'], ['a', 'b'])
        # An unknown id makes the whole finding ambiguous, so its real members are not retried.
        self.assertEqual(third['tasks'], ['a', 'b', 'ghost'])
        self.assertTrue(third['ambiguous'])
        self.assertEqual(result['implicated'], ['a', 'b', 'c'])
        self.assertEqual(result['ambiguous'], [3])
        self.assertNotIn('ghost', result['implicated'])

    def test_unknown_id_missing_file_and_unowned_file_are_ambiguous_only_when_blocking(self):
        text = reply(
            '- [P1][CONFIRMED] src/a.py:1 — names a stranger {tasks=a,ghost}',
            '- [P0][CONFIRMED] — no file at all, e.g. it crashes',
            '- [P1][CONFIRMED] src/nobody.py:3 — no member changed it',
            '- [P3][CONFIRMED] src/a.py:8 — tolerated, and the id is unknown {tasks=ghost}',
            '- [P3][CONFIRMED] — tolerated and no file',
            '- [P1][PLAUSIBLE] src/missing.py:1 — plausible, and no member changed it',
            '- [P1][CONFIRMED] src/b.py:2 — this one is clear',
        )
        result = attr.attribute(text, MEMBERS, 'P2')
        rows = result['findings']
        self.assertEqual([row['n'] for row in rows], list(range(1, 8)))
        self.assertEqual(rows[0]['tasks'], ['a', 'ghost'])
        self.assertTrue(rows[0]['ambiguous'])
        self.assertIsNone(rows[1]['file'])
        self.assertEqual(rows[1]['tasks'], [])
        self.assertTrue(rows[1]['ambiguous'])
        self.assertEqual(rows[2]['file'], 'src/nobody.py')
        self.assertEqual(rows[2]['line'], 3)
        self.assertTrue(rows[2]['ambiguous'])
        for row in rows[3:6]:
            self.assertTrue(row['ambiguous'], row)
            self.assertFalse(row['blocking'], row)
        self.assertEqual(rows[6]['tasks'], ['b'])
        self.assertFalse(rows[6]['ambiguous'])
        # The tolerated and plausible ones are listed, and only the blocking ambiguous n's surface.
        self.assertEqual(result['ambiguous'], [1, 2, 3])
        self.assertEqual(result['implicated'], ['b'])
        self.assertTrue(result['blocking'])
        self.assertNotIn('a', result['implicated'])

    def test_plausible_and_tolerated_findings_are_listed_and_implicate_nobody(self):
        text = reply(
            '- [P0][PLAUSIBLE] src/a.py:1 — maybe {tasks=a,b}',
            '- [P3][CONFIRMED] src/b.py:2 — nit',
            '- [P3][CONFIRMED] docs/c.md:1 — nit {tasks=c, a}',
        )
        result = attr.attribute(text, MEMBERS, 'P2')
        self.assertEqual([row['status'] for row in result['findings']],
                         ['PLAUSIBLE', 'CONFIRMED', 'CONFIRMED'])
        self.assertEqual([row['severity'] for row in result['findings']], ['P0', 'P3', 'P3'])
        self.assertEqual([row['tasks'] for row in result['findings']], [['a', 'b'], ['b'], ['a', 'c']])
        self.assertFalse(any(row['blocking'] or row['ambiguous'] for row in result['findings']))
        self.assertEqual(result['implicated'], [])
        self.assertEqual(result['ambiguous'], [])
        self.assertFalse(result['blocking'])
        # `none` is the gate's P3: the tolerated findings start blocking, the plausible one does not.
        strict = attr.attribute(text, MEMBERS, 'none')
        self.assertFalse(strict['findings'][0]['blocking'])
        self.assertTrue(strict['findings'][1]['blocking'])
        self.assertTrue(strict['findings'][2]['blocking'])
        self.assertEqual(strict['implicated'], ['a', 'b', 'c'])
        self.assertEqual(attr.attribute(text, MEMBERS, None)['implicated'], [])
        self.assertEqual(attr.attribute(text, MEMBERS, '')['implicated'], [])

    def test_the_facts_tag_beside_the_tasks_tag_is_not_read_as_tasks(self):
        text = reply(
            '- [P1][CONFIRMED] src/b.py:4 — crosses {reach=realistic origin=new evidence=traced} {tasks=c, a}',
            '- [P1][CONFIRMED] src/b.py:5 — crosses {tasks=a} {reach=main origin=existing evidence=ran}',
            '- [P1][CONFIRMED] src/a.py:1 — only facts {reach=unusual origin=existing evidence=suspected}',
            '- [P1][CONFIRMED] src/b.py:6 — wrapped',
            '  {tasks=b, c} {reach=realistic origin=new evidence=traced}',
        )
        result = attr.attribute(text, MEMBERS, 'P2')
        self.assertEqual([row['tasks'] for row in result['findings']],
                         [['a', 'c'], ['a'], ['a'], ['b', 'c']])
        self.assertEqual([row['file'] for row in result['findings']],
                         ['src/b.py', 'src/b.py', 'src/a.py', 'src/b.py'])
        self.assertFalse(any(row['ambiguous'] for row in result['findings']))
        self.assertEqual(result['implicated'], ['a', 'b', 'c'])
        named = {task for row in result['findings'] for task in row['tasks']}
        self.assertTrue(named.isdisjoint({'realistic', 'main', 'unusual', 'new', 'existing', 'traced', 'ran', 'suspected'}))
        # Either order keeps the shadow measurement. The second bullet puts tasks first; the first
        # and the wrapped one put the facts brace first, which is the order the tail now forbids.
        parsed = contract.parse_findings(text)
        self.assertEqual([contract.facts(finding.text) for finding in parsed], [
            dict(reach='realistic', origin='new', evidence='traced'),
            dict(reach='main', origin='existing', evidence='ran'),
            dict(reach='unusual', origin='existing', evidence='suspected'),
            dict(reach='realistic', origin='new', evidence='traced'),
        ])

    def test_an_empty_tasks_tag_names_nobody_and_is_ambiguous_when_it_blocks(self):
        # A present tag with no id does not fall back to the file it names. Only the blocking
        # ones are escalated; a tolerated empty tag stays in the list and implicates nobody.
        text = reply(
            '- [P1][CONFIRMED] src/a.py:3 — x {tasks=}',
            '- [P1][CONFIRMED] src/b.py:1 — separators only {tasks= , }',
            '- [P3][CONFIRMED] src/a.py:4 — tolerated empty {tasks=}',
        )
        result = attr.attribute(text, MEMBERS, 'P2')
        self.assertEqual([row['tasks'] for row in result['findings']], [[], [], []])
        self.assertEqual([row['ambiguous'] for row in result['findings']], [True, True, True])
        self.assertEqual([row['file'] for row in result['findings']], ['src/a.py', 'src/b.py', 'src/a.py'])
        self.assertEqual(result['implicated'], [])
        self.assertEqual(result['ambiguous'], [1, 2])
        self.assertTrue(result['blocking'])
        self.assertFalse(result['findings'][2]['blocking'])

    def test_a_dotted_word_before_the_path_line_is_not_the_file(self):
        # The block's first path:line wins over a dotted word earlier in the prose. Those tokens
        # match a bare path, and taking the earliest match would mark a real file unattributable.
        text = reply(
            '- [P1][CONFIRMED] Calls os.path.join badly in src/a.py:3',
            '- [P1][CONFIRMED] json.loads of node.js fails at src/b.py:2',
            '- [P1][CONFIRMED] see docs/c.md before src/a.py:9 — the path:line still wins',
        )
        result = attr.attribute(text, MEMBERS, 'P2')
        self.assertEqual([(row['file'], row['line']) for row in result['findings']],
                         [('src/a.py', 3), ('src/b.py', 2), ('src/a.py', 9)])
        self.assertEqual([row['tasks'] for row in result['findings']], [['a'], ['b'], ['a']])
        self.assertEqual([row['ambiguous'] for row in result['findings']], [False, False, False])
        self.assertEqual(result['implicated'], ['a', 'b'])
        self.assertEqual(result['ambiguous'], [])

    def test_a_word_and_a_number_is_not_a_file(self):
        # A host, a time or a key is not a path:line. The file the bullet names stays the
        # location, with or without its own line, and that member is retried. Reading every
        # word:number as a file would mark these blocking findings ambiguous and retry nobody.
        members = [('a', ['src/a.py']), ('b', ['docs/c.md'])]
        text = reply(
            '- [P1][CONFIRMED] docs/c.md — documents localhost:8080 but the server binds :9000',
            '- [P1][CONFIRMED] docs/c.md — the timeout 12:30 is wrong',
            '- [P1][CONFIRMED] src/a.py — breaks for key a:1 in the dict',
            '- [P1][CONFIRMED] key a:1 breaks src/a.py:4, and 12:30 does not',
            '- [P1][CONFIRMED] localhost:8080 is documented in docs/c.md:2',
        )
        result = attr.attribute(text, members, 'P2')
        self.assertEqual([(row['file'], row['line']) for row in result['findings']], [
            ('docs/c.md', None), ('docs/c.md', None), ('src/a.py', None),
            ('src/a.py', 4), ('docs/c.md', 2)])
        self.assertEqual([row['tasks'] for row in result['findings']],
                         [['b'], ['b'], ['a'], ['a'], ['b']])
        self.assertEqual(result['ambiguous'], [])
        self.assertEqual(result['implicated'], ['a', 'b'])
        self.assertTrue(all(row['blocking'] and not row['ambiguous'] for row in result['findings']))

    def test_blocking_is_the_gate_decision_and_the_parser_drops_what_it_always_drops(self):
        text = reply(
            '- [P1][CONFIRMED] src/a.py:1 — blocks',
            '- [P3][CONFIRMED] src/b.py:2 — tolerated',
            '- [P0][PLAUSIBLE] docs/c.md:3 — plausible',
            '- [P2] src/a.py:4 — unstated',
            '- [CONFIRMED] src/b.py:5 — no label',
            '- [P1][PLAUSIBLE] src/a.py:6 — NOT CONFIRMED, could not run it',
            '- [P0][CONFIRMED] path/file.py:123 — the contract placeholder',
        )
        result = attr.attribute(text, MEMBERS, 'P2')
        parsed = contract.parse_findings(text)
        self.assertEqual(len(result['findings']), len(parsed))
        self.assertEqual([row['status'] for row in result['findings']],
                         [finding.status for finding in parsed])
        for row, finding in zip(result['findings'], parsed):
            self.assertEqual(row['blocking'], contract.finding_blocks(finding, 'P2'))
        # NOT CONFIRMED and the placeholder are not findings, same as the gate.
        # NOT CONFIRMED stays PLAUSIBLE; the contract's placeholder path is not a finding.
        self.assertEqual([row['severity'] for row in result['findings']], ['P1', 'P3', 'P0', 'P2', None, 'P1'])
        self.assertEqual(result['findings'][5]['status'], 'PLAUSIBLE')
        self.assertFalse(result['findings'][5]['blocking'])
        self.assertEqual(result['implicated'], ['a', 'b'])
        self.assertEqual(attr.attribute('nothing to report\n', MEMBERS, 'P2'),
                         {'findings': [], 'implicated': [], 'ambiguous': [], 'blocking': False})

    def test_two_members_that_changed_one_file_are_both_named(self):
        text = '- [P1][CONFIRMED] src/a.py:1 — shared\n'
        result = attr.attribute(text, [('a', ['src/a.py']), ('b', ['src/a.py']), ('c', ['docs/c.md'])], 'P2')
        self.assertEqual(result['findings'][0]['tasks'], ['a', 'b'])
        self.assertFalse(result['findings'][0]['ambiguous'])
        self.assertEqual(result['implicated'], ['a', 'b'])


class CommandLineTests(unittest.TestCase):
    def test_cli_prints_the_json_and_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            qa = root / 'qa.last'
            members = root / 'members.tsv'
            qa.write_text('- [P1][CONFIRMED] src/a.py:4 — breaks\n'
                          '- [P3][CONFIRMED] ./src/b.py:1 — nit\n', encoding='utf-8')
            # A commented row would own src/a.py if `#` lines were parsed.
            members.write_text('# a\tsrc/a.py\n\nb\t./src/b.py\na\tsrc/a.py\n', encoding='utf-8')
            result = subprocess.run(
                [sys.executable, str(SCRIPT), '--qa-last', str(qa), '--members', str(members),
                 '--threshold', 'P2'],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            got = json.loads(result.stdout)
            expect = attr.attribute(qa.read_text(encoding='utf-8'),
                                    [('b', ['./src/b.py']), ('a', ['src/a.py'])], 'P2')
            self.assertEqual(got, expect)
            self.assertEqual(got['implicated'], ['a'])
            self.assertEqual(got['findings'][1]['tasks'], ['b'])
            self.assertFalse(got['findings'][1]['blocking'])
            self.assertEqual(list(got), ['findings', 'implicated', 'ambiguous', 'blocking'])
            self.assertEqual(list(got['findings'][0]),
                             ['n', 'severity', 'status', 'file', 'line', 'tasks', 'blocking', 'ambiguous'])

            omitted = subprocess.run(
                [sys.executable, str(SCRIPT), '--qa-last', str(qa), '--members', str(members)],
                capture_output=True, text=True)
            self.assertEqual(omitted.returncode, 0, omitted.stderr)
            self.assertEqual(json.loads(omitted.stdout),
                             attr.attribute(qa.read_text(encoding='utf-8'),
                                            [('b', ['./src/b.py']), ('a', ['src/a.py'])], None))

            (root / 'bad.tsv').write_text('a src/a.py\n', encoding='utf-8')
            cases = (
                ['--qa-last', str(qa), '--members', str(members), '--threshold', 'P9'],
                ['--qa-last', str(qa), '--members', str(root / 'bad.tsv')],
                ['--qa-last', str(root / 'missing'), '--members', str(members)],
                [],
            )
            for args in cases:
                with self.subTest(args=args):
                    bad = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)
                    self.assertEqual(bad.returncode, 2, bad.stderr)
                    self.assertEqual(bad.stdout, '')
                    self.assertTrue(bad.stderr)
