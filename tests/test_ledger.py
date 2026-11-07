"""The findings ledger (GitHub #69, part 1): the matcher, the ledger rows, the signal, the decisions and
the command line. Task dirs are built by hand the way the dispatch and forge-runtime.py leave them:
attempts/qa-<uuid>/ with qa.last and metrics.json, plus attempts.tsv, qa.gate and changes.diff."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-ledger.py'


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec); spec.loader.exec_module(result)
    return result


ledger = module('forge-ledger')
contract = ledger.contract

CACHE = '- [P1][CONFIRMED] src/cache.py:10 — the cache key ignores the user id'
REWORDED = '- [P1][CONFIRMED] src/cache.py:14 — the cache key ignores the user identity'
ELSEWHERE = '- [P1][CONFIRMED] src/other.py:10 — the cache key ignores the user id'
UNRELATED = '- [P1][CONFIRMED] src/cache.py:90 — the retry loop never sleeps between attempts'
UNLOCATED = '- [P2][CONFIRMED] the retry loop never sleeps between attempts'
UNLOCATED_REWORDED = '- [P2][CONFIRMED] the retry loop never sleeps between failed attempts'
SPACED = '- [P1][CONFIRMED] src/first dir/cache.py:10 — the cache key ignores the user id'
SPACED_REWORDED = '- [P1][CONFIRMED] src/first dir/cache.py:14 — the cache key ignores the user identity'
FIRST_DIR = '- [P1][CONFIRMED] src/first dir/cache.py:10 — the cache ignores account identity'
SECOND_DIR = '- [P1][CONFIRMED] src/second dir/cache.py:20 — the cache ignores account identity'
BOLD_A = '- [P1][CONFIRMED] **src/a.py:10** — the cache ignores account identity'
BOLD_B = '- [P1][CONFIRMED] **src/b.py:20** — the cache ignores account identity'
BOLD_TAGS = '- **P1** **CONFIRMED** **src/cache.py:14** — the cache key ignores the user identity'
PLAIN_CACHE = '- [P1][CONFIRMED] src/cache.py:14 — the cache key ignores the user identity'
PAREN = '- [P1][CONFIRMED] (src/cache.py:14) — the cache key ignores the user identity'
BRACKET = '- [P1][CONFIRMED] [src/cache.py:14] — the cache key ignores the user identity'
DOCKER_ONE = '- [P1][CONFIRMED] one/Dockerfile:10 — the image runs as root'
DOCKER_TWO = '- [P1][CONFIRMED] two/Dockerfile:20 — the image runs as root'
DOCKER_ONE_REWORDED = '- [P1][CONFIRMED] one/Dockerfile:12 — the image runs as the root user'

FIRST = '\n'.join([
    '- [P1][CONFIRMED] src/a.py:10 — the cache key ignores the user id',
    '- [P2][CONFIRMED] src/b.py:5 — the retry loop never sleeps between attempts',
    '- [P2][PLAUSIBLE] src/c.py:7 — the log may be truncated',
])
SECOND = '\n'.join([
    '- [P1][CONFIRMED] src/a.py:14 — the cache key ignores the user identity',
    '- [P3][CONFIRMED] src/d.py:3 — the docstring names a removed flag',
])
THIRD = '\n'.join([
    '- [P2][CONFIRMED] src/d.py:3 — the docstring still names a removed flag',
    '- [P2][CONFIRMED] src/e.py:9 — the output is written before the rename',
])
FOURTH = '- [P1][CONFIRMED] src/f.py:1 — this review wrote no verdict line'

# Two histories that differ in one thing: the first keeps finding new problems at the same severity,
# the second finds the same problem at a lower severity each time.
MOVING = [
    '- [P2][CONFIRMED] src/a.py:1 — the cache key ignores the user id',
    '- [P1][CONFIRMED] src/b.py:2 — the retry loop never sleeps between attempts\n'
    '- [P2][CONFIRMED] src/c.py:3 — the parser drops the final record',
    '- [P1][CONFIRMED] src/d.py:4 — the export writes before the rename',
    '- [P1][CONFIRMED] src/e.py:5 — the reader leaks the handle on error',
]
CONVERGING = [
    '- [P1][CONFIRMED] src/a.py:1 — the cache key ignores the user id',
    '- [P2][CONFIRMED] src/b.py:2 — the retry loop never sleeps between attempts',
    '- [P3][CONFIRMED] src/c.py:3 — the export writes before the rename',
    '- [P3][CONFIRMED] src/d.py:4 — the reader leaks the handle on error',
]


STALE_FACTS = '- [P2][CONFIRMED] src/cache.py:10 — stale entry {reach=unusual origin=new evidence=traced}'
HEADER_FACTS = '- [P2][CONFIRMED] src/cache.py:40 — wrong header {reach=unusual origin=new evidence=traced}'


def finding(text):
    found = contract.parse_findings(text)
    assert len(found) == 1, found
    return found[0]


def make_review(task, started, body, verdict='FAIL'):
    """A review dir as forge-runtime.py leaves it: attempts/qa-<uuid>/ with qa.last and metrics.json."""
    review = task / 'attempts' / ('qa-' + uuid.uuid4().hex)
    review.mkdir(parents=True)
    tail = '' if verdict is None else '\nFORGE_VERDICT: %s\n' % verdict
    (review / 'qa.last').write_text(body + tail, encoding='utf-8')
    (review / 'metrics.json').write_text(
        json.dumps({'role': 'qa', 'wall_started': started, 'exit_code': 0}), encoding='utf-8')
    return review


def record(task, epoch, attempt):
    """One attempts.tsv row, written when an attempt ends (epoch, attempt, status, infra class, dwarf)."""
    with (task / 'attempts.tsv').open('a', encoding='utf-8') as handle:
        handle.write('%d\t%d\tFAIL\t\t\n' % (epoch, attempt))


def signal_row(attempt=1, verdict='FAIL', gate='-', confirmed=3, new=2, worst='P1'):
    return {'attempt': attempt, 'verdict': verdict, 'gate': gate,
            'confirmed': confirmed, 'new': new, 'worst': worst}


def snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes() if path.is_file() else None)
            for path in sorted(root.rglob('*'))}


class MatchTests(unittest.TestCase):
    def test_same_finding(self):
        cases = [
            ('a reworded finding in the same file matches', CACHE, REWORDED, True),
            ('the same words in another file do not match', CACHE, ELSEWHERE, False),
            ('an unrelated finding in the same file does not match', CACHE, UNRELATED, False),
            ('two unlocated findings with similar text match', UNLOCATED, UNLOCATED_REWORDED, True),
            ('a reworded finding in a directory with a space matches', SPACED, SPACED_REWORDED, True),
            ('the same basename in two directories with spaces does not match', FIRST_DIR, SECOND_DIR, False),
            ('a bold location in the same file as a plain one matches', BOLD_TAGS, PLAIN_CACHE, True),
            ('bold locations in two files do not match', BOLD_A, BOLD_B, False),
            ('a location in parentheses matches its plain form', PAREN, PLAIN_CACHE, True),
            ('a location in brackets matches its plain form', BRACKET, PLAIN_CACHE, True),
            ('a reworded finding at the same extensionless path matches', DOCKER_ONE, DOCKER_ONE_REWORDED, True),
            ('the same extensionless name in two directories does not match', DOCKER_ONE, DOCKER_TWO, False),
            # #119: the facts suffix's words are no content: two problems in one file stay two
            ('two findings that share only their facts do not match', STALE_FACTS, HEADER_FACTS, False),
            ('a reworded finding with facts on one side matches', CACHE + ' {reach=main origin=new evidence=ran}',
             REWORDED, True),
        ]
        for name, a, b, expected in cases:
            with self.subTest(name):
                self.assertEqual(ledger.same_finding(finding(a), finding(b)), expected)
                self.assertEqual(ledger.same_finding(finding(b), finding(a)), expected)

    def test_file_of_a_finding(self):
        self.assertEqual(ledger._file(finding(BOLD_A)), 'src/a.py')
        self.assertEqual(ledger._file(finding(BOLD_TAGS)), 'src/cache.py')
        self.assertEqual(ledger._file(finding(DOCKER_ONE)), 'one/Dockerfile')
        self.assertEqual(ledger._file(finding('- [P1][CONFIRMED] Dockerfile:3 — the image runs as root')), 'Dockerfile')
        # `the retry loop runs 3` has a space and no extension: prose that happens to end in `:30`, not a file.
        self.assertIsNone(ledger._file(finding('- [P2][CONFIRMED] the retry loop runs 3:30 times')))
        self.assertIsNone(ledger._file(finding('- [P2][CONFIRMED] the retry loop never sleeps between attempts')))


class RowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.task = Path(tmp.name) / 'plan' / 'tasks' / 't1'
        self.task.mkdir(parents=True)

    def history(self):
        first = make_review(self.task, 100, FIRST)
        second = make_review(self.task, 200, SECOND)
        third = make_review(self.task, 300, THIRD)
        record(self.task, 150, 1)     # the first review ran in attempt 1 ...
        record(self.task, 250, 2)     # ... the second in attempt 2
        (self.task / 'attempt').write_text('3\n')   # the third is still in attempt 3, unrecorded
        (self.task / 'changes.diff').write_bytes(b'x' * 1234)
        (self.task / 'changes.since.diff').write_bytes(b'x' * 77)
        (self.task / 'qa.gate').write_text('P2\tkeep\tP2\t2\t0\t0\n')
        return first, second, third

    def test_three_reviews_hand_computed(self):
        first, second, third = self.history()
        # The PLAUSIBLE finding of the first review is not counted: 2 confirmed, not 3.
        expected = [
            dict(epoch=100, attempt=1, review=first.name, verdict='FAIL', gate='-', worst='P1',
                 confirmed=2, new=2, carried=0, diff_bytes=1234, delta_bytes=77),
            # A (P1, reworded) is carried; B is gone; D is new (P3).
            dict(epoch=200, attempt=2, review=second.name, verdict='FAIL', gate='-', worst='P1',
                 confirmed=2, new=1, carried=1, diff_bytes=1234, delta_bytes=77),
            # D (P2, reworded) is carried; E is new. The gate column belongs to the latest review.
            dict(epoch=300, attempt=3, review=third.name, verdict='FAIL', gate='keep', worst='P2',
                 confirmed=2, new=1, carried=1, diff_bytes=1234, delta_bytes=77),
        ]
        self.assertEqual(ledger.task_rows(self.task), expected)

    def test_a_review_starting_after_a_recorded_second_is_the_next_attempt(self):
        # attempts.tsv records whole seconds: attempt 1 was recorded at 100. A review that starts at
        # 100.0 ran in attempt 1; one that starts at 100.9 came after it, so it is attempt 2.
        record(self.task, 100, 1)
        record(self.task, 200, 2)
        make_review(self.task, 100.0, FIRST)
        make_review(self.task, 100.9, SECOND)
        self.assertEqual([row['attempt'] for row in ledger.task_rows(self.task)], [1, 2])

    def test_review_without_a_verdict_is_not_a_row(self):
        self.history()
        make_review(self.task, 400, FOURTH, verdict=None)
        self.assertEqual(len(ledger.task_rows(self.task)), 3)

    def test_missing_changes_since_diff_is_a_dash(self):
        self.history()
        (self.task / 'changes.since.diff').unlink()
        self.assertEqual({row['delta_bytes'] for row in ledger.task_rows(self.task)}, {'-'})


class SignalTests(unittest.TestCase):
    def test_signal_table(self):
        cases = [
            ('moving target: 4 failed reviews, mostly new, worst not improving fires', [
                signal_row(confirmed=2, new=2, worst='P2'), signal_row(confirmed=3, new=2, worst='P1'),
                signal_row(confirmed=3, new=3, worst='P1'), signal_row(confirmed=3, new=2, worst='P2')], True),
            ('converging: worst improves from P1 to P3 does not fire', [
                signal_row(confirmed=3, new=3, worst='P1'), signal_row(confirmed=3, new=3, worst='P2'),
                signal_row(confirmed=3, new=2, worst='P2'), signal_row(confirmed=2, new=2, worst='P3')], False),
            ('flat: the same findings carried every round does not fire', [
                signal_row(confirmed=3, new=0, worst='P2') for _ in range(4)], False),
            ('a pass inside the window does not fire', [
                signal_row(confirmed=3, new=3, worst='P2'), signal_row(confirmed=3, new=3, worst='P1'),
                signal_row(verdict='PASS', confirmed=2, new=2, worst='P2'),
                signal_row(confirmed=3, new=3, worst='P2')], False),
            ('a gate fail counts as a failed review', [
                signal_row(confirmed=3, new=3, worst='P2'), signal_row(confirmed=3, new=3, worst='P1'),
                signal_row(verdict='PASS', gate='fail', confirmed=2, new=2, worst='P2'),
                signal_row(confirmed=3, new=3, worst='P2')], True),
            ('a review in the window with no CONFIRMED finding does not fire', [
                signal_row(confirmed=3, new=3, worst='P2'), signal_row(confirmed=0, new=0, worst='-'),
                signal_row(confirmed=3, new=3, worst='P2'), signal_row(confirmed=3, new=3, worst='P2')], False),
        ]
        for name, rows, expected in cases:
            with self.subTest(name):
                self.assertEqual(ledger.converging(rows, window=4)[0], expected)

    def test_summary_line(self):
        rows = [signal_row(confirmed=2, new=2, worst='P2'), signal_row(confirmed=3, new=2, worst='P1'),
                signal_row(confirmed=3, new=3, worst='P1'), signal_row(confirmed=3, new=2, worst='P2')]
        self.assertEqual(ledger.converging(rows, window=4),
                         (True, '4 failed reviews in a row, 7 of 9 findings new, worst P2 → P2'))

    def test_window_three_fires_and_four_does_not_on_three_reviews(self):
        rows = [signal_row(confirmed=2, new=2, worst='P2'), signal_row(confirmed=3, new=2, worst='P2'),
                signal_row(confirmed=3, new=3, worst='P2')]
        self.assertTrue(ledger.converging(rows, window=3)[0])
        self.assertFalse(ledger.converging(rows, window=4)[0])

    def test_window_zero_never_fires(self):
        rows = [signal_row(confirmed=3, new=3, worst='P2') for _ in range(6)]
        self.assertFalse(ledger.converging(rows, window=0)[0])
        self.assertFalse(ledger.converging(rows, window=1)[0])

    def test_share(self):
        rows = [signal_row(confirmed=2, new=2, worst='P2'), signal_row(confirmed=3, new=2, worst='P1'),
                signal_row(confirmed=3, new=3, worst='P1'), signal_row(confirmed=3, new=2, worst='P2')]
        self.assertTrue(ledger.converging(rows, window=4, share=0.5)[0])     # 7 of 9 new
        self.assertFalse(ledger.converging(rows, window=4, share=0.8)[0])


class DecisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.decisions = Path(tmp.name) / 'decisions.tsv'
        self.rows = [signal_row(attempt=number) for number in range(1, 7)]

    def test_rows_up_to_the_decided_attempt_are_ignored(self):
        self.decisions.write_text('400\t4\tcontinue\treviewed by hand\n', encoding='utf-8')
        kept = ledger.after_decision(self.rows, self.decisions)
        self.assertEqual([row['attempt'] for row in kept], [5, 6])
        self.assertTrue(ledger.converging(self.rows, window=4)[0])
        self.assertFalse(ledger.converging(kept, window=4)[0])

    def test_no_decisions_file_keeps_every_row(self):
        self.assertEqual(ledger.after_decision(self.rows, self.decisions), self.rows)


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan = Path(tmp.name) / 'plan'
        self.plan.mkdir()

    def cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)

    def build(self, task_id, bodies, append=False, first=100):
        """One review per body, each in its own attempt, as a run leaves them. With append, the
        runner's `append` runs after each review, as it would in a run."""
        task = self.plan / 'tasks' / task_id
        task.mkdir(parents=True)
        for number, body in enumerate(bodies, 1):
            started = first + 100 * number
            make_review(task, started, body)
            record(task, started + 50, number)
            if append:
                self.assertEqual(self.cli('append', self.plan, task_id).returncode, 0)
        return task

    def test_append_twice_leaves_one_row(self):
        task = self.build('once', [MOVING[0]])
        self.assertEqual(self.cli('append', self.plan, 'once').returncode, 0)
        self.assertEqual(self.cli('append', self.plan, 'once').returncode, 0)
        self.assertEqual(len((task / 'ledger.tsv').read_text().splitlines()), 1)
        second = make_review(task, 300, MOVING[1])
        record(task, 350, 2)
        self.assertEqual(self.cli('append', self.plan, 'once').returncode, 0)
        lines = (task / 'ledger.tsv').read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1].split('\t')[2], second.name)

    def test_append_with_nothing_to_append_exits_zero(self):
        (self.plan / 'tasks' / 'empty').mkdir(parents=True)
        self.assertEqual(self.cli('append', self.plan, 'empty').returncode, 0)
        self.assertFalse((self.plan / 'tasks' / 'empty' / 'ledger.tsv').exists())

    def test_append_skips_a_review_without_a_verdict(self):
        task = self.build('unverdicted', [MOVING[0]])
        unverdicted = make_review(task, 300, FOURTH, verdict=None)
        self.assertEqual(self.cli('append', self.plan, 'unverdicted').returncode, 0)
        lines = (task / 'ledger.tsv').read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertNotEqual(lines[0].split('\t')[2], unverdicted.name)

    def test_signal_fires_on_a_moving_target(self):
        self.build('moving', MOVING, append=True)
        result = self.cli('signal', self.plan / 'tasks' / 'moving')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout,
                         'fire\t4 failed reviews in a row, 4 of 4 findings new, worst P2 → P1\n')

    def test_signal_exits_one_and_prints_nothing_when_converging(self):
        self.build('converging', CONVERGING, append=True)
        result = self.cli('signal', self.plan / 'tasks' / 'converging')
        self.assertEqual((result.returncode, result.stdout), (1, ''))

    def test_window_from_the_plan_file_and_the_flag(self):
        self.build('three', MOVING[:3], append=True)
        task = self.plan / 'tasks' / 'three'
        self.assertEqual(self.cli('signal', task).returncode, 1)          # 3 reviews, window 4
        (self.plan / 'converge_window').write_text('3\n', encoding='utf-8')
        self.assertEqual(self.cli('signal', task).returncode, 0)          # the plan file says 3
        self.assertEqual(self.cli('signal', task, '--window', '4').returncode, 1)

    def test_decisions_reset_the_signal(self):
        task = self.build('decided', MOVING, append=True)
        self.assertEqual(self.cli('signal', task).returncode, 0)
        (task / 'decisions.tsv').write_text('250\t2\tcontinue\treviewed by hand\n', encoding='utf-8')
        self.assertEqual(self.cli('signal', task).returncode, 1)          # only attempts 3 and 4 count now

    def test_replay_prints_the_firing_row_and_writes_nothing(self):
        self.build('moving', MOVING)
        self.build('converging', CONVERGING)
        before = snapshot(self.plan)
        result = self.cli('replay', self.plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.plan), before)
        firing = [line for line in result.stdout.splitlines() if 'signal fires' in line]
        self.assertEqual(len(firing), 1)
        self.assertEqual(firing[0].split()[0], '4')                       # the fourth review
        self.assertEqual(result.stdout.count('the signal does not fire'), 1)   # the converging task

    def test_replay_rejects_a_missing_plan(self):
        self.assertEqual(self.cli('replay', self.plan / 'missing').returncode, 2)


if __name__ == '__main__':
    unittest.main()
