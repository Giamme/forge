import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-contract.py'


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


contract = module('forge-contract')

# The wording the two prompt builders used before the contract existed. Kept here
# verbatim so a reworded sentence is a visible diff, not a silent behaviour change.
LEGACY_HEAD = (
    'The implementer was asked to do the task above. Review the diff below for correctness\n'
    'bugs: logic errors, broken edge cases, behaviour that does not match what was asked.\n'
    "Also say if it solved a different problem, or touched files outside the task's scope.\n")
LEGACY_FINDINGS = (
    'Report findings only — do not edit any file. For each finding give the file and line,\n'
    'what breaks, and a concrete input that triggers it. Mark each CONFIRMED if you traced\n'
    'it in the code, or PLAUSIBLE if you could not fully verify it.\n')
LEGACY_VERDICT = (
    'End your reply with exactly one line:\n'
    '  FORGE_VERDICT: PASS   — no confirmed correctness bug (style nits are not failures)\n'
    '  FORGE_VERDICT: FAIL   — at least one CONFIRMED correctness bug\n')


def bullet(severity, status='CONFIRMED', where='a.py:1', what='breaks', tags=None):
    tags = tags if tags is not None else ('[%s]' % severity if severity else '') + '[%s]' % status
    return '- %s %s — %s (input x)' % (tags, where, what)


def decide(verdict, text, threshold=None):
    result = contract.gate(verdict, text, threshold)
    return result.decision, result.worst, result.n_confirmed, result.n_unlabelled, result.n_below


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT)] + [str(a) for a in args],
                          capture_output=True, text=True, encoding='utf-8')


