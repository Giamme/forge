"""Brief mode in a run (#135): the writer `run` starts, the gated wait in the scheduler, strict refusal,
and the possibly-stale flags after a merge.

Scheduler behaviour is driven with test_schedule's mock worker and brief.json files written by hand, so
no writer is needed. `run` itself (the writer's start, strict's refusal) goes through test_forge's fake
CLIs; every task there has a brief.json already, so `forge brief write` has nothing to dispatch."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import test_forge as fixtures
from test_schedule import WORKER as BASE_WORKER

ROOT = Path(__file__).resolve().parents[1]

# `_merge` also leaves the task's changes.diff when the test staged one: what `forge brief stale` reads.
# A task named `locked` makes the plan unreadable so that `stale` fails.
WORKER = BASE_WORKER.replace(
    'touch "$t/merged"; echo "$name merged" >> "$plan/events.log"',
    'touch "$t/merged"; echo "$name merged" >> "$plan/events.log"; '
    '[ -f "$plan/diffs/$name" ] && cp "$plan/diffs/$name" "$t/changes.diff"; '
    '[ -f "$plan/break-plan" ] && chmod 000 "$plan/tasks.tsv"')
assert WORKER != BASE_WORKER


def question(qid, must=False, text=None):
    return {'id': qid, 'text': text or 'what about ' + qid + '?', 'why': '', 'options': [], 'default': 'the default',
            'scope': 'task', 'must_answer': must}


def diff_of(path):
    return f'diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+x\n'


class SchedulerBriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.plan = self.root / 'plan'; self.plan.mkdir()
        (self.plan / 'outcomes').mkdir(); (self.plan / 'diffs').mkdir()
        (self.plan / 'wt_root').write_text(str(self.root))
        self.worker = self.root / 'worker.sh'; self.worker.write_text(WORKER)
        self.env = dict(os.environ, FORGE_INFRA_BACKOFF='0.05', XDG_CONFIG_HOME=str(self.root / 'config'),
                        HOME=str(self.root / 'home'))
        self.env.pop('FORGE_BRIEF', None)
        self.rows = []
        self.procs = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            for stream in (proc.stdout, proc.stderr):
                if stream: stream.close()

    def level(self, word):
        (self.plan / 'brief').write_text(word + '\n')

    def task(self, name, deps='-', questions=None, files=None):
        self.rows.append(f'{name}\t{deps}\tlow\t{files or "f_" + name}\tsol\topus\t{name.upper()}')
        (self.plan / 'outcomes' / name).write_text('PASS\n')
        root = self.plan / 'tasks' / name; root.mkdir(parents=True)
        (root / 'base_ref').write_text('base\n')
        if questions is not None:
            (root / 'brief.json').write_text(json.dumps({'questions': questions}))

    def answer(self, name, *qids):
        path = self.plan / 'tasks' / name / 'brief.answers.json'
        path.write_text(json.dumps({qid: {'text': 'yes', 'actor': 'cli'} for qid in qids}))

    def command(self, capacity=3):
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')
        return [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker), str(self.plan), str(capacity)]

    def run_scheduler(self):
        return subprocess.run(self.command(), env=self.env, text=True, timeout=60,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def start_scheduler(self):
        proc = subprocess.Popen(self.command(), env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.procs.append(proc)
        return proc

    def events(self):
        f = self.plan / 'events.log'
        return f.read_text().splitlines() if f.exists() else []

    def until(self, condition, what, limit=30):
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail('timed out waiting for ' + what + '; events: ' + repr(self.events()))

    def finish(self, proc):
        out, err = proc.communicate(timeout=60)
        return proc.returncode, out, err

    # --- advisory and off ----------------------------------------------------------
    def test_advisory_never_delays_a_task(self):
        self.level('advisory')
        self.task('a', questions=[question('q1', must=True)])
        self.task('b', questions=[question('q1')])
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(e for e in self.events() if e.endswith('merged')), ['a merged', 'b merged'])
        self.assertNotIn('waits for', r.stderr)

    def test_brief_off_dispatches_as_before_and_runs_nothing_of_brief(self):
        self.task('a', questions=[question('q1', must=True)])
        self.task('b', deps='a', questions=[question('q1', must=True)])
        self.task('c', questions=[question('q1')])
        self.level('off')
        (self.plan / 'diffs' / 'a').write_text(diff_of('elsewhere.txt'))
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 0, r.stderr)
        starts = [e.split()[0] for e in self.events() if ' start ' in e]
        self.assertEqual(sorted(starts), ['a', 'b', 'c'])
        self.assertLess(starts.index('a'), starts.index('b'))
        self.assertNotIn('brief', r.stderr)
        self.assertFalse((self.plan / 'brief.pid').exists() or (self.plan / 'brief.out').exists())
        # no stale flag either: the merge of a touched elsewhere.txt and b still has its questions as written
        flagged = json.loads((self.plan / 'tasks/b/brief.json').read_text())['questions']
        self.assertNotIn('possibly_stale', flagged[0])

    # --- gated ---------------------------------------------------------------------
    def test_gated_holds_only_the_task_with_an_unanswered_flagged_question(self):
        self.level('gated')
        self.task('a', questions=[question('q1', must=True), question('q2')])
        self.task('b', questions=[question('q1')])                  # a question it may skip
        self.task('c', questions=[])
        proc = self.start_scheduler()
        self.until(lambda: {'b merged', 'c merged'} <= set(self.events()), 'b and c to merge')
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), 'the run ended while a task still waits for an answer')
        self.assertFalse(any(e.startswith('a ') for e in self.events()))
        self.answer('a', 'q1')
        code, _, err = self.finish(proc)
        self.assertEqual(code, 0, err)
        events = self.events()
        self.assertIn('a start 1', events)
        self.assertEqual(self.events().count('a merged'), 1)
        wait_lines = [line for line in err.splitlines() if 'waits for your answer' in line]
        self.assertEqual(len(wait_lines), 1, err)
        self.assertEqual(wait_lines[0], f'forge: a waits for your answer to q1: forge brief answer {self.plan} a q1 "…"')

    def test_gated_an_answer_before_the_run_never_holds(self):
        self.level('gated')
        self.task('a', questions=[question('q1', must=True)])
        self.answer('a', 'q1')
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn('waits for', r.stderr)

    def test_gated_a_waiting_task_holds_its_dependents_and_the_run_goes_on_after_the_answer(self):
        self.level('gated')
        self.task('a', questions=[question('q1', must=True)])
        self.task('b', deps='a')
        proc = self.start_scheduler()
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), 'the run ended while a task waits for an answer')
        self.assertEqual(self.events(), [])
        self.answer('a', 'q1')
        code, _, err = self.finish(proc)
        self.assertEqual(code, 0, err)
        self.assertEqual([e for e in self.events() if e.endswith('merged')], ['a merged', 'b merged'])

    def test_gated_waits_while_the_writer_runs_and_goes_once_it_is_gone(self):
        self.level('gated')
        self.task('a')                                             # no brief.json yet
        self.task('b', questions=[])
        writer = subprocess.Popen(['sleep', '120'])
        self.procs.append(writer)
        (self.plan / 'brief.pid').write_text(f'{writer.pid}\n')
        proc = self.start_scheduler()
        self.until(lambda: 'b merged' in self.events(), 'b to merge')
        time.sleep(1.2)
        self.assertIsNone(proc.poll())
        self.assertFalse(any(e.startswith('a ') for e in self.events()), self.events())
        writer.kill(); writer.wait()                               # gone, and no brief.json: the wait is over
        code, _, err = self.finish(proc)
        self.assertEqual(code, 0, err)
        self.assertIn('a merged', self.events())
        self.assertEqual(sum('waits for its brief' in line for line in err.splitlines()), 1, err)

    def test_gated_a_missing_brief_does_not_hold_without_a_writer(self):
        self.level('gated')
        self.task('a')
        (self.plan / 'brief.pid').write_text(f'{2 ** 22 + 12345}\n')   # above any pid this machine hands out
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('a merged', self.events())

    def test_gated_the_environment_switch_wins(self):
        self.level('gated')
        self.task('a', questions=[question('q1', must=True)])
        self.env['FORGE_BRIEF'] = 'off'
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 0, r.stderr)

    # --- stale ---------------------------------------------------------------------
    def stale_plan(self, level='advisory'):
        self.level(level)
        self.task('a', files='src/a.py')
        self.task('b', deps='a', questions=[question('q1')])
        self.task('c', deps='-', questions=[question('q1')])        # does not depend on a

    def flagged(self, name):
        questions = json.loads((self.plan / 'tasks' / name / 'brief.json').read_text())['questions']
        return [q['id'] for q in questions if q.get('possibly_stale') is True]

    def test_a_merge_outside_the_declared_files_flags_the_dependents_open_questions(self):
        self.stale_plan()
        (self.plan / 'diffs' / 'a').write_text(diff_of('src/a.py') + diff_of('src/shared.py'))
        # b waits for a, so the flag is set before b starts and nothing else has touched its file
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.flagged('b'), self.flagged('c')), (['q1'], []))
        self.assertNotIn('could not flag', r.stderr)

    def test_a_merge_inside_its_files_flags_nothing(self):
        self.stale_plan()
        (self.plan / 'diffs' / 'a').write_text(diff_of('src/a.py'))
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.flagged('b'), [])

    def test_a_failing_stale_check_is_one_warning_and_the_run_goes_on(self):
        self.stale_plan()
        (self.plan / 'break-plan').write_text('')
        self.addCleanup(lambda: (self.plan / 'tasks.tsv').chmod(0o644))
        r = self.run_scheduler()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sum('could not flag the dependents of' in line for line in r.stderr.splitlines()), 3, r.stderr)
        self.assertIn('b merged', self.events())


class RunBriefTests(unittest.TestCase):
    """`run` with the real forge-parallel.sh and test_forge's fake CLIs."""
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        for name in ('FORGE_BRIEF', 'FORGE_RETRO'):
            self.env.pop(name, None)

    def plan_of(self, *flags, questions=None):
        plan = self.root / 'plan'
        plan.mkdir()
        (plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\nb\ta\tlow\tb.txt\tsol\topus\tB\n')
        for ident in ('a', 'b'):
            task = plan / 'tasks' / ident
            task.mkdir(parents=True)
            (task / 'prompt.md').write_text('Implement change. TASK_%s\n' % ident)
        result = self.run_script('forge-parallel.sh', 'plan', plan, '--repo', self.repo, '--no-memory', *flags)
        self.assertEqual(result.returncode, 0, result.stdout)
        for ident, asked in (questions or {}).items():
            (plan / 'tasks' / ident / 'brief.json').write_text(json.dumps({'questions': asked}))
        return plan

    def test_strict_refuses_to_start_while_a_question_is_open_and_runs_once_answered(self):
        plan = self.plan_of('--brief', 'strict', '--brief-model', 'sol',
                            questions={'a': [question('q1', text='Which port?')], 'b': [question('q1', text='Which colour?')]})
        before = self.calls()
        refused = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(refused.returncode, 3, refused.stdout)
        for expected in ('a q1: Which port?', 'b q1: Which colour?', 'default: the default',
                         'forge brief answer %s a q1' % plan, 'forge brief answer %s b q1' % plan):
            self.assertIn(expected, refused.stdout)
        self.assertEqual(self.calls(), before, 'a model was called')
        self.assertFalse((plan / 'tasks' / 'a' / 'status').exists())
        self.assertFalse((plan / 'tasks' / 'a' / 'dwarf.last').exists())
        # one answered question is still a refusal, and it lists only the other
        brief = ROOT / 'scripts' / 'forge-brief.py'
        done = subprocess.run([sys.executable, str(brief), 'answer', str(plan), 'a', 'q1', 'port 80'],
                              env=self.env, text=True, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        again = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(again.returncode, 3, again.stdout)
        self.assertNotIn('a q1:', again.stdout)
        self.assertIn('b q1: Which colour?', again.stdout)
        done = subprocess.run([sys.executable, str(brief), 'answer', str(plan), 'b', 'q1', 'blue'],
                              env=self.env, text=True, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        final = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(final.returncode, 0, final.stdout)
        self.assertTrue((plan / 'tasks' / 'a' / 'merged').exists() and (plan / 'tasks' / 'b' / 'merged').exists())

    def test_advisory_starts_the_writer_beside_the_run_and_never_waits_for_it(self):
        plan = self.plan_of('--brief', 'advisory', '--brief-model', 'sol',
                            questions={'a': [question('q1', must=True)], 'b': []})
        result = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((plan / 'brief.out').exists(), 'the writer was not started')
        self.until_gone(plan / 'brief.pid')
        self.assertTrue((plan / 'tasks' / 'a' / 'merged').exists())

    def test_gated_runs_the_writer_and_the_run_waits_for_an_answer_it_is_given_late(self):
        plan = self.plan_of('--brief', 'gated', '--brief-model', 'sol',
                            questions={'a': [question('q1', must=True)], 'b': []})
        env = dict(self.env)
        proc = subprocess.Popen(['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'run', str(plan)], cwd=self.repo, env=env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        seen = []
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:     # the scheduler's wait line: a is held, b waits behind it
            line = proc.stdout.readline()
            if not line:
                break
            seen.append(line)
            if 'waits for your answer to q1' in line:
                break
        self.assertTrue(any('waits for your answer to q1' in line for line in seen), ''.join(seen))
        time.sleep(1.0)
        self.assertIsNone(proc.poll(), 'the run ended while a waits for an answer')
        self.assertFalse((plan / 'tasks' / 'a' / 'dwarf.last').exists())
        brief = ROOT / 'scripts' / 'forge-brief.py'
        done = subprocess.run([sys.executable, str(brief), 'answer', str(plan), 'a', 'q1', 'yes'],
                              env=self.env, text=True, capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        out, _ = proc.communicate(timeout=120)
        self.assertEqual(proc.returncode, 0, ''.join(seen) + out)
        self.assertTrue((plan / 'tasks' / 'b' / 'merged').exists())

    def test_brief_off_runs_no_brief_process(self):
        plan = self.plan_of()
        result = self.run_script('forge-parallel.sh', 'run', plan)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse((plan / 'brief.out').exists() or (plan / 'brief.pid').exists())
        self.assertNotIn('brief', result.stdout)

    def until_gone(self, path, limit=30):
        deadline = time.monotonic() + limit
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(path.exists(), str(path) + ' is still there')


if __name__ == '__main__':
    unittest.main()
