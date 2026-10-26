"""`forge control retry` and `forge control add` on a live run (#31): the CLI queues one request
file each, the scheduler applies or refuses it and logs one outcome line with the same id,
and no request outlives the scheduler. The scheduler cases use a worker standing in for
forge-parallel.sh, as test_schedule_live.py does; the last case drives a real run."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / 'scripts/forge-control.py'

WORKER = r'''#!/bin/bash
cmd="$1"; plan="$2"; name="$3"; t="$plan/tasks/$name"
waitfor() { for _ in $(seq 1 1500); do [ -e "$1" ] && return 0; sleep 0.02; done; return 1; }
case "$cmd" in
  _task)
    n=$(( $(cat "$t/calls" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$t/calls"
    echo "$name start $n trigger=${FORGE_DATASET_TRIGGER:-}" >> "$plan/events.log"
    [ -f "$plan/hooks/$name.$n" ] && . "$plan/hooks/$name.$n"
    outcome="$(head -1 "$plan/outcomes/$name")"
    tail -n +2 "$plan/outcomes/$name" > "$plan/outcomes/$name.rest"; mv "$plan/outcomes/$name.rest" "$plan/outcomes/$name"
    echo "$outcome" > "$t/status"
    echo "$name end $outcome" >> "$plan/events.log"
    ;;
  _merge)
    [ -f "$plan/hold-merge/$name" ] && { touch "$plan/merging.$name"; waitfor "$plan/gate"; }
    if [ "$(cat "$t/status" 2>/dev/null)" = PASS ]; then touch "$t/merged"; echo "$name merged" >> "$plan/events.log"; fi
    ;;
  _prepare_retry)
    echo "$name prepared ${*:4}" >> "$plan/events.log"
    if [ "${4:-}" = --dwarf ]; then rm -f "$t/escalation.stop"
    elif [ -f "$t/escalation.stop" ]; then echo "split $name, or name a --dwarf" > "$t/split.suggested"; exit 3; fi
    rm -f "$t/status"
    ;;
  _waves)
    echo "waves" >> "$plan/events.log"
    ;;
esac
'''

# Succeeds for any spec but `bad:spec:x`, and records how it was called.
STUB = textwrap.dedent('''\
    #!/bin/sh
    echo "$@" >> "$0.calls"
    [ "$2" = bad:spec:x ] && { echo "stub: bad:spec:x is not a pairing" >&2; exit 1; }
    exit 0
    ''')


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve(); self.plan = self.root / 'plan'; self.plan.mkdir()
        for sub in ('outcomes', 'hooks', 'hold-merge', 'tasks'):
            (self.plan / sub).mkdir()
        (self.plan / 'wt_root').write_text(str(self.root))
        self.worker = self.root / 'worker.sh'; self.worker.write_text(WORKER)
        self.stub = self.root / 'doctor.sh'; self.stub.write_text(STUB); self.stub.chmod(0o755)
        self.env = dict(os.environ, FORGE_DATASET='off', FORGE_CONTROL_DOCTOR=str(self.stub))
        self.rows = []

    def task(self, name, outcomes=('PASS',), deps='-', status=None, row=True):
        if row:
            self.rows.append(f'{name}\t{deps}\tlow\tf_{name}\tsol\topus\t{name.upper()}')
        (self.plan / 'outcomes' / name).write_text(''.join(o + '\n' for o in outcomes))
        root = self.plan / 'tasks' / name; root.mkdir(parents=True, exist_ok=True)
        (root / 'base_ref').write_text('base\n')
        (root / 'prompt.md').write_text('Do ' + name + '.\n')
        if status:
            (root / 'status').write_text(status + '\n')
        self.write_tasks()

    def write_tasks(self):
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows) + '\n')

    def control(self, *args):
        return subprocess.run([sys.executable, str(CONTROL), *map(str, args)], capture_output=True, text=True,
                              env=self.env, timeout=60)

    def live(self):
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': os.getpid(), 'started': time.time()}))

    def log(self):
        path = self.plan / 'control.log'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def outcomes(self):
        return [e for e in self.log() if e.get('actor') == 'scheduler']

    def requests(self):
        directory = self.plan / 'control.requests'
        return sorted(p.name for p in directory.iterdir()) if directory.exists() else []

    def events(self):
        f = self.plan / 'events.log'
        return f.read_text().splitlines() if f.exists() else []

    def status(self, name):
        root = self.plan / 'tasks' / name
        if (root / 'merged').exists():
            return 'MERGED'
        f = root / 'status'
        return f.read_text().strip() if f.exists() else 'PENDING'

    def start(self, *extra, capacity=3):
        command = [sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker), str(self.plan),
                   str(capacity), *map(str, extra)]
        proc = subprocess.Popen(command, env=self.env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc

    def wait_for(self, condition, what, timeout=20):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.02)
        self.fail('timed out waiting for ' + what)


class CliTests(Base):
    def test_retry_and_add_write_a_request_and_log_it(self):
        self.task('a', status='FAIL')
        self.task('n')
        self.live()
        result = self.control('retry', self.plan, 'a', '--dwarf', 'sol:xhigh:codex', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        first = json.loads(result.stdout)
        self.assertEqual(first['queued'], True)
        self.assertEqual(self.control('add', self.plan, 'n').returncode, 0)
        self.assertEqual(self.control('retry', self.plan, 'a', '--actor', 'ui').returncode, 0)
        names = self.requests()
        self.assertEqual(len(names), 3)
        self.assertEqual(names[0], first['request'])
        bodies = [json.loads((self.plan / 'control.requests' / n).read_text()) for n in names]
        self.assertEqual([(b['verb'], b['task'], b['dwarf'], b['actor']) for b in bodies],
                         [('retry', 'a', 'sol:xhigh:codex', 'cli'), ('add', 'n', None, 'cli'), ('retry', 'a', None, 'ui')])
        self.assertEqual([(e['action'], e['task'], e['value'], e['request']) for e in self.log()],
                         [('retry', 'a', 'sol:xhigh:codex', names[0]), ('add', 'n', None, names[1]),
                          ('retry', 'a', None, names[2])])
        self.assertIn('--spec sol:xhigh:codex --role dwarf', (self.root / 'doctor.sh.calls').read_text())

    def test_retry_refusals(self):
        self.task('a', status='FAIL')
        self.task('m', status='PASS')
        (self.plan / 'tasks/m/merged').write_text('')
        cases = [
            ('not live', 3, ('retry', self.plan, 'a'), False),
            ('merged', 3, ('retry', self.plan, 'm'), True),
            ('unknown task', 2, ('retry', self.plan, 'zz'), True),
            ('bad dwarf', 3, ('retry', self.plan, 'a', '--dwarf', 'bad:spec:x'), True),
            ('dwarf with a space', 2, ('retry', self.plan, 'a', '--dwarf', 'sol high'), True),
        ]
        for what, code, args, live in cases:
            with self.subTest(what):
                (self.plan / 'scheduler.live').unlink(missing_ok=True)
                if live:
                    self.live()
                result = self.control(*args)
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.live()
        (self.plan / 'fractal-selection.json').write_text('{"enabled": true}')
        self.assertEqual(self.control('retry', self.plan, 'a').returncode, 3)
        (self.plan / 'fractal-selection.json').unlink()
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.control('retry', self.plan, 'a').returncode, 0)   # the valid control

    def test_add_validates_the_row_like_plan_would(self):
        self.task('a')
        self.live()
        good = 'n\ta\tmedium\tsrc/n.py\tsol:high:codex\topus:xhigh:claude\tNew'
        (self.plan / 'tasks/n').mkdir()
        (self.plan / 'tasks/n/prompt.md').write_text('Do n.\n')
        mutations = [
            ('six columns', good.rsplit('\t', 1)[0]),
            ('bad difficulty', good.replace('medium', 'huge')),
            ('unknown dep', good.replace('\ta\t', '\tzz\t')),
            ('self cycle', good.replace('\ta\t', '\tn\t')),
            ('empty files', good.replace('src/n.py', '-')),
            ('unassigned dwarf', good.replace('sol:high:codex', 'UNASSIGNED')),
            ('pooled dwarf', good.replace('sol:high:codex', 'sol,astra')),
            ('dash qa', good.replace('opus:xhigh:claude', '-')),
            ('doctor refuses the qa', good.replace('opus:xhigh:claude', 'bad:spec:x')),
        ]
        for what, row in mutations:
            with self.subTest(what):
                (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows + [row]) + '\n')
                result = self.control('add', self.plan, 'n')
                self.assertIn(result.returncode, (2, 3), what)
        with self.subTest('missing prompt'):
            (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows + [good]) + '\n')
            (self.plan / 'tasks/n/prompt.md').write_text('  \n')
            self.assertEqual(self.control('add', self.plan, 'n').returncode, 2)
            (self.plan / 'tasks/n/prompt.md').write_text('Do n.\n')
        with self.subTest('duplicate id'):
            (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows + [good, good]) + '\n')
            self.assertEqual(self.control('add', self.plan, 'n').returncode, 2)
        self.assertEqual(self.requests(), [])
        (self.plan / 'tasks.tsv').write_text('\n'.join(self.rows + [good]) + '\n')
        result = self.control('add', self.plan, 'n')                       # the valid control
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--spec opus:xhigh:claude --role qa', (self.root / 'doctor.sh.calls').read_text())

    def test_a_cyclic_add_is_refused_and_waves_stay(self):
        self.task('a')
        self.live()
        (self.plan / 'waves.tsv').write_text('1\ta\n')
        rows = self.rows[:]
        rows[0] = rows[0].replace('\t-\t', '\tn\t')          # a now depends on n, and n on a
        rows.append('n\ta\tlow\tf_n\tsol\topus\tN')
        (self.plan / 'tasks.tsv').write_text('\n'.join(rows) + '\n')
        (self.plan / 'tasks/n').mkdir(); (self.plan / 'tasks/n/prompt.md').write_text('Do n.\n')
        result = self.control('add', self.plan, 'n')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('cycle', result.stderr)
        self.assertEqual((self.plan / 'waves.tsv').read_text(), '1\ta\n')
        # And the waves entry the scheduler calls fails without touching them either.
        (self.plan / 'deferrals.txt').write_text('kept\n')
        waves = subprocess.run(['bash', str(ROOT / 'scripts/forge-parallel.sh'), '_waves', str(self.plan)],
                               capture_output=True, text=True, timeout=60)
        self.assertNotEqual(waves.returncode, 0)
        self.assertEqual((self.plan / 'waves.tsv').read_text(), '1\ta\n')
        self.assertEqual((self.plan / 'deferrals.txt').read_text(), 'kept\n')
        self.assertEqual(sorted(p.name for p in self.plan.glob('.*')), [])


class SchedulerTests(Base):
    def queue(self, verb, task, dwarf=None):
        """What `forge control` writes; liveness is the scheduler's own."""
        args = [verb, self.plan, task] + (['--dwarf', dwarf] if dwarf else []) + ['--json']
        result = self.control(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)['request']

    def test_a_stopped_task_is_retried_on_the_named_model_while_the_run_goes_on(self):
        self.task('x', status='FAIL')
        (self.plan / 'tasks/x/escalation.stop').write_text('3\tsol\n')
        self.task('a')
        self.task('b')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/tasks/x/merged"\n')
        proc = self.start('--retry-failed', 1)
        self.wait_for(lambda: (self.plan / 'scheduler.live').exists(), 'the scheduler to go live')
        request = self.queue('retry', 'x', 'sol:xhigh:codex')
        _, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual([self.status(n) for n in 'xab'], ['MERGED'] * 3)
        self.assertIn('x prepared --dwarf sol:xhigh:codex', self.events())
        self.assertIn('x start 1 trigger=human', self.events())
        self.assertFalse((self.plan / 'tasks/x/retries.used').exists())     # no automatic budget spent
        [line] = self.outcomes()
        self.assertEqual((line['request'], line['action'], line['task'], line['value'], line['outcome'], line['reason']),
                         (request, 'retry', 'x', 'sol:xhigh:codex', 'applied', None))
        self.assertIn('forge: control: retry x applied', err)
        self.assertEqual(self.requests(), [])

    def test_without_a_dwarf_the_policy_stop_refuses_the_retry(self):
        self.task('x', status='FAIL')
        (self.plan / 'tasks/x/escalation.stop').write_text('3\tsol\n')
        self.task('a')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/tasks/x/split.suggested"; sleep 0.3\n')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'scheduler.live').exists(), 'the scheduler to go live')
        self.queue('retry', 'x')
        proc.communicate(timeout=60)
        [line] = self.outcomes()
        self.assertEqual((line['outcome'], line['reason']), ('refused', 'split x, or name a --dwarf'))
        self.assertEqual(self.status('x'), 'FAIL')

    def test_a_retry_of_a_running_task_is_refused_and_logged(self):
        self.task('a')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/release"\n')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'tasks/a/calls').exists(), 'a to start')
        request = self.queue('retry', 'a')
        self.wait_for(lambda: self.outcomes(), 'the outcome line')
        (self.plan / 'release').write_text('')
        proc.communicate(timeout=60)
        [line] = self.outcomes()
        self.assertEqual((line['request'], line['outcome'], line['reason']), (request, 'refused', 'the task is running'))
        self.assertEqual(self.events().count('a start 1 trigger='), 1)

    def test_a_row_added_mid_run_runs_after_its_dependency(self):
        self.task('a')
        (self.plan / 'hooks/a.1').write_text('waitfor "$plan/release"\n')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'tasks/a/calls').exists(), 'a to start')
        self.rows.append('n\ta\tlow\tf_n\tsol:high:codex\topus:xhigh:claude\tN')
        self.task('n', row=False)
        self.write_tasks()
        self.queue('add', 'n')
        self.wait_for(lambda: self.outcomes(), 'the outcome line')
        self.assertEqual(self.status('n'), 'PENDING')        # its dependency is still running
        (self.plan / 'release').write_text('')
        _, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual((self.status('a'), self.status('n')), ('MERGED', 'MERGED'))
        events = self.events()
        self.assertLess(events.index('a merged'), events.index('n start 1 trigger='))
        self.assertIn('waves', events)
        self.assertEqual(self.outcomes()[0]['outcome'], 'applied')

    def test_a_request_left_by_a_dead_scheduler_is_refused_at_start(self):
        self.task('x', status='FAIL')
        self.task('a')
        self.live()
        request = self.queue('retry', 'x')
        (self.plan / 'scheduler.live').write_text(json.dumps({'pid': 999999, 'started': 1}))   # it died
        proc = self.start()
        proc.communicate(timeout=60)
        [line] = self.outcomes()
        self.assertEqual((line['request'], line['outcome'], line['reason']), (request, 'refused', 'left by an earlier run'))
        self.assertEqual(self.status('x'), 'FAIL')
        self.assertNotIn('x prepared', ' '.join(self.events()))

    def test_every_request_gets_exactly_one_outcome_even_at_shutdown(self):
        # The scheduler is held in its last merge and interrupted there: it leaves the loop
        # without another pass, so only live_stop can settle the request queued meanwhile.
        self.task('x', status='FAIL')
        self.task('a')
        (self.plan / 'hold-merge/a').write_text('')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'merging.a').exists(), 'the last merge')
        late = self.queue('retry', 'x')
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.2)
        (self.plan / 'gate').write_text('')
        proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 130)
        [line] = self.outcomes()
        self.assertEqual((line['request'], line['outcome'], line['reason']), (late, 'refused', 'run ended'))
        self.assertEqual(self.requests(), [])
        # The same at a normal exit: a request queued during the last merge is decided once.
        for f in ('gate', 'merging.a', 'tasks/a/merged', 'tasks/a/status', 'tasks/a/calls', 'control.log'):
            (self.plan / f).unlink(missing_ok=True)
        (self.plan / 'outcomes/a').write_text('PASS\n')
        proc = self.start()
        self.wait_for(lambda: (self.plan / 'merging.a').exists(), 'the last merge')
        ids = [self.queue('retry', 'x'), self.queue('retry', 'a')]
        (self.plan / 'gate').write_text('')
        proc.communicate(timeout=60)
        self.assertEqual(sorted(e['request'] for e in self.outcomes()), sorted(ids))
        self.assertEqual(self.requests(), [])