class GateTableTests(unittest.TestCase):
    def reply(self, *severities, verdict='FAIL'):
        lines = [bullet(s, where='f%d.py:%d' % (i, i + 1)) for i, s in enumerate(severities)]
        return 'Findings:\n\n' + '\n'.join(lines) + '\n\nFORGE_VERDICT: ' + verdict + '\n'

    def test_boundaries(self):
        cases = [
            # threshold, severities of the CONFIRMED findings, decision, worst, n_below
            ('P2', ['P3'], 'accept', 'P3', 1),
            ('P2', ['P2'], 'keep', 'P2', 0),
            ('P2', ['P1', 'P3'], 'keep', 'P1', 1),
            ('P2', ['P3', 'P3'], 'accept', 'P3', 2),
            ('P1', ['P2'], 'accept', 'P2', 1),
            ('P0', ['P1'], 'accept', 'P1', 1),
            ('P0', ['P0'], 'keep', 'P0', 0),
            ('P3', ['P3'], 'keep', 'P3', 0),
            ('P1', ['P0', 'P2', 'P3'], 'keep', 'P0', 2),
        ]
        for threshold, severities, decision, worst, below in cases:
            with self.subTest(threshold=threshold, severities=severities):
                got = decide('FAIL', self.reply(*severities), threshold)
                self.assertEqual(got, (decision, worst, len(severities), 0, below))

    def test_no_threshold_never_accepts(self):
        for severities in (['P3'], ['P3', 'P3'], ['P0']):
            with self.subTest(severities=severities):
                got = decide('FAIL', self.reply(*severities))
                self.assertEqual((got[0], got[3], got[4]), ('keep', 0, 0))
        self.assertEqual(decide('FAIL', self.reply('P3'), ''), decide('FAIL', self.reply('P3')))

    def test_unlabelled_confirmed_always_blocks(self):
        self.assertEqual(decide('FAIL', bullet(None) + '\n', 'P0'), ('keep', '-', 1, 1, 0))
        mixed = bullet('P3') + '\n' + bullet(None, where='b.py:2') + '\n'
        self.assertEqual(decide('FAIL', mixed, 'P2'), ('keep', 'P3', 2, 1, 1))
        # A severity-looking token glued to a path or range is not a label.
        for first in ('- [CONFIRMED] src/P1/x.py:3 — breaks', '- [CONFIRMED] P1.py:3 — breaks',
                      '- [CONFIRMED] a.py:3 P1-P2 breaks', '- [CONFIRMED] a.py:3 — HP2 breaks'):
            with self.subTest(first=first):
                self.assertEqual(decide('FAIL', first + '\n', 'P0')[:2], ('keep', '-'))

    def test_severity_without_status_blocks(self):
        # Neither CONFIRMED nor PLAUSIBLE: ambiguous, so it can never be tolerated.
        text = bullet('P3') + '\n- [P3] b.py:2 — also breaks\n'
        self.assertEqual(decide('FAIL', text, 'P2'), ('keep', 'P3', 1, 1, 1))
        # Severity definitions have no file:line, so an echoed one is not a finding.
        self.assertEqual(decide('FAIL', bullet('P3') + '\n- P0: data loss\n', 'P2')[0], 'accept')

    def test_a_fail_with_no_confirmed_finding_stays_fail(self):
        plausible = '- [P3][PLAUSIBLE] a.py:1 — maybe breaks (input x)\n'
        self.assertEqual(decide('FAIL', plausible, 'P2'), ('keep', '-', 0, 0, 0))
        self.assertEqual(decide('FAIL', 'The change looks wrong.\n', 'P2'), ('keep', '-', 0, 0, 0))
        self.assertEqual(decide('FAIL', '', 'P2'), ('keep', '-', 0, 0, 0))

    def test_status_words(self):
        both = '- [P3][CONFIRMED][PLAUSIBLE] a.py:1 — traced most of it\n'
        self.assertEqual(decide('FAIL', both, 'P2'), ('accept', 'P3', 1, 0, 1))
        for prose in ('- [P1][PLAUSIBLE] a.py:1 — I have not confirmed this\n',
                      '- [P1][PLAUSIBLE] a.py:1 — NOT CONFIRMED, could not run it\n'):
            with self.subTest(prose=prose):
                self.assertEqual(decide('FAIL', prose, 'P2')[2], 0)
        self.assertEqual(decide('FAIL', '- [P3][Confirmed] a.py:1 — x\n', 'P2')[:3], ('accept', 'P3', 1))

    def test_verdict_other_than_fail_keeps(self):
        text = self.reply('P3')
        for verdict in ('PASS', 'UNKNOWN'):
            self.assertEqual(decide(verdict, text, 'P2'), ('keep', 'P3', 1, 0, 1))

    def test_label_forms_and_bullet_styles(self):
        forms = [
            '- [P3][CONFIRMED] a.py:1 — x', '* (P3) CONFIRMED a.py:1 — x',
            '1. **P3** CONFIRMED a.py:1 — x', '2) P3: CONFIRMED a.py:1 — x',
            '+ P3 - CONFIRMED a.py:1 — x', '- CONFIRMED P3 a.py:1 — x',
            '### [P3][CONFIRMED] a.py:1 — x', '**[P3][CONFIRMED]** a.py:1 — x',
            '| P3 | CONFIRMED | a.py:1 | x |', '> - [P3][CONFIRMED] a.py:1 — x',
        ]
        for form in forms:
            with self.subTest(form=form):
                self.assertEqual(decide('FAIL', 'Review:\n\n' + form + '\n', 'P2'), ('accept', 'P3', 1, 0, 1))
        self.assertEqual(decide('FAIL', '- [p3][CONFIRMED] a.py:1 — x\n', 'P2')[:2], ('keep', '-'))

    def test_multi_line_findings(self):
        text = ('- [P3][CONFIRMED] a.py:1 — first\n'
                '  continues on an indented line\n'
                '  - nested trigger: input 0\n'
                '\n'
                '  and a second paragraph of the same finding\n'
                '- [P1][PLAUSIBLE] b.py:2 — second\n'
                '  Status: CONFIRMED, stated on a continuation line\n')
        got = contract.parse_findings(text)
        self.assertEqual([(f.status, f.severity) for f in got], [('CONFIRMED', 3), ('CONFIRMED', 1)])
        self.assertIn('and a second paragraph', got[0].text)
        self.assertNotIn('b.py', got[0].text)
        # The status word may sit on a continuation line of its own bullet.
        later = '- [P3] a.py:1 — breaks\n  Status: CONFIRMED (traced)\n'
        self.assertEqual(decide('FAIL', later, 'P2'), ('accept', 'P3', 1, 0, 1))
        # A flush-left paragraph after a blank line is not part of the finding.
        summary = bullet('P3', status='PLAUSIBLE') + '\n\nSummary: CONFIRMED nothing.\n'
        self.assertEqual(decide('FAIL', summary, 'P2')[2], 0)
        nested = '- Findings:\n  - [P3][CONFIRMED] a.py:1 — x\n  - [P1][CONFIRMED] b.py:2 — y\n'
        self.assertEqual(decide('FAIL', nested, 'P2')[:4], ('keep', '-', 1, 1))

    def test_crlf_input(self):
        text = (bullet('P3') + '\r\n   continues\r\n\r\n' + bullet('P3', where='b.py:2') +
                '\r\n\r\nFORGE_VERDICT: FAIL\r\n')
        self.assertEqual(decide('FAIL', text, 'P2'), ('accept', 'P3', 2, 0, 2))
        self.assertNotIn('\r', contract.parse_findings(text)[0].text)
        self.assertEqual(decide('FAIL', text.replace('\r\n', '\r'), 'P2'), ('accept', 'P3', 2, 0, 2))

    def test_protocol_lines_are_not_findings(self):
        verdict = 'FORGE_VERDICT: FAIL — at least one CONFIRMED correctness bug\n'
        # Directly under a PLAUSIBLE bullet the verdict gloss must not turn it CONFIRMED.
        text = '- [P3][PLAUSIBLE] a.py:1 — maybe\n' + verdict
        self.assertEqual(decide('FAIL', text, 'P2'), ('keep', '-', 0, 0, 0))
        # Neither does a reviewer's own gloss, nor a learning note wedged under a bullet.
        gloss = 'FORGE_VERDICT: FAIL \u2014 one CONFIRMED P1 bug found\n'
        self.assertEqual(decide('FAIL', '- [P3][PLAUSIBLE] a.py:1 \u2014 maybe\n' + gloss, 'P2'), ('keep', '-', 0, 0, 0))
        note = 'FORGE_LEARNING: finding | CONFIRMED bugs hide in a.py [a.py]\n'
        for text in ('- [P3][PLAUSIBLE] a.py:1 \u2014 maybe\n' + note + gloss,
                     '- [P3][PLAUSIBLE] a.py:1 \u2014 maybe ' + note + gloss):
            self.assertEqual(decide('FAIL', text, 'P2'), ('keep', '-', 0, 0, 0))
        learning = 'FORGE_LEARNING: finding | CONFIRMED P0 data loss in a.py:9 [a.py]\n'
        text = bullet('P3') + '\n' + learning + 'FORGE_VERDICT: FAIL\n'
        self.assertEqual(decide('FAIL', text, 'P2'), ('accept', 'P3', 1, 0, 1))
        text = learning + bullet('P3') + '\n\n  FORGE_LEARNING: trap | P0 CONFIRMED\nFORGE_VERDICT: FAIL\n'
        self.assertEqual(decide('FAIL', text, 'P2'), ('accept', 'P3', 1, 0, 1))

    def test_echoed_contract_changes_nothing(self):
        echo = ''
        for kind in contract.KINDS:
            for threshold in (None, 'P0', 'P1', 'P2', 'P3'):
                echo += contract.qa_text('head', kind, threshold) + '\n'
                echo += contract.qa_text('tail', kind, threshold) + '\n'
        echo += contract.dwarf_text()
        self.assertEqual(contract.parse_findings(echo), [])
        # Re-wrapped and bulleted, as a reviewer restating its instructions might.
        rewrapped = ('- Mark each CONFIRMED if you traced it in the code, or PLAUSIBLE if you\n'
                     '  could not fully verify it.\n'
                     '- [P1][CONFIRMED] path/file.py:123 — what breaks (trigger input)\n'
                     '- [P0][CONFIRMED] path/file.py:123 — my own placeholder\n'
                     '- P0: data loss, security hole, crash or wrong result on the main path\n')
        self.assertEqual(contract.parse_findings(rewrapped), [])
        cases = [(self.reply('P3'), 'P2'), (self.reply('P1', 'P3'), 'P2'),
                 ('- [P3][PLAUSIBLE] a.py:1 — maybe\n', 'P2'), ('', 'P2'), (self.reply('P3'), None)]
        for text, threshold in cases:
            with self.subTest(text=text, threshold=threshold):
                self.assertEqual(decide('FAIL', echo + '\n' + text, threshold), decide('FAIL', text, threshold))
                self.assertEqual(decide('FAIL', text + '\n' + echo, threshold), decide('FAIL', text, threshold))
                self.assertEqual(decide('FAIL', rewrapped + text, threshold), decide('FAIL', text, threshold))

    def test_tolerated_findings_are_in_the_result(self):
        result = contract.gate('FAIL', self.reply('P3', 'P1'), 'P2')
        self.assertEqual([f.severity for f in result.tolerated], [3])


class KnownIssuesFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.qa = self.root / 'qa.last'; self.out = self.root / 'known_issues.md'
        self.qa.write_text('Review.\n\n- [P3][CONFIRMED] a.py:1 — nit\n  more detail ß\n\n'
                           '- [P1][PLAUSIBLE] c.py:3 — maybe\n- [P2][CONFIRMED] b.py:2 — edge\n\n'
                           'FORGE_VERDICT: FAIL\n', encoding='utf-8')

    def gate(self, verdict, threshold, known=True):
        args = ['gate', '--verdict', verdict, '--qa-last', self.qa]
        if threshold: args += ['--threshold', threshold]
        if known: args += ['--known-issues', self.out]
        return run(*args)

    def test_written_verbatim_under_threshold_heading(self):
        result = self.gate('FAIL', 'P1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'accept\tP2\t2\t0\t2\n')
        text = self.out.read_text(encoding='utf-8')
        self.assertTrue(text.startswith('## Known issues (QA findings below blocking threshold P1)\n'))
        self.assertIn('- [P3][CONFIRMED] a.py:1 — nit\n  more detail ß\n', text)
        self.assertIn('- [P2][CONFIRMED] b.py:2 — edge\n', text)
        self.assertNotIn('c.py', text)
        self.assertTrue(text.endswith('\n') and not text.endswith('\n\n'))
        self.assertFalse(any(line != line.rstrip() for line in text.splitlines()))

    def test_only_the_tolerated_ones_are_listed(self):
        self.assertEqual(self.gate('FAIL', 'P2').stdout, 'keep\tP2\t2\t0\t1\n')
        text = self.out.read_text(encoding='utf-8')
        self.assertIn('a.py:1', text); self.assertNotIn('b.py:2', text)

    def test_written_whatever_the_verdict(self):
        for verdict in ('PASS', 'UNKNOWN'):
            with self.subTest(verdict=verdict):
                if self.out.exists():
                    self.out.unlink()
                self.assertEqual(self.gate(verdict, 'P1').stdout, 'keep\tP2\t2\t0\t2\n')
                self.assertTrue(self.out.exists())

    def test_not_created_without_tolerated_findings(self):
        self.assertEqual(self.gate('FAIL', 'P3').stdout, 'keep\tP2\t2\t0\t0\n')
        self.gate('FAIL', None)
        self.assertFalse(self.out.exists())
        self.qa.write_text('- [P1][CONFIRMED] a.py:1 — bad\n', encoding='utf-8')
        self.assertEqual(self.gate('FAIL', 'P2').stdout, 'keep\tP1\t1\t0\t0\n')
        self.assertFalse(self.out.exists())

    def test_unwritable_destination_is_exit_3(self):
        self.out = self.root / 'missing-dir' / 'known_issues.md'
        result = self.gate('FAIL', 'P1')
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, '')


