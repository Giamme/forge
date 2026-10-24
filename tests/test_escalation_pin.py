"""A human's one-shot escalation pin: prepare_retry applies it at the next FULL implementer
retry (prepare_resume at an infrastructure stop's resume), claims it by rename, records who decided each escalation, and `escalation --check`
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
ARTIFACT = ROOT / 'scripts' / 'forge-artifact.sh'
ROUTE = ROOT / 'scripts' / 'forge-route.py'
PATH = '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin'

# What forge-parallel.sh and forge-artifact.sh provide to the library, reduced to what
# prepare_retry and the report need. `field` only knows the dwarf column. forge-artifact.sh
# gives the real forge_fingerprint, which a QA-only resume is validated with.
PRELUDE = r'''
source "$ARTIFACT"
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
        """A SKILL_DIR whose policy script only records that it was asked.

        `record` writes escalations.tsv; it is not a policy decision, so it runs the real
        forge-route.py. Counting it would make every applied pin look like the policy ran.
        """
        skill = self.root / 'skill'; (skill / 'scripts').mkdir(parents=True)
        (skill / 'scripts' / 'forge-route.py').write_text(
            'import subprocess, sys\n'
            'if len(sys.argv) > 1 and sys.argv[1] == "record":\n'
            '    raise SystemExit(subprocess.call([sys.executable, %r, *sys.argv[1:]]))\n'
            'open(%r, "a").write("called\\n")\n' % (str(ROUTE), str(self.policy_calls)))
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
        env = {'PATH': path, 'SKILL_DIR': str(skill or self.skill), 'LIB': str(LIB), 'ARTIFACT': str(ARTIFACT),
               'EVENTS': str(self.events)}
        return subprocess.run(['/bin/bash', '-c', PRELUDE + body], capture_output=True, text=True,
                              encoding='utf-8', env=env)

    def retry(self, *dwarf, skill=None, path=PATH):
        return self.sh("prepare_retry '%s' a %s" % (self.plan, ' '.join("'%s'" % d for d in dwarf)),
                       skill=skill, path=path)

    def resume(self):
        """What the scheduler runs before it dispatches an INFRA task again."""
        return self.sh("retry_for_scheduler '%s' a --resume" % self.plan)

    def reviewed_worktree(self):
        """The task's worktree, committed and recorded as what the reviewer saw, so a `resume`
        marker for it holds; returns the worktree."""
        wt = self.root / 'wt' / 'a'; wt.mkdir(parents=True)
        git = ['git', '-C', str(wt), '-c', 'user.name=t', '-c', 'user.email=t@t']
        subprocess.run(git + ['init', '-q'], check=True)
        (wt / 'a.txt').write_text('implemented\n')
        subprocess.run(git + ['add', '.'], check=True)
        subprocess.run(git + ['commit', '-qm', 'attempt 1'], check=True)
        (self.plan / 'wt_root').write_text(str(self.root / 'wt') + '\n')
        head = subprocess.run(git + ['rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout
        (self.tdir / 'reviewed.commit').write_text(head)
        (self.tdir / 'changes.diff').write_text('+implemented\n')
        fp = self.sh("forge_fingerprint '%s'" % wt)
        self.assertEqual(fp.returncode, 0, fp.stderr)
        (self.tdir / 'source.fingerprint').write_text(fp.stdout)
        return wt

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
        self.reviewed_worktree()
        self.pin()
        result = self.retry()
        self.assert_pin_waits(result)
        self.assertIn('pin waits', result.stderr)

    def test_a_qa_stage_infra_stop_resumes_qa_and_keeps_the_pin(self):
        self.reviewed_worktree()
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
        self.reviewed_worktree()
        self.pin()
        self.assertEqual(self.retry().returncode, 0)
        (self.tdir / 'resume').unlink()                          # the QA-only attempt ran and failed the review
        self.fail_attempts(1)
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual(self.dwarf(), 'opus:high')
        self.assertEqual(self.pin_files(), [])

    # --- a QA-only marker the worktree no longer matches is a full retry ----------------------
    def assert_pin_applied(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dwarf(), 'opus:high')
        self.assertEqual(self.records()[0][2:], ['sol', 'opus:high', 'pin'])
        self.assertEqual(self.pin_files(), [])
        self.assertFalse((self.tdir / 'resume').exists())       # do_task_body runs the full task too

    def test_an_unknown_verdict_whose_worktree_changed_is_a_full_retry_and_takes_the_pin(self):
        (self.tdir / 'status').write_text('UNKNOWN\n')
        (self.tdir / 'qa.last').write_text('no verdict line here\n')
        (self.reviewed_worktree() / 'a.txt').write_text('edited after the review\n')
        self.pin()
        result = self.retry()
        self.assert_pin_applied(result)
        self.assertIn('no longer matches the worktree', result.stderr)
        self.assertNotIn('re-running QA', result.stderr)

    def test_a_qa_stage_infra_stop_on_a_changed_worktree_takes_the_pin(self):
        (self.reviewed_worktree() / 'a.txt').write_text('edited after the review\n')
        self.pin()
        self.assert_pin_applied(self.sh("infra_stop '%s' a qa 1; prepare_retry '%s' a" % (self.tdir, self.plan)))

    # --- an infrastructure stop's resume (the scheduler, no retry) ---------------------------
    def infra_stop(self, role):
        result = self.sh("infra_stop '%s' a %s 1" % (self.tdir, role))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_dwarf_stage_resume_applies_the_pin_and_nothing_else(self):
        (self.tdir / 'retry_findings.md').write_text('carried forward\n')
        self.infra_stop('dwarf')
        self.pin()
        result = self.resume()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.dwarf(), 'opus:high')
        [(_stamp, attempt, old, new, source)] = self.records()
        self.assertEqual((attempt, old, new, source), ('0', 'sol', 'opus:high', 'pin'))   # the stop refunded attempt 1
        self.assertEqual(self.event_lines(), [
            "escalation --dir %s --task a --set from=sol --set to=opus:high --set after_attempt=0 --set source=pin"
            % self.plan])
        self.assertEqual(self.pin_files(), [])
        self.assertFalse(self.policy_was_asked())
        self.assertEqual((self.tdir / 'retry_findings.md').read_text(), 'carried forward\n')
        self.assertEqual((self.tdir / 'status').read_text().strip(), 'INFRA')   # not turned into a retry

    def test_a_qa_stage_resume_keeps_the_pin(self):
        self.reviewed_worktree()
        self.infra_stop('qa')
        self.pin()
        result = self.resume()
        self.assert_pin_waits(result)
        self.assertIn('pin waits', result.stderr)
        self.assertFalse(self.policy_was_asked())

    def test_a_qa_stage_resume_on_a_changed_worktree_takes_the_pin(self):
        (self.reviewed_worktree() / 'a.txt').write_text('edited after the review\n')
        self.infra_stop('qa')
        self.pin()
        self.assert_pin_applied(self.resume())

    def test_a_resume_without_a_pin_changes_nothing(self):
        self.infra_stop('dwarf')
        result = self.resume()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.dwarf(), self.records(), self.event_lines()), ('sol', [], []))
        self.assertFalse(self.policy_was_asked())

    def test_a_resume_whose_pin_cannot_be_applied_refuses_and_keeps_the_pin(self):
        self.infra_stop('dwarf')
        self.pin()
        self.block_rewrite()
        result = self.resume()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.pin_files(), ['escalation.pin'])
        self.assertEqual((self.dwarf(), self.records()), ('sol', []))

    def test_a_resume_in_a_fractal_plan_leaves_the_pin(self):
        (self.plan / 'fractal-selection.json').write_text('{"enabled": true}')
        self.infra_stop('dwarf')
        self.pin()
        self.assertEqual(self.resume().returncode, 0)
        self.assertEqual((self.dwarf(), self.pin_files()), ('sol', ['escalation.pin']))

    # --- the claim's destination ------------------------------------------------------------
    def test_a_claim_symlinked_out_of_the_plan_neither_moves_the_pin_out_nor_drops_it(self):
        # mv would move the pin INTO the directory the link names, out of the plan.
        outside = self.root / 'outside'; outside.mkdir()
        os.symlink(outside, self.tdir / 'escalation.pin.applying')
        self.pin()
        result = self.retry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.dwarf(), 'opus:high')               # the stray link was replaced, the pin applied
        self.assertEqual(self.records()[0][2:], ['sol', 'opus:high', 'pin'])
        self.assertEqual(self.pin_files(), [])

    def test_a_directory_in_the_claims_place_leaves_the_pin_for_a_later_retry(self):
        (self.tdir / 'escalation.pin.applying').mkdir()
        self.pin()
        result = self.retry()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('could not claim the escalation pin', result.stderr)
        self.assertEqual(list((self.tdir / 'escalation.pin.applying').iterdir()), [])
        self.assertEqual((self.tdir / 'escalation.pin').read_text(), 'opus:high\n')
        self.assertEqual(self.dwarf(), 'sol')
        self.assertTrue(self.policy_was_asked())

    def test_a_stale_claim_file_is_replaced_by_the_new_claim(self):
        (self.tdir / 'escalation.pin.applying').write_text('luna:high\n')   # left by a run that died mid-claim
        self.pin()
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
        self.assertEqual(self.dwarf(), 'sol')                     # found out before tasks.tsv was touched
        self.assertEqual(self.event_lines(), [])

    def break_record_after_rewrite(self, rollback_fails=False):
        """An awk that, once it has rewritten tasks.tsv to the pinned spec, makes
        escalations.tsv unwritable (the record fails after the rewrite, past the preflight)."""
        bin_dir = self.root / 'bin'; bin_dir.mkdir()
        awk = bin_dir / 'awk'
        esc = self.tdir / 'escalations.tsv'
        awk.write_text('#!/bin/sh\ncase "$*" in\n'
                       '  *" dw=opus:high"*) rm -f "%s"; mkdir "%s" ;;\n' % (esc, esc)
                       + ('  *" dw=sol"*) exit 1 ;;\n' if rollback_fails else '')
                       + 'esac\nexec /usr/bin/awk "$@"\n')
        awk.chmod(0o755)
        return str(bin_dir) + ':' + PATH

    def test_a_record_that_fails_after_the_rewrite_takes_the_rewrite_back(self):
        self.pin()
        result = self.retry(path=self.break_record_after_rewrite())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('nothing was retried', result.stderr)
        self.assertEqual(self.dwarf(), 'sol')                     # not left on the pinned model, unrecorded
        self.assertEqual(self.pin_files(), ['escalation.pin'])
        self.assertEqual((self.tdir / 'escalation.pin').read_text(), 'opus:high\n')
        self.assertEqual(self.event_lines(), [])
        (self.tdir / 'escalations.tsv').rmdir()                   # the cause is fixed: the pin is a real escalation again
        self.assertEqual(self.retry().returncode, 0)
        self.assertEqual(self.records()[0][2:], ['sol', 'opus:high', 'pin'])

    # --- nothing is appended through a symlink, at the file or any parent -----------------
    def outside_history(self):
        """escalations.tsv as a symlink to a file outside the plan."""
        outside = self.root / 'outside.tsv'
        outside.write_text('keep\n')
        (self.tdir / 'escalations.tsv').symlink_to(outside)
        return outside

    def assert_refused(self, result, outside, tasks_before):
        self.assertEqual(outside.read_text(), 'keep\n')
        self.assertTrue((self.tdir / 'escalations.tsv').is_symlink())
        self.assertEqual((self.plan / 'tasks.tsv').read_text(), tasks_before)
        self.assertEqual((self.tdir / 'escalation.pin').read_text(), 'opus:high\n')
        self.assertEqual(self.pin_files(), ['escalation.pin'])          # claimed by nobody
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('escalations.tsv', result.stderr)

    def test_a_symlinked_escalations_file_is_not_written_and_the_pin_stays(self):
        outside = self.outside_history()
        before = (self.plan / 'tasks.tsv').read_text()
        self.pin()
        self.assert_refused(self.retry(), outside, before)

    def test_a_symlinked_escalations_file_blocks_an_infrastructure_resume(self):
        outside = self.outside_history()
        self.infra_stop('dwarf')
        self.pin()
        before = (self.plan / 'tasks.tsv').read_text()
        self.assert_refused(self.resume(), outside, before)

    def test_a_symlinked_escalations_file_is_not_written_by_a_policy_escalation(self):
        outside = self.outside_history()
        (self.plan / 'fractal-routing.json').write_text(json.dumps({'low': ['sol'], 'high': ['astra']}))
        self.fail_attempts(3)
        result = self.retry(skill=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outside.read_text(), 'keep\n')
        self.assertTrue((self.tdir / 'escalations.tsv').is_symlink())
        self.assertEqual(self.dwarf(), 'astra')                       # the rewrite stands, as it does today

    def test_a_symlinked_task_dir_gets_no_escalation_record_outside_the_plan(self):
        # O_NOFOLLOW on the full path would still follow a swapped tasks/<id>.
        outside = self.root / 'outside-task'
        self.tdir.rename(outside)
        (self.plan / 'tasks' / 'a').symlink_to(outside)
        self.pin()
        before = (self.plan / 'tasks.tsv').read_text()
        result = self.retry()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('escalations.tsv', result.stderr)
        self.assertEqual((self.plan / 'tasks.tsv').read_text(), before)
        self.assertEqual((outside / 'escalation.pin').read_text(), 'opus:high\n')
        self.assertFalse((outside / 'escalation.pin.applying').exists())
        self.assertFalse((outside / 'escalations.tsv').exists())

    def test_a_symlinked_tasks_dir_gets_no_escalation_record_outside_the_plan(self):
        # The parent of the task dir, one component further up, is the same kind of redirect.
        outside = self.root / 'outside-tasks'
        (self.plan / 'tasks').rename(outside)
        (self.plan / 'tasks').symlink_to(outside)
        self.pin()
        before = (self.plan / 'tasks.tsv').read_text()
        result = self.retry()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('escalations.tsv', result.stderr)
        self.assertEqual((self.plan / 'tasks.tsv').read_text(), before)
        task = outside / 'a'
        self.assertEqual((task / 'escalation.pin').read_text(), 'opus:high\n')
        self.assertFalse((task / 'escalation.pin.applying').exists())
        self.assertFalse((task / 'escalations.tsv').exists())

    def record(self, *args, cwd):
        return subprocess.run([sys.executable, str(ROUTE), 'record', '--plan', str(self.plan),
                               '--task', 'a', *args], cwd=cwd, capture_output=True, text=True)

    def test_a_missing_task_dir_does_not_write_escalations_outside_the_plan(self):
        """`record --line` with tasks/<id> absent must not open escalations.tsv in the cwd.

        _open_parent returns no directory descriptor then. append_line used to create or
        append the leaf relative to the process cwd and only afterwards fail.
        """
        self.tdir.rename(self.root / 'removed-task')
        seeded = self.root / 'seeded'
        seeded.mkdir()
        outside = seeded / 'escalations.tsv'
        outside.write_text('keep\n')
        appended = self.record('--line', 'TEXT', cwd=seeded)
        self.assertEqual(appended.returncode, 1, appended.stderr)
        self.assertEqual(outside.read_text(), 'keep\n')
        empty = self.root / 'empty'
        empty.mkdir()
        created = self.record('--line', 'TEXT', cwd=empty)
        self.assertEqual(created.returncode, 1, created.stderr)
        self.assertEqual(list(empty.iterdir()), [])
        self.assertFalse((self.plan / 'escalations.tsv').exists())
        self.assertFalse((self.plan / 'tasks' / 'escalations.tsv').exists())
        self.assertEqual(self.record('--probe', cwd=seeded).returncode, 1)

    def test_a_record_after_the_task_dir_vanishes_does_not_write_outside(self):
        """The pin precheck can pass, and the directory can be gone when the line is written."""
        cwd = self.root / 'cwd'
        cwd.mkdir()
        outside = cwd / 'escalations.tsv'
        outside.write_text('keep\n')
        probe = self.record('--probe', cwd=cwd)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.tdir.rename(self.root / 'removed-task')
        result = self.record('--line', 'TEXT', cwd=cwd)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(outside.read_text(), 'keep\n')
        self.assertFalse((self.root / 'removed-task' / 'escalations.tsv').exists())
        self.assertEqual(list(self.plan.rglob('escalations.tsv')), [])

    def test_a_record_that_fails_with_no_way_back_consumes_the_pin_and_goes_on(self):
        self.pin()
        result = self.retry(path=self.break_record_after_rewrite(rollback_fails=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('could not be put back', result.stderr)
        self.assertEqual(self.dwarf(), 'opus:high')               # the human's choice is in effect
        self.assertEqual(self.pin_files(), [])                    # and is not replayed as a same-spec pin
        self.assertIn('--set from=sol --set to=opus:high', self.event_lines()[0])

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
    def report(self, skill=None):
        result = self.sh("report_escalations '%s'" % self.plan, skill=skill)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_the_summary_says_the_next_retry_is_pinned(self):
        self.pin()
        self.assertIn('a              its next retry goes to opus:high (pinned)\n', self.report())

    def test_the_summary_says_when_the_pin_waits_for_a_qa_only_re_run(self):
        self.pin()
        self.reviewed_worktree()
        (self.tdir / 'resume').write_text('qa\n')
        self.assertIn('its next retry goes to opus:high (pinned; after the QA-only re-run)', self.report())

    def test_a_resume_marker_the_worktree_no_longer_matches_is_not_a_qa_only_re_run(self):
        self.pin()
        (self.reviewed_worktree() / 'a.txt').write_text('edited after the review\n')
        (self.tdir / 'resume').write_text('qa\n')
        text = self.report()
        self.assertIn('its next retry goes to opus:high (pinned)\n', text)
        self.assertNotIn('QA-only', text)
        self.assertEqual((self.tdir / 'resume').read_text(), 'qa\n')   # the summary does not clear it

    def test_the_summary_sees_an_unreviewed_verdict_as_a_qa_only_re_run_before_the_marker_exists(self):
        (self.tdir / 'status').write_text('UNKNOWN\n')           # prepare_retry has not written `resume` yet
        self.reviewed_worktree()
        self.pin()
        self.assertIn('its next retry goes to opus:high (pinned; after the QA-only re-run)', self.report())
        (self.tdir / 'reviewed.commit').write_text('')            # nothing was reviewed: a full retry
        self.assertIn('its next retry goes to opus:high (pinned)\n', self.report())

    def test_an_unreviewed_verdict_the_worktree_no_longer_matches_is_not_a_qa_only_re_run(self):
        (self.tdir / 'status').write_text('UNKNOWN\n')
        (self.reviewed_worktree() / 'a.txt').write_text('edited after the review\n')
        self.pin()
        text = self.report()
        self.assertIn('its next retry goes to opus:high (pinned)\n', text)
        self.assertNotIn('QA-only', text)
        self.assertFalse((self.tdir / 'resume').exists())

    def test_a_qa_stage_stop_on_an_edited_worktree_reports_the_pin_for_a_full_retry(self):
        wt = self.reviewed_worktree()
        self.assertEqual(self.sh("infra_stop '%s' a qa 1" % self.tdir).returncode, 0)
        (wt / 'a.txt').write_text('edited after the review\n')
        self.pin()
        text = self.report()
        self.assertIn('a              its next retry goes to opus:high (pinned)\n', text)
        self.assertNotIn('QA-only', text)
        self.assertEqual((self.tdir / 'resume').read_text().strip(), 'qa')
        # What do_task passes to `escalation --check`: the summary's own answer.
        result = self.sh(
            "if qa_only_next '%s' a; then qa=yes; else qa=no; fi\n"
            "python3 '%s' escalation --plan '%s' --task a --check --qa-only-next \"$qa\""
            % (self.plan, ROUTE, self.plan))
        self.assertEqual((result.returncode, result.stdout), (0, 'opus:high\n'), result.stderr)
        self.assertEqual((self.tdir / 'escalation.next').read_text(), 'opus:high\n')

    def test_a_withdrawn_pin_leaves_the_summary_with_the_policys_verdict(self):
        (self.plan / 'fractal-routing.json').write_text(json.dumps({'low': ['sol']}))   # no stronger model
        self.fail_attempts(3)
        self.pin()
        route = [sys.executable, str(ROUTE), 'escalation', '--plan', str(self.plan), '--task', 'a', '--check']
        self.assertEqual(subprocess.run(route, capture_output=True, text=True).stdout, 'opus:high\n')
        (self.tdir / 'escalation.pin').unlink()                  # `forge control unpin`; no attempt, no new --check
        report = self.report(skill=ROOT)
        self.assertIn('split suggested:', report)
        self.assertNotIn('opus:high', report)

    def test_a_marker_the_withdrawn_pin_left_behind_is_decided_again(self):
        (self.tdir / 'escalation.next').write_text('opus:high\n')   # what `--check` wrote while the pin was pending
        self.assertEqual(self.report(skill=ROOT), '')             # one failure: the policy has nothing to say
        self.assertFalse((self.tdir / 'escalation.next').exists())

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

    def test_check_reports_the_pin_over_a_due_policy_escalation(self):
        self.fail_three_times({'low': ['sol'], 'high': ['astra']})
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'opus:high\n'), result.stderr)
        self.assertEqual(self.next_marker(), 'opus:high\n')       # not astra, the policy's own pick

    def test_check_keeps_the_policys_stop_under_a_pin_and_it_holds_when_the_pin_goes(self):
        self.fail_three_times({'low': ['sol']})                      # no stronger model: the policy stops the task
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (0, 'opus:high\n'), result.stderr)
        self.assertEqual(self.next_marker(), 'opus:high\n')
        self.assertEqual((self.tdir / 'escalation.stop').read_text(), '3\tsol\n')   # nothing has applied the pin yet
        self.assertIn('already in the highest tier', (self.tdir / 'split.suggested').read_text())
        (self.tdir / 'escalation.pin').unlink()
        result = self.route('--check')
        self.assertEqual((result.returncode, result.stdout), (3, ''), result.stderr)
        self.assertIsNone(self.next_marker())                        # the old pin is not the next model any more
        self.assertTrue((self.tdir / 'escalation.stop').exists())

    def test_check_replaces_a_stale_policy_marker_with_the_pin_and_keeps_none_of_its_own(self):
        self.fail_three_times({'low': ['sol']})
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        (self.plan / 'escalate_after').write_text('0\n')
        (self.tdir / 'escalation.stop').write_text('3\tsol\n')
        self.assertEqual(self.route('--check').stdout, 'opus:high\n')
        self.assertEqual(sorted(p.name for p in self.tdir.iterdir() if p.name.startswith(('escalation.', 'split.'))),
                         ['escalation.next', 'escalation.pin'])      # the policy is off: its markers go

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

    def test_check_does_not_report_a_pin_behind_an_unreviewed_verdict_of_committed_work(self):
        (self.tdir / 'escalation.pin').write_text('opus:high\n')
        (self.tdir / 'status').write_text('UNKNOWN\n')              # prepare_retry will turn this into `resume`
        (self.tdir / 'reviewed.commit').write_text('abc123\n')
        self.assertEqual(self.route('--check').stdout, '')
        self.assertIsNone(self.next_marker())
        (self.tdir / 'reviewed.commit').unlink()                     # nothing reviewed: a full retry
        self.assertEqual(self.route('--check').stdout, 'opus:high\n')

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

    # --- an infrastructure stop resumed inside the run ------------------------------------
    QUOTA = {'rc': 1, 'stderr': 'Claude AI usage limit reached|1760000000'}

    def run_pinning_during_the_backoff(self, p, pin='astra\n'):
        """`run --infra-retries 1` with a backoff long enough to pin the task once its first
        entry has stopped on the infrastructure failure, and before the scheduler resumes it."""
        self.env['FORGE_INFRA_BACKOFF'] = '3'
        out = self.root / 'run.out'
        with open(out, 'w') as log:
            proc = subprocess.Popen(['bash', str(ROOT / 'scripts' / 'forge-parallel.sh'), 'run', str(p),
                                     '--infra-retries', '1'], cwd=self.repo, env=self.env, stdout=log,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            try:
                status = p / 'tasks/a/status'
                deadline = time.time() + 60
                while time.time() < deadline and proc.poll() is None:
                    if status.exists() and status.read_text().strip() == 'INFRA':
                        break
                    time.sleep(0.02)
                self.assertIsNone(proc.poll(), out.read_text())
                (p / 'tasks/a/escalation.pin').write_text(pin)
                proc.wait(timeout=120)
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, 9)
                    proc.wait()
        return proc.returncode, out.read_text()

    def test_an_implementer_stage_infra_resume_runs_the_pinned_model(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        self.scenario({'a.dwarf.1': self.QUOTA})
        rc, out = self.run_pinning_during_the_backoff(p)
        self.assertEqual(rc, 0, out)
        self.assertIn('waiting', out)
        self.assertNotIn('automatic retry', out)                 # a resume, not a retry
        self.assertEqual(self.calls(), ['dwarf', 'dwarf', 'qa'])
        self.assertIn('model=gpt-6-astra', (p / 'tasks/a/dwarf.resolved').read_text())
        self.assertEqual(tsv(p / 'tasks/a/escalations.tsv')[0][1:], ['0', 'sol', 'astra', 'pin'])
        self.assertEqual(sorted(x.name for x in (p / 'tasks/a').glob('escalation.*')), [])
        self.assertEqual([r[4] for r in tsv(p / 'tasks/a/attempts.tsv')], ['sol', 'astra'])
        self.assertTrue((p / 'tasks/a/merged').exists())

    def test_a_qa_stage_infra_resume_keeps_the_pin_pending(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        # No reset time in the message, so the test's short backoff is the wait.
        self.scenario({'a.qa.1': {'rc': 1, 'is_error': True, 'verdict': 'none',
                                  'findings': "You've hit your usage limit."}})
        rc, out = self.run_pinning_during_the_backoff(p)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'qa'])    # QA only: the implementer did not run again
        self.assertIn('pin waits', out)
        self.assertEqual((p / 'tasks/a/escalation.pin').read_text(), 'astra\n')
        self.assertFalse((p / 'tasks/a/escalations.tsv').exists())
        self.assertEqual([r[4] for r in tsv(p / 'tasks.tsv') if r[0] == 'a'], ['sol'])

    def test_a_re_run_after_an_implementer_stage_infra_stop_runs_the_pinned_model(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        self.scenario({'a.dwarf.1': self.QUOTA})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 8, r.stdout)
        (p / 'tasks/a/escalation.pin').write_text('astra\n')
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('model=gpt-6-astra', (p / 'tasks/a/dwarf.resolved').read_text())
        self.assertEqual(tsv(p / 'tasks/a/escalations.tsv')[0][2:], ['sol', 'astra', 'pin'])

    # --- an unreviewed verdict whose worktree changed ---------------------------------------
    def test_retry_of_an_unreviewed_task_whose_worktree_changed_runs_the_pinned_model(self):
        p = self.pooled_plan('--dwarf-low', 'sol', '--dwarf-high', 'astra')
        self.scenario({'a.qa.1': {'verdict': 'none', 'findings': 'I looked, but I will not say.'},
                       'a.dwarf.2': {'content': 'rewritten'}})
        r = self.run_script('forge-parallel.sh', 'run', p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual((p / 'tasks/a/status').read_text().strip(), 'UNKNOWN')
        self.assertTrue((p / 'tasks/a/reviewed.commit').read_text().strip())
        wt = Path((p / 'wt_root').read_text().strip()) / 'a'
        (wt / 'a.txt').write_text('edited after the review\n')
        (p / 'tasks/a/escalation.pin').write_text('astra\n')
        r = self.run_script('forge-parallel.sh', 'retry', p, 'a')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('no longer matches the worktree', r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa', 'dwarf', 'qa'])
        self.assertIn('model=gpt-6-astra', (p / 'tasks/a/dwarf.resolved').read_text())
        self.assertEqual(tsv(p / 'tasks/a/escalations.tsv')[0][1:], ['1', 'sol', 'astra', 'pin'])
        self.assertFalse((p / 'tasks/a/escalation.pin').exists())


if __name__ == '__main__':
    unittest.main()
