"""forge-verify.py: parallel verification lanes, groups, first lanes, live state, interrupts."""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/forge-verify.py'


class VerifyLanesCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.checkout = self.base / 'checkout'
        self.lanes = self.base / 'lanes'
        self.out = self.base / 'out'
        self.marks = self.base / 'marks'
        for d in (self.checkout, self.lanes, self.marks):
            d.mkdir()
        self.git('init', '-q')
        (self.checkout / 'a.txt').write_text('one\n')
        self.git('add', '-A')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'init')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.checkout), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def lane(self, name, text):
        (self.lanes / name).write_text(text)

    def argv(self, command, *extra):
        return [sys.executable, str(SCRIPT), command, '--lanes-dir', str(self.lanes), *extra]

    def run_args(self, *extra):
        return self.argv('run', '--checkout', str(self.checkout), '--out', str(self.out), *extra)

    def run_verify(self, *extra, timeout=60):
        return subprocess.run(self.run_args(*extra), capture_output=True, text=True, timeout=timeout)

    def doc(self):
        return json.loads((self.out / 'verification.lanes.json').read_text())

    def lane_doc(self, name):
        return next(l for l in self.doc()['lanes'] if l['name'] == name)

    def rows(self):
        text = (self.out / 'verification.runs.tsv').read_text()
        return [line.split('\t') for line in text.splitlines()]

    def index_hash(self):
        return hashlib.sha256((self.checkout / '.git/index').read_bytes()).hexdigest()


class SchedulingTests(VerifyLanesCase):
    def test_independent_lanes_overlap(self):
        for name in 'abc':
            self.lane(name, 'sleep 1')
        t0 = time.monotonic()
        done = self.run_verify('--parallel', '3')
        took = time.monotonic() - t0
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertLess(took, 2.5)
        self.assertEqual(self.doc()['state'], 'pass')

    def test_parallel_one_runs_lanes_one_at_a_time(self):
        for name in 'abc':
            self.lane(name, 'sleep 0.3')
        self.assertEqual(self.run_verify('--parallel', '1').returncode, 0)
        spans = sorted((l['started'], l['ended']) for l in self.doc()['lanes'])
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(end, start)

    def test_group_lanes_never_overlap_but_others_do(self):
        self.lane('a', '#forge: group=browser\nsleep 0.6')
        self.lane('b', '#forge: group=browser\nsleep 0.6')
        self.lane('c', 'sleep 0.6')
        self.assertEqual(self.run_verify('--parallel', '3').returncode, 0)
        a, b, c = (self.lane_doc(n) for n in 'abc')
        self.assertLessEqual(a['ended'], b['started'])
        self.assertLess(c['started'], a['ended'])

    def test_lanes_start_in_declaration_order(self):
        for name in ('c', 'a', 'b'):
            self.lane(name, 'sleep 0.1')
        self.assertEqual(self.run_verify('--parallel', '1').returncode, 0)
        self.assertEqual([r[6] for r in self.rows()], ['a', 'b', 'c'])

    def test_failing_first_lane_skips_the_rest(self):
        self.lane('gate', '#forge: first\nexit 3')
        self.lane('later', f'touch {self.marks}/later')
        self.lane('other', f'touch {self.marks}/other')
        done = self.run_verify()
        self.assertEqual(done.returncode, 1)
        self.assertEqual(os.listdir(self.marks), [])
        self.assertEqual([l['state'] for l in self.doc()['lanes']], ['fail', 'skipped', 'skipped'])
        self.assertEqual(self.doc()['state'], 'fail')

    def test_first_lane_runs_before_the_rest(self):
        self.lane('gate', '#forge: first\nsleep 0.4')
        self.lane('a', 'true')
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertLessEqual(self.lane_doc('gate')['ended'], self.lane_doc('a')['started'])


