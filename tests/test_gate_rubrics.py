"""The rubrics of the C3-C5 gates that ask about one item (#43-#47), and the verbs that ask them.

The rubrics are checked for wire shape, for binding (two items never share a text) and for the
600-character cut. The verbs are run with no module behind them, as they are until each gate's
module lands: each must exit 3 with nothing on stdout, exit 2 on a missing argument or path, and
hand its namespace to the module's main when there is one.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SHIM = ROOT / 'scripts' / 'forge-jev.py'
sys.path.insert(0, str(ROOT / 'scripts'))
import forge_jev  # noqa: E402
from forge_jev import cli, questions  # noqa: E402

# Environment that would make a verb reach a real model or read a developer's config.
ISOLATED_PREFIXES = ('FORGE_JEV', 'FORGE_DECIDE', 'FORGE_CLEF', 'TYPESAFE', 'CLOUDFLARE')

# Verb -> the module it imports (scripts/forge_jev/<module>.py).
MODULES = {'coverage': 'coverage', 'retry-addressed': 'addressed', 'dwarf-message': 'message',
           'attribution': 'attribution', 'findings': 'findings'}

CANDIDATES = {'7': 'Crash on empty input', '9': 'Slow startup'}


def flat(pairs):
    return [token for pair in pairs for token in pair]


class RubricShapeTests(unittest.TestCase):
    def test_requirement_coverage_is_a_choice_over_the_tasks_and_none(self):
        q = questions.requirement_coverage('the CLI prints a summary line', ['a', 'b'])
        self.assertEqual(q['type'], 'choice')
        self.assertEqual(q['criteria'], {'a': 'task a covers it', 'b': 'task b covers it',
                                         'none': "no task's acceptance criteria cover it"})

    def test_requirement_coverage_without_a_task_is_refused(self):
        with self.assertRaises(ValueError):
            questions.requirement_coverage('the CLI prints a summary line', [])

    def test_finding_untouched_is_a_noul_with_its_true_and_false_criteria(self):
        q = questions.finding_untouched('leaks the handle in open()')
        self.assertEqual(q['type'], 'noul')
        self.assertEqual(set(q['criteria']), {'true', 'false'})
        self.assertIn("'leaks the handle in open()'", q['instructions'])

    def test_the_dwarf_message_rubrics_are_nouls_without_criteria(self):
        for builder in (questions.message_unfinished, questions.message_blocked,
                        questions.message_question):
            with self.subTest(builder=builder.__name__):
                q = builder()
                self.assertEqual(q['type'], 'noul')
                self.assertNotIn('criteria', q)
                self.assertTrue(q['instructions'])

    def test_culprit_is_a_choice_over_the_merged_tasks_and_none(self):
        q = questions.culprit(['t1', 't2'])
        self.assertEqual(q['type'], 'choice')
        self.assertEqual(set(q['criteria']), {'t1', 't2', 'none'})

    def test_culprit_without_a_merged_task_is_refused(self):
        with self.assertRaises(ValueError):
            questions.culprit([])

    def test_finding_duplicate_is_a_choice_over_the_candidates_and_none(self):
        q = questions.finding_duplicate('leaks the handle in open()', CANDIDATES)
        self.assertEqual(q['type'], 'choice')
        self.assertEqual(set(q['criteria']), {'7', '9', 'none'})

    def test_finding_duplicate_without_a_candidate_is_refused(self):
        with self.assertRaises(ValueError):
            questions.finding_duplicate('leaks the handle in open()', {})

    def test_finding_concrete_is_a_noul_with_its_true_and_false_criteria(self):
        q = questions.finding_concrete('leaks the handle in open()')
        self.assertEqual(q['type'], 'noul')
        self.assertEqual(set(q['criteria']), {'true', 'false'})
        self.assertIn("'leaks the handle in open()'", q['instructions'])


class BindingTests(unittest.TestCase):
    """A rubric about one item quotes that item: two items never get the same text."""

    def assertBinds(self, build, first, second):
        one, two = build(first), build(second)
        self.assertNotEqual(one['instructions'], two['instructions'])
        self.assertIn(repr(first), one['instructions'])
        self.assertIn(repr(second), two['instructions'])
        self.assertNotIn(repr(first), two['instructions'])

    def test_finding_untouched_binds_each_finding(self):
        self.assertBinds(questions.finding_untouched,
                         'leaks the handle in open()', 'skips the retry on a 503')

    def test_finding_concrete_binds_each_finding(self):
        self.assertBinds(questions.finding_concrete,
                         'the naming of helper_x is unclear', 'the loop never ends on empty input')

    def test_finding_duplicate_binds_each_finding(self):
        self.assertBinds(lambda finding: questions.finding_duplicate(finding, CANDIDATES),
                         'leaks the handle in open()', 'skips the retry on a 503')

    def test_requirement_coverage_binds_each_requirement(self):
        self.assertBinds(lambda requirement: questions.requirement_coverage(requirement, ['a', 'b']),
                         'the CLI prints a summary line', 'the CLI exits 2 on a bad path')


class CutTests(unittest.TestCase):
    """Text quoted into a rubric is cut to 600 characters first."""

    def test_a_long_item_is_cut_to_600_characters_before_it_is_quoted(self):
        long = 'a' * 600 + 'TAIL' + 'b' * 1400
        texts = {
            'finding_untouched': questions.finding_untouched(long)['instructions'],
            'finding_concrete': questions.finding_concrete(long)['instructions'],
            'finding_duplicate': questions.finding_duplicate(long, CANDIDATES)['instructions'],
            'requirement_coverage': questions.requirement_coverage(long, ['a', 'b'])['instructions'],
        }
        for name, text in texts.items():
            with self.subTest(builder=name):
                self.assertIn(repr('a' * 600), text)
                self.assertNotIn('TAIL', text)

    def test_a_long_candidate_title_is_cut_too(self):
        long = 'a' * 600 + 'TAIL'
        q = questions.finding_duplicate('leaks the handle', {'7': long, '9': 'Slow startup'})
        self.assertIn(repr('a' * 600), q['criteria']['7'])
        self.assertNotIn('TAIL', q['criteria']['7'])


class VerbFixture(unittest.TestCase):
    """A plan dir, a repo dir, a verify log, a findings input and a private config home."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.plan = root / 'plan'
        self.repo = root / 'repo'
        self.plan.mkdir()
        self.repo.mkdir()
        self.log = root / 'verify.log'
        self.log.write_text('FAILED test_x\n')
        self.findings = root / 'findings.json'
        self.findings.write_text('{"findings": [], "candidates": []}\n')
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(ISOLATED_PREFIXES)}
        self.env['XDG_CONFIG_HOME'] = str(root / 'config')

    def requests(self):
        """Each verb with the arguments it requires, as (flag, value) pairs."""
        plan, repo = str(self.plan), str(self.repo)
        return {
            'coverage': [('--plan', plan), ('--repo', repo)],
            'retry-addressed': [('--plan', plan), ('--task', 't1'), ('--attempt', '2'), ('--repo', repo)],
            'dwarf-message': [('--plan', plan), ('--task', 't1'), ('--attempt', '2'), ('--repo', repo)],
            'attribution': [('--plan', plan), ('--repo', repo), ('--log', str(self.log))],
            'findings': [('--repo', repo), ('--input', str(self.findings))],
        }

    def run_shim(self, verb, pairs, extra=()):
        return subprocess.run([sys.executable, str(SHIM), verb, *flat(pairs), *extra],
                              capture_output=True, text=True, env=self.env)


