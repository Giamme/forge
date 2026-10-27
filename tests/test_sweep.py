"""The finding sweep (#73): `run --sweep Pn` turns the findings a run left open into fix tasks once every
task has merged, and `run --sweep-ahead` hands a task the open findings in files it owns.

The planner (forge-sweep.py plan) runs on hand-made plans over a real git repository and integration
branch; the scheduler cases use a worker standing in for forge-parallel.sh, as test_control_requests.py
does; the last cases drive real runs with the fake model CLIs of test_forge.py."""
import dataset_isolation  # noqa: F401  keeps the run dataset out of the developer's store
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
SWEEP = ROOT / 'scripts/forge-sweep.py'
ISSUES = ROOT / 'scripts/forge-issues.py'
HEADER = '# id\tdeps\tdifficulty\tfiles\tdwarf\tqa\ttitle\n'


def known(*lines):
    return '## Known issues\n\nTolerated under the P2 threshold.\n\n' + '\n'.join(lines) + '\n'


class PlanBase(unittest.TestCase):
    """A plan whose tasks merged onto a real integration branch, with the findings their reviews left."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.root / 'state'), GIT_AUTHOR_NAME='t',
                        GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
        self.git(self.repo, 'init', '-q')
        self.git(self.repo, 'remote', 'add', 'origin', 'git@github.com:o/r.git')
        for name in ('a.txt', 'b.txt', 'c.txt'):
            (self.repo / name).write_text(''.join('%s line %d\n' % (name, n) for n in range(1, 6)))
        self.git(self.repo, 'add', '.'); self.git(self.repo, 'commit', '-qm', 'initial')
        self.plan = self.root / 'plan'; (self.plan / 'tasks').mkdir(parents=True)
        self.wt = self.root / 'wt'; self.wt.mkdir()
        self.int = self.wt / '_integration'
        self.git(self.repo, 'worktree', 'add', '-q', '-b', 'forge/run-integration', str(self.int))
        for name, text in (('repo', str(self.repo)), ('wt_root', str(self.wt)), ('run_id', 'run')):
            (self.plan / name).write_text(text + '\n')
        self.rows = []

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True,
                              env=self.env).stdout.strip()

    def task(self, ident, files=None, known_issues=None, qa=None, dwarf='sol', reviewer='opus', merge=True):
        """A task that changed `files` on its branch, was reviewed there and merged onto the integration branch."""
        self.rows.append('\t'.join([ident, '-', 'low', ','.join(files or ['-']), dwarf, reviewer, ident.upper()]))
        (self.plan / 'tasks.tsv').write_text(HEADER + ''.join(r + '\n' for r in self.rows))
        root = self.plan / 'tasks' / ident; root.mkdir()
        (root / 'prompt.md').write_text('Do ' + ident + '.\n')
        (root / 'status').write_text('PASS\n')
        if known_issues:
            (root / 'known_issues.md').write_text(known_issues)
        (root / 'qa.last').write_text((qa or '') + '\nFORGE_VERDICT: PASS\n')
        branch = 'forge/run/' + ident
        self.git(self.int, 'checkout', '-q', '-b', branch)
        for path, text in (files or {}).items() if isinstance(files, dict) else []:
            if text is None:
                self.git(self.int, 'rm', '-q', path)
            else:
                (self.int / path).write_text(text)
        self.git(self.int, 'add', '-A'); self.git(self.int, 'commit', '-q', '--allow-empty', '-m', ident)
        (root / 'reviewed.commit').write_text(self.git(self.int, 'rev-parse', 'HEAD') + '\n')
        self.git(self.int, 'checkout', '-q', 'forge/run-integration')
        if merge:
            self.git(self.int, 'merge', '-q', '--no-ff', '-m', 'forge: merge ' + ident, branch)
            (root / 'merged').touch()
        return root

    def sweep(self, *args, command='plan', code=0):
        result = subprocess.run([sys.executable, str(SWEEP), command, str(self.plan), *map(str, args)],
                                capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(result.returncode, code, result.stderr)
        return result

    def record(self):
        return json.loads((self.plan / 'sweep/plan.json').read_text())

    def dropped(self):
        return sorted((d['task'], '%s:%s' % (d['file'], d['line']), d['reason']) for d in self.record()['dropped'])


class Planner(PlanBase):
    def test_open_findings_become_one_fix_task_per_file_worst_first(self):
        self.task('a', {'a.txt': 'a one\na two\na three\n'}, known(
            '- [P3][CONFIRMED] a.txt:1 — The first line is lower case.',
            '- [P2][CONFIRMED] a.txt:2 — The second line loses its number.'), dwarf='sol:high', reviewer='opus:xhigh')
        self.task('b', {'b.txt': 'b one\n'}, known('- [P3][CONFIRMED] b.txt:1 — Trailing space.'))
        result = self.sweep('--severity', 'P3')
        self.assertEqual(result.stdout.split(), ['sweep-1', 'sweep-2'])
        rows = (self.plan / 'tasks.tsv').read_text().splitlines()[-2:]
        self.assertEqual(rows, ['sweep-1\t-\tlow\ta.txt\tsol:high\topus:xhigh\tFix 2 review findings in a.txt',
                                'sweep-2\t-\tlow\tb.txt\tsol\topus\tFix 1 review finding in b.txt'])
        items = json.loads((self.plan / 'tasks/sweep-1/sweep.json').read_text())['findings']
        self.assertEqual([(i['id'], i['severity'], i['location'], i['task']) for i in items],
                         [('S1', 'P2', 'a.txt:2', 'a'), ('S2', 'P3', 'a.txt:1', 'a')])
        prompt = (self.plan / 'tasks/sweep-1/prompt.md').read_text()
        self.assertIn('- S1 [P2][CONFIRMED] a.txt:2 — The second line loses its number.  (task a)', prompt)
        self.assertIn('## Acceptance criteria', prompt)
        self.assertEqual(self.record()['estimate'], {'dispatches': 4})
        self.assertIn('forge: sweep: # Finding sweep: 2 fix task(s) for 3 finding(s)', result.stderr)
        # one sweep per plan: a second call plans nothing and leaves tasks.tsv alone
        before = (self.plan / 'tasks.tsv').read_text()
        self.assertEqual(self.sweep('--severity', 'P3').stdout, '')
        self.assertEqual((self.plan / 'tasks.tsv').read_text(), before)

    def test_triage_leaves_each_kind_of_finding_for_forge_issues_with_its_reason(self):
        self.task('a', {'a.txt': 'alpha\nbeta\ngamma\n'}, known(
            '- [P2][CONFIRMED] a.txt:2 — Beta is spelled wrong.',
            '- [P3][CONFIRMED] a.txt:1 — Alpha should be capitalised.',          # below --severity P2
            '- [P2][CONFIRMED] a.txt:3 — Gamma breaks the parser.',              # changed by c's merge
            '- [P2][CONFIRMED] general — Logging is too chatty.',                # no file to fix it in
            '- [P2][CONFIRMED] b.txt:1 — The b header is stale.',                # b.txt deleted by c
            '- [P2][CONFIRMED] a.txt:2 — Dismissed by a person.'))
        self.task('d', {'d.txt': 'delta\n'}, known('- [P2][CONFIRMED] a.txt:2 — Beta is spelled wrong.'))   # again
        self.task('c', {'a.txt': 'alpha\nbeta\nGAMMA\n', 'b.txt': None})
        issues = subprocess.run([sys.executable, str(ISSUES), str(self.plan), '--offline', '--json'], env=self.env,
                                capture_output=True, text=True, check=True)
        fp = next(f['fp'] for f in json.loads(issues.stdout)['findings'] if f['text'] == 'Dismissed by a person.')
        subprocess.run([sys.executable, str(ISSUES), str(self.plan), '--dismiss', fp], env=self.env, check=True,
                       capture_output=True)
        self.assertEqual(self.sweep('--severity', 'P2').stdout.split(), ['sweep-1'])
        self.assertEqual([i['location'] for i in json.loads((self.plan / 'tasks/sweep-1/sweep.json').read_text())['findings']],
                         ['a.txt:2'])
        self.assertEqual(self.dropped(), [('a', 'a.txt:1', 'below-severity'), ('a', 'a.txt:2', 'dismissed'),
                                          ('a', 'a.txt:3', 'changed'), ('a', 'b.txt:1', 'file-gone'),
                                          ('a', 'general:None', 'no-location'), ('d', 'a.txt:2', 'duplicate')])
        changed = next(d for d in self.record()['dropped'] if d['reason'] == 'changed')
        self.assertEqual(changed['detail'], {'merged_since': ['c']})
        self.assertIn('the code it points at changed after the review (merged since: c)',
                      (self.plan / 'sweep/plan.md').read_text())

    def test_a_line_that_only_moved_is_still_swept(self):
        self.task('a', {'a.txt': 'alpha\nbeta\n'}, known('- [P3][CONFIRMED] a.txt:2 — Beta is spelled wrong.'))
        self.task('c', {'a.txt': 'new first line\nalpha\nbeta\n'})
        self.assertEqual(self.sweep('--severity', 'P3').stdout.split(), ['sweep-1'])

    def test_plausible_findings_only_on_request_and_never_accepted_or_a_fix_tasks_own(self):
        self.task('a', {'a.txt': 'alpha\n'}, known(
            '- [P2][CONFIRMED] a.txt:1 — Tolerated.',
            '',
            "## Accepted by a human despite the reviewer's FAIL verdict",
            '',
            '- [P1][CONFIRMED] b.txt:1 — A person accepted this one.'),
            qa='- [P3][PLAUSIBLE] c.txt:2 — Maybe c drops a line.')
        fix = self.task('sweep-9', {'d.txt': 'fixed\n'}, known('- [P3][CONFIRMED] d.txt:1 — The fix itself regressed.'))
        (fix / 'sweep.json').write_text('{"findings": []}\n')
        self.sweep('--severity', 'P3')
        self.assertEqual([(t['file'], [i['text'] for i in t['findings']]) for t in self.record()['tasks']],
                         [('a.txt', ['Tolerated.'])])
        self.assertEqual(self.record()['dropped'], [])
        (self.plan / 'sweep/plan.json').unlink()
        self.sweep('--severity', 'P3', '--plausible')
        self.assertEqual([t['file'] for t in self.record()['tasks']], ['a.txt', 'c.txt'])

    def test_the_caps_leave_the_rest_for_forge_issues(self):
        self.task('a', {'a.txt': ''.join('line %d\n' % n for n in range(1, 11))},
                  known(*['- [P3][CONFIRMED] a.txt:%d — Problem %d.' % (n, n) for n in range(1, 11)]))
        self.task('b', {'b.txt': 'b\n'}, known('- [P2][CONFIRMED] b.txt:1 — Worse problem.'))
        self.sweep('--severity', 'P3', '--max', '1')
        self.assertEqual([t['file'] for t in self.record()['tasks']], ['b.txt'])     # worst severity first
        self.assertEqual({d['reason'] for d in self.record()['dropped']}, {'over-max'})
        (self.plan / 'sweep/plan.json').unlink()
        self.sweep('--severity', 'P3')
        tasks = self.record()['tasks']
        self.assertEqual([(t['file'], len(t['findings'])) for t in tasks], [('b.txt', 1), ('a.txt', 8)])
        self.assertEqual(sorted(d['text'] for d in self.record()['dropped'] if d['reason'] == 'group-cap'),
                         ['Problem 10.', 'Problem 9.'])

    def test_the_plans_low_tier_implements_the_fixes(self):
        (self.plan / 'fractal-routing.json').write_text(json.dumps({'low': ['luna:low'], 'high': ['sol:xhigh']}))
        self.task('a', {'a.txt': 'alpha\n'}, known('- [P3][CONFIRMED] a.txt:1 — Tolerated.'), dwarf='sol:xhigh')
        self.sweep('--severity', 'P3')
        self.assertEqual((self.record()['tasks'][0]['dwarf'], self.record()['tasks'][0]['qa']), ('luna:low', 'opus'))

    def test_the_fix_tasks_of_one_sweep_share_a_wave(self):
        self.task('a', {'a.txt': 'alpha\n', 'b.txt': 'beta\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.',
                                                                         '- [P3][CONFIRMED] b.txt:1 — Two.'))
        self.task('b', {'c.txt': 'gamma\n'}, known('- [P3][CONFIRMED] c.txt:1 — Three.'))
        self.sweep('--severity', 'P3')
        subprocess.run(['bash', str(ROOT / 'scripts/forge-parallel.sh'), '_waves', str(self.plan)], check=True,
                       env=self.env, capture_output=True)
        waves = dict(line.split('\t')[::-1] for line in (self.plan / 'waves.tsv').read_text().splitlines())
        self.assertEqual(len({waves['sweep-1'], waves['sweep-2'], waves['sweep-3']}), 1)

    def test_settle_reads_one_line_per_finding_from_the_review(self):
        self.task('a', {'a.txt': 'alpha\nbeta\ngamma\n'}, known(*['- [P3][CONFIRMED] a.txt:%d — Problem %d.' % (n, n)
                                                                    for n in (1, 2, 3)]))
        self.sweep('--severity', 'P3')
        fix = self.plan / 'tasks/sweep-1'
        (fix / 'qa.last').write_text('Checked each.\n\n'
                                     '- S<n>: FIXED\n'                                  # an echoed placeholder
                                     '* **S2**: NOT FIXED — still wrong for empty input\n'
                                     '- S1 — fixed\n'
                                     '- S2: FIXED\n'                                     # the first line counts
                                     '\nFORGE_VERDICT: PASS\n')
        self.sweep('sweep-1', command='settle')
        outcome = json.loads((fix / 'sweep.outcome.json').read_text())['findings']
        self.assertEqual([(o['id'], o['status'], o['note']) for o in outcome],
                         [('S1', 'fixed', ''), ('S2', 'not-fixed', 'still wrong for empty input'),
                          ('S3', 'unreported', '')])

    def test_the_prompt_step_hands_a_task_the_open_findings_in_files_it_owns(self):
        self.task('a', {'a.txt': 'alpha\nbeta\n', 'b.txt': 'b\n'}, known(
            '- [P3][CONFIRMED] a.txt:1 — One.', '- [P2][CONFIRMED] a.txt:2 — Two.',
            '- [P1][CONFIRMED] b.txt:1 — Not this task\'s file, and the worst.'))
        self.task('later', ['a.txt', 'docs'], merge=False)
        dwarf = self.sweep('later', '--role', 'dwarf', '--cap', '1', command='ahead').stdout
        self.assertIn('## Open findings on your files', dwarf)
        self.assertIn('- [P2][CONFIRMED] a.txt:2 — Two.  (task a)', dwarf)              # worst first, one kept
        self.assertNotIn('One.', dwarf)
        self.assertNotIn('b.txt', dwarf)
        # every attempt and its reviewer get the list picked first
        (self.plan / 'tasks/a/known_issues.md').write_text(known('- [P0][CONFIRMED] a.txt:1 — New.'))
        review = self.sweep('later', '--role', 'qa', command='ahead').stdout
        self.assertIn('A fix of one is in scope', review)
        self.assertIn('a.txt:2 — Two.', review)
        self.assertNotIn('New.', review)
        self.assertEqual(self.sweep('a', command='ahead').stdout, '')                       # its own findings

    def test_the_report_counts_what_the_sweep_fixed(self):
        self.task('a', {'a.txt': 'alpha\nbeta\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.', '- [P3][CONFIRMED] a.txt:2 — Two.'))
        self.task('b', {'b.txt': 'b\n'}, known('- [P3][CONFIRMED] b.txt:1 — Three.', '- [P3][CONFIRMED] gone — Four.'))
        self.sweep('--severity', 'P3')
        for ident, reply in (('sweep-1', '- S1: FIXED\n- S2: NOT FIXED — no\n'), ('sweep-2', '- S1: FIXED\n')):
            root = self.plan / 'tasks' / ident
            (root / 'qa.last').write_text(reply + 'FORGE_VERDICT: PASS\n')
            self.sweep(ident, command='settle')
        (self.plan / 'tasks/sweep-1/merged').touch()
        (self.plan / 'tasks/sweep-2/status').write_text('FAIL\n')
        report = self.sweep(command='report').stdout
        self.assertIn('finding sweep (P3 and worse): 2 fix task(s), 1 of 3 finding(s) fixed; 3 left for forge issues', report)
        self.assertIn('sweep-2        FAIL      0 of 1 fixed  b.txt', report)


class Issues(PlanBase):
    def test_forge_issues_files_only_what_the_sweep_did_not_fix(self):
        self.task('a', {'a.txt': 'alpha\nbeta\n'}, known('- [P3][CONFIRMED] a.txt:1 — One.', '- [P3][CONFIRMED] a.txt:2 — Two.'))
        self.sweep('--severity', 'P3')
        fix = self.plan / 'tasks/sweep-1'
        (fix / 'qa.last').write_text('- S1: FIXED\n- S2: NOT FIXED — still there\nFORGE_VERDICT: PASS\n')
        self.sweep('sweep-1', command='settle')

        def listing():
            result = subprocess.run([sys.executable, str(ISSUES), str(self.plan), '--offline', '--json'], env=self.env,
                                    capture_output=True, text=True, check=True)
            return json.loads(result.stdout)

        out = listing()                    # not merged yet: nothing is fixed
        self.assertEqual(sorted(d['findings'][0][-4:] for d in out['drafts']), ['One.', 'Two.'])
        (fix / 'merged').touch()
        out = listing()
        self.assertEqual([d['findings'][0][-4:] for d in out['drafts']], ['Two.'])
        self.assertEqual({f['text']: f['swept'] for f in out['findings']},
                         {'One.': {'task': 'sweep-1', 'finding': 'S1'}, 'Two.': None})
        self.assertEqual(out['summary']['swept'], 1)


# Stands in for forge-parallel.sh: each task ends with the next outcome in outcomes/<name> (PASS when none
# is left), and `_sweep` adds the ids in sweep-ids as fix tasks, the way forge-sweep.py plan does.
WORKER = r'''#!/bin/bash
cmd="$1"; plan="$2"; name="$3"; t="$plan/tasks/$name"
case "$cmd" in
  _task)
    outcome="$(head -1 "$plan/outcomes/$name" 2>/dev/null)"
    [ -f "$plan/outcomes/$name" ] && { tail -n +2 "$plan/outcomes/$name" > "$plan/o.tmp"; mv "$plan/o.tmp" "$plan/outcomes/$name"; }
    echo "${outcome:-PASS}" > "$t/status"
    echo "$name end ${outcome:-PASS}" >> "$plan/events.log" ;;
  _merge)
    [ "$(cat "$t/status")" = PASS ] && { touch "$t/merged"; echo "$name merged" >> "$plan/events.log"; } ;;
  _prepare_retry)
    rm -f "$t/status"; echo "$name retried" >> "$plan/events.log" ;;
  _waves) ;;
  _sweep)
    echo "sweep ${*:3}" >> "$plan/events.log"
    mkdir -p "$plan/sweep"; echo '{}' > "$plan/sweep/plan.json"
    for id in $(cat "$plan/sweep-ids" 2>/dev/null); do
      printf '%s\t-\tlow\tfix_%s\tsol\topus\tFix\n' "$id" "$id" >> "$plan/tasks.tsv"
      mkdir -p "$plan/tasks/$id"; echo '{"findings": []}' > "$plan/tasks/$id/sweep.json"; echo base > "$plan/tasks/$id/base_ref"
      echo "$id"
    done ;;
esac
'''


class Scheduler(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve(); self.plan = self.root / 'plan'
        (self.plan / 'outcomes').mkdir(parents=True)
        (self.plan / 'wt_root').write_text(str(self.root))
        (self.plan / 'tasks.tsv').write_text('a\t-\tlow\tf_a\tsol\topus\tA\nb\t-\tlow\tf_b\tsol\topus\tB\n')
        for name in ('a', 'b'):
            (self.plan / 'tasks' / name).mkdir(parents=True)
            (self.plan / 'tasks' / name / 'base_ref').write_text('base\n')
        (self.plan / 'sweep-ids').write_text('sweep-1 sweep-2\n')
        self.worker = self.root / 'worker.sh'; self.worker.write_text(WORKER)

    def outcomes(self, name, *values):
        (self.plan / 'outcomes' / name).write_text(''.join(v + '\n' for v in values))

    def schedule(self, *extra):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/forge-schedule.py'), str(self.worker), str(self.plan),
                                 '3', *map(str, extra)], capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, FORGE_DATASET='off'))
        log = self.plan / 'events.log'
        return result, log.read_text().splitlines() if log.exists() else []

    def test_after_the_last_merge_the_sweep_queues_its_fix_tasks_and_their_failures_do_not_fail_the_run(self):
        self.outcomes('sweep-2', 'FAIL')
        result, events = self.schedule('--sweep', 'P3')
        self.assertEqual(result.returncode, 0, result.stderr)
        sweep = events.index('sweep --severity P3 --max 5')
        self.assertLess(max(events.index('a merged'), events.index('b merged')), sweep)
        self.assertEqual(sorted(events[sweep + 1:]), ['sweep-1 end PASS', 'sweep-1 merged', 'sweep-2 end FAIL'])
        self.assertEqual([e for e in events if e.startswith('sweep ')], ['sweep --severity P3 --max 5'])   # one round

    def test_without_sweep_the_scheduler_never_plans_one(self):
        result, events = self.schedule()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(events), ['a end PASS', 'a merged', 'b end PASS', 'b merged'])

    def test_no_sweep_while_a_task_has_not_merged_or_once_the_plan_was_swept(self):
        self.outcomes('b', 'FAIL')
        result, events = self.schedule('--sweep', 'P3')
        self.assertEqual(result.returncode, 5)
        self.assertFalse(any(e.startswith('sweep ') for e in events))
        (self.plan / 'events.log').unlink()
        (self.plan / 'tasks/b/status').unlink()
        (self.plan / 'sweep').mkdir(); (self.plan / 'sweep/plan.json').write_text('{}')
        result, events = self.schedule('--sweep', 'P3')
        self.assertEqual((result.returncode, events), (0, ['b end PASS', 'b merged']))

    def test_a_run_whose_tasks_all_merged_earlier_is_swept_when_asked(self):
        for name in ('a', 'b'):
            (self.plan / 'tasks' / name / 'status').write_text('PASS\n')
            (self.plan / 'tasks' / name / 'merged').touch()
        result, events = self.schedule('--sweep', 'P3')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(events[0], 'sweep --severity P3 --max 5')
        self.assertEqual(sorted(events[1:]), ['sweep-1 end PASS', 'sweep-1 merged', 'sweep-2 end PASS', 'sweep-2 merged'])

    def test_the_planner_gets_the_sweep_flags_and_a_fix_task_retries_on_the_run_budget(self):
        (self.plan / 'sweep-ids').write_text('sweep-1\n')
        self.outcomes('sweep-1', 'FAIL', 'PASS')
        result, events = self.schedule('--sweep', 'P1', '--sweep-max', '2', '--sweep-plausible', '--retry-failed', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sweep --severity P1 --max 2 --plausible', events)
        self.assertEqual(events[-4:], ['sweep-1 end FAIL', 'sweep-1 retried', 'sweep-1 end PASS', 'sweep-1 merged'])


class EndToEnd(unittest.TestCase):
    """Real runs: task a's reviewer tolerates a P3 finding on a.txt under --qa-threshold P2."""
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    plan = fixtures.ForgeTests.plan

    def sweep_run(self, fix_review):
        self.git('remote', 'add', 'origin', 'git@github.com:o/r.git')
        self.env['XDG_STATE_HOME'] = str(self.root / 'state')
        self.scenario({'a.qa.1': {'findings': '- [P3][CONFIRMED] a.txt:1 — The greeting is lower case.', 'verdict': 'FAIL'},
                       'change.qa.*': fix_review})
        plan = self.plan('-')
        result = self.run_script('forge-parallel.sh', 'run', plan, '--qa-threshold', 'P2', '--sweep', 'P3')
        issues = subprocess.run([sys.executable, str(ISSUES), str(plan), '--offline', '--json'], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(issues.returncode, 0, issues.stderr)
        return plan, result, json.loads(issues.stdout)

    def test_a_run_with_sweep_fixes_the_tolerated_finding_and_forge_issues_leaves_it_out(self):
        plan, result, issues = self.sweep_run({'findings': '- S1: FIXED', 'verdict': 'PASS'})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((plan / 'tasks/sweep-1/merged').exists(), True)
        self.assertIn('sweep-1\t-\tlow\ta.txt\tsol\topus\t', (plan / 'tasks.tsv').read_text())
        review = (plan / 'tasks/sweep-1/qa.input').read_text()
        self.assertIn('This is a finding sweep.', review)
        self.assertIn('- S1 [P3][CONFIRMED] a.txt:1 — The greeting is lower case.  (task a)', review)
        self.assertIn('forge: sweep: ## sweep-1: a.txt', result.stdout)
        self.assertIn('finding sweep (P3 and worse): 1 fix task(s), 1 of 1 finding(s) fixed; 0 left for forge issues',
                      result.stdout)
        self.assertEqual(issues['drafts'], [])
        self.assertEqual([f['swept'] for f in issues['findings']], [{'task': 'sweep-1', 'finding': 'S1'}])

    def test_a_fix_task_that_fails_its_review_leaves_the_finding_and_the_run_passes(self):
        plan, result, issues = self.sweep_run({'findings': '- S1: NOT FIXED — still lower case', 'verdict': 'FAIL'})
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((plan / 'tasks/sweep-1/status').read_text().strip(), 'FAIL')
        self.assertEqual([d['findings'][0] for d in issues['drafts']],
                         ['[P3][CONFIRMED] a.txt:1 — The greeting is lower case.'])

    def test_sweep_ahead_hands_a_later_task_the_open_findings_in_its_files(self):
        self.scenario({'a.qa.1': {'findings': '- [P3][CONFIRMED] a.txt:1 — The greeting is lower case.', 'verdict': 'FAIL'}})
        plan = self.plan('a', ('a.txt', 'a.txt,b.txt'))
        result = self.run_script('forge-parallel.sh', 'run', plan, '--qa-threshold', 'P2', '--sweep-ahead')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual((plan / 'sweep_ahead').read_text(), 'on\n')
        finding = '- [P3][CONFIRMED] a.txt:1 — The greeting is lower case.  (task a)'
        self.assertIn('## Open findings on your files', (plan / 'tasks/b/dwarf.input').read_text())
        self.assertIn(finding, (plan / 'tasks/b/dwarf.input').read_text())
        self.assertIn(finding, (plan / 'tasks/b/qa.input').read_text())
        self.assertNotIn('Open findings', (plan / 'tasks/a/dwarf.input').read_text())
        self.assertFalse((plan / 'sweep').exists())                         # the prompt step plans no sweep

    def test_the_sweeps_implementer_is_checked_before_any_model_is_paid_for(self):
        plan = self.plan('-')
        (plan / 'fractal-routing.json').write_text(json.dumps({'low': ['literal:typo:codex']}))
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', plan, '--dry-run', '--sweep', 'P3').returncode, 0)
        result = self.run_script('forge-parallel.sh', 'run', plan, '--sweep', 'P3')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse((self.root / 'calls').exists())
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', plan).returncode, 0)      # no sweep, no check

    def test_sweep_options_need_sweep(self):
        plan = self.plan('-')
        result = self.run_script('forge-parallel.sh', 'run', plan, '--sweep-max', '2')
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn('--sweep-plausible and --sweep-max go with --sweep', result.stdout)
        self.assertFalse((self.root / 'calls').exists())


if __name__ == '__main__':
    unittest.main()
