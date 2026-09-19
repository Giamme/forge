"""Post-review annotation. The load-bearing test here is that nothing overturns a FAIL."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from forge_jev import review  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def _answers(correctness=0.9, citation=0.9):
    answers = {}
    if correctness is not None:
        answers['correctness'] = dict(type='noul', noul=correctness)
    if citation is not None:
        answers['citation'] = dict(type='noul', noul=citation)
    return dict(answers=answers)


class AnnotateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.review = root / 'qa.last'
        self.diff = root / 'changes.diff'
        self.review.write_text('CONFIRMED: cart.py line 3 returns a negative total.')
        self.diff.write_text('--- a/cart.py\n+++ b/cart.py\n+    return subtotal\n')

    def _run(self, **kwargs):
        with patch('forge_jev.review.ask', return_value=_answers(**kwargs)):
            return review.annotate_failure(review_path=self.review, diff_path=self.diff)

    def test_a_sound_review_is_not_suspect(self):
        result = self._run(correctness=0.96, citation=0.88)
        self.assertFalse(result['suspect'])
        self.assertEqual(result['reasons'], [])

    def test_style_only_findings_are_flagged(self):
        result = self._run(correctness=0.21, citation=0.80)
        self.assertTrue(result['suspect'])
        self.assertIn('style preferences', result['reasons'][0])

    def test_a_bad_citation_is_reported_alone(self):
        # Measured on a hallucinated review: citation 0.02, correctness 0.45. Printing
        # "these are style preferences" alongside describes the wrong problem, because
        # nothing can be said about the correctness of code that is not in the diff.
        result = self._run(correctness=0.45, citation=0.02)
        self.assertEqual(len(result['reasons']), 1)
        self.assertIn('cites code', result['reasons'][0])

    def test_no_judgment_is_none_not_a_clean_bill(self):
        with patch('forge_jev.review.ask', return_value=None):
            self.assertIsNone(review.annotate_failure(review_path=self.review,
                                                      diff_path=self.diff))
        with patch('forge_jev.review.ask', return_value=dict(answers={})):
            self.assertIsNone(review.annotate_failure(review_path=self.review,
                                                      diff_path=self.diff))

    def test_a_missing_or_empty_file_asks_nothing(self):
        with patch('forge_jev.review.ask') as mocked:
            self.assertIsNone(review.annotate_failure(review_path='/nope/qa.last',
                                                      diff_path=self.diff))
            self.diff.write_text('   ')
            self.assertIsNone(review.annotate_failure(review_path=self.review,
                                                      diff_path=self.diff))
        mocked.assert_not_called()

    def test_oversized_inputs_are_truncated_and_say_so(self):
        self.diff.write_text('x' * (review.DIFF_CHARS + 500))
        captured = {}

        def fake_ask(state, questions, **kwargs):
            captured['state'] = state
            return _answers()

        with patch('forge_jev.review.ask', side_effect=fake_ask):
            result = review.annotate_failure(review_path=self.review, diff_path=self.diff)
        self.assertEqual(len(captured['state']['diff']), review.DIFF_CHARS)
        self.assertTrue(result['truncated'])

    def test_one_missing_answer_still_yields_a_judgment(self):
        result = self._run(correctness=0.2, citation=None)
        self.assertTrue(result['suspect'])


class IndependenceTests(unittest.TestCase):
    """Forge's second invariant, enforced by reading the shipped code.

    A unit test cannot prove the runner never rewrites a status, so this asserts the
    property structurally: the annotation block must not contain a status write.
    """

    def test_the_module_offers_no_way_to_change_a_verdict(self):
        source = (REPO_ROOT / 'scripts' / 'forge_jev' / 'review.py').read_text()
        for forbidden in ('status', 'PASS', 'verdict'):
            self.assertNotIn(forbidden + ' =', source)
        self.assertNotIn('FORGE_VERDICT', source)

    def test_the_runner_writes_no_status_inside_the_annotation_block(self):
        source = (REPO_ROOT / 'scripts' / 'forge-parallel.sh').read_text()
        start = source.index('Annotate a FAIL that looks unsound')
        block = source[start:source.index('\n  return 0', start)]
        self.assertIn('review-triage', block)
        # The one thing this block must never do.
        self.assertNotIn('> "$tdir/status"', block)
        self.assertNotIn("> '$tdir/status'", block)

    def test_the_solo_runner_has_the_same_block_with_the_same_restraint(self):
        source = (REPO_ROOT / 'scripts' / 'forge-solo.sh').read_text()
        start = source.index('Annotate a FAIL that looks unsound')
        block = source[start:source.index('\nfi\n', start)]
        self.assertIn('review-triage', block)
        self.assertIn('if [ "$verdict" = FAIL ]', block)
        # The verdict file is written before this block and never inside it.
        self.assertNotIn('> "$RUN/verdict"', block)
        self.assertLess(source.index('> "$RUN/verdict"'), start)

    def test_the_annotation_runs_only_for_a_failing_verdict(self):
        source = (REPO_ROOT / 'scripts' / 'forge-parallel.sh').read_text()
        start = source.index('Annotate a FAIL that looks unsound')
        # A PASS is the outcome that gets merged, so its reviewer's judgement is left
        # entirely alone -- nothing here ever makes a PASS look suspect.
        self.assertIn('if [ "$verdict" = FAIL ]', source[start:start + 400])


class CliContractTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        self.review = root / 'qa.last'
        self.diff = root / 'changes.diff'
        self.review.write_text('finding')
        self.diff.write_text('diff')

    def _exit(self, env):
        env = dict(env, XDG_CONFIG_HOME=str(Path(self.dir.name) / 'config'))
        env.pop('TYPESAFE_API_KEY', None)
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / 'scripts' / 'forge-jev.py'), 'review-triage',
             '--review', str(self.review), '--diff', str(self.diff)],
            capture_output=True, text=True, env=env)
        return result.returncode

    def test_disabled_jev_exits_3_and_says_nothing(self):
        import os
        env = dict(os.environ, FORGE_JEV='off')
        self.assertEqual(self._exit(env), 3)


if __name__ == '__main__':
    unittest.main()


class SummarySurfacingTests(unittest.TestCase):
    """A flag nobody sees is a flag that does nothing."""

    HELPER = None

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.plan = Path(self.dir.name)
        (self.plan / 'tasks' / 'alpha').mkdir(parents=True)
        (self.plan / 'tasks' / 'beta').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text(
            '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
            'alpha\t-\tlow\ta.py\t-\t-\tA\n'
            'beta\t-\tlow\tb.py\t-\t-\tB\n')

    def _run(self):
        script = (REPO_ROOT / 'scripts' / 'forge-parallel.sh').read_text()
        start = script.index('forge_jev_review_flags() {')
        end = script.index('\n}\n', start) + 3
        body = 'set -uo pipefail\n' \
               'all_ids() { awk -F"\\t" \'!/^#/ && NF {print $1}\' "$1"; }\n' \
               + script[start:end] + '\nforge_jev_review_flags "%s"\n' % self.plan
        return subprocess.run(['/bin/bash', '-c', body], capture_output=True, text=True)

    def test_a_flagged_review_is_named_in_the_summary(self):
        # It was written to tasks/<id>/task.out, which the scheduler echoes only under
        # FORGE_OUTPUT=full. The default is summary, so the one signal that a FAIL may
        # be unsound went where nobody reads it.
        (self.plan / 'tasks' / 'beta' / 'qa.jev.json').write_text(
            json.dumps({'reasons': ['the review cites code that may not be in this diff']}))
        out = self._run().stdout
        self.assertIn('beta', out)
        self.assertIn('cites code', out)
        self.assertIn('not an overturned one', out)

    def test_nothing_is_printed_when_no_review_was_flagged(self):
        # review-triage deletes the record when the review looks sound, so its absence
        # is the signal. A summary that says "no flags" on every clean run is noise.
        self.assertEqual(self._run().stdout.strip(), '')

    def test_an_unreadable_record_is_skipped_not_fatal(self):
        (self.plan / 'tasks' / 'alpha' / 'qa.jev.json').write_text('{not json')
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '')

    def test_a_record_with_no_reasons_prints_nothing(self):
        (self.plan / 'tasks' / 'alpha' / 'qa.jev.json').write_text(json.dumps({'reasons': []}))
        self.assertEqual(self._run().stdout.strip(), '')