class VerbShimTests(VerbFixture):
    """`python3 scripts/forge-jev.py <verb>` with no module behind the verb."""

    def test_each_verb_without_a_module_exits_3_with_nothing_on_stdout(self):
        for verb, pairs in self.requests().items():
            with self.subTest(verb=verb):
                result = self.run_shim(verb, pairs)
                self.assertEqual((result.returncode, result.stdout), (3, ''), result.stderr)

    def test_a_missing_required_argument_exits_2(self):
        for verb, pairs in self.requests().items():
            for index, (flag, _) in enumerate(pairs):
                with self.subTest(verb=verb, missing=flag):
                    result = self.run_shim(verb, pairs[:index] + pairs[index + 1:])
                    self.assertEqual(result.returncode, 2, result.stderr)

    def test_a_directory_that_is_not_there_exits_2(self):
        missing = str(self.plan / 'no-such-plan')
        result = self.run_shim('coverage', [('--plan', missing), ('--repo', str(self.repo))])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('--plan', result.stderr)
        self.assertIn('is not a directory', result.stderr)

    def test_a_file_that_is_not_there_exits_2(self):
        cases = {
            'attribution': [('--plan', str(self.plan)), ('--repo', str(self.repo)),
                            ('--log', str(self.plan / 'no-such.log'))],
            'findings': [('--repo', str(self.repo)), ('--input', str(self.plan / 'no-such.json'))],
        }
        for verb, pairs in cases.items():
            with self.subTest(verb=verb):
                result = self.run_shim(verb, pairs)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('is not a file', result.stderr)

    def test_findings_accepts_run_dir(self):
        result = self.run_shim('findings', self.requests()['findings'],
                               extra=('--run-dir', str(self.plan)))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_findings_run_dir_that_is_not_there_exits_2(self):
        result = self.run_shim('findings', self.requests()['findings'],
                               extra=('--run-dir', str(self.plan / 'no-such-run')))
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_coverage_accepts_json(self):
        result = self.run_shim('coverage', self.requests()['coverage'], extra=('--json',))
        self.assertEqual(result.returncode, 3, result.stderr)


class VerbDelegationTests(VerbFixture):
    """`cli.main` in-process: the verb's module is faked, so what it receives can be checked."""

    def test_each_verb_hands_its_namespace_to_its_module_and_returns_its_code(self):
        for verb, module_name in MODULES.items():
            with self.subTest(verb=verb):
                seen = {}

                def main(args):
                    seen['args'] = args
                    seen['repo'] = forge_jev.current_repo()
                    return 0

                fake = types.ModuleType(f'forge_jev.{module_name}')
                fake.main = main
                with patch.dict(sys.modules, {f'forge_jev.{module_name}': fake}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = cli.main([verb, *flat(self.requests()[verb])])
                self.assertEqual(code, 0)
                self.assertEqual(seen['args'].command, verb)
                self.assertEqual(seen['repo'], str(self.repo.resolve()))

    def test_the_attempt_reaches_the_module_as_an_int(self):
        seen = {}

        def main(args):
            seen['attempt'] = args.attempt
            return 0

        fake = types.ModuleType('forge_jev.message')
        fake.main = main
        with patch.dict(sys.modules, {'forge_jev.message': fake}), \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(['dwarf-message', *flat(self.requests()['dwarf-message'])])
        self.assertEqual(seen['attempt'], 2)

    def test_a_module_without_a_main_exits_3_with_nothing_on_stdout(self):
        out = io.StringIO()
        with patch.dict(sys.modules, {'forge_jev.coverage': types.ModuleType('forge_jev.coverage')}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(['coverage', *flat(self.requests()['coverage'])])
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), '')

    def test_a_module_that_cannot_be_imported_exits_3_with_nothing_on_stdout(self):
        out = io.StringIO()
        with patch.dict(sys.modules, {'forge_jev.addressed': None}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(['retry-addressed', *flat(self.requests()['retry-addressed'])])
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
