"""No guarded harness outlives an interrupted run (#29).

The real scheduler runs a one-task plan whose `_task` is the real forge-dispatch.sh with
the orphan guard on. The fake harness leads its own session, as a real one does, and is
slow to exit on TERM, so only a dispatch that is given time to finish its EXIT trap, or the
scheduler's record-driven backstop, can stop it."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest

import test_forge as fixtures
from test_dispatch import HELP

ROOT = Path(__file__).resolve().parents[1]

# Writes its pid, then runs for a minute. TERM is answered only after 30 s, far longer than
# the scheduler's grace: a harness still alive after the grace was never KILLed by anyone.
HARNESS = '''#!/usr/bin/env python3
import os, signal, sys, time
if '--help' in sys.argv: print(%r); sys.exit()
signal.signal(signal.SIGTERM, lambda *_: (time.sleep(30), sys.exit(143)))
open(os.environ['HARNESS_PID'], 'w').write(str(os.getpid()))
time.sleep(60)
''' % HELP

WORKER = r'''#!/bin/bash
cmd="$1"; plan="$2"; name="$3"; t="$plan/tasks/$name"
case "$cmd" in
  _task)
    echo $$ > "$t/task.pid"
    echo RUNNING > "$t/status"
    bash "$DISPATCH" dwarf sol --repo "$REPO" --run-dir "$t" --prompt-file "$PROMPT"
    echo FAIL > "$t/status"
    ;;
esac
'''


def alive(pid):
    state = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'stat='], stdout=subprocess.PIPE, text=True).stdout.strip()
    return bool(state) and not state.startswith('Z')


def wait_until(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class InterruptHarnessTests(unittest.TestCase):
    GRACE = 4
    git = fixtures.ForgeTests.git

    def setUp(self):
        fixtures.ForgeTests.setUp(self)
        harness = self.bin / 'codex'
        harness.write_text(HARNESS); harness.chmod(0o755)
        self.plan = self.root / 'plan'
        (self.plan / 'tasks' / 'a').mkdir(parents=True)
        (self.plan / 'tasks' / 'a' / 'base_ref').write_text('base\n')
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        (self.plan / 'wt_root').write_text(str(self.root))
        prompt = self.root / 'prompt.md'; prompt.write_text('Implement change.\n')
        self.worker = self.root / 'worker.sh'; self.worker.write_text(WORKER)
        self.pidfile = self.root / 'harness.pid'
        self.env.update(DISPATCH=str(ROOT / 'scripts' / 'forge-dispatch.sh'), REPO=str(self.repo),
                        PROMPT=str(prompt), HARNESS_PID=str(self.pidfile),
                        FORGE_INTERRUPT_GRACE=str(self.GRACE), FORGE_ORPHAN_GUARD='on',
                        FORGE_CAPABILITY_CACHE=str(self.root / 'capabilities'))
        self.env.pop('FORGE_FRACTAL_RUN', None)
        self.addCleanup(self.kill_harness)

    def kill_harness(self):
        if self.pidfile.exists():
            pid = int(self.pidfile.read_text())
            for target in (lambda: os.killpg(pid, signal.SIGKILL), lambda: os.kill(pid, signal.SIGKILL)):
                try:
                    target()
                except OSError:
                    pass

    def scheduler(self):
        return subprocess.Popen([sys.executable, str(ROOT / 'scripts' / 'forge-schedule.py'), str(self.worker),
                                 str(self.plan), '1'], env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def harness_started(self):
        record = self.plan / 'tasks' / 'a' / 'dwarf.harness'
        self.assertTrue(wait_until(self.pidfile.exists, 30), 'the fake harness never started')
        pid = int(self.pidfile.read_text())
        wait_until(record.exists, 10)   # what the backstop reads; its absence shows below
        return pid

    def status(self):
        return (self.plan / 'tasks' / 'a' / 'status').read_text().strip()

    def test_an_interrupt_stops_the_harness_before_the_scheduler_exits(self):
        scheduler = self.scheduler()
        harness = self.harness_started()
        scheduler.send_signal(signal.SIGTERM)
        out, _ = scheduler.communicate(timeout=self.GRACE + 20)
        self.assertEqual(scheduler.returncode, 130, out)
        self.assertTrue(wait_until(lambda: not alive(harness), 3), 'the harness outlived the interrupted run')
        self.assertEqual(self.status(), 'INTERRUPTED')
        self.assertEqual(sorted(p.name for p in (self.plan / 'tasks' / 'a').glob('*.harness')), [])

    def test_a_harness_leaked_by_a_killed_scheduler_is_stopped_at_the_next_start(self):
        scheduler = self.scheduler()
        harness = self.harness_started()
        task = int((self.plan / 'tasks' / 'a' / 'task.pid').read_text())
        # Everything but the harness dies at once, as when the whole run is SIGKILLed: no
        # EXIT trap runs, and the harness, alone in its own session, survives.
        scheduler.kill(); scheduler.communicate()
        os.killpg(task, signal.SIGKILL)
        self.assertTrue(wait_until(lambda: not alive(task), 5))
        self.assertTrue(alive(harness))
        self.assertEqual(self.status(), 'RUNNING')
        self.worker.write_text('#!/bin/bash\nexit 0\n')   # the second scheduler dispatches nothing
        second = self.scheduler()
        out, _ = second.communicate(timeout=30)
        self.assertTrue(wait_until(lambda: not alive(harness), 3), 'the leaked harness is still running\n' + out)
        self.assertEqual(self.status(), 'INTERRUPTED')
        self.assertFalse((self.plan / 'tasks' / 'a' / 'dwarf.harness').exists())


if __name__ == '__main__':
    unittest.main()
