"""Verification through lanes (#32): `run`, `retry` and `integrate` verify with the lane runner when
lanes are declared, the verification files describe the lanes, and the run report, a whole-run
review's prompt and the dataset read them. Runs use test_forge.py's fake CLIs."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import os
from pathlib import Path
import signal
import subprocess
import time
import unittest

import test_dataset
import test_forge as fixtures
import test_interrupt
import test_review
from test_verify_reruns import fails_first

ROOT = Path(__file__).resolve().parents[1]


class LanesRunTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    plan = fixtures.ForgeTests.plan

    def lanes(self, **lanes):
        """Repository lanes in .forge/verify.d/, name -> command."""
        d = self.repo / '.forge' / 'verify.d'
        d.mkdir(parents=True, exist_ok=True)
        for name, command in lanes.items():
            (d / name).write_text(command + '\n')
        self.git('add', '.forge/verify.d')                 # a repository's lanes are part of it
        self.git('commit', '-qm', 'verification lanes')
        return d

    def lane_runs(self, plan):
        """{lane: [kind, ...]} from the 8-column runs.tsv rows."""
        runs = {}
        for line in (plan / 'verification.runs.tsv').read_text().splitlines():
            cells = line.split('\t')
            self.assertEqual(len(cells), 8, line)
            runs.setdefault(cells[6], []).append(cells[1])
        return runs

    def test_repo_lanes_with_a_flaky_lane_pass_and_say_so(self):
        self.scenario({})
        marks = self.root / 'marks'; marks.mkdir()
        self.lanes(unit='test -f a.txt', lint='echo clean', flappy=fails_first(marks, 'flappy'))
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((p / 'verification.status').read_text().strip(), 'PASS')
        self.assertEqual(self.lane_runs(p), {'flappy': ['initial', 'rerun'], 'lint': ['initial'], 'unit': ['initial']})
        flaky = (p / 'verification.flaky').read_text()
        self.assertIn('lanes=flappy\n', flaky)
        self.assertIn('flappy.first_exit=1\n', flaky)
        self.assertIn('flappy.retries_used=1\n', flaky)
        self.assertIn('FLAKY: lane flappy failed once and passed on rerun', r.stdout)
        self.assertEqual((p / 'verification.exit').read_text().strip(), '0')
        self.assertEqual(sorted((p / 'verification.command').read_text().splitlines()),
                         ['flappy: ' + fails_first(marks, 'flappy'), 'lint: echo clean', 'unit: test -f a.txt'])
        self.assertTrue((p / 'verification.fingerprint').read_text().strip())

    def test_a_lane_that_keeps_failing_fails_the_run_with_its_tail(self):
        self.scenario({})
        self.lanes(bad='echo boom-line; exit 4', good='echo fine')
        p = self.plan()
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual((p / 'verification.status').read_text().strip(), 'FAIL')
        self.assertEqual((p / 'verification.exit').read_text().strip(), '1')
        log = (p / 'verification.log').read_text()
        self.assertIn('== lane bad failed (exit 4)', log)
        self.assertIn('boom-line', log)
        self.assertNotIn('lane good failed', log)
        self.assertEqual(self.lane_runs(p), {'bad': ['initial', 'rerun'], 'good': ['initial']})
        self.assertIn('verification failed in lane bad', r.stdout)
        self.assertFalse((p / 'verification.flaky').exists())

    def test_retry_verifies_through_lanes(self):
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                       'a.dwarf.2': {'content': 'fixed'}})
        self.lanes(unit='test -f a.txt')
        p = self.plan('-')
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.lane_runs(p), {'unit': ['initial']})
        self.assertTrue((p / 'verification.lanes.json').exists())

    def test_integrate_verifies_through_lanes(self):
        self.scenario({})
        self.lanes(unit='test -f a.txt && test -f b.txt')
        p = self.plan()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        r = self.run_script('forge-parallel.sh', 'integrate', p, '--approved')
        self.assertEqual(r.returncode, 0, r.stdout)
        [out] = p.glob('integrate-*')
        self.assertEqual(self.lane_runs(out), {'unit': ['initial']})
        self.assertEqual((out / 'verification.status').read_text().strip(), 'PASS')


class LanesInterruptTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    plan = fixtures.ForgeTests.plan
    kill_group = staticmethod(test_interrupt.InterruptTests.kill_group)
    start, wait_for, pgrep, assertLockFree, finish = (
        test_interrupt.InterruptTests.start,
        test_interrupt.InterruptTests.wait_for, test_interrupt.InterruptTests.pgrep,
        test_interrupt.InterruptTests.assertLockFree, test_interrupt.InterruptTests.finish)

    def interrupted(self, command):
        self.scenario({})
        started = self.root / 'lane-started'
        unique = 'lane-interrupt-%d' % os.getpid()
        d = self.repo / '.forge' / 'verify.d'
        d.mkdir(parents=True)
        (d / 'slow').write_text(f'touch {started}; {command} # {unique}\n')
        (d / 'quick').write_text('echo quick\n')
        p = self.plan()
        run = self.start(p)
        self.wait_for(started.exists, what='the lane to start')
        os.kill(run.pid, signal.SIGTERM)
        self.assertEqual(self.finish(run, 30), 130, (self.root / 'run.out').read_text())
        self.assertEqual((p / 'verification.status').read_text().strip(), 'INTERRUPTED')
        self.assertEqual(self.pgrep(unique), [])
        self.assertLockFree(p)
        self.assertIn('interrupted during verification', (self.root / 'run.out').read_text())

    def test_sigterm_during_a_long_lane_stops_it_and_exits_130(self):
        self.interrupted('sleep 60')

    def test_a_lane_that_ignores_term_is_killed_too(self):
        self.interrupted("trap '' TERM; sleep 60")


class LanesReviewTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    plan_of, merged_plan, latest, review, text = (
        test_review.ReviewTests.plan_of, test_review.ReviewTests.merged_plan, test_review.ReviewTests.latest,
        test_review.ReviewTests.review, test_review.ReviewTests.text)

    def test_a_whole_run_review_names_each_lane_and_its_state(self):
        marks = self.root / 'marks'; marks.mkdir()
        d = self.repo / '.forge' / 'verify.d'
        d.mkdir(parents=True)
        (d / 'unit').write_text('true\n')
        (d / 'flappy').write_text(fails_first(marks, 'flappy') + '\n')
        p = self.merged_plan()
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        prompt = (self.latest(p) / 'qa.input').read_text()
        self.assertIn('unit: pass', prompt)
        self.assertIn('flappy: flaky', prompt)
        self.assertIn('FLAKY: lane flappy failed once and passed on rerun', prompt)
        self.assertNotIn('the command failed once', prompt)


class LanesDatasetTests(test_dataset.StoreCase):
    setUp_fixture = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    plan = fixtures.ForgeTests.plan

    def setUp(self):
        test_dataset.StoreCase.setUp(self)
        self.setUp_fixture()
        self.env['FORGE_DATA_HOME'] = str(self.home)

    def test_a_verify_run_event_carries_its_lane(self):
        self.scenario({})
        d = self.repo / '.forge' / 'verify.d'
        d.mkdir(parents=True)
        (d / 'unit').write_text('true\n')
        (d / 'lint').write_text('true\n')
        p = self.plan()
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        [verification] = self.events(kind='verification')
        self.assertEqual((verification['data']['command_source'], verification['data']['status']), ('lanes:repo', 'PASS'))
        self.assertEqual(sorted(e['data']['lane'] for e in self.events(kind='verify_run')), ['lint', 'unit'])
        self.assertEqual(self.errors(), '')


if __name__ == '__main__':
    unittest.main()
