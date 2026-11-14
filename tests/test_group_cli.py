"""forge group show and forge group retry (#140).

Fixture plan dirs with canned group files. Retry of an ambiguous decision runs the real
`_group_settle`; that path needs the group's review files and the member task dirs, not a git repo.
"""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
FORGE = str(ROOT / 'forge')

HEADER = '# id\treview\tqa\tmembers\ttitle\tgoal\n'
TASKS = (
    '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
    'a\t-\tlow\ta.txt\tsol\topus\tA\n'
    'b\t-\tlow\tb.txt\tsol\topus\tB\n'
    'c\t-\tlow\tc.txt\tsol\topus\tC\n'
    'd\t-\tlow\td.txt\tsol\topus\tD\n'
    'e\t-\tlow\te.txt\tsol\topus\tE\n'
)

P1_B = '- [P1][CONFIRMED] b.txt:1 — b is broken {reach=realistic origin=new evidence=traced}'
P1_X = '- [P1][CONFIRMED] elsewhere/x.py:4 — breaks something {reach=main origin=new evidence=traced}'
QA_AMBIGUOUS = 'The review of g1.\n\n%s\n%s\n\nFORGE_VERDICT: FAIL\n' % (P1_B, P1_X)
ATTRIBUTION = {
    'round': 1,
    'findings': [
        {'n': 1, 'severity': 'P1', 'status': 'CONFIRMED', 'file': 'b.txt', 'line': 1,
         'tasks': ['b'], 'blocking': True, 'ambiguous': False},
        {'n': 2, 'severity': 'P1', 'status': 'CONFIRMED', 'file': 'elsewhere/x.py', 'line': 4,
         'tasks': [], 'blocking': True, 'ambiguous': True},
    ],
    'implicated': ['b'],
    'ambiguous': [2],
}

SHOW_TEXT = """\
gp  review=group  qa=opus:high
  title: Passing
  goal: Ship it
  members:
    a  MERGED
  round: 1
  status: PASS
  implicated: -
  merged: yes

gf  review=group  qa=astra:xhigh
  title: Failing
  goal: Fix the break
  members:
    b  FAIL
    c  FAIL
  round: 2
  status: FAIL
  implicated: b, c

gd  review=group  qa=-
  title: Decide
  goal: Name who fixes it
  members:
    d  IMPLEMENTED
  round: 1
  status: NEEDS_DECISION
  implicated: -
  decision: reasons: ambiguous

gt  review=task  qa=-
  title: Alone
  goal: Each task is reviewed alone
  members:
    e  PASS
"""

SHOW_JSON = [
    {
        'id': 'gp', 'review': 'group', 'qa': 'opus:high', 'title': 'Passing', 'goal': 'Ship it',
        'members': [{'id': 'a', 'status': 'MERGED'}],
        'round': 1, 'status': 'PASS', 'implicated': [], 'decision': None, 'merged': True,
    },
    {
        'id': 'gf', 'review': 'group', 'qa': 'astra:xhigh', 'title': 'Failing', 'goal': 'Fix the break',
        'members': [{'id': 'b', 'status': 'FAIL'}, {'id': 'c', 'status': 'FAIL'}],
        'round': 2, 'status': 'FAIL', 'implicated': ['b', 'c'], 'decision': None, 'merged': False,
    },
    {
        'id': 'gd', 'review': 'group', 'qa': '-', 'title': 'Decide', 'goal': 'Name who fixes it',
        'members': [{'id': 'd', 'status': 'IMPLEMENTED'}],
        'round': 1, 'status': 'NEEDS_DECISION', 'implicated': [], 'decision': 'reasons: ambiguous',
        'merged': False,
    },
    {
        'id': 'gt', 'review': 'task', 'qa': '-', 'title': 'Alone', 'goal': 'Each task is reviewed alone',
        'members': [{'id': 'e', 'status': 'PASS'}],
    },
]


class GroupCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.plan = (Path(tmp.name) / 'plan').resolve()
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text(TASKS)
        for task in 'abcde':
            (self.plan / 'tasks' / task).mkdir()
        self.env = dict(os.environ, FORGE_DATASET='off')

    def group(self, *args, timeout=60):
        return subprocess.run([FORGE, 'group', *(str(a) for a in args)],
                              capture_output=True, text=True, env=self.env, timeout=timeout)

    def put(self, rel, text):
        path = self.plan / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def status(self, task):
        path = self.plan / 'tasks' / task / 'status'
        return path.read_text().strip() if path.exists() else None

    def log(self):
        path = self.plan / 'control.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def write_show_plan(self):
        self.put('tasks/a/status', 'PASS\n')
        self.put('tasks/a/merged', '')
        self.put('tasks/b/status', 'FAIL\n')
        self.put('tasks/c/status', 'FAIL\n')
        self.put('tasks/d/status', 'IMPLEMENTED\n')
        self.put('tasks/e/status', 'PASS\n')
        self.put('groups.tsv', HEADER
                 + 'gp\tgroup\topus:high\ta\tPassing\tShip it\n'
                 + 'gf\tgroup\tastra:xhigh\tb,c\tFailing\tFix the break\n'
                 + 'gd\tgroup\t-\td\tDecide\tName who fixes it\n'
                 + 'gt\ttask\t-\te\tAlone\tEach task is reviewed alone\n')
        self.put('groups/gp/status', 'PASS\n')
        self.put('groups/gp/round', '1\n')
        self.put('groups/gp/rounds.tsv', '1\t10\tPASS\t-\topus:high\n')
        self.put('groups/gp/merged', '')
        self.put('groups/gf/status', 'FAIL\n')
        self.put('groups/gf/round', '2\n')
        self.put('groups/gf/rounds.tsv', '1\t10\tFAIL\tb\topus\n2\t20\tFAIL\tb,c\topus\n')
        self.put('groups/gd/status', 'NEEDS_DECISION\n')
        self.put('groups/gd/round', '1\n')
        self.put('groups/gd/rounds.tsv', '1\t10\tNEEDS_DECISION\t-\t-\n')
        self.put('groups/gd/decision.md', 'reasons: ambiguous\n\nGroup gd stopped for your decision.\n')

    def write_review_group(self, status, decision=None, gid='g1'):
        for task in 'abc':
            self.put(f'tasks/{task}/status', 'IMPLEMENTED\n')
        self.put('groups.tsv', HEADER + 'g1\tgroup\topus:high\ta,b,c\tThree\tDo all three\n')
        self.put(f'groups/{gid}/status', status + '\n')
        self.put(f'groups/{gid}/round', '1\n')
        self.put(f'groups/{gid}/retries.used', '4\n')
        self.put(f'groups/{gid}/rounds.tsv', '1\t10\t' + status + '\t-\topus:high\n')
        if decision is not None:
            self.put(f'groups/{gid}/decision.md', decision)

    def write_ambiguous(self):
        self.write_review_group('NEEDS_DECISION', 'reasons: ambiguous\n\nChoices:\n')
        self.put('groups/g1/qa.last', QA_AMBIGUOUS)
        self.put('groups/g1/attribution.json', json.dumps(ATTRIBUTION))
        self.put('groups/g1/qa.gate', 'P2\tkeep\tP1\t1\t0\t0\n')

    # --- launcher ---------------------------------------------------------------------------------

    def test_forge_help_lists_group(self):
        for args in (['help'], ['--help']):
            with self.subTest(args=args):
                listed = subprocess.run([FORGE, *args], capture_output=True, text=True, env=self.env)
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertRegex(listed.stdout, r'(?m)^  group  ')
        helped = subprocess.run([FORGE, 'group', '--help'], capture_output=True, text=True, env=self.env)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn('usage: forge group', helped.stdout)
        self.assertIn('show', helped.stdout)
        self.assertIn('retry', helped.stdout)
        bare = subprocess.run([FORGE, 'group'], capture_output=True, text=True, env=self.env)
        self.assertEqual((bare.returncode, bare.stdout), (2, ''))
        self.assertIn('usage: forge group', bare.stderr)

    # --- show -------------------------------------------------------------------------------------

    def test_show_text_and_json_for_each_kind_of_group(self):
        self.write_show_plan()
        text = self.group('show', self.plan)
        self.assertEqual(text.returncode, 0, text.stderr)
        self.assertEqual(text.stdout, SHOW_TEXT)
        self.assertNotIn('Group gd stopped', text.stdout)
        data = self.group('show', self.plan, '--json')
        self.assertEqual(data.returncode, 0, data.stderr)
        self.assertEqual(json.loads(data.stdout), SHOW_JSON)
        self.assertNotIn('round', SHOW_JSON[3])
        one = self.group('show', self.plan, '--group', 'gf', '--json')
        self.assertEqual(json.loads(one.stdout), [SHOW_JSON[1]])
        self.assertEqual(self.group('show', self.plan, '--group', 'gf').stdout,
                         '\n\n'.join(SHOW_TEXT.split('\n\n')[1:2]) + '\n')

    def test_show_without_groups_tsv_exits_3(self):
        missing = self.group('show', self.plan)
        self.assertEqual(missing.returncode, 3, missing.stderr)
        self.assertIn('groups.tsv', missing.stderr)
        self.assertEqual(missing.stdout, '')
        self.assertTrue(missing.stderr.startswith('forge group: '))
        absent = self.group('show', self.plan.parent / 'no-plan')
        self.assertEqual(absent.returncode, 3, absent.stderr)
        self.assertIn('groups.tsv', absent.stderr)
        self.assertEqual(absent.stdout, '')

    def test_show_of_an_unknown_group_exits_2(self):
        self.write_show_plan()
        result = self.group('show', self.plan, '--group', 'nope')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('nope', result.stderr)
        self.assertEqual(result.stdout, '')

    # --- retry ------------------------------------------------------------------------------------

    def test_retry_of_an_ambiguous_group_fails_the_named_members(self):
        self.write_ambiguous()
        result = self.group('retry', self.plan, 'g1', '--tasks', 'c', '--reason', 'c owns the stray finding')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # c takes the stray finding; b goes back too, for the finding the review attributed to it.
        self.assertEqual([self.status(task) for task in 'abc'], ['IMPLEMENTED', 'FAIL', 'FAIL'])
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'FAIL')
        self.assertFalse((self.plan / 'groups' / 'g1' / 'decision.md').exists())
        self.assertIn('forge-parallel.sh run ', result.stdout)
        self.assertIn('--retry-failed', result.stdout)
        self.assertIn(str(self.plan), result.stdout)
        entry = self.log()[-1]
        self.assertEqual(entry['actor'], 'cli')
        self.assertEqual(entry['action'], 'retry')
        self.assertEqual(entry['key'], 'g1')
        self.assertEqual(entry['previous'], 'NEEDS_DECISION')
        self.assertEqual(entry['value'], 'c')
        self.assertEqual(entry['reason'], 'c owns the stray finding')
        self.assertIsInstance(entry['at'], float)

    def test_retry_of_an_ambiguous_group_on_a_live_run_names_the_sweep(self):
        self.write_ambiguous()
        self.put('scheduler.live', json.dumps({'pid': os.getpid(), 'started': time.time()}))
        self.put('retry_budget', '2\n')
        result = self.group('retry', self.plan, 'g1', '--tasks', 'b,c')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([self.status(task) for task in 'abc'], ['IMPLEMENTED', 'FAIL', 'FAIL'])
        self.assertIn('--retry-failed 2', result.stdout)
        self.assertIn('sweep', result.stdout)
        self.assertIn('every 1s', result.stdout)
        self.assertIn('budget left', result.stdout)
        self.assertEqual(self.log()[-1]['value'], 'b,c')

    def test_retry_of_an_ambiguous_group_without_tasks_refuses(self):
        self.write_ambiguous()
        before = [self.status(task) for task in 'abc']
        result = self.group('retry', self.plan, 'g1')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('--tasks', result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual([self.status(task) for task in 'abc'], before)
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'NEEDS_DECISION')
        self.assertEqual(self.log(), [])

    def test_retry_of_an_oversized_group_refuses(self):
        self.write_review_group('NEEDS_DECISION', 'reasons: oversized\n')
        result = self.group('retry', self.plan, 'g1', '--tasks', 'a')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('groups.tsv', result.stderr)
        self.assertIn('plan', result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'NEEDS_DECISION')
        self.assertEqual([self.status(task) for task in 'abc'], ['IMPLEMENTED', 'IMPLEMENTED', 'IMPLEMENTED'])
        self.assertEqual(self.log(), [])

    def test_retry_of_unknown_infra_timeout_interrupted_and_error_resets_to_interrupted(self):
        self.write_review_group('UNKNOWN')
        for status in ('UNKNOWN', 'INFRA', 'TIMEOUT', 'INTERRUPTED', 'ERROR'):
            with self.subTest(status=status):
                self.put('groups/g1/status', status + '\n')
                result = self.group('retry', self.plan, 'g1', '--reason', 'again')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'INTERRUPTED')
                self.assertIn('no member attempt was spent', result.stdout)
                self.assertEqual([self.status(task) for task in 'abc'], ['IMPLEMENTED'] * 3)
                self.assertEqual((self.plan / 'groups' / 'g1' / 'retries.used').read_text(), '4\n')
                self.assertEqual((self.plan / 'groups' / 'g1' / 'round').read_text(), '1\n')
                entry = self.log()[-1]
                self.assertEqual((entry['actor'], entry['action'], entry['key'], entry['value'], entry['previous']),
                                 ('cli', 'retry', 'g1', 'INTERRUPTED', status))
                self.assertEqual(entry['reason'], 'again')

    def test_retry_with_tasks_does_not_rearm(self):
        self.write_review_group('UNKNOWN')
        result = self.group('retry', self.plan, 'g1', '--tasks', 'a')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('--tasks', result.stderr)
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'UNKNOWN')
        self.assertEqual(self.log(), [])

    def test_retry_of_a_passing_group_refuses(self):
        self.write_review_group('PASS')
        result = self.group('retry', self.plan, 'g1')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('PASS', result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'PASS')
        self.assertEqual(self.log(), [])

    def test_retry_of_a_conflict_says_to_fix_one_member(self):
        self.write_review_group('CONFLICT')
        result = self.group('retry', self.plan, 'g1')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("members' branches conflict", result.stderr)
        self.assertIn('forge-parallel.sh retry', result.stderr)
        self.assertIn(str(self.plan), result.stderr)
        self.assertIn('re-reviewed', result.stderr)
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'CONFLICT')

    def test_retry_of_a_merged_group_refuses(self):
        self.write_review_group('PASS')
        self.put('groups/g1/merged', '')
        result = self.group('retry', self.plan, 'g1')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('merged', result.stderr)
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'PASS')
        self.assertEqual(self.log(), [])

    def test_retry_of_an_unknown_group_exits_2(self):
        self.write_review_group('UNKNOWN')
        result = self.group('retry', self.plan, 'nope')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('nope', result.stderr)
        self.assertTrue(result.stderr.startswith('forge group: '))
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'UNKNOWN')
        self.assertEqual(self.log(), [])

    def test_retry_of_a_task_review_group_refuses(self):
        self.put('groups.tsv', HEADER + 'gt\ttask\t-\ta,b\tAlone\tEach\n')
        self.put('tasks/a/status', 'FAIL\n')
        result = self.group('retry', self.plan, 'gt')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('review=task', result.stderr)
        self.assertEqual(self.status('a'), 'FAIL')
        self.assertEqual(self.log(), [])

    def test_retry_of_a_bad_attribution_changes_nothing_and_logs_nothing(self):
        self.write_ambiguous()
        self.put('groups/g1/qa.last', 'no findings here\n\nFORGE_VERDICT: FAIL\n')
        result = self.group('retry', self.plan, 'g1', '--tasks', 'c')
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual([self.status(task) for task in 'abc'], ['IMPLEMENTED'] * 3)
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'NEEDS_DECISION')
        self.assertEqual(self.log(), [])

    def test_retry_takes_the_control_lock(self):
        self.write_review_group('UNKNOWN')
        lock = open(self.plan / 'control.lock', 'a')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = self.group('retry', self.plan, 'g1', timeout=20)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn('control lock', result.stderr)
        self.assertEqual((self.plan / 'groups' / 'g1' / 'status').read_text().strip(), 'UNKNOWN')
        self.assertEqual(self.log(), [])


if __name__ == '__main__':
    unittest.main()
