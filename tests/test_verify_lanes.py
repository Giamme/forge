"""forge-verify.py: parallel verification lanes, groups, first lanes, live state, interrupts."""
import hashlib
import contextlib
import importlib.util
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
        done = self.run_verify('--parallel', '3')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lanes = [self.lane_doc(name) for name in 'abc']
        self.assertLess(max(l['started'] for l in lanes), min(l['ended'] for l in lanes))   # all three at once
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

    def test_a_group_waits_for_what_its_last_lane_left_running(self):
        gone = self.marks / 'a.gone'
        # a leaves a server that takes a while to shut down once TERMed at a's end
        self.lane('a', '#forge: group=db\n'
                  f"( trap 'sleep 1; touch {gone}; exit 0' TERM; while :; do sleep 0.1; done ) &\necho started")
        self.lane('b', f'#forge: group=db\ntest -f {gone}')
        done = self.run_verify('--parallel', '2', '--retries', '0')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.lane_doc('b')['state'], 'pass')

    def test_logs_left_as_symlinks_in_out_are_not_written_through(self):
        outside = self.base / 'outside.txt'; outside.write_text('keep me\n')
        self.out.mkdir()
        (self.out / 'verification.lane.unit.log').symlink_to(outside)
        (self.out / 'verification.lane.unit.run-1.log').symlink_to(self.base / 'dangling.txt')
        self.lane('unit', 'echo failing; exit 1')
        self.assertEqual(self.run_verify().returncode, 1)          # with its default rerun
        self.assertEqual(outside.read_text(), 'keep me\n')
        self.assertFalse((self.base / 'dangling.txt').exists())
        self.assertEqual(len(self.lane_doc('unit')['runs']), 2)

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
        done = self.run_verify('--retries', '0')   # reruns have their own tests (test_verify_reruns.py)
        self.assertEqual(done.returncode, 1)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(r) == 8 for r in rows))
        bad = next(r for r in rows if r[6] == 'bad')
        self.assertEqual(bad[:6], ['1', 'initial', '4', '0', '0',
                                   'verification.lane.bad.run-1.log'])
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
        (self.checkout / 'b.json').write_text('')   # run checks that the baseline exists
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

    def test_nul_byte_in_a_lane(self):
        (self.lanes / 'bad').write_bytes(b'echo hi\0there\n')
        self.assert_config_error('NUL')

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

    def test_sigterm_during_a_slow_fingerprint_stops_it_and_exits_130(self):
        self.lane('unit', f'touch {self.marks}/never')
        fp = f'echo $$ > {self.marks}/fp; sleep 600; echo stable'
        proc = subprocess.Popen(self.run_args('--fingerprint-cmd', fp), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        self.wait_for(self.marks / 'fp')
        time.sleep(0.2)
        pid = int((self.marks / 'fp').read_text())
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
        self.assertLess(time.monotonic() - started, 8)
        self.assertEqual(proc.returncode, 130)
        with self.assertRaises(ProcessLookupError):
            os.killpg(pid, 0)
        self.assertEqual((self.doc()['state'], self.lane_doc('unit')['state']), ('interrupted', 'interrupted'))
        self.assertFalse((self.marks / 'never').exists())

    def test_sigterm_is_honoured_while_a_lane_floods_its_log(self):
        self.lane('flood', 'yes xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
                           ' | head -c 300000000')
        self.lane('quick', 'sleep 0.2')
        proc = subprocess.Popen(self.run_args('--parallel', '2'), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        log = self.out / 'verification.lane.flood.log'
        deadline = time.monotonic() + 30
        while not log.exists() or log.stat().st_size < 200_000_000:
            self.assertLess(time.monotonic(), deadline, 'the flood never got going')
            time.sleep(0.05)
        self.assertEqual(self.lane_doc('quick')['state'], 'pass')  # reaped while the flood runs
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 130)
        self.assertEqual(self.doc()['state'], 'interrupted')
        self.assertEqual(self.lane_doc('flood')['state'], 'interrupted')

    def test_background_writer_of_a_finished_lane_is_stopped_before_its_log_is_drained(self):
        self.lane('bg', 'yes xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
                        f' & echo $! > {self.marks}/pid; echo done')
        proc = subprocess.Popen(self.run_args(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        self.addCleanup(proc.kill)
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            # $! is the writer's pid; its group is the lane's, led by the lane's shell.
            writer = int((self.marks / 'pid').read_text())
            with contextlib.suppress(OSError):
                os.killpg(os.getpgid(writer), signal.SIGKILL)
            with contextlib.suppress(OSError):
                os.kill(writer, signal.SIGKILL)
            self.fail('the leftover writer was not stopped; the run never finished')
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.lane_doc('bg')['state'], 'pass')
        self.assertEqual(len(self.rows()), 1)

    def test_draining_one_lanes_backlog_does_not_hold_up_the_others(self):
        written = self.marks / 'written'
        self.lane('a_flood', 'yes xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
                             f' | head -c 400000000; python3 -c "import time; print(time.time())" > {written}')
        self.lane('b', 'sleep 1')
        self.lane('c', f'touch {self.marks}/c')
        done = self.run_verify('--parallel', '2', timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # Measured against the events, not the wall clock: on a busy machine the 400 MB alone take a while.
        b, c = self.lane_doc('b'), self.lane_doc('c')
        self.assertLess(c['started'] - b['ended'], 1.5)       # when b ends, not when the flood is read
        # The finished lane's backlog is not read through at all: it ends with its writer,
        # however slowly a capped reader would get through 400 MB.
        flood = self.lane_doc('a_flood')
        self.assertLess(flood['ended'] - float(written.read_text()), 1.5)
        self.assertEqual(flood['last_line'], 'x' * 49)   # head cuts the last line: 400000000 % 81


def alive(pgid):
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


class CleanupTests(VerifyLanesCase):
    def gone(self, pgid, timeout=3):
        self.addCleanup(lambda: alive(pgid) and os.killpg(pgid, signal.SIGKILL))
        deadline = time.monotonic() + timeout
        while alive(pgid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not alive(pgid)

    def test_background_process_of_a_finished_lane_does_not_outlive_the_script(self):
        self.lane('bg', f'echo $$ > {self.marks}/pid; sleep 300 & echo started')
        done = self.run_verify()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        pid = int((self.marks / 'pid').read_text())
        self.assertTrue(self.gone(pid), 'the lane\'s background sleep is still running')

    def test_background_process_ignoring_term_is_killed(self):
        self.lane('bg', f'echo $$ > {self.marks}/pid; (trap "" TERM; exec sleep 300) & echo started')
        done = self.run_verify()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        pid = int((self.marks / 'pid').read_text())
        self.assertTrue(self.gone(pid), 'the TERM-ignoring background process survived')

    def test_unexpected_error_kills_lanes_and_finishes_lanes_json(self):
        spec = importlib.util.spec_from_file_location('forge_verify', SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        real, failed = mod.write_atomic, []

        def flaky(path, text):
            if path.name == 'verification.lanes.json' and not failed and '"last_line": "hi"' in text:
                failed.append(1)
                raise OSError(28, 'No space left on device')
            real(path, text)

        mod.write_atomic = flaky
        self.lane('long', f'echo $$ > {self.marks}/pid; echo hi; sleep 300')
        self.lane('zlater', f'touch {self.marks}/never')
        lanes, errors = mod.load_lanes(str(self.lanes))
        self.assertEqual(errors, [])
        verification = mod.Verification(str(self.checkout), str(self.out), lanes, 1)
        self.assertEqual(verification.run(), 1)
        self.assertTrue(failed)
        pid = int((self.marks / 'pid').read_text())
        self.assertTrue(self.gone(pid), 'the lane survived the error')
        doc = self.doc()
        self.assertEqual(doc['state'], 'fail')
        self.assertEqual([l['state'] for l in doc['lanes']], ['interrupted', 'skipped'])
        self.assertFalse((self.marks / 'never').exists())


class PreRunTests(VerifyLanesCase):
    def stale(self):
        self.out.mkdir()
        (self.out / 'verification.lanes.json').write_text('{"state": "running", "lanes": []}')

    def test_failing_fingerprint_exits_2_and_does_not_leave_running_state(self):
        self.stale()
        self.lane('a', f'touch {self.marks}/ran')
        done = self.run_verify('--fingerprint-cmd', 'exit 1')
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn('cannot fingerprint', done.stderr)
        self.assertFalse((self.marks / 'ran').exists())
        doc = self.doc()
        self.assertEqual(doc['state'], 'fail')
        self.assertEqual([l['state'] for l in doc['lanes']], ['skipped'])

    def test_non_git_checkout_is_not_left_running(self):
        self.stale()
        self.lane('a', 'true')
        plain = self.base / 'plain'; plain.mkdir()
        done = subprocess.run(self.argv('run', '--checkout', str(plain), '--out', str(self.out)),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertNotEqual(self.doc()['state'], 'running')

    def test_fingerprint_output_that_is_not_utf8_is_accepted(self):
        self.lane('a', 'true')
        done = self.run_verify('--fingerprint-cmd', "printf '\\xff\\xfe'")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertNotIn('Traceback', done.stderr)
        self.assertEqual(self.doc()['state'], 'pass')


class OutputFileTests(VerifyLanesCase):
    def test_multibyte_character_split_across_reads_is_kept_whole(self):
        self.lane('a', "printf 'caf\\xc3'; sleep 0.5; printf '\\xa9 ok\\n'")
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertEqual(self.lane_doc('a')['last_line'], 'caf\u00e9 ok')

    def test_state_files_get_the_umask_mode_like_the_logs(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        self.lane('a', 'echo hi')
        self.assertEqual(self.run_verify().returncode, 0)
        for name in ('verification.lane.a.log', 'verification.lanes.json', 'verification.runs.tsv'):
            self.assertEqual((self.out / name).stat().st_mode & 0o777, 0o644, name)


if __name__ == '__main__':
    unittest.main()