class ResultTests(VerifyLanesCase):
    def test_failure_exits_1_and_rows_have_eight_columns(self):
        self.lane('bad', 'echo broken; exit 4')
        self.lane('good', 'echo fine')
        done = self.run_verify()
        self.assertEqual(done.returncode, 1)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(r) == 8 for r in rows))
        bad = next(r for r in rows if r[6] == 'bad')
        self.assertEqual(bad[:6], ['1', 'initial', '4', '0', '0',
                                   'verification.lane.bad.log'])
        float(bad[7])
        self.assertEqual(self.lane_doc('bad')['exit'], 4)
        self.assertIn('broken', (self.out / 'verification.lane.bad.log').read_text())
        self.assertIn('bad', done.stdout)

    def test_lane_runs_in_checkout_without_stdin(self):
        self.lane('here', f'pwd > {self.marks}/pwd; cat > {self.marks}/stdin')
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertEqual((self.marks / 'pwd').read_text().strip(), str(self.checkout))
        self.assertEqual((self.marks / 'stdin').read_text(), '')

    def test_lanes_json_describes_lanes(self):
        self.lane('unit', '#forge: group=g retries=2 reports=out baseline=b.json\necho hi')
        self.assertEqual(self.run_verify().returncode, 0)
        doc = self.doc()
        self.assertEqual((doc['version'], doc['state'], doc['parallel'], doc['mutated']),
                         (1, 'pass', 3, False))
        lane = doc['lanes'][0]
        self.assertEqual(lane['options'], {'retries': 2, 'reports': 'out', 'baseline': 'b.json'})
        self.assertEqual((lane['group'], lane['first'], lane['state'], lane['exit']),
                         ('g', False, 'pass', 0))
        self.assertEqual(lane['runs'][0]['kind'], 'initial')
        self.assertEqual(lane['runs'][0]['k'], 1)
        self.assertEqual(lane['log'], 'verification.lane.unit.log')
        self.assertEqual(lane['last_line'], 'hi')
        self.assertIsNone(lane['step'])
        self.assertIsNone(lane['tests'])

    def test_last_line_drops_ansi_and_is_capped(self):
        self.lane('x', "printf '\\033[31mred\\033[0m\\n'; printf 'y%.0s' $(seq 400); echo; echo")
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertEqual(self.lane_doc('x')['last_line'], 'y' * 300)
        self.lane('x', "printf '\\033[31mred\\033[0m\\n'")
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertEqual(self.lane_doc('x')['last_line'], 'red')


class MutationTests(VerifyLanesCase):
    def test_lane_writing_into_checkout_fails_verification(self):
        self.lane('writer', f'echo new > {self.checkout}/new.txt')
        done = self.run_verify()
        self.assertEqual(done.returncode, 1)
        self.assertTrue(self.doc()['mutated'])
        self.assertEqual(self.doc()['state'], 'fail')
        self.assertIn('cannot be pinned to one lane', done.stdout)

    def test_second_edit_of_a_modified_file_is_a_mutation(self):
        (self.checkout / 'a.txt').write_text('two\n')
        self.lane('edit', f'echo three > {self.checkout}/a.txt')
        before = self.index_hash()
        self.assertEqual(self.run_verify().returncode, 1)
        self.assertTrue(self.doc()['mutated'])
        self.assertEqual(self.index_hash(), before)

    def test_clean_lanes_leave_index_and_status_alone(self):
        (self.checkout / 'a.txt').write_text('two\n')
        (self.checkout / 'untracked.txt').write_text('u\n')
        self.lane('noop', 'true')
        before = self.index_hash()
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertFalse(self.doc()['mutated'])
        self.assertEqual(self.index_hash(), before)
        status = self.git('status', '--porcelain')
        self.assertIn('a.txt', status); self.assertIn('?? untracked.txt', status)

    def test_custom_fingerprint_command_decides(self):
        self.lane('writer', f'echo new > {self.checkout}/new.txt')
        self.assertEqual(self.run_verify('--fingerprint-cmd', 'echo same').returncode, 0)
        self.assertFalse(self.doc()['mutated'])
        self.lane('writer', f'date +%s%N > {self.marks}/t')
        done = self.run_verify('--fingerprint-cmd', f'cat {self.marks}/t 2>/dev/null || echo none')
        self.assertEqual(done.returncode, 1)
        self.assertTrue(self.doc()['mutated'])