class PromptTextTests(unittest.TestCase):
    def test_legacy_wording_is_kept(self):
        self.assertIn(LEGACY_HEAD, contract.qa_text('head', 'task'))
        for threshold in (None, 'P2'):
            tail = contract.qa_text('tail', 'task', threshold)
            self.assertTrue(tail.startswith(LEGACY_FINDINGS))
        self.assertTrue(contract.qa_text('tail', 'task').endswith(LEGACY_VERDICT))

    def test_legacy_wording_matches_the_parallel_script(self):
        # While forge-parallel.sh still carries its own copy, the two must not drift.
        source = (ROOT / 'scripts' / 'forge-parallel.sh').read_text(encoding='utf-8')
        if 'The implementer was asked to do the task above' not in source:
            self.skipTest('forge-parallel.sh builds the contract through forge-contract.py')
        for line in (LEGACY_HEAD + LEGACY_FINDINGS + LEGACY_VERDICT).splitlines():
            self.assertIn('echo "%s"' % line, source, line)

    def test_new_head_sentence_and_final_kind(self):
        task = contract.qa_text('head', 'task')
        self.assertIn("Do not trust the implementer's own reports", task)
        final = contract.qa_text('head', 'final')
        self.assertNotEqual(final, task)
        self.assertNotIn(LEGACY_HEAD, final)
        for phrase in ('COMBINED', 'cross', 'interactions between tasks', 'interface mismatches',
                       'ordering races', 'regressions of existing contracts', 'per-task review'):
            self.assertIn(phrase.lower(), final.lower(), phrase)
        self.assertEqual(contract.qa_text('head', 'final', 'P2'), final)
        self.assertEqual(contract.qa_text('tail', 'final'), contract.qa_text('tail', 'task'))

    def test_solo_kind_keeps_the_solo_wording(self):
        head = contract.qa_text('head', 'solo')
        self.assertIn("one stated, or changed files outside the goal's scope.", head)
        self.assertIn('If the diff is correct, say so plainly', contract.qa_text('tail', 'solo'))
        self.assertNotIn('say so plainly', contract.qa_text('tail', 'task'))

    def test_severity_instruction(self):
        tail = contract.qa_text('tail')
        self.assertIn('- [P1][CONFIRMED] path/file.py:123 — what breaks (trigger input)', tail)
        for label in ('P0  data loss, security hole, crash or wrong result on the main path',
                      'P1  wrong behaviour on realistic input, or breaks an existing contract',
                      'P2  edge-case bug', 'P3  minor: cosmetic, naming'):
            self.assertIn(label, tail)
        self.assertIn('One finding per bullet', tail)
        self.assertIn('impact, not confidence', tail)

    def test_threshold_clause_only_with_a_threshold(self):
        plain = contract.qa_text('tail')
        self.assertNotIn('BLOCKING', plain); self.assertNotIn('Blocking threshold', plain)
        self.assertNotIn('known issue', plain)
        expected = {'P0': '(P0)', 'P1': '(P0, P1)', 'P2': '(P0, P1, P2)', 'P3': '(P0, P1, P2, P3)'}
        for threshold, blocking in expected.items():
            with self.subTest(threshold=threshold):
                tail = contract.qa_text('tail', 'task', threshold)
                self.assertIn('Blocking threshold: %s.' % threshold, tail)
                self.assertIn(blocking, tail)
                flat = ' '.join(tail.split())
                self.assertIn('if and only if at least one CONFIRMED finding is blocking' if threshold != 'P3'
                              else 'any CONFIRMED finding justifies FAIL', flat)
                self.assertIn('without a severity tag counts as blocking', tail)
                self.assertIn('blocking threshold (%s or worse)' % threshold, tail)
                self.assertNotIn('no confirmed correctness bug', tail)
        self.assertIn('recorded as a known issue', contract.qa_text('tail', 'task', 'P2'))
        self.assertIn('(P3) is recorded', contract.qa_text('tail', 'task', 'P2'))

    def test_deterministic_clean_text(self):
        for part in ('head', 'tail'):
            for kind in contract.KINDS:
                for threshold in (None, 'P0', 'P3'):
                    text = contract.qa_text(part, kind, threshold)
                    self.assertEqual(text, contract.qa_text(part, kind, threshold))
                    self.assertTrue(text.endswith('\n') and not text.endswith('\n\n') and not text.startswith('\n'))
                    self.assertFalse(any(line != line.rstrip() for line in text.splitlines()), (part, kind))
        self.assertTrue(contract.dwarf_text().endswith('\n'))

    def test_invalid_arguments_raise(self):
        for call in (lambda: contract.qa_text('middle'), lambda: contract.qa_text('head', 'other'),
                     lambda: contract.qa_text('tail', 'task', 'P4'), lambda: contract.gate('FAIL', '', 'high')):
            with self.assertRaises(ValueError):
                call()

    def test_the_verdict_lines_still_parse(self):
        # Reviewers copy the verdict line with its gloss; forge_verdict must accept every variant.
        artifact = ROOT / 'scripts' / 'forge-artifact.sh'
        for threshold in (None, 'P0', 'P2', 'P3'):
            lines = contract.qa_text('tail', 'task', threshold).splitlines()[-2:]
            for line, expected in zip(lines, ('PASS', 'FAIL')):
                with self.subTest(threshold=threshold, expected=expected):
                    with tempfile.TemporaryDirectory() as tmp:
                        last = Path(tmp) / 'qa.last'
                        last.write_text('review\n' + line.strip() + '\n', encoding='utf-8')
                        out = subprocess.run(['/bin/bash', '-c', 'source "$1"; forge_verdict "$2"', 'x', str(artifact), str(last)],
                                             capture_output=True, text=True, encoding='utf-8')
                    self.assertEqual(out.stdout.strip(), expected, out.stderr)


