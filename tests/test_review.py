"""The opt-in holistic final review: `review <plan>` and `integrate --final-review <spec>`.

Per-task QA sees one slice at a time; the final review looks at the COMBINED result with
the goal and every task's requirements in front of it. It spends model quota, so nothing
here may run unless asked for. Uses the fake agent CLI of test_forge.py, driven per call
through SCENARIO.

The fake names a call after the FIRST task marker in the prompt (a, then b, c, d). The
final review's prompt contains every task's requirements, so its QA call is keyed
`a.qa.<n>` where n counts every QA call whose prompt mentions TASK_a, and only while a
scenario is set. Plans that run first (a's own QA is a.qa.1) have their review at a.qa.2;
a later review is the next number, or `a.qa.*` once the per-task calls are over.

A merged plan costs several seconds to build, so tests that only READ it (every `review`
leaves the plan's repository state alone, which is asserted) run several reviews on one."""
import re
import subprocess
import unittest
from pathlib import Path

import test_forge as fixtures

ROOT = Path(__file__).resolve().parents[1]
# What a claude reviewer (the `opus` spec) emits when its quota runs out: an is_error result.
QUOTA = {'rc': 1, 'is_error': True, 'verdict': 'none', 'findings': "You've hit your usage limit. Try again in 2h 5m."}
HAS_INFRA = 'class=' in (ROOT / 'scripts' / 'forge-dispatch.sh').read_text()
FINAL = 'a.qa.2'          # the first review, after a's own QA call
FINAL_AGAIN = 'a.qa.3'    # the second
ANY_REVIEW = 'a.qa.*'     # every review, once the per-task QA calls are over
P1 = '- [P1][CONFIRMED] a.txt:1 — the combined result drops the write (a then b)'
P3 = '- [P3][CONFIRMED] a.txt:1 — cosmetic naming (none)'
UNLABELLED = '- [CONFIRMED] a.txt:1 — no severity given (x)'
FOUR = ['dwarf', 'qa', 'dwarf', 'qa']      # a merged two-task plan, before any review


