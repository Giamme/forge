"""A human's one-shot escalation pin: prepare_retry applies it at the next FULL implementer
retry, claims it by rename, records who decided each escalation, and `escalation --check`
reports it as the next model.

Most tests source forge-lib-task.sh in a temp plan directory and call prepare_retry
directly, with stubs for the helpers forge-parallel.sh provides. The end-to-end ones drive
`forge-parallel.sh` with the fake agent CLI of test_forge, like test_retry."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import test_forge as fixtures
import test_retry as retry_fixtures

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / 'scripts' / 'forge-lib-task.sh'
ROUTE = ROOT / 'scripts' / 'forge-route.py'
PATH = '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin'

# What forge-parallel.sh and forge-artifact.sh provide to the library, reduced to what
# prepare_retry and the report need. `field` only knows the dwarf column.
PRELUDE = r'''
note() { printf 'forge: %s\n' "$*" >&2; }
field() { awk -F'\t' -v id="$2" '!/^#/ && NF && $1==id {print $5; exit}' "$1"; }
all_ids() { awk -F'\t' '!/^#/ && NF {print $1}' "$1"; }
task_status() {
  [ -f "$1/tasks/$2/merged" ] && { echo MERGED; return; }
  [ -f "$1/tasks/$2/status" ] && { cat "$1/tasks/$2/status"; return; }
  echo PENDING
}
forge_verdict() { sed -n 's/^FORGE_VERDICT: *//p' "$1" | tail -n 1; }
forge_dataset_event() { printf '%s\n' "$*" >> "$EVENTS"; }
source "$LIB"
'''


def tsv(path):
    return [line.split('\t') for line in Path(path).read_text().splitlines()]


class PinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name))
        self.plan = self.root / 'plan'
        self.tdir = self.plan / 'tasks' / 'a'
        self.tdir.mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')
        self.events = self.root / 'events'
        self.policy_calls = self.root / 'policy-called'
        self.skill = self.stub_skill()
        self.fail_attempts(1)

    def stub_skill(self):
        """A SKILL_DIR whose policy script only records that it was asked."""
        skill = self.root / 'skill'; (skill / 'scripts').mkdir(parents=True)
        (skill / 'scripts' / 'forge-route.py').write_text(
            'import sys\nopen(%r, "a").write("called\\n")\n' % str(self.policy_calls))
        return skill

    def fail_attempts(self, n, spec='sol'):
        """n failed attempts on `spec`, the last reviewed FAIL."""
        (self.tdir / 'status').write_text('FAIL\n')
        (self.tdir / 'attempt').write_text('%d\n' % n)
        (self.tdir / 'qa.last').write_text('- [P1][CONFIRMED] a.txt:1 — broken\n\nFORGE_VERDICT: FAIL\n')
        (self.tdir / 'attempts.tsv').write_text(
            ''.join('1700000%03d\t%d\tFAIL\t\t%s\n' % (i, i, spec) for i in range(1, n + 1)))

    def pin(self, spec='opus:high\n'):
        (self.tdir / 'escalation.pin').write_text(spec)

    def sh(self, body, skill=None, path=PATH):
        env = {'PATH': path, 'SKILL_DIR': str(skill or self.skill), 'LIB': str(LIB), 'EVENTS': str(self.events)}
        return subprocess.run(['/bin/bash', '-c', PRELUDE + body], capture_output=True, text=True,
                              encoding='utf-8', env=env)

    def retry(self, *dwarf, skill=None, path=PATH):
        return self.sh("prepare_retry '%s' a %s" % (self.plan, ' '.join("'%s'" % d for d in dwarf)),
                       skill=skill, path=path)

    def dwarf(self):
        return tsv(self.plan / 'tasks.tsv')[0][4]

    def records(self):
        path = self.tdir / 'escalations.tsv'
        return tsv(path) if path.exists() else []

    def event_lines(self):
        return self.events.read_text().splitlines() if self.events.exists() else []

    def pin_files(self):
        return sorted(p.name for p in self.tdir.glob('escalation.pin*'))

    def policy_was_asked(self):
        return self.policy_calls.exists()

    # --- applying -------------------------------------------------------------------
    def test_a_pin_applies_at_the_next_full_retry_without_the_policy(self):
        self.pin()
        result = self.retry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dwarf(), 'opus:high')
        [(stamp, attempt, old, new, source)] = self.records()
        self.assertTrue(stamp.isdigit())
        self.assertEqual((attempt, old, new, source), ('1', 'sol', 'opus:high', 'pin'))
        self.assertEqual(self.pin_files(), [])                   # neither the pin nor the claim is left
        self.assertFalse(self.policy_was_asked())
        self.assertEqual(self.event_lines(), [
            "escalation --dir %s --task a --set from=sol --set to=opus:high --set after_attempt=1 --set source=pin"
            % self.plan])

    def test_a_pin_applies_whatever_escalate_after_says(self):
        (self.plan / 'escalate_after').write_text('0\n')
        self.pin('luna:high')
        result = self.retry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dwarf(), 'luna:high')
        self.assertEqual(self.records()[0][3:], ['luna:high', 'pin'])

    def test_a_pin_is_trimmed_to_its_first_line(self):
        self.pin('  opus:high \r\nnot part of the spec\n')
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual(self.dwarf(), 'opus:high')

    def test_a_pin_clears_the_policys_stop_and_next_markers(self):
        for name in ('escalation.stop', 'split.suggested', 'escalation.next'):
            (self.tdir / name).write_text('x\n')
        self.pin()
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual([p.name for p in self.tdir.glob('escalation.*')] + [p.name for p in self.tdir.glob('split.*')],
                         [])

    def test_a_pin_for_the_current_spec_is_recorded_and_consumed_without_a_rewrite(self):
        self.pin('sol\n')
        before = (self.plan / 'tasks.tsv').stat()
        result = self.retry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dwarf(), 'sol')
        self.assertEqual(self.records()[0][1:], ['1', 'sol', 'sol', 'pin'])
        self.assertEqual(self.pin_files(), [])
        after = (self.plan / 'tasks.tsv').stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertIn('--set source=pin', self.event_lines()[0])
        self.assertFalse(self.policy_was_asked())

    def test_an_unusable_pin_is_dropped_and_the_policy_decides(self):
        for text in ('', '\n', 'two words\n', '-\n'):
            with self.subTest(pin=text):
                self.pin(text)
                result = self.retry()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('no usable spec', result.stderr)
                self.assertEqual(self.pin_files(), [])
                self.assertEqual(self.dwarf(), 'sol')
                self.assertEqual(self.records(), [])
        self.assertTrue(self.policy_was_asked())

    # --- who decided ------------------------------------------------------------------
    def test_an_explicit_dwarf_records_retry_and_leaves_a_pin_alone(self):
        self.pin()
        result = self.retry('luna:high')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dwarf(), 'luna:high')
        self.assertEqual(self.records()[0][2:], ['sol', 'luna:high', 'retry'])
        self.assertIn('--set source=retry', self.event_lines()[0])
        self.assertEqual(self.pin_files(), ['escalation.pin'])    # a --dwarf is not the pin's retry
        self.assertFalse(self.policy_was_asked())

    def test_the_policy_records_policy(self):
        (self.plan / 'fractal-routing.json').write_text(json.dumps({'low': ['sol'], 'high': ['astra']}))
        self.fail_attempts(3)
        result = self.retry(skill=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dwarf(), 'astra')
        self.assertEqual(self.records()[0][1:], ['3', 'sol', 'astra', 'policy'])
        self.assertIn('--set source=policy', self.event_lines()[0])

    def test_the_same_spec_is_no_escalation_for_retry_and_policy(self):
        self.assertEqual(self.retry('sol').returncode, 0)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.event_lines(), [])

    # --- QA-only resumes leave the pin alone --------------------------------------------
    def assert_pin_waits(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pin_files(), ['escalation.pin'])
        self.assertEqual((self.tdir / 'escalation.pin').read_text(), 'opus:high\n')
        self.assertEqual(self.dwarf(), 'sol')
        self.assertEqual(self.records(), [])
        self.assertEqual(self.event_lines(), [])
        self.assertEqual((self.tdir / 'resume').read_text().strip(), 'qa')

    def test_an_unknown_verdict_with_a_reviewed_commit_resumes_qa_and_keeps_the_pin(self):
        (self.tdir / 'status').write_text('UNKNOWN\n')
        (self.tdir / 'qa.last').write_text('no verdict line here\n')
        (self.tdir / 'reviewed.commit').write_text('abc123\n')
        self.pin()
        result = self.retry()
        self.assert_pin_waits(result)
        self.assertIn('pin waits', result.stderr)

    def test_a_qa_stage_infra_stop_resumes_qa_and_keeps_the_pin(self):
        self.pin()
        stop = self.sh("infra_stop '%s' a qa 1; prepare_retry '%s' a" % (self.tdir, self.plan))
        self.assert_pin_waits(stop)
        self.assertEqual((self.tdir / 'status').read_text().strip(), 'INFRA')

    def test_a_dwarf_stage_infra_stop_is_a_full_retry_and_takes_the_pin(self):
        self.pin()
        result = self.sh("infra_stop '%s' a dwarf 2; prepare_retry '%s' a" % (self.tdir, self.plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dwarf(), 'opus:high')
        self.assertEqual(self.records()[0][2:], ['sol', 'opus:high', 'pin'])

    def test_an_unknown_verdict_without_a_reviewed_commit_is_a_full_retry(self):
        (self.tdir / 'status').write_text('UNKNOWN\n')
        self.pin()
        self.assertEqual(self.retry().returncode, 0)
        self.assertFalse((self.tdir / 'resume').exists())
        self.assertEqual(self.dwarf(), 'opus:high')

    def test_the_pin_applies_at_the_full_retry_after_a_qa_only_one(self):
        (self.tdir / 'status').write_text('UNKNOWN\n')
        (self.tdir / 'qa.last').write_text('no verdict line here\n')
        (self.tdir / 'reviewed.commit').write_text('abc123\n')
        self.pin()
        self.assertEqual(self.retry().returncode, 0)
        (self.tdir / 'resume').unlink()                          # the QA-only attempt ran and failed the review
        self.fail_attempts(1)
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual(self.dwarf(), 'opus:high')
        self.assertEqual(self.pin_files(), [])

    # --- failure keeps the pin ------------------------------------------------------------
    def block_rewrite(self):
        """tasks.tsv cannot be rewritten: the temp file's name is taken by a directory (a
        read-only plan dir does the same, but not for a root-run test)."""
        (self.plan / '.tasks.tsv.retry').mkdir()

    def test_a_failed_rewrite_gives_the_pin_back_and_records_nothing(self):
        self.pin()
        self.block_rewrite()
        result = self.retry()
        self.assertEqual(result.returncode, 1)
        self.assertIn('nothing was retried', result.stderr)
        self.assertEqual(self.pin_files(), ['escalation.pin'])
        self.assertEqual((self.tdir / 'escalation.pin').read_text(), 'opus:high\n')
        self.assertEqual(self.dwarf(), 'sol')
        self.assertEqual(self.records(), [])
        self.assertEqual(self.event_lines(), [])

    def test_a_pin_written_during_a_failing_application_survives_it(self):
        self.pin()
        # An awk that, instead of rewriting tasks.tsv, behaves like a human running
        # `forge control pin` between the claim and the failure.
        bin_dir = self.root / 'bin'; bin_dir.mkdir()
        awk = bin_dir / 'awk'
        awk.write_text('#!/bin/sh\ncase "$*" in *" dw="*) printf "sol:xhigh\\n" > "%s"; exit 1 ;; esac\n'
                       'exec /usr/bin/awk "$@"\n' % (self.tdir / 'escalation.pin'))
        awk.chmod(0o755)
        result = self.retry(path=str(bin_dir) + ':' + PATH)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.pin_files(), ['escalation.pin'])    # the claim is gone, the newer pin stays
        self.assertEqual((self.tdir / 'escalation.pin').read_text(), 'sol:xhigh\n')
        self.assertEqual(self.records(), [])

    def test_a_pin_is_not_lost_when_its_record_cannot_be_written(self):
        self.pin()
        (self.tdir / 'escalations.tsv').mkdir()                   # appending to a directory fails
        result = self.retry()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.pin_files(), ['escalation.pin'])
        self.assertIn('could not record', result.stderr)

    # --- Fractal ---------------------------------------------------------------------------
    def test_a_fractal_plan_never_applies_a_pin(self):
        (self.plan / 'fractal-selection.json').write_text('{"enabled": true}')
        self.pin()
        result = self.retry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Fractal plans never apply pins', result.stderr)
        self.assertEqual(self.pin_files(), ['escalation.pin'])
        self.assertEqual(self.dwarf(), 'sol')
        self.assertEqual(self.records(), [])

    def test_a_disabled_fractal_selection_is_not_a_fractal_plan(self):
        (self.plan / 'fractal-selection.json').write_text('{"enabled": false}')
        self.pin()
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual(self.dwarf(), 'opus:high')

    # --- the run summary ---------------------------------------------------------------------
    def report(self):
        result = self.sh("report_escalations '%s'" % self.plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_summary_says_the_next_retry_is_pinned(self):
        self.pin()
        self.assertIn('a              its next retry goes to opus:high (pinned)\n', self.report())

    def test_the_summary_says_when_the_pin_waits_for_a_qa_only_re_run(self):
        self.pin()
        (self.tdir / 'resume').write_text('qa\n')
        self.assertIn('its next retry goes to opus:high (pinned; after the QA-only re-run)', self.report())

    def test_a_pin_outranks_a_split_suggestion_and_a_merged_task_shows_none(self):
        (self.tdir / 'split.suggested').write_text('split it\n')
        self.pin()
        report = self.report()
        self.assertIn('(pinned)', report)
        self.assertNotIn('split suggested', report)
        (self.tdir / 'split.suggested').unlink()
        (self.tdir / 'merged').write_text('x\n')
        self.assertEqual(self.report(), '')

    def test_the_summary_ignores_a_pin_in_a_fractal_plan(self):
        (self.plan / 'fractal-selection.json').write_text('{"enabled": true}')
        self.pin()
        self.assertEqual(self.report(), '')


class CheckTests(unittest.TestCase):
    """`escalation --check`, the policy's marker writes and the control lock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.plan = Path(os.path.realpath(self.tmp.name)) / 'plan'
        self.tdir = self.plan / 'tasks' / 'a'
        self.tdir.mkdir(parents=True)
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\ta.txt\tsol\topus\tA\n')

    def fail_three_times(self, pools):
        (self.plan / 'fractal-routing.json').write_text(json.dumps(pools))
        (self.tdir / 'attempts.tsv').write_text(''.join('17000000%02d\t%d\tFAIL\t\tsol\n' % (i, i) for i in (1, 2, 3)))

    def route(self, *extra, task='a'):
        return subprocess.run([sys.executable, str(ROUTE), 'escalation', '--plan', str(self.plan), '--task', task,
                               *extra], capture_output=True, text=True, timeout=30)

    def next_marker(self):
        path = self.tdir / 'escalation.next'
        return path.read_text() if path.exists() else None

    def test_check_reports_a_pending_pin_as_the_next_model(self):
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'opus:high\n'), result.stderr)
        self.assertEqual(self.next_marker(), 'opus:high\n')
        self.assertTrue((self.tdir / 'escalation.pin').exists())     # reporting is not claiming

    def test_check_reports_the_pin_over_a_due_policy_escalation_and_a_stop(self):
        self.fail_three_times({'low': ['sol'], 'high': ['astra']})
        (self.tdir / 'escalation.stop').write_text('3\tsol\n')
        (self.tdir / 'split.suggested').write_text('split it\n')
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'opus:high\n'), result.stderr)
        self.assertEqual(self.next_marker(), 'opus:high\n')
        self.assertFalse((self.tdir / 'escalation.stop').exists() or (self.tdir / 'split.suggested').exists())

    def test_check_reports_the_pin_even_with_the_policy_off(self):
        (self.plan / 'escalate_after').write_text('0\n')
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        self.assertEqual(self.route('--check').stdout, 'opus:high\n')
        self.assertEqual(self.next_marker(), 'opus:high\n')

    def test_check_does_not_report_a_pin_that_cannot_apply_next(self):
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        (self.tdir / 'resume').write_text('qa\n')                    # the next retry only re-runs QA
        self.assertEqual(self.route('--check').stdout, '')
        self.assertIsNone(self.next_marker())
        (self.tdir / 'resume').unlink()
        (self.plan / 'fractal-selection.json').write_text('{"enabled": true}')
        self.assertEqual(self.route('--check').stdout, '')
        self.assertIsNone(self.next_marker())

    def test_without_check_a_pin_is_not_the_policys_business(self):
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        self.assertEqual(self.route().stdout, '')
        self.assertIsNone(self.next_marker())

    def test_a_pin_that_is_gone_stops_being_reported(self):
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        self.route('--check')
        (self.tdir / 'escalation.pin').unlink()
        self.route('--check')                                        # the streak does not hold: markers go
        self.assertIsNone(self.next_marker())

    def run_locked(self, after_release):
        """Hold the control lock while a stop-writing `--check` waits on it for a second,
        set escalate_after to `after_release`, then release."""
        self.fail_three_times({'low': ['sol']})                      # no stronger model: the policy stops the task
        (self.plan / 'escalate_after').write_text('3\n')
        fd = os.open(self.plan / 'control.lock', os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        proc = subprocess.Popen([sys.executable, str(ROUTE), 'escalation', '--plan', str(self.plan), '--task', 'a',
                                 '--check'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1.0)
            self.assertIsNone(proc.poll(), 'the policy wrote past a held control lock')
            (self.plan / 'escalate_after').write_text(after_release)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)
        out, err = proc.communicate(timeout=30)
        self.assertGreaterEqual(time.monotonic() - started, 1.0)
        return proc.returncode, out, err

    def test_the_policy_re_reads_escalate_after_inside_the_control_lock(self):
        rc, out, err = self.run_locked('0\n')
        self.assertEqual((rc, out), (0, ''), err)
        self.assertEqual(sorted(p.name for p in self.tdir.iterdir() if p.name != 'attempts.tsv'), [])

    def test_the_policy_still_writes_its_stop_when_the_policy_stays_on(self):
        rc, out, err = self.run_locked('3\n')
        self.assertEqual(rc, 3, err)
        self.assertEqual((self.tdir / 'escalation.stop').read_text(), '3\tsol\n')
        self.assertIn('already in the highest tier', (self.tdir / 'split.suggested').read_text())

    # --- path safety ------------------------------------------------------------------------
    def test_markers_are_not_written_through_a_symlinked_task_dir(self):
        self.fail_three_times({'low': ['sol']})
        elsewhere = self.plan.parent / 'elsewhere'; elsewhere.mkdir()
        (elsewhere / 'attempts.tsv').write_text((self.tdir / 'attempts.tsv').read_text())
        (self.plan / 'tasks' / 'b').symlink_to(elsewhere)
        (self.plan / 'tasks.tsv').write_text('b\t-\tlow\tb.txt\tsol\topus\tB\n')
        result = self.route(task='b')
        self.assertEqual((result.returncode, list(elsewhere.iterdir())), (0, [elsewhere / 'attempts.tsv']))

    def test_a_task_id_the_scheduler_would_refuse_writes_nothing(self):
        self.fail_three_times({'low': ['sol']})
        for task in ('..', '.', 'a/../a', ''):
            with self.subTest(task=task):
                self.assertEqual(self.route(task=task).returncode, 0)
        self.assertEqual(sorted(p.name for p in self.tdir.iterdir()), ['attempts.tsv'])

    def test_a_symlinked_marker_is_not_followed(self):
        self.fail_three_times({'low': ['sol']})
        victim = self.plan.parent / 'victim'; victim.write_text('keep\n')
        (self.tdir / 'escalation.stop').symlink_to(victim)
        self.route('--check')
        self.assertEqual(victim.read_text(), 'keep\n')


class PinEndToEndTests(unittest.TestCase):
    """The pin through the real `retry` and the scheduler's automatic retries."""
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls
    pooled_plan = retry_fixtures.RetryTests.pooled_plan

    FAIL_ONCE = {'a.qa.1': {'verdict': 'FAIL', 'findings': '- [P1][CONFIRMED] a.txt:1 — broken'},
                 'a.dwarf.2': {'content': 'fixed'}}

    def test_retry_applies_a_pin_the_run_summary_announced(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        self.scenario(self.FAIL_ONCE)
        (p / 'tasks/a/escalation.pin').write_text('astra\n')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertIn('its next retry goes to astra (pinned)', r.stdout)
        self.assertEqual((p / 'tasks/a/escalation.next').read_text(), 'astra\n')   # `--check` after the attempt
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('model=gpt-6-astra', (p / 'tasks/a/dwarf.resolved').read_text())
        self.assertEqual(tsv(p / 'tasks/a/escalations.tsv')[0][1:], ['1', 'sol', 'astra', 'pin'])
        self.assertEqual(sorted(x.name for x in (p / 'tasks/a').glob('escalation.*')), [])

    def test_the_scheduler_applies_a_pin_at_its_automatic_retry(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        self.scenario(self.FAIL_ONCE)
        (p / 'tasks/a/escalation.pin').write_text('astra\n')
        r = self.run_script('forge-parallel.sh', 'run', p, '--retry-failed', 1)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls().count('dwarf'), 2)
        self.assertIn('model=gpt-6-astra', (p / 'tasks/a/dwarf.resolved').read_text())
        self.assertEqual(tsv(p / 'tasks/a/escalations.tsv')[0][2:], ['sol', 'astra', 'pin'])
        self.assertEqual(sorted(x.name for x in (p / 'tasks/a').glob('escalation.*')), [])


if __name__ == '__main__':
    unittest.main()
