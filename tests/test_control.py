"""`forge control`: show and change a plan's live controls, under the control lock, with an audit log."""
import dataset_isolation  # noqa: F401  keeps forge's dataset and config out of the developer's home
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/forge-control.py'

TASKS = ('# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'
         'a\t-\thigh\tsrc\tsol:high:codex\tastra:xhigh:codex\tFirst\n'
         'b\ta\tlow\tsrc\tsol:high:codex\t-\tSecond\n')

# Succeeds for any spec but `bad:spec:x`, and records how it was called.
STUB = textwrap.dedent('''\
    #!/bin/sh
    echo "$@" >> "$0.calls"
    [ "$2" = bad:spec:x ] && { echo "stub: bad:spec:x is not a pairing" >&2; exit 1; }
    exit 0
    ''')


class ControlTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.plan = self.base / 'plan'
        (self.plan / 'tasks').mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text(TASKS)
        self.stub = self.base / 'doctor.sh'
        self.stub.write_text(STUB); self.stub.chmod(0o755)
        self.env = dict(os.environ, FORGE_DATASET='off', FORGE_CONTROL_DOCTOR=str(self.stub))

    def run_control(self, *args, env=None, timeout=60):
        return subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
                              env=env or self.env, timeout=timeout)

    def ok(self, *args):
        result = self.run_control(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def code(self, expected, *args):
        result = self.run_control(*args)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertTrue(result.stderr.startswith('forge control: '), result.stderr)
        return result

    def show(self):
        return json.loads(self.ok('show', self.plan, '--json').stdout)

    def log(self):
        path = self.plan / 'control.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def put(self, rel, text):
        path = self.plan / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def live(self, pid=None):
        self.put('scheduler.live', json.dumps({'pid': os.getpid() if pid is None else pid, 'started': time.time()}))

    def files(self, root):
        return sorted(str(p.relative_to(root)) for p in Path(root).rglob('*'))

    # ------------------------------------------------------------------------------------ show

    def test_show_reports_overrides_pin_retries_log_and_null_budgets_when_not_live(self):
        self.put('run_id', 'r-7\n')
        self.put('qa_threshold', 'P2')
        self.put('timeout', '90')
        self.put('retry_budget', '4')   # left over from a finished run: not live, so not reported
        self.put('infra_budget', '1')
        self.put('tasks/a/qa_threshold', 'none')
        self.put('tasks/a/retry_budget', '2')
        self.put('tasks/a/retries.used', '1')
        self.put('tasks/a/escalation.pin', 'sol:xhigh:codex\n')
        self.put('tasks/a/escalation.stop', '3\tsol:high:codex\n')
        self.put('tasks/a/escalation.next', 'sol:xhigh:codex\n')
        self.put('tasks/a/status', 'FAIL\n')
        self.put('tasks/b/merged', '')
        self.put('control.log', json.dumps({'at': 1.0, 'actor': 'cli', 'task': None}) + '\nnot json\n'
                 + json.dumps({'at': 2.0, 'actor': 'ui', 'task': 'a'}) + '\n')
        shown = self.show()
        self.assertEqual((shown['plan'], shown['run_id'], shown['live'], shown['fractal']),
                         (str(self.plan), 'r-7', False, False))
        self.assertEqual(shown['run'], dict(qa_threshold='P2', timeout=90, escalate_after=3, verify_retries=1,
                                            retry_budget=None, infra_budget=None))
        self.assertEqual(list(shown['tasks']), ['a', 'b'])
        self.assertEqual(shown['tasks']['a'], dict(
            status='FAIL', dwarf='sol:high:codex', qa='astra:xhigh:codex', qa_threshold='none', retry_budget=2,
            retries_used=1, pin='sol:xhigh:codex', escalation_stop=True, escalation_next='sol:xhigh:codex'))
        self.assertEqual(shown['tasks']['b'], dict(
            status='MERGED', dwarf='sol:high:codex', qa=None, qa_threshold=None, retry_budget=None, retries_used=0,
            pin=None, escalation_stop=False, escalation_next=None))
        self.assertEqual([e['at'] for e in shown['log']], [2.0, 1.0])   # newest first, the bad line skipped

    def test_show_keeps_only_the_last_twenty_log_entries(self):
        self.put('control.log', ''.join(json.dumps({'at': float(i)}) + '\n' for i in range(30)))
        self.assertEqual([e['at'] for e in self.show()['log']], [float(i) for i in range(29, 9, -1)])

    def test_show_reports_budgets_when_live(self):
        self.live()
        self.put('retry_budget', '3'); self.put('infra_budget', '5')
        run = self.show()['run']
        self.assertEqual((run['retry_budget'], run['infra_budget']), (3, 5))

    def test_show_table_lists_the_tasks(self):
        out = self.ok('show', self.plan).stdout
        self.assertIn('live no', out)
        self.assertIn('sol:high:codex', out)
        self.assertEqual([line.split()[0] for line in out.splitlines() if line.startswith('  ') and line.split()[0] in 'ab'],
                         ['a', 'b'])

    def test_a_directory_without_tasks_tsv_is_not_a_plan(self):
        (self.base / 'empty').mkdir()
        self.code(2, 'show', self.base / 'empty')
        self.code(2, 'set', self.base / 'empty', 'timeout', '5')

    # ------------------------------------------------------------------------------------ set

    def test_qa_threshold_at_plan_and_task_level_and_the_log_keeps_previous(self):
        self.ok('set', self.plan, 'qa-threshold', 'P2')
        self.assertEqual((self.plan / 'qa_threshold').read_text().strip(), 'P2')
        self.ok('set', self.plan, 'qa-threshold', 'P1', '--actor', 'ui')
        self.ok('set', self.plan, 'qa-threshold', 'none', '--task', 'a')
        self.assertEqual((self.plan / 'tasks/a/qa_threshold').read_text().strip(), 'none')
        self.assertEqual(self.show()['tasks']['a']['qa_threshold'], 'none')
        self.ok('set', self.plan, 'qa-threshold', 'inherit', '--task', 'a')
        self.assertFalse((self.plan / 'tasks/a/qa_threshold').exists())
        self.ok('set', self.plan, 'qa-threshold', 'P3', '--task', 'b')
        self.ok('set', self.plan, 'qa-threshold', 'none')
        self.assertFalse((self.plan / 'qa_threshold').exists())
        rows = [(e['actor'], e['action'], e['task'], e['key'], e['value'], e['previous']) for e in self.log()]
        self.assertEqual(rows, [('cli', 'set', None, 'qa-threshold', 'P2', None),
                                ('ui', 'set', None, 'qa-threshold', 'P1', 'P2'),
                                ('cli', 'set', 'a', 'qa-threshold', 'none', None),
                                ('cli', 'set', 'a', 'qa-threshold', 'inherit', 'none'),
                                ('cli', 'set', 'b', 'qa-threshold', 'P3', None),
                                ('cli', 'set', None, 'qa-threshold', 'none', 'P1')])
        self.assertTrue(all(isinstance(e['at'], float) for e in self.log()))

    def test_plan_level_numbers_are_written_and_validated(self):
        for key, name in (('timeout', 'timeout'), ('escalate-after', 'escalate_after'), ('verify-retries', 'verify_retries')):
            self.ok('set', self.plan, key, '007')
            self.assertEqual((self.plan / name).read_text().strip(), '7')
        self.assertEqual(self.show()['run']['verify_retries'], 7)
        for key, value in (('timeout', '-1'), ('timeout', 'abc'), ('timeout', '1.5'), ('timeout', ''),
                           ('timeout', '1' * 10), ('qa-threshold', 'P4'), ('qa-threshold', 'inherit'),
                           ('qa-threshold', 'off'), ('retry-budget', 'inherit')):
            self.code(2, 'set', self.plan, key, value)
        self.assertEqual(self.run_control('set', self.plan, 'colour', 'red').returncode, 2)   # argparse
        self.assertEqual(len(self.log()), 3)

    def test_plan_only_keys_refuse_a_task_and_the_task_must_be_a_plan_row(self):
        self.code(2, 'set', self.plan, 'timeout', '5', '--task', 'a')
        self.code(2, 'set', self.plan, 'qa-threshold', 'P1', '--task', 'zzz')
        self.code(2, 'set', self.plan, 'qa-threshold', 'P1', '--task', '')
        self.code(2, 'set', self.plan, 'qa-threshold', 'P1', '--task', '..')
        self.assertEqual(self.log(), [])

    def test_each_change_says_when_it_applies(self):
        self.live()
        self.put('retry_budget', '1')
        said = {key: self.ok('set', self.plan, key, '2').stdout.strip() for key in
                ('timeout', 'escalate-after', 'verify-retries', 'retry-budget', 'infra-retries')}
        self.assertIn("at the task's next attempt", said['timeout'])
        self.assertIn('at the next retry decision', said['escalate-after'])
        self.assertIn('at the next retry decision', said['retry-budget'])
        self.assertIn('at the next retry decision', said['infra-retries'])
        self.assertIn('at the next verification', said['verify-retries'])
        self.assertIn('(was 1)', said['retry-budget'])
        self.assertIn('at the next full retry (not QA-only resumes)', self.ok('pin', self.plan, 'a', 'sol:high:codex').stdout)
        self.assertIn('at the next full retry (not QA-only resumes)', self.ok('unpin', self.plan, 'a').stdout)

    def test_a_change_that_changes_nothing_is_not_logged(self):
        self.ok('set', self.plan, 'timeout', '60')
        self.assertIn('nothing changed', self.ok('set', self.plan, 'timeout', '60').stdout)
        self.ok('unpin', self.plan, 'a')
        self.assertEqual([e['action'] for e in self.log()], ['set'])

    def test_json_shape_of_set(self):
        out = json.loads(self.ok('set', self.plan, 'timeout', '60', '--json').stdout)
        self.assertEqual(out['ok'], True)
        self.assertEqual(out['changed'], dict(action='set', task=None, key='timeout', value='60', previous=None))
        self.assertEqual(out['applies'], "at the task's next attempt")
        self.assertEqual(out['controls']['run']['timeout'], 60)

    def test_writes_leave_no_temp_files(self):
        self.ok('set', self.plan, 'qa-threshold', 'P1', '--task', 'a')
        self.ok('set', self.plan, 'timeout', '5')
        self.assertEqual(self.files(self.plan), ['control.lock', 'control.log', 'tasks', 'tasks.tsv', 'tasks/a',
                                                 'tasks/a/qa_threshold', 'timeout'])

    # ------------------------------------------------------------------------------------ live

    def test_live_only_keys_refuse_when_no_scheduler_is_live(self):
        for key in ('retry-budget', 'infra-retries'):
            self.assertIn('live', self.code(3, 'set', self.plan, key, '2').stderr)
        self.code(3, 'set', self.plan, 'retry-budget', '2', '--task', 'a')
        self.assertFalse((self.plan / 'retry_budget').exists())
        self.assertEqual(self.log(), [])

    def test_a_dead_pid_is_not_live(self):
        child = subprocess.Popen([sys.executable, '-c', 'pass']); child.wait()
        for pid in (child.pid, 0, -1, 'x'):
            self.live(pid)
            self.code(3, 'set', self.plan, 'retry-budget', '2')
        self.put('scheduler.live', 'not json')
        self.code(3, 'set', self.plan, 'retry-budget', '2')
        self.assertFalse(self.show()['live'])

    def test_retry_budget_is_written_while_live_at_plan_and_task_level(self):
        self.live()
        self.put('retry_budget', '1')
        self.ok('set', self.plan, 'retry-budget', '4')
        self.assertEqual((self.plan / 'retry_budget').read_text().strip(), '4')
        self.ok('set', self.plan, 'retry-budget', '0', '--task', 'a')
        self.assertEqual((self.plan / 'tasks/a/retry_budget').read_text().strip(), '0')
        self.ok('set', self.plan, 'infra-retries', '3')
        self.assertEqual((self.plan / 'infra_budget').read_text().strip(), '3')
        shown = self.show()
        self.assertEqual((shown['live'], shown['run']['retry_budget'], shown['run']['infra_budget'],
                          shown['tasks']['a']['retry_budget']), (True, 4, 3, 0))
        self.ok('set', self.plan, 'retry-budget', 'inherit', '--task', 'a')
        self.assertFalse((self.plan / 'tasks/a/retry_budget').exists())
        self.assertEqual([(e['task'], e['key'], e['value'], e['previous']) for e in self.log()],
                         [(None, 'retry-budget', '4', '1'), ('a', 'retry-budget', '0', None),
                          (None, 'infra-retries', '3', None), ('a', 'retry-budget', 'inherit', '0')])

    def test_the_liveness_check_is_made_inside_the_lock(self):
        # the lock is held while the scheduler goes away, so the mutation has to see the state after it
        self.live()
        holder = self.hold_lock()
        runner = subprocess.Popen([sys.executable, str(SCRIPT), 'set', str(self.plan), 'retry-budget', '2'], env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.5)
        (self.plan / 'scheduler.live').unlink()
        holder.stdin.close(); holder.wait()
        _, err = runner.communicate(timeout=30)
        self.assertEqual(runner.returncode, 3, err)
        self.assertFalse((self.plan / 'retry_budget').exists())

    # ------------------------------------------------------------------------------------ lock

    def hold_lock(self):
        """A child that holds control.lock until its stdin closes."""
        code = ('import fcntl, sys\n'
                'fd = open(sys.argv[1], "a")\n'
                'fcntl.flock(fd, fcntl.LOCK_EX)\n'
                'print("held", flush=True)\n'
                'sys.stdin.read()\n')
        child = subprocess.Popen([sys.executable, '-c', code, str(self.plan / 'control.lock')], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        self.addCleanup(child.wait)
        self.addCleanup(child.stdin.close)
        self.assertEqual(child.stdout.readline().strip(), 'held')
        return child

    def test_a_held_control_lock_makes_a_mutation_fail_busy_after_about_five_seconds(self):
        holder = self.hold_lock()
        started = time.monotonic()
        result = self.code(3, 'set', self.plan, 'timeout', '5')
        waited = time.monotonic() - started
        self.assertIn('busy', result.stderr)
        self.assertTrue(4.5 <= waited < 15, waited)
        self.code(3, 'pin', self.plan, 'a', 'sol:high:codex')
        self.assertFalse((self.plan / 'timeout').exists())
        self.assertEqual(self.log(), [])
        holder.stdin.close(); holder.wait()
        self.ok('set', self.plan, 'timeout', '5')

    def test_show_does_not_wait_for_the_lock(self):
        self.hold_lock()
        started = time.monotonic()
        self.show()
        self.assertLess(time.monotonic() - started, 4)

    def test_schedule_lock_is_never_touched(self):
        with open(self.plan / 'schedule.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)   # a running scheduler's lock: a probe or a take would fail or block
            self.ok('set', self.plan, 'timeout', '5')
            self.ok('pin', self.plan, 'a', 'sol:high:codex')
            self.code(3, 'set', self.plan, 'retry-budget', '1')   # not live: and still no probe of schedule.lock
            self.show()
        (self.plan / 'schedule.lock').unlink()
        self.ok('set', self.plan, 'timeout', '6')
        self.assertFalse((self.plan / 'schedule.lock').exists())

    def test_the_lock_file_is_not_followed_through_a_symlink(self):
        target = self.base / 'elsewhere'; target.write_text('keep')
        (self.plan / 'control.lock').symlink_to(target)
        self.code(2, 'set', self.plan, 'timeout', '5')
        self.assertEqual(target.read_text(), 'keep')
        self.assertFalse((self.plan / 'timeout').exists())

    # ------------------------------------------------------------------------------------ escalate-after

    def test_escalate_after_zero_clears_the_stop_markers_of_every_task(self):
        for task in ('a', 'b'):
            for marker in ('escalation.stop', 'split.suggested', 'escalation.next'):
                self.put(f'tasks/{task}/{marker}', 'x\n')
            self.put(f'tasks/{task}/escalation.pin', 'sol:high:codex\n')
        self.put('escalate_after', '3')
        self.ok('set', self.plan, 'escalate-after', '2')
        self.assertTrue((self.plan / 'tasks/a/escalation.stop').exists())   # only 0 means off
        out = json.loads(self.ok('set', self.plan, 'escalate-after', '0', '--json').stdout)
        self.assertEqual((self.plan / 'escalate_after').read_text().strip(), '0')
        for task in ('a', 'b'):
            self.assertEqual(self.files(self.plan / 'tasks' / task), ['escalation.pin'])
            self.assertFalse(out['controls']['tasks'][task]['escalation_stop'])
        self.assertEqual(out['changed']['previous'], '2')

    def test_escalate_after_zero_changes_nothing_when_a_marker_path_is_unsafe(self):
        elsewhere = self.base / 'elsewhere'; elsewhere.mkdir()
        self.put('tasks/a/escalation.stop', 'x\n')
        (self.plan / 'tasks/b').symlink_to(elsewhere)
        self.code(2, 'set', self.plan, 'escalate-after', '0')
        (self.plan / 'tasks/b').unlink()
        (self.plan / 'tasks/b').mkdir()
        os.mkfifo(self.plan / 'tasks/b/split.suggested')
        self.code(2, 'set', self.plan, 'escalate-after', '0')
        self.assertTrue((self.plan / 'tasks/a/escalation.stop').exists())
        self.assertFalse((self.plan / 'escalate_after').exists())

    # ------------------------------------------------------------------------------------ path safety

    def test_a_symlinked_task_dir_is_refused_and_nothing_is_written_outside(self):
        outside = self.base / 'outside'; outside.mkdir()
        (self.plan / 'tasks/a').symlink_to(outside)
        self.code(2, 'set', self.plan, 'qa-threshold', 'P1', '--task', 'a')
        self.code(2, 'pin', self.plan, 'a', 'sol:high:codex')
        self.code(2, 'unpin', self.plan, 'a')
        self.assertEqual(self.files(outside), [])
        self.assertEqual(self.log(), [])

    def test_a_symlinked_tasks_dir_is_refused(self):
        outside = self.base / 'outside'; outside.mkdir()
        (self.plan / 'tasks').rmdir()
        (self.plan / 'tasks').symlink_to(outside)
        self.code(2, 'set', self.plan, 'qa-threshold', 'P1', '--task', 'a')
        self.assertEqual(self.files(outside), [])

    def test_a_task_id_that_escapes_the_plan_is_refused_even_when_tasks_tsv_lists_it(self):
        (self.base / 'x').mkdir()
        (self.plan / 'tasks.tsv').write_text(TASKS + 'x/../../x\t-\thigh\tsrc\tsol:high:codex\t-\tEvil\n'
                                             + '../x\t-\thigh\tsrc\tsol:high:codex\t-\tEvil\n')
        for task in ('../x', 'x/../../x', '.', '..'):
            self.code(2, 'set', self.plan, 'qa-threshold', 'P1', '--task', task)
            self.code(2, 'pin', self.plan, task, 'sol:high:codex')
            self.code(2, 'unpin', self.plan, task)
        self.assertEqual(self.files(self.base / 'x'), [])
        self.assertEqual(sorted(p.name for p in self.base.iterdir()), ['doctor.sh', 'plan', 'x'])
        shown = self.show()   # such a row is not listed either
        self.assertEqual(list(shown['tasks']), ['a', 'b'])

    def test_a_fifo_or_symlink_at_the_target_is_refused(self):
        (self.plan / 'tasks/a').mkdir()
        os.mkfifo(self.plan / 'tasks/a/qa_threshold')
        self.code(2, 'set', self.plan, 'qa-threshold', 'P1', '--task', 'a')
        self.code(2, 'set', self.plan, 'qa-threshold', 'inherit', '--task', 'a')   # nor deleted
        self.assertTrue(os.path.exists(self.plan / 'tasks/a/qa_threshold'))
        self.assertFalse((self.plan / 'tasks/a/qa_threshold').is_file())
        secret = self.base / 'secret'; secret.write_text('keep\n')
        (self.plan / 'tasks/a/escalation.pin').symlink_to(secret)
        self.code(2, 'pin', self.plan, 'a', 'sol:high:codex')
        self.code(2, 'unpin', self.plan, 'a')
        self.assertEqual(secret.read_text(), 'keep\n')
        self.assertTrue((self.plan / 'tasks/a/escalation.pin').is_symlink())
        (self.plan / 'timeout').symlink_to(secret)
        self.code(2, 'set', self.plan, 'timeout', '5')
        self.assertEqual(secret.read_text(), 'keep\n')
        self.assertEqual(self.log(), [])

    def test_an_unsafe_control_log_refuses_the_change_before_it_is_made(self):
        secret = self.base / 'secret'; secret.write_text('keep\n')
        (self.plan / 'control.log').symlink_to(secret)
        self.code(2, 'set', self.plan, 'timeout', '5')
        self.assertFalse((self.plan / 'timeout').exists())
        self.assertEqual(secret.read_text(), 'keep\n')

    def test_show_does_not_read_through_symlinks(self):
        secret = self.base / 'secret'; secret.write_text('P0\n')
        (self.plan / 'qa_threshold').symlink_to(secret)
        self.assertIsNone(self.show()['run']['qa_threshold'])

    # ------------------------------------------------------------------------------------ pins

    def test_a_pin_on_a_merged_task_or_a_fractal_plan_is_refused(self):
        self.put('tasks/b/merged', '')
        self.assertIn('MERGED', self.code(3, 'pin', self.plan, 'b', 'sol:high:codex').stderr)
        self.assertIn('MERGED', self.code(3, 'set', self.plan, 'qa-threshold', 'P1', '--task', 'b').stderr)
        self.code(3, 'unpin', self.plan, 'b')
        self.assertFalse((self.plan / 'tasks/b/escalation.pin').exists())
        self.put('fractal-selection.json', json.dumps({'enabled': True}))
        self.assertIn('Fractal', self.code(3, 'pin', self.plan, 'a', 'sol:high:codex').stderr)
        self.assertTrue(self.show()['fractal'])
        self.ok('set', self.plan, 'qa-threshold', 'P1', '--task', 'a')   # only pins are Fractal-refused
        self.put('fractal-selection.json', json.dumps({'enabled': False}))
        self.ok('pin', self.plan, 'a', 'sol:high:codex')

    def test_a_valid_pin_is_written_a_failing_one_is_refused_and_unpin_removes_it(self):
        self.ok('pin', self.plan, 'a', 'sol:xhigh:codex')
        pin = self.plan / 'tasks/a/escalation.pin'
        self.assertEqual(pin.read_text(), 'sol:xhigh:codex\n')
        self.assertEqual(Path(str(self.stub) + '.calls').read_text().strip(), '--spec sol:xhigh:codex --role dwarf')
        self.assertEqual(self.show()['tasks']['a']['pin'], 'sol:xhigh:codex')
        failed = self.code(3, 'pin', self.plan, 'a', 'bad:spec:x')
        self.assertIn('bad:spec:x', failed.stderr)
        self.assertEqual(pin.read_text(), 'sol:xhigh:codex\n')   # the earlier pin is untouched
        self.ok('unpin', self.plan, 'a')
        self.assertFalse(pin.exists())
        self.assertEqual([(e['action'], e['key'], e['value'], e['previous']) for e in self.log()],
                         [('pin', 'pin', 'sol:xhigh:codex', None), ('unpin', 'pin', None, 'sol:xhigh:codex')])

    def test_the_real_doctor_refuses_an_unknown_alias(self):
        env = {k: v for k, v in self.env.items() if k != 'FORGE_CONTROL_DOCTOR'}
        result = self.run_control('pin', self.plan, 'a', 'nosuchalias', env=env)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertFalse((self.plan / 'tasks/a/escalation.pin').exists())

    def test_a_pin_must_be_one_clean_spec(self):
        for spec in ('', 'sol high', 'sol:high:codex\nx', 'a\tb'):
            self.code(2, 'pin', self.plan, 'a', spec)
        self.assertFalse(Path(str(self.stub) + '.calls').exists())   # never reached the doctor

    def test_pin_and_unpin_print_the_documented_json(self):
        pinned = json.loads(self.ok('pin', self.plan, 'a', 'sol:xhigh:codex', '--actor', 'ui', '--json').stdout)
        self.assertEqual(set(pinned), {'ok', 'changed', 'applies', 'controls'})
        self.assertEqual(pinned['ok'], True)
        self.assertEqual(pinned['changed'], dict(action='pin', task='a', key='pin', value='sol:xhigh:codex', previous=None))
        self.assertEqual(pinned['applies'], 'at the next full retry (not QA-only resumes)')
        self.assertEqual(pinned['controls']['tasks']['a']['pin'], 'sol:xhigh:codex')
        unpinned = json.loads(self.ok('unpin', self.plan, 'a', '--actor', 'ui', '--json').stdout)
        self.assertEqual(set(unpinned), {'ok', 'changed', 'applies', 'controls'})
        self.assertEqual(unpinned['changed'], dict(action='unpin', task='a', key='pin', value=None,
                                                   previous='sol:xhigh:codex'))
        self.assertIsNone(unpinned['controls']['tasks']['a']['pin'])
        self.assertEqual([e['actor'] for e in self.log()], ['ui', 'ui'])

    # ------------------------------------------------------------------------------------ dataset

    def test_each_change_emits_a_control_event_with_the_log_lines_fields(self):
        env = {k: v for k, v in self.env.items() if k != 'FORGE_DATASET'}
        self.run_control('set', self.plan, 'qa-threshold', 'P1', '--task', 'a', env=env)
        self.run_control('set', self.plan, 'timeout', '90', env=env)
        events = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-dataset.py'), 'dump', '--kind', 'event'],
                                capture_output=True, text=True, env=env).stdout.splitlines()
        events = [e['data'] for e in map(json.loads, events) if e['data'].get('name') == 'control']
        by_key = {e['key']: e for e in events}
        self.assertEqual(set(by_key), {'qa-threshold', 'timeout'})
        for entry in self.log():
            event = by_key[entry['key']]
            for field in ('at', 'actor', 'action', 'key', 'value', 'previous'):
                self.assertEqual(event[field], '' if entry[field] is None else str(entry[field]), field)
        self.assertEqual(by_key['timeout']['previous'], '')

    # ------------------------------------------------------------------------------------ launcher

    def test_the_launcher_has_control(self):
        helped = subprocess.run([str(ROOT / 'forge'), 'control', '--help'], capture_output=True, text=True)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn('usage: forge control', helped.stdout)
        listed = subprocess.run([str(ROOT / 'forge'), '--help'], capture_output=True, text=True).stdout
        self.assertRegex(listed, r'(?m)^  control  ')
        shown = subprocess.run([str(ROOT / 'forge'), 'control', 'show', str(self.plan), '--json'], capture_output=True,
                               text=True, env=self.env)
        self.assertEqual(json.loads(shown.stdout)['plan'], str(self.plan))


if __name__ == '__main__':
    unittest.main()
