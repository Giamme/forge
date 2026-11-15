"""A scripted task runs its command, commits the result and is reviewed as a script (#142).

A tasks.tsv row whose dwarf cell is `script` runs the executable tasks/<id>/run in its worktree, from
the task's base, instead of dispatching an implementer. Cases drive forge-parallel.sh with the fake agent
CLI from test_forge.py, as test_outside_stop.py does; the fake CLI's calls tell a model dispatch from a
review. Escalation, the dataset and forge-ui for script tasks are not part of this.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'scripts' / 'forge-contract.py'
SCRIPT_INSTRUCTION = 'This diff was produced by the command below, not written by hand.'
WRITES_A = '#!/bin/sh\necho "generated line" > a.txt\necho "script says hello"\n'


def contract(*args):
    return subprocess.run(['python3', str(CONTRACT), 'qa', *args], capture_output=True, text=True, check=True).stdout


class ScriptRunCase(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, body, timeout=None, with_b=False):
        """a is a scripted task running `body`; with_b adds b, a model task that depends on a."""
        p = self.root / 'plan'; p.mkdir()
        (p / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tscript\topus\tA\n'
                                     + ('b\ta\tlow\tb.txt\tsol\topus\tB\n' if with_b else ''))
        for i in ('ab' if with_b else 'a'):
            (p / 'tasks' / i).mkdir(parents=True)
            (p / 'tasks' / i / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        run = p / 'tasks' / 'a' / 'run'
        run.write_text(body)
        run.chmod(0o755)
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        if timeout is not None:
            (p / 'timeout').write_text('%s\n' % timeout)
        return p

    def read(self, p, name, task='a'):
        return (p / 'tasks' / task / name).read_text()

    def status(self, p, task='a'):
        return self.read(p, 'status', task).strip()

    def branch_file(self, p, path, task='a'):
        """A file as the task's branch committed it (a merged task's worktree is gone)."""
        return self.git('show', 'forge/%s/%s:%s' % ((p / 'run_id').read_text().strip(), task, path)).decode()

    def attempt_records(self, p):
        return sorted((p / 'tasks' / 'a' / 'attempts').glob('script-*'))

    def only_a(self, p, *flags):
        return self.run_script('forge-parallel.sh', 'run', p, *flags)


class RunTests(ScriptRunCase):
    def test_exit_zero_commits_and_is_reviewed_as_a_script(self):
        p = self.plan_of(WRITES_A, with_b=True)
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertEqual(self.branch_file(p, 'a.txt'), 'generated line\n')
        # a: no model dispatch, one review. b is a model task: its own dispatch and review.
        self.assertEqual(self.calls(), ['qa', 'dwarf', 'qa'])
        qa = self.read(p, 'qa.input')
        self.assertIn(SCRIPT_INSTRUCTION, qa)
        self.assertIn('The command (tasks/a/run):\n```\n' + WRITES_A + '```\n', qa)
        self.assertIn('Its output (last 3000 characters of script.log):\n```\nscript says hello\n```\n', qa)
        self.assertLess(qa.index(SCRIPT_INSTRUCTION), qa.index('The command (tasks/a/run):'))
        self.assertLess(qa.index('Its output'), qa.index('```diff'))
        self.assertIn('{outside=', qa)
        self.assertNotIn(SCRIPT_INSTRUCTION, self.read(p, 'qa.input', 'b'))
        self.assertNotIn('The command (tasks/', self.read(p, 'qa.input', 'b'))
        subject = self.git('log', '--format=%s', '-1', 'forge/%s/a' % (p / 'run_id').read_text().strip()).decode().strip()
        self.assertEqual(subject, 'forge(a) attempt 1: A (script)')
        self.assertEqual(self.read(p, 'script.log'), 'script says hello\n')
        (record,) = self.attempt_records(p)
        self.assertRegex(record.name, r'^script-[0-9a-f]{16}$')
        metrics = json.loads((record / 'metrics.json').read_text())
        self.assertEqual((metrics['role'], metrics['harness'], metrics['exit_code']), ('script', 'script', 0))
        self.assertIsInstance(metrics['wall_started'], float)
        self.assertGreaterEqual(metrics['elapsed_s']['total'], 0)
        self.assertEqual((record / 'script.log').read_text(), 'script says hello\n')
        self.assertEqual((record / 'run').read_text(), WRITES_A)
        # a model task's dispatch input has no script wording
        self.assertNotIn('script', self.read(p, 'dwarf.input', 'b').lower().split('requirements')[0])

    def test_the_command_gets_its_environment_and_no_stdin(self):
        p = self.plan_of('#!/bin/sh\n{ echo "$FORGE_TASK_ID"; echo "$FORGE_PLAN"; pwd -P; echo "$FORGE_WORKTREE";'
                         ' echo "$FORGE_BASE_REF"; cat; } > a.txt\n')
        self.assertEqual(self.only_a(p).returncode, 0)
        base = self.read(p, 'base_ref').strip()
        wt = (p / 'wt_root').read_text().strip() + '/a'
        lines = self.branch_file(p, 'a.txt').splitlines()
        self.assertEqual(lines[0], 'a')
        self.assertEqual(lines[1], str(p))
        self.assertEqual(os.path.realpath(lines[2]), os.path.realpath(wt))
        self.assertEqual(lines[3], wt)
        self.assertEqual(lines[4], base)
        self.assertEqual(len(lines), 5)          # stdin was empty

    def test_a_file_without_a_shebang_runs_under_bash(self):
        p = self.plan_of('x=(one two)\necho "${x[1]}" > a.txt\n')
        self.assertEqual(self.only_a(p).returncode, 0)
        self.assertEqual(self.branch_file(p, 'a.txt'), 'two\n')

    def test_a_nonzero_exit_fails_without_a_review_and_without_noretry(self):
        p = self.plan_of('#!/bin/sh\necho partial > a.txt\necho "boom: cannot continue" >&2\nexit 3\n')
        r = self.only_a(p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.status(p), 'FAIL')
        self.assertEqual(self.calls(), [])
        last = self.read(p, 'qa.last')
        self.assertTrue(last.startswith('The script exited 3'), last)
        self.assertIn('```\nboom: cannot continue\n```', last)
        self.assertFalse((p / 'tasks/a/noretry').exists())
        self.assertFalse((p / 'tasks/a/reviewed.commit').exists())
        # the worktree is left as the command left it, uncommitted
        wt = Path((p / 'wt_root').read_text().strip() + '/a')
        self.assertEqual((wt / 'a.txt').read_text(), 'partial\n')
        self.assertIn('a.txt', subprocess.check_output(['git', 'status', '--porcelain'], cwd=wt, text=True))
        (record,) = self.attempt_records(p)
        self.assertEqual(json.loads((record / 'metrics.json').read_text())['exit_code'], 3)

    def test_only_the_last_3000_characters_of_the_output_are_kept(self):
        p = self.plan_of('#!/bin/sh\nawk \'BEGIN { for (i = 0; i < 400; i++) print "line-" i }\'\nexit 1\n')
        self.only_a(p)
        last = self.read(p, 'qa.last')
        self.assertIn('line-399', last)
        self.assertNotIn('line-0\n', last)
        body = last.split('```\n')[1]
        self.assertLessEqual(len(body), 3001)

    def test_a_timeout_stops_the_process_group(self):
        marker = self.root / 'child.pid'
        p = self.plan_of('#!/bin/sh\n(sleep 300 & echo $! > "%s"; wait) &\necho started\nsleep 300\n' % marker, timeout=2)
        began = time.time()
        r = self.only_a(p)
        self.assertLess(time.time() - began, 60)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.status(p), 'FAIL')
        self.assertEqual(self.calls(), [])
        last = self.read(p, 'qa.last')
        self.assertIn('timed out after 2s', last)
        self.assertIn('started', last)
        self.assertFalse((p / 'tasks/a/noretry').exists())
        (record,) = self.attempt_records(p)
        metrics = json.loads((record / 'metrics.json').read_text())
        self.assertIsNone(metrics['exit_code'])
        self.assertEqual(metrics['role'], 'script')
        child = int(marker.read_text())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_an_empty_result_stays_a_failure_that_is_not_retried(self):
        p = self.plan_of('#!/bin/sh\nexit 0\n')
        r = self.only_a(p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.status(p), 'FAIL')
        self.assertEqual(self.read(p, 'qa.last').strip(), 'The script produced no changes.')
        self.assertTrue((p / 'tasks/a/noretry').exists())
        self.assertEqual(self.calls(), [])


class RetryTests(ScriptRunCase):
    def test_a_retry_starts_from_the_base_so_the_diff_repeats(self):
        p = self.plan_of('#!/bin/sh\necho "one more line" >> base.txt\n')
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] base.txt:2 — wrong'}})
        r = self.only_a(p, '--retry-failed', 2)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.read(p, 'attempt').strip(), '2')
        # appended once, not twice: the second run began from the base, so its diff is the first's
        self.assertEqual(self.read(p, 'changes.diff'), self.read(p, 'changes.prev.diff'))
        self.assertIn('The retry changed nothing', self.read(p, 'qa.last'))
        self.assertTrue((p / 'tasks/a/noretry').exists())
        self.assertEqual(self.calls(), ['qa'])
        self.assertEqual(len(self.attempt_records(p)), 2)
        self.assertEqual(self.branch_file(p, 'base.txt'), 'base\none more line\n')

    def test_a_retry_drops_what_the_previous_run_left_untracked(self):
        counter = self.root / 'count'
        p = self.plan_of('#!/bin/sh\nn=$(cat "%s" 2>/dev/null || echo 0); echo $((n + 1)) > "%s"\n'
                         'echo "attempt $n" > a.txt\n[ "$n" = 0 ] && touch leftover.tmp\nexit 0\n' % (counter, counter))
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — wrong'}})
        self.only_a(p, '--retry-failed', 2)
        self.assertEqual(self.branch_file(p, 'a.txt'), 'attempt 1\n')
        self.assertNotIn('leftover.tmp', self.git('ls-tree', '-r', '--name-only',
                                                  'forge/%s/a' % (p / 'run_id').read_text().strip()).decode())
        self.assertNotIn('leftover.tmp', self.read(p, 'changes.diff'))
        self.assertTrue((p / 'tasks/a/merged').exists())

    def test_failures_never_count_as_converging(self):
        # The same reviews on a model task stop it as NEEDS_DECISION: converging (test_needs_decision.py).
        counter = self.root / 'count'
        p = self.plan_of('#!/bin/sh\nn=$(cat "%s" 2>/dev/null || echo 0); echo $((n + 1)) > "%s"\necho "v$n" > a.txt\n'
                         % (counter, counter))
        (p / 'converge_window').write_text('2\n')
        moving = ['- [P1][CONFIRMED] alpha.py:1 — frobnicator leaks sockets',
                  '- [P1][CONFIRMED] beta.py:2 — widget tolerance computed wrongly',
                  '- [P1][CONFIRMED] gamma.py:3 — scheduler ignores cancellation']
        self.scenario({'a.qa.%d' % n: {'verdict': 'FAIL', 'findings': moving[n - 1]} for n in (1, 2, 3)})
        self.only_a(p, '--retry-failed', 2)
        decision = p / 'tasks/a/decision.md'
        text = decision.read_text() if decision.exists() else ''
        self.assertNotIn('converging', text)
        self.assertEqual(self.read(p, 'attempt').strip(), '3')
        self.assertEqual(self.calls(), ['qa', 'qa', 'qa'])


class ContractTests(unittest.TestCase):
    def test_the_script_head_adds_the_instruction_to_the_task_head(self):
        head = contract('--part', 'head', '--kind', 'script')
        task = contract('--part', 'head', '--kind', 'task')
        self.assertTrue(head.startswith(task))
        self.assertIn(SCRIPT_INSTRUCTION, head)
        self.assertIn('sample it,', head)
        self.assertNotIn(SCRIPT_INSTRUCTION, task)

    def test_the_script_tail_is_the_task_tail(self):
        for threshold in ('P2', 'P0', 'none'):
            tail = contract('--part', 'tail', '--kind', 'script', '--threshold', threshold)
            self.assertEqual(tail, contract('--part', 'tail', '--kind', 'task', '--threshold', threshold))
        self.assertIn('{outside=', tail)


if __name__ == '__main__':
    unittest.main()