class EndToEndTests(unittest.TestCase):
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    plan = fixtures.ForgeTests.plan
    scenario = fixtures.ForgeTests.scenario

    def setUp(self):
        fixtures.ForgeTests.setUp(self)

    def test_control_retry_with_a_dwarf_against_a_live_run(self):
        p = self.plan('-')
        # a's first review fails; b keeps the run busy long enough to ask for the retry.
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P2] the change is wrong'},
                       'a.dwarf.2': {'content': 'fixed'}, 'b.dwarf.1': {'sleep': 8}})
        out = {}
        runner = threading.Thread(target=lambda: out.update(r=self.run_script('forge-parallel.sh', 'run', p)))
        runner.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not ((p / 'tasks/a/status').exists()
                                                   and (p / 'tasks/a/status').read_text().strip() == 'FAIL'):
            time.sleep(0.05)
        # The status is written before the task's process is done; a retry of a running task is refused.
        while time.monotonic() < deadline and subprocess.run(['/usr/bin/pgrep', '-f', f'_task {p} a$'],
                                                             stdout=subprocess.DEVNULL).returncode == 0:
            time.sleep(0.05)
        time.sleep(0.3)
        result = subprocess.run([sys.executable, str(CONTROL), 'retry', str(p), 'a', '--dwarf', 'opus:high:claude'],
                                capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        runner.join(120)
        self.assertEqual(out['r'].returncode, 0, out['r'].stdout)
        self.assertTrue((p / 'tasks/a/merged').exists())
        self.assertEqual([line.split('\t')[4] for line in (p / 'tasks.tsv').read_text().splitlines()
                          if line.startswith('a\t')], ['opus:high:claude'])
        record = (p / 'tasks/a/escalations.tsv').read_text().splitlines()[-1].split('\t')
        self.assertEqual(record[2:], ['sol', 'opus:high:claude', 'retry'])


if __name__ == '__main__':
    unittest.main()