class ReviewTests(unittest.TestCase):
    setUp = fixtures.ForgeTests.setUp
    git = fixtures.ForgeTests.git
    run_script = fixtures.ForgeTests.run_script
    scenario = fixtures.ForgeTests.scenario
    calls = fixtures.ForgeTests.calls

    def plan_of(self, rows, goal=None, specs=None):
        """rows: (id, deps, files). One prompt per task carrying TASK_<id> for the fake."""
        p = self.root / 'plan'; p.mkdir()
        specs = specs or {}
        (p / 'tasks.tsv').write_text(''.join(
            f'{i}\t{d}\tlow\t{f}\tsol\t{specs.get(i, "opus")}\t{i.upper()}\n' for i, d, f in rows))
        for i, _d, _f in rows:
            t = p / 'tasks' / i; t.mkdir(parents=True)
            (t / 'prompt.md').write_text('Implement change. REQUIREMENT_SENTINEL TASK_' + i)
        if goal:
            (p / 'goal.txt').write_text(goal + '\n')
        r = self.run_script('forge-parallel.sh', 'plan', p, '--repo', self.repo, '--no-memory')
        self.assertEqual(r.returncode, 0, r.stdout)
        return p

    def merged_plan(self, *run_args, goal='GOAL_SENTINEL ship the combined feature', specs=None):
        """a (a.txt) then b (b.txt), both merged; the per-task QA calls are spent."""
        if 'SCENARIO' not in self.env:
            self.scenario({})            # the fake only counts calls while a scenario is set
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')], goal=goal, specs=specs)
        r = self.run_script('forge-parallel.sh', 'run', p, *run_args)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), FOUR)
        return p

    def text(self, plan, name):
        return (plan / name).read_text().strip()

    def latest(self, plan, pattern='final-review-*'):
        """The newest artifact directory matching the pattern."""
        dirs = sorted(plan.glob(pattern), key=lambda d: d.stat().st_mtime_ns)
        self.assertTrue(dirs, 'nothing matching %s in %s' % (pattern, plan))
        return dirs[-1]

    def state(self, plan):
        """Everything a read-only review must leave alone."""
        wt = Path(self.text(plan, 'wt_root')) / '_integration'
        return {
            'head': self.git('rev-parse', 'HEAD'),
            'branch': self.git('rev-parse', '--abbrev-ref', 'HEAD'),
            'status': self.git('status', '--porcelain'),
            'int': self.git('rev-parse', 'forge/%s-integration' % self.text(plan, 'run_id')),
            'branches': self.git('branch', '--list'),
            'accepted': self.text(plan, 'accepted.integration'),
            'int_status': subprocess.check_output(['git', 'status', '--porcelain'], cwd=wt, env=self.env),
        }

    def review(self, plan, *args):
        return self.run_script('forge-parallel.sh', 'review', plan, *args)

    def integrate(self, plan, *args):
        return self.run_script('forge-parallel.sh', 'integrate', plan, '--approved', *args)

    def user_commit(self):
        (self.repo / 'user.txt').write_text('user work\n'); self.git('add', '.'); self.git('commit', '-qm', 'user')

    # --- what `review` produces ------------------------------------------------------------
    def test_review_runs_one_qa_call_and_writes_artifacts_and_a_complete_prompt(self):
        self.scenario({'a.qa.1': {'verdict': 'FAIL', 'findings': P3}})        # a's own review tolerates a P3
        p = self.merged_plan('--qa-threshold', 'P2', '--verify', 'test -f a.txt && test -f b.txt')
        self.assertTrue((p / 'tasks/a/known_issues.md').exists())
        before = self.state(p)
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), FOUR + ['qa'])                          # exactly one more call
        self.assertEqual(self.state(p), before)                                # read-only
        out = self.latest(p)
        for name in ('qa.input', 'qa.last', 'qa.out', 'verdict', 'review.meta', 'source.fingerprint'):
            self.assertTrue((out / name).is_file(), name)
        self.assertEqual(self.text(out, 'verdict'), 'PASS')
        self.assertEqual(self.text(p, 'final-review.verdict'), 'PASS')
        self.assertEqual((p / 'final-review').resolve(), out.resolve())        # points at the latest
        self.assertIn(str(out / 'qa.last'), r.stdout)
        self.assertIn('verdict PASS', r.stdout)
        self.assertIn('integrate', r.stdout)                                   # says how to go on
        meta = self.text(out, 'review.meta')
        self.assertIn('spec=opus', meta)                                       # the spec every task shares
        self.assertEqual(self.text(p, 'final_qa'), 'opus')                     # ... and it is remembered
        self.assertIn('base=' + self.text(p, 'integration.base'), meta)
        self.assertIn('tip=' + self.text(p, 'accepted.integration'), meta)
        # the reviewer works in a disposable snapshot of the combined result, not in any worktree
        review = next(out.glob('review-*'))
        self.assertTrue((review / 'a.txt').exists() and (review / 'b.txt').exists())
        self.assertIn('repo=' + str(review), (out / 'qa.resolved').read_text())
        self.assertIn('yolo=off', (out / 'qa.resolved').read_text())

        prompt = (out / 'qa.input').read_text()
        self.assertIn('GOAL_SENTINEL ship the combined feature', prompt)
        for marker in ('TASK_a', 'TASK_b'):
            self.assertIn(marker, prompt)
        self.assertIn('REQUIREMENT_SENTINEL', prompt)
        self.assertIn('cosmetic naming', prompt)                               # a's accepted known issue
        self.assertIn('Context the per-task reviewers did not have', prompt)
        self.assertIn('status: PASS', prompt)                                  # the verification summary
        self.assertIn('test -f a.txt && test -f b.txt', prompt)
        self.assertIn('whole-run review', prompt)
        self.assertIn('COMBINED result', prompt)
        self.assertIn('interactions between tasks', prompt)
        self.assertIn('Blocking threshold: P2', prompt)                        # the plan's threshold
        self.assertIn('```diff', prompt)
        self.assertIn('+implemented', prompt)                                  # the combined diff
        self.assertNotIn(self.text(p, 'wt_root') + '/_integration', prompt)
        self.assertLess(prompt.index('GOAL_SENTINEL'), prompt.index('whole-run review'))   # "tasks above"
        self.assertLess(prompt.index('whole-run review'), prompt.index('```diff'))

    def test_huge_requirements_are_truncated_visibly_and_the_short_ones_are_not(self):
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        big = p / 'tasks/b/prompt.md'
        big.write_text('Implement change. TASK_b ' + 'x' * 100_000 + ' TAIL_OF_B')
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        prompt = (self.latest(p) / 'qa.input').read_text()
        markers = re.findall(r'\[\.\.\. truncated (\d+) bytes; full text at (\S+) \.\.\.\]', prompt)
        self.assertEqual(len(markers), 1, markers)                             # only the long one
        self.assertEqual(markers[0][1], str(big))
        self.assertGreater(int(markers[0][0]), 15_000)
        self.assertNotIn('TAIL_OF_B', prompt)
        self.assertIn('REQUIREMENT_SENTINEL TASK_a', prompt)                   # the short one is whole
        self.assertLess(len(prompt), 100_000)                                  # the budget is ~80 KB, not 100 KB+

    def test_a_diff_too_large_to_inline_tells_the_reviewer_how_to_read_it(self):
        self.scenario({'a.dwarf.1': {'content': 'y' * 450_000}})
        p = self.plan_of([('a', '-', 'a.txt'), ('b', 'a', 'b.txt')])
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 0)
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        out = self.latest(p)
        prompt = (out / 'qa.input').read_text()
        self.assertNotIn('```diff', prompt)
        self.assertIn('git diff HEAD~1 HEAD', prompt)
        self.assertLess(len(prompt), 100_000)
        # ... and the instruction is true: HEAD~1..HEAD of the review checkout is the combined diff
        changed = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD~1', 'HEAD'],
                                          cwd=next(out.glob('review-*')), env=self.env, text=True)
        self.assertEqual(sorted(changed.split()), ['a.txt', 'b.txt'])

    # --- verdicts and exit codes -----------------------------------------------------------
    def test_verdicts_exit_codes_and_the_threshold_gate(self):
        p = self.merged_plan()
        before = self.state(p)
        cases = [   # (what the reviewer says, extra args, exit code, verdict file)
            ({'verdict': 'FAIL', 'findings': P1}, [], 5, 'FAIL'),
            ({'verdict': 'none', 'findings': 'looks fine, but no verdict line'}, [], 5, 'UNKNOWN'),
            ({'verdict': 'FAIL', 'findings': P3}, [], 5, 'FAIL'),                     # no threshold: a FAIL is a FAIL
            ({'verdict': 'FAIL', 'findings': P3}, ['--qa-threshold', 'P2'], 0, 'ACCEPTED'),
            ({'verdict': 'FAIL', 'findings': P1 + '\n' + P3}, [], 5, 'FAIL'),         # P1 beats the persisted P2
            ({'verdict': 'FAIL', 'findings': UNLABELLED}, [], 5, 'FAIL'),             # unlabelled is never tolerated
            ({}, [], 0, 'PASS'),
        ]
        seen = []
        for said, args, code, verdict in cases:
            with self.subTest(verdict=verdict, said=said, args=args):
                self.scenario({ANY_REVIEW: said})
                r = self.review(p, *args)
                self.assertEqual(r.returncode, code, r.stdout)
                out = self.latest(p)
                seen.append(out)
                self.assertEqual(self.text(out, 'verdict'), verdict)
                self.assertEqual(self.text(p, 'final-review.verdict'), verdict)
                self.assertEqual((p / 'final-review').resolve(), out.resolve())
                self.assertEqual(self.state(p), before)                              # nothing moved, whatever it said
                self.assertIn(str(out / 'qa.last'), r.stdout)
                if code == 5:
                    self.assertIn('follow-up', r.stdout)
                    self.assertIn('retry', r.stdout)
                if verdict == 'ACCEPTED':
                    self.assertIn('cosmetic naming', (out / 'known_issues.md').read_text())
                    self.assertTrue(self.text(out, 'qa.gate').startswith('P2\taccept'))
                    self.assertIn('Blocking threshold: P2', (out / 'qa.input').read_text())
        self.assertEqual(len(set(seen)), len(cases))                                 # every review kept its own directory
        self.assertEqual(self.calls().count('qa'), 2 + len(cases))

    def test_a_reviewer_that_modifies_a_checkout_invalidates_the_review(self):
        p = self.merged_plan()
        self.env['MUTATE'] = '1'                                                    # only the review runs from here on
        r = self.review(p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(self.latest(p), 'verdict'), 'INVALIDATED')
        self.assertEqual(self.text(p, 'final-review.verdict'), 'INVALIDATED')
        del self.env['MUTATE']
        self.env['SOURCE'] = str(Path(self.text(p, 'wt_root')) / '_integration')    # now it edits the source checkout
        r = self.review(p)
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.text(self.latest(p), 'verdict'), 'INVALIDATED')

    @unittest.skipUnless(HAS_INFRA, 'the dispatch layer does not classify infrastructure failures yet')
    def test_an_infrastructure_stop_judges_nothing_and_says_how_to_retry(self):
        p = self.merged_plan()
        parent = self.git('rev-parse', 'HEAD')
        self.scenario({FINAL: QUOTA})
        r = self.review(p)
        self.assertEqual(r.returncode, 8, r.stdout)
        out = self.latest(p)
        self.assertFalse((out / 'verdict').exists())                               # nothing was judged
        self.assertFalse((p / 'final-review.verdict').exists())
        self.assertIn('class=quota', (out / 'qa.infra').read_text())
        self.assertIn('infrastructure', r.stdout)
        self.assertIn('forge-parallel.sh review', r.stdout)                        # how to retry
        # the same stop while integrating: exit 8, the user's branch untouched, the exact command to re-run
        self.scenario({FINAL_AGAIN: QUOTA})
        r = self.integrate(p, '--final-review', 'opus')
        self.assertEqual(r.returncode, 8, r.stdout)
        self.assertEqual(parent, self.git('rev-parse', 'HEAD'))
        self.assertFalse((self.repo / 'b.txt').exists())
        self.assertIn('integrate %s --approved --final-review opus' % p, r.stdout)
        self.assertFalse((self.latest(p, 'integrate-*/final-review') / 'verdict').exists())
        # after the fix the same command goes through
        self.scenario({'a.qa.4': {}})
        r = self.integrate(p, '--final-review', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotEqual(parent, self.git('rev-parse', 'HEAD'))

    # --- spec resolution -------------------------------------------------------------------
    def test_spec_resolution_and_yolo(self):
        p = self.merged_plan(specs={'b': 'sol'})
        # tasks use two different specs: ask for one instead of guessing, and spend nothing
        r = self.review(p)
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertIn('opus', r.stdout); self.assertIn('sol', r.stdout)
        self.assertIn('--qa', r.stdout)
        self.assertEqual(self.calls(), FOUR)
        self.assertFalse((p / 'final_qa').exists())
        # a spec that cannot resolve fails in the offline preflight, before any dispatch
        for bad in ('nosuchalias', 'opus::openclaude'):                              # unknown alias; harness not installed
            r = self.review(p, '--qa', bad)
            self.assertNotEqual(r.returncode, 0, (bad, r.stdout))
            self.assertEqual(self.calls(), FOUR)
        # --qa wins and is remembered
        r = self.review(p, '--qa', 'sol')
        self.assertEqual(r.returncode, 0, r.stdout)
        out = self.latest(p)
        self.assertEqual(self.text(p, 'final_qa'), 'sol')
        self.assertIn('spec=sol', self.text(out, 'review.meta'))
        self.assertIn('yolo=off', (out / 'qa.resolved').read_text())
        # the next bare review uses what was remembered: the mixed table no longer matters
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('spec=sol', self.text(self.latest(p), 'review.meta'))
        self.assertEqual(self.calls().count('qa'), 4)
        # an explicit --qa still beats the remembered one
        r = self.review(p, '--qa', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.text(p, 'final_qa'), 'opus')
        # --yolo-qa reaches the dispatch and is remembered for integrate --final-review. Under
        # --yolo the reviewer loses its read-only flag, so the fake takes the call for an
        # implementer: its verdict has to come from the dwarf script.
        self.scenario({'a.dwarf.2': {'message': 'FORGE_VERDICT: PASS'}})
        r = self.review(p, '--yolo-qa')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('yolo=ON', (self.latest(p) / 'qa.resolved').read_text())
        self.assertTrue((p / 'final_yolo').exists())

    # --- preconditions ---------------------------------------------------------------------
    def test_a_plan_that_never_ran_has_nothing_to_review(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.review(p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_run_with_nothing_merged_has_nothing_to_review(self):
        self.scenario({'a.qa.*': {'verdict': 'FAIL'}})
        p = self.plan_of([('a', '-', 'a.txt')])
        self.assertEqual(self.run_script('forge-parallel.sh', 'run', p).returncode, 5)
        r = self.review(p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.calls(), ['dwarf', 'qa'])

    def test_option_errors_old_plans_and_a_moved_integration_branch(self):
        p = self.merged_plan()
        r = self.review(p, '--bogus')
        self.assertEqual(r.returncode, 2, r.stdout)
        self.assertEqual(len(self.calls()), 4)
        # a plan from before integration.base existed falls back to the merge-base, and says so
        (p / 'integration.base').unlink()
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('merge-base', r.stdout)
        self.assertIn('base=' + self.git('rev-parse', 'HEAD').decode().strip(), self.text(self.latest(p), 'review.meta'))
        # an integration branch that moved after forge accepted it is not what forge merged
        wt = Path(self.text(p, 'wt_root')) / '_integration'
        subprocess.run(['git', 'commit', '--allow-empty', '-qm', 'unreviewed'], cwd=wt, env=self.env, check=True)
        r = self.review(p)
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertEqual(self.calls().count('qa'), 3)

    def test_tasks_that_are_not_merged_are_named_as_outside_the_review(self):
        self.scenario({'b.qa.*': {'verdict': 'FAIL'}})
        p = self.plan_of([('a', '-', 'a.txt'), ('b', '-', 'b.txt')], goal='GOAL_SENTINEL')
        self.run_script('forge-parallel.sh', 'run', p)
        r = self.review(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn('NOT part of the reviewed result', r.stdout)
        self.assertIn('b(FAIL)', r.stdout)
        prompt = (self.latest(p) / 'qa.input').read_text()
        self.assertIn('FAIL: NOT in the diff below', prompt)                       # the reviewer is told too
        self.assertIn('TASK_b', prompt)

    def test_the_flag_belongs_to_integrate_and_needs_a_spec(self):
        p = self.plan_of([('a', '-', 'a.txt')])
        r = self.run_script('forge-parallel.sh', 'run', p, '--final-review', 'opus')
        self.assertEqual(r.returncode, 2, r.stdout)
        r = self.integrate(p, '--final-review')
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), [])

    # --- integrate --final-review ----------------------------------------------------------
    def test_integrate_with_a_passing_final_review_fast_forwards_what_will_land(self):
        p = self.merged_plan()
        self.user_commit()
        parent = self.git('rev-parse', 'HEAD')
        r = self.integrate(p, '--final-review', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), FOUR + ['qa'])
        self.assertNotEqual(parent, self.git('rev-parse', 'HEAD'))
        self.assertTrue((self.repo / 'a.txt').exists() and (self.repo / 'b.txt').exists())
        art = self.latest(p, 'integrate-*/final-review')
        self.assertEqual(self.text(art, 'verdict'), 'PASS')
        self.assertIn('TASK_a', (art / 'qa.input').read_text())
        self.assertIn('merged candidate', r.stdout)
        self.assertEqual(self.text(p, 'final_qa'), 'opus')
        self.assertEqual(self.text(p, 'final-review.verdict'), 'PASS')
        # parent..candidate: what lands on the user's branch, not the user's own history
        files = subprocess.check_output(['git', 'diff', '--name-only', 'HEAD~1', 'HEAD'],
                                        cwd=next(art.glob('review-*')), env=self.env, text=True).split()
        self.assertEqual(sorted(files), ['a.txt', 'b.txt'])

    def test_integrate_without_the_flag_or_after_a_failed_verification_spends_no_review(self):
        p = self.merged_plan('--verify', 'true')
        parent = self.git('rev-parse', 'HEAD')
        (p / 'verify_cmd').write_text('exit 9')                                     # integration re-verifies the candidate
        r = self.integrate(p, '--final-review', 'opus')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.calls(), FOUR)                                        # the review comes after verification
        # a spec that cannot resolve stops it before any dispatch, branch untouched
        (p / 'verify_cmd').write_text('true')
        r = self.integrate(p, '--final-review', 'nosuchalias')
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertEqual(parent, self.git('rev-parse', 'HEAD'))
        self.assertEqual(self.calls(), FOUR)
        # asked for nothing, spends nothing: a plain integrate makes no model call
        self.assertFalse(list(p.glob('final-review*')))
        r = self.integrate(p)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.calls(), FOUR)
        self.assertNotEqual(parent, self.git('rev-parse', 'HEAD'))

    def test_integrate_stops_on_a_review_that_blocks_and_leaves_the_branch_alone(self):
        p = self.merged_plan('--qa-threshold', 'P2')
        self.user_commit()                                                          # so the candidate is a real merge
        before = self.state(p)
        blocking = [
            ({'verdict': 'FAIL', 'findings': P1}, 'FAIL'),
            ({'verdict': 'FAIL', 'findings': UNLABELLED}, 'FAIL'),
            ({'verdict': 'none', 'findings': 'no verdict line'}, 'UNKNOWN'),
        ]
        for said, verdict in blocking:
            with self.subTest(verdict=verdict, said=said):
                self.scenario({ANY_REVIEW: said})
                r = self.integrate(p, '--final-review', 'opus')
                self.assertEqual(r.returncode, 5, r.stdout)
                self.assertEqual(self.state(p), before)                              # HEAD, tree, branches, integration intact
                self.assertFalse((self.repo / 'b.txt').exists())
                art = self.latest(p, 'integrate-*/final-review')
                self.assertEqual(self.text(art, 'verdict'), verdict)
                self.assertIn('untouched', r.stdout)
                self.assertIn(str(art / 'qa.last'), r.stdout)
                self.assertEqual(self.text(p, 'final-review.verdict'), verdict)
        self.env['MUTATE'] = '1'                                                    # the reviewer edits its checkout
        self.scenario({ANY_REVIEW: {}})
        r = self.integrate(p, '--final-review', 'opus')
        self.assertEqual(r.returncode, 5, r.stdout)
        self.assertEqual(self.state(p), before)
        self.assertEqual(self.text(self.latest(p, 'integrate-*/final-review'), 'verdict'), 'INVALIDATED')
        del self.env['MUTATE']
        # a FAIL made only of findings below the plan's threshold is accepted, and then it merges
        self.scenario({ANY_REVIEW: {'verdict': 'FAIL', 'findings': P3}})
        r = self.integrate(p, '--final-review', 'opus')
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotEqual(before['head'], self.git('rev-parse', 'HEAD'))
        art = self.latest(p, 'integrate-*/final-review')
        self.assertEqual(self.text(art, 'verdict'), 'ACCEPTED')
        self.assertTrue((art / 'known_issues.md').exists())


if __name__ == '__main__':
    unittest.main()