class LiveStateTests(VerifyLanesCase):
    def test_lanes_json_shows_running_step_and_last_line(self):
        self.lane('slow', 'echo compiling\necho "::forge step name=browser state=start"\n'
                          'echo "page 3/9"\nsleep 4\n'
                          'echo "::forge step name=browser state=end exit=0"')
        proc = subprocess.Popen(self.run_args(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        seen = None
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline and not seen:
            try:
                lane = self.lane_doc('slow')
            except (FileNotFoundError, ValueError, StopIteration):
                lane = None
            if lane and lane['state'] == 'running' and lane['step'] and lane['last_line'] == 'page 3/9':
                seen = lane
            time.sleep(0.05)
        self.assertIsNotNone(seen, 'lanes.json never showed the running step')
        self.assertEqual(seen['step']['name'], 'browser')
        self.assertEqual(self.doc()['state'], 'running')
        self.assertIsNotNone(seen['last_output_at'])
        proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0)
        final = self.lane_doc('slow')
        self.assertEqual((final['state'], final['step']), ('pass', None))
        self.assertIn('state=start', (self.out / 'verification.lane.slow.log').read_text())

    def test_step_end_clears_the_step(self):
        self.lane('s', 'echo "::forge step name=a state=start"; echo "::forge step name=a state=end exit=0"; sleep 0.2')
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertIsNone(self.lane_doc('s')['step'])


class ConfigurationTests(VerifyLanesCase):
    def assert_config_error(self, expect):
        self.lane('ok', f'touch {self.marks}/ran')
        done = self.run_verify()
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertEqual(os.listdir(self.marks), [])
        self.assertFalse((self.out / 'verification.lanes.json').exists())
        self.assertIn(expect, done.stderr)
        listed = subprocess.run(self.argv('lanes'), capture_output=True, text=True)
        self.assertEqual(listed.returncode, 2)

    def test_bad_name(self):
        self.lane('has.dot', 'true')
        self.assert_config_error('has.dot')

    def test_unknown_key(self):
        self.lane('bad', '#forge: speed=fast\ntrue')
        self.assert_config_error('speed')

    def test_retries_not_a_number(self):
        self.lane('bad', '#forge: retries=x\ntrue')
        self.assert_config_error('retries=x')

    def test_retries_out_of_range(self):
        self.lane('bad', '#forge: retries=10\ntrue')
        self.assert_config_error('retries=10')

    def test_empty_command(self):
        self.lane('bad', '#forge: first\n\n')
        self.assert_config_error('empty command')

    def test_symlinked_lane(self):
        target = self.base / 'elsewhere'
        target.write_text('true')
        (self.lanes / 'link').symlink_to(target)
        self.assert_config_error('link')

    def test_directory_lane(self):
        (self.lanes / 'sub').mkdir()
        self.assert_config_error('sub')

    def test_no_lanes(self):
        done = self.run_verify()
        self.assertEqual(done.returncode, 2)

    def test_parallel_must_be_positive(self):
        self.lane('a', 'true')
        self.assertEqual(self.run_verify('--parallel', '0').returncode, 2)

    def test_dotfiles_and_backups_are_skipped(self):
        self.lane('a', 'true')
        self.lane('.hidden', 'true')
        self.lane('a~', 'true')
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertEqual([l['name'] for l in self.doc()['lanes']], ['a'])

    def test_lanes_json_lists_a_valid_directory(self):
        self.lane('b', '#forge: first group=g retries=3\necho b')
        self.lane('a', 'echo a')
        done = subprocess.run(self.argv('lanes', '--json'), capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        lanes = json.loads(done.stdout)['lanes']
        self.assertEqual([l['name'] for l in lanes], ['a', 'b'])
        self.assertEqual((lanes[1]['first'], lanes[1]['group'], lanes[1]['options']),
                         (True, 'g', {'retries': 3}))
        self.assertEqual(lanes[1]['command'], 'echo b')
        self.assertEqual((lanes[0]['first'], lanes[0]['group'], lanes[0]['options']), (False, None, {}))


class InterruptTests(VerifyLanesCase):
    def wait_for(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while not path.exists():
            self.assertLess(time.monotonic(), deadline, f'{path} never appeared')
            time.sleep(0.05)

    def test_sigterm_stops_lane_process_groups_and_exits_130(self):
        self.lane('long', f'echo $$ > {self.marks}/pid; sleep 120; echo after')
        self.lane('stubborn', f'trap "" TERM; echo $$ > {self.marks}/pid2; sleep 120; sleep 120')
        self.lane('zlast', f'touch {self.marks}/never')
        proc = subprocess.Popen(self.run_args('--parallel', '2'), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        self.wait_for(self.marks / 'pid'); self.wait_for(self.marks / 'pid2')
        time.sleep(0.3)
        pids = [int((self.marks / n).read_text()) for n in ('pid', 'pid2')]
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 130)
        for pid in pids:
            with self.assertRaises(ProcessLookupError):
                os.killpg(pid, 0)
        doc = self.doc()
        self.assertEqual(doc['state'], 'interrupted')
        self.assertEqual([l['state'] for l in doc['lanes']], ['interrupted'] * 3)
        self.assertFalse((self.marks / 'never').exists())
        self.assertNotIn('after', (self.out / 'verification.lane.long.log').read_text())


if __name__ == '__main__':
    unittest.main()
