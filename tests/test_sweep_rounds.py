"""Sweep rounds (#152): `--sweep Pn` sweeps each finding once, in rounds, and records what a sweep fixed.

The planner cases run forge-sweep.py on hand-made plans (test_sweep.PlanBase); the scheduler cases use a
worker standing in for forge-parallel.sh whose `_sweep` is the real planner. State and config homes are
isolated per test."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_sweep import ISSUES, ROOT, SWEEP, PlanBase, known

sys.path.insert(0, str(ROOT / 'scripts'))
import forge_identity  # noqa: E402
from forge_findings import fingerprint, store  # noqa: E402


class RoundsBase(PlanBase):
    def setUp(self):
        super().setUp()
        self.config = self.root / 'config'
        self.env.update(XDG_CONFIG_HOME=str(self.config), FORGE_DATASET='off')
        patcher = patch.dict(os.environ, {'XDG_STATE_HOME': self.env['XDG_STATE_HOME'], 'XDG_CONFIG_HOME': str(self.config)})
        patcher.start(); self.addCleanup(patcher.stop)

    def add_task(self, *args, **kwargs):
        """PlanBase.task rewrites tasks.tsv from its own rows: keep the fix tasks a sweep appended."""
        self.rows = [line for line in (self.plan / 'tasks.tsv').read_text().splitlines()
                     if line and not line.startswith('#')] if (self.plan / 'tasks.tsv').exists() else []
        return self.task(*args, **kwargs)

    def ident(self):
        return forge_identity.identity(self.plan)

    def fp(self, file, text):
        return fingerprint('o/r', file, text)

    def round_file(self, number, name='plan.json'):
        base = self.plan / 'sweep'
        return (base if number == 1 else base / 'rounds' / str(number)) / name

    def round(self, number):
        return json.loads(self.round_file(number).read_text())

    def keys(self, number):
        return [(t['id'], [f['text'] for f in t['findings']]) for t in self.round(number)['tasks']]

    def reasons(self, number):
        return sorted((d['text'], d['reason'], d['detail']) for d in self.round(number)['dropped'])


class Rounds(RoundsBase):
    def test_a_second_plan_sweeps_only_what_round_one_did_not_attempt_and_continues_the_ids(self):
        self.add_task('a', {'a.txt': 'alpha\nbeta\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.',
                                                               '- [P3][CONFIRMED] a.txt:2 — Two.'))
        self.assertEqual(self.sweep('--severity', 'P3').stdout.split(), ['sweep-1'])
        self.assertEqual(self.round(1)['round'], 1)
        # round 1 fixed nothing (no review verdict was settled): its findings are still not swept again
        self.add_task('b', {'b.txt': 'gamma\n'}, known('- [P3][CONFIRMED] b.txt:1 — Three.'))
        before = self.round_file(1).read_text()
        result = self.sweep('--severity', 'P3')
        self.assertEqual(result.stdout.split(), ['sweep-2'])
        self.assertEqual(self.round_file(1).read_text(), before)                    # round 1 is left as it was
        self.assertEqual(self.round(2)['round'], 2)
        self.assertEqual(self.keys(2), [('sweep-2', ['Three.'])])
        self.assertEqual(self.reasons(2), [('One.', 'attempted', {'task': 'sweep-1'}),
                                           ('Two.', 'attempted', {'task': 'sweep-1'})])
        self.assertIn('round 2', self.round_file(2, 'plan.md').read_text())
        self.assertIn('an earlier sweep round already tried it', self.round_file(2, 'plan.md').read_text())
        rows = (self.plan / 'tasks.tsv').read_text().splitlines()
        self.assertEqual([row.split('\t')[0] for row in rows if row.startswith('sweep-')], ['sweep-1', 'sweep-2'])
        self.assertTrue((self.plan / 'tasks/sweep-2/sweep.json').is_file())
        # every finding is attempted now: a third plan prints and writes nothing
        tsv = (self.plan / 'tasks.tsv').read_text()
        self.assertEqual(self.sweep('--severity', 'P3').stdout, '')
        self.assertFalse(self.round_file(3).exists())
        self.assertEqual((self.plan / 'tasks.tsv').read_text(), tsv)

    def test_a_later_round_with_nothing_new_writes_no_plan(self):
        self.add_task('a', {'a.txt': 'alpha\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.'))
        self.sweep('--severity', 'P3')
        result = self.sweep('--severity', 'P3')
        self.assertEqual((result.stdout, result.stderr), ('', ''))
        self.assertFalse((self.plan / 'sweep/rounds').exists())

    def test_a_fixed_filed_or_duplicate_record_is_left_out_as_recorded_and_a_dismissed_one_as_dismissed(self):
        texts = ['Open.', 'Fixed.', 'Filed.', 'Dupe.', 'Dismissed.']
        self.add_task('a', {'a.txt': 'a1\na2\na3\na4\na5\n'}, known(*['- [P3][CONFIRMED] a.txt:%d — %s' % (n, t)
                                                                       for n, t in enumerate(texts, 1)]))
        ident = self.ident()
        store.set_status(ident, [self.fp('a.txt', 'Fixed.')], 'fixed', 'cli', reason='by hand')
        store.set_status(ident, [self.fp('a.txt', 'Filed.')], 'filed', 'cli', issue='https://github.com/o/r/issues/1')
        store.set_status(ident, [self.fp('a.txt', 'Dupe.')], 'duplicate', 'cli', duplicate_of=self.fp('a.txt', 'Open.'))
        store.set_status(ident, [self.fp('a.txt', 'Dismissed.')], 'dismissed', 'cli', reason='no')
        self.assertEqual(self.sweep('--severity', 'P3').stdout.split(), ['sweep-1'])
        self.assertEqual(self.keys(1), [('sweep-1', ['Open.'])])
        self.assertEqual(self.reasons(1), [('Dismissed.', 'dismissed', {}), ('Dupe.', 'recorded', {'status': 'duplicate'}),
                                           ('Filed.', 'recorded', {'status': 'filed'}),
                                           ('Fixed.', 'recorded', {'status': 'fixed'})])

    def test_a_to_file_or_reopened_record_is_still_swept(self):
        self.add_task('a', {'a.txt': 'a1\na2\n'}, known('- [P3][CONFIRMED] a.txt:1 — Queued.',
                                                          '- [P3][CONFIRMED] a.txt:2 — Reopened.'))
        ident = self.ident()
        store.set_status(ident, [self.fp('a.txt', 'Queued.')], 'to-file', 'cli')
        store.set_status(ident, [self.fp('a.txt', 'Reopened.')], 'dismissed', 'cli', reason='no')
        store.set_status(ident, [self.fp('a.txt', 'Reopened.')], 'open', 'cli')
        self.sweep('--severity', 'P3')
        self.assertEqual(self.keys(1), [('sweep-1', ['Queued.', 'Reopened.'])])

    def test_the_prompt_step_skips_findings_the_record_settled(self):
        self.add_task('a', {'a.txt': 'alpha\nbeta\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.',
                                                               '- [P2][CONFIRMED] a.txt:2 — Two.'))
        store.set_status(self.ident(), [self.fp('a.txt', 'Two.')], 'fixed', 'cli', reason='by hand')
        self.add_task('later', ['a.txt'], merge=False)
        out = self.sweep('later', '--role', 'dwarf', command='ahead').stdout
        self.assertIn('One.', out)
        self.assertNotIn('Two.', out)

    def test_an_unreadable_record_leaves_the_findings_in_and_settle_still_succeeds(self):
        self.add_task('a', {'a.txt': 'alpha\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.'))
        broken = self.root / 'not-a-directory'
        broken.write_text('x')
        self.env['XDG_STATE_HOME'] = str(broken)
        self.assertEqual(self.sweep('--severity', 'P3').stdout.split(), ['sweep-1'])
        (self.plan / 'tasks/sweep-1/qa.last').write_text('- S1: FIXED\nFORGE_VERDICT: PASS\n')
        (self.plan / 'accepted.integration').write_text(self.git(self.int, 'rev-parse', 'HEAD') + '\n')
        self.sweep('sweep-1', command='settle')
        outcome = json.loads((self.plan / 'tasks/sweep-1/sweep.outcome.json').read_text())['findings']
        self.assertEqual([o['status'] for o in outcome], ['fixed'])


class Check(RoundsBase):
    def check(self, code):
        result = subprocess.run([sys.executable, str(SWEEP), 'plan', str(self.plan), '--severity', 'P3', '--check'],
                                capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(result.returncode, code, result.stderr)
        return json.loads(result.stdout)

    def test_check_counts_without_writing_and_exits_by_whether_anything_is_eligible(self):
        self.add_task('a', {'a.txt': 'alpha\nbeta\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.',
                                                               '- [P3][CONFIRMED] a.txt:2 — Two.',
                                                               '- [P3][CONFIRMED] general — Nowhere.'))
        tsv = (self.plan / 'tasks.tsv').read_text()
        self.assertEqual(self.check(0), {'eligible': 2, 'attempted': 0, 'left': 1})
        self.assertEqual((self.plan / 'tasks.tsv').read_text(), tsv)
        self.assertFalse((self.plan / 'sweep').exists())
        self.assertEqual(sorted(p.name for p in (self.plan / 'tasks').iterdir()), ['a'])
        self.sweep('--severity', 'P3')
        self.assertEqual(self.check(1), {'eligible': 0, 'attempted': 2, 'left': 1})
        self.add_task('b', {'b.txt': 'gamma\n'}, known('- [P3][CONFIRMED] b.txt:1 — Three.'))
        self.assertEqual(self.check(0), {'eligible': 1, 'attempted': 2, 'left': 1})

    def test_check_is_reachable_through_the_runners_internal_command(self):
        self.add_task('a', {'a.txt': 'alpha\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.'))
        result = subprocess.run(['bash', str(ROOT / 'scripts/forge-parallel.sh'), '_sweep', str(self.plan),
                                 '--severity', 'P3', '--check'], capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual((result.returncode, json.loads(result.stdout)), (0, {'eligible': 1, 'attempted': 0, 'left': 0}))


class Settle(RoundsBase):
    def test_settle_records_each_finding_the_review_marked_fixed(self):
        self.add_task('a', {'a.txt': 'alpha\nbeta\ngamma\n'}, known(*['- [P3][CONFIRMED] a.txt:%d — Problem %d.' % (n, n)
                                                                        for n in (1, 2, 3)]))
        self.sweep('--severity', 'P3')
        commit = self.git(self.int, 'rev-parse', 'HEAD')
        (self.plan / 'accepted.integration').write_text(commit + '\n')
        (self.plan / 'tasks/sweep-1/qa.last').write_text('- S1: FIXED\n- S2: NOT FIXED — still wrong\nFORGE_VERDICT: PASS\n')
        (self.plan / 'tasks/sweep-1/merged').touch()  # a real run settles only after the merge
        self.sweep('sweep-1', command='settle')
        record = store.load(self.ident())
        fixed = record[self.fp('a.txt', 'Problem 1.')]
        self.assertEqual((fixed['status'], fixed['actor'], fixed['reason'], fixed['commit']),
                         ('fixed', 'sweep', 'sweep sweep-1', commit))
        self.assertNotIn(self.fp('a.txt', 'Problem 2.'), record)                     # not fixed
        self.assertNotIn(self.fp('a.txt', 'Problem 3.'), record)                     # unreported
        # the next plan drops it as recorded, the ones it did not fix as attempted
        self.add_task('b', {'b.txt': 'x\n'}, known('- [P3][CONFIRMED] b.txt:1 — New.'))
        self.sweep('--severity', 'P3')
        self.assertEqual(self.keys(2), [('sweep-2', ['New.'])])
        self.assertEqual(self.reasons(2), [('Problem 1.', 'recorded', {'status': 'fixed'}),
                                           ('Problem 2.', 'attempted', {'task': 'sweep-1'}),
                                           ('Problem 3.', 'attempted', {'task': 'sweep-1'})])


# A worker standing in for forge-parallel.sh: tasks end with PASS (or the next line of outcomes/<name>) and
# merge; `_sweep` is the real planner.
WORKER = r'''#!/bin/bash
cmd="$1"; plan="$2"; name="$3"; t="$plan/tasks/$name"
case "$cmd" in
  _task)
    outcome="$(head -1 "$plan/outcomes/$name" 2>/dev/null)"
    echo "${outcome:-PASS}" > "$t/status"; echo "$name end ${outcome:-PASS}" >> "$plan/events.log" ;;
  _merge)
    [ "$(cat "$t/status")" = PASS ] && { touch "$t/merged"; echo "$name merged" >> "$plan/events.log"; } ;;
  _waves) ;;
  _sweep)
    echo "sweep ${*:3}" >> "$plan/events.log"
    python3 "$SWEEP" plan "$plan" "${@:3}" ;;
esac
'''


class Scheduler(RoundsBase):
    def setUp(self):
        super().setUp()
        (self.plan / 'outcomes').mkdir()
        self.worker = self.root / 'worker.sh'
        self.worker.write_text(WORKER)
        self.env['SWEEP'] = str(SWEEP)
        self.add_task('a', {'a.txt': 'alpha\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.'))

    def schedule(self, *extra, code=0):
        log = self.plan / 'events.log'
        log.unlink(missing_ok=True)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker), str(self.plan),
                                 '3', '--sweep', 'P3', *extra], capture_output=True, text=True, timeout=120, env=self.env)
        self.assertEqual(result.returncode, code, result.stderr)
        return log.read_text().splitlines() if log.exists() else []

    def report(self):
        return self.sweep(command='report').stdout

    def test_a_rerun_that_merged_new_findings_starts_round_two_and_one_with_nothing_new_says_so(self):
        self.assertEqual(self.schedule(), ['sweep --severity P3 --max 5', 'sweep-1 end PASS', 'sweep-1 merged'])
        self.assertTrue(self.round_file(1).is_file())
        self.assertNotIn('nothing new', self.report())
        # nothing new: no round, no planner call, and the summary says so
        self.assertEqual(self.schedule(), [])
        self.assertFalse(self.round_file(2).exists())
        nothing = json.loads(self.round_file(1, 'nothing.json').read_text())
        self.assertEqual((nothing['attempted'], nothing['left']), (1, 0))
        report = self.report()
        self.assertIn('finding sweep (P3 and worse): 1 fix task(s), 0 of 1 finding(s) fixed; 1 left for forge issues', report)
        self.assertIn('sweep: nothing new to sweep (1 finding(s) already swept or attempted, 0 left for forge issues)', report)
        # a rerun that merged new work with a finding starts round 2
        self.add_task('b', {'b.txt': 'beta\n'}, known('- [P3][CONFIRMED] b.txt:1 — Two.'))
        self.assertEqual(self.schedule(), ['sweep --severity P3 --max 5', 'sweep-2 end PASS', 'sweep-2 merged'])
        self.assertEqual(self.keys(2), [('sweep-2', ['Two.'])])
        report = self.report()
        self.assertIn('finding sweep round 1 (P3 and worse)', report)
        self.assertIn('finding sweep round 2 (P3 and worse): 1 fix task(s)', report)
        self.assertNotIn('nothing new', report)                    # older than round 2's plan
        self.assertEqual(self.schedule(), [])                      # everything was attempted: nothing new again
        self.assertIn('nothing new to sweep (2 finding(s) already swept or attempted', self.report())

    def test_a_stopped_fix_task_of_the_last_round_does_not_hold_the_next_round(self):
        (self.plan / 'outcomes' / 'sweep-1').write_text('FAIL\n')
        self.assertEqual(self.schedule(), ['sweep --severity P3 --max 5', 'sweep-1 end FAIL'])
        self.add_task('b', {'b.txt': 'beta\n'}, known('- [P3][CONFIRMED] b.txt:1 — Two.'))
        self.assertEqual(self.schedule(), ['sweep --severity P3 --max 5', 'sweep-2 end PASS', 'sweep-2 merged'])

    def test_a_task_that_has_not_merged_holds_the_next_round(self):
        self.schedule()
        self.add_task('b', {'b.txt': 'beta\n'}, known('- [P3][CONFIRMED] b.txt:1 — Two.'), merge=False)
        (self.plan / 'outcomes' / 'b').write_text('FAIL\n')
        (self.plan / 'tasks/b/status').unlink()
        self.assertEqual(self.schedule(code=5), ['b end FAIL'])
        self.assertFalse(self.round_file(2).exists())


if __name__ == '__main__':
    unittest.main()
