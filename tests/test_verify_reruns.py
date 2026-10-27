"""Per-lane reruns in forge-verify.py (#32): only a failed lane runs again, within its budget,
in rounds under the same parallel and group rules; a pass on a rerun is flaky. Lane state
that must survive a rerun (how many times a lane ran) lives in marker files outside the
checkout, so the checkout itself is never changed."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import signal
import subprocess
import time
import unittest

from test_verify_lanes import VerifyLanesCase


def fails_first(marks, name, times=1):
    """A lane command that fails its first `times` runs, then passes."""
    counter = marks / ('count.' + name)
    return (f'n=$(( $(cat {counter} 2>/dev/null || echo 0) + 1 )); echo $n > {counter}; '
            f'echo "{name} run $n"; [ $n -gt {times} ]')


class RerunTests(VerifyLanesCase):
    def runs(self, name):
        return self.lane_doc(name)['runs']

    def test_a_flaky_lane_is_rerun_alone_and_the_verification_passes(self):
        self.lane('flappy', fails_first(self.marks, 'flappy'))
        self.lane('good', 'echo fine')
        done = self.run_verify()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.doc()['state'], 'pass')
        lane = self.lane_doc('flappy')
        self.assertEqual((lane['state'], lane['exit']), ('flaky', 0))
        self.assertEqual([(r['k'], r['kind'], r['exit'], r['log']) for r in lane['runs']],
                         [(1, 'initial', 1, 'verification.lane.flappy.run-1.log'),
                          (2, 'rerun', 0, 'verification.lane.flappy.run-2.log')])
        self.assertEqual(len(self.runs('good')), 1)
        self.assertIn('flappy run 1', (self.out / 'verification.lane.flappy.run-1.log').read_text())
        second = (self.out / 'verification.lane.flappy.run-2.log').read_text()
        self.assertIn('flappy run 2', second)
        self.assertNotIn('flappy run 1', second)            # a rerun starts a fresh log
        self.assertTrue((self.out / 'verification.lane.good.run-1.log').exists())
        rows = self.rows()
        self.assertEqual(sorted((r[6], r[1]) for r in rows), [('flappy', 'initial'), ('flappy', 'rerun'), ('good', 'initial')])
        self.assertEqual(sorted(int(r[0]) for r in rows), [1, 2, 3])
        self.assertIn('flaky', done.stdout)

    def test_the_lanes_own_budget_overrides_the_flag(self):
        self.lane('broken', '#forge: retries=2\necho no; exit 3')
        done = self.run_verify('--retries', '0')
        self.assertEqual(done.returncode, 1)
        self.assertEqual([r['kind'] for r in self.runs('broken')], ['initial', 'rerun', 'rerun'])
        self.assertEqual(self.lane_doc('broken')['state'], 'fail')

    def test_no_reruns_with_a_zero_budget(self):
        self.lane('flappy', fails_first(self.marks, 'flappy'))
        done = self.run_verify('--retries', '0')
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(self.runs('flappy')), 1)
        self.assertEqual(self.lane_doc('flappy')['state'], 'fail')

    def test_reruns_run_in_parallel_but_never_two_of_one_group(self):
        for name in ('a', 'b'):
            self.lane(name, 'sleep 1; ' + fails_first(self.marks, name))
        for name in ('g1', 'g2'):
            self.lane(name, '#forge: group=db\nsleep 1; ' + fails_first(self.marks, name))
        done = self.run_verify('--parallel', '4')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        a, b = self.runs('a')[1], self.runs('b')[1]
        self.assertLess(max(a['started'], b['started']), min(a['ended'], b['ended']))   # they overlapped
        g1, g2 = self.runs('g1')[1], self.runs('g2')[1]
        self.assertTrue(g1['ended'] <= g2['started'] or g2['ended'] <= g1['started'], (g1, g2))

    def test_no_rerun_after_the_checkout_changed(self):
        self.lane('writer', 'echo changed > a.txt; exit 1')
        done = self.run_verify('--retries', '2')
        self.assertEqual(done.returncode, 1)
        self.assertEqual(len(self.runs('writer')), 1)
        self.assertTrue(self.doc()['mutated'])

    def test_a_flaky_first_lane_lets_the_others_run(self):
        self.lane('setup', '#forge: first\n' + fails_first(self.marks, 'setup'))
        self.lane('unit', f'touch {self.marks}/unit-ran')
        done = self.run_verify()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.lane_doc('setup')['state'], 'flaky')
        self.assertTrue((self.marks / 'unit-ran').exists())
        self.assertGreater(self.runs('unit')[0]['started'], self.runs('setup')[1]['ended'])

    def test_sigterm_during_a_rerun_exits_130_and_leaves_no_process(self):
        self.lane('slow', fails_first(self.marks, 'slow') + f' || exit 1; echo $$ > {self.marks}/pid; sleep 300')
        proc = subprocess.Popen(self.run_args(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 20
        while not (self.marks / 'pid').exists():
            self.assertLess(time.monotonic(), deadline, 'the rerun never started')
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 130)
        lane = self.lane_doc('slow')
        self.assertEqual((lane['state'], [r['kind'] for r in lane['runs']]), ('interrupted', ['initial', 'rerun']))
        pgid = int((self.marks / 'pid').read_text())
        self.assertEqual(subprocess.run(['/bin/ps', '-g', str(pgid), '-o', 'pid='],
                                        capture_output=True, text=True).stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