class DwarfRulesTests(unittest.TestCase):
    def test_rules(self):
        text = contract.dwarf_text()
        for phrase in ('git commit, checkout, switch, reset, stash or rebase', 'change set ambiguous',
                       'background processes', 'leave nothing running', 'BEFORE your last message',
                       'actual', 'say so explicitly instead of claiming it passed',
                       'which verification commands you ran'):
            self.assertIn(phrase, ' '.join(text.split()), phrase)
        self.assertLessEqual(len(text.splitlines()), 8)
        self.assertFalse(any(line != line.rstrip() for line in text.splitlines()))


class CommandLineTests(unittest.TestCase):
    def test_output_matches_the_functions(self):
        for part in ('head', 'tail'):
            for kind in contract.KINDS:
                with self.subTest(part=part, kind=kind):
                    result = run('qa', '--part', part, '--kind', kind, '--threshold', 'P2')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, contract.qa_text(part, kind, 'P2'))
        self.assertEqual(run('qa', '--part', 'tail').stdout, contract.qa_text('tail'))
        self.assertEqual(run('qa', '--part', 'tail', '--threshold', '').stdout, contract.qa_text('tail'))
        self.assertEqual(run('qa', '--part', 'head', '--kind', 'final').stdout, contract.qa_text('head', 'final'))
        self.assertEqual(run('dwarf').stdout, contract.dwarf_text())

    def test_usage_errors_exit_2(self):
        for args in ([], ['qa'], ['qa', '--part', 'middle'], ['qa', '--part', 'head', '--kind', 'x'],
                     ['qa', '--part', 'head', '--threshold', 'P4'], ['bogus'], ['gate'],
                     ['gate', '--verdict', 'MAYBE', '--qa-last', 'x'], ['gate', '--verdict', 'FAIL'],
                     ['gate', '--qa-last', 'x'], ['dwarf', '--extra']):
            with self.subTest(args=args):
                result = run(*args)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(result.stdout, '')

    def test_unreadable_file_exits_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            for target in (Path(tmp) / 'nope', Path(tmp)):
                result = run('gate', '--verdict', 'FAIL', '--qa-last', target, '--threshold', 'P2')
                self.assertEqual(result.returncode, 3)
                self.assertEqual(result.stdout, '')
                self.assertIn('cannot read', result.stderr)

    def test_gate_prints_one_tab_separated_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            last = Path(tmp) / 'qa.last'
            last.write_bytes(b'- [P3][CONFIRMED] a.py:1 \xe2\x80\x94 nit\r\n\r\nFORGE_VERDICT: FAIL\r\n')
            result = run('gate', '--verdict', 'FAIL', '--qa-last', last, '--threshold', 'P2')
            self.assertEqual((result.returncode, result.stdout), (0, 'accept\tP3\t1\t0\t1\n'))
            result = run('gate', '--verdict', 'FAIL', '--qa-last', last)
            self.assertEqual((result.returncode, result.stdout), (0, 'keep\tP3\t1\t0\t0\n'))
            last.write_bytes(b'\xff\xfe- [P3][CONFIRMED] a.py:1 invalid utf-8\n')
            self.assertEqual(run('gate', '--verdict', 'FAIL', '--qa-last', last, '--threshold', 'P2').returncode, 0)
            last.write_bytes(b'')
            self.assertEqual(run('gate', '--verdict', 'UNKNOWN', '--qa-last', last, '--threshold', 'p2').stdout,
                             'keep\t-\t0\t0\t0\n')

    def test_executable_with_a_python3_shebang(self):
        self.assertTrue(SCRIPT.stat().st_mode & 0o100)
        self.assertEqual(SCRIPT.read_text(encoding='utf-8').splitlines()[0], '#!/usr/bin/env python3')


if __name__ == '__main__':
    unittest.main()
