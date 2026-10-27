"""Lane test reports and baselines in forge-verify.py (#32, #33): JUnit and TAP parsing,
per-run counts and failing tests with their log line, and `known` lanes whose every failure,
on complete evidence, is in the baseline. The checkout ignores reports/ in git, as a real
project's report directory is, so writing reports is not a change to the tree."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from test_verify_lanes import VerifyLanesCase

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'verify-reports'
spec = importlib.util.spec_from_file_location('forge_verify_reports', ROOT / 'scripts/forge_verify_reports.py')
reports = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reports)


def junit(failures, passes=0):
    cases = ''.join(f'<testcase classname="big" name="f{i}"><failure message="m{i}">t{i}</failure></testcase>'
                    for i in range(failures))
    cases += ''.join(f'<testcase classname="big" name="p{i}"/>' for i in range(passes))
    return f'<testsuite name="big">{cases}</testsuite>'


class ParserTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def scan(self, *names):
        for name in names:
            shutil.copy(FIXTURES / name, self.dir / name)
        return reports.scan(str(self.dir), 0)

    def test_junit(self):
        tests, names, complete = self.scan('junit.xml')
        self.assertTrue(complete)
        self.assertEqual({k: tests[k] for k in ('pass', 'fail', 'skip', 'error')},
                         {'pass': 2, 'fail': 1, 'skip': 1, 'error': 1})
        self.assertEqual(names, ['calc.DivTests.test_divides', 'calc.DivTests.test_zero'])
        divides, zero = tests['failing']
        self.assertEqual((divides['suite'], divides['file'], divides['message']), ('calc', 'junit.xml', 'expected 2, got 3'))
        self.assertIn('line 9, in divide', divides['excerpt'])
        self.assertIn('dividing 6 by 3', divides['excerpt'])        # system-out joins the excerpt
        self.assertEqual(zero['message'], 'ZeroDivisionError: division by zero')
        self.assertEqual((divides['known'], divides['log_line']), (False, None))
        self.assertEqual((tests['reports'], tests['notes'], tests['truncated']), (['junit.xml'], [], False))

    def test_tap(self):
        tests, names, complete = self.scan('tap13.tap')
        self.assertTrue(complete)
        self.assertEqual({k: tests[k] for k in ('pass', 'fail', 'skip', 'error')},
                         {'pass': 2, 'fail': 1, 'skip': 2, 'error': 0})
        self.assertEqual(names, ['rejects a bad header'])
        [failing] = tests['failing']
        self.assertEqual(failing['message'], 'expected an error')
        self.assertIn('file: parser.js', failing['excerpt'])

    def test_a_utf16_report_is_refused_before_it_is_parsed(self):
        doc = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE t [<!ENTITY e "expanded">]>'
               '<testsuite><testcase name="a"><failure>&e;</failure></testcase></testsuite>')
        (self.dir / 'r.xml').write_bytes(doc.encode('utf-16'))
        tests, names, complete = reports.scan(str(self.dir), 0)
        self.assertEqual((complete, names, tests['reports']), (False, [], []))
        self.assertIn('only UTF-8 reports are read', ' '.join(tests['notes']))

    def test_a_tap_stream_that_bailed_out_or_misses_its_plan_is_incomplete(self):
        cases = {
            'bailed out': ('1..2\nnot ok 1 - known_failure\nBail out! database unavailable\n', 'bailed out (database unavailable)'),
            'short of its plan': ('1..2\nnot ok 1 - known_failure\n', 'its plan says 2 tests and it has 1'),
            'no plan': ('not ok 1 - known_failure\n', 'no plan line'),
        }
        for what, (text, note) in cases.items():
            with self.subTest(what):
                (self.dir / 'r.tap').write_text(text)
                tests, names, complete = reports.scan(str(self.dir), 0)
                self.assertFalse(complete)
                self.assertEqual(names, ['known_failure'])            # its failures still show
                self.assertIn(note, ' '.join(tests['notes']))

    def test_malformed_and_doctype_reports_are_skipped_with_a_note(self):
        tests, names, complete = self.scan('malformed.xml', 'doctype.xml', 'junit.xml')
        self.assertFalse(complete)
        self.assertEqual(tests['reports'], ['junit.xml'])
        notes = ' '.join(tests['notes'])
        self.assertIn('malformed.xml skipped', notes)
        self.assertIn('doctype.xml skipped: DOCTYPE or ENTITY declarations are refused', notes)

    def test_caps_on_failing_messages_and_excerpts(self):
        long = 'x' * 3000
        # Sorted first, so it is among the 200 kept before the cap fills.
        (self.dir / 'a-long.xml').write_text(f'<testsuite><testcase name="t"><failure message="{long}">{long}</failure>'
                                           '</testcase></testsuite>')
        (self.dir / 'big.xml').write_text(junit(201))
        tests, names, complete = reports.scan(str(self.dir), 0)
        self.assertEqual(len(names), 202)                            # every failure, for the verdict
        self.assertEqual(len(tests['failing']), 200)
        self.assertTrue(tests['truncated'])
        entry = next(f for f in tests['failing'] if f['name'] == 't')
        self.assertEqual((len(entry['message']), len(entry['excerpt'])), (500, 2000))

    def test_reports_older_than_the_run_and_symlinks_are_left_out(self):
        shutil.copy(FIXTURES / 'junit.xml', self.dir / 'old.xml')
        os.utime(self.dir / 'old.xml', (100, 100))
        (self.dir / 'link.tap').symlink_to(FIXTURES / 'tap13.tap')
        tests, names, complete = reports.scan(str(self.dir), 1000)
        self.assertEqual((tests['reports'], names, complete), ([], [], True))


class LaneReportTests(VerifyLanesCase):
    def setUp(self):
        super().setUp()
        (self.checkout / '.gitignore').write_text('reports/\n')
        self.git('add', '.gitignore')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'ignore reports')

    def writes(self, name, *, text=None, fixture=None, exit_code=1, before=''):
        """A lane that prints `before`, writes one report into reports/, and exits."""
        source = self.base / f'src-{name}'
        source.write_text(text if text is not None else (FIXTURES / fixture).read_text())
        return f'{before}mkdir -p reports; cp {source} reports/{name}; exit {exit_code}'

    def baseline(self, *names):
        (self.checkout / 'known.txt').write_text('# known failures\n\n' + '\n'.join(names) + '\n')
        self.git('add', 'known.txt')
        self.git('-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'baseline')

    def test_a_lane_writing_a_report_gets_its_counts_and_failing_tests(self):
        self.lane('unit', '#forge: reports=reports retries=0\n'
                  + self.writes('r.xml', fixture='junit.xml', before='echo starting; echo running calc.DivTests.test_zero; '))
        done = self.run_verify()
        self.assertEqual(done.returncode, 1)
        tests = self.lane_doc('unit')['tests']
        self.assertEqual((tests['pass'], tests['fail'], tests['error'], tests['skip']), (2, 1, 1, 1))
        lines = {f['name']: f['log_line'] for f in tests['failing']}
        self.assertEqual(lines, {'calc.DivTests.test_divides': None, 'calc.DivTests.test_zero': 2})
        self.assertIn('tests 2 passed, 2 failed, 1 skipped', done.stdout)

    def test_a_report_left_by_an_earlier_run_does_not_count(self):
        (self.checkout / 'reports').mkdir()
        shutil.copy(FIXTURES / 'junit.xml', self.checkout / 'reports' / 'old.xml')
        os.utime(self.checkout / 'reports' / 'old.xml', (100, 100))
        self.lane('unit', '#forge: reports=reports\necho fine')
        self.assertEqual(self.run_verify().returncode, 0)
        self.assertIsNone(self.lane_doc('unit')['tests'])

    def test_every_failure_known_is_a_passing_known_lane_with_no_rerun(self):
        self.baseline('calc.DivTests.test_divides', 'calc.DivTests.test_zero')
        self.lane('unit', '#forge: reports=reports baseline=known.txt\n' + self.writes('r.xml', fixture='junit.xml'))
        done = self.run_verify()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        lane = self.lane_doc('unit')
        self.assertEqual((lane['state'], lane['exit'], len(lane['runs'])), ('known', 1, 1))
        self.assertTrue(all(f['known'] for f in lane['tests']['failing']))
        self.assertEqual(self.doc()['state'], 'pass')
        self.assertIn('known 2', done.stdout)

    def test_a_new_failure_fails_and_is_rerun(self):
        self.baseline('calc.DivTests.test_divides')
        self.lane('unit', '#forge: reports=reports baseline=known.txt\n' + self.writes('r.xml', fixture='junit.xml'))
        done = self.run_verify()
        self.assertEqual(done.returncode, 1)
        lane = self.lane_doc('unit')
        self.assertEqual((lane['state'], len(lane['runs'])), ('fail', 2))
        self.assertEqual({f['name']: f['known'] for f in lane['tests']['failing']},
                         {'calc.DivTests.test_divides': True, 'calc.DivTests.test_zero': False})

    def test_an_unknown_failure_past_the_display_cap_still_fails(self):
        self.baseline(*[f'big.f{i}' for i in range(200)])         # f200, the 201st, is new
        self.lane('unit', '#forge: reports=reports baseline=known.txt retries=0\n' + self.writes('r.xml', text=junit(201)))
        self.assertEqual(self.run_verify().returncode, 1)
        lane = self.lane_doc('unit')
        self.assertEqual(lane['state'], 'fail')
        self.assertTrue(lane['tests']['truncated'])

    def test_a_report_that_cannot_be_read_means_the_exit_code_decides(self):
        self.baseline('calc.DivTests.test_divides', 'calc.DivTests.test_zero')
        broken = self.base / 'broken.xml'; shutil.copy(FIXTURES / 'malformed.xml', broken)
        self.lane('unit', '#forge: reports=reports baseline=known.txt retries=0\n'
                  + self.writes('r.xml', fixture='junit.xml', before=f'mkdir -p reports; cp {broken} reports/b.xml; '))
        self.assertEqual(self.run_verify().returncode, 1)
        lane = self.lane_doc('unit')
        self.assertEqual(lane['state'], 'fail')
        self.assertIn('judged by its exit code', ' '.join(lane['tests']['notes']))

    def test_a_missing_baseline_is_a_configuration_error(self):
        self.lane('unit', '#forge: reports=reports baseline=nowhere.txt\nexit 0')
        done = self.run_verify()
        self.assertEqual(done.returncode, 2)
        self.assertIn('baseline=nowhere.txt is not a regular file', done.stderr)
        self.assertFalse((self.out / 'verification.lanes.json').exists())

    def test_lanes_checks_report_and_baseline_paths(self):
        self.lane('unit', '#forge: reports=reports baseline=nowhere.txt\nexit 0')
        listing = subprocess.run(self.argv('lanes'), capture_output=True, text=True)
        self.assertEqual(listing.returncode, 0, listing.stderr)          # the form alone is fine
        checked = subprocess.run(self.argv('lanes', '--checkout', str(self.checkout)), capture_output=True, text=True)
        self.assertEqual(checked.returncode, 2)
        for options in ('baseline=../x', 'reports=/abs', 'reports=a/../../b'):
            with self.subTest(options):
                self.lane('unit', f'#forge: {options}\nexit 0')
                done = subprocess.run(self.argv('lanes'), capture_output=True, text=True)
                self.assertEqual(done.returncode, 2, options)
                self.assertIn('must be a path inside the checkout', done.stderr)

    def test_counts_appear_while_the_lane_still_runs(self):
        self.lane('unit', '#forge: reports=reports\n' + self.writes('r.xml', fixture='junit.xml', exit_code=0)
                  .replace('exit 0', 'sleep 30'))
        proc = subprocess.Popen(self.run_args(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(proc.kill)
        deadline = time.monotonic() + 25
        seen = None
        while time.monotonic() < deadline:
            path = self.out / 'verification.lanes.json'
            if path.exists():
                lane = json.loads(path.read_text())['lanes'][0]
                if lane['tests']:
                    seen = lane
                    break
            time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=15)
        self.assertIsNotNone(seen, 'no counts while the lane ran')
        self.assertEqual((seen['state'], seen['tests']['fail']), ('running', 1))


if __name__ == '__main__':
    unittest.main()
