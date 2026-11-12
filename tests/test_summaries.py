"""Generated task summaries: one side dispatch, and only when the facts changed (#133).

The fake CLI harness is the one test_side uses, so a summary dispatch is a real
`forge-dispatch.sh side` and never a task attempt.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

import test_forge

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'forge-summarize.py'

REPLY = {
    'text': 'Login route checks the token.\n'
            'Attempt 1 failed on expiry.\n'
            'Attempt 2 was retried after review.\n'
            'The task stands passed.',
    'sentence': 'The login route passed after a review retry.',
}
ATTEMPTS = '1700000001\t1\tFAIL\t\tsol\n1700000002\t2\tPASS\t\tastra\n'
QA_FIRST = '- [P1][CONFIRMED] a.txt:1 — token check skips expiry\nFORGE_VERDICT: FAIL\n'
QA_LATEST = '- [P3][CONFIRMED] a.txt:4 — clock skew on expiry\nFORGE_VERDICT: PASS\n'
KNOWN = '- [P3][CONFIRMED] a.txt:9 — stale comment in the header\n'
TASKS = (
    'tokens\t-\tlow\ttoken.py\tsol\topus\tToken model\n'
    'a\ttokens\tmedium\tauth.py\tsol\topus\tLogin route\n'
    'docs\ta\tlow\tREADME.md\tsol\topus\tDocument auth\n'
)
# Hand-built. A change to the facts shape or to a title has to change this literal.
EXPECTED = {
    'id': 'a',
    'title': 'Login route',
    'status': 'PASS',
    'attempts': [
        {'attempt': 1, 'dwarf': 'sol', 'verdict': 'FAIL', 'findings': 1, 'worst': 'P1', 'reason': None},
        {'attempt': 2, 'dwarf': 'astra', 'verdict': 'PASS', 'findings': 1, 'worst': 'P3', 'reason': 'review'},
    ],
    'findings': [{'title': 'clock skew on expiry', 'severity': 'P3'}],
    'known_issues': ['stale comment in the header'],
    'dependencies': ['tokens'],
    'waiting': ['docs'],
}
SOURCES = [
    {'kind': 'attempt', 'ref': '1'},
    {'kind': 'attempt', 'ref': '2'},
    {'kind': 'finding', 'ref': '2:clock skew on expiry'},
]
HOLDER = (
    'import fcntl, os, sys\n'
    'fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o644)\n'
    'fcntl.flock(fd, fcntl.LOCK_EX)\n'
    'sys.stdout.write("locked\\n")\n'
    'sys.stdout.flush()\n'
    'sys.stdin.read()\n'
)


def hashed(body: dict) -> str:
    raw = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def summarize_module():
    """forge-summarize.py, loaded by path. The facts are what collect() returns."""
    spec = importlib.util.spec_from_file_location('forge_summarize_tests', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SummaryTests(unittest.TestCase):
    git = test_forge.ForgeTests.git
    scenario = test_forge.ForgeTests.scenario
    calls = test_forge.ForgeTests.calls

    def setUp(self):
        test_forge.ForgeTests.setUp(self)
        self._saved_env = dict(os.environ)
        self.addCleanup(self._restore_env)
        for name in ('FORGE_RETRO', 'FORGE_BRIEF', 'FORGE_SUMMARIES', 'FORGE_REGISTRY'):
            os.environ.pop(name, None)
            self.env.pop(name, None)
        self.plan = self.root / 'plan'
        self.cache = self.root / 'capability-cache'
        self.cache.mkdir()
        data_home = self.root / 'data-home'
        data_home.mkdir()
        os.environ['FORGE_DATA_HOME'] = str(data_home)
        self.env['FORGE_DATA_HOME'] = str(data_home)
        (self.repo / 'secret.txt').write_text('REPO_SENTINEL_do_not_send\n')

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._saved_env)

    def arm(self, table):
        self.scenario(table)
        os.environ.update(self.env)
        os.environ['FORGE_CAPABILITY_CACHE'] = str(self.cache)
        self.env['FORGE_CAPABILITY_CACHE'] = str(self.cache)

    def arm_reply(self, reply):
        self.arm({'*.qa.*': {'findings': json.dumps(reply), 'verdict': 'none'}})

    def summarize(self, *args, timeout=120):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            cwd=str(self.repo), env=self.env, text=True, capture_output=True, timeout=timeout)

    def _fresh(self, tasks='a\t-\tlow\ta.txt\tsol\topus\tA\n'):
        if self.plan.exists():
            shutil.rmtree(self.plan)
        self.plan.mkdir()
        (self.plan / 'repo').write_text(str(self.repo) + '\n')
        (self.plan / 'tasks.tsv').write_text(tasks)
        calls = self.root / 'calls'
        if calls.exists():
            calls.unlink()
        decide = self.repo / '.forge' / 'decide.json'
        if decide.exists():
            decide.unlink()
        self.env.pop('FORGE_SUMMARIES', None)

    def _on(self, model='sol'):
        (self.plan / 'summaries').write_text('on\n')
        (self.plan / 'summaries_model').write_text(model + '\n')

    def _rich(self):
        """One task with two attempts, a saved review and a later one, plus its neighbours."""
        self._fresh(TASKS)
        self._on()
        task = self.plan / 'tasks' / 'a'
        task.mkdir(parents=True)
        (task / 'status').write_text('PASS\n')
        (task / 'attempts.tsv').write_text(ATTEMPTS)
        (task / 'retry_sources.tsv').write_text('2\treview\t1700000002\n')
        (task / 'qa.last').write_text(QA_LATEST)
        (task / 'known_issues.md').write_text(KNOWN)
        (task / 'prompt.md').write_text('PROMPT_SENTINEL_do_not_send\n')
        (task / 'changes.diff').write_text('DIFF_SENTINEL_do_not_send\n')
        archive = task / 'attempts' / 'qa-1'
        archive.mkdir(parents=True)
        (archive / 'qa.last').write_text(QA_FIRST)
        (archive / 'dataset.json').write_text(json.dumps({'attempt_now': 1}) + '\n')
        (archive / 'metrics.json').write_text(json.dumps({'wall_started': 1700000001}) + '\n')
        return task

    def side_dirs(self, task='a'):
        root = self.plan / 'side' / 'summary' / task
        if not root.is_dir():
            return []
        return sorted(path.name for path in root.iterdir() if path.is_dir())

    def summary(self, task='a'):
        return json.loads((self.plan / 'tasks' / task / 'summary.json').read_text())

    def test_nothing_is_dispatched_when_summaries_are_refused(self):
        # Each case is one switch away from a plan that would dispatch. The model is set,
        # so a missing check would run and leave side/summary behind.
        cases = ('off', 'env', 'never-send')
        for name in cases:
            with self.subTest(name=name):
                self._fresh()
                self._on()
                if name == 'off':
                    (self.plan / 'summaries').write_text('off\n')
                    reason = 'off'
                elif name == 'env':
                    self.env['FORGE_SUMMARIES'] = 'off'
                    reason = 'env'
                else:
                    forge = self.repo / '.forge'
                    forge.mkdir(exist_ok=True)
                    (forge / 'decide.json').write_text('{"privacy": "never-send"}\n')
                    reason = 'never-send'
                self.arm_reply(REPLY)
                result = self.summarize(self.plan, '--json')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout),
                                 {'reason': reason, 'status': 'skipped', 'task': 'a'})
                self.assertFalse((self.plan / 'side' / 'summary').exists())
                self.assertEqual(self.calls(), [])

    def test_the_first_summary_records_every_field_and_its_sources(self):
        task = self._rich()
        (task / 'summary.error').write_text('stale\n')
        before = int(time.time())
        self.arm_reply(REPLY)
        attempts = (task / 'attempts.tsv').read_text()
        result = self.summarize(self.plan, '--task', 'a', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'reason': None, 'status': 'done', 'task': 'a'})
        self.assertEqual(self.calls(), ['qa'])
        self.assertEqual(self.side_dirs(), ['1'])
        dest = self.plan / 'side' / 'summary' / 'a' / '1'
        self.assertIn('role=side\n', (dest / 'side.resolved').read_text())
        self.assertEqual((task / 'attempts.tsv').read_text(), attempts)
        self.assertEqual(sorted(path.name for path in (task / 'attempts').iterdir()), ['qa-1'])
        self.assertFalse((task / 'summary.error').exists())

        body = self.summary()
        self.assertEqual(set(body), {'text', 'sentence', 'model', 'generated_at', 'inputs_hash', 'sources'})
        self.assertEqual(body['text'], REPLY['text'])
        self.assertEqual(body['sentence'], REPLY['sentence'])
        self.assertEqual(body['model'], 'sol')
        self.assertIsInstance(body['generated_at'], int)
        self.assertGreaterEqual(body['generated_at'], before)
        self.assertLessEqual(body['generated_at'], int(time.time()))
        self.assertEqual(body['sources'], SOURCES)
        self.assertEqual(body['inputs_hash'], hashed(EXPECTED))

        prompt = (dest / 'prompt.txt').read_text()
        self.assertIn('Use only the facts given. Invent nothing. Do not restate the live "Now" facts.', prompt)
        self.assertIn('3 to 5 short lines', prompt)
        shown = prompt.split('Facts:\n', 1)[1]
        if shown.endswith('\n'):
            shown = shown[:-1]
        self.assertEqual(shown, json.dumps(EXPECTED, ensure_ascii=False, separators=(',', ':')))
        for sentinel in ('PROMPT_SENTINEL_do_not_send', 'DIFF_SENTINEL_do_not_send', 'REPO_SENTINEL_do_not_send'):
            self.assertNotIn(sentinel, prompt)

    def test_unchanged_inputs_do_not_dispatch_and_force_does(self):
        task = self._rich()
        self.arm_reply(REPLY)
        first = self.summarize(self.plan, '--task', 'a', '--json')
        self.assertEqual(first.returncode, 0, first.stderr)
        kept = (task / 'summary.json').read_bytes()
        second = self.summarize(self.plan, '--task', 'a', '--json')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout),
                         {'reason': 'unchanged', 'status': 'skipped', 'task': 'a'})
        self.assertEqual((task / 'summary.json').read_bytes(), kept)
        self.assertEqual(self.side_dirs(), ['1'])
        self.assertEqual(self.calls(), ['qa'])

        forced = self.summarize(self.plan, '--task', 'a', '--force', '--json')
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(json.loads(forced.stdout), {'reason': None, 'status': 'done', 'task': 'a'})
        self.assertEqual(self.side_dirs(), ['1', '2'])
        self.assertEqual(self.calls(), ['qa', 'qa'])
        self.assertEqual(self.summary()['inputs_hash'], hashed(EXPECTED))

    def test_a_changed_verdict_dispatches_once(self):
        task = self._rich()
        self.arm_reply(REPLY)
        first = self.summarize(self.plan, '--task', 'a')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.summary()['inputs_hash'], hashed(EXPECTED))
        self.assertEqual(self.side_dirs(), ['1'])
        (task / 'attempts.tsv').write_text(ATTEMPTS.replace('\t2\tPASS\t', '\t2\tFAIL\t', 1))
        edited = json.loads(json.dumps(EXPECTED))
        edited['attempts'][1]['verdict'] = 'FAIL'
        second = self.summarize(self.plan, '--task', 'a', '--json')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['status'], 'done')
        self.assertEqual(self.side_dirs(), ['1', '2'])
        self.assertEqual(self.calls(), ['qa', 'qa'])
        self.assertEqual(self.summary()['inputs_hash'], hashed(edited))
        self.assertNotEqual(hashed(edited), hashed(EXPECTED))

    def test_a_bad_reply_keeps_the_previous_summary(self):
        task = self._rich()
        self.arm_reply(REPLY)
        good = self.summarize(self.plan, '--task', 'a')
        self.assertEqual(good.returncode, 0, good.stderr)
        kept = (task / 'summary.json').read_bytes()

        def refuse(findings, reason):
            self.arm({'*.qa.*': {'findings': findings, 'verdict': 'none'}})
            result = self.summarize(self.plan, '--task', 'a', '--force', '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {'reason': reason, 'status': 'skipped', 'task': 'a'})
            self.assertEqual((task / 'summary.json').read_bytes(), kept)
            self.assertEqual((task / 'summary.error').read_text(), reason + '\n')

        refuse('\n'.join('line %d' % n for n in range(1, 10)), 'not json')
        refuse('{"text": "one\\ntwo\\nthree"}', 'missing sentence')
        refuse('{"sentence": "only"}', 'missing text')
        nine = '\n'.join('line %d' % n for n in range(1, 10))
        refuse(json.dumps({'text': nine, 'sentence': 'too long'}), 'text over 8 lines')
        # Eight lines is still a summary. The bad reply above did not have to be the last word.
        eight = '\n'.join('line %d' % n for n in range(1, 9))
        self.arm_reply({'text': eight, 'sentence': 'Eight lines still fit.'})
        accepted = self.summarize(self.plan, '--task', 'a', '--force', '--json')
        self.assertEqual(json.loads(accepted.stdout)['status'], 'done')
        self.assertEqual(self.summary()['text'], eight)
        self.assertFalse((task / 'summary.error').exists())
        # The inputs did not change, so the next call leaves that summary where it is.
        dirs = self.side_dirs()
        skipped = self.summarize(self.plan, '--task', 'a', '--json')
        self.assertEqual(json.loads(skipped.stdout),
                         {'reason': 'unchanged', 'status': 'skipped', 'task': 'a'})
        self.assertEqual(self.side_dirs(), dirs)

    def test_a_task_override_skips_only_that_task(self):
        self._fresh('a\t-\tlow\ta.txt\tsol\topus\tA\nb\ta\tlow\tb.txt\tsol\topus\tB\n')
        self._on()
        overridden = self.plan / 'tasks' / 'a'
        overridden.mkdir(parents=True)
        (overridden / 'summaries').write_text('off\n')
        self.arm_reply(REPLY)
        result = self.summarize(self.plan, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(lines, [
            {'reason': 'off', 'status': 'skipped', 'task': 'a'},
            {'reason': None, 'status': 'done', 'task': 'b'},
        ])
        self.assertFalse((self.plan / 'side' / 'summary' / 'a').exists())
        self.assertFalse((overridden / 'summary.json').exists())
        self.assertEqual(self.side_dirs('b'), ['1'])
        self.assertEqual(self.summary('b')['model'], 'sol')
        self.assertEqual(self.calls(), ['qa'])

    def test_facts_longer_than_8kb_are_capped_and_the_hash_is_not(self):
        self._fresh('a\t-\tlow\ta.txt\tsol\topus\t' + ('T' * 20000) + '\n')
        self._on()
        self.arm_reply(REPLY)
        result = self.summarize(self.plan, '--task', 'a', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        full = {
            'id': 'a', 'title': 'T' * 20000, 'status': None, 'attempts': [], 'findings': [],
            'known_issues': [], 'dependencies': [], 'waiting': [],
        }
        self.assertEqual(self.summary()['inputs_hash'], hashed(full))
        shown = (self.plan / 'side' / 'summary' / 'a' / '1' / 'prompt.txt').read_text().split('Facts:\n', 1)[1]
        if shown.endswith('\n'):
            shown = shown[:-1]
        self.assertLessEqual(len(shown), 8 * 1024)
        self.assertTrue(shown.endswith('… (capped)'))
        self.assertNotEqual(shown, json.dumps(full, ensure_ascii=False, separators=(',', ':')))

    def test_a_held_lock_skips_the_task_as_busy(self):
        self._fresh()
        self._on()
        task = self.plan / 'tasks' / 'a'
        task.mkdir(parents=True)
        self.arm_reply(REPLY)
        holder = subprocess.Popen(
            [sys.executable, '-c', HOLDER, str(task / 'summary.lock')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            line = holder.stdout.readline()
            self.assertEqual(line, 'locked\n')
            result = self.summarize(self.plan, '--task', 'a', '--json', timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout),
                             {'reason': 'busy', 'status': 'skipped', 'task': 'a'})
            self.assertFalse((self.plan / 'side' / 'summary').exists())
            self.assertEqual(self.calls(), [])
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.wait(timeout=5)

    def facts(self, task='a'):
        body, sources = summarize_module().collect(self.plan, task)
        return body, sources

    def _archive(self, task, name, text, started, total=None):
        archive = task / 'attempts' / name
        archive.mkdir(parents=True)
        (archive / 'qa.last').write_text(text)
        metrics = {'wall_started': started, 'exit_code': 0}
        if total is not None:
            metrics['elapsed_s'] = {'total': total}
        (archive / 'metrics.json').write_text(json.dumps(metrics) + '\n')
        return archive

    def test_a_fast_review_stays_on_the_attempt_that_logged_it(self):
        # attempts.tsv stores date +%s. A review that starts at 1700000001.2 and
        # finishes at 1700000001.8 is logged as 1700000001, and production
        # archives carry no attempt_now. It belongs to attempt 1, not attempt 2.
        first = '- [P1][CONFIRMED] a.txt:1 — First bug\nFORGE_VERDICT: FAIL\n'
        second = '- [P3][CONFIRMED] a.txt:4 — Second bug\nFORGE_VERDICT: PASS\n'
        for total in (0.6, None):
            with self.subTest(total=total):
                self._fresh(TASKS)
                task = self.plan / 'tasks' / 'a'
                task.mkdir(parents=True)
                (task / 'status').write_text('PASS\n')
                (task / 'attempts.tsv').write_text(
                    '1700000001\t1\tFAIL\t\tsol\n1700000010\t2\tPASS\t\tastra\n')
                (task / 'qa.last').write_text(second)
                self._archive(task, 'qa-fast', first, 1700000001.2, total)
                self._archive(task, 'qa-later', second, 1700000009.2, total)
                body, sources = self.facts()
                self.assertEqual(
                    [(row['findings'], row['worst']) for row in body['attempts']],
                    [(1, 'P1'), (1, 'P3')])
                self.assertEqual(
                    [item['ref'] for item in sources if item['kind'] == 'finding'],
                    ['2:Second bug'])

    def test_attempts_sharing_an_epoch_second_get_no_guessed_review(self):
        # attempts.tsv stores date +%s, so rows 1 and 2 are both 1700000001 and the
        # fraction of a second cannot say whose review is whose. An attempt that stopped
        # before QA (empty diff, backend error) also logs a row and looks reviewed.
        # Nothing in that second is assigned. The finding ref is 0 (unknown) when the
        # live qa.last is one of those reviews. When it is not (the attempt wrote its own),
        # it belongs to the open attempt and attempt 1 keeps nothing taken from it.
        first = '- [P1][CONFIRMED] a.txt:1 — First bug\nFORGE_VERDICT: FAIL\n'
        second = '- [P2][CONFIRMED] a.txt:2 — Second bug\nFORGE_VERDICT: FAIL\n'
        synthetic = '- [P2][CONFIRMED] a.txt:3 — Empty diff\nFORGE_VERDICT: FAIL\n'
        cases = (
            # Attempt 1 reviewed at .8, attempt 2 stopped before QA and wrote its own qa.last.
            ('unreviewed-fail', 'FAIL', ((1700000001.3, 0.5, first),), synthetic, 'FAIL', '2:Empty diff', [(None, None), (1, 'P2')]),
            # Both reviews ran, then attempt 2's backend exited nonzero.
            ('error-after-review', 'ERROR',
             ((1700000000.2, 0.5, first), (1700000001.5, 0.3, second)), second, 'ERROR', '0:Second bug', [(None, None), (None, None)]),
            ('both-reviews', 'FAIL',
             ((1700000001.1, 0.5, first), (1700000001.7, 0.15, second)), second, 'FAIL', '0:Second bug', [(None, None), (None, None)]),
        )
        for name, verdict, archives, live, status, ref, expected in cases:
            with self.subTest(name=name):
                self._fresh(TASKS)
                task = self.plan / 'tasks' / 'a'
                task.mkdir(parents=True)
                (task / 'status').write_text(status + '\n')
                (task / 'attempts.tsv').write_text(
                    '1700000001\t1\tFAIL\t\tsol\n1700000001\t2\t%s\t\tastra\n' % verdict)
                (task / 'qa.last').write_text(live)
                for index, (started, total, text) in enumerate(archives):
                    self._archive(task, 'qa-%d' % index, text, started, total)
                body, sources = self.facts()
                self.assertEqual(
                    [(row['findings'], row['worst']) for row in body['attempts']],
                    expected)
                self.assertEqual(
                    [item['ref'] for item in sources if item['kind'] == 'finding'], [ref])

    def test_an_identical_older_reply_does_not_claim_a_newer_unassigned_review(self):
        # Attempts 2 and 3 share one logged second, so a review that finished there cannot be
        # assigned. Attempt 3's review repeats attempt 1's text exactly, and it is the live
        # qa.last. Its attempt is unknown (ref 0). Attempt 1's identical reply does not take it.
        first = '- [P1][CONFIRMED] a.txt:1 — First bug\nFORGE_VERDICT: FAIL\n'
        self._fresh(TASKS)
        task = self.plan / 'tasks' / 'a'
        task.mkdir(parents=True)
        (task / 'status').write_text('FAIL\n')
        (task / 'attempts.tsv').write_text(
            '1700000001\t1\tFAIL\t\tsol\n1700000010\t2\tFAIL\t\tsol\n1700000010\t3\tFAIL\t\tastra\n')
        (task / 'qa.last').write_text(first)
        self._archive(task, 'qa-1', first, 1700000000.2, 0.5)
        self._archive(task, 'qa-3', first, 1700000009.5, 0.7)
        body, sources = self.facts()
        self.assertEqual(
            [(row['findings'], row['worst']) for row in body['attempts']],
            [(1, 'P1'), (None, None), (None, None)])
        self.assertEqual(
            [item['ref'] for item in sources if item['kind'] == 'finding'], ['0:First bug'])

    def test_a_newer_attempt_keeps_its_ref_over_an_identical_unassigned_older_review(self):
        # Attempts 1 and 2 share one logged second, so attempt 1's review cannot be
        # assigned. Attempt 3 is logged later and its review repeats the same text. It is
        # the live qa.last and the newest review, so the finding ref is 3, not 0.
        first = '- [P1][CONFIRMED] a.txt:1 — First bug\nFORGE_VERDICT: FAIL\n'
        self._fresh(TASKS)
        task = self.plan / 'tasks' / 'a'
        task.mkdir(parents=True)
        (task / 'status').write_text('FAIL\n')
        (task / 'attempts.tsv').write_text(
            '1700000001\t1\tFAIL\t\tsol\n1700000001\t2\tFAIL\t\tsol\n1700000010\t3\tFAIL\t\tastra\n')
        (task / 'qa.last').write_text(first)
        self._archive(task, 'qa-1', first, 1700000000.2, 0.5)
        self._archive(task, 'qa-3', first, 1700000009.5, 0.7)
        body, sources = self.facts()
        self.assertEqual(
            [(row['findings'], row['worst']) for row in body['attempts']],
            [(None, None), (None, None), (1, 'P1')])
        self.assertEqual(
            [item['ref'] for item in sources if item['kind'] == 'finding'], ['3:First bug'])

    def test_a_review_archived_before_its_row_keeps_the_open_attempt(self):
        # Summarise after attempt 2's review is archived and before record_attempt
        # appends its row. The counter already says 2. The new findings must not
        # replace attempt 1's, and the finding ref is 2, not 1.
        first = '- [P1][CONFIRMED] a.txt:1 — First bug\nFORGE_VERDICT: FAIL\n'
        second = '- [P2][CONFIRMED] a.txt:2 — Second bug\nFORGE_VERDICT: FAIL\n'
        cases = (
            ('later', 1700000005.2, 0.5, None),
            # Appended at .1, so a start at .5 is the next attempt even though
            # both sit in the logged second. One case has no finish time.
            ('same-second', 1700000001.5, None, 1700000001.1),
            ('written-earlier', 1700000001.5, 0.3, 1700000001.1),
        )
        for name, started, total, written in cases:
            with self.subTest(name=name):
                self._fresh('a\t-\tlow\ta.txt\tsol\topus\tA\n')
                task = self.plan / 'tasks' / 'a'
                task.mkdir(parents=True)
                (task / 'status').write_text('FAIL\n')
                (task / 'attempt').write_text('2\n')
                log = task / 'attempts.tsv'
                log.write_text('1700000001\t1\tFAIL\t\tsol\n')
                if written is not None:
                    os.utime(log, (written, written))
                (task / 'qa.last').write_text(second)
                self._archive(task, 'qa-old', first, 1700000000.2, 0.5)
                self._archive(task, 'qa-new', second, started, total)
                body, sources = self.facts()
                self.assertEqual(body['attempts'][0]['findings'], 1)
                self.assertEqual(body['attempts'][0]['worst'], 'P1')
                self.assertEqual(
                    [item['ref'] for item in sources if item['kind'] == 'finding'],
                    ['2:Second bug'])


if __name__ == '__main__':
    unittest.main()
